#!/usr/bin/env python3
"""保護記録らしい名前の直下エントリだけを列挙する。内部内容は読まない。

記録の外にある所属の印(owner-*)を読み、別のプロジェクトの記録と確かめられたものは止めない。
今回の作業のGitの置き場は、Gitを起動せずに .git・HEAD・commondir の属性と短い中身から決める。
"""

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
ROOT_DIGEST_KEYS = ("id", "input_path_bytes_hex", "resolved_path_bytes_hex",
                    "status", "reason", "identity", "canonical_id")
# list_sha256の入力へ写す候補の11項目。候補に項目が増えても、この名前だけを写す。
LIST_CANDIDATE_KEYS = ("root_id", "path_bytes_hex", "name_bytes_hex", "kind", "dev", "ino",
                       "mode", "uid", "size", "mtime_ns", "ctime_ns")
# identity_sha256の入力へ写す候補の8項目。更新時刻・大きさ・権限は入れない。
# reasonを入れるので、控えた後に分類が変われば(例: no-markからoverlapへ)値も変わる。
IDENTITY_CANDIDATE_KEYS = ("root_id", "path_bytes_hex", "name_bytes_hex", "kind", "dev", "ino", "uid", "reason")
# identity_sha256の入力では、置き場のidentityから権限(mode)を外す。
IDENTITY_ROOT_KEYS = ("dev", "ino", "uid")
MAX_MARK = 65536          # 所属の印の上限
MAX_POINTER = 4096        # .gitファイル・HEAD・commondirの上限
MAX_DEPTH = 128           # 上へたどる階層の上限
MARK_KEYS = ("version", "guard_dev", "guard_ino", "top", "top_dev", "top_ino", "common", "common_dev",
             "common_ino", "taskdir", "taskdir_dev", "taskdir_ino", "tmpdir")
MARK_PATH_KEYS = ("top", "common", "taskdir")
OVERLAP_REASONS = ("overlap", "overlap-unknown")
TEMP_DIRS = (b"/tmp", b"/private/tmp")
OVERLAP_LINE = "重なりのある候補があるため、identity の確認値では続行できません。list_sha256 で確認してください。"
HELP = """使用方法:
  python3 pending-implementation.py scan --cwd <作業対象の管理ルート>
      [--acknowledged-list <64桁の一覧確認値>
       | --acknowledged-identities <64桁の実体確認値>]

保護記録らしい候補を3か所の直下から列挙します。候補の内部は読みません。
記録の外の所属の印を読み、別のプロジェクトの記録と確かめられた候補では止めません。
印が無い・壊れている・今回の作業と重なる・確かめられない候補は、人の確認が必要です。
確認値は「確認が要る」候補だけで計算し、人が全候補を具体的に確認した場合だけ渡してください。
  --acknowledged-list: 一覧確認値(list_sha256)。更新時刻・大きさ・権限の変化でも変わります。
  --acknowledged-identities: 実体確認値(identity_sha256)。全候補が今回と無関係と
      人が答えた場合だけ使います。同じ実体の更新時刻・大きさ・権限の変化では変わりません。
      重なり(overlap・overlap-unknown)のある候補があれば、値が一致しても続行しません。
2つの確認値は同時に渡せません(終了値2)。
保護記録の真正性や、処理の停止を証明する値ではありません。
終了値: 0=候補なし(none)/別プロジェクトの記録だけ(other-projects-only)/確認値一致、
        10=人の確認が必要、11=列挙不能、2=引数または作業対象の不正、20=想定外の失敗。
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


def payload_digest(payload):
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def confirmation_candidates(result):
    # 確認値は「確認が要る」候補だけで計算する。別プロジェクトの記録は入れない。
    return [item for item in result["candidates"] if item["relation"] == "confirmation-required"]


def list_digest(result):
    # 入力の形(schema_version 1と候補の11項目)は、関連する記録の再開手順のために変えない。
    payload = {"schema_version": 1, "cwd_bytes_hex": result["cwd_bytes_hex"],
               "roots": [{key: root[key] for key in ROOT_DIGEST_KEYS}
                         for root in result["roots"]],
               "candidates": [{key: item[key] for key in LIST_CANDIDATE_KEYS}
                              for item in confirmation_candidates(result)]}
    return payload_digest(payload)


def identity_digest(result):
    # 人が全候補を無関係と答えたときだけ呼出側が使う。同じ実体の時刻・大きさ・権限では変わらない。
    roots = []
    for root in result["roots"]:
        item = {key: root[key] for key in ROOT_DIGEST_KEYS}
        if item["identity"] is not None:
            item["identity"] = {key: item["identity"][key] for key in IDENTITY_ROOT_KEYS}
        roots.append(item)
    payload = {"schema_version": 1, "digest": "identity", "cwd_bytes_hex": result["cwd_bytes_hex"],
               "roots": roots,
               "candidates": [{key: item[key] for key in IDENTITY_CANDIDATE_KEYS}
                              for item in confirmation_candidates(result)]}
    return payload_digest(payload)


def check_features():
    if sys.version_info < (3, 8):
        raise ScanError("unsupported-python", "Python 3.8以上が必要です。")
    if os.name != "posix":
        raise ScanError("unsupported-platform", "POSIXのファイル機能が必要です。")
    names = ("open", "close", "fstat", "stat", "scandir", "readlink", "read",
             "fsencode", "fsdecode", "getcwdb")
    missing = ["os." + name for name in names if not callable(getattr(os, name, None))]
    missing += ["os." + name for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
                if not isinstance(getattr(os, name, None), int)]
    for name, support in (("scandir", "supports_fd"), ("stat", "supports_dir_fd"),
                          ("stat", "supports_follow_symlinks"), ("open", "supports_dir_fd")):
        if getattr(os, name, None) not in getattr(os, support, ()):
            missing.append("os." + support + ":" + name)
    missing += ["stat_result.st_" + name for name in STAT_FIELDS
                if not hasattr(getattr(os, "stat_result", None), "st_" + name)]
    if not callable(getattr(time, "monotonic", None)):
        missing.append("time.monotonic")
    if missing:
        raise ScanError("missing-feature", "必要な機能がありません: " + ", ".join(missing))


def is_unsupported(exc):
    unsupported = {errno.ENOSYS, getattr(errno, "ENOTSUP", errno.ENOSYS),
                   getattr(errno, "EOPNOTSUPP", errno.ENOSYS)}
    return isinstance(exc, NotImplementedError) or getattr(exc, "errno", None) in unsupported


def raise_if_unsupported(exc):
    """必要な機能の実行時の未対応は、通常の読み取りの失敗(印の異常・決められない等)にせず、11へ伝える。

    印・.git・HEAD・commondirの読み取りや`..`を開く処理で握りつぶすと、確認値で解除できてしまうため。
    """
    if is_unsupported(exc):
        raise os_failure(exc, None, None)


def os_failure(exc, root_id, path, changed=False):
    if is_unsupported(exc):
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


def stable(values):
    """走査中に比べる項目: 番号・種類・持ち主。更新時刻・大きさ・権限は比べない。"""
    return values["dev"], values["ino"], stat.S_IFMT(values["mode"]), values["uid"]


def mark_version(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def decimal(value):
    if re.fullmatch(rb"0|[1-9][0-9]{0,19}", value) is None:
        return None
    return int(value)


def parse_mark(data):
    """印の形を確かめる。全キーが1回ずつ・version 1・十進と小文字16進・絶対パス。外れたらNone。"""
    if not data.endswith(b"\n"):
        return None
    values = {}
    for line in data[:-1].split(b"\n"):
        key, tab, value = line.partition(b"\t")
        try:
            key = key.decode("ascii")
        except UnicodeDecodeError:
            return None
        if not tab or key not in MARK_KEYS or key in values:
            return None
        values[key] = value
    if len(values) != len(MARK_KEYS) or values["version"] != b"1":
        return None
    mark = {}
    for key in MARK_KEYS[1:]:
        value = values[key]
        if key.endswith(("_dev", "_ino")):
            mark[key] = decimal(value)
            if mark[key] is None:
                return None
        elif key == "tmpdir" and not value:
            mark[key] = b""
        else:
            if re.fullmatch(rb"(?:[0-9a-f]{2})+", value) is None:
                return None
            raw = bytes.fromhex(value.decode("ascii"))
            if not raw.startswith(b"/") or b"\0" in raw:
                return None
            mark[key] = raw
    return mark


def pointer_target(data, prefix=b""):
    """.gitファイルのgitdir:やcommondirの1行を取り出す。相対パスは呼出側が基準のfdで開く。"""
    if data is None or not data.startswith(prefix):
        return None
    raw = data[len(prefix):].rstrip(b"\r\n")
    if not raw or b"\0" in raw or b"\n" in raw or b"\r" in raw:
        return None
    return raw


def head_is_valid(data):
    """HEADはブランチなどを指す書き方(ref: refs/)か、40桁か64桁の16進だけを受け入れる。"""
    if data is None:
        return False
    if data.startswith(b"ref: refs/"):
        return True
    return re.fullmatch(rb"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\r?\n?", data) is not None


def close_quietly(fd):
    try:
        os.close(fd)
    except (OSError, NotImplementedError):
        pass


class Scanner:
    def __init__(self, result, budget):
        self.result = result
        self.budget = budget
        self.opened = []
        self.canonical = {}
        self.scope_done = False
        self.scope_chains = None
        self.temp_chains = {}

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
        # 置き場の直下は別のセッションのtake・cleanupで更新時刻が変わる。番号・種類・持ち主だけを比べる。
        if (current is None or current[0].hex() != root["resolved_path_bytes_hex"]
                or stable(stat_values(current[1])) != stable(original)
                or stable(stat_values(held)) != stable(original)):
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
                    item.update(relation="confirmation-required", reason="no-mark", owner=None)
                    self.result["candidates"].append(item)
        except (OSError, NotImplementedError) as exc:
            raise os_failure(exc, root["id"], path, changed=True)

    def same_candidate(self, root, fd, item):
        # 候補の番号・種類・持ち主を比べる。更新時刻・大きさ・権限の変化では止めない(別のセッションが使っている記録)。
        # 印の持ち主の確かめは列挙のときの持ち主と比べるので、持ち主の途中の変化もここで止める。
        values = self.candidate_stat(root, fd, bytes.fromhex(item["name_bytes_hex"]))
        if stable(values) != stable(item):
            self.changed(root, bytes.fromhex(item["path_bytes_hex"]))

    def entry_stat(self, dir_fd, name):
        self.budget.check()
        try:
            return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def mark_stat(self, root, fd, name):
        self.budget.check(root["id"])
        try:
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except (OSError, NotImplementedError) as exc:
            raise_if_unsupported(exc)
            return False

    def read_small(self, dir_fd, name, before, limit):
        """名前で取った属性と同じ通常ファイルを、リンクを辿らず・書き手を待たずに開き、上限付きで読む。"""
        if before is None or before is False or not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            return None
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        except (OSError, NotImplementedError) as exc:
            raise_if_unsupported(exc)
            return None
        try:
            held = os.fstat(fd)
            if (held.st_dev, held.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(held.st_mode):
                return None
            chunks = []
            total = 0
            while total <= limit:
                self.budget.check()
                part = os.read(fd, min(8192, limit + 1 - total))
                if not part:
                    break
                chunks.append(part)
                total += len(part)
            if total > limit or total != held.st_size:
                return None
            return b"".join(chunks)
        except (OSError, NotImplementedError) as exc:
            raise_if_unsupported(exc)
            return None
        finally:
            close_quietly(fd)

    def chain(self, fd):
        """fdのフォルダから`..`を開いて根までたどった(dev, ino)の並びと、たどり切れたか。fdは閉じない。"""
        ids = []
        current = fd
        try:
            info = os.fstat(fd)
            for _ in range(MAX_DEPTH + 1):
                self.budget.check()
                ids.append((info.st_dev, info.st_ino))
                parent = os.open(b"..", os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=current)
                if current != fd:
                    close_quietly(current)
                current = parent
                info = os.fstat(current)
                if (info.st_dev, info.st_ino) == ids[-1]:
                    return ids, True
            return ids, False
        except (OSError, NotImplementedError) as exc:
            raise_if_unsupported(exc)
            return ids, False
        finally:
            if current != fd:
                close_quietly(current)

    def temp_chain(self, raw):
        """一時フォルダの祖先の並び。無いものはNone(比べない)、開けないものはたどれない扱い。"""
        if raw not in self.temp_chains:
            try:
                dfd = os.open(raw, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_CLOEXEC)
            except (FileNotFoundError, NotADirectoryError):
                self.temp_chains[raw] = None
            except (OSError, NotImplementedError) as exc:
                raise_if_unsupported(exc)
                self.temp_chains[raw] = ([], False)
            else:
                try:
                    self.temp_chains[raw] = self.chain(dfd)
                finally:
                    close_quietly(dfd)
        return self.temp_chains[raw]

    def scope(self):
        if not self.scope_done:
            self.scope_done = True
            self.scope_chains = self.resolve_scope()
        return self.scope_chains

    def resolve_scope(self):
        """今回の範囲(--cwd・最上位・Gitの置き場・共通の置き場)の祖先の並び。決められなければNone。

        Gitを起動しない。名前で属性を取ってから開き、取り直しの一致を確かめ、短い中身だけを読む。
        """
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            if os.environ.get(name):
                return None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_CLOEXEC
        fds = []
        try:
            fds.append(os.open(bytes.fromhex(self.result["cwd_bytes_hex"]), flags))
            level = fds[0]
            dotgit = None
            for _ in range(MAX_DEPTH):
                dotgit = self.entry_stat(level, b".git")
                if dotgit is not None:
                    break
                fds.append(os.open(b"..", flags, dir_fd=level))
                here, up = os.fstat(level), os.fstat(fds[-1])
                if (here.st_dev, here.st_ino) == (up.st_dev, up.st_ino):
                    return None
                level = fds[-1]
            if dotgit is None:
                return None
            top_fd = level
            if stat.S_ISDIR(dotgit.st_mode):
                fds.append(os.open(b".git", flags | os.O_NOFOLLOW, dir_fd=top_fd))
                held = os.fstat(fds[-1])
                if (held.st_dev, held.st_ino) != (dotgit.st_dev, dotgit.st_ino):
                    return None
            elif stat.S_ISREG(dotgit.st_mode):
                target = pointer_target(self.read_small(top_fd, b".git", dotgit, MAX_POINTER), b"gitdir: ")
                if target is None:
                    return None
                fds.append(os.open(target, flags, dir_fd=top_fd))
            else:
                return None
            git_fd = fds[-1]
            if not head_is_valid(self.read_small(git_fd, b"HEAD", self.entry_stat(git_fd, b"HEAD"), MAX_POINTER)):
                return None
            commondir = self.entry_stat(git_fd, b"commondir")
            common_fd = git_fd
            if commondir is not None:
                target = pointer_target(self.read_small(git_fd, b"commondir", commondir, MAX_POINTER))
                if target is None:
                    return None
                fds.append(os.open(target, flags, dir_fd=git_fd))
                common_fd = fds[-1]
            for name in (b"objects", b"refs"):
                entry = self.entry_stat(common_fd, name)
                if entry is None or not stat.S_ISDIR(entry.st_mode):
                    return None
            return [self.chain(dfd) for dfd in (fds[0], top_fd, git_fd, common_fd)]
        except (OSError, NotImplementedError) as exc:
            raise_if_unsupported(exc)
            return None
        finally:
            for dfd in reversed(fds):
                close_quietly(dfd)

    def relate(self, mark, item):
        """形の正しい印と候補から、図解の表の順に理由を決める。別プロジェクトの記録ならNone。"""
        if (mark["guard_dev"], mark["guard_ino"]) != (item["dev"], item["ino"]):
            return "guard-mismatch"
        fds = []
        try:
            for key in MARK_PATH_KEYS:
                self.budget.check()
                try:
                    fds.append(os.open(mark[key], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                       | os.O_NONBLOCK | os.O_CLOEXEC))
                    info = os.fstat(fds[-1])
                except (OSError, NotImplementedError) as exc:
                    raise_if_unsupported(exc)
                    return "owner-path-mismatch"
                if (info.st_dev, info.st_ino) != (mark[key + "_dev"], mark[key + "_ino"]):
                    return "owner-path-mismatch"
            mine = self.scope()
            if mine is None:
                return "git-unavailable"
            theirs = [self.chain(dfd) for dfd in fds]
            for raw in TEMP_DIRS + ((mark["tmpdir"],) if mark["tmpdir"] else ()):
                theirs.append(self.temp_chain(raw))
            hit = unknown = False
            for other in theirs:
                if other is None:
                    continue
                for b_ids, b_complete in mine:
                    a_ids, a_complete = other
                    if not a_ids or not b_ids:
                        unknown = True
                    elif a_ids[0] in b_ids or b_ids[0] in a_ids:
                        hit = True
                    elif not (a_complete and b_complete):
                        unknown = True
            if hit:
                return "overlap"
            return "overlap-unknown" if unknown else None
        finally:
            for dfd in reversed(fds):
                close_quietly(dfd)

    def classify(self, root, fd, item):
        """候補ごとに、同じ置き場のowner-*を名前で読み、分類する。印の中身はパスと番号だけ。"""
        name = bytes.fromhex(item["name_bytes_hex"])
        mark_name = b"owner-" + name[len(b"guard-"):]
        self.same_candidate(root, fd, item)
        before = self.mark_stat(root, fd, mark_name)
        reason, mark = "no-mark", None
        if before is False:
            reason = "mark-invalid"
        elif before is not None:
            if before.st_uid != item["uid"]:
                reason = "mark-invalid"
            else:
                mark = parse_mark(self.read_small(fd, mark_name, before, MAX_MARK) or b"")
                reason = "mark-invalid" if mark is None else None
        after = self.mark_stat(root, fd, mark_name)
        if before is None or after is None:
            reason, mark = "no-mark", None   # 走査の途中で現れた・消えた
        elif before is False or after is False or mark_version(before) != mark_version(after):
            reason, mark = "mark-invalid", None
        owner = None
        if mark is not None:
            owner = {}
            for key in MARK_PATH_KEYS:
                owner.update({key: os.fsdecode(mark[key]), key + "_bytes_hex": mark[key].hex()})
            reason = self.relate(mark, item)
        self.same_candidate(root, fd, item)
        item.update(relation="other-project" if reason is None else "confirmation-required",
                    reason=reason, owner=owner)

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
            # 最後の取り直しで印を読んで分類する。候補は番号・種類・持ち主だけを比べる。
            for root, fd, original in self.opened:
                self.check_root(root, fd, original)
                if root["canonical_id"] == root["id"]:
                    for item in self.result["candidates"]:
                        if item["root_id"] == root["id"]:
                            self.classify(root, fd, item)
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
        if name not in ("--cwd", "--acknowledged-list", "--acknowledged-identities") or name in values:
            raise ScanError("invalid-arguments", "不明または重複した引数があります。", exit_code=2)
        if not equal:
            value = next(args, None)
            if value is None or value.startswith("--"):
                raise ScanError("invalid-arguments", "引数の値がありません。", exit_code=2)
        values[name] = value
    if "--cwd" not in values:
        raise ScanError("invalid-arguments", "--cwdは必須です。", exit_code=2)
    if "--acknowledged-list" in values and "--acknowledged-identities" in values:
        raise ScanError("invalid-arguments", "2つの確認値は同時に指定できません。", exit_code=2)
    acknowledged = {}
    for name, kind in (("--acknowledged-list", "list"), ("--acknowledged-identities", "identity")):
        value = values.get(name)
        if value is None:
            continue
        if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
            raise ScanError("invalid-arguments", "確認値は64桁のASCII hexで指定してください。", exit_code=2)
        acknowledged[kind] = value.lower()
    return values["--cwd"], acknowledged


def decide_outcome(result, acknowledged, digests):
    """候補と確認値から、(終了値, 結果の値, 注記の並び)を決める。入出力をせず、resultを書き換えない。"""
    notes = []
    confirm = confirmation_candidates(result)
    if acknowledged:
        # parse_argumentsが2つの同時指定を拒むので、渡された確認値は1つだけ。
        kind, value = next(iter(acknowledged.items()))
        if kind == "identity" and any(item["reason"] in OVERLAP_REASONS for item in confirm):
            # 重なりを見つけた・確かめられなかった候補は、人が「無関係」と答えても実体の確認値で続行しない。
            outcome = "list-changed"
            notes.append(OVERLAP_LINE)
        else:
            outcome = "acknowledged" if value == digests[kind] else "list-changed"
    elif confirm:
        outcome = "confirmation-required"
    else:
        outcome = "other-projects-only" if result["candidates"] else "none"
    others = len(result["candidates"]) - len(confirm)
    if others:
        notes.insert(0, "別プロジェクトの保護記録: " + str(others) + " 件。所属の印で別のプロジェクトと確かめたため、"
                     "止めません(名前とパスはJSONにだけ出します)。")
    return (0 if outcome in ("none", "other-projects-only", "acknowledged") else 10), outcome, notes


def run(argv=None):
    """JSON出力前の結果と終了値を返す。永続状態は作らない。"""
    code, result, _ = run_detail(argv)
    return code, result


def run_detail(argv=None):
    """run と同じ結果に、標準エラーへ出す固定の注記(JSONには入れない)を足して返す。"""
    result = {"schema_version": 2, "result": "internal", "cwd": None, "cwd_bytes_hex": None,
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
        digests = {"list": list_digest(result), "identity": identity_digest(result)}
        budget.check()
        code, outcome, notes = decide_outcome(result, acknowledged, digests)
        result.update(result=outcome, complete=True, list_sha256=digests["list"],
                      identity_sha256=digests["identity"])
        return code, result, notes
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
    return code, result, []


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["scan", "--help"]:
        sys.stdout.write(HELP)
        return 0
    code, result, notes = run_detail(argv)
    sys.stdout.write(canonical_json(result) + "\n")
    if result["issues"]:
        # 診断は固定文とOSエラー番号だけ。候補の名前はJSONにだけ出す。
        for issue in result["issues"]:
            sys.stderr.write("pending-implementation: " + issue["code"] + " "
                             + issue["message"] + "\n")
        return code
    # 別プロジェクトの記録は件数だけ。名前とパスはJSONにだけ出す。
    for note in notes:
        sys.stderr.write(note + "\n")
    if code == 10:
        sys.stderr.write("保護記録候補の一覧について、人の確認が必要です。\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
