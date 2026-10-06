import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "plugins/dev-workflow/skills/ship-task/scripts/origin-repo.py"

# ssh -G のスタブ。引数(ユーザー名つきかどうか)で値を変え、Match user の構成を真似る
SSH_STUB = """#!/bin/sh
mode="${SSH_STUB_MODE:-github}"
case "$mode" in
  fail) exit 255 ;;
  sleep) sleep 5; exit 0 ;;
esac
if [ -n "${SSH_STUB_OUTPUT:-}" ]; then
  cat "$SSH_STUB_OUTPUT"
  exit 0
fi
user_given=no
for a in "$@"; do case "$a" in *@*) user_given=yes ;; esac; done
host=github.com
port=22
case "$mode" in
  matchuser) [ "$user_given" = yes ] && port=2222 ;;
  otherhost) host=gh.example.com ;;
  port443) host=ssh.github.com; port=443 ;;
esac
printf 'user git\\nhostname %s\\nport %s\\nstricthostkeychecking ask\\nuserknownhostsfile ~/.ssh/known_hosts\\nglobalknownhostsfile /etc/ssh/ssh_known_hosts\\nnohostauthenticationforlocalhost no\\n' "$host" "$port"
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

    def ssh_output(self, settings, *, form="scp"):
        """ssh -G の出力を隔離したスタブから渡す。"""
        output = Path(self.temp.name) / "ssh-output"
        output.write_text(settings, encoding="utf-8")
        url = "git@github.com:o/r.git" if form == "scp" else "ssh://git@github.com/o/r.git"
        self.origin(url)
        return self.result(extra_env={"SSH_STUB_OUTPUT": str(output)})

    def standard_settings(self, **changes):
        settings = {
            "hostname": "github.com", "port": "22",
            "stricthostkeychecking": "ask",
            "userknownhostsfile": "~/.ssh/known_hosts",
            "globalknownhostsfile": "/etc/ssh/ssh_known_hosts",
            "nohostauthenticationforlocalhost": "no",
        }
        settings.update(changes)
        return "".join(f"{key} {value}\n" for key, value in settings.items())

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
        url = "https://x-access-token:SECRETTOKEN@github.com/Owner/Repo.git/"
        self.origin(url)
        got, raw = self.result()
        self.assertTrue(got["same"])
        self.assertEqual("github.com/Owner/Repo", got["repo"])
        self.assertEqual("https", got["form"])
        self.assertEqual("sha256:" + hashlib.sha256(url.encode()).hexdigest(), got["push_url_sha256"])
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

    def test_ssh_strict_modes_and_url_forms(self):
        for form in ("scp", "ssh"):
            for strict, allowed in (
                ("no", False), ("off", False), ("false", False),
                ("yes", True), ("true", True), ("ask", True), ("accept-new", True),
                ("unknown", False),
            ):
                with self.subTest(form=form, strict=strict):
                    got, _ = self.ssh_output(
                        self.standard_settings(stricthostkeychecking=strict), form=form
                    )
                    self.assertEqual("github.com/o/r" if allowed else None, got["repo"])
                    if not allowed:
                        self.assertTrue(got["reason"])

    def test_ssh_accept_new_first_user_file_and_none(self):
        for first in ("/dev/null", "/dev//null", "/dev/./null",
                      "/dev/x/../null", "//dev/null"):
            for global_file in ("none", "/etc/ssh/ssh_known_hosts"):
                with self.subTest(first=first, global_file=global_file):
                    settings = self.standard_settings(
                        stricthostkeychecking="accept-new",
                        userknownhostsfile=f"{first} /safe/second",
                        globalknownhostsfile=global_file,
                    )
                    got, _ = self.ssh_output(settings)
                    self.assertIsNone(got["repo"])
                    self.assertTrue(got["reason"])
        for user_file in ("none", "/safe/first /dev/null"):
            with self.subTest(user_file=user_file):
                got, _ = self.ssh_output(self.standard_settings(
                    stricthostkeychecking="accept-new",
                    userknownhostsfile=user_file,
                ))
                self.assertEqual("github.com/o/r", got["repo"])
        got, _ = self.ssh_output(self.standard_settings(
            stricthostkeychecking="accept-new", userknownhostsfile="none /safe/second"
        ))
        self.assertIsNone(got["repo"])
        for strict in ("yes", "true", "ask"):
            with self.subTest(strict=strict):
                got, _ = self.ssh_output(self.standard_settings(
                    stricthostkeychecking=strict,
                    userknownhostsfile="/dev/null",
                    globalknownhostsfile="none",
                ))
                self.assertEqual("github.com/o/r", got["repo"])

    def test_ssh_hostkeyalias_and_localhost_setting(self):
        for alias, allowed in (("", True), ("github.com", True),
                               ("none", False), ("other.example", False)):
            with self.subTest(alias=alias):
                settings = self.standard_settings()
                if alias:
                    settings += f"hostkeyalias {alias}\n"
                got, _ = self.ssh_output(settings)
                self.assertEqual("github.com/o/r" if allowed else None, got["repo"])
        for local, allowed in (("no", True), ("false", True),
                               ("yes", False), ("true", False), ("other", False)):
            with self.subTest(local=local):
                got, _ = self.ssh_output(self.standard_settings(
                    nohostauthenticationforlocalhost=local
                ))
                self.assertEqual("github.com/o/r" if allowed else None, got["repo"])

    def test_ssh_required_settings_are_present_unique_and_nonempty(self):
        for key in ("hostname", "port", "stricthostkeychecking",
                    "userknownhostsfile", "globalknownhostsfile",
                    "nohostauthenticationforlocalhost"):
            lines = self.standard_settings().splitlines()
            for mode in ("missing", "empty", "duplicate"):
                with self.subTest(key=key, mode=mode):
                    selected = [line for line in lines if not line.startswith(f"{key} ")]
                    if mode == "empty":
                        selected.append(f"{key}\t  ")
                    elif mode == "duplicate":
                        original = next(line for line in lines if line.startswith(f"{key} "))
                        selected.extend((original, original))
                    got, _ = self.ssh_output("\n".join(selected) + "\n")
                    self.assertIsNone(got["repo"])
                    self.assertTrue(got["reason"])
        got, _ = self.ssh_output(self.standard_settings() +
                                 "hostkeyalias github.com\nhostkeyalias github.com\n")
        self.assertIsNone(got["repo"])
        got, _ = self.ssh_output(self.standard_settings() + "hostkeyalias\t \n")
        self.assertIsNone(got["repo"])

    def test_ssh_keys_are_case_insensitive_and_whitespace_separated(self):
        settings = self.standard_settings()
        settings = settings.replace("hostname github.com", "  HostName\tgithub.com")
        settings = settings.replace("port 22", "PORT\t22")
        settings = settings.replace("stricthostkeychecking ask", "StrictHostKeyChecking\task")
        got, _ = self.ssh_output(settings + "identityfile a\nidentityfile b\n")
        self.assertEqual("github.com/o/r", got["repo"])
        got, _ = self.ssh_output(settings + "HOSTNAME github.com\n")
        self.assertIsNone(got["repo"])

    def test_ssh_unrelated_repeatable_settings_do_not_rescue_or_reject(self):
        extras = (
            "identityfile /tmp/key1\nidentityfile /tmp/key2\n"
            "proxycommand secret-proxy-command\nproxyjump jump.example\n"
            "knownhostscommand secret-known-hosts-command\nverifyhostkeydns yes\n"
        )
        for strict, allowed in (("ask", True), ("no", False)):
            with self.subTest(strict=strict):
                got, raw = self.ssh_output(
                    self.standard_settings(stricthostkeychecking=strict) + extras
                )
                self.assertEqual("github.com/o/r" if allowed else None, got["repo"])
                self.assertNotIn("secret-", raw)
                self.assertNotIn("secret-", self.run_script(
                    extra_env={"SSH_STUB_OUTPUT": str(Path(self.temp.name) / "ssh-output")}
                ).stderr)

    def test_ssh_decode_failure_is_rejected_without_leaking(self):
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("origin_repo", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module.subprocess, "run", side_effect=UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid")):
            ok, why = module.ssh_is_github()
        self.assertFalse(ok)
        self.assertTrue(why)
        self.assertNotIn("xff", why)

    def test_ssh_launch_failure_has_safe_reason(self):
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("origin_repo", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module.subprocess, "run", side_effect=OSError("SECRET-PATH")):
            ok, why = module.ssh_is_github()
        self.assertFalse(ok)
        self.assertTrue(why)
        self.assertNotIn("SECRET-PATH", why)

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH が無い")
    def test_real_ssh_g_with_isolated_config(self):
        config = Path(self.temp.name) / "ssh-config"
        cases = (
            ("unsafe", "StrictHostKeyChecking no\n"
                       "UserKnownHostsFile /dev/null /safe/second\n"
                       "GlobalKnownHostsFile /safe/global\n"
                       "ProxyCommand /bin/false\n", False),
            ("safe", "StrictHostKeyChecking accept-new\n"
                     "UserKnownHostsFile /safe/first /dev/null\n"
                     "GlobalKnownHostsFile /safe/global\n"
                     "ProxyJump jump.example\n", True),
            ("first-null", "StrictHostKeyChecking accept-new\n"
                           "UserKnownHostsFile /dev/null /safe/second\n"
                           "GlobalKnownHostsFile /safe/global\n", False),
            ("none", "StrictHostKeyChecking accept-new\n"
                     "UserKnownHostsFile none\n"
                     "GlobalKnownHostsFile none\n", True),
            ("yes", "StrictHostKeyChecking true\n"
                    "UserKnownHostsFile /dev/null\n"
                    "GlobalKnownHostsFile none\n", True),
            ("ask", "StrictHostKeyChecking ask\n"
                    "UserKnownHostsFile /dev/null\n"
                    "GlobalKnownHostsFile none\n", True),
        )
        for name, body, allowed in cases:
            with self.subTest(name=name):
                config.write_text("Host github.com\n"
                                  "  HostName github.com\n"
                                  "  Port 22\n"
                                  "  NoHostAuthenticationForLocalhost no\n"
                                  + body, encoding="utf-8")
                done = subprocess.run(
                    [shutil.which("ssh"), "-G", "-F", str(config), "--", "git@github.com"],
                    env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                    text=True, check=False,
                )
                self.assertEqual(0, done.returncode, done.stderr)
                got, _ = self.ssh_output(done.stdout)
                self.assertEqual("github.com/o/r" if allowed else None, got["repo"])

    def test_https_ignores_ssh_settings(self):
        self.origin("https://github.com/o/r.git")
        got, _ = self.result(extra_env={"SSH_STUB_MODE": "fail"})
        self.assertEqual("github.com/o/r", got["repo"])

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
