#!/usr/bin/env python3
"""候補の確認と採用改名を一つの入口で行う。本文の設計は呼出側が行う。"""
import argparse
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
MAX_CANDIDATE = 8 * 1024 * 1024
SAFE_GIT_PREFIX = (
    '--no-pager', '--no-replace-objects',
    '-c', 'core.quotePath=false', '-c', 'core.fsmonitor=', '-c', 'core.hooksPath=/dev/null',
    '-c', 'core.ignoreCase=false', '-c', 'core.splitIndex=false', '-c', 'core.ignoreStat=false',
    '-c', 'commit.gpgSign=false', '-c', 'push.gpgSign=false', '-c', 'filter.lfs.smudge=',
    '-c', 'filter.lfs.clean=', '-c', 'filter.lfs.process=', '-c', 'filter.lfs.required=false',
)


def refuse_loop():
    if os.environ.get('DEV_WORKFLOW_LOOP_ITER'):
        raise ValueError('DEV_WORKFLOW_LOOP_ITER があるため候補を採用できない')


def directory(path):
    """絶対パスを component-by-component nofollow で開く。呼出側が close する。"""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).absolute().parts[1:]:
            if part in ('.', '..'):
                raise ValueError('親への参照を含むパスは使えない')
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def file_identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def read_candidate(fd, name):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CANDIDATE:
            raise ValueError('候補は 8 MiB 以下の通常ファイルだけを使う')
        data = bytearray()
        while len(data) <= before.st_size:
            chunk = os.read(handle, min(65536, before.st_size + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(handle)
        if file_identity(before) != file_identity(after) or len(data) != before.st_size:
            raise ValueError('候補が読取り中に変わった')
        return bytes(data), before
    finally:
        os.close(handle)


def validate_candidate(data, digest):
    if not re.fullmatch('[0-9a-f]{64}', digest) or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('候補の sha256 が保持値と違う')
    spec = importlib.util.spec_from_file_location('adopt_candidate_keys', SCRIPTS / 'candidate-keys.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, checked = module.check_text(data.decode('utf-8'), None)
    problems = checked.get('problems', [])
    blocking = [item for item in problems if isinstance(item, dict)
                and item.get('code') in ('skipped', 'skipped-malformed')]
    if blocking:
        raise ValueError('見送りの行を確認し、削除して commit してから呼び直す')
    return problems


def root_run(root_fd, argv, env, timeout):
    """/proc の cwd 表現を使わず、保持済み root fd だけから子の cwd を決める。"""
    def enter_root():
        os.fchdir(root_fd)
    return subprocess.run(argv, cwd='/', pass_fds=(root_fd,), preexec_fn=enter_root,
                          env=env, capture_output=True, timeout=timeout)


def native_rename_noreplace(parent_fd, source, dest):
    """利用可能な Linux renameat2 を使う。False は POSIX fallback を選ぶ意味だけ。"""
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        return False
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(source), parent_fd, os.fsencode(dest), 1) == 0:  # RENAME_NOREPLACE
        return True
    code = ctypes.get_errno()
    if code in (errno.ENOSYS, errno.EINVAL):
        return False
    raise OSError(code, '上書きしない改名に失敗')


def destination_is(parent_fd, name, held):
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == held


def rollback_created_destination(parent_fd, dest, held):
    """作成直後の自分の inode だけを消す。別 inode は第三者の宛先として保全する。"""
    if destination_is(parent_fd, dest, held):
        os.unlink(dest, dir_fd=parent_fd)
        return True
    return False


def fallback_link_unlink(parent_fd, source, dest, data, identity):
    """POSIX fallback。link は no-overwrite、unlink 前の失敗では自分の link だけ戻す。"""
    try:
        os.link(source, dest, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
    except FileExistsError:
        raise ValueError('改名先が既にある')
    created = os.stat(dest, dir_fd=parent_fd, follow_symlinks=False)
    held = created.st_dev, created.st_ino
    if not stat.S_ISREG(created.st_mode) or held != (identity.st_dev, identity.st_ino):
        # link 成功直後でも、同じ UID の別プロセスが宛先を差し替え得る。
        # source と同じ inode でない宛先は自分の作成物と証明できないため削除しない。
        raise ValueError('改名先を自分が作った通常ファイルと確認できない')
    try:
        fresh, current = read_candidate(parent_fd, source)
        if fresh != data or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise ValueError('改名直前に候補が変わった')
        os.unlink(source, dir_fd=parent_fd)
    except BaseException as exc:
        try:
            rolled = rollback_created_destination(parent_fd, dest, held)
        except OSError:
            rolled = False
        detail = '自身が作った宛先を戻した' if rolled else '候補と宛先を保全した'
        raise ValueError(f'非原子的な採用を完了できない({detail})') from exc
    moved, after = read_candidate(parent_fd, dest)
    if moved != data or (after.st_dev, after.st_ino) != held:
        raise ValueError('改名後に宛先が変わった。候補の残存状態を人が確認する')


def index_stage_zero(git, relative, when):
    """候補の index entry を、改名前/後で同じ厳密な形として保持する。"""
    staged = git('--literal-pathspecs', 'ls-files', '--stage', '-z', '--', str(relative))
    records = staged.stdout.split(b'\0')
    if staged.returncode or len(records) != 2 or records[1]:
        raise ValueError(f'追跡済み候補の{when} index を確認できない')
    try:
        fields, path = records[0].split(b'\t', 1)
        mode, oid, stage = fields.split(b' ')
    except ValueError as exc:
        raise ValueError(f'追跡済み候補の{when} index が一意でない') from exc
    if (mode not in (b'100644', b'100755') or stage != b'0' or
            path != os.fsencode(str(relative)) or not re.fullmatch(b'[0-9a-f]{40,64}', oid)):
        raise ValueError(f'追跡済み候補の{when} index が通常ファイル1件でない')
    return mode, oid


def verify_tracked_move(parent_fd, root, dest, identity, data, held_stage, git):
    """git mv 後の作業ツリーと index が、それぞれ改名前の保持値を保つか照合する。"""
    moved, after = read_candidate(parent_fd, dest.name)
    if moved != data or (after.st_dev, after.st_ino) != (identity.st_dev, identity.st_ino):
        raise ValueError('追跡済み候補の改名後に本文または inode が保持値と違う。候補と index の残存状態を人が確認する')
    relative = str(dest.relative_to(root))
    try:
        current_stage = index_stage_zero(git, relative, '改名後の')
    except ValueError as exc:
        raise ValueError(f'{exc}。候補と index の残存状態を人が確認する') from exc
    if current_stage != held_stage:
        raise ValueError('追跡済み候補の改名後の index stage が改名前の保持値と違う。候補と index の残存状態を人が確認する')


def adopt(root, source, name, digest, accept=None):
    refuse_loop()
    root, source = Path(root).absolute(), Path(source).absolute()
    relative = source.relative_to(root)
    if not source.name.startswith('候補_') or not source.name.endswith('.md') or source.name == '候補_.md':
        raise ValueError('候補_{名}.md を指定する')
    new_name = name if name is not None else source.name[3:-3]
    if not new_name or any(c in new_name for c in '/\\\0\n\r') or new_name in ('.', '..'):
        raise ValueError('採用名が不正')
    root_fd, parent_fd = directory(root), directory(source.parent)
    try:
        data, identity = read_candidate(parent_fd, source.name)
        warnings = validate_candidate(data, digest)
        for state in ('候補', '進行中', '完了', '保留', '中断'):
            candidate = f'{state}_{new_name}.md'
            if candidate == source.name:
                continue
            try:
                os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError('同じ名の状態ファイルがある')
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
        git_prefix = ['git', *SAFE_GIT_PREFIX]
        def git(*args):
            return root_run(root_fd, [*git_prefix, *args], env, 30)
        repository = any(os.path.lexists(parent / '.git') for parent in (root, *root.parents))
        tracked = False
        if repository:
            guard = SCRIPTS.parents[1] / 'do-task/scripts/diff-snapshot.sh'
            precheck = ['bash', str(guard), '--cwd', str(root), '--precheck']
            if accept:
                precheck += ['--accept', accept]
            result = root_run(root_fd, precheck, env, 60)
            if result.returncode:
                raise ValueError('git の事前検査に通らない: ' + result.stderr.decode('utf-8', 'replace'))
            if git('rev-parse', '--show-toplevel').returncode:
                raise ValueError('git の作業ツリーを確認できない')
            tracked = git('--literal-pathspecs', 'ls-files', '--error-unmatch', '--', str(relative)).returncode == 0
        fresh, current = read_candidate(parent_fd, source.name)
        if fresh != data or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise ValueError('採用直前に候補が変わった')
        # 正式入口の最後の検査。開始後に印が付いた場合にも改名も stage もしない。
        refuse_loop()
        dest = source.with_name(f'進行中_{new_name}.md')
        if tracked:
            held_stage = index_stage_zero(git, relative, '改名前の')
            result = git('--literal-pathspecs', 'mv', '--', str(relative), str(dest.relative_to(root)))
            if result.returncode:
                raise ValueError('git mv に失敗: ' + result.stderr.decode('utf-8', 'replace'))
            verify_tracked_move(parent_fd, root, dest, identity, data, held_stage, git)
        elif not native_rename_noreplace(parent_fd, source.name, dest.name):
            fallback_link_unlink(parent_fd, source.name, dest.name, data, current)
        else:
            moved, after = read_candidate(parent_fd, dest.name)
            if moved != data or (after.st_dev, after.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError('改名後に宛先が変わった。候補の残存状態を人が確認する')
        return {'path': str(dest), 'tracked': tracked, 'sha256': digest, 'warnings': warnings}
    finally:
        os.close(parent_fd)
        os.close(root_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--expect-sha256', required=True)
    parser.add_argument('--name')
    parser.add_argument('--accept')
    parser.add_argument('--unattended', action='store_true')
    parser.add_argument('candidate')
    args = parser.parse_args()
    try:
        refuse_loop()
        if args.unattended:
            raise ValueError('無人実行では候補を採用できない')
        print(json.dumps(adopt(args.root, args.candidate, args.name, args.expect_sha256, args.accept), ensure_ascii=False))
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f'候補の採用を停止: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
