"""通常入口の外部子待機で親だけへ信号を送り、回収後の停止を比べる。"""
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from . import closed_env
from .compare import Result,exact_ids,fixed,same_result,unchanged

# 待機させる2道具に加え、旧版とRustの両方が外部実行する一時領域・path解決の道具も記録する。
# 信号の後にこれらが1回でも起動すれば、後続のGit・ファイル操作へ進んだとみなす。
TOOLS=('git','grep','mktemp','realpath','readlink')
STUB=r'''import json,os,pathlib,subprocess,sys
root=pathlib.Path(os.environ['S3D_ENTRY_SYNC'])
name=pathlib.Path(sys.argv[0]).name
args=[os.fsencode(value).hex() for value in sys.argv[1:]]
fd=os.open(root/'calls',os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)
os.write(fd,(json.dumps({'tool':name,'args_hex':args})+'\n').encode());os.close(fd)
real=os.environ['S3D_REAL_'+name.upper()]
match=os.environ.get('S3D_ENTRY_MATCH','')
def selected(item):
    return item['tool']==os.environ['S3D_ENTRY_TARGET'] and (not match or match in item['args_hex'])
if not selected({'tool':name,'args_hex':args}):os.execv(real,[name,*sys.argv[1:]])
count=sum(selected(json.loads(line)) for line in (root/'calls').read_bytes().splitlines())
if count!=int(os.environ.get('S3D_ENTRY_NTH','1')):os.execv(real,[name,*sys.argv[1:]])
def notify(event,**values):
 fd=os.open(root/'notice',os.O_WRONLY)
 os.write(fd,(json.dumps(dict(event=event,pid=os.getpid(),**values))+'\n').encode());os.close(fd)
first=os.environ.get('S3D_ENTRY_RUN_FIRST')=='1'
if first:result=subprocess.run([real,*sys.argv[1:]],check=False,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
notify('waiting')
fd=os.open(root/'release',os.O_RDONLY)
if os.read(fd,1)!=b'x':raise RuntimeError('child release missing')
os.close(fd)
if first:
    os.write(1,result.stdout);os.write(2,result.stderr)
else:result=subprocess.run([real,*sys.argv[1:]],check=False)
notify('completed',code=result.returncode)
sys.exit(result.returncode)
'''


def cases():
    result=[dict(id=f'entry-signal-{tool}-{name}',tool=tool,signal=name)
            for tool in ('git','grep') for name in ('none','TERM','INT','HUP')]
    # Rustが待つ子はgitとgrepだけ(23:19の決定)。mktempなどはRustの中で行う。
    # 一時領域(613行)を作った後のgit・名前の列挙・patchの差分の待機中に信号を送り、
    # 一時領域と出力の親に何も残さないことを確かめる(mktempの入口の9件の置き換え)。
    for label,match in (('tmpd','--show-scope'),('listing','--exclude-standard'),('patch','--binary')):
        result+=[dict(id=f'entry-signal-{label}-{name}',tool='git',signal=name,match=match)
                 for name in ('TERM','INT','HUP')]
    # profile: 旧helperのGit待機中に親だけへ信号。helperは不正なrefで失敗する。
    result+=[dict(id=f'entry-signal-profile-{name}',tool='git',signal=name,match='--show-object-format',profile=True)
             for name in ('none','TERM','INT','HUP')]
    return result


def listing(path):
    """一時領域と出力の親に残った名前。中身は読まない。"""
    return sorted(os.fsencode(name) for name in os.listdir(path))


def verify(case,result,calls,notifications,*,leftovers,root_before,root_after):
    """1回の入口観測を検査し、旧新で比べる部分だけを返す。"""
    if [value.get('event') for value in notifications]!=['waiting','completed']:
        raise AssertionError('入口で待機した子の完了を観測していません')
    if notifications[1].get('pid')!=notifications[0].get('pid'):
        raise AssertionError('待機した子の完了が一致しません')
    if case.get('profile'):
        return verify_profile(case,result,calls,notifications,leftovers=leftovers,root_before=root_before,root_after=root_after)
    fixed(result,0 if case['signal']=='none' else 20,stdout=b'',stderr=b'')
    match=bytes(case.get('match','').encode()).hex()
    targets=[index for index,item in enumerate(calls) if item['tool']==case['tool'] and (not match or match in item['args_hex'])]
    if len(targets)<case.get('nth',1):raise AssertionError('待機対象の道具が起動されていません')
    target=targets[case.get('nth',1)-1]
    if leftovers:raise AssertionError('一時領域が片付けられていません')
    # 旧版はほかの道具も起動する。比べるのはRustも起動するgitとgrepの列。
    # ERE照合でないgrep(705・1576・1652行)はRustの中で行うので、列から除く。
    def launched(item):
        return item['tool']=='git' or (item['tool']=='grep' and item['args_hex'][:1] and bytes.fromhex(item['args_hex'][0]).startswith(b'-E'))
    prefix=[item for item in calls[:target+1] if launched(item)]
    # 1245行のパイプの2つの子は同時に起動するので、記録の順は競合で入れ替わる。
    index_info=b'--index-info'.hex()
    for i in range(len(prefix)-1):
        pair=prefix[i:i+2]
        if sum(index_info in item['args_hex'] for item in pair)==1 and all(item['tool']=='git' for item in pair):
            if index_info in pair[0]['args_hex'] and b'-s'.hex() in pair[1]['args_hex']:
                prefix[i],prefix[i+1]=pair[1],pair[0]
    if case['signal']=='none':
        if len(calls)<=target+1 or result.report is None or result.patch is None:
            raise AssertionError('正常対照が入口の後段へ到達していません')
    else:
        if len(calls)!=target+1:
            raise AssertionError('入口が保留中の信号の後も外部呼出しを続けました')
        if result.report is not None or result.patch is not None or root_before!=root_after:
            raise AssertionError('入口が保留中の信号の後もファイルを作成・公開しました')
    return {'calls_through_target':prefix,'reached_later_calls':len(calls)>target+1,
            'child_code':notifications[1].get('code'),'sync_target_hits':1,'child_completed':True}


INTERNAL='ERROR [internal] secret profile の和集合を作れない\n'.encode()


def verify_profile(case,result,calls,notifications,*,leftovers,root_before,root_after):
    """helperは信号を受けずに最後まで動く。親はhelperの失敗表示まで進まない。"""
    if case['signal']=='none':
        if result.returncode!=20 or result.stdout or not result.stderr.endswith(INTERNAL):
            raise AssertionError('正常対照がhelperの失敗表示に到達していません')
    else:
        fixed(result,20,stdout=b'',stderr=b'')
    if leftovers:raise AssertionError('一時領域が片付けられていません')
    if result.report is not None or result.patch is not None or root_before!=root_after:
        raise AssertionError('helperの失敗後にファイルを作成・公開しました')
    match=b'--show-object-format'.hex()
    targets=[i for i,item in enumerate(calls) if item['tool']=='git' and match in item['args_hex']]
    if len(targets)!=1:raise AssertionError('helperの最初のGitを1回だけ待機していません')
    later=calls[targets[0]:]
    if any(item['tool']!='git' for item in later):
        raise AssertionError('helperの後に外部呼出しを続けました')
    return {'calls_from_target':later,'child_code':notifications[1].get('code'),'sync_target_hits':1,'child_completed':True}


def observe(case,fixture,command,label):
    sync=fixture.root/(label+'-sync');sync.mkdir();bindir=sync/'bin';bindir.mkdir()
    scratch=fixture.root/(label+'-tmp');scratch.mkdir()
    real={}
    for tool in TOOLS:
        found=shutil.which(tool,path=fixture.env['PATH'])
        if found is None:raise AssertionError(f'入口の観測に必要な道具がありません: {tool}')
        real['S3D_REAL_'+tool.upper()]=found
        path=bindir/tool;path.write_text('#!'+sys.executable+'\n'+STUB);path.chmod(0o700)
    for name in ('notice','release'):os.mkfifo(sync/name,0o600)
    notice=os.open(sync/'notice',os.O_RDWR|os.O_NONBLOCK);release=os.open(sync/'release',os.O_RDWR|os.O_NONBLOCK)
    selector=selectors.DefaultSelector();selector.register(notice,selectors.EVENT_READ)
    env=dict(fixture.env,PATH=str(bindir)+os.pathsep+fixture.env['PATH'],S3D_ENTRY_SYNC=str(sync),
             S3D_ENTRY_TARGET=case['tool'],S3D_ENTRY_NTH=str(case.get('nth',1)),
             S3D_ENTRY_MATCH=case.get('match','').encode().hex(),
             S3D_ENTRY_RUN_FIRST='1' if case.get('run_first') else '0',TMPDIR=str(scratch),**real)
    fixture.clean_outputs();before=fixture.input_state()
    ignored={sync.name.encode(),scratch.name.encode()}
    root_before=[name for name in listing(fixture.root) if name not in ignored]
    started=time.time()
    extra=['--secret-profile-ref','f'*40] if case.get('profile') else []
    closed_env.guard(env, fixture.root)
    process=subprocess.Popen([*command,*fixture.normal(),'--patch-out',str(fixture.patch),*extra],cwd=fixture.root,env=env,
                             stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    began=time.monotonic();buffer=b'';notifications=[];stopped=False
    try:
        while process.poll() is None:
            if time.monotonic()-began>30:raise AssertionError('入口の待機子を回収できません')
            for key,_ in selector.select(0.05):
                buffer+=os.read(key.fd,4096)
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1);value=json.loads(line);notifications.append(value)
                    if value['event']=='waiting':
                        if len(notifications)!=1:raise AssertionError('入口の待機通知が重複しました')
                        if case['signal']!='none':
                            # 親を止めてから信号を保留させ、待機子の完了後に再開する。
                            os.kill(process.pid,signal.SIGSTOP)
                            pid,status=os.waitpid(process.pid,os.WUNTRACED)
                            if pid!=process.pid or not os.WIFSTOPPED(status) or os.WSTOPSIG(status)!=signal.SIGSTOP:
                                raise AssertionError('親の同期停止を確認できません')
                            stopped=True;os.kill(process.pid,getattr(signal,'SIG'+case['signal']))
                        os.write(release,b'x')
                    elif value['event']=='completed':
                        if len(notifications)!=2 or value['pid']!=notifications[0]['pid']:raise AssertionError('待機した子の完了が一致しません')
                        if stopped:os.kill(process.pid,signal.SIGCONT);stopped=False
                    else:raise AssertionError('未知の入口通知です')
        stdout,stderr=process.communicate(timeout=1)
    except BaseException:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.communicate(timeout=2);raise
    finally:selector.close();os.close(notice);os.close(release)
    if buffer:raise AssertionError('入口の通知が途中で切れました')
    unchanged(before,fixture.input_state())
    result=Result(process.returncode,stdout,stderr,fixture.out.read_bytes() if fixture.out.is_file() else None,
                  fixture.patch.read_bytes() if fixture.patch.is_file() else None,started,time.time())
    root_after=[name for name in listing(fixture.root) if name not in ignored and name not in (fixture.out.name.encode(),fixture.patch.name.encode())]
    calls=[json.loads(line) for line in (sync/'calls').read_bytes().splitlines()] if (sync/'calls').exists() else []
    observed=verify(case,result,calls,notifications,leftovers=listing(scratch),
                    root_before=root_before,root_after=root_after)
    return result,observed,calls


def run_cases(binary,*,fixture_type,legacy):
    done=[]
    with tempfile.TemporaryDirectory(prefix='s3d-entry-signals-') as temporary:
        for case in cases():
            root=Path(temporary)/case['id'];root.mkdir();fixture=fixture_type(root)
            (fixture.repo/'seed.txt').write_bytes(b'public entry signal change\n')
            if case.get('profile'):
                profile=fixture.repo/'.claude/project-profile.yml';profile.parent.mkdir();profile.write_bytes(b'secret_paths: []\n')
            try:
                old,old_observed,_=observe(case,fixture,['bash',str(legacy)],'legacy')
                new,new_observed,new_calls=observe(case,fixture,[str(binary)],'rust')
                same_result(old,new)
                if old_observed!=new_observed:raise AssertionError('入口の子実行列または完了が旧版と違います')
            except Exception as error:raise AssertionError(f'{case["id"]}: {error}') from error
            done.append(dict(case_id=case['id'],code=new.returncode,calls=len(new_calls),
                             compared_calls=len(new_observed.get('calls_through_target',new_observed.get('calls_from_target',[]))),
                             reached_later_calls=new_observed.get('reached_later_calls'),child_code=new_observed['child_code']))
    exact_ids([case['id'] for case in cases()],[value['case_id'] for value in done]);return done
