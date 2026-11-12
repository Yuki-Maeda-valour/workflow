#!/usr/bin/env python3
"""S2Lの同一物理入力に対する生出力・終了値・状態の旧新比較。"""
from __future__ import annotations
import argparse
import errno
import hashlib
import importlib.util
import json
import os
import stat
import sysconfig
from pathlib import Path
import subprocess
import sys
import time
import unittest
import platform
import re
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "plugins/dev-workflow/skills/ship-task/scripts/loop-state.py"
LEGACY_SHA256 = "bc85c2f6b0060371d204f057c7183bd13fbdc04e8523de949d1ccbbfe9a9ea10"
_RUN = subprocess.run


class Mismatch(AssertionError):
    pass


# いま流している(または最後に流した)case。失敗したときに、その label と旧新の結果の末尾を CI に残す。
# case を起動する前に label だけを記録し、旧新の結果は流し終えた側から足す(合否は変えない)。
LAST_CASE = {}


def start_case(label):
    LAST_CASE.clear()
    LAST_CASE.update(label=label, at=time.monotonic_ns())


def record(side, result):
    LAST_CASE[side] = result


def remember(label, old, new):
    if LAST_CASE.get("label") != label:
        start_case(label)
    LAST_CASE.update(old=old, new=new)


def last_case_text(modules=None):
    """いちばん新しく記録した 1 件の case の label と、旧新の結果の末尾。

    本体として起動すると、比較器自身(__main__)と case の部品が読み込む check_loop_state は別の module になる。
    2 つの記録のうち、起動の前に記録した時刻がいちばん新しいものだけを出す。
    """
    if modules is None:
        modules = {id(m): m for m in (sys.modules.get("check_loop_state"), sys.modules.get(__name__)) if m}.values()
    cases = [case for case in (getattr(module, "LAST_CASE", {}) for module in modules) if case]
    if not cases:
        return ""
    case = max(cases, key=lambda row: row["at"])
    finished = case.get("old") is not None and case.get("new") is not None
    rows = [("最後に旧新を流し終えた case: " if finished else "失敗した時に流していた case: ") + case["label"]]
    for side in ("old", "new"):
        result = case.get(side)
        if result is None:
            rows.append(f"  {side}: (結果なし)")
            continue
        rows.append(f"  {side}: 終了値 {getattr(result, 'returncode', None)!r} "
                    f"標準出力の末尾 {(getattr(result, 'stdout', b'') or b'')[-600:]!r} "
                    f"標準エラーの末尾 {(getattr(result, 'stderr', b'') or b'')[-600:]!r}")
    return "\n".join(rows)


def process_group(pid, zombie_wait=3.0):
    """killpg(pid, 0) で、子の process group が無くなったか。(無いか, 見た結果) を返す。

    Mac(XNU の kern_sig.c の killpg1)は、ゾンビだけが残る process group に ESRCH ではなく EPERM を返す。
    そのときだけ、回収されて ESRCH になるまで zombie_wait 秒まで待つ。0(生きている)はすぐに偽を返す
    (Linux の動きは変えない)。
    """
    deadline = time.monotonic() + zombie_wait
    eperm = 0
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True, f"ESRCH(EPERM {eperm} 回の後)" if eperm else "ESRCH"
        except PermissionError:
            eperm += 1
            if time.monotonic() < deadline:
                time.sleep(0.01)
                continue
            return False, f"EPERM {eperm} 回"
        return False, "alive"


def os_label():
    return "macos" if sys.platform == "darwin" else sys.platform


def create_raw(path, create):
    """UTF-8 でない名前を実際に作る。Mac の APFS が EILSEQ で拒むときだけ偽を返す。

    ほかの OS とほかの失敗は例外のまま止める(Linux の検査は変えない)。
    """
    try:
        create(path)
    except OSError as error:
        if sys.platform == "darwin" and error.errno == errno.EILSEQ:
            return False
        raise
    return True


def os_boundary(name, action, control):
    """ファイル系が受け付けない入力の扱いを、Rust の検査と同じ S2L_OS_BOUNDARY の行で出す。

    action は skipped(case を流さない)・not_created(ファイルを作らずに同じ引数で比べる)・
    replaced(作る名前を置き換えて比べる)。control は ASCII の対照の case の label(無ければ none)。
    """
    print(f"S2L_OS_BOUNDARY name={name} os={os_label()} input=non_utf8_name errno={errno.EILSEQ} "
          f"action={action} ascii_control={control}", flush=True)
    return {"name": name, "input": "non_utf8_name", "errno": errno.EILSEQ, "action": action, "ascii_control": control}


# Mac の APFS が UTF-8 でない名前を拒むときの扱い(名前, action)。
# Linux では 1 件も許さない。Mac ではちょうどこの一覧を求める(黙って合格にしない)。
MAC_OS_BOUNDARIES = {
    "arguments": [("arguments-state-raw", "not_created")],
    "contracts": [("E09-snapshot-bare-utf8", "skipped"), ("R2-denied-directory-raw", "skipped"),
                  ("R2-denied-file-raw", "skipped")],
    "encoding": [("encoding-file-names", "replaced")],
    "decisions": [("r3-mode0400-raw-empty", "skipped"), ("r3-mode0400-raw-child", "skipped")],
}


def expected_os_boundaries(group):
    return MAC_OS_BOUNDARIES.get(group, []) if sys.platform == "darwin" else []


def compare_result(name, old, new):
    for key in ("returncode", "stdout", "stderr"):
        left, right = getattr(old, key), getattr(new, key)
        if key == "stderr" and isinstance(left, bytes) and isinstance(right, bytes):
            left = exception_locations(left, right)
        if left != right:
            raise Mismatch(f"S2L_{key.upper()}: {name}: old={left!r}, new={right!r}")


# 旧スクリプト自身のフレームの File 欄は、Rust が固定名を出す(2026-11-11 17:58・21:35 の決定)。
LEGACY_NAME = "loop-state.py"
# 導入先のフレームは本家 CPython 3.10.11 の行・関数・ソースの行(22:26 の決定)。
# SHA-256 は CPython の v3.10.11 タグの Lib/ から取った値(手元の Python から取らない)。
STDLIB_SHA256 = {
    "posixpath.py": "2c770bac0680a5a180b1310102d14e1e120f2f2e8e47925b1574ec808cb8ad6d",
    "json/__init__.py": "d5d41e2c29049515d295d81a6d40b4890fbec8d8482cfb401630f8ef2f77e4d5",
    "json/decoder.py": "9f02654649816145bc76f8c210a5fe3ba1de142d4d97a1c93105732e747c285b",
    "json/encoder.py": "06b881b824f71e95d72af4ab865de4c35553e791b6d959a125caac61401cc350",
    "re.py": "018b1a2c70e8ef537cc328d1026afdeca5839392ce47235a7b8d22e1b788cdc7",
    "sre_compile.py": "800f4c7df096e10336cd54f1aa6382721d6fcb974669463fe67f7a9189e52407",
    "sre_parse.py": "dad10892e970f3b9d0ad874d893edde31c0303e39ea5e2678935129e29128c2f",
    "types.py": "5e6c3b3380332249908d7907bc4c1102c0bf14076248c8244d03314f6b2e2078",
    "encodings/utf_8_sig.py": "1ef3da8d8aa08149e7f274dc64dbfce2155da812e5258ca8e8f832428d3b5c2d",
    "subprocess.py": "53bb0d0780e166ef4ae94f3b2a817a8aae49a84d899db1f4c31f933d429057d6",
    "tempfile.py": "50065da96ff7437151098b576de9d00bfb86496fb8c411d8bb7c99d014797672",
    # SIGINT の途中のフレームだけに出る(比べない)。行の照合の自己検査に使う。
    "selectors.py": "b16e673c110fd6a362414308ef73ffc3c15125c87361333b309791735931040d",
}
# 対応づけてよい導入先のフレーム(本家 3.10.11 の行番号だけ)。各行の位置・関数は固定し、理由や値は置換しない。
EXCEPTION_LOCATIONS = (("json/__init__.py", 346, "loads"),
                       ("re.py", 190, "match"),
                       ("re.py", 290, "_compile"),
                       ("re.py", 291, "_compile"),
                       ("re.py", 301, "_compile"),
                       ("re.py", 303, "_compile"),
                       ("re.py", 273, "escape"),
                       ("re.py", 274, "escape"),
                       ("types.py", 183, "__get__"),
                       ("sre_compile.py", 620, "isstring"),
                       ("sre_compile.py", 788, "compile"),
                       ("sre_parse.py", 226, "__init__"),
                       ("sre_parse.py", 233, "__init__"),
                       ("sre_parse.py", 241, "__next"),
                       ("sre_parse.py", 257, "get"),
                       ("sre_parse.py", 947, "parse"),
                       ("sre_parse.py", 950, "parse"),
                       ("sre_parse.py", 955, "parse"),
                       ("sre_parse.py", 444, "_parse_sub"),
                       ("sre_parse.py", 496, "_parse"),
                       ("sre_parse.py", 512, "_parse"),
                       ("sre_parse.py", 841, "_parse"),
                       ("json/__init__.py", 341, "loads"),
                       ("json/decoder.py", 340, "decode"),
                       ("json/decoder.py", 355, "raw_decode"),
                       ("encodings/utf_8_sig.py", 23, "decode"),
                       ("json/decoder.py", 337, "decode"),
                       ("json/decoder.py", 353, "raw_decode"),
                       ("json/decoder.py", 32, "__init__"),
                       ("json/__init__.py", 179, "dump"),
                       ("json/__init__.py", 180, "dump"),
                       ("json/encoder.py", 431, "_iterencode"),
                       ("json/encoder.py", 429, "_iterencode"),
                       ("json/encoder.py", 405, "_iterencode_dict"),
                       ("json/encoder.py", 353, "_iterencode_dict"),
                       ("json/encoder.py", 325, "_iterencode_list"),
                       # posixpath.realpath 系(realpath.rs が出す呼出しの位置)。
                       ("posixpath.py", 42, "_get_sep"),
                       ("posixpath.py", 62, "isabs"),
                       ("posixpath.py", 63, "isabs"),
                       ("posixpath.py", 64, "isabs"),
                       ("posixpath.py", 76, "join"),
                       ("posixpath.py", 77, "join"),
                       ("posixpath.py", 82, "join"),
                       ("posixpath.py", 83, "join"),
                       ("posixpath.py", 85, "join"),
                       ("posixpath.py", 103, "split"),
                       ("posixpath.py", 104, "split"),
                       ("posixpath.py", 105, "split"),
                       ("posixpath.py", 107, "split"),
                       ("posixpath.py", 108, "split"),
                       ("posixpath.py", 340, "normpath"),
                       ("posixpath.py", 341, "normpath"),
                       ("posixpath.py", 379, "abspath"),
                       ("posixpath.py", 380, "abspath"),
                       ("posixpath.py", 381, "abspath"),
                       ("posixpath.py", 384, "abspath"),
                       ("posixpath.py", 385, "abspath"),
                       ("posixpath.py", 386, "abspath"),
                       ("posixpath.py", 395, "realpath"),
                       ("posixpath.py", 396, "realpath"),
                       ("posixpath.py", 397, "realpath"),
                       ("posixpath.py", 402, "_joinrealpath"),
                       ("posixpath.py", 411, "_joinrealpath"),
                       ("posixpath.py", 416, "_joinrealpath"),
                       ("posixpath.py", 417, "_joinrealpath"),
                       ("posixpath.py", 420, "_joinrealpath"),
                       ("posixpath.py", 423, "_joinrealpath"),
                       ("posixpath.py", 424, "_joinrealpath"),
                       ("posixpath.py", 425, "_joinrealpath"),
                       ("posixpath.py", 429, "_joinrealpath"),
                       ("posixpath.py", 431, "_joinrealpath"),
                       ("posixpath.py", 437, "_joinrealpath"),
                       ("posixpath.py", 454, "_joinrealpath"),
                       ("posixpath.py", 456, "_joinrealpath"),
                       ("posixpath.py", 458, "_joinrealpath"),
                       # index_snapshot の subprocess.run(timeout) の TimeoutExpired。
                       ("subprocess.py", 505, "run"),
                       ("subprocess.py", 1154, "communicate"),
                       ("subprocess.py", 1209, "wait"),
                       ("subprocess.py", 1951, "_wait"),
                       ("subprocess.py", 2047, "_communicate"),
                       # onbranch の tempfile.mkdtemp。
                       ("tempfile.py", 126, "_sanitize_params"),
                       ("tempfile.py", 223, "_get_default_tempdir"),
                       ("tempfile.py", 292, "_gettempdir"),
                       ("tempfile.py", 299, "gettempdir"),
                       ("tempfile.py", 357, "mkdtemp"),
                       ("tempfile.py", 368, "mkdtemp"),
                       ("tempfile.py", 381, "mkdtemp"))
_FRAME = re.compile(rb'  File "(?P<path>[^"\n]*)", line (?P<line>[0-9]+), in (?P<function>[^\n]+)\n')
_STDLIB_CHECKED = {}


def stdlib_root():
    return Path(sysconfig.get_path("stdlib"))


def stdlib_matches(module):
    """比べる旧Pythonの導入先のファイルが、本家 3.10.11 と同じ中身か。"""
    if module not in _STDLIB_CHECKED:
        try:
            actual = hashlib.sha256((stdlib_root() / module).read_bytes()).hexdigest()
        except OSError:
            actual = None
        _STDLIB_CHECKED[module] = actual == STDLIB_SHA256[module]
    return _STDLIB_CHECKED[module]


def canonical_frame(line):
    """旧版のフレームの行を、固定名を使う行へ。対応づけられない行は None。"""
    match = _FRAME.fullmatch(line)
    if match is None:
        return None
    path = match["path"]
    tail = b'", line ' + match["line"] + b", in " + match["function"] + b"\n"
    if path == os.fsencode(LEGACY):
        return b'  File "' + LEGACY_NAME.encode("ascii") + tail
    root = os.fsencode(stdlib_root()) + b"/"
    if not path.startswith(root):
        return None
    module = os.fsdecode(path[len(root):])
    key = (module, int(match["line"]), os.fsdecode(match["function"]))
    if module not in STDLIB_SHA256 or key not in EXCEPTION_LOCATIONS:
        return None
    if not stdlib_matches(module):
        raise Mismatch(f"S2L_STDLIB_SHA256: {module} は本家 CPython 3.10.11 と違う")
    return b'  File "<CPython3.10>/' + module.encode("ascii") + tail


def traceback_frames(lines):
    """実際の Traceback の塊の中にある File の行の番号。診断の本文の擬似的な行は含めない。"""
    frames = set()
    inside = False
    header_allowed = True
    actual_trace = False
    for index, line in enumerate(lines):
        if header_allowed and line == b"Traceback (most recent call last):\n":
            inside = True
            actual_trace = True
            header_allowed = False
        elif inside:
            if line.startswith(b'  File "'):
                frames.add(index)
            elif not (line.startswith(b"    ") or line.startswith(b"  [Previous line repeated ")):
                inside = False
        elif line in (b"During handling of the above exception, another exception occurred:\n",
                      b"The above exception was the direct cause of the following exception:\n"):
            # 先頭に実Tracebackが無い通常診断の本文から、因果連鎖を開始しない。
            header_allowed = actual_trace
        elif line.strip():
            header_allowed = False
    return frames


def exception_locations(raw, expected=None):
    """旧版の stderr のフレームの行のパスの欄だけを、Rust の同じ位置の行が持つ固定名へ対応づける。

    expected(Rust の stderr)の同じ行番号に、その固定名の行がある場合に限る。
    expected を省くと、対応表にある行をすべて固定名へ写した結果を返す(自己検査用)。
    """
    lines = raw.splitlines(keepends=True)
    others = None if expected is None else expected.splitlines(keepends=True)
    result = []
    frames = traceback_frames(lines)
    for index, line in enumerate(lines):
        if index in frames:
            mapped = canonical_frame(line)
            if mapped is not None and (others is None or (index < len(others) and others[index] == mapped)):
                line = mapped
        result.append(line)
    return b"".join(result)


class Comparison:
    def __init__(self, fixture):
        self.fixture = str(fixture)
        self.count = 0
        self.commands = {}

    def run(self, command, *args, **kwargs):
        if (not isinstance(command, (list, tuple)) or len(command) < 2
                or Path(str(command[1])).resolve() != LEGACY):
            return _RUN(command, *args, **kwargs)
        self.count += 1
        target = list(command[2:])
        self.commands[target[0]] = self.commands.get(target[0], 0) + 1
        output = None
        if "--out" in target:
            output = Path(target[target.index("--out") + 1])
        initial = output_state(output)
        # 旧テストがHOMEを一時ディレクトリへ移しても、比較基準のPyYAMLを変えない。
        supplied=kwargs.get("env")
        child_env=child_environment(os.environ.copy() if supplied is None else dict(supplied),keep_case=False)
        kwargs["env"]=child_env
        # 旧版は比較器を動かした Python(sys.executable)で、元のファイル名のまま動かす。
        legacy_command=[sys.executable,*command[1:]]
        if Path(str(command[1])).name != LEGACY_NAME:
            raise Mismatch("S2L_LEGACY_NAME: 旧スクリプトの名前が固定名と違う")
        start_case(" ".join(map(str, target)))
        old = _RUN(legacy_command, *args, **kwargs)
        record("old", old)
        state = output_state(output)
        restore_output(output, initial)
        new = _RUN([self.fixture, *target], *args, **kwargs)
        remember(" ".join(map(str, target)), old, new)
        compare_result(" ".join(map(str, target)), old, new)
        actual = output_state(output)
        if state != actual:
            if state is not None and actual is None:
                raise Mismatch("S2L_STATE_MISSING: Rust did not create its state output")
            raise Mismatch("S2L_STATE_BYTES: output bytes, kind or mode differ")
        return new


def output_state(path):
    if path is None or not path.exists() and not path.is_symlink():
        return None
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return ("link", mode, os.readlink(path))
    if stat.S_ISREG(metadata.st_mode):
        return ("file", mode, path.read_bytes())
    raise RuntimeError("比較器のoutは通常ファイルまたはリンクでなければなりません")


def restore_output(path, initial):
    if path is None:
        return
    if path.exists() or path.is_symlink():
        path.unlink()
    if initial is None:
        return
    kind, mode, content = initial
    if kind == "link":
        path.symlink_to(content)
    else:
        path.write_bytes(content)
        path.chmod(mode)


# 22:31 の決定が挙げた、効果を再現しない起動の変数(PYTHONPATH は PyYAML の置き場だけを比較器が渡す)。
REMOVED_2231 = ("PYTHONPATH", "PYTHONHOME", "PYTHONPROFILEIMPORTTIME", "PYTHONVERBOSE", "PYTHONINSPECT",
                "PYTHONWARNINGS", "PYTHONMALLOC", "PYTHONDEVMODE", "PYTHONFAULTHANDLER", "PYTHONTRACEMALLOC",
                "PYTHONUSERBASE", "PYTHONNOUSERSITE")
# 00:28 の決定(R4E #482 の Q5 の A)の、Python 本体の設定・計測・診断の変数。一覧に無い PYTHON* も外す。
REMOVED_0028 = ("PYTHONMALLOCSTATS", "PYTHONPLATLIBDIR", "PYTHONWARNDEFAULTENCODING", "PYTHONPYCACHEPREFIX",
                "PYTHONEXECUTABLE", "PYTHONDONTWRITEBYTECODE", "PYTHONSTARTUP", "PYTHONBREAKPOINT",
                "PYTHONASYNCIODEBUG", "PYTHONCASEOK", "PYTHONTHREADDEBUG", "PYTHONDUMPREFS")
# case が与えてよい変数。旧版どおり再現する(効果か起動の失敗を比べる)。
CASE_GIVEN = ("PYTHONUNBUFFERED", "PYTHONINTMAXSTRDIGITS", "PYTHONIOENCODING", "PYTHONUTF8",
              "PYTHONCOERCECLOCALE", "PYTHONHASHSEED", "PYTHONOPTIMIZE")


def yaml_root():
    import yaml
    return Path(yaml.__file__).resolve().parent.parent


def _key(name, env):
    return name.encode("ascii") if any(isinstance(k, bytes) for k in env) else name


def child_environment(env, keep_case=True, given=None):
    """旧新の子の環境の土台。外す変数を除き、PyYAML の置き場の PYTHONPATH を比較器が与える。

    keep_case が偽なら、受け継いだ case の変数も外す(case が何も与えない起動)。
    """
    byte_env = any(isinstance(k, bytes) for k in env)
    def keep(key):
        name = os.fsdecode(key)
        return not name.startswith("PYTHON") or (keep_case and name in CASE_GIVEN)
    result = {k: v for k, v in env.items() if keep(k)}
    root = yaml_root()
    result[_key("PYTHONPATH", env)] = os.fsencode(root) if byte_env else os.fsdecode(root)
    for name, value in (given or {}).items():
        key = _key(name, env)
        if value is None:
            result.pop(key, None)
        else:
            result[key] = os.fsencode(value) if byte_env else os.fsdecode(value)
    return verify_child_environment(result, given or {})


def verify_child_environment(env, given):
    """外す変数が残れば失敗、case が与えた変数が消えれば失敗(比較器自身の検査)。"""
    names = {os.fsdecode(k): v for k, v in env.items()}
    for name, value in names.items():
        if not name.startswith("PYTHON"):
            continue
        if name == "PYTHONPATH":
            if os.fsdecode(value) != os.fsdecode(yaml_root()):
                raise Mismatch("S2L_ENV_REMOVED_LEFT: PYTHONPATH はPyYAMLの置き場だけ")
        elif name not in CASE_GIVEN:
            raise Mismatch("S2L_ENV_REMOVED_LEFT: " + name)
    for name, value in given.items():
        if value is None:
            if name in names:
                raise Mismatch("S2L_ENV_CASE_UNSET_LEFT: " + name)
        elif os.fsencode(names.get(name, b"\0")) != os.fsencode(value):
            raise Mismatch("S2L_ENV_CASE_LOST: " + name)
    return env


def interpreter_record():
    """比較の前提(sitecustomize 等が無いこと・受け継いだ資源の上限)を確かめて記録する。"""
    import gettext, importlib.util as util, resource, site
    record = {"executable": sys.executable, "version": sys.version.split()[0]}
    for module in ("sitecustomize", "usercustomize"):
        if util.find_spec(module) is not None:
            raise RuntimeError(module + " があると比較の前提が成り立ちません")
        record[module] = "absent"
    # 旧版は比較器と同じ Python で動く。利用者の site が無効なら .pth は読まれない。
    user_site = Path(site.getusersitepackages())
    pth = sorted(str(p) for p in user_site.glob("*.pth")) if user_site.is_dir() else []
    if site.ENABLE_USER_SITE and pth:
        raise RuntimeError("利用者の site の .pth があると比較の前提が成り立ちません: " + ", ".join(pth))
    record["user_site_enabled"] = bool(site.ENABLE_USER_SITE)
    record["user_site_pth_loaded"] = 0
    if gettext.find("argparse") is not None:
        raise RuntimeError("argparse の翻訳ファイルがあると比較の前提が成り立ちません")
    record["argparse_translation"] = "absent"
    limits = {}
    for name in ("RLIMIT_NOFILE", "RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_STACK", "RLIMIT_FSIZE", "RLIMIT_DATA"):
        soft, hard = resource.getrlimit(getattr(resource, name))
        limits[name] = ["unlimited" if v == resource.RLIM_INFINITY else v for v in (soft, hard)]
    # 比較器は資源の上限を設定しない(00:42 の決定)。受け継いだ値だけを記録する。
    record["inherited_limits"] = limits
    record["stdlib_sha256_matches"] = {m: stdlib_matches(m) for m in sorted(STDLIB_SHA256)}
    return record


def environment():
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise RuntimeError("Python 3.10系とroot以外の利用者が必要です")
    if LEGACY.name != LEGACY_NAME:
        raise RuntimeError("旧スクリプトの名前が固定名と違います")
    import yaml
    if yaml.__version__ != "6.0.3":
        raise RuntimeError("PyYAML 6.0.3が必要です")
    if hashlib.sha256(LEGACY.read_bytes()).hexdigest() != LEGACY_SHA256:
        raise RuntimeError("比較元の旧Pythonが基準から変わっています")


def legacy_cases(names=None):
    spec = importlib.util.spec_from_file_location("s2l_legacy_tests", ROOT / "scripts/test_loop_state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if names:
        suite = unittest.TestSuite(module.LoopStateTest(name) for name in names)
    else:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.LoopStateTest)
    return suite


COMMANDS={"snapshot","stop-control","preflight","derive-removed-worktree","state-digest","state-digests","compare"}
LEGACY_INVOCATIONS={"snapshot":118,"compare":38,"derive-removed-worktree":2,"preflight":2,"state-digest":9,"state-digests":12,"stop-control":2}
def decision_groups():
    """全コマンド共通の決定と r3 の必須 case の件数(Linux)。Mac は Linux だけの到達を数えない。"""
    groups={"startup_directory":49,"hashseed":17,"variables_baseline":5,"pythonhashseed":45,"pythonoptimize":25,
            "sigint":5,"sigterm":5,"sigint_ignored":1,"timeouts":2,"timeouts_10s":2,"write_failure":10,"onbranch":4,
            "r3_tree":4,"r3_realpath":17,"r4_config_snapshot":9,"r4_config_prior":10,"r4_preflight_json":5,
            "r4_realpath":4,"r5_saved_paths":9,"r5_same_bytes":14,"r6_os_path":27,"r6_strings":15,"r7_held_errors":14,"r7_exception_audit":6,"r7_path_equality":25,"git_only":2}
    if sys.platform=="darwin":
        # Mac の旧版は、/proc/self/fd/<番号> を渡す git config --file の解析の失敗で snapshot・preflight を拒否する。
        # 前の状態の無い snapshot は、その前に worktree の一覧・停止ファイルの確かめと、名前の順で最初の worktree の
        # rev-parse・secret_paths の読取り・diff-index・config の出典の一覧を行う。
        # 前の状態を使う snapshot と preflight は、保存した config の確かめの中で拒否する。
        # 待ちの点・onbranch・前の状態を使う case へ届かない case は数えない(到達しない成功にしない)。
        for key in ("sigint","sigterm","sigint_ignored","onbranch","timeouts","timeouts_10s","r4_config_prior",
                    "r5_saved_paths","r5_same_bytes","r7_held_errors","r7_exception_audit"):groups.pop(key)
        # Mac は前の状態を取れないので、gitdir の preflight・保存パス・HEAD・buffering の case を数えない。
        # r7 の比較(前の状態・権限)は前の状態を取れないので、初回の snapshot の 5 件だけを数える。
        groups.update(git_only=1,r6_os_path=8,r6_strings=6,r7_path_equality=5)
        # Mac の APFS は UTF-8 でない名前を作れない。ASCII の対照の 2 件だけを数える(MAC_OS_BOUNDARIES)。
        groups.update(r3_tree=2)
    return groups


def contract_groups():
    """契約の比較の群ごとの件数。Mac は /dev/full と、APFS が作れない UTF-8 でない名前の case を数えない。"""
    mac=sys.platform=="darwin"
    return {"json19":19,"structure":13,"paths":16,"io":32 if mac else 51,"numeric23":23,"prior_structure78":78,
            "repository2":1 if mac else 2,"r1_compare":24,"r1_json":110 if mac else 165,"r1_walk":17,
            "r1_state_stop":28 if mac else 42,"r2_compare":92 if mac else 138,"r2_permissions":2 if mac else 4,"r2_refs":18}


def require_full_coverage(report):
    expected_groups=contract_groups()
    if report.get("partial"):
        raise Mismatch("S2L_PARTIAL_CANNOT_PASS_FULL_GATE")
    if report.get("arguments",{}).get("arguments")!=404 or report.get("terminals",{}).get("pty")!=40:
        raise Mismatch("S2L_ARGUMENT_OR_PTY_COVERAGE")
    contract=report.get("contracts",{})
    if contract.get("groups")!=expected_groups or contract.get("contract_cases")!=sum(expected_groups.values()):
        raise Mismatch("S2L_CONTRACT_COVERAGE")
    encoding=report.get("encoding",{})
    if (encoding.get("cases")!=35 or encoding.get("legacy_expected")!=35 or encoding.get("rust_expected")!=35
            or set(encoding.get("commands",[]))!=COMMANDS or encoding.get("timeouts")!=0
            or not all(encoding.get(k) for k in ("inputs_unchanged","all_groups_absent","git_raw_env_preserved"))):
        raise Mismatch("S2L_ENCODING_OR_COMMAND_COVERAGE")
    yaml=report.get("yaml",{})
    if yaml.get("yaml_cli")!=80 or yaml.get("top")!=40 or yaml.get("linked")!=40:
        raise Mismatch("S2L_YAML_COVERAGE")
    matrix=report.get("encoding_matrix",{})
    if (matrix.get("encoding_matrix")!=126 or matrix.get("settings")!=18 or matrix.get("regular_file")!=8
            or matrix.get("unknown_handler_write")!=1 or set(matrix.get("commands",[]))!=COMMANDS):
        raise Mismatch("S2L_ENCODING_MATRIX_COVERAGE")
    decisions=report.get("decisions",{})
    if decisions.get("groups")!=decision_groups() or decisions.get("decision_cases")!=sum(decision_groups().values()):
        raise Mismatch("S2L_DECISION_COVERAGE")
    for group in ("arguments","terminals","contracts","encoding","yaml","encoding_matrix","decisions"):
        if report[group].get("skipped")!=0:raise Mismatch("S2L_SKIPPED: "+group)
        if report[group].get("timeouts",0)!=0:raise Mismatch("S2L_TIMEOUT: "+group)
        # ファイル系が拒んで飛ばした入力は、OS ごとに決めた一覧とちょうど同じでなければならない。
        if [(row.get("name"),row.get("action")) for row in report[group].get("os_boundaries",[])]!=expected_os_boundaries(group):
            raise Mismatch("S2L_OS_BOUNDARY: "+group)
    if sys.platform!="darwin" and (report.get("legacy_methods")!=57 or report.get("legacy_comparisons")!=183
                                    or report.get("legacy_commands")!=LEGACY_INVOCATIONS):
        raise Mismatch("S2L_LEGACY_COVERAGE")
    return True

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture",type=Path,required=True)
    parser.add_argument("--case",action="append",help="Linux旧クラスの明示した部分検査。CI全件合格には使わない")
    args=parser.parse_args();start=time.monotonic()
    try:
        environment()
        fixture=args.fixture.resolve()
        if not fixture.is_file() or not os.access(fixture,os.X_OK):raise RuntimeError("S2Lの今回ビルドしたexampleが必要です")
        before=hashlib.sha256(fixture.read_bytes()).hexdigest();comparison=Comparison(fixture)
        report={"binary_sha256":before,"os":platform.system(),"partial":bool(args.case),"legacy_methods":0,"legacy_comparisons":0,"interpreter":interpreter_record()}
        if sys.platform!="darwin":
            suite=legacy_cases(args.case)
            with mock.patch.object(subprocess,"run",comparison.run):result=unittest.TextTestRunner(verbosity=2).run(suite)
            if not result.wasSuccessful() or result.skipped or result.expectedFailures or result.unexpectedSuccesses or not result.testsRun:raise Mismatch("S2L_LEGACY_RESULT")
            report.update(legacy_methods=result.testsRun,legacy_comparisons=comparison.count,legacy_commands=comparison.commands)
        elif args.case:
            raise RuntimeError("--caseはLinux固定期待の旧クラスだけを選びます。Macの実拒否は全件比較で検査します")
        else:
            report["legacy_class_applicability"]="Linux固定期待57件はRustの22観測/共通8性質へ対応付ける。Macは全7CLIの実拒否/成功を個別期待で比較する"
        if not args.case:
            import loop_state_arguments,loop_state_contract_cases,loop_state_encoding,loop_state_yaml_cases,loop_state_encoding_matrix,loop_state_decision_cases
            # 群の準備(case の前)で止まったときも、前の群の case を出さないよう、群の名前を先に記録する。
            for group,call in [("arguments",loop_state_arguments.arguments),("terminals",loop_state_arguments.terminals),
                               ("contracts",loop_state_contract_cases.run),("encoding",loop_state_encoding.run),
                               ("yaml",loop_state_yaml_cases.run),("encoding_matrix",loop_state_encoding_matrix.run),
                               ("decisions",loop_state_decision_cases.run)]:
                start_case(group+" の準備(case の前)")
                report[group]=call(fixture)
            require_full_coverage(report)
        if hashlib.sha256(fixture.read_bytes()).hexdigest()!=before:raise Mismatch("S2L_BINARY_CHANGED_DURING_COMPARISON")
        report["seconds"]=round(time.monotonic()-start,3);print(json.dumps(report,ensure_ascii=False));return 0
    except (OSError,ValueError,RuntimeError,AssertionError,subprocess.TimeoutExpired) as exc:
        print(f"異常: {exc}",file=sys.stderr)
        context=last_case_text()
        if context:print(context,file=sys.stderr)
        return 1

if __name__=="__main__":raise SystemExit(main())
