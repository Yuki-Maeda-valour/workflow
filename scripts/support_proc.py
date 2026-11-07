"""テストの共通の補助: プロセスの不在の確かめと、試験が起動したプロセスの片付け(#388)。

テストではない(名前を test_ で始めないので、discover は拾わない)。
discover の形(`python3 -m unittest discover -s scripts -p <ファイル>`)と run-tests.py では、
scripts/ が読み込みの起点になるので `import support_proc` で読み込める。
dotted の形(`python3 -m unittest scripts.<名前>`)で流すファイルは、次のように読み込む
(test_check_process.py・test_review_guard.py の書き方):

    try:
        import support_proc
    except ModuleNotFoundError as error:
        if error.name != 'support_proc':
            raise
        from scripts import support_proc

標準ライブラリだけを使い、ほかの共通の補助を読み込まない。
test_check_process.py と test_review_guard.py に置いていた同じ関数を、ここへ 1 つにまとめた。
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def probe(send, target):
    try:
        send(target, 0)
    except ProcessLookupError:
        return 'absent'
    except PermissionError:
        return 'denied'
    return 'alive'


def assert_absent_now(pid, send=os.kill):
    # 製品が「止めて回収した」と返した直後に使う。生きていれば待たずに失敗にする。
    # EPERM だけは macOS の回収直後の一過性の応答として、1 秒以内で 0.02 秒ごとに確かめ直す。
    deadline = time.monotonic() + 1
    while True:
        state = probe(send, pid)
        if state == 'absent':
            return
        if state == 'alive':
            raise AssertionError(f'{pid} が残っている')
        if time.monotonic() >= deadline:
            raise AssertionError(f'{pid} の不在を 1 秒以内に確かめられない(EPERM が続く)')
        time.sleep(.02)


def assert_group_absent_now(pgid):
    assert_absent_now(pgid, os.killpg)


def wait_absent(pid, timeout=30, send=os.kill):
    # 自分で終わるはずのプロセスと後始末だけに使う。期限まで待つ。
    deadline = time.monotonic() + timeout
    while probe(send, pid) != 'absent':
        if time.monotonic() >= deadline:
            return False
        time.sleep(.02)
    return True


# 後続コマンドの中で、assert_absent_now と同じ規則で不在を確かめる関数(品質コマンドに埋め込む)。
ABSENT_NOW = '''def absent_now(send, target):
    end = time.monotonic() + 1
    while True:
        try:
            send(target, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            if time.monotonic() >= end:
                return False
            time.sleep(.02)
            continue
        return False
'''


def record(path):
    # 子と孫が自分の pid と pgid を JSON で書く 1 文。書きかけを読ませないよう、置き換えで書く。
    part = str(path) + '.part'
    return (f"Path({part!r}).write_text(json.dumps({{'pid': os.getpid(), 'pgid': os.getpgrp()}})); "
            f"os.replace({part!r}, {str(path)!r})")


def read_record(path):
    try:
        value = json.loads(Path(path).read_text())
        return int(value['pid']), int(value['pgid'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def command_line(pid):
    found = subprocess.run(['ps', '-ww', '-o', 'command=', '-p', str(pid)], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True)
    return found.stdout


def owned(root, pid, pgid):
    # 信号を送る直前に、今の pgid が記録と同じで、コマンド行に試験の領域を含むかを確かめる。
    try:
        if os.getpgid(pid) != pgid:
            return False
    except OSError:
        return False
    return str(root) in command_line(pid)


def cleanup(root, stop=(), children=(), groups=(), records=(), raw=()):
    # 試験が起動したものだけを止めて回収し、残ったものの番号を返す。
    # stop: 監督(run-checks)の Popen。children: 試験の直接の子の Popen(差し替えで捕まえた anchor・無関係なプロセス)。
    # groups: 元の群の pgid を書いた記録。records: JSON の記録。raw: 素の pid(既存の Linux の試験の形)。
    found = [value for value in map(read_record, records) if value]
    groups = [value[1] for value in map(read_record, groups) if value]
    pids = [pid for pid, _ in found] + list(raw)
    # 手順 1: 監督がまだ動いていれば、TERM で製品の停止の道を先に通す。
    for process in stop:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    # 手順 2: 元の群の先頭が生きていて、コマンド行に試験の領域を含むときだけ、群ごと止める。
    for pgid in groups:
        if owned(root, pgid, pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
    # 手順 3: 記録した各プロセスを止める。素の pid は、まだ終わっていない自分の子だけ。
    for pid, pgid in found:
        if owned(root, pid, pgid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    for pid in raw:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if not done:
            os.kill(pid, signal.SIGKILL)
    # 手順 4: 直接の子と、試験のプロセスが引き取った孤児(Linux の subreaper)を回収する。
    for process in children:
        if process.poll() is None:
            process.kill()
        process.wait()
    for pid in pids:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    # 手順 5: 記録したすべての pid と元の群が不在になるまで待つ。
    left = [pid for pid in pids if not wait_absent(pid)]
    return left + [pgid for pgid in groups if not wait_absent(pgid, send=os.killpg)]
