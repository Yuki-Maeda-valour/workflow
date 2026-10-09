"""保持した管理データを私有領域へコピーし、そこだけでGitを起動する。"""
import os
import re
import stat

R=None
SAFE=()

def configure(runtime,safe):
    global R,SAFE
    R,SAFE=runtime,safe

def valid_ref(name):
    return (isinstance(name,bytes) and name.startswith(b'refs/') and len(name)<=1024
            and not name.endswith((b'/',b'.')) and b'..' not in name and b'@{' not in name
            and not any(c<32 or c==127 or c in b' ~^:?*[\\' for c in name)
            and all(p and not p.startswith(b'.') and not p.endswith(b'.lock') for p in name.split(b'/')))

class Private:
    def __init__(self,rt,state,context):
        self.rt=rt; self.reader=rt.reader; self.budget=rt.budget; self.context=context
        self.safe=SAFE
        self.width=40 if context['format']=='sha1' else 64
        if any(os.environ.get(n) for n in ('GIT_OBJECT_DIRECTORY','GIT_ALTERNATE_OBJECT_DIRECTORIES')): R.fail('unsupported-object-environment',23)
        self.path=os.path.join(rt.temp,('context-%d'%context['id']).encode()); os.mkdir(self.path,0o700)
        self.gd=self.path+b'/gitdir'; self.common=self.path+b'/common'; self.work=self.path+b'/worktree'; self.home=self.path+b'/home'
        for d in (self.gd,self.common,self.work,self.home): os.mkdir(d,0o700)
        for d in (self.gd+b'/refs',self.common+b'/refs',self.common+b'/objects'): os.mkdir(d,0o700)
        self.write(self.gd+b'/commondir',self.common+b'\n')
        self.copy(context['gd']+b'/HEAD',self.gd+b'/HEAD',False)
        self.copy(context['gd']+b'/index',self.gd+b'/index',True)
        # 選択されたworktreeのsharedindexだけ。同じ私有gitdirへ置く。
        for name in self.reader.names(context['gd']):
            if name.startswith(b'sharedindex.'):
                self.copy(context['gd']+b'/'+name,self.gd+b'/'+name,False)
        for source,target in ((context['common'],self.common),(context['gd'],self.gd)):
            for name in (b'refs',b'reftable'):
                self.copy_tree(source+b'/'+name,target+b'/'+name,0)
        for name in (b'packed-refs',b'shallow',b'logs/refs/stash'):
            self.copy(context['common']+b'/'+name,self.common+b'/'+name,True)
        if context['gd']==context['common']:
            # Gitは共有refsをcommonから読む。HEADとindexは固有領域に置く。
            pass
        cfg=b'[core]\nrepositoryformatversion = '+(b'1' if context['format']=='sha256' or context['backend']=='reftable' else b'0')+b'\nbare = false\n'
        if context['format']=='sha256' or context['backend']=='reftable':
            cfg+=b'[extensions]\n'
            if context['format']=='sha256': cfg+=b'objectformat = sha256\n'
            if context['backend']=='reftable': cfg+=b'refstorage = reftable\n'
        self.write(self.common+b'/config',cfg)
        self.objects=os.path.realpath(context['common']+b'/objects')
        self.odb(self.objects,set(),0)
        self.env={'PATH':os.path.dirname(rt.git),'HOME':os.fsdecode(self.home),'LC_ALL':'C',
                  'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_SYSTEM':os.devnull,'GIT_CONFIG_GLOBAL':os.devnull,
                  'GIT_CONFIG_COUNT':'0','GIT_DIR':os.fsdecode(self.gd),'GIT_COMMON_DIR':os.fsdecode(self.common),
                  'GIT_INDEX_FILE':os.fsdecode(self.gd+b'/index'),'GIT_WORK_TREE':os.fsdecode(self.work),
                  'GIT_OBJECT_DIRECTORY':os.fsdecode(self.objects),'GIT_OPTIONAL_LOCKS':'0','GIT_NO_LAZY_FETCH':'1',
                  'GIT_TERMINAL_PROMPT':'0','GIT_LITERAL_PATHSPECS':'1','GIT_ATTR_NOSYSTEM':'1'}
        rc,version=self.command('version',limit=4096)
        match=re.match(rb'git version ([0-9]+)\.([0-9]+)(?:\.|[ \n])',version)
        if not match: R.fail('runtime-unavailable')
        rc,fmt=self.command('rev-parse','--show-object-format',allow=(0,128))
        if rc or fmt.strip()!=context['format'].encode(): R.fail('unsupported-format',23)
    def write(self,path,raw):
        os.makedirs(os.path.dirname(path),mode=0o700,exist_ok=True)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            view=memoryview(raw)
            while view:
                self.budget.check(); n=os.write(fd,view[:65536])
                if n<=0: R.fail('write-failed')
                view=view[n:]
        finally: os.close(fd)
    def copy(self,source,target,missing):
        raw=self.reader.read(source,200*R.MIB,missing=missing)
        if raw is not None: self.write(target,raw)
    def copy_tree(self,source,target,depth):
        if depth>40: R.fail('management-depth-limit')
        s=self.reader.lstat(source)
        if s is None: return
        if not stat.S_ISDIR(s.st_mode): R.fail('incomplete-metadata',23)
        os.makedirs(target,mode=0o700,exist_ok=True)
        for n in self.reader.names(source):
            p=source+b'/'+n; s=self.reader.lstat(p,False)
            if stat.S_ISDIR(s.st_mode): self.copy_tree(p,target+b'/'+n,depth+1)
            elif stat.S_ISREG(s.st_mode): self.copy(p,target+b'/'+n,False)
            else: R.fail('incomplete-metadata',23)
    def odb(self,path,seen,depth):
        if depth>40: R.fail('management-depth-limit')
        path=os.path.realpath(path)
        if path in seen: return
        seen.add(path); fd=self.reader.directory(path); os.close(fd)
        raw=self.reader.read(path+b'/info/alternates',16*R.MIB,missing=True)
        if raw:
            for line in raw.splitlines():
                self.budget.check()
                if not line: continue
                if line.startswith(b'"'):
                    # GitのC引用と同じ限定decoderを呼出側から受ける。
                    line=self.cfield(line)
                child=line if line.startswith(b'/') else os.path.join(path,line)
                self.odb(child,seen,depth+1)
    def command(self,*args,allow=(0,),limit=None):
        rc,raw=self.rt.process([self.rt.git,*SAFE,*args],env=self.env,cwd=self.work,
                               output_kind='git_output',output_limit=limit)
        if rc not in allow: R.fail('git-failed',23)
        return rc,raw
    def git(self,*args,limit=None): return self.command(*args,limit=limit)[1]
    def profile_backend(self,*args):
        limit=R.MIB if args[0]=='cat-file' and args[1]=='blob' else 16*1024
        return self.git(*args,limit=limit)
    def oid(self,value,unborn=False):
        original=value
        if value=='HEAD': value=self.head()['oid']
        elif value.startswith('refs/'):
            value=next((r['oid'] for r in self.refs() if R.unb64(r['name_b64'])==value.encode()),None)
        if value is None:
            if unborn and original=='HEAD': return None
            R.fail('revision-unavailable',23)
        rc,raw=self.command('rev-parse','--verify','--quiet','--end-of-options',value+'^{commit}',allow=(0,1,128),limit=4096)
        if rc:
            if unborn and value=='HEAD' and self.head()['oid'] is None: return None
            R.fail('revision-unavailable',23)
        oid=raw.strip().decode('ascii')
        if not re.fullmatch('[0-9a-f]{%d}'%self.width,oid): R.fail('revision-unavailable',23)
        return oid
    def refs(self):
        if hasattr(self,'ref_records'): return self.ref_records
        if self.context['backend']=='reftable':
            try:
                common=self.reftable.read_stack(self.common+b'/reftable',self.context['format'],self.budget)
                local=self.reftable.read_stack(self.gd+b'/reftable',self.context['format'],self.budget)
            except self.reftable.ReftableError as e: R.fail(e.reason,e.code)
        else:
            common=self.file_refs(self.common); local=self.file_refs(self.gd)
        private=lambda p:p.startswith((b'refs/bisect/',b'refs/worktree/',b'refs/rewritten/'))
        values={k:v for k,v in common.items() if not private(k)}
        if self.context['gd']==self.context['common']: values.update(common)
        else: values.update({k:v for k,v in local.items() if private(k) or k==b'HEAD'})
        self.head_value=values.pop(b'HEAD',None)
        values={k:v for k,v in values.items() if not re.fullmatch(rb'[A-Z][A-Z0-9_]*',k)}
        for name,(oid,target) in values.items():
            if not valid_ref(name) or (target is not None and not valid_ref(target)): R.fail('invalid-ref',23)
        resolved={}
        for name in values:
            self.budget.check(); chain=[]; visited=set(); cur=name
            while cur in values and cur not in resolved:
                if cur in visited: R.fail('symbolic-ref-cycle',23)
                visited.add(cur); chain.append(cur); oid,target=values[cur]
                if target is None:
                    resolved[cur]=oid; break
                cur=target
            value=resolved.get(cur)
            for item in reversed(chain): resolved[item]=value
        ids=sorted(set(v for v in resolved.values() if v is not None))
        if ids:
            rc,checked=self.rt.process([self.rt.git,*SAFE,'cat-file','--batch-check=%(objectname) %(objecttype)'],
                                      input_bytes=('\n'.join(ids)+'\n').encode(),env=self.env,cwd=self.work)
            rows=checked.splitlines()
            if rc or len(rows)!=len(ids): R.fail('incomplete-metadata',23)
            for oid,row in zip(ids,rows):
                fields=row.split()
                if len(fields)!=2 or fields[0]!=oid.encode() or fields[1] not in (b'blob',b'tree',b'commit',b'tag'): R.fail('incomplete-metadata',23)
        self.ref_records=[{'name_b64':R.b64(n),'oid':resolved.get(n),'symbolic_target_b64':None if values[n][1] is None else R.b64(values[n][1]),
                 'resolution':'resolved' if resolved.get(n) else 'unresolved'} for n in sorted(values)]
        return self.ref_records
    def file_refs(self,directory):
        values={}; packed=directory+b'/packed-refs'
        if os.path.exists(packed):
            raw=open(packed,'rb').read()
            for line in raw.splitlines():
                self.budget.check()
                if line.startswith((b'#',b'^')): continue
                f=line.split(b' ')
                if len(f)!=2 or not re.fullmatch(rb'[0-9a-f]{%d}'%self.width,f[0]): R.fail('invalid-ref',23)
                values[f[1]]=(f[0].decode(),None)
        def visit(path,prefix):
            if not os.path.isdir(path): return
            with os.scandir(path) as it:
                for e in it:
                    self.budget.check(); name=prefix+b'/'+os.fsencode(e.name)
                    if e.is_dir(follow_symlinks=False): visit(e.path,name)
                    else:
                        raw=open(e.path,'rb').read(4097)
                        if raw.startswith(b'ref: '): values[name]=(None,raw[5:].rstrip(b'\n'))
                        elif re.fullmatch(rb'[0-9a-f]{%d}\n?'%self.width,raw): values[name]=(raw.strip().decode(),None)
                        else: R.fail('invalid-ref',23)
        visit(directory+b'/refs',b'refs'); return values
    def head(self):
        if self.context['backend']=='reftable':
            records=self.refs()
            if self.head_value is None: R.fail('incomplete-metadata',23)
            oid,target=self.head_value
            if target is not None:
                if not valid_ref(target): R.fail('invalid-ref',23)
                oid=next((r['oid'] for r in records if R.unb64(r['name_b64'])==target),None)
            return {'symbolic_target_b64':None if target is None else R.b64(target),'oid':oid}
        raw=open(self.gd+b'/HEAD','rb').read(4097)
        if raw.startswith(b'ref: '):
            target=raw[5:].rstrip(b'\n')
            if not valid_ref(target): R.fail('invalid-ref',23)
            value=next((r['oid'] for r in self.refs() if R.unb64(r['name_b64'])==target),None)
            return {'symbolic_target_b64':R.b64(target),'oid':value}
        if not re.fullmatch(rb'[0-9a-f]{%d}\n?'%self.width,raw): R.fail('invalid-ref',23)
        return {'symbolic_target_b64':None,'oid':raw.strip().decode()}
