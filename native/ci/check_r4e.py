#!/usr/bin/env python3
"""R4E(#482)の専用比較。

旧版(plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py)と Rust の検査入口
(examples/r4e_contract)を、同じ長さの名前の別々の置き場で同じ形の入力に流し、終了値・標準出力・
標準エラー・ファイルの状態・XLSX の中身を比べる。旧版は変えない。

使い方(リポジトリの直下で。Python 3.10 系・root 以外の利用者):
  python3 -B native/ci/check_r4e.py --fixture <r4e_contract の絶対パス> \\
      --legacy-python <旧版用の venv の python の絶対パス> [--only <ID の正規表現>] [--jobs <数>]

結果(最後の行):
- 合格のときだけ「合格: <件数> 件(未実施 <件数> 件)/ <秒> 秒」で終了 0。
- --only を使った回は、選んだケースが全部一致しても「一部だけ: …」で終了 3(合格にしない)。
- 不一致・未実行・想定外の失敗・時間切れ・0 件は「不合格: …」で終了 1。

比べる前に、検査入口の隠しの切り替えで、起動前の記録(ふつう・fd 1 を閉じた・fd 2 を閉じた起動)と、呼出履歴の段の表
(固定名・行番号・関数・ソースの行)が旧版の環境の実物と同じことを確かめる(fixture_preflight)。

一時の置き場は --temp-parent(無ければ環境変数 R4E_TEMP_PARENT、それも無ければ tempfile の既定)の下に
1 つだけ作り、終わったらその 1 つだけを消す。CI では RUNNER_TEMP などを渡す。
子へ設定する資源の上限は RLIMIT_FSIZE だけ。ほかの上限(AS・NOFILE・CPU・STACK)は受け継いだ値のまま流し、
その値を結果の行に記録する(比較の前提の外。2026-11-12 00:42 の決定)。

比べ方の正本は設計(Issue #482 の「テスト計画」と「決定済みの差」)。ケースの一覧は
native/fixtures/r4e/cases.json(生成器 generate_cases.py)、固定期待は native/fixtures/r4e/expected/。
"""

import argparse
import ast
import concurrent.futures
import dataclasses
import datetime
import fcntl
import functools
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import resource
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import zlib
import zoneinfo

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'native' / 'fixtures' / 'r4e'
CASES_JSON = FIXTURES / 'cases.json'
EXPECTED_DIR = FIXTURES / 'expected'
MANIFEST_JSON = EXPECTED_DIR / 'manifest.json'
LEGACY_REL = 'plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py'
LEGACY = ROOT / LEGACY_REL
SCRIPT_NAME = b'md_tables_to_xlsx.py'
IS_LINUX = sys.platform.startswith('linux')
RUN_TIMEOUT = 300.0
WAIT_TIMEOUT = 60.0
GENERATOR_TIMEOUT = 600.0
SIGNAL_JOBS = 16  # 信号のケースは待つ時間が主で CPU をほとんど使わないので、CPU の数より多く並べてよい
PATH_LIMIT = 3800
ROOT_MARK = b'@ROOT@'
THREAD_MARK = b'@THREAD@'
ADDRESS_MARK = b'@ADDRESS@'
TIME_MARKS = {b'created': b'@CREATED@', b'modified': b'@MODIFIED@'}
EXIT_PASS, EXIT_FAIL, EXIT_PARTIAL = 0, 1, 3
LAYOUT = (b'work', b'home', b'tmp')
PREFILL_BYTE = b'#'
LEGACY_SIZE_MARGIN = 512  # 旧版の XLSX の大きさは core.xml の時刻の圧縮で数バイト変わる(cases.json の rlimit_rules)
TEMP_PARENT_ENV = 'R4E_TEMP_PARENT'
# 旧版の処理系そのものの資源の上限は比較の前提の外(2026-11-12 00:42)。設定せず、受け継いだ値を結果に記録する。
RECORDED_LIMITS = ('RLIMIT_AS', 'RLIMIT_NOFILE', 'RLIMIT_CPU', 'RLIMIT_STACK', 'RLIMIT_FSIZE')

# 決定済みの差(設計「決定済みの差」の表の行)。各ケースは cases.json の approved_differences で当てる差を宣言する。
# 宣言の無い差は、どの専用の比べ方も当てない(全部のバイトを比べる)。
APPROVED_DIFFERENCES = {
    'csv-path': 'openpyxl が無い旧版の CSV(2026-11-11 23:02)。比較は openpyxl がある旧版とだけ行う',
    'xlsx-bytes': 'XLSX のバイト(2026-11-11 23:02)。ZIP の構造と展開した各部分のバイトで比べ、圧縮のバイト列・圧縮後の大きさ・'
                  'ZIP の時刻の値・core.xml の 2 つの時刻の値は形式と実行の前後の範囲だけを確かめる',
    'encoding-settings': '文字コードの設定(22:24)。期待は PYTHONUTF8=1 の旧版',
    'fatal-current-thread': '起動の失敗の Current thread の値(17:45)。位置と形式だけ',
    'runtime-paths': '実行環境のパス(17:58・21:35・22:26・22:38)。呼出履歴の File の行と警告の所在地の欄だけを固定名へ',
    'variable-output': '実行ごとに変わる出力(22:31・22:35)。SIGINT は信号で終わること・終了値・最後の行・それまでの標準出力。'
                       'ZipFile.__del__ の番地は形式だけ',
    'write-partial-failure': '書き込みの途中の失敗(Q1)',
    'memory-and-zip64-position': '記憶域の不足と、位置・大きさで決まる ZIP64 の欄(Q2)。比較の前提の外',
    'uncompressed': '圧縮しない(Q3)。Rust だけが容量の上限で失敗する差',
    'temp-file': '一時ファイルを使わない(Q4)',
    'unlisted-startup-env': '一覧に無い起動の環境変数(Q5)。比較の前提の外',
}
ALWAYS_APPLIED = ('xlsx-bytes', 'runtime-paths')
OUT_OF_SCOPE = ('memory-and-zip64-position', 'unlisted-startup-env')
CATEGORIES = ('existing', 'external', 'csv', 'args', 'input', 'tables', 'cells', 'sheets', 'output', 'generated',
              'env', 'io', 'signals', 'write')
STREAM_MODES = ('pipe', 'closed', 'closed-pipe', 'dev-full', 'directory', 'file', 'full-pipe', 'prefilled-full-pipe',
                'nonblock-full-pipe')

TB_HEADER = b'Traceback (most recent call last):'
FRAME = re.compile(rb'  File "(?P<file>[^"\n]*)", line (?P<line>[0-9]+), in (?P<func>.*)')
REPEATED = re.compile(rb'  \[Previous line repeated [0-9]+ more times?\]')
WARNING_LINE = re.compile(rb'(?P<path>/[^\n:]*\.py):(?P<line>[0-9]+): (?P<category>[A-Za-z_][A-Za-z0-9_]*Warning): ')
CURRENT_THREAD = re.compile(rb'Current thread 0x(?P<value>[0-9a-f]{16}) \(most recent call first\):')
ZIP_DEL = re.compile(rb'Exception ignored in: <function ZipFile\.__del__ at 0x(?P<value>[0-9a-f]{1,16})>')
BLOCK_MARKERS = (b'During handling of the above exception, another exception occurred:',
                 b'The above exception was the direct cause of the following exception:')
CORE_TIME = re.compile(
    rb'<dcterms:(created|modified) xsi:type="dcterms:W3CDTF">([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)</dcterms:\1>'
)
PART_ORDER_HEAD = (b'docProps/app.xml', b'docProps/core.xml', b'xl/theme/theme1.xml')
PART_ORDER_TAIL = (b'xl/styles.xml', b'_rels/.rels', b'xl/workbook.xml', b'xl/_rels/workbook.xml.rels', b'[Content_Types].xml')
WB_SAVE_LINE = b'    wb.save(checked_out)'
Q3_LAST_LINE = b'OSError: [Errno 27] File too large'
Q3_SECOND_HEADER = b'Exception ignored in: <function ZipFile.__del__ at 0x' + ADDRESS_MARK + b'>'
# 検査入口の隠しの切り替え(examples/r4e_contract.rs)。比較の前に、起動前の記録(Task 1.2 の門)と、
# 呼出履歴の段の表が旧版の環境の実物と同じことを確かめる(fixture_preflight)。
STARTUP_PROBE_ENV = b'R4E_CONTRACT_STARTUP_PROBE'
FRAMES_PROBE_ENV = b'R4E_CONTRACT_FRAMES'
STARTUP_RECORD = re.compile(rb'r4e-startup (?P<fd0>[^ \n]+) (?P<fd1>[^ \n]+) (?P<fd2>[^ \n]+) '
                            rb'sigpipe=(?:default|ignore) sigint=(?:default|ignore) now=[^ \n]+ [^ \n]+ [^ \n]+\n')
CLOSED_RECORD = b'closed/invalid:9'  # EBADF(Linux・Mac とも 9)
ANONYMOUS_SCOPES = {'Lambda': '<lambda>', 'ListComp': '<listcomp>', 'SetComp': '<setcomp>', 'DictComp': '<dictcomp>',
                    'GeneratorExp': '<genexpr>'}


class Failure(Exception):
    """比較を続けられない失敗(前提の誤り・旧版の環境の違い・想定外の失敗)。"""


class ZipFormatError(Exception):
    pass


def show(data, limit=240):
    text = data.decode('utf-8', 'backslashreplace') if isinstance(data, bytes) else str(data)
    text = text.replace('\r', '\\r').replace('\n', '\\n')
    return text if len(text) <= limit else text[:limit] + '…'


def lines_of(data):
    """改行を残して行に分ける(\\n だけで分ける。\\r は行の中の文字として残す)。"""
    return re.findall(rb'[^\n]*\n|[^\n]+\Z', data)


def eol_strip(line):
    if line.endswith(b'\r\n'):
        return line[:-2]
    if line.endswith(b'\n'):
        return line[:-1]
    return line


def first_difference(old, new):
    old_lines, new_lines = lines_of(old), lines_of(new)
    for index, (left, right) in enumerate(zip(old_lines, new_lines)):
        if left != right:
            return f'{index + 1} 行目: 旧版 {show(left)} / Rust {show(right)}'
    if len(old_lines) != len(new_lines):
        extra = old_lines[len(new_lines):] or new_lines[len(old_lines):]
        return f'行数: 旧版 {len(old_lines)} / Rust {len(new_lines)}(余分の最初 {show(extra[0])})'
    return 'バイトが違う'


# --- 出力の正規化 ---

@dataclasses.dataclass(frozen=True)
class RuntimePaths:
    """旧版の実行環境の絶対パス。呼出履歴の File の行と警告の所在地の欄だけで固定名へ置き換える。"""

    script: bytes
    stdlib: bytes
    site: bytes

    def relative(self, path):
        if path == self.script:
            return SCRIPT_NAME
        for prefix in (self.site, self.stdlib):
            if prefix and path.startswith(prefix + b'/'):
                return path[len(prefix) + 1:]
        return path


def normalize_runtime(data, paths):
    """呼出履歴の File の行と、警告の先頭の所在地の欄だけを置き換える。

    利用者が渡したパス(WARN・ERROR の行)と、例外の文(入力本文を含みうる)の中の偽の File の行は置き換えない。
    File の行と見なすのは、Traceback の見出しの直後に続く File の行・ソースの行・繰り返しの行の並びだけ。
    """
    out = []
    state = None
    for line in lines_of(data):
        body = eol_strip(line)
        if body == TB_HEADER:
            state = 'frames'
            out.append(line)
            continue
        if state is not None:
            match = FRAME.fullmatch(body)
            if match:
                path = match.group('file')
                new = paths.relative(path)
                if new != path:
                    line = line[:match.start('file')] + new + line[match.end('file'):]
                out.append(line)
                state = 'after-frame'
                continue
            if state == 'after-frame' and body.startswith(b'    '):
                out.append(line)
                state = 'frames'
                continue
            if REPEATED.fullmatch(body):
                out.append(line)
                state = 'frames'
                continue
            state = None
        match = WARNING_LINE.match(body)
        if match:
            path = match.group('path')
            new = paths.relative(path)
            if new != path and new != SCRIPT_NAME:
                line = line[:match.start('path')] + new + line[match.end('path'):]
        out.append(line)
    return b''.join(out)


def mark_lines(data, pattern, mark):
    out = []
    for line in lines_of(data):
        match = pattern.fullmatch(eol_strip(line))
        if match:
            line = line[:match.start('value')] + mark + line[match.end('value'):]
        out.append(line)
    return b''.join(out)


def normalize_output(data, root, paths, diffs):
    """置き場のパスを印へ置き換え、宣言された差の欄だけを印にする。"""
    if root:
        data = data.replace(root, ROOT_MARK)
    data = normalize_runtime(data, paths)
    if 'fatal-current-thread' in diffs:
        data = mark_lines(data, CURRENT_THREAD, THREAD_MARK)
    if 'variable-output' in diffs:
        data = mark_lines(data, ZIP_DEL, ADDRESS_MARK)
    return data


@dataclasses.dataclass
class Frame:
    file: bytes
    line: int
    func: bytes


@dataclasses.dataclass
class Block:
    """1 つの呼出履歴。header は直前の Exception ignored in: や連鎖の区切りの行。"""

    header: list
    lines: list
    frames: list
    exception: list


def is_marker(body):
    return body == TB_HEADER or body.startswith(b'Exception ignored in: ') or body in BLOCK_MARKERS


def parse_blocks(data):
    lines = lines_of(data)
    blocks = []
    pending = []
    index = 0
    while index < len(lines):
        body = eol_strip(lines[index])
        if body != TB_HEADER:
            if is_marker(body) or (pending and body == b''):
                pending.append(lines[index])
            else:
                pending = []
            index += 1
            continue
        block = Block(header=[eol_strip(line) for line in pending if eol_strip(line)], lines=[lines[index]], frames=[], exception=[])
        pending = []
        index += 1
        after_frame = False
        while index < len(lines):
            body = eol_strip(lines[index])
            match = FRAME.fullmatch(body)
            if match:
                block.frames.append(Frame(match.group('file'), int(match.group('line')), match.group('func')))
                after_frame = True
            elif after_frame and body.startswith(b'    '):
                after_frame = False
            elif REPEATED.fullmatch(body):
                after_frame = False
            else:
                break
            block.lines.append(lines[index])
            index += 1
        exception = []
        while index < len(lines) and not is_marker(eol_strip(lines[index])):
            exception.append(lines[index])
            index += 1
        while exception and eol_strip(exception[-1]) == b'':
            pending.insert(0, exception.pop())
        block.lines.extend(exception)
        block.exception = [eol_strip(line) for line in exception]
        blocks.append(block)
    return blocks


def temp_file_failure(stderr, tmpdir):
    """Q4 の見分け方。旧版の失敗が一時ファイルによるときだけ、当たった条件の名前を返す。

    stderr は正規化(置き場の印・実行環境のパス)した後のもの。tmpdir はそのケースの TMPDIR(印の形)。
    """
    reasons = []
    name = re.compile(re.escape(tmpdir) + rb'/openpyxl\.[a-z0-9_]{8}(?![a-z0-9_])')
    for block in parse_blocks(stderr):
        if any(name.search(line) for line in block.exception):
            reasons.append('一時ファイルの名前')
        if any(frame.file == b'openpyxl/writer/excel.py' and frame.line in (199, 200) for frame in block.frames):
            reasons.append('excel.py の 199・200 行')
        if any(frame.file == b'tempfile.py' for frame in block.frames) and any(
                b'No usable temporary directory found' in line for line in block.exception):
            reasons.append('No usable temporary directory found')
    return sorted(set(reasons))


def last_line(data):
    lines = lines_of(data)
    return lines[-1] if lines else b''


def head_through(data, marker):
    """先頭から marker の行(改行まで)を返す。無ければ None。"""
    for line in lines_of(data):
        if eol_strip(line) == marker:
            end = data.index(line) + len(line)
            return data[:end]
    return None


@functools.lru_cache(maxsize=None)
def legacy_head():
    """Q3 の固定期待の呼出履歴の先頭。旧版のソースから作る(旧版や Rust の実行結果からは作らない)。"""
    source = LEGACY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    lines = source.splitlines()

    def call_line(scope, predicate):
        found = sorted({node.lineno for node in ast.walk(scope) if isinstance(node, ast.Call) and predicate(node.func)})
        if len(found) != 1:
            raise Failure('旧版のソースに呼出履歴の参照行が見つからない')
        return found[0]

    def function(name):
        return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)

    frames = [
        (call_line(tree, lambda f: isinstance(f, ast.Attribute) and f.attr == 'exit'
                   and isinstance(f.value, ast.Name) and f.value.id == 'sys'), b'<module>'),
        (call_line(function('main'), lambda f: isinstance(f, ast.Name) and f.id == 'write_xlsx'), b'main'),
        (call_line(function('write_xlsx'), lambda f: isinstance(f, ast.Attribute) and f.attr == 'save'
                   and isinstance(f.value, ast.Name) and f.value.id == 'wb'), b'write_xlsx'),
    ]
    out = [TB_HEADER + b'\n']
    for number, name in frames:
        out.append(b'  File "' + SCRIPT_NAME + b'", line ' + str(number).encode() + b', in ' + name + b'\n')
        out.append(b'    ' + lines[number - 1].strip().encode('utf-8') + b'\n')
    head = b''.join(out)
    if not head.endswith(WB_SAVE_LINE + b'\n'):
        raise Failure('旧版のソースの wb.save の行が想定と違う')
    return head


# --- ZIP の読取り ---

EOCD = struct.Struct('<4s4H2LH')
ZIP64_LOCATOR = struct.Struct('<4sLQL')
ZIP64_EOCD = struct.Struct('<4sQ2H2L4Q')
CENTRAL = struct.Struct('<4s4B4HL2L5H2L')
LOCAL = struct.Struct('<4s2B4HL2L2H')


def _zip64_extra(extra, usize, csize, offset):
    position = 0
    while position + 4 <= len(extra):
        tag, size = struct.unpack_from('<HH', extra, position)
        body = extra[position + 4:position + 4 + size]
        if tag == 1:
            values = []
            cursor = 0
            for value in (usize, csize, offset):
                if value == 0xFFFFFFFF:
                    if cursor + 8 > len(body):
                        raise ZipFormatError('ZIP64 の追加の欄が短い')
                    values.append(struct.unpack_from('<Q', body, cursor)[0])
                    cursor += 8
                else:
                    values.append(value)
            return values
        position += 4 + size
    return usize, csize, offset


def parse_zip(data):
    """自前の ZIP の読取り。構造が想定と違えば ZipFormatError。"""
    if len(data) < EOCD.size:
        raise ZipFormatError('中央ディレクトリの終わりが無い')
    start = data.rfind(b'PK\x05\x06', max(0, len(data) - 65557))
    if start < 0:
        raise ZipFormatError('中央ディレクトリの終わりが無い')
    sig, disk, cd_disk, disk_count, total, cd_size, cd_offset, comment_len = EOCD.unpack_from(data, start)
    if start + EOCD.size + comment_len != len(data):
        raise ZipFormatError('中央ディレクトリの終わりがファイルの終わりで終わらない')
    eocd = {'disk': disk, 'cd_disk': cd_disk, 'disk_count': disk_count, 'total': total, 'cd_size': cd_size,
            'comment': data[start + EOCD.size:], 'zip64': None}
    end_of_cd = start
    if start >= ZIP64_LOCATOR.size and data[start - ZIP64_LOCATOR.size:start - ZIP64_LOCATOR.size + 4] == b'PK\x06\x07':
        _, z_disk, record_offset, z_disks = ZIP64_LOCATOR.unpack_from(data, start - ZIP64_LOCATOR.size)
        if record_offset + ZIP64_EOCD.size > len(data) or data[record_offset:record_offset + 4] != b'PK\x06\x06':
            raise ZipFormatError('ZIP64 の中央ディレクトリの終わりが無い')
        fields = ZIP64_EOCD.unpack_from(data, record_offset)
        _, record_size, made, needed, d1, d2, count_disk, count_total, cd_size, cd_offset = fields
        if record_offset + 12 + record_size != start - ZIP64_LOCATOR.size:
            raise ZipFormatError('ZIP64 の記録の大きさが合わない')
        eocd['zip64'] = {'made': made, 'needed': needed, 'disk': d1, 'cd_disk': d2, 'count_disk': count_disk,
                         'count_total': count_total, 'locator_disk': z_disk, 'disks': z_disks}
        total = count_total
        eocd['cd_size'] = cd_size
        end_of_cd = record_offset
    if (disk, cd_disk) != (0, 0) or (eocd['zip64'] is None and disk_count != total):
        raise ZipFormatError('複数の円盤の ZIP')
    if cd_offset + cd_size != end_of_cd:
        raise ZipFormatError('中央ディレクトリの位置と大きさが、終わりの位置と合わない')
    entries = []
    position = cd_offset
    for _ in range(total):
        if position + CENTRAL.size > end_of_cd or data[position:position + 4] != b'PK\x01\x02':
            raise ZipFormatError('中央ディレクトリの項目が壊れている')
        (_, create_version, create_system, extract_version, reserved, flags, method, mtime, mdate, crc, csize,
         usize, name_len, extra_len, comment_len2, disk_start, internal, external, offset) = CENTRAL.unpack_from(data, position)
        cursor = position + CENTRAL.size
        name = data[cursor:cursor + name_len]
        extra = data[cursor + name_len:cursor + name_len + extra_len]
        comment = data[cursor + name_len + extra_len:cursor + name_len + extra_len + comment_len2]
        position = cursor + name_len + extra_len + comment_len2
        usize, csize, offset = _zip64_extra(extra, usize, csize, offset)
        entries.append({
            'name': name, 'create_version': create_version, 'create_system': create_system,
            'extract_version': extract_version, 'reserved': reserved, 'flag_bits': flags, 'method': method,
            'time': mtime, 'date': mdate, 'crc': crc, 'compress_size': csize, 'file_size': usize,
            'extra': extra, 'comment': comment, 'disk_start': disk_start, 'internal_attr': internal,
            'external_attr': external, 'offset': offset,
        })
    if position != end_of_cd:
        raise ZipFormatError('中央ディレクトリの大きさが項目の合計と合わない')
    cursor = 0
    for entry in entries:
        if entry['offset'] != cursor:
            raise ZipFormatError(f'部分 {show(entry["name"])} が隙間なく並んでいない(位置 {entry["offset"]}、想定 {cursor})')
        if cursor + LOCAL.size > cd_offset or data[cursor:cursor + 4] != b'PK\x03\x04':
            raise ZipFormatError(f'部分 {show(entry["name"])} のローカルヘッダが無い')
        (_, l_extract, l_reserved, l_flags, l_method, l_time, l_date, l_crc, l_csize, l_usize, l_name_len,
         l_extra_len) = LOCAL.unpack_from(data, cursor)
        body = cursor + LOCAL.size
        l_name = data[body:body + l_name_len]
        l_extra = data[body + l_name_len:body + l_name_len + l_extra_len]
        raw_start = body + l_name_len + l_extra_len
        raw_end = raw_start + entry['compress_size']
        if raw_end > cd_offset:
            raise ZipFormatError(f'部分 {show(entry["name"])} が中央ディレクトリに重なる')
        raw = data[raw_start:raw_end]
        cursor = raw_end
        if entry['flag_bits'] & 0x08:
            if data[cursor:cursor + 4] == b'PK\x07\x08':
                cursor += 4
            d_crc, d_csize, d_usize = struct.unpack_from('<3L', data, cursor)
            cursor += 12
            if (d_crc, d_csize, d_usize) != (entry['crc'], entry['compress_size'] & 0xFFFFFFFF, entry['file_size'] & 0xFFFFFFFF):
                raise ZipFormatError(f'部分 {show(entry["name"])} のデータ記述子が中央ディレクトリと合わない')
            local_sizes = (l_crc, l_csize, l_usize) == (0, 0, 0)
        else:
            local_usize, local_csize, _ = _zip64_extra(l_extra, l_usize, l_csize, 0)
            local_sizes = (l_crc, local_csize, local_usize) == (entry['crc'], entry['compress_size'], entry['file_size'])
        mismatch = [label for label, ok in (
            ('名前', l_name == entry['name']),
            ('版', (l_extract, l_reserved) == (entry['extract_version'], entry['reserved'])),
            ('旗', l_flags == entry['flag_bits']),
            ('方式', l_method == entry['method']),
            ('時刻', (l_time, l_date) == (entry['time'], entry['date'])),
            ('CRC-32 と大きさ', local_sizes),
            ('追加の欄', l_extra == entry['extra'] or entry['extra'][:2] == b'\x01\x00'),
        ) if not ok]
        if mismatch:
            raise ZipFormatError(f'部分 {show(entry["name"])} のローカルヘッダと中央ディレクトリが合わない: {"・".join(mismatch)}')
        entry['local_extra'] = l_extra
        if entry['flag_bits'] & 0x01:
            raise ZipFormatError(f'部分 {show(entry["name"])} が暗号化されている')
        if entry['method'] == 0:
            content = raw
        elif entry['method'] == 8:
            inflater = zlib.decompressobj(-15)
            try:
                content = inflater.decompress(raw) + inflater.flush()
            except zlib.error as error:
                raise ZipFormatError(f'部分 {show(entry["name"])} を展開できない: {error}') from error
            if not inflater.eof or inflater.unused_data:
                raise ZipFormatError(f'部分 {show(entry["name"])} の圧縮データの終わりが合わない')
        else:
            raise ZipFormatError(f'部分 {show(entry["name"])} の方式 {entry["method"]} を扱えない')
        if len(content) != entry['file_size']:
            raise ZipFormatError(f'部分 {show(entry["name"])} の展開後の大きさが合わない')
        if zlib.crc32(content) != entry['crc']:
            raise ZipFormatError(f'部分 {show(entry["name"])} の CRC-32 が合わない')
        entry['content'] = content
    if cursor != cd_offset:
        raise ZipFormatError(f'最後の部分と中央ディレクトリの間に隙間がある(位置 {cursor}、中央ディレクトリ {cd_offset})')
    return {'entries': entries, 'eocd': eocd}


def is_complete_zip(data):
    try:
        parse_zip(data)
    except (ZipFormatError, struct.error):
        return False
    return True


def dos_fields(date, time_value):
    return ((date >> 9) + 1980, (date >> 5) & 0x0F, date & 0x1F, time_value >> 11, (time_value >> 5) & 0x3F, (time_value & 0x1F) * 2)


@functools.lru_cache(maxsize=None)
def zone(tz):
    if tz is None:
        try:
            with open('/etc/localtime', 'rb') as handle:
                return zoneinfo.ZoneInfo.from_file(handle)
        except FileNotFoundError:
            return datetime.timezone.utc
    name = tz[1:] if tz.startswith(':') else tz
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
        if name in ('UTC', 'GMT', 'UTC0'):
            return datetime.timezone.utc
        raise Failure(f'TZ={tz} の現地時刻を求められない') from error


def local_fields(epoch, tz):
    return datetime.datetime.fromtimestamp(epoch, zone(tz)).timetuple()[:6]


def time_problems(entries, run, label):
    """ZIP の時刻(現地時刻・2 秒単位)と core.xml の 2 つの時刻(UTC)が実行の前後の範囲に入るか。"""
    problems = []
    low = list(local_fields(math.floor(run.before), run.tz))
    low[5] -= low[5] % 2
    high = local_fields(math.floor(run.after), run.tz)
    for entry in entries:
        fields = dos_fields(entry['date'], entry['time'])
        if not tuple(low) <= fields <= tuple(high):
            problems.append(f'{label} の ZIP の時刻が実行の前後の範囲に入らない: {show(entry["name"])} {fields}(範囲 {tuple(low)}〜{high})')
    return problems


def mark_core(content, run, label):
    problems = []
    found = CORE_TIME.findall(content)
    if [name for name, _ in found] != [b'created', b'modified']:
        return content, [f'{label} の docProps/core.xml の時刻の形が想定と違う']
    for _, value in found:
        moment = datetime.datetime.strptime(value.decode(), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()
        if not math.floor(run.before) <= moment <= run.after:
            problems.append(f'{label} の docProps/core.xml の時刻 {value.decode()} が実行の前後の範囲に入らない')

    def replace(match):
        return match.group(0).replace(match.group(2), TIME_MARKS[match.group(1)])

    return CORE_TIME.sub(replace, content), problems


ENTRY_FIELDS = ('name', 'create_version', 'create_system', 'extract_version', 'reserved', 'flag_bits', 'method',
                'file_size', 'internal_attr', 'external_attr', 'extra', 'comment', 'disk_start', 'local_extra')
EOCD_FIELDS = ('disk', 'cd_disk', 'disk_count', 'total', 'cd_size', 'comment', 'zip64')


def xlsx_side_problems(archive, run, label, fixed_parts):
    problems = time_problems(archive['entries'], run, label)
    names = [entry['name'] for entry in archive['entries']]
    sheets = names[len(PART_ORDER_HEAD):len(names) - len(PART_ORDER_TAIL)]
    if (tuple(names[:len(PART_ORDER_HEAD)]) != PART_ORDER_HEAD or tuple(names[len(names) - len(PART_ORDER_TAIL):]) != PART_ORDER_TAIL
            or sheets != [f'xl/worksheets/sheet{index}.xml'.encode() for index in range(1, len(sheets) + 1)] or not sheets):
        problems.append(f'{label} の ZIP の部分の順が想定と違う: {[show(name) for name in names]}')
    for entry in archive['entries']:
        expected = fixed_parts.get(entry['name'])
        if expected is not None and hashlib.sha256(entry['content']).hexdigest() != expected:
            problems.append(f'{label} の {show(entry["name"])} が固定期待(native/fixtures/r4e/expected/manifest.json)と違う')
    return problems


def compare_xlsx(rel, old_data, new_data, old, new, fixed_parts):
    """XLSX を ZIP の構造と展開した各部分のバイトで比べる(決定済みの差「XLSX のバイト」)。"""
    where = show(rel)
    try:
        old_zip = parse_zip(old_data)
    except (ZipFormatError, struct.error) as error:
        return [f'{where}: 旧版の XLSX を読めない: {error}']
    try:
        new_zip = parse_zip(new_data)
    except (ZipFormatError, struct.error) as error:
        return [f'{where}: Rust の XLSX を読めない: {error}']
    problems = []
    problems += [f'{where}: {text}' for text in xlsx_side_problems(old_zip, old, '旧版', fixed_parts)]
    problems += [f'{where}: {text}' for text in xlsx_side_problems(new_zip, new, 'Rust', fixed_parts)]
    for key in EOCD_FIELDS:
        if old_zip['eocd'][key] != new_zip['eocd'][key]:
            problems.append(f'{where}: 中央ディレクトリの終わりの {key} が違う: 旧版 {old_zip["eocd"][key]!r} / Rust {new_zip["eocd"][key]!r}')
    old_names = [entry['name'] for entry in old_zip['entries']]
    new_names = [entry['name'] for entry in new_zip['entries']]
    if old_names != new_names:
        problems.append(f'{where}: 部分の順か名前が違う: 旧版 {[show(n) for n in old_names]} / Rust {[show(n) for n in new_names]}')
        return problems
    for left, right in zip(old_zip['entries'], new_zip['entries']):
        name = show(left['name'])
        for key in ENTRY_FIELDS:
            if left[key] != right[key]:
                problems.append(f'{where}: 部分 {name} の {key} が違う: 旧版 {left[key]!r} / Rust {right[key]!r}')
        old_content, new_content = left['content'], right['content']
        if left['name'] == b'docProps/core.xml':
            old_content, extra_old = mark_core(old_content, old, '旧版')
            new_content, extra_new = mark_core(new_content, new, 'Rust')
            problems += [f'{where}: {text}' for text in extra_old + extra_new]
        elif left['crc'] != right['crc']:
            problems.append(f'{where}: 部分 {name} の CRC-32 が違う: 旧版 {left["crc"]:08x} / Rust {right["crc"]:08x}')
        if old_content != new_content:
            position = next((i for i, (a, b) in enumerate(zip(old_content, new_content)) if a != b), min(len(old_content), len(new_content)))
            problems.append(f'{where}: 部分 {name} の展開後のバイトが違う(最初に違う位置 {position}: 旧版 '
                            f'{show(old_content[max(0, position - 40):position + 40])} / Rust {show(new_content[max(0, position - 40):position + 40])})')
    return problems


# --- 置き場の操作(長いパスは親の fd からたどる) ---

def _parts(rel):
    return [part for part in rel.split(b'/') if part not in (b'', b'.')]


def _walk_fd(root, parts, create=False):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


class Place:
    """置き場の root からの相対パス(bytes)で操作する。"""

    def __init__(self, root):
        self.root = root

    def _full(self, rel):
        full = self.root + b'/' + rel if rel else self.root
        return full if len(full) < PATH_LIMIT else None

    def _at(self, rel, action, create=False):
        parts = _parts(rel)
        fd = _walk_fd(self.root, parts[:-1], create=create)
        try:
            return action(parts[-1], fd)
        finally:
            os.close(fd)

    def lstat(self, rel):
        full = self._full(rel)
        if full is not None:
            return os.lstat(full)
        return self._at(rel, lambda name, fd: os.stat(name, dir_fd=fd, follow_symlinks=False))

    def listdir(self, rel):
        full = self._full(rel)
        if full is not None:
            return os.listdir(full)
        fd = _walk_fd(self.root, _parts(rel))
        try:
            return [os.fsencode(name) for name in os.listdir(fd)]
        finally:
            os.close(fd)

    def readlink(self, rel):
        full = self._full(rel)
        if full is not None:
            return os.readlink(full)
        return self._at(rel, lambda name, fd: os.readlink(name, dir_fd=fd))

    def read(self, rel):
        def action(name, fd):
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                chunks = []
                while True:
                    chunk = os.read(handle, 1 << 20)
                    if not chunk:
                        return b''.join(chunks)
                    chunks.append(chunk)
            finally:
                os.close(handle)
        return self._at(rel, action)

    def chmod(self, rel, mode):
        self._at(rel, lambda name, fd: os.chmod(name, mode, dir_fd=fd))

    def mkdirs(self, rel):
        fd = _walk_fd(self.root, _parts(rel), create=True)
        os.close(fd)

    def write(self, rel, data, mode=0o644):
        def action(name, fd):
            handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(handle, view):]
            finally:
                os.close(handle)
        self._at(rel, action, create=True)

    def symlink(self, rel, target):
        self._at(rel, lambda name, fd: os.symlink(target, name, dir_fd=fd), create=True)

    def mkfifo(self, rel):
        # mkfifoat は Mac 13 より前に無いので、短いパスはそのまま使う。
        self.mkdirs(parent_of(rel))
        full = self._full(rel)
        if full is None:
            raise Failure(f'長すぎるパスに FIFO を作れない: {show(rel)}')
        os.mkfifo(full)


@dataclasses.dataclass(frozen=True)
class Entry:
    kind: int
    mode: int
    dev: int
    ino: int
    nlink: int
    size: int
    mtime: int
    ctime: int
    link: object
    digest: object


def snapshot(place, tops=LAYOUT):
    found = {}
    stack = list(reversed(tops))
    while stack:
        rel = stack.pop()
        try:
            info = place.lstat(rel)
        except FileNotFoundError:
            continue
        link = digest = None
        if stat.S_ISLNK(info.st_mode):
            link = place.readlink(rel)
        elif stat.S_ISREG(info.st_mode):
            try:
                digest = hashlib.sha256(place.read(rel)).hexdigest()
            except PermissionError:
                digest = None
        found[rel] = Entry(stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_dev, info.st_ino, info.st_nlink,
                           info.st_size, info.st_mtime_ns, info.st_ctime_ns, link, digest)
        if stat.S_ISDIR(info.st_mode):
            try:
                names = place.listdir(rel)
            except PermissionError:
                continue
            for name in sorted(names, reverse=True):
                stack.append(rel + b'/' + name)
    return found


def parent_of(rel):
    return rel.rsplit(b'/', 1)[0] if b'/' in rel else b''


def kind_name(kind):
    return {stat.S_IFDIR: 'dir', stat.S_IFREG: 'file', stat.S_IFLNK: 'symlink', stat.S_IFIFO: 'fifo'}.get(kind, oct(kind))


def diff_trees(before, after, scratch):
    """実行の前後の一覧の差。scratch は一時ファイルの置き場(TMPDIR)。

    入力(前からあるもの)は、種類・モード・内容・リンクの字面・dev・ino・mtime・ctime が変わらないこと。
    変わってよいのは、中の項目が増減したディレクトリの mtime・ctime・大きさ・リンク数と、TMPDIR の mtime・ctime と、
    上書きした .xlsx の通常ファイル(旧版と同じかを別に比べる)だけ。
    """
    created = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed_parents = {parent_of(rel) for rel in created + removed}
    violations = [f'入力が消えた: {show(rel)}' for rel in removed]
    modified = []
    for rel in sorted(set(before) & set(after)):
        old, new = before[rel], after[rel]
        if old == new:
            continue
        if (old.kind == new.kind == stat.S_IFDIR and (old.mode, old.dev, old.ino, old.link) == (new.mode, new.dev, new.ino, new.link)
                and (rel in changed_parents or rel in scratch)):
            continue
        if old.kind == new.kind == stat.S_IFREG and rel.endswith(b'.xlsx') and old.dev == new.dev:
            modified.append((rel, new.mode, old.ino == new.ino))
            continue
        fields = [name for name in ('kind', 'mode', 'dev', 'ino', 'nlink', 'size', 'mtime', 'ctime', 'link', 'digest')
                  if getattr(old, name) != getattr(new, name)]
        violations.append(f'入力が変わった: {show(rel)}({"・".join(fields)})')
    created_list = []
    for rel in created:
        entry = after[rel]
        created_list.append((rel, kind_name(entry.kind), entry.mode, entry.link))
        if rel.startswith((b'home/', b'tmp/')) or any(rel.startswith(path + b'/') for path in scratch):
            violations.append(f'TMPDIR か HOME に残った: {show(rel)}')
    return created_list, modified, violations


def make_removable(place):
    """後片付けのため、置き場のディレクトリを全部 0o700 にする(長いパスと深い入れ子に対応する)。"""
    stack = [b'']
    while stack:
        rel = stack.pop()
        try:
            info = place.lstat(rel)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_IMODE(info.st_mode) != 0o700:
            if rel:
                place.chmod(rel, 0o700)
            else:
                os.chmod(place.root, 0o700)
        for name in place.listdir(rel):
            stack.append(rel + b'/' + name if rel else name)


def remove_tree(root):
    """深い入れ子と長いパスでも、再帰と多くの fd を使わずに消す。"""
    if not os.path.lexists(root):
        return
    place = Place(root)
    make_removable(place)
    order = []
    stack = [b'']
    while stack:
        rel = stack.pop()
        info = place.lstat(rel)
        order.append((rel, stat.S_ISDIR(info.st_mode)))
        if stat.S_ISDIR(info.st_mode):
            for name in place.listdir(rel):
                stack.append(rel + b'/' + name if rel else name)
    for rel, is_dir in reversed(order):
        if not rel:
            os.rmdir(root)
        elif is_dir:
            place._at(rel, lambda name, fd: os.rmdir(name, dir_fd=fd))
        else:
            place._at(rel, lambda name, fd: os.unlink(name, dir_fd=fd))


# --- 子の起動 ---

LAUNCHER = r'''
import json, os, resource, signal, sys
error_fd = None
try:
    config = json.loads(sys.argv[1])
    error_fd = config["error_fd"]
    os.set_inheritable(error_fd, False)
    os.umask(config["umask"])
    if config["rlimit_fsize"] is not None:
        hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
        resource.setrlimit(resource.RLIMIT_FSIZE, (config["rlimit_fsize"], hard))
    signal.pthread_sigmask(signal.SIG_SETMASK, set())
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN if config["sigint_ignored"] else signal.SIG_DFL)
    os.chdir(bytes.fromhex(config["cwd"]))
    for part in config["components"]:
        os.chdir(bytes.fromhex(part))
    sources = config["fds"]
    for target, source in enumerate(sources):
        if source is None:
            try:
                os.close(target)
            except OSError:
                pass
        else:
            os.dup2(source, target)
    for source in set(value for value in sources if value is not None):
        os.close(source)
    argv = [bytes.fromhex(value) for value in config["argv"]]
    env = {bytes.fromhex(key): bytes.fromhex(value) for key, value in config["env"]}
    os.execve(argv[0], argv, env)
except BaseException as error:
    if error_fd is not None:
        os.write(error_fd, repr(error).encode("utf-8", "backslashreplace"))
    os._exit(127)
'''


@dataclasses.dataclass
class RunSpec:
    argv: list
    cwd: bytes
    components: list
    env: dict
    umask: int
    stdin: object
    stdout: object
    stderr: object
    stream_dir: bytes
    rlimit_fsize: object = None
    sigint_ignored: bool = False
    signals: object = None
    output: object = None
    timeout: float = RUN_TIMEOUT


@dataclasses.dataclass
class RawResult:
    status: tuple
    stdout: bytes
    stderr: bytes
    streams: dict
    before: float
    after: float
    timed_out: bool
    sent: int


def configure_pty(slave, columns, rows):
    attributes = termios.tcgetattr(slave)
    attributes[1] |= termios.OPOST | termios.ONLCR
    for name in ('OCRNL', 'ONOCR', 'ONLRET'):
        attributes[1] &= ~getattr(termios, name, 0)
    termios.tcsetattr(slave, termios.TCSANOW, attributes)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))


def prefill_pipe(fd, keep_nonblocking=False):
    """パイプを満杯にする(標準出力が一杯の場面)。書いたバイト数を返す。

    1 バイトも書けなくなるまで、書く大きさを半分にしながら書く。keep_nonblocking が真なら、書く側の
    O_NONBLOCK を残す(子の write は待たずに EAGAIN になる)。
    """
    os.set_blocking(fd, False)
    total = 0
    size = 65536
    while size >= 1:
        try:
            total += os.write(fd, PREFILL_BYTE * size)
        except BlockingIOError:
            size //= 2
    if not keep_nonblocking:
        os.set_blocking(fd, True)
    return total


# write の系の呼出しの番号(/proc/<pid>/syscall の最初の欄)。
WRITE_SYSCALLS = {'x86_64': '1', 'aarch64': '64'}


def stderr_write_blocked(pid):
    """子が fd 2 への write の中で止まっているか(Linux の /proc/<pid>/syscall と wchan)。

    拒否の行のように、WARN を出さずに標準エラーへ書く所で止まったことを確かめる。FIONREAD は先に
    満杯にしたパイプでは書く前から変わらないので使えない。読めなければ失敗にする(Linux だけのケース)。
    """
    number = WRITE_SYSCALLS.get(os.uname().machine)
    if number is None:
        raise Failure(f'write の系の呼出しの番号が分からない機種: {os.uname().machine}')
    try:
        with open(f'/proc/{pid}/syscall') as handle:
            fields = handle.read().split()
        with open(f'/proc/{pid}/wchan') as handle:
            wchan = handle.read().strip()
    except OSError as error:
        raise Failure(f'子の系の呼出しを読めない(/proc/{pid}): {error}') from error
    return fields[:2] == [number, '0x2'] and wchan == 'pipe_write'


def pending_bytes(fd):
    buffer = bytearray(struct.pack('i', 0))
    fcntl.ioctl(fd, termios.FIONREAD, buffer)
    return struct.unpack('i', buffer)[0]


def xlsx_complete(path):
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size < EOCD.size:
                return False
            handle.seek(size - EOCD.size)
            return handle.read(4) == b'PK\x05\x06'
    except OSError:
        return False


def status_of(returncode):
    if returncode < 0:
        return ('signal', signal.Signals(-returncode).name)
    return ('exit', returncode)


def execute(spec):
    """子を 1 回起動して、終了値・出力・送れた信号の回数を集める。"""
    own = []          # 比較器の側で閉じる fd
    sources = [None, None, None]
    readers = {}      # 名前 -> 読む側の fd
    masters = set()   # 疑似端末の master(読む側)
    slaves = []       # 疑似端末の slave(子が終わるまで比較器も開いておく。閉じると未読の出力を捨てる OS がある)
    full = None
    prefill = {}      # 先に満杯にしたパイプ: 名前 -> 書いたバイト数(読んだ後に先頭から除く)
    held = {}         # O_NONBLOCK の満杯のパイプ: 名前 -> 先に書いたバイト数(子が終わるまで読まない)
    streams = {}

    def open_fd(path, flags, mode=0o644):
        fd = os.open(path, flags, mode)
        own.append(fd)
        return fd

    try:
        if spec.stdin == 'devnull':
            sources[0] = open_fd(os.devnull, os.O_RDONLY)
        elif spec.stdin == 'directory':
            sources[0] = open_fd(spec.stream_dir, os.O_RDONLY | os.O_DIRECTORY)
        elif spec.stdin != 'closed':
            raise Failure(f'未知の stdin: {spec.stdin}')
        for index, name, mode in ((1, 'stdout', spec.stdout), (2, 'stderr', spec.stderr)):
            if mode == 'stdout':
                sources[2] = sources[1]
            elif isinstance(mode, dict) and 'pty' in mode:
                master, slave = os.openpty()
                own.append(master)
                slaves.append(slave)
                configure_pty(slave, mode['pty']['columns'], mode['pty']['rows'])
                sources[index] = slave
                readers[name] = master
                masters.add(master)
            elif mode in ('pipe', 'full-pipe', 'prefilled-full-pipe'):
                read_end, write_end = os.pipe()
                own.extend((read_end, write_end))
                sources[index] = write_end
                readers[name] = read_end
                if mode != 'pipe':
                    full = name
                    # 標準出力の full-pipe と prefilled-full-pipe は先に満杯にする(標準エラーの
                    # full-pipe は、子の WARN で満たす)。
                    if name == 'stdout' or mode == 'prefilled-full-pipe':
                        prefill[name] = prefill_pipe(write_end)
            elif mode == 'nonblock-full-pipe':
                read_end, write_end = os.pipe()
                own.extend((read_end, write_end))
                sources[index] = write_end
                readers[name] = read_end
                held[name] = prefill_pipe(write_end, keep_nonblocking=True)
            elif mode == 'closed-pipe':
                read_end, write_end = os.pipe()
                os.close(read_end)
                own.append(write_end)
                sources[index] = write_end
            elif mode == 'dev-full':
                sources[index] = open_fd('/dev/full', os.O_WRONLY)
            elif mode == 'directory':
                sources[index] = open_fd(spec.stream_dir, os.O_RDONLY | os.O_DIRECTORY)
            elif mode == 'file':
                path = spec.stream_dir + b'/' + name.encode()
                sources[index] = open_fd(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                streams[name] = path
            elif mode != 'closed':
                raise Failure(f'未知の {name}: {mode}')
        if any(fd is not None and fd <= 2 for fd in sources):
            raise Failure('比較器の fd 0〜2 が閉じている')
        error_read, error_write = os.pipe()
        own.extend((error_read, error_write))
        config = {
            'error_fd': error_write,
            'umask': spec.umask,
            'rlimit_fsize': spec.rlimit_fsize,
            'sigint_ignored': spec.sigint_ignored,
            'cwd': spec.cwd.hex(),
            'components': [part.hex() for part in spec.components],
            'fds': sources,
            'argv': [value.hex() for value in spec.argv],
            'env': [[key.hex(), value.hex()] for key, value in sorted(spec.env.items())],
        }
        passed = sorted({fd for fd in sources if fd is not None} | {error_write})
        before = time.time()
        started = time.monotonic()
        process = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', LAUNCHER, json.dumps(config)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   pass_fds=passed, start_new_session=True, env={})
        try:
            os.close(error_write)
            own.remove(error_write)
            message = b''
            while True:
                chunk = os.read(error_read, 4096)
                if not chunk:
                    break
                message += chunk
            if message:
                process.wait()
                raise Failure(f'子を起動できない: {show(message)}')
            # 子に渡した fd は、読む側と疑似端末の slave を除いて閉じる(閉じないとパイプの終わりが来ない)。
            keep = set(readers.values()) | set(slaves)
            for fd in list(own):
                if fd in sources and fd not in keep:
                    os.close(fd)
                    own.remove(fd)
            buffers = {name: bytearray() for name in readers}
            selector = selectors.DefaultSelector()
            active = {}

            def register(name):
                fd = readers[name]
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ, name)
                active[name] = fd

            for name in readers:
                if name != full and name not in held:
                    register(name)
            plan = SignalPlan(spec.signals, readers.get(full), spec.output, process.pid) if spec.signals else None
            full_open = full is None
            if plan is None and not full_open:
                register(full)
                full_open = True
            timed_out = False
            exited_at = None
            quiet_since = None
            while True:
                got = False
                for key, _ in selector.select(0.02):
                    name = key.data
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b''
                    if chunk:
                        buffers[name].extend(chunk)
                        got = True
                    else:
                        selector.unregister(key.fd)
                        del active[name]
                now = time.monotonic()
                if plan is not None:
                    plan.step(process, now)
                    if plan.drain and not full_open:
                        register(full)
                        full_open = True
                if process.poll() is not None:
                    if exited_at is None:
                        exited_at = now
                        # 満杯のまま渡したパイプは、子が終わってから読む(先に読むと空きができる)。
                        for name in held:
                            register(name)
                    if not full_open:
                        register(full)
                        full_open = True
                    if not [name for name, fd in active.items() if fd not in masters]:
                        if not any(fd in masters for fd in active.values()):
                            break
                        if got or quiet_since is None:
                            quiet_since = now
                        if now - quiet_since >= 0.2:
                            break
                if now - started > spec.timeout:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    break
            selector.close()
            returncode = process.wait()
            after = time.time()
        except BaseException:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
        stdout = bytes(buffers.get('stdout', b''))
        stderr = bytes(buffers.get('stderr', b''))
        for name, size in [*prefill.items(), *held.items()]:
            data = stdout if name == 'stdout' else stderr
            if not data.startswith(PREFILL_BYTE * size):
                raise Failure(f'満杯にした {name} のパイプの先頭が想定と違う')
            if name == 'stdout':
                stdout = data[size:]
            else:
                stderr = data[size:]
        files = {}
        for name, path in streams.items():
            with open(path, 'rb') as handle:
                files[name] = handle.read()
        return RawResult(status_of(returncode), stdout, stderr, files, before, after, timed_out,
                         plan.sent if plan is not None else 0)
    finally:
        for fd in own + slaves:
            try:
                os.close(fd)
            except OSError:
                pass


class SignalPlan:
    """信号のケースの手順(待つ条件 → 決まった回数と間隔で送る → 読み切る)。"""

    def __init__(self, spec, full_fd, output, pid):
        self.spec = spec
        self.full_fd = full_fd
        self.output = output
        self.pid = pid
        self.phase = 'wait'
        self.started = time.monotonic()
        self.last_check = 0.0
        self.last_value = None
        self.stable = 0
        self.times = []
        self.drain_at = None
        self.sent = 0
        self.next = 0
        self.drain = False
        self.signal = signal.Signals[spec['signal']]

    def condition(self, now):
        if now - self.last_check < 0.05:
            return False
        self.last_check = now
        if self.spec['wait'] == 'xlsx-complete':
            return self.output is not None and xlsx_complete(self.output)
        if self.spec['wait'] == 'pipe-full-stable':
            value = pending_bytes(self.full_fd)
            if value == self.last_value:
                self.stable += 1
            else:
                self.stable = 0
                self.last_value = value
            return self.stable >= 10
        if self.spec['wait'] == 'stderr-write-blocked':
            return self.count_stable(stderr_write_blocked(self.pid))
        raise Failure(f'未知の待つ条件: {self.spec["wait"]}')

    def count_stable(self, holds):
        self.stable = self.stable + 1 if holds else 0
        return self.stable >= 10

    def step(self, process, now):
        if self.phase == 'wait':
            if process.poll() is not None:
                self.phase = 'done'
                self.drain = True
                return
            if now - self.started > WAIT_TIMEOUT:
                raise Failure(f'信号を送る前の条件({self.spec["wait"]})が {WAIT_TIMEOUT:.0f} 秒で満たされない')
            if self.condition(now):
                settle = self.spec.get('settle_ms', 0) / 1000
                interval = self.spec['interval_ms'] / 1000
                self.times = [now + settle + index * interval for index in range(self.spec['count'])]
                self.drain_at = self.times[-1] + interval
                self.phase = 'send'
        if self.phase == 'send':
            while self.next < len(self.times) and now >= self.times[self.next]:
                if process.poll() is None:
                    os.kill(process.pid, self.signal)
                    self.sent += 1
                    self.next += 1
                else:
                    self.next = len(self.times)
            if (self.next >= len(self.times) and now >= self.drain_at) or process.poll() is not None:
                self.phase = 'done'
                self.drain = True


# --- 旧版の読取りの確かめ(zipfile.testzip と openpyxl.load_workbook) ---

LOADER = r'''
import hashlib, json, sys, warnings, zipfile
warnings.simplefilter("ignore")
import openpyxl

def describe(path):
    result = {}
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
        result["testzip"] = "ok" if bad is None else "bad"
    except Exception as error:
        result["testzip"] = "error:" + type(error).__name__
    try:
        book = openpyxl.load_workbook(path)
    except Exception as error:
        result["load"] = "error:" + type(error).__module__ + "." + type(error).__qualname__
        return result
    digest = hashlib.sha256()
    sheets = []
    for sheet in book.worksheets:
        head = [sheet.title, sheet.dimensions, sheet.freeze_panes, sheet.max_row, sheet.max_column,
                sorted([key, dim.width] for key, dim in sheet.column_dimensions.items())]
        digest.update(json.dumps(head).encode())
        sheets.append(head[:3])
        for row in sheet.iter_rows():
            for cell in row:
                digest.update(json.dumps([cell.coordinate, cell.value, cell.data_type, bool(cell.font.b)]).encode())
    result["load"] = "ok"
    result["sheets"] = sheets
    result["digest"] = digest.hexdigest()
    return result

for line in sys.stdin:
    print(json.dumps(describe(bytes.fromhex(json.loads(line)))), flush=True)
'''


class Loader:
    """旧版用の Python で XLSX を開く常駐の子。スレッドごとに 1 つ。"""

    def __init__(self, python):
        self.python = python
        self.local = threading.local()
        self.all = []
        self.lock = threading.Lock()

    def load(self, path):
        process = getattr(self.local, 'process', None)
        if process is None:
            process = subprocess.Popen([self.python, '-I', '-B', '-c', LOADER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env={'PATH': '/usr/bin:/bin'})
            self.local.process = process
            with self.lock:
                self.all.append(process)
        process.stdin.write((json.dumps(path.hex()) + '\n').encode())
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise Failure('XLSX を開く子が止まった(旧版用の Python の openpyxl を確かめる)')
        return json.loads(line)

    def close(self):
        for process in self.all:
            try:
                process.stdin.close()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()


# --- ケースの組み立て ---

def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def to_bytes(value, work):
    if isinstance(value, dict):
        if 'hex' in value:
            return bytes.fromhex(value['hex'])
        raise Failure(f'未知の値の形: {value}')
    return value.encode('utf-8', 'surrogateescape').replace(b'@WORK@', work)


def build_argv(case, work):
    out = []
    for value in case['argv']:
        if isinstance(value, dict) and 'expand' in value:
            spec = value['expand']
            out.extend(spec['format'].format(index=index).encode('utf-8') for index in range(spec['count']))
        else:
            out.append(to_bytes(value, work))
    return out


def output_rel(case):
    argv = case['argv']
    for index, value in enumerate(argv[:-1]):
        if value == '-o' and isinstance(argv[index + 1], str):
            return argv[index + 1].encode('utf-8')
    return None


def apply_setup(place, case, cases_module):
    """置き場の work に setup の項目を作る(place の root は work)。"""
    for op in case['setup']:
        kind = op['op']
        if kind == 'file':
            rel = to_bytes(op['path'], place.root)
            if 'text' in op:
                data = op['text'].encode('utf-8', 'surrogateescape')
            elif 'hex' in op:
                data = bytes.fromhex(op['hex'])
            else:
                data = cases_module.build_content(op['build'])
            place.write(rel, data)
            if 'mode' in op:
                place.chmod(rel, int(op['mode'], 8))
        elif kind == 'dir':
            rel = to_bytes(op['path'], place.root)
            place.mkdirs(rel)
            if 'mode' in op:
                place.chmod(rel, int(op['mode'], 8))
        elif kind == 'symlink':
            place.symlink(to_bytes(op['path'], place.root), to_bytes(op['target'], place.root))
        elif kind == 'symlink_chain':
            prefix = op['prefix'].encode()
            place.symlink(prefix + b'0', op['target'].encode())
            for index in range(1, op['count']):
                place.symlink(prefix + str(index).encode(), prefix + str(index - 1).encode())
        elif kind == 'hardlink':
            os.link(place.root + b'/' + to_bytes(op['target'], place.root), place.root + b'/' + to_bytes(op['path'], place.root))
        elif kind == 'fifo':
            place.mkfifo(to_bytes(op['path'], place.root))
        elif kind == 'chmod':
            # 後片付け(make_removable)が権限を戻してから消す。
            place.chmod(to_bytes(op['path'], place.root), int(op['mode'], 8))
        elif kind == 'deep_dirs':
            place.mkdirs(b'/'.join(part.encode() for part in op['components']))
        elif kind == 'generated':
            for rel, body in cases_module.generated_group(op['seed'], op['group'])['files']:
                place.write(rel.encode('utf-8'), body)
        else:
            raise Failure(f'未知の setup: {kind}')


def build_env(document, case, impl, root, work, variant):
    base = document['base_env']['legacy' if impl == 'legacy' else 'rust']
    env = {}
    for key, value in base.items():
        value = value.replace('<置き場>', os.fsdecode(root))
        env[key.encode()] = value.encode('utf-8', 'surrogateescape')
    change = case.get('legacy_env') if impl == 'legacy' and 'legacy_env' in case else case['env']
    for key, value in change['set'].items():
        if variant == 'normal' and key == 'TMPDIR':
            continue
        env[key.encode()] = to_bytes(value, work)
    for key in change['unset']:
        env.pop(key.encode(), None)
    return env


@dataclasses.dataclass
class Run:
    """1 回の起動の結果(印へ置き換える前)。"""

    status: tuple
    stdout: bytes = b''
    stderr: bytes = b''
    streams: dict = dataclasses.field(default_factory=dict)
    created: list = dataclasses.field(default_factory=list)
    modified: list = dataclasses.field(default_factory=list)
    violations: list = dataclasses.field(default_factory=list)
    contents: dict = dataclasses.field(default_factory=dict)
    loads: dict = dataclasses.field(default_factory=dict)
    root: bytes = b''
    tz: object = 'UTC'
    tmpdir: bytes = ROOT_MARK + b'/tmp'
    before: float = 0.0
    after: float = 0.0
    timed_out: bool = False
    sent: int = 0
    tmp_touched: bool = False
    execve_extra: object = None


class Context:
    def __init__(self, *, fixture, python, paths, document, cases_module, base, fixed_parts, strace, loader, fixed_manifest=None):
        self.fixed_manifest = fixed_manifest
        self.fixture = fixture
        self.python = python
        self.paths = paths
        self.document = document
        self.cases_module = cases_module
        self.base = base
        self.fixed_parts = fixed_parts
        self.strace = strace
        self.loader = loader


def run_side(ctx, case, root, impl, *, variant=None, rlimit=None, strace_log=None):
    """1 つの置き場を作って、旧版か Rust を流す(runs の回数だけ)。"""
    os.makedirs(root)
    for name in LAYOUT:
        os.mkdir(root + b'/' + name)
    work = root + b'/work'
    apply_setup(Place(work), case, ctx.cases_module)
    place = Place(root)
    argv_tail = build_argv(case, work)
    env = build_env(ctx.document, case, impl, root, work, variant)
    normal = variant == 'normal'
    umask = 0o022 if normal else int(case.get('umask', '022'), 8)
    cwd = case.get('cwd', '.')
    components = []
    if isinstance(cwd, dict):
        components = [part.encode() for part in cwd['components']]
        cwd_path = work
    else:
        cwd_path = work if cwd == '.' else work + b'/' + cwd.encode()
    if impl == 'legacy':
        command = [ctx.python, os.fsencode(LEGACY)]
    else:
        command = [ctx.fixture]
        if strace_log is not None:
            command = [ctx.strace, b'-f', b'-e', b'trace=execve', b'-o', strace_log] + command
    tmpdir_value = env.get(b'TMPDIR', root + b'/tmp')
    scratch = {b'tmp'}
    if tmpdir_value.startswith(root + b'/'):
        scratch.add(tmpdir_value[len(root) + 1:])
    output = output_rel(case)
    output_path = (cwd_path + b'/' + output) if output is not None and not output.startswith(b'/') else None
    tz = env.get(b'TZ')
    runs = []
    for _ in range(case.get('runs', 1)):
        before_tree = snapshot(place)
        spec = RunSpec(
            argv=command + argv_tail, cwd=cwd_path, components=components, env=env, umask=umask,
            stdin='devnull' if normal else case.get('stdin', 'devnull'),
            stdout='pipe' if normal else case.get('stdout', 'pipe'),
            stderr='pipe' if normal else case.get('stderr', 'pipe'),
            stream_dir=root, rlimit_fsize=rlimit, sigint_ignored=False if normal else case.get('sigint_ignored', False),
            signals=None if normal else case.get('signals'), output=output_path)
        raw = execute(spec)
        after_tree = snapshot(place)
        created, modified, violations = diff_trees(before_tree, after_tree, scratch)
        tmp_touched = any(before_tree.get(rel) != after_tree.get(rel) for rel in scratch)
        contents = {}
        for rel, kind, mode, _ in created:
            if kind == 'file':
                contents[rel] = read_any(place, rel, mode)
        for rel, mode, _ in modified:
            contents[rel] = read_any(place, rel, mode)
        loads = {}
        for rel, data in contents.items():
            if is_complete_zip(data):
                loads[rel] = load_file(ctx, place, rel, data)
        execve_extra = None
        if strace_log is not None:
            execve_extra = strace_extra(strace_log)
        runs.append(Run(
            status=raw.status, stdout=raw.stdout, stderr=raw.stderr, streams=raw.streams, created=created, modified=modified,
            violations=violations, contents=contents, loads=loads, root=root, tz=None if tz is None else tz.decode(),
            tmpdir=tmpdir_value.replace(root, ROOT_MARK), before=raw.before, after=raw.after, timed_out=raw.timed_out,
            sent=raw.sent, tmp_touched=tmp_touched, execve_extra=execve_extra))
    return runs


def read_any(place, rel, mode):
    try:
        return place.read(rel)
    except PermissionError:
        place.chmod(rel, mode | 0o400)
        try:
            return place.read(rel)
        finally:
            place.chmod(rel, mode)


def load_file(ctx, place, rel, data):
    path = place.root + b'/' + rel
    if len(path) >= PATH_LIMIT or not os.access(path, os.R_OK):
        handle, temporary = tempfile.mkstemp(dir=ctx.base, suffix=b'.xlsx')
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
        try:
            return ctx.loader.load(temporary)
        finally:
            os.unlink(temporary)
    return ctx.loader.load(path)


def strace_extra(path):
    with open(path, 'rb') as handle:
        lines = [line for line in handle.read().splitlines() if b'execve(' in line]
    if not lines:
        raise Failure('strace の記録に execve が 1 行も無い(strace が動いていない)')
    return len(lines) - 1


# --- 比べ方 ---

def declared(case):
    return set(case.get('approved_differences', [])) | set(ALWAYS_APPLIED)


def norm(run, data, paths, diffs):
    return normalize_output(data, run.root, paths, diffs)


def created_view(run):
    return [(rel, kind, mode, None if link is None else link.replace(run.root, ROOT_MARK)) for rel, kind, mode, link in run.created]


def compare_status(old, new):
    return [] if old.status == new.status else [f'終了値が違う: 旧版 {old.status} / Rust {new.status}']


def compare_text(label, old, new):
    return [] if old == new else [f'{label}が違う: {first_difference(old, new)}']


def compare_files(case, old, new, fixed_parts, *, contents=True):
    problems = []
    for side, run in (('旧版', old), ('Rust', new)):
        problems += [f'{side}: {text}' for text in run.violations]
    old_created, new_created = created_view(old), created_view(new)
    if old_created != new_created:
        only_old = [item for item in old_created if item not in new_created]
        only_new = [item for item in new_created if item not in old_created]
        problems.append(f'作ったファイルとディレクトリが違う: 旧版だけ {[(show(r), k, oct(m)) for r, k, m, _ in only_old]} / '
                        f'Rust だけ {[(show(r), k, oct(m)) for r, k, m, _ in only_new]}')
    if old.modified != new.modified:
        problems.append(f'上書きしたファイルの状態が違う: 旧版 {[(show(r), oct(m), s) for r, m, s in old.modified]} / '
                        f'Rust {[(show(r), oct(m), s) for r, m, s in new.modified]}')
    for side, run in (('旧版', old), ('Rust', new)):
        for rel, _, same in run.modified:
            if not same:
                problems.append(f'{side}: 既存の出力先を同じ実体のまま上書きしていない: {show(rel)}')
    if not contents:
        return problems
    for rel in sorted(set(old.contents) | set(new.contents)):
        left, right = old.contents.get(rel), new.contents.get(rel)
        if left is None or right is None:
            continue
        if is_complete_zip(left):
            problems += compare_xlsx(rel, left, right, old, new, fixed_parts)
            if old.loads.get(rel) != new.loads.get(rel):
                problems.append(f'{show(rel)}: zipfile.testzip と openpyxl.load_workbook の結果が違う: '
                                f'旧版 {old.loads.get(rel)} / Rust {new.loads.get(rel)}')
        elif left != right:
            problems.append(f'{show(rel)}: 作ったファイルの中身が違う')
    return problems


def compare_full(case, old, new, paths, fixed_parts, *, sigint=False):
    diffs = declared(case)
    problems = compare_status(old, new)
    problems += compare_text('標準出力', norm(old, old.stdout, paths, diffs), norm(new, new.stdout, paths, diffs))
    old_err, new_err = norm(old, old.stderr, paths, diffs), norm(new, new.stderr, paths, diffs)
    if sigint:
        if last_line(old_err) != last_line(new_err):
            problems.append(f'標準エラーの最後の行が違う: 旧版 {show(last_line(old_err))} / Rust {show(last_line(new_err))}')
    else:
        problems += compare_text('標準エラー', old_err, new_err)
    for name in sorted(set(old.streams) | set(new.streams)):
        problems += compare_text(f'{name} のファイル', norm(old, old.streams.get(name, b''), paths, diffs),
                                 norm(new, new.streams.get(name, b''), paths, diffs))
    if old.sent != new.sent:
        problems.append(f'送れた信号の回数が違う: 旧版 {old.sent} / Rust {new.sent}')
    problems += compare_files(case, old, new, fixed_parts)
    return problems


def compare_partial(case, old, new, paths, fixed_parts):
    """Q1: XLSX の書き込みの途中の失敗。

    区切りの行(`During handling …` と `Exception ignored in: …`)の形は、失敗の位置で変わりうる。
    試す上限(旧版の XLSX の大きさの半分と 0)では、両方とも途中の部分で失敗して形が同じになるので、
    今は形まで比べる(各呼出履歴の前の行と最後の行を比べる)。
    """
    diffs = declared(case)
    old_err, new_err = norm(old, old.stderr, paths, diffs), norm(new, new.stderr, paths, diffs)
    reasons = temp_file_failure(old_err, old.tmpdir)
    if reasons:
        return [f'旧版の失敗が一時ファイルによる({"・".join(reasons)})。Q1 の比べ方は当てない']
    problems = compare_status(old, new)
    if old.stdout or new.stdout:
        problems.append(f'標準出力が空でない: 旧版 {show(old.stdout)} / Rust {show(new.stdout)}')
    if case.get('rlimit_fsize') == 0:
        # 最初の書き出しで失敗する場合は、番地の欄のほかは全部の行を比べる。
        problems += compare_text('標準エラー', old_err, new_err)
        problems += compare_files(case, old, new, fixed_parts)
        return problems
    head = head_through(old_err, WB_SAVE_LINE)
    if head is None:
        problems.append('旧版の呼出履歴に wb.save(checked_out) の行が無い')
    elif not new_err.startswith(head):
        problems.append(f'呼出履歴の先頭から wb.save(checked_out) の行までが違う: {first_difference(head, new_err[:len(head)])}')
    old_blocks, new_blocks = parse_blocks(old_err), parse_blocks(new_err)
    if len(old_blocks) != len(new_blocks):
        problems.append(f'呼出履歴の数が違う: 旧版 {len(old_blocks)} / Rust {len(new_blocks)}')
    for index, (left, right) in enumerate(zip(old_blocks, new_blocks)):
        if left.header != right.header:
            problems.append(f'{index + 1} つ目の呼出履歴の前の行が違う: 旧版 {[show(x) for x in left.header]} / Rust {[show(x) for x in right.header]}')
        if left.exception != right.exception:
            problems.append(f'{index + 1} つ目の呼出履歴の最後の行が違う: 旧版 {[show(x) for x in left.exception]} / Rust {[show(x) for x in right.exception]}')
    if not old_blocks:
        problems.append('旧版に呼出履歴が無い')
    problems += compare_files(case, old, new, fixed_parts, contents=False)
    return problems


def compare_fixed(case, old, new, paths, fixed_parts):
    """Q3: 旧版は成功し Rust だけが失敗する場面。Rust を設計の固定期待と比べる。"""
    expected = case.get('expected_rust') or {}
    problems = []
    if old.status != ('exit', 0):
        problems.append(f'旧版が成功しない(終了値 {old.status})。ケースの作り方の誤り')
    if new.status != ('exit', expected.get('exit', 1)):
        problems.append(f'Rust の終了値が固定期待と違う: {new.status}')
    if new.stdout != expected.get('stdout', '').encode():
        problems.append(f'Rust の標準出力が空でない: {show(new.stdout)}')
    stderr = norm(new, new.stderr, paths, declared(case))
    head = legacy_head()
    if not stderr.startswith(head):
        problems.append(f'Rust の呼出履歴の先頭から wb.save(checked_out) の行までが固定期待と違う: {first_difference(head, stderr[:len(head)])}')
    blocks = parse_blocks(stderr)
    last = expected.get('stderr_last_line', Q3_LAST_LINE.decode()).encode()
    if len(blocks) != 2:
        problems.append(f'Rust の呼出履歴の数が 2 でない: {len(blocks)}')
    else:
        if blocks[0].exception != [last]:
            problems.append(f'Rust の呼出履歴の最後の行が固定期待と違う: {[show(x) for x in blocks[0].exception]}')
        if blocks[1].header != [Q3_SECOND_HEADER]:
            problems.append(f'Rust の 2 つ目の呼出履歴が ZipFile.__del__ の形でない: {[show(x) for x in blocks[1].header]}')
        if blocks[1].exception != [last]:
            problems.append(f'Rust の 2 つ目の呼出履歴の最後の行が固定期待と違う: {[show(x) for x in blocks[1].exception]}')
    for side, run in (('旧版', old), ('Rust', new)):
        problems += [f'{side}: {text}' for text in run.violations]
    old_created = [(rel, kind, mode) for rel, kind, mode, _ in created_view(old)]
    new_created = [(rel, kind, mode) for rel, kind, mode, _ in created_view(new)]
    if old_created != new_created:
        problems.append(f'作ったファイルとディレクトリが違う: 旧版 {old_created} / Rust {new_created}')
    if new.modified:
        problems.append('Rust が既存のファイルを上書きした')
    return problems


def compare_temp_file(case, old, normal, new, paths, fixed_parts):
    """Q4: 旧版の失敗が一時ファイルによる場面。Rust の期待は、同じ入力を通常の条件で流した旧版。

    作るファイルとディレクトリの権限は、そのケースの umask で決まる値(一時ファイルで失敗した旧版が残したもの)。
    """
    problems = []
    modes = {rel: mode for rel, _, mode, _ in old.created}
    expected_created = []
    for rel, kind, mode, link in normal.created:
        if rel not in modes:
            problems.append(f'通常の条件の旧版が作った {show(rel)} を、一時ファイルで失敗した旧版が作っていない')
            continue
        expected_created.append((rel, kind, modes[rel], link))
    if sorted(modes) != sorted(rel for rel, *_ in normal.created):
        problems.append('一時ファイルで失敗した旧版と通常の条件の旧版で、作ったものの一覧が違う')
    expected = dataclasses.replace(normal, created=expected_created)
    problems += compare_full(case, expected, new, paths, fixed_parts)
    if new.tmp_touched:
        problems.append('Rust が TMPDIR に何かを作った(TMPDIR のディレクトリが変わった)')
    return problems


def compare_runs(case, old, new, paths, fixed_parts, *, normal=None):
    """1 組の結果を、ケースが宣言した決定済みの差に従って比べる。不一致の説明の一覧を返す。"""
    diffs = declared(case)
    if old.timed_out or new.timed_out:
        return [f'時間切れ: 旧版 {old.timed_out} / Rust {new.timed_out}']
    if case.get('compare') == 'fixed':
        if 'uncompressed' not in diffs:
            return ['固定期待の比べ方は、決定済みの差 uncompressed の宣言が要る']
        return compare_fixed(case, old, new, paths, fixed_parts)
    if 'temp-file' in diffs:
        reasons = temp_file_failure(norm(old, old.stderr, paths, diffs), old.tmpdir)
        if reasons:
            if normal is None:
                return ['旧版の失敗が一時ファイルによるが、通常の条件の旧版の結果が無い']
            return compare_temp_file(case, old, normal, new, paths, fixed_parts)
    if 'write-partial-failure' in diffs:
        return compare_partial(case, old, new, paths, fixed_parts)
    signals = case.get('signals')
    sigint = bool(signals) and signals['signal'] == 'SIGINT' and 'variable-output' in diffs
    problems = compare_full(case, old, new, paths, fixed_parts, sigint=sigint)
    if 'csv-path' in diffs:
        if b'FALLBACK' in new.stdout:
            problems.append('Rust が FALLBACK を出した')
        if any(rel == b'work/out/book' for rel, *_ in new.created):
            problems.append('Rust が CSV のディレクトリ out/book/ を作った')
    return problems


def expect_problems(case, run, paths):
    expect = case.get('expect_legacy')
    if not expect:
        return []
    problems = []
    if 'exit' in expect and run.status != ('exit', expect['exit']):
        problems.append(f'旧版の終了値が想定(終了 {expect["exit"]})と違う: {run.status}。ケースの作り方の誤り')
    if 'signal' in expect and run.status != ('signal', expect['signal']):
        problems.append(f'旧版が想定の信号({expect["signal"]})で終わらない: {run.status}。ケースの作り方の誤り')
    if 'stderr_contains' in expect and expect['stderr_contains'].encode() not in norm(run, run.stderr, paths, set()):
        problems.append(f'旧版の標準エラーに想定の文({expect["stderr_contains"]})が無い。ケースの作り方の誤り')
    return problems


# --- ケースの実行 ---

@dataclasses.dataclass
class CaseResult:
    id: str
    state: str          # 'match' | 'mismatch' | 'error' | 'skipped'
    problems: list
    seconds: float = 0.0
    skip_reason: str = ''
    partial_skips: list = dataclasses.field(default_factory=list)


def needs_strace(case):
    return any(item.startswith('strace') for item in case.get('requires', []))


def zip_part_size(data, name):
    archive = parse_zip(data)
    for entry in archive['entries']:
        if entry['name'] == name:
            return entry['file_size']
    raise Failure(f'旧版の XLSX に {show(name)} が無い')


def rlimit_for(ctx, case, directory):
    value = case.get('rlimit_fsize')
    if value is None or isinstance(value, int):
        return value, []
    rule = value.get('rule')
    output = b'work/' + output_rel(case)
    legacy = run_side(ctx, case, directory + b'/msl', 'legacy', variant='measure')[0]
    if legacy.status != ('exit', 0) or output not in legacy.contents:
        raise Failure('上限を測る旧版の実行が成功しない')
    size = len(legacy.contents[output])
    sheet = zip_part_size(legacy.contents[output], b'xl/worksheets/sheet1.xml')
    if rule == 'half-legacy-xlsx':
        limit = size // 2
        if limit <= sheet:
            raise Failure(f'上限 {limit} が旧版のシートの XML の大きさ {sheet} 以下')
        return limit, []
    if rule == 'legacy-xlsx-size':
        rust = run_side(ctx, case, directory + b'/msr', 'rust', variant='measure')[0]
        if rust.status != ('exit', 0) or output not in rust.contents:
            return size, [f'上限を測る Rust の実行が成功しない: {rust.status} {show(rust.stderr)}']
        rust_size = len(rust.contents[output])
        limit = size + LEGACY_SIZE_MARGIN
        if not sheet <= limit < rust_size:
            return limit, [f'上限の前提が成り立たない: シートの XML {sheet}・旧版の XLSX {size}(上限 {limit})・Rust の XLSX {rust_size}']
        return limit, []
    raise Failure(f'未知の上限の決め方: {rule}')


def run_case(ctx, index, case):
    started = time.monotonic()
    if case.get('linux_only') and not IS_LINUX:
        return CaseResult(case['id'], 'skipped', [], skip_reason=case.get('linux_only_reason', 'Linux だけのケース'))
    directory = ctx.base + f'/c{index:04d}'.encode()
    os.mkdir(directory)
    partial_skips = []
    try:
        problems = []
        limit, notes = rlimit_for(ctx, case, directory)
        problems += notes
        strace_log = None
        if needs_strace(case):
            if IS_LINUX:
                strace_log = directory + b'/strace-new.txt'
            else:
                partial_skips.append('strace による外部の道具の確かめ(Linux だけ)')
        old_runs = run_side(ctx, case, directory + b'/old', 'legacy', rlimit=limit)
        new_runs = run_side(ctx, case, directory + b'/new', 'rust', rlimit=limit, strace_log=strace_log)
        normal = None
        diffs = declared(case)
        for number, (old, new) in enumerate(zip(old_runs, new_runs), 1):
            prefix = f'{number} 回目: ' if len(old_runs) > 1 else ''
            problems += [prefix + text for text in expect_problems(case, old, ctx.paths)]
            if 'temp-file' in diffs and normal is None and temp_file_failure(norm(old, old.stderr, ctx.paths, diffs), old.tmpdir):
                normal = run_side(ctx, case, directory + b'/nrm', 'legacy', variant='normal')[0]
                if normal.status != ('exit', 0):
                    problems.append(f'通常の条件の旧版が成功しない: {normal.status}')
            problems += [prefix + text for text in compare_runs(case, old, new, ctx.paths, ctx.fixed_parts, normal=normal)]
            if strace_log is not None and new.execve_extra != 0:
                problems.append(f'Rust が外部の道具を起動した(最初の 1 行を除いた execve の行が {new.execve_extra})')
        state = 'mismatch' if problems else 'match'
        return CaseResult(case['id'], state, problems, time.monotonic() - started, partial_skips=partial_skips)
    except Failure as error:
        return CaseResult(case['id'], 'error', [f'想定外の失敗: {error}'], time.monotonic() - started)
    except Exception as error:  # noqa: BLE001 - 想定外の失敗も結果に残し、合格にしない
        return CaseResult(case['id'], 'error', [f'想定外の失敗: {type(error).__name__}: {error}'], time.monotonic() - started)
    finally:
        try:
            remove_tree(directory)
        except OSError:
            pass


def case_weight(case):
    weight = 0
    text = json.dumps(case['setup'])
    for word, value in (('wide_table', 50), ('large_table', 40), ('generated', 20), ('long_cell', 10), ('symlink_chain', 10)):
        if word in text:
            weight += value
    if 'recursion' in case['id']:
        weight += 10
    return -weight


def run_cases(ctx, selected, jobs, progress=None):
    """必須のケースを流す。信号のケースは、CPU を使うほかのケースと競らないよう、最後にまとめて流す。"""
    indexed = list(enumerate(selected))
    ordinary = sorted([item for item in indexed if 'signals' not in item[1]], key=lambda item: case_weight(item[1]))
    timed = [item for item in indexed if 'signals' in item[1]]
    results = {}
    for group, width in ((ordinary, jobs), (timed, jobs if jobs == 1 else max(jobs, SIGNAL_JOBS))):
        if not group:
            continue
        with concurrent.futures.ThreadPoolExecutor(max_workers=width) as pool:
            futures = {pool.submit(run_case, ctx, index, case): case['id'] for index, case in group}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results[result.id] = result
                if progress is not None:
                    progress(result)
    return [results.get(case['id']) for case in selected]


# --- 固定期待(Task 1.1 の代表の 5 ケース) ---

def run_fixed_expected(ctx, record):
    """Rust を固定期待の 1 ケースで流して、manifest.json と展開した各部分のバイトに比べる。"""
    directory = ctx.base + b'/fixed-' + record['id'].encode()
    root = directory + b'/new'
    os.makedirs(root)
    for name in LAYOUT:
        os.mkdir(root + b'/' + name)
    work = root + b'/work'
    place = Place(work)
    try:
        for entry in record['files']:
            rel = bytes.fromhex(entry['path_hex']) if 'path_hex' in entry else entry['path'].encode()
            place.write(rel, entry['text'].encode('utf-8'))
        manifest = ctx.fixed_manifest
        env = {}
        for key, value in manifest['run']['env'].items():
            if key == 'PYTHONUTF8':
                continue
            env[key.encode()] = value.replace('<置き場>', os.fsdecode(root)).encode()
        argv = [to_bytes(value, work) for value in record['argv']]
        before_tree = snapshot(Place(root))
        raw = execute(RunSpec(argv=[ctx.fixture] + argv, cwd=work, components=[], env=env, umask=int(manifest['run']['umask'], 8),
                              stdin='devnull', stdout='pipe', stderr='pipe', stream_dir=root))
        after_tree = snapshot(Place(root))
        created, _, violations = diff_trees(before_tree, after_tree, {b'tmp'})
        problems = list(violations)
        run = Run(status=raw.status, root=root, tz=env.get(b'TZ', b'UTC').decode(), before=raw.before, after=raw.after)
        if raw.timed_out:
            return ['時間切れ']
        if raw.status != ('exit', record['exit_code']):
            problems.append(f'終了値が違う: {raw.status}')
        for name, data in (('stdout', raw.stdout), ('stderr', raw.stderr)):
            expected = record[name]
            expected = expected['text'].encode('utf-8') if 'text' in expected else bytes.fromhex(expected['hex'])
            got = normalize_runtime(data.replace(work, b'@WORK@'), ctx.paths)
            if got != expected:
                problems.append(f'{name} が違う: {first_difference(expected, got)}')
        got_created = [{'path': os.fsdecode(rel[len(b'work/'):]), 'type': kind, 'mode': f'0o{mode:o}'} for rel, kind, mode, _ in created]
        if got_created != record['created']:
            problems.append(f'作ったものが違う: {got_created}')
        output = b'work/' + record['output'].encode()
        try:
            data = Place(root).read(output)
            archive = parse_zip(data)
        except (OSError, ZipFormatError, struct.error) as error:
            return problems + [f'XLSX を読めない: {error}']
        problems += xlsx_side_problems(archive, run, 'Rust', ctx.fixed_parts)
        parts = archive['entries']
        if [entry['name'].decode('utf-8', 'surrogateescape') for entry in parts] != [part['name'] for part in record['parts']]:
            return problems + ['部分の順か名前が違う']
        for entry, part in zip(parts, record['parts']):
            content = entry['content']
            if part['marked']:
                content, extra = mark_core(content, run, 'Rust')
                problems += extra
            else:
                if f'{entry["crc"]:08x}' != part['crc32']:
                    problems.append(f'{part["name"]} の CRC-32 が違う')
            external = (f'0o{entry["external_attr"] >> 16:o}<<16' if entry['external_attr'] & 0xFFFF == 0 else hex(entry['external_attr']))
            fields = {
                'file_size': entry['file_size'], 'compress_type': entry['method'], 'create_system': entry['create_system'],
                'create_version': entry['create_version'], 'extract_version': entry['extract_version'], 'flag_bits': entry['flag_bits'],
                'volume': entry['disk_start'], 'internal_attr': entry['internal_attr'], 'external_attr': external,
                'extra': entry['extra'].hex(), 'comment': entry['comment'].hex(),
            }
            for key, value in fields.items():
                if part[key] != value:
                    problems.append(f'{part["name"]} の {key} が違う: 固定期待 {part[key]!r} / Rust {value!r}')
            if hashlib.sha256(content).hexdigest() != part['sha256']:
                problems.append(f'{part["name"]} の展開後のバイトが固定期待と違う')
            stored = (EXPECTED_DIR / record['id'] / part['name']).read_bytes()
            if stored != content:
                problems.append(f'{part["name"]} が固定期待のファイルと違う')
        return problems
    finally:
        remove_tree(directory)


# --- 前提の確かめ ---

PROBE = r'''
import importlib.util, json, os, sys
info = {
    "implementation": sys.implementation.name,
    "version": list(sys.version_info[:3]),
    "stdlib": os.path.dirname(os.__file__),
    "lxml": importlib.util.find_spec("lxml") is not None,
}
try:
    import openpyxl, et_xmlfile
    from openpyxl.xml import LXML
except ImportError as error:
    info["import_error"] = str(error)
else:
    info["openpyxl"] = openpyxl.__version__
    info["et_xmlfile"] = et_xmlfile.__version__
    info["openpyxl_lxml"] = bool(LXML)
    info["site_packages"] = os.path.dirname(os.path.dirname(openpyxl.__file__))
print(json.dumps(info))
'''


def legacy_problems(info):
    """旧版用の Python が CPython 3.10.11・openpyxl 3.1.5・et_xmlfile 2.0.0・lxml なしかを確かめる。"""
    problems = []
    if info.get('implementation') != 'cpython' or info.get('version') != [3, 10, 11]:
        problems.append(f'CPython 3.10.11 でない: {info.get("implementation")} {info.get("version")}')
    if 'import_error' in info:
        problems.append(f'openpyxl を読み込めない: {info["import_error"]}')
    else:
        if info.get('openpyxl') != '3.1.5':
            problems.append(f'openpyxl が 3.1.5 でない: {info.get("openpyxl")}')
        if info.get('et_xmlfile') != '2.0.0':
            problems.append(f'et_xmlfile が 2.0.0 でない: {info.get("et_xmlfile")}')
        if info.get('openpyxl_lxml'):
            problems.append('openpyxl が lxml を使う')
        if not info.get('site_packages'):
            problems.append('site-packages の場所が分からない')
    if info.get('lxml', True):
        problems.append('lxml が読み込める')
    if not info.get('stdlib'):
        problems.append('標準ライブラリの場所が分からない')
    return problems


def probe_legacy(python):
    result = subprocess.run([python, '-I', '-B', '-c', PROBE], stdin=subprocess.DEVNULL, capture_output=True,
                            env={'PATH': '/usr/bin:/bin'}, timeout=120, check=False)
    if result.returncode != 0:
        raise Failure(f'旧版用の Python を調べられない(終了 {result.returncode}): {show(result.stderr)}')
    try:
        info = json.loads(result.stdout)
    except ValueError as error:
        raise Failure('旧版用の Python の情報を読めない') from error
    problems = legacy_problems(info)
    if problems:
        raise Failure('旧版の環境が違う: ' + ' / '.join(problems))
    return RuntimePaths(script=os.fsencode(LEGACY), stdlib=os.fsencode(info['stdlib']), site=os.fsencode(info['site_packages']))


def executable_path(value, label):
    """引数の省略・空・相対・不在・通常ファイル以外・実行不能を拒む(PATH や配布版へ切り替えない)。"""
    if value is None or value == '':
        raise Failure(f'{label} が空')
    if not os.path.isabs(value):
        raise Failure(f'{label} は絶対パスで指定する(PATH から探さない): {value}')
    try:
        info = os.stat(value)
    except OSError as error:
        raise Failure(f'{label} を調べられない: {value}: {error.strerror}') from error
    if not stat.S_ISREG(info.st_mode):
        raise Failure(f'{label} が通常ファイルでない: {value}')
    if not os.access(value, os.X_OK):
        raise Failure(f'{label} が実行できない: {value}')
    return os.fsencode(value)


def validate_cases(document, legacy_sha256):
    """cases.json の形と、決定済みの差の名前を確かめる。"""
    problems = []
    if document.get('schema_version') != 1 or document.get('issue') != 482:
        problems.append('cases.json の版か Issue の番号が違う')
    if document.get('legacy_script', {}).get('sha256') != legacy_sha256:
        problems.append('cases.json を作ったときの旧版のスクリプトと、今の旧版のスクリプトが違う')
    if set(document.get('approved_differences', {})) != set(APPROVED_DIFFERENCES):
        problems.append(f'cases.json の決定済みの差の名前が、比較器の一覧と違う: {sorted(document.get("approved_differences", {}))}')
    if list(document.get('always_applied', [])) != list(ALWAYS_APPLIED):
        problems.append('cases.json のいつも当てる差が、比較器の一覧と違う')
    cases = document.get('cases', [])
    ids = [case.get('id') for case in cases]
    if len(ids) != len(set(ids)):
        problems.append('ケースの ID が重なっている')
    if document.get('case_count') != len(cases) or not cases:
        problems.append(f'ケースの数が合わない: case_count {document.get("case_count")} / 一覧 {len(cases)}')
    counts = {}
    for case in cases:
        counts[case.get('category')] = counts.get(case.get('category'), 0) + 1
        names = case.get('approved_differences', [])
        unknown = [name for name in names if name not in APPROVED_DIFFERENCES]
        if unknown:
            problems.append(f'{case.get("id")}: 未知の決定済みの差: {unknown}')
        if any(name in OUT_OF_SCOPE for name in names):
            problems.append(f'{case.get("id")}: 比較の前提の外の差をケースが宣言した')
        if 'legacy_env' in case and not ({'encoding-settings', 'csv-path'} & set(names)):
            problems.append(f'{case.get("id")}: 旧版だけ環境を変えるのに、決定済みの差の宣言が無い')
        if case.get('compare') == 'fixed' and 'uncompressed' not in names:
            problems.append(f'{case.get("id")}: 固定期待の比べ方に uncompressed の宣言が無い')
        if 'write-partial-failure' in names and case.get('rlimit_fsize') is None:
            problems.append(f'{case.get("id")}: Q1 の比べ方に書き込みの上限が無い')
        for name in ('stdout', 'stderr'):
            mode = case.get(name, 'pipe')
            if not (mode in STREAM_MODES or (isinstance(mode, dict) and 'pty' in mode) or (name == 'stderr' and mode == 'stdout')):
                problems.append(f'{case.get("id")}: 未知の {name}: {mode}')
    if counts != document.get('counts'):
        problems.append('分類ごとの数が counts と合わない')
    missing = [name for name in CATEGORIES if not counts.get(name)]
    if missing or set(counts) - set(CATEGORIES):
        problems.append(f'必須の分類が欠けているか、未知の分類がある: 欠け {missing} / 未知 {sorted(set(counts) - set(CATEGORIES))}')
    generated = document.get('generated', {})
    if generated.get('inputs', 0) < 300:
        problems.append('生成したケースの入力が 300 件未満')
    return problems


def run_generators(python, parent):
    """3 つの生成器の --check を最初に始める。

    generate_cases と generate_expected はすぐ終わるので、ケースを流す前に待つ(wait_generators)。
    generate_unicode13 は 1 本の CPU で数十秒かかるので、ケースと並べて流し、結果をまとめる前に待つ。
    どれが失敗しても合格にしない。
    """
    commands = [
        ('generate_unicode13.py --check', [python, b'-I', b'-B', os.fsencode(FIXTURES / 'generate_unicode13.py'), b'--check']),
        ('generate_cases.py --check', [os.fsencode(sys.executable), b'-B', os.fsencode(FIXTURES / 'generate_cases.py'), b'--check']),
        ('generate_expected.py --check', [os.fsencode(sys.executable), b'-B', os.fsencode(FIXTURES / 'generate_expected.py'),
                                          b'--legacy-python', python, b'--check']),
    ]
    processes = []
    for label, command in commands:
        processes.append((label, subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE, env={'PATH': '/usr/bin:/bin', 'TMPDIR': parent})))
    return processes


def wait_generators(processes):
    lines = []
    for label, process in processes:
        try:
            stdout, stderr = process.communicate(timeout=GENERATOR_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise Failure(f'{label} が時間切れ')
        if process.returncode != 0:
            raise Failure(f'{label} が失敗した(終了 {process.returncode}): {show(stderr)}')
        lines.append(f'{label}: {show(stdout.strip())}')
    return lines


def require_strace(base):
    """Linux では strace を必須にする(無い・動かないなら失敗)。"""
    found = None
    for directory in os.environ.get('PATH', '/usr/bin:/bin').split(os.pathsep) + ['/usr/bin', '/bin']:
        candidate = os.path.join(directory, 'strace')
        if directory and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            found = os.fsencode(os.path.abspath(candidate))
            break
    if found is None:
        raise Failure('strace が無い(Linux では必須。例: sudo apt-get install -y strace)')
    log = base + b'/strace-probe.txt'
    result = subprocess.run([found, b'-f', b'-e', b'trace=execve', b'-o', log, b'/bin/true'], capture_output=True, timeout=60, check=False)
    if result.returncode != 0:
        raise Failure(f'strace が動かない(ptrace が禁止された容器など): {show(result.stderr)}')
    if strace_extra(log) != 0:
        raise Failure('strace の確かめで、/bin/true の execve が 1 行でない')
    os.unlink(log)
    return found


def startup_problems(raw, closed):
    """起動前の記録の 1 回の結果を確かめる。closed は起動の前に閉じた fd(1・2)か None。"""
    output = raw.stderr if closed == 1 else raw.stdout
    if raw.status != ('exit', 0) or raw.timed_out:
        return [f'終了値が 0 でない: {raw.status}: {show(raw.stderr)}']
    match = STARTUP_RECORD.fullmatch(output)
    if match is None:
        return [f'記録の行の形が違う(main の前の関数が呼ばれていない): {show(output)}']
    problems = []
    for fd in (0, 1, 2):
        value = match.group(f'fd{fd}')
        if fd == closed:
            if value != CLOSED_RECORD:
                problems.append(f'起動の前に閉じた fd {fd} が {CLOSED_RECORD.decode()} と記録されていない: {show(value)}')
        elif not value.endswith(b'/valid'):
            problems.append(f'開いていた fd {fd} が使えると記録されていない: {show(value)}')
    return problems


def scope_names(tree, line):
    """その行を含む関数・クラス・無名の範囲の名前(Python の co_name)を内側から順に。"""
    found = []
    for node in ast.walk(tree):
        kind = type(node).__name__
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif kind in ANONYMOUS_SCOPES:
            name = ANONYMOUS_SCOPES[kind]
        else:
            continue
        if node.lineno <= line <= node.end_lineno:
            found.append(((node.lineno, node.col_offset), name))
    found.sort(reverse=True)
    return [name for _, name in found] + ['<module>']


def frame_problems(frame, paths, cache):
    """段の表の 1 段(固定名・行番号・関数・ソースの行)を、旧版の環境の実物と照らす。"""
    try:
        name, line, function, source = frame['file'], frame['line'], frame['function'], frame['source']
    except (KeyError, TypeError):
        return [f'段の形が違う: {frame!r}']
    label = f'{name}:{line} {function}'
    if not (isinstance(name, str) and isinstance(line, int) and isinstance(function, str) and isinstance(source, str)):
        return [f'{label}: 段の値の型が違う']
    parts = name.split('/')
    if name == SCRIPT_NAME.decode():
        path = os.fsencode(LEGACY)
    elif name.startswith('/') or '..' in parts or '' in parts:
        return [f'{label}: 固定名が標準ライブラリか site-packages からの相対でない']
    else:
        path = next((prefix + b'/' + os.fsencode(name) for prefix in (paths.stdlib, paths.site)
                     if os.path.isfile(prefix + b'/' + os.fsencode(name))), None)
        if path is None:
            return [f'{label}: 旧版の環境にファイルが無い']
    if path not in cache:
        text = Path(os.fsdecode(path)).read_text(encoding='utf-8')
        cache[path] = (text.split('\n'), ast.parse(text))
    lines, tree = cache[path]
    if not 1 <= line <= len(lines):
        return [f'{label}: 行番号がファイルの外']
    problems = []
    if lines[line - 1] != source:
        problems.append(f'{label}: ソースの行が違う: 実物 {lines[line - 1]!r} / 表 {source!r}')
    scopes = scope_names(tree, line)
    # 無名の範囲(内包表記・lambda)の行は、それを含む範囲の段(最初の反復の評価や呼出し)でもある。
    allowed = scopes[:2] if scopes[0] in ANONYMOUS_SCOPES.values() else scopes[:1]
    if function not in allowed:
        problems.append(f'{label}: 関数の名前が違う: 実物 {allowed}')
    return problems


def fixture_preflight(fixture, paths, base):
    """検査入口の隠しの切り替えで、起動前の記録と呼出履歴の段の表を確かめる(比較の前に 1 回)。

    - 起動前の記録(Task 1.2 の門): main の前に呼ばれる関数が、ふつうの起動・fd 1 を閉じた起動・fd 2 を
      閉じた起動の状態を記録する(Rust の標準ライブラリが /dev/null で埋める前の値)。
    - 段の表: すべての段の固定名・行番号・関数の名前・ソースの行が、旧版の環境(旧スクリプト・標準ライブラリ・
      site-packages)の実物と同じ。比較のケースが通らない段(時計の端・信号の所など)も確かめる。
    """
    problems = []
    for closed, mode in ((None, b'1'), (1, b'stderr'), (2, b'1')):
        spec = RunSpec(argv=[fixture], cwd=base, components=[], env={b'PATH': b'/usr/bin:/bin', STARTUP_PROBE_ENV: mode},
                       umask=0o022, stdin='devnull', stdout='closed' if closed == 1 else 'pipe',
                       stderr='closed' if closed == 2 else 'pipe', stream_dir=base, timeout=WAIT_TIMEOUT)
        problems += [f'起動前の記録(fd {closed} を閉じた起動): {text}' if closed else f'起動前の記録: {text}'
                     for text in startup_problems(execute(spec), closed)]
    result = subprocess.run([fixture], stdin=subprocess.DEVNULL, capture_output=True, timeout=WAIT_TIMEOUT, check=False,
                            env={'PATH': '/usr/bin:/bin', os.fsdecode(FRAMES_PROBE_ENV): '1'})
    if result.returncode != 0:
        problems.append(f'段の表を出せない(終了 {result.returncode}): {show(result.stderr)}')
    count = 0
    cache = {}
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        count += 1
        try:
            frame = json.loads(line)
        except ValueError:
            problems.append(f'段の表の行を読めない: {show(line)}')
            continue
        problems += [f'段の表: {text}' for text in frame_problems(frame, paths, cache)]
    if result.returncode == 0 and count == 0:
        problems.append('段の表が 0 件')
    if problems:
        raise Failure('検査入口の事前の確かめ: ' + ' / '.join(problems[:20]))
    return count


def c_utf8_preflight(ctx):
    """Linux の LC_ALL=C.UTF-8(PYTHONUTF8 なし)の旧版が、PYTHONUTF8=1 の旧版と同じ結果になること。"""
    table = ctx.cases_module.TABLE
    scenarios = [
        {'id': 'preflight/c-utf8-warn-ok', 'setup': [{'op': 'file', 'path': 'doc/日本語.md', 'text': table}],
         'argv': ['doc/日本語.md', 'doc/無い.md', '-o', {'hex': b'out/\xff\xe8\xa1\xa8.xlsx'.hex()}]},
        {'id': 'preflight/c-utf8-traceback', 'setup': [{'op': 'file', 'path': 'doc/誤り.md', 'hex': b'\xff'.hex()}],
         'argv': ['doc/誤り.md', '-o', 'out/book.xlsx']},
    ]
    problems = []
    for index, scenario in enumerate(scenarios):
        base_case = {**scenario, 'category': 'env', 'env': {'set': {}, 'unset': []}, 'approved_differences': []}
        variant = {**base_case, 'legacy_env': {'set': {'LC_ALL': 'C.UTF-8'}, 'unset': ['PYTHONUTF8']}}
        directory = ctx.base + f'/pre{index}'.encode()
        os.mkdir(directory)
        try:
            left = run_side(ctx, base_case, directory + b'/old', 'legacy')[0]
            right = run_side(ctx, variant, directory + b'/new', 'legacy')[0]
            problems += [f'{scenario["id"]}: {text}' for text in compare_runs(base_case, left, right, ctx.paths, ctx.fixed_parts)]
        finally:
            remove_tree(directory)
    if problems:
        raise Failure('LC_ALL=C.UTF-8 の旧版が PYTHONUTF8=1 の旧版と違う: ' + ' / '.join(problems))


def selftests():
    path = Path(__file__).with_name('test_r4e.py')
    if not path.is_file():
        raise Failure('比較器自身の検査(test_r4e.py)が無い')
    module = load_module(path, 'r4e_comparator_selftests')
    module.subject = sys.modules[__name__]
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if suite.countTestCases() == 0:
        raise Failure('比較器自身の検査が 0 件')
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    if not result.wasSuccessful() or result.skipped:
        raise Failure('比較器自身の検査が不合格または未実施')


def require_runtime():
    if sys.version_info[:2] != (3, 10):
        raise Failure(f'比較は Python 3.10 系で実行する: {sys.version.split()[0]}')
    if not hasattr(os, 'geteuid') or os.geteuid() == 0:
        raise Failure('比較は root 以外の通常の利用者で実行する')


def temp_parent(value):
    """一時の置き場を作る親。引数 → 環境変数 R4E_TEMP_PARENT → tempfile の既定の順。"""
    if value is None:
        value = os.environ.get(TEMP_PARENT_ENV) or tempfile.gettempdir()
    if not os.path.isabs(value) or not os.path.isdir(value):
        raise Failure(f'一時の置き場の親が、絶対パスの既存のディレクトリでない: {value}')
    return os.path.realpath(value)


def inherited_limits():
    """受け継いだ資源の上限(soft/hard)。比較器は RLIMIT_FSIZE のほかを変えない。"""
    def text(value):
        return 'unlimited' if value == resource.RLIM_INFINITY else str(value)
    parts = []
    for name in RECORDED_LIMITS:
        soft, hard = resource.getrlimit(getattr(resource, name))
        parts.append(f'{name}={text(soft)}/{text(hard)}')
    return '・'.join(parts)


def default_jobs():
    if hasattr(os, 'sched_getaffinity'):
        return max(1, len(os.sched_getaffinity(0)))
    return max(1, os.cpu_count() or 1)


# --- 結果のまとめ ---

def summarize(results, selected, *, partial, seconds, skips, fixed=(0, 0)):
    """結果の行と終了値を返す。0 件・未実行・想定外の失敗・不一致を合格にしない。"""
    lines = []
    executed = mismatched = errors = 0
    skipped = list(skips)
    if len(results) != len(selected):
        return EXIT_FAIL, [f'不合格: 結果の数がケースの数と合わない(未実行がある)/ {seconds:.0f} 秒']
    missing = []
    for case, result in zip(selected, results):
        if result is None or result.id != case['id']:
            missing.append(case['id'])
            continue
        if result.state == 'skipped':
            skipped.append(f'{result.id}: {result.skip_reason}')
            continue
        executed += 1
        skipped += [f'{result.id}: {text}' for text in result.partial_skips]
        if result.state == 'mismatch':
            mismatched += 1
        elif result.state == 'error':
            errors += 1
        elif result.state != 'match':
            errors += 1
        for text in result.problems:
            lines.append(f'不一致: {result.id}: {text}')
    fixed_done, fixed_failed = fixed
    for text in skipped:
        lines.append(f'未実施: {text}')
    total = executed + fixed_done
    if missing:
        lines.append(f'不合格: 未実行のケースが {len(missing)} 件(最初 {missing[0]})/ {seconds:.0f} 秒')
        return EXIT_FAIL, lines
    if executed == 0:
        lines.append(f'不合格: 流したケースが 0 件(未実施 {len(skipped)} 件)/ {seconds:.0f} 秒')
        return EXIT_FAIL, lines
    if mismatched or errors or fixed_failed:
        lines.append(f'不合格: 不一致 {mismatched} 件・想定外の失敗 {errors} 件・固定期待の不一致 {fixed_failed} 件'
                     f'(流した {total} 件・未実施 {len(skipped)} 件)/ {seconds:.0f} 秒')
        return EXIT_FAIL, lines
    if partial:
        lines.append(f'一部だけ: {total} 件が一致(未実施 {len(skipped)} 件。--only を使った回は合格にしない)/ {seconds:.0f} 秒')
        return EXIT_PARTIAL, lines
    lines.append(f'合格: {total} 件(未実施 {len(skipped)} 件)/ {seconds:.0f} 秒')
    return EXIT_PASS, lines


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description='R4E(#482)の旧版と Rust の比較')
    parser.add_argument('--fixture', required=True, help='今回ビルドした r4e_contract の絶対パス')
    parser.add_argument('--legacy-python', required=True, help='旧版用の venv の python の絶対パス(CPython 3.10.11)')
    parser.add_argument('--only', help='流すケースの ID の正規表現(使った回は合格にしない)')
    parser.add_argument('--jobs', type=int, help='同時に流すケースの数(既定は使える CPU の数)')
    parser.add_argument('--temp-parent', help=f'一時の置き場を作る親(絶対パス。既定は環境変数 {TEMP_PARENT_ENV}、無ければ tempfile の既定)')
    return parser.parse_args(argv)


def main(argv=None):
    started = time.monotonic()
    args = parse_arguments(argv)
    generators = []
    base = None
    loader = None
    try:
        fixture = executable_path(args.fixture, '--fixture')
        python = executable_path(args.legacy_python, '--legacy-python')
        if args.jobs is not None and args.jobs < 1:
            raise Failure('--jobs は 1 以上')
        try:
            only = re.compile(args.only) if args.only is not None else None
        except re.error as error:
            raise Failure(f'--only の正規表現が正しくない: {error}') from error
        jobs = args.jobs or default_jobs()
        parent = temp_parent(args.temp_parent)
        os.environ[TEMP_PARENT_ENV] = parent  # 比較器自身の検査(test_r4e.py)も同じ親を使う
        limits = inherited_limits()
        require_runtime()
        os.umask(0o022)
        # 比較器が SIGINT を無視した状態で起動されても、子へは既定の扱いを渡す(起動の部品でも既定へ戻す)。
        signal.signal(signal.SIGINT, signal.default_int_handler)
        selftests()
        paths = probe_legacy(python)
        generators = run_generators(python, parent)
        generator_lines = wait_generators(generators[1:])
        generators = generators[:1]
        document = json.loads(CASES_JSON.read_text(encoding='utf-8'))
        legacy_sha = hashlib.sha256(LEGACY.read_bytes()).hexdigest()
        problems = validate_cases(document, legacy_sha)
        if problems:
            raise Failure('cases.json: ' + ' / '.join(problems))
        manifest = json.loads(MANIFEST_JSON.read_text(encoding='utf-8'))
        fixed_parts = {name.encode(): value for name, value in manifest['fixed_parts_sha256'].items()}
        legacy_head()
        base = os.fsencode(os.path.realpath(tempfile.mkdtemp(prefix='r4e-check-', dir=parent)))
        if not re.fullmatch(rb'[A-Za-z0-9/._-]+', base):
            raise Failure(f'一時の置き場のパスに想定外の文字がある: {show(base)}')
        strace = require_strace(base) if IS_LINUX else None
        frame_count = fixture_preflight(fixture, paths, base)
        loader = Loader(python)
        cases_module = load_module(FIXTURES / 'generate_cases.py', 'r4e_generate_cases')
        ctx = Context(fixture=fixture, python=python, paths=paths, document=document, cases_module=cases_module, base=base,
                      fixed_parts=fixed_parts, strace=strace, loader=loader, fixed_manifest=manifest)
        skips = []
        if IS_LINUX:
            c_utf8_preflight(ctx)
        else:
            skips.append('LC_ALL=C.UTF-8 の事前の確かめ(Linux だけ)')
        selected = [case for case in document['cases'] if only is None or only.search(case['id'])]
        fixed_done = fixed_failed = 0
        fixed_lines = []
        for record in manifest['cases']:
            identifier = 'fixed/' + record['id']
            if only is not None and not only.search(identifier):
                continue
            if record['linux_only'] and not IS_LINUX:
                skips.append(f'{identifier}: Linux だけの固定期待')
                continue
            problems = run_fixed_expected(ctx, record)
            fixed_done += 1
            if problems:
                fixed_failed += 1
                fixed_lines += [f'不一致: {identifier}: {text}' for text in problems]
        if only is not None and not selected and fixed_done == 0:
            raise Failure('--only に当たるケースが 0 件')
        done = [0]

        def progress(result):
            done[0] += 1
            if done[0] % 100 == 0:
                print(f'経過: {done[0]} / {len(selected)} 件({time.monotonic() - started:.0f} 秒)', file=sys.stderr, flush=True)

        results = run_cases(ctx, selected, jobs, progress)
        generator_lines = wait_generators(generators) + generator_lines
        generators = []
        seconds = time.monotonic() - started
        code, lines = summarize(results, selected, partial=only is not None, seconds=seconds, skips=skips,
                                fixed=(fixed_done, fixed_failed))
        for line in generator_lines:
            print(f'生成器: {line}')
        print(f'固定期待: {fixed_done} 件を流し、{fixed_failed} 件が不一致')
        print(f'検査入口: 起動前の記録 3 通りと、呼出履歴の段の表 {frame_count} 段が一致')
        print(f'旧版: CPython 3.10.11・openpyxl 3.1.5・et_xmlfile 2.0.0・lxml なし / OS: {sys.platform} / 並列 {jobs}')
        print(f'資源の上限(受け継いだ値。比較の前提の外): {limits}')
        for line in fixed_lines + lines:
            print(line)
        return code
    except Failure as error:
        print(f'不合格: {error} / {time.monotonic() - started:.0f} 秒')
        return EXIT_FAIL
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'不合格: 想定外の失敗: {type(error).__name__}: {error} / {time.monotonic() - started:.0f} 秒')
        return EXIT_FAIL
    finally:
        for _, process in generators:
            if process.poll() is None:
                process.kill()
            process.communicate()
        if loader is not None:
            loader.close()
        if base is not None:
            try:
                remove_tree(base)
            except OSError as error:
                print(f'警告: 一時の置き場を消せない: {show(base)}: {error}', file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
