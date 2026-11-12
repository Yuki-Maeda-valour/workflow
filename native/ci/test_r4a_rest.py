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
        """自己検査の入口を呼ぶ。落ちたときは失敗の文、合格・省略は None を返す。"""
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
                return checker.run_selftest(options, Path("/bin/true"), test_file=test_file)
            finally:
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_a_failing_selftest_is_reported_but_does_not_stop_the_comparison(self):
        message = self.run_gate("self.fail('boom')")
        self.assertIn("自己検査が失敗した", message)
        self.assertIn("test_it", message)

    def test_a_passing_selftest_returns_nothing(self):
        self.assertIsNone(self.run_gate("pass"))

    def test_the_gate_is_skipped_inside_a_selftest_and_when_asked(self):
        self.assertIsNone(self.run_gate("self.fail('流れてはいけない')", in_selftest=True))
        self.assertIsNone(self.run_gate("self.fail('流れてはいけない')", no_selftest=True))

    def test_the_comparison_runs_even_if_the_selftest_fails_and_the_last_line_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            failing = Path(directory) / "failing_tests.py"
            failing.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail('x')\n")
            env = {k: v for k, v in os.environ.items() if k != "R4A_REST_IN_SELFTEST"}
            result = subprocess.run([sys.executable, "-B", str(HERE / "check_r4a_rest.py"), "--fixture", str(fixture_binary()),
                                     "--only", "host-argv", "--selftest-file", str(failing)],
                                    capture_output=True, text=True, timeout=300, cwd=directory, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("比べた case", result.stdout)  # 本体の比較は流れた
        last = [line for line in result.stdout.splitlines() if line.strip()][-1]
        self.assertTrue(last.startswith("結果: 自己検査が失敗した"), last)


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

    def test_the_shim_records_a_summary_of_the_calls_it_saw(self):
        if sys.platform not in ("linux", "darwin"):
            self.skipTest("差込みの対象 OS でない")
        with tempfile.TemporaryDirectory() as directory:
            shim = checker.build_shim(directory)
            calls = Path(directory) / "calls.txt"
            env = dict(os.environ, R4A_CALLS=str(calls), PYTHONNOUSERSITE="1", **checker.shim_env(shim))
            subprocess.run([sys.executable, "-I", "-c", "import os\nos.write(1, b'abc')\nos.write(2, b'de')"],
                           capture_output=True, env=env, timeout=60)
            line = calls.read_text().strip()
            self.assertRegex(line, r"^write fd0=\d+/\d+ fd1=\d+/\d+ fd2=\d+/\d+ other=\d+/\d+ read=\d+ open=\d+ close=\d+$")
            # os.write(1, 3 バイト) と os.write(2, 2 バイト)を数えている(Python の起動の書き込みがあれば、それ以上)。
            fd1 = int(line.split("fd1=")[1].split()[0].split("/")[1])
            fd2 = int(line.split("fd2=")[1].split()[0].split("/")[1])
            self.assertGreaterEqual(fd1, 3)
            self.assertGreaterEqual(fd2, 2)


class BoundaryGroups(unittest.TestCase):
    """境界の前・等値・後の組。旧版の出力が境界(等値と後の間)で変わらない組は、比較器の誤りとして失敗にする。"""

    def test_every_group_member_is_a_required_case(self):
        required = json.loads((checker.FIXTURES / "required_cases.json").read_text(encoding="utf-8"))["cases"]
        groups = checker.boundary_groups()
        self.assertGreaterEqual(len(groups), 21)
        for name, ids, rule, part in groups:
            self.assertIn(rule, ("accept", "cap"))
            self.assertIn(part, ("bytes", "lines", "refusal"))
            self.assertEqual(len(ids), 3, name)  # 前・等値・後の 3 件に固定する
            for case_id in ids:
                self.assertIn(case_id, required, f"{name}: {case_id}")

    def test_identical_outputs_before_and_after_are_a_failure(self):
        groups = [("g", ["a", "b", "c"], "accept", "bytes")]
        ok, bad = (0, b"ok", b""), (1, b"error", b"")
        # 陽性: 前 = 等値 かつ 等値 ≠ 後。
        self.assertEqual(checker.check_boundaries({"a": ok, "b": ok, "c": bad}, groups), (1, []))
        # 陰性: 3 つとも同じ・前だけ違う(境界が前と等値の間)・全部違う・後が等値と同じ。
        for outputs in ({"a": ok, "b": ok, "c": ok}, {"a": bad, "b": ok, "c": ok}, {"a": ok, "b": bad, "c": (2, b"x", b"")},
                        {"a": ok, "b": bad, "c": bad}):
            checked, failures = checker.check_boundaries(outputs, groups)
            self.assertEqual((checked, len(failures)), (1, 1), outputs)
            self.assertIn("見分けられない", failures[0])

    def test_a_cap_group_needs_short_then_equal_and_equal(self):
        groups = [("g", ["a", "b", "c"], "cap", "bytes")]
        out = lambda n: (0, b"x" * n, b"")
        self.assertEqual(checker.check_boundaries({"a": out(9), "b": out(10), "c": out(10)}, groups), (1, []))
        for sizes in ((10, 10, 10), (9, 10, 11), (9, 9, 9), (11, 10, 10)):
            checked, failures = checker.check_boundaries({k: out(n) for k, n in zip("abc", sizes)}, groups)
            self.assertEqual((checked, len(failures)), (1, 1), sizes)

    def test_incomplete_groups_are_not_judged(self):
        self.assertEqual(checker.check_boundaries({"a": (0, b"", b"")}, [("g", ["a", "b", "c"], "accept", "bytes")]), (0, []))

    def test_the_part_that_is_compared_is_the_boundary_part(self):
        # 量がそのまま出る出力でも、境界に関わる部分だけで判定する。`line=` の数(明細の上限)と、読取上限の拒否の有無。
        lines = lambda n: (0, b"deny=%d\n" % n + b"line=x\n" * min(n, 100), b"")
        cap = [("g", ["a", "b", "c"], "cap", "lines")]
        self.assertEqual(checker.check_boundaries({"a": lines(99), "b": lines(100), "c": lines(101)}, cap), (1, []))
        # 件数が 199・200・201 でも `line=` は 100 で頭打ちになるので、前 < 等値 でなく、失敗になる。
        self.assertEqual(len(checker.check_boundaries({"a": lines(199), "b": lines(200), "c": lines(201)}, cap)[1]), 1)
        refuse = lambda flag: (0, b"allow=1\n" + (b"reference-error=x\n" if flag else b""), b"")
        accept = [("g", ["a", "b", "c"], "accept", "refusal")]
        self.assertEqual(checker.check_boundaries({"a": refuse(False), "b": refuse(False), "c": refuse(True)}, accept), (1, []))
        self.assertEqual(len(checker.check_boundaries({"a": refuse(True), "b": refuse(True), "c": refuse(True)}, accept)[1]), 1)

    def test_a_full_run_requires_every_group_to_be_checked(self):
        groups = [("g", ["a", "b", "c"], "accept", "bytes"), ("h", ["d", "e", "f"], "accept", "bytes")]
        ok, bad = (0, b"ok", b""), (1, b"error", b"")
        outputs = {"a": ok, "b": ok, "c": bad}
        checked, failures = checker.check_boundaries(outputs, groups, expected_groups=2)
        self.assertEqual(checked, 1)
        self.assertIn("定義した 2 組と違う", failures[0])
        self.assertEqual(checker.check_boundaries(outputs, groups), (1, []))

    def test_generated_inputs_have_the_exact_length(self):
        for digits in (639, 640, 641, 4299, 4300, 4301):
            self.assertEqual(checker.int_in_dict_result(digits).count("9"), digits)
            self.assertEqual(checker.int_in_dict_origin(digits).count(b"9"), digits)
        for total in (1999, 2000, 2001):
            text = json.loads(checker.denials_input(total))
            self.assertEqual(len(json.dumps({"denials": text["denials"]}, ensure_ascii=False)), total)
        self.assertEqual(checker.nest_depth("[" * 7 + "]" * 7), 7)
        self.assertEqual(checker.nest_depth('{"a":' * 5 + "1" + "}" * 5), 5)


class FifoWait(unittest.TestCase):
    def test_fifo_wait_needs_an_open_syscall_and_the_sleeping_state(self):
        import unittest.mock as mock
        stat = lambda state: f"123 (python3) {state} 1 1 1 0 -1"
        with mock.patch("platform.machine", return_value="x86_64"):
            open_call = "257 0xffffffffffffff9c 0x7ffd 0x0"
            # 陽性: open 系の中で、眠っている(S)。wchan が読めない・0 のときは名前を見ない。
            self.assertTrue(checker.fifo_wait(open_call, stat("S"), "wait_for_partner"))
            self.assertTrue(checker.fifo_wait(open_call, stat("S"), "0"))
            self.assertTrue(checker.fifo_wait(open_call, stat("S"), "(読めない: PermissionError 13)"))
            # 陰性: D 状態の open(旧 Python の起動の途中)・走っている・open でない syscall・wchan の名前が違う。
            self.assertFalse(checker.fifo_wait(open_call, stat("D"), "wait_for_partner"))
            self.assertFalse(checker.fifo_wait(open_call, stat("R"), "0"))
            self.assertFalse(checker.fifo_wait("0 0x0 0x7ffd 0x2000", stat("S"), "wait_for_partner"))
            self.assertFalse(checker.fifo_wait("running", stat("S"), "0"))
            self.assertFalse(checker.fifo_wait(open_call, stat("S"), "do_some_other_wait"))
            # syscall が読めない機械は、wchan の名前と状態だけで確かめる。
            self.assertTrue(checker.fifo_wait("(読めない: OSError 1)", stat("S"), "fifo_open"))
            self.assertFalse(checker.fifo_wait("(読めない: OSError 1)", stat("S"), "0"))
        self.assertEqual(checker.proc_state("42 (a b) c) S 1"), "S")
        self.assertEqual(checker.proc_state(""), "")

    @unittest.skipUnless(sys.platform == "linux", "/proc は Linux だけ")
    def test_a_real_fifo_opener_is_recognised_and_a_regular_file_reader_is_not(self):
        import platform
        import unittest.mock as mock
        if platform.machine() not in checker.SYSCALL_NUMBERS:
            self.skipTest("syscall の番号表が無いアーキテクチャ")
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "f.fifo"
            os.mkfifo(fifo)
            regular = Path(directory) / "plain.txt"
            regular.write_text("x")
            opener = subprocess.Popen(["cat", str(fifo)])  # 書き手がいない FIFO の open で待つ
            try:
                checker.wait_for_state(opener.pid, "cat", "fifo")
            finally:
                opener.kill()
                opener.wait()
            # 普通のファイルを開いた後に眠る子は、FIFO の open の待ちと見なさない。
            sleeper = subprocess.Popen(["sh", "-c", f"exec 3< {regular}; exec sleep 30"])
            try:
                with mock.patch.object(checker, "CHILD_TIMEOUT", 1):
                    with self.assertRaises(checker.Failure):
                        checker.wait_for_state(sleeper.pid, None, "fifo")
            finally:
                sleeper.kill()
                sleeper.wait()


class OsConditionMimicry(unittest.TestCase):
    """Linux で Mac の条件(EILSEQ と locale の有り)をまねる。まねは自己検査の中だけ。固定の一覧と違えば失敗、同じなら未完で終了 0。"""

    def run_mimic(self, *args, wrapper=None, extra_env=None, in_selftest=True):
        env = {k: v for k, v in os.environ.items() if k not in ("R4A_REST_SIMULATE_EILSEQ", "R4A_REST_SIMULATE_LOCALE")}
        env.update(extra_env or {})
        if not in_selftest:
            env.pop("R4A_REST_IN_SELFTEST", None)
        fixture = wrapper or fixture_binary()
        with tempfile.TemporaryDirectory(prefix="r4a-rest-mimic-") as cwd:
            return subprocess.run([sys.executable, "-B", str(HERE / "check_r4a_rest.py"), "--fixture", str(fixture), "--no-selftest", *args],
                                  capture_output=True, text=True, timeout=600, cwd=cwd, env=env)

    def test_the_fixed_lists_are_consistent(self):
        required = json.loads((checker.FIXTURES / "required_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(required["requires_raw_name"], sorted(checker.RAW_NAME_CASES))
        self.assertEqual(checker.RAW_NAME_EXPECTED_REJECTED["linux"], [])
        self.assertEqual(len(checker.RAW_NAME_EXPECTED_REJECTED["darwin"]), 2)
        self.assertEqual(checker.UNAVAILABLE_BY_OS["linux"], [])
        self.assertEqual(len(checker.UNAVAILABLE_BY_OS["darwin"]), 7)
        for case_id in checker.UNAVAILABLE_BY_OS["darwin"]:
            self.assertIn(case_id, required["cases"])
        # UTF-8 でない名前は、`store` を使う case には入れない(専用の置き場 `rawstore` に分ける)。
        with tempfile.TemporaryDirectory() as directory:
            checker.make_candidate_inputs(directory)
            names = [os.fsencode(n) for n in os.listdir(Path(directory) / "store")]
            self.assertFalse(any(b"\xff" in n for n in names))
            self.assertTrue(any(b"\xff" in os.fsencode(n) for n in os.listdir(Path(directory) / "rawstore")))

    def test_simulation_is_refused_outside_the_selftest(self):
        for name in ("R4A_REST_SIMULATE_EILSEQ", "R4A_REST_SIMULATE_LOCALE"):
            result = self.run_mimic("--only", "host-argv", extra_env={name: "1"}, in_selftest=False)
            self.assertEqual(result.returncode, 1, name)
            self.assertIn("自己検査の中", result.stderr)

    def test_the_mac_conditions_give_an_incomplete_result_with_exit_zero(self):
        result = self.run_mimic("--only", "candidate-keys,loop-task-text",
                                extra_env={"R4A_REST_SIMULATE_EILSEQ": "1", "R4A_REST_SIMULATE_LOCALE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertIn("結果: 未完", result.stdout)
        # 未実施は、raw name の 2 件と locale の 2 件(candidate-key・task-info)。成功には数えない。
        self.assertIn("未実施 4 件", result.stdout)
        self.assertIn("charset/locale-ja_JP.eucJP/candidate-key", result.stdout)
        self.assertIn("sha256", result.stdout)  # 旧版の出力の要点
        self.assertNotIn("結果: 合格", result.stdout)

    def test_a_set_that_differs_from_the_fixed_list_is_a_failure(self):
        # locale だけをまねる(Linux の一覧は空)と、未実施の集合が一覧と違うので失敗(終了 1)。
        result = self.run_mimic("--only", "loop-task-text", extra_env={"R4A_REST_SIMULATE_LOCALE": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("固定の一覧と違う", result.stderr)

    def test_rust_is_compared_with_the_unconfigured_old_version_before_the_locale_is_given_up(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "w"
            # eucJP の case のときだけ、Rust の出力を変える。locale を理由に未実施にしても、この差は不一致にする。
            wrapper.write_text(f'#!/bin/sh\n"{fixture_binary()}" "$@"; rc=$?\n[ "$LC_ALL" = ja_JP.eucJP ] && echo extra\nexit $rc\n')
            wrapper.chmod(0o755)
            result = self.run_mimic("--only", "loop-task-text", wrapper=wrapper,
                                    extra_env={"R4A_REST_SIMULATE_LOCALE": "1", "R4A_REST_SIMULATE_EILSEQ": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Rust が設定を読んでいる", result.stderr)


class WaitingAndRawNames(unittest.TestCase):
    def test_syscall_line_decides_the_wait_by_number_and_fd(self):
        import unittest.mock as mock
        with mock.patch("platform.machine", return_value="x86_64"):
            self.assertTrue(checker.syscall_wait("stdin", "0 0x0 0x7ffd 0x2000 0x0 0x0 0x0 0x7ffd 0x7f00"))
            self.assertFalse(checker.syscall_wait("stdin", "0 0x3 0x7ffd 0x2000"))  # 別の fd の read
            self.assertTrue(checker.syscall_wait("fullpipe", "1 0x1 0x7ffd 0x30d42"))
            self.assertFalse(checker.syscall_wait("fullpipe", "1 0x2 0x7ffd 0x30d42"))
            self.assertTrue(checker.syscall_wait("fifo", "257 0xffffffffffffff9c 0x7ffd 0x0"))
            self.assertFalse(checker.syscall_wait("stdin", "running"))
            self.assertFalse(checker.syscall_wait("stdin", "-1 0x7ffd 0x7f00"))
        with mock.patch("platform.machine", return_value="aarch64"):
            self.assertTrue(checker.syscall_wait("stdin", "63 0x0 0x7ffd 0x2000"))
            self.assertTrue(checker.syscall_wait("fullpipe", "64 0x1 0x7ffd 0x2000"))
        with mock.patch("platform.machine", return_value="riscv64"):
            self.assertFalse(checker.syscall_wait("stdin", "63 0x0"))

    @unittest.skipUnless(sys.platform == "linux", "/proc は Linux だけ")
    def test_wait_timeout_reports_what_was_seen(self):
        import unittest.mock as mock
        child = subprocess.Popen(["sleep", "30"])
        try:
            with mock.patch.object(checker, "CHILD_TIMEOUT", 1):
                with self.assertRaises(checker.Failure) as raised:
                    checker.wait_for_state(child.pid, "sleep", "stdin")  # sleep は read で待たない
            message = str(raised.exception)
            for word in ("wchan=", "syscall=", "stat=", "cmdline=", "kernel="):
                self.assertIn(word, message)
        finally:
            child.kill()
            child.wait()

    @unittest.skipUnless(sys.platform == "linux", "/proc は Linux だけ")
    def test_real_blocked_children_are_recognised_without_wchan_names(self):
        import platform
        import unittest.mock as mock
        if platform.machine() not in checker.SYSCALL_NUMBERS:
            self.skipTest("syscall の番号表が無いアーキテクチャ")
        # wchan の名前に頼らず(空にして)、/proc/<pid>/syscall の番号と fd だけで、本物の待ちを見分ける。
        child = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            with mock.patch.object(checker, "WAIT_STATES", {"stdin": (), "fifo": (), "fullpipe": ()}):
                checker.wait_for_state(child.pid, "cat", "stdin")
                with mock.patch.object(checker, "CHILD_TIMEOUT", 1):
                    with self.assertRaises(checker.Failure):
                        checker.wait_for_state(child.pid, "cat", "fullpipe")  # cat は write で待たない
        finally:
            child.kill()
            child.wait()

    def test_raw_name_rejection_is_tolerated_only_off_linux_and_only_for_eilseq(self):
        import errno
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as directory:
            rejected = OSError(errno.EILSEQ, "Illegal byte sequence")
            checker.RAW_NAME_REJECTED.clear()
            with mock.patch("builtins.open", side_effect=rejected), mock.patch.object(checker.sys, "platform", "darwin"):
                checker.create_raw_name_file(directory, b"bad-\xff", b"x")
            self.assertEqual(checker.RAW_NAME_REJECTED, [(b"bad-\xff", errno.EILSEQ)])
            checker.RAW_NAME_REJECTED.clear()
            with mock.patch("builtins.open", side_effect=rejected), mock.patch.object(checker.sys, "platform", "linux"):
                with self.assertRaises(OSError):
                    checker.create_raw_name_file(directory, b"bad-\xff", b"x")
            with mock.patch("builtins.open", side_effect=OSError(errno.EACCES, "denied")), \
                    mock.patch.object(checker.sys, "platform", "darwin"):
                with self.assertRaises(OSError):
                    checker.create_raw_name_file(directory, b"bad-\xff", b"x")
            self.assertEqual(checker.RAW_NAME_REJECTED, [])


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
            wrapper.write_text('#!/bin/sh\n"' + str(real) + '" "$@"; rc=$?\nif ! ( exec 3>&1 ) 2>/dev/null; then echo extra >&2; fi\nexit $rc\n')
            wrapper.chmod(0o755)
            result = run_checker(wrapper, "--only", "host-argv")
        self.assertEqual(result.returncode, 1)
        self.assertIn("closed-host", result.stderr, result.stdout[-1500:] + result.stderr[-1500:])

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
        # 故障の case の失敗には、差込みが記録した呼び出しの要点(write の fd ごとの回数とバイト数など)が付く。
        self.assertIn("差込みが見た呼び出し", result.stderr)
        self.assertRegex(result.stderr, r"write fd0=\d+/\d+ fd1=\d+/\d+ fd2=\d+/\d+ other=\d+/\d+ read=\d+ open=\d+ close=\d+")

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
