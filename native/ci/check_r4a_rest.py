#!/usr/bin/env python3
"""R4A 残り 5 本の開発専用の比較器(旧 Python と Rust の専用 example を同じ入力で比べる)。

使い方:
  "$R4A_REST_PYTHON" -B native/ci/check_r4a_rest.py --fixture <binary> [--only <helper>,...]

- `--fixture`: Rust の専用 example の絶対パス(不在・空・実行不能は失敗)。
- `--only`: 比べる補助の部分集合。部分比較は全件合格として扱わない(終了時に明記する)。
- 旧版は自分を動かした Python(`sys.executable`)で動かす。本家と同じ中身の Python 3.10.11 で動かす。
- 必須 case ID の集合は `r4a_rest_fixtures/required_cases.json` が正本。実行した集合と
  完全に一致しなければ失敗にする(欠落・重複・余計な case)。
- 旧版の診断に出る実行環境のパスは、構造(Traceback の Frame の行)で解析して固定名に直す。
  対応づけるのは、case が固定期待を持つ Frame の `File` 欄だけである。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import stat as stat_module
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "r4a_rest_fixtures"
SKILLS = ROOT / "plugins/dev-workflow/skills"

SCRIPTS = {
    "resolve-task-store": SKILLS / "create-task/scripts/resolve-task-store.py",
    "candidate-keys": SKILLS / "create-task/scripts/candidate-keys.py",
    "host-argv": SKILLS / "ship-task/scripts/host-argv.py",
    "loop-task-text": SKILLS / "ship-task/scripts/loop-task-text.py",
    "loop-child-output": SKILLS / "ship-task/scripts/loop-child-output.py",
}
HELD_CODE = {"loop-task-text", "loop-child-output"}

# 機械の事情(locale の有無)で確かめられない case の、OS ごとに固定した一覧。違えば失敗にする。
UNAVAILABLE_BY_OS = {"linux": [], "darwin": []}

# 本家 CPython 3.10.11 の標準ライブラリの SHA-256(設計の固定期待)。
UPSTREAM_SHA256 = {
    "argparse.py": "a238b700513237e10d26056e42c5b4e42d4961ea70e15ebeef4c61c76615f004",
    "pathlib.py": "936f14a3ecc4ac5e896e4d673666d76b25a9d581f3b4c8d8d424e830fda2a69b",
    "json/__init__.py": "d5d41e2c29049515d295d81a6d40b4890fbec8d8482cfb401630f8ef2f77e4d5",
    "json/decoder.py": "9f02654649816145bc76f8c210a5fe3ba1de142d4d97a1c93105732e747c285b",
    "json/scanner.py": "8604d9d03786d0d509abb49e9f069337278ea988c244069ae8ca2c89acc2cb08",
}

# 旧 Python の環境変数の扱い(CPython 3.10.11 の `Python/initconfig.c`・`Python/preconfig.c` などの環境変数の読取りから作って固定する。
# 実行する旧版の実行ファイルの文字列(`PYTHON*`)が、この 3 つの一覧のどれかに入ることを、実行のたびに確かめる)。
# (1) 前提の外にして、両方の子から外す変数(2026-11-11 22:31・2026-11-12 00:28 の決定)。
STRIPPED_ENV = [
    "PYTHONASYNCIODEBUG", "PYTHONBREAKPOINT", "PYTHONCASEOK", "PYTHONDEBUG", "PYTHONDEVMODE", "PYTHONDUMPREFS",
    "PYTHONDUMPREFSFILE", "PYTHONEXECUTABLE", "PYTHONFAULTHANDLER", "PYTHONHOME", "PYTHONINSPECT", "PYTHONMALLOC",
    "PYTHONMALLOCSTATS", "PYTHONPATH", "PYTHONPLATLIBDIR", "PYTHONPROFILEIMPORTTIME", "PYTHONPYCACHEPREFIX",
    "PYTHONSTARTUP", "PYTHONTRACEMALLOC", "PYTHONUSERBASE", "PYTHONVERBOSE", "PYTHONWARNDEFAULTENCODING", "PYTHONWARNINGS",
]
# (2) 旧版どおり再現するので、外さない変数(case が意図して与える)。
KEPT_ENV = ["PYTHONUNBUFFERED", "PYTHONINTMAXSTRDIGITS", "PYTHONOPTIMIZE", "PYTHONHASHSEED", "PYTHONIOENCODING", "PYTHONUTF8",
            "PYTHONCOERCECLOCALE", "LC_ALL", "LC_CTYPE", "LANG"]
# (3) 比較器自身が決める変数。
COMPARER_ENV = ["PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"]

CHILD_TIMEOUT = int(os.environ.get("R4A_REST_CHILD_TIMEOUT", "15"))  # 1 子の上限(秒)。自己検査だけが縮める
TOTAL_TIMEOUT = 900


class Failure(Exception):
    pass


# ---------------------------------------------------------------- case の定義


class Case:
    def __init__(self, case_id, helper, argv, *, env=None, stdin=b"", stdout=None, stderr=None,
                 frames=None, setup=None, binary_argv=None, thread=False, build=None, judge=None, cwd=None,
                 fault=None, charset=None, pty=None, signal=None, pending=None, preload=None, bytecode=False, race=None, fifo=None):
        self.id = case_id
        self.helper = helper
        self.argv = list(argv)
        self.env = dict(env or {})
        self.stdin = stdin
        self.stdout = stdout  # None=pipe / "full"=/dev/full
        self.stderr = stderr
        # 固定期待のフレーム: 旧版の標準エラーの Traceback ごとの [(固定名, 行, 関数)] の列。
        self.frames = frames
        self.setup = setup
        self.binary_argv = binary_argv
        # True: Python 起動失敗の診断。`Current thread` の値だけを位置と形式で検査する(設計の承認差)。
        self.thread = thread
        # 旧テストの表を再利用する case: build(tmp) -> (argv, stdin)・judge(結果) で固定期待を照らす。
        self.build = build
        self.judge = judge
        self.cwd = cwd
        # "<fd>:<mode>:<n>": 書込みの故障の規則(budget・cut-first・short-fail)。fault_shim.c と Rust の差替えが同じ規則を当てる。
        self.fault = fault
        # 文字コード設定の case(承認差): Rust は設定を読まず通常の結果(設定なしの旧版と同じ)を返す。
        # 設定ありの旧版は、記録した固定期待(charset_expectations.json)と一致する。
        self.charset = charset
        # (行, 列, "stdout"|"stderr"|"both"): 疑似端末で動かす(出力は CRLF になる)。端末幅は疑似端末の大きさ。
        self.pty = pty
        # ("INT"|"TERM", 先に送る標準入力): 標準入力の読取りの途中で信号を送る。
        # 旧い 2 要素の形も受ける。正規形は (信号名, 先に送る入力, 待ち方, 無視を受け継ぐか, 信号の後に足す入力)。
        if signal is not None and len(signal) == 2:
            signal = (signal[0], signal[1], "stdin", False, b"")
        self.signal = signal
        self.fifo_name = None  # fifo の待ち方の case の FIFO の名前(作業フォルダの中)
        # 接続待ち: Rust 側が platform と接続するまで合格しない case。一致しなくても失敗に数えず、件数を別に報告する。
        self.pending = pending
        # 旧版だけに差込みライブラリを当てる環境変数(例: {"R4A_CLOSE_FAIL": "5"})。Rust 側には変数だけ渡す。
        self.preload = preload
        # True: 旧版を -B なしで動かす(旧スクリプト自身が __pycache__ を作らないこと(旧テストの観点)を確かめる)。
        self.bytecode = bytecode
        # (build, 期待する標準出力の部分文字列): permlog の競合。旧版と Rust に、同じ内容の新しい作業フォルダをそれぞれ作って流す。
        self.race = race
        # (パス, 内容): FIFO を入力にする case。読取りが始まったら書く(合図で同期。sleep の偶然に頼らない)。
        self.fifo = fifo


def needs_shim(case):
    return case.fault is not None or bool(case.preload) or case.race is not None


def needs_linux(case):
    """Linux でしか確かめられない case: `/dev/full`(Mac に無い)と、待ちの状態を確かめる信号の case(/proc の wchan)。"""
    return case.signal is not None or case.stdout == "full" or case.stderr == "full"


def b(text):
    return os.fsencode(text)


STORE = "resolve-task-store"
HOST = "host-argv"

STORE_MODULE = [("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 28, "main")]
STORE_PARSE = [("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 26, "main")]


LOADS_TYPE = [STORE_MODULE + [("json/__init__.py", 339, "loads")]]
LOADS_RECURSION = [STORE_MODULE + [("json/__init__.py", 346, "loads"), ("json/decoder.py", 337, "decode"),
                                   ("json/decoder.py", 353, "raw_decode")]]


def store_cases():
    cases = []

    def add(case_id, argv, **kw):
        cases.append(Case(f"store/{case_id}", STORE, argv, **kw))

    values = {
        "omitted": None, "empty": "", "null": "null", "file": '"file"', "issue": '"github-issue"',
        "other-string": '"x"', "empty-json-string": '""', "int": "1", "true": "true", "list": "[]",
        "object": "{}", "float": "1.5", "nan": "NaN", "inf": "-Infinity", "huge-exp": "1e999999999999",
        "lone-surrogate": '"\\ud800"', "paired-surrogate": '"\\ud800\\udc00"', "bad-text": "bad",
        "space": " ", "dup-keys": '{"a":1,"a":2}', "trailing": "1 2", "bom": "﻿1",
        "unterminated": '"a', "control": '"a\tb"', "bad-escape": '"\\x"', "bad-unicode": '"\\u12"',
        "expecting-comma": "[1 2]", "expecting-name": "{1:2}", "expecting-colon": '{"a" 1}',
        "leading-zero": "01", "minus-only": "-", "exp-partial": "1e", "nested-null": "[null]",
        "file-with-space": ' "file" ', "newline": '"file"\n', "unicode-string": '"ファイル"',
    }
    for name, value in values.items():
        if value is None:
            add("value-omitted", [])
        else:
            add(f"value-sep-{name}", ["--value-json", value])
            add(f"value-eq-{name}", ["--value-json=" + value])
    add("value-abbrev-val", ["--val", '"file"'])
    add("value-abbrev-v", ["--v", '"file"'])
    add("value-abbrev-eq", ["--value=" + '"github-issue"'])
    add("value-repeat-last-wins", ["--value-json", '"file"', "--value-json", '"github-issue"'])
    add("value-repeat-first-bad", ["--value-json", "x", "--value-json", '"file"'])
    # 整数の桁・深さの境界。
    for digits in (640, 4300, 4301, 5000):
        add(f"int-digits-{digits}", ["--value-json", "9" * digits])
    for depth in (993, 994, 995):
        recursion = LOADS_RECURSION if depth == 995 else None
        add(f"deep-array-{depth}", ["--value-json", "[" * depth + "]" * depth], frames=recursion)
        add(f"deep-object-{depth}", ["--value-json", '{"a":' * depth + "1" + "}" * depth], frames=recursion)
    add("deep-array-994-nan", ["--value-json", "[" * 994 + "NaN" + "]" * 994], frames=LOADS_RECURSION)
    add("deep-array-995-unclosed", ["--value-json", "[" * 995], frames=LOADS_RECURSION)
    add("int-limit-env-640-ok", ["--value-json", "9" * 640], env={"PYTHONINTMAXSTRDIGITS": "640"})
    add("int-limit-env-640-over", ["--value-json", "9" * 641], env={"PYTHONINTMAXSTRDIGITS": "640"})
    add("int-limit-env-0", ["--value-json", "9" * 5000], env={"PYTHONINTMAXSTRDIGITS": "0"})
    for bad in ("639", "abc", "640 ", "-1", "2147483648"):
        add(f"int-limit-env-invalid-{bad.strip() or 'blank'}", [], env={"PYTHONINTMAXSTRDIGITS": bad})
    add("int-limit-env-empty", [], env={"PYTHONINTMAXSTRDIGITS": ""})
    add("int-limit-env-padded", ["--value-json", "9" * 641], env={"PYTHONINTMAXSTRDIGITS": " +0700"})
    # 引数の境界(argparse)。
    shapes = {
        "help-long": ["--help"], "help-short": ["-h"], "help-abbrev": ["--he"], "help-hh": ["-hh"],
        "help-hhh": ["-hhh"], "help-hbad": ["-hbad"], "help-hhbad": ["-hhbad"], "help-eq": ["--help=x"],
        "help-eq-empty": ["--help="], "ambiguous-eq": ["--=x"],
        "double-dash": ["--"], "double-dash-value": ["--", "x"], "double-dash-then-option": ["--", "--value-json", "x"],
        "value-missing": ["--value-json"], "value-then-option": ["--value-json", "--help"],
        "value-then-dd": ["--value-json", "--"],
        "help-before-missing": ["--help", "--value-json"], "unknown-then-help": ["--bad", "--help"],
        "unknown-then-missing": ["--bad", "--value-json"], "unknown-long": ["--bad"],
        "unknown-short": ["-x"], "unknown-eq": ["--bad=1"], "positional": ["a"], "two-positional": ["a", "b"],
        "negative-number": ["-1"], "negative-float": ["-.5"], "negative-unicode": ["-１２"],
        "value-negative": ["--value-json", "-1"], "value-negative-unicode": ["--value-json", "-１２"],
        "value-space": ["--value-json", "-1 x"], "value-tab": ["--value-json", "-1\tx"],
        "value-dash-text": ["--value-json", "-x"], "single-dash": ["-"], "value-single-dash": ["--value-json", "-"],
        "extra-after-value": ["--value-json", '"file"', "extra"],
        "empty-arg": [""], "value-empty-arg": ["--value-json", ""],
        "help-and-extra": ["--help", "extra"], "short-value": ["-v", "x"], "short-value-attached": ["-vx"],
        "eq-in-value": ["--value-json=a=b"],
    }
    for name, argv in shapes.items():
        add(f"args-{name}", argv)
    add("args-value-eq-dd", ["--value-json=--"], frames=LOADS_TYPE)
    cases.append(Case("store/args-non-utf8-value", STORE, [], binary_argv=[b"--value-json=\xff"]))
    cases.append(Case("store/args-non-utf8-extra", STORE, [], binary_argv=[b"--bad\xff"]))
    cases.append(Case("store/args-non-utf8-quoted", STORE, [], binary_argv=[b"-h\xfe"]))
    cases.append(Case("store/args-non-utf8-json-string", STORE, [], binary_argv=[b"--value-json", b'"\xff"']))
    # 端末幅。
    for width in ("1", "10", "20", "30", "40", "60", "80", "100", "0", "-5", " 30 ", "３０", "1_0", "abc", "",
                  "+25", "99999999999999999999"):
        label = {"": "empty", " 30 ": "padded", "３０": "fullwidth", "-5": "negative", "+25": "plus"}.get(width, width)
        add(f"width-help-{label}", ["--help"], env={"COLUMNS": width})
        add(f"width-usage-{label}", ["--bad"], env={"COLUMNS": width})
    add("width-lines-only", ["--help"], env={"LINES": "10"})
    add("width-columns-lines", ["--help"], env={"COLUMNS": "25", "LINES": "3"})
    add("width-columns-bad-lines-zero", ["--help"], env={"COLUMNS": "25", "LINES": "0"})
    # 出力の方式と故障。
    add("unbuffered-success", [], env={"PYTHONUNBUFFERED": "1"})
    add("unbuffered-zero", [], env={"PYTHONUNBUFFERED": "0"})
    add("unbuffered-help", ["--help"], env={"PYTHONUNBUFFERED": "1"})
    # Python 起動失敗の診断(directory の標準入出力)。`Current thread` は位置と形式だけを検査する。
    add("startup-stdin-dir", [], stdin="dir", thread=True)
    add("startup-stdin-dir-help", ["--help"], stdin="dir", thread=True)
    add("startup-stdout-dir", [], stdout="dir", thread=True)
    add("startup-stderr-dir", [], stderr="dir", thread=True)
    # PYTHONUNBUFFERED の字面(空白・符号・ゼロ列・不正値)と PYTHONINTMAXSTRDIGITS の字面。
    for label, value in {"empty": "", "zero": "0", "one": "1", "two": "2", "space-one": " 1", "plus-one": "+1", "minus-zero": "-0",
                         "double-zero": "00", "text": "x", "true": "true", "space-only": " ", "plus": "+", "tab-zero": "\t0"}.items():
        add(f"env-unbuffered-{label}-success", [], env={"PYTHONUNBUFFERED": value}, stdout="full",
            frames=STORE_SUCCESS_FRAMES if value.strip("\t") not in ("", "0", "-0", "00") else None)
        add(f"env-unbuffered-{label}-error", ["--bad"], env={"PYTHONUNBUFFERED": value})
    for label, value in {"640": "640", "641": "641", "0": "0", "big": "5000", "space-641": " 641", "plus-700": "+700",
                         "zero-padded": "00700", "639": "639", "neg": "-5", "text": "abc", "exp": "1e3", "hex": "0x400",
                         "underscore": "1_000", "empty": "", "trailing-space": "700 ", "huge": "99999999999", "int-max": "2147483647",
                         "over": "2147483648"}.items():
        add(f"env-intmax-{label}-640digits", ["--value-json", "9" * 640], env={"PYTHONINTMAXSTRDIGITS": value})
        add(f"env-intmax-{label}-5000digits", ["--value-json", "9" * 5000], env={"PYTHONINTMAXSTRDIGITS": value})
        add(f"env-intmax-{label}-1000digits-help", ["--help"], env={"PYTHONINTMAXSTRDIGITS": value, "COLUMNS": "9" * 1000})
    add("startup-int-limit-wins-over-stdin-dir", [], stdin="dir", env={"PYTHONINTMAXSTRDIGITS": "639"})
    add("stdout-full-success", [], stdout="full")
    add("stdout-full-help", ["--help"], stdout="full")
    add("stdout-full-unbuffered-success", [], stdout="full", env={"PYTHONUNBUFFERED": "1"},
        frames=[[("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 36, "main")]])
    add("stdout-full-unbuffered-help", ["--help"], stdout="full", env={"PYTHONUNBUFFERED": "1"},
        frames=[[("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 26, "main"),
                 ("argparse.py", 1833, "parse_args"), ("argparse.py", 1866, "parse_known_args"),
                 ("argparse.py", 2079, "_parse_known_args"), ("argparse.py", 2019, "consume_optional"),
                 ("argparse.py", 1943, "take_action"), ("argparse.py", 1106, "__call__"),
                 ("argparse.py", 2567, "print_help"), ("argparse.py", 2573, "_print_message")]])
    add("stderr-full-error", ["--bad"], stderr="full")
    add("stderr-full-unbuffered-error", ["--bad"], stderr="full", env={"PYTHONUNBUFFERED": "1"})
    add("stderr-full-bad-json", ["--value-json=x"], stderr="full")
    add("both-full-unbuffered", ["--value-json=x"], stdout="full", stderr="full", env={"PYTHONUNBUFFERED": "1"})
    # 未処理の例外(旧版の標準ライブラリのフレームを含む)。
    add("trace-value-eq-dd", ["--value-json=--"],
        frames=[STORE_MODULE + [("json/__init__.py", 339, "loads")]])
    add("trace-deep-array-995", ["--value-json", "[" * 995 + "]" * 995],
        frames=[STORE_MODULE + [("json/__init__.py", 346, "loads"), ("json/decoder.py", 337, "decode"),
                                ("json/decoder.py", 353, "raw_decode")]])
    add("trace-deep-object-995", ["--value-json", '{"a":' * 995 + "1" + "}" * 995],
        frames=[STORE_MODULE + [("json/__init__.py", 346, "loads"), ("json/decoder.py", 337, "decode"),
                                ("json/decoder.py", 353, "raw_decode")]])
    # 利用者の入力に偽の File 行を含む(置き換えてはならない)。
    add("fake-file-line", ["--bad", '\n  File "/x/y.py", line 1, in z\n'])
    add("fake-file-line-json", ["--value-json", '\n  File "/x/y.py", line 1, in z\n'])
    return cases


def host_cases():
    cases = []

    def add(case_id, argv, **kw):
        cases.append(Case(f"host/{case_id}", HOST, argv, **kw))

    tables = {
        "empty": [], "dashdash": ["--"], "dashdash-only-exe": ["--", "run"], "exe-only": ["run"],
        "exe-empty": [""], "exe-dash": ["-x"], "exe-dashdash-dashdash": ["--", "--"],
        "verbose": ["run", "--verbose"], "verbose-twice": ["run", "--verbose", "--verbose"],
        "verbose-eq": ["run", "--verbose=1"], "verbose-eq-empty": ["run", "--verbose="],
        "model-ok": ["run", "--model=claude-x:1.2/y_z"], "model-bad-lead": ["run", "--model=-x"],
        "model-empty": ["run", "--model="], "model-space": ["run", "--model=a b"], "model-sep": ["run", "--model", "x"],
        "model-dup": ["run", "--model=a", "--model=b"], "name-ok": ["run", "--name=Abc.d"],
        "name-bad": ["run", "--name=a;b"], "effort-low": ["run", "--effort=low"], "effort-max": ["run", "--effort=max"],
        "effort-bad": ["run", "--effort=Low"], "effort-xhigh": ["run", "--effort=xhigh"],
        "turns-ok": ["run", "--max-turns=12"], "turns-zero": ["run", "--max-turns=0"],
        "turns-lead-zero": ["run", "--max-turns=01"], "turns-neg": ["run", "--max-turns=-1"],
        "turns-unicode": ["run", "--max-turns=１２"], "turns-newline": ["run", "--max-turns=1\n"],
        "budget-int": ["run", "--max-budget-usd=5"], "budget-zero": ["run", "--max-budget-usd=0"],
        "budget-zero-frac": ["run", "--max-budget-usd=0.5"], "budget-lead-zero": ["run", "--max-budget-usd=00.5"],
        "budget-no-int": ["run", "--max-budget-usd=.5"], "budget-trailing-dot": ["run", "--max-budget-usd=1."],
        "budget-two-dots": ["run", "--max-budget-usd=1.2.3"], "budget-exp": ["run", "--max-budget-usd=1e3"],
        "unknown": ["run", "--other=1"], "positional": ["run", "extra"], "double-dash-later": ["run", "--", "x"],
        "all-ok": ["run", "--verbose", "--model=m", "--name=n", "--effort=high", "--max-budget-usd=1.5", "--max-turns=3"],
        "dup-after-ok": ["run", "--name=a", "--effort=low", "--name=a"],
        "bad-before-dup": ["run", "--other=1", "--other=1"],
        "dashdash-then-dash-exe": ["--", "-x"], "secret-not-echoed": ["run", "--model", "SECRET-VALUE"],
        "eq-only": ["run", "="], "name-only-dashes": ["run", "--=x"],
    }
    for name, argv in tables.items():
        add(f"args-{name}", argv)
    cases.append(Case("host/args-non-utf8-exe", HOST, [], binary_argv=[b"\xff"]))
    cases.append(Case("host/args-non-utf8-value", HOST, [], binary_argv=[b"run", b"--model=\xff"]))
    add("startup-stdin-dir", ["run"], stdin="dir", thread=True)
    add("startup-stdout-dir", ["-x"], stdout="dir", thread=True)
    add("startup-stderr-dir", ["-x"], stderr="dir", thread=True)
    add("unbuffered-error", ["-x"], env={"PYTHONUNBUFFERED": "1"})
    add("unbuffered-ok", ["run"], env={"PYTHONUNBUFFERED": "1"})
    add("stderr-full-error", ["-x"], stderr="full")
    add("stderr-full-unbuffered-error", ["-x"], stderr="full", env={"PYTHONUNBUFFERED": "1"})
    add("stdout-full-ok", ["run"], stdout="full")
    for value in ("640", "639"):
        add(f"int-limit-env-{value}", ["run"], env={"PYTHONINTMAXSTRDIGITS": value})
    return cases


TASK = "loop-task-text"


def make_task_inputs(directory):
    """loop-task-text の入力ファイル(以前の試作の B1 と同じ内容)を作る。"""
    directory = Path(directory)
    (directory / "normal.md").write_bytes(
        "> **無人実行**: 可\r\n> **作成日**: 2026-02-30\r\n## 追加修正記録\r\n"
        "- **保留**(ship-task・2026-01-02): G2\r\n".encode())
    (directory / "invalid.md").write_bytes(b"\xef\xbb\xbf\xef\xbb\xbf> **\xe4\xbd\x9c\xe6\x88\x90\xe6\x97\xa5**: \xff2026-01-02\rX")
    (directory / "fenced.md").write_text("```\n> **無人実行**: 可\n```\n    ```\n## 追加修正記録\n", encoding="utf-8")
    (directory / "unicode-date.md").write_text("> **作成日**: ２０２６-01-02\n", encoding="utf-8")
    (directory / "records.md").write_text(
        "## 追加修正記録\n- **保留**(ship-task・2026-01-02): S1\n## x\n## 追加修正記録\n"
        "- **保留**(ship-task・2026-01-02): D7junk\n", encoding="utf-8")
    with open(os.fsencode(directory) + b"/bad-\xff", "wb") as handle:
        handle.write((directory / "normal.md").read_bytes())


def task_cases():
    cases = []
    commands = ["taskinfo", "holdcount", "holdcode"]
    files = {"normal": "normal.md", "bom-cr-invalid-utf8": "invalid.md", "fences": "fenced.md",
             "unicode-date": "unicode-date.md", "records": "records.md"}
    first = True
    for label, name in files.items():
        for command in commands:
            case = Case(f"task/{label}-{command}", TASK, [command, name])
            if first:
                case.setup = make_task_inputs
                first = False
            cases.append(case)
    normal = (Path(__file__).parent / "r4a_rest_fixtures" / "loop_task_text_normal.md").read_bytes()
    for command in commands:
        cases.append(Case(f"task/stdin-{command}", TASK, [command, "-"], stdin=normal))
    cases += [
        Case("task/missing-command", TASK, []),
        Case("task/unknown-command", TASK, ["unknown"]),
        Case("task/missing-arguments", TASK, ["taskinfo"]),
        Case("task/extra-arguments", TASK, ["taskinfo", "normal.md", "extra"]),
        Case("task/not-found", TASK, ["taskinfo", "missing"]),
        Case("task/directory", TASK, ["taskinfo", "."]),
        Case("task/non-utf8-path", TASK, [], binary_argv=[b"taskinfo", b"bad-\xff"]),
    ]
    return cases


CAND = "candidate-keys"
CHILD = "loop-child-output"

CAND_MODULE = ("candidate-keys.py", 232, "<module>")
PATHLIB_NEW_FRAMES = [("pathlib.py", 960, "__new__"), ("pathlib.py", 594, "_from_parts"),
                      ("pathlib.py", 578, "_parse_args")]

HEADER_OK = "> **発見元**: data-audit\n> **指摘キー**: キー1\n## 本文\n"


def write(directory, name, data):
    path = Path(os.fsdecode(directory)) / name if isinstance(name, str) else None
    target = os.fsencode(directory) + b"/" + (os.fsencode(name) if isinstance(name, str) else name)
    with open(target, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode())
    return path


def make_candidate_inputs(directory):
    """candidate-keys の入力(保存先・候補の本文)を作る。"""
    base = Path(directory)
    store = base / "store"
    store.mkdir(exist_ok=True)
    files = {
        "進行中_a.md": HEADER_OK + "- [ ] x\n",
        "完了_b.md": "> **指摘キー**: b  b\u00a0\u3000c\n> **見送り**: 2026-01-02 — 理由\n",
        "中断_c.md": "> **指摘キー**: e\u0301\n> **指摘キー**: \u00e9\n> **指摘キー**:   \n",
        "保留_d.md": "> **見送り**: bad\n> **見送り**: 2026-01-02 - x\n> **指摘キー**: d\n",
        "候補_e.md": "\ufeff> **発見元**: refactor\r\n> **指摘キー**: e\r\n## x\r\n> **指摘キー**: late\r\n",
        "候補_fenced.md": "```\n> **指摘キー**: hidden\n```\n> **指摘キー**: shown\n## h\n",
        "候補_unclosed.md": "```\n> **指摘キー**: hidden\n",
        "候補_.md": "> **指摘キー**: empty-name\n",
        "その他_x.md": "> **指摘キー**: ignored\n",
        "候補_x.txt": "> **指摘キー**: ignored\n",
        "候補_same.md": "> **指摘キー**: s\n",
        "完了_same.md": "> **指摘キー**: s2\n",
    }
    for name, body in files.items():
        write(store, name, body)
    write(store, "候補_invalid-utf8.md", b"> **\xe6\x8c\x87\xe6\x91\x98\xe3\x82\xad\xe3\x83\xbc**: \xff\n")
    write(store, "候補_truncated-utf8.md", b"> x\n\xe2\x82")
    write(store, b"\xe5\x80\x99\xe8\xa3\x9c_\xff.md", "> **指摘キー**: raw-name\n")
    (store / "候補_dir.md").mkdir(exist_ok=True)
    for link, target in (("候補_link.md", "進行中_a.md"), ("候補_broken.md", "missing")):
        if not os.path.lexists(store / link):
            os.symlink(target, store / link)
    locked = store / "候補_locked.md"
    write(store, "候補_locked.md", "> **指摘キー**: locked\n")
    locked.chmod(0)
    (base / "empty").mkdir(exist_ok=True)
    (base / "locked").mkdir(exist_ok=True)
    (base / "locked" / "x").mkdir(exist_ok=True)
    (base / "locked").chmod(0)
    if not os.path.lexists(base / "dirlink"):
        os.symlink("store", base / "dirlink")
    if not os.path.lexists(base / "brokendir"):
        os.symlink("nowhere", base / "brokendir")
    write(base, "plain.txt", "x\n")
    # --check の入力。
    checks = {
        "ok": HEADER_OK,
        "ok-source-text": "> **発見元**: refactor(補足)\n> **指摘キー**: k\n",
        "ok-source-space": "> **発見元**: reflect-decisions 補足\n> **指摘キー**: k\n",
        "source-prefix-only": "> **発見元**: refactoring\n> **指摘キー**: k\n",
        "no-source": "> **指摘キー**: k\n",
        "two-sources": "> **発見元**: refactor\n> **発見元**: refactor\n> **指摘キー**: k\n",
        "bad-source": "> **発見元**: nope\n> **指摘キー**: k\n",
        "no-key": "> **発見元**: refactor\n",
        "empty-key": "> **発見元**: refactor\n> **指摘キー**:  \n",
        "two-keys": "> **発見元**: refactor\n> **指摘キー**: a\n> **指摘キー**: b\n",
        "same-keys": "> **発見元**: refactor\n> **指摘キー**: a\n> **指摘キー**:  a \n",
        "nfc-keys": "> **発見元**: refactor\n> **指摘キー**: e\u0301\n",
        "hangul-key": "> **発見元**: refactor\n> **指摘キー**: \u1100\u1161\u11a8\n",
        "unattended": HEADER_OK.replace("## 本文", "> **無人実行**: 可\n## 本文"),
        "skipped": HEADER_OK.replace("## 本文", "> **見送り**: 2026-01-02 — 理由\n## 本文"),
        "skipped-bad": HEADER_OK.replace("## 本文", "> **見送り**: 2026-1-2 — 理由\n## 本文"),
        "skipped-short": HEADER_OK.replace("## 本文", "> **見送り**: 2026-01-02 — \n## 本文"),
        "record": HEADER_OK + "## 追加修正記録\n",
        "record-fenced": HEADER_OK + "```\n## 追加修正記録\n```\n",
        "checkbox": HEADER_OK + "  - [x] a\n\x1c- [ ] b\n```\n- [X] c\n```\n",
        "checkbox-bad": HEADER_OK + "- [y] a\n-[ ] b\n",
        "header-after-h2": "## x\n> **発見元**: refactor\n> **指摘キー**: k\n",
        "header-in-fence": "```\n> **発見元**: refactor\n> **指摘キー**: k\n```\n",
        "tilde-fence": "~~~~\n## x\n~~~\n> **発見元**: refactor\n> **指摘キー**: k\n~~~~\n",
        "indented-fence": "    ```\n> **発見元**: refactor\n> **指摘キー**: k\n",
        "crlf-cr": "> **発見元**: refactor\r> **指摘キー**: k\r\n",
        "bom-twice": "\ufeff\ufeff> **発見元**: refactor\n> **指摘キー**: k\n",
        "trailing-space": "> **発見元**: refactor \t\n> **指摘キー**: k \n",
        "empty": "",
        "many-problems": "> **発見元**: x\n> **指摘キー**: a\n> **指摘キー**: b\n> **無人実行**: 可\n> **見送り**: 2026-01-02 — x\n> **見送り**: bad\n## 追加修正記録\n- [ ] x\n",
    }
    for name, body in checks.items():
        write(base, f"check-{name}.md", body)
    write(base, "check-invalid-utf8.md", b"> **\xff\n")
    write(base, "check-unexpected-eof.md", b"> **\xe2\x82")
    write(base, "check-cont.md", b"\xe2\x82A")
    write(base, "check-surrogate.md", b"\xed\xa0\x80")
    write(base, "check-start.md", b"ab\xc0\x80")
    write(base, "check-big-key.md", HEADER_OK.replace("キー1", "k" * 70000))
    return sorted(checks)


def candidate_cases():
    cases = []
    first = [True]

    def add(case_id, argv, **kw):
        case = Case(f"candidate/{case_id}", CAND, argv, **kw)
        if first[0]:
            case.setup = make_candidate_inputs
            first[0] = False
        cases.append(case)

    add("collect-store", ["--task-dir", "store"])
    for label, value in {"empty": "empty", "absent": "absent", "plain-file": "plain.txt", "plain-file-slash": "plain.txt/",
                         "dirlink": "dirlink", "brokendir": "brokendir", "locked": "locked", "dot-slash": "./store",
                         "double-slash": "store//", "dot": ".", "nested-dot": "store/./", "dotdot": "store/../store",
                         "abs-double": "//tmp-r4a-absent", "empty-string": "", "slash-only-dots": "./."}.items():
        add(f"collect-{label}", ["--task-dir", value] if label != "locked" else ["--task-dir", value])
    add("collect-locked-nested", ["--task-dir", "locked/x"],
        frames=[[CAND_MODULE, ("candidate-keys.py", 223, "main"), ("pathlib.py", 1290, "exists"), ("pathlib.py", 1097, "stat")]])
    add("collect-eq", ["--task-dir=store"])
    add("collect-abbrev", ["--task-d", "store"])
    add("collect-eq-dd", ["--task-dir=--"],
        frames=[[CAND_MODULE, ("candidate-keys.py", 222, "main")] + PATHLIB_NEW_FRAMES])
    add("collect-non-utf8-dir", [], binary_argv=[b"--task-dir=store\xff"])
    add("collect-unbuffered", ["--task-dir", "store"], env={"PYTHONUNBUFFERED": "1"})
    add("collect-stdout-full", ["--task-dir", "store"], stdout="full")
    # 使い方の拒否。
    for label, argv in {"none": [], "dir-and-path": ["--task-dir", "store", "x"], "dir-and-source": ["--task-dir", "store", "--source", "refactor"],
                        "path-only": ["x"], "source-only": ["--source", "refactor"], "check-only": ["--check"],
                        "check-dir": ["--check", "--task-dir", "store", "x"], "check-bad-source": ["--check", "--source", "nope", "x"],
                        "check-source-dd": ["--check", "--source=--", "check-ok.md"],
                        "check-two-paths": ["--check", "a", "b"], "unknown": ["--bad"], "help": ["--help"], "h": ["-h"],
                        "missing-value": ["--task-dir"], "check-eq": ["--check=1"], "check-eq-empty": ["--check="],
                        "check-abbrev": ["--che", "check-ok.md"], "ambiguous": ["--s", "x"], "dd-path": ["--check", "--", "check-ok.md"],
                        "dd-dd": ["--check", "--", "--"], "dd-only": ["--check", "--"]}.items():
        add(f"args-{label}", argv)
    for name in sorted(make_candidate_inputs_names()):
        add(f"check-file-{name}", ["--check", f"check-{name}.md"])
    for name in ("invalid-utf8", "unexpected-eof", "cont", "surrogate", "start", "big-key"):
        add(f"check-file-{name}", ["--check", f"check-{name}.md"])
    for source in ("data-audit", "refactor", "stack-research", "reflect-decisions"):
        add(f"check-source-{source}", ["--check", "--source", source, "check-ok.md"])
        add(f"check-source-eq-{source}", ["--check", f"--source={source}", "check-ok-source-text.md"])
    add("check-missing-file", ["--check", "check-absent.md"])
    add("check-directory", ["--check", "store"])
    add("check-path-normalized-slash", ["--check", "check-ok.md/"])
    add("check-locked", ["--check", "store/候補_locked.md"])
    add("check-non-utf8-path", [], binary_argv=[b"--check", b"check-\xff.md"])
    for name in ("ok", "no-source", "nfc-keys", "many-problems"):
        body = (Path(__file__).parent / "r4a_rest_fixtures" / f"candidate_{name}.md").read_bytes()
        add(f"check-stdin-{name}", ["--check", "-"], stdin=body)
    add("check-stdin-empty", ["--check", "-"], stdin=b"")
    add("check-stdin-invalid", ["--check", "-"], stdin=b"\xff")
    add("check-stdin-source", ["--check", "--source", "refactor", "-"],
        stdin=(Path(__file__).parent / "r4a_rest_fixtures" / "candidate_ok.md").read_bytes())
    add("check-stdin-dir", ["--check", "-"], stdin="dir", thread=True)
    return cases


def make_candidate_inputs_names():
    return ["ok", "ok-source-text", "ok-source-space", "source-prefix-only", "no-source", "two-sources", "bad-source",
            "no-key", "empty-key", "two-keys", "same-keys", "nfc-keys", "hangul-key", "unattended", "skipped", "skipped-bad",
            "skipped-short", "record", "record-fenced", "checkbox", "checkbox-bad", "header-after-h2", "header-in-fence",
            "tilde-fence", "indented-fence", "crlf-cr", "bom-twice", "trailing-space", "empty", "many-problems"]


def make_child_inputs(directory):
    base = Path(directory) / "child"
    base.mkdir(exist_ok=True)
    result = lambda text, **kw: json.dumps({"type": "result", "result": text, **kw}, ensure_ascii=False)
    outcome = "無人のイテレーションの結果: {} — {}"
    files = {
        "result-pr": result(outcome.format("PR", "ok #1")),
        "result-multi": result("x\n" + outcome.format("保留", "a") + "\r\n" + outcome.format("失敗扱い", "b\tc\x01") + "\ny"),
        "result-all-kinds": "\n".join(outcome.format(k, "d") for k in ("PR", "縮退", "保留", "失敗扱い", "候補なし")),
        "result-no-outcome": result("nothing"),
        "result-bad-outcome": result("無人のイテレーションの結果: 不明 — x"),
        "result-no-sep": result("無人のイテレーションの結果: PR - x"),
        "result-long-detail": result(outcome.format("PR", "あ" * 800)),
        "result-not-str": json.dumps({"result": 5}),
        "result-no-type": json.dumps({"result": outcome.format("PR", "single")}),
        "result-denials-one": json.dumps({"type": "result", "permission_denials": [{"a": 1}]}),
        "result-denials-many": json.dumps([{"type": "result", "permission_denials": [1]}, {"type": "result", "permission_denials": [2, 3]},
                                          {"type": "x", "permission_denials": [9]}]),
        "result-denials-mixed": json.dumps([{"type": "result", "denials": [1]}, {"type": "result", "denials": "s"}, {"type": "result", "x_DENIAL_y": None}]),
        "result-denials-float": '{"type":"result","Denials":[1.5,1e300,-0.0,1e-7,NaN,Infinity,-Infinity,123456789012345678]}',
        "result-denials-long": json.dumps({"type": "result", "denials": ["x" * 3000]}),
        "result-denials-ctrl": json.dumps({"type": "result", "denials": ["a\nb\u0000c\u007f"]}),
        "result-denials-unicode": json.dumps({"type": "result", "denials": ["日本語\u00e9\ud800"]}),
        "result-denials-dup": '{"type":"result","denials":[1],"denials":[2],"a":1}',
        "result-list-empty": "[]",
        "result-list-no-result": json.dumps([{"type": "assistant"}, 5, "x"]),
        "result-list-last": json.dumps([{"type": "result", "result": outcome.format("PR", "first")}, {"type": "result", "result": outcome.format("保留", "last")}]),
        "result-bad-json": "not json",
        "result-empty": "",
        "result-scalar": "5",
        "result-string": '"x"',
        "result-bom": "\ufeff{}",
        "result-nan": "NaN",
        "result-big-int": "9" * 5000,
        "result-deep-993": "[" * 993 + "]" * 993,
        "result-deep-994": "[" * 994 + "]" * 994,
        "result-deep-995": "[" * 995 + "]" * 995,
        "result-deep-denials": '{"type":"result","denials":' + "[" * 990 + "]" * 990 + "}",
        "result-dumps-single-993": '{"type":"result","denials":' + "[" * 993 + "]" * 993 + "}",
        "result-dumps-mixed-992": '[{"type":"result","denials":' + "[" * 992 + "]" * 992 + '},{"type":"result","denials":"s"}]',
        "result-dumps-mixed-991": '[{"type":"result","denials":' + "[" * 991 + "]" * 991 + '},{"type":"result","denials":"s"}]',
        "result-dumps-mixed-990": '[{"type":"result","denials":' + "[" * 990 + "]" * 990 + '},{"type":"result","denials":"s"}]',
        "result-dumps-concat-991": '[{"type":"result","denials":' + "[" * 991 + "]" * 991 + '},{"type":"result","denials":' + "[" * 991 + "]" * 991 + '}]',
        "result-dumps-concat-990": '[{"type":"result","denials":' + "[" * 990 + "]" * 990 + '},{"type":"result","denials":[1]}]',
        "result-dumps-objects-496": '{"type":"result","denials":' + '{"a":' * 496 + "1" + "}" * 496 + "}",
        "result-deep-denials-994": '{"type":"result","denials":' + "[" * 993 + "]" * 993 + "}",
    }
    for name, body in files.items():
        write(base, name + ".json", body)
    write(base, "result-invalid-utf8.json", b'{"type":"result","result":"\xff' + "無人のイテレーションの結果: PR — x".encode() + b'"}')
    # permlog。
    line = lambda **kw: json.dumps(kw, ensure_ascii=False)
    perm = {
        "ok": "\n".join([line(decision="allow", tool_name="Bash"), line(decision="deny", kind="shell", tool_name="Bash", reason="r", subject="s"),
                         line(decision="deny", tool_name=["a", 1.5, None, True], reason={"k": "v"}, subject=3)]) + "\n",
        "empty": "",
        "no-lf": line(decision="allow"),
        "blank-line": line(decision="allow") + "\n\n",
        "bad-json": "{\n",
        "not-dict": "[1]\n",
        "bad-decision": line(decision="maybe") + "\n",
        "decision-int": line(decision=1) + "\n",
        "kind-int": line(decision="deny", kind=5) + "\n",
        "kind-null": line(decision="deny", kind=None) + "\n",
        "kind-long": line(decision="deny", kind="k" * 300, tool_name="t") + "\n",
        "kind-ctrl": line(decision="deny", kind="a\tb") + "\n",
        "kinds-sorted": "".join(line(decision="deny", kind=k) + "\n" for k in ("b", "a", "b", "あ", "A", "a")),
        "partial": line(decision="allow") + "\n" + line(decision="deny", kind="x") + "\n" + "garbage\n" + line(decision="allow") + "\n",
        "many-deny": "".join(line(decision="deny", kind=f"k{n % 7}", tool_name=f"t{n}", reason="r", subject="s") + "\n" for n in range(150)),
        "reason-long": line(decision="deny", kind="x", tool_name="t", reason="r" * 900, subject="s") + "\n",
        "unicode-breaks": line(decision="allow") + "\x0b" + line(decision="deny", kind="u") + "\x1c" + line(decision="allow") + "\u2028\n",
        "cr-lf": line(decision="allow") + "\r\n" + line(decision="allow") + "\r\n",
        "lines-10000": (line(decision="allow") + "\n") * 10000,
        "lines-10001": (line(decision="allow") + "\n") * 10001,
        "big-int": '{"decision":"allow","n":' + "9" * 5000 + "}\n",
        "deep": '{"decision":"allow","d":' + "[" * 100 + "]" * 100 + "}\n",
        "deep-993": '{"decision":"allow","d":' + "[" * 992 + "]" * 992 + "}\n",
        "deep-994": '{"decision":"allow","d":' + "[" * 993 + "]" * 993 + "}\n",
        "deep-repr": '{"decision":"deny","kind":"x","tool_name":' + "[" * 900 + "]" * 900 + "}\n",
        "nested-repr": '{"decision":"deny","kind":"x","tool_name":{"a":[1,"b\'c",2.5,null,{"z":false}]},"reason":"\\"q\\"","subject":[]}\n',
        "dup-keys": '{"decision":"allow","decision":"deny","kind":"d"}\n',
    }
    for name, body in perm.items():
        write(base, f"perm-{name}.log", body)
    big = (line(decision="allow") + "\n").encode()
    exact = (big * (1048576 // len(big) + 1))[:1048576 - 1] + b"\n"
    write(base, "perm-1mib.log", exact)
    write(base, "perm-over-1mib.log", exact + b"\n")
    # 境界の手前(1MiB-1・9999 行・明細 99 件と 100 件ちょうど)。
    def exact(size):
        unit = (line(decision="allow") + "\n").encode()
        count = size // len(unit) - 2
        head = unit * count
        rest = size - len(head)
        pad = rest - len((line(decision="allow", pad="") + "\n").encode())
        return head + (line(decision="allow", pad="p" * pad) + "\n").encode()

    write(base, "perm-1mib-minus1.log", exact(1048575))
    write(base, "perm-1mib-minus2.log", exact(1048574))
    write(base, "perm-9999.log", (line(decision="allow") + "\n") * 9999)
    for count in (99, 100, 101):
        write(base, f"perm-deny-{count}.log",
              "".join(line(decision="deny", kind="k", tool_name=f"t{n}", reason="r", subject="s") + "\n" for n in range(count)))
    write(base, "perm-70000.log", exact(70000))
    write(base, "perm-invalid-utf8.log", b'{"decision":"allow","x":"\xff"}\n')
    write(base, "perm-truncated-utf8.log", b'{"decision":"allow","x":"\xe2\x82"}\n')
    write(base, "perm-bom.log", b"\xef\xbb\xbf" + line(decision="allow").encode() + b"\n")
    if not os.path.lexists(base / "perm-link.log"):
        os.symlink("perm-ok.log", base / "perm-link.log")
    if not os.path.lexists(base / "perm-link-dir"):
        os.symlink(".", base / "perm-link-dir")
    if not os.path.lexists(base / "perm-fifo"):
        os.mkfifo(base / "perm-fifo")
    write(base, "perm-locked.log", line(decision="allow") + "\n")
    (base / "perm-locked.log").chmod(0)
    (base / "perm-dir").mkdir(exist_ok=True)
    # origin-json(標準入力)は fixtures の本文を使う。
    return base


def child_cases():
    cases = legacy_child_cases()
    first = [True]

    def add(case_id, argv, **kw):
        case = Case(f"child/{case_id}", CHILD, argv, **kw)
        if first[0]:
            case.setup = make_child_inputs
            first[0] = False
        cases.append(case)

    base = Path(__file__).parent  # 名前の一覧は setup と同じ決まりで作る
    result_names = ["pr", "multi", "all-kinds", "no-outcome", "bad-outcome", "no-sep", "long-detail", "not-str", "no-type",
                    "denials-one", "denials-many", "denials-mixed", "denials-float", "denials-long", "denials-ctrl",
                    "denials-unicode", "denials-dup", "list-empty", "list-no-result", "list-last", "bad-json", "empty",
                    "scalar", "string", "bom", "nan", "big-int", "deep-993", "deep-994", "deep-995", "deep-denials",
                    "deep-denials-994", "dumps-single-993", "dumps-mixed-992", "dumps-mixed-991", "dumps-mixed-990",
                    "dumps-concat-991", "dumps-concat-990", "dumps-objects-496", "invalid-utf8"]
    for name in result_names:
        add(f"result-{name}", ["result", f"child/result-{name}.json"])
    for label, argv in {"missing-file": ["result", "child/absent.json"], "directory": ["result", "child"],
                        "dash-is-a-file": ["result", "-"], "no-path": ["result"], "extra": ["result", "a", "b"],
                        "empty-path": ["result", ""]}.items():
        add(f"result-args-{label}", argv)
    add("result-non-utf8-path", [], binary_argv=[b"result", b"child/\xff.json"])
    perm_names = ["ok", "empty", "no-lf", "blank-line", "bad-json", "not-dict", "bad-decision", "decision-int", "kind-int",
                  "kind-null", "kind-long", "kind-ctrl", "kinds-sorted", "partial", "many-deny", "reason-long",
                  "unicode-breaks", "cr-lf", "lines-10000", "lines-10001", "big-int", "deep", "deep-993", "deep-994",
                  "deep-repr", "nested-repr", "dup-keys"]
    for name in perm_names:
        add(f"permlog-{name}", ["permlog", f"child/perm-{name}.log"])
    for name in ("1mib", "over-1mib", "invalid-utf8", "truncated-utf8", "bom", "link", "locked"):
        add(f"permlog-file-{name}", ["permlog", f"child/perm-{name}.log"])
    for label, argv in {"missing": ["permlog", "child/absent.log"], "directory": ["permlog", "child/perm-dir"],
                        "fifo": ["permlog", "child/perm-fifo"], "link-dir-parent": ["permlog", "child/perm-link-dir/perm-ok.log"],
                        "dotdot": ["permlog", "child/perm-dir/../perm-ok.log"], "dot": ["permlog", "./child/./perm-ok.log"],
                        "double-slash": ["permlog", "child//perm-ok.log"], "empty": ["permlog", ""], "root": ["permlog", "/"],
                        "root-double": ["permlog", "//child"], "dotdot-root": ["permlog", "/../.."],
                        "no-path": ["permlog"], "extra": ["permlog", "a", "b"], "trailing-slash": ["permlog", "child/perm-ok.log/"],
                        "parent-not-dir": ["permlog", "plain.txt/x"]}.items():
        add(f"permlog-args-{label}", argv)
    add("permlog-non-utf8-path", [], binary_argv=[b"permlog", b"child/\xff.log"])
    origins = {
        "ok": {"origin": True, "same": False, "vcs": True, "reason": "ok", "repo": "owner/name"},
        "null-repo": {"origin": False, "same": False, "vcs": False, "reason": "none", "repo": None},
        "no-repo": {"origin": True, "same": True, "vcs": True},
        "repo-newline": {"origin": True, "same": True, "vcs": True, "reason": "r\nx\t", "repo": "a\nb\r\u2028c"},
        "long-reason": {"origin": True, "same": True, "vcs": True, "reason": "あ" * 400},
        "reason-int": {"origin": True, "same": True, "vcs": True, "reason": 5, "repo": ""},
        "repo-int": {"origin": True, "same": True, "vcs": True, "repo": 5},
        "origin-int": {"origin": 1, "same": True, "vcs": True},
        "missing-key": {"origin": True, "same": True},
        "extra-keys": {"origin": True, "same": True, "vcs": True, "other": [1, 2]},
        "lone-surrogate-repo": {"origin": True, "same": True, "vcs": True, "repo": "a\ud800b"},
        "valid-surrogate-pair": {"origin": True, "same": True, "vcs": True, "repo": "\ud83d\ude00"},
    }
    for name, value in origins.items():
        add(f"origin-{name}", ["origin-json"], stdin=json.dumps(value).encode())
    raw = {"empty": b"", "not-json": b"x", "list": b"[]", "invalid-utf8": b"\xff", "bom": b"\xef\xbb\xbf{}", "scalar": b"1",
           "big-int": ("9" * 5000).encode(), "deep-994": ("[" * 994 + "]" * 994).encode(), "deep-995": ("[" * 995 + "]" * 995).encode(),
           "nan": b"NaN", "dup-bool": b'{"origin":true,"origin":false,"same":true,"vcs":true}',
           "big-repo": b'{"origin":true,"same":true,"vcs":true,"repo":"' + b"r" * 9000 + b'"}',
           "huge-repo": b'{"origin":true,"same":true,"vcs":true,"repo":"' + b"r" * 70000 + b'"}'}
    for name, body in raw.items():
        add(f"origin-raw-{name}", ["origin-json"], stdin=body)
    for label, argv in {"unknown": ["unknown"], "none": [], "extra": ["origin-json", "x"], "dash": ["-"]}.items():
        add(f"args-{label}", argv)
    add("origin-unbuffered", ["origin-json"], stdin=json.dumps(origins["ok"]).encode(), env={"PYTHONUNBUFFERED": "1"})
    add("origin-stdout-full", ["origin-json"], stdin=json.dumps(origins["ok"]).encode(), stdout="full")
    add("origin-stdin-dir", ["origin-json"], stdin="dir", thread=True)
    add("result-int-limit", ["result", "child/result-big-int.json"], env={"PYTHONINTMAXSTRDIGITS": "0"})
    add("result-int-limit-640", ["result", "child/result-big-int.json"], env={"PYTHONINTMAXSTRDIGITS": "640"})
    return cases


def legacy_child_cases():
    """`scripts/test_loop_child_output.py` の固定 CASE 表(BUILD と TABLE)を変えずに再利用する。"""
    import importlib.util
    path = ROOT / "scripts/test_loop_child_output.py"
    spec = importlib.util.spec_from_file_location("legacy_test_loop_child_output", path)
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(module)
    cases = []
    for name, command, builder, mode, expected in module.TABLE:
        def build(tmp, command=command, builder=builder):
            argv, data = module.BUILD[builder](tmp)
            return [command, *argv], data

        def judge(result, name=name, mode=mode, expected=expected):
            rc, out, err = expected
            got = (result[0], result[1].decode("utf-8"), result[2].decode("utf-8"))
            if mode == "全体":
                if got != (rc, out, err):
                    raise Failure(f"固定期待と違う: 期待 {(rc, out, err)!r}・実際 {got!r}")
            elif mode[1] == "stderr":
                if (got[0], got[1]) != (rc, out) or got[2].count("\n") != 1 or not got[2].startswith(err):
                    raise Failure(f"固定期待(stderr の先頭)と違う: {got!r}")
            else:
                got_lines, want_lines = got[1].split("\n"), out.split("\n")
                if (got[0], got[2]) != (rc, err) or len(got_lines) != len(want_lines):
                    raise Failure(f"固定期待と違う: {got!r}")
                for i, (g, w) in enumerate(zip(got_lines, want_lines)):
                    if (i == mode[2] and not g.startswith(w)) or (i != mode[2] and g != w):
                        raise Failure(f"固定期待の行 {i} が違う: {g!r}")

        cases.append(Case(f"child/legacy/{name}", CHILD, [], build=build, judge=judge, cwd="/"))
    return cases


STORE_SUCCESS_FRAMES = [[("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 36, "main")]]
HOST_MODULE = ("host-argv.py", 41, "<module>")


def pty_cases():
    """疑似端末の case(出力は CRLF・端末幅は疑似端末の大きさ)。Rust 側は platform の `terminal_size` で実 tty の端末幅を取り、旧版と同じ結果になることを比べる(接続待ちの印は付けない)。"""
    cases = []
    def add(case_id, helper, argv, pty, **kw):
        cases.append(Case(f"pty/{case_id}", helper, argv, pty=pty, **kw))

    for cols in (80, 100, 60, 30, 10, 1, 0):
        add(f"store-help-stdout-tty-{cols}", STORE, ["--help"], (24, cols, "stdout"))
        add(f"store-usage-stdout-tty-{cols}", STORE, ["--bad"], (24, cols, "stdout"))
        add(f"store-usage-both-tty-{cols}", STORE, ["--bad"], (24, cols, "both"))
        # stderr だけが端末のときは、標準出力(pipe)の幅の問い合わせで 80 に落ちる。
        add(f"store-usage-stderr-tty-{cols}", STORE, ["--bad"], (24, cols, "stderr"))
        add(f"host-error-stderr-tty-{cols}", HOST, ["-x"], (24, cols, "stderr"))
    for columns in ("40", "120", "x"):
        add(f"store-help-tty-columns-{columns}", STORE, ["--help"], (24, 100, "stdout"), env={"COLUMNS": columns})
    add("store-help-tty-lines-only", STORE, ["--help"], (50, 70, "stdout"), env={"LINES": "5"})
    add("store-success-stdout-tty", STORE, [], (24, 80, "stdout"))
    add("store-unbuffered-stdout-tty", STORE, [], (24, 80, "stdout"), env={"PYTHONUNBUFFERED": "1"})
    return cases


def make_signal_fifo(directory):
    path = Path(directory) / "sig.fifo"
    if not os.path.lexists(path):
        os.mkfifo(path)


def signal_cases():
    """標準入力の読取り・FIFO の open・満杯の pipe への write の途中の SIGINT・SIGTERM と、受け継いだ SIG_IGN。"""
    cases = []
    for label, helper, argv in (("candidate", CAND, ["--check", "-"]), ("task", TASK, ["taskinfo", "-"]),
                                ("child", CHILD, ["origin-json"])):
        for name in ("INT", "TERM"):
            for preload in (b"", b"partial"):
                kind = "empty" if not preload else "partial"
                cases.append(Case(f"signal/{label}-{name.lower()}-{kind}", helper, argv, signal=(name, preload)))
    # FIFO を開いて待つ間の SIGINT(書き手がいない)。旧版は KeyboardInterrupt で -2、受け継いだ SIG_IGN なら続く。
    fifo_argv = (("candidate", CAND, ["--check", "sig.fifo"]), ("task", TASK, ["taskinfo", "sig.fifo"]),
                 ("child", CHILD, ["result", "sig.fifo"]))
    for label, helper, argv in fifo_argv:
        case = Case(f"signal/{label}-int-fifo-open", helper, argv, signal=("INT", b"", "fifo", False, b""),
                    setup=make_signal_fifo)
        case.fifo_name = "sig.fifo"
        cases.append(case)
    ignored_tail = {"candidate": b"> **\xe7\x99\xba\xe8\xa6\x8b\xe5\x85\x83**: data-audit\n> **\xe6\x8c\x87\xe6\x91\x98\xe3\x82\xad\xe3\x83\xbc**: \xe3\x82\xad\xe3\x83\xbc1\n## \xe6\x9c\xac\xe6\x96\x87\n",
                    "task": b"> **\xe7\x84\xa1\xe4\xba\xba\xe5\xae\x9f\xe8\xa1\x8c**: \xe5\x8f\xaf\n",
                    "child": b'{"type":"result","result":"ok"}\n'}
    for label, helper, argv in fifo_argv:
        case = Case(f"signal/{label}-int-ignored-fifo-open", helper, argv,
                    signal=("INT", b"", "fifo", True, ignored_tail[label]), setup=make_signal_fifo)
        case.fifo_name = "sig.fifo"
        cases.append(case)
    # 受け継いだ SIG_IGN: 標準入力の読取りの途中の SIGINT は効かず、残りを読んで正常に終わる。
    cases.append(Case("signal/child-int-ignored-stdin-read", CHILD, ["origin-json"],
                      signal=("INT", b'{"origin":true,', "stdin", True, b'"same":true,"vcs":true}')))
    cases.append(Case("signal/candidate-int-ignored-stdin-read", CAND, ["--check", "-"],
                      signal=("INT", b"> **", "stdin", True, ignored_tail["candidate"][4:])))
    # 読み手のいない満杯の pipe へ書く間の SIGINT(両方のバッファの方式)。
    big = ("> **発見元**: data-audit\n> **指摘キー**: " + "k" * 200000 + "\n## 本文\n").encode()
    for label, env in (("buffered", {}), ("unbuffered", {"PYTHONUNBUFFERED": "1"})):
        cases.append(Case(f"signal/candidate-int-full-pipe-{label}", CAND, ["--check", "-"], env=env,
                          signal=("INT", big, "fullpipe", False, b"")))
        cases.append(Case(f"signal/candidate-int-ignored-full-pipe-{label}", CAND, ["--check", "-"], env=env,
                          signal=("INT", big, "fullpipe", True, b"")))
    return cases


def fault_cases():
    """書込みの故障(部分書込み)の case。故障の規則は出来事で決める(最初の書込み・合計の上限)。"""
    cases = []
    unbuffered = {"PYTHONUNBUFFERED": "1"}

    def add(case_id, helper, argv, fault, **kw):
        cases.append(Case(f"fault/{case_id}", helper, argv, fault=fault, **kw))

    # 標準出力が満杯 → 標準エラーに Traceback(File 欄を含む)。標準エラーへの書込みを切る。
    # 合計の上限が、Frame の File 欄の途中・欄の外・旧版だけが切れる範囲に来る位置を並べる。
    for limit in (20, 34, 35, 60, 88, 100, 120, 160, 200, 260, 400, 5000):
        add(f"store-trace-budget-{limit}", STORE, [], f"2:budget:{limit}", stdout="full", env=unbuffered,
            frames=STORE_SUCCESS_FRAMES)
    for limit in (1, 10, 34):
        add(f"store-trace-cut-first-{limit}", STORE, [], f"2:cut-first:{limit}", stdout="full", env=unbuffered,
            frames=STORE_SUCCESS_FRAMES)
        add(f"store-trace-short-fail-{limit}", STORE, [], f"2:short-fail:{limit}", stdout="full", env=unbuffered,
            frames=STORE_SUCCESS_FRAMES)
    # argparse の help の書込みの失敗(8 フレームの Traceback)。
    help_frames = [[("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 26, "main"),
                    ("argparse.py", 1833, "parse_args"), ("argparse.py", 1866, "parse_known_args"),
                    ("argparse.py", 2079, "_parse_known_args"), ("argparse.py", 2019, "consume_optional"),
                    ("argparse.py", 1943, "take_action"), ("argparse.py", 1106, "__call__"),
                    ("argparse.py", 2567, "print_help"), ("argparse.py", 2573, "_print_message")]]
    for limit in (60, 300, 700):
        add(f"store-help-trace-budget-{limit}", STORE, ["--help"], f"2:budget:{limit}", stdout="full", env=unbuffered,
            frames=help_frames)
    # 通常のバッファ(短い書込みの残りを送り直す)。標準出力が満杯でも Traceback は出ず、終了時の診断だけ。
    for mode, limit in (("cut-first", 10), ("short-fail", 10), ("budget", 50), ("budget", 5)):
        add(f"store-buffered-{mode}-{limit}", STORE, [], f"2:{mode}:{limit}", stdout="full")
    # 標準エラーの診断(パスを含まない)への故障。
    for mode, limit in (("budget", 30), ("cut-first", 20), ("short-fail", 20), ("budget", 5000)):
        add(f"store-usage-{mode}-{limit}", STORE, ["--bad"], f"2:{mode}:{limit}")
        add(f"store-usage-unbuffered-{mode}-{limit}", STORE, ["--bad"], f"2:{mode}:{limit}", env=unbuffered)
        add(f"host-error-{mode}-{limit}", HOST, ["-x"], f"2:{mode}:{limit}")
        add(f"host-error-unbuffered-{mode}-{limit}", HOST, ["-x"], f"2:{mode}:{limit}", env=unbuffered)
    # 標準出力への故障(標準エラーの Traceback は通常どおり対応づける)。
    for mode, limit in (("budget", 5), ("cut-first", 5), ("short-fail", 5), ("budget", 5000)):
        add(f"store-stdout-{mode}-{limit}", STORE, [], f"1:{mode}:{limit}")
        add(f"store-stdout-unbuffered-{mode}-{limit}", STORE, [], f"1:{mode}:{limit}", env=unbuffered,
            frames=STORE_SUCCESS_FRAMES if mode != "cut-first" and limit < 22 else None)
    add("store-help-stdout-budget-50", STORE, ["--help"], "1:budget:50", env=unbuffered)
    add("host-stdout-ok", HOST, ["run"], "1:budget:5")
    # 標準入力から読む 3 本(ファイルを作らない)。標準出力の長い 1 行(LF を含む)への故障。
    big_origin = json.dumps({"origin": True, "same": True, "vcs": True, "reason": "ok", "repo": "r\nx" * 3000}).encode()
    big_key = ("> **発見元**: refactor\n> **指摘キー**: " + "k" * 9000 + "\n").encode()
    task_text = ("> **無人実行**: 可\n> **作成日**: 2026-01-02\n").encode()
    for mode, limit in (("budget", 40), ("budget", 5000), ("cut-first", 20), ("short-fail", 20), ("budget", 0)):
        for label, env in (("", {}), ("-unbuffered", unbuffered)):
            add(f"child-origin{label}-{mode}-{limit}", CHILD, ["origin-json"], f"1:{mode}:{limit}", stdin=big_origin, env=env)
            traced = (label, mode, limit) in {("", "budget", 40), ("-unbuffered", "budget", 40), ("-unbuffered", "budget", 5000),
                                              ("", "short-fail", 20), ("-unbuffered", "short-fail", 20),
                                              ("", "budget", 0), ("-unbuffered", "budget", 0)}
            add(f"candidate-check{label}-{mode}-{limit}", CAND, ["--check", "-"], f"1:{mode}:{limit}", stdin=big_key, env=env,
                frames=[[CAND_MODULE, ("candidate-keys.py", 227, "main")]] if traced else None)
            add(f"task-info{label}-{mode}-{limit}", TASK, ["taskinfo", "-"], f"1:{mode}:{limit}", stdin=task_text, env=env)
    add("child-origin-stderr-budget-30", CHILD, ["origin-json"], "2:budget:30", stdin=big_origin, stdout="full")
    add("task-info-stderr-cut-20", TASK, ["taskinfo", "-"], "2:cut-first:20", stdin=task_text, stdout="full")
    return cases


ARG_PREFIX = {
    "store": [("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 26, "main")],
    "candidate": [("candidate-keys.py", 232, "<module>"), ("candidate-keys.py", 208, "main")],
}
ARG_ORIGIN = {
    "extra": [("argparse.py", 1836, "parse_args")],
    "argument": [("argparse.py", 1833, "parse_args"), ("argparse.py", 1869, "parse_known_args")],
    "ambiguous": [("argparse.py", 1833, "parse_args"), ("argparse.py", 1866, "parse_known_args"),
                  ("argparse.py", 1910, "_parse_known_args"), ("argparse.py", 2230, "_parse_optional")],
}
ARG_CAUSE = {
    "one": [("argparse.py", 1866, "parse_known_args"), ("argparse.py", 2079, "_parse_known_args"),
            ("argparse.py", 2009, "consume_optional"), ("argparse.py", 2174, "_match_argument")],
    "bundled": [("argparse.py", 1866, "parse_known_args"), ("argparse.py", 2079, "_parse_known_args"),
                ("argparse.py", 1987, "consume_optional")],
    "long": [("argparse.py", 1866, "parse_known_args"), ("argparse.py", 2079, "_parse_known_args"),
             ("argparse.py", 2001, "consume_optional")],
}
USAGE_TAIL = [("argparse.py", 2592, "error"), ("argparse.py", 2562, "print_usage"), ("argparse.py", 2573, "_print_message")]
MESSAGE_TAIL = [("argparse.py", 2594, "error"), ("argparse.py", 2580, "exit"), ("argparse.py", 2573, "_print_message")]


def m3b_cases():
    """誤り表示の途中の書込みの失敗・標準入力の閉鎖と EAGAIN・permlog の close の失敗・dumps の深さ。"""
    cases = []
    unbuffered = {"PYTHONUNBUFFERED": "1"}

    def add(case_id, helper, argv, **kw):
        cases.append(Case(f"m3b/{case_id}", helper, argv, **kw))

    # 1. argparse の誤り表示の途中(使い方の書込み・本文の書込み)で標準エラーの書込みが 1 回だけ失敗する。
    shapes = {
        "store": [("extra", None, ["--bad"]), ("argument", "one", ["--value-json"]), ("argument", "bundled", ["-hbad"]),
                  ("argument", "long", ["--help=x"]), ("ambiguous", None, ["--=x"])],
        "candidate": [("extra", None, ["--bad"]), ("argument", "one", ["--task-dir"]), ("argument", "long", ["--check=1"]),
                      ("ambiguous", None, ["--=x"])],
    }
    for helper_key, items in shapes.items():
        helper = STORE if helper_key == "store" else CAND
        for origin, cause, argv in items:
            label = argv[0].lstrip("-").replace("=", "-").replace(" ", "")
            for mode, tail in (("fail-first", USAGE_TAIL), ("fail-second", MESSAGE_TAIL)):
                for env_label, env in (("", {}), ("-unbuffered", unbuffered)):
                    group = ARG_PREFIX[helper_key] + ARG_ORIGIN[origin] + tail
                    groups = [group] if cause is None else [ARG_CAUSE[cause], group]
                    add(f"argparse-{helper_key}-{label}-{mode}{env_label}", helper, argv, fault=f"2:{mode}:28", env=env,
                        frames=groups)
    # 標準出力(help の書込み)の失敗。
    help_group = [("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 26, "main"),
                  ("argparse.py", 1833, "parse_args"), ("argparse.py", 1866, "parse_known_args"),
                  ("argparse.py", 2079, "_parse_known_args"), ("argparse.py", 2019, "consume_optional"),
                  ("argparse.py", 1943, "take_action"), ("argparse.py", 1106, "__call__"),
                  ("argparse.py", 2567, "print_help"), ("argparse.py", 2573, "_print_message")]
    add("argparse-store-help-stdout-fail-first-unbuffered", STORE, ["--help"], fault="1:fail-first:28", env=unbuffered,
        frames=[help_group])
    add("argparse-store-help-stdout-fail-first", STORE, ["--help"], fault="1:fail-first:32")
    add("argparse-store-help-stdout-epipe-unbuffered", STORE, ["--help"], fault="1:fail-first:32", env=unbuffered,
        frames=[help_group])
    # 2. `-c` で動かす loop の 2 本。print の失敗(標準出力・標準エラー)・PYTHONUNBUFFERED=1。
    task_text = "> **無人実行**: 可\n> **作成日**: 2026-01-02\n".encode()
    origin = json.dumps({"origin": True, "same": True, "vcs": True, "reason": "ok", "repo": "r"}).encode()
    held_calls = {TASK: ("taskinfo", 99, 103, task_text), CHILD: ("origin-json", 138, 142, origin)}
    for helper, (command, call_line, print_line, stdin) in held_calls.items():
        key = "task" if helper == TASK else "child"
        argv = [command, "-"] if helper == TASK else [command]
        for mode in ("fail-first", "fail-second"):
            for env_label, env in (("", {}), ("-unbuffered", unbuffered)):
                add(f"held-{key}-stdout-{mode}{env_label}", helper, argv, fault=f"1:{mode}:28", env=env, stdin=stdin)
                add(f"held-{key}-stdout-epipe-{mode}{env_label}", helper, argv, fault=f"1:{mode}:32", env=env, stdin=stdin)
        # 標準エラーの診断(`補助の失敗:`)の print が失敗する → 元の例外が文脈に残る。
        for mode in ("fail-first",):
            for env_label, env in (("", {}), ("-unbuffered", unbuffered)):
                add(f"held-{key}-unknown-stderr-{mode}{env_label}", helper, ["unknown"], fault=f"2:{mode}:28", env=env,
                    frames=[[("<string>", call_line, "<module>")], [("<string>", print_line, "<module>")]])
                add(f"held-{key}-stdout-then-stderr-fail{env_label}", helper, argv, fault=f"1:{mode}:28", env=env, stdin=stdin)
    # 3. 標準入力の閉鎖・EAGAIN(最初と途中)。閉鎖の case は、起動時に閉じた fd を Rust 標準の実行時が `/dev/null` で開き直す前提で、旧版と別の固定期待で照らす(08:55 の決定)。
    candidate_read = [("candidate-keys.py", 232, "<module>"), ("candidate-keys.py", 217, "main"),
                      ("candidate-keys.py", 196, "check"), ("candidate-keys.py", 110, "_read")]
    candidate_decode = candidate_read[:3] + [("candidate-keys.py", 111, "_read")]
    for env_label, env in (("", {}), ("-unbuffered", unbuffered)):
        add(f"stdin-closed-candidate{env_label}", CAND, ["--check", "-"], stdin="closed", env=env,
            frames=[candidate_read])
        add(f"stdin-closed-task{env_label}", TASK, ["taskinfo", "-"], stdin="closed", env=env)
        add(f"stdin-closed-child{env_label}", CHILD, ["origin-json"], stdin="closed", env=env)
        add(f"stdin-eagain-candidate{env_label}", CAND, ["--check", "-"], stdin=("eagain", b""), env=env,
            frames=[candidate_decode])
        add(f"stdin-eagain-task{env_label}", TASK, ["taskinfo", "-"], stdin=("eagain", b""), env=env)
        add(f"stdin-eagain-child{env_label}", CHILD, ["origin-json"], stdin=("eagain", b""), env=env)
        add(f"stdin-eagain-partial-candidate{env_label}", CAND, ["--check", "-"],
            stdin=("eagain", b"> **\xe7\x99\xba\xe8\xa6\x8b**"), env=env)
        add(f"stdin-eagain-partial-task{env_label}", TASK, ["taskinfo", "-"], stdin=("eagain", task_text), env=env)
        add(f"stdin-eagain-partial-child{env_label}", CHILD, ["origin-json"], stdin=("eagain", origin), env=env)
        add(f"stdin-eagain-partial-child-invalid{env_label}", CHILD, ["origin-json"], stdin=("eagain", b'{"origin"'), env=env)
    # 4. permlog の close の失敗(開いた後に close が OSError)。旧版へは差込みライブラリで同じ条件を作る。
    for errno_value in ("5", "9", "28"):
        for name in ("perm-ok", "perm-empty", "perm-no-lf", "perm-bad-json", "perm-dir"):
            path = f"child/{name}.log" if name != "perm-dir" else "child/perm-dir"
            add(f"permlog-close-fail-{errno_value}-{name}", CHILD, ["permlog", path], preload={"R4A_CLOSE_FAIL": errno_value})
        add(f"permlog-close-fail-{errno_value}-missing", CHILD, ["permlog", "child/absent.log"],
            preload={"R4A_CLOSE_FAIL": errno_value})
    add("permlog-close-fail-unbuffered", CHILD, ["permlog", "child/perm-ok.log"], preload={"R4A_CLOSE_FAIL": "5"}, env=unbuffered)
    return cases


def make_mirror_inputs(directory):
    """旧テストが使う入力を、旧テストと同じ内容で作る(対応表 legacy-coverage-map.md の case が使う)。"""
    base = Path(directory) / "mirror"
    base.mkdir(exist_ok=True)
    for index, data in enumerate(MIRROR_TASK_DATA):
        write(base, f"task-{index}.md", data)
    candidate = ("> **発見元**: data-audit\n> **指摘キー**: data-audit:authz:route:src/routes/orders.ts#ordersRouter:GET /:id\n"
                 "> **発見時点**: 2026-01-01\n## 本文\n")
    for marker in ("```", "~~~"):
        name = "backtick" if marker == "```" else "tilde"
        directory_ = base / f"unclosed-{name}"
        directory_.mkdir(exist_ok=True)
        text = candidate + f"\n{marker}\n> **指摘キー**: fenced\n## 追加修正記録\n"
        write(directory_, "候補_未閉鎖.md", text)
    for n, text in enumerate(indent_texts()):
        directory_ = base / f"indent-{n}"
        directory_.mkdir(exist_ok=True)
        write(directory_, "候補_字下げ.md", text)
    write(base, "file", b"")
    if not os.path.lexists(base / "broken"):
        os.symlink("missing-target", base / "broken")
    (base / "dir").mkdir(exist_ok=True)
    write(base / "dir", "候補_a.md", candidate)
    write(base, "x.md", candidate)
    # ファイルの種類(loop-task-text・result・--check の入力)。
    kinds = base / "kinds"
    kinds.mkdir(exist_ok=True)
    write(kinds, "target.md", candidate)
    for name, target in (("link.md", "target.md"), ("broken.md", "missing.md")):
        if not os.path.lexists(kinds / name):
            os.symlink(target, kinds / name)
    write(kinds, "locked.md", candidate)
    (kinds / "locked.md").chmod(0)
    if not os.path.lexists(kinds / "fifo"):
        os.mkfifo(kinds / "fifo")
    (kinds / "dir").mkdir(exist_ok=True)


def indent_texts():
    """旧テスト test_unsupported_indentation_… の 28 通りの候補本文(字下げ × 記号 × 閉じの有無)。"""
    candidate = ("> **発見元**: data-audit\n> **指摘キー**: data-audit:authz:route:src/routes/orders.ts#ordersRouter:GET /:id\n"
                 "> **発見時点**: 2026-01-01\n## 本文\n")
    texts = []
    for indent in (" " * 4, " " * 5, " " * 8, "\t", " \t", "\t ", "  \t  "):
        for marker in ("```", "~~~"):
            for closed in (False, True):
                header = f"{indent}{marker}text\n> **指摘キー**: literal\n" + (f"{indent}{marker}\n" if closed else "")
                texts.append(candidate.replace("> **発見時点**:", header + "> **発見時点**:"))
    return texts


HOLD = "- **保留**(ship-task・2026-10-09): "
MIRROR_TASK_DATA = [
    b"",
    "\ufeff# task\r> **無人実行**: 可\r\n> **作成日**: 2026-10-09\r## body\r> **作成日**: 2000-01-01".encode(),
    b"## body\n" + "\ufeff# task\r> **無人実行**: 可\r\n> **作成日**: 2026-10-09\r## body\r> **作成日**: 2000-01-01".encode(),
    b"\xff\n" + "> **無人実行**: 可".encode(),
    "\ufeff\ufeff> **無人実行**: 可".encode(),
    "```\n## false\n> **作成日**: 2000-01-01\n```\n> **無人実行**: 可\n## real\n".encode(),
    (HOLD + "G9 ignored\n## 追加修正記録\n" + HOLD + "G1 stop\n## other\n" + HOLD + "G8 ignored\n## 追加修正記録\n" + HOLD + "D2 stop\n").encode(),
    (HOLD + "G9 ignored\n## 追加修正記録\n" + HOLD + "G1 stop\n## other\n" + HOLD + "G8 ignored\n## 追加修正記録\n" + HOLD + "D2 stop\n" + HOLD + "番号なし\n").encode(),
]
for _reason in ("G1: 許可の要る操作が拒否された", "S4: 承認の後に本文が変わった", "G1 許可の拒否"):
    MIRROR_TASK_DATA.append(("## 追加修正記録\n" + "- **保留**(ship-task・2026-11-04): " + _reason + " — 人が確認する\n").encode())
_hold_g1 = "- **保留**(ship-task・2026-10-09): G1 stop\n"
for _opening, _closing in [("```", "```"), ("   ~~~~ info", "  ~~~~~"), ("````", "`````")]:
    MIRROR_TASK_DATA.append(("## 追加修正記録\n" + _opening + "\n" + _hold_g1 + _closing + "\n" + _hold_g1).encode())
for _closing in ["~~~", "``", "``` trailing", "    ```"]:
    MIRROR_TASK_DATA.append(("## 追加修正記録\n```\n" + _hold_g1 + _closing + "\n" + _hold_g1).encode())
MIRROR_TASK_DATA.append(("## 追加修正記録\n    ```\n" + _hold_g1).encode())
MIRROR_TASK_DATA.append(("## 追加修正記録\n" + _hold_g1 + "~~~\n" + _hold_g1.replace("G1", "D2")).encode())


def mirror_cases():
    """旧テストの条件を、旧テストと同じ入力で再現する case(旧テストの観点の対応表が指す)。"""
    cases = []
    first = [True]

    def add(case_id, helper, argv, **kw):
        case = Case(f"mirror/{case_id}", helper, argv, **kw)
        if first[0]:
            case.setup = make_mirror_inputs
            first[0] = False
        cases.append(case)

    # test_task_store.py
    for label, value in {"false": "false", "true": "true", "zero": "0", "one": "1", "float": "1.5", "list": "[]", "dict": "{}",
                         "list-file": '["file"]', "space": '" "', "upper": '"FILE"', "issue": '"issue"',
                         "issue-lf": '"github-issue\\n"', "null": "null", "empty": '""', "file": '"file"', "gh": '"github-issue"'}.items():
        add(f"store-value-{label}", STORE, ["--value-json", value])
    for label, value in {"empty": "", "bare-file": "file", "open-bracket": "[", "null-trailing": "null trailing"}.items():
        add(f"store-json-{label}", STORE, ["--value-json", value])
    add("store-omitted", STORE, [])
    # test_host_argv.py
    for arg in ("--continue", "-c", "--resume=x", "-rfoo", "-vc", "--remote", "--cloud=id", "--plugin-url=x", "--bare",
                "--settings={}", "--no-hooks", "--mystery", "--verbose=false", "--model", "--model=--continue", "--",
                "prompt", "--max-turns=0", "--effort=unknown"):
        add(f"host-unsafe-{arg.strip('-').replace('=', '-').replace('/', '_').replace('{}', 'braces') or 'dd'}", HOST, ["host", arg])
    for label, argv in {"normal": ["host", "--model=model", "--effort=high", "--max-budget-usd=1.5", "--max-turns=4", "--name=normal", "--verbose"],
                        "only-host": ["host"], "none": [], "empty-exe": [""], "verbose-first": ["--verbose"],
                        "dup-verbose": ["host", "--verbose", "--verbose"], "dup-model": ["host", "--model=a", "--model=b"],
                        "cli-normal": ["--", "nonexistent-host", "--model=normal"],
                        "cli-secret": ["--", "nonexistent-host", "--resume=private-sentinel"]}.items():
        add(f"host-{label}", HOST, argv)
    # test_loop_task_text.py(ファイル入力と標準入力の両方・3 コマンド)
    for index in range(len(MIRROR_TASK_DATA)):
        for command in ("taskinfo", "holdcount", "holdcode"):
            add(f"task-{index}-{command}-file", TASK, [command, f"mirror/task-{index}.md"])
            add(f"task-{index}-{command}-stdin", TASK, [command, "-"], stdin=MIRROR_TASK_DATA[index])
    for command in ("taskinfo", "holdcount", "holdcode"):
        add(f"task-error-none-{command}", TASK, [command])
        add(f"task-error-extra-{command}", TASK, [command, "-", "extra"], stdin=b"")
        add(f"task-error-missing-{command}", TASK, [command, "/nonexistent-loop-task-text-fixture"])
    # test_candidate_keys.py
    for label, argv in {"none": [], "file": ["--task-dir=mirror/file"], "broken": ["--task-dir=mirror/broken"],
                        "dir-extra": ["--task-dir=mirror/dir", "extra"], "dir-source": ["--task-dir=mirror/dir", "--source=refactor"],
                        "check-only": ["--check"], "check-dir": ["--check", "--task-dir=mirror/dir", "x.md"],
                        "check-unknown-source": ["--check", "--source=unknown", "x.md"], "unknown": ["--unknown"]}.items():
        add(f"candidate-usage-{label}", CAND, argv)
    add("candidate-missing-dir", CAND, ["--task-dir=mirror/missing"])
    for name in ("backtick", "tilde"):
        add(f"candidate-unclosed-{name}-check-file", CAND, ["--check", f"mirror/unclosed-{name}/候補_未閉鎖.md"])
        add(f"candidate-unclosed-{name}-collect", CAND, [f"--task-dir=mirror/unclosed-{name}"])
    for n in range(28):
        add(f"candidate-indent-{n}-collect", CAND, [f"--task-dir=mirror/indent-{n}"])
        add(f"candidate-indent-{n}-check-file", CAND, ["--check", f"mirror/indent-{n}/候補_字下げ.md"])
        add(f"candidate-indent-{n}-check-stdin", CAND, ["--check", "-"], stdin=indent_texts()[n].encode())
    add("candidate-bytecode-collect", CAND, ["--task-dir=mirror/dir"], bytecode=True)
    add("candidate-bytecode-check", CAND, ["--check", "mirror/x.md"], bytecode=True)
    return cases


STORE_ENTRY = (STORE, "store", ["--help"], b"")
HOST_ENTRY = (HOST, "host", ["run"], b"")
CAND_ENTRY = (CAND, "candidate", ["--check", "-"], b"> **x**\n")
TASK_ENTRY = (TASK, "task", ["taskinfo", "-"], b"> **\xe7\x84\xa1\xe4\xba\xba\xe5\xae\x9f\xe8\xa1\x8c**: \xe5\x8f\xaf\n")
CHILD_ENTRY = (CHILD, "child", ["origin-json"], b"{}")


def m3c_cases():
    """中間レビューの指摘を閉じる case: 標準エラーの書込みが途中で失敗した後の続け方・旧版だけが切れる範囲・起動時の fd。"""
    cases = []
    unbuffered = {"PYTHONUNBUFFERED": "1"}

    def add(case_id, helper, argv, **kw):
        cases.append(Case(f"m3c/{case_id}", helper, argv, **kw))

    type_error = [STORE_MODULE + [("json/__init__.py", 339, "loads")]]
    for env_label, env in (("", {}), ("-unbuffered", unbuffered)):
        # (1) 書込みが 1 回だけ失敗した後も、旧 Python は残りを書き続ける(最後の改行・ソースの行の失敗の無視)。
        for mode in ("fail-first", "fail-second"):
            add(f"traceback-{mode}{env_label}", STORE, ["--value-json=--"], fault=f"2:{mode}:28", env=env, frames=type_error)
            add(f"traceback-recursion-{mode}{env_label}", STORE, ["--value-json", "[" * 995 + "]" * 995], fault=f"2:{mode}:28",
                env=env, frames=LOADS_RECURSION)
            add(f"stdout-full-{mode}{env_label}", STORE, [], fault=f"2:{mode}:28", stdout="full", env=env,
                frames=STORE_SUCCESS_FRAMES if env else None)
            add(f"stdout-full-epipe-{mode}{env_label}", STORE, [], fault=f"2:{mode}:32", stdout="full", env=env,
                frames=STORE_SUCCESS_FRAMES if env else None)
        # (6) 合計の上限が、旧版の実パスの欄の途中に来て、固定名の欄は切れない(旧版だけが切れる範囲)。
        for limit in (300, 427, 450, 520, 600, 700):
            add(f"legacy-only-cut-budget-{limit}{env_label}", STORE, ["--value-json=--"], fault=f"2:budget:{limit}", env=env,
                frames=type_error)
    # (M3d-1) except の中の print が失敗して、捕まえた例外が文脈として出る。文脈の呼出履歴は、捕まえた関数の Frame から始まり、
    # builtins 以外の例外の名前にはモジュール名が付く。
    loads_frames = [("json/__init__.py", 346, "loads"), ("json/decoder.py", 337, "decode"), ("json/decoder.py", 355, "raw_decode")]
    store_py = "resolve-task-store.py"
    context_cases = {
        "store-json-error": (STORE, ["--value-json=x"], [[(store_py, 28, "main")] + loads_frames, [(store_py, 41, "<module>"), (store_py, 31, "main")]]),
        "store-invalid-value": (STORE, ['--value-json="x"'], [[(store_py, 29, "main"), (store_py, 20, "resolve")],
                                                               [(store_py, 41, "<module>"), (store_py, 34, "main")]]),
        "host-exe": (HOST, ["-x"], [[("host-argv.py", 33, "main"), ("host-argv.py", 9, "validate_argv")],
                                    [("host-argv.py", 41, "<module>"), ("host-argv.py", 35, "main")]]),
        "host-duplicate": (HOST, ["run", "--model=a", "--model=b"], [[("host-argv.py", 33, "main"), ("host-argv.py", 19, "validate_argv")],
                                                                    [("host-argv.py", 41, "<module>"), ("host-argv.py", 35, "main")]]),
        "candidate-scandir": (CAND, ["--task-dir=locked"], [[("candidate-keys.py", 122, "collect")],
                                                            [("candidate-keys.py", 232, "<module>"), ("candidate-keys.py", 226, "main"),
                                                             ("candidate-keys.py", 125, "collect")]]),
    }
    for label, (helper, argv, groups) in context_cases.items():
        for mode in ("fail-first", "fail-second"):
            for env_label, env in (("", {}), ("-unbuffered", unbuffered)):
                # 通常のバッファでは print の本文と LF が 1 回の書込みにまとまり、2 番目の書込みは起きない(Traceback も出ない)。
                no_second_write = mode == "fail-second" and not env
                add(f"context-{label}-{mode}{env_label}", helper, argv, fault=f"2:{mode}:28", env=env,
                    frames=None if no_second_write else groups)
    # (M3e-1) ソースの行の 3 つの書込み(字下げ・本文・改行)のどれが失敗しても、次の Frame へ進む。n 番目の 1 回だけ失敗させる。
    for env_label, env, counts in (("-unbuffered", unbuffered, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18)),
                                   ("", {}, (1, 2, 3, 4, 5, 6, 7, 8, 9))):
        for n in counts:
            add(f"nth-traceback-{n}{env_label}", STORE, ["--value-json=--"], fault=f"2:fail-nth:{n}", env=env, frames=type_error)
    for n in (1, 6, 10, 14, 18, 30, 34):
        add(f"nth-argparse-{n}-unbuffered", STORE, ["--value-json"], fault=f"2:fail-first-and-nth:{n}", env=unbuffered,
            frames=[ARG_CAUSE["one"], ARG_PREFIX["store"] + ARG_ORIGIN["argument"] + USAGE_TAIL])
    # (2)(7b) 標準出力・標準エラーを閉じた条件。argparse の使い方は、標準エラーが無ければ標準出力へ、どちらも無ければ AttributeError。
    entries = [
        ("store-usage", STORE, ["--bad"], b""), ("store-help", STORE, ["--help"], b""), ("store-ok", STORE, [], b""),
        ("host-error", HOST, ["-x"], b""), ("host-ok", HOST, ["run"], b""),
        ("candidate-usage", CAND, ["--bad"], b""), ("candidate-check", CAND, ["--check", "-"], b"> **x**\n"),
        ("task-info", TASK, ["taskinfo", "-"], b""), ("task-unknown", TASK, ["unknown"], b""),
        ("child-origin", CHILD, ["origin-json"], b"{}"), ("child-unknown", CHILD, ["unknown"], b""),
    ]
    for label, helper, argv, stdin in entries:
        for which in ("stdin", "stdout", "stderr", "stdout+stderr", "all"):
            for env_label, env in (("", {}), ("-unbuffered", unbuffered)):
                kw = {}
                if which in ("stdin", "all"):
                    kw["stdin"] = "closed"
                else:
                    kw["stdin"] = stdin
                if which in ("stdout", "stdout+stderr", "all"):
                    kw["stdout"] = "closed"
                if which in ("stderr", "stdout+stderr", "all"):
                    kw["stderr"] = "closed"
                add(f"closed-{label}-{which}{env_label}", helper, argv, env=env, **kw)
    # (M3f-3) PYTHONOPTIMIZE(`-OO` 相当で `__doc__` が消える)と PYTHONHASHSEED(有効な値は出力を変えない・不正な値は起動に失敗する)。
    for label, value in {"empty": "", "zero": "0", "one": "1", "two": "2", "three": "3", "hundred": "100", "int-max": "2147483647",
                         "over-int": "2147483648", "huge": "99999999999", "negative": "-5", "text": "x", "space-two": " 2",
                         "two-space": "2 ", "plus-two": "+2", "float": "1.5", "hex": "0x2", "space-only": " ", "tab-three": "\t3",
                         "double-zero": "00", "zero-two": "02", "fullwidth": "２"}.items():
        add(f"optimize-{label}-help", STORE, ["--help"], env={"PYTHONOPTIMIZE": value})
        add(f"optimize-{label}-ok", STORE, [], env={"PYTHONOPTIMIZE": value})
    add("optimize-two-usage", STORE, ["--bad"], env={"PYTHONOPTIMIZE": "2"})
    add("optimize-two-width-help", STORE, ["--help"], env={"PYTHONOPTIMIZE": "2", "COLUMNS": "30"})
    add("optimize-two-host", HOST, ["-x"], env={"PYTHONOPTIMIZE": "2"})
    add("optimize-two-candidate", CAND, ["--bad"], env={"PYTHONOPTIMIZE": "2"})
    add("optimize-two-task", TASK, ["taskinfo", "-"], env={"PYTHONOPTIMIZE": "2"}, stdin="> **無人実行**: 可\n".encode())
    add("optimize-two-child", CHILD, ["origin-json"], env={"PYTHONOPTIMIZE": "2"}, stdin=b"{}")
    for label, value in {"random": "random", "zero": "0", "minus-zero": "-0", "plus-zero": "+0", "one": "1", "max": "4294967295",
                         "over": "4294967296", "negative": "-1", "text": "x", "empty": "", "space-one": " 1", "one-space": "1 ",
                         "double-zero": "00", "plus-one": "+1", "random-x": "randomx", "upper-random": "Random", "hex": "0x1",
                         "underscore": "1_0", "fullwidth": "４"}.items():
        for helper, name, argv, stdin in (STORE_ENTRY, HOST_ENTRY, CAND_ENTRY, TASK_ENTRY, CHILD_ENTRY):
            add(f"hashseed-{label}-{name}", helper, argv, env={"PYTHONHASHSEED": value}, stdin=stdin)
    add("hashseed-invalid-wins-over-int-limit", STORE, [], env={"PYTHONHASHSEED": "x", "PYTHONINTMAXSTRDIGITS": "5"})
    add("hashseed-invalid-with-stdin-dir", STORE, [], env={"PYTHONHASHSEED": "x"}, stdin="dir")
    add("hashseed-valid-big-json", STORE, ["--value-json", '{"a":[1,2,3]}'], env={"PYTHONHASHSEED": "123"})
    return cases


def all_cases(helpers):
    cases = []
    if STORE in helpers:
        cases += store_cases()
    if HOST in helpers:
        cases += host_cases()
    cases += [c for c in fault_cases() + pty_cases() + signal_cases() if c.helper in helpers]
    cases += [c for c in charset_cases() if c.helper in helpers]
    if TASK in helpers:
        cases += task_cases()
    if CAND in helpers:
        cases += candidate_cases()
    if CHILD in helpers:
        cases += child_cases()
    cases += [c for c in m3b_cases() + mirror_cases() + m3c_cases() if c.helper in helpers]
    return cases


# ---------------------------------------------------------------- 実行


def child_env(case):
    # 一覧に無い `PYTHON*` も外す(一覧は、実行ファイルの文字列との照合で網羅を確かめる)。
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON") and k not in STRIPPED_ENV}
    for name in KEPT_ENV:
        env.pop(name, None)
    env["PYTHONUTF8"] = "1"  # 文字コード設定を固定する(承認差。UTF-8 + surrogateescape)
    if not case.bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    else:
        env.pop("PYTHONDONTWRITEBYTECODE", None)  # 旧スクリプト自身が __pycache__ を作らないことを確かめる
    env["PYTHONNOUSERSITE"] = "1"  # 利用者の site の .pth を前提の外にする(無いことを下で確かめる)
    env.pop("COLUMNS", None)
    env.pop("LINES", None)
    for name, value in case.env.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    for name in STRIPPED_ENV:
        if name in env:
            raise Failure(f"{case.id}: 前提の外の変数 {name} が子の環境に残っている")
    for name, value in case.env.items():
        if env.get(name) != value:
            raise Failure(f"{case.id}: case が与えた変数 {name} が子の環境から消えた")
    return env


class FdCloser:
    """directory の fd は `os.fdopen` で包めないので、閉じる処理だけを持つ。"""

    def __init__(self, fd):
        self.fd = fd

    def close(self):
        os.close(self.fd)


# ---------------------------------------------------------------- 文字コード設定(承認差)

CHARSET_FILE = FIXTURES / "charset_expectations.json"
CLOSED_FILE = FIXTURES / "closed_fd_expectations.json"


STORE_HELP_FRAMES = [[("resolve-task-store.py", 41, "<module>"), ("resolve-task-store.py", 26, "main"),
                      ("argparse.py", 1833, "parse_args"), ("argparse.py", 1866, "parse_known_args"),
                      ("argparse.py", 2079, "_parse_known_args"), ("argparse.py", 2019, "consume_optional"),
                      ("argparse.py", 1943, "take_action"), ("argparse.py", 1106, "__call__"),
                      ("argparse.py", 2567, "print_help"), ("argparse.py", 2573, "_print_message")]]


def charset_cases():
    """文字コード設定だけによる差の case。設定ごとの旧新別の期待で検査し、承認差を通常一致の分母へ混ぜない。"""
    configs = {}
    for encoding in ("utf-8", "ascii", "latin-1", "utf-16", "cp932"):
        for handler in ("", ":strict", ":backslashreplace", ":surrogateescape", ":replace", ":ignore"):
            configs[f"io-{encoding}{handler.replace(':', '-')}"] = {"PYTHONIOENCODING": encoding + handler}
    for value in ("nope", "nope:strict", "utf-8:nope", "rot13", "hex", "rot13:strict", "latin-1:replace", "ascii:ignore"):
        configs[f"io-special-{value.replace(':', '-')}"] = {"PYTHONIOENCODING": value}
    for value in ("0", "1", "x", ""):
        configs[f"utf8-{value or 'empty'}"] = {"PYTHONUTF8": value}
    for locale in ("C", "C.UTF-8", "POSIX", "ja_JP.eucJP"):
        configs[f"locale-{locale}"] = {"LC_ALL": locale, "PYTHONUTF8": None}
    for coerce in ("0", "1", "warn", "x"):
        configs[f"coerce-{coerce}"] = {"LC_ALL": "C", "PYTHONUTF8": None, "PYTHONCOERCECLOCALE": coerce}
    commands = {
        "store-help": (STORE, ["--help"], b""),
        "host-error": (HOST, ["-x"], b""),
        "store-arg-echo": (STORE, ["--bad=日本語"], b""),
        "store-non-utf8-arg": (STORE, None, b""),
        "candidate-key": (CAND, ["--check", "-"], "> **発見元**: refactor\n> **指摘キー**: 日本語é\n".encode()),
        "child-origin": (CHILD, ["origin-json"],
                         json.dumps({"origin": True, "same": True, "vcs": True, "reason": "理由", "repo": "リポジトリ"},
                                    ensure_ascii=False).encode()),
    }
    cases = []
    for config, env in configs.items():
        for name, (helper, argv, stdin) in commands.items():
            kwargs = {"binary_argv": [b"--bad\xff"]} if argv is None else {}
            # 標準出力の符号化の失敗で Traceback が出る 2 本(store の help・candidate の出力)には、固定期待の Frame を付ける。
            frames = {"store-help": STORE_HELP_FRAMES,
                      "candidate-key": [[("candidate-keys.py", 232, "<module>"), ("candidate-keys.py", 227, "main")]]}.get(name)
            cases.append(Case(f"charset/{config}/{name}", helper, argv or [], env=env, stdin=stdin, charset=config,
                              frames=frames, **kwargs))
    return cases


def charset_codec(case):
    """設定の文字コード(PYTHONIOENCODING の `:` より前)。標準エラーの復号に使う。"""
    value = case.env.get("PYTHONIOENCODING") or "utf-8"
    return value.split(":")[0] or "utf-8"


def map_charset_stderr(err, case, mapper):
    """標準エラーを設定の文字コードで復号し、構造(Mapper.parse・apply)で解析して Frame の File 欄の旧版の実行環境のパスを
    固定名にしてから、同じ文字コードで符号化し直す。Traceback が出るときは、case の固定期待の Frame を厳密に当てる。
    復号できない出力は対応づけず、そのまま比べる(設計: 復号できない欄は通常どおり)。"""
    codec = charset_codec(case)
    try:
        text = err.decode(codec)
    except (UnicodeDecodeError, LookupError):
        return err
    utf8 = text.encode("utf-8", "surrogatepass")
    _, frames = mapper.parse(utf8)
    if not frames:
        return err
    if case.frames is None:
        raise Failure("Traceback が出たが、この case に固定期待の Frame が無い")
    mapped, _ = mapper.apply(utf8, case.frames)
    try:
        return mapped.decode("utf-8", "surrogatepass").encode(codec)
    except UnicodeEncodeError:
        raise Failure("対応づけた標準エラーを、設定の文字コードへ符号化し直せない")


def charset_record(result, case=None, mapper=None):
    rc, out, err = result
    if case is not None and mapper is not None:
        err = map_charset_stderr(err, case, mapper)
    if err.startswith(b"Fatal Python error"):
        record = {"rc": rc, "stdout": out.hex(), "startup_failure": err.split(b"\n")[0].hex()}
    else:
        record = {"rc": rc, "stdout": out.hex(), "stderr": err.hex()}
    if case is not None and case.env.get("LC_ALL"):
        record["locale_available"] = locale_available(case.env["LC_ALL"])
    return record


def locale_available(name):
    """その locale が、この機械で使えるか(使えなければ旧版は C に落ちる。結果が機械で変わる)。"""
    result = subprocess.run([sys.executable, "-c", "import locale,sys\ntry:\n locale.setlocale(locale.LC_ALL, sys.argv[1])\nexcept locale.Error:\n sys.exit(1)", name],
                            capture_output=True, env=dict(os.environ, PYTHONUTF8="1"))
    return result.returncode == 0


class Unavailable(Exception):
    """この機械では確かめられない(未実施として数える。成功には数えない)。"""


def run_charset_case(case, env, run_dir, binary, expectations, record, mapper):
    base_case = Case(case.id, case.helper, case.argv, stdin=case.stdin, binary_argv=case.binary_argv)
    base = run_child(legacy_command(case), base_case, child_env(base_case), run_dir)
    configured = run_child(legacy_command(case), case, env, run_dir)
    rust = run_child(rust_command(binary, case), case, env, run_dir)
    current = charset_record(configured, case, mapper)
    if record is not None:
        record[case.id] = current
        return
    if case.id not in expectations:
        raise Failure("設定ありの旧版の固定期待が charset_expectations.json に無い(--record-charset で記録する)")
    expected = expectations[case.id]
    if "locale_available" in expected and expected["locale_available"] != current["locale_available"]:
        raise Unavailable("locale の有無が、記録した機械と違う(結果が locale に依存する)")
    if current != expected:
        raise Failure(f"設定ありの旧版が固定期待と違う: {current!r}")
    if rust != base:
        raise Failure(f"Rust が設定を読んでいる(設定なしの旧版と違う):\n旧(設定なし): {base!r}\n新: {rust!r}")


# ---------------------------------------------------------------- 起動時に閉じた標準の fd(旧新別の期待)


def closed_record(result, mapper):
    """旧版の結果を、実行環境のパスを固定名にして記録する形にする(構造で解析できる Frame の File 欄だけ)。"""
    rc, out, err = result
    lines, frames = mapper.parse(err)
    for at, path, line, func, _ in frames:
        fixed = path if path.startswith(b"<") and path.endswith(b">") else mapper.fixed(path)
        if fixed is not None:
            lines[at] = b'  File "' + fixed + b'", line ' + str(line).encode() + b", in " + func + eol(lines[at])
    return {"rc": rc, "stdout": out.hex(), "stderr": b"\n".join(lines).hex()}


def run_closed_case(case, env, run_dir, binary, mapper, expectations, record):
    """起動時に閉じた fd(0〜2)。Rust は標準の実行時の初期化(`/dev/null` で開き直す)をそのまま使い、旧版との差を認める
    (2026-11-12 08:55 の決定)。旧版は閉じたまま動かした実測を固定期待と比べる。Rust の期待は、旧版を閉じた fd の代わりに
    `/dev/null` を開いて動かした結果で、Rust の固定期待とも一致させる。認めた差は開き直しだけで、その後の動きは旧版に合わせる。"""
    null_case = Case(case.id, case.helper, case.argv, stdin=case.stdin, binary_argv=case.binary_argv, env=case.env,
                     stdout="null" if case.stdout == "closed" else case.stdout,
                     stderr="null" if case.stderr == "closed" else case.stderr)
    if case.stdin == "closed":
        null_case.stdin = "null"
    old_closed = run_child(legacy_command(case), case, env, run_dir)
    old_null = run_child(legacy_command(null_case), null_case, env, run_dir)
    new_closed = run_child(rust_command(binary, case), case, env, run_dir)
    current = {"legacy_closed": closed_record(old_closed, mapper), "devnull": closed_record(old_null, mapper)}
    if record is not None:
        record[case.id] = current
        return {}
    if case.id not in expectations:
        raise Failure("起動時に閉じた fd の固定期待が closed_fd_expectations.json に無い(--record-closed で記録する)")
    expected = expectations[case.id]
    if current["legacy_closed"] != expected["legacy_closed"]:
        raise Failure(f"閉じた fd のままの旧版が、旧版の固定期待と違う: {current['legacy_closed']!r}")
    if current["devnull"] != expected["devnull"]:
        raise Failure(f"旧版を /dev/null で動かした結果が、Rust の固定期待と違う: {current['devnull']!r}")
    # Rust(閉じた fd を `/dev/null` で開き直す)は、Rust の固定期待と同じ。端末へ向かない(/dev/null)ストリームの出力は空。
    got = closed_record(new_closed, mapper)
    if got != expected["devnull"]:
        raise Failure(f"Rust が /dev/null の期待と違う:\n期待: {expected['devnull']!r}\n実際: {got!r}")
    return {}


# ---------------------------------------------------------------- 書込みの故障(部分書込み)

FAULT_SOURCE = FIXTURES / "fault_shim.c"
FRAME_PIECE = re.compile(rb'^  File "(.*)", line (\d+), in (.*)\n$')


def shim_env(shim):
    """旧版に差込みライブラリを読ませる環境変数(Linux は LD_PRELOAD、Mac は DYLD_INSERT_LIBRARIES)。"""
    return {"DYLD_INSERT_LIBRARIES" if sys.platform == "darwin" else "LD_PRELOAD": str(shim)}


def shim_is_effective(shim):
    """差込みが旧版に効いているかを、旧版の Python 自身で確かめる(効いた印: write が ENOSPC で失敗する)。"""
    code = ("import os,sys\ntry:\n os.write(1,b'x')\nexcept OSError as e:\n sys.stderr.write('R4A-SHIM-EFFECTIVE %d' % e.errno)\n")
    env = dict(os.environ, R4A_FAULT="1:budget:0", PYTHONNOUSERSITE="1", **shim_env(shim))
    try:
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return b"R4A-SHIM-EFFECTIVE 28" in result.stderr


# 差込みが旧版に効くかの期待(OS ごとの固定)。Mac も「効く」を固定している。CI で効かないと分かったら、そのときに固定し直す。
SHIM_EXPECTED_EFFECTIVE = {"linux": True, "darwin": True}


def build_shim(directory):
    """故障の差込みライブラリをビルドする。対象 OS(Linux・Mac)で cc が無い・ビルドに失敗する・
    効きの期待(`SHIM_EXPECTED_EFFECTIVE`)と違うときは、比較器を止める(未実施にしない)。対象外の OS は `None`(未実施として数える)。"""
    if sys.platform == "linux":
        target = Path(directory) / "fault_shim.so"
        command = ["cc", "-shared", "-fPIC", "-O2", "-o", str(target), str(FAULT_SOURCE), "-ldl"]
    elif sys.platform == "darwin":
        target = Path(directory) / "fault_shim.dylib"
        command = ["cc", "-dynamiclib", "-O2", "-o", str(target), str(FAULT_SOURCE)]
    else:
        return None
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        detail = (getattr(error, "stderr", b"") or b"").decode(errors="replace")[-300:]
        raise SystemExit(f"差込みライブラリを作れない(cc が無い・ビルドの失敗)。{sys.platform} でも失敗にする: {error} {detail}")
    effective = shim_is_effective(target)
    if effective != SHIM_EXPECTED_EFFECTIVE[sys.platform]:
        raise SystemExit(f"差込みの効きが固定の期待と違う({sys.platform}: 期待 {SHIM_EXPECTED_EFFECTIVE[sys.platform]}・実際 {effective})")
    if not effective:
        print(f"差込み: {sys.platform} では効かない(固定の期待どおり)。故障・競合の case は未実施にする")
        return None
    print(f"差込み: {sys.platform} で旧版に効いた(故障・競合の case を実施する)")
    return target


def read_trace(path):
    data = Path(path).read_bytes() if Path(path).exists() else b""
    pieces, at = [], 0
    while at + 4 <= len(data):
        length = int.from_bytes(data[at:at + 4], "little")
        pieces.append(data[at + 4:at + 4 + length])
        at += 4 + length
    return pieces


def fault_rule(mode, limit, pieces, retry):
    """書込みの要求の列に故障の規則を当てて、通ったバイト列を返す(故障の後のプログラムの振る舞いは足さない)。
    retry は通常のバッファ(短い書込みの残りを送り直す)。"""
    whole = b"".join(pieces)
    if mode == "budget":
        return whole[:limit]
    first = pieces[0] if pieces else b""
    if mode == "cut-first":
        if len(first) <= limit or retry:
            return whole
        return first[:limit] + b"".join(pieces[1:])
    if mode == "short-fail":
        return whole if len(first) <= limit else first[:limit]
    raise Failure(f"未知の故障の規則: {mode}")


def map_pieces(mapper, pieces):
    """書込みごとの中身で、Frame の行 1 つだけの書込みの File 欄を固定名にする(対象は構造で限る)。"""
    mapped = []
    for piece in pieces:
        match = FRAME_PIECE.match(piece)
        fixed = mapper.fixed(match.group(1)) if match else None
        if fixed is not None:
            piece = b'  File "' + fixed + b'", line ' + match.group(2) + b", in " + match.group(3) + b"\n"
        mapped.append(piece)
    return mapped


def run_fault_case(case, env, run_dir, binary, mapper, shim, work):
    fd, mode, limit = case.fault.split(":")
    fd, limit = int(fd), int(limit)
    retry = "PYTHONUNBUFFERED" not in case.env or case.env["PYTHONUNBUFFERED"] in ("", "0")
    legacy_env = dict(env, **shim_env(shim))
    trace = Path(work) / "trace.bin"
    trace.unlink(missing_ok=True)
    # 故障なしの旧版の書込みの列(同じ case・同じバッファの方式)を記録する。
    quiet = dict(legacy_env, R4A_FAULT=f"{fd}:budget:999999999", R4A_TRACE=str(trace))
    quiet_result = run_child(legacy_command(case), case, quiet, run_dir)
    pieces = read_trace(trace)
    old = run_child(legacy_command(case), case, dict(legacy_env, R4A_FAULT=case.fault), run_dir)
    new = run_child(rust_command(binary, case), case, dict(env, R4A_FAULT=case.fault), run_dir)
    if fd == 2 and mode not in ("fail-first", "fail-second", "fail-nth", "fail-first-and-nth"):
        joined = b"".join(pieces)
        if joined != quiet_result[2]:
            raise Failure("故障なしの旧版の書込みの列と標準エラーが一致しない(記録の失敗)")
        mapped_whole, _ = mapper.apply(joined, case.frames)
        mapped_pieces = map_pieces(mapper, pieces)
        if b"".join(mapped_pieces) != mapped_whole:
            raise Failure("書込みごとの対応づけと、構造での対応づけの結果が違う(固定名の置換が欄の途中の書込みにまたがる)")
        want_old = fault_rule(mode, limit, pieces, retry)
        want_new = fault_rule(mode, limit, mapped_pieces, retry)
        if old[2] != want_old:
            raise Failure(f"故障ありの旧版の標準エラーが、故障なしの書込みの列に同じ規則を当てた結果と違う:\n期待: {want_old!r}\n実際: {old[2]!r}")
        if new[2] != want_new:
            raise Failure(f"Rust の標準エラーが、固定名にした書込みの列に同じ規則を当てた結果と違う:\n期待: {want_new!r}\n実際: {new[2]!r}")
        whole_new = b"".join(mapped_pieces)
        if want_new == whole_new and len(want_old) < len(joined):
            # 置き換えた書込みが切れない(旧版だけが切れる範囲): 故障の無かったときの旧版の結果(固定名の stderr・stdout・終了値)が期待。
            # 故障ありの旧版との終了値の差(例: 120 と 1)は認める(22:38 の決定)。
            if (new[0], new[1]) != (quiet_result[0], quiet_result[1]) or new[2] != mapped_whole:
                raise Failure(f"旧版だけが切れる範囲: Rust は故障の無い旧版の結果を返すはず:\n期待 {(quiet_result[0], quiet_result[1])!r}・実際 {(new[0], new[1])!r}")
        elif old[1] != new[1] or old[0] != new[0]:
            raise Failure(f"標準出力か終了値が違う: 旧 {old[0]} {old[1]!r}・新 {new[0]} {new[1]!r}")
        # 上限の位置: 故障なしの旧版の書込みの列の上で、固定名にする File 欄(実パス)のバイトの範囲を求める。
        ranges, offset = [], 0
        for piece in pieces:
            match = FRAME_PIECE.match(piece)
            if match and mapper.fixed(match.group(1)) is not None:
                start = offset + len(b'  File "')
                ranges.append((start, start + len(match.group(1))))
            offset += len(piece)
        legacy_only = want_new == whole_new and len(want_old) < len(joined)
        if mode == "budget" and ranges:
            inside = any(start < limit <= end for start, end in ranges)
            position = "inside" if inside else ("outside" if limit < len(joined) else "none")
        else:
            position = "none"
        return {"cut_inside_field": [(case.id, position, legacy_only)]}
    # 標準出力の故障、または標準エラーが 1 回だけ失敗する故障。
    # 固定期待のフレームは、Traceback が出る出力(故障なしの旧版の出力、無ければ故障ありの旧版の出力)に厳密に当てる
    # (数・順・File 欄・行・関数)。途中で切れた出力への対応づけは、固定期待の Frame だけに限る。
    quiet_frames = mapper.parse(quiet_result[2])[1]
    if mode == "fail-first-and-nth":
        # 最初の失敗の後の Traceback の途中で、もう 1 回失敗する。Frame の固定期待は、最初の失敗だけの旧版の出力(完全な Traceback)に当てる。
        reference = run_child(legacy_command(case), case, dict(legacy_env, R4A_FAULT=f"{fd}:fail-first:28"), run_dir)
        quiet_result = (quiet_result[0], quiet_result[1], reference[2])
        quiet_frames = mapper.parse(quiet_result[2])[1]
    mapper.glued_header = (fd == 2 and mode in ("fail-first", "fail-second", "fail-nth", "fail-first-and-nth"))
    try:
        return compare_faulted(case, mapper, quiet_result, old, new, fd, mode, quiet_frames)
    finally:
        mapper.glued_header = False


def compare_faulted(case, mapper, quiet_result, old, new, fd, mode, quiet_frames):
    if case.frames is None:
        for label, output in (("故障なし", quiet_result[2]), ("故障あり", old[2])):
            if mapper.parse(output)[1]:
                raise Failure(f"固定期待の無い case の旧版({label})の標準エラーに Traceback がある(固定期待を足す)")
    elif fd == 1 or not (mode in ("fail-first", "fail-second", "fail-nth", "fail-first-and-nth")):
        mapper.apply(old[2], case.frames)
    else:
        mapper.apply(quiet_result[2] if quiet_frames else old[2], case.frames)
    mapped_stderr = mapper.map_expected(old[2], case.frames)
    if old[1] != new[1] or old[0] != new[0] or mapped_stderr != new[2]:
        raise Failure(f"標準出力・標準エラー・終了値が違う: 旧 {old[0]} {old[1]!r} {mapped_stderr!r}・新 {new[0]} {new[1]!r} {new[2]!r}")
    return {}


def run_pty(argv, case, env, cwd):
    """疑似端末で動かす。標準入力は /dev/null。端末に向けた出力は master から読む(CRLF になる)。"""
    import fcntl
    import pty as pty_module
    import struct
    import termios
    rows, cols, which = case.pty
    master, slave = pty_module.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        stdout = slave if which in ("stdout", "both") else subprocess.PIPE
        stderr = slave if which in ("stderr", "both") else subprocess.PIPE
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, cwd=cwd)
        os.close(slave)
        slave = -1
        chunks = []
        deadline = time.monotonic() + CHILD_TIMEOUT
        import select
        while True:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            elif process.poll() is not None:
                # 子が終わった後に残りを読み切る。
                try:
                    while True:
                        ready, _, _ = select.select([master], [], [], 0.1)
                        if not ready:
                            break
                        data = os.read(master, 65536)
                        if not data:
                            break
                        chunks.append(data)
                except OSError:
                    pass
                break
            if time.monotonic() > deadline:
                process.kill()
                raise Failure(f"{case.id}: 時間切れ(疑似端末)")
        out, err = process.communicate(timeout=CHILD_TIMEOUT)
        terminal = b"".join(chunks)
        if which == "stdout":
            return process.returncode, terminal, err or b""
        if which == "stderr":
            return process.returncode, out or b"", terminal
        return process.returncode, terminal, b""
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)


WAIT_STATES = {
    # 子が待っている場所(Linux の /proc/<pid>/wchan の先頭)。標準入力の read・FIFO の open・満杯の pipe への write。
    "stdin": ("pipe_read", "wait_woken", "pipe_wait"),
    "fifo": ("wait_for_partner", "fifo_open"),
    "fullpipe": ("pipe_write", "pipe_wait_writable"),
}


def wait_for_state(pid, argv0, kind):
    """子が待つ場所に入ったことを、Linux では /proc/<pid>/wchan で確かめる(sleep の偶然に頼らない)。
    Linux 以外には確かめる手段が無いので、この関数を使う case は Linux 以外では未実施にする(固定の一覧)。"""
    if sys.platform != "linux":
        raise Failure("Linux 以外では待ちの状態を確かめられない(この case は未実施の固定の一覧に入る)")
    deadline = time.monotonic() + CHILD_TIMEOUT
    while time.monotonic() < deadline:
        try:
            wchan = Path(f"/proc/{pid}/wchan").read_text()
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            wchan, command = "", b""
        if argv0 is not None and not command.startswith(os.fsencode(str(argv0))):
            time.sleep(0.005)
            continue  # exec の前(fork した直後)の子を、待っていると見なさない
        if wchan.startswith(WAIT_STATES[kind]):
            return
        time.sleep(0.02)
    raise Failure(f"子が {kind} で待たなかった(合図の時間切れ)")


def wait_for_blocked_read(pid, argv0=None):
    wait_for_state(pid, argv0, "stdin")


def run_signal(argv, case, env, cwd):
    """case.signal = (信号名, 先に送る標準入力, 待ち方, 信号を受け継いで無視するか, 信号の後に足す入力)。
    待ち方: stdin(標準入力の read)・fifo(FIFO の open)・fullpipe(満杯の pipe への write)。"""
    import signal as signal_module
    name, preload, mode, ignored, tail = case.signal
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
        start_new_session=True,
        preexec_fn=(lambda: signal_module.signal(signal_module.SIGINT, signal_module.SIG_IGN)) if ignored else None)
    try:
        process.stdin.write(preload)
        process.stdin.flush()
        if mode == "fullpipe":
            process.stdin.close()  # 入力は全部渡す。出力は読まない(pipe を満杯にする)
            process.stdin = None
        wait_for_state(process.pid, argv[0], mode)
        process.send_signal(getattr(signal_module, "SIG" + name))
        if mode == "fifo" and tail:
            # 無視した場合: FIFO の open は続いている。書き手が開けば読取りが進む。
            fifo = os.path.join(cwd, case.fifo_name)
            descriptor = os.open(fifo, os.O_WRONLY)
            try:
                os.write(descriptor, tail)
            finally:
                os.close(descriptor)
        elif tail and mode == "stdin":
            process.stdin.write(tail)
            process.stdin.flush()
        try:
            out, err = process.communicate(timeout=CHILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise Failure(f"{case.id}: 信号の後に終わらない(時間切れ)")
        try:
            os.killpg(process.pid, 0)
            raise Failure(f"{case.id}: 信号の後に、子のプロセスグループが残っている")
        except ProcessLookupError:
            pass
    finally:
        try:
            os.killpg(process.pid, 9)  # 子を残さない
        except OSError:
            pass
    return process.returncode, out, err


def judge_signal(case, old, new):
    name = case.signal[0]
    if case.signal[3]:
        # 受け継いで無視した SIGINT は、効かない。全バイトで比べ、正常に終わること。
        if old != new:
            raise Failure(f"無視した SIGINT の後の結果が違う: 旧 {old!r}・新 {new!r}")
        if old[0] < 0:
            raise Failure(f"無視したはずの SIGINT で終わった: {old[0]}")
        return
    if name == "TERM":
        if old != new:
            raise Failure(f"SIGTERM は全バイトで比べる: 旧 {old!r}・新 {new!r}")
        if old[0] != -15:
            raise Failure(f"SIGTERM で終わっていない: {old[0]}")
        return
    # SIGINT は、信号で終わること・終了値・最後の行・それまでの標準出力を比べる(呼出履歴の途中の行は比べない)。
    for label, result in (("旧", old), ("新", new)):
        lines = [line for line in result[2].split(b"\n") if line]
        if result[0] != -2:
            raise Failure(f"{label}: SIGINT で終わっていない(終了値 {result[0]})")
        if not lines or lines[-1] != b"KeyboardInterrupt":
            raise Failure(f"{label}: 最後の行が KeyboardInterrupt でない: {result[2][-80:]!r}")
    if old[1] != new[1]:
        raise Failure(f"それまでの標準出力が違う: 旧 {old[1]!r}・新 {new[1]!r}")


def run_one_case(case, env, run_dir, binary, mapper, shim, work, mismatched, charset_expectations, charset_record_out,
                 closed_expectations=None, closed_record_out=None):
    """1 つの case を経路ごとに流して比べる。入力の無変更の検査は、呼出側がどの経路にも同じに当てる。"""
    script = mapper.script_path
    if "closed" in (case.stdin, case.stdout, case.stderr):
        return run_closed_case(case, env, run_dir, binary, mapper, closed_expectations, closed_record_out)
    if case.signal is not None:
        old = run_child(legacy_command(case), case, env, run_dir)
        new = run_child(rust_command(binary, case), case, env, run_dir)
        judge_signal(case, old, new)
        return {}
    if case.charset is not None:
        run_charset_case(case, env, run_dir, binary, charset_expectations, charset_record_out, mapper)
        return {}
    if case.fault is not None:
        return run_fault_case(case, env, run_dir, binary, mapper, shim, work)
    if case.race is not None:
        build, expected = case.race
        outputs = []
        for who in ("legacy", "rust"):
            directory = Path(work) / f"race-{who}"
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir()
            argv, extra = build(directory.resolve())
            variant = Case(case.id, case.helper, argv, env=dict(case.env, **extra), stdin=case.stdin)
            variant_env = dict(child_env(variant))
            if who == "legacy":
                variant_env.update(shim_env(shim))
                outputs.append(run_child(legacy_command(variant), variant, variant_env, directory))
            else:
                outputs.append(run_child(rust_command(binary, variant), variant, variant_env, directory))
        old, new = outputs
        if expected.encode() not in old[1] + old[2]:
            raise Failure(f"競合が旧版に起きていない(期待した出力が無い): {expected!r}・実際 {old!r}")
        if old != new:
            raise Failure(f"競合の結果が違う:\n旧: {old!r}\n新: {new!r}")
        return {}
    legacy_env = env
    if case.preload:
        env = dict(env, **case.preload)
        legacy_env = dict(env, **shim_env(shim))
    old = run_child(legacy_command(case), case, legacy_env, run_dir)
    new = run_child(rust_command(binary, case), case, env, run_dir)
    if case.judge is not None:
        case.judge(old)
        case.judge(new)
    mapped_stderr, _ = mapper.apply(old[2], case.frames)
    if case.frames is None:
        # 固定期待のフレームが無い case では、旧版の標準エラーにパスが出ていないこと。
        lines, frames = mapper.parse(old[2])
        if frames:
            raise Failure("固定期待の無い case の旧版の標準エラーに Traceback がある(固定期待を足す)")
    verdict = {}
    if case.thread and case.stderr != "dir":
        mapped_stderr = normalize_thread(mapped_stderr)
        new = (new[0], new[1], normalize_thread(new[2]))
    # 標準出力・標準エラーが directory の fd のときは、バイト列を捕捉できない。空の観測に読み替えず、明示して比べない。
    notes = []
    if case.stdout == "dir":
        notes.append(f"{case.id}: 標準出力が directory(捕捉不能。終了値と捕捉できる出力だけを比べた)")
        old, new = (old[0], b"", old[2]), (new[0], b"", new[2])
    if case.stderr == "dir":
        notes.append(f"{case.id}: 標準エラーが directory(捕捉不能。終了値と捕捉できる出力だけを比べた)")
        mapped_stderr, new = b"", (new[0], new[1], b"")
    if notes:
        verdict["uncapturable"] = notes
    if old[1] != new[1]:
        raise Failure(f"標準出力が違う:\n旧: {old[1]!r}\n新: {new[1]!r}")
    if old[0] != new[0]:
        raise Failure(f"終了値が違う: 旧 {old[0]}・新 {new[0]}")
    if mapped_stderr != new[2]:
        raise Failure(f"標準エラーが違う:\n旧: {mapped_stderr!r}\n新: {new[2]!r}")
    return verdict


def feed_fifo(path, content, stop):
    """FIFO の読み手が開くまで O_NONBLOCK で待ってから書く(読み手が開くと ENXIO が止まる)。"""
    deadline = time.monotonic() + CHILD_TIMEOUT
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            stop.wait(0.005)
            continue
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return


RECENT = []  # この case で流した子の出力((終了値, 標準出力, 標準エラー))。strerror の表の照合に使う


def run_child(argv, case, env, cwd):
    result = run_child_with_fifo(argv, case, env, cwd)
    RECENT.append(result)
    return result


def run_child_with_fifo(argv, case, env, cwd):
    if case.fifo is not None:
        import threading
        stop = threading.Event()
        target = os.path.join(cwd, case.fifo[0]) if not os.path.isabs(case.fifo[0]) else case.fifo[0]
        feeder = threading.Thread(target=feed_fifo, args=(target, case.fifo[1], stop))
        feeder.start()
        try:
            return run_child_inner(argv, case, env, cwd)
        finally:
            stop.set()
            feeder.join(timeout=CHILD_TIMEOUT)
    return run_child_inner(argv, case, env, cwd)


def run_child_inner(argv, case, env, cwd):
    if case.pty is not None:
        return run_pty(argv, case, env, cwd)
    if case.signal is not None:
        return run_signal(argv, case, env, cwd)
    stdout = subprocess.PIPE
    stderr = subprocess.PIPE
    handles = []
    closes = []
    try:
        if case.stdout == "closed":
            stdout = None
            closes.append(1)
        elif case.stdout == "null":
            stdout = subprocess.DEVNULL
        if case.stderr == "closed":
            stderr = None
            closes.append(2)
        elif case.stderr == "null":
            stderr = subprocess.DEVNULL
        if case.stdout == "full":
            stdout = open("/dev/full", "wb")
            handles.append(stdout)
        elif case.stdout == "dir":
            stdout = os.open(cwd, os.O_RDONLY)
            handles.append(FdCloser(stdout))
        if case.stderr == "full":
            stderr = open("/dev/full", "wb")
            handles.append(stderr)
        elif case.stderr == "dir":
            stderr = os.open(cwd, os.O_RDONLY)
            handles.append(FdCloser(stderr))
        stdin_arg = {"input": case.stdin}
        if case.stdin == "closed":
            stdin_arg = {"stdin": None}
            closes.append(0)
        elif case.stdin == "null":
            stdin_arg = {"stdin": subprocess.DEVNULL}
        elif isinstance(case.stdin, tuple) and case.stdin[0] == "eagain":
            read_end, write_end = os.pipe()
            os.set_blocking(read_end, False)
            if case.stdin[1]:
                os.write(write_end, case.stdin[1])
            handles.append(FdCloser(read_end))
            handles.append(FdCloser(write_end))
            stdin_arg = {"stdin": read_end}
        elif case.stdin == "dir":
            stdin_fd = os.open(cwd, os.O_RDONLY)
            handles.append(FdCloser(stdin_fd))
            stdin_arg = {"stdin": stdin_fd}
        try:
            if closes:
                stdin_arg["preexec_fn"] = lambda: [os.close(fd) for fd in closes]
            completed = subprocess.run(argv, stdout=stdout, stderr=stderr, env=env, cwd=cwd,
                                       timeout=CHILD_TIMEOUT, check=False, **stdin_arg)
        except subprocess.TimeoutExpired:
            raise Failure(f"{case.id}: 時間切れ({CHILD_TIMEOUT} 秒): {argv[:3]}")
        return completed.returncode, completed.stdout or b"", completed.stderr or b""
    finally:
        for handle in handles:
            handle.close()


THREAD_LINE = re.compile(rb"^Current thread 0x([0-9a-f]{16}) \(most recent call first\):$")


def normalize_thread(stderr):
    """`Current thread` の行を、位置と形式を検査して値を伏せる。ちょうど 1 回、`<no Python frame>` の前に出ること。"""
    lines = stderr.split(b"\n")
    hits = [i for i, line in enumerate(lines) if line.startswith(b"Current thread")]
    if len(hits) != 1:
        raise Failure(f"Current thread の行が 1 回でない: {len(hits)} 回")
    at = hits[0]
    if not THREAD_LINE.match(lines[at]):
        raise Failure(f"Current thread の行の形式が違う: {lines[at]!r}")
    if lines[at + 1:at + 2] != [b"  <no Python frame>"] or lines[at + 2:at + 3] != [b""] or len(lines) != at + 3:
        raise Failure("Current thread の行の後の形が違う")
    if at < 1 or lines[at - 1] != b"":
        raise Failure("Current thread の行の前の空行が無い")
    lines[at] = b"Current thread 0x<TID> (most recent call first):"
    return b"\n".join(lines)


def case_argv(case):
    return case.binary_argv if case.binary_argv is not None else [b(a) for a in case.argv]


def legacy_command(case):
    script = SCRIPTS[case.helper]
    argv = case_argv(case)
    flags = [] if case.bytecode else ["-B"]  # bytecode の case は -B を外す(旧スクリプト自身が __pycache__ を作らないことを確かめる)
    if case.helper in HELD_CODE:
        return [sys.executable] + flags + ["-c", script.read_text(encoding="utf-8")] + argv
    return [sys.executable] + flags + [str(script)] + argv


def rust_command(binary, case):
    return [str(binary), case.helper] + case_argv(case)


# ---------------------------------------------------------------- パスの対応づけ(旧版の実行環境のパス)

FRAME_RE = re.compile(rb'^  File "(.*)", line (\d+), in (.*)$')
# 故障でソースの行の末尾の LF が通らなかったとき、次の Frame の行はソースの行の続きに付く(その出力だけで許す)。
GLUED_FRAME_RE = re.compile(rb'^((?:    .*)?)  File "(.*)", line (\d+), in (.*)$')
HEADER = b"Traceback (most recent call last):"
CHAIN = (b"During handling of the above exception, another exception occurred:",
         b"The above exception was the direct cause of the following exception:")


def eol(line):
    """行末の CR(疑似端末の CRLF)を、書き換えた行へ引き継ぐ。"""
    return b"\r" if line.endswith(b"\r") else b""


class Mapper:
    def __init__(self, script, stdlib_dirs):
        # 故障で直前の書込み(末尾の LF)が通らなかったとき、見出しの行は前の行の続きに付く。その出力だけ、行末の見出しも許す。
        self.glued_header = False
        self.script_path = Path(script)
        self.script = os.fsencode(script)
        # 兄弟のスクリプトの欄: 旧版と同じ求め方(起動したパスのリンクを解決した親のディレクトリと、ファイル名)で求めた
        # 絶対パスに、文字列で一致するものだけ。candidate-keys.py が読む resolve-task-dir.py・task-digest.py。
        self.siblings = {}
        if Path(script).name == "candidate-keys.py":
            parent = os.fsencode(os.path.realpath(Path(script).parent))
            for name in ("resolve-task-dir.py", "task-digest.py"):
                self.siblings[parent + b"/" + name.encode()] = name.encode()
        self.stdlib = [os.fsencode(d) for d in stdlib_dirs]

    def fixed(self, path):
        if path == self.script:
            return os.path.basename(path)
        if path in self.siblings:
            return self.siblings[path]
        for base in self.stdlib:
            prefix = base.rstrip(b"/") + b"/"
            if path.startswith(prefix):
                rel = path[len(prefix):]
                parts = rel.split(b"/")
                if b".." in parts or b"" in parts:
                    return None
                name = rel.decode("utf-8", "replace")
                if name in UPSTREAM_SHA256:
                    return rel
        return None

    def parse(self, stderr):
        """構造で解析して、(行の列, [(行の位置, パス, 行, 関数)]) を返す。"""
        lines = stderr.split(b"\n")
        frames = []
        self.glue_prefix = {}
        state = "start"
        at = 0
        sequence = -1
        while at < len(lines):
            line = lines[at].rstrip(b"\r")  # 疑似端末の CRLF も同じ規則で解析する
            if state in ("start", "between") and (line == HEADER or (self.glued_header and line.endswith(HEADER))):
                state = "frames"
                sequence += 1
            elif state == "frames":
                match = FRAME_RE.match(line)
                if match is None and self.glued_header:
                    glued = GLUED_FRAME_RE.match(line)
                    if glued:
                        self.glue_prefix[at] = glued.group(1)
                        match = re.match(rb'^(.*)$', b"")  # 下で glued の値を使う
                        frames.append((at, glued.group(2), int(glued.group(3)), glued.group(4), sequence))
                        at += 1
                        continue
                if match:
                    frames.append((at, match.group(1), int(match.group(2)), match.group(3), sequence))
                elif line.startswith(b"    ") or line.startswith(b"  [Previous line repeated"):
                    pass
                else:
                    state = "exception"
                    continue  # 同じ行を、例外の行(または連鎖の区切り)として読み直す
            elif state == "exception":
                if (at + 2 < len(lines) and line == b"" and lines[at + 1] in CHAIN and lines[at + 2] == b""
                        and lines[at + 3:at + 4] == [HEADER]):
                    state = "between"
                    at += 2
            at += 1
        return lines, frames

    def map_expected(self, stderr, expected):
        """故障で途中までしか出なかった出力: 構造で解析して、固定期待の Frame(固定名・行・関数が期待のどれかと同じもの)の
        File 欄だけを固定名にする。期待に無い Frame・未知のパス・偽の File 行は、そのまま残す(数・順は `apply` が検査する)。"""
        allowed = {(name.encode(), line, func.encode()) for group in (expected or []) for name, line, func in group}
        lines, frames = self.parse(stderr)
        for at, path, line, func, _ in frames:
            fixed = path if path.startswith(b"<") and path.endswith(b">") else self.fixed(path)
            if fixed is not None and (fixed, line, func) in allowed:
                lines[at] = self.glue_prefix.get(at, b"") + b'  File "' + fixed + b'", line ' + str(line).encode() + b", in " + func + eol(lines[at])
        return b"\n".join(lines)

    def apply(self, stderr, expected):
        """固定期待を持つフレームの File 欄だけを固定名へ直す。期待の数・順・関数・行を検査する。"""
        lines, frames = self.parse(stderr)
        if expected is None:
            return stderr, frames
        groups = {}
        for item in frames:
            groups.setdefault(item[4], []).append(item)
        if len(groups) != len(expected):
            raise Failure(f"Traceback の数が期待と違う: 期待 {len(expected)}・実際 {len(groups)}")
        for sequence, want in enumerate(expected):
            got = groups.get(sequence, [])
            if len(got) != len(want):
                raise Failure(f"フレーム数が違う: 期待 {len(want)}・実際 {len(got)}")
            for (at, path, line, func, _), (name, want_line, want_func) in zip(got, want):
                fixed = path if (name.startswith("<") and path == name.encode()) else self.fixed(path)
                if fixed is None or fixed != name.encode():
                    raise Failure(f"フレームの File 欄が固定名 {name!r} に対応づかない: {path!r}")
                if line != want_line or func != want_func.encode():
                    raise Failure(f"フレームの行・関数が違う: 期待 {name}:{want_line}:{want_func}・実際 {line}:{func!r}")
                lines[at] = self.glue_prefix.get(at, b"") + b'  File "' + fixed + b'", line ' + str(line).encode() + b", in " + func + eol(lines[at])
        return b"\n".join(lines), frames


def python_layout():
    code = (
        "import os, sys, sysconfig, json\n"
        "print(json.dumps({'os': os.path.dirname(os.__file__), 'stdlib': sysconfig.get_path('stdlib'),"
        " 'platstdlib': sysconfig.get_path('platstdlib'), 'version': sys.version.split()[0]}))\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, check=True).stdout
    data = json.loads(out)
    dirs = []
    for key in ("os", "stdlib", "platstdlib"):
        for path in (data[key], os.path.realpath(data[key])):
            if path not in dirs:
                dirs.append(path)
    return data, dirs


def check_site_free():
    code = (
        "import site, glob, os\n"
        "found = []\n"
        "for mod in ('sitecustomize', 'usercustomize'):\n"
        "    try:\n"
        "        __import__(mod); found.append(mod)\n"
        "    except ImportError: pass\n"
        "user = site.getusersitepackages() if site.ENABLE_USER_SITE else ''\n"
        "pth = glob.glob(os.path.join(user, '*.pth')) if user else []\n"
        "print(repr((found, pth)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True,
                         env={**{k: v for k, v in os.environ.items() if k not in STRIPPED_ENV},
                              "PYTHONNOUSERSITE": "1"}).stdout
    found, pth = eval(out.decode())
    if found or pth:
        raise Failure(f"sitecustomize/usercustomize/利用者の .pth が有る: {found} {pth}")
    return found, pth


def check_env_lists():
    """旧版の実行ファイル(と libpython)に現れる `PYTHON*` の名前が、固定の 3 つの一覧のどれかに入ることを確かめる。"""
    names = set()
    candidates = [Path(sys.executable).resolve()] + sorted(Path(sys.executable).resolve().parents[1].glob("lib/libpython3.10*"))
    for path in candidates:
        if path.is_file():
            names |= {m.decode() for m in re.findall(rb"PYTHON[A-Z0-9_]{2,}", path.read_bytes())}
    known = set(STRIPPED_ENV) | set(KEPT_ENV) | set(COMPARER_ENV)
    unknown = sorted(names - known - {"PYTHONAPI"})  # PYTHONAPI は `PYTHONAPI_VERSION` などの記号名の断片
    if unknown:
        raise Failure(f"旧版の実行ファイルが読む環境変数が、固定の一覧に無い: {unknown}(一覧を CPython の読取りから見直す)")
    return sorted(names)


def record_rlimits():
    """処理系の資源の上限は設定せず、受け継いだ値を記録する(2026-11-12 00:28・00:42 の決定)。"""
    import resource
    parts = []
    for name in ("RLIMIT_AS", "RLIMIT_NOFILE", "RLIMIT_CPU", "RLIMIT_STACK", "RLIMIT_FSIZE"):
        soft, hard = resource.getrlimit(getattr(resource, name))
        show = lambda v: "無制限" if v == resource.RLIM_INFINITY else str(v)
        parts.append(f"{name}={show(soft)}/{show(hard)}")
    return " ".join(parts)


def strerror_table_matches(binary):
    """比較機の旧版の `os.strerror` の文が、Rust の表と一致するか(一致しなければ strerror を含む case を失敗にする)。"""
    numbers = list(range(-1, 300)) + [9999, 100000]
    text = subprocess.run([sys.executable, "-I", "-c", "import os,sys\nfor n in %r: print(n, os.strerror(n), sep='\\t')" % numbers],
                          capture_output=True, check=True, env=dict(os.environ, PYTHONUTF8="1")).stdout
    mine = subprocess.run([str(binary), "strerror"], capture_output=True, timeout=60).stdout
    return text == mine


STRERROR_MARK = re.compile(rb"\[Errno -?\d+\]|\((?:-?\d+), '")


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- 入力の無変更


def snapshot(*roots):
    """入力の状態: 種類・mode・大きさ・dev・ino・mtime_ns・ctime_ns・リンクの字面・内容の SHA-256。
    読取り時刻(atime)は除く。__pycache__ ができたらそれも記録される。"""
    state = {}
    for root in roots:
        root = Path(root)
        for path in sorted(root.rglob("*")):
            key = str(path)
            try:
                info = path.lstat()
            except OSError as error:
                state[key] = ("err", error.errno)
                continue
            if stat_module.S_ISFIFO(info.st_mode):
                # FIFO は読み書きで時刻が変わる(読み手・書き手の動作)。種類・mode・dev・ino だけを記録する。
                state[key] = (info.st_mode, info.st_dev, info.st_ino, "fifo")
                continue
            target = os.readlink(path) if path.is_symlink() else None
            content = ""
            if path.is_file() and not path.is_symlink():
                try:
                    content = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError as error:
                    content = f"err:{error.errno}"
            state[key] = (info.st_mode, info.st_size, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, target,
                          content, "pycache" if path.name == "__pycache__" else "")
    return state


def describe_snapshot_change(before, after):
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return ", ".join(changed[:5])


# ---------------------------------------------------------------- 実行本体


def run_selftest(options, binary, test_file=None):
    """比較器の自己検査(`test_r4a_rest.py`)を、比較の前に流す。落ちたら比較を合格にしない(R4B・R4R と同じ)。
    自己検査の中の子の比較器は、環境変数で自己検査を流さない(再帰しない)。"""
    if options.no_selftest or options.list_cases or options.write_required or os.environ.get("R4A_REST_IN_SELFTEST"):
        return
    import importlib.util
    import unittest
    os.environ["R4A_REST_IN_SELFTEST"] = "1"
    os.environ["R4A_REST_FIXTURE"] = str(binary)
    path = test_file or Path(__file__).with_name("test_r4a_rest.py")
    spec = importlib.util.spec_from_file_location("r4a_rest_comparator_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromModule(module))
    os.environ.pop("R4A_REST_IN_SELFTEST", None)
    if not result.wasSuccessful() or result.testsRun == 0:
        raise SystemExit(f"比較器の自己検査が失敗した({result.testsRun} 件・失敗 {len(result.failures)}・エラー {len(result.errors)})。比較は流さない")
    print(f"比較器の自己検査: {result.testsRun} 件合格 / {time.monotonic() - started:.1f} 秒")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--only", default="")
    parser.add_argument("--list-cases", action="store_true")
    parser.add_argument("--strict-pending", action="store_true",
                        help="接続待ちの case も失敗に数える(接続の後の最終確認で使う)")
    parser.add_argument("--record-closed", action="store_true",
                        help="起動時に閉じた fd の旧版の結果を closed_fd_expectations.json へ記録する(旧版を取り換えたときだけ人が実行する)")
    parser.add_argument("--record-charset", action="store_true",
                        help="設定ありの旧版の結果を charset_expectations.json へ記録する(旧版を取り換えたときだけ人が実行する)")
    parser.add_argument("--write-required", action="store_true",
                        help="必須 case の一覧を書き出す(case を足したときだけ人が実行して差分を確かめる)")
    parser.add_argument("--no-selftest", action="store_true",
                        help="比較器の自己検査を流さない(同じ比較器を 2 回目に流すとき。自己検査の中の子の比較は、環境変数で自動に流さない)")
    options = parser.parse_args()
    # 比較器と、その子(旧版・Rust・補助のコマンド)は、使い捨ての一時フォルダの中で動かす。リポジトリに何も残さない。
    scratch = tempfile.TemporaryDirectory(prefix="r4a-rest-scratch-")
    os.chdir(scratch.name)

    if sys.version_info[:2] != (3, 10):
        raise SystemExit(f"旧版は Python 3.10 系で動かす: {sys.version}")
    if os.geteuid() == 0:
        raise SystemExit("root では実行できない(非 root の通常利用者で実行する)")
    binary = options.fixture
    if str(binary) == "" or not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit(f"検査用の実行ファイルが絶対パスの実行可能なファイルでない: {binary!r}")

    run_selftest(options, binary)

    known = ["resolve-task-store", "candidate-keys", "host-argv", "loop-task-text", "loop-child-output"]
    helpers = [h for h in options.only.split(",") if h] if options.only else list(known)
    unknown = [h for h in helpers if h not in known]
    if unknown:
        raise SystemExit(f"未知の補助: {unknown}")

    cases = all_cases(helpers)
    if not cases and not options.write_required:
        raise SystemExit("比べる case が 0 件(0 件を成功にしない)")
    if options.write_required:
        every = all_cases(known)
        (FIXTURES / "required_cases.json").write_text(
            json.dumps({"cases": {c.id: c.helper for c in sorted(every, key=lambda c: c.id)},
                        "requires_shim": sorted(c.id for c in every if needs_shim(c)),
                        "requires_linux": sorted(c.id for c in every if needs_linux(c))},
                       ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        print(f"書き出した case: {len(every)}")
        return
    required = json.loads((FIXTURES / "required_cases.json").read_text(encoding="utf-8"))
    if options.list_cases:
        for case in cases:
            print(case.id)
        return

    # 起動前: 必須 case の集合と重複の検査。
    ids = [c.id for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SystemExit(f"case ID が重複している: {duplicates}")
    wanted = sorted(i for i, helper in required["cases"].items() if helper in helpers)
    if sorted(ids) != wanted:
        missing = sorted(set(wanted) - set(ids))
        extra = sorted(set(ids) - set(wanted))
        raise SystemExit(f"必須 case の集合と一致しない: 欠落 {missing[:10]}・余計 {extra[:10]}")

    layout, stdlib_dirs = python_layout()
    env_names = check_env_lists()
    print(f"旧版の実行ファイルが読む環境変数 {len(env_names)} 個は、固定の一覧(外す {len(STRIPPED_ENV)}・再現 {len(KEPT_ENV)}・比較器 {len(COMPARER_ENV)})に入る")
    print("受け継いだ資源の上限(設定しない): " + record_rlimits())
    strerror_ok = strerror_table_matches(binary)
    print("strerror の表: " + ("比較機の旧版の文と一致" if strerror_ok else "比較機の旧版の文と違う(strerror を含む case を失敗にする)"))
    found_custom, found_pth = check_site_free()
    print(f"sitecustomize・usercustomize・利用者の .pth: 無い(PYTHONNOUSERSITE=1 の下で確かめた: {len(found_custom)} 個・{len(found_pth)} 個)")
    stdlib_root = Path(layout["os"])
    hashes = {}
    mismatched = []
    for name, expect in UPSTREAM_SHA256.items():
        actual = sha_of(stdlib_root / name)
        hashes[name] = actual
        if actual != expect:
            mismatched.append(name)
    print(f"旧版の Python: {layout['version']} ({sys.executable})")
    print("標準ライブラリの SHA-256: " + json.dumps(hashes, sort_keys=True))
    print(f"導入先の置き場: {sorted(set(stdlib_dirs))}")
    print(f"本家と違うファイル: {mismatched or 'なし'}")

    started = time.monotonic()
    passed = 0
    failures = []
    executed = []
    not_run = []
    pending = []
    uncapturable = []
    cut_flags = []
    charset_expectations = json.loads(CHARSET_FILE.read_text(encoding="utf-8")) if CHARSET_FILE.exists() else {}
    charset_record_out = {} if options.record_charset else None
    closed_expectations = json.loads(CLOSED_FILE.read_text(encoding="utf-8")) if CLOSED_FILE.exists() else {}
    closed_record_out = {} if options.record_closed else None
    with tempfile.TemporaryDirectory(prefix="r4a-rest-compare-") as work:
        work = Path(work)
        shim = build_shim(work)
        fixtures_dir = work / "inputs"
        fixtures_dir.mkdir()
        (fixtures_dir / "sentinel.txt").write_bytes(b"unchanged\n")
        for case in cases:
            if time.monotonic() - started > TOTAL_TIMEOUT:
                raise SystemExit(f"全体の上限 {TOTAL_TIMEOUT} 秒を超えた")
            executed.append(case.id)
            try:
                env = child_env(case)
                if case.setup is not None:
                    case.setup(fixtures_dir)
                run_dir = fixtures_dir
                if case.build is not None:
                    run_dir = (work / f"case-{len(executed)}")
                    run_dir.mkdir()
                    argv_list, case.stdin = case.build(run_dir.resolve())
                    case.argv = argv_list
                    run_dir = Path(case.cwd) if case.cwd else run_dir
                    case_dir = work / f"case-{len(executed)}"
                else:
                    case_dir = fixtures_dir
                script = SCRIPTS[case.helper]
                mapper = Mapper(script, stdlib_dirs)
                # 本家 3.10.11 の標準ライブラリでなければ、導入先のフレームを含む case はどの経路でも失敗にする。
                needs_stdlib = any(name != script.name and not name.startswith("<")
                                   for group in (case.frames or []) for name, _, _ in group)
                if needs_stdlib and mismatched:
                    raise Failure(f"導入先のフレームを含むが、本家と違う標準ライブラリを使っている: {mismatched}")
                if needs_linux(case) and sys.platform != "linux":
                    not_run.append(case.id)
                    continue
                if needs_shim(case) and shim is None:
                    if sys.platform == "linux":
                        raise Failure("Linux で故障の差込みライブラリを作れない・効かない(cc が無い・ビルドの失敗・差込みの無効)。未実施にしない")
                    not_run.append(case.id)
                    continue
                before = snapshot(case_dir, SCRIPTS[case.helper].parent)
                RECENT.clear()
                try:
                    verdict = run_one_case(case, env, run_dir, binary, mapper, shim, work, mismatched,
                                           charset_expectations, charset_record_out, closed_expectations, closed_record_out)
                except Unavailable as reason:
                    not_run.append(f"{case.id}: {reason}")
                    continue
                if not strerror_ok and any(STRERROR_MARK.search(out[1] + out[2]) for out in RECENT):
                    raise Failure("strerror の表が比較機の旧版の文と違うので、errno の文を含むこの case は失敗にする")
                after = snapshot(case_dir, SCRIPTS[case.helper].parent)
                if before != after:
                    raise Failure("入力・旧スクリプトの置き場が変わった(または __pycache__ ができた): "
                                  + describe_snapshot_change(before, after))
                uncapturable.extend(verdict.get("uncapturable", []))
                cut_flags.extend(verdict.get("cut_inside_field", []))
                passed += 1
            except Failure as error:
                if case.pending:
                    pending.append(f"{case.id}: {error}")
                else:
                    failures.append(f"{case.id}: {error}")
            except OSError as error:
                # case の中の OS の失敗(例: 装置が無い)は、その case の失敗にして、ほかの case を流し続ける。
                failures.append(f"{case.id}: OS の失敗: {error!r}")
    if sorted(executed) != sorted(ids):
        failures.append("実行した case が必須の集合と違う")
    if closed_record_out is not None:
        CLOSED_FILE.write_text(json.dumps(closed_record_out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"記録した起動時に閉じた fd の case: {len(closed_record_out)}")
        return
    if charset_record_out is not None:
        CHARSET_FILE.write_text(json.dumps(charset_record_out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"記録した文字コード設定の case: {len(charset_record_out)}")
        return
    elapsed = time.monotonic() - started
    partial = "" if sorted(helpers) == sorted(known) else "(部分比較: 全件合格ではない)"
    if pending:
        print(f"接続待ち: {len(pending)} 件(Rust 側が platform と接続するまで一致しない。成功には数えない)")
        for line in pending[:60]:
            print("  接続待ち: " + line[:200])
        if options.strict_pending:
            failures.extend(pending)
    # 未実施: Linux では 0 件でなければならない。Linux 以外は、必須一覧の requires_shim と一致しなければならない。
    shim_ids = sorted(c.id for c in cases if c.fault is not None or c.preload or c.race is not None)
    unavailable = [line for line in not_run if ": " in line]
    # locale の有無などで確かめられない case(Unavailable)の集合も、OS ごとに固定した一覧と照らす(今は全 OS で空)。
    unexpected_unavailable = sorted(line.split(": ", 1)[0] for line in unavailable
                                    if line.split(": ", 1)[0] not in UNAVAILABLE_BY_OS.get(sys.platform, []))
    if unexpected_unavailable:
        failures.append(f"この OS の固定の一覧に無い未実施(locale の有無が記録と違う)がある: {unexpected_unavailable[:5]}"
                        "(別の機械では、期待を記録し直すか、OS ごとの一覧に足す)")
    plain_not_run = sorted(line for line in not_run if ": " not in line)
    fixed_unrun = False  # この OS の固定の一覧のとおりの未実施だけ(終了 2 にしない)
    id_set = set(ids)
    listed_linux = sorted(i for i in required.get("requires_linux", []) if i in id_set)
    if sys.platform == "linux":
        if plain_not_run:
            failures.append(f"Linux で未実施の case がある: {plain_not_run[:5]}")
    else:
        listed = set(listed_linux)
        if shim is None:
            listed |= {i for i in required.get("requires_shim", []) if i in id_set}
        if plain_not_run != sorted(listed):
            failures.append(f"未実施の集合が、この OS の固定の一覧(requires_linux・requires_shim)と違う: 未実施 {len(plain_not_run)} 件・一覧 {len(listed)} 件")
        else:
            fixed_unrun = not unavailable
    if not_run:
        print(f"未実施: {len(not_run)} 件(この機械・OS で確かめられない。成功には数えない)")
        for line in unavailable[:10]:
            print("  未実施: " + line[:200])
    if uncapturable:
        print(f"捕捉不能な出力(directory の fd): {len(uncapturable)} 件。空の観測に読み替えず、捕捉できる出力と終了値だけを比べた")
    # 故障の case: 合計の上限が、固定名の欄の途中・欄の外・旧版だけが切れる範囲の各位置に来ていること。
    positions = {item[1] for item in cut_flags}
    if any(item[2] for item in cut_flags):
        positions.add("legacy-only")
    if "fault" in " ".join(ids) and "resolve-task-store" in helpers and not options.only:
        for needed in ("inside", "outside", "legacy-only"):
            if needed not in positions:
                failures.append(f"故障の case に、上限が「{needed}」の位置に来るものが無い")
    if not_run or pending or partial:
        marker = []
        if partial:
            marker.append("部分比較")
        if pending:
            marker.append(f"接続待ち {len(pending)} 件")
        if not_run:
            marker.append(f"未実施 {len(not_run)} 件")
        partial = "(" + "・".join(marker) + ": 全件合格ではない)"
    print(f"比べた case: {passed}/{len(cases)} {partial} / {elapsed:.1f} 秒")
    if failures:
        for line in failures[:40]:
            print("不一致: " + line, file=sys.stderr)
        print(f"不一致 {len(failures)} 件", file=sys.stderr)
        raise SystemExit(1)
    if partial:
        print("結果: 未完" + partial)
        if pending or (not_run and not fixed_unrun):
            raise SystemExit(2)  # 接続待ち・未実施が残るときは、合格(終了 0)にしない
        return  # `--only` の部分比較だけ: 比べた分は不一致が無い(全件合格とは言わない)
    print("結果: 合格")


if __name__ == "__main__":
    main()
