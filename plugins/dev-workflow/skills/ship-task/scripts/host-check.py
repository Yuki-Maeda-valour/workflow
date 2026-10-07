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
import re
import stat
import sys
import time

MAX_BINARY = 2 * 1024 * 1024 * 1024
MAX_TEXT = 4 * 1024 * 1024
MAX_STATE = 64 * 1024 * 1024
MAX_PROOF = 64 * 1024
MAX_SECONDS = 120.0
PROOF_VERSION = 1
PROOF_BOUND = ('realpath', 'dev', 'ino', 'size', 'sha256')


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


def load_proof(store, held, shape):
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
    probe = value.get('probe')
    if not isinstance(probe, dict) or probe.get('hook') != 'invoked' or probe.get('decision') != 'deny' \
            or probe.get('tool') != 'Write' or probe.get('file_absent') is not True:
        raise Stop(f'証明に実 hook の拒否の記録が無い: {path}')
    return path, value


def output_digest(path):
    return sha256(read_regular(path, MAX_TEXT))


def proof_match(store, held, shape, version_file, help_file):
    path, value = load_proof(store, held, shape)
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


def proof_write(store, held, shape, version_file, help_file, probe, plugin_version):
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
        'probe': dict(probe, date=time.strftime('%Y-%m-%dT%H:%M:%S%z'), plugin_version=plugin_version),
    }
    ensure_store(store)
    path = proof_file(store, held, shape)
    write_new_file(store.rstrip('/'), os.path.basename(path), (json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode())
    return path


def result_object(raw):
    value = json.loads(raw)
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
            raise Unclear(f'allowed-tools に似た鍵がある({one_line(key, 60)})')
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
            raise Unclear(f'同じ規則が重複している({one_line(rule, 80)})')
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


def user_allow_rules(path, entries):
    key = 'external:' + path
    held = entries.get(key)
    if held is None:
        raise Stop('利用者の設定が環境の控えに無い')
    if held == ['missing']:
        if os.path.lexists(path):
            raise Stop('控えた時に無かった利用者の設定がある')
        return []
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_TEXT:
            raise Stop('利用者の設定が通常ファイルでない')
        raw = os.read(fd, MAX_TEXT + 1)
    finally:
        os.close(fd)
    if not isinstance(held, list) or len(held) != 3 or sha256(raw) != held[2]:
        raise Stop('利用者の設定が控えた時と違う')
    try:
        data = json.loads(raw)
    except ValueError:
        raise Stop('利用者の設定の JSON を読めない') from None
    permissions = data.get('permissions') if isinstance(data, dict) else None
    allow = permissions.get('allow') if isinstance(permissions, dict) else None
    if not isinstance(allow, list):
        return []
    # 字面のまま渡す(ホストは設定の規則を整えずに読むので、前後の空白がある規則は何も許さない。parse_rule で unknown)
    return [r for r in allow if isinstance(r, str)]


MANIFEST_SUFFIX = '/.claude-plugin/plugin.json'


def manifest_paths_ok(value):
    """マニフェストのパスの指定(文字列か文字列の列)が、installPath の中の相対パスだけか。"""
    paths = [value] if isinstance(value, str) else value
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return False
    return all(p and not p.startswith('/') and '..' not in p.replace('\\', '/').split('/') for p in paths)


def manifest_grants(raw):
    """plugin のマニフェストから、ホストが command の frontmatter の allowed-tools へ合成する規則の列。

    ホスト(2.1.289 を静的に読んだ)は、`commands` が対応表のとき、項目の `allowedTools` を `,` でつないで
    allowed-tools にし、インラインの `content` も frontmatter として読む。パスの指定は installPath の中だけを許す
    (外を指すと、控えにも検査にも入らない)。返り値は [(見出し, 規則の列)]。"""
    try:
        data = json.loads(raw)
    except ValueError:
        raise Unclear('plugin.json を JSON として読めない') from None
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
                raise Unclear(f'plugin.json の commands の項目 {one_line(name, 60)} が対応表でない')
            source = item.get('source')
            if source is not None and not manifest_paths_ok(source):
                raise Unclear(f'plugin.json の commands の項目 {one_line(name, 60)} の source が installPath の外を指す')
            if 'allowedTools' in item:
                tools = item['allowedTools']
                if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                    raise Unclear(f'plugin.json の commands の項目 {one_line(name, 60)} の allowedTools が文字列の列でない')
                out.append((f'commands.{name}.allowedTools', split_both(','.join(tools))))
            if 'content' in item:
                content = item['content']
                if not isinstance(content, str):
                    raise Unclear(f'plugin.json の commands の項目 {one_line(name, 60)} の content が文字列でない')
                rules = frontmatter_grants(content.encode('utf-8'))
                if rules is not None:
                    out.append((f'commands.{name}.content', rules))
    return out


def grants(state_path, expected, user_settings, allowed_tools, classifier=False):
    value = load_state(state_path, expected)
    entries = value['entries']
    user_dirs = [d for d in value.get('specs', {}).get('user_directories', []) if isinstance(d, str)]
    allow = []
    for v in allowed_tools:
        try:
            allow += split_both(v)  # ホストは ' ' と ',' だけで区切る。タブ・改行を含む値は読み方が分かれる
        except Unclear:
            raise Stop(f'--allowed-tools の値の切り方がホストと違う(タブ・改行・入れ子の括弧など): {one_line(v, 120)}') from None
    allow += user_allow_rules(os.path.abspath(user_settings), entries)
    copy = value.get('copy')
    files = problems = 0
    lines = []
    for key in sorted(entries):
        entry = entries[key]
        # ホストは .md を大文字小文字を区別せずに読む。plugin のマニフェストも合成の元になる
        manifest = key.endswith(MANIFEST_SUFFIX) or key == 'plugin/.claude-plugin/plugin.json'
        if not (key.lower().endswith('.md') or manifest) or not isinstance(entry, list) or entry[:1] != ['file']:
            continue
        if key.startswith('plugin/') and isinstance(copy, str):
            shown, raw = key, read_regular(copy + key[len('plugin'):], MAX_TEXT)
        elif key.startswith('installed:'):
            # 控えは有効な plugin の installPath ごとにだけ installed: の項目を作る(正規化した字面)ので、すべて調べる
            shown = key[len('installed:'):]
            raw = read_regular(shown, MAX_TEXT)
        elif key.startswith('external:'):
            shown = key[len('external:'):]
            base = next((d for d in user_dirs if shown.startswith(d.rstrip('/') + '/')), None)
            if base is None:
                continue
            raw = read_logical(shown, entries, base.rstrip('/'))
        else:
            continue
        if sha256(raw) != entry[2]:
            raise Stop(f'控えた時から内容が変わった: {one_line(shown, 300)}')
        files += 1
        try:
            if manifest:
                groups = manifest_grants(raw)
            else:
                rules = frontmatter_grants(raw)
                groups = [] if rules is None else [('', rules)]
            found = [(label, judge_rules(rules, allow, classifier)) for label, rules in groups]
        except Unclear as exc:
            problems += 1
            lines.append(f'{one_line(shown, 300)}: allowed-tools を解釈できない({exc})')
            continue
        for label, wider in found:
            where = f'{one_line(shown, 300)}{" の " + one_line(label, 80) if label else ""}'
            for rule in wider:
                problems += 1
                lines.append(f'{where}: allowed-tools の {one_line(rule, 120)} が許可リストより広い')
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
    c = sub.add_parser('proof-match')
    for name in ('--store', '--identity', '--shape', '--version-file', '--help-file'):
        c.add_argument(name, required=True)
    c = sub.add_parser('probe-judge')
    for name in ('--out', '--permlog', '--target'):
        c.add_argument(name, required=True)
    c = sub.add_parser('proof-write')
    for name in ('--store', '--identity', '--shape', '--version-file', '--help-file', '--probe', '--plugin-version'):
        c.add_argument(name, required=True)
    c = sub.add_parser('grants')
    for name in ('--state', '--expect-sha256', '--user-settings'):
        c.add_argument(name, required=True)
    c.add_argument('--allowed-tools', action='append', default=[])
    c.add_argument('--classifier', action='store_true')
    a = p.parse_args(argv)
    try:
        if a.command == 'identity':
            print(json.dumps(identity(a.host), sort_keys=True))
        elif a.command == 'same':
            same(a.host, parse_identity(a.identity), a.hash)
        elif a.command == 'proof-get':
            path, value = load_proof(a.store, parse_identity(a.identity), a.shape)
            print(path)
        elif a.command == 'proof-match':
            path, value = proof_match(a.store, parse_identity(a.identity), a.shape, a.version_file, a.help_file)
            print(f"{path}\t{one_line(value.get('host_version', ''), 120)}\t{one_line(value['probe'].get('date', ''), 40)}")
        elif a.command == 'probe-judge':
            print(json.dumps(probe_judge(a.out, a.permlog, a.target), sort_keys=True))
        elif a.command == 'proof-write':
            probe = json.loads(a.probe)
            if not isinstance(probe, dict):
                raise Stop('確認の結果の形が違う')
            print(proof_write(a.store, parse_identity(a.identity), a.shape, a.version_file, a.help_file, probe, a.plugin_version))
        elif a.command == 'grants':
            files = grants(a.state, a.expect_sha256, a.user_settings, a.allowed_tools, a.classifier)
            print(f"files={files} yaml={'on' if yaml_loader() else 'off'}")
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
