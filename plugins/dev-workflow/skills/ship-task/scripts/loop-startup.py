#!/usr/bin/env python3
"""Git を起動せず、現在の worktree と既存の中断印の場所を解決する。

設定、include、中断した周の JSON/meta は読まない。返却値は現在の管理構造の
観察であり、中断記録を信頼したり、同 UID の改竄を隔離する証明ではない。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import sys
import time

MAX_POINTER = 65536
MAX_COMPONENTS = 512
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK


class Stop(RuntimeError):
    pass


def identity(st):
    return st.st_dev, st.st_ino, st.st_mode


def version(st):
    return identity(st) + (st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class Observation:
    """親を保持し、戻る前に経路の各要素の同一性を再確認する。"""
    def __init__(self):
        self.fds = []
        self.edges = []
        self.count = 0
        self.deadline = time.monotonic() + 5

    def tick(self):
        self.count += 1
        if self.count > MAX_COMPONENTS or time.monotonic() > self.deadline:
            raise Stop("起動前の管理パス検査が上限を超えた")

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)

    def directory(self, path, missing=False):
        path = os.path.abspath(path)
        fd = os.open(os.path.sep, DIR_FLAGS)
        self.fds.append(fd)
        for name in path.split(os.path.sep)[1:]:
            if not name:
                continue
            self.tick()
            try:
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if missing:
                    self.edges.append((fd, name, None, False))
                    return None
                raise
            if not stat.S_ISDIR(before.st_mode):
                raise Stop("管理パスの親が通常ディレクトリでない")
            child = os.open(name, DIR_FLAGS, dir_fd=fd)
            self.fds.append(child)
            if identity(before) != identity(os.fstat(child)):
                raise Stop("管理パスの親が検査中に変わった")
            self.edges.append((fd, name, before, False))
            fd = child
        return fd

    def entry(self, parent, name):
        self.tick()
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        self.edges.append((parent, name, st, st is not None and not stat.S_ISDIR(st.st_mode)))
        return st

    def pointer(self, parent, name, before):
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_POINTER:
            raise Stop("管理先を示すファイルが通常形式または読取上限を満たさない")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if version(before) != version(os.fstat(fd)):
                raise Stop("管理先を示すファイルが検査中に変わった")
            chunks = bytearray()
            while len(chunks) <= before.st_size:
                self.tick()
                part = os.read(fd, min(8192, before.st_size + 1 - len(chunks)))
                if not part:
                    break
                chunks.extend(part)
            if len(chunks) != before.st_size or version(before) != version(os.fstat(fd)):
                raise Stop("管理先を示すファイルが読取中に変わった")
            return bytes(chunks)
        finally:
            os.close(fd)

    def check(self):
        for parent, name, before, exact in self.edges:
            self.tick()
            try:
                after = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                after = None
            if before is None and after is None:
                continue
            if before is None or after is None or (version if exact else identity)(before) != (version if exact else identity)(after):
                raise Stop("起動前の管理パスが検査中に変わった")


def pointer_path(raw, base, prefix=b""):
    if prefix:
        if not raw.startswith(prefix):
            raise Stop("Git 管理先の指定形式を確認できない")
        raw = raw[len(prefix):]
    raw = raw.rstrip(b"\r\n")
    if not raw or b"\0" in raw or b"\n" in raw or b"\r" in raw:
        raise Stop("Git 管理先の指定形式を確認できない")
    value = os.fsdecode(raw)
    return os.path.abspath(value if os.path.isabs(value) else os.path.join(base, value))


def kind(st):
    if st is None:
        return "absent"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "unsafe"


def lock_is_held(parent, item):
    if item is None:
        return False
    if not stat.S_ISREG(item.st_mode):
        raise Stop("保存状態のロックが通常ファイルでない")
    fd = os.open("lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if version(item) != version(os.fstat(fd)):
            raise Stop("保存状態のロックが検査中に変わった")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def binding_path(input_path, state_base):
    return os.path.join(state_base, ".repo-bind-" + hashlib.sha256(os.fsencode(input_path)).hexdigest() + ".json")


def read_binding(observation, path):
    parent = observation.directory(os.path.dirname(path), missing=True)
    if parent is None:
        return None
    item = observation.entry(parent, os.path.basename(path))
    if item is None:
        return None
    try:
        held = json.loads(observation.pointer(parent, os.path.basename(path), item))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Stop("保存した起動先の対応を読めない。前の状態を人が確認する") from exc
    if (not isinstance(held, dict) or held.get("schema") != 2 or
            any(not isinstance(held.get(key), str) for key in ("input", "top", "repo_admin", "common"))):
        raise Stop("保存した起動先の対応が不正。前の状態を人が確認する")
    if not isinstance(held.get("management"), dict):
        raise Stop("保存した管理入口の状態が無い")
    return held


def stamp(st):
    if st is None:
        return None
    return list(identity(st) if stat.S_ISDIR(st.st_mode) else version(st))


def held_entry(held, name, st):
    value = stamp(st)
    if held and (name not in held["management"] or held["management"][name] != value):
        raise Stop("保存した管理入口が変わった。前の状態と管理先の変更を人が確認する")
    return value


def resolve(repo, state_base):
    # 呼出側が選んだ repo 自体のリンクは従来の cd -P と同じく物理化する。
    # その後に読む管理ファイルや親はリンクを辿らない。
    input_path = os.path.abspath(repo)
    top = os.path.realpath(input_path)
    state_base = os.path.realpath(os.path.abspath(state_base))
    observation = Observation()
    try:
        anchor_path = binding_path(input_path, state_base)
        held = read_binding(observation, anchor_path)
        if held and (held["input"] != input_path or held["top"] != top):
            raise Stop("起動先が保存した対応と違う。前の状態と起動先の変更を人が確認する")
        root = observation.directory(top)
        dotgit = observation.entry(root, ".git")
        management = {"dotgit": held_entry(held, "dotgit", dotgit)}
        if dotgit is None:
            # 診断だけを選ぶ。bare/subdir でも Git や config 本文は使わない。
            if all(observation.entry(root, name) is not None for name in ("HEAD", "objects", "refs")):
                raise Stop("bare: bare リポジトリには使えない")
            ancestor = os.path.dirname(top)
            while ancestor != os.path.dirname(ancestor):
                parent = observation.directory(ancestor)
                if observation.entry(parent, ".git") is not None:
                    raise Stop("not-toplevel: --repo が作業ツリーのトップでない")
                ancestor = os.path.dirname(ancestor)
            raise Stop("not-git: 指定した場所に Git の管理入口が無い")
        if stat.S_ISDIR(dotgit.st_mode):
            admin = os.path.join(top, ".git")
        else:
            admin = pointer_path(observation.pointer(root, ".git", dotgit), top, b"gitdir: ")
        if held and held["repo_admin"] != admin:
            raise Stop("Git 管理先が保存した対応と違う。前の状態と管理先の変更を人が確認する")
        admin_fd = observation.directory(admin)
        management["admin"] = held_entry(held, "admin", os.fstat(admin_fd))
        commondir = observation.entry(admin_fd, "commondir")
        management["commondir"] = held_entry(held, "commondir", commondir)
        common = admin if commondir is None else pointer_path(observation.pointer(admin_fd, "commondir", commondir), admin)
        if held and held["common"] != common:
            raise Stop("共有管理先が保存した対応と違う。前の状態と管理先の変更を人が確認する")
        common_fd = observation.directory(common)
        management["common"] = held_entry(held, "common", os.fstat(common_fd))
        state_id = hashlib.sha256(os.fsencode(common)).hexdigest()[:16]
        state_path = os.path.join(state_base, state_id)
        state_fd = observation.directory(state_path, missing=True)
        state_root = {"path": state_path, "identity": None if state_fd is None else stamp(os.fstat(state_fd))}
        if held and held.get("state_root") != state_root:
            raise Stop("保存した状態の置き場が変わった。前の状態を人が確認する")
        markers = {"inflight": "absent", "stop_mark": "absent"}
        locked = False
        if state_fd is not None:
            locked = lock_is_held(state_fd, observation.entry(state_fd, "lock"))
            markers = {"inflight": kind(observation.entry(state_fd, "inflight")),
                       "stop_mark": kind(observation.entry(state_fd, "stop-mark.md"))}
            for name in ("lock", "last-verified.json"):
                if kind(observation.entry(state_fd, name)) not in ("absent", "file"):
                    raise Stop("保存状態の制御ファイルが通常ファイルでない")
            if markers["inflight"] == "directory":
                inflight_fd = observation.directory(os.path.join(state_path, "inflight"))
                for name in ("meta", "base.json"):
                    if kind(observation.entry(inflight_fd, name)) != "file":
                        raise Stop("周の途中の印が通常ファイルを持たない")
        if markers["inflight"] not in ("absent", "directory") or markers["stop_mark"] not in ("absent", "file"):
            raise Stop("中断印の種類を安全に確認できない")
        observation.check()
        return {"input": input_path, "top": top, "repo_admin": admin, "common": common,
                "state_id": state_id, "state_path": state_path,
                "state_exists": state_fd is not None, "markers": markers, "locked": locked, "management": management, "state_root": state_root,
                "binding_path": anchor_path, "bound": held is not None,
                "management_sha256": hashlib.sha256(json.dumps(management, sort_keys=True).encode()).hexdigest()}
    finally:
        observation.close()


def bind(repo, state_base, expected=None):
    """通常の Git 検証後、呼出側が作った state-base に対応だけを初回固定する。"""
    result = resolve(repo, state_base)
    if expected is not None and any(result[key] != value for key, value in expected.items()):
        raise Stop("Git 検証後に起動先が変わった")
    if result["bound"]:
        return result
    observation = Observation()
    try:
        parent = observation.directory(os.path.dirname(result["binding_path"]))
        name = os.path.basename(result["binding_path"])
        data = json.dumps({"schema": 2, **{k: result[k] for k in ("input", "top", "repo_admin", "common", "management", "state_root")}},
                          ensure_ascii=True).encode() + b"\n"
        observation.check()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            offset = 0
            while offset < len(data):
                observation.tick()
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise Stop("起動先の対応を保存できない")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent)
        observation.check()
        result["bound"] = True
        return result
    finally:
        observation.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-base", required=True)
    parser.add_argument("--bind", action="store_true")
    parser.add_argument("--reject-inflight", action="store_true")
    for name in ("top", "repo-admin", "common", "management-sha256"):
        parser.add_argument("--expect-" + name)
    args = parser.parse_args()
    try:
        if args.bind:
            expected = {key: getattr(args, "expect_" + key) for key in ("top", "repo_admin", "common", "management_sha256")}
            if any(value is None for value in expected.values()):
                raise Stop("起動先を固定する前に Git で検証した場所が必要")
            result = bind(args.repo, args.state_base, expected)
        else:
            result = resolve(args.repo, args.state_base)
        if args.reject_inflight and result["markers"]["inflight"] != "absent":
            raise Stop("未信頼の周の途中の印がある。内容を使わず人の確認まで保存する")
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (Stop, OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, Stop) else "管理パスを安全に確認できない"
        code = "startup-state"
        for candidate in ("bare", "not-toplevel", "not-git"):
            if reason.startswith(candidate + ": "):
                code, reason = candidate, reason[len(candidate) + 2:]
                break
        print("ERROR [" + code + "] " + reason, file=sys.stderr)
        return 20


if __name__ == "__main__":
    sys.exit(main())
