#!/usr/bin/env python3
"""Digest the local git configuration that ship-task's add, commit and push follow.

The contract is ship-task/references/unattended-mode.md (周の中で守る値と照合).
Read-only: this script runs git but writes nothing. It never prints configuration
values, because they may carry credentials (URLs).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys

GIT_TIMEOUT = 10
EXPECT = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
SAFE_GIT = ("--no-pager", "--no-replace-objects", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
            "-c", "core.ignoreCase=false", "-c", "core.splitIndex=false", "-c", "core.ignoreStat=false",
            "-c", "commit.gpgSign=false", "-c", "push.gpgSign=false", "-c", "filter.lfs.smudge=",
            "-c", "filter.lfs.clean=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false")


class Unreadable(Exception):
    pass


def git(directory: str, *args: str) -> bytes:
    # 設定ファイルが FIFO だと git は戻らないので打ち切る。呼び出し元のパイプは継がせない
    try:
        done = subprocess.run(
            ["git", "-C", directory, *SAFE_GIT, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Unreadable(f"git {args[0]} が時間切れ({GIT_TIMEOUT} 秒)") from exc
    except OSError as exc:
        raise Unreadable(f"git を起動できない: {exc}") from exc
    if done.returncode != 0:
        # git の stderr は値を含みうるので出さない
        raise Unreadable(f"git {args[0]} が失敗した(終了コード {done.returncode})")
    return done.stdout


def locations(directory: str) -> tuple[str, str, str]:
    # --path-format=absolute は symlink を解いた実パスを返す。config.worktree が symlink なら、--git-path は指す先の
    # パスを返すので、置き場そのものの記述には使わない(digest() は <git-dir>/config.worktree を記述し、
    # この値は置き場の欄として残す)
    out = git(directory, "rev-parse", "--path-format=absolute",
              "--git-dir", "--git-common-dir", "--git-path", "config.worktree")
    paths = out.split(b"\n")
    if len(paths) != 4 or paths[3] != b"" or not all(paths[:3]):
        raise Unreadable("git rev-parse の出力が 3 行でない")
    git_dir, common_dir, worktree_config = (os.fsdecode(p) for p in paths[:3])
    return git_dir, common_dir, worktree_config


def entries(directory: str, path: str) -> list:
    # loop.sh の snap_config と同じ読み方: include を辿らない項目の並び(同じ値の書き直しで変わらない)
    out = git(directory, "config", "--file", path, "--no-includes", "--list", "-z")
    result = []
    for record in out.split(b"\0"):
        if not record:
            continue
        key, sep, value = record.partition(b"\n")
        result.append([key.decode("utf-8", "surrogateescape"),
                       value.decode("utf-8", "surrogateescape") if sep else None])
    return result


def describe(directory: str, path: str) -> dict:
    """種類(absent / file / symlink / other)・symlink の指す先の字面・通常ファイルの項目の並び"""
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return {"path": path, "kind": "absent"}
    except OSError as exc:
        raise Unreadable(f"設定ファイルを調べられない: {exc.strerror}") from exc
    found = {"path": path}
    if stat.S_ISLNK(st.st_mode):
        found["kind"] = "symlink"
        try:
            found["target"] = os.readlink(path)
        except OSError as exc:
            raise Unreadable(f"symlink の字面を読めない: {exc.strerror}") from exc
        try:
            regular = stat.S_ISREG(os.stat(path).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            regular = False
        except OSError as exc:
            raise Unreadable(f"symlink の指す先を調べられない: {exc.strerror}") from exc
    elif stat.S_ISREG(st.st_mode):
        found["kind"] = "file"
        regular = True
    else:
        found["kind"] = "other"
        regular = False
    # 通常ファイルでないもの(FIFO など)は git に渡さない
    if regular:
        found["entries"] = entries(directory, path)
    return found


def digest(directory: str) -> str:
    git_dir, common_dir, worktree_config = locations(directory)
    material = {
        "git-dir": git_dir,
        "git-common-dir": common_dir,
        "config": describe(directory, os.path.join(common_dir, "config")),
        # per-worktree の置き場(main でも linked worktree でも <git-dir> の直下)を、解かずに lstat・readlink で記述する
        "config.worktree": describe(directory, os.path.join(git_dir, "config.worktree")),
        "config.worktree-git-path": worktree_config,
    }
    data = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("ascii")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--expect")
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.expect is not None and not EXPECT.match(args.expect):
        print("--expect の形が sha256:<64 桁の 16 進の小文字> でない", file=sys.stderr)
        return 2
    if not os.path.isdir(args.dir):
        print("--dir がディレクトリでない", file=sys.stderr)
        return 2
    try:
        value = digest(args.dir)
    except Unreadable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.expect is None:
        print(value)
        return 0
    if value != args.expect:
        print("git 設定のダイジェストが --expect と一致しない", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
