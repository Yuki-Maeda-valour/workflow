"""literal bytesの選択とsnapshot互換のGNU ERE照合。"""
import os
import re

R=None

def configure(runtime):
    global R
    R=runtime

def literal(raw,under=False):
    if not raw or len(raw)>4096 or raw.startswith(b'/') or b'\0' in raw: R.fail('path',2)
    if under and raw==b'.': return raw
    if any(x in (b'',b'.',b'..') for x in raw.split(b'/')): R.fail('path',2)
    return raw

def revision(value):
    if not isinstance(value,str) or len(value.encode('utf-8'))>1024: R.fail('revision',2)
    if value=='HEAD' or re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',value): return value
    raw=value.encode('utf-8')
    if not raw.startswith(b'refs/') or any(c<32 or c==127 or c in b' ~^:?*[\\' for c in raw) or b'..' in raw or b'@{' in raw or any(not p or p.startswith(b'.') or p.endswith(b'.lock') for p in raw.split(b'/')) or raw.endswith((b'/',b'.')): R.fail('revision',2)
    return value

def glob_ere(g):
    anchor=b'(^|/)'; raw=g.encode('utf-8')
    if raw.endswith(b'/'): raw=raw[:-1]
    if raw.startswith(b'/'): raw=raw[1:]; anchor=b'^'
    out=bytearray(); i=0
    while i<len(raw):
        if raw[i:i+3]==b'**/': out.extend(b'(.*/)?'); i+=3
        elif raw[i:i+2]==b'**': out.extend(b'.*'); i+=2
        elif raw[i:i+1]==b'*': out.extend(b'[^/]*'); i+=1
        elif raw[i:i+1]==b'?': out.extend(b'[^/]'); i+=1
        else:
            c=raw[i]
            if c in b'\\.^$+()[]{}|': out.append(92)
            out.append(c); i+=1
    return anchor+out+b'($|/)'

DEFAULT=b'(^|/)\\.claude/(reviews/|grasp\\.md$|settings\\.local\\.json$|\\.understand-project-done$)'
REP=(b'x',b'a/b/c.ts',b'.claude/x',b'src/app.ts',b'README.md')

class Matcher:
    def __init__(self,rt,state,plan,cid):
        self.rt=rt; self.state=state; self.cid=cid; self.compiled=[]; self.seq=0
        for p in plan['policies']:
            if p['context_id'] not in state.ancestors(cid): continue
            expressions=[glob_ere(g) for g in p['globs']]+[e.encode('utf-8') for e in p['eres']]
            for expr in expressions:
                if not expr or b'\0' in expr: R.fail('expression',2)
                if self.match(expr,REP)==set(REP): R.fail('all-paths-excluded',2)
            combined=b'|'.join(expressions)
            if combined and self.match(combined,REP)==set(REP): R.fail('all-paths-excluded',2)
            ancestor=state.contexts[p['context_id']]['wt']; selected=state.contexts[cid]['wt']
            if os.path.commonpath((ancestor,selected))!=ancestor: R.fail('context-binding',22)
            rel=os.path.relpath(selected,ancestor); rel=b'' if rel==b'.' else rel
            self.compiled.append((rel,R.unb64(p['root_prefix_b64']),combined))
    def match(self,expr,names):
        if not names: return set()
        self.seq+=1; file=os.path.join(self.rt.temp,('pattern-%d'%self.seq).encode())
        fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            data=expr+b'\n'; offset=0
            while offset<len(data):
                self.rt.budget.check(); count=os.write(fd,data[offset:offset+65536])
                if count<=0: R.fail('write-failed')
                offset+=count
        finally: os.close(fd)
        rc,out=self.rt.process([self.rt.grep,'-E','-z','-f',file],b''.join(n+b'\0' for n in names),
                               env={'LC_ALL':'C','PATH':os.defpath},output_kind='management_output')
        if rc not in (0,1): R.fail('expression',2)
        return set(out.split(b'\0')[:-1]) if out else set()
    def excluded(self,names):
        names=list(dict.fromkeys(names)); excluded=self.match(DEFAULT,names)
        excluded.update(n for n in names if b'.git' in n.split(b'/'))
        for relative,prefix,expr in self.compiled:
            if not expr: continue
            translated={}
            for n in names:
                v=relative+b'/'+n if relative else n
                if prefix:
                    if v==prefix: v=b''
                    elif v.startswith(prefix+b'/'): v=v[len(prefix)+1:]
                    else: continue
                translated.setdefault(v,[]).append(n)
            for v in self.match(expr,list(translated)):
                excluded.update(translated[v])
        return excluded
    def scope(self,options):
        scopes=[('path',R.unb64(n)) for n in options.get('path_b64',[])]+[('under',R.unb64(n)) for n in options.get('under_b64',[])]
        excluded=self.excluded([p for _,p in scopes])
        return [{'kind':kind,'path_b64':None if p in excluded else R.b64(p),'redacted':p in excluded} for kind,p in scopes]

def selected(path,options):
    for value in options.get('path_b64',[]):
        if path==R.unb64(value): return True
    for value in options.get('under_b64',[]):
        prefix=R.unb64(value)
        if prefix==b'.' or path.startswith(prefix+b'/'): return True
    return False
