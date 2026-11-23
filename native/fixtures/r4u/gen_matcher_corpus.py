#!/usr/bin/env python3
"""R4U の 2 種類の照合の生成入力と、旧版の正規表現の結果を作る。製品の入口ではない。

- 決まった乱数の種で入力を作り、旧版 check_links.py の LINK_RE・CODEPATH_RE・strip_link をそのまま当てる。
- 出力は matcher-corpus.json。Python 3.10 系なら、流し直しても同じバイト列になる。
- 使い方: python3 -B native/fixtures/r4u/gen_matcher_corpus.py [--out <パス>]
"""
import argparse
import hashlib
import importlib.util
import json
import random
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LEGACY_REL = "plugins/dev-workflow/skills/update-doc/scripts/check_links.py"
SEED = 20261112
FAMILY_COUNT = 4000

# 照合の境目に効く字。空白は Python 3.10 の \s に入る字と、入らない似た字を混ぜる。
# ソースに見えない字を置かないため、ASCII の外の字は符号位置で書く。
SPACES = [chr(c) for c in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x85, 0xA0, 0x1680,
                           0x2000, 0x2005, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000)]
NON_SPACES = [chr(c) for c in (0x180E, 0x200B, 0x2060, 0xFEFF, 0xAD, 0x00, 0x7F, 0xFFFD)]
NIHON, EMOJI, IDEOGRAPHIC_SPACE, ZERO_WIDTH_SPACE, FULLWIDTH_A, E_ACUTE = map(
    chr, (0x65E5, 0x1F600, 0x3000, 0x200B, 0xFF41, 0xE9))
# リンクの [ ] の中・( ) の中に置く字
INNER = ["a", "b", " ", "\n", "[", "(", ")", "`", NIHON, "\\", "!"] + SPACES[:4] + NON_SPACES[:3]
TARGET = (["a", "m.md", "x/y", "#", "#x", "/", ".", "..", "\\", "<", ">", "[", "]", "(", NIHON, EMOJI,
           "http://", "https://", "mailto:", "tel:", "HTTP://", "http:/", "tel", "\"t\"", "a b", "#a b"]
          + SPACES + NON_SPACES)
NOISE = ["x", " ", "\n", "[", "]", "(", ")", "`", "!", IDEOGRAPHIC_SPACE, "\r\n"]
# コード中のパスの字(A: 先頭の部品、B: 最後の部品、C: 拡張子)と、外の字
CODE_A = ["a", "Z", "0", "_", ".", "@", "/", "-", "x/y", "@s", "..", "http", "npm", "pnpm", "yarn", "Yarn"]
CODE_B = ["a", "Z", "9", "_", ".", "-", "b.c", "md"]
CODE_C = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
CODE_BAD = ["`", " ", "@", "/", ".", "-", FULLWIDTH_A, E_ACUTE, "\n", "\t", "$", "+", ":", "~", IDEOGRAPHIC_SPACE,
            "[", ")"]
FIXED = [
    "", "[x](m.md)", "[x](m.md \"t\")", "[x](a(b).md)", "[i](j)k)", "[x]()", "[[x]](y)", "[a[b](c)",
    "[a\nb](c)", "[x](a" + IDEOGRAPHIC_SPACE + "b)", "[x](a" + ZERO_WIDTH_SPACE + "b)", "[x](http://a)",
    "[x](HTTP://a)", "[x](#a)", "[x](a#b)", "[x](  )", "[x](a)(b)", "[x](a)[y](b)", "[x]\n(a)", "](a)", "[x](a",
    "[x](" + chr(0x85) + "a)", "[x](a" + chr(0x1C) + "b)",
    "`a/b.c`", "`a/b.abcdefghij`", "`a/b.abcdefghijk`", "`@s/p.json`", "`a/@b.md`", "`a@b/c.md`", "`./a.md`",
    "`a/.b`", "`a/b.`", "`a/b.c.d`", "`a/b.c-d`", "`/a/b.md`", "`a//b.md`", "`x`a/b.md`", "`a b` c/d.md`",
    "``a/b.md``", "`a/b.md", "`http/x.md`", "`npmx/a.b`", "`a/b.md` `c/d.md`", "`a/b.md``c/d.md`",
    "`a/b/c.d/e.f`", "`-/-.-`", "`_/_._`", "`./..`", "`/.a`", "`//a.b`", "`" + FULLWIDTH_A + "/b.md`",
]


def load_legacy():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("r4u_legacy_check_links", ROOT / LEGACY_REL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ranges(predicate):
    result = []
    start = None
    for code in range(0x110000):
        hit = predicate(chr(code))
        if hit and start is None:
            start = code
        elif not hit and start is not None:
            result.append([start, code - 1])
            start = None
    if start is not None:
        result.append([start, 0x10FFFF])
    return result


def pick(rng, tokens, low, high):
    return "".join(rng.choice(tokens) for _ in range(rng.randint(low, high)))


def mutate(rng, text, bad):
    """0〜2 回、字を足す・消す・置き換える。境目の外れを作るため。"""
    for _ in range(rng.choice([0, 0, 1, 2])):
        at = rng.randint(0, len(text))
        how = rng.randint(0, 2)
        if how == 0 or not text:
            text = text[:at] + rng.choice(bad) + text[at:]
        elif how == 1:
            at = min(at, len(text) - 1)
            text = text[:at] + text[at + 1:]
        else:
            at = min(at, len(text) - 1)
            text = text[:at] + rng.choice(bad) + text[at + 1:]
    return text


def link_piece(rng):
    piece = "[" + pick(rng, INNER, 0, 4) + "](" + pick(rng, TARGET, 1, 4) + ")"
    return mutate(rng, piece, ["[", "]", "(", ")", " ", "\n", IDEOGRAPHIC_SPACE, "a"])


def code_piece(rng):
    ext = "".join(rng.choice(CODE_C) for _ in range(rng.choice([0, 1, 1, 2, 3, 5, 9, 10, 10, 11, 12])))
    piece = "`" + pick(rng, CODE_A, 1, 3) + "/" + pick(rng, CODE_B, 1, 3) + "." + ext + "`"
    return mutate(rng, piece, CODE_BAD)


def compose(rng, makers):
    parts = []
    for _ in range(rng.randint(1, 4)):
        if parts and rng.random() < 0.7:
            parts.append(pick(rng, NOISE, 0, 2))
        parts.append(rng.choice(makers)(rng))
    return "".join(parts)


def record(legacy, text):
    links = []
    for m in legacy.LINK_RE.finditer(text):
        raw = m.group(1)
        if raw.startswith(("http://", "https://", "mailto:", "#", "tel:")):
            target = None
        else:
            target = legacy.strip_link(raw)
        links.append([m.start(), text.count("\n", 0, m.start()) + 1, raw, target])
    codes = []
    for m in legacy.CODEPATH_RE.finditer(text):
        target = m.group(1)
        skipped = target.startswith(("http", "npm", "pnpm", "yarn"))
        codes.append([m.start(), text.count("\n", 0, m.start()) + 1, target, skipped])
    return {"text": text, "links": links, "codes": codes}


def build():
    if sys.version_info[:2] != (3, 10):
        raise SystemExit("Python 3.10 系で流す(Unicode 13.0.0 の \\s を固定するため)")
    legacy = load_legacy()
    space = re.compile(r"\s")
    rng = random.Random(SEED)
    texts = list(FIXED)
    texts += [compose(rng, [link_piece]) for _ in range(FAMILY_COUNT)]
    texts += [compose(rng, [code_piece]) for _ in range(FAMILY_COUNT)]
    texts += [compose(rng, [link_piece, code_piece]) for _ in range(FAMILY_COUNT)]
    cases = [record(legacy, text) for text in texts]
    head = {
        "schema_version": 1,
        "purpose": "旧版の LINK_RE・CODEPATH_RE・飛ばす接頭辞・strip_link の結果(位置は符号位置の番号、行は LF の数 + 1)",
        "generator": "native/fixtures/r4u/gen_matcher_corpus.py",
        "seed": SEED,
        "unicode": unicodedata.unidata_version,
        "legacy": {"path": LEGACY_REL, "sha256": hashlib.sha256((ROOT / LEGACY_REL).read_bytes()).hexdigest()},
        "patterns": {"link": legacy.LINK_RE.pattern, "codepath": legacy.CODEPATH_RE.pattern},
        "space_ranges": {"re_s": ranges(lambda c: space.match(c) is not None),
                         "str_isspace": ranges(str.isspace)},
        "case_count": len(cases),
        "link_case_count": sum(1 for case in cases if case["links"]),
        "code_case_count": sum(1 for case in cases if case["codes"]),
        "link_match_count": sum(len(case["links"]) for case in cases),
        "code_match_count": sum(len(case["codes"]) for case in cases),
    }
    lines = [json.dumps(head, ensure_ascii=False)[:-1] + ', "cases": [']
    lines += [json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "," for case in cases]
    lines[-1] = lines[-1][:-1]
    lines.append("]}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=HERE / "matcher-corpus.json")
    args = parser.parse_args()
    args.out.write_bytes(build())


if __name__ == "__main__":
    main()
