#!/usr/bin/env python3
"""Merge the base branch into a reviewed work branch and verify what came in.

The caller keeps the reviewed commit D and the base commit B outside the work
tree.  This helper merges B into D without trusting the work tree, and compares
the work-versus-base diff of the reviewed state with the one after the merge.
When the integration changes what was reviewed, or when it conflicts, it stops
with `needs-review` so that a person or a reviewer decides.  URLs and Git
stderr are never copied to diagnostics.

Exit codes: 0 ok, 1 refused or failed, 2 usage, 3 needs-review (merge `merge`
only; the merge is left in progress), 4 secret-overlap.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


GIT_TIMEOUT = 30
NET_TIMEOUT = 120  # main() may shorten it through INTEGRATE_GUARD_NET_TIMEOUT (1-120), for tests
PRECHECK_TIMEOUT = 120
JSON_LIMIT = 16 * 1024 * 1024
FILE_LIMIT = 64 * 1024 * 1024
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
REPO = re.compile(r"[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
SKILLS = Path(__file__).parents[2]
# Same four state files as review-guard.py DEFAULT_EXCLUDE.
STATE_FILES = re.compile(r"(^|/)\.claude/(reviews(/|$)|grasp\.md$|settings\.local\.json$|\.understand-project-done$)")
MARKERS = re.compile(rb"(?m)^(<<<<<<< |\|\|\|\|\|\|\| |>>>>>>> )")
MERGE_MARKER = re.compile(rb"(?m)^<<<<<<< ")
FIELDS = ("result", "reason", "base", "base_sha", "reviewed_sha", "merge_base", "head", "overlap",
          "problem_paths", "binary_conflicts", "index_sha256", "changed_paths", "untracked",
          "exclude_ere", "exclude_globs", "added_globs")
LIST_FIELDS = {"overlap", "problem_paths", "binary_conflicts", "changed_paths", "untracked",
               "exclude_ere", "exclude_globs", "added_globs"}

# Every Git process started here receives these before its subcommand.  No
# repository hook, fsmonitor, GPG signer, LFS filter, pager or lazy fetch may
# turn an observation or an integration into arbitrary local code.
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
# fetch / merge / commit: no remembered resolutions, readable conflict markers, no gc.
MERGE_FLAGS = ("-c", "rerere.enabled=false", "-c", "merge.conflictStyle=diff3",
               "-c", "maintenance.auto=false", "-c", "gc.auto=0")
# Commands that read the work tree or stage from it must not follow a local
# core.filemode=false / core.symlinks=false (same as diff-snapshot.sh).
FS_FLAGS = ("-c", "core.filemode=true", "-c", "core.symlinks=true")

STATE: dict = {}


class Stop(Exception):
    def __init__(self, code: int, message: str, result: str = "error", reason: str | None = None, **fields):
        super().__init__(message)
        self.code, self.message, self.result, self.reason, self.fields = code, message, result, reason, fields


def fail(message: str, **fields) -> Stop:
    return Stop(1, message, **fields)


# ---------------------------------------------------------------- processes

def run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_LITERAL_PATHSPECS": "1"},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise fail("外部コマンドが時間切れ") from exc
    except OSError as exc:
        raise fail("外部コマンドを起動できない") from exc


def git_rc(root: str, *args: str, timeout: int = GIT_TIMEOUT, extra: tuple[str, ...] = ()) -> tuple[int, bytes]:
    # stderr can contain an authenticated remote URL; it is never relayed.
    done = run(["git", "-C", root, *SAFE_GIT_PREFIX, *extra, *args], timeout=timeout)
    return done.returncode, done.stdout


def git(root: str, *args: str, timeout: int = GIT_TIMEOUT, extra: tuple[str, ...] = ()) -> bytes:
    code, out = git_rc(root, *args, timeout=timeout, extra=extra)
    if code:
        raise fail(f"git {args[0]} が失敗した")
    return out


def tool(root: str, script: str, *args: str) -> bytes:
    done = run([sys.executable, "-B", str(Path(__file__).with_name(script)), "--dir", root, *args], timeout=GIT_TIMEOUT)
    if done.returncode:
        raise fail("ローカル照合が失敗した")
    return done.stdout


def precheck(root: str) -> None:
    """diff-snapshot.sh --precheck without --accept: nothing needing approval may exist."""
    script = SKILLS / "do-task/scripts/diff-snapshot.sh"
    done = run(["bash", str(script), "--cwd", root, "--precheck"], timeout=PRECHECK_TIMEOUT)
    if done.returncode or done.stdout:
        raise fail("事前検査が止めた(filter・include・textconv・インデックスの印など、承認が要る設定がある。人が取り込む)")


def nul_list(data: bytes) -> list[str]:
    parts = data.split(b"\0")
    if parts[-1] != b"":
        raise fail("git の出力が不正")
    return [os.fsdecode(part) for part in parts[:-1]]


# ---------------------------------------------------------------- argument forms

def valid_ref_name(name: str) -> bool:
    return (bool(name) and not name.startswith("/") and not name.endswith(("/", "."))
            and ".." not in name and "@{" not in name
            and not any(ord(char) < 32 or ord(char) == 127 or char in " ~^:?*[\\" for char in name))


def check_oids(**values: str) -> None:
    for name, value in values.items():
        if not OID.fullmatch(value):
            raise fail(f"--{name.replace('_', '-')} が完全な object ID でない")


def check_names(branch: str | None = None, base: str | None = None) -> None:
    if branch is not None and not valid_ref_name(branch):
        raise fail("--branch が参照の名前の形でない")
    if base is not None and not valid_ref_name(base):
        raise fail("--base が参照の名前の形でない")


def check_eres(eres: list[str]) -> None:
    if not eres or any(not value or "\0" in value or "\n" in value for value in eres):
        raise fail("--exclude-ere の形が不正")


def rev_parse(root: str, rev: str) -> str | None:
    code, out = git_rc(root, "rev-parse", "-q", "--verify", rev)
    return out.decode().strip() if code == 0 else None


def commit_exists(root: str, oid: str) -> None:
    if rev_parse(root, f"{oid}^{{commit}}") != oid:
        raise fail("commit が見つからない")


# ---------------------------------------------------------------- secret paths

def excluded_paths(paths, patterns: list[str]) -> set[str]:
    """Same rule as review-guard.py excluded_paths: GNU ERE, LC_ALL=C, ignore case, NUL set."""
    unique = sorted(set(paths))
    if not unique or not patterns:
        return set()
    if any("\0" in path for path in unique):
        raise fail("除外判定の path に NUL がある")
    expression = "|".join(f"({pattern})" for pattern in patterns)
    try:
        proc = subprocess.run(("grep", "-Eiz", "--", expression),
                              input=b"".join(os.fsencode(path) + b"\0" for path in unique),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env=dict(os.environ, LC_ALL="C"), timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise fail("機密除外 ERE を実行できない") from exc
    if proc.returncode not in (0, 1):
        raise fail("機密除外 ERE が不正")
    values = proc.stdout.split(b"\0")
    if values[-1] != b"":
        raise fail("機密除外 ERE の NUL 出力が不正")
    found = {os.fsdecode(value) for value in values[:-1]}
    if not found.issubset(set(unique)):
        raise fail("機密除外 ERE が未知の path を返した")
    return found


def secret_globs(root: str, ref: str | None = None) -> list[str]:
    command = [sys.executable, "-B", str(SKILLS / "do-task/scripts/secret-profiles.py"), "--cwd", root]
    if ref:
        command += ["--ref", ref]
    done = run(command, timeout=GIT_TIMEOUT)
    if done.returncode:
        raise fail("機密の指定(profile)を読めない")
    return nul_list(done.stdout)


def glob_to_ere(glob: str) -> str:
    script = SKILLS / "do-task/scripts/diff-snapshot.sh"
    done = run(["bash", str(script), "--print-exclude-ere", "--exclude-glob", glob], timeout=GIT_TIMEOUT)
    lines = done.stdout.decode("utf-8", "replace").splitlines()
    if done.returncode or len(lines) != 1 or not lines[0]:
        raise fail("機密の glob を ERE に変えられない")
    return lines[0]


def extend_columns(prev_globs: list[str], prev_ere: list[str], wanted: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Append only globs that are new as globs.  Never reorder or drop earlier items."""
    known = set(prev_globs)
    added: list[str] = []
    for glob in wanted:
        if glob not in known:
            known.add(glob)
            added.append(glob)
    eres = list(prev_ere) + [glob_to_ere(glob) for glob in added]
    return list(prev_globs) + added, eres, added


# ---------------------------------------------------------------- git readers

def tree_entries(root: str, rev: str) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for record in git(root, "ls-tree", "-r", "-z", "--full-tree", rev).split(b"\0")[:-1]:
        meta, _, path = record.partition(b"\t")
        mode, _kind, oid = meta.decode().split(" ")
        result[os.fsdecode(path)] = (mode, oid)
    return result


def index_entries(root: str) -> list[tuple[str, str, int, str]]:
    result = []
    for record in git(root, "ls-files", "-s", "-z", "--full-name").split(b"\0")[:-1]:
        meta, _, path = record.partition(b"\t")
        mode, oid, stage = meta.decode().split(" ")
        result.append((mode, oid, int(stage), os.fsdecode(path)))
    return result


def index_digest(root: str, skip: set[str]) -> str:
    digest = hashlib.sha256()
    for mode, oid, stage, path in index_entries(root):
        if stage == 0 and path not in skip:
            digest.update(f"{mode} {oid} {path}".encode("utf-8", "surrogateescape") + b"\0")
    return digest.hexdigest()


def unmerged_paths(root: str) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for _mode, _oid, stage, path in index_entries(root):
        if stage:
            result.setdefault(path, set()).add(stage)
    return result


def status_entries(root: str) -> list[tuple[str, str, str]]:
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no", "--ignore-submodules=dirty",
              "--no-renames", extra=FS_FLAGS)
    result = []
    for record in out.split(b"\0")[:-1]:
        text = os.fsdecode(record)
        if len(text) < 4 or text[2] != " ":
            raise fail("git status の出力が不正")
        result.append((text[0], text[1], text[3:]))
    return result


def merge_bases(root: str, d: str, b: str) -> list[str]:
    code, out = git_rc(root, "merge-base", "--all", d, b)
    if code not in (0, 1):
        raise fail("git merge-base が失敗した")
    return out.decode().split()


def raw_diff(root: str, old: str, new: str) -> dict[str, tuple[str, ...]]:
    tokens = git(root, "diff-tree", "-r", "-z", "--raw", "--no-abbrev", "--no-renames", old, new).split(b"\0")[:-1]
    if len(tokens) % 2:
        raise fail("git diff-tree の出力が不正")
    result = {}
    for index in range(0, len(tokens), 2):
        fields = tokens[index].decode().lstrip(":").split(" ")
        result[os.fsdecode(tokens[index + 1])] = tuple(fields)
    return result


def normalize_diff(text: bytes) -> bytes:
    lines = []
    for line in text.split(b"\n"):
        if line.startswith(b"index "):
            continue
        lines.append(b"@@" if line.startswith(b"@@") else line)
    return b"\n".join(lines)


def diff_body(root: str, old: str, new: str, path: str) -> bytes:
    return normalize_diff(git(root, "diff", "--text", "--no-ext-diff", "--no-textconv", "--no-color",
                              "--no-renames", "-U3", old, new, "--", path))


def verify_integration(root: str, d: str, b: str, target: str, patterns: list[str], *, parents: bool) -> list[str] | None:
    """Return None when the integration matches, else the list of differing paths (maybe empty)."""
    if parents:
        line = git(root, "rev-list", "--parents", "-n", "1", target).decode().split()
        if line[1:] != [d, b]:
            return []
    bases = merge_bases(root, d, b)
    if len(bases) != 1:
        return []
    mb = bases[0]
    work, base_side, after = raw_diff(root, mb, d), raw_diff(root, mb, b), raw_diff(root, b, target)
    bad = set(work) ^ set(after)
    overlap = []
    for path in work.keys() & after.keys():
        if path not in base_side:
            if work[path] != after[path]:
                bad.add(path)
        else:
            overlap.append(path)
    secret = excluded_paths(overlap, patterns)
    for path in overlap:
        rows = (work[path], base_side[path], after[path])
        if path in secret:
            bad.add(path)
        elif not all(row[4] == "M" and row[0] == row[1] and row[0] in ("100644", "100755") for row in rows):
            bad.add(path)
        elif diff_body(root, mb, d, path) != diff_body(root, b, target, path):
            bad.add(path)
    return sorted(bad) if bad else None


# ---------------------------------------------------------------- safe work tree reads

def open_parent(root: str, parts: list[str]) -> int:
    """Walk every parent below the root with O_NOFOLLOW directory descriptors."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_entry(root: str, rel: str) -> tuple[str, str | None, bytes | None]:
    """Return (kind, git mode, bytes) of one work tree path without converting or following links.

    kind: missing / regular / symlink / other.  A parent that is a link or a file is `other`.
    """
    parts = rel.split("/")
    try:
        parent = open_parent(root, parts)
    except OSError as exc:
        return ("missing", None, None) if exc.errno == errno.ENOENT else ("other", None, None)
    try:
        try:
            first = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return "missing", None, None
        if stat.S_ISLNK(first.st_mode):
            return "symlink", "120000", os.fsencode(os.readlink(parts[-1], dir_fd=parent))
        if not stat.S_ISREG(first.st_mode):
            return "other", None, None
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or identity(before) != identity(first) or before.st_size > FILE_LIMIT:
                return "other", None, None
            chunks, total = [], 0
            while True:
                block = os.read(fd, min(1024 * 1024, before.st_size - total + 1))
                if not block:
                    break
                total += len(block)
                if total > before.st_size:
                    return "other", None, None
                chunks.append(block)
            after = os.fstat(fd)
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if total != before.st_size or identity(after) != identity(before) or identity(named) != identity(before):
                return "other", None, None
        finally:
            os.close(fd)
        return "regular", "100755" if first.st_mode & 0o100 else "100644", b"".join(chunks)
    except OSError:
        return "other", None, None
    finally:
        os.close(parent)


def read_json_file(path: str, expected_sha256: str, label: str) -> dict:
    if not SHA256_HEX.fullmatch(expected_sha256):
        raise fail(f"{label}の SHA-256 の形でない")
    full = os.path.abspath(path)
    try:
        parent = os.open(os.path.realpath(os.path.dirname(full)), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise fail(f"{label}を読めない") from exc
    try:
        name = os.path.basename(full)
        first = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(first.st_mode) or first.st_size > JSON_LIMIT:
            raise fail(f"{label}が通常ファイルでない")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if identity(before) != identity(first):
                raise fail(f"{label}が読取り前に変わった")
            data = os.read(fd, JSON_LIMIT + 1) if before.st_size else b""
            while len(data) < before.st_size:
                more = os.read(fd, before.st_size - len(data) + 1)
                if not more:
                    break
                data += more
            if len(data) != before.st_size or identity(os.fstat(fd)) != identity(before):
                raise fail(f"{label}の読取り中に変わった")
        finally:
            os.close(fd)
    except OSError as exc:
        raise fail(f"{label}を読めない") from exc
    finally:
        os.close(parent)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise fail(f"{label}の SHA-256 が保持した値と合わない")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise fail(f"{label}を読めない") from exc
    if not isinstance(value, dict):
        raise fail(f"{label}の形が不正")
    return value


def str_list(value: dict, key: str, label: str) -> list[str]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise fail(f"{label}の {key} の形が不正")
    return items


def content_mismatches(root: str, head: str, patterns: list[str], skip: set[str]) -> list[str]:
    """Compare work tree bytes with HEAD blobs in Python, never through a Git filter."""
    entries = tree_entries(root, head)
    algo = git(root, "rev-parse", "--show-object-format").decode().strip()
    if algo not in ("sha1", "sha256"):
        raise fail("object format を確認できない")
    secret = excluded_paths(entries, patterns)
    bad = []
    for path in sorted(entries):
        mode, oid = entries[path]
        if path in secret or path in skip or mode == "160000":
            continue
        kind, found_mode, data = read_entry(root, path)
        if kind not in ("regular", "symlink") or found_mode != mode or data is None:
            bad.append(path)
            continue
        if hashlib.new(algo, b"blob %d\0" % len(data) + data).hexdigest() != oid:
            bad.append(path)
    return bad


def require_clean_content(root: str, head: str, patterns: list[str]) -> None:
    bad = content_mismatches(root, head, patterns, set())
    if bad:
        raise fail("作業ツリーの中身が HEAD と違う(改行の変換・LFS の実体など)", problem_paths=bad)


# ---------------------------------------------------------------- shared checks

def head_checked(root: str, branch: str, reviewed: str) -> None:
    code, out = git_rc(root, "symbolic-ref", "--quiet", "HEAD")
    if code or out.decode().strip() != f"refs/heads/{branch}":
        raise fail("今のブランチが固定ブランチでない")
    if rev_parse(root, f"refs/heads/{branch}") != reviewed or rev_parse(root, "HEAD") != reviewed:
        raise fail("固定ブランチがレビュー済み SHA と一致しない")


def merge_head(root: str) -> str | None:
    return rev_parse(root, "MERGE_HEAD")


def tracked_clean(root: str, *, unstaged_only: bool = False) -> bool:
    return not [entry for entry in status_entries(root) if not unstaged_only or entry[1] != " "]


def origin_checked(root: str, repo: str) -> None:
    try:
        data = json.loads(tool(root, "origin-repo.py"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise fail("origin の判定を読めない") from exc
    if not (isinstance(data, dict) and data.get("origin") is True and data.get("same") is True
            and data.get("vcs") is False and data.get("repo") == repo):
        raise fail("origin の送信先が固定リポジトリと一致しない")


def secret_index_check(root: str, d: str, b: str, patterns: list[str]) -> list[str]:
    """Index entries of secret paths must equal D's or B's entry (nothing reviewable is hidden there)."""
    tree_d, tree_b = tree_entries(root, d), tree_entries(root, b)
    entries = index_entries(root)
    stages: dict[str, list[tuple[str, str, int]]] = {}
    for mode, oid, stage, path in entries:
        stages.setdefault(path, []).append((mode, oid, stage))
    secret = excluded_paths(set(tree_d) | set(tree_b) | set(stages), patterns)
    bad = []
    for path in sorted(secret):
        rows = stages.get(path, [])
        if any(stage for _m, _o, stage in rows):
            bad.append(path)
            continue
        now = (rows[0][0], rows[0][1]) if rows else None
        if now != tree_d.get(path) and now != tree_b.get(path):
            bad.append(path)
    return bad


def marker_paths(root: str, paths: list[str]) -> list[str]:
    found = []
    for path in paths:
        kind, _mode, data = read_entry(root, path)
        if kind == "regular" and MARKERS.search(data or b""):
            found.append(path)
    return found


def merge_result_loaded(args: argparse.Namespace) -> dict:
    result = read_json_file(args.merge_result, args.expect_merge_result_sha256, "merge の結果のファイル")
    if (result.get("base_sha") != args.base_sha or result.get("reviewed_sha") != args.reviewed_sha
            or result.get("result") != "needs-review"):
        raise fail("merge の結果のファイルが今の取り込みと合わない")
    for key in ("problem_paths", "binary_conflicts", "exclude_ere"):
        str_list(result, key, "merge の結果のファイル")
    if not isinstance(result.get("index_sha256"), str):
        raise fail("merge の結果のファイルに index_sha256 が無い")
    missing = [value for value in result["exclude_ere"] if value not in args.exclude_ere]
    if missing:
        raise fail("--exclude-ere が merge の結果の exclude_ere を含まない(機密の指定を落とせない)")
    return result


def resolution_preconditions(args: argparse.Namespace) -> dict:
    check_oids(reviewed_sha=args.reviewed_sha, base_sha=args.base_sha)
    check_names(branch=args.branch)
    check_eres(args.exclude_ere)
    result = merge_result_loaded(args)
    STATE.update(base_sha=args.base_sha, reviewed_sha=args.reviewed_sha, exclude_ere=list(args.exclude_ere),
                 exclude_globs=result.get("exclude_globs") if isinstance(result.get("exclude_globs"), list) else [])
    precheck(args.dir)
    head_checked(args.dir, args.branch, args.reviewed_sha)
    if merge_head(args.dir) != args.base_sha:
        raise fail("MERGE_HEAD が取り込み先の commit でない")
    return result


def kind_problems(root: str, result: dict) -> None:
    if result["binary_conflicts"]:
        raise fail("印の無い衝突(バイナリなど)は人が解く", problem_paths=list(result["binary_conflicts"]))
    bad = [path for path in result["problem_paths"] if read_entry(root, path)[0] not in ("missing", "regular")]
    if bad:
        raise fail("衝突したパスがリンク・フォルダなど通常ファイルでない", problem_paths=bad)


# ---------------------------------------------------------------- merge

def abort_if_merging(root: str) -> None:
    if merge_head(root) is None:
        return
    try:
        code, _ = git_rc(root, "merge", "--abort", timeout=NET_TIMEOUT, extra=MERGE_FLAGS)
    except Stop:
        code = -1
    if code:
        raise fail("取り込みの途中の状態が残った")


def name_collisions(root: str, d: str, b: str, bases: list[str]) -> list[str]:
    tree_d, tree_b = tree_entries(root, d), tree_entries(root, b)
    tree_bases = [tree_entries(root, base) for base in bases]
    found = []
    for path in sorted(tree_b):
        if path in tree_d or not any(tb.get(path) != tree_b[path] for tb in tree_bases):
            continue
        current = root
        parts = path.split("/")
        for position, part in enumerate(parts):
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                break
            except OSError:
                found.append(path)
                break
            if position == len(parts) - 1 or not stat.S_ISDIR(info.st_mode):
                found.append(path)
                break
    return found


def needs_review(root: str, reason: str, d: str, b: str, bases: list[str], overlap: list[str],
                 problem: list[str], binary: list[str]) -> Stop:
    return Stop(3, "取り込みの途中で止めた(レビューが要る)", result="needs-review", reason=reason, head=d,
                merge_base=bases[0] if len(bases) == 1 else None, overlap=overlap, problem_paths=problem,
                binary_conflicts=binary, index_sha256=index_digest(root, set(problem)))


def commit_merge(root: str, branch: str, d: str, b: str, patterns: list[str], result: dict) -> str:
    """Create M for a prepared merge.  Used by `commit` and by a clean `merge`."""
    bad = secret_index_check(root, d, b, patterns)
    if bad:
        raise Stop(4, "機密のパスにレビューも照合もできない変更が入る", result="secret-overlap", problem_paths=bad)
    problem = list(result["problem_paths"])
    secret_problem = sorted(excluded_paths(problem, patterns))
    if secret_problem:
        raise fail("衝突したパスが機密の除外に当たる(中身は読まない)", problem_paths=secret_problem)
    if unmerged_paths(root):
        raise fail("衝突の項目が残っている", problem_paths=sorted(unmerged_paths(root)))
    if not tracked_clean(root, unstaged_only=True):
        raise fail("追跡しているファイルにステージしていない変更がある")
    if index_digest(root, set(problem)) != result["index_sha256"]:
        raise fail("衝突の外のインデックスが merge の直後と違う")
    marked = marker_paths(root, problem)
    if marked:
        raise fail("衝突の印が残っている", problem_paths=marked)
    if result.get("reason") == "conflict":
        kind_problems(root, result)
    tree = git(root, "write-tree").decode().strip()
    git(root, "commit", "--no-edit", "--cleanup=strip", timeout=NET_TIMEOUT, extra=MERGE_FLAGS)
    head = rev_parse(root, "HEAD")
    code, ref = git_rc(root, "symbolic-ref", "--quiet", "HEAD")
    parents = git(root, "rev-list", "--parents", "-n", "1", "HEAD").decode().split()
    if (head is None or code or ref.decode().strip() != f"refs/heads/{branch}" or parents[1:] != [d, b]
            or rev_parse(root, "HEAD^{tree}") != tree):
        raise fail("取り込みの commit の親か tree が想定と違う")
    return head


def merge_driver_refused(root: str) -> None:
    """A configured merge driver would run arbitrary local code inside `git merge`."""
    code, out = git_rc(root, "config", "--get-regexp", r"^merge\.")
    if code not in (0, 1):
        raise fail("git config を読めない")
    for line in out.decode("utf-8", "replace").splitlines():
        key = line.split(" ", 1)[0].lower()
        if key == "merge.default" or (key.startswith("merge.") and key.endswith(".driver")):
            raise fail("merge driver の設定がある(人が取り込む)")


def cmd_merge(args: argparse.Namespace) -> int:
    root, branch, d, base = args.dir, args.branch, args.reviewed_sha, args.base
    patterns = list(args.exclude_ere)
    # 1-5: nothing is written yet
    check_oids(reviewed_sha=d)
    check_names(branch=branch, base=base)
    check_eres(patterns)
    if not REPO.fullmatch(args.repo):
        raise fail("--repo が host/owner/name の形でない")
    STATE.update(base=base, reviewed_sha=d, exclude_ere=patterns)
    precheck(root)
    head_checked(root, branch, d)
    merge_driver_refused(root)
    if merge_head(root) is not None:
        raise fail("途中の merge が残っている")
    if not tracked_clean(root):
        raise fail("追跡しているファイルに未 commit の変更がある")
    require_clean_content(root, d, patterns)
    # 6-7
    origin_checked(root, args.repo)
    git(root, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "origin",
        f"+refs/heads/{base}:refs/remotes/origin/{base}", timeout=NET_TIMEOUT, extra=MERGE_FLAGS)
    b = rev_parse(root, f"refs/remotes/origin/{base}^{{commit}}")
    if b is None:
        raise fail("取り込み先の最新を取れない")
    STATE["base_sha"] = b
    globs, patterns, added = extend_columns(secret_globs(root), patterns, secret_globs(root, b))
    STATE.update(exclude_ere=patterns, exclude_globs=globs, added_globs=added)
    # 8-9
    code, _ = git_rc(root, "merge-base", "--is-ancestor", b, d)
    if code == 0:
        return finish(0, "up-to-date", head=d, merge_base=b)
    if code != 1:
        raise fail("git merge-base が失敗した")
    code, _ = git_rc(root, "merge-base", "--is-ancestor", d, b)
    if code == 0:
        raise fail("作業が取り込み先に含まれている(PR の差分が空になる)")
    if code != 1:
        raise fail("git merge-base が失敗した")
    # 10-12
    bases = merge_bases(root, d, b)
    if not bases:
        raise fail("共通の祖先が無い")
    work: set[str] = set()
    theirs: set[str] = set()
    for base_commit in bases:
        work |= set(raw_diff(root, base_commit, d))
        theirs |= set(raw_diff(root, base_commit, b))
    overlap = sorted(work & theirs)
    secret = sorted(excluded_paths(overlap, patterns))
    if secret:
        raise Stop(4, "機密のパスを作業と取り込み先の両方が変えた", result="secret-overlap", problem_paths=secret,
                   merge_base=bases[0] if len(bases) == 1 else None)
    collisions = name_collisions(root, d, b, bases)
    if collisions:
        raise fail("取り込み先が足すファイルと同じ名前のものが作業ツリーにある", problem_paths=collisions)
    # 13
    message = f"Merge remote-tracking branch 'origin/{base}' into {branch}"
    try:
        code, _ = git_rc(root, "merge", "--no-ff", "--no-commit", "--no-squash", "--no-autostash",
                         "--no-verify-signatures", "-m", message, b, timeout=NET_TIMEOUT, extra=MERGE_FLAGS)
        timed_out = False
    except Stop:
        code, timed_out = -1, True  # timed out or could not start: look at what was left behind
    if merge_head(root) is None:
        if (not timed_out and rev_parse(root, "HEAD") == d and rev_parse(root, f"refs/heads/{branch}") == d and tracked_clean(root)):
            raise fail("取り込みを始められない(作業ツリーは変わっていない)")
        raise fail("取り込みの途中の状態が残った")
    try:
        if timed_out:
            raise fail("取り込みの途中で失敗した")
        if merge_head(root) != b or (code and not unmerged_paths(root)):
            raise fail("merge の状態が想定と違う")
        # 14-15
        precheck(root)
        bad = secret_index_check(root, d, b, patterns)
        if bad:
            raise Stop(4, "機密のパスにレビューも照合もできない変更が入る", result="secret-overlap", problem_paths=bad,
                       merge_base=bases[0] if len(bases) == 1 else None)
        # 16-18
        conflicts = unmerged_paths(root)
        if conflicts:
            binary = []
            for path in sorted(conflicts):
                if {2, 3} <= conflicts[path]:
                    kind, _mode, data = read_entry(root, path)
                    if kind != "regular" or not MERGE_MARKER.search(data or b""):
                        binary.append(path)
            review = needs_review(root, "conflict", d, b, bases, overlap, sorted(conflicts), binary)
        elif len(bases) != 1:
            review = needs_review(root, "multiple-merge-bases", d, b, bases, overlap, [], [])
        else:
            tree = git(root, "write-tree").decode().strip()
            mismatch = verify_integration(root, d, b, tree, patterns, parents=False)
            review = (needs_review(root, "diff-changed", d, b, bases, overlap, mismatch, [])
                           if mismatch is not None else None)
        if review is not None:
            raise review
        # 19
        clean = {"reason": "clean", "problem_paths": [], "binary_conflicts": [], "index_sha256": index_digest(root, set())}
        head = commit_merge(root, branch, d, b, patterns, clean)
    except Stop as stop:
        if stop.code == 3:
            raise
        abort_if_merging(root)
        raise
    except BaseException as exc:
        abort_if_merging(root)
        raise fail("取り込みの途中で失敗した") from exc
    return finish(0, "merged", head=head, merge_base=bases[0], overlap=overlap)


# ---------------------------------------------------------------- stage-resolved / commit

def cmd_stage_resolved(args: argparse.Namespace) -> int:
    root = args.dir
    result = resolution_preconditions(args)
    patterns, problem = list(args.exclude_ere), list(result["problem_paths"])
    if result["binary_conflicts"]:
        raise fail("印の無い衝突(バイナリなど)は人が解く", problem_paths=list(result["binary_conflicts"]))
    secret = sorted(excluded_paths(problem, patterns))
    if secret:
        raise fail("衝突したパスが機密の除外に当たる", problem_paths=secret)
    plan: list[tuple[str, str]] = []
    marked = []
    for path in problem:
        kind, _mode, data = read_entry(root, path)
        if kind == "missing":
            plan.append(("remove", path))
        elif kind == "regular":
            if MARKERS.search(data or b""):
                marked.append(path)
            plan.append(("add", path))
        else:
            raise fail("衝突したパスがリンク・フォルダなど通常ファイルでない", problem_paths=[path])
    if marked:
        raise fail("衝突の印が残っている", problem_paths=marked)
    if index_digest(root, set(problem)) != result["index_sha256"]:
        raise fail("衝突の外のインデックスが merge の直後と違う")
    outside = [path for _x, y, path in status_entries(root) if y != " " and path not in set(problem)]
    if outside:
        raise fail("衝突の外にステージしていない変更がある", problem_paths=sorted(set(outside)))
    for action, path in plan:
        if action == "add":
            git(root, "add", "--", path, extra=FS_FLAGS)
        else:
            git(root, "update-index", "--force-remove", "--", path)
    if unmerged_paths(root):
        raise fail("衝突の項目が残っている", problem_paths=sorted(unmerged_paths(root)))
    return finish(0, "resolved", head=args.reviewed_sha, problem_paths=problem)


def cmd_commit(args: argparse.Namespace) -> int:
    result = resolution_preconditions(args)
    STATE["merge_base"] = result.get("merge_base")
    head = commit_merge(args.dir, args.branch, args.reviewed_sha, args.base_sha, list(args.exclude_ere), result)
    overlap = result.get("overlap") if isinstance(result.get("overlap"), list) else []
    return finish(0, "committed", head=head, overlap=overlap, problem_paths=list(result["problem_paths"]))


# ---------------------------------------------------------------- check / exclude / worktree

def cmd_check(args: argparse.Namespace) -> int:
    check_oids(sha=args.sha, reviewed_sha=args.reviewed_sha, base_sha=args.base_sha)
    check_eres(args.exclude_ere)
    STATE.update(base_sha=args.base_sha, reviewed_sha=args.reviewed_sha, head=args.sha,
                 exclude_ere=list(args.exclude_ere))
    precheck(args.dir)
    for oid in (args.sha, args.reviewed_sha, args.base_sha):
        commit_exists(args.dir, oid)
    mismatch = verify_integration(args.dir, args.reviewed_sha, args.base_sha, args.sha, list(args.exclude_ere),
                                  parents=True)
    if mismatch is not None:
        raise Stop(1, "取り込みの照合が合わない", result="mismatch", problem_paths=mismatch)
    return finish(0, "match")


def cmd_exclude(args: argparse.Namespace) -> int:
    root = args.dir
    check_oids(reviewed_sha=args.reviewed_sha, base_sha=args.base_sha)
    STATE.update(base_sha=args.base_sha, reviewed_sha=args.reviewed_sha)
    precheck(root)
    previous = read_json_file(args.previous, args.expect_previous_sha256, "--previous のファイル")
    if previous.get("reviewed_sha") != args.reviewed_sha or previous.get("base_sha") != args.base_sha:
        raise fail("--previous が今の取り込みと合わない")
    prev_globs = str_list(previous, "exclude_globs", "--previous のファイル")
    prev_ere = str_list(previous, "exclude_ere", "--previous のファイル")
    commit_exists(root, args.base_sha)
    globs, eres, added = extend_columns(prev_globs, prev_ere, secret_globs(root, args.base_sha))
    STATE.update(exclude_globs=globs, exclude_ere=eres, added_globs=added)
    if added:
        new_eres = eres[len(prev_ere):]
        head = tree_entries(root, "HEAD")
        tree_d, tree_b = tree_entries(root, args.reviewed_sha), tree_entries(root, args.base_sha)
        hit = excluded_paths(set(head) | set(tree_d) | set(tree_b), new_eres)
        bad = sorted(path for path in hit
                     if head.get(path) != tree_d.get(path) and head.get(path) != tree_b.get(path))
        if bad:
            raise Stop(4, "後から機密に当たったパスに、レビューも照合もできない中身が入っている",
                       result="secret-overlap", problem_paths=bad)
    return finish(0, "excluded", head=rev_parse(root, "HEAD"))


def cmd_worktree(args: argparse.Namespace) -> int:
    root, patterns = args.dir, list(args.exclude_ere)
    check_oids(head=args.head)
    check_names(branch=args.branch)
    check_eres(patterns)
    STATE.update(head=args.head, exclude_ere=patterns)
    precheck(root)
    head_checked_loose = git_rc(root, "symbolic-ref", "--quiet", "HEAD")
    if head_checked_loose[0] or head_checked_loose[1].decode().strip() != f"refs/heads/{args.branch}":
        raise fail("今のブランチが固定ブランチでない")
    if rev_parse(root, "HEAD") != args.head:
        raise fail("HEAD が --head と違う")
    if merge_head(root) is not None:
        raise fail("途中の merge が残っている")
    if git(root, "diff-index", "--cached", "--name-only", "-z", "HEAD"):
        raise fail("ステージした変更がある")
    changed = sorted({path for _x, _y, path in status_entries(root)})
    untracked = [path for path in nul_list(git(root, "ls-files", "-o", "--exclude-standard", "-z", "--full-name",
                                                extra=FS_FLAGS)) if not STATE_FILES.search(path)]
    allowed = set(args.allow_path)
    baseline = None
    if args.baseline is not None:  # validate the baseline before any file content is read
        baseline = read_json_file(args.baseline, args.expect_baseline_sha256, "--baseline のファイル")
        if baseline.get("head") != args.head:
            raise fail("基準の head が --head と違う")
        if any(value not in patterns for value in str_list(baseline, "exclude_ere", "基準のファイル")):
            raise fail("--exclude-ere が基準の exclude_ere を含まない(機密の指定を落とせない)")
    bad = content_mismatches(root, "HEAD", patterns, allowed)
    STATE.update(changed_paths=changed, untracked=untracked)
    if bad:
        raise fail("作業ツリーの中身が HEAD と違う", problem_paths=bad)
    if baseline is None:
        if changed:
            raise fail("追跡しているファイルに変更がある", problem_paths=changed)
        return finish(0, "ok")
    before = set(str_list(baseline, "untracked", "基準のファイル"))
    fresh = sorted(set(untracked) - before)
    outside = sorted((set(fresh) | set(changed)) - allowed)
    if outside:
        raise fail("許していないパスが変わっている", problem_paths=outside)
    secret = sorted(excluded_paths(set(fresh) | set(changed), patterns))
    if secret:
        raise fail("機密のパスが変わっている", problem_paths=secret)
    gone = sorted(before - set(untracked))
    if gone:
        raise fail("基準にあった未追跡のパスが消えている", problem_paths=gone)
    return finish(0, "ok")


# ---------------------------------------------------------------- entry

def finish(code: int, result: str, reason: str | None = None, **fields) -> int:
    emit(result, reason, fields)
    return code


def emit(result: str, reason: str | None, fields: dict) -> None:
    data = {key: ([] if key in LIST_FIELDS else None) for key in FIELDS}
    data.update(STATE)
    data.update({key: value for key, value in fields.items() if value is not None or key in ("merge_base",)})
    data["result"], data["reason"] = result, reason
    sys.stdout.write(json.dumps(data, ensure_ascii=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, prog="integrate-guard.py")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, *required: str, optional: tuple[str, ...] = ()) -> argparse.ArgumentParser:
        item = sub.add_parser(name)
        item.set_defaults(handler=handler)
        for option in required:
            item.add_argument(f"--{option}", required=True)
        for option in optional:
            item.add_argument(f"--{option}")
        return item

    merge = add("merge", cmd_merge, "dir", "branch", "reviewed-sha", "base", "repo")
    merge.add_argument("--exclude-ere", action="append", required=True)
    for name, handler in (("stage-resolved", cmd_stage_resolved), ("commit", cmd_commit)):
        item = add(name, handler, "dir", "branch", "reviewed-sha", "base-sha", "merge-result",
                   "expect-merge-result-sha256")
        item.add_argument("--exclude-ere", action="append", required=True)
    check = add("check", cmd_check, "dir", "sha", "reviewed-sha", "base-sha")
    check.add_argument("--exclude-ere", action="append", required=True)
    worktree = add("worktree", cmd_worktree, "dir", "branch", "head", optional=("baseline", "expect-baseline-sha256"))
    worktree.add_argument("--exclude-ere", action="append", required=True)
    worktree.add_argument("--allow-path", action="append", default=[])
    add("exclude", cmd_exclude, "dir", "reviewed-sha", "base-sha", "previous", "expect-previous-sha256")
    return parser


def main() -> int:
    parser = build_parser()
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.command == "worktree" and (args.baseline is None) != (args.expect_baseline_sha256 is None):
        parser.error("--baseline と --expect-baseline-sha256 は一緒に渡す")
    if not os.path.isdir(args.dir):
        print("usage: --dir がディレクトリでない", file=sys.stderr)
        return 2
    args.dir = os.path.realpath(args.dir)
    raw_timeout = os.environ.get("INTEGRATE_GUARD_NET_TIMEOUT")
    if raw_timeout is not None:
        if not re.fullmatch(r"[0-9]{1,3}", raw_timeout) or not 1 <= int(raw_timeout) <= 120:
            print("usage: INTEGRATE_GUARD_NET_TIMEOUT は 1〜120 の整数", file=sys.stderr)
            return 2
        global NET_TIMEOUT
        NET_TIMEOUT = int(raw_timeout)
    try:
        return args.handler(args)
    except Stop as stop:
        print(stop.message, file=sys.stderr)
        emit(stop.result, stop.reason, stop.fields)
        return stop.code
    except (OSError, ValueError, KeyError, IndexError, UnicodeError):
        print("予期しない失敗(診断は出さない)", file=sys.stderr)
        emit("error", None, {})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
