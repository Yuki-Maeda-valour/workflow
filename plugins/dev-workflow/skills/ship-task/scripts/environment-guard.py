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
import unicodedata

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


def host_path(value, name):
    """ホストが起動時に使うパスだけの、cwd に依らない解決。"""
    if not isinstance(value, str) or not value or value.startswith(('~', '//')):
        raise Stop('host-config-path')
    if not os.path.isabs(value) or unicodedata.normalize('NFC', value) != value:
        raise Stop('host-config-path')
    if '..' in Path(value).parts:
        raise Stop('host-config-path')
    # /./・重複した /・末尾 / は同じ絶対位置として受け入れる。Path はここでだけ
    # 字面を正規化し、設定を読む他の経路へ相対値を渡さない。
    return str(Path(value))


def host_paths(environ=None):
    environ = os.environ if environ is None else environ
    home = host_path(environ.get('HOME'), 'HOME')
    if 'CLAUDE_CONFIG_DIR' in environ:
        config = host_path(environ['CLAUDE_CONFIG_DIR'], 'CLAUDE_CONFIG_DIR')
    else:
        config = str(Path(home) / '.claude')
    return {'home': home, 'config': config, 'user_settings': str(Path(config) / 'settings.json'),
            'cache_settings': str(Path(config) / 'remote-settings.json')}


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


COMPONENT_KINDS = frozenset(('personal-skill', 'enterprise-skill', 'personal-command', 'enterprise-command',
                             'personal-agent', 'enterprise-agent', 'marketplace-manifest', 'plugin'))


def _component_keep(key, value, entries, expected):
    if expected is not None and expected.get(key) != value:
        raise Stop('component の実体が開始時から変わった')
    entries[key] = value


@contextmanager
def _component_directory(start_fd, parts, key, budget, entries, expected, links=False, optional=False):
    """各要素を lstat→保持値比較→openat の順に解き、開いた親を最後まで保つ。"""
    fds, checks, seen = [os.dup(start_fd)], [], set()
    queue, index, link_count = list(parts), 0, 0
    missing = False
    try:
        _component_keep('@origin:' + key, ['directory-stat', *directory_identity(os.fstat(fds[0]))],
                        entries, expected)
        while queue:
            budget.tick(1)
            name = queue.pop(0)
            if name == '':
                continue
            fd = fds[-1]
            trace = '@route:' + key + ':' + str(index)
            index += 1
            try:
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not optional or link_count:
                    raise Stop('component の参照先が存在しない') from None
                _component_keep(trace, ['missing', name], entries, expected)
                checks.append((fd, name, None, None))
                missing = True
                break
            held = ['directory-step', name, *directory_identity(before)]
            text = None
            if stat.S_ISLNK(before.st_mode):
                if not links:
                    raise Stop('component の親に導入リンクがある')
                text = os.readlink(name, dir_fd=fd)
                held = ['link-step', name, text, *directory_identity(before)]
            _component_keep(trace, held, entries, expected)
            checks.append((fd, name, directory_identity(before), text))
            if text is not None:
                mark = (before.st_dev, before.st_ino)
                if mark in seen or link_count >= 40:
                    raise Stop('component 導入リンクが循環しているか上限を超える')
                seen.add(mark)
                link_count += 1
                # split preserves . and .. and their order across symlink expansion.
                queue = text.split('/') + queue
                if text.startswith('/'):
                    rootfd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    fds.append(rootfd)
                    _component_keep('@absolute:' + trace, ['directory-stat', *directory_identity(os.fstat(rootfd))],
                                    entries, expected)
                continue
            if not stat.S_ISDIR(before.st_mode):
                raise Stop('component の参照先が通常ディレクトリでない')
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            fds.append(child)
            if directory_identity(before) != directory_identity(os.fstat(child)):
                raise Stop('component の親が open 時に変わった')
        _component_keep('@route-end:' + key, ['missing' if missing else 'directory', index], entries, expected)
        yield None if missing else fds[-1]
        for fd, name, held, text in reversed(checks):
            try:
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if held is None:
                    continue
                raise Stop('component の親が検査中に消えた') from None
            if held is None or directory_identity(current) != held or \
                    (text is not None and os.readlink(name, dir_fd=fd) != text):
                raise Stop('component の親または導入リンクが検査中に変わった')
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _component_file(fd, name, key, budget, entries, expected, blobs):
    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    meta = ['file-stat', *identity(st)]
    _component_keep('@stat:' + key, meta, entries, expected)
    if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o444:
        raise Stop('component が読める通常ファイルでない')
    # 同じ物理ファイルは一度だけ読む。論理 alias ごとの記録/照合は省略しない。
    cache = getattr(budget, 'component_bytes', None)
    if cache is None:
        cache = budget.component_bytes = {}
    cachekey = identity(st)
    if cachekey in cache:
        raw, mode = cache[cachekey]
    else:
        raw, mode = read_at(fd, name, budget, identity(st))
        cache[cachekey] = (raw, mode)
    if identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != identity(st):
        raise Stop('component が読取中に変わった')
    _component_keep(key, ['file', mode, sha(raw)], entries, expected)
    if blobs is not None:
        blobs[key] = raw


def _component_walk(fd, key, budget, entries, expected, blobs, installation_links=False, depth=0):
    budget.tick(1)
    if depth > 100:
        raise Stop('component の深さが上限を超える')
    before = os.fstat(fd)
    _component_keep('@stat:' + key, ['directory-stat', *directory_identity(before)], entries, expected)
    _component_keep(key, ['directory', stat.S_IMODE(before.st_mode)], entries, expected)
    names = []
    with os.scandir(fd) as scan:
        for item in scan:
            budget.tick(1)
            names.append(item.name)
    names.sort()
    # 集合を本文より先に比較する。新設 entry の本文には一度も触れない。
    _component_keep('@names:' + key, names, entries, expected)
    for name in names:
        childkey = key + '/' + name
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            if not installation_links:
                raise Stop('component に許可されない導入リンクがある')
            text = os.readlink(name, dir_fd=fd)
            _component_keep('@link:' + childkey,
                            ['installation-link', text, *directory_identity(st)], entries, expected)
            with _component_directory(fd, [name], childkey, budget, entries, expected, links=True) as child:
                _component_walk(child, childkey, budget, entries, expected, blobs, depth=depth + 1)
            if directory_identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != directory_identity(st) or \
                    os.readlink(name, dir_fd=fd) != text:
                raise Stop('component の導入リンクが検査中に変わった')
        elif stat.S_ISDIR(st.st_mode):
            with _component_directory(fd, [name], childkey, budget, entries, expected) as child:
                _component_walk(child, childkey, budget, entries, expected, blobs, depth=depth + 1)
        elif stat.S_ISREG(st.st_mode):
            _component_file(fd, name, childkey, budget, entries, expected, blobs)
        else:
            raise Stop('component に特殊ファイルがある')
    if identity(before) != identity(os.fstat(fd)):
        raise Stop('component の集合が検査中に変わった')


def component_tree(path, kind, budget, entries, expected=None, blobs=None):
    if kind not in COMPONENT_KINDS:
        raise Stop('component の種別が不明')
    path = str(absolute(path))
    key = 'component:' + kind + ':' + path
    rootfd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # marketplace-manifest は一つの JSON だけを保持する。entry の他の
        # plugin を含む marketplace tree 全体へ探索を広げない。
        parts = path.split('/')[1:]
        directory_parts = parts[:-1] if kind == 'marketplace-manifest' else parts
        with _component_directory(rootfd, directory_parts, key, budget, entries, expected,
                                  optional=True) as fd:
            if fd is None:
                if kind == 'plugin':
                    raise Stop('component の plugin root が存在しない')
                _component_keep(key, ['missing'], entries, expected)
            elif kind == 'marketplace-manifest':
                parent_before = os.fstat(fd)
                try:
                    os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    _component_keep(key, ['missing'], entries, expected)
                    try:
                        os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise Stop('component の不在 entry が検査中に新設された')
                else:
                    # missing -> regular は stat 比較の時点で拒否する。
                    _component_file(fd, parts[-1], key, budget, entries, expected, blobs)
                if identity(parent_before) != identity(os.fstat(fd)):
                    raise Stop('component の集合が検査中に変わった')
            else:
                _component_walk(fd, key, budget, entries, expected, blobs,
                                installation_links=kind in ('personal-skill', 'enterprise-skill'))
    finally:
        os.close(rootfd)
    return key


def _component_descriptors(value):
    specs = value.get('specs', {})
    components = specs.get('components')
    if not isinstance(components, list) or not components:
        raise Stop('component の保持記述子が無い')
    seen = set()
    for item in components:
        if not isinstance(item, dict) or item.get('kind') not in COMPONENT_KINDS or \
                not isinstance(item.get('path'), str) or not os.path.isabs(item['path']):
            raise Stop('component の記述子が不正')
        pair = item['kind'], item['path']
        if pair in seen:
            raise Stop('component の記述子が重複している')
        seen.add(pair)
    return components


def component_policy_digest(state_path, expected_sha):
    """run ごとの配布コピー位置/stat を除き、出所・リンク字面・内容を結ぶ。"""
    value = load_verified(state_path, expected_sha)
    components = _component_descriptors(value)
    workflow = [item['path'] for item in components if item.get('source') == 'workflow']
    if len(workflow) != 1:
        raise Stop('配布物の component 記述子が一意でない')
    rootkey = 'component:plugin:' + workflow[0]
    root_routes = tuple('@route:component:' + item['kind'] + ':' + item['path'] + ':'
                        for item in components)

    def logical(key):
        for prefix in ('', '@names:', '@link:'):
            start = prefix + rootkey
            if key == start or key.startswith(start + '/'):
                return prefix + 'component:plugin:@workflow' + key[len(start):]
        return key

    kept = {}
    for key, entry in value['entries'].items():
        if key.startswith('component:') or key.startswith('@names:component:'):
            kept[logical(key)] = entry
        elif key.startswith('@link:component:'):
            kept[logical(key)] = entry[:2]
        elif key.startswith('@route:component:'):
            # 論理root自身の絶対パスはdescriptorにある。rootに至る親の不在位置は
            # 内容ではないので除く。導入linkのchild routeは引き続き全て保持する。
            if key.startswith(root_routes) or key.startswith('@route:' + rootkey + '/'):
                continue
            kept[key] = entry[:3] if entry[0] == 'link-step' else entry[:2]
    descriptors = [{**item, 'path': '@workflow'} if item.get('source') == 'workflow' else item
                   for item in components]
    # active installPath は実体の出所であり、runコピーの正規化対象にしない。
    active = [{key: item[key] for key in ('id', 'installPath', 'scope', 'version') if key in item}
              for item in value.get('inventory', [])]
    return sha(dump({'descriptors': descriptors, 'entries': kept, 'active_sources': active}))


def read_components(state_path, expected_sha):
    """保持 SHA と記述子を照合し、同じ walker の同一 fd 由来 bytes を返す。"""
    value = load_verified(state_path, expected_sha)
    components = _component_descriptors(value)
    entries, blobs, budget, result = {}, {}, Budget(), []
    for item in components:
        root, kind = item['path'], item['kind']
        rootkey = component_tree(root, kind, budget, entries, value['entries'], blobs)
        for key, raw in blobs.items():
            if key != rootkey and not key.startswith(rootkey + '/'):
                continue
            relative = key[len(rootkey) + 1:] if key != rootkey else Path(root).name
            result.append({'key': key, 'kind': kind, 'root': root, 'relative': relative,
                           'path': root if key == rootkey else root + '/' + relative, 'raw': raw,
                           **{k: item[k] for k in ('source', 'plugin', 'entry') if k in item}})
    wanted = {k: v for k, v in value['entries'].items()
              if k.startswith(('component:', '@stat:component:', '@names:component:',
                               '@link:component:', '@route:component:', '@route-end:component:',
                               '@origin:component:', '@absolute:@route:component:'))}
    if entries != wanted:
        raise Stop('component の保持集合が変わった')
    return result


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
    paths = host_paths()
    home = Path(paths['home'])
    xdg = absolute(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
    config = Path(paths['config'])
    files = [Path(paths['user_settings']), Path(paths['cache_settings']), config / 'plugins/installed_plugins.json', config / 'plugins/known_marketplaces.json',
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
    specs = {'files': list(map(str, files + shells)), 'configs': list(map(str, configs)),
            'directories': ['/etc/claude-code/managed-settings.d'],
            'user_directories': [],
            'host_paths': paths,
            'managed_roots': ['/etc/claude-code'],
            'components': [
              {'kind':'personal-skill','path':str(config / 'skills')}, {'kind':'personal-command','path':str(config / 'commands')},
              {'kind':'personal-agent','path':str(config / 'agents')}, {'kind':'enterprise-skill','path':'/etc/claude-code/.claude/skills'},
              {'kind':'enterprise-agent','path':'/etc/claude-code/.claude/agents'},
              {'kind':'enterprise-command','path':'/etc/claude-code/.claude/commands'}]}
    # drop-in の記述子は snapshot 後に、その保持済み tree だけから作る。
    # ここで directory を列挙すると列挙と snapshot の間に新設された設定を
    # 記述子から外してしまうため、bootstrap では下で entries を渡す。
    specs['host_settings'] = []
    return specs


FORBIDDEN_SETTINGS_ENV = frozenset((
    'CLAUDE_CONFIG_DIR', 'HOME', 'XDG_CONFIG_HOME', 'CLAUDE_CODE_SIMPLE',
    'CLAUDE_CODE_SAFE_MODE', 'CLAUDE_CODE_SHELL_PREFIX',
))


def _no_duplicate_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise Stop('host-settings')
        value[key] = item
    return value


def strict_settings(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicate_object,
                           parse_constant=lambda unused: (_ for _ in ()).throw(Stop('host-settings')))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise Stop('host-settings') from None
    if not isinstance(value, dict):
        raise Stop('host-settings')
    # JSON の decoder は object_pairs_hook を全階層へ適用する。深い object/array は
    # 解析後も明示的に上限を設け、次の利用者設定への DoS を持ち込まない。
    def walk(item, depth=0):
        if depth > 100:
            raise Stop('host-settings')
        if isinstance(item, dict):
            for child in item.values(): walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item: walk(child, depth + 1)
    walk(value)
    return value


def check_host_setting(value, managed=False):
    if 'env' in value:
        env = value['env']
        if not isinstance(env, dict):
            raise Stop('host-settings')
        for key, item in env.items():
            if key in FORBIDDEN_SETTINGS_ENV or key in (
                    'CLAUDE_CODE_REMOTE_SETTINGS_PATH', 'CLAUDE_CODE_MANAGED_SETTINGS_PATH',
                    'CLAUDE_CODE_MOCK_REMOTE_SETTINGS'):
                raise Stop('host-settings' if key in FORBIDDEN_SETTINGS_ENV else 'host-config-source')
            if not isinstance(item, str):
                raise Stop('host-settings')
    for key in ('policyHelper', 'policyHelpers'):
        if key in value:
            raise Stop('host-settings')
    if value.get('disableAllHooks') is True:
        raise Stop('hooks-disabled')
    if managed and value.get('allowManagedHooksOnly') is True:
        raise Stop('hooks-disabled')


def host_settings_specs(specs, entries):
    """保持済み entries からホストが読む設定の記述子を作る。"""
    paths = specs.get('host_paths')
    if not isinstance(paths, dict) or not all(isinstance(paths.get(k), str) for k in ('user_settings', 'cache_settings')):
        raise Stop('host-settings')
    roots = specs.get('managed_roots')
    if not isinstance(roots, list) or not all(isinstance(root, str) and os.path.isabs(root) for root in roots):
        raise Stop('host-settings')
    result = [
        {'path': paths['user_settings'], 'kind': 'user'},
        {'path': paths['cache_settings'], 'kind': 'cache'},
    ]
    for root in roots:
        result.append({'path': str(Path(root) / 'managed-settings.json'), 'kind': 'managed'})
        directory = str(Path(root) / 'managed-settings.d')
        prefix = 'external:' + directory + '/'
        names = []
        for key, held in entries.items():
            if not key.startswith(prefix) or held[:1] != ['file']:
                continue
            name = key[len(prefix):]
            # direct child だけを対象にする。拡張子の小文字性はホスト仕様どおり
            # suffix に限り、Alpha.json は対象、Alpha.JSON は対象外である。
            if '/' not in name and not name.startswith('.') and name.endswith('.json'):
                names.append(name)
        for name in sorted(names):
            result.append({'path': str(Path(directory) / name), 'kind': 'managed'})
    return result


def read_host_settings(state_path, expected_sha):
    """保持済みの設定だけを安全に読み、同じ process 内で object を返す。"""
    value = verify(state_path, expected_sha)
    desc = value.get('host_settings')
    if not isinstance(desc, list):
        raise Stop('host-settings')
    entries = value['entries']
    budget = Budget()
    def related(table, key):
        """この設定本文と、その最終 symlink の連鎖だけを取り出す。"""
        result = {key}
        for name in table:
            if name.startswith('@file-link:') and name.split(':', 2)[-1] == key:
                result.add(name)
        return result
    def load(item):
        if not isinstance(item, dict) or set(item) != {'path', 'kind'} or not isinstance(item['path'], str):
            raise Stop('host-settings')
        path, kind = item['path'], item['kind']
        if kind not in ('user', 'managed', 'cache'):
            raise Stop('host-settings')
        key = 'external:' + path
        if key not in entries:
            raise Stop('host-settings')
        # descriptor ごとに fresh な記録を作る。累積した前回の link 記録で、
        # 今回消えた最終 link を隠さない。
        local = {}
        raw = record(path, key, budget, local, entries)
        # verify() と設定本文の再読取の間にも、同じ held entry 以外を採用しない。
        # 通常ファイルの hash/mode、missing、最終 symlink とその target を含め、
        # 関連する external と全 @file-link の集合を双方向で照合する。
        # 同 bytes/mode の通常ファイルへの置換でも、消えた link entry を拒否する。
        held_keys, local_keys = related(entries, key), related(local, key)
        if held_keys != local_keys:
            raise Stop('host-settings')
        for local_key in local_keys:
            if entries.get(local_key) != local.get(local_key):
                raise Stop('host-settings')
        if raw is None:
            return kind, path, None
        budget.tick()
        item = strict_settings(raw)
        budget.tick()
        check_host_setting(item, kind != 'user')
        budget.tick()
        return kind, path, item
    user, cache, cache_seen, checked = {}, None, False, 0
    managed, permission_sources = [], []
    user_path = None
    for item in desc:
        kind, path, value = load(item)
        checked += 1
        # 元の設定値は同一 process 内だけで渡す。kind と論理 path の hash は
        # 別 run でも同じで、複数の管理 root/drop-in を取り違えない。
        source_kind = 'managed-drop-in' if kind == 'managed' and Path(path).parent.name == 'managed-settings.d' else kind
        permission_sources.append({'kind': source_kind, 'source': sha(dump([source_kind, path])),
                                   'present': value is not None, 'settings': value})
        if kind == 'user':
            if user_path is not None: raise Stop('host-settings')
            user_path, user = path, value or {}
        elif kind == 'cache':
            if cache_seen: raise Stop('host-settings')
            cache_seen = True
            cache = value
        elif value is not None:
            managed.append(value)
    if user_path is None:
        raise Stop('host-settings')
    return {'user_settings': user_path, 'user': user, 'cache': cache,
            'managed': managed, 'checked': checked, 'permission_sources': permission_sources}


def split_rules(value):
    rules, current, depth = [], '', 0
    for ch in value:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        if depth == 0 and (ch == ',' or ch.isspace()):
            if current: rules.append(current)
            current = ''
        else:
            current += ch
    if current: rules.append(current)
    return rules


def bash_rule(rule):
    if rule == 'Bash': return ('all', [])
    if not (rule.startswith('Bash(') and rule.endswith(')')): return 'skip'
    inner = rule[5:-1].strip()
    if inner in ('', '*'): return None
    if inner.endswith(':*') and '*' not in inner[:-2]:
        words = inner[:-2].split(); return ('prefix', words) if words else None
    if inner.endswith(' *') and '*' not in inner[:-2]:
        words = inner[:-2].split(); return ('prefix', words) if words else None
    return ('exact', inner.split()) if '*' not in inner else None


def host_allowlist(state_path, expected_sha, values):
    settings = read_host_settings(state_path, expected_sha)
    sources = [('--allowed-tools', rule) for value in values for rule in split_rules(value)]
    permissions = settings['user'].get('permissions')
    allow = permissions.get('allow') if isinstance(permissions, dict) else []
    if isinstance(allow, list):
        sources.extend(('利用者の設定', rule) for rule in allow if isinstance(rule, str))
    out, unused, seen = [], [], set()
    for origin, rule in sources:
        got = bash_rule(rule.strip())
        if got == 'skip': continue
        if got is None:
            unused.append(f'{rule}({origin})'); continue
        key = (got[0], tuple(got[1]))
        if key not in seen:
            seen.add(key); out.append({'kind': got[0], 'words': got[1]})
    return out, unused


def plugin_inventory(raw):
    value = json.loads(raw, object_pairs_hook=_no_duplicate_object,
                       parse_constant=lambda unused: (_ for _ in ()).throw(Stop('plugin 一覧の形式が不正')))
    if not isinstance(value, list):
        raise Stop('有効 plugin 一覧が配列でない')
    active, ids = [], set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get('enabled', True), bool):
            raise Stop('plugin 一覧の項目が不正')
        if not item.get('enabled', True):
            continue
        install = item.get('installPath')
        if not isinstance(install, str) or not Path(install).is_absolute():
            raise Stop('有効 plugin の installPath を解決できない')
        plugin_id = item.get('id')
        if not isinstance(plugin_id, str) or not plugin_id or plugin_id in ids:
            raise Stop('有効 plugin の ID が一意でない')
        ids.add(plugin_id)
        active.append(item)
    return sorted(active, key=lambda x: json.dumps(x, sort_keys=True))


def _component_inventory(specs, inventory, budget, entries, blobs):
    """保持済み registry から選択 entry だけを active installPath と結ぶ。"""
    def add(item):
        for old in specs['components']:
            if (old['kind'], old['path']) == (item['kind'], item['path']):
                if item.get('entry') != old.get('entry') or (old.get('plugin') and item.get('plugin')
                        and old['plugin'] != item['plugin']):
                    raise Stop('component の出所が曖昧')
                return
        specs['components'].append(item)

    config = specs.get('host_paths', {}).get('config')
    for item in inventory or []:
        path = str(absolute(item['installPath']))
        plugin = item.get('id')
        if not isinstance(plugin, str) or not plugin:
            raise Stop('component の plugin ID が不明')
        if plugin.endswith('@skills-dir'):
            name = plugin[:-len('@skills-dir')]
            if not name or '@' in name or item['installPath'] != path:
                raise Stop('component の skills-dir ID が不明')
            # ホストがskill-folder pluginへ付ける擬似出所。registryの例外を
            # IDだけでは認めず、先に保持したskillroot直下のmanifestへ結ぶ。
            candidates = [descriptor for descriptor in specs['components']
                          if descriptor['kind'] in ('personal-skill', 'enterprise-skill')
                          and str(Path(path).parent) == descriptor['path']]
            if len(candidates) != 1:
                raise Stop('component の skills-dir 出所が一意でない')
            descriptor = candidates[0]
            manifest_key = 'component:' + descriptor['kind'] + ':' + path + '/.claude-plugin/plugin.json'
            raw = blobs.get(manifest_key)
            if raw is None or strict_settings(raw).get('name') != name:
                raise Stop('component の skills-dir manifest が保持出所と一致しない')
            # personal/enterprise descriptorがrootSKILL・nested plugin双方を
            # 既に保持する。同じ実体をpluginとして追加して名前空間を二重にしない。
            continue
        add({'kind': 'plugin', 'path': path, 'plugin': plugin, 'source': item.get('scope', 'plugin')})
        if item.get('scope') == 'synced' or '@' not in plugin:
            continue
        name, marketplace = plugin.rsplit('@', 1)
        if not name or not marketplace or not config:
            raise Stop('component の marketplace 出所が不明')
        raw = blobs.get('external:' + str(Path(config) / 'plugins/known_marketplaces.json'))
        if raw is None:
            raise Stop('component の marketplace registry が無い')
        known = strict_settings(raw)
        source = known.get(marketplace)
        if not isinstance(source, dict) or not isinstance(source.get('installLocation'), str) or \
                not os.path.isabs(source['installLocation']):
            raise Stop('component の marketplace 出所が不明')
        location = str(absolute(source['installLocation']))
        manifest = str(Path(location) / '.claude-plugin/marketplace.json')
        key = component_tree(manifest, 'marketplace-manifest', budget, entries, blobs=blobs)
        if key not in blobs:
            raise Stop('component の marketplace manifest が無い')
        data = strict_settings(blobs[key])
        definitions = data.get('plugins')
        if not isinstance(definitions, list):
            raise Stop('component の marketplace entry が不明')
        matches = [entry for entry in definitions if isinstance(entry, dict) and entry.get('name') == name]
        if len(matches) != 1:
            raise Stop('component の marketplace entry が一意でない')
        entry = matches[0]
        # 参照の意味は host-check が検査する。ローカル source 自身も保持し、
        # active installPath と異なる source に置いた component を取りこぼさない。
        declared = entry.get('source')
        if not isinstance(declared, str) or not declared.startswith('./') or \
                any(part == '..' for part in declared.split('/')):
            raise Stop('component の marketplace source が root 内相対パスでない')
        source_path = str(Path(location) / declared)
        add({'kind': 'plugin', 'path': source_path, 'plugin': plugin, 'source': 'marketplace-source'})
        # 一つの manifest から複数の選択 entry を保持するときはまとめる。
        existing = next((old for old in specs['components']
                         if old['kind'] == 'marketplace-manifest' and old['path'] == manifest), None)
        selection = {'plugin': plugin, 'entry': entry}
        if existing is None:
            add({'kind': 'marketplace-manifest', 'path': manifest,
                 'source': location, 'entry': [selection]})
        elif selection not in existing['entry']:
            existing['entry'].append(selection)
        installed = blobs.get('external:' + str(Path(config) / 'plugins/installed_plugins.json'))
        if installed is None:
            raise Stop('component の installPath registry が無い')
        registry = strict_settings(installed).get('plugins')
        candidates = registry.get(plugin) if isinstance(registry, dict) else None
        if not isinstance(candidates, list):
            raise Stop('component の installPath registry が不明')
        matches = [candidate for candidate in candidates if isinstance(candidate, dict)
                   and candidate.get('installPath') == path
                   and (not item.get('scope') or candidate.get('scope') == item['scope'])]
        if len(matches) != 1:
            raise Stop('component の installPath 出所が一意でない')


def snapshot(root, specs, inventory=None, blobs=None, expected=None):
    budget, entries = Budget(), {}
    blobs = {} if blobs is None else blobs
    root = absolute(root)
    # 全配布物を含める。scripts の sibling imports/source もこのコピーで閉じる。
    if 'components' not in specs:
        tree(root, 'plugin', budget, entries, blobs)
    for path in specs['files']:
        raw = record(path, 'external:' + path, budget, entries, expected)
        if raw is not None:
            blobs['external:' + path] = raw
    for path in specs['directories']:
        tree(path, 'external:' + path, budget, entries, optional=True)
    for path in specs.get('user_directories', []):
        tree(path, 'external:' + path, budget, entries, optional=True, user_links=True, expected=expected)
    def hold_components(components):
        for component in components:
            if not isinstance(component, dict) or not isinstance(component.get('kind'), str) or not isinstance(component.get('path'), str):
                raise Stop('component の記述子が不正')
            component_tree(component['path'], component['kind'], budget, entries, expected=expected, blobs=blobs)
    # inventoryのskills-dirを、同じBudget/walkerで先に保持したbytesだけへ結ぶ。
    held_count = len(specs.get('components', []))
    hold_components(specs.get('components', []))
    if 'components' in specs and expected is None:
        _component_inventory(specs, inventory, budget, entries, blobs)
        hold_components(specs['components'][held_count:])
    if 'components' in specs:
        rootkey = 'component:plugin:' + str(root)
        if rootkey not in entries or entries[rootkey][0] != 'directory':
            raise Stop('配布物の component 記述子が無い')
        for key, entry in list(entries.items()):
            if key == rootkey or key.startswith(rootkey + '/'):
                legacy = 'plugin' + key[len(rootkey):]
                entries[legacy] = entry
                if key in blobs:
                    blobs[legacy] = blobs[key]
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
    # 古い state は従来どおり installed tree を照合する。
    if 'components' not in specs:
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
    specs['components'] += [{'kind': 'personal-skill', 'path': str(absolute(x))} for x in a.skills_dir]
    specs['components'].append({'kind': 'plugin', 'path': str(root), 'source': 'workflow'})
    specs['directories'] += [str(absolute(x)) for x in a.settings_dir]
    for managed_root in a.managed_dir:
        managed_root = host_path(managed_root, '--managed-dir')
        specs['managed_roots'].append(managed_root)
        specs['files'].append(str(Path(managed_root) / 'managed-settings.json'))
        specs['directories'].append(str(Path(managed_root) / 'managed-settings.d'))
        specs['components'] += [{'kind': 'enterprise-' + kind, 'path': str(Path(managed_root) / '.claude' / folder)}
                                for kind, folder in (('skill', 'skills'), ('agent', 'agents'), ('command', 'commands'))]
    specs['components'] = list({(item['kind'], item['path']): item for item in specs['components']}.values())
    inventory = plugin_inventory(read_regular(a.inventory)) if a.inventory else []
    blobs = {}
    entries = snapshot(root, specs, inventory, blobs)
    specs['host_settings'] = host_settings_specs(specs, entries)
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
             'host_settings':specs['host_settings'], 'inventory':inventory, 'entries':entries,
             'guard_sha256':sha(guard)}
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
    for flag in ('setting','config','shell','skills-dir','settings-dir', 'managed-dir'):
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
    child = sub.add_parser('host-settings')
    child.add_argument('--state', required=True)
    child.add_argument('--expect-sha256', required=True)
    child = sub.add_parser('host-allowlist')
    child.add_argument('--state', required=True)
    child.add_argument('--expect-sha256', required=True)
    child.add_argument('--allowed-tools', action='append', default=[])
    sub.add_parser('host-paths')
    a = p.parse_args()
    try:
        if a.command == 'bootstrap':
            bootstrap(a)
        elif a.command == 'host-paths':
            print(json.dumps(host_paths(), ensure_ascii=True, separators=(',', ':')))
        elif a.command == 'host-settings':
            result = read_host_settings(a.state, a.expect_sha256)
            print(json.dumps({'user_settings': result['user_settings'], 'checked': result['checked']},
                             ensure_ascii=True, separators=(',', ':')))
        elif a.command == 'host-allowlist':
            rules, unused = host_allowlist(a.state, a.expect_sha256, a.allowed_tools)
            print(json.dumps(rules, ensure_ascii=False))
            for item in unused:
                print('unused=' + re.sub(r'[\x00-\x1f\x7f]', ' ', item)[:300])
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
        if a.command in ('host-paths', 'host-settings', 'host-allowlist'):
            code = detail if detail in ('host-config-path', 'host-settings', 'host-config-source', 'hooks-disabled') else 'host-settings'
            print('ERROR [' + code + '] ホスト設定を安全に検査できない', file=sys.stderr)
        else:
            print('ERROR [environment-guard] ' + detail, file=sys.stderr)
        return 20


if __name__ == '__main__':
    raise SystemExit(main())
