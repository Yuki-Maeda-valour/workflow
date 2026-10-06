#!/usr/bin/env python3
"""Linux の subreaper で、一周の子孫を最後の waitpid まで所有して回収する。"""
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

STOP = 0


def interrupted(number, _frame):
    global STOP
    STOP = number


def setup():
    if not sys.platform.startswith('linux') or not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise RuntimeError('Linux subreaper と pidfd が必要')
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'subreaper を設定できない')
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, interrupted)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0):  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), '親の終了通知を設定できない')
    if parent == 1 or os.getppid() != parent:
        interrupted(signal.SIGTERM, None)


def identity(pid):
    raw = Path(f'/proc/{pid}/stat').read_bytes()
    fields = raw[raw.rindex(b')') + 2:].split()
    return (int(fields[1]), int(fields[19]))  # ppid, starttime


def own_children():
    # 環境・dumpable・セッションに依存しない。自分の直接の子だけを見る。
    path = Path(f'/proc/self/task/{os.getpid()}/children')
    return [int(value) for value in path.read_text().split()]


def signal_owned(number):
    for pid in own_children():
        try:
            before = identity(pid)
            if before[0] != os.getpid():
                raise RuntimeError('子の所有関係が変わった')
            fd = os.pidfd_open(pid)
            try:
                after = identity(pid)
                if before != after:
                    raise RuntimeError('子の starttime が変わった')
                signal.pidfd_send_signal(fd, number)
            finally:
                os.close(fd)
        except ProcessLookupError:
            continue
        except FileNotFoundError:
            # 消えた pid は次の waitpid で確かめる。読めない pid は成功にしない。
            continue


def reap(statuses):
    """ECHILD のときだけ、所有する生存プロセスが無いと証明できる。"""
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if not pid:
            return False
        statuses[pid] = os.waitstatus_to_exitcode(status)


def supervise(argv, timeout, grace):
    setup()
    if STOP:
        raise RuntimeError('起動前に親が終了した')
    process = subprocess.Popen(argv, start_new_session=True, close_fds=True)
    statuses = {}
    started = time.monotonic()
    timed_out = False
    while process.pid not in statuses and not STOP:
        reap(statuses)
        if process.pid in statuses:
            break
        if time.monotonic() - started >= timeout:
            timed_out = True
            break
        time.sleep(.02)
    # 生きている親を止めると、その子はここへ reparent される。二重 fork も同じ。
    term_end = time.monotonic() + grace
    hard_end = term_end + 5
    while not reap(statuses):
        signal_owned(signal.SIGTERM if time.monotonic() < term_end else signal.SIGKILL)
        if time.monotonic() >= hard_end:
            raise RuntimeError('子の不在を期限内に証明できない')
        time.sleep(.02)
    process.returncode = statuses[process.pid]
    return {'clean': True, 'returncode': process.returncode, 'timed_out': timed_out, 'signal': STOP}


def main():
    if sys.argv[1:] == ['--check']:
        try:
            setup()
            fd = os.pidfd_open(os.getpid())
            try:
                signal.pidfd_send_signal(fd, 0)
            finally:
                os.close(fd)
        except (OSError, ValueError, RuntimeError):
            print('子の監督に必要な Linux subreaper・pidfd を使えない', file=sys.stderr)
            return 97
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', required=True)
    parser.add_argument('--nonce', required=True)
    parser.add_argument('--timeout', required=True, type=float)
    parser.add_argument('--grace', required=True, type=float)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        if not command or args.timeout <= 0 or args.grace < 0:
            raise ValueError('コマンドと正の timeout、非負の grace が必要')
        # 最初に確保するが、不在証明が得られるまで成功の内容を書かない。
        fd = os.open(args.result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            result = supervise(command, args.timeout, args.grace)
            result['nonce'] = args.nonce
            raw = (json.dumps(result, sort_keys=True) + '\n').encode()
            if os.write(fd, raw) != len(raw):
                raise RuntimeError('回収結果を書けない')
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'子の回収を確認できない: {exc}', file=sys.stderr)
        return 97
    return 0


if __name__ == '__main__':
    sys.exit(main())
