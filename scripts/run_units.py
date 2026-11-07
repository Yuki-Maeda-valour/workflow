"""テストの流し方の共通の処理。run-tests.py・run-loop-selftest.py・run-all.py が読み込んで使う。

- 子のプロセスの起動と止め方(Children)・止める合図の受け方(INTERRUPT_SIGNALS・raise_interrupt)。
- 単位(Unit): 1 つの子のプロセスで流す仕事。枠の内(同時に流す数を数える)と枠の外(数えず、最初に始める)がある。
- run_units: 枠の外を先に始め、枠の単位を重い順に流す。終わるたびに 1 行を出す。止めたときの決まりもここに持つ。
- 重みの決め方(weight_of)と、重みの記録の読み書き(run-all.py だけが使う)。
- 結果の使い回しの記録の読み書き(load_reuse・save_reuse。run-all.py だけが使う)。
- 失敗した単位の出力をまとめて出す関数(print_failures)。

このファイルを読み込む側は、読み込む前に sys.dont_write_bytecode = True にする(scripts/ の下に __pycache__ を作らない)。
"""

import base64
import concurrent.futures
import contextlib
import datetime
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import zlib
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
    - ident: 結果の組 (先頭の値, 終了コード, 出力, 秒, 件数) の先頭の値。run-all.py が、使い回した結果を
      組み立て直すのに使う(unittest はジョブ、loop は節の名前〈節の外だけは None〉、4 本の selftest は鍵)。
    - entry: 単位が起動するファイルの、リポジトリ直下からの相対パス(None なら分からない)。run-all.py が、
      使い回しの範囲の中にあるかを確かめる。
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
    ident: Any = None
    entry: Optional[str] = None


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


# ---- 結果の使い回しの記録 ----

REUSE_VERSION = 1
# 1 つの単位の鍵あたりに残す記録の数(終わった時刻の新しい順)。
REUSE_KEEP = 8
# 記録の 4 つのハッシュ値(どれも sha256 の 64 桁の 16 進)。
REUSE_DIGESTS = ("input", "tools", "env", "files")
HEX64 = re.compile(r"[0-9a-f]{64}")
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
# 無効の印と比べるときの余裕(マイクロ秒)。ファイルの更新時刻は、時計より数ミリ秒前に丸められることがある。
# 印の直前に終わった記録を確実に捨てるため、印の更新時刻の 1 秒後までに終わった記録も捨てる(捨てる側に寄せる)。
VOID_MARGIN = 1_000_000


def reuse_lock_path(path):
    """使い回しの記録の錠のファイル(同じフォルダの <名前>.lock)。"""
    path = Path(path)
    return path.with_name(path.name + ".lock")


def reuse_void_path(path):
    """使い回しの記録の無効の印(同じフォルダの <名前>.void。中身は空で、更新時刻だけを使う)。"""
    path = Path(path)
    return path.with_name(path.name + ".void")


def utc_now():
    """今の時刻(UTC の ISO 8601。マイクロ秒まで)。記録の at(単位が終わった時刻)に使う。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def micros_of(at):
    """ISO 8601 の時刻を、1970 年からのマイクロ秒(整数)にする。時差の無い形は ValueError。"""
    moment = datetime.datetime.fromisoformat(at)
    if moment.tzinfo is None:
        raise ValueError(f"時差の無い時刻: {at}")
    return (moment - EPOCH) // datetime.timedelta(microseconds=1)


def pack_output(text):
    """単位の出力を、zlib で縮めて base64 にした文字列にする(記録の output)。"""
    return base64.b64encode(zlib.compress(text.encode("utf-8", "replace"), 6)).decode("ascii")


def unpack_output(data):
    """pack_output の逆。壊れていれば ValueError。"""
    try:
        return zlib.decompress(base64.b64decode(data.encode("ascii"), validate=True)).decode("utf-8")
    except zlib.error as exc:
        raise ValueError(f"出力を戻せない({exc})") from exc


def _valid_reuse_record(item):
    """1 つの記録の形が正しいか。"""
    if not isinstance(item, dict):
        return False
    if not all(isinstance(item.get(name), str) and HEX64.fullmatch(item[name]) for name in REUSE_DIGESTS):
        return False
    code = item.get("code")
    if not isinstance(item.get("ok"), bool) or not _valid_seconds(item.get("seconds")) \
            or not isinstance(code, int) or isinstance(code, bool):
        return False
    extra = item.get("extra")
    if extra is not None and not isinstance(extra, (list, dict)):
        return False
    if not isinstance(item.get("output"), str) or not isinstance(item.get("at"), str):
        return False
    try:
        micros_of(item["at"])
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def _parse_reuse(data):
    """記録のファイルの中身(バイト列)を、単位の鍵 → 記録の一覧にする。形の違う記録は 1 つずつ捨てる。

    全体の形(version・units)が違えば ValueError。
    """
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict) or parsed.get("version") != REUSE_VERSION:
        raise ValueError("version が合わない")
    units = parsed.get("units")
    if not isinstance(units, dict) or not all(isinstance(key, str) and isinstance(items, list)
                                              for key, items in units.items()):
        raise ValueError("units の形が違う")
    return {key: [item for item in items if _valid_reuse_record(item)] for key, items in units.items()}


def _void_micros(path):
    """無効の印の更新時刻(マイクロ秒)。印が無ければ None。調べられなければ OSError。"""
    try:
        return os.stat(reuse_void_path(path)).st_mtime_ns // 1000
    except FileNotFoundError:
        return None


def _tidy(units, void):
    """記録を減らす: 無効の印より前に終わった記録を捨て、同じ input は at のいちばん新しい 1 つだけを残し、
    1 つの単位の鍵あたり at の新しい順に REUSE_KEEP 個までにする。同じ at なら失敗を残す。"""
    tidy = {}
    for key, items in units.items():
        newest = {}
        for item in items:
            at = micros_of(item["at"])
            if void is not None and at <= void + VOID_MARGIN:
                continue
            kept = newest.get(item["input"])
            if kept is None or at > kept[0] or (at == kept[0] and not item["ok"]):
                newest[item["input"]] = (at, item)
        ordered = [item for _, item in sorted(newest.values(), key=lambda pair: -pair[0])][:REUSE_KEEP]
        if ordered:
            tidy[key] = ordered
    return tidy


@contextlib.contextmanager
def _locked(path, mode):
    """記録の錠(<名前>.lock)を fcntl.flock で取る。取れるまで待つ。持つのは with の中だけ。

    錠は、持ったプロセスが終われば外れる。開けない・取れないときは OSError。
    """
    descriptor = os.open(reuse_lock_path(path), os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o666)
    try:
        fcntl.flock(descriptor, mode)
        yield
    finally:
        os.close(descriptor)


def load_reuse(path, notes):
    """使い回しの記録(単位の鍵 → 記録の一覧。at の新しい順)を読む。

    - 共有の錠を取ってから読む。無効の印より前に終わった記録と、形の違う記録は捨てる。
    - 記録のファイルが無ければ空(注意を出さない)。
    - 錠を開けない・取れない・読めない・形が違うときは、注意の行を notes に足して空を返す。
    """
    path = Path(path)

    def warn(reason):
        notes.append(f"注意: 使い回しの記録を読めない({path}: {reason})。全部の単位を新しく流す")
        return {}

    try:
        # 記録のファイルが無ければ、錠を開けずに空を返す(下の FileNotFoundError・NotADirectoryError)。
        # 親のフォルダを調べる権限が無いときなどの OSError は、注意を出して空を返す。
        os.lstat(path)
        with _locked(path, fcntl.LOCK_SH):
            data = path.read_bytes()
            void = _void_micros(path)
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except OSError as exc:
        return warn(exc)
    try:
        return _tidy(_parse_reuse(data), void)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        return warn(exc)


def _mark_void(path):
    """無効の印を作るか、更新時刻を今にする。作れなければ OSError。"""
    void = reuse_void_path(path)
    descriptor = os.open(void, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o666)
    os.close(descriptor)
    os.utime(void)


def save_reuse(path, entries, failed):
    """今回の記録 entries((単位の鍵, 記録) の一覧)を、使い回しの記録に足す。書けたら True。

    - 排他の錠を取ってから読み直し、今回の記録を足し、同じフォルダの一時ファイルに書いて os.replace で
      置き換えてから、錠を外す。錠を持たずに、記録のファイルを書く・消す・置き換えることはしない
      (同時に書いたほかの回の新しい失敗を消し、古い合格を戻しうるため)。
    - 読み直した記録が壊れていれば、捨てて、今回の記録だけで書き直す。
    - 減らし方は _tidy(同じ input は at のいちばん新しい 1 つ・鍵あたり REUSE_KEEP 個・無効の印より前は捨てる)。
    - 書けないときは、注意を出して続ける(合否に効かせない)。failed(今回落ちた単位がある)が真なら、
      無効の印を作るか、更新時刻を今にする(記録のファイルを書き換えないので、錠は要らない)。
      今回より前に終わった記録は、だれが後から書いても使われなくなる。印も作れなければ、注意を出す。
    """
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(path, fcntl.LOCK_EX):
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                data = None
            void = _void_micros(path)
            units = {}
            if data is not None:
                try:
                    units = _parse_reuse(data)
                except (ValueError, TypeError, OverflowError, RecursionError):
                    units = {}
            for key, record in entries:
                units.setdefault(key, []).append(record)
            units = _tidy(units, void)
            descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": REUSE_VERSION, "units": units}, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, path)
            temporary = None
        return True
    except (OSError, ValueError) as exc:
        print(f"注意: 使い回しの記録を書けない({path}: {exc})。今回の結果は記録しない", flush=True)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
    if failed:
        try:
            _mark_void(path)
        except OSError as exc:
            print(f"注意: 使い回しの記録を無効にできない({path}: {exc})。次の --reuse の回の前に、記録のファイルを消す",
                  flush=True)
    return False
