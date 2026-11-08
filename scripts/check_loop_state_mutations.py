#!/usr/bin/env python3
"""Git 設定の新規テストが、独立コピーに入れた不具合を検出するか確かめる。"""
from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
PRODUCT = pathlib.Path("plugins/dev-workflow/skills/ship-task/scripts/loop-state.py")
TEST_FILE = pathlib.Path("scripts/test_loop_state.py")
TEST_CLASS = "scripts.test_loop_state.ConfigSnapshotContractTest"
EFFECTIVE = ('    raw = run_git(top, "config", "--show-origin", "--show-scope", '
             '"--includes", "--null", "--list", budget=budget)\n')
RESULT = ('    return {"effective_sha256": hashlib.sha256(raw).hexdigest(), '
          '"origins": sorted(observed, key=lambda x: x["path"]),\n'
          '            "candidates": config_candidates(top, common, git_dir or common, budget)}')

# Each mutation selects one new test and an assertion-specific marker.  A load
# error, timeout, skipped test or arbitrary nonzero status cannot pass this check.
MUTATIONS = (
    ("early-effective", "実効 Git の前倒し", "orders_reads_conditions_rechecks_and_candidates",
     "CONFIG_ORDER: graph before effective Git",
     ((EFFECTIVE, ""), ("    roots = config_origins(base, top)\n",
                         "    roots = config_origins(base, top)\n" + EFFECTIVE))),
    ("truncated-queue", "増えた待ち行列の無視", "drains_nested_hasconfig_queue",
     "CONFIG_QUEUE: unexpected Stop: include の到達先を事前に安全に検査できない",
     (("    while cursor < len(pending):\n",
       "    initial_pending = len(pending)\n    while cursor < initial_pending:\n"),)),
    ("new-budget", "途中で上限の入れ物を新設", "passes_same_budget_through_every_phase",
     "CONFIG_BUDGET_ID:",
     ((EFFECTIVE, "    budget = Budget(budget.items, budget.total, budget.one, 10)\n" + EFFECTIVE),)),
    ("extended-deadline", "途中で期限を延長", "keeps_callers_deadline_at_late_phases",
     "CONFIG_DEADLINE",
     ((EFFECTIVE, "    budget.deadline += 60\n" + EFFECTIVE),)),
    ("missing-cycle-check", "遅れて有効になった循環の検査を削除",
     "rejects_normal_and_deferred_cycles_before_effective_git",
     "CONFIG_CYCLE: effective Git reached before cycle rejection",
     (("    for node in edges:\n        no_cycle(node)\n", ""),)),
    ("missing-recheck", "実効 Git 後の再検査を削除", "rejects_origin_change_after_effective_git",
     "CONFIG_RECHECK",
     (("    for path in actual:\n"
       "        after, _ = safe_config_file(top, path, budget, trusted)\n"
       "        if after != checked[path]:\n"
       '            raise Stop("config が実効値の読取中に変わった")\n', ""),)),
    ("result-key", "返却値のキーを変更", "returns_exact_values_and_ordered_candidates", "CONFIG_KEYS",
     ((RESULT, RESULT.replace('"effective_sha256"', '"effective_digest"')),)),
    ("result-digest", "実効設定の SHA256 を変更", "returns_exact_values_and_ordered_candidates", "CONFIG_DIGEST",
     ((RESULT, RESULT.replace("hashlib.sha256(raw)", 'hashlib.sha256(b"wrong")')),)),
    ("result-origin-order", "出典の順序を反転", "returns_exact_values_and_ordered_candidates", "CONFIG_ORIGINS_ORDER",
     ((RESULT, RESULT.replace('key=lambda x: x["path"])', 'key=lambda x: x["path"], reverse=True)')),)),
    ("result-candidate-state", "不在の候補を存在へ変更", "returns_exact_values_and_ordered_candidates", "CONFIG_CANDIDATES",
     (('            result.append({"kind": kind, "path": path, "state": "absent"})\n',
       '            result.append({"kind": kind, "path": path, "state": "present"})\n'),)),
)

CHILD = r'''
import io, json, sys, unittest
class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.assertions = []
    def remember(self, test, error):
        self.assertions.append({"test": test.id(), "exception": error[0].__name__, "message": str(error[1])})
    def addFailure(self, test, error):
        self.remember(test, error)
        super().addFailure(test, error)
    def addSubTest(self, test, subtest, error):
        if error is not None and issubclass(error[0], test.failureException):
            self.remember(subtest, error)
        super().addSubTest(test, subtest, error)
suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=2, resultclass=Result).run(suite)
def rows(values):
    return [{"test": test.id(), "detail": detail} for test, detail in values]
print(json.dumps({"tests_run": result.testsRun, "failures": result.assertions,
                  "errors": rows(result.errors), "skipped": rows(result.skipped),
                  "expected_failures": rows(result.expectedFailures),
                  "unexpected_successes": [test.id() for test in result.unexpectedSuccesses]},
                 ensure_ascii=False))
sys.exit(0 if result.wasSuccessful() else 1)
'''


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def run(root: pathlib.Path, test: str) -> tuple[int, dict, float]:
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-B", "-c", CHILD, test], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if result.stderr:
        raise RuntimeError("検査プロセスが標準エラーを出した: " + result.stderr.decode("utf-8", "replace"))
    record = json.loads(result.stdout)
    if (record["errors"] or record["skipped"] or record["expected_failures"]
            or record["unexpected_successes"] or record["tests_run"] == 0):
        raise RuntimeError("検査失敗以外の結果を検出成功とは扱わない: " + json.dumps(record, ensure_ascii=False))
    return result.returncode, record, time.monotonic() - started


def copied_tree(root: pathlib.Path, product: bytes, tests: bytes) -> None:
    for relative, raw in ((PRODUCT, product), (TEST_FILE, tests)):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def emit(kind: str, test: str, source: bytes, changed: bytes, tests: bytes,
         code: int, record: dict, seconds: float) -> None:
    failures = [{"test": row["test"], "assertion": row["exception"] + ": " + row["message"]}
                for row in record["failures"]]
    print(json.dumps({"case": kind, "test": test, "source_sha256": digest(source),
                      "copy_sha256": digest(changed), "tests_sha256": digest(tests),
                      "exit_code": code, "tests_run": record["tests_run"],
                      "assertion_failures": failures, "seconds": round(seconds, 3)}, ensure_ascii=False), flush=True)


def main() -> int:
    source = (ROOT / PRODUCT).read_bytes()
    tests = (ROOT / TEST_FILE).read_bytes()
    tree = ast.parse(tests)
    test_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                      and node.name == "ConfigSnapshotContractTest")
    expected_count = sum(isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
                         for node in test_class.body)
    original_mode = (ROOT / PRODUCT).stat().st_mode
    started = time.monotonic()
    try:
        code, record, seconds = run(ROOT, TEST_CLASS)
        if code != 0 or record["failures"] or record["tests_run"] != expected_count:
            raise RuntimeError("元の新規テストが成功しない: " + json.dumps(record, ensure_ascii=False))
        emit("original", TEST_CLASS, source, source, tests, code, record, seconds)
        with tempfile.TemporaryDirectory(prefix="loop-state-mutations-") as temporary:
            parent = pathlib.Path(temporary)
            baseline = parent / "unchanged"
            copied_tree(baseline, source, tests)
            code, record, seconds = run(baseline, TEST_CLASS)
            if code != 0 or record["failures"] or record["tests_run"] != expected_count:
                raise RuntimeError("無変更コピーが成功しない: " + json.dumps(record, ensure_ascii=False))
            emit("unchanged-copy", TEST_CLASS, source, source, tests, code, record, seconds)
            for name, label, method, marker, edits in MUTATIONS:
                changed = source.decode("utf-8")
                for before, after in edits:
                    if changed.count(before) != 1:
                        raise RuntimeError(f"{name}: 差替え場所が 1 か所ではない")
                    changed = changed.replace(before, after, 1)
                ast.parse(changed)
                raw = changed.encode("utf-8")
                if raw == source:
                    raise RuntimeError(f"{name}: 不具合が適用されていない")
                root = parent / name
                copied_tree(root, raw, tests)
                target = TEST_CLASS + ".test_config_snapshot_" + method
                code, record, seconds = run(root, target)
                if (code != 1 or record["tests_run"] != 1 or not record["failures"]
                        or any(not (row["test"] == target or row["test"].startswith(target + " ("))
                               or marker not in row["message"] or row["exception"] != "AssertionError"
                               for row in record["failures"])):
                    raise RuntimeError(f"{name}: 狙った検査の失敗ではない: " + json.dumps(record, ensure_ascii=False))
                emit(name + ": " + label, target, source, raw, tests, code, record, seconds)
                # Each case is a new independent byte copy.  Do not keep a
                # mutated product around for the next case to inherit.
                shutil.rmtree(root)
        if ((ROOT / PRODUCT).read_bytes() != source or (ROOT / PRODUCT).stat().st_mode != original_mode
                or (ROOT / TEST_FILE).read_bytes() != tests):
            raise RuntimeError("元の製品またはテストが実行中に変わった")
        print(f"結果: 合格 / 無変更 2 件・不具合 {len(MUTATIONS)} 件 / {time.monotonic() - started:.3f} 秒")
        return 0
    except (OSError, ValueError, SyntaxError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("異常: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
