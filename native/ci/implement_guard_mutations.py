#!/usr/bin/env python3
"""S3I-A: Rust のソースへ変異を 1 つずつ当てたコピーをビルドし、比較器と旧 selftest の Rust 流しが落ちることを
確かめる(selftest の群 L の代わり)。CI では流さない。実装担当が流し、結果を Issue に記録する。

変異は native/ci/implement_guard_fixtures/mutations/<記号>-<名前>.patch(リポジトリ直下からの git apply)。
  a: ダイジェストの照合を外す / f: チェックの正規化で任意の 1 文字を受ける / m: `..` の正規化を外す /
  e: R4R の段の検査を外す(旧版の変異 e と同じく、親のリンクを辿らない開き方〈O_NOFOLLOW〉と、段ごとの確かめ
     〈check_chains〉の両方を外す)
各変異で「当てたことを確かめる(patch が当たる・ビルドが通る)」「狙った群の比較が不一致を出す」「Rust 流しの
selftest の狙ったケースが FAIL を出す」の 3 つを見る。どれかが落ちなければ、その変異は生き残った(不合格)。

使い方(リポジトリ直下。基準の容器・Python 3.10 系・root 以外・cargo 1.99.1):
  python3 -B native/ci/implement_guard_mutations.py [--only <記号>]
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
MUTATIONS = Path(__file__).with_name('implement_guard_fixtures') / 'mutations'
# 記号 → 比較器の群(selftest は Rust 流しの全部の範囲を流し、許す失敗の一覧の外の FAIL を数える)
TARGETS = {
    'a': '保護領域の確かめ',
    'f': 'taskmd-diff',
    'm': '保護領域の確かめ',
    'e': 'restore-taskmd',
}
EXPECTED = 4


def tracked_files():
    done = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True)
    return [Path(os.fsdecode(p)) for p in done.stdout.split(b'\0') if p]


def make_copy(base):
    """追跡しているファイルを写し、コピーの中だけで git に足す(build.rs が追跡の一覧を読む)。"""
    copy = base / 'copy'
    for rel in tracked_files() + untracked_needed():
        source = ROOT / rel
        if not source.exists() and not source.is_symlink():
            continue
        target = copy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), target)
        else:
            shutil.copy2(source, target)
    lib = copy / 'native/crates/platform/src/lib.rs'
    text = lib.read_text()
    if 'pub mod implement_guard;' not in text:
        lib.write_text(text + '\n/// S3I-A専用(変異のコピーの中だけ)。\npub mod implement_guard;\n')
    for args in (['init', '-q'], ['add', '-A']):
        subprocess.run(['git', '-C', str(copy), *args], check=True, capture_output=True,
                       env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null'})
    return copy


def untracked_needed():
    """まだ追跡していない S3I-A の専用ファイル(共有ファイルの段の前は、作業ツリーにだけある)。"""
    done = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z', '--others', '--exclude-standard', '--', 'native'],
                          capture_output=True, check=True)
    return [Path(os.fsdecode(p)) for p in done.stdout.split(b'\0') if p]


def build(copy, target_dir):
    env = {**os.environ, 'CARGO_TARGET_DIR': str(target_dir)}
    done = subprocess.run(['cargo', 'build', '--locked', '-p', 'dev-workflow-cli', '--example', 'implement_guard_contract'],
                          cwd=copy / 'native', env=env, capture_output=True)
    if done.returncode:
        raise AssertionError('変異のビルドが通らない: ' + done.stderr.decode(errors='replace')[-2000:])
    return Path(target_dir) / 'debug/examples/implement_guard_contract'


def comparator_fails(copy, fixture, group):
    done = subprocess.run([sys.executable, '-B', str(copy / 'native/ci/check_implement_guard.py'), '--fixture', str(fixture),
                           '--groups', group], capture_output=True, timeout=3600)
    text = done.stdout.decode(errors='replace') + done.stderr.decode(errors='replace')
    match = re.search(r'群 ' + re.escape(group) + r': 比べた \d+ 件・境界 \d+ 件・不一致 (\d+) 件', text)
    if not match:
        raise AssertionError(f'比較器の結果の行が無い: {text[-1500:]}')
    return int(match.group(1))


def allowed_lines(copy):
    data = json.loads((copy / 'native/ci/implement_guard_fixtures/selftest_allowed.json').read_text(encoding='utf-8'))
    return {(e['case'], e['line']) for e in data}


def selftest_ids(copy):
    text = (copy / 'native/ci/check_implement_guard.py').read_text(encoding='utf-8')
    match = re.search(r"SELFTEST_IDS = \(([^)]*)\)", text)
    return [item.strip().strip("'") for item in match.group(1).split(',') if item.strip()]


def selftest_fails(copy, fixture, base):
    log = base / 'wrapper.log'
    wrapper = base / 'wrapper.sh'
    old = copy / 'plugins/dev-workflow/skills/do-task/scripts/implement-guard.sh'
    wrapper.write_text('#!/usr/bin/env bash\ncase "${1-}" in\n  take|config-check|compare) exec bash \'%s\' "$@" ;;\n'
                       '  *) printf \'%%s\\n\' "${1-<none>}" >> \'%s\'; exec \'%s\' "$@" ;;\nesac\n' % (old, log, fixture))
    wrapper.chmod(0o755)
    done = subprocess.run(['bash', str(copy / 'plugins/dev-workflow/skills/do-task/scripts/implement-guard-selftest.sh'),
                           '--only', ','.join(selftest_ids(copy))], env={**os.environ, 'IMPLEMENT_GUARD': str(wrapper)},
                          capture_output=True, timeout=3600, stdin=subprocess.DEVNULL)
    allowed = allowed_lines(copy)
    failures = 0
    for line in done.stdout.decode(errors='replace').splitlines():
        match = re.match(r'^FAIL  (\S+) (.*)$', line)
        if match and (match.group(1), match.group(2)) not in allowed:
            failures += 1
    return failures, log.exists()


def run_one(patch, base):
    symbol = patch.name.split('-', 1)[0]
    group = TARGETS[symbol]
    copy = make_copy(base)
    applied = subprocess.run(['git', '-C', str(copy), 'apply', '--unidiff-zero', str(patch)], capture_output=True)
    if applied.returncode:
        return f'{symbol}: 当たらない(patch の誤り): ' + applied.stderr.decode(errors='replace')
    fixture = build(copy, base / 'target')
    mismatches = comparator_fails(copy, fixture, group)
    failures, used = selftest_fails(copy, fixture, base)
    print(f'S3I-A 変異 {symbol}: 比較器の不一致 {mismatches} 件({group})・Rust 流しの selftest の一覧の外の FAIL '
          f'{failures} 行(Rust の起動 {"あり" if used else "なし"})', flush=True)
    if mismatches == 0:
        return f'{symbol}: 比較器が落ちない(生き残った)'
    if failures == 0 or not used:
        return f'{symbol}: Rust 流しの selftest が落ちない(生き残った)'
    return None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--only')
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise SystemExit('Python 3.10 系・root 以外で流してください')
    patches = sorted(MUTATIONS.glob('*.patch'))
    if len(patches) != EXPECTED:
        raise SystemExit(f'変異の数が {EXPECTED} でない: {len(patches)}')
    problems = []
    for patch in patches:
        if args.only and not patch.name.startswith(args.only + '-'):
            continue
        # 比較器と selftest が作る保護領域は /tmp・$TMPDIR の外でなければ作れないので、HOME の下に作る。
        base = Path(tempfile.mkdtemp(prefix='s3ia-mutation-', dir=str(Path.home())))
        try:
            problem = run_one(patch, base)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        if problem:
            problems.append(problem)
    for problem in problems:
        print(f'S3I-A 変異: 不合格: {problem}', file=sys.stderr)
    if problems:
        return 1
    print('S3I-A 変異: 合格(全部の変異で比較器と Rust 流しが落ちた)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
