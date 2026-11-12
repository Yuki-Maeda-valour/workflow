"""先行点検2で見つかった旧版との不一致(論理PWD・TOPへのcd・findのlstat・読取りの失敗・SHLVLの桁あふれ・
grepのNULの扱い・TMPDIRの末尾の/・python3が無いときの`_`)を、旧版とRustを並べて確かめる追加の群。

各ケースの起動の場所(cwd)は、名前がケースIDのフォルダにする(版に依る欄の記録はcwdの名前で数える)。
"""
import dataclasses
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from .compare import fixed, run, same_result, unchanged
from .diagnostics import bash_locations
from . import review_cases

GROUP = 'prereview-2'
# /proc/PID/mem(別のプロセスのもの)は、`[ -r ]`を通り、openだけが拒まれる。Linuxだけで作れる。
PROC_KINDS = ('proc-pre-list', 'proc-tracked')


def cases():
    """(ID, 種類, 値)。"""
    items = []

    def add(name, kind, **values):
        items.append(dict(id='pr2-' + name, group=GROUP, kind='pr2', variant=kind, **values))
    # 1: 呼出元の場所はbashの論理PWD(601行のCALLER_PWD="$PWD")。
    add('logical-pwd-symlink-out', 'logical-pwd', pwd='lnk', out='o.md', code=2)
    add('logical-pwd-dot-dot', 'logical-pwd', pwd='lnk/../lnk', out='o.md', code=2)
    add('logical-pwd-dir-out', 'logical-pwd-dir', code=2)
    add('logical-pwd-other-dir', 'logical-pwd-other', code=0)
    # 2: 610行のcd -P -- "$TOP"の後のmktemp(相対のTMPDIRはTOPから解く)。
    add('relative-tmpdir-in-sub', 'relative-tmpdir', where='sub', inside_top=False, code=20)
    add('relative-tmpdir-outside-missing', 'relative-tmpdir', where='outside', inside_top=False, code=20)
    add('relative-tmpdir-in-top', 'relative-tmpdir', where='outside', inside_top=True, code=2)
    # 3: findは、ディレクトリと`.git`の名前のエントリをlstatする(読めるが入れないディレクトリ)。
    add('find-unsearchable-dir', 'find', entries=['inner/', 'file'], code=0)
    add('find-unsearchable-git-dir', 'find', entries=['.git/', 'file'], code=0)
    add('find-unsearchable-git-file', 'find', entries=['.git', 'inner/'], code=0)
    # 4: 読取りの失敗(開けない)。旧版の地点の診断と止まり方。
    add('proc-pre-list', 'proc-pre-list', code=20)
    add('proc-tracked', 'proc-tracked', code=20)
    # 6: SHLVLはbashのintで桁あふれする。
    add('shlvl-int-max', 'shlvl', value='2147483647')
    add('shlvl-wrap-to-one', 'shlvl', value='4294967296')
    # 7: 1652行のgrep(GNU grep 3.11)はNULを含むバッファから後を出さない。
    add('grep-nul-same-buffer', 'grep', body='nul-same', code=0)
    add('grep-nul-later-buffer', 'grep', body='nul-later', code=22)
    add('grep-short-second-read', 'grep', body='short-second', code=22)
    add('grep-untracked-after-binary-body', 'grep', body='untracked', code=22)
    # 10: TMPDIRの末尾の/(mktempは1本にする)とTOPが/の判定、python3が無いときの`_`。
    add('tmpdir-trailing-slashes', 'tmpdir-slashes', missing=False, code=0)
    add('tmpdir-trailing-slashes-missing', 'tmpdir-slashes', missing=True, code=20)
    add('profile-git-without-python', 'profile-underscore')
    # 8: profileの親(.claude)がsymlinkのとき、OSの失敗の文(固定表)で止まる。muslとglibcで文が違うerrno
    # (EIOなど)は実物で作れないので、型検査(muslの対象でも流す)で確かめる。
    add('profile-parent-symlink', 'profile-loop', code=20)
    return items


def _pair(f, binary, legacy, args, *, cwd, code, env_update=None, report=None, after=None, rows=None, state=None):
    """cwdと環境を指定して旧版とRustを動かし、終了値・出力・公開物を比べる。
    rowsは、旧版のbashの所在地の欄(呼出名: line N: …)の位置と行と内容の固定値。"""
    report = Path(report) if report is not None else f.out
    values = []
    for command in (['bash', str(legacy)], [str(binary)]):
        env = dict(f.env)
        env.update(env_update or {})
        f.clean_outputs()
        if report.is_file():
            report.unlink()
        before = (state or f.input_state)()
        result = run([*command, *args], cwd=cwd, env=env, report=report, patch=f.patch)
        unchanged(before, (state or f.input_state)())
        if after:
            after()
        values.append(result)
    old, new = values
    fixed(old, code)
    if rows is not None:
        old = dataclasses.replace(old, stderr=bash_locations(old.stderr, invocation=str(legacy), rows=rows))
    same_result(old, new)
    return old, new


def _args(f, *extra, out=None):
    return ['--cwd', str(f.repo), '--base', f.base, '--exclude-glob', '.env', '--out', str(out or f.out), *extra]


def _logical(case, f, binary, legacy):
    sub = f.repo / case['id']
    sub.mkdir()
    (f.repo / 'lnk').symlink_to(case['id'])
    (sub / 'keep.txt').write_bytes(b'keep\n')
    f.git('add', '-A')
    f.git('commit', '-q', '-m', 'logical')
    f.base = f.git('rev-parse', 'HEAD').strip().decode()
    pwd = str(f.repo / case['pwd'])
    old, _ = _pair(f, binary, legacy, ['--cwd', '.', '--base', f.base, '--exclude-glob', '.env', '--out', case['out']],
                   cwd=sub, code=case['code'], env_update={'PWD': pwd})
    if ('symlink がある: ' + str(f.repo / 'lnk')).encode() not in old.stderr:
        raise AssertionError('旧版が論理PWDの経路のsymlinkで止まっていません')


def _logical_dir(case, f, binary, legacy):
    (f.root / 'link').symlink_to('.')
    (f.repo / 'd').mkdir()
    (f.repo / 'd/keep.txt').write_bytes(b'keep\n')
    f.git('add', '-A')
    f.git('commit', '-q', '-m', 'directory')
    f.base = f.git('rev-parse', 'HEAD').strip().decode()
    work = f.repo / case['id']
    work.mkdir()
    (work / 'keep.txt').write_bytes(b'x\n')
    old, _ = _pair(f, binary, legacy, ['--cwd', '..', '--base', f.base, '--exclude-glob', '.env', '--out', '../d'],
                   cwd=work, code=2, env_update={'PWD': str(f.root / 'link/repo' / case['id'])})
    if ('通常ファイルでない: ' + str(f.root / 'link/repo' / case['id']) + '/../d').encode() not in old.stderr:
        raise AssertionError('旧版が論理PWDから--outを求めていません')


def _logical_other(case, f, binary, legacy):
    # PWDが別のディレクトリを指すとき、bashはgetcwd(物理)を使う。
    elsewhere = f.root / 'elsewhere'
    elsewhere.mkdir()
    (f.repo / 'seed.txt').write_bytes(b'logical other change\n')
    report = f.root / 'o.md'
    _pair(f, binary, legacy, _args(f, out='o.md'), cwd=f.root, code=0, env_update={'PWD': str(elsewhere)},
          report=report)
    if not report.is_file() or (elsewhere / 'o.md').exists():
        raise AssertionError('PWDが別の場所のとき、物理の場所へ書いていません')


def _relative_tmpdir(case, f, binary, legacy):
    (f.repo / 'seed.txt').write_bytes(b'relative tmpdir change\n')
    if case['where'] == 'sub':
        cwd = f.repo / case['id']
        (cwd / 'tmprel').mkdir(parents=True)
        (cwd / 'tmprel/keep.txt').write_bytes(b'keep\n')
        args = ['--cwd', '.', '--base', f.base, '--exclude-glob', '.env', '--out', str(f.out)]
    else:
        cwd = f.root
        (cwd / 'tmprel').mkdir()
        args = _args(f)
    if case['inside_top']:
        (f.repo / 'tmprel').mkdir()

    def left():
        # 旧版のEXITのtrapは、TOPから解いた相対の一時ディレクトリを消す。
        inside = f.repo / 'tmprel'
        if inside.is_dir() and os.listdir(inside):
            raise AssertionError('リポジトリ内の一時ディレクトリが残りました')
    old, _ = _pair(f, binary, legacy, args, cwd=cwd, code=case['code'], env_update={'TMPDIR': 'tmprel'}, after=left)
    if case['code'] == 20 and b"via template 'tmprel/tmp.XXXXXXXXXX': No such file or directory" not in old.stderr:
        raise AssertionError('旧版が613行のmktempで止まっていません')


def _tmpdir_slashes(case, f, binary, legacy):
    (f.repo / 'seed.txt').write_bytes(b'tmpdir slashes change\n')
    folder = f.root / ('missing' if case['missing'] else 'tmp-slashes')
    if not case['missing']:
        folder.mkdir()
    old, _ = _pair(f, binary, legacy, _args(f), cwd=f.root, code=case['code'], env_update={'TMPDIR': str(folder) + '//'})
    if case['missing'] and ("template '" + str(folder) + "/tmp.XXXXXXXXXX'").encode() not in old.stderr:
        raise AssertionError('旧版のmktempの文が末尾の/を1本にしていません')


def _find(case, f, binary, legacy):
    folder = f.repo / 'rdir'
    folder.mkdir()
    for entry in case['entries']:
        if entry.endswith('/'):
            (folder / entry).mkdir()
        else:
            (folder / entry).write_bytes(b'x\n')
    (f.repo / 'seed.txt').write_bytes(b'find change\n')
    from .compare import file_state

    def state():
        # 入れないディレクトリの中は読めない(lstatできない)ので、そのディレクトリの種類と権限だけを見る。
        paths = [f.repo / '.git/index', f.repo / '.git/config', folder]
        for top, dirs, files in os.walk(f.repo, followlinks=False):
            dirs[:] = [name for name in dirs if name != '.git' and Path(top) / name != folder]
            paths.extend(Path(top) / name for name in files)
        return {os.fsencode(path): (file_state(path), os.lstat(path).st_mode) for path in paths}
    folder.chmod(0o444)
    try:
        old, _ = _pair(f, binary, legacy, _args(f), cwd=f.root, code=case['code'], state=state)
    finally:
        folder.chmod(0o755)
    if '走査に失敗した経路あり'.encode() not in old.report or '`.git` という名前のエントリ'.encode() in old.report:
        raise AssertionError('旧版のfindがlstatの失敗を数えていません')


def proc_available():
    return platform.system() == 'Linux' and Path('/proc/self/mem').exists()


def _sleeper():
    return subprocess.Popen(['sleep', '300'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _proc(case, f, binary, legacy):
    if not proc_available():
        raise NotImplementedError('proc')
    sleeper = _sleeper()
    mem = f'/proc/{sleeper.pid}/mem'
    try:
        if not os.access(mem, os.R_OK):
            raise AssertionError('別のプロセスの/proc/PID/memが`[ -r ]`を通りません')
        # 旧版とRustは比較器の子(sleepの兄弟)なので、同じ立場の子で開けないことを確かめる。
        probe = subprocess.run([sys.executable, '-c', 'import os,sys; os.open(sys.argv[1], os.O_RDONLY)', mem],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env={}, check=False)
        if probe.returncode == 0 or b'PermissionError' not in probe.stderr:
            raise AssertionError('別のプロセスの/proc/PID/memが開けました(ptraceの制限が無い機械)')
        if case['variant'] == 'proc-pre-list':
            args = _args(f, '--pre-untracked', mem, '--pre-untracked-sha256', '0' * 64)
            rows = [dict(position=0, line=279, detail=mem.encode() + b': Permission denied')]
            old, _ = _pair(f, binary, legacy, args, cwd=f.root, code=20, rows=rows)
            expected = (b'diff-snapshot.sh: line 279: ' + mem.encode() + b': Permission denied\n'
                        + review_cases.internal(1, 279) + review_cases.internal(20, 694))
        else:
            (f.repo / 'd').mkdir()
            (f.repo / 'd/mem').write_bytes(b'tracked\n')
            f.git('add', '-A')
            f.git('commit', '-q', '-m', 'proc')
            f.base = f.git('rev-parse', 'HEAD').strip().decode()
            shutil.rmtree(f.repo / 'd')
            (f.repo / 'd').symlink_to(f'/proc/{sleeper.pid}')
            rows = [dict(position=i, line=line, detail=b'd/mem: Permission denied')
                    for i, line in enumerate((1314, 1315, 1347))]
            old, _ = _pair(f, binary, legacy, _args(f), cwd=f.root, code=20, rows=rows)
            expected = b''.join(b'diff-snapshot.sh: line %d: d/mem: Permission denied\n' % line
                                for line in (1314, 1315, 1347)) + review_cases.internal(1, 1347) \
                + review_cases.internal(20, 1347)
        if old.stderr != expected:
            raise AssertionError('旧版が予定の地点の診断で止まっていません')
    finally:
        os.killpg(sleeper.pid, 9)
        sleeper.wait()


def _shlvl(case, f, binary, legacy):
    review_cases.env_probe(dict(id=case['id'], env={'SHLVL': case['value']}), f, binary, legacy)


GREP_LINE = b'Binary files a/seed.txt and b/seed.txt differ\n'


def _grep_body(kind):
    """スタブのgitが本文(1389行の追跡差分)として返す内容。"""
    filler = b''.join(b'+filler line %06d with some width to the right\n' % i for i in range(5000))
    if kind == 'nul-same':
        return b'diff --git a/seed.txt b/seed.txt\n+x\0y\n' + GREP_LINE
    if kind == 'nul-later':
        # 最初の読取り(98304)の中の行は出し、NULのある後のバッファの行は出さない。
        head = b'diff --git a/seed.txt b/seed.txt\n' + GREP_LINE
        body = head + filler
        return body[:150000] + b'\n+late\0nul\n' + b'Binary files a/late.txt and b/late.txt differ\n'
    if kind == 'short-second':
        # 最初の読取りの終わりに200バイトの途中の行を残すと、基準のgrep 3.11(glibc 2.39)の2回目の読取りは
        # 98304ではなく94208バイトになる(3984 mod 4096の位置のバッファ)。その境の前の行は出る。
        head = b'diff --git a/seed.txt b/seed.txt\n'
        pad = b''
        line = b'+' + b'p' * 62 + b'\n'
        while len(head) + len(pad) + len(line) <= 98304 - 200:
            pad += line
        first = head + pad + b'+' + b'q' * (98304 - 200 - len(head) - len(pad) - 2) + b'\n'
        body = first + b'+' + b'r' * 400 + b'\n'
        while len(body) < 98304 + 94208 - 2000:
            body += line
        body += GREP_LINE
        while len(body) < 98304 + 94208 + 100:
            body += line
        return body + b'+late\0nul\n' + b'Binary files a/late.txt and b/late.txt differ\n'
    raise AssertionError(kind)


GREP_STUB = r'''import os,sys
args=sys.argv[1:]
spec=dict(line.split('=',1) for line in open(os.environ['S3D_PR2_SPEC']).read().splitlines())
def has(seq):
    return any(args[i:i+len(seq)]==seq for i in range(len(args)-len(seq)+1))
if has(['--dst-prefix=b/',spec['base'],'--']):
    os.write(1,open(spec['body'],'rb').read());sys.exit(0)
if spec.get('untracked') and has(['--no-index','--','/dev/null']) and args[-1]==spec['untracked']:
    os.write(1,b'Binary files /dev/null and b/'+spec['untracked'].encode()+b' differ\n');sys.exit(1)
os.execv(spec['real'],['git',*args])
'''


def _grep(case, f, binary, legacy):
    """1389行の追跡差分(と1490行の未追跡の差分)の出力をスタブのgitで決め、1652行のgrepの結果を比べる。
    本文は128 KiBを超えるので、環境変数ではなくファイルで渡す。"""
    (f.repo / 'seed.txt').write_bytes(b'grep change\n')
    stub = f.root / 'stub-grep'
    stub.mkdir()
    body = _grep_body('nul-same' if case['body'] == 'untracked' else case['body'])
    (stub / 'body').write_bytes(body)
    spec = {'base': f.base, 'body': str(stub / 'body'), 'real': shutil.which('git', path=f.env['PATH'])}
    if case['body'] == 'untracked':
        (f.repo / 'new.txt').write_bytes(b'new\n')
        spec['untracked'] = 'new.txt'
    (stub / 'spec').write_text(''.join(f'{k}={v}\n' for k, v in spec.items()))
    (stub / 'git').write_text('#!' + sys.executable + '\n' + GREP_STUB)
    (stub / 'git').chmod(0o700)
    env = {'PATH': str(stub) + os.pathsep + f.env['PATH'], 'S3D_PR2_SPEC': str(stub / 'spec')}
    old, _ = _pair(f, binary, legacy, _args(f), cwd=f.root, code=case['code'], env_update=env)
    rows = old.stderr.count('Binary files 検出'.encode())
    expected = {'nul-same': 0, 'nul-later': 1, 'short-second': 1, 'untracked': 1}[case['body']]
    if rows != expected:
        raise AssertionError(f'旧版のgrepの検出の行数が予定と違います: {rows}')


def _profile_underscore(case, f, binary, legacy):
    """Rustだけの確かめ: python3がPATHに無いと、profile用のgitへ`_`を渡さない(設計のD5の条件)。"""
    profile = f.repo / '.claude/project-profile.yml'
    profile.parent.mkdir()
    profile.write_bytes(b'secret_paths: [secret.key]\n')
    f.git('add', '-A')
    f.git('commit', '-q', '-m', 'profile')
    f.base = f.git('rev-parse', 'HEAD').strip().decode()
    (f.repo / 'seed.txt').write_bytes(b'profile underscore change\n')
    found = {}
    for python in (True, False):
        folder = f.root / ('tools-' + ('python' if python else 'none'))
        folder.mkdir()
        for tool in ('git', 'grep'):
            real = shutil.which(tool, path=f.env['PATH'])
            (folder / ('real-' + tool)).symlink_to(os.path.realpath(real))
        log = folder / 'log'
        (folder / 'git').write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                                    'fd=os.open(' + repr(str(log)) + ',os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)\n'
                                    'os.write(fd,(json.dumps([sys.argv[1:],os.environ.get("_")])+"\\n").encode())\n'
                                    'os.close(fd)\nos.execv(' + repr(str(folder / 'real-git')) + ',["git",*sys.argv[1:]])\n')
        (folder / 'git').chmod(0o700)
        (folder / 'grep').symlink_to(os.path.realpath(shutil.which('grep', path=f.env['PATH'])))
        if python:
            (folder / 'python3').symlink_to(os.path.realpath(sys.executable))
        f.clean_outputs()
        result = run([str(binary), *_args(f, '--secret-profile-ref', f.base)], cwd=f.root,
                     env=dict(f.env, PATH=str(folder)), report=f.out, patch=f.patch)
        fixed(result, 0)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        profile_calls = [value for args, value in calls if args[:1] == ['-C']]
        if not profile_calls:
            raise AssertionError('profile用のgitの起動を観測できません')
        found[python] = set(profile_calls)
    if found[True] != {str(f.root / 'tools-python/python3')}:
        raise AssertionError(f'python3があるとき`_`がpython3の解決パスではありません: {found[True]}')
    if found[False] != {None}:
        raise AssertionError(f'python3が無いときに`_`を渡しました: {found[False]}')


def _profile_loop(case, f, binary, legacy):
    # 旧helperは.claudeをO_NOFOLLOW|O_DIRECTORYで開く。symlinkはENOTDIRになる。
    (f.repo / 'real-claude').mkdir()
    (f.repo / 'real-claude/project-profile.yml').write_bytes(b'secret_paths: [secret.key]\n')
    (f.repo / '.claude').symlink_to('real-claude')
    (f.repo / 'seed.txt').write_bytes(b'profile loop change\n')
    old, _ = _pair(f, binary, legacy, _args(f, '--secret-profile-ref', f.base), cwd=f.root, code=20)
    if 'profile の親を安全に開けない: Not a directory'.encode() not in old.stderr:
        raise AssertionError('旧版がprofileの親を開けない文で止まっていません')


def run_case(case, f, binary, legacy):
    variant = case['variant']
    handlers = {'logical-pwd': _logical, 'logical-pwd-dir': _logical_dir, 'logical-pwd-other': _logical_other,
                'relative-tmpdir': _relative_tmpdir, 'tmpdir-slashes': _tmpdir_slashes, 'find': _find,
                'proc-pre-list': _proc, 'proc-tracked': _proc, 'shlvl': _shlvl, 'grep': _grep,
                'profile-underscore': _profile_underscore, 'profile-loop': _profile_loop}
    return handlers[variant](case, f, binary, legacy)
