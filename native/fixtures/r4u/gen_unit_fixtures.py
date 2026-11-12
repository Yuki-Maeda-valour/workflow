#!/usr/bin/env python3
"""R4U の文字列だけの部品と、呼出履歴の定型の固定期待を、本家 CPython 3.10.11 の実測で作る。製品の入口ではない。

- units.json: pathlib の整え方・parent・parents・連結・並べ方・除外、posixpath の join・split・normpath、
  UTF-8 の置換と改行の統一(実ファイルを read_text で読む)、repr、str.isprintable の表、呼出履歴の各フレーム
  (ソースの行と関数名を、本家 3.10.11 の標準ライブラリと旧版のソースから探す)。
- linux.json: glibc の strerror の文と、errno から選ぶ例外の種類と、旧版を一時フォルダで動かした呼出履歴
  (旧スクリプトと標準ライブラリのパスは固定名に、一時フォルダは @ROOT@ に置き換える)。
- 使い方: Linux・root でない利用者で、本家と同じ中身の 3.10.11 を使う。
  "$R4U_PYTHON" -B native/fixtures/r4u/gen_unit_fixtures.py [--out-dir <フォルダ>]
- 流し直しても同じバイト列になる(乱数は決まった種を使う)。
"""
import argparse
import ast
import errno
import hashlib
import json
import os
import platform
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LEGACY_REL = "plugins/dev-workflow/skills/update-doc/scripts/check_links.py"
LEGACY = ROOT / LEGACY_REL
SEED = 20261112
STDLIB_SHA256 = {
    "pathlib.py": "936f14a3ecc4ac5e896e4d673666d76b25a9d581f3b4c8d8d424e830fda2a69b",
    "posixpath.py": "2c770bac0680a5a180b1310102d14e1e120f2f2e8e47925b1574ec808cb8ad6d",
    "genericpath.py": "9d58ad64056a89f12258a62596c2073cd9e52b459dde152f710edc0cffcd865b",
}
STDLIB = Path(os.path.dirname(os.__file__))
EXCLUDE_DIRS = {".git", "node_modules", "dist", "build", ".next", "vendor", "__pycache__"}


def form(value):
    """bytes を固定期待の形にする(UTF-8 なら文字列、そうでなければ {"hex": ...})。"""
    if isinstance(value, str):
        value = os.fsencode(value)
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return {"hex": value.hex()}


def check_python():
    if sys.version_info[:3] != (3, 10, 11):
        raise SystemExit("本家と同じ中身の CPython 3.10.11 で流す")
    for name, digest in STDLIB_SHA256.items():
        if hashlib.sha256((STDLIB / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{name} が本家 3.10.11 と違う")
    if sys.platform != "linux" or os.geteuid() == 0:
        raise SystemExit("Linux の root でない利用者で流す")


# ---- pathlib・posixpath ----

PATH_INPUTS = [
    b"", b".", b"..", b"a", b"a/", b"/a", b"//a", b"///a", b"////a", b"a//b", b"a/./b", b"./a", b"a/..", b"../..",
    b"/", b"//", b"///", b"a/b/c", b"//a/b", b"/..", b"//..", b".hidden", b"a\nb", b"\xff/x", "日本/語".encode(),
    b"a/./../b", b"./", b"././.", b"a/b/", b"/a/./b/../c/", b"x.md/", b"...", b". /x", b"a/ /b", b"a/.git/b",
    b"node_modules", b"/build/x", b"dist", b"xbuild/a", b".next/a", b"vendor/b", b"__pycache__", b"a/.gitx",
    b"//build", b"a/Build", b"\x80\xff/\xe4\xb8", b"/a/b/c/d/e",
]
JOIN_OPERANDS = [b"x", b"x/y", b"/x", b"//x", b"///x", b"", b".", b"..", b"x/../y", b"./x/", b"a//b/", b"\xfe"]


def pure_path_cases():
    cases = []
    for raw in PATH_INPUTS:
        path = PurePosixPath(os.fsdecode(raw))
        cases.append({
            "input": form(raw),
            "str": form(str(path)),
            "parts": [form(part) for part in path.parts],
            "parent": form(str(path.parent)),
            "parents": [form(str(parent)) for parent in path.parents],
            "excluded": any(part in EXCLUDE_DIRS for part in path.parts),
            "join": [[form(other), form(str(path / os.fsdecode(other)))] for other in JOIN_OPERANDS],
            "child": form(str(Path(os.fsdecode(raw))._make_child_relpath("c.md"))),
        })
    return cases


SORT_POOL = [b"a", b"a-b", b"a.md", b"B", b"b", b"_", "日本".encode(), chr(0xFF5A).encode(), chr(0x1F600).encode(),
             b"\x80", b"\xff", chr(0xE000).encode(), b"~", b"A", b"a b", b"-", b".md", b"\xe4\xb8", chr(0x4E00).encode(),
             chr(0xDF).encode(), b"\n", b"a\x80"]


def sort_cases():
    rng = random.Random(SEED)
    lists = [
        [b"t/a-b.md", b"t/a/b.md", b"t/a.md", b"t/B.md", b"t/a/A.md", "t/日本.md".encode(), b"t/\x80.md"],
        [b"/a", b"a", b"//a", b"/a/b", b"//a/b", b"a/b"],
    ]
    for _ in range(300):
        items = []
        for _ in range(rng.randint(2, 7)):
            parts = [rng.choice(SORT_POOL) for _ in range(rng.randint(1, 3))]
            items.append(b"/".join(parts))
        lists.append(items)
    cases = []
    for items in lists:
        paths = [PurePosixPath(os.fsdecode(item)) for item in items]
        cases.append({"input": [form(item) for item in items],
                      "sorted": [form(str(path)) for path in sorted(paths)]})
    return cases


def posixpath_cases():
    import posixpath
    joins = [[b"", b"a"], [b"a", b"b"], [b"a/", b"b"], [b"a", b"/b"], [b"/", b"a"], [b"", b""], [b"a", b""],
             [b"a", b"..", b".."], [b"", b"..", b".."], [b"//", b"a"], [b"a//", b"b"], [b"..", b"..", b".."],
             [b"\xff", b"\xfe"], [b"/a", b"b", b"/c", b"d"]]
    splits = [b"", b"a", b"/", b"//", b"///", b"a/b", b"a/b/", b"/a", b"//a", b"a//b", b"a///", b"..", b"a/..",
              b"/a/b/..", b"\xff/\xfe", b"///a//b"]
    norms = [b"", b".", b"..", b"/", b"//", b"///", b"////a", b"a/..", b"a/../..", b"/..", b"//..", b"/../a",
             b"../a/..", b"a//b/./c/", b"//a/../b", b"///a/../b", b"a/b/../../..", b"./.", b"..//..", b"\xff/./x",
             b"/a/./b/../../../c", b"x/../..", b"../../x/..", b"a/../../b"]
    return {
        "join": [{"input": [form(p) for p in parts],
                  "result": form(posixpath.join(*[os.fsdecode(p) for p in parts]))} for parts in joins],
        "split": [{"input": form(p), "result": [form(x) for x in posixpath.split(os.fsdecode(p))]} for p in splits],
        "normpath": [{"input": form(p), "result": form(posixpath.normpath(os.fsdecode(p)))} for p in norms],
        "isabs": [{"input": form(p), "result": posixpath.isabs(os.fsdecode(p))} for p in splits],
    }


# ---- 読取り・repr・isprintable ----

def decode_inputs():
    inputs = []
    for first in range(256):
        inputs.append(bytes([first]) + b"a")
    seconds = [0x00, 0x7F, 0x80, 0x8F, 0x90, 0x9F, 0xA0, 0xBF, 0xC0, 0xFF]
    for lead in range(0xC0, 0x100):
        for second in seconds:
            inputs.append(bytes([lead, second]) + b"a")
            inputs.append(bytes([lead, second, 0x80]) + b"a")
            inputs.append(bytes([lead, second, 0x80, 0x80]))
    inputs += [b"\r", b"\n", b"\r\n", b"\r\r\n", b"\n\r", b"a\rb\r\nc\nd\r", b"\r\n\r\n", b"\xef\xbb\xbfa",
               b"\xef\xbb\xbf", b"\xe3\x81\r\n", b"\xed\xa0\x80\xed\xb0\x80", b"\xf4\x8f\xbf\xbf", b"\xf4\x90\x80\x80"]
    rng = random.Random(SEED + 1)
    alphabet = list(range(0x20, 0x80, 7)) + list(range(0x80, 0x100)) + [0x0D, 0x0A, 0x0D]
    for _ in range(1500):
        inputs.append(bytes(rng.choice(alphabet) for _ in range(rng.randint(1, 12))))
    return inputs


def decode_cases(temp):
    path = Path(temp) / "decode.md"
    cases = []
    for data in decode_inputs():
        path.write_bytes(data)
        cases.append({"input": data.hex(), "text": path.read_text(encoding="utf-8", errors="replace")})
    return cases


def repr_cases():
    inputs = [b"a", b"'", b'"', b"'\"", b"\\", b"\n\r\t", b"\x00\x1f\x7f", chr(0xA0).encode() + chr(0xAD).encode(),
              b"\x80\xff", "日本".encode(), chr(0x1F600).encode(), chr(0x378).encode(), chr(0x200B).encode(),
              chr(0xE0001).encode(), b"a\xed\xa0\x80", b"", b"'a'", b"\"a\"", b"a'b\"c", chr(0x3000).encode(),
              chr(0x85).encode(), chr(0x2028).encode(), b"/tmp/x/y", chr(0xFFFD).encode()]
    rng = random.Random(SEED + 2)
    pool = [b"a", b"'", b'"', b"\\", b"\n", b"\x01", b"\x7f", b"\x80", b"\xff", "日".encode(), chr(0xA0).encode(),
            chr(0x1F600).encode(), chr(0x378).encode(), b"\xed\xa0\x80", b" ", b"/"]
    for _ in range(400):
        inputs.append(b"".join(rng.choice(pool) for _ in range(rng.randint(1, 6))))
    return [{"input": form(data), "repr": repr(os.fsdecode(data))} for data in inputs]


def ranges(predicate):
    result = []
    start = None
    for code in range(0x110000):
        hit = predicate(chr(code))
        if hit and start is None:
            start = code
        elif not hit and start is not None:
            result.append([start, code - 1])
            start = None
    if start is not None:
        result.append([start, 0x10FFFF])
    return result


# ---- 呼出履歴のフレーム ----

# (固定名, 関数の修飾名, ソースの行に含む文字列, 何番目か)
FRAMES = {
    "legacy.module": ("check_links.py", "<module>", "sys.exit(main(sys.argv[1:]))", 0),
    "legacy.main_iter": ("check_links.py", "main", "for md in iter_md_files(argv):", 0),
    "legacy.main_doc": ("check_links.py", "main", "print(__doc__)", 0),
    "legacy.main_read": ("check_links.py", "main", "md.read_text(", 0),
    "legacy.main_link_resolve": ("check_links.py", "main", "resolved = (base / target).resolve()", 0),
    "legacy.main_link_exists": ("check_links.py", "main", "if not resolved.exists():", 0),
    "legacy.main_code_resolve": ("check_links.py", "main", "resolved = (base / target).resolve()", 1),
    "legacy.main_code_alt": ("check_links.py", "main", "if (up / target).exists():", 0),
    "legacy.main_code_exists": ("check_links.py", "main", "if not resolved.exists() and alt is None:", 0),
    "legacy.main_print_count": ("check_links.py", "main", "print(f\"検査ファイル数", 0),
    "legacy.main_print_warn": ("check_links.py", "main", "print(f\"WARN  {w}\")", 0),
    "legacy.main_print_error": ("check_links.py", "main", "print(f\"ERROR {e}\")", 0),
    "legacy.main_print_result": ("check_links.py", "main", "print(f\"結果", 0),
    "legacy.iter_is_dir": ("check_links.py", "iter_md_files", "if p.is_dir():", 0),
    "legacy.iter_rglob": ("check_links.py", "iter_md_files", "p.rglob(", 0),
    "legacy.iter_is_file": ("check_links.py", "iter_md_files", "elif p.is_file():", 0),
    "legacy.iter_warn": ("check_links.py", "iter_md_files", "print(f\"WARN: ", 0),
    "pathlib.is_dir": ("pathlib.py", "Path.is_dir", "self.stat()", 0),
    "pathlib.is_file": ("pathlib.py", "Path.is_file", "self.stat()", 0),
    "pathlib.exists": ("pathlib.py", "Path.exists", "self.stat()", 0),
    "pathlib.stat": ("pathlib.py", "Path.stat", "self._accessor.stat(", 0),
    "pathlib.read_text_open": ("pathlib.py", "Path.read_text", "self.open(", 0),
    "pathlib.read_text_read": ("pathlib.py", "Path.read_text", "f.read()", 0),
    "pathlib.open": ("pathlib.py", "Path.open", "self._accessor.open(", 0),
    "pathlib.resolve_realpath": ("pathlib.py", "Path.resolve", "self._accessor.realpath(", 0),
    "pathlib.resolve_stat": ("pathlib.py", "Path.resolve", "p.stat()", 0),
    "pathlib.resolve_check_eloop": ("pathlib.py", "Path.resolve", "check_eloop(e)", 1),
    "pathlib.check_eloop": ("pathlib.py", "Path.resolve.check_eloop", "raise RuntimeError(", 0),
    "pathlib.rglob": ("pathlib.py", "Path.rglob", "selector.select_from(self)", 0),
    "pathlib.select_from_is_dir": ("pathlib.py", "_Selector.select_from", "is_dir(parent_path)", 0),
    "pathlib.recursive_iterate": ("pathlib.py", "_RecursiveWildcardSelector._select_from",
                                  "self._iterate_directories(", 0),
    "pathlib.recursive_successor": ("pathlib.py", "_RecursiveWildcardSelector._select_from",
                                    "successor_select(starting_point", 0),
    "pathlib.iterate_scandir": ("pathlib.py", "_RecursiveWildcardSelector._iterate_directories",
                                "scandir(parent_path)", 0),
    "pathlib.iterate_entry_is_dir": ("pathlib.py", "_RecursiveWildcardSelector._iterate_directories",
                                     "entry.is_dir()", 0),
    "pathlib.iterate_entry_is_symlink": ("pathlib.py", "_RecursiveWildcardSelector._iterate_directories",
                                         "entry.is_symlink()", 0),
    "pathlib.iterate_recurse": ("pathlib.py", "_RecursiveWildcardSelector._iterate_directories",
                                "self._iterate_directories(path", 0),
    "pathlib.wildcard_scandir": ("pathlib.py", "_WildcardSelector._select_from", "scandir(parent_path)", 0),
    "pathlib.fspath": ("pathlib.py", "PurePath.__fspath__", "str(self)", 0),
    "posixpath.realpath_join": ("posixpath.py", "realpath", "_joinrealpath(", 0),
    "posixpath.realpath_abspath": ("posixpath.py", "realpath", "abspath(path)", 0),
    "posixpath.abspath_getcwd": ("posixpath.py", "abspath", "os.getcwd()", 0),
    "posixpath.joinrealpath_isabs": ("posixpath.py", "_joinrealpath", "if isabs(rest):", 0),
    "posixpath.joinrealpath_lstat": ("posixpath.py", "_joinrealpath", "os.lstat(newpath)", 0),
    "posixpath.joinrealpath_recurse": ("posixpath.py", "_joinrealpath", "os.readlink(newpath)", 0),
    "posixpath.isabs_sep": ("posixpath.py", "isabs", "_get_sep(s)", 0),
    "posixpath.get_sep": ("posixpath.py", "_get_sep", "isinstance(path, bytes)", 0),
}


def function_ranges(source):
    """修飾名 → (始まりの行, 終わりの行)。<module> はファイル全体。"""
    tree = ast.parse(source)
    result = {"<module>": (1, len(source.splitlines()))}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if isinstance(child, ast.FunctionDef):
                    result[name] = (child.lineno, child.end_lineno)
                walk(child, name + ".")
    walk(tree, "")
    return result


def innermost(ranges_, line):
    best = None
    for name, (start, end) in ranges_.items():
        if start <= line <= end and (best is None or end - start < ranges_[best][1] - ranges_[best][0]):
            best = name
    return best


def frame_cases():
    sources = {"check_links.py": LEGACY.read_text(encoding="utf-8"),
               "pathlib.py": (STDLIB / "pathlib.py").read_text(encoding="utf-8"),
               "posixpath.py": (STDLIB / "posixpath.py").read_text(encoding="utf-8")}
    result = []
    for key, (file, qualname, needle, nth) in FRAMES.items():
        source = sources[file]
        spans = function_ranges(source)
        start, end = spans[qualname]
        lines = source.splitlines()
        hits = [number for number in range(start, end + 1)
                if needle in lines[number - 1] and innermost(spans, number) == qualname]
        if len(hits) <= nth:
            raise SystemExit(f"{key}: ソースの行が見つからない")
        line = hits[nth]
        text = lines[line - 1].lstrip(" \t\f")
        result.append({"key": key, "file": file, "line": line, "name": qualname.rsplit(".", 1)[-1], "source": text})
    return result


# ---- Linux の表 ----

def oserror_tables():
    strerror = [[number, os.strerror(number)] for number in range(0, 201)]
    types = [[number, type(OSError(number, "x")).__name__] for number in range(0, 201)]
    names = sorted([name, type(OSError(number, "x")).__name__] for number, name in errno.errorcode.items())
    return strerror, types, names


# ---- 旧版の呼出履歴の実測 ----

def scrub(stderr, root):
    """フレームの行のパスを固定名にし、一時フォルダを @ROOT@ にする。対象外の File 欄は止める。"""
    text = stderr.decode("utf-8")
    out = []
    for line in text.split("\n"):
        if line.startswith('  File "'):
            path, rest = line[len('  File "'):].split('", line ', 1)
            if path == str(LEGACY):
                path = "check_links.py"
            elif os.path.dirname(path) == str(STDLIB) and os.path.basename(path) in STDLIB_SHA256:
                path = os.path.basename(path)
            else:
                raise SystemExit(f"想定外の File 欄: {path}")
            line = f'  File "{path}", line {rest}'
        out.append(line)
    return "\n".join(out).replace(root, "@ROOT@")


def legacy_env():
    return {"PATH": os.environ["PATH"], "LC_ALL": "C"}


def run(cwd, argv, stdout=subprocess.PIPE):
    return subprocess.run([sys.executable, str(LEGACY), *argv], cwd=cwd, env=legacy_env(), stdin=subprocess.DEVNULL,
                          stdout=stdout, stderr=subprocess.PIPE, timeout=60)


def make_chain(directory, count):
    for index in range(count):
        os.symlink(f"l{index + 1}", os.path.join(directory, f"l{index}"))


def make_deep(path, depth, name):
    os.mkdir(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        for _ in range(depth):
            os.mkdir(name, dir_fd=fd)
            child = os.open(name, os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def wait_until(predicate, what):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise SystemExit(f"待ちきれない: {what}")


def opened(pid, target):
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except FileNotFoundError:
        return False
    for name in names:
        try:
            if os.readlink(f"/proc/{pid}/fd/{name}") == target:
                return True
        except OSError:
            pass
    return False


def blocked_in_fifo_open(pid):
    try:
        with open(f"/proc/{pid}/wchan") as stream:
            return stream.read() in ("fifo_open", "wait_for_partner")
    except FileNotFoundError:
        return False


def scenario_getcwd(root):
    work = os.path.join(root, "w")
    os.mkdir(work)
    fifo = os.path.join(work, "a.md")
    os.mkfifo(fifo)
    writer = os.open(fifo, os.O_RDWR)
    proc = subprocess.Popen([sys.executable, str(LEGACY), "."], cwd=work, env=legacy_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: opened(proc.pid, fifo), "旧版が FIFO を開く")
        os.unlink(fifo)
        os.rmdir(work)
        os.write(writer, b"[x](b)\n")
    finally:
        os.close(writer)
    stdout, stderr = proc.communicate(timeout=60)
    return proc.returncode, stdout, stderr


def scenario_sigint(root):
    os.mkfifo(os.path.join(root, "a.md"))
    proc = subprocess.Popen([sys.executable, str(LEGACY), "."], cwd=root, env=legacy_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wait_until(lambda: blocked_in_fifo_open(proc.pid), "旧版が FIFO を開くのを待つ")
    proc.send_signal(signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=60)
    return proc.returncode, stdout, stderr


def scenario_brokenpipe(root):
    Path(root, "a.md").write_text("".join(f"[x](m{index:03d}{'q' * 60})\n" for index in range(200)))
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        done = run(root, ["a.md"], stdout=write_end)
    finally:
        os.close(write_end)
    return done.returncode, b"", done.stderr


def scenario_devfull(root):
    Path(root, "a.md").write_text("[x](m)\n")
    with open("/dev/full", "wb") as full:
        done = run(root, ["a.md"], stdout=full)
    return done.returncode, b"", done.stderr


def os_exc(number, filename):
    return {"kind": "os", "errno": number, "filename": None if filename is None else form(filename)}


def scenarios():
    """(ID, 準備, 引数, 期待の組立て)。組立ては呼出履歴の列(先頭が前の例外)と、終わりの unraisable。"""
    long_name = "a" * 300

    def eacces_dir(root, name):
        os.mkdir(os.path.join(root, name), 0o000)

    def write(root, rel, data):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(data)

    def setup_deep(root):
        make_deep(os.path.join(root, "t"), 21, "d" * 200)

    def setup_entry_link(root):
        make_deep(os.path.join(root, "t"), 20, "d" * 200)
        fd = os.open(os.path.join(root, "t"), os.O_RDONLY)
        for _ in range(20):
            child = os.open("d" * 200, os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = child
        os.symlink("x", "l" * 100, dir_fd=fd)
        os.close(fd)

    def setup_recursion(root):
        current = os.path.join(root, "t")
        os.mkdir(current)
        fd = os.open(current, os.O_RDONLY)
        for _ in range(995):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = child
        os.close(fd)

    def setup_read_eacces(root):
        write(root, "a.md", b"[x](m)\n")
        os.chmod(os.path.join(root, "a.md"), 0o000)

    def setup_loop(root):
        os.mkdir(os.path.join(root, "d"))
        os.symlink("loop", os.path.join(root, "d/loop"))
        write(root, "d/a.md", b"[a](loop)\n")

    def setup_chain(root, body):
        make_chain(root, 1100)
        write(root, "a.md", body)

    def setup_code_alt(root):
        eacces_dir(root, "x")
        write(root, "d/doc.md", b"`x/y.md`\n")

    def setup_code_exists(root):
        write(root, "x/y.md", b"")
        write(root, "d/doc.md", b"`x/y.md`\n")
        eacces_dir(root, "d/x")

    return [
        ("target-isdir-eacces", lambda r: eacces_dir(r, "noperm"), ["noperm/x"],
         [{"site": "target-is-dir", "exception": os_exc(13, "noperm/x")}]),
        ("target-isdir-enametoolong", lambda r: None, [long_name],
         [{"site": "target-is-dir", "exception": os_exc(36, long_name)}]),
        ("target-warn-then-eacces", lambda r: eacces_dir(r, "noperm"), ["missing", "noperm/x"],
         [{"site": "target-is-dir", "exception": os_exc(13, "noperm/x")}]),
        ("rglob-deep-enametoolong", setup_deep, ["t"],
         [{"site": "rglob-wildcard-scandir", "exception": os_exc(36, "t/" + "/".join(["d" * 200] * 21))}]),
        ("rglob-recursion", setup_recursion, ["t"],
         [{"site": "rglob-recursion", "repeat": 989, "exception": {"kind": "recursion", "c_call": False}}]),
        ("read-isdir", lambda r: (os.makedirs(os.path.join(r, "g/x.md")), write(r, "g/y.md", b"[a](b)\n")), ["g"],
         [{"site": "read-open", "exception": os_exc(21, "g/x.md")}]),
        ("read-eacces", setup_read_eacces, ["a.md"], [{"site": "read-open", "exception": os_exc(13, "a.md")}]),
        ("read-fifo-sigint", scenario_sigint, None, [{"site": "read-open", "exception": {"kind": "keyboard"}}]),
        ("link-resolve-nul", lambda r: write(r, "a.md", b"[x](a\x00b)\n"), ["a.md"],
         [{"site": "link-resolve-lstat", "exception": {"kind": "value-null"}}]),
        ("link-resolve-loop", setup_loop, ["d/a.md"],
         [{"site": "resolve-stat", "exception": os_exc(40, "@ROOT@/d/loop")},
          {"site": "link-resolve-loop", "exception": {"kind": "runtime-loop", "filename": "@ROOT@/d/loop"}}]),
        ("link-resolve-getcwd", scenario_getcwd, None,
         [{"site": "link-resolve-getcwd", "exception": os_exc(2, None)}]),
        ("link-resolve-recursion", lambda r: setup_chain(r, b"[x](l0)\n"), ["a.md"],
         [{"site": "link-resolve-recursion", "repeat": 993, "exception": {"kind": "recursion", "c_call": True}}]),
        ("link-exists-eacces", lambda r: (eacces_dir(r, "n"), write(r, "a.md", b"[x](n/a.md)\n")), ["a.md"],
         [{"site": "link-exists", "exception": os_exc(13, "@ROOT@/n/a.md")}]),
        ("link-exists-enametoolong", lambda r: write(r, "a.md", f"[x]({long_name})\n".encode()), ["a.md"],
         [{"site": "link-exists", "exception": os_exc(36, "@ROOT@/" + long_name)}]),
        ("code-resolve-recursion", lambda r: setup_chain(r, b"`l0/x.md`\n"), ["a.md"],
         [{"site": "code-resolve-recursion", "repeat": 993, "exception": {"kind": "recursion", "c_call": True}}]),
        ("code-alt-eacces", setup_code_alt, ["d/doc.md"],
         [{"site": "code-alt-exists", "exception": os_exc(13, "x/y.md")}]),
        ("code-exists-eacces", setup_code_exists, ["d/doc.md"],
         [{"site": "code-exists", "exception": os_exc(13, "@ROOT@/d/x/y.md")}]),
        ("rglob-entry-isdir-enametoolong", setup_entry_link, ["t"],
         [{"site": "rglob-entry-is-dir", "repeat": 20,
           "exception": os_exc(36, "t/" + "/".join(["d" * 200] * 20) + "/" + "l" * 100)}]),
        ("print-brokenpipe", scenario_brokenpipe, None,
         [{"site": "print-error", "exception": os_exc(32, None)}]),
        ("exit-flush-devfull", scenario_devfull, None, [], {"exception": os_exc(28, None)}),
    ]


def restore_modes(root):
    """権限を外したのは 2 段目までなので、そこだけ戻す(深い木を辿らない)。"""
    for first in os.scandir(root):
        if first.is_symlink():
            continue
        os.chmod(first.path, 0o700)
        if first.is_dir(follow_symlinks=False):
            for second in os.scandir(first.path):
                if not second.is_symlink():
                    os.chmod(second.path, 0o700)


def remove_tree(root):
    """深さ 995 の木も消せるように、この生成の処理だけ再帰の上限を上げて消す。"""
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(10000)
    try:
        shutil.rmtree(root)
    finally:
        sys.setrecursionlimit(limit)


def traceback_cases():
    cases = []
    for item in scenarios():
        key, setup, argv, chain = item[:4]
        unraisable = item[4] if len(item) > 4 else None
        temp = tempfile.mkdtemp(prefix="r4u-tb-")
        root = os.path.realpath(temp)
        try:
            os.chmod(root, 0o700)
            if argv is None:
                code, stdout, stderr = setup(root)
            else:
                setup(root)
                done = run(root, argv)
                code, stdout, stderr = done.returncode, done.stdout, done.stderr
        finally:
            restore_modes(root)
            remove_tree(root)
        cases.append({"id": key, "exit": code, "stdout": form(stdout), "stderr": scrub(stderr, root),
                      "chain": chain, "unraisable": unraisable})
    return cases


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=HERE)
    args = parser.parse_args()
    check_python()
    sys.dont_write_bytecode = True
    python = {"version": platform.python_version(), "stdlib_sha256": STDLIB_SHA256,
              "legacy": {"path": LEGACY_REL, "sha256": hashlib.sha256(LEGACY.read_bytes()).hexdigest()}}
    with tempfile.TemporaryDirectory() as temp:
        decode = decode_cases(temp)
    units = {
        "schema_version": 1,
        "purpose": "R4U の文字列だけの部品の固定期待(本家 CPython 3.10.11 の実測)",
        "generator": "native/fixtures/r4u/gen_unit_fixtures.py",
        "byte_form": "文字列は UTF-8。非 UTF-8 は {\"hex\": ...}",
        "python": python,
        "exclude_dirs": sorted(EXCLUDE_DIRS),
        "pure_path": pure_path_cases(),
        "sort": sort_cases(),
        "posixpath": posixpath_cases(),
        "decode": decode,
        "repr": repr_cases(),
        "nonprintable_ranges": ranges(lambda c: not c.isprintable()),
        "frames": frame_cases(),
    }
    strerror, types, names = oserror_tables()
    linux = {
        "schema_version": 1,
        "purpose": "R4U の Linux の固定期待(glibc の文・例外の種類・旧版の呼出履歴。本家 CPython 3.10.11 の実測)",
        "generator": "native/fixtures/r4u/gen_unit_fixtures.py",
        "python": python,
        "glibc": platform.libc_ver()[1],
        "strerror": strerror,
        "exception_type": types,
        "exception_type_by_name": names,
        "tracebacks": traceback_cases(),
    }
    dump(args.out_dir / "units.json", units)
    dump(args.out_dir / "linux.json", linux)


if __name__ == "__main__":
    main()
