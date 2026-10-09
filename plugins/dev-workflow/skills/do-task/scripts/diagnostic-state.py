"""保持したstateのbytes形式と入口だけを、Git無しで検査する。"""
import os
import re
import stat

R=None

def configure(runtime):
    global R
    R=runtime

def cfield(raw):
    if not raw.startswith(b'"'):
        if b'\0' in raw or b'\n' in raw or b'\t' in raw: R.fail('state-schema',22)
        return raw
    if len(raw)<2 or raw[-1:]!=b'"': R.fail('state-schema',22)
    out=bytearray(); i=1
    escapes={ord('a'):7,ord('b'):8,ord('t'):9,ord('n'):10,ord('v'):11,ord('f'):12,ord('r'):13,34:34,92:92}
    while i<len(raw)-1:
        c=raw[i]; i+=1
        if c==34: R.fail('state-schema',22)
        if c==92:
            if i>=len(raw)-1: R.fail('state-schema',22)
            c=raw[i]; i+=1
            if c in escapes: c=escapes[c]
            elif 48<=c<=55:
                digits=bytes([c])+raw[i:i+2]
                if len(digits)!=3 or any(n<48 or n>55 for n in digits): R.fail('state-schema',22)
                c=int(digits,8); i+=2
                if c>255: R.fail('state-schema',22)
            else: R.fail('state-schema',22)
        if c==0: R.fail('state-schema',22)
        out.append(c)
    return bytes(out)

def lines(raw):
    if raw and not raw.endswith(b'\n'): R.fail('state-schema',22)
    return raw.split(b'\n')[:-1]

def integer(raw,minimum=0,maximum=2000):
    if not re.fullmatch(rb'-?[0-9]+',raw): R.fail('state-schema',22)
    n=int(raw)
    if not minimum<=n<=maximum: R.fail('state-schema',22)
    return n

class State:
    def __init__(self,args,runtime):
        self.rt=runtime; self.reader=runtime.reader; self.budget=runtime.budget
        self.path=os.path.realpath(os.fsencode(args.state)); self.cwd=os.path.realpath(os.fsencode(args.cwd))
        given=os.path.abspath(os.fsencode(args.state))
        s=self.reader.lstat(given,False)
        if not stat.S_ISDIR(s.st_mode) or stat.S_IMODE(s.st_mode)!=0o700 or not os.path.basename(given).startswith(b'guard-'): R.fail('state-binding',22)
        self.reader.owner(given,s)
        if given!=self.path: R.fail('state-binding',22)
        self.binding={'manifest_sha256':args.manifest_sha256,'snapshot_sha256':args.snapshot_sha256}
        raw=self.reader.read(self.path+b'/manifest.tsv')
        if R.digest(raw)!=args.manifest_sha256: R.fail('state-binding',22)
        self.manifest(raw)
        snapshot=self.path+b'/snapshot'; aggregate=bytearray(); records={}
        for name in self.reader.names(snapshot):
            if name.startswith(b'.'): continue
            data=self.reader.read(snapshot+b'/'+name)
            aggregate.extend(R.digest(data).encode()+b'  '+name+b'\n')
            if name in (b'4-meta.txt',b'config-contexts.txt',b'config-origins.txt'): records[name]=data
        if R.digest(aggregate)!=args.snapshot_sha256: R.fail('state-binding',22)
        if set(records)!={b'4-meta.txt',b'config-contexts.txt',b'config-origins.txt'}: R.fail('old-state',22)
        self.meta={}
        for line in lines(records[b'4-meta.txt']):
            fields=line.split(b'\t',1)
            if len(fields)!=2: R.fail('state-schema',22)
            k,v=fields
            if k in (b'cwd',b'toplevel'):
                if k in self.meta: R.fail('state-schema',22)
                self.meta[k]=cfield(v)
        if self.meta.get(b'cwd')!=self.cwd or not self.cwd.startswith(b'/'): R.fail('state-binding',22)
        self.contexts=[]; roots=[]
        rows=lines(records[b'config-contexts.txt'])
        if not rows or rows.pop(0)!=b'context-plan-v1': R.fail('old-state',22)
        for row in rows:
            f=row.split(b'\t')
            if f[0]==b'context' and len(f)==15:
                n=integer(f[1],0,1999); parent=integer(f[2],-1,1999); depth=integer(f[3],0,40); active=integer(f[4],0,1)
                if n!=len(self.contexts) or (n==0 and parent!=-1) or (n>0 and not 0<=parent<n): R.fail('state-schema',22)
                if depth!=(0 if parent<0 else self.contexts[parent]['depth']+1): R.fail('state-schema',22)
                wt,gd,common=map(cfield,f[5:8]); fmt,backend=f[8:10]
                if fmt not in (b'sha1',b'sha256') or backend not in (b'files',b'reftable'): R.fail('unsupported-format',23)
                if not wt.startswith(b'/') or any(p!=b'-' and not p.startswith(b'/') for p in (gd,common)): R.fail('state-schema',22)
                width=40 if fmt==b'sha1' else 64
                if f[10]!=b'-' and not re.fullmatch(rb'[0-9a-f]{%d}'%width,f[10]): R.fail('state-schema',22)
                integer(f[11],0,16*R.MIB)
                if f[12] not in (b'none',b'file',b'blob',b'unmerged'): R.fail('state-schema',22)
                self.contexts.append(dict(id=n,parent=parent,depth=depth,active=bool(active),wt=wt,gd=gd,common=common,
                                          format=fmt.decode(),backend=backend.decode(),held=None if f[10]==b'-' else f[10].decode()))
            elif f[0]==b'root' and len(f)==3:
                pair=f[1].split(b':')
                if len(pair)!=2: R.fail('state-schema',22)
                roots.append(tuple(integer(x,0,1999) for x in pair)); p=cfield(f[2])
                if not p.startswith(b'/'): R.fail('state-schema',22)
            else: R.fail('state-schema',22)
        if not self.contexts or self.contexts[0]['wt']!=self.meta.get(b'toplevel') or any(max(p)>=len(self.contexts) for p in roots): R.fail('state-schema',22)
        if not 0<=args.context_id<len(self.contexts) or not self.contexts[args.context_id]['active']: R.fail('context-binding',22)
        self.origin_bytes=records[b'config-origins.txt']
        self.origins(self.origin_bytes)
        self.management=self.management_binding()
    def manifest(self,raw):
        rows=lines(raw)
        if not rows: R.fail('state-schema',22)
        for n,row in enumerate(rows):
            self.budget.check(); f=row.split(b'\t')
            if len(f)!=4 or (n==0 and f[0]!=b'T') or (n>0 and f[0] not in (b'f',b'l',b'o',b'u')): R.fail('state-schema',22)
            kind,mode,value,path=f
            if not re.fullmatch(rb'[0-7]{3,4}|-',mode): R.fail('state-schema',22)
            if kind in (b'T',b'f') and not re.fullmatch(rb'[0-9a-f]{64}',value): R.fail('state-schema',22)
            if kind==b'l': cfield(value)
            if kind in (b'o',b'u') and value!=b'-': R.fail('state-schema',22)
            path=cfield(path)
            if not path or (n==0)!=path.startswith(b'/'): R.fail('state-schema',22)
    def origins(self,raw):
        rows=lines(raw)
        if not rows or rows.pop(0)!=b'config-origins-v3': R.fail('old-state',22)
        groups={}; current=None; admin=None
        for row in rows:
            f=row.split(b'\t'); tag=f[0]
            if tag==b'candidate' and len(f)==2:
                current=cfield(f[1])
                if current in groups: R.fail('state-schema',22)
                groups[current]=[]
            elif tag in (b'node',b'missing',b'data'):
                if current is None: R.fail('state-schema',22)
                if len(f)!={b'node':5,b'missing':2,b'data':2}[tag]: R.fail('state-schema',22)
                groups[current].append(f)
            elif tag==b'plan-admin' and len(f)==3:
                if admin is not None: R.fail('state-schema',22)
                admin=tuple(cfield(x) for x in f[1:])
            elif tag==b'plan-root' and len(f)==2: cfield(f[1])
            elif tag==b'env' and len(f)==4: pass
            else: R.fail('state-schema',22)
        if admin is None: R.fail('old-state',22)
        entries={}
        for path in groups:
            self.budget.check()
            entries.setdefault(os.path.realpath(path),[]).append(path)
        def choices(path,admin=False):
            # gitfileの相対gitdirは生成元が..を含む論理名で記録する。
            # 2回目の照合ではcontextが物理名でも、元の全node/link/hashを検査する。
            names=(path,path+b'/.implement-guard-admin-entry') if admin else (path,)
            result={p for p in names if p in groups}
            for name in names:
                result.update(entries.get(os.path.realpath(name),[]))
            if not result: R.fail('entry-binding',22)
            return result
        wanted=set()
        for c in self.contexts:
            if not c['active']: continue
            for path in (c['wt']+b'/.git',c['gd'],c['common']):
                wanted.update(choices(path,True))
            wanted.update(choices(c['gd']+b'/commondir'))
        for name in wanted:
            if name not in groups: R.fail('entry-binding',22)
            last=None
            for f in groups[name]:
                if f[0]==b'node':
                    kind,ident,link,path=f[1],f[2],cfield(f[3]),cfield(f[4])
                    try: s=os.lstat(path)
                    except OSError: R.fail('entry-binding',22)
                    if ident!=('%d:%d'%(s.st_dev,s.st_ino)).encode() and kind!=b'f': R.fail('entry-binding',22)
                    actual=b'l' if stat.S_ISLNK(s.st_mode) else b'd' if stat.S_ISDIR(s.st_mode) else b'f' if stat.S_ISREG(s.st_mode) else b'?'
                    if actual!=kind: R.fail('entry-binding',22)
                    if kind==b'l' and os.readlink(path)!=link: R.fail('entry-binding',22)
                    last=os.path.realpath(path)
                    if kind==b'd': fd=self.reader.directory(last); os.close(fd)
                elif f[0]==b'missing':
                    path=cfield(f[1])
                    if os.path.lexists(path): R.fail('entry-binding',22)
                    self.reader.missing.add(path)
                elif f[0]==b'data':
                    if last is None or R.digest(self.reader.read(last))!=f[1].decode('ascii'): R.fail('entry-binding',22)
        for c in self.contexts:
            if c['active']:
                for key in ('wt','gd','common'):
                    p=os.path.realpath(c[key]); fd=self.reader.directory(p); os.close(fd); c[key]=p
    def management_binding(self):
        root=self.contexts[0]['wt']
        if os.path.commonpath((root,self.cwd))!=root: R.fail('management-binding',22)
        prefix=os.path.relpath(self.cwd,root)
        if prefix==b'.': prefix=b''
        current=root; chain=[]
        for name in prefix.split(b'/') if prefix else []:
            current=os.path.join(current,name); fd=self.reader.directory(current)
            try: info=os.fstat(fd)
            finally: os.close(fd)
            chain.append({'component_b64':R.b64(name),'dev':info.st_dev,'ino':info.st_ino,'type':'directory'})
        return {'context_id':0,'relative_prefix_b64':R.b64(prefix),'identity_chain':chain}
    def ancestors(self,n):
        result=[]
        while n>=0:
            result.append(n); n=self.contexts[n]['parent']
        result.reverse(); return result
