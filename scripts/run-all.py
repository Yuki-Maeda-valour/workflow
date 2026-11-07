#!/usr/bin/env python3
"""全回帰テスト(回帰テストをすべて流すこと)を、1 つのコマンドと 1 つの共通の枠で流す。

使い方(リポジトリ直下で実行する):
  python3 -B scripts/run-all.py                         # 全部を流す
  python3 -B scripts/run-all.py -j 8                    # 同時に流す枠の数(既定は使える CPU の数)
  python3 -B scripts/run-all.py --log-dir DIR           # 全部の単位の出力を DIR/<鍵>.log に残す
  python3 -B scripts/run-all.py --weights-file PATH     # 重みの記録の置き場(既定は ~/.cache/workflow-run-all/weights.json)
  python3 -B scripts/run-all.py --plan                  # 流さずに、始める順を出して終わる
  python3 -B scripts/run-all.py --reuse                 # 入力が前の合格と同じ単位を流さず、前の結果を使う(容器の中だけ)
  python3 -B scripts/run-all.py --reuse-file PATH       # 使い回しの記録の置き場(既定は ~/.cache/workflow-run-all/results.json)

- 流す単位: unittest のファイルとシャード(run-tests.py)・loop-selftest の節と節の外だけ(run-loop-selftest.py)・
  implement-guard-selftest.sh の単位・diff-snapshot-selftest.sh。review-agent-selftest.sh と
  implement-agent-selftest.sh は、待ちが主なので枠の外で、最初に始める。
- 枠の単位は、重みの大きい順に、1 つの共通の枠で流す。重みは、重みの記録 → 各ランナーの表 → 既定の 200 秒の順に引く。
- 部ごとに件数を照らし合わせる(部の集計は、2 つのランナーの関数を使う)。
- 結果の使い回しと記録は、容器の入口(run-all-container.sh)が正しい形の RUN_ALL_IMAGE を渡したときだけ働く。
  容器の中の回は、--reuse が無くても、新しく流した単位の結果を記録する(次の --reuse の回が使う)。
- 終了コード: 合格なら 0。ほかは 1。引数の誤りは 2。止めたときは 128 + シグナルの番号(結果の行は出さない)。
- 詳しい決まりは docs/testing.md を見る。
"""

import argparse
import collections
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ほかのファイルを読み込む前に、__pycache__ を書かないようにする。
sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import run_units  # noqa: E402


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_tests = load_module("run_tests", "run-tests.py")
run_loop = load_module("run_loop_selftest", "run-loop-selftest.py")

SKILLS = REPO / "plugins" / "dev-workflow" / "skills"
GUARD = SKILLS / "do-task" / "scripts" / "implement-guard-selftest.sh"
AGENT = SKILLS / "do-task" / "scripts" / "implement-agent-selftest.sh"
REVIEW = SKILLS / "do-task" / "scripts" / "review-agent-selftest.sh"
DIFF_SNAPSHOT = SKILLS / "do-task" / "scripts" / "diff-snapshot-selftest.sh"

# 既定の重みの表(秒。2026-11-06 の実測。Ubuntu 22.04 の容器・git 2.56.0・16 CPU)。0 秒は 1。
# unittest と loop の節は、2 つのランナーの WEIGHTS を使う。
# implement-guard の群(--list が無い版で使う頭文字の単位)と、--list の単位(M1〜M27・N1〜N13・L-a〜L-z)。
GUARD_WEIGHTS = {
    "A": 1, "B": 58, "C": 26, "D": 21, "E": 43, "F": 31, "G": 23, "H": 6, "I": 9, "J": 16, "K": 2,
    "L": 244, "M": 189, "N": 101, "P": 14,
    "M1": 2, "M2": 3, "M3": 16, "M4": 11, "M5": 13, "M6": 2, "M7": 7, "M8": 1, "M9": 2, "M10": 3, "M11": 5,
    "M12": 6, "M13": 1, "M14": 4, "M15": 3, "M16": 1, "M17": 2, "M18": 2, "M19": 3, "M20": 2, "M21": 3,
    "M22": 9, "M23": 14, "M24": 36, "M25": 8, "M26": 24, "M27": 5,
    "N1": 15, "N2": 7, "N3": 9, "N4": 2, "N5": 4, "N6": 18, "N7": 5, "N8": 13, "N9": 3, "N10": 3, "N11": 15,
    "N12": 7, "N13": 1,
    "L-a": 12, "L-b": 7, "L-c": 5, "L-d": 4, "L-e": 6, "L-f": 3, "L-g": 6, "L-h": 3, "L-i": 4, "L-j": 7,
    "L-k": 4, "L-l": 4, "L-m": 5, "L-n": 5, "L-o": 6, "L-p": 17, "L-q": 7, "L-r": 43, "L-s": 4, "L-t": 6,
    "L-u": 5, "L-v": 5, "L-w": 11, "L-x": 22, "L-y": 21, "L-z": 19,
}
WHOLE_WEIGHTS = {"implement-guard": 781, "diff-snapshot": 81}
LOOP_OUTSIDE_WEIGHT = 14

# loop-selftest の各プロセスに、環境に無いときだけ足す上限(秒)。
LOOP_ENV_DEFAULTS = {"LOOP_SELFTEST_TIMEOUT": "6000", "RUN_TIMEOUT": "600"}

OUTSIDE_LABELS = ("review-agent-selftest.sh", "implement-agent-selftest.sh")
GIT_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")
ALL_CASES = re.compile(r'^ALL_CASES="([^"]*)"', re.M)
LIST_MISSING = "ERROR: 不明な引数: --list"
STATE_DIR = Path("~/.local/state/dev-workflow")


# ---- 4 本の selftest の単位 ----

def relative_entry(path):
    """単位が起動するファイルの、リポジトリ直下からの相対パス(リポジトリの外なら None)。"""
    try:
        return Path(path).relative_to(REPO).as_posix()
    except ValueError:
        return None


def selftest_unit(key, label, script, args=(), in_slot=True):
    """bash の selftest を 1 つ流す単位。結果は (鍵, 終了コード, 出力, 秒, (PASS, FAIL) か None)。"""
    def run(children):
        code, output, elapsed = run_units.run_command(children, ["bash", str(script), *args])
        found = run_loop.RESULT.findall(output)
        counts = (int(found[-1][0]), int(found[-1][1])) if found else None
        return key, code, output, elapsed, counts

    weight = GUARD_WEIGHTS.get(key.partition(":")[2]) if key.startswith("implement-guard:") else WHOLE_WEIGHTS.get(key)
    return run_units.Unit(key=key, label=label, run=run, describe=run_loop.describe, in_slot=in_slot,
                          weight=weight, table_weight=weight, ident=key, entry=relative_entry(script))


# ---- implement-guard の分け方 ----

def covers(units, ids):
    """units が ids をちょうど 1 回ずつ覆うか。規則は implement-guard-selftest.sh の want() と同じ。"""
    def matches(unit, case):
        return unit == case or (len(unit) == 1 and unit != "L" and case[:1] == unit)
    return all(sum(matches(unit, case) for unit in units) == 1 for case in ids)


def read_all_cases():
    """implement-guard-selftest.sh の ALL_CASES の ID の一覧。読めなければ None。"""
    try:
        match = ALL_CASES.search(GUARD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ids = match.group(1).split() if match else []
    return ids or None


def guard_split():
    """implement-guard-selftest.sh の単位の一覧と、警告の行(無ければ None)を返す。

    - `--list` が終了コード 0 で 1 行以上を返せば、その各行を単位にする。
    - そうでなければ、ALL_CASES の頭文字の一覧にする。それも読めない・覆わないなら 1 本で流す。
    """
    reason = None
    legacy = False
    try:
        result = subprocess.run(["bash", str(GUARD), "--list"], cwd=REPO, capture_output=True, text=True,
                                errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        result = None
        reason = f"--list を流せない({exc})"
    if result is not None:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0:
            error = result.stderr.strip()
            # --list が無い版は、不明な引数として 1 行だけ出して終わる(警告は出さない)。
            legacy = error == LIST_MISSING
            first = error.splitlines()[0] if error else "標準エラーは空"
            reason = f"--list が終了コード {result.returncode} で終わった({first})"
        elif not lines:
            reason = "--list が 1 行も返さなかった"
        elif any(re.search(r"[\s,]", line) for line in lines):
            reason = "--list の行に空白か , がある"
        elif len(set(lines)) != len(lines):
            reason = "--list が同じ行を 2 つ以上返した"
        else:
            return lines, None
    ids = read_all_cases()
    if ids is None:
        return None, f"警告: implement-guard-selftest.sh を 1 本 で流した({reason}。ALL_CASES も読めない)"
    letters = list(dict.fromkeys(case[:1] for case in ids))
    if not covers(letters, ids):
        return None, f"警告: implement-guard-selftest.sh を 1 本 で流した({reason}。頭文字の一覧が ALL_CASES を覆わない)"
    return letters, None if legacy else f"警告: implement-guard-selftest.sh を 頭文字の一覧 で流した({reason})"


def guard_units():
    names, warning = guard_split()
    if names is None:
        units = [selftest_unit("implement-guard", "implement-guard-selftest.sh", GUARD)]
    else:
        units = [selftest_unit(f"implement-guard:{name}", f"implement-guard-selftest.sh --only {name}", GUARD,
                               ("--only", name)) for name in names]
    return units, warning


# ---- 単位の組み立て ----

def build_units(workdir):
    """全部の単位と、警告の行(無ければ None)を返す。loop-selftest.sh の節を読めなければ ValueError。"""
    units = []
    for unit in run_tests.build_units(run_tests.make_jobs(run_tests.resolve_files([])), workdir):
        unit.label = f"unittest {unit.label}"
        units.append(unit)
    sections = run_loop.read_sections(run_loop.SELFTEST.read_bytes())
    for unit in run_loop.build_units(sections, outside_weight=LOOP_OUTSIDE_WEIGHT, defaults=LOOP_ENV_DEFAULTS):
        unit.label = f"loop-selftest {unit.label}"
        units.append(unit)
    guards, warning = guard_units()
    units.extend(guards)
    units.append(selftest_unit("diff-snapshot", "diff-snapshot-selftest.sh", DIFF_SNAPSHOT))
    units.append(selftest_unit("review-agent", "review-agent-selftest.sh", REVIEW, in_slot=False))
    units.append(selftest_unit("implement-agent", "implement-agent-selftest.sh", AGENT, in_slot=False))
    return units, warning


def apply_weights(units, record):
    """各単位の重みと出所を、記録 → 表 → 既定の順で決める。"""
    table = {unit.key: unit.table_weight for unit in units if unit.table_weight is not None}
    for unit in units:
        unit.weight, unit.source = run_units.weight_of(unit.key, record, table)


def log_name_of(key):
    return key.replace(":", "_").replace("/", "_") + ".log"


# ---- 始める前の確かめ ----

def ensure_state_dir():
    """~/.local/state/dev-workflow が無ければ作る(終わっても消さない)。同時に動く流しが取り合わないように、先に作る。"""
    path = STATE_DIR.expanduser()
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"注意: 置き場を作れない({path}: {exc})", flush=True)
        return
    print(f"置き場を作った: {path}", flush=True)


def git_lines():
    """git の版を読み、出す行(`git: …` と、警告・注意)を返す。"""
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, errors="replace", timeout=30)
        text = result.stdout.strip()
        match = GIT_VERSION.search(text)
        if result.returncode != 0 or not match:
            raise ValueError(f"終了コード {result.returncode}: {text or result.stderr.strip()}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return [f"警告: git の版を読めない({exc})"]
    major, minor = int(match.group(1)), int(match.group(2))
    version = ".".join(part for part in match.groups() if part is not None)
    lines = [f"git: {text}"]
    if (major, minor) < (2, 42):
        lines.append(f"警告: git {version} は 2.42 より古い。loop.sh は 2.42 以上の機能を使い、2.36 より古いと "
                     f"loop 系の回帰テストが変更と関係なく落ちる。新しい git の容器(bash scripts/run-all-container.sh)で流す")
    elif (major, minor) < (2, 45):
        lines.append(f"注意: git {version} では reftable の確かめ(3 件)が skip になる(2.45 以上で流れる)")
    return lines


# ---- 集計 ----

def group_of(unit):
    return unit.key.split(":")[0]


def counts_line(name, results):
    """selftest の部の結果の行と、成功か。件数が分からなければ「不明」の行にする。"""
    counts = [result[4] for result in results]
    if not counts or any(item is None for item in counts):
        return f"結果({name}): 件数は不明(上の「異常:」を見る)"
    return f"結果({name}): PASS {sum(item[0] for item in counts)} 件 / FAIL {sum(item[1] for item in counts)} 件"


def summarize(units, results):
    """部ごとの結果の行 6 行と、異常の一覧を返す。"""
    groups = {}
    for unit, result in zip(units, results):
        groups.setdefault(group_of(unit), []).append((unit, result))
    problems = []
    lines = []

    # unittest: シャードの総数と配った件数を照らす(run-tests.py の関数)。
    unit_results = [result for _, result in groups.get("unittest", [])]
    unit_problems, totals = run_tests.summarize(unit_results)
    problems.extend(unit_problems)
    known = all(result[4] is not None for result in unit_results) and \
        not any("件数が合わない" in problem for problem in unit_problems)
    if known:
        lines.append(f"結果(run-tests.py の分): {totals['run']} 件 / 失敗 {totals['failures']} 件 / "
                     f"エラー {totals['errors']} 件 / skip {totals['skipped']} 件")
    else:
        lines.append("結果(run-tests.py の分): 件数は不明(上の「異常:」を見る)")

    # loop-selftest: 節の外の差し引きと、節ごとに 1 件以上(run-loop-selftest.py の関数)。
    loop_results = [result for _, result in groups.get("loop", [])]
    loop_problems, _, total = run_loop.problems_of(loop_results)
    problems.extend(loop_problems)
    if total is None:
        lines.append("結果(run-loop-selftest.py の分): 件数は不明(上の「異常:」を見る)")
    else:
        lines.append(f"結果(run-loop-selftest.py の分): PASS {total[0]} 件 / FAIL {total[1]} 件")

    # 4 本の selftest: 結果の行があり、FAIL 0 で終了コード 0。implement-guard は分けたとき単位ごとに 1 件以上。
    for group, name in (("review-agent", "review-agent-selftest.sh"), ("implement-agent", "implement-agent-selftest.sh"),
                        ("diff-snapshot", "diff-snapshot-selftest.sh"), ("implement-guard", "implement-guard-selftest.sh")):
        pairs = groups.get(group, [])
        for unit, result in pairs:
            _, code, output, elapsed, counts = result
            if counts is None:
                problems.append(f"{unit.label}: 結果の行が無い(終了コード {code})")
            elif code != 0 or counts[1] != 0:
                problems.append(f"{unit.label}: 失敗(終了コード {code})")
            elif unit.key.startswith("implement-guard:") and sum(counts) < 1:
                problems.append(f"{unit.label}: 確かめが 1 件も流れていない(単位の名前が合わなかった)")
        lines.append(counts_line(name, [result for _, result in pairs]))
    return lines, problems


# ---- 結果の使い回し(容器の中だけ) ----
#
# 鍵(使い回してよいかを決める値)は、道具の版(tools)・環境変数(env)・単位の範囲のファイル(files)と、
# 記録の形の版・単位の鍵の名前をまとめた input。1 文字でも変われば鍵が変わり、その単位を流す。
# 単位の動きを変える引数を run-all.py に足すときは、その引数を鍵に入れる(tool_versions か input の材料に足す)。
# 容器の入口が渡す環境変数を足すときは、環境変数はすでに鍵に入る。新しい部を足すときは SCOPES を直す。

# 容器の入口が渡すイメージの形(<名前>@sha256:<64 桁の小文字の 16 進>)。この形のときだけ、容器の中と見なす。
IMAGE_FORM = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
# 鍵の環境変数から除く名前。HOSTNAME は容器の ID(毎回変わる)。RUN_ALL_IMAGE は道具の版の側に入れる。
ENV_SKIP = frozenset({"HOSTNAME", "PWD", "OLDPWD", "SHLVL", "_", "RUN_ALL_IMAGE"})
# 範囲の部品(どれも入力の一覧の部分集合)。
PARTS = ("runner", "top", "plugins", "all")
# 部ごとの範囲(範囲の部品の名前の並び)。表に無い部は、範囲が決まらない(使い回さない)。
# bash の selftest が読む本物のファイルは plugins/dev-workflow/ の下だけ。plugins/ の外を読むようにしたら、
# この表と docs/testing.md の範囲の表を直す。unittest は、リポジトリの全部を読むテストがあるので all。
BASH_PARTS = ("runner", "top", "plugins")
SCOPES = {"loop": BASH_PARTS, "implement-guard": BASH_PARTS, "diff-snapshot": BASH_PARTS,
          "review-agent": BASH_PARTS, "implement-agent": BASH_PARTS, "unittest": ("all",)}
MODULE_SUFFIXES = tuple(importlib.machinery.all_suffixes())
RUNNER_TEST = re.compile(r"scripts/test_[^/]*\.py")
# 新しく流す理由(新しく流す理由の行に出す順)。
REASONS = ("範囲が決まらない", "前の回が失敗", "記録が無い", "道具の版が違う", "環境変数が違う", "入力のファイルが違う")
SPECIAL_KINDS = ((stat.S_ISFIFO, "FIFO"), (stat.S_ISSOCK, "ソケット"), (stat.S_ISCHR, "文字デバイス"),
                 (stat.S_ISBLK, "ブロックデバイス"))


def sha256_of(value):
    """JSON にできる値の sha256(鍵の並びを決めて作る)。

    ASCII だけの JSON にする(環境変数やファイルの名前の、UTF-8 にできない文字も \\u の形で入る)。
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("ascii")).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def link_value(full, target, root_real):
    """シンボリックリンクの値 ("l", リンクの文字列, リンク先の種類, リンク先の相対パスか None)。

    リンク先の種類: 壊れている・file(リポジトリの中の通常のファイル)・dir・special・outside(リポジトリの外)。
    """
    try:
        pointed = os.stat(full)
    except OSError:
        return ("l", target, "壊れている", None)
    real = os.path.realpath(full)
    relative = os.path.relpath(real, root_real)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative):
        return ("l", target, "outside", None)
    if stat.S_ISDIR(pointed.st_mode):
        return ("l", target, "dir", None)
    if not stat.S_ISREG(pointed.st_mode):
        return ("l", target, "special", None)
    return ("l", target, "file", Path(relative).as_posix())


def take_inventory(root, after=False):
    """リポジトリ直下からたどった、全部のディレクトリとファイルの一覧(相対パス → 値)を作る。

    (一覧, None) か、鍵を作らないとき (None, 理由) を返す。git は使わない(一覧に git の版や設定が効かない)。
    - 名前が .git のもの(どの深さでも)だけは、たどらない。無視されたファイル(.gitignore に当たるもの)も入れる。
    - ディレクトリは ("d", 権限, uid, gid)(リポジトリ直下は「.」)。通常のファイルは ("f", 中身の sha256, 権限, uid, gid)。
      特別なファイル(FIFO・ソケット・デバイス)は ("s", 種類)(中身を読まない)。リンクは link_value。
    - 一覧を読めないディレクトリ・読めないファイルがあれば、鍵を作らない。
    - 流す前(after が偽)に __pycache__ という名前のディレクトリがあれば、鍵を作らない(.pyc の中身と、元のファイルの
      更新時刻で読まれ方が変わるため)。流した後(after が真)は、__pycache__ とその下を除く(流している間に Python が書く)。
    """
    root = str(root)
    root_real = os.path.realpath(root)
    inventory = {}
    try:
        top = os.stat(root)
    except OSError as exc:
        return None, f"リポジトリ直下を調べられない({exc})"
    inventory["."] = ("d", stat.S_IMODE(top.st_mode), top.st_uid, top.st_gid)
    stack = [""]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(os.path.join(root, folder) if folder else root) as items:
                entries = sorted(items, key=lambda item: item.name)
        except OSError as exc:
            return None, f"ディレクトリの一覧を読めない({folder or '.'}: {exc.strerror or exc})"
        for item in entries:
            if item.name == ".git":
                continue
            relative = f"{folder}/{item.name}" if folder else item.name
            try:
                info = item.stat(follow_symlinks=False)
                mode = info.st_mode
                if stat.S_ISLNK(mode):
                    inventory[relative] = link_value(item.path, os.readlink(item.path), root_real)
                elif stat.S_ISDIR(mode):
                    if item.name == "__pycache__":
                        if after:
                            continue
                        return None, f"流す前に __pycache__ がある({relative})"
                    inventory[relative] = ("d", stat.S_IMODE(mode), info.st_uid, info.st_gid)
                    stack.append(relative)
                elif stat.S_ISREG(mode):
                    inventory[relative] = ("f", file_sha256(item.path), stat.S_IMODE(mode), info.st_uid, info.st_gid)
                else:
                    kind = next((name for test, name in SPECIAL_KINDS if test(mode)), "不明")
                    inventory[relative] = ("s", kind)
            except OSError as exc:
                return None, f"ファイルを読めない({relative}: {exc.strerror or exc})"
    return inventory, None


def parts_of(relative, packages):
    """相対パスが入る範囲の部品の名前の集合。packages はリポジトリ直下の Python のパッケージの名前。"""
    parts = {"all"}
    if relative == "requirements-dev.txt" or ((relative == "scripts" or relative.startswith("scripts/"))
                                              and not RUNNER_TEST.fullmatch(relative)):
        # ランナーは scripts/ を読み込みの起点にして起動する。scripts/ に標準ライブラリと同じ名前のファイル
        # (例: json.py)を置くと、ランナーの動きが変わる。テストのファイル(test_ で始まる名前)は読み込まない。
        parts.add("runner")
    if relative != "." and "/" not in relative and relative.endswith(MODULE_SUFFIXES):
        # 単位はリポジトリ直下を作業場所にして起動する。作業場所の Python のモジュールは、標準ライブラリより先に読まれうる。
        parts.add("top")
    if relative.split("/")[0] in packages:
        parts.add("top")
    if relative.split("/")[0] in ("plugins", ".claude-plugin"):
        parts.add("plugins")
    return parts


def top_packages(inventory):
    """リポジトリ直下のディレクトリのうち、__init__ のモジュールのファイルを持つもの(Python のパッケージ)と、
    直下のリンクのうち、ディレクトリかリポジトリの外を指すものの名前の集合。

    リンクは中をたどらないので、パッケージかどうかを決められない(例: 直下の json が、外のパッケージの
    ディレクトリを指す)。top に入れると、file_context が top のハッシュ値を None(範囲が決まらない)にする。
    """
    names = set()
    for relative, value in inventory.items():
        head, _, rest = relative.partition("/")
        if rest and "/" not in rest and rest.startswith("__init__") and rest[len("__init__"):] in MODULE_SUFFIXES:
            names.add(head)
        elif not rest and value[0] == "l" and value[2] in ("dir", "outside"):
            names.add(head)
    return names


def file_context(root, after=False):
    """入力の一覧と、範囲の部品ごとのハッシュ値・入るパスの集合を作る。(材料, None) か (None, 理由)。

    部品のハッシュ値は、部品に入るパスと値を、パスの順に並べた sha256。部品ごとに 1 回だけ作る。
    部品の中のリンクが、同じ部品の中の通常のファイルを指さないとき(壊れたリンクを除く)は、その部品の
    ハッシュ値を None(範囲が決まらない)にする。コピーすると中身をたどり、部品の外の変化はハッシュ値に入らないため。
    """
    inventory, reason = take_inventory(root, after=after)
    if inventory is None:
        return None, reason
    packages = top_packages(inventory)
    members = {part: [] for part in PARTS}
    for relative in sorted(inventory):
        for part in parts_of(relative, packages):
            members[part].append(relative)
    digests = {}
    for part, paths in members.items():
        inside = set(paths)
        digest = hashlib.sha256()
        for relative in paths:
            value = inventory[relative]
            if value[0] == "l" and value[2] != "壊れている" and not (
                    value[2] == "file" and value[3] in inside and inventory.get(value[3], ("",))[0] == "f"):
                digest = None
                break
            digest.update(json.dumps([relative, value]).encode("ascii") + b"\n")
        digests[part] = None if digest is None else digest.hexdigest()
    sets = {part: set(paths) for part, paths in members.items()}
    return {"inventory": inventory, "digests": digests, "members": sets}, None


def current_umask():
    """今の umask(新しく作るファイルから除く権限)。単位を流す前(ほかのスレッドが無い間)に呼ぶ。"""
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def command_text(command):
    """コマンドの出力(読めなければ理由)。道具の版を鍵に入れるのに使う。"""
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"起動できない: {exc}"
    return f"終了コード {result.returncode}: {result.stdout.strip()}"


def tool_versions(image):
    """道具の版(tools)。イメージの ID が、イメージの中の道具・Python のパッケージ・git の設定・PATH のコマンドの
    全部を決める。版の値は、報告で読めるように入れる。"""
    bash = command_text(["bash", "--version"])
    return {"image": image, "git": command_text(["git", "--version"]), "python": sys.version,
            "executable": sys.executable, "bash": bash.splitlines()[0] if bash else bash,
            "umask": f"{current_umask():04o}", "uid": os.geteuid(), "gid": os.getegid()}


def reuse_context():
    """鍵の材料を作る。(材料, None) か、鍵を作らないとき (None, 理由)。

    環境変数 RUN_ALL_IMAGE が <名前>@sha256:<64 桁> の形のときだけ、容器の中と見なす。
    """
    image = os.environ.get("RUN_ALL_IMAGE")
    if image is None:
        return None, "ホストで流しているので、使い回さない"
    if not IMAGE_FORM.fullmatch(image):
        return None, "容器のイメージの ID が分からない"
    context, reason = file_context(REPO)
    if context is None:
        return None, reason
    context["tools"] = sha256_of(tool_versions(image))
    context["env"] = sha256_of({name: value for name, value in os.environ.items() if name not in ENV_SKIP})
    return context, None


def unit_files(unit, context):
    """単位の範囲の部品のハッシュ値の並び [[名前, ハッシュ値], …]。範囲が決まらなければ None。

    範囲が決まらないのは、範囲の決まりの無い部・entry が範囲の中の通常のファイルでない単位・範囲の部品の
    ハッシュ値が None(リンク)のとき。
    """
    parts = SCOPES.get(group_of(unit))
    if parts is None or unit.entry is None:
        return None
    if context["inventory"].get(unit.entry, ("",))[0] != "f" or \
            not any(unit.entry in context["members"][part] for part in parts):
        return None
    if any(context["digests"][part] is None for part in parts):
        return None
    return [[part, context["digests"][part]] for part in parts]


def unit_key(unit, context):
    """単位の鍵 {input, tools, env, files}。範囲が決まらなければ None。"""
    files = unit_files(unit, context)
    if files is None:
        return None
    key = {"tools": context["tools"], "env": context["env"], "files": sha256_of(files)}
    key["input"] = sha256_of({"version": run_units.REUSE_VERSION, "unit": unit.key, **key})
    return key


def rebuild(unit, item):
    """記録から結果の組 (unit.ident, 終了コード, 出力, 秒, 件数) を組み立て直す。describe が合格と言わなければ None。

    先頭の値は記録からでなく unit.ident から取る。件数は元の型に戻す(JSON の配列は組に、辞書は辞書のまま)。
    部ごとの集計が組の形を前提にしているため。
    """
    try:
        output = run_units.unpack_output(item["output"])
    except ValueError:
        return None
    extra = tuple(item["extra"]) if isinstance(item["extra"], list) else item["extra"]
    result = (unit.ident, item["code"], output, float(item["seconds"]), extra)
    try:
        ok = unit.describe(result)[1]
    except (KeyError, TypeError, IndexError, ValueError):
        return None
    return result if ok else None


def reuse_of(unit, key, records):
    """(使い回す結果, None) か (None, 新しく流す理由)。records は同じ単位の鍵の記録(at の新しい順)。"""
    if key is None:
        return None, "範囲が決まらない"
    if not records:
        return None, "記録が無い"
    same = [item for item in records if item["input"] == key["input"]]
    if same:
        latest = max(same, key=lambda item: run_units.micros_of(item["at"]))
        result = rebuild(unit, latest) if latest["ok"] else None
        return (None, "前の回が失敗") if result is None else (result, None)
    if not any(item["tools"] == key["tools"] for item in records):
        return None, "道具の版が違う"
    if not any(item["tools"] == key["tools"] and item["env"] == key["env"] for item in records):
        return None, "環境変数が違う"
    return None, "入力のファイルが違う"


def make_record(key, result, ok, at):
    """新しく流した単位の記録。失敗の記録の出力は空にする。"""
    extra = list(result[4]) if isinstance(result[4], tuple) else result[4]
    return {**key, "ok": ok, "seconds": round(result[3], 3), "at": at, "code": result[1], "extra": extra,
            "output": run_units.pack_output(result[2]) if ok else ""}


def reuse_lines(units, reused, reasons, plan):
    """使い回しの行(使い回し: …・新しく流す理由: …・使い回した単位ごとの行)。"""
    fresh = len(units) - len(reused)
    lines = [f"使い回し: 入力が前の合格と同じ {len(reused)} 個の単位は流さない(新しく流すのは {fresh} 個)"]
    counts = collections.Counter(reasons.values())
    if fresh:
        lines.append("新しく流す理由: " + "・".join(f"{reason} {counts[reason]} 個" for reason in REASONS if counts[reason]))
    for unit in units:
        if unit.key not in reused:
            continue
        if plan:
            lines.append(f"使い回し  {unit.label}")
        else:
            result = reused[unit.key]
            lines.append(f"使い回し {result[3]:7.1f} 秒  {unit.label}  {unit.describe(result)[0]}")
    return lines


def record_reuse(path, context, keys, fresh, results, finished):
    """新しく流した単位の結果(合格も失敗も)を、使い回しの記録に書く。

    流し終えた後に入力の一覧をもう一度取り、範囲の部品のハッシュ値が流す前と同じ単位だけを、流す前の鍵で記録する
    (流している間に入力が変わった単位は、どちらの中身を確かめたか分からないため)。
    新しく流して落ちた単位があれば failed を真にする(記録を書けないとき、無効の印で古い合格を使わせないため)。
    """
    after, _ = file_context(REPO, after=True)
    entries = []
    failed = False
    for unit, result in zip(fresh, results):
        key = keys.get(unit.key)
        if key is None:
            continue
        ok = unit.describe(result)[1]
        failed = failed or not ok
        if after is None or unit_files(unit, after) != unit_files(unit, context):
            continue
        entries.append((unit.key, make_record(key, result, ok, finished[unit.key])))
    if entries or failed:
        run_units.save_reuse(path, entries, failed)


# ---- 本体 ----

def main(argv=None):
    parser = argparse.ArgumentParser(description="全回帰テストを、1 つの共通の枠で流す。")
    parser.add_argument("-j", "--jobs", type=int, default=run_units.cpu_count(),
                        help="同時に流す枠の数(既定は使える CPU の数)")
    parser.add_argument("--log-dir", type=Path, help="全部の単位の出力を、このフォルダの <鍵>.log に書く")
    parser.add_argument("--weights-file", type=Path, default=None,
                        help="重みの記録の置き場(既定は ${XDG_CACHE_HOME:-~/.cache}/workflow-run-all/weights.json)")
    parser.add_argument("--plan", action="store_true", help="流さずに、始める順を出して終わる")
    parser.add_argument("--reuse", action="store_true",
                        help="入力が前の合格と同じ単位を流さず、前の結果を使う(容器の中だけ)")
    parser.add_argument("--reuse-file", type=Path, default=None,
                        help="使い回しの記録の置き場(既定は ${XDG_CACHE_HOME:-~/.cache}/workflow-run-all/results.json)")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("-j は 1 以上にする")
    if args.log_dir is not None and not args.log_dir.is_dir():
        parser.error(f"--log-dir のフォルダが無い: {args.log_dir}")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or str(Path("~/.cache").expanduser())) / "workflow-run-all"
    weights_file = args.weights_file if args.weights_file is not None else cache / "weights.json"
    reuse_file = args.reuse_file if args.reuse_file is not None else cache / "results.json"

    started = time.monotonic()
    if not args.plan:
        ensure_state_dir()
    notes = git_lines()
    warnings = [line for line in notes if line.startswith("警告:")]
    # 鍵の材料(入力の一覧など)は、単位を組み立てる前に 1 回だけ取る。既定の回も記録するので、--reuse の無い
    # --plan の回のほかは、いつも取る。取れないとき、--reuse の無い回は何も出さない。
    context, no_key = (None, None) if args.plan and not args.reuse else reuse_context()
    with tempfile.TemporaryDirectory(prefix="run-all-") as workdir:
        try:
            units, guard_warning = build_units(workdir)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            print(f"異常: loop-selftest.sh の節を読めない: {exc}", flush=True)
            return 1
        if guard_warning:
            notes.append(guard_warning)
            warnings.append(guard_warning)
        record = run_units.load_weights(weights_file, notes=notes)
        apply_weights(units, record)
        keys = {} if context is None else {unit.key: unit_key(unit, context) for unit in units}
        reused = {}
        reasons = {}
        if args.reuse and context is None:
            notes.append(f"注意: 使い回せない({no_key})。全部の単位を新しく流す")
        elif args.reuse:
            records = run_units.load_reuse(reuse_file, notes)
            for unit in units:
                result, reason = reuse_of(unit, keys[unit.key], records.get(unit.key, []))
                if result is None:
                    reasons[unit.key] = reason
                else:
                    reused[unit.key] = result
        reporting = args.reuse and context is not None
        fresh = [unit for unit in units if unit.key not in reused]
        slot = run_units.slot_order(fresh)
        outside = [unit for unit in fresh if not unit.in_slot]

        if args.plan:
            print(f"計画: 枠の単位 {len(slot)} 個を、同時に {args.jobs} 個まで流す(ほかに枠の外 {len(outside)} 個)")
            for line in notes:
                print(line)
            if reporting:
                for line in reuse_lines(units, reused, reasons, plan=True):
                    print(line)
            for unit in outside:
                print(f"枠の外  {unit.label}")
            for number, unit in enumerate(slot, 1):
                print(f"{number:4d}  {unit.weight:7.1f} 秒  {unit.source}  {unit.label}")
            for line in warnings:
                print(line)
            return 0

        for line in notes:
            print(line, flush=True)
        if reporting:
            for line in reuse_lines(units, reused, reasons, plan=False):
                print(line, flush=True)
        for unit in fresh:
            if args.log_dir is not None:
                unit.log_name = log_name_of(unit.key)
                unit.output_of = lambda result: result[2]
        finished = {}
        if context is not None:
            # 記録の at(単位が終わった時刻)を取る。
            for unit in fresh:
                def timed(children, unit=unit, inner=unit.run):
                    try:
                        return inner(children)
                    finally:
                        finished[unit.key] = run_units.utc_now()
                unit.run = timed
        print(f"{len(fresh)} 個の単位を、同時に {args.jobs} 個まで流す"
              f"(枠の外: {'・'.join(unit.label for unit in outside) or 'なし'})", flush=True)

        def on_interrupt(started_units):
            """止めたとき、loop の単位の残った一時フォルダを表示する。"""
            futures = [future for unit, future in started_units if group_of(unit) == "loop"]
            for section, path in run_loop.left_scratches(futures):
                print(f"残った一時フォルダ({run_loop.label(section)}。確かめてから消す): {path}", flush=True)

        fresh_results, code = run_units.run_units(fresh, args.jobs, log_dir=args.log_dir, on_interrupt=on_interrupt)
    if code is not None:
        return code

    by_key = dict(reused)
    by_key.update((unit.key, result) for unit, result in zip(fresh, fresh_results))
    results = [by_key[unit.key] for unit in units]
    # 部ごとの集計と失敗の一覧は、使い回した結果も含めた全部の単位で行う。
    lines, problems = summarize(units, results)
    failures = [(unit.label, result[1], result[2]) for unit, result in zip(units, results)
                if not unit.describe(result)[1]]
    run_units.print_failures(failures)
    print()
    for problem in problems:
        print(f"異常: {problem}")
    for line in warnings:
        print(line)
    # 新しく流して成功した単位の時間を、重みの記録に書く(止めた回と、失敗した単位と、使い回した単位は書かない)。
    run_units.save_weights(weights_file, record, {unit.key: result[3] for unit, result in zip(fresh, fresh_results)
                                                   if unit.describe(result)[1]})
    if context is not None:
        record_reuse(reuse_file, context, keys, fresh, fresh_results, finished)
    if reporting:
        print(f"使い回した単位: {len(reused)} 個 / 新しく流した単位: {len(fresh)} 個")
    for line in lines:
        print(line)
    elapsed = time.monotonic() - started
    passed = not problems and not failures
    if not passed:
        print(f"結果: 不合格(上の「異常:」を見る)/ {elapsed:.1f} 秒")
    elif reused:
        print(f"結果: 合格({len(reused)} 個の単位は使い回した)/ {elapsed:.1f} 秒")
    else:
        print(f"結果: 合格 / {elapsed:.1f} 秒")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
