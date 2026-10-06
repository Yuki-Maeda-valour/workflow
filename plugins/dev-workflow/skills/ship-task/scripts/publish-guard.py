#!/usr/bin/env python3
"""Publish one reviewed commit to one checked repository and verify its PR.

The caller keeps the reviewed object ID outside the child worktree.  This helper
does not discover a branch tip: it sends that exact object ID after rechecking
the origin decision and the local Git-config digest.  URLs and Git-config
values are intentionally never copied to diagnostics.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import urlparse


GIT_TIMEOUT = 30
GH_TIMEOUT = 30
BODY_LIMIT = 1024 * 1024
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
REPO = re.compile(r"[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")

# Every Git process started here receives these before its subcommand.  In
# particular, no repository hook, fsmonitor, GPG signer, LFS filter, pager or
# lazy fetch may turn an observation or publish into arbitrary local code.
SAFE_GIT_PREFIX = (
    "--no-pager", "--no-replace-objects",
    "-c", "core.quotePath=false",
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.ignoreCase=false",
    "-c", "core.splitIndex=false",
    "-c", "core.ignoreStat=false",
    "-c", "commit.gpgSign=false",
    "-c", "push.gpgSign=false",
    "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.clean=",
    "-c", "filter.lfs.process=",
    "-c", "filter.lfs.required=false",
)


class Failed(RuntimeError):
    pass


def run(argv: list[str], *, timeout: int, input_data: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, input=input_data, stdin=subprocess.DEVNULL if input_data is None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Failed("外部コマンドが時間切れ") from exc
    except OSError as exc:
        raise Failed("外部コマンドを起動できない") from exc


def git(directory: str, *args: str) -> str:
    done = run(["git", "-C", directory, *SAFE_GIT_PREFIX, *args], timeout=GIT_TIMEOUT)
    if done.returncode:
        # stderr can contain an authenticated remote URL; do not relay it.
        raise Failed("Git の公開前後の照合または送信が失敗した")
    return done.stdout


def tool(directory: str, script: str, *args: str) -> str:
    done = run([sys.executable, "-B", str(Path(__file__).with_name(script)), "--dir", directory, *args],
               timeout=GIT_TIMEOUT)
    if done.returncode:
        raise Failed("公開前のローカル照合が失敗した")
    return done.stdout


def valid_ref_name(name: str) -> bool:
    return (bool(name) and not name.startswith("/") and not name.endswith(("/", "."))
            and ".." not in name and "@{" not in name
            and not any(ord(char) < 32 or ord(char) == 127 or char in " ~^:?*[\\" for char in name))


def repo_parts(repo: str) -> tuple[str, str, str]:
    if not REPO.fullmatch(repo):
        raise Failed("固定リポジトリ名の形でない")
    return tuple(repo.split("/", 2))  # type: ignore[return-value]


def local_checked_sha(directory: str, branch: str, held_sha: str) -> None:
    if not OID.fullmatch(held_sha):
        raise Failed("レビュー済み SHA が完全な object ID でない")
    if not valid_ref_name(branch):
        raise Failed("固定ブランチ名の形でない")
    actual = git(directory, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").strip()
    if actual != held_sha:
        raise Failed("固定ブランチがレビュー済み SHA と一致しない")
    git(directory, "cat-file", "-e", f"{held_sha}^{{commit}}")


def origin_checked(directory: str, repo: str) -> None:
    try:
        data = json.loads(tool(directory, "origin-repo.py"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise Failed("origin の判定を読めない") from exc
    if not (data.get("origin") is True and data.get("same") is True and data.get("vcs") is False
            and data.get("repo") == repo):
        raise Failed("origin の送信先が固定リポジトリと一致しない")


def config_checked(directory: str, expected: str) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise Failed("固定設定ダイジェストの形でない")
    tool(directory, "git-config-digest.py", "--expect", expected)


def remote_checked(directory: str, branch: str, held_sha: str) -> None:
    ref = f"refs/heads/{branch}"
    lines = [line for line in git(directory, "ls-remote", "origin", ref).splitlines() if line]
    if len(lines) != 1:
        raise Failed("送信後の remote branch を一意に確認できない")
    fields = lines[0].split("\t")
    if len(fields) != 2 or fields[0] != held_sha or fields[1] != ref:
        raise Failed("送信後の remote branch がレビュー済み SHA と一致しない")


def set_tracking(directory: str, branch: str) -> None:
    """The interactive equivalent of -u, after the exact-SHA push succeeded."""
    if os.environ.get("DEV_WORKFLOW_LOOP_ITER"):
        raise Failed("無人の周では追跡設定を変更できない")
    # `push -u origin <branch>` records these same two local values.  The
    # branch was already syntactically checked, and no remote URL is accepted.
    git(directory, "config", "--local", "--replace-all", f"branch.{branch}.remote", "origin")
    git(directory, "config", "--local", "--replace-all", f"branch.{branch}.merge", f"refs/heads/{branch}")


def read_body(directory: str, path: str) -> str:
    """Read one bounded reviews file through nofollow directory descriptors."""
    parts = path.split("/")
    if os.path.isabs(path) or len(parts) < 3 or parts[:2] != [".claude", "reviews"] or any(
            part in {"", ".", ".."} for part in parts):
        raise Failed("PR 本文は worktree の .claude/reviews/ 下の通常ファイルでない")
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(directory, flags)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            leaf = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
        except BaseException:
            os.close(fd)
            raise
    except OSError as exc:
        raise Failed("PR 本文を読めない") from exc
    try:
        found = os.fstat(leaf)
        if not stat.S_ISREG(found.st_mode) or found.st_size > BODY_LIMIT:
            raise Failed("PR 本文が通常ファイルでない")
        chunks = []
        remaining = BODY_LIMIT + 1
        while remaining:
            piece = os.read(leaf, min(65536, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        raw = b"".join(chunks)
        after = os.fstat(leaf)
    finally:
        os.close(leaf)
        os.close(fd)
    if len(raw) > BODY_LIMIT or after.st_size != found.st_size or after.st_mtime_ns != found.st_mtime_ns or after.st_ctime_ns != found.st_ctime_ns:
        raise Failed("PR 本文が上限を超える")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Failed("PR 本文が UTF-8 でない") from exc


def exact_pr_url(text: str, repo: str) -> tuple[str, int]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise Failed("PR 作成結果が URL 1 件でない")
    url = lines[0]
    parsed = urlparse(url)
    host, owner, name = repo_parts(repo)
    pieces = parsed.path.split("/")
    if (parsed.scheme != "https" or parsed.netloc != host or parsed.params or parsed.query or parsed.fragment
            or pieces[:4] != ["", owner, name, "pull"] or len(pieces) != 5
            or not pieces[4].isdigit() or int(pieces[4]) <= 0):
        raise Failed("PR 作成結果が固定リポジトリの URL 1 件でない")
    return url, int(pieces[4])


def created_pr_checked(directory: str, gh: str, repo: str, branch: str, base: str, held_sha: str, body: str, title: str) -> str:
    body_text = read_body(directory, body)
    create = run([gh, "pr", "create", "-R", repo, "--base", base, "--head", branch,
                  "--title", title, "--body-file", "-"], timeout=GH_TIMEOUT, input_data=body_text)
    if create.returncode:
        raise Failed("PR を作成できない")
    url, number = exact_pr_url(create.stdout, repo)
    host, owner, name = repo_parts(repo)
    view = run([gh, "api", "--hostname", host, f"repos/{owner}/{name}/pulls/{number}"], timeout=GH_TIMEOUT)
    if view.returncode:
        raise Failed(f"作成済み PR {url} を照合できない")
    try:
        data = json.loads(view.stdout)
    except json.JSONDecodeError as exc:
        raise Failed(f"作成済み PR {url} の照合結果を読めない") from exc
    expected_name = f"{owner}/{name}"
    head = data.get("head") or {}
    base_value = data.get("base") or {}
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base_value.get("repo") if isinstance(base_value, dict) else None
    head_owner = head_repo.get("owner") if isinstance(head_repo, dict) else None
    if not (
        data.get("html_url") == url and data.get("number") == number
        and isinstance(head, dict) and head.get("ref") == branch and head.get("sha") == held_sha
        and isinstance(head_repo, dict) and head_repo.get("full_name") == expected_name
        and isinstance(head_owner, dict) and head_owner.get("login") == owner
        and isinstance(base_value, dict) and base_value.get("ref") == base
        and isinstance(base_repo, dict) and base_repo.get("full_name") == expected_name
    ):
        # The PR may be useful evidence for a human; never close a mismatched one.
        raise Failed(f"作成済み PR {url} が固定した repository/head/base/SHA と一致しない")
    return url


def publish(args: argparse.Namespace) -> int:
    if not valid_ref_name(args.base):
        raise Failed("base ブランチ名の形でない")
    repo_parts(args.repo)
    local_checked_sha(args.dir, args.branch, args.sha)
    # These are deliberately the final operations before the exact-SHA push.
    origin_checked(args.dir, args.repo)
    config_checked(args.dir, args.config_digest)
    destination = f"refs/heads/{args.branch}"
    git(args.dir, "push", "--no-follow-tags", "--recurse-submodules=no", "origin", f"{args.sha}:{destination}")
    remote_checked(args.dir, args.branch, args.sha)
    if args.set_upstream:
        set_tracking(args.dir, args.branch)
    url = created_pr_checked(args.dir, args.gh, args.repo, args.branch, args.base, args.sha, args.body_file, args.title)
    print(url)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--config-digest", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--set-upstream", action="store_true")
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        return 2 if exc.code else 0
    if not os.path.isdir(args.dir):
        print("--dir がディレクトリでない", file=sys.stderr)
        return 2
    try:
        return publish(args)
    except Failed as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
