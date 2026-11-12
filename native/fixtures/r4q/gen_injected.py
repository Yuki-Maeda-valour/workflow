#!/usr/bin/env python3
"""R4Q の差し替えの比較の固定期待を、本家 CPython 3.10.11 で旧版の関数を mock して作る。製品の入口ではない。

- injected.json: ケースの ID ごとに「入力・差し替えた操作の応答 → 期待(操作の呼出しの並び・結果・文)」。
  Rust の単体テスト(native/crates/cli/tests/r4q_units.rs)が、同じ応答を操作の単位の trait で与えて照らす。
- 操作の単位は、設計の「比べ方の決まり」の表の名前。旧版の mock する名前と記録の形:
  monotonic・sleep(time)、getpid・getppid(os)、prctl・get_errno(ctypes)、set_handler(signal.signal)、
  spawn(loop-supervisor の subprocess.Popen)、waitpid_any(os.waitpid(-1, WNOHANG))、
  read_children・read_stat(Path.read_text・Path.read_bytes)、pidfd_open・pidfd_send_signal・close、
  spawn_monitor・poll_monitor・wait_monitor(check-process の launch_monitor が呼ぶ Popen と、その poll・wait)、
  token_hex(secrets)、pipe・select・read(os・select)、killpg、open・fstat・write・fsync(os)、
  open_output・close_output・fstat_output・rewind_output・read_output(Path.open('w+b') の stream)。
  platform と has_pidfd は、呼出しを記録しない世界の設定(sys.platform と hasattr)として与える。
- 応答: {"ok": 値} か {"err": {"errno", "text"[, "filename"]}} か {"timeout": true}(wait_monitor)か
  {"raise": [型, 文]}(token_hex の NotImplementedError・RuntimeError)。/proc の読取りの err に "at": "read" を
  書くと、開いた後の読む段の誤り(ファイル名が付かない)になる。
  どの応答にも "advance"(時計を進める秒)と "signal"(この操作の後に、置いた信号の処理を呼ぶ番号)を書ける。
  SIGINT は、置き換えるまで Python の既定の処理(KeyboardInterrupt を投げる)。
- run_command のケースは、run_command を呼ぶまでの Python のフレームの数("caller_frames"。この生成器は 4)も
  入力に持つ(記録と通知の json.loads の入れ子の上限を決める)。
  応答の並びは操作ごとの待ち行列。尽きたら操作ごとの既定の応答を使う(ケースの "defaults" で替えられる)。
- 時計は 1000.0 から始め、sleep(s) は s だけ進める。monotonic の {"ok": v} は時計を v にする。
- errno は Linux と Mac で番号と例外の型が同じものに限る。Python が C の中で繰り返す EINTR(PEP 475)を
  応答に使わない(拒む)。文は応答で与え、OS の表を引かない(どの機械でも同じバイト列になる)。
- 使い方(Linux でも Mac でも同じ出力):
  "$R4Q_PYTHON" -B native/fixtures/r4q/gen_injected.py [--out <ファイル>]
"""
import argparse
import contextlib
import ctypes
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
import traceback
import types
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCRIPTS = ROOT / "plugins/dev-workflow/skills/ship-task/scripts"
LEGACY = {"check-process.py": SCRIPTS / "check-process.py", "loop-supervisor.py": SCRIPTS / "loop-supervisor.py"}
LEGACY_SHA256 = {
    "check-process.py": "f50b3278ee99fbd1587b8282141de57e264c9405253f766a8032011c9bff4649",
    "loop-supervisor.py": "6356a2195b728249411f0944646fe30f1df4297915eec14e710854633dabe2ab",
}
STDLIB = Path(os.path.dirname(os.__file__))
STDLIB_SHA256 = {
    "subprocess.py": "53bb0d0780e166ef4ae94f3b2a817a8aae49a84d899db1f4c31f933d429057d6",
    "pathlib.py": "936f14a3ecc4ac5e896e4d673666d76b25a9d581f3b4c8d8d424e830fda2a69b",
    "json/__init__.py": "d5d41e2c29049515d295d81a6d40b4890fbec8d8482cfb401630f8ef2f77e4d5",
    "json/decoder.py": "9f02654649816145bc76f8c210a5fe3ba1de142d4d97a1c93105732e747c285b",
    "json/encoder.py": "06b881b824f71e95d72af4ab865de4c35553e791b6d959a125caac61401cc350",
}
# Linux と Mac で番号と Python の例外の型が同じ errno(設計の一覧)。
ERRNO = {
    "EPERM": 1, "ENOENT": 2, "ESRCH": 3, "EINTR": 4, "EIO": 5, "E2BIG": 7, "ENOEXEC": 8, "EBADF": 9,
    "ECHILD": 10, "EACCES": 13, "EEXIST": 17, "ENOTDIR": 20, "EISDIR": 21, "EINVAL": 22, "EMFILE": 24,
    "EFBIG": 27, "ENOSPC": 28,
}
TEXT = {
    "EPERM": "Operation not permitted", "ENOENT": "No such file or directory", "ESRCH": "No such process",
    "EIO": "Input/output error", "E2BIG": "Argument list too long", "ENOEXEC": "Exec format error",
    "EBADF": "Bad file descriptor", "ECHILD": "No child processes", "EACCES": "Permission denied",
    "EEXIST": "File exists", "EINVAL": "Invalid argument", "EMFILE": "Too many open files",
    "EFBIG": "File too large", "ENOSPC": "No space left on device",
}
OPS = (
    "monotonic", "sleep", "getpid", "getppid", "prctl", "get_errno", "set_handler", "spawn", "waitpid_any",
    "read_children", "read_stat", "pidfd_open", "pidfd_send_signal", "close", "spawn_monitor", "poll_monitor",
    "wait_monitor", "token_hex", "pipe", "select", "read", "killpg", "open", "fstat", "write", "fsync",
    "open_output", "close_output", "fstat_output", "rewind_output", "read_output",
)
# Python が C の中で EINTR を繰り返す操作(PEP 475)。応答に EINTR を使わない。
RETRIED = {
    "sleep", "spawn", "waitpid_any", "read_children", "read_stat", "spawn_monitor", "poll_monitor",
    "wait_monitor", "select", "read", "open", "fstat", "write", "fsync", "open_output", "fstat_output",
    "rewind_output", "read_output",
}
TOKEN = "0123456789abcdef" * 4
# token_hex(os.urandom)が投げうる、OSError でない例外(`/dev/urandom` の段)。
RAISABLE = {"NotImplementedError": NotImplementedError, "RuntimeError": RuntimeError}
CLOCK = 1000.0
LIMIT = 20000
FLAG_NAMES = ("O_CREAT", "O_EXCL", "O_NOFOLLOW", "O_NONBLOCK", "O_WRONLY", "O_RDWR")
# pid_t の引数の変換(os.pidfd_open と同じ _Py_PARSE_PID)。C の int の外の pid は、本物の os.kill に渡して
# 呼出しの前の OverflowError を出させる(Mac の Python には pidfd_open が無いため)。
REAL_KILL = os.kill


def check_python():
    if sys.version_info[:3] != (3, 10, 11):
        raise SystemExit("本家と同じ中身の CPython 3.10.11 で流す")
    for name, digest in STDLIB_SHA256.items():
        if hashlib.sha256((STDLIB / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{name} が本家 3.10.11 と違う")
    for name, path in LEGACY.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != LEGACY_SHA256[name]:
            raise SystemExit(f"旧版 {name} が main e19ca1d と違う")


class Abort(Exception):
    """固定期待を作れない(ケースの誤り)。"""


class World:
    """差し替えた OS の世界(応答の待ち行列・呼出しの記録・時計・信号の処理)。"""

    def __init__(self, spec):
        self.queues = {name: list(items) for name, items in spec.get("responses", {}).items()}
        self.defaults = dict(spec.get("defaults", {}))
        for name, items in list(self.queues.items()) + [(name, [item]) for name, item in self.defaults.items()]:
            if name not in OPS:
                raise Abort(f"操作の名前でない: {name}")
            for item in items:
                validate(name, item)
        self.platform = spec.get("platform", "linux")
        self.has_pidfd = spec.get("has_pidfd", True)
        self.pid = spec.get("pid", 100)
        self.ppid = spec.get("ppid", 50)
        self.calls = []
        self.clock = CLOCK
        # SIGINT は、置き換えるまで Python の既定の処理(KeyboardInterrupt を投げる)。
        self.handlers = {int(signal.SIGINT): signal.default_int_handler}
        self.saved = 0
        self.next_fd = 10
        self.output_fds = set()

    def record(self, call):
        self.calls.append(call)
        if len(self.calls) > LIMIT:
            raise Abort("呼出しが多すぎる")

    def take(self, name):
        queue = self.queues.get(name)
        if queue:
            return queue.pop(0)
        return self.defaults.get(name)

    def fd(self):
        self.next_fd += 1
        return self.next_fd

    def after(self, response):
        """応答の "signal": 置いた処理を呼ぶ(呼出元の値を返す前)。"""
        if response and "signal" in response:
            number = response["signal"]
            handler = self.handlers.get(number)
            if not callable(handler):
                raise Abort(f"信号 {number} の処理が置かれていない")
            handler(number, None)

    def step(self, name, call, response=None):
        """記録して応答を取り、時計を進める。"""
        self.record(call)
        response = self.take(name) if response is None else response
        if response and "advance" in response:
            self.clock += response["advance"]
        return response

    def finish(self, response):
        """信号を届け、誤りの応答なら OSError を投げる。"""
        self.after(response)
        if response and "err" in response:
            error = response["err"]
            if "filename" in error:
                raise OSError(error["errno"], error["text"], error["filename"])
            raise OSError(error["errno"], error["text"])

    def unused(self):
        return {name: len(items) for name, items in self.queues.items() if items}


def validate(name, response):
    keys = set(response) - {"advance", "signal"}
    if len(keys) != 1 or not keys <= {"ok", "err", "timeout", "raise"}:
        raise Abort(f"{name} の応答の形が違う: {response}")
    if "timeout" in response and name != "wait_monitor":
        raise Abort("timeout は wait_monitor の応答だけ")
    if "raise" in response and (name != "token_hex" or response["raise"][0] not in RAISABLE):
        raise Abort("raise は token_hex の NotImplementedError・RuntimeError だけ")
    if "err" in response:
        error = response["err"]
        if set(error) - {"errno", "text", "filename", "at"} or error.get("at", "read") != "read":
            raise Abort(f"{name}: err の欄が違う: {error}")
        if "at" in error and name not in ("read_children", "read_stat"):
            raise Abort("at は /proc の読取りだけ")
        if error["errno"] not in ERRNO.values():
            raise Abort(f"{name}: Linux と Mac で揃わない errno {error['errno']}")
        if error["errno"] == ERRNO["EINTR"] and name in RETRIED:
            raise Abort(f"{name}: Python が繰り返す EINTR を応答に使わない")


def ok(value=None, **extra):
    return {"ok": value, **extra}


def err(name, filename=None, text=None, **extra):
    error = {"errno": ERRNO[name], "text": text or TEXT[name]}
    if filename is not None:
        error["filename"] = filename
    return {"err": error, **extra}


def handler_name(handler):
    if isinstance(handler, tuple) and handler[0] == "saved":
        return ["previous", handler[1]]
    name = getattr(handler, "__name__", None)
    if name in ("interrupted", "receive"):
        return name
    if name == "<lambda>":
        return "nothing"
    raise Abort(f"知らない信号の処理: {handler!r}")


def flag_names(flags):
    names = [name for name in FLAG_NAMES if flags & getattr(os, name)]
    if flags & os.O_ACCMODE == os.O_RDONLY:
        names.append("O_RDONLY")
    rest = flags
    for name in FLAG_NAMES:
        rest &= ~getattr(os, name)
    if rest:
        raise Abort(f"知らない flags: {flags}")
    return sorted(names)


def to_bytes(response, default=b""):
    if not response or "ok" not in response:
        return default
    return bytes.fromhex(response["ok"])


class FakeLibc:
    def __init__(self, world):
        self.world = world

    def prctl(self, option, arg, *rest):
        if rest != (0, 0, 0):
            raise Abort("prctl の残りの引数が 0 でない")
        response = self.world.step("prctl", ["prctl", int(option), int(arg)])
        value = response["ok"] if response else 0
        self.world.finish(response)
        return value


class FakeStream:
    """Path.open('w+b') の stream(open_output の単位)。"""

    def __init__(self, world, fd):
        self.world = world
        self.number = fd

    def fileno(self):
        return self.number

    def flush(self):
        pass

    def seek(self, offset):
        if offset != 0:
            raise Abort("seek(0) だけ")
        response = self.world.step("rewind_output", ["rewind_output", self.number])
        self.world.finish(response)

    def read(self, size):
        response = self.world.step("read_output", ["read_output", self.number, size])
        value = to_bytes(response)
        self.world.finish(response)
        return value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        response = self.world.step("close_output", ["close_output", self.number])
        self.world.finish(response)
        return False


class FakePopen:
    """loop-supervisor の Popen(spawn)と、check-process の監督の Popen(spawn_monitor)。"""

    def __init__(self, world, kind, argv, **options):
        self.world = world
        self.returncode = None
        if kind == "spawn":
            if options != {"start_new_session": True, "close_fds": True}:
                raise Abort(f"spawn の設定が違う: {options}")
            response = world.step("spawn", ["spawn", list(argv)])
            self.pid = response["ok"] if response and "ok" in response else 200
            world.finish(response)
            self.args = argv
            return
        argv = list(argv)
        if argv[0] == sys.executable and argv[1] == "-B" and argv[2] == str(LEGACY["check-process.py"].resolve()):
            argv[0:3] = ["python3", "-B", "check-process.py"]
        else:
            raise Abort("監督の argv の頭が旧版と違う")
        if options.get("stdin") != subprocess.DEVNULL:
            raise Abort("監督の stdin が DEVNULL でない")
        env = options.get("env")
        launch = {
            "argv": argv,
            "cwd": options.get("cwd"),
            "env": None if env is None else [[key, value] for key, value in env.items()],
            "stdout": options["stdout"].fileno(),
            "stderr": options["stderr"].fileno(),
            "pass_fds": list(options.get("pass_fds", ())),
            "new_session": bool(options.get("start_new_session", False)),
        }
        response = world.step("spawn_monitor", ["spawn_monitor", launch])
        self.pid = response["ok"] if response and "ok" in response else 300
        world.finish(response)
        self.args = argv

    def poll(self):
        response = self.world.step("poll_monitor", ["poll_monitor", self.pid])
        value = response["ok"] if response else None
        self.world.finish(response)
        if value is not None:
            self.returncode = value
        return value

    def wait(self, timeout=None):
        seconds = None if timeout is None else float(timeout)
        response = self.world.step("wait_monitor", ["wait_monitor", self.pid, seconds])
        if response and response.get("timeout"):
            self.world.after(response)
            raise subprocess.TimeoutExpired(self.args, timeout)
        value = response["ok"] if response and "ok" in response else 0
        self.world.finish(response)
        self.returncode = value
        return value


def patches(world, stack, popen_kind):
    w = world

    def monotonic():
        response = w.step("monotonic", ["monotonic"])
        if response and "ok" in response:
            w.clock = float(response["ok"])
        value = w.clock
        w.finish(response)
        return value

    def sleep(seconds):
        seconds = float(seconds)
        response = w.step("sleep", ["sleep", seconds])
        w.clock += seconds
        w.finish(response)

    def simple(name, default):
        def call(*args):
            response = w.step(name, [name, *args])
            if response and "err" in response:
                w.finish(response)
            value = response["ok"] if response and "ok" in response else default()
            w.finish(response)
            return value
        return call

    def set_handler(number, handler):
        w.record(["set_handler", int(number), handler_name(handler)])
        w.handlers[int(number)] = handler
        w.saved += 1
        response = w.take("set_handler")
        if response and "advance" in response:
            w.clock += response["advance"]
        w.finish(response)
        return ("saved", w.saved)

    def waitpid(pid, options):
        if (pid, options) != (-1, os.WNOHANG):
            raise Abort("waitpid(-1, WNOHANG) だけ")
        response = w.step("waitpid_any", ["waitpid_any"])
        value = tuple(response["ok"]) if response and "ok" in response else (0, 0)
        w.finish(response)
        return value

    def proc_error(response, path):
        """開く段の誤りはファイル名付き(FileIO の open)、読む段("at": "read")は付かない(readall)。"""
        error = dict(response["err"])
        if error.pop("at", None) != "read":
            error["filename"] = path
        return {**response, "err": error}

    def read_text(self, *args, **kwargs):
        path = str(self)
        response = w.step("read_children", ["read_children", path])
        value = response["ok"] if response and "ok" in response else ""
        if response and "err" in response:
            response = proc_error(response, path)
        w.finish(response)
        return value

    def read_bytes(self):
        path = str(self)
        response = w.step("read_stat", ["read_stat", path])
        if response is None:
            response = err("ENOENT")
        value = to_bytes(response)
        if "err" in response:
            response = proc_error(response, path)
        w.finish(response)
        return value

    def path_open(self, mode="r", *args, **kwargs):
        if mode != "w+b" or args or kwargs:
            raise Abort("Path.open('w+b') だけ")
        path = str(self)
        response = w.step("open_output", ["open_output", path])
        if response and "err" in response:
            w.finish({**response, "err": {**response["err"], "filename": path}})
        number = response["ok"] if response and "ok" in response else w.fd()
        w.finish(response)
        w.output_fds.add(number)
        return FakeStream(w, number)

    def os_open(path, flags, mode=None):
        path = os.fspath(path)  # os.open の filename は fspath の値(Path でも str)
        response = w.step("open", ["open", path, flag_names(flags), mode])
        if response and "err" in response:
            w.finish({**response, "err": {**response["err"], "filename": path}})
        number = response["ok"] if response and "ok" in response else w.fd()
        w.finish(response)
        return number

    def fstat(fd):
        if fd in w.output_fds:
            response = w.step("fstat_output", ["fstat_output", fd])
            size = response["ok"] if response and "ok" in response else 0
            w.finish(response)
            return types.SimpleNamespace(st_size=size, st_mode=stat.S_IFREG | 0o600)
        response = w.step("fstat", ["fstat", fd])
        info = response["ok"] if response and "ok" in response else {"regular": True, "size": 0}
        w.finish(response)
        mode = (stat.S_IFREG if info["regular"] else stat.S_IFIFO) | 0o600
        return types.SimpleNamespace(st_size=info["size"], st_mode=mode)

    def os_read(fd, size):
        response = w.step("read", ["read", fd, size])
        value = to_bytes(response)
        w.finish(response)
        return value

    def os_write(fd, data):
        response = w.step("write", ["write", fd, bytes(data).hex()])
        value = response["ok"] if response and "ok" in response else len(data)
        w.finish(response)
        return value

    def token_hex(size):
        if size != 32:
            raise Abort("token_hex(32) だけ")
        response = w.step("token_hex", ["token_hex"])
        if response and "raise" in response:
            w.after(response)
            kind, message = response["raise"]
            raise RAISABLE[kind](message)
        value = response["ok"] if response and "ok" in response else TOKEN
        w.finish(response)
        return value

    def pipe():
        response = w.step("pipe", ["pipe"])
        value = tuple(response["ok"]) if response and "ok" in response else (20, 21)
        w.finish(response)
        return value

    def select_select(read, write, error, timeout):
        if write or error or len(read) != 1:
            raise Abort("select は読み口 1 つだけ")
        fd = read[0]
        w.record(["select", fd, float(timeout)])
        if fd >= 1024:
            raise ValueError("filedescriptor out of range in select()")
        response = w.take("select")
        if response and "advance" in response:
            w.clock += response["advance"]
        ready = response["ok"] if response and "ok" in response else False
        w.finish(response)
        return ([fd] if ready else [], [], [])

    def get_errno():
        response = w.step("get_errno", ["get_errno"])
        value = response["ok"] if response and "ok" in response else 0
        w.finish(response)
        return value

    def popen(argv, **options):
        return FakePopen(w, popen_kind, argv, **options)

    stack.enter_context(mock.patch.object(time, "monotonic", monotonic))
    stack.enter_context(mock.patch.object(time, "sleep", sleep))
    stack.enter_context(mock.patch.object(os, "getpid", simple("getpid", lambda: w.pid)))
    stack.enter_context(mock.patch.object(os, "getppid", simple("getppid", lambda: w.ppid)))
    stack.enter_context(mock.patch.object(os, "waitpid", waitpid))
    stack.enter_context(mock.patch.object(os, "close", simple("close", lambda: None)))
    stack.enter_context(mock.patch.object(os, "open", os_open))
    stack.enter_context(mock.patch.object(os, "fstat", fstat))
    stack.enter_context(mock.patch.object(os, "read", os_read))
    stack.enter_context(mock.patch.object(os, "write", os_write))
    stack.enter_context(mock.patch.object(os, "fsync", simple("fsync", lambda: None)))
    stack.enter_context(mock.patch.object(os, "pipe", pipe))
    stack.enter_context(mock.patch.object(os, "killpg", lambda pgid, number: simple("killpg", lambda: None)(pgid, int(number))))
    stack.enter_context(mock.patch.object(signal, "signal", set_handler))
    stack.enter_context(mock.patch.object(ctypes, "CDLL", lambda name, use_errno=False: FakeLibc(w)))
    stack.enter_context(mock.patch.object(ctypes, "get_errno", get_errno))
    stack.enter_context(mock.patch.object(subprocess, "Popen", popen))
    stack.enter_context(mock.patch.object(secrets, "token_hex", token_hex))
    stack.enter_context(mock.patch.object(select, "select", select_select))
    stack.enter_context(mock.patch.object(pathlib.Path, "read_text", read_text))
    stack.enter_context(mock.patch.object(pathlib.Path, "read_bytes", read_bytes))
    stack.enter_context(mock.patch.object(pathlib.Path, "open", path_open))
    stack.enter_context(mock.patch.object(sys, "platform", w.platform))
    # pidfd の有無は hasattr で見る。Mac の Python には無いので、無いときは消し、あるときは作る。
    def pidfd_open(pid):
        if not -2 ** 31 <= pid < 2 ** 31:
            REAL_KILL(pid, 0)
            raise Abort("pid_t の変換が通った")
        return simple("pidfd_open", w.fd)(pid)

    pidfd = (
        (os, "pidfd_open", pidfd_open),
        (signal, "pidfd_send_signal", lambda fd, number: simple("pidfd_send_signal", lambda: None)(fd, int(number))),
    )
    for module, name, fake in pidfd:
        missing = object()
        original = module.__dict__.get(name, missing)
        stack.callback(restore, module, name, original, missing)
        if w.has_pidfd:
            setattr(module, name, fake)
        elif original is not missing:
            delattr(module, name)


def restore(module, name, original, missing):
    if original is missing:
        if name in module.__dict__:
            delattr(module, name)
    else:
        setattr(module, name, original)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def number(value):
    if "int" in value:
        return int(value["int"])
    return float(value["float"])


def form(value):
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise Abort(f"結果に知らない値: {value!r}")


def run_main(kind, world, args):
    script = "loop-supervisor.py" if kind == "ls" else "check-process.py"
    module = load(f"r4q_injected_{kind}", LEGACY[script])
    stderr = io.StringIO()
    with contextlib.ExitStack() as stack:
        patches(world, stack, "spawn")
        stack.enter_context(mock.patch.object(sys, "argv", [script, *args]))
        stack.enter_context(contextlib.redirect_stderr(stderr))
        try:
            code = module.main()
        except Abort:
            raise
        except KeyboardInterrupt:  # 既定の SIGINT の処理(呼出履歴の途中の行は比べない)
            return {"keyboard_interrupt": True, "stderr": stderr.getvalue()}
        except Exception as exc:  # 旧版が捕まえない例外(呼出履歴と終了 1)
            legacy = {str(path) for path in LEGACY.values()}
            frames = [[os.path.basename(item.filename), item.lineno, item.name, item.line]
                      for item in traceback.extract_tb(exc.__traceback__) if item.filename in legacy]
            return {"uncaught": [type(exc).__name__, str(exc)], "frames": frames, "stderr": stderr.getvalue()}
    return {"exit": code, "stderr": stderr.getvalue()}


def run_rc(world, request):
    # run_command を呼ぶまでの Python のフレームの数(<module>・main・build・run_rc)。入れ子の上限を決める。
    if len(traceback.extract_stack()) != request["caller_frames"]:
        raise Abort(f"呼出元のフレームの数が {request['caller_frames']} でない")
    module = load("r4q_injected_cp", LEGACY["check-process.py"])
    env = None if request["env"] is None else dict(request["env"])
    with contextlib.ExitStack() as stack:
        patches(world, stack, "spawn_monitor")
        stop = module.StopSignals()
        with stop:
            try:
                result = module.run_command(
                    list(request["argv"]), request["cwd"], env, number(request["timeout"]),
                    Path(request["directory"]), stop, grace=number(request["grace"]),
                    method=request["method"], keep=request["keep"])
                outcome = {"result": [[key, form(value)] for key, value in result.items()]}
            except Abort:
                raise
            except Exception as exc:
                outcome = {"error": [type(exc).__name__, str(exc)]}
    return outcome


CASES = []


def case(case_id, kind, given, world=None):
    if any(item["id"] == case_id for item in CASES):
        raise Abort(f"ID が重なる: {case_id}")
    CASES.append({"id": case_id, "kind": kind, "input": given, "world": world or {}})


def stat_line(pid, ppid, starttime, name="cmd"):
    """/proc/<pid>/stat の 1 行(fields[1] が ppid、fields[19] が starttime)。"""
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(starttime)] + ["0"] * 5
    return f"{pid} ({name}) {' '.join(fields)}\n".encode().hex()


def status(code=None, signal_number=None):
    """waitpid の状態(終了は code << 8、信号は番号)。"""
    return signal_number if signal_number is not None else code << 8


LS_ARGS = ["--result", "/inj/rec.json", "--nonce", "n", "--timeout", "5", "--grace", "0.2", "--", "cmd", "a"]


def ls_args(timeout="5", grace="0.2"):
    return ["--result", "/inj/rec.json", "--nonce", "n", "--timeout", timeout, "--grace", grace, "--", "cmd", "a"]


def cp_args(timeout="5", grace="0.2"):
    return ["--linux", "--result", "/inj/control/recovery.json", "--nonce", "n", "--timeout", timeout,
            "--grace", grace, "--", "cmd", "a"]


ECHILD = err("ECHILD")
RUNNING = ok([0, 0])
OWN = {"read_children": ok("200\n"), "read_stat": ok(stat_line(200, 100, 777))}
# 最初の reap の後に時間切れにする(次の待ちで signal_owned に入る)。
TIMED = [ok(1000.0), ok(1010.0)]


def owned(**responses):
    """時間切れの後の最初の signal_owned で応答を使うケース。"""
    return {"responses": {"monotonic": list(TIMED), **responses}}


def supervise_cases():
    exited = {"waitpid_any": [ok([200, status(3)]), ECHILD, ECHILD]}
    case("inj-ls-exit3", "ls", {"args": LS_ARGS}, {"responses": exited})
    case("inj-ls-setup-not-linux", "ls", {"args": LS_ARGS}, {"platform": "darwin"})
    case("inj-ls-setup-no-pidfd", "ls", {"args": LS_ARGS}, {"has_pidfd": False})
    case("inj-ls-prctl-subreaper-eperm", "ls", {"args": LS_ARGS},
         {"responses": {"prctl": [ok(-1)], "get_errno": [ok(1)]}})
    case("inj-ls-prctl-pdeathsig-einval", "ls", {"args": LS_ARGS},
         {"responses": {"prctl": [ok(0), ok(-1)], "get_errno": [ok(22)]}})
    case("inj-ls-prctl-errno-zero", "ls", {"args": LS_ARGS},
         {"responses": {"prctl": [ok(1)], "get_errno": [ok(0)]}})
    case("inj-ls-parent-is-init", "ls", {"args": LS_ARGS}, {"ppid": 1})
    case("inj-ls-parent-changed", "ls", {"args": LS_ARGS}, {"responses": {"getppid": [ok(50), ok(1)]}})
    case("inj-ls-signal-in-setup", "ls", {"args": LS_ARGS},
         {"responses": {"set_handler": [ok(), ok(), ok(signal=2)]}})
    case("inj-ls-signal-after-start", "ls", {"args": LS_ARGS},
         {"responses": {"waitpid_any": [ok([0, 0], signal=15), ok([200, status(signal_number=15)]), ECHILD]}})
    case("inj-ls-two-signals-latest", "ls", {"args": LS_ARGS},
         {"responses": {"waitpid_any": [ok([0, 0], signal=1), ok([0, 0], signal=2),
                                        ok([200, status(0)]), ECHILD]}, "defaults": OWN})
    case("inj-ls-spawn-enoent", "ls", {"args": LS_ARGS},
         {"responses": {"spawn": [err("ENOENT", filename="cmd")]}})
    case("inj-ls-spawn-eacces", "ls", {"args": LS_ARGS},
         {"responses": {"spawn": [err("EACCES", filename="cmd")]}})
    # 時間切れの後に TERM を grace の間だけ送り直し、その後は KILL(回数は時計で決まる)。
    case("inj-ls-timeout-term-resend", "ls", {"args": ls_args("0.1", "0.1")},
         {"responses": {"waitpid_any": [RUNNING] * 14 + [ok([200, status(signal_number=9)]), ECHILD]},
          "defaults": OWN})
    case("inj-ls-grace-zero-kill-first", "ls", {"args": ls_args("0.04", "0")},
         {"responses": {"waitpid_any": [RUNNING] * 5 + [ok([200, status(signal_number=9)]), ECHILD]},
          "defaults": OWN})
    # 期限と同じ時刻: monotonic() - started >= timeout と monotonic() < term_end の境目。
    case("inj-ls-deadline-equal", "ls", {"args": ls_args("5", "1")},
         {"responses": {"monotonic": [ok(1000.0), ok(1005.0), ok(1005.0), ok(1006.0), ok(1006.0)],
                        "waitpid_any": [RUNNING, RUNNING, ok([200, status(signal_number=9)]), ECHILD]},
          "defaults": OWN})
    case("inj-ls-hard-end", "ls", {"args": ls_args("0.02", "0")},
         {"responses": {"sleep": [ok(), ok(advance=4.9), ok(advance=4.9)]}})
    case("inj-ls-hard-end-equal", "ls", {"args": ls_args("1", "1")},
         {"responses": {"monotonic": [ok(1000.0), ok(1001.0), ok(1001.0), ok(1001.5), ok(1007.0)]},
          "defaults": OWN})
    case("inj-ls-owner-changed", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200 201\n")], read_stat=[ok(stat_line(200, 99, 777))]))
    case("inj-ls-starttime-changed", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")],
               read_stat=[ok(stat_line(200, 100, 777)), ok(stat_line(200, 100, 778))]))
    case("inj-ls-starttime-changed-close-ebadf", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")],
               read_stat=[ok(stat_line(200, 100, 777)), ok(stat_line(200, 100, 778))], close=[err("EBADF")]))
    case("inj-ls-pidfd-open-esrch-skips", "ls", {"args": LS_ARGS},
         owned(waitpid_any=[RUNNING, RUNNING, ok([200, status(1)]), ECHILD], read_children=[ok("200 201\n")],
               read_stat=[ok(stat_line(200, 100, 777)), ok(stat_line(201, 100, 9)), ok(stat_line(201, 100, 9))],
               pidfd_open=[err("ESRCH"), ok(31)]))
    case("inj-ls-pidfd-open-eperm", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")], read_stat=[ok(stat_line(200, 100, 777))], pidfd_open=[err("EPERM")]))
    case("inj-ls-pidfd-open-emfile", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")], read_stat=[ok(stat_line(200, 100, 777))], pidfd_open=[err("EMFILE")]))
    case("inj-ls-send-esrch-skips", "ls", {"args": LS_ARGS},
         owned(waitpid_any=[RUNNING, RUNNING, ok([200, status(0)]), ECHILD], read_children=[ok("200\n")],
               read_stat=[ok(stat_line(200, 100, 777)), ok(stat_line(200, 100, 777))],
               pidfd_send_signal=[err("ESRCH")]))
    case("inj-ls-send-eperm", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")],
               read_stat=[ok(stat_line(200, 100, 777)), ok(stat_line(200, 100, 777))],
               pidfd_send_signal=[err("EPERM")]))
    case("inj-ls-children-eacces", "ls", {"args": LS_ARGS}, owned(read_children=[err("EACCES")]))
    case("inj-ls-children-enoent", "ls", {"args": LS_ARGS}, owned(read_children=[err("ENOENT")]))
    case("inj-ls-children-not-int", "ls", {"args": LS_ARGS}, owned(read_children=[ok("200 x1\n")]))
    case("inj-ls-children-pid-above-int", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("2147483648\n")], read_stat=[ok(stat_line(2147483648, 100, 1))]))
    case("inj-ls-children-pid-below-int", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("-2147483649\n")], read_stat=[ok(stat_line(-2147483649, 100, 1))]))
    case("inj-ls-children-pid-above-long", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("1" + "0" * 20 + "\n")], read_stat=[ok(stat_line(10 ** 20, 100, 1))]))
    case("inj-cp-linux-children-pid-above-int", "cp", {"args": cp_args()},
         owned(read_children=[ok("2147483648\n")], read_stat=[ok(stat_line(2147483648, 100, 1))]))
    case("inj-ls-children-unicode-space", "ls", {"args": LS_ARGS},
         owned(waitpid_any=[RUNNING, RUNNING, ok([200, status(0)]), ECHILD], read_children=[ok("　200\x1c")],
               read_stat=[ok(stat_line(200, 100, 777)), ok(stat_line(200, 100, 777))]))
    case("inj-ls-stat-enoent-skips", "ls", {"args": LS_ARGS},
         owned(waitpid_any=[RUNNING, RUNNING, ok([200, status(0)]), ECHILD], read_children=[ok("200\n")],
               read_stat=[err("ENOENT")]))
    case("inj-ls-stat-after-enoent-skips", "ls", {"args": LS_ARGS},
         owned(waitpid_any=[RUNNING, RUNNING, ok([200, status(0)]), ECHILD], read_children=[ok("200\n")],
               read_stat=[ok(stat_line(200, 100, 777)), err("ENOENT")]))
    case("inj-ls-stat-eacces", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")], read_stat=[err("EACCES")]))
    case("inj-ls-stat-no-paren", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")], read_stat=[ok(b"200 cmd S 100 1 1\n".hex())]))
    case("inj-ls-stat-short", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")], read_stat=[ok(b"200 (cmd) S 100 1 1\n".hex())]))
    case("inj-ls-stat-short-after", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")],
               read_stat=[ok(stat_line(200, 100, 777)), ok(b"200 (cmd) S 100\n".hex())]))
    case("inj-ls-stat-bad-int", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")], read_stat=[ok(stat_line(200, "1x", 777))]))
    case("inj-ls-stat-paren-in-name", "ls", {"args": LS_ARGS},
         owned(waitpid_any=[RUNNING, RUNNING, ok([200, status(0)]), ECHILD], read_children=[ok("200\n")],
               read_stat=[ok(stat_line(200, 100, 777, name="a) (b")), ok(stat_line(200, 100, 777, name="a) (b"))]))
    # 読む段(開いた後の readall)の誤りはファイル名が付かない。ESRCH は ProcessLookupError で飛ばす。
    case("inj-ls-stat-read-esrch-skips", "ls", {"args": LS_ARGS},
         owned(waitpid_any=[RUNNING, RUNNING, ok([200, status(0)]), ECHILD], read_children=[ok("200\n")],
               read_stat=[{"err": {"errno": ERRNO["ESRCH"], "text": TEXT["ESRCH"], "at": "read"}}]))
    case("inj-ls-stat-read-eio", "ls", {"args": LS_ARGS},
         owned(read_children=[ok("200\n")],
               read_stat=[{"err": {"errno": ERRNO["EIO"], "text": TEXT["EIO"], "at": "read"}}]))
    case("inj-ls-children-read-eio", "ls", {"args": LS_ARGS},
         owned(read_children=[{"err": {"errno": ERRNO["EIO"], "text": TEXT["EIO"], "at": "read"}}]))
    # KeyboardInterrupt(既定の SIGINT の処理)は、setup が SIGINT を置き換える前の、呼出しが返った所で起きる。
    case("inj-ls-ki-after-open", "ls", {"args": LS_ARGS}, {"responses": {"open": [ok(None, signal=2)]}})
    case("inj-ls-ki-after-getppid", "ls", {"args": LS_ARGS}, {"responses": {"getppid": [ok(50, signal=2)]}})
    case("inj-ls-ki-after-prctl", "ls", {"args": LS_ARGS}, {"responses": {"prctl": [ok(0, signal=2)]}})
    case("inj-ls-ki-after-prctl-failure", "ls", {"args": LS_ARGS}, {"responses": {"prctl": [ok(-1, signal=2)]}})
    case("inj-ls-ki-after-get-errno", "ls", {"args": LS_ARGS},
         {"responses": {"prctl": [ok(-1)], "get_errno": [ok(1, signal=2)]}})
    case("inj-ls-ki-before-sigint-handler", "ls", {"args": LS_ARGS},
         {"responses": {"set_handler": [ok(signal=2)]}})
    case("inj-ls-sigint-after-handler", "ls", {"args": LS_ARGS},
         {"responses": {"set_handler": [ok(), ok(signal=2)]}})
    case("inj-ls-ki-close-eio", "ls", {"args": LS_ARGS},
         {"responses": {"getppid": [ok(50, signal=2)], "close": [err("EIO")]}})
    case("inj-ls-check-ki", "ls", {"args": ["--check"]}, {"responses": {"getppid": [ok(50, signal=2)]}})
    case("inj-ls-waitpid-einval", "ls", {"args": LS_ARGS}, {"responses": {"waitpid_any": [err("EINVAL")]}})
    case("inj-ls-waitpid-stopped", "ls", {"args": LS_ARGS}, {"responses": {"waitpid_any": [ok([200, 0x137f])]}})
    case("inj-ls-sigchld-keyerror", "ls", {"args": ls_args("0.05", "0.2")}, {"defaults": {"waitpid_any": ECHILD}})
    case("inj-ls-grandchild-reaped", "ls", {"args": LS_ARGS},
         {"responses": {"waitpid_any": [ok([200, status(0)]), RUNNING, RUNNING, ok([201, status(5)]), ECHILD]},
          "defaults": {"read_children": ok("201\n"), "read_stat": ok(stat_line(201, 100, 5))}})
    case("inj-ls-record-open-eexist", "ls", {"args": LS_ARGS}, {"responses": {"open": [err("EEXIST")]}})
    case("inj-ls-record-write-enospc", "ls", {"args": LS_ARGS},
         {"responses": {**exited, "write": [err("ENOSPC")]}})
    case("inj-ls-record-write-eio", "ls", {"args": LS_ARGS}, {"responses": {**exited, "write": [err("EIO")]}})
    case("inj-ls-record-write-short", "ls", {"args": LS_ARGS}, {"responses": {**exited, "write": [ok(10)]}})
    case("inj-ls-record-fsync-eio", "ls", {"args": LS_ARGS}, {"responses": {**exited, "fsync": [err("EIO")]}})
    case("inj-ls-record-fsync-enospc", "ls", {"args": LS_ARGS},
         {"responses": {**exited, "fsync": [err("ENOSPC")]}})
    case("inj-ls-record-close-eio", "ls", {"args": LS_ARGS}, {"responses": {**exited, "close": [err("EIO")]}})
    case("inj-ls-record-close-after-error", "ls", {"args": LS_ARGS},
         {"responses": {"prctl": [ok(-1)], "get_errno": [ok(1)], "close": [err("EBADF")]}})
    case("inj-ls-check-ok", "ls", {"args": ["--check"]})
    case("inj-ls-check-pidfd-eperm", "ls", {"args": ["--check"]}, {"responses": {"pidfd_open": [err("EPERM")]}})
    case("inj-ls-check-send-einval", "ls", {"args": ["--check"]},
         {"responses": {"pidfd_send_signal": [err("EINVAL")]}})
    case("inj-ls-check-close-ebadf", "ls", {"args": ["--check"]}, {"responses": {"close": [err("EBADF")]}})
    case("inj-ls-check-not-linux", "ls", {"args": ["--check"]}, {"platform": "darwin"})
    # check-process の linux worker(記録は鍵の順が入れた順。起動の失敗は 127)。
    case("inj-cp-linux-exit3", "cp", {"args": cp_args()}, {"responses": exited})
    case("inj-cp-linux-launch-failure", "cp", {"args": cp_args()},
         {"responses": {"spawn": [err("ENOENT", filename="cmd")], "waitpid_any": [ECHILD]}})
    case("inj-cp-linux-launch-failure-signal", "cp", {"args": cp_args()},
         {"responses": {"spawn": [err("ENOENT", filename="cmd", signal=15)], "waitpid_any": [ECHILD]}})
    case("inj-cp-linux-launch-failure-unreaped", "cp", {"args": cp_args()},
         {"responses": {"spawn": [err("ENOEXEC", filename="cmd")], "waitpid_any": [RUNNING]}})
    case("inj-cp-linux-launch-failure-reaps-first", "cp", {"args": cp_args()},
         {"responses": {"spawn": [err("E2BIG", filename="cmd")], "waitpid_any": [ok([201, status(0)]), ECHILD]}})
    case("inj-cp-linux-record-enospc", "cp", {"args": cp_args()},
         {"responses": {**exited, "write": [err("ENOSPC")]}})
    case("inj-cp-linux-record-short", "cp", {"args": cp_args()}, {"responses": {**exited, "write": [ok(0)]}})
    case("inj-cp-linux-record-fsync-eio", "cp", {"args": cp_args()}, {"responses": {**exited, "fsync": [err("EIO")]}})
    case("inj-cp-linux-sigchld-keyerror", "cp", {"args": cp_args("0.05")}, {"defaults": {"waitpid_any": ECHILD}})
    case("inj-cp-linux-prctl-eperm", "cp", {"args": cp_args()},
         {"responses": {"prctl": [ok(-1)], "get_errno": [ok(1)]}})
    case("inj-cp-linux-stat-short", "cp", {"args": cp_args()},
         owned(read_children=[ok("200\n")], read_stat=[ok(b"200 (cmd) S 100\n".hex())]))
    case("inj-cp-linux-ki-after-open", "cp", {"args": cp_args()}, {"responses": {"open": [ok(None, signal=2)]}})
    case("inj-cp-linux-ki-before-sigint-handler", "cp", {"args": cp_args()},
         {"responses": {"set_handler": [ok(signal=2)]}})
    case("inj-cp-linux-timeout-kill", "cp", {"args": cp_args("0.04", "0.04")},
         {"responses": {"waitpid_any": [RUNNING] * 6 + [ok([200, status(signal_number=9)]), ECHILD]},
          "defaults": OWN})


# 固定期待を作る親のフレームの数(<module>・main・build・run_rc。run_rc の中で確かめる)。
CALLER_FRAMES = 4
# 記録と通知の json.loads が許す入れ子(sys.getrecursionlimit() 1000 から、呼出元と run_command の中の
# フレームと C の scanner の 1 を引いた値。Rust の read_result_room・notice_room と同じ式)。
RECORD_ROOM = 1000 - (CALLER_FRAMES + 6 + 1)
NOTICE_ROOM = 1000 - (CALLER_FRAMES + 5 + 1)


def rc(method="linux", argv=("cmd", "a"), timeout=None, grace=None, keep=0, env=None, cwd=None):
    return {"argv": list(argv), "cwd": cwd, "env": env, "timeout": timeout or {"int": "5"},
            "grace": grace or {"float": "0.2"}, "directory": "/inj/control", "method": method, "keep": keep,
            "int_max_str_digits": 4300, "caller_frames": CALLER_FRAMES}


def record(returncode=3, signal_number=0, nonce=TOKEN, **extra):
    value = {"clean": True, "returncode": returncode, "timed_out": False, "signal": signal_number, "nonce": nonce}
    value.update(extra)
    return (json.dumps(value) + "\n").encode()


def shaped(base, drop=(), **changes):
    """記録・通知の辞書の欄を抜くか替えた JSON の行。"""
    value = {key: item for key, item in base.items() if key not in drop}
    value.update(changes)
    return (json.dumps(value) + "\n").encode()


RECORD = {"clean": True, "returncode": 3, "timed_out": False, "signal": 0, "nonce": TOKEN}
NOTICE = {"nonce": TOKEN, "returncode": 3}


def notice(returncode=3, nonce=TOKEN):
    return (json.dumps({"nonce": nonce, "returncode": returncode}) + "\n").encode()


def measured(out=b"out", error=b"err"):
    """出力の 2 つのファイルを計る応答(長さと中身)。"""
    sizes = [ok(len(out)), ok(len(error))]
    reads = [ok(part.hex()) for part in (out, error) if part]
    return {"fstat_output": sizes, "read_output": reads}


def linux_world(raw=None, poll=None, measure=False, **more):
    raw = record() if raw is None else raw
    responses = {"poll_monitor": poll or [ok(None), ok(0)], "fstat": [ok({"regular": True, "size": len(raw)})],
                 "read": [ok(raw.hex())]}
    if measure:
        responses.update(measured())
    responses.update(more.pop("responses", {}))
    return {"responses": responses, **more}


def posix_world(raw=None, measure=False, **more):
    raw = notice() if raw is None else raw
    responses = {"select": [ok(False), ok(True), ok(False)], "read": [ok(raw.hex())],
                 "killpg": [ok(), ok(), err("ESRCH")], "wait_monitor": [ok(-9)]}
    if measure:
        responses.update(measured())
    responses.update(more.pop("responses", {}))
    return {"responses": responses, **more}


def parent_cases():
    case("inj-rc-linux-exit3", "rc", rc(), linux_world(measure=True))
    case("inj-rc-linux-keep", "rc", rc(keep=2, env=[["Z", "1"], ["A", "2"], ["Z", "3"]], cwd="/inj/work"),
         linux_world(measure=True))
    case("inj-rc-method-default-linux", "rc", rc(method=None), linux_world(measure=True))
    case("inj-rc-method-default-darwin", "rc", rc(method=None), posix_world(measure=True, platform="darwin"))
    case("inj-rc-method-linux-without-pidfd", "rc", rc(), {"has_pidfd": False})
    case("inj-rc-method-default-linux-without-pidfd", "rc", rc(method=None), {"has_pidfd": False})
    case("inj-rc-method-posix-on-linux", "rc", rc(method="posix"), posix_world(measure=True))
    case("inj-rc-stop-before", "rc", rc(), {"responses": {"set_handler": [ok(), ok(signal=2)]}})
    case("inj-rc-timeout-invalid", "rc", rc(timeout={"float": "nan"}))
    case("inj-rc-grace-negative", "rc", rc(grace={"int": "-1"}))
    case("inj-rc-timeout-overflow", "rc", rc(timeout={"int": "1" + "0" * 400}))
    case("inj-rc-output-open-eacces", "rc", rc(), {"responses": {"open_output": [err("EACCES")]}})
    case("inj-rc-output-open-stderr-eacces", "rc", rc(), {"responses": {"open_output": [ok(41), err("EACCES")]}})
    case("inj-rc-output-short", "rc", rc(),
         linux_world(responses={"fstat_output": [ok(10)], "read_output": [ok(b"0123".hex()), ok("")]}))
    case("inj-rc-output-cap-short", "rc", rc(keep=3),
         linux_world(responses={"fstat_output": [ok(1024 * 1024 + 5)], "read_output": [ok(b"abc".hex()), ok("")]}))
    case("inj-rc-output-read-eio", "rc", rc(),
         linux_world(responses={"fstat_output": [ok(3)], "read_output": [err("EIO")]}))
    case("inj-rc-output-close-eio", "rc", rc(),
         linux_world(measure=True, responses={"close_output": [ok(), err("EIO")]}))
    case("inj-rc-output-stdout-empty", "rc", rc(keep=8), linux_world(responses=measured(b"", b"abcdefghij")))
    # Linux の方式(pidfd で送る。pidfd が無いときに POSIX へ切り替えない)。
    case("inj-rc-linux-launch-failure", "rc", rc(),
         {"responses": {"spawn_monitor": [err("ENOENT", filename="python3")]}})
    case("inj-rc-linux-pidfd-open-esrch", "rc", rc(),
         {"responses": {"pidfd_open": [err("ESRCH")], "wait_monitor": [ok(0)]}})
    case("inj-rc-linux-pidfd-open-wait-timeout", "rc", rc(),
         {"responses": {"pidfd_open": [err("EMFILE")], "wait_monitor": [{"timeout": True}]}})
    case("inj-rc-linux-pidfd-open-wait-timeout-int", "rc", rc(timeout={"int": "3"}, grace={"int": "1"}),
         {"responses": {"pidfd_open": [err("EMFILE")], "wait_monitor": [{"timeout": True}]}})
    case("inj-rc-linux-deadline-kill", "rc", rc(timeout={"float": "0.04"}, grace={"int": "0"}),
         {"responses": {"monotonic": [ok(1000.0), ok(1000.0), ok(1007.0), ok(1007.04)]},
          "defaults": {"poll_monitor": ok(None)}})
    case("inj-rc-linux-deadline-kill-wait-timeout", "rc", rc(),
         {"responses": {"monotonic": [ok(1000.0), ok(1012.2)], "wait_monitor": [{"timeout": True}]},
          "defaults": {"poll_monitor": ok(None)}})
    case("inj-rc-linux-deadline-kill-send-esrch", "rc", rc(),
         {"responses": {"monotonic": [ok(1000.0), ok(1013.0)], "pidfd_send_signal": [err("ESRCH")]},
          "defaults": {"poll_monitor": ok(None)}})
    case("inj-rc-linux-stop-forwarded", "rc", rc(),
         linux_world(record(signal_number=15), poll=[ok(None, signal=15), ok(None), ok(0)]))
    case("inj-rc-linux-stop-shortens-deadline", "rc", rc(timeout={"int": "100"}),
         {"responses": {"poll_monitor": [ok(None, signal=1)], "monotonic": [ok(1000.0), ok(1000.0), ok(1007.2)]},
          "defaults": {"poll_monitor": ok(None)}})
    case("inj-rc-linux-stop-sent-once", "rc", rc(),
         linux_world(record(signal_number=15), poll=[ok(None, signal=15), ok(None, signal=2), ok(None), ok(0)]))
    case("inj-rc-linux-stop-forward-eperm", "rc", rc(),
         {"responses": {"poll_monitor": [ok(None, signal=15)], "pidfd_send_signal": [err("EPERM")]}})
    case("inj-rc-linux-stop-after-exit", "rc", rc(), linux_world(poll=[ok(0, signal=2)]))
    case("inj-rc-linux-record-signal", "rc", rc(), linux_world(record(signal_number=1), poll=[ok(0)]))
    case("inj-rc-linux-monitor-97", "rc", rc(), {"responses": {"poll_monitor": [ok(97)]}})
    case("inj-rc-linux-monitor-killed", "rc", rc(), {"responses": {"poll_monitor": [ok(None), ok(-9)]}})
    case("inj-rc-linux-record-open-enoent", "rc", rc(),
         {"responses": {"poll_monitor": [ok(0)], "open": [err("ENOENT")]}})
    case("inj-rc-linux-record-not-regular", "rc", rc(),
         {"responses": {"poll_monitor": [ok(0)], "fstat": [ok({"regular": False, "size": 10})]}})
    case("inj-rc-linux-record-large-size", "rc", rc(),
         {"responses": {"poll_monitor": [ok(0)], "fstat": [ok({"regular": True, "size": 4097})]}})
    case("inj-rc-linux-record-grew", "rc", rc(),
         {"responses": {"poll_monitor": [ok(0)], "fstat": [ok({"regular": True, "size": 4096})],
                        "read": [ok((b" " * 4097).hex())]}})
    case("inj-rc-linux-record-read-eio", "rc", rc(),
         {"responses": {"poll_monitor": [ok(0)], "fstat": [ok({"regular": True, "size": 10})], "read": [err("EIO")]}})
    case("inj-rc-linux-record-bad-json", "rc", rc(), linux_world(b"{", poll=[ok(0)]))
    case("inj-rc-linux-record-bad-utf8", "rc", rc(), linux_world(b'{"a": "\xff"}', poll=[ok(0)]))
    case("inj-rc-linux-record-nonce-mismatch", "rc", rc(), linux_world(record(nonce="other"), poll=[ok(0)]))
    case("inj-rc-linux-record-returncode-bool", "rc", rc(), linux_world(record(returncode=True), poll=[ok(0)]))
    case("inj-rc-linux-record-signal-9", "rc", rc(), linux_world(record(signal_number=9), poll=[ok(0)]))
    case("inj-rc-linux-record-list", "rc", rc(), linux_world(b"[1]\n", poll=[ok(0)]))
    # json.loads の入れ子の上限(呼出元のフレームの数で決まる。上限ちょうどは通り、1 つ深いと RecursionError)。
    for name, depth in (("nested-at-room", RECORD_ROOM), ("nested-over-room", RECORD_ROOM + 1)):
        case(f"inj-rc-linux-record-{name}", "rc", rc(), linux_world(b"[" * depth + b"]" * depth, poll=[ok(0)]))
    for name, depth in (("nested-at-room", NOTICE_ROOM), ("nested-over-room", NOTICE_ROOM + 1)):
        case(f"inj-rc-posix-notice-{name}", "rc", rc("posix"),
             posix_world(b"[" * depth + b"]" * depth + b"\n", responses={"select": [ok(True)]}))
    # secrets.token_hex(os.urandom)の誤り: OSError は包み、NotImplementedError・RuntimeError は包まない。
    case("inj-rc-linux-token-eio", "rc", rc(), {"responses": {"token_hex": [err("EIO")]}})
    case("inj-rc-posix-token-eio", "rc", rc("posix"), {"responses": {"token_hex": [err("EIO")]}})
    case("inj-rc-linux-token-not-found", "rc", rc(),
         {"responses": {"token_hex": [{"raise": ["NotImplementedError", "/dev/urandom (or equivalent) not found"]}]}})
    case("inj-rc-posix-token-short", "rc", rc("posix"),
         {"responses": {"token_hex": [{"raise": ["RuntimeError", "Failed to read 32 bytes from /dev/urandom"]}]}})
    # 記録の照合の条件を 1 つずつ外す(旧版 check-process.py:77-79)。
    for name, raw in (
        ("timed-out-missing", shaped(RECORD, drop=("timed_out",))),
        ("timed-out-zero", shaped(RECORD, timed_out=0)),
        ("timed-out-null", shaped(RECORD, timed_out=None)),
        ("clean-one", shaped(RECORD, clean=1)),
        ("clean-missing", shaped(RECORD, drop=("clean",))),
        ("clean-false", shaped(RECORD, clean=False)),
        ("signal-true", shaped(RECORD, signal=True)),
        ("signal-missing", shaped(RECORD, drop=("signal",))),
        ("signal-float", shaped(RECORD, signal=15.0)),
        ("signal-3", shaped(RECORD, signal=3)),
        ("returncode-float", shaped(RECORD, returncode=3.0)),
        ("returncode-missing", shaped(RECORD, drop=("returncode",))),
        ("returncode-str", shaped(RECORD, returncode="3")),
        ("nonce-missing", shaped(RECORD, drop=("nonce",))),
        ("nonce-int", shaped(RECORD, nonce=5)),
    ):
        case(f"inj-rc-linux-record-{name}", "rc", rc(), linux_world(raw, poll=[ok(0)]))
    # 照合を通る形(-0 は 0、重なる鍵は後の値、timed_out は真でもよい)。
    for name, raw in (
        ("signal-minus-zero", b'{"clean": true, "returncode": 3, "timed_out": false, "signal": -0, "nonce": "%s"}\n'
         % TOKEN.encode()),
        ("duplicate-key", b'{"returncode": "x", "clean": true, "returncode": 4, "timed_out": true, "signal": 0, '
         b'"nonce": "%s"}\n' % TOKEN.encode()),
    ):
        case(f"inj-rc-linux-record-{name}", "rc", rc(), linux_world(raw, poll=[ok(0)], measure=True))
    case("inj-rc-linux-record-close-eio", "rc", rc(), linux_world(poll=[ok(0)], responses={"close": [err("EIO")]}))
    case("inj-rc-linux-pidfd-close-ebadf", "rc", rc(),
         linux_world(poll=[ok(0)], responses={"close": [ok(), err("EBADF")]}))
    case("inj-rc-linux-record-extra-keys", "rc", rc(keep=1),
         linux_world(record(extra="x", recovery_method="m"), poll=[ok(0)], measure=True))
    # POSIX の方式(最後の KILL まで anchor を回収しない。EPERM を不在として扱わない。読み口は最後に閉じる)。
    case("inj-rc-posix-exit3", "rc", rc("posix"), posix_world(measure=True))
    case("inj-rc-posix-launch-failure", "rc", rc("posix"),
         {"responses": {"spawn_monitor": [err("ENOENT", filename="python3")]}})
    case("inj-rc-posix-launch-failure-close-ebadf", "rc", rc("posix"),
         {"responses": {"spawn_monitor": [err("ENOENT", filename="python3")], "close": [err("EBADF")]}})
    case("inj-rc-posix-pipe-emfile", "rc", rc("posix"), {"responses": {"pipe": [err("EMFILE")]}})
    case("inj-rc-posix-close-write-ebadf", "rc", rc("posix"), {"responses": {"close": [err("EBADF")]}})
    case("inj-rc-posix-term-eperm", "rc", rc("posix"),
         posix_world(responses={"killpg": [err("EPERM")], "wait_monitor": []}))
    case("inj-rc-posix-kill-eperm", "rc", rc("posix"),
         posix_world(responses={"killpg": [ok(), err("EPERM")], "wait_monitor": []}))
    case("inj-rc-posix-term-esrch", "rc", rc("posix"),
         posix_world(measure=True, responses={"killpg": [err("ESRCH"), err("ESRCH"), err("ESRCH")]}))
    case("inj-rc-posix-eperm-continues", "rc", rc("posix"),
         posix_world(responses={"killpg": [ok(), ok()], "sleep": [ok(), ok(advance=4.9)]},
                     defaults={"killpg": err("EPERM")}))
    case("inj-rc-posix-eperm-then-esrch", "rc", rc("posix"),
         posix_world(measure=True, platform="darwin",
                     responses={"killpg": [ok(), ok(), err("EPERM"), err("EPERM"), err("ESRCH")]}))
    case("inj-rc-posix-group-alive-until-deadline", "rc", rc("posix"),
         posix_world(responses={"killpg": [ok(), ok()], "sleep": [ok(), ok(advance=4.9), ok(advance=0.1)]}))
    case("inj-rc-posix-group-exists-einval", "rc", rc("posix"),
         posix_world(responses={"killpg": [ok(), ok(), err("EINVAL")]}))
    case("inj-rc-posix-wait-timeout", "rc", rc("posix"),
         posix_world(responses={"wait_monitor": [{"timeout": True}], "killpg": [ok(), ok()]}))
    case("inj-rc-posix-select-fd-1024", "rc", rc("posix"),
         posix_world(responses={"pipe": [ok([1500, 1501])], "select": [], "read": []}))
    case("inj-rc-posix-select-ebadf", "rc", rc("posix"),
         posix_world(responses={"select": [err("EBADF")], "read": []}))
    case("inj-rc-posix-anchor-lost-before-notice", "rc", rc("posix"),
         posix_world(responses={"select": [ok(True)], "read": [ok("")]}))
    case("inj-rc-posix-read-eio", "rc", rc("posix"),
         posix_world(responses={"select": [ok(True)], "read": [err("EIO")]}))
    case("inj-rc-posix-notice-in-parts", "rc", rc("posix"),
         posix_world(measure=True, responses={"select": [ok(True), ok(True), ok(False)],
                                              "read": [ok(notice()[:5].hex()), ok(notice()[5:].hex())]}))
    case("inj-rc-posix-notice-too-big", "rc", rc("posix"),
         posix_world(responses={"select": [ok(True), ok(True)],
                                "read": [ok((b"x" * 4000).hex()), ok((b"y" * 97).hex())]}))
    case("inj-rc-posix-notice-invalid", "rc", rc("posix"),
         posix_world(notice(nonce="other"), responses={"select": [ok(True)]}))
    case("inj-rc-posix-notice-returncode-str", "rc", rc("posix"),
         posix_world(b'{"nonce": "%s", "returncode": "3"}\n' % TOKEN.encode(), responses={"select": [ok(True)]}))
    case("inj-rc-posix-notice-bad-json", "rc", rc("posix"), posix_world(b"\n", responses={"select": [ok(True)]}))
    # 通知の照合の条件を 1 つずつ外す(旧版 check-process.py:186-188)。
    for name, raw in (
        ("list", b"[]\n"),
        ("nonce-missing", shaped(NOTICE, drop=("nonce",))),
        ("nonce-int", shaped(NOTICE, nonce=5)),
        ("returncode-true", shaped(NOTICE, returncode=True)),
        ("returncode-missing", shaped(NOTICE, drop=("returncode",))),
        ("returncode-float", shaped(NOTICE, returncode=3.0)),
        ("returncode-null", shaped(NOTICE, returncode=None)),
    ):
        case(f"inj-rc-posix-notice-{name}", "rc", rc("posix"), posix_world(raw, responses={"select": [ok(True)]}))
    case("inj-rc-posix-notice-extra-keys", "rc", rc("posix"),
         posix_world(shaped(NOTICE, extra=1, returncode=0), measure=True))
    case("inj-rc-posix-notice-127", "rc", rc("posix", keep=4), posix_world(notice(127), measure=True))
    case("inj-rc-posix-timeout", "rc", rc("posix", timeout={"float": "0.05"}),
         posix_world(measure=True, responses={"select": [ok(False, advance=0.02), ok(False, advance=0.02),
                                                         ok(False, advance=0.02), ok(False)], "read": []}))
    case("inj-rc-posix-deadline-equal", "rc", rc("posix", timeout={"int": "1"}),
         posix_world(measure=True, responses={"monotonic": [ok(1000.0), ok(1001.0)],
                                              "select": [ok(False), ok(False)], "read": []}))
    case("inj-rc-posix-stop-signal", "rc", rc("posix"),
         posix_world(responses={"select": [ok(False, signal=2), ok(False)], "read": []}))
    case("inj-rc-posix-stop-after-notice", "rc", rc("posix"),
         posix_world(responses={"select": [ok(True, signal=1), ok(False)]}))
    case("inj-rc-posix-eof-after-notice-data", "rc", rc("posix"),
         posix_world(measure=True, responses={"select": [ok(True), ok(True)],
                                              "read": [ok(notice().hex()), ok(b"z".hex())]}))
    case("inj-rc-posix-read-closed-ebadf", "rc", rc("posix"),
         posix_world(responses={"close": [ok(), err("EBADF")]}))
    case("inj-rc-posix-stop-group-and-problem", "rc", rc("posix"),
         posix_world(responses={"select": [ok(True)], "read": [ok("")], "wait_monitor": [{"timeout": True}],
                                "killpg": [ok(), ok()]}))
    # anchor の KILL の通知の後: 通知の行の読み取り → 0 秒の select の EOF → stop_group の killpg の結果
    # (Linux は成功で「anchor が回収前に失われた」、Mac は EPERM で置き換わる)→ 返す誤りの文。
    after_notice = {"select": [ok(True), ok(True)], "read": [ok(notice().hex()), ok("")]}
    case("inj-posix-anchor-killed-after-notice-linux", "rc", rc("posix"),
         posix_world(platform="linux", responses=dict(after_notice)))
    case("inj-posix-anchor-killed-after-notice-macos", "rc", rc(method=None),
         posix_world(platform="darwin", has_pidfd=False,
                     responses={**after_notice, "killpg": [err("EPERM")], "wait_monitor": []}))


def build():
    supervise_cases()
    parent_cases()
    out = []
    for item in CASES:
        world = World(item["world"])
        if item["kind"] == "rc":
            result = run_rc(world, item["input"])
        else:
            result = run_main(item["kind"], world, item["input"]["args"])
        unused = world.unused()
        if unused:
            raise Abort(f"{item['id']}: 使わなかった応答 {unused}")
        out.append({**item, "expect": {"calls": world.calls, "result": result}})
    return out


def main():
    parser = argparse.ArgumentParser(description="R4Q の差し替えの比較の固定期待を作る")
    parser.add_argument("--out", default=str(HERE / "injected.json"))
    args = parser.parse_args()
    check_python()
    sys.dont_write_bytecode = True
    cases = build()
    head = {
        "schema_version": 1,
        "purpose": "R4Q の差し替えの比較の固定期待(本家 CPython 3.10.11 で旧版の関数を mock した実測)",
        "generator": "native/fixtures/r4q/gen_injected.py",
        "python": {"version": "3.10.11", "legacy_sha256": LEGACY_SHA256},
        "clock": CLOCK,
        "token": TOKEN,
        "count": len(cases),
    }
    # ケースは 1 行に 1 つ(差分を読みやすくする)。
    text = (json.dumps(head, ensure_ascii=False, indent=1)[:-2] + ',\n "cases": [\n'
            + ",\n".join(json.dumps(item, ensure_ascii=False) for item in cases) + "\n ]\n}\n")
    Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
