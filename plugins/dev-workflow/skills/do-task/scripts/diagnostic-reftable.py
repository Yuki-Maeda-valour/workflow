#!/usr/bin/env python3
"""隔離済み reftable の ref block を有界に列挙する内部モジュール。

元の管理領域を渡さないこと。名前・symbolic target は bytes のまま返し、
名前の文法、symbolic graph、linked worktree の合成は呼出側で検査する。
log/object section の本文は解釈せず、その履歴・索引の健全性は保証しない。
"""

import os
import stat
import zlib


MAX_INPUT = 200 * 1024 * 1024
MAX_REF = 1024
MAX_UINT64 = (1 << 64) - 1
READ_CHUNK = 64 * 1024


class ReftableError(Exception):
    """入力値を含まない固定 reason と診断用終了値。"""

    def __init__(self, reason, code=23):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def _require(condition, reason="invalid-reftable"):
    if not condition:
        raise ReftableError(reason)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _uint(data):
    return int.from_bytes(data, "big")


class _Input:
    """親 fd に結び付けた通常ファイル。読取りの前後で同一性を確認する。"""

    def __init__(self, directory_fd, name, budget, totals):
        self.fd = None
        self.directory_fd = directory_fd
        self.name = name
        self.budget = budget
        budget.check()
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _require(stat.S_ISREG(before.st_mode), "invalid-reftable-file")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
        try:
            self.initial = os.fstat(fd)
            _require(_signature(before) == _signature(self.initial),
                     "changed-reftable")
            totals[0] += self.initial.st_size
            if self.initial.st_size < 0 or totals[0] > MAX_INPUT:
                raise ReftableError("management-input-limit", 20)
            self.size = self.initial.st_size
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, offset, count):
        self.budget.check()
        _require(0 <= offset <= self.size and 0 <= count <= self.size - offset)
        os.lseek(self.fd, offset, os.SEEK_SET)
        result = bytearray()
        while len(result) < count:
            self.budget.check()
            part = os.read(self.fd, min(READ_CHUNK, count - len(result)))
            _require(bool(part), "changed-reftable")
            result.extend(part)
        self.budget.check()
        return bytes(result)

    def verify(self):
        self.budget.check()
        _require(_signature(os.fstat(self.fd)) == _signature(self.initial),
                 "changed-reftable")
        _require(_signature(os.stat(self.name, dir_fd=self.directory_fd,
                                    follow_symlinks=False)) == _signature(self.initial),
                 "changed-reftable")

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)


class _Records:
    """1 block の restart 配列と、境界を持つレコード読取り。"""

    def __init__(self, data, header_size, budget):
        self.data = data
        self.budget = budget
        self.position = header_size + 4
        _require(len(data) >= self.position + 6)
        count = _uint(data[-2:])
        self.end = len(data) - 2 - count * 3
        _require(count > 0 and self.end > self.position)
        self.restarts = []
        previous = -1
        for offset in range(self.end, len(data) - 2, 3):
            budget.check()
            value = _uint(data[offset:offset + 3])
            _require(self.position <= value < self.end and value > previous)
            self.restarts.append(value)
            previous = value
        _require(self.restarts[0] == self.position)
        self.restart_index = 0
        self.previous = b""

    def take(self, length):
        _require(0 <= length <= self.end - self.position)
        start = self.position
        self.position += length
        return self.data[start:self.position]

    def varint(self):
        value = 0
        for unused in range(10):
            byte = self.take(1)[0]
            value = (value << 7) | (byte & 127)
            _require(value <= MAX_UINT64)
            if not byte & 128:
                return value
            value += 1
            _require(value <= MAX_UINT64)
        raise ReftableError("invalid-reftable-varint")

    def key(self):
        self.budget.check()
        start = self.position
        prefix = self.varint()
        length_and_type = self.varint()
        suffix_length = length_and_type >> 3
        _require(prefix <= len(self.previous) and
                 0 < prefix + suffix_length <= MAX_REF)
        name = self.previous[:prefix] + self.take(suffix_length)
        _require(name > self.previous)
        if self.restart_index < len(self.restarts):
            restart = self.restarts[self.restart_index]
            _require(start <= restart)
            if start == restart:
                _require(prefix == 0)
                self.restart_index += 1
        self.previous = name
        return name, length_and_type & 7

    def finish(self):
        _require(self.position == self.end and
                 self.restart_index == len(self.restarts))


def _block(source, position, header_size, limit, alignment, expected):
    """非圧縮 block と次位置。最終 block の padding 省略も受理する。"""
    _require(position + header_size + 4 <= limit)
    header = source.read(position + header_size, 4)
    _require(header[0] == expected)
    length = _uint(header[1:])
    _require(header_size + 10 <= length <= limit - position)
    if expected == ord("r") and alignment:
        _require(length <= alignment)
    data = source.read(position, length)
    end = position + length
    next_position = end
    if end < limit and source.read(end, 1) == b"\0":
        _require(alignment > length)
        next_position = position + alignment
        _require(next_position <= limit)
        offset = end
        while offset < next_position:
            source.budget.check()
            count = min(READ_CHUNK, next_position - offset)
            _require(not any(source.read(offset, count)))
            offset += count
    return data, next_position


def _section_header(source, position, expected, limit):
    _require(position + 4 <= limit)
    header = source.read(position, 4)
    _require(header[0] == expected)
    length = _uint(header[1:])
    _require(length >= 10)
    # log の block_len は展開後の長さなので、圧縮 bytes の長さと比較しない。
    if expected != ord("g"):
        _require(length <= limit - position)


def _table(source, hash_format, merged):
    _require(source.size >= 5)
    prefix = source.read(0, 5)
    _require(prefix[:4] == b"REFT" and prefix[4] in (1, 2))
    header_size = 24 if prefix[4] == 1 else 28
    footer_size = header_size + 44
    _require(source.size >= header_size + footer_size)
    header = source.read(0, header_size)
    footer_position = source.size - footer_size
    footer = source.read(footer_position, footer_size)
    _require(footer[:header_size] == header)
    _require(zlib.crc32(footer[:-4]) == _uint(footer[-4:]))
    actual_hash = b"sha1" if prefix[4] == 1 else header[24:28]
    _require(actual_hash in (b"sha1", b"s256"))
    _require(actual_hash == {"sha1": b"sha1", "sha256": b"s256"}[hash_format],
             "reftable-hash-mismatch")
    hash_size = 20 if hash_format == "sha1" else 32
    alignment = _uint(header[5:8])
    minimum, maximum = _uint(header[8:16]), _uint(header[16:24])
    _require(minimum <= maximum)
    fields = [_uint(footer[i:i + 8])
              for i in range(header_size, header_size + 40, 8)]
    ref_index, packed_object, object_index, log_position, log_index = fields
    object_position, object_length = packed_object >> 5, packed_object & 31
    positions = [ref_index, object_position, object_index, log_position, log_index]
    nonzero = [value for value in positions if value]
    _require(all(header_size <= value < footer_position for value in nonzero))
    _require(all(left < right for left, right in zip(nonzero, nonzero[1:])))
    _require((2 <= object_length <= min(31, hash_size)) if object_position
             else object_length == 0)
    _require(not object_index or object_position)
    if footer_position == header_size:
        _require(not any(positions))
        return minimum, maximum
    first = source.read(header_size, 1)
    _require(first in (b"r", b"g"))
    if first == b"g":
        _require(not any((ref_index, object_position, object_index, log_position)))
        _section_header(source, header_size, ord("g"), footer_position)
    else:
        _require(not log_index or log_position)
    destinations = ((ref_index, ord("i")), (object_position, ord("o")),
                    (object_index, ord("i")), (log_position, ord("g")),
                    (log_index, ord("i")))
    for position, expected in destinations:
        if position:
            # 次の宣言済み section に block が食い込むことを禁止する。
            limit = min([value for value in nonzero if value > position] +
                        [footer_position])
            _section_header(source, position, expected, limit)
    if first == b"g":
        return minimum, maximum

    limit = min([value for value in (object_position, log_position) if value] +
                [footer_position])
    position = 0
    last_name = b""
    block_keys = {}
    ref_blocks = 0
    saw_index = False
    saw_root_index = False
    while position < limit:
        source.budget.check()
        skip = header_size if position == 0 else 0
        _require(position + skip < limit)
        tag = source.read(position + skip, 1)[0]
        _require(tag in (ord("r"), ord("i")))
        if tag == ord("r"):
            _require(not saw_index and (not ref_index or position < ref_index))
            ref_blocks += 1
        else:
            _require(ref_index and ref_blocks > 0)
            saw_index = True
            saw_root_index = saw_root_index or position == ref_index
        data, next_position = _block(source, position, skip, limit, alignment, tag)
        records = _Records(data, skip, source.budget)
        while records.position < records.end:
            name, value_type = records.key()
            if tag == ord("i"):
                _require(value_type == 0)
                child = records.varint()
                _require(child < position and child in block_keys and
                         block_keys[child] == name)
                continue
            _require(name > last_name)
            last_name = name
            delta = records.varint()
            _require(delta <= maximum - minimum)
            oid, target = None, None
            if value_type in (1, 2):
                oid = records.take(hash_size).hex()
                if value_type == 2:
                    records.take(hash_size)
            elif value_type == 3:
                length = records.varint()
                _require(0 < length <= MAX_REF)
                target = records.take(length)
            else:
                _require(value_type == 0)
            source.budget.charge("management_output", len(name) +
                                 (len(oid) if oid else 0) +
                                 (len(target) if target else 0) + 32)
            if value_type == 0:
                merged.pop(name, None)
            else:
                merged[name] = (oid, target)
        records.finish()
        block_keys[position] = records.previous
        _require(next_position > position)
        position = next_position
    _require(position == limit and bool(ref_index) == saw_root_index)
    _require(alignment or ref_blocks <= 1 or ref_index)
    return minimum, maximum


def read_stack(directory_bytes, hash_format, budget):
    """私有コピーの stack を古い table から合成する。Git は起動しない。

    budget.check()/charge('management_output', bytes) の例外はそのまま伝播する。
    malformed table は ReftableError(code=23)、I/O/入力上限は code=20。
    stack 不在を空集合と推定しない。空 stack は空の tables.list で表す。
    """
    _require(isinstance(directory_bytes, bytes), "invalid-reftable-directory")
    _require(hash_format in ("sha1", "sha256"), "reftable-hash-mismatch")
    directory_fd = None
    listing = None
    tables = []
    try:
        budget.check()
        before = os.stat(directory_bytes, follow_symlinks=False)
        _require(stat.S_ISDIR(before.st_mode), "invalid-reftable-directory")
        directory_fd = os.open(directory_bytes,
                               os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        original = os.fstat(directory_fd)
        _require(_signature(before) == _signature(original), "changed-reftable")
        totals = [0]
        listing = _Input(directory_fd, b"tables.list", budget, totals)
        raw_list = listing.read(0, listing.size)
        _require(not raw_list or raw_list.endswith(b"\n"), "invalid-reftable-list")
        seen = set()
        merged = {}
        previous_maximum = None
        cursor = 0
        while cursor < len(raw_list):
            budget.check()
            newline = raw_list.find(b"\n", cursor)
            _require(newline >= 0 and newline - cursor <= 4096,
                     "invalid-reftable-list")
            name = raw_list[cursor:newline]
            cursor = newline + 1
            _require(name not in (b"", b".", b"..", b"tables.list") and
                     b"/" not in name and b"\0" not in name and
                     name not in seen,
                     "invalid-reftable-list")
            seen.add(name)
            table = _Input(directory_fd, name, budget, totals)
            tables.append(table)
            minimum, maximum = _table(table, hash_format, merged)
            _require(previous_maximum is None or minimum > previous_maximum,
                     "invalid-reftable-stack-order")
            previous_maximum = maximum
        for table in tables:
            table.verify()
        listing.verify()
        _require(_signature(os.fstat(directory_fd)) == _signature(original),
                 "changed-reftable")
        _require(_signature(os.stat(directory_bytes, follow_symlinks=False)) ==
                 _signature(original), "changed-reftable")
        budget.check()
        return merged
    except FileNotFoundError as error:
        raise ReftableError("incomplete-metadata") from error
    except OSError as error:
        raise ReftableError("reftable-io", 20) from error
    finally:
        close_error = None
        for item in tables + ([listing] if listing is not None else []):
            try:
                item.close()
            except OSError as error:
                close_error = error
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError as error:
                close_error = error
        if close_error is not None:
            raise ReftableError("reftable-io", 20) from close_error
