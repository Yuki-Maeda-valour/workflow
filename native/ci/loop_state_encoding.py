#!/usr/bin/env python3
"""全7コマンド×5文字設定の通常処理。承認差は旧新それぞれの期待を検査する。"""
import copy,datetime,hashlib,json,os,pathlib,re,signal,stat,subprocess,sys,tempfile,time
import check_loop_state as check
import check_loop_state as s2l_comparator
import loop_state_arguments
ROOT=check.ROOT
OLD=check.LEGACY
MAC_PREPARE_SCRIPT="import importlib.util,json,sys\ns=importlib.util.spec_from_file_location('s2l_mac_prepare',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\ntop,common=sys.argv[2:];b=m.Budget(100000,10000000,1000000,60);c=m.config_candidates(top,common,common,b);o=[r['entry'] for r in c if r['state']=='present'];e={'effective_sha256':'0'*64,'origins':o,'candidates':c};v={'schema':7,'refs':[],'worktrees':[{'worktree':top,'branch':'refs/heads/main','locked':''}],'worktree_contents':[],'worktree_configs':[{'path':top,'git_dir':common,'effective':e}],'secret_patterns':[],'stop_control':None,'config':e,'common_admin':{},'common_config':{},'common_config_worktree':{},'repo_admin':{}};print(json.dumps(v,ensure_ascii=True,sort_keys=True,separators=(',',':')),end='')\n  "

def run(fixture):
 with tempfile.TemporaryDirectory(prefix='s2l-encoding-normal-') as temporary:
  return _run(pathlib.Path(temporary).resolve(),pathlib.Path(fixture))

def _run(TMP,NEW):
 HASH=hashlib.sha256(NEW.read_bytes()).hexdigest()
 DIR=TMP
 assert sys.version_info[:2]==(3,10) and os.geteuid()!=0
 def raw(v):return v if isinstance(v,bytes) else os.fsencode(v)
 def describe(v):return {'hex':v.hex(),'text':v.decode('utf-8','backslashreplace'),'length':len(v),'sha256':hashlib.sha256(v).hexdigest()}
 def canonical(v):return json.dumps(v,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode('ascii')
 def metadata(path):
  s=path.lstat();d={'dev':s.st_dev,'ino':s.st_ino,'mode':s.st_mode,'size':s.st_size,'mtime_ns':s.st_mtime_ns,'ctime_ns':s.st_ctime_ns}
  if stat.S_ISREG(s.st_mode):d['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
  if stat.S_ISLNK(s.st_mode):d['target_hex']=os.fsencode(os.readlink(path)).hex()
  return d
 # Mac の APFS は UTF-8 でない名前を作れない。最初の名前を実際に作って確かめ、拒んだときだけ、
 # 作るファイルとディレクトリの名前の生のバイトを ASCII の xff に置き換える。置き換えたことは行で出す。
 # そのため Mac では、その名前を値に持つ環境変数 HOME と GIT_CONFIG_GLOBAL も xff の名前になる。
 # 作らない名前(停止ファイルの相対パス・除いた worktree・JSON の中の文字列)とほかの環境変数は、生のバイトのまま比べる。
 boundaries=[];refused=not s2l_comparator.create_raw(TMP/'home-\udcff',pathlib.Path.mkdir)
 def fs(name):return name.replace('\udcff','xff') if refused else name
 if refused:boundaries.append(s2l_comparator.os_boundary('encoding-file-names','replaced','none'))
 repo=TMP/'repo';repo.mkdir();home=TMP/fs('home-\udcff')
 if refused:home.mkdir()
 xdg=TMP/'xdg';xdg.mkdir();bindir=TMP/'bin';bindir.mkdir();outputs=TMP/'outputs';outputs.mkdir();inputs=TMP/'inputs';inputs.mkdir()
 global_config=home/fs('global-\udcff');global_config.write_bytes('[s2l]\nlabel = 漢字-'.encode()+b'\xff\n')
 system_config=TMP/'system.cfg';system_config.write_bytes(b'')
 gitlog=TMP/'git-events.bin'
 wrapper=bindir/'git'
 wrapper.write_text('#!/bin/sh\nprintf \'%s\\000\' S2L-BEGIN "$S2L_PROBE_BYTES" "$HOME" "$GIT_CONFIG_GLOBAL" "$GIT_CONFIG_VALUE_0" "$@" S2L-END >> "$S2L_GIT_LOG"\nexec /usr/bin/git "$@"\n',encoding='ascii');wrapper.chmod(0o700)
 base={k:v for k,v in os.environb.items() if not k.startswith(b'PYTHON') and not k.startswith(b'GIT_') and k not in (b'HOME',b'XDG_CONFIG_HOME',b'LC_ALL',b'LC_CTYPE',b'LANG')}
 base[b'PYTHONPATH']=loop_state_arguments.environment()[b'PYTHONPATH']
 base.update({b'HOME':raw(home),b'XDG_CONFIG_HOME':raw(xdg),b'LC_ALL':b'C',b'LANG':b'C',b'PYTHONUTF8':b'1',b'PYTHONCOERCECLOCALE':b'0',b'PYTHONIOENCODING':b'utf-8:surrogateescape',b'COLUMNS':b'80',b'GIT_CONFIG_GLOBAL':raw(global_config),b'GIT_CONFIG_SYSTEM':raw(system_config),b'GIT_CONFIG_COUNT':b'1',b'GIT_CONFIG_KEY_0':b's2l.env',b'GIT_CONFIG_VALUE_0':'環境-'.encode()+b'\xff',b'S2L_PROBE_BYTES':'子環境-'.encode()+b'\xff',b'S2L_GIT_LOG':raw(gitlog),b'PATH':raw(bindir)+b':'+base.get(b'PATH',b'/usr/bin:/bin')})
 setup=[]
 def git(*args):
  command=[b'/usr/bin/git',b'-c',b'maintenance.auto=false',b'-c',b'gc.auto=0',b'-c',b'maintenance.autoDetach=false',b'-C',raw(repo),*[raw(a) for a in args]]
  p=subprocess.Popen(command,env=base,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
  try:out,err=p.communicate(timeout=10)
  except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.communicate(timeout=2);raise
  assert p.returncode==0,(args,p.returncode,out,err)
  setup.append({'argv_hex':[a.hex() for a in command],'exit':p.returncode,'stdout':describe(out),'stderr':describe(err)})
  return out
 git('init','-q','--initial-branch=main')
 (repo/'tracked').write_bytes(b'tracked\r\n\x00\xff\n')
 (repo/'private-owned.txt').write_bytes(b'OWN-PROBE-SECRET\x00\xff')
 (repo/'.claude').mkdir();(repo/'.claude/project-profile.yml').write_bytes(b'secret_paths: [private-owned.txt]\n')
 file_name=fs('資料-\udcff');(repo/file_name).write_bytes(b'\xff\xfe\x00\x00raw\r\n\x80\n')
 link_name=fs('link-漢-\udcff');os.symlink(os.fsencode(file_name),raw(repo/link_name))
 git('add','tracked');git('-c','user.name=probe','-c','user.email=probe@example.invalid','commit','-qm','base')
 admin=repo/'.git';stop_rel='停止-\udcff'
 state_out=outputs/fs('out-状態-\udcff.json')
 state_input=inputs/fs('held-状態-\udcff.json')
 payload=inputs/fs('bytes-資料-\udcff.bin');payload.write_bytes(b'\xff\xfe\x00\x00\r\nraw\x00\xff\x80\n')
 control={'schema':7,'stop_control':{'worktree':str(repo),'rel':'新規-\udcff/停止','new_dirs':['新規-\udcff']}}
 control_input=inputs/fs('control-停止-\udcff.json');control_input.write_bytes(b'\xff\xfe\x00\x00'+canonical(control).decode('ascii').encode('utf-32-le'))
 removed=str(TMP/'removed-漢-\udcff')
 derive={'schema':7,'worktrees':[{'worktree':removed}],'worktree_contents':[{'path':removed}],'worktree_configs':[{'path':removed}],'secret_patterns':[{'path':removed}],'raw_marker':'漢字-\udcff'}
 derive_input=inputs/fs('derive-除去-\udcff.json');derive_input.write_bytes(b'\xff\xfe'+canonical(derive).decode('ascii').encode('utf-16-le'))
 derived=copy.deepcopy(derive)
 for key in ('worktrees','worktree_contents','worktree_configs','secret_patterns'):derived[key]=[]
 comparison={'schema':7,'refs':[{'name':'refs/heads/漢-\udcff','oid':'0'*40,'symref':''}],'worktrees':[],'worktree_contents':[],'worktree_configs':[],'secret_patterns':[],'stop_control':None,'config':{},'common_admin':{},'common_config':{'state':'absent'},'common_config_worktree':{'state':'absent'},'repo_admin':{'path':'/self','config_worktree':{'state':'absent'}}}
 changed=copy.deepcopy(comparison);changed['stop_control']={'worktree':'/self','rel':'停止','new_dirs':[]}
 before=inputs/fs('before-状態-\udcff.json');before.write_bytes(b'\xff\xfe'+canonical(comparison).decode('ascii').encode('utf-16-le'))
 after=inputs/fs('after-状態-\udcff.json');after.write_bytes(b'\x00\x00\xfe\xff'+canonical(changed).decode('ascii').encode('utf-32-be'))
 commands={
  'snapshot':['snapshot','--out',state_out,'--top',repo,'--common',admin,'--repo-admin',admin,'--stop-control-worktree',repo,'--stop-control-rel',stop_rel],
  'stop-control':['stop-control','--state',control_input],
  'preflight':['preflight','--state',state_input,'--top',repo,'--common',admin,'--repo-admin',admin],
  'derive-removed-worktree':['derive-removed-worktree','--state',derive_input,'--out',state_out,'--removed-worktree',removed,'--own-worktree',removed],
  'state-digest':['state-digest','--state',payload],
  'state-digests':['state-digests','--state',payload,'--state',before],
  'compare':['compare','--before',before,'--after',after],
 }
 def output_state():
  if not state_out.exists():return None
  s=state_out.lstat();assert stat.S_ISREG(s.st_mode)
  return {'kind':'regular','mode':stat.S_IMODE(s.st_mode),'bytes':describe(state_out.read_bytes())}
 def clear_output():
  if state_out.exists() or state_out.is_symlink():state_out.unlink()
  assert not state_out.exists()
 def git_events(data):
  fields=data.split(b'\0');events=[];at=0
  while at<len(fields)-1:
   assert fields[at]==b'S2L-BEGIN',(at,fields[at:at+5])
   end=fields.index(b'S2L-END',at+5)
   event={'probe_hex':fields[at+1].hex(),'home_hex':fields[at+2].hex(),'global_hex':fields[at+3].hex(),'config_value_hex':fields[at+4].hex(),'argv_hex':[v.hex() for v in fields[at+5:end]]}
   event['raw_env_preserved']=fields[at+1]==base[b'S2L_PROBE_BYTES'] and fields[at+2]==base[b'HOME'] and fields[at+4]==base[b'GIT_CONFIG_VALUE_0']
   event['global_expected']=fields[at+3] in (base[b'GIT_CONFIG_GLOBAL'],b'/dev/null')
   events.append(event);at=end+1
  return events
 
 def run(who,command,setting):
  clear_output();gitlog.write_bytes(b'')
  env=base.copy();env.update(setting)
  s2l_comparator.verify_child_environment(env,{os.fsdecode(k):v for k,v in setting.items()})
  prefix=[raw(sys.executable),b'-B',raw(OLD)] if who=='legacy' else [raw(NEW)]
  start=time.monotonic();p=subprocess.Popen([*prefix,*[raw(a) for a in commands[command]]],cwd=TMP,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
  timed=False
  try:out,err=p.communicate(timeout=10)
  except subprocess.TimeoutExpired:timed=True;os.killpg(p.pid,signal.SIGKILL);out,err=p.communicate(timeout=2)
  try:os.killpg(p.pid,0);absent=False
  except ProcessLookupError:absent=True
  events=git_events(gitlog.read_bytes())
  return {'exit':p.returncode,'stdout':describe(out),'stderr':describe(err),'output':output_state(),'initial_output_absent':True,'git_events':events,'git_log':describe(gitlog.read_bytes()),'timeout':timed,'group_absent':absent,'seconds':round(time.monotonic()-start,6)}
 
 # Old successful output is the baseline oracle; explicit semantic assertions below
 # prevent a no-op or stale output from becoming the Rust expectation.
 pilot=run('legacy','snapshot',{})
 (DIR/'utf8-normal-v3-pilot.json').write_text(json.dumps({'tmp':str(TMP),'pilot':pilot,'setup':setup},ensure_ascii=True,indent=2)+'\n')
 if sys.platform=='darwin':
  # 失敗したときは、旧版の終了値と標準エラーの末尾を CI に残す(pilot の JSON は一時の置き場と一緒に消える)。
  assert pilot['exit']==20 and pilot['output'] is None and bytes.fromhex(pilot['stderr']['hex'])==b'ERROR [loop-state] '+ 'git の状態読取に失敗した\n'.encode(), 'MAC_NORMAL_SNAPSHOT_REJECTION: exit='+repr(pilot['exit'])+' stderr='+ascii(pilot['stderr']['text'][-600:])
  script=MAC_PREPARE_SCRIPT
  prepared=subprocess.run([sys.executable,'-B','-c',script,str(OLD),str(repo),str(admin)],env=base,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8,check=False)
  assert prepared.returncode==0,'MAC_PREPARE: exit='+repr(prepared.returncode)+' stderr='+ascii(prepared.stderr[-600:])
  snapshot_bytes=prepared.stdout;snapshot=json.loads(snapshot_bytes);state_input.write_bytes(snapshot_bytes);ascii_snapshot=copy.deepcopy(snapshot)
 else:
  assert pilot['exit']==0 and pilot['output'] is not None,pilot['stderr']['text']
  snapshot_bytes=bytes.fromhex(pilot['output']['bytes']['hex']);snapshot=json.loads(snapshot_bytes)
  assert snapshot_bytes==canonical(snapshot) and snapshot['schema']==7
  rows=snapshot['worktree_contents'][0]['files']
  assert next(r for r in rows if r['path']==file_name)['sha256']==hashlib.sha256((repo/file_name).read_bytes()).hexdigest()
  assert next(r for r in rows if r['path']==link_name)['target']==file_name
  secret=next(r for r in rows if r['path']=='private-owned.txt');assert secret['secret'] and 'sha256' not in secret
  assert snapshot['stop_control']=={'worktree':str(repo),'rel':stop_rel,'new_dirs':[]}
  assert b'OWN-PROBE-SECRET' not in snapshot_bytes
  state_input.write_bytes(snapshot_bytes)
  # Only filesystem-derived names and argv-derived rel change with old ASCII FS.
  ascii_snapshot=copy.deepcopy(snapshot)
  for row in ascii_snapshot['worktree_contents'][0]['files']:
   if row['path'] in (file_name,link_name):row['path']=raw(row['path']).decode('ascii','surrogateescape')
   if row.get('target')==file_name:row['target']=raw(file_name).decode('ascii','surrogateescape')
  ascii_snapshot['stop_control']['rel']=raw(stop_rel).decode('ascii','surrogateescape')
 expected={'snapshot':(0,b'',b'',snapshot_bytes),'stop-control':(0,b''.join(raw(v)+b'\0' for v in (str(repo),control['stop_control']['rel'],*control['stop_control']['new_dirs'])),b'',None),'preflight':(0,b'',b'',None),'derive-removed-worktree':(0,b'',b'',canonical(derived)),'state-digest':(0,hashlib.sha256(payload.read_bytes()).hexdigest().encode()+b'\n',b'',None),'state-digests':(0,b''.join(hashlib.sha256(p.read_bytes()).hexdigest().encode()+b'\n' for p in (payload,before)),b'',None),'compare':(1,'差分: stop_control が変わった\n'.encode(),b'',None)}
 if sys.platform=='darwin':
  for command in ('snapshot','preflight'):expected[command]=(20,b'','ERROR [loop-state] git の状態読取に失敗した\n'.encode(),None)
 settings=[('utf8',{}),('stdio-utf16',{b'PYTHONIOENCODING':b'utf-16'}),('filesystem-ascii',{b'PYTHONUTF8':b'0'}),('unknown-codec',{b'PYTHONIOENCODING':b's2l_no_such_codec'}),('invalid-utf8',{b'PYTHONUTF8':b'2'})]
 def check(result,expect):
  code,out,err,output=expect
  return result['exit']==code and result['stdout']['hex']==out.hex() and result['stderr']['hex']==err.hex() and ((result['output'] is None and output is None) or (output is not None and result['output'] is not None and result['output']['mode']==0o600 and result['output']['bytes']['hex']==output.hex()))
 def check_old(result,command,setting):
  exp=expected[command]
  if setting=='unknown-codec':
   e=bytes.fromhex(result['stderr']['hex'])
   pat=rb'Fatal Python error: init_stdio_encoding: failed to get the Python codec name of the stdio encoding\nPython runtime state: core initialized\nLookupError: unknown encoding: s2l_no_such_codec\n\nCurrent thread 0x[0-9a-f]+ \(most recent call first\):\n  <no Python frame>\n'
   return result['exit']==1 and result['stdout']['hex']=='' and result['output'] is None and not result['git_events'] and re.fullmatch(pat,e) is not None
  if setting=='invalid-utf8':
   return check(result,(1,b'',b'Fatal Python error: preconfig_init_utf8_mode: invalid PYTHONUTF8 environment variable value\nPython runtime state: preinitializing\n\n',None)) and not result['git_events']
  if setting=='stdio-utf16':
   code,out,err,output=exp
   if command in ('state-digest','state-digests','compare'):out=out.decode('utf-8').encode('utf-16-le')
   if err:err=err.decode('utf-8').encode('utf-16-le')
   return check(result,(code,out,err,output))
  if setting=='filesystem-ascii':
   if command=='snapshot' and sys.platform!='darwin':return check(result,(0,b'',b'',canonical(ascii_snapshot)))
   # Mac の CPython は引数とファイル系の文字化を常に UTF-8 にする(_Py_FORCE_UTF8_FS_ENCODING)。
   # PYTHONUTF8=0 と C の locale でも引数は JSON の文字列と一致し、通常の期待と同じになる。
   if command=='derive-removed-worktree' and sys.platform!='darwin':return check(result,(20,b'','ERROR [loop-state] 親が削除した worktree の記録が基準と一致しない\n'.encode(),None))
  return check(result,exp)
 
 # Include input inode/mtime/ctime/bytes/link target; atime is not compared because reads may update it.
 owned_inputs=[repo,home,xdg,global_config,system_config,*[p for p in repo.rglob('*')],*[p for p in inputs.iterdir()]]
 manifest_before={raw(p).hex():metadata(p) for p in owned_inputs}
 report={'note':'35 normal-path comparisons. No normalization of old/new result. Encoding differences have separate expectations.','time':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat(),'tmp':str(TMP),'python':sys.version,'binary':str(NEW),'binary_sha256':HASH,'legacy_sha256':hashlib.sha256(OLD.read_bytes()).hexdigest(),'pilot':pilot,'setup':setup,'baseline_env':{os.fsdecode(k):describe(v) for k,v in base.items() if k.startswith((b'PYTHON',b'GIT_',b'S2L_')) or k in (b'HOME',b'XDG_CONFIG_HOME',b'LC_ALL',b'LANG',b'COLUMNS')},'snapshot_semantics':{'raw_file_sha256':hashlib.sha256((repo/file_name).read_bytes()).hexdigest(),'raw_file_name_hex':raw(file_name).hex(),'link_name_hex':raw(link_name).hex(),'link_target_hex':raw(file_name).hex(),'secret_metadata_only':True},'expected':{k:{'exit':v[0],'stdout':describe(v[1]),'stderr':describe(v[2]),'output':None if v[3] is None else describe(v[3])} for k,v in expected.items()},'cases':[]}
 start=time.monotonic()
 for setting,delta in settings:
  for command in commands:
   old=run('legacy',command,delta);new=run('rust',command,delta)
   old_ok=check_old(old,command,setting);new_ok=check(new,expected[command])
   same=all(old[k]==new[k] for k in ('exit','stdout','stderr','output'))
   row={'setting':setting,'command':command,'argv_hex':[raw(x).hex() for x in commands[command]],'env_delta':{os.fsdecode(k):describe(v) for k,v in delta.items()},'legacy_expected':old_ok,'rust_expected':new_ok,'raw_same':same,'legacy':old,'rust':new}
   report['cases'].append(row)
   print(setting,command,'old_expected',old_ok,'rust_expected',new_ok,'same',same,'exits',old['exit'],new['exit'],'git',len(old['git_events']),len(new['git_events']),flush=True)
   # 期待と違えば、旧新の標準エラーを出す(Mac の値を推測で決めず、CI の出力で確かめるため)。
   if not (old_ok and new_ok):print('ENCODING_UNEXPECTED',setting,command,json.dumps({'legacy':old['stderr']['text'][-600:],'rust':new['stderr']['text'][-600:]},ensure_ascii=True),flush=True)
 manifest_after={raw(p).hex():metadata(p) for p in owned_inputs}
 report['input_manifest_before']=manifest_before;report['input_manifest_after']=manifest_after
 report['inputs_unchanged']=manifest_before==manifest_after
 report['binary_sha256_after']=hashlib.sha256(NEW.read_bytes()).hexdigest();assert report['binary_sha256_after']==HASH
 report['seconds']=round(time.monotonic()-start,6)
 report['summary']={'cases':len(report['cases']),'legacy_expected':sum(r['legacy_expected'] for r in report['cases']),'rust_expected':sum(r['rust_expected'] for r in report['cases']),'raw_same':sum(r['raw_same'] for r in report['cases']),'timeouts':sum(r[k]['timeout'] for r in report['cases'] for k in ('legacy','rust')),'all_groups_absent':all(r[k]['group_absent'] for r in report['cases'] for k in ('legacy','rust')),'inputs_unchanged':report['inputs_unchanged'],'git_raw_env_preserved':all(e['raw_env_preserved'] and e['global_expected'] for r in report['cases'] for k in ('legacy','rust') for e in r[k]['git_events']),'max_child_seconds':max(r[k]['seconds'] for r in report['cases'] for k in ('legacy','rust'))}
 (DIR/'utf8-normal-v3-probes.json').write_text(json.dumps(report,ensure_ascii=True,indent=2)+'\n')
 print(json.dumps(report['summary']),flush=True);print('seconds',report['seconds'],flush=True)
 
 assert len(report['cases'])==35
 assert report['summary']['legacy_expected']==35 and report['summary']['rust_expected']==35, 'ENCODING_INDIVIDUAL_EXPECTATIONS'
 assert report['summary']['timeouts']==0 and report['summary']['all_groups_absent'] and report['summary']['inputs_unchanged'] and report['summary']['git_raw_env_preserved'], 'ENCODING_STATE_OR_PROCESS'
 report['summary']['mac_snapshot_success_semantics_reached']=sys.platform!='darwin'
 report['summary']['commands']=sorted({row['command'] for row in report['cases']})
 report['summary']['skipped']=0
 report['summary']['os_boundaries']=boundaries
 return report['summary']
