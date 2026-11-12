#!/usr/bin/env python3
"""R4E(#482)の比較のケースを作る。

使い方(リポジトリの直下で。どの Python 3.10 以上でもよい):
  python3 -B native/fixtures/r4e/generate_cases.py           # cases.json を書き出す
  python3 -B native/fixtures/r4e/generate_cases.py --check   # 2 回作って同じで、cases.json と一致するかを確かめる
  python3 -B native/fixtures/r4e/generate_cases.py --materialize <空のディレクトリ> --group <番号>
                                                             # 生成したケースの 1 組を書き出して見る

- cases.json は、比較器(native/ci/check_r4e.py)が使う必須のケースの一覧。形は SCHEMA に書く。
- 生成したケースは、決まった種(SEED)と組の番号から作る。乱数は SHA-256 の数え上げで作るので、
  Python の版や機械に依らず、同じ種と番号から同じバイトになる。
- 大きな入力(1 MiB・16385 列など)は、build の名前と引数だけを cases.json に書き、中身は
  build_content() で作る。比較器はこのファイルを読み込んで build_content() と generated_group() を使う。
- 決定済みの差の名前は、設計(Issue #482 の「決定済みの差」)の表の行に対応する。
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
CASES_JSON = HERE / 'cases.json'
CASES_REL = 'native/fixtures/r4e/cases.json'
ROOT = HERE.parents[2]
LEGACY_REL = 'plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py'

SEED = 482
GROUPS = 20
FILES_PER_GROUP = 16

# 既存テスト(scripts/test_export_doc.py)の表。
TABLE = '# Heading\n\n| Name | Value |\n| --- | --- |\n| 日本語 | **bold** |\n'
# 書き込みの失敗のケースで名指しする 3 行の表(見出しの行と 2 行のデータ)。
THREE_ROWS = '# 三行\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n'
ERROR_WORDS = ('#NULL!', '#DIV/0!', '#VALUE!', '#REF!', '#NAME?', '#NUM!', '#N/A')

SCHEMA = {
    'case': {
        'id': '一意の名前(<分類>/<名前>)。比較器は一覧の全部を流し、欠けたら失敗にする',
        'category': 'categories の鍵',
        'title': '何を確かめるか',
        'source_test': '既存テストからの転記のとき、元のテストの名前',
        'setup': '置き場の work の中に、順に作るもの(下の setup_ops)',
        'argv': '引数。文字列(@WORK@ などの印を置き換える)か {"hex": バイト列の16進} か '
                '{"expand": {"format": 文字列, "count": N}}(format.format(index=i) を i=0..N-1 で並べる)',
        'cwd': '現在のディレクトリ。work からの相対の文字列(既定 ".")か {"components": [...]}(子の中で work から 1 つずつ chdir する)',
        'env': '{"set": {...}, "unset": [...]}。base_env に当てる。旧版と Rust の両方に当てる',
        'legacy_env': 'あれば旧版だけ env の代わりにこれを当てる(決定済みの差で、期待を通常の環境の旧版で作るとき)',
        'umask': '8 進の文字列(既定 "022")',
        'stdin': '"devnull"(既定)・"closed"・"directory"',
        'stdout': '"pipe"(既定)・"closed"・"closed-pipe"・"dev-full"・"directory"・"file"・"full-pipe"・'
                  '"prefilled-full-pipe"(先に満杯にしたパイプ。標準エラーの full-pipe は子の WARN で満たす)・'
                  '"nonblock-full-pipe"(書く側を O_NONBLOCK にして満杯にしたパイプ。子が終わるまで読まない)・'
                  '{"pty": {"columns": N, "rows": N}}',
        'stderr': 'stdout と同じ値と "stdout"(fd 1 と同じものを渡す。pty なら同じ端末)',
        'runs': '同じ引数で続けて流す回数(既定 1)。回ごとに比べる',
        'rlimit_fsize': '子の RLIMIT_FSIZE(バイト)。数か {"rule": ...}(rlimit_rules)',
        'sigint_ignored': '真なら SIGINT を無視した状態で起動する',
        'signals': '{"wait": signal_waits の鍵, "signal": 名前, "count": 回数, "interval_ms": 間隔, "after": "drain"}',
        'approved_differences': '当てる決定済みの差の名前(approved_differences の鍵)。always_applied は書かなくても当てる',
        'linux_only': '真なら Mac では流さず、件数つきで「未実施」と記録する',
        'linux_only_reason': 'linux_only の理由',
        'requires': '比較器が満たす前提(strace など)',
        'expect_legacy': '旧版の結果が満たすべきこと。満たさなければケースの作り方の誤りとして失敗にする',
        'expect_legacy_mac': 'Mac で旧版の結果が満たすべきこと(Mac で expect_legacy と違うときだけ書く。Mac ではこちらを使う)',
        'compare': '既定は旧版との比較。"fixed" のときは expected_rust の固定期待と比べる',
        'notes': '補足',
    },
    'setup_ops': {
        'file': '{"op": "file", "path": P, "text": 文字列 | "hex": 16進 | "build": {...}, "mode"?: "0o.."}。P は文字列か {"hex": ...}',
        'dir': '{"op": "dir", "path": P, "mode"?: "0o.."}(親も作る)',
        'symlink': '{"op": "symlink", "path": P, "target": 文字列(@WORK@ を置き換える)}',
        'symlink_chain': '{"op": "symlink_chain", "prefix": 名, "count": N, "target": 名}。<prefix>0 -> target、<prefix>i -> <prefix>(i-1)',
        'hardlink': '{"op": "hardlink", "path": P, "target": work からの相対}',
        'fifo': '{"op": "fifo", "path": P}',
        'chmod': '{"op": "chmod", "path": P, "mode": "0o.."}。比較器は後で権限を戻してから消す',
        'deep_dirs': '{"op": "deep_dirs", "components": [...]}。PATH_MAX を超えるので、親の fd からの相対(mkdirat・openat)で 1 つずつ作る。'
                     '同じく PATH_MAX を超える file の op も、親の fd からたどって openat で書く(比較器の chdir は並列の実行と競るので使わない)',
        'generated': '{"op": "generated", "seed": S, "group": N}。generated_group(S, N) の files を書く',
    },
    'build': {
        'wide_table': '{"name": "wide_table", "columns": N}。N 列の見出しと 1 行のデータ',
        'large_table': '{"name": "large_table", "min_bytes": N}。N バイト以上になるまで行を足した表',
        'long_cell': '{"name": "long_cell", "length": N, "suffix": 文字列}。N 文字の a の後に suffix を置いたセルの表',
        'invalid_utf8_at': '{"name": "invalid_utf8_at", "offset": N}。N バイトの正しい UTF-8 の後に 0xFF を置く',
    },
}

PLACEHOLDERS = {
    '@WORK@': '置き場の work の絶対パス(realpath したもの)。旧版と Rust の置き場は同じ長さの名前にする',
}

LAYOUT = {
    'root': '使い捨ての置き場(realpath したパス。Mac の /var -> /private/var のリンクで深さが変わらないように)',
    'work': '<置き場>/work。setup の置き場で、既定の現在のディレクトリ',
    'home': '<置き場>/home(HOME)',
    'tmp': '<置き場>/tmp(TMPDIR)。実行の後に空であること',
    'stream_file': 'stdout・stderr が "file" のときは <置き場>/stdout・<置き場>/stderr',
}

BASE_ENV = {
    'legacy': {'PYTHONUTF8': '1', 'TZ': 'UTC', 'PATH': '/usr/bin:/bin', 'HOME': '<置き場>/home', 'TMPDIR': '<置き場>/tmp'},
    'rust': {'TZ': 'UTC', 'PATH': '/usr/bin:/bin', 'HOME': '<置き場>/home', 'TMPDIR': '<置き場>/tmp'},
    'command': {
        'legacy': ['<旧版用の python>', '<リポジトリ>/' + LEGACY_REL, '<argv...>'],
        'rust': ['<r4e_contract の絶対パス>', '<argv...>'],
    },
    'notes': 'ほかの環境変数は渡さない。Rust には旧版の環境から PYTHONUTF8 を除いたものを渡す(設計「比べ方」)',
}

CATEGORIES = {
    'existing': '既存テストからの転記',
    'external': '外部の道具',
    'csv': 'CSV の経路の扱い',
    'args': '引数',
    'input': '入力',
    'tables': '表の取り出し',
    'cells': 'セルの型と値',
    'sheets': 'シート名',
    'output': '出力先',
    'generated': '生成したケース',
    'env': '環境',
    'io': '入出力の故障',
    'signals': '信号',
    'write': '書き込みの失敗',
}

APPROVED_DIFFERENCES = {
    'csv-path': {
        'decision': '2026-11-11 23:02',
        'summary': 'openpyxl が無い旧版の CSV。Rust はいつも XLSX。比較は openpyxl がある旧版とだけ行う',
    },
    'xlsx-bytes': {
        'decision': '2026-11-11 23:02',
        'summary': 'ZIP の構造と展開した各部分のバイトで比べる。圧縮のバイト列・圧縮後の大きさ・ZIP の時刻の値・core.xml の 2 つの時刻の値は比べず、形式と実行の前後の範囲を確かめる',
    },
    'encoding-settings': {
        'decision': '22:24(全コマンド)',
        'summary': '文字コードの設定。Rust は UTF-8 と surrogateescape に固定(標準エラーは backslashreplace)。期待は PYTHONUTF8=1 の旧版',
    },
    'fatal-current-thread': {
        'decision': '17:45(全コマンド)',
        'summary': '起動の失敗の Current thread の値は、位置と形式だけを検査する',
    },
    'runtime-paths': {
        'decision': '17:58・21:35・22:26・22:38(全コマンド)',
        'summary': '旧スクリプトは md_tables_to_xlsx.py、標準ライブラリと site-packages は相対のパスへ置き換えて比べる。行番号は CPython 3.10.11 と openpyxl 3.1.5 の値',
    },
    'variable-output': {
        'decision': '22:31(22:35 に全コマンドへ広げた)',
        'summary': 'SIGINT は、信号で終わること・終了値・最後の行・それまでの標準出力を比べる。SIGTERM は全バイト。ZipFile.__del__ の番地は形式だけ',
    },
    'write-partial-failure': {
        'decision': '2026-11-12 00:28 Q1(全コマンド)',
        'summary': 'XLSX の書き込みの途中の失敗は、終了値・空の標準出力・呼出履歴の先頭から wb.save(checked_out) の行まで・各呼出履歴の最後の行・入力の無変更・ほかのファイルを作らないことを比べる。最初の書き出しで失敗する場合は、番地の欄のほかは全部の行を比べる',
    },
    'memory-and-zip64-position': {
        'decision': '2026-11-12 00:28 Q2(全コマンド)',
        'summary': '記憶域の不足と、位置・大きさで決まる ZIP64 の欄は比較の前提の外(ケースを作らない)',
    },
    'uncompressed': {
        'decision': '2026-11-12 00:28 Q3(R4E だけ)',
        'summary': '圧縮しない。空き容量・ulimit -f・割り当てが両者の大きさの間にあるとき、Rust だけが失敗する差を認める',
    },
    'temp-file': {
        'decision': '2026-11-12 00:28 Q4(R4E だけ)',
        'summary': '旧版の失敗が一時ファイルによる場面(3 つの見分け方のどれか)だけ、Rust の期待を同じ入力を通常の条件で流した旧版にする。権限はそのケースの umask で決まる値',
    },
    'unlisted-startup-env': {
        'decision': '2026-11-12 00:28 Q5(全コマンド)',
        'summary': '一覧に無い起動の環境変数の効果は比較の前提の外(ケースを作らない)',
    },
}

ALWAYS_APPLIED = ['xlsx-bytes', 'runtime-paths']

SIGNAL_WAITS = {
    'xlsx-complete': '出力の XLSX の最後に中央ディレクトリの終わり(PK\\x05\\x06)が書かれてから、さらに settle_ms 待つ(標準出力の終了時の書き出しで止まる場面)',
    'pipe-full-stable': '対象のパイプの読み手で FIONREAD の値が 0 より大きく、50 ms ごとに 10 回続けて変わらなくなるまで待つ'
                        '(標準エラーの WARN の書き込みで止まる場面。子が何も書く前の 0 は数えない。Linux・Mac で同じ手順)',
    'stderr-write-blocked': '子の /proc/<pid>/syscall が fd 2 への write で、/proc/<pid>/stat の状態が S で、読み手の FIONREAD が'
                            'パイプの容量(F_GETPIPE_SZ)以上の状態が、50 ms ごとに 10 回続くまで待つ(先に満杯にした標準エラーへ、'
                            'WARN を出さずに拒否の行を書く所で止まる場面。カーネルの内部の関数名 wchan には頼らない。Linux だけ)',
}

RLIMIT_RULES = {
    'half-legacy-xlsx': '先に同じ入力を通常の条件で旧版に流して XLSX の大きさ S を測り、S // 2 にする。旧版のシートの XML の大きさより大きいことを確かめる(旧版の失敗が ZIP の書き込みの中であること)',
    'legacy-xlsx-size': '先に旧版と Rust を通常の条件で流して XLSX の大きさを測る。旧版の XLSX の大きさに 512 バイトを足した値にする'
                        '(core.xml の時刻で、旧版の圧縮後の大きさが実行ごとに数バイト変わるため)。'
                        'シートの XML の大きさ以上で、Rust の XLSX の大きさより小さいことを確かめる',
}

MEASURED = {
    'environment': 'hp-pc(Linux x86_64)・本家 CPython 3.10.11・openpyxl 3.1.5・et_xmlfile 2.0.0・lxml なし・2026-11-12',
    'recursion': [
        '出力先の親の無い階層: -o out/<d を k 個>/book.xlsx で、k=985 は成功(986 階層の作成を含む)、k=986 と 987 は終了 2・'
        '「ERROR 出力を拒否: 出力先または正本を解決できない: maximum recursion depth exceeded while calling a Python object」',
        'ディレクトリの作成(Path.mkdir)の再帰は、同じ階層の physical_directory の再帰より浅いので、上限に先に当たらない(k=985 で作成まで成功、k=986 は事前検査で止まる)。'
        '作成の側の境界は、無い親の階層の 3 つのケースで確かめる',
        'リンクの連なり: -o l<k-1>/book.xlsx(l0 -> real、l<i> -> l<i-1>)で、k=989 は成功、k=990・991・992 は終了 2・同じ文',
    ],
    'signals': [
        '標準出力のパイプが一杯で終了時の書き出しで止まった旧版(既定の溜め方): 1 回目の SIGINT は何も出さずに write を書き直す。'
        '2 回目で「Exception ignored in: <_io.TextIOWrapper name=\'<stdout>\' mode=\'w\' encoding=\'utf-8\'>」と「KeyboardInterrupt: 」を出してまた止まる。'
        '3 回目で SIGINT で終わる。1 回・2 回の後にパイプを読み切ると、それぞれ終了 0・終了 120',
        '同じ場面の PYTHONUNBUFFERED=1: print の行で止まり、1 回目の SIGINT で KeyboardInterrupt の呼出履歴を出して SIGINT で終わる',
        '同じ場面で起動時に SIGINT を無視: 1 回送ってパイプを読み切ると終了 0',
        '同じ場面の SIGTERM: 何も出さずに SIGTERM で終わる',
        '標準エラーのパイプが一杯(2000 個の無い入力の WARN)の既定の溜め方: 1 回・2 回はどちらも呼出履歴の最後の行 KeyboardInterrupt で SIGINT で終わる(2 回目は書き直しで吸収される)。'
        '3 回では呼出履歴が「Traceback (most recent call last):」の後で切れる',
        '同じ場面の PYTHONUNBUFFERED=1: 1 回は呼出履歴の最後の行 KeyboardInterrupt。2 回目で呼出履歴が切れて SIGINT で終わる。3 回目は送る前に終わっている',
        'どの回数でも、同じ条件で 2 回流して標準エラーのバイトが同じだった',
    ],
    'temp_dir': [
        'TMPDIR が無い・書けない置き場のとき、旧版は tempfile の候補の /tmp などへ切り替えて成功する(TMPDIR には何も作らない)',
        'umask 277・出力先の親あり: 一時ファイルを開き直せず PermissionError(例外の文に openpyxl.XXXXXXXX)。XLSX は 0o400 で途中まで残る',
        'umask 477・出力先の親あり: zipfile.py の write で一時ファイルを読めず PermissionError(excel.py の 203 行)。XLSX は 0o200 で途中まで残る',
        'umask 277・出力先の親なし: 作った親が 0o500 なので XLSX を開けず PermissionError(一時ファイルの前。通常の比較)',
    ],
    'startup': [
        'PYTHONINTMAXSTRDIGITS: 0・640・" 640"・"+640"・空は起動する。639・1・abc・-1・"640 "・99999999999999999999 は '
        '「Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.」で終了 1',
        '標準入力がディレクトリ: 「Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue」と Current thread の行で終了 1',
        '標準出力がディレクトリ: 「Fatal Python error: init_sys_streams: can\'t initialize sys standard streams」「IsADirectoryError: [Errno 21] Is a directory: 1」と Current thread の行で終了 1',
        '標準エラーがディレクトリ: 終了 1・標準出力は空(診断は fd 2 へ書けない)',
    ],
    'partial_write': [
        '標準出力を通常のファイルにし RLIMIT_FSIZE=10: 既定の溜め方は先頭の 10 バイトが残り、「Exception ignored in: …<stdout>…」「OSError: [Errno 27] File too large」で終了 120。'
        'PYTHONUNBUFFERED=1 は同じ 10 バイトと print の行の呼出履歴で終了 1',
        '標準エラーを通常のファイルにし RLIMIT_FSIZE=10: 既定の溜め方は WARN の先頭 10 バイトが残り終了 120。PYTHONUNBUFFERED=1 は終了 1。どちらも診断は出せない',
    ],
}


# --- 決まった乱数 ---

class Rng:
    """SHA-256 の数え上げによる乱数。Python の random に依らない。"""

    def __init__(self, *key):
        self.key = ':'.join(map(str, key)).encode('utf-8')
        self.counter = 0

    def u64(self):
        digest = hashlib.sha256(self.key + b':' + str(self.counter).encode('ascii')).digest()
        self.counter += 1
        return int.from_bytes(digest[:8], 'big')

    def below(self, n):
        return self.u64() % n

    def choice(self, items):
        return items[self.below(len(items))]

    def chance(self, numerator, denominator):
        return self.below(denominator) < numerator


# --- 生成したケース ---

STEMS = (
    'Doc', 'doc', 'DOC', '仕様', '表1', '表١', '表１', 'data2', 'Data', 'ΣΑΣ', 'σας',
    'straße', 'STRASSE', 'İstanbul', 'istanbul', 'ǅemal', 'KelvinK', 'kelvink', 'x' * 26, 'X' * 26,
    'a,b', 'v5', 'V', 'notes.v1', 'résumé', 'メモ', 'Sheet', 'sheet', '\u2c2f', '\u2c5f', 'z' * 31, 'Z' * 31,
)
HEADINGS = (
    '', '概要', '一覧', 'Summary', 'SUMMARY', 'summary', 'a', 'a5', 'A', 'a٣', 'İ', 'i', 'ı', 'I',
    'Σ', 'σ', 'ς', '仕様 / 詳細', 'a:b*c?', '[x]', 'long heading ' * 3, '1', '10', 'Ab,1', 'ſ', 'S',
    '\ua7c0', '\ua7c1', '\U00010570', '\U00010597', '\U0001fae0', '日本語の長い見出しで三十一文字を超えるかもしれない名前',
)
WORDS = ('alpha', 'beta', 'gamma', 'x', 'y', 'z', 'Value', 'name', 'data', 'id', '42', '3.14', '-1', '1e3', 'TRUE')
CJK = 'あいうえお漢字表計算日本語試験項目名前値備考ｶﾅ'
SPACES = (' ', '\t', '\u00a0', '\u2003', '\u3000', '\u1680', '\u202f', '\u205f')
LINE_SEPARATORS = ('\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')
SEPARATOR_CELLS = ('---', ':---', '---:', ':---:', ' --- ', '-----', ':-----:')
DECORATED = (
    '**b**', '`c`', '[l](u)', '**`x`**', '**unclosed', '`a', '[x](y', '**a **b** c**', '**  sp  **', '` code `',
    '[**bold link**](http://example.com/a_(b))', '**[l](u)**', '`**not bold**`', '[](empty)', '****',
)
BR = ('a<br>b', 'x<BR/>', '<br />y', 'p<br\u3000>q', 'm<Br  />n', '<br><br>', 'r<br/ >s', 'u<brx>v')
SPECIAL = ('&<>"\'', ']]>', 'a \\| b', 'x\\|', '\\\\|', '١٢٣', '１２３', '\U0001f600', '\U00020bb7',
           'a\u3000b', 'c\u00a0d', 'tab\there')


def gen_cell(rng):
    kind = rng.below(14)
    if kind == 0:
        return ''
    if kind == 1:
        return rng.choice(WORDS) + (' ' + rng.choice(WORDS) if rng.chance(1, 3) else '')
    if kind == 2:
        return ''.join(rng.choice(CJK) for _ in range(1 + rng.below(8)))
    if kind == 3:
        return rng.choice(('=', '==', '=A1+1', '=SUM(1,2)', '= 1', '=A1&"x"<2', '=' + rng.choice(WORDS)))
    if kind == 4:
        word = rng.choice(ERROR_WORDS)
        return rng.choice((word, word, word.lower(), word + '!', ' ' + word + ' '))
    if kind == 5:
        length = 30 + rng.below(170)
        return (rng.choice(('w', 'あ', 'ab ')) * length)[:length]
    if kind == 6:
        return rng.choice(DECORATED)
    if kind == 7:
        return rng.choice(BR)
    if kind == 8:
        return rng.choice(SPECIAL)
    if kind == 9:
        return rng.choice(SPACES) + rng.choice(WORDS) + rng.choice(SPACES)
    if kind == 10:
        return rng.choice(WORDS) + rng.choice(SPACES) + rng.choice(WORDS)
    if kind == 11:
        return str(rng.below(100000))
    if kind == 12:
        return rng.choice(HEADINGS)
    return rng.choice(WORDS)


def gen_row(rng, columns, lead):
    cells = [gen_cell(rng) for _ in range(columns)]
    edge = rng.choice(('|', '|', '|', ''))
    inner = ' | '.join(cells) if rng.chance(3, 4) else '|'.join(cells)
    return lead + '| ' + inner + ' ' + edge


def gen_table(rng):
    columns = 1 + (rng.below(30) if rng.chance(1, 10) else rng.below(8))
    lead = rng.choice(('', '', '', ' ', '  ', '\t', '\u3000'))
    lines = [lead + '| ' + ' | '.join(gen_cell(rng) or 'h' for _ in range(columns)) + ' |']
    separator = [rng.choice(SEPARATOR_CELLS) for _ in range(columns)]
    if rng.chance(1, 8):
        lines.append(' | '.join(separator))
    else:
        lines.append(lead + '|' + '|'.join(separator) + '|')
    for _ in range(rng.below(9)):
        width = max(1, columns + rng.below(5) - 2)
        if rng.chance(1, 20):
            lines.append(lead + '|' + '|'.join('---' for _ in range(width)) + '|')
        else:
            lines.append(gen_row(rng, width, lead))
    return lines


def gen_heading(rng):
    level = 1 + rng.below(7)
    space = rng.choice((' ', ' ', ' ', '\t', '\u3000', '  '))
    text = rng.choice(HEADINGS)
    if rng.chance(1, 6):
        text += ' ' + '#' * (1 + rng.below(3))
    return '#' * level + space + text


def gen_document(rng):
    lines = []
    if rng.chance(1, 12):
        lines.append('\ufeff' + rng.choice(('# 先頭', '| bom | table |')))
    in_fence = None
    for _ in range(2 + rng.below(9)):
        kind = rng.below(10)
        if kind in (0, 1):
            lines.append(gen_heading(rng))
        elif kind in (2, 3, 4, 5):
            lines.extend(gen_table(rng))
        elif kind == 6:
            fence = rng.choice(('```', '```python', '~~~', '  ```', '````'))
            lines.append(fence)
            if rng.chance(1, 2):
                lines.extend(gen_table(rng))
            if rng.chance(3, 4):
                lines.append(fence.strip()[:3] if fence.strip().startswith('`') else '~~~')
            else:
                in_fence = fence
        elif kind == 7:
            lines.append(rng.choice(WORDS) + ' | ' + rng.choice(WORDS))
            lines.append('---|---')
        elif kind == 8:
            lines.append('')
        else:
            lines.append(' '.join(rng.choice(WORDS) for _ in range(1 + rng.below(6))))
    if in_fence and rng.chance(1, 2):
        lines.extend(gen_table(rng))
    newline_kind = rng.below(6)
    out = []
    for index, line in enumerate(lines):
        out.append(line)
        if index == len(lines) - 1 and rng.chance(1, 5):
            break
        if newline_kind == 0:
            out.append('\r\n')
        elif newline_kind == 1:
            out.append('\r')
        elif newline_kind == 2:
            out.append(rng.choice(('\n', '\r\n', '\r')))
        elif newline_kind == 3:
            out.append(rng.choice(('\n', '\n', '\n') + LINE_SEPARATORS))
        else:
            out.append('\n')
    return ''.join(out)


def generated_group(seed, group):
    """生成したケースの 1 組。{'files': [(相対パス, バイト)], 'argv': [...], 'output': 相対パス}。"""
    rng = Rng('r4e-generated', seed, group)
    files = []
    argv = []
    for index in range(FILES_PER_GROUP):
        stem = rng.choice(STEMS)
        path = f'gen/g{group:03d}/f{index:02d}/{stem}.md'
        files.append((path, gen_document(rng).encode('utf-8')))
        argv.append(path)
        if rng.chance(1, 16):
            argv.append(path)
        if rng.chance(1, 24):
            argv.append(f'gen/g{group:03d}/missing-{index:02d}.md')
    output = f'out/g{group:03d}.xlsx'
    return {'files': files, 'argv': argv + ['-o', output], 'output': output}


def generated_digest(seed, groups):
    digest = hashlib.sha256(b'r4e-generated-v1\0')
    inputs = 0
    for group in range(groups):
        data = generated_group(seed, group)
        for path, body in data['files']:
            inputs += 1
            for field in (path.encode('utf-8'), body):
                digest.update(len(field).to_bytes(8, 'big'))
                digest.update(field)
        for arg in data['argv']:
            field = arg.encode('utf-8')
            digest.update(len(field).to_bytes(8, 'big'))
            digest.update(field)
    return digest.hexdigest(), inputs


# --- 大きな入力 ---

def column_name(index):
    name = ''
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def build_content(spec):
    name = spec['name']
    if name == 'wide_table':
        columns = spec['columns']
        header = '| ' + ' | '.join(f'h{column_name(i)}' for i in range(1, columns + 1)) + ' |\n'
        separator = '|' + '|'.join('---' for _ in range(columns)) + '|\n'
        row = '| ' + ' | '.join(str(i) for i in range(1, columns + 1)) + ' |\n'
        return ('# 広い表\n\n' + header + separator + row).encode('utf-8')
    if name == 'large_table':
        parts = ['# 大きな表\n\n| 番号 | 値 | 備考 |\n|---|---|---|\n']
        size = len(parts[0].encode('utf-8'))
        index = 0
        while size < spec['min_bytes']:
            line = f'| {index} | 値{index:07d} | {"あいうえお" * (1 + index % 5)} |\n'
            parts.append(line)
            size += len(line.encode('utf-8'))
            index += 1
        return ''.join(parts).encode('utf-8')
    if name == 'long_cell':
        cell = 'a' * spec['length'] + spec.get('suffix', '')
        return ('# 長い値\n\n| 値 |\n|---|\n| ' + cell + ' |\n').encode('utf-8')
    if name == 'invalid_utf8_at':
        offset = spec['offset']
        head = ('# 誤り\n\n| a |\n|---|\n').encode('utf-8')
        filler = b'x' * (offset - len(head))
        return head + filler + b'\xff\n'
    raise ValueError(f'未知の build: {name}')


# --- 必須のケース ---

CASES = []


def add(case_id, category, title, setup, argv, **extra):
    if category not in CATEGORIES:
        raise ValueError(f'未知の分類: {category}')
    for name in extra.get('approved_differences', []):
        if name not in APPROVED_DIFFERENCES:
            raise ValueError(f'未知の差: {name}')
    case = {
        'id': case_id,
        'category': category,
        'title': title,
        'setup': setup,
        'argv': argv,
        'env': extra.pop('env', {'set': {}, 'unset': []}),
        'approved_differences': extra.pop('approved_differences', []),
        'linux_only': extra.pop('linux_only', False),
    }
    case.update(extra)
    CASES.append(case)
    return case


def f(path, text, **extra):
    return {'op': 'file', 'path': path, 'text': text, **extra}


def fh(path, data, **extra):
    return {'op': 'file', 'path': path, 'hex': data.hex(), **extra}


def d(path, **extra):
    return {'op': 'dir', 'path': path, **extra}


def link(path, target):
    return {'op': 'symlink', 'path': path, 'target': target}


def env_set(**values):
    return {'set': values, 'unset': []}


def doc(*more):
    return [f('doc/input.md', TABLE), *more]


OUT = ['-o', 'out/book.xlsx']


def existing_cases():
    add('existing/normal-initial-and-overwrite', 'existing', '通常の出力と、同じ出力先への上書き(2 回流す)',
        doc(), ['doc/input.md', '-o', 'space xlsx/book.xlsx'], runs=2,
        source_test='test_normal_cli_initial_and_overwrite_both_formats', expect_legacy={'exit': 0})
    add('existing/source-directory-rejected', 'existing', '正本(入力の親)の中への出力は、親を作る前に拒否する',
        doc(), ['doc/input.md', '-o', 'doc/new/book.xlsx'],
        source_test='test_source_directory_rejected_before_mkdir', expect_legacy={'exit': 2})
    add('existing/direct-input-other-root', 'existing', 'ほかの正本を指定しても、入力そのものへの出力は拒否する',
        doc(d('other')), ['doc/input.md', '-o', 'doc/input.md', '--source-root', 'other'],
        source_test='test_direct_input_rejected_even_with_explicit_other_root', expect_legacy={'exit': 2})
    roots = doc(f('second doc/other.md', TABLE), f('scratch/copy.md', TABLE))
    common = ['scratch/copy.md', 'second doc/other.md']
    for name, output, code in (('first-root', 'doc/new/book.xlsx', 2), ('second-root', 'second doc/new/book.xlsx', 2),
                               ('scratch-output', 'export/book.xlsx', 0)):
        add(f'existing/multiple-roots-{name}', 'existing', f'複数の正本({name})', roots,
            common + ['-o', output, '--source-root', 'doc', '--source-root', 'second doc'],
            source_test='test_scratch_and_multiple_source_roots', expect_legacy={'exit': code})
    add('existing/parent-symlink-safe', 'existing', '親がリンクで、リンク先が正本の外なら出力する(作られた実体の場所も比べる)',
        doc(d('space output'), link('safe', '@WORK@/space output')), ['doc/input.md', '-o', 'safe/new/book.xlsx'],
        source_test='test_parent_symlink_safe_and_source_overlap', expect_legacy={'exit': 0})
    add('existing/parent-symlink-into-source', 'existing', '親がリンクで、リンク先が正本なら拒否する',
        doc(link('bad', '@WORK@/doc')), ['doc/input.md', '-o', 'bad/new/book.xlsx'],
        source_test='test_parent_symlink_safe_and_source_overlap', expect_legacy={'exit': 2})
    for original, original_path in (('input', 'doc/input.md'), ('unselected', 'doc/unselected.md')):
        for kind in ('symlink', 'hardlink', 'dangling'):
            folder = f'{original}-{kind}-False'
            setup = doc(f('doc/unselected.md', 'private original'), f('scratch/copy.md', TABLE), d(folder))
            if kind == 'hardlink':
                setup.append({'op': 'hardlink', 'path': f'{folder}/book.xlsx', 'target': original_path})
            else:
                setup.append(link(f'{folder}/book.xlsx', f'@WORK@/{original_path}' if kind == 'symlink' else '@WORK@/missing'))
            add(f'existing/final-{kind}-{original}', 'existing', f'出力先の名前そのものが {kind}({original})なら拒否する',
                setup, ['scratch/copy.md', '-o', f'{folder}/book.xlsx', '--source-root', 'doc'],
                source_test='test_final_symlink_and_hardlink_rejected_in_both_formats', expect_legacy={'exit': 2})
    for name, op in (('broken', link('broken', '@WORK@/missing')), ('cycle', link('cycle', '@WORK@/cycle')),
                     ('regular', f('regular', 'keep parent'))):
        add(f'existing/{name}-parent', 'existing', f'出力先の親が {name} なら拒否する', doc(op),
            ['doc/input.md', '-o', f'{name}/child/book.xlsx'],
            source_test='test_broken_cyclic_and_file_parents', expect_legacy={'exit': 2})
    for name, op in (('directory', d('directory')), ('fifo', {'op': 'fifo', 'path': 'fifo'}),
                     ('symlink', link('symlink', '@WORK@/doc/input.md'))):
        add(f'existing/output-is-{name}', 'existing', f'出力先が {name} なら拒否する', doc(op),
            ['doc/input.md', '-o', name], source_test='test_non_regular_xlsx_and_direct_xlsx_symlink',
            expect_legacy={'exit': 2})
    for name, root in (('missing', 'missing'), ('file', 'doc/input.md')):
        add(f'existing/invalid-source-root-{name}', 'existing', f'正本が {name} なら拒否する', doc(),
            ['doc/input.md', '-o', 'export/book.xlsx', '--source-root', root],
            source_test='test_invalid_source_root_rejected', expect_legacy={'exit': 2})
    alias = doc(d('selected'), link('selected/alias.md', '@WORK@/doc/input.md'))
    for name, output in (('selected-root', 'selected/new/book.xlsx'), ('real-root', 'doc/new/book.xlsx')):
        add(f'existing/symlink-input-{name}', 'existing', f'入力がリンクなら、選んだ親と実体の親の両方を守る({name})',
            alias, ['selected/alias.md', '-o', output],
            source_test='test_symlink_input_protects_both_selected_and_real_parents', expect_legacy={'exit': 2})
    empty = doc(f('doc/empty.md', '# no table'))
    add('existing/missing-only', 'existing', '無い入力だけなら終了 2', empty, ['missing', '-o', 'False/book.xlsx'],
        source_test='test_missing_and_no_tables_behavior', expect_legacy={'exit': 2})
    add('existing/no-tables', 'existing', '表が無ければ終了 1', empty, ['doc/empty.md', '-o', 'False/book.xlsx'],
        source_test='test_missing_and_no_tables_behavior', expect_legacy={'exit': 1})
    add('existing/missing-and-table', 'existing', '無い入力があっても表があれば出力する', empty,
        ['missing', 'doc/input.md', '-o', 'False/book.xlsx'],
        source_test='test_missing_and_no_tables_behavior', expect_legacy={'exit': 0})


NOT_TRANSCRIBED = {
    'test_csv_final_symlink_never_changes_source': 'CSV の経路(write_csv_fallback)だけの場面。Rust に CSV の経路は無い(決定 23:02)',
    'test_all_csv_outputs_preflight_before_overwrite': 'CSV の経路(write_csv_fallback)だけの場面。Rust に CSV の経路は無い(決定 23:02)',
}


def external_cases():
    add('external/empty-path', 'external', 'PATH を空の置き場にして流す。Linux では Rust を strace -f -e trace=execve で流し、最初の 1 行を除いた execve の行が 0',
        doc(d('emptybin')), ['doc/input.md'] + OUT, env=env_set(PATH='@WORK@/emptybin'),
        requires=['strace(Linux。無い・動かないなら失敗。Mac は件数つきで未実施)'], expect_legacy={'exit': 0})
    add('external/empty-path-error', 'external', 'PATH を空にして、呼出履歴になる入力でも外部の道具を起動しない',
        [d('emptybin'), fh('doc/bad.md', b'| a |\n|---|\n| \xff |\n')], ['doc/bad.md'] + OUT,
        env=env_set(PATH='@WORK@/emptybin'), requires=['strace(Linux)'], expect_legacy={'exit': 1})


def csv_cases():
    add('csv/no-python-in-path', 'csv', 'Rust を Python の無い PATH で流し、openpyxl のある旧版の XLSX と比べる。FALLBACK を出さず、out/book/ を作らない',
        doc(d('emptybin')), ['doc/input.md'] + OUT, env=env_set(PATH='@WORK@/emptybin'),
        legacy_env={'set': {}, 'unset': []}, approved_differences=['csv-path'], expect_legacy={'exit': 0},
        notes='旧版は base_env の PATH(openpyxl の venv の python を絶対パスで起動する)')


def args_cases():
    base = doc(f('doc/other.md', TABLE), d('roots/a'), d('roots/b'), f('neg/-1', TABLE))
    simple = [
        ('no-arguments', [], '引数なし(必須の欠落 2 つ)', 2),
        ('missing-output', ['doc/input.md'], '-o の欠落', 2),
        ('missing-inputs', ['-o', 'out/book.xlsx'], '入力の欠落', 2),
        ('unknown-long', ['doc/input.md', '-o', 'out/book.xlsx', '--bogus'], '未知の長い選択肢', 2),
        ('unknown-short', ['doc/input.md', '-o', 'out/book.xlsx', '-x'], '未知の短い選択肢', 2),
        ('unknown-and-missing', ['--bogus'], '未知の選択肢と必須の欠落', 2),
        ('output-missing-value', ['doc/input.md', '-o'], '-o の値の欠落', 2),
        ('output-long-missing-value', ['doc/input.md', '--output'], '--output の値の欠落', 2),
        ('source-root-missing-value', ['doc/input.md', '-o', 'out/book.xlsx', '--source-root'], '--source-root の値の欠落', 2),
        ('output-value-is-option', ['doc/input.md', '-o', '--source-root', 'roots/a'], '-o の値の位置に選択肢', 2),
        ('abbrev-o', ['doc/input.md', '--o', 'out/book.xlsx'], '短縮 --o', 0),
        ('abbrev-out', ['--out', 'out/book.xlsx', 'doc/input.md'], '短縮 --out', 0),
        ('abbrev-s', ['doc/input.md', '-o', 'out/book.xlsx', '--s', 'roots/a'], '短縮 --s', 0),
        ('abbrev-equals', ['doc/input.md', '--outp=out/book.xlsx'], '短縮と = の形', 0),
        ('abbrev-h', ['--h'], '短縮 --h', 0),
        ('long-equals', ['doc/input.md', '--output=out/book.xlsx'], '--output=x', 0),
        ('short-attached', ['doc/input.md', '-oout/book.xlsx'], '-ox', 0),
        ('short-equals', ['doc/input.md', '-o=out/book.xlsx'], '-o=x', 0),
        ('single-dash-long', ['-output', 'out/book.xlsx'], '-output(-o と utput に分かれ、out/book.xlsx は入力になる)', 2),
        ('double-dash', ['-o', 'out/book.xlsx', '--', 'doc/input.md'], '--', 0),
        ('double-dash-option-like', ['-o', 'out/book.xlsx', '--', '-o'], '-- の後の -o は入力', 2),
        ('interleaved', ['doc/input.md', '-o', 'out/book.xlsx', 'doc/other.md'], '位置引数と選択肢の混在', 2),
        ('options-first', ['-o', 'out/book.xlsx', 'doc/input.md', 'doc/other.md'], '選択肢が先', 0),
        ('negative-number-input', ['-1', '-o', '../out/book.xlsx'], '-1 の形の入力(neg/-1 があり、neg で起動する)', 0),
        ('negative-number-value', ['doc/input.md', '-o', '-1.xlsx'], '-1 で始まる -o の値(負の数の形でないので値にならない)', 2),
        ('negative-number-pure-value', ['doc/input.md', '-o', '-1'], '負の数の形の -o の値', 0),
        ('dash-value', ['doc/input.md', '-o', '-'], '- を -o の値にする', 0),
        ('duplicate-output', ['doc/input.md', '-o', 'first.xlsx', '-o', 'out/book.xlsx'], '-o の重複(最後が勝つ)', 0),
        ('duplicate-source-root', ['doc/input.md', '-o', 'out/book.xlsx', '--source-root', 'roots/a', '--source-root', 'roots/b'],
         '--source-root の繰り返し', 0),
        ('help-short', ['-h'], '-h', 0),
        ('help-long', ['--help'], '--help', 0),
        ('help-with-other-args', ['doc/input.md', '-o', 'out/book.xlsx', '-h'], 'ほかの引数と -h', 0),
        ('help-after-unknown', ['--bogus', '-h'], '未知の選択肢の後の -h', 0),
        ('empty-input-string', ['', '-o', 'out/book.xlsx'], '空の入力の引数', 2),
    ]
    for name, argv, title, code in simple:
        extra = {'expect_legacy': {'exit': code}} if code is not None else {}
        if name == 'negative-number-input':
            extra['cwd'] = 'neg'
        add(f'args/{name}', 'args', title, base, argv, **extra)
    for width in range(1, 201):
        add(f'args/help-columns-{width:03d}', 'args', f'--help を COLUMNS={width} で折り返す', doc(), ['--help'],
            env=env_set(COLUMNS=str(width)), expect_legacy={'exit': 0})
        add(f'args/usage-error-columns-{width:03d}', 'args', f'誤りの使い方の行を COLUMNS={width} で折り返す', doc(), [],
            env=env_set(COLUMNS=str(width)), expect_legacy={'exit': 2})
    forms = [
        ('unset', None), ('plus', '+80'), ('underscore', '1_0'), ('spaces', ' 50 '), ('tab-newline', '\t50\n'),
        ('zero', '0'), ('negative', '-5'), ('arabic-indic-digits', '٥٠'), ('fullwidth-digits', '５０'),
        ('invalid', 'abc'), ('empty', ''), ('float', '80.0'), ('hex', '0x50'), ('huge', '99999999999999999999999'),
        ('leading-zero', '0050'), ('underscore-edge', '_50'),
    ]
    for name, value in forms:
        env = {'set': {}, 'unset': ['COLUMNS']} if value is None else env_set(COLUMNS=value)
        add(f'args/columns-{name}', 'args', f'COLUMNS の形: {name}', doc(), ['--help'], env=env, expect_legacy={'exit': 0})
    add('args/help-pty', 'args', '標準出力が疑似端末(57 列)で COLUMNS が無い --help', doc(), ['--help'],
        env={'set': {}, 'unset': ['COLUMNS']}, stdout={'pty': {'columns': 57, 'rows': 24}}, expect_legacy={'exit': 0})
    add('args/usage-error-pty', 'args', '標準出力が疑似端末(57 列)のときの誤りの使い方の行', doc(), [],
        env={'set': {}, 'unset': ['COLUMNS']}, stdout={'pty': {'columns': 57, 'rows': 24}}, expect_legacy={'exit': 2})
    add('args/help-pty-columns-env', 'args', '疑似端末(57 列)でも COLUMNS=30 が先', doc(), ['--help'],
        env=env_set(COLUMNS='30'), stdout={'pty': {'columns': 57, 'rows': 24}}, expect_legacy={'exit': 0})
    add('args/help-pty-zero-size', 'args', '疑似端末の大きさが 0 列', doc(), ['--help'],
        env={'set': {}, 'unset': ['COLUMNS']}, stdout={'pty': {'columns': 0, 'rows': 0}}, expect_legacy={'exit': 0})
    add('args/help-stderr-pty-only', 'args', '標準エラーだけが疑似端末(幅は標準出力から決まる)', doc(), [],
        env={'set': {}, 'unset': ['COLUMNS']}, stderr={'pty': {'columns': 40, 'rows': 24}}, expect_legacy={'exit': 2})


def input_cases():
    add('input/missing', 'input', '無い入力', doc(), ['doc/none.md'] + OUT, expect_legacy={'exit': 2})
    add('input/directory', 'input', 'ディレクトリの入力', doc(), ['doc'] + OUT, expect_legacy={'exit': 2})
    add('input/fifo', 'input', 'FIFO の入力(stat で通常ファイルでない)', doc({'op': 'fifo', 'path': 'doc/pipe.md'}),
        ['doc/pipe.md'] + OUT, expect_legacy={'exit': 2})
    add('input/unreadable-file', 'input', '読めない入力(0000)は呼出履歴', doc(f('doc/locked.md', TABLE, mode='0o000')),
        ['doc/locked.md'] + OUT, expect_legacy={'exit': 1}, requires=['root でない利用者'])
    add('input/unreadable-parent', 'input', '親が読めない(0000)入力は stat の EACCES で呼出履歴',
        doc(f('locked/a.md', TABLE), {'op': 'chmod', 'path': 'locked', 'mode': '0o000'}),
        ['locked/a.md'] + OUT, expect_legacy={'exit': 1}, requires=['root でない利用者'])
    add('input/symlink-absolute', 'input', '絶対パスのリンクの入力', doc(link('links/link.md', '@WORK@/doc/input.md')),
        ['links/link.md'] + OUT, expect_legacy={'exit': 0})
    add('input/symlink-relative', 'input', '相対パスのリンクの入力', doc(link('links/link.md', '../doc/input.md')),
        ['links/link.md'] + OUT, expect_legacy={'exit': 0})
    add('input/broken-symlink', 'input', '切れたリンクの入力', doc(link('links/broken.md', 'nowhere.md')),
        ['links/broken.md'] + OUT, expect_legacy={'exit': 2})
    add('input/symlink-loop', 'input', '環のリンクの入力(ELOOP は無いと扱う)', doc(link('links/loop.md', 'loop.md')),
        ['links/loop.md'] + OUT, expect_legacy={'exit': 2})
    add('input/not-a-directory', 'input', '親がファイルの入力(ENOTDIR は無いと扱う)', doc(),
        ['doc/input.md/x.md'] + OUT, expect_legacy={'exit': 2})
    add('input/name-too-long', 'input', '長すぎる名前の入力(ENAMETOOLONG は呼出履歴)', doc(),
        ['doc/' + 'n' * 300 + '.md'] + OUT, expect_legacy={'exit': 1})
    add('input/missing-then-table', 'input', '無い入力の WARN は全部を読んだ後に出す', doc(),
        ['doc/none1.md', 'doc/input.md', 'doc/none2.md'] + OUT, expect_legacy={'exit': 0})
    utf8 = [
        ('invalid-start-byte-at-0', b'\xff' + TABLE.encode()),
        ('invalid-start-byte-at-end', TABLE.encode() + b'\xff'),
        ('invalid-continuation', b'| a |\n|---|\n| \xe3\x81A |\n'),
        ('unexpected-end', TABLE.encode() + b'\xe3\x81'),
        ('unexpected-end-4byte', TABLE.encode() + b'\xf0\x9f\x98'),
        ('surrogate', b'| a |\n|---|\n| \xed\xa0\x80 |\n'),
        ('overlong', b'| a |\n|---|\n| \xc0\xaf |\n'),
        ('too-large', b'| a |\n|---|\n| \xf4\x90\x80\x80 |\n'),
        ('lone-continuation', b'| a |\n|---|\n| \x80 |\n'),
    ]
    for name, data in utf8:
        add(f'input/utf8-{name}', 'input', f'UTF-8 の誤り({name})は位置と理由つきの呼出履歴', [fh('doc/bad.md', data)],
            ['doc/bad.md'] + OUT, expect_legacy={'exit': 1})
    for offset in (8191, 8192, 8193, 70000):
        add(f'input/utf8-invalid-at-{offset}', 'input', f'{offset} バイト目の UTF-8 の誤り(位置は先頭からのバイト数)',
            [{'op': 'file', 'path': 'doc/bad.md', 'build': {'name': 'invalid_utf8_at', 'offset': offset}}],
            ['doc/bad.md'] + OUT, expect_legacy={'exit': 1})
    add('input/utf8-error-after-missing', 'input', '後の入力で呼出履歴になると、先の無い入力の WARN は出ない',
        [fh('doc/bad.md', b'\xff')], ['doc/none.md', 'doc/bad.md'] + OUT, expect_legacy={'exit': 1})
    add('input/bom-before-table', 'input', 'BOM の直後の表の行は表にならない(BOM は残る)',
        [fh('doc/bom.md', b'\xef\xbb\xbf| a | b |\n|---|---|\n| 1 | 2 |\n')], ['doc/bom.md'] + OUT, expect_legacy={'exit': 1})
    add('input/bom-before-heading', 'input', 'BOM の直後の見出しは見出しにならない',
        [fh('doc/bom.md', b'\xef\xbb\xbf# H\n\n| a | b |\n|---|---|\n| 1 | 2 |\n')], ['doc/bom.md'] + OUT, expect_legacy={'exit': 0})
    add('input/crlf', 'input', '\\r\\n の改行', [fh('doc/crlf.md', TABLE.replace('\n', '\r\n').encode())],
        ['doc/crlf.md'] + OUT, expect_legacy={'exit': 0})
    add('input/cr-only', 'input', '\\r だけの改行', [fh('doc/cr.md', TABLE.replace('\n', '\r').encode())],
        ['doc/cr.md'] + OUT, expect_legacy={'exit': 0})
    add('input/mixed-newlines', 'input', '\\n・\\r\\n・\\r の混在',
        [fh('doc/mixed.md', b'# H\r\n\r| a | b |\n|---|---|\r\n| 1 | 2 |\r| 3 | 4 |\n')], ['doc/mixed.md'] + OUT,
        expect_legacy={'exit': 0})
    rows = ['| a | b |', '|---|---|'] + [f'| {index} | {ord(sep):x} |' for index, sep in enumerate(LINE_SEPARATORS)]
    text = '# 改行\n' + rows[0] + '\n' + rows[1] + '\n' + ''.join(row + sep for row, sep in zip(rows[2:], LINE_SEPARATORS))
    add('input/unicode-line-breaks', 'input', 'str.splitlines の改行(\\v・\\f・\\x1c〜\\x1e・\\x85・\\u2028・\\u2029)',
        [f('doc/breaks.md', text)], ['doc/breaks.md'] + OUT, expect_legacy={'exit': 0})
    spaced = '#\u3000見出し\u3000\n\n\u3000| a | b |\n\u00a0|---|---|\n\x1f| \u2003x\u2003 | \u205fy\u205f |\n\t| 1 | 2 |\n'
    add('input/unicode-spaces', 'input', 'Unicode の空白(\\u3000・\\xa0・\\x1f など)の字下げと前後', [f('doc/spaces.md', spaced)],
        ['doc/spaces.md'] + OUT, expect_legacy={'exit': 0})
    add('input/empty-file', 'input', '空のファイル', [f('doc/empty.md', '')], ['doc/empty.md'] + OUT, expect_legacy={'exit': 1})
    add('input/whitespace-only', 'input', '空白だけのファイル', [f('doc/ws.md', ' \n\t\n\u3000\n')], ['doc/ws.md'] + OUT,
        expect_legacy={'exit': 1})
    add('input/no-trailing-newline', 'input', '最後の改行が無い', [f('doc/eof.md', TABLE.rstrip('\n'))], ['doc/eof.md'] + OUT,
        expect_legacy={'exit': 0})
    add('input/one-mib', 'input', '1 MiB の入力',
        [{'op': 'file', 'path': 'doc/large.md', 'build': {'name': 'large_table', 'min_bytes': 1048576}}],
        ['doc/large.md'] + OUT, expect_legacy={'exit': 0})


def tables_cases():
    def one(name, title, text, code=0):
        add(f'tables/{name}', 'tables', title, [f('doc/t.md', text)], ['doc/t.md'] + OUT, expect_legacy={'exit': code})

    one('fence-open-close', '``` の中の表は取らず、閉じた後の表は取る',
        '```\n| in | fence |\n|---|---|\n| 1 | 2 |\n```\n| out | side |\n|---|---|\n| 3 | 4 |\n')
    one('fence-unclosed', '閉じない ``` の後の表は取らない',
        '| a |\n|---|\n| 1 |\n```\n| b |\n|---|\n| 2 |\n')
    one('fence-indented', '行頭の空白(Unicode を含む)の後の ``` もフェンス',
        '\u3000```\n| a |\n|---|\n| 1 |\n  ```\n| b |\n|---|\n| 2 |\n')
    one('fence-info-and-four', '```python と ```` もフェンス(数は数えない)',
        '```python\n| a |\n|---|\n````\n| b |\n|---|\n| 2 |\n')
    one('tilde-fence', '~~~ はフェンスにしない(中の表も取る)', '~~~\n| a |\n|---|\n| 1 |\n~~~\n')
    one('heading-levels', '見出しの段 1〜6 と、7 個の # は見出しにしない',
        ''.join(f'{"#" * level} 段{level}\n| h{level} |\n|---|\n| {level} |\n' for level in range(1, 8)))
    one('heading-empty-forms', '「# 」は見出しを空に戻す。「#」だけは見出しにしない。「#\\t」「#\\u3000x」',
        '# 最初\n| a |\n|---|\n| 1 |\n# \n| b |\n|---|\n| 2 |\n# 次\n#\n| c |\n|---|\n| 3 |\n#\t\n| d |\n|---|\n| 4 |\n'
        '#\u3000全角\n| e |\n|---|\n| 5 |\n')
    one('heading-trailing-hashes', '後ろの # も見出しの文字', '## a ##\n| a |\n|---|\n| 1 |\n')
    one('heading-indented', '行頭に空白のある # は見出しにしない', '# 本当\n  # 字下げ\n| a |\n|---|\n| 1 |\n')
    one('heading-no-space', '# の後に空白が無いものは見出しにしない', '# 本当\n#見出し\n| a |\n|---|\n| 1 |\n')
    one('pipe-not-leading', '| で始まらない a | b の形は表にしない', 'a | b\n---|---\n1 | 2\n', 1)
    one('no-data-rows', 'データの行が無い表', '| a | b |\n|---|---|\n')
    one('separator-forms', '区切り行の形(:---:・空のセル・| の無い ---・-- は区切りでない・:-: は区切りでない)',
        '| a | b |\n|:---:|---:|\n| 1 | 2 |\n\n| c | d |\n| --- | |\n| 3 | 4 |\n\n| e |\n---\n| 5 |\n\n'
        '| f | g |\n|--|--|\n| 6 | 7 |\n\n| h |\n|:-:|\n| 8 |\n\n| i |\n| :----- |\n| 9 |\n\n| j | k |\n --- | --- \n| 10 | 11 |\n')
    one('data-row-separator-like', '区切り行の後の |---| はデータの行', '| a | b |\n|---|---|\n|---|---|\n| 1 | 2 |\n')
    one('escaped-pipes', '\\| と \\\\| の扱い',
        '| a | b |\n|---|---|\n| x \\| y | z |\n| \\\\| w | v\\| |\n| \\| | end\\|\n')
    one('decoration-nested-unclosed', '装飾の入れ子と未閉じ',
        '| v |\n|---|\n' + ''.join(f'| {value} |\n' for value in DECORATED))
    one('br-forms', '<br> の形(大文字小文字・空白・/)',
        '| v |\n|---|\n' + ''.join(f'| {value} |\n' for value in BR + ('<br\t/>', '<br//>', 'a<BR>b<bR />c')))
    one('column-count-varies', '行ごとに列数が違う', '| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 | 5 |\n| |\n')
    for columns, code in ((27, 0), (703, 0), (16385, 0), (18278, 0), (18279, 1)):
        add(f'tables/columns-{columns}', 'tables', f'{columns} 列の表' + ('(列幅の計算で ValueError)' if code else ''),
            [{'op': 'file', 'path': 'doc/wide.md', 'build': {'name': 'wide_table', 'columns': columns}}],
            ['doc/wide.md'] + OUT, expect_legacy={'exit': code})
    one('table-at-eof-no-newline', '最後の行に改行の無い表', '| a |\n|---|\n| 1 |')
    one('indented-table', '空白とタブで字下げした表', '  | a |\n\t|---|\n \t| 1 |\n')
    one('header-on-last-line', '最後の行の見出しの行(区切り行が無い)', '# H\n| a |', 1)
    one('consecutive-tables', '空行の無い 2 つ目の表は 1 つ目のデータの行になる', '| a |\n|---|\n| 1 |\n| b |\n|---|\n| 2 |\n')
    one('heading-inside-table', '表の途中の見出しで表が終わる', '| a |\n|---|\n| 1 |\n# 次\n| 2 |\n|---|\n| 3 |\n')
    one('fence-inside-table', '表の途中の ``` で表が終わり、フェンスが始まる', '| a |\n|---|\n| 1 |\n```\n| b |\n|---|\n| 2 |\n')
    one('separator-without-header-pipe', '見出しの行が | で始まり、区切り行が | で始まらない', '| a | b |\n:--- | ---:\n| 1 | 2 |\n')


def cells_cases():
    def one(name, title, rows, code=0, header='| v |\n|---|\n'):
        add(f'cells/{name}', 'cells', title, [f('doc/c.md', '# 値\n\n' + header + ''.join(f'| {row} |\n' for row in rows))],
            ['doc/c.md'] + OUT, expect_legacy={'exit': code})

    one('formula-forms', '= で始まる値(1 文字は文字列、2 文字以上は数式)', ['=', '==', '=A1+1', '= 1', '=1<2&"x"', ' =B2 '])
    one('error-words', '誤りの 7 語と、近い形', list(ERROR_WORDS) + ['#n/a', '#N/A!', ' #REF! ', '#DIV/0'])
    one('empty-cells', '空のセルと空白だけのセル', ['', ' ', '\u3000', '\t'])
    one('numbers-stay-strings', '数・真偽の形の値も文字列', ['1', '1.5', '1e3', '-0', 'TRUE', 'false', '0x10', '00012'])
    one('xml-special', 'XML のエスケープの要る文字', ['&', '<tag>', '"quoted"', "'single'", ']]>', 'a&amp;b', '&#65;'])
    one('astral-and-emoji', '基本面の外の文字(幅は 2 と数える)', ['\U0001f600', '\U00020bb7', 'a\U0001f600b', '\U0001fae0'])
    one('newline-only', '<br> だけの値と、前後の改行', ['<br>', '<br>a', 'a<br>', '<br><br>'])
    one('tab-inside', 'セルの中のタブ', ['a\tb', 'x\t\ty'])
    one('edge-spaces-from-decoration', '装飾を外した後に前後の空白が残る', ['**  x  **', '` y `', '[ z ](u)', '**\u3000w\u3000**'])
    one('long-first-line-width', '列幅は最初の行の文字数で 8〜60', ['a' * 58, 'b' * 59, 'あ' * 29, 'x<br>' + 'y' * 100])
    one('formula-after-decoration', '装飾を外した後の = で数式になる', ['**=1+1**', '`=A1`', '[=B1](u)'])
    one('error-after-decoration', '装飾を外した後の誤りの語', ['`#N/A`', '**#REF!**'])
    for name, spec, code in (('length-32767', {'length': 32767}, 0), ('length-32768', {'length': 32768}, 0),
                             ('over-32767', {'length': 40000}, 0),
                             ('control-after-32767', {'length': 32767, 'suffix': '\x01b'}, 0)):
        add(f'cells/{name}', 'cells', f'32767 文字で切る({name})',
            [{'op': 'file', 'path': 'doc/c.md', 'build': {'name': 'long_cell', **spec}}], ['doc/c.md'] + OUT,
            expect_legacy={'exit': code})
    for char in ('\x00', '\x08', '\x0e', '\x1b', '\x1f'):
        add(f'cells/control-{ord(char):02x}', 'cells', f'制御文字 \\x{ord(char):02x} は IllegalCharacterError(何も作らない)',
            [f('doc/c.md', f'| v |\n|---|\n| a{char}b |\n')], ['doc/c.md'] + OUT, expect_legacy={'exit': 1})
    add('cells/control-in-second-input', 'cells', '2 つ目の入力の制御文字でも何も作らない',
        [f('doc/a.md', TABLE), f('doc/b.md', '| v |\n|---|\n| x\x02y |\n')], ['doc/a.md', 'doc/b.md'] + OUT,
        expect_legacy={'exit': 1})


def sheets_cases():
    def tables(headings):
        return ''.join(f'# {heading}\n| v |\n|---|\n| {index} |\n' for index, heading in enumerate(headings))

    def one(name, title, headings, stem='v', code=0, **extra):
        add(f'sheets/{name}', 'sheets', title, [f(f'doc/{stem}.md', tables(headings))], [f'doc/{stem}.md'] + OUT,
            expect_legacy={'exit': code}, **extra)

    one('forbidden-chars', '禁止文字 \\/*?[]: は空白にして前後を除く', ['a\\b/c*d?e[f]g:h', '[x]', ':y:'])
    add('sheets/empty-name-default', 'sheets', '名前が空なら Sheet、重なれば Sheet_2',
        [f('doc/[].md', '| a |\n|---|\n| 1 |\n\n| b |\n|---|\n| 2 |\n')], ['doc/[].md'] + OUT, expect_legacy={'exit': 0})
    one('length-31-and-32', '31 文字はそのまま、32 文字以上は 31 文字で切る', ['a' * 29, 'b' * 30, 'c' * 40])
    one('duplicates-many', '同じ名前の 12 回(_2〜_12。31 文字の名前は前を切る)', ['同じ'] * 12 + ['z' * 29] * 11)
    one('case-differences', '大文字小文字だけの違い(openpyxl が番号を足す)', ['Abc', 'abc', 'ABC', 'aBc'])
    one('warning-31-exact-case', '31 文字ちょうどと大文字小文字違い(警告は 1 回だけ)', ['a' * 29, 'A' * 29, 'a' * 28 + 'A'])
    one('warning-30-two-digit', '30 文字の名前と 2 桁の番号(警告)', ['a' * 28, 'a' * 28 + '9', 'A' * 28])
    one('name-ending-digit', '見出しが a・a5・A の順(v a・v a5・v A6)', ['a', 'a5', 'A'])
    one('name-ending-digit-again', 'v a・v A1 の後の a1(v a11)', ['a', 'A', 'a1'])
    one('comma-in-names', '名前の , と、つないだ文字列の照合', ['x', 'x,2', 'X', 'y5,z', 'Y', 'z,', 'Z'])
    one('unicode-digits', 'Unicode の 10 進数字(\\d)と、10 進数字でない数字', ['a', 'a٣', 'A', 'b', 'b５', 'B', 'c', 'c²', 'C'])
    one('turkish-i', 'トルコ語の i(İ・ı)と re.I の同じと見なす組', ['ı', 'i', 'I', 'İ', 'jİ', 'Jİ'])
    one('long-s-and-kelvin', 'ſ と s、K(U+212A)と k', ['ſ', 's', 'S', 'k', 'K', 'K'])
    one('greek-sigma', 'ギリシア文字のシグマ(語末の ς と σ)', ['ΣΑΣ', 'σας', 'Σ', 'σ',
                                                    'ς', 'ΑΣ.', 'ας.', 'ΑΣ\u0301'])
    one('unicode-14-plus', 'Unicode 14 以降の文字(Unicode 13 では大文字小文字を持たない)',
        ['\u2c2f', '\u2c5f', '\ua7c0', '\ua7c1', '\U00010570', '\U00010597', '\U0001fae0', '\U0001fae0'])
    one('titlecase-and-special', '字形の大文字(ǅ)・İ・ẞ の小文字化', ['ǅ', 'ǆ', 'Ǆ', 'ẞ', 'ß', 'SS'])
    one('heading-unicode-spaces', '見出しの前後の Unicode の空白', ['\u3000a\u3000', '\u00a0a', 'a\u2003'])
    add('sheets/non-utf8-filename', 'sheets', 'UTF-8 でないファイル名(サロゲートのシート名)',
        [{'op': 'file', 'path': {'hex': b'doc/\xff\xfe.md'.hex()}, 'text': tables(['a', 'A'])}],
        [{'hex': b'doc/\xff\xfe.md'.hex()}] + OUT, linux_only=True, linux_only_reason='Mac の標準のファイルシステムは UTF-8 でない名前を作れない',
        expect_legacy={'exit': 0})
    names = ['.md', 'a.', 'a.b.md', 'x//y.md', './z.md', 'dir/.hidden.md', 'noext', 'trail. .md']
    add('sheets/doc-name-forms', 'sheets', '文書名の形(.md・a.・a.b.md・x//y.md・先頭の ./ など。names で起動する)',
        [f('names/' + (name if not name.startswith('./') else name[2:]), TABLE) for name in names if name != 'x//y.md']
        + [f('names/x/y.md', TABLE)],
        names + ['-o', '../out/book.xlsx'], cwd='names', expect_legacy={'exit': 0})
    one('long-doc-name', '31 文字を超える文書名', ['見出し'], stem='d' * 40)
    add('sheets/control-in-heading', 'sheets', '見出しの制御文字はそのまま XML に入る(openpyxl でも読めない出力)',
        [f('doc/v.md', '# 見\x01出し\n| v |\n|---|\n| 1 |\n')], ['doc/v.md'] + OUT, expect_legacy={'exit': 0})
    add('sheets/across-inputs', 'sheets', '入力をまたぐ重複(大文字小文字違いの文書名)',
        [f('doc/one/Name.md', TABLE), f('doc/two/name.md', TABLE), f('doc/three/NAME.md', TABLE)],
        ['doc/one/Name.md', 'doc/two/name.md', 'doc/three/NAME.md'] + OUT, expect_legacy={'exit': 0})


def output_cases():
    def one(name, title, argv_out, setup=(), code=None, **extra):
        expect = {'exit': code} if code is not None else {}
        add(f'output/{name}', 'output', title, doc(*setup), ['doc/input.md', '-o', argv_out],
            **({'expect_legacy': expect} if expect else {}), **extra)

    one('relative', '相対の -o', 'out/book.xlsx', code=0)
    one('absolute', '絶対の -o', '@WORK@/out/book.xlsx', code=0)
    one('dotdot-inside', '.. を含む -o', 'out/../out2/book.xlsx', code=0)
    one('dotdot-outside', '置き場の外へ出て戻る -o', '../work/out/book.xlsx', code=0)
    one('double-slashes', '連続した /', 'out//sub///book.xlsx', code=0)
    one('leading-double-slash', '先頭がちょうど //', '/@WORK@/out/book.xlsx', code=0)
    one('triple-leading-slash', '先頭が ///', '//@WORK@/out/book.xlsx', code=0)
    one('trailing-slash', '末尾の /', 'out/book.xlsx/', code=0)
    one('empty', '空の -o(現在のディレクトリになり拒否)', '', code=2)
    one('dot', '. の -o', '.', code=2)
    one('parent-symlink', '親がリンクの -o(作られた実体の場所)', 'link/book.xlsx',
        setup=(d('real'), link('link', '@WORK@/real')), code=0)
    one('parent-symlink-relative', '相対のリンクの親の下の無いディレクトリ', 'link/new/book.xlsx',
        setup=(d('real'), link('link', 'real')), code=0)
    for depth in (1, 2, 3):
        parts = '/'.join('abc'[:depth])
        one(f'readonly-dir-missing-{depth}', f'読み取り専用のディレクトリの下に {depth} 階層の無い親(2 以上は例外の連鎖)',
            f'ro/{parts}/book.xlsx', setup=(d('ro', mode='0o555'),), code=1, requires=['root でない利用者'])
    one('readonly-dir-existing', '読み取り専用の既存のディレクトリの中へは開けない', 'ro/book.xlsx',
        setup=(d('ro', mode='0o555'),), code=1, requires=['root でない利用者'])
    one('existing-regular-overwrite', '既存の通常ファイルへ上書き(同じ実体・権限 0o640 を保つ)', 'out/book.xlsx',
        setup=(f('out/book.xlsx', 'old', mode='0o640'),), code=0)
    one('existing-write-only', '書くだけの既存ファイル(0o200。読み書きで開けず、書くだけで開き直す)', 'out/book.xlsx',
        setup=(f('out/book.xlsx', 'old', mode='0o200'),), code=0, requires=['root でない利用者'])
    one('parent-is-file', '親がファイル', 'doc/input.md/book.xlsx', code=2)
    one('into-source-via-dotdot', '.. を通って正本の中', 'out/../doc/new.xlsx', code=2)
    add('output/repr-unassigned-char', 'output', 'OSError の repr(Unicode 13 で未割り当ての U+1FAE0 は \\U でエスケープ)',
        doc(), ['doc/input.md', '-o', 'out/book.xlsx', '--source-root', 'missing-\U0001fae0'], expect_legacy={'exit': 2})
    add('output/repr-quotes', 'output', 'OSError の repr の引用符の選び方', doc(),
        ['doc/input.md', '-o', 'out/book.xlsx', '--source-root', 'it\'s "x"'], expect_legacy={'exit': 2})
    add('output/repr-single-quote', 'output', "OSError の repr(' だけを含む)", doc(),
        ['doc/input.md', '-o', 'out/book.xlsx', '--source-root', "it's"], expect_legacy={'exit': 2})
    add('output/repr-non-utf8', 'output', 'OSError の repr(UTF-8 でないバイトは \\udcXX)', doc(),
        ['doc/input.md', '-o', 'out/book.xlsx', '--source-root', {'hex': b'miss\xff'.hex()}], linux_only=True,
        linux_only_reason='Mac の標準のファイルシステムは UTF-8 でない名前を作れない', expect_legacy={'exit': 2})
    for depth, code in ((985, 0), (986, 2), (987, 2)):
        label = {985: '前', 986: '等しい', 987: '後'}[depth]
        add(f'output/recursion-missing-{depth}', 'output', f'出力先の親の無い階層の再帰の上限の{label}(out と d を {depth} 個)',
            doc(), ['doc/input.md', '-o', 'out/' + '/'.join(['d'] * depth) + '/book.xlsx'],
            expect_legacy={'exit': code, **({'stderr_contains': 'maximum recursion depth exceeded'} if code else {})},
            expect_legacy_mac={'exit': 2, 'stderr_contains': 'File name too long'},
            notes='再帰の上限はディレクトリの作成(Path.mkdir)より事前検査(physical_directory)が先に当たる(measured.recursion)。'
                  'Mac はパスの長さの上限(1024 バイト)が先に当たり、physical_directory の最初の lstat が ENAMETOOLONG に'
                  'なる(旧版のコードから推測。Mac の実測は未確認)')
    for count, code in ((989, 0), (990, 2), (991, 2)):
        label = {989: '前', 990: '等しい', 991: '後'}[count]
        add(f'output/recursion-link-chain-{count}', 'output', f'出力先の親のリンクの連なりの再帰の上限の{label}({count} 個)',
            doc(d('real'), {'op': 'symlink_chain', 'prefix': 'l', 'count': count, 'target': 'real'}),
            ['doc/input.md', '-o', f'l{count - 1}/book.xlsx'],
            expect_legacy={'exit': code, **({'stderr_contains': 'maximum recursion depth exceeded'} if code else {})})
    components = ['long'] + [f'{index:02d}' + 'd' * 248 for index in range(18)]
    add('output/long-cwd-relative', 'output', '4096 バイトを超える現在のディレクトリからの相対の入力と -o',
        [{'op': 'deep_dirs', 'components': components}, f('/'.join(components) + '/in.md', TABLE)],
        ['in.md', '-o', 'out/book.xlsx'], cwd={'components': components},
        expect_legacy={'exit': 2, 'stderr_contains': 'File name too long'},
        notes='setup の file のパスも PATH_MAX を超えるので、比較器は deep_dirs と同じく親の fd からたどって書く')
    add('output/long-cwd-absolute-output', 'output', '4096 バイトを超える現在のディレクトリからの相対の入力と、短い絶対の -o',
        [{'op': 'deep_dirs', 'components': components}, f('/'.join(components) + '/in.md', TABLE)],
        ['in.md', '-o', '@WORK@/out/book.xlsx'], cwd={'components': components},
        expect_legacy={'exit': 2, 'stderr_contains': 'File name too long'},
        notes='旧版は getcwd で長い現在地を取れた後、正本の解決の lstat が ENAMETOOLONG になり終了 2(measured)')


def env_cases():
    for name, value in (('unset', None), ('utc', 'UTC'), ('asia-tokyo', 'Asia/Tokyo'), ('america-new-york', 'America/New_York')):
        env = {'set': {}, 'unset': ['TZ']} if value is None else env_set(TZ=value)
        add(f'env/tz-{name}', 'env', f'TZ: {name}(ZIP の時刻が実行の前後の現地時刻の範囲に入る)', doc(), ['doc/input.md'] + OUT,
            env=env, expect_legacy={'exit': 0})
    for mask in ('022', '077'):
        add(f'env/umask-{mask}', 'env', f'umask {mask}(作ったディレクトリとファイルの権限)', doc(), ['doc/input.md'] + OUT,
            umask=mask, expect_legacy={'exit': 0})
    for mask in ('277', '477'):
        add(f'env/umask-{mask}-existing-parent', 'env', f'umask {mask}・出力先の親あり(旧版は一時ファイルで失敗。Q4)',
            doc(d('out')), ['doc/input.md'] + OUT, umask=mask, approved_differences=['temp-file'], expect_legacy={'exit': 1},
            notes='Rust の期待は同じ入力を umask 022 で流した旧版の中身・出力・終了値。権限はこの umask の値(旧版が残したものと同じ)')
    add('env/umask-277-missing-parent', 'env', 'umask 277・出力先の親なし(親が 0o500 になり XLSX を開けない。通常の比較)',
        doc(), ['doc/input.md'] + OUT, umask='277', expect_legacy={'exit': 1})
    scenario = [f('doc/日本語.md', TABLE)]
    argv = ['doc/日本語.md', 'doc/無い.md', '-o', 'out/出力.xlsx']
    encodings = [
        ('lc-all-c', {'LC_ALL': 'C'}, ['PYTHONUTF8']),
        ('lc-all-posix', {'LC_ALL': 'POSIX'}, ['PYTHONUTF8']),
        ('lang-en-us-utf8', {'LANG': 'en_US.UTF-8'}, ['PYTHONUTF8']),
        ('lc-all-c-utf8', {'LC_ALL': 'C.UTF-8'}, ['PYTHONUTF8']),
        ('pythonioencoding-ascii', {'PYTHONIOENCODING': 'ascii'}, []),
        ('pythonioencoding-latin1-strict', {'PYTHONIOENCODING': 'latin-1:strict'}, []),
        ('pythonutf8-0', {'PYTHONUTF8': '0', 'LC_ALL': 'C'}, []),
        ('pythoncoercelocale-0', {'PYTHONCOERCECLOCALE': '0', 'LC_ALL': 'C'}, ['PYTHONUTF8']),
    ]
    for name, values, unset in encodings:
        add(f'env/encoding-{name}', 'env', f'文字コードの設定: {name}(期待は PYTHONUTF8=1 の旧版)', scenario, argv,
            env={'set': values, 'unset': unset}, legacy_env={'set': {}, 'unset': []}, approved_differences=['encoding-settings'],
            expect_legacy={'exit': 0})
    add('env/encoding-traceback-lc-all-c', 'env', '文字コードの設定: LC_ALL=C での呼出履歴(UTF-8 の誤りの入力)',
        [fh('doc/誤り.md', b'\xff')], ['doc/誤り.md'] + OUT, env={'set': {'LC_ALL': 'C'}, 'unset': ['PYTHONUTF8']},
        legacy_env={'set': {}, 'unset': []}, approved_differences=['encoding-settings'], expect_legacy={'exit': 1})
    add('env/encoding-non-utf8-arg-lc-all-c', 'env', '文字コードの設定: LC_ALL=C で UTF-8 でない -o(標準出力は surrogateescape)',
        doc(), ['doc/input.md', '-o', {'hex': b'out/\xff.xlsx'.hex()}], env={'set': {'LC_ALL': 'C'}, 'unset': ['PYTHONUTF8']},
        legacy_env={'set': {}, 'unset': []}, approved_differences=['encoding-settings'], linux_only=True,
        linux_only_reason='Mac の標準のファイルシステムは UTF-8 でない名前を作れない', expect_legacy={'exit': 0})
    both = doc()
    both_argv = ['doc/none.md', 'doc/input.md'] + OUT
    add('env/pythonunbuffered', 'env', 'PYTHONUNBUFFERED=1(WARN と OK)', both, both_argv, env=env_set(PYTHONUNBUFFERED='1'),
        expect_legacy={'exit': 0})
    add('env/default-merged-streams', 'env', '標準エラーを標準出力と同じパイプにする(出る順)', both, both_argv, stderr='stdout',
        expect_legacy={'exit': 0})
    add('env/pythonunbuffered-merged-streams', 'env', 'PYTHONUNBUFFERED=1 で標準エラーを標準出力と同じパイプにする', both, both_argv,
        stderr='stdout', env=env_set(PYTHONUNBUFFERED='1'), expect_legacy={'exit': 0})
    add('env/no-tables-merged-streams', 'env', '表なしの文と WARN を同じパイプにする', doc(f('doc/empty.md', '# none\n')),
        ['doc/none.md', 'doc/empty.md'] + OUT, stderr='stdout', expect_legacy={'exit': 2})
    digits = [('0', 0), ('640', 0), ('space-640', 0, ' 640'), ('plus-640', 0, '+640'), ('empty', 0, ''),
              ('639', 1), ('1', 1), ('abc', 1), ('minus-1', 1, '-1'), ('trailing-space', 1, '640 '),
              ('huge', 1, '99999999999999999999')]
    for item in digits:
        name, code = item[0], item[1]
        value = item[2] if len(item) > 2 else name
        add(f'env/pythonintmaxstrdigits-{name}', 'env', f'PYTHONINTMAXSTRDIGITS={value!r}', doc(), ['doc/input.md'] + OUT,
            env=env_set(PYTHONINTMAXSTRDIGITS=value), expect_legacy={'exit': code})
    optimize = [('empty', ''), ('space-2', ' 2'), ('tab-2', '\t2'), ('trailing-space', '2 '), ('plus-2', '+2'),
                ('leading-zero', '02'), ('minus-0', '-0'), ('hex', '0x2'), ('int-max', '2147483647'),
                ('int-max-plus-1', '2147483648'), ('huge', '99999999999'), ('minus-2', '-2'), ('1', '1'), ('3', '3')]
    for name, value in optimize:
        add(f'env/pythonoptimize-{name}', 'env', f'PYTHONOPTIMIZE={value!r} の --help(2 以上で説明文が消える)', doc(), ['--help'],
            env=env_set(PYTHONOPTIMIZE=value), expect_legacy={'exit': 0})
    add('env/pythonoptimize-2-normal-run', 'env', 'PYTHONOPTIMIZE=2 の通常の出力', doc(), ['doc/input.md'] + OUT,
        env=env_set(PYTHONOPTIMIZE='2'), expect_legacy={'exit': 0})
    seeds = [('empty', '', 0), ('space-5', ' 5', 0), ('tab-5', '\t5', 0), ('plus-5', '+5', 0), ('minus-0', '-0', 0),
             ('trailing-space', '5 ', 1), ('max', '4294967295', 0), ('max-plus-1', '4294967296', 1), ('minus-1', '-1', 1),
             ('abc', 'abc', 1), ('random', 'random', 0), ('Random', 'Random', 1)]
    for name, value, code in seeds:
        add(f'env/pythonhashseed-{name}', 'env', f'PYTHONHASHSEED={value!r}', doc(), ['doc/input.md'] + OUT,
            env=env_set(PYTHONHASHSEED=value), expect_legacy={'exit': code})


def io_cases():
    both = doc()
    both_argv = ['doc/none.md', 'doc/input.md'] + OUT
    no_tables = doc(f('doc/empty.md', '# none\n'))
    warn_only = ['doc/none.md'] + OUT
    bad = [fh('doc/bad.md', b'\xff')]
    warning_doc = [f('doc/v.md', ''.join(f'# {heading}\n| v |\n|---|\n| 1 |\n' for heading in ('a' * 29, 'A' * 29)))]
    linux = {'linux_only': True, 'linux_only_reason': 'Mac に /dev/full が無い'}

    add('io/stdout-closed-pipe', 'io', '標準出力が閉じたパイプ(終了時の書き出しで終了 120)', doc(), ['doc/input.md'] + OUT,
        stdout='closed-pipe', expect_legacy={'exit': 120})
    add('io/stdout-closed-pipe-unbuffered', 'io', '標準出力が閉じたパイプ・PYTHONUNBUFFERED(print の呼出履歴)', doc(),
        ['doc/input.md'] + OUT, stdout='closed-pipe', env=env_set(PYTHONUNBUFFERED='1'), expect_legacy={'exit': 1})
    add('io/stdout-dev-full', 'io', '標準出力が /dev/full(XLSX を作った後に終了 120)', doc(), ['doc/input.md'] + OUT,
        stdout='dev-full', expect_legacy={'exit': 120}, **linux)
    add('io/stdout-dev-full-unbuffered', 'io', '標準出力が /dev/full・PYTHONUNBUFFERED', doc(), ['doc/input.md'] + OUT,
        stdout='dev-full', env=env_set(PYTHONUNBUFFERED='1'), expect_legacy={'exit': 1}, **linux)
    add('io/stdout-closed-fd', 'io', '標準出力の fd が閉じている(OK の行は出ない)', both, both_argv, stdout='closed',
        expect_legacy={'exit': 0})
    add('io/stdout-directory', 'io', '標準出力がディレクトリ(起動の失敗)', doc(), ['doc/input.md'] + OUT, stdout='directory',
        approved_differences=['fatal-current-thread'], expect_legacy={'exit': 1})
    for limit in (0, 10):
        for unbuffered in (False, True):
            suffix = '-unbuffered' if unbuffered else ''
            add(f'io/stdout-file-fsize-{limit}{suffix}', 'io',
                f'標準出力を通常のファイルにし RLIMIT_FSIZE={limit}(表なしの文の書き込みが途中まで通る)',
                no_tables, ['doc/empty.md'] + OUT, stdout='file', rlimit_fsize=limit,
                env=env_set(PYTHONUNBUFFERED='1') if unbuffered else {'set': {}, 'unset': []},
                expect_legacy={'exit': 1 if unbuffered else 120})
            add(f'io/stderr-file-fsize-{limit}{suffix}', 'io',
                f'標準エラーを通常のファイルにし RLIMIT_FSIZE={limit}(WARN の書き込みが途中まで通る)',
                doc(), warn_only, stderr='file', rlimit_fsize=limit,
                env=env_set(PYTHONUNBUFFERED='1') if unbuffered else {'set': {}, 'unset': []},
                expect_legacy={'exit': 1 if unbuffered else 120})
    add('io/stderr-closed-pipe', 'io', '標準エラーが閉じたパイプ(WARN)', doc(), warn_only, stderr='closed-pipe',
        expect_legacy={'exit': 120})
    add('io/stderr-closed-pipe-unbuffered', 'io', '標準エラーが閉じたパイプ・PYTHONUNBUFFERED(WARN)', doc(), warn_only,
        stderr='closed-pipe', env=env_set(PYTHONUNBUFFERED='1'), expect_legacy={'exit': 1})
    add('io/stderr-dev-full', 'io', '標準エラーが /dev/full(WARN)', doc(), warn_only, stderr='dev-full',
        expect_legacy={'exit': 120}, **linux)
    add('io/stderr-closed-fd', 'io', '標準エラーの fd が閉じている(WARN は標準出力へ出る)', doc(), warn_only, stderr='closed',
        expect_legacy={'exit': 2})
    add('io/stderr-closed-fd-traceback', 'io', '標準エラーの fd が閉じている呼出履歴(捨てる)', bad, ['doc/bad.md'] + OUT,
        stderr='closed', expect_legacy={'exit': 1})
    add('io/stderr-closed-fd-success', 'io', '標準エラーの fd が閉じた通常の出力', both, both_argv, stderr='closed',
        expect_legacy={'exit': 0})
    add('io/stderr-directory', 'io', '標準エラーがディレクトリ(起動の失敗)', doc(), ['doc/input.md'] + OUT, stderr='directory',
        approved_differences=['fatal-current-thread'], expect_legacy={'exit': 1})
    add('io/stdin-closed', 'io', '標準入力の fd が閉じている', doc(), ['doc/input.md'] + OUT, stdin='closed',
        expect_legacy={'exit': 0})
    add('io/stdin-directory', 'io', '標準入力がディレクトリ(起動の失敗)', doc(), ['doc/input.md'] + OUT, stdin='directory',
        approved_differences=['fatal-current-thread'], expect_legacy={'exit': 1})
    add('io/both-closed-success', 'io', 'fd 1 と fd 2 が閉じた通常の出力', both, both_argv, stdout='closed', stderr='closed',
        expect_legacy={'exit': 0})
    pty = {'pty': {'columns': 80, 'rows': 24}}
    add('io/stdout-pty', 'io', '標準出力が疑似端末(行ごとに書く)', both, both_argv, stdout=pty, expect_legacy={'exit': 0})
    add('io/stderr-pty', 'io', '標準エラーが疑似端末', both, both_argv, stderr=pty, expect_legacy={'exit': 0})
    add('io/both-pty', 'io', '標準出力と標準エラーが同じ疑似端末', both, both_argv, stdout=pty, stderr='stdout',
        expect_legacy={'exit': 0})
    add('io/warning-stderr-closed-fd', 'io', '標準エラーが閉じた起動での 31 文字超えの警告(捨てる)', warning_doc,
        ['doc/v.md'] + OUT, stderr='closed', expect_legacy={'exit': 0})
    add('io/warning-stderr-closed-pipe', 'io', '標準エラーが閉じたパイプでの警告(溜めに残り終了 120)', warning_doc,
        ['doc/v.md'] + OUT, stderr='closed-pipe', expect_legacy={'exit': 120})
    add('io/warning-stderr-closed-pipe-unbuffered', 'io', '標準エラーが閉じたパイプでの警告・PYTHONUNBUFFERED(終了 0)', warning_doc,
        ['doc/v.md'] + OUT, stderr='closed-pipe', env=env_set(PYTHONUNBUFFERED='1'), expect_legacy={'exit': 0})
    # 書く側を O_NONBLOCK にして満杯にしたパイプ(write は EAGAIN)。旧版の FileIO.write は None を
    # 返し、BufferedWriter は BlockingIOError(write could not complete without blocking)にして書けな
    # かった分を溜めに残し、溜めの無い TextIOWrapper(PYTHONUNBUFFERED)は None を見ずに行を捨てる。
    nonblock = [
        ('ok', '成功', doc(), ['doc/input.md'] + OUT),
        ('warn', 'WARN', doc(), both_argv),
        ('reject', '出力を拒否', doc(), ['doc/input.md', '-o', 'doc/new.xlsx']),
    ]
    exits = {
        ('stdout', False): {'ok': 120, 'warn': 120, 'reject': 2},
        ('stdout', True): {'ok': 0, 'warn': 0, 'reject': 2},
        ('stderr', False): {'ok': 0, 'warn': 120, 'reject': 120},
        ('stderr', True): {'ok': 0, 'warn': 0, 'reject': 2},
    }
    for stream in ('stdout', 'stderr'):
        for unbuffered in (False, True):
            suffix = '-unbuffered' if unbuffered else ''
            label = '・PYTHONUNBUFFERED' if unbuffered else '(既定の溜め方)'
            for name, title, setup, argv in nonblock:
                add(f'io/{stream}-nonblock-full-{name}{suffix}', 'io',
                    f'{"標準出力" if stream == "stdout" else "標準エラー"}が O_NONBLOCK の満杯のパイプ{label}: {title}',
                    setup, argv, **{stream: 'nonblock-full-pipe'},
                    env=env_set(PYTHONUNBUFFERED='1') if unbuffered else {'set': {}, 'unset': []},
                    expect_legacy={'exit': exits[(stream, unbuffered)][name]})
    for unbuffered, code in ((False, 120), (True, 0)):
        suffix = '-unbuffered' if unbuffered else ''
        env = env_set(PYTHONUNBUFFERED='1') if unbuffered else {'set': {}, 'unset': []}
        add(f'io/warning-stderr-nonblock-full{suffix}', 'io',
            '標準エラーが O_NONBLOCK の満杯のパイプでの 31 文字超えの警告' + ('・PYTHONUNBUFFERED' if unbuffered else ''),
            warning_doc, ['doc/v.md'] + OUT, stderr='nonblock-full-pipe', env=env, expect_legacy={'exit': code})
    for unbuffered, codes in ((False, (120, 120, 120)), (True, (1, 0, 2))):
        suffix = '-unbuffered' if unbuffered else ''
        env = env_set(PYTHONUNBUFFERED='1') if unbuffered else {'set': {}, 'unset': []}
        no_tables_code, help_code, argerror_code = codes
        add(f'io/stdout-nonblock-full-no-tables{suffix}', 'io', '標準出力が O_NONBLOCK の満杯のパイプ: 表なしの文',
            no_tables, ['doc/empty.md'] + OUT, stdout='nonblock-full-pipe', env=env, expect_legacy={'exit': no_tables_code})
        add(f'io/stdout-nonblock-full-help{suffix}', 'io', '標準出力が O_NONBLOCK の満杯のパイプ: --help',
            doc(), ['--help'], stdout='nonblock-full-pipe', env=env, expect_legacy={'exit': help_code})
        add(f'io/stderr-nonblock-full-argerror{suffix}', 'io', '標準エラーが O_NONBLOCK の満杯のパイプ: 引数の誤り',
            doc(), ['--bogus'], stderr='nonblock-full-pipe', env=env, expect_legacy={'exit': argerror_code})
    argparse_fd = [
        ('help-fd1-closed', ['--help'], 'closed', 'pipe', 0, 'fd 1 が閉じた --help(説明を標準エラーへ)'),
        ('help-fd2-closed', ['--help'], 'pipe', 'closed', 0, 'fd 2 だけが閉じた --help'),
        ('argerror-fd2-closed', ['doc/input.md', '-o', 'out/book.xlsx', '--bogus'], 'pipe', 'closed', 1,
         'fd 2 が閉じた引数の誤り(使い方の行だけを標準出力へ)'),
        ('required-missing-fd2-closed', ['doc/input.md'], 'pipe', 'closed', 1, 'fd 2 が閉じた必須の引数の欠落'),
        ('help-both-closed', ['--help'], 'closed', 'closed', 1, '両方が閉じた --help'),
        ('argerror-both-closed', ['--bogus'], 'closed', 'closed', 1, '両方が閉じた引数の誤り'),
        ('argerror-fd1-closed', ['--bogus'], 'closed', 'pipe', 2, 'fd 1 だけが閉じた引数の誤り'),
    ]
    for name, argv, stdout, stderr, code, title in argparse_fd:
        add(f'io/{name}', 'io', title, doc(), argv, stdout=stdout, stderr=stderr, expect_legacy={'exit': code})


def signals_cases():
    stdout_full = {'wait': 'xlsx-complete', 'settle_ms': 500, 'signal': 'SIGINT', 'interval_ms': 500, 'after': 'drain'}
    for count, expect in ((1, {'exit': 0}), (2, {'exit': 120}), (3, {'signal': 'SIGINT'})):
        add(f'signals/stdout-full-sigint-{count}', 'signals',
            f'標準出力のパイプが一杯で終了時の書き出しで止まった間に SIGINT を {count} 回(既定の溜め方)',
            doc(), ['doc/input.md'] + OUT, stdout='full-pipe', signals={**stdout_full, 'count': count},
            approved_differences=['variable-output'], expect_legacy=expect)
    add('signals/stdout-full-unbuffered-sigint-1', 'signals', '標準出力のパイプが一杯・PYTHONUNBUFFERED で print の所で SIGINT を 1 回',
        doc(), ['doc/input.md'] + OUT, stdout='full-pipe', env=env_set(PYTHONUNBUFFERED='1'),
        signals={**stdout_full, 'count': 1}, approved_differences=['variable-output'], expect_legacy={'signal': 'SIGINT'})
    missing = [{'expand': {'format': 'doc/missing{index:04d}.md', 'count': 2000}}]
    stderr_full = {'wait': 'pipe-full-stable', 'signal': 'SIGINT', 'interval_ms': 500, 'after': 'drain'}
    for unbuffered in (False, True):
        for count in (1, 2, 3):
            suffix = '-unbuffered' if unbuffered else ''
            add(f'signals/stderr-full-warn{suffix}-sigint-{count}', 'signals',
                f'標準エラーのパイプが一杯で WARN を書く間に SIGINT を {count} 回' + ('(PYTHONUNBUFFERED)' if unbuffered else '(既定の溜め方)'),
                doc(), missing + ['doc/input.md'] + OUT, stderr='full-pipe',
                env=env_set(PYTHONUNBUFFERED='1') if unbuffered else {'set': {}, 'unset': []},
                signals={**stderr_full, 'count': count}, approved_differences=['variable-output'],
                expect_legacy={'signal': 'SIGINT'},
                notes='stderr は「pipe-full」ではなく、2000 個の WARN でパイプを満たす。送る前に子が終わっていたら、それ以降は送らずに記録して比べる')
    # 拒否の行(ERROR 出力を拒否)の print が、先に満杯にした標準エラーで止まった間の SIGINT。旧版の
    # print の例外は、処理中の OutputError(正本・出力先を解決できないときは from exc の元も)を
    # __context__ に持つ。2〜3 回では表示が途中で切れても、最後の行は KeyboardInterrupt になる。
    blocked = {'wait': 'stderr-write-blocked', 'signal': 'SIGINT', 'interval_ms': 500, 'after': 'drain'}
    reasons = [
        ('inside-source', '正本の中', doc(), ['doc/input.md', '-o', 'doc/new.xlsx']),
        ('missing-source-root', '--source-root が無い(from exc の連鎖)', doc(),
         ['doc/input.md', '-o', 'out/book.xlsx', '--source-root', 'missing-root']),
        ('hardlink', '出力先に複数のハードリンク', doc(f('out/book.xlsx', 'old'), {'op': 'hardlink', 'path': 'out/other', 'target': 'out/book.xlsx'}),
         ['doc/input.md'] + OUT),
    ]
    for name, title, setup, argv in reasons:
        for unbuffered in (False, True):
            for count in (1, 2, 3):
                suffix = '-unbuffered' if unbuffered else ''
                add(f'signals/rejected-{name}{suffix}-sigint-{count}', 'signals',
                    f'拒否({title})の行を書く間に、先に満杯にした標準エラーで SIGINT を {count} 回'
                    + ('(PYTHONUNBUFFERED)' if unbuffered else '(既定の溜め方)'),
                    setup, argv, stderr='prefilled-full-pipe',
                    env=env_set(PYTHONUNBUFFERED='1') if unbuffered else {'set': {}, 'unset': []},
                    signals={**blocked, 'count': count}, approved_differences=['variable-output'],
                    expect_legacy={'signal': 'SIGINT'}, linux_only=True,
                    linux_only_reason='子が標準エラーへの write で止まったことを /proc/<pid>/syscall と stat で確かめる(Mac に無い)')
    add('signals/sigint-ignored-stdout-full', 'signals', '起動時に SIGINT を無視した子は、無視を続ける(標準出力が一杯の間に 1 回)',
        doc(), ['doc/input.md'] + OUT, stdout='full-pipe', sigint_ignored=True, signals={**stdout_full, 'count': 1},
        expect_legacy={'exit': 0})
    add('signals/sigint-ignored-stderr-full', 'signals', '起動時に SIGINT を無視した子(標準エラーが一杯の間に 2 回)',
        doc(), missing + ['doc/input.md'] + OUT, stderr='full-pipe', sigint_ignored=True,
        signals={**stderr_full, 'count': 2}, expect_legacy={'exit': 0})
    add('signals/sigterm-stdout-full', 'signals', '標準出力が一杯の間の SIGTERM(全バイトを比べる)', doc(), ['doc/input.md'] + OUT,
        stdout='full-pipe', signals={**stdout_full, 'signal': 'SIGTERM', 'count': 1}, expect_legacy={'signal': 'SIGTERM'})
    add('signals/sigterm-stderr-full', 'signals', '標準エラーが一杯の間の SIGTERM(全バイトを比べる)', doc(),
        missing + ['doc/input.md'] + OUT, stderr='full-pipe', signals={**stderr_full, 'signal': 'SIGTERM', 'count': 1},
        expect_legacy={'signal': 'SIGTERM'})


def write_cases():
    three = [f('doc/three.md', THREE_ROWS)]
    add('write/open-eacces', 'write', '出力先を開けない(読み取り専用 0o444 の既存ファイル。読み書きも書くだけも EACCES)',
        three + [f('out/book.xlsx', 'old', mode='0o444')], ['doc/three.md'] + OUT, expect_legacy={'exit': 1},
        requires=['root でない利用者'], notes='両方とも失敗したら、後の誤りだけを呼出履歴にする')
    add('write/fsize-zero', 'write', '最初の書き出しで失敗(RLIMIT_FSIZE=0)。番地の欄のほかは全部の行を比べる',
        three, ['doc/three.md'] + OUT, rlimit_fsize=0, approved_differences=['write-partial-failure', 'variable-output'],
        expect_legacy={'exit': 1})
    add('write/fsize-half-legacy', 'write', '両方が途中で失敗(RLIMIT_FSIZE を旧版の XLSX の大きさの半分に)',
        three, ['doc/three.md'] + OUT, rlimit_fsize={'rule': 'half-legacy-xlsx'},
        approved_differences=['write-partial-failure', 'variable-output'], expect_legacy={'exit': 1},
        notes='旧版の失敗が ZIP の書き込みの中であること(Q4 の見分け方に当たらないこと)を確かめる')
    add('write/fsize-legacy-ok-rust-fails', 'write', '旧版は成功し Rust だけが失敗(RLIMIT_FSIZE を旧版の XLSX の大きさに)',
        three, ['doc/three.md'] + OUT, rlimit_fsize={'rule': 'legacy-xlsx-size'},
        approved_differences=['uncompressed', 'write-partial-failure', 'variable-output'], expect_legacy={'exit': 0},
        compare='fixed',
        expected_rust={
            'exit': 1,
            'stdout': '',
            'stderr_head_through': 'wb.save(checked_out)',
            'stderr_last_line': 'OSError: [Errno 27] File too large',
            'second_traceback': 'Exception ignored in: <function ZipFile.__del__ at 0x…> の形',
            'inputs_unchanged': True,
        })
    for name, setup, value in (('tmpdir-missing', [], '@WORK@/no-tmp'),
                               ('tmpdir-unwritable', [d('tmp-ro', mode='0o555')], '@WORK@/tmp-ro'),
                               ('tmpdir-is-file', [f('tmp-file', 'x')], '@WORK@/tmp-file')):
        add(f'write/{name}', 'write', f'TMPDIR が {name.split("-", 1)[1]}(旧版は /tmp などへ切り替える)',
            three + setup, ['doc/three.md'] + OUT, env=env_set(TMPDIR=value), approved_differences=['temp-file'],
            expect_legacy={'exit': 0},
            notes='旧版は tempfile の候補へ切り替えて成功する(measured.temp_dir)。TMPDIR に何も作らないことも確かめる')


def generated_cases():
    for group in range(GROUPS):
        data = generated_group(SEED, group)
        add(f'generated/g{group:03d}', 'generated', f'生成したケースの組 {group}({FILES_PER_GROUP} 個の入力を 1 回の起動で)',
            [{'op': 'generated', 'seed': SEED, 'group': group}], data['argv'])


def build_cases():
    CASES.clear()
    existing_cases()
    external_cases()
    csv_cases()
    args_cases()
    input_cases()
    tables_cases()
    cells_cases()
    sheets_cases()
    output_cases()
    env_cases()
    io_cases()
    signals_cases()
    write_cases()
    generated_cases()
    ids = [case['id'] for case in CASES]
    if len(ids) != len(set(ids)):
        raise ValueError('ケースの ID が重なっている')
    return list(CASES)


def render():
    cases = build_cases()
    digest, inputs = generated_digest(SEED, GROUPS)
    if inputs < 300:
        raise ValueError('生成したケースの入力が 300 件未満')
    legacy = ROOT / LEGACY_REL
    counts = {}
    for case in cases:
        counts[case['category']] = counts.get(case['category'], 0) + 1
    document = {
        'schema_version': 1,
        'issue': 482,
        'generator': 'native/fixtures/r4e/generate_cases.py',
        'legacy_script': {'path': LEGACY_REL, 'sha256': hashlib.sha256(legacy.read_bytes()).hexdigest()},
        'schema': SCHEMA,
        'layout': LAYOUT,
        'placeholders': PLACEHOLDERS,
        'base_env': BASE_ENV,
        'categories': CATEGORIES,
        'approved_differences': APPROVED_DIFFERENCES,
        'always_applied': ALWAYS_APPLIED,
        'signal_waits': SIGNAL_WAITS,
        'rlimit_rules': RLIMIT_RULES,
        'preflight': [
            'Linux では最初に 1 回、LC_ALL=C.UTF-8(PYTHONUTF8 なし)の旧版が PYTHONUTF8=1 の旧版と同じ結果になることを確かめる(設計「決定済みの差」の文字コードの行)',
            '旧版用の Python が CPython 3.10.11・openpyxl 3.1.5・et_xmlfile 2.0.0・lxml なしであること',
            'native/fixtures/r4e の 3 つの生成器の --check が通ること',
        ],
        'not_transcribed': NOT_TRANSCRIBED,
        'measured': MEASURED,
        'generated': {
            'seed': SEED,
            'groups': GROUPS,
            'files_per_group': FILES_PER_GROUP,
            'inputs': inputs,
            'sha256': digest,
            'function': 'generated_group(seed, group)',
        },
        'counts': counts,
        'case_count': len(cases),
        'cases': cases,
    }
    return (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def materialize(target, group):
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError('書き出し先は空のディレクトリにしてください')
    data = generated_group(SEED, group)
    for path, body in data['files']:
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
    (target / 'argv.json').write_text(json.dumps(data['argv'], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description='R4E の比較のケースを作る')
    parser.add_argument('--check', action='store_true', help='2 回作って同じで、cases.json と一致するかだけを確かめる')
    parser.add_argument('--materialize', metavar='DIR', help='生成したケースの 1 組を DIR へ書き出す')
    parser.add_argument('--group', type=int, default=0, help='--materialize で書き出す組の番号')
    args = parser.parse_args(argv)
    try:
        if args.materialize:
            materialize(args.materialize, args.group)
            print(f'書いた: {args.materialize}(組 {args.group})')
            return 0
        first = render()
        second = render()
    except (OSError, ValueError) as error:
        print(f'失敗: {error}', file=sys.stderr)
        return 1
    if first != second:
        print('失敗: 同じ種で 2 回作った結果が違う', file=sys.stderr)
        return 1
    if args.check:
        try:
            current = CASES_JSON.read_bytes()
        except OSError as error:
            print(f'失敗: {CASES_REL} を読めない: {error}', file=sys.stderr)
            return 1
        if current != first:
            print(f'失敗: {CASES_REL} が生成の結果と違う。生成し直す', file=sys.stderr)
            return 1
        print(f'一致: {CASES_REL}')
        return 0
    CASES_JSON.write_bytes(first)
    print(f'書いた: {CASES_REL}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
