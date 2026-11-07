"""implement-guard-selftest.sh の単位(--list・--check-units・--only L-<記号>)と、保護領域の置き場の作り直しの回帰テスト。

重いケースは流さない(--only A1 の 1 件だけ、置き場の作り直しの確かめに流す)。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
GUARD_DIR = REPO / "plugins" / "dev-workflow" / "skills" / "do-task" / "scripts"
SELFTEST = GUARD_DIR / "implement-guard-selftest.sh"
TARGET = GUARD_DIR / "implement-guard.sh"
# 変異の目印を持たない変異(復元 helper の階層検査を外す e)
MARKLESS_MUTANT = "e"


def run_selftest(*args, home=None, env_extra=None, timeout=180):
    env = dict(os.environ)
    env.pop("IMPLEMENT_GUARD", None)
    env.pop("IMPLEMENT_GUARD_SELFTEST_PROT", None)
    if home is not None:
        env["HOME"] = str(home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SELFTEST), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def make_home():
    """本物の HOME の下に、一意な名前の空のフォルダを作る(/tmp・$TMPDIR の下だと selftest が止めるため)。"""
    return Path(tempfile.mkdtemp(prefix=".test-implement-guard-units-", dir=Path.home()))


class UnitsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listed = run_selftest("--list")
        cls.units = cls.listed.stdout.splitlines()

    def test_list_prints_every_unit_once(self):
        self.assertEqual(0, self.listed.returncode, self.listed.stderr)
        self.assertEqual("", self.listed.stderr)
        self.assertEqual(79, len(self.units))
        self.assertEqual(len(self.units), len(set(self.units)))
        for unit in self.units:
            self.assertRegex(unit, r"^[A-Za-z0-9-]+$")
            for bad in (",", " ", ":"):
                self.assertNotIn(bad, unit)
        groups = [u for u in self.units if re.fullmatch(r"[A-Z]", u)]
        self.assertEqual(12, len(groups))
        self.assertEqual(27, len([u for u in self.units if re.fullmatch(r"M[0-9]+", u)]))
        self.assertEqual(14, len([u for u in self.units if re.fullmatch(r"N[0-9]+", u)]))
        self.assertEqual(26, len([u for u in self.units if u.startswith("L-")]))

    def test_mutant_units_match_the_marks_in_the_target(self):
        marks = set(re.findall(r"# MUT:([a-z])\s*$", TARGET.read_text(encoding="utf-8"), re.M))
        self.assertEqual(25, len(marks))
        listed = {u[2:] for u in self.units if u.startswith("L-")}
        self.assertEqual(marks | {MARKLESS_MUTANT}, listed)

    def test_list_creates_nothing(self):
        home = make_home()
        try:
            result = run_selftest("--list", home=home)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(self.units, result.stdout.splitlines())
            self.assertEqual([], list(home.iterdir()))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_list_with_other_arguments_is_a_usage_error(self):
        for extra in (["-v"], ["--only", "A1"], ["--check-units", "A"]):
            result = run_selftest("--list", *extra)
            self.assertEqual(1, result.returncode, extra)
            self.assertEqual("", result.stdout)

    def test_check_units_accepts_the_whole_list(self):
        result = run_selftest("--check-units", ",".join(self.units))
        self.assertEqual(0, result.returncode, result.stderr)

    def test_check_units_reports_a_missing_unit(self):
        rest = [u for u in self.units if u != "M5"]
        result = run_selftest("--check-units", ",".join(rest))
        self.assertEqual(1, result.returncode)
        self.assertIn("M5", result.stderr)
        rest = [u for u in self.units if u != "L-q"]
        result = run_selftest("--check-units", ",".join(rest))
        self.assertEqual(1, result.returncode)
        self.assertIn("L-q", result.stderr)

    def test_check_units_reports_overlaps_and_unknown_units(self):
        result = run_selftest("--check-units", ",".join([*self.units, "A1"]))
        self.assertEqual(1, result.returncode)
        self.assertIn("A1", result.stderr)
        for extra in ("L-zz", "Q"):
            result = run_selftest("--check-units", ",".join([*self.units, extra]))
            self.assertEqual(1, result.returncode, extra)
            self.assertIn(extra, result.stderr)

    def test_check_units_creates_nothing(self):
        home = make_home()
        try:
            result = run_selftest("--check-units", ",".join(self.units), home=home)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], list(home.iterdir()))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_only_rejects_an_unknown_mutant(self):
        result = run_selftest("--only", "L-zz")
        self.assertEqual(1, result.returncode)
        self.assertIn("ERROR: --only のケース ID が無い: L-zz", result.stderr)


class ProtectionBaseTest(unittest.TestCase):
    def test_base_removed_before_mktemp_is_made_again(self):
        home = make_home()
        bin_dir = Path(tempfile.mkdtemp(prefix="test-implement-guard-bin-"))
        try:
            flag = bin_dir / "removed-once"
            real = shutil.which("mktemp")
            stub = bin_dir / "mktemp"
            stub.write_text(
                "#!/bin/bash\n"
                f'case "$*" in *selftest-guard-*)\n'
                f'  if [ ! -e "{flag}" ]; then : >"{flag}"; rm -rf "$HOME/.local/state/dev-workflow"; fi ;;\n'
                "esac\n"
                f'exec "{real}" "$@"\n',
                encoding="utf-8",
            )
            stub.chmod(0o755)
            result = run_selftest(
                "--only", "A1", home=home, env_extra={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
            )
            self.assertTrue(flag.exists(), "スタブが置き場を消していない")
            self.assertEqual(0, result.returncode, result.stdout[-2000:] + result.stderr)
            self.assertNotIn("保護領域を作れない", result.stderr)
            base = home / ".local" / "state" / "dev-workflow"
            self.assertEqual([], sorted(p.name for p in base.glob("selftest-guard-*")) if base.exists() else [])
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(bin_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
