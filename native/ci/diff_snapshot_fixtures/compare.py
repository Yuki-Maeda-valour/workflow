"""S3D比較の判定。出力本文を失敗診断へ転記しない。"""
from collections import Counter
from dataclasses import dataclass
import datetime
import hashlib
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes
    report: bytes | None = None
    patch: bytes | None = None
    started: float = 0.0
    ended: float = 0.0
    timed_out: bool = False
    case: str = ''


def binary_path(value):
    if not value or not Path(value).is_absolute():
        raise ValueError('DEV_WORKFLOW_TEST_DIFF_SNAPSHOT_BINARYに絶対パスが必要です')
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('指定したRust実行物が存在しないか、実行できません')
    return path


def exact_ids(expected, actual):
    expected, actual = list(expected), list(actual)
    if len(set(expected)) != len(expected):
        raise AssertionError('予定IDが重複しています')
    if len(set(actual)) != len(actual):
        raise AssertionError('実行IDが重複しています')
    missing, extra = set(expected) - set(actual), set(actual) - set(expected)
    if missing or extra:
        raise AssertionError(f'ケースID不一致: 未到達={sorted(missing)} 予定外={sorted(extra)}')


def no_secrets(result, markers):
    for field in ('stdout', 'stderr', 'report', 'patch'):
        value = getattr(result, field)
        if value is not None and any(marker and marker in value for marker in markers):
            raise AssertionError(f'機密markerが{field}へ混入しました')


def report_time(value, started, ended):
    """生成日時の1行だけを、実行区間内か確かめてから置換する。"""
    if value is None:
        return None
    prefix = '- 生成日時: '.encode()
    header, separator, body = value.partition(b'\n## ')
    matches = list(re.finditer(rb'^' + re.escape(prefix) + rb'([^\n]*)$', header, re.M))
    if len(matches) != 1:
        raise AssertionError('生成日時がヘッダーに1行ありません')
    match = matches[0]
    try:
        stamp = datetime.datetime.strptime(match.group(1).decode('ascii'), '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, UnicodeError) as error:
        raise AssertionError('生成日時の書式が違います') from error
    if not int(started) <= stamp <= int(ended):
        raise AssertionError('生成日時が実行区間の外です')
    return header[:match.start(1)] + b'<generated-time>' + header[match.end(1):] + separator + body


def same_result(old, new, *, markers=(), generated_report=True):
    for result in (old, new):
        no_secrets(result, markers)
        if result.timed_out:
            raise AssertionError('比較対象が時間切れです')
    for field in ('returncode', 'stdout', 'stderr', 'patch'):
        if getattr(old, field) != getattr(new, field):
            if field == 'stderr' and version_masked(old, new):
                continue
            raise AssertionError(f'{field}が旧版と一致しません')
    reports = [result.report for result in (old, new)]
    if generated_report:
        reports = [report_time(result.report, result.started, result.ended) for result in (old, new)]
    if reports[0] != reports[1]:
        raise AssertionError('Markdownが旧版と一致しません')


def fixed(result, code, *, stdout=None, stderr=None):
    if result.timed_out or result.returncode != code:
        raise AssertionError(f'固定の終了値と一致しません: expected={code} actual={result.returncode} timeout={result.timed_out}')
    if stdout is not None and result.stdout != stdout:
        raise AssertionError('固定のstdoutと一致しません')
    if stderr is not None and result.stderr != stderr:
        raise AssertionError('固定のstderrと一致しません')


# 版に依る欄(Rustが作る道具の文・bash自身の文・SHLVL・bashの起動時の警告)。
# manifest(cases.jsonのversion_fields)が欄ごとに指定し、比較器は旧版の出力の印と、ケースが宣言した欄から
# 観測した集合が、manifestと一致することを確かめる(印の抜けと誤りを失敗にする)。
VERSION_MARKERS = re.compile(rb'^(?:(?:mktemp|mkdir|cp|mv|rm|find|cat): |(?:[^\n]*/)?diff-snapshot\.sh: line \d+: |bash: |(?:Terminated|Hangup)$)', re.M)
VERSION_KINDS = ('tool-message', 'bash-message', 'SHLVL', 'bash-startup-warning', 'LINENO')
# 観測: 群(--onlyの名前) → 欄の名前(ケースのフォルダ名か「ID:SHLVL」) → 欄の種類。
VERSION_HITS = {}
CURRENT_SCOPE = ['']
# 基準でない機械では、manifestの欄だけを比べずに記録する(ほかの欄は比べる)。
VERSION_TOLERANT = {'names': None}
VERSION_MASKED = set()


class NotRun(Exception):
    """Macで作れないケース(straceが無い)。未実施として記録し、成功に数えない。"""
    def __init__(self, ids, reason):
        super().__init__(reason)
        self.ids, self.reason = list(ids), reason


def version_kinds(stderr):
    kinds = set()
    for match in VERSION_MARKERS.finditer(stderr or b''):
        line = match.group(0)
        if line.endswith((b'mktemp: ', b'mkdir: ', b'cp: ', b'mv: ', b'rm: ', b'find: ', b'cat: ')) and b'/' not in line:
            kinds.add('tool-message')
        elif (stderr or b'')[match.start():].startswith(b'bash: warning: setlocale: '):
            kinds.add('bash-startup-warning')
        else:
            kinds.add('bash-message')
    return kinds


def note_field(name, kind):
    if kind not in VERSION_KINDS:
        raise AssertionError(f'未知の版に依る欄の種類です: {kind}')
    VERSION_HITS.setdefault(CURRENT_SCOPE[0], {}).setdefault(name, set()).add(kind)


def note_versions(argv, cwd, stderr):
    if len(argv) > 1 and str(argv[0]).rsplit('/', 1)[-1] == 'bash' and str(argv[1]).endswith('diff-snapshot.sh'):
        for kind in version_kinds(stderr):
            note_field(Path(cwd).name, kind)


def version_masked(old, new):
    """基準でない機械で、manifestの欄を持つケースの標準エラーを、版に依る行を除いて比べる。"""
    names = VERSION_TOLERANT['names']
    if names is None or not old.case or old.case != new.case or old.case not in names:
        return False
    def strip(value):
        return b''.join(line for line in (value or b'').splitlines(keepends=True)
                        if not VERSION_MARKERS.match(line) and not line.startswith(b'bash: warning: setlocale: '))
    if strip(old.stderr) != strip(new.stderr):
        return False
    VERSION_MASKED.add(old.case)
    return True


def check_versions(manifest, scopes):
    """走らせた群の観測が、manifestの版に依る欄と一致することを確かめる。"""
    problems = []
    for scope in scopes:
        expected = {name: set(kinds) for name, kinds in manifest.get(scope, {}).items()}
        observed = VERSION_HITS.get(scope, {})
        for name in sorted(set(expected) | set(observed)):
            if expected.get(name, set()) != observed.get(name, set()):
                problems.append(f'{scope}/{name}: manifest={sorted(expected.get(name, ()))} '
                                f'observed={sorted(observed.get(name, ()))}')
    if problems:
        raise AssertionError('版に依る欄の印の抜けか誤りです: ' + '; '.join(problems))


def run(argv, *, cwd, env, report=None, patch=None, timeout=60, pass_fds=()):
    from . import closed_env
    closed_env.guard(os.environ if env is None else env, cwd)
    started = time.time()
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, pass_fds=pass_fds)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.output or b'', error.stderr or b''
            process.stdout.close()
            process.stderr.close()
            process.kill()
            process.wait()
    ended = time.time()
    note_versions(argv, cwd, stderr)
    def content(path):
        return Path(path).read_bytes() if path is not None and Path(path).is_file() else None
    return Result(process.returncode, stdout, stderr, content(report), content(patch), started, ended, timed_out,
                  Path(cwd).name)


def file_state(path):
    """観測は実行開始前/回収後のみ。atimeは読取りで動くので契約に含めない。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    body = None
    if stat.S_ISLNK(info.st_mode):
        body = os.fsencode(os.readlink(path))
    elif stat.S_ISREG(info.st_mode):
        try:
            with open(path, 'rb') as stream:
                body = hashlib.sha256(stream.read()).digest()
        except PermissionError:
            body = ('unreadable',)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, body)


def unchanged(before, after):
    if before != after:
        raise AssertionError('index・設定・入力の実体または内容が変わりました')


@dataclass(frozen=True)
class AuditEvent:
    phase: str
    operation: str
    path: bytes = b''
    device: int | None = None
    inode: int | None = None
    fd: int | None = None
    byte_count: int = 0
    child_pid: int | None = None
    argv: tuple[bytes, ...] = ()
    executable: bytes = b''


def audit_reads(old, new, secrets, *, git_executables):
    """Gitの実行列も読む量も増やさない。Git以外の本文操作を拒む。"""
    secrets, git_executables = set(secrets), set(git_executables)
    if not secrets or not git_executables:
        raise AssertionError('読取り監査の対象が空です')
    def inspect(events, rust):
        calls, reads = [], Counter()
        for event in events:
            if event.operation not in {'Open', 'Read', 'Hash', 'Spawn', 'Map', 'Close', 'Dup'}:
                raise AssertionError('読取り監査に未知の操作があります')
            if event.operation == 'Spawn' and event.executable in git_executables:
                calls.append((event.phase, event.executable, event.argv))
            is_secret = event.path in secrets or (event.device, event.inode) in secrets
            if not is_secret or event.operation not in {'Open', 'Read', 'Hash', 'Map'}:
                continue
            if event.executable not in git_executables:
                raise AssertionError('Git比較以外から機密本文へアクセスしました')
            if event.operation == 'Hash':
                raise AssertionError('機密本文のhash処理を追加しました')
            if event.byte_count < 0:
                raise AssertionError('読取りbyte数が不正です')
            reads[(event.phase, event.executable, event.path, event.operation)] += (
                event.byte_count if event.operation in {'Read', 'Map'} else 1)
        return calls, reads
    old_calls, old_reads = inspect(old, False)
    new_calls, new_reads = inspect(new, True)
    if old_calls != new_calls:
        raise AssertionError('Git比較の呼出し・順序・引数が旧版と一致しません')
    if any(value > old_reads[key] for key, value in new_reads.items()):
        raise AssertionError('機密本文の読取りが旧版より増えました')
