#!/usr/bin/env python3
"""Read the origin remote's URLs and decide which push target can be trusted.

The contract is ship-task/references/discover-mode.md (origin の URL の読み方).
Read-only: this script runs git and `ssh -G` but writes nothing. It never prints
the URLs themselves, because they may carry credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

SSH_TIMEOUT = 10
PART = r"[A-Za-z0-9._-]+"
# 読む形は 3 つだけ。ポートを明示した形は読まない(HOST に `:` を含めない)
HTTPS = re.compile(rf"^https://(?:[^@/]+@)?(?P<host>[A-Za-z0-9.-]+)/(?P<owner>{PART})/(?P<repo>{PART})/?$")
SSH = re.compile(rf"^ssh://(?:(?P<user>[^@/:]+)@)?(?P<host>[A-Za-z0-9.-]+)/(?P<owner>{PART})/(?P<repo>{PART})/?$")
SCP = re.compile(rf"^(?:(?P<user>[^@/:]+)@)?(?P<host>[A-Za-z0-9.-]+):(?P<owner>{PART})/(?P<repo>{PART})$")


class GitFailed(Exception):
    pass


def git(directory: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", directory, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise GitFailed(f"git を起動できない: {exc}") from exc


def lines(directory: str, *args: str) -> list[str]:
    done = git(directory, *args)
    if done.returncode != 0:
        raise GitFailed(f"git {args[0]} が失敗した(終了コード {done.returncode})")
    return [line for line in done.stdout.splitlines() if line]


def config_value(directory: str, *args: str) -> str:
    # 設定が無いと rc 1(値なし)。それ以外の 0 でない終了は git の失敗
    done = git(directory, "config", *args)
    if done.returncode == 1:
        return ""
    if done.returncode != 0:
        raise GitFailed(f"git config が失敗した(終了コード {done.returncode})")
    return done.stdout.strip()


def strip_git(repo: str) -> str:
    return repo[:-4] if repo.endswith(".git") else repo


def parse(url: str) -> dict | None:
    """字面から (形, ユーザー, HOST, OWNER, REPO) を読む。ssh の設定は解かない"""
    for form, pattern in (("https", HTTPS), ("ssh", SSH), ("scp", SCP)):
        found = pattern.match(url)
        if not found:
            continue
        repo = strip_git(found.group("repo"))
        if not repo or repo in (".", ".."):
            return None
        return {
            "form": form,
            "user": found.groupdict().get("user"),
            "host": found.group("host").lower(),
            "owner": found.group("owner"),
            "repo": repo,
        }
    return None


def value(parsed: dict) -> str:
    return f"{parsed['host']}/{parsed['owner']}/{parsed['repo']}"


def comparable(parsed: dict) -> str:
    # GitHub は OWNER・REPO の大小を区別しない。ほかのホストは字面のまま(安全側)
    if parsed["host"] == "github.com":
        return value(parsed).lower()
    return value(parsed)


def ssh_is_github() -> tuple[bool, str]:
    """`ssh -G -- git@github.com` の hostname と port を見る。接続はしない"""
    try:
        done = subprocess.run(
            ["ssh", "-G", "--", "git@github.com"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "ssh -G が時間切れ"
    except OSError:
        return False, "ssh -G を起動できない"
    if done.returncode != 0:
        return False, f"ssh -G が失敗した(終了コード {done.returncode})"
    settings = {}
    for line in done.stdout.splitlines():
        key, _, rest = line.partition(" ")
        settings.setdefault(key.lower(), rest.strip())
    if settings.get("hostname") != "github.com":
        return False, "ssh の設定で github.com の hostname が github.com でない"
    if settings.get("port") != "22":
        return False, "ssh の設定で github.com の port が 22 でない"
    return True, ""


def ssh_overridden(directory: str) -> bool:
    if os.environ.get("GIT_SSH_COMMAND") or os.environ.get("GIT_SSH"):
        return True
    return bool(config_value(directory, "--get", "core.sshCommand"))


def tls_verify_off(directory: str, url: str) -> bool:
    # git は値が空でも、変数があれば検証を外す
    if "GIT_SSL_NO_VERIFY" in os.environ:
        return True
    # 設定が無い(rc 1)なら検証あり
    return config_value(directory, "--type=bool", "--get-urlmatch", "http.sslverify", url) == "false"


def trusted_repo(directory: str, push: dict | None, push_url: str) -> tuple[str | None, str]:
    """公開の確認と PR に使える push 先(許す形の列挙)。ほかは None"""
    if push is None:
        return None, "push の URL が読める形でない"
    if push["form"] == "https":
        if tls_verify_off(directory, push_url):
            return None, "TLS の検証を外している"
        return value(push), ""
    if push["user"] != "git" or push["host"] != "github.com":
        return None, "ssh の push 先が git@github.com でない"
    if ssh_overridden(directory):
        return None, "ssh を上書きする設定がある"
    ok, why = ssh_is_github()
    if not ok:
        return None, why
    return value(push), ""


def judge(directory: str) -> dict:
    result = {"origin": False, "same": False, "repo": None, "form": None, "vcs": False, "reason": ""}
    if "origin" not in lines(directory, "remote"):
        result["reason"] = "origin が無い"
        return result
    result["origin"] = True
    if config_value(directory, "--get", "remote.origin.vcs"):
        # push・ls-remote が git-remote-<vcs> のヘルパーを通り、字面と送り先が離れる
        result["vcs"] = True
        result["reason"] = "remote.origin.vcs がある"
        return result
    fetch_urls = lines(directory, "remote", "get-url", "--all", "origin")
    push_urls = lines(directory, "remote", "get-url", "--push", "--all", "origin")
    if len(fetch_urls) != 1 or len(push_urls) != 1:
        result["reason"] = "fetch か push の URL が 1 つでない"
        return result
    fetch_url, push_url = fetch_urls[0], push_urls[0]
    fetch, push = parse(fetch_url), parse(push_url)
    result["form"] = push["form"] if push else "other"
    if fetch and push:
        result["same"] = comparable(fetch) == comparable(push)
    else:
        result["same"] = fetch_url == push_url
    if not result["same"]:
        result["reason"] = "fetch と push が別のリポジトリ"
        return result
    result["repo"], result["reason"] = trusted_repo(directory, push, push_url)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--dir", required=True)
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        return 2 if exc.code else 0
    if not os.path.isdir(args.dir):
        print("--dir がディレクトリでない", file=sys.stderr)
        return 2
    try:
        result = judge(args.dir)
    except GitFailed as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
