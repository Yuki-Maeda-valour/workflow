"""型監査の実体情報とGit実行列を、旧実入口の実行列に照合する。"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from .compare import exact_ids,fixed,run,unchanged


def ids():
    return [f'audit-typed-{access}-{change}' for access in ('readable','denied') for change in ('modified','unchanged')]+[
        'audit-typed-reject-'+kind for kind in ('read','hash','extra-git')]


def git_calls(events,root):
    calls=[]
    for event in events:
        if event['operation'] not in ('Open','Read','Hash','Spawn'):raise AssertionError('型監査に未知の操作があります')
        if event['operation']!='Spawn':continue
        args=[bytes.fromhex(value) for value in event['arguments_hex']]
        path=bytes.fromhex(event['path_hex'])
        if not event['child_pid'] or not event['dev'] or not event['ino'] or not os.path.isabs(path):
            raise AssertionError('子の実体情報が欠落しています')
        if not args or args[0]!=b'git':continue
        if Path(os.fsdecode(path)).name!='git':raise AssertionError('Gitの実行実体とargv0が違います')
        mapped=[]
        for index,arg in enumerate(args):
            if arg==root:
                if index==0 or args[index-1]!=b'-C':raise AssertionError('未確認の引数へrootを対応づけようとしました')
                arg=b'<fixture-root>'
            mapped.append(arg)
        calls.append((path,tuple(mapped)))
    for i in range(len(calls)-1):
        if b'--index-info' in calls[i][1] and b'ls-files' in calls[i+1][1] and b'-s' in calls[i+1][1]:calls[i:i+2]=[calls[i+1],calls[i]]
    if not calls:raise AssertionError('Gitの実行が観測されませんでした')
    return calls


def validate_secret(value,*,reject=None):
    secret=value['secret'];path=bytes.fromhex(secret['path_hex']);identity=(secret['dev'],secret['ino'])
    if not os.path.isabs(path) or not all(type(part) is int and part>0 for part in identity):raise AssertionError('機密実体の識別が欠落しています')
    operations=[]
    for event in value['events']:
        target=bytes.fromhex(event['path_hex'])==path or (event['dev'],event['ino'])==identity
        if target and event['operation'] in ('Open','Read','Hash'):
            operations.append(event['operation'])
    if reject is None and operations:raise AssertionError('Rust自身が機密本文へアクセスしました')
    if reject in ('read','hash'):
        required=['Read'] if reject=='read' else ['Read','Hash']
        if any(item not in operations for item in required):raise AssertionError('意図的な本文操作の証拠が欠落しています')
        for operation in required:
            aliases=[event for event in value['events'] if event['operation']==operation and bytes.fromhex(event['path_hex'])!=path and (event['dev'],event['ino'])==identity and type(event.get('bytes')) is int and event['bytes']>0 and type(event.get('fd')) is int and event['fd']>=0 and event.get('phase')=='after-classification-mutant']
            if not aliases:raise AssertionError('分類後のhardlink本文操作の実体・fd・量を検出していません')
    if reject is not None and value.get('detected') is not True:raise AssertionError('意図的な監査違反を検出していません')
    if reject=='extra-git':
        calls=git_calls(value['events'],b'<unused-root>')
        if not any(args[-3:]==(b'diff',b'--name-only',b'HEAD') for _,args in calls):raise AssertionError('余分なGit実行の証拠が欠落しています')


def legacy_calls(case_id,root,fixture_type,legacy):
    fixture=fixture_type(root)
    fixture.git('rm','--','seed.txt');(fixture.repo/'public.txt').write_bytes(b'before\n')
    fixture.git('add','--','public.txt');fixture.git('commit','-q','-m','fixture')
    secret=fixture.repo/'.env';secret.write_bytes(b'S3D_TYPED_SECRET_ORIGINAL\n')
    fixture.git('add','--','.env');fixture.git('commit','-q','-m','secret fixture')
    if case_id.endswith('-modified'):secret.write_bytes(b'S3D_TYPED_SECRET_MODIFIED\n')
    (fixture.repo/'public.txt').write_bytes(b'after\n')
    if '-denied-' in case_id:secret.chmod(0)
    fixture.base='HEAD';fixture.out=fixture.repo/'audit.md';fixture.patch=fixture.repo/'audit.patch'
    real=Path(shutil.which('git')).resolve();metadata=real.stat();log=root/'git-calls.jsonl';bindir=root/'bin';bindir.mkdir()
    stub='#!'+sys.executable+'\nimport json,os,sys\n'
    stub+='real='+repr(str(real))+'\nlog='+repr(str(log))+'\n'
    stub+="value={'operation':'Spawn','path_hex':os.fsencode(real).hex(),'dev':"+str(metadata.st_dev)+",'ino':"+str(metadata.st_ino)+",'child_pid':os.getpid(),'arguments_hex':[b'git'.hex()]+[os.fsencode(v).hex() for v in sys.argv[1:]]}\n"
    stub+="fd=os.open(log,os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)\nos.write(fd,(json.dumps(value)+'\\n').encode());os.close(fd)\nos.execv(real,['git',*sys.argv[1:]])\n"
    entry=bindir/'git';entry.write_text(stub);entry.chmod(0o700);fixture.env['PATH']=str(bindir)+os.pathsep+fixture.env['PATH']
    before=fixture.input_state()
    try:
        value=run(['bash',str(legacy),*fixture.normal(),'--patch-out',str(fixture.patch)],cwd=fixture.root,env=fixture.env,report=fixture.out,patch=fixture.patch)
        fixed(value,0,stdout=b'',stderr=b'')
        for marker in (b'S3D_TYPED_SECRET_ORIGINAL',b'S3D_TYPED_SECRET_MODIFIED'):
            from .compare import no_secrets
            no_secrets(value,[marker])
        fixture.clean_outputs();unchanged(before,fixture.input_state())
        calls=[json.loads(line) for line in log.read_bytes().splitlines()]
        return git_calls(calls,os.fsencode(fixture.repo))
    finally:secret.chmod(0o600)


def run_cases(*,fixture_type,legacy,typed_observations):
    exact_ids(ids(),[value['case_id'] for value in typed_observations]);done=[]
    with tempfile.TemporaryDirectory(prefix='s3d-typed-audit-') as temporary:
        for value in typed_observations:
            case_id=value['case_id']
            if '-reject-' in case_id:validate_secret(value,reject=case_id.rsplit('-',1)[1] if not case_id.endswith('extra-git') else 'extra-git')
            else:
                validate_secret(value)
                if value.get('code')!=0:raise AssertionError('正常監査の終了値が違います')
                root=Path(temporary)/case_id;root.mkdir()
                try:old=legacy_calls(case_id,root,fixture_type,legacy)
                except Exception as error:raise AssertionError(f'{case_id}: {error}') from error
                new=git_calls(value['events'],bytes.fromhex(value['root_hex']))
                if old!=new:raise AssertionError(f'{case_id}: 型監査のGit実行列が旧版と一致しません')
            done.append(case_id)
    return done
