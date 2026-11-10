"""task-digest 比較器が誤値・想定外失敗・未実行を拒むことを検査する。"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_task_digest as checker


class CheckerTests(unittest.TestCase):
    def good(self):
        return checker.Completed(0, b"sha256:21eefa756f838e54\n", b"")

    def test_each_raw_result_field_is_required(self):
        expected = (0, self.good().stdout, b"")
        for value in (checker.Completed(1, b"", b"same failure\n"),
                      checker.Completed(0, b"sha256:0000000000000000\n", b""),
                      checker.Completed(0, self.good().stdout + b"\n", b""),
                      checker.Completed(0, self.good().stdout, b"extra"),
                      checker.Completed(-15, b"", b""),
                      checker.Completed(None, b"", b""),
                      checker.Completed(0, self.good().stdout, b"", True)):
            with self.subTest(value=value), self.assertRaises(checker.Failure):
                checker.check_result("fault", value, expected)

    def test_same_wrong_result_on_both_sides_fails(self):
        comparison = object.__new__(checker.Comparison)
        comparison.binary = Path(sys.executable)
        for value in (checker.Completed(1, b"", b"same failure"),
                      checker.Completed(0, b"same wrong digest\n", b""),
                      checker.Completed(None, b"", b"", True)):
            with patch.object(checker, "run", return_value=value), self.assertRaises(checker.Failure):
                comparison.both("both-wrong", "cli", ["-"], (0, self.good().stdout, b""))

    def test_one_side_timeout_and_both_timeouts_fail(self):
        comparison = object.__new__(checker.Comparison)
        comparison.binary = Path(sys.executable)
        timeout = checker.Completed(None, b"", b"", True)
        for values in ([timeout, self.good()], [self.good(), timeout], [timeout, timeout]):
            with patch.object(checker, "run", side_effect=values), self.assertRaises(checker.Failure):
                comparison.both("timeout", "cli", ["-"], (0, self.good().stdout, b""))

    def test_json_fields_have_strict_values_and_types(self):
        expected = {"normalized_hex": "6162", "digest": "sha256:0123456789abcdef", "error": None}
        wrong = [dict(expected, normalized_hex="6163"), dict(expected, digest="sha256:0000000000000000"),
                 dict(expected, error="unexpected"), {"digest": expected["digest"]}, dict(expected, extra=0)]
        for record in wrong:
            with self.subTest(record=record), self.assertRaises(checker.Failure):
                checker.check_record("wrong", checker.Completed(0, (json.dumps(record) + "\n").encode(), b""), expected)
        for wrong_type in (False, 0.0, "0", None):
            got = checker.Completed(0, (json.dumps({"code": wrong_type}) + "\n").encode(), b"")
            with self.subTest(wrong_type=wrong_type), self.assertRaises(checker.Failure):
                checker.check_record("type", got, {"code": 0})
        for wrong_type in (True, 1.0, "1"):
            got = checker.Completed(0, (json.dumps({"changed": wrong_type}) + "\n").encode(), b"")
            with self.subTest(wrong_type=wrong_type), self.assertRaises(checker.Failure):
                checker.check_record("type", got, {"changed": 1})

    def test_json_duplicate_keys_and_extra_bytes_fail(self):
        for output in (b'{"code":1,"code":0}\n', b'{"code":0,"other":{"x":1,"x":1}}\n',
                       b' {"code":0}\n', b'{"code":0}\n\n', b'{"code":0} \n',
                       b'{\n"code":0}\n', b'{"code":0}', b'{"code":0}\nextra',
                       b'{"code":0}\n{}\n', b'\xff\n', b'null\n'):
            with self.subTest(output=output), self.assertRaises(checker.Failure):
                checker.check_record("json", checker.Completed(0, output, b""), {"code": 0})
        checker.check_record("valid", checker.Completed(0, b'{"code": 0}\n', b""), {"code": 0})

    def test_record_nonzero_timeout_and_stderr_fail(self):
        for value in (checker.Completed(1, b'{"code":0}\n', b""),
                      checker.Completed(0, b'{"code":0}\n', b"extra"),
                      checker.Completed(None, b'{"code":0}\n', b"", True)):
            with self.assertRaises(checker.Failure):
                checker.check_record("record", value, {"code": 0})

    def test_zero_and_duplicate_cases_fail(self):
        sample = checker.Case("a", b"", "", None, "sha256:e3b0c44298fc1c14")
        for values in ([], [sample, sample]):
            with self.assertRaises(checker.Failure):
                checker.require_cases(values)

    def test_process_timeout_and_spawn_failure_fail(self):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            got = checker.run(["fixture"])
            self.assertTrue(got.timed_out)
            with self.assertRaises(checker.Failure):
                checker.check_result("timeout", got, (0, b"", b""))
        with patch.object(subprocess, "run", side_effect=FileNotFoundError(errno.ENOENT, "missing")):
            with self.assertRaises(checker.Failure):
                checker.run(["missing"])

    def test_missing_native_and_legacy_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(checker.Failure):
                checker.Comparison(root / "missing")
            inaccessible = root / "not-executable"
            inaccessible.write_bytes(b"x")
            inaccessible.chmod(0o600)
            with self.assertRaises(checker.Failure):
                checker.Comparison(inaccessible)
            with patch.object(checker, "LEGACY", root / "missing-legacy"):
                with self.assertRaises(checker.Failure):
                    checker.Comparison(sys.executable)

    def test_input_content_mode_and_links_cannot_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "input"
            path.write_bytes(b"body")
            before = checker.snapshot(root)
            path.write_bytes(b"changed")
            with self.assertRaises(checker.Failure):
                checker.check_unchanged("content", before, checker.snapshot(root))
            before = checker.snapshot(root)
            path.chmod(0o400)
            with self.assertRaises(checker.Failure):
                checker.check_unchanged("mode", before, checker.snapshot(root))
            path.chmod(0o600)
            link = root / "link"
            link.symlink_to("input")
            before = checker.snapshot(root)
            link.unlink()
            link.symlink_to("other")
            with self.assertRaises(checker.Failure):
                checker.check_unchanged("link", before, checker.snapshot(root))

    def test_input_modification_is_checked_after_each_side(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "input"
            path.write_bytes(b"body")
            comparison = object.__new__(checker.Comparison)
            comparison.binary = Path(sys.executable)
            def modified(*args, **kwargs):
                path.write_bytes(b"changed")
                return self.good()
            with patch.object(checker, "run", side_effect=modified), self.assertRaises(checker.Failure):
                comparison.both("modified", "cli", [str(path)], (0, self.good().stdout, b""), root=root)

    def test_runtime_does_not_silently_skip_python_or_permissions(self):
        with patch.object(sys, "version_info", (3, 11)), self.assertRaises(checker.Failure):
            checker.runtime()
        with patch.object(sys, "version_info", (3, 10)), patch.object(os, "geteuid", return_value=0):
            with self.assertRaises(checker.Failure):
                checker.runtime()

    def test_existing_inputs_and_fixed_values_are_required(self):
        old, methods, fixed = checker.load_existing(checker.ROOT)
        self.assertEqual(37, len(methods))
        self.assertEqual(16, sum(map(len, fixed.values())))
        selected, external = checker.cases(checker.ROOT)
        checker.require_cases(selected)
        base = next(case for case in selected if case.name == "base")
        self.assertEqual(old.BASE.encode(), base.data)
        self.assertEqual("sha256:21eefa756f838e54", base.digest)
        self.assertEqual(6, len(external))
        record = next(case for case in selected if case.name == "record-only")
        self.assertEqual("", record.body)
        self.assertEqual("sha256:e3b0c44298fc1c14", record.digest)

    def race_snapshots(self, root):
        place = root / "race"
        place.mkdir()
        (place / "input").write_bytes(b"original")
        (place / "replacement").write_bytes(b"replacement")
        (root / "unrelated").write_bytes(b"keep")
        before = checker.snapshot(root)
        (place / "input").rename(place / "opened-old")
        (place / "replacement").rename(place / "input")
        boundary = checker.snapshot(root)
        return place, before, boundary

    def test_race_allows_only_rename_at_the_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            place, before, boundary = self.race_snapshots(root)
            checker.check_rename_boundary("rename", before, boundary, b"race")
            # renameの直後に元実体だけをchmodする不具合も見逃さない。
            (place / "opened-old").chmod(0o400)
            with self.assertRaises(checker.Failure):
                checker.check_rename_boundary("chmod-old", before, checker.snapshot(root), b"race")
            with self.assertRaises(checker.Failure):
                checker.check_unchanged("chmod-after-boundary", boundary, checker.snapshot(root))

    def test_race_refuses_original_replacement_and_unrelated_mutations(self):
        for path in (b"race/opened-old", b"race/input", b"unrelated"):
            for index in range(6):
                with self.subTest(path=path, index=index), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _, before, boundary = self.race_snapshots(root)
                    changed = boundary.copy()
                    attributes, body = changed[path]
                    attributes = list(attributes)
                    attributes[index] += 1
                    changed[path] = (tuple(attributes), body)
                    # 移した実体のctimeは境界で一度採用するが、その後の変更は拒否する。
                    if index != 5 or path == b"unrelated":
                        with self.assertRaises(checker.Failure):
                            checker.check_rename_boundary("attribute", before, changed, b"race")
                    with self.assertRaises(checker.Failure):
                        checker.check_unchanged("after-boundary", boundary, changed)
            with self.subTest(path=path, body=True), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, before, boundary = self.race_snapshots(root)
                changed = boundary.copy()
                changed[path] = (changed[path][0], b"changed")
                with self.assertRaises(checker.Failure):
                    checker.check_rename_boundary("body", before, changed, b"race")

    def test_race_parent_and_unexpected_paths_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, before, boundary = self.race_snapshots(root)
            changed = boundary.copy()
            attributes, body = changed[b"race"]
            changed[b"race"] = ((attributes[0], attributes[1], attributes[2] ^ 0o100,
                                  *attributes[3:]), body)
            with self.assertRaises(checker.Failure):
                checker.check_rename_boundary("parent-mode", before, changed, b"race")
            for path_change in ("missing", "extra"):
                changed = boundary.copy()
                if path_change == "missing":
                    del changed[b"race/opened-old"]
                else:
                    changed[b"extra"] = boundary[b"unrelated"]
                with self.assertRaises(checker.Failure):
                    checker.check_rename_boundary(path_change, before, changed, b"race")

    def test_normal_environment_fixes_buffering_and_columns_explicitly(self):
        with patch.dict(os.environ, {"PYTHONUNBUFFERED": "1", "COLUMNS": "999", "PYTHONINTMAXSTRDIGITS": "640"}):
            normal = checker.Comparison.environment()
            self.assertNotIn("PYTHONUNBUFFERED", normal)
            self.assertNotIn("PYTHONINTMAXSTRDIGITS", normal)
            self.assertEqual("80", normal["COLUMNS"])
            terminal = checker.Comparison.environment(columns=None)
            self.assertNotIn("COLUMNS", terminal)


    def special_results(self, case_id, encoding):
        # 比較器が生成する期待値とは別に、承認された例外経路と診断を固定する。
        if case_id == checker.SPECIAL_EMPTY_STDIN:
            line, source = 114, 'result = digest(data.decode("utf-8"))'
            error = "AttributeError: 'NoneType' object has no attribute 'decode'"
            message = "算出できない: 非同期の標準入力にデータがない\n"
        elif case_id == checker.SPECIAL_CLOSED_STDOUT:
            line, source = 124, "print(result)"
            error = "BrokenPipeError: [Errno 32] Broken pipe"
            message = "算出できない: 標準出力のパイプが閉じている\n"
        elif case_id == checker.SPECIAL_FULL_STDOUT:
            line, source = 124, "print(result)"
            error = "OSError: [Errno 28] No space left on device"
            message = "算出できない: 標準出力の空き容量がない\n"
        else:
            raise AssertionError(case_id)
        trace = ("Traceback (most recent call last):\n"
                 f'  File "{checker.LEGACY}", line 129, in <module>\n'
                 "    sys.exit(main())\n"
                 f'  File "{checker.LEGACY}", line {line}, in main\n'
                 f"    {source}\n{error}\n")
        codec = "ascii" if encoding == "ascii:strict" else "utf-8"
        return (checker.Completed(1, b"", trace.encode(codec, "backslashreplace")),
                checker.Completed(1, b"", message.encode(codec, "backslashreplace")))

    def test_special_expectations_fix_both_sides_and_encodings(self):
        for case_id in checker.SPECIAL_IDS:
            for encoding in checker.SPECIAL_ENCODINGS:
                with self.subTest(case_id=case_id, encoding=encoding):
                    old, native = self.special_results(case_id, encoding)
                    expected = ((old.code, old.stdout, old.stderr),
                                (native.code, native.stdout, native.stderr))
                    self.assertEqual(expected, checker.special_expectations(case_id, encoding))
                    checker.check_special_pair(case_id, old, native, encoding)
                    if encoding == "ascii:strict":
                        self.assertTrue(native.stderr.isascii())
                        self.assertIn(b"\\u7b97", native.stderr)

    def test_special_pair_refuses_each_wrong_field_on_either_side(self):
        for case_id in checker.SPECIAL_IDS:
            for encoding in checker.SPECIAL_ENCODINGS:
                valid = self.special_results(case_id, encoding)
                for side in (0, 1):
                    original = valid[side]
                    wrong = (
                        checker.Completed(0, b"", original.stderr),
                        checker.Completed(120, b"", original.stderr),
                        checker.Completed(True, b"", original.stderr),
                        checker.Completed(1, b"extra", original.stderr),
                        checker.Completed(1, b"", original.stderr + b"extra\n"),
                        checker.Completed(1, b"", original.stderr.rstrip(b"\n")),
                        checker.Completed(1, b"", b"unrelated failure\n"),
                        checker.Completed(None, b"", original.stderr),
                        checker.Completed(1, b"", original.stderr, True),
                    )
                    for observed in wrong:
                        pair = list(valid)
                        pair[side] = observed
                        with self.subTest(case_id=case_id, encoding=encoding, side=side, observed=observed):
                            with self.assertRaises(checker.Failure):
                                checker.check_special_pair(case_id, *pair, encoding)

    def test_special_pair_refuses_equal_failures_and_wrong_exception_routes(self):
        for case_id in checker.SPECIAL_IDS:
            old, native = self.special_results(case_id, "utf-8:strict")
            for same in (old, native, checker.Completed(1, b"", b"same failure\n"),
                         checker.Completed(None, b"", b"", True)):
                with self.subTest(case_id=case_id, same=same), self.assertRaises(checker.Failure):
                    checker.check_special_pair(case_id, same, same, "utf-8:strict")
            path = str(checker.LEGACY).encode()
            changes = ((path, b"/wrong/task-digest.py"), (b"line 129", b"line 128"),
                       (b"in <module>", b"in wrapper"), (b"in main", b"in other"),
                       (b"sys.exit(main())", b"main()"),
                       (b"AttributeError:", b"RuntimeError:") if case_id == checker.SPECIAL_EMPTY_STDIN
                       else (b"BrokenPipeError:", b"OSError:") if case_id == checker.SPECIAL_CLOSED_STDOUT
                       else (b"OSError:", b"RuntimeError:"))
            for original, replacement in changes:
                self.assertIn(original, old.stderr)
                wrong = checker.Completed(1, b"", old.stderr.replace(original, replacement, 1))
                with self.subTest(case_id=case_id, mutation=original), self.assertRaises(checker.Failure):
                    checker.check_special_pair(case_id, wrong, native, "utf-8:strict")
            # 正しい例外でも余分な呼出段・別の末尾なら拒否する。
            for trace in (b"extra frame\n" + old.stderr, old.stderr + b"RuntimeError: extra\n"):
                with self.assertRaises(checker.Failure):
                    checker.check_special_pair(case_id, checker.Completed(1, b"", trace), native, "utf-8:strict")

    def test_special_scope_rejects_unapproved_conditions_and_encodings(self):
        for case_id in ("unbuffered-other-device", "unbuffered-yes-closed-stdout", "unknown", ""):
            with self.subTest(case_id=case_id), self.assertRaises(checker.Failure):
                checker.special_expectations(case_id, "utf-8:strict")
        for encoding in (None, "latin-1:strict", "ascii:replace", ""):
            with self.subTest(encoding=encoding), self.assertRaises(checker.Failure):
                checker.special_expectations(checker.SPECIAL_EMPTY_STDIN, encoding)

    def test_special_source_hash_and_exact_statement_lines_are_required(self):
        checker.validate_legacy_special_source(checker.LEGACY)
        source = checker.LEGACY.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "task-digest.py"
            path.write_bytes(source + b"# changed\n")
            with self.assertRaises(checker.Failure):
                checker.validate_legacy_special_source(path)
            for original, replacement in (
                (b'        result = digest(data.decode("utf-8"))', b'        result = digest(data)'),
                (b'    print(result)', b'    print("wrong")'),
                (b'    sys.exit(main())', b'    sys.exit(1)'),
            ):
                changed = source.replace(original, replacement, 1)
                self.assertNotEqual(source, changed)
                path.write_bytes(changed)
                with patch.object(checker, "LEGACY_SPECIAL_SHA256", hashlib.sha256(changed).hexdigest()):
                    with self.subTest(statement=original), self.assertRaises(checker.Failure):
                        checker.validate_legacy_special_source(path)

    def test_special_counts_require_each_condition_control_and_encoding(self):
        count = len(checker.SPECIAL_ENCODINGS)
        good = dict.fromkeys(checker.SPECIAL_IDS, count)
        controls = dict.fromkeys(checker.SPECIAL_CONTROLS, count)
        if not sys.platform.startswith("linux"):
            good[checker.SPECIAL_FULL_STDOUT] = 0
        checker.check_special_counts(good, controls)
        for side in (0, 1):
            for key in (good if side == 0 else controls):
                correct = (good if side == 0 else controls)[key]
                for value in (None, True, -1, count + 1, *[n for n in (0, 1, count - 1, count) if n != correct]):
                    pair = [good.copy(), controls.copy()]
                    pair[side][key] = value
                    with self.subTest(side=side, key=key, value=value), self.assertRaises(checker.Failure):
                        checker.check_special_counts(*pair)
            for change in ("missing", "extra", "empty"):
                pair = [good.copy(), controls.copy()]
                if change == "missing":
                    pair[side].pop(next(iter(pair[side])))
                elif change == "extra":
                    pair[side]["unapproved"] = count
                else:
                    pair[side].clear()
                with self.subTest(side=side, change=change), self.assertRaises(checker.Failure):
                    checker.check_special_counts(*pair)

    def test_special_legacy_command_uses_real_script_without_driver(self):
        comparison = object.__new__(checker.Comparison)
        comparison.binary = Path("/native-fixture")
        self.assertEqual([sys.executable, "-B", str(checker.LEGACY), "-"],
                         comparison.special_command("legacy"))
        self.assertEqual(["/native-fixture", "--task-digest-fixture", "cli", "-"],
                         comparison.special_command("rust"))

    def test_nonblocking_timeout_and_spawn_failure_are_not_success(self):
        for error in (subprocess.TimeoutExpired("fixture", 1), FileNotFoundError(errno.ENOENT, "missing")):
            with patch.object(subprocess, "run", side_effect=error):
                if isinstance(error, subprocess.TimeoutExpired):
                    got = checker.run_nonblocking(["fixture"], data=b"", writer_open=True, cwd=None, env={})
                    self.assertTrue(got.timed_out)
                    with self.assertRaises(checker.Failure):
                        checker.check_result("nonblocking-timeout", got, (1, b"", b""))
                else:
                    with self.assertRaises(checker.Failure):
                        checker.run_nonblocking(["fixture"], data=b"", writer_open=True, cwd=None, env={})


    def closed_stderr_comparison(self):
        comparison = object.__new__(checker.Comparison)
        comparison.binary = Path("/native-fixture")
        comparison.counts = {"closed_stderr": 0}
        comparison.closed_stderr_coverage = set()
        return comparison

    def test_closed_stderr_coverage_requires_all_eighteen_conditions(self):
        required = {(case, buffering, encoding) for case in checker.CLOSED_STDERR_CASES
                    for buffering in checker.CLOSED_STDERR_BUFFERING for encoding in checker.SPECIAL_ENCODINGS}
        self.assertEqual(18, len(required))
        checker.check_closed_stderr_coverage(required)
        for key in required:
            with self.subTest(missing=key), self.assertRaises(checker.Failure):
                checker.check_closed_stderr_coverage(required - {key})
        for observed in (set(), required | {("unapproved", "unbuffered", "ascii:strict")}):
            with self.assertRaises(checker.Failure):
                checker.check_closed_stderr_coverage(observed)

    def test_closed_stderr_uses_both_real_pipe_destinations_and_fixed_results(self):
        comparison = self.closed_stderr_comparison()
        observed = []
        def completed(command, **kwargs):
            env = kwargs["env"]
            unbuffered = env.get("PYTHONUNBUFFERED") == "1"
            self.assertIsInstance(kwargs["stderr"], int)
            os.fstat(kwargs["stderr"])
            if "writer_open" in kwargs:
                self.assertTrue(kwargs["writer_open"])
                self.assertEqual(b"", kwargs["data"])
                case = "empty-nonblocking-closed-stderr"
            elif kwargs["stdout"] != subprocess.PIPE:
                os.fstat(kwargs["stdout"])
                self.assertEqual(b"body", kwargs["data"])
                case = "both-closed"
            else:
                self.assertEqual(b"", kwargs["data"])
                case = "rejection-closed-stderr"
            side = "legacy" if command[0] == sys.executable else "rust"
            observed.append((case, "unbuffered" if unbuffered else "buffered",
                             env["PYTHONIOENCODING"], side))
            return checker.Completed(1 if unbuffered else 120, b"", b"")
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(checker, "run", side_effect=completed), patch.object(checker, "run_nonblocking", side_effect=completed):
                comparison.closed_stderr(Path(temporary), b"body")
        required = {(case, buffering, encoding, side) for case in checker.CLOSED_STDERR_CASES
                    for buffering in checker.CLOSED_STDERR_BUFFERING for encoding in checker.SPECIAL_ENCODINGS
                    for side in ("legacy", "rust")}
        self.assertEqual(36, len(observed))
        self.assertEqual(required, set(observed))
        self.assertEqual(18, comparison.counts["closed_stderr"])
        checker.check_closed_stderr_coverage(comparison.closed_stderr_coverage)

    def test_closed_stderr_refuses_wrong_exit_output_timeout_and_one_bad_side(self):
        for fault in ("wrong-buffered-exit", "wrong-unbuffered-exit", "stdout", "stderr", "timeout", "missing"):
            for side in ("legacy", "rust"):
                comparison = self.closed_stderr_comparison()
                def completed(command, **kwargs):
                    unbuffered = kwargs["env"].get("PYTHONUNBUFFERED") == "1"
                    code = 1 if unbuffered else 120
                    stdout = stderr = b""
                    bad_side = (command[0] == sys.executable) == (side == "legacy")
                    if bad_side:
                        if fault == "wrong-buffered-exit" and not unbuffered:
                            code = 1
                        elif fault == "wrong-unbuffered-exit" and unbuffered:
                            code = 120
                        elif fault == "stdout":
                            stdout = b"extra"
                        elif fault == "stderr":
                            stderr = b"extra"
                        elif fault == "timeout":
                            return checker.Completed(None, b"", b"", True)
                        elif fault == "missing":
                            code = None
                    return checker.Completed(code, stdout, stderr)
                with tempfile.TemporaryDirectory() as temporary:
                    with patch.object(checker, "run", side_effect=completed), patch.object(checker, "run_nonblocking", side_effect=completed):
                        with self.subTest(fault=fault, side=side), self.assertRaises(checker.Failure):
                            comparison.closed_stderr(Path(temporary), b"body")


    def test_environment_boundary_table_is_fixed_and_raw(self):
        values = {name: (value, enabled) for name, value, enabled in checker.UNBUFFERED_CASES}
        self.assertEqual(49, len(values))
        self.assertEqual(len(values), len(checker.UNBUFFERED_CASES))
        off = {"unset", "empty", "zero", "zeros", "plus-zero", "minus-zero", "long-zero",
               "space-zero", "tab-zero", "lf-zero", "cr-zero", "vt-zero", "ff-zero", "spaces-signed-zeros"}
        self.assertEqual(off, {name for name, (_, enabled) in values.items() if not enabled})
        self.assertEqual((b"0" * 4096, False), values["long-zero"])
        self.assertEqual((b" \t\n\r\x0b\x0c-00", False), values["spaces-signed-zeros"])
        self.assertEqual((b"0 ", True), values["zero-space"])
        self.assertEqual((b"\xff", True), values["raw"])
        for name, value, _ in checker.UNBUFFERED_CASES:
            env = checker.env_with_value({}, value)
            self.assertEqual(None if value is None else value,
                             os.fsencode(env["PYTHONUNBUFFERED"]) if "PYTHONUNBUFFERED" in env else None, name)

    def test_environment_coverage_rejects_missing_extra_duplicate_and_linux_skip(self):
        flags = {name for name, _, _ in checker.UNBUFFERED_CASES}
        for platform in ("linux", "darwin"):
            with patch.object(sys, "platform", platform):
                required = checker.env_stream_required()
                self.assertEqual(165 if platform == "linux" else 132, len(required))
                checker.check_env_coverage(flags, required)
                for missing in required:
                    with self.assertRaises(checker.Failure):
                        checker.check_env_coverage(flags, required - {missing})
                for flag_set, stream_set in ((flags - {"yes"}, required), (flags | {"extra"}, required),
                                             (flags, required | {("extra", "closed-stdout", "ascii:strict")})):
                    with self.assertRaises(checker.Failure):
                        checker.check_env_coverage(flag_set, stream_set)
                if platform == "linux":
                    without_full = {key for key in required if key[1] != "full-stdout"}
                    with self.assertRaises(checker.Failure):
                        checker.check_env_coverage(flags, without_full)
        covered = set()
        checker.record_coverage(covered, ("one", "full-stdout", "ascii:strict"))
        with self.assertRaises(checker.Failure):
            checker.record_coverage(covered, ("one", "full-stdout", "ascii:strict"))

    def test_linux_special_full_is_mandatory_and_other_os_does_not_count_it(self):
        controls = dict.fromkeys(checker.SPECIAL_CONTROLS, 3)
        counts = dict.fromkeys(checker.SPECIAL_IDS, 3)
        with patch.object(sys, "platform", "linux"):
            checker.check_special_counts(counts, controls)
            with self.assertRaises(checker.Failure):
                checker.check_special_counts(dict(counts, **{checker.SPECIAL_FULL_STDOUT: 0}), controls)
        with patch.object(sys, "platform", "darwin"):
            checker.check_special_counts(dict(counts, **{checker.SPECIAL_FULL_STDOUT: 0}), controls)
            with self.assertRaises(checker.Failure):
                checker.check_special_counts(counts, controls)

    def environment_comparison(self):
        comparison = object.__new__(checker.Comparison)
        comparison.binary = Path("/native-fixture")
        comparison.special_counts = dict.fromkeys(checker.SPECIAL_IDS, 0)
        comparison.env_coverage = set()
        comparison.env_stream_coverage = set()
        comparison.synthetic_outputs = 0
        comparison.skips = []
        return comparison

    def environment_completed(self, observed, command, **kwargs):
        value = kwargs["env"].get("PYTHONUNBUFFERED")
        raw = os.fsencode(value) if value is not None else None
        matches = [(name, enabled) for name, val, enabled in checker.UNBUFFERED_CASES if val == raw]
        self.assertEqual(1, len(matches))
        name, enabled = matches[0]
        side = "legacy" if command[0] == sys.executable else "rust"
        encoding = kwargs["env"]["PYTHONIOENCODING"]
        if "unbuffered" in command or "-c" in command:
            observed.append(("flag", name, side))
            return checker.Completed(0, (json.dumps({"enabled": enabled}) + "\n").encode(), b"")
        if "writer" in command or "writer-full-fd" in command:
            stage = command[-3] if "writer" in command else "write"
            diagnostic = (f"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='{'ascii' if encoding.startswith('ascii') else 'utf-8'}'>\n"
                          "OSError: [Errno 28] No space left on device\n").encode()
            record = {"code": 120, "written_hex": b"ok\n".hex() if stage == "flush" else b"o".hex() if stage == "partial" else "",
                      "stdout_hex": "", "stderr_hex": diagnostic.hex()}
            data = (json.dumps(record) + "\n").encode()
            if "writer-full-fd" in command:
                self.assertTrue(stat.S_ISCHR(os.fstat(kwargs["stdout"].fileno()).st_mode))
                return checker.Completed(0, b"", data)
            return checker.Completed(0, data, b"")
        if "writer_open" in kwargs:
            self.assertTrue(kwargs["writer_open"])
            self.assertEqual(b"", kwargs["data"])
            target = "empty-nonblocking-closed-stderr"
        elif kwargs["stderr"] != subprocess.PIPE:
            os.fstat(kwargs["stderr"])
            target = "both-closed" if kwargs["stdout"] != subprocess.PIPE else "rejection-closed-stderr"
        elif hasattr(kwargs["stdout"], "fileno"):
            info = os.fstat(kwargs["stdout"].fileno())
            self.assertTrue(stat.S_ISCHR(info.st_mode))
            self.assertEqual((1, 7), (os.major(info.st_rdev), os.minor(info.st_rdev)))
            target = "full-stdout"
        else:
            os.fstat(kwargs["stdout"])
            target = "closed-stdout"
        observed.append((target, name, encoding, side))
        if target in checker.CLOSED_STDERR_CASES:
            return checker.Completed(1 if enabled else 120, b"", b"")
        if enabled:
            case = checker.SPECIAL_FULL_STDOUT if target == "full-stdout" else checker.SPECIAL_CLOSED_STDOUT
            return self.special_results(case, encoding)[0 if side == "legacy" else 1]
        detail = "OSError: [Errno 28] No space left on device\n" if target == "full-stdout" else "BrokenPipeError: [Errno 32] Broken pipe\n"
        diagnostic = (f"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='{'ascii' if encoding.startswith('ascii') else 'utf-8'}'>\n" + detail).encode()
        return checker.Completed(120, b"", diagnostic)

    def test_environment_runtime_compares_both_sides_and_real_descriptors(self):
        comparison = self.environment_comparison()
        observed = []
        def completed(command, **kwargs):
            return self.environment_completed(observed, command, **kwargs)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(checker, "run", side_effect=completed), patch.object(checker, "run_nonblocking", side_effect=completed):
                comparison.environment_io(Path(temporary), b"body")
        flags = {("flag", name, side) for name, _, _ in checker.UNBUFFERED_CASES for side in ("legacy", "rust")}
        streams = {(target, name, encoding, side) for name, target, encoding in checker.env_stream_required()
                   for side in ("legacy", "rust")}
        self.assertEqual(len(flags | streams), len(observed))
        self.assertEqual(flags | streams, set(observed))
        self.assertEqual(10 if sys.platform.startswith("linux") else 9, comparison.synthetic_outputs)
        self.assertEqual(3 if sys.platform.startswith("linux") else 0, comparison.special_counts[checker.SPECIAL_FULL_STDOUT])
        if not sys.platform.startswith("linux"):
            self.assertIn("33条件未実施", comparison.skips[0])

    def test_environment_runtime_refuses_both_wrong_flags_and_input_changes(self):
        for fault in ("wrong-flags", "input-change"):
            comparison = self.environment_comparison()
            def completed(command, **kwargs):
                if fault == "input-change":
                    (kwargs["cwd"] / "input").write_bytes(b"changed")
                    return checker.Completed(0, b'{"enabled": false}\n', b"")
                return checker.Completed(0, b'{"enabled": true}\n', b"")
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "input").write_bytes(b"body")
                with patch.object(checker, "run", side_effect=completed), self.assertRaises(checker.Failure):
                    comparison.environment_io(root, b"body")


if __name__ == "__main__":
    unittest.main()
