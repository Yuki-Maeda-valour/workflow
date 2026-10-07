#!/usr/bin/env python3
"""レビュー・検証結果・最終 task・commit の集合を照合する保護器。

start → take → snapshot 生成 → seal → run-checks / review → attest → finalize → verify。
実装、文書、未承認保留を別 phase とし、外部本文の正本には caller の fresh な控えを使う。
state と直接結果の sha256 は検証担当がセッションに保持する。同一 UID の完全隔離ではない。
詳細な呼出契約は do-task/references/review-protocol.md。
"""

from __future__ import annotations

import argparse
import base64
import datetime
import importlib.util
import re
import hashlib
import json
import math
import os
import stat
import secrets
import shutil
import time
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


SAFE_GIT = (
    "--no-pager", "--no-replace-objects", "-c", "core.quotePath=false",
    "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
    "-c", "core.ignoreCase=false", "-c", "core.splitIndex=false",
    "-c", "core.filemode=true", "-c", "core.symlinks=true",
)
TASK_PREFIXES = ("進行中_", "完了_", "保留_", "中断_", "候補_")
DEFAULT_EXCLUDE = r"(^|/)\.claude/(reviews(/|$)|grasp\.md$|settings\.local\.json$|\.understand-project-done$)"


TASK_SOURCE: dict = {}
TASK_ROOT: Path | None = None


class GuardError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_READ_SECONDS = 15


def open_directory(path: Path) -> int:
    """各親を dirfd に固定する。走査後に親が symlink へ変わっても辿らない。"""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise GuardError("親移動を含むパスを受け付けない")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def same_directory(path: Path, held_fd: int) -> None:
    fresh = open_directory(path)
    try:
        old, now = os.fstat(held_fd), os.fstat(fresh)
        if (old.st_dev, old.st_ino) != (now.st_dev, now.st_ino):
            raise GuardError("親ディレクトリが差し替わった")
    finally:
        os.close(fresh)


def safe_lstat(path: Path) -> os.stat_result:
    fd = open_directory(path.parent)
    try:
        return os.stat(path.name, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)


def safe_readlink(path: Path) -> str:
    fd = open_directory(path.parent)
    try:
        return os.readlink(path.name, dir_fd=fd)
    finally:
        os.close(fd)


def regular(path: Path, label: str) -> None:
    try:
        info = safe_lstat(path)
    except OSError as exc:
        raise GuardError(f"{label}を検査できない: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise GuardError(f"{label}が通常ファイルではない: {path}")


def read_regular(path: Path, label: str) -> bytes:
    parent_fd = None
    fd = None
    try:
        parent_fd = open_directory(path.parent)
        initial = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(initial.st_mode):
            raise GuardError(f"{label}が通常ファイルではない: {path}")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        before = os.fstat(fd)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if not stat.S_ISREG(before.st_mode) or identity(initial) != identity(before):
            raise GuardError(f"{label}が読取り前に変わった")
        if before.st_size > MAX_FILE_BYTES:
            raise GuardError(f"{label}が読取上限を超える")
        chunks, total = [], 0
        deadline = time.monotonic() + MAX_READ_SECONDS
        while True:
            if time.monotonic() > deadline:
                raise GuardError(f"{label}の読取時間上限")
            # 初期サイズ + 1 byte だけ読む。EOF の来ない増大ファイルでも有限で止まる。
            block = os.read(fd, min(1024 * 1024, before.st_size - total + 1))
            if not block:
                break
            total += len(block)
            if total > before.st_size:
                raise GuardError(f"{label}が読取り中に増大した")
            chunks.append(block)
        after = os.fstat(fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if total != before.st_size or identity(after) != identity(before) or identity(named) != identity(before):
            raise GuardError(f"{label}の読取り中に差し替わった")
        same_directory(path.parent, parent_fd)
        return b"".join(chunks)
    except OSError as exc:
        raise GuardError(f"{label}を安全に読めない: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)


def create_bytes(path: Path, raw: bytes) -> None:
    if len(raw) > MAX_FILE_BYTES:
        raise GuardError("出力が読取上限を超える")
    parent_fd = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        same_directory(path.parent, parent_fd)
    finally:
        os.close(parent_fd)


def git(cwd: Path, *args: str, input_data: bytes | None = None, binary: bool = True, missing_ok: bool = False) -> bytes:
    env = dict(os.environ)
    env["GIT_NO_LAZY_FETCH"] = "1"
    options: dict[str, object] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": env,
                                  "timeout": 30, "check": False}
    if input_data is None:
        options["stdin"] = subprocess.DEVNULL
    else:
        options["input"] = input_data
    try:
        proc = subprocess.run(
        ("git", "-C", str(cwd), *SAFE_GIT, *args), **options,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError("git を安全に実行できない") from exc
    if missing_ok and proc.returncode == 1:
        return b""
    if proc.returncode:
        raise GuardError(f"git {' '.join(args[:2]) or 'command'} が失敗")
    return proc.stdout


def repo_root(cwd: Path) -> Path:
    root = Path(git(cwd, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if cwd.resolve() != root and root not in cwd.resolve().parents:
        raise GuardError("管理ルートが git worktree の外")
    return root


def rel_path(root: Path, candidate: Path, label: str) -> str:
    try:
        rel = candidate.absolute().relative_to(root)
    except ValueError as exc:
        raise GuardError(f"{label}が管理ルートの外: {candidate}") from exc
    if any(part in ("", ".", "..") for part in rel.parts):
        raise GuardError(f"{label}のパスが不正: {candidate}")
    return rel.as_posix()


def real_under_root(root: Path, candidate: Path, label: str) -> None:
    """最終要素と親の解決先を確認し、task/review の親 symlink で外へ出ない。"""
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise GuardError(f"{label}の親を解決できない") from exc
    if resolved_parent != root and root not in resolved_parent.parents:
        raise GuardError(f"{label}の親が管理ルートの外")


def nul_fields(data: bytes, count: int | None = None) -> list[list[bytes]]:
    values = data.split(b"\0")
    if values[-1] != b"":
        raise GuardError("git の NUL 出力が壊れている")
    values.pop()
    if count is None:
        return [[value] for value in values]
    if len(values) % count:
        raise GuardError("git の NUL 出力の列数が不正")
    return [values[i:i + count] for i in range(0, len(values), count)]


def decode_path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8", "surrogateescape")
    except UnicodeError as exc:
        raise GuardError("パスを復号できない") from exc
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or value in ("", "."):
        raise GuardError("git が不正なパスを返した")
    return value


def excluded_paths(paths: list[str], patterns: list[str]) -> set[str]:
    """diff-snapshot と同じ GNU ERE/LC_ALL=C/ignore-case で NUL 集合を一度に分類する。"""
    if len(paths) != len(set(paths)):
        raise GuardError("除外判定の path 集合に重複がある")
    if any("\0" in path for path in paths):
        raise GuardError("除外判定の path に NUL がある")
    expression = "|".join(f"({pattern})" for pattern in patterns)
    env = dict(os.environ, LC_ALL="C")
    try:
        proc = subprocess.run(("grep", "-Eiz", "--", expression), input=b"".join(os.fsencode(path) + b"\0" for path in paths),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError("機密除外 ERE を実行できない") from exc
    if proc.returncode not in (0, 1):
        raise GuardError("機密除外 ERE が不正")
    values = proc.stdout.split(b"\0")
    if not values or values[-1] != b"":
        raise GuardError("機密除外 ERE の NUL 出力が不正")
    found = {decode_path(value) for value in values[:-1]}
    if not found.issubset(set(paths)):
        raise GuardError("機密除外 ERE が未知の path を返した")
    return found


def entry(path: Path, root: Path, secret: bool) -> dict[str, str]:
    try:
        info = safe_lstat(path)
    except OSError as exc:
        raise GuardError(f"作業ツリーの path を検査できない: {exc}") from exc
    item: dict[str, str] = {"path": rel_path(root, path, "作業ツリー"), "mode": oct(info.st_mode & 0o7777)}
    if stat.S_ISREG(info.st_mode):
        item["kind"] = "file"
        if not secret:
            body = read_regular(path, "作業ツリーの通常ファイル")
            item["sha256"] = sha256(body)
            item["oid"] = git(root, "hash-object", "--stdin", input_data=body).decode().strip()
    elif stat.S_ISLNK(info.st_mode):
        item["kind"] = "symlink"
        try:
            if not secret:
                link = safe_readlink(path)
                item["link"] = link
                item["oid"] = git(root, "hash-object", "--stdin", input_data=os.fsencode(link)).decode().strip()
        except OSError as exc:
            raise GuardError(f"symlink を読めない: {exc}") from exc
    elif stat.S_ISDIR(info.st_mode):
        item["kind"] = "dir"
    else:
        raise GuardError(f"FIFO・device・socket を review 集合に含められない: {item['path']}")
    return item


def walk(root: Path, patterns: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    result: list[dict[str, str]] = []
    excluded_entries: list[dict[str, str]] = []
    ignored_paths = set(ignored(root))
    index_items, excluded_items = git_index(root, patterns)
    gitlinks = {item["path"]: item for item in index_items + excluded_items if item["mode"] == "160000"}
    stack = [root]
    raw: list[Path] = []
    while stack:
        current = stack.pop()
        try:
            directory_fd = open_directory(current)
            try:
                with os.scandir(directory_fd) as stream:
                    children = sorted((child.name for child in stream), reverse=True)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise GuardError(f"作業ツリーを走査できない: {exc}") from exc
        for child in children:
            if child == ".git" and current == root:
                continue
            path = current / child
            info = safe_lstat(path)
            raw.append(path)
            if stat.S_ISDIR(info.st_mode) and rel_path(root, path, "走査") not in gitlinks and not excluded_paths([rel_path(root, path, "走査") + "/"], [DEFAULT_EXCLUDE]):
                stack.append(path)
    rels = [rel_path(root, path, "作業ツリー") for path in raw]
    secret_paths = excluded_paths(rels, patterns)
    state_paths = excluded_paths(rels, [DEFAULT_EXCLUDE])
    for path, rel in zip(raw, rels):
        # reviews/grasp 等は揮発 state。guard 自身の manifest/log で集合を変えない。
        if rel in state_paths:
            continue
        ignored_content = rel in ignored_paths and path.name not in (".gitignore", ".gitattributes")
        item = entry(path, root, rel in secret_paths or ignored_content)
        if ignored_content and rel not in secret_paths:
            item["ignored"] = True
        if rel in gitlinks:
            if item["kind"] != "dir":
                raise GuardError("gitlink の作業パスがディレクトリでない")
            item["kind"] = "gitlink"
            if rel not in secret_paths:
                item["oid"] = (git(path, "rev-parse", "HEAD").decode().strip()
                               if (path / ".git").exists() else gitlinks[rel]["oid"])
        if rel in secret_paths:
            excluded_entries.append(item)
        else:
            result.append(item)
    present = set(rels)
    for rel, item in gitlinks.items():
        if rel in present:
            continue
        secret = rel in excluded_paths([rel], patterns)
        value = {"path": rel, "kind": "gitlink", "mode": "0o755"}
        if not secret:
            value["oid"] = item["oid"]
        (excluded_entries if secret else result).append(value)
    return sorted(result, key=lambda x: x["path"]), sorted(excluded_entries, key=lambda x: x["path"])


def git_index(cwd: Path, patterns: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    output = git(cwd, "ls-files", "--stage", "-z")
    entries: list[dict[str, str]] = []
    excluded_entries: list[dict[str, str]] = []
    raw_items: list[dict[str, str]] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        try:
            left, name = raw.split(b"\t", 1)
            mode, oid, stage = left.split()
        except ValueError as exc:
            raise GuardError("index 出力を読めない") from exc
        item = {"path": decode_path(name), "mode": mode.decode(), "oid": oid.decode(), "stage": stage.decode()}
        raw_items.append(item)
    secret_paths = excluded_paths([item["path"] for item in raw_items], patterns)
    for item in raw_items:
        if item["path"] in secret_paths:
            # 内容は読まず表示にも出さない。既存 Git object の OID だけを内部比較に残す。
            item["_guard_oid"] = item.pop("oid")
            excluded_entries.append(item)
        else:
            entries.append(item)
    return sorted(entries, key=lambda x: (x["path"], x["stage"])), sorted(excluded_entries, key=lambda x: (x["path"], x["stage"]))


def tree(cwd: Path, ref: str, patterns: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    output = git(cwd, "ls-tree", "-r", "-z", ref)
    entries: list[dict[str, str]] = []
    excluded_entries: list[dict[str, str]] = []
    raw_items: list[dict[str, str]] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        try:
            left, name = raw.split(b"\t", 1)
            mode, kind, oid = left.split()
        except ValueError as exc:
            raise GuardError("tree 出力を読めない") from exc
        item = {"path": decode_path(name), "mode": mode.decode(), "kind": kind.decode(), "oid": oid.decode()}
        raw_items.append(item)
    secret_paths = excluded_paths([item["path"] for item in raw_items], patterns)
    for item in raw_items:
        if item["path"] in secret_paths:
            item["_guard_oid"] = item.pop("oid")
            excluded_entries.append(item)
        else:
            entries.append(item)
    return sorted(entries, key=lambda x: x["path"]), sorted(excluded_entries, key=lambda x: x["path"])


def ignored(cwd: Path) -> list[str]:
    raw = git(cwd, "ls-files", "-o", "--ignored", "--exclude-standard", "-z")
    paths = [decode_path(x[0]) for x in nul_fields(raw)]
    # walk と同じ固定除外だけを外す。guard 自身の記録追加を対象集合の変更にしない。
    state_paths = excluded_paths(paths, [DEFAULT_EXCLUDE])
    return sorted(path for path in paths if path not in state_paths)


def control_files(worktree: list[dict[str, str]], excluded_entries: list[dict[str, str]]) -> list[dict[str, str]]:
    controls: list[dict[str, str]] = []
    for item in excluded_entries:
        if item["path"] == ".gitignore" or item["path"].endswith("/.gitignore"):
            raise GuardError("機密除外が .gitignore を隠すため ignore 規則を照合できない")
    for item in worktree:
        if item["path"] == ".gitignore" or item["path"].endswith("/.gitignore") or item["path"] == ".gitattributes" or item["path"].endswith("/.gitattributes"):
            controls.append(item)
    return controls


def task_files(task: Path, root: Path, patterns: list[str]) -> list[dict[str, str]]:
    parent = root / TASK_SOURCE["task_dir"] if TASK_SOURCE else task.parent
    result: list[dict[str, str]] = []
    try:
        directory_fd = open_directory(parent)
        try:
            with os.scandir(directory_fd) as stream:
                files = [parent / name for name in sorted(item.name for item in stream)]
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise GuardError(f"task_dir を読めない: {exc}") from exc
    candidates = [file for file in files if file.suffix == ".md" and file.name.startswith(TASK_PREFIXES)]
    secret_paths = excluded_paths([rel_path(root, file, "状態名 task MD") for file in candidates], patterns)
    for file in candidates:
        if file.suffix == ".md" and file.name.startswith(TASK_PREFIXES):
            rel = rel_path(root, file, "状態名 task MD")
            if rel in secret_paths:
                raise GuardError("機密除外が状態名 task MD を隠す")
            regular(file, "状態名 task MD")
            item = entry(file, root, False)
            result.append(item)
    return result


def digest_json(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode())


def current(root: Path, task: Path, reviews: list[Path], patterns: list[str]) -> dict[str, object]:
    if TASK_SOURCE:
        task = Path(TASK_SOURCE["body_path"])
        public_path(TASK_ROOT or root, task, patterns[1:], "外部 task 本文")
        raw = read_regular(task, "外部 task 本文")
        if sha256(raw) != TASK_SOURCE["body_sha256"]:
            raise GuardError("外部 task 本文の外部保持 sha256 が一致しない")
        name = "@external:" + TASK_SOURCE["task_id"]
    else:
        real_under_root(TASK_ROOT or root, task, "対象 task MD")
        regular(task, "対象 task MD")
        name = rel_path(TASK_ROOT or root, task, "対象 task MD")
        public_path(TASK_ROOT or root, task, patterns, "対象 task MD")
    head = git(root, "rev-parse", "HEAD").decode().strip()
    worktree, excluded_worktree = walk(root, patterns)
    index, excluded_index = git_index(root, patterns)
    head_tree, excluded_tree = tree(root, "HEAD", patterns)
    data: dict[str, object] = {
        "version": 1,
        "root": str(root),
        "task_root": str(TASK_ROOT or root),
        "task_path": name,
        "task_sha256": sha256(read_regular(task, "対象 task MD")),
        "head": head,
        "head_tree": head_tree,
        "excluded_head_tree": excluded_tree,
        # `git write-tree` は cache-tree を index に書き得る。read-only の index 表現を使う。
        "index_tree": digest_json(index),
        "index": index,
        "excluded_index": excluded_index,
        "worktree": worktree,
        "excluded_worktree": excluded_worktree,
        "ignored": ignored(root),
        "controls": control_files(worktree, excluded_worktree),
        "external_ignore": external_ignore(root, patterns),
        "exclude_patterns": patterns,
        "task_files": task_files(task, TASK_ROOT or root, patterns),
    }
    for review in reviews:
        if review.is_absolute() and (review == root or root in review.parents):
            real_under_root(root, review, "review 入力")
            review_rel = rel_path(root, review, "review 入力")
            # snapshot は既定除外の reviews に置く。しかし利用者の機密 ERE にも当たる入力は渡さない。
            if review_rel in excluded_paths([review_rel], patterns[1:]):
                raise GuardError("機密除外が review 入力を隠す")
    data["review_inputs"] = [{"path": str(review), "sha256": sha256(read_regular(review, "review 入力"))} for review in reviews]
    if TASK_SOURCE and data["task_sha256"] != TASK_SOURCE["body_sha256"]:
        raise GuardError("外部 task 本文が収集中に変わった")
    data["target_sha256"] = digest_json({key: value for key, value in data.items() if key != "review_inputs"})
    data["review_binding_sha256"] = digest_json({"target_sha256": data["target_sha256"], "review_inputs": data["review_inputs"]})
    return data


def state_file(state: Path) -> Path:
    return state / "manifest.json"


def write_manifest(state: Path, data: dict[str, object], *, create: bool) -> str:
    parent_fd = None
    state_fd = None
    temporary = ".review-guard-" + secrets.token_hex(12)
    try:
        parent_fd = open_directory(state.parent)
        if create:
            os.mkdir(state.name, 0o700, dir_fd=parent_fd)
        state_fd = os.open(state.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        payload = dict(data)
        payload["manifest_sha256"] = digest_json(data)
        raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2).encode() + b"\n"
        if len(raw) > MAX_FILE_BYTES:
            raise GuardError("state が読取上限を超える")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=state_fd)
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, "manifest.json", src_dir_fd=state_fd, dst_dir_fd=state_fd)
        same_directory(state, state_fd)
        return sha256(raw)
    except OSError as exc:
        raise GuardError(f"state を安全に書けない: {exc}") from exc
    finally:
        if state_fd is not None:
            try:
                os.unlink(temporary, dir_fd=state_fd)
            except FileNotFoundError:
                pass
            os.close(state_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def write_state(state: Path, data: dict[str, object]) -> str:
    return write_manifest(state, data, create=True)


def start_manifest(state: Path) -> Path:
    return state / "start.json"


def precheck(args: argparse.Namespace) -> None:
    """全入口で、内容を読むより先に既存の安全検査を通す。"""
    script = Path(__file__).parents[2] / "do-task/scripts/diff-snapshot.sh"
    command = ["bash", str(script), "--cwd", args.cwd, "--precheck"]
    if args.accept:
        command += ["--accept", args.accept]
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode:
        raise GuardError("precheck が拒否した: " + proc.stderr.decode(errors="replace").strip())
    if proc.stdout:
        tokens = proc.stdout.split(b"\0")
        if tokens.pop() != b"":
            raise GuardError("precheck の NUL 出力が不正")
        for token in tokens:
            key, sep, value = token.decode().partition("=")
            if not sep or not re.fullmatch(r"GIT_CONFIG_(COUNT|KEY_[0-9]+|VALUE_[0-9]+)", key):
                raise GuardError("precheck の環境指定が不正")
            os.environ[key] = value


def public_path(root: Path, path: Path, patterns: list[str], label: str) -> None:
    path = path.absolute()
    if path == root or root in path.parents:
        real_under_root(root, path, label)
        name = rel_path(root, path, label)
    else:
        name = path.as_posix().lstrip("/")
    if excluded_paths([name], patterns):
        raise GuardError(f"機密除外が {label} を隠す")


def external_ignore(root: Path, patterns: list[str]) -> list[dict]:
    info = Path(os.fsdecode(git(root, "rev-parse", "--git-path", "info/exclude")).strip())
    if not info.is_absolute():
        info = root / info
    configured = os.fsdecode(git(root, "config", "--path", "--get", "core.excludesFile", missing_ok=True)).strip()
    default = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "git/ignore"
    global_file = Path(configured) if configured else default
    if not global_file.is_absolute():
        global_file = root / global_file
    result = []
    for kind, path in (("info/exclude", info), ("core.excludesFile" if configured else "global-default", global_file)):
        public_path(root, path, patterns[1:], "ignore 規則")
        result.append({"source": kind, "path": str(path.absolute()),
                       "sha256": sha256(read_regular(path, "ignore 規則")) if path.exists() or path.is_symlink() else None})
    return result


def load_json(path: Path, expected: str, label: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        raise GuardError(f"{label}の外部保持 sha256 が必要")
    raw = read_regular(path, label)
    if sha256(raw) != expected:
        raise GuardError(f"{label}の外部保持 sha256 が一致しない")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise GuardError(f"{label}が JSON ではない") from exc
    if not isinstance(data, dict):
        raise GuardError(f"{label}の形式が不正")
    return data


def load_start(state: Path, expected: str) -> dict:
    data = load_json(start_manifest(state), expected, "開始時 state")
    return data


def load_state(state: Path, expected: str) -> dict:
    if state.is_symlink() or not state.is_dir():
        raise GuardError("state が安全なディレクトリではない")
    value = load_json(state_file(state), expected, "state manifest")
    declared = value.pop("manifest_sha256", None)
    if digest_json(value) != declared:
        raise GuardError("state manifest の自己 sha256 が一致しない")
    return value


def safe_output(path: Path, root: Path) -> None:
    if path.exists() or path.is_symlink():
        raise GuardError("出力先は未使用のパスが必要")
    if path.parent.resolve() != path.parent or not path.parent.is_dir():
        raise GuardError("出力先の親が安全なディレクトリでない")
    if root in path.parents and not excluded_paths([rel_path(root, path, "出力先")], [DEFAULT_EXCLUDE]):
        raise GuardError("管理ルート内の出力は reviews 配下に限る")


def output_json(path: Path, data: dict, root: Path, label: str) -> str:
    safe_output(path, root)
    raw = json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2).encode() + b"\n"
    create_bytes(path, raw)
    print(f"{label}_SHA256={sha256(raw)}")
    return sha256(raw)


def body_digest(raw: bytes) -> str:
    script = Path(__file__).parents[2] / "create-task/scripts/task-digest.py"
    spec = importlib.util.spec_from_file_location("task_digest", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.digest(raw.decode("utf-8"))
    except (module.DigestError, UnicodeError) as exc:
        raise GuardError("task 本文 digest を算出できない: " + str(exc)) from exc


def quality_inventory(root: Path, data: dict, explicit: list[str], patterns: list[str]) -> dict:
    # 静的な入口の一覧。動的 import やネットワーク取得先の網羅は主張しない。
    names = {"package.json", "pyproject.toml", "Makefile", "makefile", "tox.ini", "pytest.ini", "setup.cfg",
             "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Rakefile", "Gemfile", "project-profile.yml"}
    selected = set()
    for item in data["worktree"]:
        path = PurePosixPath(item["path"])
        if path.name in names or any(part in ("test", "tests", "scripts", "__tests__", ".github") for part in path.parts) or re.search(r"(test|spec|selftest|validate|check|lint)", path.name, re.I):
            if item["kind"] != "dir":
                selected.add(item["path"])
    for value in explicit:
        path = Path(value).absolute()
        public_path(root, path, patterns, "品質入口")
        name = rel_path(root, path, "品質入口")
        selected.add(name)
    files = {item["path"]: item for item in data["worktree"]}
    return {name: files.get(name) for name in sorted(selected)}


def changes(before: dict, after: dict) -> list[dict]:
    return [{"path": name, "before": before.get(name), "after": after.get(name)}
            for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]


def task_bytes(root: Path, data: dict) -> bytes:
    path = Path(TASK_SOURCE["body_path"]) if TASK_SOURCE else Path(data["task_root"]) / data["task_path"]
    raw = read_regular(path, "対象 task MD")
    if sha256(raw) != data["task_sha256"]:
        raise GuardError("task MD が収集中に変わった")
    return raw


def configure_source(args: argparse.Namespace, root: Path, patterns: list[str], saved: dict | None = None) -> Path:
    global TASK_SOURCE, TASK_ROOT
    TASK_SOURCE = {}
    TASK_ROOT = Path(saved["task_root"]) if saved else Path(args.task_root).absolute() if args.task_root else root
    if TASK_ROOT.resolve() != TASK_ROOT or not TASK_ROOT.is_dir():
        raise GuardError("task-root が通常ディレクトリでない")
    if args.task_root and Path(args.task_root).absolute() != TASK_ROOT:
        raise GuardError("task-root が開始時と違う")
    source = saved.get("task_source") if saved else None
    if args.task_body and getattr(args, "task_md", None):
        raise GuardError("task MD と外部本文は併用できない")
    if args.task_body or source:
        if not args.task_body or not re.fullmatch(r"[0-9a-f]{64}", args.expect_task_body_sha256 or ""):
            raise GuardError("外部 task 本文には fresh な控えと外部保持 sha256 が必要")
        if source:
            task_id, directory = source["task_id"], source["task_dir"]
        else:
            if not args.task_id or not args.task_dir or not re.fullmatch(r"[A-Za-z0-9_.:-]+", args.task_id):
                raise GuardError("外部 task の task-id と保護する task-dir が必要")
            task_id = args.task_id
            directory = rel_path(TASK_ROOT, Path(args.task_dir).absolute(), "task_dir")
        path = Path(args.task_body).absolute()
        public_path(TASK_ROOT, path, patterns[1:], "外部 task 本文")
        if path.parent.resolve() != path.parent:
            raise GuardError("外部 task 本文の親に symlink がある")
        task_dir = TASK_ROOT / directory
        if task_dir.resolve() != task_dir or not task_dir.is_dir():
            raise GuardError("task_dir が通常の管理ルート内ディレクトリでない")
        TASK_SOURCE = {"task_id": task_id, "task_dir": directory, "body_path": str(path), "body_sha256": args.expect_task_body_sha256}
        return path
    if saved:
        path = TASK_ROOT / saved["task_path"]
        if getattr(args, "task_md", None) and Path(args.task_md).absolute() != path:
            raise GuardError("task MD が開始時と違う")
        return path
    if not args.task_md:
        raise GuardError("task MD または外部 task 本文が必要")
    return Path(args.task_md).absolute()


def source_identity() -> dict | None:
    return {k: TASK_SOURCE[k] for k in ("task_id", "task_dir")} if TASK_SOURCE else None


def start(args: argparse.Namespace) -> int:
    root = repo_root(Path(args.cwd))
    patterns = compile_excludes(args.exclude_ere)
    task = configure_source(args, root, patterns)
    # 品質入口は walk が読む前にも機密判定する。
    for name in args.quality_path:
        public_path(root, Path(name), patterns, "品質入口")
    data = current(root, task, [], patterns)
    raw = task_bytes(root, data)
    data["task_body_digest"] = body_digest(raw)
    data["task_source"] = source_identity()
    data["quality_paths"] = [str(Path(name).absolute()) for name in args.quality_path]
    data["quality"] = quality_inventory(root, data, data["quality_paths"], patterns)
    data["preexisting_untracked"] = sorted(decode_path(x[0]) for x in nul_fields(git(root, "ls-files", "-o", "--exclude-standard", "-z")))
    state = Path(args.state).absolute()
    safe_output(state, root)
    parent_fd = open_directory(state.parent)
    try:
        os.mkdir(state.name, 0o700, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    output_json(start_manifest(state), data, root, "START")
    print(f"START_STATE={state}")
    return 0


def observation(data: dict) -> dict:
    # target_sha256 自体はこの観測から導出するので循環させない。
    keys = ("root", "task_root", "task_path", "task_sha256", "head", "head_tree", "excluded_head_tree", "index_tree", "index",
            "excluded_index", "worktree", "excluded_worktree", "ignored", "controls", "external_ignore", "task_files", "exclude_patterns")
    return {key: data[key] for key in keys}


def binding(data: dict) -> None:
    data["target_sha256"] = digest_json({"observation": observation(data), "disclosures": data["disclosures"],
                                         "commit_paths": data["commit_paths"], "phase": data["phase"]})
    data["review_binding_sha256"] = digest_json({"target_sha256": data["target_sha256"], "review_inputs": data["review_inputs"]})


def print_state(state: Path, data: dict, checksum: str) -> None:
    print(f"STATE={state}\nSTATE_SHA256={checksum}\nREVIEW_BINDING_SHA256={data['review_binding_sha256']}\nTARGET_SHA256={data['target_sha256']}")


def take(args: argparse.Namespace) -> int:
    root = repo_root(Path(args.cwd))
    patterns = compile_excludes(args.exclude_ere)
    started = load_start(Path(args.start_state).absolute(), args.expect_start_sha256)
    task = configure_source(args, root, patterns, started)
    identity = "@external:" + TASK_SOURCE["task_id"] if TASK_SOURCE else rel_path(TASK_ROOT, task, "対象 task MD")
    if started["root"] != str(root) or started["task_path"] != identity:
        raise GuardError("開始時 state の対象が一致しない")
    if not set(started["exclude_patterns"]).issubset(patterns):
        raise GuardError("開始時の機密除外 ERE が削除された")
    quality_paths = sorted(set(started["quality_paths"] + [str(Path(p).absolute()) for p in args.quality_path]))
    for name in quality_paths:
        public_path(root, Path(name), patterns, "品質入口")
    data = current(root, task, [], patterns)
    raw = task_bytes(root, data)
    digest = body_digest(raw)
    if digest != started["task_body_digest"]:
        raise GuardError("開始時から task 本文 digest が変わった")
    other = lambda d: [item for item in d["task_files"] if item["path"] != d["task_path"]]
    if other(started) != other(data):
        raise GuardError("開始時から対象外の状態名 task MD が変わった")
    if args.phase == "doc" and not TASK_SOURCE and not task.name.startswith("完了_"):
        raise GuardError("doc は完了 task で新しい start が必要")
    quality = quality_inventory(root, data, quality_paths, patterns)
    ignores = lambda d: {item["path"]: item for item in d["controls"] + d["external_ignore"]}
    hidden = set(data["ignored"]) - set(started["ignored"])
    # start 後の正当な機密指定追加を許すが、開始時の hash を開示欄へ運ばない。
    newly_private_quality = excluded_paths(list(started["quality"]), patterns)
    before_quality = {name: value for name, value in started["quality"].items() if name not in newly_private_quality}
    before_ignores = ignores(started)
    hidden_controls = excluded_paths(list(before_ignores), patterns)
    before_ignores = {name: value for name, value in before_ignores.items() if name not in hidden_controls}
    data.update({"start_sha256": args.expect_start_sha256, "phase": args.phase, "sealed": args.phase == "hold",
                 "task_original": base64.b64encode(raw).decode(), "task_body_digest": digest, "task_source": source_identity(),
                 "quality_paths": quality_paths, "quality": quality,
                 "reviewers": sorted(set(args.reviewer)),
                 "disclosures": {"ignore_changes": changes(before_ignores, ignores(data)),
                                 "new_ignored_paths": sorted(hidden), "quality_changes": changes(before_quality, quality),
                                 "quality_coverage": "静的入口・指定パスのみ。動的依存の網羅は保証しない"}})
    existing = set(started["preexisting_untracked"])
    tracked = {item["path"] for item in started["head_tree"] + started["index"]}
    # 開始前の私的な未追跡は、後から git add されても対象に昇格しない。対象 task だけ明示的に含む。
    data["commit_paths"] = sorted(item["path"] for item in data["worktree"] if item["kind"] in ("file", "symlink", "gitlink")
                                   and item["path"] not in set(data["ignored"])
                                   and (item["path"] not in existing or item["path"] in tracked or (TASK_ROOT == root and item["path"] == data["task_path"])))
    if observation(current(root, task, [], patterns)) != observation(data):
        raise GuardError("take の収集中に対象集合が変わった")
    binding(data)
    state = Path(args.state).absolute()
    safe_output(state, root)
    print_state(state, data, write_state(state, data))
    print("DISCLOSURES=" + json.dumps(data["disclosures"], ensure_ascii=True, sort_keys=True))
    return 0


def read_context(args: argparse.Namespace) -> tuple[Path, dict, list[str]]:
    root = repo_root(Path(args.cwd))
    patterns = compile_excludes(args.exclude_ere)
    public_path(root, Path(args.state).absolute() / "manifest.json", patterns[1:], "state")
    saved = load_state(Path(args.state).absolute(), args.expect_state_sha256)
    if str(root) != saved["root"] or patterns != saved["exclude_patterns"]:
        raise GuardError("管理ルートまたは機密除外集合が state と一致しない")
    configure_source(args, root, patterns, saved)
    return root, saved, patterns


def seal(args: argparse.Namespace) -> int:
    root, saved, patterns = read_context(args)
    if saved["phase"] == "hold":
        raise GuardError("未承認保存を承認用 seal に変換できない")
    task = Path(saved["task_root"]) / saved["task_path"]
    before = current(root, task, [], patterns)
    if observation(before) != observation(saved):
        raise GuardError("snapshot 生成の前後で review 対象集合が変わった")
    reviews = [Path(path).absolute() for path in args.review_input]
    actual = current(root, task, reviews, patterns)
    if observation(actual) != observation(saved):
        raise GuardError("seal の収集中に review 対象集合が変わった")
    saved["review_inputs"] = actual["review_inputs"]
    saved["sealed"] = True
    binding(saved)
    state = Path(args.state).absolute()
    print_state(state, saved, write_manifest(state, saved, create=False))
    return 0


def git_mode(item: dict) -> str:
    if item["kind"] == "gitlink":
        return "160000"
    return "120000" if item["kind"] == "symlink" else ("100755" if int(item["mode"], 8) & 0o111 else "100644")


def new_task_path(saved: dict, mode: str | None) -> str:
    if not mode or saved.get("task_source"):
        return saved["task_path"]
    path = PurePosixPath(saved["task_path"])
    if not path.name.startswith("進行中_"):
        raise GuardError("状態遷移元が 進行中_ ではない")
    return str(path.with_name(("完了_" if mode == "complete" else "保留_") + path.name[len("進行中_"):]))


def expected_index(saved: dict, actual: dict, mode: str | None) -> dict:
    paths = set(saved["commit_paths"])
    result = {item["path"]: {"path": item["path"], "mode": git_mode(item), "oid": item["oid"], "stage": "0"}
              for item in saved["worktree"] if item["path"] in paths}
    if mode and not saved.get("task_source") and saved["task_root"] == saved["root"]:
        result.pop(saved["task_path"], None)
        name = new_task_path(saved, mode)
        item = next(item for item in actual["worktree"] if item["path"] == name)
        result[name] = {"path": name, "mode": git_mode(item), "oid": item["oid"], "stage": "0"}
    return result


def evidence(args: argparse.Namespace, saved: dict) -> dict:
    if not args.evidence or not args.expect_evidence_sha256:
        raise GuardError("外部保持 hash を伴う直接検証結果が必要")
    public_path(Path(saved["root"]), Path(args.evidence), saved["exclude_patterns"][1:], "検証根拠")
    result = load_json(Path(args.evidence).absolute(), args.expect_evidence_sha256, "検証根拠")
    if result.get("review_binding_sha256") != saved["review_binding_sha256"] or result.get("phase") != saved["phase"]:
        raise GuardError("検証根拠が review 集合に結び付いていない")
    expected = "UNAPPROVED" if saved["phase"] == "hold" else "APPROVED"
    if result.get("verdict") != expected:
        raise GuardError("検証根拠の承認区分が一致しない")
    return result


def final_bytes(saved: dict, proof: dict, mode: str) -> bytes:
    if (mode == "pending") != (saved["phase"] == "hold"):
        raise GuardError("完了と未承認保留の区分が一致しない")
    new_task_path(saved, mode)
    raw = base64.b64decode(saved["task_original"], validate=True)
    text = raw.decode("utf-8")
    # 既存内容を保持し、ヘッダの状態 1 行と既存の追加修正記録節の末尾への追記だけを許す。
    lines = text.splitlines(keepends=True)
    heading = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    status = [i for i in range(heading) if lines[i].startswith("> **ステータス**:")]
    if len(status) > 1:
        raise GuardError("ステータス行が重複")
    if status:
        newline = "\r\n" if lines[status[0]].endswith("\r\n") else "\n"
        lines[status[0]] = "> **ステータス**: " + ("✅ 完了(" + proof["record_date"] + ")" if mode == "complete" else "保留（未承認保存）") + newline
    # フェンス内の見出しは除外する。task-digest が未閉鎖フェンス等を既に拒否している。
    fence = None
    records = []
    for i, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", bare)
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*", bare):
                fence = None
        elif marker:
            fence = marker.group(1)
        elif bare == "## 追加修正記録":
            records.append(i)
    if len(records) != 1:
        raise GuardError("追加修正記録節を一意に決められない")
    end = next((i for i in range(records[0] + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    record = "\n- 検証器記録: " + proof["verdict"] + " / 根拠 sha256:" + digest_json(proof) + " / review:" + saved["review_binding_sha256"] + "\n"
    if mode == "pending":
        record = "\n- **保留**(ship-task・" + proof["record_date"] + "): " + proof["hold_reason"] + " — " + proof["hold_next"] + " / UNAPPROVED / 根拠 sha256:" + digest_json(proof) + "\n"
    lines.insert(end, ("\n" if end and not lines[end - 1].endswith("\n") else "") + record)
    result = "".join(lines).encode("utf-8")
    if body_digest(result) != saved["task_body_digest"]:
        raise GuardError("最終記録の変換で task 本文 digest が変わった")
    return result


def finalized_bytes(args: argparse.Namespace, saved: dict) -> bytes | None:
    if not args.task_transition:
        if args.expected_task or args.expect_task_sha256:
            raise GuardError("task 遷移の無い期待バイト指定")
        return None
    proof = evidence(args, saved)
    expected = final_bytes(saved, proof, args.task_transition)
    if not args.expected_task or not args.expect_task_sha256:
        raise GuardError("期待 task と外部保持 sha256 が必要")
    public_path(Path(saved["root"]), Path(args.expected_task), saved["exclude_patterns"][1:], "期待 task")
    supplied = read_regular(Path(args.expected_task).absolute(), "期待 task")
    if sha256(supplied) != args.expect_task_sha256 or supplied != expected:
        raise GuardError("期待 task が元本文と直接検証結果の正規変換に一致しない")
    return expected


def compare(actual: dict, saved: dict, mode: str, transition: str | None, expected: bytes | None) -> bool:
    a, b = observation(actual), observation(saved)
    if transition:
        old, new = saved["task_path"], new_task_path(saved, transition)
        if actual["task_path"] != new or actual["task_sha256"] != sha256(expected):
            return False
        for key in (() if saved.get("task_source") else (("worktree", "task_files") if saved["task_root"] == saved["root"] else ("task_files",))):
            amap = {item["path"]: item for item in a[key]}
            bmap = {item["path"]: item for item in b[key]}
            replacement, original = amap.pop(new, None), bmap.pop(old, None)
            if not replacement or not original or old in amap or new in bmap:
                return False
            if replacement["kind"] != "file" or replacement["sha256"] != sha256(expected) or replacement["mode"] != original["mode"]:
                return False
            a[key], b[key] = amap, bmap
        a["task_path"], a["task_sha256"] = b["task_path"], b["task_sha256"]
    # stage すると未追跡から外れる。ignored は元から index にある対象を除いて比較する。
    staged_paths = set(saved["commit_paths"]) | {new_task_path(saved, transition)}
    a["ignored"] = [p for p in a["ignored"] if p not in staged_paths]
    b["ignored"] = [p for p in b["ignored"] if p not in staged_paths]
    if mode != "pre-stage":
        for key in ("head", "head_tree", "index_tree", "index"):
            a.pop(key), b.pop(key)
    if a != b or actual["review_inputs"] != saved["review_inputs"]:
        return False
    if mode == "pre-stage":
        return True
    # 除外 path を事前に stage しておく操作も、commit 候補へ運ばない。
    excluded_index = {i["path"]: (i["mode"], i["_guard_oid"]) for i in actual["excluded_index"]}
    excluded_head = {i["path"]: (i["mode"], i["_guard_oid"]) for i in saved["excluded_head_tree"]}
    if excluded_index != excluded_head:
        return False
    entries = {item["path"]: item for item in actual["index"]}
    wanted = expected_index(saved, actual, transition)
    if entries != wanted:
        return False
    if mode == "stage":
        return actual["head"] == saved["head"] and actual["head_tree"] == saved["head_tree"]
    wanted_tree = {path: {"path": path, "mode": item["mode"], "kind": "commit" if item["mode"] == "160000" else "blob", "oid": item["oid"]} for path, item in wanted.items()}
    if {item["path"]: item for item in actual["head_tree"]} != wanted_tree:
        return False
    # 一つの正規 commit だけを許す。未 review の途中 commit を隠して tree を戻す操作も拒否。
    parents = git(Path(saved["root"]), "rev-list", "--parents", "-n", "1", "HEAD").decode().split()
    return parents == [actual["head"], saved["head"]]


def check_current(args: argparse.Namespace, saved: dict, root: Path, patterns: list[str]) -> bool:
    if not saved["sealed"]:
        raise GuardError("seal 前の state は検証に使えない")
    expected = finalized_bytes(args, saved)
    if args.mode != "pre-stage":
        evidence(args, saved)
    paths = args.review_input or [item["path"] for item in saved["review_inputs"]]
    actual = current(root, Path(saved["task_root"]) / new_task_path(saved, args.task_transition), [Path(p).absolute() for p in paths], patterns)
    return compare(actual, saved, args.mode, args.task_transition, expected)


def verify(args: argparse.Namespace) -> int:
    root, saved, patterns = read_context(args)
    good = check_current(args, saved, root, patterns)
    print("RESULT=" + ("match" if good else "mismatch"))
    if good:
        print("REVIEW_BINDING_SHA256=" + saved["review_binding_sha256"])
    else:
        print("CHANGE=review-or-commit-set")
    return 0 if good else 1


def check_process_module():
    spec = importlib.util.spec_from_file_location("review_guard_check_process", Path(__file__).with_name("check-process.py"))
    module = importlib.util.module_from_spec(spec)
    # 検査対象内の helper でも .pyc を生成しない。呼出元の設定は必ず戻す。
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def run_checks(args: argparse.Namespace) -> int:
    try:
        valid_timeout = type(args.timeout) is int and args.timeout > 0 and math.isfinite(float(args.timeout))
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise GuardError("timeout は有限の正の整数が必要")
    root, saved, patterns = read_context(args)
    if not check_current(args, saved, root, patterns):
        raise GuardError("品質実行前に集合が変わった")
    public_path(root, Path(args.checks_file), patterns[1:], "品質コマンド計画")
    plan = load_json(Path(args.checks_file).absolute(), args.expect_checks_sha256, "品質コマンド計画")
    commands = plan.get("commands")
    if not isinstance(commands, list) or not commands:
        raise GuardError("品質コマンド計画が空")
    results = []
    # 実リポジトリの index/config/hooks/ignore・既存未追跡・ignored 生成物を持ち込まない。
    helper = check_process_module()
    clean = Path(tempfile.mkdtemp(prefix="review-guard-clean-"))
    control = Path(tempfile.mkdtemp(prefix="review-guard-checks-"))
    confirmed = True
    stop = helper.StopSignals()
    try:
        with stop:
            for item in saved["worktree"]:
                if item["path"] not in saved["commit_paths"]:
                    continue
                target = clean / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if item["kind"] == "gitlink":
                    target.mkdir(exist_ok=True)
                elif item["kind"] == "symlink":
                    # checkout の外へ向くリンクを品質コマンドに渡さない。
                    resolved = (target.parent / item["link"]).resolve()
                    if clean not in resolved.parents:
                        raise GuardError("clean checkout の外を指す symlink")
                    target.symlink_to(item["link"])
                else:
                    raw = read_regular(root / item["path"], "clean checkout 入力")
                    if sha256(raw) != item["sha256"]:
                        raise GuardError("clean checkout 作成中に入力が変わった")
                    target.write_bytes(raw)
                    target.chmod(int(item["mode"], 8))
            env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env["GIT_CONFIG_GLOBAL"] = os.devnull
            env["GIT_NO_LAZY_FETCH"] = "1"
            subprocess.run(["git", "init", "-q", str(clean)], env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # 機密除外済みの実体だけ。clean checkout の git 依存 test 用に index と HEAD も作る。
            subprocess.run(["git", "-C", str(clean), "add", "-f", "--all"], env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for item in saved["worktree"]:
                if item["kind"] == "gitlink" and item["path"] in saved["commit_paths"]:
                    subprocess.run(["git", "-C", str(clean), "update-index", "--add", "--cacheinfo", "160000", item["oid"], item["path"]],
                                   env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "-C", str(clean), "-c", "user.name=review-guard", "-c", "user.email=review-guard@example.invalid",
                            "-c", "core.hooksPath=/dev/null", "commit", "-qm", "reviewed tree"], env=env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for index, command in enumerate(commands):
                stop.check()
                if not isinstance(command, dict) or not isinstance(command.get("name"), str) or not command["name"] or not isinstance(command.get("argv"), list) or not command["argv"] or not all(isinstance(a, str) and "\0" not in a for a in command["argv"]):
                    raise GuardError("品質コマンドの name/argv が不正")
                record = control / str(index)
                record.mkdir()
                confirmed = False
                outcome = helper.run_command(command["argv"], clean, env, args.timeout, record, stop)
                confirmed = True
                stop.check()
                result = {"name": command["name"], "argv": command["argv"],
                          "exit_code": outcome["returncode"],
                          "stdout_sha256": outcome["stdout_sha256"], "stderr_sha256": outcome["stderr_sha256"],
                          "recovery_method": outcome["recovery_method"], "recovery_scope": outcome["recovery_scope"]}
                if outcome["timed_out"]:
                    result.update(exit_code=None, error="timeout")
                results.append(result)
                if outcome["timed_out"]:
                    break
            stop.check()
    except helper.RecoveryError as exc:
        raise GuardError(f"{exc}; 検証用コピーを保持: {clean}; 診断: {control}") from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if not confirmed or stop.number:
            raise GuardError(f"{exc}; 検証用コピーを保持: {clean}; 診断: {control}") from exc
        raise
    finally:
        if confirmed and not stop.number:
            shutil.rmtree(clean)
            shutil.rmtree(control)
    if not check_current(args, saved, root, patterns):
        raise GuardError("品質実行中に集合が変わった")
    receipt = {"kind": "direct-checks", "review_binding_sha256": saved["review_binding_sha256"],
               "clean_checkout": True, "plan_sha256": args.expect_checks_sha256, "results": results}
    output_json(Path(args.out).absolute(), receipt, root, "CHECKS")
    return 0 if all(item["exit_code"] == 0 for item in results) else 1


def attest(args: argparse.Namespace) -> int:
    root, saved, patterns = read_context(args)
    if not check_current(args, saved, root, patterns):
        raise GuardError("根拠固定前に集合が変わった")
    reviews = []
    if len(args.review_result) != len(args.expect_review_sha256):
        raise GuardError("review 返答と外部保持 hash の数が違う")
    for path, checksum in zip(args.review_result, args.expect_review_sha256):
        public_path(root, Path(path), patterns[1:], "review 返答")
        result = load_json(Path(path).absolute(), checksum, "review 返答")
        if result.get("review_binding_sha256") != saved["review_binding_sha256"] or result.get("verdict") != "APPROVED" or result.get("issues") not in ([], None):
            raise GuardError("review 返答が対象集合の APPROVED ではない")
        reviews.append({"reviewer": result.get("reviewer"), "verdict": "APPROVED", "sha256": checksum})
    checks = None
    if args.checks:
        public_path(root, Path(args.checks), patterns[1:], "直接検証結果")
        checks = load_json(Path(args.checks).absolute(), args.expect_checks_sha256, "直接検証結果")
        if checks.get("kind") != "direct-checks" or checks.get("review_binding_sha256") != saved["review_binding_sha256"] or checks.get("clean_checkout") is not True or not checks.get("results"):
            raise GuardError("直接検証結果の対象集合または clean checkout が不正")
        if not isinstance(checks["results"], list) or any(not isinstance(i, dict) or not isinstance(i.get("name"), str)
                or not isinstance(i.get("argv"), list) or (type(i.get("exit_code")) is not int and i.get("error") != "timeout")
                for i in checks["results"]):
            raise GuardError("直接検証結果の形式が不正")
    if saved["phase"] != "hold":
        if sorted(str(item["reviewer"]) for item in reviews) != saved["reviewers"] or not reviews:
            raise GuardError("必要な reviewer の直接返答が揃っていない")
        if not checks or any(item.get("exit_code") != 0 for item in checks["results"]):
            raise GuardError("直接検証結果が揃っていないか失敗している")
    elif (reviews or not re.fullmatch(r"[SDUG][0-9]+[ :][^\r\n]+", args.hold_reason or "")
          or not args.hold_next or any(c in args.hold_next for c in "\r\n")):
        raise GuardError("保留は承認返答を付けず対話点番号付きの理由と次の操作が必要")
    result = {"kind": "review-evidence", "phase": saved["phase"], "review_binding_sha256": saved["review_binding_sha256"],
              "verdict": "UNAPPROVED" if saved["phase"] == "hold" else "APPROVED", "checks": checks,
              "checks_sha256": args.expect_checks_sha256, "reviews": reviews, "disclosures": saved["disclosures"],
              "hold_reason": args.hold_reason if saved["phase"] == "hold" else None,
              "hold_next": args.hold_next if saved["phase"] == "hold" else None, "record_date": datetime.date.today().isoformat()}
    output_json(Path(args.out).absolute(), result, root, "EVIDENCE")
    return 0


def finalize(args: argparse.Namespace) -> int:
    root, saved, patterns = read_context(args)
    if not check_current(args, saved, root, patterns):
        raise GuardError("最終 task 生成前に集合が変わった")
    proof = evidence(args, saved)
    raw = final_bytes(saved, proof, args.transition)
    out = Path(args.out).absolute()
    safe_output(out, root)
    create_bytes(out, raw)
    print("EXPECTED_TASK_SHA256=" + sha256(raw))
    print("TASK_PATH=" + new_task_path(saved, args.transition))
    return 0


def pr_evidence(args: argparse.Namespace) -> int:
    root, saved, patterns = read_context(args)
    if not check_current(args, saved, root, patterns):
        raise GuardError("PR 根拠生成前に commit 集合が変わった")
    if args.mode != "commit":
        raise GuardError("PR 根拠は commit 照合だけを使う")
    proof = evidence(args, saved)
    print("## 検証\n")
    print("- 区分: " + proof["verdict"])
    print("- review 集合: `" + saved["review_binding_sha256"] + "`")
    print("- 検証根拠: `" + args.expect_evidence_sha256 + "`")
    if proof["checks"]:
        print("- 機密・既存未追跡・ignored 生成物を持ち込まない clean checkout で実行")
        for item in proof["checks"]["results"]:
            print("- 検証: " + json.dumps(item["name"], ensure_ascii=True) + " / argv=" + json.dumps(item["argv"], ensure_ascii=True) + " / exit=" + str(item["exit_code"]))
    for item in proof["reviews"]:
        print("- review: " + json.dumps(item["reviewer"], ensure_ascii=True) + " / " + item["verdict"])
    print("\n## ignore・品質入口の変更\n\n```json")
    print(json.dumps(proof["disclosures"], ensure_ascii=True, sort_keys=True, indent=2))
    print("```")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("start", "take", "seal", "verify", "run-checks", "attest", "finalize", "pr-evidence"):
        p = commands.add_parser(name)
        p.add_argument("--cwd", required=True)
        p.add_argument("--state", required=True)
        p.add_argument("--exclude-ere", action="append", required=True)
        p.add_argument("--accept")
        p.add_argument("--task-body")
        p.add_argument("--expect-task-body-sha256")
        p.add_argument("--task-id")
        p.add_argument("--task-dir")
        p.add_argument("--task-root")
        if name in ("start", "take"):
            p.add_argument("--task-md")
            p.add_argument("--quality-path", action="append", default=[])
        else:
            p.add_argument("--expect-state-sha256", required=True)
        if name == "take":
            p.add_argument("--start-state", required=True)
            p.add_argument("--expect-start-sha256", required=True)
            p.add_argument("--phase", choices=("implementation", "doc", "hold"), default="implementation")
            p.add_argument("--reviewer", action="append", default=[])
        if name == "seal":
            p.add_argument("--review-input", action="append", required=True)
        if name in ("verify", "run-checks", "attest", "finalize", "pr-evidence"):
            p.add_argument("--review-input", action="append")
            p.add_argument("--task-transition", choices=("complete", "pending"))
            p.add_argument("--expected-task")
            p.add_argument("--expect-task-sha256")
            p.add_argument("--evidence")
            p.add_argument("--expect-evidence-sha256")
            p.add_argument("--mode", choices=("pre-stage", "stage", "commit"), default="commit" if name == "pr-evidence" else "pre-stage")
        if name == "run-checks":
            p.add_argument("--checks-file", required=True)
            p.add_argument("--expect-checks-sha256", required=True)
            p.add_argument("--timeout", type=int, default=600)
        if name == "attest":
            p.add_argument("--checks")
            p.add_argument("--expect-checks-sha256")
            p.add_argument("--review-result", action="append", default=[])
            p.add_argument("--expect-review-sha256", action="append", default=[])
            p.add_argument("--hold-reason")
            p.add_argument("--hold-next")
        if name == "finalize":
            p.add_argument("--transition", choices=("complete", "pending"), required=True)
        if name in ("run-checks", "attest", "finalize"):
            p.add_argument("--out", required=True)
    return result


def compile_excludes(values: list[str]) -> list[str]:
    patterns = [DEFAULT_EXCLUDE, *values]
    excluded_paths([], patterns)
    return patterns


def main() -> int:
    args = parser().parse_args()
    try:
        precheck(args)
        if args.command == "take" and not args.reviewer:
            args.reviewer = ["reviewer"]
        return globals()[args.command.replace("-", "_")](args)
    except (GuardError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
