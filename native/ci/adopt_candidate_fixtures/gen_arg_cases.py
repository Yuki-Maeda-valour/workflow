#!/usr/bin/env python3
"""旧 adopt-candidate.py の引数解析の期待を、旧 Python 3.10.11 から採る(R4C 用)。

旧スクリプトの `adopt` を差し替えて、解析後の値だけを取り出す。標準出力・標準エラー・終了値は
旧スクリプトの `main()` の実出力をそのまま記録する。Rust 側の単体テストと比較器が読む固定期待の元になる。

    python3 gen_arg_cases.py --python <旧 Python> --script <adopt-candidate.py> --count 160 --seed 1 --out arg_cases.json

旧 Python は本家 CPython 3.10.11 を使う。標準ライブラリの中身が違う Python を渡すと、
argparse.py の SHA-256 が記録値と違うので失敗にする(skip にしない)。
"""
import argparse
import json
import os
import random
import subprocess
import sys

ARGPARSE_SHA256 = 'a238b700513237e10d26056e42c5b4e42d4961ea70e15ebeef4c61c76615f004'

HARNESS = r'''
import sys
sys.dont_write_bytecode = True
import hashlib, io, json, os, sys, importlib.util, argparse as _argparse
script, cases_path = sys.argv[1], sys.argv[2]
digest = hashlib.sha256(open(_argparse.__file__, 'rb').read()).hexdigest()
spec = importlib.util.spec_from_file_location('adopt_old', script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
def value(v):
    if isinstance(v, list):
        return {'list': len(v)}
    return {'text': v.encode('utf-8', 'surrogateescape').hex()}
class Parsed(BaseException):
    pass
_original = _argparse.ArgumentParser.parse_args
def capture(self, *a, **k):
    ns = _original(self, *a, **k)
    raise Parsed({
        'root': value(ns.root), 'candidate': value(ns.candidate),
        'name': None if ns.name is None else value(ns.name),
        'expect_sha256': value(ns.expect_sha256),
        'accept': None if ns.accept is None else value(ns.accept),
        'unattended': ns.unattended})
_argparse.ArgumentParser.parse_args = capture
cases = json.load(open(cases_path))
out = []
for case in cases:
    argv = [bytes.fromhex(h).decode('utf-8', 'surrogateescape') for h in case['args']]
    for key in ('COLUMNS', 'PYTHONINTMAXSTRDIGITS'):
        env_key = {'COLUMNS': 'columns', 'PYTHONINTMAXSTRDIGITS': 'max_digits'}[key]
        if case.get(env_key) is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = case[env_key]
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    sys.argv = ['adopt-candidate.py'] + argv
    try:
        parsed = None
        try:
            code = mod.main()
        except SystemExit as exit_:
            code = exit_.code
        except Parsed as caught:
            code, parsed = 0, caught.args[0]
        stdout, stderr = sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    result = {'args': case['args'], 'columns': case.get('columns'), 'max_digits': case.get('max_digits'),
              'code': code, 'stdout': stdout.encode('utf-8').decode('utf-8'),  # 厳密。UTF-8 にできない出力は失敗にする
              'stderr': stderr.encode('utf-8', 'backslashreplace').decode('utf-8')}
    if parsed is not None:
        result['parsed'] = parsed
    out.append(result)
print(json.dumps({'argparse_sha256': digest, 'cases': out}, ensure_ascii=False))
'''

TOKENS = [
    '--root', '--root=', '--root=r', '--root=--', '--expect-sha256', '--expect-sha256=s', '--name', '--name=n',
    '--name=', '--accept', '--accept=a', '--unattended', '--unattended=x', '-h', '--help', '--help=x', '-hh',
    '-hx', '-h=', '-h=v', '--ro', '--ex', '--na', '--ac', '--un', '--h', '--', '--=x', '--=', '-x', '-5',
    '-1.5', '-a b', 'x', 'cand', '', '-', '--unknown', '--unknown=1', '-1\n', '-١', 'a b',
    '-é', '-hé', '-hhé', '--accept=--', '--name=--', '--expect-sha256=--', '-h-', '候補_a.md', '\udcff', '-\udcff', '--root=\udcff', '---', '--r=1',
]
COLUMNS = [None, None, None, '10', '11', '20', '30', '40', '45', '60', '80', '100', '0', '-5', 'abc', ' 70 ', '+50',
           '1_0', '٣٠', '9' * 5000, '2']
HAND = [
    [], ['-h'], ['--help'], ['--root', 'r', '--expect-sha256', 's', 'c'],
    ['--root', 'r', '--expect-sha256', 's', '--name', 'n', '--accept', 'a', '--unattended', 'c'],
    ['--root=r', '--expect-sha256=s', 'c'], ['c', '--root', 'r', '--expect-sha256', 's'],
    ['--root', 'r', '--expect-sha256', 's', '--', 'c'], ['--root', 'r', '--expect-sha256', 's', 'c', '--'],
    ['--root', 'r', '--expect-sha256', 's', '--', '--'], ['--root', 'r', '--expect-sha256', 's', 'a', 'b'],
    ['--root', 'r', '--root', 'r2', '--expect-sha256', 's', 'c'],
]


def encode(token):
    return token.encode('utf-8', 'surrogateescape').hex()


def build_cases(count, seed):
    rng = random.Random(seed)
    cases = []
    for width in COLUMNS[:1] + ['10', '20', '30', '40', '45', '60', '80', '100', '7']:
        cases.append({'args': [encode('-h')], 'columns': width})
        cases.append({'args': [encode('--bad')], 'columns': width})
    for args in HAND:
        cases.append({'args': [encode(a) for a in args], 'columns': None})
    for _ in range(count):
        args = [rng.choice(TOKENS) for _ in range(rng.randint(0, 6))]
        cases.append({'args': [encode(a) for a in args], 'columns': rng.choice(COLUMNS)})
    unique, seen = [], set()
    for case in cases:
        key = (tuple(case['args']), case['columns'])
        if key not in seen:
            seen.add(key)
            unique.append(case)
    return unique


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', required=True)
    parser.add_argument('--script', required=True)
    parser.add_argument('--count', type=int, default=150)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--out')
    options = parser.parse_args()
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        cases_path = os.path.join(directory, 'cases.json')
        with open(cases_path, 'w') as handle:
            json.dump(build_cases(options.count, options.seed), handle)
        env = {'PATH': os.environ.get('PATH', ''), 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
               'LC_ALL': 'C.UTF-8'}
        result = subprocess.run([options.python, '-I', '-B', '-c', HARNESS, options.script, cases_path],
                                capture_output=True, env=env, check=False)
    if result.returncode:
        sys.stderr.buffer.write(result.stderr)
        return 1
    document = json.loads(result.stdout)
    if document['argparse_sha256'] != ARGPARSE_SHA256:
        print('旧 Python の argparse.py が本家 3.10.11 と違う: ' + document['argparse_sha256'], file=sys.stderr)
        return 1
    text = json.dumps(document, ensure_ascii=False, indent=0) + '\n'
    if options.out:
        with open(options.out, 'w') as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
