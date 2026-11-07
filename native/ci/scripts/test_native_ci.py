"""CI の単体テスト。実バイナリや他 Task の完成を前提にしない。"""

import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import binary_tools
import build
import check_probe
import check_workflow
import make_dist
import source_input_hash as source_hash


def elf(machine=62, segment=1, needed=False):
    data = bytearray(160)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", data, 16, 2, machine, 1)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HHH", data, 52, 64, 56, 1)
    struct.pack_into("<IIQQQQQQ", data, 64, segment, 5, 128, 0, 0, 32, 32, 8)
    if needed:
        struct.pack_into("<qQ", data, 128, 1, 1)
    return bytes(data)


def macho():
    # fat header と 2 つの Mach-O 64 のヘッダだけを持つ偽の実行ファイル。
    data = bytearray(160)
    struct.pack_into(">II", data, 0, 0xCAFEBABE, 2)
    for index, cpu in enumerate((0x01000007, 0x0100000C)):
        start = 64 + index * 48
        struct.pack_into(">IIIII", data, 8 + index * 20, cpu, 3 if index == 0 else 0, start, 32, 4)
        struct.pack_into("<IIIIIIII", data, start, 0xFEEDFACF, cpu, 3 if index == 0 else 0, 2, 0, 0, 0, 0)
    return bytes(data)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, relative, data=b"test", mode=0o644):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path


class SourceInputTests(TempCase):
    def setUp(self):
        super().setUp()
        source_hash.git(self.root, "init", "--quiet")
        self.write("native/a", b"hello\0\n")
        source_hash.git(self.root, "add", "--", "native/a")

    def test_known_vector(self):
        self.assertEqual(source_hash.source_input_hash(self.root), "64c7696a322f08896afd9e2f6e7d824dc1c780f16282b89d40fdacfbd79c3f06")

    def test_outside_and_untracked_do_not_change_hash(self):
        before = source_hash.source_input_hash(self.root)
        self.write("outside", b"first")
        source_hash.git(self.root, "add", "--", "outside")
        self.write("outside", b"changed")
        self.write("native/untracked", b"not included")
        self.assertEqual(before, source_hash.source_input_hash(self.root))

    def test_content_path_and_index_mode_affect_hash(self):
        before = source_hash.source_input_hash(self.root)
        self.write("native/a", b"changed")
        changed = source_hash.source_input_hash(self.root)
        self.assertNotEqual(before, changed)
        source_hash.git(self.root, "update-index", "--chmod=+x", "--", "native/a")
        mode = source_hash.source_input_hash(self.root)
        self.assertNotEqual(changed, mode)
        (self.root / "native/a").rename(self.root / "native/b")
        source_hash.git(self.root, "add", "--all", "--", "native")
        self.assertNotEqual(mode, source_hash.source_input_hash(self.root))

    def test_symlink_hashes_link_name_not_destination(self):
        self.write("outside", b"a")
        (self.root / "native/link").symlink_to("../outside")
        source_hash.git(self.root, "add", "--", "native/link")
        before = source_hash.source_input_hash(self.root)
        self.write("outside", b"b")
        self.assertEqual(before, source_hash.source_input_hash(self.root))

    def test_rejects_wrong_file_kind_and_parent_link(self):
        (self.root / "native/a").unlink()
        (self.root / "native/a").symlink_to("../outside")
        with self.assertRaises(ValueError):
            source_hash.source_input_hash(self.root)
        (self.root / "native/a").unlink()
        (self.root / "native").rmdir()
        (self.root / "actual").mkdir()
        (self.root / "native").symlink_to("actual")
        with self.assertRaises(ValueError):
            source_hash.source_input_hash(self.root)

    def test_invalid_inventory(self):
        valid = b"100644 " + b"a" * 40 + b" 0\tnative/a\0"
        for raw in (b"", valid[:-1], valid + valid, valid.replace(b" 0\t", b" 1\t"),
                    valid.replace(b"native/a", b"native/../a"), valid.replace(b"native/a", b"outside"),
                    valid.replace(b"native/a", b"native/\xff"), valid.replace(b"native/a", b"native/a\n"),
                    valid.replace(b"100644", b"160000")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                source_hash.parse_entries(raw)

    def test_subdirectory_root_and_missing_tracked_file_fail(self):
        with self.assertRaises(ValueError):
            source_hash.source_input_hash(self.root / "native")
        (self.root / "native/a").unlink()
        with self.assertRaises(OSError):
            source_hash.source_input_hash(self.root)


class MakeDistTests(TempCase):
    def setUp(self):
        super().setUp()
        self.binaries = [self.write("input/" + str(index), data, 0o755)
                         for index, data in enumerate((elf(), elf(183), macho()))]
        self.metadata = {
            "schema_version": 1, "source_commit": "a" * 40,
            "source_input_sha256": "b" * 64, "cargo_lock_sha256": "c" * 64,
            "rustc": "rustc 1.99.1 (test)", "targets": {},
            "ci": {"run_id": "1", "run_url": "https://github.com/example/repo/actions/runs/1"},
        }
        self.output = self.root / "dist"
        self.checker = mock.Mock(return_value=[])
        patch = mock.patch.object(make_dist, "load_bundle_checker", return_value=self.checker)
        patch.start()
        self.addCleanup(patch.stop)

    def test_three_fake_binaries_become_five_files(self):
        inspected = []

        def check(path, mode):
            inspected.append((mode, sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file())))
            return []

        self.checker.side_effect = check
        make_dist.make_dist(self.binaries, self.metadata, self.output)
        expected_names = sorted([*make_dist.BINARY_PATHS, "SHA256SUMS", "build-info.json"])
        self.assertEqual(inspected, [("artifact", expected_names)])
        info = make_dist.read_json(self.output / "build-info.json")
        expected = []
        for source, relative in zip(self.binaries, make_dist.BINARY_PATHS):
            self.assertEqual((self.output / relative).read_bytes(), source.read_bytes())
            self.assertEqual((self.output / relative).stat().st_mode & 0o777, 0o755)
            expected.append({"path": relative, "sha256": binary_tools.sha256(source)})
        self.assertEqual(info["artifacts"], expected)
        self.assertEqual((self.output / "SHA256SUMS").read_bytes(), "".join(f"{x['sha256']}  {x['path']}\n" for x in expected).encode())
        self.assertEqual((self.output / "build-info.json").stat().st_mode & 0o777, 0o644)
        self.assertFalse((self.output / "launch.sh").exists())

    def test_failed_bundle_check_does_not_publish(self):
        self.checker.return_value = ["壊れた ELF"]
        with self.assertRaisesRegex(ValueError, "壊れた ELF"):
            make_dist.make_dist(self.binaries, self.metadata, self.output)
        self.assertFalse(self.output.exists())

    def test_non_executable_is_not_fixed(self):
        self.binaries[0].chmod(0o644)
        with self.assertRaises(ValueError):
            make_dist.make_dist(self.binaries, self.metadata, self.output)
        self.assertEqual(self.binaries[0].stat().st_mode & 0o777, 0o644)

    def test_symlink_and_oversize_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.binaries[0])
        with self.assertRaises(ValueError):
            make_dist.make_dist([link, *self.binaries[1:]], self.metadata, self.output)
        with self.binaries[0].open("ab") as output:
            output.truncate(8 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            make_dist.make_dist(self.binaries, self.metadata, self.output)

    def test_existing_output_and_supplied_artifacts_rejected(self):
        self.output.mkdir()
        with self.assertRaises(ValueError):
            make_dist.make_dist(self.binaries, self.metadata, self.output)
        self.output.rmdir()
        with self.assertRaises(ValueError):
            make_dist.make_dist(self.binaries, {**self.metadata, "artifacts": []}, self.output)

    def test_json_duplicate_keys_rejected(self):
        path = self.write("bad.json", b'{"schema_version":1,"schema_version":2}')
        with self.assertRaises(ValueError):
            make_dist.read_json(path)


class CheckerImportTests(TempCase):
    def test_loads_repository_validate_by_path(self):
        self.write("scripts/validate.py", b'def check_native_bundle(path, mode):\n    return [str(path), mode]\n')
        with mock.patch.object(make_dist, "ROOT", self.root):
            checker = make_dist.load_bundle_checker()
        self.assertEqual(checker(Path("sample"), "artifact"), ["sample", "artifact"])


class WorkflowTests(TempCase):
    def setUp(self):
        super().setUp()
        self.document = make_dist.read_json(source_hash.ROOT / ".github/workflows/native.yml")

    def test_actual_workflow_is_valid(self):
        self.assertEqual(check_workflow.check_document(self.document), [])
        self.assertEqual(check_workflow.check_workflows(), [])

    def test_unsafe_mutations_fail(self):
        changes = (
            lambda d: d["on"].update(pull_request_target={}),
            lambda d: d.update(permissions={"contents": "write"}),
            lambda d: d["jobs"]["build"].update(permissions={"id-token": "write"}),
            lambda d: d["jobs"]["build"].update(**{"continue-on-error": True}),
            lambda d: d["jobs"]["policy"]["steps"][0]["with"].pop("persist-credentials"),
            lambda d: d["jobs"]["policy"]["steps"][0]["with"].update(**{"persist-credentials": True}),
            lambda d: d["jobs"]["policy"]["steps"][0]["with"].update(ref="${{ github.sha }}"),
            lambda d: d["jobs"]["policy"]["steps"][0].update(uses="actions/checkout@v4"),
            lambda d: d["jobs"]["policy"]["steps"].append({"uses": "actions/cache@" + "a" * 40}),
            lambda d: d["jobs"]["policy"]["steps"].append({"uses": "./local"}),
            lambda d: d["jobs"]["policy"]["steps"].append({"run": "cargo build --release"}),
            lambda d: d["jobs"]["policy"]["steps"][1].update(run="python3 -B native/ci/build.py test | true"),
            lambda d: d["jobs"]["policy"]["steps"][1].update(**{"if": "false"}),
            lambda d: d.update(env={"RUSTFLAGS": "-C opt-level=1"}),
            lambda d: d["jobs"]["policy"]["steps"][0]["with"].update(token="${{ secrets.PAT }}"),
            lambda d: d["jobs"]["policy"]["steps"][0]["with"].update(token="${{ toJSON(secrets) }}"),
            lambda d: d["jobs"]["policy"]["steps"][0]["with"].update(token="${{ github.token }}"),
            lambda d: d["jobs"]["policy"]["steps"][0]["with"].update(token="${{ toJSON(github) }}"),
            lambda d: d["jobs"]["policy"]["steps"][1].update(shell="bash -c 'exit 0' {0}"),
            lambda d: d["jobs"]["policy"]["steps"][1].update(**{"working-directory": "elsewhere"}),
            lambda d: d["jobs"]["build"]["strategy"]["matrix"]["include"][0].update(platform="linux-x86_64'; echo bypass; '"),
        )
        for change in changes:
            document = copy.deepcopy(self.document)
            change(document)
            with self.subTest(change=change):
                self.assertTrue(check_workflow.check_document(document))

    def test_checks_all_yml_and_yaml_files(self):
        folder = self.root / ".github/workflows"
        folder.mkdir(parents=True)
        make_dist.write_json(folder / "one.yml", self.document)
        make_dist.write_json(folder / "two.yaml", self.document)
        self.assertEqual(check_workflow.check_workflows(self.root), [])
        (folder / "two.yaml").write_text('on: [pull_request_target]\n')
        self.assertTrue(check_workflow.check_workflows(self.root))

    def test_duplicates_or_empty_directory_are_not_success(self):
        self.assertTrue(check_workflow.check_workflows(self.root))
        self.write(".github/workflows/a.yml", b'{"permissions":{},"permissions":{"contents":"read"}}')
        self.assertTrue(check_workflow.check_workflows(self.root))

    def test_final_artifact_waits_for_both_mac_hosts(self):
        jobs = self.document["jobs"]
        self.assertEqual(len(jobs["build"]["strategy"]["matrix"]["include"]), 4)
        self.assertIn("verify-universal", jobs["distribution"]["needs"])
        rows = jobs["verify-universal"]["strategy"]["matrix"]["include"]
        self.assertEqual({row["runner"] for row in rows}, {"macos-15-intel", "macos-15"})
        self.assertEqual(jobs["verify-universal"]["needs"], "combine")


class BinaryTests(TempCase):
    def test_static_elf_accepts_both_cpus(self):
        for machine in (62, 183):
            binary_tools.check_static_elf(self.write(str(machine), elf(machine)), machine)

    def test_elf_rejects_dynamic_or_wrong_cpu_or_broken_header(self):
        for data in (elf(183), elf(segment=3), elf(segment=2, needed=True), elf()[:80], b"not ELF"):
            with self.subTest(data=data[:8]), self.assertRaises(ValueError):
                binary_tools.check_static_elf(self.write("binary", data), 62)

    def test_static_pie_without_needed_is_allowed(self):
        binary_tools.check_static_elf(self.write("binary", elf(segment=2)), 62)

    def test_tar_roundtrip_preserves_execute_permissions(self):
        source = self.root / "source"
        self.write("source/dev-workflow", elf(), 0o755)
        self.write("source/record.json", b"{}\n")
        archive = self.root / "result.tar"
        binary_tools.pack_tar(source, archive, ["dev-workflow", "record.json"])
        result = binary_tools.unpack_tar(archive, self.root / "received", ["dev-workflow", "record.json"])
        self.assertEqual((result / "dev-workflow").stat().st_mode & 0o777, 0o755)
        self.assertEqual((result / "dev-workflow").read_bytes(), elf())

    def test_tar_rejects_links_and_traversal(self):
        for index, name in enumerate(("dev-workflow", "../escape", "/absolute")):
            archive = self.root / f"{index}.tar"
            with tarfile.open(archive, "w") as tar:
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = "outside"
                info.mode = 0o755
                tar.addfile(info)
            with self.assertRaises(ValueError):
                binary_tools.unpack_tar(archive, self.root / str(index), [name])


class ProbeTests(TempCase):
    def setUp(self):
        super().setUp()
        self.binary = self.write("binary", b"fake executable", 0o755)
        self.write("native/Cargo.toml", b'version = "0.1.0"\n')
        self.info = {"schema_version": 1, "ok": True, "os": "linux", "arch": "x86_64",
                     "native_version": "0.1.0", "source_input_sha256": "b" * 64, "rustc": "rustc 1.99.1 (test)"}
        self.support = SimpleNamespace(require_native_binary=mock.Mock(return_value=self.binary),
                                       run_command=mock.Mock(side_effect=self.command),
                                       snapshot_tree=mock.Mock(return_value={}))
        for patch in (mock.patch.object(check_probe, "source_input_hash", return_value="b" * 64),
                      mock.patch.object(check_probe.platform, "system", return_value="Linux"),
                      mock.patch.object(check_probe.platform, "machine", return_value="x86_64")):
            patch.start()
            self.addCleanup(patch.stop)

    def command(self, argv, **kwargs):
        args = argv[1:]
        output, error, code = b"", b"", 0
        if args == ["probe", "--json"]:
            output = json.dumps(self.info).encode() + b"\n"
        elif args == ["probe", "--self-sha256"]:
            output = hashlib.sha256(self.binary.read_bytes()).hexdigest().encode() + b"\n"
        elif args == ["--version"]:
            output = ("dev-workflow 0.1.0 (source_input_sha256: " + "b" * 64 + ")\n").encode()
        elif args == ["--help"]:
            output = b"--version\n--help\nprobe --json\nprobe --self-sha256\n"
        else:
            code, error = 2, b"invalid arguments\n"
        return SimpleNamespace(returncode=code, stdout=output, stderr=error, timed_out=False)

    def test_valid_probe_uses_support_and_checks_all_four_commands(self):
        self.assertEqual(check_probe.check_probe(self.support, self.root), self.info)
        self.support.require_native_binary.assert_called_once_with()
        self.assertEqual(self.support.run_command.call_count, 14)

    def test_missing_binary_is_failure(self):
        self.support.require_native_binary.side_effect = ValueError("missing binary")
        with self.assertRaisesRegex(ValueError, "missing binary"):
            check_probe.check_probe(self.support, self.root)
        self.support.run_command.assert_not_called()

    def test_source_hash_cpu_and_version_mismatch_fail(self):
        for key, value in (("source_input_sha256", "c" * 64), ("arch", "aarch64"),
                           ("rustc", "rustc 1.98.0 (test)"), ("schema_version", True)):
            original = self.info[key]
            self.info[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_probe.check_probe(self.support, self.root)
            self.info[key] = original

    def test_timeout_and_filesystem_changes_fail(self):
        self.support.run_command.side_effect = None
        self.support.run_command.return_value = SimpleNamespace(returncode=0, timed_out=True, stdout=b"", stderr=b"")
        with self.assertRaises(ValueError):
            check_probe.check_probe(self.support, self.root)
        self.support.run_command.side_effect = self.command
        self.support.snapshot_tree.side_effect = [{}, {"created": {}}]
        with self.assertRaisesRegex(ValueError, "作業フォルダ"):
            check_probe.check_probe(self.support, self.root)


class BuildTests(TempCase):
    def test_external_settings_cannot_override_source_inputs(self):
        for name in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_PROFILE_RELEASE_OPT_LEVEL", "CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER"):
            with mock.patch.dict(os.environ, {name: "test"}, clear=True), self.assertRaises(ValueError):
                build.build_environment(self.root, "linux-x86_64")

    def test_mac_minimum_and_deterministic_time_are_set_in_script(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(build, "run", return_value="/SDK"):
            env = build.build_environment(self.root, "macos-arm64")
        self.assertEqual(env["MACOSX_DEPLOYMENT_TARGET"], "11.0")
        self.assertEqual(env["SOURCE_DATE_EPOCH"], "0")
        self.assertEqual(env["ZERO_AR_DATE"], "1")

    def test_workspace_must_be_outside_repository(self):
        with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": str(source_hash.ROOT / "native/target")}):
            with self.assertRaises(ValueError):
                build.workspace()

    def test_signature_has_fixed_identifier_and_no_timestamp(self):
        with mock.patch.object(build, "run", return_value="x86_64 arm64") as run:
            build.sign(self.root / "binary")
        self.assertIn("--timestamp=none", run.call_args_list[0].args[0])
        self.assertIn(build.SIGN_ID, run.call_args_list[0].args[0])
        self.assertIn("--verify", run.call_args_list[1].args[0])

    def reproduce_with_bytes(self, second):
        output_dirs = []

        def run(args, *, env=None, capture=False):
            if args[:2] == ["cargo", "build"]:
                target_dir = Path(env["CARGO_TARGET_DIR"])
                self.assertEqual(list(target_dir.iterdir()), [])
                output_dirs.append(target_dir)
                self.assertEqual(args, ["cargo", "build", "--release", "--locked", "--target", "test-target"])
                binary = target_dir / "test-target/release/dev-workflow"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(elf() if len(output_dirs) == 1 else second)
            elif args[0] == "cmp":
                subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.fail(f"unexpected command: {args}")

        with mock.patch.object(build, "target_config", return_value=("test-target", 62)), \
                mock.patch.object(build, "tool_record", return_value={"linker": "test"}), \
                mock.patch.object(build, "identity", return_value={"rustc": "rustc 1.99.1 (test)"}), \
                mock.patch.object(build, "run", side_effect=run), \
                mock.patch.dict(os.environ, {}, clear=True):
            folder = build.reproduce("linux-x86_64", self.root)
        self.assertEqual(len(output_dirs), 2)
        self.assertNotEqual(output_dirs[0], output_dirs[1])
        self.assertTrue(all(not path.exists() for path in output_dirs))
        return folder

    def test_reproduction_uses_two_empty_targets_and_compares_bytes(self):
        folder = self.reproduce_with_bytes(elf())
        record = make_dist.read_json(folder / "record.json")
        self.assertEqual(record["files"]["1/dev-workflow"], record["files"]["2/dev-workflow"])

    def test_reproduction_rejects_different_bytes(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.reproduce_with_bytes(elf() + b"changed")

    def test_combine_compares_after_both_signatures(self):
        left = self.write("left/1/dev-workflow", b"left1").parent.parent
        right = self.write("right/1/dev-workflow", b"right1").parent.parent
        self.write("left/2/dev-workflow", b"left1")
        self.write("right/2/dev-workflow", b"right1")
        signed = []

        def sign(path):
            path.write_bytes(path.read_bytes() + b"signed")
            signed.append(path)

        def run(args, *, env=None, capture=False):
            if args[:2] == ["lipo", "-create"]:
                Path(args[-1]).write_bytes(Path(args[2]).read_bytes() + Path(args[3]).read_bytes())
            elif args[0] == "cmp":
                self.assertEqual(list(args[1:]), signed)
                subprocess.run(args, check=True)
            else:
                return "tool version"

        with mock.patch.object(build, "extract_build", side_effect=[(left, {"targets": {"x86": {}}}), (right, {"targets": {"arm": {}}})]), \
                mock.patch.object(build, "identity", return_value={}), \
                mock.patch.object(build, "run", side_effect=run), \
                mock.patch.object(build, "sign", side_effect=sign), \
                mock.patch.object(build.shutil, "which", return_value=sys.executable):
            build.combine(self.root, self.root)
        self.assertEqual(len(signed), 2)
        output = binary_tools.unpack_tar(self.root / "macos-universal.tar", self.root / "unpacked", ["dev-workflow", "record.json"])
        self.assertEqual((output / "dev-workflow").read_bytes(), b"left1right1signed")


if __name__ == "__main__":
    unittest.main()
