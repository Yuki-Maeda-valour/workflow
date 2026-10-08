#!/usr/bin/env python3
"""品質コマンドを監督し、回収の確認が取れてから呼出元へ返す。

Linux は既存の専用 subreaper、他 POSIX は未回収の anchor が保持する群を使う。
群外への切離しと同じ利用者権限による監督への攻撃を完全に隔離するものではない。
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import select
import signal
import stat
import subprocess
import sys
import time


class RecoveryError(Exception):
    """不在を証明できない。呼出元はコピーを保持して停止する。"""


class Interrupted(RecoveryError):
    pass


class LaunchError(Exception):
    pass


def launch_monitor(*args, **kwargs):
    try:
        return subprocess.Popen(*args, **kwargs)
    except OSError as exc:
        # Popen が返らない起動失敗では、作成途中の直子も Popen が wait する。
        raise LaunchError() from exc


class StopSignals:
    def __init__(self):
        self.number = 0
        self.previous = {}

    def receive(self, number, _frame):
        self.number = self.number or number

    def __enter__(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.previous[number] = signal.signal(number, self.receive)
        return self

    def __exit__(self, *_):
        for number, handler in self.previous.items():
            signal.signal(number, handler)

    def check(self):
        if self.number:
            raise Interrupted(f"終了通知 {self.number} を受けた")


def read_result(path, nonce):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise RecoveryError("回収結果の型または大きさが不正")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise RecoveryError("回収結果が大きすぎる")
        value = json.loads(raw)
    finally:
        os.close(fd)
    if (not isinstance(value, dict) or value.get('nonce') != nonce or value.get('clean') is not True
            or type(value.get('returncode')) is not int or type(value.get('timed_out')) is not bool
            or type(value.get('signal')) is not int or value['signal'] not in (0, signal.SIGTERM, signal.SIGINT, signal.SIGHUP)):
        raise RecoveryError("回収結果の照合に失敗")
    return value


def linux_command(argv, cwd, env, timeout, grace, directory, stdout, stderr, stop):
    result_path = directory / 'recovery.json'
    nonce = secrets.token_hex(32)
    helper = Path(__file__).resolve()
    # 専用 worker の動的 import も、呼出元の環境に依存せず .pyc を作らない。
    process = launch_monitor([sys.executable, '-B', str(helper), '--linux', '--result', str(result_path), '--nonce', nonce,
                                '--timeout', str(timeout), '--grace', str(grace), '--', *argv],
                               cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
    # pidfd は、監督の番号が再利用されても別プロセスへ送らないための参照。
    try:
        fd = os.pidfd_open(process.pid)
    except OSError as exc:
        process.wait(timeout=timeout + grace + 7)
        raise RecoveryError("監督の参照を保持できない") from exc
    try:
        deadline = time.monotonic() + timeout + grace + 7
        sent = False
        while process.poll() is None:
            if stop.number and not sent:
                signal.pidfd_send_signal(fd, stop.number)
                sent = True
                deadline = min(deadline, time.monotonic() + grace + 7)
            if time.monotonic() >= deadline:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
                process.wait(timeout=2)
                raise RecoveryError("監督の終了を期限内に確認できない")
            time.sleep(.02)
        if process.returncode != 0:
            raise RecoveryError("監督が正常に終了しなかった")
        result = read_result(result_path, nonce)
        if result['signal'] or stop.number:
            raise Interrupted(f"終了通知 {stop.number or result['signal']} を受けた")
        result['recovery_method'] = 'linux-subreaper'
        result['recovery_scope'] = 'owned-descendants'
        return result
    finally:
        os.close(fd)


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_group(process, grace):
    """最終 signal まで leader を reap しない。reap 後は signal 0 だけ。"""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    time.sleep(grace)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)
    deadline = time.monotonic() + 5
    while group_exists(process.pid):
        if time.monotonic() >= deadline:
            raise RecoveryError("プロセス群の不在を確認できない")
        time.sleep(.02)


def posix_command(argv, cwd, env, timeout, grace, directory, stdout, stderr, stop):
    read_fd, write_fd = os.pipe()
    nonce = secrets.token_hex(32)
    try:
        process = launch_monitor([sys.executable, '-B', str(Path(__file__).resolve()), '--anchor-fd', str(write_fd),
                                    '--nonce', nonce, '--', *argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, pass_fds=(write_fd,), start_new_session=True)
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    raw = b''
    result = None
    problem = None
    timed_out = False
    try:
        deadline = time.monotonic() + timeout
        while not stop.number:
            ready, _, _ = select.select([read_fd], [], [], .02)
            if ready:
                chunk = os.read(read_fd, 4097)
                if not chunk:
                    raise RecoveryError("anchor が終了通知の前に失われた")
                raw += chunk
                if len(raw) > 4096:
                    raise RecoveryError("anchor の通知が大きすぎる")
                if b'\n' in raw:
                    result = json.loads(raw)
                    if (not isinstance(result, dict) or result.get('nonce') != nonce
                            or type(result.get('returncode')) is not int):
                        raise RecoveryError("anchor の通知が不正")
                    break
            if time.monotonic() >= deadline:
                timed_out = True
                break
        # コマンド終了の通知後も anchor は生存する。EOF は監督の異常終了。
        ready, _, _ = select.select([read_fd], [], [], 0)
        if ready and not os.read(read_fd, 1):
            raise RecoveryError("anchor が回収前に失われた")
    except (OSError, ValueError, RecoveryError) as exc:
        problem = exc
    finally:
        os.close(read_fd)
        # この呼出しまで poll/wait/communicate をしない。異常終了も未回収で保持する。
        stop_group(process, grace)
    if problem:
        raise RecoveryError(str(problem)) from problem
    stop.check()
    return {'clean': True, 'returncode': result['returncode'] if result else -signal.SIGKILL,
            'timed_out': timed_out, 'signal': 0, 'recovery_method': 'posix-process-group',
            'recovery_scope': 'original-process-group-only'}


def digest_stream(stream):
    stream.flush()
    stream.seek(0)
    digest = hashlib.sha256()
    # 群外へ逃げた POSIX の書き手が追記しても、開始時の長さだけを読む。
    remaining = os.fstat(stream.fileno()).st_size
    while remaining:
        part = stream.read(min(1024 * 1024, remaining))
        if not part:
            raise RecoveryError("停止後の品質出力が短くなった")
        digest.update(part)
        remaining -= len(part)
    return digest.hexdigest()


def run_command(argv, cwd, env, timeout, directory, stop, *, grace=.2, method=None):
    stop.check()
    if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(grace) or grace < 0:
        raise RecoveryError("監督の期限が不正")
    # 呼出元の control 領域に出力を持つ。子孫が PIPE を開き続けても待機しない。
    method = method or ('linux' if sys.platform.startswith('linux') else 'posix')
    if method == 'linux' and (not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal')):
        raise RecoveryError("Linux の監督に必要な pidfd がない")
    try:
        with (directory / 'stdout').open('w+b') as stdout, (directory / 'stderr').open('w+b') as stderr:
            function = linux_command if method == 'linux' else posix_command
            try:
                result = function(argv, cwd, env, timeout, grace, directory, stdout, stderr, stop)
            except LaunchError:
                result = {'clean': True, 'returncode': 127, 'timed_out': False, 'signal': 0,
                          'recovery_method': 'not-started', 'recovery_scope': 'no-command-started'}
            stop.check()
            result['stdout_sha256'] = digest_stream(stdout)
            result['stderr_sha256'] = digest_stream(stderr)
            return result
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError) as exc:
        raise RecoveryError("品質プロセスの回収を確認できない: " + str(exc)) from exc


def anchor(fd, nonce, argv):
    # 捕捉 handler は exec で既定値に戻る。SIG_IGN の継承は避ける。
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: None)
    os.set_inheritable(fd, False)
    try:
        process = subprocess.Popen(argv, close_fds=True)
        rc = process.wait()
    except OSError:
        rc = 127  # 実コマンドの起動失敗。anchor 自身は群の停止まで残る。
    os.write(fd, (json.dumps({'nonce': nonce, 'returncode': rc}) + '\n').encode())
    while True:
        signal.pause()


def linux_worker(args, argv):
    # subreaper はこの専用プロセスだけで設定する。既存 loop の回収規則は変更しない。
    spec = importlib.util.spec_from_file_location('check_loop_supervisor', Path(__file__).with_name('loop-supervisor.py'))
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    original_popen = supervisor.subprocess.Popen

    class LaunchFailure(Exception):
        pass

    def launch(*values, **options):
        try:
            return original_popen(*values, **options)
        except OSError as exc:
            raise LaunchFailure() from exc

    fd = os.open(args.result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        supervisor.subprocess.Popen = launch
        try:
            result = supervisor.supervise(argv, args.timeout, args.grace)
        except LaunchFailure:
            if not supervisor.reap({}):
                raise RecoveryError("起動失敗後に子の不在を確認できない")
            result = {'clean': True, 'returncode': 127, 'timed_out': False, 'signal': supervisor.STOP}
        result['nonce'] = args.nonce
        raw = (json.dumps(result) + '\n').encode()
        if os.write(fd, raw) != len(raw):
            raise RecoveryError("回収結果を書けない")
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--anchor-fd', type=int)
    parser.add_argument('--linux', action='store_true')
    parser.add_argument('--result')
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--grace', type=float)
    parser.add_argument('--nonce', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        if not argv:
            raise ValueError('コマンドが空')
        if args.linux:
            if args.timeout is None or args.grace is None or not math.isfinite(args.timeout) or args.timeout <= 0 or not math.isfinite(args.grace) or args.grace < 0:
                raise ValueError('期限が不正')
            linux_worker(args, argv)
        else:
            anchor(args.anchor_fd, args.nonce, argv)
    except (OSError, ValueError, RuntimeError, RecoveryError):
        print('品質プロセスの回収を確認できない', file=sys.stderr)
        return 97
    return 0


if __name__ == '__main__':
    sys.exit(main())
