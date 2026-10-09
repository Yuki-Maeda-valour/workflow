"""#260: Gitの公開reftable書式から独立に作るref-block境界試験。

書式の根拠: Git Documentation/technical/reftable.adoc のheader/ref/footer。
製品の符号化関数を期待値の生成に使わず、Gitの形式変換も使わない。
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import zlib
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/dev-workflow/skills/do-task/scripts'


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / (name + '.py'))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def uint(value, size):
    return value.to_bytes(size, 'big')


def varint(value):
    result = [value & 127]
    while value >> 7:
        value = (value >> 7) - 1
        result.append(128 | (value & 127))
    return bytes(reversed(result))


def record(name, kind=1, value=None, delta=0):
    raw = b'\0' + varint((len(name) << 3) | kind) + name + varint(delta)
    if kind in (1, 2): raw += value
    elif kind == 3: raw += varint(len(value)) + value
    return raw


def ref_block(records, header=b'', kind=b'r'):
    body = b''; offsets = []
    for item in records:
        offsets.append(len(header) + 4 + len(body)); body += item
    restarts = b''.join(uint(offset, 3) for offset in offsets) + uint(len(offsets), 2)
    length = len(header) + 4 + len(body) + len(restarts)
    return header + kind + uint(length, 3) + body + restarts


def table(blocks=(), *, version=1, fmt=b'sha1', index=1, alignment=0, fields=None):
    header = b'REFT' + bytes([version]) + uint(alignment, 3) + uint(index, 8)*2
    if version == 2: header += fmt
    if not blocks:
        data = header
    else:
        chunks = []
        for i, records in enumerate(blocks):
            chunk = ref_block(records, header if i == 0 else b'')
            if i < len(blocks)-1:
                assert alignment >= len(chunk)
                chunk += b'\0'*(alignment-len(chunk))
            chunks.append(chunk)
        data = b''.join(chunks)
    footer = header + b''.join(uint(value, 8) for value in (fields or [0]*5))
    return data + footer + uint(zlib.crc32(footer), 4)


class DiagnosticReftableTests(unittest.TestCase):
    def setUp(self):
        self.reader = module('diagnostic-reftable')
        self.runtime = module('diagnostic-runtime')
        self.temp = tempfile.TemporaryDirectory(prefix='diagnostic-table-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def stack(self, tables):
        names = []
        for i, content in enumerate(tables):
            name = 'table-%d.ref' % i; names.append(name)
            (self.root / name).write_bytes(content)
        (self.root / 'tables.list').write_text(''.join(name+'\n' for name in names))

    def read(self, fmt='sha1', budget=None):
        return self.reader.read_stack(os.fsencode(self.root), fmt, budget or self.runtime.Budget())

    def test_versions_hash_formats_and_non_utf8_symbolic_target(self):
        for version, fmt, size in [(1, 'sha1', 20), (2, 'sha1', 20), (2, 'sha256', 32)]:
            with self.subTest(version=version, fmt=fmt):
                value = b'\x12'*size
                name, target = b'refs/heads/\xff', b'refs/heads/\xfe'
                self.stack([table([[record(b'refs/heads/a', value=value), record(name, 3, target)]],
                                  version=version, fmt=b's256' if size == 32 else b'sha1')])
                self.assertEqual(self.read(fmt), {b'refs/heads/a': (value.hex(), None), name: (None, target)})

    def test_newest_record_and_tombstone_win_without_losing_other_refs(self):
        self.stack([table([[record(b'a', value=b'1'*20), record(b'b', value=b'2'*20)]], index=1),
                    table([[record(b'a', 0), record(b'b', value=b'3'*20)]], index=2)])
        self.assertEqual(self.read(), {b'b': ((b'3'*20).hex(), None)})

    def test_empty_stack_empty_table_and_aligned_multiple_blocks(self):
        self.stack([]); self.assertEqual(self.read(), {})
        self.stack([table()]); self.assertEqual(self.read(), {})
        self.stack([table([[record(b'a', value=b'1'*20)], [record(b'b', value=b'2'*20)]], alignment=256)])
        self.assertEqual(set(self.read()), {b'a', b'b'})

    def test_complete_log_only_table_does_not_invent_refs(self):
        for version, fmt in [(1, 'sha1'), (2, 'sha256')]:
            with self.subTest(format=fmt):
                header = table(version=version, fmt=b's256' if version == 2 else b'sha1')[:24 if version == 1 else 28]
                key = b'refs/heads/main\0' + uint((1 << 64)-2, 8)
                entry = b'\0' + varint(len(key) << 3) + key  # 削除log record、本文なし。
                expanded = entry + uint(len(header)+4, 3) + uint(1, 2)
                log = header + b'g' + uint(len(header)+4+len(expanded), 3) + zlib.compress(expanded)
                footer = header + b'\0'*40
                self.stack([log + footer + uint(zlib.crc32(footer), 4)])
                self.assertEqual(self.read(fmt), {})

    def test_unaligned_multi_block_and_two_level_ref_index(self):
        header = table()[:24]
        first = ref_block([record(b'a', value=b'1'*20)], header)
        second = ref_block([record(b'b', value=b'2'*20)])
        lower_position = len(first)+len(second)
        def index_entry(name, offset):
            return b'\0' + varint(len(name) << 3) + name + varint(offset)
        lower = ref_block([index_entry(b'a', 0), index_entry(b'b', len(first))], kind=b'i')
        for upper in (False, True):
            with self.subTest(two_levels=upper):
                body = first+second+lower
                index_position = lower_position
                if upper:
                    index_position = len(body)
                    body += ref_block([index_entry(b'b', lower_position)], kind=b'i')
                footer = header + uint(index_position, 8) + b'\0'*32
                self.stack([body+footer+uint(zlib.crc32(footer), 4)])
                self.assertEqual(self.read(), {b'a': ((b'1'*20).hex(), None), b'b': ((b'2'*20).hex(), None)})

    def test_every_truncation_of_valid_table_is_an_explicit_failure(self):
        good = table([[record(b'refs/heads/a', value=b'a'*20)]])
        for cut in range(len(good)):
            with self.subTest(cut=cut):
                self.stack([good[:cut]])
                with self.assertRaises(self.reader.ReftableError): self.read()

    def test_more_than_2000_refs_are_not_metadata_file_limit(self):
        records = [record(('refs/heads/r%04d' % n).encode(), value=b'a'*20) for n in range(2001)]
        self.stack([table([records])])
        self.assertEqual(len(self.read()), 2001)

    def test_footer_crc_header_mismatch_hash_and_section_bounds_fail(self):
        good = table([[record(b'a', value=b'1'*20)]])
        crc = good[:-1] + bytes([good[-1] ^ 1])
        header = b'BAD!' + good[4:]
        section = table([[record(b'a', value=b'1'*20)]], fields=[999999, 0, 0, 0, 0])
        for broken in (crc, header, section, good[:4], good[:-20]):
            with self.subTest(size=len(broken)):
                self.stack([broken])
                with self.assertRaises(self.reader.ReftableError): self.read()
        self.stack([good])
        with self.assertRaises(self.reader.ReftableError): self.read('sha256')

    def test_bad_restart_prefix_unknown_value_and_unbounded_varint_fail(self):
        good = table([[record(b'a', value=b'1'*20)]])
        # footerは変更しない。block内の矛盾をCRCだけで検出した扱いにしない。
        footer_size = 68
        restart = bytearray(good); restart[-footer_size-5:-footer_size-2] = uint(1, 3)
        prefix = bytearray(good); prefix[28] = 1
        unknown = table([[record(b'a', kind=7)]])
        excessive = table([[b'\xff'*11 + b'\0']])
        for broken in (bytes(restart), bytes(prefix), unknown, excessive):
            with self.subTest(size=len(broken)):
                self.stack([broken])
                with self.assertRaises(self.reader.ReftableError): self.read()

    def test_update_order_list_grammar_missing_table_and_symlink_fail(self):
        self.stack([table(index=2), table(index=1)])
        with self.assertRaises(self.reader.ReftableError): self.read()
        for raw in (b'../outside\n', b'table-0.ref\ntable-0.ref\n', b'table-0.ref', b'\n'):
            (self.root / 'tables.list').write_bytes(raw)
            with self.assertRaises(self.reader.ReftableError): self.read()
        self.stack([table()]); (self.root / 'table-0.ref').unlink()
        with self.assertRaises(self.reader.ReftableError): self.read()
        target = self.root / 'other'; target.write_bytes(table())
        (self.root / 'table-0.ref').symlink_to(target)
        with self.assertRaises(self.reader.ReftableError): self.read()

    def test_budget_is_charged_and_short_read_fails(self):
        self.stack([table([[record(b'a', value=b'1'*20)]])])
        limits = dict(self.runtime.LIMITS); limits['management_output'] = 1
        with self.assertRaises(self.runtime.Failure): self.read(budget=self.runtime.Budget(limits=limits))
        with mock.patch.object(self.reader.os, 'read', return_value=b''):
            with self.assertRaises(self.reader.ReftableError): self.read()

    def test_changed_file_after_read_is_detected(self):
        self.stack([table([[record(b'a', value=b'1'*20)]])])
        original = self.reader._Input.verify
        changed = [False]
        def verify(instance):
            if not changed[0]:
                changed[0] = True
                (self.root / 'table-0.ref').chmod(0o700)
            return original(instance)
        with mock.patch.object(self.reader._Input, 'verify', verify):
            with self.assertRaises(self.reader.ReftableError): self.read()


if __name__ == '__main__':
    unittest.main()
