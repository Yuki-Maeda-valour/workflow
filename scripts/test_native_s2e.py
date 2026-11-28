"""S2Eの起動前の記録のマクロを展開する場所を、外部のRustのビルドなしで静的に確かめる(#470)。

`s2e_record_startup_state!`は、展開した側のcrateに`#[unsafe(link_section)]`の`static`を作る。
展開してよいのは、検査専用の入口`examples/s2e_contract.rs`の1か所と、platformの
`environment_guard.rs`の`#[cfg(test)]`のテストのモジュールの中だけ。製品の`dev-workflow`には入れない。
先例は`scripts/test_native_r4g.py`。比較器自身の検査(`native/ci/test_s2e.py`)は別に流れる。
"""
from pathlib import Path
import re
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'native'

ALLOWED_EXPANSIONS = ('native/crates/cli/examples/s2e_contract.rs',
                      'native/crates/platform/src/environment_guard.rs')
# 括弧の種類(`()`・`{}`・`[]`)と空白を問わず、マクロの呼出しを数える。
MACRO_CALL = re.compile(r'\bs2e_record_startup_state\s*!')
MACRO_ALIAS = re.compile(r'\bs2e_record_startup_state\s+as\b')
RECORD_FUNCTION = re.compile(r'\bcapture_inherited_sigpipe\b')


def _code_lines(path):
    """コメントを除いた行(行番号つき)。文字列の中の // は無い前提で、行の // 以降を捨てる。"""
    lines = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        code = line.split('//', 1)[0]
        if code.strip():
            lines.append((number, code))
    return lines


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
            if RECORD_FUNCTION.search(code) and name != ALLOWED_EXPANSIONS[1]:
                references.append((name, number, code.strip()))
    return calls, aliases, references


def check_macro_placement(root):
    """展開は入口の1か所とplatformのテストの中だけ。別名と、許可の外からの記録の関数の参照を拒む。"""
    calls, aliases, references = macro_findings(root)
    if [item[0] for item in calls] != list(ALLOWED_EXPANSIONS):
        raise AssertionError(f'展開の場所が違う: {calls}')
    if calls[0][2] != 'dev_workflow_platform::s2e_record_startup_state!();':
        raise AssertionError(f'入口の展開の形が違う: {calls[0]}')
    if calls[1][2] != 'crate::s2e_record_startup_state!();':
        raise AssertionError(f'platform の展開の形が違う: {calls[1]}')
    if aliases or references:
        raise AssertionError(f'別名の取り込みか、許可の外の参照: {aliases + references}')
    platform = (root / ALLOWED_EXPANSIONS[1]).read_text(encoding='utf-8').splitlines()
    tests_start = next(index for index, line in enumerate(platform, 1) if line.strip() == 'mod child_tests {')
    # テストのモジュールの終わり(行頭の `}`)より前でなければ、モジュールの外。
    tests_end = next(index for index, line in enumerate(platform, 1) if index > tests_start and line == '}')
    if platform[tests_start - 2].strip() != '#[cfg(test)]' or not tests_start < calls[1][1] < tests_end:
        raise AssertionError('platform の展開がテストのモジュールの外にある')


REGISTRATION_WORDS = re.compile(r'link_section|\.init_array|__mod_init_func')
MACRO_START = 'macro_rules! s2e_record_startup_state {'


def check_registration_inside_macro(root):
    """`link_section`・`.init_array`・`__mod_init_func`は、マクロの本体の中だけに置く(行コメントを除く)。

    マクロの外に登録を足す変異(別の関数を`main`より前に載せる形を含む)を拒む。
    """
    lines = (root / ALLOWED_EXPANSIONS[1]).read_text(encoding='utf-8').splitlines()
    start = next((i for i, line in enumerate(lines, 1) if line.strip() == MACRO_START), None)
    if start is None:
        raise AssertionError('マクロの定義が見つからない')
    # マクロの終わりは、定義の後の最初の行頭の `}`。
    end = next((i for i, line in enumerate(lines, 1) if i > start and line == '}'), None)
    if end is None:
        raise AssertionError('マクロの終わりが見つからない')
    outside = [(i, line.strip()) for i, line in enumerate(lines, 1)
               if not start <= i <= end and REGISTRATION_WORDS.search(line.split('//', 1)[0])]
    if outside:
        raise AssertionError(f'マクロの外に登録の語がある: {outside}')
    inside = ''.join(line.split('//', 1)[0] for line in lines[start - 1:end])
    for word in ('.init_array', '__mod_init_func'):
        if word not in inside:
            raise AssertionError(f'マクロの中に {word} が無い')


class S2EStartupMacroPlacement(unittest.TestCase):
    def test_registration_words_are_only_inside_the_macro_body(self):
        check_registration_inside_macro(ROOT)

    def test_registration_check_rejects_a_registration_outside_the_macro(self):
        mutations = {
            'same form outside the macro': (
                '\n#[used]\n#[unsafe(link_section = ".init_array")]\n'
                'static X: unsafe extern "C" fn() = capture_inherited_sigpipe;\n'),
            'another function before main': (
                '\n#[used]\n#[cfg_attr(target_vendor = "apple", unsafe(link_section = "__DATA,__mod_init_func"))]\n'
                'static Y: extern "C" fn() = other_startup;\n'),
        }
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp)
            (copy / ALLOWED_EXPANSIONS[1]).parent.mkdir(parents=True)
            original = (ROOT / ALLOWED_EXPANSIONS[1]).read_text(encoding='utf-8')
            path = copy / ALLOWED_EXPANSIONS[1]
            path.write_text(original, encoding='utf-8')
            check_registration_inside_macro(copy)
            for label, text in mutations.items():
                with self.subTest(mutation=label):
                    path.write_text(original + text, encoding='utf-8')
                    with self.assertRaises(AssertionError):
                        check_registration_inside_macro(copy)

    def test_startup_macro_is_expanded_only_by_the_contract_entry_and_platform_tests(self):
        check_macro_placement(ROOT)

    def test_contract_entry_keeps_forbid_unsafe_and_has_no_link_section(self):
        entry = (ROOT / ALLOWED_EXPANSIONS[0]).read_text(encoding='utf-8')
        self.assertTrue(entry.startswith('#![forbid(unsafe_code)]\n'))
        for path in [ROOT / ALLOWED_EXPANSIONS[0],
                     *sorted((NATIVE / 'crates/cli/src/commands/environment_guard').glob('*.rs'))]:
            for number, code in _code_lines(path):
                with self.subTest(path=path.name, line=number):
                    self.assertNotRegex(code, r'\bunsafe\b|link_section|no_mangle|export_name')

    def test_macro_check_rejects_other_brackets_aliases_and_direct_references(self):
        mutations = {
            'braces in main.rs': ('native/crates/cli/src/main.rs',
                                  '\ndev_workflow_platform::s2e_record_startup_state! {}\n'),
            'brackets in a library': ('native/crates/core/src/lib.rs',
                                      '\ndev_workflow_platform::s2e_record_startup_state ![];\n'),
            'space before the bang in main.rs': ('native/crates/cli/src/main.rs',
                                                 '\ndev_workflow_platform::s2e_record_startup_state !();\n'),
            'alias import': ('native/crates/cli/src/main.rs',
                             '\nuse dev_workflow_platform::s2e_record_startup_state as record;\n'),
            'direct reference': ('native/crates/cli/src/main.rs',
                                 '\nconst _: extern "C" fn() = dev_workflow_platform::environment_guard::capture_inherited_sigpipe;\n'),
            'second expansion in the platform': ('native/crates/platform/src/lib.rs',
                                                 '\ncrate::s2e_record_startup_state!();\n'),
        }
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp)
            shutil.copytree(ROOT / 'native/crates', copy / 'native/crates',
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

    def test_macro_check_rejects_a_platform_expansion_outside_the_test_module(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp)
            shutil.copytree(ROOT / 'native/crates', copy / 'native/crates',
                            ignore=shutil.ignore_patterns('target', '*.lock'))
            path = copy / ALLOWED_EXPANSIONS[1]
            text = path.read_text(encoding='utf-8')
            moved = text.replace('\n    crate::s2e_record_startup_state!();\n', '\n', 1)
            self.assertNotEqual(text, moved)
            path.write_text(moved + '\ncrate::s2e_record_startup_state!();\n', encoding='utf-8')
            with self.assertRaises(AssertionError):
                check_macro_placement(copy)


if __name__ == '__main__':
    unittest.main()
