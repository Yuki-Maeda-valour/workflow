#!/usr/bin/env python3
"""同じsnapshot CLIでTOP/linked双方のYAML境界を比較する。"""
from pathlib import Path
import json,os,subprocess,sys,tempfile,time
import loop_state_contract_cases as contracts
import loop_state_arguments as arguments

SUCCESS=set(['flow-plain-489', 'flow-lines-489', 'flow-second-489', 'flow-quoted-489', 'flow-empty-489', 'flow-mapping-489', 'flow-plain-490', 'flow-lines-490', 'flow-second-490', 'flow-quoted-490', 'flow-empty-490', 'flow-empty-491', 'flow-implicit-243', 'flow-implicit-244', 'block-map-489', 'block-seq-489', 'merge-987', 'bom', 'crlf', 'nel', 'directives', 'alias', 'anchor-tag', 'tag-anchor', 'blockscalar'])

def cases():
    cases=[]
    for n in [489,490,491]:
     for label,start,leaf,end in [('flow-plain','[','0',']'),('flow-lines','[\n','0','\n]'),('flow-second','[0,','0',']'),('flow-quoted','[','"q"',']'),('flow-empty','[','',']'),('flow-mapping','{a: ','0','}')]:cases.append((f'{label}-{n}','unused: '+start*n+leaf+end*n+'\n'))
    for n in [243,244,245,246,247]:cases.append((f'flow-implicit-{n}','unused: '+'[a: '*n+'0'+']'*n+'\n'))
    for n in [489,490,491]:
     for label,entry in [('block-map','a:'),('block-seq','-')]:cases.append((f'{label}-{n}','unused:\n'+''.join('  '*(i+1)+entry+'\n'for i in range(n))+'  '*(n+1)+'0\n'))
    for n in [987,988,1100]:cases.append((f'merge-{n}','a0: &a0 {other: ok}\n'+''.join(f'a{i}: &a{i} {{<<: *a{i-1}}}\n'for i in range(1,n+1))+f'<<: *a{n}\n'))
    for label,raw in [('bom','\ufeffunused: [x]\n'),('crlf','unused:\r\n  a: "x\\nq"\r\n'),('nel','unused:\u0085  a: x\u0085'),('directives','%YAML 1.1\n---\nunused: {a: b}\n...\n'),('alias','unused: &a [*a]\n'),('anchor-tag','unused: &a !!seq [0]\n'),('tag-anchor','unused: !!seq &a [0]\n'),('blockscalar','unused: >-\n  a\n  b\n')]:cases.append((label,raw))
    assert len(cases)==40
    return cases

def run(fixture):
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='s2l-yaml-cli-') as temporary:
        work=Path(temporary).resolve();repo=work/'repo';repo.mkdir();linked=work/'linked';pair=contracts.Pairs(fixture,work)
        pair.env={k:v for k,v in pair.env.items()if not k.startswith(b'GIT_')}
        pair.env.update({b'GIT_CONFIG_GLOBAL':b'/dev/null',b'GIT_CONFIG_NOSYSTEM':b'1',b'HOME':os.fsencode(work),b'XDG_CONFIG_HOME':os.fsencode(work/'xdg')})
        def git(*args):
            result=arguments.run(['git','-C',str(repo),*args],pair.env,work)
            assert result.returncode==0, (args,result.stderr)
        git('-c','init.defaultBranch=main','init','-q');(repo/'tracked').write_bytes(b'baseline\n');git('add','tracked');git('-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','baseline');git('worktree','add','-q','--detach',str(linked),'HEAD')
        profiles=[]
        for root in [repo,linked]:
            (root/'.claude').mkdir();profiles.append(root/'.claude/project-profile.yml')
        for target in range(2):
            for profile in profiles:profile.write_bytes(b'secret_paths: []\n')
            for label,text in cases():
                profile=profiles[target];profile.write_bytes(text.encode());baseline=[arguments.identity(p)for p in profiles];out=work/'out'
                expected=20 if sys.platform=='darwin' else 0 if label in SUCCESS else 20
                result,saved=pair.run(f'yaml-{target}-{label}',['snapshot','--out',out,'--top',repo,'--common',repo/'.git','--repo-admin',repo/'.git'],expected,output=out,group='yaml80')
                assert baseline==[arguments.identity(p)for p in profiles],'YAML_PROFILE_INPUT_CHANGED'
                if sys.platform=='darwin':assert result.stderr=='ERROR [loop-state] git の状態読取に失敗した\n'.encode() and saved is None,'MAC_YAML_UNREACHED_EXPECTATION'
                elif expected==0:
                    assert saved is not None and saved[1]==0o600 and json.loads(saved[2])['schema']==7,'YAML_STATE_CREATED'
                    if target==0 and label=='flow-plain-489':
                        old=work/'old-saved';new=work/'new-saved';old.write_bytes(saved[2]);new.write_bytes(out.read_bytes());pair.run('mutual-saved-json-read',['compare','--before',old,'--after',new],0,group='saved_interop')
                else:assert saved is None and result.stdout==b'','YAML_REJECTION_OUTPUT'
        assert pair.groups['yaml80']==80
        return {'yaml_cli':80,'top':40,'linked':40,'saved_interop':pair.groups.get('saved_interop',0),'success_semantics_reached':sys.platform!='darwin','skipped':0,'seconds':round(time.monotonic()-started,3)}
