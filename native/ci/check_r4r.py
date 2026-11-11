#!/usr/bin/env python3
"""R4R専用比較。旧 helper(restore-taskmd.py)を変えず、同じパスの配置・同じ argv で
Rust の検査専用入口 r4r_contract と比べる。

比較の正規化は 2 つだけ: stdout の一時フォルダの 16 進 32 桁と、偽の cp が記録した argv の
fd 番号(継いだ fd の番号と一致することを別に確かめる)。時間切れは成功に数えない。
cp の子の環境の比較から外すのは、2026-11-12 02:41 の決定の __CF_USER_TEXT_ENCODING の 1 項目だけ
(条件は exclude_cf_encoding。除いた値と有無は記録の行に出す)。
"""
import argparse
import contextlib
import ctypes
import dataclasses
import errno
import hashlib
import importlib.util
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
import traceback
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from support_proc import assert_absent_now  # noqa: E402

HELPER = ROOT / 'plugins/dev-workflow/skills/do-task/scripts/restore-taskmd.py'
EXISTING = ROOT / 'scripts/test_restore_taskmd.py'
MAP = Path(__file__).with_name('r4r_test_map.json')
FIXTURE = None
SYSTEM = 'linux' if sys.platform.startswith('linux') else 'darwin' if sys.platform == 'darwin' else 'other'
LINUX = SYSTEM == 'linux'

# scripts/test_restore_taskmd.py の RestoreTests の全 63 件の名前(改行で結んだ sha256)。
EXISTING_MANIFEST = (63, 'ab50c74e40a926393e9818e133f6e9e35b9cf64a8410642eda43db3f3248b9a9')
# 旧 helper を起動し、内部を差し替えない 16 件。
SIXTEEN = (
    'test_normal_existing_and_absent_binary_empty_modes_and_names',
    'test_strict_arguments_reject_before_file_access',
    'test_source_type_mode_and_hash_fail_closed',
    'test_destination_types_and_parent_symlink',
    'test_metadata_matches_gnu_archive_copy',
    'test_available_posix_acl_matches_gnu_archive_copy',
    'test_larger_than_200_mib_is_allowed',
    'test_cp_version_output_limit_and_invalid_banner',
    'test_copy_child_limit_and_only_two_fds',
    'test_cp_receives_reflink_never_under_file_size_limit',
    'test_cp_rejecting_reflink_never_is_fd_copy_unavailable',
    'test_probe_copy_content_mismatch_is_fd_copy_unavailable',
    'test_copy_over_initial_size_is_stopped_and_never_published',
    'test_caller_terminates_real_full_pipe_and_reaps_fixture',
    'test_public_helper_term_int_hup_stop_copy_and_reap',
    'test_public_helper_kill_has_no_claim_of_unchanged_or_success',
)
# 監督の関数だけを確かめる 1 件(helper を起動しない)。Linux で元のまま流し、比較の件数に数えない。
SUPERVISION = ('test_caller_requires_gnu_timeout_and_fixed_argv',)
# 直接 helper を起動する件。L と R は回の中の関係を突き合わせる。
DIRECT = (
    'test_strict_arguments_reject_before_file_access',
    'test_caller_terminates_real_full_pipe_and_reaps_fixture',
    'test_public_helper_term_int_hup_stop_copy_and_reap',
    'test_public_helper_kill_has_no_claim_of_unchanged_or_success',
)
MAC_APPLY = (
    'test_strict_arguments_reject_before_file_access',
    'test_source_type_mode_and_hash_fail_closed',
    'test_destination_types_and_parent_symlink',
    'test_cp_version_output_limit_and_invalid_banner',
)
MAC_RESULT_ONLY = (
    'test_normal_existing_and_absent_binary_empty_modes_and_names',
    'test_metadata_matches_gnu_archive_copy',
    'test_larger_than_200_mib_is_allowed',
    'test_copy_child_limit_and_only_two_fds',
    'test_cp_receives_reflink_never_under_file_size_limit',
    'test_cp_rejecting_reflink_never_is_fd_copy_unavailable',
    'test_probe_copy_content_mismatch_is_fd_copy_unavailable',
    'test_copy_over_initial_size_is_stopped_and_never_published',
)
MAC_SKIP = (
    'test_caller_terminates_real_full_pipe_and_reaps_fixture',
    'test_public_helper_term_int_hup_stop_copy_and_reap',
    'test_public_helper_kill_has_no_claim_of_unchanged_or_success',
    'test_available_posix_acl_matches_gnu_archive_copy',
)
# 対応表の中の、Rust で再現できない件(件数と、名前を改行で結んだ sha256)。
UNREPRODUCIBLE_MANIFEST = (0, hashlib.sha256(b'').hexdigest())
TEMP_HEX = re.compile(rb'(\.restore-taskmd-)[0-9a-f]{32}')
BANNER = b'cp (GNU coreutils) fixture\n'
STUB_ENV_KEYS = ()


class ComparisonError(Exception):
    """旧 helper と Rust の結果の不一致。既存の検査の失敗(AssertionError)と区別する。"""


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def use_real_tempdir():
    """このプロセスの一時フォルダの置き場を、リンクを含まない実体のパスにする。Mac の既定(/var/folders/…)
    は /var が /private/var へのリンクなので、親の各段をリンクを辿らずに開く旧 helper と Rust は、退避
    本文を開けずに taskmd-body で止まり、比べたい経路に届かない。比べる配置は実体のパスに置く(既存の
    テストの TemporaryDirectory も、同じプロセスの中でこの置き場を使う)。"""
    tempfile.tempdir = os.path.realpath(tempfile.gettempdir())
    return tempfile.tempdir


def fixture_path(value):
    if not value or not Path(value).is_absolute():
        raise ValueError('検査入口は絶対パスで指定してください')
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('検査入口は実行可能な通常ファイルである必要があります')
    return path


@dataclasses.dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


def normalize(stdout):
    """stdout の一時フォルダの 16 進 32 桁だけを置き換える。"""
    return TEMP_HEX.sub(lambda m: m.group(1) + b'X' * 32, stdout)


def launch(argv, *, env=None, stdout=subprocess.PIPE, preexec=None, timeout=60, pass_fds=(), during=None,
           close_fds=True):
    """新しいセッションを作らず、stdin は /dev/null で起動する。時間切れは直接の子を KILL して回収する。
    during は起動の直後に呼ぶ(コピー中に信号を送る件など)。"""
    process = subprocess.Popen([str(a) if isinstance(a, Path) else a for a in argv], env=env,
                               stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.PIPE,
                               preexec_fn=preexec, pass_fds=pass_fds, close_fds=close_fds)
    if during is not None:
        try:
            during(process)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    timed_out = False
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        out, err = process.communicate()
    return RunResult(process.returncode, out or b'', err or b'', timed_out)


def legacy_argv(args):
    return [sys.executable, '-I', '-B', str(HELPER), *args]


def native_argv(args):
    return [str(FIXTURE), *args]


def same_result(old, new):
    if old.timed_out or new.timed_out:
        raise ComparisonError('比較対象が時間切れになった')
    if old.returncode != new.returncode:
        raise ComparisonError(f'終了値が違う: 旧 {old.returncode} / Rust {new.returncode}')
    if normalize(old.stdout) != normalize(new.stdout):
        raise ComparisonError(f'stdout が違う: 旧 {old.stdout!r} / Rust {new.stdout!r}')
    if old.stderr != new.stderr:
        raise ComparisonError(f'stderr が違う: 旧 {old.stderr!r} / Rust {new.stderr!r}')


def fields(stdout):
    lines = stdout.split(b'\n')
    if not stdout.endswith(b'\n'):
        raise AssertionError('応答が改行で終わらない')
    pairs = [line.split(b'=', 1) for line in lines[:-1]]
    return {key.decode(): value for key, value in pairs}


def fixed(result, code, reason=None, touched=None, temp=None):
    """旧 helper の固定期待(終了値・理由・TOUCHED・TEMP)。"""
    if result.timed_out or result.returncode != code:
        raise AssertionError(f'固定期待の終了値と違う: {result.returncode} != {code}')
    values = fields(result.stdout)
    for key, expected in (('REASON', reason), ('TOUCHED', touched), ('TEMP', temp)):
        if expected is not None and values.get(key) != expected.encode():
            raise AssertionError(f'固定期待の {key} と違う: {values.get(key)!r} != {expected!r}')
    if reason is None and 'REASON' in values:
        raise AssertionError('固定期待に無い REASON がある')


# ---- 配置の控えと観測 ----

def snapshot(root, backup):
    if backup.exists():
        shutil.rmtree(backup)
    subprocess.run(['cp', '-a', '--', str(root), str(backup)], check=True)


def restore_layout(root, backup):
    for child in list(root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            make_writable(child)
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in backup.iterdir():
        subprocess.run(['cp', '-a', '--', str(child), str(root)], check=True)


def make_writable(path):
    os.chmod(path, 0o700)
    for current, dirs, _ in os.walk(path):
        for name in dirs:
            child = os.path.join(current, name)
            if not os.path.islink(child):
                os.chmod(child, 0o700)


def getfacl(path):
    getter = shutil.which('getfacl')
    if not getter:
        return None
    done = subprocess.run([getter, '-c', '-p', '-n', str(path)], capture_output=True)
    return done.stdout if done.returncode == 0 else b'error'


def xattrs(path):
    if not hasattr(os, 'listxattr'):
        return None
    try:
        names = sorted(n for n in os.listxattr(path, follow_symlinks=False) if n.startswith('user.'))
        return [(n, os.getxattr(path, n, follow_symlinks=False).hex()) for n in names]
    except OSError as error:
        return ('error', error.errno)


def file_record(path, *, content=True):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        return ('error', error.errno)
    record = {'type': stat.S_IFMT(info.st_mode), 'mode': info.st_mode, 'uid': info.st_uid, 'gid': info.st_gid}
    if stat.S_ISREG(info.st_mode):
        record['mtime_ns'] = info.st_mtime_ns
        record['size'] = info.st_size
        if content:
            try:
                digest = hashlib.sha256()
                with open(path, 'rb') as stream:
                    for block in iter(lambda: stream.read(1 << 20), b''):
                        digest.update(block)
                record['sha256'] = digest.hexdigest()
            except OSError as error:
                record['sha256'] = ('error', error.errno)
        record['xattr'] = xattrs(path)
        record['acl'] = getfacl(path)
    return record


def temp_record(parent):
    """一時フォルダの残り。名前の 16 進は置き換え、中のエントリの名前・種類・mode を記録する。"""
    result = []
    try:
        names = sorted(os.listdir(os.fsencode(parent)))
    except OSError as error:
        return ('error', error.errno)
    for name in names:
        if not name.startswith(b'.restore-taskmd-'):
            continue
        path = os.path.join(os.fsencode(parent), name)
        info = os.lstat(path)
        entries = []
        try:
            for child in sorted(os.listdir(path)):
                child_info = os.lstat(os.path.join(path, child))
                entries.append((child, child_info.st_mode))
        except OSError as error:
            entries = ('error', error.errno)
        result.append((TEMP_HEX.sub(lambda m: m.group(1) + b'X' * 32, name), info.st_mode, entries))
    return result


@dataclasses.dataclass
class Observed:
    result: RunResult
    files: dict
    temps: list
    relations: dict
    stub: list
    elapsed: float
    # helper を起動したときに渡した環境に __CF_USER_TEXT_ENCODING があったか(run_side が起動の直前に
    # 記録する)。None は記録していない(不明なので、除く条件に当たらない)。
    cf_passed: object = None


STUB_FIELDS = ('argv0', 'args', 'env', 'fsize', 'fds', 'blocked', 'ignored', 'caught')


def differences(old, new):
    """旧と Rust で違う欄の名前の一覧。"""
    found = []
    if old.result.timed_out or new.result.timed_out:
        found.append('timeout')
    if old.result.returncode != new.result.returncode:
        found.append('returncode')
    if normalize(old.result.stdout) != normalize(new.result.stdout):
        found.append('stdout')
    if old.result.stderr != new.result.stderr:
        found.append('stderr')
    for name in sorted(set(old.files) | set(new.files)):
        if old.files.get(name) != new.files.get(name):
            found.append('files.' + name)
    if old.temps != new.temps:
        found.append('temps')
    if old.relations != new.relations:
        found.append('relations')
    if len(old.stub) != len(new.stub):
        found.append('cp.count')
    else:
        for left, right in zip(old.stub, new.stub):
            for field in STUB_FIELDS:
                if left.get(field) != right.get(field) and 'cp.' + field not in found:
                    found.append('cp.' + field)
    return found


# cp の環境の違いを出すときに、値も出す名前(地域設定と、Mac の CoreFoundation・Python の起動役が足しうる
# 名前)。ほかの名前は、実行機の環境の値を出さないよう、名前だけを出す。
ENV_SHOWN = ('LANG', 'LANGUAGE', '__CF_USER_TEXT_ENCODING', '__PYVENV_LAUNCHER__')
DETAIL_LIMIT = 300


def clip(text, limit=DETAIL_LIMIT):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def env_entries(hexed):
    return [entry for entry in bytes.fromhex(hexed).split(b'\0') if entry]


def env_name(entry):
    return entry.split(b'=', 1)[0]


def env_shown(entry):
    """値を出してよい名前なら NAME=値、ほかは名前だけ(値は出さない)。"""
    name = env_name(entry)
    text = entry if name.startswith(b'LC_') or name.decode('latin-1') in ENV_SHOWN else name
    return repr(text.decode('utf-8', 'backslashreplace'))


def env_difference(old_hex, new_hex):
    """cp が受け取った環境の違い: 旧だけにある項目・Rust だけにある項目・並びの違い。"""
    left, right = env_entries(old_hex), env_entries(new_hex)
    only_old = [entry for entry in left if entry not in right]
    only_new = [entry for entry in right if entry not in left]
    parts = []
    if only_old:
        parts.append('旧だけ [' + ', '.join(map(env_shown, only_old)) + ']')
    if only_new:
        parts.append('Rust だけ [' + ', '.join(map(env_shown, only_new)) + ']')
    common_old = [entry for entry in left if entry in right]
    common_new = [entry for entry in right if entry in left]
    if common_old != common_new:
        index = next((i for i, (a, b) in enumerate(zip(common_old, common_new)) if a != b),
                     min(len(common_old), len(common_new)))

        def name(items):
            if index >= len(items):
                return '(なし)'
            return repr(env_name(items[index]).decode('utf-8', 'backslashreplace'))
        parts.append(f'並び: 共通の項目の {index + 1} 番目が 旧 {name(common_old)} / Rust {name(common_new)}')
    hidden = any(env_shown(entry) != repr(entry.decode('utf-8', 'backslashreplace')) for entry in only_old + only_new)
    return clip('cp.env: ' + ('; '.join(parts) if parts else '項目の数だけが違う')), hidden


def stub_details(old, new):
    """cp の欄の違いの中身。env は決めた名前だけ値を出し、ほかの欄は両方の値を出す。"""
    if len(old.stub) != len(new.stub):
        return [f'cp.count: 旧 {len(old.stub)} 件 / Rust {len(new.stub)} 件']
    details = []
    hidden = False
    for number, (left, right) in enumerate(zip(old.stub, new.stub), 1):
        prefix = f'cp {number} 回目の ' if len(old.stub) > 1 else ''
        for field in STUB_FIELDS:
            if left.get(field) == right.get(field):
                continue
            if field == 'env':
                text, some_hidden = env_difference(left['env'], right['env'])
                details.append(prefix + text)
                hidden = hidden or some_hidden
            else:
                details.append(prefix + clip(f'cp.{field}: 旧 {left.get(field)!r} / Rust {right.get(field)!r}'))
    if hidden:
        details.append('env は LC_*・LANG・LANGUAGE・__CF_USER_TEXT_ENCODING・__PYVENV_LAUNCHER__ のほかは名前だけを出す')
    return details


# 2026-11-12 02:41 の決定で、cp の子の環境の比較から外す 1 項目。Mac の旧 Python(CoreFoundation)が
# 旧の子の環境へ自分で足す値で、Rust は再現しない。
CF_ENCODING = '__CF_USER_TEXT_ENCODING'


def cf_encoding_prefix():
    """除く値の始まり: 0x と、比較器の uid の 16 進(大文字・先頭に 0 を付けない。uid 0 は 0)と :。"""
    return b'0x' + format(os.getuid(), 'X').encode() + b':'


def passed_has_cf(env):
    """旧 helper を起動するときに渡す環境に CF_ENCODING があるか。env を指定しない起動(環境を受け継ぐ
    起動)は、起動の直前の比較器の os.environ で決める。"""
    return CF_ENCODING in (os.environ if env is None else env)


def exclude_cf_encoding(old, new, system=None):
    """旧の側の cp の子の環境から CF_ENCODING の 1 項目を除いた観測と、記録の行を返す。次のすべてを
    満たす cp の記録だけを除く: Mac(darwin)で流している・旧 helper を起動したときに渡した環境に無い
    (old.cf_passed が False。記録していない None は除かない)・旧の記録にこの名前の項目がちょうど 1 個
    あり、値が cf_encoding_prefix() で始まる。Rust の側は一切除かない(渡していないのに Rust の子に
    現れたら、比べて不一致になる)。ほかの名前・形の違う値・2 個以上は除かない。"""
    system = system or SYSTEM
    if system != 'darwin' or old.cf_passed is not False:
        return old, []
    name = CF_ENCODING.encode()
    prefix = cf_encoding_prefix()
    stub = []
    notes = []
    for number, record in enumerate(old.stub, 1):
        # 並びと空の項目を変えないよう、区切りだけで分けて、除く 1 項目のほかは字面のまま戻す。
        entries = bytes.fromhex(record['env']).split(b'\0')
        found = [index for index, entry in enumerate(entries) if env_name(entry) == name]
        if len(found) != 1:
            stub.append(record)
            continue
        entry = entries[found[0]]
        _, equals, value = entry.partition(b'=')
        if not equals or not value.startswith(prefix):
            stub.append(record)
            continue
        del entries[found[0]]
        stub.append(dict(record, env=b'\0'.join(entries).hex()))
        rust = new.stub[number - 1] if number <= len(new.stub) else None
        rust_count = None if rust is None else sum(1 for item in env_entries(rust['env']) if env_name(item) == name)
        rust_text = '記録なし' if rust is None else 'なし' if rust_count == 0 else f'あり({rust_count} 個)'
        notes.append(f'R4R: 記録({CF_ENCODING} を旧の cp の子の環境から除いた): cp {number} 回目: '
                     f'値 {value.decode("utf-8", "backslashreplace")!r} / 渡した環境に なし / 旧の子に あり(1 個) / '
                     f'Rust の子に {rust_text}')
    if not notes:
        return old, []
    return dataclasses.replace(old, stub=stub), notes


def compare_observed(old, new, system=None):
    """旧と Rust の観測を比べる。cp の子の環境は、exclude_cf_encoding の条件を満たす 1 項目だけを旧の側
    から除いてから比べ、除いたときは比べる前に記録の行を出す(比べて落ちても記録が残る)。"""
    old, notes = exclude_cf_encoding(old, new, system)
    for note in notes:
        print(note, flush=True)
    found = differences(old, new)
    if found:
        detail = []
        for name in found:
            if name in ('returncode', 'stdout', 'stderr'):
                detail.append(f'{name}: 旧 {getattr(old.result, name)!r} / Rust {getattr(new.result, name)!r}')
        if any(name.startswith('cp.') for name in found):
            detail.extend(stub_details(old, new))
        raise ComparisonError('旧と Rust が違う: ' + ', '.join(found) + ('(' + '; '.join(detail) + ')' if detail else ''))


# ---- 偽の cp ----

OBSERVER_C = r'''/* R4R の偽の cp。exec された時点の argv[0]・argv・environ の並び・RLIMIT_FSIZE・開いた fd と flags・
 * 信号の mask と既定を記録してから、指定の動作をする。環境・信号・fd を変えないため、シェルや
 * Python を通さない。設定(R4R_*)は比較器が組む時に config.h へ書く。 */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include "config.h"

extern char **environ;

static char record[1 << 20];
static size_t used;

static void put(const char *text) {
    size_t length = strlen(text);
    if (used + length < sizeof(record)) {
        memcpy(record + used, text, length);
        used += length;
    }
}

static void put_hex(const char *key, const char *value) {
    static const char digits[] = "0123456789abcdef";
    char pair[3] = {0, 0, 0};
    put(key);
    put("\t");
    for (const unsigned char *p = (const unsigned char *)value; *p; p++) {
        pair[0] = digits[*p >> 4];
        pair[1] = digits[*p & 15];
        put(pair);
    }
    put("\n");
}

static void send_text(const char *text, size_t length) {
    struct sockaddr_un address;
    int channel = socket(AF_UNIX, SOCK_STREAM, 0);
    if (channel < 0) {
        _exit(98);
    }
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    strncpy(address.sun_path, R4R_SOCKET, sizeof(address.sun_path) - 1);
    if (connect(channel, (struct sockaddr *)&address, sizeof(address)) != 0) {
        _exit(98);
    }
    while (length > 0) {
        ssize_t written = write(channel, text, length);
        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            _exit(98);
        }
        text += written;
        length -= (size_t)written;
    }
    close(channel);
}

static const char *kind_of(int fd) {
    struct stat info, null;
    if (fstat(fd, &info) != 0) {
        return "?";
    }
    if (S_ISFIFO(info.st_mode)) {
        return "pipe";
    }
    if (S_ISCHR(info.st_mode) && stat("/dev/null", &null) == 0 && info.st_rdev == null.st_rdev) {
        return "/dev/null";
    }
    if (S_ISREG(info.st_mode)) {
        return "file";
    }
    if (S_ISSOCK(info.st_mode)) {
        return "socket";
    }
    if (S_ISDIR(info.st_mode)) {
        return "dir";
    }
    return "other";
}

static void put_fd(int fd) {
    char line[128];
    int flags = fcntl(fd, F_GETFL);
    int fdflags = fcntl(fd, F_GETFD);
    if (flags < 0 || fdflags < 0) {
        return;
    }
    snprintf(line, sizeof(line), "fd\t%d %d %d %s\n", fd, flags, fdflags, kind_of(fd));
    put(line);
}

static void put_fds(void) {
    DIR *directory = opendir("/proc/self/fd");
    if (directory != NULL) {
        int own = dirfd(directory);
        struct dirent *entry;
        while ((entry = readdir(directory)) != NULL) {
            char *end;
            long fd = strtol(entry->d_name, &end, 10);
            if (*entry->d_name && *end == 0 && fd != own) {
                put_fd((int)fd);
            }
        }
        closedir(directory);
        return;
    }
    struct rlimit limit;
    long highest = 4096;
    if (getrlimit(RLIMIT_NOFILE, &limit) == 0 && limit.rlim_cur != RLIM_INFINITY && (long)limit.rlim_cur > highest) {
        highest = (long)limit.rlim_cur;
    }
    for (int fd = 0; fd < highest; fd++) {
        put_fd(fd);
    }
}

static void put_limit(void) {
    struct rlimit limit;
    char line[128];
    if (getrlimit(RLIMIT_FSIZE, &limit) != 0) {
        put("fsize\terror\n");
        return;
    }
    long long soft = limit.rlim_cur == RLIM_INFINITY ? -1 : (long long)limit.rlim_cur;
    long long hard = limit.rlim_max == RLIM_INFINITY ? -1 : (long long)limit.rlim_max;
    snprintf(line, sizeof(line), "fsize\t%lld %lld\n", soft, hard);
    put(line);
}

static void put_signals(void) {
    sigset_t blocked;
    unsigned long long mask = 0, ignored = 0, caught = 0;
    char line[160];
    sigemptyset(&blocked);
    sigprocmask(SIG_BLOCK, NULL, &blocked);
    for (int number = 1; number < 32; number++) {
        struct sigaction action;
        if (sigismember(&blocked, number) == 1) {
            mask |= 1ULL << (number - 1);
        }
        if (sigaction(number, NULL, &action) == 0) {
            if (action.sa_handler == SIG_IGN) {
                ignored |= 1ULL << (number - 1);
            } else if (action.sa_handler != SIG_DFL) {
                caught |= 1ULL << (number - 1);
            }
        }
    }
    snprintf(line, sizeof(line), "signals\t%llx %llx %llx\n", mask, ignored, caught);
    put(line);
}

static void say(const char *text) {
    size_t length = strlen(text);
    while (length > 0) {
        ssize_t written = write(1, text, length);
        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            return;
        }
        text += written;
        length -= (size_t)written;
    }
}

static void banner_with_padding(size_t total) {
    static char text[8200];
    const char *banner = "cp (GNU coreutils) fixture\n";
    size_t length = strlen(banner);
    memcpy(text, banner, length);
    memset(text + length, 'x', total - length);
    text[total] = 0;
    say(text);
}

int main(int argc, char **argv) {
    char line[64];
    put_hex("argv0", argv[0]);
    for (int i = 1; i < argc; i++) {
        put_hex("arg", argv[i]);
    }
    for (char **entry = environ; *entry; entry++) {
        put_hex("env", *entry);
    }
    put_limit();
    put_fds();
    put_signals();
    snprintf(line, sizeof(line), "pid\t%d\n", (int)getpid());
    put(line);
    send_text(record, used);

    const char *mode = R4R_MODE;
    if (argc == 2 && strcmp(argv[1], "--r4r-probe") == 0) {
        /* 組んだ直後の確かめ。記録だけを送って終わる。 */
        return 0;
    }
    /* 旧 helper と Rust は ["--version"] だけで呼ぶ。比較器の自己検査が argv だけを変えても
     * 動きが変わらないよう、2 番目以降は見ない。 */
    if (argc >= 2 && strcmp(argv[1], "--version") == 0) {
        if (strcmp(mode, "version-exit1") == 0) {
            say("cp (GNU coreutils) fixture\n");
            return 1;
        }
        if (strcmp(mode, "version-8192") == 0) {
            banner_with_padding(8192);
            return 0;
        }
        if (strcmp(mode, "version-8193") == 0) {
            banner_with_padding(8193);
            return 0;
        }
        if (strcmp(mode, "version-term-ignore") == 0) {
            signal(SIGTERM, SIG_IGN);
            say("cp (GNU coreutils) fixture\n");
            sleep(60);
            return 0;
        }
        say("cp (GNU coreutils) fixture\n");
        if (strcmp(mode, "version-self-delete") == 0) {
            unlink(R4R_SELF);
        }
        return 0;
    }
    if (strcmp(mode, "version-only") == 0) {
        return 1;
    }
    if (strcmp(mode, "copy-hold") == 0 && argc >= 2) {
        char target[4096];
        ssize_t length = readlink(argv[argc - 2], target, sizeof(target) - 1);
        size_t hold = strlen(R4R_HOLD);
        if (length >= (ssize_t)hold) {
            target[length] = 0;
            if (strcmp(target + length - hold, R4R_HOLD) == 0) {
                snprintf(line, sizeof(line), "held\t%d\n", (int)getpid());
                send_text(line, strlen(line));
                sleep(60);
                return 0;
            }
        }
    }
    argv[0] = (char *)R4R_REAL;
    execv(R4R_REAL, argv);
    return 127;
}
'''


def real_cp():
    return os.path.realpath(shutil.which('cp'))


def c_string(text):
    if any(ord(c) < 32 or ord(c) > 126 for c in text):
        raise ValueError('観測器へ渡す文字列は ASCII の印字可能な文字にしてください')
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def compiler():
    found = shutil.which('cc')
    if not found:
        # 省略しない。観測器を組めなければ比較を始めない。
        raise AssertionError('cc が無いので、偽の cp の観測器を組めない')
    return found


class Channel:
    """偽の cp の記録を受けるストリームのソケット。短いパス(/tmp の下)に置き、受け手のスレッドで読む。"""

    def __init__(self):
        import socket
        import threading
        self.directory = Path(tempfile.mkdtemp(prefix='r4r-', dir='/tmp'))
        self.path = self.directory / 's'
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(128)
        self.lock = threading.Lock()
        self.records = []
        self.busy = 0
        self.closing = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        import select
        while True:
            # 受け付ける前に「読みかけ」を数える(settle が、受け付けた後で数える前の隙間を見ないため)。
            try:
                select.select([self.server], [], [])
            except (OSError, ValueError):
                return
            with self.lock:
                self.busy += 1
            try:
                connection, _ = self.server.accept()
            except OSError:
                with self.lock:
                    self.busy -= 1
                return
            if self.closing:
                connection.close()
                with self.lock:
                    self.busy -= 1
                return
            try:
                chunks = []
                with connection:
                    while True:
                        data = connection.recv(65536)
                        if not data:
                            break
                        chunks.append(data)
                with self.lock:
                    self.records.append(parse_record(b''.join(chunks)))
            finally:
                with self.lock:
                    self.busy -= 1

    def settle(self, timeout=10):
        """受け付け待ちの接続と読みかけの接続が無くなるまで待つ。"""
        import select
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = select.select([self.server], [], [], 0)[0]
            with self.lock:
                busy = self.busy
            if not pending and not busy:
                return
            time.sleep(.01)
        raise AssertionError('偽の cp の記録を受け終えられない')

    def drain(self):
        self.settle()
        with self.lock:
            result, self.records = self.records, []
        return result

    def wait_held(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for record in self.records:
                    if 'held' in record:
                        self.records.remove(record)
                        return record['held']
            time.sleep(.01)
        raise AssertionError('held を待てなかった')

    def close(self):
        import socket
        # 受け手のスレッドを起こしてから閉じる(閉じた番号を別の fd が使い回す前に終わらせる)。
        self.closing = True
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                wake.connect(str(self.path))
        except OSError:
            pass
        self.thread.join(5)
        try:
            self.server.close()
        finally:
            shutil.rmtree(self.directory, True)


def parse_record(data):
    record = {'args': [], 'env': [], 'fds': {}}
    for line in data.decode('ascii').splitlines():
        key, _, value = line.partition('\t')
        if key == 'argv0':
            record['argv0'] = os.fsdecode(bytes.fromhex(value))
        elif key == 'arg':
            record['args'].append(os.fsdecode(bytes.fromhex(value)))
        elif key == 'env':
            record['env'].append(bytes.fromhex(value))
        elif key == 'fsize':
            record['fsize'] = [int(part) for part in value.split()]
        elif key == 'fd':
            fd, flags, fdflags, kind = value.split(' ', 3)
            record['fds'][int(fd)] = [int(flags), int(fdflags), kind]
        elif key == 'signals':
            blocked, ignored, caught = (int(part, 16) for part in value.split())
            record.update(blocked=blocked, ignored=ignored, caught=caught)
        elif key == 'pid':
            record['pid'] = int(value)
        elif key == 'held':
            record['held'] = int(value)
        else:
            raise ComparisonError(f'偽の cp の記録の形が不正: {key}')
    return record


PROBE_ENV = {'R4R_PROBE_A': '1', 'R4R_PROBE_B': '2'}


def make_stub(directory, *, mode='gnu', hold='', source_text=None):
    """C の偽の cp を組む。argv[0]・argv・environ の並び・RLIMIT_FSIZE・fd と flags・信号の mask と
    既定を記録してソケットへ送ってから、mode の動作をする。組んだ直後に、決めた環境で起動して、
    観測器が argv と環境を変えずに記録することを確かめる(cc が無い・組めない・送れない・環境を
    書き換える観測器は、ここで失敗にする)。"""
    directory.mkdir(parents=True, exist_ok=True)
    channel = Channel()
    try:
        stub = directory / 'cp-stub'
        source = directory / 'observer.c'
        source.write_text(OBSERVER_C if source_text is None else source_text)
        (directory / 'config.h').write_text(
            f'#define R4R_SOCKET {c_string(str(channel.path))}\n#define R4R_MODE {c_string(mode)}\n'
            f'#define R4R_REAL {c_string(real_cp())}\n#define R4R_SELF {c_string(str(stub))}\n'
            f'#define R4R_HOLD {c_string(hold)}\n')
        built = subprocess.run([compiler(), '-O1', '-o', str(stub), str(source)], cwd=directory,
                               capture_output=True)
        if built.returncode:
            raise AssertionError('偽の cp の観測器を組めない: ' + built.stderr.decode(errors='replace'))
        stub.chmod(0o755)
        check_observer(stub, channel)
    except BaseException:
        channel.close()
        raise
    return stub, channel


def check_observer(stub, channel):
    """観測器を決めた argv と環境で起動し、記録がその argv と環境の並びのままであることを確かめる。"""
    done = subprocess.run([str(stub), '--r4r-probe'], env=dict(PROBE_ENV), stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=30)
    records = channel.drain()
    expected = [f'{key}={value}'.encode() for key, value in PROBE_ENV.items()]
    if done.returncode != 0 or len(records) != 1:
        raise AssertionError(f'偽の cp の観測器が失敗した(終了値 {done.returncode}・記録 {len(records)} 件)')
    record = records[0]
    if record.get('argv0') != str(stub) or record.get('args') != ['--r4r-probe']:
        raise AssertionError('偽の cp の観測器が argv を変えて記録した')
    if record.get('env') != expected:
        raise AssertionError('偽の cp の観測器が環境を書き換えた')


def read_stub(channel):
    """偽の cp の記録。fd 番号は、argv の別名の番号が継いだ fd と一致することを確かめてから役割へ置き換える。"""
    records = []
    if channel is None:
        return records
    for record in channel.drain():
        if 'held' in record:
            continue
        pid = record['pid']
        fds = record['fds']
        args = list(record['args'])
        roles = {}
        for index, role in ((-2, 'input'), (-1, 'output')):
            if len(args) >= 5 and re.fullmatch(r'/(proc/self|dev)/fd/[0-9]+', args[index]):
                number = int(args[index].rsplit('/', 1)[1])
                if number not in fds:
                    raise ComparisonError('cp の argv の fd 番号が、継いだ fd と一致しない')
                roles[number] = role
                args[index] = args[index].rsplit('/', 1)[0] + '/<' + role + '>'
        described = {}
        for fd, (flags, fdflags, kind) in fds.items():
            key = str(fd) if fd <= 2 else roles.get(fd, 'extra-%d' % fd)
            described[key] = [flags, fdflags, kind if fd <= 2 else '']
        records.append({'argv0': record['argv0'], 'args': args, 'fds': described, 'fsize': record['fsize'],
                        'blocked': record['blocked'], 'ignored': record['ignored'], 'caught': record['caught'],
                        'env': b'\0'.join(record['env']).hex()})
        try:
            assert_absent_now(pid)
        except AssertionError as error:
            raise ComparisonError(f'偽の cp が回収されていない: {error}')
    return records


# ---- 旧と Rust を同じ配置で順に流す ----

@dataclasses.dataclass
class Scenario:
    base: Path
    root: Path
    args: list
    env: dict = None
    stdout: str = 'pipe'
    preexec: object = None
    lock: object = None
    unlock: object = None
    log: Path = None
    timeout: float = 60
    entity: bool = True
    observe_extra: tuple = ()
    during: object = None
    pass_fds: tuple = ()
    source: str = None
    destination: str = None
    close_fds: bool = True
    stub_expected: bool = True


def path_arg(sc, key):
    explicit = sc.source if key == '--source' else sc.destination
    if explicit is not None:
        return explicit
    return sc.args[sc.args.index(key) + 1]


def observe(sc, result, relations, started, cf_passed=None):
    source = path_arg(sc, '--source')
    destination = path_arg(sc, '--destination')
    files = {'source': file_record(source), 'destination': file_record(destination)}
    for extra in sc.observe_extra:
        files[str(extra)] = file_record(extra)
    parents = sorted({os.path.dirname(os.fsencode(destination))})
    temps = [temp_record(parent) for parent in parents]
    stub = read_stub(sc.log)
    if sc.log is not None and sc.stub_expected and not stub:
        # 偽の cp を使う件で記録が 1 件も無ければ、観測器が失敗した(比べずに合格にしない)。
        raise AssertionError('偽の cp の記録が無い(観測器が失敗した)')
    return Observed(result, files, temps, relations, stub, time.monotonic() - started, cf_passed)


def open_stdout(kind, base):
    if kind == 'pipe':
        return subprocess.PIPE, None
    if kind == 'full':
        stream = open('/dev/full', 'wb')
        return stream, stream
    if kind == 'closed-pipe':
        read, write = os.pipe()
        os.close(read)
        stream = os.fdopen(write, 'wb')
        return stream, stream
    if kind.startswith('file:'):
        stream = open(base / kind[5:], 'wb')
        return stream, stream
    raise ValueError(kind)


def run_side(sc, legacy):
    destination = path_arg(sc, '--destination')
    link = sc.base / ('entity-link-' + ('old' if legacy else 'new'))
    if link.exists():
        link.unlink()
    held = None
    original = None
    relations = {}
    if sc.entity and os.path.isfile(destination) and not os.path.islink(destination):
        original = Path(destination).read_bytes()
        os.link(destination, link)
        held = os.open(destination, os.O_RDONLY | os.O_CLOEXEC)
    if sc.log is not None:
        sc.log.drain()
    if sc.lock:
        sc.lock()
    stream, owned = open_stdout(sc.stdout, sc.base)
    # 起動の直前に、渡す環境(env を指定しない起動では比較器の os.environ)に CF_ENCODING があるかを記録する。
    cf_passed = passed_has_cf(sc.env)
    started = time.monotonic()
    try:
        result = launch(legacy_argv(sc.args) if legacy else native_argv(sc.args), env=sc.env, stdout=stream,
                        preexec=sc.preexec, timeout=sc.timeout, pass_fds=sc.pass_fds, during=sc.during,
                        close_fds=sc.close_fds)
    finally:
        if owned is not None:
            owned.close()
        if sc.unlock:
            sc.unlock()
    if sc.stdout.startswith('file:'):
        result = dataclasses.replace(result, stdout=(sc.base / sc.stdout[5:]).read_bytes())
    if held is not None:
        now = os.lstat(destination) if os.path.lexists(destination) else None
        linked = os.lstat(link)
        relations = {
            'same-entity': now is not None and (now.st_dev, now.st_ino) == (linked.st_dev, linked.st_ino),
            'link-kept': link.read_bytes() == original,
            'fd-kept': os.pread(held, len(original) + 1, 0) == original,
        }
        os.close(held)
        link.unlink()
    return observe(sc, result, relations, started, cf_passed)


def run_pair(sc, before_compare=None):
    """before_compare は、比べる前に旧と Rust の観測を受け取る(比べて落ちても記録を残すため)。"""
    backup = sc.base / 'backup'
    snapshot(sc.root, backup)
    old = run_side(sc, True)
    restore_layout(sc.root, backup)
    new = run_side(sc, False)
    if before_compare is not None:
        before_compare(old, new)
    compare_observed(old, new)
    return old, new


# ---- 既存 16 件を旧(L)と Rust(R)で流す ----

SHIM = r'''import os, signal, sys
with open({log!r}, 'a') as stream:
    stream.write({label!r} + '\n')
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
original = {original!r}
if original is None:
    os.environ.pop('LC_CTYPE', None)
else:
    os.environ['LC_CTYPE'] = original
target = {target!r}
if target == 'legacy':
    os.execv(sys.executable, [sys.executable, '-I', '-B', {helper!r}] + sys.argv[1:])
os.execv(target, [target] + sys.argv[1:])
'''


def write_shim(directory, target, label):
    """HELPER の代わりに、Rust(または比較器の自己検査では旧 helper)を exec する小さな入口。"""
    log = directory / 'shim-launches'
    log.write_text('')
    shim = directory / 'r4r_shim.py'
    shim.write_text(SHIM.format(log=str(log), label=label, original=os.environ.get('LC_CTYPE'),
                                target=target, helper=str(HELPER)))
    return shim, log


class Clock:
    """既存の時間の検査を 1 回ずつ測るため、R の比較で先に流した旧 helper の時間を差し引く。"""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return time.monotonic() - self.offset

    def __getattr__(self, name):
        return getattr(time, name)


# 監督役の bash が子の終了を知らせる行(英語と日本語の地域設定)。信号の語だけを残す。
BASH_NOTICE = re.compile(rb'^[^:\n]+: (?:line [0-9]+|[0-9]+ \xe8\xa1\x8c): +[0-9]+ (\S+)\s+.*$')


def runner_stderr(err):
    """監督役の bash の stderr を行ごとに分ける。終了の知らせの行は信号の語だけに縮め、ほかの行
    (helper 自身の stderr を含む)は字面のまま残す。"""
    lines = []
    for line in err.split(b'\n'):
        match = BASH_NOTICE.match(line)
        lines.append(('notice', match.group(1)) if match else ('line', line))
    return lines


class Recorder:
    """テストが直接起動した helper の結果(終了値・stdout・stderr)を記録する subprocess の代わり。
    helper の起動(L は旧 helper、R は入口)と監督役の bash だけを記録し、ほかの起動は記録しない。
    Mac の代用品の cp で作る対照のコピー(cp -a)は、本物の cp で作る。"""

    def __init__(self, sink, helpers, substitute=None):
        self.sink = sink
        self.helpers = {str(helper) for helper in helpers}
        self.substitute = substitute

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def argv(self, args, kwargs):
        argv = list(args[0] if args else kwargs.pop('args'))
        if self.substitute is not None and argv and str(argv[0]) == self.substitute:
            argv[0] = real_cp()
        return argv, args[1:]

    def kind(self, argv):
        text = [os.fspath(item) if isinstance(item, os.PathLike) else item for item in argv]
        if text and text[0] == '/bin/bash':
            return 'runner'
        if any(isinstance(item, str) and item in self.helpers for item in text):
            return 'launch'
        return None

    def run(self, *args, **kwargs):
        argv, rest = self.argv(args, kwargs)
        done = subprocess.run(argv, *rest, **kwargs)
        if self.kind(argv) == 'launch':
            self.sink.append(('run', done.returncode, normalize(done.stdout or b''), done.stderr or b''))
        return done

    def Popen(self, *args, **kwargs):
        sink = self.sink
        argv, rest = self.argv(args, kwargs)
        kind = self.kind(argv)
        process = subprocess.Popen(argv, *rest, **kwargs)

        class Wrapped:
            recorded = False

            def __getattr__(self, name):
                return getattr(process, name)

            def communicate(self, *a, **k):
                out, err = process.communicate(*a, **k)
                # 後片付けで 2 回目を呼ぶ件があるので、プロセスごとに最初の 1 回だけ記録する。
                if self.recorded:
                    return out, err
                self.recorded = True
                if kind == 'runner':
                    sink.append(('runner', process.returncode, normalize(out or b''), runner_stderr(err or b'')))
                elif kind == 'launch':
                    sink.append(('popen', process.returncode, normalize(out or b''), err or b''))
                return out, err
        return Wrapped()


def end_state(test):
    """回の中の関係。値そのものではなく、等しいか・元のままか・一時フォルダが残るかを記録する。"""
    def read(path):
        try:
            return Path(path).read_bytes(), os.lstat(path)
        except OSError:
            return None, None
    dest, dest_info = read(test.destination)
    source, source_info = read(test.source)
    return {
        'dest-equals-source': dest is not None and dest == source,
        'dest-mode-equals-source': dest_info is not None and source_info is not None and
        stat.S_IMODE(dest_info.st_mode) == stat.S_IMODE(source_info.st_mode),
        # 復元していない回の mtime の一致は、作った時刻の刻みの粗さで偶然に決まるので、中身が等しい
        # (復元した)時だけ記録する。
        'dest-mtime-equals-source': (dest_info.st_mtime_ns == source_info.st_mtime_ns)
        if dest is not None and dest == source else None,
        'dest-unchanged': dest is not None and (dest, stat.S_IMODE(dest_info.st_mode)) == test.before,
        'temp-dirs': len(list(Path(test.root).glob('.restore-taskmd-*'))),
    }


class Deferred:
    """R の operation()。argv だけを控え、run() の時点の配置で旧 helper と Rust を同じパスで順に流す。"""

    def __init__(self, test, argv):
        self.test = test
        self.argv = argv

    def run(self):
        return self.test.pair(self.argv, cli=False)


ACL = 'test_available_posix_acl_matches_gnu_archive_copy'
ACL_REASONS = ('POSIX ACL の設定・読取りツールがないため未確認',
               'scratch filesystem が POSIX ACL の設定を受け付けないため未確認')


def check_partition():
    """Mac の 3 つの組(既存の検査を当てる 4・結果だけを比べる 8・省く 4)が重ならずに 16 件を覆う。"""
    groups = (MAC_APPLY, MAC_RESULT_ONLY, MAC_SKIP)
    if tuple(len(group) for group in groups) != (4, 8, 4) or len(SIXTEEN) != 16:
        raise AssertionError('Mac の組の件数が 4・8・4 でない')
    union = set().union(*groups)
    if union != set(SIXTEEN) or sum(len(set(group)) for group in groups) != len(union):
        raise AssertionError('Mac の 3 つの組が 16 件を重ならずに覆わない')


@dataclasses.dataclass
class Existing:
    suite: unittest.TestSuite
    launches: Path
    records: dict
    rust_names: list
    channels: list
    system: str

    def close(self):
        for channel in self.channels:
            channel.close()


def existing_suite(target='rust', records=None, system=None):
    """既存の 63 件の名前を固定の一覧で照らし、16 件を L と R に分けて集める。system は OS の分岐
    ('linux' か 'darwin')。比較器の自己検査は Linux で 'darwin' を模擬する(Mac の成功には数えない)。"""
    system = system or SYSTEM
    linux = system == 'linux'
    check_partition()
    module = load(EXISTING, 'r4r_existing_' + target + '_' + system)
    loader = unittest.TestLoader()
    names = loader.getTestCaseNames(module.RestoreTests)
    if (len(names), hashlib.sha256('\n'.join(names).encode()).hexdigest()) != EXISTING_MANIFEST:
        raise AssertionError('既存ケースの固定一覧と一致しない')
    internal = sorted(set(names) - set(SIXTEEN) - set(SUPERVISION))
    if len(internal) != 46 or not set(SIXTEEN) <= set(names) or not set(SUPERVISION) <= set(names):
        raise AssertionError('既存ケースの区分が 16・1・46 にならない')
    records = records if records is not None else {}
    work = Path(tempfile.mkdtemp(prefix='r4r-existing-'))
    shim, launches = write_shim(work, 'legacy' if target == 'legacy' else str(FIXTURE), target)
    channels = []
    mac_cp = None
    if not linux:
        # GNU を名乗る --version だけに答え、コピーの要求には失敗で答える代用品(記録の受け手を保持する)。
        stub, channel = make_stub(work / 'mac-cp', mode='version-only')
        channels.append(channel)
        mac_cp = str(stub)

    class Base(module.RestoreTests):
        side = None
        helpers = ()

        def setUp(self):
            super().setUp()
            self.records = records.setdefault((self.side, self._testMethodName), [])
            self.addCleanup(lambda: self.records.append(('end', end_state(self))))
            patches = [mock.patch.object(module, 'subprocess', Recorder(self.records, self.helpers, mac_cp))]
            if mac_cp is not None:
                patches.append(mock.patch.object(module, 'CP', mac_cp))
            for patcher in patches:
                patcher.start()
                self.addCleanup(patcher.stop)

    class Legacy(Base):
        side = 'L'
        helpers = (HELPER,)

    class Rust(Base):
        side = 'R'
        helpers = (shim,)

        def setUp(self):
            self.clock = Clock()
            self.pair_base = Path(tempfile.mkdtemp(prefix='r4r-pair-'))
            self.addCleanup(shutil.rmtree, self.pair_base, True)
            super().setUp()
            before = len(launches.read_text().splitlines())
            self.addCleanup(lambda: self.records.append(
                ('launches', len(launches.read_text().splitlines()) - before)))
            for patcher in (mock.patch.object(module, 'HELPER', shim), mock.patch.object(module, 'time', self.clock)):
                patcher.start()
                self.addCleanup(patcher.stop)

        def pair(self, argv, *, cli):
            started = time.monotonic()
            sc = Scenario(base=self.pair_base, root=Path(self.root), args=list(argv), entity=False,
                          source=os.fspath(self.source), destination=os.fspath(self.destination))
            backup = sc.base / 'backup'
            snapshot(sc.root, backup)
            old = run_side(sc, True)
            restore_layout(sc.root, backup)
            self.clock.offset += time.monotonic() - started
            new_started = time.monotonic()
            result = launch([sys.executable, '-B', str(shim), *argv], timeout=sc.timeout)
            new = observe(sc, result, {}, new_started)
            # 追加の境界と同じ関数で比べる(__CF_USER_TEXT_ENCODING を除く条件も同じ。環境を受け継ぐ起動
            # なので、run_side が起動の直前の os.environ で判定する)。この組は偽の cp を観測しない(log が
            # 無い)ので、cp の記録は両方 0 件で、除く項目も無い。
            compare_observed(old, new)
            self.records.append(('pair', old.result.returncode, normalize(old.result.stdout)))
            if not cli and new.result.stderr:
                raise ComparisonError('Rust の stderr が空でない')
            if cli:
                return subprocess.CompletedProcess(argv, new.result.returncode, new.result.stdout, new.result.stderr)
            return new.result.returncode, new.result.stdout

        def cli(self, argv=None):
            return self.pair(argv or self.argv(), cli=True)

        def operation(self, **overrides):
            return Deferred(self, self.argv(**overrides))

    if linux:
        legacy_names = list(SIXTEEN)
        rust_names = list(SIXTEEN)
    else:
        # ACL の件も流し、既存の省略(setfacl・getfacl が無い)で省かれることを確かめる。
        legacy_names = list(MAC_APPLY) + [ACL]
        rust_names = list(MAC_APPLY) + list(MAC_RESULT_ONLY) + [ACL]
        for name in MAC_RESULT_ONLY:
            setattr(Rust, name, result_only(getattr(Rust, name)))
    suite = unittest.TestSuite()
    suite.addTests(Legacy(name) for name in legacy_names)
    suite.addTests(Rust(name) for name in rust_names)
    if linux:
        suite.addTest(module.RestoreTests(SUPERVISION[0]))
    return Existing(suite, launches, records, rust_names, channels, system)


def result_only(original):
    """Mac の「結果だけを比べる件」。既存の検査(AssertionError)は subTest ごとに捨て、旧との比較の
    失敗(ComparisonError)は通す。旧との比較が 1 回以上あったことを照らす。"""
    def wrapped(self):
        records = self.records

        @contextlib.contextmanager
        def lenient(msg=None, **params):
            try:
                yield
            except ComparisonError:
                raise
            except AssertionError:
                records.append(('ignored', 'subtest'))
        self.subTest = lenient
        try:
            original(self)
        except ComparisonError:
            raise
        except AssertionError:
            records.append(('ignored', 'body'))
        if not any(item[0] == 'pair' for item in records):
            raise AssertionError(f'旧との比較が 1 回も無い: {self._testMethodName}')
    wrapped.__name__ = original.__name__
    return wrapped


def compare_existing(records, launches, rust_names, result, system=None):
    """L と R の記録を突き合わせる。R の各件で、入口の起動の回数が、比べた組の数と直接起動の記録の
    数の和と等しいことを照らす(比べずに起動した回と、入口が起動されなかった件を失敗にする)。"""
    system = system or SYSTEM
    skipped = {}
    for test, reason in result.skipped:
        skipped.setdefault(test._testMethodName, []).append((type(test).__name__, reason))
    counts = launches.read_text().splitlines()
    if not counts:
        raise AssertionError('Rust の入口が 1 度も起動されなかった')
    for name in rust_names:
        if name in skipped:
            continue
        rust = records.get(('R', name), [])
        launched = [item[1] for item in rust if item[0] == 'launches']
        pairs = sum(1 for item in rust if item[0] == 'pair')
        direct = sum(1 for item in rust if item[0] in ('run', 'popen', 'runner'))
        if len(launched) != 1 or launched[0] < 1:
            raise AssertionError(f'R で入口が起動されなかった: {name}')
        if launched[0] != pairs + direct:
            raise AssertionError(f'R の入口の起動の回数が、比べた組と直接起動の記録の和と違う: {name}: '
                                 f'{launched[0]} != {pairs} + {direct}')
    if system != 'linux':
        reasons = [reason for _, reason in skipped.get(ACL, [])]
        if len(reasons) != 2 or any(reason not in ACL_REASONS for reason in reasons) or len(set(reasons)) != 1:
            raise AssertionError('Mac で ACL の件が既存の省略で省かれなかった。この件を「結果だけを比べる件」へ'
                                 '移し、件数の固定を直す')
    for name in DIRECT:
        if system != 'linux' and name in MAC_SKIP:
            continue
        legacy = [item for item in records.get(('L', name), []) if item[0] not in ('pair', 'launches', 'ignored')]
        rust = [item for item in records.get(('R', name), []) if item[0] not in ('pair', 'launches', 'ignored')]
        if legacy != rust:
            raise ComparisonError(f'直接起動の回の関係が L と R で違う: {name}: {legacy} / {rust}')
    return {name: items[0][1] for name, items in skipped.items()}


# ---- 追加の境界 ----

FIRST_LINE = b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n"


def unraisable(kind, number, text):
    return FIRST_LINE + f'{kind}: [Errno {number}] {text}\n'.encode()


def deep_parent(base, length=3995):
    """応答が B=4096 より長くなる親のパス(Linux の PATH_MAX 未満)。Mac の PATH_MAX(1024)より長いので、
    パスの全体を OS へ渡さず、1 段ずつ相対で作る(旧 helper と Rust も、親を 1 段ずつ相対で開く)。"""
    os.makedirs(base, exist_ok=True)
    path = str(base)
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        while len(path) < length:
            size = min(200, length - len(path) - 1)
            if size <= 0:
                break
            name = 'd' * size
            os.mkdir(name, dir_fd=fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            path += '/' + name
    finally:
        os.close(fd)
    return Path(path)


def reset_deep(parent):
    """deep_parent の親を mode 0700 にし、旧 helper が残した一時フォルダ(umask 0777 で mode 0。中は空)を
    消す。Mac の PATH_MAX より長いパスを全体で渡さないよう、/ から 1 段ずつ相対で開く。"""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in Path(parent).parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        os.fchmod(fd, 0o700)
        for name in os.listdir(fd):
            if name.startswith('.restore-taskmd-'):
                os.chmod(name, 0o700, dir_fd=fd)
                os.rmdir(name, dir_fd=fd)
    finally:
        os.close(fd)


class Contract(unittest.TestCase):
    """追加の境界。旧 helper と Rust を同じパスで順に流し、旧 helper の固定期待も確かめる。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='r4r-contract-'))
        self.addCleanup(lambda: (make_writable(self.base), shutil.rmtree(self.base, True)))
        self.root = self.base / 'root'
        self.root.mkdir()
        self.source = self.root / 'taskmd-body'
        self.source.write_bytes(b'baseline\x00\xff\n')
        self.source.chmod(0o640)
        self.destination = self.root / 'current'
        self.destination.write_bytes(b'current\x00contents\n')
        self.destination.chmod(0o600)
        self.cp = real_cp()
        self.log = None

    def stub(self, mode='gnu', hold=''):
        stub, self.log = make_stub(self.base / ('stub-' + mode), mode=mode, hold=hold)
        self.addCleanup(self.log.close)
        self.cp = str(stub)
        return stub

    def args(self, **overrides):
        values = dict(source=os.fspath(self.source), destination=os.fspath(self.destination),
                      expected_mode=format(stat.S_IMODE(self.source.stat().st_mode), 'o'),
                      expected_sha256=hashlib.sha256(self.source.read_bytes()).hexdigest(), cp=self.cp)
        values.update(overrides)
        result = ['restore']
        for key, value in values.items():
            result.extend(['--' + key.replace('_', '-'), value])
        return result

    def scenario(self, args=None, **options):
        options.setdefault('log', self.log)
        return Scenario(base=self.base, root=self.root, args=self.args() if args is None else args,
                        source=os.fspath(self.source), destination=os.fspath(self.destination), **options)

    def pair(self, args=None, *, code, reason=None, touched=None, temp=None, **options):
        sc = self.scenario(args, **options)
        old, new = run_pair(sc)
        fixed(old.result, code, reason, touched, temp)
        fixed(new.result, code, reason, touched, temp)
        if old.relations:
            # 成功では復元先が新しい実体になり、旧い中身はハードリンクと開いた fd に残る。
            expected = {'same-entity': code != 0 or touched not in ('replaced',), 'link-kept': True, 'fd-kept': True}
            if code == 0:
                expected['same-entity'] = False
            elif touched in ('replaced', 'unknown'):
                expected.pop('same-entity')
            for key, value in expected.items():
                if old.relations[key] != value:
                    raise AssertionError(f'実体の関係の固定期待と違う: {key}')
        return old, new

    def test_arguments(self):
        valid = self.args()
        cases = [[], ['restore'], ['other'] + valid[1:], valid + ['--extra', 'x']]
        for index in range(1, 11, 2):
            cases.append(valid[:index] + valid[index + 2:])
            empty = list(valid)
            empty[index + 1] = ''
            cases.append(empty)
        duplicate = list(valid)
        duplicate[9:11] = ['--source', valid[2]]
        cases.append(duplicate)
        for key, value in (('source', '/'), ('source', str(self.root) + '//taskmd-body'),
                           ('destination', str(self.destination) + '/'), ('source', str(self.root) + '/./taskmd-body'),
                           ('source', str(self.root) + '/../root/taskmd-body'), ('cp', 'cp'),
                           ('expected_mode', '12345'), ('expected_mode', '8'), ('expected_mode', '9'),
                           ('expected_sha256', 'A' * 64), ('expected_sha256', '0' * 63), ('expected_sha256', '0' * 65)):
            cases.append(self.args(**{key: value}))
        for args in cases:
            with self.subTest(args=args):
                old, _ = self.pair(args, code=33, reason='internal-argument', touched='none', temp='none')
                self.assertEqual(old.result.stdout, b'RESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=internal-argument\n')

    def test_non_utf8_parent_and_name(self):
        parent = Path(os.fsdecode(os.fsencode(self.root) + b'/\xff\n"\\dir'))
        parent.mkdir()
        self.destination = Path(os.fsdecode(os.fsencode(parent) + b'/file-\xfe'))
        self.destination.write_bytes(b'old')
        self.pair(code=0, touched='replaced', temp='removed')

    def test_cp_resolution(self):
        cp = real_cp()
        missing_dir = '/nonexistent-r4r/..' + cp
        through_file = os.path.join(str(self.source), '..', os.path.relpath(cp, str(self.root)))
        for value, code in ((str(self.base / 'missing-cp'), 33), (str(self.base), 33),
                            (missing_dir, 0), (through_file, 0)):
            with self.subTest(cp=value):
                self.destination.write_bytes(b'current\x00contents\n')
                self.pair(self.args(cp=value), code=code, reason=None if code == 0 else 'runtime-unavailable',
                          touched='replaced' if code == 0 else 'none', temp='removed' if code == 0 else 'none')
        plain = self.base / 'not-executable'
        shutil.copyfile(cp, plain)
        plain.chmod(0o644)
        self.pair(self.args(cp=str(plain)), code=33, reason='runtime-unavailable', touched='none', temp='none')

    def chain(self, count, target, name):
        folder = self.base / name
        folder.mkdir()
        previous = target
        for index in range(1, count + 1):
            link = folder / f'l{index}'
            link.symlink_to(previous)
            previous = str(link)
        return previous

    def test_link_chains_and_nesting_depth(self):
        cp = real_cp()
        cases = [(41, cp, 0), (45, cp, 0), (991, cp, 0), (992, cp, 33)]
        if os.path.islink('/bin') and os.path.exists('/bin/cp'):
            cases += [(990, '/bin/cp', 0), (991, '/bin/cp', 33)]
        for number, (count, target, code) in enumerate(cases):
            with self.subTest(count=count, target=target):
                self.destination.write_bytes(b'current\x00contents\n')
                value = self.chain(count, target, f'chain-{number}')
                self.pair(self.args(cp=value), code=code, reason=None if code == 0 else 'runtime-unavailable',
                          touched='replaced' if code == 0 else 'none', temp='removed' if code == 0 else 'none')
        loop = self.base / 'loop'
        loop.mkdir()
        (loop / 'a').symlink_to(loop / 'b')
        (loop / 'b').symlink_to(loop / 'a')
        self.pair(self.args(cp=str(loop / 'a')), code=33, reason='runtime-unavailable', touched='none', temp='none')

    def test_version_answers(self):
        # Mac は --version が通った後、本物の /dev/fd の形でコピーが止まる(設計の推測。CI で確かめる)。
        accepted = (0, None, 'replaced', 'removed') if LINUX else (33, 'fd-copy-unavailable', 'none', 'removed')
        refused = (33, 'runtime-unavailable', 'none', 'none')
        for mode, expected in (('version-exit1', refused), ('version-8192', accepted), ('version-8193', refused)):
            with self.subTest(mode=mode):
                self.destination.write_bytes(b'current\x00contents\n')
                self.stub(mode)
                code, reason, touched, temp = expected
                self.pair(code=code, reason=reason, touched=touched, temp=temp)

    def test_version_ignoring_term_is_killed_after_full_stop_time(self):
        self.stub('version-term-ignore')
        old, new = self.pair(code=33, reason='runtime-unavailable', touched='none', temp='none', timeout=60)
        for observed in (old, new):
            # --version の 5 秒の後、TERM から 5 秒待って KILL する(短縮しない)。
            self.assertGreaterEqual(observed.elapsed, 9.5)
            self.assertLess(observed.elapsed, 20)

    def test_cp_deleting_itself_after_version(self):
        stub = self.stub('version-self-delete')
        backup = self.base / 'stub-backup'
        shutil.copy2(stub, backup)

        def lock():
            if not stub.exists():
                shutil.copy2(backup, stub)
        self.pair(code=33, reason='runtime-unavailable', touched='none', temp='removed', lock=lock)

    def test_ignored_sigchld_with_copy_child_launch_failure(self):
        # SIGCHLD を無視した起動で、コピーの子の起動が失敗する(--version に答えた後に自分を消す cp)。
        stub = self.stub('version-self-delete')
        backup = self.base / 'stub-backup'
        shutil.copy2(stub, backup)

        def lock():
            if not stub.exists():
                shutil.copy2(backup, stub)
        self.pair(code=33, reason='runtime-unavailable', touched='none', temp='removed', lock=lock,
                  preexec=lambda: signal.signal(signal.SIGCHLD, signal.SIG_IGN))

    def test_cp_without_shebang_is_not_run_by_sh(self):
        # シバンの無い実行可能ファイルは execv で ENOEXEC になる(execvp のように /bin/sh で動かさない)。
        bare = self.base / 'bare-cp'
        bare.write_text('echo "cp (GNU coreutils) fixture"\n')
        bare.chmod(0o755)
        for ignore in (False, True):
            with self.subTest(ignore_sigchld=ignore):
                def preexec(ignore=ignore):
                    if ignore:
                        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
                self.pair(self.args(cp=str(bare)), code=33, reason='runtime-unavailable', touched='none',
                          temp='none', preexec=preexec)

    def test_files(self):
        other = self.base / 'root' / 'other'
        other.mkdir()
        self.destination = other / 'current'
        self.destination.write_bytes(b'x')
        self.pair(code=0, touched='replaced', temp='removed')
        self.destination.unlink()
        self.pair(code=0, touched='copied', temp='removed')
        self.destination.unlink()
        self.source.write_bytes(b'')
        self.pair(code=0, touched='copied', temp='removed')
        for mode in (0o4755, 0o1777):
            with self.subTest(mode=oct(mode)):
                self.destination.unlink()
                self.source.write_bytes(b'mode\n')
                self.source.chmod(mode)
                self.pair(code=0, touched='copied', temp='removed')

    def test_unreadable_source_and_unwritable_parent(self):
        self.source.chmod(0o600)
        args = self.args(expected_mode='0')
        self.pair(args, code=33, reason='taskmd-body', touched='none', temp='none',
                  lock=lambda: self.source.chmod(0), unlock=lambda: self.source.chmod(0o600))
        self.pair(code=33, reason='prepare-failed', touched='none', temp='none',
                  lock=lambda: self.root.chmod(0o500), unlock=lambda: self.root.chmod(0o700))

    def test_umask_0777_prepare_failed(self):
        old, _ = self.pair(code=33, reason='prepare-failed', touched='none', temp='retained',
                           preexec=lambda: os.umask(0o777))
        self.assertIn(b'TEMP_PATH=', old.result.stdout)

    def test_file_size_limits(self):
        hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
        self.pair(code=33, reason='runtime-unavailable', touched='none', temp='removed',
                  preexec=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (0, hard)))
        self.source.write_bytes(b'S' * 64)
        self.pair(code=33, reason='runtime-unavailable', touched='none', temp='removed',
                  preexec=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (32, 32)))

    def test_ignored_sigchld_is_echild_success(self):
        self.stub('gnu')
        self.pair(code=0, touched='replaced', temp='removed',
                  preexec=lambda: signal.signal(signal.SIGCHLD, signal.SIG_IGN))

    def test_copy_signals_including_hup_ignored_by_parent(self):
        for number, ignore_hup in ((signal.SIGTERM, False), (signal.SIGINT, False), (signal.SIGHUP, False),
                                   (signal.SIGHUP, True)):
            with self.subTest(number=number, ignore_hup=ignore_hup):
                self.stub('copy-hold', hold='/taskmd-body')
                log = self.log

                def during(process, number=number, log=log):
                    log.wait_held()
                    process.send_signal(number)

                def preexec(ignore=ignore_hup):
                    if ignore:
                        signal.signal(signal.SIGHUP, signal.SIG_IGN)
                self.pair(code=20, reason='interrupted', touched='none', temp='removed', during=during,
                          preexec=preexec)

    def test_inherited_and_high_fds_are_not_passed(self):
        self.stub('gnu')
        extra = os.open('/dev/null', os.O_RDONLY)
        os.set_inheritable(extra, True)
        self.addCleanup(os.close, extra)
        old, _ = self.pair(code=0, touched='replaced', temp='removed', pass_fds=(extra,))
        hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]

        def high():
            os.dup2(extra, 900, inheritable=True)
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
        self.destination.write_bytes(b'current\x00contents\n')
        old2, _ = self.pair(code=0, touched='replaced', temp='removed', preexec=high, close_fds=False)
        for observed in (old, old2):
            for record in observed.stub:
                self.assertEqual(sorted(record['fds']), sorted(['0', '1', '2'] + (
                    ['input', 'output'] if record['args'] != ['--version'] else [])))
                self.assertEqual(record['fds']['0'][2], '/dev/null')
                self.assertEqual(record['fds']['2'][2], '/dev/null')
                self.assertEqual(record['fds']['0'][0] & os.O_ACCMODE, os.O_RDWR)

    def test_version_pipe_select_limit(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 1200:
            raise AssertionError('1024 前後の fd を開くには RLIMIT_NOFILE の上限が足りない')
        for last, code in ((1021, 0), (1022, 33)):
            with self.subTest(last=last):
                self.destination.write_bytes(b'current\x00contents\n')

                def fill(last=last):
                    resource.setrlimit(resource.RLIMIT_NOFILE, (2048, hard))
                    null = os.open('/dev/null', os.O_RDONLY)
                    os.set_inheritable(null, True)
                    for fd in range(3, last + 1):
                        if fd != null:
                            os.dup2(null, fd, inheritable=True)
                    if null > last:
                        os.close(null)
                self.pair(code=code, reason=None if code == 0 else 'runtime-unavailable',
                          touched='replaced' if code == 0 else 'none', temp='removed' if code == 0 else 'none',
                          preexec=fill, close_fds=False)

    def test_signal_mask_and_dispositions_reach_cp(self):
        self.stub('gnu')

        def preexec():
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
            signal.signal(signal.SIGUSR2, signal.SIG_IGN)
        old, _ = self.pair(code=0, touched='replaced', temp='removed', preexec=preexec)
        for record in old.stub:
            blocked, ignored = record['blocked'], record['ignored']
            self.assertEqual(record['argv0'], self.cp)
            self.assertEqual(record['caught'], 0)
            self.assertTrue(blocked & (1 << (signal.SIGUSR1 - 1)))
            self.assertTrue(ignored & (1 << (signal.SIGUSR2 - 1)))
            for number in (signal.SIGPIPE, signal.SIGXFSZ, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                self.assertFalse(ignored & (1 << (number - 1)), number)
            if record['args'] != ['--version']:
                size = len(b'baseline\x00\xff\n')
                self.assertIn(record['fsize'], ([9, 9], [size, size]))
                self.assertEqual(record['args'][:3], ['-aL', '--reflink=never', '--'])

    def test_environment_lc_ctype(self):
        self.stub('gnu')
        for env, expected in (({}, b'C.UTF-8'), ({'LANG': 'C'}, b'C.UTF-8'), ({'LC_ALL': 'C'}, None),
                              ({'LC_ALL': ''}, b'C.UTF-8'), ({'LC_ALL': 'C.UTF-8'}, None),
                              ({'LC_CTYPE': 'POSIX'}, b'C.UTF-8'),
                              ({'Z': '1', 'LC_CTYPE': 'POSIX', 'A': '2'}, b'C.UTF-8')):
            with self.subTest(env=env):
                self.destination.write_bytes(b'current\x00contents\n')
                old, _ = self.pair(code=0, touched='replaced', temp='removed', env=env)
                for record in old.stub:
                    entries = bytes.fromhex(record['env']).split(b'\0')
                    values = [e.split(b'=', 1)[1] for e in entries if e.startswith(b'LC_CTYPE=')]
                    self.assertEqual(values, [expected] if expected is not None else [])
                    if 'Z' in env:
                        self.assertEqual([e.split(b'=')[0] for e in entries if e], [b'Z', b'LC_CTYPE', b'A'])

    def test_environment_lc_ctype_recorded_on_mac(self):
        """Mac は旧 Python と同じ libc で決めるので、--version の子が受け取る環境を旧と Rust で比べ
        (旧が書き換えなかったときは Rust も書き換えないことを含む)、値を記録する。"""
        self.stub('gnu')
        for env in ({}, {'LANG': 'C'}, {'LC_CTYPE': 'POSIX'}, {'LANG': 'POSIX'}, {'LC_ALL': 'C'},
                    {'Z': '1', 'LC_CTYPE': 'POSIX', 'A': '2'}):
            with self.subTest(env=env):
                self.destination.write_bytes(b'current\x00contents\n')

                def record(old, new, env=env):
                    # 比べる前に、旧と Rust の両方の値を出す(比べて落ちても記録が残る)。
                    def values(observed):
                        return [[e.split(b'=', 1)[1] for e in env_entries(item['env']) if e.startswith(b'LC_CTYPE=')]
                                for item in observed.stub]
                    print(f'R4R: 記録(Mac の LC_CTYPE): env={env} → 旧 {values(old)} / Rust {values(new)}', flush=True)
                run_pair(self.scenario(env=env), before_compare=record)

    def test_unreadable_locale_name_is_legacy_only_fixed_expectation(self):
        """読み込めない地域設定の名前は、glibc が読めるかを musl の Rust は知れないので、R4R では Rust と
        比べず(S3I-A #474 で決める)、旧 helper だけの固定期待にする。"""
        self.stub('gnu')
        sc = self.scenario(env={'LANG': 'xx_YY.UTF-8'})
        old = run_side(sc, True)
        fixed(old.result, 0, None, 'replaced', 'removed')
        self.assertTrue(old.stub)
        for record in old.stub:
            entries = [e for e in bytes.fromhex(record['env']).split(b'\0') if e]
            self.assertEqual(entries, [b'LANG=xx_YY.UTF-8', b'LC_CTYPE=C.UTF-8'])
        print('R4R: 記録(読み込めない名前・旧 helper だけ): LANG=xx_YY.UTF-8 → LC_CTYPE=C.UTF-8')

    def test_output_failures(self):
        enospc = unraisable('OSError', errno.ENOSPC, 'No space left on device')
        epipe = unraisable('BrokenPipeError', errno.EPIPE, 'Broken pipe')
        efbig = unraisable('OSError', errno.EFBIG, 'File too large')
        for kind, expected in (('full', enospc), ('closed-pipe', epipe)):
            with self.subTest(kind=kind, response='short'):
                self.destination.write_bytes(b'current\x00contents\n')
                sc = self.scenario(stdout=kind)
                old, new = run_pair(sc)
                self.assertEqual((old.result.returncode, old.result.stderr), (120, expected))
                self.assertEqual(old.files['destination']['sha256'], old.files['source']['sha256'])
                arg = self.scenario(['restore'], stdout=kind, entity=False)
                old, new = run_pair(arg)
                self.assertEqual((old.result.returncode, old.result.stderr), (120, expected))
        parent = deep_parent(self.root / 'deep')
        self.destination = parent / 'current'
        self.destination.write_bytes(b'x')
        for kind in ('full', 'closed-pipe'):
            with self.subTest(kind=kind, response='long'):
                sc = self.scenario(stdout=kind, preexec=lambda: os.umask(0o777), entity=False)
                old, new = run_pair(sc)
                self.assertEqual((old.result.returncode, old.result.stderr), (33, b''))
                make_writable(parent)
                for child in parent.glob('.restore-taskmd-*'):
                    child.rmdir()
        self.destination = self.root / 'current'
        hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
        sc = self.scenario(stdout='file:limited',
                           preexec=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (40, hard)))
        old, new = run_pair(sc)
        self.assertEqual((old.result.returncode, old.result.stderr, len(old.result.stdout)), (120, efbig, 40))

    def test_full_pipe_term_then_reader_closed(self):
        expected = unraisable('BrokenPipeError', errno.EPIPE, 'Broken pipe')
        import fcntl
        for args, code, stderr in ((['restore'], -signal.SIGTERM, b''), (None, 120, expected)):
            observed = []
            for legacy in (True, False):
                self.destination.write_bytes(b'current\x00contents\n')
                argv = args or self.args()
                read, write = os.pipe()
                capacity = fcntl.fcntl(write, 1032) if LINUX else 65536
                os.write(write, b'x' * capacity)
                process = subprocess.Popen(legacy_argv(argv) if legacy else native_argv(argv),
                                           stdin=subprocess.DEVNULL, stdout=write, stderr=subprocess.PIPE)
                os.close(write)
                try:
                    if args is None:
                        deadline = time.monotonic() + 30
                        while self.destination.read_bytes() != self.source.read_bytes():
                            if time.monotonic() >= deadline:
                                raise AssertionError('復元を待てなかった')
                            time.sleep(.01)
                    time.sleep(1)
                    process.send_signal(signal.SIGTERM)
                    time.sleep(.2)
                    os.close(read)
                    read = None
                    _, err = process.communicate(timeout=30)
                finally:
                    if read is not None:
                        os.close(read)
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                observed.append((process.returncode, err))
            self.assertEqual(observed[0], (code, stderr))
            self.assertEqual(observed[0], observed[1])


class StopOnMac(Contract):
    """Mac は停止の比較だけ。"""

    def test_mac_stops(self):
        self.pair(code=33, reason='runtime-unavailable', touched='none', temp='none')
        self.stub('gnu')
        self.pair(code=33, reason='fd-copy-unavailable', touched='none', temp='removed')
        self.stub('version-only')
        self.pair(code=33, reason='fd-copy-unavailable', touched='none', temp='removed', env={})


# ---- 旧 helper の出力先を差し替える実験(固定期待) ----

FAULT_WRAPPER = r'''import io, os, runpy, sys
plan = sys.argv[1].split(',') if sys.argv[1] else []
size = int(sys.argv[2])
helper = sys.argv[3]
class Fault(io.RawIOBase):
    name = '<stdout>'
    def __init__(self):
        self.step = 0
    def writable(self):
        return True
    def write(self, data, _write=os.write, _kill=os.kill, _getpid=os.getpid, _plan=plan):
        step = self.step
        self.step += 1
        action = _plan[step] if step < len(_plan) else 'OK'
        if action == 'EIO':
            raise OSError(5, 'Input/output error')
        if action == 'TERM':
            _kill(_getpid(), 15)
        if action.isdigit():
            return _write(1, bytes(data)[:int(action)])
        return _write(1, data)
stream = io.TextIOWrapper(io.BufferedWriter(Fault(), buffer_size=size), encoding='utf-8')
stream.mode = 'w'
sys.stdout = sys.__stdout__ = stream
del stream
sys.argv = [helper] + sys.argv[4:]
runpy.run_path(helper, run_name='__main__')
'''

# (計画, 期待の終了値, stdout の長さ(応答の全体なら None), stderr の最後の行)
FLUSH_EXPECTATIONS = (
    ('EIO', 33, None, None),
    ('EIO,EIO', 33, None, None),
    ('EIO,EIO,EIO', 120, None, b'OSError: [Errno 5] Input/output error'),
    ('EIO,EIO,EIO,EIO', 120, None, b'OSError: [Errno 5] Input/output error'),
    ('EIO,EIO,EIO,EIO,EIO', 120, 0, b'OSError: [Errno 5] Input/output error'),
    ('EIO,EIO,EIO,TERM', -signal.SIGTERM, 0, b'OSError: [Errno 5] Input/output error'),
)


class FlushExperiment(unittest.TestCase):
    """旧 helper の出力先を差し替え、終了時の flush の順と回数を固定期待で確かめる。"""

    def run_wrapped(self, plan, size, args, preexec=None):
        with tempfile.TemporaryDirectory() as temp:
            wrapper = Path(temp) / 'wrap.py'
            wrapper.write_text(FAULT_WRAPPER)
            return launch([sys.executable, '-I', '-B', str(wrapper), plan, str(size), str(HELPER), *args],
                          preexec=preexec, timeout=60)

    def check(self, result, code, length, last, response):
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, code)
        self.assertEqual(result.stdout, response if length is None else response[:length])
        if last is None:
            self.assertEqual(result.stderr, b'')
        else:
            lines = result.stderr.splitlines()
            self.assertEqual(lines[0] + b'\n', FIRST_LINE)
            self.assertEqual(lines[-1], last)
            self.assertEqual(result.stderr.count(b'Exception ignored in:'), 1)

    def test_short_response_flush_count(self):
        response = b'RESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=internal-argument\n'
        for plan, code, length, last in FLUSH_EXPECTATIONS:
            with self.subTest(plan=plan):
                self.check(self.run_wrapped(plan, 4096, ['restore']), code, length, last, response)

    def test_valid_arguments_term_during_close_flush_ends_by_term_after_two_lines(self):
        # 正しい引数の経路では TERM・INT・HUP に処理があるので、(3) で既定へ戻した後の (4) の途中の
        # TERM で終わる(2 行は先に出る)。
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'taskmd-body'
            source.write_bytes(b'body\n')
            source.chmod(0o640)
            destination = base / 'current'
            destination.write_bytes(b'old\n')
            args = ['restore', '--source', str(source), '--destination', str(destination),
                    '--expected-mode', '640', '--expected-sha256', hashlib.sha256(b'body\n').hexdigest(),
                    '--cp', real_cp()]
            result = self.run_wrapped('EIO,EIO,EIO,TERM', 4096, args)
            self.check(result, -signal.SIGTERM, 0, b'OSError: [Errno 5] Input/output error', b'')
            if LINUX:
                self.assertEqual(destination.read_bytes(), b'body\n')

    def gnu_cp(self, base):
        """--version に GNU と答える cp。Linux は本物の GNU cp。Mac の cp は BSD で --version を受けず、
        旧 helper が一時フォルダを作る前に runtime-unavailable で止まって応答が B より長くならないので、
        --version だけに答える観測器を使う(この実験はコピーまで進まない)。"""
        if LINUX:
            return real_cp()
        stub, channel = make_stub(base / 'stub', mode='version-only')
        self.addCleanup(channel.close)
        return str(stub)

    def test_partial_direct_write_then_two_failures_is_33(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'taskmd-body'
            source.write_bytes(b'body\n')
            source.chmod(0o640)
            parent = deep_parent(base / 'deep')
            args = ['restore', '--source', str(source), '--destination', str(parent / 'current'),
                    '--expected-mode', '640', '--expected-sha256', hashlib.sha256(b'body\n').hexdigest(),
                    '--cp', self.gnu_cp(base)]
            plain = launch(legacy_argv(args), preexec=lambda: os.umask(0o777))
            reset_deep(parent)
            self.assertIn(b'REASON=prepare-failed\n', plain.stdout)
            self.assertGreater(len(plain.stdout), 4096)
            result = self.run_wrapped('1000,EIO,EIO', 4096, args, preexec=lambda: os.umask(0o777))
            reset_deep(parent)
            self.assertEqual(result.returncode, 33)
            self.assertEqual(normalize(result.stdout), normalize(plain.stdout))
            self.assertEqual(result.stderr, b'')


# ---- 対応表 ----

def check_map(listed, ignored, system, data=None):
    """46 件が対応表に全件あり、その OS で流す対応先が --list にあり、--list --ignored に無いことを照らす。"""
    data = data if data is not None else json.loads(MAP.read_text())
    module = load(EXISTING, 'r4r_map_names')
    names = unittest.TestLoader().getTestCaseNames(module.RestoreTests)
    internal = sorted(set(names) - set(SIXTEEN) - set(SUPERVISION))
    entries = data.get('tests', {})
    if sorted(entries) != internal or len(internal) != 46:
        raise AssertionError('対応表の旧テスト名が 46 件と一致しない')
    unreproducible = sorted(name for name, entry in entries.items() if entry.get('unreproducible'))
    if (len(unreproducible), hashlib.sha256('\n'.join(unreproducible).encode()).hexdigest()) != UNREPRODUCIBLE_MANIFEST:
        raise AssertionError('再現できない件の固定の一覧と一致しない')
    tests = {line.split(': test')[0] for line in listed.splitlines() if line.endswith(': test')}
    ignored_tests = {line.split(': test')[0] for line in ignored.splitlines() if line.endswith(': test')}
    if ignored_tests:
        raise AssertionError('#[ignore] の検査がある')
    used = 0
    for name, entry in entries.items():
        if entry.get('unreproducible'):
            continue
        systems = entry.get('os')
        if not systems or any(value not in ('linux', 'darwin') for value in systems) or 'linux' not in systems:
            raise AssertionError(f'対応表の OS の印が不正: {name}')
        rust = entry.get('rust')
        if not rust:
            raise AssertionError(f'対応先が無い: {name}')
        if system in systems:
            if rust not in tests:
                raise AssertionError(f'対応先が --list に無い: {name} → {rust}')
            used += 1
    return used


EXCEPTION_LINE = re.compile(r'[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Expired)\b')


def reason_of(trace):
    """traceback の最後の例外の行(と続く 2 行まで)。"""
    lines = [line for line in trace.rstrip().splitlines() if line.strip()]
    for index in range(len(lines) - 1, -1, -1):
        if EXCEPTION_LINE.match(lines[index]):
            return ' / '.join(lines[index:index + 3])[:500]
    return lines[-1][:500] if lines else ''


def suite_problems(result, count, unexpected, limit=40):
    """落ちた件の名前と理由。出力を捨てる自己検査でも、次の CI で原因が分かるよう、不合格の知らせに残す。"""
    lines = [f'- 失敗: {test.id()}: {reason_of(trace)}' for test, trace in result.failures]
    lines += [f'- エラー: {test.id()}: {reason_of(trace)}' for test, trace in result.errors]
    lines += [f'- 省略: {test.id()}: {reason}' for test, reason in unexpected]
    lines += [f'- 想定外の成功: {test.id()}' for test in result.unexpectedSuccesses]
    if result.testsRun != count:
        lines.append(f'- 流した件数 {result.testsRun} / 集めた件数 {count}')
    if len(lines) > limit:
        lines = lines[:limit] + [f'- ほか {len(lines) - limit} 件']
    return '\n'.join(lines)


def run_suite(suite, *, allow_skips=()):
    count = suite.countTestCases()
    if count == 0:
        raise AssertionError('検査ケースが0件')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    unexpected = [(test, reason) for test, reason in result.skipped if test._testMethodName not in allow_skips]
    if not result.wasSuccessful() or result.testsRun != count or unexpected:
        raise AssertionError('未実行・失敗・省略したケースがある\n' + suite_problems(result, count, unexpected))
    return result


@contextlib.contextmanager
def subreaper():
    libc = None
    old = ctypes.c_int()
    if LINUX:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(37, ctypes.byref(old), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), 'R4R test subreaper')
    try:
        yield
    finally:
        if libc is not None and libc.prctl(36, old.value, 0, 0, 0):
            raise OSError(ctypes.get_errno(), 'restore R4R test subreaper')


LINUX_CONTRACT = (
    'test_arguments', 'test_non_utf8_parent_and_name', 'test_cp_resolution', 'test_link_chains_and_nesting_depth',
    'test_version_answers', 'test_version_ignoring_term_is_killed_after_full_stop_time',
    'test_cp_deleting_itself_after_version', 'test_ignored_sigchld_with_copy_child_launch_failure',
    'test_cp_without_shebang_is_not_run_by_sh', 'test_files', 'test_unreadable_source_and_unwritable_parent',
    'test_umask_0777_prepare_failed', 'test_file_size_limits', 'test_ignored_sigchld_is_echild_success',
    'test_copy_signals_including_hup_ignored_by_parent', 'test_inherited_and_high_fds_are_not_passed',
    'test_version_pipe_select_limit', 'test_signal_mask_and_dispositions_reach_cp', 'test_environment_lc_ctype',
    'test_unreadable_locale_name_is_legacy_only_fixed_expectation',
    'test_output_failures', 'test_full_pipe_term_then_reader_closed',
)
MAC_CONTRACT = ('test_arguments', 'test_version_answers', 'test_version_ignoring_term_is_killed_after_full_stop_time',
                'test_cp_without_shebang_is_not_run_by_sh', 'test_environment_lc_ctype_recorded_on_mac')


def contract_suite():
    suite = unittest.TestSuite()
    if LINUX:
        suite.addTests(Contract(name) for name in LINUX_CONTRACT)
    else:
        suite.addTests(Contract(name) for name in MAC_CONTRACT)
        suite.addTest(StopOnMac('test_mac_stops'))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(FlushExperiment))
    return suite


def record_cf_environ(system=None):
    """Mac の実行の始めに、比較器の os.environ に __CF_USER_TEXT_ENCODING があるかを記録する(環境を
    受け継ぐ起動で、除く条件の「渡した環境に無い」を決める根拠)。Mac のほかでは何も出さない。"""
    if (system or SYSTEM) != 'darwin':
        return None
    value = os.environ.get(CF_ENCODING)
    line = (f'R4R: 記録(Mac の比較器の os.environ の {CF_ENCODING}): '
            + ('なし' if value is None else f'あり(値 {value!r})'))
    print(line, flush=True)
    return line


def record_eagain():
    """待たずに書く設定の空きの無いパイプ。旧 helper の結果だけを記録する(S3I-A の基準。Rust とは比べない)。"""
    if not LINUX:
        return None
    import fcntl
    read, write = os.pipe()
    try:
        os.set_blocking(write, False)
        capacity = fcntl.fcntl(write, 1032)
        os.write(write, b'x' * capacity)
        result = launch(legacy_argv(['restore']), stdout=write, timeout=30)
    finally:
        os.close(write)
        os.close(read)
    print(f'R4R: 記録(EAGAIN・旧 helper だけ): 終了値 {result.returncode} / stderr {result.stderr!r}')
    return result


def main(argv=None):
    global FIXTURE
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--fixture')
    group.add_argument('--map-list')
    parser.add_argument('--map-ignored')
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise ValueError('R4R比較はPython3.10系・非rootで実行してください')
    if args.map_list is not None:
        if not args.map_ignored:
            raise ValueError('--map-list には --map-ignored も指定してください')
        used = check_map(Path(args.map_list).read_text(), Path(args.map_ignored).read_text(), SYSTEM)
        print(f'R4R: 対応表 合格({SYSTEM} で {used} 件)')
        return 0
    FIXTURE = fixture_path(args.fixture)
    use_real_tempdir()
    record_cf_environ()
    problems = []

    def stage(name, action):
        """段を流す。落ちても後の段を省かずに流し、最後にまとめて不合格で終える(1 回の CI で多くの
        原因を見るため。比較器の自己検査が落ちた回の比較は、合格に数えない)。"""
        try:
            action()
        except Exception as error:  # noqa: BLE001 - 後の段を流してから、必ず不合格で終える。
            problems.append(name)
            if not isinstance(error, (AssertionError, ComparisonError)):
                traceback.print_exc()
            print(f'R4R: 不合格({name}): {type(error).__name__}: {error}', file=sys.stderr, flush=True)

    # 比較器自身の検査を先に流す。
    module = load(Path(__file__).with_name('test_r4r.py'), 'r4r_comparator_tests')
    module.subject = sys.modules[__name__]
    stage('比較器の自己検査', lambda: run_suite(unittest.defaultTestLoader.loadTestsFromModule(module)))
    with subreaper():
        # 比べ方が揺れないことの確かめ: 旧 helper どうしを同じ手順で比べ、全記録が一致すること。
        for target in ('legacy', 'rust'):
            stage(f'既存({target})', lambda target=target: run_existing(target))
        stage('追加の境界', lambda: run_suite(contract_suite()))
        stage('EAGAIN の記録', record_eagain)
    if problems:
        raise AssertionError('不合格の段: ' + '・'.join(problems) + '(各段の「R4R: 不合格(…)」の行を見る)')
    print('R4R: 合格')
    return 0


def run_existing(target):
    existing = existing_suite(target)
    try:
        result = run_suite(existing.suite, allow_skips=(ACL,))
        skipped = compare_existing(existing.records, existing.launches, existing.rust_names, result)
    finally:
        existing.close()
    launches = existing.launches
    sides = {}
    for test, reason in result.skipped:
        sides.setdefault(test._testMethodName, []).append((type(test).__name__, reason))
    for name, items in sides.items():
        if sorted(side for side, _ in items) != ['Legacy', 'Rust'] or len({r for _, r in items}) != 1:
            raise AssertionError(f'旧と Rust で省略の理由が違う: {name}')
        print(f'R4R: 省略({SYSTEM}・{target}): {name}: {items[0][1]}')
    print(f'R4R: 既存({target}): {result.testsRun} 件・省略 {len(skipped)} 件・入口の起動 '
          f'{len(launches.read_text().splitlines())} 回')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AssertionError, ComparisonError, ValueError, OSError) as error:
        print(f'R4R: 不合格: {error}', file=sys.stderr)
        sys.exit(1)
