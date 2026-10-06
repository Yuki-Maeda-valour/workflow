#!/usr/bin/env python3
"""Bounded, fail-closed state snapshots for the unattended loop.

The helper intentionally does not restore anything.  It records enough metadata to
stop the loop when another checkout, a ref, a worktree, or a reachable config input
changes while a child is running.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA = 6
DEFAULT_SECRET_PATHS = (".env", ".env.*", ".dev.vars")

# Keep every helper Git process on the shared publish-path contract.  State
# observation must not invoke repository hooks, filters, signing, fsmonitor or
# a pager while it is reading a repository that an interrupted child may alter.
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


class Stop(RuntimeError):
    pass


class Budget:
    def __init__(self, items: int, total: int, one: int, seconds: int) -> None:
        self.items, self.total, self.one = items, total, one
        self.count = self.bytes = 0
        self.deadline = time.monotonic() + seconds

    def tick(self, size: int = 0) -> None:
        if time.monotonic() > self.deadline:
            raise Stop("状態の観察が 60 秒の上限を超えた")
        self.count += 1
        if self.count > self.items:
            raise Stop("状態の観察の項目数が上限を超えた")
        if size > self.one:
            raise Stop("通常ファイル 1 件の読取量が上限を超えた")
        self.bytes += size
        if self.bytes > self.total:
            raise Stop("通常ファイルの総読取量が上限を超えた")

    def read(self, size: int) -> None:
        if time.monotonic() > self.deadline:
            raise Stop("状態の観察が 60 秒の上限を超えた")
        self.bytes += size
        if self.bytes > self.total:
            raise Stop("通常ファイルの総読取量が上限を超えた")

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise Stop("状態の観察が 60 秒の上限を超えた")
        return left


def meta(st: os.stat_result) -> dict[str, Any]:
    return {"mode": stat.S_IMODE(st.st_mode), "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
            "dev": st.st_dev, "ino": st.st_ino}


def same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino, a.st_mode, a.st_size, a.st_mtime_ns, a.st_ctime_ns) == (
        b.st_dev, b.st_ino, b.st_mode, b.st_size, b.st_mtime_ns, b.st_ctime_ns)


def same_directory_path(a: os.stat_result, b: os.stat_result) -> bool:
    """Identity for an ancestor held only to resolve a path, not its contents."""
    return (a.st_dev, a.st_ino, stat.S_IFMT(a.st_mode), stat.S_IMODE(a.st_mode)) == (
        b.st_dev, b.st_ino, stat.S_IFMT(b.st_mode), stat.S_IMODE(b.st_mode))


def same_inode(a: os.stat_result, expected: dict[str, Any]) -> bool:
    """Identity check that permits a held regular target's content to change."""
    return (a.st_dev, a.st_ino, stat.S_IMODE(a.st_mode)) == (
        expected.get("dev"), expected.get("ino"), expected.get("mode"))


def safe_hash(path: str, before: os.stat_result, budget: Budget) -> str:
    if before.st_size < 0:
        raise Stop("通常ファイルのサイズを読めない")
    budget.tick()
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise Stop(f"通常ファイルを安全に開けない: {exc.strerror}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not same(before, opened):
            raise Stop("通常ファイルが lstat と open の間に変わった")
        h = hashlib.sha256(); got = 0
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            got += len(block); budget.read(len(block))
            if got > budget.one:
                raise Stop("通常ファイル 1 件の読取量が上限を超えた")
            h.update(block)
        finished = os.fstat(fd)
        if not same(opened, finished):
            raise Stop("通常ファイルが読取中に変わった")
        return h.hexdigest()
    except OSError as exc:
        raise Stop(f"通常ファイルを読めない: {exc.strerror}") from exc
    finally:
        os.close(fd)


def safe_read(path: str, before: os.stat_result, budget: Budget) -> bytes:
    """Read one bounded regular file from the fd already checked against lstat."""
    if before.st_size > budget.one:
        raise Stop("通常ファイル 1 件の読取量が上限を超えた")
    budget.tick()
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise Stop(f"通常ファイルを安全に開けない: {exc.strerror}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not same(before, opened):
            raise Stop("通常ファイルが lstat と open の間に変わった")
        blocks: list[bytes] = []; got = 0
        while True:
            block = os.read(fd, 65536)
            if not block: break
            got += len(block); budget.read(len(block))
            if got > budget.one:
                raise Stop("通常ファイル 1 件の読取量が上限を超えた")
            blocks.append(block)
        if not same(opened, os.fstat(fd)):
            raise Stop("通常ファイルが読取中に変わった")
        return b"".join(blocks)
    finally:
        os.close(fd)


def kind(st: os.stat_result) -> str:
    if stat.S_ISREG(st.st_mode): return "file"
    if stat.S_ISDIR(st.st_mode): return "dir"
    if stat.S_ISLNK(st.st_mode): return "link"
    if stat.S_ISFIFO(st.st_mode): return "fifo"
    if stat.S_ISSOCK(st.st_mode): return "socket"
    if stat.S_ISCHR(st.st_mode): return "char"
    if stat.S_ISBLK(st.st_mode): return "block"
    return "other"


def secret_path(rel: str, patterns: list[str]) -> bool:
    # Match the existing snapshot contract: an unanchored pattern applies under
    # every directory, * does not cross '/', ** does, and matching ignores case.
    for raw in patterns:
        pattern = raw.strip("/")
        if not pattern:
            continue
        out, i = "", 0
        while i < len(pattern):
            # GNU-style `**/` also matches zero directory components.  The
            # old snapshot's exclude glob has that property; requiring a slash
            # here would open/hash a root `key.txt` for `**/key.txt`.
            if pattern.startswith("**/", i): out += "(?:.*/)?"; i += 3
            elif pattern.startswith("**", i): out += ".*"; i += 2
            elif pattern[i] == "*": out += "[^/]*"; i += 1
            elif pattern[i] == "?": out += "[^/]"; i += 1
            else: out += re.escape(pattern[i]); i += 1
        prefix = r"(?:^|.*/)" if not raw.startswith("/") else "^"
        suffix = r"(?:/.*)?$" if raw.endswith("/") else "$"
        if re.match(prefix + out + suffix, rel, re.IGNORECASE):
            return True
    return False


DIR_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


def open_dir_path(path: str) -> int:
    """Open every directory component without trusting a later pathname lookup."""
    if not os.path.isabs(path):
        raise Stop("絶対パスでないディレクトリは観察できない")
    fd = os.open("/", DIR_FLAGS)
    try:
        for name in (part for part in path.split("/") if part):
            try:
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                nxt = os.open(name, DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise Stop(f"親ディレクトリを安全に開けない: {exc.strerror}") from exc
            try:
                if not stat.S_ISDIR(before.st_mode) or not same_directory_path(before, os.fstat(nxt)):
                    raise Stop("親ディレクトリが観察中に差し替わった")
            except Exception:
                os.close(nxt)
                raise
            os.close(fd)
            fd = nxt
        return fd
    except Exception:
        os.close(fd)
        raise


def hash_at(parent: int, name: str, before: os.stat_result, budget: Budget) -> str:
    budget.tick(); got = 0
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not same(before, opened):
            raise Stop("通常ファイルが lstat と open の間に変わった")
        h = hashlib.sha256()
        while True:
            block = os.read(fd, 65536)
            if not block: break
            got += len(block); budget.read(len(block))
            if got > budget.one: raise Stop("通常ファイル 1 件の読取量が上限を超えた")
            h.update(block)
        if not same(opened, os.fstat(fd)):
            raise Stop("通常ファイルが読取中に変わった")
        return h.hexdigest()
    finally:
        os.close(fd)


def read_at(parent: int, name: str, before: os.stat_result, budget: Budget) -> bytes:
    if before.st_size > budget.one:
        raise Stop("通常ファイル 1 件の読取量が上限を超えた")
    budget.tick(); got = 0; blocks: list[bytes] = []
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not same(before, opened):
            raise Stop("通常ファイルが lstat と open の間に変わった")
        while True:
            block = os.read(fd, 65536)
            if not block: break
            got += len(block); budget.read(len(block))
            if got > budget.one: raise Stop("通常ファイル 1 件の読取量が上限を超えた")
            blocks.append(block)
        if not same(opened, os.fstat(fd)):
            raise Stop("通常ファイルが読取中に変わった")
        return b"".join(blocks)
    finally:
        os.close(fd)


def entry_at(parent: int, name: str, rel: str, patterns: list[str], budget: Budget) -> dict[str, Any]:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise Stop(f"パスを lstat できない: {rel}: {exc.strerror}") from exc
    budget.tick()
    k, result = kind(before), {"path": rel, "kind": kind(before), "meta": meta(before)}
    if secret_path(rel, patterns):
        result["secret"] = True
        return result
    if k == "file":
        result["sha256"] = hash_at(parent, name, before, budget)
    elif k == "link":
        try:
            result["target"] = os.readlink(name, dir_fd=parent)
        except OSError as exc:
            raise Stop(f"symlink を読めない: {rel}: {exc.strerror}") from exc
    elif k in ("char", "block"):
        result["rdev"] = before.st_rdev
    return result


def entry(path: str, rel: str, patterns: list[str], budget: Budget) -> dict[str, Any]:
    """Observe one path while holding its parent directory fd through the read."""
    parent_path, name = os.path.split(os.path.abspath(path))
    parent = open_dir_path(parent_path)
    try:
        return entry_at(parent, name, rel, patterns, budget)
    finally:
        os.close(parent)


def bounded_names(parent: int, budget: Budget, label: str) -> list[str]:
    """Collect one directory's names without materialising an unbounded list."""
    names: list[str] = []
    try:
        with os.scandir(parent) as children:
            for child in children:
                # Charge before retaining the name.  This also bounds a tree of
                # empty directories, whose names otherwise have no file bytes.
                budget.tick()
                names.append(child.name)
    except OSError as exc:
        raise Stop(f"{label} を列挙できない: {exc.strerror}") from exc
    return sorted(names)


def tree(root: str, patterns: list[str], budget: Budget) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    def walk(parent: int, name: str, rel: str, inherited_secret: bool = False) -> None:
        try: before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc: raise Stop(f"パスを lstat できない: {rel}: {exc.strerror}") from exc
        budget.tick(); k = kind(before); item = {"path": rel, "kind": k, "meta": meta(before)}
        # A directory glob protects the bytes below it, not their existence or
        # metadata.  Continue enumeration through it so add/remove/type/size
        # changes are still observed, but never open descendants for hashing.
        secret = inherited_secret or secret_path(rel, patterns)
        if secret: item["secret"] = True
        elif k == "file": item["sha256"] = hash_at(parent, name, before, budget)
        elif k == "link": item["target"] = os.readlink(name, dir_fd=parent)
        elif k in ("char", "block"): item["rdev"] = before.st_rdev
        result.append(item)
        if k != "dir": return
        fd = os.open(name, DIR_FLAGS, dir_fd=parent)
        try:
            if not same(before, os.fstat(fd)): raise Stop(f"ディレクトリが差し替わった: {rel}")
            for child in bounded_names(fd, budget, f"ディレクトリ {rel}"):
                if rel == "." and child == ".git": continue
                walk(fd, child, child if rel == "." else rel + "/" + child, secret)
            if not same(before, os.fstat(fd)): raise Stop(f"ディレクトリが読取中に変わった: {rel}")
        finally: os.close(fd)
    root_parent = -1
    try:
        parent_path, name = os.path.dirname(root), os.path.basename(root)
        root_parent = open_dir_path(parent_path)
        walk(root_parent, name, ".")
    finally:
        if root_parent >= 0: os.close(root_parent)
    return result


def run_git(top: str, *args: str, budget: Budget | None = None,
            allow_failure: bool = False, pass_fds: tuple[int, ...] = (),
            return_status: bool = False, env_extra: dict[str, str] | None = None) -> bytes | tuple[int, bytes]:
    proc: subprocess.Popen[bytes] | None = None
    try:
        env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
        if env_extra:
            env.update(env_extra)
        proc = subprocess.Popen(["git", "-C", top, *SAFE_GIT_PREFIX, *args],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, pass_fds=pass_fds)
        assert proc.stdout is not None
        deadline = time.monotonic() + 10
        if budget: deadline = min(deadline, budget.deadline)
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Stop("git の状態読取が時間内に終わらない")
                for key, _ in selector.select(left):
                    try: chunk = os.read(key.fd, 65536)
                    except BlockingIOError: continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    # Reject before retaining a full pipe buffer beyond the
                    # caller's shared observation limit.
                    if budget and len(chunk) > budget.total - budget.bytes:
                        raise Stop("状態の観察の総読取量が上限を超えた")
                    if budget: budget.read(len(chunk))
                    output.extend(chunk)
        try:
            rc = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise Stop("git の状態読取が時間内に終わらない") from exc
        if rc and not allow_failure:
            raise Stop("git の状態読取に失敗した")
        result = bytes(output)
        return (rc, result) if return_status else result
    except OSError as exc:
        raise Stop("git の状態読取に失敗した") from exc
    except Stop:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        if proc and proc.stdout:
            proc.stdout.close()


def refs(top: str, budget: Budget) -> list[dict[str, str]]:
    raw = run_git(top, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)%00", budget=budget)
    result = []
    # The record separator is LF; fields are NUL.  Splitting the whole stream
    # on NUL makes the LF preceding the next ref name part of that name.
    for record in raw.splitlines():
        budget.tick()
        fields = record.split(b"\0")
        if fields and fields[-1] == b"": fields.pop()
        if len(fields) != 3: raise Stop("ref の出力が壊れている")
        result.append({"name": fields[0].decode("utf-8", "surrogateescape"),
                       "oid": fields[1].decode("ascii", "strict"),
                       "symref": fields[2].decode("utf-8", "surrogateescape")})
    names = {record["name"] for record in result}
    # for-each-ref intentionally omits an unresolved symbolic ref.  It is
    # still a mutable logical ref and must participate in the snapshot.  Loose
    # symrefs are the only on-disk representation, so inspect them by held
    # directory fds and add just the omitted records.  A normal self-ref or a
    # packed ref remains represented by for-each-ref and is not rejected.
    common = run_git(top, "rev-parse", "--path-format=absolute", "--git-common-dir", budget=budget).decode().strip()
    def walk(parent: int, rel: str) -> None:
        budget.tick()
        for name in bounded_names(parent, budget, "loose ref"):
            budget.tick()
            child_rel = name if not rel else rel + "/" + name
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                fd = os.open(name, DIR_FLAGS, dir_fd=parent)
                try:
                    if not same(st, os.fstat(fd)): raise Stop("loose ref の親が差し替わった")
                    walk(fd, child_rel)
                finally: os.close(fd)
            elif stat.S_ISREG(st.st_mode):
                raw_ref = read_at(parent, name, st, budget)
                if raw_ref.startswith(b"ref: "):
                    target = raw_ref[5:].strip().decode("utf-8", "surrogateescape")
                    logical = "refs/" + child_rel
                    if not target.startswith("refs/") or logical in names:
                        continue
                    result.append({"name": logical, "oid": "", "symref": target})
                    names.add(logical)
            elif stat.S_ISLNK(st.st_mode):
                raise Stop("loose ref が symlink である")
    try:
        root = open_dir_path(os.path.join(common, "refs"))
    except Stop:
        # Repositories with all refs packed have no refs/ directory; Git's own
        # list above is complete in that case.
        if not os.path.lexists(os.path.join(common, "refs")): return sorted(result, key=lambda x: x["name"])
        raise
    try: walk(root, "")
    finally: os.close(root)
    return sorted(result, key=lambda x: x["name"])


def worktrees(top: str, budget: Budget) -> list[dict[str, str]]:
    records, current = [], {}
    for line in run_git(top, "worktree", "list", "--porcelain", "-z", budget=budget).split(b"\0"):
        if not line:
            if current:
                records.append(current); current = {}
            continue
        budget.tick()
        text = line.decode("utf-8", "surrogateescape")
        key, _, value = text.partition(" ")
        if key == "worktree" and current:
            records.append(current); current = {}
        current[key] = value
    if current: records.append(current)
    if not records: raise Stop("worktree 一覧が空")
    for rec in records:
        if "worktree" not in rec or "HEAD" not in rec: raise Stop("worktree の必須欄が無い")
        rec.setdefault("locked", "")
        rec.setdefault("branch", "")
    return sorted(records, key=lambda x: x["worktree"])


def config_rows(raw: bytes) -> list[tuple[str, str | None]]:
    rows: list[tuple[str, str | None]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        key, sep, value = record.partition(b"\n")
        rows.append((key.decode("utf-8", "surrogateescape"),
                     value.decode("utf-8", "surrogateescape") if sep else None))
    return rows


def config_origins(raw: bytes, top: str) -> list[str]:
    """Return file origins in Git's order without asking it to follow includes."""
    result: list[str] = []
    for part in raw.split(b"\0"):
        if not part.startswith(b"file:"):
            continue
        origin = part.decode("utf-8", "surrogateescape").split("\t", 1)[0][5:]
        # Keep `..` and other spelling intact.  open_dir_path() validates each
        # component; normalizing here could turn symlink/../target into another
        # object before that check.
        if not os.path.isabs(origin):
            origin = os.path.join(top, origin)
        if origin not in result:
            result.append(origin)
    return result


def read_open_regular(fd: int, before: os.stat_result, budget: Budget) -> bytes:
    """Read a held regular fd with the same bounds as a pathname observation."""
    if before.st_size > budget.one:
        raise Stop("通常ファイル 1 件の読取量が上限を超えた")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        blocks: list[bytes] = []; got = 0
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            got += len(block); budget.read(len(block))
            if got > budget.one:
                raise Stop("通常ファイル 1 件の読取量が上限を超えた")
            blocks.append(block)
        if not same(before, os.fstat(fd)):
            raise Stop("通常ファイルが読取中に変わった")
        return b"".join(blocks)
    except OSError as exc:
        raise Stop("通常ファイルを読めない") from exc


def open_regular_path(path: str, budget: Budget) -> tuple[int, os.stat_result]:
    """Open one leaf through held parent fds, rejecting links and special files."""
    parent_path, name = os.path.split(path)
    parent = open_dir_path(parent_path)
    try:
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise Stop("到達する config が無い") from exc
        budget.tick()
        if not stat.S_ISREG(before.st_mode):
            raise Stop("到達する config が通常ファイルでない")
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        opened = os.fstat(fd)
        if not same(before, opened):
            os.close(fd)
            raise Stop("config が lstat と open の間に変わった")
        return fd, before
    except OSError as exc:
        raise Stop("到達する config を安全に開けない") from exc
    finally:
        os.close(parent)


def safe_config_file(top: str, path: str, budget: Budget,
                     trusted: dict[str, Any] | None = None, *,
                     require_exact: bool = False,
                     parse: bool = True) -> tuple[dict[str, Any], list[tuple[str, str | None]]]:
    """Parse config through a held fd, accepting a trusted leaf link only once."""
    parent_path, name = os.path.split(path)
    parent = open_dir_path(parent_path)
    fd = -1
    try:
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise Stop("到達する config が無い") from exc
        budget.tick()
        expected = trusted.get(path) if trusted is not None else None
        if trusted is not None and expected is None:
            # A prior state fixes the complete active config graph.  A new
            # include must not be parsed merely because its parent was changed
            # during a child run; a fresh completed-run baseline is the only
            # point where a new origin becomes trusted.
            raise Stop("config の新しい origin は基準が無いため開けない")
        if expected is not None and (not isinstance(expected, dict) or expected.get("kind") not in ("file", "link")):
            raise Stop("保存済み config の形式を読めない")
        link_target: str | None = None
        link_before: os.stat_result | None = None
        if stat.S_ISREG(before.st_mode):
            if expected is not None and expected.get("kind") != "file":
                raise Stop("config の link 構造が基準から変わった")
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            target_before = before
        elif stat.S_ISLNK(before.st_mode):
            if expected is not None and (expected.get("kind") != "link" or expected.get("meta") != meta(before)):
                raise Stop("config の link 構造が基準から変わった")
            try:
                link_target = os.readlink(name, dir_fd=parent)
            except OSError as exc:
                raise Stop("config の link を読めない") from exc
            if expected is not None and expected.get("target") != link_target:
                raise Stop("config の link 先が基準から変わった")
            target_path = link_target if os.path.isabs(link_target) else os.path.join(parent_path, link_target)
            fd, target_before = open_regular_path(target_path, budget)
            if expected is not None:
                target_expected = expected.get("target_entry")
                if not isinstance(target_expected, dict) or not isinstance(target_expected.get("meta"), dict) or not same_inode(target_before, target_expected["meta"]):
                    raise Stop("config の link 先の実体が基準から変わった")
            link_before = before
        else:
            raise Stop("到達する config が通常ファイルでも link でもない")
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not same(target_before, opened):
            raise Stop("config が lstat と open の間に変わった")
        raw = read_open_regular(fd, target_before, budget)
        target_entry = {"kind": "file", "meta": meta(target_before), "sha256": hashlib.sha256(raw).hexdigest()}
        if link_before is not None:
            # Build the held record before any Git invocation.  A child can
            # change a root config to enable a new config layer; preflight must
            # reject that source before Git's setup has a chance to follow it.
            observed = {"path": "config:" + path, "kind": "link", "meta": meta(link_before), "target": link_target,
                        "target_entry": target_entry}
        else:
            observed = {"path": "config:" + path, **target_entry}
        if require_exact and observed != expected:
            raise Stop("保持済み config が周の途中で変わった")
        if link_before is not None:
            now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not same(link_before, now) or os.readlink(name, dir_fd=parent) != link_target:
                raise Stop("config の link が読取中に変わった")
        if not parse:
            return observed, []
        # A --file parser has no reason to initialize the observed repository.
        # Run it outside that worktree with user/system config disabled, so a
        # root source cannot make Git discover another config layer first.
        parsed = run_git("/", "config", "--file", f"/proc/self/fd/{fd}", "--no-includes", "--null", "--list",
                         budget=budget, pass_fds=(fd,),
                         env_extra={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        if not same(target_before, os.fstat(fd)):
            raise Stop("config が Git の解析中に変わった")
        if link_before is not None:
            now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not same(link_before, now) or os.readlink(name, dir_fd=parent) != link_target:
                raise Stop("config の link が読取中に変わった")
        return observed, config_rows(parsed)
    except OSError as exc:
        raise Stop("到達する config を安全に開けない") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


def config_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def memory_file(name: str, raw: bytes) -> int:
    try:
        fd = os.memfd_create(name, 0)
    except (AttributeError, OSError) as exc:
        raise Stop("includeIf の安全な条件判定を作れない") from exc
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except Exception:
        os.close(fd)
        raise


def probe_condition(top: str, condition: str, origin: str,
                    effective: list[tuple[str, str | None]], budget: Budget,
                    *, execution_top: str | None = None,
                    env_extra: dict[str, str] | None = None) -> bool:
    """Let this Git evaluate one includeIf condition without opening its target."""
    lower = condition.lower()
    for prefix in ("gitdir/i:", "gitdir:"):
        if lower.startswith(prefix) and condition[len(prefix):].startswith("./"):
            condition = condition[:len(prefix)] + os.path.join(os.path.dirname(origin), condition[len(prefix) + 2:])
            break
    # hasconfig:remote.*.url must see the active remote URLs.  Do not emulate
    # Git's pattern grammar; give those values to the same Git binary instead.
    remote_lines: list[str] = []
    for key, value in effective:
        folded = key.lower()
        if value is not None and folded.startswith("remote.") and folded.endswith(".url") and len(key) > len("remote..url"):
            remote_lines.extend((f"[remote {config_quote(key[7:-4])}]", f"\turl = {config_quote(value)}"))
    marker_fd = memory_file("loop-state-include-marker", b"[loop-state-probe]\n\tactive = yes\n")
    try:
        probe = "\n".join((*remote_lines,
                              f"[includeIf {config_quote(condition)}]",
                              f"\tpath = /proc/self/fd/{marker_fd}", "")).encode()
        probe_fd = memory_file("loop-state-include-condition", probe)
        try:
            rc, out = run_git(execution_top or top, "config", "--file", f"/proc/self/fd/{probe_fd}", "--includes", "--get",
                              "loop-state-probe.active", budget=budget, allow_failure=True,
                              pass_fds=(probe_fd, marker_fd), return_status=True, env_extra=env_extra)
            if rc == 1:
                return False
            if rc != 0:
                raise Stop("includeIf の条件を Git で判定できない")
            return out.strip() == b"yes"
        finally:
            os.close(probe_fd)
    finally:
        os.close(marker_fd)


def held_git_dir(top: str, budget: Budget) -> str:
    """Resolve .git through held descriptors, without opening its target."""
    root = open_dir_path(top)
    try:
        dotgit = os.stat(".git", dir_fd=root, follow_symlinks=False)
        budget.tick()
        if stat.S_ISDIR(dotgit.st_mode):
            admin = os.open(".git", DIR_FLAGS, dir_fd=root)
            try:
                if not same(dotgit, os.fstat(admin)):
                    raise Stop("worktree の git-dir が差し替わった")
            finally:
                os.close(admin)
            return os.path.abspath(os.path.join(top, ".git"))
        if stat.S_ISREG(dotgit.st_mode):
            raw = read_at(root, ".git", dotgit, budget)
            prefix = b"gitdir: "
            if not raw.startswith(prefix) or raw.count(b"\n") > 1:
                raise Stop("worktree の git-dir を読めない")
            value = raw[len(prefix):].rstrip(b"\n")
            if not value or b"\0" in value:
                raise Stop("worktree の git-dir を読めない")
            target = value.decode("utf-8", "surrogateescape")
            return target if os.path.isabs(target) else os.path.abspath(os.path.join(top, target))
        raise Stop("worktree の git-dir を安全に開けない")
    except OSError as exc:
        raise Stop("worktree の git-dir を安全に読めない") from exc
    finally:
        os.close(root)


def held_head(top: str, expected_git_dir: str, budget: Budget) -> bytes:
    """Read only a previously fixed worktree HEAD; config is never consulted."""
    actual_git_dir = held_git_dir(top, budget)
    if actual_git_dir != os.path.abspath(expected_git_dir):
        raise Stop("worktree の git-dir が基準から変わった")
    admin = -1
    try:
        admin = open_dir_path(actual_git_dir)
        head = os.stat("HEAD", dir_fd=admin, follow_symlinks=False)
        if not stat.S_ISREG(head.st_mode):
            raise Stop("worktree の HEAD を安全に読めない")
        return read_at(admin, "HEAD", head, budget)
    except OSError as exc:
        raise Stop("worktree の HEAD を安全に読めない") from exc
    finally:
        if admin >= 0: os.close(admin)


def held_common_dir(top: str, expected_git_dir: str, common: str, budget: Budget) -> None:
    """Fix a linked admin's commondir before Git setup can follow it."""
    actual_git_dir = held_git_dir(top, budget)
    if actual_git_dir != os.path.abspath(expected_git_dir):
        raise Stop("worktree の git-dir が基準から変わった")
    admin = -1
    try:
        admin = open_dir_path(actual_git_dir)
        try:
            before = os.stat("commondir", dir_fd=admin, follow_symlinks=False)
        except FileNotFoundError:
            # The primary worktree's admin is already the common directory.
            if actual_git_dir != os.path.abspath(common):
                raise Stop("worktree の commondir が基準から変わった")
            return
        if not stat.S_ISREG(before.st_mode):
            raise Stop("worktree の commondir を安全に読めない")
        raw = read_at(admin, "commondir", before, budget)
        if raw.count(b"\n") > 1:
            raise Stop("worktree の commondir を安全に読めない")
        value = raw.rstrip(b"\n")
        if not value or b"\0" in value:
            raise Stop("worktree の commondir を安全に読めない")
        target = value.decode("utf-8", "surrogateescape")
        resolved = target if os.path.isabs(target) else os.path.abspath(os.path.join(actual_git_dir, target))
        if resolved != os.path.abspath(common):
            raise Stop("worktree の commondir が基準から変わった")
    except OSError as exc:
        raise Stop("worktree の commondir を安全に読めない") from exc
    finally:
        if admin >= 0:
            os.close(admin)


def held_branch(top: str, expected_git_dir: str, common: str, budget: Budget) -> str | None:
    """Resolve HEAD's loose symbolic-ref chain without letting Git read config.

    ``includeIf.onbranch`` uses the final branch name, not merely the first
    spelling in HEAD.  A linked worktree HEAD can name a common ref which is in
    turn a symbolic ref.  Read that bounded chain through nofollow descriptors
    before the isolated condition probe; a direct or packed ref needs no bytes
    to establish its branch spelling.
    """
    head = held_head(top, expected_git_dir, budget)
    if not head.startswith(b"ref: refs/heads/"):
        return None
    current = head[len(b"ref: "):].strip().decode("utf-8", "surrogateescape")
    seen: set[str] = set()
    for _ in range(32):
        budget.tick()
        if (not current.startswith("refs/heads/") or current in seen
                or any(part in ("", ".", "..") for part in current.split("/"))):
            raise Stop("worktree の HEAD を読めない")
        seen.add(current)
        parts = current.split("/")
        parent = -1
        try:
            parent = open_dir_path(common)
            missing_parent = False
            for part in parts[:-1]:
                try:
                    before_parent = os.stat(part, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    # ``pack-refs --prune`` removes even a nested loose-ref
                    # parent.  There is then no symbolic link to follow; the
                    # current spelling is a normal packed/direct branch.
                    missing_parent = True
                    break
                if not stat.S_ISDIR(before_parent.st_mode):
                    raise Stop("worktree の HEAD を読めない")
                child = os.open(part, DIR_FLAGS, dir_fd=parent)
                try:
                    if not same_directory_path(before_parent, os.fstat(child)):
                        raise Stop("worktree の HEAD を読めない")
                except Exception:
                    os.close(child)
                    raise
                os.close(parent); parent = child
            if missing_parent:
                return current
            name = parts[-1]
            try:
                before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                # A packed/direct ref has no loose file.  Its name, which is
                # what onbranch evaluates, is already fixed by this chain.
                return current
            if not stat.S_ISREG(before.st_mode):
                raise Stop("worktree の HEAD を読めない")
            raw = read_at(parent, name, before, budget)
        except OSError as exc:
            raise Stop("worktree の HEAD を読めない") from exc
        finally:
            if parent >= 0:
                os.close(parent)
        if not raw.startswith(b"ref: "):
            return current
        current = raw[len(b"ref: "):].strip().decode("utf-8", "surrogateescape")
    raise Stop("worktree の HEAD の symbolic ref が深すぎる")


def preflight_condition(top: str, expected_git_dir: str, common: str, condition: str, origin: str,
                        effective: list[tuple[str, str | None]], budget: Budget,
                        expected_branch: str | None = None) -> bool | None:
    """Evaluate onbranch/hasconfig without loading this worktree configuration."""
    lower = condition.lower()
    isolated = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    if lower.startswith("hasconfig:remote.*.url:"):
        return probe_condition(top, condition, origin, effective, budget, execution_top="/", env_extra=isolated)
    if not lower.startswith("onbranch:"):
        return None if lower.startswith(("gitdir:", "gitdir/i:")) else False
    branch = held_branch(top, expected_git_dir, common, budget)
    if branch is None:
        return False
    if expected_branch is not None and branch != expected_branch:
        raise Stop("worktree の branch が基準から変わった")
    parts = branch.split("/")
    parent = tempfile.mkdtemp(prefix="loop-state-branch-")
    temp = os.path.join(parent, "git")
    try:
        run_git("/", "init", "--bare", temp, budget=budget, env_extra=isolated)
        head_path = os.path.join(temp, "HEAD")
        fd = os.open(head_path, os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ("ref: " + branch + "\n").encode("utf-8", "surrogateescape"))
        finally:
            os.close(fd)
        ref_path = os.path.join(temp, *parts)
        os.makedirs(os.path.dirname(ref_path), mode=0o700, exist_ok=True)
        ref_fd = os.open(ref_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(ref_fd, b"0" * 40 + b"\n")
        finally:
            os.close(ref_fd)
        return probe_condition(top, condition, origin, effective, budget, execution_top="/",
                               env_extra={**isolated, "GIT_DIR": temp})
    finally:
        # init can fail before it creates temp.  Do not replace that original
        # comparison failure with a cleanup-only ENOENT.
        if os.path.isdir(temp):
            for base, directories, files in os.walk(temp, topdown=False):
                for name in files: os.unlink(os.path.join(base, name))
                for name in directories: os.rmdir(os.path.join(base, name))
            os.rmdir(temp)
        if os.path.isdir(parent):
            os.rmdir(parent)


def include_target(top: str, value: str | None, origin: str, budget: Budget) -> str:
    if not value or "\0" in value:
        raise Stop("include の path が不正である")
    # Git owns ~(user) and %(prefix) semantics.  Feed the literal through its
    # path converter on a held memfd; a still-unexpanded ~ is not a safe path.
    fd = memory_file("loop-state-include-path", ("[loop-state-path]\n\tvalue = " + config_quote(value) + "\n").encode())
    try:
        raw = run_git(top, "config", "--file", f"/proc/self/fd/{fd}", "--no-includes", "--type=path", "--get",
                      "loop-state-path.value", budget=budget, pass_fds=(fd,))
    finally:
        os.close(fd)
    expanded = raw.decode("utf-8", "surrogateescape")
    if expanded.endswith("\n"):
        expanded = expanded[:-1]
    if not expanded or expanded.startswith("~"):
        raise Stop("include の path を Git と同じ規則で展開できない")
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(os.path.dirname(origin), expanded)


def config_candidate_paths(common: str, git_dir: str, budget: Budget, *,
                           held_system: str | None = None) -> list[tuple[str, str]]:
    """Potential setup roots, including absent roots, with their Git role.

    An explicit ``GIT_CONFIG_GLOBAL`` is authoritative even when it is
    ``/dev/null``: Git does not then fall back to HOME/XDG.  The installation
    system path comes from the same Git binary only while establishing a fresh
    baseline.  Later observations retain that spelling from state, before an
    ordinary repository Git command can consult a changed environment.
    """
    paths: list[tuple[str, str]] = [
        ("common", os.path.join(common, "config")),
        ("common", os.path.join(common, "config.worktree")),
    ]
    if os.path.abspath(git_dir) != os.path.abspath(common):
        paths.append(("worktree", os.path.join(git_dir, "config.worktree")))
    if "GIT_CONFIG_GLOBAL" in os.environ:
        global_config = os.environ["GIT_CONFIG_GLOBAL"]
        if global_config != os.devnull:
            paths.append(("global", os.path.abspath(global_config)))
    elif os.environ.get("HOME"):
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.environ["HOME"], ".config")
        paths.extend((("global", os.path.join(xdg, "git", "config")),
                      ("global", os.path.join(os.environ["HOME"], ".gitconfig"))))
    if os.environ.get("GIT_CONFIG_NOSYSTEM") not in ("1", "true", "yes"):
        explicit = os.environ.get("GIT_CONFIG_SYSTEM")
        if explicit:
            system_path = os.path.abspath(explicit)
        elif held_system is not None:
            system_path = held_system
        else:
            try:
                raw = run_git("/", "var", "GIT_CONFIG_SYSTEM", budget=budget,
                              env_extra={"GIT_CONFIG_GLOBAL": os.devnull})
            except Stop as exc:
                raise Stop("Git が system config の既定パス取得(git var GIT_CONFIG_SYSTEM)に対応しない。GIT_CONFIG_SYSTEM で実パスを明示する") from exc
            system_path = raw.decode("utf-8", "surrogateescape").strip()
            if not system_path or "\0" in system_path:
                raise Stop("Git の system config の既定パスを読めない。GIT_CONFIG_SYSTEM で実パスを明示する")
            system_path = os.path.abspath(system_path)
        paths.append(("system", system_path))
    return list(dict.fromkeys((kind, os.path.abspath(path)) for kind, path in paths))


def config_candidates(top: str, common: str, git_dir: str, budget: Budget) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for kind, path in config_candidate_paths(common, git_dir, budget):
        if not os.path.lexists(path):
            result.append({"kind": kind, "path": path, "state": "absent"})
            continue
        item, _ = safe_config_file(top, path, budget, parse=False)
        result.append({"kind": kind, "path": path, "state": "present", "entry": item})
    return result


def preflight_config_candidates(top: str, common: str, git_dir: str,
                                expected: dict[tuple[str, str], dict[str, Any]], budget: Budget,
                                *, allow_new_worktree_root: bool = False) -> None:
    """Compare setup roots before opening an appeared or retargeted root."""
    held_systems = {path for (kind, path) in expected if kind == "system"}
    if len(held_systems) > 1:
        raise Stop("保存済み system config の場所が矛盾している")
    held_system = next(iter(held_systems), None)
    paths = config_candidate_paths(common, git_dir, budget, held_system=held_system)
    # The aggregate has a distinct worktree root for every linked checkout;
    # only this context's root is relevant here, while common/global/system
    # records must remain identical everywhere.
    relevant = {key: expected[key] for key in paths if key in expected}
    missing = set(paths) - set(relevant)
    own_root = ("worktree", os.path.abspath(os.path.join(git_dir, "config.worktree")))
    if missing:
        # The parent supplied this exact new admin for its own just-created
        # checkout.  Git normally leaves config.worktree absent; accept only
        # that absence, never a new file or link, and never a human checkout.
        if not allow_new_worktree_root or missing != {own_root} or os.path.lexists(own_root[1]):
            raise Stop("config 候補が周の途中で変わった")
        paths = [key for key in paths if key != own_root]
    for kind, path in paths:
        saved = relevant[(kind, path)]
        exists = os.path.lexists(path)
        if saved["state"] == "absent":
            if exists:
                # In particular, do not follow a newly created symlink to
                # decide its digest: Git setup has not yet been invoked.
                raise Stop("config 候補が周の途中で変わった")
            continue
        if not exists:
            raise Stop("config 候補が周の途中で変わった")
        # Present candidates may themselves be leaf links.  Give their held
        # record to the no-parser read so a retarget is rejected before its
        # target bytes are opened.
        item, _ = safe_config_file(top, path, budget, {path: saved["entry"]},
                                   require_exact=True, parse=False)
        if {"kind": kind, "path": path, "state": "present", "entry": item} != saved:
            raise Stop("config 候補が周の途中で変わった")

def config_snapshot(top: str, common: str, budget: Budget,
                    trusted: dict[str, Any] | None = None, *, git_dir: str | None = None) -> dict[str, Any]:
    # First ask Git only for its non-include source list.  Every include target
    # is then opened through a held nofollow fd *before* the effective command
    # below can follow it.
    base = run_git(top, "config", "--show-origin", "--show-scope", "--no-includes", "--null", "--list", budget=budget)
    roots = config_origins(base, top)
    common_config = os.path.join(common, "config")
    if common_config not in roots:
        roots.append(common_config)
    observed: list[dict[str, Any]] = []
    effective: list[tuple[str, str | None]] = []
    # An origin spelling is part of its context: two paths may name the same
    # inode but resolve a relative nested include from different directories.
    # Keep that context in every recorded/processed node.  A recursion is the
    # same config inode reached from the same held parent directory; a hardlink
    # under another parent can legitimately resolve another relative include.
    prepared: dict[str, tuple[dict[str, Any], list[tuple[str, str | None]], tuple[int, int], tuple[int, int, int]]] = {}
    registered: set[str] = set()
    done: set[str] = set()
    active: set[tuple[tuple[int, int], tuple[int, int, int]]] = set()
    pending: list[tuple[str, str, str, str | None]] = []
    pending_seen: set[tuple[str, str, str | None]] = set()
    edges: dict[str, set[str]] = {}

    def load(path: str) -> tuple[dict[str, Any], list[tuple[str, str | None]], tuple[int, int], tuple[int, int, int]]:
        if path not in prepared:
            item, rows = safe_config_file(top, path, budget, trusted)
            parent = open_dir_path(os.path.dirname(path))
            try:
                parent_st = os.fstat(parent)
            finally:
                os.close(parent)
            prepared[path] = (item, rows, (item["meta"]["dev"], item["meta"]["ino"]),
                              (parent_st.st_dev, parent_st.st_ino, stat.S_IMODE(parent_st.st_mode)))
        return prepared[path]

    def register(path: str) -> tuple[dict[str, Any], list[tuple[str, str | None]], tuple[int, int], tuple[int, int, int]]:
        item, rows, identity, parent_identity = load(path)
        if path not in registered:
            registered.add(path); observed.append(item)
            # The full value stream is retained for Git's hasconfig condition.
            # include directives themselves are control flow, not values.
            effective.extend((key, value) for key, value in rows
                             if key.lower() != "include.path" and not key.lower().startswith("includeif."))
        return item, rows, identity, parent_identity

    def follow(parent: str, origin: str, value: str | None) -> None:
        target = include_target(top, value, origin, budget)
        register(target)
        child = target
        edges.setdefault(parent, set()).add(child)
        non_hasconfig(target)

    def non_hasconfig(path: str) -> None:
        _, rows, identity, parent_identity = register(path)
        if path in done:
            return
        context = (identity, parent_identity)
        if context in active:
            raise Stop("include が循環している")
        active.add(context)
        try:
            for key, value in rows:
                folded = key.lower()
                if folded == "include.path":
                    follow(path, path, value)
                elif folded.startswith("includeif.") and folded.endswith(".path"):
                    condition = key[len("includeif."):-len(".path")]
                    if condition.lower().startswith("hasconfig:"):
                        marker = (path, condition, value)
                        if marker not in pending_seen:
                            pending_seen.add(marker); pending.append(marker)
                    elif probe_condition(top, condition, path, effective, budget):
                        follow(path, path, value)
                    # A false non-hasconfig condition is deliberately not
                    # opened.  Missing machine-specific inactive files are a
                    # normal configuration state.
            done.add(path)
        finally:
            active.remove(context)

    # Git's hasconfig scans all normal source files, including a later local
    # remote URL when a global file came first.  Register every root before any
    # condition is tested, then collect the active non-hasconfig graph.
    for root in roots:
        register(root)
    for root in roots:
        non_hasconfig(root)
    cursor = 0
    while cursor < len(pending):
        origin, condition, value = pending[cursor]; cursor += 1
        if probe_condition(top, condition, origin, effective, budget):
            follow(origin, origin, value)
    visited: set[str] = set()
    visiting: set[str] = set()
    def no_cycle(node: str) -> None:
        if node in visiting:
            raise Stop("include が循環している")
        if node in visited:
            return
        visiting.add(node)
        for child in edges.get(node, ()):
            no_cycle(child)
        visiting.remove(node); visited.add(node)
    for node in edges:
        no_cycle(node)
    # Preflight has established that every active target is a held regular file.
    # Git may now calculate the normal effective configuration, including quoted
    # paths and all of its own includeIf grammar.
    raw = run_git(top, "config", "--show-origin", "--show-scope", "--includes", "--null", "--list", budget=budget)
    actual = set(config_origins(raw, top))
    checked = {item["path"][len("config:"):]: item for item in observed}
    if not actual <= checked.keys():
        raise Stop("include の到達先を事前に安全に検査できない")
    # `--includes` runs only after the graph is complete.  Re-open each source
    # through nofollow dirfds and require identical bytes/identity afterwards;
    # a swap during either phase is comparison-impossible, never accepted.
    for path in actual:
        after, _ = safe_config_file(top, path, budget, trusted)
        if after != checked[path]:
            raise Stop("config が実効値の読取中に変わった")
    return {"effective_sha256": hashlib.sha256(raw).hexdigest(), "origins": sorted(observed, key=lambda x: x["path"]),
            "candidates": config_candidates(top, common, git_dir or common, budget)}


def profile_patterns(root: str, budget: Budget) -> list[str]:
    root_fd = open_dir_path(root)
    parent = -1
    try:
        try:
            claude = os.stat(".claude", dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return list(DEFAULT_SECRET_PATHS)
        if not stat.S_ISDIR(claude.st_mode):
            raise Stop("secret_paths を安全に読めない")
        parent = os.open(".claude", DIR_FLAGS, dir_fd=root_fd)
        if not same(claude, os.fstat(parent)):
            raise Stop("secret_paths の親ディレクトリが差し替わった")
        try:
            st = os.stat("project-profile.yml", dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return list(DEFAULT_SECRET_PATHS)
        except OSError as exc:
            raise Stop("secret_paths を安全に読めない") from exc
        if not stat.S_ISREG(st.st_mode):
            raise Stop("secret_paths を安全に読めない")
        # The profile is a non-secret configuration input.  Keep its parent fd
        # while parsing so a replacement of .claude cannot redirect the read.
        raw = read_at(parent, "project-profile.yml", st, budget)
    finally:
        if parent >= 0: os.close(parent)
        os.close(root_fd)
    try:
        import yaml
        # Keep SafeLoader's valid merge-key behavior, but inspect the composed
        # graph first: an explicit duplicate secret_paths in any mapping (also
        # behind an alias) can otherwise erase an exclusion silently.
        node = yaml.compose(raw.decode("utf-8"), Loader=yaml.SafeLoader)
        seen: set[int] = set()
        def duplicates(value: object) -> None:
            if id(value) in seen: return
            seen.add(id(value))
            if isinstance(value, yaml.MappingNode):
                count = sum(k.tag == "tag:yaml.org,2002:str" and k.value == "secret_paths"
                            for k, _ in value.value)
                if count > 1: raise ValueError("duplicate secret_paths")
                for k, v in value.value: duplicates(k); duplicates(v)
            elif isinstance(value, yaml.SequenceNode):
                for child in value.value: duplicates(child)
        duplicates(node)
        data = yaml.safe_load(raw)
    except Exception as exc:
        raise Stop("secret_paths を安全に読めない") from exc
    paths = data.get("secret_paths", []) if isinstance(data, dict) else []
    return canonical_secret_patterns(paths)


def canonical_secret_patterns(paths: object) -> list[str]:
    """Validate profile patterns and make their persisted union deterministic."""
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise Stop("secret_paths が文字列配列でない")
    for value in paths:
        if ("//" in value or not value.strip("/") or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
                or any(ch in "'\"`$[]{}" for ch in value)):
            raise Stop("secret_paths に不正な glob がある")
    return list(dict.fromkeys((*DEFAULT_SECRET_PATHS, *paths)))


def prior_state(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.secret_patterns_from:
        return None
    prior = read_state(args.secret_patterns_from, args.max_items, args.max_bytes,
                       args.max_file_bytes, args.max_seconds)
    if prior.get("schema") != SCHEMA:
        raise Stop("前の状態が新しい必須欄を持たない")
    return prior


def prior_secret_patterns(prior: dict[str, Any] | None) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Load the prior safe union before any current worktree bytes are opened."""
    if prior is None:
        return {}, {}
    rows = prior.get("secret_patterns")
    if not isinstance(rows, list):
        raise Stop("前の secret_paths の状態が新しい必須欄を持たない")
    found: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "patterns"} or not isinstance(row["path"], str):
            raise Stop("前の secret_paths の状態を読めない")
        if row["path"] in found:
            raise Stop("前の secret_paths の状態が重複している")
        # Stored values include the defaults.  Validate as a profile-shaped
        # list by removing only the fixed prefix when it is present.
        patterns = row["patterns"]
        if not isinstance(patterns, list) or not all(isinstance(v, str) for v in patterns):
            raise Stop("前の secret_paths の状態を読めない")
        if not all(value in patterns for value in DEFAULT_SECRET_PATHS):
            raise Stop("前の secret_paths の状態に既定値が無い")
        canonical = canonical_secret_patterns([value for value in patterns if value not in DEFAULT_SECRET_PATHS])
        if canonical != patterns:
            raise Stop("前の secret_paths の状態が正規形でない")
        found[row["path"]] = canonical
    configs = prior.get("worktree_configs")
    if not isinstance(configs, list):
        raise Stop("前の secret_paths の worktree 状態を読めない")
    by_git_dir: dict[str, list[str]] = {}
    for config in configs:
        if not isinstance(config, dict) or not isinstance(config.get("path"), str) or not isinstance(config.get("git_dir"), str):
            raise Stop("前の secret_paths の worktree 状態を読めない")
        patterns = found.get(config["path"])
        if patterns is None:
            raise Stop("前の secret_paths の worktree 対応が無い")
        key = os.path.realpath(config["git_dir"])
        old = by_git_dir.get(key)
        if old is not None and old != patterns:
            raise Stop("前の secret_paths の worktree 対応が矛盾している")
        by_git_dir[key] = patterns
    if set(found) != {config["path"] for config in configs if isinstance(config, dict) and isinstance(config.get("path"), str)}:
        raise Stop("前の secret_paths の worktree 対応が矛盾している")
    return found, by_git_dir


def trusted_config_origins(prior: dict[str, Any] | None, fresh: bool) -> dict[str, Any] | None:
    """Use a prior run as link structure trust only inside one execution."""
    if prior is None or fresh:
        return None
    blocks: list[object] = [prior.get("config")]
    worktree_configs = prior.get("worktree_configs")
    if not isinstance(worktree_configs, list):
        raise Stop("前の config の状態を読めない")
    blocks.extend(row.get("effective") if isinstance(row, dict) else None for row in worktree_configs)
    trusted: dict[str, Any] = {}
    for block in blocks:
        origins = block.get("origins") if isinstance(block, dict) else None
        if not isinstance(origins, list):
            raise Stop("前の config の状態を読めない")
        for item in origins:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].startswith("config:"):
                raise Stop("前の config の状態を読めない")
            path = item["path"][len("config:"):]
            old = trusted.get(path)
            if old is not None and old != item:
                raise Stop("前の config の状態が矛盾している")
            trusted[path] = item
    return trusted


def trusted_config_candidates(prior: dict[str, Any] | None, fresh: bool) -> dict[tuple[str, str], dict[str, Any]] | None:
    if prior is None or fresh:
        return None
    blocks: list[object] = [prior.get("config")]
    worktree_configs = prior.get("worktree_configs")
    if not isinstance(worktree_configs, list):
        raise Stop("前の config 候補の状態を読めない")
    blocks.extend(row.get("effective") if isinstance(row, dict) else None for row in worktree_configs)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for block in blocks:
        candidates = block.get("candidates") if isinstance(block, dict) else None
        if not isinstance(candidates, list):
            raise Stop("前の config 候補の状態を読めない")
        for item in candidates:
            if (not isinstance(item, dict) or not isinstance(item.get("kind"), str)
                    or item["kind"] not in ("common", "worktree", "global", "system")
                    or not isinstance(item.get("path"), str) or item.get("state") not in ("absent", "present")):
                raise Stop("前の config 候補の状態を読めない")
            if item["state"] == "present" and not isinstance(item.get("entry"), dict):
                raise Stop("前の config 候補の状態を読めない")
            if item["state"] == "absent" and set(item) != {"kind", "path", "state"}:
                raise Stop("前の config 候補の状態を読めない")
            key = (item["kind"], item["path"])
            old = result.get(key)
            if old is not None and old != item:
                raise Stop("前の config 候補の状態が矛盾している")
            result[key] = item
    return result


def prior_worktree_git_dirs(prior: dict[str, Any] | None) -> dict[str, str]:
    if prior is None:
        return {}
    rows = prior.get("worktree_configs")
    if not isinstance(rows, list):
        raise Stop("前の worktree の git-dir 状態を読めない")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not isinstance(row.get("git_dir"), str):
            raise Stop("前の worktree の git-dir 状態を読めない")
        path, raw_git_dir = row["path"], row["git_dir"]
        if path in result or not os.path.isabs(raw_git_dir):
            raise Stop("前の worktree の git-dir 状態が重複している")
        result[path] = os.path.abspath(raw_git_dir)
    return result


def prior_worktree_branches(prior: dict[str, Any] | None) -> dict[str, str]:
    if prior is None:
        return {}
    rows = prior.get("worktrees")
    if not isinstance(rows, list):
        raise Stop("前の worktree の branch 状態を読めない")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("worktree"), str) or not isinstance(row.get("branch"), str):
            raise Stop("前の worktree の branch 状態を読めない")
        path, branch = row["worktree"], row["branch"]
        if path in result:
            raise Stop("前の worktree の branch 状態が重複している")
        result[path] = branch
    return result


def preflight_trusted_configs(top: str, trusted: dict[str, Any] | None,
                              candidates: dict[str, dict[str, Any]] | None, budget: Budget,
                              expected_git_dir: str | None = None, *, common: str | None = None,
                              expected_branch: str | None = None,
                              allow_new_worktree_root: bool = False) -> None:
    """Validate the held config graph before an ordinary Git command can load it.

    ``git config --file /proc/self/fd/... --no-includes`` only parses the fd
    already opened through safe_config_file.  This permits us to spot a changed
    root link, or a newly-added include target, without asking an ordinary Git
    command to discover and follow that target first.  A fresh completed-run
    baseline has no held graph and deliberately establishes one below.
    """
    if trusted is None:
        return
    if candidates is None or not expected_git_dir or not common:
        raise Stop("worktree の git-dir の基準が無い")
    # Reject a replaced linked-worktree admin before it can supply a new HEAD
    # to onbranch or a new config to any ordinary Git command.
    if held_git_dir(top, budget) != os.path.abspath(expected_git_dir):
        raise Stop("worktree の git-dir が基準から変わった")
    held_common_dir(top, expected_git_dir, common, budget)
    preflight_config_candidates(top, common, expected_git_dir, candidates, budget,
                                allow_new_worktree_root=allow_new_worktree_root)
    prepared: dict[str, tuple[dict[str, Any], list[tuple[str, str | None]]]] = {}
    effective: list[tuple[str, str | None]] = []
    # Complete a no-Git identity/hash pass before *any* parser is launched.
    # A changed root source can otherwise enable another config layer while a
    # previously checked global/include source is being parsed.
    for path in trusted:
        item, _ = safe_config_file(top, path, budget, trusted, require_exact=True, parse=False)
        prepared[path] = item, []
    # The checked files can now be parsed from held fds outside the repository.
    # Preserve their rows for include and hasconfig evaluation.
    for path in trusted:
        item, rows = safe_config_file(top, path, budget, trusted, require_exact=True)
        prepared[path] = item, rows
        effective.extend((key, value) for key, value in rows
                         if key.lower() != "include.path" and not key.lower().startswith("includeif."))
    for origin, (item, rows) in prepared.items():
        for key, value in rows:
            folded = key.lower()
            if folded == "include.path":
                target = include_target(top, value, origin, budget)
            elif folded.startswith("includeif.") and folded.endswith(".path"):
                condition = key[len("includeif."):-len(".path")]
                active = preflight_condition(top, expected_git_dir, common, condition, origin, effective, budget,
                                             expected_branch)
                if active is None:
                    # gitdir is unchanged for a held worktree.  An unknown
                    # target is therefore safe only when its source bytes are
                    # also unchanged from the graph that already classified it
                    # inactive.  Any edited source is comparison-impossible.
                    target = include_target(top, value, origin, budget)
                    if target not in trusted and item != trusted.get(origin):
                        raise Stop("config の条件付き origin を安全に判定できない")
                    continue
                if not active:
                    continue
                target = include_target(top, value, origin, budget)
            else:
                continue
            if target not in trusted:
                # Do not open a newly named file to decide whether it is safe.
                raise Stop("config の新しい origin は基準が無いため開けない")


def index_snapshot(admin: str, worktree: str, budget: Budget) -> dict[str, Any]:
    path = os.path.join(admin, "index")
    result = {"state": "absent"} if not os.path.lexists(path) else entry(path, "index", [], budget)
    # Raw index bytes catch all human worktrees.  For the loop's own checkout a
    # commit necessarily rewrites them, so record whether that rewrite is clean
    # and only then allow its exact ref/HEAD transition in compare().
    proc = subprocess.run(["git", "-C", worktree, *SAFE_GIT_PREFIX,
                           "diff-index", "--quiet", "HEAD", "--"], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=min(10, budget.remaining()),
                          env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    if proc.returncode not in (0, 1): raise Stop("index の整合を読めない")
    result["head_clean"] = proc.returncode == 0
    return result


def read_state(path: str, items: int, total: int, one: int, seconds: int) -> dict[str, Any]:
    """Read a saved state as an untrusted filesystem object, never as a pathname."""
    parent_path, name = os.path.split(os.path.abspath(path))
    parent = open_dir_path(parent_path)
    try:
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise Stop("状態を読めない") from exc
        if not stat.S_ISREG(before.st_mode):
            raise Stop("状態が通常ファイルでない")
        # A state file aggregates many permitted files, so its own single-file
        # ceiling is the caller's total ceiling.  It remains bounded and is never
        # a reason to silently skip comparison.
        raw = read_at(parent, name, before, Budget(items, total, max(total, one), seconds))
    finally:
        os.close(parent)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Stop("状態を読めない") from exc
    if not isinstance(value, dict):
        raise Stop("状態がオブジェクトでない")
    return value


def write_state(path: str, value: dict[str, Any]) -> None:
    """Atomically replace the output without opening an attacker supplied .tmp link."""
    parent_path, name = os.path.split(os.path.abspath(path))
    parent = open_dir_path(parent_path)
    tmp_name = f".{name}.tmp.{os.getpid()}"
    try:
        try:
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
        except OSError as exc:
            raise Stop("状態の一時ファイルを安全に作れない") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            try: os.unlink(tmp_name, dir_fd=parent)
            except OSError: pass
            raise
        try:
            os.replace(tmp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
        except OSError as exc:
            raise Stop("状態を安全に置き換えられない") from exc
    finally:
        os.close(parent)


def optional_entry(path: str, rel: str, budget: Budget) -> dict[str, Any]:
    """Record an admin input without treating its absence as a read failure."""
    if not os.path.lexists(path):
        return {"state": "absent"}
    return entry(path, rel, [], budget)


def common_admin_snapshot(common: str, budget: Budget) -> dict[str, Any]:
    hooks = os.path.join(common, "hooks")
    if not os.path.lexists(hooks):
        hooks_value: Any = {"state": "absent"}
    else:
        st = os.lstat(hooks)
        if stat.S_ISDIR(st.st_mode):
            hooks_value = {"state": "dir", "entries": tree(hooks, [], budget)}
        else:
            hooks_value = optional_entry(hooks, "common:hooks", budget)
    info = {name: optional_entry(os.path.join(common, "info", name), "common:info/" + name, budget)
            for name in ("exclude", "attributes", "sparse-checkout", "grafts")}
    return {"hooks": hooks_value, "info": info}


def private_ref_snapshot(git_dir: str, budget: Budget) -> dict[str, Any]:
    """Observe refs private to one linked worktree, including unresolved refs."""
    head = entry(os.path.join(git_dir, "HEAD"), "private:HEAD", [], budget)
    refs_path = os.path.join(git_dir, "refs")
    refs: Any = {"state": "absent"}
    if os.path.lexists(refs_path):
        refs = tree(refs_path, [], budget)
    return {"head": head, "refs": refs}


def config_file_snapshot(top: str, path: str, rel: str, budget: Budget,
                         trusted: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep the old useful config diagnostic without trusting Git to open links."""
    if not os.path.lexists(path):
        return {"state": "absent"}
    observed, rows = safe_config_file(top, path, budget, trusted)
    observed["path"] = rel
    return {"state": "ok", "entry": observed,
            "entries": [[key, value] for key, value in rows]}


def snapshot(args: argparse.Namespace) -> None:
    budget = Budget(args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)
    prior = prior_state(args)
    previous_patterns, previous_patterns_by_git_dir = prior_secret_patterns(prior)
    trusted_configs = trusted_config_origins(prior, args.fresh_config_baseline)
    trusted_candidates = trusted_config_candidates(prior, args.fresh_config_baseline)
    prior_git_dirs = prior_worktree_git_dirs(prior)
    prior_branches = prior_worktree_branches(prior)
    # This must precede worktree/rev-parse/config discovery: those ordinary Git
    # commands may load GIT_CONFIG_GLOBAL and other held config roots.
    top_expected = prior_git_dirs.get(args.top)
    preflight_trusted_configs(args.top, trusted_configs, trusted_candidates, budget, top_expected,
                              common=args.common, expected_branch=prior_branches.get(args.top))
    wts = worktrees(args.top, budget)
    if prior is not None and not args.fresh_config_baseline:
        old_paths = {row["worktree"] for row in prior.get("worktrees", [])
                     if isinstance(row, dict) and isinstance(row.get("worktree"), str)}
        if len(old_paths) != len(prior.get("worktrees", [])):
            raise Stop("前の worktree 状態を読めない")
        own = os.path.realpath(args.exclude_worktree) if args.exclude_worktree else None
        for worktree in wts:
            path = worktree["worktree"]
            if path not in old_paths and os.path.realpath(path) != own:
                # A moved/added human checkout can otherwise lose its path-keyed
                # secret union before its profile or files are observed.
                raise Stop("人の worktree が周の途中で追加または移動した")
    # includeIf.onbranch/gitdir is evaluated in the worktree that will later
    # run rev-parse/index/config.  Preflight each such context first: a target
    # inactive at TOP can be active in a human or the loop's own checkout.
    top_real = os.path.realpath(args.top)
    for worktree in wts:
        path = worktree["worktree"]
        if os.path.realpath(path) == top_real:
            continue
        expected = prior_git_dirs.get(path)
        own_path = bool(args.exclude_worktree and os.path.realpath(path) == os.path.realpath(args.exclude_worktree))
        own_new = False
        if expected is None and own_path:
            expected = args.wt_admin if args.wt_admin != "-" else None
            own_new = expected is not None
        # The loop's own detached checkout legitimately becomes its exact task
        # branch before compare() verifies that bounded ref/HEAD transition.
        # Human worktrees remain branch-fixed before ordinary Git is invoked.
        expected_branch = None if own_path else prior_branches.get(path)
        preflight_trusted_configs(path, trusted_configs, trusted_candidates, budget, expected,
                                  common=args.common, expected_branch=expected_branch,
                                  allow_new_worktree_root=own_new)
    contents, configs, secret_patterns = [], [], []
    for wt in wts:
        path = wt["worktree"]
        # Once a path was declared secret, later profile deletion or narrowing
        # cannot turn its pre-comparison observation into a content hash.  The
        # parent-held preceding state is read before this worktree is walked.
        git_dir = run_git(path, "rev-parse", "--path-format=absolute", "--git-dir", budget=budget).decode().strip()
        inherited = previous_patterns.get(path)
        if inherited is None and args.fresh_config_baseline:
            # A normal completed-run boundary may legitimately move a human
            # worktree.  Its Git admin directory is stable across that move,
            # so retain only its own accumulated union under the new pathname.
            inherited = previous_patterns_by_git_dir.get(os.path.realpath(git_dir), [])
        patterns = list(dict.fromkeys((inherited or []) + profile_patterns(path, budget)))
        secret_patterns.append({"path": path, "patterns": patterns})
        content: dict[str, Any] = {"path": path, "index": index_snapshot(git_dir, path, budget)}
        configs.append({"path": path, "git_dir": git_dir,
                        "config_worktree": (entry(os.path.join(git_dir, "config.worktree"), "config.worktree", [], budget)
                                            if os.path.lexists(os.path.join(git_dir, "config.worktree")) else {"state": "absent"}),
                        # The primary worktree's git-dir is the common dir;
                        # its refs are already covered by refs().  Only a
                        # linked worktree has an additional private namespace.
                        "private_refs": (private_ref_snapshot(git_dir, budget)
                                         if os.path.realpath(git_dir) != os.path.realpath(args.common)
                                         else {"state": "common-refs-covered"}),
                        "effective": config_snapshot(path, args.common, budget, trusted_configs, git_dir=git_dir)})
        if os.path.realpath(path) != os.path.realpath(args.exclude_worktree or ""):
            content["files"] = tree(path, patterns, budget)
        else:
            content["files"] = {"state": "self-worktree-content-excluded"}
        contents.append(content)
    state = {"schema": SCHEMA, "refs": refs(args.top, budget), "worktrees": wts,
             "worktree_contents": contents, "worktree_configs": configs,
             "secret_patterns": secret_patterns,
             "config": config_snapshot(args.top, args.common, budget, trusted_configs, git_dir=args.repo_admin),
             "common_admin": common_admin_snapshot(args.common, budget),
             "common_config": config_file_snapshot(args.top, os.path.join(args.common, "config"), "config", budget, trusted_configs),
             "common_config_worktree": config_file_snapshot(args.top, os.path.join(args.common, "config.worktree"), "config.worktree", budget, trusted_configs),
             "repo_admin": {"path": args.repo_admin,
                            "config_worktree": config_file_snapshot(args.top, os.path.join(args.repo_admin, "config.worktree"), "repo:config.worktree", budget, trusted_configs)}}
    write_state(args.out, state)


def compare(args: argparse.Namespace) -> int:
    try:
        before = read_state(args.before, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)
        after = read_state(args.after, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)
    except Stop:
        print("差分: 状態を読めない", file=sys.stdout); return 1
    required = {"schema", "refs", "worktrees", "worktree_contents", "worktree_configs", "secret_patterns", "config", "common_admin", "common_config", "common_config_worktree", "repo_admin"}
    if not isinstance(before, dict) or before.get("schema") != SCHEMA or not required <= before.keys():
        if isinstance(before, dict) and "repo_admin" not in before:
            print("差分: repo:git-dir: 保存済み状態に人の管理パスが無い(前の起動元の設定を確認する。現在の設定で中断前の基準を補完しない)")
        print("差分: 保存済み状態が新しい必須欄を持たない(補完せず停止)"); return 1
    # A child that made no repository change is a normal failed/held iteration;
    # it must reach the later judge instead of requiring a task branch that does
    # not exist.  Any difference takes the strict self-transition path below.
    if before == after:
        return 0
    # The loop itself creates exactly one locked worktree and advances exactly its
    # task ref.  The accepted delta is tied to this path/ref and to the resulting
    # worktree HEAD; no prefix or name-only exception is used.
    if args.self_worktree and args.self_ref:
        bw = next((w for w in before["worktrees"] if w["worktree"] == args.self_worktree), None)
        aw = next((w for w in after["worktrees"] if w["worktree"] == args.self_worktree), None)
        ar = next((r for r in after["refs"] if r["name"] == args.self_ref), None)
        bc = next((c for c in before["worktree_contents"] if c["path"] == args.self_worktree), None)
        ac = next((c for c in after["worktree_contents"] if c["path"] == args.self_worktree), None)
        detached_child = (bw and aw and bw.get("branch") == "" and aw.get("branch") == ""
                          and bw.get("HEAD") != aw.get("HEAD") and ar is None)
        if (not bw or not aw or not bc or not ac or bw.get("locked") != aw.get("locked")
                or bw.get("branch") not in ("", args.self_ref)
                or (not detached_child and (not ar or aw.get("branch") != args.self_ref
                                            or aw.get("HEAD") != ar.get("oid") or ar.get("symref")))
                or not ac.get("index", {}).get("head_clean")):
            for line in diagnostic_diffs(before, after, required): print("差分: " + line)
            print("差分: 自分の worktree/ref の期待状態に一致しない"); return 1
        # Normalize only the post-commit HEAD and its clean, necessarily rewritten
        # index.  Every other existing worktree remains byte-for-byte compared.
        expected_oid = aw["HEAD"]
        aw["HEAD"] = bw["HEAD"]
        aw["branch"] = bw["branch"]
        if "detached" in bw:
            aw["detached"] = bw["detached"]
        else:
            aw.pop("detached", None)
        ac["index"] = bc["index"]
        # Creating the task branch changes only this worktree's private HEAD
        # from a detached OID to the already-checked exact task ref.  Keep the
        # rest of its private refs strict: they are not part of a commit.
        bcfg = next((c for c in before["worktree_configs"] if c["path"] == args.self_worktree), None)
        acfg = next((c for c in after["worktree_configs"] if c["path"] == args.self_worktree), None)
        if not bcfg or not acfg or not isinstance(bcfg.get("private_refs"), dict) or not isinstance(acfg.get("private_refs"), dict):
            print("差分: 自分の private HEAD の期待状態に一致しない"); return 1
        acfg["private_refs"]["head"] = bcfg["private_refs"].get("head")
        if detached_child:
            if before == after: return 0
            for line in diagnostic_diffs(before, after, required): print("差分: " + line)
            return 1
        br = next((r for r in before["refs"] if r["name"] == args.self_ref), None)
        # A task can legitimately end without a commit (for example a held or
        # failed implementation after it created its branch).  A newly created
        # self branch then points at the initial detached HEAD.  Its path,
        # exact ref name, clean index, and worktree identity were all checked
        # above; it is not an unbounded ref exception.  An already present
        # branch, however, must still advance when this path is taken.
        if br and ar.get("oid") == br.get("oid"):
            print("差分: 自分の branch が新しい commit へ進んでいない"); return 1
        if br:
            ar["oid"] = br["oid"]
        else:
            after["refs"].remove(ar)
        # A successful push may create or advance only the matching origin
        # tracking ref to the same commit.  A different remote, name, target, or
        # symbolic ref is never an exception.
        remote = "refs/remotes/origin/" + args.self_ref.removeprefix("refs/heads/")
        br = next((r for r in before["refs"] if r["name"] == remote), None)
        ar = next((r for r in after["refs"] if r["name"] == remote), None)
        if ar and (not br or ar != br):
            if ar.get("oid") != expected_oid or ar.get("symref"):
                print("差分: 自分の remote-tracking ref の期待状態に一致しない"); return 1
            if br: ar["oid"] = br["oid"]
            else: after["refs"].remove(ar)
    if before == after: return 0
    for line in diagnostic_diffs(before, after, required): print("差分: " + line)
    return 1


def diagnostic_diffs(before: dict[str, Any], after: dict[str, Any], required: set[str]) -> list[str]:
    """Preserve actionable config diagnostics while retaining strict equality."""
    out: list[str] = []
    configs = (("config", before.get("common_config"), after.get("common_config")),
               ("config.worktree", before.get("common_config_worktree"), after.get("common_config_worktree")),
               ("repo:config.worktree", before.get("repo_admin", {}).get("config_worktree"), after.get("repo_admin", {}).get("config_worktree")))
    if before.get("repo_admin", {}).get("path") != after.get("repo_admin", {}).get("path"):
        out.append("repo:git-dir: 人の管理パスが変わった(前の起動元の設定を確認する。未変更とは判定しない)")
    for name, left, right in configs:
        if left == right: continue
        if isinstance(left, dict) and isinstance(right, dict) and left.get("state") == right.get("state") == "ok":
            old = [k if v is None else k + "=" + v for k, v in left.get("entries", [])]
            new = [k if v is None else k + "=" + v for k, v in right.get("entries", [])]
            for line in difflib.unified_diff(old, new, lineterm="", n=0):
                if not line.startswith(("---", "+++", "@@")): out.append(name + ": " + line)
        else:
            out.append(name + " が変わった")
    for key in sorted(required):
        if key not in {"common_config", "common_config_worktree", "repo_admin"} and before.get(key) != after.get(key):
            out.append(f"{key} が変わった")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    for option in ("out", "top", "common", "repo-admin"): snap.add_argument("--" + option, required=True)
    snap.add_argument("--wt-admin", default="-"); snap.add_argument("--exclude-worktree", default="")
    snap.add_argument("--secret-patterns-from", default="")
    snap.add_argument("--fresh-config-baseline", action="store_true")
    snap.add_argument("--max-items", type=int, default=100000); snap.add_argument("--max-bytes", type=int, default=1073741824)
    snap.add_argument("--max-file-bytes", type=int, default=67108864); snap.add_argument("--max-seconds", type=int, default=60)
    cmp = sub.add_parser("compare"); cmp.add_argument("--before", required=True); cmp.add_argument("--after", required=True)
    cmp.add_argument("--self-worktree", default=""); cmp.add_argument("--self-ref", default="")
    cmp.add_argument("--max-items", type=int, default=100000); cmp.add_argument("--max-bytes", type=int, default=1073741824)
    cmp.add_argument("--max-file-bytes", type=int, default=67108864); cmp.add_argument("--max-seconds", type=int, default=60)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            if any(getattr(args, x) <= 0 for x in ("max_items", "max_bytes", "max_file_bytes", "max_seconds")):
                raise Stop("状態観察の上限は正の整数でなければならない")
            snapshot(args)
            return 0
        if any(getattr(args, x) <= 0 for x in ("max_items", "max_bytes", "max_file_bytes", "max_seconds")):
            raise Stop("状態観察の上限は正の整数でなければならない")
        return compare(args)
    except Stop as exc:
        print(f"ERROR [loop-state] {exc}", file=sys.stderr)
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
