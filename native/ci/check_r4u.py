#!/usr/bin/env python3
"""R4U(#480)の比較器: 旧版 check_links.py(本家 CPython 3.10.11)・固定期待・Rust の検査用の実行物を比べる。

使い方:
    python3 -B native/ci/check_r4u.py --fixture <Rust の検査用の実行物>
        cargo test(native/crates/cli/tests/check_links.rs)から呼ばれる。必須のケースを全部比べ、
        ID ごとの結果と、組ごとの件数を出す。
    python3 -B native/ci/check_r4u.py --measure-cases [--out <ファイル>]
        Linux で旧版を動かし、ケースごとの固定期待(cases-linux.json)を作る。
    python3 -B native/ci/check_r4u.py --measure-step2 [--out <ファイル>]
        手順 2(呼出履歴を出している間)の書込みの失敗と SIGINT の部品ごとの表(step2.json)を作る。

- 旧版を動かす Python は R4U_PYTHON(無ければ PATH の python3)。版と、対応づける標準ライブラリ
  (pathlib.py・posixpath.py・genericpath.py)の SHA-256 を記録する。本家 3.10.11 と違う中身のファイルの
  フレームを含むケースは失敗にする(成功にも skip にもしない)。
- 子の環境は、許すものを並べて一から作る(PATH・LC_ALL=C と、ケースが与える変数と、書込みの記録の 3 つ)。
  子の中で共有ライブラリが環境変数の名前を記録し、ほかの変数が残っていないことと、ケースが与えた変数が
  消えていないことを確かめる。
- write(2) は native/ci/r4u_write_shim.c を一時フォルダで作り、旧版と Rust の両方に同じ形で差し込んで記録する。
- 呼出履歴の File 欄は、旧スクリプトと本家の標準ライブラリのパスだけを固定名(check_links.py・pathlib.py など)に
  する。対応づけは、書込みの区切りと行の始まりで呼出履歴の構造を読み、フレームの行のパスの欄だけに当てる。
- 書込みが途中までしか通らない故障で、固定名の欄を含む書込みが切れるときは、2026-11-11 22:26・22:38 の決定
  (#471 の設計の (ア)(イ))のとおりに Rust の期待を作る。
- Mac で行わなくてよいケースは、設計の閉じた一覧(/dev/full が無い・非 UTF-8 の名前を作れない・旧版の試しの
  書込みが記録に現れない)だけ。件数と理由を出し、成功に数えない。
"""
from __future__ import annotations

import argparse
import ctypes
import difflib
import errno
import hashlib
import json
import os
import pty
import re
import resource
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
LEGACY_REL = "plugins/dev-workflow/skills/update-doc/scripts/check_links.py"
LEGACY = ROOT / LEGACY_REL
LEGACY_SHA256 = "b2eea09e6b0c1b4f84b8c4b36f2d04aa517c88b3eb410c5a19e619457e09d4ab"
FIXTURES = ROOT / "native/fixtures/r4u"
CASES_FIXTURE = FIXTURES / "cases-linux.json"
STEP2_FIXTURE = FIXTURES / "step2.json"
CORPUS_FIXTURE = FIXTURES / "matcher-corpus.json"
CORPUS_GENERATOR = FIXTURES / "gen_matcher_corpus.py"
SHIM_SOURCE = Path(__file__).with_name("r4u_write_shim.c")
STDLIB_SHA256 = {
    "pathlib.py": "936f14a3ecc4ac5e896e4d673666d76b25a9d581f3b4c8d8d424e830fda2a69b",
    "posixpath.py": "2c770bac0680a5a180b1310102d14e1e120f2f2e8e47925b1574ec808cb8ad6d",
    "genericpath.py": "9d58ad64056a89f12258a62596c2073cd9e52b459dde152f710edc0cffcd865b",
}
FIXED_NAMES = frozenset({b"check_links.py", *(name.encode() for name in STDLIB_SHA256)})
LINUX = sys.platform.startswith("linux")
MAC = sys.platform == "darwin"
PRELOAD = "DYLD_INSERT_LIBRARIES" if MAC else "LD_PRELOAD"
SHIM_KEYS = (PRELOAD, "R4U_SHIM_LOG", "R4U_SHIM_RULES")
FIXTURE_FLAG = "--check-links-fixture"
MATCHER_FLAG = "--check-links-matcher"
RUN_TIMEOUT = 120
TEMP_PREFIX = "r4u-check-"
ROOT_MARK = b"@ROOT@"
TOP_MARK = b"@TOP@"
TOPNAME_MARK = b"@TOPNAME@"
LEGACY_MARK = b"@LEGACY@"
FILE_PREFIX = b'  File "'
FRAME_RE = re.compile(rb'  File "([^"\n]*)", line (\d+), in ([^\n]*)')
THREAD_RE = re.compile(rb"Current thread 0x[0-9a-f]{16} \(most recent call first\):\n")
THREAD_VALUE_RE = re.compile(rb"^[0-9a-f]{16}$")
EFBIG = errno.EFBIG
EAGAIN = errno.EAGAIN

# Mac で行わなくてよいケースの閉じた一覧(設計の「テスト計画」)。ほかの未実施は失敗。
SKIP_DEVFULL = "/dev/full が無い"
SKIP_NON_UTF8 = "非 UTF-8 の名前の作成が EILSEQ か EINVAL で拒否された"
SKIP_NO_RECORD = "旧版の試しの書込みが記録に現れない"
CLOSED_LIST = (SKIP_DEVFULL, SKIP_NON_UTF8, SKIP_NO_RECORD)

# 必須のケースの組(設計の「テスト計画」の順)。
GROUPS = {
    "basic": "基本", "args": "引数", "walk": "走査", "body": "本文", "code": "コード中のパス", "output": "出力",
    "fault": "故障", "partial": "部分書込み(固定名の欄)", "signal": "信号", "stage": "段階ごとの失敗と中断",
    "encoding": "文字コードの設定", "env": "環境変数", "corpus": "照合の生成入力",
}

# ケースが子に与えてよい変数。ほか(22:31〈22:35〉と 00:28 の決定で比較の前提の外にした変数)は与えない。
LAUNCHER_VARIABLE = "__PYVENV_LAUNCHER__"
CASE_ENV_ALLOWED = frozenset({"PYTHONOPTIMIZE", "PYTHONHASHSEED", "PYTHONINTMAXSTRDIGITS", "PYTHONUNBUFFERED",
                              "LANG", "PYTHONIOENCODING", "PYTHONUTF8"})


class Failure(Exception):
    """比較の不合格。"""


class Skip(Exception):
    """閉じた一覧に当たる未実施(Mac だけ)。理由は CLOSED_LIST のどれか。"""


# ---- 旧 Python ----

@dataclass(frozen=True)
class Python:
    path: str
    version: str
    dirs: tuple
    sha256: dict
    mismatched: frozenset
    # Mac の framework の起動用のプログラム(R4U_PYTHON などが指したもの)。本体を直接起動したときだけ入る。
    launcher: str | None = None


PROBE = r"""
import hashlib, json, os, platform, sys, sysconfig
base = os.path.dirname(os.__file__)
dirs = [base, sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")]
digest = {}
for name in ("pathlib.py", "posixpath.py", "genericpath.py"):
    with open(os.path.join(base, name), "rb") as stream:
        digest[name] = hashlib.sha256(stream.read()).hexdigest()
print(json.dumps({"version": platform.python_version(), "dirs": dirs, "sha256": digest,
                  "executable": sys.executable, "base_prefix": sys.base_prefix,
                  "framework": sysconfig.get_config_var("PYTHONFRAMEWORK") or "",
                  "stdout": [sys.stdout.encoding, sys.stdout.errors],
                  "stderr": [sys.stderr.encoding, sys.stderr.errors],
                  "environ": sorted(os.environ)}))
"""


def child_env(case_env, shim=None, log=None, rules=""):
    """子の環境を一から作る。PATH と LC_ALL のほかは、ケースが与えた変数と書込みの記録の 3 つだけ。"""
    for key in case_env:
        if key not in CASE_ENV_ALLOWED:
            raise Failure(f"比較の前提の外の変数をケースが子に与えようとした: {key}")
    env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C", **case_env}
    if shim is not None:
        env.update({PRELOAD: str(shim), "R4U_SHIM_LOG": log, "R4U_SHIM_RULES": rules})
    return env


def check_python(info):
    """PROBE の結果から、比べてよい Python かを決める。本家と違う中身のファイルの名前を返す。"""
    if not info["version"].startswith("3.10."):
        raise Failure(f"Python 3.10 系で流す(実際は {info['version']})。R4U_PYTHON を渡す")
    if info["stdout"] != ["utf-8", "surrogateescape"] or info["stderr"] != ["utf-8", "backslashreplace"]:
        raise Failure(f"基準の環境で旧 Python の文字コードが違う: {info['stdout']} {info['stderr']}")
    if info["environ"] != ["LC_ALL", "PATH"]:
        raise Failure(f"子の環境に余計な変数がある: {info['environ']}")
    return frozenset(name for name, digest in STDLIB_SHA256.items() if info["sha256"].get(name) != digest)


def direct_executable(path, base_prefix, framework, mac=MAC):
    """実際に動く Python の本体のパス。

    Mac の framework の Python(CI の arm64 は python.org の pkg)では、bin/python3.10 は起動用のプログラム
    (Mac/Tools/pythonw.c)で、__PYVENV_LAUNCHER__ を付けて <base_prefix>/Resources/Python.app/Contents/MacOS/
    <PYTHONFRAMEWORK> を posix_spawn(POSIX_SPAWN_SETEXEC)で起動し直す。書込みの記録と環境の確かめを
    実際に動くプログラムに当てるため、本体を直接起動する。framework でない Python(Linux・CI の Mac x86_64 の
    tar)は、そのまま使う。
    """
    if not mac or not framework:
        return path
    app = os.path.join(base_prefix, "Resources", "Python.app", "Contents", "MacOS", framework)
    if not os.path.isfile(app) or not os.access(app, os.X_OK):
        raise Failure(f"framework の Python の本体が見つからない: {app}")
    return app


def this_python():
    """自分を動かしている Python の本体(自己検査が子を起動するときに使う)。"""
    import sysconfig
    return direct_executable(sys.executable, sys.base_prefix, sysconfig.get_config_var("PYTHONFRAMEWORK") or "")


def probe_python(path):
    with tempfile.TemporaryDirectory(prefix="r4u-probe-") as temp:
        done = subprocess.run([path, "-c", PROBE], cwd=temp, env=child_env({}), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if done.returncode != 0 or done.stderr:
        raise Failure(f"旧版を動かす Python を調べられない: {done.stderr!r}")
    return json.loads(done.stdout.decode("utf-8"))


def find_python():
    given = os.environ.get("R4U_PYTHON") or shutil.which("python3")
    if not given:
        raise Failure("旧版を動かす Python が見つからない(R4U_PYTHON か PATH の python3)")
    info = probe_python(given)
    path = direct_executable(given, info["base_prefix"], info["framework"])
    launcher = None
    if path != given:
        launcher = given
        info = probe_python(path)
    mismatched = check_python(info)
    dirs = []
    for directory in info["dirs"]:
        for form in (directory, os.path.realpath(directory)):
            if form not in dirs:
                dirs.append(form)
    return Python(path, info["version"], tuple(dirs), info["sha256"], mismatched, launcher)


# ---- 書込みの記録 ----

def build_shim(temp):
    out = Path(temp) / ("r4u_write_shim.dylib" if MAC else "r4u_write_shim.so")
    command = ["cc", "-O1", "-Wall", "-Wextra", "-Werror", "-fPIC",
               "-dynamiclib" if MAC else "-shared", "-o", str(out), str(SHIM_SOURCE)]
    if LINUX:
        command.append("-ldl")
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f"書込みの記録の共有ライブラリを作れない: {exc}") from exc
    if done.returncode != 0:
        raise Failure("書込みの記録の共有ライブラリを作れない: " + done.stderr.decode("utf-8", "replace"))
    return out


@dataclass(frozen=True)
class Write:
    fd: int
    data: bytes
    length: int
    ret: int
    errno: int
    action: str


def read_log(data):
    """記録の行を読む。戻り値は (write の列, 環境変数の名前の集合か None, 実行の連鎖のプログラムの列)。

    - 途中で切れた行は失敗。
    - 環境の行は、プログラムが始まるたびに 1 行ある。exec の連鎖(同じ pid)なら、最後の行が実際に動いた
      プログラムの環境。連鎖の前のプログラムが書いていたら失敗、別の pid(子のプロセス)の行があれば失敗。
    """
    if data and not data.endswith(b"\n"):
        raise Failure("書込みの記録が途中で切れた")
    writes = []
    images = []
    for line in data.decode("ascii").splitlines():
        record = json.loads(line)
        if "env" in record:
            exe = bytes.fromhex(record.get("exe", ""))
            if writes:
                raise Failure(f"exec の連鎖の前のプログラムが書いた後に、次のプログラム({exe!r})が始まった")
            names = {bytes.fromhex(name).decode("utf-8", "surrogateescape") for name in record["env"]}
            images.append((names, record.get("pid"), exe))
            continue
        writes.append(Write(record["fd"], bytes.fromhex(record["data"]), record["len"], record["ret"],
                            record["errno"], record["action"]))
    if len({pid for _, pid, _ in images}) > 1:
        raise Failure("子が別のプロセスを起動し、そこでも記録した")
    return writes, (images[-1][0] if images else None), [exe for _, _, exe in images]


# ---- 入力の木 ----

def raw(value):
    if isinstance(value, dict):
        if set(value) != {"hex"}:
            raise Failure(f"バイト列の形が違う: {value!r}")
        return bytes.fromhex(value["hex"])
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def valid_utf8(data):
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def create(path, action):
    """1 つの名前を作る。Mac で非 UTF-8 の名前が拒否されたら、閉じた一覧の条件に当たる。"""
    try:
        action()
    except OSError as exc:
        if MAC and exc.errno in (errno.EILSEQ, errno.EINVAL) and not valid_utf8(path):
            raise Skip(SKIP_NON_UTF8) from exc
        raise


def build_tree(root, ops):
    """`ops` は legacy-cases.json の形(dir・file・symlink・fifo)か、(root) を受ける関数。"""
    for op in ops:
        if callable(op):
            op(root)
            continue
        path = os.path.join(os.fsencode(root), raw(op["path"]))
        kind = op["op"]
        if kind == "dir":
            create(path, lambda: os.mkdir(path))
        elif kind == "file":
            def make_file():
                with open(path, "xb") as stream:
                    stream.write(raw(op["data"]))
            create(path, make_file)
        elif kind == "symlink":
            create(path, lambda: os.symlink(raw(op["target"]), path))
        elif kind == "fifo":
            create(path, lambda: os.mkfifo(path))
        else:
            raise Failure(f"未知の操作: {kind}")


def make_deep(path, depth, name):
    """深い木を fd から辿って作る(パスの長さの上限を超えても作れる)。"""
    os.mkdir(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        for _ in range(depth):
            os.mkdir(name, dir_fd=fd)
            child = os.open(name, os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)
    return path


def make_chain(directory, count):
    for index in range(count):
        os.symlink(f"l{index + 1}", os.path.join(directory, f"l{index}"))


def restore_and_remove(root):
    """外した権限を戻してから、深い木も含めて消す。"""
    if not os.path.lexists(root):
        return
    if not os.path.isdir(root) or os.path.islink(root):
        os.unlink(root)
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            st = os.lstat(current)
            if stat.S_ISDIR(st.st_mode):
                os.chmod(current, 0o700)
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False) and len(entry.path) < 900:
                            stack.append(entry.path)
        except OSError:
            continue
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(20000)
    try:
        shutil.rmtree(root)
    finally:
        sys.setrecursionlimit(limit)


def snapshot(root):
    """入力の木の中身(種類・権限・大きさ・時刻・中身・リンク先)。深い所は fd から辿る。

    FIFO は中身を持たないので、比較器が書いて変わる時刻を見ない(種類・権限・inode は見る)。
    """
    found = {}

    def visit(dir_fd, relative):
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as exc:
            found[relative + b"/<list>"] = ("error", exc.errno)
            return
        for name in names:
            name = os.fsencode(name)
            path = relative + b"/" + name
            try:
                st = os.lstat(name, dir_fd=dir_fd)
            except OSError as exc:
                found[path] = ("error", exc.errno)
                continue
            if stat.S_ISFIFO(st.st_mode):
                found[path] = ((st.st_mode, st.st_ino), None)
                continue
            attributes = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
            body = None
            if stat.S_ISLNK(st.st_mode):
                body = os.readlink(name, dir_fd=dir_fd)
            elif stat.S_ISREG(st.st_mode):
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK, dir_fd=dir_fd)
                except OSError as exc:
                    body = ("error", exc.errno)
                else:
                    with os.fdopen(fd, "rb") as stream:
                        body = hashlib.sha256(stream.read()).hexdigest()
            found[path] = (attributes, body)
            if stat.S_ISDIR(st.st_mode):
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
                except OSError as exc:
                    found[path + b"/<open>"] = ("error", exc.errno)
                    continue
                try:
                    visit(child, path)
                finally:
                    os.close(child)

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        visit(fd, b"")
    finally:
        os.close(fd)
    return found


# ---- ケース ----

@dataclass
class Case:
    id: str
    group: str
    tree: list = field(default_factory=list)
    argv: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    stdin: str = "devnull"
    stdout: str = "pipe"
    stderr: str = "pipe"
    rules: str = ""
    scenario: str | None = None
    # True: 中断の比べ方。None: 旧版が SIGINT で終わったときだけ中断の比べ方。
    interrupt: bool | None = False
    fatal: bool = False
    expected: dict | None = None
    expected_os: str | None = None
    preserve: bool = True
    rlimit_fsize: int | None = None
    sigint_ignored: bool = False
    # 親が子へ渡す SIGPIPE の扱い("default" か "ignore")。None は subprocess の既定(既定の扱いに戻す)。
    sigpipe: str | None = None
    # 文字コードの設定だけで旧版が変わる条件: Rust はこの設定を読まないので、設定なしの旧版と比べる。
    # 設定ありの旧版は、設定ごとの別の期待(固定期待の configured)と比べる。
    encoding_env: dict | None = None
    encoding_differs: bool = True
    encoding_fatal: bool = False
    # 入力の木の中の作業場所(相対)。None は木の根。
    cwd: str | None = None
    # /tmp の直下に作る一時フォルダ(@TOP@)の中の木と、その隣に置く md の中身を作る関数(None は作らない)。
    top_tree: list = field(default_factory=list)
    top_file: object = None
    # 部分書込みのケース: (種類, 位置, ...)。種類は quota・lost(ア)、resend・cut(イ)。
    partial: tuple | None = None
    # 故障なしの旧版の結果から規則を決める(戻り値は rules の文字列)。
    derive: object = None
    # cases-linux.json の固定期待を持つか(readdir の順に依るケースは持たない)。
    fixture: bool = True
    step2: dict | None = None


@dataclass
class Result:
    code: int | None
    stdout: bytes
    stderr: bytes
    writes: list
    timed_out: bool = False
    env_names: set | None = None


@dataclass
class Obs:
    """比べる形にした結果(固定名にした記録と出力)。"""
    exit: int
    stdout: bytes
    stderr: bytes
    writes: list | None
    frames: list


class Context:
    def __init__(self, python, rust, shim, temp, top):
        self.python = python
        self.rust = rust
        self.shim = shim
        self.temp = Path(temp)
        self.root = str(self.temp / "case")
        self.top = top
        self.counter = 0
        self.legacy_recorded = True
        self.note = ""
        self.measured = {}
        self.norm = Normalizer(python)

    def marks(self):
        top = os.fsencode(self.top)
        return [(os.fsencode(self.root), ROOT_MARK), (top, TOP_MARK), (os.path.basename(top), TOPNAME_MARK),
                (os.fsencode(str(LEGACY)), LEGACY_MARK)]


def stdio_target(spec, temp, closers, pty_masters, which):
    """標準入出力の 1 つを用意する。戻り値は Popen に渡す値(None は閉じる)。"""
    if spec == "pipe":
        return subprocess.PIPE
    if spec == "stdout":
        return subprocess.STDOUT
    if spec == "devnull":
        return subprocess.DEVNULL
    if spec == "devfull":
        if not os.path.exists("/dev/full"):
            raise Skip(SKIP_DEVFULL)
        fd = os.open("/dev/full", os.O_WRONLY)
        closers.append(fd)
        return fd
    if spec == "file":
        path = temp / f"{which}.out"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        closers.append(fd)
        return fd
    if spec == "dir":
        fd = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
        closers.append(fd)
        return fd
    if spec == "broken":
        read_end, write_end = os.pipe()
        os.close(read_end)
        closers.append(write_end)
        return write_end
    if spec == "eagain":
        read_end, write_end = os.pipe()
        os.set_blocking(write_end, False)
        try:
            while True:
                os.write(write_end, b"x" * 65536)
        except BlockingIOError:
            pass
        closers.append(write_end)
        closers.append(("drain", read_end))
        return write_end
    if spec == "pty":
        master, slave = pty.openpty()
        closers.append(slave)
        pty_masters.append((which, master))
        return slave
    if spec == "closed":
        return None
    raise Failure(f"未知の標準入出力の指定: {spec}")


def read_all(fd, sink):
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        sink.append(chunk)


def wait_until(predicate, what, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise Failure(f"待ちきれない: {what}")


class MacTask:
    """Mac の proc_pidinfo(PROC_PIDTASKINFO)で、動いているスレッドの数と Unix の呼出しの回数を読む。"""
    FLAVOR = 4
    SIZE = 96

    def __init__(self):
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)

    def read(self, pid):
        buffer = ctypes.create_string_buffer(self.SIZE)
        got = self.lib.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(self.FLAVOR), ctypes.c_uint64(0), buffer,
                                    ctypes.c_int(self.SIZE))
        if got != self.SIZE:
            return None
        syscalls = struct.unpack_from("<i", buffer.raw, 76)[0]
        threads, running = struct.unpack_from("<ii", buffer.raw, 84)
        return threads, running, syscalls


def blocked(pid, proc):
    """子が FIFO を開く待ち(書き手を待つ)で止まっているか。

    Linux は /proc/<pid>/wchan。Mac は、動いているスレッドが 0 で、Unix の呼出しの回数が 0.3 秒変わらない状態。
    """
    if proc.poll() is not None:
        raise Failure("子が FIFO の待ちに入る前に終わった")
    if LINUX:
        try:
            with open(f"/proc/{pid}/wchan") as stream:
                return stream.read() in ("fifo_open", "wait_for_partner")
        except OSError:
            return False
    task = MacTask()
    first = task.read(pid)
    if first is None or first[0] < 1 or first[1] != 0:
        return False
    for _ in range(30):
        time.sleep(0.01)
        now = task.read(pid)
        if now is None or now[1] != 0 or now[2] != first[2]:
            return False
    return True


def opened(pid, target):
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return False
    for name in names:
        try:
            if os.readlink(f"/proc/{pid}/fd/{name}") == target:
                return True
        except OSError:
            pass
    return False


def resolve_arg(ctx, value):
    if callable(value):
        return value(ctx)
    return raw(value)


def prepare_inputs(ctx, case):
    restore_and_remove(ctx.root)
    os.mkdir(ctx.root, 0o700)
    build_tree(ctx.root, case.tree)
    restore_and_remove(ctx.top)
    restore_and_remove(ctx.top + ".md")
    os.mkdir(ctx.top, 0o700)
    build_tree(ctx.top, case.top_tree)
    if case.top_file is not None:
        with open(ctx.top + ".md", "xb") as stream:
            stream.write(case.top_file(ctx.top))


def input_state(ctx):
    state = {"root": snapshot(ctx.root), "top": snapshot(ctx.top)}
    try:
        st = os.lstat(ctx.top + ".md")
    except FileNotFoundError:
        state["top_file"] = None
    else:
        state["top_file"] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino,
                             Path(ctx.top + ".md").read_bytes())
    return state


def execute(ctx, case, kind, *, env=None, rules=None, rlimit=None):
    """1 回の実行。入力の木は毎回同じ絶対パスに作り直す。"""
    prepare_inputs(ctx, case)
    before = input_state(ctx) if case.preserve else None
    ctx.counter += 1
    work = ctx.temp / f"run-{ctx.counter}"
    work.mkdir()
    log_read, log_write = os.pipe()
    extra = case.env if env is None else env
    rules = case.rules if rules is None else rules
    rlimit = case.rlimit_fsize if rlimit is None else rlimit
    child = child_env(extra, ctx.shim, f"fd:{log_write}", rules)
    argv = [resolve_arg(ctx, arg) for arg in case.argv]
    if kind == "legacy":
        command = [ctx.python.path, str(LEGACY), *argv]
    else:
        command = [ctx.rust, FIXTURE_FLAG, *argv]
    cwd = ctx.root if case.cwd is None else os.path.join(ctx.root, case.cwd)
    closers = [log_write]
    masters = []
    fds = {}
    closed = []
    proc = None
    log_sink = []
    try:
        for which, spec in (("stdin", case.stdin), ("stdout", case.stdout), ("stderr", case.stderr)):
            target = stdio_target(spec, work, closers, masters, which)
            fds[which] = target
            if target is None:
                closed.append({"stdin": 0, "stdout": 1, "stderr": 2}[which])
                fds[which] = subprocess.DEVNULL

        def preexec():
            for fd in closed:
                os.close(fd)
            if rlimit is not None:
                resource.setrlimit(resource.RLIMIT_FSIZE, (rlimit, rlimit))
            if case.sigint_ignored:
                signal.signal(signal.SIGINT, signal.SIG_IGN)
            if case.sigpipe is not None:
                signal.signal(signal.SIGPIPE, {"default": signal.SIG_DFL, "ignore": signal.SIG_IGN}[case.sigpipe])

        proc = subprocess.Popen(command, cwd=cwd, env=child, stdin=fds["stdin"], stdout=fds["stdout"],
                                stderr=fds["stderr"], preexec_fn=preexec, pass_fds=(log_write,))
        log_reader = threading.Thread(target=read_all, args=(log_read, log_sink), daemon=True)
        log_reader.start()
        for fd in closers:
            if isinstance(fd, int):
                os.close(fd)
        closers = [fd for fd in closers if not isinstance(fd, int)]
        pty_out = {}
        readers = []
        for which, master in masters:
            sink = []
            pty_out[which] = sink
            reader = threading.Thread(target=read_all, args=(master, sink), daemon=True)
            reader.start()
            readers.append(reader)
        try:
            run_scenario(ctx, case, proc)
        except BaseException:
            proc.kill()
            proc.communicate()
            raise
        try:
            stdout, stderr = proc.communicate(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return Result(None, b"", b"", [], True)
        for reader in readers:
            reader.join(timeout=30)
        log_reader.join(timeout=30)
        stdout = stdout or b""
        stderr = stderr or b""
        if case.stdout == "file":
            stdout = (work / "stdout.out").read_bytes()
        if case.stderr == "file":
            stderr = (work / "stderr.out").read_bytes()
        if "stdout" in pty_out:
            stdout = b"".join(pty_out["stdout"])
        if "stderr" in pty_out:
            stderr = b"".join(pty_out["stderr"])
    finally:
        for item in closers:
            if isinstance(item, tuple):
                os.close(item[1])
            else:
                os.close(item)
        for _, master in masters:
            os.close(master)
        os.close(log_read)
    writes, names, chain = read_log(b"".join(log_sink))
    check_env_names(f"{case.id}/{kind}", names, child, kind == "rust" or ctx.legacy_recorded, chain)
    if case.preserve and input_state(ctx) != before:
        raise Failure(f"{case.id}/{kind}: 入力の木が変わった(またはファイルが増えた)")
    leftover = sorted(set(os.listdir(work)) - {"stdout.out", "stderr.out"})
    if leftover:
        raise Failure(f"{case.id}/{kind}: 作業フォルダに新しいファイルができた: {leftover}")
    return Result(proc.returncode, stdout, stderr, writes, False, names)


def check_env_names(where, names, child, recorded, chain=()):
    """子の中で共有ライブラリが記録した環境変数の名前が、比較器が許して並べた変数と同じか。

    `__PYVENV_LAUNCHER__` は、Mac の framework の起動用のプログラム(Mac/Tools/pythonw.c)が本体を起動し直すときに
    付ける変数。2026-11-12 00:28 の決定で効果を再現しない変数なので、比較の前提の外として子に入れない
    (起動用を通さず、本体を直接起動する)。入っていたら、その理由を示して失敗にする。
    """
    if names is None:
        if recorded:
            raise Failure(f"{where}: 子の環境の記録が無い(共有ライブラリが読み込まれていない)")
        return
    if LAUNCHER_VARIABLE in names and LAUNCHER_VARIABLE not in child:
        raise Failure(f"{where}: Mac の framework の起動用のプログラムが {LAUNCHER_VARIABLE} を付けた"
                      f"(実行の連鎖 {[os.fsdecode(exe) for exe in chain]})。比較の前提の外の変数なので、"
                      "本体を直接起動する")
    allowed = {"PATH", "LC_ALL", *SHIM_KEYS} | CASE_ENV_ALLOWED
    if set(child) - allowed:
        raise Failure(f"{where}: 比較の前提の外の変数を子に渡した: {sorted(set(child) - allowed)}")
    if names != set(child):
        raise Failure(f"{where}: 子の環境が、許した変数の並びと違う(余計 {sorted(names - set(child))}・"
                      f"欠け {sorted(set(child) - names)})")


def run_scenario(ctx, case, proc):
    if case.scenario is None:
        return
    pid = proc.pid
    if case.scenario in ("fifo-sigint", "fifo-sigterm", "fifo-ignored", "fifo-write"):
        wait_until(lambda: blocked(pid, proc), f"{case.id}: FIFO を開く待ち")
        if case.scenario == "fifo-sigint":
            proc.send_signal(signal.SIGINT)
        elif case.scenario == "fifo-sigterm":
            proc.send_signal(signal.SIGTERM)
        else:
            if case.scenario == "fifo-ignored":
                proc.send_signal(signal.SIGINT)
                time.sleep(0.05)
            fifo = os.path.join(ctx.root, "f", "a.md")
            writer = os.open(fifo, os.O_WRONLY)
            os.write(writer, b"[x](m)\n")
            os.close(writer)
        return
    if case.scenario == "getcwd-removed":
        work = os.path.join(ctx.root, "w")
        fifo = os.path.join(work, "a.md")
        wait_until(lambda: blocked(pid, proc), f"{case.id}: FIFO を開く待ち")
        writer = os.open(fifo, os.O_WRONLY)
        try:
            if LINUX:
                wait_until(lambda: opened(pid, fifo), f"{case.id}: FIFO を開いた")
            os.unlink(fifo)
            os.rmdir(work)
            os.write(writer, b"[x](b)\n")
        finally:
            os.close(writer)
        return
    raise Failure(f"未知の進め方: {case.scenario}")


# ---- 固定名の対応づけ ----

class Normalizer:
    """旧版の呼出履歴の File 欄だけを固定名にする。それ以外の欄・行・stdout には触れない。

    呼出履歴の構造で読む: フレームの行(`  File "<パス>", line <数>, in <名前>`)は、書込みの始まりか、
    書込みの中の改行の直後にだけ現れる(C の書出しは部品ごとに write へ渡り、フレームの行は 1 回の書出し)。
    その位置のパスが、旧スクリプトの起動に渡した絶対パスか、旧 Python の導入先の置き場(os.__file__ の親・
    sysconfig の stdlib・platstdlib。解決しない形と解決した形)に固定名を連結したパスと、文字列で同じときだけ
    置き換える。既に固定名ならそのまま(Rust)。ほかのパスがフレームの位置にあれば失敗にする。
    """

    def __init__(self, python):
        names = {os.fsencode(str(LEGACY)): b"check_links.py"}
        for directory in python.dirs:
            for name in STDLIB_SHA256:
                names[os.fsencode(os.path.join(directory, name))] = name.encode()
        self.names = names
        self.mismatched = {name.encode() for name in python.mismatched}

    def map(self, data, at_start=True, frames=None):
        starts = [0] if at_start else []
        starts += [m.end() for m in re.finditer(b"\n", data)]
        out = bytearray()
        position = 0
        for start in starts:
            if not data.startswith(FILE_PREFIX, start):
                continue
            found = FRAME_RE.match(data, start)
            if found is None:
                continue
            path = found.group(1)
            if path in self.names:
                fixed = self.names[path]
            elif path in FIXED_NAMES:
                fixed = path
            else:
                raise Failure(f"想定外の File 欄: {path!r}")
            if fixed in self.mismatched:
                raise Failure(f"本家 3.10.11 と中身が違う {fixed.decode()} のフレームを含む(行番号を比べられない)")
            out += data[position:found.start(1)] + fixed
            position = found.end(1)
            if frames is not None:
                frames.append([fixed.decode(), int(found.group(2)),
                               found.group(3).decode("utf-8", "surrogateescape")])
        out += data[position:]
        return bytes(out)

    def text(self, data, frames=None):
        """記録が無いとき(Mac で旧版が記録に現れない)の、捕まえた出力の対応づけ。行の始まりだけを見る。"""
        return self.map(data, True, frames)


def map_records(norm, writes, partial=False):
    """記録の列を固定名にする。戻り値は ((fd, 中身, 戻り値, errno, 動作) の列, フレームの列)。

    - 書込みが途中で切れ、その中身に固定名の欄があるとき(固定名にすると中身が変わるとき):
      partial が偽なら失敗(部分書込みの規則に当たらない)。真なら、22:26・22:38 の決定(#471 の (イ))のとおり、
      固定名にした中身を同じバイト数で切る。その残りの送り直し(続く書込みの頭)は、固定名にした中身の残りにする。
    - 戻り値は、全部が通れば "full"、途中までなら通ったバイト数、失敗なら -1。
    """
    mapped = []
    frames = []
    pending = {}
    for write in writes:
        if write.fd not in (1, 2):
            continue
        data = write.data
        head_raw = head_fixed = b""
        rest = data
        at_start = True
        held = pending.get(write.fd)
        if held is not None:
            if data.startswith(held[0]):
                head_raw, head_fixed = held
                rest = data[len(head_raw):]
                at_start = head_raw.endswith(b"\n")
            else:
                del pending[write.fd]
        body = norm.map(rest, at_start, frames) if write.fd == 2 else rest
        new = head_fixed + body
        if 0 <= write.ret < write.length:
            if new != data:
                if not partial:
                    raise Failure("部分書込みの規則に当たらないのに、固定名の欄を含む書込みが途中で切れた")
                if write.ret >= len(new):
                    raise Failure("固定名にした書込みは切れない(旧版だけが切れる)のに、(イ)の期待を作ろうとした")
            ret = write.ret
            pending[write.fd] = (data[write.ret:], new[write.ret:])
        elif write.ret == write.length:
            pending.pop(write.fd, None)
            ret = "full"
        else:
            ret = write.ret
        mapped.append((write.fd, new, ret, write.errno if write.ret < 0 else 0, write.action))
    return mapped, frames


def passed(mapped, fds):
    out = bytearray()
    for fd, data, ret, _, _ in mapped:
        if fd in fds:
            out += data if ret == "full" else data[:max(ret, 0)]
    return bytes(out)


def raw_passed(writes, fds):
    out = bytearray()
    for write in writes:
        if write.fd in fds:
            out += write.data[:max(write.ret, 0)]
    return bytes(out)


def check_capture(case, result):
    """捕まえた出力が、記録の通ったバイトと同じか(記録漏れと余計な出力を拒む)。"""
    if case.stdout in ("pipe", "file"):
        fds = (1, 2) if case.stderr == "stdout" else (1,)
        if result.stdout != raw_passed(result.writes, fds):
            raise Failure("捕まえた stdout と書込みの記録が合わない")
    elif case.stdout == "pty":
        if result.stdout != raw_passed(result.writes, (1,)).replace(b"\n", b"\r\n"):
            raise Failure("捕まえた疑似端末の出力と書込みの記録が合わない")
    if case.stderr in ("pipe", "file"):
        if result.stderr != raw_passed(result.writes, (2,)):
            raise Failure("捕まえた stderr と書込みの記録が合わない")


def mask_thread(data):
    """起動の失敗の診断の Current thread の値を、形式を確かめてから伏せる。"""
    found = THREAD_RE.findall(data)
    if len(found) != 1:
        raise Failure(f"Current thread の行が 1 つでない: {len(found)}")
    return THREAD_RE.sub(b"Current thread 0x<thread> (most recent call first):\n", data)


def mask_thread_writes(writes):
    result = []
    previous = None
    count = 0
    for write in writes:
        fd, data, ret, err, action = write
        if previous == b"Current thread 0x" and fd == 2:
            if not THREAD_VALUE_RE.match(data):
                raise Failure(f"Current thread の値の形式が違う: {data!r}")
            data = b"<thread>"
            count += 1
        previous = data if fd == 2 else previous
        result.append((fd, data, ret, err, action))
    return result, count


def observe(ctx, case, result, recorded=True, partial=False, fatal=None):
    """1 回の実行を比べる形にする。"""
    if result.timed_out:
        raise Failure("時間切れ")
    if type(result.code) is not int:
        raise Failure("未実行")
    fatal = case.fatal if fatal is None else fatal
    if recorded:
        mapped, frames = map_records(ctx.norm, result.writes, partial)
        check_capture(case, result)
        if case.stderr == "stdout":
            stdout = passed(mapped, (1, 2))
            stderr = b""
        else:
            stdout = result.stdout
            stderr = passed(mapped, (2,)) if case.stderr in ("pipe", "file") else result.stderr
    else:
        mapped = None
        frames = []
        stdout = ctx.norm.text(result.stdout) if case.stderr == "stdout" else result.stdout
        stderr = ctx.norm.text(result.stderr, frames)
    if fatal:
        if case.stderr == "pipe":
            stderr = mask_thread(stderr)
        if mapped is not None:
            mapped, count = mask_thread_writes(mapped)
            if count != 1:
                raise Failure(f"Current thread の値の書込みが 1 回でない: {count}")
    return Obs(result.code, stdout, stderr, mapped, frames)


def last_line(data):
    lines = data.rstrip(b"\n").split(b"\n")
    return lines[-1] if lines else b""


def unlabeled(writes):
    return None if writes is None else [w[:4] for w in writes]


def compare_obs(case_id, legacy, rust, interrupt, labels=True):
    """旧版から作った期待と Rust を比べる。中断の比べ方は、信号で終わること・終了値・最後の行・stdout。"""
    if legacy.exit != rust.exit:
        raise Failure(f"{case_id}: 終了値が違う(旧版 {legacy.exit}・Rust {rust.exit})")
    if legacy.stdout != rust.stdout:
        raise Failure(f"{case_id}: stdout が違う\n旧版 {legacy.stdout[:2000]!r}\nRust {rust.stdout[:2000]!r}")
    if interrupt:
        if legacy.exit != -signal.SIGINT:
            raise Failure(f"{case_id}: 旧版が SIGINT で終わっていない({legacy.exit})")
        if last_line(legacy.stderr) != b"KeyboardInterrupt" or last_line(rust.stderr) != b"KeyboardInterrupt":
            raise Failure(f"{case_id}: 最後の行が KeyboardInterrupt でない")
        if legacy.writes is not None and \
                [w for w in legacy.writes if w[0] == 1] != [w for w in rust.writes if w[0] == 1]:
            raise Failure(f"{case_id}: stdout の書込みの記録が違う")
        return
    if legacy.stderr != rust.stderr:
        diff = "\n".join(difflib.unified_diff(
            legacy.stderr.decode("utf-8", "replace").splitlines(),
            rust.stderr.decode("utf-8", "replace").splitlines(), "旧版", "Rust", lineterm=""))
        raise Failure(f"{case_id}: stderr が違う\n{diff}")
    if legacy.writes is not None:
        left, right = (legacy.writes, rust.writes) if labels else (unlabeled(legacy.writes), unlabeled(rust.writes))
        if left != right:
            raise Failure(f"{case_id}: 書込みの記録が違う\n旧版 {left!r}\nRust {right!r}")
    if legacy.writes is not None and legacy.frames != rust.frames:
        raise Failure(f"{case_id}: フレームの列が違う\n旧版 {legacy.frames}\nRust {rust.frames}")


# ---- 固定期待の形 ----

def apply_marks(data, marks):
    for value, mark in marks:
        if value:
            data = data.replace(value, mark)
    return data


def blob(data):
    if len(data) > 4096:
        return {"sha256": hashlib.sha256(data).hexdigest(), "len": len(data)}
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return {"hex": data.hex()}


def digest_writes(writes, marks, labels=True):
    rows = [[fd, apply_marks(data, marks).hex(), ret, err] + ([action] if labels else [])
            for fd, data, ret, err, action in writes]
    text = json.dumps(rows, separators=(",", ":"))
    return {"count": len(rows), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def canonical(obs, marks, interrupt=False, labels=True):
    """固定期待に置く形(作業場所などの印を当て、大きいものは SHA-256 にする)。

    書込みの記録が無いとき(Mac で旧版が記録に現れないとき)は、記録とフレームの欄を null にする。
    """
    records = obs.writes is not None
    if interrupt:
        return {"exit": obs.exit, "stdout": blob(apply_marks(obs.stdout, marks)),
                "last_line": blob(last_line(obs.stderr)),
                "stdout_writes": digest_writes([w for w in obs.writes if w[0] == 1], marks) if records else None}
    return {"exit": obs.exit, "stdout": blob(apply_marks(obs.stdout, marks)),
            "stderr": blob(apply_marks(obs.stderr, marks)),
            "writes": digest_writes(obs.writes, marks, labels) if records else None,
            "frames": obs.frames if records else None}


def without_records(entry):
    """記録とフレームの欄を null にした形(記録の無い側と比べるとき)。"""
    entry = dict(entry)
    for key in ("writes", "frames", "stdout_writes"):
        if key in entry:
            entry[key] = None
    return entry


# ---- 部分書込みの期待(22:26・22:38 の決定。#471 の (ア)(イ)) ----

def fd2(writes):
    return [w for w in writes if w.fd == 2]


def quota_position(position, fixed, legacy):
    """合計の上限 Q を、固定名にした stderr の流れ(fixed)の上の位置で決める。"""
    first = fixed.find(b"\n" + FILE_PREFIX) + 1
    if first <= 0:
        raise Failure("固定名にした流れにフレームの行が無い")
    name_start = first + len(FILE_PREFIX)
    name_end = fixed.index(b'"', name_start)
    if position == "field":
        return name_start + (name_end - name_start) // 2
    if position == "outside":
        source = fixed.index(b"\n", name_end) + 1
        if not fixed.startswith(b"    ", source):
            raise Failure("最初のフレームの次がソースの行でない")
        return source + 6
    if position == "legacy-only":
        if len(legacy) <= len(fixed):
            raise Failure("旧版の流れが固定名の流れより長くない(旧版だけが切れる範囲が無い)")
        return len(fixed) + (len(legacy) - len(fixed)) // 2
    raise Failure(f"未知の位置: {position}")


def event_bytes(position, legacy_write, fixed_write):
    """出来事で選んだ 1 回の書込み(File の行)を切るバイト数。"""
    name_end = fixed_write.index(b'"', len(FILE_PREFIX))
    if position == "field":
        return len(FILE_PREFIX) + (name_end - len(FILE_PREFIX)) // 2
    if position == "outside":
        return len(fixed_write) - 2
    if position == "legacy-only":
        if len(legacy_write) <= len(fixed_write):
            raise Failure("旧版の書込みが固定名の書込みより長くない")
        return len(fixed_write) + (len(legacy_write) - len(fixed_write)) // 2
    raise Failure(f"未知の位置: {position}")


def select_write(writes, prefix, occ):
    """共有ライブラリの prefix・occ と同じ選び方(fd 2 の書込みのうち、prefix で始まる occ 回目)。"""
    seen = 0
    for index, write in enumerate(writes):
        if write.data.startswith(prefix):
            seen += 1
            if seen == occ:
                return index
    raise Failure(f"選ぶ書込みが無い: {prefix!r} の {occ} 回目")


def locate(fixed_writes, quota):
    """固定名にした書込みの列で、合計 quota が何回目(1 から)の書込みの何バイト目に来るか。"""
    total = 0
    for index, data in enumerate(fixed_writes):
        if total + len(data) > quota:
            return index + 1, quota - total
        total += len(data)
    raise Failure("上限が固定名の流れの外にある")


def legacy_only_note(faulted, base):
    return (f"22:38 の決定: 旧版だけが切れる範囲。故障の無かったときの旧版に合わせた(故障ありの旧版の終了値 {faulted}・"
            f"故障なしの旧版の終了値 {base})")


def partial_expectation(ctx, case, recorded=True):
    """部分書込みのケースの、Rust の期待(Obs)と、Rust に当てる故障(rules・rlimit)と、記録の動作を比べるか。"""
    if not recorded:
        raise Skip(SKIP_NO_RECORD)
    kind, position = case.partial[:2]
    base = execute(ctx, case, "legacy", rules="", rlimit=None)
    base_obs = observe(ctx, case, base)
    if base.code is None or any(w.ret != w.length for w in base.writes):
        raise Failure("故障なしの旧版の書込みが全部通っていない")
    raw2 = fd2(base.writes)
    fixed2 = [w for w in base_obs.writes if w[0] == 2]
    legacy_stream = b"".join(w.data for w in raw2)
    fixed_stream = b"".join(w[1] for w in fixed2)
    if kind == "quota":
        quota = quota_position(position, fixed_stream, legacy_stream)
        faulted = execute(ctx, case, "legacy", rules="", rlimit=quota)
        if faulted.timed_out or faulted.code is None:
            raise Failure("故障ありの旧版が終わらない")
        # (ア)の確かめ: 故障ありの旧版の出力は、故障なしの旧版の書込みの列に同じ規則を当てた結果と同じ。
        if faulted.stderr != legacy_stream[:quota]:
            raise Failure("(ア) 故障ありの旧版の出力が、故障なしの旧版の列に上限を当てた結果と違う")
        if quota >= len(fixed_stream):
            if len(faulted.stderr) >= len(legacy_stream):
                raise Failure("旧版だけが切れる範囲なのに、旧版が切れていない")
            ctx.note = legacy_only_note(faulted.code, base.code)
            return base_obs, {"rlimit": quota}, False
        index, cut = locate([w[1] for w in fixed2], quota)
        shifted = execute(ctx, case, "legacy",
                          rules=f"fd=2,nth={index},action=cut,bytes={cut},errno={EFBIG}", rlimit=None)
        shifted_obs = observe(ctx, case, shifted, partial=True)
        if shifted_obs.stderr != fixed_stream[:quota]:
            raise Failure("(ア) 同じ位置で切った旧版の出力が、固定名の流れに上限を当てた結果と違う")
        expected = Obs(faulted.code, faulted.stdout, fixed_stream[:quota], shifted_obs.writes, shifted_obs.frames)
        return expected, {"rlimit": quota}, False
    occ = case.partial[2]
    index = select_write(raw2, FILE_PREFIX, occ)
    nbytes = event_bytes(position, raw2[index].data, fixed2[index][1])
    action = "short" if kind in ("lost", "resend") else "cut"
    rules = f"fd=2,prefix={FILE_PREFIX.hex()},occ={occ},action={action},bytes={nbytes}"
    if kind == "cut":
        rules += f",errno={EFBIG}"
    faulted = execute(ctx, case, "legacy", rules=rules)
    if faulted.timed_out or faulted.code is None:
        raise Failure("故障ありの旧版が終わらない")
    selected = [w for w in fd2(faulted.writes) if w.action in ("short", "cut")]
    if not selected or selected[0].ret != nbytes:
        raise Failure("故障ありの旧版で、選んだ書込みが切れていない")
    if nbytes >= len(fixed2[index][1]):
        # 旧版だけが切れる: 故障の無かったときの旧版の結果に合わせる(終了値の差は 22:38 の決定で認める)。
        label = "short" if action == "short" else "cut-full"
        ctx.note = legacy_only_note(faulted.code, base.code)
        writes = list(base_obs.writes)
        position_all = [i for i, w in enumerate(writes) if w[0] == 2][index]
        fd, data, ret, err, _ = writes[position_all]
        writes[position_all] = (fd, data, ret, err, label)
        return Obs(base_obs.exit, base_obs.stdout, base_obs.stderr, writes, base_obs.frames), {"rules": rules}, True
    if kind == "lost":
        # (ア): 故障なしの旧版の列に同じ規則を当てた結果が、故障ありの旧版と同じ。
        want = [replace(w, ret=nbytes, action="short") if i == index else w for i, w in enumerate(raw2)]
        if fd2(faulted.writes) != want or [w for w in faulted.writes if w.fd == 1] != \
                [w for w in base.writes if w.fd == 1]:
            raise Failure("(ア) 故障ありの旧版の記録が、故障なしの旧版の列に同じ規則を当てた結果と違う")
        writes = list(base_obs.writes)
        position_all = [i for i, w in enumerate(writes) if w[0] == 2][index]
        fd, data, _, err, _ = writes[position_all]
        writes[position_all] = (fd, data, nbytes, err, "short")
        stderr = passed(writes, (2,))
        return Obs(faulted.code, faulted.stdout, stderr, writes, base_obs.frames), {"rules": rules}, True
    # (イ): 故障ありの旧版の記録から作る。
    return observe(ctx, case, faulted, partial=True), {"rules": rules}, True


# ---- ケースの一覧 ----

def chmod(path, mode):
    return lambda root: os.chmod(os.path.join(root, path), mode)


def write(path, data):
    def op(root):
        full = os.path.join(os.fsencode(root), raw(path))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as stream:
            stream.write(raw(data))
    return op


def legacy_cases():
    data = json.loads((FIXTURES / "legacy-cases.json").read_text(encoding="utf-8"))
    if data["legacy"]["sha256"] != LEGACY_SHA256:
        raise Failure("legacy-cases.json の旧版の SHA-256 が違う")
    prefixes = (("basic-", "basic"), ("args-", "args"), ("walk-", "walk"), ("link-", "body"), ("text-", "body"),
                ("codepath-", "code"), ("output-", "output"), ("exit-", "output"))
    cases = []
    for case in data["cases"]:
        group = next(name for prefix, name in prefixes if case["id"].startswith(prefix))
        cases.append(Case("legacy:" + case["id"], group, tree=case["tree"], argv=case["argv"],
                          expected=case["expected"]))
    return cases


def deep(depth, name, link=None):
    def op(root):
        path = make_deep(os.path.join(root, "t"), depth, name)
        if link:
            fd = os.open(path, os.O_RDONLY)
            for _ in range(depth):
                child = os.open(name, os.O_RDONLY, dir_fd=fd)
                os.close(fd)
                fd = child
            os.symlink("x", link, dir_fd=fd)
            os.close(fd)
    return op


def linux_traceback_cases():
    """linux.json の旧版の呼出履歴の固定期待(Task 1.1)と同じ入力。"""
    data = json.loads((FIXTURES / "linux.json").read_text(encoding="utf-8"))
    expected = {case["id"]: case for case in data["tracebacks"]}
    long_name = "a" * 300

    def chain(root):
        make_chain(root, 1100)

    def links(root):
        with open(os.path.join(root, "a.md"), "w") as stream:
            stream.write("".join(f"[x](m{index:03d}{'q' * 60})\n" for index in range(200)))

    cases = [
        Case("target-isdir-eacces", "args", [{"op": "dir", "path": "noperm"}, chmod("noperm", 0)], ["noperm/x"]),
        Case("target-isdir-enametoolong", "args", [], [long_name]),
        Case("target-warn-then-eacces", "args", [{"op": "dir", "path": "noperm"}, chmod("noperm", 0)],
             ["missing", "noperm/x"]),
        Case("rglob-deep-enametoolong", "walk", [deep(21, "d" * 200)], ["t"]),
        Case("rglob-recursion", "walk", [deep(995, "d")], ["t"]),
        Case("read-isdir", "walk", [write("g/y.md", "[a](b)\n"), {"op": "dir", "path": "g/x.md"}], ["g"]),
        Case("read-eacces", "body", [write("a.md", "[x](m)\n"), chmod("a.md", 0)], ["a.md"]),
        Case("read-fifo-sigint", "signal", [{"op": "fifo", "path": "a.md"}], ["."], scenario="fifo-sigint",
             interrupt=True),
        Case("link-resolve-nul", "body", [write("a.md", b"[x](a\x00b)\n")], ["a.md"]),
        Case("link-resolve-loop", "body", [write("d/a.md", "[a](loop)\n"),
                                           {"op": "symlink", "path": "d/loop", "target": "loop"}], ["d/a.md"]),
        Case("link-resolve-getcwd", "body", [{"op": "dir", "path": "w"}, {"op": "fifo", "path": "w/a.md"}],
             ["."], scenario="getcwd-removed", preserve=False, cwd="w"),
        Case("link-resolve-recursion", "body", [chain, write("a.md", "[x](l0)\n")], ["a.md"]),
        Case("link-exists-eacces", "body", [{"op": "dir", "path": "n"}, chmod("n", 0),
                                            write("a.md", "[x](n/a.md)\n")], ["a.md"]),
        Case("link-exists-enametoolong", "body", [write("a.md", f"[x]({long_name})\n")], ["a.md"]),
        Case("code-resolve-recursion", "code", [chain, write("a.md", "`l0/x.md`\n")], ["a.md"]),
        Case("code-alt-eacces", "code", [{"op": "dir", "path": "x"}, chmod("x", 0),
                                         write("d/doc.md", "`x/y.md`\n")], ["d/doc.md"]),
        Case("code-exists-eacces", "code", [write("x/y.md", ""), write("d/doc.md", "`x/y.md`\n"),
                                            {"op": "dir", "path": "d/x"}, chmod("d/x", 0)], ["d/doc.md"]),
        Case("rglob-entry-isdir-enametoolong", "walk", [deep(20, "d" * 200, "l" * 100)], ["t"]),
        Case("print-brokenpipe", "fault", [links], ["a.md"], stdout="broken"),
        Case("exit-flush-devfull", "fault", [write("a.md", "[x](m)\n")], ["a.md"], stdout="devfull"),
    ]
    for case in cases:
        fixture = expected.pop(case.id)
        if not case.interrupt:
            case.expected = {"exit": fixture["exit"], "stdout": fixture["stdout"], "stderr": fixture["stderr"]}
            case.expected_os = "linux"
        case.id = "traceback:" + case.id
    if expected:
        raise Failure("linux.json の呼出履歴のケースが比較に無い: " + ", ".join(sorted(expected)))
    return cases


BASIC = [{"op": "dir", "path": "docs"}, {"op": "file", "path": "docs/a.md", "data": "[x](missing.md) `src/none.py`\n"}]
ONE = [write("g/y.md", "[a](b)\n"), {"op": "dir", "path": "g/x.md"}]
TWO = [write("d/a.md", "[a](loop)\n"), {"op": "symlink", "path": "d/loop", "target": "loop"}]
NON_UTF8 = [{"op": "dir", "path": "d"}, {"op": "file", "path": {"hex": "642f78ff2e6d64"}, "data": "[x](m)\n"}]


def sized_tree(total):
    """stdout がちょうど `total` バイトになる入力(ERROR の行を並べ、最後の行の長さで合わせる)。"""
    def lines(targets):
        out = "検査ファイル数: 1\n"
        out += "".join(f"ERROR a.md:{i + 1}: リンク切れ -> {t}\n" for i, t in enumerate(targets))
        out += f"結果: ERROR {len(targets)} 件 / WARN 0 件\n"
        return len(out.encode())
    targets = []
    while lines(targets + ["m" * 80]) < total - 200:
        targets.append(f"m{len(targets):04d}" + "q" * 70)
    targets.append("m")
    while lines(targets) < total:
        targets[-1] += "z"
    if lines(targets) != total:
        targets[-1] = targets[-1][:-1]
        targets.insert(0, "m" * 10)
        while lines(targets) < total:
            targets[-1] += "z"
    if lines(targets) != total:
        raise Failure(f"stdout を {total} バイトにできない")
    return [write("a.md", "".join(f"[x]({t})\n" for t in targets))]


def fake_file_line_tree(root):
    """利用者の入力(ディレクトリの名前)に、旧スクリプトの実際のパスを持つ偽の File の行を置く。

    名前は例外の本文(repr)の行の途中に出る。フレームの位置ではないので、対応づけてはならない。
    """
    base = os.fsencode(root) + b'/  File "' + os.fsencode(str(LEGACY)) + b'", line 1, in x'
    os.makedirs(base + b"/x.md")


def last_stdout_write(base):
    """故障なしの旧版で、stdout への最後の書込みが何回目か(最後の書き出しの時点の SIGINT のため)。"""
    count = sum(1 for w in base.writes if w.fd == 1)
    if count == 0:
        raise Failure("stdout への書込みが無い")
    return f"fd=1,nth={count},action=sigint"


def behavior_cases():
    cases = []

    def add(case_id, group, *args, **kwargs):
        cases.append(Case(case_id, group, *args, **kwargs))

    # 引数 0 個と PYTHONOPTIMIZE
    for name, value in (("unset", None), ("0", "0"), ("1", "1"), ("2", "2"), ("3", "3"), ("bad", "x"),
                        ("negative", "-2"), ("space", " 2"), ("trailing", "2 "), ("empty", "")):
        env = {} if value is None else {"PYTHONOPTIMIZE": value}
        add(f"args-none-optimize-{name}", "env", [], [], env=env)
    add("args-none-pty", "output", [], [], stdout="pty")
    # 非 UTF-8 のバイトの引数(不在と、非 UTF-8 の名前のファイル)
    add("args-non-utf8", "args", [{"op": "file", "path": {"hex": "78ff2e6d64"}, "data": "[x](m)\n"}],
        [{"hex": "78ff2e6d64"}, {"hex": "ff2e6d64"}])
    # 起動の失敗と環境変数
    for name, value in (("random", "random"), ("zero", "0"), ("max", "4294967295"), ("space", " 7"),
                        ("minus-zero", "-0"), ("empty", "")):
        add(f"env-hashseed-ok-{name}", "env", BASIC, ["docs"], env={"PYTHONHASHSEED": value})
    for name, value in (("word", "Random"), ("over", "4294967296"), ("negative", "-1"), ("trailing", "5 "),
                        ("huge", "9" * 30)):
        add(f"env-hashseed-bad-{name}", "env", BASIC, ["docs"], env={"PYTHONHASHSEED": value})
    for name, value in (("zero", "0"), ("min", "640"), ("big", "100000")):
        add(f"env-intmax-ok-{name}", "env", BASIC, ["docs"], env={"PYTHONINTMAXSTRDIGITS": value})
    for name, value in (("small", "639"), ("negative", "-1"), ("word", "x"), ("huge", "9" * 30)):
        add(f"env-intmax-bad-{name}", "env", BASIC, ["docs"], env={"PYTHONINTMAXSTRDIGITS": value})
    add("env-both-bad", "env", BASIC, ["docs"], env={"PYTHONHASHSEED": "x", "PYTHONINTMAXSTRDIGITS": "1"})
    # 起動の判定の 1・2 の診断は、親から受け継いだ SIGPIPE の扱いのまま書く(閉じたパイプの stderr)。
    for name, env in (("hashseed", {"PYTHONHASHSEED": "x"}), ("intmax", {"PYTHONINTMAXSTRDIGITS": "1"})):
        for disposition, code in (("default", -signal.SIGPIPE), ("ignore", 1)):
            add(f"startup-{name}-broken-stderr-sigpipe-{disposition}", "fault", BASIC, ["docs"], env=env,
                stderr="broken", sigpipe=disposition, expected={"exit": code, "stdout": "", "stderr": ""})
    # 対照: 標準入出力の判定(起動の判定の 4 以降)では、もう SIGPIPE を無視にしている。
    add("startup-stdout-dir-broken-stderr-sigpipe-default", "fault", BASIC, ["docs"], stdout="dir",
        stderr="broken", sigpipe="default", fatal=True, expected={"exit": 1, "stdout": "", "stderr": ""})
    for name, value in (("one", "1"), ("zero", "0"), ("zeros", "000"), ("bad", "x"), ("negative", "-1"),
                        ("empty", ""), ("space-zero", " 0")):
        add(f"env-unbuffered-{name}", "env", BASIC, ["docs"], env={"PYTHONUNBUFFERED": value})
    add("env-lang-c-utf8", "encoding", BASIC, ["docs"], env={"LANG": "C.UTF-8"})
    # 文字コードの設定(Rust は読まない。設定なしの旧版と比べ、設定ありの旧版は別の期待と比べる)
    for name, env, tree, argv, differs, fatal in (
            ("pythonioencoding-utf8-strict-non-utf8-name", {"PYTHONIOENCODING": "utf-8"}, NON_UTF8, ["d"], True,
             False),
            ("pythonioencoding-ascii", {"PYTHONIOENCODING": "ascii"}, BASIC, ["docs"], True, False),
            ("pythonioencoding-unknown", {"PYTHONIOENCODING": "no-such-codec"}, BASIC, ["docs"], True, True),
            ("pythonutf8-zero", {"PYTHONUTF8": "0"}, NON_UTF8, ["d"], True, False),
            ("lang-c-utf8-non-utf8-name", {"LANG": "C.UTF-8"}, NON_UTF8, ["d"], False, False)):
        add(f"encoding-{name}", "encoding", tree, argv, env=env, encoding_env={}, encoding_differs=differs,
            encoding_fatal=fatal)
    # 標準入出力がディレクトリ・閉じている
    add("stdio-stdin-dir", "fault", BASIC, ["docs"], stdin="dir", fatal=True)
    add("stdio-stdout-dir", "fault", BASIC, ["docs"], stdout="dir", fatal=True)
    add("stdio-stderr-dir", "fault", BASIC, ["docs"], stderr="dir", fatal=True)
    add("stdio-stdout-stderr-dir", "fault", BASIC, ["docs"], stdout="dir", stderr="dir", fatal=True)
    add("stdio-stdin-dir-bad-hashseed", "fault", BASIC, ["docs"], stdin="dir", env={"PYTHONHASHSEED": "x"})
    # 呼出元が渡した読み書きの /dev/null(subprocess.DEVNULL と同じ O_RDWR)は使える fd なので、旧版は書く。
    add("stdio-devnull-stdout", "fault", BASIC, ["docs"], stdout="devnull")
    add("stdio-devnull-stderr-exception", "fault", ONE, ["missing", "g"], stderr="devnull")
    add("stdio-devnull-both", "fault", BASIC, ["docs"], stdout="devnull", stderr="devnull")
    add("stdio-devnull-both-exception", "fault", ONE, ["missing", "g"], stdout="devnull", stderr="devnull")
    for fd in ("stdin", "stdout", "stderr"):
        add(f"stdio-closed-{fd}", "fault", BASIC, ["docs"], **{fd: "closed"})
        add(f"stdio-closed-{fd}-exception", "fault", ONE, ["g"], **{fd: "closed"})
    # 出力の溜めの境目(パイプ・ファイル・疑似端末 × PYTHONUNBUFFERED の有効と無効)
    for total in (8191, 8192, 8193, 16383, 16384, 16385):
        for mode, extra, out in (("pipe", {}, "pipe"), ("file", {}, "file"), ("pty", {}, "pty"),
                                 ("unbuffered", {"PYTHONUNBUFFERED": "1"}, "pipe"),
                                 ("file-unbuffered", {"PYTHONUNBUFFERED": "1"}, "file"),
                                 ("pty-unbuffered", {"PYTHONUNBUFFERED": "1"}, "pty")):
            add(f"output-{total}-{mode}", "output", sized_tree(total), ["a.md"], env=extra, stdout=out)
    add("output-pty-cr-lf-names", "output", [write("d/a\rb.md", "[x](m)\n"), write("d/c\nd.md", "[y](n)\n")],
        ["d"], stdout="pty")
    add("output-same-pipe-exception", "output", ONE, ["missing", "g"], stderr="stdout")
    # 書込みの故障
    for mode, extra in (("buffered", {}), ("unbuffered", {"PYTHONUNBUFFERED": "1"})):
        add(f"fault-stdout-devfull-{mode}", "fault", BASIC, ["docs"], env=extra, stdout="devfull")
        add(f"fault-stdout-broken-{mode}", "fault", BASIC, ["docs"], env=extra, stdout="broken")
        add(f"fault-stdout-eagain-{mode}", "fault", BASIC, ["docs"], env=extra, stdout="eagain")
        add(f"fault-stderr-devfull-exception-{mode}", "fault", ONE, ["g"], env=extra, stderr="devfull")
        add(f"fault-stderr-devfull-noexception-{mode}", "fault", BASIC, ["docs"], env=extra, stderr="devfull")
        add(f"fault-stderr-broken-exception-{mode}", "fault", ONE, ["g"], env=extra, stderr="broken")
        add(f"fault-stderr-broken-noexception-{mode}", "fault", BASIC, ["docs"], env=extra, stderr="broken")
        add(f"fault-stderr-eagain-exception-{mode}", "fault", ONE, ["g"], env=extra, stderr="eagain")
        add(f"fault-both-devfull-{mode}", "fault", BASIC, ["docs"], env=extra, stdout="devfull", stderr="devfull")
        add(f"fault-both-devfull-exception-{mode}", "fault", ONE, ["missing", "g"], env=extra, stdout="devfull",
            stderr="devfull")
        add(f"fault-stdout-eio-once-{mode}", "fault", BASIC, ["docs"], env=extra,
            rules="fd=1,nth=1,action=fail,errno=5")
        add(f"fault-stdout-short-{mode}", "fault", sized_tree(9000), ["a.md"], env=extra,
            rules="fd=1,nth=1,action=short,bytes=100")
        add(f"fault-stdout-quota-{mode}", "fault", sized_tree(9000), ["a.md"], env=extra,
            rules="fd=1,action=quota,bytes=5000,errno=27")
        add(f"fault-stdout-rlimit-{mode}", "fault", sized_tree(9000), ["a.md"], env=extra, stdout="file",
            rlimit_fsize=5000)
        add(f"fault-stdout-flushio-eagain-sigint-{mode}", "stage", BASIC, ["docs"], env=extra,
            rules=f"fd=1,nth=1,action=fail,errno={EAGAIN};fd=1,nth=2,action=sigint")
        add(f"fault-stdout-close-sigint-{mode}", "stage", BASIC, ["docs"], env=extra,
            rules=f"fd=1,nth=1,action=fail,errno={EAGAIN};fd=1,nth=2,action=fail,errno={EAGAIN};fd=1,nth=3,action=sigint")
        # 手順 4(閉じるとき)の書き出しの失敗と成功
        add(f"fault-stdout-close-fail-{mode}", "stage", BASIC, ["docs"], env=extra,
            rules=f"fd=1,nth=1,action=fail,errno={EAGAIN};fd=1,nth=2,action=fail,errno={EAGAIN};fd=1,nth=3,action=fail,errno=5")
        add(f"fault-stdout-close-ok-{mode}", "stage", BASIC, ["docs"], env=extra,
            rules=f"fd=1,nth=1,action=fail,errno={EAGAIN};fd=1,nth=2,action=fail,errno={EAGAIN}")
        add(f"fault-unraisable-sigint-{mode}", "stage", BASIC, ["docs"], env=extra,
            rules=f"fd=1,nth=1,action=fail,errno={EAGAIN};fd=1,nth=2,action=fail,errno={EAGAIN};fd=2,nth=1,action=sigint")
    # 信号
    warn = [{"op": "dir", "path": "f"}, {"op": "fifo", "path": "f/a.md"}]
    for prefix, argv in (("", ["f"]), ("warn-", ["missing", "f"])):
        for mode, extra, out in (("pipe", {}, "pipe"), ("unbuffered", {"PYTHONUNBUFFERED": "1"}, "pipe"),
                                 ("pty", {}, "pty")):
            add(f"signal-fifo-sigint-{prefix}{mode}", "signal", warn, argv, env=extra, stdout=out,
                scenario="fifo-sigint", interrupt=True)
            add(f"signal-fifo-sigterm-{prefix}{mode}", "signal", warn, argv, env=extra, stdout=out,
                scenario="fifo-sigterm")
    add("signal-sigint-ignored", "signal", warn, ["missing", "f"], scenario="fifo-ignored", sigint_ignored=True)
    big = sized_tree(20000)
    for mode, extra, out in (("pipe", {}, "pipe"), ("unbuffered", {"PYTHONUNBUFFERED": "1"}, "pipe"),
                             ("pty", {}, "pty")):
        for nth in (1, 2, 3):
            # 何回目の write が本処理の中かは条件で変わる。旧版が SIGINT で終わったときだけ中断の比べ方にする。
            add(f"signal-write-sigint-{mode}-{nth}", "signal", big, ["a.md"], env=extra, stdout=out,
                rules=f"fd=1,nth={nth},action=sigint", interrupt=None)
        add(f"signal-write-sigint-{mode}-last", "signal", big, ["a.md"], env=extra, stdout=out,
            derive=last_stdout_write, interrupt=None)
    # 走査
    add("walk-fifo-writer", "walk", warn, ["f"], scenario="fifo-write")
    add("rglob-depth-988", "walk", [lambda root: make_deep(os.path.join(root, "t"), 988, "d")], ["t"])
    add("rglob-depth-989", "walk", [lambda root: make_deep(os.path.join(root, "t"), 989, "d")], ["t"])
    add("walk-unreadable-dir", "walk", [write("t/a.md", ""), write("t/r/b.md", ""), chmod("t/r", 0o300)], ["t"])
    add("walk-unsearchable-dir", "walk", [write("t/a.md", ""), write("t/x/b.md", ""), chmod("t/x", 0o600)], ["t"])
    add("walk-kinds", "walk", [write("t/a.md", "[x](m)\n"), {"op": "symlink", "path": "t/broken.md",
                                                             "target": "nowhere"}], ["t"])
    # 打ち切りの結果は readdir の順に依るので、固定期待を持たない(旧版と Rust の一致で比べる)。
    add("walk-permission-abort", "walk", [permission_abort_tree], ["t"], fixture=False)
    # 本文とリンクの深さ
    for count in (992, 993):
        add(f"link-chain-{count}", "body", [lambda root, count=count: make_chain(root, count),
                                            write("a.md", "[x](l0)\n")], ["a.md"])
        add(f"code-chain-{count}", "code", [lambda root, count=count: make_chain(root, count),
                                            write("a.md", "`l0/x.md`\n")], ["a.md"])
    add("args-stat-eacces", "args", [{"op": "dir", "path": "n"}, chmod("n", 0)], ["n/a.md"])
    add("body-link-eacces-and-long", "body", [{"op": "dir", "path": "n"}, chmod("n", 0),
                                              write("a.md", "[x](n/m.md)\n")], ["a.md"])
    add("body-relative-nul", "body", [write("a.md", b"[x](a\x00b)\n")], ["a.md"])
    add("body-loop-nul", "body", [write("a.md", b"[x](loop/a\x00b)\n"),
                                  {"op": "symlink", "path": "loop", "target": "loop"}], ["a.md"])
    add("body-loop-absolute-target", "body", [write("d/a.md", "[a](loop/../x)\n"),
                                              {"op": "symlink", "path": "d/loop", "target": "loop"}],
        [lambda ctx: os.fsencode(ctx.root) + b"/d/a.md"])
    add("body-absolute-loop", "body", [{"op": "symlink", "path": "loop", "target": "loop"},
                                       lambda root: write("a.md", f"[x]({root}/loop)\n[y]({root}/a.md)\n")(root)],
        ["a.md"])
    add("body-double-slash-md", "body", [write("d/a.md", "[a](b.md) `c/d.md`\n")],
        [lambda ctx: b"/" + os.fsencode(ctx.root) + b"/d/a.md"])
    # コード中のパス: md の場所(祖先は字面で決まる)
    body = "`x/y.md` `x/z.md` `q/zz.md`\n"
    add("code-location-dotdot", "code", [write("w/doc.md", body), write("x/y.md", ""), write("w/x/z.md", ""),
                                         {"op": "dir", "path": "w/v"}], ["../doc.md"], cwd="w/v")
    add("code-location-dotdot2", "code", [write("w/v/u/x", ""), write("doc.md", body), write("x/y.md", ""),
                                          write("w/x/z.md", "")], ["../../doc.md"], cwd="w/v")
    for name, slashes in (("double-slash", b"//"), ("triple-slash", b"///"), ("single-slash", b"/")):
        add(f"code-location-top-{name}", "code", [], [lambda ctx, s=slashes: s + os.fsencode(ctx.top)[1:] +
                                                      b"/doc.md"],
            top_tree=[lambda top: write("doc.md", top_body(top))(top),
                      lambda top: write("z.md", "")(top), write("x/y.md", "")])
    # md の場所が /tmp の直下(祖先は / だけ)。/ の直下は root でない利用者には作れないので、これで代える。
    add("code-location-root-child", "code", [], [lambda ctx: os.fsencode(ctx.top) + b".md"],
        top_tree=[write("z.md", "")], top_file=lambda top: top_body(top).encode())
    add("selftest-fake-file-line", "body", [fake_file_line_tree], ["."])
    return cases


def top_body(top):
    """@TOP@/doc.md の中身: @TOP@ の名前を含むコード中のパス(祖先の // と / に連結して見つかる)。"""
    name = os.path.basename(top)
    return f"`x/y.md` `tmp/{name}/z.md` `{name}/z.md` `q/zz.md`\n"


def permission_abort_tree(root):
    """子の判定の PermissionError で、残りの子への走査を打ち切る木(リンクが子のディレクトリより先に並ぶ)。

    並び(os.scandir の順)は、旧版を動かす Python で取る(PermissionError の前提を旧版で確かめる)。
    """
    base = os.path.join(root, "t")
    os.mkdir(base)
    os.mkdir(os.path.join(root, "n"))
    os.mkdir(os.path.join(root, "n", "x"))
    for attempt in range(40):
        link = os.path.join(base, f"l{attempt}")
        sub = os.path.join(base, f"s{attempt}")
        os.symlink("../n/x/y", link)
        os.mkdir(sub)
        with open(os.path.join(sub, "z.md"), "w") as stream:
            stream.write("")
        names = scandir_order(base)
        if names.index(f"l{attempt}") < names.index(f"s{attempt}"):
            os.chmod(os.path.join(root, "n"), 0o600)
            return
        os.unlink(link)
        os.unlink(os.path.join(sub, "z.md"))
        os.rmdir(sub)
    raise Failure("打ち切りの前提(リンクが子のディレクトリより先に並ぶ)を作れない")


SCANDIR_PYTHON = None


def scandir_order(path):
    python = SCANDIR_PYTHON or sys.executable
    done = subprocess.run([python, "-c", "import json, os, sys; print(json.dumps([e.name for e in "
                                         "os.scandir(sys.argv[1])]))", path],
                          env=child_env({}), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=60)
    if done.returncode != 0:
        raise Failure("旧版の Python で os.scandir の順を取れない")
    return json.loads(done.stdout)


def partial_cases():
    """固定名の欄を含む書込みが途中で切れる故障(22:26・22:38 の決定。#471 の (ア)(イ))。"""
    cases = []
    for mode, extra in (("buffered", {}), ("unbuffered", {"PYTHONUNBUFFERED": "1"})):
        for position in ("field", "outside", "legacy-only"):
            # (ア) 合計の上限(RLIMIT_FSIZE。stderr はファイル)
            cases.append(Case(f"partial-rlimit-{mode}-{position}", "partial", ONE, ["g"], env=extra,
                              stderr="file", partial=("quota", position)))
            # (イ) 部分書込みの後に失敗が続く(選んだ File の行で上限に当たる)
            cases.append(Case(f"partial-cut-{mode}-{position}", "partial", ONE, ["g"], env=extra,
                              partial=("cut", position, 1)))
        # 2 段の呼出履歴でも、上限が欄の途中と外に来るとき
        for position in ("field", "outside"):
            cases.append(Case(f"partial-rlimit-two-{mode}-{position}", "partial", TWO, ["d/a.md"], env=extra,
                              stderr="file", partial=("quota", position)))
    for position in ("field", "outside", "legacy-only"):
        # (ア) 無バッファで 1 回の書込みが切れ、残りが失われる
        cases.append(Case(f"partial-lost-unbuffered-{position}", "partial", ONE, ["g"],
                          env={"PYTHONUNBUFFERED": "1"}, partial=("lost", position, 1)))
        # (イ) 通常のバッファで、残りを送り直す
        cases.append(Case(f"partial-resend-buffered-{position}", "partial", ONE, ["g"],
                          partial=("resend", position, 1)))
    # 標準ライブラリ(pathlib.py)のフレームの欄の途中
    cases.append(Case("partial-resend-buffered-stdlib-field", "partial", ONE, ["g"], partial=("resend", "field", 3)))
    cases.append(Case("partial-cut-buffered-stdlib-field", "partial", ONE, ["g"], partial=("cut", "field", 3)))
    return cases


# ---- 手順 2 の表 ----

STEP2_INPUTS = {
    "one": (ONE, ["g"]),
    "two": (TWO, ["d/a.md"]),
    # Mac はパスの長さの上限(1024 バイト)で先に ENAMETOOLONG になるので、同じ RecursionError をリンクの鎖で作る。
    "rec": ([lambda root: make_deep(os.path.join(root, "t"), 995, "d")], ["t"]) if not MAC else
           ([lambda root: make_chain(root, 1100), write("a.md", "[x](l0)\n")], ["a.md"]),
}
STEP2_ACTIONS = {
    "sigint": "action=sigint",
    "fail-once": "action=fail,errno=28",
    "fail-persist": "action=fail,errno=28,persist=1",
    "fail-persist-epipe": f"action=fail,errno={errno.EPIPE},persist=1",
    # RLIMIT_FSIZE の上限が、選んだ部品の途中に来る(途中まで書いて、その後は EFBIG)
    "cut": f"action=cut,errno={EFBIG}",
}
# 必須の部品。無バッファでは、ソースの行は字下げ・本文・改行に、例外の行は型の名前・": "・本文・改行に分かれて書かれる。
STEP2_COMMON = {"buffered": {"header", "file", "source", "exception"},
                "unbuffered": {"header", "file", "indent", "source", "source_end", "typename", "colon", "value", "end"}}
STEP2_PIECES = {"one": set(), "two": {"separator", "header2"}, "rec": {"repeated"}}


def piece_kind(text, mode, index, texts):
    """呼出履歴の書込み 1 回の部品の種類。"""
    if text == "Traceback (most recent call last):\n":
        return "header2" if any(t.startswith("\nDuring handling") for t in texts[:index]) else "header"
    if text.startswith('  File "'):
        return "file"
    if text.startswith("  [Previous line repeated"):
        return "repeated"
    if text.startswith("\nDuring handling"):
        return "separator"
    if mode == "buffered":
        if text.startswith("    "):
            return "source"
        return "exception"
    if text == "    ":
        return "indent"
    previous = texts[index - 1] if index else ""
    if text == "\n":
        return "source_end" if not previous.endswith("\n") and index >= 2 and texts[index - 2] == "    " \
            else "end"
    if previous == "    ":
        return "source"
    if text == ": ":
        return "colon"
    if index + 1 < len(texts) and texts[index + 1] == ": ":
        return "typename"
    return "value"


def step2_targets(texts, mode):
    """部品の種類ごとに、最初に現れる書込みを選ぶ規則(prefix・occ・exact)。File 欄はパスを含めない。"""
    chosen = {}
    for index, text in enumerate(texts):
        kind = piece_kind(text, mode, index, texts)
        if kind in chosen:
            continue
        if text.startswith('  File "'):
            prefix, exact = '  File "', 0
        elif len(text) <= 40:
            prefix, exact = text, 1
        else:
            prefix, exact = text[:40], 0
        occ = sum(1 for t in texts[:index + 1]
                  if (t == prefix if exact else t.startswith(prefix)))
        chosen[kind] = (index, prefix, occ, exact)
    return chosen


def step2_cut_bytes(text):
    """cut の動作で切るバイト数。File の行は固定名の欄の途中(どの固定名も 10 バイト以上)。"""
    data = text.encode("utf-8")
    if text.startswith('  File "'):
        return len(FILE_PREFIX) + 4
    return min(len(data) // 2, 20)


def step2_rule(prefix, occ, exact, action, text):
    rule = STEP2_ACTIONS[action]
    if action == "cut":
        rule = f"action=cut,bytes={step2_cut_bytes(text)},errno={EFBIG}"
    return f"fd=2,prefix={prefix.encode().hex()},occ={occ},exact={exact},{rule}"


def line_kind(line):
    if line == "Traceback (most recent call last):":
        return "見出し"
    if line.startswith('  File "'):
        return "File の行"
    if line.startswith("    "):
        return "ソースの行"
    if line.startswith("  [Previous line repeated"):
        return "繰り返しの行"
    if line.startswith("During handling"):
        return "2 段の区切り"
    if line == "":
        return "空行"
    return "例外の行"


def runs(kinds):
    """続く同じ種類をまとめる(例: File の行・ソースの行×2)。"""
    out = []
    for kind in kinds:
        if out and out[-1][0] == kind:
            out[-1][1] += 1
        else:
            out.append([kind, 1])
    return "・".join(kind if count == 1 else f"{kind}×{count}" for kind, count in out)


def describe(base_text, got_text):
    """出力の行の差(落とした行の種類・足した行)と、出す・落とす・改行するの分類。"""
    base = base_text.split("\n")
    got = got_text.split("\n")
    matcher = difflib.SequenceMatcher(a=base, b=got, autojunk=False)
    dropped = []
    added = []
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op in ("delete", "replace"):
            dropped += [line_kind(line) for line in base[a1:a2]]
        if op in ("insert", "replace"):
            added += got[b1:b2]
    dropped = runs(dropped)
    if not dropped and not added:
        result = "出す"
    elif dropped and added and all(line == "" for line in added):
        result = "改行する"
    elif dropped and not added:
        result = "落とす"
    else:
        result = "置き換える"
    return result, dropped, added


def step2_record(ctx, result, case):
    """手順 2 のケースの結果を、固定期待と同じ形(作業場所は印)にする。"""
    obs = observe(ctx, case, result, partial=True)
    root = os.fsencode(ctx.root)
    writes = [[w[0], w[1].replace(root, ROOT_MARK).decode("utf-8"), w[2], w[3], w[4]] for w in obs.writes]
    return obs.exit, obs.stderr.replace(root, ROOT_MARK).decode("utf-8"), writes


def step2_measure(ctx):
    entries = []
    for name, (tree, argv) in STEP2_INPUTS.items():
        for mode, extra in (("buffered", {}), ("unbuffered", {"PYTHONUNBUFFERED": "1"})):
            base_case = Case(f"step2-{name}-{mode}-base", "stage", tree, argv, env=extra)
            base = observe(ctx, base_case, execute(ctx, base_case, "legacy"))
            texts = [w[1].decode("utf-8") for w in base.writes if w[0] == 2]
            base_text = base.stderr.decode("utf-8")
            for kind, (index, prefix, occ, exact) in step2_targets(texts, mode).items():
                for action in STEP2_ACTIONS:
                    rules = step2_rule(prefix, occ, exact, action, texts[index])
                    case = Case(f"step2:{name}:{mode}:{kind}:{action}", "stage", tree, argv, env=extra, rules=rules)
                    got = execute(ctx, case, "legacy")
                    if not any(w.action != "none" for w in got.writes):
                        raise Failure(f"{case.id}: 故障を当てた書込みが記録に無い")
                    code, stderr, writes = step2_record(ctx, got, case)
                    result, dropped, added = describe(base_text.replace(ctx.root, ROOT_MARK.decode()), stderr)
                    entries.append({
                        "id": case.id, "input": name, "mode": mode, "piece": kind, "action": action,
                        "rules": rules, "exit": code, "stderr": stderr, "writes": writes,
                        "result": result, "dropped": dropped, "added": added,
                    })
    return entries


def step2_table(entries):
    """部品の種類 × 動作 × バッファの有無の表(入力ごとに違えば、入力ごとに書く)。"""
    rows = {}
    for entry in entries:
        key = (entry["mode"], entry["piece"], entry["action"])
        rows.setdefault(key, []).append(entry)
    table = []
    for (mode, piece, action), group in sorted(rows.items()):
        outcomes = sorted({(e["result"], e["dropped"], tuple(e["added"]), e["exit"]) for e in group})
        table.append({
            "mode": mode, "piece": piece, "action": action,
            "inputs": [e["input"] for e in group],
            "outcomes": [{"result": r, "dropped": d, "added": list(a), "exit": x}
                         for r, d, a, x in outcomes],
        })
    return table


def step2_cases():
    data = json.loads(STEP2_FIXTURE.read_text(encoding="utf-8"))
    cases = []
    for entry in data["entries"]:
        tree, argv = STEP2_INPUTS[entry["input"]]
        extra = {"PYTHONUNBUFFERED": "1"} if entry["mode"] == "unbuffered" else {}
        case = Case(entry["id"], "stage", tree, argv, env=extra, rules=entry["rules"])
        case.step2 = entry
        cases.append(case)
    return cases, data


def check_step2_coverage(data):
    """手順 2 の表が、3 つの入力 × 2 つのバッファ × 必須の部品 × 5 つの動作を欠けなく持つか。"""
    seen = {}
    for entry in data["entries"]:
        seen.setdefault((entry["input"], entry["mode"], entry["action"]), set()).add(entry["piece"])
    for name, pieces in STEP2_PIECES.items():
        for mode in ("buffered", "unbuffered"):
            for action in STEP2_ACTIONS:
                missing = (pieces | STEP2_COMMON[mode]) - seen.get((name, mode, action), set())
                if missing:
                    raise Failure(f"手順 2 の表に欠けた部品がある: {name}・{mode}・{action}: {sorted(missing)}")
    for row in data["table"]:
        if not row["outcomes"] or not row["inputs"]:
            raise Failure(f"手順 2 の表に欄が空の部品がある: {row['mode']}・{row['piece']}・{row['action']}")


def mac_step2_rules(ctx, case, cache):
    """Mac は errno の番号と文が違うので、旧版の故障なしの結果から同じ選び方で規則を作り直す。"""
    entry = case.step2
    key = (entry["input"], entry["mode"])
    if key not in cache:
        base_case = replace(case, rules="", step2=None)
        base = observe(ctx, base_case, execute(ctx, base_case, "legacy"))
        texts = [w[1].decode("utf-8") for w in base.writes if w[0] == 2]
        cache[key] = (texts, step2_targets(texts, entry["mode"]))
    texts, targets = cache[key]
    if entry["piece"] not in targets:
        raise Failure(f"{case.id}: この OS の旧版の呼出履歴に部品 {entry['piece']} が無い")
    index, prefix, occ, exact = targets[entry["piece"]]
    return step2_rule(prefix, occ, exact, entry["action"], texts[index])


def step2_os_entry(rules, record):
    """Mac の手順 2 のケースの固定期待の形(規則と、旧版の結果。記録は SHA-256)。"""
    code, stderr, writes = record
    text = json.dumps(writes, ensure_ascii=False, separators=(",", ":"))
    return {"rules": rules, "exit": code, "stderr": stderr,
            "writes": {"count": len(writes), "sha256": hashlib.sha256(text.encode()).hexdigest()}}


def check_step2(ctx, case, mac_cache, fixtures=None, measure=False):
    """手順 2 のケース。Linux は step2.json、Mac はこの OS の固定期待(cases-macos.json)と比べる。"""
    entry = case.step2
    rules = mac_step2_rules(ctx, case, mac_cache) if MAC else entry["rules"]
    results = {}
    for kind in (("legacy",) if measure else ("legacy", "rust")):
        result = execute(ctx, case, kind, rules=rules)
        if not any(w.action != "none" for w in result.writes):
            raise Failure(f"{case.id}/{kind}: 故障を当てた書込みが記録に無い")
        results[kind] = step2_record(ctx, result, case)
    if MAC:
        ctx.measured[case.id] = step2_os_entry(rules, results["legacy"])
    if measure:
        return ctx.measured.get(case.id)
    if results["legacy"] != results["rust"]:
        raise Failure(f"{case.id}: 旧版と Rust の記録が違う\n旧版 {results['legacy']!r}\nRust {results['rust']!r}")
    if LINUX:
        if results["rust"] != (entry["exit"], entry["stderr"], entry["writes"]):
            raise Failure(f"{case.id}: 手順 2 の固定期待と違う\n期待 {entry['writes']!r}\n実際 {results['rust'][2]!r}")
        return True
    if fixtures is None:
        return False
    stored = fixtures.get(case.id)
    if stored is None:
        raise Failure(f"固定期待({os_fixture_path().name})が無い")
    if step2_os_entry(rules, results["rust"]) != stored:
        raise Failure(f"{case.id}: 手順 2 の固定期待と違う\n期待 {stored}\n実際 {step2_os_entry(rules, results['rust'])}")
    return True


# ---- 照合の生成入力(1 万件以上) ----

def check_corpus(ctx):
    """旧版の関数を 1 回の Python の起動でまとめて当てた結果・固定期待・Rust の 3 者を比べる。"""
    fixture_bytes = CORPUS_FIXTURE.read_bytes()
    corpus = json.loads(fixture_bytes.decode("utf-8"))
    out = ctx.temp / "corpus.json"
    done = subprocess.run([ctx.python.path, "-B", str(CORPUS_GENERATOR), "--out", str(out)],
                          env=child_env({}), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=300)
    if (done.returncode, done.stdout, done.stderr) != (0, b"", b""):
        raise Failure(f"旧版の照合を当てられない: {done.returncode} {done.stderr[-2000:]!r}")
    if out.read_bytes() != fixture_bytes:
        raise Failure("旧版の照合の結果が固定期待(matcher-corpus.json)と違う")
    out.unlink()
    done = subprocess.run([ctx.rust, MATCHER_FLAG, str(CORPUS_FIXTURE)], env=child_env({}),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if done.returncode != 0 or done.stderr:
        raise Failure(f"Rust の照合を当てられない: {done.returncode} {done.stderr[-2000:]!r}")
    got = json.loads(done.stdout.decode("utf-8"))
    want = [{"links": case["links"], "codes": case["codes"]} for case in corpus["cases"]]
    if len(got) != len(want) or len(want) < 10000:
        raise Failure(f"照合の件数が違う(Rust {len(got)}・固定期待 {len(want)})")
    for index, (left, right) in enumerate(zip(got, want)):
        if left != right:
            raise Failure(f"照合の {index} 件目が違う: {corpus['cases'][index]['text']!r}\nRust {left}\n期待 {right}")
    return len(want)


# ---- 1 件の比較 ----

@dataclass
class Outcome:
    status: str  # 合格・未実施・不合格
    message: str = ""
    fixed: bool = False  # 固定期待と比べたか
    record: bool = True  # 書込みの記録を比べたか


def needs_injection(case):
    return bool(case.rules or case.partial or case.step2 or case.derive)


def legacy_side(ctx, case):
    """旧版から Rust の期待を作る。戻り値は (期待, Rust に当てる故障, 記録の動作を比べるか, 別の期待)。"""
    recorded = ctx.legacy_recorded
    if case.partial:
        expected, params, labels = partial_expectation(ctx, case, recorded)
        return expected, params, labels, None
    params = {}
    if case.derive is not None:
        base = execute(ctx, case, "legacy", rules="")
        params["rules"] = case.derive(base)
    if case.encoding_env is not None:
        legacy = observe(ctx, case, execute(ctx, case, "legacy", env=case.encoding_env), recorded)
        configured = observe(ctx, case, execute(ctx, case, "legacy"), recorded, fatal=case.encoding_fatal)
        differs = (configured.exit, configured.stdout, configured.stderr) != \
            (legacy.exit, legacy.stdout, legacy.stderr)
        if differs != case.encoding_differs:
            raise Failure(f"設定ありの旧版が、設定なしの旧版と{'同じ' if case.encoding_differs else '違う'}"
                          "(別の期待と違う)")
        return legacy, params, True, configured
    legacy = observe(ctx, case, execute(ctx, case, "legacy", **params), recorded)
    return legacy, params, True, None


def is_interrupt(case, legacy):
    return case.interrupt if case.interrupt is not None else legacy.exit == -signal.SIGINT


def run_case(ctx, case, fixtures, mac_cache, measure=False):
    """1 件を比べる。measure なら、旧版から作った期待の固定期待の形を返す。"""
    if not LINUX and not MAC:
        raise Failure("この OS は比較の対象でない")
    if MAC and not ctx.legacy_recorded and needs_injection(case):
        raise Skip(SKIP_NO_RECORD)
    if case.step2 is not None:
        if measure:
            return check_step2(ctx, case, mac_cache, fixtures, measure=True)
        return Outcome("合格", fixed=check_step2(ctx, case, mac_cache, fixtures))
    ctx.note = ""
    legacy, params, labels, configured = legacy_side(ctx, case)
    interrupt = is_interrupt(case, legacy)
    marks = ctx.marks()
    entry = canonical(legacy, marks, interrupt, labels)
    if configured is not None:
        entry["configured"] = canonical(configured, marks)
    if case.fixture:
        # この OS の固定期待が無いとき、ログへ出す(管理担当が写して固定期待にする)。
        ctx.measured[case.id] = entry
    if measure:
        return entry
    rust_result = execute(ctx, case, "rust", **params)
    rust = observe(ctx, case, rust_result)
    compare_obs(case.id, legacy, rust, interrupt, labels)
    for kind, result in (("Rust", rust_result),):
        if (result.stdout or result.stderr) and not result.writes:
            raise Failure(f"{kind}の書込みが記録に現れない")
    fixed = False
    if case.expected is not None and (case.expected_os is None or case.expected_os == sys.platform):
        expected = case.expected
        got = (rust.exit, rust_result.stdout, rust.stderr.replace(os.fsencode(ctx.root), ROOT_MARK))
        want = (expected["exit"], raw(expected["stdout"]), raw(expected["stderr"]))
        if got != want:
            raise Failure(f"既存の固定期待と違う\n期待 {want!r}\n実際 {got!r}")
        fixed = True
    if fixtures is not None and case.fixture:
        check_fixture(fixtures.get(case.id), legacy, rust, configured, marks, interrupt, labels)
        fixed = True
    return Outcome("合格", ctx.note, fixed=fixed, record=legacy.writes is not None)


def check_fixture(entry, legacy, rust, configured, marks, interrupt, labels):
    """旧版から作った期待・固定期待・Rust の 3 者を比べる(両側の同じ誤りを拒む)。

    固定期待が記録なしで作られていれば(Mac で旧版が記録に現れないとき)、記録の欄を除いて比べる。
    """
    if entry is None:
        raise Failure(f"固定期待({os_fixture_path().name})が無い")
    stored = {key: value for key, value in entry.items() if key != "configured"}
    got = canonical(rust, marks, interrupt, labels)
    if stored.get("writes", stored.get("stdout_writes")) is None:
        got = without_records(got)
    if got != stored:
        raise Failure(f"Rust が固定期待と違う\n期待 {stored}\n実際 {got}")
    mine = canonical(legacy, marks, interrupt, labels)
    if mine.get("writes", mine.get("stdout_writes")) is None:
        stored = without_records(stored)
    if mine != stored:
        raise Failure("旧版から作った期待が固定期待と違う")
    if (configured is None) != ("configured" not in entry):
        raise Failure("設定ごとの別の期待の有無が、固定期待と違う")
    if configured is not None and canonical(configured, marks) != entry["configured"]:
        raise Failure(f"設定ありの旧版が、別の期待と違う\n期待 {entry['configured']}\n実際 {canonical(configured, marks)}")


# ---- 実行 ----

def probe_recording(ctx, kinds=("legacy", "rust")):
    """試しの書込み(引数 0 個の docstring)が、旧版と Rust の記録に現れるかを別々に確かめる。"""
    case = Case("probe-recording", "basic", [], [])
    found = {"rust": True}
    for kind in kinds:
        saved = ctx.legacy_recorded
        ctx.legacy_recorded = True
        try:
            result = execute(ctx, case, kind)
            found[kind] = any(w.fd == 1 and b"Markdown" in w.data for w in result.writes)
        except Failure:
            if kind == "rust" or not MAC:
                raise
            found[kind] = False
        finally:
            ctx.legacy_recorded = saved
    if not found["rust"]:
        raise Failure("Rust の試しの書込みが記録に現れない")
    if not found["legacy"]:
        if not MAC:
            raise Failure("旧版の試しの書込みが記録に現れない(Linux では閉じた一覧に当たらない)")
        ctx.legacy_recorded = False
    print("試しの書込みの記録: 旧版 " + ("あり" if found["legacy"] else "なし") +
          ("・Rust あり" if "rust" in kinds else ""), flush=True)


def all_cases():
    step2, step2_data = step2_cases()
    cases = legacy_cases() + linux_traceback_cases() + behavior_cases() + partial_cases() + step2
    return cases, step2_data


def check_case_list(cases, fixture_ids):
    """ケースの欠落・重複・0 件と、固定期待の一覧との違いを拒む。"""
    ids = [case.id for case in cases]
    if not ids:
        raise Failure("比較ケースが 0 件")
    if len(ids) != len(set(ids)):
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        raise Failure("比較ケースの ID が重複している: " + ", ".join(duplicated))
    for case in cases:
        if case.group not in GROUPS:
            raise Failure(f"{case.id}: 未知の組 {case.group}")
    missing = sorted(set(REQUIRED) - set(ids))
    if missing:
        raise Failure("必須のケースが無い: " + ", ".join(missing))
    listed = [case.id for case in cases if case.step2 is None]
    if fixture_ids is not None and sorted(listed) != sorted(fixture_ids):
        extra = sorted(set(listed) - set(fixture_ids))
        lost = sorted(set(fixture_ids) - set(listed))
        raise Failure(f"ケースの一覧が固定期待の一覧と違う(余計 {extra}・欠け {lost})")
    empty = [name for name in GROUPS if name != "corpus" and not any(case.group == name for case in cases)]
    if empty:
        raise Failure("ケースが 0 件の組がある: " + ", ".join(empty))


# 設計の「テスト計画」の必須のケースの組から、項目ごとに必ず要る ID(一覧の生成を誤っても欠けを見つける)。
REQUIRED = (
    "legacy:basic-doc-example",
    # 引数
    "args-none-optimize-0", "args-none-optimize-1", "args-none-optimize-2", "args-none-optimize-bad",
    "legacy:args-empty-string", "legacy:args-dash-like", "args-non-utf8", "legacy:args-missing", "legacy:args-fifo",
    "legacy:args-file-non-md", "legacy:args-file-under-excluded", "legacy:args-symlinks", "legacy:args-self-loop",
    "traceback:target-isdir-eacces", "traceback:target-isdir-enametoolong", "legacy:args-normalized-spelling",
    "legacy:args-absolute-missing", "legacy:args-duplicate-and-order", "traceback:target-warn-then-eacces",
    # 走査
    "legacy:walk-sort-order", "legacy:walk-sort-non-utf8", "legacy:walk-name-patterns", "legacy:walk-newline-name",
    "legacy:walk-exclude", "legacy:args-dir-excluded-self", "legacy:walk-symlink-dir-not-followed",
    "traceback:read-isdir", "walk-kinds", "walk-fifo-writer", "walk-unreadable-dir", "walk-unsearchable-dir",
    "walk-permission-abort", "traceback:rglob-deep-enametoolong", "traceback:rglob-entry-isdir-enametoolong",
    "rglob-depth-988", "rglob-depth-989", "traceback:rglob-recursion",
    # 本文
    "legacy:text-invalid-utf8", "legacy:text-bom", "legacy:text-crlf-cr", "legacy:link-multiline",
    "legacy:link-boundaries", "legacy:link-unicode-space", "legacy:link-skip-prefixes", "legacy:link-targets",
    "legacy:link-nul-absolute", "body-absolute-loop", "legacy:link-symlink-mid", "traceback:link-resolve-loop",
    "legacy:link-loop-then-dotdot", "body-loop-absolute-target", "body-double-slash-md",
    "traceback:link-exists-enametoolong", "traceback:link-exists-eacces", "traceback:link-resolve-nul",
    "body-relative-nul", "link-chain-992", "link-chain-993", "traceback:link-resolve-recursion",
    "traceback:link-resolve-getcwd", "selftest-fake-file-line",
    # コード中のパス
    "traceback:code-exists-eacces", "traceback:code-alt-eacces", "legacy:codepath-boundaries",
    "legacy:codepath-backtick-mixup", "legacy:codepath-skip-prefixes", "legacy:codepath-ancestors",
    "legacy:codepath-absolute", "legacy:codepath-shallow", "code-location-dotdot", "code-location-dotdot2",
    "code-location-top-double-slash", "code-location-top-triple-slash", "code-location-top-single-slash",
    "code-location-root-child", "code-chain-992", "code-chain-993", "corpus:matcher",
    # 出力
    *(f"output-{total}-{mode}" for total in (8191, 8192, 8193, 16383, 16384, 16385)
      for mode in ("pipe", "file", "pty", "unbuffered", "file-unbuffered", "pty-unbuffered")),
    "output-pty-cr-lf-names", "args-none-pty", "output-same-pipe-exception",
    # 故障
    *(f"fault-{name}-{mode}" for mode in ("buffered", "unbuffered") for name in (
        "stdout-devfull", "stdout-broken", "stdout-eagain", "stderr-devfull-exception", "stderr-devfull-noexception",
        "stderr-broken-exception", "stderr-broken-noexception", "stdout-rlimit")),
    "stdio-closed-stdin", "stdio-closed-stdout", "stdio-closed-stderr", "stdio-stdin-dir", "stdio-stdout-dir",
    "stdio-stderr-dir", "env-hashseed-bad-word", "env-intmax-bad-small", "env-both-bad", "env-hashseed-ok-random",
    "env-intmax-ok-min", "startup-hashseed-broken-stderr-sigpipe-default", "startup-hashseed-broken-stderr-sigpipe-ignore",
    # 部分書込み(固定名の欄の途中・外・旧版だけが切れる範囲)
    *(f"partial-rlimit-{mode}-{position}" for mode in ("buffered", "unbuffered")
      for position in ("field", "outside", "legacy-only")),
    *(f"partial-{kind}-{position}" for kind in ("lost-unbuffered", "resend-buffered", "cut-buffered", "cut-unbuffered")
      for position in ("field", "outside", "legacy-only")),
    # 信号
    *(f"signal-fifo-{sig}-{prefix}{mode}" for sig in ("sigint", "sigterm") for prefix in ("", "warn-")
      for mode in ("pipe", "unbuffered", "pty")),
    *(f"signal-write-sigint-{mode}-{when}" for mode in ("pipe", "unbuffered", "pty") for when in ("1", "2", "last")),
    "signal-sigint-ignored", "traceback:read-fifo-sigint",
    # 段階ごとの失敗と中断
    "fault-stdout-flushio-eagain-sigint-buffered", "fault-stdout-close-fail-buffered",
    "step2:one:buffered:file:sigint", "step2:two:buffered:separator:fail-once", "step2:rec:buffered:repeated:cut",
    "step2:one:unbuffered:file:cut", "step2:two:unbuffered:header2:fail-persist-epipe",
    # 文字コードの設定
    "encoding-pythonioencoding-utf8-strict-non-utf8-name", "encoding-pythonioencoding-ascii",
    "encoding-pythonioencoding-unknown", "env-lang-c-utf8",
    # 環境変数
    "args-none-optimize-unset", "env-hashseed-ok-zero", "env-unbuffered-one",
)


def summary_line(outcomes, cases, elapsed, python, corpus_count):
    groups = {}
    for case in cases:
        bucket = groups.setdefault(case.group, {"全": 0, "合格": 0, "未実施": 0, "不合格": 0, "固定期待": 0})
        outcome = outcomes[case.id]
        bucket["全"] += 1
        bucket[outcome.status] += 1
        bucket["固定期待"] += 1 if outcome.fixed else 0
    lines = []
    for name, label in GROUPS.items():
        if name in groups:
            b = groups[name]
            lines.append(f"  {label}: 全 {b['全']} 件・合格 {b['合格']}・未実施 {b['未実施']}・不合格 {b['不合格']}"
                         f"(固定期待と比べた {b['固定期待']} 件)")
    return lines


def main_compare(rust, only=None):
    if os.geteuid() == 0:
        raise Failure("root でない利用者で流す")
    if not (LINUX or MAC):
        raise Failure("この OS は比較の対象でない")
    if hashlib.sha256(LEGACY.read_bytes()).hexdigest() != LEGACY_SHA256:
        raise Failure("旧版が変わった")
    started = time.monotonic()
    python = find_python()
    global SCANDIR_PYTHON
    SCANDIR_PYTHON = python.path
    print(f"旧 Python: {python.version}({python.path})", flush=True)
    if python.launcher:
        print(f"  起動用のプログラム {python.launcher} を通さず、本体を直接起動する", flush=True)
    if python.version != "3.10.11":
        print("  注意: 本家 3.10.11 でない。標準ライブラリの中身が同じでも、C の部分の動作の差(例: 3.10.12 は"
              "書込みの失敗が続くときの試行が 1 回多い)で失敗するケースがある。成功にも skip にもしない", flush=True)
    for name in STDLIB_SHA256:
        state = "本家 3.10.11 と同じ" if name not in python.mismatched else "本家 3.10.11 と違う(このフレームを含むケースは失敗)"
        print(f"  {name}: sha256 {python.sha256[name]}({state})", flush=True)
    # ケースの一覧は OS に依らない(cases-linux.json の case_ids)。固定期待は OS ごと(cases-<OS>.json)。
    list_data = load_fixture_document(CASES_FIXTURE)
    os_path = os_fixture_path()
    os_data = list_data if os_path == CASES_FIXTURE else (load_fixture_document(os_path) if os_path.exists() else None)
    if os_data is not None and os_data["case_ids"] != list_data["case_ids"]:
        raise Failure(f"{os_path.name} のケースの一覧が {CASES_FIXTURE.name} と違う")
    fixtures = os_data["cases"] if os_data is not None else None
    if fixtures is None:
        print(f"この OS の固定期待 {os_path.name} が無い。旧版で測った値を最後に出し、比較は不合格にする", flush=True)
    cases, step2_data = all_cases()
    corpus_case = Case("corpus:matcher", "corpus")
    check_case_list(cases + [corpus_case], list_data["case_ids"] + ["corpus:matcher"])
    check_step2_coverage(step2_data)
    temp_base = os.path.realpath(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, dir=temp_base) as temp:
        os.chmod(temp, 0o700)
        top = tempfile.mkdtemp(prefix="r4u-top-", dir="/tmp")
        ctx = None
        try:
            ctx = Context(python, rust, build_shim(temp), temp, top)
            probe_recording(ctx)
            if only:
                # 調べるための部分実行。合格にはしない。
                for case in cases:
                    if case.id in only:
                        for kind in ("legacy", "rust"):
                            show(case, kind, execute(ctx, case, kind))
                print("部分実行(--only)は合格にしない")
                return 2
            outcomes = {}
            mac_cache = {}
            corpus_count = 0
            for case in cases + [corpus_case]:
                try:
                    if case is corpus_case:
                        corpus_count = check_corpus(ctx)
                        outcome = Outcome("合格", fixed=True)
                    else:
                        outcome = run_case(ctx, case, fixtures, mac_cache)
                except Skip as exc:
                    outcome = classify_skip(str(exc), MAC)
                except Failure as exc:
                    outcome = Outcome("不合格", str(exc))
                outcomes[case.id] = outcome
                if outcome.status == "合格":
                    note = "" if outcome.record else "(書込みの記録の比較は未実施: " + SKIP_NO_RECORD + ")"
                    note += f"({outcome.message})" if outcome.message else ""
                    print(f"合格 {case.id}{note}", flush=True)
                elif outcome.status == "未実施":
                    print(f"未実施 {case.id}: {outcome.message}", flush=True)
                else:
                    print(f"不合格 {case.id}: {outcome.message}", flush=True)
        finally:
            executions = ctx.counter if ctx is not None else 0
            restore_and_remove(top)
            restore_and_remove(top + ".md")
            restore_and_remove(os.path.join(temp, "case"))
    every = cases + [corpus_case]
    if fixtures is None:
        skipped = {i: o.message for i, o in outcomes.items() if o.status == "未実施"}
        document = fixture_document(python, list_data["case_ids"], ctx.measured, skipped, ctx.legacy_recorded)
        print(fixture_dump(os_path.name, document), flush=True)
    elapsed = time.monotonic() - started
    limits = {name: resource.getrlimit(getattr(resource, name))
              for name in ("RLIMIT_AS", "RLIMIT_NOFILE", "RLIMIT_CPU", "RLIMIT_STACK", "RLIMIT_FSIZE")}
    print("受け継いだ資源の上限(比較器は設定しない。RLIMIT_FSIZE はケースが与えるときだけ子で設定): " +
          ", ".join(f"{name}={value}" for name, value in limits.items()))
    counts = {status: sum(1 for o in outcomes.values() if o.status == status) for status in ("合格", "未実施", "不合格")}
    fixed = sum(1 for o in outcomes.values() if o.fixed)
    no_record = sum(1 for o in outcomes.values() if o.status == "合格" and not o.record)
    print("組ごとの件数:")
    for line in summary_line(outcomes, every, elapsed, python, corpus_count):
        print(line)
    reasons = {}
    for o in outcomes.values():
        if o.status == "未実施":
            reasons[o.message] = reasons.get(o.message, 0) + 1
    if reasons:
        print("未実施の理由(成功に数えない): " + "・".join(f"{r} {n} 件" for r, n in reasons.items()))
    if no_record:
        print(f"書込みの記録を比べずに合格にした件数(成功の一部に数えない): {no_record} 件({SKIP_NO_RECORD})")
    print(f"照合の生成入力: {corpus_count} 件(旧版・固定期待・Rust)")
    print(f"子の実行: {executions} 回(旧版と Rust の合計。照合の生成入力と旧 Python の確かめを除く)")
    print(f"R4U の比較: 合格 {counts['合格']} 件 / 全 {len(every)} 件・未実施 {counts['未実施']} 件・"
          f"不合格 {counts['不合格']} 件・固定期待と比べた {fixed} 件 / {sys.platform} / 旧 Python {python.version}"
          f"({python.path})/ {elapsed:.1f} 秒")
    status = final_status(outcomes, [case.id for case in every], MAC)
    if status:
        print("不合格の ID: " + ", ".join(i for i, o in outcomes.items() if o.status != "合格"))
    return overall_status(status, fixtures, os_path)


def overall_status(status, fixtures, os_path):
    """この OS の固定期待が無ければ、ほかが全部合格でも不合格(成功にも skip にもしない)。"""
    if fixtures is None:
        print(f"不合格: この OS の固定期待 {os_path.name} が無い(旧版・固定期待・Rust の 3 者で比べていない)。"
              f"上の区切りの間の JSON を native/fixtures/r4u/{os_path.name} に置く(--extract-fixture <ログ> で取り出せる)")
        return 1
    return status


def classify_skip(reason, mac):
    """未実施は、Mac で閉じた一覧の条件に当たるときだけ。ほかは不合格。"""
    if not mac or reason not in CLOSED_LIST:
        return Outcome("不合格", f"閉じた一覧に無い未実施: {reason}")
    return Outcome("未実施", reason, record=False)


def final_status(outcomes, ids, mac):
    """全ケースが合格(Mac は閉じた一覧の未実施を含む)で、1 件以上が合格のときだけ 0。"""
    if not ids or set(outcomes) != set(ids):
        return 1
    statuses = [outcomes[i].status for i in ids]
    if not statuses.count("合格"):
        return 1
    for case_id in ids:
        outcome = outcomes[case_id]
        if outcome.status == "合格":
            continue
        if outcome.status == "未実施" and mac and outcome.message in CLOSED_LIST:
            continue
        return 1
    return 0


def show(case, kind, result):
    print(f"--- {case.id}/{kind}: 終了 {result.code}")
    print(f"stdout {result.stdout[:300]!r}")
    print(f"stderr {result.stderr[-600:]!r}")
    for w in result.writes:
        print(f"  write fd={w.fd} len={w.length} ret={w.ret} errno={w.errno} {w.action} {w.data[:80]!r}")


def measure_setup():
    if not (LINUX or MAC):
        raise Failure("この OS は比較の対象でない")
    python = find_python()
    if python.mismatched or python.version != "3.10.11":
        raise Failure("固定期待は本家と同じ中身の 3.10.11 で作る")
    global SCANDIR_PYTHON
    SCANDIR_PYTHON = python.path
    return python


def main_measure_cases(out):
    """この OS の旧版で固定期待(cases-<OS>.json)を作る。Mac は手順 2 のケースも入れる(step2.json は Linux)。"""
    python = measure_setup()
    cases, _ = all_cases()
    case_ids = [case.id for case in cases if case.step2 is None]
    if not MAC:
        cases = [case for case in cases if case.step2 is None]
    skipped = {}
    temp_base = os.path.realpath(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, dir=temp_base) as temp:
        os.chmod(temp, 0o700)
        top = tempfile.mkdtemp(prefix="r4u-top-", dir="/tmp")
        try:
            ctx = Context(python, None, build_shim(temp), temp, top)
            probe_recording(ctx, kinds=("legacy",))
            mac_cache = {}
            for case in cases:
                if not case.fixture:
                    continue
                try:
                    run_case(ctx, case, None, mac_cache, measure=True)
                except Skip as exc:
                    if classify_skip(str(exc), MAC).status != "未実施":
                        raise Failure(f"{case.id}: 閉じた一覧に無い未実施: {exc}") from exc
                    skipped[case.id] = str(exc)
                    print(f"未実施 {case.id}: {exc}", flush=True)
                    continue
                print(f"固定期待 {case.id}", flush=True)
        finally:
            restore_and_remove(top)
            restore_and_remove(top + ".md")
            restore_and_remove(os.path.join(temp, "case"))
    data = fixture_document(python, case_ids, ctx.measured, skipped, ctx.legacy_recorded)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"固定期待: {len(data['cases'])} 件・一覧 {len(case_ids)} 件・未実施 {len(skipped)} 件 → {out}")
    return 0


OS_NAME = "linux" if LINUX else ("macos" if MAC else sys.platform)


def os_fixture_path():
    return FIXTURES / f"cases-{OS_NAME}.json"


def load_fixture_document(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["legacy"]["sha256"] != LEGACY_SHA256 or data["python"]["stdlib_sha256"] != STDLIB_SHA256:
        raise Failure(f"{path.name} の旧版か標準ライブラリの SHA-256 が違う")
    if data["platform"] != (OS_NAME if path != CASES_FIXTURE else "linux"):
        raise Failure(f"{path.name} の OS が違う: {data['platform']}")
    return data


def fixture_document(python, case_ids, entries, skipped, legacy_recorded):
    """固定期待の文書(--measure-cases の出力と、固定期待が無いときにログへ出す値は同じ形)。"""
    import platform
    return {
        "schema_version": 1,
        "purpose": f"R4U の比較の固定期待({OS_NAME}。旧版〈本家 CPython 3.10.11〉から作った Rust の期待)",
        "generator": "native/ci/check_r4u.py --measure-cases(または固定期待が無いときの比較器のログ)",
        "python": {"version": python.version, "stdlib_sha256": STDLIB_SHA256},
        "legacy": {"path": LEGACY_REL, "sha256": LEGACY_SHA256},
        "platform": OS_NAME,
        "machine": platform.machine(),
        "legacy_recorded": legacy_recorded,
        "marks": {"@ROOT@": "入力の木の根(作業場所)", "@TOP@": "/tmp の直下に作る一時フォルダ",
                  "@TOPNAME@": "その名前", "@LEGACY@": "旧スクリプトの絶対パス(利用者の入力に置いた偽の File の行)"},
        "case_ids": list(case_ids),
        "skipped": dict(sorted(skipped.items())),
        "cases": dict(sorted(entries.items())),
    }


FIXTURE_BEGIN = "===== R4U の固定期待 {name} ここから ====="
FIXTURE_END = "===== R4U の固定期待 {name} ここまで(sha256 {digest})====="
LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")


def fixture_dump(name, document):
    """CI のログから写せる形: 区切りの行の間に JSON。終わりの行に JSON の SHA-256 を置く(混ざりを見つける)。"""
    text = json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return "\n".join([FIXTURE_BEGIN.format(name=name), text, FIXTURE_END.format(name=name, digest=digest)])


def extract_fixture(log, name):
    """CI のログ(行頭の時刻があってもよい)から、区切りの行の間の JSON を取り出し、SHA-256 を確かめる。"""
    lines = [LOG_PREFIX.sub("", line) for line in log.splitlines()]
    begin = FIXTURE_BEGIN.format(name=name)
    starts = [i for i, line in enumerate(lines) if line == begin]
    if len(starts) != 1:
        raise Failure(f"{name} の始まりの行が 1 つでない: {len(starts)}")
    end_re = re.compile(re.escape(FIXTURE_END.format(name=name, digest="@")).replace("@", "([0-9a-f]{64})") + "$")
    for index in range(starts[0] + 1, len(lines)):
        found = end_re.match(lines[index])
        if found:
            text = "\n".join(lines[starts[0] + 1:index])
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != found.group(1):
                raise Failure(f"{name} の JSON の SHA-256 が終わりの行と違う(ほかの出力が混ざった)")
            return json.loads(text)
    raise Failure(f"{name} の終わりの行が無い")


def main_extract_fixture(log_path, out):
    name = out.name
    data = extract_fixture(log_path.read_text(encoding="utf-8", errors="replace"), name)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"固定期待: {len(data['cases'])} 件 → {out}")
    return 0


def main_measure_step2(out):
    python = measure_setup()
    temp_base = os.path.realpath(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, dir=temp_base) as temp:
        top = tempfile.mkdtemp(prefix="r4u-top-", dir="/tmp")
        try:
            ctx = Context(python, None, build_shim(temp), temp, top)
            entries = step2_measure(ctx)
        finally:
            restore_and_remove(top)
            restore_and_remove(os.path.join(temp, "case"))
    data = {
        "schema_version": 1,
        "purpose": "手順 2(呼出履歴を出している間)の書込みの失敗と SIGINT の、部品ごとの扱い(本家 CPython 3.10.11 の実測)",
        "generator": "native/ci/check_r4u.py --measure-step2",
        "python": {"version": python.version, "stdlib_sha256": STDLIB_SHA256,
                   "legacy": {"path": LEGACY_REL, "sha256": LEGACY_SHA256}},
        "platform": sys.platform,
        "actions": STEP2_ACTIONS,
        "table": step2_table(entries),
        "entries": entries,
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"手順 2 の表: {len(data['table'])} 行・{len(entries)} 件 → {out}")
    return 0


def main(argv=None):
    # 深い木(約 1000 段)を辿る確かめと片付けのため、比較器だけ再帰の上限を上げる。
    sys.setrecursionlimit(20000)
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fixture", help="Rust の検査用の実行物")
    group.add_argument("--measure-cases", action="store_true")
    group.add_argument("--measure-step2", action="store_true")
    group.add_argument("--extract-fixture", type=Path, metavar="ログ",
                       help="固定期待が無い OS の CI のログから、比較器が出した固定期待を取り出す(--out に書く)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", action="append", help="調べるための部分実行(ケースの ID。合格にはしない)")
    args = parser.parse_args(argv)
    try:
        if args.extract_fixture:
            return main_extract_fixture(args.extract_fixture, args.out or FIXTURES / "cases-macos.json")
        if args.measure_cases:
            return main_measure_cases(args.out or os_fixture_path())
        if args.measure_step2:
            return main_measure_step2(args.out or STEP2_FIXTURE)
        return main_compare(args.fixture, args.only)
    except Failure as exc:
        print(f"不合格: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
