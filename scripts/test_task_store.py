"""task_store の値と、無人ループの起動前の拒否を外部コマンドなしで確かめる。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "plugins/dev-workflow/skills/create-task/scripts/resolve-task-store.py"
LOOP = ROOT / "plugins/dev-workflow/skills/ship-task/scripts/loop.sh"


class TaskStoreTest(unittest.TestCase):
    def resolve(self, *args):
        return subprocess.run([sys.executable, "-B", str(RESOLVER), *args],
                              capture_output=True, text=True, timeout=10)

    def profile(self, text):
        # loop 全体は起動せず、製品に埋め込まれた profile reader だけを実行する。
        source = LOOP.read_text(encoding="utf-8")
        code = source.split("read -r -d '' PY_HELPER <<'PY' || true\n", 1)[1].split("\nPY\npy()", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.yml"
            path.write_text(text, encoding="utf-8")
            return subprocess.run([sys.executable, "-B", "-c", code, "profile", str(path)],
                                  capture_output=True, text=True, timeout=10)

    def test_default_and_exact_values(self):
        result = self.resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"task_store": "file"})
        for value, expected in ((None, "file"), ("", "file"), ("file", "file"), ("github-issue", "github-issue")):
            with self.subTest(value=value):
                result = self.resolve("--value-json", json.dumps(value))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"task_store": expected})
                loop = self.profile("task_store: " + json.dumps(value))
                self.assertEqual(loop.returncode, 6 if expected == "github-issue" else 0, loop.stderr)
                if expected == "github-issue":
                    self.assertIn("未対応", loop.stderr)
                else:
                    self.assertEqual(loop.stdout, "")

    def test_invalid_types_and_values_never_fall_back(self):
        for value in (False, True, 0, 1, 1.5, [], {}, ["file"], " ", "FILE", "issue", "github-issue\n"):
            with self.subTest(value=value):
                result = self.resolve("--value-json", json.dumps(value))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("file または github-issue", result.stderr)
                loop = self.profile("task_store: " + json.dumps(value))
                self.assertEqual(loop.returncode, 1, loop.stderr)
                self.assertIn("file または github-issue", loop.stderr)

    def test_invalid_json_and_yaml_stop(self):
        for value in ("", "file", "[", "null trailing"):
            with self.subTest(value=value):
                result = self.resolve("--value-json", value)
                self.assertEqual(result.returncode, 2)
                self.assertIn("JSON", result.stderr)
        for value in ("task_store: [", "[]", "false"):
            with self.subTest(value=value):
                self.assertEqual(self.profile(value).returncode, 1)

    def test_key_absence_and_empty_profile_keep_existing_defaults(self):
        for value in ("", "{}", "task_dir: tasks"):
            with self.subTest(value=value):
                result = self.profile(value)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_rejects_unsupported_store_during_profile_validation(self):
        source = LOOP.read_text(encoding="utf-8")
        profile = source.split("# ── §2 の 9: profile", 1)[1].split("# ── §2 の 10:", 1)[0]
        self.assertIn('6) die 20 task-store-unsupported', profile)
        self.assertIn('*) die 20 profile-invalid', profile)


if __name__ == "__main__":
    unittest.main()
