#!/usr/bin/env python3
"""プロセスと時間を扱う自己検査を、負荷の下で何度も流し、揺れ(ときどき落ちること)が無いかを確かめる(#532)。

使い方(リポジトリ直下で実行する):
  python3 -B scripts/stress-tests.py --repeat 200 --jobs 16 --load 16 test_support_proc.ProcessStateTest
  python3 -B scripts/stress-tests.py --dir native/ci --repeat 100 --jobs 16 --load 16 test_r4g.Comparator.<テスト>

- 1 回ずつ別のプロセス(<dir> を作業フォルダにした `python3 -B -m unittest <名前>…`)で、同時の数ずつ、
  合わせて回数だけ流す。--dir の既定は、このファイルがある scripts。子の Python は、この道具を動かした Python。
- 回ごとの合否は終了コードで決める(失敗も ERROR も、落ちた回に数える)。合格と失敗の回数と、最初に落ちた回の
  出力を出す。全部合格なら終了 0、1 回でも落ちれば終了 1。引数の誤りは 2。止めたときは 130(負荷の群が残れば 1)。
- --load <数> は、CPU を使い続ける子を、その数だけ 1 つの別の群(プロセスグループ)で動かす(混んだ機械をまねる)。
  終わりと例外のときに、その群を止めて回収し、group_gone で残りが無いことを確かめる。残れば終了 1。
  この道具が外から SIGKILL で止められたときも残らないよう、負荷の子は 1 秒ごとに親が変わったかを見て、変われば終わる
  (親の番号は起動の引数で渡す。子が起動の後に読むと、その前に道具が止まったとき、引き取った側の番号を控えてしまう)。
- 各回は別のセッションで起こし、終わった後と時間切れ(--timeout。既定 3600 秒)のときに、その群に残ったプロセスを
  SIGKILL で止めてから回収する(テストが残したプロセスを、次の回に持ち越さない)。
- 1 回あたり 1% で落ちる揺れを、200 回で 87%、100 回で 63% の確率で見つける(#532 の完了条件 3)。
"""

import argparse
import concurrent.futures
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import support_proc  # noqa: E402

# 負荷の子: CPU を使い続け、1 秒ごとに親が変わったか(親が先に終わったか)を見て、変われば終わる。
# 親(この道具)の番号は、引数で受け取る(起動の後に os.getppid() で控えると、その前に道具が止まったときに、
# 引き取った側の番号を控えて終わらなくなる)。
BURN = '''import os, sys, time
parent = int(sys.argv[1])
check = time.monotonic() + 1
while True:
    if time.monotonic() >= check:
        if os.getppid() != parent:
            break
        check = time.monotonic() + 1
'''
# 止める合図(SIGINT のほか)。受けたら KeyboardInterrupt にして、同じ後片付けを通す。
INTERRUPT_SIGNALS = tuple(getattr(signal, name) for name in ('SIGTERM', 'SIGHUP') if hasattr(signal, name))


class Interrupted(Exception):
    """止める合図を受けた後に、新しい回を始めようとした。"""


def raise_interrupt(signum, frame):
    raise KeyboardInterrupt(signum)


class Load:
    """CPU を使い続ける子を、1 つの別の群で動かす。群の番号は最初の子の pid。"""

    def __init__(self):
        self.children = []
        self.pgid = None

    def start(self, count):
        # 途中で起動に失敗しても、起動した子は stop で止める(呼ぶ側は、起動の前にこの物を控える)。
        for _ in range(count):
            # 1 本目が新しい群を作り、2 本目からはその群に入る(同じセッションの中なので setpgid で入れる)。
            pgid = 0 if self.pgid is None else self.pgid
            child = subprocess.Popen([sys.executable, '-B', '-c', BURN, str(os.getpid())], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     preexec_fn=lambda pgid=pgid: os.setpgid(0, pgid))
            self.children.append(child)
            if self.pgid is None:
                self.pgid = child.pid

    def reap(self):
        for child in self.children:
            child.poll()

    def stop(self):
        """群を止めて回収し、(残りが無いか, 見た結果) を返す。"""
        if self.pgid is None:
            return True, '子なし'
        try:
            support_proc.signal_group(self.pgid, signal.SIGKILL, reap=self.reap)
        except ProcessLookupError:
            pass
        for child in self.children:
            child.wait()
        return support_proc.group_gone(self.pgid, reap=self.reap)


class Runs:
    """動いている回。止める合図の後は、新しく始めない。"""

    def __init__(self, command, cwd, timeout):
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.lock = threading.Lock()
        self.running = set()
        self.stopped = False

    def run(self, number):
        """1 回を流し、(回, 終了コード, 出力, 秒, 時間切れか) を返す。"""
        with tempfile.TemporaryFile() as log:
            started = time.monotonic()
            with self.lock:
                if self.stopped:
                    raise Interrupted()
                # 出力はファイルへ書く(残ったプロセスがパイプを持ち続けても、待ちが止まらない)。
                process = subprocess.Popen(self.command, cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                self.running.add(process)
            try:
                timed_out = not wait_exited(process, started + self.timeout)
            finally:
                # 終わった子は回収する前に群を止める(回収前の子が群の番号を持つので、ほかの群と取り違えない)。
                # 一覧から外すのも回収の前に、錠の中で行う(stop_all は錠を持ったまま送るので、回収の後の番号へ送らない)。
                with self.lock:
                    stop_group(process.pid)
                    self.running.discard(process)
                process.wait()
            elapsed = time.monotonic() - started
            log.seek(0)
            output = log.read().decode('utf-8', 'replace')
        return number, process.returncode, output, elapsed, timed_out

    def stop_all(self):
        # 錠を持ったまま送る。一覧にある子は、まだ回収されていない(回す側は、錠の中で一覧から外してから回収する)。
        with self.lock:
            self.stopped = True
            for process in self.running:
                stop_group(process.pid)


def wait_exited(process, deadline):
    """直接の子が終わるまで待つ(回収はしない)。期限までに終われば True。"""
    while True:
        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def stop_group(pgid):
    # 回の群に残ったプロセスを止める。EPERM(Mac で、送れる生きたプロセスが無い群)と ESRCH は、止める物が無い。
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError('1 以上にしてください')
    return value


def not_negative(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError('0 以上にしてください')
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(description='unittest の名前を、負荷の下で何度も流す。')
    parser.add_argument('--dir', type=Path, default=SCRIPTS, help='作業フォルダ(既定はこのファイルがある scripts)')
    parser.add_argument('--repeat', type=positive, required=True, help='流す回数の合計')
    parser.add_argument('--jobs', type=positive, required=True, help='同時に流す回の数')
    parser.add_argument('--load', type=not_negative, default=0, help='CPU を使い続ける子の数(既定 0)')
    parser.add_argument('--timeout', type=positive, default=3600, help='1 回の上限の秒数(既定 3600)')
    parser.add_argument('names', nargs='+', help='unittest の名前(<モジュール>[.<クラス>[.<テスト>]])')
    args = parser.parse_args(argv)
    if not args.dir.is_dir():
        parser.error(f'--dir がフォルダではない: {args.dir}')
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    for number in INTERRUPT_SIGNALS:
        signal.signal(number, raise_interrupt)
    started = time.monotonic()
    cwd = args.dir.resolve()
    runs = Runs([sys.executable, '-B', '-m', 'unittest', *args.names], cwd, args.timeout)
    print(f'計画: {" ".join(args.names)} を {args.repeat} 回・同時 {args.jobs}・負荷 {args.load}(作業フォルダ {cwd})',
          flush=True)
    load = None
    results = []
    interrupted = False
    load_left = False
    try:
        load = Load()
        load.start(args.load)
        if args.load:
            print(f'負荷: CPU を使い続ける子 {args.load} 本を、群 {load.pgid} で動かす', flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(runs.run, number) for number in range(1, args.repeat + 1)]
            try:
                for future in concurrent.futures.as_completed(futures):
                    number, code, output, elapsed, timed_out = future.result()
                    results.append((number, code, output, elapsed, timed_out))
                    if code != 0 or timed_out:
                        reason = f'時間切れ・{args.timeout} 秒' if timed_out else f'終了コード {code}'
                        print(f'失敗: {number} 回目({reason}・{elapsed:.1f} 秒)', flush=True)
            except BaseException:
                runs.stop_all()
                for future in futures:
                    future.cancel()
                raise
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if load is not None:
            gone, seen = load.stop()
            if not gone:
                load_left = True
                print(f'異常: 負荷の群 {load.pgid} が残った({seen})', flush=True)
            elif args.load:
                print(f'負荷: 群 {load.pgid} を止めて回収した({seen})', flush=True)
    failed = [item for item in results if item[1] != 0 or item[4]]
    seconds = time.monotonic() - started
    if failed:
        number, code, output, _, timed_out = failed[0]
        print(f'最初に落ちた回: {number} 回目(' + ('時間切れ' if timed_out else f'終了コード {code}') + ')')
        print(output, end='' if output.endswith('\n') else '\n')
    if interrupted:
        print(f'止めた: {len(results)} 回を流した後に止める合図を受けた / {seconds:.1f} 秒', flush=True)
        return 1 if load_left else 130
    average = sum(item[3] for item in results) / len(results)
    print(f'結果: 合格 {len(results) - len(failed)} 回・失敗 {len(failed)} 回({args.repeat} 回中)'
          f' / {seconds:.1f} 秒(1 回の平均 {average:.1f} 秒)', flush=True)
    return 1 if failed or load_left else 0


if __name__ == '__main__':
    sys.exit(main())
