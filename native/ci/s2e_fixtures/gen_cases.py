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


# ---------------------------------------------------------------- 段階 2a(記述子・設定・一覧・控えの値)

EMPTY = []  # 旧版の `[]`(`--x=--` の値)。


def with_env(env, cwd, function):
    """環境変数と作業場所を一時的に差し替えて `function` を呼ぶ。"""
    import os

    saved_env, saved_cwd = dict(os.environ), os.getcwd()
    os.environ.clear()
    os.environ.update(env)
    os.chdir(cwd)
    try:
        return function()
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        os.chdir(saved_cwd)


def result_of(function):
    try:
        return {"ok": typed(function())}
    except Exception as error:  # noqa: BLE001
        return {"error": {"type": type(error).__name__, "msg": str(error)}}


def pick(r, pool):
    return r.choice(pool)


def default_env(r):
    env = {}
    pools = {
        "HOME": ["/h", "/h", "/h", "/h/", "/home/u", "//h", "/h/./x//", "~", "", "rel", "/h/..", "/h/\u00e9", "/h/e\u0301", "/h/\udc80", None],
        "CLAUDE_CONFIG_DIR": [None, None, "/c", "/c/", "", "~/c", "/c/./d", "/c/..", "//c", "rel"],
        "XDG_CONFIG_HOME": [None, None, "", "/x", "rel", "~/x", "~root/x", "/x/"],
        "BASH_ENV": [None, None, "", "/b", "rel", "~/b"],
        "ENV": [None, None, "", "/e", "rel"],
        "ZDOTDIR": [None, None, "", "/z", "rel", "~root/z"],
        "GIT_CONFIG_GLOBAL": [None, None, "", "/g", "rel", "~/g"],
        "GIT_CONFIG_SYSTEM": [None, None, "", "/s", "rel"],
        "GIT_CONFIG_NOSYSTEM": [None, None, "1", "true", "yes", "0", "", "TRUE"],
    }
    for name, pool in pools.items():
        value = pick(r, pool)
        if value is not None:
            env[name] = value
    return env


def specs_cases():
    import argparse
    import copy
    import os

    guard = load_guard()
    r = rng("specs")
    out = {"host_path": [], "host_paths": [], "default_specs": [], "bootstrap": []}
    values = ["/a", "/a/", "//a", "///a", "/a/./b//c", "/a/../b", "..", "/..", "a", "", "~", "~/a", "/\u00e9", "/e\u0301", "/a/\udc80", "/", "/a b/", " /a",
              None, 1, True, [], {}, 1.5, ["/a"]]
    for value in values:
        out["host_path"].append({"value": typed(value), **result_of(lambda: guard.host_path(value, "x"))})
    envs = [{"HOME": h} for h in ["/h", "/h/", "~", "", "rel", "/h/..", "//h", "/h/e\u0301", "/h/\udc80"]]
    for config in ["/c", "", "~", "/c/..", "rel", "/c//x/./"]:
        envs.append({"HOME": "/h", "CLAUDE_CONFIG_DIR": config})
    envs += [{}, {"CLAUDE_CONFIG_DIR": "/c"}, {"HOME": "/h"}]
    for env in envs:
        out["host_paths"].append({"env": {k: cp(v) for k, v in env.items()}, **result_of(lambda: guard.host_paths(dict(env)))})
    for _ in range(170):
        env = default_env(r)
        cwd = pick(r, ["/", "/usr"])
        case = {"env": {k: cp(v) for k, v in env.items()}, "cwd": cp(cwd)}
        case.update(with_env({k: v for k, v in env.items()}, cwd, lambda: result_of(guard.default_specs)))
        out["default_specs"].append(case)

    class Captured(Exception):
        pass

    def run_bootstrap(args):
        captured = {}

        def capture(root, specs, inventory=None, blobs=None, expected=None):
            captured["root"] = str(root)
            captured["specs"] = copy.deepcopy(specs)
            raise Captured()

        saved = guard.snapshot
        guard.snapshot = capture
        try:
            namespace = argparse.Namespace(inventory=None, **args)
            try:
                guard.bootstrap(namespace)
            except Captured:
                return {"ok": {"root": cp(captured["root"]), "output": cp(str(guard.absolute(args["output"]))), "specs": typed(captured["specs"])}}
            except Exception as error:  # noqa: BLE001
                return {"error": {"type": type(error).__name__, "msg": str(error)}}
        finally:
            guard.snapshot = saved

    path_pool = ["/p", "/p/", "rel", "~/p", "~root/p", "/p/./q", "/p//q", "", "/e\u0301", "/p/\udc80", EMPTY]
    for index in range(150):
        env = {"HOME": "/h"} if r.random() < 0.8 else default_env(r)
        if r.random() < 0.3:
            env.update({k: v for k, v in default_env(r).items() if k != "HOME"})
        root = pick(r, ["/r", "/r/", "rel", "/r/..", "/r/./x", "~/r", "/"])
        output = pick(r, ["/o", "/r", "/r/o", "/r/", "rel/o", "/o/..", "/", "~/o", "/r/..", "/o//x"])
        def many():
            return [pick(r, path_pool) for _ in range(r.choice([0, 0, 1, 2, 3]))]
        managed = [pick(r, ["/m", "/m/", "/etc/claude-code", "rel", "~/m", "//m", "", "/m/..", "/m/./x", "/m/e\u0301", EMPTY, "/m"]) for _ in range(r.choice([0, 0, 1, 2]))]
        args = {"root": root, "output": output, "setting": many(), "config": many(), "shell": many(), "skills_dir": many(), "settings_dir": many(), "managed_dir": managed}
        cwd = pick(r, ["/", "/usr"])
        case = {"env": {k: cp(v) for k, v in env.items()}, "cwd": cp(cwd), "args": {k: typed_arg_list(v) for k, v in args.items()}}
        case.update(with_env(env, cwd, lambda: run_bootstrap(args)))
        out["bootstrap"].append(case)
    return out


def typed_arg_list(value):
    if isinstance(value, list) and value is not EMPTY:
        return {"t": "list", "v": [typed_arg_list(v) for v in value]}
    if value is EMPTY:
        return {"t": "empty"}
    return {"t": "str", "v": cp(value)}


def settings_json_cases():
    texts = [
        "{}", '{"env":{}}', '{"env":{"A":"b"}}', '{"env":[]}', '{"env":"x"}', '{"env":{"A":1}}', '{"env":{"HOME":"x"}}', '{"env":{"CLAUDE_CONFIG_DIR":"x"}}',
        '{"env":{"CLAUDE_CODE_REMOTE_SETTINGS_PATH":"x"}}', '{"env":{"CLAUDE_CODE_MANAGED_SETTINGS_PATH":"x"}}', '{"env":{"CLAUDE_CODE_MOCK_REMOTE_SETTINGS":"1"}}',
        '{"env":{"A":"b","XDG_CONFIG_HOME":"x"}}', '{"env":{"CLAUDE_CODE_SIMPLE":"x"}}', '{"env":{"CLAUDE_CODE_SAFE_MODE":"x"}}', '{"env":{"CLAUDE_CODE_SHELL_PREFIX":"x"}}',
        '{"env":{"HOME":1}}', '{"policyHelper":1}', '{"policyHelpers":[]}', '{"disableAllHooks":true}', '{"disableAllHooks":1}', '{"disableAllHooks":"true"}',
        '{"disableAllHooks":false}', '{"allowManagedHooksOnly":true}', '{"allowManagedHooksOnly":1}', '{"allowManagedHooksOnly":true,"disableAllHooks":false}',
        '{"env":null}', '{"permissions":{"allow":["Bash(ls)"]}}',
    ]
    return texts


def settings_cases():
    import os

    guard = load_guard()
    r = rng("settings")
    out = {"check": [], "specs": [], "rules": [], "bash": [], "allowlist": [], "read": []}
    for text in settings_json_cases():
        value = json.loads(text)
        for managed in (False, True):
            out["check"].append({"value": typed(value), "managed": managed, **result_of(lambda: guard.check_host_setting(value, managed))})
    # host_settings_specs
    def entries_of(names, kind="file"):
        entries = {}
        for name in names:
            entries[name] = [kind, 420, "ab"] if kind == "file" else [kind]
        return entries

    base_specs = {"host_paths": {"user_settings": "/u/s.json", "cache_settings": "/u/r.json"}, "managed_roots": ["/etc/claude-code"]}
    d = "external:/etc/claude-code/managed-settings.d/"
    spec_inputs = [
        (base_specs, {}),
        (base_specs, {d + "a.json": ["file", 420, "x"], d + "B.json": ["file", 420, "x"], d + "c.JSON": ["file", 420, "x"], d + ".h.json": ["file", 420, "x"],
                      d + "sub/x.json": ["file", 420, "x"], d + "z.txt": ["file", 420, "x"], d + "d.json": ["directory", 493], d + "e.json": ["missing"]}),
        ({**base_specs, "managed_roots": ["/etc/claude-code", "/m/x/"]}, {d + "a.json": ["file", 420, "x"], "external:/m/x/managed-settings.d/q.json": ["file", 1, "y"]}),
        ({**base_specs, "managed_roots": []}, {}),
        ({**base_specs, "managed_roots": ["rel"]}, {}),
        ({**base_specs, "managed_roots": [1]}, {}),
        ({**base_specs, "managed_roots": "x"}, {}),
        ({"managed_roots": []}, {}),
        ({"host_paths": {"user_settings": "/u"}, "managed_roots": []}, {}),
        ({"host_paths": {"user_settings": 1, "cache_settings": "/c"}, "managed_roots": []}, {}),
        ({"host_paths": [], "managed_roots": []}, {}),
        (base_specs, {d + "a.json": "file"}),
        (base_specs, {d + "a.json": 1}),
        (base_specs, {d + "a.json": {"x": 1}}),
        (base_specs, {d + "a.json": None}),
        (base_specs, {d + "a.json": ["file"], d + "b.json": [], d + "c.json": "f"}),
        ({**base_specs, "managed_roots": ["/etc/claude-code/"]}, {d + "a.json": ["file", 1, "x"]}),
        ({**base_specs, "managed_roots": ["/e//c/./d"]}, {"external:/e/c/d/managed-settings.d/a.json": ["file", 1, "x"]}),
    ]
    def dropins(root, names):
        return {"external:" + root + "/managed-settings.d/" + name: ["file", 420, "x"] for name in names}

    unsorted_names = ["b.json", "a.json", "B.json", "10.json", "2.json", "\u00e9.json", "\u3042.json", "Z.json", "_x.json", "a b.json"]
    spec_inputs += [
        (base_specs, dropins("/etc/claude-code", unsorted_names)),
        ({**base_specs, "managed_roots": ["/etc/claude-code", "/m/x"]},
         {**dropins("/m/x", ["q.json", "c.json", "a.json"]), **dropins("/etc/claude-code", unsorted_names[:5]), "external:/etc/other": ["file", 1, "z"],
          **dropins("/m/x", ["b.json"])}),
        ({**base_specs, "managed_roots": ["/m/x", "/etc/claude-code", "/m/y"]},
         {**dropins("/etc/claude-code", ["z.json", "m.json"]), **dropins("/m/y", ["k.json", "d.json"]), **dropins("/m/x", ["f.json", "e.json", "g.json"])}),
    ]
    for specs, entries in spec_inputs:
        out["specs"].append({"specs": typed(specs), "entries": typed(entries), **result_of(lambda: guard.host_settings_specs(specs, entries))})
    # split_rules / bash_rule
    pool = ["Bash", "Bash(ls)", "Bash(ls:*)", "Bash(git commit *)", "Bash(ls *)", "Bash(*)", "Bash()", "Bash( )", "Bash(:*)", "Bash( *)", "Bash(a*b)", "Bash(a *b)",
            "Bash(a  b:*)", " ", ",", "(", ")", "*", ":*", "\u3000", "\u00a0", "\x1c", "a", "Read", "Bash(", "Bash)", "bash", "Bash(ls\u3000-l)", "Bash(\tls\n)", ",,", "(,)", "Bash(a, b)"]
    texts = list(pool)
    for _ in range(260):
        texts.append("".join(r.choice(pool) for _ in range(r.randint(1, 5))))
    for text in texts:
        out["rules"].append({"text": cp(text), "rules": [cp(x) for x in guard.split_rules(text)]})
        got = guard.bash_rule(text)
        out["bash"].append({"text": cp(text), "result": "skip" if got == "skip" else None if got is None else [got[0], [cp(w) for w in got[1]]]})
    # host-allowlist(`main` を動かして、出力と終了コードを比べる)
    def capture_main(argv, settings):
        import io

        out_stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="surrogateescape", newline="\n")
        err_stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="backslashreplace", newline="\n")
        saved = (sys.stdout, sys.stderr, sys.argv, guard.read_host_settings)
        sys.stdout, sys.stderr, sys.argv = out_stream, err_stream, ["environment-guard.py"] + argv
        guard.read_host_settings = lambda state, expected: settings
        code = None
        try:
            code = guard.main()
        except SystemExit as error:
            code = error.code
        finally:
            sys.stdout, sys.stderr, sys.argv, guard.read_host_settings = saved
        out_stream.flush()
        err_stream.flush()
        return {"code": code, "stdout": out_stream.buffer.getvalue().hex(), "stderr": err_stream.buffer.getvalue().hex()}

    allow_pool = ["Bash(ls)", "Bash(ls:*)", "Bash(git *)", "Bash", "Bash(*)", "Read", "Bash( )", "Bash(a*b)", "Bash(ls)", 1, None, "Bash(x\ny)", "Bash(" + "z" * 320 + "*)",
                  "Bash(a\x00\x1fb*)", "Bash(\u00e9*)", "Bash(\udc80 *)", "Bash(\ud800*)"]
    values_pool = ["Bash(ls)", "Bash(ls:*),Bash(cat)", "Bash(a b) Bash(c)", "Read", "(Bash(x*))", "Bash(x*)", ",", "Bash", "Bash(a (b) *)", "Bash(a\u3000b)"]
    for index in range(70):
        allow = [pick(r, allow_pool) for _ in range(r.choice([0, 1, 2, 4]))]
        user = {} if r.random() < 0.2 else {"permissions": pick(r, [{"allow": allow}, {"allow": allow}, {"allow": "x"}, {"allow": None}, [], "p", {}])}
        argv = ["host-allowlist", "--state", "s", "--expect-sha256", "h"]
        values = []
        for _ in range(r.choice([0, 1, 2, 3])):
            kind = r.random()
            if kind < 0.8:
                v = pick(r, values_pool)
                argv += ["--allowed-tools", v]
                values.append(v)
            else:
                argv += ["--allowed-tools=--"]
                values.append(None)
        settings = {"user_settings": "/u", "user": user, "cache": None, "managed": [], "checked": 1, "permission_sources": []}
        try:
            result = capture_main(argv, settings)
        except Exception as error:  # noqa: BLE001
            result = {"python_error": str(error)}
        out["allowlist"].append({"argv": argv, "user": typed(user), **result})
    # read_host_settings の後半
    out["read"] = read_settings_cases(guard, r)
    out["output"] = host_settings_output_cases(guard)
    out["host_paths_main"] = host_paths_main_cases(guard)
    return out


def capture_guard_main(guard, argv, settings=None):
    """`guard.main()` を `argv` で動かし、標準出力・標準エラー(16 進)と終了コードを返す。`settings` があれば
    `read_host_settings` をその値で置き換える。"""
    import io

    out_stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="surrogateescape", newline="\n")
    err_stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="backslashreplace", newline="\n")
    saved = (sys.stdout, sys.stderr, sys.argv, guard.read_host_settings)
    sys.stdout, sys.stderr, sys.argv = out_stream, err_stream, ["environment-guard.py"] + argv
    if settings is not None:
        guard.read_host_settings = lambda state, expected: settings
    code = None
    try:
        code = guard.main()
    except SystemExit as error:
        code = error.code
    finally:
        sys.stdout, sys.stderr, sys.argv, guard.read_host_settings = saved
    out_stream.flush()
    err_stream.flush()
    return {"code": code, "stdout": out_stream.buffer.getvalue().hex(), "stderr": err_stream.buffer.getvalue().hex()}


def host_settings_output_cases(guard):
    cases = []
    paths = ["/u/settings.json", "/\u00e9/\u65e5\u672c\u8a9e.json", '/a"b\\c', "/\udc80", "/\x00x", "/\u2028", "", "/\ud83d\ude00", "/a\tb", "relative"]
    for path in paths:
        for checked in (0, 1, 5, 10 ** 30):
            settings = {"user_settings": path, "user": {}, "cache": None, "managed": [], "checked": checked, "permission_sources": []}
            result = capture_guard_main(guard, ["host-settings", "--state", "s", "--expect-sha256", "h"], settings)
            cases.append({"user_settings": cp(path), "checked": checked if checked < 10 ** 9 else str(checked), "big": checked >= 10 ** 9, **result})
    return cases


def host_paths_main_cases(guard):
    cases = []
    homes = ["/h", "/h/", "~", "", "rel", "/h/..", "//h", "/h/e\u0301", "/h/\udc80", "/h/\u65e5\u672c", '/h/"q', "/h/\x01", "/h/\u2028", None]
    configs = [None, "/c", "", "~", "/c/..", "rel", "/c//x/./", "/c/\u00e9", "/c/\udcff"]
    for home in homes:
        for config in configs:
            env = {}
            if home is not None:
                env["HOME"] = home
            if config is not None:
                env["CLAUDE_CONFIG_DIR"] = config
            result = with_env(env, "/", lambda: capture_guard_main(guard, ["host-paths"]))
            cases.append({"env": {k: cp(v) for k, v in env.items()}, **result})
    return cases


def read_settings_cases(guard, r):
    import hashlib

    def sha(raw):
        return hashlib.sha256(raw).hexdigest()

    cases = []

    def case(desc, entries, fake, extra=None, site=None):
        value = {"version": 3, "host_settings": desc, "entries": entries}
        if entries is Ellipsis:
            del value["entries"]
        if extra:
            value.update(extra)
        record_calls = []

        def verify(state, expected, current_inventory=None):
            return value

        def record(path, key, budget, local, expected=None, **kw):
            record_calls.append(path)
            item = fake.get(path)
            if item is None:
                raise FileNotFoundError(2, "No such file or directory", path)
            if "raise" in item:
                raise OSError(13, "Permission denied", path)
            for k, v in item.get("local", {}).items():
                local[k] = v
            return bytes.fromhex(item["raw"]) if item.get("raw") is not None else None

        saved = (guard.verify, guard.record)
        guard.verify, guard.record = verify, record
        try:
            if site is None:
                result = result_of(lambda: guard.read_host_settings("s", "h"))
            else:
                result = outcome(call_at(site, guard.read_host_settings, "s", "h"))
                if result["site"] is None:
                    return
        finally:
            guard.verify, guard.record = saved
        cases.append({"value": typed(value), "fake": typed(fake), "records": typed(record_calls), **result})

    def raw(text):
        return text.encode().hex()

    def file_entry(path, text):
        return {"external:" + path: ["file", 420, sha(text.encode())]}

    u, c, m, d = "/u/settings.json", "/u/remote.json", "/etc/claude-code/managed-settings.json", "/etc/claude-code/managed-settings.d/a.json"
    entries = {}
    for path, text in ((u, '{"permissions":{"allow":["Bash(ls)"]}}'), (c, '{}'), (m, '{"x":1}'), (d, '{"env":{"A":"b"}}')):
        entries.update(file_entry(path, text))
    fake = {u: {"raw": raw('{"permissions":{"allow":["Bash(ls)"]}}'), "local": file_entry(u, '{"permissions":{"allow":["Bash(ls)"]}}')},
            c: {"raw": raw("{}"), "local": file_entry(c, "{}")},
            m: {"raw": raw('{"x":1}'), "local": file_entry(m, '{"x":1}')},
            d: {"raw": raw('{"env":{"A":"b"}}'), "local": file_entry(d, '{"env":{"A":"b"}}')}}
    full = [{"path": u, "kind": "user"}, {"path": c, "kind": "cache"}, {"path": m, "kind": "managed"}, {"path": d, "kind": "managed"}]
    case(full, entries, fake)
    case(full[:1], entries, fake)
    case([], entries, fake)
    case(full[1:], entries, fake)
    case(full + [{"path": u, "kind": "user"}], entries, fake)
    case(full + [{"path": c, "kind": "cache"}], entries, fake)
    case("x", entries, fake)
    case(None, entries, fake)
    for bad in [{"path": u}, {"path": u, "kind": "other"}, {"path": 1, "kind": "user"}, {"path": u, "kind": "user", "x": 1}, "s", [], {"path": u, "kind": ["user"]}, {"kind": "user", "path": "/no"}]:
        case([bad] + full, entries, fake)
    # 不在・本文違い・link
    missing_entries = dict(entries)
    missing_entries["external:" + c] = ["missing"]
    case(full, missing_entries, {**fake, c: {"raw": None, "local": {"external:" + c: ["missing"]}}})
    case(full, missing_entries, {**fake, c: {"raw": raw("{}"), "local": file_entry(c, "{}")}})
    case(full, entries, {**fake, u: {"raw": raw('{"a":2}'), "local": file_entry(u, '{"a":2}')}})
    case(full, entries, {**fake, u: {"raw": raw('{"permissions":{"allow":["Bash(ls)"]}}'), "local": {**file_entry(u, '{"permissions":{"allow":["Bash(ls)"]}}'), "@file-link:0:external:" + u: ["configuration-link", "t", 1, 2]}}})
    link_entries = dict(entries)
    link_entries["@file-link:0:external:" + u] = ["configuration-link", "t", 1, 2, 3, 4, 5]
    case(full, link_entries, fake)
    case(full, link_entries, {**fake, u: {**fake[u], "local": {**fake[u]["local"], "@file-link:0:external:" + u: ["configuration-link", "t", 1, 2, 3, 4, 5]}}})
    case(full, link_entries, {**fake, u: {**fake[u], "local": {**fake[u]["local"], "@file-link:0:external:" + u: ["configuration-link", "t", 1, 2, 3, 4, 6]}}})
    colon_entries = dict(entries)
    colon_entries["@file-link:1:external:" + u] = ["configuration-link", "t", 1, 2]
    case(full, colon_entries, {**fake, u: {**fake[u], "local": {**fake[u]["local"], "@file-link:1:external:" + u: ["configuration-link", "t", 1, 2]}}})
    case(full, entries, {**fake, d: {"raise": True}})
    case(full, entries, {k: v for k, v in fake.items() if k != m})
    # 設定の本文の検査の失敗
    for text in ["[]", "x", '{"a":1,"a":2}', '{"a":NaN}', '{"env":{"HOME":"x"}}', '{"disableAllHooks":true}', '{"allowManagedHooksOnly":true}', "\ufeff{}", '{"a":' + "[" * 120 + "]" * 120 + "}"]:
        for kind_index in (0, 2, 3):
            path = [u, c, m, d][kind_index]
            e2 = dict(entries)
            e2.update(file_entry(path, text))
            f2 = dict(fake)
            f2[path] = {"raw": raw(text), "local": file_entry(path, text)}
            case(full, e2, f2)
    case(full, entries, fake, {"host_settings": full})
    not_dict_entries = {"version": 3}
    case(full, "e", fake)
    case(full, 5, fake)
    case([], 5, fake)
    # entries が辞書でないとき: 項目の検査が先、`key not in entries` はその後(旧版の順)
    for bad_entries in ["e", 5, None, [], ["x"], {}, 1.5, True, "external:" + u, ["external:" + u], ["external:" + u, "@file-link:0:external:" + u], [1, "external:" + u]]:
        case(full, bad_entries, fake)
        case([{"path": u}] + full, bad_entries, fake)
        case([{"path": u, "kind": "user"}], bad_entries, fake)
        case([], bad_entries, fake)
    case(full, Ellipsis, fake)
    case([], Ellipsis, fake)
    case(None, Ellipsis, fake)
    # 呼び出し側の深さの境界(`dump` のフレーム 1 段を含む)
    missing_user = {"external:" + u: ["missing"]}
    missing_fake = {u: {"raw": None, "local": {"external:" + u: ["missing"]}}}
    for site in range(988, 1000):
        case(full[:1], entries, fake, site=site)
        # ファイルが無いときは本文の解析に進まないので、`dump` の境界まで届く。
        case(full[:1], missing_user, missing_fake, site=site)
        case([{"path": u, "kind": "cache"}, {"path": "/u/x", "kind": "user"}], {**missing_user, "external:/u/x": ["missing"]}, {**missing_fake, "/u/x": {"raw": None, "local": {"external:/u/x": ["missing"]}}}, site=site)
    return cases


def inventory_cases():
    import copy
    import hashlib

    guard = load_guard()
    r = rng("inventory")
    cases = []
    config = "/h/.claude"

    def blob(value):
        return json.dumps(value).encode()

    def make(inventory, components, blobs, trees, host_paths=None, label="", specs=None):
        if specs is None:
            specs = {"components": components, "host_paths": {"config": config} if host_paths is None else host_paths}
        specs_for_case = copy.deepcopy(specs)

        def fake_tree(path, kind, budget, entries, expected=None, blobs=None):
            item = trees.get(path)
            if item is None:
                return "component:" + kind + ":" + path
            if item.get("blob") is not None:
                blobs[item["key"]] = bytes.fromhex(item["blob"])
            return item["key"]

        saved = guard.component_tree
        guard.component_tree = fake_tree
        stored = {k: v for k, v in blobs.items()}
        try:
            result = with_env({"HOME": "/h"}, "/", lambda: result_of(lambda: (guard._component_inventory(specs, inventory, None, {}, stored), specs)[1]))
        finally:
            guard.component_tree = saved
        cases.append({"label": label, "inventory": typed(inventory), "specs": typed(specs_for_case),
                      "blobs": {k: v.hex() for k, v in blobs.items()}, "trees": typed(trees), **result})

    skills = [{"kind": "personal-skill", "path": "/h/.claude/skills"}, {"kind": "enterprise-skill", "path": "/etc/s"}, {"kind": "plugin", "path": "/d", "source": "workflow"}]
    known = {"m": {"installLocation": "/mk"}}
    manifest_path = "/mk/.claude-plugin/marketplace.json"
    manifest = {"plugins": [{"name": "p", "source": "./plug"}, {"name": "q", "source": "./q"}]}
    installed = {"plugins": {"p@m": [{"installPath": "/ins/p", "scope": "user"}]}}
    full_blobs = {"external:" + config + "/plugins/known_marketplaces.json": blob(known), "external:" + config + "/plugins/installed_plugins.json": blob(installed)}
    trees = {manifest_path: {"key": "component:marketplace-manifest:" + manifest_path, "blob": blob(manifest).hex()}}
    item = {"id": "p@m", "installPath": "/ins/p", "scope": "user"}
    make([], skills, {}, {}, label="empty")
    make([item], skills, full_blobs, trees, label="full")
    make([item, item], skills, full_blobs, trees, label="twice")
    make([{**item, "scope": "synced"}], skills, full_blobs, trees, label="synced")
    make([{"id": "p", "installPath": "/ins/p"}], skills, {}, {}, label="no-marketplace")
    make([{"id": "p@m", "installPath": "/ins/p"}], skills, full_blobs, trees, label="no-scope")
    make([{**item, "scope": "project"}], skills, full_blobs, trees, label="scope-mismatch")
    make([{**item, "installPath": "/other"}], skills, full_blobs, trees, label="path-mismatch")
    make([{"id": "", "installPath": "/ins/p"}], skills, {}, {}, label="empty-id")
    make([{"installPath": "/ins/p"}], skills, {}, {}, label="no-id")
    make([{"id": 1, "installPath": "/ins/p"}], skills, {}, {}, label="int-id")
    make([item], skills, full_blobs, trees, host_paths={}, label="no-config")
    make([item], skills, full_blobs, trees, host_paths={"config": ""}, label="empty-config")
    make([item], skills, full_blobs, trees, host_paths={"config": 5}, label="int-config")
    make([item], skills, full_blobs, trees, host_paths=[], label="list-host-paths")
    make([item], skills, {k: v for k, v in full_blobs.items() if "known" not in k}, trees, label="no-registry")
    make([item], skills, {k: v for k, v in full_blobs.items() if "installed" not in k}, trees, label="no-installed")
    make([item], skills, full_blobs, {}, label="no-manifest")
    make([item], skills, full_blobs, {manifest_path: {"key": "k", "blob": None}}, label="manifest-key-without-blob")
    for name, value in [("not-dict", {"m": 1}), ("no-location", {"m": {}}), ("rel-location", {"m": {"installLocation": "rel"}}), ("int-location", {"m": {"installLocation": 1}}), ("other-market", {"x": {"installLocation": "/mk"}})]:
        make([item], skills, {**full_blobs, "external:" + config + "/plugins/known_marketplaces.json": blob(value)}, trees, label=name)
    for name, value in [("no-plugins", {}), ("plugins-dict", {"plugins": {}}), ("zero-match", {"plugins": [{"name": "z"}]}), ("two-match", {"plugins": [{"name": "p", "source": "./a"}, {"name": "p", "source": "./b"}]}),
                        ("non-dict-entry", {"plugins": [1, {"name": "p", "source": "./a"}]}), ("source-abs", {"plugins": [{"name": "p", "source": "/abs"}]}), ("source-dotdot", {"plugins": [{"name": "p", "source": "./a/../b"}]}),
                        ("source-int", {"plugins": [{"name": "p", "source": 1}]}), ("source-none", {"plugins": [{"name": "p"}]}), ("source-dotslash-only", {"plugins": [{"name": "p", "source": "./"}]}),
                        ("source-dots", {"plugins": [{"name": "p", "source": "./.../x"}]})]:
        make([item], skills, full_blobs, {manifest_path: {"key": "component:marketplace-manifest:" + manifest_path, "blob": blob(value).hex()}}, label=name)
    for name, value in [("not-dict", []), ("no-plugin", {"plugins": {}}), ("plugins-list", {"plugins": []}), ("two", {"plugins": {"p@m": [{"installPath": "/ins/p", "scope": "user"}, {"installPath": "/ins/p", "scope": "user"}]}}),
                        ("not-list", {"plugins": {"p@m": {}}}), ("non-dict-candidate", {"plugins": {"p@m": [1]}}), ("wrong-path", {"plugins": {"p@m": [{"installPath": "/x", "scope": "user"}]}})]:
        make([item], skills, {**full_blobs, "external:" + config + "/plugins/installed_plugins.json": blob(value)}, trees, label="installed-" + name)
    # skills-dir
    skills_manifest = "component:personal-skill:/h/.claude/skills/foo/.claude-plugin/plugin.json"
    sd = {"id": "foo@skills-dir", "installPath": "/h/.claude/skills/foo"}
    make([sd], skills, {skills_manifest: blob({"name": "foo"})}, {}, label="skills-dir")
    make([sd], skills, {skills_manifest: blob({"name": "bar"})}, {}, label="skills-dir-name-mismatch")
    make([sd], skills, {}, {}, label="skills-dir-no-manifest")
    make([sd], skills[1:], {skills_manifest: blob({"name": "foo"})}, {}, label="skills-dir-no-candidate")
    make([{"id": "@skills-dir", "installPath": "/h/.claude/skills/foo"}], skills, {}, {}, label="skills-dir-empty-name")
    make([{"id": "a@b@skills-dir", "installPath": "/h/.claude/skills/foo"}], skills, {}, {}, label="skills-dir-at")
    make([{**sd, "installPath": "/h/.claude/skills/./foo"}], skills, {skills_manifest: blob({"name": "foo"})}, {}, label="skills-dir-unnormalized")
    make([sd], skills + [{"kind": "enterprise-skill", "path": "/h/.claude/skills"}], {skills_manifest: blob({"name": "foo"})}, {}, label="skills-dir-two-candidates")
    make([sd], skills, {skills_manifest: b"{bad"}, {}, label="skills-dir-bad-json")
    make([sd], skills, {skills_manifest: blob([])}, {}, label="skills-dir-list-manifest")
    # add の衝突
    dup = [{"kind": "plugin", "path": "/ins/p", "plugin": "other@m", "source": "x"}] + skills
    make([{**item, "scope": "synced"}], dup, {}, {}, label="add-conflict-plugin")
    dup2 = [{"kind": "plugin", "path": "/ins/p", "plugin": "p@m", "source": "x"}] + skills
    make([{**item, "scope": "synced"}], dup2, {}, {}, label="add-same-plugin")
    dup3 = [{"kind": "plugin", "path": "/ins/p", "entry": [1]}] + skills
    make([{**item, "scope": "synced"}], dup3, {}, {}, label="add-conflict-entry")
    dup4 = [{"kind": "marketplace-manifest", "path": manifest_path, "source": "/mk", "entry": [{"plugin": "z@m", "entry": {}}]}] + skills
    make([item], dup4, full_blobs, trees, label="manifest-existing-new-selection")
    dup5 = [{"kind": "marketplace-manifest", "path": manifest_path, "source": "/mk", "entry": [{"plugin": "p@m", "entry": manifest["plugins"][0]}]}] + skills
    make([item], dup5, full_blobs, trees, label="manifest-existing-same-selection")
    # 壊れた控え(SHA-256 は合うが形が違う)での、評価の順と例外
    synced = {**item, "scope": "synced"}
    hp = {"config": config}
    make([], None, {}, {}, specs={"host_paths": hp}, label="no-components-empty-inventory")
    make([synced], None, {}, {}, specs={"host_paths": hp}, label="no-components-add")
    make([sd], None, {}, {}, specs={"host_paths": hp}, label="no-components-skills-dir")
    make([synced], None, {}, {}, specs={"host_paths": []}, label="host-paths-list-before-components")
    make([synced], None, {}, {}, specs={"host_paths": None}, label="host-paths-none-before-components")
    make([], None, {}, {}, specs={"host_paths": []}, label="host-paths-list-empty-inventory")
    for name, value in [("none", None), ("dict", {"a": 1}), ("str", "ab"), ("int", 5), ("empty-dict", {})]:
        make([synced], None, {}, {}, specs={"components": value, "host_paths": hp}, label="components-" + name)
        make([sd], None, {}, {}, specs={"components": value, "host_paths": hp}, label="components-skills-" + name)
    for name, olds in [("old-no-path", [{"kind": "other"}]), ("old-kind-only-plugin", [{"kind": "plugin"}]), ("old-no-kind", [{"path": "/x"}]), ("old-int", [1]),
                       ("old-str", ["abc"]), ("old-list", [[]]), ("old-none", [None]), ("old-match-then-bad", [{"kind": "plugin", "path": "/ins/p", "plugin": "p@m", "entry": None}, 1]),
                       ("old-bad-after-other", [{"kind": "x", "path": "/y"}, 1])]:
        make([synced], None, {}, {}, specs={"components": olds, "host_paths": hp}, label="add-" + name)
        make([sd], None, {}, {}, specs={"components": olds, "host_paths": hp}, label="skills-" + name)
    for name, entry in [("missing", "MISSING"), ("none", None), ("dict", {}), ("str", "x"), ("int", 5), ("list", []), ("float", 1.5), ("bool", True)]:
        existing = {"kind": "marketplace-manifest", "path": manifest_path, "source": "/mk"}
        if entry != "MISSING":
            existing["entry"] = entry
        make([item], None, full_blobs, trees, specs={"components": [existing] + skills, "host_paths": hp}, label="manifest-entry-" + name)
    make([item], None, full_blobs, trees, specs={"components": [{"kind": "marketplace-manifest", "path": manifest_path}, 1] + skills, "host_paths": hp}, label="manifest-then-bad")
    make([{"id": "p@m"}], skills, {}, {}, label="no-install-path")
    make([1], skills, {}, {}, label="item-int")
    make(["x"], skills, {}, {}, label="item-str")
    make([{**item, "scope": None}], skills, full_blobs, trees, label="scope-none")
    make([{**item, "scope": ""}], skills, full_blobs, trees, label="scope-empty")
    make([{**item, "scope": 0}], skills, full_blobs, trees, label="scope-zero")
    return cases


def state_cases():
    import hashlib
    import os

    guard = load_guard()
    r = rng("state")
    out = {"load": [], "digest": [], "bootstrap": []}

    def sha(raw):
        return hashlib.sha256(raw).hexdigest()

    def load(expected, raw, label=""):
        def read(path):
            if raw is None:
                raise FileNotFoundError(2, "No such file or directory", "s")
            return raw

        saved = guard.read_regular
        guard.read_regular = read
        try:
            result = result_of(lambda: guard.load_verified("s", expected))
        finally:
            guard.read_regular = saved
        out["load"].append({"label": label, "expected": typed(expected), "raw": raw.hex() if raw is not None else None, **result})

    good = json.dumps({"version": 3, "a": [1, 2.5, "x"]}).encode()
    load(sha(good), good, "good")
    load(sha(good).upper(), good, "upper")
    load(sha(good)[:-1], good, "short")
    load(sha(good) + "0", good, "long")
    load("", good, "empty")
    load(None, good, "none")
    load("g" * 64, good, "bad-hex")
    load(sha(good), None, "missing-file")
    load("0" * 64, good, "mismatch")
    load(sha(good) + "\n", good, "newline")
    for label, body in [("version-float", b'{"version":3.0}'), ("version-true", b'{"version":true}'), ("version-str", b'{"version":"3"}'), ("version-2", b'{"version":2}'),
                        ("no-version", b"{}"), ("list", b"[3]"), ("bad-json", b"{"), ("nan", b'{"version":3,"x":NaN}'), ("dup", b'{"version":3,"version":2}'), ("bom", b'\xef\xbb\xbf{"version":3}'),
                        ("utf16", '{"version":3}'.encode("utf-16")), ("big-int", b'{"version":3,"x":' + b"9" * 5000 + b"}"), ("empty-body", b""), ("bad-utf8", b'{"version":3,"x":"\xff"}')]:
        load(sha(body), body, label)

    def comp(kind, path, **extra):
        return {"kind": kind, "path": path, **extra}

    def base():
        return {
            "version": 3,
            "specs": {"components": [comp("personal-skill", "/c/skills"), comp("plugin", "/dist/plugin", source="workflow"), comp("plugin", "/ins/p", plugin="p@m", source="user"),
                                     comp("marketplace-manifest", "/m/.claude-plugin/marketplace.json", source="/m", entry=[{"plugin": "p@m", "entry": {"name": "p"}}])]},
            "inventory": [{"id": "p@m", "installPath": "/ins/p", "scope": "user", "version": "1", "extra": 1}],
            "entries": {
                "component:plugin:/dist/plugin": ["directory", 493],
                "component:plugin:/dist/plugin/a.txt": ["file", 420, "abc"],
                "@stat:component:plugin:/dist/plugin": ["directory-stat", 1, 2, 3],
                "@names:component:plugin:/dist/plugin": ["a.txt"],
                "@link:component:plugin:/dist/plugin/l": ["installation-link", "t", 1, 2, 3],
                "@link:component:personal-skill:/c/skills/x": ["installation-link", "t", 1, 2, 3],
                "@route:component:personal-skill:/c/skills:/x": ["link-step", "a", 1, 2, 3, 4],
                "@route:component:personal-skill:/c/skills:/y": ["route", "a", 1, 2],
                "@route:component:plugin:/dist/plugin:/q": ["link-step", 1, 2, 3, 4],
                "@route:component:plugin:/dist/plugin/z": ["link-step", 1, 2, 3, 4],
                "@route:component:plugin:/ins/p:/w": ["route", 1, 2, 3],
                "@route:component:other:/x": ["route", 1, 2, 3],
                "@names:component:personal-skill:/c/skills": ["x", "y"],
                "component:personal-skill:/c/skills": ["directory", 493],
                "external:/etc/x": ["file", 420, "zz"],
                "plugin": ["directory", 493],
                "@origin:component:plugin:/dist/plugin": ["origin"],
            },
        }

    def digest(value, label):
        def load_fake(state, expected):
            return value

        saved = guard.load_verified
        guard.load_verified = load_fake
        try:
            result = result_of(lambda: guard.component_policy_digest("s", "h"))
            descriptors = result_of(lambda: guard._component_descriptors(value))
        finally:
            guard.load_verified = saved
        out["digest"].append({"label": label, "value": typed(value), "digest": result, "descriptors": descriptors})

    digest(base(), "base")
    for label, mutate in [
        ("no-specs", lambda v: v.pop("specs")), ("specs-list", lambda v: v.update(specs=[])), ("specs-none", lambda v: v.update(specs=None)),
        ("specs-empty", lambda v: v.update(specs={})), ("components-none", lambda v: v["specs"].update(components=None)), ("components-empty", lambda v: v["specs"].update(components=[])),
        ("components-dict", lambda v: v["specs"].update(components={"a": 1})), ("item-not-dict", lambda v: v["specs"]["components"].append(1)),
        ("kind-unknown", lambda v: v["specs"]["components"].append(comp("x", "/p"))), ("kind-list", lambda v: v["specs"]["components"].append({"kind": [], "path": "/p"})),
        ("kind-dict", lambda v: v["specs"]["components"].append({"kind": {}, "path": "/p"})), ("kind-none", lambda v: v["specs"]["components"].append({"path": "/p"})),
        ("kind-int", lambda v: v["specs"]["components"].append({"kind": 1, "path": "/p"})), ("path-relative", lambda v: v["specs"]["components"].append(comp("plugin", "p"))),
        ("path-int", lambda v: v["specs"]["components"].append({"kind": "plugin", "path": 1})), ("no-path", lambda v: v["specs"]["components"].append({"kind": "plugin"})),
        ("duplicate", lambda v: v["specs"]["components"].append(comp("plugin", "/ins/p"))), ("same-path-other-kind", lambda v: v["specs"]["components"].append(comp("personal-agent", "/ins/p"))),
        ("no-workflow", lambda v: v["specs"]["components"].__setitem__(1, comp("plugin", "/dist/plugin"))),
        ("two-workflow", lambda v: v["specs"]["components"].append(comp("plugin", "/dist2", source="workflow"))),
        ("workflow-source-int", lambda v: v["specs"]["components"][1].update(source=1)),
        ("no-entries", lambda v: v.pop("entries")), ("entries-list", lambda v: v.update(entries=[])), ("entries-str", lambda v: v.update(entries="x")), ("entries-none", lambda v: v.update(entries=None)),
        ("entry-empty-list-route", lambda v: v["entries"].update({"@route:component:other:/e": []})), ("entry-str-route", lambda v: v["entries"].update({"@route:component:other:/e": "link-step"})),
        ("entry-int-route", lambda v: v["entries"].update({"@route:component:other:/e": 5})), ("entry-dict-route", lambda v: v["entries"].update({"@route:component:other:/e": {"a": 1}})),
        ("entry-none-route", lambda v: v["entries"].update({"@route:component:other:/e": None})),
        ("link-entry-int", lambda v: v["entries"].update({"@link:component:other:/e": 5})), ("link-entry-str", lambda v: v["entries"].update({"@link:component:other:/e": "abc"})),
        ("link-entry-dict", lambda v: v["entries"].update({"@link:component:other:/e": {}})), ("link-entry-none", lambda v: v["entries"].update({"@link:component:other:/e": None})),
        ("collision", lambda v: v["entries"].update({"@names:component:plugin:/dist/plugin/": ["x"], "component:plugin:@workflow": ["c"]})),
        ("no-inventory", lambda v: v.pop("inventory")), ("inventory-none", lambda v: v.update(inventory=None)), ("inventory-int", lambda v: v.update(inventory=5)),
        ("inventory-dict", lambda v: v.update(inventory={"id": 1, "x": 2})), ("inventory-str", lambda v: v.update(inventory="idx")), ("inventory-str-none", lambda v: v.update(inventory="abc")),
        ("inventory-item-int", lambda v: v["inventory"].append(5)), ("inventory-item-none", lambda v: v["inventory"].append(None)), ("inventory-item-str", lambda v: v["inventory"].append("id")),
        ("inventory-item-str-no", lambda v: v["inventory"].append("zzz")), ("inventory-item-list", lambda v: v["inventory"].append(["id"])), ("inventory-item-list-no", lambda v: v["inventory"].append([1])),
        ("inventory-item-empty", lambda v: v["inventory"].append({})), ("inventory-empty", lambda v: v.update(inventory=[])),
        ("unicode", lambda v: v["entries"].update({"component:plugin:/dist/plugin/\u00e9\ud83d\ude00": ["file", 1, "q"], "@names:component:plugin:/dist/plugin/\u00e9": ["\u3042"]})),
    ]:
        value = base()
        mutate(value)
        digest(value, label)
    # `dump` のフレーム 1 段を含む深さの境界: 複数キーの dict を葉にした入れ子の entries と、呼び出し側の深さ
    # 入れ子が深い値は JSON の読み込みの深さ制限に当たるので、基本の控えと段数 `k` だけを持つ(値は Rust 側で作る)。
    out["digest_depth"] = {"base": typed(base()), "cases": []}
    for site in (900, 975, 994):
        center = 1000 - site - 9
        for k in range(max(1, center - 4), center + 5):
            leaf = {"a": 1, "b": 2}
            for _ in range(k):
                leaf = [leaf]
            value = base()
            value["entries"]["component:plugin:/dist/plugin/deep"] = leaf

            def load_fake(state, expected, value=value):
                return value

            saved = guard.load_verified
            guard.load_verified = load_fake
            try:
                result = outcome(call_at(site, guard.component_policy_digest, "s", "h"))
            finally:
                guard.load_verified = saved
            if result["site"] is None:
                continue
            out["digest_depth"]["cases"].append({"k": k, **result})
    # bootstrap の控えと受領(旧版の `bootstrap` を一時フォルダで動かす)
    def run_bootstrap(args, entries, blobs_hex):
        saved = (guard.snapshot, guard.verify)
        blobs_bytes = {k: bytes.fromhex(v) for k, v in blobs_hex.items()}

        def snapshot(root, specs, inventory=None, blobs=None, expected=None):
            blobs.update(blobs_bytes)
            return json.loads(json.dumps(entries))

        guard.snapshot, guard.verify = snapshot, lambda *a, **k: None
        try:
            import shutil
            import tempfile

            folder = tempfile.mkdtemp(prefix="s2e-fixture-")
            try:
                import io

                output = folder + "/out"
                namespace = __import__("argparse").Namespace(inventory=None, **{**args, "output": output})
                stdout = io.StringIO()
                saved_out = sys.stdout
                sys.stdout = stdout
                try:
                    guard.bootstrap(namespace)
                finally:
                    sys.stdout = saved_out
                raw = open(output + "/environment.json", "rb").read().decode()
                receipt = stdout.getvalue()
                guard_copy = open(output + "/environment-guard.py", "rb").read()
                files = {}
                for base_dir, dirs, names in os.walk(output + "/plugin"):
                    for name in names:
                        path = os.path.join(base_dir, name)
                        files[path[len(output):]] = open(path, "rb").read().hex()
                # 一時パスは `/O` へ置き換える。控えの `sha256` は、置き換えた本文から計算し直す(中身が毎回同じになる)。
                replaced = raw.replace(folder, "/O")
                receipt = receipt.replace(folder, "/O").replace(sha(raw.encode()), sha(replaced.encode()))
                result = {"ok": {"raw": replaced, "receipt": receipt, "files": files,
                                 "guard_sha256": sha(guard_copy), "state_sha256": sha(replaced.encode())}}
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        except Exception as error:  # noqa: BLE001
            result = {"error": {"type": type(error).__name__, "msg": str(error)}}
        finally:
            guard.snapshot, guard.verify = saved
        return result

    plugin_entries = {"plugin": ["directory", 493], "plugin/a.txt": ["file", 420, sha(b"A")], "plugin/sub": ["directory", 448], "plugin/sub/b.txt": ["file", 384, sha(b"B")],
                      "external:/h/.claude/settings.json": ["missing"], "component:plugin:/r": ["directory", 493]}
    blobs = {"plugin/a.txt": b"A".hex(), "plugin/sub/b.txt": b"B".hex()}
    for index, args in enumerate([
        {"root": "/r", "setting": [], "config": [], "shell": [], "skills_dir": [], "settings_dir": [], "managed_dir": []},
        {"root": "/r", "setting": ["/s1"], "config": ["/g1"], "shell": ["/sh"], "skills_dir": ["/sk"], "settings_dir": ["/sd"], "managed_dir": ["/m"]},
        {"root": "/r", "setting": ["/s1", "/s1"], "config": [], "shell": [], "skills_dir": ["/sk", "/sk"], "settings_dir": [], "managed_dir": ["/etc/claude-code"]},
        {"root": "/r", "setting": ["rel"], "config": ["~/g"], "shell": [], "skills_dir": [], "settings_dir": ["/d1", "/d1/"], "managed_dir": ["/m", "/m"]},
        {"root": "/r", "setting": [], "config": [], "shell": [], "skills_dir": [], "settings_dir": [], "managed_dir": ["rel"]},
        {"root": "/r/..", "setting": [], "config": [], "shell": [], "skills_dir": [], "settings_dir": [], "managed_dir": []},
    ]):
        env = {"HOME": "/h"}
        case = {"args": {k: typed_arg_list(v) for k, v in args.items()}, "entries": typed(plugin_entries), "blobs": blobs}
        case.update(with_env(env, "/", lambda: run_bootstrap(args, plugin_entries, blobs)))
        out["bootstrap"].append(case)
    return out


@area("stage2a")
def make_stage2a():
    return {"specs_cases.json": specs_cases(), "settings_cases.json": settings_cases(), "inventory_cases.json": inventory_cases(), "state_cases.json": state_cases()}


# ---------------------------------------------------------------- 子の起動(2b-1a)
# 旧版の `subprocess.Popen`(Python 3.10)の PATH の探し方・信号・fd・returncode を測る。
# Rust の platform の単体テストが同じ配置を作って同じ結果を期待する。一時フォルダは
# 環境変数 S2E_FIXTURE_TMP の下(無ければ OS の既定)に作り、自分で作ったものだけを消す。

TRACKED_SIGNALS = ["INT", "USR1", "USR2", "PIPE", "XFSZ"]


def _child_layout(root, entries):
    for entry in entries:
        path = os.path.join(root, entry["path"])
        kind = entry["kind"]
        if kind == "dir":
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if kind == "dangling":
            os.symlink("nowhere", path)
            continue
        tag = entry.get("tag", "x")
        body = {
            "exec": "#!/bin/sh\necho %s\n" % tag,
            "noexec": "#!/bin/sh\necho %s\n" % tag,
            "shebangless": "echo %s\n" % tag,
            "file": "plain\n",
        }[kind]
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o644 if kind in ("noexec", "file") else 0o755)


def _child_outcome(argv, env, cwd, stdin_data=None, stderr="devnull"):
    import errno
    import subprocess

    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if stderr == "devnull" else None, env=env, cwd=cwd)
    except OSError as error:
        target = "none"
        if error.filename is not None:
            target = "executable" if error.filename == argv[0] else "cwd"
        return {"error": errno.errorcode[error.errno], "target": target, "type": type(error).__name__}
    out, _ = proc.communicate(stdin_data)
    return {"stdout": out.decode(), "returncode": proc.returncode}


def _child_path_cases(root):
    tool = lambda d, kind="exec", tag=None: {"path": d + "/tool", "kind": kind, "tag": tag or d}
    cases = [
        ("second-dir", [{"path": "d1", "kind": "dir"}, tool("d2")], "{r}/d1:{r}/d2", "tool", None),
        ("first-wins", [tool("d1"), tool("d2")], "{r}/d1:{r}/d2", "tool", None),
        ("noexec-then-ok", [tool("d1", "noexec"), tool("d2")], "{r}/d1:{r}/d2", "tool", None),
        ("noexec-only", [tool("d1", "noexec")], "{r}/d1:{r}/d2", "tool", None),
        ("notdir-then-missing", [{"path": "d1", "kind": "file"}, {"path": "d2", "kind": "dir"}], "{r}/d1:{r}/d2", "tool", None),
        ("notdir-then-noexec", [{"path": "d1", "kind": "file"}, tool("d2", "noexec")], "{r}/d1:{r}/d2", "tool", None),
        ("enoexec-then-ok", [tool("d1", "shebangless"), tool("d2")], "{r}/d1:{r}/d2", "tool", None),
        ("enoexec-only", [tool("d1", "shebangless")], "{r}/d1", "tool", None),
        ("enoexec-then-eacces", [tool("d1", "shebangless"), tool("d2", "noexec")], "{r}/d1:{r}/d2", "tool", None),
        ("eacces-then-enoexec", [tool("d1", "noexec"), tool("d2", "shebangless")], "{r}/d1:{r}/d2", "tool", None),
        ("dir-named-tool-then-ok", [{"path": "d1/tool", "kind": "dir"}, tool("d2")], "{r}/d1:{r}/d2", "tool", None),
        ("dir-named-tool-only", [{"path": "d1/tool", "kind": "dir"}], "{r}/d1", "tool", None),
        ("dangling-then-ok", [{"path": "d1/tool", "kind": "dangling"}, tool("d2")], "{r}/d1:{r}/d2", "tool", None),
        ("missing-everywhere", [{"path": "d1", "kind": "dir"}], "{r}/d1:{r}/d2", "tool", None),
        ("empty-path-entry-uses-cwd", [tool("work")], "", "tool", "{r}/work"),
        ("empty-entry-in-middle", [tool("work"), {"path": "d1", "kind": "dir"}], "{r}/d1::{r}/d2", "tool", "{r}/work"),
        ("relative-entry-after-chdir", [tool("work/rel")], "rel", "tool", "{r}/work"),
        ("relative-entry-not-from-parent-cwd", [tool("rel"), {"path": "work", "kind": "dir"}], "rel", "tool", "{r}/work"),
        ("trailing-slash-entry", [tool("d1")], "{r}/d1/", "tool", None),
        ("slash-in-name-skips-path", [tool("d1")], "{r}/d1", "{r}/d1/tool", None),
        ("slash-in-name-missing", [{"path": "d1", "kind": "dir"}], "{r}/d1", "{r}/d1/tool", None),
        ("relative-slash-name-uses-cwd", [tool("work/sub")], "{r}/nowhere", "sub/tool", "{r}/work"),
        ("cwd-missing", [tool("d1")], "{r}/d1", "tool", "{r}/nope"),
        ("cwd-is-file", [tool("d1"), {"path": "afile", "kind": "file"}], "{r}/d1", "tool", "{r}/afile"),
        ("cwd-missing-and-exec-missing", [], "{r}/d1", "tool", "{r}/nope"),
        ("path-unset-uses-defpath", [], None, "sh", None, ["-c", "echo defpath"]),
        ("path-unset-missing", [], None, "no-such-tool-s2e-2b1a", None),
        ("empty-path-missing", [], "", "no-such-tool-s2e-2b1a", None),
    ]
    out = []
    for name, layout, path_env, argv0, cwd, *rest in cases:
        out.append({"name": name, "layout": layout, "path": path_env, "argv0": argv0, "cwd": cwd,
                    "rest": rest[0] if rest else []})
    return out


def _child_signal_state(spec):
    """子の `/proc/self/status` の SigIgn・SigBlk から、追う信号だけを名前で返す。"""
    import signal
    import subprocess

    numbers = {n: getattr(signal, "SIG" + n) for n in TRACKED_SIGNALS}
    saved = {n: signal.getsignal(numbers[n]) for n in numbers}
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        for n in TRACKED_SIGNALS:
            # 指定が無い PIPE・XFSZ は無視(Python の起動時の状態)。ほかは既定。指定があればそのとおり。
            how = spec.get(n, "ignore" if n in ("PIPE", "XFSZ") else "default")
            signal.signal(numbers[n], {"default": signal.SIG_DFL, "ignore": signal.SIG_IGN, "handler": (lambda *a: None)}[how])
        signal.pthread_sigmask(signal.SIG_SETMASK, [numbers[n] for n in TRACKED_SIGNALS if spec.get(n + "-block")])
        out = subprocess.run(["cat", "/proc/self/status"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             env={"PATH": "/usr/bin:/bin"}).stdout.decode()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        for n, handler in saved.items():
            signal.signal(numbers[n], handler)
    masks = {}
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key in ("SigIgn", "SigBlk"):
            masks[key] = int(value.strip(), 16)
    pick = lambda mask: [n for n in TRACKED_SIGNALS if mask >> (numbers[n] - 1) & 1]
    return {"ignored": pick(masks["SigIgn"]), "blocked": pick(masks["SigBlk"])}


def _child_signal_cases():
    specs = [
        {"name": "all-default"},
        {"name": "int-ignored", "INT": "ignore"},
        {"name": "usr1-ignored", "USR1": "ignore"},
        {"name": "usr1-handler", "USR1": "handler"},
        {"name": "usr2-blocked", "USR2-block": True},
        {"name": "ignore-and-block", "USR1": "ignore", "USR2": "ignore", "USR2-block": True, "INT-block": True},
        {"name": "pipe-xfsz-default-in-parent", "PIPE": "default", "XFSZ": "default"},
    ]
    out = []
    for spec in specs:
        spec = dict(spec)
        name = spec.pop("name")
        state = _child_signal_state(spec)
        out.append({"name": name, "spec": spec, "expect": state})
    return out


def _child_returncode_cases():
    import signal
    import subprocess

    def run(argv, sigchld_ignored=False, kill=False, poll_first=False):
        saved = signal.getsignal(signal.SIGCHLD)
        try:
            if sigchld_ignored:
                signal.signal(signal.SIGCHLD, signal.SIG_IGN)
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"})
            first = None
            if kill:
                proc.kill()
            elif poll_first:
                first = proc.poll()
            code = proc.wait()
            proc.stdout.close()
            return {"poll_right_after_spawn": first, "returncode": code}
        finally:
            signal.signal(signal.SIGCHLD, saved)

    exit3 = ["sh", "-c", "exit 3"]
    sleeper = ["sh", "-c", "sleep 1"]
    return [
        {"name": "exit-3", "argv": exit3, "sigchld_ignored": False, "kill": False, "poll_first": False,
         "expect": run(exit3)},
        {"name": "killed-by-signal-9", "argv": ["sh", "-c", "kill -9 $$"], "sigchld_ignored": False, "kill": False,
         "poll_first": False,
         "expect": run(["sh", "-c", "kill -9 $$"])},
        {"name": "kill-method", "argv": sleeper, "sigchld_ignored": False, "kill": True, "poll_first": False,
         "expect": run(sleeper, kill=True)},
        {"name": "sigchld-ignored-exit-3", "argv": exit3, "sigchld_ignored": True, "kill": False, "poll_first": False,
         "expect": run(exit3, sigchld_ignored=True)},
        {"name": "sigchld-ignored-still-running", "argv": sleeper, "sigchld_ignored": True, "kill": False,
         "poll_first": True, "expect": run(sleeper, sigchld_ignored=True, poll_first=True)},
    ]


def _child_stdio_cases():
    """fd の受け継ぎ・環境変数の集合・作業場所・標準エラーの捨て先。"""
    import subprocess

    cases = []

    def outcome(name, argv, pairs, cwd, leak_fd=False):
        env = dict(pairs)
        fd = None
        try:
            if leak_fd:
                fd = os.open("/dev/null", os.O_RDONLY)
                os.set_inheritable(fd, True)
            result = _child_outcome(argv, env, cwd)
        finally:
            if fd is not None:
                os.close(fd)
        cases.append({"name": name, "argv": argv, "env": [list(pair) for pair in pairs], "cwd": cwd, "leak_fd": leak_fd, "expect": result})

    path = [("PATH", "/usr/bin:/bin")]
    fds = ["sh", "-c", "for f in /proc/$$/fd/*; do echo ${f##*/}; done"]
    outcome("fds-0-2-only", fds, path, None)
    outcome("fds-0-2-only-with-leaked-fd", fds, path, None, leak_fd=True)
    outcome("env-exact-in-order", ["env"], [("PATH", "/usr/bin:/bin"), ("B", ""), ("A", "1")], None)
    outcome("env-without-path-uses-defpath", ["env"], [("X", "1")], None)
    outcome("cwd-root", ["sh", "-c", "pwd"], path, "/")
    outcome("cwd-usr", ["sh", "-c", "pwd"], path, "/usr")
    outcome("stderr-to-devnull", ["sh", "-c", "echo err >&2; echo out"], path, None)
    return cases


def _child_chmod_cases():
    """リンクを辿らない chmod(`os.chmod(name, mode, dir_fd=fd, follow_symlinks=False)`)の結果。"""
    import tempfile
    import shutil

    base = tempfile.mkdtemp(dir=os.environ.get("S2E_FIXTURE_TMP"))
    try:
        os.mkdir(os.path.join(base, "d"))
        os.symlink("d", os.path.join(base, "l"))
        with open(os.path.join(base, "f"), "w"):
            pass
        fd = os.open(base, os.O_RDONLY)
        out = []
        try:
            for name in ("d", "f", "l", "missing"):
                try:
                    os.chmod(name, 0o750, dir_fd=fd, follow_symlinks=False)
                    out.append({"name": name, "ok": True})
                except Exception as error:
                    import errno
                    out.append({"name": name, "error": type(error).__name__, "message": str(error),
                                "errno": errno.errorcode.get(error.errno) if isinstance(error, OSError) else None})
        finally:
            os.close(fd)
        return out
    finally:
        shutil.rmtree(base)


def _child_env_error_cases():
    """環境変数の名前・値の検査(`Popen` が `ValueError` にするもの)と、`exec` の前の失敗のファイル名。"""
    import subprocess

    out = []
    for name, env in (
        ("equals-in-key", [("A=B", "1"), ("PATH", "/usr/bin")]),
        ("empty-key-is-allowed", [("", "1"), ("PATH", "/usr/bin")]),
        ("nul-in-key", [("A\0B", "1"), ("PATH", "/usr/bin")]),
        ("nul-in-value", [("A", "1\0"), ("PATH", "/usr/bin")]),
        ("equals-in-value-is-allowed", [("A", "x=y"), ("PATH", "/usr/bin")]),
    ):
        try:
            proc = subprocess.Popen(["env"], env=dict(env), stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
            expect = {"stdout": proc.communicate()[0].decode(), "returncode": proc.returncode}
        except ValueError as error:
            expect = {"error": "ValueError", "message": str(error)}
        out.append({"name": name, "env": [list(pair) for pair in env], "expect": expect})
    # 環境変数の誤りは、作業場所の誤りより先に出る(どちらも `fork` の前)。
    try:
        subprocess.Popen(["env"], env={"A=B": "1"}, cwd="/nonexistent")
    except ValueError as error:
        out.append({"name": "env-error-before-cwd-error", "env": [["A=B", "1"]], "expect": {"error": "ValueError", "message": str(error)}})
    return out


def _child_preexec_cases():
    """`exec` の前に失敗したとき(標準入力に閉じた fd を渡す)の、例外のファイル名の種類。"""
    import subprocess

    out = []
    for cwd in (None, "/"):
        try:
            subprocess.Popen(["true"], stdin=99, cwd=cwd)
        except OSError as error:
            target = "none" if error.filename is None else "cwd"
            out.append({"cwd": cwd, "target": target, "type": type(error).__name__})
    return out


@area("child")
def make_child():
    import shutil
    import tempfile

    if os.getuid() == 0:
        raise SystemExit("root では権限の入力が変わる。root 以外で実行する")
    root = tempfile.mkdtemp(dir=os.environ.get("S2E_FIXTURE_TMP"))
    try:
        path_cases = _child_path_cases(root)
        for case in path_cases:
            case_root = os.path.join(root, case["name"])
            os.mkdir(case_root)
            _child_layout(case_root, case["layout"])
            sub = lambda text: None if text is None else text.replace("{r}", case_root)
            env = {} if case["path"] is None else {"PATH": sub(case["path"])}
            argv = [sub(case["argv0"]), *case["rest"]]
            case["expect"] = _child_outcome(argv, env, sub(case["cwd"]))
        # 絶対パスが出力に入らないことを確かめる(固定期待に一時フォルダの名前を残さない)。
        text = json.dumps(path_cases)
        if root in text:
            raise SystemExit("固定期待に一時フォルダの名前が入った")
        return {"child_cases.json": {
            "python": "3.10",
            "path_cases": path_cases,
            "signal_cases": _child_signal_cases(),
            "returncode_cases": _child_returncode_cases(),
            "stdio_cases": _child_stdio_cases(),
            "chmod_cases": _child_chmod_cases(),
            "env_error_cases": _child_env_error_cases(),
            "preexec_cases": _child_preexec_cases(),
        }}
    finally:
        shutil.rmtree(root)


# ---------------------------------------------------------------- 木の走査・部品・記録・Git 解析(2b-1b)
# 旧版の `tree`・`component_tree`・`record`・`includes` を、同じ物理入力(一時フォルダ)に対して動かした結果。
# 一時フォルダは環境変数 S2E_FIXTURE_TMP の下(無ければ OS の既定)に作り、自分で作ったものだけを消す。
# dev・ino・時刻はその実行ごとに変わるので、`*` に置き換える。ほかの値(mode・サイズ・ハッシュ・名前・数)は
# そのまま残す。一時フォルダの絶対パスは `{r}` に置き換える。

OBSERVE_HOME = "/fixture-home"
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MASKED = "*"
# 種類ごとに、その実行ごとに変わる位置(0 始まり。0 番は種類の名前)。
MASK_POSITIONS = {
    "file-stat": (1, 2, 5, 6),
    "directory-stat": (1, 2),
    "directory-step": (2, 3),
    "link-step": (3, 4, 5),
}


def obs_mask_entry(entry):
    if not isinstance(entry, list) or not entry or not isinstance(entry[0], str):
        return entry
    kind = entry[0]
    positions = MASK_POSITIONS.get(kind)
    if kind == "installation-link":
        positions = (2, 3, 4) if len(entry) == 5 else (2, 3, 4, 5)
    if kind == "configuration-link":
        positions = (2, 3, 4, 5)
    if positions is None:
        return entry
    return [MASKED if i in positions else v for i, v in enumerate(entry)]


def obs_plain(entries, root, ancestors, drop_routes, rootkey):
    """旧版の `entries` を、固定期待に残せる JSON にする。

    部品の木の根(`rootkey`)の足取り(`@route:`・`@route-end:`)は、`/` から根までの親の数(`ancestors`)が
    一時フォルダの場所で変わるので、その数を引く(親の足取りは捨てる)。入れ子の部品の足取りは、そのまま。
    """
    def unroot(value):
        if isinstance(value, str):
            return value.replace(root, "{r}")
        if isinstance(value, list):
            return [unroot(v) for v in value]
        return value

    out = {}
    for key, value in entries.items():
        for prefix in ("@route:", "@absolute:@route:"):
            if key.startswith(prefix):
                break
        else:
            prefix = None
        if drop_routes and key.startswith(("@route:", "@absolute:", "@route-end:", "@origin:")):
            continue
        if key.startswith("@origin:"):
            value = [value[0], MASKED, MASKED, MASKED]
        elif key.startswith("@route-end:"):
            if key == "@route-end:" + str(rootkey):
                value = [value[0], value[1] - ancestors]
        elif prefix is not None:
            head, _, number = key.rpartition(":")
            if head == prefix + str(rootkey):
                if int(number) < ancestors:
                    continue
                key = head + ":" + str(int(number) - ancestors)
        out[unroot(key)] = unroot(obs_mask_entry(value))
    return out


def obs_depth_ticks(entries, root, ancestors, rootkey):
    """部品の木の呼出しが、一時フォルダの深さのせいで余計に使った予算の項目数。

    根の足取り `@route:<根>:<n>`(n < ancestors)が 1 つにつき 1 回、`{r}/…` を指す絶対パスの導入リンクが
    辿る空の要素 1 つと親の要素 `ancestors` 個で 1 + ancestors 回。足取りを捨てる回(drop_routes)も、捨てる前に数える。
    """
    if rootkey is None:
        return 0
    total = 0
    for key, value in entries.items():
        head, _, number = key.rpartition(":")
        if key.startswith("@route:") and head == "@route:" + rootkey and int(number) < ancestors:
            total += 1
        if isinstance(value, list) and value and value[0] == "link-step":
            text = value[2]
            if text == root or text.startswith(root + "/"):
                total += 1 + ancestors
    return total


def obs_build(root, items):
    """配置(layout)を作る。暗黙の親ディレクトリは 0o755。"""
    dirs = {}
    for item in items:
        path = os.path.join(root, item["path"])
        kind = item["kind"]
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            made = []
            walk = parent
            while not os.path.isdir(walk):
                made.append(walk)
                walk = os.path.dirname(walk)
            os.makedirs(parent)
            for each in made:
                dirs.setdefault(each, 0o755)
        if kind == "dir":
            os.makedirs(path, exist_ok=True)
            dirs[path] = item.get("mode", 0o755)
        elif kind == "file":
            data = item["body"].encode() if "body" in item else b"x" * item["size"]
            with open(path, "wb") as handle:
                handle.write(data)
            os.chmod(path, item.get("mode", 0o644))
        elif kind == "link":
            os.symlink(item["target"].replace("{r}", root), path)
        elif kind == "hardlink":
            os.link(os.path.join(root, item["target"]), path)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            raise SystemExit("配置の種類が不明: " + kind)
    # 浅い親から順に、mode を決める(深い方を先に 0o555 などにすると、後の chmod が届かないため)。
    for path in sorted(dirs):
        os.chmod(path, dirs[path])


def obs_apply(root, ops):
    import time
    import shutil

    # 内容を変えても時刻が同じにならないよう、カーネルの時計(粗い)が進むのを待つ。
    time.sleep(0.03)
    for op in ops:
        path = os.path.join(root, op["path"]) if "path" in op else None
        kind = op["op"]
        if kind == "write":
            with open(path, "wb") as handle:
                handle.write(op["body"].encode())
            if "mode" in op:
                os.chmod(path, op["mode"])
        elif kind == "rm":
            os.unlink(path)
        elif kind == "rmtree":
            shutil.rmtree(path)
        elif kind == "mkdir":
            os.makedirs(path, exist_ok=True)
            os.chmod(path, op.get("mode", 0o755))
        elif kind == "link":
            if os.path.lexists(path):
                os.unlink(path)
            os.symlink(op["target"].replace("{r}", root), path)
        elif kind == "rename":
            os.rename(path, os.path.join(root, op["to"]))
        elif kind == "chmod":
            os.chmod(path, op["mode"])
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            raise SystemExit("操作の種類が不明: " + kind)


def obs_run(guard, root, ancestors, run, previous):
    """1 回の呼出し。`previous` は、前の回の (entries, budget)。"""
    import hashlib

    if run.get("ops_before"):
        obs_apply(root, run["ops_before"])
    call = run["call"]
    budget = previous[1] if run.get("reuse_budget") else guard.Budget()
    # 一時フォルダの深さで変わる予算の分(部品の木の `/` から根までの足取り)を、記録の前に引く。
    adjust = previous[2] if run.get("reuse_budget") else 0
    preload = run.get("preload", {})
    if "entries" in preload:
        # 部品の木では、先に親の足取りが `ancestors` 回、予算を使う。上限に当たる場所が深さで動かないよう、
        # その分を先に引いておき、記録のときに足し戻す。
        offset = ancestors if call["fn"] == "component_tree" else 0
        budget.entries = preload["entries"] - offset
        adjust = -offset
    if "bytes" in preload:
        budget.bytes = preload["bytes"]
    expected = None
    if run.get("expected_from") is not None:
        expected = previous_entries[run["expected_from"]]
    entries = {}
    blobs = {} if call.get("blobs") else None
    sub = lambda text: text.replace("{r}", root)
    ret = None
    error = None
    import functools

    def at_depth(target, function):
        """`function` の本体のフレームの深さが、ちょうど `target` になるよう、Python のフレームを積んで呼ぶ。"""
        def rec():
            if frame_count(sys._getframe()) < target - 1:
                return rec()
            return function()
        return rec() if target is not None else function()

    target = run.get("depth")
    try:
        # `functools.partial` はキーワード引数があると、C の呼出し(`tp_call`)を 1 つ挟んで深さが 1 増える。
        # 呼出元のフレームの深さを正確にするため、位置引数だけで呼ぶ。
        if call["fn"] == "tree":
            at_depth(target, functools.partial(
                guard.tree, sub(call["path"]), call["key"], budget, entries, blobs,
                call.get("optional", False), call.get("user_links", False), expected))
        elif call["fn"] == "component_tree":
            ret = at_depth(target, functools.partial(
                guard.component_tree, sub(call["path"]), call["kind"], budget, entries, expected, blobs))
        elif call["fn"] == "record":
            raw = at_depth(target, functools.partial(
                guard.record, sub(call["path"]), call["key"], budget, entries, expected,
                call.get("depth", 0), call.get("required", False)))
            ret = None if raw is None else raw.hex()
        else:
            raise SystemExit("呼出しが不明: " + call["fn"])
    except Exception as caught:  # noqa: BLE001 旧版の例外の型と文面をそのまま記録する
        error = {"type": type(caught).__name__, "msg": str(caught).replace(root, "{r}")}
    rootkey = "component:%s:%s" % (call["kind"], sub(call["path"])) if call["fn"] == "component_tree" else None
    depth_ticks = obs_depth_ticks(entries, root, ancestors, rootkey)
    result = {
        "ret": ret.replace(root, "{r}") if isinstance(ret, str) and call["fn"] == "component_tree" else ret,
        "entries": obs_plain(entries, root, ancestors, run.get("drop_routes", False),
                             "component:%s:%s" % (call["kind"], sub(call["path"])) if call["fn"] == "component_tree" else None),
        "blobs": [[k.replace(root, "{r}"), hashlib.sha256(v).hexdigest(), len(v)] for k, v in (blobs or {}).items()],
        "budget": [budget.entries - adjust - depth_ticks, budget.bytes],
        "error": error,
    }
    if run.get("summary"):
        # 大きな控えは、数と SHA-256(鍵を並べた、空白なしの JSON)だけを残す。
        import hashlib
        text = json.dumps(result["entries"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        result["entries"] = {"count": len(result["entries"]), "sha256": hashlib.sha256(text.encode()).hexdigest()}
    if expected is not None:
        result["equals_expected"] = entries == expected
    return result, entries, budget, adjust + depth_ticks


previous_entries = []


def obs_scenario(guard, case):
    import shutil
    import tempfile

    global previous_entries
    base = os.path.realpath(tempfile.mkdtemp(dir=os.environ.get("S2E_FIXTURE_TMP")))
    root = os.path.join(base, "case")
    os.mkdir(root)
    ancestors = len([p for p in root.split("/") if p])
    previous_entries = []
    results = []
    try:
        obs_build(root, case["layout"])
        previous = (None, None, 0)
        for run in case["runs"]:
            result, entries, budget, adjust = obs_run(guard, root, ancestors, run, previous)
            previous_entries.append(entries)
            previous = (entries, budget, adjust)
            results.append(result)
        text = json.dumps(results)
        if base in text:
            raise SystemExit("固定期待に一時フォルダの名前が入った: " + case["name"])
    finally:
        shutil.rmtree(base)
    return results


def L(path, kind="file", **kw):
    return {"path": path, "kind": kind, **kw}


def T(key="k", path="{r}/t", **kw):
    return {"fn": "tree", "path": path, "key": key, **kw}


def C(kind, path, **kw):
    return {"fn": "component_tree", "kind": kind, "path": path, **kw}


def R(path, key="external:x", **kw):
    return {"fn": "record", "path": path, "key": key, **kw}


def obs_cases():
    cases = []

    def add(name, layout, *runs):
        cases.append({"name": name, "layout": layout, "runs": [r if "call" in r else {"call": r} for r in runs]})

    def run(call, **kw):
        return {"call": call, **kw}

    # ---- tree
    tree_files = [L("t/z.txt", body="zed", mode=0o600), L("t/a.txt", body="alpha"), L("t/m/inner.txt", body="in"),
                  L("t/m/deep/leaf.txt", body="leaf", mode=0o400), L("t/empty", "dir", mode=0o700),
                  L("t/B.txt", body="upper")]
    add("tree-basic", tree_files, T(blobs=True))
    add("tree-no-blobs", tree_files, T())
    add("tree-missing-required", [L("t/x", body="x")], T(path="{r}/none"))
    add("tree-missing-optional", [L("t/x", body="x")], T(path="{r}/none", optional=True))
    add("tree-missing-parent-optional", [L("t/x", body="x")], T(path="{r}/none/deeper", optional=True))
    add("tree-parent-is-file", [L("f", body="x")], T(path="{r}/f/sub", optional=True))
    add("tree-root-is-file", [L("f", body="x")], T(path="{r}/f"))
    add("tree-root-is-link", [L("real/a", body="a"), L("t", "link", target="{r}/real")], T())
    add("tree-link-without-user-links", [L("t/a", body="a"), L("t/l", "link", target="a")], T())
    add("tree-fifo", [L("t/a", body="a"), L("t/p", "fifo")], T())
    add("tree-unreadable", [L("t/a", body="a", mode=0o000)], T())
    add("tree-write-only-file", [L("t/a", body="a", mode=0o200)], T())
    add("tree-subdir-link", [L("t/sub/l", "link", target="x"), L("t/a", body="a")], T())
    links = [L("t/own.txt", body="own"), L("store/skill/s.md", body="skill"), L("store/skill2/s2.md", body="two"),
             L("store/skill2/nested/n.md", body="nested"),
             L("t/abs", "link", target="{r}/store/skill"), L("t/rel", "link", target="../store/skill2")]
    add("tree-user-links", links, T(user_links=True, blobs=True))
    add("tree-user-links-optional-dangling", [L("t/ok", body="ok"), L("t/dang", "link", target="{r}/nowhere")],
        T(user_links=True, optional=True))
    add("tree-user-links-dangling-required", [L("t/ok", body="ok"), L("t/dang", "link", target="{r}/nowhere")],
        T(user_links=True))
    add("tree-user-links-target-is-file", [L("t/f", "link", target="{r}/file"), L("file", body="x")], T(user_links=True))
    add("tree-user-links-nested-link-ignored", links + [L("t/sub/inner", "link", target="{r}/store/skill")],
        T(user_links=True))
    add("tree-user-links-expected-same", links, T(user_links=True, blobs=True),
        run(T(user_links=True, blobs=True), expected_from=0))
    add("tree-user-links-expected-new-link", links, T(user_links=True),
        run(T(user_links=True), ops_before=[{"op": "link", "path": "t/more", "target": "{r}/store/skill"}], expected_from=0))
    add("tree-user-links-expected-text-changed", links, T(user_links=True),
        run(T(user_links=True), ops_before=[{"op": "link", "path": "t/abs", "target": "{r}/store/skill2"}], expected_from=0))
    add("tree-user-links-expected-target-replaced", links, T(user_links=True),
        run(T(user_links=True), ops_before=[
            {"op": "mkdir", "path": "store/new"}, {"op": "write", "path": "store/new/s.md", "body": "skill"},
            {"op": "rename", "path": "store/skill", "to": "store/old"}, {"op": "rename", "path": "store/new", "to": "store/skill"}],
            expected_from=0))
    add("tree-user-links-expected-removed-link", links, T(user_links=True),
        run(T(user_links=True), ops_before=[{"op": "rm", "path": "t/abs"}], expected_from=0))
    add("tree-budget-items-last-ok", [L("t/a%d" % i, body="x") for i in range(5)], run(T(), preload={"entries": 10000 - 5}))
    add("tree-budget-items-one-over", [L("t/a%d" % i, body="x") for i in range(5)], run(T(), preload={"entries": 10000 - 4}))
    add("tree-budget-bytes-last-ok", [L("t/a", body="0123456789")], run(T(), preload={"bytes": MAX_TOTAL - 10}))
    add("tree-budget-bytes-one-over", [L("t/a", body="0123456789")], run(T(), preload={"bytes": MAX_TOTAL - 9}))
    add("tree-file-limit-ok", [L("t/big", size=MAX_FILE)], T(blobs=True))
    add("tree-file-limit-over", [L("t/big", size=MAX_FILE + 1)], T(blobs=True))
    add("tree-empty-dir", [L("t", "dir")], T(blobs=True))

    # ---- component_tree
    plugin = [L("p/b.txt", body="bee"), L("p/a/x.md", body="ex"), L("p/a/deep/y.md", body="why", mode=0o444),
              L("p/empty", "dir"), L("p/Z", body="zed", mode=0o755)]
    add("component-plugin", plugin, C("plugin", "{r}/p", blobs=True))
    add("component-plugin-no-blobs", plugin, C("plugin", "{r}/p"))
    add("component-plugin-missing", plugin, C("plugin", "{r}/none"))
    add("component-skill-missing", plugin, C("personal-skill", "{r}/none"))
    add("component-skill-missing-deep-parent", plugin, C("personal-agent", "{r}/none/deeper/agents"))
    add("component-kind-unknown", plugin, C("nonsense", "{r}/p"))
    add("component-root-is-file", [L("f", body="x")], C("plugin", "{r}/f"))
    add("component-root-link-parent", [L("real/skills/s.md", body="s"), L("lnk", "link", target="real")],
        C("personal-skill", "{r}/lnk/skills"))
    add("component-root-link-itself", [L("real/s.md", body="s"), L("skills", "link", target="real")],
        C("personal-skill", "{r}/skills"))
    add("component-plugin-link-rejected", plugin + [L("p/l", "link", target="a")], C("plugin", "{r}/p"))
    add("component-command-link-rejected", [L("c/x.md", body="x"), L("c/l", "link", target="x.md")], C("personal-command", "{r}/c"))
    skills = [L("s/own/SKILL.md", body="own"), L("store/one/SKILL.md", body="one"), L("store/two/SKILL.md", body="two"),
              L("store/two/res/r.txt", body="res"),
              L("s/rel", "link", target="../store/one"), L("s/hop", "link", target="rel2"),
              L("s/rel2", "link", target="../store/two")]
    add("component-skill-links", skills, C("personal-skill", "{r}/s", blobs=True))
    add("component-enterprise-skill-links", skills, C("enterprise-skill", "{r}/s", blobs=True))
    add("component-skill-abs-link", skills + [L("s/abs", "link", target="{r}/store/one")],
        run(C("personal-skill", "{r}/s", blobs=True), drop_routes=True))
    add("component-skill-link-to-file", skills + [L("s/tofile", "link", target="own/SKILL.md")], C("personal-skill", "{r}/s"))
    add("component-skill-link-dangling", skills + [L("s/gone", "link", target="../store/none")], C("personal-skill", "{r}/s"))
    add("component-skill-link-dotdot", skills + [L("s/dots", "link", target="own/../../store/one")], C("personal-skill", "{r}/s", blobs=True))
    add("component-skill-link-loop", [L("s/a", "link", target="a")], C("personal-skill", "{r}/s"))
    add("component-skill-link-loop-pair", [L("s/a", "link", target="b"), L("s/b", "link", target="a")], C("personal-skill", "{r}/s"))
    # 導入リンクの鎖: `s/e` から数えて `count` 回のリンクを辿って、実体のディレクトリへ着く(`e` を 1 回目と数える)。
    for count in (39, 40, 41):
        chain = [L("chain/c%d" % i, "link", target="c%d" % (i + 1)) for i in range(1, count - 1)]
        chain.append(L("chain/c%d" % (count - 1), "link", target="../store/one"))
        add("component-link-chain-%d" % count,
            [L("store/one/SKILL.md", body="one"), L("s/e", "link", target="../chain/c1")] + chain,
            C("personal-skill", "{r}/s"))
    for depth_count in (100, 101):
        layout = [L("d/" + "/".join("d" for _ in range(depth_count)) + "/leaf.txt", body="leaf")]
        add("component-depth-%d" % depth_count, layout, C("plugin", "{r}/d"))
    mani = "{r}/m/.claude-plugin/marketplace.json"
    add("component-manifest-file", [L("m/.claude-plugin/marketplace.json", body="{}"), L("m/other/x", body="x")],
        C("marketplace-manifest", mani, blobs=True))
    add("component-manifest-missing", [L("m/.claude-plugin/other", body="x")], C("marketplace-manifest", mani))
    add("component-manifest-parent-missing", [L("x", body="x")], C("marketplace-manifest", mani))
    add("component-manifest-is-dir", [L("m/.claude-plugin/marketplace.json/x", body="x")], C("marketplace-manifest", mani))
    add("component-manifest-is-link", [L("m/.claude-plugin/real.json", body="{}"),
                                       L("m/.claude-plugin/marketplace.json", "link", target="real.json")],
        C("marketplace-manifest", mani))
    add("component-fifo", plugin + [L("p/pipe", "fifo")], C("plugin", "{r}/p"))
    add("component-unreadable", plugin + [L("p/secret", body="s", mode=0o000)], C("plugin", "{r}/p"))
    add("component-hardlinks-read-once", [L("p/a", body="same-bytes"), L("p/b", "hardlink", target="p/a"),
                                          L("p/c", "hardlink", target="p/a")], C("plugin", "{r}/p", blobs=True))
    add("component-hardlinks-across-components",
        [L("p/a", body="shared-bytes"), L("q/b", "hardlink", target="p/a")],
        C("plugin", "{r}/p", blobs=True), run(C("plugin", "{r}/q", blobs=True), reuse_budget=True))
    add("component-distinct-files-read-twice", [L("p/a", body="same-bytes"), L("p/b", body="same-bytes")],
        C("plugin", "{r}/p", blobs=True))
    add("component-expected-same", plugin, C("plugin", "{r}/p", blobs=True),
        run(C("plugin", "{r}/p", blobs=True), expected_from=0))
    add("component-expected-new-file", plugin, C("plugin", "{r}/p"),
        run(C("plugin", "{r}/p"), ops_before=[{"op": "write", "path": "p/new.txt", "body": "n", "mode": 0}], expected_from=0))
    add("component-expected-removed-file", plugin, C("plugin", "{r}/p"),
        run(C("plugin", "{r}/p"), ops_before=[{"op": "rm", "path": "p/b.txt"}], expected_from=0))
    add("component-expected-changed-content", plugin, C("plugin", "{r}/p"),
        run(C("plugin", "{r}/p"), ops_before=[{"op": "write", "path": "p/b.txt", "body": "bat"}], expected_from=0))
    add("component-expected-link-retargeted", skills, C("personal-skill", "{r}/s"),
        run(C("personal-skill", "{r}/s"), ops_before=[{"op": "link", "path": "s/rel", "target": "../store/two"}], expected_from=0))
    add("component-expected-link-target-replaced", skills, C("personal-skill", "{r}/s"),
        run(C("personal-skill", "{r}/s"), ops_before=[
            {"op": "mkdir", "path": "store/new"}, {"op": "write", "path": "store/new/SKILL.md", "body": "one"},
            {"op": "rename", "path": "store/one", "to": "store/old"}, {"op": "rename", "path": "store/new", "to": "store/one"}],
            expected_from=0))
    add("component-expected-missing-then-created", plugin, C("personal-skill", "{r}/none"),
        run(C("personal-skill", "{r}/none"), ops_before=[{"op": "mkdir", "path": "none"}], expected_from=0))
    add("component-expected-manifest-missing-then-created", [L("m/.claude-plugin/other", body="x")],
        C("marketplace-manifest", mani),
        run(C("marketplace-manifest", mani), ops_before=[{"op": "write", "path": "m/.claude-plugin/marketplace.json", "body": "{}"}],
            expected_from=0))
    add("component-budget-items-over", plugin, run(C("plugin", "{r}/p"), preload={"entries": 10000 - 3}))
    add("component-budget-bytes-over", [L("p/a", body="0123456789")],
        run(C("plugin", "{r}/p"), preload={"bytes": MAX_TOTAL - 9}))
    add("component-budget-bytes-last-ok", [L("p/a", body="0123456789")],
        run(C("plugin", "{r}/p"), preload={"bytes": MAX_TOTAL - 10}))

    # ---- record
    f = [L("c/file.json", body="{\"a\": 1}", mode=0o640)]
    add("record-file", f, R("{r}/c/file.json"))
    add("record-missing", f, R("{r}/c/none.json"))
    add("record-missing-parent", f, R("{r}/none/none.json"))
    add("record-parent-is-file", [L("c", body="x")], R("{r}/c/none.json"))
    add("record-directory", f, R("{r}/c"))
    add("record-unreadable", [L("c/s", body="x", mode=0o000)], R("{r}/c/s"))
    add("record-fifo", [L("c/p", "fifo")], R("{r}/c/p"))
    add("record-link", f + [L("c/l", "link", target="file.json")], R("{r}/c/l"))
    add("record-link-absolute", f + [L("c/l", "link", target="{r}/c/file.json")], R("{r}/c/l"))
    add("record-link-chain", f + [L("c/l1", "link", target="l2"), L("c/l2", "link", target="{r}/c/l3"), L("c/l3", "link", target="file.json")],
        R("{r}/c/l1"))
    add("record-link-dotdot", f + [L("c/sub/x", body="x"), L("c/l", "link", target="sub/../file.json")], R("{r}/c/l"))
    add("record-link-dangling", [L("c/l", "link", target="gone")], R("{r}/c/l"))
    add("record-link-to-dir", [L("c/d/x", body="x"), L("c/l", "link", target="d")], R("{r}/c/l"))
    add("record-link-loop", [L("c/l", "link", target="l")], R("{r}/c/l"))
    for count in (39, 40, 41):
        chain = [L("c/l%d" % i, "link", target="l%d" % (i + 1)) for i in range(count - 1)]
        chain.append(L("c/l%d" % (count - 1), "link", target="file.json"))
        add("record-link-depth-%d" % count, f + chain, R("{r}/c/l0"))
    add("record-depth-argument-over", f, run(R("{r}/c/file.json", depth=41)))
    add("record-depth-argument-last", f, run(R("{r}/c/file.json", depth=40)))
    add("record-required-missing", f, R("{r}/c/none.json", required=True))
    add("record-budget-bytes-over", f, run(R("{r}/c/file.json"), preload={"bytes": MAX_TOTAL - 7}))
    add("record-budget-items-over", f, run(R("{r}/c/file.json"), preload={"entries": 10000}))
    add("record-expected-same", f + [L("c/l", "link", target="file.json")], R("{r}/c/file.json"),
        run(R("{r}/c/file.json"), expected_from=0))
    add("record-expected-key-unknown", f, R("{r}/c/file.json"),
        run(R("{r}/c/file.json", key="external:other"), expected_from=0))
    add("record-expected-missing-then-created", f, R("{r}/c/new.json"),
        run(R("{r}/c/new.json"), ops_before=[{"op": "write", "path": "c/new.json", "body": "{}"}], expected_from=0))
    add("record-expected-still-missing", f, R("{r}/c/new.json"), run(R("{r}/c/new.json"), expected_from=0))
    add("record-expected-content-changed", f, R("{r}/c/file.json"),
        run(R("{r}/c/file.json"), ops_before=[{"op": "write", "path": "c/file.json", "body": "{\"a\": 2}"}], expected_from=0))
    lnk = f + [L("c/l", "link", target="file.json")]
    add("record-expected-link-same", lnk, R("{r}/c/l"), run(R("{r}/c/l"), expected_from=0))
    add("record-expected-link-text-changed", lnk + [L("c/other.json", body="o")], R("{r}/c/l"),
        run(R("{r}/c/l"), ops_before=[{"op": "link", "path": "c/l", "target": "other.json"}], expected_from=0))
    add("record-expected-link-target-replaced", lnk, R("{r}/c/l"),
        run(R("{r}/c/l"), ops_before=[
            {"op": "write", "path": "c/new.json", "body": "{\"a\": 1}", "mode": 0o640},
            {"op": "rename", "path": "c/file.json", "to": "c/old.json"}, {"op": "rename", "path": "c/new.json", "to": "c/file.json"}],
            expected_from=0))
    add("record-expected-file-became-link", f, R("{r}/c/file.json"),
        run(R("{r}/c/file.json"), ops_before=[
            {"op": "rename", "path": "c/file.json", "to": "c/real.json"}, {"op": "link", "path": "c/file.json", "target": "real.json"}],
            expected_from=0))
    add("record-expected-link-became-file", lnk, R("{r}/c/l"),
        run(R("{r}/c/l"), ops_before=[{"op": "rm", "path": "c/l"}, {"op": "write", "path": "c/l", "body": "x"}], expected_from=0))
    return cases


# ---- Git 設定の解析(includes)

# 記録用の偽の git(Python)。呼ばれた引数・標準入力の SHA-256・環境を 1 行の JSON で記録し、本物の git を同じ引数・
# 標準入力で呼んで、その標準出力と終了コードをそのまま返す。`@PYTHON@`・`@OUT@`・`@GIT@` は、使う側が置き換える。
GIT_RECORDER = """#!@PYTHON@ -IS
import hashlib, json, os, subprocess, sys
data = sys.stdin.buffer.read()
call = {"argv": [os.fsdecode(a) for a in sys.argv[1:]], "stdin": hashlib.sha256(data).hexdigest(),
        "env": {os.fsdecode(k): os.fsdecode(v) for k, v in os.environb.items()}}
with open("@OUT@/calls.jsonl", "a") as handle:
    handle.write(json.dumps(call) + "\\n")
done = subprocess.run(["@GIT@"] + sys.argv[1:], input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
sys.stdout.buffer.write(done.stdout)
sys.exit(done.returncode)
"""


def recorder_text(python, out, git):
    for value in (python, out, git):
        assert "'" not in value and '"' not in value and "\\" not in value
    return GIT_RECORDER.replace("@PYTHON@", python).replace("@OUT@", out).replace("@GIT@", git)


def git_cases_definition():
    return [
        {"name": "no-include", "raw": "[user]\n\tname = x\n"},
        {"name": "relative", "raw": "[include]\n\tpath = inc.cfg\n"},
        {"name": "absolute", "raw": "[include]\n\tpath = /abs/inc.cfg\n"},
        {"name": "two-values-one-key", "raw": "[include]\n\tpath = a\n\tpath = b\n"},
        {"name": "include-if", "raw": "[includeIf \"gitdir:/x/\"]\n\tpath = c\n[includeIf \"onbranch:main\"]\n\tpath = d\n[includeif \"gitdir/i:/Y/\"]\n\tpath = e\n"},
        {"name": "case-insensitive", "raw": "[INCLUDE]\n\tPATH = up\n"},
        {"name": "tilde", "raw": "[include]\n\tpath = ~/inc.cfg\n"},
        {"name": "empty-value", "raw": "[include]\n\tpath =\n"},
        {"name": "dotdot", "raw": "[include]\n\tpath = ../x/../y\n"},
        {"name": "syntax-error", "raw": "[include\n\tpath = x\n"},
        {"name": "quoted-and-continued", "raw": "[include]\n\tpath = \"dir with space/x\"\n[include]\n\tpath = long\\\nname\n"},
        {"name": "boolean-path", "raw": "[include]\n\tpath\n"},
        {"name": "non-utf8", "raw_hex": "5b696e636c7564655d0a09706174683d636166e90a"},
        {"name": "entries-last-ok", "many": 9998, "tail": "[include]\n\tpath = last\n"},
        {"name": "entries-over", "many": 9999, "tail": "[include]\n\tpath = last\n"},
        {"name": "only-section-keys", "raw": "[includeX]\n\tpath = no\n[include]\n\tother = no\n[includeif]\n\tpath = odd\n"},
        {"name": "empty-raw", "raw": ""},
    ]


def git_raw(case):
    if "many" in case:
        return ("[s]\n" + "".join("\tk%d = v\n" % i for i in range(case["many"])) + case["tail"]).encode()
    return bytes.fromhex(case["raw_hex"]) if "raw_hex" in case else case["raw"].encode()


def git_run_old(guard, case, root, bindir, recorder):
    """旧版の `includes` を、記録用の偽の git(本物の git を呼ぶ)の PATH で動かす。"""
    from pathlib import Path

    raw = git_raw(case)
    env = git_env_base(bindir)
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        try:
            value = guard.includes(Path(root) / "cfg/config", raw)
            return {"ok": [[ord(c) for c in item.replace(root, "{r}")] for item in value]}
        except Exception as error:  # noqa: BLE001
            return {"error": {"type": type(error).__name__, "msg": str(error).replace(root, "{r}")}}
    finally:
        os.environ.clear()
        os.environ.update(saved)


def git_env_base(bindir):
    return {"PATH": bindir + ":" + os.environ.get("PATH", "/usr/bin:/bin"), "HOME": OBSERVE_HOME, "LANG": "C.UTF-8"}


GIT_ENV_CASE = {
    "FOO": "bar",
    "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c", "GIT_CONFIG_KEY_1": "d.e",
    "GIT_CONFIG_PARAMETERS": "'x.y=z'", "GIT_CONFIG_NOSYSTEM": "0", "GIT_CONFIG_GLOBAL": "/home/other/gitconfig",
    "GIT_CONFIG_SYSTEM": "/etc/gitconfig", "GIT_TERMINAL_PROMPT": "1", "GIT_NO_LAZY_FETCH": "0",
    "GIT_CONFIG_KEYS": "kept", "GIT_CONFIGX": "kept2",
}


def git_cases():
    import shutil
    import tempfile
    import subprocess

    guard = load_guard()
    git = shutil.which("git")
    python = sys.executable
    base = os.path.realpath(tempfile.mkdtemp(dir=os.environ.get("S2E_FIXTURE_TMP")))
    out = {"cases": [], "recorder": GIT_RECORDER, "env_extra": GIT_ENV_CASE}
    try:
        root = os.path.join(base, "r")
        os.makedirs(root + "/cfg")
        bindir = os.path.join(base, "bin")
        os.mkdir(bindir)
        recorder = os.path.join(bindir, "git")
        text = recorder_text(python, base, git)
        with open(recorder, "w") as handle:
            handle.write(text)
        os.chmod(recorder, 0o755)
        for case in git_cases_definition():
            record_file = os.path.join(base, "calls.jsonl")
            if os.path.exists(record_file):
                os.unlink(record_file)
            result = git_run_old(guard, case, root, bindir, recorder)
            calls = []
            if os.path.exists(record_file):
                for line in open(record_file):
                    call = json.loads(line)
                    env = {k: v for k, v in call["env"].items() if k not in ("PATH", "__CF_USER_TEXT_ENCODING")}
                    calls.append({"argv": call["argv"], "stdin": call["stdin"], "env": env})
            out["cases"].append({**case, "result": result, "calls": calls})
        # 環境変数の扱い(除くもの・足すもの・上書きするもの)。
        env_case = {"raw": "[include]\n\tpath = a\n"}
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update({**git_env_base(bindir), **GIT_ENV_CASE})
        record_file = os.path.join(base, "calls.jsonl")
        if os.path.exists(record_file):
            os.unlink(record_file)
        try:
            from pathlib import Path
            guard.includes(Path(root) / "cfg/config", env_case["raw"].encode())
        finally:
            os.environ.clear()
            os.environ.update(saved)
        calls = [json.loads(line) for line in open(record_file)]
        out["env_case"] = {"raw": env_case["raw"], "calls": [
            {"argv": c["argv"], "env": {k: v for k, v in c["env"].items() if k not in ("PATH", "__CF_USER_TEXT_ENCODING")}}
            for c in calls]}
        text = json.dumps(out)
        if base in text:
            raise SystemExit("固定期待に一時フォルダの名前が入った")
        out["safe_prefix"] = list(guard.SAFE_GIT_PREFIX)
        return out
    finally:
        shutil.rmtree(base)


# ---------------------------------------------------------------- environ の生の項目

# 旧 Python 3.10.12 の `os.environ` は、`environ` の各項目を最初の `=` で分け(名前が空でもよい)、`=` の無い項目を捨て、
# 同じ名前が重なれば最初の値と位置を残す(`convertenviron` の `PyDict_SetDefault`)。`dict(os.environ)` が子の環境になる。
# `os.environ` は辞書なので、重なった名前を持つ `environ` は ctypes の `execve` で作る。
ENVIRON_MARKER = "S2E_PLATFORM_ISOLATED=environ"

ENVIRON_LAUNCH = r"""
import ctypes, json, sys
spec = json.loads(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
envp = (ctypes.c_char_p * (len(spec["envp"]) + 1))(*[e.encode() for e in spec["envp"]], None)
argv = (ctypes.c_char_p * len(spec["argv"] + [None]))(*[a.encode() for a in spec["argv"]], None)
sys.stdout.flush()
libc.execve(spec["path"].encode(), argv, envp)
raise SystemExit("execve が失敗した: %d" % ctypes.get_errno())
"""

ENVIRON_PROBE = r"""
import json, os, subprocess, sys
pairs = [[os.fsencode(k).decode("latin-1"), os.fsencode(v).decode("latin-1")] for k, v in dict(os.environ).items()]
try:
    subprocess.run([sys.executable, "-c", "pass"], env=dict(os.environ), check=True)
    spawn = "ok"
except Exception as caught:
    spawn = type(caught).__name__ + ": " + str(caught)
print(json.dumps({"pairs": pairs, "spawn": spawn}))
"""

ENVIRON_CASES = [
    # (名前, envp, 地域設定の足し込み(PEP 538)を Linux で比べるか)
    ("dup-first-wins", ["A=1", "A=2", "PATH=/usr/bin:/bin", "LC_ALL=C"], False),
    ("dup-path-first-wins", ["PATH=/first", "A=1", "PATH=/second", "LC_ALL=C"], False),
    ("leading-equals", ["=foo=bar", "A=1", "LC_ALL=C"], False),
    ("only-equals", ["=", "A=", "LC_ALL=C"], False),
    ("empty-name-dup", ["=one", "A=1", "=two", "LC_ALL=C"], False),
    ("no-equals-dropped", ["NOEQUALS", "A=1", "NOEQ2", "LC_ALL=C"], False),
    ("value-with-equals", ["A=x=y=z", "B==", "LC_ALL=C"], False),
    ("brief-mixed", ["A=1", "A=2", "=foo=bar", "PATH=/usr/bin:/bin", "=", "B=x=y", "NOEQ", "PATH=/other", "LC_ALL=C"], False),
    ("coerced-linux-no-locale", ["A=1", "A=2", "=foo=bar"], True),
    ("coerced-linux-lang-c", ["LANG=C", "A=1", "LANG=POSIX"], True),
    ("coerced-linux-lc-ctype-empty", ["LC_CTYPE=", "A=1", "LC_CTYPE=C.UTF-8"], True),
]


def environ_cases():
    import subprocess

    out = {"python": "3.10", "marker": ENVIRON_MARKER, "cases": []}
    for name, envp, coerce in ENVIRON_CASES:
        full = envp + [ENVIRON_MARKER]
        spec = {"path": sys.executable, "argv": [sys.executable, "-B", "-c", ENVIRON_PROBE], "envp": full}
        result = subprocess.run([sys.executable, "-B", "-c", ENVIRON_LAUNCH, json.dumps(spec)],
                                capture_output=True, timeout=60)
        if result.returncode != 0:
            raise SystemExit("環境の観測が失敗した(%s): %r" % (name, result.stderr))
        observed = json.loads(result.stdout)
        out["cases"].append({"name": name, "envp": full, "coerce": coerce,
                             "pairs": observed["pairs"], "spawn": observed["spawn"]})
    return out


@area("environ")
def make_environ():
    if sys.platform != "linux":
        raise SystemExit("PEP 538 の足し込みの固定期待は Linux で作る")
    return {"environ_cases.json": environ_cases()}


@area("observe")
def make_observe():
    if os.getuid() == 0:
        raise SystemExit("root では権限の入力が変わる。root 以外で実行する")
    guard = load_guard()
    cases = obs_cases()
    for case in cases:
        case["results"] = obs_scenario(guard, case)
    return {"observe_cases.json": {"python": "3.10", "cases": cases}, "git_cases.json": git_cases()}


# ---- 再帰の深さ(2b-1b の続き)
# 旧版の `tree` を、呼出元のフレームの深さ(`depth`)を 1 つずつ変えて呼び、RecursionError が出る位置(誤りの文面・
# それまでに控えた項目と予算)を測る。基準の起動は旧 guard の直接起動(`python3 -B <guard>`)で、呼出元の深さは
# 固定期待の生成スクリプトが積む Python のフレームの数で決める。Rust の `depth` は、`tree` のフレームの深さ。


def depth_chain(count, leaf, root="t", every=None, extra_leaf_files=0):
    path = root + "/" + "/".join("d%d" % i for i in range(count))
    items = [L(root + "/top.txt", body="top")]
    if every is not None:
        for i in range(count):
            items.append(L(root + "/" + "/".join("d%d" % j for j in range(i + 1)) + "/f.txt", body="level%d" % i))
    if leaf == "file":
        items.append(L(path + "/leaf.txt", body="leaf"))
    elif leaf == "files":
        items += [L(path + "/%s.txt" % name, body=name) for name in ("c", "a", "b")]
    else:
        items.append(L(path, "dir"))
    return items


def tree_depth_cases():
    cases = []

    def add(name, layout, call, depths):
        cases.append({"name": name, "layout": layout,
                      "runs": [{"call": call, "depth": d, "summary": True} for d in depths]})

    near = lambda count, extra=0: range(1000 - count - 14 - extra, 1000 - count + 4)
    add("depth-tree-dir-leaf", depth_chain(95, "dir"), T(), near(95))
    add("depth-tree-file-leaf", depth_chain(95, "file"), T(), near(95))
    add("depth-tree-files-leaf", depth_chain(95, "files"), T(), near(95))
    add("depth-tree-file-every-level", depth_chain(90, "file", every=True), T(), near(90))
    # 導入リンクの先の木(`walk_at` は、リンクの先を `parent` の中で走査する)
    deep = "/".join("c%d" % i for i in range(90))
    link_dir = [L("t/own.txt", body="own"), L("t/lnk", "link", target="{r}/store/top"), L("store/top/" + deep, "dir")]
    add("depth-tree-link-dir-leaf", link_dir, T(user_links=True), near(90, 4))
    link_file = [L("t/own.txt", body="own"), L("t/lnk", "link", target="{r}/store/top"),
                 L("store/top/" + deep + "/f.txt", body="in-link")]
    add("depth-tree-link-file-leaf", link_file, T(user_links=True), near(90, 4))
    # 木の入口(`parent` の連なり)が上限に当たる深さ
    small = [L("t/a.txt", body="a"), L("t/sub/b.txt", body="b")]
    add("depth-tree-prologue", small, T(), range(960, 1001))
    small_link = [L("t/a.txt", body="a"), L("t/lnk", "link", target="{r}/store"), L("store/s.txt", body="s")]
    add("depth-tree-prologue-link", small_link, T(user_links=True), range(960, 1001))
    return cases


@area("depth")
def make_depth():
    if os.getuid() == 0:
        raise SystemExit("root では権限の入力が変わる。root 以外で実行する")
    guard = load_guard()
    cases = tree_depth_cases()
    for case in cases:
        case["results"] = obs_scenario(guard, case)
    return {"depth_cases.json": {"python": "3.10", "cases": cases}}


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
