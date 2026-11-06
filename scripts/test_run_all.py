"""scripts/run-all.py(と scripts/run_units.py)の回帰テスト。

使い捨てのフォルダに、ランナーのコピーと、小さな偽の部(test_*.py 2 つ・偽の loop-selftest.sh・偽の selftest 4 本)を
置いて、サブプロセスで流す。HOME・XDG_CACHE_HOME・TMPDIR は、使い捨てのフォルダに向ける
(本物の重みの記録・置き場には触れない)。
"""

import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
RUNNERS = ("run_units.py", "run-tests.py", "run-loop-selftest.py", "run-all.py")
SKILLS = Path("plugins/dev-workflow/skills")
LOOP = SKILLS / "ship-task/scripts/loop-selftest.sh"
GUARD = SKILLS / "do-task/scripts/implement-guard-selftest.sh"
AGENT = SKILLS / "do-task/scripts/implement-agent-selftest.sh"
REVIEW = SKILLS / "do-task/scripts/review-agent-selftest.sh"
DIFF = SKILLS / "do-task/scripts/diff-snapshot-selftest.sh"

# 重なりの確かめ: 枠の単位は、短い間だけ共有の錠(フォルダ)を持つ。枠 1 つなら、錠を取り合わない。
LOCK_SH = r'''
if [ -n "${RUN_ALL_MARKS:-}" ]; then
  if mkdir "$RUN_ALL_MARKS/lock" 2>/dev/null; then sleep 0.2; rmdir "$RUN_ALL_MARKS/lock"
  else echo overlap >>"$RUN_ALL_MARKS/overlap.log"; fi
fi
'''

PASSING = '''
import os
import unittest

def hold_slot():
    marks = os.environ.get("RUN_ALL_MARKS")
    if not marks:
        return
    import time
    try:
        os.mkdir(os.path.join(marks, "lock"))
    except FileExistsError:
        with open(os.path.join(marks, "overlap.log"), "a") as stream:
            stream.write("overlap\\n")
        return
    time.sleep(0.2)
    os.rmdir(os.path.join(marks, "lock"))

class Passing(unittest.TestCase):
    def test_one(self):
        hold_slot()

    def test_two(self):
        self.assertEqual(2, 1 + 1)

    @unittest.skip("理由")
    def test_skipped(self):
        pass
'''

OTHER = '''
import unittest

class Other(unittest.TestCase):
    def test_a(self):
        pass

    def test_b(self):
        pass
'''

FAILING = '''
import unittest

class Failing(unittest.TestCase):
    def test_fails(self):
        self.assertEqual("期待", "実際の値")
'''

# 偽の loop-selftest.sh(節の外の確かめは前に 2 件・後ろに 1 件。alpha は 2 件・beta は 1 件)。
LOOP_HEAD = r'''#!/usr/bin/env bash
set -uo pipefail
PASS=0
FAIL=0
RESULTS=""
ok() { PASS=$((PASS + 1)); RESULTS="$RESULTS
PASS  $1"; }
ng() { FAIL=$((FAIL + 1)); RESULTS="$RESULTS
FAIL  $1"; }
[ -z "${RUN_LOOP_SELFTEST_LOG:-}" ] || printf '%s\n' "${LOOP_SELFTEST_ONLY:-(空)}" >>"$RUN_LOOP_SELFTEST_LOG"
[ -z "${RUN_LOOP_SELFTEST_ENV_LOG:-}" ] || printf '%s %s\n' "${LOOP_SELFTEST_TIMEOUT:-}" "${RUN_TIMEOUT:-}" >>"$RUN_LOOP_SELFTEST_ENV_LOG"
ok "準備: 前"
want() { [ -z "${LOOP_SELFTEST_ONLY:-}" ] && return 0; case ",$LOOP_SELFTEST_ONLY," in *",$1,"*) return 0 ;; esac; return 1; }
ok "準備: want の後"
''' + LOCK_SH

LOOP_SECTIONS = r'''
if want alpha; then
ok "alpha 1"
ok "alpha 2"
fi
if want beta; then
ok "beta 1"
fi
'''

LOOP_SLOW = r'''
if want slow; then
scratch="$(mktemp -d "$RUN_ALL_MARKS/scratch-XXXXXX")"
echo "loop-selftest: 対象 fake(scratch $scratch)"
sleep 60 &
child=$!
printf '%s %s\n' "$$" "$child" >"$RUN_ALL_MARKS/slow.tmp"
mv "$RUN_ALL_MARKS/slow.tmp" "$RUN_ALL_MARKS/slow.pids"
wait "$child"
ok "slow"
fi
'''

LOOP_TAIL = r'''
ok "終わり"
echo
printf '%s\n' "$RESULTS"
echo
echo "結果: PASS ${PASS} 件 / FAIL ${FAIL} 件"
[ "$FAIL" -eq 0 ]
'''


def selftest(passed, extra="", lock=True):
    """偽の selftest。PASS を passed 件出す。extra は、結果の行の前に走る処理。lock が偽なら、枠を使わない単位。"""
    return f'''#!/usr/bin/env bash
{LOCK_SH if lock else ""}
{extra}
echo "結果: PASS {passed} 件 / FAIL 0 件"
'''


# 枠の外の偽の selftest。RUN_ALL_HANG_OUTSIDE があると、自分と子の pid を書いて眠る(止めたときの確かめ)。
OUTSIDE_HANG = r'''
if [ -n "${RUN_ALL_HANG_OUTSIDE:-}" ]; then
  sleep 60 &
  child=$!
  name="$(basename "$0")"
  printf '%s %s\n' "$$" "$child" >"$RUN_ALL_MARKS/$name.tmp"
  mv "$RUN_ALL_MARKS/$name.tmp" "$RUN_ALL_MARKS/$name.pids"
  wait "$child"
fi
'''

GUARD_UNIVERSE = '''#!/usr/bin/env bash
mode="${FAKE_GUARD_MODE:-t2}"
%(all_cases)s
count_of() {
  case "$1" in
    "") echo 8 ;;
    A) echo 2 ;; B) echo 1 ;; M) echo 2 ;; L) echo 3 ;;
    A1|A2|B1|M1|M2|L-a|L-b|L-c|X) echo 1 ;;
    *) echo 0 ;;
  esac
}
if [ "${1:-}" = "--list" ]; then
  case "$mode" in
    t2) printf 'A\\nB\\nM1\\nM2\\nL-a\\nL-b\\nL-c\\n'; exit 0 ;;
    missing) echo "ERROR: 不明な引数: --list" >&2; exit 1 ;;
    failed) echo "ERROR: 単位がケースを覆わない" >&2; exit 1 ;;
    empty) exit 0 ;;
    one0) echo A; exit 0 ;;
    dup) printf 'A\\nA\\nB\\nM1\\nM2\\nL-a\\nL-b\\nL-c\\n'; exit 0 ;;
  esac
fi
''' + LOCK_SH + r'''
if [ "$mode" = one0 ]; then echo "結果: PASS 0 件 / FAIL 0 件"; exit 0; fi
[ -z "${FAKE_GUARD_LOG:-}" ] || printf '%s\n' "${2:-(なし)}" >>"$FAKE_GUARD_LOG"
echo "結果: PASS $(count_of "${2:-}") 件 / FAIL 0 件"
'''

ALL_CASES_STANDARD = 'ALL_CASES="A1 A2 B1\n\tM1 M2 L"'
ALL_CASES_ODD = "ALL_CASES=$(printf 'A1 A2 B1 M1 M2 L')"
ALL_CASES_L_ONLY = 'ALL_CASES="A1 A2 B1 M1 M2 L-a L-b L-c"'


def process_gone(pid):
    """pid のプロセスが無いか、ゾンビなら True。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def load_run_units():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("run_units_under_test", SCRIPTS / "run_units.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RunAllTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        for name in RUNNERS:
            shutil.copy2(SCRIPTS / name, self.scripts / name)
        self.home = self.root / "home"
        self.cache = self.root / "cache"
        self.tmp = self.root / "tmp"
        self.marks = self.root / "marks"
        self.logs = self.root / "logs"
        for folder in (self.home, self.cache, self.tmp, self.marks, self.logs):
            folder.mkdir()
        self.weights = self.cache / "workflow-run-all" / "weights.json"
        self.write_test("test_a.py", PASSING)
        self.write_test("test_b.py", OTHER)
        self.write_loop()
        self.write_guard()
        self.write(AGENT, selftest(4, OUTSIDE_HANG, lock=False))
        self.write(REVIEW, selftest(3, OUTSIDE_HANG, lock=False))
        self.write(DIFF, selftest(5, 'if [ "${FAKE_DIFF_MODE:-}" = nocount ]; then exit 3; fi'))

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_test(self, name, text):
        (self.scripts / name).write_text(textwrap.dedent(text), encoding="utf-8")

    def write_loop(self, sections=LOOP_SECTIONS):
        self.write(LOOP, LOOP_HEAD + sections + LOOP_TAIL)

    def write_guard(self, all_cases=ALL_CASES_STANDARD):
        self.write(GUARD, GUARD_UNIVERSE.replace("%(all_cases)s", all_cases))

    def env(self, **extra):
        env = dict(os.environ, HOME=str(self.home), XDG_CACHE_HOME=str(self.cache), TMPDIR=str(self.tmp),
                   RUN_ALL_MARKS=str(self.marks), RUN_LOOP_SELFTEST_LOG=str(self.root / "loop.log"),
                   FAKE_GUARD_LOG=str(self.root / "guard.log"))
        for name in ("LOOP_SELFTEST_ONLY", "LOOP_SELFTEST_TIMEOUT", "RUN_TIMEOUT", "XDG_STATE_HOME"):
            env.pop(name, None)
        env.update(extra)
        return env

    def run_all(self, *args, flags=("-B",), **extra):
        return subprocess.run([sys.executable, *flags, str(self.scripts / "run-all.py"), *args], cwd=self.root,
                              capture_output=True, text=True, env=self.env(**extra), timeout=180)

    def lines(self, name):
        path = self.root / name
        return sorted(path.read_text(encoding="utf-8").split()) if path.exists() else []

    def fake_git(self, version):
        """PATH の先頭に、`git --version` だけ偽の版を返す git を置き、その PATH を返す。"""
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        real = shutil.which("git")
        script = bindir / "git"
        script.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "git version {version}"; exit 0; fi\n'
                          f'exec "{real}" "$@"\n', encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return f"{bindir}{os.pathsep}{os.environ['PATH']}"

    # ---- 全部合格 ----

    def test_all_pass_prints_six_result_lines_and_final_line(self):
        result = self.run_all("-j", "4")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        out = result.stdout
        order = [
            "結果(run-tests.py の分): 5 件 / 失敗 0 件 / エラー 0 件 / skip 1 件",
            "結果(run-loop-selftest.py の分): PASS 6 件 / FAIL 0 件",
            "結果(review-agent-selftest.sh): PASS 3 件 / FAIL 0 件",
            "結果(implement-agent-selftest.sh): PASS 4 件 / FAIL 0 件",
            "結果(diff-snapshot-selftest.sh): PASS 5 件 / FAIL 0 件",
            "結果(implement-guard-selftest.sh): PASS 8 件 / FAIL 0 件",
        ]
        tail = out.strip().splitlines()[-7:]
        self.assertEqual(order, tail[:6], out)
        self.assertRegex(tail[6], r"^結果: 合格 / \d+\.\d 秒$")
        self.assertNotIn("異常:", out)
        self.assertNotIn("=====", out)
        # 単位は 2(unittest)+ 3(loop)+ 7(guard)+ 1(diff)+ 2(枠の外)= 15。
        self.assertIn("15 個の単位を、同時に 4 個まで流す(枠の外: review-agent-selftest.sh・implement-agent-selftest.sh)", out)
        self.assertIn("unittest test_a.py  3 件", out)
        self.assertIn("loop-selftest (節の外だけ)  PASS 3 件 / FAIL 0 件", out)
        self.assertIn("implement-guard-selftest.sh --only L-a  PASS 1 件 / FAIL 0 件", out)
        # 子の環境に、無いときだけ上限が入る(loop の各プロセス)。
        self.assertEqual([], list(self.root.rglob("__pycache__")))

    def test_loop_environment_defaults_are_added_only_when_missing(self):
        log = self.root / "env.log"
        self.assertEqual(0, self.run_all(RUN_LOOP_SELFTEST_ENV_LOG=str(log)).returncode)
        self.assertEqual({"6000 600"}, set(log.read_text(encoding="utf-8").splitlines()))
        log.unlink()
        self.assertEqual(0, self.run_all(RUN_LOOP_SELFTEST_ENV_LOG=str(log), LOOP_SELFTEST_TIMEOUT="77").returncode)
        self.assertEqual({"77 600"}, set(log.read_text(encoding="utf-8").splitlines()))

    def test_parent_without_dash_b_writes_no_pycache(self):
        result = self.run_all("-j", "4", flags=())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], list(self.root.rglob("__pycache__")))
        self.assertEqual([], list(self.root.rglob("*.pyc")))

    # ---- 失敗 ----

    def test_failures_are_shown_at_the_end_and_exit_1(self):
        self.write_test("test_fail.py", FAILING)
        result = self.run_all("-j", "4", FAKE_DIFF_MODE="nocount")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        out = result.stdout
        self.assertIn("===== unittest test_fail.py(終了コード 1)=====", out)
        self.assertIn("'期待' != '実際の値'", out)
        self.assertIn("異常: test_fail.py: 失敗(終了コード 1)", out)
        # 結果の行が無い selftest も異常になる。
        self.assertIn("===== diff-snapshot-selftest.sh(終了コード 3)=====", out)
        self.assertIn("異常: diff-snapshot-selftest.sh: 結果の行が無い(終了コード 3)", out)
        self.assertIn("結果(diff-snapshot-selftest.sh): 件数は不明(上の「異常:」を見る)", out)
        self.assertNotIn("===== unittest test_a.py", out)
        self.assertRegex(out.strip().splitlines()[-1], r"^結果: 不合格\(上の「異常:」を見る\)/ \d+\.\d 秒$")

    # ---- implement-guard の分け方 ----

    def guard_run(self, mode, all_cases=ALL_CASES_STANDARD, *args):
        self.write_guard(all_cases)
        (self.root / "guard.log").unlink(missing_ok=True)
        result = self.run_all("-j", "4", *args, FAKE_GUARD_MODE=mode)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("結果(implement-guard-selftest.sh): PASS 8 件 / FAIL 0 件", result.stdout)
        return result.stdout

    def test_guard_runs_each_line_of_list(self):
        out = self.guard_run("t2")
        self.assertEqual(sorted(["A", "B", "M1", "M2", "L-a", "L-b", "L-c"]), self.lines("guard.log"))
        self.assertNotIn("警告:", out.replace("警告: git", ""))

    def test_guard_without_list_uses_initials_without_warning(self):
        out = self.guard_run("missing")
        self.assertEqual(["A", "B", "L", "M"], self.lines("guard.log"))
        self.assertNotIn("implement-guard-selftest.sh を", out)

    def test_guard_with_failing_list_uses_initials_and_warns_twice(self):
        out = self.guard_run("failed")
        self.assertEqual(["A", "B", "L", "M"], self.lines("guard.log"))
        warning = "警告: implement-guard-selftest.sh を 頭文字の一覧 で流した(--list が終了コード 1 で終わった(ERROR: 単位がケースを覆わない))"
        self.assertEqual(2, out.count(warning), out)

    def test_guard_with_empty_list_or_duplicate_lines_uses_initials_and_warns(self):
        for mode, reason in (("empty", "--list が 1 行も返さなかった"), ("dup", "--list が同じ行を 2 つ以上返した")):
            with self.subTest(mode=mode):
                out = self.guard_run(mode)
                self.assertEqual(["A", "B", "L", "M"], self.lines("guard.log"))
                self.assertEqual(2, out.count(f"を 頭文字の一覧 で流した({reason})"), out)

    def test_guard_without_list_and_all_cases_runs_whole_and_warns(self):
        out = self.guard_run("missing", all_cases="")
        self.assertEqual(["(なし)"], self.lines("guard.log"))
        self.assertEqual(2, out.count("警告: implement-guard-selftest.sh を 1 本 で流した("), out)
        self.assertIn("implement-guard-selftest.sh  PASS 8 件 / FAIL 0 件", out)

    def test_guard_initials_that_do_not_cover_all_cases_run_whole(self):
        # `L-a` だけがあり、ID `L` が無いと、頭文字 L の単位(ID L だけに当たる)は L-a を覆えない。
        out = self.guard_run("missing", all_cases=ALL_CASES_L_ONLY)
        self.assertEqual(["(なし)"], self.lines("guard.log"))
        self.assertIn("頭文字の一覧が ALL_CASES を覆わない", out)

    def test_guard_list_is_used_even_if_all_cases_has_another_form(self):
        out = self.guard_run("t2", all_cases=ALL_CASES_ODD)
        self.assertEqual(sorted(["A", "B", "M1", "M2", "L-a", "L-b", "L-c"]), self.lines("guard.log"))
        self.assertNotIn("implement-guard-selftest.sh を", out)

    def test_single_split_unit_with_zero_checks_is_reported(self):
        # --list が 1 行だけ返し、その単位が 0 件で終わっても、分けた単位として異常にする。
        self.write_guard()
        result = self.run_all("-j", "4", FAKE_GUARD_MODE="one0")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("異常: implement-guard-selftest.sh --only A: 確かめが 1 件も流れていない", result.stdout)

    # ---- 重い順・重み ----

    def test_plan_lists_outside_first_then_slot_units_by_weight(self):
        self.weights.parent.mkdir()
        self.weights.write_text(json.dumps({"version": 1, "weights": {"unittest:test_b.py": 900.0}}), encoding="utf-8")
        result = self.run_all("--plan", "-j", "5", PATH=self.fake_git("2.56.0"))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual("計画: 枠の単位 13 個を、同時に 5 個まで流す(ほかに枠の外 2 個)", lines[0])
        outside = [line for line in lines if line.startswith("枠の外  ")]
        self.assertEqual(["枠の外  review-agent-selftest.sh", "枠の外  implement-agent-selftest.sh"], outside)
        slot = [re.match(r"^\s*(\d+)  +([\d.]+) 秒  (記録|表|既定)  (.*)$", line) for line in lines]
        slot = [match.groups() for match in slot if match]
        self.assertEqual(13, len(slot), result.stdout)
        self.assertEqual([str(number) for number in range(1, 14)], [item[0] for item in slot])
        weights = [float(item[1]) for item in slot]
        self.assertEqual(sorted(weights, reverse=True), weights)
        # 出所は、記録 → 表 → 既定の順に決まる。
        by_label = {item[3]: (float(item[1]), item[2]) for item in slot}
        self.assertEqual((900.0, "記録"), by_label["unittest test_b.py"])
        self.assertEqual((200.0, "既定"), by_label["unittest test_a.py"])
        self.assertEqual((81.0, "表"), by_label["diff-snapshot-selftest.sh"])
        self.assertEqual((14.0, "表"), by_label["loop-selftest (節の外だけ)"])
        self.assertEqual("unittest test_b.py", slot[0][3])
        self.assertRegex(lines[-1], r"^  13  ")

    def test_plan_prints_no_start_lines_and_runs_nothing(self):
        result = self.run_all("--plan")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], self.lines("loop.log"))
        self.assertEqual([], self.lines("guard.log"))
        self.assertFalse((self.home / ".local").exists())
        self.assertNotIn("置き場を作った", result.stdout)
        self.assertFalse(self.weights.exists())

    # ---- 重みの記録 ----

    def test_weights_record_is_written_for_successes_only_and_averaged(self):
        self.write_test("test_fail.py", FAILING)
        self.weights.parent.mkdir()
        self.weights.write_text(json.dumps({"version": 1, "weights": {"unittest:test_a.py": 100.0}}), encoding="utf-8")
        result = self.run_all("-j", "4")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        record = json.loads(self.weights.read_text(encoding="utf-8"))
        self.assertEqual(1, record["version"])
        weights = record["weights"]
        self.assertNotIn("unittest:test_fail.py", weights)
        for key in ("unittest:test_b.py", "loop:alpha", "loop:(節の外だけ)", "implement-guard:L-a", "diff-snapshot",
                    "review-agent", "implement-agent"):
            self.assertIn(key, weights)
        # 前の値(100)と今回の時間(数秒以内)の平均。
        self.assertTrue(50.0 <= weights["unittest:test_a.py"] < 60.0, weights["unittest:test_a.py"])
        self.assertEqual([], [path.name for path in self.weights.parent.iterdir() if path.name != "weights.json"])

    def test_broken_record_warns_and_still_runs(self):
        for text in ("これは JSON ではない", json.dumps({"version": 2, "weights": {}}),
                     json.dumps({"version": 1, "weights": {"a": "x"}})):
            with self.subTest(text=text[:10]):
                self.weights.parent.mkdir(exist_ok=True)
                self.weights.write_text(text, encoding="utf-8")
                result = self.run_all("-j", "4")
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn(f"注意: 重みの記録を読めない({self.weights}: ", result.stdout)
                self.assertIn("。表と既定の重みで並べる", result.stdout)
                # 流した後は、正しい記録に書き直す。
                self.assertEqual(1, json.loads(self.weights.read_text(encoding="utf-8"))["version"])

    def test_broken_record_note_comes_after_plan_line(self):
        self.weights.parent.mkdir(exist_ok=True)
        for text in ("これは JSON ではない", '{"version": 1, "weights": {"a": 1e999}}',
                     '{"version": 1, "weights": {"a": ' + "1" + "0" * 400 + "}}"):
            with self.subTest(text=text[:12]):
                self.weights.write_text(text, encoding="utf-8")
                result = self.run_all("--plan", PATH=self.fake_git("2.56.0"))
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                lines = result.stdout.splitlines()
                self.assertTrue(lines[0].startswith("計画: "), result.stdout)
                noted = [i for i, line in enumerate(lines) if line.startswith("注意: 重みの記録を読めない(")]
                self.assertEqual(1, len(noted), result.stdout)
                self.assertGreater(noted[0], 0)

    def test_unwritable_record_warns_and_does_not_fail(self):
        (self.cache / "workflow-run-all").write_text("ファイル", encoding="utf-8")
        result = self.run_all("-j", "4")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("注意: 重みの記録を書けない(", result.stdout)
        self.assertIn("。今回の時間は記録しない", result.stdout)

    def test_weights_file_option_changes_the_place(self):
        other = self.root / "other" / "w.json"
        result = self.run_all("-j", "4", "--weights-file", str(other))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(other.is_file())
        self.assertFalse(self.weights.exists())

    # ---- 置き場 ----

    def test_state_directory_is_made_once_and_not_by_plan(self):
        state = self.home / ".local" / "state" / "dev-workflow"
        result = self.run_all("-j", "4")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"置き場を作った: {state}", result.stdout)
        self.assertTrue(state.is_dir())
        again = self.run_all("-j", "4")
        self.assertNotIn("置き場を作った", again.stdout)
        self.assertTrue(state.is_dir())

    # ---- git の版 ----

    def test_old_git_warns_at_start_and_end(self):
        path = self.fake_git("2.34.1")
        result = self.run_all("-j", "4", PATH=path)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        warnings = [index for index, line in enumerate(lines) if line.startswith("警告: git 2.34.1 は 2.42 より古い。")]
        self.assertEqual(2, len(warnings), result.stdout)
        self.assertIn("git: git version 2.34.1", lines)
        self.assertIn("(bash scripts/run-all-container.sh)で流す", lines[warnings[0]])
        progress = [index for index, line in enumerate(lines) if line.startswith(("ok  ", "FAIL"))]
        # 始めの警告は最初の単位の前に、最後の警告は最後の単位の後で、部ごとの結果の行(6 行)と最後の行の前に出る。
        self.assertLess(warnings[0], min(progress))
        self.assertGreater(warnings[1], max(progress))
        self.assertEqual(len(lines) - 8, warnings[1], result.stdout)
        plan = self.run_all("--plan", PATH=path)
        planned = [line for line in plan.stdout.splitlines() if line.startswith("警告:")]
        self.assertEqual(2, len(planned), plan.stdout)
        # --plan は、最後の単位の行の後にもう一度出す。
        self.assertTrue(plan.stdout.splitlines()[-1].startswith("警告: git 2.34.1"))

    def test_middle_git_notes_reftable_and_new_git_is_silent(self):
        middle = self.run_all("--plan", PATH=self.fake_git("2.43.0"))
        self.assertIn("注意: git 2.43.0 では reftable の確かめ(3 件)が skip になる(2.45 以上で流れる)", middle.stdout)
        self.assertNotIn("警告:", middle.stdout)
        new = self.run_all("--plan", PATH=self.fake_git("2.56.0"))
        self.assertIn("git: git version 2.56.0", new.stdout)
        self.assertNotIn("警告:", new.stdout)
        self.assertNotIn("注意:", new.stdout)

    # ---- 枠の数 ----

    def test_one_slot_never_runs_slot_units_at_the_same_time(self):
        result = self.run_all("-j", "1")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.marks / "overlap.log").exists(), result.stdout)

    # ---- --log-dir ----

    def test_log_dir_keeps_every_output(self):
        result = self.run_all("-j", "4", "--log-dir", str(self.logs))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        names = sorted(path.name for path in self.logs.iterdir())
        for expected in ("unittest_test_a.py.log", "loop_alpha.log", "loop_(節の外だけ).log", "implement-guard_A.log",
                         "diff-snapshot.log", "review-agent.log", "implement-agent.log"):
            self.assertIn(expected, names)
        self.assertEqual(15, len(names), names)
        self.assertIn("PASS  alpha 1", (self.logs / "loop_alpha.log").read_text(encoding="utf-8"))

    # ---- 止めたとき ----

    def test_interrupt_stops_everything_and_reports_left_scratch(self):
        self.write_loop(LOOP_SECTIONS + LOOP_SLOW)
        runner = subprocess.Popen([sys.executable, "-B", str(self.scripts / "run-all.py"), "-j", "1"], cwd=self.root,
                                  env=self.env(RUN_ALL_HANG_OUTSIDE="1"), stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        pids = []
        try:
            deadline = time.monotonic() + 90
            names = ("slow.pids", "review-agent-selftest.sh.pids", "implement-agent-selftest.sh.pids")
            while time.monotonic() < deadline and not all((self.marks / name).exists() for name in names):
                time.sleep(0.05)
            for name in names:
                self.assertTrue((self.marks / name).exists(), f"{name} が出ない")
            pids = [int(pid) for name in names for pid in (self.marks / name).read_text(encoding="utf-8").split()]
            self.addCleanup(self.kill_leftovers, pids)
            runner.send_signal(signal.SIGTERM)
            output, _ = runner.communicate(timeout=90)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.communicate()
        self.assertEqual(143, runner.returncode, output)
        # 動いていたのは、slow の節と、枠の外の 2 本(review-agent・implement-agent)。始めていない単位は捨てた。
        self.assertRegex(output, r"中断: 動いていた 3 個のプロセスを止め、まだ始めていない \d+ 個を始めずに捨てた")
        self.assertNotIn("結果:", output)
        # 始めていない単位(節の外だけ・implement-guard)は流れていない。
        self.assertNotIn(",", self.lines("loop.log"))
        self.assertEqual([], self.lines("guard.log"))
        scratches = list(self.marks.glob("scratch-*"))
        self.assertEqual(1, len(scratches), scratches)
        self.assertIn(f"残った一時フォルダ(slow。確かめてから消す): {scratches[0]}", output)
        deadline = time.monotonic() + 15
        while not all(process_gone(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([], [pid for pid in pids if not process_gone(pid)], pids)
        self.assertEqual([], [path.name for path in self.tmp.iterdir()])

    @staticmethod
    def kill_leftovers(pids):
        for pid in pids:
            if not process_gone(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    # ---- 引数の誤り ----

    def test_argument_errors_exit_2_without_running(self):
        for args in (("-j", "0"), ("--log-dir", str(self.root / "missing")), ("--unknown",)):
            with self.subTest(args=args):
                result = self.run_all(*args)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertEqual([], self.lines("loop.log"))
                self.assertEqual([], self.lines("guard.log"))
                self.assertFalse((self.home / ".local").exists())


class RunUnitsTest(unittest.TestCase):
    """重みの決め方と、重みの記録の読み書き(run_units.py を直接呼ぶ)。"""

    def setUp(self):
        self.module = load_run_units()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "sub" / "weights.json"

    def test_weight_of_prefers_record_then_table_then_default(self):
        weight_of = self.module.weight_of
        self.assertEqual((5.0, "記録"), weight_of("a", {"a": 5}, {"a": 9}))
        self.assertEqual((9.0, "表"), weight_of("a", {}, {"a": 9}))
        self.assertEqual((200.0, "既定"), weight_of("a", {}, {}))

    def test_save_averages_with_previous_value_and_creates_folder(self):
        self.module.save_weights(self.path, {}, {"a": 10.0})
        self.assertEqual({"a": 10.0}, self.module.load_weights(self.path))
        self.module.save_weights(self.path, self.module.load_weights(self.path), {"a": 20.0, "b": 4.0})
        self.assertEqual({"a": 15.0, "b": 4.0}, self.module.load_weights(self.path))
        self.assertEqual(["weights.json"], [path.name for path in self.path.parent.iterdir()])

    def test_huge_integer_in_record_is_a_broken_record(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"version": 1, "weights": {"a": ' + "1" + "0" * 400 + "}}", encoding="utf-8")
        notes = []
        self.assertEqual({}, self.module.load_weights(self.path, notes=notes))
        self.assertEqual(1, len(notes), notes)
        self.assertIn("。表と既定の重みで並べる", notes[0])

    def test_missing_record_is_empty_without_warning(self):
        self.assertEqual({}, self.module.load_weights(self.path))


if __name__ == "__main__":
    unittest.main()
