"""R4U(#480)の比較器 check_r4u.py の自己検査。誤った結果・未実行・対象外の置き換えを合格にしないことを確かめる。

- 単体: 固定名の対応づけ(構造・対象の欄・導入先の置き場)、部分書込みの期待、Current thread の形式検査、
  比較・固定期待・ケースの一覧・未実施の扱い・環境の組み立て・旧 Python の確かめ。
- 実際の子: 共有ライブラリを作り、旧版と、旧版をそのまま動かす偽の検査対象で、合格の対照と、余計な出力・
  入力の変更・両側の同じ誤り・時間切れ・記録に現れない検査対象・許さない変数を拒むことを確かめる。
- 0 件・skip・失敗のどれかがあれば、終了値 1 で終わる。
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
import check_r4u as checker  # noqa: E402

LEGACY = os.fsencode(str(checker.LEGACY))
STDLIB = os.path.dirname(os.__file__)


def python(mismatched=()):
    return checker.Python(sys.executable, "3.10.11", (STDLIB,), dict(checker.STDLIB_SHA256), frozenset(mismatched))


def frame(path, line=93, func="<module>"):
    return b'  File "' + path + b'", line ' + str(line).encode() + b", in " + func.encode() + b"\n"


def write(fd, data, ret=None, errno=0, action="none"):
    return checker.Write(fd, data, len(data), len(data) if ret is None else ret, errno, action)


def image(names, pid=7, exe=b"/bin/x", values=None):
    return checker.Image(frozenset(names), pid, exe, {} if values is None else values)


def obs(exit=1, stdout=b"", stderr=b"", writes=None, frames=None):
    return checker.Obs(exit, stdout, stderr, [] if writes is None else writes, [] if frames is None else frames)


class NormalizerTests(unittest.TestCase):
    def setUp(self):
        self.norm = checker.Normalizer(python())

    def test_maps_only_frame_lines_at_write_and_line_starts(self):
        frames = []
        data = b"Traceback (most recent call last):\n" + frame(LEGACY)
        got = self.norm.map(data, True, frames)
        self.assertEqual(b"Traceback (most recent call last):\n" + frame(b"check_links.py"), got)
        self.assertEqual([["check_links.py", 93, "<module>"]], frames)
        stdlib = os.fsencode(os.path.join(STDLIB, "pathlib.py"))
        self.assertEqual(frame(b"pathlib.py", 1134, "read_text"), self.norm.map(frame(stdlib, 1134, "read_text")))

    def test_fake_file_line_in_user_input_is_not_replaced(self):
        # 利用者の入力(例外の本文の repr)に置いた偽の File の行は、行の途中なので対応づけない。
        line = b"IsADirectoryError: [Errno 21] Is a directory: '" + frame(LEGACY)[:-1] + b"/x.md'\n"
        self.assertEqual(line, self.norm.map(line))
        # 書込みの途中から始まる断片(行の始まりでない)も対応づけない。
        self.assertEqual(frame(LEGACY), self.norm.map(frame(LEGACY), at_start=False))

    def test_working_directory_and_other_paths_at_frame_position_fail(self):
        for path in (b"/tmp/r4u-check-x/case/check_links.py", os.fsencode(STDLIB) + b"/../lib/pathlib.py",
                     os.fsencode(STDLIB) + b"/json/pathlib.py", b"pathlib.py.bak", LEGACY + b"x",
                     b"<frozen importlib._bootstrap>"):
            with self.subTest(path=path), self.assertRaises(checker.Failure):
                self.norm.map(frame(path))

    def test_fixed_names_already_fixed_pass_unchanged(self):
        data = frame(b"check_links.py") + frame(b"posixpath.py", 397, "realpath")
        self.assertEqual(data, self.norm.map(data))

    def test_stdlib_that_differs_from_upstream_fails_only_its_frames(self):
        norm = checker.Normalizer(python({"posixpath.py"}))
        norm.map(frame(os.fsencode(os.path.join(STDLIB, "pathlib.py")), 1, "x"))
        with self.assertRaises(checker.Failure):
            norm.map(frame(os.fsencode(os.path.join(STDLIB, "posixpath.py")), 397, "realpath"))

    def test_cut_fixed_field_outside_partial_rule_fails(self):
        data = frame(LEGACY)
        with self.assertRaises(checker.Failure):
            checker.map_records(self.norm, [write(2, data, ret=20)], partial=False)
        # 固定名の欄を含まない書込みの切れは、そのまま比べる。
        mapped, _ = checker.map_records(self.norm, [write(1, b"abcdef", ret=3)], partial=False)
        self.assertEqual([(1, b"abcdef", 3, 0, "none")], mapped)

    def test_partial_rule_cuts_fixed_content_and_maps_the_resend(self):
        line = frame(LEGACY)
        fixed = frame(b"check_links.py")
        k = 12
        nxt = frame(LEGACY, 52, "main")
        writes = [write(2, b"Traceback (most recent call last):\n"), write(2, line, ret=k, action="short"),
                  write(2, line[k:] + nxt)]
        mapped, frames = checker.map_records(self.norm, writes, partial=True)
        self.assertEqual((2, fixed, k, 0, "short"), mapped[1])
        self.assertEqual((2, fixed[k:] + frame(b"check_links.py", 52, "main"), "full", 0, "none"), mapped[2])
        self.assertEqual(b"Traceback (most recent call last):\n" + fixed + frame(b"check_links.py", 52, "main"),
                         checker.passed(mapped, (2,)))
        self.assertEqual([["check_links.py", 93, "<module>"], ["check_links.py", 52, "main"]], frames)

    def test_partial_rule_refuses_when_only_legacy_is_cut(self):
        line = frame(LEGACY)
        with self.assertRaises(checker.Failure):
            checker.map_records(self.norm, [write(2, line, ret=len(frame(b"check_links.py")) + 3)], partial=True)


class ThreadValueTests(unittest.TestCase):
    good = b"Fatal Python error: x\n\nCurrent thread 0x00007f0123456789 (most recent call first):\n"

    def test_missing_malformed_and_twice_fail(self):
        checker.mask_thread(self.good)
        for data in (b"Fatal Python error: x\n", self.good.replace(b"0x00007f", b"0x0007f"),
                     self.good.replace(b"0x00007f0123456789", b"0x00007F0123456789"), self.good * 2):
            with self.subTest(data=data), self.assertRaises(checker.Failure):
                checker.mask_thread(data)

    def test_value_write_format_and_count(self):
        pieces = [(2, b"Current thread 0x", "full", 0, "none"), (2, b"00007f0123456789", "full", 0, "none")]
        masked, count = checker.mask_thread_writes(pieces)
        self.assertEqual((1, b"<thread>"), (count, masked[1][1]))
        with self.assertRaises(checker.Failure):
            checker.mask_thread_writes([pieces[0], (2, b"7f0123456789", "full", 0, "none")])
        self.assertEqual(2, checker.mask_thread_writes(pieces + pieces)[1])

    def test_format_check_is_not_applied_to_other_cases(self):
        # 起動の失敗でないケース(fatal でない)では、値の違いを比べる(形式だけの比べ方に転用しない)。
        case = checker.Case("not-fatal", "fault")
        ctx = object.__new__(checker.Context)
        ctx.norm = checker.Normalizer(python())
        results = []
        for value in (b"00007f0123456789", b"00007f0000000000"):
            data = b"Current thread 0x" + value + b" (most recent call first):\n"
            result = checker.Result(1, b"", data, [write(2, data)])
            results.append(checker.observe(ctx, case, result))
        with self.assertRaises(checker.Failure):
            checker.compare_obs("not-fatal", results[0], results[1], False)


class CompareTests(unittest.TestCase):
    def base(self):
        return obs(1, b"out\n", frame(b"check_links.py") + b"E: x\n", [(1, b"out\n", "full", 0, "none")],
                   [["check_links.py", 93, "<module>"]])

    def test_each_field_is_compared(self):
        good = self.base()
        checker.compare_obs("ok", good, self.base(), False)
        wrong = [obs(0, good.stdout, good.stderr, good.writes, good.frames),
                 obs(1, b"out\nextra\n", good.stderr, good.writes, good.frames),
                 obs(1, good.stdout, frame(b"check_links.py", 92) + b"E: x\n", good.writes, good.frames),
                 obs(1, good.stdout, frame(b"pathlib.py") + b"E: x\n", good.writes, good.frames),
                 obs(1, good.stdout, good.stderr, [(1, b"out\n", 2, 0, "none")], good.frames),
                 obs(1, good.stdout, good.stderr, good.writes, [["pathlib.py", 93, "<module>"]])]
        for value in wrong:
            with self.subTest(value=value), self.assertRaises(checker.Failure):
                checker.compare_obs("wrong", good, value, False)

    def test_interrupt_compares_signal_last_line_and_stdout(self):
        legacy = obs(-signal.SIGINT, b"a\n", b"Traceback\nKeyboardInterrupt\n", [(1, b"a\n", "full", 0, "none")])
        checker.compare_obs("ok", legacy, obs(-signal.SIGINT, b"a\n", b"other\nKeyboardInterrupt\n",
                                              [(1, b"a\n", "full", 0, "none")]), True)
        for left, right in ((obs(1, b"a\n", legacy.stderr, legacy.writes), obs(1, b"a\n", legacy.stderr, legacy.writes)),
                            (legacy, obs(-signal.SIGINT, b"a\n", b"KeyboardInterrupt: x\n", legacy.writes)),
                            (legacy, obs(-signal.SIGINT, b"b\n", legacy.stderr, legacy.writes)),
                            (legacy, obs(-signal.SIGINT, b"a\n", legacy.stderr, [(1, b"a\n", 1, 0, "none")]))):
            with self.assertRaises(checker.Failure):
                checker.compare_obs("bad", left, right, True)

    def test_timeout_and_not_run_fail(self):
        ctx = object.__new__(checker.Context)
        ctx.norm = checker.Normalizer(python())
        case = checker.Case("x", "basic")
        for result in (checker.Result(None, b"", b"", [], True), checker.Result(None, b"", b"", []),
                       checker.Result(0, b"", b"", [], True)):
            with self.assertRaises(checker.Failure):
                checker.observe(ctx, case, result)

    def test_extra_output_not_in_the_record_fails(self):
        case = checker.Case("x", "basic")
        result = checker.Result(0, b"out\nextra", b"", [write(1, b"out\n")])
        with self.assertRaises(checker.Failure):
            checker.check_capture(case, result)
        checker.check_capture(case, checker.Result(0, b"out\n", b"", [write(1, b"out\n")]))
        pty_case = checker.Case("x", "basic", stdout="pty")
        checker.check_capture(pty_case, checker.Result(0, b"a\r\n", b"", [write(1, b"a\n")]))
        with self.assertRaises(checker.Failure):
            checker.check_capture(pty_case, checker.Result(0, b"a\n", b"", [write(1, b"a\n")]))

    def test_log_must_end_with_a_complete_line(self):
        with self.assertRaises(checker.Failure):
            checker.read_log(b'{"env":[],"pid":7,"exe":""}\n{"fd":1')
        writes, image, chain = checker.read_log(b'{"env":["50415448"],"values":{},"pid":7,"exe":"2f62"}\n{"fd":1,'
                                                b'"len":1,"ret":1,"errno":0,"action":"none","data":"61"}\n')
        self.assertEqual(({"PATH"}, 7, b"/b", [write(1, b"a")], [b"/b"]),
                         (set(image.names), image.pid, image.exe, writes, chain))

    def test_exec_chain_uses_the_last_program_and_refuses_other_shapes(self):
        line = b'{"fd":1,"len":1,"ret":1,"errno":0,"action":"none","data":"61"}\n'
        first = b'{"env":["50415448","58"],"pid":7,"exe":"2f61"}\n'
        last = b'{"env":["50415448"],"pid":7,"exe":"2f62"}\n'
        writes, image, chain = checker.read_log(first + last + line)
        self.assertEqual(({"PATH"}, [b"/a", b"/b"], 1), (set(image.names), chain, len(writes)))
        # 値を記録する 2 つの変数の値を読む(__CF_USER_TEXT_ENCODING=0x1F5:0x0:0x0)。
        cf = checker.CF_ENCODING.encode().hex()
        _, image, _ = checker.read_log(('{"env":["%s"],"values":{"%s":"%s"},"pid":7,"exe":""}\n'
                                        % (cf, cf, b"0x1F5:0x0:0x0".hex())).encode())
        self.assertEqual({checker.CF_ENCODING: "0x1F5:0x0:0x0"}, image.values)
        # 連鎖の前のプログラムが書いていた・別の pid(子のプロセス)が記録した
        for data in (first + line + last, first + last.replace(b'"pid":7', b'"pid":8') + line):
            with self.subTest(data=data), self.assertRaises(checker.Failure):
                checker.read_log(data)


class LauncherTests(unittest.TestCase):
    """Mac の framework の起動用のプログラム(__PYVENV_LAUNCHER__ を付けて本体を起動し直す)の扱い。"""

    def test_framework_python_is_started_directly(self):
        with tempfile.TemporaryDirectory() as prefix:
            app = Path(prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
            app.parent.mkdir(parents=True)
            app.write_text("")
            app.chmod(stat.S_IRWXU)
            launcher = os.path.join(prefix, "bin", "python3.10")
            self.assertEqual(str(app), checker.direct_executable(launcher, prefix, "Python", mac=True))
            # framework でない Python(Linux・CI の Mac x86_64 の tar)と、Mac 以外は、そのまま使う。
            self.assertEqual(launcher, checker.direct_executable(launcher, prefix, "", mac=True))
            self.assertEqual(launcher, checker.direct_executable(launcher, prefix, "Python", mac=False))
            with self.assertRaises(checker.Failure):
                checker.direct_executable(launcher, prefix, "Other", mac=True)
        self.assertEqual(sys.executable if not checker.MAC else checker.this_python(), checker.this_python())

    def test_launcher_variable_is_out_of_scope_and_named_in_the_failure(self):
        child = checker.child_env({}, "/shim", "fd:9", "")
        image = checker.Image(frozenset(set(child) | {checker.LAUNCHER_VARIABLE}), 7, b"/app/Python",
                              {checker.LAUNCHER_VARIABLE: "/bin/python3.10"})
        with self.assertRaisesRegex(checker.Failure, "起動用"):
            checker.check_env_names("x", image, child, True, [b"/bin/python3.10", b"/app/Python"])
        with self.assertRaises(checker.Failure):
            checker.child_env({checker.LAUNCHER_VARIABLE: "/bin/python3.10"})


class FixtureDocumentTests(unittest.TestCase):
    """OS ごとの固定期待: 無いときにログへ出す形と、取り出し。"""

    def document(self):
        return checker.fixture_document(python(), ["a", "b"], {"a": {"exit": 0}}, {"b": checker.SKIP_DEVFULL}, True)

    def test_dump_round_trips_through_a_ci_log_with_timestamps(self):
        dump = checker.fixture_dump("cases-macos.json", self.document())
        log = "前の出力\n" + "\n".join("2026-11-12T00:00:00.0000000Z " + line for line in dump.splitlines()) + "\n後"
        self.assertEqual(self.document(), checker.extract_fixture(log, "cases-macos.json"))
        self.assertEqual(self.document(), checker.extract_fixture(dump, "cases-macos.json"))

    def test_mixed_missing_or_doubled_dump_fails(self):
        dump = checker.fixture_dump("cases-macos.json", self.document())
        lines = dump.splitlines()
        for log in ("\n".join(lines[:2] + ["混ざった行"] + lines[2:]), "\n".join(lines[:-1]), dump + "\n" + dump,
                    dump.replace('"exit": 0', '"exit": 1'), ""):
            with self.subTest(log=log[-80:]), self.assertRaises(checker.Failure):
                checker.extract_fixture(log, "cases-macos.json")

    def test_missing_os_fixture_fails_even_when_everything_passed(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(1, checker.overall_status(0, None, checker.FIXTURES / "cases-macos.json"))
        self.assertIn("cases-macos.json が無い", out.getvalue())
        self.assertEqual(0, checker.overall_status(0, {}, checker.FIXTURES / "cases-macos.json"))
        self.assertEqual(1, checker.overall_status(1, {}, checker.FIXTURES / "cases-macos.json"))

    def test_fixture_made_without_records_compares_the_rest(self):
        marks = [(b"/tmp/root", checker.ROOT_MARK)]
        recorded = obs(0, b"ok\n", b"", [(1, b"ok\n", "full", 0, "none")])
        unrecorded = checker.Obs(0, b"ok\n", b"", None, [])
        entry = checker.canonical(unrecorded, marks)
        self.assertIsNone(entry["writes"])
        checker.check_fixture(entry, unrecorded, recorded, None, marks, False, True)
        with self.assertRaises(checker.Failure):
            checker.check_fixture(entry, unrecorded, obs(1, b"ok\n", b"", recorded.writes), None, marks, False, True)
        # 記録ありの固定期待と、記録の無い旧版は、記録の欄を除いて比べる(Rust は記録まで比べる)。
        full = checker.canonical(recorded, marks)
        checker.check_fixture(full, unrecorded, recorded, None, marks, False, True)
        with self.assertRaises(checker.Failure):
            checker.check_fixture(full, unrecorded, obs(0, b"ok\n", b"", [(1, b"ok\n", 1, 0, "none")]), None,
                                  marks, False, True)


class DeepTreeTests(unittest.TestCase):
    """深い木(最も深いケースは 995 段)を、段の数に依らない fd の数で辿り、消す。"""

    SCRIPT = textwrap.dedent("""\
        import os, resource, sys
        sys.path.insert(0, sys.argv[2])
        sys.setrecursionlimit(20000)
        import check_r4u as c
        root = sys.argv[1]
        c.make_deep(os.path.join(root, "t"), 995, "d")
        os.mkdir(os.path.join(root, "n"))
        os.chmod(os.path.join(root, "n"), 0)
        os.makedirs(os.path.join(root, "x", "y"))
        os.chmod(os.path.join(root, "x"), 0o600)
        resource.setrlimit(resource.RLIMIT_NOFILE, (int(sys.argv[3]), resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
        try:
            before = c.snapshot(root)
        except c.Failure as exc:
            print("Failure", exc)
            sys.exit(3)
        fd = os.open(os.path.join(root, "t"), os.O_RDONLY)
        for _ in range(995):
            child = os.open("d", os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = child
        os.close(os.open("deep-file", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
        os.close(fd)
        after = c.snapshot(root)
        c.restore_and_remove(root)
        print(len(before), before != after, any(k.endswith(b"/deep-file") and len(k) > 1990 for k in after),
              before.get(b"/n/<open>"), before.get(b"/x/y"), os.path.lexists(root))
        """)

    def run_script(self, limit):
        with tempfile.TemporaryDirectory(prefix="r4u-deep-") as temp:
            root = os.path.join(temp, "case")
            os.mkdir(root)
            try:
                return subprocess.run([sys.executable, "-B", "-c", self.SCRIPT, root, str(Path(checker.__file__).parent),
                                       str(limit)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=120)
            finally:
                checker.restore_and_remove(root)

    def test_deep_tree_is_walked_and_removed_with_few_descriptors(self):
        done = self.run_script(24)
        self.assertEqual(0, done.returncode, done.stderr.decode())
        self.assertEqual("1000 True True ('error', 13) ('error', 13) False", done.stdout.decode().strip())

    def test_exhausted_descriptors_are_a_failure_not_data(self):
        # 開いている fd の番号より上限を小さくすると、最初の open が EMFILE になる。データにせず失敗にする。
        done = self.run_script(3)
        self.assertEqual(3, done.returncode, done.stdout.decode() + done.stderr.decode())
        self.assertIn("fd が尽きた", done.stdout.decode())

    def test_comparator_needs_a_fixed_number_of_descriptors(self):
        checker.check_nofile(checker.REQUIRED_NOFILE)
        with self.assertRaisesRegex(checker.Failure, "ulimit -n"):
            checker.check_nofile(checker.REQUIRED_NOFILE - 1)


class MacFixtureTests(unittest.TestCase):
    """Mac の固定期待(cases-macos.json): Mac の CI の 2 種類のログから取り出して 1 つにしたもの。"""

    # Task 3.1b で足したケース。Mac の値は、Mac 2 種類の CI のログ(比較器が、固定期待に無いケースだけを旧版で
    # 測って出したもの)から --extract-fixture で足す。それまで Mac の比較は不合格になる(ここでは欠けを許す)。
    MAC_PENDING = frozenset({
        *(f"signal-args-none-sigint-{mode}-{when}" for mode in ("pipe", "unbuffered", "pty") for when in ("1", "last")),
        "signal-env-hashseed-bad-word-sigint", "signal-stdio-stdin-dir-sigint"})

    @classmethod
    def setUpClass(cls):
        with patch.object(checker, "OS_NAME", "macos"):
            cls.path = checker.os_fixture_path()
            cls.doc = checker.load_fixture_document(cls.path)
        cls.linux = json.loads(checker.CASES_FIXTURE.read_text(encoding="utf-8"))
        cls.cases, cls.step2 = checker.all_cases()
        _, cls.missing = checker.os_fixture_plan(cls.linux["case_ids"], cls.doc, cls.path.name)

    def test_document_is_read_on_mac_and_matches_the_case_list(self):
        self.assertEqual("cases-macos.json", self.path.name)
        # Mac の一覧は、Linux の一覧から、まだ Mac の値を足していないケースだけを除いた形(順は同じ)。
        self.assertLessEqual(set(self.missing), self.MAC_PENDING)
        self.assertEqual([i for i in self.linux["case_ids"] if i not in self.missing], self.doc["case_ids"])
        self.assertEqual(["arm64", "x86_64"], self.doc["machines"])
        self.assertTrue(self.doc["legacy_recorded"])
        # Linux では、この OS の固定期待として読まない(OS の欄が違う)。
        with patch.object(checker, "OS_NAME", "linux"), self.assertRaises(checker.Failure):
            checker.load_fixture_document(self.path)

    def test_every_case_has_a_mac_expectation_or_a_closed_list_reason(self):
        reasons = set(self.doc["skipped"].values())
        self.assertLessEqual(reasons, {checker.SKIP_DEVFULL, checker.SKIP_NON_UTF8})
        for case in self.cases:
            with self.subTest(case=case.id):
                if case.id in self.missing:
                    self.assertNotIn(case.id, self.doc["cases"])
                    self.assertNotIn(case.id, self.doc["skipped"])
                elif case.step2 is None and not case.fixture:
                    self.assertNotIn(case.id, self.doc["cases"])
                elif case.id in self.doc["skipped"]:
                    self.assertNotIn(case.id, self.doc["cases"])
                else:
                    self.assertIn(case.id, self.doc["cases"])
        self.assertEqual(sum(1 for c in self.cases if c.step2 is not None), len(self.step2["entries"]))

    def test_merge_requires_everything_but_the_machine_to_match(self):
        one = dict(self.doc, machines=["x86_64"])
        other = dict(self.doc, machines=["arm64"])
        self.assertEqual(["arm64", "x86_64"], checker.merge_fixtures([one, other])["machines"])
        old_style = {k: v for k, v in self.doc.items() if k != "machines"}
        self.assertEqual(["arm64", "x86_64"],
                         checker.merge_fixtures([dict(old_style, machine="arm64"), one])["machines"])
        changed = json.loads(json.dumps(other))
        changed["cases"]["legacy:basic-doc-example"]["exit"] = 0
        with self.assertRaisesRegex(checker.Failure, "legacy:basic-doc-example"):
            checker.merge_fixtures([one, changed])
        with self.assertRaises(checker.Failure):
            checker.merge_fixtures([])


class MissingFixtureTests(unittest.TestCase):
    """この OS の固定期待に無いケース: 固定期待の一覧の形を確かめ、無いケースがあれば比較を不合格にする。"""

    def test_plan_finds_only_missing_cases_and_keeps_the_order(self):
        doc = {"case_ids": ["a", "c"], "cases": {"a": {"exit": 0}}}
        self.assertEqual(({"a": {"exit": 0}}, ["b", "d"]), checker.os_fixture_plan(["a", "b", "c", "d"], doc, "x"))
        self.assertEqual(({"a": {"exit": 0}}, []), checker.os_fixture_plan(["a", "c"], doc, "x"))
        self.assertEqual((None, []), checker.os_fixture_plan(["a", "c"], None, "x"))
        # 固定期待にだけあるケース・順の違い・重複は、何も測らずに失敗。
        for ids, have in ((["a"], ["a", "c"]), (["c", "a", "b"], ["a", "c"]), (["a", "b"], ["a", "a"]),
                          (["a", "a"], ["a"])):
            with self.subTest(ids=ids, have=have), self.assertRaises(checker.Failure):
                checker.os_fixture_plan(ids, {"case_ids": have, "cases": {}}, "x")

    def test_missing_cases_fail_even_when_everything_passed(self):
        import contextlib
        import io
        path = checker.FIXTURES / "cases-macos.json"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(1, checker.overall_status(0, {}, path, ["a", "b"]))
        self.assertIn("無いケースが 2 件", out.getvalue())
        self.assertEqual(0, checker.overall_status(0, {}, path, []))

    def test_additions_document_holds_only_missing_cases(self):
        measured = {"a": {"exit": 0}, "b": {"exit": 1}, "d": {"exit": 2}}
        outcomes = {"a": checker.Outcome("合格"), "b": checker.Outcome("不合格", "x"),
                    "c": checker.classify_skip(checker.SKIP_DEVFULL, True),
                    "e": checker.classify_skip(checker.SKIP_NON_UTF8, True)}
        doc = checker.additions_document(python(), ["a", "b", "c", "d", "e"], ["b", "c"], measured, outcomes, True)
        self.assertEqual((["b", "c"], {"b": {"exit": 1}}, {"c": checker.SKIP_DEVFULL}),
                         (doc[checker.ADDED_KEY], doc["cases"], doc["skipped"]))
        self.assertEqual(["a", "b", "c", "d", "e"], doc["case_ids"])


class ExtractAdditionsTests(unittest.TestCase):
    """--extract-fixture の足す形: 既存の固定期待の項目を 1 バイトも変えず、足りないケースの項目と一覧だけを足す。"""

    NEW = "signal-new-selftest"

    @classmethod
    def setUpClass(cls):
        with patch.object(checker, "OS_NAME", "macos"):
            cls.path = checker.os_fixture_path()
        cls.text = cls.path.read_text(encoding="utf-8")
        cls.existing = json.loads(cls.text)

    def additions(self, machines=None, entry=None, added=None, ids=None, skipped=None):
        old = self.existing["case_ids"]
        new_ids = ids if ids is not None else old[:len(old) // 2] + [self.NEW] + old[len(old) // 2:]
        cases = {self.NEW: {"exit": 1, "stdout": "x\n"}} if entry is None else entry
        with patch.object(checker, "OS_NAME", "macos"):
            doc = checker.fixture_document(python(), new_ids, cases, skipped or {}, True)
        doc["machines"] = list(self.existing["machines"] if machines is None else machines)
        doc[checker.ADDED_KEY] = [self.NEW] if added is None else added
        return doc

    def test_existing_entries_are_kept_byte_for_byte(self):
        import difflib
        text = checker.add_to_fixture(self.text, self.additions(), set())
        new = json.loads(text)
        self.assertEqual({"exit": 1, "stdout": "x\n"}, new["cases"][self.NEW])
        self.assertEqual(self.additions()["case_ids"], new["case_ids"])
        for key, value in self.existing.items():
            if key not in ("case_ids", "cases"):
                self.assertEqual(value, new[key])
        for key, value in self.existing["cases"].items():
            self.assertEqual(value, new["cases"][key])
        # 行で見ても、既存の行は 1 行も変わらず、足した行(一覧の ID の 1 行と、項目の行)だけが入る。
        old_lines, new_lines = self.text.splitlines(), text.splitlines()
        ops = {op for op, *_ in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()}
        self.assertEqual({"equal", "insert"}, ops)
        added = 1 + len(checker.entry_text(self.NEW, new["cases"][self.NEW]).splitlines())
        self.assertEqual(len(old_lines) + added, len(new_lines))

    def test_round_trip_through_two_mac_logs(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory(prefix="r4u-extract-") as temp:
            out = Path(temp, "cases-macos.json")
            out.write_text(self.text, encoding="utf-8")
            logs = []
            for machine in ("arm64", "x86_64"):
                doc = self.additions(machines=[machine])
                log = Path(temp, f"{machine}.log")
                with patch.object(checker, "OS_NAME", "macos"):
                    dump = checker.fixture_dump(out.name, doc)
                log.write_text("前\n" + "\n".join("2026-11-12T00:00:00.0000000Z " + line for line in dump.splitlines())
                               + "\n後\n", encoding="utf-8")
                logs.append(log)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, checker.main_extract_fixture(logs, out))
            self.assertEqual(checker.add_to_fixture(self.text, self.additions(), set()), out.read_text(encoding="utf-8"))
            # 片方の機械のログだけ: 2 種類の Mac の値が同じことを確かめられないので、足さない。
            out.write_text(self.text, encoding="utf-8")
            with self.assertRaisesRegex(checker.Failure, "機械"):
                checker.main_extract_fixture(logs[:1], out)
            # 2 種類の機械で値が違う: 1 つにできない。
            other = self.additions(machines=["x86_64"], entry={self.NEW: {"exit": 2}})
            logs[1].write_text(checker.fixture_dump(out.name, other), encoding="utf-8")
            with self.assertRaises(checker.Failure):
                checker.main_extract_fixture(logs, out)
            self.assertEqual(self.text, out.read_text(encoding="utf-8"))
            # 全体の形のログは、既存の固定期待を上書きしない。足す形のログは、固定期待が無ければ書かない。
            full = dict(self.additions())
            del full[checker.ADDED_KEY]
            logs[0].write_text(checker.fixture_dump(out.name, full), encoding="utf-8")
            with self.assertRaisesRegex(checker.Failure, "上書きしない"):
                checker.main_extract_fixture(logs[:1], out)
            missing = Path(temp, "none", "cases-macos.json")
            logs[0].write_text(checker.fixture_dump(out.name, self.additions()), encoding="utf-8")
            with self.assertRaisesRegex(checker.Failure, "無い"):
                checker.main_extract_fixture(logs[:1], missing)

    def test_additions_that_would_change_existing_items_fail(self):
        old = self.existing["case_ids"]
        changed_header = self.additions()
        changed_header["python"] = dict(changed_header["python"], version="3.10.12")
        bad = [
            self.additions(entry={self.NEW: {"exit": 1}, old[0]: {"exit": 9}}),  # 既存の項目を書き換える
            self.additions(added=[self.NEW, old[0]]),  # 既存の一覧にあるケースを足す
            self.additions(ids=[self.NEW] + old[::-1]),  # 既存の順を変える
            self.additions(ids=old),  # 足すケースが一覧に無い
            self.additions(entry={}),  # 足すケースの値が無い
            self.additions(skipped={self.NEW: checker.SKIP_DEVFULL}),  # 項目と未実施の理由の両方
            self.additions(entry={"other": {"exit": 0}, self.NEW: {"exit": 1}}),  # 足すケースでない項目
            self.additions(machines=["arm64"]),  # 測った機械が足りない
            changed_header,  # 測った条件が違う
        ]
        for doc in bad:
            with self.subTest(doc={k: v for k, v in doc.items() if k != "case_ids"}), \
                    self.assertRaises(checker.Failure):
                checker.add_to_fixture(self.text, doc, set())
        # 固定期待を持たないケースの項目・比較器の書き方でない既存のファイル
        with self.assertRaises(checker.Failure):
            checker.add_to_fixture(self.text, self.additions(), {self.NEW})
        with self.assertRaises(checker.Failure):
            checker.add_to_fixture(json.dumps(self.existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                   self.additions(), set())
        # 固定期待を持たないケースは、値が無くても足せる(一覧だけに入る)。
        text = checker.add_to_fixture(self.text, self.additions(entry={}), {self.NEW})
        self.assertNotIn(self.NEW, json.loads(text)["cases"])
        self.assertIn(self.NEW, json.loads(text)["case_ids"])


class OpenFrameTests(unittest.TestCase):
    """送った時点の確かめ(Task 3.1b): FIFO の待ちの途中の SIGINT の呼出履歴の最後のフレーム。"""

    HEAD = b"Traceback (most recent call last):\n" + frame(b"check_links.py", 52, "main") + b"    text = x\n"

    def test_open_frame_is_the_last_frame_of_the_linux_fixture(self):
        data = json.loads((checker.FIXTURES / "linux.json").read_text(encoding="utf-8"))
        stderr = next(case["stderr"] for case in data["tracebacks"] if case["id"] == "read-fifo-sigint")
        self.assertEqual(checker.OPEN_FRAME, checker.last_frame(stderr.encode()))

    def test_other_last_frames_fail(self):
        good = self.HEAD + frame(b"pathlib.py", 1134, "read_text") + b"    with\n" + checker.OPEN_FRAME + \
            b"\n    return\nKeyboardInterrupt\n"
        checker.check_open_frame("x", "旧版", obs(-signal.SIGINT, b"", good))
        for stderr in (self.HEAD + frame(b"pathlib.py", 1134, "read_text") + b"    with\nKeyboardInterrupt\n",
                       self.HEAD + b"KeyboardInterrupt\n",
                       self.HEAD + frame(b"pathlib.py", 1118, "open") + b"    return\nKeyboardInterrupt\n",
                       self.HEAD + frame(b"posixpath.py", 1119, "open") + b"    return\nKeyboardInterrupt\n",
                       good.replace(b"KeyboardInterrupt\n", frame(b"check_links.py", 1, "x") + b"KeyboardInterrupt\n"),
                       b"KeyboardInterrupt: " + checker.OPEN_FRAME + b"\n",
                       b"KeyboardInterrupt\n", b""):
            with self.subTest(stderr=stderr[-120:]), self.assertRaises(checker.Failure):
                checker.check_open_frame("x", "Rust ", obs(-signal.SIGINT, b"", stderr))

    def test_every_fifo_sigint_case_checks_the_frame(self):
        flagged = sorted(case.id for case in checker.all_cases()[0] if case.open_frame)
        expected = sorted([f"signal-fifo-sigint-{prefix}{mode}" for prefix in ("", "warn-")
                           for mode in ("pipe", "unbuffered", "pty")] + ["traceback:read-fifo-sigint"])
        self.assertEqual(expected, flagged)


class FixtureTests(unittest.TestCase):
    marks = [(b"/tmp/root", checker.ROOT_MARK)]

    def test_same_wrong_result_on_both_sides_fails(self):
        right = obs(1, b"ok\n", b"", [(1, b"ok\n", "full", 0, "none")])
        wrong = obs(1, b"bad\n", b"", [(1, b"bad\n", "full", 0, "none")])
        entry = checker.canonical(right, self.marks)
        checker.check_fixture(entry, right, right, None, self.marks, False, True)
        with self.assertRaises(checker.Failure):
            checker.check_fixture(entry, wrong, wrong, None, self.marks, False, True)
        with self.assertRaises(checker.Failure):
            checker.check_fixture(entry, wrong, right, None, self.marks, False, True)
        with self.assertRaises(checker.Failure):
            checker.check_fixture(None, right, right, None, self.marks, False, True)

    def test_wrong_fixed_name_line_number_and_frames_change_the_fixture_form(self):
        good = obs(1, b"", frame(b"check_links.py"), [(2, frame(b"check_links.py"), "full", 0, "none")],
                   [["check_links.py", 93, "<module>"]])
        entry = checker.canonical(good, self.marks)
        for stderr, frames in ((frame(b"pathlib.py"), [["pathlib.py", 93, "<module>"]]),
                               (frame(b"check_links.py", 94), [["check_links.py", 94, "<module>"]])):
            bad = obs(1, b"", stderr, [(2, stderr, "full", 0, "none")], frames)
            with self.assertRaises(checker.Failure):
                checker.check_fixture(entry, bad, bad, None, self.marks, False, True)

    def test_configured_expectation_is_required_both_ways(self):
        right = obs(0, b"ok\n", b"", [(1, b"ok\n", "full", 0, "none")])
        entry = checker.canonical(right, self.marks)
        with self.assertRaises(checker.Failure):
            checker.check_fixture(entry, right, right, right, self.marks, False, True)
        entry["configured"] = checker.canonical(obs(1, b"", b"E\n", [(2, b"E\n", "full", 0, "none")]), self.marks)
        with self.assertRaises(checker.Failure):
            checker.check_fixture(entry, right, right, right, self.marks, False, True)
        with self.assertRaises(checker.Failure):
            checker.check_fixture(entry, right, right, None, self.marks, False, True)

    def test_marks_replace_only_the_given_paths(self):
        data = b"/tmp/root/a.md /tmp/rootx /tmp/other"
        self.assertEqual(b"@ROOT@/a.md @ROOT@x /tmp/other", checker.apply_marks(data, self.marks))


class CaseListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases, cls.step2 = checker.all_cases()
        cls.fixture = json.loads(checker.CASES_FIXTURE.read_text(encoding="utf-8"))
        cls.corpus = checker.Case("corpus:matcher", "corpus")

    def ids(self):
        return self.fixture["case_ids"] + ["corpus:matcher"]

    def test_full_list_matches_fixture_and_required(self):
        checker.check_case_list(self.cases + [self.corpus], self.ids())
        self.assertEqual(len(self.fixture["case_ids"]) + len(self.step2["entries"]), len(self.cases))
        self.assertGreaterEqual(len(self.cases), 490)
        for case_id in checker.REQUIRED:
            self.assertIn(case_id, [case.id for case in self.cases + [self.corpus]])

    def test_missing_duplicate_empty_and_extra_cases_fail(self):
        everything = self.cases + [self.corpus]
        for required in ("legacy:basic-doc-example", "partial-rlimit-buffered-field", "signal-fifo-sigterm-warn-pty",
                         "step2:one:buffered:file:sigint", "corpus:matcher"):
            with self.subTest(required=required), self.assertRaises(checker.Failure):
                checker.check_case_list([c for c in everything if c.id != required], self.ids())
        with self.assertRaises(checker.Failure):
            checker.check_case_list(everything + [everything[0]], self.ids())
        with self.assertRaises(checker.Failure):
            checker.check_case_list([], [])
        with self.assertRaises(checker.Failure):
            checker.check_case_list(everything + [checker.Case("extra", "basic")], self.ids())
        with self.assertRaises(checker.Failure):
            checker.check_case_list(everything + [checker.Case("unknown-group", "nowhere")],
                                    self.ids() + ["unknown-group"])

    def test_every_case_without_fixture_is_order_dependent_only(self):
        without = sorted(case.id for case in self.cases if case.step2 is None and not case.fixture)
        self.assertEqual(["walk-permission-abort"], without)
        stored = set(self.fixture["cases"])
        for case in self.cases:
            if case.step2 is None and case.fixture:
                self.assertIn(case.id, stored)

    def test_inputs_have_no_case_insensitive_or_normalization_collisions(self):
        # Mac の CI の既定のファイルシステム(大文字小文字と Unicode の正規化を区別しない APFS)でも作れる入力にする。
        import unicodedata
        for case in self.cases:
            seen = {}
            for op in case.tree + case.top_tree:
                if not isinstance(op, dict):
                    continue
                path = checker.raw(op["path"])
                try:
                    key = unicodedata.normalize("NFD", path.decode("utf-8")).casefold()
                except UnicodeDecodeError:
                    key = path.lower()
                with self.subTest(case=case.id, path=path):
                    self.assertNotIn(key, seen)
                seen[key] = path

    def test_step2_table_must_cover_every_piece_and_action(self):
        checker.check_step2_coverage(self.step2)
        data = json.loads(json.dumps(self.step2))
        data["entries"] = [e for e in data["entries"]
                           if not (e["input"] == "two" and e["piece"] == "separator" and e["action"] == "cut")]
        with self.assertRaises(checker.Failure):
            checker.check_step2_coverage(data)
        data = json.loads(json.dumps(self.step2))
        data["table"][0]["outcomes"] = []
        with self.assertRaises(checker.Failure):
            checker.check_step2_coverage(data)


class StatusTests(unittest.TestCase):
    def test_skip_is_allowed_only_on_mac_for_the_closed_list(self):
        for reason in checker.CLOSED_LIST:
            self.assertEqual("不合格", checker.classify_skip(reason, False).status)
            self.assertEqual("未実施", checker.classify_skip(reason, True).status)
        self.assertEqual("不合格", checker.classify_skip("その他の理由", True).status)

    def test_final_status_refuses_failures_missing_zero_and_linux_skips(self):
        ok = checker.Outcome("合格")
        mac_skip = checker.classify_skip(checker.SKIP_DEVFULL, True)
        self.assertEqual(0, checker.final_status({"a": ok, "b": ok}, ["a", "b"], False))
        self.assertEqual(0, checker.final_status({"a": ok, "b": mac_skip}, ["a", "b"], True))
        for outcomes, ids, mac in (({"a": ok, "b": checker.Outcome("不合格", "x")}, ["a", "b"], False),
                                   ({"a": ok}, ["a", "b"], False),
                                   ({}, [], False),
                                   ({"a": mac_skip}, ["a"], True),
                                   ({"a": ok, "b": mac_skip}, ["a", "b"], False),
                                   ({"a": ok, "b": checker.Outcome("未実施", "その他")}, ["a", "b"], True)):
            with self.subTest(outcomes=outcomes, mac=mac):
                self.assertEqual(1, checker.final_status(outcomes, ids, mac))


class EnvironmentTests(unittest.TestCase):
    def test_child_environment_is_built_from_an_allow_list(self):
        polluted = {"PYTHONPATH": "/x", "PYTHONMALLOCSTATS": "1", "PYTHONHOME": "/y", "LD_LIBRARY_PATH": "/z",
                    "PYTHONIOENCODING": "ascii", "LANG": "ja_JP.UTF-8", "R4U_PYTHON": "/p"}
        with patch.dict(os.environ, polluted):
            env = checker.child_env({"PYTHONUNBUFFERED": "1"}, "/shim", "fd:9", "")
        self.assertEqual({"PATH", "LC_ALL", "PYTHONUNBUFFERED", *checker.SHIM_KEYS}, set(env))
        self.assertEqual("C", env["LC_ALL"])

    def test_out_of_scope_variables_cannot_be_given_to_children(self):
        for name in ("PYTHONMALLOCSTATS", "PYTHONPLATLIBDIR", "PYTHONPATH", "PYTHONHOME", "PYTHONWARNINGS",
                     "PYTHONDEVMODE", "PYTHONFAULTHANDLER", "PYTHONDONTWRITEBYTECODE", "__PYVENV_LAUNCHER__"):
            with self.subTest(name=name), self.assertRaises(checker.Failure):
                checker.child_env({name: "1"})

    def test_recorded_child_environment_must_match_exactly(self):
        child = checker.child_env({"PYTHONOPTIMIZE": "2"}, "/shim", "fd:9", "")
        self.assertEqual(set(), checker.check_env_names("x", image(child), child, True))
        for names in (set(child) | {"PYTHONPATH"}, set(child) - {"PYTHONOPTIMIZE"}, set()):
            with self.subTest(names=names), self.assertRaises(checker.Failure):
                checker.check_env_names("x", image(names), child, True)
        with self.assertRaises(checker.Failure):
            checker.check_env_names("x", None, child, True)
        checker.check_env_names("x", None, child, False)
        bad = dict(child, PYTHONMALLOCSTATS="1")
        with self.assertRaises(checker.Failure):
            checker.check_env_names("x", image(bad), bad, True)

    def test_failure_names_the_record_and_the_expected_set(self):
        # 次に Mac で落ちたとき、1 回で原因が分かるように、pid・実行物・期待の集合・記録の集合を出す。
        child = checker.child_env({}, "/shim", "fd:9", "")
        try:
            checker.check_env_names("case/legacy", image(set(child) | {"EXTRA"}, pid=4242, exe=b"/x/Python"), child,
                                    True, [b"/x/Python"])
        except checker.Failure as exc:
            text = str(exc)
        else:
            self.fail("余計な変数を通した")
        for part in ("余計 ['EXTRA']", "欠け []", "pid 4242", "/x/Python", "期待の集合", "記録の集合"):
            self.assertIn(part, text)
        self.assertNotIn("余計 ['LC_ALL'", text)

    def test_os_added_variable_is_excluded_only_with_every_condition(self):
        # 2026-11-12 02:41 の決定: Mac の CoreFoundation が足す __CF_USER_TEXT_ENCODING だけ。
        child = checker.child_env({}, "/shim", "fd:9", "")
        good = f"0x{os.getuid():X}:0x0:0x0"
        cf = checker.CF_ENCODING
        self.assertEqual({cf}, checker.check_env_names(
            "x", image(set(child) | {cf}, values={cf: good}), child, True, mac=True))
        refused = [
            (image(set(child) | {cf}, values={cf: good}), child, False),  # Mac でない
            (image(set(child) | {cf}, values={cf: "0x0:0x0:0x0" if os.getuid() else "0x1:0x0:0x0"}), child, True),
            (image(set(child) | {cf}, values={cf: good + "x"}), child, True),  # 形の違う値
            (image(set(child) | {cf}, values={}), child, True),  # 値の記録が無い
            (image(set(child) | {cf, "EXTRA"}, values={cf: good}), child, True),  # ほかの余計な変数
            (image(set(child) | {"__CF_OTHER"}, values={}), child, True),  # 似た名前
        ]
        for record, passed, mac in refused:
            with self.subTest(record=record, mac=mac), self.assertRaises(checker.Failure):
                checker.check_env_names("x", record, passed, True, mac=mac)
        line = checker.os_added_line({("legacy", cf, good): 3, ("rust", cf, good): 1})
        self.assertIn("旧版 3 回", line)
        self.assertIn("Rust 1 回", line)
        self.assertIn("なし", checker.os_added_line({}))


class PythonTests(unittest.TestCase):
    def info(self, **changes):
        info = {"version": "3.10.11", "dirs": [STDLIB], "sha256": dict(checker.STDLIB_SHA256),
                "stdout": ["utf-8", "surrogateescape"], "stderr": ["utf-8", "backslashreplace"],
                "environ": ["LC_ALL", "PATH"]}
        info.update(changes)
        return info

    def test_base_environment_python_is_required(self):
        self.assertEqual(frozenset(), checker.check_python(self.info()))
        for changes in ({"version": "3.11.4"}, {"version": "3.9.18"}, {"stdout": ["utf-8", "strict"]},
                        {"stdout": ["ascii", "surrogateescape"]}, {"stderr": ["utf-8", "strict"]},
                        {"environ": ["LC_ALL", "PATH", "PYTHONPATH"]}):
            with self.subTest(changes=changes), self.assertRaises(checker.Failure):
                checker.check_python(self.info(**changes))

    def test_os_added_variable_in_the_probe_only_on_mac(self):
        environ = ["LC_ALL", "PATH", checker.CF_ENCODING]
        good = f"0x{os.getuid():X}:0x0:0x0"
        self.assertEqual(frozenset(), checker.check_python(self.info(environ=environ, cf=good), mac=True))
        for info, mac in ((self.info(environ=environ, cf=good), False), (self.info(environ=environ, cf="x"), True),
                          (self.info(environ=environ + ["EXTRA"], cf=good), True)):
            with self.subTest(info=info, mac=mac), self.assertRaises(checker.Failure):
                checker.check_python(info, mac=mac)
        try:
            checker.check_python(self.info(environ=environ + ["EXTRA"], cf=good), mac=True)
        except checker.Failure as exc:
            self.assertIn("余計 ['EXTRA']", str(exc))
            self.assertIn("期待の集合 ['LC_ALL', 'PATH']", str(exc))

    def test_rlimit_behavior_is_classified_from_a_small_write(self):
        efbig = -checker.EFBIG
        self.assertEqual("partial", checker.classify_rlimit([6, 4, efbig]))
        self.assertEqual("reject", checker.classify_rlimit([6, efbig, 4]))
        for results in ([6, efbig, efbig], [6, 6, 4], [6, 4, 0], [efbig, efbig, efbig], []):
            with self.subTest(results=results), self.assertRaises(checker.Failure):
                checker.classify_rlimit(results)
        # この機械の動き(実行の最初に確かめるものと同じ)。Linux は partial だけ。
        mode = checker.probe_rlimit()
        self.assertEqual("partial", mode) if checker.LINUX else self.assertIn(mode, ("partial", "reject"))

    def test_stdlib_that_differs_is_reported_by_name(self):
        sha = dict(checker.STDLIB_SHA256, **{"posixpath.py": "0" * 64})
        self.assertEqual(frozenset({"posixpath.py"}), checker.check_python(self.info(sha256=sha)))


class PartialHelperTests(unittest.TestCase):
    fixed = (b"Traceback (most recent call last):\n" + frame(b"check_links.py") + b"    sys.exit(main())\n"
             + b"E: x\n")
    legacy = fixed.replace(b'"check_links.py"', b'"' + LEGACY + b'"')

    def test_quota_positions(self):
        field = checker.quota_position("field", self.fixed, self.legacy)
        start = self.fixed.index(b"check_links.py")
        self.assertTrue(start < field < start + len(b"check_links.py"))
        outside = checker.quota_position("outside", self.fixed, self.legacy)
        self.assertTrue(self.fixed[outside - 6:outside].startswith(b"    "))
        only = checker.quota_position("legacy-only", self.fixed, self.legacy)
        self.assertTrue(len(self.fixed) <= only < len(self.legacy))
        with self.assertRaises(checker.Failure):
            checker.quota_position("legacy-only", self.fixed, self.fixed)

    def test_event_bytes_and_locate(self):
        line, fixed_line = frame(LEGACY), frame(b"check_links.py")
        self.assertTrue(8 < checker.event_bytes("field", line, fixed_line) < 8 + len(b"check_links.py"))
        self.assertEqual(len(fixed_line) - 2, checker.event_bytes("outside", line, fixed_line))
        only = checker.event_bytes("legacy-only", line, fixed_line)
        self.assertTrue(len(fixed_line) <= only < len(line))
        self.assertEqual((2, 3), checker.locate([b"abcd", b"efgh"], 7))
        self.assertEqual((1, 0), checker.locate([b"abcd", b"efgh"], 0))
        with self.assertRaises(checker.Failure):
            checker.locate([b"abcd"], 4)
        writes = [write(2, b"x"), write(2, frame(LEGACY)), write(2, frame(LEGACY, 52))]
        self.assertEqual(2, checker.select_write(writes, checker.FILE_PREFIX, 2))
        with self.assertRaises(checker.Failure):
            checker.select_write(writes, checker.FILE_PREFIX, 3)


class RealChildTests(unittest.TestCase):
    """実際の子と共有ライブラリで、合格の対照と、拒むべき結果を確かめる。"""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.mkdtemp(prefix="r4u-selftest-")
        cls.top = tempfile.mkdtemp(prefix="r4u-top-", dir="/tmp")
        cls.shim = checker.build_shim(cls.temp)

    @classmethod
    def tearDownClass(cls):
        checker.restore_and_remove(cls.temp)
        checker.restore_and_remove(cls.top)

    def fake(self, name, before="", after=""):
        """旧版を同じプロセスで動かす偽の検査対象(--check-links-fixture を外して旧版へ渡す)。"""
        path = Path(self.temp) / name
        path.write_text(f"#!{checker.this_python()}\n" + textwrap.dedent(f"""\
            import runpy, sys
            {before}
            sys.argv = [{str(checker.LEGACY)!r}] + sys.argv[2:]
            try:
                runpy.run_path({str(checker.LEGACY)!r}, run_name="__main__")
            finally:
                {after or "pass"}
            """))
        path.chmod(stat.S_IRWXU)
        return str(path)

    def context(self, rust, legacy=None):
        work = tempfile.mkdtemp(prefix="run-", dir=self.temp)
        py = checker.Python(legacy or checker.this_python(), "3.10.11", (STDLIB,), dict(checker.STDLIB_SHA256),
                            frozenset())
        return checker.Context(py, rust, self.shim, work, self.top)

    def basic(self):
        return checker.Case("legacy:basic-doc-example", "basic", checker.BASIC, ["docs"])

    def run_pair(self, ctx, fixtures=None):
        case = self.basic()
        if fixtures is None:
            measured = self.context(ctx.rust)
            fixtures = {case.id: checker.run_case(measured, case, None, {}, measure=True)}
        return checker.run_case(ctx, case, fixtures, {})

    def test_positive_control_passes(self):
        ctx = self.context(self.fake("same"))
        checker.probe_recording(ctx)
        outcome = self.run_pair(ctx)
        self.assertEqual(("合格", True, True), (outcome.status, outcome.fixed, outcome.record))

    def test_positive_control_through_a_pseudo_terminal(self):
        # 子が終わるまで子の側の端を開いたままにして、親の側の端から読み切る(Mac で読み残しを捨てないため)。
        ctx = self.context(self.fake("same-pty"))
        case = checker.Case("x", "output", checker.BASIC, ["docs"], stdout="pty")
        measured = self.context(ctx.rust)
        fixtures = {case.id: checker.run_case(measured, case, None, {}, measure=True)}
        self.assertEqual("合格", checker.run_case(ctx, case, fixtures, {}).status)
        result = checker.execute(ctx, case, "legacy")
        self.assertTrue(result.stdout.endswith("件\r\n".encode()))

    def test_extra_output_fails(self):
        ctx = self.context(self.fake("extra", after='print("extra")'))
        with self.assertRaises(checker.Failure):
            self.run_pair(ctx)

    def test_input_change_fails(self):
        ctx = self.context(self.fake("touch", before='open("made-by-target", "w").close()'))
        with self.assertRaisesRegex(checker.Failure, "入力の木が変わった"):
            self.run_pair(ctx)

    def test_same_wrong_result_on_both_sides_fails(self):
        wrong = self.fake("wrong", before='print("wrong"); sys.exit(1)')
        right = self.context(self.fake("right"))
        case = self.basic()
        fixtures = {case.id: checker.run_case(right, case, None, {}, measure=True)}
        ctx = self.context(wrong, legacy=wrong)
        with self.assertRaisesRegex(checker.Failure, "固定期待と違う"):
            checker.run_case(ctx, case, fixtures, {})

    def test_timeout_fails(self):
        ctx = self.context(self.fake("sleep", before="import time; time.sleep(30)"))
        with patch.object(checker, "RUN_TIMEOUT", 2), self.assertRaisesRegex(checker.Failure, "時間切れ"):
            self.run_pair(ctx)

    def test_target_missing_from_the_record_fails(self):
        # Mac の /bin/sh は保護されていて共有ライブラリを読まないので、Python で何も書かない検査対象を作る。
        silent = Path(self.temp) / "silent"
        silent.write_text(f"#!{checker.this_python()}\nimport sys\nsys.exit(0)\n")
        silent.chmod(stat.S_IRWXU)
        with self.assertRaisesRegex(checker.Failure, "Rust の試しの書込みが記録に現れない"):
            checker.probe_recording(self.context(str(silent)))
        # Linux では、旧版の試しの書込みが記録に現れないことは閉じた一覧に当たらない。
        if checker.LINUX:
            with self.assertRaisesRegex(checker.Failure, "旧版の試しの書込み"):
                checker.probe_recording(self.context(self.fake("same2"), legacy=str(silent)))

    def script(self, name, text):
        path = Path(self.temp) / name
        path.write_text(text)
        path.chmod(stat.S_IRWXU)
        return str(path)

    def exec_head(self, name, target, extra=""):
        """環境をそのまま(extra があれば足して)次のプログラムへ exec する、連鎖の先頭のプログラム。"""
        return self.script(name, f"#!{checker.this_python()}\n" + textwrap.dedent(f"""\
            import os, sys
            {extra or "pass"}
            os.execv({checker.this_python()!r}, [{checker.this_python()!r}, {target!r}] + sys.argv[1:])
            """))

    def test_exec_chain_is_recorded_for_the_last_program(self):
        # 起動用のプログラムが本体を exec し直す形(同じ pid)。最後のプログラムの記録と環境で比べる。
        chain = self.exec_head("chain", self.fake("chained"))
        ctx = self.context(chain)
        checker.probe_recording(ctx)
        self.assertEqual("合格", self.run_pair(ctx).status)

    def test_launcher_variable_added_in_the_chain_fails(self):
        # Mac の起動用のプログラム(pythonw.c)と同じく、exec の前に __PYVENV_LAUNCHER__ を付ける。
        chain = self.exec_head("launcher", self.fake("launched"),
                               'os.environ["__PYVENV_LAUNCHER__"] = "/x/bin/python3.10"')
        with self.assertRaisesRegex(checker.Failure, "起動用"):
            checker.execute(self.context(chain), self.basic(), "rust")

    def test_closed_log_descriptor_after_exec_does_not_abort(self):
        # 記録の fd が close-on-exec で閉じたまま exec された本体: 共有ライブラリは abort せず、記録しない。
        closer = self.exec_head("closer", self.fake("after-cloexec"), "import fcntl; fcntl.fcntl("
                                "int(os.environ['R4U_SHIM_LOG'][3:]), fcntl.F_SETFD, fcntl.FD_CLOEXEC)")
        ctx = self.context(closer)
        result = checker.execute(ctx, self.basic(), "rust")
        self.assertEqual((1, []), (result.code, result.writes))
        self.assertIn("検査ファイル数".encode(), result.stdout)
        with self.assertRaisesRegex(checker.Failure, "記録が合わない"):
            checker.observe(ctx, self.basic(), result)

    def writer(self, name, sizes):
        """stderr(ファイル)へ、決まった大きさの write を順に出す検査対象(結果は記録で見る)。"""
        return self.script(name, f"#!{checker.this_python()}\nimport os\n"
                                 f"for data in {sizes!r}:\n    try:\n        os.write(2, data)\n"
                                 f"    except OSError:\n        pass\n")

    def test_budget_rule_rejects_only_the_crossing_write(self):
        # reject の OS の RLIMIT_FSIZE の模擬: 上限をまたぐ write だけを拒み、後の収まる write は通す。
        ctx = self.context(self.writer("budget", [b"x" * 6, b"x" * 6, b"x" * 4, b"x"]))
        case = checker.Case("budget", "fault", [], [], stderr="file")
        result = checker.execute(ctx, case, "rust", rules=f"fd=2,action=budget,bytes=10,errno={checker.EFBIG}")
        got = [(w.ret, w.errno, w.action) for w in result.writes if w.fd == 2]
        self.assertEqual([(6, 0, "budget"), (-1, checker.EFBIG, "budget-fail"), (4, 0, "budget"),
                          (-1, checker.EFBIG, "budget-fail")], got)
        self.assertEqual(b"x" * 10, result.stderr)

    def test_budget_rule_counts_mapped_paths_at_the_fixed_length(self):
        # 判定にだけ、旧版の実行環境のパスを固定名の長さで数える(書く中身は元のまま)。
        long = b"  File \"/very/long/legacy/path/check_links.py\""
        ctx = self.context(self.writer("budget-map", [long, b"x" * 2, b"y"]))
        case = checker.Case("budget-map", "fault", [], [], stderr="file")
        fixed = b'  File "check_links.py"'
        rule = (f"fd=2,action=budget,bytes={len(fixed) + 2},errno={checker.EFBIG},"
                f"map={b'/very/long/legacy/path/check_links.py'.hex()}:{b'check_links.py'.hex()}")
        result = checker.execute(ctx, case, "rust", rules=rule)
        self.assertEqual([len(long), 2, -1], [w.ret for w in result.writes if w.fd == 2])
        self.assertEqual(long + b"x" * 2, result.stderr)

    def test_reject_os_flow_builds_the_expectation_from_the_same_rule(self):
        # reject の OS の流れを、Linux で共有ライブラリの模擬だけを使って通す(実際の上限との確かめは、
        # Linux の動きが partial なので外す)。偽の検査対象は旧版そのものなので、合格の対照になる。
        # 呼出履歴を旧版と同じにするため、偽の検査対象は旧版を exec する(実行の連鎖。最後のプログラムが旧版)。
        target = self.script("reject-flow", f"#!{checker.this_python()}\n" + textwrap.dedent(f"""\
            import os, sys
            os.execv({checker.this_python()!r}, [{checker.this_python()!r}, {str(checker.LEGACY)!r}] + sys.argv[2:])
            """))
        ctx = self.context(target)
        ctx.rlimit_mode = "reject"
        case = checker.Case("partial-reject", "partial", checker.ONE, ["g"], stderr="file",
                            partial=("quota", "field"))
        with patch.object(checker, "check_rlimit_model", lambda real, model, who: None):
            measured = self.context(ctx.rust)
            measured.rlimit_mode = "reject"
            fixtures = {case.id: checker.run_case(measured, case, None, {}, measure=True)}
            outcome = checker.run_case(ctx, case, fixtures, {})
        self.assertEqual("合格", outcome.status)
        self.assertGreater(len(ctx.norm.seen), 0)
        with patch.object(checker, "check_rlimit_model", lambda real, model, who: None):
            # 欄の途中の上限: 固定名の長さで数えても、上限をまたぐ write が拒まれる。
            expected, params, _ = checker.partial_expectation(ctx, case)
            self.assertIn("budget-fail", [w[4] for w in expected.writes])
            self.assertIn(",map=", params["rules"])
            # 旧版だけが切れる範囲: 固定名の長さで数えると拒まれない(元の長さで数えると拒まれる)。
            only = checker.replace(case, partial=("quota", "legacy-only"))
            expected, params, _ = checker.partial_expectation(ctx, only)
            self.assertNotIn("budget-fail", [w[4] for w in expected.writes])
            plain = params["rules"].split(",map=")[0]
            unmapped = checker.execute(ctx, only, "legacy", rules=plain)
            self.assertIn("budget-fail", [w.action for w in unmapped.writes])

    def test_rlimit_model_mismatch_fails(self):
        real = checker.Result(1, b"", b"ab", [write(2, b"ab"), write(2, b"cd", ret=-1, errno=checker.EFBIG)])
        model = checker.Result(1, b"", b"ab", [write(2, b"ab"), write(2, b"cd", ret=-1, errno=checker.EFBIG,
                                                                      action="budget-fail")])
        checker.check_rlimit_model(real, model, "旧版")
        other = checker.Result(1, b"", b"abc", [write(2, b"ab"), write(2, b"cd", ret=1)])
        with self.assertRaisesRegex(checker.Failure, "模擬"):
            checker.check_rlimit_model(real, other, "旧版")

    def mac_cases(self):
        with patch.object(checker, "OS_NAME", "macos"):
            return checker.load_fixture_document(checker.os_fixture_path())["cases"]

    def test_mac_fixture_is_used_as_the_third_party(self):
        # OS に依らない入力の Mac の固定期待で、旧版・固定期待・Rust の 3 者の比較の経路を Linux で通す。
        case = next(c for c in checker.all_cases()[0] if c.id == "legacy:basic-doc-example")
        ctx = self.context(self.fake("mac-third"))
        fixtures = json.loads(json.dumps(self.mac_cases()))
        outcome = checker.run_case(ctx, case, fixtures, {})
        self.assertEqual(("合格", True), (outcome.status, outcome.fixed))
        for key, value in (("exit", 0), ("stdout", "検査ファイル数: 1\n"), ("writes", {"count": 1, "sha256": "0" * 64})):
            changed = json.loads(json.dumps(fixtures))
            changed[case.id][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(checker.Failure, "固定期待と違う"):
                checker.run_case(self.context(ctx.rust), case, changed, {})

    def test_mac_step2_fixture_is_used_as_the_third_party(self):
        # 手順 2 の Mac の経路(規則を旧版から作り直し、cases-macos.json と比べる)を、OS の印だけ Mac にして通す。
        case = next(c for c in checker.all_cases()[0] if c.id == "step2:one:buffered:file:sigint")
        target = self.script("mac-step2", f"#!{checker.this_python()}\n" + textwrap.dedent(f"""\
            import os, sys
            os.execv({checker.this_python()!r}, [{checker.this_python()!r}, {str(checker.LEGACY)!r}] + sys.argv[2:])
            """))
        fixtures = json.loads(json.dumps(self.mac_cases()))
        with patch.object(checker, "MAC", True), patch.object(checker, "LINUX", False):
            self.assertTrue(checker.check_step2(self.context(target), case, {}, fixtures))
            changed = json.loads(json.dumps(fixtures))
            changed[case.id]["exit"] = 0
            with self.assertRaisesRegex(checker.Failure, "手順 2 の固定期待と違う"):
                checker.check_step2(self.context(target), case, {}, changed)

    def interrupted(self, name, last):
        """呼出履歴(最後のフレームが last)と KeyboardInterrupt を stderr へ書き、SIGINT で終わる検査対象。"""
        lines = [b"Traceback (most recent call last):\n", frame(b"check_links.py", 52, "main"), b"    text = x\n",
                 last + b"\n", b"    return\n", b"KeyboardInterrupt\n"]
        return self.script(name, f"#!{checker.this_python()}\n" + textwrap.dedent(f"""\
            import os, signal
            for data in {lines!r}:
                os.write(2, data)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)
            """))

    def test_fifo_sigint_with_another_last_frame_fails(self):
        # 比べる欄(終了値・stdout・最後の行・stdout の書込みの記録)は同じでも、最後のフレームが違えば不合格。
        good = self.interrupted("frame-good", checker.OPEN_FRAME)
        early = self.interrupted("frame-early", frame(b"pathlib.py", 1134, "read_text")[:-1])
        case = checker.Case("fifo-frame", "signal", [], [], interrupt=True, open_frame=True)
        self.assertEqual("合格", checker.run_case(self.context(good, legacy=good), case, None, {}).status)
        with self.assertRaisesRegex(checker.Failure, "旧版の呼出履歴の最後のフレーム"):
            checker.run_case(self.context(good, legacy=early), case, None, {})
        with self.assertRaisesRegex(checker.Failure, "旧版の呼出履歴の最後のフレーム"):
            checker.run_case(self.context(good, legacy=early), case, None, {}, measure=True)
        with self.assertRaisesRegex(checker.Failure, "Rust の呼出履歴の最後のフレーム"):
            checker.run_case(self.context(early, legacy=good), case, None, {})
        # 確かめを付けないケースでは、同じ結果を合格にする(比べる欄は 22:35 の決定のまま)。
        plain = checker.replace(case, open_frame=False)
        self.assertEqual("合格", checker.run_case(self.context(early, legacy=good), plain, None, {}).status)

    def test_missing_cases_are_measured_from_the_legacy_and_fail(self):
        import contextlib
        import io
        listed = self.basic()
        new = checker.Case("new-case", "basic", checker.BASIC, ["docs", "missing"])
        reference = self.context(self.fake("reference"))
        fixtures = {listed.id: checker.run_case(reference, listed, None, {}, measure=True)}
        want = checker.run_case(self.context(self.fake("reference2")), new, None, {}, measure=True)
        for name, rust in (("same-missing", self.fake("same-missing")),
                           # Rust の出力が違っても、足す値は旧版だけから作る。
                           ("extra-missing", self.fake("extra-missing", after='print("extra")'))):
            with self.subTest(name=name):
                ctx = self.context(rust)
                with contextlib.redirect_stdout(io.StringIO()):
                    outcomes, _ = checker.compare_each(ctx, [listed, new], fixtures, [new.id])
                    status = checker.overall_status(checker.final_status(outcomes, [listed.id, new.id], False),
                                                    fixtures, checker.FIXTURES / "cases-macos.json", [new.id])
                if name == "same-missing":
                    self.assertEqual(("合格", True), (outcomes[listed.id].status, outcomes[listed.id].fixed))
                self.assertEqual("不合格", outcomes[new.id].status)
                self.assertIn("固定期待に無いケース", outcomes[new.id].message)
                self.assertEqual(1, status)
                doc = checker.additions_document(python(), [listed.id, new.id], [new.id], ctx.measured, outcomes,
                                                 True)
                self.assertEqual(({new.id: want}, [new.id]), (doc["cases"], doc[checker.ADDED_KEY]))

    def test_out_of_scope_variable_reaching_the_child_fails(self):
        ctx = self.context(self.fake("same3"))
        original = checker.child_env

        def leaky(case_env, shim=None, log=None, rules=""):
            env = original(case_env, shim, log, rules)
            env["PYTHONMALLOCSTATS"] = "1"
            return env
        with patch.object(checker, "child_env", leaky), self.assertRaisesRegex(checker.Failure, "前提の外"):
            checker.execute(ctx, self.basic(), "rust")


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(f"R4U の比較器の自己検査: {result.testsRun} 件・失敗 {len(result.failures) + len(result.errors)} 件・"
          f"skip {len(result.skipped)} 件")
    if not result.wasSuccessful() or result.testsRun == 0 or result.skipped or result.expectedFailures \
            or result.unexpectedSuccesses:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
