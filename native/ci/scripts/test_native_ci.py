"""CI の単体・結合テスト。配布物の検査と実行補助はリポジトリの実装を使う。"""

import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
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
import check_resolve_task_dir
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
            "rustc": "rustc 1.99.1 (test)",
            "targets": {
                "x86_64-unknown-linux-musl": {"linker": "GNU ld", "sdk": "musl 1.2.5"},
                "aarch64-unknown-linux-musl": {"linker": "GNU ld", "sdk": "musl 1.2.5"},
                "x86_64-apple-darwin": {"linker": "Apple ld", "sdk": "15.0"},
                "aarch64-apple-darwin": {"linker": "Apple ld", "sdk": "15.0"},
            },
            "ci": {"run_id": "1", "run_url": "https://github.com/example/repo/actions/runs/1"},
        }
        self.output = self.root / "dist"
        self.checker = make_dist.load_bundle_checker()

    def test_three_fake_binaries_become_five_files(self):
        make_dist.make_dist(self.binaries, self.metadata, self.output)
        expected_names = sorted([*make_dist.BINARY_PATHS, "SHA256SUMS", "build-info.json"])
        self.assertEqual(sorted(p.relative_to(self.output).as_posix()
                                for p in self.output.rglob("*") if p.is_file()), expected_names)
        self.assertEqual(self.checker(self.output, "artifact"), [])
        self.assertTrue(any("launch.sh" in problem for problem in self.checker(self.output, "distribution")))
        info = make_dist.read_json(self.output / "build-info.json")
        self.assertEqual({key: value for key, value in info.items() if key != "artifacts"}, self.metadata)
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
        self.binaries[0].write_bytes(elf(183))
        with self.assertRaisesRegex(ValueError, "ELF"):
            make_dist.make_dist(self.binaries, self.metadata, self.output)
        self.assertFalse(self.output.exists())

    def test_incomplete_metadata_does_not_publish(self):
        self.metadata["targets"] = {}
        with self.assertRaisesRegex(ValueError, "targets"):
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

    def test_native_comparison_job_timeouts_are_fixed(self):
        for job,minutes in (("build",60),("verify-universal",30)):
            self.assertEqual(self.document["jobs"][job]["timeout-minutes"],minutes)
            for display_name in ("Native", "Renamed", None):
                for renamed_job in (False, True):
                    base=copy.deepcopy(self.document)
                    if display_name is None:base.pop("name")
                    else:base["name"]=display_name
                    key=job
                    if renamed_job:
                        key="comparison"
                        base["jobs"][key]=base["jobs"].pop(job)
                    self.assertEqual(check_workflow.check_document(base),[])
                    for value in (None,0,15,40,minutes-1,minutes+1,str(minutes),True):
                        changed=copy.deepcopy(base)
                        if value is None:changed["jobs"][key].pop("timeout-minutes")
                        else:changed["jobs"][key]["timeout-minutes"]=value
                        with self.subTest(job=job,display_name=display_name,renamed_job=renamed_job,value=value):
                            self.assertTrue(check_workflow.check_document(changed))

    def test_native_build_option_first_forms_are_rejected_even_with_correct_timeout(self):
        for job,operation,minutes in (("build","build",60),("verify-universal","verify-universal",30)):
            for arguments in (f"--platform linux-x86_64 {operation}", f"--downloads downloads {operation}", f"-- {operation}"):
                for value in (minutes,None,1):
                    changed=copy.deepcopy(self.document)
                    steps=changed["jobs"][job]["steps"]
                    step=next(step for step in steps if check_workflow.native_build_command(step.get("run"))==operation)
                    step["run"]="python3 -B native/ci/build.py "+arguments
                    if value is None:changed["jobs"][job].pop("timeout-minutes")
                    else:changed["jobs"][job]["timeout-minutes"]=value
                    with self.subTest(job=job,arguments=arguments,value=value):
                        errors=check_workflow.check_document(changed)
                        self.assertTrue(any("コマンド名をオプションより先" in error for error in errors))

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

    def test_only_the_exact_dist_launch_command_is_allowed_outside_native_ci(self):
        document = copy.deepcopy(self.document)
        document["jobs"]["policy"]["steps"].append({"name": "配布の確かめ", "run": check_workflow.DIST_LAUNCH_COMMAND})
        self.assertEqual(check_workflow.check_document(document), [])
        for command in (
                "python3 -B scripts/check_dist_launch.py",
                "python3 -B scripts/check_dist_launch.py --root /",
                "python3 -B scripts/check_dist_launch.py --root . --extra",
                "python3 -B scripts/check_dist_launch.py --root .; curl example.com",
                "python3 -B scripts/check_dist_launch.py --root . && true",
                "python3 -B scripts/other.py --root .",
                "python3 scripts/check_dist_launch.py --root .",
                " python3 -B scripts/check_dist_launch.py --root ."):
            document = copy.deepcopy(self.document)
            document["jobs"]["policy"]["steps"].append({"name": "配布の確かめ", "run": command})
            with self.subTest(command=command):
                self.assertTrue(check_workflow.check_document(document))

    def test_runner_mutations_fail(self):
        for runner in ("self-hosted", "ubuntu-latest", ["ubuntu-24.04"],
                       {"group": "private"}, None, "${{ matrix.platform }}", "${{ matrix.runner }}"):
            document = copy.deepcopy(self.document)
            document["jobs"]["policy"]["runs-on"] = runner
            with self.subTest(runner=runner):
                self.assertTrue(check_workflow.check_document(document))
        for mutation in (
                lambda j: j.pop("runs-on"),
                lambda j: j.pop("strategy"),
                lambda j: j.update(strategy=None),
                lambda j: j["strategy"]["matrix"]["include"][0].update(runner="self-hosted"),
                lambda j: j["strategy"]["matrix"]["include"][0].update(platform="unknown", runner=None),
                lambda j: j["strategy"]["matrix"].update(runner=["self-hosted"]),
                lambda j: j["strategy"]["matrix"].update(include=[])):
            document = copy.deepcopy(self.document)
            mutation(document["jobs"]["build"])
            with self.subTest(mutation=mutation):
                self.assertTrue(check_workflow.check_document(document))
        for runner in ("ubuntu-24.04", "ubuntu-24.04-arm", "macos-15-intel", "macos-15"):
            document = copy.deepcopy(self.document)
            document["jobs"]["policy"]["runs-on"] = runner
            self.assertEqual(check_workflow.check_document(document), [])

    def test_unknown_top_job_and_strategy_keys_fail(self):
        for scope, key in ((scope, key) for scope in ("top", "job")
                           for key in ("environment", "outputs", "concurrency", "future-setting")):
            document = copy.deepcopy(self.document)
            target = document if scope == "top" else document["jobs"]["build"]
            target[key] = {}
            with self.subTest(scope=scope, key=key):
                self.assertTrue(check_workflow.check_document(document))
        document = copy.deepcopy(self.document)
        document["jobs"]["build"]["strategy"]["future-setting"] = True
        self.assertTrue(check_workflow.check_document(document))

    def test_action_input_mutations_fail(self):
        cases = {
            "actions/checkout": {"ssh-key": "private-key", "submodules": True,
                                 "fetch-depth": 0, "token": "literal", "path": "other"},
            "actions/upload-artifact": {"overwrite": True, "include-hidden-files": True},
            "actions/download-artifact": {"repository": "other/repo", "run-id": 123,
                                          "github-token": "literal"},
        }
        for action, mutations in cases.items():
            for key, value in {**mutations, "future-setting": True}.items():
                document = copy.deepcopy(self.document)
                step = next(step for job in document["jobs"].values() for step in job["steps"]
                            if step.get("uses", "").startswith(action + "@"))
                step.setdefault("with", {})[key] = value
                with self.subTest(action=action, key=key):
                    self.assertTrue(check_workflow.check_document(document))
            for settings in ([], None, "name=test"):
                document = copy.deepcopy(self.document)
                step = next(step for job in document["jobs"].values() for step in job["steps"]
                            if step.get("uses", "").startswith(action + "@"))
                step["with"] = settings
                with self.subTest(action=action, settings=settings):
                    self.assertTrue(check_workflow.check_document(document))
        for depth in (-1, 2, True, "1", None, "${{ matrix.platform }}"):
            document = copy.deepcopy(self.document)
            document["jobs"]["policy"]["steps"][0]["with"]["fetch-depth"] = depth
            with self.subTest(depth=depth):
                self.assertTrue(check_workflow.check_document(document))

    def test_event_value_mutations_fail(self):
        for event in ("pull_request", "push", "workflow_dispatch"):
            for value in ({"tags": ["*"]}, {"branches": ["main"]}, {"paths": ["native/**"]},
                          {"types": ["closed"]}, {"inputs": {}}, {"future-setting": {}},
                          [], "", True):
                document = copy.deepcopy(self.document)
                document["on"][event] = value
                with self.subTest(event=event, value=value):
                    self.assertTrue(check_workflow.check_document(document))
            document = copy.deepcopy(self.document)
            document["on"][event] = None
            self.assertEqual(check_workflow.check_document(document), [])

    def test_second_workflow_can_check_distribution_from_shallow_checkout(self):
        document = copy.deepcopy(self.document)
        document["name"] = "Distribution check"
        job = document["jobs"]["policy"]
        document["jobs"] = {"distribution-check": job}
        job["steps"][0]["with"]["fetch-depth"] = 1
        # 入口の実行は 0b のスクリプトに置く。ここでは呼び出しの形だけを検査する。
        job["steps"][2]["run"] = "python3 -B native/ci/check_distribution.py"
        folder = self.root / ".github/workflows"
        folder.mkdir(parents=True)
        make_dist.write_json(folder / "native.yml", self.document)
        make_dist.write_json(folder / "dist-check.yml", document)
        self.assertEqual(check_workflow.check_workflows(self.root), [])
        for mutate in (
                lambda d: d["jobs"]["distribution-check"].update(**{"runs-on": "self-hosted"}),
                lambda d: d.update(concurrency="group"),
                lambda d: d["jobs"]["distribution-check"]["steps"][0]["with"].update(submodules=True),
                lambda d: d["on"]["push"].update(tags=["*"])):
            changed = copy.deepcopy(document)
            mutate(changed)
            make_dist.write_json(folder / "dist-check.yml", changed)
            problems = check_workflow.check_workflows(self.root)
            self.assertTrue(problems)
            self.assertTrue(all(problem.startswith("dist-check.yml:") for problem in problems))

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


class ProbeSupportIntegrationTests(TempCase):
    def setUp(self):
        super().setUp()
        source_hash.git(self.root, "init", "--quiet")
        self.write("native/Cargo.toml", b'version = "0.1.0"\n')
        source_hash.git(self.root, "add", "--", "native/Cargo.toml")
        self.info = {
            "schema_version": 1, "ok": True,
            "os": {"Linux": "linux", "Darwin": "macos"}[check_probe.platform.system()],
            "arch": {"x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}[check_probe.platform.machine()],
            "native_version": "0.1.0", "source_input_sha256": source_hash.source_input_hash(self.root),
            "rustc": "rustc 1.99.1 (test)",
        }
        self.binary = self.write("probe", (
            f"#!{sys.executable}\n"
            "import hashlib, json, sys\nfrom pathlib import Path\n"
            f"info = {self.info!r}\n"
            "args = sys.argv[1:]\n"
            "if args == ['probe', '--json']:\n    print(json.dumps(info))\n"
            "elif args == ['probe', '--self-sha256']:\n"
            "    print(hashlib.sha256(Path(__file__).read_bytes()).hexdigest())\n"
            "elif args == ['--version']:\n"
            "    print('dev-workflow 0.1.0 (source_input_sha256: ' + info['source_input_sha256'] + ')')\n"
            "elif args == ['--help']:\n    print('--version\\n--help\\nprobe --json\\nprobe --self-sha256')\n"
            "else:\n    print('invalid arguments', file=sys.stderr)\n    sys.exit(2)\n"
        ).encode(), 0o755)
        self.support = check_probe.load_support()

    def test_actual_support_runs_probe_and_snapshots(self):
        with mock.patch.dict(os.environ, {"DEV_WORKFLOW_TEST_NATIVE_BINARY": str(self.binary)}):
            self.assertEqual(check_probe.check_probe(self.support, self.root), self.info)

    def test_actual_support_detects_side_effects(self):
        with self.binary.open("a") as output:
            output.write("Path('created').write_text('side effect')\n")
        with mock.patch.dict(os.environ, {"DEV_WORKFLOW_TEST_NATIVE_BINARY": str(self.binary)}):
            with self.assertRaisesRegex(ValueError, "作業フォルダ"):
                check_probe.check_probe(self.support, self.root)

    def test_cli_without_binary_fails_with_actual_support(self):
        env = dict(os.environ)
        env.pop("DEV_WORKFLOW_TEST_NATIVE_BINARY", None)
        result = subprocess.run([sys.executable, "-B", check_probe.__file__], env=env,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn("DEV_WORKFLOW_TEST_NATIVE_BINARY が未設定", result.stderr.decode())


class BuildTests(TempCase):
    def test_prepare_installs_comparison_dependencies_with_the_running_python(self):
        with mock.patch.object(build, "target_config", return_value=("test-target", None)), \
                mock.patch.object(build, "run") as run:
            build.prepare("linux-x86_64")
        self.assertEqual(run.call_args_list[0].args[0],
                         [sys.executable, "-m", "pip", "install", "-r", source_hash.ROOT / "requirements-dev.txt"])
        self.assertIn(mock.call(["rustup", "target", "add", "--toolchain", build.TOOLCHAIN, "test-target"]),
                      run.call_args_list)

    def test_prepare_dependency_failure_stops_before_build_tool_setup(self):
        with mock.patch.object(build, "target_config", return_value=("test-target", None)), \
                mock.patch.object(build, "run", side_effect=subprocess.CalledProcessError(1, "pip")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                build.prepare("linux-x86_64")
        self.assertEqual(run.call_count, 1)


    def test_wrapper_preserves_cargo_binary_environment_and_remaps_paths(self):
        binary = str(self.root / "dev-workflow")
        result = subprocess.run(
            [source_hash.ROOT / "native/.cargo/rustc-wrapper.sh", sys.executable, "-c",
             "import json, os, sys; print(json.dumps([os.environ['CARGO_BIN_EXE_dev-workflow'], sys.argv[1:]]))"],
            env={**os.environ, "CARGO_BIN_EXE_dev-workflow": binary, "CARGO_TARGET_DIR": str(self.root)},
            check=True, capture_output=True, text=True, timeout=10,
        )
        inherited, arguments = json.loads(result.stdout)
        self.assertEqual(inherited, binary)
        self.assertIn(f"--remap-path-prefix={self.root}=/target", arguments)

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
                self.assertEqual(args, ["cargo", "build", "--release", "--locked", "-p", "dev-workflow-cli", "--bin", "dev-workflow", "--example", "r4b_contract", "--example", "loop_state_fixture", "--target", "test-target"])
                binary = target_dir / "test-target/release/dev-workflow"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(elf() if len(output_dirs) == 1 else second)
                fixture=binary.parent / "examples/r4b_contract"
                fixture.parent.mkdir()
                fixture.write_bytes(elf())
                (binary.parent / "examples/loop_state_fixture").write_bytes(elf())
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
        for name in ("left", "right"):
            for number in (1,2):
                self.write(f"{name}/{number}/r4b_contract", b"fixture")
                self.write(f"{name}/{number}/loop_state_fixture", b"loop fixture")
        signed = []

        def sign(path):
            path.write_bytes(path.read_bytes() + b"signed")
            signed.append(path)

        def run(args, *, env=None, capture=False):
            if args[:2] == ["lipo", "-create"]:
                Path(args[-1]).write_bytes(Path(args[2]).read_bytes() + Path(args[3]).read_bytes())
            elif args[0] == "cmp":
                self.assertEqual(list(args[1:]), signed[-2:])
                subprocess.run(args, check=True)
            else:
                return "tool version"

        with mock.patch.object(build, "extract_build", side_effect=[(left, {"targets": {"x86": {}}}), (right, {"targets": {"arm": {}}})]), \
                mock.patch.object(build, "identity", return_value={}), \
                mock.patch.object(build, "run", side_effect=run), \
                mock.patch.object(build, "sign", side_effect=sign), \
                mock.patch.object(build.shutil, "which", return_value=sys.executable):
            build.combine(self.root, self.root)
        self.assertEqual(len(signed), 6)
        self.assertEqual({path.name for path in signed},set(build.CI_BINARIES))
        output = binary_tools.unpack_tar(self.root / "macos-universal.tar", self.root / "unpacked", ["dev-workflow", "r4b_contract", "loop_state_fixture", "record.json"])
        self.assertEqual((output / "dev-workflow").read_bytes(), b"left1right1signed")


class ResolveTaskDirTests(TempCase):
    def test_python_version_and_binary_selection_are_mandatory(self):
        support = SimpleNamespace(require_native_binary=mock.Mock(side_effect=ValueError("missing binary")))
        for version in ((3, 9), (3, 11), (3, 12)):
            with mock.patch.object(check_resolve_task_dir.sys, "version_info", version), self.assertRaisesRegex(ValueError, "Python 3.10"):
                check_resolve_task_dir.check_resolve_task_dir(support)
        support.require_native_binary.assert_not_called()
        with mock.patch.object(check_resolve_task_dir.sys, "version_info", (3, 10)), self.assertRaisesRegex(ValueError, "missing binary"):
            check_resolve_task_dir.check_resolve_task_dir(support)

    def test_comparison_requires_both_trees_to_equal_the_initial_tree(self):
        comparison = SimpleNamespace(matches=True, legacy=SimpleNamespace(returncode=0),
                                     before={"before": 1}, legacy_tree={}, native_tree={})
        support = SimpleNamespace(compare_runs=mock.Mock(return_value=comparison))
        with self.assertRaisesRegex(ValueError, "初期のファイル"):
            check_resolve_task_dir.check_case(support, self.root / "binary", self.root / "initial", self.root / "work", [], 0)
        comparison.legacy_tree = comparison.native_tree = comparison.before
        check_resolve_task_dir.check_case(support, self.root / "binary", self.root / "initial", self.root / "work", [], 0)
        comparison.matches = False
        comparison.differences = ("stdout",)
        comparison.native = "native-result"
        with self.assertRaisesRegex(ValueError, "不一致"):
            check_resolve_task_dir.check_case(support, self.root / "binary", self.root / "initial", self.root / "work", [], 0)

    def test_root_does_not_pass_permission_cases(self):
        for check in (check_resolve_task_dir.permission_cases, check_resolve_task_dir.permission_child_metadata_case):
            with mock.patch.object(check_resolve_task_dir.os, "geteuid", return_value=0), self.assertRaisesRegex(ValueError, "通常利用者"):
                check(None, self.root / "binary", self.root)

    def native_non_utf8_result(self, argv, **kwargs):
        # 本体も検査対象の期待値生成も呼ばず、固定した 3 入力の JSON バイトを作る。
        root = os.fsencode(Path(kwargs["cwd"]).resolve())
        if argv[-1] == b"--task-dir=x\xff":
            output = b'{"path": "' + root + b'/x\xff", "source": "profile", "task_dir": "x\xff"}\n'
            code = 0
        elif argv[-1] == b"\xff":
            output = b'{"path": "' + root + b'/\xff/docs/tasks", "source": "default", "task_dir": "docs/tasks"}\n'
            code = 0
        else:
            output = (b'{"candidates": ["\xff", "' + "\ue000".encode() + b'", "' + "\U00010000".encode()
                      + b'"], "error": "' + "タスクディレクトリの候補が複数あります。task_dir を明示してください".encode() + b'"}\n')
            code = 2
        return SimpleNamespace(returncode=code, stdout=output, stderr=b"", timed_out=False)

    def non_utf8_support(self, mutate=None):
        actual = check_probe.load_support()

        def command(argv, **kwargs):
            old = argv[0] == sys.executable
            result = actual.run_command(argv, **kwargs) if old else self.native_non_utf8_result(argv, **kwargs)
            result = SimpleNamespace(**vars(result))
            if mutate:
                mutate(argv, kwargs, result)
            return result

        return SimpleNamespace(snapshot_tree=actual.snapshot_tree, run_command=mock.Mock(side_effect=command))

    def test_non_utf8_both_modes_use_actual_legacy_and_independent_native_bytes(self):
        support = self.non_utf8_support()
        # policy の新しい Python でも、子の traceback 表示を比較元の 3.10 と同じ形にする。
        with mock.patch.dict(os.environ, {"PYTHONNODEBUGRANGES": "1"}):
            total = check_resolve_task_dir.non_utf8_cases(support, self.root / "binary", self.root)
        self.assertIn(total, (2, 6))  # APFS の実在名の拒否だけは、明示値の 2 件を必須にする。
        if total == 2:
            self.assertEqual(check_resolve_task_dir.platform.system(), "Darwin")
        calls = support.run_command.call_args_list
        self.assertEqual(len(calls), total * 3)
        for probe, legacy, native in zip(calls[::3], calls[1::3], calls[2::3]):
            self.assertEqual(probe.kwargs, legacy.kwargs)
            self.assertEqual(probe.args[0][:3], legacy.args[0][:3])
            self.assertEqual(probe.args[0][4:], legacy.args[0][4:])
            self.assertIn("sys.stdout.encoding, sys.stdout.errors", probe.args[0][3])
            self.assertEqual(legacy.kwargs["env"], native.kwargs["env"])
            self.assertEqual(legacy.kwargs["env"]["PYTHONNODEBUGRANGES"], "1")
        self.assertEqual({call.kwargs["env"]["PYTHONIOENCODING"] for call in calls},
                         {"utf-8:strict", "utf-8:surrogateescape"})

    def test_non_utf8_rejects_wrong_exceptions_results_streams_and_expected_bytes(self):
        for mode in ("strict", "surrogateescape"):
            work = self.root / mode
            work.mkdir()
            initial = work / "initial"
            initial.mkdir()
            support = self.non_utf8_support()
            check_resolve_task_dir.check_non_utf8_case(support, self.root / "binary", initial, work / "valid", "explicit", mode)
            calls = support.run_command.call_args_list
            old = check_probe.load_support().run_command(*calls[1].args, **calls[1].kwargs)
            new = self.native_non_utf8_result(*calls[2].args, **calls[2].kwargs)
            old = SimpleNamespace(**vars(old))
            folders = [work / "valid" / side for side in ("legacy", "native")]
            payloads = [check_resolve_task_dir.non_utf8_expectation("explicit", folder)[1] for folder in folders]
            mutations = [("old-timeout", "old", "timed_out", True), ("new-timeout", "new", "timed_out", True),
                         ("old-code", "old", "returncode", 2), ("new-code", "new", "returncode", 1),
                         ("new-stderr", "new", "stderr", b"diagnostic"),
                         ("new-stdout", "new", "stdout", new.stdout.replace(b"\xff", b"?")),
                         ("new-space", "new", "stdout", new.stdout.replace(b'": ', b'":'))]
            if mode == "strict":
                self.assertIn(b"UnicodeEncodeError:", old.stderr)
                for label, before, after in (
                    ("type", b"UnicodeEncodeError:", b"ValueError:"),
                    ("encoding", b"'utf-8'", b"'ascii'"),
                    ("character", b"\\udcff", b"\\udcfe"),
                    ("reason", b"surrogates not allowed", b"invalid start byte"),
                    ("function", b"in main\n", b"in resolve\n"),
                    ("statement", b"json.dumps(result,", b"json.dumps(payload,"),
                    ("source", os.fsencode(check_resolve_task_dir.LEGACY), b"other.py"),
                ):
                    altered = old.stderr.replace(before, after)
                    self.assertNotEqual(altered, old.stderr)
                    mutations.append((label, "old", "stderr", altered))
                position = re.sub(rb"in position [0-9]+:", b"in position 0:", old.stderr)
                line = re.sub(rb", line [0-9]+, in main", b", line 1, in main", old.stderr)
                self.assertNotEqual(position, old.stderr)
                self.assertNotEqual(line, old.stderr)
                mutations.extend([("position", "old", "stderr", position), ("line", "old", "stderr", line),
                                  ("extra-exception", "old", "stderr", b"RuntimeError: unexpected\n" + old.stderr),
                                  ("old-stdout", "old", "stdout", b"unexpected"),
                                  ("old-success", "old", "returncode", 0)])
            else:
                mutations.extend([("old-stderr", "old", "stderr", b"diagnostic"),
                                  ("old-stdout", "old", "stdout", old.stdout.replace(b"\xff", b"?")),
                                  ("old-code-one", "old", "returncode", 1)])
            for name, side, field, value in mutations:
                changed_old, changed_new = copy.copy(old), copy.copy(new)
                setattr(changed_old if side == "old" else changed_new, field, value)
                with self.subTest(mode=mode, mutation=name), self.assertRaises(ValueError):
                    check_resolve_task_dir.check_non_utf8_results(changed_old, changed_new, mode=mode,
                                                                 payloads=payloads, code=0, folders=folders)
            if mode == "surrogateescape":
                old.stdout = old.stdout.replace(b"\xff", b"?")
                new.stdout = new.stdout.replace(b"\xff", b"?")
                with self.assertRaises(ValueError):
                    check_resolve_task_dir.check_non_utf8_results(old, new, mode=mode, payloads=payloads, code=0, folders=folders)

    def test_non_utf8_candidate_context_and_resolution_exit_code_are_exact(self):
        folders = [self.root / side for side in ("legacy", "native")]
        for folder in folders:
            folder.mkdir()
        payloads = [check_resolve_task_dir.non_utf8_expectation("candidates", folder)[1] for folder in folders]
        # 候補名を作れない APFS でも、専用判定の拒否検査は省かない。
        old = SimpleNamespace(returncode=1, stdout=b"", timed_out=False,
                              stderr=check_resolve_task_dir.strict_exception(payloads[0], 2))
        new = self.native_non_utf8_result(["binary", "resolve-task-dir", "--project-root", "."], cwd=folders[1])
        check_resolve_task_dir.check_non_utf8_results(old, new, mode="strict", payloads=payloads, code=2, folders=folders)
        for before, after in ((b"legacy_task_dir.ResolutionError:", b"RuntimeError:"),
                              (b"in resolve\n", b"in unrelated\n"),
                              (b"json.dumps(payload,", b"json.dumps(result,")):
            changed = copy.copy(old)
            changed.stderr = old.stderr.replace(before, after)
            self.assertNotEqual(changed.stderr, old.stderr)
            with self.subTest(before=before), self.assertRaises(ValueError):
                check_resolve_task_dir.check_non_utf8_results(changed, new, mode="strict", payloads=payloads, code=2, folders=folders)
        for code in (0, 1):
            changed = copy.copy(new)
            changed.returncode = code
            with self.subTest(code=code), self.assertRaises(ValueError):
                check_resolve_task_dir.check_non_utf8_results(old, changed, mode="strict", payloads=payloads, code=2, folders=folders)

    def test_non_utf8_requires_actual_stdout_settings_and_unchanged_trees(self):
        for mode in ("strict", "surrogateescape"):
            for mutation in ("encoding", "errors", "probe-code", "probe-stderr", "probe-timeout", "old-content", "new-content", "mode", "link", "kind"):
                work = self.root / (mode + "-" + mutation)
                work.mkdir()
                initial = work / "initial"
                initial.mkdir()
                (initial / "unchanged").write_bytes(b"original")
                (initial / "unchanged").chmod(0o644)
                (initial / "link").symlink_to("unchanged")

                def mutate(argv, kwargs, result):
                    old = argv[0] == sys.executable
                    probe = old and "sys.stdout.encoding, sys.stdout.errors" in argv[3]
                    folder = kwargs["cwd"]
                    if probe:
                        if mutation == "encoding":
                            result.stdout = result.stdout.replace(b"utf-8", b"ascii")
                        elif mutation == "errors":
                            result.stdout = result.stdout.replace(mode.encode(), b"replace")
                        elif mutation == "probe-code":
                            result.returncode = 1
                        elif mutation == "probe-stderr":
                            result.stderr = b"diagnostic"
                        elif mutation == "probe-timeout":
                            result.timed_out = True
                    elif (mutation == "old-content" and old) or (mutation == "new-content" and not old):
                        (folder / "unchanged").write_bytes(b"changed")
                    elif mutation == "mode":
                        (folder / "unchanged").chmod(0o600)
                    elif mutation == "link":
                        (folder / "link").unlink()
                        (folder / "link").symlink_to("absent")
                    elif mutation == "kind":
                        (folder / "unchanged").unlink()
                        (folder / "unchanged").mkdir()

                with self.subTest(mode=mode, mutation=mutation), self.assertRaises(ValueError):
                    check_resolve_task_dir.check_non_utf8_case(self.non_utf8_support(mutate), self.root / "binary",
                                                              initial, work / "case", "explicit", mode)

    def test_non_utf8_only_skips_name_creation_eilseq_on_mac(self):
        for system, error, skipped in (("Darwin", errno.EILSEQ, True), ("Darwin", errno.EACCES, False),
                                       ("Linux", errno.EILSEQ, False)):
            work = self.root / (system + str(error))
            work.mkdir()

            def state(root, directory):
                if directory == os.fsdecode(b"\xff"):
                    raise OSError(error, "name creation failed")

            with mock.patch.object(check_resolve_task_dir.platform, "system", return_value=system), \
                    mock.patch.object(check_resolve_task_dir, "state", side_effect=state), \
                    mock.patch.object(check_resolve_task_dir, "check_non_utf8_case") as check, \
                    mock.patch("builtins.print") as report:
                if skipped:
                    self.assertEqual(check_resolve_task_dir.non_utf8_cases(None, self.root / "binary", work), 2)
                    self.assertIn("EILSEQ", report.call_args.args[0])
                else:
                    with self.assertRaises(OSError):
                        check_resolve_task_dir.non_utf8_cases(None, self.root / "binary", work)
                    report.assert_not_called()
                self.assertEqual([(call.args[-2], call.args[-1]) for call in check.call_args_list],
                                 [("explicit", "strict"), ("explicit", "surrogateescape")])
        work = self.root / "execution-error"
        work.mkdir()
        with mock.patch.object(check_resolve_task_dir.platform, "system", return_value="Darwin"), \
                mock.patch.object(check_resolve_task_dir, "state"), \
                mock.patch.object(check_resolve_task_dir, "check_non_utf8_case", side_effect=[None, None, OSError(errno.EILSEQ, "execution failed")]), \
                self.assertRaises(OSError):
            check_resolve_task_dir.non_utf8_cases(None, self.root / "binary", work)

    def test_normal_comparison_rejects_the_same_strict_output_difference(self):
        actual = check_probe.load_support()
        initial = self.root / "initial"
        initial.mkdir()
        original_run = actual.run_command

        def command(argv, **kwargs):
            if argv[0] == sys.executable:
                return original_run(argv, **kwargs)
            return self.native_non_utf8_result(argv, **kwargs)

        with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "utf-8:strict"}), \
                mock.patch.object(actual, "run_command", side_effect=command), self.assertRaisesRegex(ValueError, "不一致"):
            check_resolve_task_dir.check_case(actual, self.root / "binary", initial, self.root / "case",
                                              ["--project-root", ".", b"--task-dir=x\xff"], 0)
        self.assertNotIn("non-utf8-explicit", [case[0] for case in check_resolve_task_dir.cases()])
        self.assertIn((["--project-root=.", b"--unknown\xff"], 2), list(check_resolve_task_dir.parser_cases()))

    @unittest.skipIf(os.geteuid() == 0, "0400 の権限拒否は通常利用者が必須。実バイナリの比較では root を失敗にする")
    def test_child_metadata_permission_checks_each_output_exactly_and_restores_the_tree(self):
        actual_support = check_probe.load_support()
        for mutation in (None, "type", "errno", "path", "old-stdout", "json-key", "json-space", "new-stderr", "timeout", "code", "mode", "content"):
            parent = self.root / (mutation or "valid")
            parent.mkdir()

            def command(argv, **kwargs):
                folder = Path(argv[-1])
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o400)
                self.assertNotEqual(kwargs["cwd"], folder)
                target = folder.resolve(strict=True) / "child"
                error = str(PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target)))
                old = argv[0] == sys.executable
                result = SimpleNamespace(returncode=1, stdout=b"", stderr=b"", timed_out=False)
                if old:
                    result.stderr = ("Traceback (most recent call last):\n  test frame\nPermissionError: " + error + "\n").encode()
                    if mutation == "type":
                        result.stderr = result.stderr.replace(b"PermissionError:", b"FileNotFoundError:")
                    elif mutation == "errno":
                        result.stderr = result.stderr.replace(f"[Errno {errno.EACCES}]".encode(), b"[Errno 2]")
                    elif mutation == "path":
                        result.stderr = result.stderr.replace(os.fsencode(target), os.fsencode(target) + b"-wrong")
                    elif mutation == "old-stdout":
                        result.stdout = b"unexpected output"
                    elif mutation == "mode":
                        folder.chmod(0o500)
                    elif mutation == "content":
                        folder.chmod(0o700)
                        (folder / "child/進行中_作業.md").write_bytes(b"changed")
                        folder.chmod(0o400)
                else:
                    result.stdout = (json.dumps({"error": error}, ensure_ascii=False, sort_keys=True) + "\n").encode()
                    if mutation == "json-key":
                        result.stdout = result.stdout.replace(b'"error"', b'"other"')
                    elif mutation == "json-space":
                        result.stdout = result.stdout.replace(b'": ', b'":')
                    elif mutation == "new-stderr":
                        result.stderr = b"unexpected diagnostic"
                    elif mutation == "timeout":
                        result.timed_out = True
                    elif mutation == "code":
                        result.returncode = 2
                return result

            support = SimpleNamespace(snapshot_tree=actual_support.snapshot_tree,
                                      run_command=mock.Mock(side_effect=command))
            with self.subTest(mutation=mutation):
                if mutation is None:
                    check_resolve_task_dir.permission_child_metadata_case(support, self.root / "binary", parent)
                    self.assertEqual(support.run_command.call_count, 2)
                else:
                    with self.assertRaises(ValueError):
                        check_resolve_task_dir.permission_child_metadata_case(support, self.root / "binary", parent)
                for side in ("legacy", "native"):
                    folder = parent / ("permission-child-metadata-" + side)
                    initial = parent / "permission-initial-child-metadata"
                    self.assertEqual(stat.S_IMODE(folder.stat().st_mode), stat.S_IMODE(initial.stat().st_mode))

    def test_main_returns_failure_for_version_missing_binary_and_comparison_errors(self):
        for error in (ValueError("Python 3.10"), ValueError("missing binary"), ValueError("comparison differs")):
            with mock.patch.object(check_resolve_task_dir, "check_resolve_task_dir", side_effect=error):
                self.assertEqual(check_resolve_task_dir.main(), 1)

    def test_actual_cli_rejects_missing_empty_absent_and_non_executable_binary(self):
        path = self.write("not-executable", b"not an executable", 0o644)
        for value in (None, "", str(self.root / "absent"), str(path)):
            environment = dict(os.environ)
            if value is None:
                environment.pop("DEV_WORKFLOW_TEST_NATIVE_BINARY", None)
            else:
                environment["DEV_WORKFLOW_TEST_NATIVE_BINARY"] = value
            result = subprocess.run([sys.executable, "-B", check_resolve_task_dir.__file__], env=environment,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertTrue(result.stderr.startswith(b"ERROR:"))


class ResolveTaskDirConnectionTests(TempCase):
    def test_probe_runs_both_required_checks_with_the_same_binary_and_python(self):
        binary = self.root / "binary"
        with mock.patch.object(build, "run") as run:
            build.probe(binary)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            [sys.executable, "-B", source_hash.ROOT / "native/ci/check_probe.py"],
            [sys.executable, "-B", source_hash.ROOT / "native/ci/check_resolve_task_dir.py"]])
        self.assertTrue(all(call.kwargs["env"]["DEV_WORKFLOW_TEST_NATIVE_BINARY"] == str(binary)
                            for call in run.call_args_list))
        with mock.patch.object(build, "run", side_effect=[None, subprocess.CalledProcessError(1, "comparison")]):
            with self.assertRaises(subprocess.CalledProcessError):
                build.probe(binary)

    def test_build_and_reproduce_check_each_of_the_two_binaries_for_all_four_platforms(self):
        for command in ("build", "reproduce"):
            for target in build.TARGETS:
                with mock.patch.object(sys, "argv", ["build.py", command, "--platform", target]), \
                        mock.patch.object(build, "workspace", return_value=self.root), \
                        mock.patch.object(build, "quality"), \
                        mock.patch.object(build, "reproduce", return_value=self.root), \
                        mock.patch.object(build, "pack_tar"), \
                        mock.patch.object(build, "r4b_probe"), mock.patch.object(build, "loop_state_probe"), \
                        mock.patch.object(build, "probe") as probe:
                    self.assertEqual(build.main(), 0)
                    self.assertEqual(probe.call_args_list, [mock.call(self.root / "1/dev-workflow"), mock.call(self.root / "2/dev-workflow")])
                with mock.patch.object(sys, "argv", ["build.py", command, "--platform", target]), \
                        mock.patch.object(build, "workspace", return_value=self.root), \
                        mock.patch.object(build, "quality"), \
                        mock.patch.object(build, "reproduce", return_value=self.root), \
                        mock.patch.object(build, "r4b_probe"), mock.patch.object(build, "loop_state_probe"), \
                        mock.patch.object(build, "probe", side_effect=subprocess.CalledProcessError(1, "comparison")):
                    self.assertEqual(build.main(), 1)

    def test_signed_universal_binary_runs_both_checks_and_propagates_failure(self):
        with mock.patch.object(build, "extract_universal", return_value=(self.root, {})), \
                mock.patch.object(build, "run", return_value="arm64 x86_64"), \
                mock.patch.object(build, "r4b_probe"), mock.patch.object(build, "loop_state_probe"), \
                        mock.patch.object(build, "probe") as probe:
            build.verify_universal(self.root, self.root)
            probe.assert_called_once_with(self.root / "dev-workflow")
            probe.side_effect = subprocess.CalledProcessError(1, "comparison")
            with self.assertRaises(subprocess.CalledProcessError):
                build.verify_universal(self.root, self.root)

    def test_workflow_requires_fixed_python_after_the_initial_policy_check(self):
        document = json.loads((source_hash.ROOT / ".github/workflows/native.yml").read_text())
        for job in ("build", "verify-universal"):
            steps = document["jobs"][job]["steps"]
            python_step = steps[2]
            self.assertEqual(python_step["uses"], "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0")
            self.assertEqual(python_step["with"], {"python-version": "3.10.11"})
            for mutation in ("remove", "late", "early", "version", "cache", "tag"):
                changed = copy.deepcopy(document)
                altered = changed["jobs"][job]["steps"]
                if mutation == "remove":
                    altered.pop(2)
                elif mutation == "late":
                    altered.append(altered.pop(2))
                elif mutation == "early":
                    altered.insert(1, altered.pop(2))
                elif mutation == "version":
                    altered[2]["with"]["python-version"] = "3.11"
                elif mutation == "cache":
                    altered[2]["with"]["cache"] = "pip"
                elif mutation == "tag":
                    altered[2]["uses"] = "actions/setup-python@v7"
                with self.subTest(job=job, mutation=mutation):
                    self.assertTrue(check_workflow.check_document(changed))


class R4BConnectionTests(TempCase):
    def test_quality_requires_example_tests_and_comparison_failure_is_fatal(self):
        with mock.patch.object(build,"build_environment",return_value={}),mock.patch.object(build,"run") as run:
            build.quality("linux-x86_64",self.root)
        self.assertIn(["cargo","test","--locked","-p","dev-workflow-cli","--example","r4b_contract"],[call.args[0] for call in run.call_args_list])
        fixture=self.root/"r4b_contract"
        with mock.patch.object(build,"run") as run:
            build.r4b_probe(fixture)
        self.assertEqual(run.call_args.args[0],[sys.executable,"-B",source_hash.ROOT/"native/ci/check_r4b.py","--fixture",fixture])
        with mock.patch.object(build,"run",side_effect=subprocess.CalledProcessError(1,"comparison")):
            with self.assertRaises(subprocess.CalledProcessError):build.r4b_probe(fixture)

    def test_each_build_and_signed_cpu_requires_r4b_comparison(self):
        for command in ("build","reproduce"):
            for platform_name in build.TARGETS:
                with mock.patch.object(sys,"argv",["build.py",command,"--platform",platform_name]),mock.patch.object(build,"workspace",return_value=self.root),mock.patch.object(build,"quality"),mock.patch.object(build,"reproduce",return_value=self.root),mock.patch.object(build,"pack_tar"),mock.patch.object(build,"probe"),mock.patch.object(build,"loop_state_probe"),mock.patch.object(build,"r4b_probe") as check:
                    self.assertEqual(build.main(),0)
                    self.assertEqual(check.call_args_list,[mock.call(self.root/"1/r4b_contract"),mock.call(self.root/"2/r4b_contract")])
                    check.side_effect=subprocess.CalledProcessError(1,"R4B")
                    self.assertEqual(build.main(),1)
        with mock.patch.object(build,"extract_universal",return_value=(self.root,{})),mock.patch.object(build,"run",return_value="arm64 x86_64"),mock.patch.object(build,"probe"),mock.patch.object(build,"loop_state_probe"),mock.patch.object(build,"r4b_probe") as check:
            build.verify_universal(self.root,self.root)
            check.assert_called_once_with(self.root/"r4b_contract")
            check.side_effect=subprocess.CalledProcessError(1,"R4B")
            with self.assertRaises(subprocess.CalledProcessError):build.verify_universal(self.root,self.root)

    def test_transfer_file_allowlist_and_hashes_are_exact(self):
        for name in build.BUILD_FILES:self.write(name,b"fixture",0o755)
        record={"files":{name:binary_tools.sha256(self.root/name) for name in build.BUILD_FILES}}
        make_dist.write_json(self.root/"record.json",record)
        with mock.patch.object(build,"identity",return_value={}):
            self.assertEqual(build.read_record(self.root),record)
            for mutation in ("missing","hash","unlisted","extra-record"):
                changed=copy.deepcopy(record)
                fixture=self.root/"1/r4b_contract"
                if mutation=="missing":fixture.rename(self.root/"saved")
                elif mutation=="hash":fixture.write_bytes(b"changed")
                elif mutation=="unlisted":self.write("unexpected",b"x")
                else:changed['files']['unexpected']='0'*64
                make_dist.write_json(self.root/"record.json",changed)
                with self.assertRaises(ValueError):build.read_record(self.root)
                if mutation=="missing":(self.root/"saved").rename(fixture)
                elif mutation=="hash":fixture.write_bytes(b"fixture")
                elif mutation=="unlisted":(self.root/"unexpected").unlink()
            make_dist.write_json(self.root/"record.json",record)

    def test_fixture_is_not_a_distribution_binary(self):
        self.assertEqual(build.CI_BINARIES,("dev-workflow","r4b_contract","loop_state_fixture"))
        self.assertFalse(any("r4b" in path for path in make_dist.BINARY_PATHS))
        self.assertEqual(set(build.BUILD_FILES),{f"{number}/{name}" for number in (1,2) for name in ("dev-workflow","r4b_contract","loop_state_fixture")})


class S2LConnectionTests(TempCase):
    def test_quality_requires_units_and_mutations_once_on_all_platforms(self):
        for platform_name in build.TARGETS:
            with mock.patch.object(build,"build_environment",return_value={}),mock.patch.object(build,"run") as run:
                build.quality(platform_name,self.root)
            commands=[call.args[0]for call in run.call_args_list]
            for name in ('check_loop_state_units.py','check_loop_state_mutations.py'):
                self.assertEqual(commands.count([sys.executable,'-B',source_hash.ROOT/'native/ci'/name]),1)
            self.assertNotIn(['cargo','test','--locked','-p','dev-workflow-cli','--example','loop_state_fixture'],commands)

    def test_each_build_and_signed_cpu_requires_cli_and_selftest(self):
        for platform_name in build.TARGETS:
            with mock.patch.object(sys,'argv',['build.py','build','--platform',platform_name]),mock.patch.object(build,'workspace',return_value=self.root),mock.patch.object(build,'quality'),mock.patch.object(build,'reproduce',return_value=self.root),mock.patch.object(build,'pack_tar'),mock.patch.object(build,'probe'),mock.patch.object(build,'r4b_probe'),mock.patch.object(build,'loop_state_probe')as check:
                self.assertEqual(build.main(),0)
                self.assertEqual(check.call_args_list,[mock.call(self.root/'1/loop_state_fixture'),mock.call(self.root/'2/loop_state_fixture')])
                check.side_effect=subprocess.CalledProcessError(1,'S2L');self.assertEqual(build.main(),1)
        with mock.patch.object(build,'extract_universal',return_value=(self.root,{})),mock.patch.object(build,'run',return_value='arm64 x86_64'),mock.patch.object(build,'probe'),mock.patch.object(build,'r4b_probe'),mock.patch.object(build,'loop_state_probe')as check:
            build.verify_universal(self.root,self.root);check.assert_called_once_with(self.root/'loop_state_fixture')
            check.side_effect=subprocess.CalledProcessError(1,'S2L')
            with self.assertRaises(subprocess.CalledProcessError):build.verify_universal(self.root,self.root)
        fixture=self.root/'loop_state_fixture'
        with mock.patch.object(build,'run')as run:build.loop_state_probe(fixture)
        self.assertEqual([c.args[0]for c in run.call_args_list],[[sys.executable,'-B',source_hash.ROOT/'native/ci'/name,'--fixture',fixture]for name in ('check_loop_state.py','test_loop_state.py')])
        with mock.patch.object(build,'run',side_effect=subprocess.CalledProcessError(1,'S2L')):
            with self.assertRaises(subprocess.CalledProcessError):build.loop_state_probe(fixture)


if __name__ == "__main__":
    unittest.main()
