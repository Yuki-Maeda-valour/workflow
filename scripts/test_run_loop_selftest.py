"""scripts/run-loop-selftest.py の回帰テスト。使い捨てのフォルダに置いた小さな偽の loop-selftest.sh で動かす。"""

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
RUNNER = SCRIPTS / "run-loop-selftest.py"
RUN_TESTS = SCRIPTS / "run-tests.py"
SELFTEST_PATH = Path("plugins/dev-workflow/skills/ship-task/scripts/loop-selftest.sh")

# 偽の loop-selftest.sh。本物と同じ形で、節の外の確かめ(前に 2 件・後に 1 件)と、節の確かめを数える。
# 流れたときの LOOP_SELFTEST_ONLY を、環境変数で渡したファイルへ 1 行ずつ足す。
HEAD = r'''#!/usr/bin/env bash
set -uo pipefail
PASS=0
FAIL=0
RESULTS=""
ok() { PASS=$((PASS + 1)); RESULTS="$RESULTS
PASS  $1"; }
ng() { FAIL=$((FAIL + 1)); RESULTS="$RESULTS
FAIL  $1"; }
[ -z "${RUN_LOOP_SELFTEST_LOG:-}" ] || printf '%s\n' "${LOOP_SELFTEST_ONLY:-(空)}" >>"$RUN_LOOP_SELFTEST_LOG"
ok "準備: 前"
want() { [ -z "${LOOP_SELFTEST_ONLY:-}" ] && return 0; case ",$LOOP_SELFTEST_ONLY," in *",$1,"*) return 0 ;; esac; return 1; }
ok "準備: want の後"
'''

TAIL = r'''
if [ "${LOOP_SELFTEST_ONLY:-}" = "${RUN_LOOP_SELFTEST_BREAK_OUTSIDE:-(なし)}" ]; then ng "終わり(落とした)"; else ok "終わり"; fi
echo
printf '%s\n' "$RESULTS"
echo
echo "結果: PASS ${PASS} 件 / FAIL ${FAIL} 件"
[ "$FAIL" -eq 0 ]
'''

SECTIONS = r'''
if want alpha; then
ok "alpha 1"
ok "alpha 2"
fi
if want beta; then
ok "beta 1"
fi
if want gamma; then
ok "gamma 1"
fi
if want gamma || want gamma-extra; then
ok "gamma 共有"
fi
'''

# 自分(bash)と、自分が起動した子の pid を書いてから眠る(止めたときに両方が消えるかを見る)。
SLOW = r'''
if want slow-{index}; then
scratch="$(mktemp -d "$RUN_LOOP_SELFTEST_MARKS/scratch-{index}-XXXXXX")"
echo "loop-selftest: 対象 fake(scratch $scratch)"
sleep 60 &
child=$!
printf '%s %s\n' "$$" "$child" >"$RUN_LOOP_SELFTEST_MARKS/slow-{index}.tmp"
mv "$RUN_LOOP_SELFTEST_MARKS/slow-{index}.tmp" "$RUN_LOOP_SELFTEST_MARKS/slow-{index}.pids"
wait "$child"
ok "slow-{index}"
fi
'''


def process_gone(pid):
    """pid のプロセスが無いか、ゾンビ(親が回収する前の終わったプロセス)なら True。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


class RunLoopSelftestTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        self.runner = scripts / "run-loop-selftest.py"
        shutil.copy2(RUNNER, self.runner)
        shutil.copy2(RUN_TESTS, scripts / "run-tests.py")
        self.selftest = self.root / SELFTEST_PATH
        self.selftest.parent.mkdir(parents=True)
        self.log = self.root / "ran.txt"

    def write_selftest(self, sections=SECTIONS):
        self.selftest.write_text(HEAD + sections + TAIL, encoding="utf-8")

    def ran(self):
        """流れたプロセスの LOOP_SELFTEST_ONLY の値(並べ替えた一覧)。"""
        if not self.log.exists():
            return []
        return sorted(self.log.read_text(encoding="utf-8").split())

    def run_runner(self, *args, **extra_env):
        env = dict(os.environ, RUN_LOOP_SELFTEST_LOG=str(self.log), **extra_env)
        return subprocess.run([sys.executable, "-B", str(self.runner), *args], cwd=self.root,
                              capture_output=True, text=True, env=env, timeout=120)

    def run_single(self):
        """偽の loop-selftest.sh を、節を選ばずに 1 本で流す。"""
        env = {key: value for key, value in os.environ.items() if key != "LOOP_SELFTEST_ONLY"}
        result = subprocess.run(["bash", str(self.selftest)], capture_output=True, text=True, env=env, timeout=60)
        return result.stdout.strip().splitlines()[-1]

    def test_split_counts_match_single_run(self):
        self.write_selftest()
        single = self.run_single()
        self.assertEqual("結果: PASS 8 件 / FAIL 0 件", single)
        # 呼び出し元の LOOP_SELFTEST_ONLY は、各プロセスへ持ち込まない(全部の節を流す)。
        result = self.run_runner("-j", "2", LOOP_SELFTEST_ONLY="beta")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("loop-selftest.sh の 3 節を、4 個のプロセスに分けて、同時に 2 個まで流す", result.stdout)
        # どの節も、ちょうど 1 つのプロセスで流れる。節の外だけのプロセスも 1 つ流れる。
        self.assertEqual([",", "alpha", "beta", "gamma"], self.ran())
        self.assertIn("(節の外だけ)  PASS 3 件 / FAIL 0 件", result.stdout)
        self.assertIn("alpha  PASS 5 件 / FAIL 0 件", result.stdout)
        self.assertIn("gamma  PASS 5 件 / FAIL 0 件", result.stdout)
        self.assertIn("節の外の確かめ(PASS 3 件 / FAIL 0 件)は、どのプロセスでも流れる。合計では 1 回分だけ数えた",
                      result.stdout)
        self.assertIn(single + " / ", result.stdout)
        self.assertNotIn("異常:", result.stdout)
        self.assertNotIn("=====", result.stdout)
        self.assertEqual([], list(self.root.rglob("__pycache__")))

    def test_failure_shows_output_and_exits_1(self):
        self.write_selftest(SECTIONS.replace('ok "beta 1"', 'ok "beta 1"\nng "beta の失敗"'))
        self.assertEqual("結果: PASS 8 件 / FAIL 1 件", self.run_single())
        result = self.run_runner()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("===== beta(終了コード 1)=====", result.stdout)
        self.assertIn("FAIL  beta の失敗", result.stdout)
        self.assertNotIn("===== alpha", result.stdout)
        self.assertIn("異常: beta: 失敗(終了コード 1)", result.stdout)
        self.assertIn("結果: PASS 8 件 / FAIL 1 件 / ", result.stdout)

    def test_missing_result_line_is_reported(self):
        self.write_selftest(SECTIONS.replace('ok "beta 1"', 'exit 3'))
        result = self.run_runner()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("beta  結果の行が無い", result.stdout)
        self.assertIn("異常: beta: 結果の行が無い(終了コード 3)", result.stdout)

    def test_section_without_checks_is_reported(self):
        self.write_selftest(SECTIONS + "if want empty; then\n:\nfi\n")
        result = self.run_runner()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("異常: empty: 節の確かめが 1 件も流れていない", result.stdout)
        self.assertEqual([",", "alpha", "beta", "empty", "gamma"], self.ran())

    def test_layout_that_cannot_be_split_is_reported_before_running(self):
        outside = "最上位の `if want <節>; then` の外で want を呼んでいる"
        cases = {
            "2 つの節を見る": (SECTIONS + "if want alpha || want beta; then\nok x\nfi\n",
                               "`if want alpha || want beta` が、ちょうど 1 つのプロセスで流れない(単独の節の名前: alpha beta)"),
            "単独の節が無い": (SECTIONS + "if want only-a || want only-b; then\nok x\nfi\n",
                               "`if want only-a || want only-b` が、ちょうど 1 つのプロセスで流れない(単独の節の名前: 無し)"),
            "elif": (SECTIONS + "if [ -n x ]; then\n:\nelif want beta; then\nok x\nfi\n", outside),
            "字下げした if": (SECTIONS + "if want alpha; then\n  if want beta; then ok x; fi\nfi\n", outside),
            "名前を引用符で囲む": (SECTIONS + 'if want "delta"; then\nok x\nfi\n', outside),
            "名前を変数で渡す": (SECTIONS + 's=delta\nif want "$s"; then\nok x\nfi\n', outside),
            "次の行の then": (SECTIONS + "if want delta\nthen\nok x\nfi\n",
                              "5 番目の `if want` が、行頭に 1 行で書いた `if want <節>; then` と合わない"
                              "(bash の読み: delta / 行頭の行: 無し)"),
            "行頭の入れ子": (SECTIONS.replace('ok "beta 1"\n', 'ok "beta 1"\nif want delta; then\nok x\nfi\n'), outside),
            "&& で呼ぶ": (SECTIONS + "want delta && ok x\n", outside),
            "文字列の続きの後で呼ぶ": (SECTIONS + 'x="a\nb" && want delta\n', outside),
            "構文の誤り": (SECTIONS + "if want delta; then\n", "bash で構文を読めない"),
            "節が無い": ("\n", "`if want <節>; then` の行が無い"),
        }
        for name, (sections, reason) in cases.items():
            with self.subTest(case=name):
                self.write_selftest(sections)
                result = self.run_runner()
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn("異常: loop-selftest.sh の節を読めない: " + reason, result.stdout)
                self.assertEqual([], self.ran())

    def test_want_inside_heredoc_and_variables_is_not_a_call(self):
        # ヒアドキュメントの中の `want = ...`・`local want`・`$want` は、want の呼び出しではない。
        self.write_selftest(SECTIONS + r'''
check_want() { local want got; want="$1"; got="$2"; [ "$want" = "$got" ]; }
python3 - <<'PY' >/dev/null
want = 1
print(want)
PY
if check_want a a; then ok "変数の want"; fi
''')
        self.assertEqual("結果: PASS 9 件 / FAIL 0 件", self.run_single())
        result = self.run_runner()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("結果: PASS 9 件 / FAIL 0 件 / ", result.stdout)

    def test_outside_check_failing_only_in_outside_process_stops_subtraction(self):
        self.write_selftest()
        result = self.run_runner(RUN_LOOP_SELFTEST_BREAK_OUTSIDE=",")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("異常: 節の外の確かめが、節の外だけのプロセスで落ちた(差し引けない)", result.stdout)
        # 差し引きをやめる(合計の FAIL が負にならない)。最後の行は件数を出さない。
        self.assertTrue(result.stdout.strip().splitlines()[-1].startswith("結果: 件数は不明(上の「異常:」を見る)/ "),
                        result.stdout)
        self.assertNotIn("FAIL -", result.stdout)

    def test_outside_check_failing_only_in_one_section_process_stops_subtraction(self):
        self.write_selftest()
        result = self.run_runner(RUN_LOOP_SELFTEST_BREAK_OUTSIDE="beta")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("異常: beta: 失敗(終了コード 1)", result.stdout)
        self.assertIn("異常: beta: 節の外の確かめの結果が、節の外だけのプロセスと違う(差し引けない)", result.stdout)
        self.assertIn("FAIL  終わり(落とした)", result.stdout)
        self.assertTrue(result.stdout.strip().splitlines()[-1].startswith("結果: 件数は不明(上の「異常:」を見る)/ "),
                        result.stdout)

    def test_only_named_sections_run(self):
        self.write_selftest()
        result = self.run_runner("beta", "beta")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([",", "beta"], self.ran())
        self.assertIn("結果: PASS 4 件 / FAIL 0 件 / ", result.stdout)

    def test_argument_errors_exit_2_without_running(self):
        self.write_selftest()
        for args in (("delta",), ("gamma-extra",), ("-j", "0"), ("--log-dir", str(self.root / "missing"))):
            with self.subTest(args=args):
                result = self.run_runner(*args)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertEqual([], self.ran())

    def test_log_dir_keeps_each_output(self):
        self.write_selftest()
        logs = self.root / "logs"
        logs.mkdir()
        result = self.run_runner("--log-dir", str(logs))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["_outside.log", "alpha.log", "beta.log", "gamma.log"], sorted(p.name for p in logs.iterdir()))
        self.assertIn("PASS  gamma 共有", (logs / "gamma.log").read_text(encoding="utf-8"))
        self.assertNotIn("PASS  gamma 共有", (logs / "_outside.log").read_text(encoding="utf-8"))

    def test_interrupt_stops_running_processes_and_skips_queued_ones(self):
        marks = self.root / "marks"
        marks.mkdir()
        self.write_selftest("".join(SLOW.replace("{index}", str(index)) for index in range(3)))
        env = dict(os.environ, RUN_LOOP_SELFTEST_LOG=str(self.log), RUN_LOOP_SELFTEST_MARKS=str(marks))
        runner = subprocess.Popen([sys.executable, "-B", str(self.runner), "-j", "1"], cwd=self.root, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        pids = []
        try:
            deadline = time.monotonic() + 60
            while not list(marks.glob("*.pids")) and time.monotonic() < deadline:
                time.sleep(0.05)
            started = list(marks.glob("*.pids"))
            self.assertEqual(["slow-0.pids"], [path.name for path in started])
            pids = [int(pid) for pid in started[0].read_text(encoding="utf-8").split()]
            self.addCleanup(self.kill_leftovers, pids)
            runner.send_signal(signal.SIGTERM)
            output, _ = runner.communicate(timeout=60)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.communicate()
        self.assertEqual(143, runner.returncode, output)
        # 節の外だけのプロセスは終わっていた。動いていた 1 つを止め、待ち行列の 2 つは始めていない。
        self.assertIn("中断: 動いていた 1 個のプロセスを止め、まだ始めていない 2 個を始めずに捨てた(終わっていたのは 1 個)",
                      output)
        self.assertNotIn("結果:", output)
        self.assertEqual([",", "slow-0"], self.ran())
        self.assertEqual(["slow-0.pids"], [path.name for path in marks.glob("*.pids")])
        # 止めたプロセスの一時フォルダは、後片付けが走らずに残る。残っているものを表示する。
        scratches = list(marks.glob("scratch-0-*"))
        self.assertEqual(1, len(scratches), scratches)
        self.assertIn(f"残った一時フォルダ(slow-0。確かめてから消す): {scratches[0]}", output)
        deadline = time.monotonic() + 15
        while not all(process_gone(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([], [pid for pid in pids if not process_gone(pid)], pids)

    @staticmethod
    def kill_leftovers(pids):
        """テストのための後片付け。検査の後に addCleanup で走り、残った子孫を止める。"""
        for pid in pids:
            if not process_gone(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class RealSelftestTest(unittest.TestCase):
    """本物の loop-selftest.sh を、節に分けて流せる形のままにしておく(流しはしない)。"""

    def test_every_guard_runs_in_exactly_one_process(self):
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("run_loop_selftest", RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = module.SELFTEST.read_bytes()
        text = data.decode("utf-8")
        sections = module.read_sections(data)
        self.assertGreater(len(sections), 1)
        for name in sections:
            self.assertIn(f"\nif want {name}", text)
        # 重みの表は、実在する節だけを載せる(節の名前を変えたら、表も直す)。
        self.assertEqual([], sorted(set(module.WEIGHTS) - set(sections)))


if __name__ == "__main__":
    unittest.main()
