"""scripts/run-all.py(と scripts/run_units.py・scripts/run-all-container.sh)の回帰テスト。

使い捨てのフォルダに、ランナーのコピーと、小さな偽の部(test_*.py 2 つ・偽の loop-selftest.sh・偽の selftest 4 本)を
置いて、サブプロセスで流す。HOME・XDG_CACHE_HOME・TMPDIR は、使い捨てのフォルダに向ける
(本物の重みの記録・使い回しの記録・置き場には触れない)。
- RunAllTest: 既定の回(RUN_ALL_IMAGE を除いて流すので、使い回しの記録を読み書きしない)。
- RunAllReuseTest: 結果の使い回し(--reuse)。容器の中と同じく、正しい形の RUN_ALL_IMAGE を渡して流す。
- RunUnitsTest・RunUnitsReuseTest・RunAllScopeTest: 関数を直接呼ぶ。
- RunAllContainerTest: 容器の入口(docker のモックを PATH の先頭に置く。本物の docker は起動しない)。
"""

import contextlib
import datetime
import importlib.util
import io
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

import support_git


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


# 枠の外の偽の selftest。RUN_ALL_HANG_OUTSIDE があると、自分と子プロセスの pid を書いて眠る(止めたときの確かめ)。
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
        for name in ("LOOP_SELFTEST_ONLY", "LOOP_SELFTEST_TIMEOUT", "RUN_TIMEOUT", "XDG_STATE_HOME", "RUN_ALL_IMAGE"):
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
        # 子プロセスの環境に、無いときだけ上限が入る(loop の各プロセス)。
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



# ---- 結果の使い回し(--reuse) ----

# 容器の中と同じ、正しい形のイメージ(<名前>@sha256:<64 桁>)。
IMAGE = "test@sha256:" + "0" * 64
OTHER_IMAGE = "test@sha256:" + "1" * 64

# モックの部の単位が始まるたびに、印のフォルダの started.log に 1 行を書く(単位のプロセスの数を数える)。
MARK_PY = """
import os as _os
_marks = _os.environ.get("RUN_ALL_MARKS")
if _marks:
    with open(_os.path.join(_marks, "started.log"), "a", encoding="utf-8") as _stream:
        _stream.write(__name__ + "\\n")
"""
MARK_SH = 'printf "%s\\n" "$(basename "$0") ${LOOP_SELFTEST_ONLY:-} ${2:-}" >>"$RUN_ALL_MARKS/started.log"\n'

# 分けて流す unittest(run-tests.py の SHARDS で 2 つに分ける名前)。テスト 3 件。
THREE = """
import unittest

class Three(unittest.TestCase):
    def test_1(self):
        pass

    def test_2(self):
        pass

    def test_3(self):
        pass
"""

# モックの diff-snapshot-selftest.sh の仕掛け(印のフォルダの中の印のファイルで、一度だけ働く。鍵に入るものは変えない)。
DIFF_HOOKS = r"""
if [ -e "$RUN_ALL_MARKS/fail-once" ]; then rm -f "$RUN_ALL_MARKS/fail-once"; echo "結果: PASS 0 件 / FAIL 1 件"; exit 1; fi
if [ -e "$RUN_ALL_MARKS/touch-plugins" ]; then rm -f "$RUN_ALL_MARKS/touch-plugins"; echo "書き換えた" >>plugins/dev-workflow/notes.md; fi
if [ -e "$RUN_ALL_MARKS/write-pycache" ]; then
  rm -f "$RUN_ALL_MARKS/write-pycache"
  mkdir -p plugins/dev-workflow/__pycache__
  echo pyc >plugins/dev-workflow/__pycache__/m.cpython-310.pyc
fi
"""

# 作業場所(リポジトリ直下)で python3 を起動して json を読み込む節。直下の json.py の FAIL が 0 でなければ落ちる。
LOOP_SHADOW = r"""
if want shadow; then
if python3 -B -c 'import json, sys; sys.exit(getattr(json, "FAIL", 0))'; then ok "shadow"; else ng "shadow"; fi
fi
"""

# scripts/ の直下に置く json.py。ランナーが読み込むので、標準の json を読み込み直して、そのまま使わせる。
SHADOW_JSON = """
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
del sys.modules[__name__]
_saved = sys.path[:]
sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or ".") != _here]
try:
    import json as _real
finally:
    sys.path[:] = _saved
sys.modules[__name__] = _real
"""

NOTES = "plugins/dev-workflow/notes.md"
UNITTEST_LABELS = frozenset({"unittest test_a.py", "unittest test_b.py", "unittest test_host_check.py [1/2]",
                             "unittest test_host_check.py [2/2]"})
BASH_LABELS = frozenset({
    "loop-selftest (節の外だけ)", "loop-selftest alpha", "loop-selftest beta",
    *(f"implement-guard-selftest.sh --only {name}" for name in ("A", "B", "M1", "M2", "L-a", "L-b", "L-c")),
    "diff-snapshot-selftest.sh", "review-agent-selftest.sh", "implement-agent-selftest.sh"})
ALL_LABELS = UNITTEST_LABELS | BASH_LABELS
PROGRESS = re.compile(r"^(?:ok  |FAIL) +\d+\.\d 秒  (.+?)  ", re.M)
REUSED = re.compile(r"^使い回し +\d+\.\d 秒  (.+?)  ", re.M)


class RunAllReuseTest(unittest.TestCase):
    """容器の中(正しい形の RUN_ALL_IMAGE)での、結果の使い回しと記録。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        # 記録や印のファイルが入力の一覧に入らないように、リポジトリと、ほかのフォルダを分ける。
        self.root = self.base / "repo"
        self.scripts = self.root / "scripts"
        self.scripts.mkdir(parents=True)
        for name in RUNNERS:
            shutil.copy2(SCRIPTS / name, self.scripts / name)
        self.home = self.base / "home"
        self.cache = self.base / "cache"
        self.tmp = self.base / "tmp"
        self.marks = self.base / "marks"
        for folder in (self.home, self.cache, self.tmp, self.marks):
            folder.mkdir()
        self.results = self.cache / "workflow-run-all" / "results.json"
        self.write_test("test_a.py", PASSING)
        self.write_test("test_b.py", OTHER)
        self.write_test("test_host_check.py", THREE)
        self.write_loop()
        self.write(GUARD, GUARD_UNIVERSE.replace(LOCK_SH, MARK_SH + LOCK_SH, 1).replace("%(all_cases)s",
                                                                                          ALL_CASES_STANDARD))
        self.write(AGENT, selftest(4, MARK_SH, lock=False))
        self.write(REVIEW, selftest(3, MARK_SH, lock=False))
        self.write(DIFF, selftest(5, MARK_SH + DIFF_HOOKS))
        self.write(NOTES, "メモ\n")
        self.write("docs/x.md", "文書\n")

    # ---- 土台 ----

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def append(self, relative, text):
        with open(self.root / relative, "a", encoding="utf-8") as stream:
            stream.write(text)

    def write_test(self, name, text):
        (self.scripts / name).write_text(MARK_PY + textwrap.dedent(text), encoding="utf-8")

    def write_loop(self, sections=LOOP_SECTIONS):
        head = LOOP_HEAD.replace('ok "準備: 前"\n', MARK_SH + 'ok "準備: 前"\n', 1)
        self.write(LOOP, head + sections + LOOP_TAIL)

    def env(self, **extra):
        """決まった環境変数だけで流す(テストを流す機械の環境変数で、鍵が変わらないように)。None は除く。"""
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.home), "XDG_CACHE_HOME": str(self.cache),
               "TMPDIR": str(self.tmp), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "RUN_ALL_MARKS": str(self.marks),
               "RUN_ALL_IMAGE": IMAGE}
        for name, value in extra.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def run_all(self, *args, umask=-1, **extra):
        return subprocess.run([sys.executable, "-B", str(self.scripts / "run-all.py"), "-j", "8", *args],
                              cwd=self.root, capture_output=True, text=True, env=self.env(**extra), timeout=180,
                              umask=umask)

    def run_ok(self, *args, **extra):
        result = self.run_all(*args, **extra)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def started(self):
        """単位のプロセスが始まった数(モックの部が書いた印の行の数)。"""
        path = self.marks / "started.log"
        return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0

    def rerun(self, fresh, reason=None, code=0, *args, **extra):
        """--reuse で流し、新しく流した単位の表示名の集合が fresh で、残りを使い回したことを確かめる。"""
        before = self.started()
        result = self.run_all("--reuse", *args, **extra)
        out = result.stdout
        self.assertEqual(code, result.returncode, out + result.stderr)
        self.assertEqual(set(fresh), set(PROGRESS.findall(out)), out)
        self.assertEqual(len(fresh), self.started() - before, out)
        labels = self.labels if hasattr(self, "labels") else ALL_LABELS
        self.assertEqual(set(labels) - set(fresh), set(REUSED.findall(out)), out)
        self.assertIn(f"使い回し: 入力が前の合格と同じ {len(labels) - len(fresh)} 個の単位は流さない"
                      f"(新しく流すのは {len(fresh)} 個)", out)
        if reason is not None:
            self.assertIn(f"新しく流す理由: {reason}\n", out)
        return out

    @staticmethod
    def result_lines(out):
        return [line for line in out.splitlines() if line.startswith("結果(")]

    def record(self):
        return json.loads(self.results.read_text(encoding="utf-8"))

    def edit_record(self, key, change):
        """記録の中の、単位の鍵 key のいちばん新しい記録を change で書き換える(input はそのまま)。"""
        data = self.record()
        change(data["units"][key][0])
        self.results.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def plan_reasons(self):
        result = self.run_all("--plan", "--reuse")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return next((line for line in result.stdout.splitlines() if line.startswith("新しく流す理由: ")), None)

    # ---- 既定の回 ----

    def test_default_run_is_unchanged_and_records_results(self):
        out = self.run_ok()
        self.assertEqual(17, self.started())
        self.assertFalse([line for line in out.splitlines() if line.startswith("使い回し")], out)
        self.assertNotIn("使い回せない", out)
        self.assertIn("17 個の単位を、同時に 8 個まで流す(枠の外: review-agent-selftest.sh・implement-agent-selftest.sh)", out)
        self.assertRegex(out.strip().splitlines()[-1], r"^結果: 合格 / \d+\.\d 秒$")
        self.assertEqual(17, len(self.record()["units"]))
        self.run_ok()
        self.assertEqual(34, self.started())

    def test_first_reuse_runs_everything_because_no_record(self):
        out = self.run_ok("--reuse")
        self.assertEqual(17, self.started())
        self.assertIn("使い回し: 入力が前の合格と同じ 0 個の単位は流さない(新しく流すのは 17 個)", out)
        self.assertIn("新しく流す理由: 記録が無い 17 個\n", out)
        self.assertIn("使い回した単位: 0 個 / 新しく流した単位: 17 個", out)
        self.assertRegex(out.strip().splitlines()[-1], r"^結果: 合格 / \d+\.\d 秒$")

    def test_second_reuse_starts_no_unit_and_keeps_the_counts(self):
        first = self.run_ok("--reuse")
        count = self.started()
        second = self.run_ok("--reuse")
        self.assertEqual(count, self.started(), second)
        self.assertIn("使い回し: 入力が前の合格と同じ 17 個の単位は流さない(新しく流すのは 0 個)", second)
        self.assertNotIn("新しく流す理由:", second)
        self.assertIn("0 個の単位を、同時に 8 個まで流す(枠の外: なし)", second)
        self.assertEqual(set(ALL_LABELS), set(REUSED.findall(second)))
        self.assertRegex(second, r"(?m)^使い回し +\d+\.\d 秒  implement-guard-selftest.sh --only L-a  PASS 1 件 / FAIL 0 件$")
        self.assertRegex(second, r"(?m)^使い回し +\d+\.\d 秒  unittest test_host_check.py \[2/2\]  1 件$")
        self.assertEqual(6, len(self.result_lines(first)))
        self.assertEqual(self.result_lines(first), self.result_lines(second))
        lines = second.strip().splitlines()
        self.assertEqual("使い回した単位: 17 個 / 新しく流した単位: 0 個", lines[-8])
        self.assertRegex(lines[-1], r"^結果: 合格\(17 個の単位は使い回した\)/ \d+\.\d 秒$")

    def test_host_and_unknown_image_neither_reuse_nor_record(self):
        for image, reason in ((None, "ホストで流しているので、使い回さない"), ("不明", "容器のイメージの ID が分からない"),
                              ("test@sha256:" + "0" * 63, "容器のイメージの ID が分からない")):
            with self.subTest(image=image):
                before = self.started()
                out = self.run_ok("--reuse", RUN_ALL_IMAGE=image)
                self.assertEqual(17, self.started() - before)
                self.assertIn(f"注意: 使い回せない({reason})。全部の単位を新しく流す", out)
                self.assertNotIn("使い回し:", out)
                self.assertRegex(out.strip().splitlines()[-1], r"^結果: 合格 / \d+\.\d 秒$")
                default = self.run_ok(RUN_ALL_IMAGE=image)
                self.assertNotIn("使い回", default)
                self.assertFalse(self.results.exists())

    # ---- 1 文字の変更ごとに流れる単位 ----

    def test_change_in_a_test_file_runs_only_unittest(self):
        self.run_ok("--reuse")
        self.append("scripts/test_b.py", "#")
        self.rerun(UNITTEST_LABELS, "入力のファイルが違う 4 個")

    def test_change_in_docs_runs_only_unittest(self):
        self.run_ok("--reuse")
        self.append("docs/x.md", "!")
        self.rerun(UNITTEST_LABELS, "入力のファイルが違う 4 個")

    def test_change_under_plugins_runs_everything(self):
        self.run_ok("--reuse")
        self.append(NOTES, "!")
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")

    def test_adding_or_changing_the_dockerfile_runs_everything(self):
        self.run_ok("--reuse")
        self.write("scripts/container/Dockerfile", "FROM ubuntu:22.04\n")
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")
        self.append("scripts/container/Dockerfile", "#")
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")

    def test_permission_only_changes_run_the_units_that_hold_the_file(self):
        self.run_ok("--reuse")
        path = self.root / "docs" / "x.md"
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.rerun(UNITTEST_LABELS, "入力のファイルが違う 4 個")
        (self.root / "docs").chmod(0o700)
        self.rerun(UNITTEST_LABELS, "入力のファイルが違う 4 個")
        (self.root / NOTES).chmod(0o444)
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")

    def test_git_folders_are_not_in_the_inventory(self):
        # 容器の入口は、流すたびに git init と commit をする(.git の中身は毎回変わる)。
        self.write(".git/HEAD", "ref: refs/heads/main\n")
        self.write("plugins/dev-workflow/.git", "gitdir: ../../.git/modules/x\n")
        self.run_ok("--reuse")
        self.append(".git/HEAD", "!")
        self.append("plugins/dev-workflow/.git", "!")
        self.write(".git/objects/aa/bb", "物\n")
        self.rerun(set())

    def test_empty_directory_under_plugins_runs_everything(self):
        self.run_ok("--reuse")
        (self.root / "plugins/dev-workflow/skills/empty-probe").mkdir(parents=True)
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")

    def test_modules_that_can_shadow_the_standard_library_run_everything(self):
        self.run_ok("--reuse")
        self.write("json.py", "FAIL = 0\n")
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")
        self.write("scripts/json.py", SHADOW_JSON)
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")
        self.write("json/__init__.py", "")
        self.write("json/decoder.py", "VALUE = 1\n")
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")
        self.append("json/decoder.py", "#")
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")

    def test_shadow_module_in_the_working_folder_changes_the_loop_result(self):
        self.write_loop(LOOP_SECTIONS + LOOP_SHADOW)
        self.write("json.py", "FAIL = 0\n")
        self.labels = ALL_LABELS | {"loop-selftest shadow"}
        self.run_ok("--reuse")
        self.rerun(set())
        self.write("json.py", "FAIL = 1\n")
        out = self.rerun(self.labels, "入力のファイルが違う 18 個", 1)
        self.assertRegex(out, r"(?m)^FAIL +\d+\.\d 秒  loop-selftest shadow  PASS \d+ 件 / FAIL 1 件$")

    def test_ignored_file_runs_only_unittest(self):
        self.write(".gitignore", ".claude/reviews/\n")
        self.write(".claude/reviews/x.md", "控え\n")
        self.run_ok("--reuse")
        self.append(".claude/reviews/x.md", "!")
        self.rerun(UNITTEST_LABELS, "入力のファイルが違う 4 個")

    def test_ignored_untracked_test_file_is_in_the_scope(self):
        self.write(".gitignore", "scripts/test_ignored.py\n")
        self.write_test("test_ignored.py", OTHER)
        self.labels = ALL_LABELS | {"unittest test_ignored.py"}
        self.run_ok("--reuse")
        self.rerun(set())
        self.write_test("test_ignored.py", FAILING)
        out = self.rerun(UNITTEST_LABELS | {"unittest test_ignored.py"}, "入力のファイルが違う 5 個", 1)
        self.assertIn("異常: test_ignored.py: 失敗(終了コード 1)", out)

    def test_link_inside_the_same_part_follows_the_target(self):
        os.symlink("notes.md", self.root / "plugins/dev-workflow/link.md")
        self.run_ok("--reuse")
        self.rerun(set())
        self.append(NOTES, "!")
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")

    def test_links_out_of_the_part_make_the_scope_undetermined(self):
        self.run_ok("--reuse")
        outside = self.base / "outside.txt"
        outside.write_text("外\n", encoding="utf-8")
        cases = (("to-docs.md", "../../docs/x.md", "範囲が決まらない 13 個・入力のファイルが違う 4 個"),
                 ("to-outside.md", str(outside), "範囲が決まらない 17 個"),
                 ("to-folder", "../../docs", "範囲が決まらない 17 個"))
        for name, target, reasons in cases:
            with self.subTest(name=name):
                link = self.root / "plugins/dev-workflow" / name
                os.symlink(target, link)
                try:
                    self.assertEqual(f"新しく流す理由: {reasons}", self.plan_reasons())
                finally:
                    link.unlink()
        self.assertIsNone(self.plan_reasons())

    def test_top_level_link_to_a_package_outside_the_repository_makes_the_scope_undetermined(self):
        self.run_ok("--reuse")
        package = self.base / "outside-package" / "json"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("FAIL = 1\n", encoding="utf-8")
        # 作業場所の import json が、リポジトリの外のパッケージを読みうる。bash の単位も使い回さない。
        os.symlink(package, self.root / "json")
        self.rerun(ALL_LABELS, "範囲が決まらない 17 個")

    def test_pycache_before_the_run_stops_reuse_but_one_written_during_the_run_is_ignored(self):
        (self.marks / "write-pycache").touch()
        self.run_ok("--reuse")
        self.assertTrue((self.root / "plugins/dev-workflow/__pycache__").is_dir())
        self.assertEqual(17, len(self.record()["units"]))
        before = self.started()
        out = self.run_ok("--reuse")
        self.assertEqual(17, self.started() - before)
        self.assertIn("注意: 使い回せない(流す前に __pycache__ がある(plugins/dev-workflow/__pycache__))。"
                      "全部の単位を新しく流す", out)
        self.assertNotIn("使い回し:", out)

    # ---- 鍵のほかの要素 ----

    def test_environment_variables_are_in_the_key_except_the_skipped_names(self):
        self.run_ok("--reuse")
        self.rerun(ALL_LABELS, "環境変数が違う 17 個", 0, REUSE_PROBE="1")
        self.rerun(set(), None, 0, PWD="/elsewhere", HOSTNAME="other-container")

    def test_tool_versions_are_in_the_key(self):
        bindir = self.base / "bin"
        bindir.mkdir()
        real = shutil.which("git")

        def fake_git(version):
            script = bindir / "git"
            script.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "git version {version}"; exit 0; fi\n'
                              f'exec "{real}" "$@"\n', encoding="utf-8")
            script.chmod(0o755)

        path = f"{bindir}{os.pathsep}{os.environ.get('PATH', os.defpath)}"
        fake_git("2.56.0")
        self.run_ok("--reuse", PATH=path, umask=0o022)
        fake_git("2.56.1")
        self.rerun(ALL_LABELS, "道具の版が違う 17 個", 0, PATH=path, umask=0o022)
        self.rerun(ALL_LABELS, "道具の版が違う 17 個", 0, PATH=path, umask=0o022, RUN_ALL_IMAGE=OTHER_IMAGE)
        self.rerun(ALL_LABELS, "道具の版が違う 17 個", 0, PATH=path, umask=0o077)
        self.rerun(set(), None, 0, PATH=path, umask=0o022)

    # ---- 失敗を使い回さない ----

    def test_failure_is_never_reused(self):
        self.write_test("test_fail.py", FAILING)
        self.labels = ALL_LABELS | {"unittest test_fail.py"}
        result = self.run_all("--reuse")
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("異常: test_fail.py: 失敗(終了コード 1)", result.stdout)
        self.rerun({"unittest test_fail.py"}, "前の回が失敗 1 個", 1)

    def test_later_failure_with_the_same_input_hides_the_earlier_pass(self):
        self.run_ok("--reuse")
        (self.marks / "fail-once").touch()
        failed = self.run_all()
        self.assertEqual(1, failed.returncode, failed.stdout)
        self.assertIn("異常: diff-snapshot-selftest.sh: 失敗(終了コード 1)", failed.stdout)
        self.rerun({"diff-snapshot-selftest.sh"}, "前の回が失敗 1 個")

    def test_units_whose_input_changed_during_the_run_are_not_recorded(self):
        self.run_ok()
        self.append(NOTES, "!")
        changed = (self.root / NOTES).read_text(encoding="utf-8")
        # 流している間に、モックの selftest が plugins/ の下のファイルを書き換える。流し終えた後に元の中身へ戻す。
        (self.marks / "touch-plugins").touch()
        self.run_ok()
        self.assertNotEqual(changed, (self.root / NOTES).read_text(encoding="utf-8"))
        self.write(NOTES, changed)
        self.assertEqual({1}, {len(items) for items in self.record()["units"].values()})
        # 流す前の中身で記録していれば、ここで使い回してしまう。
        self.rerun(ALL_LABELS, "入力のファイルが違う 17 個")

    # ---- 使い回した結果を含めた集計 ----

    def test_reused_and_fresh_shards_are_counted_together(self):
        first = self.run_ok("--reuse")
        data = self.record()
        del data["units"]["unittest:test_host_check.py[1/2]"]
        self.results.write_text(json.dumps(data), encoding="utf-8")
        out = self.rerun({"unittest test_host_check.py [1/2]"}, "記録が無い 1 個")
        self.assertNotIn("異常:", out)
        self.assertEqual(self.result_lines(first), self.result_lines(out))

    def test_reused_records_still_go_through_the_count_checks(self):
        module = load_run_units()

        def shard_total(record):
            record["extra"]["total"] = 4

        def outside_name(record):
            output = module.unpack_output(record["output"]).replace("PASS  準備: 前", "PASS  準備: 別")
            record["output"] = module.pack_output(output)

        def zero_checks(record):
            record["extra"] = [0, 0]

        cases = (("unittest:test_host_check.py[2/2]", shard_total, "異常: test_host_check.py: 分けたプロセスの件数が合わない"),
                 ("loop:alpha", outside_name, "異常: alpha: 節の外の確かめの結果が、節の外だけのプロセスと違う(差し引けない)"),
                 ("implement-guard:A", zero_checks,
                  "異常: implement-guard-selftest.sh --only A: 確かめが 1 件も流れていない(単位の名前が合わなかった)"))
        self.run_ok("--reuse")
        saved = self.results.read_bytes()
        for key, change, problem in cases:
            with self.subTest(key=key):
                self.results.write_bytes(saved)
                self.edit_record(key, change)
                out = self.rerun(set(), None, 1)
                self.assertIn(problem, out)
                self.assertRegex(out.strip().splitlines()[-1], r"^結果: 不合格")

    def test_broken_record_warns_runs_everything_and_is_rewritten(self):
        self.results.parent.mkdir(parents=True)
        self.results.write_text("これは JSON ではない", encoding="utf-8")
        out = self.rerun(ALL_LABELS, "記録が無い 17 個")
        self.assertIn(f"注意: 使い回しの記録を読めない({self.results}: ", out)
        self.assertIn("。全部の単位を新しく流す", out)
        data = self.record()
        self.assertEqual(1, data["version"])
        self.assertEqual(17, len(data["units"]))

    def test_plan_with_reuse_lists_only_fresh_units_and_starts_nothing(self):
        self.run_ok("--reuse")
        self.append("scripts/test_b.py", "#")
        saved = self.results.read_bytes()
        before = self.started()
        result = self.run_all("--plan", "--reuse")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual("計画: 枠の単位 4 個を、同時に 8 個まで流す(ほかに枠の外 0 個)", lines[0])
        self.assertIn("使い回し: 入力が前の合格と同じ 13 個の単位は流さない(新しく流すのは 4 個)", lines)
        self.assertIn("新しく流す理由: 入力のファイルが違う 4 個", lines)
        self.assertEqual(set(BASH_LABELS), {line[len("使い回し  "):] for line in lines if line.startswith("使い回し  ")})
        planned = [re.match(r"^\s*\d+  +[\d.]+ 秒  (?:記録|表|既定)  (.*)$", line) for line in lines]
        self.assertEqual(set(UNITTEST_LABELS), {match.group(1) for match in planned if match})
        self.assertFalse([line for line in lines if line.startswith("枠の外  ")])
        self.assertEqual(before, self.started())
        self.assertEqual(saved, self.results.read_bytes())

    def test_interrupted_run_writes_no_record(self):
        self.write_loop(LOOP_SECTIONS + LOOP_SLOW)
        runner = subprocess.Popen([sys.executable, "-B", str(self.scripts / "run-all.py"), "-j", "8", "--reuse"],
                                  cwd=self.root, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and not (self.marks / "slow.pids").exists():
                time.sleep(0.05)
            self.assertTrue((self.marks / "slow.pids").exists(), "slow.pids が出ない")
            pids = [int(pid) for pid in (self.marks / "slow.pids").read_text(encoding="utf-8").split()]
            self.addCleanup(RunAllTest.kill_leftovers, pids)
            runner.send_signal(signal.SIGTERM)
            output, _ = runner.communicate(timeout=90)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.communicate()
        self.assertEqual(143, runner.returncode, output)
        self.assertNotIn("結果:", output)
        self.assertFalse(self.results.exists())


def record_of(module, digest, ok=True, at=None, **extra):
    """関数のテストに使う 1 つの記録。digest は input(と tools・env・files)に入れる 1 文字。"""
    item = {"input": digest * 64, "tools": "a" * 64, "env": "b" * 64, "files": "c" * 64, "ok": ok, "seconds": 1.5,
            "at": at or module.utc_now(), "code": 0 if ok else 1, "extra": [1, 0] if ok else [0, 1],
            "output": module.pack_output("PASS  x\n") if ok else ""}
    item.update(extra)
    return item


def iso(seconds_from_now):
    """今から seconds_from_now 秒ずらした時刻(UTC の ISO 8601)。"""
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds_from_now)
    return moment.isoformat(timespec="microseconds")


# 錠を持ったまま待つプロセス(順序を決めた 2 つのプロセスの確かめの、先に錠を取る側)。
HOLDER = r"""
import fcntl, json, os, sys, time
path, lock, ready, go, record = sys.argv[1:6]
descriptor = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o666)
fcntl.flock(descriptor, fcntl.LOCK_EX)
with open(path, "w", encoding="utf-8") as stream:
    json.dump({"version": 1, "units": {"k": [json.loads(record)]}}, stream)
open(ready, "w").close()
deadline = time.monotonic() + 60
while not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.02)
os.close(descriptor)
"""

# save_reuse を 1 回呼ぶプロセス(後から書く側)。
WRITER = r"""
import importlib.util, json, sys
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("run_units_writer", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.save_reuse(sys.argv[2], [("k", json.loads(sys.argv[3]))], False)
"""


class RunUnitsReuseTest(unittest.TestCase):
    """使い回しの記録の読み書き(run_units.py の load_reuse・save_reuse を直接呼ぶ)。"""

    def setUp(self):
        self.module = load_run_units()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "sub" / "results.json"

    def save(self, entries, failed=False):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            written = self.module.save_reuse(self.path, entries, failed)
        return written, stream.getvalue()

    def load(self):
        notes = []
        return self.module.load_reuse(self.path, notes), notes

    def test_keeps_eight_per_key_newest_first_and_drops_malformed_records(self):
        entries = [("k", record_of(self.module, digit, at=iso(-100 + index)))
                   for index, digit in enumerate("0123456789")]
        self.assertTrue(self.save(entries)[0])
        records, notes = self.load()
        self.assertEqual([], notes)
        self.assertEqual([digit * 64 for digit in "98765432"], [item["input"] for item in records["k"]])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["units"]["k"].append({"input": "x"})
        data["units"]["k"].append(dict(data["units"]["k"][0], code=True))
        self.path.write_text(json.dumps(data), encoding="utf-8")
        records, notes = self.load()
        self.assertEqual([], notes)
        self.assertEqual(8, len(records["k"]))
        for text in ("[]", json.dumps({"version": 2, "units": {}}), json.dumps({"version": 1, "units": {"k": {}}})):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                records, notes = self.load()
                self.assertEqual({}, records)
                self.assertEqual(1, len(notes), notes)
                self.assertIn("注意: 使い回しの記録を読めない(", notes[0])

    def test_newest_finish_time_wins_for_the_same_input(self):
        # 回 A が古い合格を読んだ後に、回 B が同じ input の新しい失敗を書き、その後に A が古い合格を書く。
        old_pass = record_of(self.module, "1", ok=True, at=iso(-20))
        new_fail = record_of(self.module, "1", ok=False, at=iso(-10))
        self.assertEqual(({}, []), self.load())
        self.assertTrue(self.save([("k", new_fail)], failed=True)[0])
        self.assertTrue(self.save([("k", old_pass)])[0])
        records, _ = self.load()
        self.assertEqual([False], [item["ok"] for item in records["k"]])

    def test_writer_waits_for_the_lock_and_reads_again_before_writing(self):
        self.path.parent.mkdir(parents=True)
        ready = self.folder / "ready"
        go = self.folder / "go"
        first = record_of(self.module, "1", at=iso(-10))
        second = record_of(self.module, "2", at=iso(-5))
        holder = subprocess.Popen([sys.executable, "-B", "-c", HOLDER, str(self.path),
                                   str(self.module.reuse_lock_path(self.path)), str(ready), str(go), json.dumps(first)])
        self.addCleanup(holder.kill)
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists())
        writer = subprocess.Popen([sys.executable, "-B", "-c", WRITER, str(SCRIPTS / "run_units.py"), str(self.path),
                                   json.dumps(second)])
        self.addCleanup(writer.kill)
        time.sleep(0.5)
        self.assertIsNone(writer.poll(), "錠を持つ間に書いた")
        go.touch()
        self.assertEqual(0, writer.wait(timeout=30))
        self.assertEqual(0, holder.wait(timeout=30))
        records, _ = self.load()
        self.assertEqual({"1" * 64, "2" * 64}, {item["input"] for item in records["k"]})

    def test_unopenable_lock_is_a_note_for_both_reading_and_writing(self):
        self.assertTrue(self.save([("k", record_of(self.module, "1"))])[0])
        saved = self.path.read_bytes()
        lock = self.module.reuse_lock_path(self.path)
        lock.unlink()
        os.symlink(lock.name, lock)
        written, out = self.save([("k", record_of(self.module, "2"))])
        self.assertFalse(written)
        self.assertIn(f"注意: 使い回しの記録を書けない({self.path}: ", out)
        self.assertIn("。今回の結果は記録しない", out)
        self.assertEqual(saved, self.path.read_bytes())
        records, notes = self.load()
        self.assertEqual({}, records)
        self.assertEqual(1, len(notes), notes)
        self.assertIn(f"注意: 使い回しの記録を読めない({self.path}: ", notes[0])

    def test_void_mark_hides_records_that_finished_before_it(self):
        # 回 A の単位が合格して終わった後に、回 B の同じ input の単位が落ち、B は記録を書けずに印を作る。
        a_pass = record_of(self.module, "1", at=iso(-30))
        self.path.mkdir(parents=True)
        written, out = self.save([("k", record_of(self.module, "1", ok=False, at=iso(-20)))], failed=True)
        self.assertFalse(written)
        self.assertIn("注意: 使い回しの記録を書けない(", out)
        self.assertTrue(self.module.reuse_void_path(self.path).exists())
        self.path.rmdir()
        # その後に A が古い合格を書いても、次の読み込みは返さない。
        self.assertTrue(self.save([("k", a_pass)])[0])
        self.assertEqual(({}, []), self.load())
        # 印より後に終わった記録は返す。
        later = record_of(self.module, "2", at=iso(30))
        self.assertTrue(self.save([("k", later)])[0])
        records, _ = self.load()
        self.assertEqual(["2" * 64], [item["input"] for item in records["k"]])

    @unittest.skipIf(os.geteuid() == 0, "root は権限の無いフォルダも調べられる")
    def test_folder_that_cannot_be_searched_is_a_note_for_reading(self):
        self.assertTrue(self.save([("k", record_of(self.module, "1"))])[0])
        # 記録のフォルダ(sub)の親を調べる権限を外す。
        mode = self.folder.stat().st_mode
        self.folder.chmod(0)
        self.addCleanup(self.folder.chmod, mode)
        records, notes = self.load()
        self.assertEqual({}, records)
        self.assertEqual(1, len(notes), notes)
        self.assertIn(f"注意: 使い回しの記録を読めない({self.path}: ", notes[0])
        self.assertIn("。全部の単位を新しく流す", notes[0])

    def test_void_mark_that_cannot_be_made_is_a_note(self):
        self.path.mkdir(parents=True)
        self.module.reuse_void_path(self.path).mkdir()
        written, out = self.save([("k", record_of(self.module, "1", ok=False))], failed=True)
        self.assertFalse(written)
        self.assertIn(f"注意: 使い回しの記録を無効にできない({self.path}: ", out)
        self.assertIn("。次の --reuse の回の前に、記録のファイルを消す", out)


def load_run_all():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("run_all_under_test", SCRIPTS / "run-all.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RunAllScopeTest(unittest.TestCase):
    """単位の範囲(run-all.py の unit_files を直接呼ぶ)。"""

    def test_units_without_a_rule_or_with_entry_outside_the_scope_have_no_scope(self):
        module = load_run_all()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tree = Path(temporary.name)
        for relative in ("plugins/x.sh", "docs/x.md", "scripts/run-all.py"):
            (tree / relative).parent.mkdir(parents=True, exist_ok=True)
            (tree / relative).write_text("x\n", encoding="utf-8")
        context, reason = module.file_context(tree)
        self.assertIsNone(reason)

        def files(key, entry):
            unit = module.run_units.Unit(key=key, label=key, run=None, describe=None, entry=entry)
            return module.unit_files(unit, context)

        self.assertIsNone(files("other:x", "plugins/x.sh"))
        self.assertIsNone(files("loop:a", "docs/x.md"))
        self.assertIsNone(files("loop:a", "plugins/missing.sh"))
        self.assertIsNone(files("loop:a", None))
        self.assertEqual(["runner", "top", "plugins"], [part for part, _ in files("loop:a", "plugins/x.sh")])
        self.assertEqual(["all"], [part for part, _ in files("unittest:test_x.py", "docs/x.md")])
        self.assertEqual((None, "範囲が決まらない"), module.reuse_of(None, None, []))


# ---- 容器の入口(docker のモック) ----

FIRST_ID = "sha256:" + "a" * 64
SECOND_ID = "sha256:" + "b" * 64

# docker のモック: 引数を 1 行の JSON で記録する。`image inspect --format` には、最初は FIRST_ID、2 回目からは
# SECOND_ID を返す(名前が別のイメージを指すようになった形)。DOCKER_MOCK_ID=fail なら失敗する。
# `run` は標準入力を読み捨てて 0 で終わる。
DOCKER_MOCK = r"""
import json, os, sys
args = sys.argv[1:]
log = os.environ["DOCKER_MOCK_LOG"]
calls = []
if os.path.exists(log):
    with open(log, encoding="utf-8") as stream:
        calls = [json.loads(line) for line in stream]
with open(log, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args, ensure_ascii=False) + "\n")
if args[:2] == ["image", "inspect"]:
    if "--format" in args:
        if os.environ.get("DOCKER_MOCK_ID") == "fail":
            sys.exit(1)
        seen = [call for call in calls if call[:2] == ["image", "inspect"] and "--format" in call]
        print("%s" if not seen else "%s")
    sys.exit(0)
if args[:1] == ["run"]:
    sys.stdin.buffer.read()
sys.exit(0)
""" % (FIRST_ID, SECOND_ID)


class RunAllContainerTest(unittest.TestCase):
    """容器の入口が、イメージの ID で起動し、RUN_ALL_IMAGE を渡すこと(本物の docker は起動しない)。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "repo"
        (self.repo / "scripts" / "container").mkdir(parents=True)
        shutil.copy2(SCRIPTS / "run-all-container.sh", self.repo / "scripts" / "run-all-container.sh")
        (self.repo / "scripts" / "container" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.repo / "requirements-dev.txt").write_text("PyYAML\n", encoding="utf-8")
        identity = ("-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false")
        support_git.git("init", "-q", cwd=self.repo)
        support_git.git("add", "-A", cwd=self.repo)
        support_git.git("commit", "-qm", "init", cwd=self.repo, options=identity)
        bindir = self.base / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(f"#!{sys.executable} -B\n" + DOCKER_MOCK, encoding="utf-8")
        docker.chmod(0o755)
        self.log = self.base / "docker.log"
        self.path = f"{bindir}{os.pathsep}{os.environ.get('PATH', os.defpath)}"

    def run_entry(self, *args, **extra):
        env = dict(os.environ, PATH=self.path, HOME=str(self.base / "home"), XDG_CACHE_HOME=str(self.base / "cache"),
                   DOCKER_MOCK_LOG=str(self.log))
        for name in ("LOOP_SELFTEST_TIMEOUT", "RUN_TIMEOUT", "LOOP_STARTUP_CASE_TIMEOUT", "RUN_ALL_IMAGE",
                     "DOCKER_MOCK_ID", "DOCKER_CONTEXT", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(name, None)
        env.update(extra)
        result = subprocess.run(["bash", str(self.repo / "scripts" / "run-all-container.sh"), *args], cwd=self.repo,
                                capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        runs = [call for call in calls if call[:1] == ["run"]]
        self.assertEqual(1, len(runs), calls)
        run = runs[0]
        at = next(index for index in range(len(run) - 1) if run[index:index + 2] == ["bash", "-c"])
        passed = [run[index + 1] for index in range(at - 1) if run[index] == "-e"]
        name = next(line for line in result.stdout.splitlines() if line.startswith("イメージ: "))[len("イメージ: "):]
        return result.stdout, run[at - 1], passed, name, run

    def test_runs_by_the_image_id_and_passes_it_to_the_runner(self):
        out, target, passed, name, _ = self.run_entry()
        self.assertRegex(name, r"^workflow-test:run-all-[0-9a-f]{12}$")
        self.assertEqual(FIRST_ID, target)
        self.assertIn(f"RUN_ALL_IMAGE={name}@{FIRST_ID}", passed)
        self.assertIn(f"イメージの ID: {FIRST_ID}", out.splitlines())
        self.assertNotIn("注意:", out)

    def test_keeps_the_first_id_even_if_the_name_moves_after_inspect(self):
        out, target, passed, name, run = self.run_entry("--worktree")
        self.assertEqual(FIRST_ID, target)
        self.assertNotIn(name, run)
        self.assertFalse([arg for arg in run if SECOND_ID in arg], run)
        self.assertIn(f"RUN_ALL_IMAGE={name}@{FIRST_ID}", passed)

    def test_unreadable_id_runs_by_name_and_passes_unknown(self):
        out, target, passed, name, _ = self.run_entry(DOCKER_MOCK_ID="fail")
        self.assertEqual(name, target)
        self.assertIn("RUN_ALL_IMAGE=不明", passed)
        self.assertIn("注意: イメージの ID を読めない。容器の中では結果を使い回さない", out.splitlines())
        self.assertNotIn("イメージの ID: ", out)

    def test_given_image_passes_unknown_even_if_the_id_is_readable(self):
        out, target, passed, name, _ = self.run_entry("--image", "other/image:tag")
        self.assertEqual("other/image:tag", name)
        self.assertEqual(FIRST_ID, target)
        self.assertIn("RUN_ALL_IMAGE=不明", passed)
        self.assertIn("注意: --image を渡したので、容器の中では結果を使い回さない", out.splitlines())
        self.assertNotIn("イメージの ID: ", out)

    def test_passed_variables_line_counts_only_the_three_host_variables(self):
        out, _, passed, name, _ = self.run_entry()
        self.assertIn("渡す環境変数: なし", out.splitlines())
        self.log.unlink()
        out, _, passed, name, _ = self.run_entry(RUN_TIMEOUT="5", RUN_ALL_IMAGE="host-value")
        self.assertIn("渡す環境変数: RUN_TIMEOUT", out.splitlines())
        self.assertEqual(sorted([f"RUN_ALL_IMAGE={name}@{FIRST_ID}", "RUN_TIMEOUT"]), sorted(passed))


if __name__ == "__main__":
    unittest.main()
