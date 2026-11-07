"""起動入口をモックとリポジトリ内の実際の配布物で別々に検査する。"""

import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest


LAUNCH = (Path(__file__).resolve().parents[1]
          / "plugins/dev-workflow/bin/dev-workflow/launch.sh")
BINARIES = (
    "linux-x86_64/dev-workflow",
    "linux-arm64/dev-workflow",
    "macos-universal/dev-workflow",
)

# 外の道具を呼ばず、受け取った引数・標準入力と起動順を記録する。
MOCK_TOOL = r'''
import hashlib
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
data = sys.stdin.buffer.read() if name in ("sha256sum", "shasum", "openssl") else b""
with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"tool": name, "args": args,
                             "input_sha256": hashlib.sha256(data).hexdigest()}) + "\n")
if name == "uname":
    key = {"-s": "OS", "-m": "CPU", "-r": "RELEASE"}.get(args[0]) if len(args) == 1 else None
    if key is None:
        sys.exit(90)
    print(os.environ["MOCK_" + key])
    sys.exit(int(os.environ.get("MOCK_" + key + "_STATUS", "0")))
if name == "sw_vers":
    if args != ["-productVersion"]:
        sys.exit(91)
    print(os.environ["MOCK_MACOS"])
    sys.exit(int(os.environ.get("MOCK_MACOS_STATUS", "0")))
expected_args = {"sha256sum": [], "shasum": ["-a", "256"],
                 "openssl": ["dgst", "-sha256"]}[name]
if args != expected_args:
    sys.exit(92)
digest = hashlib.sha256(data).hexdigest()
if os.environ.get("MOCK_HASH_UPPER") == "1":
    digest = digest.upper()
prefix = os.environ.get("MOCK_OPENSSL_PREFIX", "SHA2-256(stdin)= ")
output = prefix + digest + "\n" if name == "openssl" else digest + "  -\n"
if "MOCK_HASH_BYTES" in os.environ:
    sys.stdout.buffer.write(bytes.fromhex(os.environ["MOCK_HASH_BYTES"]))
else:
    sys.stdout.write(os.environ.get("MOCK_HASH_OUTPUT", output))
sys.exit(int(os.environ.get("MOCK_HASH_STATUS", "0")))
'''

MOCK_NATIVE = r'''
import base64
import json
import os
from pathlib import Path
import sys

data = {"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
        "target": str(Path(sys.argv[0]).parent.name),
        "stdin": base64.b64encode(sys.stdin.buffer.read()).decode("ascii")}
Path(os.environ["MOCK_NATIVE_LOG"]).write_text(json.dumps(data), encoding="utf-8")
if "MOCK_NATIVE_SIGNAL" in os.environ:
    os.kill(os.getpid(), int(os.environ["MOCK_NATIVE_SIGNAL"]))
if "MOCK_NATIVE_STATUS" in os.environ:
    sys.stdout.write("native stdout\n")
    sys.stderr.write("native stderr\n")
    sys.exit(int(os.environ["MOCK_NATIVE_STATUS"]))
valid = (["--version"], ["--help"], ["probe", "--json"], ["probe", "--self-sha256"])
if sys.argv[1:] not in valid and os.environ.get("MOCK_ECHO_ARGS") != "1":
    sys.stderr.write("ERROR [usage] mock native\n")
    sys.exit(2)
print(json.dumps(data))
'''


class NativeLaunchMockTest(unittest.TestCase):
    """Task 1.1 のモック検査。Task 2.2 の実際の配布物の検査とは分ける。"""

    @classmethod
    def setUpClass(cls):
        cls.shells = []
        for name, arguments in (("sh", []), ("dash", []), ("bash", ["--posix"])):
            executable = shutil.which(name)
            if executable:
                cls.shells.append((name, [executable, *arguments]))
            elif name != "dash" or sys.platform.startswith("linux"):
                raise AssertionError(f"起動入口の検査に {name} が必要です")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-launch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bundle = self.root / "配布 元" / "bin" / "dev-workflow"
        self.bundle.mkdir(parents=True)
        self.launch = self.bundle / "launch.sh"
        shutil.copy2(LAUNCH, self.launch)
        self.cwd = self.root / "作業 場所"
        self.cwd.mkdir()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("awk", "wc"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable, name)
            (self.tools / name).symlink_to(executable)
        for name in ("uname", "sw_vers", "sha256sum", "shasum", "openssl"):
            self.write_executable(self.tools / name, MOCK_TOOL)
        for name in BINARIES:
            target = self.bundle / name
            target.parent.mkdir()
            self.write_executable(target, MOCK_NATIVE + "\n# " + name + "\n")
        self.manifest = self.bundle / "SHA256SUMS"
        self.write_manifest()
        self.log = self.root / "tools.jsonl"
        self.native_log = self.root / "native.json"
        self.env = {
            "PATH": str(self.tools), "LC_ALL": "C", "DEV_WORKFLOW_ENGINE": "rust",
            "MOCK_OS": "Linux", "MOCK_CPU": "x86_64", "MOCK_RELEASE": "5.6.0",
            "MOCK_MACOS": "11.0", "MOCK_LOG": str(self.log),
            "MOCK_NATIVE_LOG": str(self.native_log),
        }

    def write_executable(self, path, body):
        path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def write_manifest(self):
        self.manifest.write_bytes(b"".join(
            hashlib.sha256((self.bundle / name).read_bytes()).hexdigest().encode("ascii")
            + b"  " + name.encode("ascii") + b"\n" for name in BINARIES))

    def run_launch(self, shell, args=("probe", "--json"), *, launch=None, data=b""):
        self.log.unlink(missing_ok=True)
        self.native_log.unlink(missing_ok=True)
        command = [*shell, str(launch or self.launch), *args]
        with subprocess.Popen(command, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                stdout, stderr = process.communicate(data, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            self.last_pid = process.pid
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def hashes(self):
        return [call for call in self.calls() if call["tool"] in ("sha256sum", "shasum", "openssl")]

    def assert_rejected(self, code=1, reason=None, args=("probe", "--json"), **kwargs):
        for name, shell in self.shells:
            with self.subTest(shell=name):
                result = self.run_launch(shell, args, **kwargs)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"ERROR [", result.stderr)
                if reason:
                    self.assertIn(f"ERROR [{reason}]".encode(), result.stderr)
                self.assertFalse(self.native_log.exists(), result.stderr)

    def assert_started(self, target="linux-x86_64", args=("probe", "--json"), **kwargs):
        for name, shell in self.shells:
            with self.subTest(shell=name):
                result = self.run_launch(shell, args, **kwargs)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                record = json.loads(result.stdout)
                self.assertEqual(record["target"], target)
                self.assertEqual(record["argv"], list(args))
                self.assertEqual(record["cwd"], str(self.cwd))
                self.assertEqual(record["pid"], self.last_pid)
                self.assertEqual(base64.b64decode(record["stdin"]), kwargs.get("data", b""))

    def test_posix_syntax_and_executable_mode(self):
        self.assertEqual(stat.S_IMODE(LAUNCH.stat().st_mode), 0o755)
        for name, shell in self.shells:
            with self.subTest(shell=name):
                result = subprocess.run([*shell, "-n", str(LAUNCH)], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_and_invalid_settings_need_no_bundle_or_tools(self):
        for entry in self.bundle.iterdir():
            if entry != self.launch:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        shutil.rmtree(self.tools)
        self.tools.mkdir()
        for engine, reason in ((None, "legacy-unavailable"), ("legacy", "legacy-unavailable"),
                               ("", "engine"), ("unknown", "engine"), ("RUST", "engine"),
                               (" rust", "engine"), ("rust\n", "engine")):
            if engine is None:
                self.env.pop("DEV_WORKFLOW_ENGINE", None)
            else:
                self.env["DEV_WORKFLOW_ENGINE"] = engine
            for args in (("probe", "--json"), ("probe", "--self-sha256"), ("--help",),
                         ("--version",), ("unknown",), ()):
                with self.subTest(engine=engine, args=args):
                    self.assert_rejected(2, reason, args)
                    self.assertEqual(self.calls(), [])
                    if reason == "legacy-unavailable":
                        result = self.run_launch(self.shells[0][1], args)
                        self.assertIn(b"DEV_WORKFLOW_ENGINE=rust", result.stderr)

    def test_all_supported_os_cpu_pairs_and_commands(self):
        for os_name, cpu, target in (("Linux", "x86_64", "linux-x86_64"),
                                     ("Linux", "aarch64", "linux-arm64"),
                                     ("Linux", "arm64", "linux-arm64"),
                                     ("Darwin", "x86_64", "macos-universal"),
                                     ("Darwin", "arm64", "macos-universal")):
            self.env.update(MOCK_OS=os_name, MOCK_CPU=cpu)
            for args in (("--version",), ("--help",), ("probe", "--json"), ("probe", "--self-sha256")):
                with self.subTest(os=os_name, cpu=cpu, args=args):
                    self.assert_started(target, args)

    def test_unknown_os_cpu_and_unreadable_identity(self):
        for os_name, cpu in (("FreeBSD", "x86_64"), ("Windows_NT", "arm64"), ("", "x86_64"),
                             ("Linux", "i686"), ("Linux", "riscv64"), ("Linux", ""),
                             ("Darwin", "aarch64"), ("Darwin", "ppc64")):
            with self.subTest(os=os_name, cpu=cpu):
                self.env.update(MOCK_OS=os_name, MOCK_CPU=cpu)
                self.assert_rejected(reason="platform")
                self.assertEqual(self.hashes(), [])
        self.env.update(MOCK_OS="Linux", MOCK_CPU="x86_64")
        for key in ("MOCK_OS_STATUS", "MOCK_CPU_STATUS"):
            with self.subTest(key=key):
                self.env[key] = "1"
                self.assert_rejected(reason="platform")
                del self.env[key]
        (self.tools / "uname").unlink()
        self.assert_rejected(reason="platform")

    def test_linux_minimum_and_release_suffixes(self):
        for version in ("5.6", "5.6.0", "5.6.0-rc1", "5.6.0+", "5.15.0-1092-azure",
                        "6.0.0", "12.1.0", "05.06.000"):
            with self.subTest(version=version):
                self.env["MOCK_RELEASE"] = version
                self.assert_started()
        for version in ("5.5.999", "4.99.0", "5", "", "unknown", "5.6oops", "5.6.0\n4.0.0",
                        " 6.0.0", "6.0.0\r", "6..1", "6.a", "-6.1", "6.0;exit 0"):
            with self.subTest(version=version):
                self.env["MOCK_RELEASE"] = version
                self.assert_rejected(reason="os-version")
                self.assertEqual(self.hashes(), [])
        self.env.update(MOCK_RELEASE="6.0.0", MOCK_RELEASE_STATUS="1")
        self.assert_rejected(reason="os-version")

    def test_macos_minimum_and_unreadable_version(self):
        self.env["MOCK_OS"] = "Darwin"
        for version in ("11", "11.0", "11.0.1", "12.1.0", "26.0", "011.00"):
            with self.subTest(version=version):
                self.env["MOCK_MACOS"] = version
                self.assert_started("macos-universal")
        for version in ("10.15.7", "10.16", "9.99.99", "", "unknown", "11x", "11.0-beta",
                        "11.0\n10.15", "11..1", " 11.0", "11.0\r"):
            with self.subTest(version=version):
                self.env["MOCK_MACOS"] = version
                self.assert_rejected(reason="os-version")
        self.env.update(MOCK_MACOS="11.0", MOCK_MACOS_STATUS="1")
        self.assert_rejected(reason="os-version")
        (self.tools / "sw_vers").unlink()
        self.assert_rejected(reason="os-version")

    def test_first_hash_choice_and_stdin_only(self):
        for os_name, tool, target in (("Linux", "sha256sum", BINARIES[0]),
                                      ("Darwin", "shasum", BINARIES[2])):
            with self.subTest(os=os_name):
                self.env["MOCK_OS"] = os_name
                self.assert_started(target.split("/")[0])
                calls = self.hashes()
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["tool"], tool)
                self.assertEqual(calls[0]["args"], [] if tool == "sha256sum" else ["-a", "256"])
                self.assertEqual(calls[0]["input_sha256"], hashlib.sha256((self.bundle / target).read_bytes()).hexdigest())

    def test_openssl_is_the_only_alternative_on_both_os(self):
        for os_name, first, target in (("Linux", "sha256sum", "linux-x86_64"),
                                       ("Darwin", "shasum", "macos-universal")):
            with self.subTest(os=os_name):
                self.env["MOCK_OS"] = os_name
                (self.tools / first).unlink()
                for prefix in ("SHA256(stdin)= ", "SHA2-256(stdin)= "):
                    self.env["MOCK_OPENSSL_PREFIX"] = prefix
                    self.assert_started(target)
                    self.assertEqual([call["tool"] for call in self.hashes()], ["openssl"])
                    self.assertEqual(self.hashes()[0]["args"], ["dgst", "-sha256"])
                self.write_executable(self.tools / first, MOCK_TOOL)

    def hash_configurations(self):
        return (("Linux", "sha256sum", "linux-x86_64"),
                ("Darwin", "shasum", "macos-universal"),
                ("Linux", "openssl", "linux-x86_64"),
                ("Darwin", "openssl", "macos-universal"))

    def configure_hash(self, os_name, tool):
        self.env["MOCK_OS"] = os_name
        for name in ("sha256sum", "shasum", "openssl"):
            self.write_executable(self.tools / name, MOCK_TOOL)
        if tool == "openssl":
            (self.tools / ("sha256sum" if os_name == "Linux" else "shasum")).unlink()

    def test_uppercase_hash_output_is_normalized(self):
        self.env["MOCK_HASH_UPPER"] = "1"
        for os_name, tool, target in self.hash_configurations():
            with self.subTest(os=os_name, tool=tool):
                self.configure_hash(os_name, tool)
                self.assert_started(target)

    def test_hash_failure_does_not_try_another_tool_or_legacy(self):
        self.env["MOCK_HASH_STATUS"] = "7"
        for os_name, tool, _ in self.hash_configurations():
            with self.subTest(os=os_name, tool=tool):
                self.configure_hash(os_name, tool)
                self.assert_rejected(reason="hash")
                self.assertEqual([call["tool"] for call in self.hashes()], [tool])

    def test_malformed_hash_output_is_not_executed(self):
        marker = self.cwd / "interpreted"
        for os_name, tool, target in self.hash_configurations():
            self.configure_hash(os_name, tool)
            digest = hashlib.sha256((self.bundle / target / "dev-workflow").read_bytes()).hexdigest()
            valid = "SHA256(stdin)= " + digest if tool == "openssl" else digest + "  -"
            cases = ("", "\n", "z" * 64, digest[:63], "0" + digest, valid + " extra\n",
                     valid + "\nextra\n", valid + "\n\n", "\r" + valid + "\n",
                     "$(printf executed > interpreted)\n", "`printf executed > interpreted`\n",
                     "SHA256(file)= " + digest + "\n", digest + "  file\n")
            for output in cases:
                with self.subTest(os=os_name, tool=tool, output=output):
                    self.env["MOCK_HASH_OUTPUT"] = output
                    self.assert_rejected(reason="hash")
                    self.assertEqual([call["tool"] for call in self.hashes()], [tool])
                    self.assertFalse(marker.exists())

    def test_missing_hash_tools_does_not_use_other_os_tool(self):
        (self.tools / "openssl").unlink()
        for os_name, first in (("Linux", "sha256sum"), ("Darwin", "shasum")):
            with self.subTest(os=os_name):
                self.env["MOCK_OS"] = os_name
                (self.tools / first).unlink()
                self.assert_rejected(reason="hash-tool")
                self.assertEqual(self.hashes(), [])
                self.write_executable(self.tools / first, MOCK_TOOL)

    def test_hash_output_rejects_invalid_bytes_and_accepts_no_final_newline(self):
        for os_name, tool, target in self.hash_configurations():
            self.configure_hash(os_name, tool)
            digest = hashlib.sha256((self.bundle / target / "dev-workflow").read_bytes()).hexdigest()
            valid = ("SHA256(stdin)= " + digest if tool == "openssl" else digest + "  -").encode()
            self.env["MOCK_HASH_BYTES"] = valid.hex()
            self.assert_started(target)
            for output in (b"\x00" + valid + b"\n", valid + b"\x00\n", b"\xff" + valid + b"\n",
                           valid[:32] + b"\x00" + valid[32:] + b"\n"):
                with self.subTest(os=os_name, tool=tool, output=output):
                    self.env["MOCK_HASH_BYTES"] = output.hex()
                    self.assert_rejected(reason="hash")

    def test_missing_changed_and_nonexecutable_selected_binary(self):
        target = self.bundle / BINARIES[0]
        original = target.read_bytes()
        target.unlink()
        self.assert_rejected(reason="binary")
        target.mkdir()
        self.assert_rejected(reason="binary")
        target.rmdir()
        os.mkfifo(target)
        target.chmod(0o755)
        self.assert_rejected(reason="binary")
        target.unlink()
        target.write_bytes(original)
        target.chmod(0o644)
        self.assert_rejected(reason="binary")
        target.chmod(0o755)
        target.write_bytes(original + b"\n# changed\n")
        self.assert_rejected(reason="hash-mismatch")

    @unittest.skipIf(os.geteuid() == 0, "root はファイルの読み取り権限を迂回する")
    def test_unreadable_selected_binary_and_manifest_are_rejected(self):
        target = self.bundle / BINARIES[0]
        target.chmod(0o111)
        self.assert_rejected(reason="hash")
        target.chmod(0o755)
        self.manifest.chmod(0o000)
        self.assert_rejected(reason="manifest")

    def test_hash_mismatch_on_all_selected_targets(self):
        for os_name, cpu, relative in (("Linux", "x86_64", BINARIES[0]),
                                       ("Linux", "arm64", BINARIES[1]),
                                       ("Darwin", "arm64", BINARIES[2])):
            with self.subTest(target=relative):
                self.env.update(MOCK_OS=os_name, MOCK_CPU=cpu)
                target = self.bundle / relative
                target.write_bytes(target.read_bytes() + b"\n# changed\n")
                self.assert_rejected(reason="hash-mismatch")

    def test_only_selected_binary_is_read_and_build_info_is_not_required(self):
        for name in BINARIES[1:]:
            (self.bundle / name).unlink()
        self.assert_started()
        (self.bundle / "build-info.json").write_text("not JSON", encoding="utf-8")
        self.assert_started()

    def test_missing_and_nonregular_manifest(self):
        self.manifest.unlink()
        self.assert_rejected(reason="manifest")
        self.manifest.mkdir()
        self.assert_rejected(reason="manifest")
        self.manifest.rmdir()
        os.mkfifo(self.manifest)
        self.assert_rejected(reason="manifest")

    def test_manifest_requires_exact_three_lines(self):
        data = self.manifest.read_bytes()
        lines = data.splitlines(keepends=True)
        cases = [b"", data[:-1], data + b"\n", data.replace(b"\n", b"\r\n"),
                 data.replace(b"  ", b" "), data.replace(b"  ", b"\t"),
                 data.replace(b"  ", b" *"), data.replace(lines[0][:64], lines[0][:64].upper()),
                 b"".join(reversed(lines)), data + lines[0], b"".join(lines[:2]),
                 lines[0] + lines[0] + lines[2], b"\xef\xbb\xbf" + data, b"\xff" + data,
                 b"g" + data[1:], b"\x00" + data, data + b"\x00", b"\x00" + data[1:],
                 data.replace(b"linux-x86_64", b"linux-x86_6\x00"), data.replace(b"\n", b"\x00", 1)]
        for path in (b"/linux-x86_64/dev-workflow", b"../linux-x86_64/dev-workflow",
                     b"other/dev-workflow", b"./linux-x86_64/dev-workflow",
                     b"linux-x86_64/../dev-workflow", b"$(printf executed > interpreted)"):
            cases.append(data.replace(BINARIES[0].encode(), path))
        for invalid in cases:
            with self.subTest(value=invalid):
                self.manifest.write_bytes(invalid)
                self.assert_rejected(reason="manifest")
                self.assertEqual(self.hashes(), [])
                self.assertFalse((self.cwd / "interpreted").exists())

    def test_manifest_symlink_including_dangling_is_rejected(self):
        outside = self.root / "outside-sums"
        self.manifest.rename(outside)
        for destination in (outside, self.root / "absent"):
            with self.subTest(destination=destination):
                self.manifest.symlink_to(destination)
                self.assert_rejected(reason="manifest")
                self.assertEqual(self.hashes(), [])
                self.manifest.unlink()

    def test_binary_and_intermediate_directory_symlinks_are_rejected(self):
        for relative in BINARIES:
            for directory in (False, True):
                path = self.bundle / relative
                if directory:
                    path = path.parent
                outside = self.root / "outside"
                path.rename(outside)
                for destination in (outside, self.root / "absent"):
                    with self.subTest(relative=relative, directory=directory, destination=destination):
                        path.symlink_to(destination, target_is_directory=directory)
                        self.assert_rejected(reason="symlink")
                        self.assertEqual(self.hashes(), [])
                        path.unlink()
                outside.rename(path)

    def test_setup_style_bundle_symlink_and_relative_path_ignore_cdpath(self):
        installed = self.cwd / "導入 先" / "bin" / "dev-workflow"
        installed.parent.mkdir(parents=True)
        installed.symlink_to(self.bundle, target_is_directory=True)
        self.env["CDPATH"] = str(self.root / "配布 元") + ":/"
        self.assert_started(launch=installed / "launch.sh")
        self.assert_started(launch=(installed / "launch.sh").relative_to(self.cwd))
        local = self.cwd / "local"
        local.symlink_to(self.bundle, target_is_directory=True)
        self.assert_started(launch="local/launch.sh")
        self.cwd = self.bundle
        self.assert_started(launch="launch.sh")

    def test_arguments_stdin_and_cwd_are_preserved_by_exec(self):
        self.env["MOCK_ECHO_ARGS"] = "1"
        args = ("", "two words", "日本語", "*", "--option=value", "a\nb", "a'b", "$HOME", "a\\b")
        self.assert_started(args=args, data=b"stdin\x00\xff\nsecond line\n")

    def test_native_invalid_arguments_are_returned_unchanged(self):
        cases = ((), ("unknown",), ("--version", "--help"), ("--help", "extra"), ("probe",),
                 ("probe", "--json", "--self-sha256"), ("probe", "--json", "--json"),
                 ("probe", "--unknown"), ("probe", "positional"))
        for args in cases:
            for name, shell in self.shells:
                with self.subTest(args=args, shell=name):
                    result = self.run_launch(shell, args)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, b"")
                    self.assertEqual(result.stderr, b"ERROR [usage] mock native\n")
                    self.assertEqual(json.loads(self.native_log.read_text())["argv"], list(args))

    def test_native_stdout_stderr_and_failure_are_preserved(self):
        for code in (1, 2, 23, 127):
            self.env["MOCK_NATIVE_STATUS"] = str(code)
            for name, shell in self.shells:
                with self.subTest(code=code, shell=name):
                    result = self.run_launch(shell)
                    self.assertEqual(result.returncode, code)
                    self.assertEqual(result.stdout, b"native stdout\n")
                    self.assertEqual(result.stderr, b"native stderr\n")
                    self.assertEqual(json.loads(self.native_log.read_text())["pid"], self.last_pid)
                    self.assertEqual(len(self.hashes()), 1)

    def test_signal_reaches_exec_process(self):
        self.env["MOCK_NATIVE_SIGNAL"] = str(int(signal.SIGTERM))
        for name, shell in self.shells:
            with self.subTest(shell=name):
                result = self.run_launch(shell)
                self.assertEqual(result.returncode, -signal.SIGTERM)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")

    def test_test_binary_and_profile_do_not_select_another_executable(self):
        external = self.root / "external"
        self.write_executable(external, "raise AssertionError('must not execute')\n")
        self.env["DEV_WORKFLOW_TEST_NATIVE_BINARY"] = str(external)
        profile = self.cwd / ".claude" / "project-profile.yml"
        profile.parent.mkdir()
        profile.write_text("DEV_WORKFLOW_ENGINE: legacy\ninvalid: [\n", encoding="utf-8")
        self.assert_started()
        self.env.pop("DEV_WORKFLOW_ENGINE")
        self.assert_rejected(2, "legacy-unavailable")
        self.assertEqual(self.calls(), [])

    def test_launch_creates_no_files_and_does_not_modify_bundle(self):
        before = {str(path.relative_to(self.root)): (stat.S_IMODE(path.stat().st_mode), path.read_bytes())
                  for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()}
        self.assert_started()
        after = {str(path.relative_to(self.root)): (stat.S_IMODE(path.stat().st_mode), path.read_bytes())
                 for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()
                 and path not in (self.log, self.native_log)}
        self.assertEqual(before, after)


class NativeDistributionLaunchTests(unittest.TestCase):
    """配布した 3 本を必須にし、実行中の OS と CPU の 1 本を入口から起動する。"""

    def test_repository_distribution_runs_all_four_commands(self):
        bundle = LAUNCH.parent
        required = (*BINARIES, "launch.sh", "SHA256SUMS", "build-info.json")
        missing = [name for name in required if not (bundle / name).is_file()]
        self.assertEqual(missing, [], f"配布物が欠けています: {missing}")
        for name in required:
            path = bundle / name
            self.assertFalse(path.is_symlink(), name)
            self.assertFalse(path.parent.is_symlink(), name)
            executable = name in (*BINARIES, "launch.sh")
            self.assertEqual(bool(path.stat().st_mode & 0o111), executable, name)
            if executable:
                self.assertTrue(os.access(path, os.X_OK), name)

        expected_os = {"Linux": "linux", "Darwin": "macos"}.get(platform.system())
        expected_arch = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine())
        self.assertIsNotNone(expected_os, "検査対象でない OS です")
        self.assertIsNotNone(expected_arch, "検査対象でない CPU です")
        selected = ("macos-universal/dev-workflow" if expected_os == "macos" else
                    "linux-x86_64/dev-workflow" if expected_arch == "x86_64" else
                    "linux-arm64/dev-workflow")
        hashes = {name: hashlib.sha256((bundle / name).read_bytes()).hexdigest() for name in BINARIES}
        self.assertEqual((bundle / "SHA256SUMS").read_bytes(),
                         "".join(f"{hashes[name]}  {name}\n" for name in BINARIES).encode())
        metadata = json.loads((bundle / "build-info.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["schema_version"], 1)
        self.assertCountEqual(metadata["artifacts"],
                              [{"path": name, "sha256": hashes[name]} for name in BINARIES])

        def snapshot(root):
            return {path.relative_to(root).as_posix():
                    (stat.S_IMODE(path.stat().st_mode),
                     hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
                    for path in root.rglob("*")}

        before_bundle = snapshot(bundle)
        with tempfile.TemporaryDirectory(prefix="native-distribution-launch-") as temporary:
            cwd = Path(temporary)
            (cwd / "unchanged").write_bytes(b"before\0\n")
            before_cwd = snapshot(cwd)
            env = dict(os.environ, DEV_WORKFLOW_ENGINE="rust")
            outputs = {}
            for args in (("--version",), ("--help",), ("probe", "--json"), ("probe", "--self-sha256")):
                with self.subTest(args=args):
                    result = subprocess.run(["sh", str(LAUNCH), *args], cwd=cwd, env=env,
                                            stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, b"")
                    self.assertEqual(snapshot(cwd), before_cwd)
                    self.assertEqual(snapshot(bundle), before_bundle)
                    outputs[args] = result.stdout
            self.assertEqual(len(outputs), 4, "4 コマンドすべての成功が必要です")

        raw = outputs[("probe", "--json")]
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        info = json.loads(raw)
        self.assertEqual(set(info), {"schema_version", "ok", "os", "arch", "native_version",
                                     "source_input_sha256", "rustc"})
        self.assertIs(type(info["schema_version"]), int)
        self.assertEqual(info["schema_version"], 1)
        self.assertIs(info["ok"], True)
        self.assertEqual((info["os"], info["arch"]), (expected_os, expected_arch))
        self.assertEqual(info["source_input_sha256"], metadata["source_input_sha256"])
        self.assertRegex(info["source_input_sha256"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(info["rustc"], metadata["rustc"])
        self.assertRegex(info["rustc"], r"\Arustc [^\r\n]+\Z")
        self.assertRegex(info["native_version"], r"\A\d+\.\d+\.\d+\Z")
        expected_version = (f"dev-workflow {info['native_version']} "
                            f"(source_input_sha256: {info['source_input_sha256']})\n").encode()
        self.assertEqual(outputs[("--version",)], expected_version)
        self.assertTrue(outputs[("--help",)].endswith(b"\n"))
        for command in (b"--version", b"--help", b"probe --json", b"probe --self-sha256"):
            self.assertIn(command, outputs[("--help",)])
        self.assertEqual(outputs[("probe", "--self-sha256")], hashes[selected].encode() + b"\n")


if __name__ == "__main__":
    unittest.main()
