"""Regression tests for the unattended profile-union handoff."""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "plugins/dev-workflow/skills/do-task/scripts/diff-snapshot.sh"
HELPER = ROOT / "plugins/dev-workflow/skills/do-task/scripts/secret-profiles.py"


class ProfileUnionHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="profile-union-")
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        profile = self.repo / ".claude/project-profile.yml"
        profile.parent.mkdir()
        profile.write_text("secret_paths: [old-private.txt]\n", encoding="utf-8")
        (self.repo / "old-private.txt").write_text("old secret\n", encoding="utf-8")
        (self.repo / "public.txt").write_text("public\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def initial_union(self) -> tuple[list[str], str]:
        head = self.git("rev-parse", "HEAD").stdout.decode().strip()
        done = subprocess.run(["python3", str(HELPER), "--cwd", str(self.repo), "--ref", head],
                              check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return [item.decode() for item in done.stdout.split(b"\0") if item], hashlib.sha256(done.stdout).hexdigest()

    def snapshot(self, globs: list[str], digest: str, output: pathlib.Path) -> subprocess.CompletedProcess[bytes]:
        head = self.git("rev-parse", "HEAD").stdout.decode().strip()
        args = ["bash", str(SNAPSHOT), "--cwd", str(self.repo), "--base", "HEAD", "--out", str(output),
                "--secret-profile-ref", head, "--secret-profile-union-sha256", digest]
        for glob in globs:
            args.extend(["--exclude-glob", glob])
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_stable_union_is_accepted_and_changed_current_profile_stops(self) -> None:
        globs, digest = self.initial_union()
        normal = self.snapshot(globs, digest, pathlib.Path(self.tmp.name) / "normal.md")
        self.assertEqual(0, normal.returncode, normal.stderr.decode())

        (self.repo / ".claude/project-profile.yml").write_text(
            "secret_paths: [old-private.txt, private.txt]\n", encoding="utf-8")
        (self.repo / "private.txt").write_text("new secret\n", encoding="utf-8")
        attacked_output = pathlib.Path(self.tmp.name) / "attack.md"
        attacked = self.snapshot(globs, digest, attacked_output)
        self.assertNotEqual(0, attacked.returncode)
        self.assertNotIn(b"new secret", attacked.stdout + attacked.stderr)
        self.assertNotIn(b"new secret", attacked_output.read_bytes() if attacked_output.exists() else b"")

    def test_digest_option_rejects_empty_invalid_and_duplicate_values(self) -> None:
        globs, digest = self.initial_union()
        head = self.git("rev-parse", "HEAD").stdout.decode().strip()
        for supplied in ("", "UPPER", "0" * 63):
            args = ["bash", str(SNAPSHOT), "--cwd", str(self.repo), "--base", "HEAD",
                    "--out", str(pathlib.Path(self.tmp.name) / "bad.md"),
                    "--secret-profile-ref", head, "--secret-profile-union-sha256", supplied]
            for glob in globs:
                args.extend(["--exclude-glob", glob])
            done = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(2, done.returncode, done.stderr.decode())
        args = ["bash", str(SNAPSHOT), "--cwd", str(self.repo), "--base", "HEAD",
                "--out", str(pathlib.Path(self.tmp.name) / "duplicate.md"),
                "--secret-profile-ref", head, "--secret-profile-union-sha256", digest,
                "--secret-profile-union-sha256", ""]
        for glob in globs:
            args.extend(["--exclude-glob", glob])
        done = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(2, done.returncode, done.stderr.decode())


if __name__ == "__main__":
    unittest.main()
