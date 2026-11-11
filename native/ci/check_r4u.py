#!/usr/bin/env python3
"""R4U(#480)の比較器: 旧版 check_links.py(本家 CPython 3.10.11)・固定期待・Rust の検査用の実行物を比べる。

使い方:
    python3 -B native/ci/check_r4u.py --fixture <Rust の検査用の実行物>
        cargo test(native/crates/cli/tests/check_links.rs)から呼ばれる。全ケースを比べ、ID ごとの結果と件数を出す。
    python3 -B native/ci/check_r4u.py --measure-step2 [--out <ファイル>]
        手順 2(呼出履歴を出している間)の書込みの失敗と SIGINT の部品ごとの表を、旧版で実測して固定期待を作る。

- 旧版を動かす Python は R4U_PYTHON(無ければ PATH の python3)。版と標準ライブラリの SHA-256 が本家 3.10.11 と
  違えば失敗にする(成功にも skip にもしない)。
- 子の環境は、許すものを並べて一から作る(PATH・LC_ALL=C と、ケースが与える変数と、書込みの記録の 3 つ)。
- write(2) は native/ci/r4u_write_shim.c を一時フォルダで作り、旧版と Rust の両方に同じ形で差し込んで記録する。
- 呼出履歴の File 欄は、旧スクリプトと本家の標準ライブラリのパスだけを固定名(check_links.py・pathlib.py など)にする。
- 旧版と Rust は、同じ絶対パスに作り直した同じ入力で動かす(作業場所のパスは置き換えない)。
"""
from __future__ import annotations

import argparse
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
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
LEGACY_REL = "plugins/dev-workflow/skills/update-doc/scripts/check_links.py"
LEGACY = ROOT / LEGACY_REL
LEGACY_SHA256 = "b2eea09e6b0c1b4f84b8c4b36f2d04aa517c88b3eb410c5a19e619457e09d4ab"
FIXTURES = ROOT / "native/fixtures/r4u"
SHIM_SOURCE = Path(__file__).with_name("r4u_write_shim.c")
STDLIB_SHA256 = {
    "pathlib.py": "936f14a3ecc4ac5e896e4d673666d76b25a9d581f3b4c8d8d424e830fda2a69b",
    "posixpath.py": "2c770bac0680a5a180b1310102d14e1e120f2f2e8e47925b1574ec808cb8ad6d",
    "genericpath.py": "9d58ad64056a89f12258a62596c2073cd9e52b459dde152f710edc0cffcd865b",
}
LINUX = sys.platform.startswith("linux")
MAC = sys.platform == "darwin"
PRELOAD = "DYLD_INSERT_LIBRARIES" if MAC else "LD_PRELOAD"
SHIM_KEYS = (PRELOAD, "R4U_SHIM_LOG", "R4U_SHIM_RULES")
FIXTURE_FLAG = "--check-links-fixture"
RUN_TIMEOUT = 120
EAGAIN = errno.EAGAIN
ROOT_MARK = "@ROOT@"
THREAD_RE = re.compile(rb"Current thread 0x[0-9a-f]{16} \(most recent call first\):\n")
THREAD_VALUE_RE = re.compile(rb"^[0-9a-f]{16}$")
FILE_LINE_RE = re.compile(r'^  File "(.*)", line (\d+), in (.*)$')


class Failure(Exception):
    """比較の不合格(ケースの ID を本文に入れる)。"""


# ---- 旧 Python ----

@dataclass(frozen=True)
class Python:
    path: str
    version: str
    stdlib: str


PROBE = r"""
import hashlib, json, os, platform, sys
stdlib = os.path.dirname(os.__file__)
digest = {}
for name in ("pathlib.py", "posixpath.py", "genericpath.py"):
    with open(os.path.join(stdlib, name), "rb") as stream:
        digest[name] = hashlib.sha256(stream.read()).hexdigest()
print(json.dumps({"version": platform.python_version(), "stdlib": stdlib, "sha256": digest,
                  "stdout": [sys.stdout.encoding, sys.stdout.errors],
                  "stderr": [sys.stderr.encoding, sys.stderr.errors],
                  "environ": sorted(os.environ)}))
"""


def base_env(extra=None):
    """子の環境を一から作る。PATH と LC_ALL のほかは、渡したものだけ。"""
    env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}
    for key, value in (extra or {}).items():
        env[key] = value
    allowed = {"PATH", "LC_ALL", *SHIM_KEYS, *(extra or {})}
    if set(env) - allowed:
        raise Failure("比較の前提の外の変数が子の環境に残っている: " + ", ".join(sorted(set(env) - allowed)))
    return env


def find_python():
    path = os.environ.get("R4U_PYTHON") or shutil.which("python3")
    if not path:
        raise Failure("旧版を動かす Python が見つからない(R4U_PYTHON か PATH の python3)")
    with tempfile.TemporaryDirectory(prefix="r4u-probe-") as temp:
        done = subprocess.run([path, "-c", PROBE], cwd=temp, env=base_env(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if done.returncode != 0 or done.stderr:
        raise Failure(f"旧版を動かす Python を調べられない: {done.stderr!r}")
    info = json.loads(done.stdout.decode("utf-8"))
    if info["version"] != "3.10.11":
        raise Failure(f"本家と同じ中身の 3.10.11 で流す(実際は {info['version']})。R4U_PYTHON を渡す")
    for name, digest in STDLIB_SHA256.items():
        if info["sha256"][name] != digest:
            raise Failure(f"{name} が本家 3.10.11 と違う(標準ライブラリの行番号を比べられない)。R4U_PYTHON を渡す")
    if info["stdout"] != ["utf-8", "surrogateescape"] or info["stderr"] != ["utf-8", "backslashreplace"]:
        raise Failure(f"基準の環境で旧 Python の文字コードが違う: {info['stdout']} {info['stderr']}")
    if info["environ"] != ["LC_ALL", "PATH"]:
        raise Failure(f"子の環境に余計な変数がある: {info['environ']}")
    return Python(path, info["version"], info["stdlib"])


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
    writes = []
    for line in data.decode("ascii").splitlines():
        record = json.loads(line)
        writes.append(Write(record["fd"], bytes.fromhex(record["data"]), record["len"], record["ret"],
                            record["errno"], record["action"]))
    return writes


# ---- 入力の木 ----

def raw(value):
    if isinstance(value, dict):
        if set(value) != {"hex"}:
            raise Failure(f"バイト列の形が違う: {value!r}")
        return bytes.fromhex(value["hex"])
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def build_tree(root, ops):
    """`ops` は legacy-cases.json の形(dir・file・symlink・fifo)か、(root) を受ける関数。"""
    for op in ops:
        if callable(op):
            op(root)
            continue
        path = os.path.join(os.fsencode(root), raw(op["path"]))
        kind = op["op"]
        if kind == "dir":
            os.mkdir(path)
        elif kind == "file":
            with open(path, "xb") as stream:
                stream.write(raw(op["data"]))
        elif kind == "symlink":
            os.symlink(raw(op["target"]), path)
        elif kind == "fifo":
            os.mkfifo(path)
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
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            st = os.lstat(current)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            os.chmod(current, 0o700)
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False) and len(entry.path) < 3000:
                            stack.append(entry.path)
            except OSError:
                pass
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(20000)
    try:
        shutil.rmtree(root)
    finally:
        sys.setrecursionlimit(limit)


def snapshot(root):
    """入力の木の中身(種類・権限・大きさ・時刻・中身・リンク先)。深い所は fd から辿る。"""
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
    tree: list = field(default_factory=list)
    argv: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    stdin: str = "devnull"
    stdout: str = "pipe"
    stderr: str = "pipe"
    rules: str = ""
    scenario: str | None = None
    interrupt: bool | None = False
    fatal: bool = False
    expected: dict | None = None
    expected_os: str | None = None
    preserve: bool = True
    rlimit_fsize: int | None = None
    sigint_ignored: bool = False
    # 親が子へ渡す SIGPIPE の扱い("default" か "ignore")。None は subprocess の既定(既定の扱いに戻す)。
    sigpipe: str | None = None
    group: str = "behavior"
    # 文字コードの設定だけで旧版が変わる条件: Rust はこの設定を読まないので、設定なしの旧版と比べる。
    # 設定ありの旧版は、設定なしの旧版と違う(か、同じと決めた)ことを別の期待として確かめる。
    encoding_env: dict | None = None
    encoding_differs: bool = True


@dataclass
class Result:
    code: int | None
    stdout: bytes
    stderr: bytes
    writes: list
    timed_out: bool = False


class Context:
    def __init__(self, python, rust, shim, temp):
        self.python = python
        self.rust = rust
        self.shim = shim
        self.temp = Path(temp)
        self.root = str(self.temp / "case")
        self.counter = 0


def stdio_target(spec, temp, closers, pty_masters, which):
    """標準入出力の 1 つを用意する。戻り値は Popen に渡す値(None は閉じる)。"""
    if spec == "pipe":
        return subprocess.PIPE
    if spec == "stdout":
        return subprocess.STDOUT
    if spec == "devnull":
        return subprocess.DEVNULL
    if spec == "devfull":
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


def read_pty(master, sink):
    while True:
        try:
            chunk = os.read(master, 65536)
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


def blocked_in_fifo_open(pid):
    try:
        with open(f"/proc/{pid}/wchan") as stream:
            return stream.read() in ("fifo_open", "wait_for_partner")
    except OSError:
        return False


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


def execute(ctx, case, kind, env_override=None):
    """1 回の実行。入力の木は毎回同じ絶対パスに作り直す。"""
    restore_and_remove(ctx.root)
    os.mkdir(ctx.root, 0o700)
    build_tree(ctx.root, case.tree)
    before = snapshot(ctx.root) if case.preserve else None
    ctx.counter += 1
    work = ctx.temp / f"run-{ctx.counter}"
    work.mkdir()
    log_read, log_write = os.pipe()
    extra = case.env if env_override is None else env_override
    env = base_env({**extra, PRELOAD: str(ctx.shim), "R4U_SHIM_LOG": f"fd:{log_write}",
                    "R4U_SHIM_RULES": case.rules})
    argv = [arg(ctx.root) if callable(arg) else raw(arg) for arg in case.argv]
    if kind == "legacy":
        command = [ctx.python.path, str(LEGACY), *argv]
    else:
        command = [ctx.rust, FIXTURE_FLAG, *argv]
    cwd = ctx.root
    if case.scenario == "getcwd-removed":
        cwd = os.path.join(ctx.root, "w")
    closers = [log_write]
    masters = []
    fds = {}
    closed = []
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
            if case.rlimit_fsize is not None:
                resource.setrlimit(resource.RLIMIT_FSIZE, (case.rlimit_fsize, case.rlimit_fsize))
            if case.sigint_ignored:
                signal.signal(signal.SIGINT, signal.SIG_IGN)
            if case.sigpipe is not None:
                signal.signal(signal.SIGPIPE, {"default": signal.SIG_DFL, "ignore": signal.SIG_IGN}[case.sigpipe])

        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=fds["stdin"], stdout=fds["stdout"],
                                stderr=fds["stderr"], preexec_fn=preexec, pass_fds=(log_write,))
        log_sink = []
        log_reader = threading.Thread(target=read_pty, args=(log_read, log_sink), daemon=True)
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
            reader = threading.Thread(target=read_pty, args=(master, sink), daemon=True)
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
    writes = read_log(b"".join(log_sink))
    if case.preserve and snapshot(ctx.root) != before:
        raise Failure(f"{case.id}/{kind}: 入力の木が変わった(またはファイルが増えた)")
    return Result(proc.returncode, stdout, stderr, writes)


def run_scenario(ctx, case, proc):
    if case.scenario is None:
        return
    if not LINUX:
        raise Failure(f"{case.id}: この OS では待ちの検出が未実装")
    if case.scenario in ("fifo-sigint", "fifo-sigterm", "fifo-ignored"):
        wait_until(lambda: blocked_in_fifo_open(proc.pid), f"{case.id}: FIFO を開く待ち")
        if case.scenario == "fifo-sigint":
            proc.send_signal(signal.SIGINT)
        elif case.scenario == "fifo-sigterm":
            proc.send_signal(signal.SIGTERM)
        else:
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
        wait_until(lambda: blocked_in_fifo_open(proc.pid), f"{case.id}: FIFO を開く待ち")
        writer = os.open(fifo, os.O_WRONLY)
        try:
            wait_until(lambda: opened(proc.pid, fifo), f"{case.id}: FIFO を開いた")
            os.unlink(fifo)
            os.rmdir(work)
            os.write(writer, b"[x](b)\n")
        finally:
            os.close(writer)
        return
    raise Failure(f"未知の進め方: {case.scenario}")


# ---- 正規化と比較 ----

class Normalizer:
    """旧版の呼出履歴の File 欄だけを固定名にする。それ以外の欄・行・stdout には触れない。

    File 欄は `  File "<パス>", line <数>, in ` の形でだけ探す(前の部品が改行で終わらず、行の途中に来たときも)。
    パスが旧スクリプトか本家の標準ライブラリのとき以外は置き換えず、行頭の File 欄なら失敗にする。
    """

    def __init__(self, python):
        names = {str(LEGACY): "check_links.py"}
        for name in STDLIB_SHA256:
            names[os.path.join(python.stdlib, name)] = name
        self.names = {os.fsencode(path): fixed.encode() for path, fixed in names.items()}
        alternatives = b"|".join(re.escape(path) for path in sorted(self.names, key=len, reverse=True))
        self.field = re.compile(b'  File "(' + alternatives + b')", line (\\d+), in ')

    def text(self, data):
        def replace(match):
            return b'  File "' + self.names[match.group(1)] + b'", line ' + match.group(2) + b", in "
        replaced = self.field.sub(replace, data)
        for line in replaced.split(b"\n"):
            found = FILE_LINE_RE.match(line.decode("utf-8", "surrogateescape"))
            if found and os.fsencode(found.group(1)) not in {v for v in self.names.values()}:
                raise Failure(f"想定外の File 欄: {found.group(1)!r}")
        return replaced

    def writes(self, writes):
        result = []
        for write in writes:
            if write.fd not in (1, 2):
                continue
            data = self.text(write.data) if write.fd == 2 else write.data
            if 0 <= write.ret < write.length and data != write.data:
                raise Failure("固定名の欄を含む書込みが途中で切れた(22:38 の決定の扱いは Task 3.1)")
            ret = "full" if write.ret == write.length else write.ret
            result.append((write.fd, data, ret, write.errno if write.ret < 0 else 0, write.action))
        return result


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


def last_line(data):
    lines = data.rstrip(b"\n").split(b"\n")
    return lines[-1] if lines else b""


def compare(ctx, normalizer, case, legacy, rust):
    for kind, result in (("legacy", legacy), ("rust", rust)):
        if result.timed_out:
            raise Failure(f"{case.id}/{kind}: 時間切れ")
        if type(result.code) is not int:
            raise Failure(f"{case.id}/{kind}: 未実行")
    legacy_stderr = normalizer.text(legacy.stderr)
    if case.stderr == "stdout":
        legacy = Result(legacy.code, normalizer.text(legacy.stdout), legacy.stderr, legacy.writes,
                        legacy.timed_out)
    legacy_writes = normalizer.writes(legacy.writes)
    rust_writes = normalizer.writes(rust.writes)
    rust_stderr = rust.stderr
    if case.fatal:
        if case.stderr == "pipe":
            legacy_stderr = mask_thread(legacy_stderr)
            rust_stderr = mask_thread(rust_stderr)
        legacy_writes, legacy_count = mask_thread_writes(legacy_writes)
        rust_writes, rust_count = mask_thread_writes(rust_writes)
        if legacy_count != 1 or rust_count != 1:
            raise Failure(f"{case.id}: Current thread の値の書込みが 1 回でない(旧版 {legacy_count}・Rust {rust_count})")
    if legacy.code != rust.code:
        raise Failure(f"{case.id}: 終了値が違う(旧版 {legacy.code}・Rust {rust.code})")
    if legacy.stdout != rust.stdout:
        raise Failure(f"{case.id}: stdout が違う\n旧版 {legacy.stdout!r}\nRust {rust.stdout!r}")
    interrupt = case.interrupt if case.interrupt is not None else legacy.code == -signal.SIGINT
    if interrupt:
        if legacy.code != -signal.SIGINT:
            raise Failure(f"{case.id}: 旧版が SIGINT で終わっていない({legacy.code})")
        if last_line(legacy_stderr) != b"KeyboardInterrupt" or last_line(rust_stderr) != b"KeyboardInterrupt":
            raise Failure(f"{case.id}: 最後の行が KeyboardInterrupt でない")
        if [w for w in legacy_writes if w[0] == 1] != [w for w in rust_writes if w[0] == 1]:
            raise Failure(f"{case.id}: stdout の書込みの記録が違う")
    else:
        if legacy_stderr != rust_stderr:
            diff = "\n".join(difflib.unified_diff(
                legacy_stderr.decode("utf-8", "replace").splitlines(),
                rust_stderr.decode("utf-8", "replace").splitlines(), "旧版", "Rust", lineterm=""))
            raise Failure(f"{case.id}: stderr が違う\n{diff}")
        if legacy_writes != rust_writes:
            raise Failure(f"{case.id}: 書込みの記録が違う\n旧版 {legacy_writes!r}\nRust {rust_writes!r}")
    if case.expected is not None and (case.expected_os is None or case.expected_os == sys.platform):
        expected = case.expected
        got_stderr = rust_stderr.replace(os.fsencode(ctx.root), ROOT_MARK.encode())
        got = (rust.code, rust.stdout, got_stderr)
        want = (expected["exit"], raw(expected["stdout"]), raw(expected["stderr"]))
        if got != want:
            raise Failure(f"{case.id}: 固定期待と違う\n期待 {want!r}\n実際 {got!r}")
    for kind, result in (("旧版", legacy), ("Rust", rust)):
        if (result.stdout or result.stderr) and not result.writes:
            raise Failure(f"{case.id}: {kind}の書込みが記録に現れない")


# ---- ケースの一覧 ----

def legacy_cases():
    data = json.loads((FIXTURES / "legacy-cases.json").read_text(encoding="utf-8"))
    if data["legacy"]["sha256"] != LEGACY_SHA256:
        raise Failure("legacy-cases.json の旧版の SHA-256 が違う")
    return [Case("legacy:" + case["id"], tree=case["tree"], argv=case["argv"], expected=case["expected"],
                 group="legacy-cases") for case in data["cases"]]


def chmod(path, mode):
    return lambda root: os.chmod(os.path.join(root, path), mode)


def write(path, data):
    def op(root):
        full = os.path.join(os.fsencode(root), raw(path))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as stream:
            stream.write(raw(data))
    return op


def linux_traceback_cases():
    """linux.json の旧版の呼出履歴の固定期待(Task 1.1)と同じ入力。"""
    data = json.loads((FIXTURES / "linux.json").read_text(encoding="utf-8"))
    expected = {case["id"]: case for case in data["tracebacks"]}
    long_name = "a" * 300

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

    def chain(root):
        make_chain(root, 1100)

    def links(root):
        with open(os.path.join(root, "a.md"), "w") as stream:
            stream.write("".join(f"[x](m{index:03d}{'q' * 60})\n" for index in range(200)))

    cases = [
        Case("target-isdir-eacces", [{"op": "dir", "path": "noperm"}, chmod("noperm", 0)], ["noperm/x"]),
        Case("target-isdir-enametoolong", [], [long_name]),
        Case("target-warn-then-eacces", [{"op": "dir", "path": "noperm"}, chmod("noperm", 0)],
             ["missing", "noperm/x"]),
        Case("rglob-deep-enametoolong", [deep(21, "d" * 200)], ["t"]),
        Case("rglob-recursion", [deep(995, "d")], ["t"]),
        Case("read-isdir", [write("g/y.md", "[a](b)\n"), {"op": "dir", "path": "g/x.md"}], ["g"]),
        Case("read-eacces", [write("a.md", "[x](m)\n"), chmod("a.md", 0)], ["a.md"]),
        Case("read-fifo-sigint", [{"op": "fifo", "path": "a.md"}], ["."], scenario="fifo-sigint",
             interrupt=True),
        Case("link-resolve-nul", [write("a.md", b"[x](a\x00b)\n")], ["a.md"]),
        Case("link-resolve-loop", [write("d/a.md", "[a](loop)\n"), {"op": "symlink", "path": "d/loop",
                                                                    "target": "loop"}], ["d/a.md"]),
        Case("link-resolve-getcwd", [{"op": "dir", "path": "w"}, {"op": "fifo", "path": "w/a.md"}], ["."],
             scenario="getcwd-removed", preserve=False),
        Case("link-resolve-recursion", [chain, write("a.md", "[x](l0)\n")], ["a.md"]),
        Case("link-exists-eacces", [{"op": "dir", "path": "n"}, chmod("n", 0), write("a.md", "[x](n/a.md)\n")],
             ["a.md"]),
        Case("link-exists-enametoolong", [write("a.md", f"[x]({long_name})\n")], ["a.md"]),
        Case("code-resolve-recursion", [chain, write("a.md", "`l0/x.md`\n")], ["a.md"]),
        Case("code-alt-eacces", [{"op": "dir", "path": "x"}, chmod("x", 0), write("d/doc.md", "`x/y.md`\n")],
             ["d/doc.md"]),
        Case("code-exists-eacces", [write("x/y.md", ""), write("d/doc.md", "`x/y.md`\n"),
                                    {"op": "dir", "path": "d/x"}, chmod("d/x", 0)], ["d/doc.md"]),
        Case("rglob-entry-isdir-enametoolong", [deep(20, "d" * 200, "l" * 100)], ["t"]),
        Case("print-brokenpipe", [links], ["a.md"], stdout="broken"),
        Case("exit-flush-devfull", [write("a.md", "[x](m)\n")], ["a.md"], stdout="devfull"),
    ]
    for case in cases:
        case.group = "linux-tracebacks"
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


def behavior_cases():
    cases = []
    add = cases.append
    # 引数 0 個と PYTHONOPTIMIZE
    for name, value in (("unset", None), ("0", "0"), ("1", "1"), ("2", "2"), ("3", "3"), ("bad", "x"),
                        ("negative", "-2"), ("space", " 2"), ("trailing", "2 "), ("empty", "")):
        env = {} if value is None else {"PYTHONOPTIMIZE": value}
        add(Case(f"args-none-optimize-{name}", [], [], env=env))
    add(Case("args-none-pty", [], [], stdout="pty"))
    # 起動の失敗と環境変数
    for name, value in (("random", "random"), ("zero", "0"), ("max", "4294967295"), ("space", " 7"),
                        ("minus-zero", "-0"), ("empty", "")):
        add(Case(f"env-hashseed-ok-{name}", BASIC, ["docs"], env={"PYTHONHASHSEED": value}))
    for name, value in (("word", "Random"), ("over", "4294967296"), ("negative", "-1"), ("trailing", "5 "),
                        ("huge", "9" * 30)):
        add(Case(f"env-hashseed-bad-{name}", BASIC, ["docs"], env={"PYTHONHASHSEED": value}))
    for name, value in (("zero", "0"), ("min", "640"), ("big", "100000")):
        add(Case(f"env-intmax-ok-{name}", BASIC, ["docs"], env={"PYTHONINTMAXSTRDIGITS": value}))
    for name, value in (("small", "639"), ("negative", "-1"), ("word", "x"), ("huge", "9" * 30)):
        add(Case(f"env-intmax-bad-{name}", BASIC, ["docs"], env={"PYTHONINTMAXSTRDIGITS": value}))
    add(Case("env-both-bad", BASIC, ["docs"], env={"PYTHONHASHSEED": "x", "PYTHONINTMAXSTRDIGITS": "1"}))
    # 起動の判定の 1・2 の診断は、親から受け継いだ SIGPIPE の扱いのまま書く(閉じたパイプの stderr)。
    for name, env in (("hashseed", {"PYTHONHASHSEED": "x"}), ("intmax", {"PYTHONINTMAXSTRDIGITS": "1"})):
        for disposition, code in (("default", -signal.SIGPIPE), ("ignore", 1)):
            add(Case(f"startup-{name}-broken-stderr-sigpipe-{disposition}", BASIC, ["docs"], env=env,
                     stderr="broken", sigpipe=disposition,
                     expected={"exit": code, "stdout": "", "stderr": ""}))
    # 対照: 標準入出力の判定(起動の判定の 4 以降)では、もう SIGPIPE を無視にしている。
    add(Case("startup-stdout-dir-broken-stderr-sigpipe-default", BASIC, ["docs"], stdout="dir", stderr="broken",
             sigpipe="default", fatal=True, expected={"exit": 1, "stdout": "", "stderr": ""}))
    for name, value in (("one", "1"), ("zero", "0"), ("zeros", "000"), ("bad", "x"), ("negative", "-1"),
                        ("empty", ""), ("space-zero", " 0")):
        add(Case(f"env-unbuffered-{name}", BASIC, ["docs"], env={"PYTHONUNBUFFERED": value}))
    add(Case("env-lang-c-utf8", BASIC, ["docs"], env={"LANG": "C.UTF-8"}))
    # 文字コードの設定(Rust は読まない。設定なしの旧版と比べ、設定ありの旧版との差を別の期待として確かめる)
    non_utf8 = [{"op": "dir", "path": "d"}, {"op": "file", "path": {"hex": "642f78ff2e6d64"},
                                             "data": "[x](m)\n"}]
    for name, env, tree, argv, differs in (
            ("pythonioencoding-utf8-strict-non-utf8-name", {"PYTHONIOENCODING": "utf-8"}, non_utf8, ["d"], True),
            ("pythonioencoding-ascii", {"PYTHONIOENCODING": "ascii"}, BASIC, ["docs"], True),
            ("pythonioencoding-unknown", {"PYTHONIOENCODING": "no-such-codec"}, BASIC, ["docs"], True),
            ("pythonutf8-zero", {"PYTHONUTF8": "0"}, non_utf8, ["d"], True),
            ("lang-c-utf8-non-utf8-name", {"LANG": "C.UTF-8"}, non_utf8, ["d"], False)):
        add(Case(f"encoding-{name}", tree, argv, env=env, encoding_env={}, encoding_differs=differs))
    # 標準入出力がディレクトリ・閉じている
    add(Case("stdio-stdin-dir", BASIC, ["docs"], stdin="dir", fatal=True))
    add(Case("stdio-stdout-dir", BASIC, ["docs"], stdout="dir", fatal=True))
    add(Case("stdio-stderr-dir", BASIC, ["docs"], stderr="dir", fatal=True))
    add(Case("stdio-stdout-stderr-dir", BASIC, ["docs"], stdout="dir", stderr="dir", fatal=True))
    add(Case("stdio-stdin-dir-bad-hashseed", BASIC, ["docs"], stdin="dir", env={"PYTHONHASHSEED": "x"}))
    # 呼出元が渡した読み書きの /dev/null(subprocess.DEVNULL と同じ O_RDWR)は使える fd なので、旧版は書く。
    add(Case("stdio-devnull-stdout", BASIC, ["docs"], stdout="devnull"))
    add(Case("stdio-devnull-stderr-exception", ONE, ["missing", "g"], stderr="devnull"))
    add(Case("stdio-devnull-both", BASIC, ["docs"], stdout="devnull", stderr="devnull"))
    add(Case("stdio-devnull-both-exception", ONE, ["missing", "g"], stdout="devnull", stderr="devnull"))
    for fd in ("stdin", "stdout", "stderr"):
        add(Case(f"stdio-closed-{fd}", BASIC, ["docs"], **{fd: "closed"}))
        add(Case(f"stdio-closed-{fd}-exception", ONE, ["g"], **{fd: "closed"}))
    # 出力の溜めの境目
    for total in (8191, 8192, 8193, 16383, 16384, 16385):
        for mode, extra in (("pipe", {}), ("file", {}), ("unbuffered", {"PYTHONUNBUFFERED": "1"})):
            add(Case(f"output-{total}-{mode}", sized_tree(total), ["a.md"], env=extra,
                     stdout="file" if mode == "file" else "pipe"))
        add(Case(f"output-{total}-pty", sized_tree(total), ["a.md"], stdout="pty"))
    add(Case("output-pty-cr-lf-names", [write("d/a\rb.md", "[x](m)\n"), write("d/c\nd.md", "[y](n)\n")], ["d"],
             stdout="pty"))
    add(Case("output-same-pipe-exception", ONE, ["missing", "g"], stderr="stdout"))
    # 書込みの故障
    for mode, extra in (("buffered", {}), ("unbuffered", {"PYTHONUNBUFFERED": "1"})):
        add(Case(f"fault-stdout-devfull-{mode}", BASIC, ["docs"], env=extra, stdout="devfull"))
        add(Case(f"fault-stdout-broken-{mode}", BASIC, ["docs"], env=extra, stdout="broken"))
        add(Case(f"fault-stdout-eagain-{mode}", BASIC, ["docs"], env=extra, stdout="eagain"))
        add(Case(f"fault-stderr-devfull-exception-{mode}", ONE, ["g"], env=extra, stderr="devfull"))
        add(Case(f"fault-stderr-devfull-noexception-{mode}", BASIC, ["docs"], env=extra, stderr="devfull"))
        add(Case(f"fault-stderr-broken-exception-{mode}", ONE, ["g"], env=extra, stderr="broken"))
        add(Case(f"fault-both-devfull-{mode}", BASIC, ["docs"], env=extra, stdout="devfull", stderr="devfull"))
        add(Case(f"fault-both-devfull-exception-{mode}", ONE, ["missing", "g"], env=extra, stdout="devfull",
                 stderr="devfull"))
        add(Case(f"fault-stdout-eio-once-{mode}", BASIC, ["docs"], env=extra, rules="fd=1,nth=1,action=fail,errno=5"))
        add(Case(f"fault-stdout-short-{mode}", sized_tree(9000), ["a.md"], env=extra,
                 rules="fd=1,nth=1,action=short,bytes=100"))
        add(Case(f"fault-stdout-quota-{mode}", sized_tree(9000), ["a.md"], env=extra,
                 rules="fd=1,action=quota,bytes=5000,errno=27"))
        add(Case(f"fault-stdout-rlimit-{mode}", sized_tree(9000), ["a.md"], env=extra, stdout="file",
                 rlimit_fsize=5000))
        add(Case(f"fault-stdout-flushio-eagain-sigint-{mode}", BASIC, ["docs"], env=extra,
                 rules="fd=1,nth=1,action=fail,errno=11;fd=1,nth=2,action=sigint"))
        add(Case(f"fault-stdout-close-sigint-{mode}", BASIC, ["docs"], env=extra,
                 rules="fd=1,nth=1,action=fail,errno=11;fd=1,nth=2,action=fail,errno=11;fd=1,nth=3,action=sigint"))
        add(Case(f"fault-unraisable-sigint-{mode}", BASIC, ["docs"], env=extra,
                 rules="fd=1,nth=1,action=fail,errno=11;fd=1,nth=2,action=fail,errno=11;fd=2,nth=1,action=sigint"))
    # 信号
    warn = [{"op": "dir", "path": "f"}, {"op": "fifo", "path": "f/a.md"}]
    for prefix, argv in (("", ["f"]), ("warn-", ["missing", "f"])):
        for mode, extra, out in (("pipe", {}, "pipe"), ("unbuffered", {"PYTHONUNBUFFERED": "1"}, "pipe"),
                                 ("pty", {}, "pty")):
            add(Case(f"signal-fifo-sigint-{prefix}{mode}", warn, argv, env=extra, stdout=out,
                     scenario="fifo-sigint", interrupt=True))
            add(Case(f"signal-fifo-sigterm-{prefix}{mode}", warn, argv, env=extra, stdout=out,
                     scenario="fifo-sigterm"))
    # 比較器が FIFO へ書くと FIFO の時刻が変わるので、入力の無変更は確かめない。
    add(Case("signal-sigint-ignored", warn, ["missing", "f"], scenario="fifo-ignored", sigint_ignored=True,
             preserve=False))
    big = sized_tree(20000)
    for mode, extra, out in (("pipe", {}, "pipe"), ("unbuffered", {"PYTHONUNBUFFERED": "1"}, "pipe"),
                             ("pty", {}, "pty")):
        for nth in (1, 2, 3):
            # 何回目の write が本処理の中かは条件で変わる。旧版が SIGINT で終わったときだけ中断の比べ方にする。
            add(Case(f"signal-write-sigint-{mode}-{nth}", big, ["a.md"], env=extra, stdout=out,
                     rules=f"fd=1,nth={nth},action=sigint", interrupt=None))
    # 再帰の上限の境目
    add(Case("rglob-depth-988", [lambda root: make_deep(os.path.join(root, "t"), 988, "d")], ["t"]))
    add(Case("rglob-depth-989", [lambda root: make_deep(os.path.join(root, "t"), 989, "d")], ["t"]))
    for count in (992, 993):
        add(Case(f"link-chain-{count}", [lambda root, count=count: make_chain(root, count),
                                         write("a.md", "[x](l0)\n")], ["a.md"]))
        add(Case(f"code-chain-{count}", [lambda root, count=count: make_chain(root, count),
                                         write("a.md", "`l0/x.md`\n")], ["a.md"]))
    # 走査の権限と種類
    add(Case("walk-unreadable-dir", [write("t/a.md", ""), write("t/r/b.md", ""), chmod("t/r", 0o300)], ["t"]))
    add(Case("walk-unsearchable-dir", [write("t/a.md", ""), write("t/x/b.md", ""), chmod("t/x", 0o600)], ["t"]))
    add(Case("walk-kinds", [write("t/a.md", "[x](m)\n"), {"op": "symlink", "path": "t/broken.md",
                                                         "target": "nowhere"}], ["t"]))
    add(Case("walk-permission-abort", [permission_abort_tree], ["t"]))
    add(Case("args-stat-eacces", [{"op": "dir", "path": "n"}, chmod("n", 0)], ["n/a.md"]))
    add(Case("body-link-eacces-and-long", [{"op": "dir", "path": "n"}, chmod("n", 0),
                                           write("a.md", "[x](n/m.md)\n")], ["a.md"]))
    add(Case("body-relative-nul", [write("a.md", b"[x](a\x00b)\n")], ["a.md"]))
    add(Case("body-loop-nul", [write("a.md", b"[x](loop/a\x00b)\n"), {"op": "symlink", "path": "loop",
                                                                     "target": "loop"}], ["a.md"]))
    add(Case("body-loop-absolute-target", [write("d/a.md", "[a](loop/../x)\n"),
                                           {"op": "symlink", "path": "d/loop", "target": "loop"}],
             [lambda root: os.fsencode(root) + b"/d/a.md"]))
    add(Case("body-double-slash-md", [write("d/a.md", "[a](b.md) `c/d.md`\n")], [lambda root: b"/" + os.fsencode(root) + b"/d/a.md"]))
    for case in cases:
        case.group = "behavior"
    return cases


def permission_abort_tree(root):
    """子の判定の PermissionError で、残りの子への走査を打ち切る木(リンクが子のディレクトリより先に並ぶ)。"""
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
        names = [entry.name for entry in os.scandir(base)]
        if names.index(f"l{attempt}") < names.index(f"s{attempt}"):
            os.chmod(os.path.join(root, "n"), 0o600)
            return
        os.unlink(link)
        os.unlink(os.path.join(sub, "z.md"))
        os.rmdir(sub)
    raise Failure("打ち切りの前提(リンクが子のディレクトリより先に並ぶ)を作れない")


# ---- 手順 2 の表 ----

STEP2_INPUTS = {
    "one": (ONE, ["g"]),
    "two": ([write("d/a.md", "[a](loop)\n"), {"op": "symlink", "path": "d/loop", "target": "loop"}], ["d/a.md"]),
    "rec": ([lambda root: make_deep(os.path.join(root, "t"), 995, "d")], ["t"]),
}
STEP2_ACTIONS = {
    "sigint": "action=sigint",
    "fail-once": "action=fail,errno=28",
    "fail-persist": "action=fail,errno=28,persist=1",
    "fail-persist-epipe": f"action=fail,errno={errno.EPIPE},persist=1",
}


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


def step2_measure(ctx, normalizer):
    entries = []
    for name, (tree, argv) in STEP2_INPUTS.items():
        for mode, extra in (("buffered", {}), ("unbuffered", {"PYTHONUNBUFFERED": "1"})):
            base = execute(ctx, Case(f"step2-{name}-{mode}-base", tree, argv, env=extra), "legacy")
            base_writes = normalizer.writes(base.writes)
            texts = [w[1].decode("utf-8") for w in base_writes if w[0] == 2]
            base_text = normalizer.text(base.stderr).decode("utf-8")
            for kind, (index, prefix, occ, exact) in step2_targets(texts, mode).items():
                for action, rule in STEP2_ACTIONS.items():
                    rules = f"fd=2,prefix={prefix.encode().hex()},occ={occ},exact={exact},{rule}"
                    case = Case(f"step2:{name}:{mode}:{kind}:{action}", tree, argv, env=extra, rules=rules)
                    got = execute(ctx, case, "legacy")
                    writes = normalizer.writes(got.writes)
                    injected = [i for i, w in enumerate(writes) if w[4] != "none"]
                    if not injected:
                        raise Failure(f"{case.id}: 故障を当てた書込みが記録に無い")
                    result, dropped, added = describe(base_text, normalizer.text(got.stderr).decode("utf-8"))
                    entries.append({
                        "id": case.id, "input": name, "mode": mode, "piece": kind, "action": action,
                        "rules": rules, "exit": got.code,
                        "stderr": normalizer.text(got.stderr).decode("utf-8").replace(ctx.root, ROOT_MARK),
                        "writes": [[w[0], w[1].decode("utf-8").replace(ctx.root, ROOT_MARK), w[2], w[3], w[4]]
                                   for w in writes],
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
    path = FIXTURES / "step2.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for entry in data["entries"]:
        tree, argv = STEP2_INPUTS[entry["input"]]
        extra = {"PYTHONUNBUFFERED": "1"} if entry["mode"] == "unbuffered" else {}
        case = Case(entry["id"], tree, argv, env=extra, rules=entry["rules"], group="step2")
        case.step2 = entry
        cases.append(case)
    return cases, data


def check_step2(ctx, normalizer, case, legacy, rust):
    entry = case.step2
    for kind, result in (("legacy", legacy), ("rust", rust)):
        writes = normalizer.writes(result.writes)
        got = [[w[0], w[1].decode("utf-8").replace(ctx.root, ROOT_MARK), w[2], w[3], w[4]] for w in writes]
        stderr = normalizer.text(result.stderr).decode("utf-8").replace(ctx.root, ROOT_MARK) \
            if kind == "legacy" else result.stderr.decode("utf-8").replace(ctx.root, ROOT_MARK)
        if (result.code, stderr, got) != (entry["exit"], entry["stderr"], entry["writes"]):
            raise Failure(f"{case.id}/{kind}: 手順 2 の固定期待と違う\n期待 {entry['writes']!r}\n実際 {got!r}")


# ---- 実行 ----

def probe_recording(ctx):
    """試しの書込み(引数 0 個の docstring)が、旧版と Rust の記録に現れるかを別々に確かめる。"""
    case = Case("probe-recording", [], [])
    for kind in ("legacy", "rust"):
        result = execute(ctx, case, kind)
        if not any(w.fd == 1 and b"Markdown" in w.data for w in result.writes):
            if kind == "rust":
                raise Failure("Rust の試しの書込みが記録に現れない")
            raise Failure("旧版の試しの書込みが記録に現れない(Mac の閉じた一覧の条件は Task 3.1)")


def show(case, kind, result):
    print(f"--- {case.id}/{kind}: 終了 {result.code}")
    print(f"stdout {result.stdout[:300]!r}")
    print(f"stderr {result.stderr[-600:]!r}")
    for write in result.writes:
        print(f"  write fd={write.fd} len={write.length} ret={write.ret} errno={write.errno} "
              f"{write.action} {write.data[:80]!r}")


def main_compare(rust, only=None):
    if not LINUX:
        raise Failure("この段の比較器は Linux だけ(Mac の閉じた一覧と待ちの検出は Task 3.1)")
    if os.geteuid() == 0:
        raise Failure("root でない利用者で流す")
    if hashlib.sha256(LEGACY.read_bytes()).hexdigest() != LEGACY_SHA256:
        raise Failure("旧版が変わった")
    started = time.monotonic()
    python = find_python()
    normalizer = Normalizer(python)
    with tempfile.TemporaryDirectory(prefix="r4u-check-") as temp:
        temp = os.path.realpath(temp)
        os.chmod(temp, 0o700)
        ctx = Context(python, rust, build_shim(temp), temp)
        probe_recording(ctx)
        step2, step2_data = step2_cases()
        cases = legacy_cases() + linux_traceback_cases() + behavior_cases() + step2
        if only:
            # 調べるための部分実行。合格にはしない。
            for case in cases:
                if case.id in only:
                    for kind in ("legacy", "rust"):
                        show(case, kind, execute(ctx, case, kind))
            print("部分実行(--only)は合格にしない")
            return 2
        ids = [case.id for case in cases]
        if not cases:
            raise Failure("比較ケースが 0 件")
        if len(ids) != len(set(ids)):
            raise Failure("比較ケースの ID が重複している")
        passed = 0
        failed = []
        for case in cases:
            try:
                if case.encoding_env is not None:
                    legacy = execute(ctx, case, "legacy", case.encoding_env)
                    configured = execute(ctx, case, "legacy")
                    differs = (configured.code, configured.stdout, configured.stderr) != \
                        (legacy.code, legacy.stdout, normalizer.text(legacy.stderr)) and \
                        (configured.code, configured.stdout, normalizer.text(configured.stderr)) != \
                        (legacy.code, legacy.stdout, normalizer.text(legacy.stderr))
                    if differs != case.encoding_differs:
                        raise Failure(f"{case.id}: 設定ありの旧版が、設定なしの旧版と"
                                      f"{'同じ' if case.encoding_differs else '違う'}(別の期待と違う)")
                else:
                    legacy = execute(ctx, case, "legacy")
                rust_result = execute(ctx, case, "rust")
                if case.group == "step2":
                    check_step2(ctx, normalizer, case, legacy, rust_result)
                else:
                    compare(ctx, normalizer, case, legacy, rust_result)
            except Failure as exc:
                failed.append(case.id)
                print(f"不合格 {case.id}: {exc}", flush=True)
                continue
            passed += 1
            print(f"合格 {case.id}", flush=True)
        restore_and_remove(ctx.root)
    groups = {}
    for case in cases:
        groups[case.group] = groups.get(case.group, 0) + 1
    elapsed = time.monotonic() - started
    limits = {name: resource.getrlimit(getattr(resource, name))
              for name in ("RLIMIT_AS", "RLIMIT_NOFILE", "RLIMIT_CPU", "RLIMIT_STACK", "RLIMIT_FSIZE")}
    print("受け継いだ資源の上限(比較器は設定しない。RLIMIT_FSIZE はケースが与えるときだけ子で設定): " +
          ", ".join(f"{name}={value}" for name, value in limits.items()))
    print(f"R4U の比較: 合格 {passed} 件 / 全 {len(cases)} 件(" +
          "・".join(f"{name} {count}" for name, count in sorted(groups.items())) +
          f")/ 旧 Python {python.version}({python.path})/ {elapsed:.1f} 秒")
    if failed or passed != len(cases) or passed == 0:
        print("不合格の ID: " + ", ".join(failed))
        return 1
    return 0


def main_measure(out):
    python = find_python()
    normalizer = Normalizer(python)
    with tempfile.TemporaryDirectory(prefix="r4u-step2-") as temp:
        temp = os.path.realpath(temp)
        ctx = Context(python, None, build_shim(temp), temp)
        entries = step2_measure(ctx, normalizer)
        restore_and_remove(ctx.root)
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
    group.add_argument("--measure-step2", action="store_true")
    parser.add_argument("--out", type=Path, default=FIXTURES / "step2.json")
    parser.add_argument("--only", action="append", help="調べるための部分実行(ケースの ID。合格にはしない)")
    args = parser.parse_args(argv)
    try:
        if args.measure_step2:
            return main_measure(args.out)
        return main_compare(args.fixture, args.only)
    except Failure as exc:
        print(f"不合格: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
