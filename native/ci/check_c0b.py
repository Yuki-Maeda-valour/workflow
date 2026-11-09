#!/usr/bin/env python3
"""C0b: 固定期待値・旧実関数・今回の Rust テスト実行物を比較する。"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from support_native import compare_runs, run_command, snapshot_tree

LEGACY = ROOT / "plugins/dev-workflow/skills/ship-task/scripts/loop-state.py"
KINDS = {"deadline", "items", "file-bytes", "total-bytes", "overflow", "io",
         "changed-open", "changed-read", "changed-entry", "not-regular", "not-directory",
         "invalid-input", "already-read"}
TOP_KEYS = {"case", "outcome", "body_hex", "error", "usage", "deadline_unchanged"}
STOP_TEXT = {
    "deadline": "状態の観察が 60 秒の上限を超えた",
    "items": "状態の観察の項目数が上限を超えた",
    "file-bytes": "通常ファイル 1 件の読取量が上限を超えた",
    "total-bytes": "通常ファイルの総読取量が上限を超えた",
    "changed-open": "通常ファイルが lstat と open の間に変わった",
    "changed-read": "通常ファイルが読取中に変わった",
    "not-regular": "通常ファイルが lstat と open の間に変わった",
}


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重複キー: {key}")
        result[key] = value
    return result


def observation(run, case):
    """接続の失敗と、処理が返した正しい拒否を混同しない。"""
    if run.timed_out or run.returncode != 0 or run.stderr:
        raise ValueError(f"{case}: 接続の終了・標準エラー・時間切れが不正")
    if not run.stdout.endswith(b"\n") or run.stdout.count(b"\n") != 1:
        raise ValueError("JSON 1 件と改行 1 個ではありません")
    value = json.loads(run.stdout, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != TOP_KEYS or value["case"] != case:
        raise ValueError("ケース名またはトップキーが不正")
    if type(value["deadline_unchanged"]) is not bool:
        raise ValueError("期限確認の型が不正")
    usage = value["usage"]
    if not isinstance(usage, dict) or set(usage) != {"items", "bytes"}:
        raise ValueError("消費量のキーが不正")
    if any(type(n) is not int or n < 0 for n in usage.values()):
        raise ValueError("消費量の型または値が不正")
    if value["outcome"] == "ok":
        body = value["body_hex"]
        if (not isinstance(body, str) or len(body) % 2 or
                any(c not in "0123456789abcdef" for c in body) or value["error"] is not None):
            raise ValueError("成功本文またはエラー欄が不正")
    elif value["outcome"] == "error":
        error = value["error"]
        if (value["body_hex"] is not None or not isinstance(error, dict) or
                set(error) != {"kind", "errno"} or not isinstance(error["kind"], str) or
                error["kind"] not in KINDS):
            raise ValueError("拒否結果の形式が不正")
        if error["kind"] == "io":
            if type(error["errno"]) is not int or error["errno"] <= 0:
                raise ValueError("IO の errno が不正")
        elif error["errno"] is not None:
            raise ValueError("IO 以外に errno がある")
    else:
        raise ValueError("結果の種類が不正")
    return value


def verify_run(run, case, expected):
    actual = observation(run, case)
    if actual != expected:
        raise ValueError(f"{case}: 固定期待値と不一致: expected={expected!r}, actual={actual!r}")
    return actual


def verify_pair(comparison, case, expected):
    verify_run(comparison.legacy, case, expected)
    verify_run(comparison.native, case, expected)
    if not comparison.matches:
        raise ValueError(f"{case}: 旧新版または副作用が不一致: {comparison.differences}")


def expected(case, body=b"", *, kind=None, code=None, items=0, count=0):
    return {"case": case, "outcome": "error" if kind else "ok",
            "body_hex": None if kind else body.hex(),
            "error": {"kind": kind, "errno": code} if kind else None,
            "usage": {"items": items, "bytes": count}, "deadline_unchanged": True}


@dataclass
class Case:
    name: str
    plan: dict
    result: dict
    content: bytes = b"old"
    effect: str = "none"


def case(name, *, body=b"old", content=b"old", kind=None, code=None, items=1, count=3,
         effect="none", **changes):
    plan = dict(operation="read", parent="input", name_hex=b"file".hex(), items=100,
                total=10_000_000, one=10_000_000, prior_items=0, prior_bytes=0,
                size=0, expired=False, reads=1, before_open="none", after_open="none",
                setup="none", legacy_stage="read_at")
    plan.update(changes)
    if plan["operation"] in ("tick", "remaining"):
        plan["legacy_stage"] = plan["operation"]
    return Case(name, plan, expected(name, body, kind=kind, code=code, items=items, count=count), content, effect)


def cases():
    for name, body in [("empty", b""), ("ascii", b"old"), ("raw-bytes", b"\0\xff\xfe"),
                       ("multiblock", b"A" * 131_073)]:
        yield case(name, content=body, body=body, count=len(body))
    for n in [3, 4, 5]:
        yield case(f"tick-items-{n}", operation="tick", body=b"", prior_items=n - 1,
                   items=n, count=0, kind="items" if n > 4 else None, **{"items_limit": 4})
    for what in ["file", "total"]:
        for n in [3, 4, 5]:
            yield case(f"tick-{what}-{n}", operation="tick", body=b"", size=n,
                       one=4 if what == "file" else 10, total=4 if what == "total" else 10,
                       count=0 if what == "file" and n > 4 else n,
                       kind=("file-bytes" if what == "file" else "total-bytes") if n > 4 else None)
    for n in [3, 4, 5]:
        yield case(f"read-file-{n}", content=b"x" * n, body=b"x" * n, one=4,
                   kind="file-bytes" if n > 4 else None, items=0 if n > 4 else 1,
                   count=0 if n > 4 else n)
        yield case(f"read-total-{n}", content=b"x" * n, body=b"x" * n, total=4,
                   kind="total-bytes" if n > 4 else None, count=n)
    yield case("fixed-block-total", content=b"x" * 65_537, total=1, one=100_000,
               kind="total-bytes", count=65_536, items_limit=1)
    yield case("large-profile", operation="profile", parent=".claude", content=b"x" * 1_048_577,
               body=b"x" * 1_048_577, one=67_108_864, count=1_048_577)
    yield case("small-profile-limit", operation="profile", parent=".claude", content=b"x" * 1_048_577,
               one=1_048_576, kind="file-bytes", items=0, count=0)
    yield case("borrow-two", reads=2, prior_items=3, prior_bytes=7, body=b"oldold", items=5, count=13)
    yield case("borrow-items-refusal", reads=2, prior_items=3, prior_bytes=7,
               items_limit=4, kind="items", items=5, count=10)
    yield case("borrow-total-refusal", reads=2, prior_items=3, prior_bytes=7,
               total=12, kind="total-bytes", items=5, count=13)
    for operation in ["tick", "remaining", "read"]:
        yield case(f"expired-{operation}", operation=operation, expired=True, prior_items=2,
                   prior_bytes=7, kind="deadline", items=2, count=7)
    yield case("missing-file", setup="missing", kind="io", code=errno.ENOENT, items=0, count=0, legacy_stage="stat")
    yield case("missing-parent", parent="missing", kind="io", code=errno.ENOENT, items=0, count=0, legacy_stage="ancestor")
    yield case("not-directory-parent", parent="input/file", kind="io", code=errno.ENOTDIR, items=0, count=0, legacy_stage="ancestor")
    yield case("denied-file", setup="denied-file", kind="io", code=errno.EACCES, count=0, legacy_stage="open")
    yield case("denied-search", parent="input/blocked/child", setup="denied-search", kind="io", code=errno.EACCES, items=0, count=0, legacy_stage="ancestor")
    yield case("broken-link", setup="broken-link", kind="io", code=errno.ELOOP, count=0, legacy_stage="open")
    yield case("terminal-link", setup="link", kind="io", code=errno.ELOOP, count=0, legacy_stage="open")
    # O_DIRECTORY の種類検査を先に通るため、両 OS で ENOTDIR。
    yield case("middle-link", parent="alias", setup="middle-link", kind="io",
               code=errno.ENOTDIR, items=0, count=0, legacy_stage="ancestor")
    yield case("directory-file", setup="directory", kind="not-regular", count=0, legacy_stage="opened-type")
    yield case("fifo-file", setup="fifo", kind="not-regular", count=0, legacy_stage="opened-type")
    # Darwin の socket vnode は EOPNOTSUPP、Linux の socket inode は ENXIO。
    # Apple の根拠: https://github.com/apple-oss-distributions/xnu/blob/main/bsd/vfs/vfs_subr.c
    # vn_authorize_open_existing: O_DIRECTORY の ENOTDIR と VSOCK の EOPNOTSUPP。
    yield case("socket-file", setup="socket", kind="io", code=errno.EOPNOTSUPP if sys.platform == "darwin" else errno.ENXIO,
               count=0, legacy_stage="open")
    yield case("held-parent", operation="profile", parent=".claude", before_open="parent-replace", effect="parent-replace")
    yield case("full-parent-content", operation="profile", parent=".claude", before_open="parent-content", effect="parent-content",
               kind="changed-open", items=0, count=0, legacy_stage="profile-parent")
    for mutation, kind, code, stage in [
        ("replace-file", "changed-open", None, "opened-identity"),
        ("replace-link", "io", errno.ELOOP, "open"),
        ("replace-fifo", "not-regular", None, "opened-type")]:
        yield case(mutation, before_open=mutation, setup="replacement-fifo" if mutation == "replace-fifo" else "none",
                   effect=mutation, kind=kind, code=code, count=0, legacy_stage=stage)
    for mutation, length in [("shrink", 1), ("grow", 6), ("overwrite", 3), ("chmod", 3), ("mtime-restored", 3)]:
        yield case(mutation, after_open=mutation, effect=mutation, kind="changed-read", count=length, legacy_stage="read-verify")
    yield case("actual-file-limit", after_open="grow", effect="grow", one=3,
               kind="file-bytes", count=6)
    yield case("ancestor-content", operation="ancestor", before_open="ancestor-content", effect="ancestor-content")
    yield case("ancestor-replace", operation="ancestor", before_open="ancestor-replace", effect="ancestor-replace",
               kind="changed-open", items=0, count=0, legacy_stage="ancestor")
    yield case("non-utf8-name", name_hex=b"file-\xff".hex())
    # 新部品だけの追加操作は旧版の同等性へ混ぜない。
    yield case("explicit-entry", operation="entry", effect="replace-file", kind="changed-entry", count=3)


def load_legacy():
    spec = importlib.util.spec_from_file_location("c0b_loop_state", LEGACY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def replace(path, action):
    path.rename(path.with_name("replaced-old"))
    if action == "replace-file":
        path.write_bytes(b"NEW")
    elif action == "replace-link":
        path.symlink_to("replaced-old")
    elif action == "replace-fifo":
        path.with_name("replacement-fifo").rename(path)
    else:
        raise ValueError(action)


def mutate_opened(path, action, opened):
    mtime = path.stat().st_mtime_ns
    if action == "shrink":
        path.write_bytes(b"x")
    elif action == "grow":
        path.write_bytes(b"longer")
    elif action in ("overwrite", "mtime-restored"):
        path.write_bytes(b"NEW")
    elif action == "chmod":
        path.chmod(0o400)
    elif action == "none":
        return
    else:
        raise ValueError(action)
    if action == "mtime-restored":
        until = time.monotonic() + 2
        while True:
            os.utime(path, ns=(path.stat().st_atime_ns, mtime))
            if path.stat().st_ctime_ns != opened.st_ctime_ns:
                break
            if time.monotonic() >= until:
                raise RuntimeError("ctime が変わらない入力環境です")


def legacy_record(case_name, plan):
    """例外を対応づける前にケース・実際の段階・完全な Stop 文を照合する。"""
    definitions = {item.name: item for item in cases()}
    definition = definitions[case_name]
    if plan != normalized_plan(definition):
        raise ValueError("固定した接続計画ではありません")
    old = load_legacy()
    budget = old.Budget(plan["items"], plan["total"], plan["one"], -1 if plan["expired"] else 30)
    budget.count, budget.bytes = plan["prior_items"], plan["prior_bytes"]
    deadline = budget.deadline
    stage = "tick"

    def read_one():
        nonlocal stage
        root_fd = parent = None
        directory = Path.cwd() / plan["parent"]
        path = directory / os.fsdecode(bytes.fromhex(plan["name_hex"]))
        try:
            stage = "ancestor"
            if plan["operation"] == "profile":
                root_fd = old.open_dir_path(str(Path.cwd()))
                before_parent = os.stat(".claude", dir_fd=root_fd, follow_symlinks=False)
                if plan["before_open"] == "parent-content":
                    (directory / "added").write_bytes(b"x")
                parent = os.open(".claude", old.DIR_FLAGS, dir_fd=root_fd)
                stage = "profile-parent"
                if not old.same(before_parent, os.fstat(parent)):
                    raise old.Stop("secret_paths の親ディレクトリが差し替わった")
            elif plan["operation"] == "ancestor":
                real_open = os.open
                def open_ancestor(name, *args, **kwargs):
                    if name == "input":
                        if plan["before_open"] == "ancestor-content":
                            (directory / "added").write_bytes(b"x")
                        else:
                            directory.rename(Path.cwd() / "held-input")
                            directory.mkdir()
                            (directory / "file").write_bytes(b"NEW")
                    return real_open(name, *args, **kwargs)
                with patch.object(old.os, "open", open_ancestor):
                    parent = old.open_dir_path(str(directory))
            else:
                parent = old.open_dir_path(str(directory))
            stage = "stat"
            name = os.fsdecode(bytes.fromhex(plan["name_hex"]))
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            action = plan["before_open"]
            if action.startswith("replace-"):
                replace(path, action)
            elif action == "parent-replace":
                directory.rename(Path.cwd() / "held-claude")
                directory.mkdir()
                path.write_bytes(b"NEW")
            real_open, real_fstat = os.open, os.fstat
            opened_once = False

            def tracked_open(*args, **kwargs):
                nonlocal stage
                stage = "open"
                return real_open(*args, **kwargs)

            def tracked_fstat(fd):
                nonlocal stage, opened_once
                actual = real_fstat(fd)
                if not opened_once:
                    opened_once = True
                    stage = "opened-type" if not stat.S_ISREG(actual.st_mode) else "opened-identity"
                    if old.same(before, actual) and stat.S_ISREG(actual.st_mode):
                        mutate_opened(path, plan["after_open"], actual)
                        stage = "read_at"
                else:
                    stage = "read-verify"
                return actual

            stage = "read_at"
            with patch.object(old.os, "open", tracked_open), patch.object(old.os, "fstat", tracked_fstat):
                return old.read_at(parent, name, before, budget)
        finally:
            if parent is not None:
                os.close(parent)
            if root_fd is not None:
                os.close(root_fd)

    try:
        if plan["operation"] == "tick":
            budget.tick(plan["size"])
            body = b""
        elif plan["operation"] == "remaining":
            stage = "remaining"
            budget.remaining()
            body = b""
        else:
            body = b"".join(read_one() for _ in range(plan["reads"]))
        record = expected(case_name, body, items=budget.count, count=budget.bytes)
    except old.Stop as error:
        wanted = definition.result["error"]
        if wanted is None:
            raise
        kind, code = wanted["kind"], wanted["errno"]
        allowed_stages = {plan["legacy_stage"]}
        if stage not in allowed_stages:
            raise RuntimeError(f"Stop の段階が違います: {stage}") from error
        if kind == "io" and stage == "ancestor":
            message = "親ディレクトリを安全に開けない: " + os.strerror(code)
            if not isinstance(error.__cause__, OSError) or error.__cause__.errno != code:
                raise RuntimeError("祖先取得の原因 errno が違います") from error
        elif kind == "changed-open" and stage == "profile-parent":
            message = "secret_paths の親ディレクトリが差し替わった"
        elif kind == "changed-open" and stage == "ancestor":
            message = "親ディレクトリが観察中に差し替わった"
        else:
            message = STOP_TEXT[kind]
        if str(error) != message:
            raise RuntimeError("未知の Stop 文です") from error
        record = expected(case_name, kind=kind, code=code, items=budget.count, count=budget.bytes)
    except OSError as error:
        wanted = definition.result["error"]
        if (wanted is None or wanted["kind"] != "io" or error.errno != wanted["errno"] or
                stage != plan["legacy_stage"]):
            raise
        record = expected(case_name, kind="io", code=error.errno, items=budget.count, count=budget.bytes)
    record["deadline_unchanged"] = budget.deadline == deadline
    return record


def normalized_plan(item):
    plan = dict(item.plan)
    plan["items"] = plan.pop("items_limit", plan["items"])
    return plan


@contextmanager
def runtime_setup(plan):
    """copytree が扱えない特殊ファイルと権限を、両側の起動直前に同じ形で用意する。"""
    path = Path.cwd() / "input" / "file"
    setup = plan["setup"]
    sock = None
    held = {}
    try:
        if setup == "denied-file":
            path.chmod(0)
        elif setup == "denied-search":
            (path.parent / "blocked/child").mkdir(parents=True)
            # 探索権限を外した後も、この既知の空の親子を観察できるようにする。
            for name in ("input/blocked", "input/blocked/child"):
                held[name] = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            (path.parent / "blocked").chmod(0)
        elif setup in ("fifo", "socket", "replacement-fifo"):
            special = path.with_name("replacement-fifo") if setup == "replacement-fifo" else path
            if special.exists():
                special.unlink()
            if setup == "socket":
                sock = socket.socket(socket.AF_UNIX)
                # sockaddr_un の上限へ、一時 root の長さを持ち込まない。
                sock.bind(str(special.relative_to(Path.cwd())))
            else:
                os.mkfifo(special, 0o600)
        yield held
    finally:
        if setup == "denied-file":
            path.chmod(0o644)
        elif setup == "denied-search":
            (path.parent / "blocked").chmod(0o755)
            (path.parent / "blocked/child").rmdir()
            (path.parent / "blocked").rmdir()
        for fd in held.values():
            os.close(fd)
        if sock is not None:
            sock.close()
        if setup in ("fifo", "socket", "replacement-fifo"):
            for special in (path, path.with_name("replacement-fifo")):
                if special.exists() and not special.is_file():
                    special.unlink()
            if setup in ("fifo", "socket"):
                path.write_bytes(b"old")
                path.chmod(0o644)


ATTRIBUTE_KEYS = {"dev", "ino", "mode", "size", "mtime_ns", "ctime_ns"}
DIRECTORY_CONTENT_FIELDS = {"size", "mtime_ns", "ctime_ns"}


def snapshot_attributes(root, held=None):
    """各コピー内の属性。atime は通常の読取りでも変わるため記録しない。"""
    root, held = Path(root), held or {}
    result = {}

    def visit(path, relative):
        try:
            info = path.lstat()
        except PermissionError:
            # denied-search の既知の子だけ。新しい名前や別の読取拒否は隠さない。
            if relative not in held:
                raise
            info = os.fstat(held[relative])
        result[relative] = {key: getattr(info, "st_" + key) for key in ATTRIBUTE_KEYS}
        if stat.S_ISDIR(info.st_mode):
            # 権限を外した親も、保持済みの fd で現在の名前を列挙する。
            # 名前の置換は親の属性と名前一覧で拒否する。変更の許可はないケースだけで使う。
            names = os.listdir(held[relative]) if relative in held else os.listdir(path)
            for name in sorted(names):
                child = name if relative == "." else relative + "/" + name
                visit(path / name, child)

    visit(root, ".")
    return result


def check_attribute_effects(before, after, item):
    """意図した変更の場所と属性だけを許す。独立コピー間の inode は比較しない。"""
    allowed = {}
    origins = {}
    added, removed = set(), set()
    effect = item.effect
    new_types = {}
    if effect in ("shrink", "grow", "overwrite", "mtime-restored", "chmod"):
        allowed["input/file"] = {
            "shrink": {"size", "mtime_ns", "ctime_ns"},
            "grow": {"size", "mtime_ns", "ctime_ns"},
            "overwrite": {"mtime_ns", "ctime_ns"},
            "mtime-restored": {"ctime_ns"},
            "chmod": {"mode", "ctime_ns"},
        }[effect]
        wanted_size = {"shrink": 1, "grow": 6}.get(effect, before["input/file"]["size"])
        if after.get("input/file", {}).get("size") != wanted_size:
            raise ValueError(f"{item.name}: 予定外のファイル長")
        if effect == "chmod" and stat.S_IMODE(after["input/file"]["mode"]) != 0o400:
            raise ValueError(f"{item.name}: 予定外の権限変更")
    elif effect in ("parent-content", "ancestor-content"):
        parent = ".claude" if effect == "parent-content" else "input"
        allowed[parent] = DIRECTORY_CONTENT_FIELDS
        added = {parent + "/added"}
        new_types[parent + "/added"] = (stat.S_IFREG, 1)
    elif effect in ("parent-replace", "ancestor-replace"):
        parent, saved = (".claude", "held-claude") if effect == "parent-replace" else ("input", "held-input")
        allowed["."] = DIRECTORY_CONTENT_FIELDS
        allowed[parent] = {"ino", "size", "mtime_ns", "ctime_ns"}
        allowed[parent + "/file"] = ATTRIBUTE_KEYS - {"dev"}
        origins[saved], origins[saved + "/file"] = parent, parent + "/file"
        allowed[saved] = {"ctime_ns"}
        added = {saved, saved + "/file"}
        new_types[parent], new_types[parent + "/file"] = (stat.S_IFDIR, None), (stat.S_IFREG, 3)
    elif effect in ("replace-file", "replace-link", "replace-fifo"):
        allowed["input"] = DIRECTORY_CONTENT_FIELDS
        origins["input/replaced-old"] = "input/file"
        allowed["input/replaced-old"] = {"ctime_ns"}
        added = {"input/replaced-old"}
        if effect == "replace-fifo":
            origins["input/file"] = "input/replacement-fifo"
            allowed["input/file"] = {"ctime_ns"}
            removed = {"input/replacement-fifo"}
        else:
            allowed["input/file"] = ATTRIBUTE_KEYS - {"dev"}
            new_types["input/file"] = (stat.S_IFREG, 3) if effect == "replace-file" else (stat.S_IFLNK, len("replaced-old"))
    elif effect != "none":
        raise ValueError(f"属性の変化を定義していないケース: {item.name}")
    if set(after) != (set(before) - removed) | added:
        raise ValueError(f"{item.name}: 属性一覧の予定外の名前変更")
    unexpected = []
    for name, current in after.items():
        origin = origins.get(name, name)
        if origin in before:
            different = {key for key in ATTRIBUTE_KEYS if before[origin][key] != current[key]}
            forbidden = different - allowed.get(name, set())
            if forbidden:
                unexpected.append((name, sorted(forbidden)))
        elif name not in added:
            unexpected.append((name, ["unexpected-entry"]))
        if name in new_types:
            kind, size = new_types[name]
            if (stat.S_IFMT(current["mode"]) != kind or current["dev"] != before["."]["dev"] or
                    (size is not None and current["size"] != size)):
                unexpected.append((name, ["unexpected-new-entry"]))
    if unexpected:
        raise ValueError(f"{item.name}: 予定外の属性変更: {unexpected}")


def seed(root, item):
    plan = normalized_plan(item)
    (root / "input").mkdir()
    name = os.fsdecode(bytes.fromhex(plan["name_hex"]))
    target = root / (".claude" if plan["operation"] == "profile" else "input")
    target.mkdir(exist_ok=True)
    path = target / name
    path.write_bytes(item.content)
    path.chmod(0o644)
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    setup = plan["setup"]
    if setup == "missing":
        path.unlink()
    elif setup in ("link", "broken-link"):
        path.rename(target / "real")
        path.symlink_to("real" if setup == "link" else "absent")
    elif setup == "middle-link":
        (root / "alias").symlink_to("input")
    elif setup == "directory":
        path.unlink()
        path.mkdir()
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))


def check_effects(comparison, item):
    """意図した変更だけを期待する。通常ケースは入力と完全一致。"""
    before, after = comparison.before, comparison.native_tree
    effect = item.effect
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    if effect == "none":
        wanted = set()
    elif effect == "parent-replace":
        wanted = {".claude/file", "held-claude", "held-claude/file"}
    elif effect == "parent-content":
        wanted = {".claude/added"}
    elif effect == "ancestor-content":
        wanted = {"input/added"}
    elif effect == "ancestor-replace":
        wanted = {"input/file", "held-input", "held-input/file"}
    elif effect.startswith("replace-"):
        wanted = {"input/file", "input/replaced-old"}
    else:
        wanted = {"input/file"}
    if changed != wanted:
        raise ValueError(f"{item.name}: 予定外の入力変更: {changed} != {wanted}")


def selftests():
    spec = importlib.util.spec_from_file_location("c0b_checker_selftests", Path(__file__).with_name("test_c0b.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    if not result.wasSuccessful() or result.testsRun == 0:
        raise ValueError("C0b 比較器の自己テストが失敗しました")
    return result.testsRun


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--legacy-child")
    parser.add_argument("--native-child", nargs=2)
    args = parser.parse_args()
    if args.legacy_child or args.native_child:
        raw = sys.stdin.buffer.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("接続計画が長すぎます")
        plan = json.loads(raw)
        name = args.legacy_child or args.native_child[1]
        item = next(item for item in cases() if item.name == name)
        if plan != normalized_plan(item):
            raise ValueError("固定した接続計画ではありません")
        with runtime_setup(plan) as held:
            before_attributes = snapshot_attributes(Path.cwd(), held)
            if args.legacy_child:
                stdout, stderr, returncode = encode(legacy_record(name, plan)), b"", 0
            else:
                fixture = args.native_child[0]
                result = run_command([fixture, "--c0b-fixture", name], cwd=Path.cwd(), input=raw, timeout=5)
                if result.timed_out:
                    raise RuntimeError("Rust 接続の時間切れ")
                stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
            check_attribute_effects(before_attributes, snapshot_attributes(Path.cwd(), held), item)
        sys.stdout.buffer.write(stdout)
        sys.stderr.buffer.write(stderr)
        return returncode
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise ValueError("C0b 比較は Python 3.10 系・root 以外の通常利用者で実行してください")
    if args.fixture is None:
        raise ValueError("--fixture が必要です")
    fixture = args.fixture.resolve(strict=True)
    if not fixture.is_file() or not os.access(fixture, os.X_OK):
        raise ValueError("fixture は今回作った実行可能ファイルを指定してください")
    total_self = selftests()
    count = 0
    with tempfile.TemporaryDirectory(prefix="c0b-compare-") as directory:
        root = Path(directory).resolve(strict=True)
        for item in cases():
            initial = root / (item.name + "-input")
            initial.mkdir()
            try:
                seed(initial, item)
            except OSError as error:
                if item.name == "non-utf8-name" and sys.platform == "darwin" and error.errno in (errno.EILSEQ, errno.EINVAL):
                    print(f"T2 未実施: Mac の実在名を作れません: {error}")
                    continue
                raise
            raw = encode(normalized_plan(item))
            native = [sys.executable, "-B", str(Path(__file__).resolve()), "--native-child", str(fixture), item.name]
            if item.name == "explicit-entry":
                before = snapshot_tree(initial)
                result = run_command(native, cwd=initial, input=raw, timeout=10)
                verify_run(result, item.name, item.result)
                after = snapshot_tree(initial)
                if {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)} != {"input/file", "input/replaced-old"}:
                    raise ValueError("explicit-entry の予定外の入力変更")
            else:
                legacy = [sys.executable, "-B", str(Path(__file__).resolve()), "--legacy-child", item.name]
                result = compare_runs(legacy, native, initial_tree=initial, work_dir=root / (item.name + "-work"), input=raw, timeout=10)
                verify_pair(result, item.name, item.result)
                check_effects(result, item)
            count += 1
    print(f"C0b: 比較器の自己テスト {total_self} 件、実ファイルと予算 {count} ケース 合格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
