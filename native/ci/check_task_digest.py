#!/usr/bin/env python3
"""task-digest の旧版・固定期待値・Rust の必須比較。"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import pty
import runpy
import socket
import selectors
import stat
import subprocess
import struct
import sys
import tempfile
import termios
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "plugins/dev-workflow/skills/create-task/scripts/task-digest.py"
LEGACY_SPECIAL_SHA256 = "2ed5fb5e4fc70c1255e2622b0e9de65eef7d70fae4f6938a4507d2b7621c98c0"
SPECIAL_EMPTY_STDIN = "empty-nonblocking-stdin"
SPECIAL_CLOSED_STDOUT = "unbuffered-closed-stdout"
SPECIAL_FULL_STDOUT = "unbuffered-dev-full"
SPECIAL_IDS = (SPECIAL_EMPTY_STDIN, SPECIAL_CLOSED_STDOUT, SPECIAL_FULL_STDOUT)
SPECIAL_ENCODINGS = ("utf-8:strict", "utf-8:surrogateescape", "ascii:strict")
CLOSED_STDERR_CASES = ("rejection-closed-stderr", "both-closed", "empty-nonblocking-closed-stderr")
CLOSED_STDERR_BUFFERING = ("buffered", "unbuffered")
SPECIAL_CONTROLS = ("nonblocking-data", "nonblocking-empty-eof", "nonblocking-data-eof",
                    "unbuffered-open-stdout", "buffered-closed-stdout", "file-read-would-block",
                    "unbuffered-writer-boundaries")
sys.path.insert(0, str(Path(__file__).with_name("task_digest_fixtures")))
from cases import (Case, cases, load_existing, BASE_BODY, headings_error,
                   UNBUFFERED_CASES, UNBUFFERED_STREAM_IDS)


class Failure(RuntimeError):
    pass


@dataclass(frozen=True)
class Completed:
    code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


def check_result(name, got, expected):
    if got.timed_out:
        raise Failure(f"{name}: 時間切れ")
    if type(got.code) is not int:
        raise Failure(f"{name}: 未実行")
    if (got.code, got.stdout, got.stderr) != expected:
        raise Failure(f"{name}: 期待 {expected!r}、実際 {(got.code, got.stdout, got.stderr)!r}")


def special_encode(text, encoding):
    codec = "ascii" if encoding.startswith("ascii:") else "utf-8"
    return text.encode(codec, "backslashreplace")


def validate_legacy_special_source(path):
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != LEGACY_SPECIAL_SHA256:
        raise Failure("特殊I/O比較の旧ソースが固定した版と違う")
    lines = source.decode("utf-8").splitlines()
    required = {114: '        result = digest(data.decode("utf-8"))',
                124: '    print(result)', 129: '    sys.exit(main())'}
    if any(lines[number - 1] != text for number, text in required.items()):
        raise Failure("特殊I/O比較の旧ソース行が違う")


def special_expectations(case_id, encoding, path=LEGACY):
    if encoding not in SPECIAL_ENCODINGS:
        raise Failure("特殊I/O比較の文字コードが未定義: " + str(encoding))
    if case_id == SPECIAL_EMPTY_STDIN:
        number, statement = 114, 'result = digest(data.decode("utf-8"))'
        exception = "AttributeError: 'NoneType' object has no attribute 'decode'"
        diagnostic = "算出できない: 非同期の標準入力にデータがない\n"
    elif case_id == SPECIAL_CLOSED_STDOUT:
        number, statement = 124, 'print(result)'
        exception = "BrokenPipeError: [Errno 32] Broken pipe"
        diagnostic = "算出できない: 標準出力のパイプが閉じている\n"
    elif case_id == SPECIAL_FULL_STDOUT:
        number, statement = 124, 'print(result)'
        exception = "OSError: [Errno 28] No space left on device"
        diagnostic = "算出できない: 標準出力の空き容量がない\n"
    else:
        raise Failure("承認されていない特殊I/O条件: " + case_id)
    traceback = ("Traceback (most recent call last):\n"
                 f'  File "{path}", line 129, in <module>\n'
                 "    sys.exit(main())\n"
                 f'  File "{path}", line {number}, in main\n'
                 f"    {statement}\n{exception}\n")
    return ((1, b"", special_encode(traceback, encoding)),
            (1, b"", special_encode(diagnostic, encoding)))


def check_special_pair(case_id, legacy, native, encoding):
    old_expected, native_expected = special_expectations(case_id, encoding)
    check_result(case_id + "/legacy", legacy, old_expected)
    check_result(case_id + "/rust", native, native_expected)


def check_special_counts(counts, controls):
    if counts.keys() != set(SPECIAL_IDS) or controls.keys() != set(SPECIAL_CONTROLS):
        raise Failure("特殊I/O条件または対照の比較が欠けている")
    expected = dict.fromkeys(SPECIAL_IDS, len(SPECIAL_ENCODINGS))
    if not sys.platform.startswith("linux"):
        expected[SPECIAL_FULL_STDOUT] = 0
    if any(type(n) is not int or n != expected[key] for key, n in counts.items()) or any(
            type(n) is not int or n != len(SPECIAL_ENCODINGS) for n in controls.values()):
        raise Failure("特殊I/O条件または対照の必須文字コードが未実行または件数不正")


def check_closed_stderr_coverage(covered):
    required = {(case, buffering, encoding) for case in CLOSED_STDERR_CASES
                for buffering in CLOSED_STDERR_BUFFERING for encoding in SPECIAL_ENCODINGS}
    if covered != required:
        raise Failure("切断stderrの必須比較18条件が不足または余分")


def record_coverage(covered, key):
    if key in covered:
        raise Failure("専用I/O比較の重複: " + repr(key))
    covered.add(key)


def env_stream_required():
    targets = ("closed-stdout", *CLOSED_STDERR_CASES)
    if sys.platform.startswith("linux"):
        targets += ("full-stdout",)
    return {(name, target, encoding) for name in UNBUFFERED_STREAM_IDS
            for target in targets for encoding in SPECIAL_ENCODINGS}


def check_env_coverage(flags, streams):
    required = {name for name, _, _ in UNBUFFERED_CASES}
    if len(required) != len(UNBUFFERED_CASES) or flags != required:
        raise Failure("環境flagの固定表が重複、未実行または余分")
    if len(set(UNBUFFERED_STREAM_IDS)) != len(UNBUFFERED_STREAM_IDS) or not set(UNBUFFERED_STREAM_IDS) <= required:
        raise Failure("実stdout/stderr代表値の固定表が重複または余分")
    if streams != env_stream_required():
        raise Failure("環境値ごとの実stdout/stderr比較が未実行または余分")


def env_with_value(env, value):
    if value is not None:
        env["PYTHONUNBUFFERED"] = os.fsdecode(value)
    return env


def strict_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重複キー: " + key)
        result[key] = value
    return result


def check_record(name, got, expected):
    check_result(name, got, (0, got.stdout, b""))
    try:
        text = got.stdout.decode("utf-8")
        value, end = json.JSONDecoder(object_pairs_hook=unique_object).raw_decode(text)
        # 検査用JSONは1行・末尾LF1個だけ。内部の空白とキーの順だけを許す。
        if text[end:] != "\n" or "\n" in text[:end]:
            raise ValueError("JSONの前後の余分な出力")
    except (ValueError, UnicodeDecodeError) as exc:
        raise Failure(f"{name}: JSON ではない") from exc
    # 未知の欄も、欠けた欄も許さない。余分な出力をJSONとして読み飛ばさない。
    if not strict_equal(value, expected):
        raise Failure(f"{name}: 期待 {expected!r}、実際 {value!r}")


def require_cases(found):
    if not found:
        raise Failure("比較ケースが0件")
    if len({v.name for v in found}) != len(found):
        raise Failure("比較ケースの重複")


def run(argv, *, data=b"", cwd=None, env=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    try:
        result = subprocess.run(argv, input=data, stdout=stdout, stderr=stderr,
                                cwd=cwd, env=env, timeout=15, check=False)
    except subprocess.TimeoutExpired as exc:
        return Completed(None, exc.stdout or b"", exc.stderr or b"", True)
    except OSError as exc:
        raise Failure(f"比較対象を起動できない: {argv[0]!r}: {exc}") from exc
    return Completed(result.returncode, result.stdout or b"", result.stderr or b"")


def run_nonblocking(argv, *, data, writer_open, cwd, env, stderr=subprocess.PIPE):
    read_end, write_end = os.pipe()
    try:
        os.set_blocking(read_end, False)
        if data and os.write(write_end, data) != len(data):
            raise Failure("非同期stdinの固定本文を準備できない")
        if not writer_open:
            os.close(write_end)
            write_end = None
        try:
            result = subprocess.run(argv, stdin=read_end, stdout=subprocess.PIPE,
                                    stderr=stderr, cwd=cwd, env=env, timeout=15, check=False)
        except subprocess.TimeoutExpired as exc:
            return Completed(None, exc.stdout or b"", exc.stderr or b"", True)
        except OSError as exc:
            raise Failure(f"非同期stdinの比較を起動できない: {exc}") from exc
        return Completed(result.returncode, result.stdout or b"", result.stderr or b"")
    finally:
        os.close(read_end)
        if write_end is not None:
            os.close(write_end)


def snapshot(root):
    found = {}

    def visit(path):
        relative = os.fsencode(path.relative_to(root))
        try:
            st = path.lstat()
            attributes = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            kind = stat.S_IFMT(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                body = os.fsencode(os.readlink(path))
            elif stat.S_ISREG(st.st_mode):
                try:
                    body = path.read_bytes()
                except PermissionError:
                    body = ("unreadable", errno.EACCES)
            else:
                body = None
            found[relative] = (attributes, body)
            if kind == stat.S_IFDIR:
                try:
                    children = sorted(path.iterdir())
                except PermissionError:
                    children = []
                for child in children:
                    visit(child)
        except PermissionError as exc:
            found[relative] = ("unsearchable", exc.errno)
    visit(root)
    return found


def check_unchanged(name, before, after):
    if before != after:
        raise Failure(f"{name}: 入力の本文・種類・mode・リンク先・属性が変わった")


def check_rename_boundary(name, before, boundary, relative):
    """許可する変更は同一フォルダ内の2回のrenameだけ。"""
    original = relative + b"/input"
    replacement = relative + b"/replacement"
    opened_old = relative + b"/opened-old"
    required = {original, replacement, relative}
    if not required <= before.keys() or opened_old in before:
        raise Failure(f"{name}: 差し替え前の入力が不足または余分")
    expected_keys = (before.keys() - {replacement}) | {opened_old}
    if boundary.keys() != expected_keys:
        raise Failure(f"{name}: rename以外のパス変更")
    for path, observed in boundary.items():
        source = original if path == opened_old else replacement if path == original else path
        expected = before[source]
        if path in (original, opened_old):
            # rename自体で変わり得るctime以外は、移した同じ実体と完全一致させる。
            matches = expected[0][:5] == observed[0][:5] and expected[1] == observed[1]
        elif path == relative:
            # 直下の名前を変えた親だけ、size・mtime・ctimeの変化を許す。
            matches = expected[0][:3] == observed[0][:3] and expected[1] == observed[1]
        else:
            # 無関係な入力のctimeも検査する。
            matches = expected == observed
        if not matches:
            raise Failure(f"{name}: renameに不要な本文または属性の変更: {os.fsdecode(path)}")


def read_checkpoint(process):
    """改行までの待機にも期限を置く。末尾の実結果を先読みしない。"""
    deadline = time.monotonic() + 15
    notice = bytearray()
    with selectors.DefaultSelector() as poll:
        poll.register(process.stdout, selectors.EVENT_READ)
        while not notice.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not poll.select(remaining):
                raise Failure("差し替え境界の通知が時間切れ")
            byte = os.read(process.stdout.fileno(), 1)
            if not byte:
                raise Failure("差し替え境界を通知せず終了した")
            notice.extend(byte)
            if len(notice) > 128:
                raise Failure("差し替え境界の通知が長すぎる")
    check_record("race-checkpoint", Completed(0, bytes(notice), b""), {"checkpoint": "renamed"})
    if process.poll() is not None:
        raise Failure("差し替え境界の確認応答を待っていない")


def run_race(argv, *, root, place, env):
    before = snapshot(root)
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=root, env=env)
    except OSError as exc:
        raise Failure(f"差し替え比較を起動できない: {exc}") from exc
    with process:
        try:
            read_checkpoint(process)
            boundary = snapshot(root)
            check_rename_boundary("race-boundary", before, boundary,
                                  os.fsencode(place.relative_to(root)))
            try:
                stdout, stderr = process.communicate(input=b"continue\n", timeout=15)
            except subprocess.TimeoutExpired as exc:
                raise Failure("差し替え境界の確認後に時間切れ") from exc
            # rename後の正確なctimeを含め、以後は全属性の一致を要求する。
            check_unchanged("race-after-boundary", boundary, snapshot(root))
            return Completed(process.returncode, stdout, stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


DRIVER = r'''
import io,json,os,runpy,sys
from unittest.mock import patch
script,mode,*args=sys.argv[1:]
module=runpy.run_path(script,run_name="task_digest_comparison")
sys.argv=["task-digest.py",*args]
if mode=="cli":
    sys.exit(module["main"]())
elif mode=="reader":
    if args[0]=="would-block":
        import errno
        def blocked(path):
            raise BlockingIOError(errno.EAGAIN,os.strerror(errno.EAGAIN))
        sys.argv=["task-digest.py","synthetic-regular"]
        with patch.dict(module["main"].__globals__,{"read_path":blocked}):
            sys.exit(module["main"]())
    class Source:
        def __init__(self):
            self.buffer=self
        def read(self):
            if args[0]=="fail":
                raise OSError(5,os.strerror(5))
            return saved
    saved=sys.stdin.buffer.read()
    sys.stdin=Source()
    sys.argv=["task-digest.py","-"]
    sys.exit(module["main"]())
elif mode=="race":
    from pathlib import Path
    path=Path(args[0]); boundary=args[1]; fired=[0]
    original=getattr(os,boundary)
    before={int(n) for n in os.listdir("/dev/fd")}
    def wrapped(value,*a,**kw):
        result=original(value,*a,**kw)
        if os.fspath(value)==str(path) and not fired[0]:
            path.rename(path.with_name("opened-old"))
            path.with_name("replacement").rename(path)
            fired[0]+=1
            sys.__stdout__.write('{"checkpoint":"renamed"}\n')
            sys.__stdout__.flush()
            if sys.stdin.buffer.readline()!=b"continue\n": raise AssertionError("checkpoint not acknowledged")
        return result
    out,err=io.StringIO(),io.StringIO()
    sys.argv=["task-digest.py",str(path)]
    with patch.object(os,boundary,wrapped),patch.object(sys,"stdout",out),patch.object(sys,"stderr",err):
        rc=module["main"]()
    after={int(n) for n in os.listdir("/dev/fd")}
    if before!=after or fired[0]!=1: raise AssertionError("fd leak or hook not reached")
    print(json.dumps({"changed":fired[0],"fd_closed":True,"code":rc,"stdout_hex":out.getvalue().encode().hex(),"stderr_hex":err.getvalue().encode().hex()}))
else: raise AssertionError(mode)
'''


class Comparison:
    def __init__(self, binary):
        self.binary = Path(binary).resolve()
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise Failure("Rust の専用実行物が不在または実行不能")
        if not LEGACY.is_file():
            raise Failure("旧 Python がない")
        self.legacy = runpy.run_path(str(LEGACY), run_name="task_digest_functions")
        self.counts = {"normalized": 0, "stdin": 0, "file": 0, "arguments": 0, "pty": 0,
                       "filesystem": 0, "race": 0, "reader": 0, "output": 0, "closed_stderr": 0}
        self.skips = []
        self.covered_external = set()
        self.special_counts = dict.fromkeys(SPECIAL_IDS, 0)
        self.special_controls = dict.fromkeys(SPECIAL_CONTROLS, 0)
        self.closed_stderr_coverage = set()
        self.synthetic_outputs = 0
        self.env_coverage = set()
        self.env_stream_coverage = set()

    @staticmethod
    def environment(encoding="utf-8:strict", columns="80"):
        result = os.environ.copy()
        result.update(PYTHONDONTWRITEBYTECODE="1", LC_ALL="C", PYTHONUTF8="1")
        # 通常比較は明示的にbuffered。特殊IOの比較を済ませた意味にはしない。
        result.pop("PYTHONUNBUFFERED", None)
        result.pop("PYTHONINTMAXSTRDIGITS", None)
        if columns is None:
            result.pop("COLUMNS", None)
        else:
            result["COLUMNS"] = columns
        if encoding is None:
            result.pop("PYTHONIOENCODING", None)
        else:
            result["PYTHONIOENCODING"] = encoding
        return result

    def command(self, side, mode, args):
        if side == "legacy":
            return [sys.executable, "-B", "-c", DRIVER, str(LEGACY), mode, *args]
        return [str(self.binary), "--task-digest-fixture", mode, *args]

    def both(self, name, mode, args, expected, *, data=b"", cwd=None, root=None, encoding="utf-8:strict", columns="80", record=False):
        env = self.environment(encoding, columns)
        for side in ("legacy", "rust"):
            before = snapshot(root) if root else None
            got = run(self.command(side, mode, args), data=data, cwd=cwd, env=env)
            if root:
                check_unchanged(name + "/" + side, before, snapshot(root))
            (check_record if record else check_result)(name + "/" + side, got, expected)

    def normalized(self, case):
        try:
            text = case.data.decode("utf-8")
            body = "\n".join(self.legacy["body_lines"](text))
            value = {"normalized_hex": body.encode().hex(), "digest": self.legacy["digest"](text), "error": None}
        except UnicodeDecodeError as exc:
            value = {"normalized_hex": None, "digest": None, "error": "UTF-8 として読めない: " + str(exc)}
        except self.legacy["DigestError"] as exc:
            value = {"normalized_hex": None, "digest": None, "error": str(exc)}
        if value != case.record:
            raise Failure(f"{case.name}/legacy-functions: 固定本文または値と不一致: {value!r}")
        got = run(self.command("rust", "normalize", []), data=case.data, env=self.environment())
        check_record(case.name + "/rust-functions", got, case.record)
        self.counts["normalized"] += 1

    def normal_cases(self, root, selected):
        path = root / "input.md"
        for case in selected:
            self.normalized(case)
            path.write_bytes(case.data)
            self.both(case.name + "/stdin", "cli", ["-"], case.cli, data=case.data, cwd=root, root=root)
            self.counts["stdin"] += 1
            self.both(case.name + "/file", "cli", [str(path)], case.cli, cwd=root, root=root)
            self.counts["file"] += 1
        self.covered_external.add("test_stdin")

    def invalid_utf8(self, root):
        base = load_existing(ROOT)[0].BASE.encode()
        probes = [
            (b"\xff", 0, 1, "invalid start byte"), (b"\x80", 0, 1, "invalid start byte"),
            (b"\xc0\xaf", 0, 1, "invalid start byte"), (b"\xf5\x80\x80\x80", 0, 1, "invalid start byte"),
            (b"\xe2", 0, 1, "unexpected end of data"), (b"\xe2\x82", 0, 2, "unexpected end of data"),
            (b"\xf0\x90\x80", 0, 3, "unexpected end of data"),
            (b"\xc2A", 0, 1, "invalid continuation byte"), (b"\xe2\x82A", 0, 2, "invalid continuation byte"),
            (b"\xe0\x80\x80", 0, 1, "invalid continuation byte"), (b"\xed\xa0\x80", 0, 1, "invalid continuation byte"),
            (b"\xf4\x90\x80\x80", 0, 1, "invalid continuation byte"),
            ("あ".encode() + b"\xff", 3, 4, "invalid start byte"),
            (base + b"\xff\xfe", len(base), len(base) + 1, "invalid start byte"),
        ]
        selected = []
        for n, (data, start, end, reason) in enumerate(probes):
            if end == start + 1:
                detail = f"byte 0x{data[start]:02x} in position {start}"
            else:
                detail = f"bytes in position {start}-{end - 1}"
            message = f"UTF-8 として読めない: 'utf-8' codec can't decode {detail}: {reason}"
            selected.append(Case(f"invalid-utf8-{n}", data, None, message, None))
        self.normal_cases(root, selected)
        self.covered_external.add("test_invalid_inputs")

    def arguments(self, root, base):
        success = (0, b"sha256:21eefa756f838e54\n", b"")
        required = "the following arguments are required: path"
        tests = [([], required), (["-h"], required), (["--help"], required), (["--unknown"], required),
                 (["--"], required), (["-h", "-"], "unrecognized arguments: -h"),
                 (["--help", "-"], "unrecognized arguments: --help"),
                 (["-", "extra"], "unrecognized arguments: extra"),
                 (["--unknown", "-", "extra"], "unrecognized arguments: --unknown extra"),
                 (["-", "extra", "--"], "unrecognized arguments: extra --"),
                 (["-", "--", "extra"], "unrecognized arguments: extra"),
                 (["-", "-\udcff"], "unrecognized arguments: -\udcff"),
                 (["-\udcff"], required)]
        for columns in ("80", "23", "1", "0", "bad", "+８０", "2_3", "-1"):
            usage = "usage: task-digest.py path\n" if columns not in ("23", "1", "2_3") else "usage: task-digest.py\n       path\n"
            for args, message in tests:
                for encoding in ("utf-8:strict", "utf-8:surrogateescape", "ascii:strict"):
                    diagnostic = (usage + "task-digest.py: error: " + message + "\n").encode("ascii" if encoding.startswith("ascii") else "utf-8", "backslashreplace")
                    self.both(f"args-{args!r}-{columns}-{encoding}", "cli", args, (2, b"", diagnostic), data=base, cwd=root, root=root, columns=columns, encoding=encoding)
                    self.counts["arguments"] += 1
        for name in ("-1", "-.5", "-1.25", "-١٢", "-١.٢٥", "-.٥", "-1\n", "-١\n", "- bad", "-"):
            (root / name).write_bytes(base)
            args = ["./-"] if name == "-" else [name]
            self.both("positional-" + repr(name), "cli", args, success, cwd=root, root=root)
            self.counts["arguments"] += 1
        for args in (["--", "-"], ["--", "--"], ["-", "--"]):
            if args == ["--", "--"]:
                (root / "--").write_bytes(base)
            self.both("separator-" + repr(args), "cli", args, success, data=base, cwd=root, root=root)
            self.counts["arguments"] += 1
        self.os_case("empty-argument", [""], root, errno.ENOENT)

    def terminal_widths(self, root):
        required = "task-digest.py: error: the following arguments are required: path\n"
        # stdoutの端末幅を使う。不正・未指定・非正数のCOLUMNSも独立に測る。
        ordinary = (
            (23, None, True), (23, "bad", True), (23, "", True),
            (23, "0", True), (23, "-1", True), (23, "\x1c80", True),
            (23, "80", False), (80, None, False), (80, "23", True),
        )
        # int()の上限は符号/underscoreを数えず、Unicode数字と先頭0は数える。
        boundaries = (
            (None, "1" * 4300, False), (None, "1" * 4301, True),
            (None, "+" + "1" * 4300, False), (None, "+" + "1" * 4301, True),
            (None, "1_" * 4299 + "1", False), (None, "1_" * 4300 + "1", True),
            (None, "١" * 4300, False), (None, "١" * 4301, True),
            (None, "0" * 4298 + "80", False), (None, "0" * 4299 + "80", True),
            ("0", "1" * 4301, False), ("-0", "1" * 4301, False),
            ("640", "1" * 640, False), ("640", "1" * 641, True),
            (" +000640", "1_" * 639 + "1", False),
            (" +000640", "١" * 641, True),
            ("5000", "1" * 5000, False), ("5000", "1" * 5001, True),
        )
        tests = [(width, columns, wrapped, None) for width, columns, wrapped in ordinary]
        tests += [(23, columns, wrapped, limit) for limit, columns, wrapped in boundaries]
        for terminal_width, columns, wrapped, limit in tests:
            usage = "usage: task-digest.py\n       path\n" if wrapped else "usage: task-digest.py path\n"
            expected = (2, b"", (usage + required).encode())
            for side in ("legacy", "rust"):
                master, slave = pty.openpty()
                try:
                    fcntl.ioctl(slave, termios.TIOCSWINSZ,
                                struct.pack("HHHH", 24, terminal_width, 0, 0))
                    env = self.environment(columns=columns)
                    env["LINES"] = "24"
                    if limit is not None:
                        env["PYTHONINTMAXSTRDIGITS"] = limit
                    before = snapshot(root)
                    got = run(self.command(side, "cli", []), cwd=root, env=env, stdout=slave)
                    check_unchanged("pty-" + side, before, snapshot(root))
                    check_result(f"pty-{side}-{terminal_width}-{columns!r}", got, expected)
                    os.set_blocking(master, False)
                    try:
                        stdout = os.read(master, 4096)
                    except BlockingIOError:
                        stdout = b""
                    if stdout:
                        raise Failure(f"pty-{side}: 余分な標準出力 {stdout!r}")
                    self.counts["pty"] += 1
                finally:
                    os.close(slave)
                    os.close(master)

    def os_case(self, name, args, root, number, encoding="utf-8:strict"):
        message = f"算出できない: ファイルを読めない: [Errno {number}] {os.strerror(number)}: {args[-1]!r}\n"
        expected = (1, b"", message.encode("ascii" if encoding.startswith("ascii") else "utf-8", "backslashreplace"))
        self.both(name, "cli", args, expected, cwd=root, root=root, encoding=encoding)
        self.counts["filesystem"] += 1

    def filesystem(self, root, base):
        success = (0, b"sha256:21eefa756f838e54\n", b"")
        regular = root / "regular.md"
        regular.write_bytes(base)
        directory = root / "parent"
        directory.mkdir()
        (directory / "input").write_bytes(base)
        (root / "regular.link").symlink_to(regular)
        (root / "parent.link").symlink_to(directory, target_is_directory=True)
        for name in ("regular.md", "regular.link", "parent.link/input", "parent/../regular.md", "./regular.md"):
            self.both("regular-" + name, "cli", [name], success, cwd=root, root=root)
            self.counts["filesystem"] += 1
        self.covered_external.add("test_regular_file_and_symlink_keep_the_known_value")
        fifo = root / "fifo"
        os.mkfifo(fifo)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(root / "socket"))
        try:
            for name in ("fifo", "socket", "parent", "/dev/null"):
                if name == "/dev/null" and not Path(name).is_char_device():
                    raise Failure("文字deviceの必須条件がない")
                target = root / name if not name.startswith("/") else Path(name)
                link = root / (target.name + ".nonregular.link")
                link.symlink_to(target)
                for path in (str(target), str(link)):
                    self.both("nonregular-" + path, "cli", [path], (1, b"", "算出できない: 入力パスが通常ファイルでない\n".encode()), cwd=root, root=root)
                    self.counts["filesystem"] += 1
        finally:
            server.close()
        self.covered_external.add("test_non_regular_paths_are_rejected_without_waiting")
        self.os_case("missing", ["無い.md"], root, errno.ENOENT)
        (root / "broken").symlink_to("missing")
        self.os_case("broken-link", ["broken"], root, errno.ENOENT)
        (root / "cycle").symlink_to("cycle")
        self.os_case("cycle", ["cycle"], root, errno.ELOOP)
        self.os_case("not-directory", ["regular.md/child"], root, errno.ENOTDIR)
        self.os_case("too-long", ["n" * 4097], root, errno.ENAMETOOLONG)
        denied = root / "denied"
        denied.write_bytes(base)
        denied.chmod(0)
        try:
            self.os_case("permission", ["denied"], root, errno.EACCES)
        finally:
            denied.chmod(0o600)
        directory.chmod(0o600)
        try:
            self.os_case("unsearchable", ["parent/input"], root, errno.EACCES)
        finally:
            directory.chmod(0o700)
        raw = root / os.fsdecode(b"raw-\xff-'\n")
        try:
            raw.write_bytes(base)
        except OSError as exc:
            if sys.platform != "darwin" or exc.errno not in (errno.EINVAL, errno.EILSEQ):
                raise
            self.skips.append(f"Macの非UTF-8実在名: {exc}")
        else:
            for encoding in ("utf-8:strict", "utf-8:surrogateescape", "ascii:strict"):
                self.both("raw-existing-" + encoding, "cli", [str(raw)], success, cwd=root, root=root, encoding=encoding)
                self.counts["filesystem"] += 1
        for encoding in ("utf-8:strict", "utf-8:surrogateescape", "ascii:strict"):
            self.os_case("raw-missing-" + encoding, [os.fsdecode(b"missing-\xff-'\n")], root, errno.ENOENT, encoding)
            self.os_case("quote-missing-" + encoding, ["'\"\t\x1c\u2028\U0001f979"], root, errno.ENOENT, encoding)

    def races(self, root, base):
        for boundary, kind in (("stat", "fifo"), ("open", "fifo"), ("stat", "file"), ("open", "file")):
            # 両側を同じ物理パスで順に測る。各回の交換は期待した変更として別に検査する。
            place = root / "race"
            place.mkdir()
            target = place / "input"
            replacement = place / "replacement"
            new = "NEW\n## 追加修正記録\n".encode()
            new_digest = "sha256:" + __import__("hashlib").sha256(b"NEW").hexdigest()[:16]
            stdout = b"sha256:21eefa756f838e54\n" if boundary == "open" else (new_digest + "\n").encode()
            rc, err = (1, "算出できない: 入力パスが通常ファイルでない\n".encode()) if boundary == "stat" and kind == "fifo" else (0, b"")
            if rc:
                stdout = b""
            expected = {"changed": 1, "fd_closed": True, "code": rc, "stdout_hex": stdout.hex(), "stderr_hex": err.hex()}
            for side in ("legacy", "rust"):
                target.write_bytes(base)
                if kind == "fifo":
                    os.mkfifo(replacement)
                else:
                    replacement.write_bytes(new)
                got = run_race(self.command(side, "race", [str(target), boundary]),
                               root=root, place=place, env=self.environment())
                check_record(f"race-{boundary}-{kind}-{side}", got, expected)
                if (place / "opened-old").read_bytes() != base or replacement.exists():
                    raise Failure("差し替え後の元本文または残存パスが不一致")
                if kind == "fifo":
                    if not stat.S_ISFIFO(target.lstat().st_mode):
                        raise Failure("FIFOの差し替えが未実行")
                elif target.read_bytes() != new:
                    raise Failure("通常ファイルの差し替えが未実行")
                target.unlink()
                (place / "opened-old").unlink()
                self.counts["race"] += 1
            place.rmdir()
        self.covered_external.update(("test_replacement_before_open_is_rejected_without_waiting", "test_replacement_after_open_reads_the_opened_regular_file"))

    def readers(self, root, base):
        for mode, expected in (("short", (0, b"sha256:21eefa756f838e54\n", b"")), ("fail", (1, b"", f"算出できない: ファイルを読めない: [Errno 5] {os.strerror(5)}\n".encode()))):
            self.both("reader-" + mode, "reader", [mode], expected, data=base, cwd=root, root=root)
            self.counts["reader"] += 1

    def outputs(self, root, base):
        for encoding in (None, "utf-8:strict", "utf-8:surrogateescape", "ascii:strict"):
            encoding_name = "ascii" if encoding and encoding.startswith("ascii") else "utf-8"
            diagnostic = (f"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='{encoding_name}'>\nBrokenPipeError: [Errno 32] Broken pipe\n").encode()
            for side in ("legacy", "rust"):
                read_end, write_end = os.pipe()
                os.close(read_end)
                try:
                    got = run(self.command(side, "cli", ["-"]), data=base, cwd=root, env=self.environment(encoding), stdout=write_end)
                    check_result("closed-stdout-" + side + "-" + str(encoding), got, (120, b"", diagnostic))
                finally:
                    os.close(write_end)
                self.counts["output"] += 1

                read_end, write_end = os.pipe()
                os.close(read_end)
                try:
                    got = run(self.command(side, "cli", ["-"]), data=b"", cwd=root, env=self.environment(encoding), stderr=write_end)
                    check_result("closed-stderr-" + side, got, (120, b"", b""))
                finally:
                    os.close(write_end)
                self.counts["output"] += 1
                readers, writers = [], []
                for _ in range(2):
                    r, w = os.pipe(); readers.append(r); writers.append(w)
                for r in readers:
                    os.close(r)
                try:
                    got = run(self.command(side, "cli", ["-"]), data=base, cwd=root, env=self.environment(encoding), stdout=writers[0], stderr=writers[1])
                    check_result("both-closed-" + side, got, (120, b"", b""))
                finally:
                    for w in writers:
                        os.close(w)
                self.counts["output"] += 1
                if sys.platform.startswith("linux"):
                    with open("/dev/full", "wb", buffering=0) as full:
                        got = run(self.command(side, "cli", ["-"]), data=base, cwd=root, env=self.environment(encoding), stdout=full)
                    detail = f"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='{encoding_name}'>\nOSError: [Errno 28] No space left on device\n".encode()
                    check_result("full-" + side, got, (120, b"", detail))
                    self.counts["output"] += 1
        if not sys.platform.startswith("linux"):
            self.skips.append("/dev/full: Linux専用")
        for stage in ("write", "flush"):
            for stream in ("stdout", "stderr"):
                source = b"ok\n" if stream == "stdout" else b"\xe7\xae\x97\xe5\x87\xba\xe3\x81\xa7\xe3\x81\x8d\xe3\x81\xaa\xe3\x81\x84: bad\n"
                diagnostic = b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\nBrokenPipeError: [Errno 32] Broken pipe\n" if stream == "stdout" else b""
                expected = {"code": 120, "written_hex": source.hex() if stage == "flush" else "", "stdout_hex": "", "stderr_hex": diagnostic.hex()}
                got = run(self.command("rust", "writer", [stage, stream]), env=self.environment())
                check_record(f"writer-{stage}-{stream}", got, expected)
                self.synthetic_outputs += 1

    def special_command(self, side):
        # 旧ソースの<module>とmainの2段の経路をそのまま観測する。
        # 通常比較用の-c driverを挟んで例外の経路を増やさない。
        if side == "legacy":
            return [sys.executable, "-B", str(LEGACY), "-"]
        return self.command(side, "cli", ["-"])

    def closed_stderr(self, root, base):
        # 診断を観測できない実パイプも、双方を固定終了値・空出力と比較する。
        # 合成writerの結果だけを旧版との互換性の根拠にしない。
        for case in CLOSED_STDERR_CASES:
            for buffering in CLOSED_STDERR_BUFFERING:
                expected = (1 if buffering == "unbuffered" else 120, b"", b"")
                for encoding in SPECIAL_ENCODINGS:
                    env = self.environment(encoding)
                    if buffering == "unbuffered":
                        env["PYTHONUNBUFFERED"] = "1"
                    for side in ("legacy", "rust"):
                        writers = []
                        before = snapshot(root)
                        try:
                            read_end, stderr = os.pipe()
                            writers.append(stderr)
                            os.close(read_end)
                            stdout = subprocess.PIPE
                            if case == "both-closed":
                                read_end, stdout = os.pipe()
                                writers.append(stdout)
                                os.close(read_end)
                            command = self.special_command(side)
                            if case == "empty-nonblocking-closed-stderr":
                                got = run_nonblocking(command, data=b"", writer_open=True,
                                                      cwd=root, env=env, stderr=stderr)
                            else:
                                data = base if case == "both-closed" else b""
                                got = run(command, data=data, cwd=root, env=env,
                                          stdout=stdout, stderr=stderr)
                        finally:
                            for descriptor in writers:
                                os.close(descriptor)
                        check_unchanged(f"{case}/{buffering}/{encoding}/{side}", before, snapshot(root))
                        check_result(f"{case}/{buffering}/{encoding}/{side}", got, expected)
                    key = (case, buffering, encoding)
                    if key in self.closed_stderr_coverage:
                        raise Failure("切断stderrの比較条件が重複")
                    self.closed_stderr_coverage.add(key)
                    self.counts["closed_stderr"] += 1
        check_closed_stderr_coverage(self.closed_stderr_coverage)

    def special_io(self, root, base):
        validate_legacy_special_source(LEGACY)
        success = (0, b"sha256:21eefa756f838e54\n", b"")
        empty_eof = (1, b"", ("算出できない: " + headings_error(0) + "\n").encode())
        for encoding in SPECIAL_ENCODINGS:
            results = {}
            for side in ("legacy", "rust"):
                before = snapshot(root)
                results[side] = run_nonblocking(self.special_command(side), data=b"", writer_open=True,
                                               cwd=root, env=self.environment(encoding))
                check_unchanged("special-empty-stdin-" + side, before, snapshot(root))
            check_special_pair(SPECIAL_EMPTY_STDIN, results["legacy"], results["rust"], encoding)
            self.special_counts[SPECIAL_EMPTY_STDIN] += 1

            for control, data, writer_open, expected in (
                ("nonblocking-data", base, True, success),
                ("nonblocking-empty-eof", b"", False, empty_eof),
                ("nonblocking-data-eof", base, False, success),
            ):
                if encoding.startswith("ascii"):
                    expected = (expected[0], expected[1], expected[2].decode().encode("ascii", "backslashreplace"))
                for side in ("legacy", "rust"):
                    before = snapshot(root)
                    got = run_nonblocking(self.special_command(side), data=data, writer_open=writer_open,
                                          cwd=root, env=self.environment(encoding))
                    check_unchanged(control + "/" + side, before, snapshot(root))
                    check_result(control + "/" + side, got, expected)
                self.special_controls[control] += 1

            results = {}
            for side in ("legacy", "rust"):
                env = self.environment(encoding)
                env["PYTHONUNBUFFERED"] = "1"
                read_end, write_end = os.pipe()
                os.close(read_end)
                before = snapshot(root)
                try:
                    results[side] = run(self.special_command(side), data=base, cwd=root,
                                        env=env, stdout=write_end)
                finally:
                    os.close(write_end)
                check_unchanged("special-unbuffered-stdout-" + side, before, snapshot(root))
                got = run(self.special_command(side), data=base, cwd=root, env=env)
                check_result("unbuffered-open-stdout/" + side, got, success)
            check_special_pair(SPECIAL_CLOSED_STDOUT, results["legacy"], results["rust"], encoding)
            self.special_counts[SPECIAL_CLOSED_STDOUT] += 1
            self.special_controls["unbuffered-open-stdout"] += 1

            encoding_name = "ascii" if encoding.startswith("ascii") else "utf-8"
            buffered_diagnostic = (f"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='{encoding_name}'>\n"
                                   "BrokenPipeError: [Errno 32] Broken pipe\n").encode()
            for side in ("legacy", "rust"):
                read_end, write_end = os.pipe()
                os.close(read_end)
                try:
                    got = run(self.special_command(side), data=base, cwd=root,
                              env=self.environment(encoding), stdout=write_end)
                finally:
                    os.close(write_end)
                check_result("buffered-closed-stdout/" + side, got, (120, b"", buffered_diagnostic))
            self.special_controls["buffered-closed-stdout"] += 1

            message = f"算出できない: ファイルを読めない: [Errno {errno.EAGAIN}] {os.strerror(errno.EAGAIN)}\n"
            self.both("file-read-would-block", "reader", ["would-block"],
                      (1, b"", special_encode(message, encoding)), data=base, cwd=root, root=root, encoding=encoding)
            self.special_controls["file-read-would-block"] += 1

            # 限定差は成功値の最初のwriteが0バイトでBrokenPipeとなった場合だけ。
            # flush・部分出力・別のI/O失敗・stderrへ広げない。
            # この合成writerの終了120は分岐の検査であり、旧Pythonの実比較証拠ではない。
            diagnostic = special_expectations(SPECIAL_CLOSED_STDOUT, encoding)[1][2]
            for stage, stream, other_error in (
                ("write", "stdout", False), ("partial", "stdout", False),
                ("flush", "stdout", False), ("write", "stdout", True),
                ("write", "stderr", False), ("flush", "stderr", False),
            ):
                source = b"ok\n" if stream == "stdout" else special_encode("算出できない: bad\n", encoding)
                written = source if stage == "flush" else source[:1] if stage == "partial" else b""
                limited = stage == "write" and stream == "stdout" and not other_error
                detail = (f"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='{encoding_name}'>\n"
                          + (f"OSError: [Errno 5] {os.strerror(5)}\n" if other_error
                             else "BrokenPipeError: [Errno 32] Broken pipe\n")).encode()
                error_output = diagnostic if limited else detail if stream == "stdout" else b""
                code = 1 if limited or (stream == "stderr" and stage == "write") else 120
                expected = {"code": code, "written_hex": written.hex(),
                            "stdout_hex": "", "stderr_hex": error_output.hex()}
                env = self.environment(encoding)
                env["PYTHONUNBUFFERED"] = "1"
                args = [stage, stream, *(["io-error"] if other_error else [])]
                got = run(self.command("rust", "writer", args), env=env)
                check_record(f"unbuffered-writer-{stage}-{stream}-{other_error}", got, expected)
                self.synthetic_outputs += 1
            self.special_controls["unbuffered-writer-boundaries"] += 1


    def environment_io(self, root, base):
        # Python自身の環境flagとRustの判定を、独立した固定表の各行と比較する。
        probe = "import json,sys; print(json.dumps({'enabled': type(sys.stdout.buffer).__name__ == 'FileIO'}))"
        for name, value, enabled in UNBUFFERED_CASES:
            env = env_with_value(self.environment(), value)
            for side in ("legacy", "rust"):
                command = ([sys.executable, "-B", "-c", probe] if side == "legacy"
                           else self.command("rust", "unbuffered", []))
                before = snapshot(root)
                got = run(command, cwd=root, env=env)
                check_unchanged("env-flag/" + name + "/" + side, before, snapshot(root))
                check_record("env-flag/" + name + "/" + side, got, {"enabled": enabled})
            record_coverage(self.env_coverage, name)

        for name, value, enabled in UNBUFFERED_CASES:
            if name not in UNBUFFERED_STREAM_IDS:
                continue
            for encoding in SPECIAL_ENCODINGS:
                codec = "ascii" if encoding.startswith("ascii") else "utf-8"
                targets = ("closed-stdout", *CLOSED_STDERR_CASES)
                if sys.platform.startswith("linux"):
                    targets += ("full-stdout",)
                for target in targets:
                    results = {}
                    for side in ("legacy", "rust"):
                        env = env_with_value(self.environment(encoding), value)
                        before = snapshot(root)
                        descriptors = []
                        full = None
                        try:
                            stdout = stderr = subprocess.PIPE
                            if target in ("closed-stdout", "both-closed"):
                                r, stdout = os.pipe()
                                os.close(r)
                                descriptors.append(stdout)
                            if target in CLOSED_STDERR_CASES:
                                r, stderr = os.pipe()
                                os.close(r)
                                descriptors.append(stderr)
                            if target == "full-stdout":
                                # Linuxでopen/fstat/実行ができなければ、skipせず失敗する。
                                full = open("/dev/full", "wb", buffering=0)
                                st = os.fstat(full.fileno())
                                if not stat.S_ISCHR(st.st_mode) or (os.major(st.st_rdev), os.minor(st.st_rdev)) != (1, 7):
                                    raise Failure("Linux /dev/fullの実体が違う")
                                stdout = full
                            command = self.special_command(side)
                            if target == "empty-nonblocking-closed-stderr":
                                got = run_nonblocking(command, data=b"", writer_open=True,
                                                      cwd=root, env=env, stderr=stderr)
                            else:
                                data = b"" if target == "rejection-closed-stderr" else base
                                got = run(command, data=data, cwd=root, env=env, stdout=stdout, stderr=stderr)
                        finally:
                            for fd in descriptors:
                                os.close(fd)
                            if full is not None:
                                full.close()
                        check_unchanged(f"env-stream/{name}/{target}/{side}", before, snapshot(root))
                        results[side] = got
                        if target in CLOSED_STDERR_CASES:
                            expected = (1 if enabled else 120, b"", b"")
                        elif enabled:
                            case_id = SPECIAL_FULL_STDOUT if target == "full-stdout" else SPECIAL_CLOSED_STDOUT
                            expected = special_expectations(case_id, encoding)[0 if side == "legacy" else 1]
                        else:
                            detail = ("OSError: [Errno 28] No space left on device\n" if target == "full-stdout"
                                      else "BrokenPipeError: [Errno 32] Broken pipe\n")
                            diagnostic = (f"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='{codec}'>\n" + detail).encode()
                            expected = (120, b"", diagnostic)
                        check_result(f"env-stream/{name}/{target}/{encoding}/{side}", got, expected)
                    if target == "full-stdout" and name == "one":
                        check_special_pair(SPECIAL_FULL_STDOUT, results["legacy"], results["rust"], encoding)
                        self.special_counts[SPECIAL_FULL_STDOUT] += 1
                    record_coverage(self.env_stream_coverage, (name, target, encoding))

        if not sys.platform.startswith("linux"):
            self.skips.append(f"/dev/full実比較: Linux専用のため{len(UNBUFFERED_STREAM_IDS) * len(SPECIAL_ENCODINGS)}条件未実施。承認済み限定差は{len(SPECIAL_ENCODINGS)}条件未実施")

        # ENOSPCの合成writerは、実fdがfullでない限り許容差へ入らない。
        # flush・部分出力・EIOの合成検査と同様、旧版互換性の証拠とは数えない。
        for encoding in SPECIAL_ENCODINGS:
            codec = "ascii" if encoding.startswith("ascii") else "utf-8"
            diagnostic = (f"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='{codec}'>\n"
                          "OSError: [Errno 28] No space left on device\n").encode()
            for stage in ("write", "flush", "partial"):
                expected = {"code": 120, "written_hex": b"ok\n".hex() if stage == "flush" else b"o".hex() if stage == "partial" else "",
                            "stdout_hex": "", "stderr_hex": diagnostic.hex()}
                before = snapshot(root)
                got = run(self.command("rust", "writer", [stage, "stdout", "no-space"]),
                          cwd=root, env=env_with_value(self.environment(encoding), b"yes"))
                check_unchanged("synthetic-no-space/" + stage + "/" + encoding, before, snapshot(root))
                check_record("synthetic-no-space/" + stage + "/" + encoding, got, expected)
                self.synthetic_outputs += 1
        if sys.platform.startswith("linux"):
            # fd1がfullでも、任意writerにはその属性を転用してはならない。
            before = snapshot(root)
            with open("/dev/full", "wb", buffering=0) as full:
                got = run(self.command("rust", "writer-full-fd", []), cwd=root,
                          env=env_with_value(self.environment(), b"yes"), stdout=full)
            expected = {"code": 120, "written_hex": "", "stdout_hex": "",
                        "stderr_hex": ("Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n"
                                       "OSError: [Errno 28] No space left on device\n").encode().hex()}
            check_unchanged("synthetic-writer-real-fd1-full", before, snapshot(root))
            check_record("synthetic-writer-real-fd1-full", Completed(got.code, got.stderr, got.stdout, got.timed_out), expected)
            self.synthetic_outputs += 1
        check_env_coverage(self.env_coverage, self.env_stream_coverage)


def runtime():
    if sys.version_info[:2] != (3, 10):
        raise Failure(f"比較はPython3.10系が必須: {sys.version.split()[0]}")
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        raise Failure("比較はroot以外の通常利用者が必須")


def selftests():
    path = Path(__file__).with_name("test_task_digest.py")
    if not path.is_file():
        raise Failure("比較器自身の検査がない")
    spec = importlib.util.spec_from_file_location("task_digest_checker_selftests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if suite.countTestCases() == 0:
        raise Failure("比較器自身の検査が0件")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful() or result.skipped:
        raise Failure("比較器自身の検査が不合格または未実施")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    args = parser.parse_args(argv)
    try:
        runtime()
        selftests()
        selected, external = cases(ROOT)
        require_cases(selected)
        comparison = Comparison(args.fixture)
        base = load_existing(ROOT)[0].BASE.encode()
        with tempfile.TemporaryDirectory(prefix="task-digest-comparison-") as temporary:
            root = Path(temporary)
            comparison.normal_cases(root, selected)
            comparison.invalid_utf8(root)
            comparison.arguments(root, base)
            comparison.terminal_widths(root)
            comparison.filesystem(root, base)
            comparison.races(root, base)
            comparison.readers(root, base)
            comparison.outputs(root, base)
            comparison.special_io(root, base)
            comparison.closed_stderr(root, base)
            comparison.environment_io(root, base)
        if comparison.covered_external != external or any(n == 0 for n in comparison.counts.values()):
            raise Failure(f"必須比較の未実行: {external - comparison.covered_external}, {comparison.counts}")
        check_closed_stderr_coverage(comparison.closed_stderr_coverage)
        check_env_coverage(comparison.env_coverage, comparison.env_stream_coverage)
        print(f"task-digest 合格: {comparison.counts}; Python={sys.version.split()[0]}; OS={sys.platform}; 既存37観点・16固定値")
        check_special_counts(comparison.special_counts, comparison.special_controls)
        print(f"承認済み限定差(通常の一致件数と別): {comparison.special_counts}; 対照: {comparison.special_controls}")
        print(f"Rust合成writer: {comparison.synthetic_outputs}条件(実旧版との互換比較件数へ含めない)")
        print(f"環境flag固定表: {len(comparison.env_coverage)}条件; 実stdout/stderr: {len(comparison.env_stream_coverage)}条件(双方を固定期待と比較)")
        for reason in comparison.skips:
            print("未実施: " + reason)
        print(f"未実施件数: {len(comparison.skips)} (成功件数には含めない)")
        return 0
    except (Failure, AssertionError, OSError, ValueError) as exc:
        print(f"task-digest 不合格: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
