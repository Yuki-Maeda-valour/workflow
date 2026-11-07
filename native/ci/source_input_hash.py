#!/usr/bin/env python3
"""native/README.md と build.rs が定めるソース入力の SHA256。"""

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
# core/source_input.rs と base-commit.md の順序を維持する。
SAFE_GIT_PREFIX = [
    "git", "--no-pager", "--no-replace-objects",
    "-c", "core.quotePath=false", "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null", "-c", "core.ignoreCase=false",
    "-c", "core.splitIndex=false", "-c", "core.ignoreStat=false",
    "-c", "commit.gpgSign=false", "-c", "push.gpgSign=false",
    "-c", "filter.lfs.smudge=", "-c", "filter.lfs.clean=",
    "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
]


def git(root, *args):
    result = subprocess.run(
        SAFE_GIT_PREFIX + list(args), cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ValueError("git が失敗しました: " + result.stderr.decode(errors="replace").strip())
    return result.stdout


def parse_entries(raw):
    if not raw.endswith(b"\0"):
        raise ValueError("native/ の追跡ファイル一覧が空か、壊れています")
    entries = []
    for record in raw[:-1].split(b"\0"):
        header, separator, path = record.partition(b"\t")
        if not separator or not re.fullmatch(
            rb"(100644|100755|120000) [a-fA-F0-9]{40}([a-fA-F0-9]{24})? 0", header
        ):
            raise ValueError("Git のモード・オブジェクト名・競合状態が不正です")
        path.decode("utf-8", errors="strict")
        if (not path.startswith(b"native/")
                or any(c < 32 or c == 127 for c in path)
                or any(part in (b"", b".", b"..") for part in path.split(b"/"))):
            raise ValueError("追跡パスが native/ の相対パスではありません")
        entries.append((path, header.split(b" ")[0]))
    entries.sort()
    if len({path for path, _ in entries}) != len(entries):
        raise ValueError("Git の一覧に同じパスが重複しています")
    return entries


def source_input_hash(root=ROOT):
    root = Path(root).resolve()
    top = git(root, "rev-parse", "--show-toplevel").removesuffix(b"\n")
    if top != os.fsencode(root):
        raise ValueError("--root にはリポジトリ直下を指定してください")
    entries = parse_entries(git(root, "ls-files", "-s", "-z", "--", "native"))
    digest = hashlib.sha256(b"dev-workflow-source-input-v1\0")
    digest.update(len(entries).to_bytes(8, "big"))
    for raw_path, mode in entries:
        path = root / raw_path.decode("utf-8")
        parent = root
        for part in raw_path.decode("utf-8").split("/")[:-1]:
            parent /= part
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise ValueError("追跡ファイルの親が通常のフォルダではありません")
        kind = path.lstat().st_mode
        if mode == b"120000" and stat.S_ISLNK(kind):
            body = os.readlink(os.fsencode(path))
        elif mode in (b"100644", b"100755") and stat.S_ISREG(kind):
            body = path.read_bytes()
        else:
            raise ValueError("Git のモードと追跡ファイルの種類が一致しません")
        for field in (raw_path, mode, body):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        print(source_input_hash(args.root))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
