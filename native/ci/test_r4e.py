#!/usr/bin/env python3
"""R4E(#482)の比較器(check_r4e.py)自身の検査。

壊した結果・宣言の無い差・対象外の差(利用者が渡したパス・入力本文の偽の File の行)・0 件・未実行・
旧版の環境の違い・起動前の記録の無い検査入口・旧版の実物と違う呼出履歴の段の表を、比較器が合格にしないことを確かめる。

使い方(リポジトリの直下で): python3 -B -m unittest discover -s native/ci -p 'test_r4e.py'

Q4 の見分けの入力は、旧版(本家 CPython 3.10.11・openpyxl 3.1.5・et_xmlfile 2.0.0・lxml なし)の実際の
標準エラーを、比較器の正規化(置き場の印・実行環境のパス)に通したもの。2026-11-12 に hp-pc で取った。
- 2 つ目の呼出履歴が get_stream: 3000 行の表・RLIMIT_FSIZE=20000(シートの一時ファイルが先に上限に当たる)。
- 2 つ目の呼出履歴が無い: umask 277・477(出力先の親あり)。
- No usable temporary directory found: 利用者の名前空間(unshare -rm)で /tmp・/var/tmp・TMPDIR を読み取り専用の
  tmpfs にし、現在のディレクトリを読み取り専用にして出した(比較器の CI では作れないので、ここに入力として置く)。
- 2 つ目の呼出履歴が ZipFile.__del__(Q1。一時ファイルによらない): 3 行の表・RLIMIT_FSIZE=2489(旧版の XLSX 4978 の半分)と 0。
"""

import dataclasses
import errno
import importlib.util
import json
from pathlib import Path
import stat
import struct
import sys
import time
import unittest
import zlib

subject = None
HERE = Path(__file__).resolve().parent
EXPECTED = HERE.parent / 'fixtures' / 'r4e' / 'expected'
T0 = 1_700_000_000

HEAD = (b'Traceback (most recent call last):\n'
        b'  File "md_tables_to_xlsx.py", line 241, in <module>\n    sys.exit(main())\n'
        b'  File "md_tables_to_xlsx.py", line 233, in main\n    write_xlsx(tables, out, inputs=inputs, source_roots=args.source_root)\n'
        b'  File "md_tables_to_xlsx.py", line 196, in write_xlsx\n    wb.save(checked_out)\n'
        b'  File "openpyxl/workbook/workbook.py", line 386, in save\n    save_workbook(self, filename)\n'
        b'  File "openpyxl/writer/excel.py", line 294, in save_workbook\n    writer.save()\n'
        b'  File "openpyxl/writer/excel.py", line 275, in save\n    self.write_data()\n')
ZIP_DEL_TAIL = (b'Exception ignored in: <function ZipFile.__del__ at 0x@ADDRESS@>\nTraceback (most recent call last):\n'
                b'  File "zipfile.py", line 1821, in __del__\n    self.close()\n'
                b'  File "zipfile.py", line 1843, in close\n    self._fpclose(fp)\n'
                b'  File "zipfile.py", line 1943, in _fpclose\n    fp.close()\nOSError: [Errno 27] File too large\n')
WORKSHEETS = (b'  File "openpyxl/writer/excel.py", line 77, in write_data\n    self._write_worksheets()\n'
              b'  File "openpyxl/writer/excel.py", line 215, in _write_worksheets\n    self.write_worksheet(ws)\n')

# 旧版の実出力(正規化の後)。上の docstring の条件で取った。
LEGACY_GET_STREAM = (
    HEAD + WORKSHEETS
    + b'  File "openpyxl/writer/excel.py", line 200, in write_worksheet\n    writer.write()\n'
    b'  File "openpyxl/worksheet/_writer.py", line 359, in write\n    self.write_rows()\n'
    b'  File "openpyxl/worksheet/_writer.py", line 125, in write_rows\n    self.write_row(xf, row, row_idx)\n'
    b'  File "openpyxl/worksheet/_writer.py", line 147, in write_row\n    write_cell(xf, self.ws, cell, cell.has_style)\n'
    b'  File "openpyxl/cell/_writer.py", line 86, in etree_write_cell\n    xf.write(el)\n'
    b'  File "et_xmlfile/xmlfile.py", line 120, in write\n    incremental_tree._serialize_ns_xml(\n'
    b'  File "et_xmlfile/incremental_tree.py", line 626, in _serialize_ns_xml\n    _serialize_ns_xml(\n'
    b'  File "et_xmlfile/incremental_tree.py", line 626, in _serialize_ns_xml\n    _serialize_ns_xml(\n'
    b'  File "et_xmlfile/incremental_tree.py", line 613, in _serialize_ns_xml\n    ) = write_elem_start(\n'
    b'  File "et_xmlfile/incremental_tree.py", line 508, in write_elem_start\n    write("<" + tag)\n'
    b'OSError: [Errno 27] File too large\n'
    b'Exception ignored in: <generator object WorksheetWriter.get_stream at 0x772dd18bd850>\n'
    b'Traceback (most recent call last):\n'
    b'  File "openpyxl/worksheet/_writer.py", line 288, in get_stream\n'
    b'  File "et_xmlfile/xmlfile.py", line 156, in __exit__\n'
    b'  File "contextlib.py", line 142, in __exit__\n'
    b'  File "et_xmlfile/incremental_tree.py", line 878, in _get_writer\n'
    b'OSError: [Errno 27] File too large\n'
)
LEGACY_UMASK_277 = (
    b'Traceback (most recent call last):\n'
    b'  File "et_xmlfile/incremental_tree.py", line 873, in _get_writer\n    write = file_or_filename.write\n'
    b"AttributeError: 'str' object has no attribute 'write'\n\n"
    b'During handling of the above exception, another exception occurred:\n\n'
    + HEAD + WORKSHEETS
    + b'  File "openpyxl/writer/excel.py", line 199, in write_worksheet\n    writer = WorksheetWriter(ws)\n'
    b'  File "openpyxl/worksheet/_writer.py", line 56, in __init__\n    next(self.xf) # start generator\n'
    b'  File "openpyxl/worksheet/_writer.py", line 288, in get_stream\n    with xmlfile(self.out) as xf:\n'
    b'  File "et_xmlfile/xmlfile.py", line 151, in __enter__\n    writer, declared_encoding = self.writer_cm.__enter__()\n'
    b'  File "contextlib.py", line 135, in __enter__\n    return next(self.gen)\n'
    b'  File "et_xmlfile/incremental_tree.py", line 878, in _get_writer\n'
    b'    with open(file_or_filename, "w", encoding=encoding,\n'
    b"PermissionError: [Errno 13] Permission denied: '@ROOT@/tmp/openpyxl.p7kkabea'\n"
)
LEGACY_UMASK_477 = (
    HEAD + WORKSHEETS
    + b'  File "openpyxl/writer/excel.py", line 203, in write_worksheet\n    self._archive.write(writer.out, ws.path[1:])\n'
    b'  File "zipfile.py", line 1775, in write\n    with open(filename, "rb") as src, self.open(zinfo, \'w\') as dest:\n'
    b"PermissionError: [Errno 13] Permission denied: '@ROOT@/tmp/openpyxl.vd8na2mg'\n"
)
LEGACY_NO_USABLE_TEMP = (
    HEAD + WORKSHEETS
    + b'  File "openpyxl/writer/excel.py", line 199, in write_worksheet\n    writer = WorksheetWriter(ws)\n'
    b'  File "openpyxl/worksheet/_writer.py", line 52, in __init__\n    out = create_temporary_file()\n'
    b'  File "openpyxl/worksheet/_writer.py", line 36, in create_temporary_file\n'
    b"    fobj = NamedTemporaryFile(mode='w+', suffix=suffix,\n"
    b'  File "tempfile.py", line 541, in NamedTemporaryFile\n'
    b'    prefix, suffix, dir, output_type = _sanitize_params(prefix, suffix, dir)\n'
    b'  File "tempfile.py", line 126, in _sanitize_params\n    dir = gettempdir()\n'
    b'  File "tempfile.py", line 299, in gettempdir\n    return _os.fsdecode(_gettempdir())\n'
    b'  File "tempfile.py", line 292, in _gettempdir\n    tempdir = _get_default_tempdir()\n'
    b'  File "tempfile.py", line 223, in _get_default_tempdir\n    raise FileNotFoundError(_errno.ENOENT,\n'
    b"FileNotFoundError: [Errno 2] No usable temporary directory found in ['@ROOT@/tmp', '/tmp', '/var/tmp', '/usr/tmp', '@ROOT@/work']\n"
)
LEGACY_Q1_HALF = (
    HEAD + WORKSHEETS
    + b'  File "openpyxl/writer/excel.py", line 203, in write_worksheet\n    self._archive.write(writer.out, ws.path[1:])\n'
    b'  File "zipfile.py", line 1775, in write\n    with open(filename, "rb") as src, self.open(zinfo, \'w\') as dest:\n'
    b'  File "zipfile.py", line 1180, in close\n    self._fileobj.seek(self._zinfo.header_offset)\n'
    b'OSError: [Errno 27] File too large\n' + ZIP_DEL_TAIL
)
LEGACY_Q1_ZERO = (
    HEAD
    + b'  File "openpyxl/writer/excel.py", line 60, in write_data\n    archive.writestr(ARC_APP, tostring(props.to_tree()))\n'
    b'  File "zipfile.py", line 1816, in writestr\n    with self.open(zinfo, mode=\'w\') as dest:\n'
    b'  File "zipfile.py", line 1180, in close\n    self._fileobj.seek(self._zinfo.header_offset)\n'
    b'OSError: [Errno 27] File too large\n' + ZIP_DEL_TAIL
)


def get_subject():
    global subject
    if subject is None:
        spec = importlib.util.spec_from_file_location('r4e_subject', HERE / 'check_r4e.py')
        subject = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = subject
        spec.loader.exec_module(subject)
    return subject


def with_address(data, address):
    """印にした番地を、実行ごとに違う実際の番地へ戻す(旧版と Rust で番地が違う状態を作る)。"""
    return data.replace(b'0x@ADDRESS@', b'0x' + address)


def deflate(data, level):
    compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def dos_time(epoch):
    moment = time.gmtime(epoch)
    return (((moment.tm_year - 1980) << 9) | (moment.tm_mon << 5) | moment.tm_mday,
            (moment.tm_hour << 11) | (moment.tm_min << 5) | (moment.tm_sec // 2))


def make_zip(parts, *, epoch=T0 + 2, level=6, override=None, eocd_comment=b''):
    """試験用の ZIP。parts は (名前, 中身) の並び。override は名前ごとの欄の書き換え。"""
    override = override or {}
    out = bytearray()
    central = bytearray()
    for name, content in parts:
        o = override.get(name, {})
        method = o.get('method', 8)
        raw = deflate(content, level) if method == 8 else content
        date, moment = dos_time(o.get('epoch', epoch))
        crc = o.get('crc', zlib.crc32(content))
        usize = o.get('file_size', len(content))
        extra = o.get('extra', b'')
        comment = o.get('comment', b'')
        flags = o.get('flags', 0)
        external = o.get('external', (0o100600 if name.startswith(b'xl/worksheets/') else 0o600) << 16)
        offset = len(out)
        out += struct.pack('<4s2B4HL2L2H', b'PK\x03\x04', o.get('local_extract_version', o.get('extract_version', 20)), 0, flags,
                           method, moment, date, crc, len(raw), usize, len(name), len(extra))
        out += name + extra + raw + o.get('gap', b'')
        central += struct.pack('<4s4B4HL2L5H2L', b'PK\x01\x02', o.get('create_version', 20), o.get('create_system', 3),
                               o.get('extract_version', 20), 0, flags, method, moment, date, crc, len(raw), usize, len(name),
                               len(extra), len(comment), 0, o.get('internal', 0), external, offset)
        central += name + extra + comment
    cd_offset = len(out)
    out += central
    out += struct.pack('<4s4H2LH', b'PK\x05\x06', 0, 0, len(parts), len(parts), len(central), cd_offset, len(eocd_comment))
    return bytes(out + eocd_comment)


def basic_parts(created=b'2023-11-14T22:13:21Z', modified=b'2023-11-14T22:13:22Z'):
    manifest = json.loads((EXPECTED / 'manifest.json').read_text(encoding='utf-8'))
    record = next(case for case in manifest['cases'] if case['id'] == '01-basic')
    parts = []
    for part in record['parts']:
        content = (EXPECTED / '01-basic' / part['name']).read_bytes()
        if part['name'] == 'docProps/core.xml':
            content = content.replace(b'@CREATED@', created).replace(b'@MODIFIED@', modified)
        parts.append((part['name'].encode(), content))
    return parts


def fixed_parts():
    manifest = json.loads((EXPECTED / 'manifest.json').read_text(encoding='utf-8'))
    return {name.encode(): value for name, value in manifest['fixed_parts_sha256'].items()}


def replace_part(parts, name, old, new):
    out = []
    for part, content in parts:
        if part == name:
            if old not in content:
                raise AssertionError(f'{old!r} が {name!r} に無い')
            content = content.replace(old, new, 1)
        out.append((part, content))
    return out


OUTPUT = b'work/out/book.xlsx'
LOAD_OK = {'testzip': 'ok', 'load': 'ok', 'sheets': [['basic 概要', 'A1:C3', 'A2']], 'digest': 'd' * 64}


def run(m, side='old', **changes):
    root = b'/x/r4e-check-test/c0001/' + side.encode()
    base = dict(status=('exit', 0), stdout='OK out/book.xlsx にシート 1 件を出力\n'.encode(), stderr=b'',
                created=[(b'work/out', 'dir', 0o755, None), (OUTPUT, 'file', 0o644, None)],
                contents={OUTPUT: make_zip(basic_parts())}, loads={OUTPUT: dict(LOAD_OK)},
                root=root, tz='UTC', before=float(T0), after=float(T0 + 5))
    base.update(changes)
    return m.Run(**base)


def case(**extra):
    base = {'id': 'test/case', 'category': 'existing', 'setup': [], 'argv': [], 'env': {'set': {}, 'unset': []},
            'approved_differences': [], 'linux_only': False}
    base.update(extra)
    return base


def paths(m):
    return m.RuntimePaths(script=b'/repo/plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py',
                          stdlib=b'/py/lib/python3.10', site=b'/venv/lib/python3.10/site-packages')


class Base(unittest.TestCase):
    def setUp(self):
        self.m = get_subject()
        self.paths = paths(self.m)
        self.fixed = fixed_parts()

    def compare(self, the_case, old, new, **kwargs):
        return self.m.compare_runs(the_case, old, new, self.paths, self.fixed, **kwargs)

    def assertMatch(self, problems):
        self.assertEqual(problems, [])

    def assertMismatch(self, problems):
        self.assertTrue(problems, '比較器が壊した結果を合格にした')


class ZipStructure(Base):
    def xlsx(self, old_bytes, new_bytes, old=None, new=None):
        return self.m.compare_xlsx(OUTPUT, old_bytes, new_bytes, old or run(self.m), new or run(self.m, 'new'), self.fixed)

    def test_good_pair_passes_with_different_compression_and_times(self):
        parts = basic_parts()
        other = basic_parts(created=b'2023-11-14T22:13:23Z', modified=b'2023-11-14T22:13:24Z')
        # 圧縮のバイト列・圧縮後の大きさ・ZIP の時刻・core.xml の時刻の値は比べない(範囲だけ)。
        self.assertMatch(self.xlsx(make_zip(parts, level=9), make_zip(other, level=0, epoch=T0 + 4)))

    def test_parser_reads_python_zipfile_output(self):
        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in basic_parts():
                archive.writestr(zipfile.ZipInfo(name.decode(), time.gmtime(T0 + 2)[:6]), content)
        parsed = self.m.parse_zip(buffer.getvalue())
        self.assertEqual([entry['name'] for entry in parsed['entries']], [name for name, _ in basic_parts()])

    def test_every_zip_field_mutation_fails(self):
        good = make_zip(basic_parts())
        sheet = b'xl/worksheets/sheet1.xml'
        mutations = {
            'create_version': {'create_version': 21}, 'create_system': {'create_system': 0},
            'extract_version': {'extract_version': 45}, 'flag_bits': {'flags': 0x800}, 'method': {'method': 0},
            'external_attr': {'external': 0o100644 << 16}, 'internal_attr': {'internal': 1},
            'extra': {'extra': b'\x99\x99\x00\x00'}, 'comment': {'comment': b'x'},
            'crc': {'crc': 1}, 'file_size': {'file_size': 1},
            'local_vs_central': {'local_extract_version': 10}, 'gap': {'gap': b'\0'},
        }
        for label, change in mutations.items():
            with self.subTest(label):
                self.assertMismatch(self.xlsx(good, make_zip(basic_parts(), override={sheet: change})))
        self.assertMismatch(self.xlsx(good, make_zip(basic_parts(), eocd_comment=b'x')))
        self.assertMismatch(self.xlsx(good, good + b'\0'))
        self.assertMismatch(self.xlsx(good, good[:-1]))
        self.assertMismatch(self.xlsx(good, good[:len(good) // 2]))

    def test_part_order_names_and_count_fail(self):
        parts = basic_parts()
        good = make_zip(parts)
        swapped = list(parts)
        swapped[3], swapped[4] = swapped[4], swapped[3]
        renamed = [(b'xl/worksheets/sheet2.xml' if name == b'xl/worksheets/sheet1.xml' else name, content) for name, content in parts]
        extra = parts[:4] + [(b'xl/worksheets/sheet2.xml', parts[3][1])] + parts[4:]
        for label, candidate in (('順', swapped), ('名前', renamed), ('多い', extra), ('少ない', parts[:-1])):
            with self.subTest(label):
                self.assertMismatch(self.xlsx(good, make_zip(candidate)))

    def test_cell_type_bold_width_freeze_and_sheet_name_mutations_fail(self):
        parts = basic_parts()
        good = make_zip(parts)
        sheet = b'xl/worksheets/sheet1.xml'
        sheet_xml = dict(parts)[sheet]
        workbook = dict(parts)[b'xl/workbook.xml']
        cases = {
            'セル': (sheet, b'<is><t>a</t></is>', b'<is><t>z</t></is>'),
            '型': (sheet, b't="inlineStr"', b't="str"'),
            '太字': (sheet, b' s="1"', b''),
            '列幅': (sheet, b'width="', b'width="1'),
            '固定': (sheet, b'topLeftCell="A2"', b'topLeftCell="A3"'),
            'シート名': (b'xl/workbook.xml', b'name="basic', b'name="Basic'),
        }
        self.assertIn(b'<is><t>a</t></is>', sheet_xml)
        self.assertIn(b'name="basic', workbook)
        for label, (name, old, new) in cases.items():
            with self.subTest(label):
                self.assertMismatch(self.xlsx(good, make_zip(replace_part(parts, name, old, new))))

    def test_times_out_of_range_and_core_format_fail(self):
        good = make_zip(basic_parts())
        self.assertMismatch(self.xlsx(good, make_zip(basic_parts(), epoch=T0 - 10)))
        self.assertMismatch(self.xlsx(good, make_zip(basic_parts(), epoch=T0 + 60)))
        self.assertMismatch(self.xlsx(good, make_zip(basic_parts(modified=b'2023-11-14T22:14:30Z'))))
        self.assertMismatch(self.xlsx(good, make_zip(basic_parts(modified=b'2023-11-14 22:13:22Z'))))
        # 下限は偶数の秒へ切り捨てる。実行の開始 T0+1 でも T0 の ZIP の時刻は範囲に入る。
        self.assertMatch(self.xlsx(good, make_zip(basic_parts(), epoch=T0), new=run(self.m, 'new', before=float(T0 + 1))))
        # core.xml の時刻のほかの違いは印にしない。
        changed = replace_part(basic_parts(), b'docProps/core.xml', b'<dc:creator>', b'<dc:creator>x')
        self.assertMismatch(self.xlsx(good, make_zip(changed)))

    def test_local_time_uses_the_child_tz(self):
        good = make_zip(basic_parts())
        tokyo = make_zip(basic_parts(), epoch=T0 + 2 + 9 * 3600)
        self.assertMismatch(self.xlsx(good, tokyo))
        self.assertMatch(self.xlsx(tokyo, tokyo, old=run(self.m, tz='Asia/Tokyo'), new=run(self.m, 'new', tz='Asia/Tokyo')))

    def test_fixed_parts_drift_fails_even_if_both_sides_agree(self):
        drifted = make_zip(replace_part(basic_parts(), b'xl/styles.xml', b'<b', b'<i'))
        self.assertMismatch(self.xlsx(drifted, drifted))


class TreeDiff(Base):
    def entry(self, kind=stat.S_IFREG, mode=0o644, ino=1, mtime=1, ctime=1, digest='a', size=1, nlink=1):
        return self.m.Entry(kind, mode, 1, ino, nlink, size, mtime, ctime, None, digest)

    def test_input_changes_removals_and_leftovers_are_violations(self):
        directory = self.entry(stat.S_IFDIR, 0o755, ino=2, digest=None)
        before = {b'work': directory, b'work/doc': directory, b'work/doc/in.md': self.entry(), b'tmp': directory, b'home': directory}
        cases = {
            '内容': {**before, b'work/doc/in.md': self.entry(digest='b')},
            'mtime': {**before, b'work/doc/in.md': self.entry(mtime=2)},
            'ctime': {**before, b'work/doc/in.md': self.entry(ctime=2)},
            'モード': {**before, b'work/doc/in.md': self.entry(mode=0o600)},
            'ino': {**before, b'work/doc/in.md': self.entry(ino=9)},
            '消えた': {key: value for key, value in before.items() if key != b'work/doc/in.md'},
            'ディレクトリの mtime だけ': {**before, b'work/doc': dataclasses.replace(directory, mtime=5)},
            'TMPDIR に残った': {**before, b'tmp/openpyxl.abcdefgh': self.entry(ino=7)},
            'HOME に残った': {**before, b'home/x': self.entry(ino=8)},
        }
        for label, after in cases.items():
            with self.subTest(label):
                _, _, violations = self.m.diff_trees(before, after, {b'tmp'})
                self.assertTrue(violations, label)
        # 中の項目が増えたディレクトリと TMPDIR の mtime の変化、上書きした .xlsx は違反にしない。
        after = {**before, b'work': dataclasses.replace(directory, mtime=5), b'work/out': self.entry(stat.S_IFDIR, 0o755, ino=3),
                 b'tmp': dataclasses.replace(directory, mtime=6)}
        created, modified, violations = self.m.diff_trees(before, after, {b'tmp'})
        self.assertEqual(violations, [])
        self.assertEqual([item[0] for item in created], [b'work/out'])
        before[b'work/out.xlsx'] = self.entry(ino=4)
        after = {**before, b'work/out.xlsx': self.entry(ino=4, digest='c', mtime=3, ctime=3, size=9)}
        created, modified, violations = self.m.diff_trees(before, after, {b'tmp'})
        self.assertEqual((created, violations), ([], []))
        self.assertEqual(modified, [(b'work/out.xlsx', 0o644, True)])


class FullCompare(Base):
    def test_identical_results_match(self):
        self.assertMatch(self.compare(case(), run(self.m), run(self.m, 'new')))

    def test_every_result_field_mutation_fails(self):
        old = run(self.m)
        mutations = {
            '終了値': {'status': ('exit', 1)},
            '信号': {'status': ('signal', 'SIGINT')},
            '標準出力': {'stdout': b'OK out/book.xlsx\n'},
            '標準エラー': {'stderr': b'WARN x\n'},
            'ファイルの出力': {'streams': {'stdout': b'partial'}},
            '余分なファイル': {'created': old.created + [(b'work/out/extra', 'file', 0o644, None)]},
            '作ったものの権限': {'created': [(b'work/out', 'dir', 0o700, None), (OUTPUT, 'file', 0o644, None)]},
            '作らなかった': {'created': [(b'work/out', 'dir', 0o755, None)], 'contents': {}, 'loads': {}},
            '入力の変更': {'violations': ['入力が変わった: work/doc/input.md(digest)']},
            'TMPDIR の残り': {'violations': ['TMPDIR か HOME に残った: tmp/openpyxl.abcdefgh']},
            '上書き': {'modified': [(OUTPUT, 0o644, False)]},
            'testzip': {'loads': {OUTPUT: {**LOAD_OK, 'testzip': 'bad'}}},
            'load_workbook': {'loads': {OUTPUT: {'testzip': 'ok', 'load': 'error:builtins.ValueError'}}},
            '読めた中身': {'loads': {OUTPUT: {**LOAD_OK, 'digest': 'e' * 64}}},
            '信号の回数': {'sent': 1},
            '時間切れ': {'timed_out': True},
            'XLSX': {'contents': {OUTPUT: make_zip(replace_part(basic_parts(), b'xl/worksheets/sheet1.xml', b'>a<', b'>b<'))}},
            'XLSX でない': {'contents': {OUTPUT: b'PK\x03\x04broken'}},
        }
        for label, change in mutations.items():
            with self.subTest(label):
                self.assertMismatch(self.compare(case(), old, run(self.m, 'new', **change)))

    def test_workspace_paths_are_marked_but_other_paths_are_not(self):
        old = run(self.m, stderr=b'ERROR /x/r4e-check-test/c0001/old/work/doc\n')
        self.assertMatch(self.compare(case(), old, run(self.m, 'new', stderr=b'ERROR /x/r4e-check-test/c0001/new/work/doc\n')))
        self.assertMismatch(self.compare(case(), old, run(self.m, 'new', stderr=b'ERROR /x/r4e-check-test/c0001/old/work/doc\n')))

    def test_csv_path_case_rejects_fallback(self):
        the_case = case(approved_differences=['csv-path'], legacy_env={'set': {}, 'unset': []})
        self.assertMatch(self.compare(the_case, run(self.m), run(self.m, 'new')))
        fallback = run(self.m, 'new', stdout=b'FALLBACK x\n', created=[(b'work/out', 'dir', 0o755, None), (b'work/out/book', 'dir', 0o755, None)],
                       contents={}, loads={})
        self.assertMismatch(self.compare(the_case, run(self.m), fallback))


class RuntimePaths(Base):
    SCRIPT = b'/repo/plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py'

    def legacy(self, user_path=SCRIPT, fake=b'/py/lib/python3.10/pathlib.py'):
        return (b'WARN \xe5\x85\xa5\xe5\x8a\x9b: ' + user_path + b'\n'
                b'/venv/lib/python3.10/site-packages/openpyxl/workbook/child.py:99: UserWarning: Title is more than 31 characters.\n'
                b'  warnings.warn("Title is more than 31 characters. Some applications may not be able to read the file")\n'
                b'Traceback (most recent call last):\n'
                b'  File "' + self.SCRIPT + b'", line 241, in <module>\n    sys.exit(main())\n'
                b'  File "/py/lib/python3.10/pathlib.py", line 1117, in read_text\n    with self.open(encoding=encoding, errors=errors) as f:\n'
                b'  File "/venv/lib/python3.10/site-packages/openpyxl/cell/cell.py", line 211, in check_string\n'
                b'  [Previous line repeated 3 more times]\n'
                b'openpyxl.utils.exceptions.IllegalCharacterError: a\n'
                b'  File "' + fake + b'", line 1, in fake\n'
                b'b cannot be used in worksheets.\n')

    def normalized(self, user_path=SCRIPT, fake=b'/py/lib/python3.10/pathlib.py'):
        return (b'WARN \xe5\x85\xa5\xe5\x8a\x9b: ' + user_path + b'\n'
                b'openpyxl/workbook/child.py:99: UserWarning: Title is more than 31 characters.\n'
                b'  warnings.warn("Title is more than 31 characters. Some applications may not be able to read the file")\n'
                b'Traceback (most recent call last):\n'
                b'  File "md_tables_to_xlsx.py", line 241, in <module>\n    sys.exit(main())\n'
                b'  File "pathlib.py", line 1117, in read_text\n    with self.open(encoding=encoding, errors=errors) as f:\n'
                b'  File "openpyxl/cell/cell.py", line 211, in check_string\n'
                b'  [Previous line repeated 3 more times]\n'
                b'openpyxl.utils.exceptions.IllegalCharacterError: a\n'
                b'  File "' + fake + b'", line 1, in fake\n'
                b'b cannot be used in worksheets.\n')

    def test_only_frames_and_warning_location_are_replaced(self):
        self.assertEqual(self.m.normalize_runtime(self.legacy(), self.paths), self.normalized())
        self.assertMatch(self.compare(case(), run(self.m, stderr=self.legacy()), run(self.m, 'new', stderr=self.normalized())))

    def test_user_passed_path_is_not_replaced(self):
        # 利用者が渡したパス(旧版のスクリプトや site-packages の中を指す)を Rust が固定名にしたら不一致。
        for user in (self.SCRIPT, b'/venv/lib/python3.10/site-packages/openpyxl/cell/cell.py'):
            with self.subTest(user):
                renamed = self.normalized(user_path=self.paths.relative(user))
                self.assertMismatch(self.compare(case(), run(self.m, stderr=self.legacy(user_path=user)), run(self.m, 'new', stderr=renamed)))

    def test_fake_file_line_in_input_body_is_not_replaced(self):
        renamed = self.normalized(fake=b'pathlib.py')
        self.assertMismatch(self.compare(case(), run(self.m, stderr=self.legacy()), run(self.m, 'new', stderr=renamed)))

    def test_only_the_exact_script_path_is_fixed(self):
        self.assertEqual(self.paths.relative(b'/other/md_tables_to_xlsx.py'), b'/other/md_tables_to_xlsx.py')
        self.assertEqual(self.paths.relative(self.SCRIPT), b'md_tables_to_xlsx.py')
        self.assertEqual(self.paths.relative(b'/py/lib/python3.10x/zipfile.py'), b'/py/lib/python3.10x/zipfile.py')


class DeclaredDifferences(Base):
    FATAL = (b'Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\n'
             b'Python runtime state: core initialized\n\nCurrent thread 0x%s (most recent call first):\n  <no Python frame>\n')

    def test_current_thread_needs_the_declaration_and_the_format(self):
        old = run(self.m, status=('exit', 1), stdout=b'', stderr=self.FATAL % b'00007f0000000001', created=[], contents={}, loads={})
        new = dataclasses.replace(old, stderr=self.FATAL % b'000071c0ffee0002', root=b'/x/r4e-check-test/c0001/new')
        declared = case(approved_differences=['fatal-current-thread'])
        self.assertMatch(self.compare(declared, old, new))
        self.assertMismatch(self.compare(case(), old, new))
        for broken in (b'zz', b'7f0000000001', b'00007f00000000010'):
            with self.subTest(broken):
                self.assertMismatch(self.compare(declared, old, dataclasses.replace(new, stderr=self.FATAL % broken)))
        moved = b'\n' + self.FATAL % b'000071c0ffee0002'
        self.assertMismatch(self.compare(declared, old, dataclasses.replace(new, stderr=moved)))

    def test_zipfile_del_address_needs_the_declaration(self):
        old = run(self.m, status=('exit', 1), stdout=b'', stderr=with_address(LEGACY_Q1_ZERO, b'7d2a1e459cf0'), contents={}, loads={})
        new = dataclasses.replace(old, stderr=with_address(LEGACY_Q1_ZERO, b'5555deadbeef'), root=b'/x/r4e-check-test/c0001/new')
        self.assertMatch(self.compare(case(approved_differences=['variable-output']), old, new))
        self.assertMismatch(self.compare(case(), old, new))
        broken = dataclasses.replace(new, stderr=with_address(LEGACY_Q1_ZERO, b'zz'))
        self.assertMismatch(self.compare(case(approved_differences=['variable-output']), old, broken))
        # get_stream の番地は形式だけの対象に入らない。
        other = dataclasses.replace(old, stderr=LEGACY_GET_STREAM.replace(b'0x772dd18bd850', b'0x111111111111'))
        self.assertMismatch(self.compare(case(approved_differences=['variable-output']), dataclasses.replace(old, stderr=LEGACY_GET_STREAM), other))

    def test_validate_cases_rejects_undeclared_and_out_of_scope_differences(self):
        document = json.loads((HERE.parent / 'fixtures' / 'r4e' / 'cases.json').read_text(encoding='utf-8'))
        sha = document['legacy_script']['sha256']
        self.assertEqual(self.m.validate_cases(document, sha), [])

        def broken(change):
            copy = json.loads(json.dumps(document))
            change(copy)
            return self.m.validate_cases(copy, sha)

        def first(copy, prefix):
            return next(item for item in copy['cases'] if item['id'].startswith(prefix))

        checks = {
            '未知の差': lambda d: first(d, 'existing/').update(approved_differences=['made-up']),
            '前提の外': lambda d: first(d, 'existing/').update(approved_differences=['memory-and-zip64-position']),
            '宣言の無い旧版だけの環境': lambda d: first(d, 'env/encoding-').update(approved_differences=[]),
            '固定期待に uncompressed が無い': lambda d: first(d, 'write/fsize-legacy-ok').update(approved_differences=['write-partial-failure']),
            'ID の重なり': lambda d: d['cases'].append(dict(d['cases'][0])),
            '数': lambda d: d.update(case_count=1),
            '旧版のスクリプト': lambda d: d['legacy_script'].update(sha256='0' * 64),
            '差の一覧': lambda d: d['approved_differences'].pop('temp-file'),
            '分類の欠け': lambda d: (d.update(cases=[c for c in d['cases'] if c['category'] != 'csv'], case_count=len(d['cases']) - 1),
                                    d['counts'].pop('csv')),
            '未知の出力': lambda d: first(d, 'existing/').update(stdout='socket'),
        }
        for label, change in checks.items():
            with self.subTest(label):
                self.assertTrue(broken(change), label)


class TempFile(Base):
    def test_identification_uses_three_conditions_on_real_legacy_outputs(self):
        tmp = b'@ROOT@/tmp'
        self.assertEqual(self.m.temp_file_failure(LEGACY_GET_STREAM, tmp), ['excel.py の 199・200 行'])
        self.assertEqual(self.m.temp_file_failure(LEGACY_UMASK_277, tmp), ['excel.py の 199・200 行', '一時ファイルの名前'])
        self.assertEqual(self.m.temp_file_failure(LEGACY_UMASK_477, tmp), ['一時ファイルの名前'])
        self.assertEqual(self.m.temp_file_failure(LEGACY_NO_USABLE_TEMP, tmp),
                         ['No usable temporary directory found', 'excel.py の 199・200 行'])
        # 2 つ目の呼出履歴の形(ZipFile.__del__)は見分けに使わない。Q1 の実出力は一時ファイルによらない。
        self.assertEqual(self.m.temp_file_failure(LEGACY_Q1_HALF, tmp), [])
        self.assertEqual(self.m.temp_file_failure(LEGACY_Q1_ZERO, tmp), [])
        # ほかの置き場の openpyxl.XXXXXXXX は当たらない。
        self.assertEqual(self.m.temp_file_failure(LEGACY_UMASK_477, b'@ROOT@/elsewhere'), [])

    def scenario(self, legacy_stderr, mask_mode=0o400):
        old = run(self.m, status=('exit', 1), stdout=b'', stderr=legacy_stderr, created=[(OUTPUT, 'file', mask_mode, None)],
                  contents={OUTPUT: b'PK\x03\x04partial'}, loads={})
        normal = run(self.m, 'nrm', created=[(OUTPUT, 'file', 0o644, None)])
        new = run(self.m, 'new', created=[(OUTPUT, 'file', mask_mode, None)])
        return case(approved_differences=['temp-file']), old, normal, new

    def test_rust_expectation_is_the_normal_legacy_with_umask_modes(self):
        for stderr in (LEGACY_UMASK_277, LEGACY_UMASK_477, LEGACY_GET_STREAM, LEGACY_NO_USABLE_TEMP):
            with self.subTest(stderr[-80:]):
                the_case, old, normal, new = self.scenario(stderr)
                self.assertMatch(self.compare(the_case, old, new, normal=normal))
                self.assertMismatch(self.compare(the_case, old, new))
                self.assertMismatch(self.compare(the_case, old, dataclasses.replace(new, created=[(OUTPUT, 'file', 0o644, None)]), normal=normal))
                self.assertMismatch(self.compare(the_case, old, dataclasses.replace(new, tmp_touched=True), normal=normal))
                mirrored = dataclasses.replace(new, status=old.status, stdout=b'', stderr=old.stderr)
                self.assertMismatch(self.compare(the_case, old, mirrored, normal=normal))
                # 宣言が無ければ、Rust の成功は旧版の失敗と不一致。
                self.assertMismatch(self.compare(case(), old, new, normal=normal))

    def test_not_identified_failure_is_compared_as_is(self):
        the_case, old, normal, new = self.scenario(LEGACY_Q1_HALF)
        self.assertMismatch(self.compare(the_case, old, new, normal=normal))


class PartialWrite(Base):
    def pair(self, legacy=LEGACY_Q1_HALF, rust=None, **rust_changes):
        old = run(self.m, status=('exit', 1), stdout=b'', stderr=with_address(legacy, b'77962bc59cf0'),
                  contents={OUTPUT: b'PK\x03\x04partial-old'}, loads={})
        inner = rust if rust is not None else legacy
        new = run(self.m, 'new', status=('exit', 1), stdout=b'', stderr=with_address(inner, b'5d5d5d5d5d5d'),
                  contents={OUTPUT: b'PK\x03\x04other-size-partial'}, loads={})
        return old, dataclasses.replace(new, **rust_changes)

    def test_head_and_last_lines_are_compared_but_inner_lines_are_not(self):
        the_case = case(approved_differences=['write-partial-failure', 'variable-output'], rlimit_fsize={'rule': 'half-legacy-xlsx'})
        inner = LEGACY_Q1_HALF.replace(b'line 1775, in write', b'line 9999, in rust_write').replace(b'line 1180, in close', b'line 1, in x')
        old, new = self.pair(rust=inner)
        self.assertMatch(self.compare(the_case, old, new))
        broken = {
            '先頭': LEGACY_Q1_HALF.replace(b'line 196, in write_xlsx', b'line 197, in write_xlsx'),
            '最後の行': LEGACY_Q1_HALF.replace(b'File too large\nException', b'No space left on device\nException'),
            '2 つ目の最後の行': LEGACY_Q1_HALF[:-len(b'File too large\n')] + b'No space left on device\n',
            '2 つ目が無い': LEGACY_Q1_HALF[:LEGACY_Q1_HALF.index(b'Exception ignored')],
            '番地の形': LEGACY_Q1_HALF.replace(b'0x@ADDRESS@', b'0xZZ'),
        }
        for label, stderr in broken.items():
            with self.subTest(label):
                self.assertMismatch(self.compare(the_case, *self.pair(rust=stderr)))
        for label, change in {'標準出力': {'stdout': b'OK\n'}, '終了値': {'status': ('exit', 2)},
                              '余分なファイル': {'created': [(b'work/out', 'dir', 0o755, None), (OUTPUT, 'file', 0o644, None),
                                                          (b'work/out/x', 'file', 0o644, None)]},
                              '入力の変更': {'violations': ['入力が変わった: work/doc/three.md(digest)']}}.items():
            with self.subTest(label):
                self.assertMismatch(self.compare(the_case, *self.pair(**change)))

    def test_temp_file_failure_is_not_accepted_as_partial_write(self):
        the_case = case(approved_differences=['write-partial-failure', 'variable-output'], rlimit_fsize={'rule': 'half-legacy-xlsx'})
        self.assertMismatch(self.compare(the_case, *self.pair(legacy=LEGACY_GET_STREAM)))

    def test_first_write_failure_compares_every_line_but_the_address(self):
        the_case = case(approved_differences=['write-partial-failure', 'variable-output'], rlimit_fsize=0)
        old, new = self.pair(legacy=LEGACY_Q1_ZERO, contents={OUTPUT: b''})
        old = dataclasses.replace(old, contents={OUTPUT: b''})
        self.assertMatch(self.compare(the_case, old, new))
        inner = LEGACY_Q1_ZERO.replace(b'line 1816, in writestr', b'line 1, in writestr')
        self.assertMismatch(self.compare(the_case, old, dataclasses.replace(new, stderr=with_address(inner, b'abc'))))

    def test_legacy_head_from_source_matches_the_real_output(self):
        head = self.m.legacy_head()
        self.assertTrue(LEGACY_Q1_HALF.startswith(head))
        self.assertTrue(head.endswith(b'    wb.save(checked_out)\n'))


class FixedQ3(Base):
    def rust(self, **changes):
        stderr = self.m.legacy_head() + b'  File "x", line 1, in y\nOSError: [Errno 27] File too large\n' + with_address(ZIP_DEL_TAIL, b'7f00ff')
        base = dict(status=('exit', 1), stdout=b'', stderr=stderr, created=[(b'work/out', 'dir', 0o755, None), (OUTPUT, 'file', 0o644, None)],
                    contents={OUTPUT: b'PK\x03\x04partial'}, loads={})
        base.update(changes)
        return run(self.m, 'new', **base)

    def test_fixed_expectation_and_mutations(self):
        the_case = case(approved_differences=['uncompressed', 'write-partial-failure', 'variable-output'], compare='fixed',
                        expected_rust={'exit': 1, 'stdout': '', 'stderr_head_through': 'wb.save(checked_out)',
                                       'stderr_last_line': 'OSError: [Errno 27] File too large'})
        old = run(self.m)
        self.assertMatch(self.compare(the_case, old, self.rust()))
        good = self.rust().stderr
        mutations = {
            '終了値': {'status': ('exit', 0)},
            '標準出力': {'stdout': 'OK out/book.xlsx にシート 1 件を出力\n'.encode()},
            '先頭': {'stderr': good.replace(b'line 196,', b'line 195,')},
            '最後の行': {'stderr': good.replace(b'File too large\nException', b'No space left on device\nException')},
            '2 つ目が無い': {'stderr': good[:good.index(b'Exception ignored')]},
            '2 つ目の形': {'stderr': good.replace(b'ZipFile.__del__', b'WorksheetWriter.get_stream')},
            '余分なファイル': {'created': self.rust().created + [(b'work/out/x', 'file', 0o644, None)]},
            '入力の変更': {'violations': ['入力が変わった: work/doc/three.md(digest)']},
        }
        for label, change in mutations.items():
            with self.subTest(label):
                self.assertMismatch(self.compare(the_case, old, self.rust(**change)))
        self.assertMismatch(self.compare(the_case, run(self.m, status=('exit', 1)), self.rust()))
        self.assertMismatch(self.compare(case(compare='fixed', approved_differences=['write-partial-failure']), old, self.rust()))


class Signals(Base):
    TRACE = (b'Traceback (most recent call last):\n  File "md_tables_to_xlsx.py", line 241, in <module>\n    sys.exit(main())\n'
             b'  File "md_tables_to_xlsx.py", line 219, in main\n    print(f"WARN ...", file=sys.stderr)\nKeyboardInterrupt\n')

    def test_sigint_compares_status_last_line_and_stdout(self):
        sigint = case(approved_differences=['variable-output'], signals={'signal': 'SIGINT', 'count': 1})
        old = run(self.m, status=('signal', 'SIGINT'), stdout=b'', stderr=b'WARN a\n' + self.TRACE, created=[], contents={}, loads={}, sent=1)
        inner = self.TRACE.replace(b'line 219, in main', b'line 220, in main')
        new = dataclasses.replace(old, stderr=b'WARN a\nWARN b\n' + inner, root=b'/x/r4e-check-test/c0001/new')
        self.assertMatch(self.compare(sigint, old, new))
        for label, change in {'最後の行': {'stderr': self.TRACE + b'x\n'}, '終了値': {'status': ('exit', 130)},
                              '標準出力': {'stdout': b'OK\n'}, '送れた回数': {'sent': 2}}.items():
            with self.subTest(label):
                self.assertMismatch(self.compare(sigint, old, dataclasses.replace(new, **change)))
        # 宣言が無いか SIGTERM なら全部のバイトを比べる。
        self.assertMismatch(self.compare(case(signals={'signal': 'SIGINT', 'count': 1}), old, new))
        sigterm = case(approved_differences=['variable-output'], signals={'signal': 'SIGTERM', 'count': 1})
        self.assertMismatch(self.compare(sigterm, old, new))


class NonblockingFullPipe(Base):
    """O_NONBLOCK の満杯のパイプ: 子の write は待たずに EAGAIN になり、比較器は先に書いた分を除く。"""

    CHILD = (b'import os, sys\n'
             b'target, other = (1, 2) if sys.argv[1] == "stdout" else (2, 1)\n'
             b'try:\n'
             b'    os.write(target, b"x")\n'
             b'    result = "written"\n'
             b'except BlockingIOError as error:\n'
             b'    result = f"errno {error.errno}"\n'
             b'os.write(other, f"{result} blocking={os.get_blocking(target)}\\n".encode())\n')

    def test_child_write_fails_with_eagain_and_the_prefill_is_removed(self):
        # 子はファイルを作らない(-B・-c)ので、現在のディレクトリはこのファイルの場所でよい。
        place = str(HERE).encode()
        for name in ('stdout', 'stderr'):
            with self.subTest(name):
                spec = self.m.RunSpec(
                    argv=[sys.executable.encode(), b'-I', b'-S', b'-B', b'-c', self.CHILD, name.encode()],
                    cwd=place, components=[], env={}, umask=0o022, stdin='devnull',
                    stdout='nonblock-full-pipe' if name == 'stdout' else 'pipe',
                    stderr='nonblock-full-pipe' if name == 'stderr' else 'pipe',
                    stream_dir=place)
                raw = self.m.execute(spec)
                self.assertEqual(raw.status, ('exit', 0))
                held, other = (raw.stdout, raw.stderr) if name == 'stdout' else (raw.stderr, raw.stdout)
                self.assertEqual(held, b'')
                self.assertEqual(other, f'errno {errno.EAGAIN} blocking=False\n'.encode())


class PrefilledStderr(Base):
    """先に満杯にした標準エラー: 子が fd 2 への write で止まってから信号を送り、先に書いた分を除く。"""

    @unittest.skipUnless(sys.platform.startswith('linux'), '/proc の系の呼出しは Linux だけ')
    def test_signal_waits_until_the_child_blocks_writing_stderr(self):
        place = str(HERE).encode()
        child = b'import os, time\ntime.sleep(0.3)\nos.write(1, b"start\\n")\nos.write(2, b"blocked\\n")\nos.write(1, b"never\\n")\n'
        spec = self.m.RunSpec(
            argv=[sys.executable.encode(), b'-I', b'-S', b'-B', b'-c', child],
            cwd=place, components=[], env={}, umask=0o022, stdin='devnull',
            stdout='pipe', stderr='prefilled-full-pipe', stream_dir=place,
            signals={'wait': 'stderr-write-blocked', 'signal': 'SIGTERM', 'count': 1, 'interval_ms': 100,
                     'after': 'drain'})
        raw = self.m.execute(spec)
        self.assertEqual(raw.status, ('signal', 'SIGTERM'))
        self.assertEqual(raw.sent, 1)
        self.assertEqual(raw.stdout, b'start\n')
        self.assertEqual(raw.stderr, b'')


class Blocks(Base):
    def test_chained_tracebacks_and_headers(self):
        blocks = self.m.parse_blocks(LEGACY_UMASK_277)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[1].header, [b'During handling of the above exception, another exception occurred:'])
        blocks = self.m.parse_blocks(with_address(LEGACY_Q1_HALF, b'abc'))
        self.assertEqual(blocks[1].header, [b'Exception ignored in: <function ZipFile.__del__ at 0xabc>'])
        self.assertEqual(blocks[1].exception, [b'OSError: [Errno 27] File too large'])
        self.assertEqual(len(self.m.parse_blocks(LEGACY_GET_STREAM)[1].frames), 4)


class Summary(Base):
    def result(self, identifier, state='match', **extra):
        return self.m.CaseResult(identifier, state, extra.pop('problems', []), **extra)

    def cases(self, count=3):
        return [case(id=f'c/{index}') for index in range(count)]

    def test_pass_line_format(self):
        selected = self.cases()
        code, lines = self.m.summarize([self.result(c['id']) for c in selected], selected, partial=False, seconds=12.3, skips=[], fixed=(5, 0))
        self.assertEqual(code, self.m.EXIT_PASS)
        self.assertRegex(lines[-1], r'^合格: [0-9]+ 件\(未実施 [0-9]+ 件\)/ [0-9]+ 秒$')
        self.assertEqual(lines[-1], '合格: 8 件(未実施 0 件)/ 12 秒')

    def test_zero_unexecuted_error_mismatch_and_partial_never_pass(self):
        selected = self.cases()
        good = [self.result(c['id']) for c in selected]
        checks = {
            '0 件': ([self.result(c['id'], 'skipped', skip_reason='Linux だけ') for c in selected], selected),
            '空': ([], []),
            '未実行': (good[:2] + [None], selected),
            '結果の数': (good[:2], selected),
            '別のケースの結果': (good[:2] + [self.result('c/other')], selected),
            '想定外の失敗': (good[:2] + [self.result('c/2', 'error', problems=['想定外の失敗: x'])], selected),
            '不一致': (good[:2] + [self.result('c/2', 'mismatch', problems=['終了値が違う'])], selected),
            '未知の状態': (good[:2] + [self.result('c/2', 'unknown')], selected),
        }
        for label, (results, chosen) in checks.items():
            with self.subTest(label):
                code, lines = self.m.summarize(results, chosen, partial=False, seconds=1, skips=[])
                self.assertEqual(code, self.m.EXIT_FAIL)
                self.assertTrue(lines[-1].startswith('不合格: '))
        code, lines = self.m.summarize(good, selected, partial=False, seconds=1, skips=[], fixed=(5, 1))
        self.assertEqual(code, self.m.EXIT_FAIL)
        code, lines = self.m.summarize(good, selected, partial=True, seconds=1, skips=[])
        self.assertEqual(code, self.m.EXIT_PARTIAL)
        self.assertTrue(lines[-1].startswith('一部だけ: '))
        self.assertNotIn('合格: ', lines[-1].split('(')[0])

    def test_mac_skips_are_counted_and_all_skipped_fails(self):
        selected = self.cases()
        results = [self.result('c/0'), self.result('c/1', partial_skips=['strace(Linux だけ)']),
                   self.result('c/2', 'skipped', skip_reason='Mac に /dev/full が無い')]
        code, lines = self.m.summarize(results, selected, partial=False, seconds=1, skips=['C.UTF-8(Linux だけ)'])
        self.assertEqual(code, self.m.EXIT_PASS)
        self.assertEqual(lines[-1], '合格: 2 件(未実施 3 件)/ 1 秒')
        self.assertEqual(sum(line.startswith('未実施: ') for line in lines), 3)


class Arguments(Base):
    def test_fixture_and_python_paths(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory(dir=self.m.temp_parent(None)) as directory:
            plain = Path(directory) / 'plain'
            plain.write_text('x')
            plain.chmod(0o644)
            runnable = Path(directory) / 'runnable'
            runnable.write_text('#!/bin/sh\n')
            runnable.chmod(0o755)
            for value in (None, '', 'relative/r4e_contract', 'r4e_contract', str(Path(directory) / 'missing'), directory, str(plain)):
                with self.subTest(value):
                    with self.assertRaises(self.m.Failure):
                        self.m.executable_path(value, '--fixture')
            self.assertEqual(self.m.executable_path(str(runnable), '--fixture'), os.fsencode(str(runnable)))
        with self.assertRaises(SystemExit):
            with unittest.mock.patch('sys.stderr'):
                self.m.parse_arguments([])
        with self.assertRaises(SystemExit):
            with unittest.mock.patch('sys.stderr'):
                self.m.parse_arguments(['--fixture', '/x'])

    def test_temp_parent_must_be_an_existing_absolute_directory(self):
        import os
        for value in ('relative/dir', '/nonexistent/r4e-parent', __file__):
            with self.subTest(value):
                with self.assertRaises(self.m.Failure):
                    self.m.temp_parent(value)
        self.assertEqual(self.m.temp_parent(str(HERE)), os.path.realpath(str(HERE)))
        self.assertIn('RLIMIT_NOFILE=', self.m.inherited_limits())

    def test_legacy_environment_must_match_exactly(self):
        good = {'implementation': 'cpython', 'version': [3, 10, 11], 'stdlib': '/py/lib/python3.10', 'lxml': False,
                'openpyxl': '3.1.5', 'et_xmlfile': '2.0.0', 'openpyxl_lxml': False, 'site_packages': '/venv/site-packages'}
        self.assertEqual(self.m.legacy_problems(good), [])
        for label, change in {'版': {'version': [3, 10, 12]}, '実装': {'implementation': 'pypy'}, 'openpyxl': {'openpyxl': '3.1.4'},
                              'et_xmlfile': {'et_xmlfile': '1.1.0'}, 'lxml': {'lxml': True}, 'openpyxl の lxml': {'openpyxl_lxml': True},
                              '読み込めない': {'import_error': 'No module named openpyxl'}}.items():
            with self.subTest(label):
                self.assertTrue(self.m.legacy_problems({**good, **change}))


class FixturePreflight(Base):
    """検査入口の事前の確かめ(起動前の記録と、呼出履歴の段の表)が、壊した入口を合格にしないこと。"""

    def raw(self, output, closed=None, status=('exit', 0)):
        stdout, stderr = (b'', output) if closed == 1 else (output, b'')
        return self.m.RawResult(status, stdout, stderr, {}, T0, T0 + 1, False, 0)

    def test_startup_record_needs_the_pre_main_call_and_the_closed_fds(self):
        normal = b'r4e-startup chardev/valid fifo/valid fifo/valid sigpipe=default sigint=default now=chardev fifo fifo\n'
        fd1 = b'r4e-startup chardev/valid closed/invalid:9 fifo/valid sigpipe=ignore sigint=ignore now=chardev chardev fifo\n'
        fd2 = b'r4e-startup chardev/valid fifo/valid closed/invalid:9 sigpipe=default sigint=default now=chardev fifo chardev\n'
        self.assertMatch(self.m.startup_problems(self.raw(normal), None))
        self.assertMatch(self.m.startup_problems(self.raw(fd1, 1), 1))
        self.assertMatch(self.m.startup_problems(self.raw(fd2, 2), 2))
        broken = {
            '記録が無い': (self.raw(b'r4e-startup not-recorded\n'), None),
            '閉じた fd を埋めた後に記録した': (self.raw(fd1.replace(b'closed/invalid:9', b'chardev/valid'), 1), 1),
            '閉じた fd 2 を記録しない': (self.raw(normal, 2), 2),
            '開いた fd を使えないと記録した': (self.raw(normal.replace(b'fifo/valid fifo', b'fifo/invalid:9 fifo')), None),
            '終了値': (self.raw(normal, status=('exit', 70)), None),
            '余計な行': (self.raw(normal + b'x\n'), None),
        }
        for label, (raw, closed) in broken.items():
            with self.subTest(label):
                self.assertMismatch(self.m.startup_problems(raw, closed))

    def test_frames_are_checked_against_the_legacy_files(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory(dir=self.m.temp_parent(None)) as directory:
            stdlib = Path(directory) / 'stdlib'
            site = Path(directory) / 'site' / 'pkg'
            stdlib.mkdir()
            site.mkdir(parents=True)
            (stdlib / 'mod.py').write_text('x = 1\n\ndef f(items):\n    return [g(i) for i in items]\n\n'
                                           'class C:\n    y = 2\n', encoding='utf-8')
            (site / 'm.py').write_text('def h():\n    raise ValueError("x")\n', encoding='utf-8')
            paths = self.m.RuntimePaths(script=os.fsencode(self.m.LEGACY), stdlib=os.fsencode(str(stdlib)),
                                        site=os.fsencode(str(site.parent)))

            def frame(name, line, function, source):
                return {'file': name, 'line': line, 'function': function, 'source': source}

            good = [
                frame('mod.py', 1, '<module>', 'x = 1'),
                frame('mod.py', 4, 'f', '    return [g(i) for i in items]'),
                frame('mod.py', 4, '<listcomp>', '    return [g(i) for i in items]'),
                frame('mod.py', 7, 'C', '    y = 2'),
                frame('pkg/m.py', 2, 'h', '    raise ValueError("x")'),
                frame('md_tables_to_xlsx.py', 241, '<module>', '    sys.exit(main())'),
            ]
            cache = {}
            for item in good:
                with self.subTest(item=item):
                    self.assertMatch(self.m.frame_problems(item, paths, cache))
            broken = {
                '行番号': frame('mod.py', 3, 'f', '    return [g(i) for i in items]'),
                'ソースの行': frame('mod.py', 4, 'f', 'return [g(i) for i in items]'),
                '関数の名前': frame('pkg/m.py', 2, '<module>', '    raise ValueError("x")'),
                '外側の外側': frame('mod.py', 4, '<module>', '    return [g(i) for i in items]'),
                'ファイルの外': frame('mod.py', 99, 'f', ''),
                '無いファイル': frame('none.py', 1, '<module>', ''),
                '絶対パス': frame(str(stdlib / 'mod.py'), 1, '<module>', 'x = 1'),
                '親へ出る': frame('../stdlib/mod.py', 1, '<module>', 'x = 1'),
                '旧スクリプトの行': frame('md_tables_to_xlsx.py', 241, 'main', '    sys.exit(main())'),
                '欄が無い': {'file': 'mod.py', 'line': 1, 'function': '<module>'},
                '型': frame('mod.py', '1', '<module>', 'x = 1'),
            }
            for label, item in broken.items():
                with self.subTest(label):
                    self.assertMismatch(self.m.frame_problems(item, paths, cache))



class BlockedWrite(Base):
    """先に満杯にした標準エラーで止まったかの決め方。カーネルの内部の関数名(wchan)には頼らない。"""

    SYSCALL = {'x86_64': '1 0x2 0x7f00 0x8 0x0 0x0 0x0 0x7ffd 0x7f00\n',
               'aarch64': '64 0x2 0xaaaa 0x8 0x0 0x0 0x0 0xffff 0xffff\n'}
    STAT = '4242 (python3 (x) y) S 1 2 3 0 -1 4194560\n'

    def test_write_on_fd2_sleeping_on_a_full_pipe_holds_for_both_cpus(self):
        for machine, syscall in self.SYSCALL.items():
            with self.subTest(machine):
                self.assertTrue(self.m.write_blocked_on_full_pipe(syscall, self.STAT, 65536, 65536, machine))
                # pipe-user-pages-soft を超えた利用者のパイプは 2 ページ。
                self.assertTrue(self.m.write_blocked_on_full_pipe(syscall, self.STAT, 8192, 8192, machine))

    def test_other_states_do_not_hold(self):
        syscall = self.SYSCALL['x86_64']
        cases = {
            '走っている': ('running\n', self.STAT, 65536),
            '系の呼出しの外で止まった': ('-1 0x7ffd 0x7f00\n', self.STAT, 65536),
            '別の fd': (syscall.replace('0x2', '0x1', 1), self.STAT, 65536),
            '別の系の呼出し': ('230 0x0 0x0 0x7ffd\n', self.STAT, 65536),
            '状態 R': (syscall, self.STAT.replace(') S ', ') R '), 65536),
            '状態 D': (syscall, self.STAT.replace(') S ', ') D '), 65536),
            '満杯でない': (syscall, self.STAT, 65535),
            '空': ('', '', 65536),
        }
        for label, (text, stat_text, pending) in cases.items():
            with self.subTest(label):
                self.assertFalse(self.m.write_blocked_on_full_pipe(text, stat_text, pending, 65536, 'x86_64'))
        self.assertFalse(self.m.write_blocked_on_full_pipe(syscall, self.STAT, 65536, 65536, 'aarch64'))
        with self.assertRaises(self.m.Failure):
            self.m.write_blocked_on_full_pipe(syscall, self.STAT, 65536, 65536, 'riscv64')


class SignalWaits(Base):
    """信号を送る前の待ち: 時間切れの文に最後の様子を入れ、子が何も書く前の 0 を止まったと数えない。"""

    class Process:
        def poll(self):
            return None

    def plan(self, wait):
        spec = {'wait': wait, 'signal': 'SIGINT', 'count': 1, 'interval_ms': 100, 'after': 'drain'}
        return self.m.SignalPlan(spec, 7, None, 4242)

    def test_timeout_message_has_the_last_observation(self):
        plan = self.plan('stderr-write-blocked')
        observed = 'syscall=1 0x2 state=S wchan=anon_pipe_write 溜まった量=65536 容量=65536'
        with unittest.mock.patch.object(self.m, 'stderr_write_blocked', return_value=(False, observed)) as probe:
            plan.step(self.Process(), plan.started + 0.1)
            probe.assert_called_once_with(4242, 7)
            with self.assertRaises(self.m.Failure) as caught:
                plan.step(self.Process(), plan.started + self.m.WAIT_TIMEOUT + 1)
        self.assertIn('stderr-write-blocked', str(caught.exception))
        self.assertIn(observed, str(caught.exception))

    def test_pipe_full_stable_ignores_zero_before_the_child_writes(self):
        plan = self.plan('pipe-full-stable')
        values = iter([0] * 30 + [4096] * 12)
        results = []
        with unittest.mock.patch.object(self.m, 'pending_bytes', side_effect=lambda fd: next(values)):
            now = plan.started
            for _ in range(42):
                now += 0.06
                results.append(plan.condition(now))
        self.assertFalse(any(results[:40]))
        self.assertTrue(results[40] and results[41])
        self.assertEqual(plan.observed, '溜まった量=4096')


class SelftestSkips(Base):
    """自己検査の skip: Linux だけと宣言したものだけを、Linux 以外で未実施に数える。"""

    NAME = 'PrefilledStderr.test_signal_waits_until_the_child_blocks_writing_stderr'
    OTHER = 'Summary.test_pass_line_format'

    def test_declared_linux_only_skip_is_counted_as_not_run_outside_linux(self):
        names = {self.NAME, self.OTHER}
        problems, lines = self.m.selftest_skips(names, [(self.NAME, '/proc')], linux=False)
        self.assertEqual(problems, [])
        self.assertEqual(len(lines), 1)
        self.assertIn(self.NAME, lines[0])
        self.assertEqual(self.m.selftest_skips(names, [], linux=False), ([], []))
        self.assertEqual(self.m.selftest_skips(names, [], linux=True), ([], []))

    def test_undeclared_skip_linux_skip_and_missing_declaration_fail(self):
        names = {self.NAME, self.OTHER}
        checks = {
            '宣言の無い skip(Linux 以外)': (names, [(self.OTHER, 'x')], False),
            '宣言の無い skip(Linux)': (names, [(self.OTHER, 'x')], True),
            'Linux での skip': (names, [(self.NAME, '/proc')], True),
            '宣言した検査が無い(Linux 以外)': ({self.OTHER}, [], False),
            '宣言した検査が無い(Linux)': ({self.OTHER}, [], True),
        }
        for label, (given, skipped, linux) in checks.items():
            with self.subTest(label):
                problems, _ = self.m.selftest_skips(given, skipped, linux=linux)
                self.assertTrue(problems)

    def test_declared_names_exist_in_this_module(self):
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        names = {self.m.selftest_name(test) for test in self.m.selftest_tests(suite)}
        self.assertTrue(set(self.m.LINUX_ONLY_SELFTESTS) <= names)
        self.assertIn('SelftestSkips.test_declared_names_exist_in_this_module', names)


class MacExpectations(Base):
    """Mac だけで違う旧版の想定(expect_legacy_mac)と、絶対パスのまま扱う長さの上限。"""

    def test_mac_expectation_is_used_only_outside_linux(self):
        the_case = case(expect_legacy={'exit': 0}, expect_legacy_mac={'exit': 2, 'stderr_contains': 'File name too long'})
        ok = run(self.m)
        failed = run(self.m, status=('exit', 2), stderr='ERROR 出力を拒否: [Errno 63] File name too long: x\n'.encode())
        self.assertEqual(self.m.expect_problems(the_case, ok, self.paths, linux=True), [])
        self.assertTrue(self.m.expect_problems(the_case, failed, self.paths, linux=True))
        self.assertEqual(self.m.expect_problems(the_case, failed, self.paths, linux=False), [])
        self.assertTrue(self.m.expect_problems(the_case, ok, self.paths, linux=False))
        plain = case(expect_legacy={'exit': 0})
        self.assertEqual(self.m.expect_problems(plain, ok, self.paths, linux=False), [])
        self.assertTrue(self.m.expect_problems(plain, failed, self.paths, linux=False))

    def test_path_limit_is_below_the_os_path_max(self):
        self.assertLess(self.m.PATH_LIMIT, 4096 if self.m.IS_LINUX else 1024)


import unittest.mock  # noqa: E402  (Arguments で使う)


if __name__ == '__main__':
    unittest.main()
