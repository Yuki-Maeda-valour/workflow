#!/usr/bin/env python3
"""loop-selftest.sh を節ごとに別のプロセスへ分けて、並べて流す。

使い方(リポジトリ直下で実行する):
  LOOP_SELFTEST_TIMEOUT=6000 RUN_TIMEOUT=300 python3 -B scripts/run-loop-selftest.py   # 全部の節
  python3 -B scripts/run-loop-selftest.py perm d22        # 選んだ節だけ(複数可)
  python3 -B scripts/run-loop-selftest.py -j 8            # 同時に流すプロセスの数(既定は使える CPU の数の 4 分の 1)
  python3 -B scripts/run-loop-selftest.py --log-dir DIR   # 各プロセスの出力を DIR/<節>.log に残す

- 節は、loop-selftest.sh の行頭の `if want <節>; then`(`if want <節> || want <別名>; then` も)から読む。
  1 つの節を 1 つのプロセスで流す(`LOOP_SELFTEST_ONLY=<節>`)。ほかの環境変数は、そのまま各プロセスへ渡す。
- 節の外の確かめ(最初のスタブの証明と、最後の後片付けの確かめ)は、どのプロセスでも流れる。
  そこで、どの節も選ばないプロセス(`LOOP_SELFTEST_ONLY=,`)を 1 つ足して、その件数を測る。
  合計では、節の外の確かめを 1 回分だけ数える(各節のプロセスの件数から、節の外の件数を引いて足す)。
  1 本で流した `bash loop-selftest.sh` と同じ件数になる。
- 件数の照合: どの節も、ちょうど 1 つのプロセスで流れるかを、流す前に確かめる。流した後は、どの節も
  自分の確かめを 1 件以上流したか(節の名前が合わずに何も流れなかったのでないか)を確かめる。
- 終了コード: run-tests.py と同じ。全部のプロセスが成功なら 0。FAIL・結果の行の欠け・件数の食い違い・
  節を読めないことが 1 つでもあれば 1。引数の誤りは 2。止めたときは 128 + シグナルの番号。
- 止め方・失敗したプロセスの出力の出し方も run-tests.py と同じ(その仕組みを読み込んで使う)。
  限界も同じ: loop-selftest.sh が別のプロセスグループで起動した子(loop.sh など)は、止めた後に
  `RUN_TIMEOUT` の秒数まで残ることがある。そのときは、残ったプロセスを pid を指定して止める。
"""

import argparse
import concurrent.futures
import importlib.util
import os
import re
import signal
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
SELFTEST = REPO / "plugins" / "dev-workflow" / "skills" / "ship-task" / "scripts" / "loop-selftest.sh"

# どの節にも当たらない LOOP_SELFTEST_ONLY の値。loop-selftest.sh の want() は「,<節>,」を探すので、
# カンマだけの値には、どの節の名前も当たらない(空にすると、全部の節が流れる)。
NO_SECTION = ","
OUTSIDE = "(節の外だけ)"
OUTSIDE_LOG = "_outside.log"

# 先に始める順の重み(秒)。値は、節を 1 つずつ順に流したときの時間
# (2026-11-05、Ubuntu 22.04 の容器・git 2.56.0・16 CPU。LOOP_SELFTEST_TIMEOUT=6000 RUN_TIMEOUT=300。
# 節の外だけのプロセスは 11 秒。30 節の和は 4054 秒)。載っていない節は軽いものとして、後に回す。
WEIGHTS = {
    "discover": 639, "breakers": 437, "judge": 393, "humanconfig": 327, "environment": 276, "stops": 251,
    "memory": 166, "host": 147, "h28": 133, "h47": 118, "d22": 115, "h46": 114, "h50": 93, "h47enterprise": 81,
    "kill": 77, "hooks": 72, "locked": 70, "argv": 66, "h47proof": 62, "signals": 57, "shell-prefix": 56,
    "perm": 52, "h46skillsdir": 44, "h46recheck": 41, "order": 39, "between": 37, "h46market": 36, "skip": 28,
    "link": 18, "sync": 11,
}

GUARD = re.compile(r"^if (want [a-z0-9][a-z0-9-]*(?: \|\| want [a-z0-9][a-z0-9-]*)*); then$")
# want の呼び出しに見える形(行頭の if want 以外で使うと、どのプロセスで流れるかを数えられない)。
CALL = re.compile(r"\bwant [A-Za-z0-9_-]+\s*(?:;|\|\||&&)")
RESULT = re.compile(r"^結果: PASS (\d+) 件 / FAIL (\d+) 件$", re.M)


def load_run_tests():
    """同じフォルダの run-tests.py を読み込む(止め方と、並べる数の既定の計算を同じにするため)。"""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("run_tests", SCRIPTS / "run-tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_tests = load_run_tests()


def read_sections(text):
    """loop-selftest.sh の節の名前を、書いてある順に返す。数えられない形があれば ValueError。

    1 つの節だけを見る `if want <節>; then` の節を、プロセスに分ける単位にする。
    `if want a || want b; then` のように複数の名前を見る所は、その中にちょうど 1 つ、単位の節が
    無ければならない(2 つあると 2 つのプロセスで流れ、無いとどのプロセスでも流れない)。
    """
    blocks = []
    for number, line in enumerate(text.split("\n"), 1):
        match = GUARD.match(line)
        if match:
            blocks.append((number, re.findall(r"want ([a-z0-9-]+)", match.group(1))))
        elif CALL.search(line):
            raise ValueError(f"{number} 行目: 行頭の `if want <節>; then` ではない形で want を使っている")
    sections = []
    for number, names in blocks:
        if len(names) == 1 and names[0] not in sections:
            sections.append(names[0])
    if not sections:
        raise ValueError("`if want <節>; then` の行が無い")
    for number, names in blocks:
        units = [name for name in names if name in sections]
        if len(units) != 1:
            raise ValueError(f"{number} 行目: ちょうど 1 つのプロセスで流れない(単独の節の名前: {' '.join(units) or '無し'})")
    return sections


def default_jobs():
    """使える CPU の数の 4 分の 1(1 以上)。

    全回帰テストでは、run-tests.py(既定は CPU の数の半分)と同時に流す。足して CPU の数を超えないようにする
    (混んだ機械では、loop.sh を起動するテストが中の時間切れに当たる)。
    """
    return max(1, run_tests.default_jobs() // 2)


def label(section):
    return OUTSIDE if section is None else section


def run_job(section, children, log_dir):
    env = dict(os.environ, LOOP_SELFTEST_ONLY=NO_SECTION if section is None else section)
    started = time.monotonic()
    # -v: FAIL の項目に、loop.sh の出力の末尾を添える(成功した項目の出力は変わらない)。
    process = children.start(["bash", str(SELFTEST), "-v"], env=env)
    try:
        output, _ = process.communicate()
    finally:
        children.finish(process)
    elapsed = time.monotonic() - started
    if log_dir is not None:
        name = OUTSIDE_LOG if section is None else f"{section}.log"
        (log_dir / name).write_text(output, encoding="utf-8")
    found = RESULT.findall(output)
    counts = (int(found[-1][0]), int(found[-1][1])) if found else None
    return section, process.returncode, output, elapsed, counts


def main(argv=None):
    parser = argparse.ArgumentParser(description="loop-selftest.sh を節ごとに分けて、並べて流す。")
    parser.add_argument("sections", nargs="*", help="流す節(既定は全部の節)")
    parser.add_argument("-j", "--jobs", type=int, default=default_jobs(),
                        help="同時に流すプロセスの数(既定は使える CPU の数の 4 分の 1)")
    parser.add_argument("--log-dir", type=Path, help="各プロセスの出力を、このフォルダの <節>.log に書く")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("-j は 1 以上にする")
    if args.log_dir is not None and not args.log_dir.is_dir():
        parser.error(f"--log-dir のフォルダが無い: {args.log_dir}")

    try:
        sections = read_sections(SELFTEST.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"異常: loop-selftest.sh の節を読めない: {exc}", flush=True)
        return 1
    if args.sections:
        unknown = [name for name in args.sections if name not in sections]
        if unknown:
            parser.error(f"loop-selftest.sh に無い節: {' '.join(unknown)}(節: {' '.join(sections)})")
        chosen = list(dict.fromkeys(args.sections))
    else:
        chosen = sections
    # 節の外だけのプロセスを最初に始め、残りは重い節から始める。
    jobs = [None] + sorted(chosen, key=lambda name: (-WEIGHTS.get(name, 0), sections.index(name)))

    started = time.monotonic()
    results = []
    children = run_tests.Children()
    for sig in run_tests.INTERRUPT_SIGNALS:
        signal.signal(sig, run_tests.raise_interrupt)
    print(f"loop-selftest.sh の {len(chosen)} 節を、{len(jobs)} 個のプロセスに分けて、同時に {args.jobs} 個まで流す",
          flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = []
        try:
            futures.extend(pool.submit(run_job, section, children, args.log_dir) for section in jobs)
            for future in concurrent.futures.as_completed(futures):
                section, code, output, elapsed, counts = future.result()
                ok = code == 0 and counts is not None and counts[1] == 0
                shown = "結果の行が無い" if counts is None else f"PASS {counts[0]} 件 / FAIL {counts[1]} 件"
                print(f"{'ok  ' if ok else 'FAIL'} {elapsed:7.1f} 秒  {label(section)}  {shown}", flush=True)
                results.append((section, code, output, elapsed, counts, ok))
        except KeyboardInterrupt as exc:
            signum = exc.args[0] if exc.args and exc.args[0] in run_tests.INTERRUPT_SIGNALS else signal.SIGINT
            for sig in (signal.SIGINT, *run_tests.INTERRUPT_SIGNALS):
                signal.signal(sig, signal.SIG_IGN)
            dropped = sum(future.cancel() for future in futures)
            killed = children.stop_all()
            print(f"\n中断: 動いていた {killed} 個のプロセスを止め、まだ始めていない {dropped} 個を始めずに捨てた"
                  f"(終わっていたのは {len(results)} 個)", flush=True)
            return 128 + signum

    problems = []
    for section, code, output, elapsed, counts, ok in results:
        if counts is None:
            problems.append(f"{label(section)}: 結果の行が無い(終了コード {code})")
        elif not ok:
            problems.append(f"{label(section)}: 失敗(終了コード {code})")
    outside = next((counts for section, *_, counts, ok in results if section is None), None)
    if outside is None:
        # 節の外の件数が分からないので、各プロセスの件数をそのまま足す(合計は 1 本で流したときより多くなる)。
        problems.append("節の外の確かめの件数が分からない(合計は、各プロセスの件数をそのまま足した)")
        outside = (0, 0)
    total_pass, total_fail = outside
    for section, code, output, elapsed, counts, ok in results:
        if section is None or counts is None:
            continue
        own_pass, own_fail = counts[0] - outside[0], counts[1] - outside[1]
        if own_pass + own_fail < 1:
            problems.append(f"{section}: 節の確かめが 1 件も流れていない(節の名前が合わないか、節が流れなかった)")
        total_pass += own_pass
        total_fail += own_fail

    failed = [item for item in results if not item[5]]
    for section, code, output, elapsed, counts, ok in sorted(failed, key=lambda item: jobs.index(item[0])):
        print(f"\n===== {label(section)}(終了コード {code})=====")
        print(output.rstrip())
    elapsed = time.monotonic() - started
    print()
    for problem in problems:
        print(f"異常: {problem}")
    print(f"節の外の確かめ(PASS {outside[0]} 件 / FAIL {outside[1]} 件)は、どのプロセスでも流れる。合計では 1 回分だけ数えた")
    print(f"結果: PASS {total_pass} 件 / FAIL {total_fail} 件 / {elapsed:.1f} 秒")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
