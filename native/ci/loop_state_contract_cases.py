#!/usr/bin/env python3
"""S2L の値・例外・パス・出力故障を旧CLIと直接照合する。"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import check_loop_state as check
import loop_state_arguments as arguments

BASE={'schema':7,'refs':[],'worktrees':[],'worktree_contents':[],'worktree_configs':[],'secret_patterns':[],'stop_control':None,'config':{},'common_admin':{},'common_config':{},'common_config_worktree':{},'repo_admin':{}}
DERIVE={'schema':7,'worktrees':[{'worktree':'/removed'},{'worktree':'/retained'}],'worktree_contents':[{'path':'/removed'},{'path':'/retained'}],'worktree_configs':[{'path':'/removed'},{'path':'/retained'}],'secret_patterns':[{'path':'/removed'},{'path':'/retained'}]}
def canonical(value):return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode('ascii')

def invoke(command,env,cwd,out_route='pipe',err_route='pipe'):
    descriptors=[]
    def target(route):
        if route=='pipe':return subprocess.PIPE
        if route=='full':fd=os.open('/dev/full',os.O_WRONLY)
        elif route=='closed-pipe':
            reader,fd=os.pipe();os.close(reader)
        else:raise AssertionError(route)
        descriptors.append(fd);return fd
    child=None
    check.verify_child_environment(env,{})
    try:
        child=subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=target(out_route),stderr=target(err_route),start_new_session=True)
        for fd in descriptors:os.close(fd)
        descriptors.clear()
        stdout,stderr=child.communicate(timeout=8)
        return subprocess.CompletedProcess(command,child.returncode,stdout,stderr)
    finally:
        for fd in descriptors:os.close(fd)
        if child is not None and child.poll() is None:
            os.killpg(child.pid,signal.SIGKILL);child.communicate(timeout=3)

class Pairs:
    def __init__(self,fixture,work):self.fixture=fixture;self.work=work;self.count=0;self.commands=set();self.groups={};self.boundaries=[];self.env=arguments.environment()
    def run(self,label,argv,expected=None,output=None,existing=None,delta=None,group='values',out_route='pipe',err_route='pipe'):
        env=self.env.copy()
        for key,value in (delta or {}).items():
            key=os.fsencode(key)
            if value is None:env.pop(key,None)
            else:env[key]=os.fsencode(value)
        # case が与えた変数が子の環境に届くことを確かめる(消えれば失敗)。
        check.verify_child_environment(env,{k:v for k,v in (delta or {}).items() if k.startswith('PYTHON')})
        initial=None if existing is None else ('file',0o640,existing)
        check.restore_output(output,initial)
        old=invoke([sys.executable,'-B',str(check.LEGACY),*map(os.fspath,argv)],env,self.work,out_route,err_route)
        old_state=check.output_state(output)
        check.restore_output(output,initial)
        new=invoke([str(self.fixture),*map(os.fspath,argv)],env,self.work,out_route,err_route)
        check.compare_result(label,old,new)
        actual=check.output_state(output)
        if actual!=old_state:raise check.Mismatch(f'CONTRACT_STATE: {label}: {old_state!r} != {actual!r}')
        if expected is not None and old.returncode!=expected:raise AssertionError(f'CONTRACT_FIXED_EXIT: {label}: {old.returncode} != {expected}: stderr={old.stderr[-600:]!r}')
        self.count+=1;self.commands.add(argv[0]);self.groups[group]=self.groups.get(group,0)+1
        return new,actual

def nested(n,leaf):return '['*n+leaf+']'*n

def json_boundaries(pair):
    cases=[]
    def add(name,command,n,leaf,kind='schema7',existing=False):cases.append((name,command,n,leaf,kind,existing))
    for n in (991,992):add(f'load-stop-null-{n}','stop-control',n,'null')
    for n in (990,991):add(f'load-snapshot-null-{n}','snapshot',n,'null','schema0')
    for n in (990,991):add(f'load-stop-nan-{n}','stop-control',n,'NaN')
    add('load-stop-empty-object-991','stop-control',991,'{}')
    for n in (988,989,990,991):add(f'invalid-escape-{n}','stop-control',n,'"\\x"')
    add('invalid-unknown-token-991','stop-control',991,'x');add('invalid-delimiter-988','stop-control',988,'[0 0]')
    add('dump-two-keys-989','derive-removed-worktree',989,'{"a":0,"b":0}','derive');add('dump-two-keys-990','derive-removed-worktree',990,'{"a":0,"b":0}','derive');add('dump-two-keys-990-existing','derive-removed-worktree',990,'{"a":0,"b":0}','derive',True);add('dump-one-key-990','derive-removed-worktree',990,'{"a":0}','derive')
    add('equal-null-991','compare',991,'null','equal');add('equal-two-keys-990','compare',990,'{"a":0,"b":0}','equal')
    assert len(cases)==19
    for name,command,n,leaf,kind,existing in cases:
        case=pair.work/name;case.mkdir();source=case/'state';out=case/'out'
        base=DERIVE if kind=='derive' else BASE if kind=='equal' else {'schema':0 if kind=='schema0' else 7}
        raw=canonical(base)[:-1]+b',"x":'+nested(n,leaf).encode()+b'}';source.write_bytes(raw);before=arguments.identity(source)
        if command=='stop-control':argv=[command,'--state',source]
        elif command=='snapshot':argv=[command,'--secret-patterns-from',source,'--out',out,'--top',case,'--common',case,'--repo-admin',case]
        elif command=='derive-removed-worktree':argv=[command,'--state',source,'--out',out,'--removed-worktree','/removed','--own-worktree','/removed']
        else:argv=[command,'--before',source,'--after',source]
        result,_=pair.run(name,argv,output=out,existing=b'old-out' if existing else None,group='json19')
        assert before==arguments.identity(source),'JSON_BOUNDARY_INPUT_CHANGED'
        if command=='derive-removed-worktree' and n==990 and 'two-keys' in name:assert result.returncode==1 and b'in comparison' in result.stderr,'JSON_DUMP_FIXED_RECURSION'
        assert not any(p.name.startswith('.out.tmp.')for p in case.iterdir()),'JSON_DUMP_TEMP_LEFT'

def structures(pair):
    types=[None,[],0,'x',True]
    for value in types:
        before=copy.deepcopy(BASE);after=copy.deepcopy(BASE);before['repo_admin']=value;after['repo_admin']=value;after['config']=['changed']
        left=pair.work/'before';right=pair.work/'after';left.write_bytes(canonical(before));right.write_bytes(canonical(after))
        result,_=pair.run('S01-'+type(value).__name__,['compare','--before',left,'--after',right],1,group='structure')
        assert result.stdout==b'' and b'AttributeError:' in result.stderr,'S01_ATTRIBUTE_ERROR'
    for key in ['worktrees','worktree_contents']:
        for value in [None,0,[{}],['x']]:
            before=copy.deepcopy(BASE);after=copy.deepcopy(BASE);before[key]=value;after['config']=['changed'];left.write_bytes(canonical(before));right.write_bytes(canonical(after))
            result,_=pair.run('S02-'+key+'-'+repr(value),['compare','--before',left,'--after',right,'--self-worktree','/self','--self-ref','refs/heads/self'],1,group='structure')
            assert result.stdout==b'' and any(t in result.stderr for t in [b'TypeError:',b'KeyError:']),'S02_PYTHON_EVALUATION'

def paths_and_surrogates(pair):
    source=pair.work/'control';repo=pair.work/'control-root';repo.mkdir();(repo/'link').symlink_to('absent');(repo/'cycle').symlink_to('cycle')
    for name,value in [('missing-parent',str(repo/'absent/../result')),('dangling',str(repo/'link/child')),('cycle',str(repo/'cycle/../result')),('worktree-nul',str(repo)+'\0x'),('worktree-surrogate',str(repo)+'\ud800')]:
        source.write_bytes(canonical({'schema':7,'stop_control':{'worktree':value,'rel':'stop','new_dirs':[]}}));pair.run('E07-'+name,['stop-control','--state',source],1 if name in ('worktree-nul','worktree-surrogate')else 0,group='paths')
    for rel in ['\ud800\ud801','a\ud800\ud801','\ud800\udcff','\udcff\ud800','\ud800a\ud801','rel\0value']:
        source.write_bytes(canonical({'schema':7,'stop_control':{'worktree':str(repo),'rel':rel,'new_dirs':[]}}));result,_=pair.run('E08-'+repr(rel),['stop-control','--state',source],0 if '\0' in rel or rel=='\ud800\udcff' else 1,group='paths');assert result.stdout.startswith(os.fsencode(repo)+b'\0'),'E08_PARTIAL_WORKTREE_PREFIX'
    good=repo/'good';good.write_bytes(b'physical bytes');pair.run('lexical-abspath',['state-digest','--state',str(repo/'missing/../good')],0,group='paths');pair.run('root-state',['state-digest','--state','/'],20,group='paths')
    out=pair.work/'derive-out'
    for removed in ['/removed/./x','/removed//x','//removed/x']:
        d={'schema':7,'worktrees':[{'worktree':removed}],'worktree_contents':[{'path':removed}],'worktree_configs':[{'path':removed}],'secret_patterns':[{'path':removed}]};source.write_bytes(canonical(d));pair.run('removed-spelling-'+removed,['derive-removed-worktree','--state',source,'--out',out,'--removed-worktree',removed,'--own-worktree',removed],20 if '/./' in removed or removed=='/removed//x' else 0,output=out,group='paths')

def io_cases(pair):
    data=pair.work/'data';data.write_bytes(b'\x00\xffhello\n');denied=pair.work/'denied.json';denied.write_bytes(b'{"schema":7,"stop_control":null}');denied.chmod(0);missing=pair.work/'missing';malformed=pair.work/'malformed';malformed.write_bytes(b'not json');control=pair.work/'io-control';control.write_bytes(canonical({'schema':7,'stop_control':{'worktree':str(pair.work),'rel':'bad-\ud800','new_dirs':[]}}))
    commands={'digest-success':['state-digest','--state',data],'digest-missing':['state-digest','--state',missing],'compare-missing':['compare','--before',missing,'--after',missing],'compare-invalid-json':['compare','--before',malformed,'--after',malformed],'digest-denied':['state-digest','--state',denied],'compare-denied':['compare','--before',denied,'--after',denied],'digests-mixed':['state-digests','--state',data,'--state',missing,'--state',data],'digests-denied':['state-digests','--state',data,'--state',denied,'--state',data],'stop-surrogate-partial':['stop-control','--state',control]}
    for label,argv in commands.items():pair.run('E-'+label,argv,group='io')
    routes=['closed-pipe']+(['full']if sys.platform.startswith('linux')else[])
    for unbuffered in [None,'0','1']:
        for route in routes:
            for name,out,err in [('digest-success',route,'pipe'),('digest-missing','pipe',route),('digests-mixed','pipe',route),('compare-missing',route,'pipe'),('digests-mixed',route,'pipe')]:pair.run(f'E03-E04-{name}-{route}-{unbuffered}',commands[name],delta={'PYTHONUNBUFFERED':unbuffered},group='io',out_route=out,err_route=err)
    for label,argv in [('digest',commands['digest-success']),('digests',['state-digests','--state',data]),('compare',commands['compare-invalid-json']),('digests-stop-before-overflow',['state-digests','--state',missing,'--state',data])]:
        result,_=pair.run('E05-'+label,[*argv,'--max-seconds','1'+'0'*400],1,group='io');assert result.stdout==b'' and b'OverflowError:' in result.stderr,'E05_NO_ACCUMULATED_STDOUT'
    for label,before,after,extra in [('normal-diff',BASE,{**BASE,'config':['changed']},[]),('self-failure',BASE,{**BASE,'config':['changed']},['--self-worktree','/self','--self-ref','refs/heads/self']),('required-missing',{k:v for k,v in BASE.items()if k!='repo_admin'},BASE,[]),('read-failure',None,BASE,[])]:
        left=pair.work/'left';right=pair.work/'right';right.write_bytes(canonical(after));left.unlink(missing_ok=True)
        if before is not None:left.write_bytes(canonical(before))
        for route in routes:pair.run('E06-'+label+'-'+route,['compare','--before',left,'--after',right,*extra],1,delta={'PYTHONUNBUFFERED':'1'},group='io',out_route=route)
    denied.chmod(0o600)

def run(fixture):
    start=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='s2l-contract-') as temporary:
        pair=Pairs(fixture,Path(temporary).resolve());json_boundaries(pair);structures(pair);paths_and_surrogates(pair);io_cases(pair);numeric(pair);repository_cases(pair)
        import loop_state_r1_cases as r1
        r1.compare_values(pair);r1.json_causes(pair);r1.walks(pair);r1.state_stops(pair)
        import loop_state_r2_cases as r2
        r2.compare_values(pair);r2.repositories(pair)
        return {'contract_cases':pair.count,'groups':pair.groups,'commands':sorted(pair.commands),'os_boundaries':pair.boundaries,'permission_body_reached':sys.platform!='darwin','r1_walk_semantics_reached':sys.platform!='darwin','r2_refs_semantics_reached':sys.platform!='darwin','skipped':0,'timeouts':0,'seconds':round(time.monotonic()-start,3)}

def repository_cases(pair):
    """E02/E09 と、使用箇所だけ構造を確認する C04 の旧固定78入力。"""
    folder=pair.work/'repositories';folder.mkdir();repo=folder/'repo';repo.mkdir()
    pair.env={k:v for k,v in pair.env.items()if not k.startswith(b'GIT_')}
    pair.env.update({b'HOME':os.fsencode(folder),b'XDG_CONFIG_HOME':os.fsencode(folder/'xdg'),b'GIT_CONFIG_GLOBAL':b'/dev/null',b'GIT_CONFIG_NOSYSTEM':b'1'})
    def initialize(path):
        for argv in [['-c','init.defaultBranch=main','init','-q'],['config','user.name','fixture'],['config','user.email','fixture@example.invalid']]:
            result=arguments.run(['git','-C',str(path),*argv],pair.env,folder);assert result.returncode==0,result.stderr
        (path/'tracked').write_bytes(b'baseline\n')
        for argv in [['add','tracked'],['commit','-qm','baseline']]:
            result=arguments.run(['git','-C',str(path),*argv],pair.env,folder);assert result.returncode==0,result.stderr
    initialize(repo);shared=['--top',repo,'--common',repo/'.git','--repo-admin',repo/'.git'];out=folder/'out';state=folder/'state'
    pilot=invoke([sys.executable,'-B',str(check.LEGACY),'snapshot','--out',str(out),*map(os.fspath,shared)],pair.env,folder)
    if sys.platform=='darwin':
        assert pilot.returncode==20 and not out.exists(),'MAC_PRIOR_PREPARATION_REFUSAL: exit='+repr(pilot.returncode)+' stderr='+ascii(pilot.stderr[-600:])
        # 旧の実候補を読み、後段のparserに届く保存入力だけを用意する。
        # snapshot成功の代用へ数えず、以後の両CLIの実拒否を比較する。
        script="""import importlib.util,json,sys
s=importlib.util.spec_from_file_location('s2l_prepare',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
t,c=sys.argv[2:];b=m.Budget(100000,10000000,1000000,60);q=m.config_candidates(t,c,c,b);e={'effective_sha256':'0'*64,'origins':[r['entry'] for r in q if r['state']=='present'],'candidates':q};v={'schema':7,'refs':[],'worktrees':[{'worktree':t,'branch':'refs/heads/main','locked':''}],'worktree_contents':[],'worktree_configs':[{'path':t,'git_dir':c,'effective':e}],'secret_patterns':[],'stop_control':None,'config':e,'common_admin':{},'common_config':{},'common_config_worktree':{},'repo_admin':{}};print(json.dumps(v))
"""
        prepared=arguments.run([sys.executable,'-B','-c',script,str(check.LEGACY),str(repo),str(repo/'.git')],pair.env,folder);assert prepared.returncode==0,prepared.stderr;baseline=json.loads(prepared.stdout)
    else:
        assert pilot.returncode==0 and out.is_file(),'PRIOR_PREPARATION_SUCCESS';baseline=json.loads(out.read_bytes())
    cases=[('baseline',baseline,None)]
    for key in ['config','worktree_configs','worktrees','common_admin','repo_admin','secret_patterns','worktree_contents']:
        for label,value in [('null',None),('integer',1),('string','bad'),('array',[{}])]:
            bad=copy.deepcopy(baseline);bad[key]=value;cases.append((key+'-'+label,bad,key))
    for key in ['origins','candidates']:
        for label,value in [('null',None),('integer',1),('string','bad'),('array',[None]),('row',[{}])]:
            bad=copy.deepcopy(baseline);bad['config'][key]=value;cases.append(('config-'+key+'-'+label,bad,'config'))
    assert len(cases)==39
    for label,value,key in cases:
        state.write_bytes(canonical(value));before=arguments.identity(state)
        for command in ['preflight','snapshot']:
            expected=20 if sys.platform=='darwin' or key in ('config','worktree_configs','worktrees') or key=='secret_patterns' and command=='snapshot' else 0
            argv=[command,*shared,*(['--state',state]if command=='preflight'else['--out',out,'--secret-patterns-from',state])]
            pair.run('C04-'+label+'-'+command,argv,expected,output=out,group='prior_structure78')
            assert arguments.identity(state)==before,'C04_PRIOR_INPUT_UNCHANGED'
    large=repo/'large';large.write_bytes(b'large\n');large.chmod(0)
    try:
        result,saved=pair.run('E02-snapshot-permission',['snapshot','--out',out,*shared],20 if sys.platform=='darwin' else 1,output=out,group='repository2')
        if sys.platform!='darwin':assert b'PermissionError:' in result.stderr and b"'large'" in result.stderr and b'in hash_at\n' in result.stderr,'E02_PERMISSION_CALL_CHAIN'
        assert saved is None,'E02_NO_OUTPUT'
    finally:large.chmod(0o600)
    raw_repo=folder/'repo-\udcff'
    # Mac の APFS は UTF-8 でない名前を作れない。そのときだけ E09 を飛ばし、行で出す(E09 に ASCII の対照は無い)。
    if not check.create_raw(raw_repo,Path.mkdir):
        pair.boundaries.append(check.os_boundary('E09-snapshot-bare-utf8','skipped','none'));return
    initialize(raw_repo)
    result,saved=pair.run('E09-snapshot-bare-utf8',['snapshot','--out',out,'--top',raw_repo,'--common',raw_repo/'.git','--repo-admin',raw_repo/'.git'],1,output=out,group='repository2')
    assert b'UnicodeDecodeError:' in result.stderr and b'0xff in position' in result.stderr and b'invalid start byte' in result.stderr and b'in snapshot\n' in result.stderr and saved is None,'E09_BARE_UTF8_DECODE'

def numeric_cases():
    def compare_raw(value, extra):
        b=dict(BASE); del b['stop_control']
        return (json.dumps(b,separators=(',',':'))[:-1]+',"stop_control":'+value+',"extra":'+str(extra)+'}').encode()
    
    def derive_raw(value):
        return (json.dumps(DERIVE,separators=(',',':'))[:-1]+',"numbers":'+value+'}').encode()
    
    cases=[]
    def cmp(name,left,right,expected,force=False):
        cases.append(dict(name=name,command='compare',inputs={'before':compare_raw(left,0),'after':compare_raw(right,1 if force else 0)},expected_returncode=expected,limit='4300',existing=False,description=dict(left=left,right=right,extra_diff=force)))
    def derive(name,value,limit='4300',expected=0,existing=True):
        cases.append(dict(name=name,command='derive-removed-worktree',inputs={'state':derive_raw(value)},expected_returncode=expected,limit=limit,existing=existing,description=dict(value=value)))
    def stop(name,limit,expected,argument=None):
        cases.append(dict(name=name,command='stop-control',inputs={'state':b'{"schema":7,"stop_control":null}'},expected_returncode=expected,limit=limit,existing=False,argument=argument,description=dict(max_items=argument)))
    
    cmp('compare-bool-int','true','1',0)
    cmp('compare-bool-negative-zero','false','-0.0',0)
    cmp('compare-array-numeric','[true,false,7,9007199254740992,1267650600228229401496703205376]','[1.0,0,7.0,9007199254740992.0,1.2676506002282294e30]',0)
    cmp('compare-object-numeric','{"a":true,"b":-7,"c":Infinity,"d":-Infinity}','{"d":-1e999,"c":1e999,"b":-7.0,"a":1}',0)
    cmp('compare-int-float-rounded-53','9007199254740993','9007199254740992.0',1)
    cmp('compare-int-float-rounded-100','1267650600228229401496703205377','1.2676506002282294e30',1)
    cmp('compare-nan-field','NaN','NaN',0)
    cmp('compare-nan-nested','[NaN,{"v":NaN}]','[NaN,{"v":NaN}]',0)
    cmp('compare-nan-direct-diagnostic','NaN','NaN',1,True)
    cmp('compare-nan-container-diagnostic','[NaN]','[NaN]',1,True)
    cmp('compare-nan-infinity','NaN','Infinity',1)
    cmp('compare-signed-zero','-0.0','0.0',0)
    float_values={
     'zero_positive':'0.0','zero_negative':'-0.0',
     'threshold_1em4_below':repr(math.nextafter(1e-4,0.0)),'threshold_1em4':'1e-4','threshold_1em4_above':repr(math.nextafter(1e-4,math.inf)),
     'threshold_1em5':'1e-5','threshold_1em5_negative':'-1e-5',
     'threshold_1e15_below':repr(math.nextafter(1e15,0.0)),'threshold_1e15':'1e15','threshold_1e15_above':repr(math.nextafter(1e15,math.inf)),
     'threshold_1e16_below':repr(math.nextafter(1e16,0.0)),'threshold_1e16':'1e16','threshold_1e16_above':repr(math.nextafter(1e16,math.inf)),
     'subnormal_min':'5e-324','subnormal_min_negative':'-5e-324','finite_max':'1.7976931348623157e308','finite_max_negative':'-1.7976931348623157e308',
     'infinity':'Infinity','infinity_negative':'-Infinity','nan':'NaN','overflow':'1e999','underflow_negative':'-1e-999',
     'rounding_shortest':'2.9802322387695312e-8','decimal_1e23':'1e23',
     'int_u64_before':'18446744073709551615','int_u64_after':'18446744073709551616','int_large_negative':'-1267650600228229401496703205377'}
    float_raw='{'+','.join(json.dumps(k)+':'+v for k,v in float_values.items())+'}'
    derive('derive-float-matrix-absent',float_raw,existing=False)
    derive('derive-float-matrix-existing',float_raw)
    for limit,digits,expected in [('640',640,0),('640',641,20),('4300',4300,0),('4300',4301,20),('0',4301,0)]:
        value='{"positive":'+('9'*digits)+',"negative":-'+('9'*digits)+'}'
        derive('derive-int-limit-'+limit+'-digits-'+str(digits),value,limit,expected)
    stop('startup-int-limit-one','1',1)
    stop('startup-int-limit-bad','bad',1)
    stop('argument-int-limit-640-accepted','640',0,'9'*640)
    stop('argument-int-limit-640-rejected','640',2,'9'*641)
    assert len(cases)==23, len(cases)
    return cases

def numeric(pair):
    for case in numeric_cases():
        folder=pair.work/case['name'];folder.mkdir();inputs={}
        for label,raw in case['inputs'].items():
            inputs[label]=folder/label;inputs[label].write_bytes(raw)
        output=folder/'out';command=case['command']
        if command=='compare':argv=[command,'--before',inputs['before'],'--after',inputs['after']]
        elif command=='derive-removed-worktree':argv=[command,'--state',inputs['state'],'--out',output,'--removed-worktree','/removed','--own-worktree','/removed']
        else:
            argv=[command,'--state',inputs['state']]
            if case.get('argument') is not None:argv+=['--max-items',case['argument']]
        baseline={key:arguments.identity(path)for key,path in inputs.items()}
        result,saved=pair.run(case['name'],argv,case['expected_returncode'],output=output,existing=b'original-output-sentinel\n' if case['existing']else None,delta={'PYTHONINTMAXSTRDIGITS':case['limit']},group='numeric23')
        assert baseline=={key:arguments.identity(path)for key,path in inputs.items()},'NUMERIC_INPUT_CHANGED'
        if case['name'].startswith('derive-float-matrix'):
            assert saved is not None and b'2.9802322387695312e-08' in saved[2], 'J02_ROUNDING_SHORTEST'
