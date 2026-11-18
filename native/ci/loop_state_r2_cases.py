#!/usr/bin/env python3
"""source review r2 の評価順・refs再帰・OS失敗位置を同じ実入力で照合する。"""
from __future__ import annotations
import copy
import os
from pathlib import Path
import sys
import check_loop_state as check
from loop_state_contract_cases import canonical
from loop_state_r1_cases import self_pair
import loop_state_arguments as arguments
from loop_state_deep_fixture import DeepTree


def compare_cases():
    cases=[]
    def add(label,before,after,exit=1,kind=None):
        cases.append((label,copy.deepcopy(before),copy.deepcopy(after),exit,kind))
    before,after=self_pair();add('normal',before,after,0)
    remote='refs/remotes/origin/self'
    for side in ['before','after']:
        for label,bad in [('null',None),('dict',{}),('integer',0),('list',[]),('string','bad')]:
            for position in ['missing','before-match','after-match']:
                left,right=self_pair();rows=(left if side=='before'else right)['refs']
                valid={'name':remote,'oid':'a'if side=='before'else'b','symref':''}
                rows.extend([bad]if position=='missing'else[bad,valid]if position=='before-match'else[valid,bad])
                # A matched remote stops next(), but later diagnostics may still
                # inspect malformed rows; use a matching row on both sides.
                other=right if side=='before'else left
                if position!='missing':other['refs'].append({'name':remote,'oid':'b'if side=='before'else'a','symref':''})
                if position=='after-match':other['refs'].append(copy.deepcopy(bad))
                add(side+'-remote-'+label+'-'+position,left,right,0 if position=='after-match'else 1,None if position=='after-match'else('KeyError'if bad=={}else'TypeError'))
    for label,side,key in [('before-head','before','HEAD'),('after-head','after','HEAD'),('before-index','before','index'),('before-self-oid','before','oid'),('before-remote-oid','before','remote-oid')]:
        for missing in [True,False]:
            left,right=self_pair();root=left if side=='before'else right
            if key=='HEAD':row=root['worktrees'][0]
            elif key=='index':row=root['worktree_contents'][0]
            elif key=='oid':row=root['refs'][0]
            else:
                left['refs'].append({'name':remote,'oid':'a'});right['refs'].append({'name':remote,'oid':'b'});row=left['refs'][-1];key='oid'
            if missing:del row[key]
            else:row[key]=None
            if label=='after-head':
                # get() in the earlier condition deliberately treats absent and
                # explicit null alike. Only the subsequent [] must distinguish.
                right['refs'][0].pop('oid')
            add(label+('-missing'if missing else'-null'),left,right,1 if missing else 0,'KeyError'if missing else None)
    for label in ['private-head','branch-no-advance','remote']:
        left,right=self_pair()
        if label=='private-head':right['worktree_configs'][0]['private_refs']=None
        elif label=='branch-no-advance':right['worktrees'][0]['HEAD']='a';right['refs'][0]['oid']='a'
        else:right['refs'].append({'name':remote,'oid':'wrong'})
        add('reject-'+label,left,right)
    # Earlier failure must prevent missing mandatory keys from being inspected.
    for key in ['HEAD','index']:
        left,right=self_pair();right['worktrees'][0]['branch']='refs/heads/other'
        if key=='HEAD':del left['worktrees'][0]['HEAD']
        else:del left['worktree_contents'][0]['index']
        add('short-circuit-'+key,left,right)
    return cases


def compare_values(pair):
    folder=pair.work/'r2-compare';folder.mkdir();left=folder/'before';right=folder/'after'
    for label,before,after,expected,kind in compare_cases():
        left.write_bytes(canonical(before));right.write_bytes(canonical(after));identities=[arguments.identity(p)for p in (left,right)]
        for route in ['pipe','closed-pipe']+(['full']if sys.platform.startswith('linux')else[]):
            result,_=pair.run('R2-'+label+'-'+route,['compare','--before',left,'--after',right,'--self-worktree','/self','--self-ref','refs/heads/self'],expected,delta={'PYTHONUNBUFFERED':'1'},out_route=route,group='r2_compare')
            if kind:assert (kind+':').encode()in result.stderr and result.stdout in (None,b''),'R2_REQUIRED_EVALUATION'
            if expected==0:assert result.stderr==b'','R2_NORMAL_SHORT_CIRCUIT'
        assert identities==[arguments.identity(p)for p in (left,right)],'R2_COMPARE_INPUT_CHANGED'


def repositories(pair):
    folder=pair.work/'r2-repository';folder.mkdir();repo=folder/'repo';repo.mkdir();previous=pair.env
    pair.env={k:v for k,v in previous.items()if not k.startswith(b'GIT_')}
    pair.env.update({b'HOME':os.fsencode(folder),b'XDG_CONFIG_HOME':os.fsencode(folder/'xdg'),b'GIT_CONFIG_GLOBAL':b'/dev/null',b'GIT_CONFIG_NOSYSTEM':b'1'})
    try:
        for tail in [['-c','init.defaultBranch=main','init','-q'],['-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','baseline']]:
            result=arguments.run(['git','-C',str(repo),*tail],pair.env,folder);assert result.returncode==0,result.stderr
        oid=arguments.run(['git','-C',str(repo),'rev-parse','HEAD'],pair.env,folder).stdout.strip()
        out=folder/'out';argv=['snapshot','--out',out,'--top',repo,'--common',repo/'.git','--repo-admin',repo/'.git']
        for kind in ['directory','file']:
            for name in ['denied','denied-\udcff']:
                path=repo/name
                create=Path.mkdir if kind=='directory' else lambda p:p.write_bytes(b'owned permission fixture')
                if name.isascii():create(path)
                elif not check.create_raw(path,create):
                    # Mac の APFS は UTF-8 でない名前を作れない。そのときだけ飛ばし、行で出す(ASCII の対照は denied)。
                    pair.boundaries.append(check.os_boundary('R2-denied-'+kind+'-raw','skipped','R2-denied-'+kind+'-'+ascii('denied')));continue
                before=arguments.identity(path)if kind=='file'else None
                path.chmod(0)
                try:
                    result,saved=pair.run('R2-denied-'+kind+'-'+ascii(name),argv,20 if sys.platform=='darwin'else 1,output=out,group='r2_permissions')
                    assert saved is None,'R2_PERMISSION_NO_SAVE'
                    if sys.platform!='darwin':
                        assert b'PermissionError:'in result.stderr and (b'line 493, in walk'if kind=='directory'else b'in hash_at\n')in result.stderr,'R2_PERMISSION_FRAME'
                        assert (b'denied-\\udcff'if '\udcff'in name else b'denied')in result.stderr,'R2_PERMISSION_RAW_NAME'
                finally:
                    path.chmod(0o700 if kind=='directory'else 0o600)
                    if before is not None:assert path.read_bytes()==b'owned permission fixture','R2_PERMISSION_BODY_CHANGED'
                    path.rmdir()if kind=='directory'else path.unlink()
        deep=DeepTree(repo/'.git/refs',1001)
        try:
            for leaf,depth,expected in [(False,991,0),(False,992,0),(False,993,1),(False,1000,1),(True,990,0),(True,991,0),(True,992,1),(True,993,1),(True,1000,1)]:
                held=folder/'held-tail';deep.detach(depth,held)
                try:
                    if leaf:deep.leaf(depth,oid+b'\n')
                    identities=deep.identity(depth,leaf)
                    for existing in [None,b'held output remains unchanged']:
                        result,saved=pair.run(f'R2-refs-{leaf}-{depth}-{existing is not None}',argv,20 if sys.platform=='darwin'else expected,output=out,existing=existing,group='r2_refs')
                        if sys.platform!='darwin'and expected:
                            assert result.stdout==b'' and b'RecursionError:'in result.stderr and b'in refs\n'in result.stderr,'R2_REFS_RECURSION'
                            assert (b'line 260, in read_at'in result.stderr)==(leaf and depth==992),'R2_REFS_READ_BOUNDARY'
                        if sys.platform=='darwin'or expected:assert saved==(None if existing is None else('file',0o640,existing)),'R2_REFS_NO_REPLACE'
                        else:assert saved is not None and saved[1]==0o600,'R2_REFS_SAVE_CREATED'
                    after=deep.identity(depth,leaf)
                    assert identities==after,'R2_REFS_INPUT_CHANGED'
                finally:
                    if leaf:deep.leaf(depth,None)
                    deep.restore(depth,held)
        finally:deep.close()
    finally:pair.env=previous
