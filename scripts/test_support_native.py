"""旧版と Rust 版の比較用補助を、一時フォルダの実行ファイルだけで検査する。"""
import ctypes
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

try:
    import support_native
except ModuleNotFoundError as error:
    if error.name != 'support_native':
        raise
    from scripts import support_native
try:
    import support_proc
except ModuleNotFoundError as error:
    if error.name != 'support_proc':
        raise
    from scripts import support_proc


NATIVE_ENV = 'DEV_WORKFLOW_TEST_NATIVE_BINARY'


class TemporaryScripts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wf-native-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.script_count = 0

    def script(self, body):
        self.script_count += 1
        path = self.root / f'mock-{self.script_count}.py'
        path.write_text(f'#!{sys.executable} -B\n' + textwrap.dedent(body), encoding='utf-8')
        path.chmod(0o755)
        return path


class StandaloneImportTest(TemporaryScripts):
    def test_helper_imports_without_site_packages_or_other_support_files(self):
        source = Path(support_native.__file__)
        standalone = self.root / source.name
        standalone.write_bytes(source.read_bytes())
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
                                 'import runpy, sys; runpy.run_path(sys.argv[1])', str(standalone)],
                                cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(b'', result.stdout)


class RequireNativeBinaryTest(TemporaryScripts):
    def test_missing_and_empty_are_errors_even_with_a_binary_on_path(self):
        binary = self.script('raise SystemExit(0)')
        binary.rename(self.root / 'dev-workflow')
        for value, message in ((None, '未設定'), ('', '空')):
            with self.subTest(value=value), mock.patch.dict(
                    os.environ, {'PATH': str(self.root)}, clear=True):
                if value is not None:
                    os.environ[NATIVE_ENV] = value
                with self.assertRaisesRegex(support_native.NativeBinaryError, message):
                    support_native.require_native_binary()

    def test_missing_file_non_executable_directory_and_broken_link_are_errors(self):
        not_executable = self.script('raise SystemExit(0)')
        not_executable.chmod(0o644)
        broken = self.root / 'broken'
        broken.symlink_to('missing')
        cycle = self.root / 'cycle'
        cycle.symlink_to('cycle')
        for path in (self.root / 'missing', not_executable, self.root, broken, cycle):
            with self.subTest(path=path), mock.patch.dict(os.environ, {NATIVE_ENV: str(path)}):
                with self.assertRaises(support_native.NativeBinaryError):
                    support_native.require_native_binary()
        self.assertEqual(0o644, not_executable.stat().st_mode & 0o777)

    def test_returns_absolute_path_without_running_binary(self):
        marker = self.root / 'ran'
        binary = self.script(f'from pathlib import Path\nPath({str(marker)!r}).touch()')
        relative = os.path.relpath(binary)
        with mock.patch.dict(os.environ, {NATIVE_ENV: relative}):
            self.assertEqual(binary, support_native.require_native_binary())
        self.assertFalse(marker.exists())
        link = self.root / 'binary-link'
        link.symlink_to(binary.name)
        with mock.patch.dict(os.environ, {NATIVE_ENV: str(link)}):
            self.assertEqual(binary, support_native.require_native_binary())

    def test_does_not_trim_path(self):
        binary = self.script('pass').rename(self.root / ' binary with spaces ')
        with mock.patch.dict(os.environ, {NATIVE_ENV: str(binary)}):
            self.assertEqual(binary, support_native.require_native_binary())


class RunCommandTest(TemporaryScripts):
    def test_captures_bytes_input_arguments_cwd_and_nonzero_exit(self):
        work = self.root / 'work'
        work.mkdir()
        script = self.script('''
            import os, sys
            from pathlib import Path
            Path('argument').write_text(sys.argv[1])
            os.write(1, sys.stdin.buffer.read() + b'\\xff\\r\\n')
            os.write(2, b'\\x00\\xfeerror\\n')
            raise SystemExit(7)
        ''')
        result = support_native.run_command([script, 'a; echo nope'], cwd=work, input=b'\x00in\r\n')
        self.assertEqual(support_native.RunResult(7, b'\x00in\r\n\xff\r\n', b'\x00\xfeerror\n', False), result)
        self.assertEqual('a; echo nope', (work / 'argument').read_text())
        self.assertFalse((self.root / 'argument').exists())

    def test_none_input_is_eof(self):
        script = self.script('import sys\nprint(len(sys.stdin.buffer.read()))')
        result = support_native.run_command([script], cwd=self.root)
        self.assertEqual(b'0\n', result.stdout)
        self.assertFalse(result.timed_out)

    def test_env_none_empty_mapping_unset_and_empty_value(self):
        name = 'WF_NATIVE_FIXTURE_VALUE'
        script = self.script(f'import os\nprint(repr(os.environ.get({name!r})))')
        with mock.patch.dict(os.environ, {name: 'inherited'}):
            for env, expected in ((None, b"'inherited'\n"), ({}, b'None\n'),
                                  ({name: ''}, b"''\n"), ({name: 'set'}, b"'set'\n")):
                with self.subTest(env=env):
                    result = support_native.run_command([script], cwd=self.root, env=env)
                    self.assertEqual(expected, result.stdout)
        self.assertEqual({name: 'set'}, env)

    def test_failed_exec_raises_without_using_a_shell_or_changing_mode(self):
        plain = self.root / 'not-a-program'
        plain.write_text('this is not an executable format')
        blocked = self.script('pass')
        blocked.chmod(0o644)
        plain.chmod(0o755)
        for path in (self.root / 'missing', plain, blocked, self.root):
            with self.subTest(path=path), self.assertRaises(OSError):
                support_native.run_command([path], cwd=self.root)
        self.assertEqual(0o644, blocked.stat().st_mode & 0o777)

    def test_bad_arguments_fail_before_starting(self):
        marker = self.root / 'ran'
        script = self.script(f'from pathlib import Path\nPath({str(marker)!r}).touch()')
        for kwargs, error in (({'input': 'text'}, TypeError), ({'timeout': -1}, ValueError)):
            with self.subTest(kwargs=kwargs), self.assertRaises(error):
                support_native.run_command([script], cwd=self.root, **kwargs)
        with self.assertRaises(TypeError):
            support_native.run_command(str(script), cwd=self.root)
        self.assertFalse(marker.exists())

    def test_timeout_keeps_partial_output_and_reaps_the_process(self):
        record = self.root / 'process.json'
        script = self.script(f'''
            import json, os, signal, time
            from pathlib import Path
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            {support_proc.record(record)}
            os.write(1, b'partial\\xff')
            os.write(2, b'error\\x00')
            time.sleep(60)
        ''')
        self.addCleanup(support_proc.cleanup, self.root, records=[record])
        started = time.monotonic()
        result = support_native.run_command([script], cwd=self.root, timeout=2)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(support_native.RunResult(-signal.SIGKILL, b'partial\xff', b'error\x00', True), result)
        pid, pgid = support_proc.read_record(record)
        support_proc.assert_absent_now(pid)
        support_proc.assert_group_absent_now(pgid)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_term_handler_can_finish_and_reap_its_child(self):
        parent_record, child_record = self.root / 'parent.json', self.root / 'child.json'
        child = self.script(f'''
            import json, os, time
            from pathlib import Path
            {support_proc.record(child_record)}
            time.sleep(60)
        ''')
        parent = self.script(f'''
            import json, os, signal, subprocess, time
            from pathlib import Path
            child = subprocess.Popen([{str(child)!r}])
            def stop(*args):
                child.wait()
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, stop)
            {support_proc.record(parent_record)}
            while True:
                time.sleep(.01)
        ''')
        self.addCleanup(support_proc.cleanup, self.root, records=[parent_record, child_record])
        result = support_native.run_command([parent], cwd=self.root, timeout=2)
        self.assertTrue(result.timed_out)
        self.assertEqual(0, result.returncode)
        for record in (parent_record, child_record):
            pid, pgid = support_proc.read_record(record)
            support_proc.assert_absent_now(pid)
            support_proc.assert_group_absent_now(pgid)


@unittest.skipUnless(sys.platform.startswith('linux'), '孤児の停止と回収の区別には Linux の subreaper が必要')
class TimeoutDescendantsTest(TemporaryScripts):
    def setUp(self):
        super().setUp()
        self.libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        self.assertEqual(0, self.libc.prctl(37, ctypes.byref(old), 0, 0, 0))
        self.assertEqual(0, self.libc.prctl(36, 1, 0, 0, 0))
        self.addCleanup(self.libc.prctl, 36, old.value, 0, 0, 0)

    def test_timeout_kills_descendant_even_if_parent_exits_or_pipes_close(self):
        for exit_parent, close_pipes in ((False, False), (True, False), (False, True)):
            with self.subTest(exit_parent=exit_parent, close_pipes=close_pipes):
                parent_record = self.root / f'parent-{exit_parent}-{close_pipes}.json'
                child_record = self.root / f'child-{exit_parent}-{close_pipes}.json'
                script = self.script(f'''
                    import json, os, signal, time
                    from pathlib import Path
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    {support_proc.record(parent_record)}
                    child = os.fork()
                    if child == 0:
                        {support_proc.record(child_record)}
                        if {close_pipes!r}:
                            os.close(1)
                            os.close(2)
                        time.sleep(60)
                    elif {exit_parent!r}:
                        os._exit(0)
                    else:
                        if {close_pipes!r}:
                            signal.signal(signal.SIGTERM, lambda *args: os._exit(0))
                        time.sleep(60)
                ''')
                try:
                    result = support_native.run_command([script], cwd=self.root, timeout=2)
                    self.assertTrue(result.timed_out)
                    parent_pid, pgid = support_proc.read_record(parent_record)
                    child_pid, child_pgid = support_proc.read_record(child_record)
                    self.assertEqual(pgid, child_pgid)
                    support_proc.assert_absent_now(parent_pid)
                    # このテストが引き取った孤児だけを回収する。生存中なら成功にしない。
                    deadline = time.monotonic() + 2
                    while True:
                        found, status = os.waitpid(child_pid, os.WNOHANG)
                        if found or time.monotonic() >= deadline:
                            break
                        time.sleep(.01)
                    self.assertEqual(child_pid, found, '時間切れ後も子が動いている')
                    self.assertTrue(os.WIFSIGNALED(status))
                    self.assertEqual(signal.SIGKILL, os.WTERMSIG(status))
                    support_proc.assert_absent_now(child_pid)
                    support_proc.assert_group_absent_now(pgid)
                finally:
                    self.assertEqual([], support_proc.cleanup(self.root, records=[parent_record, child_record]))


class SnapshotTreeTest(TemporaryScripts):
    def test_hashes_contents_modes_hidden_files_and_directories(self):
        (self.root / 'empty').mkdir(mode=0o750)
        (self.root / 'empty').chmod(0o750)
        path = self.root / '.data'
        data = b'\x00\xff' * (1024 * 1024 + 1)
        path.write_bytes(data)
        path.chmod(0o751)
        snapshot = support_native.snapshot_tree(self.root)
        self.assertEqual({'.', '.data', 'empty'}, set(snapshot))
        self.assertEqual({'kind': 'file', 'mode': 0o751, 'sha256': hashlib.sha256(data).hexdigest()}, snapshot['.data'])
        self.assertEqual({'kind': 'directory', 'mode': 0o750, 'sha256': None}, snapshot['empty'])
        os.utime(path, (1, 1))
        self.assertEqual(snapshot, support_native.snapshot_tree(self.root))
        path.chmod(0o750)
        self.assertNotEqual(snapshot, support_native.snapshot_tree(self.root))

    def test_links_are_hashed_without_following_them_including_broken_and_cyclic_links(self):
        for name, target in (('broken', 'missing'), ('loop', '.'), ('outside', '/not-read')):
            (self.root / name).symlink_to(target)
        snapshot = support_native.snapshot_tree(self.root)
        self.assertEqual({'.', 'broken', 'loop', 'outside'}, set(snapshot))
        self.assertEqual('symlink', snapshot['broken']['kind'])
        self.assertEqual(hashlib.sha256(b'missing').hexdigest(), snapshot['broken']['sha256'])
        (self.root / 'broken').unlink()
        (self.root / 'broken').symlink_to('another-missing')
        self.assertNotEqual(snapshot['broken'], support_native.snapshot_tree(self.root)['broken'])

    def test_fifo_is_not_opened_and_missing_root_is_empty(self):
        os.mkfifo(self.root / 'pipe', 0o600)
        self.assertEqual({'kind': 'fifo', 'mode': 0o600, 'sha256': None},
                         support_native.snapshot_tree(self.root)['pipe'])
        self.assertEqual({}, support_native.snapshot_tree(self.root / 'missing'))


class CompareRunsTest(TemporaryScripts):
    def setUp(self):
        super().setUp()
        self.source = self.root / 'initial'
        self.source.mkdir()
        self.source.chmod(0o755)
        (self.source / 'keep').write_bytes(b'initial')
        (self.source / 'keep').chmod(0o640)
        (self.source / 'remove').write_bytes(b'delete me')
        (self.source / 'empty').mkdir()
        (self.source / 'empty').chmod(0o755)
        (self.source / 'broken').symlink_to('missing')
        self.noop = self.script('pass')
        self.work = self.root / 'comparison'

    def compare(self, old, new, **kwargs):
        return support_native.compare_runs([old], [new], initial_tree=self.source, work_dir=self.work, **kwargs)

    def test_same_initial_state_mutations_and_raw_results_match(self):
        before = support_native.snapshot_tree(self.source)
        script = self.script('''
            import os, sys
            from pathlib import Path
            assert Path('keep').read_bytes() == b'initial'
            assert Path('keep').stat().st_mode & 0o777 == 0o640
            assert os.readlink('broken') == 'missing'
            Path('keep').write_bytes(b'changed')
            Path('keep').chmod(0o750)
            Path('remove').unlink()
            Path('created').write_bytes(sys.stdin.buffer.read())
            os.write(1, os.environ['SAME'].encode() + b'\\xff\\n')
            os.write(2, b'\\x00err')
            raise SystemExit(2)
        ''')
        result = self.compare(script, script, env={'SAME': 'both'}, input=b'input\x00')
        self.assertTrue(result.matches, result.differences)
        self.assertEqual((), result.differences)
        self.assertEqual(result.legacy, result.native)
        self.assertEqual(support_native.RunResult(2, b'both\xff\n', b'\x00err', False), result.legacy)
        self.assertEqual(before, result.before)
        self.assertEqual(before, support_native.snapshot_tree(self.source))
        self.assertEqual(b'input\x00', (result.legacy_cwd / 'created').read_bytes())
        self.assertNotEqual((result.legacy_cwd / 'keep').stat().st_ino, (result.native_cwd / 'keep').stat().st_ino)
        self.assertEqual({'remove'}, result.before.keys() - result.native_tree.keys())
        self.assertEqual({'created'}, result.native_tree.keys() - result.before.keys())

    def test_output_bytes_and_exit_code_differences_are_separate(self):
        old = self.script("import os\nos.write(1, b'old\\xff\\r\\n')\nos.write(2, b'err\\x00')")
        new = self.script("import os\nos.write(1, b'old\\xfe\\n')\nos.write(2, b'err')\nraise SystemExit(3)")
        result = self.compare(old, new)
        self.assertFalse(result.matches)
        self.assertEqual(('returncode', 'stdout', 'stderr'), result.differences)

    def test_creation_deletion_content_mode_directory_and_type_differences(self):
        changes = {
            'created': "Path('created').touch()",
            'remove': "Path('remove').unlink()",
            'keep': "Path('keep').write_bytes(b'changed')",
            'empty': "Path('empty').chmod(0o700)",
            'broken': "Path('broken').unlink(); Path('broken').mkdir()",
            '.': "Path('.').chmod(0o700)",
        }
        for name, body in changes.items():
            with self.subTest(name=name):
                work = self.root / f'result-{name}'
                script = self.script('from pathlib import Path\n' + body)
                result = support_native.compare_runs([self.noop], [script], initial_tree=self.source, work_dir=work)
                self.assertEqual((f'tree:{name}',), result.differences)
        script = self.script("from pathlib import Path\nPath('keep').chmod(0o740)")
        result = self.compare(self.noop, script)
        self.assertEqual(('tree:keep',), result.differences)
        self.assertEqual(result.legacy_tree['keep']['sha256'], result.native_tree['keep']['sha256'])

    def test_paths_and_timestamps_in_output_are_not_normalized(self):
        script = self.script("from pathlib import Path\nprint(Path.cwd())\nprint('2026-11-08T00:00:01Z')")
        result = self.compare(script, script)
        self.assertEqual(('stdout',), result.differences)
        self.assertIn(os.fsencode(result.legacy_cwd), result.legacy.stdout)
        self.assertIn(b'2026-11-08T00:00:01Z', result.legacy.stdout)
        old = self.script("print('2026-11-08T00:00:01Z')")
        new = self.script("print('2026-11-08T00:00:02Z')")
        result = support_native.compare_runs([old], [new], initial_tree=self.source,
                                             work_dir=self.root / 'timestamps')
        self.assertEqual(('stdout',), result.differences)

    def test_explicit_allowances_apply_only_to_exact_field_and_value(self):
        old = self.script("import os\nos.write(1, b'old\\n')\nos.write(2, b'keep\\n')")
        new = self.script("import os\nos.write(1, b'new\\n')\nos.write(2, b'different\\n')")
        result = self.compare(old, new, allow_differences={
            'stdout': lambda old, new: old == b'old\n' and new == b'new\n',
            'stderr': lambda old, new: False,
        })
        self.assertEqual(('stderr',), result.differences)
        self.assertEqual(b'new\n', result.native.stdout)
        new = self.script("import os\nos.write(1, b'new\\n')\nos.write(2, b'keep\\n')")
        result = support_native.compare_runs([old], [new], initial_tree=self.source,
                                             work_dir=self.root / 'allowed', allow_differences={
                                                 'stdout': lambda old, new: (old, new) == (b'old\n', b'new\n')})
        self.assertTrue(result.matches)
        strict = support_native.compare_runs([old], [new], initial_tree=self.source,
                                             work_dir=self.root / 'strict')
        self.assertEqual(('stdout',), strict.differences)

    def test_tree_allowance_gets_missing_value_without_hiding_other_files(self):
        script = self.script("from pathlib import Path\nPath('one').touch()\nPath('two').touch()")
        seen = []

        def allow(old, new):
            seen.append((old, new))
            return old is None and new['kind'] == 'file'

        result = self.compare(self.noop, script, allow_differences={'tree:one': allow})
        self.assertEqual(('tree:two',), result.differences)
        self.assertEqual([(None, result.native_tree['one'])], seen)

    def test_invalid_allowance_fails_before_any_run(self):
        for allowance in ({'stdot': lambda *_: True}, {'native.timed_out': lambda *_: True}, {'stdout': True}):
            with self.subTest(allowance=allowance), self.assertRaises((ValueError, TypeError)):
                self.compare(self.noop, self.noop, allow_differences=allowance)
        self.assertFalse(self.work.exists())

    def test_timeout_on_both_sides_is_not_a_match(self):
        script = self.script('import time\ntime.sleep(60)')
        result = self.compare(script, script, timeout=.1)
        self.assertFalse(result.matches)
        self.assertEqual(('legacy.timed_out', 'native.timed_out'), result.differences)
        self.assertEqual(result.legacy, result.native)

    def test_failed_exec_keeps_copies_and_raises(self):
        script = self.script('pass')
        script.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.compare(self.noop, script)
        self.assertTrue((self.work / 'legacy' / 'keep').exists())
        self.assertTrue((self.work / 'native' / 'keep').exists())

    def test_work_directory_must_be_new_and_outside_source(self):
        with self.assertRaises(ValueError):
            support_native.compare_runs([self.noop], [self.noop], initial_tree=self.source,
                                        work_dir=self.source / 'recursive')
        self.work.mkdir()
        (self.work / 'keep').write_text('do not replace')
        with self.assertRaises(FileExistsError):
            self.compare(self.noop, self.noop)
        self.assertEqual('do not replace', (self.work / 'keep').read_text())


if __name__ == '__main__':
    unittest.main()
