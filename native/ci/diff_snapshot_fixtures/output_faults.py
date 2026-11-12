"""実fdの故障を旧新版へ渡し、旧スクリプト自身の所在地欄だけを対応づける。"""
import dataclasses
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time

from . import closed_env
from .compare import note_versions, Result, same_result, unchanged
from .diagnostics import bash_locations

SOURCE_SHA256='aa6c5b71368ddd7b9a12d4af62d6ead8518a6560fe05b7b23f03a512e84f04fe'


def cases():
    result=[]
    for failure in ('closed','pipe','full'):
        for state in ('default','ignored'):
            for invocation in ('absolute','relative'):
                result.append(dict(id=f'output-print-{failure}-{state}-{invocation}',mode='print',
                                   failure=failure,signal_state=state,invocation=invocation,
                                   code=-13 if failure=='pipe' and state=='default' else 20))
    for failure in ('closed','pipe'):
        for state in ('default','ignored'):
            result.append(dict(id=f'output-stderr-{failure}-{state}',mode='error',failure=failure,
                               signal_state=state,invocation='absolute',code=-13 if failure=='pipe' and state=='default' else 1))
    result.append(dict(id='output-both-closed',mode='both',failure='closed',signal_state='ignored',invocation='absolute',code=1))
    return result


def execute(command,args,*,cwd,env,failure,mode,signal_state):
    opened=[];target=1 if mode in ('print','both') else 2
    streams={1:subprocess.PIPE,2:subprocess.PIPE}
    if failure=='pipe':
        reader,writer=os.pipe();os.close(reader);opened.append(writer);streams[target]=writer
    elif failure=='full':
        fd=os.open('/dev/full',os.O_WRONLY);opened.append(fd);streams[target]=fd
    def prepare():
        signal.signal(signal.SIGPIPE,signal.SIG_IGN if signal_state=='ignored' else signal.SIG_DFL)
        if failure=='closed':os.close(target)
        if mode=='both':os.close(2)
    start=time.time()
    try:
        closed_env.guard(env, cwd)
        process=subprocess.Popen([*command,*args],cwd=cwd,env=env,stdin=subprocess.DEVNULL,
                                 stdout=streams[1],stderr=streams[2],preexec_fn=prepare,start_new_session=True)
    finally:
        for fd in opened:os.close(fd)
    try:stdout,stderr=process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL);process.communicate();raise AssertionError('出力故障の子が回収されません')
    note_versions([*command,*args],cwd,stderr)
    return Result(process.returncode,stdout or b'',stderr or b'',started=start,ended=time.time())


def compare_case(case,fixture,binary,legacy):
    if hashlib.sha256(legacy.read_bytes()).hexdigest()!=SOURCE_SHA256:
        raise AssertionError('出力故障の旧ソース確認値が違います')
    invocation=str(legacy) if case['invocation']=='absolute' else os.path.relpath(legacy,fixture.root)
    args=['--print-exclude-ere','--exclude-glob','.env'] if case['mode'] in ('print','both') else ['--unknown-s3d']
    values=[]
    for command in (['bash',invocation],[str(binary)]):
        before=fixture.input_state()
        value=execute(command,args,cwd=fixture.root,env=fixture.env,failure=case['failure'],mode=case['mode'],signal_state=case['signal_state'])
        unchanged(before,fixture.input_state())
        if value.returncode!=case['code']:raise AssertionError('出力故障の固定終了値が違います')
        values.append(value)
    old,new=values
    if case['mode']=='print' and case['code']==20:
        detail=b'printf: write error: '+{'closed':b'Bad file descriptor','pipe':b'Broken pipe','full':b'No space left on device'}[case['failure']]
        mapped=bash_locations(old.stderr,invocation=invocation,rows=[{'position':0,'line':567,'detail':detail}])
        old=dataclasses.replace(old,stderr=mapped)
    same_result(old,new)
    if fixture.out.exists() or fixture.patch.exists():raise AssertionError('出力故障で成果物を公開しました')
    return {'id':case['id'],'status':'compared','code':new.returncode}


def run_cases(binary,*,fixture_type,legacy):
    import tempfile
    done=[]
    with tempfile.TemporaryDirectory(prefix='s3d-output-faults-') as temporary:
        for case in cases():
            if case['failure']=='full' and not Path('/dev/full').is_char_device():
                done.append({'id':case['id'],'status':'input-unavailable','reason':'no-dev-full'});continue
            root=Path(temporary)/case['id'];root.mkdir()
            try:done.append(compare_case(case,fixture_type(root),binary,legacy))
            except Exception as error:raise AssertionError(f'{case["id"]}: {error}') from error
    return done
