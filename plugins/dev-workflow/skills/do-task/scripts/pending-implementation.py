#!/usr/bin/env python3
"""保護記録らしい名前の直下エントリだけを列挙する。内部内容は読まない。"""

import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections import deque


MAX_ENTRIES = 4096
MAX_CANDIDATES = 128
TIME_LIMIT_SECONDS = 5.0
ROOT_IDS = ("xdg_state", "home_state", "home_cache")
STAT_FIELDS = ("dev", "ino", "mode", "uid", "size", "mtime_ns", "ctime_ns")
HELP = """使用方法:
  python3 pending-implementation.py scan --cwd <作業対象の管理ルート>
      [--acknowledged-list <64桁の一覧確認値>]

保護記録らしい候補を3か所の直下から列挙します。候補の内部は読みません。
一覧確認値は、人が全候補を具体的に確認した場合だけ渡してください。
保護記録の真正性や、処理の停止を証明する値ではありません。
終了値: 0=候補なし/確認値一致、10=人の確認が必要、11=列挙不能、
        2=引数または作業対象の不正、20=想定外の失敗。
上限: 置き場ごと4096件、合計128候補、走査全体5秒。
"""


class ScanError(Exception):
    def __init__(self, code, message, root_id=None, path=None, exit_code=11):
        super().__init__(message)
        self.exit_code = exit_code
        self.issue = {"code": code, "root_id": root_id,
                      "path_bytes_hex": path.hex() if path is not None else None,
                      "message": message}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def list_digest(result):
    root_keys = ("id", "input_path_bytes_hex", "resolved_path_bytes_hex",
                 "status", "reason", "identity", "canonical_id")
    payload = {"schema_version": 1, "cwd_bytes_hex": result["cwd_bytes_hex"],
               "roots": [{key: root[key] for key in root_keys}
                         for root in result["roots"]],
               "candidates": [{key: value for key, value in item.items() if key != "path"}
                              for item in result["candidates"]]}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def check_features():
    if sys.version_info < (3, 8):
        raise ScanError("unsupported-python", "Python 3.8以上が必要です。")
    if os.name != "posix":
        raise ScanError("unsupported-platform", "POSIXのファイル機能が必要です。")
    names = ("open", "close", "fstat", "stat", "scandir", "readlink",
             "fsencode", "fsdecode", "getcwdb")
    missing = ["os." + name for name in names if not callable(getattr(os, name, None))]
    missing += ["os." + name for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
                if not isinstance(getattr(os, name, None), int)]
    for name, support in (("scandir", "supports_fd"), ("stat", "supports_dir_fd"),
                          ("stat", "supports_follow_symlinks")):
        if getattr(os, name, None) not in getattr(os, support, ()):
            missing.append("os." + support + ":" + name)
    missing += ["stat_result.st_" + name for name in STAT_FIELDS
                if not hasattr(getattr(os, "stat_result", None), "st_" + name)]
    if not callable(getattr(time, "monotonic", None)):
        missing.append("time.monotonic")
    if missing:
        raise ScanError("missing-feature", "必要な機能がありません: " + ", ".join(missing))


def os_failure(exc, root_id, path, changed=False):
    unsupported = {errno.ENOSYS, getattr(errno, "ENOTSUP", errno.ENOSYS),
                   getattr(errno, "EOPNOTSUPP", errno.ENOSYS)}
    if isinstance(exc, NotImplementedError) or getattr(exc, "errno", None) in unsupported:
        code, message = "missing-feature", "必要なファイル機能を利用できません。"
    elif isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        code, message = "unreadable", "置き場または候補の情報を読めません。"
    elif changed:
        code, message = "changed-during-scan", "走査中に置き場または候補が変わりました。"
    elif getattr(exc, "errno", None) == errno.ENOTDIR:
        code, message = "not-directory", "置き場の経路がディレクトリではありません。"
    else:
        code, message = "resolution-failed", "置き場の実体を確定できません。"
    detail = getattr(exc, "errno", None)
    if detail is not None:
        message += " OSエラー番号: " + str(detail)
    return ScanError(code, message, root_id, path)


class Budget:
    def __init__(self):
        self.started = self.now()

    @staticmethod
    def now():
        try:
            return time.monotonic()
        except (OSError, NotImplementedError):
            raise ScanError("missing-feature", "単調時計を利用できません。")

    def check(self, root_id=None, path=None):
        if self.now() - self.started >= TIME_LIMIT_SECONDS:
            raise ScanError("time-limit", "走査全体の5秒の上限に達しました。", root_id, path)


def stat_values(info):
    values = {}
    for name in STAT_FIELDS:
        value = getattr(info, "st_" + name, None)
        if type(value) is not int:
            raise ScanError("missing-feature", "必要な整数のファイル情報を取得できません。")
        values[name] = value
    return values


def resolve_directory(path, budget, root_id=None):
    """実在するリンクを許す。通常の欠落と壊れたリンクを区別する。

    realpathの寛容な欠落処理や、新しいPythonのstrict引数には依存しない。
    リンク先をたどる区間の欠落は、元の経路の欠落とは別に失敗とする。
    """
    pending = deque((part, False) for part in path.split(b"/"))
    resolved = b"/"
    links = 0
    info = None
    while pending:
        budget.check(root_id, path)
        part, required = pending.popleft()
        if part in (b"", b"."):
            continue
        if part == b"..":
            resolved = os.path.dirname(resolved)
            info = None
            continue
        probe = os.path.join(resolved, part)
        try:
            info = os.stat(probe, follow_symlinks=False)
        except FileNotFoundError as exc:
            if required:
                raise os_failure(exc, root_id, path)
            return None
        except (OSError, NotImplementedError) as exc:
            raise os_failure(exc, root_id, path)
        if stat.S_ISLNK(info.st_mode):
            links += 1
            if links > 40:
                raise ScanError("resolution-failed", "リンクの循環または過剰な連鎖があります。", root_id, path)
            try:
                target = os.readlink(probe)
            except (OSError, NotImplementedError) as exc:
                raise os_failure(exc, root_id, path)
            if target.startswith(b"/"):
                resolved = b"/"
            pending.extendleft(reversed([(item, True) for item in target.split(b"/")]))
            info = None
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ScanError("not-directory", "置き場の経路がディレクトリではありません。", root_id, path)
        resolved = probe
    if info is None:
        try:
            info = os.stat(resolved, follow_symlinks=False)
        except (OSError, NotImplementedError) as exc:
            raise os_failure(exc, root_id, path)
    if not stat.S_ISDIR(info.st_mode):
        raise ScanError("not-directory", "置き場がディレクトリではありません。", root_id, path)
    budget.check(root_id, path)
    stat_values(info)
    return resolved, info


def candidate_kind(mode):
    for predicate, name in ((stat.S_ISDIR, "directory"), (stat.S_ISREG, "file"),
                            (stat.S_ISLNK, "symlink"), (stat.S_ISFIFO, "fifo"),
                            (stat.S_ISSOCK, "socket"), (stat.S_ISBLK, "block"),
                            (stat.S_ISCHR, "character")):
        if predicate(mode):
            return name
    return "other"


class Scanner:
    def __init__(self, result, budget):
        self.result = result
        self.budget = budget
        self.opened = []
        self.canonical = {}

    def changed(self, root, path=None):
        raise ScanError("changed-during-scan", "走査中に置き場または候補が変わりました。",
                        root["id"], path or bytes.fromhex(root["input_path_bytes_hex"]))

    def check_root(self, root, fd, original):
        path = bytes.fromhex(root["input_path_bytes_hex"])
        self.budget.check(root["id"], path)
        try:
            current = resolve_directory(path, self.budget, root["id"])
            held = os.fstat(fd)
        except ScanError as exc:
            if exc.issue["code"] in ("resolution-failed", "not-directory"):
                self.changed(root)
            raise
        except (OSError, NotImplementedError) as exc:
            raise os_failure(exc, root["id"], path, changed=True)
        if (current is None or current[0].hex() != root["resolved_path_bytes_hex"]
                or stat_values(current[1]) != original or stat_values(held) != original):
            self.changed(root)

    def candidate_stat(self, root, fd, name):
        path = os.path.join(bytes.fromhex(root["resolved_path_bytes_hex"]), name)
        self.budget.check(root["id"], path)
        try:
            return stat_values(os.stat(name, dir_fd=fd, follow_symlinks=False))
        except (OSError, NotImplementedError) as exc:
            raise os_failure(exc, root["id"], path, changed=True)

    def enumerate_root(self, root, fd):
        path = bytes.fromhex(root["resolved_path_bytes_hex"])
        count = 0
        names = set()
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    self.budget.check(root["id"], path)
                    if count >= MAX_ENTRIES:
                        raise ScanError("entry-limit", "置き場の4096件の上限を超えました。", root["id"], path)
                    count += 1
                    name = os.fsencode(entry.name)
                    if not name.startswith(b"guard-") or len(name) <= len(b"guard-"):
                        continue
                    if name in names:
                        self.changed(root, os.path.join(path, name))
                    names.add(name)
                    if len(self.result["candidates"]) >= MAX_CANDIDATES:
                        raise ScanError("candidate-limit", "合計128候補の上限を超えました。", root["id"], path)
                    values = self.candidate_stat(root, fd, name)
                    candidate_path = os.path.join(path, name)
                    item = {"root_id": root["canonical_id"], "path": os.fsdecode(candidate_path),
                            "path_bytes_hex": candidate_path.hex(), "name_bytes_hex": name.hex(),
                            "kind": candidate_kind(values["mode"])}
                    item.update(values)
                    self.result["candidates"].append(item)
        except (OSError, NotImplementedError) as exc:
            raise os_failure(exc, root["id"], path, changed=True)

    def scan_root(self, root):
        path = bytes.fromhex(root["input_path_bytes_hex"])
        resolved = resolve_directory(path, self.budget, root["id"])
        if resolved is None:
            root.update(status="absent", reason="missing")
            return
        physical, info = resolved
        original = stat_values(info)
        root.update(resolved_path=os.fsdecode(physical), resolved_path_bytes_hex=physical.hex())
        try:
            fd = os.open(physical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except (OSError, NotImplementedError) as exc:
            raise os_failure(exc, root["id"], path, changed=True)
        self.opened.append((root, fd, original))
        self.check_root(root, fd, original)
        key = (original["dev"], original["ino"])
        canonical_id = self.canonical.get(key, root["id"])
        root.update(status="present", reason=None, canonical_id=canonical_id,
                    identity={name: original[name] for name in ("dev", "ino", "mode", "uid")})
        if key not in self.canonical:
            self.canonical[key] = canonical_id
            self.enumerate_root(root, fd)
        self.check_root(root, fd, original)

    def scan(self):
        inputs = (("xdg_state", "XDG_STATE_HOME", b"dev-workflow"),
                  ("home_state", "HOME", b".local/state/dev-workflow"),
                  ("home_cache", "HOME", b".cache/dev-workflow"))
        try:
            for root_id, variable, suffix in inputs:
                value = os.environ.get(variable)
                root = {"id": root_id, "input_path": None, "input_path_bytes_hex": None,
                        "resolved_path": None, "resolved_path_bytes_hex": None,
                        "status": "skipped", "reason": "unset", "identity": None, "canonical_id": None}
                self.result["roots"].append(root)
                self.budget.check(root_id)
                if not value:
                    continue
                raw = os.fsencode(value)
                if not os.path.isabs(raw):
                    root["reason"] = "relative"
                    continue
                path = os.path.join(raw, suffix)
                root.update(input_path=os.fsdecode(path), input_path_bytes_hex=path.hex())
                self.scan_root(root)
            if not any(root["input_path"] is not None for root in self.result["roots"]):
                raise ScanError("no-roots", "HOMEとXDG_STATE_HOMEから置き場を得られません。")
            # 他の置き場を調べている間の変化も、保持したfdと再取得で確かめる。
            for root, fd, original in self.opened:
                self.check_root(root, fd, original)
                if root["canonical_id"] == root["id"]:
                    for item in self.result["candidates"]:
                        if item["root_id"] == root["id"]:
                            values = self.candidate_stat(root, fd, bytes.fromhex(item["name_bytes_hex"]))
                            if values != {name: item[name] for name in STAT_FIELDS}:
                                self.changed(root, bytes.fromhex(item["path_bytes_hex"]))
                self.check_root(root, fd, original)
            for root in self.result["roots"]:
                if root["status"] == "absent":
                    path = bytes.fromhex(root["input_path_bytes_hex"])
                    if resolve_directory(path, self.budget, root["id"]) is not None:
                        self.changed(root)
            self.budget.check()
        finally:
            # close失敗でも残りのfdを閉じてから失敗を返す。
            failure = None
            for root, fd, original in reversed(self.opened):
                try:
                    os.close(fd)
                except (OSError, NotImplementedError) as exc:
                    failure = os_failure(exc, root["id"], bytes.fromhex(root["input_path_bytes_hex"]))
            if failure is not None:
                raise failure


def parse_arguments(argv):
    if not argv or argv[0] != "scan":
        raise ScanError("invalid-arguments", "scanと必須の--cwdを指定してください。", exit_code=2)
    values = {}
    args = iter(argv[1:])
    for token in args:
        name, equal, value = token.partition("=")
        if name not in ("--cwd", "--acknowledged-list") or name in values:
            raise ScanError("invalid-arguments", "不明または重複した引数があります。", exit_code=2)
        if not equal:
            value = next(args, None)
            if value is None or value.startswith("--"):
                raise ScanError("invalid-arguments", "引数の値がありません。", exit_code=2)
        values[name] = value
    if "--cwd" not in values:
        raise ScanError("invalid-arguments", "--cwdは必須です。", exit_code=2)
    acknowledged = values.get("--acknowledged-list")
    if acknowledged is not None and re.fullmatch(r"[0-9a-fA-F]{64}", acknowledged) is None:
        raise ScanError("invalid-arguments", "一覧確認値は64桁のASCII hexで指定してください。", exit_code=2)
    return values["--cwd"], acknowledged.lower() if acknowledged is not None else None


def run(argv=None):
    """JSON出力前の結果と終了値を返す。永続状態は作らない。"""
    result = {"schema_version": 1, "result": "internal", "cwd": None, "cwd_bytes_hex": None,
              "complete": False, "roots": [], "candidates": [], "issues": []}
    try:
        cwd, acknowledged = parse_arguments(sys.argv[1:] if argv is None else argv)
        check_features()
        budget = Budget()
        if not cwd or "\x00" in cwd:
            raise ScanError("invalid-cwd", "作業対象のディレクトリが不正です。", exit_code=2)
        raw = os.fsencode(cwd)
        try:
            if not os.path.isabs(raw):
                raw = os.path.join(os.getcwdb(), raw)
            resolved = resolve_directory(raw, budget)
            if resolved is None:
                raise ScanError("invalid-cwd", "作業対象のディレクトリがありません。", exit_code=2)
        except ScanError as exc:
            if exc.issue["code"] in ("resolution-failed", "not-directory", "unreadable"):
                raise ScanError("invalid-cwd", "作業対象の実体を確定できません。", exit_code=2)
            raise
        except OSError:
            raise ScanError("invalid-cwd", "作業対象の実体を確定できません。", exit_code=2)
        result.update(cwd=os.fsdecode(resolved[0]), cwd_bytes_hex=resolved[0].hex())
        Scanner(result, budget).scan()
        result["candidates"].sort(key=lambda item: (ROOT_IDS.index(item["root_id"]), bytes.fromhex(item["name_bytes_hex"])))
        digest = list_digest(result)
        budget.check()
        if acknowledged is not None:
            outcome = "acknowledged" if acknowledged == digest else "list-changed"
        else:
            outcome = "confirmation-required" if result["candidates"] else "none"
        result.update(result=outcome, complete=True, list_sha256=digest)
        return (0 if outcome in ("none", "acknowledged") else 10), result
    except ScanError as exc:
        result["result"] = {2: "usage", 11: "scan-unavailable"}[exc.exit_code]
        result["issues"].append(exc.issue)
        for root in result["roots"]:
            if root["id"] == exc.issue["root_id"]:
                root.update(status="error", reason=exc.issue["code"], identity=None, canonical_id=None)
        code = exc.exit_code
    except Exception as exc:
        result["result"] = "internal"
        result["issues"].append({"code": "internal-error", "root_id": None, "path_bytes_hex": None,
                                 "message": "想定外の失敗が起きました: " + type(exc).__name__})
        code = 20
    result["candidates"].sort(key=lambda item: (ROOT_IDS.index(item["root_id"]), bytes.fromhex(item["name_bytes_hex"])))
    result["issues"].sort(key=lambda item: (item["code"], -1 if item["root_id"] is None else ROOT_IDS.index(item["root_id"]),
                                             item["path_bytes_hex"] or "", item["message"]))
    return code, result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["scan", "--help"]:
        sys.stdout.write(HELP)
        return 0
    code, result = run(argv)
    sys.stdout.write(canonical_json(result) + "\n")
    if result["issues"]:
        # 診断は固定文とOSエラー番号だけ。候補の名前はJSONにだけ出す。
        for issue in result["issues"]:
            sys.stderr.write("pending-implementation: " + issue["code"] + " "
                             + issue["message"] + "\n")
    elif code == 10:
        sys.stderr.write("保護記録候補の一覧について、人の確認が必要です。\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
