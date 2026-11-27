#!/usr/bin/env python3
"""R4Q(#494)の比較器: 旧版(check-process.py・loop-supervisor.py。本家 CPython 3.10.11)・固定期待・Rust の
検査用の実行物(native/crates/cli/tests/r4q.rs)を、同じ入力・同じ環境で比べる。

使い方:
    python3 -B native/ci/check_r4q.py --rust <Rust の検査用の実行物> [--only <ID>…] [--list]
        cargo test --locked -p dev-workflow-cli --test r4q -- --r4q-compare から呼ばれる(CI はジョブ r4q の
        build.py r4q)。必須のケースを全部流し、ID ごとの結果・緩めた欄の名前と件数・組ごとの件数を出す。

- 旧版を動かす Python は R4Q_PYTHON(必須)。CPython 3.10.11・標準ライブラリの固定の一覧の SHA-256 が本家と
  同じ(表は build.py の R4Q_STDLIB_SHA256)・Linux では os.pidfd_open と signal.pidfd_send_signal があることを
  確かめる。違えば失敗にする(skip にしない)。
- 最初に、流す必須のケースの総数と並びを出す。ケースが終わるたびに、ID・結果・所要時間・緩めた欄の名前と件数を
  出し、すぐに書き出す(flush)。
- 一時フォルダは、呼んだ側が渡した場所(TMPDIR、無ければ RUNNER_TEMP)の下にだけ作り、作って控えた正確な
  パスだけを消す。/tmp などの共有の場所に残さない。終わりに、渡された場所に新しい物が残っていないことを確かめる。
- 子の環境は、許すものを並べて一から作る(PATH・地域設定と、ケースが与える変数)。PYTHONPATH などの前提の外の
  変数は渡さない(2026-11-11 22:31 の決定)。品質コマンドが見た環境に前提の外の変数があれば失敗にする。
- 旧版と Rust は、同じ作業フォルダ(ケースのフォルダ)で順に流す。入力が同じなので、利用者が渡したパスは
  伏せない。緩めるのは設計の「緩める欄の閉じた一覧」の欄だけ(RELAXED)。
- 残るプロセスは、品質コマンドの argv に入れた印で見つける。保証の外のケースのほかは 0 件で、
  保証の外のケースは旧版と同じ件数であることを確かめる。失敗の経路(時間切れ・途中の例外)でも残さないよう、
  ケースごとの後と最後に印の付いたプロセスを止め、見つけたらそのケース(か全体)を不合格にして件数を出す。
- 監督の argv の頭は、その回の監督(旧版・Rust)で決まった頭だけを固定名へ戻す。品質コマンドが監督の argv を
  読めなければ(r4q_probe の parent_argv が null)、親が先に消えるケースの固定の一覧(PARENT_GONE_IDS)のほかは
  不合格にする。
- Mac で行わなくてよいケースは、設計の閉じた一覧(CLOSED_LIST・MAC_SKIPPED_LS)だけ。件数と理由を出し、
  成功に数えない。
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "native/ci"))
import build  # noqa: E402(標準ライブラリの固定の一覧と本家 3.10.11 の SHA-256 は build.py の 1 か所に置く)

SCRIPTS = ROOT / "plugins/dev-workflow/skills/ship-task/scripts"
LS_SCRIPT = SCRIPTS / "loop-supervisor.py"
CP_SCRIPT = SCRIPTS / "check-process.py"
LOOP_SH = SCRIPTS / "loop.sh"
LEGACY_SHA256 = {
    "check-process.py": "f50b3278ee99fbd1587b8282141de57e264c9405253f766a8032011c9bff4649",
    "loop-supervisor.py": "6356a2195b728249411f0944646fe30f1df4297915eec14e710854633dabe2ab",
}
PARENT = ROOT / "native/ci/r4q_legacy_parent.py"
FAKE = ROOT / "native/ci/r4q_fake_monitor.py"
PROBE_SOURCE = ROOT / "native/ci/r4q_probe.c"
FIXTURES = ROOT / "native/fixtures/r4q"
INJECTED = FIXTURES / "injected.json"
GEN_INJECTED = FIXTURES / "gen_injected.py"
FIXED_INJECTED = ("inj-posix-anchor-killed-after-notice-linux", "inj-posix-anchor-killed-after-notice-macos")
LINUX = sys.platform.startswith("linux")
MAC = sys.platform == "darwin"
# env を渡さないケースの地域設定(build.py の build_environment は LC_ALL=C を足すので、比較器が決め直す)。
LOCALE_OF = {"linux": "C.UTF-8", "macos": "en_US.UTF-8"}
CHILD_PATH = "/usr/bin:/bin"
RUN_TIMEOUT = [120]  # 1 回の起動の上限(秒)。自己検査だけが --run-timeout で縮める。
FLAGS = {"ls": "--r4q-loop-supervisor", "cp": "--r4q-check-process"}
SCRIPT_OF = {"ls": LS_SCRIPT, "cp": CP_SCRIPT}
FIXED_SCRIPT = {"ls": "loop-supervisor.py", "cp": "check-process.py"}
STRERROR_LAST = 1024
LINUX_TABLE_LAST = 133
MAC_TABLE_LAST = 107
NONCE_RE = re.compile(r"[0-9a-f]{64}")

# 設計の「緩める欄の閉じた一覧」。比較器はこの名前の欄だけを、決めた規則で緩める。
RELAXED = (
    "固定名",  # 旧スクリプト・sys.executable・標準ライブラリのパス(17:58・21:35)
    "監督の argv の頭",  # 監督の argv と TimeoutExpired の文の前に付ける引数(17:58・21:35)
    "Current thread",  # Fatal Python error の Current thread の値(17:45)
    "SIGINT の呼出履歴",  # SIGINT で中断した呼出履歴の途中の行(22:31)
    "nonce",  # 親が作る nonce(22:31)
    "pid",  # KeyError の pid・品質コマンドが見る pid と pgid と sid(06:58)
    "群の外の書き手",  # *_sha256・*_tail・最後のファイル(06:58)
    "TERM の回数",  # 実プロセスの TERM を送り直す回数(22:31)
    "__CF_USER_TEXT_ENCODING",  # Mac の品質コマンドが見る値(02:41)
    "閉じた fd 0〜2",  # 監督を直接起動して閉じた fd 0〜2(08:55)
    "POSIX の grace=0 の TERM の数",  # POSIX の方式の grace=0 の時間切れで品質コマンドが受けた TERM の数(06:58)
)

# Mac で行わなくてよいケースの閉じた一覧(設計の「テスト計画」)。ほかの未実施は失敗。
SKIP_DEVFULL = "/dev/full が無い"
SKIP_NON_UTF8 = "非 UTF-8 の名前を作れない(EILSEQ か EINVAL)"
SKIP_MAC_LS = "Mac では流さない ls- のケース(setup の後で結果が決まる)"
SKIP_ANCHOR_PAUSE = "anchor が pause に入ったことを 60 秒の期限内に確かめられない"
CLOSED_LIST = (SKIP_DEVFULL, SKIP_NON_UTF8, SKIP_MAC_LS, SKIP_ANCHOR_PAUSE)
# Mac で流さない ls- のケースの固定の一覧(setup の後で結果が決まるもの。子孫の回収と /proc を使う)。
# Linux だけで流す ls- のケースは、この一覧とちょうど同じでなければならない(ケースを組むときに確かめる)。
MAC_SKIPPED_LS = (
    "ls-basic-exit3", "ls-exit0", "ls-exit300", "ls-segv", "ls-term-self", "ls-remainder-dashdash",
    "ls-remainder-option", "ls-command-missing", "ls-enoexec", "ls-eacces-then-candidate", "ls-path-empty",
    "ls-path-unset", "ls-path-last-enotdir", "ls-timeout-term", "ls-timeout-ignore-term", "ls-timeout-grace0",
    "ls-timeout-nan", "ls-timeout-inf", "ls-grace-nan", "ls-grandchild-double", "ls-grandchild-setsid",
    "ls-grandchild-hold", "ls-signal-term", "ls-signal-int", "ls-signal-hup", "ls-signal-burst", "ls-kill-monitor",
    "ls-pdeathsig",
    "ls-record-fsize-zero", "ls-record-fsize-1", "ls-record-fsize-half", "ls-record-fsize-partial",
    "ls-nonce-non-ascii", "ls-sigchld-ignored", "ls-mask", "ls-env-order", "ls-coerce-c",
)
GROUPS = {"pre": "前提(strerror)", "inj": "差し替えの比較(inj-。旧版の流し直しと固定期待)",
          "ls": "loop-supervisor(ls-)", "cp": "check-process の監督(cp-)", "rc": "run_command(rc-)",
          "probe": "品質コマンドが見る状態(probe-)", "py": "Python の起動(py-)", "loop": "loop.sh の照合(loop-)"}


class Failure(Exception):
    """比較の不一致・前提の誤り。"""


class Skip(Exception):
    """閉じた一覧に当たる未実施(Mac だけ)。理由は CLOSED_LIST のどれか。"""


@dataclass
class Relax:
    """ケースごとの緩めた欄の数。名前は RELAXED のどれかに限る。"""
    counts: dict = field(default_factory=dict)

    def add(self, name, count=1):
        if name not in RELAXED:
            raise Failure(f"緩める欄の閉じた一覧に無い欄を緩めようとした: {name}")
        if count:
            self.counts[name] = self.counts.get(name, 0) + count

    def text(self):
        return "・".join(f"{name} {count}" for name, count in self.counts.items())


@dataclass
class Outcome:
    status: str  # 合格・未実施・不合格
    message: str = ""
    relax: dict = field(default_factory=dict)
    seconds: float = 0.0


@dataclass
class Case:
    id: str
    group: str
    kind: str  # cli・rc・strerror・inj
    params: dict
    mac_skip: str | None = None


@dataclass
class Ctx:
    rust: str
    rust_head: list
    python: str
    stdlib: str
    temp: str
    probe: str
    locale: str
    token: str
    nonces: set = field(default_factory=set)
    executions: int = 0
    bases: dict = field(default_factory=dict)
    depth: dict = field(default_factory=dict)
    injected: dict | None = None
    injected_problem: str | None = None
    hold_report: str | None = None
    notes: list = field(default_factory=list)


def platform_name():
    return "macos" if MAC else "linux"


# ---- 一時フォルダ・環境・プロセス ----

def temp_root():
    """呼んだ側が渡した一時の場所(TMPDIR、無ければ RUNNER_TEMP)。無ければ失敗(/tmp に作らない)。"""
    for name in ("TMPDIR", "RUNNER_TEMP"):
        value = os.environ.get(name)
        if value:
            path = Path(value)
            if not path.is_dir():
                raise Failure(f"{name} がフォルダでない: {value}")
            return str(path.resolve())
    raise Failure("一時フォルダの場所を TMPDIR(または RUNNER_TEMP)で渡す")


def remove_tree(path):
    """比較器が作って控えた正確なパスの木を消す(中の権限を戻してから。リンクは辿らない)。"""
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    os.chmod(path, 0o700)
    for entry in os.scandir(path):
        remove_tree(entry.path)
    os.rmdir(path)


def clear(directory):
    """ケースのフォルダの中身を消す(フォルダ自体は残す。比較器が作ったフォルダの中だけ)。"""
    os.chmod(directory, 0o700)
    for entry in os.scandir(directory):
        remove_tree(entry.path)


BASE_ENV_NAMES = ("PATH", "LANG")


def declared_names(extra=None):
    """ケースが子へ渡すと決めた環境変数の名前(child_env の結果ではなく、決めた値から作る。漏れを見つけるため)。"""
    names = set(BASE_ENV_NAMES)
    for key, value in (extra or {}).items():
        if value is None:
            names.discard(key)
        else:
            names.add(key)
    return names


def child_env(ctx, extra=None):
    """子の環境を一から作る。PATH と地域設定(LANG)と、ケースが与えた変数(値が None なら外す)だけ。"""
    env = {"PATH": CHILD_PATH, "LANG": ctx.locale}
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def run_head(path):
    """実行物を起動したときの argv の頭(スクリプトなら #! の行の並びと自分のパス)。"""
    with open(path, "rb") as stream:
        first = stream.readline()
    if first.startswith(b"#!"):
        return [*os.fsdecode(first[2:].strip()).split(), path]
    return [path]


def processes_with(token):
    """argv に印を持つプロセス(pid の並び。自分は除く)。Linux は /proc、Mac は ps。"""
    found = []
    mark = token.encode()
    if LINUX:
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) == os.getpid():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as stream:
                    if mark in stream.read():
                        found.append(int(name))
            except OSError:
                continue
        return found
    done = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,command="], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=30)
    for line in done.stdout.splitlines():
        pid, _, command = line.strip().partition(b" ")
        if mark in command and pid.isdigit() and int(pid) != os.getpid():
            found.append(int(pid))
    return found


def sweep(token):
    """印の付いたプロセスを数え、KILL して消えるのを待つ。戻り値は見つけた数(ゾンビを除く)。"""
    found = [pid for pid in processes_with(token) if not zombie(pid)]
    for pid in found:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while [pid for pid in processes_with(token) if not zombie(pid)]:
        if time.monotonic() > deadline:
            raise Failure(f"残ったプロセスを片付けられない(印 {token})")
        time.sleep(0.05)
    return len(found)


def zombie(pid):
    """回収待ち(ゾンビ)か。無ければ真(数えない)。"""
    if LINUX:
        try:
            raw = Path(f"/proc/{pid}/stat").read_bytes()
        except OSError:
            return True
        return raw[raw.rindex(b")") + 2:raw.rindex(b")") + 3] == b"Z"
    done = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=30)
    text = done.stdout.strip()
    return not text or text.startswith(b"Z")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not zombie(pid)


def wait_for(predicate, what, timeout=30):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise Failure(f"{what} が期限内に起きない")
        time.sleep(0.01)


@dataclass
class Run:
    exit: int
    pid: int
    seconds: float
    ended: float  # 終わりを確かめた時刻(time.time)


def launch(ctx, argv, *, env, cwd, stdin, stdout, stderr, preexec=None, during=None, pass_fds=(),
           close_after=(), new_session=False, timeout=None):
    """子を起動し、during(proc) の後に終わりを待つ。stdin・stdout・stderr は開いた fd か subprocess.DEVNULL。"""
    started = time.monotonic()
    ctx.executions += 1
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                            preexec_fn=preexec, pass_fds=pass_fds, close_fds=True, start_new_session=new_session)
    for fd in close_after:
        os.close(fd)
    try:
        if during is not None:
            during(proc)
        proc.wait(timeout=timeout or RUN_TIMEOUT[0])
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise Failure(f"時間切れ: {timeout or RUN_TIMEOUT[0]} 秒の上限に当たった({argv[:3]}…)") from exc
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        raise
    return Run(proc.returncode, proc.pid, time.monotonic() - started, time.time())


def preexec_of(params):
    """起動する側の信号の扱いと資源の上限(ケースの設定)。"""
    def apply():
        if params.get("sigchld_ignored"):
            signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        if params.get("sigint_ignored"):
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        if params.get("sigquit_ignored"):
            signal.signal(signal.SIGQUIT, signal.SIG_IGN)
        if params.get("sigpipe_ignored"):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        if params.get("sigxfsz_ignored"):
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        if params.get("block"):
            signal.pthread_sigmask(signal.SIG_BLOCK, params["block"])
        if params.get("fsize") is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (params["fsize"], resource.RLIM_INFINITY))
    return apply


# ---- 正規化(緩める欄の閉じた一覧) ----

def fixed_names(ctx, data, relax):
    """旧版の出力の、旧スクリプトと標準ライブラリのパスを固定名にする(固定名)。呼出履歴の File の行だけ。"""
    count = 0
    for script, name in ((LS_SCRIPT, "loop-supervisor.py"), (CP_SCRIPT, "check-process.py")):
        old = b'File "' + os.fsencode(str(script)) + b'"'
        count += data.count(old)
        data = data.replace(old, b'File "' + name.encode() + b'"')

    def stdlib(match):
        nonlocal count
        count += 1
        return b'File "' + match.group(1) + b'"'
    prefix = re.escape(os.fsencode(ctx.stdlib.rstrip("/") + "/"))
    data = re.sub(b'File "' + prefix + rb'([^"\n]+\.py)"', stdlib, data)
    relax.add("固定名", count)
    return data


THREAD_RE = re.compile(rb"Current thread 0x[0-9a-f]+ \(most recent call first\):\n")


def mask_thread(data, relax):
    """Fatal Python error の Current thread の値は、位置と形式だけ(17:45)。"""
    found = THREAD_RE.findall(data)
    if len(found) > 1:
        raise Failure("Current thread の行が 2 つ以上ある")
    if b"Current thread" in data and not found:
        raise Failure("Current thread の行の形式が違う")
    if found:
        relax.add("Current thread")
        data = THREAD_RE.sub(b"Current thread 0x@THREAD@ (most recent call first):\n", data)
    return data


def check_nonce(value, where):
    if not isinstance(value, str) or not NONCE_RE.fullmatch(value):
        raise Failure(f"{where} の nonce が 16 進の小文字 64 字でない: {value!r}")
    return value


def expected_head(ctx, kind, monitor):
    """その回の監督の種類(legacy・rust・fake)で決まる argv の頭(設計の「緩める欄の閉じた一覧」の 2 行目)。"""
    if monitor == "legacy":
        return [ctx.python, "-B", str(SCRIPT_OF[kind])]
    if monitor == "rust":
        return [*ctx.rust_head, FLAGS[kind]]
    if monitor == "fake":
        return [ctx.python, "-B", str(FAKE)]
    raise Failure(f"監督の種類が違う: {monitor}")


def fixed_head(kind):
    return ["python3", "-B", FIXED_SCRIPT[kind]]


def monitor_argv_fixed(ctx, kind, monitor, argv, relax):
    """監督の argv(品質コマンドが読んだ親の argv)の頭を固定名の形へ戻す(監督の argv の頭)。
    戻すのは、その回の監督の種類で決まった頭だけ。ほかの頭(取り違えた監督)なら不合格にする。"""
    head = expected_head(ctx, kind, monitor)
    if argv[:len(head)] != head:
        raise Failure(f"監督の argv の頭が、この回の監督({monitor})の頭でない: {argv[:4]}(期待 {head})")
    relax.add("監督の argv の頭")
    return [*fixed_head(kind), *argv[len(head):]]


def normalize_timeout_text(ctx, text, relax, monitor):
    """旧版の親の TimeoutExpired の文の、監督の argv の頭を固定名の形へ戻す(監督の argv の頭)。
    Rust の親は、自分で固定名の形を文に出すので当てない(設計の 17:58・21:35 の当て方)。戻すのは、その回の監督の
    種類で決まった頭だけ。"""
    if "timed out after" not in text:
        return text
    literal = "[" + ", ".join(repr(item) for item in expected_head(ctx, "cp", monitor))
    fixed = "[" + ", ".join(repr(item) for item in fixed_head("cp"))
    count = text.count(literal)
    relax.add("監督の argv の頭", count)
    return text.replace(literal, fixed)


def allowed_env_names(names):
    """品質コマンドが見てよい環境変数の名前(子へ渡した環境の名前と、旧 Python が足す LC_CTYPE)。"""
    return set(names) | {"LC_CTYPE"}


# 品質コマンドの代わりの r4q_probe が状態を書くとき、親(監督か anchor)が先に消えているケースの固定の一覧。
# r4q_probe は、起動してすぐ(信号を受ける前・始めた印を書く前)に親の argv を読み、どのケースでも、親を止めるか
# KILL する動きは始めた印を見てから送る。親は品質コマンドを回収するまで待つので、先に消えるケースは無い(空の一覧)。
PARENT_GONE_IDS = ()


def state_view(ctx, state, top_pid, relax, kind, allowed, side, monitor, mask_nonce=False, case_id=None):
    """品質コマンドが見る状態を、比べる形にする(pid は関係、監督の argv は固定名、nonce は確かめて伏せる)。

    戻り値は (比べる形, argv の nonce か None)。"""
    if state is None:
        return None, None
    relax.add("pid")
    view = {
        "pgid==pid": state["pgid"] == state["pid"], "sid==pid": state["sid"] == state["pid"],
        "pgid==ppid": state["pgid"] == state["ppid"], "sid==ppid": state["sid"] == state["ppid"],
        "ppid==top": state["ppid"] == top_pid, "cwd": state["cwd"], "fds": state["fds"], "stdin": state["stdin"],
        "blocked": state["blocked"], "ignored": state["ignored"], "caught": state["caught"],
        "parent_sigblk": state["parent_sigblk"], "parent_sigign": state["parent_sigign"],
    }
    env = []
    for item in state["env"]:
        raw = bytes.fromhex(item)
        name = raw.split(b"=", 1)[0].decode("utf-8", "surrogateescape")
        if MAC and name == "__CF_USER_TEXT_ENCODING":
            relax.add("__CF_USER_TEXT_ENCODING")
            print(f"  記録({side}): 品質コマンドが見た {raw.decode('utf-8', 'replace')}", flush=True)
            continue
        if name not in allowed:
            raise Failure(f"{side}: 前提の外の変数が品質コマンドへ渡った: {name}")
        env.append(item)
    view["env"] = env
    nonce = None
    if state["parent_argv"] is not None:
        argv = [bytes.fromhex(item).decode("utf-8", "surrogateescape") for item in state["parent_argv"]]
        argv = monitor_argv_fixed(ctx, kind, monitor, argv, relax)
        if mask_nonce and "--nonce" in argv:
            at = argv.index("--nonce") + 1
            nonce = check_nonce(argv[at], f"{side} の監督の argv")
            argv[at] = "@NONCE@"
            relax.add("nonce")
        view["monitor_argv"] = argv
    elif case_id in PARENT_GONE_IDS:
        view["monitor_argv"] = None
    else:
        # 親(監督か anchor)が生きているはずのケースで argv を読めなかった。両側が null でも一致とみなさない
        # (設計 v46 の Task 3.1。Mac の KERN_PROCARGS2 の読み方を、黙って比べずに通さないため)。
        raise Failure(f"{side}: 品質コマンドが監督の argv を読めない(r4q_probe の parent_argv が null)")
    return view, nonce


def read_state(case_dir):
    path = os.path.join(case_dir, "state.json")
    if not os.path.exists(path):
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def term_times(case_dir):
    """品質コマンドが受けた TERM の時刻(time.time と同じ秒)の並び。"""
    path = os.path.join(case_dir, "terms")
    if not os.path.exists(path):
        return []
    raw = Path(path).read_bytes()
    if len(raw) % 16:
        raise Failure("TERM の記録の長さが 16 の倍数でない")
    return [seconds + nanos / 1e9 for seconds, nanos in struct.iter_unpack("=qq", raw)]


def check_terms(rule, terms, relax, side):
    """TERM の回数。many(実プロセスの送り直し)は両方とも 2 回以上で回数を記録する。zero-or-one(POSIX の方式の
    grace=0。TERM の直後に KILL が来るので、TERM の処理が走るかは順序を決めない競り合い。06:58)は、旧版の取りうる
    範囲の 0 か 1 であることを確かめて回数を記録する。ほかは数をそのまま比べる。"""
    if rule == "zero-or-one":
        if len(terms) not in (0, 1):
            raise Failure(f"{side}: POSIX の方式の grace=0 で品質コマンドが受けた TERM が 0 か 1 でない: {len(terms)}")
        relax.add("POSIX の grace=0 の TERM の数")
        print(f"  記録({side}): POSIX の方式の grace=0 で品質コマンドが受けた TERM {len(terms)} 回", flush=True)
        return "0 か 1"
    if rule != "many":
        return len(terms)
    if len(terms) < 2:
        raise Failure(f"{side}: TERM を送り直した回数が 2 回より少ない: {len(terms)}")
    relax.add("TERM の回数")
    print(f"  記録({side}): 品質コマンドが受けた TERM {len(terms)} 回", flush=True)
    return "2 回以上"


def file_mode(path):
    """ファイルの種類と権限(リンクは辿らない)。無ければ None。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)]


def snapshot(paths):
    """入力のファイルの形(種類・権限・中身の SHA-256・リンク先)。"""
    shot = {}
    for path in paths:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            shot[path] = None
            continue
        if stat.S_ISLNK(info.st_mode):
            shot[path] = ("link", os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            shot[path] = ("file", stat.S_IMODE(info.st_mode), hashlib.sha256(Path(path).read_bytes()).hexdigest())
        else:
            shot[path] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode))
    return shot


# ---- Python と固定期待の確かめ ----

def check_legacy_scripts():
    for name, path in (("check-process.py", CP_SCRIPT), ("loop-supervisor.py", LS_SCRIPT)):
        if hashlib.sha256(path.read_bytes()).hexdigest() != LEGACY_SHA256[name]:
            raise Failure(f"旧版 {name} が基準(main e19ca1d・b1744f7 と同じ中身)と違う")


def check_python(python):
    """R4Q_PYTHON を確かめる(build.py の調べと表を使う)。戻り値は調べた結果。"""
    if not python or not os.path.isabs(python):
        raise Failure("R4Q_PYTHON に本家 3.10.11 の Python の絶対パスを渡す")
    try:
        report = build.r4q_python_report(python)
    except ValueError as exc:
        raise Failure(str(exc)) from exc
    problems = []
    if report["implementation"] != "CPython" or report["version"] != build.R4Q_LEGACY_PYTHON:
        problems.append(f"CPython {build.R4Q_LEGACY_PYTHON} でない({report['implementation']} {report['version']})")
    different = [name for name, digest in build.R4Q_STDLIB_SHA256.items() if report["sha256"].get(name) != digest]
    if different:
        problems.append("標準ライブラリが本家 3.10.11 と違う: " + "・".join(different))
    if LINUX and report["pidfd_call"] != "成功":
        problems.append("Linux で os.pidfd_open・signal.pidfd_send_signal を使えない")
    if problems:
        raise Failure("基準でない Python: " + "・".join(problems))
    return report


def build_probe(temp):
    out = os.path.join(temp, "r4q_probe")
    env = {"PATH": os.environ.get("PATH", CHILD_PATH), "TMPDIR": temp, "LC_ALL": "C"}
    done = subprocess.run(["cc", "-O1", "-Wall", "-Wextra", "-Werror", "-o", out, str(PROBE_SOURCE)], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    if done.returncode != 0:
        raise Failure("r4q_probe.c を作れない: " + done.stdout.decode(errors="replace"))
    return out


def check_locale(python, locale_name):
    """env を渡さないケースの地域設定が入っていることを、旧版の Python で確かめる。"""
    env = {"PATH": CHILD_PATH, "LANG": locale_name}
    done = subprocess.run([python, "-I", "-B", "-c",
                           "import locale, sys; print(locale.setlocale(locale.LC_ALL, '')); "
                           "print(sys.getfilesystemencoding())"],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    lines = done.stdout.decode(errors="replace").split()
    if done.returncode != 0 or done.stderr or len(lines) != 2 or lines[1] != "utf-8":
        raise Failure(f"地域設定 {locale_name} が入っていない: {done.stdout!r} {done.stderr!r}")
    return lines[0]


# ---- 前提のケース: strerror ----

def mac_elast_from_sdk():
    """SDK の sys/errno.h の ELAST と SDK の版。読めなければ失敗(未実施にしない)。"""
    try:
        sdk = subprocess.run(["xcrun", "--show-sdk-path"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=60)
        version = subprocess.run(["xcrun", "--show-sdk-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Failure(f"xcrun を使えない: {exc}") from exc
    if sdk.returncode != 0 or not sdk.stdout.strip() or version.returncode != 0:
        raise Failure(f"SDK が無い: {sdk.stderr.strip()} {version.stderr.strip()}")
    header = Path(sdk.stdout.strip()) / "usr/include/sys/errno.h"
    try:
        text = header.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise Failure(f"sys/errno.h が読めない: {exc}") from exc
    values = re.findall(r"^\s*#\s*define\s+ELAST\s+(\d+)\b", text, re.MULTILINE)
    if not values:
        raise Failure(f"{header} に ELAST の行が無い")
    if len(set(values)) != 1:
        raise Failure(f"{header} の ELAST が 1 つに決まらない: {sorted(set(values))}")
    return int(values[0]), version.stdout.strip(), sdk.stdout.strip()


def mac_runtime_version():
    out = {}
    for key in ("ProductVersion", "BuildVersion"):
        done = subprocess.run(["sw_vers", "-" + key[0].lower() + key[1:]], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=60)
        if done.returncode != 0 or not done.stdout.strip():
            raise Failure(f"sw_vers の {key} を読めない: {done.stderr.strip()}")
        out[key] = done.stdout.strip()
    return out


def run_strerror(ctx):
    """Rust の表(表に無いときの文を含む)と旧版の os.strerror を 0〜1024 の全部で照らす(08:55)。"""
    done = subprocess.run([ctx.rust, "--r4q-strerror", str(STRERROR_LAST)], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=60, env=child_env(ctx))
    if done.returncode != 0:
        raise Failure("--r4q-strerror が失敗: " + done.stderr.decode(errors="replace"))
    ours = json.loads(done.stdout)
    script = (f"import json, os; print(json.dumps({{str(n): os.strerror(n) for n in range(0, {STRERROR_LAST + 1})}}, "
              "ensure_ascii=False))")
    legacy_done = subprocess.run([ctx.python, "-I", "-B", "-c", script], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=60, env=child_env(ctx))
    if legacy_done.returncode != 0:
        raise Failure("旧版の Python の os.strerror を読めない: " + legacy_done.stderr.decode(errors="replace"))
    legacy = json.loads(legacy_done.stdout)
    return ours, legacy


def check_strerror(ours, legacy, mac=None):
    """全部の番号が同じこと・表の外が Unknown error の形であること(違えば Failure)。戻り値は記録の行。"""
    mac = MAC if mac is None else mac
    numbers = [str(n) for n in range(0, STRERROR_LAST + 1)]
    if sorted(ours, key=int) != numbers or sorted(legacy, key=int) != numbers:
        raise Failure("strerror の番号の並びが 0〜1024 でない")
    different = [n for n in numbers if ours[n] != legacy[n]]
    if different:
        raise Failure(f"strerror が旧版の Python と違う番号 {len(different)} 件: " + ", ".join(
            f"{n}(Rust {ours[n]!r}・旧版 {legacy[n]!r})" for n in different[:10]))
    last = MAC_TABLE_LAST if mac else LINUX_TABLE_LAST
    form = "Unknown error: {}" if mac else "Unknown error {}"
    beyond = [n for n in range(last + 1, STRERROR_LAST + 1) if legacy[str(n)] != form.format(n)]
    if beyond:
        raise Failure(f"表の最後の番号 {last} より後に Unknown error でない番号がある(表を作り直す): {beyond[:10]}")
    if legacy[str(last)] == form.format(last):
        raise Failure(f"表の最後の番号 {last} の文が Unknown error(表が実際より長い)")
    return [f"strerror: 0〜{STRERROR_LAST} の {len(numbers)} 件が旧版の os.strerror と同じ(表の最後 {last}・"
            f"0 は {legacy['0']!r})"]


def case_strerror(ctx, case, relax):
    ours, legacy = run_strerror(ctx)
    for line in check_strerror(ours, legacy):
        print("  " + line, flush=True)
    if MAC:
        elast, version, sdk = mac_elast_from_sdk()
        runtime = mac_runtime_version()
        print(f"  Mac: SDK {version}({sdk})の sys/errno.h の ELAST {elast}・表の最後の番号 {MAC_TABLE_LAST}・"
              f"実行時の macOS {runtime['ProductVersion']}({runtime['BuildVersion']})・"
              f"os.strerror(107) {legacy['107']!r}・os.strerror(108) {legacy['108']!r}", flush=True)
        if elast > MAC_TABLE_LAST:
            raise Failure(f"SDK の ELAST {elast} が表の最後の番号 {MAC_TABLE_LAST} より大きい(表が足りない)")


# ---- 差し替えの比較: 旧版の流し直し ----

def regenerate_injected(ctx):
    """R4Q_PYTHON で gen_injected.py を流し直し、injected.json とバイト列で同じことを確かめる。"""
    out = os.path.join(ctx.temp, "injected.json")
    done = subprocess.run([ctx.python, "-B", str(GEN_INJECTED), "--out", out], env=child_env(ctx),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600, cwd=ctx.temp)
    try:
        if done.returncode != 0:
            raise Failure("gen_injected.py が失敗: " + done.stderr.decode(errors="replace"))
        fresh = Path(out).read_bytes()
    finally:
        if os.path.lexists(out):
            os.unlink(out)
    fixed = INJECTED.read_bytes()
    document = json.loads(fixed)
    ids = [item["id"] for item in document["cases"]]
    if len(ids) != len(set(ids)) or not ids or document.get("count") != len(ids):
        raise Failure("injected.json の ID が重なるか、0 件か、件数が count と違う")
    for fixed_id in FIXED_INJECTED:
        if fixed_id not in ids:
            raise Failure(f"必ず流す差し替えの ID が無い: {fixed_id}")
    if fresh != fixed:
        try:
            regenerated = {item["id"]: item for item in json.loads(fresh)["cases"]}
        except (ValueError, KeyError):
            regenerated = {}
        changed = [i for i in ids if regenerated.get(i) != next(c for c in document["cases"] if c["id"] == i)]
        return document, f"gen_injected.py の出力が injected.json と違う(手で直した固定期待か、旧版の変化): " \
                         f"違う ID {changed[:10]}"
    return document, None


def case_injected(ctx, case, relax):
    if ctx.injected is None:
        raise Failure(ctx.injected_problem or "差し替えの固定期待を読めない")
    if ctx.injected_problem is not None:
        raise Failure(ctx.injected_problem)
    if case.id not in ctx.injected:
        raise Failure("injected.json にこの ID が無い")


# ---- 監督の CLI のケース(ls-・cp-・py-・loop-) ----

def substitute(value, case_dir, token, fd=None):
    if isinstance(value, str):
        value = value.replace("@D@", case_dir).replace("@T@", token)
        return value.replace("@FD@", str(fd)) if fd is not None else value
    if isinstance(value, bytes):
        return value.replace(b"@D@", os.fsencode(case_dir)).replace(b"@T@", token.encode())
    if isinstance(value, list):
        return [substitute(item, case_dir, token, fd) for item in value]
    if isinstance(value, tuple):
        return tuple(substitute(item, case_dir, token, fd) for item in value)
    if isinstance(value, dict):
        return {key: substitute(item, case_dir, token, fd) for key, item in value.items()}
    return value


def prepare(case_dir, ops, token):
    """ケースの入力(ファイル・フォルダ・リンク)を作る。戻り値は作った入力のパス。"""
    made = []
    for op in ops:
        kind = op[0]
        path = os.path.join(case_dir, substitute(op[1], case_dir, token))
        if kind == "dir":
            os.makedirs(path, exist_ok=True)
        elif kind == "file":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as stream:
                stream.write(substitute(op[2], case_dir, token))
            os.chmod(path, op[3] if len(op) > 3 else 0o644)
        elif kind == "symlink":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.symlink(substitute(op[2], case_dir, token), path)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            raise Failure(f"知らない入力の種類: {kind}")
        made.append(path)
    return made


def probe_cmd(*options):
    return ["@P@", "state", "@D@/state.json", "--mark", "@T@", *options]


def sh(script):
    return ["sh", "-c", script, "@T@"]


def side_argv(ctx, side, kind, args):
    if side == "legacy":
        return [ctx.python, "-B", str(SCRIPT_OF[kind]), *args]
    return [ctx.rust, FLAGS[kind], *args]


def with_probe(ctx, values):
    return [ctx.probe if item == "@P@" else item for item in values]


def started(case_dir):
    return os.path.exists(os.path.join(case_dir, "started"))


class PipeReader:
    """出力をパイプで受けて、別のスレッドで読み切る(RLIMIT_FSIZE の掛からない向け先)。"""

    def __init__(self):
        import threading
        self.read_fd, self.write_fd = os.pipe()
        self.data = bytearray()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while True:
            chunk = os.read(self.read_fd, 65536)
            if not chunk:
                break
            self.data += chunk

    def finish(self):
        self.thread.join(60)
        if self.thread.is_alive():
            raise Failure("パイプの出力を読み切れない")
        os.close(self.read_fd)
        return bytes(self.data)


def open_target(spec, case_dir, name, keep, readers=None):
    """出力の向け先(file・pipe・devfull・dir・closed-pipe・full-pipe)の fd と、読み口(full-pipe)。"""
    if spec == "pipe":
        reader = PipeReader()
        readers[name] = reader
        return reader.write_fd, None
    if spec == "file":
        fd = os.open(os.path.join(case_dir, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    elif spec == "devfull":
        if not os.path.exists("/dev/full"):
            raise Skip(SKIP_DEVFULL)
        fd = os.open("/dev/full", os.O_WRONLY)
    elif spec == "dir":
        fd = os.open(case_dir, os.O_RDONLY | os.O_DIRECTORY)
    elif spec == "closed-pipe":
        read_fd, fd = os.pipe()
        os.close(read_fd)
    elif spec == "full-pipe":
        read_fd, fd = os.pipe()
        os.set_blocking(fd, False)
        try:
            while True:
                os.write(fd, b"p" * 65536)
        except BlockingIOError:
            pass
        os.set_blocking(fd, True)
        keep.append(read_fd)
        keep.append(fd)
        return fd, read_fd
    else:
        raise Failure(f"知らない出力の形: {spec}")
    keep.append(fd)
    return fd, None


def read_notice(fd, proc, timeout=60):
    """anchor の通知の行(改行まで)を読む。EOF か期限で止める。"""
    data = b""
    deadline = time.monotonic() + timeout
    import select
    while b"\n" not in data and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            continue
        chunk = os.read(fd, 4097)
        if not chunk:
            break
        data += chunk
    return data


def cli_side(ctx, case, side, case_dir, token, relax, *, fsize=True):
    """監督を直接起動する 1 回(旧版か Rust)。戻り値は観測。"""
    params = case.params
    made = prepare(case_dir, params.get("prepare", []), token)
    before = snapshot(made)
    keep = []
    notice_fd = None
    pass_fds = ()
    close_after = []
    write_fd = None
    if params.get("anchor_pipe"):
        notice_fd, write_fd = os.pipe()
        keep.append(notice_fd)
        pass_fds = (write_fd,)
        close_after.append(write_fd)
    args = with_probe(ctx, substitute(params["args"], case_dir, token, write_fd))
    env = child_env(ctx, substitute(params.get("env", {}), case_dir, token))
    argv = side_argv(ctx, side, params["kind"], args)
    targets = {}
    readers = {}
    try:
        out, out_read = open_target(params.get("stdout", "file"), case_dir, "stdout", keep, readers)
        err, _ = open_target(params.get("stderr", "file"), case_dir, "stderr", keep, readers)
        for reader in readers.values():
            close_after.append(reader.write_fd)
        stdin = subprocess.DEVNULL
        if params.get("stdin") == "dir":
            stdin = os.open(case_dir, os.O_RDONLY | os.O_DIRECTORY)
            keep.append(stdin)
        targets["stdout-read"] = out_read
        during = None
        action = params.get("action")
        if action is not None:
            def during(proc):
                action(ctx, proc, case_dir, targets, notice_fd, side)
        elif notice_fd is not None:
            def during(proc):
                targets["notice"] = read_notice(notice_fd, proc)
                anchor_finish(ctx, proc, case_dir, targets, notice_fd, side)
        options = dict(params)
        if not fsize:
            options.pop("fsize", None)
        run = launch(ctx, argv, env=env, cwd=case_dir, stdin=stdin, stdout=out, stderr=err,
                     preexec=preexec_of(options), during=during, pass_fds=pass_fds, close_after=close_after,
                     new_session=bool(params.get("anchor_pipe")), timeout=params.get("timeout"))
    finally:
        for fd in keep:
            try:
                os.close(fd)
            except OSError:
                pass
    obs = {"exit": run.exit}
    for name in ("stdout", "stderr"):
        path = os.path.join(case_dir, name)
        if name in readers:
            data = readers[name].finish()
        else:
            data = Path(path).read_bytes() if params.get(name, "file") == "file" else b""
        if side == "legacy":
            data = fixed_names(ctx, data, relax)
        if name == "stderr":
            data = mask_thread(data, relax)
        obs[name] = data
    if "drained" in targets:
        obs["drained"] = targets["drained"]
    if "record" in params:
        path = os.path.join(case_dir, params["record"])
        obs["record"] = Path(path).read_bytes() if os.path.isfile(path) and not os.path.islink(path) else None
        # 記録の権限(旧版は O_EXCL・0o600 と umask)。
        obs["record_mode"] = file_mode(path)
        if params.get("loop") and obs["record"] is not None:
            obs["loop"] = loop_result(ctx, path)
    if notice_fd is not None:
        obs["notice"] = targets.get("notice")
        obs["survived"] = targets.get("survived")
    view, _ = state_view(ctx, read_state(case_dir), run.pid, relax, params["kind"],
                         allowed_env_names(declared_names(params.get("env"))), side, side, case_id=case.id)
    if view is not None:
        obs["state"] = view
        obs["state_pid"] = read_state(case_dir)["pid"]
    if params.get("term_rule"):
        obs["terms"] = check_terms(params["term_rule"], term_times(case_dir), relax, side)
    grand = os.path.join(case_dir, "grand")
    if os.path.exists(grand):
        obs["grandchild_alive"] = alive(int(Path(grand).read_text()))
    obs["files"] = sorted(os.listdir(case_dir))
    obs["leftover"] = sweep(token)
    if not params.get("leftover_ok") and obs["leftover"]:
        raise Failure(f"{side}: 残ったプロセスが {obs['leftover']} 件ある")
    after = snapshot(made)
    if after != before:
        raise Failure(f"{side}: 入力のファイルが変わった: {sorted(k for k in before if before[k] != after.get(k))}")
    return obs


def anchor_finish(ctx, proc, case_dir, targets, notice_fd, side):
    """anchor を直接起動したとき: 通知の後に TERM・INT・HUP を送っても生きていることを確かめ、群を KILL する。"""
    if proc.poll() is None:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            os.kill(proc.pid, number)
        time.sleep(0.3)
        targets["survived"] = proc.poll() is None
    else:
        targets["survived"] = False
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def compare_values(legacy, rust, what="旧版と Rust"):
    if legacy != rust:
        raise Failure(describe(legacy, rust, what))


def describe(legacy, rust, what="旧版と Rust"):
    lines = []
    if isinstance(legacy, dict) and isinstance(rust, dict):
        keys = sorted(set(legacy) | set(rust), key=str)
        for key in keys:
            a, b = legacy.get(key), rust.get(key)
            if a != b:
                lines.append(f"{key}: 旧版 {short(a)} / Rust {short(b)}")
    else:
        lines.append(f"旧版 {short(legacy)} / Rust {short(rust)}")
    return f"{what}が違う: " + " | ".join(lines)


def short(value, limit=1200):
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"…(全 {len(text)} 字)"


def case_cli(ctx, case, relax):
    """旧版と Rust を同じケースのフォルダで順に流し、観測を比べる。"""
    case_dir = os.path.join(ctx.temp, "c", case.id)
    os.makedirs(case_dir)
    try:
        observed = {}
        for side in ("legacy", "rust"):
            try:
                if case.params.get("fsize_cut") and side == "legacy":
                    observed[side] = fsize_cut_legacy(ctx, case, case_dir, relax)
                else:
                    observed[side] = cli_side(ctx, case, side, case_dir, ctx.token, relax)
            finally:
                clear(case_dir)
        post = case.params.get("post")
        if post is not None:
            post(ctx, case, observed, relax)
        for side in ("legacy", "rust"):
            observed[side].pop("state_pid", None)
        compare_values(observed["legacy"], observed["rust"])
        check_expect(case, observed["legacy"])
    finally:
        remove_tree(case_dir)


def fsize_cut_legacy(ctx, case, case_dir, relax):
    """22:26・22:38: 旧版は上限を付けて終了値を、付けずに全部の出力を取り、固定名にしてから同じバイト数で切る。"""
    limited = cli_side(ctx, case, "legacy", case_dir, ctx.token, Relax())
    clear(case_dir)
    full = cli_side(ctx, case, "legacy", case_dir, ctx.token, relax, fsize=False)
    size = case.params["fsize"]
    full["exit"] = limited["exit"]
    for name in ("stdout", "stderr"):
        full[name] = full[name][:size]
    full["files"] = limited["files"]
    return full


def check_expect(case, obs):
    """ケースの ID ごとの固定期待(旧版の終わり方。経路の誤りを見つける)。"""
    expect = case.params.get("expect")
    if expect is None:
        return
    if isinstance(expect, dict):
        expect = expect[platform_name()]
    if callable(expect):
        problem = expect(obs)
        if problem:
            raise Failure(f"固定期待と違う: {problem}")
        return
    if obs.get("exit") != expect:
        raise Failure(f"固定期待と違う: 旧版の終了値 {obs.get('exit')}(期待 {expect})")


def loop_result(ctx, record):
    """loop.sh の supervisor-result を、loop.sh から取り出して記録に当てる(scripts/test_loop_supervisor.py の形)。"""
    helper = os.path.join(ctx.temp, "loop-helper.py")
    if not os.path.exists(helper):
        lines = LOOP_SH.read_text(encoding="utf-8").split("\n")
        start = lines.index("read -r -d '' PY_HELPER <<'PY' || true") + 1
        end = lines.index("PY", start)
        Path(helper).write_text("\n".join(lines[start:end]) + "\n", encoding="utf-8")
    done = subprocess.run([ctx.python, "-B", helper, "supervisor-result", record, "n"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=60, env=child_env(ctx))
    return [done.returncode, done.stdout, done.stderr]


def keyerror_pid(ctx, case, observed, relax):
    """KeyError: <pid> の pid を伏せ、記録した品質コマンドの pid と等しいことを確かめる(06:58)。"""
    if not LINUX:
        return
    for side in ("legacy", "rust"):
        obs = observed[side]
        found = re.findall(rb"KeyError: (\d+)\n", obs["stderr"])
        if len(found) != 1 or int(found[0]) != obs.get("state_pid"):
            raise Failure(f"{side}: KeyError の pid が品質コマンドの pid と違う: {found} {obs.get('state_pid')}")
        obs["stderr"] = re.sub(rb"KeyError: \d+\n", b"KeyError: @PID@\n", obs["stderr"])
        relax.add("pid")


def sigint_stable(ctx, case, observed, relax):
    """22:31: SIGINT で中断した監督は、信号・終了値・最後の行・それまでの出力だけを比べる。"""
    for side in ("legacy", "rust"):
        obs = observed[side]
        err = obs["stderr"]
        if obs["exit"] != -signal.SIGINT:
            raise Failure(f"{side}: SIGINT で終わっていない(終了値 {obs['exit']})")
        if not err.endswith(b"KeyboardInterrupt\n"):
            raise Failure(f"{side}: 最後の行が KeyboardInterrupt でない: {err[-200:]!r}")
        head, sep, _ = err.partition(b"Traceback (most recent call last):\n")
        obs["stderr"] = head + sep + b"@SIGINT@\nKeyboardInterrupt\n"
        relax.add("SIGINT の呼出履歴")


# ---- 動き(実プロセスの途中の操作) ----

def wait_started(case_dir, proc, timeout=60):
    wait_for(lambda: started(case_dir) or proc.poll() is not None, "品質コマンドの始めの印", timeout)
    if not started(case_dir):
        raise Failure("品質コマンドが始める前に親か監督が終わった")


def cli_signal(number):
    """監督を直接起動したケース: 品質コマンドが始めたら、監督へ信号を送る。"""
    def action(ctx, proc, case_dir, targets, notice_fd, side):
        wait_started(case_dir, proc)
        os.kill(proc.pid, number)
    return action


def burst(pid):
    """止めたプロセスへ TERM・INT・HUP を送ってから再開する(3 つが同時に届き、処理が入れ子になる)。"""
    os.kill(pid, signal.SIGSTOP)
    try:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            os.kill(pid, number)
    finally:
        os.kill(pid, signal.SIGCONT)


def cli_burst(ctx, proc, case_dir, targets, notice_fd, side):
    wait_started(case_dir, proc)
    burst(proc.pid)


def pdeathsig_side(ctx, case, side, case_dir, token, relax):
    """監督の親(sh)を KILL し、PDEATHSIG で監督が TERM を受けて片付けることを見る。"""
    args = with_probe(ctx, substitute(case.params["args"], case_dir, token))
    argv = side_argv(ctx, side, "ls", args)
    quoted = " ".join("'" + item.replace("'", "'\\''") + "'" for item in argv)
    script = (f"{quoted} > '{case_dir}/stdout' 2> '{case_dir}/stderr' & echo $! > '{case_dir}/monitor.pid'; wait")

    def during(proc):
        wait_started(case_dir, proc)
        pid = int(Path(os.path.join(case_dir, "monitor.pid")).read_text())
        os.kill(proc.pid, signal.SIGKILL)
        wait_for(lambda: not alive(pid), "監督の終わり(親の死の通知)", timeout=60)

    run = launch(ctx, ["sh", "-c", script, token], env=child_env(ctx), cwd=case_dir, stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, during=during)
    obs = {"sh_exit": run.exit}
    for name in ("stdout", "stderr"):
        data = Path(os.path.join(case_dir, name)).read_bytes()
        obs[name] = fixed_names(ctx, data, relax) if side == "legacy" else data
    record = os.path.join(case_dir, "rec")
    obs["record"] = Path(record).read_bytes() if os.path.isfile(record) else None
    if obs["record"] is not None:
        obs["loop"] = loop_result(ctx, record)
    obs["state"], _ = state_view(ctx, read_state(case_dir), None, relax, "ls",
                                 allowed_env_names(declared_names({"PWD": ""})), side, side, case_id=case.id)
    obs["state"]["ppid==top"] = None
    obs["leftover"] = sweep(token)
    if obs["leftover"]:
        raise Failure(f"{side}: 残ったプロセスが {obs['leftover']} 件ある")
    return obs


def case_pdeathsig(ctx, case, relax):
    case_dir = os.path.join(ctx.temp, "c", case.id)
    os.makedirs(case_dir)
    try:
        observed = {}
        for side in ("legacy", "rust"):
            try:
                observed[side] = pdeathsig_side(ctx, case, side, case_dir, ctx.token, relax)
            finally:
                clear(case_dir)
        compare_values(observed["legacy"], observed["rust"])
        check_expect(case, observed["legacy"])
    finally:
        remove_tree(case_dir)


def case_closed_std(ctx, case, relax):
    """08:55: 監督を直接起動して fd 0〜2 を閉じたときは、Rust の標準の初期化(/dev/null で開き直す)をそのまま使い、
    旧版との差を認める。両方の結果(終了値)を記録し、比べない(閉じた fd 0〜2)。"""
    results = {}
    for side in ("legacy", "rust"):
        argv = side_argv(ctx, side, case.params["kind"], case.params["args"])

        def close_std():
            for fd in (0, 1, 2):
                os.close(fd)
        run = launch(ctx, argv, env=child_env(ctx), cwd=ctx.temp, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, preexec=close_std)
        results[side] = run.exit
        if sweep(ctx.token):
            raise Failure(f"{side}: 残ったプロセスがある")
    relax.add("閉じた fd 0〜2")
    print(f"  記録: fd 0〜2 を閉じて起動した監督の終了値 旧版 {results['legacy']}・Rust {results['rust']}(比べない)",
          flush=True)


def drain_after_sigint(ctx, proc, case_dir, targets, notice_fd, side):
    """stdout の詰まったパイプへの書き込みで止まった監督に SIGINT を送り、パイプを空にして終わりを待つ。"""
    time.sleep(0.5)
    os.kill(proc.pid, signal.SIGINT)
    time.sleep(0.3)
    read_fd = targets["stdout-read"]
    os.set_blocking(read_fd, False)
    deadline = time.monotonic() + 60
    data = b""
    while proc.poll() is None and time.monotonic() < deadline:
        try:
            data += os.read(read_fd, 65536)
        except BlockingIOError:
            time.sleep(0.01)
    try:
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            data += chunk
    except BlockingIOError:
        pass
    targets["drained"] = data


# ---- run_command のケース(rc-・probe-) ----

def hexs(text):
    return text.encode("utf-8", "surrogatepass").hex() if isinstance(text, str) else text.hex()


def pair_value(result, name):
    for key, value in result:
        if key == {"str": hexs(name)}:
            return value
    return None


def rc_request(ctx, params, case_dir, token, monitor):
    directory = os.path.join(case_dir, params.get("directory", "control"))
    if not params.get("no_directory"):
        os.makedirs(directory, exist_ok=True)
    argv = with_probe(ctx, substitute(params["argv"], case_dir, token))
    env = params.get("env", "default")
    if env == "default":
        env = [["PATH", CHILD_PATH], ["LANG", ctx.locale]]
    env = substitute(env, case_dir, token)
    if monitor == "fake":
        monitor_value = {"program": hexs(ctx.python), "prefix": [hexs("-B"), hexs(str(FAKE))]}
        spec_path = os.path.join(case_dir, "fake-spec.json")
        spec = substitute(params["fake"], case_dir, token)
        if "data" in spec and isinstance(spec["data"], bytes):
            spec["data"] = spec["data"].hex()
        with open(spec_path, "w", encoding="utf-8") as stream:
            json.dump(spec, stream)
        env = (env or []) + [["R4Q_FAKE_SPEC", spec_path]]
    elif monitor == "missing":
        monitor_value = {"program": hexs(os.path.join(case_dir, "no-such-monitor")), "prefix": [hexs("-B")]}
    else:
        monitor_value = monitor
    request = {
        "argv": [hexs(item) for item in argv],
        "cwd": None if params.get("cwd") is None else hexs(substitute(params["cwd"], case_dir, token)),
        "env": None if env is None else [[hexs(k), hexs(v)] for k, v in env],
        "timeout": params.get("timeout", {"int": "5"}), "grace": params.get("grace", {"float": "0.2"}),
        "method": params["method"], "keep": params.get("keep", 0), "directory": hexs(directory),
        "monitor": monitor_value, "int_max_str_digits": 4300,
    }
    path = os.path.join(case_dir, "request.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(request, stream)
    names = set()
    if env is not None:
        names.update(key for key, _ in env)
    else:
        names.update(declared_names({"R4Q_PYTHON": ""}))
    return path, directory, names


def parent_env(ctx):
    """比べる親の環境(env を渡さないケースでは、監督と品質コマンドがこれを受け継ぐ)。"""
    return child_env(ctx, {"R4Q_PYTHON": ctx.python})


def rc_side(ctx, case, parent, monitor, case_dir, token, relax):
    """1 つの組(親 × 監督)の 1 回。戻り値は観測。"""
    params = case.params
    made = prepare(case_dir, params.get("prepare", []), token)
    before = snapshot(made)
    request, directory, env_names = rc_request(ctx, params, case_dir, token, monitor)
    if parent == "legacy":
        argv = [ctx.python, "-B", str(PARENT), request, ctx.rust]
    else:
        argv = [ctx.rust, "--r4q-run-command", request]
    out = os.open(os.path.join(case_dir, "parent-out"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    err = os.open(os.path.join(case_dir, "parent-err"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    keep = [out, err]
    action = params.get("action")
    hold = params.get("hold")
    during = None
    if action is not None:
        def during(proc):
            action(ctx, proc, case_dir, parent, monitor)
    stdin = subprocess.DEVNULL
    result_file = os.path.join(case_dir, "hold-result")
    if hold:
        read_fd, write_fd = os.pipe()
        keep.append(write_fd)
        argv = [ctx.probe, "hold", "30", str(os.getpid()), result_file, "--", *argv]
        stdin = read_fd

        def during(proc):
            os.close(read_fd)
            wait_for(lambda: os.path.exists(result_file) or proc.poll() is not None, "比べる親の終わり",
                     timeout=120)
            child_pid = Path(os.path.join(case_dir, "held.pid")).read_text().strip() \
                if os.path.exists(os.path.join(case_dir, "held.pid")) else None
            if ctx.hold_report is not None:
                Path(ctx.hold_report).write_text(json.dumps({"helper": proc.pid, "held": child_pid}) + "\n",
                                                 encoding="utf-8")
                time.sleep(20)  # 自己検査が、補助の保持の途中で比較器を KILL する間
            os.close(write_fd)
            keep.remove(write_fd)
    try:
        run = launch(ctx, argv, env=parent_env(ctx), cwd=case_dir, stdin=stdin,
                     stdout=out, stderr=err, preexec=preexec_of(params), during=during,
                     timeout=params.get("run_timeout"))
    finally:
        for fd in keep:
            os.close(fd)
    if hold:
        if run.exit != 0:
            raise Failure(f"{parent}: subreaper の補助が失敗した(終了値 {run.exit})")
        run.exit = os.waitstatus_to_exitcode(int(Path(result_file).read_text().strip()))
    raw = Path(os.path.join(case_dir, "parent-out")).read_bytes()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise Failure(f"{parent}: 親の結果が JSON でない: {raw[:400]!r}") from exc
    obs = {"exit": run.exit, "parent_stderr": Path(os.path.join(case_dir, "parent-err")).read_bytes()}
    nonces = set()
    legacy_monitor = monitor in ("legacy", "fake", "missing")
    files = {}
    for name in ("stdout", "stderr"):
        path = os.path.join(directory, name)
        if os.path.isfile(path):  # リンクは辿る(旧版の open('w+b') もリンクを辿る)
            files[name] = Path(path).read_bytes()
    if "result" in data:
        result = data["result"]
        value = pair_value(result, "nonce")
        if value is not None:
            nonces.add(check_nonce(bytes.fromhex(value["str"]).decode("utf-8", "surrogatepass"), f"{parent} の辞書"))
            value["str"] = "@NONCE@"
            relax.add("nonce")
        check_streams(ctx, params, result, files, legacy_monitor, relax, parent)
    else:
        message = bytes.fromhex(data["error"]["message"]).decode("utf-8", "surrogatepass")
        if parent == "legacy" and monitor in ("legacy", "rust", "fake"):
            message = normalize_timeout_text(ctx, message, relax, monitor)
        data["error"]["message"] = message
        if params.get("outside_writer") and data["error"]["message"] != "停止後の品質出力が短くなった":
            raise Failure(f"群の外の書き手のケースの誤りが、旧版の出しうる文でない: {data['error']['message']}")
    obs["result"] = data
    record_path = os.path.join(directory, "recovery.json")
    # 記録(旧版は O_EXCL・0o600)と出力のファイル(旧版は open('w+b') の 0o666)の権限。umask は両側で同じ。
    obs["modes"] = {name: file_mode(os.path.join(directory, name)) for name in ("recovery.json", "stdout", "stderr")}
    if os.path.isfile(record_path) and not os.path.islink(record_path):
        raw = Path(record_path).read_bytes()
        try:
            record_nonce = json.loads(raw).get("nonce")
        except (ValueError, AttributeError, RecursionError):
            record_nonce = None
        if isinstance(record_nonce, str) and NONCE_RE.fullmatch(record_nonce):
            nonces.add(record_nonce)
            raw = raw.replace(record_nonce.encode(), b"@NONCE@")
            relax.add("nonce")
        obs["record"] = raw
    elif os.path.lexists(record_path):
        obs["record"] = ("種類", stat.S_IFMT(os.lstat(record_path).st_mode))
    for name, content in files.items():
        if params.get("outside_writer") and name == "stdout":
            print(f"  記録({parent}×{monitor}): 群の外の書き手の後の stdout のファイル {len(content)} バイト・"
                  f"sha256 {hashlib.sha256(content).hexdigest()}", flush=True)
            obs[name] = "@WRITER@"
            relax.add("群の外の書き手")
        else:
            obs[name] = fixed_names(ctx, content, relax) if legacy_monitor else content
    state = read_state(case_dir)
    view, argv_nonce = state_view(ctx, state, run.pid, relax, "cp", allowed_env_names(env_names),
                                  f"{parent}×{monitor}", monitor, mask_nonce=True, case_id=case.id)
    if view is not None:
        if argv_nonce is not None:
            nonces.add(argv_nonce)
        obs["state"] = view
        obs["state_pid"] = state["pid"]
    if len(nonces) > 1:
        raise Failure(f"{parent}×{monitor}: 監督の argv・記録・辞書の nonce が等しくない: {sorted(nonces)}")
    for nonce in nonces:
        if nonce in ctx.nonces:
            raise Failure(f"{parent}×{monitor}: nonce が前の回と同じ")
        ctx.nonces.add(nonce)
    if params.get("nonce_seen") and not nonces:
        raise Failure(f"{parent}×{monitor}: nonce を 1 つも確かめられない")
    if params.get("term_rule"):
        times = term_times(case_dir)
        obs["terms"] = check_terms(params["term_rule"], times, relax, f"{parent}×{monitor}")
        floor = params.get("grace_floor")
        if floor is not None:
            if not times:
                raise Failure(f"{parent}×{monitor}: TERM を受けていないので、片付けの経過を確かめられない")
            spent = run.ended - times[0]
            if spent < floor:
                raise Failure(f"{parent}×{monitor}: TERM から親の終わりまでが {spent:.3f} 秒で、grace {floor} 秒より短い")
    obs["leftover"] = sweep(token)
    if not params.get("leftover_ok") and obs["leftover"]:
        raise Failure(f"{parent}×{monitor}: 残ったプロセスが {obs['leftover']} 件ある")
    after = snapshot(made)
    if after != before:
        raise Failure(f"{parent}×{monitor}: 入力のファイルが変わった")
    if params.get("keyerror"):
        check_keyerror_file(obs, parent, monitor, relax)
    obs.pop("state_pid", None)
    return obs


def check_streams(ctx, params, result, files, legacy_monitor, relax, parent):
    """辞書の *_sha256・*_bytes・*_tail が出力のファイルと合うこと。旧版の監督の出力に固定名があれば、
    固定名にした中身から値を作り直す(固定名の欄の派生)。群の外の書き手は 06:58 の規則。"""
    keep = params.get("keep", 0)
    for name in ("stdout", "stderr"):
        sha = pair_value(result, name + "_sha256")
        if sha is None:
            continue
        if params.get("outside_writer") and name == "stdout":
            size = pair_value(result, "stdout_bytes")
            if size is None or int(size["int"]) < params["outside_writer"]:
                raise Failure(f"{parent}: 群の外の書き手のケースで stdout_bytes が始めの長さより短い: {size}")
            for item_name in ("stdout_sha256", "stdout_tail", "stdout_bytes"):
                item = pair_value(result, item_name)
                if item is not None:
                    for key in list(item):
                        item[key] = "@WRITER@"
            relax.add("群の外の書き手")
            continue
        content = files.get(name)
        if content is None:
            raise Failure(f"{parent}: 辞書に {name}_sha256 があるのに、出力のファイルが無い")
        if bytes.fromhex(sha["str"]).decode() != hashlib.sha256(content).hexdigest():
            raise Failure(f"{parent}: {name}_sha256 が出力のファイルの SHA-256 と違う")
        if keep:
            size = pair_value(result, name + "_bytes")
            tail = pair_value(result, name + "_tail")
            if size != {"int": str(len(content))} or tail != {"bytes": content[-keep:].hex()}:
                raise Failure(f"{parent}: {name}_bytes・{name}_tail が出力のファイルと違う")
        if legacy_monitor:
            fixed = fixed_names(ctx, content, Relax())
            if fixed != content:
                relax.add("固定名")
                sha["str"] = hexs(hashlib.sha256(fixed).hexdigest())
                if keep:
                    pair_value(result, name + "_bytes")["int"] = str(len(fixed))
                    pair_value(result, name + "_tail")["bytes"] = fixed[-keep:].hex()


def check_keyerror_file(obs, parent, monitor, relax):
    """Linux の方式の SIGCHLD の無視: 監督の stderr のファイルの KeyError の pid は品質コマンドの pid(06:58)。"""
    if not LINUX or obs.get("stderr") is None:
        return
    found = re.findall(rb"KeyError: (\d+)\n", obs["stderr"])
    pid = obs.get("state_pid")
    if len(found) != 1 or pid is None or int(found[0]) != pid:
        raise Failure(f"{parent}×{monitor}: KeyError の pid が品質コマンドの pid と違う: {found} {pid}")
    obs["stderr"] = re.sub(rb"KeyError: \d+\n", b"KeyError: @PID@\n", obs["stderr"])
    relax.add("pid")


def case_rc(ctx, case, relax):
    """1 つの組(親 × 監督)を流し、旧版の親 × 旧版の監督(偽の監督のケースは旧版の親 × 偽の監督)と比べる。
    基準は同じ内容の組で 1 回だけ流し、同じフォルダ(同じパス)で全部の組を流す。"""
    parent, monitor = case.params["parent"], case.params["monitor"]
    key = case.params["base_key"]
    case_dir = os.path.join(ctx.temp, "c", key)
    if not os.path.isdir(case_dir):
        os.makedirs(case_dir)
    try:
        base = ctx.bases.get(key)
        if base is None:
            base_monitor = monitor if monitor in ("fake", "missing") else "legacy"
            try:
                base = rc_side(ctx, case, "legacy", base_monitor, case_dir, ctx.token, relax)
            finally:
                clear(case_dir)
            ctx.bases[key] = base
            check_expect(case, base)
        if parent == "legacy" and monitor in ("legacy", "fake", "missing"):
            return
        try:
            observed = rc_side(ctx, case, parent, monitor, case_dir, ctx.token, relax)
        finally:
            clear(case_dir)
        compare_values(base, observed, f"旧版の親×{'偽の' if monitor == 'fake' else '旧版の'}監督(基準)と "
                                       f"{parent}×{monitor} ")
        check = case.params.get("check")
        if check is not None:
            problem = check(observed)
            if problem:
                raise Failure(problem)
    finally:
        if case.params.get("last"):
            remove_tree(case_dir)


def rc_expect(kind, value=None, message=None):
    """rc の基準の固定期待: ok(returncode)か error(型と文の頭)。"""
    def check(obs):
        data = obs["result"]
        if kind == "ok":
            if "result" not in data:
                return f"辞書でなく誤り {data['error']}"
            got = pair_value(data["result"], "returncode")
            if value is not None and got != {"int": str(value)}:
                return f"returncode {got}(期待 {value})"
            return None
        if "error" not in data:
            return f"誤りでなく辞書(returncode {pair_value(data['result'], 'returncode')})"
        if data["error"]["type"] != kind:
            return f"誤りの型 {data['error']['type']}(期待 {kind})"
        if message is not None and not data["error"]["message"].startswith(message):
            return f"誤りの文 {data['error']['message']!r}(期待の頭 {message!r})"
        return None
    return check


# ---- 動き(run_command の途中の操作) ----

def send_parent(number):
    def action(ctx, proc, case_dir, *rest):
        wait_started(case_dir, proc)
        os.kill(proc.pid, number)
    return action


def send_monitor(number):
    def action(ctx, proc, case_dir, *rest):
        wait_started(case_dir, proc)
        os.kill(read_state(case_dir)["ppid"], number)
    return action


def burst_parent(ctx, proc, case_dir, *rest):
    wait_started(case_dir, proc)
    burst(proc.pid)


def stop_monitor(ctx, proc, case_dir, *rest):
    wait_started(case_dir, proc)
    os.kill(read_state(case_dir)["ppid"], signal.SIGSTOP)


def fionread(fd):
    data = fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0")
    return struct.unpack("i", data)[0]


def mac_paused(pid):
    """R4U と同じ待ちの推定(動くスレッドが 0 で、Unix の呼出しの回数が 0.3 秒変わらない)。"""
    from check_r4u import MacTask  # noqa: PLC0415(Mac だけで使う)
    task = MacTask()
    first = task.read(pid)
    if first is None or first[0] < 1 or first[1] != 0:
        return False
    for _ in range(30):
        time.sleep(0.01)
        now = task.read(pid)
        if now is None or now[1] != 0 or now[2] != first[2]:
            return False
    return True


# 通知の後の anchor の KILL の確かめの上限(秒。設計の 60 秒)と、KILL から SIGCONT までに残す余裕。自己検査だけが縮める。
ANCHOR_CHECK_LIMIT = [60.0]
ANCHOR_CONT_MARGIN = 5.0


def anchor_after_notice(ctx, proc, case_dir, parent, monitor):
    """通知の後の anchor の KILL: 親を止める → 品質コマンドを終わらせる → 回収と通知を確かめる → KILL → 再開。

    期限は、親を止めた時点からの 1 つだけ(回収・通知・pause の全部の確かめを覆い、SIGCONT までの余裕を残す)。
    Mac で pause に入ったことを期限内に確かめられなければ、閉じた一覧の未実施にする(Linux の確かめの失敗は不合格)。
    どの経路でも、抜ける前に anchor の群を止める(次のケースへ残さない)。止めた時間は、成功の経路だけで確かめる。"""
    wait_started(case_dir, proc)
    state = read_state(case_dir)
    anchor, command = state["ppid"], state["pid"]
    stopped = time.monotonic()
    limit = ANCHOR_CHECK_LIMIT[0]
    checks_end = stopped + limit - ANCHOR_CONT_MARGIN

    def left():
        return max(checks_end - time.monotonic(), 0.0)
    os.kill(proc.pid, signal.SIGSTOP)
    done = False
    try:
        os.kill(command, signal.SIGTERM)

        def reaped():
            try:
                os.kill(command, 0)
                return False
            except ProcessLookupError:
                return True
        wait_for(reaped, "品質コマンドの回収", timeout=left())
        argv = [bytes.fromhex(item).decode("utf-8", "surrogateescape") for item in state["parent_argv"]]
        anchor_fd = argv[argv.index("--anchor-fd") + 1]
        if LINUX:
            target = os.readlink(f"/proc/{anchor}/fd/{anchor_fd}")
            found = None
            for name in os.listdir(f"/proc/{proc.pid}/fd"):
                try:
                    if os.readlink(f"/proc/{proc.pid}/fd/{name}") == target:
                        found = name
                except OSError:
                    continue
            if found is None or not target.startswith("pipe:["):
                raise Failure("親の読み口(anchor と同じ pipe)が見つからない")
            line = len(json.dumps({"nonce": "0" * 64, "returncode": -15})) + 1
            fd = os.open(f"/proc/{proc.pid}/fd/{found}", os.O_RDONLY | os.O_NONBLOCK)
            try:
                wait_for(lambda: fionread(fd) >= line, "anchor の通知", timeout=left())
            finally:
                os.close(fd)
        else:
            while not mac_paused(anchor):
                if time.monotonic() >= checks_end:
                    raise Skip(SKIP_ANCHOR_PAUSE)
                time.sleep(0.05)
        os.kill(anchor, signal.SIGKILL)
        # 親が止まっているので、anchor は回収されずにゾンビになる(読み口の書き手が閉じたことの確かめ)。
        wait_for(lambda: zombie(anchor), "anchor の終わり", timeout=max(stopped + limit - time.monotonic(), 0.0))
        done = True
    finally:
        if not done:
            # 確かめの途中で抜けるときも、anchor の群(anchor が先頭)を止める。
            try:
                os.killpg(anchor, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        os.kill(proc.pid, signal.SIGCONT)
        held = time.monotonic() - stopped
        print(f"  記録({parent}×{monitor}): 親を止めた時間 {held:.2f} 秒", flush=True)
    if held > limit:
        raise Failure(f"親を止めた時間 {held:.1f} 秒が timeout(600 秒)の 1/10 を超えた")


def anchor_before_notice(ctx, proc, case_dir, *rest):
    """通知の前の anchor の KILL: 品質コマンドが始めた印を書いて待っている間に、anchor を KILL する。"""
    wait_started(case_dir, proc)
    os.kill(read_state(case_dir)["ppid"], signal.SIGKILL)


# ---- json.loads の入れ子の上限(旧版の親の実際の呼出しの深さで測る) ----

def measure_depth(ctx):
    """旧版の親 × 偽の監督で、記録(linux)と通知(posix)の入れ子の深さを二分探索し、包まれない RecursionError に
    なる最初の深さ(上限)を測る。"""
    methods = ("linux", "posix") if LINUX else ("posix",)
    found = {}
    work = os.path.join(ctx.temp, "depth")
    os.makedirs(work)
    try:
        for method in methods:
            low, high = 900, 1000  # low は通る深さ、high は RecursionError の深さ

            def recursion(depth):
                body = b"[" * depth + b"]" * depth + (b"\n" if method == "posix" else b"")
                case = Case("depth", "rc", "rc", {"argv": ["true"], "method": method, "parent": "legacy",
                                                  "monitor": "fake",
                                                  "fake": {"data": body, "record": "file", "end": "pause"
                                                           if method == "posix" else "exit"}})
                obs = rc_side(ctx, case, "legacy", "fake", work, ctx.token + "-depth", Relax())
                clear(work)
                error = obs["result"].get("error")
                return error is not None and error["type"] == "RecursionError"
            if recursion(low) or not recursion(high):
                raise Failure(f"入れ子の上限が {low}〜{high} の間に無い({method})")
            while high - low > 1:
                middle = (low + high) // 2
                if recursion(middle):
                    high = middle
                else:
                    low = middle
            found[method] = high
    finally:
        remove_tree(work)
    return found


# ---- ケースの一覧 ----

LS_REC = "@D@/rec"


def ls_args(*, result=LS_REC, nonce="n", timeout="5", grace="0.2", command=None):
    args = []
    if result is not None:
        args += ["--result", result]
    if nonce is not None:
        args += ["--nonce", nonce]
    if timeout is not None:
        args += ["--timeout", timeout]
    if grace is not None:
        args += ["--grace", grace]
    return args + ["--", *(command if command is not None else ["true"])]


def cp_linux(*, timeout="5", grace="0.2", command=None, result="@D@/rec"):
    return ["--linux", "--result", result, "--timeout", timeout, "--grace", grace, "--nonce", "n", "--",
            *(command if command is not None else ["true"])]


LS_RECORD_LEN = len(b'{"clean": true, "nonce": "n", "returncode": 0, "signal": 0, "timed_out": false}\n')


def exit_code(value):
    return value


def cli_cases():
    """監督を直接起動するケース(ls-・cp-・py-・loop-)。"""
    cases = []

    def add(case_id, kind, args, group=None, linux=False, **params):
        params.update(kind=kind, args=args)
        skip = SKIP_MAC_LS if linux and MAC else None
        params["linux_only"] = linux
        cases.append(Case(case_id, group or case_id.split("-")[0], "cli", params, skip))

    on_mac_97 = {"linux": 0, "macos": 97}
    # loop-supervisor の CLI(setup の前に結果が決まる。Mac でも同じに流す)。
    add("ls-noargs", "ls", [], expect=2)
    add("ls-check", "ls", ["--check"], expect=on_mac_97)
    add("ls-chec", "ls", ["--chec"], expect=2)
    add("ls-check-extra", "ls", ["--check", "--grace", "0"], expect=2)
    for name in ("result", "nonce", "timeout", "grace"):
        add(f"ls-missing-{name}", "ls", ls_args(**{name: None}), expect=2)
    add("ls-abbrev", "ls", ["--res", LS_REC, "--non", "n", "--ti", "0", "--gr", "0", "--", "true"], record="rec",
        expect=97)
    add("ls-abbrev-short", "ls", ["--r", LS_REC, "--n", "n", "--t", "0", "--g", "0", "--", "true"], record="rec",
        expect=97)
    add("ls-abbrev-help", "ls", ["--h"], expect=0)
    add("ls-opt-equals", "ls", [f"--result={LS_REC}", "--nonce=n", "--timeout=0", "--grace=0", "--", "true"],
        record="rec", expect=97)
    add("ls-repeat", "ls", ["--result", "@D@/a", "--result", LS_REC, "--nonce", "x", "--nonce", "n", "--timeout", "1",
                            "--timeout", "0", "--grace", "0", "--", "true"], record="rec", expect=97)
    add("ls-dash-value", "ls", ls_args(nonce="-x"), expect=2)
    add("ls-negative-number", "ls", ls_args(timeout="-1"), record="rec", expect=97)
    add("ls-negative-fraction", "ls", ls_args(timeout="-.5"), record="rec", expect=97)
    add("ls-bad-number", "ls", ls_args(timeout="x"), expect=2)
    add("ls-bad-number-grace", "ls", ls_args(grace="0x1"), expect=2)
    for name, value in (("nan", "nan"), ("inf", "inf"), ("1e309", "1e309"), ("unicode-digits", "１"),
                        ("arabic-digits", "١"), ("spaces-plus", " +5_0 ")):
        add(f"ls-{name}", "ls", ls_args(timeout=value, grace="-1"), record="rec", expect=97)
    add("ls-help", "ls", ["-h"], expect=0)
    add("ls-help-long", "ls", ["--help"], expect=0)
    add("ls-help-columns-40", "ls", ["-h"], env={"COLUMNS": "40"}, expect=0)
    add("ls-help-columns-200", "ls", ["-h"], env={"COLUMNS": "200"}, expect=0)
    add("ls-error-columns-40", "ls", ["--chec"], env={"COLUMNS": "40"}, expect=2)
    add("ls-empty-command", "ls", ls_args(command=[]), record="rec", expect=97)
    add("ls-empty-command-dashdash", "ls", ls_args(command=[])[:-1], record="rec", expect=97)
    add("ls-empty-command-two-dashes", "ls", [*ls_args(command=[]), "--"], record="rec", expect=97)
    add("ls-timeout-zero", "ls", ls_args(timeout="0"), record="rec", expect=97)
    add("ls-grace-negative", "ls", ls_args(grace="-0.1"), record="rec", expect=97)
    add("ls-record-exists", "ls", ls_args(timeout="1"), record="rec", prepare=[("file", "rec", b"old")], expect=97)
    add("ls-record-symlink", "ls", ls_args(timeout="1"), record="rec",
        prepare=[("symlink", "rec", "@D@/target")], expect=97)
    add("ls-record-dir-missing", "ls", ls_args(result="@D@/none/rec"), expect=97)
    add("ls-record-non-utf8", "ls", ls_args(result="@D@/rec\udcff", command=["true", "@T@"]),
        expect={"linux": 0, "macos": 97})
    if MAC:
        add("ls-mac-setup", "ls", ls_args(command=sh("exit 3")), record="rec", expect=97)
    # loop-supervisor の監督(Linux。Mac は MAC_SKIPPED_LS の固定の一覧で流さない)。
    add("ls-basic-exit3", "ls", ls_args(command=sh("exit 3")), record="rec", linux=True, loop=True,
        expect=check_basic_exit3)
    add("ls-exit0", "ls", ls_args(command=["true", "@T@"]), record="rec", linux=True, loop=True, expect=0)
    add("ls-exit300", "ls", ls_args(command=sh("exit 300")), record="rec", linux=True, loop=True, expect=0)
    add("ls-segv", "ls", ls_args(command=sh("kill -SEGV $$")), record="rec", linux=True, loop=True, expect=0)
    add("ls-term-self", "ls", ls_args(command=sh("kill -TERM $$")), record="rec", linux=True, loop=True, expect=0)
    add("ls-remainder-dashdash", "ls", [*ls_args(command=[]), "--", "true", "@T@"], record="rec", linux=True,
        loop=True, expect=97)
    add("ls-remainder-option", "ls", ["--result", LS_REC, "--nonce", "n", "--timeout", "5", "--grace", "0.2",
                                      "@P@", "state", "@D@/state.json", "--mark", "@T@", "--exit", "0"],
        record="rec", linux=True, loop=True, expect=0)
    add("ls-command-missing", "ls", ls_args(command=["nonexistent-r4q-command", "@T@"]), record="rec", linux=True,
        expect=97)
    add("ls-enoexec", "ls", ls_args(command=["@D@/noexec", "@T@"]), record="rec", linux=True,
        prepare=[("file", "noexec", b"echo not a program\n", 0o755)], expect=97)
    tool = b"#!/bin/sh\nexit 4\n"
    add("ls-eacces-then-candidate", "ls", ls_args(command=["tool", "@T@"]), record="rec", linux=True, loop=True,
        env={"PATH": "@D@/a:@D@/b"}, prepare=[("file", "a/tool", tool, 0o644), ("file", "b/tool", tool, 0o755)],
        expect=0)
    add("ls-path-empty", "ls", ls_args(command=["tool", "@T@"]), record="rec", linux=True, loop=True,
        env={"PATH": ""}, prepare=[("file", "tool", tool, 0o755)], expect=0)
    add("ls-path-unset", "ls", ls_args(command=["true", "@T@"]), record="rec", linux=True, loop=True,
        env={"PATH": None}, expect=0)
    add("ls-path-last-enotdir", "ls", ls_args(command=["nonexistent-r4q", "@T@"]), record="rec", linux=True,
        env={"PATH": "@D@/x:@D@/plain"}, prepare=[("dir", "x"), ("file", "plain", b"x")], expect=97)
    add("ls-timeout-term", "ls", ls_args(timeout="0.3", grace="1", command=probe_cmd("--sleep", "30")),
        record="rec", linux=True, loop=True, expect=0)
    add("ls-timeout-ignore-term", "ls", ls_args(timeout="0.3", grace="0.5", command=probe_cmd(
        "--ignore-term", "--term-log", "@D@/terms", "--sleep", "30")), record="rec", linux=True, loop=True,
        term_rule="many", expect=0)
    add("ls-timeout-grace0", "ls", ls_args(timeout="0.3", grace="0", command=probe_cmd(
        "--ignore-term", "--term-log", "@D@/terms", "--sleep", "30")), record="rec", linux=True, loop=True,
        term_rule="exact", expect=0)
    add("ls-timeout-nan", "ls", ls_args(timeout="nan", command=probe_cmd("--exit", "5")), record="rec",
        linux=True, loop=True, expect=0)
    add("ls-timeout-inf", "ls", ls_args(timeout="inf", command=probe_cmd("--exit", "6")), record="rec",
        linux=True, loop=True, expect=0)
    add("ls-grace-nan", "ls", ls_args(timeout="0.3", grace="nan", command=probe_cmd(
        "--ignore-term", "--term-log", "@D@/terms", "--sleep", "30")), record="rec", linux=True, loop=True,
        term_rule="exact", expect=0)
    for shape in ("double", "setsid", "hold"):
        add(f"ls-grandchild-{shape}", "ls", ls_args(command=probe_cmd("--grandchild", shape, "--grandchild-pid",
                                                                      "@D@/grand", "--exit", "0")),
            record="rec", linux=True, loop=True, expect=0)
    for name, number in (("term", signal.SIGTERM), ("int", signal.SIGINT), ("hup", signal.SIGHUP)):
        add(f"ls-signal-{name}", "ls", ls_args(command=probe_cmd("--started", "@D@/started", "--sleep", "30")),
            record="rec", linux=True, loop=True, action=cli_signal(number), expect=0)
    add("ls-signal-burst", "ls", ls_args(command=probe_cmd("--started", "@D@/started", "--sleep", "30")),
        record="rec", linux=True, loop=True, action=cli_burst, expect=lambda obs: None
        if obs["record"] and b'"signal": 15' in obs["record"] else f"記録が最後の TERM でない: {obs['record']!r}")
    add("ls-kill-monitor", "ls", ls_args(command=probe_cmd("--started", "@D@/started", "--sleep", "30")),
        record="rec", linux=True, action=cli_signal(signal.SIGKILL), leftover_ok=True, expect=-9)
    add("ls-pdeathsig", "ls", ls_args(command=probe_cmd("--started", "@D@/started", "--sleep", "30")), linux=True,
        runner="pdeathsig", expect=lambda obs: None if obs["record"] and b'"signal": 15' in obs["record"]
        else f"記録が TERM の信号でない: {obs['record']!r}")
    for name, size in (("zero", 0), ("1", 1), ("half", LS_RECORD_LEN // 2), ("partial", LS_RECORD_LEN - 1)):
        add(f"ls-record-fsize-{name}", "ls", ls_args(command=["true", "@T@"]), record="rec", linux=True,
            fsize=size, stdout="pipe", stderr="pipe", expect=97)
    add("ls-nonce-non-ascii", "ls", ls_args(nonce="é日\udcff\"\\\x01", command=["true", "@T@"]), record="rec",
        linux=True, expect=0)
    add("ls-sigchld-ignored", "ls", ls_args(timeout="0.3", command=probe_cmd("--exit", "0")), record="rec",
        linux=True, sigchld_ignored=True, post=keyerror_pid, expect=1)
    add("ls-mask", "ls", ls_args(command=probe_cmd("--exit", "0")), record="rec", linux=True,
        block=[signal.SIGUSR1], sigquit_ignored=True, sigpipe_ignored=True, sigxfsz_ignored=True, expect=0)
    add("ls-env-order", "ls", ls_args(command=probe_cmd("--exit", "0")), record="rec", linux=True,
        env={"Z": "1", "PATH": None, "A": "2", "LANG": None, "B": "3"}, expect=0)
    add("ls-coerce-c", "ls", ls_args(command=probe_cmd("--exit", "0")), record="rec", linux=True,
        env={"LANG": "C"}, expect=0)
    # check-process の監督の CLI(上の CLI のケースと同じ形を、check-process の parser に当てる)。
    add("cp-noargs", "cp", [], expect=2)
    add("cp-missing-nonce", "cp", ["--linux", "--result", "@D@/rec", "--timeout", "1", "--grace", "0", "--", "true"],
        expect=2)
    add("cp-abbrev", "cp", ["--l", "--r", "@D@/rec", "--t", "0", "--g", "0", "--n", "n", "--", "true"], record="rec",
        expect=97)
    add("cp-abbrev-long", "cp", ["--li", "--res", "@D@/rec", "--ti", "0", "--gr", "0", "--no", "n", "--", "true"],
        record="rec", expect=97)
    add("cp-abbrev-anchor", "cp", ["--anc", "99", "--n", "n", "--", "true"], expect=97)
    add("cp-abbrev-help", "cp", ["--h"], expect=0)
    add("cp-opt-equals", "cp", ["--linux", "--result=@D@/rec", "--timeout=0", "--grace=0", "--nonce=n", "--", "true"],
        record="rec", expect=97)
    add("cp-repeat", "cp", ["--linux", "--result", "@D@/a", "--result", "@D@/rec", "--timeout", "1", "--timeout", "0",
                            "--grace", "0", "--nonce", "x", "--nonce", "n", "--", "true"], record="rec", expect=97)
    add("cp-dash-value", "cp", ["--nonce", "-x", "--", "true"], expect=2)
    add("cp-negative-number", "cp", cp_linux(timeout="-1"), record="rec", expect=97)
    add("cp-negative-fraction", "cp", cp_linux(timeout="-.5"), record="rec", expect=97)
    add("cp-anchor-negative", "cp", ["--anchor-fd", "-1", "--nonce", "n", "--", "true"], expect=97)
    add("cp-bad-number", "cp", cp_linux(timeout="x"), expect=2)
    add("cp-anchor-not-int", "cp", ["--anchor-fd", "x", "--nonce", "n", "--", "true"], expect=2)
    add("cp-anchor-hex", "cp", ["--anchor-fd", "0x1", "--nonce", "n", "--", "true"], expect=2)
    for name, value in (("nan", "nan"), ("inf", "inf"), ("1e309", "1e309"), ("negative", "-1")):
        add(f"cp-linux-{name}", "cp", cp_linux(timeout=value, grace="0"), record="rec", expect=97)
    add("cp-linux-unicode-digits", "cp", cp_linux(timeout="１", grace="0"), record="rec", expect=on_mac_97)
    add("cp-linux-grace-nan", "cp", cp_linux(grace="nan"), record="rec", expect=97)
    add("cp-help", "cp", ["-h"], expect=0)
    add("cp-help-long", "cp", ["--help"], expect=0)
    add("cp-help-columns-40", "cp", ["-h"], env={"COLUMNS": "40"}, expect=0)
    add("cp-help-columns-200", "cp", ["-h"], env={"COLUMNS": "200"}, expect=0)
    add("cp-empty-command", "cp", ["--nonce", "n", "--"], expect=97)
    add("cp-empty-command-linux", "cp", ["--linux", "--nonce", "n", "--"], expect=97)
    add("cp-empty-command-no-dashes", "cp", ["--linux", "--nonce", "n"], expect=97)
    add("cp-timeout-zero", "cp", cp_linux(timeout="0"), record="rec", expect=97)
    add("cp-grace-negative", "cp", cp_linux(grace="-0.1"), record="rec", expect=97)
    add("cp-anchor-none", "cp", ["--nonce", "n", "--", "true"], expect=1)
    add("cp-anchor-overflow", "cp", ["--anchor-fd", "2147483648", "--nonce", "n", "--", "true"], expect=1)
    add("cp-anchor-equals-dashes", "cp", ["--anchor-fd=--", "--nonce", "n", "--", "true"], expect=1)
    add("cp-anchor-bad-fd", "cp", ["--anchor-fd", "99", "--nonce", "n", "--", "true"], expect=97)
    add("cp-linux-no-result", "cp", ["--linux", "--timeout", "1", "--grace", "0", "--nonce", "n", "--", "true"],
        expect=1)
    add("cp-linux-no-deadline", "cp", ["--linux", "--result", "@D@/rec", "--nonce", "n", "--", "true"],
        record="rec", expect=97)
    add("cp-linux-record-exists", "cp", cp_linux(), record="rec", prepare=[("file", "rec", b"x")], expect=97)
    add("cp-linux-record-symlink", "cp", cp_linux(), record="rec", prepare=[("symlink", "rec", "@D@/target")],
        expect=97)
    # check-process の監督(実際に品質コマンドを起動する。Linux は記録、Mac は記録を作った後の 97)。
    add("cp-linux-exit3", "cp", cp_linux(command=sh("exit 3")), record="rec", expect=on_mac_97)
    add("cp-linux-missing", "cp", cp_linux(command=["nonexistent-r4q", "@T@"]), record="rec", expect=on_mac_97)
    add("cp-linux-probe", "cp", cp_linux(command=probe_cmd("--exit", "0")), record="rec", expect=on_mac_97)
    add("cp-linux-sigchld-ignored", "cp", cp_linux(timeout="0.3", grace="0", command=probe_cmd("--exit", "0")),
        record="rec", sigchld_ignored=True, post=keyerror_pid, expect={"linux": 1, "macos": 97})
    anchor_ok = {"linux": -9, "macos": -9}
    add("cp-anchor-exit3", "cp", ["--anchor-fd", "@FD@", "--nonce", "n", "--", "sh", "-c", "exit 3", "@T@"],
        anchor_pipe=True, expect=anchor_ok)
    add("cp-anchor-missing", "cp", ["--anchor-fd", "@FD@", "--nonce", "n", "--", "nonexistent-r4q", "@T@"],
        anchor_pipe=True, expect=anchor_ok)
    add("cp-anchor-probe", "cp", ["--anchor-fd", "@FD@", "--nonce", "é\udcff", "--", *probe_cmd("--exit", "0")],
        anchor_pipe=True, expect=anchor_ok)
    add("cp-anchor-remainder-dashdash", "cp", ["--anchor-fd", "@FD@", "--nonce", "n", "--", "--", "true", "@T@"],
        anchor_pipe=True, expect=anchor_ok)
    # Python の起動(py-)。
    add("py-intmaxstrdigits-valid", "cp", ["--anchor-fd", "1" * 700, "--nonce", "n", "--", "true"],
        env={"PYTHONINTMAXSTRDIGITS": "640"}, expect=2)
    add("py-intmaxstrdigits-unlimited", "cp", ["--anchor-fd", "1" * 5000, "--nonce", "n", "--", "true"],
        env={"PYTHONINTMAXSTRDIGITS": "0"}, expect=1)
    add("py-intmaxstrdigits-invalid", "ls", ["-h"], env={"PYTHONINTMAXSTRDIGITS": "1"}, expect=1)
    add("py-unbuffered", "ls", ["-h"], env={"PYTHONUNBUFFERED": "1"}, expect=0)
    for value in ("0", "1", "2", "x"):
        add(f"py-optimize-{value}", "ls", ["-h"], env={"PYTHONOPTIMIZE": value}, expect=0)
    add("py-optimize-2-cp", "cp", ["-h"], env={"PYTHONOPTIMIZE": "2"}, expect=0)
    add("py-hashseed-invalid", "ls", ["-h"], env={"PYTHONHASHSEED": "x"}, expect=1)
    add("py-stdin-dir", "ls", ["-h"], stdin="dir", expect=1)
    add("py-stdout-dir", "ls", ["-h"], stdout="dir", expect=1)
    add("py-stderr-dir", "ls", [], stderr="dir", expect=1)
    add("py-stderr-devfull", "ls", ls_args(timeout="0"), stderr="devfull", expect=120)
    add("py-closed-fd0-2", "ls", ["-h"], closed_std=True, runner="closed")
    add("py-stderr-closed-pipe", "ls", ls_args(timeout="0"), stderr="closed-pipe", expect=120)
    add("py-stderr-fsize", "ls", ls_args(timeout="0"), fsize=20, fsize_cut=True, expect=120)
    add("py-traceback-fsize", "cp", ["--nonce", "n", "--", "true"], fsize=150, fsize_cut=True, expect=120)
    add("py-sigint-blocked-write", "ls", ["-h"], env={"PYTHONUNBUFFERED": "1"}, stdout="full-pipe",
        action=drain_after_sigint, post=sigint_stable, expect=-2)
    add("py-sigint-ignored", "ls", ["-h"], env={"PYTHONUNBUFFERED": "1"}, stdout="full-pipe",
        sigint_ignored=True, action=drain_after_sigint, expect=0)
    for kind in ("ls", "cp"):
        unbuffered_out = "devfull" if LINUX else "closed-pipe"
        add(f"py-argparse-help-unbuffered-{kind}", kind, ["--help"], env={"PYTHONUNBUFFERED": "1"},
            stdout=unbuffered_out, expect=1)
        add(f"py-argparse-help-buffered-{kind}", kind, ["--help"], stdout=unbuffered_out, expect=120)
    add("py-argparse-usage-fsize", "ls", ["--chec"], fsize=40, fsize_cut=True, expect=120)
    add("py-coerce-c", "cp", ["--anchor-fd", "@FD@", "--nonce", "n", "--", *probe_cmd("--exit", "0")],
        anchor_pipe=True, env={"LANG": "C"}, expect=anchor_ok)
    add("py-coerce-warn", "cp", ["--anchor-fd", "@FD@", "--nonce", "n", "--", *probe_cmd("--exit", "0")],
        anchor_pipe=True, env={"LANG": "C", "PYTHONCOERCECLOCALE": "warn"}, expect=anchor_ok)
    add("py-coerce-0", "cp", ["--anchor-fd", "@FD@", "--nonce", "n", "--", *probe_cmd("--exit", "0")],
        anchor_pipe=True, env={"LANG": "C", "PYTHONCOERCECLOCALE": "0"}, expect=anchor_ok)
    add("py-coerce-lc-all", "cp", ["--anchor-fd", "@FD@", "--nonce", "n", "--", *probe_cmd("--exit", "0")],
        anchor_pipe=True, env={"LANG": "C", "LC_ALL": "C"}, expect=anchor_ok)
    # loop.sh の照合(loop-)。旧版と Rust の記録の両方に supervisor-result を当てる(Mac も、記録の無い経路で比べる)。
    for name, command, extra in (("exit3", sh("exit 3"), {}),
                                 ("timeout", probe_cmd("--sleep", "30"), {"timeout": "0.3"}),
                                 ("signal", probe_cmd("--started", "@D@/started", "--sleep", "30"), {})):
        # Mac は setup で 97 になり品質コマンドが始まらないので、信号を送る動きは Linux だけ。
        params = {"action": cli_signal(signal.SIGHUP)} if name == "signal" and LINUX else {}
        add(f"loop-{name}", "ls", ls_args(command=command, **extra), record="rec", loop=True,
            expect=on_mac_97 if name != "signal" else {"linux": 0, "macos": 97}, **params)
    return cases


def check_basic_exit3(obs):
    """完了条件 4: 終了 0・stdout と stderr が空・記録の 1 行・supervisor-result が 3 0。"""
    want = b'{"clean": true, "nonce": "n", "returncode": 3, "signal": 0, "timed_out": false}\n'
    if obs["exit"] != 0 or obs["stdout"] or obs["stderr"] or obs["record"] != want:
        return f"ls-basic-exit3 の結果が完了条件 4 と違う: {obs}"
    if obs.get("loop") != [0, b"3 0\n", b""]:
        return f"supervisor-result が 3 0 を出さない: {obs.get('loop')}"
    return None


def rc_cases():
    """run_command のケース(rc-・probe-)。4 つの組 × 方式(Linux は linux と posix、Mac は posix と linux の拒否)。"""
    cases = []
    methods = ["linux", "posix"] if LINUX else ["posix"]
    combos = [("legacy", "legacy"), ("legacy", "rust"), ("rust", "legacy"), ("rust", "rust")]

    def add(content, method, argv, group="rc", combos_used=None, **params):
        used = combos_used or combos
        for index, (parent, monitor) in enumerate(used):
            case_id = f"{group}-{method}-{content}-{parent}-{monitor}"
            data = dict(params, argv=argv, method=method, parent=parent, monitor=monitor,
                        base_key=f"{group}-{method}-{content}", last=index == len(used) - 1)
            cases.append(Case(case_id, group, "rc", data))

    ok = rc_expect
    for method in methods:
        linux = method == "linux"
        add("exit3", method, sh("printf out; printf err >&2; exit 3"), expect=ok("ok", 3), nonce_seen=linux,
            check=check_rc_exit3 if linux else None)
        add("exit0", method, ["true", "@T@"], expect=ok("ok", 0))
        add("signal", method, sh("kill -TERM $$"), expect=ok("ok", -15))
        add("missing", method, ["nonexistent-r4q-command", "@T@"], expect=ok("ok", 127))
        add("cwd-missing", method, ["true", "@T@"], cwd="@D@/nonexistent", expect=ok("ok", 127))
        add("cwd", method, probe_cmd(), cwd="@D@/work", prepare=[("dir", "work")], expect=ok("ok", 0))
        add("keep", method, probe_cmd("--write-stdout", "10", "--write-stderr", "7"), keep=5, expect=ok("ok", 0))
        add("keep-large", method, probe_cmd("--write-stdout", "10"), keep=100, expect=ok("ok", 0))
        add("output-1mib", method, probe_cmd("--write-stdout", str(1024 * 1024)), keep=3, expect=ok("ok", 0))
        add("output-1mib-plus1", method, probe_cmd("--write-stdout", str(1024 * 1024 + 1)), keep=3,
            expect=ok("ok", 0))
        if linux:
            add("timeout-term", method, probe_cmd("--sleep", "30"), timeout={"float": "0.3"},
                grace={"float": "0.5"}, expect=ok("ok", -15))
        else:
            add("timeout-term", method, probe_cmd("--term-log", "@D@/terms", "--sleep", "30"),
                timeout={"float": "0.3"}, grace={"float": "0.5"}, term_rule="exact", grace_floor=0.5,
                expect=ok("ok", -9))
        add("timeout-ignore-term", method, probe_cmd("--ignore-term", "--term-log", "@D@/terms", "--sleep", "30"),
            timeout={"float": "0.3"}, grace={"float": "0.5"}, term_rule="many" if linux else "exact",
            grace_floor=None if linux else 0.5, expect=ok("ok", -9))
        # grace=0: Linux の supervise は TERM を送らず KILL だけ(受けた TERM は 0 回で決まり、そのまま比べる)。
        # POSIX の stop_group は TERM の直後に KILL を送るので、品質コマンドが TERM の処理を走らせるかは競り合いで
        # 決まる(緩める欄の閉じた一覧の「POSIX の方式の grace=0 の TERM の数」。0 か 1 を確かめて記録する)。
        add("timeout-grace0", method, probe_cmd("--ignore-term", "--term-log", "@D@/terms", "--sleep", "30"),
            timeout={"float": "0.3"}, grace={"int": "0"}, term_rule="exact" if linux else "zero-or-one",
            expect=ok("ok", -9))
        for name, number in (("term", signal.SIGTERM), ("int", signal.SIGINT), ("hup", signal.SIGHUP)):
            add(f"parent-{name}", method, probe_cmd("--started", "@D@/started", "--sleep", "30"),
                action=send_parent(number), expect=ok("Interrupted", message=f"終了通知 {int(number)} を受けた"))
        add("parent-burst", method, probe_cmd("--started", "@D@/started", "--sleep", "30"), action=burst_parent,
            expect=ok("Interrupted", message="終了通知 1 を受けた"))
        if linux:
            add("monitor-kill", method, probe_cmd("--started", "@D@/started", "--sleep", "30"),
                action=send_monitor(signal.SIGKILL), leftover_ok=True,
                expect=ok("RecoveryError", message="監督が正常に終了しなかった"))
        else:
            add("anchor-killed-before-notice", method, probe_cmd("--started", "@D@/started", "--sleep", "30"),
                action=anchor_before_notice, expect=ok("RecoveryError", message="anchor が終了通知の前に失われた"))
        add("env-dup", method, probe_cmd(), env=[["Z", "1"], ["PATH", CHILD_PATH], ["A", "2"], ["Z", "3"]],
            expect=ok("ok", 0))
        add("env-none", method, probe_cmd(), env=None, expect=ok("ok", 0))
        add("path-search", method, ["tool", "@T@"], env=[["PATH", "@D@/a:@D@/b"]],
            prepare=[("file", "a/tool", b"#!/bin/sh\nexit 4\n", 0o644),
                     ("file", "b/tool", b"#!/bin/sh\nexit 4\n", 0o755)], expect=ok("ok", 4))
        add("badenv-key", method, ["true", "@T@"], env=[["A=B", "1"]],
            expect=ok("RecoveryError", message="品質プロセスの回収を確認できない: illegal environment variable name"))
        add("env-nul", method, ["true", "@T@"], env=[["A", "x\0y"]],
            expect=ok("RecoveryError", message="品質プロセスの回収を確認できない: embedded null byte"))
        add("argv-nul", method, ["tr\0ue", "@T@"],
            expect=ok("RecoveryError", message="品質プロセスの回収を確認できない: embedded null byte"))
        add("timeout-int", method, probe_cmd(), timeout={"int": "5"}, grace={"int": "1"}, expect=ok("ok", 0))
        add("timeout-float-repr", method, probe_cmd(), timeout={"float": "123.456"}, grace={"float": "0.1"},
            expect=ok("ok", 0))
        add("timeout-float-exp", method, probe_cmd(), timeout={"float": "1e+300"}, grace={"float": "1e-05"},
            expect=ok("ok", 0))
        add("timeout-nan", method, ["true", "@T@"], timeout={"float": "nan"},
            expect=ok("RecoveryError", message="監督の期限が不正"))
        add("timeout-inf", method, ["true", "@T@"], timeout={"float": "inf"},
            expect=ok("RecoveryError", message="監督の期限が不正"))
        add("timeout-zero", method, ["true", "@T@"], timeout={"int": "0"},
            expect=ok("RecoveryError", message="監督の期限が不正"))
        add("grace-negative", method, ["true", "@T@"], grace={"float": "-0.5"},
            expect=ok("RecoveryError", message="監督の期限が不正"))
        add("grace-nan", method, ["true", "@T@"], grace={"float": "nan"},
            expect=ok("RecoveryError", message="監督の期限が不正"))
        add("timeout-huge", method, ["true", "@T@"], timeout={"int": "1" + "0" * 400}, expect=ok("OverflowError"))
        add("output-symlink", method, sh("printf linked"), prepare=[("dir", "control"),
                                                                     ("symlink", "control/stdout", "@D@/target")],
            expect=ok("ok", 0))
        add("output-dir", method, ["true", "@T@"], prepare=[("dir", "control/stdout")],
            expect=ok("RecoveryError", message="品質プロセスの回収を確認できない: [Errno 21]"))
        add("directory-missing", method, ["true", "@T@"], directory="none/control", no_directory=True,
            expect=ok("RecoveryError", message="品質プロセスの回収を確認できない: [Errno 2]"))
        add("directory-non-utf8", method, probe_cmd(), directory="control\udcff", non_utf8=True,
            expect=ok("ok", 0))
        add("sigchld-ignored", method, probe_cmd("--exit", "3"), sigchld_ignored=True, keyerror=linux,
            timeout={"float": "0.3"}, grace={"int": "0"},
            expect=ok("RecoveryError", message="品質プロセスの回収を確認できない: Expecting value")
            if linux else ok("ok", 0))
        for shape in ("double", "setsid", "hold"):
            escapes = shape == "setsid" and not linux
            add(f"grandchild-{shape}", method, probe_cmd("--grandchild", shape, "--grandchild-pid", "@D@/grand",
                                                         "--exit", "0"), leftover_ok=escapes, expect=ok("ok", 0))
        add("monitor-missing", method, ["true", "@T@"], combos_used=[("legacy", "missing"), ("rust", "missing")],
            expect=ok("ok", 127))
        # 品質コマンドが見る状態(probe-)。
        add("basic", method, probe_cmd(), group="probe", expect=ok("ok", 0))
        add("env-none", method, probe_cmd(), env=None, group="probe", expect=ok("ok", 0))
        add("env-order", method, probe_cmd(), env=[["Z", "1"], ["PATH", CHILD_PATH], ["A", "2"], ["LANG", "C.UTF-8"]],
            group="probe", expect=ok("ok", 0))
        for name, env in (("lc-c", [["PATH", CHILD_PATH], ["LANG", "C"]]),
                          ("lc-posix", [["PATH", CHILD_PATH], ["LC_CTYPE", "POSIX"]]),
                          ("lc-all", [["PATH", CHILD_PATH], ["LANG", "C"], ["LC_ALL", "C"]]),
                          ("coerce-0", [["PATH", CHILD_PATH], ["LANG", "C"], ["PYTHONCOERCECLOCALE", "0"]]),
                          ("coerce-warn", [["PATH", CHILD_PATH], ["LANG", "C"], ["PYTHONCOERCECLOCALE", "warn"]])):
            add(name, method, probe_cmd(), env=env, group="probe", keep=400, expect=ok("ok", 0))
        add("mask", method, probe_cmd(), group="probe", block=[signal.SIGUSR1], sigquit_ignored=True,
            sigpipe_ignored=True, sigxfsz_ignored=True, expect=ok("ok", 0))
    if LINUX:
        add("supervisor-stopped", "linux", probe_cmd("--started", "@D@/started", "--sleep", "30"),
            timeout={"float": "0.3"}, grace={"float": "0.2"}, action=stop_monitor, run_timeout=60, leftover_ok=True,
            expect=ok("RecoveryError", message="監督の終了を期限内に確認できない"))
    add("anchor-killed-after-notice", "posix", probe_cmd("--started", "@D@/started", "--sleep", "600"),
        timeout={"int": "600"}, action=anchor_after_notice, run_timeout=200,
        expect=ok("RecoveryError", message="anchor が回収前に失われた") if LINUX
        else ok("RecoveryError", message="品質プロセスの回収を確認できない: [Errno 1] Operation not permitted"))
    add("outside-writer", "posix", probe_cmd("--write-stdout", "10", "--grandchild", "writer", "--grandchild-pid",
                                             "@D@/grand", "--exit", "0"), keep=4, outside_writer=10,
        leftover_ok=True, expect=ok("ok", 0))
    if MAC:
        add("reject", "linux", ["true", "@T@"], expect=ok("RecoveryError", message="Linux の監督に必要な pidfd がない"))
    rc_fake_cases(add)
    return cases


def rc_fake_cases(add):
    """偽の監督(2 つの親 × 偽の監督)。記録と通知の誤り・入れ子の上限・後始末の誤り。"""
    fake = [("legacy", "fake"), ("rust", "fake")]
    ok = rc_expect
    record = {"clean": True, "returncode": 3, "timed_out": False, "signal": 0, "nonce": "{nonce}"}

    def raw(value):
        return (json.dumps(value) + "\n").encode()

    def padded(size):
        body = raw(record)[:-2]  # 最後の } と改行の前
        filler = size - len(body) - 2 - 64 + len("{nonce}")  # {nonce} は 64 字に置き換わる
        return body + b" " * filler + b"}\n"

    mismatch = ok("RecoveryError", message="回収結果の照合に失敗")
    linux_fakes = [
        ("record-ok", {"data": raw(record)}, ok("ok", 3)),
        ("record-missing", {"record": "none"}, ok("RecoveryError", message="品質プロセスの回収を確認できない: [Errno 2]")),
        ("record-empty", {"data": b""}, ok("RecoveryError", message="品質プロセスの回収を確認できない: Expecting value")),
        ("record-bad-json", {"data": b"{"}, ok("RecoveryError", message="品質プロセスの回収を確認できない: Expecting")),
        ("record-signal-5", {"data": raw({**record, "signal": 5})}, mismatch),
        ("record-signal-bool", {"data": raw({**record, "signal": True})}, mismatch),
        ("record-signal-term", {"data": raw({**record, "signal": 15})}, ok("Interrupted", message="終了通知 15 を受けた")),
        ("record-returncode-bool", {"data": raw({**record, "returncode": True})}, mismatch),
        ("record-timed-out-int", {"data": raw({**record, "timed_out": 0})}, mismatch),
        ("record-clean-false", {"data": raw({**record, "clean": False})}, mismatch),
        ("record-nonce-other", {"data": raw({**record, "nonce": "other"})}, mismatch),
        ("record-not-dict", {"data": b"[1]\n"}, mismatch),
        ("record-4096", {"data": padded(4096)}, ok("ok", 3)),
        ("record-4097", {"data": padded(4097)}, ok("RecoveryError", message="回収結果の型または大きさが不正")),
        ("record-extra-keys", {"data": raw({**record, "extra": 1})}, ok("ok", 3)),
        ("record-duplicate-keys", {"data": b'{"returncode": "x", "clean": true, "returncode": 4, '
                                           b'"timed_out": false, "signal": 0, "nonce": "{nonce}"}\n'}, ok("ok", 4)),
        ("record-fifo", {"record": "fifo"}, ok("RecoveryError", message="回収結果の型または大きさが不正")),
        ("record-symlink", {"record": "symlink", "data": raw(record)},
         ok("RecoveryError", message="品質プロセスの回収を確認できない: [Errno ")),
        ("record-exit-1", {"data": raw(record), "code": 1}, ok("RecoveryError", message="監督が正常に終了しなかった")),
        ("record-nested-below", {"nested": "below"}, mismatch),
        ("record-nested-limit", {"nested": "limit"}, ok("RecursionError")),
    ]
    for name, spec, expect in linux_fakes:
        if not LINUX:
            break
        spec = dict({"data": raw(record), "record": "file", "end": "exit", "code": 0}, **spec)
        add(f"fake-{name}", "linux", ["true", "@T@"], combos_used=fake, fake=spec, expect=expect)
    notice = {"nonce": "{nonce}", "returncode": 3}
    bad_notice = ok("RecoveryError", message="anchor の通知が不正")
    posix_fakes = [
        ("notice-ok", {"data": raw(notice)}, ok("ok", 3)),
        ("notice-bad-json", {"data": b"{\n"}, ok("RecoveryError", message="Expecting property name")),
        ("notice-nonce", {"data": raw({**notice, "nonce": "other"})}, bad_notice),
        ("notice-returncode-bool", {"data": raw({**notice, "returncode": True})}, bad_notice),
        ("notice-not-dict", {"data": b"[1]\n"}, bad_notice),
        ("notice-too-big", {"data": b"x" * 4097}, ok("RecoveryError", message="anchor の通知が大きすぎる")),
        ("notice-extra-after-newline", {"data": raw(notice) + b"x"},
         ok("RecoveryError", message="Extra data")),
        ("notice-silent-exit", {"end": "silent-exit"},
         ok("RecoveryError", message="anchor が終了通知の前に失われた")),
        ("notice-nested-below", {"nested": "below"}, bad_notice),
        ("notice-nested-limit", {"nested": "limit"}, ok("RecursionError")),
    ]
    for name, spec, expect in posix_fakes:
        spec = dict({"end": "pause"}, **spec)
        add(f"fake-{name}", "posix", ["true", "@T@"], combos_used=fake, fake=spec, expect=expect)
    if LINUX:
        add("fake-notice-error-stop-group-fails", "posix", ["true", "@T@"], combos_used=fake,
            fake={"data": b"{\n", "end": "child-pause", "child_pid": "@D@/held.pid"}, hold=True,
            expect=ok("RecoveryError", message="プロセスグループの不在を確認できない"))
    else:
        add("fake-notice-error-stop-group-fails", "posix", ["true", "@T@"], combos_used=fake,
            fake={"data": b"{\n", "end": "exit"},
            expect=ok("RecoveryError", message="品質プロセスの回収を確認できない: [Errno 1] Operation not permitted"))


def check_rc_exit3(obs):
    """完了条件 5: returncode 3・timed_out false・linux-subreaper・owned-descendants・stdout_sha256 は out。"""
    result = obs["result"].get("result")
    want = {"returncode": {"int": "3"}, "timed_out": {"bool": False},
            "recovery_method": {"str": hexs("linux-subreaper")}, "recovery_scope": {"str": hexs("owned-descendants")},
            "stdout_sha256": {"str": hexs(hashlib.sha256(b"out").hexdigest())}, "nonce": {"str": "@NONCE@"}}
    for name, value in want.items():
        if result is None or pair_value(result, name) != value:
            return f"rc-linux-exit3 の {name} が完了条件 5 と違う: {result}"
    return None


# 必須のケースの一覧(ID の並び)の件数と SHA-256。ケースを足すか外したら、ここも直す(自己検査が、Linux と Mac の
# 両方の一覧を作って確かめる)。欠落・重複・足したまま直し忘れたケースを見つける。
CASE_LIST = {
    "linux": (832, "dd351d8132c85bc72a8bd1b4ebef47dd256c61343d8dbab1454baa28fefe9902"),
    "macos": (589, "b5f353c373888daf017a420401b100e66c675a57c3c62f657ae6d6b60c65851d"),
}


def case_list_digest(cases):
    ids = [case.id for case in cases]
    return len(ids), hashlib.sha256("\n".join(ids).encode()).hexdigest()


def check_case_list(cases, name=None):
    name = name or platform_name()
    got = case_list_digest(cases)
    if got != CASE_LIST[name]:
        raise Failure(f"必須のケースの一覧が、固定の一覧({name}: {CASE_LIST[name][0]} 件)と違う: {got[0]} 件・"
                      f"SHA-256 {got[1]}")


def all_cases(injected_ids=()):
    cases = [Case("pre-strerror", "pre", "strerror", {})]
    cases += [Case(case_id, "inj", "inj", {}) for case_id in injected_ids]
    cases += cli_cases()
    cases += rc_cases()
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise Failure("ケースの ID が重なる: " + ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
    linux_only = sorted(case.id for case in cases if case.params.get("linux_only") and case.id.startswith("ls-"))
    if linux_only != sorted(MAC_SKIPPED_LS):
        raise Failure("Linux だけで流す ls- のケースが、Mac で流さない固定の一覧と違う: "
                      f"{sorted(set(linux_only) ^ set(MAC_SKIPPED_LS))}")
    return cases


# ---- 流す ----

def run_case(ctx, case):
    if case.mac_skip is not None:
        raise Skip(case.mac_skip)
    if case.params.get("non_utf8") and MAC:
        probe = os.path.join(ctx.temp, "non-utf8-\udcff")
        try:
            os.mkdir(probe)
        except OSError as exc:
            if exc.errno in (errno.EILSEQ, errno.EINVAL):
                raise Skip(SKIP_NON_UTF8) from exc
            raise
        os.rmdir(probe)
    relax = Relax()
    if case.kind == "strerror":
        case_strerror(ctx, case, relax)
    elif case.kind == "inj":
        case_injected(ctx, case, relax)
    elif case.kind == "rc":
        prepare_nested(ctx, case)
        case_rc(ctx, case, relax)
    elif case.params.get("runner") == "pdeathsig":
        case_pdeathsig(ctx, case, relax)
    elif case.params.get("runner") == "closed":
        case_closed_std(ctx, case, relax)
    else:
        case_cli(ctx, case, relax)
    return relax


def prepare_nested(ctx, case):
    """入れ子の上限のケースの深さを、測った上限から決める(below は 1 つ手前、limit は上限ちょうど)。"""
    spec = case.params.get("fake")
    if not spec or spec.get("nested") not in ("below", "limit"):
        return
    method = case.params["method"]
    limit = ctx.depth[method]
    depth = limit - 1 if spec["nested"] == "below" else limit
    body = b"[" * depth + b"]" * depth + (b"\n" if method == "posix" else b"")
    case.params["fake"] = {key: value for key, value in spec.items() if key != "nested"}
    case.params["fake"]["data"] = body
    case.params["depth"] = depth


def classify_skip(reason, mac=None):
    mac = MAC if mac is None else mac
    if not mac or reason not in CLOSED_LIST:
        return Outcome("不合格", f"閉じた一覧に無い未実施: {reason}")
    return Outcome("未実施", reason)


def final_status(outcomes, ids, mac=None):
    """全ケースが合格(Mac は閉じた一覧の未実施を含む)で、1 件以上が合格のときだけ 0。"""
    mac = MAC if mac is None else mac
    if not ids or set(outcomes) != set(ids) or len(ids) != len(set(ids)):
        return 1
    if not any(outcomes[i].status == "合格" for i in ids):
        return 1
    for case_id in ids:
        outcome = outcomes[case_id]
        if outcome.status == "合格":
            continue
        if outcome.status == "未実施" and mac and outcome.message in CLOSED_LIST:
            continue
        return 1
    return 0


def compare_all(ctx, cases):
    outcomes = {}
    for case in cases:
        started_at = time.monotonic()
        try:
            relax = run_case(ctx, case)
            outcome = Outcome("合格", relax=relax.counts)
        except Skip as exc:
            outcome = classify_skip(str(exc))
        except Failure as exc:
            outcome = Outcome("不合格", str(exc))
        except Exception as exc:  # noqa: BLE001 — 1 件の想定外の例外で、残りのケースの結果を失わない
            outcome = Outcome("不合格", f"比較器の想定外の例外: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        outcome = after_case(ctx, outcome)
        outcome.seconds = time.monotonic() - started_at
        outcomes[case.id] = outcome
        report_case(case, outcome)
    return outcomes


def after_case(ctx, outcome):
    """ケースごとの後始末: 印の付いたプロセス(失敗の経路で残った別のセッションの anchor や孫を含む)を止める。
    見つけたら、そのケースを不合格にして件数を出す(次のケースの不合格に誤って数えない)。"""
    try:
        left = sweep(ctx.token)
    except Failure as exc:
        return Outcome("不合格", f"ケースの後の片付け: {exc}(元の結果: {outcome.status} {outcome.message})",
                       relax=outcome.relax)
    if left:
        return Outcome("不合格", f"ケースの後に、印の付いたプロセスが {left} 件残った(止めた。元の結果: "
                       f"{outcome.status} {outcome.message})", relax=outcome.relax)
    return outcome


def report_case(case, outcome):
    spent = f"{outcome.seconds:.2f} 秒"
    if outcome.status == "合格":
        note = f"(緩めた欄: {Relax(outcome.relax).text()})" if outcome.relax else "(緩めた欄: なし)"
        print(f"合格 {case.id} {spent}{note}", flush=True)
    elif outcome.status == "未実施":
        print(f"未実施 {case.id} {spent}: {outcome.message}", flush=True)
    else:
        print(f"不合格 {case.id} {spent}: {outcome.message}", flush=True)


def summary(outcomes, cases):
    groups = {}
    relaxed = {}
    for case in cases:
        bucket = groups.setdefault(case.group, {"全": 0, "合格": 0, "未実施": 0, "不合格": 0})
        outcome = outcomes[case.id]
        bucket["全"] += 1
        bucket[outcome.status] += 1
        for name, count in outcome.relax.items():
            relaxed[name] = relaxed.get(name, 0) + count
    lines = ["組ごとの件数:"]
    for name, label in GROUPS.items():
        if name in groups:
            b = groups[name]
            lines.append(f"  {label}: 全 {b['全']} 件・合格 {b['合格']}・未実施 {b['未実施']}・不合格 {b['不合格']}")
    lines.append("緩めた欄の件数(全ケースの合計): " + ("・".join(f"{n} {c}" for n, c in relaxed.items()) or "なし"))
    return lines


def print_plan(cases):
    """最初に、流す必須のケースの総数と並びを出す(打ち切られたときに全体の時間を見積もるため)。"""
    print(f"R4Q の比較のケース: 全 {len(cases)} 件(この並びで流す)", flush=True)
    counts = {name: sum(1 for case in cases if case.group == name) for name in GROUPS}
    print("  組ごと: " + "・".join(f"{GROUPS[name]} {count} 件" for name, count in counts.items() if count), flush=True)
    for index, case in enumerate(cases, 1):
        print(f"  {index}/{len(cases)} {case.id}", flush=True)


def open_context(rust, python, report, locale_name, base, hold_report=None):
    """比べる前提の揃った状態(一時フォルダ・r4q_probe・差し替えの固定期待の流し直し)。close_context で片付ける。"""
    temp = tempfile.mkdtemp(prefix="r4q-check-", dir=base)
    try:
        os.makedirs(os.path.join(temp, "c"))
        ctx = Ctx(rust=rust, rust_head=run_head(rust), python=python, stdlib=report["stdlib"], temp=temp, probe="",
                  locale=locale_name, token=f"r4qmark-{os.getpid()}-{int(time.time())}", hold_report=hold_report)
        ctx.probe = build_probe(temp)
        try:
            document, problem = regenerate_injected(ctx)
            ctx.injected = {item["id"]: item for item in document["cases"]}
            ctx.injected_problem = problem
        except Failure as exc:
            ctx.injected_problem = str(exc)
    except BaseException:
        remove_tree(temp)
        raise
    return ctx


def injected_ids(ctx):
    if ctx.injected is not None:
        return list(ctx.injected)
    return [item["id"] for item in json.loads(INJECTED.read_text(encoding="utf-8"))["cases"]]


def close_context(ctx):
    remove_tree(ctx.temp)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rust", required=True)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--run-timeout", type=float, help="1 回の起動の上限(秒。自己検査だけが縮める)")
    parser.add_argument("--hold-report", help="自己検査だけが使う: 補助の保持の間に pid を書くファイル")
    args = parser.parse_args(argv)
    started_at = time.monotonic()
    if args.run_timeout:
        RUN_TIMEOUT[0] = args.run_timeout
    temp = None
    try:
        if os.geteuid() == 0:
            raise Failure("root でない利用者で流す")
        if not (LINUX or MAC):
            raise Failure("Linux か Mac で流す")
        check_legacy_scripts()
        base = temp_root()
        python = os.environ.get("R4Q_PYTHON", "")
        report = check_python(python)
        print(f"旧版の Python: {python}(CPython {report['version']}・標準ライブラリ {len(build.R4Q_STDLIB_SHA256)} "
              f"件が本家と同じ・pidfd {report['pidfd']}・自分への pidfd と signal 0 {report['pidfd_call']})",
              flush=True)
        locale_name = LOCALE_OF[platform_name()]
        print(f"地域設定(env を渡さないケース): LANG={locale_name}(確かめた値 {check_locale(python, locale_name)})",
              flush=True)
        limits = {name: resource.getrlimit(getattr(resource, name))
                  for name in ("RLIMIT_NOFILE", "RLIMIT_FSIZE", "RLIMIT_NPROC", "RLIMIT_STACK")}
        print("受け継いだ資源の上限(比較器は設定しない): " + ", ".join(f"{k}={v}" for k, v in limits.items()),
              flush=True)
        rust = os.path.realpath(args.rust)
        if not os.access(rust, os.X_OK):
            raise Failure(f"Rust の検査用の実行物を実行できない: {rust}")
        ctx = open_context(rust, python, report, locale_name, base, args.hold_report)
        temp = ctx.temp
        print(f"一時フォルダ: {temp}", flush=True)
    except (Failure, OSError) as exc:
        print(f"R4Q の比較: 不合格(前提): {exc}", flush=True)
        return 1
    status = 1
    try:
        cases = all_cases(injected_ids(ctx))
        check_case_list(cases)
        if args.list:
            for case in cases:
                print(case.id)
            return 0
        if args.only is not None:
            wanted = set(args.only)
            unknown = wanted - {case.id for case in cases}
            if unknown or not wanted:
                raise Failure(f"知らない ID か、空の --only: {sorted(unknown)}")
            cases = [case for case in cases if case.id in wanted]
            print(f"--only: 必須のケースのうち {len(cases)} 件だけを流す(全体の合格にはならない)", flush=True)
        print_plan(cases)
        if any(case.params.get("fake", {}).get("nested") for case in cases):
            ctx.depth = measure_depth(ctx)
            print(f"json.loads の入れ子の上限(旧版の親で測った深さ): {ctx.depth}", flush=True)
        outcomes = compare_all(ctx, cases)
        check_legacy_scripts()
        for line in summary(outcomes, cases):
            print(line, flush=True)
        counts = {s: sum(1 for o in outcomes.values() if o.status == s) for s in ("合格", "未実施", "不合格")}
        reasons = {}
        for outcome in outcomes.values():
            if outcome.status == "未実施":
                reasons[outcome.message] = reasons.get(outcome.message, 0) + 1
        if reasons:
            print("未実施の理由(成功に数えない): " + "・".join(f"{r} {n} 件" for r, n in reasons.items()), flush=True)
        if MAC:
            print(f"Mac で流さない ls- のケース(固定の一覧): {len(MAC_SKIPPED_LS)} 件", flush=True)
        print(f"子の実行: {ctx.executions} 回", flush=True)
        status = final_status(outcomes, [case.id for case in cases])
        if status:
            print("不合格の ID: " + ", ".join(i for i, o in outcomes.items() if o.status == "不合格"), flush=True)
        elapsed = time.monotonic() - started_at
        print(f"R4Q の比較: 合格 {counts['合格']} 件 / 全 {len(cases)} 件・未実施 {counts['未実施']} 件・"
              f"不合格 {counts['不合格']} 件 / {sys.platform} / 旧 Python {python} / {elapsed:.1f} 秒", flush=True)
    except Failure as exc:
        print(f"R4Q の比較: 不合格: {exc}", flush=True)
        status = 1
    finally:
        try:
            left = sweep(ctx.token)
        except Failure as exc:
            print(f"R4Q の比較: 不合格: 終わりの片付け: {exc}", flush=True)
            status = 1
        else:
            if left:
                print(f"R4Q の比較: 不合格: 終わりに、印の付いたプロセスが {left} 件残った(止めた)", flush=True)
                status = 1
        try:
            remove_tree(temp)
        except OSError as exc:
            print(f"R4Q の比較: 不合格: 一時フォルダを消せない: {temp}: {exc}", flush=True)
            status = 1
        if os.path.lexists(temp):
            print(f"R4Q の比較: 不合格: 一時フォルダが残った: {temp}", flush=True)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
