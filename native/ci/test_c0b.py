#!/usr/bin/env python3
"""C0b 比較器に不正な結果を与え、同じ失敗も合格にしないことを確かめる。"""
import copy
import errno
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# check_c0b が __main__ で走る場合も、検査対象は同じ実ファイルに固定する。
spec = importlib.util.spec_from_file_location("c0b_checker", Path(__file__).with_name("check_c0b.py"))
checker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = checker
spec.loader.exec_module(checker)
from support_native import RunResult, run_command


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.good = checker.expected("sample", b"a\0\xff", items=1, count=3)

    def run_result(self, value=None, **changes):
        fields = dict(returncode=0, stdout=checker.encode(self.good if value is None else value), stderr=b"", timed_out=False)
        fields.update(changes)
        return RunResult(**fields)

    def rejects(self, value):
        with self.assertRaises((ValueError, TypeError)):
            checker.verify_run(self.run_result(value), "sample", self.good)

    def test_valid_raw_success_and_observed_refusal(self):
        self.assertEqual(checker.verify_run(self.run_result(), "sample", self.good), self.good)
        refusal = checker.expected("sample", kind="io", code=errno.EACCES, items=1, count=2)
        self.assertEqual(checker.verify_run(self.run_result(refusal), "sample", refusal), refusal)

    def test_wrong_body_and_consumption_and_deadline(self):
        for key, value in [("body_hex", "00"), ("deadline_unchanged", False), ("case", "another")]:
            bad = copy.deepcopy(self.good)
            bad[key] = value
            self.rejects(bad)
        for key in ["items", "bytes"]:
            bad = copy.deepcopy(self.good)
            bad["usage"][key] += 1
            self.rejects(bad)

    def test_identical_errors_do_not_pass_success_expectation(self):
        bad = checker.expected("sample", kind="items", items=1)
        result = self.run_result(bad)
        comparison = SimpleNamespace(legacy=result, native=result, matches=True)
        with self.assertRaises(ValueError):
            checker.verify_pair(comparison, "sample", self.good)

    def test_different_kind_errno_and_stage_are_not_normalized(self):
        wanted = checker.expected("sample", kind="io", code=errno.EACCES, items=1)
        for error in [{"kind": "items", "errno": None}, {"kind": "io", "errno": errno.ENOENT}]:
            bad = copy.deepcopy(wanted)
            bad["error"] = error
            with self.assertRaises(ValueError):
                checker.verify_run(self.run_result(bad), "sample", wanted)
        old = checker.load_legacy()
        item = next(x for x in checker.cases() if x.name == "tick-items-5")
        with patch.object(checker, "load_legacy", return_value=old), patch.object(old.Budget, "tick", side_effect=old.Stop("未知の停止")):
            with self.assertRaisesRegex(RuntimeError, "未知の Stop"):
                checker.legacy_record(item.name, checker.normalized_plan(item))
        with patch.object(checker, "load_legacy", return_value=old), patch.object(old.Budget, "tick", side_effect=OSError(errno.EACCES, "wrong stage")):
            with self.assertRaises(OSError):
                checker.legacy_record(item.name, checker.normalized_plan(item))

    def test_missing_and_unknown_keys_at_every_object(self):
        for nested in [None, "usage", "error"]:
            base = self.good if nested != "error" else checker.expected("sample", kind="items")
            for action in ["missing", "unknown"]:
                bad = copy.deepcopy(base)
                target = bad if nested is None else bad[nested]
                if action == "missing":
                    target.pop(next(iter(target)))
                else:
                    target["unexpected"] = 0
                self.rejects(bad)

    def test_duplicate_keys_at_every_object(self):
        for raw in [b'{"case":"sample","case":"sample"}',
                    checker.encode(self.good).replace(b'"items":1', b'"items":1,"items":1'),
                    checker.encode(checker.expected("sample", kind="items")).replace(b'"errno":null', b'"errno":null,"errno":null')]:
            with self.assertRaises(ValueError):
                checker.observation(self.run_result(stdout=raw), "sample")

    def test_usage_rejects_bool_float_negative_string(self):
        for value in [True, 1.0, -1, "1", None, [], {}]:
            for key in ["items", "bytes"]:
                bad = copy.deepcopy(self.good)
                bad["usage"][key] = value
                self.rejects(bad)

    def test_body_format_and_success_error_consistency(self):
        for body in ["a", "FF", "gg", None, [], 4]:
            bad = copy.deepcopy(self.good)
            bad["body_hex"] = body
            self.rejects(bad)
        bad = copy.deepcopy(self.good)
        bad["error"] = {"kind": "items", "errno": None}
        self.rejects(bad)
        bad = checker.expected("sample", kind="items")
        bad["body_hex"] = ""
        self.rejects(bad)

    def test_invalid_kind_errno_outcome_and_boolean(self):
        for kind, code in [("unknown", None), ([], None), ("io", None), ("io", 0),
                           ("io", -1), ("io", True), ("io", 1.5), ("items", 1)]:
            bad = checker.expected("sample", kind="items")
            bad["error"] = {"kind": kind, "errno": code}
            self.rejects(bad)
        for key, value in [("outcome", "unknown"), ("deadline_unchanged", 1), ("deadline_unchanged", "true")]:
            bad = copy.deepcopy(self.good)
            bad[key] = value
            self.rejects(bad)

    def test_malformed_json_extra_records_and_trailing_text(self):
        for raw in [b"", b"{", b"null", b"[]", b"\xff", checker.encode(self.good) + b"{}\n", checker.encode(self.good) + b"extra", checker.encode(self.good).rstrip(b"\n"), checker.encode(self.good) + b"\n"]:
            with self.assertRaises((ValueError, UnicodeError)):
                checker.observation(self.run_result(stdout=raw), "sample")

    def test_returncode_timeout_and_stderr_each_refuse(self):
        for changes in [dict(returncode=1), dict(returncode=-9), dict(timed_out=True), dict(stderr=b"warning")]:
            with self.assertRaises(ValueError):
                checker.verify_run(self.run_result(**changes), "sample", self.good)

    def test_actual_child_failure_timeout_and_start_failure(self):
        with tempfile.TemporaryDirectory(prefix="c0b-selftest-") as directory:
            cwd = Path(directory).resolve(strict=True)
            for code, timeout in [("raise SystemExit(3)", 2), ("import time; time.sleep(60)", 0.05),
                                  ("import sys; sys.stderr.write('unexpected')", 2)]:
                result = run_command([sys.executable, "-B", "-c", code], cwd=cwd, timeout=timeout)
                with self.assertRaises(ValueError):
                    checker.verify_run(result, "sample", self.good)
            with self.assertRaises(OSError):
                run_command([str(cwd / "absent")], cwd=cwd, timeout=1)

    def test_pair_rejects_unexpected_side_effect_even_with_valid_output(self):
        run = self.run_result()
        comparison = SimpleNamespace(legacy=run, native=run, matches=False, differences=("tree:extra",))
        with self.assertRaises(ValueError):
            checker.verify_pair(comparison, "sample", self.good)


class InputAttributeTests(unittest.TestCase):
    def item(self, name="ascii"):
        return next(item for item in checker.cases() if item.name == name)

    def test_long_socket_path_reaches_exact_legacy_refusal(self):
        with tempfile.TemporaryDirectory(prefix="c0b-long-socket-") as temporary:
            root = Path(temporary).resolve(strict=True)
            cwd = root / ("long-" * 20) / ("nested-" * 10)
            cwd.mkdir(parents=True)
            item = self.item("socket-file")
            checker.seed(cwd, item)
            self.assertGreater(len(os.fsencode(cwd / "input/file")), 200)
            result = run_command(
                [sys.executable, "-B", checker.__file__, "--legacy-child", item.name],
                cwd=cwd, input=checker.encode(checker.normalized_plan(item)), timeout=10)
            checker.verify_run(result, item.name, item.result)
            self.assertEqual((cwd / "input/file").read_bytes(), b"old")

    def test_mtime_only_change_is_not_hidden_by_content_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="c0b-mtime-") as temporary:
            root = Path(temporary).resolve(strict=True)
            item = self.item()
            checker.seed(root, item)
            content = checker.snapshot_tree(root)
            before = checker.snapshot_attributes(root)
            path = root / "input/file"
            old = path.stat()
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
            self.assertEqual(checker.snapshot_tree(root), content)
            with self.assertRaisesRegex(ValueError, r"input/file.*mtime_ns"):
                checker.check_attribute_effects(before, checker.snapshot_attributes(root), item)

    def test_same_bytes_mode_and_mtime_on_another_inode_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="c0b-inode-") as temporary:
            root = Path(temporary).resolve(strict=True)
            item = self.item()
            checker.seed(root, item)
            content = checker.snapshot_tree(root)
            before = checker.snapshot_attributes(root)
            path = root / "input/file"
            replacement = path.with_name("replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(path.stat().st_mode)
            os.utime(replacement, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
            replacement.replace(path)
            after = checker.snapshot_attributes(root)
            self.assertNotEqual(before["input/file"]["ino"], after["input/file"]["ino"])
            self.assertEqual(checker.snapshot_tree(root), content)
            with self.assertRaisesRegex(ValueError, r"input/file.*ino"):
                checker.check_attribute_effects(before, after, item)

    def test_regular_read_and_atime_do_not_mask_other_six_attributes(self):
        with tempfile.TemporaryDirectory(prefix="c0b-attributes-") as temporary:
            root = Path(temporary).resolve(strict=True)
            item = self.item()
            checker.seed(root, item)
            before = checker.snapshot_attributes(root)
            self.assertEqual((root / "input/file").read_bytes(), b"old")
            checker.check_attribute_effects(before, checker.snapshot_attributes(root), item)
            for field in checker.ATTRIBUTE_KEYS:
                with self.subTest(field=field):
                    after = copy.deepcopy(before)
                    after["input/file"][field] += 1
                    with self.assertRaisesRegex(ValueError, field):
                        checker.check_attribute_effects(before, after, item)

    def test_intended_content_change_does_not_allow_identity_change(self):
        with tempfile.TemporaryDirectory(prefix="c0b-intended-") as temporary:
            root = Path(temporary).resolve(strict=True)
            item = self.item("grow")
            checker.seed(root, item)
            before = checker.snapshot_attributes(root)
            (root / "input/file").write_bytes(b"longer")
            after = checker.snapshot_attributes(root)
            checker.check_attribute_effects(before, after, item)
            after["input/file"]["ino"] += 1
            with self.assertRaisesRegex(ValueError, "ino"):
                checker.check_attribute_effects(before, after, item)

    def test_child_checks_attributes_before_permission_restoration(self):
        with tempfile.TemporaryDirectory(prefix="c0b-child-attributes-") as temporary:
            root = Path(temporary).resolve(strict=True)
            for name in ("ascii", "denied-file"):
                for mutation in ("none", "mtime", "inode"):
                    with self.subTest(case=name, mutation=mutation):
                        cwd = root / (name + "-" + mutation)
                        cwd.mkdir()
                        item = self.item(name)
                        checker.seed(cwd, item)
                        # 固定した正しい JSON を返しても、入力を変えた接続は拒否する。
                        script = root / "false-fixture.py"
                        script.write_text(
                            f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
                            "path = Path('input/file')\nold = path.stat()\n"
                            + ("os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000000))\n"
                               if mutation == "mtime" else
                               "new = path.with_name('replacement')\nnew.write_bytes(b'old')\n"
                               "new.chmod(old.st_mode)\nos.utime(new, ns=(old.st_atime_ns, old.st_mtime_ns))\n"
                               "new.replace(path)\n" if mutation == "inode" else "")
                            + f"sys.stdout.buffer.write({checker.encode(item.result)!r})\n")
                        script.chmod(0o755)
                        result = run_command(
                            [sys.executable, "-B", checker.__file__, "--native-child", str(script), name],
                            cwd=cwd, input=checker.encode(checker.normalized_plan(item)), timeout=10)
                        if mutation == "none":
                            checker.verify_run(result, name, item.result)
                        else:
                            self.assertNotEqual(result.returncode, 0)
                            self.assertEqual(result.stdout, b"")
                            self.assertIn("予定外の属性変更".encode(), result.stderr)


if __name__ == "__main__":
    unittest.main()
