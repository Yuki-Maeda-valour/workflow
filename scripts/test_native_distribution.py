#!/usr/bin/env python3
"""配布物を起動せずに検査する契約の回帰テスト。実バイナリもネットワークも使わない。"""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from support_load import load_script


SOURCE_REPO = Path(__file__).resolve().parent.parent
VALIDATE = load_script(SOURCE_REPO / "scripts/validate.py", "native_validate")
MAKE_DIST = load_script(SOURCE_REPO / "native/ci/make_dist.py", "native_make_dist")
# 検査器の定数を参照せず、配布する一覧をテスト側でも固定する。
BINARIES = ("linux-x86_64/dev-workflow", "linux-arm64/dev-workflow", "macos-universal/dev-workflow")
FILES = (*BINARIES, "SHA256SUMS", "build-info.json", "launch.sh")
DISTRIBUTION = Path("plugins/dev-workflow/bin/dev-workflow")
LIMIT = 8 * 1024 * 1024


def elf(machine):
    """本体を持たない ELF64 のヘッダ。"""
    return struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9,
                       2, machine, 1, 0, 0, 0, 0, 64, 0, 0, 0, 0, 0)


def macho(*, wide=False, endian=">"):
    """2 CPU の Mach-O ヘッダだけを持つ universal ファイル。"""
    cpus = ((0x01000007, 3), (0x0100000c, 0))
    fields = "IIQQII" if wide else "IIIII"
    offset = 8 + 2 * struct.calcsize(endian + fields)
    data = struct.pack(endian + "II", 0xcafebabf if wide else 0xcafebabe, 2)
    for cpu, subtype in cpus:
        entry = (cpu, subtype, offset, 32, 2)
        data += struct.pack(endian + fields, *entry, *((0,) if wide else ()))
        offset += 32
    for cpu, subtype in cpus:
        data += struct.pack("<IIIIIIII", 0xfeedfacf, cpu, subtype, 2, 0, 0, 0, 0)
    return data


def build_info(hashes):
    return {
        "schema_version": 1,
        "source_commit": "a" * 40,
        "source_input_sha256": "b" * 64,
        "cargo_lock_sha256": "c" * 64,
        "rustc": "rustc 1.99.1 (test fixture)",
        "targets": {
            "x86_64-unknown-linux-musl": {"linker": "rust-lld", "sdk": None},
            "aarch64-unknown-linux-musl": {"linker": "rust-lld", "sdk": None},
            "x86_64-apple-darwin": {"linker": "Apple ld", "sdk": "macOS 15.0"},
            "aarch64-apple-darwin": {"linker": "Apple ld", "sdk": "macOS 15.0"},
        },
        "ci": {"run_id": "123", "run_url": "https://github.com/example/workflow/actions/runs/123"},
        "artifacts": [{"path": name, "sha256": hashes[name]} for name in BINARIES],
    }


def write_metadata(bundle):
    hashes = {name: hashlib.sha256((bundle / name).read_bytes()).hexdigest() for name in BINARIES}
    (bundle / "SHA256SUMS").write_bytes(
        "".join(f"{hashes[name]}  {name}\n" for name in BINARIES).encode("utf-8"))
    (bundle / "build-info.json").write_text(json.dumps(build_info(hashes)) + "\n", encoding="utf-8")
    for name in ("SHA256SUMS", "build-info.json"):
        (bundle / name).chmod(0o644)


def make_bundle(bundle, *, distribution=True):
    for name, data in zip(BINARIES, (elf(62), elf(183), macho())):
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755)
    write_metadata(bundle)
    if distribution:
        (bundle / "launch.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (bundle / "launch.sh").chmod(0o755)
    return bundle


class NativeBundleFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = make_bundle(self.root / "bundle")

    def check(self, mode="distribution"):
        return VALIDATE.check_native_bundle(self.bundle, mode)

    def assert_problem(self, text, mode="distribution"):
        problems = self.check(mode)
        self.assertTrue(any(text in item for item in problems), problems)


class NativeBundleTest(NativeBundleFixture):
    def test_valid_distribution(self):
        before = {name: ((self.bundle / name).read_bytes(), (self.bundle / name).stat().st_mode)
                  for name in FILES}
        self.assertEqual(self.check(), [])
        self.assertEqual(before, {name: ((self.bundle / name).read_bytes(), (self.bundle / name).stat().st_mode)
                                  for name in FILES})

    def test_artifact_has_five_files_and_distribution_requires_launcher(self):
        (self.bundle / "launch.sh").unlink()
        self.assertEqual(sum(p.is_file() for p in self.bundle.rglob("*")), 5)
        self.assertEqual(self.check("artifact"), [])
        self.assert_problem("launch.sh")

    def test_make_dist_uses_the_actual_artifact_contract(self):
        metadata = json.loads((self.bundle / "build-info.json").read_text())
        del metadata["artifacts"]
        output = self.root / "artifact"
        MAKE_DIST.make_dist([self.bundle / name for name in BINARIES], metadata, output)
        self.assertEqual(VALIDATE.check_native_bundle(output, "artifact"), [])
        self.assertEqual(sum(path.is_file() for path in output.rglob("*")), 5)
        self.assertTrue(any("launch.sh" in problem
                            for problem in VALIDATE.check_native_bundle(output, "distribution")))

    def test_artifact_rejects_launcher(self):
        self.assert_problem("余分", "artifact")

    def test_invalid_mode_and_missing_bundle_are_errors(self):
        self.assert_problem("mode", "unknown")
        self.assertTrue(VALIDATE.check_native_bundle(self.root / "missing", "artifact"))

    def test_each_required_file_is_required(self):
        for name in FILES:
            with self.subTest(name=name):
                target = self.bundle / name
                data, mode = target.read_bytes(), target.stat().st_mode
                target.unlink()
                self.assert_problem(f"{name}: 存在しない")
                target.write_bytes(data)
                target.chmod(mode)

    def test_extra_files_and_empty_directories_are_rejected(self):
        for name in ("extra", "linux-x86_64/extra", ".hidden"):
            with self.subTest(name=name):
                target = self.bundle / name
                target.write_bytes(b"extra")
                self.assert_problem("余分")
                target.unlink()
                target.mkdir()
                self.assert_problem("余分")
                target.rmdir()

    def test_each_file_must_be_regular(self):
        for name in FILES:
            with self.subTest(name=name):
                target = self.bundle / name
                data, mode = target.read_bytes(), target.stat().st_mode
                target.unlink()
                target.mkdir()
                self.assert_problem("通常ファイル")
                target.rmdir()
                os.mkfifo(target)
                self.assert_problem("通常ファイル")
                target.unlink()
                target.write_bytes(data)
                target.chmod(mode)

    def test_file_symlinks_inside_outside_and_broken_are_rejected(self):
        outside = self.root / "outside"
        outside.write_bytes(elf(62))
        for name in FILES:
            target = self.bundle / name
            data, mode = target.read_bytes(), target.stat().st_mode
            for destination in (self.bundle / BINARIES[1], outside, self.root / "missing"):
                with self.subTest(name=name, destination=destination):
                    target.unlink()
                    target.symlink_to(destination)
                    self.assert_problem("シンボリックリンク")
                    target.unlink()
                    target.write_bytes(data)
                    target.chmod(mode)

    def test_directory_symlink_is_not_followed(self):
        directory = self.bundle / "linux-x86_64"
        outside = self.root / "outside"
        directory.rename(outside)
        directory.symlink_to(outside, target_is_directory=True)
        self.assert_problem("シンボリックリンク")

    def test_bundle_symlink_is_not_followed(self):
        original = self.root / "original"
        self.bundle.rename(original)
        for destination in (original, self.root / "missing"):
            with self.subTest(destination=destination):
                self.bundle.symlink_to(destination, target_is_directory=True)
                self.assert_problem("配布フォルダ")
                self.bundle.unlink()

    def test_hardlinks_are_rejected(self):
        os.link(self.bundle / BINARIES[0], self.root / "hardlink")
        self.assert_problem("ハードリンク")

    def test_execute_bits_are_checked_and_not_fixed(self):
        for name in FILES:
            with self.subTest(name=name):
                target = self.bundle / name
                before = target.stat().st_mode
                target.chmod(0o644 if name in (*BINARIES, "launch.sh") else 0o755)
                wrong = target.stat().st_mode
                self.assert_problem("実行ビット")
                self.assertEqual(target.stat().st_mode, wrong)
                target.chmod(before)

    def test_each_file_has_size_limit(self):
        for name in FILES:
            with self.subTest(name=name):
                target = self.bundle / name
                data = target.read_bytes()
                with target.open("wb") as stream:
                    stream.truncate(LIMIT + 1)
                self.assert_problem("8 MiB")
                target.write_bytes(data)

    def test_eight_mib_binary_is_allowed(self):
        with (self.bundle / BINARIES[0]).open("ab") as stream:
            stream.truncate(LIMIT)
        write_metadata(self.bundle)
        self.assertEqual(self.check(), [])

    def test_lfs_pointers_are_rejected_for_every_file(self):
        for name in FILES:
            with self.subTest(name=name):
                target = self.bundle / name
                data = target.read_bytes()
                target.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:"
                                   + b"a" * 64 + b"\nsize 123\n")
                self.assert_problem("Git LFS")
                target.write_bytes(data)

    def test_checksums_require_exact_format_order_and_paths(self):
        target = self.bundle / "SHA256SUMS"
        data = target.read_bytes()
        lines = data.splitlines(keepends=True)
        cases = [b"", data[:-1], data + b"\n", data.replace(b"\n", b"\r\n"),
                 data.replace(b"  ", b" "), data.replace(b"  ", b"\t"),
                 data.replace(lines[0][:64], lines[0][:64].upper()),
                 b"".join(reversed(lines)), data + lines[0], b"".join(lines[:2]),
                 lines[0] + lines[0] + lines[2], b"\xef\xbb\xbf" + data, b"\xff" + data]
        cases += [data.replace(BINARIES[0].encode(), name) for name in (
            b"/linux-x86_64/dev-workflow", b"../linux-x86_64/dev-workflow", b"other/dev-workflow",
            b"./linux-x86_64/dev-workflow", b"linux-x86_64/../dev-workflow")]
        for invalid in cases:
            with self.subTest(value=invalid):
                target.write_bytes(invalid)
                self.assert_problem("SHA256SUMS")

    def test_checksums_must_match_binary_bytes(self):
        with (self.bundle / BINARIES[0]).open("ab") as stream:
            stream.write(b"changed")
        self.assert_problem("SHA256SUMS: linux-x86_64/dev-workflow の SHA256")
        self.assert_problem("build-info.json")

    def test_elf_cpu_and_header_are_checked(self):
        for name in BINARIES[:2]:
            target = self.bundle / name
            correct = target.read_bytes()
            cases = [b"", correct[:19], correct[:63], b"MZ" + correct[2:],
                     elf(183 if name == BINARIES[0] else 62)]
            for offset, value in ((4, 1), (5, 2), (5, 0), (6, 0)):
                data = bytearray(correct)
                data[offset] = value
                cases.append(bytes(data))
            for data in cases:
                with self.subTest(name=name, header=data[:20]):
                    target.write_bytes(data)
                    write_metadata(self.bundle)
                    self.assert_problem("ELF")
            target.write_bytes(correct)

    def test_macho_fat32_and_fat64_headers(self):
        for wide in (False, True):
            for endian in ("<", ">"):
                with self.subTest(wide=wide, endian=endian):
                    (self.bundle / BINARIES[2]).write_bytes(macho(wide=wide, endian=endian))
                    write_metadata(self.bundle)
                    self.assertEqual(self.check(), [])

    def test_macho_rejects_missing_wrong_duplicate_and_forged_cpus(self):
        correct = macho()
        cases = [b"", correct[:7], correct[:47], correct[:-1], correct[48:80]]
        # fat count、2 個目の CPU、slice の範囲・重なり・整列、slice の CPU を壊す。
        for offset, value, endian in ((4, 1, ">"), (4, 3, ">"), (28, 0x01000007, ">"),
                                      (28, 12, ">"), (16, 0, ">"), (16, 4096, ">"),
                                      (20, 0, ">"), (20, 4096, ">"), (36, 48, ">"),
                                      (24, 32, ">"), (24, 7, ">"), (52, 12, "<")):
            data = bytearray(correct)
            struct.pack_into(endian + "I", data, offset, value)
            cases.append(bytes(data))
        for data in cases:
            with self.subTest(header=data[:48]):
                (self.bundle / BINARIES[2]).write_bytes(data)
                write_metadata(self.bundle)
                self.assert_problem("Mach-O")

    def test_files_are_never_executed(self):
        marker = self.root / "executed"
        script = f"#!/bin/sh\nprintf executed > {shlex.quote(str(marker))}\n".encode()
        launcher = self.bundle / "launch.sh"
        launcher.write_bytes(script)
        # モックが本当に印を作れることを先に確かめる。
        subprocess.run([str(launcher)], check=True, timeout=5)
        self.assertTrue(marker.exists())
        marker.unlink()
        with mock.patch.object(VALIDATE.subprocess, "run", side_effect=AssertionError("外部実行")):
            self.assertEqual(self.check(), [])
            for name in BINARIES:
                (self.bundle / name).write_bytes(script)
            write_metadata(self.bundle)
            self.assertTrue(self.check())
        self.assertFalse(marker.exists())


class NativeBuildInfoTest(NativeBundleFixture):
    def info(self):
        return json.loads((self.bundle / "build-info.json").read_text(encoding="utf-8"))

    def write_info(self, value):
        (self.bundle / "build-info.json").write_text(json.dumps(value), encoding="utf-8")

    def test_build_info_required_keys(self):
        original = self.info()
        for key in original:
            with self.subTest(key=key):
                value = copy.deepcopy(original)
                del value[key]
                self.write_info(value)
                self.assert_problem(f"必須キー {key}")

    def test_build_info_rejects_invalid_json_and_duplicate_keys_at_every_depth(self):
        original = (self.bundle / "build-info.json").read_text(encoding="utf-8")
        cases = [b"", b"{", b"\xff", b"[]", b"null", b"1", b'"string"',
                 original.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1').encode(),
                 original.replace('"linker": "rust-lld"', '"linker": "rust-lld", "linker": "rust-lld"').encode(),
                 original.replace('"run_id": "123"', '"run_id": "123", "run_id": "123"').encode(),
                 original.replace('"path":', '"path": "bad", "path":').encode(),
                 original.replace('"schema_version": 1', '"schema_version": NaN').encode(),
                 original.replace('"schema_version": 1', '"schema_version": Infinity').encode(),
                 b"[" * 1500 + b"0" + b"]" * 1500]
        for raw in cases:
            with self.subTest(raw=raw[:100]):
                (self.bundle / "build-info.json").write_bytes(raw)
                self.assert_problem("build-info.json")

    def test_build_info_required_values_have_types_and_formats(self):
        original = self.info()
        cases = {
            "schema_version": [None, True, 1.0, "1", 0, 2],
            "source_commit": [None, 123, "a" * 39, "a" * 41, "g" * 40, "A" * 40],
            "source_input_sha256": [None, 123, "", "b" * 63, "g" * 64, "B" * 64],
            "cargo_lock_sha256": [None, 123, "", "c" * 63, "g" * 64, "C" * 64],
            "rustc": [None, 123, "", "  "],
            "targets": [None, [], {}, "targets"],
            "ci": [None, [], "ci"],
            "artifacts": [None, {}, [], "artifacts"],
        }
        for key, invalids in cases.items():
            for invalid in invalids:
                with self.subTest(key=key, invalid=invalid):
                    value = copy.deepcopy(original)
                    value[key] = invalid
                    self.write_info(value)
                    self.assert_problem("build-info.json")

    def test_build_info_targets_require_linker_and_sdk(self):
        original = self.info()
        for target, settings in original["targets"].items():
            for key in ("linker", "sdk"):
                invalids = ["", 123, [], {}] + ([None] if key == "linker" or "apple" in target else [])
                for invalid in invalids:
                    with self.subTest(target=target, key=key, invalid=invalid):
                        value = copy.deepcopy(original)
                        value["targets"][target][key] = invalid
                        self.write_info(value)
                        self.assert_problem(f"targets.{target}.{key}")
                value = copy.deepcopy(original)
                del value["targets"][target][key]
                self.write_info(value)
                self.assert_problem(f"targets.{target}.{key}")
            for invalid in (None, [], "settings"):
                value = copy.deepcopy(original)
                value["targets"][target] = invalid
                self.write_info(value)
                self.assert_problem(f"targets.{target}")
        value = copy.deepcopy(original)
        value["targets"]["unknown-target"] = {"linker": "ld", "sdk": None}
        self.write_info(value)
        self.assert_problem("固定の 4 対象")

    def test_build_info_ci_requires_run_number_and_url(self):
        original = self.info()
        for key, values in {
            "run_id": [None, True, 0, -1, "0", "-1", "", "abc", []],
            "run_url": [None, 123, "", "http://example.test/run", "https://", "relative", "https://[invalid"],
        }.items():
            for invalid in values:
                with self.subTest(key=key, invalid=invalid):
                    value = copy.deepcopy(original)
                    value["ci"][key] = invalid
                    self.write_info(value)
                    self.assert_problem(f"ci.{key}")
            value = copy.deepcopy(original)
            del value["ci"][key]
            self.write_info(value)
            self.assert_problem(f"ci.{key}")

    def test_build_info_artifacts_require_fixed_paths_and_matching_sha256(self):
        original = self.info()
        cases = [[], [None] * 3, ["artifact"] * 3, [{}] * 3,
                 original["artifacts"][:2], original["artifacts"] + original["artifacts"][:1]]
        for path in (None, [], 12, "/linux-x86_64/dev-workflow", "../linux-x86_64/dev-workflow",
                     "./linux-x86_64/dev-workflow", "unknown", BINARIES[1]):
            entries = copy.deepcopy(original["artifacts"])
            entries[0]["path"] = path
            cases.append(entries)
        for digest in (None, [], 12, "d" * 64, "A" * 64, "a" * 63):
            entries = copy.deepcopy(original["artifacts"])
            entries[0]["sha256"] = digest
            cases.append(entries)
        for key in ("path", "sha256"):
            entries = copy.deepcopy(original["artifacts"])
            del entries[0][key]
            cases.append(entries)
        for entries in cases:
            with self.subTest(entries=entries):
                value = copy.deepcopy(original)
                value["artifacts"] = entries
                self.write_info(value)
                self.assert_problem("artifacts")

    def test_build_info_is_internal_consistency_not_current_source(self):
        value = self.info()
        value["source_commit"] = "d" * 64
        value["source_input_sha256"] = "e" * 64
        value["cargo_lock_sha256"] = "f" * 64
        value["ci"]["run_id"] = 456
        value["artifacts"].reverse()
        self.write_info(value)
        (self.root / "native").mkdir()
        (self.root / "native/Cargo.lock").write_text("unrelated source", encoding="utf-8")
        self.assertEqual(self.check(), [])


class NativeGitModeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = make_bundle(self.root / DISTRIBUTION)
        patch = mock.patch.object(VALIDATE, "REPO", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.git("init", "--quiet")
        self.git("add", "--", DISTRIBUTION.as_posix())

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run(["git", "-C", str(self.root), *VALIDATE._NATIVE_SAFE_GIT, *args],
                              stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=15, env=env)

    def check(self):
        return VALIDATE.check_native_bundle(self.bundle, "distribution")

    def test_git_modes_are_required_even_when_disk_modes_are_correct(self):
        self.assertEqual(self.check(), [])
        for name in FILES:
            with self.subTest(name=name):
                executable = name in (*BINARIES, "launch.sh")
                relative = (DISTRIBUTION / name).as_posix()
                self.git("update-index", "--chmod=" + ("-x" if executable else "+x"), "--", relative)
                problems = self.check()
                self.assertTrue(any(f"{name}: Git のモード" in item for item in problems), problems)
                self.git("update-index", "--chmod=" + ("+x" if executable else "-x"), "--", relative)

    def test_untracked_required_file_is_an_error(self):
        self.git("rm", "--cached", "--", (DISTRIBUTION / "launch.sh").as_posix())
        self.assertTrue(any("launch.sh: Git のモード" in item for item in self.check()))

    def test_git_failure_is_an_error(self):
        for error in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 15),
                      subprocess.CalledProcessError(1, "git")):
            with self.subTest(error=error), mock.patch.object(VALIDATE.subprocess, "run", side_effect=error):
                self.assertTrue(any("Git のモードを読み取れない" in item for item in self.check()))

    def test_external_artifacts_do_not_need_git(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        external = make_bundle(Path(temporary.name) / "artifact", distribution=False)
        with mock.patch.object(VALIDATE.subprocess, "run", side_effect=AssertionError("Git を呼んだ")):
            self.assertEqual(VALIDATE.check_native_bundle(external, "artifact"), [])

    def test_git_environment_does_not_select_another_index(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.root / "absent"),
                                          "GIT_INDEX_FILE": str(self.root / "wrong-index")}):
            self.assertEqual(self.check(), [])

    def test_git_fsmonitor_is_not_executed(self):
        marker = self.root / "executed"
        monitor = self.root / "monitor"
        monitor.write_text(f"#!/bin/sh\nprintf executed > {shlex.quote(str(marker))}\n", encoding="utf-8")
        monitor.chmod(0o755)
        self.git("config", "core.fsmonitor", str(monitor))
        self.assertEqual(self.check(), [])
        self.assertFalse(marker.exists())


class NativeMainTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        shutil.copytree(SOURCE_REPO, self.root, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        self.validator = load_script(self.root / "scripts/validate.py", "native_main_validate")

    def run_main(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = self.validator.main()
        return status, output.getvalue()

    def test_absent_distribution_adds_no_warning(self):
        self.assertFalse((self.root / DISTRIBUTION).exists())
        status, output = self.run_main()
        self.assertEqual(status, 0, output)
        self.assertIn("結果: ERROR 0 件 / WARN 0 件", output)

    def test_main_accepts_valid_distribution_and_calls_after_json(self):
        make_bundle(self.root / DISTRIBUTION)
        calls = mock.Mock()
        with mock.patch.object(self.validator, "check_json_files", wraps=self.validator.check_json_files) as metadata, \
                mock.patch.object(self.validator, "check_native_distribution",
                                  wraps=self.validator.check_native_distribution) as native:
            calls.attach_mock(metadata, "metadata")
            calls.attach_mock(native, "native")
            status, output = self.run_main()
        self.assertEqual(calls.mock_calls, [mock.call.metadata(), mock.call.native()])
        self.assertEqual(status, 0, output)
        self.assertIn("結果: ERROR 0 件 / WARN 0 件", output)

    def test_main_rejects_symlink_created_after_copy(self):
        bundle = make_bundle(self.root / DISTRIBUTION)
        target = bundle / BINARIES[0]
        target.unlink()
        target.symlink_to(self.root / "missing")
        status, output = self.run_main()
        self.assertEqual(status, 1, output)
        self.assertIn("シンボリックリンク", output)
        self.assertEqual(self.validator.WARNS, [])

    def test_broken_distribution_and_parent_links_are_not_absence(self):
        for relative in (Path("plugins/dev-workflow/bin"), DISTRIBUTION):
            with self.subTest(relative=relative):
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(self.root / "missing", target_is_directory=True)
                self.validator.ERRORS.clear()
                self.validator.check_native_distribution()
                self.assertTrue(self.validator.ERRORS)
                self.assertEqual(self.validator.WARNS, [])
                target.unlink()


if __name__ == "__main__":
    unittest.main()
