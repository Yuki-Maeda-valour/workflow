#!/usr/bin/env python3
"""R4Q の Python の再現の純粋な部品の固定期待を、本家 CPython 3.10.11 の実測で作る。製品の入口ではない。

- units.json: argparse(旧版の 2 本の parser を旧版の main から取り出して当てる)・usage と help の幅ごとの字面・
  shutil.get_terminal_size の幅・int()(桁の上限を含む)・float()・float の repr・int と float の和・
  json.loads(bytes)(文字コードの判定と surrogatepass を含む)・json.dumps・OSError などの str(exc)・
  strerror(Linux)・TimeoutExpired の文・pathlib の str(directory / name)・Unicode の空白と 10 進数字の表・
  旧版の docstring・旧版が捕まえない例外の呼出履歴(旧スクリプトのパスは固定名)・argparse を通る書き出しの
  失敗(help・usage・error の経路と、stdout・stderr の失敗と None。旧版の main をこのプロセスで流し、書いた流れと
  文と、終了値か呼出履歴の文。argparse.py も固定名)・os.pidfd_open の pid の変換(本物の os.pidfd_open)。
- 使い方: Linux・root でない利用者で、pidfd のある本家と同じ中身の 3.10.11 を使う。一時フォルダの親を渡す
  (自分で作った一時フォルダだけを最後に消す。/tmp には作らない)。
  "$R4Q_PYTHON" -B native/fixtures/r4q/gen_units.py --temp-root <フォルダ> [--out <ファイル>]
- 流し直しても同じバイト列になる(乱数は決まった種を使い、実行ごとに変わる pid は @PID@ に置き換える)。
"""
import argparse
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import traceback
import unicodedata
from pathlib import Path, PurePosixPath

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
    "argparse.py": "a238b700513237e10d26056e42c5b4e42d4961ea70e15ebeef4c61c76615f004",
    "textwrap.py": "e1541a31ac906294f915cadd0d780e1e5b256dc1897b560cdaf3fbf46d104cf0",
    "shutil.py": "4cab312042bfca2f6ce26d7240b6762c64169f602b7d8c6629f601a956b43f1b",
    "gettext.py": "46c4d4e2e72bcd572e64bfbce15975d8054a65af167664c6da193773c38dd1ae",
    "json/__init__.py": "d5d41e2c29049515d295d81a6d40b4890fbec8d8482cfb401630f8ef2f77e4d5",
    "json/decoder.py": "9f02654649816145bc76f8c210a5fe3ba1de142d4d97a1c93105732e747c285b",
    "json/encoder.py": "06b881b824f71e95d72af4ab865de4c35553e791b6d959a125caac61401cc350",
    "json/scanner.py": "8604d9d03786d0d509abb49e9f069337278ea988c244069ae8ca2c89acc2cb08",
    "subprocess.py": "53bb0d0780e166ef4ae94f3b2a817a8aae49a84d899db1f4c31f933d429057d6",
    "pathlib.py": "936f14a3ecc4ac5e896e4d673666d76b25a9d581f3b4c8d8d424e830fda2a69b",
    "posixpath.py": "2c770bac0680a5a180b1310102d14e1e120f2f2e8e47925b1574ec808cb8ad6d",
    "traceback.py": "f760b03de3e8e3653f946d62be00d03e25a6ec810a57dd598432eecd21f6cacd",
    "encodings/utf_8_sig.py": "1ef3da8d8aa08149e7f274dc64dbfce2155da812e5258ca8e8f832428d3b5c2d",
    "encodings/utf_16.py": "6c36257f7b8d214473560d195e71bccef0c69a53e1e52d2800b7a7890aad7e58",
    "encodings/utf_32.py": "2072eece5f6026ad2d3549ab193a9e38894ea15ca9d5b3cd408fd6b116acc0c2",
}
SEED = 20261112


def check_python():
    if sys.version_info[:3] != (3, 10, 11):
        raise SystemExit("本家と同じ中身の CPython 3.10.11 で流す")
    for name, digest in STDLIB_SHA256.items():
        if hashlib.sha256((STDLIB / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{name} が本家 3.10.11 と違う")
    for name, path in LEGACY.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != LEGACY_SHA256[name]:
            raise SystemExit(f"旧版 {name} が main e19ca1d と違う")
    if sys.platform != "linux" or os.geteuid() == 0:
        raise SystemExit("Linux の root でない利用者で流す")
    if not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")):
        raise SystemExit("pidfd のある 3.10.11 で流す")
    import json.decoder as decoder
    import json.scanner as scanner
    if scanner.c_make_scanner is None or decoder.c_scanstring is None:
        raise SystemExit("json の C の scanner が無い")


def hexs(text):
    """str を UTF-8(surrogatepass)の 16 進にする。"""
    return text.encode("utf-8", "surrogatepass").hex()


def encode_value(value):
    """Python の値を固定期待の形にする(設計の検査用の結果の JSON と同じ形)。"""
    if value is None:
        return {"none": None}
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int):
        return {"int": int.__repr__(value)}
    if isinstance(value, float):
        return {"float": repr(value)}
    if isinstance(value, str):
        return {"str": text_form(value)}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, list):
        return {"list": [encode_value(item) for item in value]}
    if isinstance(value, dict):
        return {"dict": [[encode_value(key), encode_value(item)] for key, item in value.items()]}
    raise TypeError(type(value))


def text_form(text):
    """str を固定期待の形にする(surrogate が無ければ文字列のまま、あれば {"hex": UTF-8(surrogatepass)})。"""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return {"hex": hexs(text)}


def value_text(value):
    """深い入れ子でも再帰せずに書ける、値の正規の字面(json の固定期待に使う)。
    None は N、True は T、False は F、int は I<10 進>;、float は D<repr>;、str は S<16 進>;、bytes は B<16 進>;、
    list は L<個数>: と要素、dict は M<個数>: と鍵と値の並び。"""
    out = []
    stack = [value]
    while stack:
        item = stack.pop()
        if item is None:
            out.append("N")
        elif item is True:
            out.append("T")
        elif item is False:
            out.append("F")
        elif isinstance(item, int):
            out.append("I" + int.__repr__(item) + ";")
        elif isinstance(item, float):
            out.append("D" + repr(item) + ";")
        elif isinstance(item, str):
            out.append("S" + hexs(item) + ";")
        elif isinstance(item, bytes):
            out.append("B" + item.hex() + ";")
        elif isinstance(item, list):
            out.append(f"L{len(item)}:")
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            out.append(f"M{len(item)}:")
            for key, entry in reversed(list(item.items())):
                stack.append(entry)
                stack.append(key)
        else:
            raise TypeError(type(item))
    return "".join(out)


def error_form(exc):
    return {"type": type(exc).__name__, "message": text_form(str(exc))}


# ---- Unicode ----

def unicode_tables():
    spaces = [code for code in range(127, 0x110000) if chr(code).isspace()]
    decimals = [[code, unicodedata.decimal(chr(code))] for code in range(0x110000)
                if unicodedata.decimal(chr(code), -1) >= 0]
    ascii_spaces = [code for code in range(127) if chr(code).isspace()]
    return {"space_above_ascii": spaces, "decimal": decimals, "ascii_isspace": ascii_spaces}


# ---- int()・float()・float の repr・数の和 ----

INT_FIXED = [
    "0", "007", "-0", "+5", " 1 ", "1_0", "1__0", "_1", "1_", "+_1", "-_1", "１２", "١٢", " 　 7  ", "\x1c1",
    "1\x1c", "1\x7f", "1.0", "1e3", "0x10", "", " ", "+", "-", "+-1", "1 2", "９_９", "\t\n\x0b\x0c\r12\r", "\xa01\xa0",
    "1é", "é1", "\udcff", "1\udcff", "٣_٣", "0_0", "00_00", "-00", "+0", "1" * 199 + "x", "x" * 300,
    "'", '"', "'\"", "\\", "日本", "1" * 640, "1" * 641, "1" * 4300, "1" * 4301, "-" + "1" * 4301, "1" * 5000 + "x",
    "1_" * 700 + "1", " " + "0" * 4301, "1" * 4300 + "_", "1" * 4301 + "__1",
]
INT_ALPHABET = ["0", "1", "5", "9", "_", " ", "+", "-", "١", "９", "　", "\xa0", "x", "\x1c", "\t", "\n", ".", "e",
                "\udcff", "日"]
INT_BYTES_FIXED = [
    b"123", b"-0", b" 12 ", b"1_2", b"1__2", b"\x00", b"12\x0034", b"x", b"\xff", b"1" * 300 + b"x", b"'", b'"',
    b"'\"", b"\\", b"\t1\n", b"", b"+", b"1" * 4301, b"1" * 641, b"12\n", b"\xe3\x81\x82", b"0x1", b"1.5", b"\x7f",
    b"\x1c1", b"\x0b1\x0c", b"b'1'", b"1" * 250, b"\r" * 3 + b"7",
]
FLOAT_FIXED = [
    "0", "1", "-1", "+1", "1.", ".5", "+.5", "-.5", ".", "1e5", "1E5", "1e+5", "1e-5", "1e", "1e+", "1.e5", ".e5", "e5",
    "nan", "-nan", "+NaN", "inf", "-inf", "Infinity", "-infinity", "INFINITY", "infx", "infinit", "nanx", "1_0.5",
    "1._5", "1_.5", "_1", "1_", "1e1_0", "1_e5", "in_f", " 1.5 ", "　 2 ", "１.５", "1e309", "-1e309", "1" * 5000,
    "0x10", "1.5.5", "--1", "", " ", "1 2", "\x1c1", "1\x00", "1_\x00", "4.9e-324", "2e-324", "1.7976931348623157e308",
    "1.7976931348623159e308", "\udcff", "1\udcff", "9" * 400 + ".5", "0." + "0" * 400 + "1", "1e-99999999999",
    "1e99999999999", "0e99999999999", "١.٥", "\xa0nan\xa0",
]
FLOAT_ALPHABET = ["0", "1", "7", ".", "e", "E", "+", "-", "_", " ", "inf", "nan", "x", "١", "　"]


def int_case(text, digits):
    sys.set_int_max_str_digits(digits)
    try:
        return {"int": int.__repr__(int(text))}
    except ValueError as exc:
        return {"error": text_form(str(exc))}
    finally:
        sys.set_int_max_str_digits(4300)


def float_value_form(value):
    return {"repr": repr(value), "bits": float_bits(value)}


def float_bits(value):
    import struct
    return struct.pack(">d", value).hex()


def number_cases():
    rng = random.Random(SEED)
    ints = []
    for text in INT_FIXED:
        for digits in (4300, 0, 640, 5000):
            ints.append({"input": text_form(text), "max_digits": digits, "result": int_case(text, digits)})
    for _ in range(2000):
        text = "".join(rng.choice(INT_ALPHABET) for _ in range(rng.randint(1, 10)))
        ints.append({"input": text_form(text), "max_digits": 4300, "result": int_case(text, 4300)})
    int_bytes = []
    for raw in INT_BYTES_FIXED:
        for digits in (4300, 0):
            sys.set_int_max_str_digits(digits)
            try:
                result = {"int": int.__repr__(int(raw))}
            except ValueError as exc:
                result = {"error": text_form(str(exc))}
            finally:
                sys.set_int_max_str_digits(4300)
            int_bytes.append({"input": raw.hex(), "max_digits": digits, "result": result})
    for _ in range(1000):
        raw = bytes(rng.choice(b"0123456789_ +-x\x00\t\xff'\"\\") for _ in range(rng.randint(0, 8)))
        try:
            result = {"int": int.__repr__(int(raw))}
        except ValueError as exc:
            result = {"error": text_form(str(exc))}
        int_bytes.append({"input": raw.hex(), "max_digits": 4300, "result": result})
    floats = []
    for text in FLOAT_FIXED:
        floats.append(float_case(text))
    for _ in range(2000):
        text = "".join(rng.choice(FLOAT_ALPHABET) for _ in range(rng.randint(1, 8)))
        floats.append(float_case(text))
    reprs = []
    specials = [0.0, -0.0, 1.0, -1.0, 0.1, 0.2, 0.3, 1 / 3, 2 / 3, 1e16, 1e15, 9999999999999998.0, 1e17, 1e-4, 1e-5,
                1.5e-5, 0.0001, 0.00012345, 123456789012345678.0, 5e-324, 2.2250738585072014e-308, 1.7976931348623157e308,
                math.inf, -math.inf, math.nan, 600.2, 607.2, 0.2 + 7, 600 + 0.2 + 7, 1e22, 1e23, 9007199254740993.0,
                4.35, 2.675, 0.30000000000000004, 1e-7, 123.456, 1e300 * 10]
    for value in specials:
        reprs.append(float_value_form(value))
    import struct
    for _ in range(3000):
        bits = rng.getrandbits(64)
        value = struct.unpack(">d", bits.to_bytes(8, "big"))[0]
        reprs.append(float_value_form(value))
    for _ in range(1500):
        mantissa = rng.randint(1, 10 ** rng.randint(1, 17))
        value = float(f"{mantissa}e{rng.randint(-30, 30)}")
        reprs.append(float_value_form(value))
    sums = []
    operands = [0, 1, -1, 7, 600, 2 ** 53, 2 ** 53 + 1, 10 ** 400, -(10 ** 400), 2 ** 1024 - 2 ** 970,
                2 ** 1024 - 2 ** 970 - 1, 0.2, 0.1, -0.0, 1e308, math.inf, math.nan, 1e-5, 600.2, 5]
    for left in operands:
        for right in operands:
            try:
                result = encode_value(left + right)
            except OverflowError as exc:
                result = {"error": text_form(str(exc))}
            try:
                finite = {"bool": math.isfinite(left)}
            except OverflowError as exc:
                finite = {"error": text_form(str(exc))}
            sums.append({"left": encode_value(left), "right": encode_value(right), "sum": result,
                         "left_isfinite": finite, "left_str": text_form(str(left))})
    return {"int": ints, "int_bytes": int_bytes, "float": floats, "float_repr": reprs, "sum": sums}


def float_case(text):
    try:
        return {"input": text_form(text), "result": float_value_form(float(text))}
    except ValueError as exc:
        return {"input": text_form(text), "error": text_form(str(exc))}


# ---- json ----

def loads_here(raw):
    return json.loads(raw)


def loads_case(raw, digits):
    sys.set_int_max_str_digits(digits)
    try:
        return {"value": value_text(loads_here(raw))}
    except (ValueError, RecursionError) as exc:
        return {"error": error_form(exc)}
    finally:
        sys.set_int_max_str_digits(4300)


def random_json_value(rng, depth=0):
    kind = rng.randint(0, 9 if depth < 3 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.choice([0, 1, -1, 3, 300, 10 ** 30, -(10 ** 20), rng.randint(-10 ** 9, 10 ** 9)])
    if kind == 3:
        return rng.choice([0.5, -0.0, 1e300, 1.5e-7, math.inf, -math.inf, math.nan, rng.random()])
    if kind in (4, 5):
        pool = ["a", "\"", "\\", "\n", "\x01", "\x7f", "é", "日", "\U0001f600", "\ud800", "\udcff", "/", " ", " "]
        return "".join(rng.choice(pool) for _ in range(rng.randint(0, 5)))
    if kind in (6, 7):
        return [random_json_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = ["a", "b", "nonce", "clean", "returncode", "", "é", "\ud800", "a"]
    return {rng.choice(keys): random_json_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}


JSON_FIXED = [
    b'{"clean": true, "returncode": 3, "timed_out": false, "signal": 0, "nonce": "n"}\n',
    b'{"clean": true, "nonce": "n", "returncode": 7, "signal": 0, "timed_out": false}\n',
    b'{"nonce": "x", "returncode": 0}\n', b"", b" ", b"\n", b"{", b"}", b"[", b"]", b"[1,]", b"[,1]", b"{,}",
    b'{"a"}', b'{"a":}', b'{"a":1,}', b'{"a" 1}', b'{1:2}', b"[1 2]", b"tru", b"true", b"truex", b"nul", b"null",
    b"false", b"fals", b"NaN", b"-NaN", b"Infinity", b"-Infinity", b"-Infinit", b"infinity", b"nan", b"-", b"--1",
    b"01", b"-01", b"0", b"-0", b"-0.0", b"1.", b"1.5", b".5", b"1e", b"1e5", b"1E+5", b"1e-5", b"1.5e", b"1.5e+",
    b"1e400", b"-1e400", b"1" * 4300, b"1" * 4301, b"-" + b"1" * 4301, b"[" + b"1" * 641 + b"]", b'"\\u12"',
    b'"\\u12g4"', b'"\\ud800"', b'"\\ud800\\udc00"', b'"\\ud800\\u0041"', b'"\\ud800\\uzzzz"', b'"\\udc00"',
    b'"\\x"', b'"\\', b'"a', b'"\x01"', b'"\x1f"', b'"\x7f"', b'"\\/\\b\\f\\n\\r\\t\\"\\\\"', b'"\\u0000"',
    b'{"a": 1, "b": 2, "a": 3}', b'{"a": 1, "a": {"b": 1}}', b"[1] [2]", b"1 x", b" \t\n\r1\r\n\t ",
    b"\xef\xbb\xbf{}", b"\xef\xbb\xbf\xef\xbb\xbf{}", b"\xef\xbb\xbf\xff", b"\xef\xbb\xbf", b"\xff", b"\xfe",
    b"\xc0\x80", b"\xe0\x80\x80", b"\xed\xa0\x80", b'"\xed\xa0\x80"', b'"\xed\xb0\x80"', b'"\xed\xa0"', b"\xed\xa0",
    b"\xf4\x90\x80\x80", b"\xf0\x80\x80\x80", b'"\xe3\x81"', b'"\xe3\x81\x82"', b"\xe3", b'"\xf0\x9f\x98\x80"',
    b'"\xf0\x9f\x98"', b'"\xf0\x9f"', b"\x80", b"\xf5", b"\xc2", b"\xc2\x41",
    '{"a": "日本"}'.encode("utf-16"), '{"a": "日本"}'.encode("utf-16-le"), '{"a": "日本"}'.encode("utf-16-be"),
    '{"a": "日本"}'.encode("utf-32"), '{"a": "日本"}'.encode("utf-32-le"), '{"a": "日本"}'.encode("utf-32-be"),
    "1".encode("utf-16-le"), "1".encode("utf-16-be"), "12".encode("utf-16-le"), "[]".encode("utf-32-le"),
    b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\xff\xfe1", b"\xff\xfe\x00\xd8",
    b"\xff\xfe\x00\xdc", b'\xff\xfe"\x00\x00\xd8"\x00', b'\xfe\xff\x00"\xd8\x00\x00"', b'\xff\xfe"\x00\x00\xd8\x00\xdc"\x00',
    b'\xff\xfe"\x00\x00\xd8', b'\xff\xfe"\x00\x00\xd8"', b'\xff\xfe"\x00"\x00\x00', b"1\x00\x00\x00", b"1\x00\x00",
    b'\xff\xfe\x00\x00"\x00\x00\x00\x00\xd8\x00\x00"\x00\x00\x00', b'\xff\xfe\x00\x00"\x00\x00\x00\x00\x00\x11\x00',
    b'\x00\x00\xfe\xff\x00\x00\x00"\x00\x00\xd8\x00\x00\x00\x00"', b"\xff\xfe\x00\x00\x31\x00\x00", b"\x00\x31",
    b"\x31\x00", b"\x00\x00\x00\x31", b"\x31\x00\x00\x00", b"\x00\x31\x00\x32", b"\x31\x00\x32\x00",
    b"\x00\x00\x00\x31\x00", b"[" * 50 + b"]" * 50, b'{"a":' * 50 + b"1" + b"}" * 50, b"\x00", b"\x00\x00",
]


def json_cases():
    rng = random.Random(SEED + 1)
    # C の scanner が許す入れ子の数を、下の固定期待と同じ呼出しの深さ(main → json_cases → loads_case)で測る。
    low, high = 1, 3000
    while low < high:
        middle = (low + high + 1) // 2
        if "value" in loads_case(b"[" * middle + b"]" * middle, 4300):
            low = middle
        else:
            high = middle - 1
    room = low
    docs = [(raw, 4300) for raw in JSON_FIXED]
    docs += [(b"1" * 4301, 0), (b"1" * 4301, 5000), (b"1" * 700, 640), (b"1" * 641, 640), (b"1" * 640, 640)]
    for depth in (room - 1, room, room + 1):
        docs.append((b"[" * depth + b"]" * depth, 4300))
        docs.append((b'{"a":' * depth + b"1" + b"}" * depth, 4300))
        docs.append((b"[" * (depth - 1) + b'{"a":1}' + b"]" * (depth - 1), 4300))
    encodings = ["utf-8", "utf-8", "utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "utf-16", "utf-32",
                 "utf-8-sig"]
    for _ in range(1500):
        value = random_json_value(rng)
        text = json.dumps(value, ensure_ascii=rng.choice([True, False]))
        if rng.random() < 0.3:
            text = rng.choice([" ", "\n", "\t", ""]) + text + rng.choice([" ", "\r\n", ""])
        encoding = rng.choice(encodings)
        raw = text.encode(encoding, "surrogatepass")
        mutation = rng.randint(0, 5)
        if mutation == 1 and raw:
            at = rng.randrange(len(raw))
            raw = raw[:at] + bytes([rng.randrange(256)]) + raw[at + 1:]
        elif mutation == 2 and raw:
            raw = raw[:rng.randrange(len(raw))]
        elif mutation == 3:
            at = rng.randrange(len(raw) + 1)
            raw = raw[:at] + rng.choice([b",", b"]", b"}", b'"', b"\\", b"\x00", b"\xff", b"1", b"e"]) + raw[at:]
        docs.append((raw, 4300))
    for _ in range(500):
        raw = bytes(rng.choice(b'01-.eE+ []{}",:tfnul\\\x00\xff\xed\xa0\x80') for _ in range(rng.randint(0, 10)))
        docs.append((raw, 4300))
    loads = []
    for raw, digits in docs:
        loads.append({"input": raw.hex(), "max_digits": digits, "result": loads_case(raw, digits)})
    detect = []
    for raw in [b"", b"1", b"12", b"123", b"1234", b"\x00", b"\x00\x00", b"\x00\x01", b"\x01\x00", b"\x00\x00\x00\x01",
                b"\x01\x00\x00\x00", b"\x01\x00\x01\x00", b"\x01\x00\x00\x01", b"\x00\x01\x00\x01", b"\xef\xbb\xbf",
                b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\x01\x00\x00"]:
        detect.append({"input": raw.hex(), "encoding": json.detect_encoding(raw)})
    dumps = []
    fixed = [{"clean": True, "returncode": 3, "timed_out": False, "signal": 0, "nonce": "n"},
             {"nonce": "é日\udcff", "returncode": 0}, {"b": 1, "a": 2, "": 3, "é": 4, "\ud800": 5, "\U0001f600": 6},
             [None, True, False, 0, -1, 10 ** 30, 0.5, -0.0, math.inf, -math.inf, math.nan, 1e16, 1e-5],
             "\x00\x1f\x7f\x80\"\\/\b\f\n\r\t \U0001f600\ud800", {}, [], {"a": []}, {"a": {}}, 10 ** 4300]
    values = fixed + [random_json_value(rng) for _ in range(1000)]
    for value in values:
        sys.set_int_max_str_digits(0)
        entry = {"value": value_text(value)}
        sys.set_int_max_str_digits(4300)
        for name, sort_keys in (("default", False), ("sort_keys", True)):
            try:
                entry[name] = text_form(json.dumps(value, sort_keys=sort_keys))
            except (TypeError, ValueError) as exc:
                entry[name] = {"error": error_form(exc)}
        dumps.append(entry)
    sys.set_int_max_str_digits(0)
    big = json.dumps([10 ** 4300])
    big_value = value_text([10 ** 4300])
    sys.set_int_max_str_digits(4300)
    dumps.append({"value": big_value, "max_digits": 0, "default": text_form(big), "sort_keys": text_form(big)})
    return {"recursion_room": room, "loads": loads, "detect_encoding": detect, "dumps": dumps}


# ---- argparse ----

class Captured(Exception):
    pass


def legacy_parser(name):
    """旧版の main が作る parser を、parse_args の前で取り出す(main の中の定義をそのまま使う)。"""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("r4q_" + name.replace("-", "_").replace(".py", ""), LEGACY[name])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    holder = {}
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        holder["parser"] = self
        raise Captured()

    saved = sys.argv
    argparse.ArgumentParser.parse_args = capture
    sys.argv = [name, "--capture"]
    try:
        module.main()
    except Captured:
        pass
    finally:
        argparse.ArgumentParser.parse_args = original
        sys.argv = saved
    parser = holder["parser"]
    if parser.prog != name or parser.description != module.__doc__:
        raise SystemExit(f"{name} の parser を取り出せない")
    return parser


def with_columns(columns):
    if columns is None:
        os.environ.pop("COLUMNS", None)
    else:
        os.environ["COLUMNS"] = str(columns)


def parse_case(parser, argv, columns, description, digits):
    with_columns(columns)
    usage = parser.format_usage()
    saved = parser.description
    if not description:
        parser.description = None
    sys.set_int_max_str_digits(digits)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            namespace = parser.parse_args(argv)
        result = {"parsed": [[key, encode_value(value)] for key, value in vars(namespace).items()]}
    except SystemExit as exc:
        if exc.code == 0:
            if out.getvalue() != parser.format_help() or err.getvalue():
                raise SystemExit("help の字面が format_help と違う")
            result = {"help": True}
        else:
            text = err.getvalue()
            prefix = usage + parser.prog + ": error: "
            if exc.code != 2 or out.getvalue() or not text.startswith(prefix) or not text.endswith("\n"):
                raise SystemExit("誤りの字面が usage と error: の形でない")
            # usage は幅ごとの固定期待(formats)で照らす。ここは error: の後の文だけを持つ。
            result = {"error": text_form(text[len(prefix):-1])}
    finally:
        parser.description = saved
        sys.set_int_max_str_digits(4300)
        with_columns(None)
    return result


CP_TOKENS = ["--anchor-fd", "--anc", "--a", "--anchor-fd=3", "--anchor-fd=", "--anchor-fd=--", "--linux", "--l",
             "--linux=1", "--li=", "--result", "--r", "--result=/x/r.json", "--res=--", "--timeout", "--t",
             "--timeout=5", "--timeout=nan", "--grace", "--g=0.2", "--grace=--", "--nonce", "--n", "--nonce=n",
             "--non=", "--nonce=--", "-h", "--help", "--h", "--he=x", "-hh", "-hx", "-h=", "-h=1", "-hhx"]
LS_TOKENS = ["--result", "--r", "--res=/r", "--result=--", "--nonce", "--n", "--nonce=", "--nonce=--", "--timeout",
             "--t", "--ti=5", "--timeout=--", "--grace", "--g", "--grace=0", "--grace=--", "-h", "--help", "--h",
             "-hh", "-hx", "--check", "--chec", "--check=1"]
COMMON_TOKENS = ["--", "--", "-", "---", "--=x", "--bogus", "-x", "-x=1", "-1", "-.5", "-1e3", "-1\n", "-١", "-1.",
                 "a b", "-a b", "--a b", "5", "0", "1_0", "１２", " 7 ", "nan", "inf", "-inf", "Infinity", "1e309",
                 "x", "", "sh", "true", "echo", "\udcff", "é", "2147483648", "-2147483649", "0.2", "-0.0", "1s",
                 "+5", "1" * 700, "日本", "'q'", "\"", "-\n"]


def argparse_cases():
    rng = random.Random(SEED + 2)
    parsers = {"check-process.py": legacy_parser("check-process.py"),
               "loop-supervisor.py": legacy_parser("loop-supervisor.py")}
    tokens = {"check-process.py": CP_TOKENS, "loop-supervisor.py": LS_TOKENS}
    fixed = {
        "check-process.py": [
            [], ["--help"], ["--h"], ["--nonce", "x"], ["--nonce", "x", "--"], ["--nonce", "x", "--", "true"],
            ["--anchor-fd", "99", "--nonce", "x", "--", "true"], ["--anchor-fd", "-1", "--nonce", "x", "true"],
            ["--anchor-fd", "2147483648", "--nonce", "x", "--", "true"], ["--anchor-fd=1", "--nonce=z", "--", "/no"],
            ["--linux", "--nonce", "n", "--", "true"], ["--linux", "--timeout", "nan", "--nonce", "n", "--", "true"],
            ["--linux", "--timeout", "1", "--grace", "0", "--nonce", "n", "--", "true"],
            ["--linux=1", "--nonce", "n"], ["-hx"], ["--nonce", "-x"], ["--nonce", "--"], ["--timeout", "-1"],
            ["--timeout", "-inf"], ["--timeout=-inf", "--nonce", "n"], ["--grace", "-.5", "--nonce", "n"],
            ["--bogus", "--nonce", "n", "--", "true"], ["--nonce", "a", "echo", "--nonce", "b"],
            ["--nonce", "a", "--", "--", "x"], ["--=x"], ["--nonce", "a", "--nonce", "b", "--", "true"],
            ["--anchor-fd", "x", "--nonce", "n"], ["--timeout", "1s", "--nonce", "n"], ["--nonce", "\udcff", "x\udcff"],
            ["--anchor-fd", "1" * 4400, "--nonce", "n"], ["--anchor-fd=--", "--nonce", "x", "--", "true"],
            ["--linux", "--result=--", "--timeout", "1", "--grace", "0", "--nonce", "n", "--", "true"],
            ["--linux", "--timeout=--", "--grace", "0", "--nonce", "n", "--", "true"], ["-hh"], ["-hhx"], ["-h="],
            ["--nonce", "x", "-1", "-x"], ["--nonce=--", "--", "true"], ["\udcff", "--nonce", "n"],
        ],
        "loop-supervisor.py": [
            [], ["--help"], ["--check"], ["--chec"], ["--check=1"], ["--check", "x"],
            ["--result", "R", "--nonce", "n", "--timeout", "5", "--grace", "0.2", "--", "sh", "-c", "exit 3"],
            ["--r", "R", "--n", "n", "--t", "5", "--g", "0.2", "true"], ["--result=R", "--nonce=n", "--timeout=nan",
                                                                         "--grace=inf", "--", "true"],
            ["--result", "R", "--nonce", "n", "--timeout", "1e309", "--grace", "١", "--"],
            ["--result", "R", "--nonce", "n", "--timeout", "0", "--grace", "-1"], ["--nonce", "n"],
            ["--result", "R", "--result", "S", "--nonce", "n", "--timeout", "1", "--grace", "0"],
            ["--result", "-R"], ["--timeout", "x", "--result", "R", "--nonce", "n", "--grace", "0"],
            ["--result=--", "--nonce", "n", "--timeout", "1", "--grace", "0", "--", "true"],
            ["--result", "R", "--nonce", "n", "--timeout=--", "--grace", "0", "--", "true"],
            ["--=1"], ["--res", "R", "--non", "n", "--tim", "1", "--gra", "0", "--", "--", "x"],
        ],
    }
    cases = []
    for name, parser in parsers.items():
        for argv in fixed[name]:
            for columns in (80, 40, 200):
                cases.append((name, argv, columns, True, 4300))
        for _ in range(1500):
            pool = tokens[name] + COMMON_TOKENS
            argv = [rng.choice(pool) for _ in range(rng.randint(0, 7))]
            columns = rng.choice([80, 80, 80, 1, 3, 10, 30, 33, 34, 35, 40, 60, 100, 200])
            description = rng.random() < 0.8
            digits = rng.choice([4300, 4300, 0, 640])
            cases.append((name, argv, columns, description, digits))
    result = []
    for name, argv, columns, description, digits in cases:
        outcome = parse_case(parsers[name], argv, columns, description, digits)
        result.append({"parser": name, "argv": [text_form(text) for text in argv], "columns": columns,
                       "description": description, "max_digits": digits, "outcome": outcome})
    formats = []
    widths = list(range(1, 141)) + [160, 200, 300, 1000]
    for name, parser in parsers.items():
        for columns in widths:
            with_columns(columns)
            usage = parser.format_usage()
            help_text = parser.format_help()
            saved = parser.description
            parser.description = None
            plain = parser.format_help()
            parser.description = saved
            with_columns(None)
            formats.append({"parser": name, "columns": columns, "usage": usage, "help": help_text,
                            "help_without_description": plain})
    return {"cases": result, "formats": formats}


def terminal_cases():
    entries = []
    values = [None, "", "80", " 50 ", "+50", "５０", "0", "-5", "abc", "1_0", "1" * 20, "1" * 5000, "\udcff", "١٢٠",
              "40\n", "1e2", "2"]
    for columns in values:
        for terminal in (None, 0, 100, 1):
            for digits in (4300, 0):
                original = os.get_terminal_size

                def fake(fd=None, terminal=terminal):
                    if terminal is None:
                        raise OSError(25, "Inappropriate ioctl for device")
                    return os.terminal_size((terminal, 24))

                os.get_terminal_size = fake
                with_columns(None)
                if columns is not None:
                    os.environ["COLUMNS"] = columns
                sys.set_int_max_str_digits(digits)
                try:
                    size = shutil.get_terminal_size().columns
                finally:
                    os.get_terminal_size = original
                    sys.set_int_max_str_digits(4300)
                    with_columns(None)
                entries.append({"columns": None if columns is None else text_form(columns), "terminal": terminal,
                                "max_digits": digits, "result": min(size, 10 ** 12)})
    return entries


# ---- 例外の文・strerror・TimeoutExpired・pathlib ----

def exception_cases():
    oserrors = []
    filenames = [None, "/a", "it's", '"q"', "'\"", "x\udcff", "日本", b"/a\xff", 3, "", "a\nb", "\\"]
    strerrors = ["No such file or directory", "subreaper を設定できない", ""]
    for errno in (1, 2, 13, 17, 20, 21, 22, 40, 9999):
        for strerror in strerrors:
            for filename in filenames:
                oserrors.append({"errno": errno, "strerror": text_form(strerror), "filename": encode_value(filename),
                                 "filename2": encode_value(None),
                                 "message": text_form(str(OSError(errno, strerror, filename)))})
    for first, second in (("/a", "/b"), ("x\udcff", b"y"), (3, "z")):
        oserrors.append({"errno": 18, "strerror": text_form("Invalid cross-device link"), "filename": encode_value(first),
                         "filename2": encode_value(second),
                         "message": text_form(str(OSError(18, "Invalid cross-device link", first, None, second)))})
    types = [{"errno": errno, "type": type(OSError(errno, "x")).__name__} for errno in range(0, 141)]
    strerror = [{"errno": errno, "text": os.strerror(errno)} for errno in range(-5, 141)]
    timeouts = []
    commands = [["python3", "-B", "check-process.py", "--linux", "--result", "/w/r/recovery.json", "--nonce", "0" * 64,
                 "--timeout", "600", "--grace", "0.2", "--", "sh", "-c", "printf 'x'; exit 3"],
                ["a'b", 'a"b', "a'\"b", "\udcff", "日本", "\n", "\\"], []]
    pairs = [(600, 0.2), (5, 0.2), (1, 0), (0.1, 0.0), (1e-5, 0.2), (2 ** 60, 0), (10 ** 20, 3), (1.5, 2), (600, 0)]
    for command in commands:
        for timeout, grace in pairs:
            total = timeout + grace + 7
            timeouts.append({"cmd": [text_form(item) for item in command], "timeout": encode_value(timeout),
                             "grace": encode_value(grace), "total": encode_value(total),
                             "message": text_form(str(subprocess.TimeoutExpired(command, total)))})
            timeouts.append({"cmd": [text_form(item) for item in command], "timeout": encode_value(2),
                             "grace": encode_value(0), "total": encode_value(2),
                             "message": text_form(str(subprocess.TimeoutExpired(command, 2)))})
    paths = []
    for directory in [b"/a/b", b"/a/b/", b"//a", b"///a", b"a", b"", b".", b"/a/./b", b"a//b", b"\xff", b"/", b"..",
                      b"/a/../b", "日本".encode()]:
        for name in [b"recovery.json", b"stdout", b"stderr"]:
            paths.append({"directory": directory.hex(), "name": name.hex(),
                          "result": os.fsencode(str(PurePosixPath(os.fsdecode(directory)) / os.fsdecode(name))).hex()})
    keys = [{"key": encode_value(key), "message": text_form(str(KeyError(key)))} for key in (12345, -1, 0, 10 ** 30)]
    backslash = [{"input": text_form(text), "result": text.encode("utf-8", "backslashreplace").hex()}
                 for text in ["a", "é", "\udcff", "𐏿", "日本\udc80", "\U0001f600", ""]]
    return {"oserror": oserrors, "oserror_type": types, "strerror_linux": strerror, "timeout_expired": timeouts,
            "pathlib": paths, "key_error": keys, "backslashreplace": backslash}


# ---- 旧版の呼出履歴 ----

WRAPPER = r'''
import os, pathlib, signal, sys, traceback
legacy, mode = sys.argv[1], sys.argv[2]
sys.argv = [legacy] + sys.argv[3:]
original = pathlib.Path.read_bytes
calls = []
def patched(self):
    text = str(self)
    if mode != "none" and text.startswith("/proc/") and text.endswith("/stat") and text != "/proc/self/stat":
        calls.append(text)
        if mode == "before" or len(calls) >= 2:
            os.kill(int(text.split("/")[2]), signal.SIGKILL)
            return b"1 (x) S"
    return original(self)
pathlib.Path.read_bytes = patched
code = compile(open(legacy).read(), legacy, "exec")
try:
    exec(code, {"__name__": "__main__", "__file__": legacy, "__builtins__": __builtins__})
except SystemExit:
    raise
except BaseException:
    kind, value, tb = sys.exc_info()
    traceback.print_exception(kind, value, tb.tb_next)
    sys.exit(1)
'''


def fixed_names(text):
    for name, path in LEGACY.items():
        text = text.replace(f'File "{path}"', f'File "{name}"')
    return text


def run_legacy(work, name, args, mode=None, sigchld_ignored=False):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"}
    command = [sys.executable, "-B"]
    if mode is None:
        command += [str(LEGACY[name]), *args]
    else:
        command += [str(work / "wrapper.py"), str(LEGACY[name]), mode, *args]

    def preexec():
        if sigchld_ignored:
            signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    done = subprocess.run(command, cwd=work, env=env, capture_output=True, preexec_fn=preexec, timeout=60)
    if done.returncode != 1 or done.stdout:
        raise SystemExit(f"旧版の呼出履歴が出ない: {name} {args} {done.returncode} {done.stderr!r}")
    return fixed_names(done.stderr.decode("utf-8"))


def traceback_cases(work):
    (work / "wrapper.py").write_text(WRAPPER, encoding="utf-8")
    record = lambda label: str(work / f"r-{label}")  # noqa: E731
    real = [
        ("cp-anchor-none", "check-process.py", ["--nonce", "x", "--", "true"], "CheckProcessAnchor"),
        ("cp-anchor-list", "check-process.py", ["--anchor-fd=--", "--nonce", "x", "--", "true"], "CheckProcessAnchor"),
        ("cp-anchor-overflow", "check-process.py", ["--anchor-fd", "2147483648", "--nonce", "x", "--", "true"],
         "CheckProcessAnchor"),
        ("cp-anchor-overflow-negative", "check-process.py", ["--anchor-fd", "-2147483649", "--nonce", "x", "--",
                                                              "true"], "CheckProcessAnchor"),
        ("cp-anchor-overflow-huge", "check-process.py", ["--anchor-fd", "9" * 30, "--nonce", "x", "--", "true"],
         "CheckProcessAnchor"),
        ("cp-deadline-timeout-list", "check-process.py", ["--linux", "--timeout=--", "--grace", "0", "--nonce", "n",
                                                           "--", "true"], "CheckProcessDeadline"),
        ("cp-deadline-grace-list", "check-process.py", ["--linux", "--timeout", "1", "--grace=--", "--nonce", "n",
                                                         "--", "true"], "CheckProcessDeadline"),
        ("cp-open-none", "check-process.py", ["--linux", "--timeout", "1", "--grace", "0", "--nonce", "n", "--",
                                              "true"], "CheckProcessOpen"),
        ("cp-open-list", "check-process.py", ["--linux", "--result=--", "--timeout", "1", "--grace", "0", "--nonce",
                                              "n", "--", "true"], "CheckProcessOpen"),
        ("ls-deadline-timeout-list", "loop-supervisor.py", ["--result", record("a"), "--nonce", "n", "--timeout=--",
                                                             "--grace", "0", "--", "true"], "LoopSupervisorDeadline"),
        ("ls-deadline-grace-list", "loop-supervisor.py", ["--result", record("b"), "--nonce", "n", "--timeout", "1",
                                                           "--grace=--", "--", "true"], "LoopSupervisorDeadline"),
        ("ls-open-list", "loop-supervisor.py", ["--result=--", "--nonce", "n", "--timeout", "1", "--grace", "0", "--",
                                                "true"], "LoopSupervisorOpen"),
    ]
    cases = []
    for label, name, args, site in real:
        text = run_legacy(work, name, args)
        wrapped = run_legacy(work, name, args, mode="none")
        if wrapped != text:
            raise SystemExit(f"包んで流した呼出履歴が、直接流した呼出履歴と違う: {label}")
        cases.append({"id": label, "site": site, "inner": None, "stderr": text})
    sigchld = [
        ("ls-keyerror", "loop-supervisor.py", ["--result", record("c"), "--nonce", "n", "--timeout", "0.2",
                                               "--grace", "0", "--", "true"], "LoopSupervisorSupervise"),
        ("cp-keyerror", "check-process.py", ["--linux", "--result", record("d"), "--timeout", "0.2", "--grace", "0",
                                             "--nonce", "n", "--", "true"], "CheckProcessSupervise"),
    ]
    for label, name, args, site in sigchld:
        text = run_legacy(work, name, args, sigchld_ignored=True)
        lines = text.split("\n")
        if not re.fullmatch(r"KeyError: [0-9]+", lines[-2]):
            raise SystemExit(f"KeyError の行が無い: {label}")
        lines[-2] = "KeyError: @PID@"
        cases.append({"id": label, "site": site, "inner": "Returncode", "stderr": "\n".join(lines)})
    injected = [
        ("ls-index-before", "loop-supervisor.py", "before", ["--result", record("e"), "--nonce", "n", "--timeout",
                                                             "0.1", "--grace", "0", "--", "sleep", "30"],
         "LoopSupervisorSupervise", "IdentityBefore"),
        ("ls-index-after", "loop-supervisor.py", "after", ["--result", record("f"), "--nonce", "n", "--timeout",
                                                           "0.1", "--grace", "0", "--", "sleep", "30"],
         "LoopSupervisorSupervise", "IdentityAfter"),
        ("cp-index-before", "check-process.py", "before", ["--linux", "--result", record("g"), "--timeout", "0.1",
                                                           "--grace", "0", "--nonce", "n", "--", "sleep", "30"],
         "CheckProcessSupervise", "IdentityBefore"),
        ("cp-index-after", "check-process.py", "after", ["--linux", "--result", record("h"), "--timeout", "0.1",
                                                         "--grace", "0", "--nonce", "n", "--", "sleep", "30"],
         "CheckProcessSupervise", "IdentityAfter"),
    ]
    for label, name, mode, args, site, inner in injected:
        text = run_legacy(work, name, args, mode=mode)
        cases.append({"id": label, "site": site, "inner": inner, "stderr": text})
    return cases


# ---- os.pidfd_open の pid の変換(壊れた children の pid。mock で置き換えない本物の変換) ----

def pid_conversion_cases():
    values = [2 ** 31 - 1, 2 ** 31, -2 ** 31, -2 ** 31 - 1, 2 ** 63 - 1, 2 ** 63, -2 ** 63, -2 ** 63 - 1, 10 ** 20,
              -10 ** 20, 10 ** 400]
    cases = []
    for value in values:
        try:
            fd = os.pidfd_open(value)
        except OverflowError as exc:
            cases.append({"pid": str(value), "overflow": str(exc)})
            continue
        except OSError:
            pass  # C の int の中の値は変換を通り、呼出しの誤り(ESRCH・EINVAL)になる
        else:
            os.close(fd)
        cases.append({"pid": str(value), "overflow": None})
    return cases


# ---- argparse を通る書き出しの失敗(help・usage・error の各経路と、stdout・stderr の失敗・None) ----

ARGPARSE_PATH = str(Path(argparse.__file__).resolve())


class Recorder:
    """sys.stdout・sys.stderr の代わり。n 回目の write で OSError を投げる(本物の write は C なのでフレームが無い。
    このクラスのフレームは呼出履歴から外す)。"""

    def __init__(self, name, fail_at, writes):
        self.name, self.fail_at, self.writes, self.count = name, fail_at, writes, 0

    def write(self, text):
        self.count += 1
        if self.count == self.fail_at:
            raise OSError(28, "No space left on device")
        self.writes.append([self.name, text])
        return len(text)

    def flush(self):
        pass


def stream(name, mode, writes):
    if mode == "none":
        return None
    return Recorder(name, int(mode[4:]) if mode.startswith("fail") else 0, writes)


def format_raised(exc):
    """呼出履歴の文(旧スクリプトと argparse.py のフレームだけ。パスは固定名)。"""
    def keep(chain_exc):
        frames = [frame for frame in traceback.extract_tb(chain_exc.__traceback__)
                  if frame.filename in {str(path) for path in LEGACY.values()} or frame.filename == ARGPARSE_PATH]
        return traceback.StackSummary.from_list(frames)
    chain = []
    current = exc
    while current is not None:
        chain.append(current)
        current = current.__context__ if not current.__suppress_context__ and current.__cause__ is None else None
    blocks = []
    for item in reversed(chain):
        lines = ["Traceback (most recent call last):\n"] + keep(item).format()
        lines += traceback.format_exception_only(type(item), item)
        blocks.append("".join(lines))
    text = "\nDuring handling of the above exception, another exception occurred:\n\n".join(blocks)
    for name, path in LEGACY.items():
        text = text.replace(f'File "{path}"', f'File "{name}"')
    return text.replace(f'File "{ARGPARSE_PATH}"', 'File "argparse.py"')


def run_argparse_write(name, args, out, err):
    writes = []
    saved = sys.stdout, sys.stderr, sys.argv
    sys.argv = [str(LEGACY[name]), *args]
    sys.stdout, sys.stderr = stream("stdout", out, writes), stream("stderr", err, writes)
    code = compile(LEGACY[name].read_text(encoding="utf-8"), str(LEGACY[name]), "exec")
    try:
        exec(code, {"__name__": "__main__", "__file__": str(LEGACY[name]), "__builtins__": __builtins__})
        end = {"exit": None}
    except SystemExit as exc:
        end = {"exit": exc.code}
    except BaseException as exc:  # noqa: B902(書き出しの失敗の例外を、そのまま呼出履歴の文にする)
        end = {"raised": format_raised(exc)}
    finally:
        sys.stdout, sys.stderr, sys.argv = saved
    return writes, end


def argparse_write_cases():
    unrecognized = ["--zz", "--result", "r", "--nonce", "n", "--timeout", "1", "--grace", "1"]
    plan = [
        ("loop-supervisor.py", "help", ["-h"], [("fail1", "ok"), ("none", "ok"), ("none", "fail1"), ("none", "none")]),
        ("loop-supervisor.py", "required", [], [("ok", "fail1"), ("ok", "fail2"), ("ok", "none"), ("fail1", "none"),
                                                 ("none", "none")]),
        ("loop-supervisor.py", "float", ["--timeout", "x"], [("ok", "fail1"), ("ok", "fail2")]),
        ("loop-supervisor.py", "expected", ["--result"], [("ok", "fail1"), ("ok", "fail2")]),
        ("loop-supervisor.py", "ignored-long", ["--help=x"], [("ok", "fail1"), ("ok", "none")]),
        ("loop-supervisor.py", "unrecognized", unrecognized, [("ok", "fail1"), ("ok", "fail2")]),
        ("check-process.py", "help", ["-h"], [("fail1", "ok"), ("none", "ok")]),
        ("check-process.py", "int", ["--anchor-fd", "x", "--nonce", "n"], [("ok", "fail1"), ("ok", "fail2")]),
        ("check-process.py", "int-digits", ["--anchor-fd", "1" * 5000, "--nonce", "n"], [("ok", "fail1")]),
        ("check-process.py", "ignored-short", ["-hx"], [("ok", "fail1"), ("ok", "fail2")]),
        ("check-process.py", "required", [], [("ok", "fail2"), ("fail1", "none")]),
    ]
    cases = []
    with_columns(80)
    for name, label, args, setups in plan:
        for out, err in setups:
            writes, end = run_argparse_write(name, args, out, err)
            cases.append({"id": f"{name[:2]}-{label}-out-{out}-err-{err}", "script": name, "args": args,
                          "stdout": out, "stderr": err, "writes": writes, "end": end})
    with_columns(None)
    return cases


def docstrings():
    result = {}
    for name, path in LEGACY.items():
        result[name] = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")), clean=False)
    return result


def main():
    parser = argparse.ArgumentParser(description="R4Q の固定期待を作る")
    parser.add_argument("--temp-root", required=True, help="一時フォルダを作る親(/tmp 以外)")
    parser.add_argument("--out", default=str(HERE / "units.json"))
    args = parser.parse_args()
    check_python()
    for name in ("COLUMNS", "LINES"):
        os.environ.pop(name, None)
    work = Path(tempfile.mkdtemp(prefix="r4q-gen-", dir=os.path.realpath(args.temp_root)))
    try:
        fixture = {
            "schema_version": 1,
            "purpose": "R4Q の Python の再現の純粋な部品の固定期待(本家 CPython 3.10.11 の実測)",
            "generator": "native/fixtures/r4q/gen_units.py",
            "string_form": "str は UTF-8(surrogatepass)の 16 進。bytes は 16 進",
            "python": {"version": "3.10.11", "stdlib_sha256": STDLIB_SHA256, "legacy_sha256": LEGACY_SHA256},
            "docstrings": docstrings(),
            "unicode": unicode_tables(),
            "number": number_cases(),
            "json": json_cases(),
            "argparse": argparse_cases(),
            "terminal": terminal_cases(),
            "exception": exception_cases(),
            "traceback": traceback_cases(work),
            "argparse_writes": argparse_write_cases(),
            "pidfd_pid": pid_conversion_cases(),
        }
    finally:
        shutil.rmtree(work)
    text = json.dumps(fixture, ensure_ascii=False, indent=1) + "\n"
    Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
