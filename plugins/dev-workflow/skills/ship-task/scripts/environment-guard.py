#!/usr/bin/env python3
"""無人入口で配布物と利用者環境を控え、保持値から使用直前に照合する。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_SECONDS = 15.0

# Configuration parsing is a Git call too.  Keep the same process-level
# contract as the unattended loop and publication helpers: Git must not load a
# hook, fsmonitor, signer, LFS filter, pager, replacement object or lazy fetch
# helper while it is only interpreting a supplied configuration stream.
SAFE_GIT_PREFIX = (
    "--no-pager", "--no-replace-objects",
    "-c", "core.quotePath=false",
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.ignoreCase=false",
    "-c", "core.splitIndex=false",
    "-c", "core.ignoreStat=false",
    "-c", "commit.gpgSign=false",
    "-c", "push.gpgSign=false",
    "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.clean=",
    "-c", "filter.lfs.process=",
    "-c", "filter.lfs.required=false",
)


class Stop(RuntimeError):
    pass


class Budget:
    def __init__(self):
        self.bytes = 0
        self.entries = 0
        self.deadline = time.monotonic() + MAX_SECONDS

    def tick(self, count=0):
        self.entries += count
        if self.entries > MAX_ENTRIES or time.monotonic() > self.deadline:
            raise Stop('環境の項目数または検査時間が上限を超える')

    def reserve(self, size):
        self.tick()
        if size > MAX_FILE or self.bytes + size > MAX_TOTAL:
            raise Stop('環境の読取バイト上限を超える')
        self.bytes += size


def identity(st):
    return st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def directory_identity(st):
    return st.st_dev, st.st_ino, st.st_mode


def absolute(path):
    # resolve() により symlink を信頼済みのパスへ変換しない。
    value = Path(os.path.expanduser(str(path)))
    return value if value.is_absolute() else Path.cwd() / value


@contextmanager
def parent(path):
    path = absolute(path)
    fds = [os.open('/', os.O_RDONLY | os.O_DIRECTORY)]
    chain = []
    try:
        for part in path.parts[1:-1]:
            old = fds[-1]
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=old)
            chain.append((old, part, directory_identity(os.fstat(fd))))
            fds.append(fd)
        yield fds[-1], path.name
        for fd, name, held in chain:
            if directory_identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != held:
                raise Stop('検査中に親ディレクトリが差し替えられた')
    finally:
        for fd in reversed(fds):
            os.close(fd)


def read_at(fd, name, budget, expected_identity=None):
    before = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if expected_identity is not None and identity(before) != expected_identity:
        raise Stop('本文読取前に監視対象が変わった')
    if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o444:
        raise Stop('監視対象が読める通常ファイルでない')
    budget.reserve(before.st_size)
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        if identity(before) != identity(os.fstat(opened)):
            raise Stop('open 時に監視対象が変わった')
        chunks, left = [], before.st_size
        while left:
            budget.tick()
            chunk = os.read(opened, min(left, 65536))
            if not chunk:
                raise Stop('読取中に短くなった')
            left -= len(chunk)
            chunks.append(chunk)
        if os.read(opened, 1):
            raise Stop('読取中に大きくなった')
        if identity(before) != identity(os.fstat(opened)) or identity(before) != identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
            raise Stop('読取中に監視対象が変わった')
        return b''.join(chunks), stat.S_IMODE(before.st_mode)
    finally:
        os.close(opened)


def read_regular(path, budget=None):
    with parent(path) as (fd, name):
        return read_at(fd, name, budget or Budget())[0]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def dump(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n').encode()


def write_new(path, raw, mode=0o600):
    with parent(path) as (fd, name):
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
        try:
            left = memoryview(raw)
            while left:
                left = left[os.write(out, left):]
            os.fchmod(out, mode)
            os.fsync(out)
        finally:
            os.close(out)


def walk_at(fd, prefix, budget, entries, blobs, links_at=None, expected=None):
    before = os.fstat(fd)
    entries[prefix] = ['directory', stat.S_IMODE(before.st_mode)]
    names = []
    with os.scandir(fd) as scan:
        for item in scan:
            budget.tick(1)
            names.append(item.name)
    for name in sorted(names):
        budget.tick()
        key = prefix + '/' + name
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if directory_identity(st) != directory_identity(os.fstat(child)):
                    raise Stop('走査前にディレクトリが変わった')
                walk_at(child, key, budget, entries, blobs)
                if directory_identity(st) != directory_identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                    raise Stop('走査中にディレクトリが変わった')
            finally:
                os.close(child)
        elif stat.S_ISLNK(st.st_mode) and links_at is not None:
            # 正式な user skills/commands 直下の導入リンクだけを固定する。
            # 再照合では対象本文に触れる前に字面と対象実体を比較する。
            text = os.readlink(name, dir_fd=fd)
            target = Path(text) if Path(text).is_absolute() else Path(links_at) / text
            link_key = '@link:' + key
            held = ['installation-link', text, st.st_dev, st.st_ino]
            if expected is not None and expected.get(link_key, [])[:4] != held:
                raise Stop('導入リンクが開始時から変わった')
            with parent(target) as (target_fd, target_name):
                opened = os.open(target_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target_fd)
                try:
                    target_st = os.fstat(opened)
                    held += list(directory_identity(target_st))
                    if expected is not None and expected.get(link_key) != held:
                        raise Stop('導入リンクの対象が開始時から変わった')
                    entries[link_key] = held
                    walk_at(opened, key, budget, entries, None)
                    if directory_identity(target_st) != directory_identity(os.stat(target_name, dir_fd=target_fd, follow_symlinks=False)):
                        raise Stop('導入リンクの対象が走査中に変わった')
                finally:
                    os.close(opened)
            if identity(st) != identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                raise Stop('導入リンクが走査中に変わった')
        elif stat.S_ISREG(st.st_mode):
            raw, mode = read_at(fd, name, budget)
            entries[key] = ['file', mode, sha(raw)]
            if blobs is not None:
                blobs[key] = raw
        else:
            raise Stop('監視対象に symlink または特殊ファイルがある')
    after = os.fstat(fd)
    if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
        raise Stop('走査中にディレクトリの集合が変わった')


def tree(path, key, budget, entries, blobs=None, optional=False, user_links=False, expected=None):
    try:
        with parent(path) as (fd, name):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if directory_identity(st) != directory_identity(os.fstat(child)):
                    raise Stop('ディレクトリが差し替えられた')
                walk_at(child, key, budget, entries, blobs, path if user_links else None, expected)
                if directory_identity(st) != directory_identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                    raise Stop('走査中にルートが差し替えられた')
            finally:
                os.close(child)
    except FileNotFoundError:
        if not optional:
            raise
        entries[key] = ['missing']


def record(path, key, budget, entries, expected=None, depth=0, required=False, expected_identity=None):
    budget.tick(1)
    if depth > 40:
        raise Stop('利用者設定のリンクが循環しているか上限を超える')
    # 保持していないパスを設定の参照から新しく信頼しない。
    if expected is not None and key not in expected:
        raise Stop('保持していない利用者設定の参照先が現れた')
    opened = False
    try:
        with parent(path) as (fd, name):
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            opened = True
            if expected is not None and expected[key] == ['missing']:
                raise Stop('不在として保持した利用者設定が新設された')
            if expected_identity is not None and identity(before) != expected_identity:
                raise Stop('本文読取前にリンク対象が変わった')
            if stat.S_ISLNK(before.st_mode):
                text = os.readlink(name, dir_fd=fd)
                target = Path(text) if Path(text).is_absolute() else absolute(path).parent / text
                link_key = '@file-link:' + str(depth) + ':' + key
                held = ['configuration-link', text, before.st_dev, before.st_ino]
                if expected is not None and expected.get(link_key, [])[:4] != held:
                    raise Stop('利用者設定のリンクが開始時から変わった')
                with parent(target) as (target_fd, target_name):
                    target_st = os.stat(target_name, dir_fd=target_fd, follow_symlinks=False)
                held += list(directory_identity(target_st))
                if expected is not None and expected.get(link_key) != held:
                    raise Stop('利用者設定のリンク対象が開始時から変わった')
                entries[link_key] = held
                raw = record(target, key, budget, entries, expected, depth + 1, True, identity(target_st))
                with parent(target) as (target_fd, target_name):
                    if directory_identity(target_st) != directory_identity(os.stat(target_name, dir_fd=target_fd, follow_symlinks=False)):
                        raise Stop('利用者設定のリンク対象が読取中に変わった')
                if identity(before) != identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                    raise Stop('利用者設定のリンクが読取中に変わった')
                return raw
            raw, mode = read_at(fd, name, budget, identity(before))
        entries[key] = ['file', mode, sha(raw)]
        return raw
    except FileNotFoundError:
        if opened or required:
            raise
        entries[key] = ['missing']
        return None


def git_config_output(argv, raw, env, budget):
    # stdin と stdout を並行して送受信し、展開後の出力も取得中に制限する。
    # stderr は値や巨大な診断を保持せず捨てる。失敗は固定の診断に変換する。
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env, cwd='/')
    poll = selectors.DefaultSelector()
    output = bytearray()
    pending = memoryview(raw)
    deadline = time.monotonic() + 3
    try:
        for stream in (proc.stdin, proc.stdout):
            os.set_blocking(stream.fileno(), False)
        poll.register(proc.stdout, selectors.EVENT_READ)
        if pending:
            poll.register(proc.stdin, selectors.EVENT_WRITE)
        else:
            proc.stdin.close()
        while poll.get_map():
            budget.tick()
            left = deadline - time.monotonic()
            if left <= 0:
                raise Stop('Git 設定の解析が時間上限を超える')
            for event, _ in poll.select(min(left, .1)):
                stream = event.fileobj
                if stream is proc.stdin:
                    try:
                        size = os.write(stream.fileno(), pending[:65536])
                        pending = pending[size:]
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        poll.unregister(stream); stream.close()
                else:
                    chunk = os.read(stream.fileno(), min(65536, MAX_FILE - len(output) + 1))
                    if not chunk:
                        poll.unregister(stream); stream.close()
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_FILE:
                            raise Stop('Git 設定の解析出力がバイト上限を超える')
        try:
            code = proc.wait(timeout=max(.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise Stop('Git 設定の解析が時間上限を超える') from None
        if code:
            raise Stop('Git 設定を解析できない')
        return bytes(output)
    finally:
        poll.close()
        for stream in (proc.stdin, proc.stdout):
            stream.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def includes(path, raw, budget=None):
    budget = budget or Budget()
    # Git 自身に引用・継続行を解析させる。include 展開は止め、各宛先を
    # nofollow で先に検査する。config の解析は外部 command を実行しない。
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
    for key in list(env):
        if key.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')) or key in ('GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'):
            env.pop(key, None)
    prefix = ['git', *SAFE_GIT_PREFIX, 'config', '--file', '-', '--no-includes', '--null']
    def parse(*args):
        budget.tick()
        return git_config_output([*prefix, *args], raw, env, budget)
    records = parse('--list').split(b'\0')
    if len(records) > MAX_ENTRIES:
        raise Stop('Git 設定の項目数が上限を超える')
    result, queried = [], set()
    for item in records:
        budget.tick()
        if not item:
            continue
        key, sep, value = item.partition(b'\n')
        if key == b'include.path' or (key.startswith(b'includeif.') and key.endswith(b'.path')):
            if key in queried:
                continue
            queried.add(key)
            # Git の path 型で ~ と %(prefix) も解決する。.. は OS が開く順を保ち、
            # symlink/../x を字面で潰して別のファイルを検査しない。
            paths = parse('--type=path', '--get-all', os.fsdecode(key)).split(b'\0')
            for value in paths[:-1]:
                if not value:
                    raise Stop('include.path が空')
                child = Path(os.fsdecode(value))
                result.append(str(absolute(child if child.is_absolute() else path.parent / child)))

    return result


def default_specs():
    home = absolute(os.environ['HOME'])
    xdg = absolute(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
    config = absolute(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude')
    files = [config / 'settings.json', config / 'plugins/installed_plugins.json', config / 'plugins/known_marketplaces.json',
             Path('/etc/claude-code/managed-settings.json')]
    shells = [home / x for x in ('.profile', '.bash_profile', '.bash_login', '.bashrc', '.bash_aliases', '.bash_logout', '.zshenv', '.zprofile', '.zshrc', '.zlogin', '.zlogout')]
    shells += [Path('/etc/profile'), Path('/etc/bash.bashrc'), Path('/etc/zsh/zshenv')]
    for key in ('BASH_ENV', 'ENV'):
        if os.environ.get(key):
            shells.append(absolute(os.environ[key]))
    if os.environ.get('ZDOTDIR'):
        shells += [absolute(os.environ['ZDOTDIR']) / x for x in ('.zshenv', '.zprofile', '.zshrc', '.zlogin')]
    configs = [absolute(os.environ['GIT_CONFIG_GLOBAL'])] if os.environ.get('GIT_CONFIG_GLOBAL') else [home / '.gitconfig', xdg / 'git/config']
    if os.environ.get('GIT_CONFIG_NOSYSTEM') not in ('1', 'true', 'yes'):
        configs.append(absolute(os.environ.get('GIT_CONFIG_SYSTEM') or '/etc/gitconfig'))
    return {'files': list(map(str, files + shells)), 'configs': list(map(str, configs)),
            'directories': ['/etc/claude-code/managed-settings.d'],
            'user_directories': list(map(str, [config / 'skills', config / 'commands']))}


def plugin_inventory(raw):
    value = json.loads(raw)
    if not isinstance(value, list):
        raise Stop('有効 plugin 一覧が配列でない')
    active = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get('enabled', True), bool):
            raise Stop('plugin 一覧の項目が不正')
        if not item.get('enabled', True):
            continue
        install = item.get('installPath')
        if not isinstance(install, str) or not Path(install).is_absolute():
            raise Stop('有効 plugin の installPath を解決できない')
        active.append(item)
    return sorted(active, key=lambda x: json.dumps(x, sort_keys=True))


def snapshot(root, specs, inventory=None, blobs=None, expected=None):
    budget, entries = Budget(), {}
    root = absolute(root)
    # 全配布物を含める。scripts の sibling imports/source もこのコピーで閉じる。
    tree(root, 'plugin', budget, entries, blobs)
    for path in specs['files']:
        record(path, 'external:' + path, budget, entries, expected)
    for path in specs['directories']:
        tree(path, 'external:' + path, budget, entries, optional=True)
    for path in specs.get('user_directories', []):
        tree(path, 'external:' + path, budget, entries, optional=True, user_links=True, expected=expected)
    pending, seen = list(specs['configs']), set()
    while pending:
        budget.tick()
        path = str(absolute(pending.pop()))
        if path in seen:
            continue
        seen.add(path)
        if path == os.devnull:
            entries['external:' + path] = ['null-device']
            continue
        key = 'external:' + path
        raw = record(path, key, budget, entries, expected)
        # 新しい include 宛先を探索する前に、既知の設定本文との一致を要求する。
        # 最後の snapshot 全体比較だけでは、拒否する変更の参照先を先に読む。
        if expected is not None and entries[key] != expected[key]:
            raise Stop('保持済み Git 設定が変わったため参照先を読まず停止した')
        if raw is not None:
            pending.extend(includes(Path(path), raw, budget))
    for item in inventory or []:
        path = str(absolute(item['installPath']))
        if path != str(root):
            tree(path, 'installed:' + path, budget, entries)
    return entries


def load_verified(state, expected):
    if not re.fullmatch('[a-f0-9]{64}', expected or ''):
        raise Stop('外部に保持した SHA-256 が必要')
    raw = read_regular(state)
    if sha(raw) != expected:
        raise Stop('環境の控えが保持値と一致しない')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('version') != 3:
        raise Stop('環境の控えの形式が違う')
    return value


def verify(state, expected, current_inventory=None):
    value = load_verified(state, expected)
    if current_inventory is not None and plugin_inventory(read_regular(current_inventory)) != value['inventory']:
        raise Stop('有効 plugin の一覧が変わった')
    if snapshot(value['root'], value['specs'], value['inventory'], expected=value['entries']) != value['entries']:
        raise Stop('配布物または利用者環境が変わった')
    copied = {}
    tree(value['copy'], 'plugin', Budget(), copied)
    wanted = {k:v for k,v in value['entries'].items() if k == 'plugin' or k.startswith('plugin/')}
    if copied != wanted:
        raise Stop('信頼コピーが変わった')
    return value


def bootstrap(a):
    root, output = absolute(a.root), absolute(a.output)
    if '..' in root.parts or '..' in output.parts:
        raise Stop('配布元とコピー先は .. を含まないパスで指定する')
    if output == root or root in output.parents:
        raise Stop('コピー先が配布物の内側にある')
    specs = default_specs()
    specs['files'] += list(map(lambda x: str(absolute(x)), a.setting + a.shell))
    specs['configs'] += list(map(lambda x: str(absolute(x)), a.config))
    specs['user_directories'] += [str(absolute(x)) for x in a.skills_dir]
    specs['directories'] += [str(absolute(x)) for x in a.settings_dir]
    inventory = plugin_inventory(read_regular(a.inventory)) if a.inventory else []
    blobs = {}
    entries = snapshot(root, specs, inventory, blobs)
    # 既存の state/copy は開かず、新しいディレクトリだけ作る。
    with parent(output) as (fd, name):
        os.mkdir(name, 0o700, dir_fd=fd)
    for key, entry in entries.items():
        if not (key == 'plugin' or key.startswith('plugin/')):
            continue
        target = output / key
        if entry[0] == 'directory':
            with parent(target) as (fd, name):
                os.mkdir(name, entry[1], dir_fd=fd)
                os.chmod(name, entry[1], dir_fd=fd, follow_symlinks=False)
        else:
            write_new(target, blobs[key], entry[1])
    # 読み込んだ helper 自体のコピーも別途保持する(元から再 import しない)。
    guard = read_regular(Path(__file__))
    write_new(output / 'environment-guard.py', guard)
    value = {'version':3, 'root':str(root), 'copy':str(output / 'plugin'), 'specs':specs,
             'inventory':inventory, 'entries':entries}
    raw = dump(value)
    write_new(output / 'environment.json', raw)
    verify(output / 'environment.json', sha(raw))
    print(json.dumps({'state':str(output / 'environment.json'), 'sha256':sha(raw),
                      'guard':str(output / 'environment-guard.py'), 'guard_sha256':sha(guard), 'plugin':str(output / 'plugin')}))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='command', required=True)
    boot = sub.add_parser('bootstrap')
    boot.add_argument('--root', required=True)
    boot.add_argument('--output', required=True)
    boot.add_argument('--inventory')
    for flag in ('setting','config','shell','skills-dir','settings-dir'):
        boot.add_argument('--'+flag, action='append', default=[])
    for cmd in ('verify','exec','read','hook'):
        child = sub.add_parser(cmd)
        child.add_argument('--state', required=True)
        child.add_argument('--expect-sha256', required=True)
        child.add_argument('--current-inventory')
        if cmd != 'verify':
            child.add_argument('--path', required=True)
        if cmd == 'exec':
            child.add_argument('argv', nargs=argparse.REMAINDER)
    a = p.parse_args()
    try:
        if a.command == 'bootstrap':
            bootstrap(a)
        else:
            value = verify(a.state, a.expect_sha256, a.current_inventory)
            if a.command != 'verify':
                rel = Path(a.path)
                if rel.is_absolute() or '..' in rel.parts or not rel.parts:
                    raise Stop('コピー内の相対パスが必要')
                path = Path(value['copy']) / rel
                if a.command == 'hook':
                    if a.path != 'skills/ship-task/scripts/loop-permission.py':
                        raise Stop('hook は固定の権限判定器だけを実行する')
                    raw = read_regular(path)
                    if sha(raw) != value['entries']['plugin/' + a.path][2]:
                        raise Stop('権限判定器が保持値と違う')
                    sys.path.insert(0, str(path.parent))
                    sys.argv = [str(path)]
                    exec(compile(raw, str(path), 'exec'), {'__name__':'__main__', '__file__':str(path)})
                elif a.command == 'read':
                    sys.stdout.buffer.write(read_regular(path))
                else:
                    argv = a.argv[1:] if a.argv[:1] == ['--'] else a.argv
                    if not argv or argv[0] not in ('python3','bash'):
                        raise Stop('helper の実行形式は python3 または bash')
                    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
                    options = ['-B'] if argv[0] == 'python3' else []
                    return subprocess.run([argv[0], *options, str(path), *argv[1:]], env=env).returncode
        return 0
    except (OSError, Stop, ValueError, KeyError, TypeError, RecursionError, subprocess.TimeoutExpired) as exc:
        detail = str(exc) if isinstance(exc, Stop) else '検査を完了できない (' + type(exc).__name__ + ')'
        print('ERROR [environment-guard] ' + detail, file=sys.stderr)
        return 20


if __name__ == '__main__':
    raise SystemExit(main())
