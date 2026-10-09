"""secret-profiles.py と snapshot の開始時 profile 保護の回帰テスト。"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "plugins/dev-workflow/skills/do-task/scripts/secret-profiles.py"
SNAPSHOT = ROOT / "plugins/dev-workflow/skills/do-task/scripts/diff-snapshot.sh"
REVIEW_AGENT = ROOT / "plugins/dev-workflow/skills/do-task/scripts/review-agent.sh"


def load_helper_module():
    spec = importlib.util.spec_from_file_location("secret_profiles_test_module", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SecretProfilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", str(self.repo), cwd=self.repo.parent)
        self.git("-c", "user.name=t", "-c", "user.email=t", "commit", "--allow-empty", "-qm", "init")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                              check=True, stdin=subprocess.DEVNULL, capture_output=True)

    def commit(self, message):
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def run_helper(self, *args, no_site=False):
        command = [sys.executable]
        if no_site:
            command.append("-S")
        command.extend([str(HELPER), "--cwd", str(self.repo), *args])
        return subprocess.run(command, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True)

    def items(self, done):
        self.assertEqual(0, done.returncode, done.stderr.decode())
        return [item.decode() for item in done.stdout.split(b"\0") if item]

    def test_unattended_update_doc_review_tree_uses_start_base_current_and_defaults_once(self):
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        profile.write_text("secret_paths:\n  - base.txt\n", encoding="utf-8")
        (self.repo / "base.txt").write_text("BASE_SECRET\n", encoding="utf-8")
        (self.repo / "public.txt").write_text("PUBLIC_BASE_187\n", encoding="utf-8")
        base = self.commit("base")
        profile.write_text("secret_paths:\n  - start.txt\n", encoding="utf-8")
        (self.repo / "start.txt").write_text("START_SECRET\n", encoding="utf-8")
        start = self.commit("start")
        profile.write_text("secret_paths:\n  - current.txt\n", encoding="utf-8")
        (self.repo / "current.txt").write_text("CURRENT_SECRET\n", encoding="utf-8")
        (self.repo / "public.txt").write_text("PUBLIC_SOURCE_187\n", encoding="utf-8")

        values = self.items(self.run_helper("--ref", base, "--ref", start, "--ref", start))
        self.assertEqual([".env", ".env.*", ".dev.vars", "current.txt", "base.txt", "start.txt"], values)

        out = self.repo / "out.md"
        patch = self.repo / "out.patch"
        done = subprocess.run(["bash", str(SNAPSHOT), "--cwd", str(self.repo), "--base", base,
                               "--out", str(out), "--secret-profile-ref", base,
                               "--secret-profile-ref", start, "--patch-out", str(patch),
                               "--patch-base", base], env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(0, done.returncode, done.stderr.decode())
        body = out.read_text(encoding="utf-8")
        for marker in ("BASE_SECRET", "START_SECRET", "CURRENT_SECRET"):
            self.assertNotIn(marker, body)
        for name in ("base.txt", "start.txt", "current.txt"):
            self.assertIn(name, body)
        self.assertIn("PUBLIC_SOURCE_187", body)
        self.assertIn("PUBLIC_SOURCE_187", patch.read_text(encoding="utf-8"))

        review = Path(self.temp.name) / "review"
        self.git("worktree", "add", "-q", "--detach", str(review), base)
        # 無人 update-doc の外部レビュー経路どおり、helper の B/S/current の結果を
        # snapshot と同じ GNU ERE にして、一時ツリーの一致パスを patch 適用前に除去する。
        ere_args = ["bash", str(SNAPSHOT), "--print-exclude-ere"]
        for value in values:
            ere_args.extend(["--exclude-glob", value])
        ere_done = subprocess.run(ere_args, env=self.env, stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(0, ere_done.returncode, ere_done.stderr.decode())
        ere = ere_done.stdout.strip()
        for target in sorted(review.rglob("*"), reverse=True):
            if not target.is_file() and not target.is_symlink():
                continue
            relative = os.fsencode(str(target.relative_to(review)))
            matched = subprocess.run(["grep", "-Eiqz", "--", ere], input=relative + b"\0", capture_output=True)
            if matched.returncode == 0:
                target.unlink()
            else:
                self.assertEqual(1, matched.returncode, matched.stderr.decode())
        self.git("apply", "--check", str(patch), cwd=review)
        self.git("apply", str(patch), cwd=review)
        for marker in ("BASE_SECRET", "START_SECRET", "CURRENT_SECRET"):
            self.assertFalse(any(marker in item.read_text(encoding="utf-8")
                                 for item in review.rglob("*") if item.is_file() and ".git" not in item.parts))
        self.assertEqual("PUBLIC_SOURCE_187\n", (review / "public.txt").read_text(encoding="utf-8"))

        # review-agent.sh を実際に起動する。stub は一時ツリーを読み、機密値が一つでも
        # 残ればプローブも本実行も失敗するので、共通集合を外部経路へ渡す陽性対照になる。
        stub = Path(self.temp.name) / "review-stub.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "if grep -R -F -e BASE_SECRET -e START_SECRET -e CURRENT_SECRET --exclude-dir=.git .; then exit 9; fi\n"
            "test \"$(cat public.txt)\" = PUBLIC_SOURCE_187 || exit 8\n"
            "printf '%s\\n' '{\"verdict\":\"APPROVED\",\"issues\":[]}'\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        prompt = Path(self.temp.name) / "prompt.md"
        prompt.write_text("review\n", encoding="utf-8")
        done = subprocess.run(
            ["bash", str(REVIEW_AGENT), "--runner", "fixture", "--command",
             f"bash {stub} --readonly-x", "--readonly-flag", "--readonly-x",
             "--prompt-file", str(prompt), "--cwd", str(review), "--log-file",
             str(Path(self.temp.name) / "review.log"), "--probe-timeout", "10", "--run-timeout", "10"],
            env={**self.env, "DEV_WORKFLOW_HOST_CLI": "secret-profile-test"},
            stdin=subprocess.DEVNULL, capture_output=True,
        )
        self.assertEqual(0, done.returncode, done.stderr.decode())

    def test_profile_absent_uses_defaults_without_yaml(self):
        values = self.items(self.run_helper(no_site=True))
        self.assertEqual([".env", ".env.*", ".dev.vars"], values)

    def test_empty_tree_is_the_only_non_commit_ref_allowed(self):
        empty = self.git("hash-object", "-t", "tree", "/dev/null").stdout.decode().strip()
        values = self.items(self.run_helper("--ref", empty, no_site=True))
        self.assertEqual([".env", ".env.*", ".dev.vars"], values)

    def test_unresolved_and_non_commit_full_oids_fail_closed(self):
        blob = self.git("hash-object", "-w", "--stdin", cwd=self.repo,).stdout
        # hash-object receives EOF from DEVNULL and creates an empty blob.  It is
        # a syntactically complete OID but must not be accepted as a commit.
        blob_oid = blob.decode().strip()
        missing = "0" * len(blob_oid)
        for ref in (blob_oid, missing):
            with self.subTest(ref=ref):
                done = self.run_helper("--ref", ref)
                self.assertEqual(20, done.returncode)
                self.assertEqual(b"", done.stdout)

    def test_profile_needs_pyyaml_only_when_profile_exists(self):
        path = self.repo / ".claude/project-profile.yml"
        path.parent.mkdir()
        path.write_text("secret_paths: []\n", encoding="utf-8")
        done = self.run_helper(no_site=True)
        self.assertEqual(20, done.returncode)
        self.assertIn("PyYAML", done.stderr.decode())

    def test_invalid_refs_and_profile_type_fail_closed(self):
        oid = self.git("rev-parse", "HEAD").stdout.decode().strip()
        for bad in ("HEAD", oid[:12], oid.upper(), oid + "^{commit}"):
            with self.subTest(ref=bad):
                done = self.run_helper("--ref", bad)
                self.assertEqual(20, done.returncode)
                self.assertEqual(b"", done.stdout)
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        profile.write_text("secret_paths: wrong\n", encoding="utf-8")
        done = self.run_helper()
        self.assertEqual(20, done.returncode)
        self.assertIn("文字列の配列", done.stderr.decode())

    def test_malformed_glob_and_duplicate_yaml_keys_fail_closed(self):
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        cases = (
            r'secret_paths: ["bad\0split"]' + "\n",
            "secret_paths: [\"bad[glob\"]\n",
            "secret_paths:\n  - a\nsecret_paths:\n  - b\n",
        )
        for content in cases:
            with self.subTest(content=content):
                profile.write_text(content, encoding="utf-8")
                done = self.run_helper()
                self.assertEqual(20, done.returncode)
                self.assertEqual(b"", done.stdout)
                self.assertNotIn("bad", done.stderr.decode())
        profile.write_text("secret_paths: [safe.txt]\n", encoding="utf-8")
        self.assertEqual([".env", ".env.*", ".dev.vars", "safe.txt"], self.items(self.run_helper()))

    def test_keyless_empty_invalid_and_merged_yaml_profiles(self):
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        for content in ("quality: {}\n", "secret_paths: []\n"):
            with self.subTest(content=content):
                profile.write_text(content, encoding="utf-8")
                self.assertEqual([".env", ".env.*", ".dev.vars"], self.items(self.run_helper()))
        profile.write_text("secret_paths: [\n", encoding="utf-8")
        done = self.run_helper()
        self.assertEqual(20, done.returncode)
        self.assertEqual(b"", done.stdout)
        profile.write_text(
            "defaults: &defaults\n  quality: {}\nconfig:\n  <<: *defaults\nsecret_paths: []\n",
            encoding="utf-8",
        )
        self.assertEqual([".env", ".env.*", ".dev.vars"], self.items(self.run_helper()))
        profile.write_text(
            "defaults: &defaults\n  secret_paths: [merged.txt]\n<<: *defaults\n",
            encoding="utf-8",
        )
        self.assertEqual(
            [".env", ".env.*", ".dev.vars", "merged.txt"],
            self.items(self.run_helper()),
        )

    def test_explicit_duplicate_secret_paths_still_fails_closed(self):
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        profile.write_text("secret_paths: [first.txt]\nsecret_paths: [second.txt]\n", encoding="utf-8")
        done = self.run_helper()
        self.assertEqual(20, done.returncode)
        self.assertEqual(b"", done.stdout)

        profile.write_text(
            "defaults: &defaults {secret_paths: [private.txt], secret_paths: []}\n<<: *defaults\n",
            encoding="utf-8",
        )
        done = self.run_helper()
        self.assertEqual(20, done.returncode)
        self.assertEqual(b"", done.stdout)

    def test_current_special_files_are_rejected_without_reading_them(self):
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        target = self.repo / "outside-profile"
        target.write_text("secret_paths: [LEAK_PROFILE_CONTENT]\n", encoding="utf-8")
        os.symlink(target, profile)
        done = self.run_helper()
        self.assertEqual(20, done.returncode)
        self.assertNotIn("LEAK_PROFILE_CONTENT", done.stderr.decode())
        profile.unlink()
        profile.parent.rmdir()
        outside_dir = self.repo / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "project-profile.yml").write_text("secret_paths: [LEAK_PROFILE_CONTENT]\n", encoding="utf-8")
        os.symlink(outside_dir, profile.parent)
        done = self.run_helper()
        self.assertEqual(20, done.returncode)
        self.assertNotIn("LEAK_PROFILE_CONTENT", done.stderr.decode())
        profile.parent.unlink()
        profile.parent.mkdir()
        os.mkfifo(profile)
        done = self.run_helper()
        self.assertEqual(20, done.returncode)
        self.assertNotIn("LEAK_PROFILE_CONTENT", done.stderr.decode())
        if os.geteuid() != 0:
            profile.unlink()
            profile.write_text("secret_paths: [LEAK_PROFILE_CONTENT]\n", encoding="utf-8")
            profile.chmod(0)
            self.addCleanup(lambda: profile.chmod(stat.S_IRUSR | stat.S_IWUSR))
            done = self.run_helper()
            self.assertEqual(20, done.returncode)
            self.assertNotIn("LEAK_PROFILE_CONTENT", done.stderr.decode())

    def test_current_socket_and_device_are_rejected_without_opening(self):
        import socket

        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(str(profile))
        done = self.run_helper()
        self.assertEqual(20, done.returncode)
        profile.unlink()
        try:
            os.mknod(profile, stat.S_IFCHR | stat.S_IRUSR | stat.S_IWUSR, os.makedev(1, 3))
        except (AttributeError, OSError, PermissionError):
            self.skipTest("device node を作る権限がない")
        self.addCleanup(lambda: profile.exists() and profile.unlink())
        done = self.run_helper()
        self.assertEqual(20, done.returncode)

    def test_lstat_to_open_replacement_and_read_mutation_fail_closed(self):
        module = load_helper_module()
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        profile.write_text("secret_paths: [before.txt]\n", encoding="utf-8")
        original_open = module.os.open
        changed = False

        def replace_after_lstat(path, flags, *args, **kwargs):
            nonlocal changed
            if path == module.PROFILE_NAME and not changed:
                changed = True
                profile.unlink()
                profile.write_text("secret_paths: [replacement.txt]\n", encoding="utf-8")
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(module.os, "open", side_effect=replace_after_lstat):
            with self.assertRaises(module.ProfileError):
                module.current_profile(self.repo)

        profile.write_text("secret_paths: [before.txt]\n", encoding="utf-8")
        original_read = module.os.read
        changed = False

        def mutate_during_read(fd, amount):
            nonlocal changed
            result = original_read(fd, amount)
            if result and not changed:
                changed = True
                profile.write_text("secret_paths: [changed-file.txt]\n", encoding="utf-8")
            return result

        with mock.patch.object(module.os, "read", side_effect=mutate_during_read):
            with self.assertRaises(module.ProfileError):
                module.current_profile(self.repo)

    def test_snapshot_rejects_incompatible_profile_and_precheck_modes(self):
        oid = self.git("rev-parse", "HEAD").stdout.decode().strip()
        bad_precheck = subprocess.run(
            ["bash", str(SNAPSHOT), "--cwd", str(self.repo), "--precheck", "--print-exclude-ere",
             "--exclude-glob", ".env"],
            env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
        )
        self.assertEqual(2, bad_precheck.returncode)
        bad_print = subprocess.run(
            ["bash", str(SNAPSHOT), "--print-exclude-ere", "--exclude-glob", ".env",
             "--secret-profile-ref", oid],
            env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
        )
        self.assertEqual(2, bad_print.returncode)

    def test_historic_symlink_and_large_profile_fail_closed(self):
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        target = self.repo / "profile-target"
        target.write_text("secret_paths: [LEAK_PROFILE_CONTENT]\n", encoding="utf-8")
        os.symlink("../profile-target", profile)
        ref = self.commit("symlink profile")
        profile.unlink()
        done = self.run_helper("--ref", ref)
        self.assertEqual(20, done.returncode)
        self.assertNotIn("LEAK_PROFILE_CONTENT", done.stderr.decode())
        profile.write_text("secret_paths: [small]\n#" + ("x" * (1024 * 1024)), encoding="utf-8")
        done = self.run_helper()
        self.assertEqual(20, done.returncode)
        self.assertNotIn("small", done.stderr.decode())

    def test_unapproved_tamper_stops_before_profile_reader(self):
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        target = self.repo / "profile-target"
        target.write_text("secret_paths: [LEAK_PROFILE_CONTENT]\n", encoding="utf-8")
        os.symlink("../profile-target", profile)
        base = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("config", "core.excludesFile", str(self.repo / "exclude"))
        done = subprocess.run(["bash", str(SNAPSHOT), "--cwd", str(self.repo), "--base", base,
                               "--out", str(self.repo / "out.md"), "--secret-profile-ref", base],
                              env=self.env, stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(22, done.returncode, done.stderr.decode())
        self.assertNotIn("secret profile の和集合", done.stderr.decode())
        self.assertNotIn("LEAK_PROFILE_CONTENT", done.stderr.decode())



class DiagnosticProfileReuseTest(unittest.TestCase):
    def test_backend_keeps_management_prefix_literal_and_returns_blob_identity(self):
        helper = load_helper_module()
        path = b'pkg[1]*/.claude/project-profile.yml'
        raw = b'secret_paths: [private.txt]\n'
        oid = 'a' * 40
        calls = []
        def backend(*args):
            calls.append(args)
            if args[0] == 'ls-tree': return b'100644 blob ' + oid.encode() + b'\t' + path + b'\0'
            if args[1] == '-s': return str(len(raw)).encode() + b'\n'
            return raw
        self.assertEqual(helper.ref_profile_backend(backend, 'b'*40, path), (oid, raw))
        self.assertEqual(calls[0], ('ls-tree', '-z', 'b'*40, '--', path))
        self.assertEqual(calls[1:], [('cat-file', '-s', oid), ('cat-file', 'blob', oid)])

    def test_backend_missing_profile_does_not_fetch_blob(self):
        helper = load_helper_module(); calls = []
        def backend(*args): calls.append(args); return b''
        self.assertEqual(helper.ref_profile_backend(backend, 'b'*40, b'.claude/project-profile.yml'), (None, None))
        self.assertEqual(len(calls), 1)

    def test_backend_rejects_size_mismatch_and_unsafe_relative_path(self):
        helper = load_helper_module(); path = b'.claude/project-profile.yml'
        def backend(*args):
            if args[0] == 'ls-tree': return b'100644 blob ' + b'a'*40 + b'\t' + path + b'\0'
            if args[1] == '-s': return b'1\n'
            return b'two'
        with self.assertRaises(helper.ProfileError): helper.ref_profile_backend(backend, 'b'*40, path)
        for bad in (b'../profile', b'/profile', b'a//profile', b'a/./profile'):
            with self.subTest(path=bad):
                with self.assertRaises(helper.ProfileError): helper.ref_profile_backend(backend, 'b'*40, bad)

    def test_fd_profile_preserves_ownership_and_reports_metadata(self):
        helper = load_helper_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / '.claude').mkdir()
            profile = root / '.claude/project-profile.yml'; profile.write_bytes(b'secret_paths: []\n')
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                info = []
                self.assertEqual(helper.current_profile_fd(fd, inspected=info.append), profile.read_bytes())
                self.assertEqual(info[0].st_ino, profile.stat().st_ino)
                self.assertEqual(os.fstat(fd).st_ino, root.stat().st_ino)
            finally: os.close(fd)

    def test_fd_profile_short_read_is_not_a_valid_empty_profile(self):
        helper = load_helper_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / '.claude').mkdir()
            (root / '.claude/project-profile.yml').write_bytes(b'secret_paths: [secret]\n')
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with mock.patch.object(helper.os, 'read', return_value=b''):
                    with self.assertRaises(helper.ProfileError): helper.current_profile_fd(fd)
            finally: os.close(fd)

if __name__ == "__main__":
    unittest.main()
