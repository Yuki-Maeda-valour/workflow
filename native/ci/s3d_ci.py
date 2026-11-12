#!/usr/bin/env python3
"""S3D専用のCI準備と比較。製品の配布物とGit設定は変更しない。"""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

# 全体の専用比較1回の時間。2026-11-12の実測は作業機974秒・基準の版の容器1085秒、
# ほかの担当の検査と並んだ容器で1415秒(いちばん遅い値を使う)。
MEASURED_SECONDS = 1415
# 1回の比較の上限。実測の2倍より長くし、遅い機械(Mac Intel)でも内側の上限で止まるようにする。
COMPARISON_LIMIT_SECONDS = 3600
# 比較はbuildとverify-universalの時間の枠に入らないので、別のジョブで流す(2026-11-12 12:59の決定)。
# ジョブごとの比較の回数: s3dは2回のビルドの各1回、s3d-universalは署名後の1回。
JOB_COMPARISONS = {'s3d': 2, 's3d-universal': 1}
# 比較のほかの準備(Rust・道具の導入・型検査の記録のためのビルド・転送物の展開)に見込む時間。
JOB_OVERHEAD_SECONDS = 1800
# 旧版を動かす道具。基準はCIのUbuntu 24.04の版(launch_sites.jsonのbase_versions)。
VERSION_TOOLS = ('bash', 'coreutils', 'findutils', 'sed', 'grep', 'glibc')
# Macで作れない欄(未実施として記録し、成功に数えない)の理由。
# Macで作れない欄: 基準の版に依る欄・straceが要る欄・/procが要る欄(別のプロセスの/proc/PID/mem)。
MAC_NOT_RUN = {'version', 'strace', 'proc'}


def execute(argv, *, env=None):
    subprocess.run(list(map(str, argv)), check=True, env=env, stdin=subprocess.DEVNULL)


def run_limited(argv, *, env, timeout):
    print('+ ' + ' '.join(map(str, argv)), file=sys.stderr, flush=True)
    subprocess.run(list(map(str, argv)), env=env, check=True, stdin=subprocess.DEVNULL, timeout=timeout)


def prepare_tools(name):
    """比較の道具を入れる。MacはGNUの道具とgit-lfs、Linuxはgit-lfsとstrace。"""
    before = shutil.which('git')
    if before is None:
        raise RuntimeError('Gitがありません')
    before = os.path.realpath(before)
    if name.startswith('macos-'):
        execute(['brew', 'install', 'bash', 'grep', 'coreutils', 'findutils'])
        if shutil.which('git-lfs') is None:
            execute(['brew', 'install', 'git-lfs'])
    elif any(shutil.which(tool) is None for tool in ('git-lfs', 'strace')):
        execute(['sudo', 'apt-get', 'update'])
        execute(['sudo', 'apt-get', 'install', '-y', 'git-lfs', 'strace'])
    if os.path.realpath(shutil.which('git') or '') != before:
        raise RuntimeError('必要道具の準備がGitの実体を変更しました')


def environment(work):
    env = dict(os.environ)
    before = shutil.which('git', path=env.get('PATH'))
    if before is None:
        raise RuntimeError('Gitがありません')
    before = os.path.realpath(before)
    if platform.system() == 'Darwin':
        folder = Path(work) / 's3d-gnu-tools'
        folder.mkdir(parents=True, exist_ok=True)
        for formula in ('grep', 'coreutils', 'findutils'):
            prefix = Path(subprocess.check_output(['brew', '--prefix', formula], text=True).strip())
            source = prefix / 'libexec/gnubin'
            if not source.is_dir():
                raise RuntimeError(f'GNU道具のフォルダがありません: {formula}')
            for candidate in source.iterdir():
                if candidate.name == 'git':
                    raise RuntimeError('GNU専用PATHへGitを混ぜることはできません')
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    target = folder / candidate.name
                    if not target.exists():
                        target.symlink_to(candidate.resolve())
        prefix = Path(subprocess.check_output(['brew', '--prefix', 'bash'], text=True).strip())
        target = folder / 'bash'
        if not target.exists():
            target.symlink_to((prefix / 'bin/bash').resolve())
        env['PATH'] = str(folder) + os.pathsep + env['PATH']
    if os.path.realpath(shutil.which('git', path=env.get('PATH')) or '') != before:
        raise RuntimeError('GNU道具のPATHがGitの実体を変更しました')
    env['DEV_WORKFLOW_S3D_PYTHON'] = sys.executable
    return env


def unit_test(name, work, env, *, run):
    """buildの品質検査で、exampleの単体検査をLinuxだけで流す(記録は作らない。道具は入れない)。

    Macのbuildはbrewの導入で時間の枠(2026-11-12 12:59の決定)を越えるので流さない。
    Macの単体検査は、s3dジョブの型検査の記録(同じexampleのcargo test)が兼ねる。
    """
    if not name.startswith('linux-'):
        raise ValueError(f'S3Dのexampleの単体検査はLinuxのbuildだけで流します: {name}')
    tools = environment(work)
    run(['cargo', 'test', '--locked', '-p', 'dev-workflow-cli', '--example', 'diff_snapshot_contract'],
        env=dict(env, PATH=tools['PATH'], DEV_WORKFLOW_S3D_PYTHON=tools['DEV_WORKFLOW_S3D_PYTHON']))


def typed_report(name, work, *, root, build_environment, run):
    report = Path(work) / 's3d-typed.json'
    report.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='s3d-typed-', dir=work) as temporary:
        env = build_environment(Path(temporary), name)
        tools = environment(work)
        env.update(PATH=tools['PATH'], DEV_WORKFLOW_S3D_PYTHON=tools['DEV_WORKFLOW_S3D_PYTHON'])
        env['CARGO_TARGET_DIR'] = temporary
        env['DEV_WORKFLOW_S3D_TYPED_REPORT'] = str(report)
        started = time.monotonic()
        run(['cargo', 'test', '--locked', '-p', 'dev-workflow-cli', '--example', 'diff_snapshot_contract', '--', '--nocapture'], env=env)
        elapsed = time.monotonic() - started
    if not report.is_file():
        raise RuntimeError('S3Dの型検査の記録がありません')
    print(json.dumps({'s3d_typed_seconds': elapsed, 'system': platform.system(), 'machine': platform.machine()}), flush=True)
    return report


def probe(binary, report, work, *, root, run=run_limited):
    env = environment(work)
    env['DEV_WORKFLOW_TEST_DIFF_SNAPSHOT_BINARY'] = str(binary)
    env['DEV_WORKFLOW_S3D_TYPED_REPORT'] = str(report)
    output = Path(work) / ('s3d-' + str(binary.parent.name) + '-comparison.json')
    started = time.monotonic()
    # The full legacy selftest and every fixed group are mandatory here.
    output.unlink(missing_ok=True)
    run([sys.executable, '-B', Path(root) / 'native/ci/check_diff_snapshot.py', '--report', output], env=env,
        timeout=COMPARISON_LIMIT_SECONDS)
    elapsed = time.monotonic() - started
    summary = check_report(json.loads(output.read_text()), platform.system())
    print(json.dumps(dict(summary, s3d_comparison_seconds=elapsed, limit_seconds=COMPARISON_LIMIT_SECONDS,
                          margin_seconds=COMPARISON_LIMIT_SECONDS - elapsed, system=platform.system(),
                          machine=platform.machine(), binary=str(binary)), ensure_ascii=False), flush=True)


def check_report(report, system):
    """道具の版と、版に依る欄の失敗・未実施が記録されていることを確かめる。

    Linuxは基準の版(Ubuntu 24.04のrunner)で流し、未実施を認めない。Macは基準の版を用意できないので、
    版に依る欄とstraceが要る欄を未実施として記録する(成功に数えない)。
    """
    versions = report.get('tool_versions')
    if not isinstance(versions, dict) or set(versions) != set(VERSION_TOOLS) or 'tool_versions_base' not in report:
        raise RuntimeError('S3Dの比較の記録に、旧版を動かす道具の版がありません')
    base = report['tool_versions_base']
    not_run = report.get('not_run')
    failures = report.get('version_failures')
    if not isinstance(not_run, list) or not isinstance(failures, list) or 'version_fields' not in report:
        raise RuntimeError('S3Dの比較の記録に、版に依る欄の失敗と未実施の欄がありません')
    if report.get('scope') != 'full-external':
        raise RuntimeError('S3Dの比較が全体の比較ではありません')
    if system == 'Linux':
        if base is not True:
            raise RuntimeError(f'Linuxの比較が基準の版で流れていません: {versions}')
        if not_run or failures:
            raise RuntimeError('Linuxの比較に未実施か版に依る欄の失敗があります')
    elif system == 'Darwin':
        reasons = {item.get('reason') for item in not_run if isinstance(item, dict)}
        if reasons - MAC_NOT_RUN or failures:
            raise RuntimeError('Macの未実施の理由が、基準の版とstraceのほかを含みます')
        recorded = {item['id'] for item in not_run if item.get('reason') == 'version'}
        if not base and not set(report.get('version_dependent', [])) <= recorded:
            raise RuntimeError('Macの版に依る欄が、未実施として記録されていません')
    else:
        raise RuntimeError(f'未知の機種です: {system}')
    counts = {}
    for item in not_run:
        counts[item['reason']] = counts.get(item['reason'], 0) + 1
    return {'tool_versions': versions, 'tool_versions_base': base,
            'version_fields': len(report.get('version_dependent', [])), 'not_run': counts}
