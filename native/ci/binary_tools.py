"""実行せずに ELF を読み、権限を保つ tar を受け渡す。"""

import hashlib
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tarfile


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_static_elf(path, machine):
    data = Path(path).read_bytes()
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError("ELF64 little-endian の実行ファイルではありません")
    kind, actual, version = struct.unpack_from("<HHI", data, 16)
    if kind not in (2, 3) or actual != machine or version != 1:
        raise ValueError("ELF の CPU または形式が違います")
    offset = struct.unpack_from("<Q", data, 32)[0]
    entry_size, count = struct.unpack_from("<HH", data, 54)
    if entry_size != 56 or not count or offset + entry_size * count > len(data):
        raise ValueError("ELF のプログラムヘッダが壊れています")
    for index in range(count):
        position = offset + index * entry_size
        segment, _, start, _, _, size = struct.unpack_from("<IIQQQQ", data, position)
        if start + size > len(data):
            raise ValueError("ELF のセグメントがファイルの外にあります")
        if segment == 3:
            raise ValueError("ELF に動的ローダーの指定があります")
        if segment == 2:
            if size % 16:
                raise ValueError("ELF の動的情報が壊れています")
            terminated = False
            for item in range(start, start + size, 16):
                tag = struct.unpack_from("<q", data, item)[0]
                if tag == 0:
                    terminated = True
                    break
                if tag == 1:
                    raise ValueError("ELF に共有ライブラリへの依存があります")
            if not terminated:
                raise ValueError("ELF の動的情報に終端がありません")


def pack_tar(root, output, names):
    with tarfile.open(output, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name in names:
            path = Path(root) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(f"tar へ入れる物は通常ファイルに限ります: {name}")
            info = archive.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            with path.open("rb") as source:
                archive.addfile(info, source)


def unpack_tar(source, output, expected):
    """既知の名前だけを、新規フォルダへ取り出す。リンクは認めない。"""
    output = Path(output)
    output.mkdir()
    with tarfile.open(source, "r:") as archive:
        members = archive.getmembers()
        if len(members) != len(expected) or {m.name for m in members} != set(expected):
            raise ValueError("tar の項目に欠落・重複・余分な物があります")
        if sum(m.size for m in members) > 64 * 1024 * 1024:
            raise ValueError("tar の合計容量が上限を超えました")
        for member in members:
            parts = PurePosixPath(member.name).parts
            if (not member.isfile() or member.name.startswith("/")
                    or ".." in parts or member.size > 8 * 1024 * 1024
                    or member.mode not in (0o644, 0o755)):
                raise ValueError("tar の形式・容量・権限が不正です")
            destination = output.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            destination.chmod(member.mode)
    return output
