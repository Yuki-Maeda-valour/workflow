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
import time
from pathlib import Path
from typing import Any

SCHEMA = 3
DEFAULT_SECRET_PATHS = (".env", ".env.*", ".dev.vars")


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
                if not stat.S_ISDIR(before.st_mode) or not same(before, os.fstat(nxt)):
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


def run_git(top: str, *args: str, budget: Budget | None = None) -> bytes:
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(["git", "-C", top, "--no-pager", "--no-replace-objects",
                                 "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", *args],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
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
        if rc:
            raise Stop("git の状態読取に失敗した")
        return bytes(output)
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


def config_snapshot(top: str, common: str, budget: Budget) -> dict[str, Any]:
    # Git resolves include/includeIf.  The origin list is then observed as files so a
    # missing, cyclic, unreadable, FIFO, or symlink-structure change cannot pass.
    raw = run_git(top, "config", "--show-origin", "--show-scope", "--includes", "--null", "--list", budget=budget)
    origins: set[str] = set()
    for part in raw.split(b"\0"):
        if part.startswith(b"file:"):
            origin = part.decode("utf-8", "surrogateescape").split("\t", 1)[0][5:]
            if not os.path.isabs(origin):
                origin = os.path.normpath(os.path.join(top, origin))
            origins.add(origin)
    origins.add(os.path.join(common, "config"))
    observed = []
    for path in sorted(origins):
        if not os.path.lexists(path):
            raise Stop("到達する config が無い")
        observed.append(entry(path, "config:" + path, [], budget))
    return {"effective_sha256": hashlib.sha256(raw).hexdigest(), "origins": observed}


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
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise Stop("secret_paths が文字列配列でない")
    for value in paths:
        if ("//" in value or not value.strip("/") or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
                or any(ch in "'\"`$[]{}" for ch in value)):
            raise Stop("secret_paths に不正な glob がある")
    return list(DEFAULT_SECRET_PATHS) + paths


def index_snapshot(admin: str, worktree: str, budget: Budget) -> dict[str, Any]:
    path = os.path.join(admin, "index")
    result = {"state": "absent"} if not os.path.lexists(path) else entry(path, "index", [], budget)
    # Raw index bytes catch all human worktrees.  For the loop's own checkout a
    # commit necessarily rewrites them, so record whether that rewrite is clean
    # and only then allow its exact ref/HEAD transition in compare().
    proc = subprocess.run(["git", "-C", worktree, "--no-pager", "--no-replace-objects",
                           "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=",
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


def config_file_snapshot(top: str, path: str, rel: str, budget: Budget) -> dict[str, Any]:
    """Keep the old useful config diagnostic without trusting Git to open links."""
    if not os.path.lexists(path):
        return {"state": "absent"}
    observed = entry(path, rel, [], budget)
    if observed.get("kind") != "file":
        return {"state": "not-file", "entry": observed}
    raw = run_git(top, "config", "--file", path, "--no-includes", "--list", "-z", budget=budget)
    entries: list[list[str | None]] = []
    for record in raw.split(b"\0"):
        if not record: continue
        key, sep, value = record.partition(b"\n")
        entries.append([key.decode("utf-8", "surrogateescape"),
                        value.decode("utf-8", "surrogateescape") if sep else None])
    return {"state": "ok", "entry": observed, "entries": entries}


def snapshot(args: argparse.Namespace) -> None:
    budget = Budget(args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)
    wts = worktrees(args.top, budget)
    contents, configs = [], []
    for wt in wts:
        path = wt["worktree"]
        patterns = profile_patterns(path, budget)
        git_dir = run_git(path, "rev-parse", "--path-format=absolute", "--git-dir", budget=budget).decode().strip()
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
                        "effective": config_snapshot(path, args.common, budget)})
        if os.path.realpath(path) != os.path.realpath(args.exclude_worktree or ""):
            content["files"] = tree(path, patterns, budget)
        else:
            content["files"] = {"state": "self-worktree-content-excluded"}
        contents.append(content)
    state = {"schema": SCHEMA, "refs": refs(args.top, budget), "worktrees": wts,
             "worktree_contents": contents, "worktree_configs": configs,
             "config": config_snapshot(args.top, args.common, budget),
             "common_admin": common_admin_snapshot(args.common, budget),
             "common_config": config_file_snapshot(args.top, os.path.join(args.common, "config"), "config", budget),
             "common_config_worktree": config_file_snapshot(args.top, os.path.join(args.common, "config.worktree"), "config.worktree", budget),
             "repo_admin": {"path": args.repo_admin,
                            "config_worktree": config_file_snapshot(args.top, os.path.join(args.repo_admin, "config.worktree"), "repo:config.worktree", budget)}}
    write_state(args.out, state)


def compare(args: argparse.Namespace) -> int:
    try:
        before = read_state(args.before, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)
        after = read_state(args.after, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)
    except Stop:
        print("差分: 状態を読めない", file=sys.stdout); return 1
    required = {"schema", "refs", "worktrees", "worktree_contents", "worktree_configs", "config", "common_admin", "common_config", "common_config_worktree", "repo_admin"}
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
