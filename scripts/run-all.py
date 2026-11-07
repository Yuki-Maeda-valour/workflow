#!/usr/bin/env python3
"""全回帰テスト(回帰テストをすべて流すこと)を、1 つのコマンドと 1 つの共通の枠で流す。

使い方(リポジトリ直下で実行する):
  python3 -B scripts/run-all.py                         # 全部を流す
  python3 -B scripts/run-all.py -j 8                    # 同時に流す枠の数(既定は使える CPU の数)
  python3 -B scripts/run-all.py --log-dir DIR           # 全部の単位の出力を DIR/<鍵>.log に残す
  python3 -B scripts/run-all.py --weights-file PATH     # 重みの記録の置き場(既定は ~/.cache/workflow-run-all/weights.json)
  python3 -B scripts/run-all.py --plan                  # 流さずに、始める順を出して終わる

- 流す単位: unittest のファイルとシャード(run-tests.py)・loop-selftest の節と節の外だけ(run-loop-selftest.py)・
  implement-guard-selftest.sh の単位・diff-snapshot-selftest.sh。review-agent-selftest.sh と
  implement-agent-selftest.sh は、待ちが主なので枠の外で、最初に始める。
- 枠の単位は、重みの大きい順に、1 つの共通の枠で流す。重みは、重みの記録 → 各ランナーの表 → 既定の 200 秒の順に引く。
- 部ごとに件数を照らし合わせる(部の集計は、2 つのランナーの関数を使う)。
- 終了コード: 合格なら 0。ほかは 1。引数の誤りは 2。止めたときは 128 + シグナルの番号(結果の行は出さない)。
- 詳しい決まりは docs/testing.md を見る。
"""

import argparse
import importlib.util
import os
import re
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

def selftest_unit(key, label, script, args=(), in_slot=True):
    """bash の selftest を 1 つ流す単位。結果は (鍵, 終了コード, 出力, 秒, (PASS, FAIL) か None)。"""
    def run(children):
        code, output, elapsed = run_units.run_command(children, ["bash", str(script), *args])
        found = run_loop.RESULT.findall(output)
        counts = (int(found[-1][0]), int(found[-1][1])) if found else None
        return key, code, output, elapsed, counts

    weight = GUARD_WEIGHTS.get(key.partition(":")[2]) if key.startswith("implement-guard:") else WHOLE_WEIGHTS.get(key)
    return run_units.Unit(key=key, label=label, run=run, describe=run_loop.describe, in_slot=in_slot,
                          weight=weight, table_weight=weight)


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


# ---- 本体 ----

def main(argv=None):
    parser = argparse.ArgumentParser(description="全回帰テストを、1 つの共通の枠で流す。")
    parser.add_argument("-j", "--jobs", type=int, default=run_units.cpu_count(),
                        help="同時に流す枠の数(既定は使える CPU の数)")
    parser.add_argument("--log-dir", type=Path, help="全部の単位の出力を、このフォルダの <鍵>.log に書く")
    parser.add_argument("--weights-file", type=Path, default=None,
                        help="重みの記録の置き場(既定は ${XDG_CACHE_HOME:-~/.cache}/workflow-run-all/weights.json)")
    parser.add_argument("--plan", action="store_true", help="流さずに、始める順を出して終わる")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("-j は 1 以上にする")
    if args.log_dir is not None and not args.log_dir.is_dir():
        parser.error(f"--log-dir のフォルダが無い: {args.log_dir}")
    weights_file = args.weights_file
    if weights_file is None:
        cache = os.environ.get("XDG_CACHE_HOME") or str(Path("~/.cache").expanduser())
        weights_file = Path(cache) / "workflow-run-all" / "weights.json"

    started = time.monotonic()
    if not args.plan:
        ensure_state_dir()
    notes = git_lines()
    warnings = [line for line in notes if line.startswith("警告:")]
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
        slot = run_units.slot_order(units)
        outside = [unit for unit in units if not unit.in_slot]

        if args.plan:
            print(f"計画: 枠の単位 {len(slot)} 個を、同時に {args.jobs} 個まで流す(ほかに枠の外 {len(outside)} 個)")
            for line in notes:
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
        for unit in units:
            if args.log_dir is not None:
                unit.log_name = log_name_of(unit.key)
                unit.output_of = lambda result: result[2]
        print(f"{len(units)} 個の単位を、同時に {args.jobs} 個まで流す"
              f"(枠の外: {'・'.join(unit.label for unit in outside)})", flush=True)

        def on_interrupt(started_units):
            """止めたとき、loop の単位の残った一時フォルダを表示する。"""
            futures = [future for unit, future in started_units if group_of(unit) == "loop"]
            for section, path in run_loop.left_scratches(futures):
                print(f"残った一時フォルダ({run_loop.label(section)}。確かめてから消す): {path}", flush=True)

        results, code = run_units.run_units(units, args.jobs, log_dir=args.log_dir, on_interrupt=on_interrupt)
    if code is not None:
        return code

    lines, problems = summarize(units, results)
    failures = [(unit.label, result[1], result[2]) for unit, result in zip(units, results)
                if not unit.describe(result)[1]]
    run_units.print_failures(failures)
    print()
    for problem in problems:
        print(f"異常: {problem}")
    for line in warnings:
        print(line)
    # 成功した単位の時間を、重みの記録に書く(止めた回と、失敗した単位は書かない)。
    run_units.save_weights(weights_file, record, {unit.key: result[3] for unit, result in zip(units, results)
                                                   if unit.describe(result)[1]})
    for line in lines:
        print(line)
    elapsed = time.monotonic() - started
    passed = not problems and not failures
    print(f"結果: 合格 / {elapsed:.1f} 秒" if passed else f"結果: 不合格(上の「異常:」を見る)/ {elapsed:.1f} 秒")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
