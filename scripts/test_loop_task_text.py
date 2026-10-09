"""本文解析の既存結果と、親shellの保持・起動契約を確認する。"""
import json
import os
import shutil
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts'
LOOP = SCRIPTS / 'loop.sh'
PARSER = SCRIPTS / 'loop-task-text.py'


def embedded():
    return LOOP.read_text().split("read -r -d '' PY_HELPER <<'PY' || true\n", 1)[1].split('\nPY\npy()', 1)[0]


def shell_function(name):
    source = LOOP.read_text()
    return name + '() {' + source.split(name + '() {', 1)[1].split('\n}\n', 1)[0] + '\n}\n'


def parent_shell():
    source = LOOP.read_text()
    initialization = source.split('unset TASK_TEXT_CODE TASK_TEXT_HELD', 1)[1].split('TASK_TEXT_HELD=0', 1)[0]
    return ('set -euo pipefail\nunset TASK_TEXT_CODE TASK_TEXT_HELD' + initialization + 'TASK_TEXT_HELD=0\n'
            + 'PY_HELPER=' + shlex.quote(embedded()) + '\n'
            + 'PY_ABS=' + shlex.quote(sys.executable) + '\n'
            + 'FIXED_POLICY="{}"\n'
            + 'die() { printf "ERROR [%s] %s\\n" "$2" "$3" >&2; exit "$1"; }\n'
            + ''.join(shell_function(name) for name in ('py', 'environment_call', 'hold_task_text', 'bind_environment')))


class TaskTextTests(unittest.TestCase):
    via_parent = False
    def run_parser(self, command, data=b'', *, file=False, args=None):
        code = PARSER.read_text()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'task.md'
            path.write_bytes(data)
            argv = args if args is not None else [str(path) if file else '-']
            invocation = [sys.executable, '-c', code, command, *argv]
            if self.via_parent:
                shell = parent_shell() + 'TASK_TEXT_HELD=1\nTASK_TEXT_CODE=' + shlex.quote(code) + '\npy "$@"\n'
                invocation = ['bash', '-c', shell, '--', command, *argv]
            result = subprocess.run(invocation, input=data,
                                    capture_output=True, timeout=10, cwd='/')
            return result

    def check(self, command, data, output):
        for file in (False, True):
            with self.subTest(command=command, file=file, data=data):
                result = self.run_parser(command, data, file=file)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, output, b''))

    def test_empty(self):
        self.check('taskinfo', b'', b'meta=0\ndate=\n')
        self.check('holdcount', b'', b'0\n')
        self.check('holdcode', b'', b'\n')

    def test_bom_newlines_and_header_boundary(self):
        data = '\ufeff# task\r> **無人実行**: 可\r\n> **作成日**: 2026-10-09\r## body\r> **作成日**: 2000-01-01'.encode()
        self.check('taskinfo', data, b'meta=1\ndate=2026-10-09\n')
        self.check('taskinfo', b'## body\n' + data, b'meta=0\ndate=\n')
        self.check('taskinfo', b'\xff\n' + '> **無人実行**: 可'.encode(), b'meta=1\ndate=\n')
        self.check('taskinfo', ('\ufeff\ufeff> **無人実行**: 可').encode(), b'meta=0\ndate=\n')

    def test_fenced_header(self):
        data = '```\n## false\n> **作成日**: 2000-01-01\n```\n> **無人実行**: 可\n## real\n'
        self.check('taskinfo', data.encode(), b'meta=1\ndate=\n')

    def test_multiple_record_sections_and_last_without_code(self):
        hold = '- **保留**(ship-task・2026-10-09): '
        data = hold+'G9 ignored\n## 追加修正記録\n'+hold+'G1 stop\n## other\n'+hold+'G8 ignored\n## 追加修正記録\n'+hold+'D2 stop\n'
        self.check('holdcount', data.encode(), b'2\n')
        self.check('holdcode', data.encode(), b'D2\n')
        self.check('holdcode', (data+hold+'番号なし\n').encode(), b'\n')

    def test_fence_length_kind_indent_and_unclosed(self):
        hold = '- **保留**(ship-task・2026-10-09): G1 stop\n'
        for opening, closing in [('```', '```'), ('   ~~~~ info', '  ~~~~~'), ('````', '`````')]:
            self.check('holdcount', ('## 追加修正記録\n'+opening+'\n'+hold+closing+'\n'+hold).encode(), b'1\n')
        for closing in ['~~~', '``', '``` trailing', '    ```']:
            self.check('holdcount', ('## 追加修正記録\n```\n'+hold+closing+'\n'+hold).encode(), b'0\n')
        self.check('holdcount', ('## 追加修正記録\n    ```\n'+hold).encode(), b'1\n')
        self.check('holdcode', ('## 追加修正記録\n'+hold+'~~~\n'+hold.replace('G1', 'D2')).encode(), b'G1\n')

    def test_error_contract(self):
        for command in ('taskinfo', 'holdcount', 'holdcode'):
            for args in ([], ['-', 'extra'], ['/nonexistent-loop-task-text-fixture']):
                with self.subTest(command=command, args=args):
                    result = self.run_parser(command, args=args)
                    self.assertEqual((result.returncode, result.stdout), (9, b''))
                    self.assertTrue(result.stderr.startswith('補助の失敗: '.encode()), result.stderr)
                    self.assertIn(b'TypeError' if len(args) != 1 else b'FileNotFoundError', result.stderr)



class ParentTaskTextTests(TaskTextTests):
    via_parent = True


@unittest.skipUnless(sys.platform == "linux", "信頼コピーの固定 /etc 検査はLinux契約")
class HeldTaskTextTests(unittest.TestCase):
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
        self.relative = Path('skills/ship-task/scripts/loop-task-text.py')
        self.source = self.root / self.relative
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(PARSER.read_bytes())
        (self.root / '.claude-plugin').mkdir()
        (self.root / '.claude-plugin/plugin.json').write_text('{}')
        for name in ('loop-permission', 'origin-repo', 'git-config-digest', 'loop-state',
                     'loop-startup', 'host-argv', 'loop-supervisor', 'host-check'):
            self.source.with_name(name + '.py').write_text('# fixture\n')
        resolver = self.root / 'skills/create-task/scripts/resolve-task-dir.py'
        resolver.parent.mkdir(parents=True)
        resolver.write_text('# fixture\n')
        self.sequence = 0

    def boot(self, root=None):
        self.sequence += 1
        result = subprocess.run([sys.executable, '-I', '-B', str(SCRIPTS / 'environment-guard.py'),
                                 'bootstrap', '--root', str(root or self.root), '--output',
                                 str(self.base / ('bundle-' + str(self.sequence)))],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def run_shell(self, body, *, data=b'', env=None, cwd=None):
        return subprocess.run(['bash', '-c', parent_shell() + body], input=data,
                              capture_output=True, timeout=25, cwd=cwd or self.base, env=env or self.env)

    def bind(self, receipt):
        return 'bind_environment ' + shlex.quote(json.dumps(receipt)) + '\n'

    def test_real_copy_rebind_and_retained_bytes(self):
        first, active = self.boot(), self.boot()
        body = self.bind(first) + 'initial="$TASK_TEXT_CODE"\n' + self.bind(active)
        body += '[ "$initial" = "$TASK_TEXT_CODE" ]\n'
        body += 'printf "%s" "$TASK_TEXT_CODE" > ' + shlex.quote(str(self.base / 'held')) + '\n'
        # 解析時は再読しない。原本も保持コピーも無くしてから実際のpyを呼ぶ。
        body += 'rm -- ' + shlex.quote(str(self.source)) + ' ' + shlex.quote(str(Path(active['plugin']) / self.relative)) + '\n'
        body += 'py taskinfo -\n'
        result = self.run_shell(body, data='> **無人実行**: 可\n'.encode())
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'meta=1\ndate=\n', b''))
        self.assertEqual((self.base / 'held').read_bytes(), PARSER.read_bytes())

    def test_rebind_rejects_changed_content_and_trailing_newline(self):
        first = self.boot()
        for suffix in (b'\n', b'\nprint("must-not-run")\n'):
            with self.subTest(suffix=suffix):
                other = self.base / ('other-' + str(self.sequence))
                shutil.copytree(self.root, other)
                (other / self.relative).write_bytes(PARSER.read_bytes() + suffix)
                active = self.boot(other)
                held = self.base / 'held-after-reject'
                body = self.bind(first)
                body += 'trap \'printf "%s" "$TASK_TEXT_CODE" > ' + shlex.quote(str(held)) + "' EXIT\n"
                body += self.bind(active) + 'echo must-not-run\n'
                result = self.run_shell(body)
                self.assertEqual((result.returncode, result.stdout), (20, b''), result.stderr)
                self.assertIn(b'ERROR [environment]', result.stderr)
                self.assertEqual(held.read_bytes(), PARSER.read_bytes())

    def test_invalid_source_bytes_rejected_before_shell_conversion(self):
        for raw in (b'', b'# valid before NUL\0\nprint("must-not-run")\n', b'# \xff\n'):
            with self.subTest(raw=raw):
                self.source.write_bytes(raw)
                receipt = self.boot()
                result = self.run_shell(self.bind(receipt) + 'echo must-not-run\n')
                self.assertEqual((result.returncode, result.stdout), (20, b''), result.stderr)
                self.assertIn(b'ERROR [environment]', result.stderr)
                self.assertNotIn(b'ignored null byte', result.stderr)

    def test_read_failure_is_not_hidden_by_sentinel(self):
        body = 'environment_call() { printf "print(123)\\n"; return 7; }\n'
        body += 'ENVIRONMENT_STATE=unused\nENVIRONMENT_SHA=unused\nhold_task_text\necho must-not-run\n'
        result = self.run_shell(body)
        self.assertEqual((result.returncode, result.stdout), (20, b''), result.stderr)
        self.assertIn(b'ERROR [environment]', result.stderr)

    def test_missing_helper_in_initial_bundle_is_rejected(self):
        self.source.unlink()
        result = self.run_shell(self.bind(self.boot()) + 'echo must-not-run\n')
        self.assertEqual((result.returncode, result.stdout), (20, b''), result.stderr)

    def test_real_read_rejects_original_and_copy_changes(self):
        original = PARSER.read_bytes()
        for location in ('original', 'copy'):
            for mutation in ('change', 'missing', 'symlink', 'fifo', 'unreadable'):
                with self.subTest(location=location, mutation=mutation):
                    if self.source.exists() or self.source.is_symlink():
                        self.source.unlink()
                    self.source.write_bytes(original)
                    receipt = self.boot()
                    target = self.source if location == 'original' else Path(receipt['plugin']) / self.relative
                    target.unlink()
                    if mutation == 'change':
                        target.write_text('print("must-not-run")\n')
                    elif mutation == 'symlink':
                        target.symlink_to(PARSER)
                    elif mutation == 'fifo':
                        os.mkfifo(target)
                    elif mutation == 'unreadable':
                        target.write_bytes(original)
                        target.chmod(0)
                    result = self.run_shell(self.bind(receipt) + 'echo must-not-run\n')
                    self.assertEqual((result.returncode, result.stdout), (20, b''), result.stderr)
                    self.assertIn(b'ERROR [environment]', result.stderr)
                    if mutation == 'unreadable':
                        target.chmod(0o600)

    def test_exported_values_are_reset_and_not_exported_again(self):
        receipt = self.boot()
        env = dict(self.env, TASK_TEXT_HELD='1', TASK_TEXT_CODE='print("inherited")')
        body = '[ "$TASK_TEXT_HELD" = 0 ] && [ -z "$TASK_TEXT_CODE" ]\n' + self.bind(receipt)
        body += '"$PY_ABS" -I -c \'import os; assert "TASK_TEXT_CODE" not in os.environ; assert "TASK_TEXT_HELD" not in os.environ\'\n'
        body += 'py holdcount -\n'
        result = self.run_shell(body, env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'0\n', b''))

    def test_actual_parent_cwd_fd_stdin_and_pythonpath(self):
        receipt = self.boot()
        cwd = self.base / 'cwd'; cwd.mkdir()
        (cwd / 're.py').write_text('raise RuntimeError("caller cwd imported")\n')
        extra = self.base / 'extra'; extra.mkdir()
        observation = self.base / 'observation.json'
        (extra / 'sitecustomize.py').write_text(
            'import errno,json,os,sys\n'
            'try:\n    os.fstat(7)\n    fd="open"\n'
            'except OSError as exc:\n    fd=exc.errno\n'
            'open(' + repr(str(observation)) + ',"w").write(json.dumps([os.getcwd(), fd, sys.flags.isolated, sys.path]))\n')
        env = dict(self.env, PYTHONPATH=str(extra))
        # bind precedes observer installation in child environment; its isolated byte check ignores it.
        body = self.bind(receipt) + 'exec 7</dev/null\npy taskinfo -\n'
        result = self.run_shell(body, env=env, cwd=cwd, data='> **作成日**: 2026-10-09\n'.encode())
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'meta=0\ndate=2026-10-09\n', b''))
        observed = json.loads(observation.read_text())
        self.assertEqual(observed[:3], ['/', 9, 0])
        self.assertIn(str(extra), observed[3])
        self.assertNotIn(str(PARSER.parent), observed[3])
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
        body = self.bind(receipt) + 'rm -f -- ' + shlex.quote(str(marker)) + '\npy holdcount -\n'
        result = self.run_shell(body, env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'0\n', b''))
        self.assertEqual(marker.read_text(), 'yes')


if __name__ == '__main__':
    unittest.main()
