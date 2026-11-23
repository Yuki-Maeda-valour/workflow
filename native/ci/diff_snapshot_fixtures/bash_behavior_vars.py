"""bashが環境から読み込んでスクリプトの動きを変える変数の一覧(2026-11-12 09:58 追加の決定 3)。

- 一覧はbash 5.2.21(基準の容器 s3d-ubuntu2404:local と同じ版)の variables.c の初期化
  (initialize_shell_variablesと、そこから呼ぶ同じファイルの関数)と、代入の処理の表(special_vars の sv_*)から作り、
  bash_behavior_vars.json に固定する。22:31 の決定の 5 つ(BASH_ENV・BASH_FUNC_<名>%%・SHELLOPTS・BASHOPTS・
  POSIXLY_CORRECT)と、09:58 の決定が名前を挙げた TMOUT も含める。
- 一覧の変数は、効果を再現せず、比較の入力に使わない。比較器は、旧版とRustへ渡す環境にこれらがあれば失敗にする。
- 起動時の警告(setlocale など)と、子の環境へ渡る値は、これまでの決定どおり再現する。それらは kept に理由を書いて
  一覧から外す(子へ渡る値の比較を一覧で外さない)。
- ソースとの照合: `python3 -B native/ci/diff_snapshot_fixtures/bash_behavior_vars.py --source <bash-5.2 の展開先>`。
  variables.c から名前を取り出し直し、fixture の記録・分類と比べる(足りない・余分なら失敗)。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

SPEC = json.loads(Path(__file__).with_name('bash_behavior_vars.json').read_text())
# 変数の名前を第1引数に取る呼出し(variables.c の初期化の関数の中だけを見る)。
ACCESS = ('set_if_not', 'bind_variable', 'find_variable', 'get_string_value', 'unbind_variable_noref',
          'make_new_array_variable', 'init_dynamic_array_var', 'init_dynamic_assoc_var', 'INIT_DYNAMIC_VAR',
          'INIT_DYNAMIC_ARRAY_VAR')
_CALL = re.compile(r'\b(' + '|'.join(ACCESS) + r'|sv_[a-z_]+)\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"')
_STREQ = re.compile(r'\bSTREQ\s*\(\s*name\s*,\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\)')
_TABLE = re.compile(r'\{\s*"([^"]+)"\s*,\s*(sv_[a-z_]+)\s*\}')
_DEFINE = re.compile(r'^#define\s+(BASH[A-Z]+)_(PREFIX|SUFFIX)\s+"([^"]*)"')
_DEFINED = re.compile(r'^([a-z_][a-z0-9_]*) \(', re.M)
_PREPROCESSOR = re.compile(r'^#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$')
_CALLS = re.compile(r'\b([a-z_][a-z0-9_]*)\s*\(')
_SPACE = b' \t\n\v\f\r'


def _bytes(value):
    return os.fsencode(value) if isinstance(value, str) else bytes(value)


def _scan(text):
    """各行の条件コンパイルの条件と、行を含むK&R形式の関数の名前を返す(関数の外は None)。"""
    guards, owners, bodies = [], [], {}
    stack, current, opened = [], None, False
    for number, line in enumerate(text.split('\n'), 1):
        _directive(line, stack)
        guards.append(_guard(stack))
        match = _DEFINED.match(line)
        if current is None and match and not line.rstrip().endswith(';'):
            current, opened = match.group(1), False
            bodies[current] = []
        elif current is not None and not opened:
            if line.startswith('{'):
                opened = True
            elif line.strip() and not line.startswith((' ', '\t')):
                del bodies[current]
                current = None
        owners.append(current if opened else None)
        if current is not None and opened:
            bodies[current].append(number)
            if line.startswith('}'):
                current = None
    return guards, owners, bodies


def _guard(stack):
    return ' && '.join(stack)


def _directive(line, stack):
    """条件コンパイルの行を読み、いまの条件の並び(stack)を更新する。"""
    match = _PREPROCESSOR.match(line)
    if match is None:
        return
    word, rest = match.group(1), re.sub(r'\s+', ' ', re.sub(r'/\*.*?\*/', '', match.group(2))).strip()
    if word in ('if', 'ifdef', 'ifndef'):
        stack.append({'if': rest, 'ifdef': f'defined ({rest})', 'ifndef': f'!defined ({rest})'}[word])
    elif word == 'endif':
        if not stack:
            raise AssertionError('条件コンパイルの #endif が対になりません')
        stack.pop()
    elif word in ('else', 'elif') and stack:
        stack[-1] = f'!({stack[-1]})' + (f' && {rest}' if word == 'elif' else '')


def extract(text, spec=SPEC):
    """variables.c の本文から、環境の変数の名前を取り出す(名前・場所・行・呼出し・条件コンパイル)。

    取り出す所: 接頭辞と接尾辞の #define(BASH_FUNC_<名>%% など)・special_vars の表・初期化の関数
    (spec の functions)の中の、変数の名前を第1引数に取る呼出し。初期化の関数が、functions と helpers の
    どちらにも無い variables.c の関数を呼べば失敗にする(一覧の作り方が source に追いつかないことを見落とさない)。
    helpers は名前の文字列で変数を読まないことも確かめる。
    """
    lines = text.split('\n')
    guards, owners, bodies = _scan(text)
    functions, helpers = spec['source']['functions'], set(spec['source']['helpers'])
    for function in functions:
        if function not in bodies:
            raise AssertionError(f'variables.c に初期化の関数がありません: {function}')
    for helper in sorted(helpers & set(bodies)):
        for number in bodies[helper]:
            if _CALL.search(re.sub(r'/\*.*?\*/', '', lines[number - 1])):
                raise AssertionError(f'補助の関数が名前の文字列で変数を読みます: {helper}({number} 行)')
    entries, in_table = [], False
    for number, line in enumerate(lines, 1):
        guard, owner = guards[number - 1], owners[number - 1]
        define = _DEFINE.match(line)
        if define:
            entries.append(dict(name=f'{define.group(1)}_{define.group(2)}={define.group(3)}', where='define',
                                line=number, via='#define', guard=guard))
        if line.startswith('static struct name_and_function special_vars[]'):
            in_table = True
            continue
        if in_table:
            if line.startswith('};'):
                in_table = False
            for name, function in _TABLE.findall(line):
                entries.append(dict(name=name, where='special_vars', line=number, via=function, guard=guard))
            continue
        if owner not in functions:
            continue
        code = re.sub(r'/\*.*?\*/', '', line)
        for call, name in _CALL.findall(code):
            entries.append(dict(name=name, where=owner, line=number, via=call, guard=guard))
        for name in _STREQ.findall(code):
            entries.append(dict(name=name, where=owner, line=number, via='STREQ', guard=guard))
        for call in _CALLS.findall(code):
            if call in bodies and call not in functions and call not in helpers and not call.startswith('sv_'):
                raise AssertionError(f'{owner} が一覧の外の variables.c の関数を呼びます: {call}')
    return entries


def names_of(entries):
    """取り出した記録から、環境の変数の名前の集合を作る(#define の接頭辞と接尾辞は BASH_FUNC_<名>%% の形にする)。"""
    names, parts = set(), {}
    for entry in entries:
        if entry['where'] == 'define':
            key, value = entry['name'].split('=', 1)
            parts[key] = value
        else:
            names.add(entry['name'])
    for key, prefix in parts.items():
        if key.endswith('_PREFIX'):
            suffix = parts.get(key[:-len('_PREFIX')] + '_SUFFIX')
            if suffix is None:
                raise AssertionError(f'接頭辞に対になる接尾辞がありません: {key}')
            names.add(f'{prefix}<名>{suffix}')
    return names


def check_classification(spec=SPEC):
    """ソースの記録から作った名前の集合と、一覧(behavior)・外した名前(kept)が過不足なく一致することを確かめる。"""
    source = names_of(spec['source']['entries']) | {item['name'] for item in spec['outside_variables_c']}
    behavior = [item['name'] for item in spec['behavior']]
    kept = [item['name'] for item in spec['kept']]
    for label, values in (('behavior', behavior), ('kept', kept)):
        if len(values) != len(set(values)):
            raise AssertionError(f'{label} に同じ名前が重複しています')
    both = set(behavior) & set(kept)
    if both:
        raise AssertionError(f'一覧と外した名前の両方にあります: {sorted(both)}')
    classified = set(behavior) | set(kept)
    missing, extra = sorted(source - classified), sorted(classified - source)
    if missing:
        raise AssertionError(f'bash のソースにある名前が一覧で分類されていません(足りない): {missing}')
    if extra:
        raise AssertionError(f'bash のソースに無い名前が一覧にあります(余分): {extra}')
    outside = [item['name'] for item in spec['outside_variables_c']]
    if not set(outside) <= set(behavior):
        raise AssertionError('決定が挙げた variables.c の外の名前が一覧にありません')
    for item in spec['behavior']:
        if item['values'] not in CONDITIONS:
            raise AssertionError(f'一覧の値の条件が不明です: {item["name"]}: {item["values"]}')
        if not item.get('effect'):
            raise AssertionError(f'一覧の効果の説明がありません: {item["name"]}')
    for item in spec['kept']:
        if item.get('reason') not in spec['kept_reasons']:
            raise AssertionError(f'外した理由が不明です: {item["name"]}')
    return sorted(behavior)


def _compat_in_range(raw):
    """sv_shcompat が受け付ける値(d.d か dd で 31〜52)。範囲の外と空は警告か既定の値になる。"""
    match = re.fullmatch(rb'([0-9])\.?([0-9])', raw)
    if match is None:
        return False
    value = int(match.group(1)) * 10 + int(match.group(2))
    return SPEC['bash_compat_range'][0] <= value <= SPEC['bash_compat_range'][1]


def _integer(raw):
    """sv_xtracefd の strtol が全体を整数として読める値(前の空白・符号を許す)。ほかの値は起動時の警告だけ。"""
    return re.fullmatch(rb'[' + re.escape(_SPACE) + rb']*[+-]?[0-9]+', raw) is not None


CONDITIONS = {'all': lambda raw: True, 'compat-in-range': _compat_in_range, 'integer': _integer}
_EXACT = {item['name']: CONDITIONS[item['values']] for item in SPEC['behavior'] if '<名>' not in item['name']}
_PATTERNS = tuple((re.compile(re.escape(item['name'].split('<名>')[0].encode()) + rb'[\s\S]*'
                              + re.escape(item['name'].split('<名>')[1].encode())),
                   CONDITIONS[item['values']], item['name']) for item in SPEC['behavior'] if '<名>' in item['name'])
NAMES = tuple(sorted(item['name'] for item in SPEC['behavior']))


def matches(name, value):
    """環境の1つの変数が一覧に当たれば、一覧の名前を返す(当たらなければ None)。"""
    raw_name, raw_value = _bytes(name), _bytes(value)
    text = raw_name.decode('utf-8', 'surrogateescape')
    if text in _EXACT:
        return text if _EXACT[text](raw_value) else None
    for pattern, condition, label in _PATTERNS:
        if pattern.fullmatch(raw_name) and condition(raw_value):
            return label
    return None


def found(env):
    return sorted({hit for name, value in env.items() if (hit := matches(name, value)) is not None})


def check(env):
    """比較の入力の環境に一覧の変数があれば失敗にする。"""
    hits = sorted(_bytes(name).decode('utf-8', 'surrogateescape') for name, value in env.items()
                  if matches(name, value) is not None)
    if hits:
        raise AssertionError(f'bashが環境から読んで動きを変える変数を比較の入力に使いました: {hits}')


def check_allowed(names, patterns):
    """閉じた許可一覧(名前と型)が一覧の変数を入力に許していないことを確かめる。"""
    listed = set(_EXACT)
    overlap = sorted(set(names) & listed)
    overlap += sorted(name for name in listed for pattern in patterns if pattern.fullmatch(name))
    overlap += sorted(name for name in names for pattern, _, _ in _PATTERNS if pattern.fullmatch(_bytes(name)))
    if overlap:
        raise AssertionError(f'許可一覧がbashの動きを変える変数を入力に許しています: {sorted(set(overlap))}')


def samples():
    """一覧の各変数について、条件に当たる値の例(比較器の変異と単体検査に使う)。"""
    values = {'compat-in-range': '51', 'integer': '2', 'all': 's3d-behavior'}
    result = {}
    for item in SPEC['behavior']:
        name = item['name'].replace('<名>', 's3d_canary')
        result[name] = '() { :; }' if '<名>' in item['name'] else values[item['values']]
    return result


def compare_entries(entries, spec=SPEC):
    """ソースから取り出した記録と fixture の記録を比べる(足りない=ソースにだけある・余分=fixture にだけある)。"""
    recorded = spec['source']['entries']
    if entries == recorded:
        return
    got = {(e['name'], e['where'], e['via']) for e in entries}
    want = {(e['name'], e['where'], e['via']) for e in recorded}
    missing, extra = sorted(got - want), sorted(want - got)
    if missing or extra:
        raise AssertionError('variables.c から取り出した名前が fixture の記録と違います: '
                             f'足りない {missing} / 余分 {extra}')
    raise AssertionError('variables.c から取り出した記録の行・条件・並びが fixture と違います')


def verify_source(root, spec=SPEC):
    """展開したbashのソース(root)から一覧を作り直し、fixtureの記録と比べる。"""
    root = Path(root)
    for relative, digest in spec['source']['files'].items():
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if actual != digest:
            raise AssertionError(f'bashのソースの確認値が違います: {relative}: {actual}')
    variables = (root / 'variables.c').read_text(encoding='latin-1')
    compare_entries(extract(variables, spec), spec)
    low = re.search(r'^#define MIN_COMPAT_LEVEL (\d+)$', variables, re.M)
    version = re.search(r'^define\(bashvers, (\d)\.(\d)\)$', (root / 'configure.ac').read_text(encoding='latin-1'), re.M)
    if low is None or version is None or [int(low.group(1)), int(version.group(1) + version.group(2))] != spec['bash_compat_range']:
        raise AssertionError('BASH_COMPAT の範囲(MIN_COMPAT_LEVEL と版の段)がソースと違います')
    for item in spec['outside_variables_c']:
        text = (root / item['file']).read_text(encoding='latin-1')
        if f'{item["call"]} ("{item["name"]}")' not in text:
            raise AssertionError(f'決定が挙げた名前の読み込みがソースにありません: {item["name"]} ({item["file"]})')
    return check_classification(spec)


def report():
    return {'count': len(SPEC['behavior']), 'names': list(NAMES), 'bash': SPEC['source']['bash'],
            'variables_c_sha256': SPEC['source']['files']['variables.c']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='bash-5.2 に 001〜021 の修正を当てて展開した場所')
    parser.add_argument('--print-entries', action='store_true', help='取り出した記録を JSON で出す(fixture の作成用)')
    args = parser.parse_args(argv)
    if args.print_entries:
        print(json.dumps(extract((args.source / 'variables.c').read_text(encoding='latin-1')), ensure_ascii=False))
        return 0
    try:
        names = verify_source(args.source)
    except (AssertionError, OSError) as error:
        print(f'一覧の照合に失敗: {error}', file=sys.stderr)
        return 1
    print(f'一覧の照合: 合格 / 一覧 {len(names)} 件 / 外した名前 {len(SPEC["kept"])} 件 / '
          f'記録 {len(SPEC["source"]["entries"])} 行')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
