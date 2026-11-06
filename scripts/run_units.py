"""テストの流し方の共通の処理。run-tests.py・run-loop-selftest.py・run-all.py が読み込んで使う。

- 子のプロセスの起動と止め方(Children)・止める合図の受け方(INTERRUPT_SIGNALS・raise_interrupt)。
- 単位(Unit): 1 つの子のプロセスで流す仕事。枠の内(同時に流す数を数える)と枠の外(数えず、最初に始める)がある。
- run_units: 枠の外を先に始め、枠の単位を重い順に流す。終わるたびに 1 行を出す。止めたときの決まりもここに持つ。
- 重みの決め方(weight_of)と、重みの記録の読み書き(run-all.py だけが使う)。
- 失敗した単位の出力をまとめて出す関数(print_failures)。

このファイルを読み込む側は、読み込む前に sys.dont_write_bytecode = True にする(scripts/ の下に __pycache__ を作らない)。
"""

import concurrent.futures
import contextlib
import json
import math
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent

# 重みの記録にも表にも無い単位の重み(秒)。重い単位を最後に始めると全体がその分延びる。軽い単位を早く始めても損は数秒。
DEFAULT_WEIGHT = 200.0
RECORD_VERSION = 1


def cpu_count():
    """使える CPU の数(1 以上)。os.sched_getaffinity が無い環境では os.cpu_count()。"""
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 1
    return max(1, cpus)


class Interrupted(Exception):
    """止める合図を受けた後に、プロセスを始めようとした。"""


class Children:
    """動いている子のプロセス。止める合図の後は、新しく始めない。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = set()
        self.stopped = False

    def start(self, command, env=None):
        """env を渡すと、子の環境変数をそれにする(既定は親と同じ)。"""
        with self.lock:
            if self.stopped:
                raise Interrupted()
            # 別のプロセスグループで起動する(端末の Ctrl-C は親だけが受け、子はグループごと止める)。
            process = subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors="replace", start_new_session=True, env=env)
            self.running.add(process)
            return process

    def finish(self, process):
        with self.lock:
            self.running.discard(process)

    def stop_all(self, grace=5.0):
        """新しく始めないようにし、動いている子をグループごと止める。止めた数を返す。

        グループへ SIGINT・SIGTERM・SIGKILL の順に送り、それぞれの後で、グループ全体が消えるまで
        最大 grace 秒待つ。次の合図は、まだ消えないグループにだけ送る。
        - SIGINT から始める: Python の子は KeyboardInterrupt で終わり、一時フォルダなどを片付けられる
          (SIGTERM では片付けずに終わる)。
        - 直接の子が終わっても待ちを打ち切らない: グループの中の孫(入れ子の run-tests.py など)が、
          別のグループで起動した自分の子を止めて回収する時間を残すため。
        - 限界: 入れ子の実行で、内側の別のグループの子孫が合図を無視すると、外側の SIGKILL の後に
          残ることがある。そのときは、残ったプロセスを pid を指定して止める(別のセッションの子孫を
          追う仕組みは入れていない)。
        """
        with self.lock:
            self.stopped = True
            running = list(self.running)
        groups = [process.pid for process in running]
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            for pgid in groups:
                if group_alive(pgid):
                    try:
                        os.killpg(pgid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and any(group_alive(pgid) for pgid in groups):
                time.sleep(0.05)
        return len(running)


def group_alive(pgid):
    """プロセスグループに、まだプロセスが残っていれば True(止められないものは待たない)。"""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# Ctrl-C(SIGINT。KeyboardInterrupt になる)のほかに受けて止めるシグナル。子は別のセッションにいるので、
# 端末を閉じたときの SIGHUP は親だけに届く。親だけが終わって子が孤児にならないよう、受けて止める。
INTERRUPT_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name))


def raise_interrupt(signum, frame):
    raise KeyboardInterrupt(signum)


def run_command(children, command, env=None):
    """子のプロセスを 1 つ流し、(終了コード, 出力, 秒) を返す。"""
    started = time.monotonic()
    process = children.start(command, env=env)
    try:
        output, _ = process.communicate()
    finally:
        children.finish(process)
    return process.returncode, output, time.monotonic() - started


@dataclass
class Unit:
    """1 つの子のプロセスで流す仕事。

    - key: 重みの記録の鍵。
    - label: 進み具合の行と、失敗の見出しに出す名前。
    - in_slot: True なら枠(同時に流す数)を使う。False なら枠を使わず、最初に始める。
    - weight: 重み(秒)。None なら表に無い単位で、DEFAULT_WEIGHT として並べる。
    - source: 重みの出所(`記録`・`表`・`既定`)。run-all.py の --plan が出す。
    - table_weight: 単位を作った側の表の重み(無ければ None)。run-all.py が記録と突き合わせる。
    - log_name: --log-dir に残すときのファイル名(None なら残さない)。
    - run(children): 単位を流して結果を返す。結果の形は、単位を作った側が決める。
    - describe(result): 結果から、進み具合の行の後半(件数)と成否 (text, ok) を作る。
    - output_of(result): 結果から、ログに残す出力を取り出す(log_name があるときだけ使う)。
    """

    key: str
    label: str
    run: Callable[[Children], Any]
    describe: Callable[[Any], tuple]
    in_slot: bool = True
    weight: Optional[float] = None
    source: str = "既定"
    table_weight: Optional[float] = None
    log_name: Optional[str] = None
    output_of: Optional[Callable[[Any], str]] = None


def effective_weight(unit):
    return DEFAULT_WEIGHT if unit.weight is None else unit.weight


def slot_order(units):
    """枠の単位を、重い順(重みの降順。同じなら与えた順)に並べて返す。"""
    slot = [unit for unit in units if unit.in_slot]
    return sorted(slot, key=lambda unit: -effective_weight(unit))


def write_log(log_dir, name, text):
    try:
        (Path(log_dir) / name).write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"注意: ログを書けない({Path(log_dir) / name}: {exc})", flush=True)


def run_units(units, jobs, log_dir=None, on_interrupt=None):
    """units を流す。(結果の一覧, None) か、止めたときは (None, 128 + シグナルの番号) を返す。

    - 枠の外の単位を先に始める(枠を使わない)。枠の単位を、重い順に、jobs 個の枠で流す。
    - 単位が終わるたびに、`<ok か FAIL>  <秒> 秒  <表示名>  <件数>` を 1 行出す。
    - 止める合図(SIGINT・SIGTERM・SIGHUP)を受けたら、2 回目の合図を無視にし、始めていない単位を捨て、
      動いている単位(枠の外も)を止める。中断の行を出し、on_interrupt(始めた単位と future の組の一覧)を
      呼んでから返す。
    - 止めなかったときは、全部の単位の結果を、与えた順で返す。
    """
    outside = [unit for unit in units if not unit.in_slot]
    slot = slot_order(units)
    elapsed_of = {}
    children = Children()
    # SIGTERM・SIGHUP も Ctrl-C と同じく止める(子を残して親だけが終わらないように)。
    for sig in INTERRUPT_SIGNALS:
        signal.signal(sig, raise_interrupt)

    def execute(unit):
        started = time.monotonic()
        try:
            return unit.run(children)
        finally:
            elapsed_of[unit.key] = time.monotonic() - started

    def execute_and_log(unit):
        result = execute(unit)
        # 止めた単位も、出力が閉じた(run が返った)後に書く。
        if log_dir is not None and unit.log_name and unit.output_of is not None:
            write_log(log_dir, unit.log_name, unit.output_of(result))
        return result

    done = {}
    with contextlib.ExitStack() as stack:
        outside_pool = stack.enter_context(concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(outside))))
        slot_pool = stack.enter_context(concurrent.futures.ThreadPoolExecutor(max_workers=jobs))
        started = []
        try:
            for unit in outside:
                started.append((unit, outside_pool.submit(execute_and_log, unit)))
            for unit in slot:
                started.append((unit, slot_pool.submit(execute_and_log, unit)))
            by_future = {future: unit for unit, future in started}
            for future in concurrent.futures.as_completed(list(by_future)):
                unit = by_future[future]
                result = future.result()
                text, ok = unit.describe(result)
                print(f"{'ok  ' if ok else 'FAIL'} {elapsed_of[unit.key]:7.1f} 秒  {unit.label}  {text}", flush=True)
                done[unit.key] = result
        except KeyboardInterrupt as exc:
            # 待ち行列の単位を捨て、動いている単位を止める。with を出るときは、止めた単位だけを待つ。
            signum = exc.args[0] if exc.args and exc.args[0] in INTERRUPT_SIGNALS else signal.SIGINT
            # 止めている間の 2 回目の合図で、片付けが途中で切れないようにする。
            for sig in (signal.SIGINT, *INTERRUPT_SIGNALS):
                signal.signal(sig, signal.SIG_IGN)
            dropped = sum(future.cancel() for _, future in started)
            killed = children.stop_all()
            print(f"\n中断: 動いていた {killed} 個のプロセスを止め、まだ始めていない {dropped} 個を始めずに捨てた"
                  f"(終わっていたのは {len(done)} 個)", flush=True)
            if on_interrupt is not None:
                on_interrupt(started)
            return None, 128 + signum
    return [done[unit.key] for unit in units], None


def print_failures(failures):
    """失敗した単位の出力を、見出しを付けてまとめて出す。failures は (表示名, 終了コード, 出力) の一覧。"""
    for label, code, output in failures:
        print(f"\n===== {label}(終了コード {code})=====")
        print(output.rstrip())


# ---- 重み ----

def weight_of(key, record, table):
    """単位の重みと出所を、記録 → 表 → 既定の順に引いて (秒, 出所) で返す。"""
    if key in record:
        return float(record[key]), "記録"
    if key in table:
        return float(table[key]), "表"
    return DEFAULT_WEIGHT, "既定"


def _valid_seconds(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        # 10**400 のような、float にできない整数は OverflowError になる。
        return math.isfinite(float(value)) and value >= 0
    except (OverflowError, TypeError, ValueError):
        return False


def load_weights(path, notes=None):
    """重みの記録(鍵 → 秒)を読む。ファイルが無ければ空。読めない・形が違うときは、注意を出して空を返す。

    notes(リスト)を渡すと、注意をすぐには出さず、その行を notes に足す(呼び出し側が、出す位置を決める)。
    """
    def warn(reason):
        line = f"注意: 重みの記録を読めない({path}: {reason})。表と既定の重みで並べる"
        if notes is None:
            print(line, flush=True)
        else:
            notes.append(line)

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        warn(exc)
        return {}
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("version") != RECORD_VERSION:
            raise ValueError("version が合わない")
        weights = data.get("weights")
        if not isinstance(weights, dict) or not all(isinstance(key, str) and _valid_seconds(value)
                                                    for key, value in weights.items()):
            raise ValueError("weights の形が違う")
        return {key: float(value) for key, value in weights.items()}
    except (ValueError, OverflowError, TypeError) as exc:
        warn(exc)
        return {}


def save_weights(path, record, times):
    """成功した単位の時間 times(鍵 → 秒)を、前の値との平均にして記録へ書く。書けないときは、注意を出して続ける。

    同じフォルダの一時ファイルに書いてから os.replace で置き換える(途中で切れた記録を残さない)。
    """
    path = Path(path)
    merged = dict(record)
    for key, seconds in times.items():
        merged[key] = (merged[key] + seconds) / 2 if key in merged else seconds
    merged = {key: round(value, 1) for key, value in merged.items()}
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"version": RECORD_VERSION, "weights": merged}, stream, ensure_ascii=False, indent=1,
                      sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError) as exc:
        print(f"注意: 重みの記録を書けない({path}: {exc})。今回の時間は記録しない", flush=True)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
