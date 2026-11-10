#!/usr/bin/env python3
"""R4Sの旧版・固定期待・Rustを別々に照合する。製品の入口ではない。"""
import argparse
import collections
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'native/fixtures/r4s'
SOURCE_SHA256 = '48893743a678f43fa46ad585abf2a8854c5abb444570f2c2bfa2c617429c6676'
FIXTURE_HASHES = {
    'coverage.json': 'afaa3c6ac27326c4517d61d5d2a9b8a6b3baddc48ba86f30568dfc50b6c64c4d',
    'unicode13.json': '83d260f157552f31aca405bd5afafda6d821870382e1216878eb4bc973683069',
    'mutations.json': '1a8e9f7dad00b79a8af81b2df8f1e6d26026c5300a28eba7b09b39490623d328',
    'approved-differences.json': '49f8bc7916e64bc6cd1a9c0204ceabcc9d499fafccbfddf231f44ea7f7046b18',
    'legacy-rejections.json': 'ba92f03c152a333fa509bd422887ec501873a9e641b8b5ef6b3cb65f9a8de6a1',
    'regressions.json': 'd515204b4140e3b9cdfe2d021a079838ee441740dfdf8d0cd2987d7321e7708d',
    'manager-regressions.json': 'fe9c4dd05d88120be261b89e35663e7c38ed2a91a543bf2d43e32f11ec220a25',
}
ALLOWED_ID = 'constructor/merge_chain/1100'
REASONS = {'secret_paths を安全に読めない', 'secret_paths が文字列配列でない',
           'secret_paths に不正な glob がある'}
sys.path.insert(0, str(ROOT / 'scripts'))
from support_native import run_command, snapshot_tree


class UnexpectedLegacyFailure(RuntimeError):
    """固定した拒否原因と違う旧版の失敗。正しい拒否へ変換しない。"""


def exception_record(error):
    """型、本文、発生した関数、連鎖を記録する。絶対パスは記録しない。"""
    if error is None:
        return None
    tail = error.__traceback__
    while tail is not None and tail.tb_next is not None:
        tail = tail.tb_next
    origin = None if tail is None else [tail.tb_frame.f_globals.get('__name__'), tail.tb_frame.f_code.co_name]
    if tail is not None and Path(tail.tb_frame.f_code.co_filename).resolve() == Path(__file__).resolve():
        origin[0] = 'check_r4s'
    message = str(error)
    if type(error) is RecursionError:
        # 同じ構築不能入力でも呼出しの段数でCPythonの末尾句が変わる。
        # 型と発生関数と入力IDは別に完全照合する。任意のRuntimeErrorは許さない。
        if message not in ('maximum recursion depth exceeded',
                           'maximum recursion depth exceeded while calling a Python object',
                           'maximum recursion depth exceeded in comparison'):
            raise UnexpectedLegacyFailure('未知の再帰例外') from error
        message = 'maximum recursion depth exceeded (CPython context-dependent suffix)'
    cause = error.__cause__
    context = None if error.__suppress_context__ else error.__context__
    return {'type': type(error).__module__ + '.' + type(error).__qualname__,
            'message': message, 'origin': origin,
            'cause': exception_record(cause), 'context': exception_record(context)}


def verify_rejection(error, expected):
    if expected is None or exception_record(error) != expected:
        raise UnexpectedLegacyFailure('旧版の拒否原因が固定入力の既知原因と違う') from error


def rejection_records():
    path = FIXTURES / 'legacy-rejections.json'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FIXTURE_HASHES['legacy-rejections.json']:
        raise ValueError('旧版の拒否原因の固定記録が変わった')
    return decode(raw)['cases']


def load_regressions(filename='regressions.json'):
    expected_counts = {'regressions.json': 109, 'manager-regressions.json': 28}
    if filename not in expected_counts:
        raise ValueError('未定義の追加比較データ')
    count = expected_counts[filename]
    raw = (FIXTURES / filename).read_bytes()
    if hashlib.sha256(raw).hexdigest() != FIXTURE_HASHES[filename]:
        raise ValueError('レビュー追加入力の固定記録が変わった')
    data = decode(raw)
    rows = data['cases']
    if (data['profile_count'] != count or data['graph_count'] != count or len(rows) != count * 2
            or len({row['id'] for row in rows}) != count * 2
            or collections.Counter(row['purpose'] for row in rows) != {'loop-profile': count, 'node-graph': count}):
        raise ValueError('レビュー追加入力の件数かIDが違う')
    for index in range(0, len(rows), 2):
        profile, graph = rows[index:index + 2]
        if (profile['source_id'] != graph['source_id'] or profile['input_hex'] != graph['input_hex']
                or profile['context'] != graph['context']):
            raise ValueError('追加profileとノードへ同じ入力・整数設定を渡していない')
    for row in rows:
        validate_result(row['expected'], row['purpose'])
    return rows


def encode(value):
    return (json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n').encode()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSONのキーが重複している')
        result[key] = value
    return result


def decode(raw):
    def invalid_constant(value):
        raise ValueError('JSONの非有限数を受理しない')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError('観測のキーが不足、余分、または型違い')


def integer(value, low=0, high=0x10FFFF):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('整数の型か値域が違う')


def codepoints(value):
    if type(value) is not list:
        raise ValueError('コードポイントが配列でない')
    for point in value:
        integer(point)


def text(points):
    codepoints(points)
    return ''.join(map(chr, points))


def typed(value):
    kind = value['kind']
    if kind == 'text':
        keys(value, 'kind codepoints')
        return text(value['codepoints'])
    if kind == 'list':
        keys(value, 'kind items')
        return [typed(item) for item in value['items']]
    if kind == 'null':
        keys(value, 'kind')
        return None
    if kind == 'bool':
        keys(value, 'kind value')
        if type(value['value']) is not bool:
            raise ValueError('bool入力の型が違う')
        return value['value']
    if kind == 'int':
        keys(value, 'kind decimal')
        return int(value['decimal'])
    if kind == 'map':
        keys(value, 'kind items')
        return {typed(k): typed(v) for k, v in value['items']}
    raise ValueError('未定義の入力型')


def validate_result(value, purpose):
    if purpose == 'matcher':
        keys(value, 'matched')
        if type(value['matched']) is not bool:
            raise ValueError('照合結果がboolでない')
        return
    if type(value) is not dict or type(value.get('accepted')) is not bool:
        raise ValueError('受理結果がboolでない')
    if purpose == 'node-graph':
        if not value['accepted']:
            keys(value, 'accepted')
            return
        keys(value, 'accepted nodes')
        nodes = value['nodes']
        if type(nodes) is not list:
            raise ValueError('ノードが配列でない')
        for node in nodes:
            keys(node, 'kind tag value')
            if type(node['tag']) is not str or not node['tag']:
                raise ValueError('ノードtagが不正')
            if node['kind'] == 'scalar':
                codepoints(node['value'])
            elif node['kind'] == 'sequence':
                if type(node['value']) is not list:
                    raise ValueError('sequenceの型が違う')
                for ref in node['value']:
                    integer(ref, 0, len(nodes) - 1)
            elif node['kind'] == 'mapping':
                if type(node['value']) is not list:
                    raise ValueError('mappingの型が違う')
                for pair in node['value']:
                    if type(pair) is not list or len(pair) != 2:
                        raise ValueError('mappingの対が不正')
                    for ref in pair:
                        integer(ref, 0, len(nodes) - 1)
            else:
                raise ValueError('未定義のノード種別')
        return
    if purpose not in ('loop-profile', 'canonical'):
        raise ValueError('未定義の観測用途')
    if value['accepted']:
        keys(value, 'accepted patterns')
        if type(value['patterns']) is not list:
            raise ValueError('指定列が配列でない')
        for pattern in value['patterns']:
            codepoints(pattern)
    else:
        keys(value, 'accepted reason')
        if type(value['reason']) is not str or value['reason'] not in REASONS:
            raise ValueError('未定義の拒否理由')


def observation(run, rows):
    if run.returncode != 0 or run.stderr or run.timed_out:
        raise ValueError(f'接続失敗: exit={run.returncode}, timeout={run.timed_out}, stderr={run.stderr[:2000]!r}')
    observed = decode(run.stdout)
    if type(observed) is not list or len(observed) != len(rows) or not observed:
        raise ValueError('未実行、または観測件数が違う')
    seen = set()
    for actual, row in zip(observed, rows):
        keys(actual, 'id result')
        identifier = actual['id']
        if type(identifier) is not str or not identifier or identifier in seen or identifier != row['id']:
            raise ValueError('観測IDが空、重複、未実行、または順序違い')
        seen.add(identifier)
        validate_result(actual['result'], row['purpose'])
    return observed


def verify_pair(legacy, native, rows, differences):
    old = observation(legacy, rows)
    new = observation(native, rows)
    for left, right, row in zip(old, new, rows):
        wanted = row['expected']
        allowed = differences.get(row['id'])
        native_wanted = allowed['rust_expected'] if allowed else wanted
        if left['result'] != wanted:
            raise ValueError(f"{row['id']}: 旧版が固定期待と違う: {left['result']!r}")
        if right['result'] != native_wanted:
            raise ValueError(f"{row['id']}: Rustが固定期待と違う: {right['result']!r}; 期待={native_wanted!r}")
        if left != right and not (allowed and left['result'] == allowed['legacy_expected'] and right['result'] == allowed['rust_expected']):
            raise ValueError(f"{row['id']}: 未承認の旧・Rust差")


def require_environment():
    import yaml
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise RuntimeError('Python3.10系とroot以外の利用者が必要です')
    if unicodedata.unidata_version != '13.0.0' or yaml.__version__ != '6.0.3':
        raise RuntimeError('Unicode13.0.0とrequirements-dev.txtのPyYAML6.0.3が必要です')
    return yaml


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def legacy_modules():
    return (load_module('r4s_legacy_loop', 'plugins/dev-workflow/skills/ship-task/scripts/loop-state.py'),
            load_module('r4s_legacy_profiles', 'plugins/dev-workflow/skills/do-task/scripts/secret-profiles.py'))


def load_data():
    raw = (FIXTURES / 'cases.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError('採用元の全項目が変わった')
    for filename, digest in FIXTURE_HASHES.items():
        if hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() != digest:
            raise ValueError('固定した対応・同値・差・故障の記録が変わった: ' + filename)
    data = decode(raw)
    rows = data['cases'] + data['graph_cases']
    if len(data['cases']) != 571 or len(data['graph_cases']) != 63 or len(data['json_boundaries']) != 4:
        raise ValueError('固定入力件数が違う')
    if collections.Counter(row['purpose'] for row in rows) != {'loop-profile': 432, 'canonical': 79, 'matcher': 60, 'node-graph': 63}:
        raise ValueError('用途別件数が違う')
    if len({row['id'] for row in rows}) != 634:
        raise ValueError('固定IDが重複している')
    for row in rows:
        validate_result(row['expected'], row['purpose'])
    for relative, digest in data['legacy_sources'].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest:
            raise ValueError('旧比較元がない、または固定した版でない: ' + relative)
    approved = decode((FIXTURES / 'approved-differences.json').read_bytes())
    if len(approved['cases']) != 1 or approved['cases'][0]['id'] != ALLOWED_ID:
        raise ValueError('承認差の対象が変わった')
    allowed = approved['cases'][0]
    original = next(row for row in rows if row['id'] == ALLOWED_ID)
    defaults = [list(map(ord, value)) for value in ('.env', '.env.*', '.dev.vars')]
    if (allowed['legacy_expected'] != original['expected'] or allowed['rust_expected'] != {'accepted': True, 'patterns': defaults}
            or approved['source_issue'] != 462 or approved['approved_date'] != '2026-11-09' or not allowed['reason']):
        raise ValueError('承認差の理由か双方の期待が違う')
    coverage = decode((FIXTURES / 'coverage.json').read_bytes())
    verify_coverage(coverage, rows)
    rejections = rejection_records()
    all_rows = rows + load_regressions() + load_regressions('manager-regressions.json')
    required_ids = {row['id'] for row in all_rows}
    required_ids.update('profiles/' + row['id'] for row in data['cases'] if 'profiles_boundary_expected' in row)
    required_ids.update('host/' + row['id'] for row in data['legacy_only']['host'])
    if set(rejections) != required_ids:
        raise ValueError('既知拒否原因の固定記録に不足か余分なIDがある')
    for row in all_rows:
        if (row['expected'].get('accepted') is False) != (rejections[row['id']] is not None):
            raise ValueError('固定した受理/拒否と原因の有無が違う')
    return data, {ALLOWED_ID: allowed}


def verify_coverage(coverage, rows):
    ids = {row['id'] for row in rows}
    if coverage['table_count'] != 15 or coverage['total_rows'] != 209 or coverage['fixture_sha256'] != SOURCE_SHA256:
        raise ValueError('出典表の件数か採用元が違う')
    if [table['number'] for table in coverage['tables']] != list(range(1, 16)):
        raise ValueError('出典表が不足、重複、順序違い')
    expected = {(table['number'], index + 1): source for table in coverage['tables']
                for index, source in enumerate(table['rows'][2:])}
    actual = {}
    for row in coverage['row_links']:
        key = (row['table'], row['row'])
        if key in actual or key not in expected or row['source'] != expected[key]:
            raise ValueError('出典行が不足、重複、内容違い')
        actual[key] = row['source']
        if any(identifier not in ids for identifier in row['case_ids']):
            raise ValueError('出典行が存在しない比較IDを指す')
        if not row['case_ids'] and not row['out_of_scope_reason']:
            raise ValueError('対象外の出典行に理由がない')
    if actual != expected or len(actual) != 209:
        raise ValueError('元表の全209行が残っていない')


def request(row):
    result = {'id': row['id'], 'purpose': row['purpose']}
    if row['purpose'] in ('loop-profile', 'node-graph'):
        result.update(input_hex=row['input_hex'], context=row.get('context', {'integer_max_str_digits': 4300}))
    else:
        result['input'] = row['input']
    return result


def file_record(path):
    info = path.lstat()
    return [stat.S_IMODE(info.st_mode), info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, hashlib.sha256(path.read_bytes()).hexdigest()]


def tree_record(path):
    result = snapshot_tree(path)
    for name, value in result.items():
        info = (path / name).lstat()
        value['identity'] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    return result


def prepare_inputs(root, rows):
    root.mkdir(parents=True)
    for index, row in enumerate(rows):
        if row['purpose'] == 'loop-profile':
            path = root / str(index) / '.claude/project-profile.yml'
            path.parent.mkdir(parents=True)
            path.write_bytes(bytes.fromhex(row['input_hex']))


def old_result(loop, callback, rejection=None):
    try:
        value = callback()
    except loop.Stop as error:
        if str(error) not in REASONS:
            raise RuntimeError('旧版の想定外失敗') from error
        verify_rejection(error, rejection)
        return {'accepted': False, 'reason': str(error)}
    if type(value) is bool:
        return {'matched': value}
    return {'accepted': True, 'patterns': [list(map(ord, item)) for item in value]}


def compose_graph(raw, rejection=None):
    import yaml
    try:
        root = yaml.compose(raw.decode('utf-8'), Loader=yaml.SafeLoader)
    except (yaml.YAMLError, UnicodeDecodeError, ValueError) as error:
        verify_rejection(error, rejection)
        return {'accepted': False}
    if root is None:
        return {'accepted': True, 'nodes': []}
    pending, indexes, nodes = [root], {id(root): 0}, []
    def reference(node):
        if id(node) not in indexes:
            indexes[id(node)] = len(pending)
            pending.append(node)
        return indexes[id(node)]
    for node in pending:
        if isinstance(node, yaml.ScalarNode):
            kind, value = 'scalar', list(map(ord, node.value))
        elif isinstance(node, yaml.SequenceNode):
            kind, value = 'sequence', [reference(child) for child in node.value]
        elif isinstance(node, yaml.MappingNode):
            kind, value = 'mapping', [[reference(k), reference(v)] for k, v in node.value]
        else:
            raise ValueError('未知の旧ノード型')
        nodes.append({'kind': kind, 'tag': node.tag, 'value': value})
    return {'accepted': True, 'nodes': nodes}


def legacy_driver(root, requests):
    require_environment()
    loop, _ = legacy_modules()
    rejections = rejection_records()
    results = []
    old_limit = sys.get_int_max_str_digits()
    try:
        for index, row in enumerate(requests):
            purpose = row['purpose']
            sys.set_int_max_str_digits(row.get('context', {}).get('integer_max_str_digits', 4300))
            if purpose == 'loop-profile':
                path = root / str(index) / '.claude/project-profile.yml'
                if path.read_bytes() != bytes.fromhex(row['input_hex']):
                    raise ValueError('旧版へ渡すファイルと固定bytesが違う')
                before = file_record(path)
                result = old_result(loop, lambda: loop.profile_patterns(str(path.parent.parent), loop.Budget(10000, 1073741824, 67108864, 30)), rejections.get(row['id']))
                if file_record(path) != before:
                    raise ValueError('旧版が入力を変更した')
            elif purpose == 'canonical':
                result = old_result(loop, lambda: loop.canonical_secret_patterns(typed(row['input'])), rejections.get(row['id']))
            elif purpose == 'matcher':
                result = old_result(loop, lambda: loop.secret_path(text(row['input']['rel']), [text(p) for p in row['input']['patterns']]))
            elif purpose == 'node-graph':
                result = compose_graph(bytes.fromhex(row['input_hex']), rejections.get(row['id']))
            else:
                raise ValueError('未定義の旧比較用途')
            validate_result(result, purpose)
            results.append({'id': row['id'], 'result': result})
    finally:
        sys.set_int_max_str_digits(old_limit)
    return results


def unicode_rows():
    import _sre
    import sre_compile
    fixed = decode((FIXTURES / 'unicode13.json').read_bytes())
    actual = []
    for point in range(0x110000):
        lower = _sre.unicode_tolower(point)
        folded = min((lower, *sre_compile._ignorecase_fixes.get(lower, ())))
        if folded != point:
            actual.append([point, folded])
    if actual != fixed['pairs'] or len(actual) != 1419 or fixed['unicode'] != '13.0.0':
        raise ValueError('Unicode13の全域再計算が固定表と違う')
    groups = collections.defaultdict(set)
    for point, folded in fixed['pairs']:
        groups[folded].update((point, folded))
    rows = []
    for group in groups.values():
        for pattern in sorted(group):
            for rel in sorted(group):
                rows.append({'id': f'unicode/{pattern:x}/{rel:x}', 'purpose': 'matcher',
                             'input': {'rel': [rel], 'patterns': [[pattern]]}, 'expected': {'matched': True}})
    return rows


def boundary_checks(data, root):
    """他用途の固定結果だけを再確認する。CLI・Git・状態走査は起動しない。"""
    loop, profiles = legacy_modules()
    rejections = rejection_records()
    count = 0
    for row in data['cases']:
        if 'profiles_boundary_expected' not in row:
            continue
        try:
            value = profiles.paths_from_yaml(bytes.fromhex(row['input_hex']), '観測')
            actual = {'accepted': True, 'patterns': [list(map(ord, item)) for item in value]}
        except profiles.ProfileError as error:
            verify_rejection(error, rejections.get('profiles/' + row['id']))
            actual = {'accepted': False, 'exception': 'ProfileError', 'reason': str(error)}
        if actual != row['profiles_boundary_expected']:
            raise ValueError(row['id'] + ': 旧用途の固定期待が違う')
        count += 1
    host = load_module('r4s_legacy_host', 'plugins/dev-workflow/skills/ship-task/scripts/host-check.py')
    for row in data['legacy_only']['host']:
        try:
            # #454のhost表は改行を字面の\\nとして保存した6例である。
            value = host.frontmatter_grants(row['input'].replace('\\n', '\n').encode())
            actual = {'accepted': True, 'value': value}
            if value is not None:
                actual['value_codepoints'] = [[f'U+{ord(c):04X}' for c in item] for item in value]
        except host.Unclear as error:
            verify_rejection(error, rejections.get('host/' + row['id']))
            actual = {'accepted': False, 'exception': 'Unclear', 'message': str(error),
                      'cause': type(error.__cause__).__name__ if error.__cause__ else None}
        if actual != row['result']:
            raise ValueError(row['id'] + ': 旧hostの固定期待が違う')
        count += 1
    extras = []
    for index, row in enumerate(data['json_boundaries']):
        profile = root / str(index) / '.claude/project-profile.yml'
        profile.parent.mkdir(parents=True)
        profile.write_bytes(bytes.fromhex(row['yaml_hex']))
        before_file = file_record(profile)
        value = loop.profile_patterns(str(profile.parent.parent), loop.Budget(10000, 1073741824, 67108864, 30))[-1]
        output = root / f'state-{index}.json'
        loop.write_state(str(output), {'patterns': [value]})
        raw = output.read_bytes()
        after = decode(raw)['patterns'][0]
        if (list(map(ord, value)) != row['before'] or raw.hex() != row['saved_json_hex'] or list(map(ord, after)) != row['after']
                or loop.secret_path(text(row['rel']), [value]) != row['before_match']
                or loop.secret_path(text(row['rel']), [after]) != row['after_match'] or file_record(profile) != before_file):
            raise ValueError(row['id'] + ': JSON保存前後の固定期待が違う')
        for side in ('before', 'after'):
            extras.append({'id': row['id'] + '/' + side, 'purpose': 'matcher',
                           'input': {'rel': row['rel'], 'patterns': [row[side]]},
                           'expected': {'matched': row[side + '_match']}})
    return count, extras


def require_bridge(path):
    if not path:
        raise ValueError('今回作ったRust接続が必要です')
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError('Rust接続が実行可能な通常ファイルでない')
    return resolved


def compare_rows(bridge, rows, root, differences):
    prepare_inputs(root, rows)
    before = tree_record(root)
    raw = encode([request(row) for row in rows])
    old = run_command([sys.executable, '-B', str(Path(__file__).resolve()), '--legacy', str(root)], cwd=root, input=raw, timeout=120)
    if tree_record(root) != before:
        raise ValueError('旧接続が入力の内容・権限・実体・時刻を変えた')
    new = run_command([str(bridge), '--r4s-fixture'], cwd=root, input=raw, timeout=120)
    if tree_record(root) != before:
        raise ValueError('Rust接続が入力の内容・権限・実体・時刻を変えた')
    verify_pair(old, new, rows, differences)


def run_selftests():
    module = load_module('r4s_comparator_tests', 'native/ci/test_r4s.py')
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
        raise RuntimeError('比較器の自己検査が失敗した')
    return suite.countTestCases()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bridge')
    parser.add_argument('--legacy', type=Path)
    args = parser.parse_args()
    require_environment()
    if args.legacy is not None:
        sys.stdout.buffer.write(encode(legacy_driver(args.legacy, decode(sys.stdin.buffer.read()))))
        return 0
    bridge = require_bridge(args.bridge)
    data, differences = load_data()
    regressions = load_regressions()
    manager_regressions = load_regressions('manager-regressions.json')
    selftests = run_selftests()
    unicode = unicode_rows()
    with tempfile.TemporaryDirectory(prefix='r4s-comparison-') as temporary:
        root = Path(temporary).resolve()
        for index in range(4):
            root /= f'physical-{index}-' + 'x' * 90
        root.mkdir(parents=True)
        if len(str(root)) < 400 or root != root.resolve():
            raise ValueError('400文字以上の物理パスになっていない')
        boundaries, json_rows = boundary_checks(data, root / 'boundary')
        compare_rows(bridge, data['cases'] + data['graph_cases'], root / 'fixed', differences)
        compare_rows(bridge, regressions, root / 'regressions', {})
        compare_rows(bridge, manager_regressions, root / 'manager-regressions', {})
        compare_rows(bridge, unicode + json_rows, root / 'unicode-json', {})
    print(f'R4S: 元固定634件(純粋571・ノード63)、レビュー追加{len(regressions)}件(profile109・ノード109)、管理確認追加{len(manager_regressions)}件(profile28・ノード28)、JSON境界4件、旧用途{boundaries}件、Unicode全0x110000と照合{len(unicode)}件、自己検査{selftests}件(実装故障10種を含む)が成功')
    return 0


if __name__ == '__main__':
    sys.exit(main())
