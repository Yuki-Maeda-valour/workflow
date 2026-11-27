"""R4Q(#494)の比較器 check_r4q.py の自己検査。誤った結果・未実行・対象外の置き換えを合格にしないことを確かめる。

使い方(cargo test --locked -p dev-workflow-cli --test r4q -- --r4q-compare が比較器と並べて起動する):
    R4Q_PYTHON=<本家 3.10.11> TMPDIR=<一時の場所> python3 -B native/ci/test_r4q.py

- わざと誤った Rust の代わり(旧版のコピーを 1 か所だけ変えたもの)を --rust へ渡し、比較器が不合格にすることを
  確かめる(設計の Task 3.2 の表)。誤りの無いコピー(合格の対照)は、同じケースで合格になることも確かめる。
- ケースの欠落・重複・0 件・skip・時間切れ・両側の同じ誤り・基準でない Python・外すべき変数の残り・残った
  一時フォルダとプロセス、緩め方の誤り(一覧に無い欄・nonce と pid の確かめを省く)を不合格にすることを確かめる。
- Linux では、比較器を補助(r4q_probe の hold)の保持の途中で KILL し、補助と保持した子が 30 秒以内に消えることを
  確かめる(自己検査は自分の subreaper を 0 に戻し、waitpid(-1) を使わない)。
- 一時フォルダは TMPDIR(無ければ RUNNER_TEMP)の下にだけ作り、作って控えた正確なパスだけを消す。
- 0 件・skip・失敗のどれかがあれば、終了値 1 で終わる。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_r4q as checker  # noqa: E402

LINUX = checker.LINUX
MAC = checker.MAC
PYTHON = os.environ.get("R4Q_PYTHON", "")
STATE = {"temp": None, "fakes": {}, "runs": 0}


def temp():
    return STATE["temp"]


def base_for(name):
    """比較器の子に渡す、専用の一時の場所(TMPDIR)。"""
    path = os.path.join(temp(), "base-" + name + f"-{STATE['runs']}")
    STATE["runs"] += 1
    os.makedirs(path)
    return path


def edit(text, edits):
    for old, new, *count in edits:
        want = count[0] if count else 1
        if text.count(old) != want:
            raise AssertionError(f"直す所が {want} か所でない({text.count(old)} か所): {old[:60]!r}")
        text = text.replace(old, new)
    return text


def make_fake(name, ls=(), cp=(), extra="", rust_head=None):
    """旧版のコピーを動かす偽の Rust(検査用の実行物と同じ引数を受ける)。ls・cp は旧版のコピーへの書き換え。"""
    if name in STATE["fakes"]:
        return STATE["fakes"][name]
    folder = os.path.join(temp(), "fakes", name)
    os.makedirs(folder)
    Path(folder, "loop-supervisor.py").write_text(edit(checker.LS_SCRIPT.read_text(encoding="utf-8"), ls),
                                                  encoding="utf-8")
    Path(folder, "check-process.py").write_text(edit(checker.CP_SCRIPT.read_text(encoding="utf-8"), cp),
                                                encoding="utf-8")
    rust = os.path.join(folder, "rust")
    Path(rust).write_text(f"#!{PYTHON} -B\n" + textwrap.dedent(f"""\
        import importlib.util, json, os, runpy, sys
        sys.dont_write_bytecode = True
        HERE = {folder!r}
        # EXTRA
        flag = sys.argv[1] if len(sys.argv) > 1 else ""
        if flag in ("--r4q-loop-supervisor", "--r4q-check-process"):
            script = os.path.join(HERE, "loop-supervisor.py" if flag == "--r4q-loop-supervisor" else "check-process.py")
            sys.argv = [script] + sys.argv[2:]
            runpy.run_path(script, run_name="__main__")
        elif flag == "--r4q-run-command":
            spec = importlib.util.spec_from_file_location("r4q_legacy_parent", {str(checker.PARENT)!r})
            parent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(parent)
            parent.main([sys.argv[2], os.path.join(HERE, "rust")], script=os.path.join(HERE, "check-process.py"),
                        legacy_head=[sys.executable, "-B", {str(checker.CP_SCRIPT)!r}], rust_head={rust_head!r})
        elif flag == "--r4q-strerror":
            print(json.dumps({{str(n): os.strerror(n) for n in range(0, int(sys.argv[2]) + 1)}}))
        else:
            sys.exit(2)
        """).replace("# EXTRA\n", extra + "\n"), encoding="utf-8")
    os.chmod(rust, stat.S_IRWXU)
    STATE["fakes"][name] = rust
    return rust


def run_checker(rust, ids, *options, timeout=900):
    """比較器を子として流す。戻り値は (終了値, 出力, ID ごとの結果)。"""
    env = {"PATH": os.environ.get("PATH", checker.CHILD_PATH), "R4Q_PYTHON": PYTHON, "TMPDIR": base_for("run"),
           "LANG": os.environ.get("LANG", "C.UTF-8")}
    done = subprocess.run([sys.executable, "-B", str(HERE / "check_r4q.py"), "--rust", rust, *options, "--only", *ids],
                          env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout)
    text = done.stdout.decode("utf-8", "replace")
    results = {}
    for line in text.splitlines():
        match = re.match(r"(合格|不合格|未実施) (\S+) ", line)
        if match:
            results[match.group(2)] = (match.group(1), line)
    left = os.listdir(env["TMPDIR"])
    if left:
        raise AssertionError(f"比較器が一時の場所に物を残した: {left}")
    os.rmdir(env["TMPDIR"])
    return done.returncode, text, results


# ---- 誤った Rust の代わり(設計の Task 3.2 の表) ----

LS_KEYS = [("    raw = (json.dumps(result, sort_keys=True) + '\\n').encode()",
            "    raw = (json.dumps(result) + '\\n').encode()")]
LS_TERM_ONCE = [("STOP = 0\n", "STOP = 0\nTERM_SENT = []\n"),
                ("        signal_owned(signal.SIGTERM if time.monotonic() < term_end else signal.SIGKILL)",
                 "        term = time.monotonic() < term_end\n"
                 "        signal_owned(signal.SIGKILL if not term else (0 if TERM_SENT else signal.SIGTERM))\n"
                 "        if term:\n            TERM_SENT.append(1)")]
LS_GRACE0_TERM = [("    term_end = time.monotonic() + grace", "    term_end = time.monotonic() + max(grace, 0.1)")]
LS_EMPTY_MASK = [("    process = subprocess.Popen(argv, start_new_session=True, close_fds=True)",
                  "    signal.pthread_sigmask(signal.SIG_SETMASK, [])\n"
                  "    process = subprocess.Popen(argv, start_new_session=True, close_fds=True)")]
LS_NO_RESTORE = [("    process = subprocess.Popen(argv, start_new_session=True, close_fds=True)",
                  "    process = subprocess.Popen(argv, start_new_session=True, close_fds=True, restore_signals=False)")]
LS_ENOEXEC_SH = [("    process = subprocess.Popen(argv, start_new_session=True, close_fds=True)",
                  "    try:\n        process = subprocess.Popen(argv, start_new_session=True, close_fds=True)\n"
                  "    except OSError as exc:\n        if exc.errno != errno.ENOEXEC:\n            raise\n"
                  "        process = subprocess.Popen(['sh', *argv], start_new_session=True, close_fds=True)")]
LS_NO_SUBREAPER = [("    if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER", "    if False:  # PR_SET_CHILD_SUBREAPER")]
LS_NO_PDEATHSIG = [("    if libc.prctl(1, signal.SIGTERM, 0, 0, 0):  # PR_SET_PDEATHSIG", "    if False:  # PR_SET_PDEATHSIG")]
LS_MESSAGE = [("print(f'子プロセスの回収を確認できない: {exc}', file=sys.stderr)",
               "print(f'子プロセスの回収を確認できない:{exc}', file=sys.stderr)")]
LS_KEYERROR_PID = [("    process.returncode = statuses[process.pid]", "    process.returncode = statuses[process.pid + 1]")]
LS_NOARGS_CHECK = [("    if sys.argv[1:] == ['--check']:", "    if sys.argv[1:] in (['--check'], []):")]
CP_SEPARATORS = [("    raw = (json.dumps(result) + '\\n').encode()",
                  "    raw = (json.dumps(result, separators=(',', ':')) + '\\n').encode()")]
CP_WRONG_NONCE = [("    result['nonce'] = args.nonce", "    result['nonce'] = args.nonce + 'x'")]
CP_CLOSE_EARLY = [("""        try:
            # この呼出しまで poll/wait/communicate をしない。異常終了も未回収で保持する。
            stop_group(process, grace)
        finally:
            # 読み口はプロセスグループの停止を確かめるまで閉じない。先に閉じると、TERM で終わったコマンドの通知を
            # anchor が書けずに終わり、プロセスグループが回収前の終了済みだけになる(macOS はそのプロセスグループへの KILL を EPERM で返す)。
            os.close(read_fd)""", """        os.close(read_fd)
        stop_group(process, grace)""")]
CP_ANCHOR_POPEN = "        process = subprocess.Popen(argv, close_fds=True)"
CP_EMPTY_MASK = [(CP_ANCHOR_POPEN, "        signal.pthread_sigmask(signal.SIG_SETMASK, [])\n" + CP_ANCHOR_POPEN)]
CP_NO_RESTORE = [(CP_ANCHOR_POPEN, "        process = subprocess.Popen(argv, close_fds=True, restore_signals=False)")]
CP_ENV_ORDER = [(CP_ANCHOR_POPEN,
                 "        process = subprocess.Popen(argv, close_fds=True, env=dict(sorted(os.environ.items())))")]
CP_NO_LC_CTYPE = [(CP_ANCHOR_POPEN, "        os.environ.pop('LC_CTYPE', None)\n" + CP_ANCHOR_POPEN)]
CP_SHORT_NONCE = [("    nonce = secrets.token_hex(32)", "    nonce = secrets.token_hex(31)", 2)]
CP_SAME_NONCE = [("    nonce = secrets.token_hex(32)", "    nonce = '0' * 64", 2)]
CP_LEFTOVER = [(CP_ANCHOR_POPEN, "        subprocess.Popen(['sh', '-c', 'sleep 30; :', 'x', *argv[-1:]], start_new_session=True)\n"
                + CP_ANCHOR_POPEN)]
CP_RECORD_MODE = [("    fd = os.open(args.result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)",
                   "    fd = os.open(args.result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)")]
CP_TERM_TWICE = [("""    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    time.sleep(grace)""", """    try:
        os.killpg(process.pid, signal.SIGTERM)
        time.sleep(0.05)
        os.killpg(process.pid, signal.SIGTERM)
        time.sleep(0.05)
    except ProcessLookupError:
        pass
    time.sleep(grace)""")]
CP_PIDFD_FALLBACK = [("""        raise RecoveryError("Linux の監督に必要な pidfd がない")""", """        method = 'posix'""")]

# 合格の対照に使うケース(呼出履歴が出ないもの。偽の Rust は旧版のコピーのパスを呼出履歴に出すため)。
POSITIVE_LINUX = ["ls-basic-exit3", "ls-timeout-ignore-term", "ls-timeout-grace0", "ls-mask", "ls-enoexec",
                  "ls-grandchild-double", "ls-pdeathsig", "ls-timeout-zero", "ls-noargs", "cp-linux-exit3",
                  "rc-linux-exit3-legacy-rust", "rc-posix-timeout-term-rust-rust", "probe-posix-mask-rust-rust",
                  "probe-posix-env-order-rust-rust", "probe-posix-lc-c-rust-rust", "probe-posix-basic-rust-rust",
                  "probe-posix-env-none-rust-rust", "cp-anchor-exit3"]
POSITIVE_MAC = ["ls-timeout-zero", "ls-noargs", "rc-posix-timeout-term-rust-rust", "probe-posix-mask-rust-rust",
                "probe-posix-env-order-rust-rust", "probe-posix-lc-c-rust-rust", "probe-posix-basic-rust-rust",
                "probe-posix-env-none-rust-rust", "cp-anchor-exit3", "rc-linux-reject-rust-rust"]


class WrongRustTests(unittest.TestCase):
    """わざと誤った Rust の代わりを、決めたケースが不合格にする(合格の対照も同じケースで確かめる)。"""

    def assert_found(self, fake, ids, word=None):
        code, text, results = run_checker(fake, ids)
        failed = [i for i in ids if results.get(i, ("",))[0] == "不合格"]
        self.assertTrue(failed, f"誤りを見つけなかった: {ids}\n{text[-3000:]}")
        self.assertEqual(1, code, text[-2000:])
        if word is not None:
            self.assertTrue(any(word in results[i][1] for i in failed), f"不合格の理由に {word!r} が無い:\n"
                            + "\n".join(results[i][1][:600] for i in failed))

    def test_positive_control_passes_every_case_used_below(self):
        ids = POSITIVE_LINUX if LINUX else POSITIVE_MAC
        code, text, results = run_checker(make_fake("same"), ids)
        self.assertEqual(0, code, text[-4000:])
        for case_id in ids:
            status = results.get(case_id, ("無い",))[0]
            # Mac で流さない ls- のケースは、閉じた一覧の未実施(成功に数えない)。
            self.assertIn(status, ("合格", "未実施") if MAC else ("合格",), case_id)

    def linux_only(self, ids):
        """Mac では setup の後で結果が決まる ls- のケースを流さない(閉じた一覧)。その一覧に入っていることを確かめる。"""
        if MAC:
            for case_id in ids:
                self.assertIn(case_id, checker.MAC_SKIPPED_LS)
            return True
        return False

    def test_record_key_order(self):
        if not self.linux_only(["ls-basic-exit3"]):
            self.assert_found(make_fake("keys", ls=LS_KEYS), ["ls-basic-exit3"], "record")

    def test_record_separators_of_the_linux_worker(self):
        if LINUX:
            self.assert_found(make_fake("separators", cp=CP_SEPARATORS), ["cp-linux-exit3"], "record")
        else:
            self.assertNotIn("rc-linux-exit3-rust-rust", [case.id for case in checker.rc_cases()])

    def test_term_is_not_resent(self):
        if not self.linux_only(["ls-timeout-ignore-term"]):
            self.assert_found(make_fake("term-once", ls=LS_TERM_ONCE), ["ls-timeout-ignore-term"], "2 回より少ない")

    def test_posix_grace_zero_term_count_outside_the_legacy_range(self):
        # POSIX の方式の grace=0 は 0 か 1 だけを認める(06:58)。TERM を 2 回送る誤りは不合格にする。
        self.assert_found(make_fake("posix-term-twice", cp=CP_TERM_TWICE), ["rc-posix-timeout-grace0-rust-rust"],
                          "0 か 1 でない")

    def test_term_is_sent_with_grace_zero(self):
        if not self.linux_only(["ls-timeout-grace0"]):
            self.assert_found(make_fake("grace0", ls=LS_GRACE0_TERM), ["ls-timeout-grace0"], "terms")

    def test_pidfd_missing_falls_back_to_posix(self):
        if MAC:
            self.assert_found(make_fake("fallback", cp=CP_PIDFD_FALLBACK), ["rc-linux-reject-rust-rust"])
            return
        # Linux の実プロセスでは pidfd が必ずあるので現れない(設計の表)。Mac の一覧に拒否のケースがあること、
        # 差し替えの否定の検査(r4q_units.rs の FallbackToPosix)があることを確かめる。
        with mock.patch.object(checker, "LINUX", False), mock.patch.object(checker, "MAC", True):
            ids = [case.id for case in checker.rc_cases()]
        for combo in ("legacy-legacy", "legacy-rust", "rust-legacy", "rust-rust"):
            self.assertIn(f"rc-linux-reject-{combo}", ids)
        units = (checker.ROOT / "native/crates/cli/tests/r4q_units.rs").read_text(encoding="utf-8")
        self.assertIn("Mutation::FallbackToPosix", units)

    def test_empty_mask_and_signals_not_restored(self):
        self.assert_found(make_fake("mask-anchor", cp=CP_EMPTY_MASK), ["probe-posix-mask-rust-rust"], "blocked")
        self.assert_found(make_fake("restore-anchor", cp=CP_NO_RESTORE), ["probe-posix-basic-rust-rust"], "ignored")
        if not self.linux_only(["ls-mask"]):
            self.assert_found(make_fake("mask-ls", ls=LS_EMPTY_MASK), ["ls-mask"], "blocked")
            self.assert_found(make_fake("restore-ls", ls=LS_NO_RESTORE), ["ls-mask"], "ignored")

    def test_environment_order_and_lc_ctype(self):
        self.assert_found(make_fake("env-order", cp=CP_ENV_ORDER), ["probe-posix-env-order-rust-rust"], "env")
        self.assert_found(make_fake("lc-ctype", cp=CP_NO_LC_CTYPE), ["probe-posix-lc-c-rust-rust"], "env")

    def test_enoexec_runs_sh(self):
        if not self.linux_only(["ls-enoexec"]):
            self.assert_found(make_fake("enoexec", ls=LS_ENOEXEC_SH), ["ls-enoexec"])

    def test_grandchild_left_and_no_pdeathsig(self):
        if not self.linux_only(["ls-grandchild-double", "ls-pdeathsig"]):
            self.assert_found(make_fake("subreaper", ls=LS_NO_SUBREAPER), ["ls-grandchild-double"], "残ったプロセス")
            self.assert_found(make_fake("pdeathsig", ls=LS_NO_PDEATHSIG), ["ls-pdeathsig"], "record")

    def test_read_end_closed_before_the_group_stops(self):
        # Linux は、anchor が EPIPE で 97 になり、品質コマンドの stderr のファイルに 97 の行が入る。Mac は EPERM。
        self.assert_found(make_fake("close-early", cp=CP_CLOSE_EARLY), ["rc-posix-timeout-term-rust-rust"])

    def test_exit_text_and_cross_combination(self):
        self.assert_found(make_fake("message", ls=LS_MESSAGE), ["ls-timeout-zero"], "stderr")
        if LINUX:
            self.assert_found(make_fake("wrong-nonce", cp=CP_WRONG_NONCE), ["rc-linux-exit3-legacy-rust"], "nonce")

    def test_record_permissions_are_compared(self):
        # 記録を 0o644 で作る誤り。Mac の check-process の --linux も、記録を作った後の 97 なので同じケースで見つかる。
        self.assert_found(make_fake("record-mode", cp=CP_RECORD_MODE), ["cp-linux-exit3"], "record_mode")

    def test_monitor_head_of_the_other_kind_fails(self):
        # 頭を取り違えた偽の親(Rust の監督の回に、旧版の監督を起動する)を、監督の argv の頭の確かめが見つける。
        wrong = make_fake("wrong-head", rust_head=[PYTHON, "-B", str(checker.CP_SCRIPT)])
        self.assert_found(wrong, ["probe-posix-basic-rust-rust"], "この回の監督(rust)の頭でない")

    def test_leftover_process_fails(self):
        self.assert_found(make_fake("leftover", cp=CP_LEFTOVER), ["cp-anchor-exit3"], "残ったプロセス")

    def test_timeout_fails(self):
        fake = make_fake("sleep", extra="import time\nif sys.argv[1:2] == ['--r4q-loop-supervisor']: time.sleep(60)")
        start = time.monotonic()
        code, text, results = run_checker(fake, ["ls-help"], "--run-timeout", "3")
        self.assertEqual(1, code)
        self.assertEqual("不合格", results["ls-help"][0])
        self.assertIn("時間切れ", results["ls-help"][1])
        self.assertLess(time.monotonic() - start, 60)

    def test_nonce_rules_are_not_skipped(self):
        # 規則と違う緩め方(長さや一致を確かめずに伏せる)なら通ってしまう誤り。
        self.assert_found(make_fake("short-nonce", cp=CP_SHORT_NONCE), ["probe-posix-basic-rust-rust"],
                          "16 進の小文字 64 字でない")
        code, text, results = run_checker(make_fake("same-nonce", cp=CP_SAME_NONCE),
                                          ["probe-posix-basic-rust-rust", "probe-posix-env-none-rust-rust"])
        self.assertEqual(1, code)
        self.assertIn("nonce が前の回と同じ", text)

    def test_injected_mistakes_are_checked_by_the_rust_unit_tests(self):
        # 差し替えで見つける誤り(設計の表の 3 行)は、r4q_units.rs の否定の検査が、壊した応答で固定期待と必ず
        # 食い違うことを示す。その否定の検査が全部あることを確かめる。
        units = (checker.ROOT / "native/crates/cli/tests/r4q_units.rs").read_text(encoding="utf-8")
        for name in ("IgnoreEperm", "EpermAsAbsent", "SendByPid", "SkipStarttime", "ReapAnchorEarly",
                     "FallbackToPosix", "SkipTermWithZeroGrace"):
            self.assertIn(f"Mutation::{name}", units)

    def test_keyerror_pid_is_checked(self):
        # 規則と違う緩め方(pid を記録した値と照らさずに伏せる)なら通ってしまう誤り。
        if not self.linux_only(["ls-sigchld-ignored"]):
            self.assert_found(make_fake("keyerror", ls=LS_KEYERROR_PID), ["ls-sigchld-ignored"], "KeyError の pid")


class CaseListTests(unittest.TestCase):
    """ケースの欠落・重複・0 件・skip と、固定の一覧。"""

    @staticmethod
    def ids():
        return [item["id"] for item in json.loads(checker.INJECTED.read_text(encoding="utf-8"))["cases"]]

    def build(self, linux, mac):
        with mock.patch.object(checker, "LINUX", linux), mock.patch.object(checker, "MAC", mac):
            return checker.all_cases(self.ids())

    def test_fixed_lists_match_both_platforms(self):
        for name, linux, mac in (("linux", True, False), ("macos", False, True)):
            cases = self.build(linux, mac)
            checker.check_case_list(cases, name)
            self.assertIn("pre-strerror", [case.id for case in cases])
            for fixed in checker.FIXED_INJECTED:
                self.assertIn(fixed, [case.id for case in cases])
        mac = self.build(False, True)
        skipped = sorted(case.id for case in mac if case.mac_skip == checker.SKIP_MAC_LS)
        self.assertEqual(sorted(checker.MAC_SKIPPED_LS), skipped)
        self.assertIn("ls-mac-setup", [case.id for case in mac])
        self.assertNotIn("ls-basic-exit3", [case.id for case in mac if case.mac_skip is None])
        linux = [case.id for case in self.build(True, False)]
        for required in ("ls-basic-exit3", "rc-linux-exit3-rust-rust", "rc-posix-sigchld-ignored-legacy-rust",
                         "rc-posix-anchor-killed-after-notice-rust-rust", "rc-posix-anchor-killed-before-notice-rust-rust",
                         "rc-linux-supervisor-stopped-rust-rust", "rc-posix-fake-notice-error-stop-group-fails-rust-fake"):
            self.assertIn(required, linux)

    def test_missing_extra_and_duplicate_cases_fail(self):
        cases = self.build(LINUX, MAC)
        with self.assertRaises(checker.Failure):
            checker.check_case_list(cases[:-1])
        with self.assertRaises(checker.Failure):
            checker.check_case_list([*cases, cases[-1]])
        original = checker.rc_cases
        with mock.patch.object(checker, "rc_cases", lambda: [*original(), original()[0]]):
            with self.assertRaisesRegex(checker.Failure, "ID が重なる"):
                checker.all_cases(self.ids())
        with mock.patch.object(checker, "MAC_SKIPPED_LS", checker.MAC_SKIPPED_LS[:-1]):
            with self.assertRaisesRegex(checker.Failure, "固定の一覧と違う"):
                checker.all_cases(self.ids())

    def test_final_status_refuses_missing_zero_duplicate_and_skip(self):
        ok = checker.Outcome("合格")
        self.assertEqual(0, checker.final_status({"a": ok}, ["a"]))
        self.assertEqual(1, checker.final_status({}, []))
        self.assertEqual(1, checker.final_status({"a": ok}, ["a", "b"]))
        self.assertEqual(1, checker.final_status({"a": ok}, ["a", "a"]))
        self.assertEqual(1, checker.final_status({"a": ok, "b": checker.Outcome("不合格", "x")}, ["a", "b"]))
        skip = checker.classify_skip(checker.SKIP_MAC_LS, mac=False)
        self.assertEqual("不合格", skip.status)
        self.assertEqual(1, checker.final_status({"a": ok, "b": skip}, ["a", "b"], mac=False))
        mac_skip = checker.classify_skip(checker.SKIP_MAC_LS, mac=True)
        self.assertEqual("未実施", mac_skip.status)
        self.assertEqual(0, checker.final_status({"a": ok, "b": mac_skip}, ["a", "b"], mac=True))
        self.assertEqual(1, checker.final_status({"b": mac_skip}, ["b"], mac=True))
        self.assertEqual("不合格", checker.classify_skip("ほかの理由", mac=True).status)
        self.assertEqual(1, checker.final_status({"a": ok, "b": checker.Outcome("未実施", "ほかの理由")}, ["a", "b"],
                                                 mac=True))


class RelaxRuleTests(unittest.TestCase):
    """緩め方の誤り(一覧に無い欄・規則と違う緩め方)を不合格にする。"""

    def test_names_outside_the_closed_list_fail(self):
        relax = checker.Relax()
        relax.add("nonce", 2)
        with self.assertRaisesRegex(checker.Failure, "閉じた一覧に無い欄"):
            relax.add("呼出履歴")
        self.assertEqual({"nonce": 2}, relax.counts)
        self.assertEqual(11, len(checker.RELAXED))
        # 06:58 の決定の範囲で足した欄(POSIX の方式の grace=0 の TERM の数)は、この名前で数える。
        relax.add("POSIX の grace=0 の TERM の数")
        self.assertEqual({"nonce": 2, "POSIX の grace=0 の TERM の数": 1}, relax.counts)

    def test_nonce_must_be_64_lowercase_hex(self):
        checker.check_nonce("0" * 64, "x")
        for value in ("0" * 63, "0" * 65, "A" * 64, "g" * 64, None, 1):
            with self.subTest(value=value), self.assertRaises(checker.Failure):
                checker.check_nonce(value, "x")

    def test_fixed_names_change_only_frame_paths(self):
        ctx = mock.Mock(stdlib="/py/lib/python3.10")
        legacy = os.fsencode(str(checker.LS_SCRIPT))
        trace = b'  File "' + legacy + b'", line 50, in main\n    x\n'
        relax = checker.Relax()
        self.assertEqual(b'  File "loop-supervisor.py", line 50, in main\n    x\n',
                         checker.fixed_names(ctx, trace, relax))
        self.assertEqual({"固定名": 1}, relax.counts)
        # 呼出履歴の全部は緩めない: 行番号や文の違いは残る。
        other = b'  File "loop-supervisor.py", line 51, in main\n    x\n'
        self.assertNotEqual(checker.fixed_names(ctx, trace, checker.Relax()), other)
        # File の行の外にあるパス(利用者が渡したパスを含む文)は、固定名にしない。
        message = b"[Errno 2] No such file or directory: '" + legacy + b"'\n"
        self.assertEqual(message, checker.fixed_names(ctx, message, checker.Relax()))
        stdlib = b'  File "/py/lib/python3.10/argparse.py", line 2577, in _print_message\n'
        self.assertEqual(b'  File "argparse.py", line 2577, in _print_message\n',
                         checker.fixed_names(ctx, stdlib, checker.Relax()))

    def test_keyerror_pid_must_equal_the_recorded_pid(self):
        observed = {side: {"stderr": b"Traceback\nKeyError: 1234\n", "state_pid": 1234} for side in ("legacy", "rust")}
        with mock.patch.object(checker, "LINUX", True):
            checker.keyerror_pid(None, None, observed, checker.Relax())
            self.assertEqual(b"Traceback\nKeyError: @PID@\n", observed["legacy"]["stderr"])
            wrong = {side: {"stderr": b"KeyError: 1235\n", "state_pid": 1234} for side in ("legacy", "rust")}
            with self.assertRaisesRegex(checker.Failure, "KeyError の pid"):
                checker.keyerror_pid(None, None, wrong, checker.Relax())

    def test_thread_value_format_is_checked(self):
        good = b"Fatal Python error: x\nCurrent thread 0x00007f0123 (most recent call first):\n"
        relax = checker.Relax()
        self.assertIn(b"0x@THREAD@", checker.mask_thread(good, relax))
        for bad in (good + good, b"Current thread 12 (most recent call first):\n"):
            with self.subTest(bad=bad), self.assertRaises(checker.Failure):
                checker.mask_thread(bad, checker.Relax())

    def test_timeout_text_head_only(self):
        ctx = mock.Mock(python="/py/bin/python3.10", rust_head=["/r/rust"])
        legacy = "Command '['/py/bin/python3.10', '-B', '" + str(checker.CP_SCRIPT) + "', '--linux']' timed out after 2 seconds"
        rust = "Command '['/r/rust', '--r4q-check-process', '--linux']' timed out after 2 seconds"
        fixed = "Command '['python3', '-B', 'check-process.py', '--linux']' timed out after 2 seconds"
        relax = checker.Relax()
        self.assertEqual(fixed, checker.normalize_timeout_text(ctx, legacy, relax, "legacy"))
        self.assertEqual(fixed, checker.normalize_timeout_text(ctx, rust, relax, "rust"))
        self.assertEqual({"監督の argv の頭": 2}, relax.counts)
        # その回の監督でない頭は戻さない(取り違えた頭は、比べると違いとして残る)。
        self.assertEqual(rust, checker.normalize_timeout_text(ctx, rust, checker.Relax(), "legacy"))
        self.assertEqual(legacy, checker.normalize_timeout_text(ctx, legacy, checker.Relax(), "rust"))
        other = "回収結果の照合に失敗: " + "['/py/bin/python3.10', '-B'"
        self.assertEqual(other, checker.normalize_timeout_text(ctx, other, checker.Relax(), "legacy"))

    def test_monitor_argv_head_must_be_the_one_of_this_run(self):
        ctx = mock.Mock(python="/py/bin/python3.10", rust_head=["/r/rust"])
        legacy = ["/py/bin/python3.10", "-B", str(checker.LS_SCRIPT), "--nonce", "n"]
        rust = ["/r/rust", "--r4q-loop-supervisor", "--nonce", "n"]
        fixed = ["python3", "-B", "loop-supervisor.py", "--nonce", "n"]
        self.assertEqual(fixed, checker.monitor_argv_fixed(ctx, "ls", "legacy", legacy, checker.Relax()))
        self.assertEqual(fixed, checker.monitor_argv_fixed(ctx, "ls", "rust", rust, checker.Relax()))
        for monitor, argv in (("legacy", rust), ("rust", legacy)):
            with self.subTest(monitor=monitor), self.assertRaisesRegex(checker.Failure, "この回の監督"):
                checker.monitor_argv_fixed(ctx, "ls", monitor, argv, checker.Relax())

    def test_sigint_rule_compares_signal_last_line_and_output_before(self):
        def obs(exit_code, err):
            return {"exit": exit_code, "stderr": err}
        good = b"before\nTraceback (most recent call last):\n  File x\nKeyboardInterrupt\n"
        observed = {"legacy": obs(-2, good), "rust": obs(-2, good.replace(b"File x", b"File y"))}
        checker.sigint_stable(None, None, observed, checker.Relax())
        self.assertEqual(observed["legacy"], observed["rust"])
        for bad in ({"legacy": obs(1, good), "rust": obs(-2, good)},
                    {"legacy": obs(-2, good + b"x"), "rust": obs(-2, good)}):
            with self.subTest(bad=bad), self.assertRaises(checker.Failure):
                checker.sigint_stable(None, None, bad, checker.Relax())
        # それまでの出力が違えば、伏せた後も違う。
        observed = {"legacy": obs(-2, b"a\n" + good), "rust": obs(-2, b"b\n" + good)}
        checker.sigint_stable(None, None, observed, checker.Relax())
        self.assertNotEqual(observed["legacy"], observed["rust"])

    def test_term_count_rule(self):
        with self.assertRaises(checker.Failure):
            checker.check_terms("many", [1.0], checker.Relax(), "rust")
        relax = checker.Relax()
        self.assertEqual("2 回以上", checker.check_terms("many", [1.0, 2.0], relax, "rust"))
        self.assertEqual({"TERM の回数": 1}, relax.counts)
        self.assertEqual(0, checker.check_terms("exact", [], checker.Relax(), "rust"))
        for times in ([], [1.0]):
            relax = checker.Relax()
            self.assertEqual("0 か 1", checker.check_terms("zero-or-one", times, relax, "rust"))
            self.assertEqual({"POSIX の grace=0 の TERM の数": 1}, relax.counts)
        with self.assertRaisesRegex(checker.Failure, "0 か 1 でない"):
            checker.check_terms("zero-or-one", [1.0, 2.0], checker.Relax(), "rust")


class EnvironmentAndPythonTests(unittest.TestCase):
    """基準でない Python・外すべき変数の残り・strerror の表の確かめ。"""

    def report(self, **changes):
        report = {"implementation": "CPython", "version": "3.10.11", "executable": "/py", "stdlib": "/py/lib",
                  "sha256": dict(checker.build.R4Q_STDLIB_SHA256),
                  "pidfd": {"os.pidfd_open": True, "signal.pidfd_send_signal": True}, "pidfd_call": "成功",
                  "strerror": {}}
        report.update(changes)
        return report

    def test_python_that_is_not_the_reference_fails(self):
        with mock.patch.object(checker.build, "r4q_python_report", return_value=self.report()):
            checker.check_python("/py")
        stdlib = dict(checker.build.R4Q_STDLIB_SHA256, **{"subprocess.py": "0" * 64})
        for changes in ({"version": "3.10.12"}, {"implementation": "PyPy"}, {"sha256": stdlib},
                        {"pidfd_call": "PermissionError"}):
            with self.subTest(changes=changes), \
                    mock.patch.object(checker.build, "r4q_python_report", return_value=self.report(**changes)), \
                    mock.patch.object(checker, "LINUX", True), self.assertRaisesRegex(checker.Failure, "基準でない"):
                checker.check_python("/py")
        with self.assertRaises(checker.Failure):
            checker.check_python("python3.10")
        with self.assertRaises(checker.Failure):
            checker.check_python("/bin/false")

    def test_out_of_scope_variable_seen_by_the_command_fails(self):
        state = {"pid": 5, "ppid": 4, "pgid": 5, "sid": 5, "cwd": "2f", "fds": [0, 1, 2], "stdin": None,
                 "blocked": [], "ignored": [], "caught": [], "parent_sigblk": None, "parent_sigign": None,
                 "parent_argv": None, "env": [b"PATH=/bin".hex(), b"PYTHONPATH=/x".hex()]}
        ctx = mock.Mock()
        with self.assertRaisesRegex(checker.Failure, "前提の外の変数.*PYTHONPATH"):
            checker.state_view(ctx, state, 4, checker.Relax(), "cp", checker.allowed_env_names(["PATH"]), "rust", "rust")
        state["env"] = [b"PATH=/bin".hex(), b"LC_CTYPE=C.UTF-8".hex()]
        ctx = mock.Mock(python="/py/bin/python3.10", rust_head=["/r/rust"])
        state["parent_argv"] = [item.encode().hex() for item in ("/r/rust", "--r4q-check-process", "--nonce", "n")]
        view, _ = checker.state_view(ctx, state, 4, checker.Relax(), "cp", checker.allowed_env_names(["PATH"]), "rust",
                                     "rust")
        self.assertEqual(2, len(view["env"]))
        self.assertEqual(["python3", "-B", "check-process.py", "--nonce", "n"], view["monitor_argv"])

    def test_unreadable_monitor_argv_fails_even_when_both_sides_are_null(self):
        state = {"pid": 5, "ppid": 4, "pgid": 5, "sid": 5, "cwd": "2f", "fds": [0, 1, 2], "stdin": None,
                 "blocked": [], "ignored": [], "caught": [], "parent_sigblk": None, "parent_sigign": None,
                 "parent_argv": None, "env": []}
        with self.assertRaisesRegex(checker.Failure, "監督の argv を読めない"):
            checker.state_view(mock.Mock(), state, 4, checker.Relax(), "cp", set(), "rust", "rust",
                               case_id="probe-posix-basic-rust-rust")
        # 親が先に消えるケースの固定の一覧(いまは空)は、どれも必須のケースの ID。
        ids = [case.id for case in checker.all_cases(CaseListTests.ids())]
        for case_id in checker.PARENT_GONE_IDS:
            self.assertIn(case_id, ids)

    def test_probe_that_returns_a_null_argv_fails_a_real_case(self):
        # probe が親の argv を null で返す代わり(読めない Mac の KERN_PROCARGS2 の代わり)を、比較器が不合格にする。
        source = checker.PROBE_SOURCE.read_text(encoding="utf-8")
        old = "    *len = 0;\n#ifdef __linux__\n    char path[64];"
        self.assertEqual(1, source.count(old))
        broken = Path(temp(), "r4q_probe_null_argv.c")
        broken.write_text(source.replace(old, "    *len = 0;\n    if (ppid > 0) return NULL;\n#ifdef __linux__\n"
                                              "    char path[64];"), encoding="utf-8")
        with mock.patch.object(checker, "PROBE_SOURCE", broken):
            ctx = open_ctx(make_fake("same"))
        try:
            case = next(case for case in checker.cli_cases() if case.id == "cp-anchor-probe")
            with self.assertRaisesRegex(checker.Failure, "監督の argv を読めない"):
                checker.run_case(ctx, case)
        finally:
            checker.close_context(ctx)

    def test_out_of_scope_variable_reaching_a_real_child_fails(self):
        original = checker.child_env

        def leaky(ctx, extra=None):
            env = original(ctx, extra)
            env["PYTHONPATH"] = os.path.join(temp(), "no-such-path")
            return env
        rust = make_fake("same")
        with mock.patch.object(checker, "child_env", leaky):
            ctx = open_ctx(rust)
            try:
                case = next(case for case in checker.cli_cases() if case.id == "cp-anchor-probe")
                with self.assertRaisesRegex(checker.Failure, "前提の外の変数"):
                    checker.run_case(ctx, case)
            finally:
                checker.close_context(ctx)

    def test_strerror_tables(self):
        numbers = range(0, checker.STRERROR_LAST + 1)
        linux = {str(n): ("Success" if n == 0 else f"Unknown error {n}" if n > 133 else f"e{n}") for n in numbers}
        checker.check_strerror(dict(linux), dict(linux), mac=False)
        changed = dict(linux, **{"5": "x"})
        with self.assertRaisesRegex(checker.Failure, "違う番号"):
            checker.check_strerror(changed, linux, mac=False)
        longer = dict(linux, **{"140": "e140"})
        with self.assertRaisesRegex(checker.Failure, "Unknown error でない"):
            checker.check_strerror(longer, longer, mac=False)
        mac = {str(n): ("Undefined error: 0" if n == 0 else f"Unknown error: {n}" if n > 107 else f"m{n}")
               for n in numbers}
        checker.check_strerror(dict(mac), dict(mac), mac=True)
        shorter = dict(mac, **{"107": "Unknown error: 107"})
        with self.assertRaisesRegex(checker.Failure, "表が実際より長い"):
            checker.check_strerror(shorter, shorter, mac=True)
        with self.assertRaisesRegex(checker.Failure, "0〜1024"):
            checker.check_strerror({k: v for k, v in mac.items() if k != "0"}, mac, mac=True)

    def test_mac_sdk_elast_above_the_table_and_missing_xcrun_fail(self):
        ours = {str(n): f"Unknown error: {n}" if n > 107 else f"m{n}" for n in range(0, checker.STRERROR_LAST + 1)}
        ctx = mock.Mock()
        runtime = {"ProductVersion": "15.7.9", "BuildVersion": "24G830"}
        with mock.patch.object(checker, "MAC", True), mock.patch.object(checker, "LINUX", False), \
                mock.patch.object(checker, "run_strerror", return_value=(ours, dict(ours))), \
                mock.patch.object(checker, "mac_runtime_version", return_value=runtime):
            with mock.patch.object(checker, "mac_elast_from_sdk", return_value=(106, "15.5", "/sdk")):
                checker.case_strerror(ctx, None, checker.Relax())
            with mock.patch.object(checker, "mac_elast_from_sdk", return_value=(108, "26.0", "/sdk")):
                with self.assertRaisesRegex(checker.Failure, "表が足りない"):
                    checker.case_strerror(ctx, None, checker.Relax())
            with mock.patch.object(checker.subprocess, "run", side_effect=FileNotFoundError(2, "xcrun")):
                with self.assertRaisesRegex(checker.Failure, "xcrun を使えない"):
                    checker.case_strerror(ctx, None, checker.Relax())


def open_ctx(rust):
    python = PYTHON
    report = checker.check_python(python)
    return checker.open_context(os.path.realpath(rust), python, report, checker.LOCALE_OF[checker.platform_name()],
                                base_for("ctx"))


class MacPauseTests(unittest.TestCase):
    """Mac の閉じた一覧の未実施(anchor が pause に入ったことを期限内に確かめられない)に届き、何も残さない。"""

    def test_pause_not_confirmed_in_time_is_not_run_and_leaves_nothing(self):
        case_id = "rc-posix-anchor-killed-after-notice-legacy-legacy"
        case = next(case for case in checker.rc_cases() if case.id == case_id)
        ctx = open_ctx(make_fake("same"))
        try:
            start = time.monotonic()
            with mock.patch.object(checker, "LINUX", False), mock.patch.object(checker, "MAC", True), \
                    mock.patch.object(checker, "mac_paused", lambda pid: False), \
                    mock.patch.object(checker, "ANCHOR_CHECK_LIMIT", [8.0]), \
                    mock.patch("builtins.print", lambda *args, **kwargs: None):
                outcomes = checker.compare_all(ctx, [case])
            outcome = outcomes[case_id]
            self.assertEqual(("未実施", checker.SKIP_ANCHOR_PAUSE), (outcome.status, outcome.message))
            self.assertLess(time.monotonic() - start, 60)
            self.assertEqual(0, checker.sweep(ctx.token))
        finally:
            checker.close_context(ctx)


class SameErrorAndTempTests(unittest.TestCase):
    """両側の同じ誤りと、残った一時フォルダ。"""

    def test_same_wrong_result_on_both_sides_fails(self):
        # 旧版と Rust が同じに誤っても、旧版の固定の SHA-256 とケースの固定期待が見つける。
        fake = make_fake("noargs-check", ls=LS_NOARGS_CHECK)
        copy = Path(fake).with_name("loop-supervisor.py")
        with mock.patch.object(checker, "LS_SCRIPT", copy), \
                mock.patch.dict(checker.SCRIPT_OF, {"ls": copy}):
            with self.assertRaisesRegex(checker.Failure, "旧版 loop-supervisor.py が基準"):
                checker.check_legacy_scripts()
            ctx = open_ctx(fake)
            try:
                case = next(case for case in checker.cli_cases() if case.id == "ls-noargs")
                with self.assertRaisesRegex(checker.Failure, "固定期待と違う"):
                    checker.run_case(ctx, case)
            finally:
                checker.close_context(ctx)

    def test_left_temporary_folder_fails(self):
        base = base_for("left")
        env = {"PATH": os.environ.get("PATH", checker.CHILD_PATH), "R4Q_PYTHON": PYTHON, "TMPDIR": base}
        output = []
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(checker, "remove_tree", lambda path: None), \
                mock.patch("builtins.print", lambda *args, **kwargs: output.append(" ".join(map(str, args)))):
            code = checker.main(["--rust", make_fake("same"), "--only", "pre-strerror"])
        self.assertEqual(1, code)
        self.assertTrue(any("一時フォルダが残った" in line for line in output), output[-5:])
        for name in os.listdir(base):
            checker.remove_tree(os.path.join(base, name))


PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


def own_subreaper_off():
    """自分の subreaper を 0 に戻し、PR_GET_CHILD_SUBREAPER で 0 を確かめる(prctl は自分にしか効かない)。"""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0):
        raise AssertionError(f"subreaper を 0 にできない: errno {ctypes.get_errno()}")
    value = ctypes.c_int(-1)
    if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0):
        raise AssertionError(f"PR_GET_CHILD_SUBREAPER を読めない: errno {ctypes.get_errno()}")
    return value.value


def comm(pid):
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return "読めない"


def ppid_of(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


class KillDuringHoldTests(unittest.TestCase):
    """比較器を補助の保持の途中で KILL しても、補助と保持した子が 30 秒以内に消える(Linux。必須)。"""

    def test_helper_and_held_child_vanish_after_the_checker_is_killed(self):
        case_id = "rc-posix-fake-notice-error-stop-group-fails-legacy-fake"
        if not LINUX:
            # Mac は補助を使わない(終了済みだけの群への killpg が EPERM になる経路で比べる)。
            with mock.patch.object(checker, "LINUX", False), mock.patch.object(checker, "MAC", True):
                case = next(case for case in checker.rc_cases() if case.id == case_id)
            self.assertFalse(case.params.get("hold"))
            return
        self.assertEqual(0, own_subreaper_off())
        report = os.path.join(temp(), "hold-report.json")
        base = base_for("kill")
        env = {"PATH": os.environ.get("PATH", checker.CHILD_PATH), "R4Q_PYTHON": PYTHON, "TMPDIR": base}
        process = subprocess.Popen([sys.executable, "-B", str(HERE / "check_r4q.py"), "--rust", make_fake("same"),
                                    "--hold-report", report, "--only", case_id], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 120
            while not os.path.exists(report):
                self.assertIsNone(process.poll(), "比較器が補助の保持の前に終わった")
                self.assertLess(time.monotonic(), deadline, "補助の保持が始まらない")
                time.sleep(0.05)
            time.sleep(0.2)
            data = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertIsNotNone(data["held"], "保持した子の pid が無い")
            helper, held = data["helper"], int(data["held"])
            self.assertTrue(os.path.exists(f"/proc/{helper}"), "補助がいない")
        finally:
            process.kill()
            process.wait()
        print(f"  pid 1: {comm(1)}・自己検査 {os.getpid()}・検査用の実行物(親) {os.getppid()} {comm(os.getppid())}",
              flush=True)
        deadline = time.monotonic() + 30
        while os.path.exists(f"/proc/{helper}"):
            self.assertLess(time.monotonic(), deadline, f"補助 {helper} が 30 秒以内に消えない")
            time.sleep(0.05)
        parent = ppid_of(held)
        if parent is None:
            print("  保持した子: 補助が消えた時には回収済み(引き取り先は読めない)", flush=True)
        else:
            who = "自己検査自身" if parent == os.getpid() else comm(parent)
            print(f"  保持した子 {held} の引き取り先(上位の subreaper か init): pid {parent} {who}", flush=True)
            self.assertNotEqual(os.getpid(), parent, "保持した子が自己検査へ引き取られた(subreaper の戻し忘れ)")
            if comm(os.getppid()).startswith("r4q-"):
                self.assertNotEqual(os.getppid(), parent, "保持した子が検査用の実行物へ引き取られた")
        while os.path.exists(f"/proc/{held}"):
            self.assertLess(time.monotonic(), deadline, f"保持した子 {held} が 30 秒以内に消えない")
            time.sleep(0.05)
        for name in os.listdir(base):
            checker.remove_tree(os.path.join(base, name))
        os.rmdir(base)


def main():
    if not PYTHON:
        print("R4Q の比較器の自己検査: 不合格: R4Q_PYTHON に本家 3.10.11 の Python を渡す", flush=True)
        return 1
    try:
        root = checker.temp_root()
    except checker.Failure as exc:
        print(f"R4Q の比較器の自己検査: 不合格: {exc}", flush=True)
        return 1
    STATE["temp"] = tempfile.mkdtemp(prefix="r4q-selftest-", dir=root)
    started = time.monotonic()
    try:
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    finally:
        checker.remove_tree(STATE["temp"])
    left = os.path.lexists(STATE["temp"])
    print(f"R4Q の比較器の自己検査: {result.testsRun} 件・失敗 {len(result.failures) + len(result.errors)} 件・"
          f"skip {len(result.skipped)} 件・{time.monotonic() - started:.1f} 秒", flush=True)
    if left or not result.wasSuccessful() or result.testsRun == 0 or result.skipped or result.expectedFailures \
            or result.unexpectedSuccesses:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
