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
        writes, names, chain = checker.read_log(b'{"env":["50415448"],"pid":7,"exe":"2f62"}\n{"fd":1,"len":1,'
                                                b'"ret":1,"errno":0,"action":"none","data":"61"}\n')
        self.assertEqual(({"PATH"}, [write(1, b"a")], [b"/b"]), (names, writes, chain))

    def test_exec_chain_uses_the_last_program_and_refuses_other_shapes(self):
        line = b'{"fd":1,"len":1,"ret":1,"errno":0,"action":"none","data":"61"}\n'
        first = b'{"env":["50415448","58"],"pid":7,"exe":"2f61"}\n'
        last = b'{"env":["50415448"],"pid":7,"exe":"2f62"}\n'
        writes, names, chain = checker.read_log(first + last + line)
        self.assertEqual(({"PATH"}, [b"/a", b"/b"], 1), (names, chain, len(writes)))
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
        with self.assertRaisesRegex(checker.Failure, "起動用"):
            checker.check_env_names("x", set(child) | {checker.LAUNCHER_VARIABLE}, child, True, [b"/bin/python3.10"])
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
        checker.check_env_names("x", set(child), child, True)
        for names in (set(child) | {"PYTHONPATH"}, set(child) - {"PYTHONOPTIMIZE"}, set()):
            with self.subTest(names=names), self.assertRaises(checker.Failure):
                checker.check_env_names("x", names, child, True)
        with self.assertRaises(checker.Failure):
            checker.check_env_names("x", None, child, True)
        checker.check_env_names("x", None, child, False)
        bad = dict(child, PYTHONMALLOCSTATS="1")
        with self.assertRaises(checker.Failure):
            checker.check_env_names("x", set(bad), bad, True)


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
        silent = Path(self.temp) / "silent"
        silent.write_text("#!/bin/sh\nexit 0\n")
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
