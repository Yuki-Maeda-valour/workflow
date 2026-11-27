#!/usr/bin/env python3
"""R4X-1: Rust のソースへ変異(わざと入れる不具合)を 1 つずつ当てたコピーをビルドし、比較器がその群で
不合格になることを確かめる。CI では流さない。実装担当が基準の容器で流し、結果を Issue に記録する。

変異は native/ci/implement_agent_fixtures/mutations/<記号>-<名前>.json(リポジトリ直下からの相対パスの
ファイルの字面 `old` を `new` に置き換える。`old` はちょうど 1 か所にあること)。比較器の群ごとに 1 つ以上ある。
各変異で「当てたことを確かめる(字面が 1 か所にあり、ビルドが通る)」「狙った群の比較が不一致を出す」を見る。
落ちなければ、その変異は生き残った(不合格)。この区間で旧版との比較では見えない部分(論理の cd の値など)は
変異に入れない(単体テストで確かめる)。

使い方(リポジトリ直下。基準の容器・Python 3.10 系・root 以外・cargo 1.99.1):
  python3 -B native/ci/implement_agent_mutations.py --fixture <今回ビルドした implement_agent_contract の絶対パス>
  [--only <記号>]
--fixture の実行物で、狙った群が全部合格することを先に確かめる(変異の前の比較器と Rust が正しいこと)。
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
MUTATIONS = Path(__file__).with_name('implement_agent_fixtures') / 'mutations'
CHECKER = ROOT / 'native/ci/check_implement_agent.py'
EXPECTED = 13
GROUPS = ('引数', 'runner と model', 'プロンプトのファイル', '--cwd', '時間', '--log-file の正規化', '候補の解決の始め',
          '中止の信号', '書き込みの故障', 'bash の起動', '地域設定', '想定外の失敗')
PREFIX = 'R4X-1 変異'


def load():
    items = []
    for path in sorted(MUTATIONS.glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        if path.stem != data['name']:
            raise SystemExit(f'{path.name}: 名前がファイル名と違う')
        if data['group'] not in GROUPS:
            raise SystemExit(f'{path.name}: 群が比較器の群でない: {data["group"]}')
        items.append(data)
    if len(items) != EXPECTED:
        raise SystemExit(f'変異の数が {EXPECTED} でない: {len(items)}')
    missing = set(GROUPS) - {item['group'] for item in items}
    if missing:
        raise SystemExit(f'変異の無い群がある: {sorted(missing)}')
    return items


def comparator(fixture, group):
    """比較器をその群だけで流し、(比べた件数, 不一致の件数, 出力の末尾)。"""
    done = subprocess.run([sys.executable, '-B', str(CHECKER), '--fixture', str(fixture), f'--groups={group}'],
                          capture_output=True, timeout=3600)
    text = done.stdout.decode(errors='replace') + done.stderr.decode(errors='replace')
    match = re.search(r'部分実行\(合格に数えない\): (\d+) 件・不一致 (\d+) 件', text)
    if not match:
        raise AssertionError(f'比較器の結果の行が無い: {text[-1500:]}')
    return int(match.group(1)), int(match.group(2)), text[-1200:]


def copy_native(base):
    """native と、単体テストが読む旧版だけを写し、コピーの中だけで git に足す(core の build.rs が、
    リポジトリの追跡の一覧を読むため)。ビルドは同じパスのコピーで続けて行う(差分だけを組み直す)。"""
    copy = base / 'copy'
    shutil.copytree(ROOT / 'native', copy / 'native', ignore=shutil.ignore_patterns('target', '__pycache__'))
    old = 'plugins/dev-workflow/skills/do-task/scripts/implement-agent.sh'
    (copy / old).parent.mkdir(parents=True)
    shutil.copy2(ROOT / old, copy / old)
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null'}
    for args in (['init', '-q'], ['add', '-A']):
        subprocess.run(['git', '-C', str(copy), *args], check=True, capture_output=True, env=env)
    return copy


def build(copy, target):
    env = {**os.environ, 'CARGO_TARGET_DIR': str(target)}
    done = subprocess.run(['cargo', '+1.99.1', 'build', '--locked', '--offline', '-p', 'dev-workflow-cli',
                           '--example', 'implement_agent_contract'], cwd=copy / 'native', env=env, capture_output=True)
    if done.returncode:
        raise AssertionError('変異のビルドが通らない: ' + done.stderr.decode(errors='replace')[-2000:])
    return target / 'debug/examples/implement_agent_contract'


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--only')
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise SystemExit('Python 3.10 系・root 以外で流してください')
    fixture = Path(args.fixture)
    if not fixture.is_absolute() or not fixture.is_file() or not os.access(fixture, os.X_OK):
        raise SystemExit('--fixture は今回ビルドした implement_agent_contract の絶対パスで指定してください')
    items = [item for item in load() if not args.only or item['name'].startswith(args.only + '-')]
    problems = []
    for group in sorted({item['group'] for item in items}):
        count, mismatches, tail = comparator(fixture, group)
        print(f'{PREFIX}: 変異の前の {group}: {count} 件・不一致 {mismatches} 件', flush=True)
        if count == 0 or mismatches:
            problems.append(f'変異の前の比較器が {group} で合格しない(件数 {count}・不一致 {mismatches}): {tail}')
    if problems:
        for problem in problems:
            print(f'{PREFIX}: 不合格: {problem}', file=sys.stderr)
        return 1
    base = Path(tempfile.mkdtemp(prefix='r4x1-mutation-', dir=os.environ.get('R4X1_WORK')))
    try:
        copy = copy_native(base)
        target = base / 'target'
        for item in items:
            path = copy / item['file']
            original = path.read_text(encoding='utf-8')
            if original.count(item['old']) != 1:
                problems.append(f'{item["name"]}: 字面がちょうど 1 か所に無い(変異を直す)')
                continue
            path.write_text(original.replace(item['old'], item['new']), encoding='utf-8')
            try:
                mutant = build(copy, target)
                count, mismatches, _ = comparator(mutant, item['group'])
            finally:
                path.write_text(original, encoding='utf-8')
            print(f'{PREFIX} {item["name"]}: {item["group"]} の {count} 件のうち不一致 {mismatches} 件({item["about"]})',
                  flush=True)
            if mismatches == 0:
                problems.append(f'{item["name"]}: 比較器が落ちない(生き残った)')
    finally:
        shutil.rmtree(base, ignore_errors=True)
    for problem in problems:
        print(f'{PREFIX}: 不合格: {problem}', file=sys.stderr)
    if problems:
        print(f'{PREFIX}: 不合格: {len(problems)} 件')
        return 1
    print(f'{PREFIX}: 合格: {len(items)} 件の変異の全部で比較器が不合格になった')
    return 0


if __name__ == '__main__':
    sys.exit(main())
