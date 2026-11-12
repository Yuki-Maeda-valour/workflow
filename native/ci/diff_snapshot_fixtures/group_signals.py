"""プロセスグループへの信号(TERM・INT・HUP)。gitのスタブを止め、その間にグループへ送る。

区間は2つ。profileの和集合を作る区間(旧版はhelperの起動から終了まで。止めるのはhelperの最初のgit)と、
通常のgitの実行中(856行の名前の列挙)。スタブは本物のgitと同じく信号の既定の動作のまま待つので、
グループへの信号で止まる。旧helperも止まる(SIGINTでは呼出履歴を自分の標準エラーのファイルへ書く)。
旧版のbashは、TERMとHUPで止まった前面の子を`Terminated`・`Hangup`の行で知らせる(INTは知らせない)。
比べるのは、終了値・標準出力・標準エラー・公開と一時領域で、呼出履歴が出力へ出ないことと、
親が終わって放した後にグループに子が残らないことも確かめる。

起動の形ごとの区間(SITES)も同じ方法で止める。旧bashは`$(…)`の中の子を知らせず、前面の子
(出力を捨てるgit・パイプのgit・`printf … | grep`のgrep)は、パイプの最初に信号で止まった要素の
信号で知らせる(job_signal_status)。基準のbashの観測で、知らせの行の有無を固定する。
"""
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

from . import closed_env
from .compare import Result, fixed, same_result, unchanged

STUB = r'''import json,os,pathlib,re,signal,subprocess,sys
# 本物のgitとgrepと同じく、受け継いだSIGINTの扱いのまま止まる(Pythonの呼出履歴を出さない)。
if signal.getsignal(signal.SIGINT) is signal.default_int_handler:signal.signal(signal.SIGINT,signal.SIG_DFL)
sync=pathlib.Path(os.environ['S3D_GROUP_SYNC'])
real=os.environ['S3D_GROUP_REAL']
tool=os.environ['S3D_GROUP_TOOL']
args=sys.argv[1:]
if not re.search(os.environ['S3D_GROUP_MATCH'],' '.join(args)) or (sync/'taken').exists():os.execv(real,[tool,*args])
(sync/'taken').write_bytes(b'')
def notify(event,**values):
    fd=os.open(sync/'notice',os.O_WRONLY);os.write(fd,(json.dumps(dict(event=event,pid=os.getpid(),**values))+'\n').encode());os.close(fd)
notify('waiting')
fd=os.open(sync/'release',os.O_RDONLY)
if os.read(fd,1)!=b'x':raise RuntimeError('release missing')
os.close(fd)
code=subprocess.run([real,*args],check=False).returncode
try:notify('completed',code=code)
except OSError:pass
sys.exit(code)
'''
AREAS = {'profile-union': '--show-object-format', 'git-run': '--exclude-standard'}
# 起動の形ごとの区間: (止める道具, 引数の正規表現, 旧版の行と形, TERM・HUPで知らせの行が出るか)。
SITES = {
    'comsub-git': ('git', r'rev-parse --show-toplevel$', '576 $(git …)', False),
    'comsub-pipe-git': ('git', r'(^| )ls-tree -z ', '1321 $(git … | tr)', False),
    'discarded-git': ('git', r'\^\{tree\}$', '790 git … >/dev/null', True),
    'merge-base-git': ('git', r'merge-base --is-ancestor ', '1127 git … >/dev/null', True),
    'pipe-last-git': ('git', r'update-index -z --index-info$', '1245 git … | git …', True),
    'pipe-head-git': ('git', r'cat-file blob ', '1339 git … | head -c 8000', True),
    'pipe-validate-grep': ('grep', r'^-Eqz -- ', "556 printf '' | grep", True),
    'pipe-hit-grep': ('grep', r'^-Eiqz -- ', "372 printf … | grep", True),
}


def group_empty(pgid, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def observe(case, f, command, label):
    sync = f.root / (label + '-group')
    (sync / 'bin').mkdir(parents=True)
    scratch = f.root / (label + '-tmp')
    scratch.mkdir()
    tool, match = (SITES[case['area']][0], SITES[case['area']][1]) if case['area'] in SITES \
        else ('git', re.escape(AREAS[case['area']]))
    real = shutil.which(tool, path=f.env['PATH'])
    stub = sync / 'bin' / tool
    stub.write_text('#!' + sys.executable + '\n' + STUB)
    stub.chmod(0o700)
    for name in ('notice', 'release'):
        os.mkfifo(sync / name, 0o600)
    notice = os.open(sync / 'notice', os.O_RDWR | os.O_NONBLOCK)
    release = os.open(sync / 'release', os.O_RDWR | os.O_NONBLOCK)
    selector = selectors.DefaultSelector()
    selector.register(notice, selectors.EVENT_READ)
    env = dict(f.env, PATH=str(sync / 'bin') + os.pathsep + f.env['PATH'], S3D_GROUP_SYNC=str(sync),
               S3D_GROUP_REAL=real, S3D_GROUP_TOOL=tool, S3D_GROUP_MATCH=match, TMPDIR=str(scratch))
    args = [*f.normal(), '--patch-out', str(f.patch)]
    if case['area'] == 'profile-union':
        args += ['--secret-profile-ref', f.base]
    f.clean_outputs()
    before = f.input_state()
    started = time.time()
    closed_env.guard(env, f.root)
    process = subprocess.Popen([*command, *args], cwd=f.root, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    buffer, events, sent = b'', [], False
    try:
        began = time.monotonic()
        while not sent:
            if time.monotonic() - began > 30 or process.poll() is not None:
                raise AssertionError('gitのスタブが待機しませんでした')
            for key, _ in selector.select(0.05):
                buffer += os.read(key.fd, 4096)
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                events.append(json.loads(line))
                if events[-1]['event'] == 'waiting' and not sent:
                    if case['area'] in SITES:
                        # パイプのほかの要素(スタブを通る本物のgit)が起動を終えるのを待つ。Pythonの起動中に
                        # SIGINTが届くと、本物のgitに無い呼出履歴が出るため。
                        time.sleep(0.3)
                    os.killpg(process.pid, getattr(signal, 'SIG' + case['signal']))
                    sent = True
        # 信号の後に放す(旧helperはその間に止まり、旧版の親は待っていた子の終了後にtrapへ進む)。
        time.sleep(0.3)
        os.write(release, b'x')
        stdout, stderr = process.communicate(timeout=60)
        if not group_empty(process.pid, 10):
            raise AssertionError('親の終了後もプロセスグループに子が残りました')
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)
        raise
    finally:
        selector.close()
        os.close(notice)
        os.close(release)
    unchanged(before, f.input_state())
    if os.listdir(scratch):
        raise AssertionError(f'{label}: 一時領域が残りました')
    result = Result(process.returncode, stdout, stderr, f.out.read_bytes() if f.out.is_file() else None,
                    f.patch.read_bytes() if f.patch.is_file() else None, started, time.time())
    if b'Traceback' in stderr or b'KeyboardInterrupt' in stderr:
        raise AssertionError(f'{label}: helperの呼出履歴が出力へ出ました')
    return result


def run_case(case, f, binary, legacy):
    (f.repo / 'seed.txt').write_bytes(b'group signal change\n')
    if case['area'] == 'profile-union':
        profile = f.repo / '.claude/project-profile.yml'
        profile.parent.mkdir()
        profile.write_bytes(b'secret_paths:\n  - secret.key\n')
        f.git('add', '--', '.claude/project-profile.yml')
        f.git('commit', '-q', '-m', 'profile')
        f.base = f.git('rev-parse', 'HEAD').strip().decode()
    old = observe(case, f, ['bash', str(legacy)], 'legacy')
    new = observe(case, f, [str(binary)], 'rust')
    fixed(old, 20, stdout=b'')
    if case['area'] in SITES:
        # 旧bashの知らせの行の有無を、起動の形の観測(基準のbash)で固定する。
        line = {'TERM': b'Terminated\n', 'HUP': b'Hangup\n', 'INT': b''}[case['signal']]
        reported = SITES[case['area']][3] and bool(line)
        if old.stderr != (line if reported else b''):
            raise AssertionError('旧bashの知らせの行が、起動の形の観測と違います')
    same_result(old, new)
    if old.report is not None or old.patch is not None:
        raise AssertionError('信号の後に公開しました')
    return old, new
