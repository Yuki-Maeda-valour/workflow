"""Python起動環境の生byteと数値境界を、現在/履歴profileの全入口で比べる。"""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import tempfile
from . import closed_env
from .compare import fixed,run,same_result,unchanged
from .fd_cases import fixed_thread
from .output_faults import SOURCE_SHA256
from . import utf8_fixed


def definitions():
    values={
        'PYTHONUTF8':[b'\xff',b'0\xff',b'00',b'01',b'+0',b'+1',b' 0',b'0 ',b' 1',b'1 ',b'-0',b'-1',b'\t1',b'1\n'],
        'PYTHONINTMAXSTRDIGITS':[b'\xff',b'0\xff',b'00',b'01',b'+0',b'+1',b' 0',b'0 ',b'-0',b'-1',b'639',b'640',b' 640',b'+0640',b'640 ',b'2147483646',b'2147483647',b'2147483648',b'4294967295',b'4294967296',b'\t640',b'640\n'],
        'PYTHONIOENCODING':[b'ut\xfff8',b'utf-8:\xff',b'\xff',b'ascii\xff',b'UTF_8',b'utf-8:strict'],
    }
    for whitespace in (b' ',b'\t',b'\n',b'\r',b'\v',b'\f'):
        for raw in (whitespace+b'640',b'640'+whitespace):
            if raw not in values['PYTHONINTMAXSTRDIGITS']:values['PYTHONINTMAXSTRDIGITS'].append(raw)
    return [dict(id=f'raw-env-{key.lower()}-{raw.hex()}-{origin}-{int(error)}',key=key,value_hex=raw.hex(),origin=origin,error=error)
            for key,items in values.items() for raw in items for origin in ('current','historic') for error in (False,True)]


def prepare(case,fixture):
    path=fixture.repo/'.claude/project-profile.yml';path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'secret_paths: null\n' if case['error'] else 'secret_paths: [日本語]\n'.encode())
    if case['origin']=='historic':
        fixture.git('add','--','.claude/project-profile.yml');fixture.git('commit','-q','-m','profile')
        fixture.base=fixture.git('rev-parse','HEAD').strip().decode();path.unlink()
    env=dict(fixture.env);env[case['key']]=os.fsdecode(bytes.fromhex(case['value_hex']))
    return fixture.normal()+['--secret-profile-ref',fixture.base,'--patch-out',str(fixture.patch)],env


def execute(command,args,env,fixture):
    fixture.clean_outputs();before=fixture.input_state()
    value=run([*command,*args],cwd=fixture.root,env=env,report=fixture.out,patch=fixture.patch)
    unchanged(before,fixture.input_state());return value


def compare(case,old,new):
    for value in (old,new):fixed(value,case['expected'])
    if 'thread_before_hex' in case:
        before=bytes.fromhex(case['thread_before_hex']);after=bytes.fromhex(case['thread_after_hex'])
        old=dataclasses.replace(old,stderr=fixed_thread(old.stderr,before,after))
        new=dataclasses.replace(new,stderr=fixed_thread(new.stderr,before,after))
    elif old.stderr.hex()!=case['stderr_hex']:raise AssertionError('生byte環境の固定された旧診断が違います')
    same_result(old,new)


def cases():
    stored=json.loads(Path(__file__).with_name('raw_env.json').read_text())
    if stored['source_sha256']!=SOURCE_SHA256:raise AssertionError('環境境界の旧ソース確認値が違います')
    expected=definitions();rows=stored['cases']
    if [{k:row[k] for k in ('id','key','value_hex','origin','error')} for row in rows]!=expected:
        raise AssertionError('生byte環境の入力集合が違います')
    return rows


def run_cases(binary,*,fixture_type,legacy):
    if hashlib.sha256(legacy.read_bytes()).hexdigest()!=SOURCE_SHA256:raise AssertionError('環境境界の旧実行ソースが違います')
    done=[]
    with tempfile.TemporaryDirectory(prefix='s3d-raw-env-') as temporary:
        for case in cases():
            root=Path(temporary)/case['id'];root.mkdir();fixture=fixture_type(root)
            args,env=prepare(case,fixture)
            try:
                old=execute(['bash',str(legacy)],args,env,fixture)
                if utf8_fixed.has_settings({case['key']:''}):
                    reference=utf8_fixed.reference_env(env);utf8_fixed.check_difference(env,reference);utf8_fixed.check_effective(reference)
                    ref=execute(['bash',str(legacy)],args,reference,fixture);new=execute([str(binary)],args,env,fixture)
                    closed_env.check_recorded_difference(fixture.root,-3,-2,utf8_fixed.SETTINGS)
                    fixed(old,case['expected'])
                    if 'thread_before_hex' in case:fixed_thread(old.stderr,bytes.fromhex(case['thread_before_hex']),bytes.fromhex(case['thread_after_hex']))
                    elif old.stderr.hex()!=case['stderr_hex']:raise AssertionError('生byte環境の固定された旧診断が違います')
                    utf8_fixed.same_with_thread(utf8_fixed.expectation(case['id'],old,ref),new,same_result)
                else:compare(case,old,execute([str(binary)],args,env,fixture))
            except Exception as error:raise AssertionError(f'{case["id"]}: {error}') from error
            done.append(case['id'])
    return done
