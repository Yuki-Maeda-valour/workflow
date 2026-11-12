"""旧版 check_links.py(update-doc のリンク検査)の動作を、固定期待で固める(R4U #480 の Task 1.1)。

- 固定期待は native/fixtures/r4u/legacy-cases.json。Rust 版の比較器と同じものを使う。
- 基準の環境は LC_ALL=C(PATH のほかの環境変数を渡さない)。このとき旧 Python は UTF-8 と surrogateescape で動く。
- 標準ライブラリの行番号に頼る呼出履歴は、ここでは比べない(比較器が本家 3.10.11 で比べる)。
- 2 種類の照合の生成入力(matcher-corpus.json)は、生成のスクリプトを流し直して同じバイト列になることを確かめる。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "native/fixtures/r4u"
LEGACY_REL = "plugins/dev-workflow/skills/update-doc/scripts/check_links.py"
LEGACY = REPO_ROOT / LEGACY_REL
CASES = json.loads((FIXTURES / "legacy-cases.json").read_text(encoding="utf-8"))
REMOVED = ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONCOERCECLOCALE")


def raw(value):
    """固定期待のバイト列(文字列は UTF-8、{"hex": ...} は 16 進)を bytes にする。"""
    if isinstance(value, dict):
        if set(value) != {"hex"}:
            raise ValueError(f"バイト列の形が違う: {value!r}")
        return bytes.fromhex(value["hex"])
    if not isinstance(value, str):
        raise ValueError(f"バイト列の形が違う: {value!r}")
    return value.encode("utf-8")


def base_environment():
    """基準の環境。PATH と LC_ALL だけを渡し、外す 3 つの変数が残らないことも確かめる。"""
    environment = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}
    if any(name in environment for name in REMOVED):
        raise AssertionError("外す変数が残っている")
    return environment


def build_tree(root, tree):
    for op in tree:
        path = os.path.join(os.fsencode(root), raw(op["path"]))
        kind = op["op"]
        if kind == "dir":
            os.mkdir(path)
        elif kind == "file":
            with open(path, "xb") as stream:
                stream.write(raw(op["data"]))
        elif kind == "symlink":
            os.symlink(raw(op["target"]), path)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            raise ValueError(f"未知の操作: {kind}")


def run_legacy(root, argv):
    return subprocess.run([sys.executable, str(LEGACY), *argv], cwd=root, env=base_environment(),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)


class LegacyFixtureShapeTest(unittest.TestCase):
    def test_fixture_matches_unchanged_legacy(self):
        self.assertEqual(LEGACY_REL, CASES["legacy"]["path"])
        self.assertEqual(CASES["legacy"]["sha256"], hashlib.sha256(LEGACY.read_bytes()).hexdigest(),
                         "旧版が変わった。固定期待を作り直すか、旧版を戻す")

    def test_case_ids_are_unique_and_not_empty(self):
        ids = [case["id"] for case in CASES["cases"]]
        self.assertGreater(len(ids), 0)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(CASES["case_count"], len(ids))
        self.assertIn("basic-doc-example", ids)

    def test_base_environment_uses_utf8_and_surrogateescape(self):
        # 比較の前提: 旧 Python の標準出力が UTF-8 と surrogateescape、標準エラーが backslashreplace
        done = subprocess.run(
            [sys.executable, "-c", "import sys; print(sys.stdout.encoding, sys.stdout.errors, "
                                   "sys.stderr.encoding, sys.stderr.errors)"],
            env=base_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30)
        self.assertEqual((0, b"utf-8 surrogateescape utf-8 backslashreplace\n", b""),
                         (done.returncode, done.stdout, done.stderr))

    def test_basic_doc_example_is_completion_condition_four(self):
        case = next(case for case in CASES["cases"] if case["id"] == "basic-doc-example")
        self.assertEqual(["docs"], case["argv"])
        self.assertEqual(1, case["expected"]["exit"])
        self.assertEqual(
            "検査ファイル数: 1\n"
            "WARN  docs/a.md:1: 記載パスが見つからない(コード片なら無視可) -> src/none.py\n"
            "ERROR docs/a.md:1: リンク切れ -> missing.md\n"
            "結果: ERROR 1 件 / WARN 1 件\n",
            case["expected"]["stdout"])


class LegacyCasesTest(unittest.TestCase):
    def test_legacy_matches_fixed_expectations(self):
        ran = 0
        for case in CASES["cases"]:
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as temp:
                build_tree(temp, case["tree"])
                done = run_legacy(temp, [os.fsdecode(raw(arg)) for arg in case["argv"]])
                expected = case["expected"]
                self.assertEqual(
                    (expected["exit"], raw(expected["stdout"]), raw(expected["stderr"])),
                    (done.returncode, done.stdout, done.stderr))
                ran += 1
        self.assertEqual(len(CASES["cases"]), ran)


class MatcherCorpusTest(unittest.TestCase):
    def test_corpus_is_reproducible_from_legacy_patterns(self):
        # 旧版の正規表現を当て直し、固定期待と同じバイト列になることを確かめる
        if sys.version_info[:2] != (3, 10):
            self.fail("生成入力の確かめは Python 3.10 系で流す")
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "corpus.json"
            done = subprocess.run(
                [sys.executable, "-B", str(FIXTURES / "gen_matcher_corpus.py"), "--out", str(out)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual((0, b"", b""), (done.returncode, done.stdout, done.stderr))
            self.assertEqual((FIXTURES / "matcher-corpus.json").read_bytes(), out.read_bytes())

    def test_corpus_has_enough_cases_and_matches(self):
        corpus = json.loads((FIXTURES / "matcher-corpus.json").read_text(encoding="utf-8"))
        self.assertEqual(corpus["case_count"], len(corpus["cases"]))
        self.assertGreaterEqual(corpus["case_count"], 10000)
        self.assertGreaterEqual(corpus["link_match_count"], 1000)
        self.assertGreaterEqual(corpus["code_match_count"], 1000)
        self.assertEqual(CASES["legacy"]["sha256"], corpus["legacy"]["sha256"])


if __name__ == "__main__":
    unittest.main()
