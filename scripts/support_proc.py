"""テストの共通の補助: プロセスの不在の確かめと、試験が起動したプロセスの片付け(#388)。
止め方・状態の読み・疑似端末の読み切り(#532)も持つ(native/ci の比較器と自己検査も読み込む)。

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

native/ci の比較器は、`Path(__file__).resolve().parents[2] / 'scripts'` を sys.path に足して読み込む
(check_r4g.py の書き方)。

標準ライブラリだけを使い、ほかの共通の補助を読み込まない。
test_check_process.py と test_review_guard.py に置いていた同じ関数を、ここへ 1 つにまとめた。
select.select・os.read・os.write・os.killpg は、呼ぶたびにモジュールの属性として引く
(自己検査が、モジュールの属性を差し替えて順番を起こすため)。
"""
from collections import namedtuple
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import uuid


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
    # 子プロセスと孫プロセスが自分の pid と pgid を JSON で書く 1 文。書きかけを読ませないよう、置き換えで書く。
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
    # stop: 監督(run-checks)の Popen。children: 試験の直接の子プロセスの Popen(差し替えで捕まえた anchor・無関係なプロセス)。
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
    # 手順 3: 記録した各プロセスを止める。素の pid は、まだ終わっていない自分の子プロセスだけ。
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
    # 手順 4: 直接の子プロセスと、試験のプロセスが引き取った孤児(Linux の subreaper)を回収する。
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


# ---------------------------------------------------------------- 止め方(#526・#531・#532)

# Mac で、群へ送る信号と信号 0 の EPERM を送り直す期限(秒)。support_native.py の写しと同じ値。
ZOMBIE_GROUP_WAIT = 5


def signal_group(pgid, number, *, reap=None):
    """os.killpg(pgid, number) と同じに送る。戻り値は無い。

    - 最初の ESRCH は ProcessLookupError を投げる(os.killpg と同じ。試す信号の厳しさを下げない)。
    - Mac(XNU)は、送れる生きたプロセスが無い群(回収前の終了済みだけが残る群)に、ESRCH ではなく EPERM を返す
      (#289・#526)。Mac の EPERM だけ、reap があれば呼び(直接の子を回収する)、0.02 秒待って送り直す。
      最初の EPERM から ZOMBIE_GROUP_WAIT 秒まで続ける。
      - 送れたら戻る。送り直しで ESRCH になれば戻る(送れる生きたプロセスが無かった。Linux で終了済みだけの群へ
        送ると成功するのと同じ扱い)。
      - 期限を過ぎても EPERM なら PermissionError を投げる(止められない子か、回収されない終了済みの子が残る)。
    - Mac 以外の EPERM は、待たずに PermissionError を投げる(Linux の動きは os.killpg と同じ)。
    """
    deadline = None
    while True:
        try:
            os.killpg(pgid, number)
            return
        except ProcessLookupError:
            if deadline is None:
                raise
            return
        except PermissionError:
            if sys.platform != 'darwin':
                raise
            now = time.monotonic()
            if deadline is None:
                deadline = now + ZOMBIE_GROUP_WAIT
            if now >= deadline:
                raise
            if reap is not None:
                reap()
            time.sleep(.02)


def group_gone(pgid, *, wait=ZOMBIE_GROUP_WAIT, reap=None):
    """信号 0 で群の有無を読み、(不在か, 見た結果) を返す。EPERM・ESRCH 以外の OSError は投げる。

    - ESRCH なら (True, 'ESRCH')。送れたら (False, 'alive')。
    - Mac の EPERM は、reap があれば呼び、0.02 秒ごとに wait 秒まで読み直す。その間に ESRCH になれば
      (True, 'EPERM <回数> 回の後 ESRCH')、送れれば (False, 'alive')、続けば (False, 'EPERM <回数> 回')。
    - Mac 以外の EPERM は、待たずに (False, 'EPERM')。
    EPERM を不在と読み替えない(assert_absent_now は製品が回収した直後の確かめで、規則が別)。
    """
    denied = 0
    deadline = None
    while True:
        state = probe(os.killpg, pgid)
        if state == 'absent':
            return True, ('ESRCH' if not denied else f'EPERM {denied} 回の後 ESRCH')
        if state == 'alive':
            return False, 'alive'
        denied += 1
        if sys.platform != 'darwin':
            return False, 'EPERM'
        now = time.monotonic()
        if deadline is None:
            deadline = now + wait
        if now >= deadline:
            return False, f'EPERM {denied} 回'
        if reap is not None:
            reap()
        time.sleep(.02)


# ---------------------------------------------------------------- 状態の読み(#435・#511・#524。check_r4g.py から移した)

def process_state(pid):
    """プロセスの状態の1文字(R・S・T・Z など)。無ければ None。Linux は /proc、ほかの機種は ps で読む。"""
    if sys.platform.startswith('linux'):
        try:
            with open('/proc/%d/stat' % pid) as handle:
                return handle.read().split(') ', 1)[1][0]
        except OSError:
            return None
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    text = result.stdout.strip()
    return text[:1].decode('ascii') if result.returncode == 0 and text else None


# 死んだと読む状態。Z は回収待ち、X は回収されている途中(Linux は Z から消えるまでの短い間、X を出す)。
# X を生きていると読むと、Z で「死んだ」と読んだ後の読み直しが、X に当たって「生きている」に変わりうる。
# CI の Linux arm64 で落ちた回の原因は、この形と推測される(未確認)。
DEAD_STATES = (None, 'Z', 'X')


def wait_state(pid, accept, timeout, *, read=None):
    """read(無ければ process_state)で 0.005 秒ごとに状態を読み、accept(状態) が真なら (True, 状態) を返す。

    期限を過ぎたら (False, 最後に読んだ状態) を返す。例外は投げない。accept は None(無いプロセス)を受け入れてよい。
    状態の文字は読むたびに変わりうるので、2 回の読みを比べない(1 回の読みで決める)。
    """
    if read is None:
        read = process_state
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        state = read(pid)
        if accept(state):
            return True, state
        time.sleep(0.005)
    return False, state


# ---------------------------------------------------------------- 疑似端末の読み切り(#519・#522)

# data: 成功なら目印を外した全体、失敗ならそれまでに読んだ全体。reason: 成功なら None、失敗なら 'deadline'・
# 'read-error'・'eof'・'write-error'・'marker-timeout'。errno: 読み書きの失敗の番号(ほかは None)。written: 目印を書いたか。
MarkerRead = namedtuple('MarkerRead', 'data reason errno written')


def read_until_marker(master, slave, finished, *, prefix, select_wait, deadline=None, marker_timeout=None):
    """疑似端末の親の側の端(master)を、時間ではなく目印で読み切り、MarkerRead を返す。例外は投げない。

    子が終わった(finished() が真)のを見た後に、親が持つ子の側の端(slave)へ、改行を含まない一意の目印
    (prefix と uuid4 の 16 進をつないだもの)を書く。端末の出力は順に届くので、目印が出た時点で、子の出力は
    すべて出ている。毎回の順番:
      1. deadline(単調な時計の時刻)があり、今が過ぎていれば 'deadline' で戻る。
      2. 目印を書く前だけ finished() を読む(select の前に読む。終わりを見た後に始めた select が空なら、
         端末に読み残しは無いので、目印の書き込みは止まらない)。
      3. select.select([master], [], [], select_wait)。
      4. 読めるなら os.read(master, 65536)。OSError は 'read-error'、空は 'eof' で戻る。目印を書いた後で、
         読んだ全体に目印があれば、目印を 1 つ外して reason=None で戻る。
      5. 読めず、2 で終わっていれば、目印を os.write(slave, …) で書く。OSError は 'write-error' で戻る。
         marker_timeout があれば、目印の期限を「今 + marker_timeout」にする。
      6. 目印の期限があり、今が過ぎていれば 'marker-timeout' で戻る。
    """
    marker = prefix + uuid.uuid4().hex.encode()
    buffer = b''
    written = False
    marker_deadline = None
    while True:
        if deadline is not None and time.monotonic() > deadline:
            return MarkerRead(buffer, 'deadline', None, written)
        ended = not written and finished()
        ready, _, _ = select.select([master], [], [], select_wait)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError as error:
                return MarkerRead(buffer, 'read-error', error.errno, written)
            if not chunk:
                return MarkerRead(buffer, 'eof', None, written)
            buffer += chunk
            if written and marker in buffer:
                return MarkerRead(buffer.replace(marker, b'', 1), None, None, written)
        elif ended:
            # 目印は、子が終わったのを見た後の select が空の回(端末の内側の buffer が空のとき)にだけ書く。
            try:
                os.write(slave, marker)
            except OSError as error:
                return MarkerRead(buffer, 'write-error', error.errno, written)
            written = True
            if marker_timeout is not None:
                marker_deadline = time.monotonic() + marker_timeout
        if marker_deadline is not None and time.monotonic() > marker_deadline:
            return MarkerRead(buffer, 'marker-timeout', None, written)
