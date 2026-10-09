"""固定7操作のraw観測。除外判定後にだけ本文を取得する。"""
import os
import re
import stat

R=None; X=None

def configure(runtime,paths):
    global R,X
    R,X=runtime,paths

def endpoint(kind='absent',git_mode=None,os_mode=None,size=0,sha256=None,oid=None):
    return {'kind':kind,'git_mode':git_mode,'os_mode':os_mode,'size':size,'sha256':sha256,'oid':oid}

def compare(left,right):
    if left['kind']==right['kind']=='absent': return 'same'
    if left['kind']=='absent': return 'added'
    if right['kind']=='absent': return 'deleted'
    if left['kind']!=right['kind']: return 'type-changed'
    a=(left['git_mode'],left['oid'] if left['kind']=='gitlink' else left['sha256'])
    b=(right['git_mode'],right['oid'] if right['kind']=='gitlink' else right['sha256'])
    return 'same' if a==b else 'modified'

class Observer:
    def __init__(self,rt,state,pv,matcher,req):
        self.rt=rt; self.state=state; self.pv=pv; self.matcher=matcher; self.req=req; self.o=req['options']; self.op=req['operation']
        self.root=pv.context['wt']; self.index={}; self.flags={}; self.trees={}; self.blobs={}; self.work={}; self.stats={}; self.links={}
        self.boundaries={}; self.warnings={'settings-not-applied'}; self.excluded=set()
    def index_entries(self):
        raw=self.pv.git('ls-files','--stage','-z')
        for row in raw.split(b'\0')[:-1]:
            self.rt.budget.check(); meta,sep,path=row.partition(b'\t'); f=meta.split()
            if not sep or len(f)!=3 or not re.fullmatch(rb'[0-9a-f]{%d}'%self.pv.width,f[1]): R.fail('index-format',23)
            X.literal(path); mode=f[0].decode(); stage=int(f[2]); oid=f[1].decode()
            if mode=='040000':
                for child,val in self.tree(oid).items(): self.index[path.rstrip(b'/')+b'/'+child]=[{'stage':stage,'mode':val[0],'oid':val[1]}]
            else: self.index.setdefault(path,[]).append({'stage':stage,'mode':mode,'oid':oid})
        for row in self.pv.git('ls-files','-v','-z').split(b'\0')[:-1]:
            if len(row)<3 or row[1:2]!=b' ': R.fail('index-format',23)
            path=row[2:]; flag=row[:1]; flags=[]
            if flag.upper()==b'S': flags.append('skip-worktree')
            if flag.islower(): flags.append('assume-unchanged')
            if path.endswith(b'/'):
                for name in self.index:
                    if name.startswith(path): self.flags[name]=flags
            else: self.flags[path]=flags
    def tree(self,oid):
        if oid is None: return {}
        if oid not in self.trees:
            result={}
            for row in self.pv.git('ls-tree','-r','-z',oid).split(b'\0')[:-1]:
                self.rt.budget.check(); meta,sep,path=row.partition(b'\t'); fields=meta.split()
                if not sep or len(fields)!=3 or not re.fullmatch(rb'[0-9a-f]{%d}'%self.pv.width,fields[2]): R.fail('tree-format',23)
                X.literal(path); result[path]=(fields[0].decode(),fields[2].decode())
            self.trees[oid]=result
        return self.trees[oid]
    def obj(self,value,body=True):
        if value is None: return endpoint(),None
        mode,oid=value
        if mode=='160000': return endpoint('gitlink',mode,None,None,None,oid),None
        if mode not in ('100644','100755','120000'): R.fail('object-type',23)
        kind='symlink' if mode=='120000' else 'regular'
        size_raw=self.pv.git('cat-file','-s',oid,limit=128).strip()
        if not re.fullmatch(rb'[0-9]+',size_raw): R.fail('object-size',23)
        size=int(size_raw)
        raw=None; sha=None
        if body:
            if size>self.rt.budget.limits['body']-self.rt.budget.used.get('body',0): R.fail('body-limit')
            if oid not in self.blobs:
                raw=self.pv.git('cat-file','blob',oid,limit=size)
                if len(raw)!=size: R.fail('object-size',23)
                self.rt.budget.charge('body',len(raw)); self.blobs[oid]=raw
            raw=self.blobs[oid]; sha=R.digest(raw)
        return endpoint(kind,mode,None,size,sha,oid),raw
    def gitlink(self,path):
        return any(entry['mode']=='160000' for entry in self.index.get(path,[]))
    def parents(self,path):
        current=self.root; parts=path.split(b'/')
        for i,part in enumerate(parts[:-1]):
            current+=b'/'+part; prefix=b'/'.join(parts[:i+1])
            if self.gitlink(prefix):
                self.boundaries[prefix]='submodule'; return prefix
            s=self.state.reader.lstat(current)
            if s is None: return None
            if stat.S_ISLNK(s.st_mode): R.fail('symlink-parent',23)
            if not stat.S_ISDIR(s.st_mode): return None
            if os.path.lexists(current+b'/.git'):
                self.boundaries[prefix]='nested-repository'; return prefix
        return path
    def walk(self,prefix,depth=0):
        if depth>128: R.fail('worktree-depth-limit')
        directory=self.root+(b'/'+prefix if prefix else b'')
        names=self.state.reader.names(directory,'worktree_entries')
        paths=[prefix+b'/'+name if prefix else name for name in names]
        excluded=self.matcher.excluded(paths); self.excluded.update(excluded)
        for path in paths:
            if path in excluded: continue
            absolute=self.root+b'/'+path; s=self.state.reader.lstat(absolute,False)
            self.stats[path]=R.fingerprint(s)
            if stat.S_ISDIR(s.st_mode):
                if self.gitlink(path) or os.path.lexists(absolute+b'/.git'):
                    self.boundaries[path]='submodule' if self.gitlink(path) else 'nested-repository'
                    self.work[path]=s
                elif path in self.index:
                    self.work[path]=s; self.walk(path,depth+1)
                else: self.walk(path,depth+1)
            else: self.work[path]=s
    def candidates(self,object_maps,worktree):
        names=set()
        for mapping in object_maps:
            names.update(p for p in mapping if X.selected(p,self.o))
        direct=[R.unb64(n) for n in self.o.get('path_b64',[])]
        names.update(direct)
        if not worktree and self.op in ('diff','show'):
            for path in direct:
                if any(any(n.startswith(path+b'/') for n in mapping) for mapping in object_maps): R.fail('directory-selection',23)
        self.excluded.update(self.matcher.excluded(names))
        if worktree:
            self.warnings.add('ignore-not-applied')
            # .gitの有無や子の実体化に依存せず、indexのgitlinkで止める。
            selected=direct+[R.unb64(n) for n in self.o.get('under_b64',[])]
            for path in self.index:
                self.rt.budget.check()
                if self.gitlink(path) and (X.selected(path,self.o) or any(
                    p==path or p.startswith(path+b'/') for p in selected)):
                    self.boundaries[path]='submodule'; names.add(path)
            for path in direct:
                if path in self.excluded: continue
                bound=self.parents(path)
                if bound!=path:
                    if bound: names.add(bound)
                    names.discard(path); continue
                s=self.state.reader.lstat(self.root+b'/'+path)
                if s is not None:
                    self.stats[path]=R.fingerprint(s); self.work[path]=s
                    if stat.S_ISDIR(s.st_mode):
                        if self.gitlink(path) or os.path.lexists(self.root+b'/'+path+b'/.git'): self.boundaries[path]='submodule' if self.gitlink(path) else 'nested-repository'
                        elif self.op!='files': R.fail('directory-selection',23)
            for encoded in self.o.get('under_b64',[]):
                prefix=R.unb64(encoded); prefix=b'' if prefix==b'.' else prefix
                if prefix and prefix in self.matcher.excluded([prefix]): self.excluded.add(prefix); continue
                if prefix:
                    bound=self.parents(prefix+b'/probe')
                    if bound!=prefix+b'/probe':
                        if bound: names.add(bound)
                        continue
                s=self.state.reader.lstat(self.root+(b'/'+prefix if prefix else b''))
                if s is not None and stat.S_ISDIR(s.st_mode): self.walk(prefix)
                elif s is not None: R.fail('directory-selection',23)
            names.update(self.work); names.update(self.boundaries)
        self.excluded.update(self.matcher.excluded(names))
        for name in list(self.boundaries):
            names={p for p in names if p==name or not p.startswith(name+b'/')}
        for name in self.excluded: self.boundaries.pop(name,None)
        return sorted(names-self.excluded)
    def wt(self,path,body=True):
        absolute=self.root+b'/'+path
        if path not in self.work:
            bound=self.parents(path)
            if bound!=path: return endpoint(),None
            self.work[path]=self.state.reader.lstat(absolute)
        s=self.work[path]
        if s is None: return endpoint(),None
        self.stats[path]=R.fingerprint(s)
        mode=stat.S_IMODE(s.st_mode)
        if path in self.boundaries and self.boundaries[path]=='submodule':
            entry=next((s for s in self.index.get(path,[]) if s['mode']=='160000'),None)
            return endpoint('gitlink','160000',mode,None,None,entry['oid'] if entry else None),None
        if stat.S_ISDIR(s.st_mode): return endpoint('directory',None,mode,None),None
        if stat.S_ISREG(s.st_mode):
            raw=self.state.reader.read(absolute,self.rt.budget.limits['body'],'body') if body else None
            return endpoint('regular','100755' if s.st_mode&stat.S_IXUSR else '100644',mode,s.st_size,None if raw is None else R.digest(raw)),raw
        if stat.S_ISLNK(s.st_mode):
            raw=None
            if body:
                fd=self.state.reader.directory(os.path.dirname(absolute))
                try:
                    raw=os.readlink(os.path.basename(absolute),dir_fd=fd)
                    if R.fingerprint(os.stat(os.path.basename(absolute),dir_fd=fd,follow_symlinks=False))!=R.fingerprint(s): R.fail('source-changed')
                finally: os.close(fd)
                self.rt.budget.charge('body',len(raw)); self.links[path]=raw
            return endpoint('symlink','120000',mode,s.st_size,None if raw is None else R.digest(raw)),raw
        kind='fifo' if stat.S_ISFIFO(s.st_mode) else 'socket' if stat.S_ISSOCK(s.st_mode) else 'device'
        if self.op!='files': R.fail('special-file',23)
        self.boundaries[path]='special-file'
        return endpoint(kind,None,mode,s.st_size),None
    def patch(self,left,right):
        if left is None and right is None: return None
        a=left or b''; b=right or b''; self.rt.budget.charge('patch_copy',len(a)+len(b))
        directory=os.path.join(self.rt.temp,('patch-%d'%len(self.rt.children)).encode()); os.mkdir(directory,0o700)
        self.pv.write(directory+b'/a',a); self.pv.write(directory+b'/b',b)
        # パスと設定を元worktreeから切り離した2つの通常fileだけを渡す。
        rc,raw=self.rt.process([self.rt.git,*self.pv.safe,
            'diff','--no-index','--no-ext-diff','--no-textconv','--no-color','--no-renames','--unified=%d'%self.o.get('context',3),'--','a','b'],
            env=self.pv.env,cwd=directory)
        if rc not in (0,1): R.fail('patch-failed',23)
        return R.b64(raw)
    def postcheck(self):
        for path,expected in self.stats.items():
            actual=self.state.reader.lstat(self.root+b'/'+path)
            if actual is None or R.fingerprint(actual)!=expected: R.fail('source-changed')
        for path,value in self.links.items():
            if os.readlink(self.root+b'/'+path)!=value: R.fail('source-changed')
    def boundaries_json(self):
        if any(x in ('nested-repository','submodule') for x in self.boundaries.values()): self.warnings.add('nested-content-not-observed')
        return [{'path_b64':R.b64(p),'reason':r} for p,r in sorted(self.boundaries.items())]
    def records(self):
        self.index_entries(); resolved=self.req['resolved_revisions']; records=[]
        if self.op in ('status','files'):
            head=self.tree(resolved.get('head')); names=self.candidates([head,self.index],True)
            for path in names:
                tracked=path in self.index; stages=self.index.get(path,[]); flags=self.flags.get(path,[])
                if self.op=='files':
                    if self.o['kind']=='tracked' and not tracked or self.o['kind']=='untracked' and tracked: continue
                    ep,_=self.wt(path,False)
                    if ep['kind']=='absent' and 'skip-worktree' in flags: self.warnings.add('sparse-not-materialized')
                    records.append({'path_b64':R.b64(path),'tracked':tracked,'endpoint':ep,'index_flags':flags,'stages':stages})
                else:
                    if not tracked and path not in head and self.o['untracked']=='no': continue
                    h,_=self.obj(head.get(path)); unmerged=any(s['stage']!=0 for s in stages)
                    i=None if unmerged else self.obj((stages[0]['mode'],stages[0]['oid']) if stages else None)[0]
                    w,_=self.wt(path,tracked or path in head)
                    wc='not-observed' if path in self.boundaries and self.boundaries[path] in ('submodule','nested-repository') else 'unmerged' if unmerged else 'untracked' if not tracked and path not in head else 'not-materialized' if w['kind']=='absent' and 'skip-worktree' in flags else compare(i,w)
                    if wc=='not-materialized': self.warnings.add('sparse-not-materialized')
                    records.append({'path_b64':R.b64(path),'head':h,'index':i,'worktree':w,'index_flags':flags,'stages':stages,
                                    'staged_comparison':'unmerged' if unmerged else compare(h,i),'worktree_comparison':wc})
            variant='status-v1' if self.op=='status' else 'files-v1'
            data={'variant':variant,'records':records,'excluded_count':len(self.excluded),'boundaries':self.boundaries_json()}
        else:
            if self.op=='show':
                left='commit'; right='commit'; lrev=resolved['parent']; rrev=resolved['rev']
            else:
                left=self.o['left']; right=self.o['right']; lrev=resolved.get('left'); rrev=resolved.get('right')
            def mapping(side,rev):
                if side=='index': return {p:(s[0]['mode'],s[0]['oid']) for p,s in self.index.items()}
                if side=='worktree': return {}
                return self.tree(rev)
            lm,rm=mapping(left,lrev),mapping(right,rrev)
            names=self.candidates([lm,rm,self.index] if right=='worktree' else [lm,rm],right=='worktree')
            if self.op=='show' and self.o['format']=='blob':
                path=R.unb64(self.o['path_b64'][0])
                if path in self.excluded: R.fail('excluded-selection',23)
                if path not in rm: R.fail('path-unavailable',23)
                ep,raw=self.obj(rm[path])
                if raw is None: R.fail('blob-selection',23)
                data={'variant':'blob-v1','path_b64':R.b64(path),'endpoint':ep,'bytes_b64':R.b64(raw),'excluded_count':0}
            else:
                for path in names:
                    if (left=='index' or right=='index') and any(s['stage']!=0 for s in self.index.get(path,[])): R.fail('unmerged-index',23)
                    le,lb=self.obj(lm.get(path)); re_,rb=self.wt(path) if right=='worktree' else self.obj(rm.get(path))
                    if path in self.boundaries and self.boundaries[path]=='nested-repository': continue
                    missing=right=='worktree' and re_['kind']=='absent' and 'skip-worktree' in self.flags.get(path,[])
                    comparison='not-materialized' if missing else compare(le,re_)
                    if missing: self.warnings.add('sparse-not-materialized')
                    if comparison=='same': continue
                    patch=self.patch(lb,rb) if self.o['format']=='patch' and not missing else None
                    records.append({'path_b64':R.b64(path),'left':le,'right':re_,'comparison':comparison,'patch_b64':patch})
                def label(side,oid):
                    if side in ('index','worktree'): return side
                    if oid: return 'commit:'+oid
                    return 'empty-tree:'+self.pv.git('hash-object','-t','tree','--stdin',limit=128).strip().decode()
                data={'variant':'diff-v1','left':label(left,lrev),'right':label(right,rrev),'records':records,'excluded_count':len(self.excluded),'boundaries':self.boundaries_json()}
        if self.excluded: self.warnings.add('paths-excluded')
        self.postcheck(); return data,self.warnings
    def log(self):
        oid=self.req['resolved_revisions']['to']; scopes=self.o['path_b64']+self.o['under_b64']; paths=[]
        if scopes and oid:
            names=set(R.unb64(p) for p in self.o['path_b64'])
            if self.o['under_b64']:
                for commit in self.pv.git('rev-list',oid).splitlines():
                    self.rt.budget.check()
                    if not re.fullmatch(rb'[0-9a-f]{%d}'%self.pv.width,commit): R.fail('history-format',23)
                    names.update(p for p in self.tree(commit.decode()) if X.selected(p,self.o))
            self.excluded.update(self.matcher.excluded(names)); paths=sorted(set(names)-self.excluded)
        fixed=['log','--no-show-signature','--no-decorate','--no-notes','--no-patch','--no-color']
        if oid is None or (scopes and not paths): raw=b''
        elif not scopes:
            raw=self.pv.git(*fixed,'--format='+self.o['format'],'--max-count=%d'%self.o['limit'],oid)
        else:
            # Gitのpath引数はdirectoryを再帰する。候補の履歴順序とmergeの
            # 簡略化はGitに任せ、変更名の完全一致を確認してからlimitを適用。
            wanted=set(paths); commits=[]
            candidates=self.pv.git(*fixed,'--format=%H',oid,'--',*paths).splitlines()
            for commit in candidates:
                self.rt.budget.check()
                if not re.fullmatch(rb'[0-9a-f]{%d}'%self.pv.width,commit): R.fail('history-format',23)
                lineage=self.pv.git('rev-list','--parents','--max-count=1',commit.decode()).split()
                if not lineage or lineage[0]!=commit or any(
                    not re.fullmatch(rb'[0-9a-f]{%d}'%self.pv.width,p) for p in lineage): R.fail('history-format',23)
                # mergeは各親に対して選択集合が変わるときに残す。同じ名前が
                # 全親で変わる必要はないため、combined diffの共通名では判定しない。
                changed_from_all=True
                for parent in lineage[1:] or [None]:
                    self.rt.budget.check()
                    revisions=[parent.decode(),commit.decode()] if parent else [commit.decode()]
                    changed=self.pv.git('diff-tree','--root','--no-commit-id','-r','--name-only',
                                        '--no-renames','--no-ext-diff','--no-textconv','-z',*revisions)
                    if changed and not changed.endswith(b'\0'): R.fail('history-format',23)
                    exact=set(changed.split(b'\0')[:-1]) & wanted
                    excluded=self.matcher.excluded(exact); self.excluded.update(excluded)
                    if not exact-excluded:
                        changed_from_all=False; break
                if changed_from_all:
                    commits.append(commit.decode())
                    if len(commits)==self.o['limit']: break
            raw=self.pv.git(*fixed,'--no-walk=unsorted','--format='+self.o['format'],*commits) if commits else b''
        if self.excluded: self.warnings.add('paths-excluded')
        return {'variant':'history-v1','format':self.o['format'],'bytes_b64':R.b64(raw),'limit':self.o['limit'],'unborn':oid is None,'excluded_count':len(self.excluded)},self.warnings
    def stashes(self):
        records=self.pv.refs(); exists=any(R.unb64(r['name_b64'])==b'refs/stash' for r in records)
        if self.pv.context['backend']=='files':
            logs=os.path.exists(self.pv.common+b'/logs/refs/stash')
            if exists and not logs: R.fail('incomplete-metadata',23)
            if not exists and logs: R.fail('incomplete-metadata',23)
        if exists:
            raw=self.pv.git('reflog','show','--format=oneline','--no-decorate','--no-abbrev','--max-count=%d'%self.o['limit'],'refs/stash')
            if not raw: R.fail('incomplete-metadata',23)
        else: raw=b''
        return {'variant':'stashes-v1','bytes_b64':R.b64(raw),'limit':self.o['limit']},self.warnings

def observe(rt,state,pv,matcher,request):
    observer=Observer(rt,state,pv,matcher,request)
    if request['operation']=='refs': return {'variant':'refs-v1','records':pv.refs()},{'settings-not-applied'}
    if request['operation']=='stashes': return observer.stashes()
    if request['operation']=='log': return observer.log()
    return observer.records()
