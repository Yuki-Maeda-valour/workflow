#!/usr/bin/env python3
"""check_r4a_rest.py(R4A 残り 5 本の比較器)の自己検査。

比較器が、不一致・欠落・重複・誤った対応づけ・前提の外の環境変数を成功にしないことを確かめる。
Rust の実行ファイルを使う検査は、環境変数 R4A_REST_FIXTURE(絶対パス)を必須とする。
指定が無いとき、その検査は失敗する(skip しない)。
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

# 比較器は、自分の自己検査(このファイル)を比較の前に流す。このファイルの中から比較器を子として動かすときは、再帰させない。
os.environ["R4A_REST_IN_SELFTEST"] = "1"

import check_r4a_rest as checker  # noqa: E402


def fixture_binary():
    value = os.environ.get("R4A_REST_FIXTURE", "")
    if not value or not Path(value).is_absolute() or not os.access(value, os.X_OK):
        raise AssertionError("R4A_REST_FIXTURE に専用 example の絶対パスを指定する")
    return Path(value)


def assert_only_pending(test, result):
    """不一致が無いこと。接続待ちが残るときは終了 2(未完)で、合格(0)にはならない。"""
    test.assertNotIn("不一致", result.stderr, result.stderr[-1500:])
    test.assertIn(result.returncode, (0, 2), result.stderr[-800:])
    if result.returncode == 2:
        test.assertIn("結果: 未完", result.stdout)
        test.assertIn("接続待ち", result.stdout)
    else:
        test.assertIn("部分比較", result.stdout)


def run_checker(binary, *extra):
    # 子は一時フォルダの中で動かす(リポジトリに何も残さない)。
    with tempfile.TemporaryDirectory(prefix="r4a-rest-test-cwd-") as cwd:
        return subprocess.run([sys.executable, "-B", str(HERE / "check_r4a_rest.py"), "--fixture", str(binary), *extra],
                              capture_output=True, text=True, timeout=300, cwd=cwd)


class RequiredCases(unittest.TestCase):
    def test_required_set_equals_defined_cases(self):
        required = list(json.loads((checker.FIXTURES / "required_cases.json").read_text(encoding="utf-8"))["cases"])
        ids = [c.id for c in checker.all_cases(["resolve-task-store", "host-argv", "candidate-keys",
                                                  "loop-task-text", "loop-child-output"])]
        self.assertEqual(len(ids), len(set(ids)), "case ID の重複")
        self.assertEqual(sorted(ids), sorted(required))

    def test_every_traceback_case_has_fixed_frames_for_target_cases(self):
        names = {c.id for c in checker.all_cases(["resolve-task-store"]) if c.frames}
        for wanted in ("store/args-value-eq-dd", "store/trace-deep-array-995", "store/stdout-full-unbuffered-help",
                       "store/trace-value-eq-dd"):
            self.assertIn(wanted, names)


class NfcAgainstPython(unittest.TestCase):
    """Rust の NFC が Python の `unicodedata.normalize("NFC")`(Unicode 13)と一致する。"""

    def test_random_strings_match(self):
        import random
        import unicodedata
        self.assertEqual(unicodedata.unidata_version, "13.0.0", "Unicode 13.0.0 の Python で流す")
        rnd = random.Random(13)
        pool = [chr(c) for c in range(0x300, 0x370)]
        pool += [chr(c) for c in (0x41, 0x61, 0x65, 0x6F, 0x1100, 0x1161, 0x11A8, 0xAC00, 0xAC01, 0x1E9B, 0x0F73, 0x0F75,
                                 0x0344, 0x212B, 0x2126, 0x1F80, 0x0958, 0x09C7, 0x09BE, 0x0B47, 0x0B3E, 0x20, 0x3099,
                                 0x30AB, 0x0D46, 0x0DD9, 0x0DCA, 0x5D0)]
        pool += [chr(c) for c in rnd.sample(range(0xC0, 0x2000), 300)]
        pool += [chr(c) for c in rnd.sample(range(0x1E00, 0x2200), 200)]
        lines = ["".join(rnd.choice(pool) for _ in range(rnd.randint(0, 8))) for _ in range(8000)]
        out = subprocess.run([str(fixture_binary()), "nfc"], input="\n".join(lines).encode(), capture_output=True,
                             timeout=60).stdout.decode().split("\n")[:-1]
        self.assertEqual(len(out), len(lines))
        for line, got in zip(lines, out):
            self.assertEqual(got, unicodedata.normalize("NFC", line), repr(line))


class GeneratedTables(unittest.TestCase):
    """固定表(Unicode 13 の文字の性質・NFC・errno)が、生成器の再実行・Python の結果と一致する。"""

    UNICODE = HERE.parent / "crates/cli/src/commands/r4a_rest_support/unicode"

    def regenerate(self, script):
        result = subprocess.run([sys.executable, "-B", str(self.UNICODE / script)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        return result.stdout

    def test_unicode_and_nfc_tables_equal_a_fresh_generation(self):
        import unicodedata
        self.assertEqual(unicodedata.unidata_version, "13.0.0", "Unicode 13.0.0 の Python で流す")
        for script, target in (("generate.py", "unicode13.rs"), ("generate_nfc.py", "nfc13.rs")):
            fresh = self.regenerate(script)
            self.assertEqual(fresh, (self.UNICODE / target).read_text(encoding="utf-8"), f"{target} が生成器の出力と違う")
            digest = re.search(r'TABLE_SHA256: &str = "([0-9a-f]{64})"', fresh)
            self.assertIsNotNone(digest, target)
            self.assertIn(digest.group(1), (self.UNICODE / target).read_text(encoding="utf-8"))

    def test_errno_table_equals_a_fresh_generation_on_linux(self):
        if sys.platform != "linux":
            # 生成元は Linux の glibc だけ(Mac は Apple Libc の表 errno_macos.rs を使い、別の生成器と検査がある)。この生成器は Linux 以外で止まる。
            result = subprocess.run([sys.executable, "-B", str(self.UNICODE / "generate_errno.py")], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            return
        self.assertEqual(self.regenerate("generate_errno.py"), (self.UNICODE / "errno_linux.rs").read_text(encoding="utf-8"))

    def test_apple_errno_table_shape_and_generator(self):
        text = (self.UNICODE / "errno_macos.rs").read_text(encoding="utf-8")
        self.assertIn("Libc-1725.40.4", text)
        entries = re.findall(r'^    "(.*)",$', text, flags=re.M)
        self.assertEqual(len(entries), 108)
        self.assertIn("[&str; 108]", text)
        self.assertEqual((entries[0], entries[47], entries[107]),
                         ("Undefined error: 0", "Address family not supported by protocol family", "Capabilities insufficient"))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "errlst.c"
            source.write_text('const char *const sys_errlist[] = {\n\t"A",\t/* 0 */\n/* c */\n\t\t\t/* 1 */\n\t"B b",\n};\n')
            result = subprocess.run([sys.executable, "-B", str(self.UNICODE / "generate_errno_macos.py"), str(source), "L-1"],
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[&str; 2]', result.stdout)
        self.assertIn('    "A",\n    "B b",\n', result.stdout)

    def test_character_properties_equal_python_for_every_code_point(self):
        import unicodedata
        data = subprocess.run([str(fixture_binary()), "chartable"], capture_output=True, timeout=120).stdout
        self.assertEqual(len(data), 0x110000)
        for code in range(0x110000):
            ch = chr(code)
            digit = unicodedata.decimal(ch, None)
            want = int(ch.isprintable()) | (int(ch.isspace()) << 1) | (((digit + 1) if digit is not None else 0) << 2)
            if data[code] != want:
                self.fail(f"U+{code:04X}: Rust {data[code]:#x}・Python {want:#x}")


class Bytecode(unittest.TestCase):
    """bytecode の case は、旧版を -B も PYTHONDONTWRITEBYTECODE も無しで動かし、旧スクリプト自身が __pycache__ を作らないことを確かめる。"""

    def run_copy(self, edit):
        source = checker.SCRIPTS["candidate-keys"].parent
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "scripts"
            copy.mkdir()
            for name in ("candidate-keys.py", "resolve-task-dir.py", "task-digest.py"):
                (copy / name).write_text(edit((source / name).read_text(encoding="utf-8")) if name == "candidate-keys.py"
                                         else (source / name).read_text(encoding="utf-8"), encoding="utf-8")
            saved = checker.SCRIPTS["candidate-keys"]
            checker.SCRIPTS["candidate-keys"] = copy / "candidate-keys.py"
            try:
                case = checker.Case("x", "candidate-keys", ["--task-dir", "absent"], bytecode=True)
                command = checker.legacy_command(case)
                self.assertNotIn("-B", command)
                env = checker.child_env(case)
                self.assertNotIn("PYTHONDONTWRITEBYTECODE", env)
                before = checker.snapshot(copy)
                result = subprocess.run(command, env=env, capture_output=True, cwd=directory, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                return before != checker.snapshot(copy)
            finally:
                checker.SCRIPTS["candidate-keys"] = saved

    def test_script_does_not_write_bytecode_by_itself(self):
        self.assertFalse(self.run_copy(lambda text: text))

    def test_case_fails_when_the_script_no_longer_stops_bytecode(self):
        self.assertIn("sys.dont_write_bytecode = True", checker.SCRIPTS["candidate-keys"].read_text(encoding="utf-8"))
        self.assertTrue(self.run_copy(lambda text: text.replace("sys.dont_write_bytecode = True", "pass")))


class NoLocalPaths(unittest.TestCase):
    """リポジトリに入れる専用ファイル(記録・固定期待)に、作業機の絶対パスが無い(hex・base64・UTF-16 も解いて確かめる)。"""

    def scan_targets(self):
        roots = [HERE / "r4a_rest_fixtures", HERE / "check_r4a_rest.py", HERE / "test_r4a_rest.py",
                 HERE.parent / "crates/cli/src/commands/r4a_rest_support", HERE.parent / "crates/cli/examples/r4a_rest_contract.rs",
                 HERE.parent / "crates/platform/src/r4a_rest.rs"]
        for root in roots:
            if root.is_file():
                yield root
            else:
                yield from (p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)

    def test_no_absolute_paths_of_this_machine(self):
        import base64
        import binascii
        # 引き継ぎ先などの手元のパスは、実行時に環境から求める(リポジトリに入れる文字列に、手元のフォルダの名前を書かない)。
        needles = [str(Path.home()), str(HERE.parents[1]), "/home/", "/Users/", str(Path(sys.executable).parent.parent)]
        python_override = os.environ.get("R4A_REST_PYTHON")
        if python_override:
            needles.append(str(Path(python_override).resolve().parents[3]))
        needles = [n for n in needles if len(n) > 4 and n != "/"]
        variants = []
        for needle in needles:
            raw = needle.encode()
            variants += [raw, needle.encode("utf-16-le"), needle.encode("utf-16-be")]
        found = []
        for path in self.scan_targets():
            if path.name == "test_r4a_rest.py":
                continue  # このテスト自身は、探す文字列を持つ
            data = path.read_bytes()
            candidates = [data]
            for match in re.finditer(rb"[0-9a-f]{16,}", data):
                try:
                    candidates.append(binascii.unhexlify(match.group(0)))
                except (binascii.Error, ValueError):
                    pass
            for match in re.finditer(rb"[A-Za-z0-9+/]{24,}={0,2}", data):
                try:
                    candidates.append(base64.b64decode(match.group(0), validate=False))
                except (binascii.Error, ValueError):
                    pass
            for candidate in candidates:
                if any(v in candidate for v in variants):
                    found.append(str(path))
                    break
        self.assertEqual(found, [])


class Environment(unittest.TestCase):
    def test_stripped_variables_are_removed_and_given_ones_are_kept(self):
        os.environ["PYTHONPATH"] = "/should/be/removed"
        os.environ["PYTHONUNBUFFERED"] = "should-be-replaced"
        try:
            case = checker.Case("x", "host-argv", [], env={"PYTHONUNBUFFERED": "1"})
            env = checker.child_env(case)
        finally:
            del os.environ["PYTHONPATH"]
            del os.environ["PYTHONUNBUFFERED"]
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_every_python_env_name_read_by_the_interpreter_is_in_a_fixed_list(self):
        names = checker.check_env_lists()
        self.assertIn("PYTHONHASHSEED", names)
        saved = list(checker.STRIPPED_ENV)
        try:
            checker.STRIPPED_ENV.remove("PYTHONMALLOCSTATS")
            with self.assertRaises(checker.Failure):
                checker.check_env_lists()
        finally:
            checker.STRIPPED_ENV[:] = saved

    def test_unknown_python_variables_are_removed_from_the_child_env(self):
        os.environ["PYTHONSOMETHINGNEW"] = "1"
        os.environ["PYTHONOPTIMIZE"] = "2"
        try:
            env = checker.child_env(checker.Case("x", "host-argv", []))
        finally:
            del os.environ["PYTHONSOMETHINGNEW"]
            del os.environ["PYTHONOPTIMIZE"]
        self.assertNotIn("PYTHONSOMETHINGNEW", env)
        self.assertNotIn("PYTHONOPTIMIZE", env)  # 再現する変数も、case が与えたものだけが残る

    def test_rlimits_are_recorded_not_set(self):
        import resource
        before = resource.getrlimit(resource.RLIMIT_NOFILE)
        text = checker.record_rlimits()
        self.assertEqual(before, resource.getrlimit(resource.RLIMIT_NOFILE))
        self.assertIn("RLIMIT_AS=", text)

    def test_case_giving_a_stripped_variable_fails(self):
        case = checker.Case("x", "host-argv", [], env={"PYTHONPATH": "/x"})
        with self.assertRaises(checker.Failure):
            checker.child_env(case)


class Mapping(unittest.TestCase):
    SCRIPT = "/legacy/dir/resolve-task-store.py"
    LIB = "/py/lib/python3.10"

    def mapper(self):
        return checker.Mapper(self.SCRIPT, [self.LIB])

    def trace(self, frames, error=b"TypeError: boom"):
        lines = [b"Traceback (most recent call last):"]
        for path, line, func in frames:
            lines.append(b'  File "' + path + b'", line ' + str(line).encode() + b", in " + func)
            lines.append(b"    source line")
        lines.append(error)
        return b"\n".join(lines) + b"\n"

    GOOD = [(b"/legacy/dir/resolve-task-store.py", 41, b"<module>"), (b"/py/lib/python3.10/json/__init__.py", 339, b"loads")]
    WANT = [[("resolve-task-store.py", 41, "<module>"), ("json/__init__.py", 339, "loads")]]

    def test_maps_only_expected_frames(self):
        out, _ = self.mapper().apply(self.trace(self.GOOD), self.WANT)
        self.assertIn(b'File "resolve-task-store.py", line 41', out)
        self.assertIn(b'File "json/__init__.py", line 339', out)
        self.assertNotIn(b"/legacy/dir", out)

    def test_rejects_wrong_fixed_name_line_function_count(self):
        bad_name = [[("resolve-task-store.py", 41, "<module>"), ("json/decoder.py", 339, "loads")]]
        bad_line = [[("resolve-task-store.py", 41, "<module>"), ("json/__init__.py", 340, "loads")]]
        bad_func = [[("resolve-task-store.py", 41, "<module>"), ("json/__init__.py", 339, "dumps")]]
        short = [[("resolve-task-store.py", 41, "<module>")]]
        for want in (bad_name, bad_line, bad_func, short, self.WANT + self.WANT):
            with self.assertRaises(checker.Failure, msg=str(want)):
                self.mapper().apply(self.trace(self.GOOD), want)

    def test_string_frames_of_held_code_are_compared_unchanged(self):
        stderr = (b"Traceback (most recent call last):\n  File \"<string>\", line 99, in <module>\nKeyError: 'x'\n")
        mapper = checker.Mapper("/legacy/dir/loop-task-text.py", [])
        out, frames = mapper.apply(stderr, [[("<string>", 99, "<module>")]])
        self.assertEqual(out, stderr)
        with self.assertRaises(checker.Failure):
            mapper.apply(stderr, [[("<string>", 98, "<module>")]])
        with self.assertRaises(checker.Failure):  # `<string>` の期待に、別のパスを当てない
            mapper.apply(stderr.replace(b"<string>", b"/x/<string>"), [[("<string>", 99, "<module>")]])

    def test_rejects_unrelated_paths(self):
        for path in (b"/work/user/file.py", b"/py/lib/python3.10/../evil/json/__init__.py",
                     b"/py/lib/python3.10x/json/__init__.py", b"/py/lib/python3.10/other.py"):
            frames = [self.GOOD[0], (path, 339, b"loads")]
            with self.assertRaises(checker.Failure, msg=str(path)):
                self.mapper().apply(self.trace(frames), self.WANT)

    def test_fake_file_line_inside_exception_text_is_not_a_frame(self):
        error = b'ValueError: x\n  File "/legacy/dir/resolve-task-store.py", line 41, in <module>'
        stderr = self.trace(self.GOOD, error)
        out, frames = self.mapper().apply(stderr, self.WANT)
        self.assertEqual(len(frames), 2)
        # 例外本文の中の偽のフレームは、そのまま残る。
        self.assertIn(b'ValueError: x\n  File "/legacy/dir/resolve-task-store.py", line 41', out)

    def test_chained_tracebacks_are_counted_separately(self):
        first = self.trace(self.GOOD)
        chain = b"\nDuring handling of the above exception, another exception occurred:\n\n"
        stderr = first + chain + self.trace(self.GOOD[:1])
        out, frames = self.mapper().apply(stderr, self.WANT + [[self.WANT[0][0]]])
        self.assertEqual(len(frames), 3)
        with self.assertRaises(checker.Failure):
            self.mapper().apply(stderr, self.WANT)

    def test_crlf_lines_are_parsed_by_the_same_rule_and_keep_their_cr(self):
        stderr = self.trace(self.GOOD).replace(b"\n", b"\r\n")
        out, frames = self.mapper().apply(stderr, self.WANT)
        self.assertEqual(len(frames), 2)
        self.assertIn(b'File "resolve-task-store.py", line 41, in <module>\r\n', out)
        self.assertIn(b'File "json/__init__.py", line 339, in loads\r\n', out)
        with self.assertRaises(checker.Failure):  # CRLF でも、期待と違う行番号は拒否する
            self.mapper().apply(stderr, [[("resolve-task-store.py", 40, "<module>"), ("json/__init__.py", 339, "loads")]])

    def test_sibling_script_fields_are_mapped_by_exact_string(self):
        parent = os.path.realpath(HERE)  # 実在するディレクトリ
        script = parent + "/candidate-keys.py"
        mapper = checker.Mapper(script, [])
        stderr = (b"Traceback (most recent call last):\n  File \"" + os.fsencode(parent) + b"/resolve-task-dir.py\", line 7, in f\n"
                  b"  File \"" + os.fsencode(parent) + b"/other.py\", line 1, in g\nValueError\n")
        with self.assertRaises(checker.Failure):  # 兄弟でないパスは対応づけない
            mapper.apply(stderr, [[("resolve-task-dir.py", 7, "f"), ("other.py", 1, "g")]])
        out, _ = mapper.apply(stderr.replace(b"other.py", b"task-digest.py"),
                              [[("resolve-task-dir.py", 7, "f"), ("task-digest.py", 1, "g")]])
        self.assertIn(b'File "resolve-task-dir.py", line 7', out)


class SelftestGate(unittest.TestCase):
    """比較器が、自分の自己検査を比較の前に流し、落ちたら比較を止める。"""

    def run_gate(self, body, *, in_selftest=False, no_selftest=False):
        import types
        with tempfile.TemporaryDirectory() as directory:
            test_file = Path(directory) / "inner_tests.py"
            test_file.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_it(self):\n        " + body + "\n")
            options = types.SimpleNamespace(no_selftest=no_selftest, list_cases=False, write_required=False)
            saved = {name: os.environ.get(name) for name in ("R4A_REST_IN_SELFTEST", "R4A_REST_FIXTURE")}
            os.environ.pop("R4A_REST_IN_SELFTEST", None)
            if in_selftest:
                os.environ["R4A_REST_IN_SELFTEST"] = "1"
            try:
                checker.run_selftest(options, Path("/bin/true"), test_file=test_file)
            finally:
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_a_failing_selftest_stops_the_comparison(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_gate("self.fail('boom')")
        self.assertIn("比較は流さない", str(raised.exception))

    def test_a_passing_selftest_lets_the_comparison_continue(self):
        self.run_gate("pass")

    def test_the_gate_is_skipped_inside_a_selftest_and_when_asked(self):
        self.run_gate("self.fail('流れてはいけない')", in_selftest=True)
        self.run_gate("self.fail('流れてはいけない')", no_selftest=True)


class ShimBuild(unittest.TestCase):
    def test_environment_variable_depends_on_the_os(self):
        saved = sys.platform
        try:
            sys.platform = "darwin"
            self.assertEqual(checker.shim_env("/x/s.dylib"), {"DYLD_INSERT_LIBRARIES": "/x/s.dylib"})
            sys.platform = "linux"
            self.assertEqual(checker.shim_env("/x/s.so"), {"LD_PRELOAD": "/x/s.so"})
        finally:
            sys.platform = saved

    def test_effective_shim_is_detected_and_ineffective_one_is_not(self):
        if sys.platform not in ("linux", "darwin"):
            self.skipTest("差込みの対象 OS でない")
        with tempfile.TemporaryDirectory() as directory:
            shim = checker.build_shim(directory)
            self.assertIsNotNone(shim, "この OS で差込みライブラリを作れて効くこと")
            self.assertTrue(checker.shim_is_effective(shim))
            empty = Path(directory) / "empty.c"
            empty.write_text("int r4a_unused;\n")
            flags = ["-shared", "-fPIC"] if sys.platform == "linux" else ["-dynamiclib"]
            ineffective = Path(directory) / "ineffective.lib"
            subprocess.run(["cc", *flags, "-o", str(ineffective), str(empty)], check=True, capture_output=True)
            self.assertFalse(checker.shim_is_effective(ineffective))


class ShimPolicy(unittest.TestCase):
    def test_effectiveness_is_fixed_to_true_on_linux_and_mac(self):
        self.assertEqual(checker.SHIM_EXPECTED_EFFECTIVE, {"linux": True, "darwin": True})

    def test_missing_compiler_is_a_failure_on_every_supported_os(self):
        import unittest.mock as mock
        for platform in ("linux", "darwin"):
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(checker.sys, "platform", platform), \
                    mock.patch.object(checker.subprocess, "run", side_effect=FileNotFoundError("cc")):
                with self.assertRaises(SystemExit) as raised:
                    checker.build_shim(directory)
                self.assertIn("失敗にする", str(raised.exception))

    def test_a_shim_that_is_not_effective_stops_the_comparison_when_expected_effective(self):
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(checker.subprocess, "run"), \
                mock.patch.object(checker, "shim_is_effective", return_value=False):
            with self.assertRaises(SystemExit) as raised:
                checker.build_shim(directory)
            self.assertIn("固定の期待と違う", str(raised.exception))

    def test_linux_only_cases_are_classified_and_listed(self):
        required = json.loads((checker.FIXTURES / "required_cases.json").read_text(encoding="utf-8"))
        cases = checker.all_cases(["resolve-task-store", "host-argv", "candidate-keys", "loop-task-text", "loop-child-output"])
        linux_only = sorted(c.id for c in cases if checker.needs_linux(c))
        self.assertEqual(linux_only, required["requires_linux"])
        self.assertTrue(any(c.stdout == "full" for c in cases if checker.needs_linux(c)))
        self.assertTrue(any(c.signal for c in cases if checker.needs_linux(c)))
        # SIG_IGN・FIFO・満杯の pipe の case が必須の集合にある。
        for needed in ("signal/child-int-ignored-stdin-read", "signal/candidate-int-fifo-open", "signal/task-int-fifo-open",
                       "signal/child-int-fifo-open", "signal/candidate-int-full-pipe-buffered",
                       "signal/candidate-int-full-pipe-unbuffered"):
            self.assertIn(needed, required["cases"])

    def test_no_case_is_left_pending(self):
        cases = checker.all_cases(["resolve-task-store", "host-argv", "candidate-keys", "loop-task-text", "loop-child-output"])
        self.assertEqual([c.id for c in cases if c.pending], [])


class FaultRules(unittest.TestCase):
    """書込みの故障の期待を作る部品(規則・欄の途中の切れ・対応づけの対象)。"""

    FRAME = b'  File "/legacy/dir/resolve-task-store.py", line 41, in <module>\n'

    def mapper(self):
        return checker.Mapper("/legacy/dir/resolve-task-store.py", ["/py/lib/python3.10"])

    def test_rules(self):
        pieces = [b"Traceback\n", b"abcdef", b"ghi"]
        self.assertEqual(checker.fault_rule("budget", 12, pieces, True), b"Traceback\nab")
        self.assertEqual(checker.fault_rule("budget", 0, pieces, True), b"")
        self.assertEqual(checker.fault_rule("cut-first", 4, pieces, False), b"Trac" + b"abcdefghi")
        self.assertEqual(checker.fault_rule("cut-first", 4, pieces, True), b"".join(pieces))  # 送り直す
        self.assertEqual(checker.fault_rule("cut-first", 100, pieces, False), b"".join(pieces))
        self.assertEqual(checker.fault_rule("short-fail", 4, pieces, False), b"Trac")
        self.assertEqual(checker.fault_rule("short-fail", 100, pieces, False), b"".join(pieces))
        with self.assertRaises(checker.Failure):
            checker.fault_rule("unknown", 1, pieces, False)

    def test_only_whole_frame_pieces_for_known_paths_are_mapped(self):
        fake = b'  File "/x/y.py", line 1, in z\n'
        text = b'ValueError: a\n  File "/legacy/dir/resolve-task-store.py", line 41, in <module>\n'
        mapped = checker.map_pieces(self.mapper(), [self.FRAME, fake, text, b"  File ", b'"/legacy/dir/resolve-task-store.py"'])
        self.assertEqual(mapped[0], b'  File "resolve-task-store.py", line 41, in <module>\n')
        self.assertEqual(mapped[1:], [fake, text, b"  File ", b'"/legacy/dir/resolve-task-store.py"'])

    def test_budget_cut_inside_a_replaced_field_differs_between_old_and_new(self):
        pieces = [self.FRAME]
        mapped = checker.map_pieces(self.mapper(), pieces)
        old = checker.fault_rule("budget", 20, pieces, False)
        new = checker.fault_rule("budget", 20, mapped, False)
        self.assertEqual(old, self.FRAME[:20])
        self.assertEqual(new, mapped[0][:20])
        self.assertNotEqual(old, new)  # 欄の長さが違うので、同じ上限でも切れる位置の中身が違う


class GluedLines(unittest.TestCase):
    """故障で LF が通らなかったとき、次の行は前の行の続きに付く。その出力だけ、構造の解析が続きの Frame を読む。"""

    def mapper(self, glued):
        mapper = checker.Mapper("/legacy/dir/resolve-task-store.py", [])
        mapper.glued_header = glued
        return mapper

    STDERR = (b"Traceback (most recent call last):\n  File \"/legacy/dir/resolve-task-store.py\", line 41, in <module>\n"
              b"    sys.exit(main())  File \"/legacy/dir/resolve-task-store.py\", line 28, in main\n    x\nTypeError: y\n")

    def test_glued_frame_is_mapped_only_in_glued_mode_and_keeps_its_prefix(self):
        lines, frames = self.mapper(False).parse(self.STDERR)
        self.assertEqual(len(frames), 1)
        mapper = self.mapper(True)
        out = mapper.map_expected(self.STDERR, [[("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 28, "main")]])
        self.assertIn(b'    sys.exit(main())  File "resolve-task-store.py", line 28, in main\n', out)
        self.assertNotIn(b"/legacy/dir", out)

    def test_glued_frame_not_in_the_expectation_is_left_alone(self):
        out = self.mapper(True).map_expected(self.STDERR, [[("resolve-task-store.py", 41, "<module>")]])
        self.assertIn(b'sys.exit(main())  File "/legacy/dir/resolve-task-store.py", line 28', out)

    def test_glued_exception_line_still_reaches_the_chained_traceback(self):
        text = (b"Traceback (most recent call last):\n  File \"/legacy/dir/resolve-task-store.py\", line 1, in a\n"
                b"    raise E(1)E: m\n\nDuring handling of the above exception, another exception occurred:\n\n"
                b"Traceback (most recent call last):\n  File \"/legacy/dir/resolve-task-store.py\", line 41, in <module>\nOSError\n")
        _, frames = self.mapper(False).parse(text)
        self.assertEqual({f[4] for f in frames}, {0, 1})


class ThreadLine(unittest.TestCase):
    """`Current thread` の限定した比べ方(位置と形式だけ)。"""

    GOOD = (b"Fatal Python error: x\nPython runtime state: core initialized\n\n"
            b"Current thread 0x00007f0000001234 (most recent call first):\n  <no Python frame>\n")

    def test_value_is_hidden_but_position_and_format_are_checked(self):
        self.assertIn(b"0x<TID>", checker.normalize_thread(self.GOOD))
        bad = {
            "欠落": self.GOOD.replace(b"Current thread", b"Current"),
            "桁が足りない": self.GOOD.replace(b"0x00007f0000001234", b"0x0000f0000001234"),
            "桁が多い": self.GOOD.replace(b"0x00007f0000001234", b"0x000007f0000001234"),
            "大文字": self.GOOD.replace(b"0x00007f0000001234", b"0x00007F0000001234"),
            "文字が違う": self.GOOD.replace(b"0x00007f0000001234", b"0x00007g0000001234"),
            "2 回": self.GOOD + b"Current thread 0x00007f0000001234 (most recent call first):\n",
            "余計な行": self.GOOD + b"extra\n",
            "前の空行なし": self.GOOD.replace(b"\n\nCurrent", b"\nCurrent"),
            "frame の行": self.GOOD.replace(b"  <no Python frame>", b"  File x"),
            "後ろの文字": self.GOOD.replace(b"first):", b"first): "),
        }
        for label, text in bad.items():
            with self.assertRaises(checker.Failure, msg=label):
                checker.normalize_thread(text)


class Judges(unittest.TestCase):
    def test_same_wrong_value_on_both_sides_fails_the_fixed_expectation(self):
        case = checker.legacy_child_cases()[0]
        wrong = (0, b"wrong", b"")
        with self.assertRaises(checker.Failure):
            case.judge(wrong)

    def test_signal_judgement_requires_signal_exit_last_line_and_stdout(self):
        case = checker.Case("x", "loop-task-text", [], signal=("INT", b""))
        good = (-2, b"", b"Traceback\n  File x\nKeyboardInterrupt\n")
        checker.judge_signal(case, good, good)
        for bad in ((130, b"", good[2]), (-2, b"", b"KeyboardInterrupt\nafter\n"), (-2, b"", b"")):
            with self.assertRaises(checker.Failure):
                checker.judge_signal(case, good, bad)
        with self.assertRaises(checker.Failure):
            checker.judge_signal(case, good, (-2, b"x", good[2]))
        term = checker.Case("y", "loop-task-text", [], signal=("TERM", b""))
        checker.judge_signal(term, (-15, b"", b""), (-15, b"", b""))
        with self.assertRaises(checker.Failure):
            checker.judge_signal(term, (-15, b"", b""), (-15, b"", b"x"))


class EndToEnd(unittest.TestCase):
    def wrapper(self, directory, body):
        path = Path(directory) / "fixture"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_passthrough_passes_and_reports_partial_comparison(self):
        result = run_checker(fixture_binary(), "--only", "host-argv")
        assert_only_pending(self, result)
        self.assertIn("部分比較", result.stdout)

    def test_extra_stderr_byte_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.wrapper(directory, f'"{fixture_binary()}" "$@"\nrc=$?\nprintf x >&2\nexit $rc\n')
            result = run_checker(fixture, "--only", "host-argv")
        self.assertEqual(result.returncode, 1)
        self.assertIn("標準エラーが違う", result.stderr)

    def test_candidate_and_child_output_differences_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.wrapper(
                directory,
                f'out=$("{fixture_binary()}" "$@")\nrc=$?\nprintf "%sX" "$out"\nexit $rc\n',
            )
            for helper in ("candidate-keys", "loop-child-output"):
                result = run_checker(fixture, "--only", helper)
                self.assertEqual(result.returncode, 1, helper)
                self.assertIn("不一致", result.stderr, helper)

    def test_input_change_and_timeout_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.wrapper(directory, f'touch changed-by-fixture\nexec "{fixture_binary()}" "$@"\n')
            result = run_checker(fixture, "--only", "host-argv")
            self.assertEqual(result.returncode, 1)
            self.assertIn("入力・旧スクリプトの置き場が変わった", result.stderr)
            slow = self.wrapper(directory, "sleep 5\n")
            env = dict(os.environ, R4A_REST_CHILD_TIMEOUT="1")
            result = subprocess.run([sys.executable, "-B", str(HERE / "check_r4a_rest.py"), "--fixture", str(slow),
                                     "--only", "host-argv"], capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("時間切れ", result.stdout + result.stderr)

    def test_mtime_only_and_relink_changes_of_inputs_are_rejected(self):
        real = fixture_binary()
        with tempfile.TemporaryDirectory() as directory:
            touch = Path(directory) / "touch"
            touch.write_text('#!/bin/sh\ntouch -d "2001-01-01 00:00:00" sentinel.txt\nexec "' + str(real) + '" "$@"\n')
            touch.chmod(0o755)
            result = run_checker(touch, "--only", "host-argv")
            self.assertEqual(result.returncode, 1)
            self.assertIn("入力・旧スクリプトの置き場が変わった", result.stderr)
            relink = Path(directory) / "relink"
            relink.write_text('#!/bin/sh\nif [ -L dirlink ]; then ln -sfn nowhere dirlink; fi\nexec "' + str(real) + '" "$@"\n')
            relink.chmod(0o755)
            result = run_checker(relink, "--only", "candidate-keys")
        self.assertEqual(result.returncode, 1)
        self.assertIn("入力・旧スクリプトの置き場が変わった", result.stderr)

    def test_snapshot_records_link_text_ids_and_times_and_ignores_read_time(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "f").write_text("x")
            os.symlink("f", base / "l")
            first = checker.snapshot(base)
            self.assertEqual(first, checker.snapshot(base))
            (base / "f").read_text()  # 読取りだけでは変わらない(atime は記録しない)
            self.assertEqual(first, checker.snapshot(base))
            os.utime(base / "f", ns=(1, 2_000_000_000))
            self.assertNotEqual(first, checker.snapshot(base))
            second = checker.snapshot(base)
            os.unlink(base / "l")
            os.symlink("other", base / "l")
            self.assertNotEqual(second, checker.snapshot(base))
            third = checker.snapshot(base)
            (base / "__pycache__").mkdir()
            self.assertNotEqual(third, checker.snapshot(base))

    def test_missing_shim_on_linux_is_a_failure_not_an_unrun_case(self):
        if sys.platform != "linux":
            return
        env = dict(os.environ, PATH="/nonexistent")
        result = subprocess.run([sys.executable, "-B", str(HERE / "check_r4a_rest.py"), "--fixture", str(fixture_binary()),
                                 "--only", "resolve-task-store"], capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("失敗にする", result.stderr)

    def test_strerror_table_mismatch_fails_the_cases_that_print_errno_text(self):
        real = fixture_binary()
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "w"
            wrapper.write_text('#!/bin/sh\nif [ "$1" = strerror ]; then "' + str(real) + '" strerror | sed "s/No space left on device/No space/"; exit 0; fi\nexec "' + str(real) + '" "$@"\n')
            wrapper.chmod(0o755)
            result = run_checker(wrapper, "--only", "host-argv")
        self.assertEqual(result.returncode, 1)
        self.assertIn("strerror の表が比較機の旧版の文と違う", result.stderr)

    def test_closed_fd_cases_are_compared_against_fixed_old_and_new_expectations(self):
        # 起動時に閉じた fd の case は、Rust が /dev/null の期待(旧版を /dev/null で動かした結果)と違えば失敗する。
        real = fixture_binary()
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "w"
            # 標準出力が閉じて渡された(起動時に閉じた fd)ときだけ、標準エラーに余計な出力を足す。
            wrapper.write_text('#!/bin/sh\n"' + str(real) + '" "$@"; rc=$?\nif [ ! -e /proc/$$/fd/1 ]; then echo extra >&2; fi\nexit $rc\n')
            wrapper.chmod(0o755)
            result = run_checker(wrapper, "--only", "host-argv")
        self.assertEqual(result.returncode, 1)
        self.assertIn("closed-host", result.stderr)

    def test_nothing_is_left_in_the_repository_even_when_started_from_it(self):
        # 比較器をリポジトリの直下から起動し、子が作業場所の直下へファイルを作ろうとしても、リポジトリには何も残らない。
        repository = HERE.parents[1]
        before = sorted(p.name for p in repository.iterdir())
        before_native = sorted(p.name for p in (repository / "native").iterdir())
        real = fixture_binary()
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "w"
            wrapper.write_text('#!/bin/sh\ntouch changed-by-fixture sentinel.txt\nexec "' + str(real) + '" "$@"\n')
            wrapper.chmod(0o755)
            subprocess.run([sys.executable, "-B", str(HERE / "check_r4a_rest.py"), "--fixture", str(wrapper), "--only", "host-argv"],
                           capture_output=True, text=True, timeout=300, cwd=repository)
        self.assertEqual(sorted(p.name for p in repository.iterdir()), before)
        self.assertEqual(sorted(p.name for p in (repository / "native").iterdir()), before_native)
        self.assertFalse((repository / "sentinel.txt").exists() or (repository / "changed-by-fixture").exists())

    def test_fault_rule_violation_by_the_rust_side_is_rejected(self):
        # Rust が故障の規則(合計の上限)を無視して全部を出すと、固定名にした書込みの列の期待と合わず失敗する。
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.wrapper(directory, f'unset R4A_FAULT\nexec "{fixture_binary()}" "$@"\n')
            result = run_checker(fixture, "--only", "resolve-task-store")
        self.assertEqual(result.returncode, 1)
        self.assertIn("固定名にした書込みの列に同じ規則を当てた結果と違う", result.stderr)

    def test_thread_value_change_is_allowed_only_for_the_startup_failure_line(self):
        real = fixture_binary()
        with tempfile.TemporaryDirectory() as directory:
            # 標準入力が directory のとき(起動失敗の診断)だけ、`Current thread` の値を書き換える。
            ok = Path(directory) / "ok"
            ok.write_text(
                "#!/bin/sh\nif [ -d /dev/stdin ]; then\n"
                f'  t=$(mktemp); "{real}" "$@" 2>"$t"; rc=$?\n'
                "  sed -E 's/0x[0-9a-f]{16}/0x0123456789abcdef/' \"$t\" >&2; rm -f \"$t\"; exit $rc\nfi\n"
                f'exec "{real}" "$@"\n')
            ok.chmod(0o755)
            result = run_checker(ok, "--only", "host-argv")
            assert_only_pending(self, result)
            # 診断に `Current thread` の行をもう 1 回足すと、1 回でないので失敗する。
            twice = Path(directory) / "twice"
            twice.write_text(
                "#!/bin/sh\nif [ -d /dev/stdin ]; then\n"
                f'  "{real}" "$@"; rc=$?\n'
                "  echo 'Current thread 0x0000000000000001 (most recent call first):' >&2; exit $rc\nfi\n"
                f'exec "{real}" "$@"\n')
            twice.chmod(0o755)
            result = run_checker(twice, "--only", "host-argv")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Current thread の行が 1 回でない", result.stderr)

    def test_a_mismatch_in_a_formerly_pending_case_is_an_ordinary_failure(self):
        # 接続待ちの印はもう付いていない。端末幅の不一致(COLUMNS が不正値のときだけ幅を変える実行物)は、ふつうの不一致で、
        # 終了 1 になる。接続待ちとは報告しない。
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.wrapper(directory, f'[ "$COLUMNS" = x ] && export COLUMNS=20\nexec "{fixture_binary()}" "$@"\n')
            result = run_checker(fixture, "--only", "resolve-task-store")
            self.assertEqual(result.returncode, 1, result.stderr[-1500:])
            self.assertIn("不一致", result.stderr)
            self.assertIn("pty/store-help-tty-columns-x", result.stderr)
            self.assertNotIn("接続待ち", result.stdout)
            self.assertNotIn("結果: 合格", result.stdout)
        # 不一致が無い補助の部分比較は、終了 0(全件合格とは言わない)。
        partial = run_checker(fixture_binary(), "--only", "resolve-task-store")
        assert_only_pending(self, partial)
        self.assertIn("部分比較", partial.stdout)

    def test_exit_code_difference_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.wrapper(directory, f'"{fixture_binary()}" "$@" >/dev/null 2>&1\nexit 7\n')
            result = run_checker(fixture, "--only", "host-argv")
        self.assertEqual(result.returncode, 1)

    def test_same_wrong_value_on_both_sides_is_not_accepted_by_fixed_expectation(self):
        # 旧版と Rust が同じ誤った Traceback を出しても、固定期待(行番号)が違えば失敗する。
        case = checker.Case("store/x", "resolve-task-store", [], frames=[[("resolve-task-store.py", 99, "<module>")]])
        mapper = checker.Mapper("/s/resolve-task-store.py", [])
        stderr = (b"Traceback (most recent call last):\n"
                  b'  File "/s/resolve-task-store.py", line 41, in <module>\n    x\nE: y\n')
        with self.assertRaises(checker.Failure):
            mapper.apply(stderr, case.frames)

    def test_missing_or_non_executable_fixture_is_rejected(self):
        for value in ("/nonexistent/fixture", ""):
            result = run_checker(Path(value) if value else Path(""), "--only", "host-argv")
            self.assertNotEqual(result.returncode, 0)

    def test_unknown_helper_and_empty_required_set_are_rejected(self):
        result = run_checker(fixture_binary(), "--only", "nonexistent")
        self.assertNotEqual(result.returncode, 0)

    def test_unlisted_case_is_rejected(self):
        # 必須の一覧を使わせるため、一覧から 1 件を抜いた複製を作って比較器を起動する。
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "ci"
            copy.mkdir()
            (copy / "check_r4a_rest.py").write_text((HERE / "check_r4a_rest.py").read_text(encoding="utf-8"))
            fixtures = copy / "r4a_rest_fixtures"
            fixtures.mkdir()
            required = json.loads((checker.FIXTURES / "required_cases.json").read_text(encoding="utf-8"))
            del required["cases"]["host/args-empty"]
            (fixtures / "required_cases.json").write_text(json.dumps(required))
            result = subprocess.run([sys.executable, "-B", str(copy / "check_r4a_rest.py"), "--fixture",
                                     str(fixture_binary()), "--only", "host-argv"],
                                    capture_output=True, text=True, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("必須 case の集合と一致しない", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
