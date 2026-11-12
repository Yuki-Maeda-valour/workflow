#!/usr/bin/env python3
"""R4C専用比較。旧 adopt-candidate.py と、明示した Rust 実行物を同じ種類の使い捨て入力で比べる。

旧版は変えない。比較は次の 3 つを必ず行う: (1) 旧版の実出力が固定期待に合う、(2) Rust 実行物の実出力が
同じ固定期待に合う、(3) 両側の終了値・出力・作業ツリーの記録が一致する。両側が同じ誤値でも、固定期待と違えば失敗にする。
実行物の指定が無い・不在・実行不能・旧版への接続(スクリプト・旧 Python・配る木の中)なら失敗にし、skip にしない。

    python3 -B native/ci/check_adopt_candidate.py --fixture <adopt_candidate_contract の絶対パス> \
        [--old-python <本家 CPython 3.10.11 の python3.10>]

旧 Python の標準ライブラリ(診断の呼出履歴に出る argparse.py など)は、本家 3.10.11 の SHA-256 と照らす。違えば失敗にする。
"""
import argparse
import ast
import hashlib
import importlib.util
import errno as errno_module
import json
import os
import re
import resource
import signal
from pathlib import Path
import stat
import subprocess
import sys
import contextlib
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from support_native import RunResult, run_command, snapshot_tree  # noqa: E402

LEGACY_SCRIPT = ROOT / 'plugins/dev-workflow/skills/create-task/scripts/adopt-candidate.py'
OLD_TEST = ROOT / 'scripts/test_adopt_candidate.py'
ARG_CASES = Path(__file__).with_name('adopt_candidate_fixtures') / 'arg_cases.json'

# 本家 CPython 3.10.11 の標準ライブラリの SHA-256。呼出履歴に出るファイルだけを載せる(R4C で届くのは argparse.py)。
# 機種ごとの追記は、各 CI 機種の 3.10.11 で採取した値を独立レビューしてから足す。
UPSTREAM_STDLIB_SHA256 = {
    'argparse.py': 'a238b700513237e10d26056e42c5b4e42d4961ea70e15ebeef4c61c76615f004',
    'pathlib.py': '936f14a3ecc4ac5e896e4d673666d76b25a9d581f3b4c8d8d424e830fda2a69b',
    're.py': '018b1a2c70e8ef537cc328d1026afdeca5839392ce47235a7b8d22e1b788cdc7',
}

# 旧 26 メソッドの観点。ID・旧テスト名・観測・この比較器の検査名(Contract のメソッド名。未実装は None)。
OLD_TEST_MAP = (
    ('A01', 'human_untracked_normal', '終了0、元なし、先通常ファイル', 'test_a01_untracked_normal'),
    ('A02', 'loop_marker_without_unattended_stops_unchanged', '終了2、stderr印名、元通常、先なし', 'test_a02_loop_marker'),
    ('A03', 'unattended_stops', '終了2、元あり', 'test_a03_unattended'),
    ('A04', 'digest_change_stops', '終了2、先なし', 'test_a04_digest_change'),
    ('A05', 'candidate_keys_normal_and_skip_attack', 'candidate-keys正常0、見送り1', None),
    ('A06', 'adoption_reuses_held_candidate_check_as_warnings', 'warnings source-count/key-count', 'test_a06_warnings_from_candidate_check'),
    ('A07', 'adoption_git_caller_uses_common_safe_prefix', '8指定をSAFE_GIT_PREFIXに含む', 'test_e06_git_calls'),
    ('A08', 'collision_all_states', '4状態の衝突終了2、第三者本文untouched', 'test_a08_collision_all_states'),
    ('A09', 'renamed_candidate_checks_candidate_state_collision', '別候補名衝突終了2', 'test_a09_renamed_collision'),
    ('A10', 'skip_and_malformed_skip', '正常/不正見送り各終了2', 'test_a10_skip_lines'),
    ('A11', 'marker_appearing_before_rename_stops', '直前印のValueError', 'test_e05_e12_injected_failures'),
    ('A12', 'tracked_normal_stages_rename', '終了0、index先あり元なし', 'test_a12_a14_tracked_adoption'),
    ('A13', 'tracked_unstaged_candidate_edit_keeps_held_index_blob', '作業本文は未stage込み、indexは追記前', 'test_a12_a14_tracked_adoption'),
    ('A14', 'tracked_partially_staged_candidate_edit_keeps_held_index_blob', '作業本文全追記、indexはstage部分だけ', 'test_a12_a14_tracked_adoption'),
    ('A15', 'tracked_replacement_before_git_mv_stops_with_staged_residue', '改名前差替えの残存', 'test_e05_e12_injected_failures'),
    ('A16', 'tracked_index_replacement_after_git_mv_stops_with_staged_residue', '改名後index差替えの残存', 'test_e05_e12_injected_failures'),
    ('A17', 'precheck_tamper_stops_tracked_rename', '終了2、stderr事前検査', 'test_a17_precheck_refusal'),
    ('A18', 'symlink_fifo_parent_refused', '末端symlink/FIFOの終了2', 'test_a18_symlink_and_fifo'),
    ('A19', 'change_while_reading_stops_before_rename', '読取り変更ValueError', 'test_e05_e12_injected_failures'),
    ('A20', 'forced_posix_fallback_normal', 'fallback到達、先通常', 'test_e05_e12_injected_failures'),
    ('A21', 'posix_fallback_collision_keeps_source', 'link前の状態衝突', 'test_e05_e12_injected_failures'),
    ('A22', 'posix_fallback_does_not_remove_destination_replaced_after_link', 'link直後差替えの保全', 'test_e05_e12_injected_failures'),
    ('A23', 'posix_fallback_unlink_failure_rolls_back_own_destination', '自分の先だけ戻す', 'test_e05_e12_injected_failures'),
    ('A24', 'posix_fallback_does_not_remove_replaced_destination', '差替え先の保全', 'test_e05_e12_injected_failures'),
    ('A25', 'posix_fallback_kill_between_link_and_unlink_leaves_both_names', '両名残存、inode同一', ('test_e05_e12_injected_failures', 'test_sigint_with_a_running_git_child')),
    ('A26', 'same_uid_can_remove_marker_documented_limit', '子が印を消すと終了0', 'test_e05_e12_injected_failures'),
)
# 追加群(旧テストに無い境界)。検査名が None のものは、Rust 実行物の完成後に CI の証拠から足す。
EXTRA_GROUPS = (
    ('E01', '引数と起動: 欠落・短縮・重複・未知・help・空と不正の名前・root外', 'test_e01_argument_cases'),
    ('E01b', '起動の失敗と環境変数: 標準の fd がディレクトリ・不正な PYTHONHASHSEED/PYTHONINTMAXSTRDIGITS・PYTHONOPTIMIZE', 'test_e01b_startup_environment'),
    ('E02', '親: symlink・不在・権限・..・差替え', 'test_e02_parent_and_root'),
    ('E03', '本文: 0・8MiB境界・特殊型・短読・成長・属性変化', ('test_e03_candidate_size', 'test_e05_e12_injected_failures')),
    ('E04', 'SHAと候補解析: 長さ・文字種・UTF-8・警告の順', 'test_e04_digest_shape'),
    ('E05', '衝突: dangling symlink・directory・stat失敗・確認後の新設', 'test_e05_e12_injected_failures'),
    ('E06', 'Git: argv順・4環境変数・GIT_*除去・保持fd cwd・stdin継承・時間切れ', ('test_e06_git_calls', 'test_e06_child_environment', 'test_e10_git_30_second_deadline')),
    ('E07', 'precheck: accept・成功時の出力の破棄・各非0・非UTF-8', 'test_e05_e12_injected_failures'),
    ('E08', '最終確認の前: precheck中の本文変更・同文inode差替え', 'test_e05_e12_injected_failures'),
    ('E09', 'index: 非0・0件/複数・区切り・mode/stage/path・OID 39/40〜64/65', 'test_e05_e12_injected_failures'),
    ('E10', 'git mv: 非0・時間切れ・遅い衝突・改名後の差替え', 'test_e05_e12_injected_failures'),
    ('E11', 'Linux改名: 実到達・ENOSYS/EINVAL/他errno・遅い衝突', 'test_e05_e12_injected_failures'),
    ('E12', 'link/unlink: EEXIST・直後stat失敗・rollback失敗・元削除後の不一致', 'test_e05_e12_injected_failures'),
    ('E13', '出口: JSON全欄の順・close失敗・診断とflush失敗・改名後の出力不能', ('test_e13_success_output_faults', 'test_e13b_large_json_in_print', 'test_e13c_terminal_stdout')),
    ('E14', '承認済みの差の個別期待(未知のcodec・ascii の help・strict の UTF-8 locale)', 'test_approved_differences'),
    ('E15', 'SIGINT・Git の実期限・子の環境の実測', ('test_sigint_with_a_running_git_child', 'test_e10_git_30_second_deadline', 'test_e06_child_environment')),
    ('E16', '8192 バイトを超える出力・端末の標準出力・捕捉されない例外の全文', ('test_e13b_large_json_in_print', 'test_e13c_terminal_stdout', 'test_uncaught_tracebacks')),
    ('E17', '順序を決めない SIGINT: link から改名後の照合までの間に実際の信号を送った結果の種類(旧版の種類に Rust の種類が入る)', 'test_e17_sigint_race'),
    ('E18', '長い文で標準エラーが書けない(ファイルの上限・/dev/full・pipe。標準エラーは line_buffering)', 'test_e18_long_stderr'),
    ('E13a', '出口(依存部品なしで流せる部分): help と引数の誤りの出力の故障(途中書込みを含む)', 'test_e13a_output_faults'),
)


# 基準の errno の文(2026-11-12 の共通決定)。Linux は CI の Ubuntu の glibc(C ロケール)、Mac は Python が使う OS の `strerror`。
# Rust の errno の表(diagnostics.rs)と同じ値であることは scripts/test_native_adopt_candidate.py が照合する。
GLIBC_ERRNO_REFERENCE = {
    1: 'Operation not permitted',
    2: 'No such file or directory',
    3: 'No such process',
    4: 'Interrupted system call',
    5: 'Input/output error',
    6: 'No such device or address',
    7: 'Argument list too long',
    8: 'Exec format error',
    9: 'Bad file descriptor',
    10: 'No child processes',
    11: 'Resource temporarily unavailable',
    12: 'Cannot allocate memory',
    13: 'Permission denied',
    14: 'Bad address',
    15: 'Block device required',
    16: 'Device or resource busy',
    17: 'File exists',
    18: 'Invalid cross-device link',
    19: 'No such device',
    20: 'Not a directory',
    21: 'Is a directory',
    22: 'Invalid argument',
    23: 'Too many open files in system',
    24: 'Too many open files',
    25: 'Inappropriate ioctl for device',
    26: 'Text file busy',
    27: 'File too large',
    28: 'No space left on device',
    29: 'Illegal seek',
    30: 'Read-only file system',
    31: 'Too many links',
    32: 'Broken pipe',
    33: 'Numerical argument out of domain',
    34: 'Numerical result out of range',
    35: 'Resource deadlock avoided',
    36: 'File name too long',
    37: 'No locks available',
    38: 'Function not implemented',
    39: 'Directory not empty',
    40: 'Too many levels of symbolic links',
    42: 'No message of desired type',
    43: 'Identifier removed',
    44: 'Channel number out of range',
    45: 'Level 2 not synchronized',
    46: 'Level 3 halted',
    47: 'Level 3 reset',
    48: 'Link number out of range',
    49: 'Protocol driver not attached',
    50: 'No CSI structure available',
    51: 'Level 2 halted',
    52: 'Invalid exchange',
    53: 'Invalid request descriptor',
    54: 'Exchange full',
    55: 'No anode',
    56: 'Invalid request code',
    57: 'Invalid slot',
    59: 'Bad font file format',
    60: 'Device not a stream',
    61: 'No data available',
    62: 'Timer expired',
    63: 'Out of streams resources',
    64: 'Machine is not on the network',
    65: 'Package not installed',
    66: 'Object is remote',
    67: 'Link has been severed',
    68: 'Advertise error',
    69: 'Srmount error',
    70: 'Communication error on send',
    71: 'Protocol error',
    72: 'Multihop attempted',
    73: 'RFS specific error',
    74: 'Bad message',
    75: 'Value too large for defined data type',
    76: 'Name not unique on network',
    77: 'File descriptor in bad state',
    78: 'Remote address changed',
    79: 'Can not access a needed shared library',
    80: 'Accessing a corrupted shared library',
    81: '.lib section in a.out corrupted',
    82: 'Attempting to link in too many shared libraries',
    83: 'Cannot exec a shared library directly',
    84: 'Invalid or incomplete multibyte or wide character',
    85: 'Interrupted system call should be restarted',
    86: 'Streams pipe error',
    87: 'Too many users',
    88: 'Socket operation on non-socket',
    89: 'Destination address required',
    90: 'Message too long',
    91: 'Protocol wrong type for socket',
    92: 'Protocol not available',
    93: 'Protocol not supported',
    94: 'Socket type not supported',
    95: 'Operation not supported',
    96: 'Protocol family not supported',
    97: 'Address family not supported by protocol',
    98: 'Address already in use',
    99: 'Cannot assign requested address',
    100: 'Network is down',
    101: 'Network is unreachable',
    102: 'Network dropped connection on reset',
    103: 'Software caused connection abort',
    104: 'Connection reset by peer',
    105: 'No buffer space available',
    106: 'Transport endpoint is already connected',
    107: 'Transport endpoint is not connected',
    108: 'Cannot send after transport endpoint shutdown',
    109: 'Too many references: cannot splice',
    110: 'Connection timed out',
    111: 'Connection refused',
    112: 'Host is down',
    113: 'No route to host',
    114: 'Operation already in progress',
    115: 'Operation now in progress',
    116: 'Stale file handle',
    117: 'Structure needs cleaning',
    118: 'Not a XENIX named type file',
    119: 'No XENIX semaphores available',
    120: 'Is a named type file',
    121: 'Remote I/O error',
    122: 'Disk quota exceeded',
    123: 'No medium found',
    124: 'Wrong medium type',
    125: 'Operation canceled',
    126: 'Required key not available',
    127: 'Key has expired',
    128: 'Key has been revoked',
    129: 'Key was rejected by service',
    130: 'Owner died',
    131: 'State not recoverable',
    132: 'Operation not possible due to RF-kill',
    133: 'Memory page has hardware error',
}
MACOS_ERRNO_REFERENCE = {
    1: 'Operation not permitted', 2: 'No such file or directory', 5: 'Input/output error', 13: 'Permission denied',
    17: 'File exists', 18: 'Cross-device link', 20: 'Not a directory', 21: 'Is a directory', 27: 'File too large',
    28: 'No space left on device', 32: 'Broken pipe', 62: 'Too many levels of symbolic links',
}
EXPECTED_VERSION = '(3, 10, 11)'
# 作れない入力(Mac に /dev/full が無いなど)は実行せず、成功に数えない。
NOT_RUN = []

FIXTURE = None
OLD_PYTHON = None
COMPARISON_ENV = {
    'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8',
    'DEV_WORKFLOW_LOOP_ITER': '',
}


# ---- 実行物と旧 Python の確認 ----

def _is_native_binary(head):
    return head[:4] in (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xfe\xed\xfa\xcf')


def fixture_path(value, *, legacy_script=LEGACY_SCRIPT, repo=ROOT):
    """Rust 実行物の絶対パス。指定が無い・不在・実行不能・旧版への接続は ValueError。"""
    if not value or not Path(value).is_absolute():
        raise ValueError('検査入口は絶対パスで指定してください')
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('検査入口は実行可能な通常ファイルである必要があります')
    resolved = path.resolve(strict=True)
    if resolved == Path(legacy_script).resolve() or Path(repo, 'plugins').resolve() in resolved.parents:
        raise ValueError('検査入口が旧版(配る木の中)を指しています')
    with open(resolved, 'rb') as stream:
        if not _is_native_binary(stream.read(4)):
            raise ValueError('検査入口が実行形式の実行物ではありません(スクリプトは使えません)')
    return path


def verify_old_python(python, expected=UPSTREAM_STDLIB_SHA256, *, run=run_command, version=EXPECTED_VERSION):
    """旧 Python の標準ライブラリが本家 3.10.11 と同じ中身か。違えば AssertionError(skip にしない)。"""
    names = sorted(expected)
    code = ('import hashlib, os, sys, argparse\n'
            'base = os.path.dirname(argparse.__file__)\n'
            'print(sys.version_info[:3])\n'
            'for name in %r:\n'
            '    print(name, hashlib.sha256(open(os.path.join(base, name), "rb").read()).hexdigest())\n') % (names,)
    result = run([str(python), '-I', '-B', '-c', code], cwd=ROOT, env={'PATH': os.environ.get('PATH', '')}, timeout=30)
    if result.timed_out or result.returncode:
        raise AssertionError('旧 Python の標準ライブラリを確かめられない')
    lines = result.stdout.decode().splitlines()
    actual = dict(line.split(' ', 1) for line in lines[1:])
    if actual != expected:
        raise AssertionError('旧 Python の標準ライブラリが本家 3.10.11 と違う(診断の比較をしない)')
    if version is not None and lines[0] != version:
        raise AssertionError(f'旧 Python の版が基準 {version} と違う: {lines[0]}')
    return lines[0]


def verify_errno_baseline(python, *, reference=None, run=run_command):
    """旧 Python(同じ libc)の `os.strerror` が基準の表と同じこと。違えば AssertionError(Rust の表との比較をしない)。"""
    if reference is None:
        reference = GLIBC_ERRNO_REFERENCE if sys.platform.startswith('linux') else MACOS_ERRNO_REFERENCE
    code = 'import json, os; print(json.dumps({n: os.strerror(n) for n in %r}))' % (sorted(reference),)
    result = run([str(python), '-I', '-B', '-c', code], cwd=ROOT, env={'PATH': os.environ.get('PATH', ''), 'LC_ALL': 'C'}, timeout=30)
    if result.timed_out or result.returncode:
        raise AssertionError('旧 Python の errno の文を取れない')
    actual = {int(key): value for key, value in json.loads(result.stdout).items()}
    wrong = sorted(number for number in reference if actual.get(number) != reference[number])
    if wrong:
        raise AssertionError(f'errno の文が基準の機械の表と違う(比較をしない): {wrong[:5]}')
    return len(reference)


# ---- 結果の比較 ----

UNCONNECTED = '未接続です'  # 依存部品(#471・#472)の未接続を知らせる example の停止文に含まれる語。


def same_result(old, new):
    if old.timed_out or new.timed_out:
        raise AssertionError('比較対象が時間切れになった')
    if (old.returncode, old.stdout, old.stderr) != (new.returncode, new.stdout, new.stderr):
        raise AssertionError('終了値または出力が一致しない' + (f'({UNCONNECTED}: 依存部品の未接続で止まった)' if UNCONNECTED.encode() in new.stderr else ''))


def fixed(result, code, stdout=b'', stderr=b''):
    if result.timed_out or (result.returncode, result.stdout, result.stderr) != (code, stdout, stderr):
        raise AssertionError('固定期待と一致しない' + (f'({UNCONNECTED}: 依存部品の未接続で止まった)' if UNCONNECTED.encode() in result.stderr else ''))


# 固定期待との照合の結果を、旧版の側と Rust の側で別に数える。旧版が固定期待と違えば、Rust の結果と無関係に比較器の前提が崩れている。
# Rust の側の不一致のうち、依存部品(#471・#472)が未接続で止まったものは別に数え、成功には数えない。
TALLY = {'old_match': 0, 'old_mismatch': [], 'native_match': 0, 'native_mismatch': [], 'native_unconnected': []}


def _mentions(value, needle):
    """実出力(バイト列・文字列・それらの入れ子)が、`needle` を含むか。バイト列は UTF-8 として読む。"""
    if isinstance(value, bytes):
        return needle in value.decode('utf-8', 'replace')
    if isinstance(value, str):
        return needle in value
    if isinstance(value, (tuple, list)):
        return any(_mentions(item, needle) for item in value)
    if isinstance(value, dict):
        return any(_mentions(item, needle) for item in value.values())
    return False


def judge(which, label, actual, expected):
    """片側の実出力を固定期待と照らす。一致なら数えて戻り、不一致なら数えて AssertionError。"""
    if actual == expected:
        TALLY[which + '_match'] += 1
        return
    unconnected = which == 'native' and _mentions(actual, UNCONNECTED)
    TALLY['native_unconnected' if unconnected else which + '_mismatch'].append(label)
    raise AssertionError(f"{'旧版' if which == 'old' else 'Rust'}が固定期待と違う: {label}" + (f'({UNCONNECTED}: 依存部品の未接続で止まった)' if unconnected else ''))


def judge_run(which, label, check):
    """片側の実行とその確認 `check()`(不一致なら AssertionError)の結果を、旧版の側と Rust の側で別に数える。"""
    try:
        check()
    except AssertionError as error:
        if which == 'native' and _mentions(str(error), UNCONNECTED):
            TALLY['native_unconnected'].append(label)
        else:
            TALLY[which + '_mismatch'].append(label)
        raise
    TALLY[which + '_match'] += 1


def judge_fixed(which, label, result, code, stdout=b'', stderr=b''):
    actual = ('時間切れ',) if result.timed_out else (result.returncode, result.stdout, result.stderr)
    judge(which, label, actual, (code, stdout, stderr))


def judge_sides(label, old, new, expected):
    """旧版と Rust の実出力を、同じ固定期待と別々に照らす(旧版が違っても、Rust の側も照らす)。
    期待が `{'old': …, 'native': …}` の辞書なら、側ごとの期待(その側の root の絶対パスを含む出力など)で照らす。"""
    failures = []
    for which, actual in (('old', old), ('native', new)):
        try:
            judge(which, label, actual, expected[which] if isinstance(expected, dict) else expected)
        except AssertionError as error:
            failures.append(str(error))
    if failures:
        raise AssertionError(' / '.join(failures))


def same_tree(old, new):
    if old != new:
        raise AssertionError('作業ツリーの記録が一致しない')


def third_party_intact(before, after, names):
    """旧版も Rust も触ってはいけない第三者の実体が、種類・mode・内容とも変わっていないこと。"""
    for name in names:
        if name not in before or after.get(name) != before[name]:
            raise AssertionError(f'第三者の実体が変わった・消えた: {name}')


def run_suite(suite, *, breakdown=False):
    count = suite.countTestCases()
    if count == 0:
        raise AssertionError('検査ケースが0件')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    problems = [text for _, text in result.failures + result.errors]
    if problems and breakdown:
        blocked = sum(UNCONNECTED in text for text in problems)
        print(f'ADOPT: 失敗 {len(problems)} 件のうち、依存部品(#471・#472)の未接続による不通過 {blocked} 件・それ以外 {len(problems) - blocked} 件', flush=True)
    if not result.wasSuccessful() or result.testsRun != count or result.skipped:
        raise AssertionError('未実行・失敗・省略したケースがある')
    return count


def old_test_methods(path=OLD_TEST):
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    return [node.name[5:] for cls in tree.body if isinstance(cls, ast.ClassDef)
            for node in cls.body if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')]


def verify_map(methods=None, table=OLD_TEST_MAP, extra=EXTRA_GROUPS, checks=None):
    """旧テストの全メソッドが観点の表へ 1 回ずつ載っているか。検査名は実在する Contract のメソッドか。"""
    methods = old_test_methods() if methods is None else methods
    checks = {name for name in dir(Contract) if name.startswith('test_')} if checks is None else checks
    ids = [row[0] for row in table] + [row[0] for row in extra]
    if len(ids) != len(set(ids)):
        raise AssertionError('観点の ID が重複している')
    named = [row[1] for row in table]
    if len(named) != len(set(named)):
        raise AssertionError('旧テスト名が重複している')
    if sorted(named) != sorted(methods):
        raise AssertionError('旧テストの全メソッドと観点の表が一致しない(欠落または余分)')
    flat = []
    for check in [row[3] for row in table] + [row[2] for row in extra]:
        flat += [check] if isinstance(check, str) else list(check or [])
    for check in flat:
        if check not in checks:
            raise AssertionError(f'観点の表が実在しない検査を指している: {check}')
    return len(table), len(extra)


# ---- 実行 ----

def check_no_git_ancestor(path):
    for parent in (Path(path), *Path(path).parents):
        if os.path.lexists(parent / '.git'):
            raise AssertionError('一時フォルダの祖先に Git がある。TMPDIR を Git の外へ指定してください')


def environment(columns=None, marker=''):
    env = {'PATH': os.environ.get('PATH', ''), **COMPARISON_ENV, 'DEV_WORKFLOW_LOOP_ITER': marker}
    if columns is not None:
        env['COLUMNS'] = columns
    return env


def legacy(args, env, cwd, timeout=50):
    return run_command([str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT), *args], cwd=cwd, env=env, timeout=timeout)


def native(args, env, cwd, timeout=50):
    return run_command([str(FIXTURE), *args], cwd=cwd, env=env, timeout=timeout)


CANDIDATE = '> **発見元**: refactor\n> **指摘キー**: H41\n\n## 指摘\n内容\n'


class Side:
    """旧版か Rust の片側。独立した使い捨て root を持つ。"""

    def __init__(self, runner, body=CANDIDATE):
        self.runner = runner
        self.directory = tempfile.TemporaryDirectory(prefix='r4c-adopt-')
        check_no_git_ancestor(self.directory.name)
        self.root = Path(self.directory.name)
        self.source = self.root / '候補_例.md'
        self.source.write_text(body, encoding='utf-8')
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def close(self):
        self.directory.cleanup()

    def put(self, name, text):
        (self.root / name).write_text(text, encoding='utf-8')

    def git(self, *args):
        """比較の準備用の Git(比較の対象ではない)。identity は引数で与え、利用者の設定を読まない。"""
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.root), 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': '/dev/null'}
        result = subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=test',
                                 '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
                                 '-c', 'init.defaultBranch=main', *args], env=env, capture_output=True, timeout=30)
        if result.returncode:
            raise AssertionError(f'準備の git が失敗した: {args}')
        return result.stdout

    def commit_candidate(self):
        self.git('init', '-q')
        self.git('add', '--', self.source.name)
        self.git('commit', '-qm', 'base')

    def index_record(self):
        """index の中身(mode・OID・stage・名前)を記録する。作業ツリーの本文とは別に比べる。"""
        listing = self.git('ls-files', '--stage', '-z')
        return {'kind': 'index', 'mode': 0, 'sha256': hashlib.sha256(listing).hexdigest()}

    def run(self, extra=(), marker='', digest=None):
        args = ['--root', str(self.root), '--expect-sha256', self.digest if digest is None else digest, *extra, str(self.source)]
        before = self.tree()
        result = self.runner(args, environment(marker=marker), self.root)
        return self.normalize(result), before, self.tree()

    def tree(self):
        """作業ツリーの記録。`.git` の中身は時刻などで変わるので除き、Git の状態は index の記録で比べる。"""
        tree = {key: value for key, value in snapshot_tree(self.root).items()
                if key != '.git' and not key.startswith('.git/')}
        if (self.root / '.git').exists():
            tree['<git-index>'] = self.index_record()
        return tree

    def normalize(self, result):
        """既知の独立 root だけを固定の語へ対応づける。ほかの文字は変えない。"""
        root = os.fsencode(self.root)
        return RunResult(result.returncode, result.stdout.replace(root, b'<ROOT>'),
                         result.stderr.replace(root, b'<ROOT>'), result.timed_out)


def pair(extra=(), marker='', body=CANDIDATE, prepare=None, digest_of=None):
    """同じ入力を旧版と Rust の独立 root で流し、(旧, Rust) の結果と作業ツリーの記録を返す。"""
    sides = (Side(legacy, body), Side(native, body))
    try:
        results = []
        for side in sides:
            if prepare:
                prepare(side)
            results.append(side.run(extra, marker, digest_of(side) if digest_of else None))
        return results
    finally:
        for side in sides:
            side.close()


def stop(message):
    return f'候補の採用を停止: {message}\n'.encode()


class Contract(unittest.TestCase):
    """旧採用テストの観点のうち、旧版だけで固定期待を作れるもの。"""

    def expect(self, outcome, code, stderr=b'', stdout=b''):
        (old, old_before, old_after), (new, new_before, new_after) = outcome
        label = self.id().rsplit('.', 1)[-1]
        failures = []
        for which, result in (('old', old), ('native', new)):
            try:
                judge_fixed(which, label, result, code, stdout, stderr)
            except AssertionError as error:
                failures.append(str(error))
        if failures:
            raise AssertionError(' / '.join(failures))
        same_result(old, new)
        same_tree(old_after, new_after)
        return old_before, old_after

    def test_a01_untracked_normal(self):
        outcome = pair()
        digest = hashlib.sha256(CANDIDATE.encode()).hexdigest()
        stdout = ('{"path": "<ROOT>/進行中_例.md", "tracked": false, "sha256": "%s", "warnings": []}\n' % digest).encode()
        before, after = self.expect(outcome, 0, stdout=stdout)
        self.assertNotIn('候補_例.md', after)
        self.assertEqual(after['進行中_例.md']['sha256'], hashlib.sha256(CANDIDATE.encode()).hexdigest())

    def test_a02_loop_marker(self):
        before, after = self.expect(pair(marker='run-1'), 2, stop('DEV_WORKFLOW_LOOP_ITER があるため候補を採用できない'))
        self.assertEqual(before, after)

    def test_a03_unattended(self):
        before, after = self.expect(pair(('--unattended',)), 2, stop('無人実行では候補を採用できない'))
        self.assertEqual(before, after)

    def test_a04_digest_change(self):
        outcome = pair(digest_of=lambda side: '0' * 64)
        before, after = self.expect(outcome, 2, stop('候補の sha256 が保持値と違う'))
        self.assertEqual(before, after)

    def test_a08_collision_all_states(self):
        for state in ('完了', '保留', '中断', '進行中'):
            with self.subTest(state=state):
                outcome = pair(prepare=lambda side, state=state: side.put(f'{state}_例.md', 'untouched'))
                before, after = self.expect(outcome, 2, stop('同じ名の状態ファイルがある'))
                third_party_intact(before, after, [f'{state}_例.md'])
                self.assertIn('候補_例.md', after)

    def test_a09_renamed_collision(self):
        outcome = pair(('--name', '別名'), prepare=lambda side: side.put('候補_別名.md', 'third party'))
        before, after = self.expect(outcome, 2, stop('同じ名の状態ファイルがある'))
        third_party_intact(before, after, ['候補_別名.md'])

    def test_a10_skip_lines(self):
        for line in ('> **見送り**: 2026-10-01 — 人の判断', '> **見送り**: broken'):
            with self.subTest(line=line):
                outcome = pair(body='# 例\n' + line + '\n## 指摘\n')
                before, after = self.expect(outcome, 2, stop('見送りの行を確認し、削除して commit してから呼び直す'))
                self.assertEqual(before, after)

    def test_e01_argument_cases(self):
        document = json.loads(ARG_CASES.read_text(encoding='utf-8'))
        run, skipped = argument_cases(document)
        self.assertGreater(run, 100)
        self.assertGreater(skipped, 5)


def _unavailable(streams, label):
    """/dev/full を作れない機種では、その入力を実行せず、成功に数えない。"""
    if 'full' in streams.values() and not os.path.exists('/dev/full'):
        NOT_RUN.append(label)
        return True
    return False


def _contract_e01b(self):
    help_text, _ = fixed_text(['-h'])
    description = '候補の確認と採用改名を一つの入口で行う。本文の設計は呼出側が行う。\n\n'.encode()
    base = environment()
    cases = [
        ('PYTHONOPTIMIZE=2 は __doc__ を消す', ['-h'], {'PYTHONOPTIMIZE': '2'}, {}, (0, help_text.replace(description, b''), b'')),
        ('PYTHONOPTIMIZE=1 は説明を残す', ['-h'], {'PYTHONOPTIMIZE': '1'}, {}, (0, help_text, b'')),
        ('不正な PYTHONHASHSEED', ['-h'], {'PYTHONHASHSEED': 'x'}, {}, (1, b'', FATAL_HASH)),
        ('不正な PYTHONINTMAXSTRDIGITS', ['-h'], {'PYTHONINTMAXSTRDIGITS': '5'}, {}, (1, b'', FATAL_DIGITS)),
        ('両方不正なら PYTHONHASHSEED が先', ['-h'], {'PYTHONHASHSEED': 'x', 'PYTHONINTMAXSTRDIGITS': '5'}, {}, (1, b'', FATAL_HASH)),
        ('環境変数の失敗は標準入力より先', ['-h'], {'PYTHONHASHSEED': 'x'}, {'stdin': 'dir'}, (1, b'', FATAL_HASH)),
        ('標準入力がディレクトリ', ['-h'], {}, {'stdin': 'dir'}, (1, b'', FATAL_STDIN)),
        ('引数の誤りでも標準入力が先', ['--bad'], {}, {'stdin': 'dir'}, (1, b'', FATAL_STDIN)),
        ('標準出力がディレクトリ', ['-h'], {}, {'stdout': 'dir'}, (1, b'', FATAL_STDOUT)),
        ('標準入力と標準出力がディレクトリなら標準入力', ['-h'], {}, {'stdin': 'dir', 'stdout': 'dir'}, (1, b'', FATAL_STDIN)),
        ('標準エラーがディレクトリなら何も出ない', ['-h'], {}, {'stderr': 'dir'}, (1, b'', b'')),
        ('標準出力と標準エラーがディレクトリ', ['-h'], {}, {'stdout': 'dir', 'stderr': 'dir'}, (1, b'', b'')),
        ('標準入力がディレクトリで標準エラーもディレクトリ', ['-h'], {}, {'stdin': 'dir', 'stderr': 'dir'}, (1, b'', b'')),
    ]
    for label, args, extra, streams, expected in cases:
        with self.subTest(label):
            if _unavailable(streams, label):
                continue
            old, new = both(args, {**base, **extra}, **streams)
            judge_sides(label, old, new, expected)
            PROVISIONAL_RUN.append(label)  # 機種で変わる診断。CI の証拠を管理担当が確かめるまで仮の期待


def _contract_e13a(self):
    help_text, _ = fixed_text(['-h'])
    usage_text = fixed_text(['--bad'])[1]
    base = environment()
    unbuffered = {**base, 'PYTHONUNBUFFERED': '1'}
    total = len(help_text)
    cases = []
    for limit in (0, 10, total - 1):
        cases.append((f'help・溜める・標準出力の上限 {limit}', ['-h'], base, {'stdout': ('file', limit)},
                      (120, help_text[:limit], UNRAISABLE % EFBIG.encode())))
        cases.append((f'help・待たずに書く・標準出力の上限 {limit}', ['-h'], unbuffered, {'stdout': ('file', limit)},
                      (1, b'', help_traceback(EFBIG)) if limit == 0 else (0, help_text[:limit], b'')))
    cases += [
        ('help・溜める・上限ちょうど', ['-h'], base, {'stdout': ('file', total)}, (0, help_text, b'')),
        ('help・待たずに書く・上限ちょうど', ['-h'], unbuffered, {'stdout': ('file', total)}, (0, help_text, b'')),
        ('help・溜める・/dev/full', ['-h'], base, {'stdout': 'full'}, (120, b'', UNRAISABLE % ENOSPC.encode())),
        ('help・待たずに書く・/dev/full', ['-h'], unbuffered, {'stdout': 'full'}, (1, b'', help_traceback(ENOSPC))),
        ('help・両方 /dev/full・溜める', ['-h'], base, {'stdout': 'full', 'stderr': 'full'}, (120, b'', b'')),
        ('help・両方 /dev/full・待たずに書く', ['-h'], unbuffered, {'stdout': 'full', 'stderr': 'full'}, (1, b'', b'')),
        ('help・標準エラーだけ /dev/full', ['-h'], base, {'stderr': 'full'}, (0, help_text, b'')),
    ]
    for limit in (0, 10):
        cases.append((f'引数の誤り・溜める・標準エラーの上限 {limit}', ['--bad'], base, {'stderr': ('file', limit)},
                      (120, b'', usage_text[:limit])))
        cases.append((f'引数の誤り・待たずに書く・標準エラーの上限 {limit}', ['--bad'], unbuffered, {'stderr': ('file', limit)},
                      (1, b'', usage_text[:limit])))
    cases += [
        ('引数の誤り・標準エラーが /dev/full・溜める', ['--bad'], base, {'stderr': 'full'}, (120, b'', b'')),
        ('引数の誤り・標準エラーが /dev/full・待たずに書く', ['--bad'], unbuffered, {'stderr': 'full'}, (1, b'', b'')),
        ('引数の誤り・標準出力が /dev/full でも影響しない', ['--bad'], base, {'stdout': 'full'}, (2, b'', usage_text)),
    ]
    for label, args, env, streams, expected in cases:
        with self.subTest(label):
            if _unavailable(streams, label):
                continue
            old, new = both(args, env, **streams)
            judge_sides(label, old, new, expected)
            PROVISIONAL_RUN.append(label)  # 機種で変わる診断。CI の証拠を管理担当が確かめるまで仮の期待


Contract.test_e01b_startup_environment = _contract_e01b
Contract.test_e13a_output_faults = _contract_e13a


def _stdout_for(digest, path='<ROOT>/進行中_例.md', tracked=False, warnings='[]'):
    return ('{"path": "%s", "tracked": %s, "sha256": "%s", "warnings": %s}\n' % (
        path, 'true' if tracked else 'false', digest, warnings)).encode()


def _tracked_pair(body, edit=None, extra=()):
    def prepare(side):
        side.commit_candidate()
        if edit:
            edit(side)

    def digest(side):
        return hashlib.sha256(side.source.read_bytes()).hexdigest()
    return pair(extra, body=body, prepare=prepare, digest_of=digest)


def _contract_a06(self):
    for body, code in (('## 指摘\n本文\n', 'source-count'), ('> **発見元**: refactor\n\n## 指摘\n本文\n', 'key-count')):
        with self.subTest(code=code):
            (old, old_before, old_after), (new, _, new_after) = pair(body=body)
            self.assertEqual(old.returncode, 0)
            self.assertIn(code.encode(), old.stdout)
            same_result(old, new)
            same_tree(old_after, new_after)


def _contract_tracked(self):
    digest = hashlib.sha256(CANDIDATE.encode()).hexdigest()
    outcome = _tracked_pair(CANDIDATE)
    before, after = self.expect(outcome, 0, stdout=_stdout_for(digest, tracked=True))
    self.assertIn('進行中_例.md', after)
    unstaged = CANDIDATE + '未 stage の追記\n'
    outcome = _tracked_pair(CANDIDATE, edit=lambda side: side.source.write_text(unstaged, encoding='utf-8'))
    self.expect(outcome, 0, stdout=_stdout_for(hashlib.sha256(unstaged.encode()).hexdigest(), tracked=True))
    staged = CANDIDATE + 'stage した追記\n'
    current = staged + '未 stage の追記\n'

    def partial(side):
        side.source.write_text(staged, encoding='utf-8')
        side.git('add', '--', side.source.name)
        side.source.write_text(current, encoding='utf-8')
    outcome = _tracked_pair(CANDIDATE, edit=partial)
    self.expect(outcome, 0, stdout=_stdout_for(hashlib.sha256(current.encode()).hexdigest(), tracked=True))


def _contract_a17(self):
    def tamper(side):
        side.commit_candidate()
        side.git('config', '--local', 'filter.untrusted.clean', 'cat')
    (old, old_before, old_after), (new, _, new_after) = pair(prepare=tamper)
    self.assertEqual(old.returncode, 2)
    self.assertTrue(old.stderr.startswith('候補の採用を停止: git の事前検査に通らない: '.encode()))
    same_result(old, new)
    same_tree(old_after, new_after)
    self.assertIn('候補_例.md', old_after)


def _contract_a18(self):
    def symlink(side):
        side.source.unlink()
        side.source.symlink_to('outside')

    def fifo(side):
        side.source.unlink()
        os.mkfifo(side.source)
    for label, prepare, message in (
        ('末端が symlink', symlink, f"[Errno {errno_module.ELOOP}] {os.strerror(errno_module.ELOOP)}: '候補_例.md'"),
        ('末端が FIFO', fifo, '候補は 8 MiB 以下の通常ファイルだけを使う'),
    ):
        with self.subTest(label):
            outcome = pair(prepare=prepare, digest_of=lambda side: '0' * 64)
            before, after = self.expect(outcome, 2, stop(message))
            self.assertEqual(before, after)


def _contract_e02(self):
    """親の確認(依存部品なしで流せる): root が symlink・不在・`..` を含む。"""
    def with_root(make_root):
        def run(side):
            return side.root, make_root(side)
        return run
    cases = []
    for label, build in (
        ('root が symlink', lambda side: (side.put('x', ''), (side.root / 'real').mkdir(), (side.root / 'link').symlink_to('real'))),
        ('root が不在', None), ('root に ..', None), ('root と候補の間のフォルダが権限なし', None),
    ):
        cases.append(label)
    results = {}
    for label in cases:
        sides = (Side(legacy), Side(native))
        try:
            outcomes = []
            for side in sides:
                if label == 'root が symlink':
                    (side.root / 'real').mkdir()
                    (side.root / 'link').symlink_to('real')
                    (side.root / 'real' / '候補_例.md').write_text(CANDIDATE, encoding='utf-8')
                    root, candidate = side.root / 'link', side.root / 'link' / '候補_例.md'
                elif label == 'root が不在':
                    root, candidate = side.root / 'absent', side.root / 'absent' / '候補_例.md'
                elif label == 'root と候補の間のフォルダが権限なし':
                    (side.root / 'mid').mkdir()
                    (side.root / 'mid' / '候補_例.md').write_text(CANDIDATE, encoding='utf-8')
                    root, candidate = side.root, side.root / 'mid' / '候補_例.md'
                else:
                    (side.root / 'sub').mkdir()
                    root, candidate = side.root / 'sub' / '..', side.root / 'sub' / '..' / '候補_例.md'
                args = ['--root', str(root), '--expect-sha256', side.digest, str(candidate)]
                before = side.tree()
                locked = label == 'root と候補の間のフォルダが権限なし'
                if locked:
                    (side.root / 'mid').chmod(0)  # root(のディレクトリ)は開けるが、その下の mid を開けない。
                try:
                    result = side.normalize(side.runner(args, environment(), side.root))
                finally:
                    if locked:
                        (side.root / 'mid').chmod(0o700)
                outcomes.append((result, before, side.tree()))
        finally:
            for side in sides:
                side.close()
        (old, old_before, old_after), (new, _, new_after) = outcomes
        self.assertEqual(old.returncode, 2, label)
        same_result(old, new)
        same_tree(old_after, new_after)
        results[label] = old.stderr
    self.assertIn('親への参照を含むパスは使えない'.encode(), results['root に ..'])
    self.assertIn(b"No such file or directory: 'absent'", results['root が不在'])
    self.assertIn(b"[Errno 13] Permission denied: 'mid'", results['root と候補の間のフォルダが権限なし'])


def _contract_e03(self):
    """候補の大きさ: 8MiB を 1 バイト超えると、依存部品なしで拒否される。ちょうど 8MiB は依存部品が要る。"""
    over = 'x' * (8 * 1024 * 1024 + 1)
    outcome = pair(body=over)
    before, after = self.expect(outcome, 2, stop('候補は 8 MiB 以下の通常ファイルだけを使う'))
    self.assertEqual(before, after)
    edge = '## 指摘\n' + 'x' * (8 * 1024 * 1024 - len('## 指摘\n'.encode()))
    (old, _, old_after), (new, _, new_after) = pair(body=edge)
    self.assertEqual(old.returncode, 0)
    same_result(old, new)
    same_tree(old_after, new_after)


def _contract_e04(self):
    """保持 SHA の形: 長さ・大文字・文字種(依存部品なしで拒否される)。"""
    good = hashlib.sha256(CANDIDATE.encode()).hexdigest()
    for bad in (good.upper(), good[:63], good + '0', 'g' * 64, '', ' ' + good, good + '\n'):
        with self.subTest(digest=bad):
            outcome = pair(digest_of=lambda side, bad=bad: bad)
            before, after = self.expect(outcome, 2, stop('候補の sha256 が保持値と違う'))
            self.assertEqual(before, after)


Contract.test_a06_warnings_from_candidate_check = _contract_a06
Contract.test_a12_a14_tracked_adoption = _contract_tracked
Contract.test_a17_precheck_refusal = _contract_a17
Contract.test_a18_symlink_and_fifo = _contract_a18
Contract.test_e02_parent_and_root = _contract_e02
Contract.test_e03_candidate_size = _contract_e03
Contract.test_e04_digest_shape = _contract_e04


def argument_cases(document):
    """引数だけで終わる固定期待(help・usage・引数の失敗)を、旧版と Rust の実出力で確かめる。

    解析が成功するケースは旧版が採用へ進むので、ここでは流さない(Rust の単体テストが固定する)。
    戻り値は (流した件数, 流さなかった件数)。
    """
    if document.get('argparse_sha256') != UPSTREAM_STDLIB_SHA256['argparse.py']:
        raise AssertionError('固定期待が本家 3.10.11 の argparse.py から採られていない')
    run = skipped = 0
    with tempfile.TemporaryDirectory(prefix='r4c-args-') as directory:
        check_no_git_ancestor(directory)
        for case in document['cases']:
            if 'parsed' in case:
                skipped += 1
                continue
            args = [bytes.fromhex(item) for item in case['args']]
            env = environment(columns=case['columns'])
            if case.get('max_digits') is not None:
                env['PYTHONINTMAXSTRDIGITS'] = case['max_digits']
            expected = (case['code'], case['stdout'].encode(), case['stderr'].encode())
            seen = {which: runner(args, env, directory) for which, runner in (('old', legacy), ('native', native))}
            as_tuple = lambda r: ('時間切れ',) if r.timed_out else (r.returncode, r.stdout, r.stderr)  # noqa: E731
            judge_sides('args:' + ' '.join(case['args']) + f":{case['columns']}", as_tuple(seen['old']), as_tuple(seen['native']), expected)
            run += 1
    return run, skipped


# ---- 標準の fd を細工した実行 ----

HELP_PATH = Path(__file__).with_name('adopt_candidate_fixtures') / 'arg_cases.json'


def fixed_text(args, columns=None):
    """固定期待(arg_cases.json)の `args`・`columns` に一致するケースの実出力。"""
    document = json.loads(ARG_CASES.read_text(encoding='utf-8'))
    wanted = [item.encode().hex() for item in args]
    for case in document['cases']:
        if case['args'] == wanted and case['columns'] == columns and 'parsed' not in case:
            return case['stdout'].encode(), case['stderr'].encode()
    raise AssertionError(f'固定期待にケースが無い: {args}')


def fd_for(kind, directory, name):
    """標準の fd の細工。'pipe'・'dir'・'full'・('file', 上限バイト数)。"""
    if kind == 'pipe':
        return subprocess.PIPE, None
    if kind == 'dir':
        return os.open(directory, os.O_RDONLY), None
    if kind == 'full':
        return os.open('/dev/full', os.O_WRONLY), None
    path = Path(directory) / name
    return open(path, 'wb'), path


def faulty(runner_argv, env, *, stdin='null', stdout='pipe', stderr='pipe', timeout=50):
    """標準の fd を細工して起動し、(終了値, 標準出力, 標準エラー)のバイト列を返す。

    'file' は通常のファイルで、上限は `RLIMIT_FSIZE` で与える(両方がファイルなら小さい方)。
    Python は起動時に SIGXFSZ を無視するので、上限を超える書込みは EFBIG で失敗する。
    """
    limits = [kind[1] for kind in (stdout, stderr) if isinstance(kind, tuple)]
    limit = min(limits) if limits else None

    def set_limit():
        if limit is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    with tempfile.TemporaryDirectory(prefix='r4c-fault-') as directory:
        check_no_git_ancestor(directory)
        opened = []
        try:
            fds = []
            for name, kind in (('stdin', stdin), ('stdout', stdout), ('stderr', stderr)):
                if name == 'stdin':
                    fd = os.open(directory, os.O_RDONLY) if kind == 'dir' else subprocess.DEVNULL
                    path = None
                else:
                    fd, path = fd_for('file' if isinstance(kind, tuple) else kind, directory, name)
                fds.append((fd, path))
                opened.append(fd)
            process = subprocess.Popen(runner_argv, env=env, cwd=directory, stdin=fds[0][0], stdout=fds[1][0],
                                       stderr=fds[2][0], preexec_fn=set_limit, start_new_session=True)
            try:
                out, err = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, 9)
                process.communicate()
                raise AssertionError('標準の fd を細工した実行が時間切れになった')
            if fds[1][1] is not None:
                out = fds[1][1].read_bytes()
            if fds[2][1] is not None:
                err = fds[2][1].read_bytes()
            return process.returncode, out or b'', err or b''
        finally:
            for fd in opened:
                if isinstance(fd, int):
                    if fd >= 0:
                        os.close(fd)
                elif hasattr(fd, 'close'):
                    fd.close()


def normalize_diagnostic(stderr, *, script=None, stdlib=None):
    """旧版の診断に出る実行環境のパスだけを固定名へ対応づける(2026-11-11 の共通決定)。

    対応づけるのは、旧スクリプト自身のパスと、旧 Python の標準ライブラリのパス(`File "…"` の欄)と、
    起動の失敗の `Current thread 0x…` の 16 進値(位置と形式だけを検査する)。ほかの文字は変えない。
    """
    text = stderr
    if script is not None:
        text = text.replace(b'File "' + os.fsencode(script) + b'"', b'File "adopt-candidate.py"')
    if stdlib is not None:
        for name in ('argparse.py', 'pathlib.py', 're.py'):
            text = text.replace(b'File "' + os.fsencode(Path(stdlib) / name) + b'"', b'File "' + name.encode() + b'"')
    pattern = re.compile(rb'(?m)^Current thread 0x([0-9a-f]{16}) \(most recent call first\):$')
    found = pattern.findall(text)
    if len(found) > 1:
        raise AssertionError('Current thread の欄が複数ある')
    return pattern.sub(b'Current thread 0x<THREAD> (most recent call first):', text)


def stdlib_directory(python):
    result = run_command([str(python), '-I', '-B', '-c', 'import argparse,os;print(os.path.dirname(argparse.__file__))'],
                         cwd=ROOT, env={'PATH': os.environ.get('PATH', '')}, timeout=30)
    if result.returncode or result.timed_out:
        raise AssertionError('旧 Python の標準ライブラリの場所を取れない')
    return result.stdout.decode().strip()


UNRAISABLE = ("Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n"
              "%s\n").encode()
HELP_FRAMES = (
    ('adopt-candidate.py', 273, '<module>', 'sys.exit(main())'),
    ('adopt-candidate.py', 260, 'main', 'args = parser.parse_args()'),
    ('argparse.py', 1833, 'parse_args', 'args, argv = self.parse_known_args(args, namespace)'),
    ('argparse.py', 1866, 'parse_known_args', 'namespace, args = self._parse_known_args(args, namespace)'),
    ('argparse.py', 2079, '_parse_known_args', 'start_index = consume_optional(start_index)'),
    ('argparse.py', 2019, 'consume_optional', 'take_action(action, args, option_string)'),
    ('argparse.py', 1943, 'take_action', 'action(self, namespace, argument_values, option_string)'),
    ('argparse.py', 1106, '__call__', 'parser.print_help()'),
    ('argparse.py', 2567, 'print_help', 'self._print_message(self.format_help(), file)'),
    ('argparse.py', 2573, '_print_message', 'file.write(message)'),
)


def help_traceback(errno_line):
    lines = ['Traceback (most recent call last):']
    for name, number, function, source in HELP_FRAMES:
        lines += [f'  File "{name}", line {number}, in {function}', f'    {source}']
    return ('\n'.join(lines) + '\n' + errno_line + '\n').encode()


EFBIG = 'OSError: [Errno 27] File too large'
ENOSPC = 'OSError: [Errno 28] No space left on device'
FATAL_HASH = (b'Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be "random" or an integer in range '
              b'[0; 4294967295]\nPython runtime state: preinitialized\n\n')
FATAL_DIGITS = (b'Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 '
                b'or 0 for unlimited.\nPython runtime state: preinitialized\n\n')
CURRENT_THREAD = b'\nCurrent thread 0x<THREAD> (most recent call first):\n  <no Python frame>\n'
FATAL_STDIN = (b'Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\n'
               b'Python runtime state: core initialized\n' + CURRENT_THREAD)
FATAL_STDOUT = (b"Fatal Python error: init_sys_streams: can't initialize sys standard streams\n"
                b'Python runtime state: core initialized\nIsADirectoryError: [Errno 21] Is a directory: 1\n' + CURRENT_THREAD)


def both(args, env, **streams):
    """旧版と Rust を同じ細工で流し、正規化した結果の組を返す。"""
    stdlib = stdlib_directory(OLD_PYTHON)
    old = faulty([str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT), *args], env, **streams)
    new = faulty([str(FIXTURE), *args], env, **streams)
    clean = lambda result, script: (result[0], result[1], normalize_diagnostic(result[2], script=script, stdlib=stdlib))  # noqa: E731
    return clean(old, LEGACY_SCRIPT), clean(new, None)


# ---- 注入(旧テストと同じ点への差し込み)の比較 ----

tempfile.tempdir = os.path.realpath(tempfile.gettempdir())  # Mac の /var/folders はリンク。実体のパスにする(前例 check_r4r.py)。
FIXTURES = Path(__file__).with_name('adopt_candidate_fixtures')
HARNESS_SCRIPT = FIXTURES / 'old_harness.py'
HARNESS_SCRIPT_TARGET = LEGACY_SCRIPT  # 旧版の診断に出るスクリプト名(起動器ではなく旧版のもの)
FAULT_SCENARIOS = FIXTURES / 'fault_scenarios.json'


@contextlib.contextmanager
def fixed_umask(value=0o022):
    """子プロセスの作る実体の権限を決めるため、実行の間だけ umask を固定し、終わったら元に戻す(比較器自身の状態を残さない)。"""
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


def _snapshot(root, origin_inode=None):
    """作業ツリーの記録。`.git`・root 自身は除く。

    各実行内の実体の関係も残す: 通常ファイルは `inode`(元の候補と同じ実体なら `origin`、ほかは同じ実体どうしが
    同じ番号の `other-N`)・`nlink`。権限は `S_IMODE` の全体(`mode`。symlink は OS で値が違うので除く)。
    inode の数値そのものは比べない(独立した入力なので)。
    """
    tree = {}
    base = Path(root)
    for path in sorted(base.rglob('*')):
        key = path.relative_to(base).as_posix()
        if key == '.git' or key.startswith('.git/'):
            continue
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            entry = {'kind': 'symlink', 'sha256': hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()}
        elif stat.S_ISDIR(info.st_mode):
            entry = {'kind': 'directory', 'sha256': None}
        elif stat.S_ISREG(info.st_mode):
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except PermissionError:
                digest = 'unreadable'
            entry = {'kind': 'file', 'sha256': digest}
        else:
            entry = {'kind': 'fifo' if stat.S_ISFIFO(info.st_mode) else 'other', 'sha256': None}
        if entry['kind'] != 'symlink':
            entry['mode'] = stat.S_IMODE(info.st_mode)
        tree[key] = entry
    labels = {}
    for key in sorted(tree):
        value = tree[key]
        info = os.lstat(Path(root) / key)
        if value['kind'] == 'file':
            if info.st_ino == origin_inode:
                value['inode'] = 'origin'
            else:
                value['inode'] = labels.setdefault(info.st_ino, f'other-{len(labels) + 1}')
            value['nlink'] = info.st_nlink
    return tree


PAST_NS = 1_000_000_000 * 10 ** 9  # 2001-09-09。読取り中の書換えを時刻の違いで検出できるよう、候補の mtime をここへ戻す。


def scenario_record(scenario, which, *, capture_log=False, extra_env=None, stdin_data=None):
    """シナリオを旧版か Rust の独立した root で流し、正規化した結果を返す。

    which は 'old'(旧版を `old_harness.py` 経由)か 'native'(example を `R4C_FAULT_SPEC` つき)。
    kind は 'run'・'bypass'(環境の印を消す子)・'kill'(窓へ着いたら SIGKILL)・'interrupt'(窓へ着いたら SIGINT)。
    窓への到達は制御用の pipe の通知で待つ(sleep には頼らない)。
    `capture_log` なら、Git の呼び出しの記録を仕様と同じフォルダに作らせ、`log_lines` に載せる。
    """
    with fixed_umask():
        return _scenario_record(scenario, which, capture_log, extra_env, stdin_data)


def _scenario_record(scenario, which, capture_log, extra_env, stdin_data):
    with tempfile.TemporaryDirectory(prefix='r4c-fault-') as directory:
        check_no_git_ancestor(directory)
        root = Path(directory).resolve() / 'root'
        root.mkdir()
        source = root / '候補_例.md'
        source.write_text(CANDIDATE, encoding='utf-8')
        side = Side.__new__(Side)  # git の補助(準備用)だけを使う。独立 root は上の root。
        side.root, side.source = root, source
        prep = scenario.get('prep', {})
        digest = scenario.get('digest') or hashlib.sha256(source.read_bytes()).hexdigest()
        for name, mode in prep.get('modes', {}).items():
            (root / name).chmod(mode)
        if prep.get('git'):
            side.commit_candidate()
        for name, text in prep.get('files', {}).items():
            (root / name).write_text(text, encoding='utf-8')
        for name in prep.get('dirs', []):
            (root / name).mkdir()
        for name, target in prep.get('symlinks', {}).items():
            (root / name).symlink_to(target)
        replace = prep.get('source_as')
        if replace:
            source.unlink()
            if replace == 'directory':
                source.mkdir()
            elif replace == 'unreadable':
                source.write_text(CANDIDATE, encoding='utf-8')
                source.chmod(0)
            elif replace == 'fifo':
                os.mkfifo(source)
            elif replace == 'empty':
                source.write_bytes(b'')
                digest = hashlib.sha256(b'').hexdigest()
            elif replace.startswith('size:'):
                body = '## 指摘\n' + 'x' * (int(replace[5:]) - len('## 指摘\n'.encode()))
                source.write_text(body, encoding='utf-8')
                digest = hashlib.sha256(body.encode()).hexdigest()
            elif replace != 'absent':
                raise AssertionError('未知の source_as: ' + replace)
        if any(rule.get('phase') == 'during' for rule in scenario.get('rules', [])) and source.is_file():
            # 読取り中の書換えは、属性(mtime)の変化で気づく。時計の刻みや待ちに頼らず、準備の後に候補の時刻を過去へ戻す。
            os.utime(source, ns=(PAST_NS, PAST_NS))
        origin = os.lstat(source).st_ino if os.path.lexists(source) else None
        spec = {'rules': scenario.get('rules', []), 'body': CANDIDATE}
        log = Path(directory) / 'calls.jsonl' if capture_log else None
        if log:
            spec['log'] = str(log)
        spec_path = Path(directory) / 'spec.json'
        spec_path.write_text(json.dumps(spec), encoding='utf-8')
        args = ['--root', str(root), '--expect-sha256', digest, *scenario.get('args', []), str(source)]
        env = environment(marker=scenario.get('marker', ''))
        env.update(extra_env or {})
        for key, value in scenario.get('env', {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        target = [str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT)] if which == 'old' else [str(FIXTURE)]
        if scenario.get('kind') != 'bypass' and which == 'old':
            target = [str(OLD_PYTHON), '-B', str(HARNESS_SCRIPT), str(LEGACY_SCRIPT), str(spec_path)]
        if which == 'native':
            env['R4C_FAULT_SPEC'] = str(spec_path)
        argv = [*target, *args]
        kind = scenario.get('kind', 'run')
        if kind == 'bypass':
            code = ('import os, subprocess, sys; os.environ.pop("DEV_WORKFLOW_LOOP_ITER", None); '
                    'raise SystemExit(subprocess.run(sys.argv[1:]).returncode)')
            argv = [sys.executable, '-c', code, *argv]
            env['DEV_WORKFLOW_LOOP_ITER'] = 'untrusted-parent'
        extra = {}
        limit = scenario.get('rlimit_fsize')

        def set_limit():
            if limit is not None:
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        if kind in ('kill', 'interrupt'):
            notify_read, notify_write = os.pipe()
            wait_read, wait_write = os.pipe()
            env['R4C_FAULT_NOTIFY'], env['R4C_FAULT_WAIT'] = str(notify_write), str(wait_read)
            process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, pass_fds=(notify_write, wait_read), start_new_session=True)
            os.close(notify_write)
            os.close(wait_read)
            try:
                import select
                ready, _, _ = select.select([notify_read], [], [], 30)
                if not ready or not os.read(notify_read, 16):
                    detail = ''
                    try:
                        detail = process.communicate(timeout=10)[1].decode('utf-8', 'replace').strip()
                    except subprocess.TimeoutExpired:
                        pass
                    raise AssertionError('窓への到達の通知が無い: ' + detail)
                extra['both_names_at_window'] = (root / '候補_例.md').exists() and (root / '進行中_例.md').exists()
                if kind == 'kill':
                    os.killpg(process.pid, 9)
                else:
                    os.kill(process.pid, signal.SIGINT)
                out, err = process.communicate(timeout=30)
            finally:
                os.close(notify_read)
                os.close(wait_write)
                if process.poll() is None:
                    os.killpg(process.pid, 9)
            result = RunResult(process.returncode, out, err, False)
            extra['same_inode'] = (root / '候補_例.md').exists() and (root / '進行中_例.md').exists() and \
                os.stat(root / '候補_例.md').st_ino == os.stat(root / '進行中_例.md').st_ino
        else:
            process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=set_limit, start_new_session=True)
            try:
                out, err = process.communicate(stdin_data, timeout=int(scenario.get('timeout', 60)))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, 9)
                process.communicate()
                raise AssertionError('注入の実行が時間切れになった')
            result = RunResult(process.returncode, out, err, False)
        root_bytes = os.fsencode(root)
        stderr = result.stderr.replace(root_bytes, b'<ROOT>').decode('utf-8', 'surrogateescape')
        if kind == 'interrupt' and result.returncode < 0:
            # 信号で終わる場合、呼出履歴の途中は比べない(2026-11-11 22:31 の決定): 最後の行だけを残す。
            stderr = stderr.rstrip('\n').rsplit('\n', 1)[-1] + '\n'
        record = {'code': result.returncode,
                  'stdout': result.stdout.replace(root_bytes, b'<ROOT>').decode('utf-8', 'surrogateescape'),
                  'stderr': stderr, 'tree': _snapshot(root, origin), **extra}
        if (root / '.git').exists():
            record['index'] = side.index_record()['sha256']
        if log is not None:
            record['log_lines'] = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return record


def fault_scenarios():
    document = json.loads(FAULT_SCENARIOS.read_text(encoding='utf-8'))
    if not document['scenarios']:
        raise AssertionError('注入のシナリオが 0 件')
    return document['scenarios']


def check_fault_scenario(scenario, *, native_too=True):
    """旧版の実出力が固定期待に合い、Rust も同じ固定期待に合うこと(旧版と Rust の結果は、旧版が固定期待と同じなので同じになる)。
    旧版の側の照合と Rust の側の照合は、別に数える。"""
    old = scenario_record(scenario, 'old')
    failures = []
    try:
        judge('old', scenario['id'], old, scenario['expect'])
    except AssertionError as error:
        failures.append(str(error))
    if native_too:
        try:
            judge('native', scenario['id'], scenario_record(scenario, 'native'), scenario['expect'])
        except AssertionError as error:
            failures.append(str(error))
    if failures:
        raise AssertionError(' / '.join(failures))


def _contract_injection(self):
    attempted = 0
    platform_name = 'linux' if sys.platform.startswith('linux') else 'darwin'
    for scenario in fault_scenarios():
        with self.subTest(scenario['id']):
            if platform_name not in scenario.get('os', ['linux', 'darwin']):
                NOT_RUN.append(scenario['id'])
                continue
            attempted += 1
            check_fault_scenario(scenario)
    self.assertGreater(attempted, 40)


def _contract_git_calls(self):
    """E06: Git の全 argv・順序・GIT_ の環境が旧版と同じ(bash の事前検査の呼び出しは比べない)。"""
    scenario = {'id': 'e06-log', 'prep': {'git': True}, 'rules': []}
    logs = {}
    for which in ('old', 'native'):
        logs[which] = scenario_record(scenario, which, capture_log=True)['log_lines']
    judge_run('old', 'e06:git-calls', lambda: self.assertTrue(logs['old']))
    # Rust の記録が空(依存部品が未接続で Git に届かなかった)なら、未接続で不通過として数える。
    judge_run('native', 'e06:git-calls', lambda: self.assertEqual(logs['old'], logs['native'], '' if logs['native'] else UNCONNECTED))
    expected_env = {'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0',
                    'GIT_TERMINAL_PROMPT': '0'}
    for entry in logs['old']:
        self.assertEqual(entry['git_env'], expected_env)
        self.assertEqual(bytes.fromhex(entry['argv'][0]), b'git')


def _contract_e13(self):
    """E13: 正常な採用の後の出力の故障。改名は済み、終了値と出力が旧 Python の実測と同じになる。"""
    base = environment()
    unbuffered = {**base, 'PYTHONUNBUFFERED': '1'}
    for limit in (0, 10, 100):
        for label, env, code, stderr in (
            ('溜める', base, 120, UNRAISABLE % EFBIG.encode()),
            ('待たずに書く', unbuffered, 2, f'候補の採用を停止: {EFBIG[9:]}\n'.encode()),
        ):
            with self.subTest(f'{label}・標準出力の上限 {limit}'):
                trees, results = [], {}
                for which, runner in (('old', legacy), ('native', native)):
                    side = Side(runner)
                    try:
                        argv = ['--root', str(side.root), '--expect-sha256', side.digest, str(side.source)]
                        base_argv = [str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT)] if which == 'old' else [str(FIXTURE)]
                        results[which] = (faulty([*base_argv, *argv], env, stdout=('file', limit)),
                                          _stdout_for(hashlib.sha256(CANDIDATE.encode()).hexdigest(), path=str(side.root / '進行中_例.md')))
                        trees.append(side.tree())
                    finally:
                        side.close()
                # 途中で切れた出力は、その実行の root の絶対パスを含む。各側で自分の root を入れた全体を同じバイト数で切る。
                judge_sides(f'e13:{label}:{limit}', results['old'][0], results['native'][0],
                            {which: (code, full[:limit], stderr) for which, (_, full) in results.items()})
                self.assertIn('進行中_例.md', trees[0])
                same_tree(trees[0], trees[1])


# ---- git の代役(PATH の先頭)で、実プロセスの子の環境・期限・SIGINT を調べる ----

GIT_SHIM = r'''#!/bin/sh
# R4C 比較用の git の代役。採用の `ls-files --error-unmatch` のときだけ記録・停止し、ほかは本物へ渡す。
case " $* " in *" --error-unmatch "*) ;; *) exec "$R4C_REAL_GIT" "$@";; esac
case "$R4C_SHIM_MODE" in
# pid のファイルは、INT の処理を入れた後に書く(比較器は、これを見てから SIGINT を送る)。
trap-fast|trap-slow) ;;
*) echo $$ > "$R4C_SHIM_DIR/shim.pid";; esac
case "$R4C_SHIM_MODE" in
log) { pwd -P; env | sort; echo "ARGV:$*"; echo "STDIN:$(cat)"; } > "$R4C_SHIM_DIR/shim.log"; exec "$R4C_REAL_GIT" "$@";;
# INT を受けたら、短い片付け(印のファイル)を書いて終わる。端末の Ctrl-C は子にも届くので、親が待てば片付けは終わる。
# `wait` で待つので、INT の処理は、待ちの刻み(sleep)を待たずにすぐ走る(機械の負荷で揺れにくくする)。
trap-fast) trap 'kill $!; echo cleaned > "$R4C_SHIM_DIR/cleaned"; exit 0' INT; sleep 120 & echo $$ > "$R4C_SHIM_DIR/shim.pid"; wait;;
# 片付けが 0.25 秒を超える。親が待ち切れずに kill すれば、印は書かれない。
trap-slow) trap 'sleep 10; echo cleaned > "$R4C_SHIM_DIR/cleaned"; exit 0' INT; sleep 120 & echo $$ > "$R4C_SHIM_DIR/shim.pid"; wait;;
# 少し待って本物へ渡す。SIGINT を無視して起動されたとき、親も子も止まらずに終わることを見る。
slow) sleep 1; exec "$R4C_REAL_GIT" "$@";;
*) exec sleep 120;;
esac
'''


def run_with_shim(which, mode, *, sigint=None, ignore_sigint=False, extra_env=None, stdin_data=None, timeout=90):
    """追跡済みの候補を、git の代役つきで流す。`mode` は 'log'(環境・cwd・stdin を記録して本物へ渡す)・
    'hold'(止まる。期限か SIGINT で終わるのを確かめる)・'trap-fast'・'trap-slow'・'slow'(上の代役)。
    `sigint` は None・'process'(親だけ)・'group'(端末の Ctrl-C のように、プロセスグループ全体へ)。
    `ignore_sigint` なら、SIGINT を無視した状態で起動する。pid のファイルが現れるのを待って(出来事で待つ)から動く。"""
    with fixed_umask():
        return _run_with_shim(which, mode, sigint, ignore_sigint, extra_env, stdin_data, timeout)


def _kill_group(pgid):
    """起動した実行のプロセスグループ(`start_new_session` で pgid は pid と同じ)に残ったプロセスを止める。無ければ何もしない。"""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _is_zombie(pid):
    """回収待ちの終わったプロセスか(Linux の `/proc`。旧版は殺した子を回収せずに終わり、回収は別のプロセスが遅れて行う)。"""
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return False
    return text.rsplit(')', 1)[-1].split()[:1] in (['Z'], ['X'])


def _run_with_shim(which, mode, sigint, ignore_sigint, extra_env, stdin_data, timeout):
    import shutil
    import time
    with tempfile.TemporaryDirectory(prefix='r4c-shim-') as directory:
        check_no_git_ancestor(directory)
        base = Path(directory).resolve()
        root, shims = base / 'root', base / 'shim'
        root.mkdir()
        shims.mkdir()
        source = root / '候補_例.md'
        source.write_text(CANDIDATE, encoding='utf-8')
        side = Side.__new__(Side)
        side.root, side.source = root, source
        side.commit_candidate()
        real_git = shutil.which('git', path=environment()['PATH'])
        (shims / 'git').write_text(GIT_SHIM)
        (shims / 'git').chmod(0o755)
        env = environment()
        env.update({'PATH': f"{shims}:{env['PATH']}", 'R4C_REAL_GIT': real_git, 'R4C_SHIM_DIR': str(shims),
                    'R4C_SHIM_MODE': mode, **(extra_env or {})})
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        args = ['--root', str(root), '--expect-sha256', digest, str(source)]
        argv = [str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT), *args] if which == 'old' else [str(FIXTURE), *args]
        before = _snapshot(root, os.lstat(source).st_ino)
        started = time.monotonic()

        def ignore():
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                   preexec_fn=ignore if ignore_sigint else None)
        pid_file = shims / 'shim.pid'
        if sigint:
            while not (pid_file.exists() and pid_file.read_text().strip()):
                if process.poll() is not None or time.monotonic() - started > 30:
                    detail = process.communicate()[1].decode('utf-8', 'replace') if process.poll() is not None else '時間切れ'
                    raise AssertionError('git の代役が起動しない: ' + detail.strip())
                time.sleep(0.02)
            if sigint == 'group':
                os.killpg(process.pid, signal.SIGINT)
            else:
                os.kill(process.pid, signal.SIGINT)
        try:
            out, err = process.communicate(stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, 9)
            process.communicate()
            raise AssertionError('git の代役の実行が時間切れになった')
        elapsed = time.monotonic() - started
        alive = None
        if pid_file.exists() and pid_file.read_text().strip():
            shim_pid = int(pid_file.read_text())
            # 殺された子は、親の終了の後に init が回収する(旧版は回収せずに終わる)。最大 10 秒、待って確かめる(機械が混んでいると、殺された子が終わるまでに時間がかかる)。
            for _ in range(500):
                try:
                    os.kill(shim_pid, 0)
                    alive = not _is_zombie(shim_pid)
                    if not alive:
                        break
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.02)
        _kill_group(process.pid)  # 代役が残した孫(後ろの sleep・trap の中の sleep)を片付ける。
        log = (shims / 'shim.log').read_text(encoding='utf-8', errors='surrogateescape') if (shims / 'shim.log').exists() else None
        root_text = str(root)
        return {'code': process.returncode, 'stdout': out.decode(), 'stderr': err.replace(os.fsencode(root), b'<ROOT>').decode(),
                'elapsed': elapsed, 'child_alive': alive, 'cleaned': (shims / 'cleaned').exists(),
                'log': log.replace(root_text, '<ROOT>') if log else None,
                'tree_before': before, 'tree_after': _snapshot(root, os.lstat(source).st_ino if source.exists() else None)}


def parse_shim_log(text):
    """代役の記録を、cwd・環境の集合・argv・stdin に分ける。bash などが足す値と OS の部品が足す値だけを集合から除く(PATH は除かない)。"""
    volatile = ('PWD=', 'OLDPWD=', 'SHLVL=', '_=', '__CF_USER_TEXT_ENCODING=', 'R4C_SHIM_', 'R4C_REAL_GIT=')
    lines = text.split('\n')
    cwd, rest = lines[0], lines[1:]
    at = next(index for index, line in enumerate(rest) if line.startswith('ARGV:'))
    environment_set = sorted(line for line in rest[:at] if line and not line.startswith(volatile))
    return {'cwd': cwd, 'env': environment_set, 'argv': rest[at][5:], 'stdin': '\n'.join(rest[at + 1:]).removeprefix('STDIN:').rstrip('\n')}


def _contract_child_environment(self):
    """E06: 子の cwd は保持した root・stdin は継承・環境は全 GIT_ を除いた値の集合に旧版の 4 変数を足したもの(PATH を含む)。"""
    extra = {'GIT_DIR': '/nonexistent-r4c', 'GIT_WORK_TREE': '/nonexistent-r4c', 'GIT_INDEX_FILE': '/nonexistent-r4c/index',
             'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.bare', 'GIT_CONFIG_VALUE_0': 'true', 'FOO': 'bar'}
    logs = {}
    for which in ('old', 'native'):
        def run():
            record = run_with_shim(which, 'log', extra_env=extra, stdin_data=b'STDIN-DATA')
            self.assertEqual(record['code'], 0, record['stderr'])
            logs[which] = parse_shim_log(record['log'])
        judge_run(which, 'e06:child-environment', run)
    old = logs['old']
    self.assertEqual(old['cwd'], '<ROOT>')
    self.assertEqual(old['stdin'], 'STDIN-DATA')
    names = [line.split('=', 1)[0] for line in old['env']]
    for wanted in ('GIT_NO_REPLACE_OBJECTS', 'GIT_NO_LAZY_FETCH', 'GIT_OPTIONAL_LOCKS', 'GIT_TERMINAL_PROMPT', 'FOO', 'PATH'):
        self.assertIn(wanted, names)
    for removed in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0'):
        self.assertNotIn(removed, names)
    self.assertEqual(logs['native'], old)


def _contract_git_deadline(self):
    """Git の 30 秒の期限を実際に確かめる: 終了値・文・子が残らないこと・所要時間。"""
    prefix = ['git', '--no-pager', '--no-replace-objects', '-c', 'core.quotePath=false', '-c', 'core.fsmonitor=',
              '-c', 'core.hooksPath=/dev/null', '-c', 'core.ignoreCase=false', '-c', 'core.splitIndex=false',
              '-c', 'core.ignoreStat=false', '-c', 'commit.gpgSign=false', '-c', 'push.gpgSign=false',
              '-c', 'filter.lfs.smudge=', '-c', 'filter.lfs.clean=', '-c', 'filter.lfs.process=',
              '-c', 'filter.lfs.required=false', '--literal-pathspecs', 'ls-files', '--error-unmatch', '--', '候補_例.md']
    expected_stderr = f"候補の採用を停止: Command '{prefix!r}' timed out after 30 seconds\n"
    for which in ('old', 'native'):
        def run():
            record = run_with_shim(which, 'hold')
            self.assertEqual((record['code'], record['stdout'], record['stderr']), (2, '', expected_stderr), which)
            self.assertFalse(record['child_alive'], which + ': 子が残った')
            self.assertTrue(30 <= record['elapsed'] < 45, f"{which}: 所要 {record['elapsed']:.1f} 秒")
            self.assertEqual(record['tree_after'], record['tree_before'], which)
        judge_run(which, 'e10:git-30-second-deadline', run)


def _last_line(text):
    return text.rstrip('\n').rsplit('\n', 1)[-1]


def _contract_sigint_with_a_running_child(self):
    """SIGINT(2026-11-11 22:31 の決定): 信号で終わる・最後の行 KeyboardInterrupt・標準出力は空・直接の子を残さない。
    旧 `Popen.communicate` は、SIGINT の後に子の終了を最大 0.25 秒待ってから kill する。次の場合を、旧版の実測と同じに合わせる:
    親だけに届く / 端末の Ctrl-C のようにグループ全体に届き、子が短い片付けをして自分で終わる / 片付けが 0.25 秒を超える /
    SIGINT を無視して起動された(親も子も止まらず、採用が終わる)。"""
    for which in ('old', 'native'):
        def process_only():
            record = run_with_shim(which, 'hold', sigint='process')
            self.assertEqual(record['code'], -signal.SIGINT, which)
            self.assertEqual(record['stdout'], '', which)
            self.assertEqual(_last_line(record['stderr']), 'KeyboardInterrupt', which)
            self.assertFalse(record['child_alive'], which + ': 子が残った')
            self.assertEqual(record['tree_after'], record['tree_before'], which)
        with self.subTest(f'親だけへの SIGINT・{which}'):
            judge_run(which, 'sigint:process', process_only)
    for mode, cleaned in (('trap-fast', True), ('trap-slow', False)):
        for which in ('old', 'native'):
            def group():
                record = run_with_shim(which, mode, sigint='group')
                self.assertEqual(record['code'], -signal.SIGINT, which)
                self.assertEqual(record['stdout'], '', which)
                self.assertEqual(_last_line(record['stderr']), 'KeyboardInterrupt', which)
                self.assertEqual(record['cleaned'], cleaned, which + ': 子の片付けが終わるまで待つかどうかが旧版と違う')
                self.assertFalse(record['child_alive'], which + ': 子が残った')
                self.assertEqual(record['tree_after'], record['tree_before'], which)
            with self.subTest(f'グループ全体への SIGINT・{mode}・{which}'):
                judge_run(which, f'sigint:group:{mode}', group)
    results = {}
    for which in ('old', 'native'):
        def ignored():
            record = run_with_shim(which, 'slow', sigint='group', ignore_sigint=True)
            results[which] = (record['code'], record['stdout'], record['stderr'], record['tree_after'])
            self.assertEqual(record['code'], 0, which + ': ' + record['stderr'])
        with self.subTest(f'SIGINT を無視して起動・{which}'):
            judge_run(which, 'sigint:ignored', ignored)
    if len(results) == 2:
        self.assertEqual(results['native'], results['old'])


Contract.test_e06_child_environment = _contract_child_environment
Contract.test_e10_git_30_second_deadline = _contract_git_deadline
Contract.test_sigint_with_a_running_git_child = _contract_sigint_with_a_running_child


# ---- 承認済みの差(個別期待・通常の一致件数とは分けて数える) ----

APPROVED_DIFFERENCE_RUN = []
RACE_RESULTS = {}
PROVISIONAL_RUN = []
UNIMPLEMENTED = {
    'E03 短読(read が要求より短く返す)': '普通のファイルの read は短く返さない。短読を作る入力が無いので、読み取りの塊の境目は platform の単体テストで固定する',
    'E02 親ディレクトリ自身の差替え(open の後)': '注入を root の直下の 1 成分だけに限る決定のため、root の外を書き換える差替えは作れない(root の symlink は E02 で比べる)',
    '標準エラーの書込みが失敗した後に、空きが戻って呼出履歴が出る場合': '旧版は失敗した例外の呼出履歴(固定名・行番号・例外の連鎖)を標準エラーへ書き直す。Rust は中身ではなく、溜めに残ることだけを模す(`stderr_failure`)。書けないままの場合(上限・満杯・閉じた pipe)は旧版と一致を確かめた。待たずに書く pipe の EAGAIN や、空きが戻る ENOSPC は作れる入力が無く未実施',
    'Mac での子の受け継いだ fd': '`/proc` が無い。信号の状態は platform の単体テスト(子が自分へ信号を送る)が Mac でも検査するが、fd の数の直接検査は Linux だけ',
    '承認済みの差: UTF-8 でない locale での引数とパスの解釈': 'この機械に該当の locale(例 ja_JP.eucJP)が無い。入っている機種でだけ実行できる',
}


def locale_installed(name):
    result = subprocess.run(['locale', '-a'], capture_output=True, text=True)
    normalize = lambda text: text.lower().replace('-', '')  # noqa: E731
    return normalize(name) in {normalize(line.strip()) for line in result.stdout.splitlines()}


def _contract_approved_differences(self):
    """利用者が承認した差(2026-11-11 22:24)。旧版と Rust の個別期待のそれぞれに一致することを確かめる。"""
    help_text, _ = fixed_text(['-h'])
    chars = help_text.decode()
    wide = [index for index, char in enumerate(chars) if ord(char) > 127]
    span = f'{wide[0]}-{wide[-1]}'
    base = environment()
    cases = [
        ('未知の codec 名', {'PYTHONIOENCODING': 'bogus-r4c-codec'},
         (1, b'', b'Fatal Python error: init_stdio_encoding: failed to get the Python codec name of the stdio encoding\n'
                  b'Python runtime state: core initialized\nLookupError: unknown encoding: bogus-r4c-codec\n' + CURRENT_THREAD)),
        ('文字コードでない codec 名', {'PYTHONIOENCODING': 'base64_codec'},
         (1, b'', b"Fatal Python error: init_sys_streams: can't initialize sys standard streams\nPython runtime state: core initialized\n"
                  b"LookupError: 'base64' is not a text encoding; use codecs.open() to handle arbitrary codecs\n" + CURRENT_THREAD)),
        ('PYTHONIOENCODING=ascii の help', {'PYTHONIOENCODING': 'ascii'},
         (1, b'', help_traceback(f"UnicodeEncodeError: 'ascii' codec can't encode characters in position {span}: ordinal not in range(128)"))),
        ('PYTHONIOENCODING=latin-1 の help', {'PYTHONIOENCODING': 'latin-1'},
         (1, b'', help_traceback(f"UnicodeEncodeError: 'latin-1' codec can't encode characters in position {span}: ordinal not in range(256)"))),
    ]
    for label, extra, old_expected in cases:
        with self.subTest(label):
            run_env = {key: value for key, value in base.items() if key not in ('PYTHONUTF8', 'LC_ALL', 'LANG')}
            run_env.update({'PYTHONUTF8': '1', **extra})
            if 'ascii' in label or 'latin-1' in label:
                run_env.pop('PYTHONUTF8', None)
            old, new = both(['-h'], run_env)
            self.assertEqual(old, old_expected, '旧版が個別期待と違う')
            self.assertEqual(new, (0, help_text, b''), 'Rust が個別期待と違う')
            APPROVED_DIFFERENCE_RUN.append(f"{label}(LANG={run_env.get('LANG')}・LC_ALL={run_env.get('LC_ALL')})")
            PROVISIONAL_RUN.append(label)
    for name in ('en_US.UTF-8', 'ja_JP.UTF-8'):
        label = f'strict になる UTF-8 の locale({name})で、UTF-8 でない名前の JSON を書けない'
        with self.subTest(label):
            if not locale_installed(name):
                NOT_RUN.append(label + ': locale が入っていない')
                continue
            records = {}
            for which in ('old', 'native'):
                with tempfile.TemporaryDirectory(prefix='r4c-locale-') as directory:
                    check_no_git_ancestor(directory)
                    root = Path(directory).resolve()
                    raw = os.fsencode(root) + b'/' + '候補_'.encode() + b'\xff.md'
                    with open(raw, 'wb') as stream:
                        stream.write(CANDIDATE.encode())
                    digest = hashlib.sha256(CANDIDATE.encode()).hexdigest()
                    argv = ([str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT)] if which == 'old' else [str(FIXTURE)])
                    run_env = {key: value for key, value in base.items() if key not in ('PYTHONUTF8', 'LC_ALL', 'LANG')}
                    run_env.update({'LC_ALL': name, 'LANG': name})
                    code, out, err = faulty([*argv, '--root', str(root), '--expect-sha256', digest, raw], run_env)
                    records[which] = (code, out, err, sorted(os.listdir(os.fsencode(root))))
            self.assertEqual(records['old'][0], 2, '旧版: 名前を含む JSON を書けず、改名の後に終了 2')
            self.assertIn(b'surrogates not allowed', records['old'][2])
            self.assertEqual(records['native'][0], 0, 'Rust: 生バイトの JSON を出して終了 0' + (f' ({UNCONNECTED}: 依存部品の未接続で止まった)' if UNCONNECTED.encode() in records['native'][2] else ''))
            self.assertIn(b'\xff', records['native'][1])
            self.assertEqual(records['old'][3], records['native'][3])
            APPROVED_DIFFERENCE_RUN.append(f'{label}(LC_ALL={name})')


def _contract_uncaught_tracebacks(self):
    """`--root=--`・`--expect-sha256=--` の TypeError の呼出履歴を、旧 Python 3.10.11 と全文で比べる(実行環境のパスだけ固定名)。"""
    main = (('adopt-candidate.py', 273, '<module>', 'sys.exit(main())'),
            ('adopt-candidate.py', 265, 'main', 'print(json.dumps(adopt(args.root, args.candidate, args.name, args.expect_sha256, args.accept), ensure_ascii=False))'))

    def text(frames, last):
        lines = ['Traceback (most recent call last):']
        for name, number, function, source in frames:
            lines += [f'  File "{name}", line {number}, in {function}', f'    {source}']
        return ('\n'.join(lines) + '\n' + last + '\n').encode()
    root_list = text(main + (('adopt-candidate.py', 190, 'adopt', 'root, source = Path(root).absolute(), Path(source).absolute()'),
                             ('pathlib.py', 960, '__new__', 'self = cls._from_parts(args)'),
                             ('pathlib.py', 594, '_from_parts', 'drv, root, parts = self._parse_args(args)'),
                             ('pathlib.py', 578, '_parse_args', 'a = os.fspath(a)')),
                     'TypeError: expected str, bytes or os.PathLike object, not list')
    digest_list = text(main + (('adopt-candidate.py', 200, 'adopt', 'warnings = validate_candidate(data, digest)'),
                               ('adopt-candidate.py', 74, 'validate_candidate',
                                "if not re.fullmatch('[0-9a-f]{64}', digest) or hashlib.sha256(data).hexdigest() != digest:"),
                               ('re.py', 195, 'fullmatch', 'return _compile(pattern, flags).fullmatch(string)')),
                       'TypeError: expected string or bytes-like object')
    for label, make_args, expected in (
        ('--root=--', lambda root, source: ['--root=--', '--expect-sha256', 'x', source], root_list),
        ('--expect-sha256=--', lambda root, source: ['--root', root, '--expect-sha256=--', source], digest_list),
    ):
        with self.subTest(label):
            results = {}
            for which in ('old', 'native'):
                side = Side(None)
                try:
                    args = make_args(str(side.root), str(side.source))
                    argv = ([str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT)] if which == 'old' else [str(FIXTURE)]) + args
                    result = faulty(argv, environment())
                    stdlib = stdlib_directory(OLD_PYTHON)
                    results[which] = (result[0], result[1],
                                      normalize_diagnostic(result[2], script=LEGACY_SCRIPT if which == 'old' else None, stdlib=stdlib))
                finally:
                    side.close()
            self.assertEqual(results['old'], (1, b'', expected), '旧版が全文の期待と違う')
            self.assertEqual(results['native'], results['old'], 'Rust が旧版と違う')


def make_deep_root(base, total_json_bytes):
    """JSON の長さがちょうど `total_json_bytes`(LF を含む)になる、深い root と候補の絶対パスを作る(PATH_MAX を超えてよい)。"""
    fixed = len('{"path": "') + len('", "tracked": false, "sha256": "') + 64 + len('", "warnings": []}\n')
    destination = '進行中_例.md'
    need = total_json_bytes - fixed - len(os.fsencode(base)) - 1 - len(destination.encode())
    fd = os.open(base, os.O_RDONLY)
    path = os.fsencode(base)
    try:
        while need > 1:
            step = min(200, need - 1)
            name = b'a' * step
            os.mkdir(name, dir_fd=fd)
            child = os.open(name, os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd, path, need = child, path + b'/' + name, need - step - 1
        handle = os.open('候補_例.md', os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
        os.write(handle, CANDIDATE.encode())
        os.close(handle)
    finally:
        os.close(fd)
    return path


# 本家 3.10.11 の実測(標準出力が通常ファイル・`st_blksize` 4096)。(JSON の長さ(LF を含む), 標準出力の上限) -> (終了値, 出る量, 終了時の文)。
# 'stop' は print の中の失敗(停止行・終了 2)、'unraisable' は終了時の flush の失敗(`Exception ignored`・終了 120)、'quiet' は何も出ない。
# 4096 を超え 8192 未満の溜めは、スクリプトの実行の直後の `flush_io()`(`PyRun_SimpleFileExFlags`。失敗を捨てる)で書込みが失敗してデータが
# 捨てられ、終了時の flush は成功する(終了 0・出力なし。`python -c` では起きない)。
LARGE_JSON_EXPECTED = {
    (4096, 0): (120, 0, 'unraisable'), (4097, 0): (0, 0, 'quiet'), (5000, 0): (0, 0, 'quiet'), (8191, 0): (0, 0, 'quiet'),
    (8192, 0): (2, 0, 'stop'), (8193, 0): (2, 0, 'stop'), (9808, 0): (2, 0, 'stop'),
    (4096, 5000): (0, 4096, 'quiet'), (4097, 5000): (0, 4097, 'quiet'), (6000, 5000): (120, 5000, 'unraisable'),
    (8190, 5000): (120, 5000, 'unraisable'), (8191, 5000): (120, 5000, 'unraisable'),
    (8192, 5000): (120, 5000, 'unraisable'), (8193, 5000): (120, 5000, 'unraisable'), (9808, 5000): (2, 5000, 'stop'),
    (8191, 8192): (0, 8191, 'quiet'), (8192, 8192): (0, 8192, 'quiet'), (8193, 8192): (120, 8192, 'unraisable'),
    (9808, 8192): (120, 8192, 'unraisable'), (20000, 8192): (2, 8192, 'stop'),
}


def _contract_large_json(self):
    """標準出力が通常ファイルで上限があるとき、JSON(+LF)の長さと上限の組ごとに、終了値・出る量・終了時の文を旧版の実測と同じにする。
    境目: BufferedWriter の大きさ 4096・TextIOWrapper の溜め 8192・部分書込み。長い path は PATH_MAX を超える深い入れ子で作る。"""
    digest = hashlib.sha256(CANDIDATE.encode()).hexdigest()
    stop_line = f'候補の採用を停止: {EFBIG[9:]}\n'.encode()
    with tempfile.TemporaryDirectory(prefix='r4c-blk-') as directory:
        if os.stat(directory).st_blksize != 4096:
            NOT_RUN.append('E13b 大きい JSON: 一時フォルダの st_blksize が 4096 でない(実測は 4096 の機械のもの)')
            return
    for (total, limit), (code, written, kind) in LARGE_JSON_EXPECTED.items():
        with self.subTest(f'JSON {total} バイト・標準出力の上限 {limit}'):
            observed, fulls = {}, {}
            for which in ('old', 'native'):
                with tempfile.TemporaryDirectory(prefix='r4c-long-') as directory:
                    check_no_git_ancestor(directory)
                    root = make_deep_root(str(Path(directory).resolve()), total)
                    argv = ([str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT)] if which == 'old' else [str(FIXTURE)])
                    result = faulty([*argv, '--root', root, '--expect-sha256', digest, root + '/候補_例.md'.encode()],
                                    environment(), stdout=('file', limit))
                    full = (b'{"path": "' + root + '/進行中_例.md'.encode() + b'", "tracked": false, "sha256": "'
                            + digest.encode() + b'", "warnings": []}\n')
                    self.assertEqual(len(full), total)
                    observed[which] = result
                    fulls[which] = full
            stderr = {'stop': stop_line, 'unraisable': UNRAISABLE % EFBIG.encode(), 'quiet': b''}[kind]
            judge_sides(f'e13b:{total}:{limit}', observed['old'], observed['native'],
                        {which: (code, fulls[which][:written], stderr) for which in fulls})


def pty_help_run(which):
    """標準出力を、本物の端末(疑似端末)にして help を流す。起動(旧 Python が `line_buffering` を決める)の後・書込みの前に、
    端末の主側を閉じる(回線を切る)。止まる窓は、旧版の起動器と Rust の example が同じ点 `start` で作り、制御用の pipe で同期する
    (sleep には頼らない)。標準エラーは pipe。戻り値は (終了値, 標準エラー)。"""
    import pty
    import select
    with tempfile.TemporaryDirectory(prefix='r4c-pty-') as directory:
        check_no_git_ancestor(directory)
        base = Path(directory).resolve()
        root = base / 'root'
        root.mkdir()
        spec_path = base / 'spec.json'
        spec_path.write_text(json.dumps({'rules': [{'point': 'start', 'do': [{'op': 'pause'}]}], 'body': ''}), encoding='utf-8')
        args = ['--root', str(root), '-h']
        if which == 'old':
            argv = [str(OLD_PYTHON), '-B', str(HARNESS_SCRIPT), str(LEGACY_SCRIPT), str(spec_path), *args]
        else:
            argv = [str(FIXTURE), *args]
        env = environment()
        if which == 'native':
            env['R4C_FAULT_SPEC'] = str(spec_path)
        notify_read, notify_write = os.pipe()
        wait_read, wait_write = os.pipe()
        env['R4C_FAULT_NOTIFY'], env['R4C_FAULT_WAIT'] = str(notify_write), str(wait_read)
        master, slave = pty.openpty()
        master_open = True
        try:
            process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=slave, stderr=subprocess.PIPE,
                                       pass_fds=(notify_write, wait_read), start_new_session=True)
            os.close(slave)
            os.close(notify_write)
            os.close(wait_read)
            ready, _, _ = select.select([notify_read], [], [], 30)
            if not ready or not os.read(notify_read, 16):
                raise AssertionError('起動の窓への到達の通知が無い')
            os.close(master)  # 回線を切る。以後、端末への書込みは失敗する。
            master_open = False
            os.close(wait_write)
            wait_write = -1
            _, err = process.communicate(timeout=30)
        finally:
            os.close(notify_read)
            if wait_write >= 0:
                os.close(wait_write)
            if master_open:
                os.close(master)
    return process.returncode, err


def _contract_terminal_stdout(self):
    """標準出力が本物の端末のとき(起動の後に回線が切れる): 旧 Python の `line_buffering=True` で、help の書込みの中で失敗する。
    旧版の実測と同じ終了値・標準エラー(呼出履歴を含む)にする。端末の挙動は OS で違うので、Linux だけで実行する。"""
    if not sys.platform.startswith('linux'):
        NOT_RUN.append('E13c 本物の端末の標準出力: 端末の挙動が OS で違う。Linux だけ実行(Mac の実測は未取得)')
        return
    expected = (120, help_traceback('OSError: [Errno 5] Input/output error') + UNRAISABLE % b'OSError: [Errno 5] Input/output error')
    old, new = pty_help_run('old'), pty_help_run('native')
    stdlib = stdlib_directory(OLD_PYTHON)
    def clean(result, script):
        stderr = result[1]
        if script is not None:
            # 起動器(old_harness.py)が `main()` を呼ぶ最上位の 1 フレームだけを、旧版を直接起動したときの最上位へ対応づける。
            frame = re.compile(rb'  File "' + re.escape(os.fsencode(HARNESS_SCRIPT)) + rb'", line \d+, in <module>\n    sys\.exit\(module\.main\(\)\)\n')
            stderr, count = frame.subn(b'  File "adopt-candidate.py", line 273, in <module>\n    sys.exit(main())\n', stderr)
            if count != 1:
                raise AssertionError('起動器の最上位のフレームが 1 つでない')
        return result[0], normalize_diagnostic(stderr, script=script, stdlib=stdlib)
    judge_sides('e13c:terminal', clean(old, HARNESS_SCRIPT_TARGET), clean(new, None), expected)
    PROVISIONAL_RUN.append('E13c 本物の端末の標準出力(help)')


LONG_STDERR_EXPECTED = {
    (3000, 'file0', False): (120, 0),
    (3000, 'file0', True): (1, 0),
    (3000, 'file100', False): (120, 100),
    (3000, 'file100', True): (1, 100),
    (3000, 'file5000', False): (2, 3236),
    (3000, 'file5000', True): (2, 3236),
    (3000, 'full', False): (120, 0),
    (3000, 'full', True): (1, 0),
    (3000, 'pipe', False): (2, 3236),
    (3000, 'pipe', True): (2, 3236),
    (5000, 'file0', False): (120, 0),
    (5000, 'file0', True): (1, 0),
    (5000, 'file100', False): (120, 100),
    (5000, 'file100', True): (1, 100),
    (5000, 'file5000', False): (120, 5000),
    (5000, 'file5000', True): (2, 5000),
    (5000, 'full', False): (120, 0),
    (5000, 'full', True): (1, 0),
    (5000, 'pipe', False): (2, 5236),
    (5000, 'pipe', True): (2, 5236),
    (9000, 'file0', False): (120, 0),
    (9000, 'file0', True): (1, 0),
    (9000, 'file100', False): (120, 100),
    (9000, 'file100', True): (1, 100),
    (9000, 'file5000', False): (120, 5000),
    (9000, 'file5000', True): (2, 5000),
    (9000, 'full', False): (120, 0),
    (9000, 'full', True): (1, 0),
    (9000, 'pipe', False): (2, 9236),
    (9000, 'pipe', True): (2, 9236),
}


def _contract_long_stderr(self):
    """長い usage と停止の文(未知の長い引数)を、書けない標準エラー(ファイルの上限 0・100・5000・/dev/full)と pipe へ出す。
    標準エラーは `line_buffering=True`(3.9 以降)で、改行の書込みは `BufferedWriter` の直接書込みの失敗や、溜めに残った分の
    終了時の失敗として、終了値(2・1・120)と出る量が変わる。旧版の実測(`st_blksize` 4096)と同じにする。"""
    with tempfile.TemporaryDirectory(prefix='r4c-blk-') as directory:
        if os.stat(directory).st_blksize != 4096:
            NOT_RUN.append('E18 長い標準エラー: 一時フォルダの st_blksize が 4096 でない(実測は 4096 の機械のもの)')
            return
    fulls = {}
    for (length, kind, unbuffered), (code, written) in LONG_STDERR_EXPECTED.items():
        if kind == 'full' and _unavailable({'stderr': 'full'}, f'E18 {length} /dev/full'):
            continue
        with self.subTest(f'長さ {length}・標準エラー {kind}・{"待たずに書く" if unbuffered else "溜める"}'):
            env = environment()
            if unbuffered:
                env['PYTHONUNBUFFERED'] = '1'
            stream = ('file', int(kind[4:])) if kind.startswith('file') else kind
            args = ['--root', '/r', '--expect-sha256', 'x', '/c', '--' + 'y' * length]
            if length not in fulls:
                fulls[length] = faulty([str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT), *args], environment(), stderr='pipe')[2]
            expected = (code, b'', fulls[length][:written])
            old = faulty([str(OLD_PYTHON), '-B', str(LEGACY_SCRIPT), *args], env, stderr=stream)
            new = faulty([str(FIXTURE), *args], env, stderr=stream)
            judge_sides(f'e18:{length}:{kind}:{unbuffered}', old, new, expected)


def race_kind(which):
    """強制した link/unlink の経路を、実際の SIGINT で割り込む(link ができた直後に送る)。時間に頼るので、結果は記録として扱う。
    種類 = (終了値, 残った名前, 標準出力があるか, 標準エラーの最後の行)。"""
    with fixed_umask(), tempfile.TemporaryDirectory(prefix='r4c-race-') as directory:
        check_no_git_ancestor(directory)
        base = Path(directory).resolve()
        root = base / 'root'
        root.mkdir()
        (root / '候補_例.md').write_text(CANDIDATE, encoding='utf-8')
        spec_path = base / 'spec.json'
        spec_path.write_text(json.dumps({'rules': [{'point': 'rename', 'missing': True}], 'body': CANDIDATE}), encoding='utf-8')
        digest = hashlib.sha256(CANDIDATE.encode()).hexdigest()
        args = ['--root', str(root), '--expect-sha256', digest, str(root / '候補_例.md')]
        env = environment()
        if which == 'old':
            argv = [str(OLD_PYTHON), '-B', str(HARNESS_SCRIPT), str(LEGACY_SCRIPT), str(spec_path), *args]
        else:
            argv = [str(FIXTURE), *args]
            env['R4C_FAULT_SPEC'] = str(spec_path)
        import time
        process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        started = time.monotonic()
        while process.poll() is None and time.monotonic() - started < 20:
            if (root / '進行中_例.md').exists():
                os.kill(process.pid, signal.SIGINT)
                break
        try:
            out, err = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, 9)
            process.communicate()
            raise AssertionError('順序を決めない SIGINT の実行が時間切れになった')
        _kill_group(process.pid)
        names = tuple(name for name in ('候補_例.md', '進行中_例.md') if (root / name).exists())
        last = err.replace(os.fsencode(root), b'<ROOT>').decode('utf-8', 'replace').rstrip('\n').rsplit('\n', 1)[-1]
        return (process.returncode, names, bool(out), last)


RACE_RUNS = 20
# 順序を決めたケースでは作れない旧版の種類。旧版の実標本で観察した。基準に固定で載せる(標本が出なくても、この種類は許す)。
RACE_STATIC_KINDS = {
    # 信号が届く前に採用が終わる。
    (0, ('進行中_例.md',), True, ''),
    # 標準出力の書込みの後・終了の前に届く(出力は出て、信号で終わり、標準エラーは空)。
    (-2, ('進行中_例.md',), True, ''),
    # 標準出力へ JSON を書いて print が返った後・SystemExit の前に SIGINT を受ける(KeyboardInterrupt の呼出履歴が出る)。
    # Rust も JSON を書いた後・終了の前に受ければ同じになる。順序を決めて止められる点が無いので、固定で載せる。
    (-2, ('進行中_例.md',), True, 'KeyboardInterrupt'),
}
RACE_ORDERED_IDS = ('A25-kill-between-link-and-unlink', 'A25b-sigint-between-link-and-unlink', 'A25c-sigint-before-link',
                    'A25d-sigint-after-unlink-keeps-the-destination', 'A25e-sigint-right-after-unlink-rolls-back',
                    'A25f-sigint-during-stat-after-link-leaves-both-names')


def race_reference_kinds():
    """順序を決めたケース(旧版の固定期待)から得た種類と、固定で載せる種類の和。時間に頼らない基準の集合。
    種類 = (終了値, 残った名前, 標準出力があるか, 標準エラーの最後の行)。kill(SIGKILL)は信号の最後の行が無いので除く。"""
    kinds = set(RACE_STATIC_KINDS)
    wanted = {item['id']: item for item in fault_scenarios()}
    for identifier in RACE_ORDERED_IDS:
        if identifier.startswith('A25-kill'):
            continue
        expect = wanted[identifier]['expect']
        names = tuple(name for name in ('候補_例.md', '進行中_例.md') if name in expect['tree'])
        kinds.add((expect['code'], names, bool(expect['stdout']), expect['stderr'].rstrip('\n').rsplit('\n', 1)[-1]))
    return kinds


def _contract_sigint_race(self):
    """順序を決めない SIGINT を両側で各 20 回流す。両側の標本の種類と回数は記録だけ(時間で変わる)。
    判定は、時間に頼らない基準の集合(順序を決めた旧版の種類と固定の種類)と旧版の標本の種類の和の外に、
    Rust の種類が出ないこと。"""
    from collections import Counter
    reference = race_reference_kinds()
    counts = {}
    for which in ('old', 'native'):
        counts[which] = Counter(race_kind(which) for _ in range(RACE_RUNS))
        RACE_RESULTS[which] = {json.dumps(list(kind), ensure_ascii=False): number for kind, number in counts[which].items()}
    RACE_RESULTS['reference'] = sorted(json.dumps(list(kind), ensure_ascii=False) for kind in reference)
    extra = set(counts['native']) - reference - set(counts['old'])
    if extra:
        raise AssertionError(f'Rust の結果の種類が基準の集合の外: {sorted(extra)}'
                             + (f' ({UNCONNECTED}: 依存部品の未接続で止まった)' if any(UNCONNECTED in kind[3] for kind in extra) else ''))


Contract.test_e17_sigint_race = _contract_sigint_race
Contract.test_e18_long_stderr = _contract_long_stderr
Contract.test_e05_e12_injected_failures = _contract_injection
Contract.test_approved_differences = _contract_approved_differences
Contract.test_uncaught_tracebacks = _contract_uncaught_tracebacks
Contract.test_e13b_large_json_in_print = _contract_large_json
Contract.test_e13c_terminal_stdout = _contract_terminal_stdout
Contract.test_e13_success_output_faults = _contract_e13
Contract.test_e06_git_calls = _contract_git_calls


def collect_evidence(python, fixture):
    """機種で変わる旧版の診断の採取(CI の各機種のログへ残す。固定期待は、この証拠を見てから作る)。

    版・OS・CPU・実行物のハッシュ・標準ライブラリの SHA-256・errno の文と、起動の失敗の各条件(標準入力・標準出力・
    標準エラーがディレクトリ)を各 3 回、正常対照、help の出力の失敗の生出力(旧版と Rust)を、16 進で記録する。
    """
    import platform
    stdlib = stdlib_directory(python)
    sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()  # noqa: E731
    version = run_command([str(python), '-I', '-B', '-c', 'import sys; print(sys.version)'], cwd=ROOT,
                          env={'PATH': os.environ.get('PATH', '')}, timeout=30).stdout.decode().strip()
    evidence = {
        'platform': platform.platform(), 'system': platform.system(), 'machine': platform.machine(),
        'libc': list(platform.libc_ver()), 'python': version,
        'stdlib_sha256': {name: sha(Path(stdlib) / name) for name in UPSTREAM_STDLIB_SHA256},
        'errno': {str(n): os.strerror(n) for n in (1, 2, 5, 13, 17, 18, 20, 21, 27, 28, 32, errno_module.ELOOP)},
        'fixture': {'sha256': sha(fixture), 'size': Path(fixture).stat().st_size},
        'runs': {},
    }
    env = environment()
    conditions = [('control-help', ['-h'], {}), ('stdin-directory', ['-h'], {'stdin': 'dir'}),
                  ('stdout-directory', ['-h'], {'stdout': 'dir'}), ('stderr-directory', ['-h'], {'stderr': 'dir'}),
                  ('help-output-failure-buffered', ['-h'], {'stdout': ('file', 0)}),
                  ('help-output-failure-unbuffered', ['-h'], {'stdout': ('file', 0), 'unbuffered': True})]
    if os.path.exists('/dev/full'):
        conditions.append(('help-output-failure-dev-full', ['-h'], {'stdout': 'full', 'unbuffered': True}))
    for name, args, streams in conditions:
        streams = dict(streams)
        run_env = {**env, 'PYTHONUNBUFFERED': '1'} if streams.pop('unbuffered', False) else env
        times = 1 if name.startswith('help-output') or name == 'control-help' else 3
        evidence['runs'][name] = {'old': [], 'native': []}
        for _ in range(times):
            for which, argv in (('old', [str(python), '-B', str(LEGACY_SCRIPT), *args]), ('native', [str(fixture), *args])):
                code, out, err = faulty(argv, run_env, **streams)
                evidence['runs'][name][which].append({'code': code, 'stdout': out.hex(), 'stderr': err.hex()})
    return evidence


def print_tally():
    """固定期待との照合の結果を、旧版の側と Rust の側で分けて出す(失敗で止まる回も出す)。"""
    print(f"ADOPT: 旧版の固定期待の照合: 一致 {TALLY['old_match']} 件・不一致 {len(TALLY['old_mismatch'])} 件", flush=True)
    for name in TALLY['old_mismatch']:
        print('ADOPT: 旧版が固定期待と違う: ' + name, flush=True)
    print(f"ADOPT: Rust の固定期待の一致: 一致 {TALLY['native_match']} 件・依存部品(#471・#472)の未接続で不通過 "
          f"{len(TALLY['native_unconnected'])} 件・それ以外の不一致 {len(TALLY['native_mismatch'])} 件", flush=True)
    for name in TALLY['native_mismatch']:
        print('ADOPT: Rust が固定期待と違う(未接続以外): ' + name, flush=True)
    print('ADOPT: 未接続で通らなかった検査の ID: ' + ', '.join(sorted(set(TALLY['native_unconnected']))), flush=True)
    print('ADOPT: 未実施の ID: ' + ', '.join(sorted(set(NOT_RUN) | set(UNIMPLEMENTED))), flush=True)
    print('ADOPT: 順序を決めない SIGINT の基準の集合(順序を決めたケース + 固定): ' + (json.dumps(RACE_RESULTS['reference'], ensure_ascii=False) if 'reference' in RACE_RESULTS else '未実行'), flush=True)
    for which in ('old', 'native'):
        print(f"ADOPT: 順序を決めない SIGINT の結果の種類({'旧版' if which == 'old' else 'Rust'}): " + (json.dumps(RACE_RESULTS.get(which), ensure_ascii=False, sort_keys=True) if which in RACE_RESULTS else '未実行'), flush=True)
    print(f'ADOPT: 未実施(成功に数えない) {len(NOT_RUN)} 件・仮の期待 {len(PROVISIONAL_RUN)} 件・承認済みの差 {len(APPROVED_DIFFERENCE_RUN)} 件(通常の一致件数とは別)', flush=True)


def main(argv=None):
    global FIXTURE, OLD_PYTHON
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--old-python', default=sys.executable)
    parser.add_argument('--evidence', action='store_true', help='機種で変わる旧版の診断の生出力を `ADOPT-EVIDENCE` の行で出す')
    args = parser.parse_args(argv)
    FIXTURE = fixture_path(args.fixture)
    OLD_PYTHON = Path(args.old_python)
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise ValueError('R4C比較はPython3.10系・非rootで実行してください')
    # 比較器そのものの自己検査を先に通す(R4B と同じ。基準の機械の確認も、この中の検査を含む)。
    spec = importlib.util.spec_from_file_location('r4c_comparator_tests', Path(__file__).with_name('test_adopt_candidate.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    run_suite(unittest.defaultTestLoader.loadTestsFromModule(module))
    version = verify_old_python(OLD_PYTHON)
    errnos = verify_errno_baseline(OLD_PYTHON)
    if args.evidence:
        print('ADOPT-EVIDENCE ' + json.dumps(collect_evidence(OLD_PYTHON, FIXTURE), sort_keys=True), flush=True)
    rows, groups = verify_map()
    try:
        count = run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Contract), breakdown=True)
    finally:
        print_tally()
    notes = [f'未実施(成功に数えない): {name}' for name in NOT_RUN] + [f'未実施(成功に数えない): {name}: {reason}' for name, reason in UNIMPLEMENTED.items()]
    head = f'旧Python {version}・errno {errnos} 件の基準・観点 {rows}+{groups}・検査 {count} 件'
    detail = f'承認済みの差 {len(APPROVED_DIFFERENCE_RUN)} 件(通常の一致件数とは別)・仮の期待 {len(PROVISIONAL_RUN)} 件・未実施 {len(notes)} 件'
    for line in notes:
        print('ADOPT: ' + line)
    print('ADOPT: 依存部品への到達: 実部品の呼出しの検証は #471・#472 の接続後(E07 の結果の差し替えは、依存部品に届いた件数に数えない)')
    for name in APPROVED_DIFFERENCE_RUN:
        print('ADOPT: 承認済みの差: ' + name)
    # 未実施・仮の期待が 1 件でもあるときは「合格」と出さない。
    word = '検査は通過(ただし' + detail + 'のため、合格とは出さない)' if notes or PROVISIONAL_RUN else '合格'
    print(f'ADOPT: {word} ({head}・{detail})')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AssertionError, ValueError, OSError) as error:
        print(f'ADOPT: 不合格: {error}', file=sys.stderr)
        sys.exit(1)
