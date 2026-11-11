#!/usr/bin/env python3
"""S2E(#470)用の固定期待を Python 3.10 から作る。出力は同じフォルダの JSON。

  python3 -B gen_cases.py --out <フォルダ> [--only pystr,json,path,args,stdout]
  python3 -B gen_cases.py --out <フォルダ> --check

--check は、既存の JSON がいまの Python から作った内容と同じかを確かめる。
文字列は文字番号の配列で持つ(surrogate を JSON に入れられるようにするため)。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import struct
import sys
import unicodedata
from pathlib import Path

if sys.version_info[:2] != (3, 10) or unicodedata.unidata_version != "13.0.0":
    raise SystemExit("Python 3.10(Unicode 13.0.0)で実行する: " + sys.version.split()[0])

HERE = Path(__file__).resolve().parent


def cp(text):
    return [ord(c) for c in text]


def text_of(cps):
    return "".join(chr(c) for c in cps)


def rng(name):
    return random.Random("s2e-" + name)


# ---------------------------------------------------------------- pystr


def int_cases():
    r = rng("int")
    pool = list("0123456789") * 3 + list("_+-  \t\n") + ["\x1c", "\x1f", "　", " ", "١", "٣", "１", "２", "x", ".", "e", "\x00", "\x7f", "​", "a", "\x85"]
    texts = [
        "", " ", "0", "00", "-0", "+0", "007", "42", " 42 ", "\t\n42\r\x0b\x0c", "1_000", "1__000", "_1", "1_", "+_1", "-_1",
        "0_0", "0_7", "1_0_0", "١٢٣", "１２３", "　５ ", "0x10", "0b1", "1.0", "1e3", "1 2", "- 1", "+ 1", "--1", "+-1",
        "\x1c1", "1\x1c", "\x851", "1\x85", "٣x", "1\x00", "\x001", "١_٢", "１_２", "٠" * 5, "9" * 640, "9" * 641, "9" * 700,
        "9" * 4300, "9" * 4301, "-" + "9" * 4301, "0" * 5000 + "1", "0" * 5000, "_" * 3, "٣" * 700, "1" * 641 + "_",
        "9" * 4301 + "x", "9" * 4301 + "é", " " + "9" * 4301 + "x", "9" * 4301 + "\x00", "9" * 4301 + "\x00" + "1", "9" * 4301 + "_", "9" * 4301 + "__1",
        "9" * 4301 + " ", "9" * 4301 + "٣", "-" + "9" * 4301 + "x", "9" * 700 + "x", "9" * 641 + "x", "_" + "9" * 4301, "9" * 4300 + "x",
        "9" * 4301 + "\u3000", "9_" * 2200 + "9", "9_" * 2151,
    ]
    for _ in range(1500):
        texts.append("".join(r.choice(pool) for _ in range(r.randint(0, 8))))
    out = []
    for limit in (4300, 640, 0):
        sys.set_int_max_str_digits(limit)
        for text in texts:
            try:
                value = int(text)
                sys.set_int_max_str_digits(0)
                out.append({"limit": limit, "text": cp(text), "ok": str(value)})
                sys.set_int_max_str_digits(limit)
            except ValueError as error:
                out.append({"limit": limit, "text": cp(text), "error": str(error)})
    sys.set_int_max_str_digits(4300)
    return out


def repr_cases():
    r = rng("repr")
    pool = cp("abc'\"\\\n\r\t \x00\x1f\x7f\x80\xa0\xadĀ͸​ 　あ") + [0xD800, 0xDC80, 0xDCFF, 0xFFFF, 0x10000, 0x1F600, 0x10FFFF, 0xE000, 0xE0001]
    values = [[], cp("'"), cp('"'), cp("'\""), cp("it's"), cp("\\"), cp("\n")]
    for _ in range(3000):
        values.append([r.choice(pool) for _ in range(r.randint(0, 6))])
    return [{"text": v, "repr": cp(repr(text_of(v)))} for v in values]


def space_cases():
    r = rng("space")
    pool = cp(" \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0   ​    　﻿") + cp("abあ0") + [0xDC80]
    values = [[], cp(" "), cp("a"), cp("a b")]
    for _ in range(3000):
        values.append([r.choice(pool) for _ in range(r.randint(0, 9))])
    out = []
    for v in values:
        s = text_of(v)
        out.append({"text": v, "split": [cp(p) for p in s.split()], "strip": cp(s.strip()), "isspace": s.isspace()})
    return out


def float_cases():
    r = rng("float")
    values = [0.0, -0.0, 1.0, -1.0, 0.1, 0.5, 1e15, 1e16, 9999999999999998.0, 1e17, 1.5e300, 5e-324, 2.2250738585072014e-308, 1.7976931348623157e308,
              1e-4, 1e-5, 0.0001234, 0.00001234, 123456789.123456789, 2.0 ** 53, 2.0 ** 63, 2.0 ** 70, 1e22, 1e23, 0.3, 0.1 + 0.2, 100.0, 1e2, 12345678901234567890.0]
    for _ in range(3000):
        bits = r.getrandbits(64)
        value = struct.unpack("<d", struct.pack("<Q", bits))[0]
        if value == value and abs(value) != float("inf"):
            values.append(value)
    for _ in range(1000):
        values.append(r.uniform(-1e6, 1e6))
        values.append(float(r.randint(-10 ** 18, 10 ** 18)))
        values.append(r.randint(1, 10 ** 6) * 10.0 ** r.randint(-30, 30))
    out = []
    for v in values:
        out.append({"bits": "%016x" % struct.unpack("<Q", struct.pack("<d", v))[0], "repr": repr(v),
                    "integral": (v == int(v)) if abs(v) < 1e308 else True,
                    "int_text": str(int(v))})
    return out


AREAS = {}


def area(name):
    def wrap(function):
        AREAS[name] = function
        return function
    return wrap


@area("pystr")
def make_pystr():
    return {
        "int_cases.json": int_cases(),
        "repr_cases.json": repr_cases(),
        "space_cases.json": space_cases(),
        "float_cases.json": float_cases(),
    }


@area("path")
def make_path():
    import os
    from pathlib import Path
    r = rng("path")
    pool = ["/", "/", "/", ".", "..", "a", "b", "~", "//", "", "x y", "あ", "\udc80", "\ud800", "a/", "./", "...", "c.d", "\u3000"]
    texts = ["", "/", "//", "///", "////", ".", "./", "a//b", "/a/./b/", "a/../b", "//a//b", "///a", "..", "/..", "~", "~/x", " /a"]
    for _ in range(3000):
        texts.append("".join(r.choice(pool) for _ in range(r.randint(0, 5))))
    paths = []
    for text in texts:
        other = "".join(r.choice(pool) for _ in range(r.randint(0, 4)))
        path = Path(text)
        paths.append({
            "text": cp(text), "str": cp(str(path)), "parts": [cp(p) for p in path.parts], "name": cp(path.name),
            "parent": cp(str(path.parent)), "parents": [cp(str(p)) for p in path.parents],
            "is_absolute": path.is_absolute(), "isabs": os.path.isabs(text),
            "other": cp(other), "joined": cp(str(path / other)),
        })
    expand = []
    homes = ["", "/", "/h", "/h/", "/h//", "rel", "//h//", "/h/\udcff", "~"]
    names = ["~", "~/", "~/a", "~//a", "~root", "~root/", "~root/x", "~nouser-zz/x", "~nouser-zz", "a", "", "a~", "~a\x00b", "~\ud800", "~\udcff/x", "~~", "~\x00", "~root\x00/x", "~/~", "~\u3042/x", "~\x00\ud800", "~\ud800\x00", "~\udc80\x00", "~\udcff\ud800/x"]
    for home in homes:
        for text in names:
            os.environ["HOME"] = home
            try:
                result = os.path.expanduser(text)
                expand.append({"text": cp(text), "home": cp(home), "result": cp(result)})
            except Exception as error:
                expand.append({"text": cp(text), "home": cp(home), "error": type(error).__name__, "msg": str(error)})
    return {"path_cases.json": {"paths": paths, "expand": expand}}


# ---------------------------------------------------------------- json

OLD_GUARD_SHA256 = "5770210377a1c5ef79effbcbf8f5f0678946f14ad9f75143fd1c219daf64d796"
GUARD_PATH = HERE.parents[2] / "plugins/dev-workflow/skills/ship-task/scripts/environment-guard.py"


def load_guard():
    import hashlib
    import importlib.util

    digest = hashlib.sha256(GUARD_PATH.read_bytes()).hexdigest()
    if digest != OLD_GUARD_SHA256:
        raise SystemExit("旧 guard の SHA-256 が違う: " + digest)
    spec = importlib.util.spec_from_file_location("verified_environment_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frame_count(frame):
    count = 0
    while frame is not None:
        count += 1
        frame = frame.f_back
    return count


def call_at(depth, function, *args, boxed=False, **kwargs):
    """`function` を呼ぶフレームの深さが `depth` 以上になるように再帰してから呼ぶ。実際の深さも返す。

    boxed が真なら、`function` 自身が `box["site"]` に、中で呼ぶ関数を呼ぶフレームの深さを入れる。
    """
    box = {}

    def inner():
        if boxed:
            return function(*args, box=box, **kwargs)
        box["site"] = frame_count(sys._getframe())
        return function(*args, **kwargs)

    def recurse():
        if frame_count(sys._getframe()) < depth:
            return recurse()
        return inner()

    try:
        value = recurse()
        return {"site": box["site"], "value": value}
    except Exception as error:  # noqa: BLE001 旧版の例外の型と文面をそのまま記録する
        return {"site": box.get("site"), "error": error}


def typed(value):
    if value is None:
        return {"t": "none"}
    if value is True or value is False:
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        limit = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else 4300
        sys.set_int_max_str_digits(0)
        text = str(value)
        sys.set_int_max_str_digits(limit)
        return {"t": "int", "v": text}
    if isinstance(value, float):
        return {"t": "float", "v": repr(value)}
    if isinstance(value, str):
        return {"t": "str", "v": cp(value)}
    if isinstance(value, list):
        return {"t": "list", "v": [typed(v) for v in value]}
    if isinstance(value, dict):
        return {"t": "dict", "v": [[typed(k), typed(v)] for k, v in value.items()]}
    raise TypeError(type(value).__name__)


def outcome(result, summary=False):
    out = {"site": result["site"]}
    if "error" in result:
        error = result["error"]
        out["error"] = {"type": type(error).__name__, "msg": str(error)}
    else:
        out["ok"] = True if summary else typed(result["value"])
    return out


def utf8_surrogatepass(text):
    return text.encode("utf-8", "surrogatepass")


def json_text_pool():
    return [
        "{", "}", "[", "]", ",", ":", '"', "\\", "\\u", "\\ud83d", "\\ude00", "\\udc00", "\\u00e9", "\\uZZZZ", "\\n", "\\x", "null", "true", "false",
        "NaN", "Infinity", "-Infinity", "-", "0", "1", "12", "1.5", "1e5", "1E+", "1e", ".5", "01", "-0", "-0.0", " ", "\n", "\t", "\r", "\x00",
        "\x1f", "é", "\ud800", "\udc80", '"a"', '"a":1', '"a":', "1e400", "-1e400", "123456789012345678901234567890", "﻿", "あ", "\U0001f600",
        "nul", "tru", "fals", "Nan", "+1", "1.", "2e-3", " ", "\\/", '"\\ud83d"', '"\\ud83d\\ude00"', '"\\ud83d\\u0041"', "\\u12",
    ]


def json_valid_seeds():
    return [
        "{}", "[]", '{"a": 1}', '{"a":1,"a":2}', '{"a":1,"b":{"a":2,"a":3}}', "[1,2,3]", '[{"x":[1,{"y":null}]}]', '{"enabled":true}',
        '[{"id":"a","installPath":"/x"}]', '[{"id":"a","installPath":"/x","enabled":false},{"id":"b","installPath":"//y"}]',
        '[{"id":"a","installPath":"/x"},{"id":"a","installPath":"/y"}]', '[{"id":"","installPath":"/x"}]', '[{"id":"a","installPath":"x"}]',
        '[{"id":"a","installPath":"/x","enabled":1}]', '[{"id":"a","installPath":"/x","z":{"b":1,"a":[1.5,2e3]}}]', '"text"', "1", "-1", "1.5e3", "true",
        "null", "[NaN]", "[Infinity]", '{"a":-Infinity}', '{"a":NaN,"a":1}', '{"a":1,"a":NaN}', '["a\\nb", "\\u00e9\\ud83d\\ude00\\ud800"]',
        '{"é":1,"\\u00e9":2}', "[1e400,-1e400,1e-400]", '{"a":' + "9" * 700 + "}", '{"a":' + "9" * 4400 + "}", "[0,-0,0.0,-0.0,1E5,1e+5]",
        ' \n\t{"a" : [ 1 , 2 ] }\r\n', '{"a":1}x', '{"a":1}}', "[1,]", '{"a":1,}', '{"a" 1}', '{1:2}', "[1 2]", '["a', '"abc\\', '"\\u00', '"a\nb"',
        "﻿{}", "{}﻿", '{"env":{"A":"b"},"permissions":{"allow":["Bash(ls:*)"]}}', '{"a":[[[[[[1]]]]]]}',
    ]


def json_text_cases():
    r = rng("json")
    pool = json_text_pool()
    seeds = json_valid_seeds()
    texts = list(seeds)
    for _ in range(1200):
        texts.append("".join(r.choice(pool) for _ in range(r.randint(1, 9))))
    for _ in range(800):
        text = list(r.choice(seeds))
        for _ in range(r.randint(1, 3)):
            action = r.randint(0, 2)
            at = r.randint(0, len(text))
            if action == 0 and text:
                del text[min(at, len(text) - 1)]
            elif action == 1:
                text.insert(at, r.choice(pool))
            elif text:
                text[min(at, len(text) - 1)] = r.choice(pool)
        texts.append("".join(text))
    return texts


def reader_results(guard, raw, depth, limit, summary=False):
    sys.set_int_max_str_digits(limit)
    try:
        out = {
            "state": outcome(call_at(depth, json.loads, raw), summary),
            "strict": outcome(call_at(depth, guard.strict_settings, raw), summary),
        }
        inventory = call_at(depth, guard.plugin_inventory, raw)
        if "value" in inventory:
            inventory["value"] = list(inventory["value"])
        out["inventory"] = outcome(inventory, summary)
    finally:
        sys.set_int_max_str_digits(4300)
    return out


def decode_outcome(function, raw):
    try:
        return {"ok": cp(function(raw))}
    except UnicodeDecodeError as error:
        return {"error": {"type": "UnicodeDecodeError", "msg": str(error)}}


def byte_cases():
    r = rng("bytes")
    utf8_pool = [0x00, 0x41, 0x7F, 0x80, 0x9F, 0xA0, 0xBF, 0xC0, 0xC1, 0xC2, 0xDF, 0xE0, 0xE1, 0xED, 0xEE, 0xEF, 0xF0, 0xF1, 0xF4, 0xF5, 0xFF, 0x8F, 0x90, 0xBB, 0xBF, 0xBB]
    units = [0x0000, 0x0041, 0xD800, 0xDBFF, 0xDC00, 0xDFFF, 0xFFFF, 0xFEFF, 0xFFFE, 0x20AC, 0xD83D, 0xDE00, 0x007B, 0x007D]
    words = [0, 0x41, 0xD800, 0xDFFF, 0x10FFFF, 0x110000, 0xFFFFFFFF, 0xFEFF, 0x7B, 0x1F600, 0x80000000]
    cases = []
    for _ in range(2500):
        cases.append(bytes(r.choice(utf8_pool) for _ in range(r.randint(0, 9))))
    prefixes = [b"\xef\xbb\xbf", b"\xef\xbb\xbf\xef\xbb\xbf", b""]
    for _ in range(500):
        cases.append(r.choice(prefixes) + bytes(r.choice(utf8_pool) for _ in range(r.randint(0, 7))))
    for order in ("little", "big"):
        bom = b"\xff\xfe" if order == "little" else b"\xfe\xff"
        for _ in range(900):
            body = b"".join(r.choice(units).to_bytes(2, order) for _ in range(r.randint(0, 5)))
            if r.random() < 0.2:
                body += bytes([r.choice([0x00, 0x41, 0xFF])])
            cases.append((bom if r.random() < 0.7 else b"") + body)
        for _ in range(500):
            body = b"".join(r.choice(words).to_bytes(4, order) for _ in range(r.randint(0, 4)))
            if r.random() < 0.2:
                body += bytes(r.choice([0x00, 0x41]) for _ in range(r.randint(1, 3)))
            bom32 = b"\xff\xfe\x00\x00" if order == "little" else b"\x00\x00\xfe\xff"
            cases.append((bom32 if r.random() < 0.7 else b"") + body)
    for _ in range(800):
        cases.append(bytes(r.choice([0, 0, 0x41, 0x7B, 0x22, 0xFF]) for _ in range(r.randint(0, 7))))
    return cases


def depth_texts(kind, k):
    """入れ子 k 段の、正しい文と、誤りの位置が違う文。"""
    if kind == "array":
        opening, closing, leaf = "[" * k, "]" * k, ""
    else:
        opening, closing, leaf = '{"a":' * k, "}" * k, "1"
    return {
        "valid": opening + leaf + closing,
        "extra": opening + leaf + closing + "x",
        "bad_leaf": opening + "x" + closing,
        "unterminated": opening + leaf,
        "nan": opening + "NaN" + closing,
        "ninf": opening + "-Infinity" + closing,
        "inf": opening + "Infinity" + closing,
        "bad_key": opening.rsplit('{"a":', 1)[0] + "{x" if kind == "object" else opening + ",",
    }


def depth_cases(guard):
    cases = []
    sites = [5, 100, 500, 900, 950, 980, 985, 986, 987, 988, 989, 990, 991, 992, 993, 994, 995, 996, 997, 998, 999]
    for site in sites:
        base = 1000 - (site + 4)
        for kind in ("array", "object"):
            ks = {0, 1, 2, 3, 50, 99, 100, 101, 102, base - 3, base - 2, base - 1, base, base + 1, base + 2}
            for k in sorted(k for k in ks if 0 <= k <= 1100):
                for shape, text in depth_texts(kind, k).items():
                    if k == 0 and shape not in ("nan", "ninf", "inf"):
                        continue
                    if shape != "valid" and k not in (0, 1, 2, 101, base - 1, base, base + 1):
                        continue
                    raw = text.encode()
                    case = {"site": site, "kind": kind, "k": k, "shape": shape}
                    case.update(reader_results(guard, raw, site, 4300, summary=True))
                    if any(case[name]["site"] is None for name in ("state", "strict", "inventory")):
                        continue
                    cases.append(case)
    # 有効な項目を持つ一覧(並べ替えの鍵の `json.dumps` の深さ)。
    for site in range(975, 1000):
        for shape in ("one", "two", "pair", "nested1", "nested3"):
            if shape == "one":
                text = '[{"id":"a","installPath":"/x"}]'
            elif shape == "two":
                text = '[{"id":"a","installPath":"/x"},{"id":"b","installPath":"/y"}]'
            elif shape == "pair":
                text = '[{"id":"a","installPath":"/x","z":1}]'
            elif shape == "nested1":
                text = '[{"id":"a","installPath":"/x","z":[1]}]'
            else:
                text = '[{"id":"a","installPath":"/x","z":[[[1]]]}]'
            case = {"site": site, "kind": "inventory", "k": 0, "shape": shape}
            case.update(reader_results(guard, text.encode(), site, 4300, summary=True))
            if any(case[name]["site"] is None for name in ("state", "strict", "inventory")):
                continue
            cases.append(case)
    return cases


def _dumps_state(value, box):
    box["site"] = frame_count(sys._getframe())
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _dumps_receipt(value, box):
    box["site"] = frame_count(sys._getframe())
    return json.dumps(value)


def _dumps_allowlist(value, box):
    box["site"] = frame_count(sys._getframe())
    return json.dumps(value, ensure_ascii=False)


DUMPS_SHAPES = {"state": _dumps_state, "receipt": _dumps_receipt, "allowlist": _dumps_allowlist, "empty": _dumps_state, "state2": _dumps_state}


def dumps_depth_cases():
    cases = []
    for site in [5, 100, 500, 900, 990, 993, 994, 995, 996, 997, 998, 999]:
        base = 1000 - (site + 4)
        for kind in ("array", "object"):
            ks = {1, 2, 3, base - 2, base - 1, base, base + 1, base + 2}
            for k in sorted(k for k in ks if 1 <= k <= 1100):
                value = 1
                for _ in range(k):
                    value = [value] if kind == "array" else {"a": value}
                for shape, function in DUMPS_SHAPES.items():
                    target = value
                    if shape == "state2":
                        target = 1
                        for _ in range(k):
                            target = [target, 1] if kind == "array" else {"a": target, "b": 1}
                    if shape == "empty":
                        target = [] if kind == "array" else {}
                        for _ in range(k - 1):
                            target = [target] if kind == "array" else {"a": target}
                    result = call_at(site, function, target, boxed=True)
                    out = {"site": site, "kind": kind, "k": k, "shape": shape}
                    out.update(outcome(result, summary=True))
                    if out["site"] is None:
                        continue
                    cases.append(out)
    for site in (5, 996, 997, 998, 999):
        for value in ("text", 1, None, [], {}):
            for shape in ("state", "receipt", "allowlist"):
                result = call_at(site, DUMPS_SHAPES[shape], value, boxed=True)
                out = {"site": site, "shape": shape, "scalar": typed(value)}
                out.update(outcome(result, summary=True))
                if out["site"] is None:
                    continue
                cases.append(out)
    return cases


def _dumps_host(value, box):
    box["site"] = frame_count(sys._getframe())
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _dumps_sortkey(value, box):
    box["site"] = frame_count(sys._getframe())
    return json.dumps(value, sort_keys=True)


DUMPS_FORMS = {"state": _dumps_state, "receipt": _dumps_receipt, "host": _dumps_host, "allowlist": _dumps_allowlist, "sortkey": _dumps_sortkey}
EQUALITY_SEEDS = ["null", "true", "false", "0", "1", "-0", "1.0", "-0.0", "0.0", "1e0", "2", "NaN", "Infinity", "-Infinity", '"1"', '""', "[]", "{}", "[1]", "[1.0]",
                  "[true]", "[NaN]", "[NaN, NaN]", '{"a":1}', '{"a":1.0}', '{"a":true}', '{"a":1,"b":2}', '{"b":2,"a":1}', '{"a":NaN}', "[[1]]", "[[1.0]]",
                  "100000000000000000000", "1e20", "100000000000000000001", "9007199254740993", "9007199254740992.0", '{"é":1}', '{"\\u00e9":1}']


def dump_outcomes(raw):
    out = {}
    value = json.loads(raw)
    for name, function in DUMPS_FORMS.items():
        result = call_at(20, function, value, boxed=True)
        entry = {"site": result["site"]}
        if "error" in result:
            entry["error"] = {"type": type(result["error"]).__name__, "msg": str(result["error"])}
        else:
            entry["ok"] = cp(result["value"])
        out[name] = entry
    return out


@area("json")
def make_json():
    guard = load_guard()
    text_cases = []
    values = []
    seeds_count = len(json_valid_seeds())
    for index, text in enumerate(json_text_cases()):
        raw = utf8_surrogatepass(text)
        case = {"raw": raw.hex()}
        case.update(reader_results(guard, raw, 20, 4300))
        if "ok" in case["state"] and (index < seeds_count or len(values) < 700):
            case["dumps"] = dump_outcomes(raw)
            values.append(raw)
        text_cases.append(case)
    pairs = []
    r = rng("equality")
    seed_raws = [text.encode() for text in EQUALITY_SEEDS]
    pool = [(a, b) for a in seed_raws for b in seed_raws] + [(r.choice(values), r.choice(values)) for _ in range(900)]
    for a, b in pool:
        left, right = json.loads(a), json.loads(b)
        pairs.append({"a": a.hex(), "b": b.hex(), "eq": left == right, "ne": left != right})
    encoded_cases = []
    r = rng("json-enc")
    for text in r.sample(json_valid_seeds(), 30) + ["[1,2]", '{"a":"é\\ud83d"}', '[1e400]', '{"a":1,"a":2}', "[NaN]"]:
        for codec in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
            try:
                raw = text.encode(codec, "surrogatepass")
            except UnicodeError:
                continue
            case = {"raw": raw.hex()}
            case.update(reader_results(guard, raw, 20, 4300))
            encoded_cases.append(case)
    limit_cases = []
    for limit in (640, 0):
        for text in ["[" + "9" * 641 + "]", '{"a":' + "9" * 640 + "}", "[" + "9" * 700 + ",1]", "1" * 5000, "[1e999]", "-" + "9" * 700]:
            raw = text.encode()
            case = {"raw": raw.hex(), "limit": limit}
            case.update(reader_results(guard, raw, 20, limit))
            limit_cases.append(case)
    decode_cases = []
    for raw in byte_cases():
        decode_cases.append({
            "raw": raw.hex(),
            "json": decode_outcome(lambda b: b.decode(json.detect_encoding(b), "surrogatepass"), raw),
            "utf8": decode_outcome(lambda b: b.decode("utf-8"), raw),
        })
    return {"json_text_cases.json": text_cases, "json_encoded_cases.json": encoded_cases, "json_limit_cases.json": limit_cases,
            "json_decode_cases.json": decode_cases, "json_equality_cases.json": pairs, "json_depth_cases.json": {"loads": depth_cases(guard), "dumps": dumps_depth_cases()}}


# ---------------------------------------------------------------- stdout

def stream_cases():
    import errno as errno_module
    import hashlib
    import io
    import os

    class Recorder(io.RawIOBase):
        def __init__(self, capacity, code):
            self.capacity = capacity
            self.code = code
            self.writes = []
            self.data = bytearray()

        def writable(self):
            return True

        def write(self, chunk):
            chunk = bytes(chunk)
            if self.capacity is not None and len(self.data) >= self.capacity and chunk:
                if self.code is None:
                    return None  # 非 block の出力先が満杯のとき、FileIO.write は None を返す。
                raise OSError(self.code, os.strerror(self.code))
            count = len(chunk) if self.capacity is None else min(len(chunk), self.capacity - len(self.data))
            self.data += chunk[:count]
            self.writes.append(count)
            return count

    r = rng("stream")
    sizes = [0, 1, 2, 7, 100, 1000, 4095, 4096, 4097, 8191, 8192, 8193, 12000, 20000]
    chars = ["a", "あ", "\n", "é", "\r"]

    def make_ops():
        ops = []
        for _ in range(r.randint(1, 6)):
            kind = r.choice(["w", "p", "p", "bw", "flush", "w"])
            size = r.choice(sizes)
            if kind in ("w", "p"):
                unit = r.choice([[97], [97], [0x3042], [97, 10], [0xe9], [97, 13], [10]])
                count = size if len(unit) == 1 else size // 2
                if r.random() < 0.06:
                    unit = r.choice([[0xd800], [0xdc80, 0xdcff], [120, 0xdc80], [0xdc80, 0xd800], [0xdcff, 0xdc80, 121], [97, 0xd800, 0xd801, 98]])
                    count = r.choice([1, 1, 2, 3])
                ops.append([kind, unit, min(count, 9000)])
            elif kind == "bw":
                ops.append([kind, r.randint(0, 255), size])
            else:
                ops.append([kind])
        return ops

    cases = []
    for _ in range(2600):
        stream_kind = r.choice(["stdout", "stdout", "stderr"])
        unbuffered = r.random() < 0.25
        block = r.choice([4096, 4096, 8192, 1024, 512])
        capacity = r.choice([None, None, 0, 0, 1, 100, 4096, 5000, 8192, 10000, 30000])
        code = r.choice([errno_module.EPIPE, errno_module.ENOSPC, None])
        tty = stream_kind == "stdout" and r.random() < 0.25
        ops = make_ops()
        raw = Recorder(capacity, code)
        errors = "surrogateescape" if stream_kind == "stdout" else "backslashreplace"
        if unbuffered:
            buffer = raw
            stream = io.TextIOWrapper(buffer, encoding="utf-8", errors=errors, newline="\n", line_buffering=False, write_through=True)
        else:
            buffer = io.BufferedWriter(raw, buffer_size=block)
            stream = io.TextIOWrapper(buffer, encoding="utf-8", errors=errors, newline="\n", line_buffering=(stream_kind == "stderr" or tty), write_through=False)
        results = []
        for op in ops:
            try:
                if op[0] == "w":
                    stream.write(text_of_cps(op[1]) * op[2])
                elif op[0] == "p":
                    stream.write(text_of_cps(op[1]) * op[2])
                    stream.write("\n")
                elif op[0] == "bw":
                    buffer.write(bytes([op[1]]) * op[2])
                else:
                    stream.flush()
                results.append(None)
            except Exception as error:  # noqa: BLE001
                results.append({"type": type(error).__name__, "msg": str(error)})
        finals = []
        for _ in range(2):
            try:
                stream.flush()
                finals.append(None)
            except Exception as error:  # noqa: BLE001
                finals.append({"type": type(error).__name__, "msg": str(error)})
        cases.append({
            "stream": stream_kind, "unbuffered": unbuffered, "tty": tty, "block": block, "capacity": capacity, "errno": code, "ops": ops,
            "results": results, "finals": finals, "writes": run_length(raw.writes), "data_sha256": hashlib.sha256(bytes(raw.data)).hexdigest(),
            "data_len": len(raw.data),
        })
    return cases


def run_length(values):
    out = []
    for value in values:
        if out and out[-1][0] == value:
            out[-1][1] += 1
        else:
            out.append([value, 1])
    return out


def text_of_cps(cps):
    return "".join(chr(c) for c in cps)


@area("stdout")
def make_stdout():
    return {"stream_cases.json": stream_cases()}


# ---------------------------------------------------------------- argparse

ARG_COMMANDS = ["bootstrap", "verify", "exec", "read", "hook", "verify-pair", "host-settings", "host-allowlist", "host-paths"]
ARG_FLAGS = ["--root", "--output", "--inventory", "--setting", "--config", "--shell", "--skills-dir", "--settings-dir", "--managed-dir", "--state",
             "--expect-sha256", "--current-inventory", "--path", "--guard-sha256", "--go-timeout", "--allowed-tools", "-h", "--help"]
ARG_COLUMNS = [None, "", "1", "5", "10", "11", "20", "21", "22", "30", "40", "50", "60", "70", "79", "80", "81", "90", "100", "120", "200", "1000",
               "abc", "0", "-5", " 70 ", "٧٠", "1_00", "+80", "　100　", "99999999999999999999999"]


def argparse_cases():
    import io
    import os
    import shutil
    import argparse

    if sys.__stdout__.isatty():
        raise SystemExit("端末の幅を読まないよう、標準出力を端末にしないで流す")
    guard = load_guard()
    captured = {}
    original_parse_args = argparse.ArgumentParser.parse_args

    def spy(self, *args, **kwargs):
        namespace = original_parse_args(self, *args, **kwargs)
        captured["namespace"] = dict(vars(namespace))
        return namespace

    argparse.ArgumentParser.parse_args = spy
    guard.bootstrap = lambda a: None
    guard.run_pair = lambda a: 0
    guard.host_paths = lambda: {}
    guard.read_host_settings = lambda s, e: {"user_settings": "", "checked": []}
    guard.host_allowlist = lambda s, e, v: ([], [])
    guard.run_copy = lambda a: 0

    def run_case(raw_args, columns):
        captured.clear()
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="surrogateescape", newline="\n")
        err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="backslashreplace", newline="\n")
        saved = (sys.stdout, sys.stderr, sys.argv, os.environ.get("COLUMNS"))
        sys.stdout, sys.stderr = out, err
        sys.argv = ["environment-guard.py"] + [os.fsdecode(a) for a in raw_args]
        if columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = os.fsdecode(columns)
        code = None
        try:
            guard.main()
        except SystemExit as error:
            code = error.code
        finally:
            sys.stdout, sys.stderr, sys.argv = saved[0], saved[1], saved[2]
            if saved[3] is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = saved[3]
        out.flush()
        err.flush()
        case = {
            "args": [a.hex() for a in raw_args],
            "args_text": [os.fsdecode(a)[:60] for a in raw_args].__repr__().encode("ascii", "backslashreplace").decode(),
            "columns": cp(os.fsdecode(columns)) if columns is not None else None,
            "columns_text": repr(columns),
        }
        if code is None:
            case["exit"] = None
            case["namespace"] = {k: typed_arg(v) for k, v in sorted(captured["namespace"].items())}
        else:
            case["exit"] = code
            for name, buffer in (("stdout", out.buffer.getvalue()), ("stderr", err.buffer.getvalue())):
                try:
                    case[name] = buffer.decode("utf-8")
                except UnicodeDecodeError:
                    case[name + "_hex"] = buffer.hex()
        return case

    cases = []
    for command in [None] + ARG_COMMANDS:
        for columns in ARG_COLUMNS:
            for flag in ("-h",):
                args = ([command] if command else []) + [flag]
                cases.append(run_case([a.encode() for a in args], columns.encode() if columns is not None else None))
    r = rng("argparse")
    value_pool = ["x", "", "--", "-", "-5", "-1.5", "-.5", "-٥", "-5\n", "a b", "--bogus", "-x", "-hx", "-hh", "-hfoo", "5", "٥", "python3",
                  "bash", "é", "abc", "-0", "--he", "--s", "--st", "--st=", "--state=", "--state=--", "--root=--", "--=", "---", "-h=", "--h", "--help=x",
                  "1e3", "�", "9" * 5000, "-" + "9" * 5000, " 7 ", "1_0", "+3", "0x10"]
    for _ in range(1500):
        parts = []
        if r.random() < 0.8:
            parts.append(r.choice(ARG_COMMANDS))
        for _ in range(r.randint(0, 9)):
            kind = r.random()
            if kind < 0.45:
                flag = r.choice(ARG_FLAGS)
                parts.append(flag)
            elif kind < 0.55:
                flag = r.choice(ARG_FLAGS)
                parts.append(flag + "=" + r.choice(["v", "", "--", "5"]))
            elif kind < 0.62:
                flag = r.choice(ARG_FLAGS)
                parts.append(flag[: r.randint(2, len(flag))])
            elif kind < 0.67:
                parts.append(r.choice(ARG_COMMANDS))
            else:
                parts.append(r.choice(value_pool))
        raw = [p.encode("utf-8", "surrogateescape") for p in parts]
        if r.random() < 0.05 and raw:
            raw[r.randrange(len(raw))] = bytes(r.choice([0xFF, 0xC3, 0x41, 0xED, 0xA0, 0x80]) for _ in range(r.randint(1, 4)))
        columns = r.choice([None, None, b"80", b"20", b"60", b"120", b"15", b"45"])
        cases.append(run_case(raw, columns))
    # 成功と失敗の両方の割合を保つため、確実に正しい文も入れる。
    valid = [
        ["bootstrap", "--root", "/r", "--output", "/o"],
        ["bootstrap", "--root=/r", "--output=/o", "--setting", "a", "--setting=b", "--config", "c", "--shell", "s", "--skills-dir", "k", "--settings-dir", "d", "--managed-dir", "m", "--inventory", "i"],
        ["verify", "--state", "s", "--expect-sha256", "h"],
        ["verify", "--stat", "s", "--expect-sha256=h", "--current-inventory", "c"],
        ["read", "--state", "s", "--expect-sha256", "h", "--path", "p"],
        ["exec", "--state", "s", "--expect-sha256", "h", "--path", "p"],
        ["exec", "--state", "s", "--expect-sha256", "h", "--path", "p", "--", "python3", "--x"],
        ["exec", "--state", "s", "--expect-sha256", "h", "--path", "p", "python3", "-B", "--state", "z"],
        ["exec", "--state", "s", "--expect-sha256", "h", "--path", "p", "bash", "--bogus"],
        ["hook", "--state", "s", "--expect-sha256", "h", "--path", "p"],
        ["verify-pair", "--state", "s", "--expect-sha256", "h", "--current-inventory", "c", "--guard-sha256", "g", "--go-timeout", "5"],
        ["verify-pair", "--state", "s", "--expect-sha256", "h", "--current-inventory", "c", "--guard-sha256", "g", "--go-timeout", " -٥_0 "],
        ["verify-pair", "--state", "s", "--expect-sha256", "h", "--current-inventory", "c", "--guard-sha256", "g", "--go-timeout=--"],
        ["verify-pair", "--state", "s", "--expect-sha256", "h", "--current-inventory", "c", "--guard-sha256", "g", "--go-timeout", "x"],
        ["verify-pair", "--state", "s", "--expect-sha256", "h", "--current-inventory", "c", "--guard-sha256", "g", "--go-timeout", "9" * 4301],
        ["verify-pair", "--state", "s", "--expect-sha256", "h", "--current-inventory", "c", "--guard-sha256", "g", "--go-timeout", "9" * 4300],
        ["host-settings", "--state", "s", "--expect-sha256", "h"],
        ["host-allowlist", "--state", "s", "--expect-sha256", "h", "--allowed-tools", "Bash(ls)", "--allowed-tools=Bash(cat)", "--allowed-tools=--"],
        ["host-paths"],
        ["verify", "--state=--", "--expect-sha256", "h"],
        ["verify", "--state", "--", "--expect-sha256", "h"],
        ["--", "verify", "--state", "s", "--expect-sha256", "h"],
        ["verify", "--", "--state", "s", "--expect-sha256", "h"],
    ]
    for args in valid:
        for columns in (None, b"40"):
            cases.append(run_case([a.encode() for a in args], columns))
    argparse.ArgumentParser.parse_args = original_parse_args
    return cases


def typed_arg(value):
    if value is None:
        return {"t": "none"}
    if isinstance(value, list):
        return {"t": "list", "v": [typed_arg(v) for v in value]}
    if isinstance(value, int):
        sys.set_int_max_str_digits(0)
        text = str(value)
        sys.set_int_max_str_digits(4300)
        return {"t": "int", "v": text}
    return {"t": "str", "v": cp(value)}


@area("argparse")
def make_argparse():
    return {"argparse_cases.json": argparse_cases()}


# ---------------------------------------------------------------- main の出力

MAIN_DRIVER = r"""
import sys

path, spec, *argv = sys.argv[1:]
source = open(path, encoding="utf-8").read()
head = source.rsplit("\nif __name__ == '__main__':", 1)[0]
namespace = {"__name__": "__main__", "__file__": path}
exec(compile(head, path, "exec"), namespace)
Stop = namespace["Stop"]


def boom(*args, **kwargs):
    kind, _, detail = spec.partition(":")
    if kind == "ok":
        return {}
    if kind == "stop":
        raise Stop(detail)
    if kind == "oserror":
        number, _, name = detail.partition(":")
        raise OSError(int(number), __import__("os").strerror(int(number)), name or None)
    if kind == "value":
        raise ValueError(detail)
    if kind == "key":
        raise KeyError(detail)
    if kind == "type":
        raise TypeError(detail)
    if kind == "recursion":
        raise RecursionError(detail)
    if kind == "index":
        raise IndexError(detail)
    if kind == "attr":
        raise AttributeError(detail)
    if kind == "overflow":
        raise OverflowError(detail)
    if kind == "zero":
        raise ZeroDivisionError(detail)
    if kind == "runtime":
        raise RuntimeError(detail)
    if kind == "unicode":
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    if kind == "timeout":
        raise namespace["subprocess"].TimeoutExpired("cmd", 1)
    if kind == "chain-context":
        try:
            raise KeyError("a")
        except KeyError:
            raise IndexError("b")
    if kind == "chain-cause":
        raise IndexError("b") from KeyError("a")
    if kind == "chain-none":
        try:
            raise KeyError("a")
        except KeyError:
            raise IndexError("b") from None
    if kind == "chain-caught":
        try:
            raise KeyError("a")
        except KeyError:
            raise ValueError("v")
    raise SystemExit(f"bad spec {spec}")


def code(*args, **kwargs):
    kind, _, detail = spec.partition(":")
    return int(detail) if kind == "code" else boom()


for name in ("bootstrap", "run_copy", "run_pair", "host_paths", "read_host_settings", "host_allowlist"):
    namespace[name] = boom if not spec.startswith("code:") else code
sys.argv = ["environment-guard.py"] + argv
if spec.startswith("ok"):
    namespace["read_host_settings"] = lambda s, e: {"user_settings": "u", "checked": []}
    namespace["host_allowlist"] = lambda s, e, v: ([{"kind": "exact", "words": ["x"]}], ["a(b)"])
    namespace["host_paths"] = lambda: {"home": "/h"}
    namespace["bootstrap"] = lambda a: None
    namespace["run_copy"] = lambda a: 0
    namespace["run_pair"] = lambda a: 0
raise SystemExit(namespace["main"]())
"""

MAIN_SPECS = ["ok", "stop:理由", "stop:host-config-path", "stop:hooks-disabled", "oserror:2:/x", "oserror:13", "value:v", "key:k", "type:t", "recursion:r",
              "index:i", "attr:a", "overflow:o", "zero:z", "runtime:r", "unicode", "timeout", "chain-context", "chain-cause", "chain-none", "chain-caught",
              "code:0", "code:20", "code:-9", "code:256", "code:3"]
MAIN_COMMANDS = [["verify", "--state", "s", "--expect-sha256", "h"], ["host-paths"], ["host-allowlist", "--state", "s", "--expect-sha256", "h"]]
MAIN_MODES = ["normal", "stdout-full", "stderr-full", "both-full", "stdout-closed"]


def normalize_traceback(text):
    """U4: 見出しと最後の行の間の途中の行(ファイルの所在とソースの行)を除く。"""
    lines = text.split("\n")
    out = []
    in_traceback = False
    for line in lines:
        if line.startswith("Traceback (most recent call last):"):
            in_traceback = True
            out.append(line)
        elif in_traceback and line.startswith("  "):
            continue
        else:
            in_traceback = False
            out.append(line)
    return "\n".join(out)


def main_cases():
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        driver = Path(folder) / "driver.py"
        driver.write_text(MAIN_DRIVER, encoding="utf-8")
        cases = []
        for command in MAIN_COMMANDS:
            for spec in MAIN_SPECS:
                if spec.startswith("code:") and command[0] != "verify":
                    continue
                for mode in MAIN_MODES:
                    for unbuffered in (False, True):
                        env = {"PATH": os.environ["PATH"], "LC_ALL": "C.UTF-8", "COLUMNS": "80"}
                        if unbuffered:
                            env["PYTHONUNBUFFERED"] = "1"
                        stdout_target, stderr_target = subprocess.PIPE, subprocess.PIPE
                        files = []
                        if mode in ("stdout-full", "both-full"):
                            stdout_target = open("/dev/full", "wb")
                            files.append(stdout_target)
                        if mode in ("stderr-full", "both-full"):
                            stderr_target = open("/dev/full", "wb")
                            files.append(stderr_target)
                        if mode == "stdout-closed":
                            read_end, write_end = os.pipe()
                            os.close(read_end)
                            stdout_target = write_end
                        try:
                            result = subprocess.run([sys.executable, "-B", str(driver), str(GUARD_PATH), spec] + command, stdout=stdout_target,
                                                    stderr=stderr_target, stdin=subprocess.DEVNULL, env=env, timeout=60)
                        finally:
                            for handle in files:
                                handle.close()
                            if mode == "stdout-closed":
                                os.close(write_end)
                        case = {"command": command, "spec": spec, "mode": mode, "unbuffered": unbuffered, "code": result.returncode}
                        for name, data in (("stdout", result.stdout), ("stderr", result.stderr)):
                            data = data or b""
                            try:
                                text = normalize_traceback(data.decode("utf-8"))
                                case[name] = text
                            except UnicodeDecodeError:
                                case[name + "_hex"] = data.hex()
                        cases.append(case)
        return cases


@area("main")
def make_main():
    return {"main_cases.json": main_cases()}


# ---------------------------------------------------------------- 環境変数

def env_cases():
    import subprocess

    r = rng("env")
    values = ["", "0", "00", "+0", "-0", " 0", "0 ", "\t0", "1", "-1", "10", "639", "640", "641", "4300", "700", "00700", "+700", " 640", "640 ", "\n640",
              "\x0b640", "\x0c640", "　640", "1_000", "0x10", "abc", "0abc", "x0", "+", "-", " ", "  ", "2147483647", "2147483648", "-2147483648",
              "99999999999999", "999999999999999999999", "0000000000000000000000640", "6\x0040", "٦٤٠", "640\x00", "1e3", "1.0"]
    for _ in range(150):
        values.append("".join(r.choice("0123456789 +-_x\t") for _ in range(r.randint(1, 6))))
    out = {"digits": [], "unbuffered": []}
    base = {"PATH": os.environ["PATH"], "LC_ALL": "C.UTF-8"}
    for value in values:
        if "\x00" in value:
            continue
        env = dict(base, PYTHONINTMAXSTRDIGITS=value)
        result = subprocess.run([sys.executable, "-B", "-c", "import sys;print(sys.get_int_max_str_digits())"], env=env, capture_output=True, timeout=30)
        entry = {"value": value.encode("utf-8", "surrogateescape").hex(), "code": result.returncode}
        if result.returncode == 0:
            entry["ok"] = int(result.stdout)
        else:
            entry["stderr"] = result.stderr.decode()
        out["digits"].append(entry)
        env = dict(base, PYTHONUNBUFFERED=value)
        result = subprocess.run([sys.executable, "-B", "-c", "import sys;print(sys.stdout.write_through)"], env=env, capture_output=True, timeout=30)
        out["unbuffered"].append({"value": value.encode("utf-8", "surrogateescape").hex(), "write_through": result.stdout.strip() == b"True"})
    return out


@area("env")
def make_env():
    return {"env_cases.json": env_cases()}


@area("errno")
def make_errno():
    import os

    cases = []
    for number in range(1, 200):
        text = os.strerror(number)
        error = OSError(number, text)
        cases.append({"errno": number, "type": type(error).__name__, "text": text, "str": str(error)})
    return {"errno_cases.json": cases}


# ---------------------------------------------------------------- main の後の flush・端末・非 block

FLUSH_DRIVER = r"""
import sys

path, size, result = sys.argv[1:4]
source = open(path, encoding="utf-8").read()
head = source.rsplit("\nif __name__ == '__main__':", 1)[0]
namespace = {"__name__": "__main__", "__file__": path}
exec(compile(head, path, "exec"), namespace)


def copy(args):
    print("x" * int(size))
    if result == "uncaught":
        raise IndexError("i")
    return 20 if result == "code20" else 0


namespace["run_copy"] = copy
sys.argv = ["environment-guard.py", "verify", "--state", "s", "--expect-sha256", "h"]
raise SystemExit(namespace["main"]())
"""

FLUSH_SIZES = [100, 3000, 4096, 4097, 5000, 8191, 8192, 9000]


def run_driver(driver, extra_args, stdout, stderr, env_extra=None, pass_fds=()):
    import subprocess

    env = {"PATH": os.environ["PATH"], "LC_ALL": "C.UTF-8", "COLUMNS": "80"}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-B", str(driver)] + extra_args, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL, env=env, timeout=60,
                          pass_fds=pass_fds)


def record(case, result):
    case["code"] = result.returncode
    for name, data in (("stdout", result.stdout), ("stderr", result.stderr)):
        data = data or b""
        try:
            case[name] = normalize_traceback(data.decode("utf-8"))
        except UnicodeDecodeError:
            case[name + "_hex"] = data.hex()


def flush_cases(folder):
    import subprocess

    driver = Path(folder) / "flush_driver.py"
    driver.write_text(FLUSH_DRIVER, encoding="utf-8")
    cases = []
    for size in FLUSH_SIZES:
        for result in ("ok", "code20", "uncaught"):
            for mode in ("pipe", "same-fd", "stdout-full", "stdout-closed"):
                args = [str(GUARD_PATH), str(size), result]
                files = []
                try:
                    if mode == "pipe":
                        done = run_driver(driver, args, subprocess.PIPE, subprocess.PIPE)
                    elif mode == "same-fd":
                        done = run_driver(driver, args, subprocess.PIPE, subprocess.STDOUT)
                    elif mode == "stdout-full":
                        handle = open("/dev/full", "wb")
                        files.append(handle)
                        done = run_driver(driver, args, handle, subprocess.PIPE)
                    else:
                        read_end, write_end = os.pipe()
                        os.close(read_end)
                        try:
                            done = run_driver(driver, args, write_end, subprocess.PIPE)
                        finally:
                            os.close(write_end)
                finally:
                    for handle in files:
                        handle.close()
                case = {"size": size, "result": result, "mode": mode}
                record(case, done)
                cases.append(case)
    return cases


def tty_cases(folder):
    import pty
    import select
    import subprocess

    driver = Path(folder) / "driver.py"
    driver.write_text(MAIN_DRIVER, encoding="utf-8")
    cases = []
    commands = [["host-allowlist", "--state", "s", "--expect-sha256", "h"], ["host-paths"]]
    for command in commands:
        for spec in ("ok", "key:k", "index:i", "stop:x"):
            for mode in ("open", "closed-master", "full"):
                for unbuffered in (False, True):
                    master, slave = pty.openpty()
                    if mode == "closed-master":
                        # 切れた端末は、Python から見ると端末でない(isatty が偽)。
                        os.close(master)
                    if mode == "full":
                        # 端末のまま、書込みが EAGAIN になる(満杯で、非 block)。
                        os.set_blocking(slave, False)
                        try:
                            while True:
                                os.write(slave, b"x" * 512)
                        except BlockingIOError:
                            pass
                    env_extra = {"PYTHONUNBUFFERED": "1"} if unbuffered else None
                    done = run_driver(driver, [str(GUARD_PATH), spec] + command, slave, subprocess.PIPE, env_extra)
                    os.close(slave)
                    if mode == "full":
                        os.close(master)
                    data = b""
                    if mode == "open":
                        while select.select([master], [], [], 0.05)[0]:
                            try:
                                chunk = os.read(master, 65536)
                            except OSError:
                                break
                            if not chunk:
                                break
                            data += chunk
                        os.close(master)
                    done.stdout = data.replace(b"\r\n", b"\n")
                    case = {"command": command, "spec": spec, "mode": mode, "unbuffered": unbuffered}
                    record(case, done)
                    cases.append(case)
    return cases


def block_cases(folder):
    import subprocess

    driver = Path(folder) / "driver.py"
    driver.write_text(MAIN_DRIVER, encoding="utf-8")
    cases = []
    commands = [["host-allowlist", "--state", "s", "--expect-sha256", "h"], ["host-paths"], ["verify", "--state", "s", "--expect-sha256", "h"]]
    for command in commands:
        for spec in ("ok", "key:k", "index:i", "stop:x"):
            for mode in ("stdout-block", "stderr-block", "both-block"):
                for unbuffered in (False, True):
                    handles = []
                    targets = {}
                    for name in ("stdout", "stderr"):
                        if mode.startswith(name) or mode == "both-block":
                            read_end, write_end = os.pipe()
                            os.set_blocking(write_end, False)
                            try:
                                while True:
                                    os.write(write_end, b"x" * 4096)
                            except BlockingIOError:
                                pass
                            handles += [read_end, write_end]
                            targets[name] = write_end
                        else:
                            targets[name] = subprocess.PIPE
                    env_extra = {"PYTHONUNBUFFERED": "1"} if unbuffered else None
                    try:
                        done = run_driver(driver, [str(GUARD_PATH), spec] + command, targets["stdout"], targets["stderr"], env_extra)
                    finally:
                        for handle in handles:
                            os.close(handle)
                    case = {"command": command, "spec": spec, "mode": mode, "unbuffered": unbuffered}
                    record(case, done)
                    cases.append(case)
    return cases


@area("main2")
def make_main2():
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        return {"main_flush_cases.json": flush_cases(folder), "main_tty_cases.json": tty_cases(folder), "main_block_cases.json": block_cases(folder)}


def serialize(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(HERE))
    parser.add_argument("--only")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    names = args.only.split(",") if args.only else list(AREAS)
    status = 0
    for name in names:
        for filename, value in AREAS[name]().items():
            path = Path(args.out) / filename
            text = serialize(value)
            if args.check:
                if not path.exists() or path.read_text(encoding="utf-8") != text:
                    print("不一致: " + str(path))
                    status = 1
            else:
                path.write_text(text, encoding="utf-8")
                print("作成: %s (%d バイト)" % (path, len(text)))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
