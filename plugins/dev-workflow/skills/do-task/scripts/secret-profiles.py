#!/usr/bin/env python3
"""Build the secret-path union for a review snapshot without reading unsafe files.

The caller supplies immutable commit object IDs with --ref.  The current profile is
read from the working tree only after lstat/open/fstat proves that the same object is
a regular file.  Output is a NUL-separated, deterministic list of glob strings.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


DEFAULT_PATHS = (".env", ".env.*", ".dev.vars")
PROFILE_PATH = ".claude/project-profile.yml"
PROFILE_DIR = ".claude"
PROFILE_NAME = "project-profile.yml"
MAX_PROFILE_BYTES = 1024 * 1024
GIT_TIMEOUT = 10


class ProfileError(Exception):
    pass


def git(cwd: str, *args: str) -> bytes:
    try:
        done = subprocess.run(
            ["git", "-C", cwd, "--no-pager", "--no-replace-objects",
             "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
            check=False,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileError("profile の git 読取が時間内に完了しない") from exc
    if done.returncode:
        raise ProfileError("profile の git 読取に失敗した")
    return done.stdout


def hash_length(cwd: str) -> int:
    value = git(cwd, "rev-parse", "--show-object-format").strip()
    if value == b"sha1":
        return 40
    if value == b"sha256":
        return 64
    raise ProfileError("object format を確認できない")


def commit_ref(cwd: str, ref: str, length: int) -> str | None:
    if not re.fullmatch(rf"[0-9a-f]{{{length}}}", ref):
        raise ProfileError("--secret-profile-ref は完全な小文字の OID でなければならない")
    empty_tree = git(cwd, "hash-object", "-t", "tree", "/dev/null").strip().decode("ascii")
    if ref == empty_tree:
        return None
    try:
        resolved = git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip().decode("ascii")
    except ProfileError as exc:
        raise ProfileError("--secret-profile-ref が commit に解決できない") from exc
    if resolved != ref:
        raise ProfileError("--secret-profile-ref の OID が一致しない")
    return ref


def current_profile(root: Path) -> bytes | None:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ProfileError("O_NOFOLLOW を使えない環境では現在の profile を読まない")
    try:
        root_fd = os.open(root, flags | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProfileError(f"profile の親を安全に開けない: {exc.strerror}") from exc
    try:
        return current_profile_fd(root_fd)
    finally:
        os.close(root_fd)


def current_profile_fd(root_fd, check=lambda: None, inspected=None):
    """保持したroot fdから固定profileを読む。所有fdは閉じない。"""
    flags = os.O_RDONLY | os.O_NONBLOCK
    try:
        try:
            profile_dir_fd = os.open(PROFILE_DIR, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProfileError(f"profile の親を安全に開けない: {exc.strerror}") from exc
        try:
            try:
                before = os.lstat(PROFILE_NAME, dir_fd=profile_dir_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise ProfileError(f"現在の profile を検査できない: {exc.strerror}") from exc
            if not stat.S_ISREG(before.st_mode):
                raise ProfileError("現在の profile が通常ファイルでない")
            if before.st_size > MAX_PROFILE_BYTES:
                raise ProfileError("現在の profile が上限 1 MiB を超える")
            try:
                fd = os.open(PROFILE_NAME, flags | os.O_NOFOLLOW, dir_fd=profile_dir_fd)
            except OSError as exc:
                raise ProfileError(f"現在の profile を安全に開けない: {exc.strerror}") from exc
            try:
                after = os.fstat(fd)
                if (not stat.S_ISREG(after.st_mode) or before.st_dev != after.st_dev
                        or before.st_ino != after.st_ino or before.st_mode != after.st_mode
                        or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                        or before.st_ctime_ns != after.st_ctime_ns
                        or after.st_size > MAX_PROFILE_BYTES):
                    raise ProfileError("現在の profile が読取中に差し替わった")
                chunks: list[bytes] = []
                size = 0
                while True:
                    check()
                    block = os.read(fd, 65536)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_PROFILE_BYTES:
                        raise ProfileError("現在の profile が読取中に上限 1 MiB を超えた")
                    chunks.append(block)
                finished = os.fstat(fd)
                if (size != after.st_size or finished.st_dev != after.st_dev or finished.st_ino != after.st_ino
                        or finished.st_size != after.st_size
                        or finished.st_mtime_ns != after.st_mtime_ns
                        or finished.st_ctime_ns != after.st_ctime_ns
                        or finished.st_mode != after.st_mode):
                    raise ProfileError("現在の profile が読取中に変更された")
                if inspected is not None:
                    inspected(finished)
                return b"".join(chunks)
            except OSError as exc:
                raise ProfileError(f"現在の profile を読めない: {exc.strerror}") from exc
            finally:
                os.close(fd)
        finally:
            os.close(profile_dir_fd)
    finally:
        check()


def ref_profile(cwd: str, ref: str) -> bytes | None:
    _oid, raw = ref_profile_backend(lambda *args: git(cwd, *args), ref,
                                    PROFILE_PATH.encode())
    return raw


def ref_profile_backend(backend, ref, profile_path):
    """呼出側が固定したbackendとliteral repo相対pathでprofileだけを取得する。"""
    if (not isinstance(profile_path, bytes) or not profile_path or profile_path.startswith(b"/")
            or b"\0" in profile_path or any(x in (b"", b".", b"..") for x in profile_path.split(b"/"))):
        raise ProfileError("profile の相対pathが不正")
    listed = backend("ls-tree", "-z", ref, "--", profile_path)
    if not listed:
        return None, None
    if not listed.endswith(b"\0") or listed.count(b"\0") != 1:
        raise ProfileError("基準 profile の形式を確認できない")
    meta, sep, name = listed[:-1].partition(b"\t")
    fields = meta.split()
    if (not sep or name != profile_path or len(fields) != 3
            or fields[0] not in (b"100644", b"100755") or fields[1] != b"blob"
            or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[2])):
        raise ProfileError("基準 profile が通常ファイルでない")
    oid = fields[2].decode("ascii")
    size = backend("cat-file", "-s", oid).strip()
    if not re.fullmatch(rb"[0-9]+", size) or len(size)>10 or int(size)>MAX_PROFILE_BYTES:
        raise ProfileError("基準 profile の大きさを確認できない")
    raw = backend("cat-file", "blob", oid)
    if len(raw)!=int(size) or len(raw)>MAX_PROFILE_BYTES:
        raise ProfileError("基準 profile の大きさが一致しない")
    return oid, raw


def paths_from_yaml(raw: bytes, source: str) -> list[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProfileError(f"{source} の profile は UTF-8 でなければならない") from exc
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ProfileError("profile があるため PyYAML が必要") from exc
    try:
        # PyYAML の通常の SafeLoader と同じく merge key は受理する。全キーの
        # 重複を独自に拒否すると、SafeLoader が正当に展開できる `<<` まで
        # constructor error になる。一方、どの mapping でも secret_paths を
        # 明示して複数回書くと、merge 元を通じて機密除外を曖昧にできるため停止する。
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        seen_nodes: set[int] = set()

        def reject_duplicate_secret_paths(candidate: object) -> None:
            candidate_id = id(candidate)
            if candidate_id in seen_nodes:
                return
            seen_nodes.add(candidate_id)
            if isinstance(candidate, yaml.MappingNode):
                explicit_secret_paths = sum(
                    key.tag == "tag:yaml.org,2002:str" and key.value == "secret_paths"
                    for key, _value in candidate.value
                )
                if explicit_secret_paths > 1:
                    raise ValueError("duplicate secret_paths")
                for key, value in candidate.value:
                    reject_duplicate_secret_paths(key)
                    reject_duplicate_secret_paths(value)
            elif isinstance(candidate, yaml.SequenceNode):
                for value in candidate.value:
                    reject_duplicate_secret_paths(value)

        reject_duplicate_secret_paths(node)
        data = yaml.safe_load(text)
    except Exception as exc:
        raise ProfileError(f"{source} の profile YAML を読めない") from exc
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ProfileError(f"{source} の profile は map でなければならない")
    value = data.get("secret_paths", [])
    if value is None:
        raise ProfileError(f"{source} の secret_paths は配列でなければならない")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileError(f"{source} の secret_paths は文字列の配列でなければならない")
    for item in value:
        validate_glob(item, source)
    return value


def validate_glob(value: str, source: str) -> None:
    if not value or "//" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ProfileError(f"{source} の secret_paths に不正な glob がある")
    if any(ch in "'\"`$[]{}" for ch in value):
        raise ProfileError(f"{source} の secret_paths に不正な glob がある")
    if not value.strip("/"):
        raise ProfileError(f"{source} の secret_paths に不正な glob がある")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--ref", action="append", default=[])
    args = parser.parse_args()
    root = Path(args.cwd)
    if not root.is_dir():
        print("ERROR [secret-profile] --cwd がディレクトリでない", file=sys.stderr)
        return 2
    try:
        length = hash_length(str(root))
        refs = [commit_ref(str(root), ref, length) for ref in args.ref]
        values: list[str] = list(DEFAULT_PATHS)
        current = current_profile(root)
        if current is not None:
            values.extend(paths_from_yaml(current, "現在"))
        for ref in refs:
            if ref is None:
                continue
            historic = ref_profile(str(root), ref)
            if historic is not None:
                values.extend(paths_from_yaml(historic, f"参照 {ref}"))
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                seen.add(value)
                unique.append(value)
        sys.stdout.buffer.write(b"".join(item.encode("utf-8") + b"\0" for item in unique))
        return 0
    except ProfileError as exc:
        print(f"ERROR [secret-profile] {exc}", file=sys.stderr)
        return 20


if __name__ == "__main__":
    sys.exit(main())
