#!/usr/bin/env python3
"""無人ループのホスト CLI の証明(H31)と、skill の allowed-tools による権限の追加の検出(H34)。

標準ライブラリだけで動く。ホスト CLI を起動しない(起動は loop.sh が行い、出力のファイルをここで照合する)。

  identity    --host <パス>                       実体(realpath・dev・ino・size・mtime・ctime・sha256)を JSON で出す
  same        --host <パス> --identity <JSON> [--hash]
                                                   実行する直前の実体が保持値と同じか(--hash で内容まで)
  proof-get   --store <dir> --identity <JSON> --shape <起動の形>
                                                   実体と起動の形の証明を読む(無ければ exit 3。ホストを起動しない)
  proof-match --store <dir> --identity <JSON> --shape <起動の形> --version-file <f> --help-file <f>
                                                   --version・--help の出力が証明と同じか
  probe-judge --out <f> --permlog <f> --target <パス>
                                                   実 hook の確認の結果を判定する(hook が Write を拒否した証拠)
  proof-write --store <dir> --identity <JSON> --shape <起動の形> --version-file <f> --help-file <f> --probe <JSON> --plugin-version <v>
  grants      --state <environment.json> --expect-sha256 <sha> --user-settings <パス> [--allowed-tools <値> ...] [--classifier]
                                                   skill・command の frontmatter の allowed-tools が実効許可リストに含まれるか

終了コード: 0 = 通る / 3 = 証明が無い / 20 = 止める(理由は stderr の `ERROR [host-check] …`)/ 2 = 使い方の誤り
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

MAX_BINARY = 2 * 1024 * 1024 * 1024
MAX_TEXT = 4 * 1024 * 1024
MAX_STATE = 64 * 1024 * 1024
MAX_PROOF = 64 * 1024
MAX_SECONDS = 120.0
PROOF_VERSION = 2
PROOF_BOUND = ('realpath', 'dev', 'ino', 'size', 'sha256')
INIT_SCHEMA_VERSION = 1
RESOLVER_VERSION = 1


class Stop(RuntimeError):
    pass


class Missing(RuntimeError):
    pass


# ── 安全な読み取り(親ディレクトリを含めて nofollow の fd で開く)──

def _split(path):
    if not isinstance(path, str) or not path.startswith('/'):
        raise Stop('絶対パスが必要')
    parts = path.split('/')[1:]
    if not parts or any(p in ('', '.', '..') for p in parts):
        raise Stop('正規化されていないパス')
    return parts


def open_dir_chain(parts):
    """'/' から parts を nofollow で辿ったディレクトリの fd を返す。"""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def file_identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def read_at(dir_fd, name, limit):
    before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise Stop('通常ファイルでない')
    if before.st_size > limit:
        raise Stop('読み取りの上限を超える')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if file_identity(os.fstat(fd)) != file_identity(before):
            raise Stop('開く間に変わった')
        chunks, left = [], before.st_size
        while left:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                raise Stop('読み取り中に短くなった')
            chunks.append(chunk)
            left -= len(chunk)
        if os.read(fd, 1):
            raise Stop('読み取り中に大きくなった')
        if file_identity(os.fstat(fd)) != file_identity(before) or \
                file_identity(os.stat(name, dir_fd=dir_fd, follow_symlinks=False)) != file_identity(before):
            raise Stop('読み取り中に変わった')
        return b''.join(chunks)
    finally:
        os.close(fd)


def read_regular(path, limit=MAX_TEXT):
    parts = _split(path)
    fd = open_dir_chain(parts[:-1])
    try:
        return read_at(fd, parts[-1], limit)
    finally:
        os.close(fd)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


# ── H31: 実体の同一性 ──

def identity(path):
    if not isinstance(path, str) or not path.startswith('/'):
        raise Stop('ホスト CLI は絶対パスで渡す')
    real = os.path.realpath(path)
    parts = _split(real)
    fd = open_dir_chain(parts[:-1])
    try:
        before = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111:
            raise Stop('ホスト CLI の実体が実行できる通常ファイルでない')
        if before.st_size > MAX_BINARY:
            raise Stop('ホスト CLI の実体が大きすぎる')
        exe = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if file_identity(os.fstat(exe)) != file_identity(before):
                raise Stop('ホスト CLI の実体が開く間に変わった')
            digest, left, deadline = hashlib.sha256(), before.st_size, time.monotonic() + MAX_SECONDS
            head = b''
            while left:
                if time.monotonic() > deadline:
                    raise Stop('ホスト CLI の実体の hash が時間の上限を超えた')
                chunk = os.read(exe, min(left, 1 << 20))
                if not chunk:
                    raise Stop('ホスト CLI の実体が読み取り中に短くなった')
                digest.update(chunk)
                if len(head) < 4:
                    head += chunk[:4 - len(head)]
                left -= len(chunk)
            if os.read(exe, 1):
                raise Stop('ホスト CLI の実体が読み取り中に大きくなった')
            after = os.fstat(exe)
        finally:
            os.close(exe)
        again = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    if file_identity(after) != file_identity(before) or file_identity(again) != file_identity(before):
        raise Stop('ホスト CLI の実体が読み取り中に変わった')
    return {'realpath': real, 'dev': before.st_dev, 'ino': before.st_ino, 'mode': before.st_mode,
            'size': before.st_size, 'mtime_ns': before.st_mtime_ns, 'ctime_ns': before.st_ctime_ns,
            'sha256': digest.hexdigest(), 'kind': 'elf' if head == b'\x7fELF' else ('script' if head[:2] == b'#!' else 'other')}


IDENTITY_KEYS = ('realpath', 'dev', 'ino', 'mode', 'size', 'mtime_ns', 'ctime_ns', 'sha256', 'kind')


def parse_identity(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Stop('保持した実体の値を読めない') from None
    if not isinstance(value, dict) or set(value) != set(IDENTITY_KEYS):
        raise Stop('保持した実体の値の形が違う')
    if not isinstance(value['realpath'], str) or not re.fullmatch('[0-9a-f]{64}', str(value['sha256'])):
        raise Stop('保持した実体の値の形が違う')
    if value['kind'] not in ('elf', 'script', 'other'):
        raise Stop('保持した実体の値の形が違う')
    for key in IDENTITY_KEYS[1:-2]:
        if type(value[key]) is not int:
            raise Stop('保持した実体の値の形が違う')
    return value


def same(path, held, full):
    """実行する直前に、実体が保持値と同じかを確かめる。path は保持した realpath そのもの。"""
    if path != held['realpath'] or os.path.realpath(path) != held['realpath']:
        raise Stop('ホスト CLI の実体のパスが変わった')
    if full:
        now = identity(path)
    else:
        parts = _split(path)
        fd = open_dir_chain(parts[:-1])
        try:
            st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        finally:
            os.close(fd)
        now = dict(held, dev=st.st_dev, ino=st.st_ino, mode=st.st_mode, size=st.st_size,
                   mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns)
    for key in IDENTITY_KEYS:
        if now[key] != held[key]:
            raise Stop(f'ホスト CLI の実体が確認した時と違う({key})')


# ── H31: 証明 ──

def proof_file(store, held, shape):
    """証明のファイル名は、結び付ける値(実体と起動の形)から決める。同じ内容の別の置き場・別の形は別の証明になる。"""
    if not re.fullmatch('[0-9a-f]{64}', held['sha256']):
        raise Stop('sha256 の形が違う')
    key = json.dumps([held[k] for k in PROOF_BOUND] + [shape], ensure_ascii=True)
    return store.rstrip('/') + '/' + sha256(key.encode()) + '.json'


def shape_digest(shape):
    return sha256(shape.encode('utf-8'))


def load_proof(store, held, shape, component_digest=None, policy_digest=None):
    path = proof_file(store, held, shape)
    try:
        raw = read_regular(path, MAX_PROOF)
    except FileNotFoundError:
        raise Missing(path) from None
    try:
        value = json.loads(raw)
    except ValueError:
        raise Stop(f'証明を読めない: {path}') from None
    if not isinstance(value, dict) or value.get('version') != PROOF_VERSION:
        raise Stop(f'証明の形が違う: {path}')
    for key in PROOF_BOUND:
        if value.get(key) != held[key]:
            raise Stop(f'証明の実体({key})が今のホスト CLI と違う。同じ内容でも置き場か inode が変わったら確かめ直す: {path}')
    if value.get('shape_sha256') != shape_digest(shape):
        raise Stop(f'証明を取った時と起動の形(権限のモード・隔離と hook のフラグ)が違う: {path}')
    for key in ('host_version_sha256', 'help_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', str(value.get(key))):
            raise Stop(f'証明の形が違う({key}): {path}')
    # H46 からは、同じ host と argv でも読込む component 又は固定方針が
    # 変われば証明を再利用しない。旧形式は version でここまで到達せず拒否する。
    for key in ('component_sha256', 'policy_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', str(value.get(key))):
            raise Stop(f'証明の形が違う({key}): {path}')
    if value.get('init_schema') != INIT_SCHEMA_VERSION:
        raise Stop(f'証明の init の形が違う: {path}')
    proof_names(value.get('public_names'))
    resolver_version_check(value.get('resolver_version'))
    init_evidence_check(value.get('init'), value.get('public_names'))
    for name, got in (('component_sha256', component_digest), ('policy_sha256', policy_digest)):
        if got is not None and value[name] != digest_value(got, name):
            raise Stop(f'証明を取った時と {name} が違う: {path}')
    probe = value.get('probe')
    if not isinstance(probe, dict) or probe.get('hook') != 'invoked' or probe.get('decision') != 'deny' \
            or probe.get('tool') != 'Write' or probe.get('file_absent') is not True:
        raise Stop(f'証明に実 hook の拒否の記録が無い: {path}')
    return path, value


def output_digest(path):
    return sha256(read_regular(path, MAX_TEXT))


def proof_match(store, held, shape, version_file, help_file, component_digest=None, policy_digest=None):
    path, value = load_proof(store, held, shape, component_digest, policy_digest)
    if output_digest(version_file) != value['host_version_sha256']:
        raise Stop(f'--version の出力が確認した時と違う(証明: {path})')
    if output_digest(help_file) != value['help_sha256']:
        raise Stop(f'--help の出力が確認した時と違う(証明: {path})')
    return path, value


def write_new_file(directory, name, raw):
    parts = _split(directory)
    fd = open_dir_chain(parts)
    try:
        tmp = f'.{name}.{os.getpid()}.tmp'
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(out, view):]
            os.fsync(out)
        finally:
            os.close(out)
        os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_store(store):
    parts = _split(store)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts:
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def digest_value(value, label):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise Stop(f'{label} の形が違う')
    return value


def proof_write(store, held, shape, version_file, help_file, probe, plugin_version,
                component_digest, policy_digest, public_names, resolver_version, init_evidence):
    if not isinstance(probe, dict) or probe.get('hook') != 'invoked' or probe.get('decision') != 'deny' or \
            probe.get('tool') != 'Write' or probe.get('file_absent') is not True:
        raise Stop('証明に実 hook の拒否の記録が無い')
    version_raw = read_regular(version_file, MAX_TEXT)
    record = {
        'version': PROOF_VERSION,
        'sha256': held['sha256'],
        'size': held['size'],
        'realpath': held['realpath'],
        'dev': held['dev'],
        'ino': held['ino'],
        'shape': shape,
        'shape_sha256': shape_digest(shape),
        'host_version': version_raw.decode('utf-8', 'replace').strip()[:200],
        'host_version_sha256': sha256(version_raw),
        'help_sha256': output_digest(help_file),
        'kind': held['kind'],
        'component_sha256': digest_value(component_digest, 'component_sha256'),
        'policy_sha256': digest_value(policy_digest, 'policy_sha256'),
        'init_schema': INIT_SCHEMA_VERSION,
        'public_names': sorted(proof_names(public_names)),
        'resolver_version': resolver_version_check(resolver_version),
        'init': init_evidence_check(init_evidence, public_names),
        'probe': dict(probe, date=time.strftime('%Y-%m-%dT%H:%M:%S%z'), plugin_version=plugin_version),
    }
    ensure_store(store)
    path = proof_file(store, held, shape)
    write_new_file(store.rstrip('/'), os.path.basename(path), (json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode())
    return path


def proof_names(value):
    if not isinstance(value, (list, tuple)) or len(set(value)) != len(value) or \
            not all(isinstance(name, str) and NAME.fullmatch(name) for name in value):
        raise Stop('public_names の形が違う')
    return list(value)


def resolver_version_check(value):
    if type(value) is not int or value != RESOLVER_VERSION:
        raise Stop('resolver_version の形が違う')
    return value


def init_evidence_check(value, public_names):
    if not isinstance(value, dict) or set(value) != {'init_schema', 'public_names'} or \
            value.get('init_schema') != INIT_SCHEMA_VERSION or \
            sorted(proof_names(value.get('public_names'))) != sorted(proof_names(public_names)):
        raise Stop('init の縮約証拠の形が違う')
    return {'init_schema': INIT_SCHEMA_VERSION, 'public_names': sorted(value['public_names'])}


def result_object(raw):
    value = json.loads(raw)
    if isinstance(value, dict) and set(value) == {'init', 'result'}:
        value = value['result']
    if isinstance(value, list):
        results = [v for v in value if isinstance(v, dict) and v.get('type') == 'result']
        if not results:
            raise Stop('結果の JSON に result が無い')
        value = results[-1]
    if not isinstance(value, dict):
        raise Stop('結果の JSON がオブジェクトでない')
    return value


def one_line(text, limit=200):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(text))[:limit]


def probe_judge(out, permlog, target):
    """実 hook の確認: ①許可の仲介の記録に Write の拒否 ②結果の拒否の欄に同じ Write ③対象が無い。"""
    if os.path.lexists(target):
        raise Stop('確認で書かせる先のファイルができた(hook を通らずに書けた)')
    try:
        result = result_object(read_regular(out, MAX_TEXT))
    except (OSError, ValueError, UnicodeError):
        raise Stop('ホスト CLI の結果の JSON を読めない(認証の失敗・時間切れ・起動の失敗を含む)') from None
    if result.get('is_error') is True:
        raise Stop('ホスト CLI が失敗を返した: ' + one_line(result.get('result') or result.get('subtype')))
    denials = result.get('permission_denials')
    if not isinstance(denials, list) or not any(
            isinstance(d, dict) and d.get('tool_name') == 'Write' and isinstance(d.get('tool_input'), dict)
            and d['tool_input'].get('file_path') == target for d in denials):
        raise Stop('結果の拒否の欄に、確認の Write が無い(モデルが Write を試みなかったか、拒否されなかった)')
    try:
        raw = read_regular(permlog, MAX_TEXT)
    except FileNotFoundError:
        raise Stop('許可の仲介の hook が呼ばれていない(判定の記録が無い)') from None
    hooked = False
    for line in raw.decode('utf-8', 'replace').splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            raise Stop('許可の仲介の判定の記録を読めない') from None
        if not isinstance(entry, dict):
            raise Stop('許可の仲介の判定の記録の形が違う')
        if entry.get('tool_name') == 'Write' and entry.get('subject') == target:
            if entry.get('decision') != 'deny':
                raise Stop('許可の仲介の hook が確認の Write を許した')
            hooked = True
    if not hooked:
        raise Stop('許可の仲介の hook が確認の Write で呼ばれていない')
    return {'tool': 'Write', 'hook': 'invoked', 'decision': 'deny', 'file_absent': True}


# ── H34: frontmatter の allowed-tools ──

# 鍵の字面。ホストが鍵を `-`・`_` を除いて小文字で比べる読み方もありうるので、その形の変種もすべて拾う
KEY_GUARD = re.compile(r'(?<![A-Za-z])' + '[-_]*'.join('allowedtools'), re.I)
# ホストの frontmatter の切り出し方(実行ファイルの文字列から読んだ 2 通り。どちらを使うかは未確認)。
# 行の形で読んだ範囲と食い違い、鍵の字面かエスケープがあれば、解釈が分かれうるとして止める
# JS の \s は U+FEFF を含み、Python の \s は含まないので足す
HOST_FRONTMATTER = (re.compile(r'---[\s\ufeff]*\n(.*?)---', re.S),
                    re.compile(r'---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)', re.S))
INDICATORS = set('"\'{[?&*!<%@`|>-:,#')
GRANT_KEY = 'allowed-tools'


class Unclear(Stop):
    pass


def split_rules(value):
    # loop.sh の split_rules と同じ(カンマか空白の区切り。括弧の中の空白・カンマは区切らない)
    rules, cur, depth = [], '', 0
    for ch in value:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        if depth == 0 and (ch == ',' or ch.isspace()):
            if cur:
                rules.append(cur)
            cur = ''
            continue
        cur += ch
    if cur:
        rules.append(cur)
    return rules


def host_split(value):
    """ホスト(2.1.289)の切り方を写したもの(静的に読んだ。括弧の中かどうかを真偽の 1 つで持ち、`(` で真・`)` で偽にし、
    括弧の外の ' ' と ',' だけで区切る)。split_rules(括弧の深さを数える)と結果が違う値は、読み方が 2 通りある。"""
    rules, cur, inside = [], '', False
    for ch in value:
        if ch == '(':
            inside = True
        elif ch == ')':
            inside = False
        if not inside and ch in ' ,':
            if cur.strip():
                rules.append(cur.strip())
            cur = ''
            continue
        cur += ch
    if cur.strip():
        rules.append(cur.strip())
    return rules


def split_both(value):
    rules = split_rules(value)
    if host_split(value) != rules:
        raise Unclear('allowed-tools の値の切り方が 2 通りある(入れ子の括弧など)')
    return rules


def scalar(text):
    """引用を外した 1 行のスカラー。エスケープ・複数の解釈がありうる形は Unclear。"""
    if not text:
        return ''
    if any((ch.isspace() and ch != ' ') or ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
        raise Unclear('値に空白以外の空白文字か制御文字がある')
    if text[0] == '"':
        if len(text) < 2 or text[-1] != '"' or '"' in text[1:-1] or '\\' in text:
            raise Unclear('二重引用の値を解釈できない')
        return text[1:-1]
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'" or "'" in text[1:-1]:
            raise Unclear('一重引用の値を解釈できない')
        return text[1:-1]
    if text[0] in INDICATORS or ': ' in text or ' #' in text or '\t#' in text or text.endswith(':'):
        raise Unclear('値を解釈できない(YAML の記号・注釈・入れ子)')
    return text


def flow_items(text):
    if not text.endswith(']') or text.count('[') != 1 or text.count(']') != 1 or '{' in text or '}' in text:
        raise Unclear('フロー列を解釈できない')
    inner = text[1:-1].strip()
    if not inner:
        return []
    items, cur, quote = [], '', ''
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = ''
        elif ch in '"\'' and not cur.strip():
            quote = ch
            cur += ch
        elif ch == ',':
            items.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if quote:
        raise Unclear('フロー列の引用が閉じていない')
    items.append(cur.strip())
    if any(not i for i in items):
        raise Unclear('フロー列に空の要素がある')
    return [scalar(i) for i in items]


def leading_blank(ch):
    return ch.isspace() or ch in '\ufeff\u200b'


def frontmatter_grants(raw):
    """frontmatter の allowed-tools の規則の列。frontmatter・鍵が無ければ None。"""
    # UTF-8 でないバイトは置換文字にして同じ規則で読む(エスケープの疑いを外さない)
    text = raw.decode('utf-8', 'replace')
    if text.startswith('\ufeff'):
        text = text[1:]
    # ホストの前処理(BOM・先頭の空白の扱い)は未確認なので、先頭の空白類と BOM を外した形も候補にする
    i = 0
    while i < len(text) and leading_blank(text[i]):
        i += 1
    core = text[i:]
    if not core.startswith('---'):
        return None
    ours = frontmatter_region(core)
    regions = [ours]
    for candidate in (text, core):
        for pattern in HOST_FRONTMATTER:
            m = pattern.match(candidate)
            regions.append(m.group(1) if m else None)
    def suspicious(region):
        return region is not None and (KEY_GUARD.search(region) or '\\' in region)
    # どれかの読み方で frontmatter が無い(None)のは、権限が足されない向きなので比べない。範囲が 2 通り以上あるときだけ疑う
    if any(suspicious(r) for r in regions) and len({normalize_region(r) for r in regions if r is not None}) > 1:
        raise Unclear('frontmatter の範囲の読み方が 2 通り以上あり、allowed-tools かエスケープがある')
    # 行の形で読めないのに、ほかの読み方の範囲(1 つだけでも)に鍵かエスケープがあるか、本文のどこかに鍵の字面がある
    if ours is None and (KEY_GUARD.search(text) or any(suspicious(r) for r in regions)):
        raise Unclear('frontmatter の始まりか終わりを行の形で読めないのに、allowed-tools の字面かエスケープがある')
    found = parse_frontmatter_lines(core)
    if suspicious(ours):
        cross_check_yaml(ours, found)
    return found


def yaml_loader():
    try:
        import yaml  # 任意。あれば YAML の読み方と食い違わないかを照らす(無ければ自前の厳しい読み方だけ)
    except ImportError:
        return None
    return yaml.safe_load


def repair_like_host(region):
    out = []
    for line in region.split('\n'):
        line = re.sub(r'^\t+', lambda m: '  ' * len(m.group(0)), line)
        m = re.match(r'^([A-Za-z0-9_-]+):[ \t]+(.*)$', line)
        if m and m.group(2) and m.group(2)[0] not in '\'"[{|>&*!':
            line = m.group(1) + ": '" + m.group(2).replace("'", "''") + "'"
        out.append(line)
    return '\n'.join(out)


def cross_check_yaml(region, found):
    """鍵かエスケープのある frontmatter を YAML としても読み、allowed-tools の規則が食い違えば止める。"""
    load = yaml_loader()
    if load is None:
        return
    if len(region) > 65536 or ('&' in region and '*' in region):
        raise Unclear('allowed-tools を持つ frontmatter が大きすぎるか、アンカーと別名を使う')
    try:
        data = load(region)
    except Exception:  # YAML として読めない(ホストは読み直す処理を持つ)
        if KEY_GUARD.search(region):
            raise Unclear('allowed-tools のある frontmatter を YAML として読めない') from None
        # 鍵の字面が無いときだけ、ホストの読み直し(最上位の素の鍵の値を引用し、先頭のタブを空白にする)を写して読む。
        # 読み直しは新しい鍵を作らない(引用で組み立てた鍵の行は、そのまま残る)
        try:
            data = load(repair_like_host(region))
        except Exception:
            raise Unclear('エスケープのある frontmatter を YAML として読めない') from None
    rules = None
    if isinstance(data, dict):
        keys = [k for k in data if not isinstance(k, str) or re.sub('[-_]', '', k).lower() == 'allowedtools']
        if len(keys) > 1 or (keys and keys[0] != GRANT_KEY):
            raise Unclear('YAML として読むと allowed-tools の鍵が違う形で現れる')
        if keys:
            value = data[GRANT_KEY]
            if value is None:
                rules = []
            elif isinstance(value, str):
                rules = host_split(value)
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                rules = [v.strip() for v in value]
            else:
                raise Unclear('YAML として読むと allowed-tools の値が文字列か文字列の列でない')
    elif data is not None:
        raise Unclear('YAML として読むと frontmatter が対応表でない')
    if rules != found and not (rules is None and found is None):
        raise Unclear('YAML として読んだ allowed-tools と食い違う')


def normalize_region(region):
    if region is None:
        return None
    return '\n'.join(line.rstrip('\r') for line in region.split('\n')).strip('\n')


def frontmatter_region(text):
    """行の形で読んだ frontmatter の中身(開きと閉じの行を除く)。無ければ None。"""
    lines = text.split('\n')
    if not lines or lines[0].rstrip('\r').rstrip(' \t') != '---':
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip('\r').rstrip(' \t') == '---'), None)
    if end is None:
        return None
    return '\n'.join(lines[1:end])


def parse_frontmatter_lines(text):
    lines = [line[:-1] if line.endswith('\r') else line for line in text.split('\n')]
    # 先頭の空行は読み飛ばす(ホストが受け付ける形を取りこぼさない向きに広く取る)
    start = next((i for i, line in enumerate(lines) if line.strip(' \t')), None)
    if start is None or lines[start].rstrip(' \t') != '---':
        if start is not None and lines[start].startswith('---') and KEY_GUARD.search(text):
            raise Unclear('frontmatter の始まりの行を解釈できない')
        return None
    # 終わりは `---` だけ(`...` で切ると、ホストが続きを読む形で鍵を取りこぼす)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].rstrip(' \t') == '---'), None)
    block = lines[start + 1:end] if end is not None else lines[start + 1:]
    joined = '\n'.join(block)
    if end is None:
        if KEY_GUARD.search(joined):
            raise Unclear('frontmatter が閉じていない')
        return None
    if end == start + 1:
        # 開きの直後が `---` のとき、閉じの前に中身の行を要する読み方では、次の `---` までが frontmatter になる
        later = next((i for i in range(start + 2, len(lines)) if lines[i].rstrip(' \t') == '---'), len(lines))
        if KEY_GUARD.search('\n'.join(lines[start + 2:later])):
            raise Unclear('空の frontmatter の後ろに allowed-tools がある(読み方が 2 通りある)')
        return None
    # 引用・エスケープ・フロー・マージで鍵を組み立てられる形は、鍵の字面かエスケープがあるときだけ疑う
    suspicious = bool(KEY_GUARD.search(joined)) or '\\' in joined
    if suspicious:
        # 鍵の字面かエスケープのある frontmatter は、素直な形だけを読む。YAML の読み方が分かれうる単独の CR・
        # YAML 1.1 の改行(U+0085・U+2028・U+2029)・タブ・改ページは、どこにあっても止める(タブか単独の CR で
        # 始まる行は、行の読み方では入れ子として読み飛ばすが、ホストの YAML は最上位の鍵として読みうる)
        if any(ch in joined for ch in '\r\x85\u2028\u2029\t\x0b\x0c'):
            raise Unclear('allowed-tools かエスケープのある frontmatter に、改行の読み方が分かれうる文字かタブがある')
    if KEY_GUARD.search(joined):
        # allowed-tools を持つ frontmatter は、注釈の行も止める
        if any(line.lstrip(' ').startswith('#') for line in block):
            raise Unclear('allowed-tools を持つ frontmatter に注釈の行がある')
    first = next((line for line in block if line.strip() and not line.lstrip(' \t').startswith('#')), None)
    if first is not None and first[0] in ' \t':
        # 最上位の対応表をまとめて字下げした形は、YAML では最上位の鍵として読まれる
        if suspicious:
            raise Unclear('frontmatter の最上位が字下げされている')
        return None
    found = None
    i = 0
    while i < len(block):
        line = block[i]
        i += 1
        if not line.strip() or line.lstrip(' ').startswith('#'):
            continue
        if line[0] in ' \t':
            continue  # ほかの鍵の入れ子(allowed-tools の値の行は下で読む)
        if line[0] in INDICATORS:
            if suspicious:
                raise Unclear('frontmatter の行を解釈できない(引用の鍵・フロー・アンカー・タグ・マージ・指示)')
            continue
        key, sep, rest = line.partition(':')
        if not sep or (rest and rest[0] not in ' \t'):
            if KEY_GUARD.search(line):
                raise Unclear('allowed-tools に似た行を解釈できない')
            continue
        key = key.rstrip(' ')
        if not KEY_GUARD.search(key):
            continue
        if key != GRANT_KEY:
            raise Unclear('allowed-tools に似た鍵がある')
        if found is not None:
            raise Unclear('allowed-tools の鍵が重複している')
        rest = rest.strip(' \t')
        if '\t' in rest and not rest.startswith(('"', "'")):
            raise Unclear('値にタブがある')
        if rest == '':
            items, indent = [], None
            while i < len(block):
                nxt = block[i]
                if not nxt.strip():
                    i += 1
                    continue
                if nxt[0] not in ' \t' and not (nxt.startswith('- ') or nxt == '-'):
                    break
                lead = nxt[:len(nxt) - len(nxt.lstrip(' \t'))]
                if '\t' in lead:
                    raise Unclear('字下げにタブがある')
                body = nxt.lstrip(' ')
                if body.startswith('#'):
                    i += 1
                    continue
                if not (body.startswith('- ') or body == '-'):
                    raise Unclear('allowed-tools のブロック列を解釈できない')
                if indent is None:
                    indent = lead
                elif lead != indent:
                    raise Unclear('allowed-tools のブロック列の字下げが揃っていない')
                value = scalar(body[2:].strip(' ') if body != '-' else '')
                if not value:
                    raise Unclear('allowed-tools のブロック列に空の要素がある')
                items.append(value)
                i += 1
            rules = []
            for item in items:
                parts = split_both(item)
                if parts != [item.strip()]:
                    raise Unclear('allowed-tools の列の要素が 1 つの規則でない')
                rules.append(item.strip())
            found = rules
            continue
        if rest[0] == '[':
            found = []
            for item in flow_items(rest):
                parts = split_both(item)
                if parts != [item.strip()]:
                    raise Unclear('allowed-tools の列の要素が 1 つの規則でない')
                found.append(item.strip())
        elif rest[0] in '|>':
            raise Unclear('allowed-tools のブロックスカラーは解釈しない')
        else:
            found = split_both(scalar(rest))
        while i < len(block) and (not block[i].strip() or block[i][:1] in (' ', '\t')):
            if block[i].strip() and not block[i].lstrip(' \t').startswith('#'):
                raise Unclear('allowed-tools の値が複数行にまたがる')
            i += 1
    # 鍵の字面が、読んだ最上位の allowed-tools の行のほかにもあれば(入れ子・フロー・値の中など)、解釈が分かれうる
    if len(KEY_GUARD.findall(joined)) != (1 if found is not None else 0):
        raise Unclear('allowed-tools の字面が最上位の鍵のほかにもある')
    return found


RULE = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)(?:\((.*)\))?', re.S)
# 組み込みの道具の名(公式文書 tools-reference の表。2026-10-07 確認)。これと mcp__<server>__<tool> の形の
# ほかは未知の道具として、許可リストの字面と同じでも安全とみなさない。表のうち、作業を別の AI に任せる道具
# (委託の道具・サブエージェントを束ねる道具・その返答の道具)は入れない — skill が無人の周で委託の権限を足すことは、知らない道具と同じく常に止める
KNOWN_TOOLS = frozenset((
    'Artifact', 'AskUserQuestion', 'Bash', 'CronCreate', 'CronDelete', 'CronList', 'Edit',
    'EndConversation', 'EnterPlanMode', 'EnterWorktree', 'ExitPlanMode', 'ExitWorktree', 'Glob', 'Grep',
    'ListMcpResourcesTool', 'LSP', 'Monitor', 'NotebookEdit', 'PowerShell', 'PushNotification',
    'Read', 'ReadMcpResourceTool', 'RemoteTrigger', 'ReportFindings', 'ScheduleWakeup', 'SendFeedback',
    'SendUserFile', 'ShareOnboardingGuide', 'Skill', 'TaskCreate', 'TaskGet',
    'TaskList', 'TaskOutput', 'TaskStop', 'TaskUpdate', 'TodoWrite', 'ToolSearch', 'WaitForMcpServers',
    'WebFetch', 'WebSearch', 'Write', 'MultiEdit'))
MCP_TOOL = re.compile(r'mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+')
# Bash の exact・prefix に使える文字(シェルの記号・引用・展開を含む規則は、ホストの照合が
# 複合コマンドを分ける前か後かで意味が変わりうるので、包含を判定しない)
BASH_WORD = re.compile(r'[A-Za-z0-9_./:=@%+,~-]+')
# 先頭の語がこれらの規則は、ホストが照合の前に外す包み(wrapper)か、prefix では承認しない形
# (exec の包み・find の -exec/-delete・フラグつきの xargs)を持つので、包含を判定しない(permissions の Wrappers の項)
BASH_SPECIAL_HEADS = frozenset((
    'timeout', 'time', 'nice', 'nohup', 'stdbuf', 'command', 'builtin', 'noglob', 'nocorrect', 'xargs',
    'watch', 'setsid', 'ionice', 'flock', 'find', 'env', 'exec', 'eval', 'sudo', 'doas'))
SHELL_TOOLS = ('Bash', 'PowerShell')
# シェルの道具の入力の欄の名。ホストは `Bash(<欄>:<値>)` を欄の照合として読む(公式文書の Parameter matching、
# 2.1.289 の実行ファイルを静的に読んだ範囲)ので、頭がこれらの名の規則は prefix・exact と読まない
SHELL_INPUT_FIELDS = frozenset(('command', 'description', 'timeout', 'run_in_background', 'dangerouslyDisableSandbox',
                                'allowed_domains'))
# auto(分類器)の起動で、ホストが許可リストから外す広い規則の道具(permission-modes の auto の項。
# もう 1 つの委託の道具は KNOWN_TOOLS に無いので、そもそも包含の根拠にならない)
CLASSIFIER_DROPPED_TOOLS = ('Monitor',)
ALLOW_RULE_VERSION = 1
DIRECT_FILE_TOOLS = frozenset(('Read', 'Grep', 'Glob', 'Write', 'Edit', 'NotebookEdit', 'MultiEdit'))
# 既知の入力欄だけを parameter matching と区別する。任意の identifier: を
# 禁止すると report:2026.txt や drive の字面まで別の意味へ変えてしまう。
FILE_INPUT_FIELDS = {
    'Read': ('file_path', 'offset', 'limit', 'pages'),
    'Edit': ('file_path', 'old_string', 'new_string', 'replace_all'),
    'Write': ('file_path', 'content'),
    'NotebookEdit': ('notebook_path', 'cell_id', 'new_source', 'cell_type', 'edit_mode'),
    'MultiEdit': ('file_path', 'edits'),
    'Glob': ('pattern', 'path'),
    'Grep': ('pattern', 'path', 'glob', 'type', 'output_mode', 'multiline', 'head_limit',
             'offset', 'context', 'A', 'B', 'C', 'i', 'n', '-A', '-B', '-C', '-i', '-n'),
}
ALLOW_SOURCE_KINDS = frozenset(('cli', 'user', 'cache', 'managed', 'managed-drop-in',
                              'plugin', 'marketplace-manifest', 'personal-skill', 'personal-command',
                              'personal-agent', 'enterprise-skill', 'enterprise-command', 'enterprise-agent'))


def validate_direct_allow_rules(rules, source_kind):
    """H47 の直接許可。包含 parser の unknown 全体を禁止する判定ではない。"""
    kind = source_kind if source_kind in ALLOW_SOURCE_KINDS else 'component'
    def stop():
        raise Unclear(f'kind={kind}: 直接 allow の規則が危険か、形を確定できない')
    if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
        stop()
    for rule in rules:
        head = re.match(r'\s*([A-Za-z][A-Za-z0-9_-]*)', rule)
        if not head or head.group(1) not in DIRECT_FILE_TOOLS:
            continue
        match = RULE.fullmatch(rule)
        if not match or rule != rule.strip() or rule.count('(') != 1 or rule.count(')') != 1:
            stop()
        spec = match.group(2)
        if not spec or spec == '*' or spec != spec.strip() or any(
                (ch.isspace() and ch != ' ') or ord(ch) < 32 or ord(ch) == 127 for ch in rule):
            stop()
        # parameter matching は allow の path 規則ではない。path 起点や字面は変換しない。
        if ':' in spec and spec.split(':', 1)[0].strip() in FILE_INPUT_FIELDS[head.group(1)]:
            stop()
    return rules


def permission_allow(settings, kind):
    """不在だけを空として扱い、明示 null や壊れた型を無視しない。"""
    if not isinstance(settings, dict):
        raise Unclear(f'kind={kind}: permissions の形を確定できない')
    permissions = settings.get('permissions', {})
    if not isinstance(permissions, dict):
        raise Unclear(f'kind={kind}: permissions の形を確定できない')
    return validate_direct_allow_rules(permissions.get('allow', []), kind)


def direct_allow_policy(settings, allowed_tools):
    """全 source を検査し、証明には規則の値を置かず要約だけを渡す。"""
    cli = []
    try:
        for value in allowed_tools:
            cli.extend(split_both(value))
    except (Unclear, TypeError):
        raise Unclear('kind=cli: --allowed-tools の値の切り方を確定できない') from None
    validate_direct_allow_rules(cli, 'cli')
    sources = settings.get('permission_sources')
    if not isinstance(sources, list) or not sources:
        raise Stop('host-settings の許可 source が保持されていない')
    seen, held, user = set(), [], []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {'kind', 'source', 'present', 'settings'}:
            raise Stop('host-settings の許可 source の形が違う')
        kind, identity, present = source['kind'], source['source'], source['present']
        if kind not in ('user', 'cache', 'managed', 'managed-drop-in') or type(present) is not bool \
                or not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity) or identity in seen:
            raise Stop('host-settings の許可 source の形が違う')
        seen.add(identity)
        if not present and source['settings'] is not None:
            raise Stop('host-settings の許可 source の不在が違う')
        rules = permission_allow(source['settings'], kind) if present else []
        if kind == 'user':
            user.extend(rules)
        digest = sha256(json.dumps(rules, ensure_ascii=True, separators=(',', ':')).encode())
        held.append({'kind': kind, 'source': identity, 'present': present, 'allow_sha256': digest})
    if sum(source['kind'] == 'user' for source in sources) != 1:
        raise Stop('host-settings の利用者 source が一意でない')
    binding = {'version': ALLOW_RULE_VERSION,
               'cli_sha256': sha256(json.dumps(cli, ensure_ascii=True, separators=(',', ':')).encode()),
               'sources': sorted(held, key=lambda item: (item['kind'], item['source']))}
    return binding, cli + user


def known_tool(tool):
    return tool in KNOWN_TOOLS or bool(MCP_TOOL.fullmatch(tool))


def parse_rule(rule):
    """規則の形。('all', 道具)・('exact', 道具, 文)・('prefix', 道具, 頭)・('tool', 道具)・('scoped', 道具, 指定)・
    ('unknown', 字面)。シェルの道具(Bash・PowerShell)だけを exact・prefix に分ける。"""
    if not rule or rule != rule.strip() or '$' in rule or rule.count('(') > 1 or rule.count(')') > 1:
        return ('unknown', rule)  # 入れ子の括弧は、ホストの中身の取り方(最初の `(` と最後の `)`)と区切り方で読み方が分かれる
    m = RULE.fullmatch(rule)
    if not m or not known_tool(m.group(1)):
        return ('unknown', rule)
    tool, spec = m.group(1), m.group(2)
    if tool not in SHELL_TOOLS:
        if spec is None:
            return ('tool', tool)
        # 単独の / で始まる指定は、定義した場所で起点が変わる(利用者の設定は ~/.claude、引数は作業ディレクトリ)
        if not spec or (spec.startswith('/') and not spec.startswith('//')):
            return ('unknown', rule)
        return ('scoped', tool, spec)
    if spec is None or spec == '*':
        return ('all', tool)
    if not spec or spec != ' '.join(spec.split()):
        return ('unknown', rule)
    head = spec
    kind = 'exact'
    for suffix in (':*', ' *'):
        if spec.endswith(suffix):
            head, kind = spec[:-2], 'prefix'
            break
    words = head.split(' ')
    if not head or head != head.strip() or not all(BASH_WORD.fullmatch(w) for w in words) \
            or words[0] in BASH_SPECIAL_HEADS or '=' in words[0] or spec.split(':', 1)[0].strip() in SHELL_INPUT_FIELDS:
        return ('unknown', rule)
    return (kind, tool, head)


def word_prefix(text, prefix):
    return text == prefix or text.startswith(prefix + ' ')


def effective_allow(allow_rules, classifier):
    """包含の根拠に使う許可リストの規則(解析した形)と、字面の一致に使う集合。

    auto(分類器)の起動では、ホストが広い規則(シェルの全体・ワイルドカードつきの規則・委託と Monitor の道具)を
    外すので、シェルは exact だけ、ほかは外される道具を除いて根拠にする。"""
    parsed, raw = [], set()
    for r in allow_rules:
        p = parse_rule(r)
        # 分類器の起動では、ホストが外す規則(パッケージマネージャの run など)を字面から決めきれないので、
        # シェルの規則はどれも根拠にしない
        if classifier and ((p[0] != 'unknown' and p[1] in SHELL_TOOLS + CLASSIFIER_DROPPED_TOOLS)
                           or (p[0] == 'unknown' and p[1].startswith(SHELL_TOOLS))):
            continue
        parsed.append(p)
        if p[0] == 'scoped':
            raw.add(r)
    return parsed, raw


def covered(rule, allow_rules, classifier=False):
    """skill の規則 rule が、実効許可リスト allow_rules(文字列)の範囲に含まれるか(同値か狭い集合だけ)。"""
    kind = parse_rule(rule)
    if kind[0] in ('unknown', 'all'):
        return False  # 解釈できない形・未知の道具・シェルの全体は、許可リストに同じ字面があっても安全とみなさない
    parsed, raw = effective_allow(allow_rules, classifier)
    if kind[0] == 'tool':
        return kind in parsed
    if kind[0] == 'scoped':
        return ('tool', kind[1]) in parsed or rule in raw
    if ('all', kind[1]) in parsed:
        return True  # 許可リストがシェルの全体を許すなら、exact・prefix は新しい許可にならない
    if kind[0] == 'exact':
        # exact は同じ exact にだけ含める(prefix の規則は exec の包み・find -delete などを承認しないが、
        # exact はそれらを承認できるので、prefix の範囲の中とはみなせない)
        return kind in parsed
    return any(p[0] == 'prefix' and p[1] == kind[1] and word_prefix(kind[2], p[2]) for p in parsed)


def judge_rules(rules, allow_rules, classifier=False):
    """(広げる規則の列)。重複は Unclear。"""
    seen = set()
    for rule in rules:
        key = parse_rule(rule)
        if key in seen or rule in seen:
            raise Unclear('同じ規則が重複している')
        seen.add(key)
        seen.add(rule)
    return [r for r in rules if not covered(r, allow_rules, classifier)]


def load_state(path, expected):
    if not re.fullmatch('[0-9a-f]{64}', expected or ''):
        raise Stop('環境の控えの保持値が必要')
    raw = read_regular(path, MAX_STATE)
    if sha256(raw) != expected:
        raise Stop('環境の控えが保持値と一致しない')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('version') != 3 or not isinstance(value.get('entries'), dict):
        raise Stop('環境の控えの形式が違う')
    return value


def read_logical(path, entries, user_dir):
    """user_dir の直下の正規導入リンクだけを、控えた字面と対象の実体が同じときに辿って読む。"""
    rel = path[len(user_dir) + 1:].split('/')
    link_key = '@link:external:' + user_dir + '/' + rel[0]
    if link_key not in entries:
        return read_regular(path, MAX_TEXT)
    held = entries[link_key]
    if not isinstance(held, list) or len(held) != 7 or held[0] != 'installation-link':
        raise Stop('導入リンクの控えの形が違う')
    parts = _split(user_dir)
    fd = open_dir_chain(parts)
    try:
        st = os.stat(rel[0], dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISLNK(st.st_mode) or [st.st_dev, st.st_ino] != held[2:4] or os.readlink(rel[0], dir_fd=fd) != held[1]:
            raise Stop('導入リンクが控えた時から変わった')
    finally:
        os.close(fd)
    text = held[1]
    target = text if text.startswith('/') else user_dir + '/' + text
    tparts = _split(os.path.normpath(target)) if '..' not in target.split('/') else None
    if tparts is None:
        raise Stop('導入リンクの字面に .. がある')
    tfd = open_dir_chain(tparts)
    try:
        tst = os.fstat(tfd)
        if [tst.st_dev, tst.st_ino, tst.st_mode] != held[4:7]:
            raise Stop('導入リンクの対象が控えた時から変わった')
        inner = open_dir_chain_from(tfd, rel[1:-1])
        try:
            return read_at(inner, rel[-1], MAX_TEXT)
        finally:
            os.close(inner)
    finally:
        os.close(tfd)


def open_dir_chain_from(fd, names):
    cur = os.dup(fd)
    try:
        for name in names:
            if name in ('', '.', '..'):
                raise Stop('正規化されていないパス')
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
            os.close(cur)
            cur = child
        return cur
    except BaseException:
        os.close(cur)
        raise


def verified_host_settings(state_path, expected):
    """state と同じ階層に保持した reader だけを import する。"""
    value = load_state(state_path, expected)
    if not isinstance(value.get('host_settings'), list) or not re.fullmatch('[0-9a-f]{64}', value.get('guard_sha256', '')):
        raise Stop('host-settings の保持記述子が無い')
    path = Path(state_path).parent / 'environment-guard.py'
    raw = read_regular(str(path), MAX_TEXT)
    if sha256(raw) != value['guard_sha256']:
        raise Stop('host-settings の reader が保持値と違う')
    try:
        # pathname を import し直さない。検証済み raw bytes 自身を実行するので、
        # read 後の差替えや __pycache__ を reader として採用しない。
        namespace = {'__name__': 'verified_environment_guard', '__file__': str(path)}
        exec(compile(raw, str(path), 'exec'), namespace)
        reader = namespace.get('read_host_settings')
        if not callable(reader):
            raise RuntimeError('reader')
        return reader(state_path, expected)
    except Exception as exc:
        # コピー側の Stop は別クラスである。任意の OSError/JSON 例外や path を
        # 含む本文を転記せず、契約にある固定理由だけを host-check 側へ渡す。
        detail = str(exc)
        fixed = {
            '配布物または利用者環境が変わった': '配布物または利用者の設定の内容が変わった',
            '導入リンクが開始時から変わった': '導入リンクが控えた時から変わった',
            '導入リンクの対象が開始時から変わった': '導入リンクの対象が控えた時から変わった',
            '導入リンクが走査中に変わった': '導入リンクが控えた時から変わった',
            '導入リンクの対象が走査中に変わった': '導入リンクの対象が控えた時から変わった',
            'component の親または導入リンクが検査中に変わった': '導入リンクが控えた時から変わった',
            '本文読取前に監視対象が変わった': '控えた時から内容が変わった',
            '本文読取前にリンク対象が変わった': '控えた時から内容が変わった',
            'component 導入リンクが開始時から変わった': '導入リンクが控えた時から変わった',
            'component 導入リンクの対象が開始時から変わった': '導入リンクの対象が控えた時から変わった',
            'component の実体が開始時から変わった': '控えた時から component の実体が変わった',
            'component の導入リンクが検査中に変わった': '控えた時から component の実体が変わった',
            'component が読取中に変わった': '控えた時から component の実体が変わった',
            'component の集合が検査中に変わった': '控えた時から component の実体が変わった',
            'host-config-path': 'host-config-path',
            'host-settings': 'host-settings',
            'host-config-source': 'host-config-source',
            'hooks-disabled': 'hooks-disabled',
        }
        raise Stop(fixed.get(detail, 'host-settings の reader を検査できない')) from None


MANIFEST_SUFFIX = '/.claude-plugin/plugin.json'


def strict_json(raw, label):
    """重複した object key を受け入れず JSON を読む。

    plugin manifest の後勝ちは host の版で変わりうる。通常の json.loads は
    重複を黙って上書きするため、ここで policy を広げる方向を残さない。
    """
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise Unclear(f'{label} の JSON の鍵が重複している')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except Unclear:
        raise
    except (ValueError, UnicodeDecodeError):
        raise Unclear(f'{label} を JSON として読めない') from None


def verified_components(state_path, expected):
    """H50 の保持 reader が再照合して返した bytes だけを component として使う。"""
    value = load_state(state_path, expected)
    if not isinstance(value.get('guard_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['guard_sha256']):
        raise Stop('component の保持 reader が無い')
    path = Path(state_path).parent / 'environment-guard.py'
    raw = read_regular(str(path), MAX_TEXT)
    if sha256(raw) != value['guard_sha256']:
        raise Stop('component の保持 reader が保持値と違う')
    try:
        namespace = {'__name__': 'verified_environment_guard', '__file__': str(path)}
        exec(compile(raw, str(path), 'exec'), namespace)
        reader = namespace.get('read_components')
        if not callable(reader):
            raise RuntimeError('read_components')
        result = reader(state_path, expected)
    except Exception as exc:
        detail = str(exc)
        fixed = {
            '導入リンクが開始時から変わった': '導入リンクが控えた時から変わった',
            '導入リンクの対象が開始時から変わった': '導入リンクの対象が控えた時から変わった',
            'component の本文が控えた時から変わった': 'component の本文が控えた時から変わった',
        }
        raise Stop(fixed.get(detail, 'component の保持 reader を検査できない')) from None
    if not isinstance(result, list):
        raise Stop('component の保持値の形が違う')
    keys, checked, directory_cache = set(), [], {}
    for item in result:
        if not isinstance(item, dict) or not {'key', 'kind', 'root', 'relative', 'path', 'raw'} <= set(item) or \
                set(item) - {'key', 'kind', 'root', 'relative', 'path', 'raw', 'source', 'plugin', 'entry'}:
            raise Stop('component の保持値の形が違う')
        key, kind = item['key'], item['kind']
        if not isinstance(key, str) or key in keys or not isinstance(kind, str) or \
                kind not in ('personal-skill', 'enterprise-skill', 'personal-command', 'enterprise-command',
                             'personal-agent', 'enterprise-agent', 'marketplace-manifest', 'plugin') or \
                not all(isinstance(item[name], str) for name in ('root', 'relative', 'path')) or \
                not isinstance(item['raw'], bytes):
            raise Stop('component の保持値の形が違う')
        if not key.startswith('component:' + kind + ':') or not os.path.isabs(item['root']) or not os.path.isabs(item['path']):
            raise Stop('component の保持値の形が違う')
        if any(name in item and not isinstance(item[name], (str, dict, list, type(None)))
               for name in ('source', 'plugin', 'entry')):
            raise Stop('component の保持値の形が違う')
        keys.add(key)
        prefix = 'component:' + kind + ':' + item['root']
        if prefix not in directory_cache:
            directory_cache[prefix] = tuple(k[len(prefix):].lstrip('/') for k, v in value.get('entries', {}).items()
                         if (k == prefix or k.startswith(prefix + '/')) and isinstance(v, list) and v[:1] == ['directory'])
        item = dict(item, held_directories=directory_cache[prefix])
        checked.append(item)
    return checked


def verified_component_digest(state_path, expected):
    """保持 reader の正規化済み digest を使用する。run 固有の stat は混ぜない。"""
    value = load_state(state_path, expected)
    path = Path(state_path).parent / 'environment-guard.py'
    raw = read_regular(str(path), MAX_TEXT)
    if sha256(raw) != value.get('guard_sha256'):
        raise Stop('component の保持 reader が保持値と違う')
    try:
        namespace = {'__name__': 'verified_environment_guard', '__file__': str(path)}
        exec(compile(raw, str(path), 'exec'), namespace)
        result = namespace['component_policy_digest'](state_path, expected)
    except Exception:
        raise Stop('component の digest を検査できない') from None
    return digest_value(result, 'component_sha256')


NAME = re.compile(r'[a-z0-9][a-z0-9:_-]*\Z')
NATIVE_RESERVED = frozenset(('doctor', 'checkup', 'design', 'plugin-authoring'))
RESERVED_COMPONENT_NAMES = frozenset(('anthropic-skills', 'synced'))


def component_fields(raw):
    """特権・名前の限定 YAML を自前で読む。任意の YAML loader に依存しない。"""
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise Unclear('component が UTF-8 でない') from None
    core = text.lstrip(' \t\r\n\ufeff\u200b')
    if not core.startswith('---'):
        return {}
    region = frontmatter_region(core)
    sensitive = {'hooks', 'modules', 'mcpservers', 'permissionmode', 'skills',
                 'name', 'userinvocable', 'disablemodelinvocation'}
    marker = re.compile(r'(?i)(hooks|modules|mcp.?servers|permission.?mode|skills|name|user.?invocable|disable.?model.?invocation)')
    regions = [region]
    for pattern in HOST_FRONTMATTER:
        match = pattern.match(core)
        if match:
            regions.append(match.group(1))
    if region is None or len({normalize_region(r) for r in regions if r is not None}) != 1:
        if any(r and (marker.search(r) or '\\' in r) for r in regions) or marker.search(core):
            raise Unclear('component frontmatter の範囲を確定できない')
        return {}
    if len(region) > 65536:
        raise Unclear('component frontmatter が上限を超えた')
    if any(ch in region.replace('\r\n', '\n') for ch in '\r\x85\u2028\u2029\x0b\x0c'):
        raise Unclear('component frontmatter の改行が曖昧')
    fields, block, current_key = {}, False, None
    for line in region.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indented = line.startswith((' ', '\t'))
        sequence = re.match(r'^-(?:[ \t]|$)', line.lstrip())
        # YAML は mapping value の列を key と同じ字下げでも書ける。
        # 次の key と誤認すると hooks/MCP/preload を空値に戻してしまう。
        if indented or (current_key and sequence):
            if block:
                continue
            if current_key == 'skills' and sequence:
                if fields['skills'] == '':
                    fields['skills'] = []
                if not isinstance(fields['skills'], list):
                    raise Unclear('agent skills の列が曖昧')
                fields['skills'].append(line.lstrip()[1:].strip())
                continue
            if current_key:
                fields[current_key] = '__nonempty_block__'
                continue
            if marker.search(line) or '\\' in line or line.lstrip().startswith('<<'):
                raise Unclear('component frontmatter の階層を確定できない')
            continue
        block, current_key = False, None
        key, sep, value = line.partition(':')
        if not sep:
            if marker.search(line) or '\\' in line:
                raise Unclear('component frontmatter の鍵を確定できない')
            continue
        key = key.strip()
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', key):
            if marker.search(key) or '\\' in key or key.startswith(('<<', '{', '[', '?')):
                raise Unclear('component frontmatter の鍵を確定できない')
            continue
        normalized = normalized_yaml_key(key)
        if normalized in sensitive:
            if normalized in fields:
                raise Unclear('component frontmatter の鍵が重複している')
            fields[normalized] = value.strip().split(' #', 1)[0].strip()
            current_key = normalized
        elif value.strip().startswith(('|', '>')):
            block = True
    return fields


def component_frontmatter(raw, label):
    fields = component_fields(raw)
    out = {}
    for key in ('name', 'userinvocable', 'disablemodelinvocation'):
        if key not in fields:
            continue
        value = fields[key]
        if key == 'name':
            value = scalar(value)
            if not NAME.fullmatch(value):
                raise Unclear('component の name の形が違う')
        elif value not in ('true', 'false'):
            raise Unclear('component の可視性が真偽値でない')
        out[key] = value
    return out


def component_manifests(components):
    """保持済み選択 entry を、対応する各 plugin root の追加 manifest として結ぶ。"""
    result = list(components)
    # skill folder の root SKILL は personal/enterprise のまま。内部の
    # plugin 宣言は別の検査済み出所にし、保持済み bytes を再分類するだけにする。
    for manifest in components:
        if manifest['kind'] not in ('personal-skill', 'enterprise-skill') or not manifest['relative'].endswith(MANIFEST_SUFFIX):
            continue
        prefix = manifest['relative'][:-len('.claude-plugin/plugin.json')]
        if len(prefix.rstrip('/').split('/')) != 1:
            raise Unclear('skill-folder plugin の位置を確定できない')
        data = strict_json(manifest['raw'], 'skill-folder plugin')
        name = data.get('name') if isinstance(data, dict) else None
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Unclear('skill-folder plugin の名前を確定できない')
        folder = prefix.rstrip('/').lower()
        if folder in ('synced', 'anthropic-skills') or folder.startswith('anthropic-skills:'):
            continue
        root = manifest['root'] + '/' + prefix.rstrip('/')
        for part in components:
            if part['root'] == manifest['root'] and part['relative'].startswith(prefix):
                rel = part['relative'][len(prefix):]
                result.append(dict(part, key='component:plugin:' + root + '/' + rel,
                                   kind='plugin', root=root, relative=rel, plugin=name,
                                   source='skill-folder-plugin',
                                   held_directories=tuple(directory[len(prefix):] for directory in part.get('held_directories', ())
                                                          if directory.startswith(prefix))))

    for item in components:
        if item['kind'] != 'marketplace-manifest':
            continue
        marketplace_security(item)
        for selected in item['entry']:
            plugin, entry = selected['plugin'], selected['entry']
            roots = sorted({part['root'] for part in components
                            if part['kind'] == 'plugin' and part.get('plugin') == plugin})
            if not roots or not isinstance(item.get('source'), str):
                raise Unclear('選択 marketplace entry の plugin root が無い')
            source_root = str(Path(item['source']) / entry['source'])
            if source_root not in roots:
                raise Unclear('選択 marketplace entry の source が保持されていない')
            if 'strict' in entry and not isinstance(entry['strict'], bool):
                raise Unclear('選択 marketplace entry の strict が真偽値でない')
            for root in roots:
                result.append({'key': item['key'] + ':' + plugin + ':' + root,
                               'kind': 'plugin', 'root': root, 'plugin': plugin,
                               'source': 'selected-entry',
                               'namespace_source': 'marketplace-source' if any(part['root'] == root and part.get('source') == 'marketplace-source' for part in components) else 'active',
                               'relative': '.claude-plugin/plugin.json',
                               'path': item['path'], 'raw': json.dumps(entry).encode()})
    return result


def plugin_prefix(component, manifests):
    plugin = component.get('plugin')
    if not plugin:
        ids = {manifest.get('plugin') for manifest, _ in manifests if manifest['root'] == component['root'] and manifest.get('plugin')}
        if len(ids) == 1:
            plugin = next(iter(ids))
        elif ids:
            raise Unclear('plugin の出所が競合している')
    names = {data.get('name') for manifest, data in manifests
             if manifest['root'] == component['root'] and isinstance(data.get('name'), str)}
    if plugin:
        prefix = plugin.rsplit('@', 1)[0]
        if names and names != {prefix}:
            raise Unclear('plugin ID と manifest 名が一致しない')
    elif len(names) == 1:
        prefix = next(iter(names))
    else:
        raise Unclear('plugin の名前空間を一意に解決できない')
    if not NAME.fullmatch(prefix):
        raise Unclear('plugin の名前空間の形が違う')
    return prefix


def held_reference(components, root, source, directory=False):
    if not isinstance(source, str) or not manifest_paths_ok(source):
        raise Unclear('component の参照が root 内相対パスでない')
    relative = source
    while relative.startswith('./'):
        relative = relative[2:]
    relative = relative.rstrip('/')
    matches = [item for item in components if item['root'] == root and
               (item['relative'] == relative or (directory and (not relative or item['relative'].startswith(relative + '/'))))]
    if not matches and not (directory and any(item['root'] == root and relative in item.get('held_directories', ()) for item in components)):
        raise Unclear('component の参照先が保持されていない')
    return matches


def component_namespace(components):
    """定義の優先順位、呼出先、初期化で表示する主名を別々に決める。"""
    components = component_manifests(components)
    namespace_components = [item for item in components if item.get('source') != 'marketplace-source' and item.get('namespace_source') != 'marketplace-source']
    manifests = [(item, strict_json(item['raw'], 'plugin.json')) for item in namespace_components
                 if item['relative'] == '.claude-plugin/plugin.json' and item['kind'] == 'plugin']
    if any(not isinstance(data, dict) for _, data in manifests):
        raise Unclear('plugin manifest が対応表でない')
    definitions = []

    def add(item, primary, kind, raw=None, alias=True):
        raw = item['raw'] if raw is None else raw
        data = component_frontmatter(raw, item['path'])
        alternate = data.get('name') if alias else None
        plugin = item['kind'] == 'plugin'
        if not plugin:
            parts = item['relative'].split('/')
            reserved = lambda name: name.lower() == 'anthropic-skills' or name.lower().startswith('anthropic-skills:')
            if (kind == 'skill' and parts[0].lower() == 'synced') or \
                    any(reserved(name) for name in (primary, alternate or '', *parts)):
                return
        if not NAME.fullmatch(primary) or (alternate and not NAME.fullmatch(alternate)):
            raise Unclear('component の公開名の形を確定できない')
        prefix = plugin_prefix(item, manifests) if plugin else None
        qualify = lambda name: name if not prefix or name.startswith(prefix + ':') else prefix + ':' + name
        definitions.append({'primary': qualify(primary), 'alias': qualify(alternate) if alternate else None,
                            'kind': item['kind'], 'loader': kind, 'path': item['path'],
                            'public': data.get('userinvocable') != 'false',
                            'model': data.get('disablemodelinvocation') != 'true',
                            'plugin': prefix, 'content_sha256': sha256(raw)})

    for item in namespace_components:
        rel, kind = item['relative'], item['kind']
        if not rel.lower().endswith('.md'):
            continue
        if kind.endswith('-skill') and len(rel.split('/')) == 2 and rel.lower().endswith('/skill.md'):
            add(item, rel.split('/')[-2], 'skill')
        elif kind.endswith('-command'):
            add(item, rel[:-3].replace('/', ':'), 'command')
        elif kind == 'plugin':
            if rel == 'SKILL.md' and item.get('source') != 'skill-folder-plugin':
                rootname = component_frontmatter(item['raw'], item['path']).get('name', Path(item['root']).name)
                add(item, rootname, 'skill', alias=False)
            elif rel.startswith('skills/') and rel.lower().endswith('/skill.md'):
                add(item, rel.split('/')[-2], 'skill')
            elif rel.startswith('commands/'):
                add(item, rel[len('commands/'):-3].replace('/', ':'), 'command')
    for manifest, data in manifests:
        commands = data.get('commands')
        if isinstance(commands, dict):
            for name, entry in commands.items():
                if not isinstance(name, str) or not isinstance(entry, dict):
                    raise Unclear('plugin commands map の形が違う')
                source = entry.get('source')
                content = entry.get('content')
                bodies = []
                if source is not None:
                    refs = held_reference(components, manifest['root'], source)
                    if len(refs) != 1:
                        raise Unclear('command source が一意でない')
                    bodies.append(refs[0]['raw'])
                if content is not None:
                    if not isinstance(content, str):
                        raise Unclear('command content が文字列でない')
                    bodies.append(content.encode())
                if not bodies:
                    raise Unclear('command の本文が無い')
                # 合成優先順位が解決できない異内容は許可名へ推定追加しない。
                if len(set(bodies)) != 1:
                    raise Unclear('command source と content が競合している')
                add(manifest, name, 'command', bodies[0], alias=False)
        elif commands is not None:
            refs = [commands] if isinstance(commands, str) else commands
            if not isinstance(refs, list):
                raise Unclear('plugin commands の形が違う')
            for source in refs:
                held = held_reference(components, manifest['root'], source, directory=True)
                relative = source
                while relative.startswith('./'):
                    relative = relative[2:]
                relative = relative.rstrip('/')
                for item in held:
                    if item['relative'].lower().endswith('.md'):
                        command_path = item['relative']
                        if command_path == relative:
                            primary = Path(command_path).stem
                        else:
                            primary = command_path[len(relative) + 1:] if relative else command_path
                            primary = primary[:-3].replace('/', ':')
                        add(item, primary, 'command')
        skills = data.get('skills')
        if skills is not None:
            refs = [skills] if isinstance(skills, str) else skills
            if not isinstance(refs, list):
                raise Unclear('plugin skills の形が違う')
            for source in refs:
                for item in held_reference(components, manifest['root'], source, directory=True):
                    if item['relative'].lower().endswith('/skill.md'):
                        root_relative = source[2:] if source.startswith('./') else source
                        direct = item['relative'] == root_relative.rstrip('/') + '/SKILL.md'
                        folder = item['relative'].split('/')[-2]
                        primary = component_frontmatter(item['raw'], item['path']).get('name', folder) if direct else folder
                        add(item, primary, 'skill', alias=not direct)
                    elif item['relative'] == 'SKILL.md':
                        primary = component_frontmatter(item['raw'], item['path']).get('name', Path(item['root']).name)
                        add(item, primary, 'skill', alias=False)

    def rank(item):
        return (2 if item['kind'].startswith('enterprise-') else 1, item['loader'] == 'skill')
    winners = {}
    for item in definitions:
        old = winners.get(item['primary'])
        if old is None or rank(item) > rank(old):
            winners[item['primary']] = item
        elif rank(item) == rank(old):
            if all(old[key] == item[key] for key in ('content_sha256', 'plugin', 'public', 'model', 'alias')):
                continue
            raise Unclear('同じ主名の component の出所が競合している')
    routes = dict(winners)
    for item in winners.values():
        alias = item['alias']
        if not alias or alias in winners:
            continue
        old = routes.get(alias)
        if old is None or rank(item) > rank(old):
            routes[alias] = item
        elif rank(item) == rank(old) and old != item:
            raise Unclear('component の alias の出所が競合している')
    loaded = {name: {key: item[key] for key in
                    ('primary', 'alias', 'kind', 'loader', 'plugin', 'content_sha256', 'public', 'model')}
              for name, item in sorted(winners.items())}
    resolved = {name: {'target': item['primary'], 'user': item['public'], 'model': item['model']}
                for name, item in sorted(routes.items())}
    return {'definitions': sorted(winners.values(), key=lambda item: item['primary']),
            'loaded_commands': loaded, 'invocation_routes': resolved, 'lookup_names': sorted(routes),
            'invocation_names': sorted(name for name, item in routes.items() if item['public']),
            'model_invocation_names': sorted(name for name, item in routes.items() if item['model']),
            # legacy command は slash_commands へ載る。init.skills の照合名へ
            # 加えず、保持済み定義と user/model の呼出先には残す。
            'public_names': sorted(name for name, item in winners.items() if item['public'] and item['loader'] == 'skill')}


def plugin_command_names(components):
    return {name for name in component_namespace(components)['loaded_commands'] if ':' in name}


def fixed_host_policy(namespace):
    """外部 component の有効 winner から、固定する host settings を導出する。"""
    external = [item for item in namespace['definitions'] if item['kind'].startswith(('enterprise-', 'personal-'))]
    definitions = {item['primary']: item for item in external}
    aliases = {item['alias'] for item in external if item['alias'] and item['alias'] in namespace['lookup_names']}
    off = set()
    # Directory exact owns reserved names. checkup's frontmatter alias is also
    # measured as a winner; doctor alias alone is not. Do not turn a normal
    # external skill with a reserved name off.
    for name in NATIVE_RESERVED:
        if name == 'checkup':
            present = name in definitions or name in aliases
        else:
            present = name in definitions
        if not present:
            off.add(name)
    return {'disableBundledSkills': True, 'syncClaudeAiSkills': False,
            'syncClaudeAiPlugins': False, 'skillOverrides': {name: 'off' for name in sorted(off)}}


def policy_digest(component_digest, namespace, policy, direct_allow=None):
    component_digest = digest_value(component_digest, 'component_sha256')
    payload = {'component_sha256': component_digest,
               'loaded_commands': namespace['loaded_commands'],
               'invocation_routes': namespace['invocation_routes'],
               'invocation_names': namespace['invocation_names'], 'lookup_names': namespace['lookup_names'],
               'model_invocation_names': namespace['model_invocation_names'], 'resolver_version': RESOLVER_VERSION,
               'public_names': namespace['public_names'], 'fixed': policy, 'direct_allow': direct_allow}
    return sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode())


def assert_fixed_policy(settings, policy):
    """H50 reader の既検証 settings と、loop が追加する固定値の競合を拒否する。"""
    if not isinstance(settings, dict):
        raise Stop('host-settings の形が違う')
    for source in [settings.get('user'), settings.get('cache'), *settings.get('managed', [])]:
        if source is None:
            continue
        if not isinstance(source, dict):
            raise Stop('host-settings の形が違う')
        for key in ('disableBundledSkills', 'syncClaudeAiSkills', 'syncClaudeAiPlugins'):
            if key in source and (type(source[key]) is not bool or source[key] != policy[key]):
                raise Stop(f'固定する {key} と既存設定が競合している')
        if 'skillOverrides' in source:
            value = source['skillOverrides']
            if not isinstance(value, dict):
                raise Stop('固定する skillOverrides と既存設定が競合している')
            for name, disabled in policy['skillOverrides'].items():
                if name in value and value[name] != disabled:
                    raise Stop(f'固定する skillOverrides.{name} と既存設定が競合している')


def apply_user_skill_off(namespace, settings):
    """保持 reader が返した利用者設定の off は user/model/public route からだけ外す。"""
    disabled = set()
    for source in [settings.get('user'), settings.get('cache'), *settings.get('managed', [])]:
        overrides = source.get('skillOverrides') if isinstance(source, dict) else None
        if overrides is None:
            continue
        if not isinstance(overrides, dict) or any(not isinstance(name, str) or value not in ('off', 'on')
                                                  for name, value in overrides.items()):
            raise Stop('保持した skillOverrides の形が違う')
        disabled.update(name for name, value in overrides.items() if value == 'off')
    result = dict(namespace)
    result['invocation_routes'] = {name: dict(route, user=False, model=False) if name in disabled else dict(route)
                                   for name, route in namespace['invocation_routes'].items()}
    for key in ('invocation_names', 'model_invocation_names', 'public_names'):
        result[key] = [name for name in namespace[key] if name not in disabled]
    return result


def plugin_agents(components):
    components = component_manifests(components)
    agents = [item for item in components if item['kind'].endswith('-agent') or
              (item['kind'] == 'plugin' and item['relative'].startswith('agents/') and item['relative'].lower().endswith('.md'))]
    for item in components:
        if item['kind'] != 'plugin' or item['relative'] != '.claude-plugin/plugin.json':
            continue
        value = strict_json(item['raw'], 'plugin.json').get('agents')
        if value is None:
            continue
        refs = [value] if isinstance(value, str) else value
        if not isinstance(refs, list):
            raise Unclear('plugin agents の参照の形が違う')
        for ref in refs:
            agents.extend(part for part in held_reference(components, item['root'], ref, directory=True)
                          if part['relative'].lower().endswith('.md'))
    return agents


def agent_preloads(components, namespace):
    known = set(namespace['lookup_names'])
    for item in plugin_agents(components):
        fields = component_fields(item['raw'])
        value = fields.get('skills')
        if value is None:
            continue
        if isinstance(value, list):
            names = [scalar(name) for name in value]
        elif value in ('', '[]', 'null', '~'):
            names = []
        elif value.startswith('[') and value.endswith(']'):
            names = [scalar(name) for name in flow_items(value)]
        else:
            raise Unclear('agent preload skills の形を確定できない')
        if any(not NAME.fullmatch(name) for name in names) or set(names) - known:
            raise Unclear('agent preload skill が検査済みでない')


def plugin_agent_only_paths(components):
    """agent 専用の読込先だけに host の無視規則を適用する。

    同じ bytes が複数 role で読まれる場合は、その全 role の条件を満たす必要がある。
    namespace の勝敗や表示可否で security 検査を省かない。
    """
    expanded = component_manifests(components)
    agents = {item['path'] for item in plugin_agents(components) if item['kind'] == 'plugin'}
    if not agents:
        return set()
    commands_skills = set()
    for item in expanded:
        rel, kind = item['relative'], item['kind']
        if not rel.lower().endswith('.md'):
            continue
        if kind.endswith('-command') or (kind.endswith('-skill') and len(rel.split('/')) == 2 and
                                        rel.lower().endswith('/skill.md')) or \
                (kind == 'plugin' and (rel == 'SKILL.md' or rel.startswith('commands/') or
                                       (rel.startswith('skills/') and rel.lower().endswith('/skill.md')))):
            commands_skills.add(item['path'])
    for manifest in expanded:
        if manifest['kind'] != 'plugin' or manifest['relative'] != '.claude-plugin/plugin.json':
            continue
        data = strict_json(manifest['raw'], 'plugin.json')
        for role in ('commands', 'skills'):
            value = data.get(role)
            if value is None:
                continue
            if role == 'commands' and isinstance(value, dict):
                for entry in value.values():
                    if not isinstance(entry, dict):
                        raise Unclear('plugin commands map の形が違う')
                    if entry.get('source') is not None:
                        commands_skills.update(part['path'] for part in held_reference(
                            expanded, manifest['root'], entry['source']))
                continue
            refs = [value] if isinstance(value, str) else value
            if not isinstance(refs, list):
                raise Unclear('plugin component の参照の形が違う')
            for ref in refs:
                for part in held_reference(expanded, manifest['root'], ref, directory=True):
                    rel = part['relative'].lower()
                    if (role == 'commands' and rel.endswith('.md')) or \
                            (role == 'skills' and (rel == 'skill.md' or rel.endswith('/skill.md'))):
                        commands_skills.add(part['path'])
    return agents - commands_skills


def checked_component_security(components):
    expanded = component_manifests(components)
    agent_paths = plugin_agent_only_paths(components)
    for item in expanded:
        if item['kind'] == 'marketplace-manifest':
            continue
        rel = item['relative']
        if rel == '.claude-plugin/plugin.json' or rel.endswith(MANIFEST_SUFFIX):
            manifest_security(item, expanded)
            manifest_grants(item['raw'])
        elif rel.lower().endswith('.md') or rel.endswith('/hooks.json') or rel == 'hooks.json' or \
                re.search(r'(?:^|/)(?:mods|modules)/[^/]+\.(?:[cm]?[jt]s|tsx?)\Z', rel):
            component_security(item['raw'], rel, item['path'] in agent_paths)
    strict_plugin_mcp(expanded)


def component_policy(state_path, expected, allowed_tools=()):
    components = verified_components(state_path, expected)
    checked_component_security(components)
    namespace = component_namespace(components)
    settings = verified_host_settings(state_path, expected)
    direct_allow, _ = direct_allow_policy(settings, allowed_tools)
    namespace = apply_user_skill_off(namespace, settings)
    agent_preloads(components, namespace)
    fixed = fixed_host_policy(namespace)
    assert_fixed_policy(settings, fixed)
    namespace = apply_user_skill_off(namespace, {'user': fixed})
    component_digest = verified_component_digest(state_path, expected)
    return {'component_sha256': component_digest,
            'policy_sha256': policy_digest(component_digest, namespace, fixed, direct_allow),
            'namespace': namespace, 'fixed': fixed}


def manifest_reference(item, components, ref):
    if not isinstance(ref, str) or not manifest_paths_ok(ref):
        raise Unclear('component manifest の参照が root 内相対パスでない')
    prefix = item['relative'][:-len('.claude-plugin/plugin.json')]
    while ref.startswith('./'):
        ref = ref[2:]
    return held_reference(components, item['root'], prefix + ref)


def manifest_security(item, components):
    data = strict_json(item['raw'], 'component manifest')
    if not isinstance(data, dict):
        raise Unclear('component manifest が対応表でない')
    def hooks(value):
        if value in (None, {}, [], ''):
            return
        refs = [value] if isinstance(value, str) else value
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            raise Unclear('component hooks に非空実行定義がある')
        for ref in refs:
            held = manifest_reference(item, components, ref)
            if len(held) != 1:
                raise Unclear('component hooks の参照を一意に解決できない')
            value = strict_json(held[0]['raw'], 'component hooks')
            if value not in (None, {}, [], {'hooks': {}}, {'hooks': []}):
                raise Unclear('component hooks の参照先に非空実行定義がある')
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                name = normalized_yaml_key(key)
                if name == 'hooks':
                    hooks(child)
                elif name == 'modules' and child not in (None, {}, [], ''):
                    raise Unclear('component modules が空でない')
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(data)


def strict_plugin_mcp(components):
    """MCP は実行せず、同一 root の保持済み JSON 参照だけを検査する。"""
    manifests = [item for item in components if item['relative'] == '.claude-plugin/plugin.json' or
                 item['relative'].endswith(MANIFEST_SUFFIX)]
    covered = {item['root'] for item in manifests if item['relative'] == '.claude-plugin/plugin.json'}
    # manifest 無しの plugin も default MCP の保持済み宣言は同じ検査を通す。
    for item in components:
        if item['kind'] == 'plugin' and item['root'] not in covered:
            manifests.append(dict(item, relative='.claude-plugin/plugin.json', raw=b'{}'))
            covered.add(item['root'])
    for item in manifests:
        data = strict_json(item['raw'], 'plugin.json')
        seen = set()
        def refs(value):
            if value in (None, {}, []):
                return
            if isinstance(value, str):
                reference(value)
            elif isinstance(value, list) and all(isinstance(ref, str) for ref in value):
                for ref in value:
                    reference(ref)
            elif isinstance(value, dict):
                if set(value) == {'mcpServers'}:
                    refs(value['mcpServers'])
                    return
                for server in value.values():
                    if not isinstance(server, dict):
                        raise Unclear('plugin MCP の server が対応表でない')
                    for key in ('source', 'config'):
                        if key in server:
                            reference(server[key])
            else:
                raise Unclear('plugin MCP の形が違う')
        def reference(ref):
            if not isinstance(ref, str) or not ref.startswith('./') or ref in seen:
                raise Unclear('plugin MCP の参照が root 内相対でないか循環している')
            seen.add(ref)
            held = manifest_reference(item, components, ref)
            if len(held) != 1:
                raise Unclear('plugin MCP の参照先が一意でない')
            refs(strict_json(held[0]['raw'], 'plugin MCP'))
        refs(data.get('mcpServers'))
        prefix = item['relative'][:-len('.claude-plugin/plugin.json')]
        defaults = [part for part in components if part['root'] == item['root'] and part['relative'] == prefix + '.mcp.json']
        for part in defaults:
            refs(strict_json(part['raw'], 'plugin MCP'))


def component_policy_output(policy):
    """loop に渡す固定 schema。component path・raw init・account 情報を混ぜない。"""
    namespace = policy['namespace']
    return {'version': 1, 'resolver_version': RESOLVER_VERSION, 'component_sha256': policy['component_sha256'],
            'policy_sha256': policy['policy_sha256'], 'fixed': policy['fixed'],
            'loaded_commands': namespace['loaded_commands'],
            'invocation_routes': namespace['invocation_routes'],
            'invocation_names': namespace['invocation_names'], 'lookup_names': namespace['lookup_names'],
            'model_invocation_names': namespace['model_invocation_names'],
            'public_names': namespace['public_names']}


def sanitize_init(raw, expected_public):
    """init の最小 schema を検証し、保存可能な名前だけを返す。raw は保存しない。"""
    value = strict_json(raw, 'init')
    if not isinstance(value, dict) or set(value) - {'type', 'subtype', 'skills'}:
        raise Stop('init の形が違う')
    if value.get('type') != 'init' or not isinstance(value.get('skills'), list):
        raise Stop('init の形が違う')
    names = []
    for item in value['skills']:
        name = item if isinstance(item, str) else None
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise Stop('init の skill 名の形が違う')
        names.append(name)
    if set(names) != set(expected_public):
        raise Stop('init の公開 skill 名が検査済み component と違う')
    return {'init_schema': INIT_SCHEMA_VERSION, 'public_names': sorted(names)}


def sanitize_supervisor(raw, expected_public):
    """host stdout から、prove に必要な result と検査済み init 名だけを保存形へ縮約する。"""
    value = strict_json(raw, 'supervisor')
    stream = value if isinstance(value, list) else [value]
    if not all(isinstance(item, dict) for item in stream):
        raise Stop('supervisor の出力の形が違う')
    init_items = [item for item in stream if item.get('type') == 'init' or
                  (item.get('type') == 'system' and item.get('subtype') == 'init')]
    results = [item for item in stream if item.get('type') == 'result']
    if len(init_items) != 1 or len(results) != 1:
        raise Stop('supervisor の init または result が一意でない')
    init = sanitize_init(json.dumps({'type': 'init', 'skills': init_items[0].get('skills')},
                                    ensure_ascii=True).encode(), expected_public)
    result = results[0]
    if type(result.get('is_error')) is not bool:
        raise Stop('supervisor の result の形が違う')
    clean = {'type': 'result', 'is_error': result['is_error']}
    denials = result.get('permission_denials', [])
    if not isinstance(denials, list):
        raise Stop('supervisor の permission_denials の形が違う')
    clean_denials = []
    for denial in denials:
        if not isinstance(denial, dict) or not isinstance(denial.get('tool_name'), str) or \
                not isinstance(denial.get('tool_input'), dict):
            raise Stop('supervisor の permission_denials の形が違う')
        # probe_judge が使う Write と file_path のみ。host の補助情報や account/id は残さない。
        if denial['tool_name'] != 'Write':
            continue
        tool_input = denial['tool_input']
        if not isinstance(tool_input.get('file_path'), str):
            raise Stop('supervisor の permission_denials の形が違う')
        if denial['tool_name'] == 'Write':
            clean_denials.append({'tool_name': 'Write', 'tool_input': {'file_path': tool_input['file_path']}})
    clean['permission_denials'] = clean_denials
    return {'init': init, 'result': clean}


def read_bounded_stdin(limit=MAX_TEXT):
    raw = sys.stdin.buffer.read(limit + 1)
    if len(raw) > limit:
        raise Stop('init の出力が読み取り上限を超える')
    return raw


SENSITIVE_COMPONENT_KEYS = frozenset(('hooks', 'mcpservers', 'permissionmode', 'modules'))


def normalized_yaml_key(value):
    return re.sub(r'[-_]', '', value).lower()


def component_frontmatter_security(raw, label, allow_plugin_agent_mcp=False):
    fields = component_fields(raw)
    for key in ('hooks', 'modules', 'mcpservers', 'permissionmode'):
        if key not in fields:
            continue
        value = fields[key]
        if allow_plugin_agent_mcp and key in ('hooks', 'mcpservers', 'permissionmode'):
            continue
        if key != 'permissionmode' and value in ('', '{}', '[]', 'null', '~'):
            continue
        if key == 'permissionmode' and scalar(value) in ('default', 'manual', 'acceptEdits', 'plan', 'dontAsk'):
            continue
        raise Unclear('component の ' + key + ' が許可された空値または mode でない')


def manifest_paths_ok(value):
    """マニフェストのパスの指定(文字列か文字列の列)が、installPath の中の相対パスだけか。"""
    paths = [value] if isinstance(value, str) else value
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return False
    return all(p and not p.startswith('/') and '\\' not in p and '\x00' not in p and
               '..' not in p.split('/') for p in paths)


def marketplace_security(component):
    """選択済み marketplace entry が保持した registry に一意に載ることを確かめる。"""
    data = strict_json(component['raw'], 'marketplace.json')
    selected = component.get('entry')
    if not isinstance(data, dict) or not isinstance(data.get('plugins'), list) or not isinstance(selected, list) or not selected:
        raise Unclear('選択 marketplace entry を解釈できない')
    selected_plugins = set()
    for item in selected:
        if not isinstance(item, dict) or set(item) != {'plugin', 'entry'} or not isinstance(item['plugin'], str) or \
                not isinstance(item['entry'], dict) or item['plugin'] in selected_plugins:
            raise Unclear('選択 marketplace entry を解釈できない')
        selected_plugins.add(item['plugin'])
        canonical = json.dumps(item['entry'], ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        matches = [known for known in data['plugins'] if isinstance(known, dict) and
                   json.dumps(known, ensure_ascii=True, sort_keys=True, separators=(',', ':')) == canonical]
        if len(matches) != 1:
            raise Unclear('選択 marketplace entry が一意でない')
        source = item['entry'].get('source')
        if source is not None and not manifest_paths_ok(source):
            raise Unclear('marketplace source が相対パスでない')
        if 'strict' in item['entry'] and not isinstance(item['entry']['strict'], bool):
            raise Unclear('marketplace entry の strict が真偽値でない')
        # 非空 inline 入口は参照解決を待たずに拒否する。
        for key in ('hooks', 'modules'):
            value = item['entry'].get(key)
            if value not in (None, {}, [], '') and not (key == 'hooks' and manifest_paths_ok(value)):
                raise Unclear('marketplace entry の実行入口が空でない')


def component_security(raw, label, allow_plugin_agent_mcp=False):
    """追加定義の実行入口を、値を展開・実行せず fail closed で判定する。"""
    component_frontmatter_security(raw, label, allow_plugin_agent_mcp)
    normalized_label = label.replace('\\', '/')
    if normalized_label.endswith('/hooks.json') or normalized_label == 'hooks.json':
        value = strict_json(raw, 'hooks.json')
        if value not in ({}, [], None, {'hooks': {}}, {'hooks': []}):
            raise Unclear('hooks.json が空でない')
    if re.search(r'(?:^|/)(?:mods|modules)/[^/]+\.(?:[cm]?[jt]s|tsx?)\Z', normalized_label):
        raise Unclear('component の mod module を実行しない')
    if label.endswith('plugin.json'):
        value = strict_json(raw, 'plugin.json')
        if not isinstance(value, dict):
            raise Unclear('plugin.json が対応表でない')
        def entries(item, trail='plugin.json'):
            if isinstance(item, dict):
                for key, child in item.items():
                    here = trail + '.' + str(key)
                    normalized = normalized_yaml_key(str(key))
                    if normalized in ('hooks', 'modules') and child not in (None, [], {}, ''):
                        raise Unclear(f'{here} が空でない')
                    if normalized == 'mcpservers' and not isinstance(child, (dict, list, type(None))):
                        raise Unclear(f'{here} の形が違う')
                    entries(child, here)
            elif isinstance(item, list):
                for number, child in enumerate(item):
                    entries(child, trail + '[' + str(number) + ']')
        entries(value)


def manifest_grants(raw):
    """plugin のマニフェストから、ホストが command の frontmatter の allowed-tools へ合成する規則の列。

    ホスト(2.1.289 を静的に読んだ)は、`commands` が対応表のとき、項目の `allowedTools` を `,` でつないで
    allowed-tools にし、インラインの `content` も frontmatter として読む。パスの指定は installPath の中だけを許す
    (外を指すと、控えにも検査にも入らない)。返り値は [(見出し, 規則の列)]。"""
    data = strict_json(raw, 'plugin.json')
    if not isinstance(data, dict):
        raise Unclear('plugin.json が対応表でない')
    out = []
    for field in ('commands', 'skills', 'agents'):
        value = data.get(field)
        if value is None or (field != 'commands' and manifest_paths_ok(value)):
            continue
        if field == 'commands' and manifest_paths_ok(value):
            continue
        if field != 'commands' or not isinstance(value, dict):
            raise Unclear(f'plugin.json の {field} の形が違うか、installPath の外を指す')
        for name, item in value.items():
            if not isinstance(item, dict):
                raise Unclear(f'plugin.json の commands の項目 が対応表でない')
            source = item.get('source')
            if source is not None and not manifest_paths_ok(source):
                raise Unclear(f'plugin.json の commands の項目 の source が installPath の外を指す')
            if 'allowedTools' in item:
                tools = item['allowedTools']
                if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                    raise Unclear(f'plugin.json の commands の項目 の allowedTools が文字列の列でない')
                out.append((f'commands.{name}.allowedTools', split_both(','.join(tools))))
            if 'content' in item:
                content = item['content']
                if not isinstance(content, str):
                    raise Unclear(f'plugin.json の commands の項目 の content が文字列でない')
                component_security(content.encode('utf-8'), 'inline-command.md')
                rules = frontmatter_grants(content.encode('utf-8'))
                if rules is not None:
                    out.append((f'commands.{name}.content', rules))
    return out


def grants(state_path, expected, user_settings, allowed_tools, classifier=False):
    value = load_state(state_path, expected)
    settings = verified_host_settings(state_path, expected)
    if not os.path.isabs(user_settings) or user_settings != settings['user_settings']:
        raise Stop('利用者の設定のパスが保持した絶対パスと違う')
    _, allow = direct_allow_policy(settings, allowed_tools)
    copy = value.get('copy')
    files = problems = 0
    lines = []
    components = verified_components(state_path, expected)
    # bundled plugin は snapshot が作った private copy を保持している。外部の
    # installPath を再読取する旧経路は使わない。
    if isinstance(copy, str):
        for key, entry in value.get('entries', {}).items():
            if not key.startswith('plugin/') or not isinstance(entry, list) or entry[:1] != ['file']:
                continue
            raw = read_regular(copy + key[len('plugin'):], MAX_TEXT)
            if sha256(raw) != entry[2]:
                raise Stop(f'控えた時から内容が変わった: {one_line(key, 300)}')
            components.append({'key': key, 'kind': 'plugin', 'root': copy, 'relative': key[len('plugin/'):],
                               'path': copy + key[len('plugin'):], 'raw': raw})
    checked_component_security(components)
    agent_paths = plugin_agent_only_paths(components)
    if any('skills' in component_fields(item['raw']) for item in plugin_agents(components)):
        agent_preloads(components, component_namespace(components))
    for component in sorted(component_manifests(components), key=lambda item: item['key']):
        shown, raw, relative = component['path'], component['raw'], component['relative']
        # host は .md を大文字小文字を区別せずに読む。manifest は active
        # marketplace に限り component reader が返す。
        normalized_relative = relative.replace('\\', '/')
        manifest = normalized_relative == '.claude-plugin/plugin.json' or normalized_relative.endswith(MANIFEST_SUFFIX)
        if not (relative.lower().endswith('.md') or manifest):
            continue
        files += 1
        try:
            if not manifest:
                component_security(raw, relative, component['path'] in agent_paths)
            if manifest:
                groups = manifest_grants(raw)
            else:
                rules = frontmatter_grants(raw)
                groups = [] if rules is None else [('', rules)]
            found = [(label, judge_rules(validate_direct_allow_rules(rules, component['kind']), allow, classifier))
                     for label, rules in groups]
        except Unclear as exc:
            problems += 1
            lines.append(f"kind={component['kind']}: allowed-tools を解釈できない")
            continue
        for label, wider in found:
            for rule in wider:
                problems += 1
                lines.append(f"kind={component['kind']}: allowed-tools が許可リストより広い")
    if problems:
        raise Stop('skill・command の allowed-tools が無人の周の権限を広げうる:\n' + '\n'.join(lines[:50]))
    return files


def main(argv=None):
    p = argparse.ArgumentParser(description='無人ループのホスト CLI の証明と allowed-tools の検査')
    sub = p.add_subparsers(dest='command', required=True)
    c = sub.add_parser('identity'); c.add_argument('--host', required=True)
    c = sub.add_parser('same'); c.add_argument('--host', required=True); c.add_argument('--identity', required=True)
    c.add_argument('--hash', action='store_true')
    c = sub.add_parser('proof-get')
    for name in ('--store', '--identity', '--shape'):
        c.add_argument(name, required=True)
    c.add_argument('--component-sha256')
    c.add_argument('--policy-sha256')
    c = sub.add_parser('proof-match')
    for name in ('--store', '--identity', '--shape', '--version-file', '--help-file'):
        c.add_argument(name, required=True)
    c.add_argument('--component-sha256', required=True)
    c.add_argument('--policy-sha256', required=True)
    c = sub.add_parser('probe-judge')
    for name in ('--out', '--permlog', '--target'):
        c.add_argument(name, required=True)
    c = sub.add_parser('proof-write')
    for name in ('--store', '--identity', '--shape', '--version-file', '--help-file', '--probe', '--plugin-version',
                 '--component-sha256', '--policy-sha256', '--init-evidence'):
        c.add_argument(name, required=True)
    c.add_argument('--public-name', action='append', default=[])
    c.add_argument('--resolver-version', required=True, type=int)
    c = sub.add_parser('grants')
    for name in ('--state', '--expect-sha256', '--user-settings'):
        c.add_argument(name, required=True)
    c.add_argument('--allowed-tools', action='append', default=[])
    c.add_argument('--classifier', action='store_true')
    c = sub.add_parser('component-policy')
    c.add_argument('--allowed-tools', action='append', default=[])
    for name in ('--state', '--expect-sha256'):
        c.add_argument(name, required=True)
    c = sub.add_parser('init-sanitize')
    c.add_argument('--allowed-tools', action='append', default=[])
    for name in ('--state', '--expect-sha256'):
        c.add_argument(name, required=True)
    a = p.parse_args(argv)
    try:
        if a.command == 'identity':
            print(json.dumps(identity(a.host), sort_keys=True))
        elif a.command == 'same':
            same(a.host, parse_identity(a.identity), a.hash)
        elif a.command == 'proof-get':
            path, value = load_proof(a.store, parse_identity(a.identity), a.shape,
                                     a.component_sha256, a.policy_sha256)
            print(path)
        elif a.command == 'proof-match':
            path, value = proof_match(a.store, parse_identity(a.identity), a.shape, a.version_file, a.help_file,
                                      a.component_sha256, a.policy_sha256)
            print(f"{path}\t{one_line(value.get('host_version', ''), 120)}\t{one_line(value['probe'].get('date', ''), 40)}")
        elif a.command == 'probe-judge':
            print(json.dumps(probe_judge(a.out, a.permlog, a.target), sort_keys=True))
        elif a.command == 'proof-write':
            probe = json.loads(a.probe)
            if not isinstance(probe, dict):
                raise Stop('確認の結果の形が違う')
            init_evidence = strict_json(a.init_evidence.encode('utf-8'), 'init の縮約証拠')
            print(proof_write(a.store, parse_identity(a.identity), a.shape, a.version_file, a.help_file, probe,
                              a.plugin_version, a.component_sha256, a.policy_sha256, a.public_name,
                              a.resolver_version, init_evidence))
        elif a.command == 'grants':
            files = grants(a.state, a.expect_sha256, a.user_settings, a.allowed_tools, a.classifier)
            print(f"files={files} yaml={'on' if yaml_loader() else 'off'}")
        elif a.command == 'component-policy':
            print(json.dumps(component_policy_output(component_policy(a.state, a.expect_sha256, a.allowed_tools)),
                             ensure_ascii=True, sort_keys=True))
        elif a.command == 'init-sanitize':
            policy = component_policy(a.state, a.expect_sha256, a.allowed_tools)
            print(json.dumps(sanitize_supervisor(read_bounded_stdin(), policy['namespace']['public_names']),
                             ensure_ascii=True, sort_keys=True))
        return 0
    except Missing as exc:
        print(f'ERROR [host-check] 証明が無い: {exc}', file=sys.stderr)
        return 3
    except (Stop, OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        detail = str(exc) if isinstance(exc, Stop) else f'検査を完了できない({type(exc).__name__}: {one_line(exc, 200)})'
        print(f'ERROR [host-check] {detail}', file=sys.stderr)
        return 20


if __name__ == '__main__':
    raise SystemExit(main())
