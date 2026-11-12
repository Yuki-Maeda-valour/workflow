#!/usr/bin/env python3
"""Re-observe fixed recipes through the unmodified standalone helper.

This script is a developer tool. The native command never launches Python.
Run with CPython 3.10.11 and PyYAML 6.0.3. The helper hash is required because
extra wrapper frames would change the recursion budget being measured.
"""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

from recursion_inputs import build

if sys.version_info[:3] != (3, 10, 11) or yaml.__version__ != "6.0.3":
    raise SystemExit("CPython 3.10.11 and PyYAML 6.0.3 are required")
root = pathlib.Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--helper", required=True, type=pathlib.Path)
parser.add_argument("--check", action="store_true", help="compare observations without replacing the fixed fixture")
args = parser.parse_args()
helper = args.helper.resolve()
path = root / "recursion_cases.json"
fixture = json.loads(path.read_text())
if hashlib.sha256(helper.read_bytes()).hexdigest() != fixture["helper_sha256"]:
    raise SystemExit("legacy helper source hash differs")
env = dict(os.environ, LC_ALL="C", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONINTMAXSTRDIGITS="4300")
with tempfile.TemporaryDirectory(prefix="s3d-recursion-reference-") as scratch:
    folder = pathlib.Path(scratch)
    subprocess.run(["git", "init", "-q", scratch], check=True)
    (folder / ".claude").mkdir()
    for row in fixture["cases"]:
        raw = build(row["recipe"]).encode("utf-8")
        (folder / ".claude/project-profile.yml").write_bytes(raw)
        result = subprocess.run([sys.executable, str(helper), "--cwd", scratch], env=env, capture_output=True, check=False)
        if result.returncode == 0:
            expected = None
        elif result.returncode == 20 and result.stderr == "ERROR [secret-profile] 現在 の profile YAML を読めない\n".encode():
            expected = "UnreadableYaml"
        elif result.returncode == 20 and result.stderr == "ERROR [secret-profile] 現在 の profile は map でなければならない\n".encode():
            expected = "NotMapping"
        else:
            raise SystemExit(f"unexpected reference result: {row['id']} {result.returncode} {result.stderr!r}")
        digest = hashlib.sha256(raw).hexdigest()
        if args.check and (expected != row["expected"] or digest != row["sha256"]):
            raise SystemExit(f"reference changed: {row['id']}")
        row.update(expected=expected, sha256=digest)
if not args.check:
    path.write_text(json.dumps(fixture, ensure_ascii=True, indent=2) + "\n")
print(f"standalone helper recursion cases: {len(fixture['cases'])}")
