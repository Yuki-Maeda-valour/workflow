#!/usr/bin/env python3
"""S2L比較器が出力・終了値・保存結果の不一致を拒否する検査。"""
from __future__ import annotations
import argparse
import copy
import importlib.util
from pathlib import Path
import subprocess
import os
import tempfile
import unittest
from unittest import mock
import check_loop_state_units as units
import check_loop_state_mutations as mutations

# 比較の部品(決定の case など)と同じ module を使い、Mismatch の型を共有する。
import check_loop_state as CHECK


class ComparisonTest(unittest.TestCase):
    def stdlib_frame(self, module, line, function):
        path = str(Path(CHECK.sysconfig.get_path("stdlib")) / module)
        return f'  File "{path}", line {line}, in {function}\n'.encode()

    def test_exception_location_mapping_is_limited_to_declared_frames(self):
        header = b"Traceback (most recent call last):\n"
        declared = self.stdlib_frame("json/decoder.py", 353, "raw_decode")
        expected = b'  File "<CPython3.10>/json/decoder.py", line 353, in raw_decode\n'
        self.assertEqual(CHECK.exception_locations(header + declared), header + expected)
        # 位置つき: Rust の同じ行に固定名があるときだけ写す。
        self.assertEqual(CHECK.exception_locations(header + declared, header + expected), header + expected)
        self.assertEqual(CHECK.exception_locations(header + declared, header + b"other\n"), header + declared)
        for changed in [declared.replace(b"353", b"354"), declared.replace(b"raw_decode", b"other"),
                        b"error value: " + declared.removeprefix(b"  File ")]:
            self.assertEqual(CHECK.exception_locations(header + changed), header + changed)
        for value in [declared, b"ValueError: reason\n" + declared, b"error value: " + declared]:
            self.assertEqual(CHECK.exception_locations(value), value)

    def test_pseudo_traceback_after_diagnostic_is_not_a_frame(self):
        # 診断の本文の後に続く擬似 Traceback(利用者の値や理由の中の字面)は、固定名へ写さない。
        old_path = str(Path(CHECK.sysconfig.get_path("stdlib")) / "json/__init__.py")
        legacy = str(CHECK.LEGACY)
        for path, fixed in [(old_path, "<CPython3.10>/json/__init__.py"), (legacy, CHECK.LEGACY_NAME)]:
            old = (f'STOP: supplied path\nTraceback (most recent call last):\n  File "{path}", line 346, in loads\n'
                   f'    return _default_decoder.decode(s)\nValueError: supplied value\n').encode()
            new = old.replace(path.encode(), fixed.encode())
            with self.subTest(path=fixed):
                self.assertEqual(CHECK.exception_locations(old), old)
                self.assertEqual(CHECK.exception_locations(old, new), old)
                with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STDERR"):
                    CHECK.compare_result("pseudo traceback", subprocess.CompletedProcess([], 1, b"", old),
                                         subprocess.CompletedProcess([], 1, b"", new))
                # header_allowed を外す変異(本文の後の見出しも Traceback の始まりと見る)では写ってしまう。
                def without_header_allowed(lines):
                    frames, inside = set(), False
                    for index, line in enumerate(lines):
                        if line == b"Traceback (most recent call last):\n":
                            inside = True
                        elif inside:
                            if line.startswith(b'  File "'):
                                frames.add(index)
                            elif not line.startswith(b"    "):
                                inside = False
                    return frames
                with mock.patch.object(CHECK, "traceback_frames", without_header_allowed):
                    self.assertEqual(CHECK.exception_locations(old, new), new)
                    CHECK.compare_result("mutated", subprocess.CompletedProcess([], 1, b"", old),
                                         subprocess.CompletedProcess([], 1, b"", new))

    def test_ubuntu_posixpath_lines_are_not_declared(self):
        # 本家 3.10.11 の行だけ。Ubuntu 3.10.12 に合わせた 393・428 は対応表に無い。
        self.assertNotIn(("posixpath.py", 393, "realpath"), CHECK.EXCEPTION_LOCATIONS)
        self.assertNotIn(("posixpath.py", 428, "_joinrealpath"), CHECK.EXCEPTION_LOCATIONS)
        self.assertIn(("posixpath.py", 396, "realpath"), CHECK.EXCEPTION_LOCATIONS)
        for module, _, _ in CHECK.EXCEPTION_LOCATIONS:
            self.assertIn(module, CHECK.STDLIB_SHA256)

    def test_stdlib_sha256_mismatch_fails_the_case(self):
        header = b"Traceback (most recent call last):\n"
        frame = self.stdlib_frame("posixpath.py", 396, "realpath")
        with mock.patch.dict(CHECK.STDLIB_SHA256, {"posixpath.py": "0" * 64}), mock.patch.dict(CHECK._STDLIB_CHECKED, {}, clear=True):
            with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STDLIB_SHA256"):
                CHECK.exception_locations(header + frame)
        # 照らしたPythonが本家と同じなら写す(比べる側の前提)。
        self.assertTrue(CHECK.stdlib_matches("posixpath.py"))

    def test_rust_stdlib_frames_match_upstream_lines(self):
        """Rust が出す導入先のフレームは、本家 3.10.11 の行・関数・ソースの行と同じ。"""
        import ast, re
        source = CHECK.ROOT / "native/crates/cli/src/commands/loop_state"
        def function_at(tree, line):
            best = None
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.lineno <= line <= node.end_lineno:
                    if best is None or node.lineno > best.lineno:
                        best = node
            return best.name
        found = []
        for path in sorted(source.glob("*.rs")):
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r'\.external\(\s*"([^"]+)",\s*"([^"]+)",\s*(\d+),\s*(?:r#"(.*?)"#|"((?:\\.|[^"\\])*)")', text, re.S):
                found.append((m.group(1), m.group(2), int(m.group(3)), m.group(4) if m.group(4) is not None else m.group(5)))
            module = {"realpath.rs": "posixpath.py", "legacy_tempfile.rs": "tempfile.py"}.get(path.name)
            if module:
                for m in re.finditer(r'frame\(\s*[^,]+,\s*"([^"]+)",\s*(\d+),\s*"((?:\\.|[^"\\])*)"', text, re.S):
                    found.append((module, m.group(1), int(m.group(2)), m.group(3)))
        self.assertGreater(len(found), 60)
        trees = {}
        # SIGINT の途中のフレームは比べない(22:31 の決定)ので、対応表に無くてよい。
        uncompared = {("selectors.py", 469, "select"), ("subprocess.py", 1953, "_wait")}
        for module, function, line, text in found:
            with self.subTest(frame=(module, function, line)):
                path = Path(CHECK.sysconfig.get_path("stdlib")) / module
                self.assertTrue(CHECK.stdlib_matches(module), module)
                if module not in trees:
                    trees[module] = (ast.parse(path.read_text()), path.read_text().split("\n"))
                tree, lines = trees[module]
                self.assertEqual(lines[line - 1].strip(), text.encode().decode("unicode_escape") if "\\" in text else text)
                self.assertEqual(function_at(tree, line), function)
                if (module, line, function) not in uncompared:
                    self.assertIn((module, line, function), CHECK.EXCEPTION_LOCATIONS)

    def test_legacy_frame_uses_fixed_name_only_in_traceback_frames(self):
        legacy = os.fsencode(CHECK.LEGACY)
        header = b"Traceback (most recent call last):\n"
        frame = b'  File "' + legacy + b'", line 2201, in <module>\n    raise SystemExit(main())\n'
        fixed = b'  File "loop-state.py", line 2201, in <module>\n    raise SystemExit(main())\n'
        tail = b"ValueError: x\n"
        self.assertEqual(CHECK.exception_locations(header + frame + tail, header + fixed + tail), header + fixed + tail)
        CHECK.compare_result("fixed", subprocess.CompletedProcess([], 1, b"", header + frame + tail),
                             subprocess.CompletedProcess([], 1, b"", header + fixed + tail))
        # 誤った固定名・利用者のパス・理由の中の字面・擬似 Traceback は写さない。
        for wrong in [b'  File "loop_state.py", line 2201, in <module>\n    raise SystemExit(main())\n',
                      b'  File "/x/loop-state.py", line 2201, in <module>\n    raise SystemExit(main())\n']:
            with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STDERR"):
                CHECK.compare_result("wrong", subprocess.CompletedProcess([], 1, b"", header + frame + tail),
                                     subprocess.CompletedProcess([], 1, b"", header + wrong + tail))
        reason = b"ERROR [loop-state] " + frame
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STDERR"):
            CHECK.compare_result("reason", subprocess.CompletedProcess([], 20, b"", reason),
                                 subprocess.CompletedProcess([], 20, b"", b"ERROR [loop-state] " + fixed))
        user = b'ValueError: [Errno 2] File "' + legacy + b'", line 1\n'
        self.assertEqual(CHECK.exception_locations(header + frame + user, header + fixed + user).count(legacy), 1)

    def test_truncated_output_is_not_mapped_to_fixed_names(self):
        # 書込みが途中で切れた出力(改行の無い最後の行・途中で切れたパス)は対応づけない。
        legacy = os.fsencode(CHECK.LEGACY)
        header = b"Traceback (most recent call last):\n"
        whole = b'  File "' + legacy + b'", line 2201, in <module>\n'
        fixed = b'  File "loop-state.py", line 2201, in <module>\n'
        for cut in [whole[:-1], whole[: len(whole) // 2]]:
            self.assertEqual(CHECK.exception_locations(header + cut, header + fixed[: len(cut)]), header + cut)
            with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STDERR"):
                CHECK.compare_result("cut", subprocess.CompletedProcess([], 1, b"", header + cut),
                                     subprocess.CompletedProcess([], 1, b"", header + fixed[:-1]))

    def test_child_environment_removes_and_keeps_case_variables(self):
        env = {"PATH": "/usr/bin", "PYTHONWARNINGS": "error", "PYTHONMALLOC": "debug", "PYTHONPATH": "/elsewhere",
               "PYTHONPYCACHEPREFIX": "/x", "PYTHONSOMETHINGNEW": "1", "PYTHONHASHSEED": "7"}
        cleaned = CHECK.child_environment(env, keep_case=False, given={"PYTHONUNBUFFERED": "1"})
        self.assertEqual({k for k in cleaned if k.startswith("PYTHON")}, {"PYTHONPATH", "PYTHONUNBUFFERED"})
        self.assertEqual(cleaned["PYTHONPATH"], str(CHECK.yaml_root()))
        for name in [*CHECK.REMOVED_2231[1:], *CHECK.REMOVED_0028, "PYTHONSOMETHINGNEW"]:
            with self.subTest(name=name), self.assertRaisesRegex(CHECK.Mismatch, "S2L_ENV_REMOVED_LEFT"):
                CHECK.verify_child_environment({**cleaned, name: "1"}, {})
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_ENV_REMOVED_LEFT"):
            CHECK.verify_child_environment({**cleaned, "PYTHONPATH": "/elsewhere"}, {})
        for name in ["PYTHONUNBUFFERED", "PYTHONINTMAXSTRDIGITS", "PYTHONIOENCODING"]:
            with self.subTest(name=name), self.assertRaisesRegex(CHECK.Mismatch, "S2L_ENV_CASE_LOST"):
                CHECK.verify_child_environment({"PATH": "/usr/bin"}, {name: "1"})
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_ENV_CASE_UNSET_LEFT"):
            CHECK.verify_child_environment({"PYTHONIOENCODING": "utf-8"}, {"PYTHONIOENCODING": None})
        import loop_state_arguments as arguments
        base = arguments.environment()
        self.assertEqual({k for k in base if k.startswith(b"PYTHON")},
                         {b"PYTHONPATH", b"PYTHONUTF8", b"PYTHONCOERCECLOCALE", b"PYTHONIOENCODING"})
        # 比較の起動の入口は、外す変数が残った環境を拒む。
        import loop_state_contract_cases as contracts
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_ENV_REMOVED_LEFT"):
            contracts.invoke(["/bin/true"], {**base, b"PYTHONWARNINGS": b"error"}, "/")
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_ENV_REMOVED_LEFT"):
            arguments.run(["/bin/true"], {**base, b"PYTHONDEVMODE": b"1"}, "/")

    def test_startup_current_thread_is_checked_only_for_required_cases(self):
        import loop_state_decision_cases as decisions
        def failure(thread):
            return (decisions.STARTUP_STREAM % 1 + b"Current thread 0x" + thread
                    + b" (most recent call first):\n" + decisions.STARTUP_TAIL)
        old = subprocess.CompletedProcess([], 1, b"", failure(b"00007f0000000001"))
        new = subprocess.CompletedProcess([], 1, b"", failure(b"00007f00000000ff"))
        decisions.startup_compare((1,))("startup", old, new)
        # 必須 case の外(通常の比較)では、値の違いは不一致のまま。
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STDERR"):
            CHECK.compare_result("not-required", old, new)
        for bad in [failure(b"7f00000000ff"), failure(b"00007F00000000FF"), failure(b"00007f00000000ff") * 2,
                    failure(b"00007f00000000ff").replace(b"Is a directory: 1", b"Is a directory: 2"),
                    b"prefix\n" + failure(b"00007f00000000ff")]:
            with self.assertRaises(CHECK.Mismatch):
                decisions.startup_compare((1,))("bad", old, subprocess.CompletedProcess([], 1, b"", bad))
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STARTUP_EXIT"):
            decisions.startup_compare((2,))("exit", subprocess.CompletedProcess([], 1, b"", b""),
                                            subprocess.CompletedProcess([], 0, b"", b""))

    def test_timeout_seconds_field_form_and_range(self):
        import loop_state_decision_cases as decisions
        def result(seconds):
            return subprocess.CompletedProcess([], 1, b"", b"subprocess.TimeoutExpired: Command 'x' timed out after "
                                               + seconds + b" seconds\n")
        decisions.timeout_compare(2)("ok", result(b"1.9999"), result(b"1.5"))
        decisions.timeout_compare(2)("exponent", result(b"5e-05"), result(b"1.5e-05"))
        # 整数だけの字面('1')は float の repr ではない。
        for bad in [b"0", b"1", b"2", b"-1.0", b"2.5", b"nan", b"1.0.0", b"1,5", b"0x1", b"1e5", b"1.", b".5"]:
            with self.subTest(bad=bad), self.assertRaises(CHECK.Mismatch):
                decisions.timeout_compare(2)("bad", result(b"1.5"), result(bad))
        # 実時間で上限が効くこと(上限より早い・余裕より遅い終わりは不合格)。
        class Timed:
            timing = {}
        for seconds, accepted in [((1.0, 1.9), True), ((0.9, 1.2), False), ((1.2, 2.1), False)]:
            Timed.last_seconds = seconds
            if accepted:
                decisions.elapsed_within(Timed, "elapsed", 1, 1.0)
            else:
                with self.assertRaisesRegex(CHECK.Mismatch, "S2L_TIMEOUT_ELAPSED"):
                    decisions.elapsed_within(Timed, "elapsed", 1, 1.0)
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STDERR"):
            changed = subprocess.CompletedProcess([], 1, b"", result(b"1.5").stderr.replace(b"'x'", b"'y'"))
            decisions.timeout_compare(2)("other", result(b"1.5"), changed)

    def test_signal_comparison_requires_signal_header_and_last_line(self):
        import signal as signals
        import loop_state_decision_cases as decisions
        good = subprocess.CompletedProcess([], -2, b"", b"Traceback (most recent call last):\n  frames\nKeyboardInterrupt\n")
        decisions.signal_compare(signals.SIGINT)("ok", good, subprocess.CompletedProcess([], -2, b"", b"Traceback (most recent call last):\nKeyboardInterrupt\n"))
        for bad in [subprocess.CompletedProcess([], 1, b"", good.stderr), subprocess.CompletedProcess([], -2, b"x", good.stderr),
                    subprocess.CompletedProcess([], -2, b"", b"KeyboardInterrupt\n"),
                    subprocess.CompletedProcess([], -2, b"", b"Traceback (most recent call last):\nValueError\n"),
                    subprocess.CompletedProcess([], -2, b"", b"early\n" + good.stderr)]:
            with self.assertRaises(CHECK.Mismatch):
                decisions.signal_compare(signals.SIGINT)("bad", good, bad)
        with self.assertRaises(CHECK.Mismatch):
            decisions.signal_compare(signals.SIGTERM)("term", subprocess.CompletedProcess([], -15, b"", b""),
                                                    subprocess.CompletedProcess([], -15, b"", b"x"))

    def test_rust_children_are_git_only(self):
        import loop_state_decision_cases as decisions
        self.assertGreaterEqual(decisions.static_git_only(), 1)
        source = "fn f(){ let _ = Command::new(\"sh\"); }"
        with mock.patch.object(Path, "read_text", lambda self, **k: source if self.name == "process.rs" else ""):
            with self.assertRaisesRegex(CHECK.Mismatch, "S2L_NON_GIT_CHILD"):
                decisions.static_git_only()

    def test_raw_exit_stdout_and_stderr_are_independent(self):
        baseline = subprocess.CompletedProcess([], 0, b"\x00\xff\n", b"cause\n")
        for field, value, marker in [("returncode", 20, "S2L_RETURNCODE"),
                                      ("stdout", b"\x00\xef\xbf\xbd\n", "S2L_STDOUT"),
                                      ("stderr", b"", "S2L_STDERR")]:
            changed = subprocess.CompletedProcess([], baseline.returncode, baseline.stdout, baseline.stderr)
            setattr(changed, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(CHECK.Mismatch, marker):
                CHECK.compare_result("mutation", baseline, changed)

    def saving_case(self, old_action, new_action, initial=None, old_code=0):
        with tempfile.TemporaryDirectory(prefix="s2l-comparator-") as temporary:
            output = Path(temporary) / "state"
            if initial is not None:
                output.write_bytes(initial)
                output.chmod(0o640)
            def run(command, *args, **kwargs):
                action = old_action if str(command[0]) == CHECK.sys.executable else new_action
                self.assertEqual(CHECK.output_state(output), None if initial is None else ("file", 0o640, initial))
                action(output)
                return subprocess.CompletedProcess(command, old_code, b"", b"")
            with mock.patch.object(CHECK, "_RUN", run):
                return CHECK.Comparison("/owned/fixture").run(["python3", str(CHECK.LEGACY), "snapshot", "--out", str(output)])

    def test_success_without_new_write_is_rejected(self):
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STATE_MISSING"):
            self.saving_case(lambda p: p.write_bytes(b'{"schema":7}'), lambda p: None)

    def test_overwrite_starts_from_same_original_bytes_and_mode(self):
        def write(path):
            path.write_bytes(b"new")
            path.chmod(0o600)
        self.saving_case(write, write, initial=b"original")
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STATE_BYTES"):
            self.saving_case(write, lambda p: None, initial=b"original")

    def test_failed_command_cannot_create_output(self):
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STATE_BYTES"):
            self.saving_case(lambda p: None, lambda p: p.write_bytes(b"unexpected"), old_code=20)

    def test_failed_command_cannot_replace_existing_output(self):
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STATE_BYTES"):
            self.saving_case(lambda p: None, lambda p: p.write_bytes(b"unexpected"), initial=b"original", old_code=20)

    def test_state_modes_are_compared(self):
        def old(path):
            path.write_bytes(b"same")
            path.chmod(0o600)
        def changed(path):
            path.write_bytes(b"same")
            path.chmod(0o644)
        with self.assertRaisesRegex(CHECK.Mismatch, "S2L_STATE_BYTES"):
            self.saving_case(old, changed)

    def complete_report(self):
        groups=CHECK.contract_groups()
        # Mac は /dev/full の経路と、APFS が作れない UTF-8 でない名前の case を数えない。
        self.assertEqual((groups["io"],groups["repository2"],groups["r2_permissions"]),(32,1,2) if CHECK.sys.platform=="darwin" else (51,2,4))
        def boundaries(group):return [{"name":name,"action":action} for name,action in CHECK.expected_os_boundaries(group)]
        return {"partial":False,"legacy_methods":57,"legacy_comparisons":183,"legacy_commands":dict(CHECK.LEGACY_INVOCATIONS),
                "arguments":{"arguments":404,"skipped":0,"os_boundaries":boundaries("arguments")},"terminals":{"pty":40,"skipped":0},
                "contracts":{"contract_cases":sum(groups.values()),"groups":groups,"skipped":0,"os_boundaries":boundaries("contracts")},
                "encoding":{"cases":35,"legacy_expected":35,"rust_expected":35,"commands":sorted(CHECK.COMMANDS),"timeouts":0,"inputs_unchanged":True,"all_groups_absent":True,"git_raw_env_preserved":True,"skipped":0,"os_boundaries":boundaries("encoding")},
                "yaml":{"yaml_cli":80,"top":40,"linked":40,"skipped":0},
                "encoding_matrix":{"encoding_matrix":126,"settings":18,"regular_file":8,"unknown_handler_write":1,"commands":sorted(CHECK.COMMANDS),"skipped":0},
                "decisions":{"decision_cases":sum(CHECK.decision_groups().values()),"groups":CHECK.decision_groups(),"skipped":0,"timeouts":0,"os_boundaries":boundaries("decisions")}}

    def test_r1_cause_and_walk_groups_are_required(self):
        import loop_state_r1_cases as r1
        cases=r1.json_cases()
        self.assertEqual(len(cases),55)
        self.assertEqual(len({row[0] for row in cases}),55)
        for group in ('r1_compare','r1_json','r1_walk','r1_state_stop','r2_compare','r2_permissions','r2_refs'):
            bad=self.complete_report();bad['contracts']['groups'][group]=0
            with self.subTest(group=group),self.assertRaises(CHECK.Mismatch):CHECK.require_full_coverage(bad)

    def test_missing_execution_partial_skip_and_timeout_cannot_pass(self):
        good=self.complete_report();self.assertTrue(CHECK.require_full_coverage(good))
        for group in ("arguments","terminals","contracts","encoding","yaml","encoding_matrix","decisions"):
            bad=copy.deepcopy(good);del bad[group]
            with self.subTest(group=group),self.assertRaises(CHECK.Mismatch):CHECK.require_full_coverage(bad)
            for key in ("skipped","timeouts"):
                bad=copy.deepcopy(good);bad[group][key]=1
                with self.subTest(group=group,key=key),self.assertRaises(CHECK.Mismatch):CHECK.require_full_coverage(bad)
        for group,key,value in [(None,"partial",True),("encoding","commands",["state-digest"]),("encoding","rust_expected",0),("encoding","all_groups_absent",False),("encoding","inputs_unchanged",False),("contracts","contract_cases",1)]:
            bad=copy.deepcopy(good);(bad if group is None else bad[group])[key]=value
            with self.subTest(group=group,key=key),self.assertRaises(CHECK.Mismatch):CHECK.require_full_coverage(bad)
        # ファイル系が拒んで飛ばした入力: 一覧に無い飛ばし(Linux では 1 件でも)・一覧の欠けは不合格。
        for group in ("arguments","terminals","contracts","encoding","yaml","encoding_matrix","decisions"):
            bad=copy.deepcopy(good);bad[group]["os_boundaries"]=[*bad[group].get("os_boundaries",[]),{"name":"unexpected"}]
            with self.subTest(group=group,boundary="extra"),self.assertRaisesRegex(CHECK.Mismatch,"S2L_OS_BOUNDARY"):CHECK.require_full_coverage(bad)
            if CHECK.expected_os_boundaries(group):
                bad=copy.deepcopy(good);bad[group]["os_boundaries"]=bad[group]["os_boundaries"][1:]
                with self.subTest(group=group,boundary="missing"),self.assertRaisesRegex(CHECK.Mismatch,"S2L_OS_BOUNDARY"):CHECK.require_full_coverage(bad)
                bad=copy.deepcopy(good);bad[group]["os_boundaries"][0]["action"]="other"
                with self.subTest(group=group,boundary="action"),self.assertRaisesRegex(CHECK.Mismatch,"S2L_OS_BOUNDARY"):CHECK.require_full_coverage(bad)
        # Linux では作れない名前を飛ばさない。Mac でも EILSEQ のときだけ飛ばす(ほかの失敗は止める)。
        import errno
        with tempfile.TemporaryDirectory(prefix='s2l-raw-name-') as temporary:
            path=Path(temporary)/'raw-\udcff'
            def refused(_):raise OSError(errno.EILSEQ,'Illegal byte sequence')
            def denied(_):raise OSError(errno.EACCES,'Permission denied')
            def created(_):pass
            for platform,create,expected in [("linux",refused,OSError),("darwin",denied,OSError),("darwin",refused,False),("linux",created,True),("darwin",created,True)]:
                with self.subTest(platform=platform,create=create.__name__),mock.patch.object(CHECK.sys,"platform",platform):
                    if expected is OSError:
                        with self.assertRaises(OSError):CHECK.create_raw(path,create)
                    else:self.assertIs(CHECK.create_raw(path,create),expected)

    def test_unit_inventory_missing_ignored_and_fake_success_fail(self):
        listing=''.join(name+': test\n' for name in sorted(units.REQUIRED)).encode();units.assert_listing(listing)
        with self.assertRaisesRegex(AssertionError,'S2L_UNIT_INVENTORY'):units.assert_listing(listing.split(b'\n',1)[1])
        # RLIMIT_NOFILE を下げた旧新の比較は恒久の比較に無い(00:42 の決定)。
        self.assertFalse(hasattr(units,'assert_low_fd_result'))
        self.assertNotIn(b'setrlimit',Path(units.__file__).read_bytes().split(b'def main')[1])
        lines=''.join(f'test {name} ... ok\n'for name in sorted(units.REQUIRED)).encode()
        summary=f'test result: ok. {units.COUNT} passed; 0 failed; 0 ignored\n'.encode()
        units.assert_result(subprocess.CompletedProcess([],0,lines+summary,b''))
        # 並行の出力が混ざっても、--logfile の行で名前を照らす。欠け・ignored は落とす。
        log=''.join(f'ok {name}\n'for name in sorted(units.REQUIRED)).encode()
        units.assert_result(subprocess.CompletedProcess([],0,summary,b''),log)
        for bad in [log.split(b'\n',1)[1],log+b'ignored extra\n']:
            with self.assertRaises(AssertionError):units.assert_result(subprocess.CompletedProcess([],0,summary,b''),bad)
        for raw in [summary,lines+summary.replace(f'{units.COUNT} passed'.encode(),f'{units.COUNT-1} passed'.encode()),lines+summary.replace(b'0 ignored',b'1 ignored'),lines+summary+b'... ignored']:
            with self.assertRaises(AssertionError):units.assert_result(subprocess.CompletedProcess([],0,raw,b''))
        # ファイル系が拒んで飛ばした検査は、Mac ではちょうど決めた一覧、ほかの OS では 0 件。
        stdout=b''.join(f'S2L_OS_BOUNDARY name={name} os=macos input=non_utf8_name errno=92 action={action} ascii_control=x\n'.encode() for name,action in units.MAC_FS_REFUSALS)
        stdout+=b'S2L_OS_BOUNDARY name=config_snapshot_x os=macos old_reject=true\n'
        self.assertEqual(units.fs_refusals(stdout),sorted(units.MAC_FS_REFUSALS))
        with self.assertRaisesRegex(AssertionError,'S2L_UNIT_FS_REFUSAL_FORM'):units.fs_refusals(b'S2L_OS_BOUNDARY name=x os=macos input=non_utf8_name\n')
        swapped=[(name,'skipped' if action=='replaced' else 'replaced') for name,action in sorted(units.MAC_FS_REFUSALS)]
        for platform,refusals,accepted in [('linux',[],True),('linux',[('r3_enumeration_failure_keeps_surrogate_name','skipped')],False),
                                           ('darwin',sorted(units.MAC_FS_REFUSALS),True),('darwin',sorted(units.MAC_FS_REFUSALS)[1:],False),
                                           ('darwin',swapped,False),('darwin',[],False)]:
            with self.subTest(platform=platform,refusals=refusals),mock.patch.object(units.sys,'platform',platform):
                if accepted:units.assert_fs_refusals(refusals)
                else:
                    with self.assertRaisesRegex(AssertionError,'S2L_UNIT_FS_REFUSALS'):units.assert_fs_refusals(refusals)

    def test_r2_mutation_anchors_and_mac_embedded_script(self):
        import ast,loop_state_encoding
        ast.parse(loop_state_encoding.MAC_PREPARE_SCRIPT)
        self.assertEqual(mutations.validate_anchors(CHECK.ROOT/'native'),21)
        script=loop_state_encoding.MAC_PREPARE_SCRIPT
        self.assertIn('s2l_mac_prepare',script)
        import loop_state_r2_cases as r2
        self.assertEqual(len(r2.compare_cases()),46)
        from loop_state_deep_fixture import DeepTree
        with tempfile.TemporaryDirectory(prefix='s2l-deep-selftest-') as tmp:
            root=Path(tmp)/'root';root.mkdir();tree=DeepTree(root,1000)
            try:
                held=Path(tmp)/'held';tree.detach(992,held)
                try:
                    tree.leaf(992,b'fixture body');self.assertEqual(tree.identity(992,True)[-1][1],b'fixture body');tree.leaf(992,None)
                finally:tree.restore(992,held)
            finally:tree.close()
            self.assertEqual(list(root.iterdir()),[])

        with self.assertRaises(IndentationError):ast.parse('import sys\n  x=1\n')

    def test_mutations_require_the_named_failure_and_not_arbitrary_exit(self):
        # 機密openと設定順序は実Rust変異で検査する。その検出器も任意失敗を受理しない。
        for name,marker in [('secret','I01_NO_OPEN'),('order','CONFIG_ORDER')]:
            raw=f'test {name} ... FAILED\nthread panicked at {marker}\n1 failed; 0 ignored'.encode()
            self.assertTrue(mutations.asserted_failure(subprocess.CompletedProcess([],101,raw,b''),name,marker))
            for code,output in [(1,raw),(0,raw),(101,raw.replace(marker.encode(),b'other')),(101,raw.replace(b'0 ignored',b'1 ignored')),(101,b'compile error')]:
                self.assertFalse(mutations.asserted_failure(subprocess.CompletedProcess([],code,output,b''),name,marker))
        with self.assertRaisesRegex(AssertionError,'anchor count'):mutations.replace_once('x x','x','y')

    def test_existing_legacy_and_tests_are_unchanged(self):
        import hashlib
        for relative,expected in [("plugins/dev-workflow/skills/ship-task/scripts/loop-state.py",CHECK.LEGACY_SHA256),("scripts/test_loop_state.py","6401da9382a2da2d0ea5ed5d4356e7488aec9d6cc018d838751f5ffcbf565e92"),("scripts/check_loop_state_mutations.py","7aaf7a70e21facf8838b5b2a91e1f9496793a5b2f59c5aabbbb553d4a5ff033c")]:
            self.assertEqual(hashlib.sha256((CHECK.ROOT/relative).read_bytes()).hexdigest(),expected,relative)

    def test_legacy_runs_with_comparator_python_and_fixed_name(self):
        captured=[]
        def fake(command,*args,**kwargs):
            captured.append(list(command));return subprocess.CompletedProcess(command,0,b'',b'')
        with mock.patch.object(CHECK,'_RUN',fake):
            CHECK.Comparison('/owned/fixture').run(['python3',str(CHECK.LEGACY),'stop-control'])
        self.assertEqual(captured[0][:2],[CHECK.sys.executable,str(CHECK.LEGACY)])
        self.assertEqual(Path(captured[0][1]).name,CHECK.LEGACY_NAME)
        with tempfile.TemporaryDirectory(prefix='s2l-legacy-name-') as temporary:
            # 同じ旧スクリプトでも、別の名前で起動したものは固定名へ対応づけない。
            other=Path(temporary)/'loop_state.py';other.symlink_to(CHECK.LEGACY)
            with mock.patch.object(CHECK,'_RUN',fake),self.assertRaisesRegex(CHECK.Mismatch,'S2L_LEGACY_NAME'):
                CHECK.Comparison('/owned/fixture').run(['python3',str(other),'stop-control'])
        record=CHECK.interpreter_record()
        self.assertEqual(record['sitecustomize'],'absent');self.assertIn('RLIMIT_NOFILE',record['inherited_limits'])

    def test_legacy_child_keeps_pyyaml_version_and_path_after_home_change(self):
        import os,yaml
        with tempfile.TemporaryDirectory(prefix='s2l-child-yaml-')as temporary:
            captured=[]
            def fake(command,*args,**kwargs):
                captured.append(kwargs['env']);return subprocess.CompletedProcess(command,0,b'',b'')
            with mock.patch.object(CHECK,'_RUN',fake):
                CHECK.Comparison('/owned/fixture').run(['python3',str(CHECK.LEGACY),'stop-control'],env={**os.environ,'HOME':temporary})
            self.assertEqual(captured[0],captured[1])
            result=subprocess.run([CHECK.sys.executable,'-B','-c',"import pathlib,yaml;print(yaml.__version__);print(pathlib.Path(yaml.__file__).resolve())"],env=captured[0],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8,check=True)
            self.assertEqual(result.stdout,('6.0.3\n'+str(Path(yaml.__file__).resolve())+'\n').encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    if args.fixture is not None:
        CHECK.environment()
        if not args.fixture.is_file():
            parser.error("今回作成したexampleが必要です")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ComparisonTest))
    return int(not result.wasSuccessful() or bool(result.skipped) or result.testsRun != 26)


if __name__ == "__main__":
    raise SystemExit(main())
