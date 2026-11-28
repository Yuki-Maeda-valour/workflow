#!/usr/bin/env python3
"""D2(#542)の比較: `loop.sh` の新しい読み込み器(`NATIVE_LOADER`)を経て起動した製品を、旧版の `loop.sh` の起動の形と比べる。

- 製品の実行ファイルは、環境変数 `DEV_WORKFLOW_TEST_NATIVE_BINARY` で渡す(未設定・空・相対・不在・実行不能は失敗)。
  比較の前に一時フォルダへ写し、その SHA-256 を親が保持する値として読み込み器へ渡す。Linux だけで流す(memfd を使う)。
- 読み込み器と補助(`PY_HELPER`)の字面は、実行のたびに `loop.sh` から読む(`read -r -d '' <名前> <<'PY'` の本文)。
- 旧版の側は、`loop.sh` と同じ字面で起動する(Python は比較器を動かした Python):
  - 直接の形: `<Python> -B <配布物の environment-guard.py> <引数>`(作業場所 `/`。`loop.sh` の開始時の 2 か所)
  - 読み込み器を経た形: `<Python> -I -c "$PY_HELPER" environment-run <guard の SHA-256> <guard のコピー> <引数>`(作業場所 `/`。残りの 7 か所)
  - `loop-state`: `<Python> <loop-state.py> <引数>`(`loop.sh` の作業場所。8 か所)
- 新しい側: `<Python> -I -c "$NATIVE_LOADER" <形> <LC_CTYPE の有無> <値> <受け継ぐ信号> <SHA-256> <製品> <guard の SHA-256 か -> <guard か -> <引数>`。
- 生の標準出力・標準エラー・終了コード・出力先の木・git の子が受けた `LC_CTYPE` を、加工せずに比べる。加工は認めた差の関数だけ
  (起動の失敗の `Current thread` の値〈2026-11-11 17:45〉・U4 の traceback の途中の行・U7 の閉じた fd)。
- 旧新それぞれの個別の期待にする物(通常の一致に数えない): Q4 の重なり・Q5(ハードの上限が実行ファイルより小さい)・U3(hook)・U7。
- 群ごとの件数を固定し、1 件でも違えば失敗。0 件・時間切れ・未起動を合格にしない。
"""
import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts'
LOOP_SH = SCRIPTS / 'loop.sh'
GUARD = SCRIPTS / 'environment-guard.py'
LOOP_STATE = SCRIPTS / 'loop-state.py'
PRODUCT_VARIABLE = 'DEV_WORKFLOW_TEST_NATIVE_BINARY'
PYTHON = sys.executable
TIMEOUT = 60
LOADER_LINE = 'ERROR [environment-guard] 保持した検査用コピーを安全に実行できない\n'.encode()
NATIVE_LINE = 'ERROR [dev-workflow] 保持した実行ファイルを安全に起動できない\n'.encode()
U3_LINE = 'ERROR [environment-guard] hook は R4P まで旧版の判定器で実行する\n'.encode()
HOOK_DENY = (json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
    "behavior": "deny", "message": "環境の保持値との照合に失敗"}}}) + '\n').encode()
COERCION_WARNING = (b'Python detected LC_CTYPE=C: LC_CTYPE coerced to C.UTF-8 (set another locale or PYTHONCOERCECLOCALE=0 '
                    b'to disable this locale coercion behavior).\n')
STDIN_FATAL = (b'Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\nPython runtime state: core initialized\n\n'
               b'Current thread 0x<THREAD> (most recent call first):\n  <no Python frame>\n')
STDOUT_FATAL = (b"Fatal Python error: init_sys_streams: can't initialize sys standard streams\nPython runtime state: core initialized\n"
                b'IsADirectoryError: [Errno 21] Is a directory: 1\n\nCurrent thread 0x<THREAD> (most recent call first):\n  <no Python frame>\n')
HASH_FATAL = (b'Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be "random" or an integer in range [0; 4294967295]\n'
              b'Python runtime state: preinitialized\n\n')
DIGITS_FATAL = (b'Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 '
                b'for unlimited.\nPython runtime state: preinitialized\n\n')
LEGACY_WARNING = (b'Python runtime initialized with LC_CTYPE=C (a locale with default ASCII encoding), which may cause Unicode '
                  b'compatibility problems. Using C.UTF-8, C.utf8, or UTF-8 (if available) as alternative Unicode-compatible locales '
                  b'is recommended.\n')
# 新しい読み込み器を、memfd・封の名前を持たない Python(python-build-standalone など)や、os.execve が fd を受けない Python と
# 同じにする前置き(比較器だけが字面の前に足す。本番の字面は変えない)。名前が無いだけなら、Linux の番号と ctypes の memfd_create で
# 旧版と同じ結果になる。fd を受けない os.execve では、パスで起動し直さずに 1 行と終了 20 で止まる。
STANDALONE_PRELUDES = {
    'no-memfd-names': ('import os, fcntl\n'
                       'for _name in ("memfd_create", "MFD_CLOEXEC", "MFD_ALLOW_SEALING"):\n    if hasattr(os, _name): delattr(os, _name)\n'
                       'for _name in ("F_ADD_SEALS", "F_GET_SEALS", "F_SEAL_SEAL", "F_SEAL_SHRINK", "F_SEAL_GROW", "F_SEAL_WRITE"):\n'
                       '    if hasattr(fcntl, _name): delattr(fcntl, _name)\n'),
    'no-fd-execve': 'import os\nos.supports_fd.discard(os.execve)\n',
}
STANDALONE_PRELUDES['no-names-no-fd-execve'] = STANDALONE_PRELUDES['no-memfd-names'] + STANDALONE_PRELUDES['no-fd-execve']
# 前置きごとの期待: 旧版と同じ(`same`)か、起動せずに止まる(`stop`)。
STANDALONE_EXPECT = {'no-memfd-names': 'same', 'no-fd-execve': 'stop', 'no-names-no-fd-execve': 'stop'}
# memfd を作る手段がどれも無い Python(ctypes も使えない)。起動せずに止まる。
NO_MEMFD_PRELUDE = STANDALONE_PRELUDES['no-memfd-names'] + ('import ctypes\n'
                                                            'def _refuse(*unused, **also):\n    raise OSError(38, "no memfd")\n'
                                                            'ctypes.CDLL = _refuse\n')
FORMS = ('direct', 'held', 'loop-state')
KINDS = {'direct': 'environment-guard', 'held': 'environment-guard-held', 'loop-state': 'loop-state'}
CURRENT_THREAD = re.compile(rb'Current thread 0x[0-9a-f]{16} \(most recent call first\):')
# 群ごとの項目の数(1 件でも違えば失敗)。`standalone` は `--standalone-python` を渡したときだけ流す。
EXPECTED = {
    'basic': 52, 'startup': 17, 'fsize': 18, 'fds': 15, 'sigpipe-locale': 80, 'locale': 324,
    'q4': 30, 'depth': 24, 'sigint': 4, 'loader': 29, 'selftest': 3,
}
EXPECTED_STANDALONE = 3


class Mismatch(AssertionError):
    pass


@dataclasses.dataclass
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    git: tuple = ()
    tree: object = None

    def key(self):
        return self.returncode, self.stdout, self.stderr


def heredoc(text, name):
    """`loop.sh` の `read -r -d '' <名前> <<'PY' || true` の本文(bash の `read` と同じく前後の空白・改行を除く)。"""
    start = f"\nread -r -d '' {name} <<'PY' || true\n"
    if text.count(start) != 1:
        raise Mismatch(f'loop.sh に {name} の字面がちょうど 1 か所ない')
    body, end, _ = text.split(start, 1)[1].partition('\nPY\n')
    if not end:
        raise Mismatch(f'loop.sh の {name} の終わりが無い')
    return body.strip(' \t\n')


def product_path(environ=None):
    value = (os.environ if environ is None else environ).get(PRODUCT_VARIABLE, '')
    if not value or not Path(value).is_absolute():
        raise ValueError(f'{PRODUCT_VARIABLE} に製品の実行ファイルの絶対パスが必要です')
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f'{PRODUCT_VARIABLE} の製品は実行可能な通常ファイルである必要があります')
    return path


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def launch(argv, *, env, cwd, stdin=None, close=(), directory=(), stderr_closed=False, ignore=(), fsize=None,
           timeout=TIMEOUT, stderr_file=None):
    """別のセッションで起動する。`close`・`directory` は閉じる fd・ディレクトリにする fd、`stderr_closed` は読む側の閉じたパイプ、
    `ignore` は無視にして受け継がせる信号、`fsize` は (ソフト, ハード) の RLIMIT_FSIZE、`stderr_file` は標準エラーにする通常ファイル
    (中身を結果の標準エラーにする)。起動できなければ例外のまま。"""
    def child():
        for number in ignore:
            signal.signal(number, signal.SIG_IGN)
        if fsize is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, fsize)
        for fd in directory:
            opened = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
            os.dup2(opened, fd)
            os.close(opened)
        for fd in close:
            os.close(fd)
    stderr = subprocess.PIPE
    handle = None
    if stderr_closed:
        read_end, handle = os.pipe()
        os.close(read_end)
        stderr = handle
    elif stderr_file is not None:
        handle = os.open(stderr_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        stderr = handle
    try:
        with subprocess.Popen(argv, env=env, cwd=cwd, start_new_session=True, preexec_fn=child,
                              stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=stderr) as process:
            try:
                out, err = process.communicate(stdin, timeout=timeout)
                if stderr_file is not None:
                    err = Path(stderr_file).read_bytes()
                return Result(process.returncode, out or b'', err or b'')
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                out, err = process.communicate()
                return Result(process.returncode, out or b'', err or b'', True)
    finally:
        if handle is not None:
            os.close(handle)


def tree(path):
    """出力先の木(名前・種類・mode・中身・リンク先)。無ければ None。"""
    path = Path(path)
    if not os.path.lexists(path):
        return None
    rows = []
    for current, folders, files in os.walk(path):
        folders.sort()
        for name in sorted(folders) + sorted(files):
            full = Path(current) / name
            info = full.lstat()
            relative = str(full.relative_to(path))
            if stat.S_ISLNK(info.st_mode):
                rows.append((relative, 'link', os.readlink(full)))
            elif stat.S_ISDIR(info.st_mode):
                rows.append((relative, 'dir', stat.S_IMODE(info.st_mode)))
            else:
                rows.append((relative, 'file', stat.S_IMODE(info.st_mode), full.read_bytes()))
    return (stat.S_IMODE(path.lstat().st_mode), tuple(rows))


def thread_normalized(stderr):
    return CURRENT_THREAD.sub(b'Current thread 0x<THREAD> (most recent call first):', stderr)


def strip_traceback_middle(stderr):
    """U4: `Traceback (most recent call last):` とそのブロックの最後の行の間の、字下げした行だけを除く。"""
    out, block = [], False
    for line in stderr.split(b'\n'):
        if line.startswith(b'Traceback (most recent call last):'):
            block = True
            out.append(line)
        elif block and line.startswith(b'  '):
            continue
        else:
            block = False
            out.append(line)
    return b'\n'.join(out)


def describe(result):
    return f'{result.returncode} {result.stdout[:200]!r} {result.stderr[:400]!r} git={result.git!r}'


class Tally:
    """項目の数。種類は、旧新の一致(`matched`)・旧新それぞれの個別の期待(`approved`)・旧版と比べない固定の確かめ(`fixed`。
    読み込み器の性質と止まり方)・比較器の自己検査(`selftest`)。4 つの和が項目の総数。"""
    KINDS = ('matched', 'approved', 'fixed', 'selftest')

    def __init__(self):
        self.groups = {}
        self.kinds = dict.fromkeys(self.KINDS, 0)
        self.notes = []

    def add(self, group, kind='matched'):
        self.groups[group] = self.groups.get(group, 0) + 1
        self.kinds[kind] += 1


class D2:
    """1 回の比較の配置(一時フォルダ)と、旧新の起動。"""

    def __init__(self, base, product):
        self.base = base
        text = LOOP_SH.read_text(encoding='utf-8')
        self.loader = heredoc(text, 'NATIVE_LOADER')
        self.helper = heredoc(text, 'PY_HELPER')
        bundle = base / 'bundle/linux'
        bundle.mkdir(parents=True)
        self.product = bundle / 'dev-workflow'
        shutil.copyfile(product, self.product)
        self.product.chmod(0o755)
        self.product_sha = sha256(self.product)
        self.tally = Tally()
        self.git_log = base / 'git.log'
        self.setup()

    # ---------------------------------------------------------------- 配置
    def setup(self):
        base = self.base
        self.dist = base / 'dist'
        scripts = self.dist / 'skills/ship-task/scripts'
        scripts.mkdir(parents=True)
        shutil.copyfile(GUARD, scripts / 'environment-guard.py')
        shutil.copyfile(LOOP_STATE, scripts / 'loop-state.py')
        (self.dist / '.claude-plugin').mkdir()
        (self.dist / '.claude-plugin/plugin.json').write_text('{"name":"dev-workflow"}')
        (self.dist / 'README.md').write_text('hello\n')
        probe = self.dist / 'skills/probe'
        probe.mkdir(parents=True)
        (probe / 'run.py').write_text('import sys\nprint("ran", sys.argv[1:])\nraise SystemExit(int(sys.argv[1]) if sys.argv[1:] else 0)\n')
        loop_permission = scripts / 'loop-permission.py'
        loop_permission.write_text('print("hook-ran")\n')
        self.home = base / 'home'
        (self.home / '.claude').mkdir(parents=True)
        (self.home / '.claude/settings.json').write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)", "Read"]}}))
        (self.home / '.gitconfig').write_text('[include]\n\tpath = extra.cfg\n')
        (self.home / 'extra.cfg').write_text('[user]\n\tname = a\n')
        bindir = base / 'bin'
        bindir.mkdir()
        real_git = shutil.which('git') or '/usr/bin/git'
        (bindir / 'git').write_text(
            '#!/bin/sh\n'
            f'if [ "${{LC_CTYPE+x}}" = x ]; then printf "set:%s\\n" "$LC_CTYPE"; else printf "unset\\n"; fi >> {self.git_log}\n'
            f'exec {real_git} "$@"\n')
        (bindir / 'git').chmod(0o755)
        self.path = f'{bindir}:{os.environ.get("PATH", "/usr/bin:/bin")}'
        self.out = base / 'out'
        self.out.mkdir()
        self.work = base / 'work'
        self.work.mkdir()
        self.repo = base / 'repo'
        self.repo.mkdir()
        quiet = ['-c', 'maintenance.auto=false', '-c', 'gc.auto=0', '-c', 'init.defaultBranch=main']
        git_env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.home), 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_AUTHOR_NAME': 'd2', 'GIT_AUTHOR_EMAIL': 'd2@example.invalid', 'GIT_COMMITTER_NAME': 'd2',
                   'GIT_COMMITTER_EMAIL': 'd2@example.invalid', 'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z',
                   'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z'}
        (self.repo / 'tracked.txt').write_text('tracked\n')
        for argv in (['init', '-q'], ['add', 'tracked.txt'], ['commit', '-qm', 'baseline']):
            subprocess.run([real_git, *quiet, '-C', str(self.repo), *argv], env=git_env, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 読み込み器を経た形の旧 guard のコピーと控え(`loop.sh` の開始時の bootstrap の受領と同じ物)。
        receipt = self.boot('start')
        self.state, self.state_sha = receipt['state'], receipt['sha256']
        self.guard_copy, self.guard_sha = Path(receipt['guard']), receipt['guard_sha256']
        if self.guard_sha != sha256(GUARD):
            raise Mismatch('開始の控えの guard が配布物の guard と違う')
        self.saved_state = self.work / 'saved.json'
        old = self.run_old('loop-state', ['snapshot', *self.snapshot_args(self.saved_state)])
        if old.returncode != 0 or not self.saved_state.is_file():
            raise Mismatch('loop-state の基準の状態を作れない: ' + describe(old))

    def boot(self, name):
        result = launch([PYTHON, '-B', str(self.dist / 'skills/ship-task/scripts/environment-guard.py'), 'bootstrap',
                         '--root', str(self.dist), '--output', str(self.out / name)], env=self.env(), cwd='/')
        if result.returncode != 0:
            raise Mismatch('旧版の bootstrap が失敗した: ' + describe(result))
        return json.loads(result.stdout)

    def snapshot_args(self, out):
        git = self.repo / '.git'
        return ['--out', str(out), '--top', str(self.repo), '--common', str(git), '--repo-admin', str(git), '--wt-admin', '-']

    def env(self, **extra):
        env = {'PATH': self.path, 'HOME': str(self.home), 'GIT_CONFIG_NOSYSTEM': '1', 'LANG': 'C.UTF-8'}
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    # ---------------------------------------------------------------- 起動の列
    def old_argv(self, form, args):
        if form == 'direct':
            return [PYTHON, '-B', str(self.dist / 'skills/ship-task/scripts/environment-guard.py'), *args], '/'
        if form == 'held':
            return [PYTHON, '-I', '-c', self.helper, 'environment-run', self.guard_sha, str(self.guard_copy), *args], '/'
        return [PYTHON, str(self.dist / 'skills/ship-task/scripts/loop-state.py'), *args], str(self.work)

    def new_argv(self, form, args, env, signals='0000', product=None, product_sha=None, guard=None, prelude='', python=PYTHON):
        has = 'LC_CTYPE' in env or b'LC_CTYPE' in env
        value = env.get('LC_CTYPE', env.get(b'LC_CTYPE', '')) if has else ''
        held = guard or ((self.guard_sha, str(self.guard_copy)) if form == 'held' else ('-', '-'))
        cwd = str(self.work) if form == 'loop-state' else '/'
        return [python, '-I', '-c', prelude + self.loader, KINDS[form], '1' if has else '0', value, signals,
                product_sha or self.product_sha, str(product or self.product), *held, *args], cwd

    def direct_argv(self, form, args):
        """読み込み器を通さずに製品を直接起動する列(読み込み器が結果を変えないことの確かめ)。"""
        cwd = str(self.work) if form == 'loop-state' else '/'
        return [str(self.product), KINDS[form], *args], cwd

    @staticmethod
    def signals(options):
        ignored = set(options.get('ignore', ()))
        return ''.join('1' if number in ignored else '0'
                       for number in (signal.SIGALRM, signal.SIGINT, signal.SIGPIPE, signal.SIGXFSZ))

    def run(self, argv_cwd, env, output=None, **options):
        argv, cwd = argv_cwd
        if self.git_log.exists():
            self.git_log.unlink()
        if output is not None and os.path.lexists(output):
            remove(output)
        result = launch(argv, env=env, cwd=cwd, **options)
        result.git = tuple(self.git_log.read_text().splitlines()) if self.git_log.exists() else ()
        result.tree = tree(output) if output is not None else None
        if output is not None and os.path.lexists(output):
            remove(output)
        return result

    def run_old(self, form, args, env=None, output=None, **options):
        return self.run(self.old_argv(form, args), env or self.env(), output, **options)

    def run_new(self, form, args, env=None, output=None, **options):
        env = env or self.env()
        return self.run(self.new_argv(form, args, env, self.signals(options)), env, output, **options)

    # ---------------------------------------------------------------- 比べ方
    def same(self, group, label, old, new, normalize=None):
        if old.timed_out or new.timed_out:
            raise Mismatch(f'{group}/{label}: 時間切れ')
        left, right = old, new
        if normalize is not None:
            left = dataclasses.replace(old, stderr=normalize(old.stderr))
            right = dataclasses.replace(new, stderr=normalize(new.stderr))
        if (left.key(), left.git, left.tree) != (right.key(), right.git, right.tree):
            raise Mismatch(f'{group}/{label}: 旧新が一致しない\n旧: {describe(old)}\n新: {describe(new)}')
        self.tally.add(group)

    def fixed(self, group, label, result, code, stdout, stderr, normalize=None):
        got = normalize(result.stderr) if normalize else result.stderr
        if result.timed_out or (result.returncode, result.stdout, got) != (code, stdout, stderr):
            raise Mismatch(f'{group}/{label}: 個別の期待と違う: {describe(result)}。期待: {code} {stdout!r} {stderr!r}')

    def pair(self, group, label, form, args, env=None, output=None, normalize=None, **options):
        old = self.run_old(form, args, env, output, **options)
        new = self.run_new(form, args, env, output, **options)
        self.same(group, label, old, new, normalize)
        return old, new


def remove(path):
    """消す(深い木も消せるよう、再帰しない `rm -rf` を使う。読めないフォルダは先に書けるようにする)。"""
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        subprocess.run(['chmod', '-R', 'u+rwx', '--', str(path)], check=True)
        subprocess.run(['rm', '-rf', '--', str(path)], check=True)
    else:
        path.unlink()


# ---------------------------------------------------------------- 項目

def basic(d):
    """通常の 9 コマンド(直接の形・読み込み器を経た形)と loop-state の入力。"""
    group = 'basic'
    state = ['--state', d.state, '--expect-sha256', d.state_sha]
    bad_state = ['--state', d.state, '--expect-sha256', '0' * 64]
    for form in ('direct', 'held'):
        cases = [
            ('host-paths', ['host-paths'], {}),
            ('host-paths-error', ['host-paths'], {'HOME': 'relative'}),
            ('verify', ['verify', *state], {}),
            ('verify-bad-sha', ['verify', *bad_state], {}),
            ('read', ['read', *state, '--path', 'README.md'], {}),
            ('read-outside', ['read', *state, '--path', '../x'], {}),
            ('exec', ['exec', *state, '--path', 'skills/probe/run.py', 'python3'], {}),
            ('exec-child-code', ['exec', *state, '--path', 'skills/probe/run.py', 'python3', '3'], {}),
            ('host-settings', ['host-settings', *state], {}),
            ('host-allowlist', ['host-allowlist', *state, '--allowed-tools', 'Bash(ls:*)', '--allowed-tools', 'Write'], {}),
            ('hook-wrong-path', ['hook', *state, '--path', 'README.md'], {}),
            ('hook-missing-arguments', ['hook'], {}),
            ('help', ['--help'], {}),
            ('usage-error', ['verify'], {}),
            ('unknown-command', ['bogus'], {}),
            ('no-arguments', [], {}),
            ('bootstrap-inside-root', ['bootstrap', '--root', str(d.dist), '--output', str(d.dist / 'x')], {}),
        ]
        for label, args, extra in cases:
            d.pair(group, f'{form}/{label}', form, args, d.env(**extra))
        output = d.out / f'basic-{form}'
        d.pair(group, f'{form}/bootstrap', form, ['bootstrap', '--root', str(d.dist), '--output', str(output)], output=output)
        for word, code in ((b'go\n', 0), (b'stop\n', 20)):
            old = verify_pair(d, d.old_argv(form, pair_args(d)), word)
            new = verify_pair(d, d.new_argv(form, pair_args(d), d.env()), word)
            d.same(group, f'{form}/verify-pair-{word.strip().decode()}', old, new)
            if old.returncode != code:
                raise Mismatch(f'{group}/{form}/verify-pair: 旧版の終了コードが {code} でない: {describe(old)}')
    # U3: 読み込み器を経た形の hook。旧版は判定器を実行し、Rust は確認の後に止め、読み込み器の変換で拒否の JSON と終了 0。
    hook = ['hook', *state, '--path', 'skills/ship-task/scripts/loop-permission.py']
    old, new = d.run_old('held', hook), d.run_new('held', hook)
    d.fixed(group, 'held/hook-u3-old', old, 0, b'hook-ran\n', b'')
    d.fixed(group, 'held/hook-u3-new', new, 0, HOOK_DENY, U3_LINE)
    d.tally.add(group, 'approved')
    # 読み込み器を経た形で、旧 guard のコピーが保持値と違う: 旧版の読み込み器も新しい読み込み器も、同じ 1 行と終了 20。
    old = d.run(([PYTHON, '-I', '-c', d.helper, 'environment-run', '0' * 64, str(d.guard_copy), 'verify', *state], '/'), d.env())
    new = d.run(d.new_argv('held', ['verify', *state], d.env(), guard=('0' * 64, str(d.guard_copy))), d.env())
    d.same(group, 'held/guard-copy-mismatch', old, new)
    d.fixed(group, 'held/guard-copy-mismatch-line', new, 20, b'', LOADER_LINE)
    # loop-state(直接の形。`loop.sh` の 8 か所)。
    second = d.work / 'second.json'
    shutil.copyfile(d.saved_state, second)
    output = d.work / 'snapshot.json'
    cases = [
        ('help', ['--help']),
        ('usage-error', ['state-digest']),
        ('unknown-command', ['bogus']),
        ('state-digest', ['state-digest', '--state', str(d.saved_state)]),
        ('state-digest-missing', ['state-digest', '--state', str(d.work / 'missing.json')]),
        ('state-digests', ['state-digests', '--state', str(d.saved_state), '--state', str(d.work / 'missing.json'), '--state', str(second)]),
        ('compare-same', ['compare', '--before', str(d.saved_state), '--after', str(second)]),
        ('stop-control', ['stop-control', '--state', str(d.saved_state)]),
        ('bad-limit', ['state-digest', '--state', str(d.saved_state), '--max-items', '0']),
    ]
    for label, args in cases:
        d.pair(group, f'loop-state/{label}', 'loop-state', args)
    d.pair(group, 'loop-state/snapshot', 'loop-state', ['snapshot', *d.snapshot_args(output)], output=output)


def pair_args(d):
    inventory = d.work / 'inventory.json'
    inventory.write_text('[]')
    return ['verify-pair', '--state', d.state, '--expect-sha256', d.state_sha, '--current-inventory', str(inventory),
            '--guard-sha256', d.guard_sha, '--go-timeout', '20']


def verify_pair(d, argv_cwd, word, *, interrupt=False, ignore=()):
    """`verified` を待ってから `word` を送る(`interrupt` なら `SIGINT` を送ってから `word`)。"""
    argv, cwd = argv_cwd

    def child():
        for number in ignore:
            signal.signal(number, signal.SIG_IGN)
    if d.git_log.exists():
        d.git_log.unlink()
    with subprocess.Popen(argv, env=d.env(), cwd=cwd, start_new_session=True, preexec_fn=child, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        line = process.stdout.readline()
        if interrupt:
            time.sleep(0.2)
            process.send_signal(signal.SIGINT)
            time.sleep(0.2)
        try:
            process.stdin.write(word)
            process.stdin.flush()
        except BrokenPipeError:
            pass
        try:
            out, err = process.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            out, err = process.communicate()
            return Result(process.returncode, line + out, err, True)
    git = tuple(d.git_log.read_text().splitlines()) if d.git_log.exists() else ()
    return Result(process.returncode, line + out, err, False, git)


def startup(d):
    """起動の失敗(設定の段階・標準の fd がディレクトリ)。読み込み器を経た形の旧版は `-I` なので `PYTHON*` を読まない。
    旧版が狙った失敗に当たったこと(Fatal の文と終了コード 1。読み込み器を経た形の設定の段階は成功)も確かめる。
    設定の段階の失敗を書く間は `SIGXFSZ` も受け継いだ扱い(旧版は設定の後に無視にする)。上限 0 の通常ファイルの標準エラーで比べる。"""
    group = 'startup'
    cases = [('hashseed', {'PYTHONHASHSEED': 'bad'}, {}, HASH_FATAL), ('intmaxstrdigits', {'PYTHONINTMAXSTRDIGITS': '1'}, {}, DIGITS_FATAL),
             ('stdin-directory', {}, {'directory': (0,)}, STDIN_FATAL), ('stdout-directory', {}, {'directory': (1,)}, STDOUT_FATAL),
             ('stderr-directory', {}, {'directory': (2,)}, b'')]
    for form in FORMS:
        args = ['state-digest', '--state', str(d.saved_state)] if form == 'loop-state' else ['host-paths']
        for label, extra, options, fatal in cases:
            old, _ = d.pair(group, f'{form}/{label}', form, args, d.env(**extra), normalize=thread_normalized, **options)
            if form == 'held' and extra:
                expected_code, expected = 0, b''
            else:
                expected_code, expected = 1, fatal
            if old.returncode != expected_code or thread_normalized(old.stderr) != expected:
                raise Mismatch(f'{group}/{form}/{label}: 旧版が狙った失敗に当たらない: {describe(old)}')
    # SIGXFSZ: RLIMIT_FSIZE のソフトの上限 0・標準エラーが通常のファイル・不正な PYTHONHASHSEED。旧版は信号 25 で終わる。
    for form in ('direct', 'loop-state'):
        args = ['state-digest', '--state', str(d.saved_state)] if form == 'loop-state' else ['host-paths']
        stderr = d.base / 'stderr-file'
        old, _ = d.pair(group, f'{form}/hashseed-sigxfsz', form, args, d.env(PYTHONHASHSEED='bad'),
                        fsize=(0, resource.RLIM_INFINITY), stderr_file=stderr)
        if old.returncode != -signal.SIGXFSZ:
            raise Mismatch(f'{group}/{form}/hashseed-sigxfsz: 旧版が SIGXFSZ で終わらない: {describe(old)}')


def fsize(d):
    """書けるファイルの大きさの上限(Q5)。ソフトの上限だけが小さいときは旧版と同じ所で止まる。ハードの上限が実行ファイルより
    小さいときは、新しい読み込み器が起動の前に止まる(個別の期待)。"""
    group = 'fsize'
    size = d.product.stat().st_size
    endless = resource.RLIM_INFINITY
    limits = [((1, 1), True), ((20000, 20000), True), ((1, endless), False), ((20000, endless), False),
              ((endless, endless), False), ((size * 2, size * 2), False)]
    for form in FORMS:
        for (soft, hard), early in limits:
            name = f'{form}/{soft if soft != endless else "inf"}-{hard if hard != endless else "inf"}'
            output = d.out / 'fsize' if form != 'loop-state' else d.work / 'fsize.json'
            if form == 'loop-state':
                args = ['snapshot', *d.snapshot_args(output)]
            else:
                args = ['bootstrap', '--root', str(d.dist), '--output', str(output)]
            if not early:
                d.pair(group, name, form, args, output=output, fsize=(soft, hard))
                continue
            old = d.run_old(form, args, output=output, fsize=(soft, hard))
            new = d.run_new(form, args, output=output, fsize=(soft, hard))
            # 旧版の固定期待: 書く物が上限より大きければ書込みの失敗で 20。loop-state の 20000 B は、控えが小さいので通る。
            want = {('loop-state', 20000): (0, True)}.get((form, soft), (20, soft == 20000))
            if old.timed_out or (old.returncode, old.tree is not None) != want:
                raise Mismatch(f'{group}/{name}: 旧版の終了コードと出力先の有無が固定期待 {want} と違う: {describe(old)}')
            line = NATIVE_LINE if form == 'loop-state' else LOADER_LINE
            d.fixed(group, name + '-new', new, 20, b'', line)
            if new.tree is not None:
                raise Mismatch(f'{group}/{name}: 新しい読み込み器が起動の前に止まらず、出力先を作った')
            d.tally.add(group, 'approved')


def fds(d):
    """起動時に閉じた標準の fd(U7)。読み込み器を通さない製品の起動と同じ結果であること(読み込み器が変えない)と、旧版との比較。
    旧版と違う場面は、旧版は `None` の標準エラーの代わりに標準出力へ書き、Rust は開き直した /dev/null へ書く(個別の期待)。"""
    group = 'fds'
    for form in FORMS:
        good = ['state-digest', '--state', str(d.saved_state)] if form == 'loop-state' else ['host-paths']
        bad = ['state-digest', '--state', str(d.work / 'missing.json')] if form == 'loop-state' else ['host-paths']
        bad_env = d.env() if form == 'loop-state' else d.env(HOME='relative')
        for label, args, env, close in (('stdin', good, d.env(), (0,)), ('stdout', good, d.env(), (1,)),
                                        ('all', good, d.env(), (0, 1, 2)), ('error-stdout', bad, bad_env, (1,)),
                                        ('error-stderr', bad, bad_env, (2,))):
            old = d.run_old(form, args, env, close=close)
            new = d.run_new(form, args, env, close=close)
            direct = d.run(d.direct_argv(form, args), env, close=close)
            if new.key() != direct.key() or new.timed_out or direct.timed_out:
                raise Mismatch(f'{group}/{form}/{label}: 読み込み器を経た起動が、製品の直接の起動と違う\n新: {describe(new)}\n直: {describe(direct)}')
            if old.key() == new.key():
                d.same(group, f'{form}/{label}', old, new)
                continue
            if label != 'error-stderr':
                raise Mismatch(f'{group}/{form}/{label}: 旧新が一致しない\n旧: {describe(old)}\n新: {describe(new)}')
            normal = d.run_old(form, args, env)
            d.fixed(group, f'{form}/{label}-old', old, normal.returncode, normal.stdout + normal.stderr, b'')
            d.fixed(group, f'{form}/{label}-new', new, normal.returncode, normal.stdout, b'')
            d.tally.add(group, 'approved')


SIGPIPE_LOCALES = (('lang-c-warn', {'LANG': 'C', 'PYTHONCOERCECLOCALE': 'warn'}),
                   ('lc-all-c-warn', {'LANG': None, 'LC_ALL': 'C', 'PYTHONCOERCECLOCALE': 'warn'}))
SIGPIPE_FAILURES = (('none', {}, {}), ('hashseed', {'PYTHONHASHSEED': 'bad'}, {}), ('stdin-directory', {}, {'directory': (0,)}))


def sigpipe_locale(d):
    """警告が出る代表の 2 組 × 起動の失敗(無し・`PYTHONHASHSEED`・標準入力がディレクトリ)× 親の `SIGPIPE`(既定・無視)×
    標準エラー(ふつう・読む側の閉じたパイプ)。直接の形は bootstrap も流す(既定のときに出力先を作らずに終わること)。"""
    group = 'sigpipe-locale'
    for form in FORMS:
        commands = [('host-paths', ['host-paths'], None)] if form != 'loop-state' else [
            ('state-digest', ['state-digest', '--state', str(d.saved_state)], None)]
        if form == 'direct':
            output = d.out / 'sigpipe'
            commands.append(('bootstrap', ['bootstrap', '--root', str(d.dist), '--output', str(output)], output))
        for locale_name, locale in SIGPIPE_LOCALES:
            for failure, extra, options in SIGPIPE_FAILURES:
                for ignored in (False, True):
                    for closed in (False, True):
                        for command, args, output in commands:
                            if command == 'bootstrap' and failure != 'none':
                                continue
                            label = f'{form}/{command}/{locale_name}/{failure}/{"ignore" if ignored else "default"}/{"closed" if closed else "normal"}'
                            env = d.env(**locale, **extra)
                            run_options = dict(options, stderr_closed=closed, ignore=(signal.SIGPIPE,) if ignored else ())
                            old = d.run_old(form, args, env, output, **run_options)
                            new = d.run_new(form, args, env, output, **run_options)
                            check_sigpipe_old(group, label, form, command, locale_name, failure, ignored, closed, old)
                            q4 = form != 'held' and locale_name == 'lang-c-warn' and failure == 'stdin-directory'
                            if q4 and thread_normalized(old.stderr) != thread_normalized(new.stderr) or q4 and old.returncode != new.returncode:
                                q4_expect(d, group, label, old, new, closed, ignored)
                                continue
                            d.same(group, label, old, new, thread_normalized)


def check_sigpipe_old(group, label, form, command, locale_name, failure, ignored, closed, old):
    """旧版が狙った経路(警告・起動の失敗・SIGPIPE)に当たったことの固定期待。読み込み器を経た形の旧版は `-I` なので、警告と
    `PYTHONHASHSEED` を読まない。"""
    if form == 'held':
        code, stderr = (1, STDIN_FATAL) if failure == 'stdin-directory' else (0, b'')
        if closed:
            stderr = b''
    else:
        warning = COERCION_WARNING if locale_name == 'lang-c-warn' else LEGACY_WARNING
        fatal = {'none': b'', 'hashseed': HASH_FATAL, 'stdin-directory': STDIN_FATAL}[failure]
        # 1 つ目の警告は起動の最初(SIGPIPE を無視にする前)、2 つ目は起動の検査の後(起動の失敗があれば出ない)。
        first = locale_name == 'lang-c-warn'
        code = 0 if failure == 'none' else 1
        stderr = (warning if first or failure == 'none' else b'') + fatal
        if closed:
            stderr = b''
            if first or failure == 'hashseed':
                code = code if ignored else -signal.SIGPIPE
            elif failure == 'none':
                code = 120
    if old.timed_out or old.returncode != code or thread_normalized(old.stderr) != stderr:
        raise Mismatch(f'{group}/{label}: 旧版が狙った経路に当たらない: {describe(old)}。期待: {code} {stderr!r}')
    if command == 'bootstrap' and closed and code == -signal.SIGPIPE and old.tree is not None:
        raise Mismatch(f'{group}/{label}: 旧版が信号で終わったのに出力先を作った')


def q4_expect(d, group, label, old, new, closed, ignored):
    """Q4 の A: 1 つ目の警告が出る `warn` と標準入力がディレクトリが重なる。旧版は警告と Fatal、新しい読み込み器(`-I`)は Fatal だけ。"""
    if closed:
        d.fixed(group, label + '-old', old, 1 if ignored else -signal.SIGPIPE, b'', b'')
        d.fixed(group, label + '-new', new, 1, b'', b'')
    else:
        d.fixed(group, label + '-old', old, 1, b'', COERCION_WARNING + STDIN_FATAL, thread_normalized)
        d.fixed(group, label + '-new', new, 1, b'', STDIN_FATAL, thread_normalized)
    d.tally.add(group, 'approved')


def locale_settings():
    """地域設定の比較の組(`LC_ALL` が無し・空なら `LC_CTYPE` × `LANG`、空でなければ 1 組)× `PYTHONCOERCECLOCALE`。"""
    rows = []
    for lc_all in (None, ''):
        for lc_ctype in (None, 'C', 'POSIX', 'C.UTF-8'):
            for lang in (None, 'C', 'POSIX'):
                rows.append({'LC_ALL': lc_all, 'LC_CTYPE': lc_ctype, 'LANG': lang})
    for lc_all in ('C', 'POSIX', 'C.UTF-8'):
        rows.append({'LC_ALL': lc_all, 'LC_CTYPE': None, 'LANG': None})
    return [(row, coerce) for row in rows for coerce in (None, '0', '1', 'warn')]


def locale(d):
    """子へ渡す `LC_CTYPE`(git の子が受けた値)・警告・終了コード。直接の形は bootstrap、読み込み器を経た形は verify、
    loop-state は snapshot(どれも git の子を起動する)。"""
    group = 'locale'
    state = ['--state', d.state, '--expect-sha256', d.state_sha]
    seen = {'coerced': 0, 'warnings': 0}
    for form in FORMS:
        for row, coerce in locale_settings():
            env = d.env(**row, PYTHONCOERCECLOCALE=coerce)
            label = f'{form}/{json.dumps(row, sort_keys=True)}/{coerce}'
            if form == 'direct':
                output = d.out / 'locale'
                old, new = d.pair(group, label, form, ['bootstrap', '--root', str(d.dist), '--output', str(output)], env, output)
            elif form == 'held':
                old, new = d.pair(group, label, form, ['verify', *state], env)
            else:
                output = d.work / 'locale.json'
                old, new = d.pair(group, label, form, ['snapshot', *d.snapshot_args(output)], env, output)
            if not old.git:
                raise Mismatch(f'{group}/{label}: git の子を起動しない入力になった')
            seen['coerced'] += any(line == 'set:C.UTF-8' for line in old.git) and 'LC_CTYPE' not in env
            seen['warnings'] += b'Python detected LC_CTYPE=C' in old.stderr or b'Python runtime initialized' in old.stderr
    # 観察が効いていること(書き換えた組と警告の組が 1 件以上ある)。
    if not seen['coerced'] or not seen['warnings']:
        raise Mismatch(f'{group}: 書き換えか警告を観察した組が無い: {seen}')
    d.tally.notes.append(f'{group}: git の子が書き換えた LC_CTYPE を受けた組 {seen["coerced"]}・警告が出た組 {seen["warnings"]}')


def q4(d):
    """Q4: 失敗が 1 つだけなら旧版と同じ診断。重なり(`PYTHONHASHSEED`・`PYTHONINTMAXSTRDIGITS`・1 つ目の警告が出る `warn` と、
    標準入力がディレクトリ)は、直接の形と loop-state で個別の期待(新しい読み込み器が先に標準入力の Fatal で止まる)。"""
    group = 'q4'
    singles = [('hashseed', {'PYTHONHASHSEED': 'bad'}, {}), ('intmaxstrdigits', {'PYTHONINTMAXSTRDIGITS': '1'}, {}),
               ('stdin-directory', {}, {'directory': (0,)}), ('warn', {'LANG': 'C', 'PYTHONCOERCECLOCALE': 'warn'}, {})]
    overlaps = [('hashseed+stdin', {'PYTHONHASHSEED': 'bad'},
                 b'Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be "random" or an integer in range [0; 4294967295]\n'
                 b'Python runtime state: preinitialized\n\n'),
                ('intmaxstrdigits+stdin', {'PYTHONINTMAXSTRDIGITS': '1'},
                 b'Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 '
                 b'for unlimited.\nPython runtime state: preinitialized\n\n'),
                ('warn+stdin', {'LANG': 'C', 'PYTHONCOERCECLOCALE': 'warn'}, COERCION_WARNING + STDIN_FATAL)]
    # 標準出力がディレクトリの重なり(設定の段階の失敗と 1 つ目の警告は、旧版では標準出力の検査より先に出る)。
    overlaps += [('hashseed+stdout', {'PYTHONHASHSEED': 'bad'}, HASH_FATAL),
                 ('intmaxstrdigits+stdout', {'PYTHONINTMAXSTRDIGITS': '1'}, DIGITS_FATAL),
                 ('warn+stdout', {'LANG': 'C', 'PYTHONCOERCECLOCALE': 'warn'}, COERCION_WARNING + STDOUT_FATAL)]
    single_old = {'hashseed': (1, HASH_FATAL), 'intmaxstrdigits': (1, DIGITS_FATAL), 'stdin-directory': (1, STDIN_FATAL),
                  'warn': (0, COERCION_WARNING)}
    for form in FORMS:
        args = ['state-digest', '--state', str(d.saved_state)] if form == 'loop-state' else ['host-paths']
        for label, extra, options in singles:
            old, _ = d.pair(group, f'{form}/{label}', form, args, d.env(**extra), normalize=thread_normalized, **options)
            # 旧版が狙った失敗・警告に当たったこと(読み込み器を経た形の旧版は `-I` なので、標準入力の Fatal だけ)。
            code, stderr = single_old[label] if form != 'held' or label == 'stdin-directory' else (0, b'')
            if old.returncode != code or thread_normalized(old.stderr) != stderr:
                raise Mismatch(f'{group}/{form}/{label}: 旧版が狙った失敗・警告に当たらない: {describe(old)}')
        for label, extra, old_stderr in overlaps:
            env = d.env(**extra)
            fd = 1 if label.endswith('+stdout') else 0
            old = d.run_old(form, args, env, directory=(fd,))
            new = d.run_new(form, args, env, directory=(fd,))
            if form == 'held':
                d.same(group, f'{form}/{label}', old, new, thread_normalized)
                continue
            d.fixed(group, f'{form}/{label}-old', old, 1, b'', old_stderr, thread_normalized)
            d.fixed(group, f'{form}/{label}-new', new, 1, b'', STDOUT_FATAL if fd == 1 else STDIN_FATAL, thread_normalized)
            d.tally.add(group, 'approved')


def make_deep(root, count):
    """深いフォルダを、親の fd をたどって 1 段ずつ作る(パスの長さの上限に当たらない)。"""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(count):
            try:
                os.mkdir('a', 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open('a', os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def depth(d):
    """再帰の深さの境目。読み込み器を経た形の旧版は 5 段深い(直接の形が通る 990 段の利用者フォルダは 985 段まで通る)。
    境目の前後で、bootstrap・verify・host-settings・host-allowlist の結果が旧新で同じこと。"""
    group = 'depth'
    boundaries = {}
    for form, counts in (('held', (984, 985, 986, 987)), ('direct', (990, 991, 992))):
        for count in counts:
            deep = d.base / f'deep-{form}-{count}'
            deep.mkdir()
            make_deep(deep, count)
            output = d.out / f'deep-{form}-{count}'
            boot = ['bootstrap', '--root', str(d.dist), '--output', str(output), '--settings-dir', str(deep)]
            old, _ = d.pair(group, f'{form}/{count}/bootstrap', form, boot, output=output)
            codes = [old.returncode]
            if old.returncode == 0:
                # 控えを旧版で作り直してから、照合の 3 コマンドを旧新で流す(読取りだけ)。
                receipt = json.loads(d.run_old(form, boot).stdout)
                new_state = ['--state', receipt['state'], '--expect-sha256', receipt['sha256']]
                for command in ('verify', 'host-settings', 'host-allowlist'):
                    codes.append(d.pair(group, f'{form}/{count}/{command}', form, [command, *new_state])[0].returncode)
                remove(output)
            boundaries[f'{form}/{count}'] = codes
    # 照合のコピーの木(読み込み器を経た形だけ。987 段は RecursionError にならずに照合が失敗し、988 段は RecursionError で失敗する)。
    # 直接の形の境目は、この比較では流していない(未確認。S2E の比較が 992・993 段を比べる)。
    for form, counts in (('held', (987, 988)),):
        for count in counts:
            output = d.out / f'copy-{form}-{count}'
            receipt = json.loads(d.run_old(form, ['bootstrap', '--root', str(d.dist), '--output', str(output)]).stdout)
            make_deep(output / 'plugin', count)
            result, _ = d.pair(group, f'{form}/copy-{count}/verify', form, ['verify', '--state', receipt['state'], '--expect-sha256', receipt['sha256']])
            boundaries[f'{form}/copy-{count}'] = [result.returncode, result.stderr.decode(errors='replace').strip()]
            remove(output)
    # 旧版の境目(読み込み器を経た形は直接の形より 5 段浅い所で落ちる)。旧新が同じでも、境目が動いたら止める。
    codes = {key: value[0] if key.split('/')[1].startswith('copy') else value for key, value in boundaries.items()}
    want = {'held/984': [0, 0, 0, 0], 'held/985': [0, 0, 0, 0], 'held/986': [0, 0, 0, 20], 'held/987': [20],
            'direct/990': [0, 0, 0, 0], 'direct/991': [0, 0, 0, 20], 'direct/992': [20], 'held/copy-987': 20, 'held/copy-988': 20}
    if codes != want or 'RecursionError' not in boundaries['held/copy-988'][1] or 'RecursionError' in boundaries['held/copy-987'][1]:
        raise Mismatch(f'{group}: 再帰の深さの境目が決めた所と違う: {boundaries}')
    d.tally.notes.append(f'{group}: 終了コード(bootstrap・verify・host-settings・host-allowlist){boundaries}')


def sigint(d):
    """`KeyboardInterrupt`(verify-pair の `go` の待ち)。信号で終わり、それまでの標準出力と最後の行が旧版と同じ(U4)。
    起動時に `SIGINT` が無視されていれば、送っても止まらない。"""
    group = 'sigint'
    for form in ('direct', 'held'):
        old = verify_pair(d, d.old_argv(form, pair_args(d)), b'go\n', interrupt=True)
        new = verify_pair(d, d.new_argv(form, pair_args(d), d.env()), b'go\n', interrupt=True)
        if old.returncode != -signal.SIGINT or not old.stderr.endswith(b'KeyboardInterrupt\n'):
            raise Mismatch(f'{group}/{form}: 旧版が KeyboardInterrupt で終わらない: {describe(old)}')
        d.same(group, f'{form}/interrupt', dataclasses.replace(old, stderr=strip_traceback_middle(old.stderr)), new)
        signals = d.signals({'ignore': (signal.SIGINT,)})
        old = verify_pair(d, d.old_argv(form, pair_args(d)), b'go\n', interrupt=True, ignore=(signal.SIGINT,))
        new = verify_pair(d, d.new_argv(form, pair_args(d), d.env(), signals), b'go\n', interrupt=True, ignore=(signal.SIGINT,))
        d.same(group, f'{form}/interrupt-ignored', old, new)


def loader(d):
    """読み込み器の性質(製品の代わりに `cat` の実体を照らして起動し、受け継いだ状態を読む)と、照らす失敗の止まり方。"""
    group = 'loader'
    cat = d.base / 'probe/cat'
    cat.parent.mkdir()
    shutil.copyfile(shutil.which('cat') or '/bin/cat', cat)
    cat.chmod(0o755)
    cat_sha = sha256(cat)
    (d.work / 'loop-state').write_bytes(b'')   # `cat loop-state <ファイル>` の 1 つ目(空)

    def through_cat(args, env, **options):
        argv, cwd = d.new_argv('loop-state', args, env, d.signals(options), product=cat, product_sha=cat_sha)
        return d.run((argv, cwd), env, **options)
    # 受け継ぐ信号(SIGALRM・SIGINT・SIGPIPE・SIGXFSZ)を、渡した 4 桁のとおりに戻す。
    numbers = (signal.SIGALRM, signal.SIGINT, signal.SIGPIPE, signal.SIGXFSZ)
    for ignored in ((), (signal.SIGPIPE,), (signal.SIGINT, signal.SIGXFSZ), numbers):
        result = through_cat(['/proc/self/status'], d.env(), ignore=ignored)
        mask = int(re.search(rb'^SigIgn:\s*([0-9a-f]+)$', result.stdout, re.M).group(1), 16)
        blocked = int(re.search(rb'^SigBlk:\s*([0-9a-f]+)$', result.stdout, re.M).group(1), 16)
        got = {number for number in numbers if mask >> (number - 1) & 1}
        if result.returncode != 0 or got != set(ignored) or blocked:
            raise Mismatch(f'{group}/signals {ignored}: 戻した信号が違う: {sorted(got)} blocked={blocked:x} {describe(result)}')
        d.tally.add(group, 'fixed')
    # 元の LC_CTYPE の有無と値を戻す(読み込み器の Python が足した C.UTF-8 を残さない)。ほかの変数と並びは変えない。
    for label, value in (('unset', None), ('c', b'C'), ('empty', b''), ('newline', b'a\nb'), ('equals', b'x=y'),
                         ('not-utf8', b'\xff\xfe'), ('utf8', b'C.UTF-8')):
        env = {b'PATH': os.fsencode(d.path), b'FIRST': b'1', b'LANG': b'C', b'HOME': os.fsencode(d.home), b'LAST': b'2'}
        if value is not None:
            env = {b'PATH': env[b'PATH'], b'LC_CTYPE': value, **{k: v for k, v in env.items() if k != b'PATH'}}
        argv, cwd = d.new_argv('loop-state', ['/proc/self/environ'], {}, '0000', product=cat, product_sha=cat_sha)
        argv[5], argv[6] = ('1', value) if value is not None else ('0', '')
        result = d.run((argv, cwd), env)
        got = [item for item in result.stdout.split(b'\0') if item]
        want = [k + b'=' + v for k, v in env.items()]
        if result.returncode != 0 or got != want:
            raise Mismatch(f'{group}/lc-ctype-{label}: 子の環境が元と違う: {got!r} 期待 {want!r}')
        d.tally.add(group, 'fixed')
    # RLIMIT_FSIZE: 書く間だけ上げた上限を、起動の前に元へ戻す。
    for soft in (1, 20000):
        result = through_cat(['/proc/self/limits'], d.env(), fsize=(soft, resource.RLIM_INFINITY))
        row = re.search(rb'^Max file size\s+(\S+)\s+(\S+)', result.stdout, re.M)
        if result.returncode != 0 or row is None or row.groups() != (str(soft).encode(), b'unlimited'):
            raise Mismatch(f'{group}/fsize-{soft}: 起動した側の上限が元の値でない: {describe(result)}')
        d.tally.add(group, 'fixed')
    # 照らす失敗: 起動せず、決めた 1 行と終了 20。
    broken = d.base / 'probe/broken'
    data = bytearray(d.product.read_bytes())
    data[len(data) // 2] ^= 1
    broken.write_bytes(bytes(data))
    broken.chmod(0o755)
    link = d.base / 'probe/link'
    os.symlink(d.product, link)
    large = d.base / 'probe/large'
    with open(large, 'wb') as handle:
        handle.truncate(8 * 1024 * 1024 + 1)
    large.chmod(0o755)
    failures = [('hash-mismatch', broken, d.product_sha), ('symlink', link, d.product_sha), ('too-large', large, sha256(large)),
                ('missing', d.base / 'probe/missing', d.product_sha), ('relative', Path('bundle/linux/dev-workflow'), d.product_sha),
                ('bad-sha', d.product, 'x' * 64)]
    for label, product, product_sha in failures:
        for form in ('direct', 'loop-state'):
            argv, cwd = d.new_argv(form, ['--help'], d.env(), product=product, product_sha=product_sha)
            result = d.run((argv, cwd), d.env())
            d.fixed(group, f'{form}/{label}', result, 20, b'', NATIVE_LINE if form == 'loop-state' else LOADER_LINE)
            d.tally.add(group, 'fixed')
    # memfd・封の名前を持たない Python(python-build-standalone など)と同じ形にした読み込み器: Linux の番号と ctypes の
    # memfd_create で、旧版と同じ結果になる。os.execve が fd を受けない形と、memfd を作る手段が無い形は、起動せずに 1 行と終了 20。
    for label, prelude in STANDALONE_PRELUDES.items():
        env = d.env()
        new = d.run(d.new_argv('direct', ['host-paths'], env, prelude=prelude), env)
        if STANDALONE_EXPECT[label] == 'same':
            d.same(group, f'standalone-{label}', d.run_old('direct', ['host-paths'], env), new)
        else:
            d.fixed(group, f'standalone-{label}', new, 20, b'', LOADER_LINE)
            d.tally.add(group, 'fixed')
    result = d.run(d.new_argv('loop-state', ['--help'], d.env(), prelude=NO_MEMFD_PRELUDE), d.env())
    d.fixed(group, 'standalone-no-memfd', result, 20, b'', NATIVE_LINE)
    d.tally.add(group, 'fixed')


def selftest(d):
    """比較器の自己検査: 違う結果を一致としない(別の入力・1 バイト違う製品・個別の期待の外れ)。"""
    group = 'selftest'
    checks = [
        lambda: d.same(group, 'different-input', d.run_old('direct', ['host-paths']), d.run_new('direct', ['host-paths'], d.env(HOME='/elsewhere'))),
        lambda: d.same(group, 'different-git', dataclasses.replace(d.run_old('held', ['verify', '--state', d.state, '--expect-sha256', d.state_sha]), git=('set:x',)),
                       d.run_new('held', ['verify', '--state', d.state, '--expect-sha256', d.state_sha])),
        lambda: d.fixed(group, 'wrong-fixed', d.run_new('direct', ['--help']), 0, b'', b''),
    ]
    for check in checks:
        try:
            check()
        except Mismatch:
            d.tally.add(group, 'selftest')
            continue
        raise Mismatch(f'{group}: 違う結果を一致とした')


def standalone(d, python):
    """`--standalone-python` の Python(例: uv が入れる python-build-standalone の 3.10.11。memfd_create と封の名前が無い)で
    新しい読み込み器を動かし、旧版(比較器の Python)と同じ結果になること。3 つの形で 1 件ずつ。"""
    group = 'standalone'
    state = ['--state', d.state, '--expect-sha256', d.state_sha]
    for form, args in (('direct', ['host-paths']), ('held', ['verify', *state]), ('loop-state', ['state-digest', '--state', str(d.saved_state)])):
        env = d.env()
        old = d.run_old(form, args, env)
        new = d.run(d.new_argv(form, args, env, python=python), env)
        d.same(group, f'{form}', old, new)


GROUPS = (('basic', basic), ('startup', startup), ('fsize', fsize), ('fds', fds), ('sigpipe-locale', sigpipe_locale),
          ('locale', locale), ('q4', q4), ('depth', depth), ('sigint', sigint), ('loader', loader), ('selftest', selftest))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--only', action='append', choices=[name for name, _ in GROUPS],
                        help='手元で一部の群だけを流す(件数の照合をその群に限る。CI の合格には使わない)')
    parser.add_argument('--standalone-python', type=Path,
                        help='memfd_create と封の名前を持たない Python(例: uv の python-build-standalone)。渡すと、その Python で読み込み器を動かす群を足す')
    args = parser.parse_args(argv)
    if not sys.platform.startswith('linux'):
        raise ValueError('D2 の比較は Linux だけで流す(新しい読み込み器は memfd を使う)')
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise ValueError('D2 の比較は Python 3.10 系・非 root で実行してください')
    product = product_path()
    before = sha256(product)
    started = time.monotonic()
    folder = Path(os.path.realpath(tempfile.mkdtemp(prefix='d2-')))
    try:
        d = D2(folder, product)
        seconds = {}
        for name, function in GROUPS:
            if args.only and name not in args.only:
                continue
            begin = time.monotonic()
            function(d)
            seconds[name] = time.monotonic() - begin
        if args.standalone_python is not None:
            begin = time.monotonic()
            standalone(d, str(args.standalone_python))
            seconds['standalone'] = time.monotonic() - begin
        tally = d.tally
    finally:
        remove(folder)
    expected = {name: count for name, count in EXPECTED.items() if not args.only or name in args.only}
    if args.standalone_python is not None:
        expected['standalone'] = EXPECTED_STANDALONE
    if tally.groups != expected:
        raise Mismatch(f'群ごとの件数が決めた数と違う: {tally.groups} 期待 {expected}')
    if sha256(product) != before:
        raise Mismatch('比較の途中で製品の実行ファイルが変わった')
    for name, count in tally.groups.items():
        print(f'D2:   {name}: {count} 項目・{seconds[name]:.1f} 秒')
    for text in tally.notes:
        print(f'D2:   記録: {text}')
    total = sum(tally.groups.values())
    if sum(tally.kinds.values()) != total:
        raise Mismatch(f'項目の種類の和が総数と違う: {tally.kinds} 総数 {total}')
    print(f'D2: {total} 項目(旧新の一致 {tally.kinds["matched"]}・旧新それぞれの個別の期待 {tally.kinds["approved"]}'
          f'・旧版と比べない固定の確かめ {tally.kinds["fixed"]}・比較器の自己検査 {tally.kinds["selftest"]})'
          f'・製品 {product}(SHA-256 {before}・{product.stat().st_size} B)・Python {sys.version.split()[0]}'
          f'・所要時間 {time.monotonic() - started:.1f} 秒')
    if args.only:
        print('D2: 一部の群だけを流した(--only)。合格ではない')
        return 3
    print('D2: 合格')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AssertionError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'D2: 不合格: {error}', file=sys.stderr)
        sys.exit(1)
