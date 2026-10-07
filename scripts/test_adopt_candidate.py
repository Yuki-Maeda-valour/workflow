"""候補採用の正式入口の回帰。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True

SCRIPT = Path(__file__).resolve().parents[1] / 'plugins/dev-workflow/skills/create-task/scripts/adopt-candidate.py'
CANDIDATE_KEYS = SCRIPT.with_name('candidate-keys.py')


class AdoptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / '候補_例.md'
        self.source.write_text('> **発見元**: refactor\n> **指摘キー**: H41\n\n## 指摘\n内容\n')
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.dest = self.root / '進行中_例.md'

    def tearDown(self):
        self.tmp.cleanup()

    def run_adopt(self, marker='', *extra):
        env = dict(os.environ, DEV_WORKFLOW_LOOP_ITER=marker)
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root),
                               '--expect-sha256', self.digest, *extra, str(self.source)],
                              env=env, text=True, capture_output=True)

    def test_human_untracked_normal(self):
        result = self.run_adopt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_file())

    def test_loop_marker_without_unattended_stops_unchanged(self):
        result = self.run_adopt('run-1')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('DEV_WORKFLOW_LOOP_ITER', result.stderr)
        self.assertTrue(self.source.is_file())
        self.assertFalse(self.dest.exists())

    def test_unattended_stops(self):
        self.assertEqual(self.run_adopt('', '--unattended').returncode, 2)
        self.assertTrue(self.source.exists())

    def test_digest_change_stops(self):
        self.source.write_text('changed')
        self.assertEqual(self.run_adopt().returncode, 2)
        self.assertFalse(self.dest.exists())

    def test_candidate_keys_normal_and_skip_attack(self):
        normal = subprocess.run([sys.executable, str(CANDIDATE_KEYS), '--check', str(self.source)],
                                text=True, capture_output=True)
        self.assertEqual(normal.returncode, 0, normal.stderr)
        self.source.write_text(
            '> **発見元**: refactor\n> **指摘キー**: H41\n'
            '> **見送り**: 2026-10-01 — 人の判断\n\n## 指摘\n')
        rejected = subprocess.run([sys.executable, str(CANDIDATE_KEYS), '--check', str(self.source)],
                                  text=True, capture_output=True)
        self.assertEqual(rejected.returncode, 1, rejected.stderr)
        self.assertIn('skipped', rejected.stdout)

    def test_adoption_reuses_held_candidate_check_as_warnings(self):
        for body, code in (
            ('## 指摘\n本文\n', 'source-count'),
            ('> **発見元**: refactor\n\n## 指摘\n本文\n', 'key-count'),
        ):
            with self.subTest(code=code):
                self.source.write_text(body)
                self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
                result = self.run_adopt()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(code, {item['code'] for item in json.loads(result.stdout)['warnings']})
                self.assertFalse(self.source.exists())
                self.assertTrue(self.dest.is_file())
                self.dest.unlink()

    def test_adoption_git_caller_uses_common_safe_prefix(self):
        module = self.module('adoption_safe_prefix')
        for item in ('--no-pager', '--no-replace-objects', 'core.hooksPath=/dev/null',
                     'core.fsmonitor=', 'commit.gpgSign=false', 'push.gpgSign=false',
                     'filter.lfs.clean=', 'filter.lfs.process='):
            self.assertIn(item, module.SAFE_GIT_PREFIX)

    def test_collision_all_states(self):
        for state in ('完了', '保留', '中断', '進行中'):
            with self.subTest(state=state):
                collision = self.root / f'{state}_例.md'
                collision.write_text('untouched')
                self.assertEqual(self.run_adopt().returncode, 2)
                self.assertEqual(collision.read_text(), 'untouched')
                collision.unlink()

    def test_renamed_candidate_checks_candidate_state_collision(self):
        collision = self.root / '候補_別名.md'
        collision.write_text('third party')
        result = self.run_adopt('', '--name', '別名')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(self.source.exists())
        self.assertEqual(collision.read_text(), 'third party')

    def test_skip_and_malformed_skip(self):
        for line in ('> **見送り**: 2026-10-01 — 人の判断', '> **見送り**: broken'):
            self.source.write_text('# 例\n' + line + '\n## 指摘\n')
            self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
            self.assertEqual(self.run_adopt().returncode, 2)
            self.assertTrue(self.source.exists())

    def test_marker_appearing_before_rename_stops(self):
        spec = importlib.util.spec_from_file_location('adoption_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_validate = module.validate_candidate
        def changed(*args, **kwargs):
            value = real_validate(*args, **kwargs)
            os.environ['DEV_WORKFLOW_LOOP_ITER'] = 'late-marker'
            return value
        with mock.patch.dict(os.environ, {'DEV_WORKFLOW_LOOP_ITER': ''}), mock.patch.object(module, 'validate_candidate', changed):
            with self.assertRaisesRegex(ValueError, 'DEV_WORKFLOW_LOOP_ITER'):
                module.adopt(self.root, self.source, None, self.digest)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.dest.exists())

    def test_tracked_normal_stages_rename(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True).stdout
        git('init', '-q')
        git('add', '--', self.source.name)
        git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        result = self.run_adopt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.dest.name.encode(), git('ls-files', '-z'))
        self.assertNotIn(self.source.name.encode(), git('ls-files', '-z'))

    def test_tracked_unstaged_candidate_edit_keeps_held_index_blob(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        git('init', '-q')
        git('add', '--', self.source.name)
        git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        indexed = self.source.read_bytes()
        self.source.write_bytes(indexed + '未 stage の追記\n'.encode())
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.run_adopt()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(indexed + '未 stage の追記\n'.encode(), self.dest.read_bytes())
        self.assertEqual(indexed, git('show', ':' + self.dest.name).stdout)

    def test_tracked_partially_staged_candidate_edit_keeps_held_index_blob(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        git('init', '-q')
        git('add', '--', self.source.name)
        git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        indexed = self.source.read_bytes() + 'stage した追記\n'.encode()
        self.source.write_bytes(indexed)
        git('add', '--', self.source.name)
        current = indexed + '未 stage の追記\n'.encode()
        self.source.write_bytes(current)
        self.digest = hashlib.sha256(current).hexdigest()
        result = self.run_adopt()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(current, self.dest.read_bytes())
        self.assertEqual(indexed, git('show', ':' + self.dest.name).stdout)

    def test_tracked_replacement_before_git_mv_stops_with_staged_residue(self):
        """git mv が読む直前の差替えを成功や自動 rollback に読み替えない。"""
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        git('init', '-q')
        git('add', '--', self.source.name)
        git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        held = self.source.read_bytes()
        module = self.module('adoption_tracked_late_replacement')
        real_root_run = module.root_run
        replaced = False

        def replace_before_mv(root_fd, argv, env, timeout):
            nonlocal replaced
            if not replaced and 'mv' in argv:
                replaced = True
                attacker = self.root / 'attacker-replacement'
                attacker.write_text('third party replacement')
                attacker.replace(self.source)
            return real_root_run(root_fd, argv, env, timeout)

        with mock.patch.object(module, 'root_run', side_effect=replace_before_mv):
            with self.assertRaisesRegex(ValueError, 'index の残存状態を人が確認する'):
                module.adopt(self.root, self.source, None, self.digest)
        self.assertTrue(replaced)
        self.assertFalse(self.source.exists())
        self.assertEqual('third party replacement', self.dest.read_text())
        staged = git('diff', '--cached', '--name-status', '-z').stdout
        self.assertIn(b'R100\0', staged)
        self.assertIn(os.fsencode(self.dest.name), staged)
        self.assertEqual(held, git('show', ':' + self.dest.name).stdout)

    def test_tracked_index_replacement_after_git_mv_stops_with_staged_residue(self):
        """作業ツリーを戻しても、第三者が差し替えた stage を許可しない。"""
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        git('init', '-q')
        git('add', '--', self.source.name)
        git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        held = self.source.read_bytes()
        module = self.module('adoption_tracked_index_replacement')
        real_root_run = module.root_run
        replaced = False

        def replace_index_after_mv(root_fd, argv, env, timeout):
            nonlocal replaced
            result = real_root_run(root_fd, argv, env, timeout)
            if not replaced and 'mv' in argv and result.returncode == 0:
                replaced = True
                self.dest.write_text('third party staged replacement')
                git('add', '--', self.dest.name)
                self.dest.write_bytes(held)
            return result

        with mock.patch.object(module, 'root_run', side_effect=replace_index_after_mv):
            with self.assertRaisesRegex(ValueError, 'index stage が改名前の保持値と違う'):
                module.adopt(self.root, self.source, None, self.digest)
        self.assertTrue(replaced)
        self.assertFalse(self.source.exists())
        self.assertEqual(held, self.dest.read_bytes())
        self.assertEqual(b'third party staged replacement', git('show', ':' + self.dest.name).stdout)

    def test_precheck_tamper_stops_tracked_rename(self):
        """採用 helper は candidate-keys 後にも snapshot の拒否を通す。"""
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        git('init', '-q')
        git('add', '--', self.source.name)
        git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        # diff-snapshot の --precheck は任意 filter を改竄候補として exit 22 にする。
        git('config', '--local', 'filter.untrusted.clean', 'cat')
        result = self.run_adopt()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('事前検査', result.stderr)
        self.assertTrue(self.source.is_file())
        self.assertFalse(self.dest.exists())
        self.assertIn(self.source.name.encode(), git('ls-files', '-z').stdout)

    def test_symlink_fifo_parent_refused(self):
        self.source.unlink()
        self.source.symlink_to(self.root / 'outside')
        self.assertEqual(self.run_adopt().returncode, 2)
        self.source.unlink()
        os.mkfifo(self.source)
        self.assertEqual(self.run_adopt().returncode, 2)

    def test_change_while_reading_stops_before_rename(self):
        spec = importlib.util.spec_from_file_location('adoption_read_race', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_read = module.os.read
        changed = False

        def mutate_after_read(fd, amount):
            nonlocal changed
            chunk = real_read(fd, amount)
            if chunk and not changed:
                changed = True
                self.source.write_text('changed while reading')
            return chunk

        with mock.patch.object(module.os, 'read', mutate_after_read):
            with self.assertRaisesRegex(ValueError, '読取り中に変わった'):
                module.adopt(self.root, self.source, None, self.digest)
        self.assertTrue(self.source.is_file())
        self.assertFalse(self.dest.exists())

    def module(self, name='adoption_portable'):
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_forced_posix_fallback_normal(self):
        module = self.module()
        with mock.patch.object(module, 'native_rename_noreplace', return_value=False):
            result = module.adopt(self.root, self.source, None, self.digest)
        self.assertEqual(result['path'], str(self.dest))
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_file())

    def test_posix_fallback_collision_keeps_source(self):
        module = self.module()
        self.dest.write_text('third party')
        with mock.patch.object(module, 'native_rename_noreplace', return_value=False):
            with self.assertRaisesRegex(ValueError, '同じ名の状態ファイル'):
                module.adopt(self.root, self.source, None, self.digest)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.dest.read_text(), 'third party')

    def test_posix_fallback_does_not_remove_destination_replaced_after_link(self):
        module = self.module()
        real_link = module.os.link
        def replace_after_link(*args, **kwargs):
            real_link(*args, **kwargs)
            self.dest.unlink()
            self.dest.write_text('third party')
        with mock.patch.object(module, 'native_rename_noreplace', return_value=False), \
             mock.patch.object(module.os, 'link', side_effect=replace_after_link):
            with self.assertRaisesRegex(ValueError, '自分が作った通常ファイルと確認できない'):
                module.adopt(self.root, self.source, None, self.digest)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.dest.read_text(), 'third party')

    def test_posix_fallback_unlink_failure_rolls_back_own_destination(self):
        module = self.module()
        real_unlink = module.os.unlink
        def blocked(name, *args, **kwargs):
            if name == self.source.name and kwargs.get('dir_fd') is not None:
                raise OSError('unlink blocked')
            return real_unlink(name, *args, **kwargs)
        with mock.patch.object(module, 'native_rename_noreplace', return_value=False), \
             mock.patch.object(module.os, 'unlink', side_effect=blocked):
            with self.assertRaisesRegex(ValueError, '自身が作った宛先を戻した'):
                module.adopt(self.root, self.source, None, self.digest)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.dest.exists())

    def test_posix_fallback_does_not_remove_replaced_destination(self):
        module = self.module()
        real_unlink = module.os.unlink
        def replaced(name, *args, **kwargs):
            if name == self.source.name and kwargs.get('dir_fd') is not None:
                self.dest.unlink()
                self.dest.write_text('third party')
                raise OSError('source unlink blocked')
            return real_unlink(name, *args, **kwargs)
        with mock.patch.object(module, 'native_rename_noreplace', return_value=False), \
             mock.patch.object(module.os, 'unlink', side_effect=replaced):
            with self.assertRaisesRegex(ValueError, '候補と宛先を保全した'):
                module.adopt(self.root, self.source, None, self.digest)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.dest.read_text(), 'third party')

    def test_posix_fallback_kill_between_link_and_unlink_leaves_both_names(self):
        marker = self.root / 'linked'
        code = '''import importlib.util, os, sys, time
script, root, source, digest, marker = sys.argv[1:]
spec = importlib.util.spec_from_file_location('child_adopt', script)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.native_rename_noreplace = lambda *args: False
real = m.os.unlink
def pause(name, *args, **kwargs):
    if name == os.path.basename(source) and kwargs.get('dir_fd') is not None:
        open(marker, 'w').write('linked')
        time.sleep(60)
    return real(name, *args, **kwargs)
m.os.unlink = pause
m.adopt(root, source, None, digest)
'''
        proc = subprocess.Popen([sys.executable, '-c', code, str(SCRIPT), str(self.root), str(self.source), self.digest, str(marker)])
        try:
            for _ in range(100):
                if marker.exists() and self.dest.exists():
                    break
                time.sleep(.02)
            self.assertTrue(marker.exists(), 'fallback did not reach its post-link window')
            self.assertTrue(self.dest.exists(), 'fallback did not create destination before signal')
            proc.kill(); proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=5)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.dest.exists())
        self.assertEqual(os.stat(self.source).st_ino, os.stat(self.dest).st_ino)

    def test_same_uid_can_remove_marker_documented_limit(self):
        # 親が周の印を渡しても、同じ利用者の子プログラムは消して helper を起動できる。
        # 正式入口の検査を OS の隔離とは扱わない。
        bypass = (
            'import os, subprocess, sys; '
            "os.environ.pop('DEV_WORKFLOW_LOOP_ITER', None); "
            'raise SystemExit(subprocess.run(sys.argv[1:]).returncode)'
        )
        result = subprocess.run(
            [sys.executable, '-c', bypass, sys.executable, str(SCRIPT), '--root', str(self.root),
             '--expect-sha256', self.digest, str(self.source)],
            env=dict(os.environ, DEV_WORKFLOW_LOOP_ITER='untrusted-parent'), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dest.exists())
