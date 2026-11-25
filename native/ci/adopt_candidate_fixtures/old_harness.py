"""旧 adopt-candidate.py へ、旧テストと同じ点で注入する比較用の起動器(R4C 用。旧版は変えない)。

    <旧 Python> -B old_harness.py <旧 adopt-candidate.py> <spec.json> [旧スクリプトの引数...]

spec の規則は Rust 側の `inject.rs` と同じ点・同じ意味を持つ(点: read・stat・link・unlink・rename・run・
precheck・marker・start。操作: write・unlink・mkdir・symlink・replace_inode・attacker_replace・git・pause)。
`main()` をそのまま呼ぶので、標準出力・標準エラー・終了値は旧スクリプトを直接起動した場合と同じ形になる。
"""
import sys

sys.dont_write_bytecode = True
import ctypes
import errno as errno_module
import importlib.util
import json
import os
import shutil
import subprocess

script, spec_path, *arguments = sys.argv[1:]
spec = json.load(open(spec_path, encoding='utf-8'))
SOURCE, DEST = '候補_例.md', '進行中_例.md'
BODY = spec.get('body', '')
root = arguments[arguments.index('--root') + 1]
rules = spec.get('rules', [])
counters = {}
module_spec = importlib.util.spec_from_file_location('adopt_old', script)
module = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(module)


def resolve(name):
    return {'source': SOURCE, 'dest': DEST}.get(name, name)


def count(point, key):
    counters[(point, key)] = counters.get((point, key), 0) + 1
    return counters[(point, key)]


def matching(point, key, number):
    result = []
    for rule in rules:
        if rule['point'] != point or rule.get('nth', 1) != number:
            continue
        wanted = rule.get('name', rule.get('match'))
        if wanted is not None and resolve(wanted) not in key:
            continue
        result.append(rule)
    return result


def component(name):
    """注入の対象は root の直下の 1 成分だけ。絶対パス・`..`・区切りは拒否する(Rust 側と同じ)。"""
    if not name or name in ('.', '..') or '/' in name or '\0' in name:
        raise SystemExit('注入の path は root の直下の 1 成分だけ: ' + repr(name))
    return os.path.join(root, name)


def perform(actions):
    for action in actions:
        op = action['op']
        path = component(action['path']) if 'path' in action else None
        text = BODY if action.get('text') == '@BODY' else action.get('text')
        if op == 'write':
            open(path, 'w', encoding='utf-8').write(text)
        elif op == 'unlink':
            os.unlink(path)
        elif op == 'mkdir':
            os.mkdir(path)
        elif op == 'symlink':
            os.symlink(action['target'], path)
        elif op == 'replace_inode':
            data = open(path, 'rb').read()
            spare = os.path.join(root, '.inode-swap')
            open(spare, 'wb').write(data)
            os.replace(spare, path)
        elif op == 'attacker_replace':
            attacker = os.path.join(root, 'attacker-replacement')
            open(attacker, 'w', encoding='utf-8').write(text)
            os.replace(attacker, path)
        elif op == 'git':
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
            subprocess.run(['git', '-C', root, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                            *action['args']], env=env, check=True, capture_output=True)
        elif op == 'pause':
            os.write(int(os.environ['R4C_FAULT_NOTIFY']), b'linked')
            wait = int(os.environ['R4C_FAULT_WAIT'])
            while os.read(wait, 16):
                pass
        else:
            raise SystemExit('未知の操作: ' + op)


real_read_candidate = module.read_candidate


def read_candidate(fd, name):
    rules_hit = matching('read', name, count('read', name))
    for rule in rules_hit:
        if 'raise' in rule:
            raise OSError(rule['raise'], os.strerror(rule['raise']), name)
    for rule in rules_hit:
        if rule.get('phase', 'before') == 'before':
            perform(rule.get('do', []))
    during = [a for rule in rules_hit if rule.get('phase') == 'during' for a in rule.get('do', [])]
    real_read = module.os.read
    if during:
        done = []

        def hooked(handle, amount):
            chunk = real_read(handle, amount)
            if chunk and not done:
                done.append(True)
                perform(during)
            return chunk
        module.os.read = hooked
    try:
        return real_read_candidate(fd, name)
    finally:
        module.os.read = real_read
        for rule in rules_hit:
            if rule.get('phase') == 'after':
                perform(rule.get('do', []))


module.read_candidate = read_candidate

real_stat = os.stat


def stat(path, *args, dir_fd=None, **kwargs):
    if dir_fd is None:
        return real_stat(path, *args, dir_fd=dir_fd, **kwargs)
    rules_hit = matching('stat', path, count('stat', path))
    for rule in rules_hit:
        if 'raise' in rule:
            raise OSError(rule['raise'], os.strerror(rule['raise']), path)
    for rule in rules_hit:
        perform(rule.get('do', []))
    return real_stat(path, *args, dir_fd=dir_fd, **kwargs)


module.os.stat = stat

real_link = os.link


def link(src, dst, **kwargs):
    rules_hit = matching('link', dst, count('link', dst))
    for rule in rules_hit:
        if rule.get('phase', 'before') == 'before':
            perform(rule.get('do', []))
    for rule in rules_hit:
        if 'raise' in rule:
            if rule['raise'] == errno_module.EEXIST:
                raise FileExistsError(rule['raise'], os.strerror(rule['raise']))
            raise OSError(rule['raise'], os.strerror(rule['raise']), src, None, dst)
    real_link(src, dst, **kwargs)
    for rule in rules_hit:
        if rule.get('phase') == 'after':
            perform(rule.get('do', []))


module.os.link = link

real_unlink = os.unlink


def unlink(name, *args, dir_fd=None, **kwargs):
    if dir_fd is None:
        return real_unlink(name, *args, dir_fd=dir_fd, **kwargs)
    rules_hit = matching('unlink', name, count('unlink', name))
    for rule in rules_hit:
        if rule.get('phase', 'before') == 'before':
            perform(rule.get('do', []))
    for rule in rules_hit:
        if 'raise' in rule:
            raise OSError(rule['raise'], os.strerror(rule['raise']), name)
    real_unlink(name, *args, dir_fd=dir_fd, **kwargs)
    for rule in rules_hit:
        if rule.get('phase') == 'after':
            perform(rule.get('do', []))


module.os.unlink = unlink

real_cdll = ctypes.CDLL


class FakeRename:
    def __init__(self, real):
        self.real = real
        self.argtypes = None
        self.restype = None

    def __call__(self, parent, source, parent2, destination, flags):
        name = os.fsdecode(source)
        rules_hit = matching('rename', name, count('rename', name))
        for rule in rules_hit:
            if rule.get('phase', 'before') == 'before':
                perform(rule.get('do', []))
        for rule in rules_hit:
            if 'raise' in rule:
                ctypes.set_errno(rule['raise'])
                return -1
        self.real.argtypes = self.argtypes
        self.real.restype = self.restype
        result = self.real(parent, source, parent2, destination, flags)
        saved = ctypes.get_errno()
        for rule in rules_hit:
            if rule.get('phase') == 'after':
                perform(rule.get('do', []))
        ctypes.set_errno(saved)
        return result


class FakeLibrary:
    def __init__(self, real):
        self.real = real

    def __getattr__(self, name):
        if name == 'renameat2':
            if any(rule['point'] == 'rename' and rule.get('missing') for rule in rules):
                raise AttributeError(name)
            return FakeRename(getattr(self.real, name))
        return getattr(self.real, name)


module.ctypes.CDLL = lambda *args, **kwargs: FakeLibrary(real_cdll(*args, **kwargs))

real_root_run = module.root_run
log_path = spec.get('log')
if log_path is not None and (os.path.dirname(os.path.realpath(log_path)) != os.path.dirname(os.path.realpath(spec_path))
                             or not os.path.basename(log_path)):
    raise SystemExit('log は仕様と同じフォルダの直下だけ')


def root_run(root_fd, argv, env, timeout):
    if argv[0] == 'bash':
        rules_hit = matching('precheck', '', count('precheck', ''))
        for rule in rules_hit:
            perform(rule.get('do', []))
        # 時間切れの注入は含めない(表示に出る argv は実部品の起動のしかたで決まる)。
        for rule in rules_hit:
            if 'result' in rule:
                result = rule['result']
                return subprocess.CompletedProcess(argv, result.get('code', 0), bytes.fromhex(result.get('stdout_hex', '')),
                                                   bytes.fromhex(result.get('stderr_hex', '')))
        return real_root_run(root_fd, argv, env, timeout)
    if log_path:
        entry = {'argv': [item.encode('utf-8', 'surrogateescape').hex() for item in argv],
                 'git_env': {k: v for k, v in sorted(env.items()) if k.startswith('GIT_')}}
        with open(log_path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry) + '\n')
    hits = []
    for token in sorted({resolve(rule.get('match')) for rule in rules if rule['point'] == 'run' and rule.get('match')}):
        if token in argv:
            hits += [rule for rule in matching('run', token, count('run', token)) if resolve(rule.get('match')) == token]
    for rule in hits:
        if rule.get('phase', 'before') == 'before':
            perform(rule.get('do', []))
    for rule in hits:
        if rule.get('timeout'):
            raise subprocess.TimeoutExpired(argv, timeout)
        if 'spawn_errno' in rule:
            raise OSError(rule['spawn_errno'], os.strerror(rule['spawn_errno']), argv[0])
    for rule in hits:
        if 'result' in rule:
            result = rule['result']
            return subprocess.CompletedProcess(argv, result.get('code', 0), bytes.fromhex(result.get('stdout_hex', '')),
                                               bytes.fromhex(result.get('stderr_hex', '')))
    result = real_root_run(root_fd, argv, env, timeout)
    if result.returncode == 0:
        for rule in hits:
            if rule.get('phase') == 'after':
                perform(rule.get('do', []))
    return result


module.root_run = root_run

if any(rule['point'] == 'marker' for rule in rules):
    real_validate = module.validate_candidate

    def validate(*args, **kwargs):
        value = real_validate(*args, **kwargs)
        os.environ['DEV_WORKFLOW_LOOP_ITER'] = 'late-marker'
        return value
    module.validate_candidate = validate

sys.argv = ['adopt-candidate.py', *arguments]
# 点 `start`: 起動(標準の fd の作成)が済んだ後・`main()` の前に止まる。比較器が端末の相手側を閉じるなどの窓を作る。
for rule in rules:
    if rule['point'] == 'start':
        perform(rule.get('do', []))
sys.exit(module.main())
