"""道具の版の確かめ(plugins/dev-workflow/hooks/tool-versions.sh)と、その一覧の自己テスト。

スタブの道具だけを置いた PATH(処理に使う sed などは実物へのリンク)と、一時の HOME・XDG_STATE_HOME で流す。
sh は /bin/sh(Ubuntu では dash)で流し、POSIX の sh で動くことも確かめる。
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from support_load import load_script

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / 'plugins/dev-workflow/hooks'
SCRIPT = HOOKS / 'tool-versions.sh'
LIST = HOOKS / 'tool-requirements.tsv'
HOOKS_JSON = HOOKS / 'hooks.json'
MANIFEST = ROOT / 'plugins/dev-workflow/.claude-plugin/plugin.json'
RUNTIME = ROOT / 'plugins/dev-workflow/skills/do-task/references/runtime-requirements.md'
HOST_CHECK = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/host-check.py'
SH = os.environ.get('TOOL_VERSIONS_TEST_SH', '/bin/sh')
# スクリプトが処理に使う道具(確かめる道具とは重ねない)
HELPERS = ('sed', 'awk', 'tr', 'mkdir', 'mv', 'rm')
TABLE_START = '<!-- tool-requirements:start -->'
TABLE_END = '<!-- tool-requirements:end -->'


def plugin_version():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))['version']


def list_rows():
    rows = []
    for line in LIST.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        rows.append(line.split('\t'))
    return rows


def stub(text):
    return "#!/bin/sh\nprintf '%s\\n' '" + text + "'\n"


# 足りる版を出すスタブ。名前 → スクリプト(None は置かない)
GOOD = {
    'git': stub('git version 2.56.0'),
    'bash': stub('GNU bash, version 5.1.16(1)-release (x86_64-pc-linux-gnu)'),
    'python3': "#!/bin/sh\ncase $1 in --version) printf '%s\\n' 'Python 3.10.12' ;; -B) printf '%s' '6.0.3' ;; *) exit 9 ;; esac\n",
    'jq': stub('jq-1.6'),
    'realpath': stub('realpath (GNU coreutils) 9.4'),
    'readlink': stub('readlink (GNU coreutils) 9.4'),
    'sha256sum': stub('sha256sum (GNU coreutils) 9.4'),
    'sort': stub('sort (GNU coreutils) 9.4'),
    'stat': stub('stat (GNU coreutils) 9.4'),
    'timeout': stub('timeout (GNU coreutils) 9.4'),
    'cp': stub('cp (GNU coreutils) 9.4'),
    'find': stub('find (GNU findutils) 4.9.0'),
    'grep': stub('grep (GNU grep) 3.11'),
    'flock': stub('flock from util-linux 2.39.3'),
    'setsid': stub('setsid from util-linux 2.39.3'),
}


class ToolVersionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.state = self.base / 'state'
        self.state_file = self.state / 'dev-workflow/tool-versions/checked-version'

    def tools(self, **changes):
        """スタブの PATH を作る。changes の値が None なら、その道具を置かない。"""
        bin_dir = Path(tempfile.mkdtemp(dir=self.base, prefix='bin'))
        for name in HELPERS:
            real = shutil.which(name)
            self.assertIsNotNone(real, name)
            os.symlink(real, bin_dir / name)
        tools = dict(GOOD, **changes)
        for name, body in tools.items():
            if body is None:
                continue
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        return str(bin_dir)

    def run_script(self, *args, path=None, **env_changes):
        env = {'PATH': path or self.tools(), 'HOME': str(self.home), 'XDG_STATE_HOME': str(self.state)}
        env.update(env_changes)
        env = {key: value for key, value in env.items() if value is not None}
        return subprocess.run([SH, str(SCRIPT), *args], cwd=self.base, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                              encoding='utf-8')

    def session(self, **kwargs):
        result = self.run_script('--session-start', **kwargs)
        self.assertEqual(0, result.returncode, result.stderr)
        if not result.stdout:
            return None
        value = json.loads(result.stdout)
        message = value['systemMessage']
        self.assertEqual({'hookEventName': 'SessionStart', 'additionalContext': message}, value['hookSpecificOutput'])
        return message

    def row(self, text, name, found, want, result):
        self.assertIn('| %s | %s | %s | %s |' % (name, found, want, result), text)

    def test_all_tools_enough(self):
        result = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('dev-workflow %s が使う道具の版:' % plugin_version(), result.stdout)
        self.row(result.stdout, 'git', '2.56.0', '2.42 以上', '足りる')
        self.row(result.stdout, 'python3', '3.10.12', '3.10 以上', '足りる')
        self.row(result.stdout, 'PyYAML', '6.0.3', '-', '足りる')
        self.row(result.stdout, 'GNU coreutils', '9.4', '8.30 以上', '足りる')
        self.row(result.stdout, 'flock・setsid', '-', '-', '足りる')
        self.assertIn('足りない道具はありません。', result.stdout)
        self.assertEqual(len(list_rows()), result.stdout.count('| 足りる |'))
        self.assertFalse(self.state.exists())

    def test_first_session_start_shows_table_and_second_is_silent(self):
        message = self.session()
        self.assertIn('dev-workflow %s が使う道具の版を確かめました(この版で初めての起動)。' % plugin_version(), message)
        self.assertIn('| 道具 | 見つけた版 | 要る版 | 結果 | 使う所 |', message)
        self.assertIn('次に知らせるのは、プラグインの版が変わった後の最初の起動です。', message)
        self.assertIn('手で確かめるには: sh "%s"' % SCRIPT.resolve(), message)
        self.assertEqual(plugin_version() + '\n', self.state_file.read_text())
        self.assertIsNone(self.session())

    def test_changed_plugin_version_shows_again(self):
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text('0.0.1\n')
        self.assertIsNotNone(self.session())
        self.assertEqual(plugin_version() + '\n', self.state_file.read_text())

    def test_short_tool_shows_fix_and_does_not_stop(self):
        message = self.session(path=self.tools(git=stub('git version 2.34.1')))
        self.row(message, 'git', '2.34.1', '2.42 以上', '足りない')
        self.assertIn('足りない道具が 1 件あります。止めずに続けます。直し方:', message)
        self.assertIn('ppa:git-core/ppa', message)
        self.assertTrue(self.state_file.exists())

    def test_missing_tools(self):
        result = self.run_script(path=self.tools(git=None, jq=None, setsid=None))
        self.assertEqual(0, result.returncode, result.stderr)
        self.row(result.stdout, 'git', '-', '2.42 以上', '無い')
        self.row(result.stdout, 'jq', '-', '1.5 以上', '無い(任意)')
        self.row(result.stdout, 'flock・setsid', '-', '-', '無い(setsid)(条件つき)')
        self.assertIn('足りない道具が 2 件あります(うち 1 件は条件つき。使う所に当たるときだけ要ります)。', result.stdout)
        self.assertNotIn('- jq(', result.stdout)

    def test_optional_tool_is_never_counted(self):
        result = self.run_script(path=self.tools(jq=stub('jq-1.4')))
        self.row(result.stdout, 'jq', '1.4', '1.5 以上', '足りない(任意)')
        self.assertIn('足りない道具はありません。', result.stdout)
        result = self.run_script(path=self.tools(jq=stub('jq version unknown')))
        self.row(result.stdout, 'jq', '-', '1.5 以上', '版を読めない(任意)')
        self.assertIn('足りない道具はありません。', result.stdout)

    def test_unreadable_and_not_gnu(self):
        result = self.run_script(path=self.tools(bash=stub('GNU bash, version unknown'),
                                                 sort=stub('sort 2.3-Apple (165.100.4)'),
                                                 find=stub('find (GNU findutils)')))
        self.assertEqual(0, result.returncode, result.stderr)
        self.row(result.stdout, 'bash', '-', '4.0 以上', '版を読めない')
        self.row(result.stdout, 'bash', '-', '4.4 以上', '版を読めない(条件つき)')
        self.row(result.stdout, 'GNU coreutils', '-', '-', 'GNU の版でない(sort)')
        self.row(result.stdout, 'GNU findutils', '-', '-', '版を読めない(find)')

    def test_lowest_gnu_version_is_compared(self):
        result = self.run_script(path=self.tools(cp=stub('cp (GNU coreutils) 8.28')))
        self.row(result.stdout, 'GNU coreutils', '8.28', '8.30 以上', '足りない(条件つき)')

    def test_lowest_version_of_several_commands_is_shown(self):
        result = self.run_script(path=self.tools(realpath=stub('realpath (GNU coreutils) 9.4'),
                                                 sort=stub('sort (GNU coreutils) 8.28')))
        self.row(result.stdout, 'GNU coreutils', '8.28', '-', '足りる')
        result = self.run_script(path=self.tools(realpath=stub('realpath (GNU coreutils) 8.28'),
                                                 sort=stub('sort (GNU coreutils) 9.4')))
        self.row(result.stdout, 'GNU coreutils', '8.28', '-', '足りる')

    def test_pyyaml_and_python3_missing(self):
        result = self.run_script(path=self.tools(python3="#!/bin/sh\ncase $1 in --version) printf 'Python 3.12.3\\n' ;; *) exit 1 ;; esac\n"))
        self.row(result.stdout, 'PyYAML', '-', '-', '無い(条件つき)')
        result = self.run_script(path=self.tools(python3=None))
        self.row(result.stdout, 'python3', '-', '3.8 以上', '無い')
        self.row(result.stdout, 'PyYAML', '-', '-', '無い(python3 が無い)(条件つき)')

    def test_runs_without_bash_and_python3(self):
        result = self.run_script(path=self.tools(bash=None, python3=None))
        self.assertEqual(0, result.returncode, result.stderr)
        self.row(result.stdout, 'bash', '-', '4.0 以上', '無い')
        self.row(result.stdout, 'bash', '-', '4.4 以上', '無い(条件つき)')

    def test_conditional_rows_are_marked_and_counted(self):
        result = self.run_script(path=self.tools(bash=stub('GNU bash, version 4.2.46(2)-release')))
        self.row(result.stdout, 'bash', '4.2.46', '4.0 以上', '足りる')
        self.row(result.stdout, 'bash', '4.2.46', '4.4 以上', '足りない(条件つき)')
        self.assertIn('足りない道具が 1 件あります(うち 1 件は条件つき。使う所に当たるときだけ要ります)。', result.stdout)
        self.assertIn('- bash(無人ループ(loop.sh)): ', result.stdout)

    def test_versions_are_compared_by_numbers(self):
        cases = {
            'git version 2.9.5': ('2.9.5', '足りない'),
            'git version 2.42': ('2.42', '足りる'),
            'git version 2.100.1': ('2.100.1', '足りる'),
            'git version 10.0': ('10.0', '足りる'),
            'git version 2.041.0': ('2.041.0', '足りない'),
            'git version 2.042.0': ('2.042.0', '足りる'),
            'git version 2.1234567890': ('2.1234567890', '版を読めない'),
            'git version 3.1234567890': ('3.1234567890', '版を読めない'),
            'git version 2.39.5 (Apple Git-154)': ('2.39.5', '足りない'),
        }
        for line, (found, verdict) in cases.items():
            with self.subTest(line=line):
                result = self.run_script(path=self.tools(git=stub(line)))
                self.assertEqual(0, result.returncode, result.stderr)
                self.row(result.stdout, 'git', found, '2.42 以上', verdict)
        result = self.run_script(path=self.tools(python3="#!/bin/sh\ncase $1 in --version) printf 'Python 3.9.18\\n' ;; -B) printf '6.0.3' ;; esac\n"))
        self.row(result.stdout, 'python3', '3.9.18', '3.8 以上', '足りる')
        self.row(result.stdout, 'python3', '3.9.18', '3.10 以上', '足りない(条件つき)')

    def test_loop_iteration_does_nothing(self):
        result = self.run_script('--session-start', DEV_WORKFLOW_LOOP_ITER='run-1-1')
        self.assertEqual((0, '', ''), (result.returncode, result.stdout, result.stderr))
        self.assertFalse(self.state.exists())

    def test_unwritable_state_still_reports(self):
        self.state.write_text('not a directory')
        for _ in range(2):
            message = self.session()
            self.assertIn('確かめた版を記録できませんでした。次の起動でも知らせます。', message)

    def test_relative_xdg_state_home_falls_back_to_home(self):
        self.session(XDG_STATE_HOME='relative/state')
        self.assertEqual(plugin_version() + '\n',
                         (self.home / '.local/state/dev-workflow/tool-versions/checked-version').read_text())

    def test_usage_error(self):
        for args in (['--unknown'], ['--session-start', 'x']):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(2, result.returncode)
                self.assertEqual('', result.stdout)

    def test_syntax(self):
        result = subprocess.run([SH, '-n', str(SCRIPT)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(0, result.returncode, result.stderr)


class ListTests(unittest.TestCase):
    def test_list_format(self):
        raw = LIST.read_bytes()
        self.assertTrue(raw.endswith(b'\n'))
        self.assertIsNone(re.search(rb'[\x00-\x08\x0b-\x1f\x7f]', raw))
        rows = list_rows()
        self.assertGreater(len(rows), 0)
        probe = re.compile(r'(version:[a-z0-9]+|gnu:[a-z]+:[a-z0-9]+(,[a-z0-9]+)*|python-module:[A-Za-z0-9_]+|command:[a-z0-9]+(,[a-z0-9]+)*)\Z')
        for row in rows:
            with self.subTest(row=row[0]):
                self.assertEqual(6, len(row))
                self.assertTrue(all(field.strip() == field and field for field in row))
                self.assertRegex(row[1], probe)
                self.assertRegex(row[2], r'\A(-|[0-9]+(\.[0-9]+)*)\Z')
                self.assertIn(row[3], ('必須', '条件つき', '任意'))
                self.assertNotIn('|', ''.join(row))

    def test_script_has_no_minimum_versions(self):
        text = SCRIPT.read_text(encoding='utf-8')
        for row in list_rows():
            if row[2] != '-':
                self.assertNotIn(row[2], text, row[0])

    def test_runtime_requirements_table_is_the_list(self):
        text = RUNTIME.read_text(encoding='utf-8')
        self.assertEqual(1, text.count(TABLE_START))
        section = text.split(TABLE_START, 1)[1].split(TABLE_END, 1)[0]
        lines = [line for line in section.splitlines() if line.startswith('|')]
        self.assertEqual('| 道具 | 最低の版 | 区分 | 使う所 |', lines[0])
        table = [[cell.strip() for cell in line.strip('|').split('|')] for line in lines[2:]]
        self.assertEqual([[row[0], row[2], row[3], row[4]] for row in list_rows()], table)

    def test_hooks_json_is_the_fixed_text_of_host_check(self):
        host_check = load_script(HOST_CHECK, 'host_check_for_tool_versions')
        self.assertEqual(host_check.WORKFLOW_HOOKS_JSON, HOOKS_JSON.read_bytes())
        value = json.loads(HOOKS_JSON.read_text(encoding='utf-8'))
        [entry] = value['hooks']['SessionStart']
        [hook] = entry['hooks']
        self.assertNotIn('matcher', entry)
        self.assertEqual('sh "${CLAUDE_PLUGIN_ROOT}/hooks/tool-versions.sh" --session-start', hook['command'])
        self.assertEqual(['SessionStart'], list(value['hooks']))


if __name__ == '__main__':
    unittest.main()
