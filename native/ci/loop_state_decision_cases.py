#!/usr/bin/env python3
"""全コマンド共通の決定(2026-11-11〜12)の適用と、正式実装レビュー r3 の 3 分類を、同じ実入力で旧新に当てる。

起動の失敗の Current thread・PYTHONHASHSEED・PYTHONOPTIMIZE・SIGINT/SIGTERM・時間切れ・
状態の保存の途中の書込みの失敗・onbranch の一時の置き場・Git 以外の子を起動しないこと・
r3(非UTF-8名の診断・保持fdからの列挙・realpath の再帰)を比べる。
資源の上限(RLIMIT_NOFILE 等)は設定しない。RLIMIT_FSIZE は書込みの失敗の case だけで使う。
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import check_loop_state as check
import loop_state_arguments as arguments
import loop_state_contract_cases as contracts

REAL_GIT = shutil.which("git") or "/usr/bin/git"
CURRENT = re.compile(rb"Current thread 0x([0-9a-f]{16}) \(most recent call first\):\n")
STARTUP_STDIN = (b"Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\n"
                 b"Python runtime state: core initialized\n\n")
STARTUP_STREAM = (b"Fatal Python error: init_sys_streams: can't initialize sys standard streams\n"
                  b"Python runtime state: core initialized\nIsADirectoryError: [Errno 21] Is a directory: %d\n\n")
STARTUP_TAIL = b"  <no Python frame>\n"
HASH_FAILURE = (b'Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be "random" or an integer '
                b"in range [0; 4294967295]\nPython runtime state: preinitialized\n\n")
TIMEOUT = re.compile(rb"timed out after ([^ ]+) seconds\n")
# Python の float の repr の形だけ(整数だけの字面は float の repr ではない)。
FLOAT_REPR = re.compile(rb"[0-9]+\.[0-9]+|[0-9](?:\.[0-9]+)?e[-+][0-9]{2,}")

# 停止する検査用の git。PATH の先頭に置き、指定の部分コマンドのときだけ止まる。
STALL_GIT = r'''#!/bin/sh
# 書込みの上限の case でも、git 自身は上限の外で動かす(旧版と Rust の書込みだけに当てる)。
if [ -n "$S2L_GIT_UNLIMITED" ]; then ulimit -f unlimited; fi
printf '%s\n' "$0" >> "$S2L_EXEC_LOG"
stall=
for argument in "$@"; do
  if [ -n "$S2L_STALL_ON" ] && [ "$argument" = "$S2L_STALL_ON" ]; then stall=yes; fi
done
if [ -z "$stall" ]; then exec "$S2L_REAL_GIT" "$@"; fi
case "$S2L_STALL_MODE" in
  fail) exit 1 ;;
  wait) exec 1>&-; : > "$S2L_STALL_MARK"; exec /bin/sleep "$S2L_STALL_SECONDS" ;;
  hang) : > "$S2L_STALL_MARK"; exec /bin/sleep "$S2L_STALL_SECONDS" ;;
  pause) : > "$S2L_STALL_MARK"; /bin/sleep "$S2L_STALL_SECONDS"; exec "$S2L_REAL_GIT" "$@" ;;
esac
exit 1
'''


class Runner:
    def __init__(self, fixture, work):
        self.fixture = fixture
        self.work = work
        self.groups = {}
        self.cases = []
        self.boundaries = []
        self.base = arguments.environment()
        self.bindir = work / "stall-bin"
        self.bindir.mkdir()
        wrapper = self.bindir / "git"
        wrapper.write_text(STALL_GIT, encoding="utf-8")
        wrapper.chmod(0o700)
        self.timing = {}
        self.last_seconds = []
        self.exec_log = work / "exec-log"
        self.mark = work / "stall-mark"
        self.base.update({b"PATH": os.fsencode(self.bindir) + b":" + self.base.get(b"PATH", b"/usr/bin:/bin"),
                          b"S2L_REAL_GIT": os.fsencode(REAL_GIT), b"S2L_EXEC_LOG": os.fsencode(self.exec_log),
                          b"S2L_STALL_MARK": os.fsencode(self.mark), b"GIT_CONFIG_NOSYSTEM": b"1",
                          b"GIT_CONFIG_GLOBAL": b"/dev/null", b"HOME": os.fsencode(work),
                          b"XDG_CONFIG_HOME": os.fsencode(work / "xdg")})
        for key in [k for k in self.base if k.startswith(b"GIT_") and k not in (b"GIT_CONFIG_NOSYSTEM", b"GIT_CONFIG_GLOBAL")]:
            self.base.pop(key)

    def env(self, delta=None):
        env = self.base.copy()
        given = {}
        for key, value in (delta or {}).items():
            raw = os.fsencode(key)
            if key.startswith("PYTHON"):
                given[key] = value
            if value is None:
                env.pop(raw, None)
            else:
                env[raw] = os.fsencode(value)
        check.verify_child_environment(env, given)
        return env

    def launch(self, argv, env, *, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               fsize=None, ignore_int=False, signal_after_mark=None, timeout=20):
        """1 回の起動。信号を送る case は、止まる git の印を待ってからプロセスグループへ送る。"""
        if self.mark.exists():
            self.mark.unlink()
        def prepare():
            if fsize is not None:
                resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, resource.RLIM_INFINITY))
            if ignore_int:
                signal.signal(signal.SIGINT, signal.SIG_IGN)
        check.verify_child_environment(env, {})
        started = time.monotonic()
        child = subprocess.Popen(argv, cwd=self.work, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                                 start_new_session=True, preexec_fn=prepare)
        try:
            if signal_after_mark is not None:
                deadline = time.monotonic() + 10
                while not self.mark.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.005)
                if not self.mark.exists():
                    raise AssertionError("S2L_STALL_NOT_REACHED: " + " ".join(map(os.fsdecode, argv[-6:])))
                time.sleep(0.05)
                os.killpg(child.pid, signal_after_mark)
            out, err = child.communicate(timeout=timeout)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=3)
        seconds = time.monotonic() - started
        # 子を残さないこと(プロセスグループが空になる)。回収されない子は親の終了後に消える。
        deadline = time.monotonic() + 3
        while True:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                # Mac(XNU)は、ゾンビだけが残る process group に EPERM を返す。回収されるまで待つ。
                pass
            if time.monotonic() > deadline:
                raise AssertionError("S2L_CHILD_REMAINS: " + " ".join(map(os.fsdecode, argv[-6:])))
            time.sleep(0.01)
        return subprocess.CompletedProcess(argv, child.returncode, out or b"", err or b""), seconds

    def pair(self, label, argv, group, delta=None, output=None, compare=None, **options):
        env = self.env(delta)
        results = []
        states = []
        seconds_pair = []
        check.start_case(label)
        for side, engine in (("old", [sys.executable, "-B", str(check.LEGACY)]), ("new", [str(self.fixture)])):
            check.restore_output(output, None)
            result, seconds = self.launch([*engine, *map(os.fspath, argv)], env, **options)
            check.record(side, result)
            results.append(result)
            seconds_pair.append(seconds)
            states.append(check.output_state(output))
        old, new = results
        check.remember(label, old, new)
        (compare or check.compare_result)(label, old, new)
        if states[0] != states[1]:
            raise check.Mismatch(f"S2L_STATE: {label}: {states[0]!r} != {states[1]!r}")
        self.groups[group] = self.groups.get(group, 0) + 1
        self.cases.append(label)
        self.last_seconds = seconds_pair
        return old, new, states[1]


def initialize(runner, path):
    env = runner.env()
    for argv in [["-c", "init.defaultBranch=main", "init", "-q"], ["config", "user.name", "fixture"],
                 ["config", "user.email", "fixture@example.invalid"]]:
        result = arguments.run([REAL_GIT, "-C", str(path), *argv], env, runner.work)
        assert result.returncode == 0, result.stderr
    (path / "tracked").write_bytes(b"baseline\n")
    for argv in [["add", "tracked"], ["commit", "-qm", "baseline"]]:
        result = arguments.run([REAL_GIT, "-C", str(path), *argv], env, runner.work)
        assert result.returncode == 0, result.stderr


def valid_argv(root, state, out):
    return {"snapshot": ["snapshot", "--out", out, "--top", root, "--common", root, "--repo-admin", root],
            "stop-control": ["stop-control", "--state", state],
            "preflight": ["preflight", "--state", state, "--top", root, "--common", root, "--repo-admin", root],
            "derive-removed-worktree": ["derive-removed-worktree", "--state", state, "--out", out,
                                        "--removed-worktree", "/removed", "--own-worktree", "/removed"],
            "state-digest": ["state-digest", "--state", state],
            "state-digests": ["state-digests", "--state", state],
            "compare": ["compare", "--before", state, "--after", state]}


def startup_compare(dirs):
    """起動の失敗(17:45)。観測できる項目だけを比べ、Current thread の値だけを形式検査にする。"""
    def compare(label, old, new):
        if old.returncode != 1 or new.returncode != 1:
            raise check.Mismatch(f"S2L_STARTUP_EXIT: {label}: {old.returncode} {new.returncode}")
        if 1 not in dirs and old.stdout != new.stdout:
            raise check.Mismatch(f"S2L_STARTUP_STDOUT: {label}")
        if 2 in dirs:
            if old.stderr or new.stderr:
                raise check.Mismatch(f"S2L_STARTUP_STDERR_DIRECTORY: {label}")
            return
        expected_head = STARTUP_STDIN if 0 in dirs else STARTUP_STREAM % 1
        for side, value in (("old", old.stderr), ("new", new.stderr)):
            found = CURRENT.findall(value)
            if len(found) != 1:
                raise check.Mismatch(f"S2L_CURRENT_THREAD_COUNT: {label}/{side}")
            head, _, tail = value.partition(CURRENT.search(value).group(0))
            if head != expected_head or tail != STARTUP_TAIL:
                raise check.Mismatch(f"S2L_STARTUP_TEXT: {label}/{side}: {value!r}")
        masked = [CURRENT.sub(b"Current thread 0x<thread> (most recent call first):\n", v) for v in (old.stderr, new.stderr)]
        if masked[0] != masked[1]:
            raise check.Mismatch(f"S2L_STARTUP_STDERR: {label}")
    return compare


def startup_cases(runner):
    root = runner.work / "startup"
    root.mkdir()
    state = root / "state"
    state.write_bytes(b'{"schema":7,"stop_control":null}')
    out = root / "out"
    combos = [(0,), (1,), (2,), (0, 1), (0, 2), (1, 2), (0, 1, 2)]
    names = {0: "stdin", 1: "stdout", 2: "stderr"}
    for command, argv in valid_argv(root, state, out).items():
        for dirs in combos:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                route = {n: directory if n in dirs else None for n in (0, 1, 2)}
                runner.pair("startup-" + command + "-" + "+".join(names[n] for n in dirs), argv, "startup_directory",
                            output=out, compare=startup_compare(dirs),
                            stdin=route[0] if route[0] is not None else subprocess.DEVNULL,
                            stdout=route[1] if route[1] is not None else subprocess.PIPE,
                            stderr=route[2] if route[2] is not None else subprocess.PIPE)
            finally:
                os.close(directory)


def interpreter_variables(runner):
    """PYTHONHASHSEED(不正値の起動の失敗と有効値の無変化)と PYTHONOPTIMIZE(無変化)。"""
    root = runner.work / "variables"
    root.mkdir()
    state = root / "state"
    state.write_bytes(b'{"schema":7,"stop_control":null}')
    left = root / "before"
    right = root / "after"
    left.write_bytes(contracts.canonical(contracts.BASE))
    right.write_bytes(contracts.canonical({**contracts.BASE, "config": ["changed"]}))
    broken = root / "broken"
    broken.write_bytes(contracts.canonical({**contracts.BASE, "repo_admin": None}))
    right_broken = root / "broken-after"
    right_broken.write_bytes(contracts.canonical({**contracts.BASE, "repo_admin": None, "config": ["changed"]}))
    out = root / "out"
    for command, argv in valid_argv(root, state, out).items():
        old, new, _ = runner.pair("hashseed-bad-" + command, argv, "hashseed", delta={"PYTHONHASHSEED": "bad"}, output=out)
        assert old.returncode == 1 and old.stdout == b"" and old.stderr == HASH_FAILURE, "HASHSEED_FIXED_FAILURE"
    for value in ["-1", "4294967296", "12 ", "0x10", "1_0", "+", " ", "Random", "-18446744069414584320",
                  "-18446744073709551616"]:
        old, _, _ = runner.pair("hashseed-invalid-" + repr(value), ["state-digest", "--state", state], "hashseed",
                                delta={"PYTHONHASHSEED": value})
        assert old.returncode == 1 and old.stderr == HASH_FAILURE, "HASHSEED_INVALID_VALUE"
    samples = {"digest": ["state-digest", "--state", state], "diff": ["compare", "--before", left, "--after", right],
               "uncaught": ["compare", "--before", broken, "--after", right_broken],
               "stop": ["state-digest", "--state", state, "--max-items", "0"], "help": ["snapshot", "--help"]}
    baselines = {}
    for name, argv in samples.items():
        old, _, _ = runner.pair("variables-baseline-" + name, argv, "variables_baseline")
        baselines[name] = (old.returncode, old.stdout, old.stderr)
    for variable, values in [("PYTHONHASHSEED", ["random", "0", "4294967295", " 12", "+5", "-0", "00", "",
                                                  "-18446744073709551615"]),
                             ("PYTHONOPTIMIZE", ["1", "2", "x", "0", ""])]:
        for value in values:
            for name, argv in samples.items():
                old, _, _ = runner.pair(f"{variable}-{value!r}-{name}", argv, variable.lower(), delta={variable: value})
                # 有効な値で旧版の出力が変わらないこと。
                if (old.returncode, old.stdout, old.stderr) != baselines[name]:
                    raise check.Mismatch(f"S2L_VARIABLE_CHANGED_LEGACY: {variable}={value!r}/{name}")


def repository(runner, name, onbranch=False):
    repo = runner.work / name
    repo.mkdir()
    initialize(runner, repo)
    if onbranch:
        (repo / ".git" / "included").write_bytes(b"[s2l]\n\tincluded = yes\n")
        with (repo / ".git" / "config").open("ab") as stream:
            stream.write(b'[includeIf "onbranch:main"]\n\tpath = included\n')
    shared = ["--top", repo, "--common", repo / ".git", "--repo-admin", repo / ".git"]
    return repo, shared


def prior_state(runner, repo, shared, name):
    state = runner.work / name
    result = arguments.run([sys.executable, "-B", str(check.LEGACY), "snapshot", "--out", str(state),
                            *map(os.fspath, shared)], runner.env(), runner.work)
    if sys.platform == "darwin":
        assert result.returncode == 20, result.stderr
        return None
    assert result.returncode == 0 and state.is_file(), result.stderr
    return state


def signal_compare(number):
    def compare(label, old, new):
        if old.returncode != -number or new.returncode != -number:
            raise check.Mismatch(f"S2L_SIGNAL_EXIT: {label}: {old.returncode} {new.returncode}")
        if old.stdout != new.stdout:
            raise check.Mismatch(f"S2L_SIGNAL_STDOUT: {label}")
        if number == signal.SIGTERM:
            if old.stderr != new.stderr:
                raise check.Mismatch(f"S2L_SIGTERM_STDERR: {label}")
            return
        header = b"Traceback (most recent call last):\n"
        for side, value in (("old", old.stderr), ("new", new.stderr)):
            if header not in value or not value.endswith(b"\nKeyboardInterrupt\n"):
                raise check.Mismatch(f"S2L_SIGINT_TRACEBACK: {label}/{side}: {value[-200:]!r}")
        if old.stderr.split(header, 1)[0] != new.stderr.split(header, 1)[0]:
            raise check.Mismatch(f"S2L_SIGINT_BEFORE_HEADER: {label}")
    return compare


def signals(runner):
    """SIGINT(既定・無視で受け継いだ場合)と SIGTERM を、止まる git の待ちの点で送る。"""
    repo, shared = repository(runner, "signal-repo")
    out = runner.work / "signal-out"
    prior = prior_state(runner, repo, shared, "signal-prior")
    if prior is None:
        return
    points = [("snapshot-read", ["snapshot", "--out", out, *shared], "for-each-ref", "hang"),
              ("snapshot-wait", ["snapshot", "--out", out, *shared], "for-each-ref", "wait"),
              ("snapshot-index", ["snapshot", "--out", out, *shared], "diff-index", "hang"),
              ("preflight-read", ["preflight", "--state", prior, *shared], "--no-includes", "hang"),
              ("preflight-wait", ["preflight", "--state", prior, *shared], "--no-includes", "wait")]
    for label, argv, point, mode in points:
        delta = {"S2L_STALL_ON": point, "S2L_STALL_MODE": mode, "S2L_STALL_SECONDS": "30"}
        for number, group in ((signal.SIGINT, "sigint"), (signal.SIGTERM, "sigterm")):
            runner.pair(f"{group}-{label}", argv, group, delta=delta, output=out, compare=signal_compare(number),
                        signal_after_mark=number)
    # 無視(SIG_IGN)で受け継いだ SIGINT は無視のまま。git の子も受け継ぎ、旧新とも普通に終わる。
    delta = {"S2L_STALL_ON": "for-each-ref", "S2L_STALL_MODE": "pause", "S2L_STALL_SECONDS": "1"}
    old, new, saved = runner.pair("sigint-ignored-snapshot", ["snapshot", "--out", out, *shared], "sigint_ignored",
                                  delta=delta, output=out, ignore_int=True, signal_after_mark=signal.SIGINT)
    assert old.returncode == 0 and saved is not None, "SIGINT_IGNORED_NORMAL_COMPLETION"


def timeout_compare(limit):
    """index_snapshot の TimeoutExpired の秒の欄だけを形式と範囲で検査し、ほかは全バイトで比べる。"""
    def compare(label, old, new):
        masked = []
        for side, result in (("old", old), ("new", new)):
            found = TIMEOUT.findall(result.stderr)
            if len(found) != 1 or FLOAT_REPR.fullmatch(found[0]) is None:
                raise check.Mismatch(f"S2L_TIMEOUT_SECONDS_FORM: {label}/{side}: {found!r}")
            value = float(found[0])
            if not 0 < value <= min(10, limit):
                raise check.Mismatch(f"S2L_TIMEOUT_SECONDS_RANGE: {label}/{side}: {value}")
            masked.append(subprocess.CompletedProcess(result.args, result.returncode, result.stdout,
                                                      TIMEOUT.sub(b"timed out after <seconds> seconds\n", result.stderr)))
        check.compare_result(label, *masked)
    return compare


def elapsed_within(runner, label, limit, margin):
    """実時間で上限が効くこと: 旧新とも、上限以上・上限+小さな余裕以下で終わる。"""
    for side, seconds in zip(("old", "new"), runner.last_seconds):
        runner.timing[label + "/" + side] = round(seconds, 3)
        if not limit <= seconds <= limit + margin:
            raise check.Mismatch(f"S2L_TIMEOUT_ELAPSED: {label}/{side}: {seconds:.3f} は [{limit}, {limit + margin}] の外")


def timeouts(runner, full):
    """時間の上限(--seconds と子の 10 秒)。時間切れの位置は止まる git の置き場で決める。"""
    if sys.platform == "darwin":
        return
    repo, shared = repository(runner, "timeout-repo")
    out = runner.work / "timeout-out"
    hang = {"S2L_STALL_MODE": "hang", "S2L_STALL_SECONDS": "40"}
    old, _, _ = runner.pair("timeout-run-git-seconds", ["snapshot", "--out", out, *shared, "--max-seconds", "1"],
                            "timeouts", delta={**hang, "S2L_STALL_ON": "for-each-ref"}, output=out)
    assert old.returncode == 20 and old.stderr == "ERROR [loop-state] git の状態読取が時間内に終わらない\n".encode()
    elapsed_within(runner, "timeout-run-git-seconds", 1, 1.0)
    old, _, _ = runner.pair("timeout-index-seconds", ["snapshot", "--out", out, *shared, "--max-seconds", "2"],
                            "timeouts", delta={**hang, "S2L_STALL_ON": "diff-index"}, output=out,
                            compare=timeout_compare(2))
    assert old.returncode == 1 and b"subprocess.TimeoutExpired: Command" in old.stderr
    elapsed_within(runner, "timeout-index-seconds", 2, 1.0)
    if full:
        # 子の 10 秒の上限が実時間で効くこと。
        old, _, _ = runner.pair("timeout-run-git-10", ["snapshot", "--out", out, *shared], "timeouts_10s",
                                delta={**hang, "S2L_STALL_ON": "for-each-ref"}, output=out, timeout=40)
        assert old.returncode == 20
        elapsed_within(runner, "timeout-run-git-10", 10, 1.0)
        # TimeoutExpired の秒は Popen.wait へ渡した残り(10 よりわずかに小さい float)。
        old, _, _ = runner.pair("timeout-index-10", ["snapshot", "--out", out, *shared], "timeouts_10s",
                                delta={**hang, "S2L_STALL_ON": "diff-index"}, output=out, timeout=40,
                                compare=timeout_compare(60))
        assert old.returncode == 1 and 9 < float(TIMEOUT.search(old.stderr).group(1)) < 10
        elapsed_within(runner, "timeout-index-10", 10, 1.0)


def write_failures(runner):
    """状態の保存の途中の失敗(00:28)。小さい状態と st_blksize の境目を超える状態の両方を全行で比べる。"""
    root = runner.work / "write"
    root.mkdir()
    blocksize = os.stat(root).st_blksize
    out = root / "out"
    for size in (100, 8192 + blocksize + 4000):
        value = {**contracts.DERIVE, "pad": "x" * size}
        source = root / f"state-{size}"
        source.write_bytes(contracts.canonical(value))
        argv = ["derive-removed-worktree", "--state", source, "--out", out, "--removed-worktree", "/removed",
                "--own-worktree", "/removed"]
        for limit in (0, 50, 5000, 13000):
            old, new, saved = runner.pair(f"write-{size}-{limit}", argv, "write_failure", output=out, fsize=limit)
            if limit < size:
                # ulimit -f の下でも信号で止まらず、EFBIG の例外として旧版の経路へ流れる。
                assert old.returncode == 1 and new.returncode == 1 and b"[Errno 27] File too large" in old.stderr
                assert saved is None
            leftovers = [p.name for p in root.iterdir() if p.name.startswith(".out.tmp.")]
            assert not leftovers, "WRITE_TEMP_LEFT"
    repo, shared = repository(runner, "write-repo")
    for limit in (0, 100):
        old, _, saved = runner.pair(f"write-snapshot-{limit}", ["snapshot", "--out", out, *shared], "write_failure",
                                    output=out, fsize=limit)
        assert old.returncode in (1, 20) and saved is None
    return blocksize


def onbranch(runner):
    """onbranch の一時の置き場(00:35)。名前が診断に出ない case だけを比べる。"""
    repo, shared = repository(runner, "onbranch-repo", onbranch=True)
    prior = prior_state(runner, repo, shared, "onbranch-prior")
    if prior is None:
        return
    tmp = runner.work / "onbranch-tmp"
    tmp.mkdir()
    argv = ["preflight", "--state", prior, *shared]
    def leftovers():
        return sorted(p.name for p in tmp.iterdir())
    # 候補が使えれば通常の結果(TMPDIR の下に作って消す)。
    runner.pair("onbranch-normal", argv, "onbranch", delta={"TMPDIR": str(tmp)})
    assert leftovers() == [], "ONBRANCH_TEMP_LEFT"
    # 使えない TMPDIR は飛ばして次の候補へ進む。
    runner.pair("onbranch-missing-tmpdir", argv, "onbranch", delta={"TMPDIR": str(tmp / "absent")})
    # git init の失敗は Stop。init が作る前なので temp は無く、親だけを消す。
    old, _, _ = runner.pair("onbranch-init-failure", argv, "onbranch",
                            delta={"TMPDIR": str(tmp), "S2L_STALL_ON": "init", "S2L_STALL_MODE": "fail"})
    assert old.returncode == 20 and leftovers() == [], "ONBRANCH_INIT_FAILURE_CLEANUP"
    # 試しの書込みが全候補で EFBIG になれば FileNotFoundError(候補の一覧だけが出る)。
    old, _, _ = runner.pair("onbranch-no-usable-directory", argv, "onbranch",
                            delta={"TMPDIR": str(tmp), "TEMP": str(tmp / "temp"), "S2L_GIT_UNLIMITED": "1"}, fsize=0)
    assert old.returncode == 1 and b"FileNotFoundError: [Errno 2] No usable temporary directory found in" in old.stderr
    assert leftovers() == [], "ONBRANCH_NO_USABLE_LEFT"


R4_CONFIGS = {
    # [includeIf](サブセクション無し)の path。旧 key[10:-5] は空の条件で False になる。
    "empty-includeif": b"[includeIf]\n\tpath = absent-target\n",
    # 明示の .encode() の失敗(全文の中の位置と呼出元のフレーム)。
    "include-nonutf8": b"[include]\n\tpath = inc\xff\n",
    "condition-nonutf8": b'[includeIf "gitdir:/nowhere\xff/"]\n\tpath = x\n',
    "hasconfig-nonutf8": b'[includeIf "hasconfig:remote.*.url:https://e\xff/**"]\n\tpath = x\n',
    "remote-nonutf8": b'[remote "o"]\n\turl = https://e\xff.invalid/r\n[includeIf "gitdir:/nowhere/"]\n\tpath = x\n',
    # memfd の書込みの失敗(RLIMIT_FSIZE の EFBIG)。git 自身は上限の外で動かす。
    "efbig-include": b"[include]\n\tpath = inc\n",
    "efbig-gitdir": b'[includeIf "gitdir:/nowhere/"]\n\tpath = x\n',
    "efbig-hasconfig": b'[includeIf "hasconfig:remote.*.url:https://x/**"]\n\tpath = x\n',
    "efbig-onbranch": b'[includeIf "onbranch:main"]\n\tpath = inc\n',
}


def patch_prior(prior, config):
    """旧版の snapshot の後に config を書き換えた場合と同じ基準へ、保存状態の config の記録だけを合わせる。"""
    value = json.loads(prior.read_bytes())
    st = config.lstat()
    sha = hashlib.sha256(config.read_bytes()).hexdigest()
    meta = {"mode": stat.S_IMODE(st.st_mode), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns, "dev": st.st_dev, "ino": st.st_ino}
    names = {"config", "config:" + str(config)}
    def walk(node):
        if isinstance(node, dict):
            if node.get("path") in names and "sha256" in node:
                node["sha256"] = sha
                node["meta"] = meta
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value)
    prior.write_bytes(contracts.canonical(value))


def r4_configs(runner):
    """r4: includeIf の空の条件・明示の encode・memfd の書込みの失敗を、snapshot と preflight で比べる。"""
    for name, raw in R4_CONFIGS.items():
        repo, shared = repository(runner, "r4-" + name)
        (repo / ".git" / "inc").write_bytes(b"[s2l]\n\tv = 1\n")
        out = runner.work / ("r4-out-" + name)
        efbig = name.startswith("efbig-")
        options = {"fsize": 10} if efbig else {}
        delta = {"S2L_GIT_UNLIMITED": "1", "TMPDIR": str(runner.work)} if efbig else {}
        config = repo / ".git" / "config"
        clean = config.read_bytes()
        if name == "empty-includeif":
            linked = runner.work / "r4-linked"
            result = arguments.run([REAL_GIT, "-C", str(repo), "worktree", "add", "-q", "--detach", str(linked), "HEAD"],
                                   runner.env(), runner.work)
            assert result.returncode == 0, result.stderr
        with config.open("ab") as stream:
            stream.write(raw)
        old, _, _ = runner.pair(f"r4-{name}-snapshot", ["snapshot", "--out", out, *shared], "r4_config_snapshot",
                                delta=delta, output=out, **options)
        if name == "empty-includeif" and sys.platform != "darwin":
            assert old.returncode == 0, old.stderr
        if sys.platform == "darwin":
            continue
        # 基準(前の状態)は上限の外の旧版で取る。encode が失敗する config は、基準を取った後に書き換える。
        if name.endswith("nonutf8"):
            config.write_bytes(clean)
        prior = prior_state(runner, repo, shared, "r4-prior-" + name)
        if name.endswith("nonutf8"):
            with config.open("ab") as stream:
                stream.write(raw)
            patch_prior(prior, config)
        if name == "empty-includeif":
            runner.pair(f"r4-{name}-snapshot-prior", ["snapshot", "--out", out, *shared, "--secret-patterns-from", prior],
                        "r4_config_prior", output=out)
            admin = repo / ".git" / "worktrees" / "r4-linked"
            for label, argv in [("top", ["preflight", "--state", prior, *shared]),
                                ("linked", ["preflight", "--state", prior, "--top", linked, "--common", repo / ".git",
                                            "--repo-admin", admin])]:
                old, _, _ = runner.pair(f"r4-{name}-preflight-{label}", argv, "r4_config_prior")
                assert old.returncode == 0, old.stderr
        elif name != "efbig-gitdir":
            # preflight の gitdir は条件を Git に聞かない(None)ので、memfd へ届かない。
            old, _, _ = runner.pair(f"r4-{name}-preflight", ["preflight", "--state", prior, *shared],
                                    "r4_config_prior", delta=delta, **options)
            if name in ("condition-nonutf8", "remote-nonutf8"):
                # preflight の gitdir の条件は Git に聞かず(None)、基準と同じ origin なので通る。
                assert old.returncode == 0, old.stderr
            else:
                assert old.returncode == 1 and (b"UnicodeEncodeError" in old.stderr or b"[Errno 27]" in old.stderr), old.stderr


def preflight_json_depth(runner):
    """r4: preflight の read_state の未捕捉の例外にも 1928 行のフレーム(json19 と同じ深さの境界)。"""
    folder = runner.work / "r4-preflight-json"
    folder.mkdir()
    for label, n, leaf in [("null", 991, "null"), ("null", 992, "null"), ("nan", 990, "NaN"), ("nan", 991, "NaN"),
                           ("empty-object", 991, "{}")]:
        state = folder / f"{label}-{n}"
        state.write_bytes(b'{"schema":7,"x":' + (b"[" * n + leaf.encode() + b"]" * n) + b"}")
        before = arguments.identity(state)
        runner.pair(f"r4-preflight-json-{label}-{n}", ["preflight", "--state", state, "--top", folder, "--common", folder,
                                                     "--repo-admin", folder], "r4_preflight_json")
        assert arguments.identity(state) == before, "PREFLIGHT_JSON_INPUT_CHANGED"


def realpath_surrogate(runner):
    """r4: 循環するリンクの残りに孤立 surrogate があると、旧版は realpath を通し、出力の符号化(2155 行)で止まる。"""
    folder = runner.work / "r4-realpath"
    folder.mkdir()
    (folder / "cycle").symlink_to("cycle")
    state = folder / "state"
    for label, worktree in [("cycle-surrogate", str(folder / "cycle") + "/x\ud800"),
                            ("plain-surrogate", str(folder) + "/x\ud800")]:
        state.write_bytes(contracts.canonical({"schema": 7, "stop_control": {"worktree": worktree, "rel": "stop",
                                                                             "new_dirs": []}}))
        old, _, _ = runner.pair(f"r4-realpath-{label}", ["stop-control", "--state", state], "r4_realpath")
        assert old.returncode == 1 and b"UnicodeEncodeError" in old.stderr
    # prior_secret_patterns(1524)の git_dir: 循環の残りの surrogate は realpath を通り、旧版は辞書の鍵にする。
    repo, shared = repository(runner, "r4-realpath-repo")
    out = folder / "out"
    for label, git_dir in [("prior-cycle-surrogate", str(folder / "cycle") + "/x\ud800"),
                           ("prior-plain-surrogate", str(folder) + "/x\ud800")]:
        prior = folder / ("prior-" + label)
        prior.write_bytes(contracts.canonical({"schema": 7, "secret_patterns": [
            {"path": "/dummy\ud800", "patterns": [".env", ".env.*", ".dev.vars"]}],
            "worktree_configs": [{"path": "/dummy\ud800", "git_dir": git_dir}]}))
        runner.pair(f"r4-realpath-{label}", ["snapshot", "--out", out, *shared, "--secret-patterns-from", prior],
                    "r4_realpath", output=out)


def r5_saved_paths(runner):
    """r5: 保存状態のパスは str のまま鍵・集合・比較に使い、ファイル操作の所でだけ符号化する。"""
    if sys.platform == "darwin":
        return
    repo, shared = repository(runner, "r5-repo")
    out = runner.work / "r5-out"
    prior = prior_state(runner, repo, shared, "r5-prior")
    baseline = json.loads(prior.read_bytes())
    def write(label, value):
        path = runner.work / ("r5-state-" + label)
        path.write_bytes(contracts.canonical(value))
        return path
    def extra_worktrees(rows):
        value = json.loads(json.dumps(baseline))
        value["worktrees"].extend({"worktree": row, "branch": "refs/heads/human", "locked": ""} for row in rows)
        return value
    states = {
        # 符号化できない文字を含む行(旧版は鍵にするだけ)。
        "surrogate-row": extra_worktrees(["/x\ud800"]),
        # 同じバイトになる別の str(旧版は別の鍵)。
        "same-bytes-rows": extra_worktrees(["/x\udcc3\udca9", "/x\u00e9"]),
    }
    # 文脈の行と git-dir の基準まで揃うと、旧版は held_git_dir の open_dir_path で符号化する。
    deep = extra_worktrees(["/x\ud800"])
    deep["worktree_configs"].append({**deep["worktree_configs"][0], "path": "/x\ud800"})
    states["surrogate-context"] = deep
    # 保存済みの config の出典の str に符号化できない文字(旧版は safe_config_file の open_dir_path で符号化)。
    origin = json.loads(json.dumps(baseline))
    item = {**origin["config"]["origins"][0], "path": "config:/x\ud800/config"}
    origin["config"]["origins"].append(item)
    states["surrogate-origin"] = origin
    for label, value in states.items():
        state = write(label, value)
        old, _, _ = runner.pair(f"r5-{label}-snapshot", ["snapshot", "--out", out, *shared, "--secret-patterns-from", state],
                                "r5_saved_paths", output=out)
        if label in ("surrogate-row", "same-bytes-rows"):
            assert old.returncode == 0, old.stderr
        old, _, _ = runner.pair(f"r5-{label}-preflight", ["preflight", "--state", state, *shared], "r5_saved_paths")
        if label in ("surrogate-row", "same-bytes-rows"):
            assert old.returncode == 20 and old.stderr == "ERROR [loop-state] worktree の git-dir の基準が無い\n".encode()
        if label in ("surrogate-context", "surrogate-origin"):
            assert old.returncode == 1 and b"UnicodeEncodeError" in old.stderr, old.stderr
    # 保存済みの system の場所が符号化できないとき、旧版は lexists で「無い」と見る。
    system = runner.env({"GIT_CONFIG_NOSYSTEM": None, "GIT_CONFIG_SYSTEM": str(runner.work / "r5-system")})
    result = arguments.run([sys.executable, "-B", str(check.LEGACY), "snapshot", "--out", str(runner.work / "r5-system-prior"),
                            *map(os.fspath, shared)], system, runner.work)
    assert result.returncode == 0, result.stderr
    value = json.loads((runner.work / "r5-system-prior").read_bytes())
    def retarget(node):
        if isinstance(node, dict):
            if node.get("kind") == "system" and node.get("state") == "absent":
                node["path"] = "/x\ud800/gitconfig"
            for child in node.values():
                retarget(child)
        elif isinstance(node, list):
            for child in node:
                retarget(child)
    retarget(value)
    state = write("surrogate-system", value)
    old, _, _ = runner.pair("r5-surrogate-system-preflight", ["preflight", "--state", state, *shared], "r5_saved_paths",
                            delta={"GIT_CONFIG_NOSYSTEM": None})
    assert old.returncode == 0, old.stderr


def r5_same_bytes_origins(runner):
    """r5: 同じバイトになる別の str(`\\udcc3\\udca9` と `é`)が、保存済みの config の出典と、
    include の相対パスの起点の両方に現れる場合。旧版は文字列の origin の dirname へ join し、
    str の鍵で引く。

    届く経路は preflight_trusted_configs(snapshot の前の状態つきの 1836 行と preflight)の
    include.path・gitdir の条件(None)・onbranch の条件の 3 つ。config_snapshot のグラフの
    origin は Git の出力から読むので、いつも正規の形になり、この形は届かない。
    probe_condition の origin(gitdir の相対の書換え)は、preflight では gitdir を Git に
    聞かないので届かない。
    """
    if sys.platform == "darwin":
        return
    repo, shared = repository(runner, "r5b-repo")
    prior = prior_state(runner, repo, shared, "r5b-prior")
    baseline = json.loads(prior.read_bytes())
    real = runner.work / "r5b-d\u00e9"
    real.mkdir()
    (real / "a.config").write_bytes(b"[include]\n\tpath = b.inc\n")
    (real / "b.inc").write_bytes(b"[s2l]\n\tv = 1\n")
    (real / "c.config").write_bytes(b'[includeIf "gitdir:./nowhere/"]\n\tpath = b.inc\n')
    (real / "o.config").write_bytes(b'[includeIf "onbranch:main"]\n\tpath = b.inc\n')
    odd = str(runner.work) + "/r5b-d\udcc3\udca9"
    plain = str(runner.work) + "/r5b-d\u00e9"
    def item(key):
        source = real / key.rsplit("/", 1)[1]
        st = source.lstat()
        return {"kind": "file", "path": "config:" + key, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "meta": {"mode": stat.S_IMODE(st.st_mode), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                         "ctime_ns": st.st_ctime_ns, "dev": st.st_dev, "ino": st.st_ino}}
    variants = {
        "odd-origin-odd-target": ([odd + "/a.config", odd + "/b.inc"], 0),
        "odd-origin-plain-target": ([odd + "/a.config", plain + "/b.inc"], 20),
        "both-forms": ([odd + "/a.config", plain + "/a.config", odd + "/b.inc", plain + "/b.inc"], 0),
        "plain-origin-odd-target": ([plain + "/a.config", odd + "/b.inc"], 20),
        "odd-gitdir-condition": ([odd + "/c.config"], 0),
        "odd-onbranch-odd-target": ([odd + "/o.config", odd + "/b.inc"], 0),
        "odd-onbranch-plain-target": ([odd + "/o.config", plain + "/b.inc"], 20),
    }
    out = runner.work / "r5b-out"
    for label, (keys, expected) in variants.items():
        value = json.loads(json.dumps(baseline))
        value["config"]["origins"].extend(item(key) for key in keys)
        state = runner.work / ("r5b-state-" + label)
        state.write_bytes(contracts.canonical(value))
        for command, argv in [("snapshot", ["snapshot", "--out", out, *shared, "--secret-patterns-from", state]),
                              ("preflight", ["preflight", "--state", state, *shared])]:
            old, _, _ = runner.pair(f"r5b-{label}-{command}", argv, "r5_same_bytes", output=out,
                                    delta={"TMPDIR": str(runner.work)})
            assert old.returncode == expected, (label, command, old.stderr)


def r6_os_path(runner):
    """r6: os.path(文字列)と Path の違い。gitdir の相対の書換え・相対や NUL の保存パス・相対の引数。"""
    # gitdir の相対の書換え: origin の os.path.dirname は「/./」の「.」を保ち、
    # condition[:len(prefix)] の字面(大文字・小文字)を保つ(git は gitdir: を区別して見る)。
    for label, include, condition in [("dot-origin", "{d}/./x.inc", "gitdir:./"),
                                      ("upper-gitdir", "{d}/x.inc", "GITDIR:./"),
                                      ("mixed-gitdir-i", "{d}/x.inc", "GitDir/I:./"),
                                      ("dot-origin-upper", "{d}/./x.inc", "GITDIR:./"),
                                      ("plain", "{d}/x.inc", "gitdir:./")]:
        repo, shared = repository(runner, "r6-" + label)
        # x.inc を worktree の直下に置く。gitdir:./ は(書換えが旧版と同じなら)この worktree の
        # .git に当たるかどうかが、書き換えた字面で決まる。
        folder = repo
        (folder / "x.inc").write_bytes(f'[includeIf "{condition}"]\n\tpath = {folder}/y.inc\n'.encode())
        (folder / "y.inc").write_bytes(b"[s2l]\n\ty = 1\n")
        with (repo / ".git" / "config").open("ab") as stream:
            stream.write(f"[include]\n\tpath = {include.format(d=folder)}\n".encode())
        out = runner.work / ("r6-out-" + label)
        runner.pair(f"r6-gitdir-{label}-snapshot", ["snapshot", "--out", out, *shared], "r6_os_path", output=out)
        if sys.platform != "darwin":
            prior = prior_state(runner, repo, shared, "r6-prior-" + label) if label != "dot-origin" else None
            if prior is not None:
                runner.pair(f"r6-gitdir-{label}-preflight", ["preflight", "--state", prior, *shared], "r6_os_path")
    # 相対の引数(旧 open_dir_path の「絶対パスでないディレクトリは観察できない」)。
    repo, shared = repository(runner, "r6-relative")
    relative = os.path.relpath(repo, runner.work)
    out = runner.work / "r6-relative-out"
    for label, argv in [("all", ["snapshot", "--out", "r6-relative-out", "--top", relative, "--common", relative + "/.git",
                                 "--repo-admin", relative + "/.git"]),
                        ("common", ["snapshot", "--out", out, "--top", repo, "--common", relative + "/.git",
                                    "--repo-admin", repo / ".git"]),
                        ("top", ["snapshot", "--out", out, "--top", relative, "--common", repo / ".git",
                                 "--repo-admin", repo / ".git"])]:
        old, _, _ = runner.pair(f"r6-relative-argv-{label}", argv, "r6_os_path", output=out)
        if sys.platform != "darwin":
            assert old.stderr == "ERROR [loop-state] 絶対パスでないディレクトリは観察できない\n".encode(), old.stderr
    if sys.platform == "darwin":
        return
    prior = prior_state(runner, repo, shared, "r6-relative-prior")
    runner.pair("r6-relative-argv-common-prior", ["snapshot", "--out", out, "--top", repo, "--common", relative + "/.git",
                                                  "--repo-admin", repo / ".git", "--secret-patterns-from", prior],
                "r6_os_path", output=out)
    # 保存状態の相対・NUL のパス(config の出典・worktree の行)。
    baseline = json.loads(prior.read_bytes())
    origin = baseline["config"]["origins"][0]
    states = {}
    for label, key in [("origin-relative", "rel/config"), ("origin-relative-surrogate", "x\ud800"),
                       ("origin-bare-name", "config"), ("origin-nul-directory", str(repo) + "/a\u0000b/config"),
                       ("origin-nul-leaf", str(repo) + "/.git/co\u0000nfig")]:
        value = json.loads(json.dumps(baseline))
        value["config"]["origins"].append({**origin, "path": "config:" + key})
        states[label] = value
    for label, row in [("worktree-relative", "rel"), ("worktree-nul", "/x\u0000y")]:
        value = json.loads(json.dumps(baseline))
        value["worktrees"].append({"worktree": row, "branch": "refs/heads/human", "locked": ""})
        value["worktree_configs"].append({**value["worktree_configs"][0], "path": row})
        states[label] = value
    for label, value in states.items():
        state = runner.work / ("r6-state-" + label)
        state.write_bytes(contracts.canonical(value))
        runner.pair(f"r6-{label}-snapshot", ["snapshot", "--out", out, *shared, "--secret-patterns-from", state],
                    "r6_os_path", output=out)
        old, _, _ = runner.pair(f"r6-{label}-preflight", ["preflight", "--state", state, *shared], "r6_os_path")
        if label in ("origin-relative", "origin-bare-name", "worktree-relative"):
            assert old.stderr == "ERROR [loop-state] 絶対パスでないディレクトリは観察できない\n".encode(), old.stderr
        if "nul" in label:
            assert old.returncode == 1 and b"ValueError: embedded null byte" in old.stderr, old.stderr


def r6_strings(runner):
    """r6 の洗い出し: bytes.strip(\\x0b も除く)・NUL の os.stat(ValueError)・strtol の空白(\\v)。"""
    # strtol は C の isspace を飛ばす(\v を含む)。PYTHONINTMAXSTRDIGITS と PYTHONUNBUFFERED。
    root = runner.work / "r6-env"
    root.mkdir()
    state = root / "state"
    state.write_bytes(b'{"schema":7,"stop_control":null}')
    for value in ["\v640", "\v639", "\f640", "\v0"]:
        runner.pair(f"r6-intmax-{value!r}", ["state-digest", "--state", state], "r6_strings",
                    delta={"PYTHONINTMAXSTRDIGITS": value})
    if sys.platform.startswith("linux"):
        # 書込みが失敗する出力で、buffering(終了時の flush の 120 と書込み時の 1)が分かれる。
        for value in ["\v0", "\v1", "0"]:
            full = os.open("/dev/full", os.O_WRONLY)
            try:
                runner.pair(f"r6-unbuffered-{value!r}", ["state-digest", "--state", state], "r6_strings",
                            delta={"PYTHONUNBUFFERED": value}, stdout=full)
            finally:
                os.close(full)
    # 緩い ref の symref の値は bytes.strip()(\x0b も除く)。
    repo, shared = repository(runner, "r6-strip")
    out = runner.work / "r6-strip-out"
    for label, raw in [("vertical-tab", b"ref: refs/heads/missing\x0b\n"), ("form-feed", b"ref: refs/heads/missing\x0c\n")]:
        (repo / ".git/refs/heads/odd").write_bytes(raw)
        runner.pair(f"r6-loose-ref-{label}", ["snapshot", "--out", out, *shared], "r6_strings", output=out)
    (repo / ".git/refs/heads/odd").unlink()
    if sys.platform == "darwin":
        return
    # onbranch の HEAD の分岐名も bytes.strip()。NUL を含む部分は os.stat の ValueError(973・994 行)。
    repo, shared = repository(runner, "r6-head", onbranch=True)
    prior = prior_state(runner, repo, shared, "r6-head-prior")
    delta = {"TMPDIR": str(runner.work)}
    for label, raw in [("vertical-tab", b"ref: refs/heads/main\x0b\n"), ("nul-leaf", b"ref: refs/heads/ma\x00in\n"),
                       ("nul-parent", b"ref: refs/heads/a\x00b/main\n")]:
        (repo / ".git/HEAD").write_bytes(raw)
        old, _, _ = runner.pair(f"r6-head-{label}-preflight", ["preflight", "--state", prior, *shared], "r6_strings",
                                delta=delta)
        if label.startswith("nul"):
            assert old.returncode == 1 and old.stderr.endswith(b"ValueError: embedded null byte\n"), old.stderr
        runner.pair(f"r6-head-{label}-snapshot", ["snapshot", "--out", out, *shared, "--secret-patterns-from", prior],
                    "r6_strings", output=out, delta=delta)
    (repo / ".git/HEAD").write_bytes(b"ref: refs/heads/main\n")


def linked_repository(runner, name, onbranch=False):
    """主の worktree と、分岐を持つ linked worktree を 1 つ作る。"""
    repo, shared = repository(runner, name, onbranch=onbranch)
    linked = runner.work / (name + "-linked")
    result = arguments.run([REAL_GIT, "-C", str(repo), "worktree", "add", "-q", "-b", "human", str(linked)],
                           runner.env(), runner.work)
    assert result.returncode == 0, result.stderr
    return repo, shared, linked, repo / ".git" / "worktrees" / linked.name


def held_mode(path, mode):
    """権限を一時に変え、戻す。自分の一時の置き場の中だけで使う。"""
    class Held:
        def __enter__(self):
            self.before = stat.S_IMODE(path.lstat().st_mode)
            path.chmod(mode)
        def __exit__(self, *_):
            path.chmod(self.before)
    return Held()


def r7_held_errors(runner):
    """r7: 保持した fd からの読取の OSError(旧 except OSError の Stop)と、
    保持した親からの open の OSError と差し替え(fstat の不一致)の区別。"""
    if sys.platform == "darwin":
        return
    delta = {"TMPDIR": str(runner.work)}
    # held_git_dir・held_common_dir の read_at(linked worktree の .git ファイルと commondir)。
    repo, shared, linked, admin = linked_repository(runner, "r7-held")
    prior = prior_state(runner, repo, shared, "r7-held-prior")
    out = runner.work / "r7-held-out"
    for label, path, reason in [("dotgit-file", linked / ".git", "worktree の git-dir を安全に読めない"),
                                ("commondir", admin / "commondir", "worktree の commondir を安全に読めない")]:
        with held_mode(path, 0):
            old, _, _ = runner.pair(f"r7-{label}-mode000-preflight", ["preflight", "--state", prior, *shared],
                                    "r7_held_errors")
            assert old.returncode == 20 and old.stderr == ("ERROR [loop-state] " + reason + "\n").encode(), old.stderr
            runner.pair(f"r7-{label}-mode000-snapshot", ["snapshot", "--out", out, *shared, "--secret-patterns-from", prior],
                        "r7_held_errors", output=out)
    # held_git_dir の .git ディレクトリの open(旧 os.open の OSError は「安全に読めない」)。
    for mode in (0o300, 0):
        with held_mode(repo / ".git", mode):
            old, _, _ = runner.pair(f"r7-dotgit-dir-mode{mode:03o}-preflight", ["preflight", "--state", prior, *shared],
                                    "r7_held_errors")
            assert old.stderr == "ERROR [loop-state] worktree の git-dir を安全に読めない\n".encode(), old.stderr
    # held_head・held_branch の read_at(onbranch の include がある repo の HEAD と緩い ref)。
    repo, shared = repository(runner, "r7-branch", onbranch=True)
    prior = prior_state(runner, repo, shared, "r7-branch-prior")
    out = runner.work / "r7-branch-out"
    for label, path, reason in [("head", repo / ".git" / "HEAD", "worktree の HEAD を安全に読めない"),
                                ("loose-ref", repo / ".git" / "refs" / "heads" / "main", "worktree の HEAD を読めない")]:
        with held_mode(path, 0):
            old, _, _ = runner.pair(f"r7-{label}-mode000-preflight", ["preflight", "--state", prior, *shared],
                                    "r7_held_errors", delta=delta)
            assert old.returncode == 20 and old.stderr == ("ERROR [loop-state] " + reason + "\n").encode(), old.stderr
            runner.pair(f"r7-{label}-mode000-snapshot", ["snapshot", "--out", out, *shared, "--secret-patterns-from", prior],
                        "r7_held_errors", output=out, delta=delta)
    # profile_patterns: .claude の os.stat・os.open と project-profile.yml の read_at の OSError は捕まえない。
    repo, shared = repository(runner, "r7-profile")
    out = runner.work / "r7-profile-out"
    claude = repo / ".claude"
    claude.mkdir()
    profile = claude / "project-profile.yml"
    profile.write_bytes(b"secret_paths: [x]\n")
    for label, path, mode in [("claude-mode000", claude, 0), ("claude-mode300", claude, 0o300),
                              ("claude-mode600", claude, 0o600), ("profile-mode000", profile, 0)]:
        with held_mode(path, mode):
            old, _, _ = runner.pair(f"r7-{label}-snapshot", ["snapshot", "--out", out, *shared], "r7_held_errors",
                                    output=out)
        if label == "claude-mode600":
            assert old.returncode == 20 and old.stderr == "ERROR [loop-state] secret_paths を安全に読めない\n".encode()
        else:
            name = b"'.claude'" if label.startswith("claude") else b"'project-profile.yml'"
            assert old.returncode == 1 and old.stderr.endswith(b"PermissionError: [Errno 13] Permission denied: " + name + b"\n")
            assert b"line 1887, in snapshot" in old.stderr, old.stderr


def r7_exception_audit(runner):
    """r7 の try/except の点検で見つけた差: 停止ファイルの親の stat・open の OSError(379・387 行は捕まえない)と、
    entry の呼出元のフレーム(entry 302・optional_entry 1786・index_snapshot 1698・<dictcomp> 1799 など)。"""
    if sys.platform == "darwin":
        return
    repo, shared = repository(runner, "r7-audit")
    out = runner.work / "r7-audit-out"
    parent = repo / "a"
    parent.mkdir()
    argv = ["snapshot", "--out", out, *shared, "--stop-control-worktree", repo, "--stop-control-rel", "a/b/stop",
            "--stop-control-new-dir", "a/b"]
    for label, mode, line, name in [("stat", 0o600, b"line 379, in stop_control_from_args", b"'b'"),
                                    ("open", 0, b"line 387, in stop_control_from_args", b"'a'")]:
        with held_mode(parent, mode):
            old, _, _ = runner.pair(f"r7-stop-parent-{label}", argv, "r7_exception_audit", output=out)
        assert old.returncode == 1 and line in old.stderr, old.stderr
        assert old.stderr.endswith(b"PermissionError: [Errno 13] Permission denied: " + name + b"\n"), old.stderr
    parent.rmdir()
    git = repo / ".git"
    (git / "config.worktree").write_bytes(b"[s2l]\n\tw = 1\n")
    (git / "info").mkdir(exist_ok=True)
    (git / "info" / "sparse-checkout").write_bytes(b"/*\n")
    hooks = git / "hooks"
    if hooks.is_dir():
        shutil.rmtree(hooks)
    hooks.write_bytes(b"not a directory\n")
    for label, path, frame in [("config-worktree", git / "config.worktree", b"line 1891, in snapshot"),
                               ("hooks-file", hooks, b"line 1786, in optional_entry"),
                               ("info-sparse-checkout", git / "info" / "sparse-checkout", b"in <dictcomp>"),
                               ("index", git / "index", b"line 1698, in index_snapshot")]:
        with held_mode(path, 0):
            old, _, _ = runner.pair(f"r7-entry-{label}-mode000", ["snapshot", "--out", out, *shared],
                                    "r7_exception_audit", output=out)
        assert old.returncode == 1 and frame in old.stderr and b"line 302, in entry" in old.stderr, old.stderr


def r7_path_equality(runner):
    """r7: 旧版は os.path の結果の str を字面どおりに比べる(Path の == は末尾の「/」・「.」・重なった「/」を無視する)。"""
    # 初回の snapshot の --common の字面(abspath は先頭の「//」を保ち、「///」は「/」にする)。
    repo, shared = repository(runner, "r7-common")
    out = runner.work / "r7-common-out"
    common = str(repo / ".git")
    for label, value in [("double-slash", "/" + common), ("triple-slash", "//" + common),
                         ("trailing-slash", common + "/"), ("dot", str(repo) + "/./.git")]:
        argv = ["snapshot", "--out", out, "--top", repo, "--common", value, "--repo-admin", repo / ".git"]
        runner.pair(f"r7-common-{label}-snapshot", argv, "r7_path_equality", output=out)
        if sys.platform != "darwin":
            prior = prior_state(runner, repo, shared, "r7-common-prior-" + label)
            runner.pair(f"r7-common-{label}-snapshot-prior", [*argv, "--secret-patterns-from", prior],
                        "r7_path_equality", output=out)
            runner.pair(f"r7-common-{label}-preflight",
                        ["preflight", "--state", prior, "--top", repo, "--common", value, "--repo-admin", repo / ".git"],
                        "r7_path_equality")
    # 同じファイルを a.inc と ./a.inc で include し、a.inc に真の hasconfig の includeIf を置く(待ち行列の重複)。
    repo, shared = repository(runner, "r7-pending")
    out = runner.work / "r7-pending-out"
    git = repo / ".git"
    (git / "a.inc").write_bytes(b'[includeIf "hasconfig:remote.*.url:https://example.invalid/**"]\n\tpath = b.inc\n')
    (git / "b.inc").write_bytes(b"[s2l]\n\tb = 1\n")
    with (git / "config").open("ab") as stream:
        stream.write(b'[remote "origin"]\n\turl = https://example.invalid/r7\n[include]\n\tpath = a.inc\n\tpath = ./a.inc\n')
    old, _, _ = runner.pair("r7-pending-dot-snapshot", ["snapshot", "--out", out, *shared], "r7_path_equality", output=out)
    if sys.platform == "darwin":
        return
    assert old.returncode == 0, old.stderr
    prior = prior_state(runner, repo, shared, "r7-pending-prior")
    runner.pair("r7-pending-dot-snapshot-prior", ["snapshot", "--out", out, *shared, "--secret-patterns-from", prior],
                "r7_path_equality", output=out)
    runner.pair("r7-pending-dot-preflight", ["preflight", "--state", prior, *shared], "r7_path_equality")
    # linked worktree の commondir と .git の gitdir の字面(末尾の「/」・重なった「/」・「.」)。
    for label, target, text in [("commondir-trailing-slash", "commondir", "{common}/\n"),
                                ("commondir-double-slash", "commondir", "/{common}\n"),
                                ("commondir-dot", "commondir", "{repo}/./.git\n"),
                                ("gitdir-trailing-slash", "dotgit", "gitdir: {admin}/\n"),
                                ("gitdir-double-slash", "dotgit", "gitdir: /{admin}\n")]:
        repo, shared, linked, admin = linked_repository(runner, "r7-" + label)
        prior = prior_state(runner, repo, shared, "r7-prior-" + label)
        path = admin / "commondir" if target == "commondir" else linked / ".git"
        path.write_text(text.format(common=repo / ".git", repo=repo, admin=admin), encoding="utf-8")
        out = runner.work / ("r7-out-" + label)
        old, _, _ = runner.pair(f"r7-{label}-preflight", ["preflight", "--state", prior, *shared], "r7_path_equality")
        runner.pair(f"r7-{label}-snapshot-prior", ["snapshot", "--out", out, *shared, "--secret-patterns-from", prior],
                    "r7_path_equality", output=out)
        assert old.returncode == 20, (label, old.stderr)


def tree_permissions(runner):
    """r3: 保持fdからの列挙(mode 0400)と、非UTF-8名の lstat の診断(資源の上限に依らない権限の失敗)。"""
    repo, shared = repository(runner, "permission-repo")
    out = runner.work / "permission-out"
    for name in ["d", "d-\udcff"]:
        for child in [False, True]:
            directory = repo / name
            if name.isascii():
                directory.mkdir()
            elif not check.create_raw(directory, Path.mkdir):
                # Mac の APFS は UTF-8 でない名前を作れない。そのときだけ飛ばし、行で出す(ASCII の対照は d)。
                kind = "child" if child else "empty"
                runner.boundaries.append(check.os_boundary("r3-mode0400-raw-" + kind, "skipped", f"r3-mode0400-{ascii('d')}-{kind}"))
                continue
            if child:
                (directory / "child").write_bytes(b"child body")
            directory.chmod(0o400)
            try:
                old, _, saved = runner.pair(f"r3-mode0400-{ascii(name)}-{'child' if child else 'empty'}",
                                            ["snapshot", "--out", out, *shared], "r3_tree", output=out)
                if sys.platform != "darwin":
                    if child:
                        expected = ("ERROR [loop-state] パスを lstat できない: " + name + "/child: Permission denied\n")
                        assert old.returncode == 20 and old.stderr == expected.encode("utf-8", "backslashreplace")
                        assert saved is None
                    else:
                        assert old.returncode == 0 and saved is not None
            finally:
                directory.chmod(0o700)
                if child:
                    (directory / "child").unlink()
                directory.rmdir()


def chain(root, n, kind):
    links = root / "links"
    links.mkdir()
    for i in range(n):
        target = "l" + str(i + 1) if i + 1 < n else ("l0" if kind == "loop" else "../missing" if kind == "parent" else "missing")
        if kind == "absolute":
            target = str(links / target)
        os.symlink(target, links / ("l" + str(i)))
    target = str(links / "l0") if n else str(links / "missing")
    if kind == "cache":
        (links / "dir").mkdir()
        os.symlink("dir", links / "cache")
        target = str(links) + "/cache/.." * 40 + "/cache"
    return target


def realpath_cases(runner):
    """r3: realpath の再帰。呼出元のフレーム数(B=3/4)ごとの境界の前後・循環・再利用・絶対/相対・存在しない終端。"""
    folder = runner.work / "realpath"
    folder.mkdir()
    rows = [("stop-relative", 992, "relative", "stop-control", None), ("stop-relative", 993, "relative", "stop-control", None),
            ("stop-absolute", 992, "absolute", "stop-control", None), ("stop-absolute", 993, "absolute", "stop-control", None),
            ("stop-loop", 992, "loop", "stop-control", None), ("stop-loop", 993, "loop", "stop-control", None),
            ("stop-parent", 992, "parent", "stop-control", None), ("stop-parent", 993, "parent", "stop-control", None),
            ("stop-cache", 0, "cache", "stop-control", None), ("stop-relative", 1000, "relative", "stop-control", None),
            ("preflight-top", 992, "relative", "preflight", "top"), ("preflight-top", 993, "relative", "preflight", "top"),
            ("preflight-common", 993, "relative", "preflight", "common"),
            ("snapshot-removed", 991, "relative", "snapshot", "removed"), ("snapshot-removed", 992, "relative", "snapshot", "removed"),
            ("snapshot-prior", 991, "relative", "snapshot", "prior"), ("snapshot-prior", 992, "relative", "snapshot", "prior")]
    for label, n, kind, command, slot in rows:
        root = folder / f"{label}-{n}"
        root.mkdir()
        target = chain(root, n, kind)
        saved = root / "out"
        state = root / "state.json"
        payload = {"schema": 7, "stop_control": {"worktree": target, "rel": "stop", "new_dirs": []}}
        argv = ["stop-control", "--state", state]
        if command == "preflight":
            payload = {"schema": 7}
            argv = ["preflight", "--state", state, "--top", target if slot == "top" else root,
                    "--common", target if slot == "common" else root, "--repo-admin", root]
        elif command == "snapshot":
            payload = {"schema": 7}
            argv = ["snapshot", "--out", saved, "--top", root, "--common", root, "--repo-admin", root,
                    "--secret-patterns-from", state]
            if slot == "removed":
                argv += ["--removed-parent-worktree", target, "--exclude-worktree", target]
            else:
                payload.update(secret_patterns=[{"path": "/dummy", "patterns": [".env", ".env.*", ".dev.vars"]}],
                               worktree_configs=[{"path": "/dummy", "git_dir": target}])
        state.write_bytes(json.dumps(payload).encode())
        before = inventory(root)
        old, _, _ = runner.pair(f"r3-realpath-{label}-{n}", argv, "r3_realpath", output=saved)
        assert inventory(root) == before, "REALPATH_INPUT_CHANGED"
        failing = (command in ("stop-control", "preflight") and n >= 993) or (command == "snapshot" and n >= 992)
        if failing:
            assert old.returncode == 1 and old.stderr.endswith(
                b"RecursionError: maximum recursion depth exceeded while calling a Python object\n"), (label, n)
        else:
            assert old.returncode in (0, 20), (label, n, old.stderr[-300:])


def inventory(root):
    rows = []
    for base, directories, files in os.walk(root):
        for name in sorted(directories + files):
            path = Path(base) / name
            st = path.lstat()
            rows.append((str(path), st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns,
                         os.readlink(path) if stat.S_ISLNK(st.st_mode) else None))
    return sorted(rows, key=repr)


def git_only(runner):
    """外部の道具は Git だけ(23:19)。PATH を検査用の git だけにして、起動した道具を記録する。"""
    repo, shared = repository(runner, "git-only-repo", onbranch=True)
    prior = prior_state(runner, repo, shared, "git-only-prior")
    out = runner.work / "git-only-out"
    only = {"PATH": str(runner.bindir)}
    runner.exec_log.write_bytes(b"")
    runner.pair("git-only-snapshot", ["snapshot", "--out", out, *shared], "git_only", delta=only, output=out)
    if prior is not None:
        runner.pair("git-only-preflight", ["preflight", "--state", prior, *shared], "git_only",
                    delta={**only, "TMPDIR": str(runner.work)})
    launched = set(runner.exec_log.read_bytes().splitlines())
    assert launched == {os.fsencode(runner.bindir / "git")}, launched
    return static_git_only()


def static_git_only():
    """製品の経路(検査以外)の子の起動が Command::new("git") だけであること。"""
    source = check.ROOT / "native/crates/cli/src/commands/loop_state"
    found = []
    for path in sorted(source.glob("*.rs")):
        if path.name.endswith("_tests.rs"):
            continue
        text = path.read_text(encoding="utf-8")
        text = text.split("#[cfg(test)]\nmod tests", 1)[0]
        for match in re.finditer(r"Command::new\(([^)]*)\)", text):
            found.append((path.name, match.group(1)))
    others = [row for row in found if row[1] != '"git"']
    # 単体の検査の中の起動(python3 等)は cfg(test) の関数にだけある。
    allowed = {("walk_recursion.rs", '"python3"')}
    unexpected = [row for row in others if row not in allowed]
    if unexpected or not any(row == ("process.rs", '"git"') for row in found):
        raise check.Mismatch("S2L_NON_GIT_CHILD: " + repr(unexpected))
    return len(found)


def run(fixture, full=True):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="s2l-decisions-") as temporary:
        runner = Runner(Path(fixture), Path(temporary).resolve())
        startup_cases(runner)
        interpreter_variables(runner)
        signals(runner)
        timeouts(runner, full)
        blocksize = write_failures(runner)
        onbranch(runner)
        tree_permissions(runner)
        realpath_cases(runner)
        r4_configs(runner)
        preflight_json_depth(runner)
        realpath_surrogate(runner)
        r5_saved_paths(runner)
        r5_same_bytes_origins(runner)
        r6_os_path(runner)
        r6_strings(runner)
        r7_held_errors(runner)
        r7_exception_audit(runner)
        r7_path_equality(runner)
        sources = git_only(runner)
        return {"decision_cases": len(runner.cases), "groups": runner.groups, "st_blksize": blocksize,
                "os_boundaries": runner.boundaries,
                "timeout_elapsed_seconds": runner.timing,
                "rust_child_sources": sources,
                # 書込みが途中まで通る出力の故障は、今の比較に無い(/dev/full と閉じた pipe は 0 バイト)。
                "partial_output_fault_cases": 0, "output_fault_routes": ["closed-pipe", "full"],
                "skipped": 0, "timeouts": 0, "seconds": round(time.monotonic() - started, 3)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--quick", action="store_true", help="10 秒の上限の case を省く(途中の確認だけ)")
    options = parser.parse_args()
    check.environment()
    print(json.dumps(run(options.fixture.resolve(), full=not options.quick), ensure_ascii=False))
