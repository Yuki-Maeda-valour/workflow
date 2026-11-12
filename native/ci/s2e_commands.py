#!/usr/bin/env python3
"""S2E 専用比較の 2 段目 b-1d(#470): bootstrap・verify・host-settings・host-allowlist と内部 reader の旧新比較。

`check_s2e.py` が使う部品。旧 guard(`environment-guard.py`)を変えず、同じ物理入力を旧版と Rust で順に流す。

- 入力: `s2e_fixtures/gen_cases.py` の 9 コマンドの流れ(`cmd_cases`・`entry_depth_cases`)と、この部品の追加の入力。
  生成スクリプトは、同じ入力から旧版の固定期待を作る。ここでは固定期待でなく、旧版と Rust を同じ入力で実際に流す。
- bootstrap: 旧版を流し、出力先を退避してから同じ出力先で Rust を流す。控え・受領・コピーの木のバイト列を、
  置き換えなしで比べる。受領の `sha256` が控えの生バイトの SHA-256 と一致することも別に確かめる。
- 読取りだけのコマンド: 旧版の前後・Rust の前後で、入力の内容・属性・リンクが変わらないことを確かめる。
- 認めた差(U2 など)は通常の一致に混ぜず、旧新それぞれの固定期待を別に確かめる。
- 各項目に「全機種で必須(`ALL`)」か「Linux だけ(`LINUX`)」の印を付ける。
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from support_native import RunResult  # noqa: E402

ALL, LINUX_ONLY = 'all', 'linux'
IS_LINUX = sys.platform.startswith('linux')
IS_MAC = sys.platform == 'darwin'
# 比較器の検査用: Linux で Mac の経路(旧版の bootstrap は patched・Rust の bootstrap は流さない・Linux だけの印の項目は流さない)を模擬する。
# Mac 実機の動きの確認ではない(Mac 実機は未確認)。
SIMULATE_MAC = os.environ.get('S2E_SIMULATE_MAC') == '1'
MAC_PATH = IS_MAC or SIMULATE_MAC
# Linux での模擬(Mac 実機ではない)。patched なしの旧版の bootstrap は、既定の監視先 /etc を「/etc へのリンク」に置き換えて流す(Mac では /etc が
# /private/etc へのリンクで失敗する見込みなので、同じ漏れを Linux で見つける)。`run_mac_default` だけは置き換えず、実際の失敗の一致を見る。
SIMULATED_MAC_ONLY = SIMULATE_MAC and not IS_MAC
LANG_NAME = 'C.UTF-8' if IS_LINUX else 'en_US.UTF-8'
TOOL_TIMEOUT = 120
GEN_PATH = HERE / 's2e_fixtures/gen_cases.py'
GUARD_REL = 'plugin/skills/ship-task/scripts/environment-guard.py'
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ENTRIES = 10000

_GEN = None


def gen():
    """固定期待の生成スクリプトを、入力の定義(流れ・配置・操作)の元として読む。"""
    global _GEN
    if _GEN is None:
        spec = importlib.util.spec_from_file_location('s2e_gen_cases_for_check', GEN_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _GEN = module
    return _GEN


# ---------------------------------------------------------------- 起動

def GUARD_BYTES():
    return (ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/environment-guard.py').read_bytes()


def inherited_nofile():
    """受け継いだ RLIMIT_NOFILE の(soft, hard)。比較器は設定せず記録だけする(2026-11-12 00:42 の決定: 処理系の資源で境目が決まる条件は比較の前提の外)。"""
    return resource.getrlimit(resource.RLIMIT_NOFILE)


def run_tool(argv, *, env, cwd, timeout=None, stdin=None, fsize=None):
    """別のプロセスグループで起動する。`fsize` は書けるファイルの大きさの上限(RLIMIT_FSIZE。None なら変えない)。
    上限を設定できなければ AssertionError(黙って続けない)。NOFILE などの処理系の資源は設定しない。
    起動できなければ OSError をそのまま投げる。時間切れは `timed_out` を立てる。"""
    timeout = TOOL_TIMEOUT if timeout is None else timeout

    def preexec():
        if fsize is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True, preexec_fn=preexec,
                                   stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.SubprocessError as error:
        raise AssertionError(f'起動前の上限の設定に失敗した(fsize={fsize}): {error}') from None
    with process:
        try:
            timed_out = False
            try:
                out, err = process.communicate(input=stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                for number in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, number)
                    except ProcessLookupError:
                        pass
                    try:
                        process.communicate(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                out, err = process.communicate()
            return RunResult(process.returncode, out or b'', err or b'', timed_out)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise


def os_bytes(value):
    return os.fsencode(value) if isinstance(value, str) else value


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def remove_tree(path):
    """自分で作った作業用のフォルダだけを消す。深い木は再帰に頼らず `rm -rf` で消す。"""
    path = str(path)
    if not os.path.lexists(path):
        return
    subprocess.run(['chmod', '-R', 'u+rwx', path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['rm', '-rf', path], check=False)
    if os.path.lexists(path):
        raise AssertionError('作業用のフォルダを消せない: ' + path)


# ---------------------------------------------------------------- 木の読取り(再帰を使わない)

def tree_snapshot(root, *, skip=(), content=True):
    """入力の内容・属性・リンクの一覧。ディレクトリ・通常ファイル・リンク・FIFO を、lstat と内容(通常ファイルだけ)で持つ。
    `skip` は `root` からの相対パス(その下は見ない)。FIFO は開かない。"""
    root = str(root)
    result = {}
    stack = ['']
    skip = set(skip)
    while stack:
        rel = stack.pop()
        path = os.path.join(root, rel) if rel else root
        st = os.lstat(path)
        info = [stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                st.st_ino, st.st_nlink]
        if stat.S_ISLNK(st.st_mode):
            info.append(os.readlink(path))
        elif stat.S_ISREG(st.st_mode) and content:
            try:
                with open(path, 'rb') as handle:
                    info.append(sha256(handle.read()))
            except PermissionError:
                info.append('unreadable')   # 読めない入力(mode 0200 など)は、lstat の値だけで比べる
        result[rel] = info
        if stat.S_ISDIR(st.st_mode):
            try:
                names = sorted(entry.name for entry in os.scandir(path))
            except PermissionError:
                result[rel].append('unreadable')
                continue
            for name in names:
                child = os.path.join(rel, name) if rel else name
                if child not in skip:
                    stack.append(child)
    return result


def tree_bytes(root):
    """出力フォルダの木のバイト列(名前・種類・mode・内容)。時刻・dev・ino は持たない(出力の実体だけを比べる)。"""
    root = str(root)
    result = {}
    stack = ['']
    while stack:
        rel = stack.pop()
        path = os.path.join(root, rel) if rel else root
        st = os.lstat(path)
        kind = stat.S_IFMT(st.st_mode)
        info = [kind, stat.S_IMODE(st.st_mode)]
        if stat.S_ISLNK(st.st_mode):
            info.append(os.readlink(path))
        elif stat.S_ISREG(st.st_mode):
            with open(path, 'rb') as handle:
                info.append(handle.read())
        elif stat.S_ISDIR(st.st_mode):
            for name in sorted(entry.name for entry in os.scandir(path)):
                stack.append(os.path.join(rel, name) if rel else name)
        result[rel] = info
    return result


def first_tree_difference(a, b):
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            if key not in a or key not in b:
                return f'{key or "."}: 片方にしか無い'
            if a[key][:2] != b[key][:2]:
                return f'{key or "."}: 種類か mode が違う({a[key][:2]} と {b[key][:2]})'
            return f'{key}: 内容が違う'
    return None


def state_digest(raw, root):
    """控えの本文を比べる値。dev・ino・時刻の位置だけを伏せ(固定期待の `obs_mask_entry` と同じ位置)、ほかは鍵も値も全部を比べる
    (固定期待の `cmd_norm_state` のように、`@origin:` などの鍵を捨てない)。JSON の控えでなければ生バイトの SHA-256。"""
    try:
        value = json.loads(raw)
    except ValueError:
        return sha256(raw)
    if not isinstance(value, dict) or not isinstance(value.get('entries'), dict):
        return sha256(raw)
    g = gen()
    normalized = {key: item for key, item in value.items() if key != 'entries'}
    normalized['entries'] = {key: g.obs_mask_entry(item) for key, item in value['entries'].items()}
    return sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=True).encode())


def listing(directory, root):
    """出力フォルダの構成(名前・種類・mode・内容の SHA-256)。作り直した別の入力どうしを比べる用なので、控えの本文だけは dev・ino・時刻を伏せた値で比べる。"""
    out = []
    for key, info in sorted(tree_bytes(directory).items()):
        if len(info) > 2 and isinstance(info[2], bytes):
            out.append([key, info[0], info[1], state_digest(info[2], root) if key == 'environment.json' else sha256(info[2])])
        else:
            out.append([key, info[0], info[1]])
    return out


# ---------------------------------------------------------------- 偽の git(呼出しの数え・競合の差し込み)

FAKE_GIT = r'''#!@PYTHON@ -IS
import json, os, shutil, subprocess, sys, time
base = "@BASE@"
data = sys.stdin.buffer.read()
with open(base + "/calls.log", "a") as handle:
    handle.write("x\n")
race = base + "/race.json"
if os.path.exists(race):
    config = json.load(open(race))
    number = len(open(base + "/calls.log").read().splitlines())
    if number == config["call"] and config.get("stop"):
        open(base + "/stopped", "w").close()
        time.sleep(60)
    if number == config["call"]:
        time.sleep(0.03)
        for op in config["ops"]:
            path = op.get("path")
            kind = op["op"]
            if kind == "write":
                with open(path, "wb") as out:
                    out.write(op["body"].encode())
            elif kind == "rm":
                os.unlink(path)
            elif kind == "rmtree":
                shutil.rmtree(path)
            elif kind == "link":
                if os.path.lexists(path):
                    os.unlink(path)
                os.symlink(op["target"], path)
            elif kind == "rename":
                os.rename(path, op["to"])
            elif kind == "chmod":
                os.chmod(path, op["mode"])
            elif kind == "mkdir":
                os.makedirs(path, exist_ok=True)
            elif kind == "fifo":
                os.mkfifo(path)
done = subprocess.run(["@GIT@"] + sys.argv[1:], input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
sys.stdout.buffer.write(done.stdout)
sys.exit(done.returncode)
'''


def fake_git_text(base, real_git):
    for value in (sys.executable, str(base), real_git):
        assert "'" not in value and '"' not in value and '\\' not in value and ' ' not in value, value
    return FAKE_GIT.replace('@PYTHON@', sys.executable).replace('@BASE@', str(base)).replace('@GIT@', real_git)


# ---------------------------------------------------------------- 旧版の reader(host-check.py と同じ読み込み方・同じ呼出しの深さ)

HOST_CHECK = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/host-check.py'

OLD_READER = r"""import importlib.util, json, re, sys
from pathlib import Path


def typed(value):
    if value is None:
        return {"t": "none"}
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": str(value)}
    if isinstance(value, float):
        return {"t": "float", "v": repr(value)}
    if isinstance(value, str):
        return {"t": "str", "v": value}
    if isinstance(value, bytes):
        return {"t": "bytes", "v": value.hex()}
    if isinstance(value, list):
        return {"t": "list", "v": [typed(item) for item in value]}
    if isinstance(value, dict):
        return {"t": "dict", "v": [[typed(k), typed(v)] for k, v in value.items()]}
    raise SystemExit("型が不明: " + repr(type(value)))


class PreStep(Exception):
    pass


PRE = []   # host-check の前処理で止まったか(保持 reader に届かなかった)


spec = importlib.util.spec_from_file_location("host_check_for_s2e", sys.argv[4])
HC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(HC)
FIXED = {"read_host_settings": ("host-settings の保持記述子が無い", "host-settings の reader が保持値と違う"),
         "read_components": ("component の保持 reader が無い", "component の保持 reader が保持値と違う"),
         "component_policy_digest": (None, "component の保持 reader が保持値と違う")}
VERIFIED = {"read_host_settings": "verified_host_settings", "read_components": "verified_components", "component_policy_digest": "verified_component_digest"}


def raw_verified(name, state_path, expected):
    # host-check.py の verified_* と同じ手順(load_state・保持記述子の形・read_regular(MAX_TEXT)・sha256 の照合・検証済みの生バイトの exec)。
    # 固定の理由への置き換えの前まで同じにして、保持 reader の戻りをそのまま返す(呼出しの深さも同じ 4)。
    try:
        value = HC.load_state(state_path, expected)
        missing, differs = FIXED[name]
        if name == "read_host_settings" and (not isinstance(value.get("host_settings"), list) or not re.fullmatch("[0-9a-f]{64}", value.get("guard_sha256", ""))):
            raise HC.Stop(missing)
        if name == "read_components" and (not isinstance(value.get("guard_sha256"), str) or not re.fullmatch("[0-9a-f]{64}", value["guard_sha256"])):
            raise HC.Stop(missing)
        path = Path(state_path).parent / "environment-guard.py"
        raw = HC.read_regular(str(path), HC.MAX_TEXT)
        if HC.sha256(raw) != value.get("guard_sha256"):
            raise HC.Stop(differs)
    except Exception as caught:
        raise PreStep(caught)
    namespace = {"__name__": "verified_environment_guard", "__file__": str(path)}
    exec(compile(raw, str(path), "exec"), namespace)
    reader = namespace.get(name)
    return reader(state_path, expected)


def raw_layer(name, state_path, expected):
    try:
        return {"ok": typed(raw_verified(name, state_path, expected))}
    except PreStep as caught:
        inner = caught.args[0]
        PRE.append(True)
        return {"error": {"type": type(inner).__name__, "msg": str(inner)}}
    except Exception as caught:
        return {"error": {"type": type(caught).__name__, "msg": str(caught)}}


def direct_verified(name, state_path, expected):
    # host-check の前処理(load_state・保持記述子の形・sha256 の照合)を通さず、控えの隣の guard のコピーの reader を同じ深さ(4)で呼ぶ。
    path = Path(state_path).parent / "environment-guard.py"
    try:
        raw = path.read_bytes()
        code = compile(raw, str(path), "exec")
    except Exception as caught:
        raise PreStep(caught)
    namespace = {"__name__": "verified_environment_guard", "__file__": str(path)}
    exec(code, namespace)
    reader = namespace.get(name)
    return reader(state_path, expected)


def direct_layer(name, state_path, expected):
    try:
        return {"ok": typed(direct_verified(name, state_path, expected))}
    except PreStep as caught:
        return {"unavailable": type(caught.args[0]).__name__}
    except Exception as caught:
        return {"error": {"type": type(caught).__name__, "msg": str(caught)}}


def hc_layer(name, state_path, expected):
    try:
        result = getattr(HC, VERIFIED[name])(state_path, expected)
    except Exception as caught:
        return {"error": {"type": type(caught).__name__, "msg": str(caught)}}
    if name == "read_host_settings":
        return {"ok": typed(result)}
    if name == "read_components":
        return {"ok": [item["key"] for item in result]}
    return {"ok": result}


def main():
    name = {"reader-host-settings": "read_host_settings", "reader-components": "read_components",
            "reader-policy-digest": "component_policy_digest"}[sys.argv[1]]
    raw = raw_layer(name, sys.argv[2], sys.argv[3])
    layers = [raw, {"pre": bool(PRE), "hc": hc_layer(name, sys.argv[2], sys.argv[3])}]
    if PRE:
        layers.append(direct_layer(name, sys.argv[2], sys.argv[3]))   # 前処理で止まった入力だけ、前処理を通さない呼出しも比べる
    for line in layers:
        sys.stdout.write(json.dumps(line, ensure_ascii=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
"""
# 上の呼出しの深さ: <module>(1) → main(2) → raw_layer・direct_layer か hc_layer(3) → raw_verified・direct_verified か host-check.py の verified_*(4)。
# reader を呼ぶのは 4 番目のフレーム(host-check.py の component-policy・grants の経路と同じ: <module> → main → component_policy → verified_*)。
READER_CALLER_DEPTH = 4
def expected_host_check_layer(command, raw, layer):
    """旧版の reader の戻り `raw`(型の印つき)と、host-check.py の verified_*(固定の理由への置き換えと、戻りの形の検査を含む)の戻り `hc` が、
    つながっていることを確かめる。保持 reader が成功したなら、host-check も成功する(host-settings は戻りが同じ・components は key の並びが同じ・
    digest は文字列)。保持 reader が失敗したなら、host-check は固定の理由の Stop で失敗する(固定の理由は host-check.py の表で決まり、R4H で扱う)。
    host-check の前処理(控えの形・guard の照合)で止まったときは、前処理の拒否が同じ文面で出る。"""
    hc = layer['hc']
    if layer['pre']:
        if hc != {'error': raw['error']}:
            raise AssertionError(f'host-check の前処理の拒否が一致しない: {raw!r} と {hc!r}')
        return
    if 'error' in raw:
        error = hc.get('error')
        if not isinstance(error, dict) or error.get('type') != 'Stop' or not isinstance(error.get('msg'), str) or not error['msg']:
            raise AssertionError(f'{command}: 保持 reader の失敗が host-check で固定の理由の Stop にならない: {raw!r} と {hc!r}')
        return
    if command == 'reader-host-settings':
        want = {'ok': raw['ok']}
    elif command == 'reader-components':
        keys = []
        for item in raw['ok']['v']:
            for key, value in item['v']:
                if key == {'t': 'str', 'v': 'key'}:
                    keys.append(value['v'])
        want = {'ok': keys}
    else:
        if not isinstance(hc.get('ok'), str):
            raise AssertionError(f'{command}: host-check の digest が文字列でない: {hc!r}')
        return
    if hc != want:
        raise AssertionError(f'{command}: host-check の戻りが保持 reader の戻りと違う: {hc!r} と {want!r}')


# ---------------------------------------------------------------- 旧版の bootstrap を、既定の監視先だけ実パスへ置き換えて流す(Mac 用)

OLD_PATCHED = r'''import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("environment_guard_old", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real = os.path.realpath("/etc")
original = module.default_specs


def fix(value):
    if isinstance(value, str):
        return real + value[4:] if value == "/etc" or value.startswith("/etc/") else value
    if isinstance(value, list):
        return [fix(item) for item in value]
    if isinstance(value, dict):
        return {key: fix(item) for key, item in value.items()}
    return value


module.default_specs = lambda: fix(original())
sys.argv = [sys.argv[1]] + sys.argv[2:]
raise SystemExit(module.main())
'''


REAL_ETC_LINE = 'real = os.path.realpath("/etc")'
assert REAL_ETC_LINE in OLD_PATCHED


# ---------------------------------------------------------------- 流れ

@dataclasses.dataclass
class Tally:
    runs: int = 0              # 旧新を比べた起動の数(通常の一致)
    bootstrap_bytes: int = 0   # bootstrap の出力一式のバイト比較
    cross: int = 0             # 相互読取り
    readers: int = 0           # 内部 reader の値の比較
    readonly: int = 0          # 読取りだけの前後確認
    approved: int = 0          # 認めた差(旧新それぞれの固定期待)
    rust_runs: int = 0         # Rust を起動した比較の数(bootstrap か、控えが有る照合・reader)。0 件の項目は何も比べていないので不合格
    excluded: list = dataclasses.field(default_factory=list)  # 比較から外した物(理由つき。U5 など)
    notes: list = dataclasses.field(default_factory=list)     # 測った値(上限の端・fd の端など。比較から外した物ではない)

    def add(self, other):
        for field in ('runs', 'bootstrap_bytes', 'cross', 'readers', 'readonly', 'approved', 'rust_runs'):
            setattr(self, field, getattr(self, field) + getattr(other, field))
        self.excluded += other.excluded
        self.notes += other.notes


def same_result(label, old, new):
    if old.timed_out or new.timed_out:
        raise AssertionError(f'{label}: 時間切れ')
    if (old.returncode, old.stdout, old.stderr) != (new.returncode, new.stdout, new.stderr):
        raise AssertionError(f'{label}: 旧新が一致しない\n旧: {old.returncode} {old.stdout[:300]!r} {old.stderr[:600]!r}\n'
                             f'新: {new.returncode} {new.stdout[:300]!r} {new.stderr[:600]!r}')


class Flow:
    """1 つの流れ(配置 → 実行の列)を、旧版と Rust で順に流す。"""

    def __init__(self, case, fixture, tmp_parent, mode=None, tool='both', base=None):
        self.case, self.fixture = case, Path(fixture)
        self.mode = mode or ('patched' if MAC_PATH else 'cli')   # Mac の bootstrap は、旧版を既定の監視先だけ実パスへ置き換えて流す
        self.tool = tool                                           # 'both'(同じ入力を順に旧新で流す)か、'old'・'new'(1 つだけ。競合の差し込みの回)
        self.g = gen()
        if base is None:
            self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix='s2e-', dir=tmp_parent)))
        else:
            self.base = Path(base)
            self.base.mkdir()
        self.root = self.base / 'case'
        self.tally = Tally()
        self.shas = {}
        self.counter = 0
        self.log = []
        self.raced = False
        self.mac_link = True   # Mac の経路の模擬で、patched なしの旧版の bootstrap の既定の監視先を /etc へのリンクにするか

    # -- 後始末
    def close(self):
        remove_tree(self.base)

    # -- 組み立て
    def setup(self):
        g = self.g
        self.root.mkdir()
        os.chmod(self.root, 0o775)
        layout = self.case['layout']
        g.cmd_build(str(self.root), layout)
        if self.case.get('real_distribution'):
            # 実際の配布物(このリポジトリの plugins/dev-workflow)を、配布物の根へ写す(旧テストと同じ)。
            shutil.copytree(ROOT / 'plugins/dev-workflow', self.root / 'plugin', symlinks=True, dirs_exist_ok=True)
        if not self.case.get('no_guard'):
            g.cmd_place_guard(str(self.root))
        self.env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.root / 'home'), 'GIT_CONFIG_NOSYSTEM': '1',
                    'LANG': LANG_NAME}
        bindir = self.base / 'bin'
        bindir.mkdir()
        real_git = shutil.which('git') or '/usr/bin/git'
        (bindir / 'git').write_text(fake_git_text(self.base, real_git))
        os.chmod(bindir / 'git', 0o755)
        self.env['PATH'] = f'{bindir}:{self.env["PATH"]}'
        self.calls = self.base / 'calls.log'
        for key, value in self.case.get('env', {}).items():
            self.env[key] = g.cmd_sub(value, str(self.root)) if value is not None else None
            if value is None:
                self.env.pop(key, None)
        os.mkdir(self.root / 'out')
        os.chmod(self.root / 'out', 0o755)
        self.guard = self.root / GUARD_REL
        if self.case.get('old_elsewhere'):
            # 旧版を、配布物の外に置いた guard から起動する(配布物の guard を変える・消す入力のため)。
            elsewhere = self.base / 'elsewhere' / 'environment-guard.py'
            elsewhere.parent.mkdir()
            elsewhere.write_bytes(GUARD_BYTES())
            self.old_script_override = elsewhere
        (self.base / 'old_reader.py').write_text(OLD_READER)
        (self.base / 'old_patched.py').write_text(OLD_PATCHED)
        if SIMULATED_MAC_ONLY:
            link = self.base / 'etc-link'
            os.symlink('/etc', link)
            (self.base / 'old_maclink.py').write_text(OLD_PATCHED.replace(REAL_ETC_LINE, f'real = {str(link)!r}'))

    # -- 起動
    def old_argv(self, args, patched=False):
        if patched:
            return [sys.executable, '-B', str(self.base / 'old_patched.py'), str(self.guard), *map(os_bytes, args)]
        if SIMULATED_MAC_ONLY and self.mac_link and args and args[0] == 'bootstrap':
            return [sys.executable, '-B', str(self.base / 'old_maclink.py'), str(self.guard), *map(os_bytes, args)]
        return [sys.executable, '-B', str(self.guard), *map(os_bytes, args)]

    def run_old(self, args, patched=False, env=None, script=None, fsize=None):
        argv = self.old_argv(args, patched)
        script = script if script is not None else getattr(self, 'old_script_override', None)
        if script is not None:
            argv[argv.index(str(self.guard))] = str(script)
        return run_tool(argv, env=env or getattr(self, 'run_env', None) or self.env, cwd=self.root, fsize=fsize)

    def run_new(self, args, env=None, fsize=None):
        result = run_tool([str(self.fixture), *map(os_bytes, args)], env=env or getattr(self, 'run_env', None) or self.env, cwd=self.root, fsize=fsize)
        if self.is_meaningful_rust_run(args):
            self.tally.rust_runs += 1
        return result

    @staticmethod
    def is_meaningful_rust_run(args):
        """Rust を起動した数に入れる起動か。bootstrap と、控えが有る照合だけ数える(失敗した bootstrap の後の、控えが無い照合は何も比べていない)。"""
        if not args or args[0] == 'bootstrap' or '--state' not in args:
            return True
        return os.path.lexists(args[args.index('--state') + 1])

    def apply_ops(self, ops):
        """入力への操作を順に当てる。`flip` は、通常ファイルの 1 バイトの 1 ビットだけを反転する(大きさ・mode は変えない)。"""
        group = []
        for op in ops:
            if op['op'] != 'flip':
                group.append(op)
                continue
            if group:
                self.g.obs_apply(str(self.root), group)
                group = []
            time.sleep(0.03)
            path = self.root / op['path']
            offset = op.get('offset', 0)
            with open(path, 'r+b') as handle:
                handle.seek(offset)
                byte = handle.read(1)
                handle.seek(offset)
                handle.write(bytes([byte[0] ^ 1]))
        if group:
            self.g.obs_apply(str(self.root), group)

    def reset_calls(self):
        if self.calls.exists():
            self.calls.unlink()

    def call_count(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def arm_race(self, run):
        race = run.get('race')
        path = self.base / 'race.json'
        if race:
            ops = []
            for op in race['ops']:
                item = dict(op)
                for key in ('path', 'to', 'target'):
                    if key in item:
                        item[key] = item[key].replace('{r}', str(self.root))
                        if key in ('path', 'to') and not os.path.isabs(item[key]):
                            item[key] = str(self.root / item[key])
                ops.append(item)
            path.write_text(json.dumps({'call': race['call'], 'ops': ops}))
        elif path.exists():
            path.unlink()

    # -- 実行
    def go(self):
        try:
            self.setup()
            for index, run in enumerate(self.case['runs']):
                self.step(index, run if isinstance(run, dict) else {'args': run})
            if self.tool == 'both':
                if self.case['name'].startswith('x-tilde-'):
                    self.tally.excluded.append(f'{self.case["name"]}: {U6_UNCONFIRMED}')
                if self.case.get('no_readers'):
                    reason = self.case.get('no_readers_reason')
                    if not reason:
                        raise AssertionError(f'{self.case["name"]}: reader を比べない流れには理由が要る')
                    self.tally.excluded.append(f'{self.case["name"]}: reader を比べない({reason})')
                else:
                    self.compare_readers()
            return self.tally
        finally:
            self.close()

    def step(self, index, run):
        g, root = self.g, str(self.root)
        self.reset_calls()
        if run.get('rewrite'):
            name = run['rewrite']['name']
            self.shas[name] = g.cmd_rewrite_state(root, self.env, name, run['rewrite']['change'])
        if run.get('ops_before'):
            self.apply_ops(run['ops_before'])
        args = [self.sha_token(g.cmd_sub(self.sha_sub(a), root)) for a in run['args']]
        label = f'{self.case["name"]}#{index} {args[0]}'
        self.run_env = None
        if run.get('env'):
            self.run_env = dict(self.env)
            for key, value in run['env'].items():
                if value is None:
                    self.run_env.pop(key, None)
                else:
                    self.run_env[key] = value.replace('{r}', root) if isinstance(value, str) else value
        self.arm_race(run)
        self.step_index = index
        self.raced = bool(run.get('race'))
        if self.tool != 'both':
            self.solo(label, args)
        elif args[0] == 'bootstrap':
            self.bootstrap_pair(label, args)
        else:
            self.readonly_pair(label, args)

    def sha_sub(self, text):
        """`{sha:名}` を、控えの今の生バイトの SHA-256 にする(通常ファイルのときだけ読む。FIFO などは開かず、記録した値を使う)。"""
        if not isinstance(text, str):
            return text
        for match in set(re.findall(r'\{sha:([A-Za-z0-9_-]+)\}', text)):
            path = self.root / 'out' / match / 'environment.json'
            try:
                regular = stat.S_ISREG(os.lstat(path).st_mode)
            except OSError:
                regular = False
            digest = sha256(path.read_bytes()) if regular else self.shas.get(match, '0' * 64)
            text = text.replace('{sha:%s}' % match, digest)
        return text

    def sha_token(self, text):
        """`{sha-bad:名}`(最後の 1 桁を変える)・`{sha-upper:名}`(大文字)・`{sha-short:名}`(63 桁)を、控えの SHA-256 から作る。"""
        if not isinstance(text, str):
            return text
        for kind in ('bad', 'upper', 'short'):
            token = re.search(r'\{sha-' + kind + r':([A-Za-z0-9_-]+)\}', text)
            if token:
                digest = self.shas.get(token.group(1)) or '0' * 64
                value = {'bad': digest[:-1] + ('0' if digest[-1] != '0' else '1'), 'upper': digest.upper(), 'short': digest[:-1]}[kind]
                text = text.replace(token.group(0), value)
        return text

    def solo(self, label, args):
        """旧版か Rust の 1 つだけで流し、結果を記録する(競合の差し込みの回。入力が 1 回の実行で変わるので、同じ入力を 2 度は読めない)。"""
        self.reset_calls()
        out = Path(args[args.index('--output') + 1]) if args[0] == 'bootstrap' and '--output' in args else None
        if args[0] == 'bootstrap' and self.mode == 'patched' and not self.raced:
            # Mac: 競合の無い bootstrap の控えは、旧版を既定の監視先だけ実パスへ置き換えて作る(旧新の回で同じ控えにする)。競合の回だけを旧新で流す。
            result = self.run_old(args, patched=True, env=self.run_env)
        else:
            result = (self.run_old if self.tool == 'old' else self.run_new)(args, env=self.run_env)
        record = {'label': label, 'code': result.returncode, 'stderr': result.stderr, 'timed_out': result.timed_out, 'calls': self.call_count()}
        if args[0] == 'bootstrap':
            if result.returncode == 0:
                receipt = json.loads(result.stdout)
                receipt.pop('sha256')
                record['stdout'] = receipt
                self.record_sha(out)
            else:
                record['stdout'] = result.stdout
            record['listing'] = listing(out, self.root) if out is not None and os.path.lexists(out) else None
        else:
            record['stdout'] = result.stdout
        self.log.append(record)

    def readonly_pair(self, label, args):
        root = self.root
        before = tree_snapshot(root)
        self.reset_calls()
        old = self.run_old(args)
        old_calls = self.call_count()
        after_old = tree_snapshot(root)
        self.reset_calls()
        new = self.run_new(args)
        new_calls = self.call_count()
        after_new = tree_snapshot(root)
        self.check_expected_old(label, old)
        same_result(label, old, new)
        if old_calls != new_calls:
            raise AssertionError(f'{label}: Git の起動回数が違う(旧 {old_calls}・新 {new_calls})')
        if not (before == after_old == after_new):
            raise AssertionError(f'{label}: 読取りだけのコマンドが入力を変えた')
        self.tally.runs += 1
        self.tally.readonly += 1

    def check_expected_old(self, label, old):
        """流れが固定期待(`expect_old`: 手順の番号・終了コード・標準エラー)を持つときは、旧版のその手順の結果が決まった理由であることを確かめる。"""
        want = self.case.get('expect_old')
        if want and want['index'] == self.step_index and (old.timed_out or (old.returncode, old.stdout, old.stderr) != (want['code'], b'', want['stderr'])):
            raise AssertionError(f'{label}: 旧版が決まった理由で止まらない(配置が変わった): 終了コード {old.returncode}・標準出力 {old.stdout[:100]!r}・'
                                 f'標準エラー {old.stderr[:300]!r}。期待: {want["code"]}・{want["stderr"]!r}')

    def bootstrap_pair(self, label, args):
        root = self.root
        out = Path(args[args.index('--output') + 1]) if '--output' in args else None
        existed = out is not None and os.path.lexists(out)
        before = tree_snapshot(root, skip=('out',))
        out_before = tree_bytes(out) if existed and out.is_dir() and not out.is_symlink() else None
        self.reset_calls()
        old = self.run_old(args, patched=self.mode == 'patched')
        old_calls = self.call_count()
        saved = None
        if out is not None and not existed and os.path.lexists(out):
            self.counter += 1
            saved = self.base / f'old-out-{self.counter}'
            os.rename(out, saved)
        self.reset_calls()
        if self.mode == 'cli':
            new = self.run_new(args)
            new_calls = self.call_count()
            same_result(label, old, new)
            if old_calls != new_calls:
                raise AssertionError(f'{label}: Git の起動回数が違う(旧 {old_calls}・新 {new_calls})')
            self.tally.runs += 1
            if saved is not None:
                if not os.path.lexists(out):
                    raise AssertionError(f'{label}: 旧版は出力先を作ったが Rust は作らなかった')
                difference = first_tree_difference(tree_bytes(saved), tree_bytes(out))
                if difference:
                    raise AssertionError(f'{label}: 出力一式のバイト列が違う: {difference}')
                self.tally.bootstrap_bytes += 1
            elif out is not None and not existed and os.path.lexists(out):
                raise AssertionError(f'{label}: 旧版は出力先を作らなかったが Rust は作った')
            elif existed and out_before is not None and tree_bytes(out) != out_before:
                raise AssertionError(f'{label}: 既存の出力先が変わった')
        else:
            # Mac: Rust の bootstrap は既定の監視先(/etc は /private/etc へのリンク)で失敗する。旧版を置き換えた出力一式を使う。
            new = old
            if saved is not None:
                os.rename(saved, out)
                saved = None
            if old.returncode == 0 and out is not None and (out / 'environment.json').exists():
                # 旧版が作った出力一式を、Rust が照合する(Mac で Rust の bootstrap の代わりになる相互読取り)。
                state = out / 'environment.json'
                digest = sha256(state.read_bytes())
                self.checked_verify(label, 'Rust が旧版の出力一式を', lambda: self.run_new(['verify', '--state', str(state), '--expect-sha256', digest]))
                self.checked_verify(label, '旧版が旧版の出力一式を', lambda: self.run_old(['verify', '--state', str(state), '--expect-sha256', digest]))
                self.tally.cross += 1
        if before != tree_snapshot(root, skip=('out',)):
            raise AssertionError(f'{label}: bootstrap が出力先の外の入力を変えた')
        expected = self.case.get('expect_bootstrap')
        if expected is not None and not getattr(self, 'expect_done', False):
            self.expect_done = True
            if (old.returncode == 0) != (expected == 'pass'):
                raise AssertionError(f'{label}: 旧版の最初の bootstrap が {expected} のはずが終了コード {old.returncode}(端の位置が動いた)')
        if old.returncode == 0 and out is not None:
            state_dir = saved if saved is not None else out
            self.check_receipt(label, old.stdout, state_dir, out)
            if self.mode == 'cli':
                self.check_receipt(label, new.stdout, out, out)
            self.record_sha(out)
            if self.mode == 'cli' and saved is not None:
                self.cross(label, saved, out)

    def record_sha(self, out):
        state = out / 'environment.json'
        if state.exists():
            self.shas[out.name] = sha256(state.read_bytes())

    def check_receipt(self, label, stdout, state_dir, out):
        """各受領の sha256 が、その実行の控えの生バイトの SHA-256 と一致する。guard_sha256 はコピーの guard の生バイト。"""
        receipt = json.loads(stdout)
        raw = (state_dir / 'environment.json').read_bytes()
        guard = (state_dir / 'environment-guard.py').read_bytes()
        expected = {'state': str(out / 'environment.json'), 'sha256': sha256(raw), 'guard': str(out / 'environment-guard.py'),
                    'guard_sha256': sha256(guard), 'plugin': str(out / 'plugin')}
        if receipt != expected:
            raise AssertionError(f'{label}: 受領が控えの生バイトの SHA-256 と合わない')
        self.tally.bootstrap_bytes += 1

    def checked_verify(self, label, who, run):
        """相互読取りの verify を流し、成功(終了コード 0・出力なし)することと、読取りだけのコマンドとして前後で入力が変わらないことを確かめる。"""
        before = tree_snapshot(self.root)
        result = run()
        if result.timed_out:
            raise AssertionError(f'{label}: {who}照合する間に時間切れ')
        if (result.returncode, result.stdout, result.stderr) != (0, b'', b''):
            raise AssertionError(f'{label}: {who}照合できない: {result.returncode} {result.stderr[:300]!r}')
        if before != tree_snapshot(self.root):
            raise AssertionError(f'{label}: {who}照合したときに読取りだけのコマンドが入力を変えた')
        self.tally.readonly += 1

    def old_reads_rust(self, label, out, script=None, readers=False):
        """旧版の verify(と、`readers` のときは host-check.py の読み込み方の旧 reader 3 つ)が、Rust の出力一式(今の出力先)を読む。"""
        state = out / 'environment.json'
        digest = sha256(state.read_bytes())
        self.checked_verify(label, '旧版が Rust の控えを', lambda: self.run_old(['verify', '--state', str(state), '--expect-sha256', digest], script=script))
        self.tally.cross += 1
        if readers:
            before = tree_snapshot(self.root)
            for command in ('reader-host-settings', 'reader-components', 'reader-policy-digest'):
                before = self.reader_pair(f'{label} {command}', state, digest, command, before)

    def cross(self, label, old_dir, out, script=None, readers=False):
        """相互読取り。旧版が Rust の控えを読み、Rust が旧版の控えを読む。旧版の出力一式(`old_dir` に退避してある)は、出力先を入れ替えて
        元のパスのまま Rust に照合させる(控えの `copy` の欄が指す出力先は、旧版の出力一式のものになる)。終わったら元に戻す。"""
        self.old_reads_rust(label, out, script, readers)
        self.counter += 1
        rust_dir = self.base / f'rust-out-{self.counter}'
        os.rename(out, rust_dir)
        os.rename(old_dir, out)
        try:
            state = out / 'environment.json'
            digest = sha256(state.read_bytes())
            self.checked_verify(label, 'Rust が旧版の控えを', lambda: self.run_new(['verify', '--state', str(state), '--expect-sha256', digest]))
        finally:
            os.rename(out, old_dir)
            os.rename(rust_dir, out)
        self.tally.cross += 1

    # -- reader
    def compare_readers(self):
        before = tree_snapshot(self.root)
        for name, sha in sorted(self.shas.items()):
            state = self.root / 'out' / name / 'environment.json'
            for command in ('reader-host-settings', 'reader-components', 'reader-policy-digest'):
                before = self.reader_pair(f'{self.case["name"]} {command} {name}', state, sha, command, before)

    def reader_pair(self, label, state, sha, command, before):
        """host-check.py と同じ読み込み方・同じ呼出しの深さの旧 reader と、Rust の reader の隠しコマンドを比べる。
        旧版の 1 行目(保持 reader の戻りの型の印つきの値)が Rust の出力と全バイトで一致し、2 行目(host-check.py の verified_* の戻り。
        固定の理由への置き換えを含む)が期待どおりで、どちらの前後でも入力が変わらない。前後の入力の一覧を返す(次の比較の前の一覧)。"""
        old = run_tool([sys.executable, '-B', str(self.base / 'old_reader.py'), command, str(state), sha, str(HOST_CHECK)], env=self.env, cwd=self.root)
        after_old = tree_snapshot(self.root)
        new = run_tool([str(self.fixture), command, str(state), sha, str(READER_CALLER_DEPTH)], env=self.env, cwd=self.root)
        after_new = tree_snapshot(self.root)
        if old.timed_out or new.timed_out:
            raise AssertionError(f'{label}: 時間切れ')
        lines = old.stdout.split(b'\n')
        if old.returncode != 0 or len(lines) not in (3, 4) or lines[-1] != b'' or new.stdout.count(b'\n') != 1:
            raise AssertionError(f'{label}: reader の出力が 1 行の JSON でない')
        layer = json.loads(lines[1])
        if len(lines) != (4 if layer['pre'] else 3):
            raise AssertionError(f'{label}: reader の出力の行数が前処理の有無と合わない')
        counted = True
        if layer['pre']:
            # host-check.py の前処理(load_state・read_regular など)で止まった入力。前処理の文面は host-check.py のもので Rust の reader には無いので、
            # 控えの隣の guard のコピーの reader を、前処理を通さず同じ深さで呼んだ戻りを、Rust の出力と全バイトで比べる。
            direct = json.loads(lines[2])
            if 'unavailable' in direct:
                counted = False
                self.tally.excluded.append(f'{label}: reader を比べない(host-check の前処理が止め、控えの隣の guard のコピーも読めない: {direct["unavailable"]})')
            else:
                same_result(label, RunResult(old.returncode, lines[2] + b'\n', old.stderr, False), new)
        else:
            same_result(label, RunResult(old.returncode, lines[0] + b'\n', old.stderr, False), new)
        expected_host_check_layer(command, json.loads(lines[0]), layer)
        if not (before == after_old == after_new):
            raise AssertionError(f'{label}: 読取りだけの reader が入力を変えた')
        if counted:
            self.tally.readers += 1
        self.tally.readonly += 1
        self.tally.rust_runs += 1
        return after_new


def run_flow(case, fixture, tmp_parent, mode=None):
    return Flow(case, fixture, tmp_parent, mode=mode).go()


def run_solo_pair(case, fixture, tmp_parent, mode=None):
    """競合を差し込む流れ。旧版の回と Rust の回で、同じパスに配置を作り直して別々に流し、結果の列を比べる。
    比べるもの: 終了コード・標準エラー・Git の起動回数・標準出力(bootstrap の受領は SHA-256 だけ除く)・出力フォルダの構成。"""
    holder = Path(os.path.realpath(tempfile.mkdtemp(prefix='s2e-solo-', dir=tmp_parent)))
    logs = {}
    try:
        tally = Tally()
        for tool in ('old', 'new'):
            flow = Flow(case, fixture, tmp_parent, mode=mode, tool=tool, base=holder / 'w')
            flow.go()
            logs[tool] = flow.log
            if tool == 'new':
                tally.rust_runs += flow.tally.rust_runs
        if len(logs['old']) != len(logs['new']):
            raise AssertionError(f'{case["name"]}: 結果の数が違う')
        for old, new in zip(logs['old'], logs['new']):
            if old['timed_out'] or new['timed_out']:
                raise AssertionError(f'{old["label"]}: 時間切れ')
            if old != new:
                raise AssertionError(f'{old["label"]}: 旧新が一致しない\n旧: {old}\n新: {new}')
            tally.runs += 1
        return tally
    finally:
        remove_tree(holder)


# ---------------------------------------------------------------- 追加の入力

def _layout_without(layout, *prefixes):
    return [item for item in layout if not item['path'].startswith(prefixes)]


PREREAD_CHANGED = 'ERROR [environment-guard] 保持済み Git 設定が変わったため参照先を読まず停止した\n'.encode()
PREREAD_NEW_ROOT = 'ERROR [environment-guard] 不在として保持した利用者設定が新設された\n'.encode()
U6_UNCONFIRMED = 'U6: NSS(LDAP・sssd など)だけにある利用者の ~user と、HOME が無いときの ~ は未確認(ローカルの /etc/passwd の利用者の固定入力だけで一致を確かめた)'
TILDE_REASON = '~user の形(U6)は Linux の /etc/passwd(ユーザーの検索)で測った値。Mac の利用者の検索は未確認'


def extra_cases():
    """生成スクリプトの流れに無い、既存テストの入力と対応表の「予定」の入力。名前は `x-` で始める。
    各要素は (印, 流れ, 'both'か'solo')。"""
    g = gen()
    L, BS, CMD, base = g.L, g.BS, g.CMD, g.cmd_base_layout
    cases = []

    def add(name, layout, runs, mark=None, reason='', kind='both', **kw):
        cases.append((mark, reason, {'name': 'x-' + name, 'layout': layout, 'runs': [r if isinstance(r, dict) else {'args': r} for r in runs], **kw}, kind))

    def op(*ops, args, **kw):
        return {'ops_before': list(ops), 'args': args, **kw}

    # ---- 実際の配布物
    add('real-distribution', _layout_without(base(), 'plugin/'), [
        BS('a'), CMD('verify'), CMD('host-settings'), CMD('host-allowlist'),
        op({'op': 'write', 'path': 'out/a/plugin/skills/ship-task/SKILL.md', 'body': 'tampered'}, args=CMD('verify')),
        op({'op': 'write', 'path': 'plugin/skills/ship-task/SKILL.md', 'body': 'tampered'}, args=CMD('verify')),
        CMD('host-settings'), BS('b'),
    ], real_distribution=True, no_guard=False)
    # 実際の配布物の 3 つ(Rust のバイナリ・launch.sh・resolve-task-dir.py)の 1 ビットの書き換えは、照合が見つける。もう一度反転して戻した後も比べる。
    flips = ('plugin/bin/dev-workflow/linux-x86_64/dev-workflow', 'plugin/bin/dev-workflow/launch.sh', 'plugin/skills/create-task/scripts/resolve-task-dir.py')
    add('real-distribution-flip', _layout_without(base(), 'plugin/'),
        [BS('a'), CMD('verify')] + [step for path in flips for step in (op({'op': 'flip', 'path': path}, args=CMD('verify')), CMD('host-settings'),
                                                                        op({'op': 'flip', 'path': path}, args=CMD('verify')))] + [BS('b')],
        real_distribution=True)
    # ---- 偽の控え(外部の保持値)
    add('forged-state', base(), [
        BS('a'), CMD('verify'),
        ['verify', '--state', '{state:a}', '--expect-sha256', '{sha-bad:a}'], ['verify', '--state', '{state:a}', '--expect-sha256', '{sha-upper:a}'],
        ['verify', '--state', '{state:a}', '--expect-sha256', '{sha-short:a}'], ['verify', '--state', '{state:a}', '--expect-sha256', ''],
        ['verify', '--state', '{r}/out/a/missing.json', '--expect-sha256', '{sha:a}'],
        ['verify', '--state', '{r}/out/a', '--expect-sha256', '{sha:a}'],
        ['host-settings', '--state', '{state:a}', '--expect-sha256', '{sha-bad:a}'],
        ['host-allowlist', '--state', '{state:a}', '--expect-sha256', '{sha-upper:a}'],
        op({'op': 'write', 'path': 'out/a/environment.json', 'body': '{"version":3}'}, args=CMD('verify')),
    ])
    # ---- 空のフォルダの増減
    empty = base() + [L('plugin/emptyd', 'dir'), L('home/.claude/skills/emptys', 'dir')]
    for name, ops in (('plugin-add', [{'op': 'mkdir', 'path': 'plugin/newempty'}]), ('plugin-remove', [{'op': 'rmtree', 'path': 'plugin/emptyd'}]),
                      ('copy-add', [{'op': 'mkdir', 'path': 'out/a/plugin/newempty'}]), ('copy-remove', [{'op': 'rmtree', 'path': 'out/a/plugin/emptyd'}]),
                      ('skills-add', [{'op': 'mkdir', 'path': 'home/.claude/skills/newempty'}]),
                      ('skills-remove', [{'op': 'rmtree', 'path': 'home/.claude/skills/emptys'}])):
        add('empty-dir-' + name, empty, [BS('a'), CMD('verify'), op(*ops, args=CMD('verify')), CMD('host-settings'), CMD('host-allowlist')])
    # ---- 特殊ファイルと親のリンク・差し替え
    add('special-fifo-in-plugin', base() + [L('plugin/skills/f', 'fifo')], [BS('a'), BS('b')])
    add('special-parent-symlink', base() + [L('external/x.txt', body='x'), L('plugin/skills/x', 'link', target='{r}/external')], [BS('a')])
    add('special-settings-fifo', _layout_without(base(), 'home/.claude/settings.json') + [L('home/.claude/settings.json', 'fifo')], [BS('a')])
    add('special-settings-mode-0200', _layout_without(base(), 'home/.claude/settings.json') + [L('home/.claude/settings.json', body='{}', mode=0o200)], [BS('a')])
    add('settings-size-limit', _layout_without(base(), 'home/.claude/settings.json') + [L('home/.claude/settings.json', size=MAX_FILE)], [BS('a'), CMD('verify')])
    add('settings-size-over', _layout_without(base(), 'home/.claude/settings.json') + [L('home/.claude/settings.json', size=MAX_FILE + 1)], [BS('a')])
    add('state-fifo', base(), [BS('a'), op({'op': 'rm', 'path': 'out/a/environment.json'}, {'op': 'fifo', 'path': 'out/a/environment.json'}, args=CMD('verify')),
                                CMD('host-settings'), CMD('host-allowlist')])
    add('reused-output', base(), [op({'op': 'mkdir', 'path': 'out/a'}, {'op': 'write', 'path': 'out/a/environment-guard.py', 'body': 'raise SystemExit(0)'}, args=BS('a')),
                                  BS('b')])
    add('component-restored', base(), [BS('a'),
                                       op({'op': 'write', 'path': 'plugin/skills/a/SKILL.md', 'body': 'changed'}, args=CMD('verify')),
                                       op({'op': 'write', 'path': 'plugin/skills/a/SKILL.md', 'body': 'a skill'}, args=CMD('verify')),
                                       op({'op': 'write', 'path': 'home/.claude/skills/s1/SKILL.md', 'body': 'x'}, args=CMD('verify')),
                                       op({'op': 'write', 'path': 'home/.claude/skills/s1/SKILL.md', 'body': '# skill\n'}, args=CMD('verify'))])
    add('receipt-replaced', base(), [BS('a'),
                                     op({'op': 'write', 'path': 'plugin/README.md', 'body': 'new'}, args=BS('b')),
                                     CMD('verify', 'a'), CMD('verify', 'b'),
                                     ['verify', '--state', '{state:b}', '--expect-sha256', '{sha:a}'],
                                     ['verify', '--state', '{state:a}', '--expect-sha256', '{sha:b}']])
    add('parent-swap-host-settings', base(), [BS('a'), CMD('host-settings'),
                                              op({'op': 'rename', 'path': 'home/.claude', 'to': 'home/old-config'}, {'op': 'mkdir', 'path': 'home/.claude'},
                                                 {'op': 'write', 'path': 'home/.claude/settings.json', 'body': '{"env":{"CLAUDE_CODE_SAFE_MODE":"secret"}}'},
                                                 args=CMD('host-settings')),
                                              CMD('host-allowlist'), CMD('verify')])
    add('config-dir-fifo', base() + [L('fifo-config/settings.json', 'fifo')], [['host-paths'], BS('a')], env={'CLAUDE_CONFIG_DIR': '{r}/fifo-config'})
    add('dropin-tree-link', base() + [L('outside/x.json', body='{}'), L('managed/x', 'dir'), L('managed/managed-settings.d', 'link', target='{r}/outside')],
        [BS('a', '--managed-dir', '{r}/managed')])
    add('settings-single-link', _layout_without(base(), 'home/.claude/settings.json') + [
        L('home/.claude/actual.json', body='{"permissions":{"allow":["Read"]}}'), L('home/.claude/settings.json', 'link', target='actual.json')],
        [BS('a'), CMD('verify'), CMD('host-settings'), CMD('host-allowlist'),
         op({'op': 'write', 'path': 'home/.claude/actual.json', 'body': '{"permissions":{"allow":["Bash"]}}'}, args=CMD('verify')), CMD('host-settings')])
    # ---- 読んではならない先を FIFO・権限の無いファイルにした流れ(Git の設定の先読みの検査の置き換え)。
    # 読むと、FIFO は開いて止まり、権限の無いファイルは別の理由(読めない)で止まる。旧版は読まずに「環境が変わった」で止まるので、
    # Rust が本文を余分に読めば、旧新の結果が違って見つかる。
    # 固定期待: 旧版の 2 回目の verify(入力を壊した後の照合)は、決まった理由(`PREREAD_*`)で終了コード 20 になる。配置が変わって旧版が別の理由
    # (FIFO を開いて止まる・読めない)で止まるようになったら、Rust も同じでも合格にしない(置き換えが何も測らなくなる)。
    changed, newroot = {'index': 2, 'code': 20, 'stderr': PREREAD_CHANGED}, {'index': 2, 'code': 20, 'stderr': PREREAD_NEW_ROOT}
    for kind in ('fifo', '0200'):
        def target(path, kind=kind):
            return L(path, 'fifo') if kind == 'fifo' else L(path, body='[private]\n\ttoken = SENTINEL\n', mode=0o200)

        def make(path, kind=kind):
            return {'op': 'fifo', 'path': path} if kind == 'fifo' else {'op': 'write', 'path': path, 'body': '[private]\n\ttoken = SENTINEL\n', 'mode': 0o200}
        add('preread-root-edit-' + kind, base() + [target('private-config')], [
            BS('a'), CMD('verify'),
            op({'op': 'write', 'path': 'home/.gitconfig', 'body': '[include]\n\tpath = ../private-config\n'}, args=CMD('verify')), CMD('host-settings')],
            expect_old=changed)
        add('preread-new-root-' + kind, _layout_without(base(), 'home/.gitconfig'), [
            BS('a'), CMD('verify'), op(make('home/.gitconfig'), args=CMD('verify')), CMD('host-settings')], expect_old=newroot)
        add('preread-include-edit-' + kind, _layout_without(base(), 'home/.gitconfig') + [
            L('home/.gitconfig', body='[include]\n\tpath = included\n'), L('home/included', body='[user]\n\tname = normal\n'), target('private-config')], [
            BS('a'), CMD('verify'),
            op({'op': 'write', 'path': 'home/included', 'body': '[include]\n\tpath = ../private-config\n'}, args=CMD('verify')), CMD('host-allowlist')],
            expect_old=changed)
        add('preread-absent-include-' + kind, _layout_without(base(), 'home/.gitconfig') + [L('home/.gitconfig', body='[include]\n\tpath = included\n')], [
            BS('a'), CMD('verify'), op(make('home/included'), args=CMD('verify')), CMD('host-settings')],
            expect_old=newroot)
    add('preread-nested-missing-graph', _layout_without(base(), 'home/.gitconfig') + [
        L('home/leaf', body='[user]\n\tname = normal\n'), L('home/included file', body='[include]\n\tpath = leaf\n\tpath = absent\n'),
        L('home/.gitconfig', body='[include]\n\tpath = "included file"\n')], [BS('a'), CMD('verify'), CMD('verify')])
    # ---- dotfile のリンク・循環・dotdot・Git のパス展開
    dot = _layout_without(base(), 'home/.gitconfig', 'home/.claude/settings.json') + [
        L('home/dotfiles/git', body='[include]\npath=include-link\n'), L('home/dotfiles/included', body='[user]\nname=normal\n'),
        L('home/.gitconfig', 'link', target='dotfiles/git'), L('home/include-link', 'link', target='dotfiles/included'),
        L('home/dotfiles/shell', body='export NORMAL=value\n'), L('home/.bashrc', 'link', target='dotfiles/shell'),
        L('home/dotfiles/settings', body='{}'), L('home/.claude/settings.json', 'link', target='../dotfiles/settings')]
    add('dotfile-links', dot, [BS('a'), CMD('verify'), op({'op': 'write', 'path': 'home/dotfiles/included', 'body': '[user]\nname=changed\n'}, args=CMD('verify')),
                               CMD('host-settings')])
    add('dotfile-retarget', dot + [L('home/dotfiles/other', body='[user]\nname=o\n')],
        [BS('a'), op({'op': 'link', 'path': 'home/include-link', 'target': 'dotfiles/other'}, args=CMD('verify')), CMD('host-allowlist')])
    add('dotfile-cycle', _layout_without(base(), 'home/.gitconfig') + [L('home/.gitconfig', 'link', target='.gitconfig')], [BS('a')])
    add('dotfile-target-replaced', dot, [BS('a'), op({'op': 'rm', 'path': 'home/dotfiles/included'}, {'op': 'link', 'path': 'home/dotfiles/included', 'target': 'git'},
                                                    args=CMD('verify'))])
    add('git-include-dotdot-symlink', _layout_without(base(), 'home/.gitconfig') + [
        L('outside/x', body='x'), L('home/link', 'link', target='{r}/outside'), L('home/.gitconfig', body='[include]\npath=link/../outside-secret\n')],
        [BS('a')])
    add('git-include-expansion', _layout_without(base(), 'home/.gitconfig') + [
        L('home/git/config', body='[include]\npath=../common\n'), L('home/common', body='[user]\nname=normal\n'),
        L('home/.gitconfig', body='[include]\npath=~/git/config\n')],
        [BS('a'), CMD('verify'), op({'op': 'write', 'path': 'home/common', 'body': '[user]\nname=changed\n'}, args=CMD('verify'))])
    # ---- インストールのリンク
    skills = [L('checkout/skills/skill%d/SKILL.md' % n, body='通常の skill 本文') for n in range(12)] + [
        L('home/.claude/skills/skill%d' % n, 'link', target='{r}/checkout/skills/skill%d' % n) for n in range(12)]
    add('install-link-official', base() + skills, [BS('a'), CMD('verify'), op({'op': 'write', 'path': 'checkout/skills/skill0/SKILL.md', 'body': '変更'}, args=CMD('verify'))])
    agents = [L('checkout/skills/skill%d/SKILL.md' % n, body='通常') for n in range(3)] + [
        L('home/.agents/skills/skill%d' % n, 'link', target='{r}/checkout/skills/skill%d' % n) for n in range(3)]
    add('install-link-agents', base() + agents, [BS('a', '--skills-dir', '{r}/home/.agents/skills'), CMD('verify'),
                                                 op({'op': 'write', 'path': 'checkout/skills/skill1/SKILL.md', 'body': '変更'}, args=CMD('verify'))])
    add('install-link-retarget', base() + skills + [L('outside/secret', body='EXTERNAL_SECRET')],
        [BS('a'), op({'op': 'link', 'path': 'home/.claude/skills/skill0', 'target': '{r}/outside'}, args=CMD('verify')), CMD('host-allowlist')])
    add('install-link-relative', base() + [L('home/source/SKILL.md', body='normal'), L('home/.claude/skills/local', 'link', target='../../source')],
        [BS('a'), CMD('verify'), op({'op': 'link', 'path': 'home/source/internal', 'target': '{r}/home/.claude/settings.json'}, args=CMD('verify'))])
    add('install-link-agent-kind', base() + [L('outside/a.md', body='agent'), L('home/.claude/agents/ag', 'link', target='{r}/outside')], [BS('a')])
    add('dynamic-directories', base() + [L('home/host-skill/SKILL.md', body='normal'), L('home/.agents/skills/local', 'link', target='{r}/home/host-skill'),
                                         L('home/host-settings/config', body='normal')],
        [BS('a', '--skills-dir', '{r}/home/.agents/skills', '--settings-dir', '{r}/home/host-settings'), CMD('verify'),
         op({'op': 'write', 'path': 'home/host-skill/SKILL.md', 'body': 'changed'}, args=CMD('verify')), CMD('host-settings')])
    add('enterprise-created', base() + [L('managed/placeholder', body='x')],
        [BS('a', '--managed-dir', '{r}/managed'), CMD('verify'),
         op({'op': 'mkdir', 'path': 'managed/.claude/commands/nested'}, {'op': 'write', 'path': 'managed/.claude/commands/nested/one.md', 'body': 'new command'},
            args=CMD('verify')), CMD('host-settings')])
    add('multihop-managed-roots', base() + [
        L('m1/.claude/skills/s/SKILL.md', body='s'), L('m1/managed-settings.json', body='{"permissions":{"allow":["Bash(m1)"]}}'),
        L('m2/.claude/agents/a.md', body='a'), L('m2/managed-settings.json', body='{"permissions":{"allow":["Bash(m2)"]}}'),
        L('hop/h1', 'link', target='{r}/hop/h2'), L('hop/h2', 'link', target='{r}/m1/.claude/skills/s'),
        L('home/.claude/skills/multi', 'link', target='{r}/hop/h1')],
        [BS('a', '--managed-dir', '{r}/m1', '--managed-dir', '{r}/m2'), CMD('verify'), CMD('host-settings'), CMD('host-allowlist'),
         op({'op': 'write', 'path': 'm2/.claude/agents/a.md', 'body': 'changed'}, args=CMD('verify'))])
    # ---- 配布物の変更・リンク・モード
    add('distribution-changes', base(), [BS('a'), op({'op': 'chmod', 'path': 'plugin/skills/ship-task/scripts/run.sh', 'mode': 0o644}, args=CMD('verify')),
                                         op({'op': 'link', 'path': 'plugin/README.md', 'target': '{r}/home'}, args=CMD('verify')),
                                         op({'op': 'rm', 'path': 'plugin/skills/ship-task/scripts/environment-guard.py'}, args=CMD('verify'))],
        old_elsewhere=True)
    add('source-guard-changed', base(), [BS('a'), op({'op': 'write', 'path': GUARD_REL, 'body': '# changed guard\n'}, args=CMD('verify')),
                                         op({'op': 'write', 'path': 'out/a/environment-guard.py', 'body': '# changed copy\n'}, args=CMD('verify')),
                                         CMD('host-settings'), CMD('host-allowlist')], old_elsewhere=True, no_readers=True,
        no_readers_reason='guard のコピーを書き換えた入力。旧 reader は書き換えたコピーを exec し、Rust の reader はコピーを使わない差がある。R4H・D2 の設計で期待を決める')
    # ---- 競合(偽の git が、解析の途中で入力を書き換える。旧版の回と Rust の回を別々に流す)
    race_base = base()
    for name, ops in (
            ('settings-edit', [{'op': 'write', 'path': 'home/.claude/settings.json', 'body': '{"changed":1}'}]),
            ('gitconfig-edit', [{'op': 'write', 'path': 'home/.gitconfig', 'body': '[user]\n\tname = swapped\n'}]),
            ('include-target-edit', [{'op': 'write', 'path': 'home/extra.cfg', 'body': '[user]\n\tname = swapped\n'}]),
            ('settings-fifo', [{'op': 'rm', 'path': 'home/.claude/settings.json'}, {'op': 'fifo', 'path': 'home/.claude/settings.json'}]),
            ('settings-link', [{'op': 'rm', 'path': 'home/.claude/settings.json'},
                               {'op': 'link', 'path': 'home/.claude/settings.json', 'target': '{r}/plugin/README.md'}]),
            ('parent-swap', [{'op': 'rename', 'path': 'home/.claude', 'to': 'home/old-config'}, {'op': 'link', 'path': 'home/.claude', 'target': '{r}/home/old-config'}]),
            ('plugin-edit', [{'op': 'write', 'path': 'plugin/README.md', 'body': 'raced'}]),
            ('copy-edit', [{'op': 'write', 'path': 'out/a/plugin/README.md', 'body': 'raced'}]),
            ('new-skill', [{'op': 'mkdir', 'path': 'home/.claude/skills/new'}, {'op': 'write', 'path': 'home/.claude/skills/new/SKILL.md', 'body': 'n'}]),
            ('state-edit', [{'op': 'write', 'path': 'out/a/environment.json', 'body': '{}'}])):
        add('race-verify-' + name, race_base, [BS('a'), op(*[], args=CMD('verify'), race={'call': 1, 'ops': ops}), CMD('verify'), CMD('host-settings')], kind='solo')
        add('race-host-settings-' + name, race_base, [BS('a'), op(*[], args=CMD('host-settings'), race={'call': 1, 'ops': ops}), CMD('verify')], kind='solo')
    for name, ops in (
            ('settings-edit', [{'op': 'write', 'path': 'home/.claude/settings.json', 'body': '{"changed":1}'}]),
            ('gitconfig-edit', [{'op': 'write', 'path': 'home/.gitconfig', 'body': '[user]\n\tname = swapped\n'}]),
            ('include-target-edit', [{'op': 'write', 'path': 'home/extra.cfg', 'body': '[user]\n\tname = swapped\n'}]),
            ('plugin-edit', [{'op': 'write', 'path': 'plugin/README.md', 'body': 'raced'}]),
            ('new-include-target', [{'op': 'write', 'path': 'home/extra2.cfg', 'body': '[user]\n\tname = new\n'},
                                    {'op': 'write', 'path': 'home/extra.cfg', 'body': '[include]\n\tpath = extra2.cfg\n'}])):
        add('race-bootstrap-' + name, race_base, [op(args=BS('a'), race={'call': 1, 'ops': ops}), CMD('verify'), BS('b')], kind='solo')
    managed = base() + [L('managed/managed-settings.d/10-a.json', body='{"permissions":{"allow":["Bash(m2:*)"]}}')]
    add('race-bootstrap-new-managed-dropin', managed, [
        op(args=BS('a', '--managed-dir', '{r}/managed'), race={'call': 1, 'ops': [{'op': 'write', 'path': 'managed/managed-settings.d/20-new.json', 'body': '{}'}]}),
        CMD('verify'), CMD('host-settings')], kind='solo')
    add('race-verify-new-managed-dropin', managed, [
        BS('a', '--managed-dir', '{r}/managed'),
        op(args=CMD('verify'), race={'call': 1, 'ops': [{'op': 'write', 'path': 'managed/managed-settings.d/20-new.json', 'body': '{}'}]}),
        CMD('host-settings')], kind='solo')
    # ---- ~user の形(U6)
    tilde = base()
    add('tilde-forms', tilde, [
        BS('a', '--setting', '~/s2e-none', '--shell', '~nosuchuser-s2e/rc'), CMD('verify'),
        BS('b', '--setting', '~root/s2e-none'), BS('c', '--config', '~root/.gitconfig-none'), BS('d', '--skills-dir', '~root/skills-none'),
        BS('e', '--setting', '~'), BS('f', '--setting', '~/'), BS('g', '--setting', '~nosuchuser-s2e'), BS('h', '--managed-dir', '~root/m')],
        mark=LINUX_ONLY, reason=TILDE_REASON)
    add('tilde-home-unset', tilde, [BS('a', '--setting', '~/s2e-none'), ['host-paths']], env={'HOME': None}, mark=LINUX_ONLY, reason=TILDE_REASON)
    return cases


# ---------------------------------------------------------------- 上限の直前と直後(guard を含めた項目数と量で作り直す)

def _filler_entries(count):
    g = gen()
    return [g.L('plugin/filler/f%05d' % n, body='') for n in range(count)]


def _filler_bytes(size):
    """合計の読取バイト数を `size` 増やす(1 ファイル 8 MiB 以下に分ける)。"""
    g = gen()
    items, number = [], 0
    while size > 0:
        part = min(size, MAX_FILE)
        items.append(g.L('plugin/filler/b%02d' % number, size=part))
        size -= part
        number += 1
    return items


# 端は、その場で旧版を流して測る(固定の値を持たない)。項目数の端は配置の根のパスの深さ(根へ至る足取りも 1 項目ずつ数えるため)で動き、
# 量の端は既定の監視先(/etc/profile など)と guard の大きさで動く。上限の定数を変えた旧 guard の写しで、bootstrap が通る最小の上限を二分探索し、
# そこから端を決める。
LIMIT_PROBE = r"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location("environment_guard_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
setattr(module, sys.argv[2], int(sys.argv[3]))
sys.argv = [sys.argv[1]] + sys.argv[4:]
raise SystemExit(module.main())
"""
_EDGE_CACHE = {}


def min_passing_limit(tmp_parent, layout, attr, high):
    """旧 guard の上限 `attr`(`MAX_ENTRIES` か `MAX_TOTAL`)を変えて、`layout` の bootstrap が通る最小の値を二分探索で測る。"""
    flow = Flow({'name': 'calibrate', 'layout': layout, 'runs': []}, '/nonexistent-fixture', tmp_parent, tool='old')
    try:
        flow.setup()
        (flow.base / 'probe.py').write_text(LIMIT_PROBE)
        count = [0]

        def passes(limit):
            count[0] += 1
            args = _bootstrap_args(flow.root, f'p{count[0]}')
            result = run_tool([sys.executable, '-B', str(flow.base / 'probe.py'), str(flow.guard), attr, str(limit), *args], env=flow.env, cwd=flow.root)
            if result.timed_out:
                raise AssertionError(f'{attr} の端の測定が時間切れ')
            return result.returncode == 0
        low = 1
        if not passes(high) or passes(low):
            raise AssertionError(f'{attr} の端の測定の前提が崩れた')
        while low + 1 < high:
            middle = (low + high) // 2
            if passes(middle):
                high = middle
            else:
                low = middle
        return high
    finally:
        flow.close()


def calibrate_entry_edge(tmp_parent):
    """このパスの深さで、旧版の bootstrap が通る最大の足し数(`plugin/filler` へ足すファイルの数)を返す。
    足し数 1 の配置の項目数 b1 を測る。ファイル 1 つは項目 1 つなので、端は MAX_ENTRIES - b1 + 1。"""
    depth = len(Path(os.path.realpath(tmp_parent)).parts)
    key = ('entries', depth)
    if key not in _EDGE_CACHE:
        layout = gen().cmd_base_layout() + _filler_entries(1)
        _EDGE_CACHE[key] = MAX_ENTRIES - min_passing_limit(tmp_parent, layout, 'MAX_ENTRIES', MAX_ENTRIES) + 1
    return _EDGE_CACHE[key]


def calibrate_bytes_edge(tmp_parent):
    """旧版の bootstrap が通る最大の足し数(`plugin/filler` へ足すファイルの合計のバイト数)を返す。足し数 0 の配置の読取バイト数 B0 を測り、
    端は MAX_TOTAL - B0。足したファイルが 1 回だけ数えられること(1 MiB 足すと B0 が 1 MiB 増える)も測って確かめる(崩れたら不合格)。"""
    key = ('bytes',)
    if key not in _EDGE_CACHE:
        base = gen().cmd_base_layout()
        b0 = min_passing_limit(tmp_parent, base, 'MAX_TOTAL', MAX_TOTAL)
        b1 = min_passing_limit(tmp_parent, base + _filler_bytes(1 << 20), 'MAX_TOTAL', MAX_TOTAL)
        if b1 - b0 != 1 << 20:
            raise AssertionError(f'量の端の測定の前提が崩れた(1 MiB 足して読取バイト数が {b1 - b0} 増えた)')
        _EDGE_CACHE[key] = MAX_TOTAL - b0
    return _EDGE_CACHE[key]


def boundary_item_cases(tmp_parent, fixture_independent=True):
    """上限の直前(通る最大)と直後(最初に落ちる)の入力。guard を含めた項目数と量で作り直す。旧版の最初の bootstrap が、直前で成功し、直後で失敗する。"""
    g = gen()
    BS, CMD, base = g.BS, g.CMD, g.cmd_base_layout
    edge, bytes_edge = calibrate_entry_edge(tmp_parent), calibrate_bytes_edge(tmp_parent)
    cases = []
    for name, layout, expect in (('entries-before', _filler_entries(edge), 'pass'), ('entries-after', _filler_entries(edge + 1), 'fail'),
                                 ('bytes-before', _filler_bytes(bytes_edge), 'pass'), ('bytes-after', _filler_bytes(bytes_edge + 1), 'fail')):
        cases.append({'name': 'x-limit-' + name, 'layout': base() + layout, 'no_readers': True, 'expect_bootstrap': expect,
                      'no_readers_reason': '上限の端の流れ。reader は端の外の流れで比べる',
                      'runs': [{'args': BS('a')}, {'args': CMD('verify')}, {'args': CMD('host-settings')}]})
    return cases


LIMIT_NAMES = ('x-limit-entries-before', 'x-limit-entries-after', 'x-limit-bytes-before', 'x-limit-bytes-after')


def run_limit(name, fixture, tmp_parent):
    case = next(case for case in boundary_item_cases(tmp_parent) if case['name'] == name)
    tally = run_flow(case, fixture, tmp_parent)
    tally.notes.append(f'上限の端(旧版を上限の定数を変えて流して測定): 項目数の足し数 {calibrate_entry_edge(tmp_parent)}・読取バイト数の足し数 {calibrate_bytes_edge(tmp_parent)}')
    return tally


# ---------------------------------------------------------------- 深い木の成功の側の端(Linux)

DEEP_REASON = 'Mac の PATH_MAX(1024)では約 990 段の絶対パスを作れない(推測)。Mac では未確認'


def deep_dir(count):
    return 'extra/deep/' + '/'.join('a' * count)


def deep_extra_cases():
    """入口の再帰の深さの境目の、成功する側(989 段)と、managed・drop-in を含む深い流れ。旧 reader も同じ呼出しの深さ(4)で比べる。"""
    g = gen()
    BS, CMD, L, base = g.BS, g.CMD, g.L, g.cmd_base_layout
    cases = [{'name': 'x-deep-settings-989', 'layout': base() + [L(deep_dir(989), 'dir')],
              'runs': [BS('a', '--settings-dir', '{r}/extra/deep'), CMD('verify'), CMD('host-settings'), CMD('host-allowlist')]}]
    for count in (989, 990):
        path = deep_dir(count)
        # 深い木の下のファイルは、配置(再帰で親を作る)では作れないので、最初の bootstrap の前の操作で足す。
        ops = [{'op': 'write', 'path': path + '/managed-settings.json', 'body': '{"permissions":{"allow":["Bash(deep:*)"]}}'},
               {'op': 'mkdir', 'path': path + '/managed-settings.d'},
               {'op': 'write', 'path': path + '/managed-settings.d/10-a.json', 'body': '{"permissions":{"allow":["Bash(drop:*)"]}}'}]
        cases.append({'name': f'x-deep-managed-{count}', 'layout': base() + [L(path, 'dir')],
                      'runs': [{'ops_before': ops, 'args': BS('a', '--managed-dir', '{r}/' + path)}, CMD('verify'), CMD('host-settings'), CMD('host-allowlist')]})
    return [(LINUX_ONLY, DEEP_REASON, {**case, 'runs': [r if isinstance(r, dict) else {'args': r} for r in case['runs']]}, 'both') for case in cases]


# ---------------------------------------------------------------- 認めた差(旧新それぞれの固定期待を別に確かめる)

GUARD = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/environment-guard.py'
U2_MISSING = 'ERROR [environment-guard] guard の配布元が配布物に無い\n'.encode()


def _bootstrap_args(root, name, *extra):
    return ['bootstrap', '--root', f'{root}/plugin', '--output', f'{root}/out/{name}', *extra]


def _receipt_and_guard(result, out):
    """成功した bootstrap の受領の guard_sha256 と、出力先のコピーの guard の生バイトの SHA-256(同じはず)を返す。"""
    if result.returncode != 0:
        raise AssertionError(f'bootstrap が失敗した: {result.stderr[:300]!r}')
    receipt = json.loads(result.stdout)
    copy = sha256((out / 'environment-guard.py').read_bytes())
    if receipt['guard_sha256'] != copy:
        raise AssertionError('受領の guard_sha256 がコピーの guard の生バイトと合わない')
    return receipt['guard_sha256']


def run_u2(fixture, tmp_parent):
    """U2: Rust の bootstrap は、配布物の guard(同じ観測の保持バイト)を控える。旧版は、起動した guard 自身(`__file__`)を控える。"""
    g = gen()
    tally = Tally()
    original = GUARD.read_bytes()
    changed = original + b'\n# U2: changed guard\n'
    base_layout = g.cmd_base_layout()
    # (1) 配布物に guard が無い: 旧版は成功し、Rust は終了コード 20(出力先を作らない)。
    flow = Flow({'name': 'u2-no-guard', 'layout': base_layout, 'no_guard': True, 'runs': []}, fixture, tmp_parent)
    try:
        flow.setup()
        elsewhere = flow.base / 'elsewhere' / 'environment-guard.py'
        elsewhere.parent.mkdir()
        elsewhere.write_bytes(original)
        out = flow.root / 'out' / 'a'
        old = flow.run_old(_bootstrap_args(flow.root, 'a'), script=elsewhere)
        if _receipt_and_guard(old, out) != sha256(original):
            raise AssertionError('U2(配布物に guard が無い): 旧版が自分の guard を控えていない')
        remove_tree(out)
        new = flow.run_new(_bootstrap_args(flow.root, 'a'))
        if (new.returncode, new.stdout, new.stderr) != (20, b'', U2_MISSING) or os.path.lexists(out):
            raise AssertionError(f'U2(配布物に guard が無い): Rust の固定期待と違う: {new.returncode} {new.stderr!r}')
        tally.approved += 1
        tally.add(flow.tally)
    finally:
        flow.close()
    # (2) 起動した guard が配布物の guard と違う: 旧版は起動した guard、Rust は配布物の guard を控える。
    flow = Flow({'name': 'u2-guard-differs', 'layout': base_layout, 'runs': []}, fixture, tmp_parent)
    try:
        flow.setup()
        flow.guard.write_bytes(changed)
        elsewhere = flow.base / 'elsewhere' / 'environment-guard.py'
        elsewhere.parent.mkdir()
        elsewhere.write_bytes(original)
        out = flow.root / 'out' / 'a'
        old = flow.run_old(_bootstrap_args(flow.root, 'a'), script=elsewhere)
        old_guard = _receipt_and_guard(old, out)
        saved = flow.base / 'old-out-u2-2'
        os.rename(out, saved)
        new = flow.run_new(_bootstrap_args(flow.root, 'a'))
        new_guard = _receipt_and_guard(new, out)
        if (old_guard, new_guard) != (sha256(original), sha256(changed)):
            raise AssertionError('U2(起動した guard が違う): 旧新それぞれの固定期待と合わない')
        tally.approved += 1
        # 控えのバイト列が旧新で違っても(コピーの guard が違う)、互いの控えを読む: 旧版の verify と host-check の読み込み方の旧 reader が Rust の控えを、
        # Rust が旧版の控えを(出力先を入れ替えて元のパスのまま)。
        flow.cross('u2-guard-differs', saved, out, script=elsewhere, readers=True)
        tally.add(flow.tally)
    finally:
        flow.close()
    # (3) 1 回目と 2 回目の bootstrap の間に、配布物の guard を変えて戻す。旧版は 2 回目に 1 回目のコピーを読み直す。
    flow = Flow({'name': 'u2-second-bootstrap', 'layout': base_layout, 'runs': []}, fixture, tmp_parent)
    try:
        flow.setup()
        label = 'u2-second-bootstrap'
        flow.bootstrap_pair(label + '#1', _bootstrap_args(flow.root, 'a'))
        first_copy = flow.root / 'out' / 'a' / 'environment-guard.py'
        if sha256(first_copy.read_bytes()) != sha256(original):
            raise AssertionError('1 回目のコピーの guard が配布物の guard と違う')
        flow.guard.write_bytes(changed)
        os.chmod(flow.guard, 0o644)
        out = flow.root / 'out' / 'b'
        old = flow.run_old(_bootstrap_args(flow.root, 'b'), script=first_copy)
        old_guard = _receipt_and_guard(old, out)
        saved = flow.base / 'old-out-u2-3'
        os.rename(out, saved)
        new = flow.run_new(_bootstrap_args(flow.root, 'b'))
        new_guard = _receipt_and_guard(new, out)
        if (old_guard, new_guard) != (sha256(original), sha256(changed)):
            raise AssertionError('U2(2 回目の bootstrap): 旧新それぞれの固定期待と合わない')
        tally.approved += 1
        flow.cross('u2-second-bootstrap', saved, out, script=first_copy, readers=True)
        remove_tree(out)
        remove_tree(saved)
        # 戻した後は、旧版(1 回目のコピーから起動)と Rust のバイト列が一致する。
        flow.guard.write_bytes(original)
        os.chmod(flow.guard, 0o644)
        flow.old_script_override = first_copy
        flow.bootstrap_pair(label + '#3', _bootstrap_args(flow.root, 'c'))
        tally.add(flow.tally)
    finally:
        flow.close()
    return tally


def run_u1(fixture, tmp_parent):
    """U1: Rust は文字コードの設定だけに由来する解釈を、UTF-8 と surrogateescape へ固定する。旧版の標準出力は locale で変わる
    (`C.UTF-8`・`C` は surrogateescape で Rust と同じ、`en_US.UTF-8` は strict で違う)。"""
    g = gen()
    tally = Tally()
    case = {'name': 'u1', 'layout': g.cmd_base_layout(), 'runs': [], 'no_readers': True, 'no_readers_reason': 'U1 は host-allowlist の出力の文字コードの比較。reader は別の流れで比べる'}
    flow = Flow(case, fixture, tmp_parent)
    try:
        flow.setup()
        flow.bootstrap_pair('u1#bootstrap', _bootstrap_args(flow.root, 'a'))
        state = ['--state', str(flow.root / 'out/a/environment.json'), '--expect-sha256', flow.shas['a']]
        values = ['Bash(git status)', 'Bash(\udcff)', 'ab\udce9', 'Bash(é )', 'Bash(\udcc3\udca9)']
        for locale in ('C.UTF-8', 'C'):
            env = dict(flow.env, LC_ALL=locale)
            env.pop('LANG', None)
            for value in values:
                args = ['host-allowlist', *state, '--allowed-tools', value]
                old, new = flow.run_old(args, env=env), flow.run_new(args, env=env)
                same_result(f'u1 LC_ALL={locale} {value!r}', old, new)
                tally.runs += 1
        probe = flow.run_old(['--help'], env=dict(flow.env, LC_ALL='en_US.UTF-8'))
        strict = run_tool([sys.executable, '-B', '-c', 'import sys; print(sys.stdout.errors)'], env=dict(flow.env, LC_ALL='en_US.UTF-8'), cwd=flow.root)
        if strict.stdout.strip() != b'strict' or probe.timed_out:
            tally.excluded.append('U1: en_US.UTF-8 の locale が無く、strict の旧版の固定期待は未確認')
        else:
            env = dict(flow.env, LC_ALL='en_US.UTF-8')
            ref_env = dict(flow.env, LC_ALL='C.UTF-8')
            for value in ('Bash(\udcff)', 'Bash(x\udce9)'):
                args = ['host-allowlist', *state, '--allowed-tools', value]
                old, new, reference = flow.run_old(args, env=env), flow.run_new(args, env=env), flow.run_old(args, env=ref_env)
                if new.timed_out or old.timed_out or (new.returncode, new.stdout, new.stderr) != (reference.returncode, reference.stdout, reference.stderr):
                    raise AssertionError(f'U1(en_US.UTF-8 {value!r}): Rust が surrogateescape の旧版(C.UTF-8)と同じでない')
                if (old.returncode, old.stdout, old.stderr) == (reference.returncode, reference.stdout, reference.stderr):
                    raise AssertionError(f'U1(en_US.UTF-8 {value!r}): 旧版が strict の差を示さない(前提が崩れた)')
                tally.approved += 1
        tally.add(flow.tally)
    finally:
        flow.close()
    return tally


def interrupted_run(flow, argv):
    """偽の git の 1 回目の起動で止めて、そこへ信号 2(SIGINT)を送る(利用者の中断)。結果の記録を返す。
    記録: 終了値・標準出力・標準エラーの最後の行・子(同じプロセスグループ)が残ったか・呼出回数。"""
    marker = flow.base / 'stopped'
    if marker.exists():
        marker.unlink()
    flow.reset_calls()
    (flow.base / 'race.json').write_text(json.dumps({'call': 1, 'ops': [], 'stop': True}))
    process = subprocess.Popen(argv, cwd=flow.root, env=flow.env, start_new_session=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not marker.exists():
            if process.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(f'偽の git の止まり所に届かなかった(終了値 {process.poll()})')
            time.sleep(0.02)
        os.kill(process.pid, signal.SIGINT)
        out, err = process.communicate(timeout=30)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    finally:
        (flow.base / 'race.json').unlink()
    left = True
    for _ in range(100):
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            left = False
            break
        time.sleep(0.03)
    if left:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    lines = [line for line in err.decode('utf-8', 'replace').splitlines() if line.strip()]
    return {'code': process.returncode, 'stdout': out, 'last': lines[-1] if lines else '', 'left': left, 'calls': flow.call_count()}


def run_sigint(command, fixture, tmp_parent, mode=None):
    """SIGINT(利用者の中断。利用者の決定: 信号で終わること・終了値・最後の行 KeyboardInterrupt・それまでの標準出力・子を残さないことを比べる。
    呼出履歴の途中の行は比べない)。偽の git が 1 回目の起動で止まる決まった止まり所で、旧版と Rust に信号 2 を送る。
    入力が変わらないことと、出力フォルダが作られたか(名前の一覧)も比べる。"""
    g = gen()
    flow = Flow({'name': f'sigint-{command}', 'layout': g.cmd_base_layout(), 'runs': []}, fixture, tmp_parent, mode=mode)
    try:
        flow.setup()
        if command == 'verify':
            flow.bootstrap_pair('sigint-verify#prepare', _bootstrap_args(flow.root, 'a'))
        out = flow.root / 'out' / 'sig'
        records = {}
        for tool in ('old', 'new'):
            if command == 'bootstrap':
                args = _bootstrap_args(flow.root, 'sig')
            else:
                state = flow.root / 'out/a/environment.json'
                args = ['verify', '--state', str(state), '--expect-sha256', flow.shas['a']]
            argv = flow.old_argv(args) if tool == 'old' else [str(fixture), *args]
            before = tree_snapshot(flow.root, skip=('out',))
            before_out = tree_snapshot(flow.root / 'out/a') if command == 'verify' else None
            record = interrupted_run(flow, argv)
            if before != tree_snapshot(flow.root, skip=('out',)) or (before_out is not None and before_out != tree_snapshot(flow.root / 'out/a')):
                raise AssertionError(f'sigint-{command} {tool}: 中断が入力を変えた')
            record['out'] = sorted(tree_bytes(out)) if os.path.lexists(out) else None
            records[tool] = record
            remove_tree(out)
            if tool == 'new':
                flow.tally.rust_runs += 1
        if records['old']['code'] != -signal.SIGINT or records['old']['last'] != 'KeyboardInterrupt' or records['old']['left']:
            raise AssertionError(f'sigint-{command}: 旧版が信号 2 で終わらない・最後の行が KeyboardInterrupt でない・子が残った(前提が崩れた): {records["old"]}')
        if records['old'] != records['new']:
            raise AssertionError(f'sigint-{command}: 旧新が一致しない\n旧: {records["old"]}\n新: {records["new"]}')
        flow.tally.runs += 1
        return flow.tally
    finally:
        flow.close()


def run_fsize(fixture, tmp_parent):
    """書込みの途中の失敗(利用者の決定: 終了値・空の標準出力・標準エラー・入力が変わらないこと・ほかのファイルを作らないことを比べる。
    途中まで書いたファイルの中身と大きさは比べない)。bootstrap のコピーや控えの書込みを、`RLIMIT_FSIZE` の EFBIG で失敗させる。
    1 バイトは最初の書込みで失敗し、20000 バイトは guard のコピー(大きい)だけが失敗する。偽の git は自分で書くので使わず、本物の git を使う。"""
    g = gen()
    flow = Flow({'name': 'fsize', 'layout': g.cmd_base_layout(), 'runs': []}, fixture, tmp_parent)
    try:
        flow.setup()
        env = dict(flow.env, PATH=os.environ.get('PATH', '/usr/bin:/bin'))
        for limit in (1, 20000):
            out = flow.root / 'out' / f'f{limit}'
            records = {}
            for tool in ('old', 'new'):
                args = _bootstrap_args(flow.root, f'f{limit}')
                before = tree_snapshot(flow.root, skip=('out',))
                result = (flow.run_old if tool == 'old' else flow.run_new)(args, env=env, fsize=limit)
                if result.timed_out:
                    raise AssertionError(f'fsize {limit} {tool}: 時間切れ')
                if before != tree_snapshot(flow.root, skip=('out',)):
                    raise AssertionError(f'fsize {limit} {tool}: 書込みの失敗が出力先の外の入力を変えた')
                records[tool] = {'code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
                                 'names': sorted(tree_bytes(out)) if os.path.lexists(out) else None}
                remove_tree(out)
            if records['old']['code'] == 0 or records['old']['stdout'] != b'':
                raise AssertionError(f'fsize {limit}: 旧版が書込みの失敗にならない(前提が崩れた): {records["old"]}')
            if records['old'] != records['new']:
                raise AssertionError(f'fsize {limit}: 旧新が一致しない\n旧: {records["old"]}\n新: {records["new"]}')
            flow.tally.runs += 1
        return flow.tally
    finally:
        flow.close()


def run_mac_default(fixture, tmp_parent):
    """既定の監視先(`/etc/…`)での bootstrap の旧新比較。Linux は成功どうし、Mac は旧版が失敗する見込み(`/etc` は `/private/etc` へのリンク。
    親を nofollow で開く旧版は失敗すると推測。Mac 実機は未確認)なので、失敗どうしの一致を必須にする。"""
    g = gen()
    flow = Flow({'name': 'mac-default-bootstrap', 'layout': g.cmd_base_layout(), 'runs': []}, fixture, tmp_parent, mode='cli')
    flow.mac_link = False   # 実際の既定の監視先での失敗(か成功)の一致を見る
    try:
        flow.setup()
        args = _bootstrap_args(flow.root, 'a')
        flow.bootstrap_pair('default-watch-bootstrap', args)
        if IS_MAC:
            if os.path.lexists(flow.root / 'out' / 'a') or flow.shas:
                raise AssertionError('Mac: 既定の監視先の bootstrap が成功した(推測が崩れた。旧版も成功したことになる)')
        return flow.tally
    finally:
        flow.close()


def run_patched_sample(fixture, tmp_parent):
    """Mac 用の経路(旧版の bootstrap を import し、既定の監視先だけ実パスへ置き換えて作った出力一式での、verify 以降の旧新比較)を、
    少数の流れで流す。Linux では `/etc` が実パスなので置き換えは何も変えない(Mac の成功には数えない)。"""
    g = gen()
    tally = Tally()
    wanted = ('basic', 'options', 'verify-copy-edit', 'verify-settings-edit', 'include-chain', 'legacy-state', 'host-managed-ok', 'userdir-links')
    for case in g.cmd_cases():
        if case['name'] in wanted:
            tally.add(Flow(case, fixture, tmp_parent, mode='patched').go())
    return tally


# ---------------------------------------------------------------- 一覧と実行

RUST_BOOTSTRAP_REASON = ('Rust の bootstrap が要る(Mac は既定の監視先の /etc が /private/etc へのリンクで、Rust の bootstrap は失敗する見込み。'
                         '旧版を既定の監視先だけ実パスへ置き換えて作る経路では、Rust の bootstrap を 1 回も起動しない)。Mac では未確認')

# 静的な規則(読取りのコマンドが 1 つ以上ある)に当てはまらなくても、Mac の経路では Rust を 1 回も起動しない流れ。Linux で Mac の経路を模擬して見つけた。
NEEDS_RUST_BOOTSTRAP = {}


@dataclasses.dataclass
class Item:
    group: str
    name: str
    mark: str
    run: object
    reason: str = ''


def auto_mark(case, kind='both'):
    """流れの印を決める。bootstrap しか流さない流れと、bootstrap 自身に競合を差し込む流れは、Rust の bootstrap が要るので Linux だけにする
    (Mac の経路は bootstrap を旧版の patched で作るので、Rust を 1 回も起動しない)。"""
    runs = [run if isinstance(run, dict) else {'args': run} for run in case['runs']]
    if case['name'] in NEEDS_RUST_BOOTSTRAP:
        return LINUX_ONLY, NEEDS_RUST_BOOTSTRAP[case['name']]
    if not any(run['args'][0] != 'bootstrap' for run in runs):
        return LINUX_ONLY, RUST_BOOTSTRAP_REASON
    if any(run.get('race') and run['args'][0] == 'bootstrap' for run in runs):
        return LINUX_ONLY, 'bootstrap の途中の競合は、Rust の bootstrap が要る。' + RUST_BOOTSTRAP_REASON
    return ALL, ''


def items(fixture):
    g = gen()
    result = []

    def flow_item(group, case, mark, reason):
        return Item(group, case['name'], mark, lambda tmp, case=case: run_flow(case, fixture, tmp), reason)

    for case in g.cmd_cases():
        # 固定期待の生成が reader を外した流れ(host-deep-ok)も、ここでは旧新を実際に流すので reader も比べる。
        case = {key: value for key, value in case.items() if key != 'no_readers'}
        mark, reason = auto_mark(case)
        result.append(flow_item('flow', case, mark, reason))
    for case in g.entry_depth_cases():
        result.append(flow_item('flow-deep', case, LINUX_ONLY, DEEP_REASON))
    for mark, reason, case, kind in extra_cases() + deep_extra_cases():
        if mark is None:
            mark, reason = auto_mark(case, kind)
        if kind == 'solo':
            result.append(Item('race', case['name'], mark, lambda tmp, case=case: run_solo_pair(case, fixture, tmp), reason))
        else:
            result.append(flow_item('flow-deep' if case['name'].startswith('x-deep') else 'extra', case, mark, reason))
    for name in LIMIT_NAMES:
        result.append(Item('limit', name, LINUX_ONLY, lambda tmp, name=name: run_limit(name, fixture, tmp),
                           '上限の端は Rust の bootstrap で比べる。' + RUST_BOOTSTRAP_REASON))
    result.append(Item('signal', 'x-sigint-bootstrap', LINUX_ONLY, lambda tmp: run_sigint('bootstrap', fixture, tmp), '中断された bootstrap は Rust の bootstrap が要る。' + RUST_BOOTSTRAP_REASON))
    result.append(Item('signal', 'x-sigint-verify', ALL, lambda tmp: run_sigint('verify', fixture, tmp)))
    result.append(Item('failure', 'x-fsize-bootstrap', LINUX_ONLY, lambda tmp: run_fsize(fixture, tmp), '書込みの失敗の bootstrap は Rust の bootstrap が要る。' + RUST_BOOTSTRAP_REASON))
    result.append(Item('u2', 'u2-guard-origin', LINUX_ONLY, lambda tmp: run_u2(fixture, tmp),
                       'U2 の旧新それぞれの固定期待は Rust の bootstrap の控える guard の差で、Mac では Rust の bootstrap を流さない。' + '(Mac では未確認)'))
    result.append(Item('u1', 'u1-locale', LINUX_ONLY, lambda tmp: run_u1(fixture, tmp),
                       'locale の名前が機種で違う(Mac の旧版の標準出力の errors は C.UTF-8 の有無で決まる)。CI の Linux で locale を確かめて固定する(Mac では未確認)'))
    result.append(Item('mac', 'default-watch-bootstrap', ALL, lambda tmp: run_mac_default(fixture, tmp)))
    result.append(Item('mac', 'patched-bootstrap-sample', ALL, lambda tmp: run_patched_sample(fixture, tmp)))
    return result


def run_items(fixture, tmp_parent, *, only=None, log=print):
    """全項目を流す。Linux だけの印の項目は、Mac の経路では流さず、理由つきで一覧に出す。落ちた項目は例外で止める。
    どの項目も、Rust を起動した比較が 1 件以上なければ不合格にする(何も比べずに合格にしない)。"""
    tally = Tally()
    per_group = {}
    skipped = []
    started = time.monotonic()
    for item in items(fixture):
        if only and not any(part in item.name or part in item.group for part in only):
            continue
        if item.mark == LINUX_ONLY and MAC_PATH:
            skipped.append((item.name, item.reason))
            continue
        begin = time.monotonic()
        try:
            result = item.run(tmp_parent)
            if result.rust_runs == 0:
                raise AssertionError('Rust を起動した比較が 0 件(何も比べていない)')
        except AssertionError as error:
            raise AssertionError(f'{item.group}/{item.name}: {error}') from None
        tally.add(result)
        entry = per_group.setdefault((item.group, item.mark), [0, 0.0])
        entry[0] += 1
        entry[1] += time.monotonic() - begin
        log(f'  {item.group}/{item.name}: {time.monotonic() - begin:.1f} 秒')
    return tally, per_group, skipped, time.monotonic() - started
