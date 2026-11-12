"""設計の固定入力の表のうち、342件より後の126件(部分出力は既存の群で持つ)。

各IDは、ここで動かすもの・既存の群のIDを共有するもの(shared)・型検査のIDを共有するもの(typed)の
どれかに固定する。動かしていないIDを成功に数えない。
"""
import dataclasses
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .compare import exact_ids, fixed, run, same_result, unchanged
from . import group_signals, launch_sites, prereview2, review_cases, startup_env, write_cuts

GIT_VARIABLES = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_VALUE',
                 'GIT_EXTERNAL_DIFF', 'GIT_PAGER', 'GIT_NO_LAZY_FETCH', 'GIT_CONFIG_PARAMETERS',
                 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM', 'GIT_ATTR_SOURCE',
                 'GIT_CEILING_DIRECTORIES', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                 'GIT_COMMON_DIR')
INJECTABLE = ('GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM',
              'GIT_ATTR_SOURCE')


def cases():
    result = []

    def add(case_id, group, kind, **values):
        result.append(dict(id=case_id, group=group, kind=kind, **values))
    for name in GIT_VARIABLES:
        for approved in (False, True):
            add(f'git-env-{name.lower().replace("_", "-")}-{"approved" if approved else "plain"}', 'git-env',
                'git-env', variable=name, approved=approved)
    for name in INJECTABLE:
        # 旧版の結果を固定する(5通りとも、注入したtextconvかfilterで終了22で止まる)。
        add(f'git-env-inject-{name.lower().replace("_", "-")}', 'git-env', 'git-env-inject', variable=name, expected=22)
    add('git-env-all-17', 'git-env', 'git-env-all')
    add('child-env-normal', 'child-env', 'shared', shared='review-env-probe-normal')
    add('child-env-profile', 'child-env', 'shared', shared='review-env-probe-profile')
    add('child-env-no-pwd-shlvl', 'child-env', 'shared', shared='review-env-probe-no-pwd-shlvl')
    add('child-env-oldpwd', 'child-env', 'env-probe', env={'OLDPWD': '<ROOT>'})
    add('child-env-shlvl-1000', 'child-env', 'env-probe', env={'SHLVL': '999'})
    add('child-env-non-identifier', 'child-env', 'env-probe', env={'A-B': 'x', 'A.B': 'y'})
    for name in ('normal', 'git-failure', 'grep-failure'):
        add('japanese-' + name, 'japanese', 'japanese', variant=name)
    add('profile-git-timeout-real', 'profile-git-timeout', 'profile-timeout-real')
    add('profile-git-timeout-injected', 'profile-git-timeout', 'typed', typed='profile-timeout-injected')
    for area in ('profile-union', 'git-run'):
        for signum in ('TERM', 'INT', 'HUP'):
            add(f'group-signal-{area}-{signum}', 'group-signal', 'group-signal', area=area, signal=signum)
    # 起動の形ごと(`$(…)`の中・出力を捨てる・パイプ・grep)の知らせの行の有無(下限の6件に足す)。
    for area in group_signals.SITES:
        for signum in ('TERM', 'INT', 'HUP'):
            add(f'group-signal-site-{area}-{signum}', 'group-signal-site', 'group-signal', area=area, signal=signum)
    for origin in ('current', 'ref'):
        # 「D5の差」の印(mark)と、旧版が止まる原因(cause)をmanifestに固定する。
        add(f'd5-python-missing-{origin}', 'd5', 'd5', condition='python-missing', origin=origin, mark='d5',
            cause='python-missing')
        add(f'd5-pyyaml-missing-{origin}', 'd5', 'd5', condition='pyyaml-missing', origin=origin, mark='d5',
            cause='pyyaml-missing')
    add('d5-helper-missing', 'd5', 'd5', condition='helper-missing', origin='ref', mark='d5', cause='helper-missing')
    add('d5-no-profile-control', 'd5', 'd5', condition='no-profile', origin='ref', mark=None, cause=None)
    for name, shared in (('mktemp-613', 'review-fail-tmpdir-missing-c'), ('mktemp-1186', 'review-fail-out-mktemp-c'),
                         ('mktemp-1190', 'review-fail-patch-mktemp'), ('mkdir-668', 'review-fail-mkdir-c')):
        add('tool-e-' + name, 'tool-failure', 'shared', shared=shared)
    add('tool-e-mktemp-589', 'tool-failure', 'e-mktemp-589')
    add('tool-e-mkdir-enametoolong', 'tool-failure', 'e-mkdir-long')
    add('tool-e-find-888', 'tool-failure', 'e-find-888')
    for typed in ('tool-n-cp-1187', 'tool-n-cp-1191', 'tool-n-mv-1192', 'tool-n-mv-1194', 'tool-n-rm-247',
                  'tool-n-redirect-1347', 'tool-n-redirect-1416', 'tool-n-redirect-1472'):
        add(typed.replace('tool-n-', 'tool-n8-'), 'tool-failure', 'typed', typed=typed)
    for name in ('git-output', 'git-stdin', 'index'):
        add('rust-file-write-' + name, 'rust-file-write', 'rust-file-write', target=name)
    for name, value in (('unset', None), ('30', '30'), ('10', '10'), ('0', '0'), ('minus-1', '-1'), ('abc', 'abc'),
                        ('4301-digits', '9' * 4301)):
        add('columns-' + name, 'columns', 'columns', value=value)
    for name, shared in (('vertical-tab', 'review-info-exclude-vertical-tab'), ('nul', 'review-info-exclude-nul'),
                         ('form-feed', 'review-info-exclude-form-feed-text')):
        add('grep-exclude-' + name, 'plain-grep', 'shared', shared=shared)
    add('grep-exclude-carriage-return', 'plain-grep', 'info-exclude', data_hex=b'\r\n'.hex())
    add('grep-exclude-space-only', 'plain-grep', 'info-exclude', data_hex=b' \t \n'.hex())
    add('grep-lfs-diagnostic-present', 'plain-grep', 'lfs-diagnostic', present=True)
    add('grep-lfs-diagnostic-absent', 'plain-grep', 'lfs-diagnostic', present=False)
    add('grep-binary-files-line', 'plain-grep', 'binary-files')
    for name, env in (('missing-lc-all', {'LC_ALL': 'xx_YY.UTF-8'}), ('installed-lc-all', {'LC_ALL': 'C.UTF-8'}),
                      ('codeset-spelling', {'LC_ALL': 'C.utf8'}), ('locpath', {'LC_ALL': 'zz_ZZ.UTF-8', 'LOCPATH': '<ROOT>/locales'}),
                      ('empty', {'LC_ALL': ''}), ('lang-only-control', {'LANG': 'xx_YY.UTF-8'})):
        add('setlocale-' + name, 'setlocale', 'setlocale', env=env)
    for name, value in (('out-of-range', '4294967296'), ('negative', '-1'), ('spaces', ' 7 '), ('random', 'random'),
                        ('valid', '7')):
        add('helper-hashseed-' + name, 'helper-startup', 'helper-env', env={'PYTHONHASHSEED': value})
    for value in ('1', '2'):
        add('helper-optimize-' + value, 'helper-startup', 'helper-env', env={'PYTHONOPTIMIZE': value})
    # 先行点検2の指摘(旧版との不一致)を並べて確かめる群と、読取りの失敗の地点の型検査(下限の外)。
    result.extend(prereview2.cases())
    for typed in launch_sites.READ_TYPED_IDS:
        add(typed.replace('tool-n-', 'pr2-typed-'), prereview2.GROUP, 'typed', typed=typed)
    # 09:58 決定 3 の後の指示: bashの起動時の警告と、実行中に値を作る変数が子へ渡る値(下限の外)。
    result.extend(startup_env.cases())
    assert len([case for case in result if case['group'] not in EXTRA_GROUPS]) == 109, len(result)
    assert len({case['id'] for case in result}) == len(result)
    return result


GROUP_LIMITS = {'git-env': 40, 'child-env': 6, 'japanese': 3, 'profile-git-timeout': 2, 'group-signal': 6, 'd5': 6,
                'tool-failure': 15, 'rust-file-write': 3, 'columns': 7, 'plain-grep': 8, 'setlocale': 6,
                'helper-startup': 7}
# 設計の表(下限)に足した群。表の件数(109 + 17 = 126)には数えない。
EXTRA_GROUPS = {'group-signal-site': 24, prereview2.GROUP: len(prereview2.cases()) + len(launch_sites.READ_TYPED_IDS),
                startup_env.GROUP: len(startup_env.cases())}


def check_counts():
    counts = {}
    for case in cases():
        counts[case['group']] = counts.get(case['group'], 0) + 1
    if counts != {**GROUP_LIMITS, **EXTRA_GROUPS}:
        raise AssertionError(f'固定入力の群の件数が設計と違います: {counts}')
    counts = {group: count for group, count in counts.items() if group not in EXTRA_GROUPS}
    # 部分出力17件は review_cases.partial_cases() が持つ(109 + 17 = 126)。
    return counts


D5_TOOLS = ('bash', 'git', 'grep', 'sed', 'sha256sum', 'cut', 'tr', 'realpath', 'readlink', 'mktemp', 'rm', 'dirname',
            'basename', 'mkdir', 'cat', 'find', 'sort', 'date', 'cp', 'mv', 'head', 'wc')
D5_CAUSES = {'python-missing': b'python3: command not found', 'pyyaml-missing': 'profile があるため PyYAML が必要'.encode(),
             'helper-missing': 'profile helper が無い'.encode()}


def d5_fill(folder, python, real_path, log=None):
    """同じPATHの文字列のまま、フォルダの中身だけを替える。python: 'yaml'・'plain'・None。

    python3は、起動を記録してから本物へ移る(起動の記録。Rustがpython3を起動したら見つける)。
    """
    for entry in list(folder.iterdir()):
        entry.unlink()
    for tool in D5_TOOLS:
        real = shutil.which(tool, path=real_path)
        if real is None:
            raise AssertionError(f'D5の専用PATHに置く道具がありません: {tool}')
        os.symlink(os.path.realpath(real), folder / tool)
    target = {'yaml': shutil.which('python3', path=real_path), 'plain': os.path.realpath(sys.executable)}.get(python)
    if python is not None:
        record = "printf 'python3\\n' >> '" + str(log) + "'\n" if log is not None else ''
        (folder / 'python3').write_text('#!/bin/sh\n' + record + "exec '" + target + "' \"$@\"\n")
        (folder / 'python3').chmod(0o700)
    seen = shutil.which('python3', path=str(folder))
    if (seen is None) != (python is None):
        raise AssertionError('D5の専用PATHのpython3の有無が条件と違います')
    if python == 'plain':
        probe = subprocess.run([str(folder / 'python3'), '-c', 'import yaml'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env={'PATH': str(folder)}, check=False)
        if probe.returncode == 0:
            raise AssertionError('PyYAMLなしの条件でPyYAMLが読めます')


def d5_case(case, f, binary, legacy):
    condition, origin = case['condition'], case['origin']
    if condition != 'no-profile':
        profile = f.repo / '.claude/project-profile.yml'
        profile.parent.mkdir()
        profile.write_bytes(b'secret_paths:\n  - secret.key\n')
        if origin == 'ref':
            f.git('add', '-A')
            f.git('commit', '-q', '-m', 'profile')
            f.base = f.git('rev-parse', 'HEAD').strip().decode()
            profile.unlink()
    (f.repo / 'seed.txt').write_bytes(b'd5 change\n')
    markers = ()
    if condition != 'no-profile':
        (f.repo / 'secret.key').write_bytes(b'S3D_SYNTHETIC_D5\n')
        markers = (b'S3D_SYNTHETIC_D5',)
    folder = f.root / 'd5-path'
    folder.mkdir()
    env = dict(f.env, PATH=str(folder))
    args = [*f.normal(), '--patch-out', str(f.patch), '--secret-profile-ref', f.base]
    real_path = f.env['PATH']

    log = f.root / 'd5-python3.log'

    def once(command, python):
        d5_fill(folder, python, real_path, log)
        log.write_bytes(b'')
        f.clean_outputs()
        before = f.input_state()
        result = run([*command, *args], cwd=f.root, env=env, report=f.out, patch=f.patch)
        unchanged(before, f.input_state())
        return result, log.read_bytes().count(b'python3\n')
    normal_old, legacy_launches = once(['bash', str(legacy)], 'yaml')
    fixed(normal_old, 0)
    rust = {python: once([str(binary)], python) for python in ('yaml', 'plain', None)}
    if condition == 'helper-missing':
        copy = f.root / 'legacy-without-helper'
        copy.mkdir()
        shutil.copy2(legacy, copy / legacy.name)
        stopped = once(['bash', str(copy / legacy.name)], 'yaml')[0]
    else:
        stopped = once(['bash', str(legacy)], {'python-missing': None, 'pyyaml-missing': 'plain',
                                               'no-profile': 'plain'}[condition])[0]
    observed = dict(normal=normal_old, legacy_launches=legacy_launches, rust=rust, stopped=stopped)
    d5_judge(case, observed, markers=markers)
    # 比較器の変異4つ(設計のD5)を、同じ観測で拒否できることを確かめる。
    mutant = f.root / 'rust-runs-python3'
    mutant.write_text("#!/bin/sh\npython3 -c '' || exit 99\nexec '" + str(binary) + "' \"$@\"\n")
    mutant.chmod(0o700)
    variants = {'rust-runs-python3': (case, dict(observed, rust=dict(rust, yaml=once([str(mutant)], 'yaml')))),
                'rust-normal-only': (case, dict(observed, rust={'yaml': rust['yaml']})),
                'mark-without-failure': (dict(case, mark='d5', cause=case['cause'] or 'python-missing'),
                                         dict(observed, stopped=normal_old))}
    if case['mark'] == 'd5':
        other = next(cause for cause in D5_CAUSES if cause != case['cause'])
        variants['mark-other-cause'] = (dict(case, cause=other), observed)
    rejected = []
    for name, (marked, values) in variants.items():
        try:
            d5_judge(marked, values, markers=markers)
        except AssertionError:
            rejected.append(name)
    if sorted(rejected) != sorted(variants):
        raise AssertionError(f'D5の比較器の変異を拒否できません: {sorted(set(variants) - set(rejected))}')
    return {'mark': case['mark'], 'rejected': rejected}


def d5_judge(case, observed, *, markers=()):
    """D5の観測を判定する。Rustは3通りすべてで旧版の正常な結果と同じで、python3を起動しない。
    印の付いたケースでは旧版が終了20で、manifestの原因で止まる。印の無い対照は旧版も止まらない。"""
    rust = observed['rust']
    if set(rust) != {'yaml', 'plain', None}:
        raise AssertionError('Rustを、python3あり・なし・PyYAMLなしの3通りで動かしていません')
    for python, (result, launches) in rust.items():
        same_result(observed['normal'], result, markers=markers)
        if launches:
            raise AssertionError(f'Rustがpython3を起動しました({python})')
    if case['condition'] != 'no-profile' and not observed['legacy_launches']:
        raise AssertionError('起動の記録が旧版のpython3の起動を見つけません')
    stopped = observed['stopped']
    if case['mark'] is None:
        same_result(observed['normal'], stopped)
        return
    # 「D5の差」の印: 旧版が実際に終了20で止まり、原因がmanifestの原因どおりであること。
    if case['mark'] != 'd5' or stopped.returncode != 20 or D5_CAUSES[case['cause']] not in stopped.stderr \
            or stopped.report is not None or any(D5_CAUSES[other] in stopped.stderr
                                                 for other in D5_CAUSES if other != case['cause']):
        raise AssertionError('D5の印を付けたケースで、旧版がmanifestの原因で止まりません')


def git_values(f, name):
    """継承するGIT_*の値。どれも同じリポジトリか、害の無い設定を指す。"""
    config = f.root / 'git-env.config'
    config.write_text('[core]\n\tabbrev = 12\n')
    (f.root / 'alternates').mkdir(exist_ok=True)
    git_dir = str(f.repo / '.git')
    return {'GIT_DIR': {'GIT_DIR': git_dir}, 'GIT_WORK_TREE': {'GIT_WORK_TREE': str(f.repo)},
            'GIT_INDEX_FILE': {'GIT_INDEX_FILE': git_dir + '/index'}, 'GIT_CONFIG_COUNT': {'GIT_CONFIG_COUNT': '0'},
            'GIT_CONFIG_KEY_VALUE': {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.abbrev', 'GIT_CONFIG_VALUE_0': '12'},
            'GIT_EXTERNAL_DIFF': {'GIT_EXTERNAL_DIFF': 'false'}, 'GIT_PAGER': {'GIT_PAGER': 'false'},
            'GIT_NO_LAZY_FETCH': {'GIT_NO_LAZY_FETCH': '0'},
            'GIT_CONFIG_PARAMETERS': {'GIT_CONFIG_PARAMETERS': "'core.abbrev'='12'"},
            'GIT_CONFIG_GLOBAL': {'GIT_CONFIG_GLOBAL': str(config)}, 'GIT_CONFIG_SYSTEM': {'GIT_CONFIG_SYSTEM': str(config)},
            'GIT_CONFIG_NOSYSTEM': {'GIT_CONFIG_NOSYSTEM': '1'}, 'GIT_ATTR_SOURCE': {'GIT_ATTR_SOURCE': f.base},
            'GIT_CEILING_DIRECTORIES': {'GIT_CEILING_DIRECTORIES': str(f.root)},
            'GIT_OBJECT_DIRECTORY': {'GIT_OBJECT_DIRECTORY': git_dir + '/objects'},
            'GIT_ALTERNATE_OBJECT_DIRECTORIES': {'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(f.root / 'alternates')},
            'GIT_COMMON_DIR': {'GIT_COMMON_DIR': git_dir}}[name]


def same_pair(f, binary, legacy, args, env):
    old = review_cases.execute(f, ['bash', str(legacy)], 'legacy', args, env_update=env)[0]
    new = review_cases.execute(f, [str(binary)], 'rust', args, env_update=env)[0]
    same_result(old, new)
    return old, new


def approval(f, legacy):
    f.git('config', 'filter.foo.clean', 'cat')
    first = run(['bash', str(legacy), '--cwd', str(f.repo), '--precheck'], cwd=f.root, env=f.env)
    fixed(first, 22)
    digest = [line for line in first.stderr.decode().splitlines() if line.startswith('承認ダイジェスト: ')]
    if len(digest) != 1:
        raise AssertionError('承認ダイジェストを1回だけ得られません')
    return ['--accept', digest[0].split(': ', 1)[1]]


def git_env_case(case, f, binary, legacy):
    kind = case['kind']
    (f.repo / 'seed.txt').write_bytes(b'git env change\n')
    if kind == 'git-env':
        env = git_values(f, case['variable'])
        accept = approval(f, legacy) if case['approved'] else []
        return same_pair(f, binary, legacy, review_cases.normal(f, '--patch-out', str(f.patch), *accept), env)
    if kind == 'git-env-inject':
        (f.repo / '.gitattributes').write_bytes(b'seed.txt diff=s3d filter=s3d\n')
        config = f.root / 'inject.config'
        config.write_text('[diff "s3d"]\n\ttextconv = printf CONVERTED:\n[filter "s3d"]\n\tclean = cat\n')
        env = {'GIT_CONFIG_COUNT': {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'diff.s3d.textconv',
                                    'GIT_CONFIG_VALUE_0': 'printf CONVERTED:'},
               'GIT_CONFIG_PARAMETERS': {'GIT_CONFIG_PARAMETERS': "'filter.s3d.clean'='cat'"},
               'GIT_CONFIG_GLOBAL': {'GIT_CONFIG_GLOBAL': str(config)},
               'GIT_CONFIG_SYSTEM': {'GIT_CONFIG_SYSTEM': str(config)}}.get(case['variable'])
        if case['variable'] == 'GIT_ATTR_SOURCE':
            (f.repo / '.gitattributes').unlink()
            f.git('config', 'filter.s3d.clean', 'cat')
            source = f.root / 'attr-source'
            f.git('worktree', 'add', '-q', '--detach', str(source), 'HEAD')
            (source / '.gitattributes').write_bytes(b'seed.txt filter=s3d\n')
            subprocess.run(['git', '-C', str(source), 'add', '.gitattributes'], env=f.env, check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=S3D', '-c', 'user.email=s3d@example.invalid',
                            'commit', '-q', '-m', 'attr'], env=f.env, check=True)
            oid = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], env=f.env, check=True,
                                 stdout=subprocess.PIPE).stdout.strip().decode()
            env = {'GIT_ATTR_SOURCE': oid}
        old, new = same_pair(f, binary, legacy, review_cases.normal(f, '--patch-out', str(f.patch)), env)
        fixed(old, case['expected'])
        return old, new
    if kind == 'git-env-all':
        env = {}
        for name in GIT_VARIABLES:
            if name not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY'):
                env.update(git_values(f, name))
        env.update(git_values(f, 'GIT_CONFIG_KEY_VALUE'))
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY'):
            env.update(git_values(f, name))
        return review_cases.env_probe(dict(case, env=env), f, binary, legacy)
    raise NotImplementedError(kind)


class NotAvailable(NotImplementedError):
    """前提(localeや翻訳ファイル)が無い。未実施として記録し、成功に数えない。"""


def japanese_ready():
    listed = subprocess.run(['locale', '-a'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
    if not any(name.lower() in (b'ja_jp.utf8', b'ja_jp.utf-8') for name in listed.split()):
        raise NotAvailable('ja_JP.UTF-8のlocaleがありません')
    for tool in ('git', 'grep'):
        if not any(Path(folder, 'ja/LC_MESSAGES', tool + '.mo').is_file()
                   for folder in ('/usr/share/locale', '/usr/share/locale-langpack')):
            raise NotAvailable(f'{tool}の日本語の翻訳ファイルがありません')


def japanese_case(case, f, binary, legacy):
    japanese_ready()
    env, remove = {'LANG': 'ja_JP.UTF-8', 'LC_MESSAGES': 'ja_JP.UTF-8'}, ('LC_ALL',)
    variant = case['variant']
    if variant == 'normal':
        (f.repo / 'seed.txt').write_bytes(b'japanese change\n')
        return review_cases.pair(f, binary, legacy, review_cases.normal(f, '--patch-out', str(f.patch)), code=0,
                                 env_update=env, env_remove=remove)
    if variant == 'git-failure':
        (f.repo / '.git/index').write_bytes(b'DIRC broken index')
        return review_cases.pair(f, binary, legacy, review_cases.normal(f), code=20, env_update=env, env_remove=remove)
    return review_cases.pair(f, binary, legacy, review_cases.normal(f, '--exclude', '['), code=2,
                             env_update=env, env_remove=remove)


# 最初に切れる書込みのファイル(起動場所の表の一時ディレクトリのファイル)。
FILE_WRITE = {'git-output': 'untracked.z', 'git-stdin': 'attrin.z', 'index': 'index.tmp.lock'}
TMPD_NAME = re.compile(rb'/tmp\.[A-Za-z0-9]{10}/')


def file_write_case(case, f, binary, legacy):
    """Rustもファイルへ書く所で、最初に切れる書込みが予定のファイルになる入力(RLIMIT_FSIZE)。

    旧版とRustの最初に切れた書込みをstraceで観測し、予定のファイルであることを確かめてから全部の行を比べる。
    """
    target = case['target']
    rules = []
    if target == 'index':
        for i in range(200):
            (f.repo / f'tracked-{i:04d}-with-a-long-long-long-name.txt').write_bytes(b't\n')
        f.git('add', '-A')
        f.git('commit', '-q', '-m', 'many')
        f.base = f.git('rev-parse', 'HEAD').strip().decode()
        # 使い捨てindexは、stage.z(1件51+名前のbyte)より1件12byte以上大きい。
        size = len(f.git('ls-files', '-s', '-z'))
        # check-attrの出力(1パスに3件)はindexより大きいので、空を返すスタブで先に切れないようにする。
        rules = [dict(tool='git', args=['check-attr', '--stdin'], code=0)]
        tail = review_cases.internal(128, 1245)
    else:
        for i in range(200):
            (f.repo / f'untracked-{i:04d}-with-a-long-long-long-name.txt').write_bytes(b'u\n')
        listed = len(f.git('ls-files', '-o', '--exclude-standard', '-z'))
        # git-output: untracked.zが切れる。git-stdin: untracked.zは収まり、tracked.zとつないだattrin.zが切れる。
        size = listed - 100 if target == 'git-output' else listed
        # git-stdinの文はcatの版に依る(基準のcoreutils 9.4は入力の名前を出す。8.32は`write error`)。
        tail = ('ERROR [internal] git の呼び出しに失敗した(終了コード 128)\n'.encode() if target == 'git-output'
                else review_cases.internal(1, 897))
    env = review_cases.stubs(f, 'shared', rules)
    scratch = f.root / 'tmp-shared'
    scratch.mkdir()
    env['TMPDIR'] = str(scratch)
    values = []
    for command in (['bash', str(legacy)], [str(binary)]):
        f.clean_outputs()
        before = f.input_state()
        result, cuts = write_cuts.run([*command, *review_cases.normal(f)], cwd=f.root, env=env, size=size)
        unchanged(before, f.input_state())
        if os.listdir(scratch) or f.out.exists() or f.patch.exists():
            raise AssertionError('一時領域を残したか、出力を作りました')
        if not cuts or Path(cuts[0]['path']).name != FILE_WRITE[target]:
            raise AssertionError(f'最初に切れた書込みが予定と違います: {cuts[:1]}')
        values.append(dataclasses.replace(result, stderr=TMPD_NAME.sub(b'/tmp.XXXXXXXXXX/', result.stderr)))
    old, new = values
    fixed(old, 20, stdout=b'')
    if not old.stderr.endswith(tail) or (target == 'index' and b'/index.tmp.lock' not in old.stderr) \
            or (target == 'git-stdin' and not old.stderr.startswith(b'cat: ')):
        raise AssertionError('旧版の診断が予定の地点のものではありません')
    same_result(old, new, generated_report=False)
    return old, new


def run_case(case, fixture, binary, legacy):
    f, kind = fixture, case['kind']
    if kind == 'rust-file-write':
        if not write_cuts.available():
            raise NotImplementedError('strace')
        return file_write_case(case, f, binary, legacy)
    normal = review_cases.normal
    if kind == 'e-mktemp-589':
        elsewhere = f.root / 'elsewhere'
        elsewhere.mkdir()
        f.git('config', 'core.worktree', str(elsewhere))
        missing = str(f.root / 'missing-tmp')
        expected = ("mktemp: failed to create directory via template '" + missing
                    + "/tmp.XXXXXXXXXX': No such file or directory\n").encode() \
            + review_cases.internal(1, 589) + review_cases.internal(20, 589)
        values = []
        for command in (['bash', str(legacy)], [str(binary)]):
            f.clean_outputs()
            before = f.input_state()
            values.append(run([*command, *normal(f)], cwd=f.root, env=dict(f.env, TMPDIR=missing),
                              report=f.out, patch=f.patch))
            unchanged(before, f.input_state())
        old, new = values
        fixed(old, 20, stdout=b'', stderr=expected)
        same_result(old, new)
        return old, new
    if kind == 'e-mkdir-long':
        parent = f.root / ('n' * 300)
        args = ['--cwd', str(f.repo), '--base', f.base, '--exclude-glob', '.env', '--out', str(parent / 'sub/out.md')]
        return review_cases.pair(f, binary, legacy, args, code=20, env_update={'TMPDIR': str(f.root)})
    if kind == 'e-find-888':
        oid = f.git('rev-parse', 'HEAD').strip().decode()
        f.git('update-index', '--add', '--cacheinfo', f'160000,{oid},module')
        module = f.repo / 'module'
        module.mkdir()
        (module / 'payload').write_bytes(b'fixture\n')
        module.chmod(0)
        try:
            return review_cases.pair(f, binary, legacy, ['--cwd', str(f.repo), '--precheck'], code=22)
        finally:
            module.chmod(0o700)
    if kind.startswith('git-env'):
        return git_env_case(case, f, binary, legacy)
    if kind == 'japanese':
        return japanese_case(case, f, binary, legacy)
    if kind == 'd5':
        return d5_case(case, f, binary, legacy)
    if kind == 'columns':
        env = {} if case['value'] is None else {'COLUMNS': case['value']}
        if case['value'] and len(case['value']) > 4300:
            env['PYTHONINTMAXSTRDIGITS'] = '0'
        return review_cases.pair(f, binary, legacy, normal(f, '--secret-profile-ref', '-x'), code=20,
                                 env_update=env, env_remove=('COLUMNS', 'PYTHONINTMAXSTRDIGITS'))
    if kind == 'helper-env':
        profile = f.repo / '.claude/project-profile.yml'
        profile.parent.mkdir()
        profile.write_bytes(b'secret_paths:\n  - secret.key\n  - "*.pem"\n')
        f.git('add', '-A')
        f.git('commit', '-q', '-m', 'profile')
        f.base = f.git('rev-parse', 'HEAD').strip().decode()
        (f.repo / 'seed.txt').write_bytes(b'helper environment change\n')
        old = review_cases.execute(f, ['bash', str(legacy)], 'legacy', normal(f, '--secret-profile-ref', f.base),
                                   env_update=case['env'])[0]
        new = review_cases.execute(f, [str(binary)], 'rust', normal(f, '--secret-profile-ref', f.base),
                                   env_update=case['env'])[0]
        same_result(old, new)
        return old, new
    if kind == 'info-exclude':
        path = f.repo / '.git/info/exclude'
        path.write_bytes(bytes.fromhex(case['data_hex']))
        (f.repo / 'seed.txt').write_bytes(b'info exclude change\n')
        return review_cases.pair(f, binary, legacy, normal(f), code=0)
    if kind == 'lfs-diagnostic':
        # 705行: 列挙の失敗の診断にgit-lfsかfilter-processがあるか(ERE照合でないgrep)。
        text = 'git-lfs filter-process: git-lfs: command not found\n' if case['present'] else 'stub: other failure\n'
        return review_cases.pair(f, binary, legacy, normal(f), code=20,
                                 rules=[dict(tool='git', args=['ls-files', '-z'], code=1, stderr=text)])
    if kind == 'binary-files':
        (f.repo / 'seed.txt').write_bytes(b'Binary files a and b differ\n\0binary\n')
        (f.repo / 'new.bin').write_bytes(b'\0\1\2Binary files x\n')
        return review_cases.pair(f, binary, legacy, normal(f, '--patch-out', str(f.patch)), code=0)
    if kind == 'setlocale':
        return setlocale_case(case, f, binary, legacy)
    if kind == 'group-signal':
        return group_signals.run_case(case, f, binary, legacy)
    if kind == 'profile-timeout-real':
        return profile_timeout_case(f, binary, legacy)
    if kind == 'pr2':
        return prereview2.run_case(case, f, binary, legacy)
    if kind == 'startup-env':
        return startup_env.run_case(case, f, binary, legacy)
    if kind == 'env-probe':
        env = {key: value.replace('<ROOT>', str(f.root)) for key, value in case['env'].items()}
        return review_cases.env_probe(dict(case, env=env), f, binary, legacy)
    raise NotImplementedError(kind)


# profile用のgitの1呼出しの上限(旧helperのsubprocess.runのtimeout=10)。最初の呼出しを30秒待たせる。
TYPED_IDS = ('profile-timeout-injected',)
PROFILE_TIMEOUT = 10
PROFILE_SLEEP = 30
TIMEOUT_OBSERVED = {}


def profile_timeout_case(f, binary, legacy):
    """実時間で10秒の上限が効くこと。旧版とRustの全部の行も比べ、旧版の結果を型検査の比較に残す。"""
    profile = f.repo / '.claude/project-profile.yml'
    profile.parent.mkdir()
    profile.write_bytes(b'secret_paths:\n  - secret.key\n')
    f.git('add', '-A')
    f.git('commit', '-q', '-m', 'profile')
    f.base = f.git('rev-parse', 'HEAD').strip().decode()
    (f.repo / 'seed.txt').write_bytes(b'profile timeout change\n')
    rules = [dict(tool='git', args=['--show-object-format'], code=0, sleep=PROFILE_SLEEP)]
    values = []
    for label, command in (('legacy', ['bash', str(legacy)]), ('rust', [str(binary)])):
        began = time.monotonic()
        result = review_cases.execute(f, command, label, review_cases.normal(f, '--secret-profile-ref', f.base),
                                      rules=rules, timeout=PROFILE_SLEEP + 30)[0]
        seconds = time.monotonic() - began
        # 上限の10秒で止まる(30秒の待機を待たない)。起動と後片付けに数秒を見込む。
        if not PROFILE_TIMEOUT <= seconds < PROFILE_SLEEP - 5:
            raise AssertionError(f'{label}: profile用gitの上限が実時間で効いていません({seconds:.1f}秒)')
        values.append(result)
    old, new = values
    fixed(old, 20, stdout=b'')
    same_result(old, new)
    TIMEOUT_OBSERVED['legacy'] = old
    return old, new


# 基準のbash 5.2.21で、setlocaleの警告が出るのは、入っていないlocaleをLC_ALLに指定したときだけ。
SETLOCALE_WARNS = {'setlocale-missing-lc-all': True, 'setlocale-installed-lc-all': False,
                   'setlocale-codeset-spelling': False, 'setlocale-locpath': False, 'setlocale-empty': False,
                   'setlocale-lang-only-control': False}
C_UTF8 = Path('/usr/lib/locale/C.utf8')


def setlocale_case(case, f, binary, legacy):
    """bashの起動時のsetlocaleの警告(glibcの規則をRustが読んで判定する)。

    LOCPATHのケースは、C.UTF-8の中身をLOCPATHの下の`zz_ZZ.utf8`へ写す。LOCPATHを読まないと警告が出る。
    """
    if sys.platform.startswith('linux') and not C_UTF8.is_dir():
        raise NotImplementedError('C.UTF-8のlocaleが無い')
    env = {key: value.replace('<ROOT>', str(f.root)) for key, value in case['env'].items()}
    if 'LOCPATH' in env:
        shutil.copytree(C_UTF8, Path(env['LOCPATH']) / 'zz_ZZ.utf8', symlinks=True)
    (f.repo / 'seed.txt').write_bytes(b'setlocale change\n')
    old, new = review_cases.pair(f, binary, legacy, review_cases.normal(f), code=0, env_update=env,
                                 env_remove=('LC_ALL', 'LOCPATH', 'LANG'))
    warned = old.stderr.startswith(b'bash: warning: setlocale: LC_ALL: cannot change locale (')
    if warned != SETLOCALE_WARNS[case['id']]:
        raise AssertionError('旧版のsetlocaleの警告の有無が、基準の観測と違います')
    return old, new


# 版に依る欄(SHLVLとbashの起動時の警告・bash自身の文・Rustが作る道具の文)を比べるケースと、その欄の種類。
VERSION_DEPENDENT = {'child-env-': 'SHLVL', 'setlocale-': 'bash-startup-warning', 'tool-e-': 'tool-message',
                     'japanese-': 'bash-message', 'rust-file-write-git-stdin': 'tool-message',
                     # bash自身の文(前面の子が止まった`Terminated`・`Hangup`の行)。
                     'group-signal-profile-union-TERM': 'bash-message', 'group-signal-profile-union-HUP': 'bash-message',
                     'group-signal-git-run-TERM': 'bash-message', 'group-signal-git-run-HUP': 'bash-message'}
# 先行点検2の群: SHLVL・bash自身の文(所在地の欄)・Rustが作る道具の文(mktemp)と、基準のglibcのgrepの読取り。
VERSION_DEPENDENT.update({'pr2-shlvl-': 'SHLVL', 'pr2-proc-': 'bash-message', 'pr2-relative-tmpdir-in-sub': 'tool-message',
                          'pr2-relative-tmpdir-outside-missing': 'tool-message',
                          'pr2-tmpdir-trailing-slashes-missing': 'tool-message',
                          'pr2-grep-short-second-read': 'tool-message'})
for _area in group_signals.SITES:
    if group_signals.SITES[_area][3]:
        for _signal in ('TERM', 'HUP'):
            VERSION_DEPENDENT[f'group-signal-site-{_area}-{_signal}'] = 'bash-message'

def typed_timeout(observations):
    """型で同じ時刻(最初のprofile用gitで10秒)を与えたRustの観測を、実時間で上限に達した旧版と比べる。"""
    found = [value for value in observations if value.get('case_id') == 'profile-timeout-injected']
    old = TIMEOUT_OBSERVED.get('legacy')
    if len(found) != 1 or old is None:
        return False
    value = found[0]
    if (value['code'], bytes.fromhex(value['stdout_hex']), bytes.fromhex(value['stderr_hex']), value['published']) \
            != (old.returncode, old.stdout, old.stderr, old.report is not None):
        raise AssertionError('profile-git-timeout-injected: 注入した時刻のRustが、上限に達した旧版と違います')
    return True


def run_cases(binary, *, fixture_type, legacy, finished_ids, typed_ids, typed_observations=()):
    """動かしたID・共有して到達を確かめたID・未実施のID・版の違いで失敗にしたIDを分けて返す。"""
    from . import launch_sites
    check_counts()
    base, _ = launch_sites.is_base(launch_sites.load())
    done, pending, version_failed = [], [], []
    with tempfile.TemporaryDirectory(prefix='s3d-fixed-extra-') as temporary:
        for case in cases():
            if case['kind'] == 'shared':
                (done if case['shared'] in finished_ids else pending).append(case['id'])
                continue
            if case['kind'] == 'typed':
                ready = case['typed'] in typed_ids
                if ready and case['typed'] in TYPED_IDS:
                    ready = typed_timeout(typed_observations)
                (done if ready else pending).append(case['id'])
                continue
            root = Path(temporary) / case['id']
            root.mkdir()
            began = time.monotonic()
            dependent = next((kind for prefix, kind in VERSION_DEPENDENT.items() if case['id'].startswith(prefix)), None)
            if dependent:
                from . import compare
                compare.note_field(case['id'], dependent)
            try:
                run_case(case, fixture_type(root), binary, legacy)
            except NotImplementedError:
                pending.append(case['id'])
                continue
            except Exception as error:
                if dependent and not base:
                    version_failed.append(case['id'])
                    continue
                raise AssertionError(f'{case["id"]}: {error}') from error
            if dependent and not base:
                # 基準と違う機械では、版に依る欄の一致を成功に数えない。
                version_failed.append(case['id'])
                continue
            done.append(case['id'])
            del began
    return {'done': done, 'pending': pending, 'version_failed': version_failed}
