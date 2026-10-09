"""診断で共有する有限読取・直接子監督。元データへの書込みは持たない。"""
import base64
import hashlib
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

MIB = 1024 * 1024
LIMITS = {'management_copy':200*MIB, 'management_output':200*MIB,
          'management_entries':2000, 'worktree_entries':200000,
          'body':1024*MIB, 'patch_copy':200*MIB, 'git_output':200*MIB,
          'stderr':MIB, 'json':280*MIB}

class Failure(Exception):
    def __init__(self, reason, code=20):
        self.reason, self.code = reason, code
        super().__init__(reason)

def fail(reason, code=20):
    raise Failure(reason, code)

def b64(value):
    return base64.b64encode(value).decode('ascii')

def unb64(value):
    if type(value) is not str:
        fail('schema', 2)
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError):
        fail('schema', 2)
    if b64(raw) != value or b'\0' in raw:
        fail('schema', 2)
    return raw

def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

def digest(value):
    return hashlib.sha256(value).hexdigest()

def unique(pairs):
    result = {}
    for k,v in pairs:
        if k in result: fail('schema', 2)
        result[k] = v
    return result

def decode(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda _: fail('schema',2))
    except (ValueError, UnicodeError):
        fail('schema',2)

def keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()): fail('schema',2)

def fingerprint(s):
    return (s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns)

def metadata(s):
    return dict(zip(('dev','ino','mode','size','mtime_ns','ctime_ns'),fingerprint(s)),type='regular')

class Budget:
    def __init__(self, clock=None, seconds=300.0, limits=None):
        self.clock = clock or time.monotonic
        self.deadline = self.clock()+seconds
        self.limits = dict(LIMITS if limits is None else limits)
        self.used = {}
        self.stopped = False
    def remaining(self):
        return max(0.0,self.deadline-self.clock())
    def check(self):
        if self.stopped: fail('interrupted')
        if self.remaining() <= 0: fail('time-limit')
    def charge(self, kind, count):
        self.check()
        self.used[kind] = self.used.get(kind,0)+count
        if self.used[kind] > self.limits[kind]: fail(kind+'-limit')

class Reader:
    def __init__(self, budget):
        self.budget=budget
        self.files={}
        self.dirs={}
        self.missing=set()
        self.sets={}
        self.owners={}
    def owner(self,path,info=None,code=22):
        path=os.path.abspath(os.fsencode(path))
        info=self.lstat(path,False) if info is None else info
        if info.st_uid!=os.geteuid(): fail('ownership',code)
        self.owners[path]=(info.st_uid,code)
    def check_owner(self,path,info):
        held=self.owners.get(path)
        if held is not None and info.st_uid!=held[0]: fail('ownership',held[1])
    def directory(self,path):
        path=os.path.abspath(os.fsencode(path))
        fd=os.open(b'/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        current=b'/'
        try:
            for part in path.split(b'/'):
                if not part: continue
                self.budget.check()
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
                os.close(fd); fd=child; current=os.path.join(current,part)
                info=os.fstat(fd); self.check_owner(current,info); identity=(info.st_dev,info.st_ino,stat.S_IFMT(info.st_mode))
                if current in self.dirs and self.dirs[current]!=identity: fail('source-changed')
                self.dirs[current]=identity
            return fd
        except BaseException:
            os.close(fd); raise
    def lstat(self,path,missing=True):
        path=os.fsencode(path)
        try:
            fd=self.directory(os.path.dirname(path))
        except FileNotFoundError:
            if missing: self.missing.add(path); return None
            raise
        try:
            try: return os.stat(os.path.basename(path),dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:
                if missing: self.missing.add(path); return None
                raise
        finally: os.close(fd)
    def read(self,path,limit=16*MIB,kind='management_copy',missing=False):
        path=os.fsencode(path)
        try: parent=self.directory(os.path.dirname(path))
        except FileNotFoundError:
            if missing: self.missing.add(path); return None
            raise
        fd=None
        try:
            try: before=os.stat(os.path.basename(path),dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:
                if missing: self.missing.add(path); return None
                raise
            self.check_owner(path,before)
            if not stat.S_ISREG(before.st_mode): fail('not-regular',23)
            if before.st_size>limit: fail('file-limit')
            fd=os.open(os.path.basename(path),os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
            if fingerprint(before)!=fingerprint(os.fstat(fd)): fail('source-changed')
            chunks=[]; size=0
            while True:
                self.budget.check(); block=os.read(fd,min(65536,limit-size+1))
                if not block: break
                size+=len(block)
                if size>limit: fail('file-limit')
                if kind: self.budget.charge(kind,len(block))
                chunks.append(block)
            after=os.fstat(fd); self.check_owner(path,after)
            if fingerprint(before)!=fingerprint(after) or size!=before.st_size: fail('source-changed')
            if fingerprint(os.stat(os.path.basename(path),dir_fd=parent,follow_symlinks=False))!=fingerprint(before): fail('source-changed')
            raw=b''.join(chunks)
            old=self.files.get(path)
            now=(fingerprint(before),digest(raw))
            if old is not None and old!=now: fail('source-changed')
            self.files[path]=now
            return raw
        finally:
            if fd is not None: os.close(fd)
            os.close(parent)
    def names(self,path,kind='management_entries',record=True):
        fd=self.directory(path)
        try:
            values=[]
            with os.scandir(fd) as entries:
                for entry in entries:
                    self.budget.charge(kind,1); values.append(os.fsencode(entry.name))
            values.sort()
            if record:
                path=os.fsencode(path)
                if path in self.sets and self.sets[path]!=values: fail('source-changed')
                self.sets[path]=values
            return values
        finally: os.close(fd)
    def verify(self):
        for path in self.owners:
            self.budget.check(); self.check_owner(path,self.lstat(path,False))
        for path,expected in list(self.dirs.items()):
            self.budget.check(); fd=self.directory(path)
            try:
                s=os.fstat(fd)
                if (s.st_dev,s.st_ino,stat.S_IFMT(s.st_mode))!=expected: fail('source-changed')
            finally: os.close(fd)
        for path,(expected,_) in self.files.items():
            self.budget.check(); s=self.lstat(path,False)
            if fingerprint(s)!=expected: fail('source-changed')
        for path in self.missing:
            self.budget.check()
            if self.lstat(path) is not None: fail('source-changed')
        for path,names in self.sets.items():
            fd=self.directory(path)
            try:
                found=[]
                with os.scandir(fd) as entries:
                    for e in entries:
                        self.budget.check(); found.append(os.fsencode(e.name))
                        if len(found)>len(names): fail('source-changed')
                if sorted(found)!=names: fail('source-changed')
            finally: os.close(fd)

class Runtime:
    def __init__(self,budget=None):
        self.budget=budget or Budget()
        self.reader=Reader(self.budget)
        self.children=[]; self.unreaped=False; self.temp=None; self.output_started=False
        self.handlers={}
        self.reap_deadline=None; self.cleanup_deadline=None
        for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):
            self.handlers[sig]=signal.signal(sig,self.stop)
    def stop(self,*_): self.budget.stopped=True
    def check_api(self):
        if not callable(getattr(os,'geteuid',None)): fail('runtime-unavailable')
        if sys.version_info<(3,10): fail('runtime-unavailable')
        for name in ('O_DIRECTORY','O_NOFOLLOW','O_NONBLOCK','O_CLOEXEC'):
            if not hasattr(os,name): fail('runtime-unavailable')
        for fn in (os.open,os.stat,os.readlink):
            if fn not in os.supports_dir_fd: fail('runtime-unavailable')
        if os.stat not in os.supports_follow_symlinks or os.scandir not in os.supports_fd or not callable(getattr(signal,'pthread_sigmask',None)): fail('runtime-unavailable')
    def check_runtime(self):
        self.check_api()
        # gettempdir()は候補を試し書きするため、未検査の環境指定には使わない。
        protected=[os.path.realpath(os.fsencode(p)) for p in getattr(self,'protected_roots',())]
        candidates=[os.environ.get(k) for k in ('TMPDIR','TEMP','TMP')]+['/tmp','/var/tmp','/usr/tmp']
        for candidate in candidates:
            self.budget.check()
            if not candidate: continue
            parent=os.path.realpath(os.fsencode(candidate))
            if any(os.path.commonpath((parent,root))==root for root in protected): continue
            try:
                fd=self.reader.directory(parent)
                os.close(fd)
                self.temp=os.fsencode(tempfile.mkdtemp(prefix='diagnostic-',dir=os.fsdecode(parent)))
                break
            except OSError: continue
        if self.temp is None: fail('runtime-unavailable')
        os.chmod(self.temp,0o700)
        fd=self.reader.directory(self.temp)
        try:
            probe=os.open(b'probe',os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=fd)
            try:
                os.write(probe,b'ok'); os.lseek(probe,0,0)
                if os.read(probe,2)!=b'ok': fail('runtime-unavailable')
            finally: os.close(probe)
            os.symlink(b'probe',b'link',dir_fd=fd)
            if os.readlink(b'link',dir_fd=fd)!=b'probe': fail('runtime-unavailable')
            with os.scandir(fd) as it: list(it)
            os.unlink(b'link',dir_fd=fd); os.unlink(b'probe',dir_fd=fd)
        finally: os.close(fd)
        self.git=shutil.which('git'); self.grep=shutil.which('grep')
        if not self.git or not self.grep: fail('runtime-unavailable')
        self.git=os.path.realpath(self.git); self.grep=os.path.realpath(self.grep)
        # 私有領域の固定入力だけで必要なGNU照合機能を確かめる。
        rc,out=self.process([self.grep,'-E','-z','-f',os.devnull],b'',env={'LC_ALL':'C','PATH':os.defpath})
        if rc not in (0,1): fail('runtime-unavailable')
    def reap(self,p):
        if p.poll() is not None: p.wait(); return
        if self.reap_deadline is None: self.reap_deadline=self.budget.clock()+10
        end=self.reap_deadline
        try: p.terminate()
        except ProcessLookupError: pass
        term=min(end,self.budget.clock()+5)
        while p.poll() is None and self.budget.clock()<term: time.sleep(min(.05,max(0,term-self.budget.clock())))
        if p.poll() is None:
            try: p.kill()
            except ProcessLookupError: pass
        while p.poll() is None and self.budget.clock()<end: time.sleep(min(.05,max(0,end-self.budget.clock())))
        if p.poll() is None: self.unreaped=True; fail('child-unreaped')
        p.wait()
    def process(self,argv,input_bytes=b'',env=None,cwd=None,timeout=30,output_kind='git_output',output_limit=None):
        self.budget.check()
        p=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           env=env,cwd=cwd or self.temp,close_fds=True)
        self.children.append(p)
        end=min(self.budget.deadline,self.budget.clock()+timeout)
        sel=selectors.DefaultSelector(); pos=0; chunks=[]; size=0
        for f,tag in ((p.stdin,'in'),(p.stdout,'out'),(p.stderr,'err')):
            os.set_blocking(f.fileno(),False); sel.register(f,selectors.EVENT_WRITE if tag=='in' else selectors.EVENT_READ,tag)
        try:
            while sel.get_map():
                self.budget.check()
                if self.budget.clock()>=end: fail('child-time-limit')
                for key,_ in sel.select(min(.05,max(0,end-self.budget.clock()))):
                    f,tag=key.fileobj,key.data
                    try:
                        if tag=='in':
                            if pos<len(input_bytes): pos+=os.write(f.fileno(),input_bytes[pos:pos+65536])
                            if pos>=len(input_bytes): sel.unregister(f); f.close()
                        else:
                            block=os.read(f.fileno(),65536)
                            if not block: sel.unregister(f); f.close(); continue
                            self.budget.charge('stderr' if tag=='err' else output_kind,len(block))
                            if tag=='out':
                                size+=len(block)
                                if output_limit is not None and size>output_limit: fail('output-limit')
                                chunks.append(block)
                    except (BlockingIOError,InterruptedError): continue
                    except BrokenPipeError:
                        sel.unregister(f); f.close()
            while p.poll() is None:
                self.budget.check()
                if self.budget.clock()>=end: fail('child-time-limit')
                time.sleep(.01)
            return p.wait(),b''.join(chunks)
        finally:
            sel.close()
            for f in (p.stdin,p.stdout,p.stderr):
                if not f.closed: f.close()
            self.reap(p)
    def cleanup(self):
        for p in self.children:
            if p.poll() is None: self.reap(p)
        if self.unreaped: fail('child-unreaped')
        if self.temp is not None:
            if self.cleanup_deadline is None: self.cleanup_deadline=self.budget.clock()+5
            end=self.cleanup_deadline
            def remove(path):
                if self.budget.clock()>=end: fail('cleanup-time-limit')
                with os.scandir(path) as it:
                    for entry in it:
                        if self.budget.clock()>=end: fail('cleanup-time-limit')
                        if entry.is_dir(follow_symlinks=False): remove(entry.path)
                        else: os.unlink(entry.path)
                os.rmdir(path)
            remove(self.temp); self.temp=None
    def response_bytes(self,value):
        chunks=[]
        for text in json.JSONEncoder(ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).iterencode(value):
            block=text.encode('utf-8'); self.budget.charge('json',len(block)); chunks.append(block)
        self.budget.charge('json',1)
        return b''.join(chunks)+b'\n'
    def emit(self,raw,code,writer):
        if self.output_started: return 20
        self.output_started=True
        p=None
        try:
            if code:
                self.budget.used['stderr']=self.budget.used.get('stderr',0)+len(raw)
                if self.budget.used['stderr']>self.budget.limits['stderr']: return 20
            self.budget.check()
            p=subprocess.Popen([sys.executable,'-I','-B',writer,'stderr' if code else 'stdout'],stdin=subprocess.PIPE,close_fds=True)
            self.children.append(p); os.set_blocking(p.stdin.fileno(),False)
            sel=selectors.DefaultSelector(); sel.register(p.stdin,selectors.EVENT_WRITE)
            pos=0
            try:
                while pos<len(raw):
                    self.budget.check()
                    if p.poll() is not None: fail('output-failed')
                    if sel.select(min(.05,self.budget.remaining())):
                        try: pos+=os.write(p.stdin.fileno(),raw[pos:pos+65536])
                        except (BlockingIOError,InterruptedError): continue
                p.stdin.close()
                while p.poll() is None:
                    self.budget.check(); time.sleep(min(.05,self.budget.remaining()))
                if p.wait()!=0: fail('output-failed')
            finally:
                sel.close()
                if not p.stdin.closed: p.stdin.close()
            old=signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGTERM,signal.SIGINT,signal.SIGHUP})
            try:
                self.budget.check(); result=code
            finally:
                try: signal.pthread_sigmask(signal.SIG_SETMASK,old)
                except (OSError,ValueError): pass
            return result
        except BaseException:
            if p is not None:
                try: self.reap(p)
                except BaseException: pass
            return 20
