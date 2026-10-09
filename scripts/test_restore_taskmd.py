"""原子的復元の公開結果と、実ファイル・fd・子の回収を検査する。"""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'plugins/dev-workflow/skills/do-task/scripts/restore-taskmd.py'
SPEC = importlib.util.spec_from_file_location('restore_taskmd_test_target', HELPER)
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)
CP = os.path.realpath(shutil.which('cp'))


def digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            value.update(block)
    return value.hexdigest()


def unquote_path(raw):
    """既存 C 風引用を独立に復号する。UTF-8 復号は行わない。"""
    if not raw.startswith(b'"'):
        return raw
    if not raw.endswith(b'"'):
        raise ValueError('unterminated quote')
    raw = raw[1:-1]
    output = bytearray()
    escapes = {ord('a'): 7, ord('b'): 8, ord('t'): 9, ord('n'): 10,
               ord('v'): 11, ord('f'): 12, ord('r'): 13, ord('"'): 34, ord('\\'): 92}
    i = 0
    while i < len(raw):
        if raw[i] != 92:
            output.append(raw[i]); i += 1; continue
        i += 1
        if raw[i] in escapes:
            output.append(escapes[raw[i]]); i += 1
        else:
            output.append(int(raw[i:i + 3], 8)); i += 3
    return bytes(output)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.source = self.root / 'taskmd-body'
        self.destination = self.root / 'current'
        self.source.write_bytes(b'baseline\x00\xff\n')
        self.source.chmod(0o640)
        self.destination.write_bytes(b'current\x00contents\n')
        self.destination.chmod(0o600)
        self.before = self.destination.read_bytes(), stat.S_IMODE(self.destination.stat().st_mode)
        for number in restore.SIGNALS:
            old = signal.getsignal(number)
            self.addCleanup(signal.signal, number, old)

    def argv(self, **overrides):
        values = dict(source=os.fspath(self.source), destination=os.fspath(self.destination),
                      expected_mode=format(stat.S_IMODE(self.source.stat().st_mode), 'o'),
                      expected_sha256=digest(self.source), cp=CP)
        values.update(overrides)
        result = ['restore']
        for key, value in values.items():
            result.extend(['--' + key.replace('_', '-'), value])
        return result

    def operation(self, **overrides):
        return restore.Restore(restore.parse_arguments(self.argv(**overrides)))

    def cli(self, argv=None):
        return subprocess.run([sys.executable, '-B', str(HELPER), *(argv or self.argv())],
                              capture_output=True, timeout=30)

    def fields(self, rc, output):
        self.assertLessEqual(len(output), 4 * 1024 * 1024)
        self.assertTrue(output.endswith(b'\n'), output)
        lines = output.splitlines()
        self.assertTrue(all(b'=' in line for line in lines), output)
        pairs = [line.split(b'=', 1) for line in lines]
        keys = [pair[0] for pair in pairs]
        self.assertEqual(keys[:3], [b'RESTORED', b'TOUCHED', b'TEMP'])
        values = dict(pairs)
        self.assertEqual(len(values), len(pairs), output)
        self.assertIn(values[b'TOUCHED'], (b'none', b'copied', b'replaced', b'unknown'))
        self.assertIn(values[b'TEMP'], (b'none', b'removed', b'retained', b'unknown'))
        expected_keys = [b'RESTORED', b'TOUCHED', b'TEMP']
        if rc == 0:
            self.assertEqual(values[b'RESTORED'], b'yes')
            self.assertIn(values[b'TOUCHED'], (b'copied', b'replaced'))
            self.assertEqual(values[b'TEMP'], b'removed')
        else:
            self.assertIn(rc, (20, 33))
            self.assertEqual(values[b'RESTORED'], b'no')
            expected_keys.append(b'REASON')
            self.assertIn(values[b'REASON'], tuple(x.encode() for x in (
                'internal-argument', 'runtime-unavailable', 'taskmd-body', 'digest',
                'dest-symlink', 'dest-dir', 'dest-special', 'prepare-failed', 'copy-failed',
                'replace-failed', 'verify-failed', 'cleanup-failed', 'budget-exceeded',
                'child-unreaped', 'interrupted')))
            if rc == 20:
                self.assertEqual(values[b'REASON'], b'interrupted')
        if values[b'TEMP'] == b'retained':
            expected_keys.append(b'TEMP_PATH')
            path = unquote_path(values[b'TEMP_PATH'])
            self.assertTrue(os.path.isdir(path), path)
            self.assertTrue(os.path.basename(path).startswith(b'.restore-taskmd-'), path)
        self.assertEqual(keys, expected_keys, output)
        return values

    def unchanged(self):
        self.assertEqual((self.destination.read_bytes(), stat.S_IMODE(self.destination.stat().st_mode)), self.before)

    def restored(self):
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), stat.S_IMODE(self.source.stat().st_mode))

    def test_normal_existing_and_absent_binary_empty_modes_and_names(self):
        for mode in (0o640, 0o444, 0o750):
            for content in (b'', b'plain\n', b'\x00\xff\x01body'):
                for exists in (False, True):
                    with self.subTest(mode=mode, content=content, exists=exists):
                        self.source.chmod(0o600); self.source.write_bytes(content); self.source.chmod(mode)
                        self.destination = self.root / '空白 日本語\nlast\xff'
                        if self.destination.exists(): self.destination.unlink()
                        if exists: self.destination.write_bytes(b'old')
                        result = self.cli()
                        fields = self.fields(result.returncode, result.stdout)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(fields[b'TOUCHED'], b'replaced' if exists else b'copied')
                        self.assertEqual(result.stderr, b'')
                        self.restored()
                        self.assertFalse(list(self.root.glob('.restore-taskmd-*')))

    def test_strict_arguments_reject_before_file_access(self):
        valid = self.argv()
        malformed = [[], ['restore'], valid + ['--extra', 'x'], valid + ['--'],
                     valid[:-1], valid[:1] + ['--source', 'x'] + valid[3:],
                     valid[:1] + ['--destination', str(self.source)] + valid[3:]]
        for key, value in (('source', '/'), ('source', '/tmp//file'), ('source', '/tmp/../file'),
                           ('source', '/tmp/./file'), ('destination', '/tmp/file/'),
                           ('expected_mode', '10000'), ('expected_mode', '888'),
                           ('expected_sha256', 'A' * 64), ('expected_sha256', '0' * 63), ('cp', 'cp')):
            malformed.append(self.argv(**{key: value}))
        for argv in malformed:
            with self.subTest(argv=argv):
                result = self.cli(argv) if argv else subprocess.run([sys.executable, str(HELPER)], capture_output=True)
                fields = self.fields(result.returncode, result.stdout)
                self.assertEqual(result.returncode, 33)
                self.assertEqual(fields[b'REASON'], b'internal-argument')
                self.assertEqual(fields[b'TEMP'], b'none')
                self.unchanged()

    def test_source_type_mode_and_hash_fail_closed(self):
        for variant in ('mode', 'hash', 'symlink', 'fifo', 'directory'):
            with self.subTest(variant=variant):
                op = self.operation()
                original = self.source.read_bytes()
                if variant == 'mode': self.source.chmod(0o600)
                elif variant == 'hash': self.source.write_bytes(b'changed')
                else:
                    self.source.unlink()
                    if variant == 'symlink': self.source.symlink_to(self.destination)
                    elif variant == 'fifo': os.mkfifo(self.source)
                    else: self.source.mkdir()
                started = time.monotonic(); rc, output = op.run()
                fields = self.fields(rc, output)
                self.assertEqual(rc, 33); self.assertEqual(fields[b'REASON'], b'taskmd-body')
                self.assertLess(time.monotonic() - started, 2)
                self.unchanged()
                if self.source.is_dir(): self.source.rmdir()
                else: self.source.unlink()
                self.source.write_bytes(original); self.source.chmod(0o640)

    def test_destination_types_and_parent_symlink(self):
        for variant, reason in (('symlink', b'dest-symlink'), ('directory', b'dest-dir'), ('fifo', b'dest-special')):
            with self.subTest(variant=variant):
                self.destination.unlink()
                if variant == 'symlink': self.destination.symlink_to(self.source)
                elif variant == 'directory': self.destination.mkdir()
                else: os.mkfifo(self.destination)
                before = self.source.read_bytes()
                rc, output = self.operation().run(); fields = self.fields(rc, output)
                self.assertEqual(rc, 33); self.assertEqual(fields[b'REASON'], reason)
                self.assertEqual(fields[b'TOUCHED'], b'none'); self.assertEqual(self.source.read_bytes(), before)
                if self.destination.is_dir(): self.destination.rmdir()
                else: self.destination.unlink()
                self.destination.write_bytes(self.before[0]); self.destination.chmod(self.before[1])
        link = self.root / 'alias'; link.symlink_to(self.root, target_is_directory=True)
        rc, output = self.operation(destination=str(link / 'current')).run()
        self.assertEqual(self.fields(rc, output)[b'REASON'], b'dest-symlink'); self.unchanged()

    def test_metadata_matches_gnu_archive_copy(self):
        ns = 1600000000123456789
        os.utime(self.source, ns=(ns, ns))
        xattr = False
        if hasattr(os, 'setxattr'):
            try: os.setxattr(self.source, b'user.restore-test', b'attribute'); xattr = True
            except OSError: pass
        control = self.root / 'gnu-control'
        subprocess.run([CP, '-a', '--', str(self.source), str(control)], check=True)
        rc, output = self.operation().run(); self.fields(rc, output); self.assertEqual(rc, 0)
        left, right = self.destination.stat(), control.stat()
        for key in ('st_mode', 'st_uid', 'st_gid', 'st_mtime_ns'):
            self.assertEqual(getattr(left, key), getattr(right, key), key)
        if xattr: self.assertEqual(os.getxattr(self.destination, b'user.restore-test'), os.getxattr(control, b'user.restore-test'))
        self.restored()

    def test_available_posix_acl_matches_gnu_archive_copy(self):
        setter, getter = shutil.which('setfacl'), shutil.which('getfacl')
        if not setter or not getter:
            self.skipTest('POSIX ACL の設定・読取りツールがないため未確認')
        configured = subprocess.run([setter, '-m', 'u:65534:r--', str(self.source)], capture_output=True)
        if configured.returncode:
            self.skipTest('scratch filesystem が POSIX ACL の設定を受け付けないため未確認')
        control = self.root / 'acl-control'
        subprocess.run([CP, '-a', '--', str(self.source), str(control)], check=True)
        rc, output = self.operation().run(); self.fields(rc, output); self.assertEqual(rc, 0)
        expected = subprocess.run([getter, '-c', '-p', '-n', str(control)], check=True, capture_output=True).stdout
        actual = subprocess.run([getter, '-c', '-p', '-n', str(self.destination)], check=True, capture_output=True).stdout
        self.assertIn(b'user:65534:r--', actual)
        self.assertEqual(actual, expected); self.restored()

    def test_prepare_failure_keeps_original(self):
        op = self.operation()
        with mock.patch.object(op, 'prepare_directory', side_effect=restore.Failure('prepare-failed')):
            rc, output = op.run()
        self.assertEqual(self.fields(rc, output)[b'REASON'], b'prepare-failed'); self.unchanged()

    def test_copy_failure_preserves_existing_and_absent(self):
        for exists in (True, False):
            with self.subTest(exists=exists):
                if not exists: self.destination.unlink()
                op = self.operation(); real = op.copy_file
                def copy(source, destination, size, root):
                    if source == op.source_fd: raise restore.Failure('copy-failed')
                    return real(source, destination, size, root)
                with mock.patch.object(op, 'copy_file', side_effect=copy): rc, output = op.run()
                fields = self.fields(rc, output)
                self.assertEqual(rc, 33); self.assertEqual(fields[b'TOUCHED'], b'none')
                self.assertEqual(fields[b'TEMP'], b'removed'); self.assertEqual(fields[b'REASON'], b'copy-failed')
                if exists: self.unchanged()
                else: self.assertFalse(self.destination.exists())

    def test_replace_failure_preserves_original(self):
        op = self.operation(); actual = os.replace
        def replace(source, destination, **kwargs):
            if source == 'prepared': raise PermissionError('fixture')
            return actual(source, destination, **kwargs)
        with mock.patch.object(restore.os, 'replace', side_effect=replace): rc, output = op.run()
        fields = self.fields(rc, output); self.assertEqual(fields[b'REASON'], b'replace-failed')
        self.assertEqual(fields[b'TOUCHED'], b'none'); self.unchanged()

    def test_post_publish_verification_failure_is_not_rolled_back(self):
        op = self.operation()
        with mock.patch.object(op, 'verify_published', side_effect=restore.Failure('verify-failed')):
            rc, output = op.run()
        fields = self.fields(rc, output); self.assertEqual(rc, 33)
        self.assertEqual(fields[b'TOUCHED'], b'replaced'); self.restored()

    def test_signals_at_every_boundary_report_actual_change_once(self):
        before = ('start', 'before-prepare', 'probe', 'before-copy', 'copy', 'before-publish', 'publish-ready', 'replace')
        after = ('after-publish', 'after-verify', 'before-cleanup', 'before-response')
        for stage in before + after:
            for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                with self.subTest(stage=stage, number=number):
                    self.destination.write_bytes(self.before[0]); self.destination.chmod(self.before[1])
                    op = self.operation(); actual = op.checkpoint; reached = []
                    def checkpoint(current):
                        if current == stage and not reached:
                            reached.append(current); op.on_signal(number, None)
                        actual(current)
                    with mock.patch.object(op, 'checkpoint', side_effect=checkpoint): rc, output = op.run()
                    self.assertTrue(reached); fields = self.fields(rc, output); self.assertEqual(rc, 20)
                    self.assertEqual(fields[b'TOUCHED'], b'replaced' if stage in after else b'none')
                    if stage in after: self.restored()
                    else: self.unchanged()

    def test_signal_during_real_replace_keeps_replaced_state(self):
        op = self.operation(); actual = os.replace
        def replace(source, destination, **kwargs):
            result = actual(source, destination, **kwargs)
            if source == 'prepared': os.kill(os.getpid(), signal.SIGTERM)
            return result
        with mock.patch.object(restore.os, 'replace', side_effect=replace): rc, output = op.run()
        fields = self.fields(rc, output); self.assertEqual(rc, 20)
        self.assertEqual(fields[b'TOUCHED'], b'replaced'); self.restored()

    def test_deadline_before_and_after_publication(self):
        for stage, touched in (('hash-before', b'none'), ('before-publish', b'none'), ('after-publish', b'replaced')):
            with self.subTest(stage=stage):
                self.destination.write_bytes(self.before[0]); self.destination.chmod(self.before[1])
                op = self.operation(); actual = op.checkpoint
                def checkpoint(current):
                    if current == stage: op.deadline = 0
                    actual(current)
                with mock.patch.object(op, 'checkpoint', side_effect=checkpoint): rc, output = op.run()
                fields = self.fields(rc, output); self.assertEqual(fields[b'REASON'], b'budget-exceeded')
                self.assertEqual(fields[b'TOUCHED'], touched)
                if touched == b'none': self.unchanged()
                else: self.restored()

    def test_source_short_read_growth_and_same_size_mutation(self):
        for variant in ('short', 'grow', 'same'):
            with self.subTest(variant=variant):
                original = b'A' * 131073
                self.source.write_bytes(original)
                op = self.operation(); actual = op.checkpoint; modified = []
                def checkpoint(stage):
                    if stage == 'hash-before' and not modified:
                        modified.append(True)
                        self.source.write_bytes(b'X' if variant == 'short' else original + b'Y' if variant == 'grow' else b'B' * len(original))
                    actual(stage)
                with mock.patch.object(op, 'checkpoint', side_effect=checkpoint): rc, output = op.run()
                self.assertEqual(self.fields(rc, output)[b'REASON'], b'taskmd-body'); self.unchanged()

    def test_hash_reads_are_chunked_and_three_passes_bounded(self):
        self.source.write_bytes(b'A' * 140000)
        op = self.operation(); original_read = os.read; reads = []
        def read(fd, size):
            if fd in (op.source_fd, op.prepared_fd): reads.append(size)
            return original_read(fd, size)
        with mock.patch.object(restore.os, 'read', side_effect=read): rc, output = op.run()
        self.fields(rc, output); self.assertEqual(rc, 0)
        self.assertTrue(reads); self.assertLessEqual(max(reads), 65536)
        self.assertEqual(sum(reads), 3 * (140000 + 1)); self.restored()

    def test_missing_static_api_is_no_temp_and_no_change(self):
        op = self.operation()
        with mock.patch.object(restore, 'resource', None): rc, output = op.run()
        fields = self.fields(rc, output); self.assertEqual(fields[b'REASON'], b'runtime-unavailable')
        self.assertEqual(fields[b'TEMP'], b'none'); self.unchanged()

    def test_missing_fd_aliases_fails_without_publication(self):
        with mock.patch.object(restore, 'FD_ROOTS', ('/missing-fd-alias',)):
            rc, output = self.operation().run()
        fields = self.fields(rc, output); self.assertEqual(fields[b'REASON'], b'runtime-unavailable')
        self.assertEqual(fields[b'TEMP'], b'removed'); self.unchanged()

    def test_second_fd_alias_candidate_can_succeed(self):
        with mock.patch.object(restore, 'FD_ROOTS', ('/missing-fd-alias', '/proc/self/fd')):
            rc, output = self.operation().run()
        self.fields(rc, output); self.assertEqual(rc, 0); self.restored()

    def test_cleanup_failure_reports_non_utf8_exact_retained_path(self):
        raw_parent = os.fsencode(self.root) + b'/nonutf-\xff\n"\\'
        os.mkdir(raw_parent)
        self.destination = Path(os.fsdecode(raw_parent + b'/file-\xfe'))
        self.destination.write_bytes(self.before[0])
        op = self.operation(); real = op.remove_entry
        def remove(name):
            if name == 'prepared': raise restore.Failure('cleanup-failed')
            return real(name)
        with mock.patch.object(op, 'publish', side_effect=restore.Failure('replace-failed')), mock.patch.object(op, 'remove_entry', side_effect=remove):
            rc, output = op.run()
        fields = self.fields(rc, output)
        self.assertEqual(fields[b'TEMP'], b'retained'); self.assertEqual(fields[b'TOUCHED'], b'none')
        self.assertEqual(unquote_path(fields[b'TEMP_PATH']), os.fsencode(op.temp_path))
        self.assertEqual(os.path.dirname(unquote_path(fields[b'TEMP_PATH'])), raw_parent)
        self.assertEqual(len(output.splitlines()), 5)
        self.assertEqual(self.destination.read_bytes(), self.before[0])
        self.assertEqual(stat.S_IMODE(os.stat(op.temp_path).st_mode), 0o700)

    def test_cleanup_after_publish_failure_is_not_success(self):
        op = self.operation()
        with mock.patch.object(op, 'cleanup', side_effect=restore.Failure('cleanup-failed')): rc, output = op.run()
        fields = self.fields(rc, output); self.assertEqual(rc, 33)
        self.assertEqual(fields[b'REASON'], b'cleanup-failed'); self.assertEqual(fields[b'TOUCHED'], b'replaced')
        self.assertEqual(fields[b'TEMP'], b'retained'); self.restored()

    def test_prepared_name_swap_is_detected_without_deleting_replacement(self):
        op = self.operation(); actual = op.checkpoint
        def checkpoint(stage):
            if stage == 'before-publish':
                prepared = Path(op.temp_path) / 'prepared'
                prepared.rename(prepared.with_name('saved'))
                prepared.write_bytes(b'foreign-sentinel')
            actual(stage)
        with mock.patch.object(op, 'checkpoint', side_effect=checkpoint): rc, output = op.run()
        fields = self.fields(rc, output); self.assertEqual(rc, 33)
        self.assertEqual(fields[b'TOUCHED'], b'none'); self.unchanged()
        self.assertEqual((Path(op.temp_path) / 'prepared').read_bytes(), b'foreign-sentinel')

    def test_target_created_after_absence_check_is_not_overwritten(self):
        self.destination.unlink(); op = self.operation(); actual = op.checkpoint
        def checkpoint(stage):
            if stage == 'before-publish': self.destination.write_bytes(b'new-sentinel')
            actual(stage)
        with mock.patch.object(op, 'checkpoint', side_effect=checkpoint): rc, output = op.run()
        self.assertEqual(self.fields(rc, output)[b'TOUCHED'], b'none')
        self.assertEqual(self.destination.read_bytes(), b'new-sentinel')

    def test_parent_swap_does_not_write_to_symlink_target(self):
        parent = self.root / 'parent'; parent.mkdir()
        self.destination = parent / 'file'; self.destination.write_bytes(b'old')
        other = self.root / 'other'; other.mkdir(); (other / 'file').write_bytes(b'sentinel')
        op = self.operation(); actual = op.checkpoint
        def checkpoint(stage):
            if stage == 'before-publish':
                parent.rename(self.root / 'held-parent'); parent.symlink_to(other, target_is_directory=True)
            actual(stage)
        with mock.patch.object(op, 'checkpoint', side_effect=checkpoint): rc, output = op.run()
        fields = self.fields(rc, output); self.assertEqual(rc, 33)
        self.assertEqual(fields[b'TOUCHED'], b'none')
        self.assertEqual((other / 'file').read_bytes(), b'sentinel')
        self.assertEqual((self.root / 'held-parent/file').read_bytes(), b'old')

    def test_larger_than_200_mib_is_allowed(self):
        size = 200 * 1024 * 1024 + 65537
        with self.source.open('wb') as stream:
            stream.truncate(size)
            stream.seek(size - 1); stream.write(b'X')
        started = time.monotonic()
        op = self.operation(); rc, output = op.run()
        self.fields(rc, output); self.assertEqual(rc, 0)
        self.assertEqual(self.destination.stat().st_size, size)
        self.assertEqual(digest(self.destination), digest(self.source))
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o640)
        print('restore >200MiB: bytes=%d seconds=%.3f' % (size, time.monotonic() - started))

    def cp_stub(self, body):
        stub = self.root / 'cp-stub'
        stub.write_text('#!' + sys.executable + '\nimport os,sys,time,signal,resource\n' + body)
        stub.chmod(0o755)
        return str(stub)

    def test_cp_version_output_limit_and_invalid_banner(self):
        for body in ("print('x' * 8193)\n", "print('cp (different-tool) 1')\n"):
            with self.subTest(body=body):
                cp = self.cp_stub(body)
                rc, output = self.operation(cp=cp).run()
                fields = self.fields(rc, output); self.assertEqual(rc, 33)
                self.assertEqual(fields[b'REASON'], b'runtime-unavailable')
                self.assertEqual(fields[b'TEMP'], b'none'); self.unchanged()

    def test_cp_version_timeout_stops_and_reaps_direct_child(self):
        cp = self.cp_stub("print('cp (GNU coreutils) fixture', flush=True)\n"
                          "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)\n")
        op = self.operation(cp=cp); started = time.monotonic()
        with mock.patch.object(restore, 'STOP_SECONDS', .05): rc, output = op.run()
        fields = self.fields(rc, output)
        self.assertEqual(fields[b'REASON'], b'runtime-unavailable')
        self.assertLess(time.monotonic() - started, 6)
        self.assertIsNone(op.child); self.unchanged()

    def test_copy_child_limit_and_only_two_fds(self):
        cp = self.cp_stub("if sys.argv[1:] == ['--version']:\n print('cp (GNU coreutils) fixture');sys.exit(0)\n"
                          "source,destination=map(lambda x:int(x.rsplit('/',1)[1]),sys.argv[-2:])\n"
                          "held=set()\nfor name in os.listdir('/proc/self/fd'):\n"
                          " try:\n  fd=int(name);os.fstat(fd)\n  if fd>2: held.add(fd)\n"
                          " except OSError: pass\n"
                          "if held != {source,destination}: sys.exit(61)\n"
                          "if resource.getrlimit(resource.RLIMIT_FSIZE) != (os.fstat(source).st_size,)*2: sys.exit(62)\n"
                          "os.execv(" + repr(CP) + ", [" + repr(CP) + "]+sys.argv[1:])\n")
        rc, output = self.operation(cp=cp).run()
        self.fields(rc, output); self.assertEqual(rc, 0); self.restored()

    def test_copy_over_initial_size_is_stopped_and_never_published(self):
        cp = self.cp_stub("if sys.argv[1:] == ['--version']:\n print('cp (GNU coreutils) fixture');sys.exit(0)\n"
                          "source,destination=sys.argv[-2:]\n"
                          "if os.readlink(source).endswith('/taskmd-body'):\n"
                          " os.write(int(destination.rsplit('/',1)[1]),b'X'*os.stat(source).st_size);os.write(int(destination.rsplit('/',1)[1]),b'Y');sys.exit(0)\n"
                          "os.execv(" + repr(CP) + ", [" + repr(CP) + "]+sys.argv[1:])\n")
        rc, output = self.operation(cp=cp).run()
        fields = self.fields(rc, output); self.assertEqual(rc, 33)
        self.assertEqual(fields[b'REASON'], b'copy-failed'); self.assertEqual(fields[b'TEMP'], b'removed')
        self.unchanged()

    def test_copy_deadline_term_ignore_kill_and_reap(self):
        marker = self.root / 'child-pid'
        cp = self.cp_stub("if sys.argv[1:] == ['--version']:\n print('cp (GNU coreutils) fixture');sys.exit(0)\n"
                          "if os.readlink(sys.argv[-2]).endswith('/taskmd-body'):\n"
                          " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                          " with open(" + repr(str(marker)) + ",'w') as f:f.write(str(os.getpid()))\n"
                          " time.sleep(30);sys.exit(0)\n"
                          "os.execv(" + repr(CP) + ", [" + repr(CP) + "]+sys.argv[1:])\n")
        op = self.operation(cp=cp); actual = op.copy_file
        def copy(source, destination, size, root):
            if source == op.source_fd: op.deadline = time.monotonic() + .25
            return actual(source, destination, size, root)
        started = time.monotonic()
        with mock.patch.object(op, 'copy_file', side_effect=copy), mock.patch.object(restore, 'STOP_SECONDS', .05):
            rc, output = op.run()
        fields = self.fields(rc, output)
        self.assertEqual(fields[b'REASON'], b'budget-exceeded'); self.assertEqual(fields[b'TEMP'], b'removed')
        self.assertLess(time.monotonic() - started, 2)
        pid = int(marker.read_text())
        with self.assertRaises(ChildProcessError): os.waitpid(pid, os.WNOHANG)
        self.unchanged()

    def test_unreaped_child_uses_single_stop_budget_and_retains_temp(self):
        class NeverExited:
            def __init__(self): self.calls = []
            def poll(self): return None
            def wait(self): raise AssertionError('unreaped child must not be treated as exited')
            def terminate(self): self.calls.append('TERM')
            def kill(self): self.calls.append('KILL')
        child = NeverExited(); op = self.operation(); actual = op.copy_file
        def copy(source, destination, size, root):
            if source != op.source_fd: return actual(source, destination, size, root)
            op.child = child
            op.stop_child()
            raise restore.Failure('child-unreaped')
        with mock.patch.object(op, 'copy_file', side_effect=copy), mock.patch.object(restore, 'STOP_SECONDS', .02):
            rc, output = op.run()
        fields = self.fields(rc, output)
        self.assertEqual(child.calls, ['TERM', 'KILL'])
        self.assertEqual(fields[b'REASON'], b'child-unreaped')
        self.assertEqual(fields[b'TEMP'], b'retained'); self.assertEqual(fields[b'TOUCHED'], b'none')
        self.assertTrue((Path(op.temp_path) / 'prepared').exists()); self.unchanged()

    def test_parent_swap_forces_unknown_temp_without_stale_path(self):
        parent = self.root / 'parent'; parent.mkdir()
        self.destination = parent / 'file'; self.destination.write_bytes(b'old')
        op = self.operation(); actual = op.checkpoint
        def checkpoint(stage):
            if stage == 'before-cleanup':
                parent.rename(self.root / 'moved'); parent.mkdir()
            actual(stage)
        with mock.patch.object(op, 'checkpoint', side_effect=checkpoint): rc, output = op.run()
        fields = self.fields(rc, output); self.assertEqual(rc, 33)
        self.assertEqual(fields[b'TEMP'], b'unknown'); self.assertNotIn(b'TEMP_PATH', fields)
        self.assertFalse((parent / 'file').exists())
        self.assertEqual((self.root / 'moved/file').read_bytes(), self.source.read_bytes())

    def test_cleanup_deadline_retains_temp(self):
        op = self.operation()
        with mock.patch.object(restore, 'CLEANUP_SECONDS', 0): rc, output = op.run()
        fields = self.fields(rc, output)
        self.assertEqual(rc, 33); self.assertEqual(fields[b'REASON'], b'cleanup-failed')
        self.assertEqual(fields[b'TEMP'], b'retained'); self.restored()

    def test_response_size_checked_before_temp_creation(self):
        op = self.operation()
        with mock.patch.object(restore, 'MAX_RESPONSE', 32): rc, output = op.run()
        fields = self.fields(rc, output)
        self.assertEqual(fields[b'REASON'], b'internal-argument')
        self.assertEqual(fields[b'TEMP'], b'none'); self.unchanged()

    def test_corrupt_prepared_bytes_are_never_published(self):
        op = self.operation(); actual = op.copy_file
        def copy(source, destination, size, root):
            result = actual(source, destination, size, root)
            if source == op.source_fd:
                os.lseek(destination, 0, os.SEEK_SET); os.write(destination, b'X')
            return result
        with mock.patch.object(op, 'copy_file', side_effect=copy): rc, output = op.run()
        fields = self.fields(rc, output)
        self.assertEqual(fields[b'REASON'], b'verify-failed'); self.assertEqual(fields[b'TOUCHED'], b'none')
        self.unchanged()

    def test_published_name_swap_reports_changed_and_preserves_foreign_inode(self):
        op = self.operation(); actual = op.checkpoint
        def checkpoint(stage):
            if stage == 'after-publish':
                self.destination.rename(self.root / 'published')
                self.destination.write_bytes(b'foreign')
            actual(stage)
        with mock.patch.object(op, 'checkpoint', side_effect=checkpoint): rc, output = op.run()
        fields = self.fields(rc, output)
        self.assertEqual(fields[b'REASON'], b'verify-failed'); self.assertEqual(fields[b'TOUCHED'], b'replaced')
        self.assertEqual(self.destination.read_bytes(), b'foreign')
        self.assertEqual((self.root / 'published').read_bytes(), self.source.read_bytes())

    def test_unexpected_exception_emits_no_traceback_or_content(self):
        op = self.operation()
        with mock.patch.object(op, 'read_source', side_effect=RuntimeError('PRIVATE-CONTENT')): rc, output = op.run()
        self.fields(rc, output); self.assertEqual(rc, 33)
        self.assertNotIn(b'PRIVATE-CONTENT', output); self.assertNotIn(b'Traceback', output)


    def test_final_response_captured_signal_before_output_is_failure(self):
        import io
        op = self.operation(); rc, result = op.run()
        op.on_signal(signal.SIGTERM, None)
        output = io.BytesIO()
        with mock.patch.object(restore.sys, 'stdout', mock.Mock(buffer=output)):
            final_rc = restore.emit_result(op, rc, result)
        fields = self.fields(final_rc, output.getvalue())
        self.assertEqual(final_rc, 20); self.assertEqual(fields[b'TOUCHED'], b'replaced')
        self.restored()

    def test_final_response_commit_masks_signal_until_single_result_written(self):
        import io
        class SignalDuringWrite(io.BytesIO):
            def __init__(self): super().__init__(); self.writes = 0
            def write(stream, payload):
                stream.writes += 1
                os.kill(os.getpid(), signal.SIGTERM)
                return super(SignalDuringWrite, stream).write(payload)
        op = self.operation(); rc, result = op.run(); output = SignalDuringWrite()
        with mock.patch.object(restore.sys, 'stdout', mock.Mock(buffer=output)):
            final_rc = restore.emit_result(op, rc, result)
        self.assertEqual(final_rc, 0); self.fields(final_rc, output.getvalue())
        self.assertEqual(output.writes, 1); self.assertEqual(op.stopped, signal.SIGTERM)
        self.restored()

    def test_final_output_failure_never_returns_success(self):
        op = self.operation(); rc, result = op.run()
        output = mock.Mock(); output.write.side_effect = BrokenPipeError()
        with mock.patch.object(restore.sys, 'stdout', mock.Mock(buffer=output)):
            self.assertEqual(restore.emit_result(op, rc, result), 33)
        self.assertEqual(output.write.call_count, 1)
        self.restored()

    def test_final_mask_setup_failure_has_no_valid_response(self):
        import io
        for error in (OSError(), ValueError(), NotImplementedError()):
            with self.subTest(error=type(error).__name__):
                op = self.operation(); rc, result = op.run(); output = io.BytesIO()
                with mock.patch.object(restore.signal, 'pthread_sigmask', side_effect=error), \
                     mock.patch.object(restore.sys, 'stdout', mock.Mock(buffer=output)):
                    self.assertNotEqual(restore.emit_result(op, rc, result), 0)
                self.assertEqual(output.getvalue(), b''); self.restored()

    def test_final_mask_restoration_failure_preserves_one_final_result(self):
        import io
        actual = signal.pthread_sigmask
        for error in (OSError(), ValueError(), NotImplementedError()):
            op = self.operation(); rc, result = op.run(); output = io.BytesIO()
            original = actual(signal.SIG_BLOCK, [])
            def mask(how, values):
                if how == signal.SIG_SETMASK: raise error
                return actual(how, values)
            try:
                with mock.patch.object(restore.signal, 'pthread_sigmask', side_effect=mask), \
                     mock.patch.object(restore.sys, 'stdout', mock.Mock(buffer=output)):
                    final_rc = restore.emit_result(op, rc, result)
                self.assertEqual(final_rc, 0); self.fields(final_rc, output.getvalue())
                self.restored()
            finally:
                actual(signal.SIG_SETMASK, original)

    def test_final_signal_mask_boundaries_generate_write_flush_and_unmask(self):
        import io
        for boundary in ('before-mask', 'after-mask', 'response', 'write', 'flush', 'unmask'):
            for number in restore.SIGNALS:
                with self.subTest(boundary=boundary, number=number):
                    op = self.operation(); rc, result = op.run()
                    actual_mask = signal.pthread_sigmask; actual_response = op.response
                    class Output(io.BytesIO):
                        writes = 0
                        def write(stream, data):
                            stream.writes += 1
                            if boundary == 'write': op.on_signal(number, None)
                            return super().write(data)
                        def flush(stream):
                            if boundary == 'flush': op.on_signal(number, None)
                            return super().flush()
                    output = Output()
                    def mask(how, values):
                        if how == signal.SIG_BLOCK and boundary == 'before-mask':
                            op.on_signal(number, None)
                        old = actual_mask(how, values)
                        if how == signal.SIG_BLOCK and boundary == 'after-mask':
                            os.kill(os.getpid(), number)
                        if how == signal.SIG_SETMASK and boundary == 'unmask':
                            op.on_signal(number, None)
                        return old
                    def response():
                        if boundary == 'response': op.on_signal(number, None)
                        return actual_response()
                    with mock.patch.object(restore.signal, 'pthread_sigmask', side_effect=mask), \
                         mock.patch.object(op, 'response', side_effect=response), \
                         mock.patch.object(restore.sys, 'stdout', mock.Mock(buffer=output)):
                        final_rc = restore.emit_result(op, rc, result)
                    self.assertEqual(final_rc, 20 if boundary == 'before-mask' else 0)
                    self.fields(final_rc, output.getvalue())
                    self.assertEqual(output.writes, 1); self.restored()

    def supervision_code(self):
        text = (ROOT / 'plugins/dev-workflow/skills/do-task/references/runtime-requirements.md').read_text()
        section = text.split('<!-- restore-taskmd-supervision:start -->', 1)[1]
        section = section.split('<!-- restore-taskmd-supervision:end -->', 1)[0]
        return section.split('```bash\n', 1)[1].split('```', 1)[0]

    def test_caller_requires_gnu_timeout_and_fixed_argv(self):
        import json
        code = self.supervision_code()
        self.assertIn('--signal=TERM --kill-after=5s 330s bash', code)
        self.assertNotIn('--foreground', code)
        bindir = self.root / 'bin'; bindir.mkdir()
        marker = self.root / 'called'; guard = self.root / 'guard'
        guard.write_text('printf touched >"$1"\n')
        runner = self.root / 'invoke'
        runner.write_text(code + '\nrestore_taskmd_supervised "$@"\n')
        env = dict(os.environ, PATH=str(bindir))
        result = subprocess.run(['/bin/bash', str(runner), str(guard), str(marker)],
                                env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 125); self.assertFalse(marker.exists())
        self.assertEqual(result.stdout, b''); self.unchanged()
        # GNU表記でない実行物も、復元を起動する前に止める。
        timeout = bindir / 'timeout'
        timeout.write_text('#!/bin/bash\nprintf "unrelated version\\n"\n'); timeout.chmod(0o700)
        result = subprocess.run(['/bin/bash', str(runner), str(guard), str(marker)],
                                env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 125); self.assertFalse(marker.exists())
        timeout.unlink()
        # gtimeoutだけの環境で、shell再解釈なしのargvを確認する。
        log = self.root / 'argv.json'; timeout = bindir / 'gtimeout'
        timeout.write_text('#!' + sys.executable + '\nimport sys,json\n'
            "if sys.argv[1:] == ['--version']: print('timeout (GNU coreutils) fixture')\n"
            "elif sys.argv[3] == '1s': sys.exit(0)\n"
            "else: open(" + repr(str(log)) + ",'w').write(json.dumps(sys.argv[1:]))\n")
        timeout.chmod(0o700); (bindir / 'true').symlink_to('/bin/true')
        unusual = '空白\n$(must-not-run)'
        result = subprocess.run(['/bin/bash', str(runner), str(guard), '--cwd', unusual],
                                env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(log.read_text()),
                         ['--signal=TERM', '--kill-after=5s', '330s', 'bash', str(guard),
                          'restore-taskmd', '--cwd', unusual])
        self.assertFalse(marker.exists()); self.unchanged()
        # GNU表記でも必要オプションを処理できなければ、起動しない。
        log.unlink(); text = timeout.read_text().replace("elif sys.argv[3] == '1s': sys.exit(0)",
                                                      "elif sys.argv[3] == '1s': sys.exit(2)")
        timeout.write_text(text); timeout.chmod(0o700)
        result = subprocess.run(['/bin/bash', str(runner), str(guard)],
                                env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 125); self.assertFalse(log.exists())

    def test_caller_terminates_real_full_pipe_and_reaps_fixture(self):
        # timeoutが送出中のmask区間をKILLできることを、実際に満杯のpipeで検査。
        # 本試験だけ短縮し、製品の330秒/追加5秒は上の固定argv試験で確認する。
        import ctypes
        import fcntl
        import shlex
        libc = ctypes.CDLL(None, use_errno=True); previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        guard = self.root / 'guard'; pidfile = self.root / 'helper-pid'
        guard.write_text('printf "%s" "$$" >' + shlex.quote(str(pidfile)) + '\n'
                         'shift\nexec ' + shlex.quote(sys.executable) + ' -B ' +
                         shlex.quote(str(HELPER)) + ' "$@"\n')
        runner = self.root / 'supervised'
        code = self.supervision_code().replace('--kill-after=5s 330s', '--kill-after=0.2s 1s')
        runner.write_text(code + '\nrestore_taskmd_supervised "$@"\n')
        read_fd, write_fd = os.pipe(); process = None; pid = None; reaped = False
        try:
            capacity = fcntl.fcntl(write_fd, fcntl.F_GETPIPE_SZ)
            os.write(write_fd, b'x' * capacity)
            started = time.monotonic()
            process = subprocess.Popen(['/bin/bash', str(runner), str(guard), *self.argv()],
                                       stdout=write_fd, stderr=subprocess.PIPE)
            process.wait(timeout=5)
            self.assertIn(process.returncode, (124, 137))
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(pidfile.exists()); pid = int(pidfile.read_text())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                got, status = os.waitpid(pid, os.WNOHANG)
                if got == pid:
                    reaped = True; self.assertTrue(os.WIFSIGNALED(status))
                    self.assertEqual(os.WTERMSIG(status), signal.SIGKILL); break
                time.sleep(.01)
            self.assertTrue(reaped)
            os.close(write_fd); write_fd = None
            output = bytearray()
            for block in iter(lambda: os.read(read_fd, 65536), b''): output.extend(block)
            self.assertEqual(output, b'x' * capacity)  # 有効な応答なし→callerはunknownで停止
            self.restored(); self.assertFalse(list(self.root.glob('.restore-taskmd-*')))
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
        finally:
            if process is not None:
                if process.poll() is None: process.kill()
                process.communicate(timeout=3)
            if pid is not None and not reaped:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
                try: os.waitpid(pid, 0)
                except ChildProcessError: pass
            if write_fd is not None: os.close(write_fd)
            os.close(read_fd)
            self.assertEqual(libc.prctl(36, previous.value, 0, 0, 0), 0)

    def test_public_helper_term_int_hup_stop_copy_and_reap(self):
        marker = self.root / 'pid'
        cp = self.cp_stub("if sys.argv[1:] == ['--version']:\n print('cp (GNU coreutils) fixture');sys.exit(0)\n"
                          "if os.readlink(sys.argv[-2]).endswith('/taskmd-body'):\n"
                          " with open(" + repr(str(marker)) + ",'w') as f:f.write(str(os.getpid()))\n"
                          " time.sleep(15);sys.exit(0)\n"
                          "os.execv(" + repr(CP) + ", [" + repr(CP) + "]+sys.argv[1:])\n")
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number):
                if marker.exists(): marker.unlink()
                process = subprocess.Popen([sys.executable, '-B', str(HELPER), *self.argv(cp=cp)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                pid = None
                try:
                    deadline = time.monotonic() + 3
                    while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists(), process.poll())
                    pid = int(marker.read_text())
                    process.send_signal(number)
                    stdout, stderr = process.communicate(timeout=3)
                    fields = self.fields(process.returncode, stdout)
                    self.assertEqual(process.returncode, 20); self.assertEqual(fields[b'TOUCHED'], b'none')
                    self.assertEqual(fields[b'TEMP'], b'removed'); self.assertEqual(stderr, b'')
                    with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                    self.unchanged()
                finally:
                    if process.poll() is None: process.kill()
                    process.communicate(timeout=3)
                    if pid is not None:
                        try: os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError: pass

    def test_public_helper_kill_has_no_claim_of_unchanged_or_success(self):
        # この試験プロセスだけを orphan の回収担当にし、fixture 子を必ず wait する。
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        original = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(original), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        marker = self.root / 'pid'
        cp = self.cp_stub("if sys.argv[1:] == ['--version']:\n print('cp (GNU coreutils) fixture');sys.exit(0)\n"
                          "if os.readlink(sys.argv[-2]).endswith('/taskmd-body'):\n"
                          " with open(" + repr(str(marker)) + ",'w') as f:f.write(str(os.getpid()))\n"
                          " time.sleep(.5);sys.exit(0)\n"
                          "os.execv(" + repr(CP) + ", [" + repr(CP) + "]+sys.argv[1:])\n")
        process = None; pid = None; reaped = False
        try:
            process = subprocess.Popen([sys.executable, '-B', str(HELPER), *self.argv(cp=cp)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(marker.exists()); pid = int(marker.read_text()); process.kill()
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertEqual(stdout, b''); self.assertEqual(stderr, b'')
            # caller 規則では無応答は unknown。短命な試験子を外側から回収する。
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                got, status = os.waitpid(pid, os.WNOHANG)
                if got == pid:
                    reaped = True; self.assertEqual(status, 0); break
                time.sleep(.01)
            self.assertTrue(reaped)
            self.unchanged(); self.assertTrue(list(self.root.glob('.restore-taskmd-*')))
        finally:
            if process is not None:
                if process.poll() is None: process.kill()
                process.communicate(timeout=3)
            if pid is not None and not reaped:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
                try: os.waitpid(pid, 0)
                except ChildProcessError: pass
            self.assertEqual(libc.prctl(36, original.value, 0, 0, 0), 0)


if __name__ == '__main__':
    unittest.main()
