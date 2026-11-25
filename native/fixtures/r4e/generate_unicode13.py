#!/usr/bin/env python3
"""R4E(#482)の Unicode 13.0.0 の表を、本家 CPython 3.10.11 から生成する。

使い方(リポジトリの直下で):
  <CPython 3.10.11 の python> -I -B native/fixtures/r4e/generate_unicode13.py           # 書き出す
  <CPython 3.10.11 の python> -I -B native/fixtures/r4e/generate_unicode13.py --check   # 一致を確かめる

- 出力は native/crates/cli/src/commands/md_tables_to_xlsx/unicode13.rs の 1 ファイル。
- 表の元は、旧版の str.lower・re が使う CPython の関数そのもの。
  - 完全な小文字化: _PyUnicode_ToLowerFull(str.lower が 1 文字ずつ使う。1 文字から複数の文字を含む)
  - 語末のシグマの判定: _PyUnicode_IsCased・_PyUnicode_IsCaseIgnorable
  - 単純な小文字化: _sre.unicode_tolower(= _PyUnicode_ToLowercase)
  - re.I の大文字小文字の判定: _sre.unicode_iscased
  - re.I で同じと見なす文字の組: sre_compile._equivalences(_ignorecase_fixes の元)
  - 空白: str.isspace(re の Unicode の \\s と str.strip も同じ集合)
- printable と decimal は native/crates/cli/src/commands/r4b_support/unicode.rs を使うので作らない。
- 書き出した表を、Rust と同じ引き方で 0..=0x10FFFF の全部の文字について引き直し、
  CPython の関数と、str.lower の語末のシグマの実際の結果と照らしてから書く。
- 版が違う Python では、何も書かずに失敗する(終了 1)。
"""

import argparse
import ctypes
import sys
from pathlib import Path

NATIVE = Path(__file__).resolve().parents[2]
OUTPUT = NATIVE / 'crates/cli/src/commands/md_tables_to_xlsx/unicode13.rs'
OUTPUT_REL = 'native/crates/cli/src/commands/md_tables_to_xlsx/unicode13.rs'
SCRIPT_REL = 'native/fixtures/r4e/generate_unicode13.py'
LIMIT = 0x110000
MAX_WIDTH = 100
SIGMA = 0x3A3


class GenerateError(Exception):
    pass


def require_python():
    import unicodedata

    if sys.implementation.name != 'cpython' or tuple(sys.version_info[:3]) != (3, 10, 11):
        raise GenerateError(f'本家 CPython 3.10.11 で動かしてください(今は {sys.implementation.name} {sys.version.split()[0]})')
    if unicodedata.unidata_version != '13.0.0':
        raise GenerateError(f'unicodedata の版が 13.0.0 でない: {unicodedata.unidata_version}')


def capi():
    api = ctypes.pythonapi
    cased = api._PyUnicode_IsCased
    ignorable = api._PyUnicode_IsCaseIgnorable
    lower_full = api._PyUnicode_ToLowerFull
    lower = api._PyUnicode_ToLowercase
    for function in (cased, ignorable, lower):
        function.argtypes = [ctypes.c_uint32]
    cased.restype = ctypes.c_int
    ignorable.restype = ctypes.c_int
    lower.restype = ctypes.c_uint32
    lower_full.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    lower_full.restype = ctypes.c_int
    return cased, ignorable, lower_full, lower


def bool_ranges(predicate):
    ranges = []
    start = None
    for code in range(LIMIT + 1):
        value = code < LIMIT and predicate(code)
        if value and start is None:
            start = code
        elif not value and start is not None:
            ranges.append((start, code - 1))
            start = None
    return ranges


def lower_runs(mapping):
    """(最初, 最後, 差, 間隔)。間隔は 1 か 2。"""
    runs = []
    for code in range(LIMIT):
        target = mapping(code)
        if target == code:
            continue
        delta = target - code
        if runs:
            first, last, last_delta, step = runs[-1]
            gap = code - last
            if last_delta == delta and gap in (1, 2) and (first == last or step == gap):
                runs[-1] = (first, code, delta, gap)
                continue
        runs.append((code, code, delta, 1))
    return runs


def build_tables():
    import _sre
    import sre_compile

    cased, ignorable, lower_full_c, lower_c = capi()
    buffer = (ctypes.c_uint32 * 3)()

    def lower_full(code):
        count = lower_full_c(code, buffer)
        if not 1 <= count <= 3:
            raise GenerateError(f'_PyUnicode_ToLowerFull の長さが想定外: U+{code:04X} {count}')
        return tuple(buffer[index] for index in range(count))

    multi = []
    for code in range(LIMIT):
        full = lower_full(code)
        simple = _sre.unicode_tolower(code)
        if lower_c(code) != simple:
            raise GenerateError(f'_sre.unicode_tolower と _PyUnicode_ToLowercase が違う: U+{code:04X}')
        if full[0] != simple:
            raise GenerateError(f'完全な小文字化の先頭が単純な小文字化と違う: U+{code:04X}')
        if code != SIGMA and ''.join(map(chr, full)) != chr(code).lower():
            raise GenerateError(f'_PyUnicode_ToLowerFull と str.lower が違う: U+{code:04X}')
        if len(full) > 1:
            multi.append((code, full))
    if lower_full(SIGMA) != (0x3C3,):
        raise GenerateError('U+03A3 の完全な小文字化が U+03C3 でない')

    equivalences = [tuple(group) for group in sre_compile._equivalences]
    fixes = {member: tuple(other for other in group if other != member) for group in equivalences for member in group}
    if fixes != sre_compile._ignorecase_fixes:
        raise GenerateError('sre_compile._ignorecase_fixes が _equivalences から作れない')
    seen = [member for group in equivalences for member in group]
    if len(seen) != len(set(seen)):
        raise GenerateError('sre_compile._equivalences の組が重なっている')
    equivalences.sort()

    return {
        'space': bool_ranges(lambda code: chr(code).isspace()),
        'simple_lower': lower_runs(_sre.unicode_tolower),
        'full_lower_multi': multi,
        'cased': bool_ranges(lambda code: bool(cased(code))),
        'case_ignorable': bool_ranges(lambda code: bool(ignorable(code))),
        'sre_iscased': bool_ranges(lambda code: bool(_sre.unicode_iscased(code))),
        'equivalences': equivalences,
    }


# --- Rust と同じ引き方(書き出した表の確かめに使う) ---

def lookup_ranges(ranges, code):
    low, high = 0, len(ranges)
    while low < high:
        middle = (low + high) // 2
        if ranges[middle][1] < code:
            low = middle + 1
        else:
            high = middle
    return low < len(ranges) and ranges[low][0] <= code


def lookup_simple_lower(runs, code):
    low, high = 0, len(runs)
    while low < high:
        middle = (low + high) // 2
        if runs[middle][1] < code:
            low = middle + 1
        else:
            high = middle
    if low < len(runs):
        first, _, delta, step = runs[low]
        if first <= code and (code - first) % step == 0:
            return code + delta
    return code


def lookup_full_lower(tables, code):
    for multi_code, full in tables['full_lower_multi']:
        if multi_code == code:
            return full
    return (lookup_simple_lower(tables['simple_lower'], code),)


def lookup_equivalence(tables, code):
    for group in tables['equivalences']:
        if code in group:
            return group
    return None


def table_lower(tables, text):
    """str.lower を表で引いて行う(語末のシグマは CPython の handle_capital_sigma と同じ順で判定する)。"""
    def ignorable(char):
        return lookup_ranges(tables['case_ignorable'], ord(char))

    def cased(char):
        return lookup_ranges(tables['cased'], ord(char))

    out = []
    for index, char in enumerate(text):
        code = ord(char)
        if code != SIGMA:
            out.extend(map(chr, lookup_full_lower(tables, code)))
            continue
        j = index - 1
        while j >= 0 and ignorable(text[j]):
            j -= 1
        final = j >= 0 and cased(text[j])
        if final and index + 1 < len(text):
            j = index + 1
            while j < len(text) and ignorable(text[j]):
                j += 1
            final = j == len(text) or not cased(text[j])
        out.append('\u03c2' if final else '\u03c3')
    return ''.join(out)


def verify(tables):
    import _sre
    import sre_compile

    cased, ignorable, _, _ = capi()
    for name in ('space', 'cased', 'case_ignorable', 'sre_iscased'):
        ranges = tables[name]
        if any(start > end for start, end in ranges) or any(a[1] + 1 >= b[0] for a, b in zip(ranges, ranges[1:])):
            raise GenerateError(f'{name} の範囲が昇順で離れていない')
    runs = tables['simple_lower']
    if any(a[1] >= b[0] for a, b in zip(runs, runs[1:])):
        raise GenerateError('simple_lower の範囲が昇順で離れていない')
    for code in range(LIMIT):
        char = chr(code)
        if lookup_ranges(tables['space'], code) != char.isspace():
            raise GenerateError(f'is_space が違う: U+{code:04X}')
        if lookup_simple_lower(runs, code) != _sre.unicode_tolower(code):
            raise GenerateError(f'simple_lower が違う: U+{code:04X}')
        full = lookup_full_lower(tables, code)
        if code != SIGMA and ''.join(map(chr, full)) != char.lower():
            raise GenerateError(f'full_lower が違う: U+{code:04X}')
        if lookup_ranges(tables['cased'], code) != bool(cased(code)):
            raise GenerateError(f'cased が違う: U+{code:04X}')
        if lookup_ranges(tables['case_ignorable'], code) != bool(ignorable(code)):
            raise GenerateError(f'case_ignorable が違う: U+{code:04X}')
        if lookup_ranges(tables['sre_iscased'], code) != bool(_sre.unicode_iscased(code)):
            raise GenerateError(f'sre_iscased が違う: U+{code:04X}')
        group = lookup_equivalence(tables, code)
        expected = sre_compile._ignorecase_fixes.get(code)
        if (group is None) != (expected is None) or (
            group is not None and sorted(group) != sorted((code,) + expected)
        ):
            raise GenerateError(f'equivalence が違う: U+{code:04X}')
    # str.lower の語末のシグマを、表の引き方で実際の結果と照らす。
    sigma = 'Σ'
    for code in range(LIMIT):
        char = chr(code)
        for text in (char + sigma, 'A' + char + sigma, 'A' + sigma + char, char + sigma + 'a', sigma + char):
            if table_lower(tables, text) != text.lower():
                raise GenerateError(f'語末のシグマの判定が str.lower と違う: {text!r}')


# --- Rust の書き出し(rustfmt の整形済みの形) ---

def hex_value(value):
    return f'0x{value:x}'


ARRAY_WIDTH = 60  # rustfmt の array_width(max_width の 6 割)


def array_lines(name, type_name, items):
    """rustfmt と同じ形の定数の配列。短ければ 1 行、長ければ 1 要素 1 行。"""
    one_line = f'const {name}: &[{type_name}] = &[{", ".join(items)}];'
    if len(", ".join(items)) <= ARRAY_WIDTH and len(one_line) <= MAX_WIDTH:
        return [one_line]
    return [f'const {name}: &[{type_name}] = &[', *(f'    {item},' for item in items), '];']


def tuple_item(values):
    return f'({", ".join(values)})'


def render(tables):
    def ranges_items(ranges):
        return [tuple_item((hex_value(start), hex_value(end))) for start, end in ranges]

    out = [
        '//! 生成物。手で直さない。',
        f'//! 生成のコマンド(リポジトリの直下で): `<CPython 3.10.11 の python> -I -B {SCRIPT_REL}`',
        f'//! 一致の確かめ: `<CPython 3.10.11 の python> -I -B {SCRIPT_REL} --check`',
        '//! 元: CPython 3.10.11(Unicode 13.0.0)の関数。文字は Python の文字の番号(サロゲートを含む)で受け取る。',
        '//! printable と decimal は `r4b_support/unicode.rs` にある。',
        '',
        '/// 空白の文字(`str.isspace`)。`str.strip`・`str.split()`・`re` の Unicode の `\\s` も同じ集合。',
    ]
    out += array_lines('SPACE', '(u32, u32)', ranges_items(tables['space']))
    out += [
        '',
        '/// 単純な小文字化(`_sre.unicode_tolower`・`_PyUnicode_ToLowercase`)。',
        '/// (最初, 最後, 差, 間隔)。最初から最後まで、間隔ごとの文字に差を足す。ほかの文字は変わらない。',
    ]
    out += array_lines(
        'SIMPLE_LOWER',
        '(u32, u32, i32, u32)',
        [tuple_item((hex_value(a), hex_value(b), str(c), str(d))) for a, b, c, d in tables['simple_lower']],
    )
    out += [
        '',
        '/// 完全な小文字化(`_PyUnicode_ToLowerFull`)が 2 文字以上になる文字。',
        '/// ほかの文字の完全な小文字化は、単純な小文字化の 1 文字と同じ。',
    ]
    out += array_lines(
        'FULL_LOWER_MULTI',
        '(u32, &[u32])',
        [f'({hex_value(code)}, &[{", ".join(map(hex_value, full))}])' for code, full in tables['full_lower_multi']],
    )
    out += [
        '',
        '/// 語末のシグマの判定の cased(`_PyUnicode_IsCased`)。',
    ]
    out += array_lines('CASED', '(u32, u32)', ranges_items(tables['cased']))
    out += [
        '',
        '/// 語末のシグマの判定の case-ignorable(`_PyUnicode_IsCaseIgnorable`)。',
    ]
    out += array_lines('CASE_IGNORABLE', '(u32, u32)', ranges_items(tables['case_ignorable']))
    out += [
        '',
        '/// `re.I` の大文字小文字の判定(`_sre.unicode_iscased`)。',
    ]
    out += array_lines('SRE_ISCASED', '(u32, u32)', ranges_items(tables['sre_iscased']))
    out += [
        '',
        '/// `re.I` で同じと見なす小文字の組(`sre_compile._equivalences`。組の先頭の昇順)。',
    ]
    out += array_lines('EQUIVALENCES', '&[u32]', [f'&[{", ".join(map(hex_value, group))}]' for group in tables['equivalences']])
    out += [
        '',
        'fn in_ranges(ranges: &[(u32, u32)], code: u32) -> bool {',
        '    let index = ranges.partition_point(|&(_, end)| end < code);',
        '    ranges.get(index).is_some_and(|&(start, _)| start <= code)',
        '}',
        '',
        '/// `str.isspace()` が真になる文字か。',
        'pub fn is_space(code: u32) -> bool {',
        '    in_ranges(SPACE, code)',
        '}',
        '',
        '/// 単純な小文字化(`_sre.unicode_tolower`)。`re.I` の照合と、完全な小文字化の 1 文字目に使う。',
        'pub fn simple_lower(code: u32) -> u32 {',
        '    let index = SIMPLE_LOWER.partition_point(|&(_, last, _, _)| last < code);',
        '    match SIMPLE_LOWER.get(index) {',
        '        Some(&(first, _, delta, step)) if first <= code && (code - first).is_multiple_of(step) => {',
        '            code.wrapping_add_signed(delta)',
        '        }',
        '        _ => code,',
        '    }',
        '}',
        '',
        '/// 1 文字の完全な小文字化(`_PyUnicode_ToLowerFull`)を `out` へ足す。',
        '/// U+03A3 は U+03C3 を足す。`str.lower` の語末のシグマ(U+03C2)の判定は呼び出す側で行う',
        '/// (CPython の `handle_capital_sigma`。前へ `case_ignorable` を飛ばした文字が `cased` で、',
        '/// 後ろへ `case_ignorable` を飛ばした所が文字列の終わりか `cased` でない文字なら U+03C2)。',
        'pub fn push_full_lower(code: u32, out: &mut Vec<u32>) {',
        '    match FULL_LOWER_MULTI.binary_search_by_key(&code, |&(multi, _)| multi) {',
        '        Ok(index) => out.extend_from_slice(FULL_LOWER_MULTI[index].1),',
        '        Err(_) => out.push(simple_lower(code)),',
        '    }',
        '}',
        '',
        '/// 語末のシグマの判定の cased か(`_PyUnicode_IsCased`)。',
        'pub fn cased(code: u32) -> bool {',
        '    in_ranges(CASED, code)',
        '}',
        '',
        '/// 語末のシグマの判定の case-ignorable か(`_PyUnicode_IsCaseIgnorable`)。',
        'pub fn case_ignorable(code: u32) -> bool {',
        '    in_ranges(CASE_IGNORABLE, code)',
        '}',
        '',
        '/// `re.I` で大文字小文字を持つ文字か(`_sre.unicode_iscased`)。',
        'pub fn sre_iscased(code: u32) -> bool {',
        '    in_ranges(SRE_ISCASED, code)',
        '}',
        '',
        '/// `lower`(単純な小文字化の後の文字)を含む、`re.I` で同じと見なす組。',
        '/// `(lower,) + sre_compile._ignorecase_fixes[lower]` と同じ集合。組に無ければ `None`。',
        "pub fn sre_equivalence(lower: u32) -> Option<&'static [u32]> {",
        '    EQUIVALENCES',
        '        .iter()',
        '        .copied()',
        '        .find(|group| group.contains(&lower))',
        '}',
    ]
    for line in out:
        if len(line) > MAX_WIDTH:
            raise GenerateError(f'行が {MAX_WIDTH} 文字を超える: {line}')
    return ('\n'.join(out) + '\n').encode('utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description='R4E の Unicode 13.0.0 の表を生成する')
    parser.add_argument('--check', action='store_true', help='生成した内容とファイルが一致するかだけを確かめる')
    args = parser.parse_args(argv)
    try:
        require_python()
        tables = build_tables()
        verify(tables)
        data = render(tables)
    except GenerateError as error:
        print(f'失敗: {error}', file=sys.stderr)
        return 1
    if args.check:
        try:
            current = OUTPUT.read_bytes()
        except OSError as error:
            print(f'失敗: {OUTPUT_REL} を読めない: {error}', file=sys.stderr)
            return 1
        if current != data:
            print(f'失敗: {OUTPUT_REL} が生成の結果と違う。生成し直す', file=sys.stderr)
            return 1
        print(f'一致: {OUTPUT_REL}')
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(data)
    print(f'書いた: {OUTPUT_REL}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
