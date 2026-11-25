"""profileの用途別受理、順序付き和集合、codec、再帰を旧実入口で比べる。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile

from . import closed_env
from .compare import exact_ids, fixed, run, same_result, unchanged
from .diagnostics import compare_surrogate, compare_unknown_codec, INTERNAL
from . import recursion_inputs
from . import utf8_fixed

HERE = Path(__file__).parent
PURE = HERE.parents[1]/'crates/core/src/s3d_profile'


def extra_recursion():
    known={case['id'] for case in json.loads((HERE/'recursion.json').read_text())['cases']}
    value=json.loads((PURE/'recursion_cases.json').read_text())
    if value['python']!='3.10.11' or value['yaml']!='6.0.3' or len(value['cases'])!=421:
        raise AssertionError('再帰境界の旧helper測定条件が違います')
    helper=HERE.parents[2]/'plugins/dev-workflow/skills/do-task/scripts/secret-profiles.py'
    if hashlib.sha256(helper.read_bytes()).hexdigest()!=value['helper_sha256']:
        raise AssertionError('再帰境界の旧helperソースが違います')
    return [dict(case,kind='recursion-extra') for case in value['cases'] if case['id'] not in known]


def block_scalar_cases():
    value=json.loads((PURE/'block_scalar_cases.json').read_text())
    helper=HERE.parents[2]/'plugins/dev-workflow/skills/do-task/scripts/secret-profiles.py'
    if value['python']!='3.10.11' or value['pyyaml']!='6.0.3' or len(value['cases'])!=126 or hashlib.sha256(helper.read_bytes()).hexdigest()!=value['helper_sha256']:
        raise AssertionError('block scalarの旧helper測定条件が違います')
    return [dict(id=case['id'],kind='yaml',raw_hex=case['raw'].encode().hex(),expected=0 if case['expected'] is None else 20)
            for case in value['cases']]
YAML_CASES = (
    ('empty', b'', 0), ('comment', b'# empty\n', 0), ('null', b'null\n', 0),
    ('tilde', b'~\n', 0), ('empty-document', b'---\n', 0), ('map', b'{}\n', 0),
    ('absent-key', b'other: value\n', 0), ('empty-list', b'secret_paths: []\n', 0),
    ('normal', 'secret_paths: [secret.key, 日本語]\n'.encode(), 0),
    ('sequence', b'[]\n', 20), ('integer', b'1\n', 20), ('scalar', b'hello\n', 20),
    ('key-null', b'secret_paths: null\n', 20), ('key-map', b'secret_paths: {}\n', 20),
    ('key-string', b'secret_paths: secret.key\n', 20),
    ('key-empty-string', b'secret_paths: ""\n', 20),
    ('item-null', b'secret_paths: [null]\n', 20), ('item-integer', b'secret_paths: [1]\n', 20),
    ('item-bool', b'secret_paths: [true]\n', 20), ('item-map', b'secret_paths: [{}]\n', 20),
    ('item-list', b'secret_paths: [[]]\n', 20), ('item-empty', b'secret_paths: [""]\n', 20),
    ('bad-glob', b'secret_paths: ["a[b]"]\n', 20),
    ('allpath', b'secret_paths: ["**"]\n', 2),
    ('type-before-glob', b'secret_paths: ["a[b]", 1]\n', 20),
    ('merge', b'base: &base {secret_paths: [secret.key]}\n<<: *base\n', 0),
    ('merge-override', b'base: &base {secret_paths: null}\n<<: *base\nsecret_paths: [secret.key]\n', 0),
    ('duplicate', b'secret_paths: []\nsecret_paths: []\n', 20),
    ('unused-duplicate', b'unused: {a: 1, a: 2}\n', 0),
    ('unknown-tag', b'unused: !unknown value\n', 20),
    ('invalid-utf8', b'secret_paths: [bad\xff]\n', 20),
    ('two-documents', b'---\n{}\n---\n{}\n', 20),
    ('cycle', b'secret_paths: &a [*a]\n', 20),
    ('unused-cycle', b'unused: &a [*a]\n', 0),
    ('str-constructor', b'secret_paths: [!!str {=: secret.key}]\n', 0),
)
ENV_CASES = (
    ('utf8-empty', {'PYTHONIOENCODING': ''}), ('utf8-errors', {'PYTHONIOENCODING': ':strict'}),
    ('utf8-mode-0', {'PYTHONUTF8': '0'}), ('utf8-mode-1', {'PYTHONUTF8': '1'}),
    ('utf8-mode-empty', {'PYTHONUTF8': ''}), ('utf8-mode-invalid', {'PYTHONUTF8': '2'}),
    ('utf8-mode-letters', {'PYTHONUTF8': 'bad'}),
    ('ascii-strict', {'PYTHONIOENCODING': 'ascii:strict'}),
    ('ascii-ignore', {'PYTHONIOENCODING': 'ascii:ignore'}),
    ('utf8-invalid-errors', {'PYTHONIOENCODING': 'utf-8:no-such-handler'}),
    ('alias-utf8', {'PYTHONIOENCODING': 'UTF_8'}),
    ('alias-sjis', {'PYTHONIOENCODING': 's_jis'}),
    ('alias-cp932', {'PYTHONIOENCODING': 'MS_Kanji'}),
    ('alias-latin1', {'PYTHONIOENCODING': 'latin1'}),
    ('locale-empty', {'LC_ALL': '', 'LC_CTYPE': 'C'}),
    ('locale-ctype', {'LC_ALL': '', 'LC_CTYPE': 'C', 'PYTHONUTF8': '0'}),
    ('coerce-off', {'PYTHONCOERCECLOCALE': '0'}),
    ('coerce-off-ascii', {'PYTHONCOERCECLOCALE': '0', 'PYTHONUTF8': '0'}),
    ('coerce-warn', {'PYTHONCOERCECLOCALE': 'warn'}),
    ('coerce-warn-ascii', {'PYTHONCOERCECLOCALE': 'warn', 'PYTHONUTF8': '0'}),
    ('coerce-on-ascii', {'PYTHONCOERCECLOCALE': '1', 'PYTHONUTF8': '0'}),
    ('errors-only-ascii', {'PYTHONIOENCODING': ':strict', 'PYTHONUTF8': '0'}),
    ('warn-utf16', {'PYTHONCOERCECLOCALE': 'warn', 'PYTHONIOENCODING': 'utf-16'}),
    ('warn-utf16le', {'PYTHONCOERCECLOCALE': 'warn', 'PYTHONIOENCODING': 'utf-16-le'}),
    ('warn-punycode', {'PYTHONCOERCECLOCALE': 'warn', 'PYTHONIOENCODING': 'punycode'}),
    ('warn-utf7', {'PYTHONCOERCECLOCALE': 'warn', 'PYTHONIOENCODING': 'utf-7'}),
)


def cases():
    result = []
    def add(case_id, kind, **kwargs):
        result.append(dict(id=case_id, kind=kind, **kwargs))
    for name, raw, expected in YAML_CASES:
        add('yaml-' + name, 'yaml', raw_hex=raw.hex(), expected=expected)
    result.extend(block_scalar_cases())
    for name in json.loads((HERE/'codecs.json').read_text())['names']:
        for error in (False, True):
            add(f'codec-{name}-{int(error)}', 'codec', env={'PYTHONIOENCODING': name}, error=error)
    for name, env in ENV_CASES:
        for error in (False, True):
            add(f'env-{name}-{int(error)}', 'codec', env=env, error=error)
    for number,raw in enumerate((b'secret_paths: ["\\uD800"]\n', b'secret_paths: ["a\\uDC00"]\n',
                b'secret_paths: ["\\uD800\\uDFFF"]\n', b'secret_paths: ["safe", "\\uDBFF"]\n')):
        add(f'surrogate-{number+1:02d}', 'surrogate', raw_hex=raw.hex(), expected=20)
    for name in ('does-not-exist-s3d', 'utf-1000'):
        add('unknown-codec-' + name, 'unknown-codec', env={'PYTHONIOENCODING': name}, expected=20)
    for value in json.loads((HERE/'recursion.json').read_text())['cases']:
        add(value['id'], 'recursion', shape=value['kind'], **{k:v for k,v in value.items() if k not in ('id','kind')})
    result.extend(extra_recursion())
    for algorithm in ('sha1', 'sha256'):
        for kind in ('valid', 'empty-tree', 'short', 'upper', 'head', 'blob', 'unresolved'):
            add(f'oid-{algorithm}-{kind}', 'oid', algorithm=algorithm, oid_kind=kind,
                expected=0 if kind in ('valid', 'empty-tree') else 20)
    for kind in ('match', 'mismatch', 'reversed', 'duplicate-ref', 'no-ref', 'empty', 'uppercase', 'short', 'long', 'duplicate-hash'):
        add('union-' + kind, 'union', union_kind=kind)
    for kind in ('socket', 'permission', 'parent-file', 'profile-unexcluded', 'unapproved-before-profile'):
        add('profile-extra-' + kind, 'filesystem', filesystem_kind=kind)
    return result


def paired(fixture, binary, args, *, legacy, expected, env=None, compare=None):
    values=[]
    for command in (['bash', str(legacy)], [str(binary)]):
        fixture.clean_outputs()
        before=fixture.input_state()
        result=run([*command,*args],cwd=fixture.root,env=env or fixture.env,report=fixture.out,patch=fixture.patch)
        unchanged(before,fixture.input_state())
        fixed(result,expected)
        values.append(result)
    (compare or same_result)(*values)
    return values


def utf8_pair(case, fixture, binary, args, *, legacy, expected, env):
    """元の環境の旧版・参照の環境の旧版・元の環境のRustを動かし、印で期待を選ぶ。"""
    reference=utf8_fixed.reference_env(env)
    utf8_fixed.check_difference(env,reference)
    utf8_fixed.check_effective(reference)
    results=[]
    for command,environment in ((['bash',str(legacy)],env),(['bash',str(legacy)],reference),([str(binary)],env)):
        fixture.clean_outputs();before=fixture.input_state()
        results.append(run([*command,*args],cwd=fixture.root,env=environment,report=fixture.out,patch=fixture.patch))
        unchanged(before,fixture.input_state())
    old,ref,new=results
    closed_env.check_recorded_difference(fixture.root,-3,-2,utf8_fixed.SETTINGS)
    fixed(old,expected)
    utf8_fixed.same_with_thread(utf8_fixed.expectation(case['id'],old,ref),new,same_result)
    return old,new


def execute(case, fixture, binary, *, legacy):
    kind=case['kind']
    profile=fixture.repo/'.claude/project-profile.yml'
    profile.parent.mkdir(exist_ok=True)
    args=fixture.normal()+['--secret-profile-ref',fixture.base,'--patch-out',str(fixture.patch)]
    if kind in ('yaml','surrogate','recursion','recursion-extra','codec','unknown-codec'):
        if kind in ('yaml','surrogate'):
            raw=bytes.fromhex(case['raw_hex'])
        elif kind=='recursion':
            raw=getattr(recursion_inputs,case['generator'])(case['shape'],case['depth']).encode()
        elif kind=='recursion-extra':
            spec=importlib.util.spec_from_file_location('s3d_fixed_recursion',PURE/'recursion_inputs.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            raw=module.build(case['recipe']).encode()
            if hashlib.sha256(raw).hexdigest()!=case['sha256']:raise AssertionError('再帰境界の生成byteが固定値と違います')
        else:
            raw=b'secret_paths: null\n' if case.get('error') else 'secret_paths: [日本語]\n'.encode()
        profile.write_bytes(raw)
        env=dict(fixture.env,**case.get('env',{}))
        expected=case.get('expected',20 if case.get('error') or env.get('PYTHONUTF8') in ('2','bad') else 0)
        if kind=='recursion-extra':expected=0 if case['expected'] is None else 20
        comparator=None
        if kind=='surrogate':
            comparator=lambda a,b: compare_surrogate(a,b,helper=legacy.with_name('secret-profiles.py'))
        if utf8_fixed.has_settings(env):
            return utf8_pair(case,fixture,binary,args,legacy=legacy,expected=expected,env=env)
        values=paired(fixture,binary,args,legacy=legacy,expected=expected,env=env,compare=comparator)
        if kind=='recursion' and case['stderr']:
            fixed(values[0],expected,stderr=case['stderr'].encode()+INTERNAL)
        if kind=='recursion-extra' and case['expected']:
            reason={'NotMapping':'現在 の profile は map でなければならない',
                    'UnreadableYaml':'現在 の profile YAML を読めない'}[case['expected']]
            fixed(values[0],20,stderr=('ERROR [secret-profile] '+reason+'\n').encode()+INTERNAL)
        return
    if kind=='oid':
        if case['algorithm']=='sha256':
            # 新しいscratchに作り直す。リポジトリ設定の変更ではない。
            import shutil
            shutil.rmtree(fixture.repo/'.git')
            fixture.git('init','-q','--object-format=sha256')
            fixture.git('add','seed.txt');fixture.git('commit','-q','-m','seed')
            fixture.base=fixture.git('rev-parse','HEAD').strip().decode()
        oid=fixture.base;k=case['oid_kind']
        if k=='empty-tree':oid=fixture.git('hash-object','-t','tree','/dev/null').strip().decode()
        elif k=='short':oid=oid[:10]
        elif k=='upper':oid=oid.upper()
        elif k=='head':oid='HEAD'
        elif k=='unresolved':oid='f'*len(oid)
        elif k=='blob':oid=fixture.git('hash-object','-w','--stdin',data=b'not commit').strip().decode()
        paired(fixture,binary,fixture.normal()+['--secret-profile-ref',oid],legacy=legacy,expected=case['expected'])
        return
    if kind=='union':
        marker=b'S3D_PROFILE_UNION_SECRET_198ab'
        for name in ('historic.key','start.key','shared.key','.env.example'):
            (fixture.repo/name).write_bytes(marker+b'-before\n')
            fixture.git('add','--',name)
        refs=[]
        for names in (['historic.key','shared.key'],['start.key','shared.key']):
            profile.write_text('secret_paths: '+json.dumps(names)+'\n')
            fixture.git('add','.claude/project-profile.yml');fixture.git('commit','-q','-m','profile')
            refs.append(fixture.git('rev-parse','HEAD').strip().decode())
        fixture.base=refs[0]
        profile.write_text('secret_paths: [current.key, shared.key]\n')
        names=['.env','.env.*','.dev.vars','current.key','shared.key','historic.key','start.key']
        digest=hashlib.sha256(b''.join(x.encode()+b'\0' for x in names)).hexdigest()
        k=case['union_kind'];expected=0
        if k=='reversed':refs.reverse();expected=20
        elif k=='duplicate-ref':refs.append(refs[0])
        elif k=='mismatch':digest='0'*64;expected=20
        elif k=='no-ref':refs=[];expected=2
        elif k=='empty':digest='';expected=2
        elif k=='uppercase':digest=digest.upper();expected=2
        elif k=='short':digest=digest[:-1];expected=2
        elif k=='long':digest+='0';expected=2
        args=fixture.normal()+sum((['--secret-profile-ref',ref] for ref in refs),[])+['--secret-profile-union-sha256',digest,'--patch-out',str(fixture.patch),'--patch-base',fixture.base]
        if k=='duplicate-hash':args+=['--secret-profile-union-sha256',digest];expected=2
        for name in names:
            if name.startswith('.'):continue
            (fixture.repo/name).write_bytes(marker+b'\n')
        (fixture.repo/'historic.key').unlink()
        (fixture.repo/'.env.example').write_bytes(marker+b'-template\n')
        (fixture.repo/'seed.txt').write_bytes(b'public union patch\n')
        values=paired(fixture,binary,args,legacy=legacy,expected=expected)
        from .compare import no_secrets
        for value in values:no_secrets(value,[marker])
        if expected==0:
            import subprocess
            target=fixture.root/'patch-apply'
            done=subprocess.run(['git','-c','core.hooksPath=/dev/null','clone','--quiet','--no-hardlinks',str(fixture.repo),str(target)],env=fixture.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
            if done.returncode:raise AssertionError('patch適用用のscratch cloneを作れません')
            for args in (['checkout','--quiet',fixture.base],['apply','--binary',str(fixture.patch)]):
                done=subprocess.run(['git','-c','core.hooksPath=/dev/null','-C',str(target),*args],env=fixture.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
                if done.returncode:raise AssertionError('和集合で除外したpatchを基準へ適用できません')
            if (target/'seed.txt').read_bytes()!=b'public union patch\n':raise AssertionError('patchの公開本文が一致しません')
            for name in ('historic.key','start.key','shared.key','.env.example'):
                if (target/name).read_bytes()!=marker+b'-before\n':raise AssertionError('patchが機密の基準本文を変更しました')
        return
    if kind=='filesystem':
        k=case['filesystem_kind'];expected=20;stream=None
        if k=='socket':
            # AF_UNIXの名前は108byteまで。長い一時フォルダでも作れるよう、親へ移って相対名で作る。
            stream=socket.socket(socket.AF_UNIX);here=os.getcwd()
            try:os.chdir(profile.parent);stream.bind(profile.name)
            finally:os.chdir(here)
        elif k=='permission':profile.write_bytes(b'{}\n');profile.chmod(0)
        elif k=='parent-file':profile.parent.rmdir();profile.parent.write_bytes(b'file\n')
        elif k=='profile-unexcluded':
            profile.write_bytes(b'secret_paths: [.claude/project-profile.yml]\n');expected=0
        elif k=='unapproved-before-profile':
            profile.write_bytes(b'invalid yaml [\n');fixture.git('config','filter.hiding.clean','cat');expected=22
        try:
            values=paired(fixture,binary,args,legacy=legacy,expected=expected)
            if k=='profile-unexcluded':
                for value in values:
                    if b'secret_paths:' not in value.report:raise AssertionError('profile自身が機密除外されました')
            if k=='unapproved-before-profile':
                for value in values:
                    if b'secret-profile' in value.stderr:raise AssertionError('未承認でprofileを読みました')
        finally:
            if stream:stream.close()
            if k=='permission':profile.chmod(0o600)
        return
    raise AssertionError('未実装の追加profile入力です')


def run_cases(binary, *, fixture_type, legacy, selected=None):
    all_cases=cases();selected=all_cases if selected is None else selected
    done=[]
    with tempfile.TemporaryDirectory(prefix='s3d-profile-extra-') as temporary:
        for case in selected:
            root=Path(temporary)/case['id'];root.mkdir()
            try:execute(case,fixture_type(root),binary,legacy=legacy)
            except Exception as error:raise AssertionError(f'{case["id"]}: {type(error).__name__}: {error}') from error
            done.append(case['id'])
    exact_ids([case['id'] for case in selected],done)
    return done
