"""テストの共通の補助: 旧版と Rust 版の実行結果とファイルの比較(#442)。

テストではない(名前を test_ で始めないので、discover は拾わない)。
discover の形(`python3 -m unittest discover -s scripts -p <ファイル>`)と run-tests.py では、
scripts/ が読み込みの起点になるので `import support_native` で読み込める。
dotted の形(`python3 -m unittest scripts.<名前>`)で流すファイルは、次のように読み込む:

    try:
        import support_native
    except ModuleNotFoundError as error:
        if error.name != 'support_native':
            raise
        from scripts import support_native

標準ライブラリだけを使い、ほかの共通の補助を読み込まない。
Git 状態や子プロセスの不在の確かめは、テストや CI の呼び出し側で既存の補助を組み合わせる。
probe には旧版が無いので、この補助のテストを旧版との同等性の確認とは扱わない。
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
from typing import Callable, Mapping, Sequence


class NativeBinaryError(RuntimeError):
    """テスト用の Rust 実行ファイルを使えない。skip には変換しない。"""


def require_native_binary() -> Path:
    """DEV_WORKFLOW_TEST_NATIVE_BINARY が指す実行可能な通常ファイルの絶対パスを返す。

    未設定・空・不在・実行不能なら NativeBinaryError。PATH 検索や配布版への切り替えはしない。
    相対パスは呼び出し時の作業場所を基準にする。検査のためには起動しない。
    形式やインタープリタの不備など、起動時にしか分からない失敗は run_command の OSError になる。
    """
    name = 'DEV_WORKFLOW_TEST_NATIVE_BINARY'
    value = os.environ.get(name)
    if value is None:
        raise NativeBinaryError(f'{name} が未設定です')
    if value == '':
        raise NativeBinaryError(f'{name} が空です')
    try:
        path = Path(value).resolve(strict=True)
        executable = path.is_file() and os.access(path, os.X_OK)
    except (OSError, RuntimeError, ValueError) as error:
        raise NativeBinaryError(f'{name} のファイルを参照できません: {value}') from error
    if not executable:
        raise NativeBinaryError(f'{name} は実行可能な通常ファイルではありません: {value}')
    return path


@dataclass(frozen=True)
class RunResult:
    """終了コードと、加工していない出力。時間切れの終了コードも実際の値を保持する。"""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


def _signal_group(process, number):
    # 親が先に終わっていても、出力のパイプを持つ子が同じ群に残ることがある。
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass


def _drain_after_kill(process):
    """群を止めた後もパイプの EOF を待ち続けず、直接の子だけを回収する。"""
    try:
        return process.communicate(timeout=1)
    except subprocess.TimeoutExpired as error:
        # communicate の再試行で得る output/stderr は、最初から読めた分の全体。
        stdout, stderr = error.output or b'', error.stderr or b''
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        process.kill()
        process.wait()
        return stdout, stderr


def run_command(argv, *, cwd, env=None, input=None, timeout=60) -> RunResult:
    """POSIX の別プロセスグループで argv を起動し、RunResult を返す。

    argv は引数の配列。シェルは通さない。cwd は必須。env=None は現在の環境を引き継ぎ、
    辞書は環境全体を置き換える(空の辞書も有効)。input は bytes か None(None は EOF)。
    終了コードが非 0 でも結果を返す。起動不能は OSError をそのまま投げる。
    timeout は秒数(None は無制限)。時間切れなら群へ TERM を送り、最大 1 秒の後に KILL を送る。
    KILL 後の読み取りも最大 1 秒。終わらなければパイプを閉じ、読めた分だけを返す。
    直接の子を wait で回収してから timed_out=True を返す。
    孫以降はその親か OS が回収する。呼び出し側が Linux の subreaper(孤児を引き取る処理)なら、
    引き取った子の wait は呼び出し側が行う。群を抜けた子は停止の対象外。
    正常終了時の背景プロセスの残存は、呼び出し側が既存の補助で確かめる。
    """
    if os.name != 'posix':
        raise NotImplementedError('run_command は POSIX のプロセスグループが必要です')
    if isinstance(argv, (str, bytes, os.PathLike)):
        raise TypeError('argv は引数の配列にしてください')
    if input is not None and not isinstance(input, bytes):
        raise TypeError('input は bytes か None にしてください')
    if timeout is not None and timeout < 0:
        raise ValueError('timeout は 0 以上か None にしてください')
    with subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True,
                          stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            timed_out = False
            try:
                stdout, stderr = process.communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _signal_group(process, signal.SIGTERM)
                try:
                    # TERM を受けた親が子を回収する時間を設ける。
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                # パイプを閉じた子も止めるため、communicate が終わった場合も群へ送る。
                _signal_group(process, signal.SIGKILL)
                stdout, stderr = _drain_after_kill(process)
            return RunResult(process.returncode, stdout, stderr, timed_out)
        except BaseException:
            _signal_group(process, signal.SIGKILL)
            _drain_after_kill(process)
            raise


def snapshot_tree(path) -> dict:
    """相対パス(POSIX 表記)から {kind, mode, sha256} への辞書を返す。

    根は '.'。mode は stat.S_IMODE の整数(実行権限を含む)。通常ファイルの sha256 は内容、
    symlink の sha256 はリンク先の名前のバイト列(os.fsencode)から計算する。リンク先を辿らない。
    directory・fifo・socket の sha256 は None。device は種類と機器番号から計算する。
    時刻や所有者は記録しない。隠しファイルも含め、除外や出力の正規化は行わない。
    不在は空の辞書。読み取り不能など、それ以外の異常は例外にする。
    """
    root = Path(path)
    result = {}

    def visit(item, relative):
        info = item.lstat()
        digest = None
        if stat.S_ISLNK(info.st_mode):
            kind = 'symlink'
            digest = hashlib.sha256(os.fsencode(os.readlink(item))).hexdigest()
        elif stat.S_ISREG(info.st_mode):
            kind = 'file'
            hasher = hashlib.sha256()
            with item.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        elif stat.S_ISDIR(info.st_mode):
            kind = 'directory'
        elif stat.S_ISFIFO(info.st_mode):
            kind = 'fifo'
        elif stat.S_ISSOCK(info.st_mode):
            kind = 'socket'
        elif stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode):
            kind = 'character' if stat.S_ISCHR(info.st_mode) else 'block'
            digest = hashlib.sha256(str(info.st_rdev).encode('ascii')).hexdigest()
        else:
            raise ValueError(f'扱えないファイルの種類です: {item}')
        result[relative] = {'kind': kind, 'mode': stat.S_IMODE(info.st_mode), 'sha256': digest}
        if kind == 'directory':
            for child in sorted(item.iterdir()):
                visit(child, child.relative_to(root).as_posix())

    try:
        root.lstat()
    except FileNotFoundError:
        return result
    visit(root, '.')
    return result


@dataclass(frozen=True)
class ComparisonResult:
    """生の結果・前後のファイルの記録・検査用に残す場所・許可されていない差の項目名。"""

    legacy: RunResult
    native: RunResult
    legacy_cwd: Path
    native_cwd: Path
    before: dict
    legacy_tree: dict
    native_tree: dict
    differences: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.differences


def compare_runs(legacy_argv: Sequence, native_argv: Sequence, *, initial_tree, work_dir,
                 env=None, input=None, timeout=60,
                 allow_differences: Mapping[str, Callable[[object, object], bool]] | None = None
                 ) -> ComparisonResult:
    """同じ初期状態の 2 つのフォルダで実行し、結果と副作用を比較する。

    initial_tree は実在する通常のフォルダ。work_dir はまだ存在しない、初期状態の外の場所。
    work_dir/legacy と work_dir/native に copytree(symlinks=True) で独立にコピーし、
    内容・権限・リンクが初期状態と等しいことを確認してから順に起動する。
    argv・env・input・timeout は run_command と同じ。argv の相対パスは各コピーを基準にする。
    両方に同じ環境のコピーと入力を渡す。__pycache__ 等を除く場合は呼び出し側で初期状態を作る。
    絶対リンクや外へ出る相対リンクの参照先はコピーしない。初期状態のリンクやコマンドが
    コピーの外を変更しないことは呼び出し側の責務。この関数は実行を隔離する仕組みではない。

    ComparisonResult.matches は差が無く、両方とも時間切れでない場合だけ True。
    differences の項目名は returncode・stdout・stderr・tree:<相対パス>、
    時間切れは legacy.timed_out・native.timed_out。before と各 *_tree で増減・変更を調べられる。
    失敗時もコピーを消さない。返した *_cwd で Git 状態や子の記録を調べ、呼び出し側で片付ける。

    allow_differences は、この比較だけで差を認める項目名と述語の辞書。
    例: {'stdout': lambda old, new: old == b'old\\n' and new == b'new\\n'}。
    述語には生の旧値と新値を渡す。tree の値は snapshot_tree の項目(不在は None)。
    True の差だけを認め、元の結果は変えない。ワイルドカードや一括の時刻・絶対パス除去はしない。
    時間切れは許可できない。項目名の形式が不正な場合や述語でない値は実行前に失敗する。
    """
    allowances = dict(allow_differences or {})
    for key, predicate in allowances.items():
        if (key not in ('returncode', 'stdout', 'stderr') and
                not (isinstance(key, str) and key.startswith('tree:') and key[5:])):
            raise ValueError(f'差を許す項目名が不正です: {key}')
        if not callable(predicate):
            raise TypeError(f'差を許す判定は関数にしてください: {key}')
    source = Path(initial_tree).resolve(strict=True)
    work = Path(work_dir).resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if work == source or source in work.parents:
        raise ValueError('work_dir は initial_tree の外にしてください')
    before = snapshot_tree(source)
    work.mkdir()  # 既存の結果を上書きしない。
    legacy_cwd, native_cwd = work / 'legacy', work / 'native'
    for destination in (legacy_cwd, native_cwd):
        shutil.copytree(source, destination, symlinks=True)
        if snapshot_tree(destination) != before:
            raise ValueError(f'コピーの初期状態が一致しません: {destination}')
    environ = dict(os.environ if env is None else env)
    legacy = run_command(legacy_argv, cwd=legacy_cwd, env=environ, input=input, timeout=timeout)
    legacy_tree = snapshot_tree(legacy_cwd)
    native = run_command(native_argv, cwd=native_cwd, env=environ, input=input, timeout=timeout)
    native_tree = snapshot_tree(native_cwd)
    differences = []

    def compare(key, old, new):
        if old != new and not (key in allowances and allowances[key](old, new)):
            differences.append(key)

    for side, result in (('legacy', legacy), ('native', native)):
        if result.timed_out:
            differences.append(f'{side}.timed_out')
    for field in ('returncode', 'stdout', 'stderr'):
        compare(field, getattr(legacy, field), getattr(native, field))
    for relative in sorted(legacy_tree.keys() | native_tree.keys()):
        compare(f'tree:{relative}', legacy_tree.get(relative), native_tree.get(relative))
    return ComparisonResult(legacy, native, legacy_cwd, native_cwd, before,
                            legacy_tree, native_tree, tuple(differences))
