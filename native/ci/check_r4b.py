#!/usr/bin/env python3
"""R4B専用比較。旧版を変えず、同じ物理入力・固定期待・無変更を確認する。"""
import argparse
import ast
import codecs
import contextlib
import errno
import fcntl
import pty
import struct
import termios
import ctypes
import inspect
import textwrap
import traceback
import dataclasses
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from support_native import RunResult, run_command, snapshot_tree
from support_proc import assert_absent_now, wait_absent, command_line

SCRIPTS = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts'
COMMANDS = ('origin-repo', 'git-config-digest')
FIXTURE = None
SECRET = b'R4B-SECRET-CREDENTIAL'
FORBIDDEN = (SECRET, b'https://', b'git@', b'x-access-token', b'R4B-CONFIG-SECRET')


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fixture_path(value):
    if not value or not Path(value).is_absolute():
        raise ValueError('検査入口は絶対パスで指定してください')
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('検査入口は実行可能な通常ファイルである必要があります')
    return path


def same_result(old, new):
    if old.timed_out or new.timed_out:
        raise AssertionError('比較対象が時間切れになった')
    if (old.returncode, old.stdout, old.stderr) != (new.returncode, new.stdout, new.stderr):
        # Gitの値やURLを診断へ転記しない。
        raise AssertionError('終了値または出力が一致しない')


def fixed(result, code, stdout=b'', stderr=b''):
    if result.timed_out or (result.returncode, result.stdout, result.stderr) != (code, stdout, stderr):
        raise AssertionError('固定期待と一致しない')


def approved_decode(old, new, *, stream='stdout'):
    try:b'\xff'.decode('utf-8')
    except UnicodeDecodeError as error:
        expected=origin_traceback(error,decode_stream=stream).encode()
    fixed(old,1,stderr=expected)
    fixed(new,1,stderr='git の出力を読めない\n'.encode())


def approved_output(old, new, *, old_code, exception, prefix=b'', broken_diagnostic=False, diagnostic=None):
    # prefixは各ケースが固定した、実際に捕捉可能な途中出力そのもの。
    # 空prefixから任意の出力を許すstartswith比較にはしない。
    if old.timed_out or old.returncode != old_code or old.stdout != prefix:
        raise AssertionError('旧版の出力故障の終了値または途中出力が違う')
    if broken_diagnostic:
        expected_old=b''
    else:
        errors={b'OSError: [Errno 28]':b'OSError: [Errno 28] No space left on device\n',
                b'BrokenPipeError: [Errno 32]':b'BrokenPipeError: [Errno 32] Broken pipe\n'}
        if exception not in errors:
            raise AssertionError('未承認の旧版例外')
        expected_old=b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n"+errors[exception]
    if old.stderr != expected_old:
        raise AssertionError('旧版の出力故障の例外が違う')
    if new.timed_out or new.returncode != 1 or new.stdout != prefix:
        raise AssertionError('Rustの出力故障の終了値または途中出力が違う')
    if any(secret in old.stdout + old.stderr + new.stdout + new.stderr for secret in FORBIDDEN):
        raise AssertionError('出力故障に機密が混入した')
    expected = b'' if broken_diagnostic else diagnostic
    if expected is None:
        expected='dev-workflow: 標準出力に書けません: No space left on device (os error 28)\n'.encode()
    if new.stderr != expected:
        raise AssertionError('Rustの出力故障の診断が違う')


def fixed_pair(old, new, old_expected, new_expected, old_calls, new_calls, expected_old_calls, expected_new_calls):
    fixed(old, *old_expected)
    fixed(new, *new_expected)
    if old_calls != expected_old_calls or new_calls != expected_new_calls:
        raise AssertionError('個別固定の呼出列と一致しない')


# 説明幅30/60/80の固定バイト列。旧版実行の結果やRustの出力からは生成しない。
ARGUMENT_USAGE = {
 ('origin-repo',80): 'usage: origin-repo.py [-h] --dir DIR\n',
 ('git-config-digest',80): 'usage: git-config-digest.py [-h] --dir DIR [--expect EXPECT]\n',
 ('origin-repo',30): 'usage: origin-repo.py [-h]\n                      --dir\n                      DIR\n',
 ('git-config-digest',30): 'usage: git-config-digest.py\n       [-h] --dir DIR\n       [--expect EXPECT]\n',
}
ARGUMENT_USAGE['origin-repo',60]=ARGUMENT_USAGE['origin-repo',80]
ARGUMENT_USAGE['git-config-digest',60]='usage: git-config-digest.py [-h] --dir DIR\n                            [--expect EXPECT]\n'
ARGUMENT_HELP = {
 ('origin-repo',80): ARGUMENT_USAGE['origin-repo',80]+'\noptions:\n  -h, --help  show this help message and exit\n  --dir DIR\n',
 ('git-config-digest',80): ARGUMENT_USAGE['git-config-digest',80]+'\noptions:\n  -h, --help       show this help message and exit\n  --dir DIR\n  --expect EXPECT\n',
 ('origin-repo',30): ARGUMENT_USAGE['origin-repo',30]+'\noptions:\n  -h, --help\n        show this help\n        message and exit\n  --dir DIR\n',
 ('git-config-digest',30): ARGUMENT_USAGE['git-config-digest',30]+'\noptions:\n  -h, --help\n        show this help\n        message and exit\n  --dir DIR\n  --expect EXPECT\n',
}

ARGUMENT_HELP['origin-repo',60]=ARGUMENT_HELP['origin-repo',80]
ARGUMENT_HELP['git-config-digest',60]=ARGUMENT_USAGE['git-config-digest',60]+'\noptions:\n  -h, --help       show this help message and exit\n  --dir DIR\n  --expect EXPECT\n'


def argument_error(command, message, *, width=80, codec='utf-8'):
    return (2,b'',(ARGUMENT_USAGE[command,width]+command+'.py: error: '+message+'\n').encode(codec,'backslashreplace'))


def argument_contract(old, new, old_expected, new_expected, old_calls, new_calls,
                      expected_old_calls, expected_new_calls, raw_before, raw_after, input_before, input_after):
    if type(raw_before) is not tuple or any(type(value) is not bytes for value in raw_before):
        raise AssertionError('引数の生バイトの記録が不正')
    if raw_before!=raw_after:
        raise AssertionError('引数または環境の生バイトが変わった')
    unchanged(input_before,input_after)
    fixed_pair(old,new,old_expected,new_expected,old_calls,new_calls,expected_old_calls,expected_new_calls)


def run_pty(argv, *, columns, env, cwd=ROOT):
    # stderrは通常のpipe、stdoutだけが実端末。改行のCRLF変換も生バイトで比較する。
    master,slave=pty.openpty()
    try:
        # OSごとの初期値に依存せず、比較する端末の改行規則を起動前に固定する。
        attributes=termios.tcgetattr(slave)
        attributes[1] |= termios.OPOST | termios.ONLCR
        for name in ('OCRNL','ONOCR','ONLRET'):
            attributes[1] &= ~getattr(termios,name,0)
        termios.tcsetattr(slave,termios.TCSANOW,attributes)
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,columns,0,0))
        process=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=slave,stderr=subprocess.PIPE,start_new_session=True)
        # 最後のslave closeで未読出力を捨てるOSでも、排出前に閉じない。
        timed_out=False
        try:_,stderr=process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            timed_out=True
            os.killpg(process.pid,signal.SIGKILL)
            _,stderr=process.communicate(timeout=2)
        stdout=bytearray()
        os.set_blocking(master,False)
        while True:
            try:part=os.read(master,65536)
            except OSError as error:
                if error.errno==errno.EIO:break
                if error.errno not in (errno.EAGAIN,errno.EWOULDBLOCK):raise
                # 直接子はcommunicateで回収済み。slave切断後の通知が
                # EAGAINでも、EOFの形ではなく排出完了として扱う。
                break
            if not part:break
            stdout.extend(part)
        return RunResult(process.returncode,bytes(stdout),stderr,timed_out)
    finally:
        if slave is not None:os.close(slave)
        os.close(master)


def terminal_contract(old,new,*,command,columns,help_mode):
    # 固定表のバイト列を端末の改行へ写す。幅60のusageも独立して保持する。
    width=columns
    if help_mode:
        expected=(0,ARGUMENT_HELP[command,width].replace('\n','\r\n').encode(),b'')
    else:expected=argument_error(command,'the following arguments are required: --dir',width=width)
    fixed(old,*expected);fixed(new,*expected);same_result(old,new)


def code_line(source, function, called, *, attribute=False):
    tree=ast.parse(source)
    scope=next(node for node in ast.walk(tree) if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name==function) if function else tree
    candidates=[node.lineno for node in ast.walk(scope) if isinstance(node,ast.Call) and
                ((isinstance(node.func,ast.Attribute) and node.func.attr==called) if attribute else (isinstance(node.func,ast.Name) and node.func.id==called))]
    if not candidates:raise AssertionError('固定tracebackの参照行が見つからない')
    return min(candidates)


def origin_traceback(exception, *, decode_stream=None):
    # 実際の失敗出力から期待値を作らない。固定した経路と正本ソースの行を使う。
    script=SCRIPTS/'origin-repo.py';source=script.read_text()
    frames=[(str(script),code_line(source,None,'main'),'<module>')]
    if decode_stream is None:
        frames.append((str(script),code_line(source,'main','print'),'main'))
        # mainの最初のprintは--dir拒否。成功JSONのprint行を明示して選ぶ。
        tree=ast.parse(source)
        main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        prints=[n for n in ast.walk(main) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='print']
        frames[-1]=(str(script),max(n.lineno for n in prints),'main')
    else:
        for function,called in [('main','judge'),('judge','lines'),('lines','git')]:
            frames.append((str(script),code_line(source,function,called),function))
        frames.append((str(script),code_line(source,'git','run',attribute=True),'git'))
        for function,called in [(subprocess.run,'communicate'),(subprocess.Popen.communicate,'_communicate'),(subprocess.Popen._communicate,'_translate_newlines'),(subprocess.Popen._translate_newlines,'decode')]:
            lines,first=inspect.getsourcelines(function)
            body=textwrap.dedent(''.join(lines))
            if function is subprocess.Popen._communicate:
                tree=ast.parse(body)
                calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr==called and n.args and isinstance(n.args[0],ast.Name) and n.args[0].id==decode_stream]
                if len(calls)!=1:raise AssertionError('復号streamの参照行が見つからない')
                line=calls[0].lineno
            else:line=code_line(body,function.__name__,called,attribute=True)
            frames.append((inspect.getsourcefile(function),first+line-1,function.__name__))
    summaries=[traceback.FrameSummary(path,line,function) for path,line,function in frames]
    return ('Traceback (most recent call last):\n'+''.join(traceback.format_list(summaries))+''.join(traceback.format_exception_only(type(exception),exception)))


# LC_ALL=CはLinux/Macで必須。Macのfilesystem encodingはlocaleから独立したUTF-8。
# io: codec名 / 出力のerrors / 子出力のcodec / Linux filesystem codec / UTF8 mode。
ENCODINGS = (
    ('posix', {'LC_ALL':'POSIX','PYTHONUTF8':'0','PYTHONCOERCECLOCALE':'0'}, 'ascii','surrogateescape','ascii','ascii',0),
    ('invalid-locale', {'LC_ALL':'r4b_INVALID_LOCALE','PYTHONUTF8':'0','PYTHONCOERCECLOCALE':'0'}, 'ascii','surrogateescape','ascii','ascii',0),
    ('locale-utf8', {'LC_ALL':'en_US.UTF-8' if sys.platform=='darwin' else 'C.UTF-8','PYTHONUTF8':'0','PYTHONCOERCECLOCALE':'0'}, 'utf-8','surrogateescape','utf-8','utf-8',0),
    ('default-utf8', {'PYTHONUTF8':'1','PYTHONCOERCECLOCALE':'0'}, 'utf-8','surrogateescape','utf-8','utf-8',1),
    ('auto-utf8', {'PYTHONCOERCECLOCALE':'0'}, 'utf-8','surrogateescape','utf-8','utf-8',1),
    ('ascii-locale', {'PYTHONUTF8':'0','PYTHONCOERCECLOCALE':'0'}, 'ascii','surrogateescape','ascii','ascii',0),
    ('ascii-all', {'PYTHONUTF8':'0','PYTHONCOERCECLOCALE':'1'}, 'ascii','surrogateescape','ascii','ascii',0),
    ('io-utf8', {'PYTHONUTF8':'0','PYTHONCOERCECLOCALE':'0','PYTHONIOENCODING':'utf-8'}, 'utf-8','strict','ascii','ascii',0),
    ('io-ascii', {'PYTHONUTF8':'1','PYTHONIOENCODING':'ascii'}, 'ascii','strict','utf-8','utf-8',1),
    ('io-ascii-escape', {'PYTHONUTF8':'1','PYTHONIOENCODING':'ascii:backslashreplace'}, 'ascii','backslashreplace','utf-8','utf-8',1),
    ('io-ascii-replace', {'PYTHONUTF8':'1','PYTHONIOENCODING':'ascii:replace'}, 'ascii','replace','utf-8','utf-8',1),
    ('io-latin1', {'PYTHONUTF8':'1','PYTHONIOENCODING':'latin-1'}, 'iso8859-1','strict','utf-8','utf-8',1),
    ('io-latin1-escape', {'PYTHONUTF8':'1','PYTHONIOENCODING':'latin-1:backslashreplace'}, 'iso8859-1','backslashreplace','utf-8','utf-8',1),
    ('io-latin1-replace', {'PYTHONUTF8':'1','PYTHONIOENCODING':'latin-1:replace'}, 'iso8859-1','replace','utf-8','utf-8',1),
)


def effective_encoding(env, setting):
    name,updates,out,errors,child,filesystem,mode=setting
    code="import json,locale,sys; print(json.dumps([sys.stdout.encoding,sys.stdout.errors,sys.stderr.encoding,sys.stderr.errors,locale.getpreferredencoding(False),sys.getfilesystemencoding(),sys.getfilesystemencodeerrors(),sys.flags.utf8_mode]))"
    result=run_command([sys.executable,'-B','-c',code],cwd=ROOT,env=env)
    if result.returncode or result.stderr or result.timed_out:raise AssertionError('文字コードの実効設定を確認できない')
    actual=json.loads(result.stdout)
    for index in (0,2,4,5):actual[index]=codecs.lookup(actual[index]).name
    expected=[out,errors,out,'backslashreplace',child,'utf-8' if sys.platform=='darwin' else filesystem,'surrogateescape',mode]
    if actual!=expected:raise AssertionError('指定した実効文字コードと一致しない: '+name)
    return filesystem if sys.platform!='darwin' else 'utf-8'


def old_print_expectation(payload, setting):
    name,updates,codec,errors,child,filesystem,mode=setting
    text=payload.decode('utf-8')
    try:return (0,text.encode(codec,errors),b'')
    except UnicodeEncodeError as error:
        return (1,b'',origin_traceback(error).encode(codec,'backslashreplace'))


def observation(path):
    """内容の共通記録に、atimeを除く物理情報を加える。リンク先は別rootで渡す。"""
    path = Path(path)
    result = snapshot_tree(path)
    for relative, value in result.items():
        st = (path / relative).lstat()
        value['physical'] = (st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino, st.st_size)
    return result


def unchanged(before, after):
    if before != after:
        raise AssertionError('比較中に入力が変わった')


def legacy(command, args, env, timeout=50, *, cwd=ROOT):
    if env.get('R4B_PROCESSES'):Path(env['R4B_PROCESSES']).write_bytes(b'')
    result=run_command([sys.executable, '-B', str(SCRIPTS / (command + '.py')), *args], cwd=cwd, env=env, timeout=timeout)
    assert_stub_absent(env)
    return result


def native(command, args, env, timeout=50, *, cwd=ROOT):
    if env.get('R4B_PROCESSES'):Path(env['R4B_PROCESSES']).write_bytes(b'')
    result=run_command([str(FIXTURE), command, *args], cwd=cwd, env=env, timeout=timeout)
    assert_stub_absent(env)
    return result


def assert_stub_absent(env):
    if env.get('R4B_PROCESSES'):
        for line in Path(env['R4B_PROCESSES']).read_bytes().splitlines():assert_absent_now(int(line))


def pair(command, args, env, roots=(), *, expected=None, compare=same_result, timeout=50):
    before = [observation(root) for root in roots]
    old = legacy(command, args, env, timeout)
    unchanged(before, [observation(root) for root in roots])
    new = native(command, args, env, timeout)
    unchanged(before, [observation(root) for root in roots])
    compare(old, new)
    if expected is not None:
        fixed(old, *expected)
        fixed(new, *expected)
    return old, new


def completed(result, args):
    return subprocess.CompletedProcess(args, result.returncode, result.stdout.decode(), result.stderr.decode())


def existing_suite():
    """既存の全メソッドをそのまま収集する。CLI呼出だけを同じ入力の比較へ包む。"""
    origin = load(ROOT / 'scripts/test_origin_repo.py', 'r4b_existing_origin')
    digest = load(ROOT / 'scripts/test_git_config_digest.py', 'r4b_existing_digest')
    class Origin(origin.OriginRepoTest):
        def run_script(self, *, directory=None, extra_env=None):
            env = dict(self.env)
            env.update(extra_env or {})
            args = [f'--dir={directory or self.repo}']
            old, _ = pair('origin-repo', args, env, [self.repo, Path(self.temp.name) / 'gitconfig'])
            return completed(old, args)
    class Digest(digest.GitConfigDigestTest):
        def run_raw(self, args, script=digest.SCRIPT):
            if script != digest.SCRIPT:
                # 元の1秒コピーを元のまま実行する。Rustの実10秒は専用ケースで確認する。
                return super().run_raw(args, script=script)
            old, _ = pair('git-config-digest', args, self.env, [self.repo, self.gitconfig])
            return completed(old, args)
    loader = unittest.TestLoader()
    suites = []
    manifests = {origin.OriginRepoTest: (29, '447d8b83bf769274c5343ad647a09542bd49c11217522a82566a0d04bb78de46'),
                 digest.GitConfigDigestTest: (17, '8e11f3397a8f6384db9b8c43545d9bd07d513f7f9c84b0703e2abee86c3ec2d2')}
    for original, wrapped in [(origin.OriginRepoTest, Origin), (digest.GitConfigDigestTest, Digest)]:
        names = loader.getTestCaseNames(original)
        if (len(names), hashlib.sha256('\n'.join(names).encode()).hexdigest()) != manifests[original]:
            raise AssertionError('既存ケースの固定一覧と一致しない')
        if not names or names != loader.getTestCaseNames(wrapped):
            raise AssertionError('既存ケースの収集が欠落した')
        suite = loader.loadTestsFromTestCase(wrapped)
        if suite.countTestCases() != len(names):
            raise AssertionError('既存ケースの件数が違う')
        suites.append(suite)
    return unittest.TestSuite(suites)


# argvとstdinを記録し、固定のバイト列を返すGit/SSH代用品。
STUB = r'''#!{python}
import base64,json,os,signal,sys,time
from pathlib import Path
kind=Path(sys.argv[0]).name
with open(os.environ['R4B_PROCESSES'],'ab') as f:f.write(str(os.getpid()).encode()+b'\n')
with open(os.environ['R4B_CALLS'],'ab') as f:
 f.write(json.dumps([kind,sys.argv[1:],os.read(0,1).hex()],ensure_ascii=True,separators=(',',':')).encode()+b'\n')
data=json.loads(Path(os.environ['R4B_PLAN']).read_text())
args=sys.argv[1:]
if kind=='git':
 args=args[2:]
 while args and (args[0].startswith('--no-') or args[0]=='-c'):
  args=args[2:] if args[0]=='-c' else args[1:]
 key=' '.join(os.fsencode(a).decode('utf-8','surrogateescape') for a in args)
else:key='ssh'
item=data[key]
if item.get('pid'):
 Path(item['pid']).write_text(str(os.getpid()))
if item.get('grandchild'):
 pid=os.fork()
 if pid==0:
  if not item.get('holdout',True):os.close(1)
  if not item.get('holderr',True):os.close(2)
  Path(item['grandchild']).write_text(str(os.getpid()))
  time.sleep(90);os._exit(0)
if item.get('sleep'):time.sleep(item['sleep'])
if item.get('signal'):os.kill(os.getpid(),item['signal'])
os.write(1,base64.b64decode(item.get('out','')))
os.write(2,base64.b64decode(item.get('err','')))
sys.exit(item.get('code',0))
'''


OUTPUT_FAULT_WRAPPER = r"""
import io,json,os,runpy,sys
realout,realerr=sys.stdout,sys.stderr
mode=os.environ['R4B_OUTPUT_FAULT']
class Writer:
 def __init__(self,remaining,flush):self.remaining=remaining;self.badflush=flush;self.parts=[]
 def write(self,text):
  if not self.remaining:raise OSError('r4b-write')
  count=min(len(text),self.remaining);self.remaining-=count;self.parts.append(text[:count]);return count
 def flush(self):
  if self.badflush:raise OSError('r4b-flush')
out=Writer(0 if mode.startswith('stdout-write') else 3 if mode=='stdout-partial' else 10**9,mode=='stdout-flush')
err=Writer(0 if 'stderr-write' in mode else 10**9,'stderr-flush' in mode)
sys.stdout,sys.stderr=out,err
script=sys.argv[1];sys.argv=sys.argv[1:]
code=0;exception=None
try:runpy.run_path(script,run_name='__main__')
except SystemExit as e:code=e.code
except BaseException as e:code=1;exception=type(e).__name__
for stream in (out,err):
 try:stream.flush()
 except BaseException as e:code=120;exception=type(e).__name__
sys.stdout,sys.stderr=realout,realerr
print(json.dumps(dict(code=code,exception=exception,stdout=''.join(out.parts),stderr=''.join(err.parts)),ensure_ascii=True,sort_keys=True,separators=(',',':')))
"""


def fault_observation(result, expected):
    if result.timed_out or result.returncode or result.stderr:
        raise AssertionError('出力故障の観測入口が失敗した')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise AssertionError('出力故障の観測キーが重複した')
            value[key]=item
        return value
    value=json.loads(result.stdout,object_pairs_hook=unique)
    if (type(value) is not dict or set(value)!=set(('code','exception','stdout','stderr'))
            or type(value['code']) is not int or type(value['stdout']) is not str or type(value['stderr']) is not str
            or (value['exception'] is not None and type(value['exception']) is not str) or value!=expected):
        raise AssertionError('出力故障の個別期待と一致しない')
    return value


def record(out=b'', err=b'', code=0, **kwargs):
    import base64
    return dict(out=base64.b64encode(out).decode(), err=base64.b64encode(err).decode(), code=code, **kwargs)


def origin_json(**changes):
    value = dict(origin=False, same=False, repo=None, form=None, vcs=False, reason='', push_url_sha256=None)
    value.update(changes)
    return (json.dumps(value, ensure_ascii=False) + '\n').encode()


class Contract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.plan_path = self.base / 'plan.json'
        self.calls = self.base / 'calls'
        self.processes=self.base/'processes'
        self.processes.write_bytes(b'')
        for name in ('git', 'ssh'):
            path = self.bin / name
            path.write_text(STUB.format(python=sys.executable))
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin), HOME=str(self.home), LC_ALL='C',COLUMNS='80',
                        R4B_PLAN=str(self.plan_path), R4B_CALLS=str(self.calls),R4B_PROCESSES=str(self.processes),PYTHONUTF8='1',PYTHONIOENCODING='utf-8',
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        for key in ('GIT_SSL_NO_VERIFY', 'GIT_SSH', 'GIT_SSH_COMMAND','R4B_OUTPUT_FAULT'):
            self.env.pop(key, None)
        self.plan = {}

    def run_pair(self, command, *, args=None, expected=None, compare=same_result, roots=(), timeout=50):
        self.plan_path.write_text(json.dumps(self.plan))
        args = [f'--dir={self.repo}'] if args is None else args
        before = [observation(p) for p in (self.repo, self.home, *roots)]
        results = []
        calls = []
        for runner in (legacy, native):
            self.calls.write_bytes(b'')
            results.append(runner(command, args, self.env, timeout))
            calls.append(self.calls.read_bytes())
            unchanged(before, [observation(p) for p in (self.repo, self.home, *roots)])
        compare(*results)
        self.assertEqual(calls[0], calls[1], 'Git/SSHの呼出順が違う')
        for line in calls[0].splitlines():
            name, argv, stdin = json.loads(line)
            self.assertEqual('', stdin)
            if name == 'git':
                self.assertEqual(['-C', str(self.repo)], argv[:2])
                self.assertEqual(list(SAFE_GIT), argv[2:2 + len(SAFE_GIT)])
        if expected is not None:
            for result in results:
                fixed(result, *expected)
        return results

    def origin(self, fetch=b'https://github.com/o/r.git', push=None):
        self.plan = {'remote': record(b'origin\n'), 'config --get remote.origin.vcs': record(code=1),
                     'remote get-url --all origin': record(fetch + b'\n'),
                     'remote get-url --push --all origin': record((push if push is not None else fetch) + b'\n'),
                     'config --type=bool --get-urlmatch http.sslverify ' + (push if push is not None else fetch).decode(): record(code=1),
                     'config --get core.sshCommand': record(code=1),
                     'ssh': record(SSH_SETTINGS)}

    def encoding_env(self, setting):
        env=dict(self.env)
        for key in ('PYTHONIOENCODING','PYTHONUTF8','PYTHONCOERCECLOCALE','LC_ALL','LC_CTYPE','LANG'):
            env.pop(key,None)
        env.update(LC_ALL='C',LANG='C')
        env.update(setting[1])
        filesystem=effective_encoding(env,setting)
        return env,filesystem

    def expected_calls(self, operations, filesystem='utf-8', directory=None):
        calls=[]
        for operation in operations:
            if operation==['ssh']:
                name='ssh';argv=['-G','--','git@github.com']
            else:
                name='git';argv=['-C',str(self.repo) if directory is None else directory,*SAFE_GIT,*operation]
            decoded=[os.fsencode(a).decode(filesystem,'surrogateescape') for a in argv]
            calls.append(json.dumps([name,decoded,''],ensure_ascii=True,separators=(',',':')).encode()+b'\n')
        return b''.join(calls)

    def encoding_pair(self, command, args, setting, old_expected, new_expected, old_operations, new_operations):
        env,filesystem=self.encoding_env(setting)
        self.plan_path.write_text(json.dumps(self.plan))
        before=[observation(self.repo),observation(self.home)]
        results=[];calls=[]
        for runner in (legacy,native):
            self.calls.write_bytes(b'')
            results.append(runner(command,args,env))
            calls.append(self.calls.read_bytes())
            unchanged(before,[observation(self.repo),observation(self.home)])
        fixed_pair(*results,old_expected,new_expected,*calls,self.expected_calls(old_operations,filesystem),self.expected_calls(new_operations,filesystem))
        if setting[2]=='utf-8' and setting[4]=='utf-8' and filesystem=='utf-8' and old_expected==new_expected:
            same_result(*results)
        return results

    def argument_pair(self, command, args, setting, old_expected, new_expected, old_operations=(), new_operations=(), *, directory=None, columns=None):
        env,filesystem=self.encoding_env(setting)
        # この節の幅を固定する。COLUMNS自体の境界は呼出側で生バイトを渡す。
        env['COLUMNS']=b'80' if columns is None else columns
        self.plan_path.write_text(json.dumps(self.plan))
        before=[observation(self.repo),observation(self.home)]
        raw_before=tuple(os.fsencode(arg) for arg in args)+(os.fsencode(env['COLUMNS']),)
        results=[];calls=[]
        for runner in (legacy,native):
            self.calls.write_bytes(b'')
            results.append(runner(command,args,env,cwd=self.repo))
            calls.append(self.calls.read_bytes())
            raw_after=tuple(os.fsencode(arg) for arg in args)+(os.fsencode(env['COLUMNS']),)
            unchanged(before,[observation(self.repo),observation(self.home)])
        argument_contract(*results,old_expected,new_expected,*calls,self.expected_calls(old_operations,filesystem,directory),self.expected_calls(new_operations,filesystem,directory),raw_before,raw_after,before,[observation(self.repo),observation(self.home)])
        if filesystem=='utf-8' and old_expected==new_expected:
            same_result(*results)

    def test_argument_numbers_under_every_effective_encoding(self):
        # 各数字名を実在させる。分類が変わるとGitを呼ぶかどうかも変わる。
        numbers=['-1','-1.5','-.5','-١','-١.٢','-.٥','-१२','-１２.５']
        numbers += [value+'\n' for value in numbers]
        for value in numbers:(self.repo/value).mkdir()
        self.plan={'remote':record()};self.digest_plan()
        operations=[['rev-parse','--path-format=absolute','--git-dir','--git-common-dir','--git-path','config.worktree']]
        material={'config':{'kind':'absent','path':str(self.common/'config')},'config.worktree':{'kind':'absent','path':str(self.admin/'config.worktree')},'git-common-dir':str(self.common),'git-dir':str(self.admin),'config.worktree-git-path':str(self.admin/'resolved')}
        digest=('sha256:'+hashlib.sha256(json.dumps(material,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode()).hexdigest()+'\n').encode()
        for setting in ENCODINGS:
            _,filesystem=self.encoding_env(setting)
            for value in numbers:
                old_accept=filesystem=='utf-8' or value.isascii()
                for command in COMMANDS:
                    with self.subTest(setting=setting[0],value=value,command=command):
                        old_accepted=old_print_expectation(origin_json(reason='origin が無い'),setting) if command=='origin-repo' else (0,digest,b'')
                        new_accepted=(0,origin_json(reason='origin が無い'),b'') if command=='origin-repo' else (0,digest,b'')
                        ops=[['remote']] if command=='origin-repo' else operations
                        old=old_accepted if old_accept else argument_error(command,'argument --dir: expected one argument',codec=setting[2])
                        self.argument_pair(command,['--dir',value],setting,old,new_accepted,ops if old_accept else (),ops,directory=value)
                old=(2,b'','--expect の形が sha256:<64 桁の 16 進の小文字> でない\n'.encode(setting[2],'backslashreplace')) if old_accept else argument_error('git-config-digest','argument --expect: expected one argument',codec=setting[2])
                new=(2,b'','--expect の形が sha256:<64 桁の 16 進の小文字> でない\n'.encode())
                self.argument_pair('git-config-digest',['--dir',str(self.repo),'--expect',value],setting,old,new)

    def test_argument_diagnostics_are_individually_fixed(self):
        self.plan={'remote':record()}
        for setting in ENCODINGS:
            _,filesystem=self.encoding_env(setting)
            for command in COMMANDS:
                for raw in ('é'.encode(),b'\xff'):
                    old_text=raw.decode(filesystem,'surrogateescape')
                    new_text=raw.decode('utf-8','surrogateescape')
                    for prefix in (b'--help=',b'--'):
                        arg=prefix+raw
                        old_message=('argument -h/--help: ignored explicit argument '+repr(old_text)) if prefix==b'--help=' else 'unrecognized arguments: --'+old_text
                        new_message=('argument -h/--help: ignored explicit argument '+repr(new_text)) if prefix==b'--help=' else 'unrecognized arguments: --'+new_text
                        args=[arg] if prefix==b'--help=' else [b'--dir',os.fsencode(self.repo),arg]
                        self.argument_pair(command,args,setting,argument_error(command,old_message,codec=setting[2]),argument_error(command,new_message))
                    self.argument_pair(command,[b'--dir',os.fsencode(self.repo),raw],setting,argument_error(command,'unrecognized arguments: '+old_text,codec=setting[2]),argument_error(command,'unrecognized arguments: '+new_text))
                # 非ASCIIでも数字でない引数は値へ誤分類しない。
                self.argument_pair(command,['--dir','-١x'],setting,argument_error(command,'argument --dir: expected one argument',codec=setting[2]),argument_error(command,'argument --dir: expected one argument'))
                # =指定の値は、先頭が-でも数字の分類を経由しない。
                self.argument_pair(command,['--dir=-١-missing'],setting,(2,b'','--dir がディレクトリでない\n'.encode(setting[2],'backslashreplace')),(2,b'','--dir がディレクトリでない\n'.encode()))

    def test_columns_under_every_effective_encoding(self):
        # 幅の固定期待は30/80。数字/空白を文字化する設定だけで旧幅が変わる。
        cases=[(b'30',30,False),(b'80',80,False),('٣٠'.encode(),30,True),('+٣٠'.encode(),30,True),('٣_٠'.encode(),30,True),('３０'.encode(),30,True),('\u00a030\u00a0'.encode(),30,True),('\u200330\u2003'.encode(),30,True),(b' 30\t',30,False),(b'\xff30',80,False),('٣٠'.encode()+b'\xff',80,False)]
        self.plan={}
        for setting in ENCODINGS:
            _,filesystem=self.encoding_env(setting)
            for raw,new_width,unicode_required in cases:
                old_width=80 if unicode_required and filesystem!='utf-8' else new_width
                for command in COMMANDS:
                    for help_mode in (False,True):
                        with self.subTest(setting=setting[0],columns=raw,command=command,help=help_mode):
                            if help_mode:
                                old=(0,ARGUMENT_HELP[command,old_width].encode(setting[2]),b'')
                                new=(0,ARGUMENT_HELP[command,new_width].encode(),b'')
                            else:
                                message='the following arguments are required: --dir'
                                old=argument_error(command,message,width=old_width,codec=setting[2])
                                new=argument_error(command,message,width=new_width)
                            self.argument_pair(command,['--help'] if help_mode else [],setting,old,new,columns=raw)

    def test_encoding_outputs_are_individually_fixed(self):
        for setting in ENCODINGS:
            with self.subTest(setting=setting[0]):
                self.plan={'remote':record()}
                payload=origin_json(reason='origin が無い')
                self.encoding_pair('origin-repo',[f'--dir={self.repo}'],setting,old_print_expectation(payload,setting),(0,payload,b''),[['remote']],[['remote']])
                for command in COMMANDS:
                    message='--dir がディレクトリでない\n'
                    self.encoding_pair(command,['--dir=/r4b-missing'],setting,(2,b'',message.encode(setting[2],'backslashreplace')),(2,b'',message.encode()),[],[])
                    usage=f'usage: {command}.py [-h] --dir DIR'+(' [--expect EXPECT]' if command=='git-config-digest' else '')+'\n'
                    expected=(usage+f'{command}.py: error: the following arguments are required: --dir\n').encode()
                    self.encoding_pair(command,[],setting,(2,b'',expected),(2,b'',expected),[],[])
                self.plan={};self.digest_plan()
                material={'config':{'kind':'absent','path':str(self.common/'config')},'config.worktree':{'kind':'absent','path':str(self.admin/'config.worktree')},'git-common-dir':str(self.common),'git-dir':str(self.admin),'config.worktree-git-path':str(self.admin/'resolved')}
                value=('sha256:'+hashlib.sha256(json.dumps(material,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode()).hexdigest()+'\n').encode()
                ops=[['rev-parse','--path-format=absolute','--git-dir','--git-common-dir','--git-path','config.worktree']]
                self.encoding_pair('git-config-digest',[f'--dir={self.repo}'],setting,(0,value,b''),(0,value,b''),ops,ops)

    def test_encoding_git_and_ssh_decoding_each_stream(self):
        github='git@github.com:o/r.git'
        https='https://github.com/o/r.git'
        for setting in ENCODINGS:
            for kind in ('git','ssh'):
                for payload in (b'ASCII', 'é'.encode(), b'\xff'):
                    for stream in ('stdout','stderr','both'):
                        with self.subTest(setting=setting[0],kind=kind,payload=payload,stream=stream):
                            self.origin((github if kind=='ssh' else https).encode())
                            if kind=='git':
                                out=b'origin\n'+(payload+b'\n' if stream in ('stdout','both') else b'')
                                err=payload if stream in ('stderr','both') else b''
                                self.plan['remote']=record(out,err)
                            else:
                                out=SSH_SETTINGS+(b'unknown '+payload+b'\n' if stream in ('stdout','both') else b'')
                                err=payload if stream in ('stderr','both') else b''
                                self.plan['ssh']=record(out,err)
                            decoder=setting[4]
                            failure=None;which=None
                            for raw,name in [(out,'stdout'),(err,'stderr')]:
                                try:raw.decode(decoder)
                                except UnicodeDecodeError as exc:failure=exc;which=name;break
                            rust_bad=payload==b'\xff'
                            ops=[['remote'],['config','--get','remote.origin.vcs'],['remote','get-url','--all','origin'],['remote','get-url','--push','--all','origin']]
                            ops += [['config','--get','core.sshCommand'],['ssh']] if kind=='ssh' else [['config','--type=bool','--get-urlmatch','http.sslverify',https]]
                            base=dict(origin=True,same=True,form='scp' if kind=='ssh' else 'https',push_url_sha256='sha256:'+hashlib.sha256((github if kind=='ssh' else https).encode()).hexdigest())
                            accepted=origin_json(**base,repo='github.com/o/r')
                            rejected=origin_json(**base,reason='ssh -G の出力を読めない')
                            if failure is not None and kind=='git':
                                old=(1,b'',origin_traceback(failure,decode_stream=which).encode(setting[2],'backslashreplace'))
                                old_ops=[['remote']]
                            else:
                                old=old_print_expectation(rejected if failure is not None else accepted,setting)
                                old_ops=ops
                            new=(1,b'','git の出力を読めない\n'.encode()) if rust_bad and kind=='git' else (0,rejected if rust_bad else accepted,b'')
                            self.encoding_pair('origin-repo',[f'--dir={self.repo}'],setting,old,new,old_ops,[['remote']] if rust_bad and kind=='git' else ops)

    def test_encoding_digest_paths_links_and_expect_are_individually_fixed(self):
        self.admin=self.repo/'管理😀';self.admin.mkdir()
        self.common=self.repo/'共通é';self.common.mkdir()
        external=self.repo/'外部';external.mkdir()
        target=external/'設定';target.write_bytes(b'fixture')
        config=self.common/'config';config.symlink_to(os.path.relpath(target,self.common))
        worktree=self.admin/'config.worktree'
        raw_location=os.fsencode(str(self.admin))+b'/resolved-\xff'
        records=b'nonascii\n\xc3\xa9\0raw\n\xff\0flag\0'
        self.plan={'rev-parse --path-format=absolute --git-dir --git-common-dir --git-path config.worktree':record(os.fsencode(str(self.admin))+b'\n'+os.fsencode(str(self.common))+b'\n'+raw_location+b'\n'),f'config --file {config} --no-includes --list -z':record(records)}
        ops=[['rev-parse','--path-format=absolute','--git-dir','--git-common-dir','--git-path','config.worktree'],['config','--file',str(config),'--no-includes','--list','-z']]
        def expected_value(codec):
            decode=lambda value:os.fsencode(value).decode(codec,'surrogateescape')
            material={'git-dir':decode(self.admin),'git-common-dir':decode(self.common),'config.worktree-git-path':raw_location.decode(codec,'surrogateescape'),'config':{'path':decode(config),'kind':'symlink','target':decode(os.readlink(config)),'entries':[['nonascii','é'],['raw','\udcff'],['flag',None]]},'config.worktree':{'path':decode(worktree),'kind':'absent'}}
            return 'sha256:'+hashlib.sha256(json.dumps(material,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode('ascii')).hexdigest()
        rust_value=expected_value('utf-8')
        for setting in ENCODINGS:
            _,filesystem=self.encoding_env(setting)
            old_value=expected_value(filesystem)
            self.encoding_pair('git-config-digest',[f'--dir={self.repo}'],setting,(0,(old_value+'\n').encode(),b''),(0,(rust_value+'\n').encode(),b''),ops,ops)
            # 各実装の自分の期待値。旧/Rustを同じ期待値へ潰さない。
            if old_value==rust_value:
                self.encoding_pair('git-config-digest',[f'--dir={self.repo}','--expect='+rust_value],setting,(0,b'',b''),(0,b'',b''),ops,ops)
            else:
                mismatch='git 設定のダイジェストが --expect と一致しない\n'
                self.encoding_pair('git-config-digest',[f'--dir={self.repo}','--expect='+old_value],setting,(0,b'',b''),(1,b'',mismatch.encode()),ops,ops)
                self.encoding_pair('git-config-digest',[f'--dir={self.repo}','--expect='+rust_value],setting,(1,b'',mismatch.encode(setting[2],'backslashreplace')),(0,b'',b''),ops,ops)

    def test_origin_fixed_order_and_early_returns(self):
        cases = [({'remote': record()}, origin_json(reason='origin が無い'), ['remote']),
                 ({'remote': record(b'origin\n'), 'config --get remote.origin.vcs': record(b'hg\n')},
                  origin_json(origin=True, vcs=True, reason='remote.origin.vcs がある'), ['remote', 'config'])]
        for plan, expected, names in cases:
            self.plan = plan
            self.run_pair('origin-repo', expected=(0, expected, b''))
            self.assertEqual(len(names), len(self.calls.read_bytes().splitlines()))
        self.origin()
        self.run_pair('origin-repo', expected=(0, origin_json(origin=True, same=True, repo='github.com/o/r', form='https', push_url_sha256='sha256:' + hashlib.sha256(b'https://github.com/o/r.git').hexdigest()), b''))
        self.assertEqual(5, len(self.calls.read_bytes().splitlines()))
        self.origin(push=b'https://github.com/o/other.git')
        self.run_pair('origin-repo')
        self.assertEqual(4, len(self.calls.read_bytes().splitlines()))

    def test_origin_invalid_git_utf8_each_stream(self):
        for out, err in [(b'\xff', b''), (b'', b'\xff'), (b'\xff', b'\xfe')]:
            self.plan = {'remote': record(out, err)}
            self.run_pair('origin-repo', compare=lambda old,new:approved_decode(old,new,stream='stdout' if out else 'stderr'))

    def test_origin_line_endings_and_signal(self):
        for separator in ('\r\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            self.plan = {'remote': record(('other' + separator).encode())}
            self.run_pair('origin-repo', expected=(0, origin_json(reason='origin が無い'), b''))
        self.plan = {'remote': record(signal=signal.SIGTERM)}
        self.run_pair('origin-repo', expected=(2, b'', 'git remote が失敗した(終了コード -15)\n'.encode()))

    def test_ssh_invalid_bytes_launch_failure_and_signal(self):
        for out, err in [(b'\xff', b''), (b'', b'\xff')]:
            self.origin(b'git@github.com:o/r.git')
            self.plan['ssh'] = record(out, err)
            self.run_pair('origin-repo')
        self.origin(b'git@github.com:o/r.git')
        self.plan['ssh'] = record(signal=signal.SIGTERM)
        self.run_pair('origin-repo')
        (self.bin / 'ssh').unlink()
        self.run_pair('origin-repo')

    def test_arguments_and_order(self):
        for command in COMMANDS:
            for args in [[], ['--help'], ['-hh'], ['-h=bad'], ['--unknown'], ['--dir'], ['--dir='],
                         ['--d', str(self.repo)], ['--dir', str(self.repo), '--dir', '/missing'],
                         ['--dir', '-١\n'], ['--dir', '-.5'], ['--', '--dir', str(self.repo)],
                         ['--dir', str(self.repo), 'extra'], ['--dir', str(self.repo), '--unknown']]:
                self.plan = {'remote': record()}
                # digest must have a valid rev-parse if the argument is accepted.
                self.digest_plan()
                self.run_pair(command, args=args)
            if command == 'git-config-digest':
                self.run_pair(command, args=['--dir=/missing', '--expect=bad'], expected=(2, b'', '--expect の形が sha256:<64 桁の 16 進の小文字> でない\n'.encode()))

    def test_real_stdout_terminal_width_and_columns_precedence(self):
        env=dict(self.env,PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
        self.plan={};self.plan_path.write_text(json.dumps(self.plan))
        before=[observation(self.repo),observation(self.home)]
        for terminal_width in (30,80):
            for columns in (None,b'bad',b'0',b'-1',b'\xff',b'60',b'80',b'30'):
                case_env=dict(env)
                if columns is None:case_env.pop('COLUMNS',None)
                else:case_env['COLUMNS']=columns
                selected=int(columns) if columns in (b'30',b'60',b'80') else terminal_width
                for command in COMMANDS:
                    for help_mode in (False,True):
                        args=['--help'] if help_mode else []
                        old=run_pty([sys.executable,'-B',str(SCRIPTS/(command+'.py')),*args],columns=terminal_width,env=case_env)
                        new=run_pty([str(FIXTURE),command,*args],columns=terminal_width,env=case_env)
                        terminal_contract(old,new,command=command,columns=selected,help_mode=help_mode)
                        unchanged(before,[observation(self.repo),observation(self.home)])

    def test_help_and_usage_at_narrow_columns(self):
        for command in COMMANDS:
            for width in ('0','1','12','20','30','40','60','80','+٣٠','1_0','bad'):
                for args in (['--help'],[],['--dir']):
                    env=dict(self.env,COLUMNS=width)
                    pair(command,args,env,[self.repo,self.home])

    def digest_plan(self):
        self.admin = self.repo / 'admin'
        self.common = self.repo / 'common'
        self.admin.mkdir(exist_ok=True)
        self.common.mkdir(exist_ok=True)
        self.plan['rev-parse --path-format=absolute --git-dir --git-common-dir --git-path config.worktree'] = record(os.fsencode(f'{self.admin}\n{self.common}\n{self.admin}/resolved\n'))

    def test_digest_independent_material_and_expect(self):
        self.digest_plan()
        config = self.common / 'config'
        config.write_bytes(b'not read directly')
        raw_entries = b'z\n1\0empty\n\0flag\0dup\nx\0dup\ny\0raw\n\xff\0emoji\n\xf0\x9f\x98\x80\0'
        self.plan[f'config --file {config} --no-includes --list -z'] = record(raw_entries)
        material = {'git-dir': str(self.admin), 'git-common-dir': str(self.common),
                    'config.worktree-git-path': str(self.admin / 'resolved'),
                    'config': {'path': str(config), 'kind': 'file', 'entries': [['z','1'], ['empty',''], ['flag',None], ['dup','x'], ['dup','y'], ['raw','\udcff'], ['emoji','😀']]},
                    'config.worktree': {'path': str(self.admin / 'config.worktree'), 'kind': 'absent'}}
        expected = ('sha256:' + hashlib.sha256(json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()).encode()
        self.run_pair('git-config-digest', expected=(0, expected+b'\n', b''))
        self.run_pair('git-config-digest', args=[f'--dir={self.repo}', '--expect='+expected.decode()], expected=(0,b'',b''))
        self.run_pair('git-config-digest', args=[f'--dir={self.repo}', '--expect=sha256:'+'0'*64], expected=(1,b'','git 設定のダイジェストが --expect と一致しない\n'.encode()))

    def test_digest_literal_sha_from_fixed_absolute_locations(self):
        self.assertFalse(Path('/__dev_workflow_r4b_fixed__').exists())
        self.plan={'rev-parse --path-format=absolute --git-dir --git-common-dir --git-path config.worktree':record(b'/__dev_workflow_r4b_fixed__/git\n/__dev_workflow_r4b_fixed__/common\n/__dev_workflow_r4b_fixed__/worktree\n')}
        self.run_pair('git-config-digest',expected=(0,b'sha256:42591488aa710e1de3814c22ad606f546825036bb9c712e7e690a1a022dd760d\n',b''))

    def test_digest_locations_malformed_and_signal(self):
        key='rev-parse --path-format=absolute --git-dir --git-common-dir --git-path config.worktree'
        for out in (b'', b'a\nb\nc', b'a\nb\nc\n\n', b'a\n\nc\n', b'a\nb\nc\nd\n'):
            self.plan={key:record(out)}
            self.run_pair('git-config-digest', expected=(2,b'','git rev-parse の出力が 3 行でない\n'.encode()))
        self.plan={key:record(signal=signal.SIGTERM)}
        self.run_pair('git-config-digest', expected=(2,b'','git rev-parse が失敗した(終了コード -15)\n'.encode()))

    def test_digest_non_regular_and_link_spellings(self):
        import socket
        self.digest_plan()
        path=self.admin/'config.worktree'
        external=self.base/'external'
        external.mkdir()
        target=external/'target'
        target.write_bytes(b'fixture')
        entries=record(b'include.path\nsecret-value\0')
        self.plan[f'config --file {path} --no-includes --list -z']=entries
        values=[]
        for spelling in (str(target), os.path.relpath(target, path.parent)):
            path.symlink_to(spelling)
            old,_=self.run_pair('git-config-digest',roots=[external])
            values.append(old.stdout)
            path.unlink()
        self.assertNotEqual(*values)
        for kind in ('dangling','directory','fifo','socket','enotdir'):
            with self.subTest(kind=kind):
                server=None
                if kind=='dangling':path.symlink_to(external/'missing')
                elif kind=='directory':path.mkdir()
                elif kind=='fifo':os.mkfifo(path)
                elif kind=='socket':
                    server=socket.socket(socket.AF_UNIX);server.bind(str(path))
                else:path.symlink_to(target/'child')
                try:
                    self.run_pair('git-config-digest',roots=[external])
                    self.assertEqual(1,len(self.calls.read_bytes().splitlines()))
                finally:
                    if server:server.close()
                    if path.is_dir() and not path.is_symlink():path.rmdir()
                    else:path.unlink()

    def test_digest_non_utf8_paths_and_json(self):
        self.digest_plan()
        config=self.common/'config'
        config.write_bytes(b'fixture')
        # --git-pathの生パスは独立した項目。Macで作れない名前も、同じ物理入力の
        # Git代用品から両実装へ渡す。実体の所在は既存の有効なパスのままにする。
        raw_git_path=os.fsencode(str(self.admin))+b'/raw-\xff-\xf0\x9f\x98\x80'
        self.plan={'rev-parse --path-format=absolute --git-dir --git-common-dir --git-path config.worktree':record(os.fsencode(f'{self.admin}\n{self.common}\n')+raw_git_path+b'\n'),
                   f'config --file {config} --no-includes --list -z':record(b'k\n\x00\xff\n\x01\x08\x0c\r\t\\"\0')}
        self.run_pair('git-config-digest')

    def test_os_independent_old_and_rust_write_flush_faults(self):
        for command in COMMANDS:
            self.plan={'remote':record()}
            if command=='git-config-digest':self.digest_plan()
            self.plan_path.write_text(json.dumps(self.plan))
            if command=='origin-repo':normal=origin_json(reason='origin が無い').decode()
            else:
                material={'config':{'kind':'absent','path':str(self.common/'config')},'config.worktree':{'kind':'absent','path':str(self.admin/'config.worktree')},'git-common-dir':str(self.common),'git-dir':str(self.admin),'config.worktree-git-path':str(self.admin/'resolved')}
                normal='sha256:'+hashlib.sha256(json.dumps(material,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode()).hexdigest()+'\n'
            for mode in ('stdout-write','stdout-partial','stdout-flush','stderr-write','stderr-flush','stdout-write+stderr-write','stdout-write+stderr-flush'):
                args=['--dir=/r4b-missing'] if mode.startswith('stderr') else [f'--dir={self.repo}']
                env=dict(self.env,R4B_OUTPUT_FAULT=mode,PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
                before=[observation(self.repo),observation(self.home)]
                old=run_command([sys.executable,'-B','-c',OUTPUT_FAULT_WRAPPER,str(SCRIPTS/(command+'.py')),*args],cwd=ROOT,env=env)
                new=native(command,args,env)
                unchanged(before,[observation(self.repo),observation(self.home)])
                partial=normal[:3] if mode=='stdout-partial' else normal if mode=='stdout-flush' else ''
                detail='--dir がディレクトリでない\n' if mode.startswith('stderr') and mode=='stderr-flush' else ''
                old_expected=dict(code=120 if 'flush' in mode else 1,exception='OSError',stdout=partial,stderr=detail)
                diagnostic='dev-workflow: 標準出力に書けません: Broken pipe (os error 32)\n'
                native_err=detail if mode.startswith('stderr') else '' if mode=='stdout-write+stderr-write' else diagnostic
                new_expected=dict(code=1,exception=None,stdout=partial,stderr=native_err)
                fault_observation(old,old_expected);fault_observation(new,new_expected)

    def test_permission_failure_has_fixed_safe_diagnostic(self):
        self.digest_plan()
        locked=self.repo/'locked';locked.mkdir()
        (locked/'config.worktree').write_bytes(b'fixture')
        key='rev-parse --path-format=absolute --git-dir --git-common-dir --git-path config.worktree'
        self.plan[key]=record(os.fsencode(f'{locked}\n{self.common}\n{locked}/config.worktree\n'))
        locked.chmod(0)
        try:
            self.plan_path.write_text(json.dumps(self.plan))
            before=locked.lstat()
            for runner in (legacy,native):
                fixed(runner('git-config-digest',[f'--dir={self.repo}'],self.env),2,stderr='設定ファイルを調べられない: Permission denied\n'.encode())
                after=locked.lstat()
                self.assertEqual((before.st_mode,before.st_mtime_ns,before.st_ctime_ns,before.st_ino,before.st_dev),(after.st_mode,after.st_mtime_ns,after.st_ctime_ns,after.st_ino,after.st_dev))
        finally:locked.chmod(0o755)

    def test_output_faults_dev_full_and_closed_pipe(self):
        for command in COMMANDS:
            self.plan={'remote':record()}
            if command=='git-config-digest':self.digest_plan()
            self.plan_path.write_text(json.dumps(self.plan))
            for failure in ('full','pipe','stderr-full','both-full'):
                if 'full' in failure and not Path('/dev/full').exists():
                    # Linux固有の実機ケース。共通write/flushはRust単体検査で必須実行する。
                    continue
                observed=[]
                for is_native in (False,True):
                    argv=([str(FIXTURE),command] if is_native else [sys.executable,'-B',str(SCRIPTS/(command+'.py'))])
                    argv += ['--dir=/r4b-missing'] if failure=='stderr-full' else [f'--dir={self.repo}']
                    if failure=='pipe':
                        read,write=os.pipe();os.close(read)
                        try:
                            with os.fdopen(write,'wb') as output:
                                done=subprocess.run(argv,env=self.env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.PIPE,timeout=20)
                        finally:pass
                    else:
                        with open('/dev/full','wb') as full:
                            done=subprocess.run(argv,env=self.env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE if failure=='stderr-full' else full,stderr=full if failure in ('stderr-full','both-full') else subprocess.PIPE,timeout=20)
                    observed.append(RunResult(done.returncode,done.stdout or b'',done.stderr or b'',False))
                old,new=observed
                if failure=='stderr-full':
                    fixed(old,120);fixed(new,1)
                else:
                    exception=b'BrokenPipeError: [Errno 32]' if failure=='pipe' else b'OSError: [Errno 28]'
                    if failure=='both-full':exception=None
                    diagnostic=('dev-workflow: 標準出力に書けません: Broken pipe (os error 32)\n'.encode() if failure=='pipe' else None)
                    approved_output(old,new,old_code=120,exception=exception,broken_diagnostic=failure=='both-full',diagnostic=diagnostic)

    def test_time_limits_are_per_child_and_origin_git_unlimited(self):
        self.plan={'remote':record(sleep=10.25)}
        started=time.monotonic()
        self.run_pair('origin-repo',expected=(0,origin_json(reason='origin が無い'),b''))
        self.assertGreater(time.monotonic()-started,20)
        self.digest_plan()
        key='rev-parse --path-format=absolute --git-dir --git-common-dir --git-path config.worktree'
        self.plan[key]['sleep']=5.1
        config=self.common/'config';config.write_bytes(b'fixture')
        self.plan[f'config --file {config} --no-includes --list -z']=record(b'k\nv\0',sleep=5.1)
        self.run_pair('git-config-digest')

    def test_timeout_with_live_and_exited_child_and_held_pipes(self):
        # 各コマンドで直接子の生存/終了と、孫のstdout/stderr保持を組み合わせる。
        for command in COMMANDS:
            for live in (False,True):
                for holdout,holderr in ((True,False),(False,True),(True,True)):
                    for runner in (legacy,native):
                        with self.subTest(command=command,live=live,holdout=holdout,holderr=holderr,runner=runner.__name__):
                            self.origin(b'git@github.com:o/r.git') if command=='origin-repo' else self.digest_plan()
                            key='ssh' if command=='origin-repo' else 'rev-parse --path-format=absolute --git-dir --git-common-dir --git-path config.worktree'
                            direct=self.base/'direct';grand=self.base/'grand'
                            for p in (direct,grand):
                                if p.exists():p.unlink()
                            self.plan[key]=record(pid=str(direct),grandchild=str(grand),holdout=holdout,holderr=holderr,sleep=90 if live else 0)
                            self.plan_path.write_text(json.dumps(self.plan))
                            before=[observation(self.repo),observation(self.home)]
                            try:
                                started=time.monotonic()
                                result=runner(command,[f'--dir={self.repo}'],self.env,20)
                                elapsed=time.monotonic()-started
                                self.assertGreaterEqual(elapsed,9)
                                self.assertLess(elapsed,16)
                                self.assertFalse(result.timed_out)
                                if command=='git-config-digest':fixed(result,2,stderr='git rev-parse が時間切れ(10 秒)\n'.encode())
                                else:self.assertEqual('ssh -G が時間切れ',json.loads(result.stdout)['reason'])
                                pid=int(direct.read_text())
                                assert_absent_now(pid)
                                unchanged(before,[observation(self.repo),observation(self.home)])
                            finally:
                                if grand.exists():
                                    pid=int(grand.read_text())
                                    if not wait_absent(pid,timeout=0):
                                        if str(self.bin) not in command_line(pid):raise AssertionError('孫の所属を確認できない')
                                        os.kill(pid,signal.SIGKILL)
                                    try:os.waitpid(pid,0)
                                    except ChildProcessError:pass
                                    self.assertTrue(wait_absent(pid,timeout=5),'後片付け後に孫が残った')


SAFE_GIT = ('--no-pager','--no-replace-objects','-c','core.fsmonitor=','-c','core.hooksPath=/dev/null',
            '-c','core.ignoreCase=false','-c','core.splitIndex=false','-c','core.ignoreStat=false',
            '-c','commit.gpgSign=false','-c','push.gpgSign=false','-c','filter.lfs.smudge=',
            '-c','filter.lfs.clean=','-c','filter.lfs.process=','-c','filter.lfs.required=false')
SSH_SETTINGS = b'hostname github.com\nport 22\nstricthostkeychecking ask\nuserknownhostsfile ~/.ssh/known_hosts\nglobalknownhostsfile /etc/ssh/ssh_known_hosts\nnohostauthenticationforlocalhost no\n'


def run_suite(suite):
    count = suite.countTestCases()
    if count == 0:
        raise AssertionError('検査ケースが0件')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != count or result.skipped:
        raise AssertionError('未実行・失敗・省略したケースがある')
    return count


@contextlib.contextmanager
def subreaper():
    libc=None;old=ctypes.c_int()
    if sys.platform.startswith('linux'):
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.prctl(37,ctypes.byref(old),0,0,0) or libc.prctl(36,1,0,0,0):raise OSError(ctypes.get_errno(),'R4B test subreaper')
    try:yield
    finally:
        if libc is not None and libc.prctl(36,old.value,0,0,0):raise OSError(ctypes.get_errno(),'restore R4B test subreaper')


def main(argv=None):
    global FIXTURE
    parser=argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    args=parser.parse_args(argv)
    FIXTURE=fixture_path(args.fixture)
    if sys.version_info[:2] != (3,10) or os.geteuid()==0:
        raise ValueError('R4B比較はPython3.10系・非rootで実行してください')
    # 専用判定そのものの検査を先に通す。
    module=load(Path(__file__).with_name('test_r4b.py'),'r4b_comparator_tests')
    module.subject=sys.modules[__name__]
    run_suite(unittest.defaultTestLoader.loadTestsFromModule(module))
    with subreaper():
        run_suite(existing_suite())
        run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Contract))
    print('R4B: 合格')
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (AssertionError,ValueError,OSError) as error:
        print(f'R4B: 不合格: {error}',file=sys.stderr)
        sys.exit(1)
