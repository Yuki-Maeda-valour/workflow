"""除外計画の型、出典、単調追加と実行直前の指紋を扱う。"""
import copy
import json
import os
import re
import stat

R=None; P=None

def configure(runtime,profiles):
    global R,P
    R,P=runtime,profiles

PLAN_KEYS='version state_binding context_id management_binding parent_plan_sha256 prepared_request source_checks policies default_policy'
POLICY_KEYS='context_id root_kind root_prefix_b64 history_origin start_kind branch_provenance sources globs eres'
SOURCE_KEYS='role commit_oid profile_blob_oid profile_sha256 absent'
ROLES={'current','base','start','branch','held-head','extra','observed-current','observed-head','observed-ref'}

def effective(plan):
    keys=('context_id','root_kind','root_prefix_b64','globs','eres')
    policies=[{k:p[k] for k in keys} for p in sorted(plan['policies'],key=lambda p:(p['context_id'],R.unb64(p['root_prefix_b64'])))]
    return R.digest(json.dumps({'policies':policies,'default_policy':'snapshot-v1'},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8'))

def schema(plan,state):
    R.keys(plan,PLAN_KEYS)
    if type(plan['version']) is not int or plan['version']!=1 or type(plan['context_id']) is not int or plan['default_policy']!='snapshot-v1': R.fail('schema',2)
    R.keys(plan['state_binding'],'manifest_sha256 snapshot_sha256')
    R.keys(plan['management_binding'],'context_id relative_prefix_b64 identity_chain')
    if any(not hex64(v) for v in plan['state_binding'].values()): R.fail('schema',2)
    mb=plan['management_binding']
    if type(mb['context_id']) is not int or type(mb['identity_chain']) is not list: R.fail('schema',2)
    R.unb64(mb['relative_prefix_b64'])
    for entry in mb['identity_chain']:
        R.keys(entry,'component_b64 dev ino type'); R.unb64(entry['component_b64'])
        if entry['type']!='directory' or any(type(entry[k]) is not int for k in ('dev','ino')): R.fail('schema',2)
    if plan['state_binding']!=state.binding or plan['management_binding']!=state.management: R.fail('plan-binding',22)
    if not 0<=plan['context_id']<len(state.contexts): R.fail('plan-binding',22)
    if plan['parent_plan_sha256'] is not None and not hex64(plan['parent_plan_sha256']): R.fail('schema',2)
    if type(plan['policies']) is not list or type(plan['source_checks']) is not list: R.fail('schema',2)
    seen=set(); count=0; expr_bytes=0
    for p in plan['policies']:
        R.keys(p,POLICY_KEYS)
        if type(p['context_id']) is not int or not 0<=p['context_id']<len(state.contexts): R.fail('schema',2)
        prefix=R.unb64(p['root_prefix_b64']); key=(p['context_id'],prefix)
        if key in seen: R.fail('schema',2)
        seen.add(key)
        if p['root_kind']=='worktree':
            if prefix: R.fail('plan-binding',22)
        elif p['root_kind']=='management':
            if p['context_id']!=0 or not prefix or p['root_prefix_b64']!=state.management['relative_prefix_b64']: R.fail('plan-binding',22)
        else: R.fail('schema',2)
        if p['history_origin'] not in ('task','context-start') or p['start_kind'] not in ('new-invocation','resume-invocation',None): R.fail('schema',2)
        if (p['history_origin']=='context-start')!=(p['start_kind'] is None): R.fail('schema',2)
        if type(p['sources']) is not list: R.fail('schema',2)
        counts={}; unique=set(); width=40 if state.contexts[p['context_id']]['format']=='sha1' else 64
        for s in p['sources']:
            R.keys(s,SOURCE_KEYS); count+=1
            if type(s['role']) is not str or s['role'] not in ROLES or type(s['absent']) is not bool: R.fail('schema',2)
            counts[s['role']]=counts.get(s['role'],0)+1
            if s['profile_sha256'] is not None and not hex64(s['profile_sha256']): R.fail('schema',2)
            for k in ('commit_oid','profile_blob_oid'):
                if s[k] is not None and (type(s[k]) is not str or not re.fullmatch('[0-9a-f]{%d}'%width,s[k])): R.fail('schema',2)
            if s['absent'] and (s['profile_sha256'] is not None or s['profile_blob_oid'] is not None): R.fail('schema',2)
            if not s['absent'] and s['profile_sha256'] is None: R.fail('schema',2)
            if s['role'] in ('current','observed-current'):
                if s['commit_oid'] is not None or s['profile_blob_oid'] is not None: R.fail('schema',2)
            elif not s['absent'] and (s['profile_blob_oid'] is None or s['commit_oid'] is None): R.fail('schema',2)
            sig=R.encoded(s)
            if sig in unique: R.fail('schema',2)
            unique.add(sig)
        for role in ('current','held-head')+(('base','start') if p['history_origin']=='task' else ()):
            if counts.get(role)!=1: R.fail('plan-sources',22)
        if p['branch_provenance'] not in ('workflow-recorded','not-recorded') or counts.get('branch',0)!=(1 if p['branch_provenance']=='workflow-recorded' else 0): R.fail('plan-sources',22)
        for kind in ('globs','eres'):
            if type(p[kind]) is not list or any(type(x) is not str for x in p[kind]) or len(set(p[kind]))!=len(p[kind]): R.fail('schema',2)
            for expr in p[kind]:
                if type(expr) is not str: R.fail('schema',2)
                try: raw=expr.encode('utf-8','strict')
                except UnicodeError: R.fail('schema',2)
                if not raw or len(raw)>65536: R.fail('expression-limit')
                expr_bytes+=len(raw)
                if kind=='globs':
                    try: P.validate_glob(expr,'診断')
                    except P.ProfileError: R.fail('profile-invalid',2)
        if any(x not in p['globs'] for x in P.DEFAULT_PATHS): R.fail('plan-sources',22)
    if count>2000 or expr_bytes>R.MIB: R.fail('plan-limit')
    schema_checks(plan,state)
    return plan

def schema_checks(plan,state):
    req=plan['prepared_request']
    if req is None:
        if plan['source_checks']: R.fail('schema',2)
        return
    width=40 if state.contexts[plan['context_id']]['format']=='sha1' else 64
    validate_request(req,width)
    for c in plan['source_checks']:
        R.keys(c,'context_id root_prefix_b64 current_profile head resolved_refs')
        if type(c['context_id']) is not int or not 0<=c['context_id']<len(state.contexts): R.fail('schema',2)
        R.unb64(c['root_prefix_b64'])
        width=40 if state.contexts[c['context_id']]['format']=='sha1' else 64
        def oid(value):
            if value is not None and (type(value) is not str or not re.fullmatch('[0-9a-f]{%d}'%width,value)): R.fail('schema',2)
        cur=c['current_profile']; R.keys(cur,'absent sha256 metadata')
        if type(cur['absent']) is not bool: R.fail('schema',2)
        if cur['absent']:
            if cur['sha256'] is not None or cur['metadata'] is not None: R.fail('schema',2)
        else:
            if not hex64(cur['sha256']): R.fail('schema',2)
            m=cur['metadata']; R.keys(m,'dev ino type mode size mtime_ns ctime_ns')
            if m['type']!='regular' or any(type(m[k]) is not int for k in ('dev','ino','mode','size','mtime_ns','ctime_ns')): R.fail('schema',2)
        h=c['head']; R.keys(h,'symbolic_target_b64 oid'); oid(h['oid'])
        if h['symbolic_target_b64'] is not None: R.unb64(h['symbolic_target_b64'])
        if type(c['resolved_refs']) is not list: R.fail('schema',2)
        seen=set()
        for ref in c['resolved_refs']:
            R.keys(ref,'name_b64 oid'); name=R.unb64(ref['name_b64']); oid(ref['oid'])
            if name in seen: R.fail('schema',2)
            seen.add(name)

def hex64(s): return type(s) is str and re.fullmatch('[0-9a-f]{64}',s) is not None

def load(path,expected,state):
    path=os.path.abspath(os.fsencode(path))
    try:
        state.reader.owner(os.path.dirname(path))
        state.reader.owner(path)
        raw=state.reader.read(path,16*R.MIB)
    except (OSError,R.Failure) as exc:
        if isinstance(exc,R.Failure) and exc.code==20: raise
        R.fail('plan-binding',22)
    if R.digest(raw)!=expected: R.fail('plan-binding',22)
    return schema(R.decode(raw),state)

def current(policy,state):
    c=state.contexts[policy['context_id']]; root=c['wt']; prefix=R.unb64(policy['root_prefix_b64'])
    root=os.path.join(root,prefix) if prefix else root
    fd=state.reader.directory(root); held=[]
    try:
        try: raw=P.current_profile_fd(fd,state.budget.check,held.append)
        except P.ProfileError: R.fail('profile-invalid',23)
    finally: os.close(fd)
    path=root+b'/.claude/project-profile.yml'
    s=state.reader.lstat(path)
    if raw is None:
        if s is not None: R.fail('source-changed')
        return None,{'absent':True,'sha256':None,'metadata':None}
    if s is None or not held or R.fingerprint(s)!=R.fingerprint(held[0]) or not stat.S_ISREG(s.st_mode) or len(raw)!=s.st_size: R.fail('source-changed')
    expected=(R.fingerprint(s),R.digest(raw))
    old=state.reader.files.get(path)
    if old is not None and old!=expected: R.fail('source-changed')
    state.reader.files[path]=expected
    return raw,{'absent':False,'sha256':expected[1],'metadata':R.metadata(s)}

def add_source(policy,role,oid,private,state,current_data=None):
    if role in ('current','observed-current'):
        raw=(current_data if current_data is not None else current(policy,state))[0]; blob=None; oid=None
    elif oid is None: raw=None; blob=None
    else:
        prefix=R.unb64(policy['root_prefix_b64']); path=(prefix+b'/' if prefix else b'')+P.PROFILE_PATH.encode()
        try: blob,raw=P.ref_profile_backend(private.profile_backend,oid,path)
        except P.ProfileError: R.fail('profile-invalid',23)
    source={'role':role,'commit_oid':oid,'profile_blob_oid':blob,'profile_sha256':None if raw is None else R.digest(raw),'absent':raw is None}
    if source not in policy['sources']: policy['sources'].append(source)
    if raw is not None:
        try: globs=P.paths_from_yaml(raw,'診断')
        except P.ProfileError: R.fail('profile-invalid',23)
        for value in globs:
            if value not in policy['globs']: policy['globs'].append(value)

def source_oid(value,private):
    width=private.width
    if not re.fullmatch('[0-9a-f]{%d}'%width,value): R.fail('source-oid',2)
    empty=private.git('hash-object','-t','tree','--stdin',limit=128).strip().decode()
    if value==empty: return None
    oid=private.oid(value)
    if oid!=value: R.fail('source-oid',22)
    return oid

def prepare_initial(args,state,private_for):
    if args.carry_plan:
        plan=copy.deepcopy(load(args.carry_plan[0],args.carry_plan[1],state)); parent=args.carry_plan[1]
    else:
        plan={'version':1,'state_binding':state.binding,'context_id':args.context_id,'management_binding':state.management,
              'parent_plan_sha256':None,'prepared_request':None,'source_checks':[],'policies':[],'default_policy':'snapshot-v1'}; parent=None
    cid=args.context_id; c=state.contexts[cid]
    existing={(p['context_id'],p['root_prefix_b64']) for p in plan['policies']}
    if (cid,'') in existing: R.fail('duplicate-context',22)
    if any((a,'') not in existing for a in state.ancestors(cid)[:-1]): R.fail('missing-ancestor-policy',22)
    if args.context_start and cid==0: R.fail('context-start',2)
    pv=private_for(cid)
    specs=[('worktree',b'')]
    if cid==0 and state.management['relative_prefix_b64']: specs.append(('management',R.unb64(state.management['relative_prefix_b64'])))
    for kind,prefix in specs:
        p={'context_id':cid,'root_kind':kind,'root_prefix_b64':R.b64(prefix),'history_origin':'context-start' if args.context_start else 'task',
           'start_kind':None if args.context_start else args.start_kind,'branch_provenance':'workflow-recorded' if args.branch_ref else 'not-recorded',
           'sources':[],'globs':list(P.DEFAULT_PATHS),'eres':[]}
        add_source(p,'current',None,pv,state)
        add_source(p,'held-head',c['held'],pv,state)
        if not args.context_start:
            add_source(p,'base',source_oid(args.base_ref,pv),pv,state); add_source(p,'start',source_oid(args.start_ref,pv),pv,state)
        if args.branch_ref: add_source(p,'branch',source_oid(args.branch_ref,pv),pv,state)
        for oid in args.profile_ref: add_source(p,'extra',source_oid(oid,pv),pv,state)
        if args.exclude_root==('management' if kind=='management' else 'context') or (kind=='worktree' and not prefix and cid==0 and not state.management['relative_prefix_b64'] and args.exclude_root=='management'):
            for field,values in (('globs',args.exclude_glob),('eres',args.exclude)):
                for value in values:
                    if value not in p[field]: p[field].append(value)
        plan['policies'].append(p)
    plan.update(context_id=cid,parent_plan_sha256=parent,prepared_request=None,source_checks=[])
    return schema(plan,state)

def necessary(plan,state,cid):
    ancestors=state.ancestors(cid); policies=[p for p in plan['policies'] if p['context_id'] in ancestors]
    for a in ancestors:
        if not any(p['context_id']==a and p['root_kind']=='worktree' for p in policies): R.fail('missing-ancestor-policy',22)
    if state.management['relative_prefix_b64'] and not any(p['root_kind']=='management' for p in policies): R.fail('missing-management-policy',22)
    return policies

def extend(args,state,private_for,request):
    plan=copy.deepcopy(load(args.plan,args.plan_sha256,state)); cid=args.context_id
    for p in necessary(plan,state,cid):
        pv=private_for(p['context_id']); data=current(p,state); add_source(p,'observed-current',None,pv,state,data)
        add_source(p,'observed-head',pv.oid('HEAD',unborn=True),pv,state)
        if p['context_id']==cid:
            for oid in request['resolved_revisions'].values():
                if oid is not None: add_source(p,'observed-ref',oid,pv,state)
    checks=[]
    for p in necessary(plan,state,cid):
        pv=private_for(p['context_id']); _raw,profile=current(p,state)
        refs=[]
        if p['context_id']==cid:
            for rev,oid in requested_refs(request).items(): refs.append({'name_b64':R.b64(rev.encode()),'oid':oid})
        checks.append({'context_id':p['context_id'],'root_prefix_b64':p['root_prefix_b64'],'current_profile':profile,'head':pv.head(),'resolved_refs':refs})
    plan.update(context_id=cid,parent_plan_sha256=args.plan_sha256,prepared_request=request,source_checks=checks)
    return schema(plan,state)

def requested_refs(request):
    options=request['options']; result={}
    for name in ('left','right','rev','to'):
        value=options.get(name)
        if value not in (None,'index','worktree'):
            result[value]=request['resolved_revisions'].get(name)
    return result

def verify_prepared(plan,state,private_for,cid):
    if plan['context_id']!=cid or plan['prepared_request'] is None: R.fail('plan-unprepared',22)
    request=plan['prepared_request']; R.keys(request,'operation options resolved_revisions')
    needed=necessary(plan,state,cid)
    checks=plan['source_checks']; index={}
    for item in checks:
        R.keys(item,'context_id root_prefix_b64 current_profile head resolved_refs')
        key=(item['context_id'],item['root_prefix_b64'])
        if key in index: R.fail('schema',2)
        index[key]=item
    if set(index)!={(p['context_id'],p['root_prefix_b64']) for p in needed}: R.fail('plan-unprepared',22)
    for p in needed:
        check=index[(p['context_id'],p['root_prefix_b64'])]; pv=private_for(p['context_id'])
        # runはYAML解析せずraw bytesとmetadataを照合するだけ。
        _raw,actual=current(p,state)
        if actual!=check['current_profile'] or pv.head()!=check['head']: R.fail('preparation-stale',22)
        expected=[{'name_b64':R.b64(name.encode()),'oid':oid} for name,oid in requested_refs(request).items()] if p['context_id']==cid else []
        if check['resolved_refs']!=expected: R.fail('plan-unprepared',22)
        for ref in check['resolved_refs']:
            R.keys(ref,'name_b64 oid'); name=R.unb64(ref['name_b64']).decode('utf-8')
            if pv.oid(name,unborn=name=='HEAD')!=ref['oid']: R.fail('preparation-stale',22)
    return request

def publish(plan,out,state):
    raw=R.encoded(plan)
    if len(raw)>16*R.MIB: R.fail('plan-limit')
    path=os.path.abspath(os.fsencode(out)); parent=os.path.dirname(path)
    # linked/separateの管理領域は作業木の外にもある。未選択の対象も保護する。
    roots=[state.path]+[c[key] for c in state.contexts for key in ('wt','gd','common')
                       if os.path.isabs(c[key])]
    physical=os.path.join(os.path.realpath(parent),os.path.basename(path))
    for root in roots:
        state.budget.check()
        root=os.path.realpath(root)
        if os.path.commonpath((physical,root))==root: R.fail('out-location',2)
    fd=state.reader.directory(parent)
    target=None
    try:
        state.reader.owner(parent,os.fstat(fd),2)
        if stat.S_IMODE(os.fstat(fd).st_mode)!=0o700: R.fail('out-parent',2)
        try: target=os.open(os.path.basename(path),os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=fd)
        except FileExistsError: R.fail('out-exists',2)
        os.fchmod(target,0o600); view=memoryview(raw)
        while view:
            state.budget.check(); n=os.write(target,view[:65536])
            if n<=0: R.fail('write-failed')
            view=view[n:]
        os.fsync(target); os.lseek(target,0,0); chunks=[]
        while True:
            state.budget.check(); block=os.read(target,65536)
            if not block: break
            chunks.append(block)
        if R.digest(b''.join(chunks))!=R.digest(raw): R.fail('out-changed')
        os.fsync(fd)
    finally:
        if target is not None: os.close(target)
        os.close(fd)
    return path,R.digest(raw)
