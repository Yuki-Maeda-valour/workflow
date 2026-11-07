"""小さな sh の配布入口で、起動検査の成功と検査漏れを確かめる。"""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import check_dist_launch as checker


class DistLaunchTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dist-launch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "配布 元"
        self.bundle = self.root / checker.BUNDLE
        self.bundle.mkdir(parents=True)
        self.launch = self.bundle / "launch.sh"
        self.log = self.root / "calls.log"
        self.tool_log = self.root / "tools.log"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.manifest = self.bundle / "SHA256SUMS"
        self.hashes = {}
        for relative in checker.BINARIES:
            binary = self.bundle / relative
            binary.parent.mkdir()
            binary.write_bytes(("fake binary: " + relative + "\n").encode())
            binary.chmod(0o755)
            self.hashes[relative] = hashlib.sha256(binary.read_bytes()).hexdigest()
        self.manifest.write_text("".join(f"{digest}  {name}\n" for name, digest in self.hashes.items()), encoding="ascii")
        self.configure("linux", "x86_64", checker.BINARIES[0])
        self.env = mock.patch.dict(os.environ, {
            "PATH": str(self.tools) + os.pathsep + os.environ.get("PATH", os.defpath),
            # 検査器が未設定の検査時に、親から受け取った rust を取り除くかも見る。
            "DEV_WORKFLOW_ENGINE": "rust",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def configure(self, os_name, arch, relative):
        self.info = {"schema_version": 1, "ok": True, "os": os_name, "arch": arch,
                     "native_version": "0.1.0", "source_input_sha256": "a" * 64,
                     "rustc": "rustc 1.99.1 (fixture)"}
        self.outputs = {
            ("--version",): f"dev-workflow 0.1.0 (source_input_sha256: {'a' * 64})\n",
            ("--help",): "Usage:\n  --version\n  --help\n  probe --json\n  probe --self-sha256\n",
            ("probe", "--json"): json.dumps(self.info) + "\n",
            ("probe", "--self-sha256"): self.hashes[relative] + "\n",
        }
        self.responses = {}
        for args, output in self.outputs.items():
            self.responses["rust", args] = (0, output, "")
            for engine in ("unset", "legacy"):
                self.responses[engine, args] = (2, "", "ERROR [legacy-unavailable] DEV_WORKFLOW_ENGINE=rust\n")
        self.write_launch()

    def write_launch(self):
        lines = ["#!/bin/sh", "set -eu",
                 "printf '%s|%s|%s\\n' \"${DEV_WORKFLOW_ENGINE-unset}\" \"$*\" \"$PWD\" >> " + shlex.quote(str(self.log)),
                 'case "${DEV_WORKFLOW_ENGINE-unset}:$*" in']
        for (engine, args), (status, stdout, stderr) in self.responses.items():
            lines.extend([
                f"  {shlex.quote(engine + ':' + ' '.join(args))})",
                "    printf '%s' " + shlex.quote(stdout),
                "    printf '%s' " + shlex.quote(stderr) + " >&2",
                f"    exit {status} ;;",
            ])
        lines.extend(["  *) exit 91 ;;", "esac", ""])
        self.launch.write_text("\n".join(lines), encoding="utf-8")
        self.launch.chmod(0o755)

    def run_check(self, system="Linux", machine="x86_64"):
        with mock.patch.object(checker.platform, "system", return_value=system), \
                mock.patch.object(checker.platform, "machine", return_value=machine):
            return checker.check_dist_launch(self.root)

    def assert_rejected(self, pattern, system="Linux", machine="x86_64"):
        with self.assertRaisesRegex((ValueError, OSError), pattern):
            self.run_check(system, machine)

    def mac_tools(self, architectures="x86_64 arm64\n", lipo_status=0, codesign_status=0):
        binary = str(self.bundle / checker.BINARIES[2])
        for name, args, output, status in (
            ("lipo", ["-archs", binary], architectures, lipo_status),
            ("codesign", ["--verify", "--strict", "--all-architectures", binary], "", codesign_status),
        ):
            lines = ["#!/bin/sh", f'[ "$#" -eq {len(args)} ] || exit 99']
            lines.extend(f'[ "${index}" = {shlex.quote(arg)} ] || exit 99' for index, arg in enumerate(args, 1))
            lines.extend(["printf '%s\\n' " + shlex.quote(name) + " >> " + shlex.quote(str(self.tool_log)),
                          "printf '%s' " + shlex.quote(output), f"exit {status}", ""])
            path = self.tools / name
            path.write_text("\n".join(lines), encoding="utf-8")
            path.chmod(0o755)

    def test_linux_targets_and_all_engine_command_combinations(self):
        for machine, arch, relative in (
            ("x86_64", "x86_64", checker.BINARIES[0]),
            ("aarch64", "aarch64", checker.BINARIES[1]),
            ("arm64", "aarch64", checker.BINARIES[1]),
        ):
            with self.subTest(machine=machine):
                self.configure("linux", arch, relative)
                self.log.unlink(missing_ok=True)
                result = self.run_check(machine=machine)
                self.assertEqual(result["self_sha256"], self.hashes[relative])
                self.assertEqual(result["binary"], relative)
                self.assertEqual(self.log.read_text().splitlines(), [
                    f"{engine}|{' '.join(args)}|{self.root.resolve()}"
                    for engine in ("rust", "unset", "legacy") for args in checker.COMMANDS
                ])
                self.assertFalse(self.tool_log.exists())

    def test_both_macs_check_the_same_universal_file(self):
        self.mac_tools()
        for machine, arch in (("x86_64", "x86_64"), ("arm64", "aarch64")):
            with self.subTest(machine=machine):
                self.configure("macos", arch, checker.BINARIES[2])
                result = self.run_check("Darwin", machine)
                self.assertEqual(result["binary"], checker.BINARIES[2])
                self.assertEqual(result["self_sha256"], self.hashes[checker.BINARIES[2]])
        self.assertEqual(self.tool_log.read_text().splitlines(), ["lipo", "codesign"] * 2)

    def test_each_rust_command_failure_and_stderr(self):
        for args in checker.COMMANDS:
            for response, message in (( (7, "", "failed"), "終了コード"),
                                      ( (0, self.outputs[args], "unexpected"), "標準エラー")):
                with self.subTest(args=args, response=response):
                    self.responses["rust", args] = response
                    self.write_launch()
                    self.assert_rejected(message)
            self.responses["rust", args] = (0, self.outputs[args], "")

    def test_version_must_match_probe(self):
        for output in ("", "dev-workflow 9.0.0\n", self.outputs["--version",].rstrip("\n")):
            with self.subTest(output=output):
                self.responses["rust", ("--version",)] = (0, output, "")
                self.write_launch()
                self.assert_rejected("--version")

    def test_help_must_list_each_command_and_end_with_newline(self):
        for output in [self.outputs["--help",].replace(" ".join(args), "missing") for args in checker.COMMANDS] + ["", "--help"]:
            with self.subTest(output=output):
                self.responses["rust", ("--help",)] = (0, output, "")
                self.write_launch()
                self.assert_rejected("--help")

    def test_probe_rejects_bad_json_shape_and_duplicate_keys(self):
        valid = self.outputs["probe", "--json"]
        for output in ("not JSON\n", "[]\n", "{}\n", valid.rstrip("\n"), valid + "\n",
                       valid.replace('"ok": true', '"ok": false, "ok": true')):
            with self.subTest(output=output):
                self.responses["rust", ("probe", "--json")] = (0, output, "")
                self.write_launch()
                self.assert_rejected("JSON|Expecting")

    def test_probe_rejects_wrong_platform_and_invalid_fields(self):
        for key, value in (("os", "macos"), ("arch", "aarch64"), ("schema_version", True),
                           ("schema_version", 2), ("ok", False), ("ok", 1),
                           ("source_input_sha256", "short"), ("source_input_sha256", 1),
                           ("native_version", ""), ("native_version", None),
                           ("rustc", "\n"), ("rustc", []), ("extra", "field")):
            with self.subTest(key=key, value=value):
                output = json.dumps({**self.info, key: value}) + "\n"
                self.responses["rust", ("probe", "--json")] = (0, output, "")
                self.write_launch()
                self.assert_rejected("probe JSON")

    def test_self_hash_must_match_manifest_exactly(self):
        valid = self.outputs["probe", "--self-sha256"]
        for output in ("b" * 64 + "\n", valid.upper(), valid.rstrip("\n"), valid + "\n", ""):
            with self.subTest(output=output):
                self.responses["rust", ("probe", "--self-sha256")] = (0, output, "")
                self.write_launch()
                self.assert_rejected("probe --self-sha256")

    def test_unset_and_legacy_reject_bad_status_or_output_for_each_command(self):
        for engine in ("unset", "legacy"):
            for args in checker.COMMANDS:
                good = self.responses[engine, args]
                for response, message in (
                    ((0, "Rust accidentally ran\n", ""), "終了コード"),
                    ((1, "", good[2]), "終了コード"),
                    ((2, "unexpected stdout\n", good[2]), "未設定・legacy"),
                    ((2, "", ""), "未設定・legacy"),
                    ((2, "", "ERROR [engine] DEV_WORKFLOW_ENGINE=rust\n"), "未設定・legacy"),
                    ((2, "", "ERROR [legacy-unavailable]\n"), "未設定・legacy"),
                ):
                    with self.subTest(engine=engine, args=args, response=response):
                        self.responses[engine, args] = response
                        self.write_launch()
                        self.assert_rejected(message)
                self.responses[engine, args] = good

    def test_manifest_rejects_missing_or_malformed_entries(self):
        valid = self.manifest.read_bytes()
        lines = valid.splitlines(keepends=True)
        for contents in (b"", b"".join(lines[:2]), b"".join(reversed(lines)), valid + lines[0],
                         valid.replace(b"  ", b" "), valid.replace(b"\n", b"\r\n"), valid[:-1],
                         valid.upper(), valid.replace(b"linux-arm64", b"linux-../.."),
                         lines[0] + lines[0] + lines[2]):
            with self.subTest(contents=contents):
                self.manifest.write_bytes(contents)
                self.assert_rejected("SHA256SUMS")
        self.assertFalse(self.log.exists())

    def test_file_hash_must_match_before_launch(self):
        (self.bundle / checker.BINARIES[0]).write_bytes(b"modified")
        self.assert_rejected("実行ファイルの SHA256")
        self.assertFalse(self.log.exists())

    def test_required_files_cannot_be_missing_directories_or_symlinks(self):
        for path in (self.launch, self.manifest, self.bundle / checker.BINARIES[0]):
            with self.subTest(path=path):
                backup = path.with_name(path.name + ".saved")
                path.rename(backup)
                try:
                    self.assert_rejected("No such file")
                    path.mkdir()
                    self.assert_rejected("通常ファイル")
                    path.rmdir()
                    path.symlink_to(backup)
                    self.assert_rejected("通常ファイル")
                    path.unlink()
                finally:
                    backup.rename(path)
        self.assertFalse(self.log.exists())

    def test_cpu_directory_cannot_be_a_symlink(self):
        directory = (self.bundle / checker.BINARIES[0]).parent
        backup = directory.with_name("saved-cpu")
        directory.rename(backup)
        directory.symlink_to(backup, target_is_directory=True)
        self.assert_rejected("CPU フォルダ")

    def test_launch_and_selected_binary_need_execute_bits(self):
        for path in (self.launch, self.bundle / checker.BINARIES[0]):
            with self.subTest(path=path):
                path.chmod(0o644)
                self.assert_rejected("実行権限")
                path.chmod(0o755)
        self.assertFalse(self.log.exists())

    def test_unsupported_platform_fails_before_launch(self):
        for system, machine in (("Windows", "x86_64"), ("Linux", "riscv64"), ("Darwin", "aarch64")):
            with self.subTest(system=system, machine=machine):
                self.assert_rejected("検査対象でない", system, machine)
        self.assertFalse(self.log.exists())

    def test_macos_requires_both_architectures(self):
        self.configure("macos", "aarch64", checker.BINARIES[2])
        for architectures in ("x86_64\n", "arm64\n", "x86_64h arm64e\n", ""):
            with self.subTest(architectures=architectures):
                self.mac_tools(architectures)
                self.assert_rejected("lipo -archs", "Darwin", "arm64")
        self.assertFalse(self.log.exists())

    def test_macos_rejects_failed_tools(self):
        self.configure("macos", "x86_64", checker.BINARIES[2])
        for lipo_status, codesign_status, tool in ((1, 0, "lipo"), (0, 1, "codesign")):
            with self.subTest(tool=tool):
                self.mac_tools(lipo_status=lipo_status, codesign_status=codesign_status)
                self.assert_rejected("終了コード.*" + tool, "Darwin", "x86_64")
        self.assertFalse(self.log.exists())

    def test_macos_rejects_missing_tools(self):
        self.configure("macos", "aarch64", checker.BINARIES[2])
        with mock.patch.dict(os.environ, {"PATH": str(self.tools)}):
            self.assert_rejected("実行できません.*lipo", "Darwin", "arm64")
            self.mac_tools()
            (self.tools / "codesign").unlink()
            self.assert_rejected("実行できません.*codesign", "Darwin", "arm64")

    def test_launch_os_error_and_timeout_become_failed_checks(self):
        for error in (OSError("cannot execute"), subprocess.TimeoutExpired("launch.sh", 60)):
            with self.subTest(error=error), mock.patch.object(checker.subprocess, "run", side_effect=error):
                self.assert_rejected("実行できません")

    def test_cli_root_works_outside_repository_and_reports_failures(self):
        os_name, arch, relative = checker.host_target()
        self.configure(os_name, arch, relative)
        if platform.system() == "Darwin":
            self.mac_tools()
        command = [sys.executable, "-B", str(checker.ROOT / "scripts/check_dist_launch.py"), "--root", str(self.root)]
        result = subprocess.run(command, cwd=self.root.parent, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        self.assertIn("配布入口: 合格".encode(), result.stdout)
        self.assertIn(self.hashes[relative].encode(), result.stdout)
        self.launch.unlink()
        result = subprocess.run(command, cwd=self.root.parent, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"ERROR:", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_cli_default_root_and_missing_root(self):
        with mock.patch.object(checker, "ROOT", self.root), mock.patch.object(checker.platform, "system", return_value="Linux"), \
                mock.patch.object(checker.platform, "machine", return_value="x86_64"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checker.main([]), 0)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(checker.main(["--root", str(self.root / "missing")]), 1)
        self.assertIn("ERROR:", errors.getvalue())


class DistWorkflowTest(unittest.TestCase):
    def test_source_commit_shallow_checkout_and_three_runners(self):
        workflow = json.loads((checker.ROOT / ".github/workflows/dist-check.yml").read_text())
        native = json.loads((checker.ROOT / ".github/workflows/native.yml").read_text())
        self.assertEqual(workflow["on"], native["on"])
        self.assertEqual(workflow["permissions"], native["permissions"])
        self.assertEqual(set(workflow["jobs"]), {"launch"})
        job = workflow["jobs"]["launch"]
        self.assertEqual(job["runs-on"], "${{ matrix.runner }}")
        self.assertIs(job["strategy"]["fail-fast"], False)
        self.assertEqual(job["strategy"]["matrix"]["include"], [
            {"platform": "linux-arm64", "runner": "ubuntu-24.04-arm"},
            {"platform": "macos-x86_64", "runner": "macos-15-intel"},
            {"platform": "macos-arm64", "runner": "macos-15"},
        ])
        checkout, policy, launch = job["steps"]
        native_checkout = native["jobs"]["policy"]["steps"][0]
        self.assertEqual(checkout["uses"], native_checkout["uses"])
        self.assertEqual(checkout["with"], {**native_checkout["with"], "fetch-depth": 1})
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertEqual(policy["run"], "python3 -B native/ci/check_workflow.py")
        self.assertEqual(launch["run"], "python3 -B scripts/check_dist_launch.py --root .")
        self.assertEqual([step["uses"] for step in job["steps"] if "uses" in step], [checkout["uses"]])


if __name__ == "__main__":
    unittest.main()
