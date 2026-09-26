import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "plugins/dev-workflow/skills/ship-task/scripts/origin-repo.py"

# ssh -G のスタブ。引数(ユーザー名つきかどうか)で値を変え、Match user の構成を真似る
SSH_STUB = """#!/bin/sh
mode="${SSH_STUB_MODE:-github}"
case "$mode" in
  fail) exit 255 ;;
  sleep) sleep 5; exit 0 ;;
esac
user_given=no
for a in "$@"; do case "$a" in *@*) user_given=yes ;; esac; done
host=github.com
port=22
case "$mode" in
  matchuser) [ "$user_given" = yes ] && port=2222 ;;
  otherhost) host=gh.example.com ;;
  port443) host=ssh.github.com; port=443 ;;
esac
printf 'user git\\nhostname %s\\nport %s\\n' "$host" "$port"
"""


class OriginRepoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.repo = base / "repo"
        self.bin = base / "bin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text(SSH_STUB, encoding="utf-8")
        ssh.chmod(0o755)
        (base / "gitconfig").write_text("", encoding="utf-8")
        self.env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(base),
            "GIT_CONFIG_GLOBAL": str(base / "gitconfig"),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        self.git("init", "-q", str(self.repo), cwd=base)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                       capture_output=True, text=True)

    def origin(self, fetch, push=None):
        # 何度呼んでもよい(前の origin を消してから足す)
        subprocess.run(["git", "remote", "remove", "origin"], cwd=self.repo, env=self.env,
                       capture_output=True, check=False)
        self.git("remote", "add", "origin", fetch)
        if push is not None:
            self.git("config", "remote.origin.pushurl", push)

    def run_script(self, *, directory=None, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        done = subprocess.run([sys.executable, str(SCRIPT), f"--dir={directory or self.repo}"],
                              env=env, capture_output=True, text=True, check=False)
        return done

    def result(self, **kwargs):
        done = self.run_script(**kwargs)
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout), done.stdout

    # ── origin の有無・URL の数 ──
    def test_no_origin(self):
        got, _ = self.result()
        self.assertFalse(got["origin"])
        self.assertFalse(got["same"])
        self.assertIsNone(got["repo"])

    def test_two_push_urls_are_not_same(self):
        self.origin("https://github.com/o/r.git")
        self.git("remote", "set-url", "--add", "--push", "origin", "https://github.com/o/r.git")
        self.git("remote", "set-url", "--add", "--push", "origin", "https://github.com/o/r2.git")
        got, _ = self.result()
        self.assertTrue(got["origin"])
        self.assertFalse(got["same"])
        self.assertIsNone(got["repo"])

    # ── 字面の読み方 ──
    def test_https_drops_userinfo_and_keeps_case(self):
        self.origin("https://x-access-token:SECRETTOKEN@github.com/Owner/Repo.git/")
        got, raw = self.result()
        self.assertTrue(got["same"])
        self.assertEqual("github.com/Owner/Repo", got["repo"])
        self.assertEqual("https", got["form"])
        self.assertNotIn("SECRETTOKEN", raw)
        self.assertNotIn("x-access-token", raw)

    def test_scp_and_ssh_forms_give_the_same_repo(self):
        for url in ("git@github.com:o/r.git", "ssh://git@github.com/o/r"):
            with self.subTest(url=url):
                self.origin(url)
                got, _ = self.result()
                self.assertTrue(got["same"])
                self.assertEqual("github.com/o/r", got["repo"])

    def test_explicit_port_is_not_read(self):
        for url in ("https://github.com:443/o/r.git", "ssh://git@github.com:22/o/r.git"):
            with self.subTest(url=url):
                self.origin(url)
                got, _ = self.result()
                self.assertEqual("other", got["form"])
                self.assertTrue(got["same"])  # 読めないときは字面の一致
                self.assertIsNone(got["repo"])

    def test_nested_owner_is_not_read(self):
        self.origin("https://gitlab.com/group/sub/repo.git")
        got, _ = self.result()
        self.assertEqual("other", got["form"])
        self.assertIsNone(got["repo"])

    def test_local_path_same_and_different(self):
        self.origin("/srv/git/r.git")
        got, _ = self.result()
        self.assertTrue(got["same"])
        self.assertIsNone(got["repo"])
        self.git("config", "remote.origin.pushurl", "/srv/git/other.git")
        got, _ = self.result()
        self.assertFalse(got["same"])
        self.assertIsNone(got["repo"])

    # ── same ──
    def test_https_fetch_and_scp_push_are_same(self):
        self.origin("https://github.com/o/r.git", "git@github.com:o/r.git")
        got, _ = self.result(extra_env={"SSH_STUB_MODE": "port443"})
        self.assertTrue(got["same"])  # ssh の設定を解かない
        self.assertEqual("scp", got["form"])
        self.assertIsNone(got["repo"])  # 443 の設定は許す形でない

    def test_different_repos_are_not_same(self):
        self.origin("https://github.com/o/r.git", "https://github.com/o/other.git")
        got, _ = self.result()
        self.assertFalse(got["same"])
        self.assertIsNone(got["repo"])

    def test_case_is_ignored_only_for_github(self):
        self.origin("https://github.com/O/R.git", "git@github.com:o/r.git")
        got, _ = self.result()
        self.assertTrue(got["same"])
        self.assertEqual("github.com/o/r", got["repo"])
        self.git("remote", "set-url", "origin", "https://gitlab.com/O/R.git")
        self.git("config", "remote.origin.pushurl", "https://gitlab.com/o/r.git")
        got, _ = self.result()
        self.assertFalse(got["same"])

    # ── repo の許す形 ──
    def test_ssh_rejected_when_match_user_changes_port(self):
        self.origin("git@github.com:o/r.git")
        got, _ = self.result(extra_env={"SSH_STUB_MODE": "matchuser"})
        self.assertTrue(got["same"])
        self.assertIsNone(got["repo"])

    def test_ssh_rejected_when_hostname_is_not_github(self):
        self.origin("git@github.com:o/r.git")
        got, _ = self.result(extra_env={"SSH_STUB_MODE": "otherhost"})
        self.assertIsNone(got["repo"])

    def test_ssh_alias_host_and_other_user_are_rejected(self):
        for url in ("git@github-work:o/r.git", "me@github.com:o/r.git"):
            with self.subTest(url=url):
                self.origin(url)
                got, _ = self.result()
                self.assertTrue(got["same"])
                self.assertIsNone(got["repo"])

    def test_ssh_overrides_are_rejected(self):
        self.origin("git@github.com:o/r.git")
        for key in ("GIT_SSH_COMMAND", "GIT_SSH"):
            with self.subTest(env=key):
                got, _ = self.result(extra_env={key: "ssh -p 2222"})
                self.assertIsNone(got["repo"])
        self.git("config", "core.sshCommand", "ssh -p 2222")
        got, _ = self.result()
        self.assertIsNone(got["repo"])

    def test_ssh_failure_is_rejected(self):
        self.origin("git@github.com:o/r.git")
        got, _ = self.result(extra_env={"SSH_STUB_MODE": "fail"})
        self.assertIsNone(got["repo"])

    def test_ssh_timeout_is_rejected(self):
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("origin_repo", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        setattr(module, "SSH_TIMEOUT", 1)
        saved = dict(os.environ)
        try:
            os.environ["PATH"] = self.env["PATH"]
            os.environ["SSH_STUB_MODE"] = "sleep"
            started = time.monotonic()
            ok, why = module.ssh_is_github()
            self.assertLess(time.monotonic() - started, 4)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertFalse(ok)
        self.assertIn("時間切れ", why)

    # ── どの形でも repo を null にする設定 ──
    def test_vcs_helper(self):
        self.origin("https://github.com/o/r.git")
        self.git("config", "remote.origin.vcs", "evil")
        got, _ = self.result()
        self.assertTrue(got["vcs"])
        self.assertFalse(got["same"])
        self.assertIsNone(got["repo"])

    def test_tls_verification_off(self):
        self.origin("https://github.com/o/r.git")
        got, _ = self.result()
        self.assertEqual("github.com/o/r", got["repo"])  # 設定が無い = 検証あり
        for value in ("1", ""):  # git は値が空でも検証を外す
            with self.subTest(value=value):
                got, _ = self.result(extra_env={"GIT_SSL_NO_VERIFY": value})
                self.assertIsNone(got["repo"])
        self.git("config", "http.https://github.com/.sslVerify", "false")
        got, _ = self.result()
        self.assertIsNone(got["repo"])

    # ── 使い方の誤り ──
    def test_usage_errors(self):
        done = self.run_script(directory=str(Path(self.temp.name) / "missing"))
        self.assertEqual(2, done.returncode)
        plain = Path(self.temp.name) / "plain"
        plain.mkdir()
        done = self.run_script(directory=str(plain))
        self.assertEqual(2, done.returncode)
        done = subprocess.run([sys.executable, str(SCRIPT)], env=self.env,
                              capture_output=True, text=True, check=False)
        self.assertEqual(2, done.returncode)


if __name__ == "__main__":
    unittest.main()
