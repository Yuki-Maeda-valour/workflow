#!/usr/bin/env python3
"""R4E(#482)の旧版の固定期待(代表の 5 ケース)を作る。

使い方(リポジトリの直下で):
  python3 -B native/fixtures/r4e/generate_expected.py --legacy-python <旧版用の venv の python>           # 書き出す
  python3 -B native/fixtures/r4e/generate_expected.py --legacy-python <旧版用の venv の python> --check   # 一致を確かめる

- 旧版用の Python は、本家 CPython 3.10.11・openpyxl 3.1.5・et_xmlfile 2.0.0・lxml なしに限る。
  違えば何も書かずに失敗する(終了 1)。venv は native/ci/r4e-requirements.txt から作る。
- 各ケースは、使い捨ての置き場(work・home・tmp)に入力を置き、work を現在のディレクトリにして
  `<python> <旧版のスクリプト> <引数...>` を流す。環境は PYTHONUTF8=1・TZ・PATH・HOME・TMPDIR だけ。umask は 022。
- 保存するもの(native/fixtures/r4e/expected/):
  - manifest.json: 入力・引数・終了値・標準出力・標準エラー・作られたファイル・ZIP の各部分の欄と SHA-256・
    テーマと書式の SHA-256。標準出力と標準エラーの置き場のパスは決まった印に置き換える。
  - <ケース>/<部分の名前>: 展開した各部分のバイト。docProps/core.xml の 2 つの時刻だけは印に置き換える。
- 時刻は、置き換える前に形式と、実行の前後の範囲に入ることを確かめる。
- 圧縮後の大きさと ZIP の時刻の値は保存しない(決定済みの差 Q3・XLSX のバイト)。
- --check は、置き場を作り直して旧版を流し、印へ置き換えた後の内容が、保存したファイルとバイト単位で
  一致するかを確かめる。一致しなければ終了 1。
"""

import argparse
import calendar
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[3]
EXPECTED = Path(__file__).resolve().parent / 'expected'
EXPECTED_REL = 'native/fixtures/r4e/expected'
LEGACY_REL = 'plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py'
LEGACY = ROOT / LEGACY_REL
SCRIPT_NAME = 'md_tables_to_xlsx.py'
TZ = 'UTC'
PATH = '/usr/bin:/bin'
UMASK = 0o022
TIMEOUT = 120
WORK_MARK = b'@WORK@'
TIME_MARKS = {'created': '@CREATED@', 'modified': '@MODIFIED@'}
CORE_TIME = re.compile(
    rb'<dcterms:(created|modified) xsi:type="dcterms:W3CDTF">([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)</dcterms:\1>'
)
PART_ORDER_HEAD = ('docProps/app.xml', 'docProps/core.xml', 'xl/theme/theme1.xml')
PART_ORDER_TAIL = ('xl/styles.xml', '_rels/.rels', 'xl/workbook.xml', 'xl/_rels/workbook.xml.rels', '[Content_Types].xml')

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

# 代表の 5 ケース。各部分のバイトが違う軸で選ぶ(設計 Task 1.1)。
CASES = [
    {
        'id': '01-basic',
        'summary': '1 つの表の基本形(見出しつき・3 列・2 行。出力先の親を作る)',
        'linux_only': False,
        'files': [
            {
                'path': 'docs/basic.md',
                'text': '# 概要\n\n| 名前 | 値 | 備考 |\n|---|---|---|\n| a | 1 | x |\n| b | 2 | y |\n',
            },
        ],
        'argv': ['docs/basic.md', '-o', 'out/basic.xlsx'],
        'output': 'out/basic.xlsx',
    },
    {
        'id': '02-duplicates',
        'summary': '複数の表と重複名(旧版の _2・_3、31 文字で切った名前の _2、openpyxl の大文字小文字違いの番号)・2 つの入力',
        'linux_only': False,
        'files': [
            {
                'path': 'docs/Report.md',
                'text': (
                    '# 一覧\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n'
                    '| c | d |\n|---|---|\n| 3 | 4 |\n\n'
                    '## 一覧\n\n| e |\n|---|\n| 5 |\n\n'
                    '# ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n\n| f |\n|---|\n| 6 |\n\n| g |\n|---|\n| 7 |\n\n'
                    '# Summary\n\n| h |\n|---|\n| 8 |\n\n'
                    '# SUMMARY\n\n| i |\n|---|\n| 9 |\n\n'
                    '# summary\n\n| j |\n|---|\n| 10 |\n'
                ),
            },
            {
                'path': 'docs/other.md',
                'text': '| k | l |\n| :---: | ---: |\n| 11 | 12 |\n',
            },
        ],
        'argv': ['docs/Report.md', 'docs/other.md', '-o', 'out/duplicates.xlsx'],
        'output': 'out/duplicates.xlsx',
    },
    {
        'id': '03-types',
        'summary': '数式(1 文字の = は文字列)・誤りの 7 語・空のセル・列の足りない行・見出しより右の空セル(1 行目の太字の空セル)',
        'linux_only': False,
        'files': [
            {
                'path': 'docs/types.md',
                'text': (
                    '# 型\n\n'
                    '| 式 | 誤り | 空 |\n'
                    '|---|---|---|\n'
                    '| =SUM(1,2) | #N/A | |\n'
                    '| = | #DIV/0! | x |\n'
                    '| =A1&"x"<2 | #NAME? | |\n'
                    '| ==1 | #NULL! | | 右 | さらに右 |\n'
                    '| a | #NUM! |\n'
                    '| b | #REF! | |\n'
                    '| c | #VALUE! | |\n'
                    '| d | #n/a | #N/A  |\n'
                    '| | | |\n'
                ),
            },
        ],
        'argv': ['docs/types.md', '-o', 'out/types.xlsx'],
        'output': 'out/types.xlsx',
    },
    {
        'id': '04-long-cjk',
        'summary': '長い値(列幅 60 の上限)・CJK(幅 2)・前後に空白の残る値・<br> の改行・装飾の除去',
        'linux_only': False,
        'files': [
            {
                'path': 'docs/long.md',
                'text': (
                    '# 長い値と日本語\n\n'
                    '| 項目 | 説明 | 備考 |\n'
                    '|---|---|---|\n'
                    '| 日本語の項目名 | ' + 'あいうえおかきくけこ' * 4 + ' | ' + 'x' * 100 + ' |\n'
                    '| a<br>b | 前<BR/>後 | 1行目<br />2行目はとても長いが列幅に数えない' + 'z' * 70 + ' |\n'
                    '| **太字** と `code` | [リンク](https://example.com/a) | **  前後の空白  ** |\n'
                    '| x<br> | <br  />y | ` コード ` |\n'
                    '| \u3000全角の空白\u3000 | 半角ｶﾅ | tab\tinside |\n'
                ),
            },
        ],
        'argv': ['docs/long.md', '-o', 'out/long.xlsx'],
        'output': 'out/long.xlsx',
    },
    {
        'id': '05-escape-nonutf8',
        'summary': 'UTF-8 でないファイル名のシート名(サロゲート)・エスケープの要る見出しとセル(& < > " \')・シート名の禁止文字',
        'linux_only': True,
        'files': [
            {
                'path_hex': (b'docs/r\xe9sum\xe9.md').hex(),
                'text': (
                    '# A & B < C > "D" \'E\'\n\n'
                    '| 記号 | 値 |\n|---|---|\n| & | <tag attr="v"> |\n| \'q\' | a > b & c < d |\n\n'
                    '# x/y\\z*?[1]:2\n\n| k |\n|---|\n| v |\n'
                ),
            },
        ],
        'argv': [{'hex': (b'docs/r\xe9sum\xe9.md').hex()}, '-o', 'out/escape.xlsx'],
        'output': 'out/escape.xlsx',
    },
]


class FixtureError(Exception):
    pass


def require_environment(python):
    if not python or not os.path.isabs(python):
        raise FixtureError('--legacy-python は絶対パスで指定してください')
    if not os.path.isfile(python) or not os.access(python, os.X_OK):
        raise FixtureError('--legacy-python が実行可能な通常ファイルでない')
    result = subprocess.run(
        [python, '-I', '-B', '-c', PROBE],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={'PATH': PATH},
        timeout=TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        raise FixtureError(f'旧版用の Python を調べられない(終了 {result.returncode})')
    info = json.loads(result.stdout)
    problems = []
    if info['implementation'] != 'cpython' or info['version'] != [3, 10, 11]:
        problems.append(f"CPython 3.10.11 でない: {info['implementation']} {info['version']}")
    if 'import_error' in info:
        problems.append(f"openpyxl を読み込めない: {info['import_error']}")
    else:
        if info['openpyxl'] != '3.1.5':
            problems.append(f"openpyxl が 3.1.5 でない: {info['openpyxl']}")
        if info['et_xmlfile'] != '2.0.0':
            problems.append(f"et_xmlfile が 2.0.0 でない: {info['et_xmlfile']}")
        if info['openpyxl_lxml']:
            problems.append('openpyxl が lxml を使う')
    if info['lxml']:
        problems.append('lxml が読み込める')
    if problems:
        raise FixtureError('旧版の環境が違う: ' + ' / '.join(problems))
    return info


def os_path(entry):
    if 'path_hex' in entry:
        return bytes.fromhex(entry['path_hex'])
    return entry['path'].encode('utf-8')


def os_arg(value):
    if isinstance(value, dict):
        return bytes.fromhex(value['hex'])
    return value.encode('utf-8')


def tree(root):
    """置き場の中の一覧(atime を除く)。"""
    found = {}
    for current, dirs, files in os.walk(root):
        for name in sorted(dirs) + sorted(files):
            path = os.path.join(current, name)
            info = os.lstat(path)
            rel = os.path.relpath(path, root)
            kind = stat.S_IFMT(info.st_mode)
            body = None
            if stat.S_ISREG(info.st_mode):
                with open(path, 'rb') as handle:
                    body = hashlib.sha256(handle.read()).hexdigest()
            elif stat.S_ISLNK(info.st_mode):
                body = os.readlink(path)
            found[rel] = (kind, stat.S_IMODE(info.st_mode), body, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
    return found


def normalize(data, replacements):
    for old, new in replacements:
        data = data.replace(old, new)
    return data


def text_or_hex(data):
    try:
        return {'text': data.decode('utf-8')}
    except UnicodeDecodeError:
        return {'hex': data.hex()}


def check_core_times(data, before, after):
    found = CORE_TIME.findall(data)
    if [name for name, _ in found] != [b'created', b'modified']:
        raise FixtureError('docProps/core.xml の時刻の形が想定と違う')
    low = int(before)
    for _, value in found:
        moment = calendar.timegm(time.strptime(value.decode(), '%Y-%m-%dT%H:%M:%SZ'))
        if not low <= moment <= after:
            raise FixtureError('docProps/core.xml の時刻が実行の前後の範囲に入らない')

    def mark(match):
        name = match.group(1).decode()
        return match.group(0).replace(match.group(2), TIME_MARKS[name].encode())

    return CORE_TIME.sub(mark, data)


def check_zip_time(info, before, after):
    # TZ=UTC で流すので、現地時刻は UTC。ZIP の時刻は 2 秒単位で切り捨てる。
    moment = calendar.timegm(info.date_time + (0, 0, 0))
    low = int(before) - int(before) % 2
    if not low <= moment <= after:
        raise FixtureError(f'ZIP の時刻が実行の前後の範囲に入らない: {info.filename}')


def zip_parts(path, before, after):
    with zipfile.ZipFile(path) as archive:
        if archive.comment != b'':
            raise FixtureError('ZIP の注記が空でない')
        if archive.testzip() is not None:
            raise FixtureError('ZIP の部分が壊れている')
        parts = []
        files = {}
        for info in archive.infolist():
            data = archive.read(info)
            check_zip_time(info, before, after)
            if info.filename == 'docProps/core.xml':
                data = check_core_times(data, before, after)
            files[info.filename] = data
            parts.append(
                {
                    'name': info.filename,
                    'file_size': info.file_size,
                    # 印を置いた部分の CRC-32 は時刻で変わるので保存しない(testzip で元のバイトと照らした)。
                    'crc32': None if info.filename == 'docProps/core.xml' else f'{info.CRC:08x}',
                    'sha256': hashlib.sha256(data).hexdigest(),
                    'marked': info.filename == 'docProps/core.xml',
                    'compress_type': info.compress_type,
                    'create_system': info.create_system,
                    'create_version': info.create_version,
                    'extract_version': info.extract_version,
                    'flag_bits': info.flag_bits,
                    'volume': info.volume,
                    'internal_attr': info.internal_attr,
                    'external_attr': f'0o{info.external_attr >> 16:o}<<16' if info.external_attr & 0xFFFF == 0 else hex(info.external_attr),
                    'extra': info.extra.hex(),
                    'comment': info.comment.hex(),
                }
            )
    names = [part['name'] for part in parts]
    sheets = names[len(PART_ORDER_HEAD) : len(names) - len(PART_ORDER_TAIL)]
    if (
        tuple(names[: len(PART_ORDER_HEAD)]) != PART_ORDER_HEAD
        or tuple(names[len(names) - len(PART_ORDER_TAIL) :]) != PART_ORDER_TAIL
        or sheets != [f'xl/worksheets/sheet{index}.xml' for index in range(1, len(sheets) + 1)]
    ):
        raise FixtureError(f'ZIP の部分の順が想定と違う: {names}')
    return parts, files


def run_case(python, info, case, scratch):
    base = Path(scratch) / case['id']
    work, home, tmp = base / 'work', base / 'home', base / 'tmp'
    for directory in (work, home, tmp):
        directory.mkdir(parents=True)
    for entry in case['files']:
        path = os.path.join(os.fsencode(work), os_path(entry))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(entry['text'].encode('utf-8'))
    env = {'PYTHONUTF8': '1', 'TZ': TZ, 'PATH': PATH, 'HOME': str(home), 'TMPDIR': str(tmp)}
    argv = [os.fsencode(python), os.fsencode(LEGACY)] + [os_arg(value) for value in case['argv']]
    before_tree = tree(work)
    before = time.time()
    result = subprocess.run(
        argv,
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        umask=UMASK,
        timeout=TIMEOUT,
        check=False,
    )
    after = time.time()
    after_tree = tree(work)
    for rel, value in before_tree.items():
        if after_tree.get(rel) != value:
            raise FixtureError(f'{case["id"]}: 入力が変わった: {rel}')
    if os.listdir(tmp) or os.listdir(home):
        raise FixtureError(f'{case["id"]}: TMPDIR か HOME に何かが残った')
    created = []
    for rel in sorted(set(after_tree) - set(before_tree)):
        kind, mode = after_tree[rel][:2]
        created.append({'path': rel, 'type': 'dir' if kind == stat.S_IFDIR else 'file' if kind == stat.S_IFREG else oct(kind), 'mode': f'0o{mode:o}'})
    expected_created = []
    parent = os.path.dirname(case['output'])
    while parent:
        expected_created.append(parent)
        parent = os.path.dirname(parent)
    if [item['path'] for item in created] != sorted(expected_created + [case['output']]):
        raise FixtureError(f'{case["id"]}: 作られたファイルが想定と違う: {created}')
    replacements = [
        (os.fsencode(LEGACY), SCRIPT_NAME.encode()),
        (os.fsencode(info['site_packages']) + b'/', b''),
        (os.fsencode(info['stdlib']) + b'/', b''),
        (os.fsencode(work), WORK_MARK),
    ]
    parts, files = zip_parts(work / case['output'], before, after)
    record = {
        'id': case['id'],
        'summary': case['summary'],
        'linux_only': case['linux_only'],
        'files': [
            {**({'path_hex': entry['path_hex']} if 'path_hex' in entry else {'path': entry['path']}), 'text': entry['text']}
            for entry in case['files']
        ],
        'argv': case['argv'],
        'output': case['output'],
        'exit_code': result.returncode,
        'stdout': text_or_hex(normalize(result.stdout, replacements)),
        'stderr': text_or_hex(normalize(result.stderr, replacements)),
        'created': created,
        'parts': parts,
    }
    if result.returncode != 0:
        raise FixtureError(f'{case["id"]}: 旧版が失敗した(終了 {result.returncode})')
    return record, files


def build(python):
    info = require_environment(python)
    with open(LEGACY, 'rb') as handle:
        legacy_sha = hashlib.sha256(handle.read()).hexdigest()
    outputs = {}
    records = []
    with tempfile.TemporaryDirectory(prefix='r4e-expected-') as scratch:
        for case in CASES:
            record, files = run_case(python, info, case, scratch)
            records.append(record)
            for name, data in files.items():
                outputs[f'{case["id"]}/{name}'] = data
    fixed = {}
    for name in ('xl/theme/theme1.xml', 'xl/styles.xml'):
        values = {next(part['sha256'] for part in record['parts'] if part['name'] == name) for record in records}
        if len(values) != 1:
            raise FixtureError(f'{name} がケースごとに違う')
        fixed[name] = values.pop()
    manifest = {
        'schema_version': 1,
        'issue': 482,
        'generator': 'native/fixtures/r4e/generate_expected.py',
        'legacy': {
            'script': LEGACY_REL,
            'script_sha256': legacy_sha,
            'python': 'CPython 3.10.11',
            'openpyxl': '3.1.5',
            'et_xmlfile': '2.0.0',
            'lxml': False,
            'requirements': 'native/ci/r4e-requirements.txt',
        },
        'run': {
            'env': {'PYTHONUTF8': '1', 'TZ': TZ, 'PATH': PATH, 'HOME': '<置き場>/home', 'TMPDIR': '<置き場>/tmp'},
            'umask': f'0o{UMASK:03o}',
            'cwd': '<置き場>/work',
            'command': ['<旧版用の python>', SCRIPT_NAME, '<argv...>'],
        },
        'markers': {
            'work': WORK_MARK.decode(),
            'script': f'{SCRIPT_NAME}(旧版のスクリプトの絶対パス)',
            'site_packages': '(site-packages の絶対パスと / を消して相対にする)',
            'stdlib': '(標準ライブラリのディレクトリの絶対パスと / を消して相対にする)',
            'core_xml_times': TIME_MARKS,
        },
        'not_stored': [
            'ZIP の圧縮後の大きさと圧縮のバイト列(Q3)',
            'ZIP の時刻の値(XLSX のバイト)',
            'docProps/core.xml の 2 つの時刻の値と、その部分の CRC-32(sha256 は印を置いた後のバイト)',
        ],
        'fixed_parts_sha256': fixed,
        'cases': records,
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    outputs['manifest.json'] = text.encode('utf-8')
    return outputs


def current_files():
    found = {}
    if not EXPECTED.is_dir():
        return found
    for path in EXPECTED.rglob('*'):
        if path.is_file() or path.is_symlink():
            found[path.relative_to(EXPECTED).as_posix()] = path.read_bytes()
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description='R4E の旧版の固定期待を作る')
    parser.add_argument('--legacy-python', required=True, help='旧版用の venv の python(絶対パス)')
    parser.add_argument('--check', action='store_true', help='作り直した内容と保存したファイルが一致するかだけを確かめる')
    args = parser.parse_args(argv)
    try:
        outputs = build(args.legacy_python)
    except (FixtureError, OSError, subprocess.SubprocessError, ValueError, zipfile.BadZipFile) as error:
        print(f'失敗: {error}', file=sys.stderr)
        return 1
    if args.check:
        current = current_files()
        missing = sorted(set(outputs) - set(current))
        extra = sorted(set(current) - set(outputs))
        changed = sorted(name for name in set(outputs) & set(current) if outputs[name] != current[name])
        if missing or extra or changed:
            for label, names in (('無い', missing), ('余分', extra), ('違う', changed)):
                for name in names:
                    print(f'{label}: {EXPECTED_REL}/{name}', file=sys.stderr)
            print('失敗: 固定期待が旧版の結果と一致しない', file=sys.stderr)
            return 1
        print(f'一致: {EXPECTED_REL}({len(outputs)} ファイル)')
        return 0
    if EXPECTED.exists():
        shutil.rmtree(EXPECTED)
    for name, data in sorted(outputs.items()):
        path = EXPECTED / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f'書いた: {EXPECTED_REL}({len(outputs)} ファイル)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
