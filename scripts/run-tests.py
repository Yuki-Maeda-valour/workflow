#!/usr/bin/env python3
"""scripts/ の回帰テスト(test_*.py)を、ファイルごとに別のプロセスへ分けて並べて流す。

使い方(リポジトリ直下で実行する):
  python3 -B scripts/run-tests.py                       # 全件
  python3 -B scripts/run-tests.py test_validate.py      # 関係するファイルだけ(複数可)
  python3 -B scripts/run-tests.py -j 4                  # 同時に流すプロセスの数(既定は使える CPU の数の半分)

- 1 つのファイルを 1 つのプロセスで流す。重いファイル(SHARDS)は、テストを順番に配って
  複数のプロセスに分ける。各プロセスは `python3 -m unittest discover -s scripts -p <file>` と
  同じ読み込み方をする。子プロセスは、いつも -B(__pycache__ を書かない)で起動する。
- テストは、ファイルが違えば別のプロセスで動く。環境変数・カレントディレクトリ・
  読み込んだモジュールは、プロセスの間で共有しない。
- 終了コード: 全部のプロセスが成功なら 0。失敗・エラー・読み込みの失敗・件数の食い違いが
  1 つでもあれば 1。引数の誤りは 2。止めたときは 128 + シグナルの番号
  (Ctrl-C の SIGINT は 130、SIGTERM は 143、端末を閉じたときなどの SIGHUP は 129)。
- 止めたときは、まだ始めていないプロセスを始めず、動いているプロセスをその子プロセスごと止める
  (各プロセスを別のプロセスグループで起動し、グループごとに止める)。
  グループへは SIGINT・SIGTERM・SIGKILL の順に送る。SIGINT なら Python の子プロセスが後片付け
  (一時フォルダを消すなど)をしてから終われる。
- 失敗したプロセスの出力は、最後にまとめて表示する。
- 子プロセスの起動・止め方・並べ方・失敗の出力は、同じフォルダの run_units.py が持つ(run-loop-selftest.py・run-all.py と共通)。
"""

import argparse
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

# 子プロセスの起動・止め方・並べ方は run_units.py に持たせる(読み込む前に、__pycache__ を書かないようにする)。
sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import run_units  # noqa: E402

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

# 先に始める順の重み(秒)。値は、ファイルごとのプロセスの時間の和(2026-11-06、Ubuntu 22.04 の容器・git 2.56.0・
# Python 3.10.12・16 CPU。6 本を同時に流した全回帰テストの中での測定。0 秒のものは 1)。
# 分けたファイルの 1 プロセスの重みは「ファイルの重み ÷ 分ける数」。
# 載っていないファイルは、既定の 200 秒(run_units.DEFAULT_WEIGHT)で、早めに始める。
WEIGHTS = {
    "test_loop_startup.py": 1214,
    "test_validate.py": 951,
    "test_review_guard.py": 254,
    "test_implementation_git.py": 216,
    "test_diagnostic_matrix.py": 119,
    "test_host_check.py": 114,
    "test_integrate_guard.py": 91,
    "test_diagnostic_submodules.py": 82,
    "test_shell_requirements.py": 78,
    "test_diagnostic_review_regressions.py": 69,
    "test_diagnostic_formats.py": 63,
    "test_issue_body.py": 54,
    "test_task_digest.py": 50,
    "test_diagnostic_input_boundaries.py": 47,
    "test_environment_guard.py": 44,
    "test_run_loop_selftest.py": 39,
    "test_git_config_digest.py": 36,
    "test_run_tests.py": 32,
    "test_pr_procedure.py": 29,
    "test_diagnostic_protection.py": 27,
    "test_loop_state.py": 26,
    "test_restore_taskmd.py": 25,
    "test_diagnostic_view_edges.py": 19,
    "test_diagnostic_io_boundaries.py": 16,
    "test_diagnostic_publication.py": 16,
    "test_loop_task_text.py": 13,
    "test_pending_implementation.py": 13,
    "test_diagnostic_contexts.py": 10,
    "test_diagnostic_state.py": 10,
    "test_diagnostic_runtime.py": 9,
    "test_loop_child_output.py": 9,
    "test_candidate_keys.py": 7,
    "test_origin_repo.py": 7,
    "test_implementation_git_input.py": 6,
    "test_publish_guard.py": 6,
    "test_check_process.py": 5,
    "test_loop_permission.py": 4,
    "test_setup.py": 4,
    "test_adopt_candidate.py": 2,
    "test_loop_supervisor.py": 2,
    "test_secret_profiles.py": 2,
    "test_task_directory.py": 2,
    "test_task_store.py": 2,
    "test_diagnostic_contract.py": 1,
    "test_diagnostic_git.py": 1,
    "test_diagnostic_reftable.py": 1,
    "test_diff_snapshot_profile_union.py": 1,
    "test_export_doc.py": 1,
    "test_host_argv.py": 1,
    "test_init_task_store.py": 1,
    "test_issue_workflow_contract.py": 1,
    "test_loop_helper_commands.py": 1,
    "test_safe_git_callers.py": 1,
    "test_ship_integration_contract.py": 1,
    "test_writing_rules.py": 1,
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

    CPU の数だけ並べると、test_loop_startup.py のテストが中の時間切れ(当時 100 秒)に当たって落ちた
    (2026-11-05、8 コア 16 スレッドの作業機の容器。順に流すと 1 通り 33 秒ほどのテスト)。
    この上限は #352 で既定 300 秒に延ばし、LOOP_STARTUP_CASE_TIMEOUT で変えられるようにした。
    """
    return max(1, run_units.cpu_count() // 2)


def run_job(job, workdir, flags, children):
    """1 つのジョブ(ファイル・シャードの番号・数)を流し、(job, 終了コード, 出力, 秒, 報告) を返す。"""
    name, index, count = job
    report = Path(workdir) / f"{name}.{index}.json"
    command = [sys.executable, *flags, str(Path(__file__).resolve()),
               "--worker", name, "--shard", f"{index}/{count}", "--report", str(report)]
    code, output, elapsed = run_units.run_command(children, command)
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    return job, code, output, elapsed, data


def label(job):
    name, index, count = job
    return name if count == 1 else f"{name} [{index + 1}/{count}]"


def key_of(job):
    """重みの記録の鍵。分けたときは番号(1 から)と数を付ける。"""
    name, index, count = job
    return f"unittest:{name}" if count == 1 else f"unittest:{name}[{index + 1}/{count}]"


def make_jobs(files):
    return [(name, index, SHARDS.get(name, 1)) for name in files for index in range(SHARDS.get(name, 1))]


def job_ok(result):
    job, code, output, elapsed, data = result
    return code == 0 and data is not None and data["ok"]


def describe(result):
    """進み具合の行の後半(件数)と成否。"""
    data = result[4]
    return ("件数不明" if data is None else f"{data['run']} 件"), job_ok(result)


# 子プロセスは、親の -B の有無にかかわらず、いつも -B で起動する。
# -B が無いと、子プロセスが配布のコードを読み込むときに plugins/ の下へ __pycache__ を書く。
# 同時に動く別の子プロセスが配布の木を shutil.copytree でコピーすると、書きかけのファイルに当たって落ちうる。
# 環境変数(PYTHONDONTWRITEBYTECODE)にはしない。テストが起動するプロセスにまで効き、
# 「.pyc を作らない」ことを確かめるテストが意味を失うため。
# テストが起動する python(-B なし)は、今も plugins/ の下へ __pycache__ を書くことがある。
# そのため、配布の木をコピーするテストは __pycache__ を除いてコピーする。
CHILD_FLAGS = ["-B"]


def build_units(jobs, workdir, flags=CHILD_FLAGS):
    """ジョブから単位を作る。重みは、表の「ファイルの重み ÷ 分ける数」(表に無ければ None)。"""
    units = []
    for job in jobs:
        weight = WEIGHTS.get(job[0])
        weight = None if weight is None else weight / job[2]
        units.append(run_units.Unit(
            key=key_of(job), label=f"{label(job)}", in_slot=True, weight=weight, table_weight=weight,
            source="既定" if weight is None else "表",
            run=lambda children, job=job: run_job(job, workdir, flags, children),
            describe=describe))
    return units


def summarize(results):
    """結果の一覧から (problems, 件数の合計) を作る。分けたファイルは、配った件数の合計が総数と同じかを照らす。"""
    problems = []
    totals = dict.fromkeys(("run", "failures", "errors", "skipped", "expected_failures", "unexpected_successes"), 0)
    by_file = {}
    for result in results:
        job, code, output, elapsed, data = result
        if data is None:
            problems.append(f"{label(job)}: 結果の報告が無い(終了コード {code})")
        else:
            for key in totals:
                totals[key] += data[key]
            by_file.setdefault(job[0], []).append(data)
        if not job_ok(result):
            problems.append(f"{label(job)}: 失敗(終了コード {code})")
    # 分けたファイルは、全部のプロセスが同じ総数を見て、配った件数の合計が総数と同じことを確かめる。
    for name, reports in by_file.items():
        expected = SHARDS.get(name, 1)
        totals_seen = {data["total"] for data in reports}
        chosen = sum(data["chosen"] for data in reports)
        if len(reports) != expected or len(totals_seen) != 1 or chosen != totals_seen.pop():
            problems.append(f"{name}: 分けたプロセスの件数が合わない")
    return problems, totals


def failures_of(results):
    """失敗したジョブの (表示名, 終了コード, 出力) の一覧(ファイル・シャードの順)。"""
    failed = [result for result in results if not job_ok(result)]
    return [(label(result[0]), result[1], result[2]) for result in sorted(failed, key=lambda item: item[0])]


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

    jobs = make_jobs(files)
    started = time.monotonic()
    print(f"{len(files)} ファイル・{len(jobs)} プロセスを、同時に {args.jobs} 個まで流す", flush=True)
    with tempfile.TemporaryDirectory(prefix="run-tests-") as workdir:
        # 重いものから始める(全体の時間が最後に始めた重いファイルで延びないように)。
        results, code = run_units.run_units(build_units(jobs, workdir), args.jobs)
    if code is not None:
        return code

    problems, totals = summarize(results)
    run_units.print_failures(failures_of(results))
    elapsed = time.monotonic() - started
    print()
    for problem in problems:
        print(f"異常: {problem}")
    print(f"結果: {totals['run']} 件 / 失敗 {totals['failures']} 件 / エラー {totals['errors']} 件 / "
          f"skip {totals['skipped']} 件 / {elapsed:.1f} 秒")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
