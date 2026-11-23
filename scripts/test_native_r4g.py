"""R4G比較器の自己検査を、外部の Rust のビルドなしで全回帰テストへつなぐ(#479)。

native/ci/test_r4g.py の Comparator(本家の Python も strace も使わない検査)を流す。
あわせて、起動前の記録のマクロを展開する場所と、unsafe を置く場所を静的に確かめる。
"""
import importlib.util
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'native'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_COMPARATOR = _load(NATIVE / 'ci/test_r4g.py', 'native_r4g_comparator_tests')


class R4GComparatorSelfTest(_COMPARATOR.Comparator):
    """比較器の自己検査(native/ci/test_r4g.py の Comparator と同じメソッド)。"""


def _code_lines(path):
    """コメントを除いた行(行番号つき)。文字列の中の // は無い前提で、行の // 以降を捨てる。"""
    lines = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        code = line.split('//', 1)[0]
        if code.strip():
            lines.append((number, code))
    return lines


ALLOWED_EXPANSIONS = ('native/crates/cli/examples/r4g_contract.rs', 'native/crates/platform/src/r4g/startup.rs')
# 括弧の種類(`()`・`{}`・`[]`)と空白を問わず、マクロの呼出しを数える。
MACRO_CALL = re.compile(r'\br4g_record_startup_state\s*!')
MACRO_ALIAS = re.compile(r'\br4g_record_startup_state\s+as\b')
RECORD_FUNCTION = re.compile(r'\brecord_before_std\b')


def macro_findings(root):
    """`root`の下の全.rs(target を除く)から、展開・別名の取り込み・記録の関数の参照を集める。"""
    calls, aliases, references = [], [], []
    native = root / 'native'
    for path in sorted(native.rglob('*.rs')):
        if 'target' in path.relative_to(native).parts:
            continue
        name = path.relative_to(root).as_posix()
        for number, code in _code_lines(path):
            if MACRO_CALL.search(code):
                calls.append((name, number, code.strip()))
            if MACRO_ALIAS.search(code):
                aliases.append((name, number, code.strip()))
            if RECORD_FUNCTION.search(code) and name not in ALLOWED_EXPANSIONS:
                references.append((name, number, code.strip()))
    return calls, aliases, references


def check_macro_placement(root):
    """展開は入口の1か所とplatformのテストの中だけ。別名と、許可の外からの記録の関数の参照を拒む。"""
    calls, aliases, references = macro_findings(root)
    if [item[0] for item in calls] != list(ALLOWED_EXPANSIONS):
        raise AssertionError(f'展開の場所が違う: {calls}')
    if calls[0][2] != 'dev_workflow_platform::r4g_record_startup_state!();':
        raise AssertionError(f'入口の展開の形が違う: {calls[0]}')
    if aliases or references:
        raise AssertionError(f'別名の取り込みか、許可の外の参照: {aliases + references}')
    startup = (root / ALLOWED_EXPANSIONS[1]).read_text(encoding='utf-8').splitlines()
    tests_start = next(index for index, line in enumerate(startup, 1) if line.strip() == 'mod tests {')
    if startup[tests_start - 2].strip() != '#[cfg(test)]' or calls[1][1] <= tests_start:
        raise AssertionError('platform の展開がテストのモジュールの外にある')


class R4GStaticPlacement(unittest.TestCase):
    def test_startup_macro_is_expanded_only_by_the_contract_entry_and_platform_tests(self):
        check_macro_placement(ROOT)

    def test_macro_check_rejects_other_brackets_aliases_and_direct_references(self):
        import shutil
        import tempfile
        mutations = {
            'braces in main.rs': ('native/crates/cli/src/main.rs', '\ndev_workflow_platform::r4g_record_startup_state! {}\n'),
            'brackets in a library': ('native/crates/core/src/lib.rs', '\ndev_workflow_platform::r4g_record_startup_state ![];\n'),
            'alias import': ('native/crates/cli/src/main.rs',
                             '\nuse dev_workflow_platform::r4g_record_startup_state as record;\n'),
            'direct reference': ('native/crates/cli/src/main.rs',
                                 '\nconst _: unsafe extern "C" fn() = dev_workflow_platform::r4g::startup::record_before_std;\n'),
        }
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp)
            for relative in ['native/crates']:
                shutil.copytree(ROOT / relative, copy / relative,
                                ignore=shutil.ignore_patterns('target', '*.lock'))
            check_macro_placement(copy)
            for label, (relative, text) in mutations.items():
                with self.subTest(mutation=label):
                    path = copy / relative
                    original = path.read_text(encoding='utf-8')
                    path.write_text(original + text, encoding='utf-8')
                    try:
                        with self.assertRaises(AssertionError):
                            check_macro_placement(copy)
                    finally:
                        path.write_text(original, encoding='utf-8')

    def test_unsafe_and_link_sections_stay_in_the_platform_r4g_module(self):
        checked = [NATIVE / 'crates/cli/examples/r4g_contract.rs',
                   *sorted((NATIVE / 'crates/cli/src/commands/r4g_support').glob('*.rs')),
                   *sorted((NATIVE / 'crates/cli/src/commands/pending_implementation').glob('*.rs')),
                   *sorted((NATIVE / 'crates/cli/src/commands/reviews_dir').glob('*.rs')),
                   *sorted((NATIVE / 'crates/cli/src/commands/implementation_git').glob('*.rs'))]
        self.assertGreater(len(checked), 5)
        self.assertTrue(any(path.parent.name == 'implementation_git' for path in checked))
        for path in checked:
            for number, code in _code_lines(path):
                with self.subTest(path=path.name, line=number):
                    self.assertNotRegex(code, r'\bunsafe\b|link_section|no_mangle|export_name')
        contract = (NATIVE / 'crates/cli/examples/r4g_contract.rs').read_text(encoding='utf-8')
        self.assertTrue(contract.startswith('#![forbid(unsafe_code)]\n'))

    def test_subshell_fork_is_called_only_from_the_single_threaded_reviews_dir_shell(self):
        # bash_fork_subshell は1つのスレッドのプロセスだけが使う包み(呼出側がスレッドを作らないことが安全の
        # 条件)。platform の外で呼ぶのは reviews-dir のシェルだけで、そこはスレッドを作らない。
        callers = []
        for path in sorted((NATIVE / 'crates').rglob('*.rs')):
            if path.is_relative_to(NATIVE / 'crates/platform'):
                continue
            for number, code in _code_lines(path):
                if 'bash_fork_subshell' in code:
                    callers.append(path.relative_to(NATIVE).as_posix())
        self.assertEqual(sorted(set(callers)), ['crates/cli/src/commands/reviews_dir/shell.rs'])
        # reviews-dir の経路(起動前の記録・入口の example・共通の部品・reviews-dir・platform の r4g)の、検査でない
        # コードにスレッドを作る字面が無いこと(reviews_dir::main までと、その中でスレッドを作らない)。
        # 検査のコード(各ファイルの末尾の `#[cfg(test)]` の `mod tests` から後)は、検査の枠のスレッドで動くので除く。
        route = [NATIVE / 'crates/cli/examples/r4g_contract.rs',
                 *sorted((NATIVE / 'crates/cli/src/commands/r4g_support').glob('*.rs')),
                 *sorted((NATIVE / 'crates/cli/src/commands/reviews_dir').glob('*.rs')),
                 *sorted((NATIVE / 'crates/platform/src/r4g').glob('*.rs'))]
        thread = re.compile(r'\bthread::(spawn|Builder|scope)\b|std::thread\b|pthread_create|rayon|tokio')
        for path in route:
            text = re.split(r'#\[cfg\(test\)\]\s*\nmod tests\b', path.read_text(encoding='utf-8'), maxsplit=1)[0]
            for number, line in enumerate(text.splitlines(), 1):
                code = line.split('//', 1)[0]
                with self.subTest(path=path.name, line=number):
                    self.assertNotRegex(code, thread)
        # 対照: 字面の確かめは、検査のコードのスレッドを見つける(除く範囲を広げすぎていない)。
        tests = re.split(r'#\[cfg\(test\)\]\s*\nmod tests\b',
                         (NATIVE / 'crates/platform/src/r4g/fs.rs').read_text(encoding='utf-8'), maxsplit=1)[1]
        self.assertRegex(tests, thread)


if __name__ == '__main__':
    unittest.main()
