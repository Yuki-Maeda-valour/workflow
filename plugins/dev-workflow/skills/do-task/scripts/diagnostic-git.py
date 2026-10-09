#!/usr/bin/env python3
"""設定を戻さずに使う選択範囲の読取り専用診断。"""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode=True

ROOT=Path(__file__).resolve().parent

def load_module(name):
    path=ROOT/(name+'.py')
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),path)
    module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
    return module

# 信頼する同梱コピーの不足は、repo/stateへ触る前に固定して停止する。
# 監督runtime又は送出workerが無い場合は、別の同期出力経路を作らない。
R=None; OUTPUT_READY=False; BOOTSTRAP_FAILED=False
try:
    R=load_module('diagnostic-runtime')
except Exception:
    BOOTSTRAP_FAILED=True
if R is not None:
    try:
        load_module('diagnostic-output'); OUTPUT_READY=True
        S=load_module('diagnostic-state'); S.configure(R)
        P=load_module('diagnostic-plan'); P.configure(R,load_module('secret-profiles'))
        D=load_module('diagnostic-private'); D.configure(R,load_module('implementation-git').SAFE_GIT)
        X=load_module('diagnostic-paths'); X.configure(R)
        D.Private.cfield=staticmethod(S.cfield)
        D.Private.reftable=load_module('diagnostic-reftable')
        V=load_module('diagnostic-views'); V.configure(R,X)
    except Exception:
        BOOTSTRAP_FAILED=True

class Parser(argparse.ArgumentParser):
    def error(self,message): R.fail('arguments',2)
class Once(argparse.Action):
    def __call__(self,parser,namespace,values,option_string=None):
        seen=getattr(namespace,'_seen',set())
        if self.dest in seen: R.fail('duplicate-argument',2)
        seen.add(self.dest); namespace._seen=seen
        setattr(namespace,self.dest,True if self.nargs==0 else values)

def option(p,name,**kw): p.add_argument(name,action=Once,**kw)

def parser_input(argv):
    p=Parser(add_help=False,allow_abbrev=False)
    modes=p.add_subparsers(dest='mode',parser_class=lambda **kw:Parser(add_help=False,allow_abbrev=False,**kw))
    initial=modes.add_parser('plan'); extend=modes.add_parser('extend-plan'); run=modes.add_parser('run')
    for e in (initial,extend,run):
        for name in ('cwd','state','manifest-sha256','snapshot-sha256'): option(e,'--'+name,required=True)
        option(e,'--context-id',required=True,type=int)
    for e in (extend,run):
        option(e,'--plan',required=True); option(e,'--plan-sha256',required=True)
    for e in (initial,extend): option(e,'--out',required=True)
    for name in ('base-ref','start-ref','start-kind','branch-ref','exclude-root'): option(initial,'--'+name)
    option(initial,'--context-start',nargs=0,default=False); option(initial,'--carry-plan',nargs=2)
    for name in ('profile-ref','exclude-glob','exclude'): initial.add_argument('--'+name,action='append',default=[])
    ops=extend.add_subparsers(dest='operation',parser_class=lambda **kw:Parser(add_help=False,allow_abbrev=False,**kw))
    for op in ('status','files','diff','show','log','refs','stashes'):
        e=ops.add_parser(op)
        if op in ('status','files','diff','show','log'):
            for n in ('path','under'): e.add_argument('--'+n,action='append',default=[])
        if op=='status': option(e,'--untracked',choices=('no','names'),default='names')
        if op=='files': option(e,'--kind',choices=('tracked','untracked','all'),default='tracked')
        if op=='diff':
            option(e,'--left',default='index'); option(e,'--right',default='worktree'); option(e,'--format',choices=('records','patch'),default='records'); option(e,'--context',type=int,default=3)
        if op=='show': option(e,'--rev',default='HEAD'); option(e,'--format',choices=('records','patch','blob'),default='patch')
        if op=='log': option(e,'--to',default='HEAD'); option(e,'--format',choices=('oneline','fuller'),default='oneline')
        if op in ('log','stashes'): option(e,'--limit',type=int,default=20)
    normalized=[]; i=0
    while i<len(argv):
        if argv[i] in ('--path','--under') and i+1<len(argv) and argv[i+1].startswith('-') and not argv[i+1].startswith('--'):
            normalized.append(argv[i]+'='+argv[i+1]); i+=2
        else: normalized.append(argv[i]); i+=1
    a=p.parse_args(normalized)
    if a.mode is None or (a.mode=='extend-plan' and a.operation is None): R.fail('arguments',2)
    for n in ('manifest_sha256','snapshot_sha256','plan_sha256'):
        value=getattr(a,n,None)
        if value is not None and not P.hex64(value): R.fail('hash',2)
    if a.context_id<0: R.fail('context-id',2)
    for n in ('cwd','state','out','plan'):
        value=getattr(a,n,None)
        if value is not None and (not value or '\0' in value): R.fail('path',2)
    if a.mode=='plan':
        if a.context_start:
            if any((a.base_ref,a.start_ref,a.start_kind,a.branch_ref)): R.fail('arguments',2)
        elif not a.base_ref or not a.start_ref or a.start_kind not in ('new-invocation','resume-invocation'): R.fail('arguments',2)
        if a.exclude_root not in (None,'management','context') or ((a.exclude or a.exclude_glob) and not a.exclude_root): R.fail('arguments',2)
        if a.exclude_root=='management' and a.context_id!=0: R.fail('arguments',2)
        if a.carry_plan and not P.hex64(a.carry_plan[1]): R.fail('hash',2)
        for value in a.exclude+a.exclude_glob:
            try: value.encode('utf-8','strict')
            except UnicodeError: R.fail('arguments',2)
    return a

OPTION_KEYS={'status':'path_b64 under_b64 untracked','files':'path_b64 under_b64 kind','diff':'path_b64 under_b64 left right format context',
             'show':'path_b64 under_b64 rev format','log':'path_b64 under_b64 to limit format','refs':'','stashes':'limit'}

def options(args):
    out={}
    for k in OPTION_KEYS[args.operation].split():
        if k in ('path_b64','under_b64'):
            values=getattr(args,k[:-4]); out[k]=list(dict.fromkeys(R.b64(X.literal(os.fsencode(v),k=='under_b64')) for v in values))
        else: out[k]=getattr(args,k)
    validate_options(args.operation,out)
    return out

def validate_options(op,o):
    if op not in OPTION_KEYS: R.fail('schema',2)
    R.keys(o,OPTION_KEYS[op])
    for k in ('path_b64','under_b64'):
        if k in o:
            if type(o[k]) is not list or any(type(x) is not str for x in o[k]) or len(o[k])!=len(set(o[k])): R.fail('schema',2)
            for x in o[k]: X.literal(R.unb64(x),k=='under_b64')
    count=len(o.get('path_b64',[]))+len(o.get('under_b64',[]))
    if count>4096 or (op in ('status','files','diff','show') and count==0): R.fail('scope',2)
    if 'limit' in o and (type(o['limit']) is not int or not 1<=o['limit']<=1000): R.fail('limit',2)
    if 'context' in o and (type(o['context']) is not int or not 0<=o['context']<=100): R.fail('context',2)
    if op=='status' and o['untracked'] not in ('no','names'): R.fail('schema',2)
    if op=='files' and o['kind'] not in ('tracked','untracked','all'): R.fail('schema',2)
    if op in ('diff','show','log') and o['format'] not in {'diff':('records','patch'),'show':('records','patch','blob'),'log':('oneline','fuller')}[op]: R.fail('schema',2)
    if op=='show' and o['format']=='blob' and (len(o['path_b64'])!=1 or o['under_b64']): R.fail('blob-scope',2)
    if op=='diff':
        if o['left']=='worktree' or o['left']==o['right']: R.fail('endpoints',2)
    for k in ('left','right','to','rev'):
        if k in o and o[k] not in ('index','worktree'): X.revision(o[k])

def request(args,pv):
    return resolve_request(args.operation,options(args),pv)

def resolve_request(operation,o,pv):
    resolved={}
    for k in ('left','right','to','rev'):
        if k in o and o[k] not in ('index','worktree'):
            resolved[k]=pv.oid(o[k],unborn=o[k]=='HEAD' and operation in ('status','diff','log'))
    if operation in ('status','files'): resolved['head']=pv.oid('HEAD',unborn=True)
    if operation=='show':
        raw=pv.git('rev-list','--parents','-n','1',resolved['rev'],limit=4096).split()
        resolved['parent']=raw[1].decode() if len(raw)>1 else None
    return {'operation':operation,'options':o,'resolved_revisions':resolved}

def validate_request(req,width):
    R.keys(req,'operation options resolved_revisions')
    if type(req['operation']) is not str: R.fail('schema',2)
    op=req['operation']; o=req['options']; validate_options(op,o)
    expected={k for k in ('left','right','to','rev') if k in o and o[k] not in ('index','worktree')}
    if op in ('status','files'): expected.add('head')
    if op=='show': expected.add('parent')
    if type(req['resolved_revisions']) is not dict or set(req['resolved_revisions'])!=expected: R.fail('schema',2)
    for key,value in req['resolved_revisions'].items():
        if value is None:
            if key!='parent' and key!='head' and not (o.get(key)=='HEAD' and op in ('diff','log')): R.fail('schema',2)
        elif type(value) is not str or not re.fullmatch('[0-9a-f]{%d}'%width,value): R.fail('schema',2)

if not BOOTSTRAP_FAILED:
    P.validate_request=validate_request

HELP='使用方法: diagnostic-git.py plan|extend-plan|run --cwd ROOT --state STATE --manifest-sha256 HEX --snapshot-sha256 HEX --context-id N ...\n設定を戻さず、保持した除外計画の範囲で読取り専用診断を行います。成功も実装再開の承認ではありません。\n'

def main(argv=None):
    if R is None or not OUTPUT_READY: return 20
    rt=None; code=20; raw=None
    try:
        rt=R.Runtime()
        if BOOTSTRAP_FAILED: R.fail('runtime-unavailable')
        if (sys.argv[1:] if argv is None else argv)==['--help']:
            raw=HELP.encode(); code=0
        else:
            a=parser_input(sys.argv[1:] if argv is None else argv)
            rt.check_api(); state=S.State(a,rt)
            rt.protected_roots=[state.path]+[c[k] for c in state.contexts for k in ('wt','gd','common') if c[k].startswith(b'/')]
            rt.check_runtime(); cache={}
            def private(cid):
                if cid not in cache: cache[cid]=D.Private(rt,state,state.contexts[cid])
                return cache[cid]
            if a.mode=='plan':
                plan=P.prepare_initial(a,state,private); X.Matcher(rt,state,plan,a.context_id)
                path,sha=P.publish(plan,a.out,state)
                result={'version':1,'status':'plan-created','plan_path_b64':R.b64(path),'plan_sha256':sha,'context_id':a.context_id,'state_binding':state.binding}
            elif a.mode=='extend-plan':
                # 入力plan/hash検査を、最初の私有Gitより前に行う。
                P.load(a.plan,a.plan_sha256,state)
                req=request(a,private(a.context_id)); plan=P.extend(a,state,private,req); X.Matcher(rt,state,plan,a.context_id)
                path,sha=P.publish(plan,a.out,state)
                result={'version':1,'status':'plan-extended','plan_path_b64':R.b64(path),'plan_sha256':sha,'context_id':a.context_id,'state_binding':state.binding,
                        'input_plan_sha256':a.plan_sha256,'effective_exclusions_sha256':P.effective(plan)}
            else:
                plan=P.load(a.plan,a.plan_sha256,state)
                if plan['prepared_request'] is None: R.fail('plan-unprepared',22)
                req=plan['prepared_request']; R.keys(req,'operation options resolved_revisions'); validate_options(req['operation'],req['options'])
                P.verify_prepared(plan,state,private,a.context_id)
                if resolve_request(req['operation'],req['options'],private(a.context_id))!=req: R.fail('preparation-stale',22)
                matcher=X.Matcher(rt,state,plan,a.context_id)
                data,warnings=V.observe(rt,state,private(a.context_id),matcher,req)
                result={'version':1,'status':'observed','operation':req['operation'],'context_id':a.context_id,'view_policy':'raw-selected-v1',
                        'scope':matcher.scope(req['options']),'data':data,'warnings':sorted(warnings|{'settings-not-applied'}),
                        'effective_exclusions_sha256':P.effective(plan),'plan_sha256':a.plan_sha256,'baseline_record_updated':False,'resume_authorized':False}
            raw=rt.response_bytes(result); state.origins(state.origin_bytes); rt.reader.verify(); rt.cleanup(); code=0
    except R.Failure as exc:
        code=exc.code; raw=('ERROR [diagnostic:%s] 診断を完了できません。\n'%exc.reason).encode()
    except (OSError,ValueError,UnicodeError):
        code=20; raw=b'ERROR [diagnostic:io] '+ '診断の入出力を完了できません。\n'.encode()
    except Exception:
        code=20; raw=b'ERROR [diagnostic:internal] '+ '診断を完了できません。\n'.encode()
    if rt is None: return 20
    if code:
        try: rt.cleanup()
        except BaseException:
            code=20
            raw+=b'TEMP_PATH_B64='+R.b64(rt.temp).encode()+b'\n' if rt.temp else b'TEMP_PATH_UNKNOWN=1\n'
    return rt.emit(raw,code,str(ROOT/'diagnostic-output.py'))

if __name__=='__main__':
    raise SystemExit(main())
