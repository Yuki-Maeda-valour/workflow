#!/usr/bin/env python3
"""S2L の引数404件とPTY40件。旧CLIを同じ物理入力で直接起動する。"""
from __future__ import annotations
import errno,fcntl,hashlib,json,os,pty,select,signal,struct,subprocess,sys,tempfile,termios,threading,time
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]
LEGACY=ROOT/'plugins/dev-workflow/skills/ship-task/scripts/loop-state.py'

def environment():
    import check_loop_state as check
    base={k:v for k,v in os.environb.items() if not k.startswith(b'PYTHON') and k not in (b'LC_ALL',b'LC_CTYPE',b'LANG',b'COLUMNS',b'LINES')}
    base.update({b'LC_ALL':b'C',b'LANG':b'C',b'COLUMNS':b'80',b'LINES':b'24'})
    assert yaml.__version__=='6.0.3', 'S2L_PYYAML_BASELINE'
    # 外す変数(22:31・00:28 の決定)を除き、PyYAML の置き場と文字コードの設定だけを与える。
    return check.child_environment(base,keep_case=False,given={'PYTHONUTF8':'1','PYTHONCOERCECLOCALE':'0','PYTHONIOENCODING':'utf-8:surrogateescape'})

def guard(env):
    import check_loop_state as check
    if env is not None:check.verify_child_environment(env,{})

def run(command,env,cwd):
    guard(env)
    child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,cwd=cwd,start_new_session=True)
    try:
        stdout,stderr=child.communicate(timeout=8)
    except BaseException:
        if child.poll() is None:os.killpg(child.pid,signal.SIGKILL)
        child.communicate(timeout=3)
        raise
    return subprocess.CompletedProcess(command,child.returncode,stdout,stderr)

def identity(path):
    st=path.lstat()
    return (st.st_dev,st.st_ino,st.st_mode,st.st_size,st.st_mtime_ns,st.st_ctime_ns,path.read_bytes())

def equal(label,old,new):
    for field in ('returncode','stdout','stderr'):
        if getattr(old,field)!=getattr(new,field):
            raise AssertionError(f'CLI_ARGUMENT_{field.upper()}: {label}: old={getattr(old,field)!r} new={getattr(new,field)!r}')

def argument_cases(TMP,boundaries):
    import check_loop_state as check
    A=TMP/'state-a'; A.write_bytes(b'{"probe":"a"}\n')
    B=TMP/'state-b'; B.write_bytes(b'{"probe":"b"}\n')
    BAD=TMP/'state-raw-\udcff'
    # Mac の APFS は UTF-8 でない名前を作れない。そのときだけ作らずに、同じ引数(生のバイトのパス)で比べる。
    if not check.create_raw(BAD,lambda p:p.write_bytes(b'raw-path\xff\n')):
        boundaries.append(check.os_boundary('arguments-state-raw','not_created','none'))
    M=TMP/'missing'
    cases=[]
    def b(v):return v if isinstance(v,bytes) else os.fsencode(v)
    def add(label,args,env=None,group='syntax'):
        assert not any(c['label']==label for c in cases),label
        cases.append(dict(label=label,args=[b(x) for x in args],env={b(k):None if v is None else b(v) for k,v in (env or {}).items()},group=group))
    def dig(*tail):return ['state-digest','--state',A,*tail]
    commands=['snapshot','stop-control','preflight','derive-removed-worktree','state-digest','state-digests','compare']
    valid={
    'snapshot':['--out',TMP/'out','--top',TMP,'--common',TMP,'--repo-admin',TMP],
    'stop-control':['--state',A],
    'preflight':['--state',A,'--top',TMP,'--common',TMP,'--repo-admin',TMP],
    'derive-removed-worktree':['--state',A,'--out',TMP/'out','--removed-worktree',TMP,'--own-worktree',TMP],
    'state-digest':['--state',A], 'state-digests':['--state',A],
    'compare':['--before',A,'--after',B]}
    for n,args in enumerate([[],['--help'],['-h'],['--h'],['--he'],['--hel'],['-hh'],['-hhh'],['-hfoo'],['-h=foo'],['--help=foo'],['--help','--bad'],['--bad','--help'],['--bad'],['--'],['--','--help'],['--','state-digest','--state',A],['--bad','state-digest','--state',A],['-x','state-digest','--state',A],['state-di'],['STATE-DIGEST'],[''],[b'\xff'],['-1'],['--help','--hel=bad'],['--bad','what'],['--help','what']]):
     add(f'root-{n:02}',args,group='root')
    for cmd in commands:
     add(f'{cmd}-missing',[cmd],group='required')
     add(f'{cmd}-help',[cmd,'--help'],group='help')
     add(f'{cmd}-help-abbrev-he',[cmd,'--he'],group='help')
     add(f'{cmd}-help-abbrev-hel',[cmd,'--hel'],group='help')
     add(f'{cmd}-help-short-concat',[cmd,'-hh'],group='help')
     add(f'{cmd}-help-before-ambiguous',[cmd,'--help','--max'],group='precedence')
     add(f'{cmd}-help-before-missing-value',[cmd,'--help','--max-items'],group='precedence')
     add(f'{cmd}-help-before-invalid-int',[cmd,'--help','--max-items','bad'],group='precedence')
     add(f'{cmd}-missing-with-unknown',[cmd,'--unknown'],group='required')
     add(f'{cmd}-valid-required-stop',[cmd,*valid[cmd],'--max-items','0'],group='required')
    for n,args in enumerate([
     ['state-digest','--help=foo'],['state-digest','--h=foo'],['state-digest','--he=foo'],['state-digest','-hfoo'],['state-digest','-h=foo'],
     ['state-digest','--hel','--max'], ['state-digest','--unknown','--help'], ['state-digest','--max','--help'],
     ['state-digest','--state','--help'],['state-digest','--state=--help'], ['state-digest','--state'],['state-digest','--sta'],
     ['state-digest','--st',A],['state-digest','--sta='+str(A)],['state-digest','--state',A,'--max-i','0'],
     ['state-digest','--state',A,'--max','0'],['state-digest','--state',A,'--max-=0'],['state-digest','--state',A,'--m=0'],
     ['state-digest','--state',A,'--state',B],['state-digest','--state',M,'--state',A],['state-digest','--state',A,'--state',M],
     ['state-digests','--state',A,'--state',B,'--state',A], ['state-digest','--state',A,'--max-items','1','--max-items','0'],
     ['state-digest','--state',A,'--max-items','0','--max-items','1'],['state-digest','--state',A,'--max-items','bad','--max-items','1'],
     ['state-digest','--state',A,'--max-items','1','--max-items','bad'],
     ['state-digest','--state',A,'what'],['state-digest','--state',A,'--unknown'],['state-digest','--state',A,'--unknown=thing'],
     ['state-digest','--state',A,'--'],['state-digest','--state',A,'--','--help'],['state-digest','--','--state',A],
     ['state-digest','--state',A,b'\xff'],['state-digest','--state',A,b'--unknown=\xff'],['state-digest','--state',A,b'--m=\xff'],
     ['state-digest','--state',BAD],['state-digest','--state',A,'--max-items',b'\xff'],
     ['snapshot',*valid['snapshot'],'--fresh-config-baseline','--fresh-config-baseline','--max-items','0'],
     ['snapshot',*valid['snapshot'],'--fresh-config-baseline=1','--max-items','0'],
     ['snapshot','--stop'],['snapshot','--s'],['snapshot','--stop-control-n=x','--help'],
     ['compare','--s'],['compare','--self'],['compare','--self-r=x','--help'],
    ]):add(f'options-{n:02}',args,group='options')
    integers=['1','0','-0','+0','-1','+1','00','01','000_001','1_0','+1_0','-1_0','_1','1_','1__0','+','-','',
    ' 1','1 ','\t1\n','\r\v\f1\r\v\f','\u00a01\u00a0','\u00851\u0085','\u001c1','1\u001f',
    '١','１２','१२','²','١_٢','+１２','-١','-١.٢','-１２.５','-1.5','-.5','-1.','1.0','1e3','-1e3','0x10','-0x10','--1','- 1','-\t1',
    '1\n','-1\n','-1\n\n','-1_0','+ 1','  +001_000  ',str(2**63-1),str(2**63),str(2**64-1),str(2**64),str(2**128),'9'*400]
    for n,v in enumerate(integers):
     add(f'int-separated-{n:02}',dig('--max-items',v),group='integer')
     if v.startswith('-') or v in ('','\u001c1','1\u001f'):
      add(f'int-explicit-{n:02}',dig('--max-items='+v),group='integer')
    for width in [None,'','bad','-1','0','1','2','3','4','5','10','20','23','24','25','26','27','28','30','32','33','34','35','36','40','50','60','78','79','80','81','100','120','200','9999','+40',' 40 ','٤٠','4_0',str(2**63),str(2**64)]:
     for cmd in ['', 'snapshot','state-digest']:
      add(f'width-{cmd or "root"}-{repr(width)}',([cmd] if cmd else [])+['--help'],{'COLUMNS':width},group='width')
    for n,v in enumerate([None,'','0','00','+0','-0',' 0','0 ','640','639','+640',' 640','\t640','640 ','0640','0000640','-640','4300','2147483647','2147483648','4294967296','bad','６４０','640\n',b'\xff']):
     add(f'intenv-{n:02}',['--help'],{'PYTHONINTMAXSTRDIGITS':v},group='intenv')
    for envlimit,n in [(None,4299),(None,4300),(None,4301),('640',639),('640',640),('640',641),('0',4301),('641',641),('641',642)]:
     for kind,value in [('nine','9'*n),('zero','0'*(n-1)+'1'),('unicode','١'*n)]:
      add(f'intlimit-{envlimit}-{n}-{kind}',dig('--max-items',value),{'PYTHONINTMAXSTRDIGITS':envlimit},group='intlimit')
    for limit,n in [('640',640),('640',641),('0',4301),('4300',4301)]:
     for cmd in ['', 'snapshot']:
      add(f'width-intlimit-{limit}-{n}-{cmd or "root"}',([cmd] if cmd else [])+['--help'],{'PYTHONINTMAXSTRDIGITS':limit,'COLUMNS':'0'*(n-2)+'40'},group='width-intlimit')
    assert len(cases)==404, len(cases)
    return cases

def run_pty(prefix,args,env,cwd,mode,width,label=''):
 """疑似端末の 1 回の起動。子が動いている間も主側(master)を読み続ける。

 Mac の疑似端末は、読まれずに溜められる量が Linux より小さい。子が終わってから読むと、
 `--help` のような出力で子が書込みで止まり、時間切れになる。旧版と Rust に同じ読み方を当てる。
 """
 master=slave=None;p=None;timed=False;start=time.monotonic();chunks=[];failures=[];done=threading.Event();reader=None
 def drain():
  try:
   while True:
    finished=done.is_set()
    ready,_,_=select.select([master],[],[],0.02)
    if ready:
     try:chunk=os.read(master,65536)
     except BlockingIOError:chunk=None
     except OSError as e:
      if e.errno==errno.EIO:return
      raise
     if chunk:chunks.append(chunk);continue
     if chunk==b'':return
    if finished:return
  except BaseException as e:failures.append(e)
 try:
  master,slave=pty.openpty()
  attrs=termios.tcgetattr(slave);attrs[1]&=~termios.OPOST
  termios.tcsetattr(slave,termios.TCSANOW,attrs)
  fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,width,0,0))
  actual=list(struct.unpack('HHHH',fcntl.ioctl(slave,termios.TIOCGWINSZ,b'\0'*8)))
  assert actual==[24,width,0,0]
  guard(env)
  os.set_blocking(master,False)
  p=subprocess.Popen([*prefix,*args],stdin=subprocess.DEVNULL,stdout=slave if mode=='stdout-pty' else subprocess.PIPE,stderr=subprocess.PIPE if mode=='stdout-pty' else slave,env=env,cwd=cwd,start_new_session=True)
  reader=threading.Thread(target=drain,daemon=True);reader.start()
  try:out,err=p.communicate(timeout=5)
  except subprocess.TimeoutExpired:
   timed=True;os.killpg(p.pid,signal.SIGKILL);out,err=p.communicate(timeout=2)
  # 子が終わった後に溜まっている分まで読んでから止める。
  done.set();reader.join(timeout=5)
  capture=b''.join(chunks);piped=out if mode=='stderr-pty' else err
  if mode=='stdout-pty':out=capture
  else:err=capture
  import check_loop_state as check
  try:absent,group=check.process_group(p.pid)
  except OSError as e:absent=False;group=f'{type(e).__name__} errno={e.errno}'
  error=repr(failures[0]) if failures else 'None'
  detail=(f'{label or " ".join(map(os.fsdecode,args))} mode={mode} width={width} timed={timed} absent={absent} killpg={group} '
          f'seconds={time.monotonic()-start:.3f} returncode={p.returncode} pty_bytes={len(capture)} pipe_bytes={len(piped or b"")} '
          f'reader_alive={reader.is_alive()} reader_error={error}')
  assert not timed and absent and not reader.is_alive() and not failures, 'PTY_TIMEOUT_OR_CHILD_REMAINS: '+detail
  return subprocess.CompletedProcess([*prefix,*args],p.returncode,out,err)
 finally:
  if p is not None and p.poll() is None:
   os.killpg(p.pid,signal.SIGKILL);p.communicate(timeout=2)
  done.set()
  if reader is not None:reader.join(timeout=5)
  for fd in (master,slave):
   if fd is not None:os.close(fd)

def arguments(fixture):
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='s2l-arguments-') as temporary:
        # Mac の一時の置き場(/var/folders)は /private へのリンク。旧版は祖先のリンクを辿らないので、実体のパスで比べる。
        boundaries=[];root=Path(temporary).resolve();cases=argument_cases(root,boundaries);base=environment()
        inputs=[root/'state-a',root/'state-b']+([] if boundaries else [root/'state-raw-\udcff']);before=[identity(p)for p in inputs]
        for case in cases:
            env=base.copy()
            for key,value in case['env'].items():
                if value is None:env.pop(key,None)
                else:env[key]=value
            import check_loop_state as check
            check.verify_child_environment(env,{os.fsdecode(k):v for k,v in case['env'].items() if k.startswith(b'PYTHON')})
            check.start_case(case['label'])
            old=run([os.fsencode(sys.executable),b'-B',os.fsencode(LEGACY),*case['args']],env,root);check.record('old',old)
            new=run([os.fsencode(fixture),*case['args']],env,root)
            check.remember(case['label'],old,new);equal(case['label'],old,new)
        assert before==[identity(p)for p in inputs], 'CLI_ARGUMENT_INPUT_CHANGED'
    return {'arguments':len(cases),'skipped':0,'os_boundaries':boundaries,'seconds':round(time.monotonic()-started,3)}

def terminals(fixture):
    started=time.monotonic();cases=[]
    for setting in (None,'bad','0'):
        for width in (20,40,120):
            for command in ('root','snapshot'):
                for mode in ('stdout-pty','stderr-pty'):cases.append((setting,width,command,mode))
    for width in (20,120):
        for command in ('root','snapshot'):cases.append(('80',width,command,'stdout-pty'))
    assert len(cases)==40
    with tempfile.TemporaryDirectory(prefix='s2l-terminals-') as temporary:
        base=environment();base.pop(b'COLUMNS');known={}
        for command in ('root','snapshot'):
            for width in (20,40,80,120):
                env=base.copy();env[b'COLUMNS']=str(width).encode();args=([]if command=='root'else[command])+['--help']
                known[command,width]=run([sys.executable,'-B',str(LEGACY),*args],env,temporary)
        for setting,width,command,mode in cases:
            env=base.copy()
            if setting is not None:env[b'COLUMNS']=setting.encode()
            args=([]if command=='root'else[command])+['--help'];label=f'{mode}/{command}/{setting}/{width}'
            import check_loop_state as check
            check.start_case(label)
            old=run_pty([sys.executable,'-B',str(LEGACY)],args,env,temporary,mode,width,'old '+label);check.record('old',old)
            new=run_pty([str(fixture)],args,env,temporary,mode,width,'new '+label)
            check.remember(label,old,new);equal(label,old,new)
            effective=80 if setting=='80' or mode=='stderr-pty' else width
            equal('PTY_FIXED_WIDTH_EXPECTATION',known[command,effective],old)
    return {'pty':len(cases),'skipped':0,'seconds':round(time.monotonic()-started,3)}
