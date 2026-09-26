import json
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins/dev-workflow/skills/create-task/scripts"
SCRIPT = SCRIPTS / "candidate-keys.py"

CANDIDATE = """# 監査-注文の詳細に認可を足す

> **ステータス**: 候補
> **作成日**: 2026-09-26
> **発見元**: data-audit(/data-audit --candidates --quick)
> **指摘キー**: data-audit:authz:route:src/routes/orders.ts#ordersRouter:GET /:id
> **発見時点**: 819fcc33d0c0
> **深刻度**: 重大 / **確度**: 確実

## 指摘

注文の詳細が認可なしで返る。

## 根拠(発見時点の path:line)

- src/routes/orders.ts:12
"""


class CandidateKeysTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name) / "tasks"
        self.dir.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args, stdin=None, script=SCRIPT, env=None):
        completed = subprocess.run(
            [sys.executable, str(script), *args],
            input=stdin,
            capture_output=True,
            check=False,
            env=env,
        )
        return completed.returncode, completed.stdout.decode("utf-8"), completed.stderr.decode("utf-8")

    def collect(self, task_dir=None):
        code, out, _ = self.run_script(f"--task-dir={task_dir or self.dir}")
        return code, json.loads(out)

    def check(self, text, *args):
        path = self.dir / "候補_x.md"
        path.write_text(text, encoding="utf-8")
        code, out, _ = self.run_script("--check", *args, str(path))
        return code, json.loads(out)

    def write(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")

    def by_file(self, result):
        return {item["file"]: item for item in result["files"]}

    # --- 収集 ---

    def test_collects_each_state(self):
        for state in ("進行中", "完了", "中断", "保留", "候補"):
            self.write(f"{state}_{state}の名.md", f"# x\n\n> **指摘キー**: refactor:file:{state}.py\n\n## 概要\n")
        code, result = self.collect()
        self.assertEqual(0, code)
        files = self.by_file(result)
        self.assertEqual(5, len(files))
        for state in ("進行中", "完了", "中断", "保留", "候補"):
            with self.subTest(state=state):
                item = files[f"{state}_{state}の名.md"]
                self.assertEqual(state, item["state"])
                self.assertEqual(f"{state}の名", item["name"])
                self.assertEqual([f"refactor:file:{state}.py"], item["keys"])
                self.assertIsNone(item["skipped"])
                self.assertEqual([], item["problems"])
        self.assertEqual(sorted(f"{state}の名" for state in ("進行中", "完了", "中断", "保留", "候補")), result["names"])

    def test_collects_multiple_key_lines(self):
        self.write(
            "進行中_まとめた.md",
            "# x\n\n> **指摘キー**: refactor:file:a.py\n> **指摘キー**: refactor:file:b.py\n\n## 概要\n",
        )
        _, result = self.collect()
        self.assertEqual(["refactor:file:a.py", "refactor:file:b.py"], result["files"][0]["keys"])

    def test_normalizes_keys(self):
        decomposed = unicodedata.normalize("NFD", "refactor:file:src/ガ.py")
        self.write("候補_a.md", f"# x\n> **指摘キー**: {decomposed}   \n> **指摘キー**:   data-audit:authz:route:a.ts#r:GET   /x\n")
        _, result = self.collect()
        self.assertEqual(["refactor:file:src/ガ.py", "data-audit:authz:route:a.ts#r:GET /x"], result["files"][0]["keys"])

    def test_skipped_value_is_the_capture_group(self):
        self.write("候補_a.md", CANDIDATE.replace("> **発見時点**", "> **見送り**: 2026-09-27 — 仕様どおり\n> **発見時点**"))
        _, result = self.collect()
        item = result["files"][0]
        self.assertEqual("2026-09-27 — 仕様どおり", item["skipped"])
        self.assertEqual([], item["problems"])

    def test_malformed_skip_lines_are_problems(self):
        for separator in ("-", "―"):
            with self.subTest(separator=separator):
                self.write("候補_a.md", CANDIDATE.replace("> **発見時点**", f"> **見送り**: 2026-09-27 {separator} 理由\n> **発見時点**"))
                code, result = self.collect()
                self.assertEqual(0, code)
                item = result["files"][0]
                self.assertIsNone(item["skipped"])
                self.assertEqual(["skipped-malformed"], [problem["code"] for problem in item["problems"]])

    def test_reads_only_header_outside_fences(self):
        self.write(
            "候補_a.md",
            "# x\n"
            "```\n"
            "> **指摘キー**: fenced\n"
            "## フェンスの中の見出し\n"
            "> **見送り**: 2026-09-27 — フェンスの中\n"
            "```\n"
            "> **指摘キー**: header\n"
            "\n"
            "## 指摘\n"
            "> **指摘キー**: after-h2\n"
            "> **見送り**: 2026-09-27 — 見出しの後\n"
            "> **見送り**: 2026-09-27 - 見出しの後\n",
        )
        _, result = self.collect()
        item = result["files"][0]
        self.assertEqual(["header"], item["keys"])
        self.assertIsNone(item["skipped"])
        self.assertEqual([], item["problems"])

    def test_excludes_symlinks_and_non_state_names(self):
        target = Path(self.temp.name) / "target.md"
        target.write_text("# x\n> **指摘キー**: linked\n", encoding="utf-8")
        (self.dir / "候補_リンク.md").symlink_to(target)
        self.write("候補_.md", "# x\n> **指摘キー**: empty-name\n")
        self.write("README.md", "# x\n> **指摘キー**: readme\n")
        (self.dir / "候補_ディレクトリ.md").mkdir()
        (self.dir / "sub").mkdir()
        (self.dir / "sub/候補_深い.md").write_text("# x\n> **指摘キー**: deep\n", encoding="utf-8")
        self.write("候補_本物.md", "# x\n> **指摘キー**: real\n")
        code, result = self.collect()
        self.assertEqual(0, code)
        self.assertEqual(["候補_本物.md"], [item["file"] for item in result["files"]])
        self.assertEqual(["本物"], result["names"])

    def test_unreadable_file_exits_1(self):
        (self.dir / "候補_壊れた.md").write_bytes(b"# x\n> **\xff\xfe**\n")
        self.write("候補_読める.md", "# x\n> **指摘キー**: ok\n")
        code, result = self.collect()
        self.assertEqual(1, code)
        files = self.by_file(result)
        self.assertEqual(["unreadable"], [problem["code"] for problem in files["候補_壊れた.md"]["problems"]])
        self.assertEqual(["ok"], files["候補_読める.md"]["keys"])

    def test_missing_directory_is_empty(self):
        code, result = self.collect(Path(self.temp.name) / "missing")
        self.assertEqual((0, {"files": [], "names": []}), (code, result))

    def test_usage_errors_exit_2(self):
        a_file = Path(self.temp.name) / "file"
        a_file.touch()
        broken = Path(self.temp.name) / "broken"
        broken.symlink_to("missing-target")
        cases = [
            (),
            (f"--task-dir={a_file}",),
            (f"--task-dir={broken}",),
            (f"--task-dir={self.dir}", "extra"),
            (f"--task-dir={self.dir}", "--source=refactor"),
            ("--check",),
            ("--check", f"--task-dir={self.dir}", "x.md"),
            ("--check", "--source=unknown", "x.md"),
            ("--unknown",),
        ]
        for args in cases:
            with self.subTest(args=args):
                code, out, _ = self.run_script(*args)
                self.assertEqual(2, code)
                self.assertEqual("", out)

    # --- 検査(--check) ---

    def test_check_ok(self):
        code, result = self.check(CANDIDATE, "--source=data-audit")
        self.assertEqual(0, code)
        self.assertEqual(
            {
                "ok": True,
                "source": "data-audit",
                "keys": ["data-audit:authz:route:src/routes/orders.ts#ordersRouter:GET /:id"],
                "problems": [],
            },
            result,
        )

    def test_check_reads_stdin(self):
        code, out, _ = self.run_script("--check", "--source=data-audit", "-", stdin=CANDIDATE.encode("utf-8"))
        self.assertEqual(0, code)
        self.assertTrue(json.loads(out)["ok"])

    def test_check_without_source_option_accepts_any_known_source(self):
        code, result = self.check(CANDIDATE.replace("data-audit(/data-audit --candidates --quick)", "refactor"))
        self.assertEqual((0, "refactor"), (code, result["source"]))

    def test_check_problems(self):
        source_line = "> **発見元**: data-audit(/data-audit --candidates --quick)\n"
        key_line = "> **指摘キー**: data-audit:authz:route:src/routes/orders.ts#ordersRouter:GET /:id\n"
        cases = {
            "source-0": (CANDIDATE.replace(source_line, ""), "source-count"),
            "source-2": (CANDIDATE.replace(source_line, source_line + "> **発見元**: refactor\n"), "source-count"),
            "source-unknown": (CANDIDATE.replace(source_line, "> **発見元**: someone\n"), "source-count"),
            "source-mismatch": (CANDIDATE, "source-mismatch", "--source=refactor"),
            "key-0": (CANDIDATE.replace(key_line, ""), "key-count"),
            "key-2": (CANDIDATE.replace(key_line, key_line + "> **指摘キー**: data-audit:authz:handler:a.py#b\n"), "key-count"),
            "unattended": (CANDIDATE.replace(key_line, key_line + "> **無人実行**: 可\n"), "unattended-meta"),
            "skipped": (CANDIDATE.replace(key_line, key_line + "> **見送り**: 2026-09-27 — 理由\n"), "skipped"),
            "skipped-hyphen": (CANDIDATE.replace(key_line, key_line + "> **見送り**: 2026-09-27 - 理由\n"), "skipped-malformed"),
            "skipped-bar": (CANDIDATE.replace(key_line, key_line + "> **見送り**: 2026-09-27 ― 理由\n"), "skipped-malformed"),
            "record": (CANDIDATE + "\n## 追加修正記録\n", "record-section"),
            "fenced-checkbox": (CANDIDATE + "\n```\n- [ ] 見本\n```\n", "checkbox"),
            "checkbox": (CANDIDATE + "\n  - [x] 字下げ\n", "checkbox"),
        }
        for label, (text, expected, *options) in cases.items():
            with self.subTest(case=label):
                code, result = self.check(text, *(options or ["--source=data-audit"]))
                self.assertEqual(1, code)
                self.assertFalse(result["ok"])
                self.assertEqual([expected], [problem["code"] for problem in result["problems"]])
                self.assertTrue(all(problem["detail"] for problem in result["problems"]))

    def test_check_ignores_fenced_record_heading_and_meta(self):
        text = CANDIDATE.replace(
            "> **発見時点**",
            "```\n> **発見元**: refactor\n> **指摘キー**: fenced\n> **無人実行**: 可\n## 追加修正記録\n```\n> **発見時点**",
        )
        code, result = self.check(text, "--source=data-audit")
        self.assertEqual((0, []), (code, result["problems"]))

    def test_check_unreadable(self):
        path = self.dir / "候補_壊れた.md"
        path.write_bytes(b"\xff\xfe")
        code, out, _ = self.run_script("--check", str(path))
        result = json.loads(out)
        self.assertEqual(1, code)
        self.assertEqual(["unreadable"], [problem["code"] for problem in result["problems"]])
        code, out, _ = self.run_script("--check", str(self.dir / "missing.md"))
        self.assertEqual((1, False), (code, json.loads(out)["ok"]))

    # --- __pycache__ ---

    def test_does_not_write_bytecode_into_scripts(self):
        env = {key: value for key, value in os.environ.items() if key not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
        copied = Path(self.temp.name) / "scripts"
        shutil.copytree(SCRIPTS, copied, ignore=shutil.ignore_patterns("__pycache__"))
        code, _, _ = self.run_script(f"--task-dir={self.dir}", script=copied / "candidate-keys.py", env=env)
        self.assertEqual(0, code)
        self.assertFalse((copied / "__pycache__").exists())

        # 対照: 同じ環境で sys.dont_write_bytecode を外した写しは __pycache__ を作る(この検査が空振りしない)
        control = Path(self.temp.name) / "control"
        shutil.copytree(SCRIPTS, control, ignore=shutil.ignore_patterns("__pycache__"))
        source = (control / "candidate-keys.py").read_text(encoding="utf-8")
        self.assertEqual(1, source.count("sys.dont_write_bytecode = True\n"))
        (control / "candidate-keys.py").write_text(source.replace("sys.dont_write_bytecode = True\n", ""), encoding="utf-8")
        code, _, _ = self.run_script(f"--task-dir={self.dir}", script=control / "candidate-keys.py", env=env)
        self.assertEqual(0, code)
        self.assertTrue((control / "__pycache__").is_dir())


if __name__ == "__main__":
    unittest.main()
