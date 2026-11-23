#!/usr/bin/env python3
"""Observe block-header acceptance with the unchanged CPython/PyYAML helper."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--helper', required=True, type=Path)
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
if sys.version_info[:3] != (3, 10, 11) or yaml.__version__ != '6.0.3':
    raise SystemExit('CPython 3.10.11 and PyYAML 6.0.3 are required')
helper = args.helper.resolve()
rows = []
env = dict(os.environ, LC_ALL='C', PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONINTMAXSTRDIGITS='4300')
with tempfile.TemporaryDirectory(prefix='s3d-block-header-') as directory:
    root = Path(directory)
    subprocess.run(['git', 'init', '-q', directory], check=True)
    (root / '.claude').mkdir()
    for style in ('|', '>'):
        for suffix in ('', ' #9', ' #0', ' #123', ' #+9', ' #-9', ' #9 +2', '+ #9', '- #9', '2 #9', '+2 #9', '2+ #9', '-2 #9', '2- #9', '9 #2', '0', '22', '++', ' 2', '\t#9', '#9'):
            for indent in (1, 2, 9):
                raw = f'unused: {style}{suffix}\n'+ ' '*indent+'abc\nsecret_paths: [safe]\n'
                (root / '.claude/project-profile.yml').write_text(raw)
                result = subprocess.run([sys.executable, str(helper), '--cwd', directory], env=env, capture_output=True)
                if result.returncode == 0 and result.stdout == b'.env\0.env.*\0.dev.vars\0safe\0' and not result.stderr:
                    expected = None
                elif result.returncode == 20 and result.stderr == 'ERROR [secret-profile] 現在 の profile YAML を読めない\n'.encode() and not result.stdout:
                    expected = 'UnreadableYaml'
                else:
                    raise SystemExit(f'unexpected helper result {raw!r}: {result!r}')
                rows.append({'id': f'block-header-{len(rows):03}', 'raw': raw, 'expected': expected})
fixture = {'python':'3.10.11', 'pyyaml':'6.0.3', 'helper_sha256':hashlib.sha256(helper.read_bytes()).hexdigest(), 'cases':rows}
path = Path(__file__).with_name('block_scalar_cases.json')
if args.check:
    if json.loads(path.read_text()) != fixture:
        raise SystemExit('block scalar reference changed')
else:
    path.write_text(json.dumps(fixture, ensure_ascii=True, indent=2)+'\n')
print(f'block scalar helper cases: {len(rows)}')
