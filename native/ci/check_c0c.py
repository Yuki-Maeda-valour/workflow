#!/usr/bin/env python3
"""C0cの固定期待、旧loop比較、実子の残存、入力無変更を確認する。"""
import argparse
import copy
import ctypes
import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import selectors
import subprocess
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from support_native import run_command, snapshot_tree
from support_proc import assert_absent_now, cleanup, probe, record as process_record


def encode(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n').encode()


def keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError('不正な観測キー')


def integer(value, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError('不正な整数')


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('重複キー')
        result[key] = value
    return result


def observation(result, name, input_length):
    if result.returncode != 0 or result.stderr or result.timed_out:
        raise ValueError(f'接続実行が失敗した: exit={result.returncode}, timeout={result.timed_out}, stderr={result.stderr[:2000]!r}')
    value = json.loads(result.stdout, object_pairs_hook=unique)
    keys(value, 'version case status cause cleanup output usage input deadline_unchanged direct_child_absent')
    integer(value['version'], 1, 1)
    if type(value['case']) is not str or value['case'] != name or value['deadline_unchanged'] is not True:
        raise ValueError('観測対象か期限が違う')
    status_value = value['status']
    if status_value is not None:
        keys(status_value, 'kind value')
        if status_value['kind'] not in ('exit', 'signal'):
            raise ValueError('不正な終了種別')
        integer(status_value['value'], 0 if status_value['kind'] == 'exit' else 1, 255 if status_value['kind'] == 'exit' else 128)
    cause = value['cause']
    if cause is not None:
        keys(cause, 'kind stream errno consumer')
        kinds = {'invalid-deadlines', 'cancelled', 'deadline', 'spawn', 'setup', 'poll', 'wait', 'input-closed', 'io', 'consumer'}
        if cause['kind'] not in kinds:
            raise ValueError('不正な原因')
        if cause['stream'] not in (None, 'stdin', 'stdout', 'stderr'):
            raise ValueError('不正なstream')
        if (cause['kind'] == 'io') != (cause['stream'] is not None):
            raise ValueError('原因とstreamが違う')
        if cause['errno'] is not None:
            integer(cause['errno'], 1)
        if (cause['kind'] in {'spawn', 'setup', 'poll', 'wait'}) and cause['errno'] is None:
            raise ValueError('OS errnoが無い')
        if cause['kind'] == 'poll' and cause['errno'] in (errno.EINTR, errno.EAGAIN):
            raise ValueError('再試行可能なpollを失敗にした')
        consumer = cause['consumer']
        if consumer not in (None, 'stdout-limit', 'stderr-limit', 'total-limit', 'refused', 'write-failed', 'flush-failed'):
            raise ValueError('不正なconsumer理由')
        if (cause['kind'] == 'consumer') != (consumer is not None):
            raise ValueError('consumer理由と原因が違う')
    rec = value['cleanup']
    keys(rec, 'kill_errno wait_errno expired reaped pending')
    for name_ in ('kill_errno', 'wait_errno'):
        if rec[name_] is not None:
            integer(rec[name_], 1)
    for name_ in ('expired', 'reaped', 'pending'):
        if type(rec[name_]) is not bool:
            raise ValueError('不正な回収状態')
    if rec['reaped'] != (status_value is not None) or (rec['pending'] and rec['reaped']):
        raise ValueError('回収状態と終了状態が違う')
    if value['direct_child_absent'] is not None and type(value['direct_child_absent']) is not bool:
        raise ValueError('不正な不在確認')
    if rec['reaped'] and value['direct_child_absent'] is not True:
        raise ValueError('回収済みの子が残っている')
    if cause is None and (not rec['reaped'] or rec['pending'] or rec['expired'] or rec['kill_errno'] is not None or rec['wait_errno'] is not None):
        raise ValueError('失敗した回収を成功にした')
    if cause is not None and cause['kind'] == 'wait':
        if rec['wait_errno'] != cause['errno'] or rec['reaped'] or not rec['pending']:
            raise ValueError('wait失敗の所有が失われた')
    keys(value['output'], 'stdout_hex stderr_hex')
    for raw in value['output'].values():
        if type(raw) is not str or len(raw) % 2 or any(c not in '0123456789abcdef' for c in raw):
            raise ValueError('生bytesが不正')
    keys(value['usage'], 'items bytes')
    for n in value['usage'].values():
        integer(n)
    keys(value['input'], 'written closed_early')
    integer(value['input']['written'], 0, input_length)
    if type(value['input']['closed_early']) is not bool:
        raise ValueError('入力閉鎖が不正')
    return value


def expected(name, stdout=b'', stderr=b'', code=0, *, signal_number=None, cause=None, consumer=None,
             os_errno=None, used=None, items=0, written=0, early=False, spawned=True):
    error = None if cause is None else dict(kind=cause, stream=None, errno=os_errno, consumer=consumer)
    state = None if not spawned else dict(kind='signal' if signal_number is not None else 'exit', value=signal_number if signal_number is not None else code)
    return dict(version=1, case=name, status=state, cause=error,
                cleanup=dict(kill_errno=None, wait_errno=None, expired=False, reaped=spawned, pending=False),
                output=dict(stdout_hex=stdout.hex(), stderr_hex=stderr.hex()),
                usage=dict(items=items, bytes=len(stdout)+len(stderr) if used is None else used),
                input=dict(written=written, closed_early=early), deadline_unchanged=True,
                direct_child_absent=True if spawned else None)


def verify_run(result, name, wanted, input_length=0, *, partial_input=False):
    value = observation(result, name, input_length)
    comparison = copy.deepcopy(value)
    if partial_input:
        if not 0 <= value['input']['written'] < input_length:
            raise ValueError('未送信入力の範囲が違う')
        # 実pipeの容量はOSが決める。厳密な短書込み長は内部故障検査で別に確認する。
        comparison['input']['written'] = wanted['input']['written']
    if comparison != wanted:
        raise ValueError(f'{name}: 固定期待と違う\n期待={wanted}\n実際={value}')
    return value


def verify_pair(old, new, name, wanted):
    left = verify_run(old, name, wanted)
    right = verify_run(new, name, wanted)
    if left != right:
        raise ValueError('旧版との観測が違う')


def tree(path):
    result = snapshot_tree(path)
    for name, value in result.items():
        info = (Path(path) / name).lstat()
        value['identity'] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    return result


def child(control, config):
    target = Path(control)
    exec(process_record(target), {'Path': Path, 'json': json, 'os': os})
    mode = config.get('mode', 'output')
    if mode in ('fork-hold', 'fork-close'):
        rd, wr = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(rd)
            exec(process_record(target.with_suffix('.grand')), {'Path': Path, 'json': json, 'os': os})
            if mode == 'fork-close':
                for fd in (0, 1, 2):
                    os.close(fd)
            os.write(wr, b'x'); os.close(wr)
            time.sleep(30)
            os._exit(0)
        os.close(wr); assert os.read(rd, 1) == b'x'; os.close(rd)
        os._exit(0)
    if mode == 'handshake':
        os.write(1,b'ready'); size=0
        while size<1_000_000:
            data=os.read(0,65536)
            if not data:raise ValueError('handshake stdin ended')
            assert data==b'x'*len(data);size+=len(data)
        os.write(1,b'\0\xffdone');os.write(2,b'\xfe');os._exit(237)
    if mode == 'signal':
        os.kill(os.getpid(), config['signal'])
        raise AssertionError('signal did not terminate')
    if mode == 'close-input':
        os.close(0)
        time.sleep(.1)
    elif mode == 'echo':
        while True:
            data = os.read(0, 32768)
            if not data:
                break
            os.write(1, data); os.write(2, data)
    elif mode == 'environment':
        os.write(1, os.environ.get('C0C_VALUE', '<absent>').encode())
    elif mode == 'fds':
        found=[]
        for fd in range(3,256):
            try:os.fstat(fd)
            except OSError as error:
                if error.errno!=errno.EBADF:raise
            else:found.append(fd)
        if found:os.write(1,json.dumps(found).encode())
    elif mode == 'cwd':
        os.write(1, os.getcwdb())
    elif mode == 'close-outputs':
        os.close(1); os.close(2)
    else:
        for fd, key in [(1, 'stdout_hex'), (2, 'stderr_hex')]:
            data = bytes.fromhex(config.get(key, '')) * config.get('repeat_bytes', 1)
            while data:
                n = os.write(fd, data); data = data[n:]
    if config.get('linger'):
        time.sleep(30)
    os._exit(config.get('code', 0))


def plan_for(cwd, control, config):
    argv = [sys.executable, '-B', str(Path(__file__).resolve()), '--child', str(control), json.dumps(config)]
    return dict(argv_hex=[os.fsencode(arg).hex() for arg in argv], cwd_hex=os.fsencode(cwd).hex(),
                input_hex='', total=20_000_000, prior_bytes=0, prior_items=0, stdout_limit=20_000_000,
                stderr_limit=20_000_000, budget_ms=5000, execution_ms=5000, cleanup_ms=7000,
                cancel='none', consumer='capture', count='pre', repeat=1, broken_pipe='reject', clear_env=False, env={})


def legacy_record(name, plan, control):
    source = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/loop-state.py'
    spec = importlib.util.spec_from_file_location('c0c_legacy', source)
    old = importlib.util.module_from_spec(spec); sys.modules[spec.name] = old; spec.loader.exec_module(old)
    argv = [os.fsdecode(bytes.fromhex(arg)) for arg in plan['argv_hex']]
    b = old.Budget(100, plan['total'], 1, 5)
    b.bytes = plan['prior_bytes']; b.count = plan['prior_items']; deadline = b.deadline
    seen = []
    popen = old.subprocess.Popen
    def capture(*args, **kwargs):
        proc = popen(*args, **kwargs); seen.append(proc); return proc
    try:
        with mock.patch.object(old.subprocess, 'Popen', side_effect=capture):
            rc, output = old.run_git(os.fsdecode(bytes.fromhex(plan['cwd_hex'])), *argv[1:], budget=b,
                                     return_status=True, allow_failure=True, env_extra=plan['env'])
        result = expected(name, output, code=rc if rc >= 0 else 0, signal_number=-rc if rc < 0 else None,
                          used=b.bytes, items=b.count)
    except old.Stop as error:
        if str(error) == '状態の観察の総読取量が上限を超えた':
            assert seen and seen[-1].returncode == -signal.SIGKILL
            result = expected(name, cause='consumer', consumer='total-limit', signal_number=signal.SIGKILL, used=b.bytes, items=b.count)
        elif str(error) == 'git の状態読取に失敗した' and isinstance(error.__cause__, OSError):
            result = expected(name, cause='spawn', os_errno=error.__cause__.errno, used=b.bytes, items=b.count, spawned=False)
        else:
            raise RuntimeError('想定外の旧版失敗') from error
    result['deadline_unchanged'] = b.deadline == deadline
    if seen:
        assert_absent_now(seen[-1].pid)
    return result


class PipeReader:
    def __init__(self, stream, deadline):
        self.stream = stream
        self.deadline = deadline
        self.buffer = b''

    def fill(self):
        left = self.deadline - time.monotonic()
        with selectors.DefaultSelector() as selector:
            selector.register(self.stream, selectors.EVENT_READ)
            if left <= 0 or not selector.select(left):
                raise ValueError('接続の返却通知が期限内に届かない')
            data = os.read(self.stream.fileno(), 65536)
        if not data:
            raise ValueError('接続が返却確認より前に終了した')
        self.buffer += data
        if len(self.buffer) > 16_000_000:
            raise ValueError('接続の観測が長すぎる')

    def exact(self, count):
        while len(self.buffer) < count:
            self.fill()
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result

    def line(self):
        while b'\n' not in self.buffer:
            self.fill()
        end = self.buffer.index(b'\n') + 1
        return self.exact(end)


def observe_return(process, notice, iteration, control, spawned):
    keys(notice, 'returned')
    integer(notice['returned'])
    if notice['returned'] != iteration or process.poll() is not None:
        raise ValueError('返却の順序か接続の生存が違う')
    if spawned:
        pid = json.loads(control.read_text(), object_pairs_hook=unique)['pid']
        integer(pid, 1)
        # aliveを見たらその場で失敗する。接続終了や子の自然終了を待たない。
        assert_absent_now(pid)
        absent = True
    else:
        if control.exists():
            raise ValueError('起動前拒否なのに子の記録がある')
        absent = None
    if process.poll() is not None:
        raise ValueError('不在確認の応答より先に接続が終了した')
    return absent


def acknowledge(process, reader, iteration, control, spawned):
    notice = json.loads(reader.line(), object_pairs_hook=unique)
    absent = observe_return(process, notice, iteration, control, spawned)
    send(process, encode(dict(direct_child_absent=absent)))


def send(process, data):
    while data:
        count = process.stdin.write(data)
        if not count:
            raise ValueError('検査の応答を書けない')
        data = data[count:]
    process.stdin.flush()


def fixture_driver(fixture, name, plan, control, spawned):
    process = subprocess.Popen([fixture, '--fixture', name], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        send(process, encode(plan))
        reader = PipeReader(process.stdout, time.monotonic() + 10)
        for iteration in range(plan['repeat']):
            acknowledge(process, reader, iteration, control, spawned)
        final = reader.line()
        stdout, stderr = process.communicate(timeout=max(.01, reader.deadline-time.monotonic()))
        if process.returncode or stderr or stdout or reader.buffer:
            raise ValueError('接続の終了値・stderr・最終観測の数が違う')
        sys.stdout.buffer.write(final)
    finally:
        if cleanup(control.parent, children=[process]):
            raise ValueError('fixture cleanup')
    return 0


def run_case(fixture, name, plan, wanted, initial, control, *, partial_input=False, legacy=False):
    before = tree(initial)
    env = {**os.environ, 'C0C_VALUE': 'inherited'}
    try:
        result = run_command([sys.executable, '-B', str(Path(__file__).resolve()), '--fixture-driver',
                              str(fixture), name, str(control), '1' if wanted['status'] is not None else '0'],
                             cwd=initial, env=env, input=encode(plan), timeout=15)
        verify_run(result, name, wanted, len(bytes.fromhex(plan['input_hex'])), partial_input=partial_input)
        if control.exists():
            pid = json.loads(control.read_text())['pid']
            assert_absent_now(pid)
        elif wanted['status'] is not None:
            raise ValueError('子の記録が無い')
        grand = control.with_suffix('.grand')
        if grand.exists() and probe(os.kill, json.loads(grand.read_text())['pid']) != 'alive':
            raise ValueError('子孫の残存境界を観測できない')
        if tree(initial) != before:
            raise ValueError('入力が変更された')
        if legacy:
            old_value = legacy_record(name, plan, control)
            from support_native import RunResult
            verify_pair(RunResult(0, encode(old_value), b'', False), result, name, wanted)
            if tree(initial) != before:
                raise ValueError('旧版が入力を変更した')
    finally:
        leftovers = cleanup(control.parent, records=[control, control.with_suffix('.grand')])
        if leftovers:
            raise RuntimeError(f'検査の子が残った: {leftovers}')


def cases():
    for code in range(256):
        yield f'exit-{code}', {'code': code}, {}, dict(code=code)
    yield 'raw', {'stdout_hex':'00fffe','stderr_hex':'fe00ff'}, {}, dict(stdout=b'\0\xff\xfe',stderr=b'\xfe\0\xff')
    yield 'large-both', {'stdout_hex':'00ff','stderr_hex':'fe00','repeat_bytes':131073}, {}, dict(stdout=b'\0\xff'*131073,stderr=b'\xfe\0'*131073)
    for number in [signal.SIGTERM,signal.SIGKILL]:
        yield f'signal-{number}', {'mode':'signal','signal':number}, {}, dict(signal_number=number)
    data=b'\0\xffa'*100000
    yield 'echo', {'mode':'echo'}, dict(input_hex=data.hex()), dict(stdout=data,stderr=data,written=len(data))
    for stream in ['stdout','stderr']:
        for size in [3,4,5]:
            config={stream+'_hex':(b'x'*size).hex()}
            changes={stream+'_limit':4}
            wanted={stream:b'x'*size}
            if size>4:
                config['linger']=True
                wanted=dict(cause='consumer',consumer=stream+'-limit',signal_number=signal.SIGKILL,used=0)
            yield f'{stream}-limit-{size}',config,changes,wanted
    for size in [3,4,5]:
        config={'stdout_hex':(b'x'*size).hex()}
        wanted=dict(stdout=b'x'*size,used=7+size,items=2)
        if size>4:
            config['linger']=True
            wanted=dict(cause='consumer',consumer='total-limit',signal_number=signal.SIGKILL,used=7,items=2)
        yield f'total-{size}',config,dict(total=11,prior_bytes=7,prior_items=2),wanted
    yield 'post-charge-limit',{'stdout_hex':'7878787878','linger':True},dict(total=4,count='post'),dict(cause='consumer',consumer='total-limit',signal_number=signal.SIGKILL,used=5)
    yield 'borrow-twice',{'stdout_hex':'78'},dict(repeat=2,prior_bytes=7,prior_items=3),dict(stdout=b'xx',used=9,items=3)
    yield 'fd-inheritance',{'mode':'fds'},{},{}
    yield 'cancel-before',{},dict(cancel='before'),dict(cause='cancelled',spawned=False)
    yield 'cancel-expired',{},dict(cancel='before',budget_ms=0),dict(cause='cancelled',spawned=False)
    yield 'execution-expired',{},dict(execution_ms=0),dict(cause='deadline',spawned=False)
    yield 'budget-expired',{},dict(budget_ms=0),dict(cause='deadline',spawned=False)
    yield 'invalid-deadlines',{},dict(cleanup_ms=0,cancel='before'),dict(cause='invalid-deadlines',spawned=False)
    yield 'cancel-output',{'stdout_hex':'78','linger':True},dict(cancel='output'),dict(stdout=b'x',cause='cancelled',signal_number=signal.SIGKILL)
    for mode in ['close-outputs','output']:
        yield 'deadline-'+mode,{'mode':mode,'linger':True},dict(execution_ms=500),dict(cause='deadline',signal_number=signal.SIGKILL)
    yield 'stdin-stall',{'stdout_hex':'78','stderr_hex':'79','linger':True},dict(input_hex=data.hex(),execution_ms=500),dict(stdout=b'x',stderr=b'y',cause='deadline',signal_number=signal.SIGKILL)
    for policy in ['reject','close']:
        wanted=dict(early=True)
        if policy=='reject':
            wanted.update(cause='input-closed',signal_number=signal.SIGKILL)
        yield 'epipe-'+policy,{'mode':'close-input','linger':policy=='reject'},dict(input_hex=data.hex(),broken_pipe=policy),wanted
    for consumer in ['refused','write-failed','flush-failed']:
        yield consumer,{'stdout_hex':'78','linger':True},dict(consumer=consumer),dict(cause='consumer',consumer=consumer,signal_number=signal.SIGKILL,used=1)
    yield 'grandchild-holds-pipe',{'mode':'fork-hold'},dict(execution_ms=500),dict(cause='deadline')
    yield 'grandchild-closes-pipe',{'mode':'fork-close'}, {}, {}
    for clear, value, expected_value in [(False,None,b'inherited'),(True,None,b'<absent>'),(True,'replaced',b'replaced')]:
        yield f'env-{clear}-{value}',{'mode':'environment'},dict(clear_env=clear,env={} if value is None else {'C0C_VALUE':value}),dict(stdout=expected_value)


def stream_driver(fixture, plan, control):
    process=subprocess.Popen([fixture,'--stream'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
    try:
        send(process, encode(plan))
        reader=PipeReader(process.stdout,time.monotonic()+10)
        if reader.exact(5)!=b'ready':raise ValueError('途中bytesが違う')
        if process.poll() is not None:raise ValueError('入力確認前に接続が終了')
        pid=json.loads(Path(control).read_text())['pid']
        if probe(os.kill,pid)!='alive':raise ValueError('入力確認前に子が終了')
        send(process,b'go\n')
        if reader.exact(6)!=b'\0\xffdone':raise ValueError('途中raw bytesが違う')
        acknowledge(process,reader,0,Path(control),True)
        stdout,stderr=process.communicate(timeout=5)
        if (process.returncode,reader.buffer+stdout,stderr)!=(237,b'finished',b'\xfe'):
            raise ValueError('C0a consumerの最終bytesか終了値が違う')
        assert_absent_now(pid)
    finally:
        if cleanup(Path(control).parent,children=[process],records=[Path(control)]):raise ValueError('stream cleanup')
    return 0


def write_launcher(path):
    path.write_text('#!'+sys.executable+' -B\nimport runpy\nrunpy.run_path('+repr(str(Path(__file__).resolve()))+',run_name="__main__")\n')
    path.chmod(0o755)


def git_plan(cwd, control, config, executable, search):
    plan = plan_for(cwd, control, config)
    plan['argv_hex'] = [os.fsencode(arg).hex() for arg in
                        [str(executable), '--c0c-child', str(control), json.dumps(config)]]
    plan['env'] = {'PATH': str(search)+os.pathsep+os.environ.get('PATH', '')}
    return plan


def run_all(fixture):
    count=0
    with tempfile.TemporaryDirectory(prefix='c0c-compare-') as directory:
        root=Path(directory).resolve()
        initial=root/'input';initial.mkdir();(initial/'unchanged').write_bytes(b'\0\xffinput')
        control=root/'control';control.mkdir()
        fake=root/'fake';fake.mkdir();git=fake/'git';write_launcher(git)
        for name,config,changes,wanted in cases():
            path=control/(name+'.json');plan=plan_for(initial,path,config);plan.update(changes)
            want=expected(name,**wanted)
            partial=name.startswith('epipe-') or name=='stdin-stall'
            run_case(fixture,name,plan,want,initial,path,partial_input=partial)
            count+=1
        stream_control=control/'stream.json'
        stream_plan=plan_for(initial,stream_control,{'mode':'handshake'})
        before=tree(initial)
        streamed=run_command([sys.executable,'-B',str(Path(__file__).resolve()),'--stream-driver',str(fixture),json.dumps(stream_plan),str(stream_control)],cwd=initial,timeout=12)
        if streamed.returncode or streamed.stdout or streamed.stderr or streamed.timed_out:raise ValueError(f'C0a stream失敗: {streamed}')
        if tree(initial)!=before:raise ValueError('streamが入力を変更した')
        count+=1
        # 長い物理cwdを通常入力として使い、argvのshebangには埋め込まない。
        long=initial/('a'*80)/('b'*80)/('日本語 空白'+ 'c'*40);long.mkdir(parents=True)
        for name,cwd in [('long-cwd',long)]:
            path=control/(name+'.json');plan=plan_for(cwd,path,{'mode':'cwd'})
            run_case(fixture,name,plan,expected(name,stdout=os.fsencode(cwd)),initial,path);count+=1
        try:
            raw=initial/os.fsdecode(b'raw-\xff');raw.mkdir()
        except OSError as error:
            if sys.platform!='darwin' or error.errno!=errno.EILSEQ:
                raise
            print('P2: MacのEILSEQにより実在名の事前作成だけ未実施')
        else:
            path=control/'raw-name.json';plan=git_plan(raw,path,{'mode':'cwd'},git,fake)
            run_case(fixture,'raw-name',plan,expected('raw-name',stdout=os.fsencode(raw)),initial,path,legacy=True);count+=1
            binary=raw/os.fsdecode(b'exec-\xff')
            write_launcher(binary)
            aliases=initial/'raw-alias';aliases.mkdir()
            alias=aliases/'git';alias.symlink_to(os.path.relpath(binary,aliases))
            if not alias.samefile(binary):raise ValueError('旧版とRustの実行物が別の実体')
            path=control/'raw-executable.json';plan=git_plan(raw,path,{'stdout_hex':'00ff'},binary,aliases)
            run_case(fixture,'raw-executable',plan,expected('raw-executable',stdout=b'\0\xff'),initial,path,legacy=True);count+=1
        for name,kind,code in [('missing','missing',errno.ENOENT),('denied','denied',errno.EACCES),('missing-cwd','cwd',errno.ENOENT)]:
            path=control/(name+'.json');plan=plan_for(initial,path,{})
            if kind=='cwd':plan['cwd_hex']=os.fsencode(initial/'missing').hex()
            else:
                binary=initial/kind
                if kind=='denied':binary.write_bytes(b'not executable');binary.chmod(0o600)
                plan['argv_hex']=[os.fsencode(binary).hex()]
            run_case(fixture,name,plan,expected(name,cause='spawn',os_errno=code,spawned=False),initial,path);count+=1
        # 旧run_gitはPATHでgitを選ぶ。短い固定interpreterパスだけをshebangへ置く。
        for name,body,code,total,prior in [('legacy-raw',b'\0\xff',237,20,0),('legacy-empty',b'',0,20,0),('legacy-below',b'xxx',0,11,7),('legacy-equal',b'xxxx',0,11,7),('legacy-over',b'xxxxx',0,11,7)]:
            path=control/(name+'.json');config={'stdout_hex':body.hex(),'code':code,'linger':name=='legacy-over'}
            plan=plan_for(initial,path,config);plan.update(total=total,prior_bytes=prior,env={'PATH':str(fake)+os.pathsep+os.environ.get('PATH','')})
            args=[str(git),'--c0c-child',str(path),json.dumps(config)]
            plan['argv_hex']=[os.fsencode(x).hex() for x in args]
            want=expected(name,stdout=body,code=code,used=prior+len(body))
            if name=='legacy-over':want=expected(name,cause='consumer',consumer='total-limit',signal_number=signal.SIGKILL,used=prior)
            run_case(fixture,name,plan,want,initial,path,legacy=True);count+=1
        name='legacy-missing';path=control/(name+'.json');plan=plan_for(initial,path,{})
        missing=root/'no-git';missing.mkdir();plan['env']={'PATH':str(missing)};plan['argv_hex']=[os.fsencode(missing/'git').hex()]
        run_case(fixture,name,plan,expected(name,cause='spawn',os_errno=errno.ENOENT,spawned=False),initial,path,legacy=True);count+=1
    return count


def main():
    args=sys.argv[1:]
    if args and args[0]=='--fixture-driver':
        return fixture_driver(args[1],args[2],json.load(sys.stdin),Path(args[3]),args[4]=='1')
    if args and args[0]=='--stream-driver':return stream_driver(args[1],json.loads(args[2]),args[3])
    for flag in ['--child','--c0c-child']:
        if flag in args:
            at=args.index(flag)
            if flag=='--c0c-child' and '-C' in args[:at]:
                os.chdir(args[args.index('-C')+1])
            child(args[at+1],json.loads(args[at+2]));return 0
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--fixture',type=Path,required=True);options=parser.parse_args()
    if sys.version_info[:2]!=(3,10) or os.geteuid()==0:
        raise RuntimeError('Python3.10系と通常利用者が必要です')
    fixture=options.fixture.resolve(strict=True)
    if not fixture.is_file() or not os.access(fixture,os.X_OK):raise ValueError('今回作った実行物が必要です')
    spec=importlib.util.spec_from_file_location('c0c_selftests',Path(__file__).with_name('test_c0c.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    suite=unittest.defaultTestLoader.loadTestsFromModule(module)
    if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():raise RuntimeError('比較器の自己検査が失敗')
    old=ctypes.c_int()
    libc=None
    if sys.platform.startswith('linux'):
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.prctl(37,ctypes.byref(old),0,0,0) or libc.prctl(36,1,0,0,0):raise OSError(ctypes.get_errno(),'test subreaper')
    try: count=run_all(fixture)
    finally:
        if libc is not None and libc.prctl(36,old.value,0,0,0):raise OSError(ctypes.get_errno(),'restore test subreaper')
    print(f'C0c: {count} ケースと比較器自己検査{suite.countTestCases()}件が成功')
    return 0

if __name__=='__main__':
    sys.exit(main())
