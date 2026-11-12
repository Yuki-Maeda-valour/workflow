"""起動前の3本のfdを閉じる/ディレクトリにする条件を各モードで比較する。"""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from . import closed_env
from .compare import note_versions, Result, same_result, unchanged, exact_ids
from .diagnostics import bash_locations
from .output_faults import SOURCE_SHA256
from . import utf8_fixed


def definitions():
    return [dict(id=f'fd-{mode}-{fd}-{kind}-{state}',mode=mode,fd=fd,kind=kind,state=state)
            for mode in ('print','precheck','normal','profile','profile-error','profile-fatal') for fd in (0,1,2)
            for kind in ('closed','directory') for state in ('default','ignored')]


def arguments(fixture, mode):
    if mode=='print':return ['--print-exclude-ere','--exclude-glob','.env']
    if mode=='precheck':return ['--cwd',str(fixture.repo),'--precheck']
    result=fixture.normal()+['--patch-out',str(fixture.patch)]
    if mode.startswith('profile'):
        path=fixture.repo/'.claude/project-profile.yml';path.parent.mkdir(exist_ok=True);path.write_bytes(b'secret_paths: null\n' if mode=='profile-error' else b'secret_paths: [secret.key]\n')
        if mode=='profile-fatal':fixture.env['PYTHONIOENCODING']='does-not-exist-s3d'
        result+=['--secret-profile-ref',fixture.base]
    return result


def execute(case,fixture,command,transform=None):
    args=arguments(fixture,case['mode']);fixture.clean_outputs();before=fixture.input_state()
    environment=transform(dict(fixture.env)) if transform else fixture.env
    streams={0:subprocess.DEVNULL,1:subprocess.PIPE,2:subprocess.PIPE};descriptor=None
    if case['kind']=='directory':descriptor=os.open(fixture.root,os.O_RDONLY);streams[case['fd']]=descriptor
    def prepare():
        signal.signal(signal.SIGPIPE,signal.SIG_IGN if case['state']=='ignored' else signal.SIG_DFL)
        if case['kind']=='closed':os.close(case['fd'])
    start=time.time()
    closed_env.guard(environment, fixture.root)
    try:process=subprocess.Popen([*command,*args],cwd=fixture.root,env=environment,
                                 stdin=streams[0],stdout=streams[1],stderr=streams[2],preexec_fn=prepare,start_new_session=True)
    finally:
        if descriptor is not None:os.close(descriptor)
    try:stdout,stderr=process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL);process.communicate();raise AssertionError('起動時fd検査の子を回収できません')
    result=Result(process.returncode,stdout or b'',stderr or b'',
                  fixture.out.read_bytes() if fixture.out.is_file() else None,
                  fixture.patch.read_bytes() if fixture.patch.is_file() else None,start,time.time())
    note_versions([*command,*args],fixture.root,result.stderr)
    unchanged(before,fixture.input_state())
    return result


def fixed_thread(raw, before, after):
    # 1入力に固定された全文の前後・位置・回数・桁を先に確認する。
    expression=re.escape(before)+rb'Current thread 0x[0-9a-f]{16} \(most recent call first\):\n'+re.escape(after)
    if raw.count(b'Current thread ')!=1 or re.fullmatch(expression,raw) is None:
        raise AssertionError('固定したPython起動失敗のthread欄と一致しません')
    return before+b'Current thread <runtime-thread> (most recent call first):\n'+after


def compare(case,old,new,*,legacy):
    if old.returncode!=case['expected'] or new.returncode!=case['expected']:
        raise AssertionError('起動時fdの固定終了値が旧新版と一致しません')
    if case.get('bash_rows'):
        rows=[dict(row,detail=bytes.fromhex(row['detail_hex'])) for row in case['bash_rows']]
        old=dataclasses.replace(old,stderr=bash_locations(old.stderr,invocation=str(legacy),rows=rows))
    if 'thread_before_hex' in case:
        before=bytes.fromhex(case['thread_before_hex']);after=bytes.fromhex(case['thread_after_hex'])
        old=dataclasses.replace(old,stderr=fixed_thread(old.stderr,before,after))
        new=dataclasses.replace(new,stderr=fixed_thread(new.stderr,before,after))
    same_result(old,new)


def cases():
    value=json.loads(Path(__file__).with_name('fd_cases.json').read_text())
    if value['source_sha256']!=SOURCE_SHA256:raise AssertionError('起動時fdの旧ソース確認値が違います')
    rows=value['cases'];expected=definitions()
    if [{k:row[k] for k in ('id','mode','fd','kind','state')} for row in rows]!=expected:
        raise AssertionError('起動時fdの入力集合が違います')
    return rows


def run_cases(binary,*,fixture_type,legacy):
    if hashlib.sha256(legacy.read_bytes()).hexdigest()!=SOURCE_SHA256:raise AssertionError('起動時fdの旧実行ソースが違います')
    done=[]
    with tempfile.TemporaryDirectory(prefix='s3d-fd-') as temporary:
        for case in cases():
            root=Path(temporary)/case['id'];root.mkdir();fixture=fixture_type(root)
            try:
                old=execute(case,fixture,['bash',str(legacy)]);new=execute(case,fixture,[str(binary)])
                if case['mode']=='profile-fatal':
                    original=dict(fixture.env);reference=utf8_fixed.reference_env(original)
                    utf8_fixed.check_difference(original,reference);utf8_fixed.check_effective(reference)
                    ref=execute(case,fixture,['bash',str(legacy)],transform=utf8_fixed.reference_env)
                    closed_env.check_recorded_difference(fixture.root,-3,-1,utf8_fixed.SETTINGS)
                    if old.returncode!=case['expected']:raise AssertionError('起動時fdの固定終了値が旧版と一致しません')
                    utf8_fixed.same_with_thread(utf8_fixed.expectation(case['id'],old,ref),new,same_result)
                else:compare(case,old,new,legacy=legacy)
            except Exception as error:raise AssertionError(f'{case["id"]}: {error}') from error
            done.append(case['id'])
    return done
