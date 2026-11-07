"""子のプロセスの出力を読む 3 つの補助(result・permlog・origin-json)の入出力を固定する。"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts'
LOOP = SCRIPTS / 'loop.sh'
CHILD = SCRIPTS / 'loop-child-output.py'
PARSER = SCRIPTS / 'loop-task-text.py'


def embedded():
    return LOOP.read_text().split("read -r -d '' PY_HELPER <<'PY' || true\n", 1)[1].split('\nPY\npy()', 1)[0]


def shell_function(name):
    source = LOOP.read_text()
    return name + '() {' + source.split(name + '() {', 1)[1].split('\n}\n', 1)[0] + '\n}\n'


def code_under_test():
    return CHILD.read_text()


def parent_shell(code, hold=True):
    source = LOOP.read_text()
    initialization = source.split('unset TASK_TEXT_CODE TASK_TEXT_HELD', 1)[1].split('CHILD_OUTPUT_HELD=0', 1)[0]
    shell = ('set -euo pipefail\nunset TASK_TEXT_CODE TASK_TEXT_HELD' + initialization + 'CHILD_OUTPUT_HELD=0\n'
             + 'PY_HELPER=' + shlex.quote(embedded()) + '\n'
             + 'PY_ABS=' + shlex.quote(sys.executable) + '\n'
             + 'FIXED_POLICY="{}"\n'
             + 'die() { printf "ERROR [%s] %s\\n" "$2" "$3" >&2; exit "$1"; }\n'
             + ''.join(shell_function(name) for name in (
                 'py', 'environment_call', 'read_held_copy', 'hold_task_text', 'hold_child_output', 'json_fields_into', 'bind_environment')))
    if hold:
        shell += 'CHILD_OUTPUT_HELD=1\nCHILD_OUTPUT_CODE=' + shlex.quote(code) + '\n'
    return shell


# ── 入力を作る関数(表の外。名前で表から引く)──
BUILD = {}


def _write(tmp, data, name='in'):
    path = tmp / name
    path.write_bytes(data)
    return path


def _file(data):
    return lambda tmp: ([str(_write(tmp, data))], b'')


def _stdin(data):
    return lambda tmp: ([], data)


def _json_file(value):
    return _file(json.dumps(value).encode())


def _perm_rows(*rows):
    return b''.join(json.dumps(r, ensure_ascii=False).encode() + b'\n' for r in rows)


DEEP = b'[' * 100000 + b']' * 100000
ALLOW = {'decision': 'allow'}
OUTCOME_PREFIX = '無人の周の結果: '

BUILD['result-empty'] = _file(b'')
BUILD['result-truncated'] = _file(b'{"a":')
BUILD['result-bom'] = _file(b'\xef\xbb\xbf{"result":"x"}')
BUILD['result-int'] = _file(b'1')
BUILD['result-null'] = _file(b'null')
BUILD['result-empty-list'] = _file(b'[]')
BUILD['result-list-without-result'] = _json_file([{'type': 'x'}, 'a'])
BUILD['result-empty-dict'] = _file(b'{}')
BUILD['result-nonstring'] = _json_file({'result': 5})
BUILD['result-three-lines'] = _json_file({'result': (
    OUTCOME_PREFIX + '保留 — a\r\n' + OUTCOME_PREFIX + 'PR — b\x01c\r\n  ' + OUTCOME_PREFIX + '縮退 — z')})
BUILD['result-unknown-outcome'] = _json_file({'result': OUTCOME_PREFIX + '成功 — a'})
BUILD['result-array3'] = _json_file([
    {'type': 'result', 'permission_denials': [1]},
    {'type': 'result', 'permission_denials': [2], 'Other_Denial': 'z', 'result': OUTCOME_PREFIX + 'PR — 2'},
    'text'])
BUILD['result-mixed-denials'] = _json_file([
    {'type': 'result', 'permission_denials': [1]}, {'type': 'result', 'permission_denials': 's'}])
BUILD['result-long'] = _json_file({'result': OUTCOME_PREFIX + 'PR — ' + 'あ' * 600, 'permission_denials': ['x' * 3000]})
BUILD['result-deep'] = _file(DEEP)
BUILD['result-missing'] = lambda tmp: ([str(tmp / 'nope')], b'')
BUILD['result-directory'] = lambda tmp: ([str(tmp)], b'')
BUILD['no-args'] = lambda tmp: ([], b'')
BUILD['two-args'] = lambda tmp: (['a', 'b'], b'')
BUILD['one-arg'] = lambda tmp: (['a'], b'')

BUILD['permlog-empty'] = _file(b'')
BUILD['permlog-mixed'] = _file(_perm_rows(
    ALLOW, ALLOW,
    {'decision': 'deny', 'kind': 'protected', 'tool_name': 'Write', 'reason': 'r', 'subject': 's'},
    {'decision': 'deny', 'tool_name': 'Bash', 'reason': 'q', 'subject': 't'},
    {'decision': 'deny', 'kind': 'b\x01k', 'tool_name': 'X', 'reason': 'y', 'subject': 'z'}))
BUILD['permlog-bad-after-counts'] = _file(
    _perm_rows(ALLOW, {'decision': 'deny', 'kind': 'k'}) + b'{bad\n' + _perm_rows(ALLOW))
BUILD['permlog-no-final-newline'] = _file(b'{"decision":"allow"}')
BUILD['permlog-bom'] = _file(b'\xef\xbb\xbf{"decision":"allow"}\n')
BUILD['permlog-u2028'] = _file(('{"decision":"deny","reason":"a b"}\n').encode())
BUILD['permlog-blank-line'] = _file(b'{"decision":"allow"}\n\n')
BUILD['permlog-not-utf8'] = _file(b'\xff\n')
BUILD['permlog-not-dict'] = _file(b'[1]\n')
BUILD['permlog-other-decision'] = _file(b'{"decision":"maybe"}\n')
BUILD['permlog-kind-not-string'] = _file(b'{"decision":"deny","kind":5}\n')
BUILD['permlog-10001-lines'] = _file(b'{"decision":"allow"}\n' * 10001)
BUILD['permlog-10000-lines'] = _file(b'{"decision":"allow"}\n' * 10000)
BUILD['permlog-over-1mib'] = _file(b'x' * 1048577)
BUILD['permlog-105-deny'] = _file(_perm_rows(*[
    {'decision': 'deny', 'tool_name': 'T', 'reason': 'r' * 700, 'subject': 's'}] * 105))
BUILD['permlog-missing'] = lambda tmp: ([str(tmp / 'nope')], b'')


def _permlog_symlink(tmp):
    _write(tmp, b'{"decision":"allow"}\n', 'real')
    (tmp / 'link').symlink_to(tmp / 'real')
    return [str(tmp / 'link')], b''


def _permlog_dir_symlink(tmp):
    (tmp / 'dir').mkdir()
    _write(tmp / 'dir', b'{"decision":"allow"}\n', 'log')
    (tmp / 'dlink').symlink_to(tmp / 'dir')
    return [str(tmp / 'dlink' / 'log')], b''


def _permlog_fifo(tmp):
    os.mkfifo(tmp / 'fifo')
    return [str(tmp / 'fifo')], b''


BUILD['permlog-symlink'] = _permlog_symlink
BUILD['permlog-dir-symlink'] = _permlog_dir_symlink
BUILD['permlog-directory'] = lambda tmp: ([str(tmp)], b'')
BUILD['permlog-fifo'] = _permlog_fifo

ORIGIN = {'origin': True, 'same': True, 'vcs': True, 'reason': 'ok', 'repo': 'owner/r'}
BUILD['origin-with'] = _stdin(json.dumps(ORIGIN).encode())
BUILD['origin-without'] = _stdin(json.dumps(
    {'origin': False, 'same': False, 'vcs': False, 'reason': 'none', 'repo': None}).encode())
BUILD['origin-empty'] = _stdin(b'')
BUILD['origin-truncated'] = _stdin(b'{')
BUILD['origin-bom'] = _stdin(b'\xef\xbb\xbf' + json.dumps(ORIGIN).encode())
BUILD['origin-not-utf8'] = _stdin(b'\xff')
BUILD['origin-list'] = _stdin(b'[]')
BUILD['origin-int-bool'] = _stdin(json.dumps(dict(ORIGIN, origin=1)).encode())
BUILD['origin-no-vcs'] = _stdin(json.dumps({k: v for k, v in ORIGIN.items() if k != 'vcs'}).encode())
BUILD['origin-repo-number'] = _stdin(json.dumps(dict(ORIGIN, repo=5)).encode())
BUILD['origin-repo-newline'] = _stdin(json.dumps(dict(ORIGIN, repo='a\nrepo=evil')).encode())
BUILD['origin-reason-number'] = _stdin(json.dumps(dict(ORIGIN, reason=5)).encode())
BUILD['origin-reason-long'] = _stdin(json.dumps(dict(ORIGIN, reason='x\x01y' + 'z' * 400)).encode())
BUILD['origin-whitespace'] = _stdin(b' \r\n' + json.dumps(ORIGIN).encode() + b'\r\n ')
BUILD['origin-duplicate-key'] = _stdin(
    b'{"origin":false,"origin":true,"same":true,"vcs":true,"reason":"ok","repo":"owner/r"}')
BUILD['origin-reason-huge'] = _stdin(json.dumps(dict(ORIGIN, reason='a' * 5000000)).encode())
BUILD['origin-deep'] = _stdin(DEEP)

# 照らし方: '全体' は終了コード・stdout・stderr の全体。
# ('先頭', 'stdout', n) は stdout の n 行目(0 から数える)だけ先頭の固定部分で照らす(ほかの行は全体)。
# ('先頭', 'stderr') は stderr が 1 行で、期待値で始まることを照らす(stdout は全体)。
# ── 期待値の表(ここから)
TABLE = [
    ('result: empty', 'result', 'result-empty', '全体', (0, 'json=bad\n', '')),
    ('result: truncated', 'result', 'result-truncated', '全体', (0, 'json=bad\n', '')),
    ('result: bom', 'result', 'result-bom', '全体', (0, 'json=bad\n', '')),
    ('result: int', 'result', 'result-int', '全体', (0, 'json=bad\n', '')),
    ('result: null', 'result', 'result-null', '全体', (0, 'json=bad\n', '')),
    ('result: empty-list', 'result', 'result-empty-list', '全体', (0, 'json=bad\n', '')),
    ('result: list-without-result', 'result', 'result-list-without-result', '全体', (0, 'json=bad\n', '')),
    ('result: empty-dict', 'result', 'result-empty-dict', '全体', (0, 'json=ok\n', '')),
    ('result: nonstring', 'result', 'result-nonstring', '全体', (0, 'json=ok\n', '')),
    ('result: three-lines', 'result', 'result-three-lines', '全体', (0, 'json=ok\noutcome=PR\ndetail=b c\n', '')),
    ('result: unknown-outcome', 'result', 'result-unknown-outcome', '全体', (0, 'json=ok\n', '')),
    ('result: array3', 'result', 'result-array3', '全体', (0, 'json=ok\noutcome=PR\ndetail=2\ndenials={"permission_denials": [1, 2], "Other_Denial": "z"}\n', '')),
    ('result: mixed-denials', 'result', 'result-mixed-denials', '全体', (0, 'json=ok\ndenials={"permission_denials": [[1], "s"]}\n', '')),
    ('result: long', 'result', 'result-long', '全体', (0, 'json=ok\noutcome=PR\ndetail=' + 'あ' * 500 + '\ndenials=' + ('{"permission_denials": ["' + 'x' * 3000 + '"]}')[:2000] + '\n', '')),
    ('permlog: empty', 'permlog', 'permlog-empty', '全体', (0, 'allow=0\ndeny=0\nkinds=\n', '')),
    ('permlog: mixed', 'permlog', 'permlog-mixed', '全体', (0, 'allow=2\ndeny=3\nkinds=b k:1,other:1,protected:1\nline=Write [protected] r — s\nline=Bash [other] q — t\nline=X [b k] y — z\n', '')),
    ('permlog: bad-after-counts', 'permlog', 'permlog-bad-after-counts', ('先頭', 'stdout', 3), (0, 'allow=1\ndeny=1\nkinds=k:1\nreference-error=Expecting \nline=None [k] None — None\n', '')),
    ('permlog: no-final-newline', 'permlog', 'permlog-no-final-newline', '全体', (0, 'allow=0\ndeny=0\nkinds=\nreference-error=末尾の行が途中で切れている\n', '')),
    ('permlog: bom', 'permlog', 'permlog-bom', ('先頭', 'stdout', 3), (0, 'allow=0\ndeny=0\nkinds=\nreference-error=Unexpected UTF-8 BOM\n', '')),
    ('permlog: u2028', 'permlog', 'permlog-u2028', ('先頭', 'stdout', 3), (0, 'allow=0\ndeny=0\nkinds=\nreference-error=Unterminated string\n', '')),
    ('permlog: blank-line', 'permlog', 'permlog-blank-line', ('先頭', 'stdout', 3), (0, 'allow=1\ndeny=0\nkinds=\nreference-error=Expecting value\n', '')),
    ('permlog: not-utf8', 'permlog', 'permlog-not-utf8', ('先頭', 'stdout', 3), (0, "allow=0\ndeny=0\nkinds=\nreference-error='utf-8' codec can't decode byte 0xff\n", '')),
    ('permlog: not-dict', 'permlog', 'permlog-not-dict', '全体', (0, 'allow=0\ndeny=0\nkinds=\nreference-error=行の形式が違う\n', '')),
    ('permlog: other-decision', 'permlog', 'permlog-other-decision', '全体', (0, 'allow=0\ndeny=0\nkinds=\nreference-error=行の形式が違う\n', '')),
    ('permlog: kind-not-string', 'permlog', 'permlog-kind-not-string', '全体', (0, 'allow=0\ndeny=1\nkinds=\nreference-error=kind が文字列でない\n', '')),
    ('permlog: 10001-lines', 'permlog', 'permlog-10001-lines', '全体', (0, 'allow=0\ndeny=0\nkinds=\nreference-error=行数上限を超える\n', '')),
    ('permlog: 10000-lines', 'permlog', 'permlog-10000-lines', '全体', (0, 'allow=10000\ndeny=0\nkinds=\n', '')),
    ('permlog: over-1mib', 'permlog', 'permlog-over-1mib', '全体', (0, 'allow=0\ndeny=0\nkinds=\nreference-error=通常ファイルでないか読取上限を超える\n', '')),
    ('permlog: 105-deny', 'permlog', 'permlog-105-deny', '全体', (0, 'allow=0\ndeny=105\nkinds=other:105\n' + ('line=T [other] ' + 'r' * 590 + '\n') * 100, '')),
    ('permlog: missing', 'permlog', 'permlog-missing', ('先頭', 'stdout', 3), (0, 'allow=0\ndeny=0\nkinds=\nreference-error=[Errno 2] No such file or directory\n', '')),
    ('permlog: symlink', 'permlog', 'permlog-symlink', ('先頭', 'stdout', 3), (0, 'allow=0\ndeny=0\nkinds=\nreference-error=[Errno 40] Too many levels of symbolic links\n', '')),
    ('permlog: dir-symlink', 'permlog', 'permlog-dir-symlink', ('先頭', 'stdout', 3), (0, 'allow=0\ndeny=0\nkinds=\nreference-error=[Errno 20] Not a directory\n', '')),
    ('permlog: directory', 'permlog', 'permlog-directory', '全体', (0, 'allow=0\ndeny=0\nkinds=\nreference-error=通常ファイルでないか読取上限を超える\n', '')),
    ('permlog: fifo', 'permlog', 'permlog-fifo', '全体', (0, 'allow=0\ndeny=0\nkinds=\nreference-error=通常ファイルでないか読取上限を超える\n', '')),
    ('origin-json: with', 'origin-json', 'origin-with', '全体', (0, 'origin=1\nsame=1\nvcs=1\nreason=ok\nrepo=owner/r\n', '')),
    ('origin-json: without', 'origin-json', 'origin-without', '全体', (0, 'origin=0\nsame=0\nvcs=0\nreason=none\nrepo=\n', '')),
    ('origin-json: empty', 'origin-json', 'origin-empty', '全体', (1, '', '')),
    ('origin-json: truncated', 'origin-json', 'origin-truncated', '全体', (1, '', '')),
    ('origin-json: bom', 'origin-json', 'origin-bom', '全体', (1, '', '')),
    ('origin-json: not-utf8', 'origin-json', 'origin-not-utf8', '全体', (1, '', '')),
    ('origin-json: list', 'origin-json', 'origin-list', '全体', (1, '', '')),
    ('origin-json: int-bool', 'origin-json', 'origin-int-bool', '全体', (1, '', '')),
    ('origin-json: no-vcs', 'origin-json', 'origin-no-vcs', '全体', (1, '', '')),
    ('origin-json: repo-number', 'origin-json', 'origin-repo-number', '全体', (1, '', '')),
    ('origin-json: repo-newline', 'origin-json', 'origin-repo-newline', '全体', (0, 'origin=1\nsame=1\nvcs=1\nreason=ok\nrepo=a\nrepo=evil\n', '')),
    ('origin-json: reason-number', 'origin-json', 'origin-reason-number', '全体', (0, 'origin=1\nsame=1\nvcs=1\nreason=\nrepo=owner/r\n', '')),
    ('origin-json: reason-long', 'origin-json', 'origin-reason-long', '全体', (0, 'origin=1\nsame=1\nvcs=1\nreason=x y' + 'z' * 297 + '\nrepo=owner/r\n', '')),
    ('origin-json: whitespace', 'origin-json', 'origin-whitespace', '全体', (0, 'origin=1\nsame=1\nvcs=1\nreason=ok\nrepo=owner/r\n', '')),
    ('origin-json: duplicate-key', 'origin-json', 'origin-duplicate-key', '全体', (0, 'origin=1\nsame=1\nvcs=1\nreason=ok\nrepo=owner/r\n', '')),
    ('origin-json: reason-huge', 'origin-json', 'origin-reason-huge', '全体', (0, 'origin=1\nsame=1\nvcs=1\nreason=' + 'a' * 300 + '\nrepo=owner/r\n', '')),
    ('想定外: result 深い入れ子', 'result', 'result-deep', ('先頭', 'stderr'), (9, '', '補助の失敗: RecursionError(')),
    ('想定外: origin-json 深い入れ子', 'origin-json', 'origin-deep', ('先頭', 'stderr'), (9, '', '補助の失敗: RecursionError(')),
    ('想定外: result 無いファイル', 'result', 'result-missing', '全体', (9, '', "補助の失敗: FileNotFoundError(2, 'No such file or directory')\n")),
    ('想定外: result フォルダ', 'result', 'result-directory', '全体', (9, '', "補助の失敗: IsADirectoryError(21, 'Is a directory')\n")),
    ('想定外: result 引数なし', 'result', 'no-args', ('先頭', 'stderr'), (9, '', '補助の失敗: TypeError(')),
    ('想定外: result 引数が多い', 'result', 'two-args', ('先頭', 'stderr'), (9, '', '補助の失敗: TypeError(')),
    ('想定外: permlog 引数なし', 'permlog', 'no-args', ('先頭', 'stderr'), (9, '', '補助の失敗: TypeError(')),
    ('想定外: permlog 引数が多い', 'permlog', 'two-args', ('先頭', 'stderr'), (9, '', '補助の失敗: TypeError(')),
    ('想定外: origin-json 引数あり', 'origin-json', 'one-arg', ('先頭', 'stderr'), (9, '', '補助の失敗: TypeError(')),
]
# ── 期待値の表(ここまで)


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in ('PYTHONWARNINGS', 'PYTHONDEVMODE', 'PYTHONIOENCODING')}


class ChildOutputTests(unittest.TestCase):
    via_parent = False

    def run_case(self, command, builder, tmp):
        argv, data = BUILD[builder](tmp)
        code = code_under_test()
        invocation = [sys.executable, '-c', code, command, *argv]
        if self.via_parent:
            invocation = ['bash', '-c', parent_shell(code) + 'py "$@"\n', '--', command, *argv]
        return subprocess.run(invocation, input=data, capture_output=True, timeout=60, cwd='/', env=clean_env())

    def judge(self, name, result, mode, expected):
        rc, out, err = expected
        got = (result.returncode, result.stdout.decode('utf-8'), result.stderr.decode('utf-8'))
        if mode == '全体':
            self.assertEqual(got, (rc, out, err), name)
        elif mode[1] == 'stderr':
            self.assertEqual((got[0], got[1]), (rc, out), name)
            self.assertEqual(got[2].count('\n'), 1, name)
            self.assertTrue(got[2].startswith(err), (name, got[2]))
        else:
            index = mode[2]
            self.assertEqual((got[0], got[2]), (rc, err), name)
            got_lines, want_lines = got[1].split('\n'), out.split('\n')
            self.assertEqual(len(got_lines), len(want_lines), (name, got[1]))
            for i, (g, w) in enumerate(zip(got_lines, want_lines)):
                if i == index:
                    self.assertTrue(g.startswith(w), (name, g))
                else:
                    self.assertEqual(g, w, name)

    def check_table(self, selected):
        rows = [r for r in TABLE if selected(r)]
        self.assertTrue(rows)
        for name, command, builder, mode, expected in rows:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmp:
                    result = self.run_case(command, builder, Path(tmp).resolve())
                self.judge(name, result, mode, expected)

    def test_result_cases(self):
        self.check_table(lambda r: r[0].startswith('result:'))

    def test_permlog_cases(self):
        self.check_table(lambda r: r[0].startswith('permlog:') and r[2] not in PERMLOG_PATH_KINDS)

    @unittest.skipUnless(sys.platform == 'linux', 'errno と文言は Linux のもの')
    def test_permlog_path_kinds(self):
        self.check_table(lambda r: r[0].startswith('permlog:') and r[2] in PERMLOG_PATH_KINDS)

    def test_origin_json_cases(self):
        self.check_table(lambda r: r[0].startswith('origin-json:'))

    def test_unexpected_failures(self):
        self.check_table(lambda r: r[0].startswith('想定外:'))


PERMLOG_PATH_KINDS = ('permlog-missing', 'permlog-symlink', 'permlog-dir-symlink', 'permlog-directory', 'permlog-fifo')


class ParentChildOutputTests(ChildOutputTests):
    via_parent = True


@unittest.skipUnless(sys.platform == "linux", "信頼コピーの固定 /etc 検査はLinux契約")
class HeldChildOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # macOSの /var alias を信頼コピーへ持ち込まない。
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in (
            'PYTHONPATH', 'PYTHONHOME', 'PYTHONNOUSERSITE', 'GIT_CONFIG_GLOBAL',
            'GIT_CONFIG_SYSTEM', 'CLAUDE_CONFIG_DIR', 'ZDOTDIR', 'BASH_ENV', 'ENV')}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'),
                        GIT_CONFIG_NOSYSTEM='1', PYTHONDONTWRITEBYTECODE='1')
        self.root = self.base / 'plugin'
        self.relative = Path('skills/ship-task/scripts/loop-child-output.py')
        self.source = self.root / self.relative
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(CHILD.read_bytes())
        self.source.with_name('loop-task-text.py').write_bytes(PARSER.read_bytes())
        (self.root / '.claude-plugin').mkdir()
        (self.root / '.claude-plugin/plugin.json').write_text('{}')
        for name in ('loop-permission', 'origin-repo', 'git-config-digest', 'loop-state',
                     'loop-startup', 'host-argv', 'loop-supervisor', 'host-check'):
            self.source.with_name(name + '.py').write_text('# fixture\n')
        resolver = self.root / 'skills/create-task/scripts/resolve-task-dir.py'
        resolver.parent.mkdir(parents=True)
        resolver.write_text('# fixture\n')
        self.sequence = 0
        self.origin = json.dumps(ORIGIN).encode()
        self.origin_output = b'origin=1\nsame=1\nvcs=1\nreason=ok\nrepo=owner/r\n'

    def boot(self, root=None):
        self.sequence += 1
        result = subprocess.run([sys.executable, '-I', '-B', str(SCRIPTS / 'environment-guard.py'),
                                 'bootstrap', '--root', str(root or self.root), '--output',
                                 str(self.base / ('bundle-' + str(self.sequence)))],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def run_shell(self, body, *, data=b'', env=None, cwd=None, hold=False):
        return subprocess.run(['bash', '-c', parent_shell(None, hold=hold) + body], input=data,
                              capture_output=True, timeout=25, cwd=cwd or self.base, env=env or self.env)

    def bind(self, receipt):
        return 'bind_environment ' + shlex.quote(json.dumps(receipt)) + '\n'

    def test_real_copy_rebind_and_retained_bytes(self):
        first, active = self.boot(), self.boot()
        body = self.bind(first) + 'initial="$CHILD_OUTPUT_CODE"\n' + self.bind(active)
        body += '[ "$initial" = "$CHILD_OUTPUT_CODE" ]\n'
        body += 'printf "%s" "$CHILD_OUTPUT_CODE" > ' + shlex.quote(str(self.base / 'held')) + '\n'
        # 解析時は再読しない。原本も保持コピーも無くしてから実際のpyを呼ぶ。
        body += 'rm -- ' + shlex.quote(str(self.source)) + ' ' + shlex.quote(str(Path(active['plugin']) / self.relative)) + '\n'
        body += 'py origin-json\n'
        result = self.run_shell(body, data=self.origin)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, self.origin_output, b''))
        self.assertEqual((self.base / 'held').read_bytes(), CHILD.read_bytes())

    def test_rebind_rejects_changed_content_and_trailing_newline(self):
        first = self.boot()
        for suffix in (b'\n', b'\nprint("must-not-run")\n'):
            with self.subTest(suffix=suffix):
                other = self.base / ('other-' + str(self.sequence))
                shutil.copytree(self.root, other)
                (other / self.relative).write_bytes(CHILD.read_bytes() + suffix)
                active = self.boot(other)
                held = self.base / 'held-after-reject'
                body = self.bind(first)
                body += 'trap \'printf "%s" "$CHILD_OUTPUT_CODE" > ' + shlex.quote(str(held)) + "' EXIT\n"
                body += self.bind(active) + 'echo must-not-run\n'
                result = self.run_shell(body)
                self.assertEqual((result.returncode, result.stdout), (20, b''), result.stderr)
                self.assertIn('ERROR [environment] 子の出力の解析のコピーが初回と異なる'.encode(), result.stderr)
                self.assertEqual(held.read_bytes(), CHILD.read_bytes())

    def test_invalid_source_bytes_rejected_before_shell_conversion(self):
        for raw in (b'', b'# valid before NUL\0\nprint("must-not-run")\n', b'# \xff\n'):
            with self.subTest(raw=raw):
                self.source.write_bytes(raw)
                receipt = self.boot()
                result = self.run_shell(self.bind(receipt) + 'echo must-not-run\n')
                self.assertEqual((result.returncode, result.stdout), (20, b''), result.stderr)
                self.assertIn('ERROR [environment] 子の出力の解析のコピーを読めない'.encode(), result.stderr)
                self.assertNotIn(b'ignored null byte', result.stderr)

    def test_missing_file_in_initial_bundle_is_rejected(self):
        self.source.unlink()
        result = self.run_shell(self.bind(self.boot()) + 'echo must-not-run\n')
        self.assertEqual((result.returncode, result.stdout), (20, b''), result.stderr)
        self.assertIn('ERROR [environment] 子の出力の解析のコピーを読めない'.encode(), result.stderr)

    def test_unheld_code_is_not_run(self):
        for command in ('result /nonexistent', 'permlog /nonexistent', 'origin-json'):
            with self.subTest(command=command):
                result = self.run_shell('py ' + command + '\necho must-not-run\n')
                self.assertEqual((result.returncode, result.stdout), (20, b''), result.stderr)
                self.assertIn('ERROR [environment] 子の出力の解析を保持していない'.encode(), result.stderr)

    def test_exported_values_are_reset_and_not_exported_again(self):
        receipt = self.boot()
        env = dict(self.env, CHILD_OUTPUT_HELD='1', CHILD_OUTPUT_CODE='print("inherited")')
        body = '[ "$CHILD_OUTPUT_HELD" = 0 ] && [ -z "$CHILD_OUTPUT_CODE" ]\n' + self.bind(receipt)
        body += '"$PY_ABS" -I -c \'import os; assert "CHILD_OUTPUT_CODE" not in os.environ; assert "CHILD_OUTPUT_HELD" not in os.environ\'\n'
        body += 'py origin-json\n'
        result = self.run_shell(body, env=env, data=self.origin)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, self.origin_output, b''))

    def test_actual_parent_cwd_fd_stdin_and_pythonpath(self):
        receipt = self.boot()
        cwd = self.base / 'cwd'; cwd.mkdir()
        (cwd / 'json.py').write_text('raise RuntimeError("caller cwd imported")\n')
        extra = self.base / 'extra'; extra.mkdir()
        observation = self.base / 'observation.json'
        (extra / 'sitecustomize.py').write_text(
            'import errno,json,os,sys\n'
            'try:\n    os.fstat(7)\n    fd="open"\n'
            'except OSError as exc:\n    fd=exc.errno\n'
            'open(' + repr(str(observation)) + ',"w").write(json.dumps([os.getcwd(), fd, sys.flags.isolated, sys.path]))\n')
        env = dict(self.env, PYTHONPATH=str(extra))
        # bind_environment の py json-get も観察のファイルに書く。bind の後に消してから対象の起動を観察する。
        body = self.bind(receipt) + 'rm -f -- ' + shlex.quote(str(observation)) + '\nexec 7</dev/null\npy origin-json\n'
        result = self.run_shell(body, env=env, cwd=cwd, data=self.origin)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, self.origin_output, b''))
        observed = json.loads(observation.read_text())
        self.assertEqual(observed[:3], ['/', 9, 0])
        self.assertIn(str(extra), observed[3])
        self.assertNotIn(str(CHILD.parent), observed[3])
        self.assertNotIn(str(Path(receipt['plugin']) / self.relative.parent), observed[3])

    def test_actual_parent_user_site_remains_enabled(self):
        userbase = self.base / 'userbase'
        env = dict(self.env, PYTHONUSERBASE=str(userbase))
        site_result = subprocess.run([sys.executable, '-c', 'import site; print(site.getusersitepackages())'],
                                     env=env, capture_output=True, text=True, check=True)
        site = Path(site_result.stdout.strip()); site.mkdir(parents=True)
        marker = self.base / 'user-site-loaded'
        (site / 'usercustomize.py').write_text('open(' + repr(str(marker)) + ',"w").write("yes")\n')
        receipt = self.boot()
        body = self.bind(receipt) + 'rm -f -- ' + shlex.quote(str(marker)) + '\npy origin-json\n'
        result = self.run_shell(body, env=env, data=self.origin)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, self.origin_output, b''))
        self.assertEqual(marker.read_text(), 'yes')


if __name__ == '__main__':
    unittest.main()
