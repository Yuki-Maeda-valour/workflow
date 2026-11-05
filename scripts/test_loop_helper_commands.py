"""loop.sh の `py <名前>` の呼出しと、補助の COMMANDS の登録が一致することを静的に確かめる。"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts'
LOOP = SCRIPTS / 'loop.sh'
TASK_TEXT = SCRIPTS / 'loop-task-text.py'
CHILD_OUTPUT = SCRIPTS / 'loop-child-output.py'
PY_START = "read -r -d '' PY_HELPER <<'PY' || true\n"
CASE = re.compile(r'case "\$\{1:-\}" in(.*?)\besac\b', re.S)
COMMENT = re.compile(r'(?:^|[ \t])#')
PY_WORD = re.compile(r'(?<![A-Za-z0-9_.-])py(?![A-Za-z0-9_.-])')
# 決まった書き方: py の前(行頭・演算子・キーワード)と、py の後(空白 + 名前 + 区切り)
HEAD = re.compile(r'(?:^[ \t]*|(?:\$\(|`|<\(|\|\||\||&&|;)[ \t]*|(?<![A-Za-z0-9_.-])(?:!|then|do|else|\{)[ \t]+)$')
TAIL = re.compile(r'py[ \t]+([a-z0-9][a-z0-9-]*)(?=[ \t);]|$)')


def split_loop(text):
    """(shell の部分, PY_HELPER の本文, py() の定義) を返す。shell の部分は PY_HELPER の本文と py() の定義を、同じ行数の空行に置き換えたもの。"""
    head, rest = text.split(PY_START, 1)
    helper, tail = rest.split("\nPY\npy()", 1)
    function, after = tail.split("\n}\n", 1)
    removed = PY_START + helper + "\nPY\npy()" + function + "\n}\n"
    # 除いた範囲を同じ行数の空行に置き換え、行番号を loop.sh と一致させる
    return head + "\n" * removed.count("\n") + after, helper, "py()" + function + "\n}\n"


def commands_keys(source):
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == 'COMMANDS' for t in node.targets)):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError('COMMANDS の辞書が見つからない')


def route_names(function, marker):
    """py() の case の腕のうち、本文に marker(TASK_TEXT_CODE か CHILD_OUTPUT_CODE)を含む腕のパターンをすべて集める。"""
    names = set()
    for block in CASE.finditer(function):
        if marker not in block.group(1):
            continue
        for arm in block.group(1).split(';;'):
            if marker in arm and ')' in arm:
                names.update(n.strip() for n in arm.split(')', 1)[0].split('|') if n.strip())
    return names


def invalid_route_patterns(names):
    """case の型が名前のままでないもの(`*`・`?`・`[` などを含む型)。行き先を名前で読めなくなる。"""
    return sorted(n for n in names if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', n))


def scan_calls(shell):
    """shell の各行(行頭が # の行を除く)から、単独の語の `py` をすべて探し、
    決まった書き方の呼出しの名前の集合と、決まった書き方でない出現(行番号: 行)の一覧を返す。
    消し忘れや書き間違いの呼出しを見つけるための検査で、shell を完全には解析しない。
    決まった書き方でない出現は、見落とさない側に倒して一覧に入れる。
    単引用符が開いている間(行をまたぐ文字列の途中を含む)の出現も一覧に入れる。
    行頭が # の行は、単引用符が開いていないときだけ飛ばし、その行の ' は数えない。
    行頭か空白・タブの直後に # がある行の `py` は、すべて決まった書き方でないとして一覧に入れる。
    前の行が `\\` で終わる(行が継続する)行は、行頭(字下げの後)の `py` を一覧に入れる(前の行の引数のため)。
    限界: 二重引用符の中の文や、ヒアドキュメントの中に、決まった書き方の `py <名前>` を書いた場合は、
    呼出しとして数える(このとき 2 つ目の確かめ〈登録したのに呼ばれない〉は落ちない)。"""
    names, bad = set(), []
    open_quote = False
    continued = False
    for number, line in enumerate(shell.splitlines(), 1):
        if not open_quote and line.lstrip().startswith('#'):
            continued = False
            continue
        for m in PY_WORD.finditer(line):
            tail = TAIL.match(line, m.start())
            commented = bool(COMMENT.search(line))
            inside = open_quote != (line[:m.start()].count("'") % 2 == 1)
            if (tail and HEAD.search(line[:m.start()]) and not inside and not commented
                    and not (continued and not line[:m.start()].strip())):
                names.add(tail.group(1))
            else:
                bad.append(f'{number}: {line.strip()}')
        if line.count("'") % 2 == 1:
            open_quote = not open_quote
        continued = line.endswith('\\') and not open_quote
    return names, bad


def split_shell(text):
    return split_loop(text)[0]


def analyze(text):
    """(PY_HELPER へ行く呼出し名の集合, loop-task-text.py へ行く集合, loop-child-output.py へ行く集合, PY_HELPER の本文)。"""
    shell, helper, function = split_loop(text)
    task_routed = route_names(function, 'TASK_TEXT_CODE')
    child_routed = route_names(function, 'CHILD_OUTPUT_CODE')
    calls, _ = scan_calls(shell)
    return calls - task_routed - child_routed, calls & task_routed, calls & child_routed, helper


def unregistered(text, task_source, child_source):
    helper_calls, task_calls, child_calls, helper = analyze(text)
    return (sorted(helper_calls - commands_keys(helper)),
            sorted(task_calls - commands_keys(task_source)),
            sorted(child_calls - commands_keys(child_source)))


class LoopHelperCommandsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = LOOP.read_text(encoding='utf-8')
        cls.task_source = TASK_TEXT.read_text(encoding='utf-8')
        cls.child_source = CHILD_OUTPUT.read_text(encoding='utf-8')

    def unregistered(self, text):
        return unregistered(text, self.task_source, self.child_source)

    def test_calls_are_found(self):
        helper_calls, task_calls, child_calls, _ = analyze(self.text)
        self.assertTrue(helper_calls)
        self.assertEqual(task_calls, {'taskinfo', 'holdcount', 'holdcode'})
        self.assertEqual(child_calls, {'result', 'permlog', 'origin-json'})

    def test_every_call_has_a_registered_command(self):
        self.assertEqual(self.unregistered(self.text), ([], [], []))

    def test_every_registered_helper_command_is_called(self):
        helper_calls, _, child_calls, helper = analyze(self.text)
        self.assertEqual(sorted(commands_keys(helper) - helper_calls), [])
        self.assertEqual(sorted(commands_keys(self.child_source) - child_calls), [])

    def test_extraction_detects_unknown_helper_command(self):
        text = self.text + '\npy no-such-command "$x"\n'
        self.assertEqual(self.unregistered(text)[0], ['no-such-command'])

    def test_extraction_detects_case_route_change(self):
        text = self.text.replace('taskinfo|holdcount|holdcode)', 'taskinfo|holdcount|holdcode|json-get)', 1)
        self.assertNotEqual(text, self.text)
        self.assertEqual(self.unregistered(text)[1], ['json-get'])
        text = self.text.replace('result|permlog|origin-json)', 'result|permlog|origin-json|json-get)', 1)
        self.assertNotEqual(text, self.text)
        self.assertEqual(self.unregistered(text)[2], ['json-get'])

    def test_extraction_detects_second_case_arm(self):
        for index, marker in ((1, 'TASK_TEXT_CODE'), (2, 'CHILD_OUTPUT_CODE')):
            arm = 'code="$' + marker + '" ;;'
            text = self.text.replace(arm, arm + '\n    json-get)\n      ' + arm, 1)
            self.assertNotEqual(text, self.text)
            self.assertEqual(self.unregistered(text)[index], ['json-get'])

    def test_extraction_ignores_comment_lines(self):
        text = self.text + '\n# py no-such-command\n'
        self.assertEqual(self.unregistered(text), ([], [], []))

    def test_scan_rejects_undecided_forms(self):
        # 決まった書き方でない出現は、見落とさず一覧に入れて落とす
        for src in ('foo  # 以前は py permlog で読んだ\n',
                    'die 9 x "読めない(以前の py permlog)"\n',
                    "echo 'py permlog'\n",
                    'echo py permlog\n',
                    'py "snapshot" x\n',
                    "py 'snapshot' x\n",
                    'py \\\n  snapshot x\n',
                    'py "json-get"-typo key\n',
                    'py json-get"$suffix" key\n',
                    'MODE=x py no-such-command\n', 'LC_ALL=C py snapshot\n',
                    'x=$(case $a in b) py zed;; esac)\n',
                    'if py snapshot; then :; fi\n', 'while py snapshot; do :; done\n',
                    "out='$(py permlog \"$X\")'\n",
                    'py permlog<(printf x)\n', 'x=$(py permlog<(printf x))\n',
                    'py permlog>out\n', 'py permlog|cat\n', 'py permlog&\n',
                    'py "$name" x\n', 'py $name x\n', 'py\n'):
            names, bad = scan_calls(src)
            self.assertEqual((names, len(bad)), (set(), 1), src)

    def test_scan_counts_decided_forms(self):
        cases = ['py snapshot x', 'a &&py snapshot x', 'a || py snapshot x', 'x=$(py snapshot x)',
                 'x="$(py snapshot x)"', 'x=`py snapshot x`', 'cat <(py snapshot x)',
                 'a | py snapshot', 'a; py snapshot',
                 '! py snapshot', '{ py snapshot; }',
                 'if a; then py snapshot; fi', 'for i in a; do py snapshot; done', 'f() { py snapshot; }', 'if a; then :; else py snapshot; fi',
                 'read -r a < <(py snapshot x)', 'a | py snapshot 2>&1']
        for src in cases:
            self.assertEqual(scan_calls(src + '\n'), ({'snapshot'}, []), src)

    def test_scan_does_not_skip_after_here_string(self):
        src = 'cat <<<text\npy no-such-command\n'
        self.assertEqual(scan_calls(src), ({'no-such-command'}, []))
        text = self.text + '\ncat <<<text\npy no-such-command\n'
        self.assertEqual(self.unregistered(text)[0], ['no-such-command'])

    def test_unregistered_name_after_assignment_is_not_overlooked(self):
        text = self.text + '\nMODE=x py no-such-command\n'
        shell = split_loop(text)[0]
        self.assertTrue(scan_calls(shell)[1])

    def test_one_line_function_and_arithmetic_shift(self):
        self.assertEqual(scan_calls('f() { py no-such-command; }\n'), ({'no-such-command'}, []))
        text = self.text + '\nf() { py no-such-command; }\n'
        self.assertEqual(self.unregistered(text)[0], ['no-such-command'])
        text = self.text + '\npermlog_of() { py permlog "$1"; }\n'
        self.assertEqual(self.unregistered(text), ([], [], []))
        src = 'mask() { echo $(( 1 << 3 )); }\npy no-such-command\n'
        self.assertEqual(scan_calls(src), ({'no-such-command'}, []))
        text = self.text + '\n' + src
        self.assertEqual(self.unregistered(text)[0], ['no-such-command'])

    def test_case_patterns_must_be_plain_names(self):
        function = split_loop(self.text)[2]
        for marker in ('TASK_TEXT_CODE', 'CHILD_OUTPUT_CODE'):
            self.assertEqual(invalid_route_patterns(route_names(function, marker)), [])
            arm = 'code="$' + marker + '" ;;'
            for pattern in ('json-*', 'json-?et', 'json-[g]et'):
                text = self.text.replace(arm, arm + '\n    ' + pattern + ')\n      ' + arm, 1)
                self.assertEqual(invalid_route_patterns(route_names(split_loop(text)[2], marker)), [pattern])

    def test_known_limit_counts_quoted_and_heredoc_forms(self):
        # 限界: 数えてしまう形。今の挙動のまま固定する
        self.assertEqual(scan_calls('echo "a; py snapshot x"\n'), ({'snapshot'}, []))
        self.assertEqual(scan_calls('cat <<EOF\npy compare x\nEOF\n'), ({'compare'}, []))

    def test_multiline_single_quote_is_not_counted(self):
        for src in ("x='\npy permlog x\n'\n",
                    "printf '%s\\n' '説明:\n  py permlog で読む'\n"):
            names, bad = scan_calls(src)
            self.assertEqual((names, len(bad)), (set(), 1), src)
        # 閉じた後は数える。開いていないときの # の行の ' は数えない
        self.assertEqual(scan_calls("x='a\nb'\npy permlog x\n"), ({'permlog'}, []))
        self.assertEqual(scan_calls("# it's\npy permlog x\n"), ({'permlog'}, []))

    def test_reported_line_numbers_match_loop_sh(self):
        lines = self.text.split('\n')
        target = next(i for i, l in enumerate(lines) if l.startswith('PLUGINS_OUT=') and 'py plugins' in l)
        self.assertEqual(split_shell(self.text).count('\n'), self.text.count('\n'))
        lines[target] = lines[target].replace('py plugins', 'echo py plugins')
        names, bad = scan_calls(split_shell('\n'.join(lines)))
        self.assertEqual(bad, [f'{target + 1}: {lines[target].strip()}'])

    def test_trailing_comment_is_not_counted(self):
        for src in ('foo # py permlog x\n', 'out="" # 旧: $(py permlog "$X")\n'):
            names, bad = scan_calls(src)
            self.assertEqual((names, len(bad)), (set(), 1), src)
        lines = self.text.split('\n')
        target = next(i for i, l in enumerate(lines) if l.strip().startswith('out="$(py permlog "$ITER_PERMLOG")"'))
        lines[target] = '  out="" # 旧: $(py permlog "$ITER_PERMLOG")'
        names, bad = scan_calls(split_shell('\n'.join(lines)))
        self.assertEqual(bad, [f'{target + 1}: {lines[target].strip()}'])

    def test_continued_line_is_not_counted(self):
        src = "printf '%s\\n' \\\n  py profile \"$x\"\n"
        names, bad = scan_calls(src)
        self.assertEqual((names, len(bad)), (set(), 1), src)
        # 継続する行の呼出し自体は数え、その次の行が py を含まなければ影響しない
        self.assertEqual(scan_calls('x="$(py profile a \\\n  b)"\n'), ({'profile'}, []))

    def test_continued_line_with_new_command_is_counted(self):
        # 継続した行でも、`<(` などの後の呼出しは新しいコマンドなので数える
        src = 'a \\\n  < <(printf x | py init-names)\n'
        self.assertEqual(scan_calls(src), ({'init-names'}, []))

    def test_comment_lines_are_skipped(self):
        self.assertEqual(scan_calls('  # py x y\n'), (set(), []))

    def test_loop_has_only_decided_forms(self):
        self.assertEqual(scan_calls(split_shell(self.text))[1], [],
                         '決まった書き方で書き直す(どうしても要るなら、このテストに例外の一覧を作って足す)')

    def test_current_call_set(self):
        helper_calls, task_calls, child_calls, _ = analyze(self.text)
        self.assertEqual(len(helper_calls | task_calls | child_calls), 20)

    def test_route_arms_do_not_share_names(self):
        # case は先の腕を選ぶので、2 つの腕に同じ名前があると後の腕に届かない
        function = split_loop(self.text)[2]
        task, child = route_names(function, 'TASK_TEXT_CODE'), route_names(function, 'CHILD_OUTPUT_CODE')
        self.assertEqual(task & child, set())
        helper_calls, task_calls, child_calls, _ = analyze(self.text)
        self.assertEqual((helper_calls & task_calls, helper_calls & child_calls, task_calls & child_calls),
                         (set(), set(), set()))


if __name__ == '__main__':
    unittest.main()
