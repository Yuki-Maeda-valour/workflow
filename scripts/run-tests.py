#!/usr/bin/env python3
"""scripts/ の回帰テスト(test_*.py)を、ファイルごとに別のプロセスへ分けて並べて流す。

使い方(リポジトリ直下で実行する):
  python3 -B scripts/run-tests.py                       # 全件
  python3 -B scripts/run-tests.py test_validate.py      # 関係するファイルだけ(複数可)
  python3 -B scripts/run-tests.py -j 4                  # 同時に流すプロセスの数(既定は使える CPU の数の半分)

- 1 つのファイルを 1 つのプロセスで流す。重いファイル(SHARDS)は、テストを順番に配って
  複数のプロセスに分ける。各プロセスは `python3 -m unittest discover -s scripts -p <file>` と
  同じ読み込み方をする。子のプロセスは、いつも -B(__pycache__ を書かない)で起動する。
- テストは、ファイルが違えば別のプロセスで動く。環境変数・カレントディレクトリ・
  読み込んだモジュールは、プロセスの間で共有しない。
- 終了コード: 全部のプロセスが成功なら 0。失敗・エラー・読み込みの失敗・件数の食い違いが
  1 つでもあれば 1。引数の誤りは 2。止めたときは 128 + シグナルの番号
  (Ctrl-C の SIGINT は 130、SIGTERM は 143、端末を閉じたときなどの SIGHUP は 129)。
- 止めたときは、まだ始めていないプロセスを始めず、動いているプロセスをその子ごと止める
  (各プロセスを別のプロセスグループで起動し、グループごとに止める)。
  グループへは SIGINT・SIGTERM・SIGKILL の順に送る。SIGINT なら Python の子が後片付け
  (一時フォルダを消すなど)をしてから終われる。
- 失敗したプロセスの出力は、最後にまとめて表示する。
"""

import argparse
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent

# 重いファイルを分けるプロセスの数と、先に始める順の重み(秒)。
# 値は、順に流したときの 1 ファイルの時間(2026-11-05、Ubuntu 22.04 の容器・git 2.56.0・Python 3.10.12・16 CPU)。
# 1 つのプロセスの時間が、全体を CPU の数で割った時間(約 160 秒)を大きく超えないように分ける。
# テストは読み込んだ順に 1 つずつ配る(1 つのテストより細かくは分けられない)。
# test_pending_implementation.py は分けない。実の HOME の下の同じ置き場(~/.local/state/dev-workflow)を
# 無ければ作り、空なら消すので、同じファイルのテストを同時に流すと取り合う。
SHARDS = {
    "test_validate.py": 12,
    "test_loop_startup.py": 12,
    "test_review_guard.py": 2,
    "test_implementation_git.py": 2,
    "test_host_check.py": 2,
}

# 載っていないファイルは軽いものとして、後に回す。
WEIGHTS = {
    "test_validate.py": 1142,
    "test_loop_startup.py": 711,
    "test_review_guard.py": 98,
    "test_implementation_git.py": 75,
    "test_host_check.py": 56,
    "test_diagnostic_matrix.py": 52,
    "test_diagnostic_submodules.py": 41,
    "test_diagnostic_review_regressions.py": 36,
    "test_shell_requirements.py": 34,
    "test_diagnostic_formats.py": 34,
    "test_git_config_digest.py": 32,
    "test_diagnostic_input_boundaries.py": 29,
    "test_restore_taskmd.py": 27,
    "test_task_digest.py": 26,
    "test_issue_body.py": 25,
    "test_environment_guard.py": 22,
    "test_diagnostic_protection.py": 18,
    "test_loop_state.py": 18,
    "test_diagnostic_view_edges.py": 14,
    "test_diagnostic_publication.py": 12,
    "test_diagnostic_io_boundaries.py": 11,
    "test_loop_task_text.py": 11,
    "test_pending_implementation.py": 10,
}


def parse_shard(text):
    index, _, count = text.partition("/")
    index, count = int(index), int(count)
    if count < 1 or not 0 <= index < count:
        raise ValueError(text)
    return index, count


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def worker(name, shard, report):
    """1 つのファイルのテストのうち、shard に当たるものを流し、件数を report に書く。"""
    index, count = shard
    # `python3 -m unittest discover -s scripts` と同じく、scripts/ を読み込みの起点にする。
    suite = unittest.defaultTestLoader.discover(str(SCRIPTS), pattern=name, top_level_dir=str(SCRIPTS))
    tests = list(flatten(suite))
    chosen = tests[index::count]
    # unittest の main と同じく、警告の表示の設定が無ければ 'default' にする。
    runner = unittest.TextTestRunner(stream=sys.stderr, warnings=None if sys.warnoptions else "default")
    result = runner.run(unittest.TestSuite(chosen))
    data = {
        "total": len(tests),
        "chosen": len(chosen),
        "run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "ok": result.wasSuccessful(),
    }
    Path(report).write_text(json.dumps(data), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


def resolve_files(names):
    if not names:
        return sorted(path.name for path in SCRIPTS.glob("test_*.py"))
    files = []
    for name in names:
        base = Path(name).name
        if not (base.startswith("test_") and base.endswith(".py") and (SCRIPTS / base).is_file()):
            raise ValueError(f"scripts/ に無いテストのファイル: {name}")
        if base not in files:
            files.append(base)
    return files


def default_jobs():
    """使える CPU の数の半分(1 以上)。

    CPU の数だけ並べると、test_loop_startup.py のテストが中の時間切れ(100 秒)に当たって落ちた
    (2026-11-05、8 コア 16 スレッドの作業機の容器。順に流すと 1 通り 33 秒ほどのテスト)。
    """
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 1
    return max(1, cpus // 2)


class Interrupted(Exception):
    """止める合図を受けた後に、プロセスを始めようとした。"""


class Children:
    """動いている子のプロセス。止める合図の後は、新しく始めない。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = set()
        self.stopped = False

    def start(self, command, env=None):
        """env を渡すと、子の環境変数をそれにする(既定は親と同じ。run-loop-selftest.py が使う)。"""
        with self.lock:
            if self.stopped:
                raise Interrupted()
            # 別のプロセスグループで起動する(端末の Ctrl-C は親だけが受け、子はグループごと止める)。
            process = subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors="replace", start_new_session=True, env=env)
            self.running.add(process)
            return process

    def finish(self, process):
        with self.lock:
            self.running.discard(process)

    def stop_all(self, grace=5.0):
        """新しく始めないようにし、動いている子をグループごと止める。止めた数を返す。

        グループへ SIGINT・SIGTERM・SIGKILL の順に送り、それぞれの後で、グループ全体が消えるまで
        最大 grace 秒待つ。次の合図は、まだ消えないグループにだけ送る。
        - SIGINT から始める: Python の子は KeyboardInterrupt で終わり、一時フォルダなどを片付けられる
          (SIGTERM では片付けずに終わる)。
        - 直接の子が終わっても待ちを打ち切らない: グループの中の孫(入れ子の run-tests.py など)が、
          別のグループで起動した自分の子を止めて回収する時間を残すため。
        - 限界: 入れ子の実行で、内側の別のグループの子孫が合図を無視すると、外側の SIGKILL の後に
          残ることがある。そのときは、残ったプロセスを pid を指定して止める(別のセッションの子孫を
          追う仕組みは入れていない)。
        """
        with self.lock:
            self.stopped = True
            running = list(self.running)
        groups = [process.pid for process in running]
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            for pgid in groups:
                if group_alive(pgid):
                    try:
                        os.killpg(pgid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and any(group_alive(pgid) for pgid in groups):
                time.sleep(0.05)
        return len(running)


def group_alive(pgid):
    """プロセスグループに、まだプロセスが残っていれば True(止められないものは待たない)。"""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def run_job(job, workdir, flags, children):
    name, index, count = job
    report = Path(workdir) / f"{name}.{index}.json"
    command = [sys.executable, *flags, str(Path(__file__).resolve()),
               "--worker", name, "--shard", f"{index}/{count}", "--report", str(report)]
    started = time.monotonic()
    process = children.start(command)
    try:
        output, _ = process.communicate()
    finally:
        children.finish(process)
    elapsed = time.monotonic() - started
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    return job, process.returncode, output, elapsed, data


# Ctrl-C(SIGINT。KeyboardInterrupt になる)のほかに受けて止めるシグナル。子は別のセッションにいるので、
# 端末を閉じたときの SIGHUP は親だけに届く。親だけが終わって子が孤児にならないよう、受けて止める。
INTERRUPT_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name))


def raise_interrupt(signum, frame):
    raise KeyboardInterrupt(signum)


def label(job):
    name, index, count = job
    return name if count == 1 else f"{name} [{index + 1}/{count}]"


def main(argv=None):
    parser = argparse.ArgumentParser(description="scripts/ の回帰テストを、ファイルごとに並べて流す。")
    parser.add_argument("files", nargs="*", help="流すテストのファイル(既定は scripts/test_*.py の全部)")
    parser.add_argument("-j", "--jobs", type=int, default=default_jobs(),
                        help="同時に流すプロセスの数(既定は使える CPU の数の半分)")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--shard", default="0/1", help=argparse.SUPPRESS)
    parser.add_argument("--report", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        try:
            shard = parse_shard(args.shard)
        except ValueError:
            parser.error(f"--shard の形が不正: {args.shard}")
        return worker(args.worker, shard, args.report)

    if args.jobs < 1:
        parser.error("-j は 1 以上にする")
    try:
        files = resolve_files(args.files)
    except ValueError as exc:
        parser.error(str(exc))

    jobs = [(name, index, SHARDS.get(name, 1)) for name in files for index in range(SHARDS.get(name, 1))]
    # 重いものから始める(全体の時間が最後に始めた重いファイルで延びないように)。
    jobs.sort(key=lambda job: (-WEIGHTS.get(job[0], 0) / job[2], job[0], job[1]))
    # 子のプロセスは、親の -B の有無にかかわらず、いつも -B で起動する。
    # -B が無いと、子が配布のコードを読み込むときに plugins/ の下へ __pycache__ を書く。
    # 同時に動く別の子が配布の木を shutil.copytree で写すと、書きかけのファイルに当たって落ちうる。
    # 環境変数(PYTHONDONTWRITEBYTECODE)にはしない。テストが起動するプロセスにまで効き、
    # 「.pyc を作らない」ことを確かめるテストが意味を失うため。
    # テストが起動する python(-B なし)は、今も plugins/ の下へ __pycache__ を書くことがある。
    # そのため、配布の木を写すテストは __pycache__ を除いて写す。
    flags = ["-B"]

    started = time.monotonic()
    results = []
    children = Children()
    # SIGTERM・SIGHUP も Ctrl-C と同じく止める(子を残して親だけが終わらないように)。
    for sig in INTERRUPT_SIGNALS:
        signal.signal(sig, raise_interrupt)
    print(f"{len(files)} ファイル・{len(jobs)} プロセスを、同時に {args.jobs} 個まで流す", flush=True)
    with tempfile.TemporaryDirectory(prefix="run-tests-") as workdir, \
            concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = []
        try:
            futures.extend(pool.submit(run_job, job, workdir, flags, children) for job in jobs)
            for future in concurrent.futures.as_completed(futures):
                job, code, output, elapsed, data = future.result()
                ok = code == 0 and data is not None and data["ok"]
                count = "件数不明" if data is None else f"{data['run']} 件"
                print(f"{'ok  ' if ok else 'FAIL'} {elapsed:7.1f} 秒  {label(job)}  {count}", flush=True)
                results.append((job, code, output, elapsed, data, ok))
        except KeyboardInterrupt as exc:
            # 待ち行列のジョブを捨て、動いているプロセスを止める。with を出るときは、止めたジョブだけを待つ。
            signum = exc.args[0] if exc.args and exc.args[0] in INTERRUPT_SIGNALS else signal.SIGINT
            # 止めている間の 2 回目の合図で、片付けが途中で切れないようにする。
            for sig in (signal.SIGINT, *INTERRUPT_SIGNALS):
                signal.signal(sig, signal.SIG_IGN)
            dropped = sum(future.cancel() for future in futures)
            killed = children.stop_all()
            print(f"\n中断: 動いていた {killed} 個のプロセスを止め、まだ始めていない {dropped} 個を始めずに捨てた"
                  f"(終わっていたのは {len(results)} 個)", flush=True)
            return 128 + signum

    problems = []
    totals = dict.fromkeys(("run", "failures", "errors", "skipped", "expected_failures", "unexpected_successes"), 0)
    by_file = {}
    for job, code, output, elapsed, data, ok in results:
        if data is None:
            problems.append(f"{label(job)}: 結果の報告が無い(終了コード {code})")
        else:
            for key in totals:
                totals[key] += data[key]
            by_file.setdefault(job[0], []).append(data)
        if not ok:
            problems.append(f"{label(job)}: 失敗(終了コード {code})")
    # 分けたファイルは、全部のプロセスが同じ総数を見て、配った件数の合計が総数と同じことを確かめる。
    for name, reports in by_file.items():
        expected = SHARDS.get(name, 1)
        totals_seen = {data["total"] for data in reports}
        chosen = sum(data["chosen"] for data in reports)
        if len(reports) != expected or len(totals_seen) != 1 or chosen != totals_seen.pop():
            problems.append(f"{name}: 分けたプロセスの件数が合わない")

    failed = [item for item in results if not item[5]]
    for job, code, output, elapsed, data, ok in sorted(failed, key=lambda item: item[0]):
        print(f"\n===== {label(job)}(終了コード {code})=====")
        print(output.rstrip())
    elapsed = time.monotonic() - started
    print()
    for problem in problems:
        print(f"異常: {problem}")
    print(f"結果: {totals['run']} 件 / 失敗 {totals['failures']} 件 / エラー {totals['errors']} 件 / "
          f"skip {totals['skipped']} 件 / {elapsed:.1f} 秒")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
