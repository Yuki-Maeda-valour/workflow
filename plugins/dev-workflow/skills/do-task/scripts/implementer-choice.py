#!/usr/bin/env python3
"""実装の担当の選択を利用者の設定に保存し、聞き直す時を決める補助(契約の正本は external-runners.md §12-10)。

標準ライブラリだけで動く(Python 3.10 以上)。外部 CLI を起動しない。

  status [--runner <ランナー名>]
      保存した選択だけを読む(短い形)。decision は internal か verify。
  status --runner <ランナー名> --project <管理ルート> --launch <dry-run の 1 行> --secret-set-sha256 <64 桁|unknown>
      そのリポジトリの承認の記録と照らす(長い形)。decision は external・internal・ask。
  record --choice <internal|ランナー名> --expect-store-sha256 <64 桁|absent>
         [--project <管理ルート> --launch <…> --secret-set-sha256 <64 桁> --expect-tuple-sha256 <64 桁>]
      選択を保存する。4 つの値があれば、そのリポジトリの承認も保存する。
  secret-set --cwd <管理ルート>
      外部に見える機密ファイルの集合(パスだけ)を出す。何も書かない。

保存ファイル: <$XDG_CONFIG_HOME(絶対パスのとき)か $HOME/.config>/dev-workflow/implementer-choice.json
終了コード: 0 成功 / 2 使い方の誤り / 20 書けない・部品の失敗・打ち切り・リポジトリの鍵などを求められない /
           21 record の組の不一致 / 22 record の保存ファイルの値の不一致(21・22 は何も作らず書かない)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

SCHEMA = 1
STORE_DIR = "dev-workflow"
STORE_NAME = "implementer-choice.json"
MAX_STORE = 1024 * 1024
MAX_APPROVALS = 8
MAX_REPOS = 64
MAX_LAUNCH = 4096
MAX_CONFIG_ITEMS = 2000
MAX_CONFIG_BYTES = 16 * 1024 * 1024
PART_TIMEOUT = 60
TIMEOUT_ENV = "DEV_WORKFLOW_IMPLEMENTER_CHOICE_TIMEOUT"
RUNNER_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
STAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
REPO_ID_RE = re.compile(r"[0-9]{1,20}")
# 外部 CLI がリポジトリの中から読む設定のフォルダの名前(ランナーごと)。
# 一覧に無いランナーは値を求められない(終了コード 20)。実装用既定表(external-runners.md §12-6)に
# ランナーを足すときは、ここにも足す。設定のフォルダを読まないと確かめたランナーは空の組にする。
RUNNER_CONFIG_DIRS = {"codex": (".codex",)}
APPROVAL_KEYS = ("repo_id", "runner", "launch_sha256", "secret_set_sha256", "runner_config_sha256", "approved_at")
GROUP_KEYS = APPROVAL_KEYS[:-1]
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
# base-commit.md の Git の安全用のオプション(implementation-git.py と同じ並び)
SAFE_GIT = (
    "--no-pager", "--no-replace-objects", "-c", "core.quotePath=false",
    "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
    "-c", "core.ignoreCase=false", "-c", "core.splitIndex=false",
    "-c", "core.ignoreStat=false", "-c", "commit.gpgSign=false",
    "-c", "push.gpgSign=false", "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.clean=", "-c", "filter.lfs.process=",
    "-c", "filter.lfs.required=false",
)
PATHSPEC_ENV = ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS",
                "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS")
TARGET_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


class Failure(Exception):
    """理由を 1 行で持つ失敗。code は終了コード。"""

    def __init__(self, message, code=20):
        super().__init__(message)
        self.message = message
        self.code = code


class Unusable(Failure):
    """置き場か保存ファイルが安全でない・壊れている(status では store-unusable、record では 20)。"""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure(f"使い方の誤り: {message}", 2)


def one_line(text):
    return str(text).replace("\r", "\\r").replace("\n", "\\n")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True) + "\n")


# ── 部品の呼び出し(git・secret-profiles.py・diff-snapshot.sh・grep)──

def part_timeout():
    raw = os.environ.get(TIMEOUT_ENV, "")
    if re.fullmatch(r"[0-9]{1,2}", raw) and 1 <= int(raw) <= PART_TIMEOUT:
        return int(raw)
    return PART_TIMEOUT


def clean_env():
    """継いだ Git の対象の指定と設定の注入を外し、遅延取得と任意のロックと入力の催促を止める。"""
    env = dict(os.environ)
    for key in list(env):
        if key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS") or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del env[key]
    for key in PATHSPEC_ENV + TARGET_ENV:
        env.pop(key, None)
    env.update(GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    return env


def run_part(argv, label, env, data=None):
    """部品を新しいプロセスグループで流し、時間を超えたらグループごと止める。(終了コード, 標準出力) を返す。"""
    limit = part_timeout()
    try:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ValueError) as exc:
        raise Failure(f"{label} を起動できない({type(exc).__name__})") from None
    try:
        out, _ = proc.communicate(input=data, timeout=limit)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.kill()
        proc.communicate()
        raise Failure(f"{label} が {limit} 秒で終わらないので打ち切った") from None
    return proc.returncode, out


def run_git(cwd, env, *args):
    """Git の唯一の起動点。失敗したら(git でない・壊れたリポジトリ)Failure。"""
    code, out = run_part(["git", "-C", cwd, *SAFE_GIT, *args], "git", env)
    if code:
        raise Failure(f"git {args[0]} が失敗した(終了コード {code}。git のリポジトリでないか、壊れている)")
    return out


def git_path(cwd, env, *args):
    out = run_git(cwd, env, *args)
    if not out.endswith(b"\n") or out.count(b"\n") != 1:
        raise Failure("git の出力の形が違う")
    value = os.fsdecode(out[:-1])
    if not value.startswith("/"):
        raise Failure("git が絶対パスを返さない")
    return value


# ── 安全な読み書き(host-check.py の open_dir_chain・read_at・write_new_file・ensure_store の形)──

def split_abs(path):
    if not path.startswith("/"):
        raise Unusable("絶対パスが要る")
    parts = path.split("/")[1:]
    if path == "/":
        return []
    if any(p in ("", ".", "..") for p in parts):
        raise Unusable("正規化されていないパス")
    return parts


def open_dir_chain(parts):
    """'/' から parts を nofollow で辿ったディレクトリの fd を返す。"""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def file_identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def read_at(dir_fd, name, limit):
    """通常のファイルを nofollow で読み、読む前後で同じ物かを確かめる。(stat, 中身) を返す。"""
    before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise Unusable("通常のファイルでない")
    if before.st_size > limit:
        raise Unusable("大きさの上限を超える")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if file_identity(os.fstat(fd)) != file_identity(before):
            raise Unusable("開く間に変わった")
        chunks, left = [], before.st_size
        while left:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                raise Unusable("読む間に短くなった")
            chunks.append(chunk)
            left -= len(chunk)
        if os.read(fd, 1):
            raise Unusable("読む間に大きくなった")
        if file_identity(os.fstat(fd)) != file_identity(before) or \
                file_identity(os.stat(name, dir_fd=dir_fd, follow_symlinks=False)) != file_identity(before):
            raise Unusable("読む間に変わった")
        return before, b"".join(chunks)
    finally:
        os.close(fd)


def write_new_file(dir_fd, name, raw):
    """同じディレクトリに O_EXCL・0600 の一時ファイルを作り、fsync・rename・親の fsync の順に置き換える。"""
    tmp = f".{name}.{os.getpid()}.tmp"
    out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    done = False
    try:
        try:
            os.fchmod(out, 0o600)
            view = memoryview(raw)
            while view:
                view = view[os.write(out, view):]
            os.fsync(out)
        finally:
            os.close(out)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        done = True
        os.fsync(dir_fd)
    finally:
        if not done:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass


def under(path, base):
    return path == base or base == "/" or path.startswith(base + "/")


# ── 保存ファイル ──

class Store:
    """置き場を確かめて保存ファイルを読んだ結果。何も作らない。"""

    def __init__(self, top=None):
        self.path = None
        self.sha = "unreadable"
        self.data = None
        self.problem = None
        self.missing = []
        self.parts = []
        try:
            self._inspect(top)
        except Unusable as exc:
            self.problem = exc.message
        except OSError as exc:
            self.problem = f"置き場を安全に開けない({exc.strerror or type(exc).__name__})"

    @property
    def absent(self):
        return self.problem is None and self.data is None

    def _inspect(self, top):
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        if xdg.startswith("/"):
            base = xdg
        else:
            home = os.environ.get("HOME", "")
            if not home.startswith("/"):
                raise Unusable("HOME も XDG_CONFIG_HOME も絶対パスでないので、置き場が無い")
            base = os.path.join(home, ".config")
        real = os.path.realpath(os.path.join(base, STORE_DIR))
        self.path = os.path.join(real, STORE_NAME)
        forbidden = [os.path.realpath("/tmp")]
        tmpdir = os.environ.get("TMPDIR", "")
        if tmpdir.startswith("/"):
            forbidden.append(os.path.realpath(tmpdir))
        if top is not None:
            forbidden.append(top)
        for item in forbidden:
            if under(real, item):
                raise Unusable(f"置き場が外部の CLI の書き込み範囲になりうる所({item})の下にある: {real}")
        self.parts = split_abs(real)
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, name in enumerate(self.parts):
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    self.missing = self.parts[index:]
                    self.sha = "absent"
                    return
                os.close(fd)
                fd = child
            check_store_dir(os.fstat(fd))
            try:
                info, raw = read_at(fd, STORE_NAME, MAX_STORE)
            except FileNotFoundError:
                self.sha = "absent"
                return
        finally:
            os.close(fd)
        self.sha = sha256(raw)
        if info.st_uid != os.geteuid():
            raise Unusable("保存ファイルの持ち主が実行者でない")
        if info.st_mode & 0o077:
            raise Unusable("保存ファイルをほかの人が読み書きできる(0600 でない)")
        self.data = parse_store(raw)

    def count(self):
        if self.problem is not None:
            return None
        if self.data is None:
            return 0
        return sum(len(items) for items in self.data["repos"].values())

    def choice(self):
        return None if self.data is None else self.data["choice"]


def check_store_dir(info):
    if not stat.S_ISDIR(info.st_mode):
        raise Unusable("置き場がディレクトリでない")
    if info.st_uid != os.geteuid():
        raise Unusable("置き場の持ち主が実行者でない")
    if info.st_mode & 0o022:
        raise Unusable("置き場をグループかほかの人が書ける")


def parse_store(raw):
    def pairs(items):
        keys = [key for key, _ in items]
        if len(set(keys)) != len(keys):
            raise ValueError("鍵が重なる")
        return dict(items)

    def no_constant(name):
        raise ValueError(name)

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Unusable("保存ファイルが JSON として読めない") from None
    if not isinstance(value, dict) or set(value) != {"schema", "choice", "repos"}:
        raise Unusable("保存ファイルの鍵が違う")
    if type(value["schema"]) is not int or value["schema"] != SCHEMA:
        raise Unusable("保存ファイルの schema が違う")
    choice = value["choice"]
    if not isinstance(choice, str) or not (choice == "internal" or runner_name(choice)):
        raise Unusable("保存ファイルの choice が違う")
    repos = value["repos"]
    if not isinstance(repos, dict) or len(repos) > MAX_REPOS:
        raise Unusable("保存ファイルの repos が違う")
    for key, items in repos.items():
        if not key.startswith("/") or "\0" in key:
            raise Unusable("保存ファイルのリポジトリの鍵が違う")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_APPROVALS:
            raise Unusable("保存ファイルの承認の数が違う")
        for item in items:
            check_approval(item)
    return value


def check_approval(item):
    if not isinstance(item, dict) or set(item) != set(APPROVAL_KEYS):
        raise Unusable("保存ファイルの承認の鍵が違う")
    if not all(isinstance(item[key], str) for key in APPROVAL_KEYS):
        raise Unusable("保存ファイルの承認の値が文字列でない")
    if not REPO_ID_RE.fullmatch(item["repo_id"]) or not runner_name(item["runner"]):
        raise Unusable("保存ファイルの承認の repo_id か runner が違う")
    for key in ("launch_sha256", "secret_set_sha256", "runner_config_sha256"):
        if not HEX64_RE.fullmatch(item[key]):
            raise Unusable(f"保存ファイルの承認の {key} が違う")
    if not STAMP_RE.fullmatch(item["approved_at"]):
        raise Unusable("保存ファイルの承認の approved_at が違う")
    try:
        datetime.datetime.strptime(item["approved_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise Unusable("保存ファイルの承認の approved_at が日時でない") from None


def runner_name(value):
    return bool(RUNNER_RE.fullmatch(value)) and value != "internal"


def serialize(data):
    return (json.dumps(data, ensure_ascii=True, indent=2) + "\n").encode("ascii")


def save(store, raw, expect):
    """足りないディレクトリを 0700 で作り、書く直前に保存ファイルの値をもう一度照らしてから置き換える。"""
    keep = len(store.parts) - len(store.missing)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, name in enumerate(store.parts):
            made = False
            if index >= keep:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                    made = True
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if made:
                os.fchmod(fd, 0o700)
        check_store_dir(os.fstat(fd))
        try:
            _, now = read_at(fd, STORE_NAME, MAX_STORE)
            current = sha256(now)
        except FileNotFoundError:
            current = "absent"
        if current != expect:
            raise Failure("保存ファイルが、覚えた値のあとに書き換わった(読み直して書かない)", 22)
        write_new_file(fd, STORE_NAME, raw)
    finally:
        os.close(fd)


# ── リポジトリの鍵と外部 CLI のリポジトリの設定 ──

class Repo:
    def __init__(self, project, runner, env):
        if not os.path.isdir(project):
            raise Failure("--project がディレクトリでない")
        common = os.path.realpath(git_path(project, env, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        top = os.path.realpath(git_path(project, env, "rev-parse", "--show-toplevel"))
        try:
            self.key_bytes = common.encode("utf-8")
        except UnicodeEncodeError:
            raise Failure("リポジトリの鍵を UTF-8 で表せない") from None
        self.key = common
        try:
            self.repo_id = str(os.stat(common).st_ino)
        except OSError as exc:
            raise Failure(f"リポジトリの鍵を stat できない({exc.strerror})") from None
        self.top = top
        project_real = os.path.realpath(project)
        if not under(project_real, top):
            raise Failure("--project がリポジトリの作業ツリーの中でない")
        self.runner = runner
        self.config = runner_config_sha256(runner, top, project_real)

    def tuple_sha256(self, launch_sha, secret):
        if secret == "unknown":
            return None
        fields = (self.key_bytes, self.repo_id.encode("ascii"), self.runner.encode("ascii"),
                  launch_sha.encode("ascii"), secret.encode("ascii"), self.config.encode("ascii"))
        return sha256(b"".join(field + b"\0" for field in fields))


def runner_config_sha256(runner, top, project):
    if runner not in RUNNER_CONFIG_DIRS:
        raise Failure(f"ランナー {runner} の、リポジトリの中の設定のフォルダの一覧が無い"
                      "(implementer-choice.py の RUNNER_CONFIG_DIRS に無いので求められない)")
    names = RUNNER_CONFIG_DIRS[runner]
    if not names:
        return EMPTY_SHA256
    rel = os.path.relpath(project, top)
    chain = [] if rel == "." else rel.split("/")
    items = []
    budget = {"items": 0, "bytes": 0}
    try:
        fd = open_dir_chain(split_abs(top))
        try:
            prefix = b""
            for depth in range(len(chain) + 1):
                if depth:
                    child = os.open(chain[depth - 1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                    prefix += os.fsencode(chain[depth - 1]) + b"/"
                for name in names:
                    walk_config(fd, os.fsencode(name), prefix + os.fsencode(name), items, budget)
        finally:
            os.close(fd)
    except Unusable as exc:
        raise Failure(f"外部 CLI のリポジトリの設定を読めない({exc.message})") from None
    except OSError as exc:
        raise Failure(f"外部 CLI のリポジトリの設定を読めない({exc.strerror or type(exc).__name__})") from None
    items.sort(key=lambda item: item[0])
    return sha256(b"".join(path + b"\0" + kind + b"\0" + value + b"\0" for path, kind, value in items))


def walk_config(dir_fd, name, rel, items, budget):
    """設定のフォルダを symlink をたどらずに 1 項目ずつ歩き、(相対パス, 種類, 値) を items に足す。

    中の名前は os.scandir で 1 つずつ読む。項目が上限を超えたら、その時点で止める。
    """
    stack = []  # (開いた fd, その fd の scandir, 相対パス)
    try:
        child = visit_config(dir_fd, name, rel, items, budget)
        if child is not None:
            stack.append(child)
        while stack:
            fd, entries, prefix = stack[-1]
            entry = next(entries, None)
            if entry is None:
                stack.pop()
                try:
                    entries.close()
                finally:
                    os.close(fd)
                continue
            sub = os.fsencode(entry.name)
            child = visit_config(fd, sub, prefix + b"/" + sub, items, budget)
            if child is not None:
                stack.append(child)
    finally:
        for fd, entries, _ in stack:
            try:
                entries.close()
            finally:
                os.close(fd)


def visit_config(parent, entry, path, items, budget):
    """1 つの項目を items に足す。ディレクトリなら (開いた fd, その scandir, 相対パス) を返す。

    symlink があれば、リンク先の変化を値に入れられないので、値を求められないとする(終了コード 20)。
    """
    def add(kind, value=b""):
        budget["items"] += 1
        if budget["items"] > MAX_CONFIG_ITEMS:
            raise Failure(f"外部 CLI のリポジトリの設定の項目が {MAX_CONFIG_ITEMS} 個を超える")
        items.append((path, kind, value))

    try:
        info = os.stat(entry, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        add(b"absent")
        return None
    if stat.S_ISLNK(info.st_mode):
        raise Failure(f"外部 CLI のリポジトリの設定に symlink がある({os.fsdecode(path)}。"
                      "リンク先の変化を値に入れられないので求められない)")
    if stat.S_ISDIR(info.st_mode):
        add(b"dir")
        child = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            entries = os.scandir(child)
        except BaseException:
            os.close(child)
            raise
        return child, entries, path
    if stat.S_ISREG(info.st_mode):
        add(b"file", read_config_file(parent, entry, budget).encode("ascii"))
    else:
        add(b"other")
    return None


def read_config_file(parent, entry, budget):
    fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Failure("外部 CLI のリポジトリの設定が読む間に変わった")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return digest.hexdigest()
            budget["bytes"] += len(chunk)
            if budget["bytes"] > MAX_CONFIG_BYTES:
                raise Failure("外部 CLI のリポジトリの設定の中身が 16 MiB を超える")
            digest.update(chunk)
    finally:
        os.close(fd)


# ── サブコマンド ──

def check_launch(launch):
    if not launch or "\n" in launch or "\r" in launch:
        raise Failure("--launch は空でない 1 行が要る", 2)
    try:
        raw = launch.encode("utf-8")
    except UnicodeEncodeError:
        raise Failure("--launch を UTF-8 で表せない", 2) from None
    if len(raw) > MAX_LAUNCH:
        raise Failure(f"--launch が {MAX_LAUNCH} バイトを超える", 2)
    return sha256(raw)


def check_runner(value, option):
    if not runner_name(value):
        raise Failure(f"{option} のランナー名が違う(^[a-z][a-z0-9-]{{0,31}}$ で、internal でない)", 2)


def check_hex(value, option, extra=()):
    if value in extra or HEX64_RE.fullmatch(value):
        return
    allowed = " か ".join(["64 桁の小文字の 16 進", *extra])
    raise Failure(f"{option} は {allowed} が要る", 2)


def unusable_note(store):
    print(f"ERROR [implementer-choice] 保存ファイルを使わない: {one_line(store.problem)}"
          f"(保存ファイル: {one_line(store.path or '置き場なし')})", file=sys.stderr)


def cmd_status(a):
    long_args = (a.project, a.launch, a.secret_set_sha256)
    if a.runner is not None:
        check_runner(a.runner, "--runner")
    if any(v is not None for v in long_args):
        if not all(v is not None for v in long_args) or a.runner is None:
            raise Failure("--runner・--project・--launch・--secret-set-sha256 はそろえて渡す", 2)
        return status_long(a)
    store = Store()
    if store.problem is not None:
        unusable_note(store)
        decision, reason = "verify", "store-unusable"
    elif store.absent:
        decision, reason = "verify", "unset"
    elif store.choice() == "internal":
        decision, reason = "internal", "saved-internal"
    elif a.runner is not None and store.choice() != a.runner:
        decision, reason = "verify", "runner-mismatch"
    else:
        decision, reason = "verify", "saved-runner"
    emit({"decision": decision, "reason": reason, "choice": store.choice(), "store": store.path,
          "store_sha256": store.sha, "approval_count": store.count()})
    return 0


def status_long(a):
    launch_sha = check_launch(a.launch)
    check_hex(a.secret_set_sha256, "--secret-set-sha256", ("unknown",))
    repo = Repo(a.project, a.runner, clean_env())
    store = Store(repo.top)
    approved_at = None
    if store.problem is not None:
        unusable_note(store)
        decision, reason = "ask", "store-unusable"
    elif store.absent:
        decision, reason = "ask", "unset"
    elif store.choice() == "internal":
        decision, reason = "internal", "saved-internal"
    elif store.choice() != a.runner:
        decision, reason = "ask", "runner-mismatch"
    else:
        found = [item for item in store.data["repos"].get(repo.key, [])
                 if item["repo_id"] == repo.repo_id and item["runner"] == a.runner]
        decision, reason = "ask", "new-project"
        if found:
            found = [item for item in found if item["launch_sha256"] == launch_sha]
            reason = "launch-changed"
        if found:
            found = [item for item in found if item["runner_config_sha256"] == repo.config]
            reason = "runner-config-changed"
        if found:
            if a.secret_set_sha256 == "unknown":
                found, reason = [], "secret-set-unknown"
            else:
                found = [item for item in found if item["secret_set_sha256"] == a.secret_set_sha256]
                reason = "secret-set-changed"
        if found:
            decision, reason = "external", "approved"
            approved_at = max(item["approved_at"] for item in found)
    emit({"decision": decision, "reason": reason, "choice": store.choice(), "store": store.path,
          "store_sha256": store.sha, "approved_at": approved_at,
          "tuple_sha256": repo.tuple_sha256(launch_sha, a.secret_set_sha256)})
    return 0


def cmd_record(a):
    if a.choice != "internal":
        check_runner(a.choice, "--choice")
    check_hex(a.expect_store_sha256, "--expect-store-sha256", ("absent",))
    four = (a.project, a.launch, a.secret_set_sha256, a.expect_tuple_sha256)
    given = [v is not None for v in four]
    if any(given) and not all(given):
        raise Failure("--project・--launch・--secret-set-sha256・--expect-tuple-sha256 はそろえて渡すか、どれも渡さない", 2)
    repo = launch_sha = None
    if all(given):
        if a.choice == "internal":
            raise Failure("--choice internal には承認の 4 つの値を付けない", 2)
        launch_sha = check_launch(a.launch)
        check_hex(a.secret_set_sha256, "--secret-set-sha256")
        check_hex(a.expect_tuple_sha256, "--expect-tuple-sha256")
        repo = Repo(a.project, a.choice, clean_env())
    store = Store(repo.top if repo else None)
    if store.problem is not None:
        raise Unusable(f"保存ファイルを使わない: {store.problem}(保存ファイル: {store.path or '置き場なし'})")
    if store.sha != a.expect_store_sha256:
        raise Failure("保存ファイルが、覚えた値のあとに書き換わった(読み直して書かない)", 22)
    if repo is not None and repo.tuple_sha256(launch_sha, a.secret_set_sha256) != a.expect_tuple_sha256:
        raise Failure("人に見せた後に、外部 CLI のリポジトリの設定かリポジトリが変わった(判定をやり直す)", 21)
    data = store.data or {"schema": SCHEMA, "choice": a.choice, "repos": {}}
    data = {"schema": SCHEMA, "choice": a.choice, "repos": dict(data["repos"])}
    if repo is not None:
        add_approval(data["repos"], repo, launch_sha, a.secret_set_sha256)
    raw = serialize(data)
    if len(raw) > MAX_STORE:
        raise Failure("書いた後の保存ファイルが 1 MiB を超えるので書かない")
    try:
        save(store, raw, store.sha)
    except Unusable as exc:
        raise Unusable(f"保存ファイルを使わない: {exc.message}(保存ファイル: {store.path})") from None
    emit({"choice": a.choice, "store": store.path, "store_sha256": sha256(raw),
          "approval_count": sum(len(items) for items in data["repos"].values())})
    return 0


def add_approval(repos, repo, launch_sha, secret):
    """承認を足す。同じ組なら approved_at だけを更新する。上限を超えたら、今の承認と今の鍵のほかで最も古いものを消す。"""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"repo_id": repo.repo_id, "runner": repo.runner, "launch_sha256": launch_sha,
             "secret_set_sha256": secret, "runner_config_sha256": repo.config, "approved_at": now}
    group = tuple(entry[key] for key in GROUP_KEYS)
    items = [dict(item) for item in repos.get(repo.key, [])]
    same = [index for index, item in enumerate(items) if tuple(item[key] for key in GROUP_KEYS) == group]
    for index in same:
        items[index]["approved_at"] = now
    if same:
        keep = same[0]
    else:
        items.append(entry)
        keep = len(items) - 1
    # 同じ時刻なら、並びの前にある方を古いとみなす
    while len(items) > MAX_APPROVALS:
        oldest = min((index for index in range(len(items)) if index != keep),
                     key=lambda index: (items[index]["approved_at"], index))
        del items[oldest]
        if oldest < keep:
            keep -= 1
    repos[repo.key] = items
    while len(repos) > MAX_REPOS:
        others = [key for key in repos if key != repo.key]
        oldest = min(range(len(others)),
                     key=lambda index: (max(item["approved_at"] for item in repos[others[index]]), index))
        del repos[others[oldest]]


def cmd_secret_set(a):
    env = clean_env()
    if not os.path.isdir(a.cwd):
        raise Failure("--cwd がディレクトリでない")
    top = git_path(a.cwd, env, "rev-parse", "--show-toplevel")
    here = Path(__file__).resolve().parent
    code, out = run_part([sys.executable, "-B", str(here / "secret-profiles.py"), "--cwd", a.cwd],
                         "secret-profiles.py", env)
    if code or not out.endswith(b"\0"):
        raise Failure(f"機密の glob を読めない(secret-profiles.py の終了コード {code})")
    globs = [os.fsdecode(item) for item in out[:-1].split(b"\0")]
    argv = ["bash", str(here / "diff-snapshot.sh"), "--print-exclude-ere"]
    for glob in globs:
        argv += ["--exclude-glob", glob]
    code, out = run_part(argv, "diff-snapshot.sh", env)
    if code or not out.endswith(b"\n") or out.count(b"\n") != 1 or len(out) < 2:
        raise Failure(f"機密の glob を正規表現にできない(diff-snapshot.sh の終了コード {code})")
    ere = os.fsdecode(out[:-1])
    listed = run_git(top, env, "ls-files", "-z", "--cached", "--others")
    paths = list(dict.fromkeys(item for item in listed.split(b"\0") if item))
    if paths:
        code, out = run_part(["grep", "-Eiz", "--", ere], "grep", dict(env, LC_ALL="C"),
                             data=b"".join(path + b"\0" for path in paths))
        if code not in (0, 1) or (code == 0 and not out.endswith(b"\0")):
            raise Failure(f"機密のパスを当てられない(grep の終了コード {code})")
        hits = list(dict.fromkeys(item for item in out.split(b"\0") if item)) if code == 0 else []
    else:
        hits = []
    hits.sort()
    emit({"count": len(hits), "paths": [os.fsdecode(item) for item in hits], "globs": globs,
          "sha256": sha256(b"".join(item + b"\0" for item in hits))})
    return 0


def build_parser():
    parser = Parser(prog="implementer-choice.py", description=__doc__.split("\n\n")[0],
                    formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", allow_abbrev=False, help="保存した選択を読む(--project などを付けると承認の記録と照らす)")
    status.add_argument("--runner")
    status.add_argument("--project")
    status.add_argument("--launch")
    status.add_argument("--secret-set-sha256")
    record = sub.add_parser("record", allow_abbrev=False, help="選択(と承認)を保存する")
    record.add_argument("--choice", required=True)
    record.add_argument("--expect-store-sha256", required=True)
    record.add_argument("--project")
    record.add_argument("--launch")
    record.add_argument("--secret-set-sha256")
    record.add_argument("--expect-tuple-sha256")
    secret = sub.add_parser("secret-set", allow_abbrev=False, help="外部に見える機密ファイルの集合を出す")
    secret.add_argument("--cwd", required=True)
    return parser


def main(argv=None):
    try:
        a = build_parser().parse_args(argv)
        if a.command == "status":
            return cmd_status(a)
        if a.command == "record":
            return cmd_record(a)
        return cmd_secret_set(a)
    except Failure as exc:
        print(f"ERROR [implementer-choice] {one_line(exc.message)}", file=sys.stderr)
        return exc.code
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        detail = getattr(exc, "strerror", None) or type(exc).__name__
        print(f"ERROR [implementer-choice] 処理を終えられない({one_line(detail)})", file=sys.stderr)
        return 20


if __name__ == "__main__":
    sys.exit(main())
