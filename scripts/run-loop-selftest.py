#!/usr/bin/env python3
"""loop-selftest.sh を節ごとに別のプロセスへ分けて、並べて流す。

使い方(リポジトリ直下で実行する):
  LOOP_SELFTEST_TIMEOUT=6000 RUN_TIMEOUT=300 python3 -B scripts/run-loop-selftest.py   # 全部の節
  python3 -B scripts/run-loop-selftest.py perm d22        # 選んだ節だけ(複数可)
  python3 -B scripts/run-loop-selftest.py -j 8            # 同時に流すプロセスの数(既定は使える CPU の数の 4 分の 1)
  python3 -B scripts/run-loop-selftest.py --log-dir DIR   # 各プロセスの出力を DIR/<節>.log に残す

- 節は、loop-selftest.sh の最上位の `if want <節>; then`(`if want <節> || want <別名>; then` も)。
  1 つの節を 1 つのプロセスで流す(`LOOP_SELFTEST_ONLY=<節>`)。ほかの環境変数は、そのまま各プロセスへ渡す。
- 節の形は、流す前に bash 自身に読ませて確かめる(read_sections)。want を呼ぶのが、最上位の
  `if want <節>; then` の行(行頭に 1 行で書いたもの)だけでないときは、流さずに止める。
- 節の外の確かめ(最初のスタブの証明と、最後の後片付けの確かめ)は、どのプロセスでも流れる。
  そこで、どの節も選ばないプロセス(`LOOP_SELFTEST_ONLY=,`)を 1 つ足して、その件数を測る。
  節の外の確かめが、どのプロセスでも同じ名前で PASS したときだけ、合計では 1 回分だけ数える
  (各節のプロセスの件数から、節の外の件数を引いて足す)。1 本で流した `bash loop-selftest.sh` と同じ件数になる。
  節の外の確かめが落ちたプロセスがあるときは、差し引きをやめて「異常:」を出し、合計の件数を「不明」にする。
- 件数の照合: どの節も、ちょうど 1 つのプロセスで流れるかを、流す前に確かめる。流した後は、どの節も
  自分の確かめを 1 件以上流したか(節の名前が合わずに何も流れなかったのでないか)を確かめる。
- 終了コード: run-tests.py と同じ。全部のプロセスが成功なら 0。FAIL・結果の行の欠け・件数の食い違い・
  節を読めないことが 1 つでもあれば 1。引数の誤りは 2。止めたときは 128 + シグナルの番号。
- 止め方・失敗したプロセスの出力の出し方も run-tests.py と同じ(その仕組みを読み込んで使う)。
- 限界: 止めたときに、loop-selftest.sh が別のプロセスグループで起動した子(loop.sh など)が、
  `RUN_TIMEOUT` の秒数まで残ることがある。そのときは、残ったプロセスを pid を指定して止める。
  loop-selftest.sh の一時フォルダ(`mktemp -d` で作る。既定は /tmp/tmp.*)も、後片付けが走らずに残ることがある。
  止めたプロセスの出力にある一時フォルダのうち、残っているものは、中断の行の後に表示する。
"""

import argparse
import collections
import concurrent.futures
import importlib.util
import os
import re
import signal
import subprocess
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

# 先に始める順の重み(秒)。値は、節を 1 つずつ順に流したときの時間を秒に丸めたもの
# (2026-11-05、Ubuntu 22.04 の容器・git 2.56.0・16 CPU。LOOP_SELFTEST_TIMEOUT=6000 RUN_TIMEOUT=300)。
# 測った時間の和は、30 節で 4054.3 秒。節の外だけのプロセス(10.8 秒)を足すと 4065.1 秒。
# 丸めたので、下の値の和は 4056 になる。載っていない節は軽いものとして、後に回す。
WEIGHTS = {
    "discover": 639, "breakers": 437, "judge": 393, "humanconfig": 327, "environment": 276, "stops": 251,
    "memory": 166, "host": 147, "h28": 133, "h47": 118, "d22": 115, "h46": 114, "h50": 93, "h47enterprise": 81,
    "kill": 77, "hooks": 72, "locked": 70, "argv": 66, "h47proof": 62, "signals": 57, "shell-prefix": 56,
    "perm": 52, "h46skillsdir": 44, "h46recheck": 41, "order": 39, "between": 37, "h46market": 36, "skip": 28,
    "link": 18, "sync": 11,
}

# 分ける単位にする形。最上位に、1 行で書く。
GUARD = re.compile(r"^if (want [a-z0-9][a-z0-9-]*(?: \|\| want [a-z0-9][a-z0-9-]*)*); then$")
NAME = re.compile(r"want ([a-z0-9-]+)")
INDENT = "    "
# want をコマンドとして呼ぶ形(bash が整えたコードの行で探す)。`local want`・`want=`・`$want` は当たらない。
CALL = re.compile(r"""(?:^|[;&|!({`]|\$\(|\b(?:if|elif|while|until|then|do|else|time|exec|command|builtin|eval|env|nohup|timeout)\s)"""
                  r"""\s*\\?["']?want["']?(?=[\s;&|)`]|$)""")
# 複数行の文字列の続きの行(bash が字下げし直さない行)では、演算子の後の want だけを探す
# (ヒアドキュメントの中の Python の `want = ...` などを、呼び出しと数えないため)。
TAIL_CALL = re.compile(r"""(?:&&|\|\||;|\||\$\(|`)\s*\\?["']?want["']?(?=[\s;&|)`]|$)""")
WRAPPER = "__run_loop_selftest_body"
RESULT = re.compile(r"^結果: PASS (\d+) 件 / FAIL (\d+) 件$", re.M)
PASSED = re.compile(r"^PASS  (.*)$", re.M)
SCRATCH = re.compile(r"^loop-selftest: 対象 .*\(scratch (.+)\)$", re.M)


def load_run_tests():
    """同じフォルダの run-tests.py を読み込む(止め方と、並べる数の既定の計算を同じにするため)。"""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("run_tests", SCRIPTS / "run-tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_tests = load_run_tests()


def bash_listing(data, levels):
    """スクリプト(バイト列)を levels 段の関数で包んで定義だけし(実行はしない)、bash が整えた本文の行を返す。"""
    head = b"".join(f"{WRAPPER}{index} () {{\n".encode() for index in range(levels))
    script = head + data + b"\n" + b"}\n" * levels + f"declare -f {WRAPPER}0\n".encode()
    # BASH_ENV は、非対話の bash が最初に読んで実行するので外す。
    env = {key: value for key, value in os.environ.items() if key != "BASH_ENV"}
    result = subprocess.run(["bash", "--norc", "--noprofile", "-s"], input=script, capture_output=True,
                            env=env, timeout=120)
    lines = result.stdout.decode("utf-8", "replace").split("\n")
    if result.returncode != 0 or result.stderr or len(lines) < 2 * levels + 2:
        error = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ValueError(f"bash で構文を読めない({error[0] if error else f'終了コード {result.returncode}'})")
    # 先頭の「名前 ()」「{」を段ごとに 2 行、末尾の「}」を段ごとに 1 行と、最後の空の行を除く。
    return lines[2 * levels:-(levels + 1)]


def code_lines(data):
    """bash が整えた本文の行を、(コードの行なら True, 行) の組で返す。

    包む段を 1 つ増やすと、コードの行は字下げが 4 つ深くなる。ヒアドキュメントの中身と、複数行の
    文字列の続きの行は、書いたまま変わらない。この差で 2 つを分ける。
    """
    one, two = bash_listing(data, 1), bash_listing(data, 2)
    if len(one) != len(two):
        raise ValueError("bash の整えた行の数が、包む段によって違う")
    lines = []
    for first, second in zip(one, two):
        if second == INDENT + first:
            lines.append((True, first))
        elif second == first:
            lines.append((False, first))
        else:
            raise ValueError(f"bash の整えた行が、包む段によって違う: {first.strip()[:80]}")
    return lines


def read_sections(data):
    """loop-selftest.sh(バイト列)の節の名前を、書いてある順に返す。数えられない形があれば ValueError。

    - bash 自身に構文を読ませる(引用符・`then` の位置・入れ子を、bash の読み方で見る)。
    - want を呼んでよいのは、最上位(字下げ 4)の `if want <節>; then`・`if want <節> || want <節>; then` だけ。
      ほかの所で want を呼ぶと、どのプロセスで流れるかを数えられない。
    - その最上位の並びは、ファイルの行頭に 1 行で書いた `if want <節>; then` の並びと同じでなければならない。
    - 1 つの節だけを見る所の名前を、プロセスに分ける単位にする。複数の名前を見る所は、その中にちょうど
      1 つ、単位の名前が無ければならない(2 つあると 2 つのプロセスで流れ、無いとどのプロセスでも流れない)。
    """
    text = data.decode("utf-8", "replace")
    written = [NAME.findall(match.group(1)) for match in map(GUARD.match, text.split("\n")) if match]
    blocks = []
    for is_code, line in code_lines(data):
        if is_code:
            if line.startswith(INDENT) and GUARD.match(line[len(INDENT):]):
                blocks.append(NAME.findall(line))
            elif CALL.search(line):
                raise ValueError(f"最上位の `if want <節>; then` の外で want を呼んでいる: {line.strip()[:100]}")
        elif TAIL_CALL.search(line):
            raise ValueError(f"最上位の `if want <節>; then` の外で want を呼んでいる: {line.strip()[:100]}")
    if blocks != written:
        index = next((i for i, (a, b) in enumerate(zip(blocks, written)) if a != b), min(len(blocks), len(written)))
        seen = " || ".join(blocks[index]) if index < len(blocks) else "無し"
        line = " || ".join(written[index]) if index < len(written) else "無し"
        raise ValueError(f"{index + 1} 番目の `if want` が、行頭に 1 行で書いた `if want <節>; then` と合わない"
                         f"(bash の読み: {seen} / 行頭の行: {line})")
    sections = []
    for names in blocks:
        if len(names) == 1 and names[0] not in sections:
            sections.append(names[0])
    if not sections:
        raise ValueError("`if want <節>; then` の行が無い")
    for names in blocks:
        units = [name for name in names if name in sections]
        if len(units) != 1:
            raise ValueError(f"`if want {' || want '.join(names)}` が、ちょうど 1 つのプロセスで流れない"
                             f"(単独の節の名前: {' '.join(units) or '無し'})")
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


def left_scratches(futures, wait=30):
    """止めたプロセスの出力にある一時フォルダのうち、まだ残っているものを (節, パス) の一覧で返す。"""
    started = [future for future in futures if not future.cancelled()]
    done, _ = concurrent.futures.wait(started, timeout=wait)
    left = []
    for future in done:
        if future.exception() is not None:
            continue
        section, code, output, elapsed, counts = future.result()
        left.extend((section, path) for path in SCRATCH.findall(output) if Path(path).is_dir())
    return left


def totals(results, problems):
    """合計の (PASS, FAIL, 節の外の PASS, 節の外の FAIL) を返す。

    節の外の確かめを 1 回分だけ数えられないときは None を返し、problems に足す。
    """
    outside = next((item for item in results if item[0] is None), None)
    if outside is None or outside[4] is None:
        problems.append("節の外だけのプロセスの件数が無い(差し引けない)")
        return None
    outside_counts = outside[4]
    outside_passed = collections.Counter(PASSED.findall(outside[2]))
    known = True
    if outside_counts[1] > 0:
        problems.append("節の外の確かめが、節の外だけのプロセスで落ちた(差し引けない)")
        known = False
    total_pass, total_fail = outside_counts
    for section, code, output, elapsed, counts in results:
        if section is None or counts is None:
            continue
        if sum(counts) - sum(outside_counts) < 1:
            problems.append(f"{section}: 節の確かめが 1 件も流れていない(節の名前が合わないか、節が流れなかった)")
        if outside_passed - collections.Counter(PASSED.findall(output)):
            problems.append(f"{section}: 節の外の確かめの結果が、節の外だけのプロセスと違う(差し引けない)")
            known = False
        total_pass += counts[0] - outside_counts[0]
        total_fail += counts[1] - outside_counts[1]
    return (total_pass, total_fail, *outside_counts) if known else None


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
        sections = read_sections(SELFTEST.read_bytes())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
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
                results.append((section, code, output, elapsed, counts))
        except KeyboardInterrupt as exc:
            signum = exc.args[0] if exc.args and exc.args[0] in run_tests.INTERRUPT_SIGNALS else signal.SIGINT
            for sig in (signal.SIGINT, *run_tests.INTERRUPT_SIGNALS):
                signal.signal(sig, signal.SIG_IGN)
            dropped = sum(future.cancel() for future in futures)
            killed = children.stop_all()
            print(f"\n中断: 動いていた {killed} 個のプロセスを止め、まだ始めていない {dropped} 個を始めずに捨てた"
                  f"(終わっていたのは {len(results)} 個)", flush=True)
            for section, path in left_scratches(futures):
                print(f"残った一時フォルダ({label(section)}。確かめてから消す): {path}", flush=True)
            return 128 + signum

    problems = []
    failed = []
    for item in results:
        section, code, output, elapsed, counts = item
        if counts is None:
            problems.append(f"{label(section)}: 結果の行が無い(終了コード {code})")
        elif code != 0 or counts[1] != 0:
            problems.append(f"{label(section)}: 失敗(終了コード {code})")
        else:
            continue
        failed.append(item)
    total = totals(results, problems)
    for section, code, output, elapsed, counts in sorted(failed, key=lambda item: jobs.index(item[0])):
        print(f"\n===== {label(section)}(終了コード {code})=====")
        print(output.rstrip())
    elapsed = time.monotonic() - started
    print()
    for problem in problems:
        print(f"異常: {problem}")
    if total is None:
        print(f"結果: 件数は不明(上の「異常:」を見る)/ {elapsed:.1f} 秒")
    else:
        print(f"節の外の確かめ(PASS {total[2]} 件 / FAIL {total[3]} 件)は、どのプロセスでも流れる。合計では 1 回分だけ数えた")
        print(f"結果: PASS {total[0]} 件 / FAIL {total[1]} 件 / {elapsed:.1f} 秒")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
