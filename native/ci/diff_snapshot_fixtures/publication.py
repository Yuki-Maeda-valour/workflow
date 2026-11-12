"""旧版の実公開操作をpipeで止め、型テストと同じ36条件の残存物を観測する。"""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

from . import closed_env
from .compare import exact_ids, file_state, fixed, high_fd, unchanged
from .vectors import vectors

STUB = r'''import json,os,pathlib,subprocess,sys
name=pathlib.Path(sys.argv[0]).name
root=pathlib.Path(os.environ['S3D_PUBLICATION_ROOT'])
point=os.environ['S3D_PUBLICATION_POINT']
action=os.environ['S3D_PUBLICATION_ACTION']

def sync(value):
    os.write(int(os.environ['S3D_NOTICE_FD']),(value+'\n').encode())
    if os.read(int(os.environ['S3D_RELEASE_FD']),1)!=b'x':raise RuntimeError('missing release')
    (root/'child-completed').write_text('done')
    return value==point and os.environ.get('S3D_INITIAL_IGNORE')!='1'

def capture():
    for directory in root.glob('.diff-snapshot.*'):
        for name in ('out','patch'):
            path=directory/(name+'.tmp')
            if path.is_file():(root/('expected-'+name)).write_bytes(path.read_bytes())

if name=='mv':
    args=sys.argv[1:];src=pathlib.Path(args[-2]);dst=pathlib.Path(args[-1])
    kind='patch' if dst==root/'out.patch' else 'out' if dst==root/'out.md' else None
    if kind:
        capture()
        stop=sync(kind+'-before')
        if stop:sys.exit(0)
        if action==kind+'-failure':sys.exit(1)
        result=subprocess.run([os.environ['S3D_REAL_MV'],*args],check=False)
        if result.returncode:sys.exit(result.returncode)
        sync(kind+'-after');sys.exit(0)
    os.execv(os.environ['S3D_REAL_MV'],[os.environ['S3D_REAL_MV'],*args])
if name=='rm':
    args=sys.argv[1:]
    if (root/'out.md').is_file() and (root/'out.patch').is_file() and any('.diff-snapshot.' in arg for arg in args):
        try:fd=os.open(root/'cleanup-noticed',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:pass
        else:os.close(fd);sync('cleanup-before')
    os.execv(os.environ['S3D_REAL_RM'],[os.environ['S3D_REAL_RM'],*args])
raise RuntimeError('unexpected command')
'''


def expected_state(case):
    success=case.get('action')=='success' or case.get('state')=='ignored'
    published=success or case.get('point') in ('out-after','cleanup-before')
    return {'code':0 if success else 20,
            'out': 'new' if published else 'old' if case['old_out'] else None,
            'patch':'new' if published else None}


def verify_typed(case, observed):
    expected=expected_state(case)
    out=None if observed.get('out_hex') is None else bytes.fromhex(observed['out_hex'])
    patch=None if observed.get('patch_hex') is None else bytes.fromhex(observed['patch_hex'])
    actual={'code':observed.get('code'),
            'out':None if out is None else 'old' if out==b'OLD_REPORT\n' else 'new' if out==b'NEW_REPORT\n' else 'unknown',
            'patch':None if patch is None else 'new' if patch==b'NEW_PATCH\n' else 'unknown'}
    if actual!=expected or observed.get('temporary_directories')!=[]:
        raise AssertionError('型検査の公開残存物または終了値が旧契約と違います')
    if observed.get('sync_target_hits')!=(1 if 'point' in case else 0):
        raise AssertionError('型検査の同期点の到達数が違います')
    if 'point' in case and observed.get('points',[]).count(case['point'])!=1:
        raise AssertionError('型検査の指定同期点へ1回到達していません')
    return actual


def observe(case, fixture, legacy):
    import shutil
    (fixture.repo/'seed.txt').write_bytes(b'new tracked text\n')
    if case['old_out']:fixture.out.write_bytes(b'OLD_REPORT\n')
    old=file_state(fixture.out);before=fixture.input_state()
    bindir=fixture.root/'bin';bindir.mkdir()
    for command in ('mv','rm'):
        path=bindir/command;path.write_text('#!'+sys.executable+'\n'+STUB);path.chmod(0o700)
    notice_read,notice_write=map(high_fd,os.pipe());release_read,release_write=map(high_fd,os.pipe())
    env=dict(fixture.env,PATH=str(bindir)+os.pathsep+fixture.env['PATH'],
             S3D_PUBLICATION_ROOT=str(fixture.root),S3D_PUBLICATION_POINT=case.get('point',''),
             S3D_PUBLICATION_ACTION=case.get('action',''),S3D_REAL_MV=shutil.which('mv'),S3D_REAL_RM=shutil.which('rm'),
             S3D_NOTICE_FD=str(notice_write),S3D_RELEASE_FD=str(release_read),
             S3D_INITIAL_IGNORE='1' if case.get('state')=='ignored' else '0')
    closed_env.guard(env, fixture.root)
    process=subprocess.Popen(['bash',str(legacy),*fixture.normal(),'--patch-out',str(fixture.patch)],cwd=fixture.root,env=env,
                             stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             pass_fds=(notice_write,release_read),start_new_session=True,
                             preexec_fn=(lambda: signal.signal(getattr(signal,'SIG'+case['signal']),signal.SIG_IGN)) if case.get('state')=='ignored' else None)
    os.close(notice_write);os.close(release_read)
    selection=selectors.DefaultSelector();selection.register(notice_read,selectors.EVENT_READ)
    began=time.monotonic();points=[];buffer=b'';hits=0
    try:
        while process.poll() is None:
            if time.monotonic()-began>30:raise AssertionError('旧公開同期点が時間切れです')
            for key,_ in selection.select(0.1):
                chunk=os.read(key.fd,4096)
                if not chunk:selection.unregister(key.fd);continue
                buffer+=chunk
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1);point=line.decode('ascii');points.append(point)
                    if point==case.get('point'):
                        hits+=1
                        if hits!=1:raise AssertionError('旧公開同期点へ2回到達しました')
                        os.kill(process.pid,getattr(signal,'SIG'+case['signal']))
                    os.write(release_write,b'x')
        stdout,stderr=process.communicate(timeout=1)
    except BaseException:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.communicate(timeout=2)
        raise
    finally:
        selection.close();os.close(notice_read);os.close(release_write)
    if buffer:raise AssertionError('旧公開同期通知が途中で切れました')
    if 'point' in case and not (fixture.root/'child-completed').is_file():raise AssertionError('同期中の子が回収前に完了していません')
    if hits!=(1 if 'point' in case else 0):raise AssertionError('旧公開同期点へ未到達です')
    if stdout:raise AssertionError('旧公開の標準出力が空でありません')
    unchanged(before,fixture.input_state())
    expected=expected_state(case)
    if process.returncode!=expected['code']:raise AssertionError('旧公開の終了値が固定値と違います')
    out=None if not fixture.out.exists() else fixture.out.read_bytes()
    patch=None if not fixture.patch.exists() else fixture.patch.read_bytes()
    if expected['out']=='new':
        if out!=(fixture.root/'expected-out').read_bytes():raise AssertionError('公開後の旧Markdownが生成byteと違います')
    elif expected['out']=='old':
        unchanged(old,file_state(fixture.out))
    elif out is not None:raise AssertionError('未公開のMarkdownが存在します')
    if expected['patch']=='new':
        if patch!=(fixture.root/'expected-patch').read_bytes():raise AssertionError('公開後の旧patchが生成byteと違います')
    elif patch is not None:raise AssertionError('失敗後のpatchが残りました')
    temporary=[path.name for path in fixture.root.glob('.diff-snapshot.*')]
    if temporary:raise AssertionError('旧公開の一時領域が残りました')
    return dict(case_id=case['id'],**expected,temporary_directories=temporary,points=points,sync_target_hits=hits)


def run_cases(*,fixture_type,legacy,typed_observations):
    cases=[case for case in vectors() if case['category']=='publish']
    exact_ids([case['id'] for case in cases],[value['case_id'] for value in typed_observations])
    typed={value['case_id']:value for value in typed_observations};done=[]
    with tempfile.TemporaryDirectory(prefix='s3d-publication-') as temporary:
        for case in cases:
            root=Path(temporary)/case['id'];root.mkdir()
            try:
                state=verify_typed(case,typed[case['id']]);legacy_state=observe(case,fixture_type(root),legacy)
                if any(legacy_state[key]!=value for key,value in state.items()):raise AssertionError('旧新版の公開状態が違います')
                done.append(legacy_state)
            except Exception as error:raise AssertionError(f'{case["id"]}: {error}') from error
    return done


def verify_signal(case,value):
    expected=0 if case['state']=='ignored' else 20
    received=None if case['state']=='ignored' else int(getattr(signal,'SIG'+case['signal']))
    if value.get('code')!=expected or type(value.get('received')) is not type(received) or value.get('received')!=received or value.get('child_completed') is not (case['state']=='child-wait') or value.get('captured_stderr_hex')!='':
        raise AssertionError('型検査の信号・子の回収が旧契約と違います')


def run_signals(*,fixture_type,legacy,typed_observations):
    cases=[case for case in vectors() if case['category']=='signal']
    exact_ids([case['id'] for case in cases],[value['case_id'] for value in typed_observations])
    typed={value['case_id']:value for value in typed_observations};done=[]
    with tempfile.TemporaryDirectory(prefix='s3d-signals-') as temporary:
        for case in cases:
            root=Path(temporary)/case['id'];root.mkdir()
            value=typed[case['id']]
            verify_signal(case,value)
            try:observed=observe(dict(case,old_out=False,point='patch-before'),fixture_type(root),legacy)
            except Exception as error:raise AssertionError(f'{case["id"]}: {error}') from error
            done.append(dict(case_id=case['id'],code=observed['code'],received=case['state']!='ignored',child_completed=True))
    return done
