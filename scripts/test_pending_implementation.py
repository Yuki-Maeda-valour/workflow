"""保護記録候補の公開契約。CLI実測と文書検査を分けて扱う。"""
import ast
import builtins
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / 'plugins/dev-workflow/skills/do-task'
HELPER = SKILL / 'scripts/pending-implementation.py'
ROOT_IDS = ['xdg_state', 'home_state', 'home_cache']
ROOT_KEYS = {'id', 'input_path', 'input_path_bytes_hex', 'resolved_path',
             'resolved_path_bytes_hex', 'status', 'reason', 'identity', 'canonical_id'}
ROOT_HASH_KEYS = ROOT_KEYS - {'input_path', 'resolved_path'}
CANDIDATE_KEYS = {'root_id', 'path', 'path_bytes_hex', 'name_bytes_hex', 'kind',
                  'dev', 'ino', 'mode', 'uid', 'size', 'mtime_ns', 'ctime_ns'}
TOP_KEYS = {'schema_version', 'result', 'cwd', 'cwd_bytes_hex', 'complete',
            'roots', 'candidates', 'issues'}
CODES = {'invalid-arguments', 'invalid-cwd', 'unsupported-python',
         'unsupported-platform', 'missing-feature', 'no-roots',
         'resolution-failed', 'not-directory', 'unreadable',
         'changed-during-scan', 'entry-limit', 'candidate-limit',
         'time-limit', 'internal-error'}
RESULTS = {'none': 0, 'acknowledged': 0, 'confirmation-required': 10,
           'list-changed': 10, 'scan-unavailable': 11, 'usage': 2, 'internal': 20}
KINDS = {'directory', 'file', 'symlink', 'fifo', 'socket', 'block', 'character', 'other'}


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def expected_payload(result):
    # D4から独立に指定する。製品のhash用関数を期待値生成に使わない。
    return {'schema_version': 1, 'cwd_bytes_hex': result['cwd_bytes_hex'],
            'roots': [{key: root[key] for key in ROOT_HASH_KEYS} for root in result['roots']],
            'candidates': [{key: item[key] for key in CANDIDATE_KEYS - {'path'}}
                           for item in result['candidates']]}


class PendingFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pending-contract-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.cwd = self.base / 'cwd'
        self.home = self.base / 'home'
        self.xdg = self.base / 'xdg'
        for path in (self.cwd, self.home, self.xdg):
            path.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), XDG_STATE_HOME=str(self.xdg),
                        PYTHONDONTWRITEBYTECODE='1')
        self.roots = [self.xdg / 'dev-workflow', self.home / '.local/state/dev-workflow',
                      self.home / '.cache/dev-workflow']

    def create_roots(self):
        for path in self.roots:
            path.mkdir(parents=True, exist_ok=True)

    def check_schema(self, rc, result):
        self.assertIs(type(result), dict)
        complete = result['result'] in ('none', 'acknowledged', 'confirmation-required', 'list-changed')
        self.assertEqual(set(result), TOP_KEYS | ({'list_sha256'} if complete else set()))
        self.assertIs(type(result['schema_version']), int)
        self.assertEqual(result['schema_version'], 1)
        self.assertIs(type(result['complete']), bool)
        self.assertEqual(result['complete'], complete)
        self.assertEqual(rc, RESULTS[result['result']])
        for key in ('cwd', 'cwd_bytes_hex'):
            self.assertTrue(result[key] is None or type(result[key]) is str)
        self.assertEqual(result['cwd'] is None, result['cwd_bytes_hex'] is None)
        if result['cwd'] is not None:
            self.assertEqual(os.fsencode(result['cwd']).hex(), result['cwd_bytes_hex'])
        for key in ('roots', 'candidates', 'issues'):
            self.assertIs(type(result[key]), list)
        self.assertEqual([r['id'] for r in result['roots']], ROOT_IDS[:len(result['roots'])])
        if complete:
            self.assertEqual(len(result['roots']), 3)
            self.assertEqual(result['issues'], [])
            self.assertRegex(result['list_sha256'], r'^[0-9a-f]{64}$')
            self.assertEqual(result['list_sha256'], hashlib.sha256(canonical(expected_payload(result))).hexdigest())
        else:
            self.assertGreater(len(result['issues']), 0)
        for root in result['roots']:
            self.assertEqual(set(root), ROOT_KEYS)
            self.assertIn(root['status'], ('skipped', 'absent', 'present', 'error'))
            for prefix in ('input', 'resolved'):
                value, encoded = root[prefix + '_path'], root[prefix + '_path_bytes_hex']
                self.assertEqual(value is None, encoded is None)
                if value is not None:
                    self.assertIs(type(value), str)
                    self.assertEqual(encoded, os.fsencode(value).hex())
            if root['status'] == 'present':
                self.assertEqual(set(root['identity']), {'dev', 'ino', 'mode', 'uid'})
                self.assertTrue(all(type(v) is int for v in root['identity'].values()))
                self.assertIsNone(root['reason'])
                self.assertIn(root['canonical_id'], ROOT_IDS)
            else:
                self.assertIsNone(root['identity'])
                self.assertIsNone(root['canonical_id'])
                expected = {'skipped': {'unset', 'relative'}, 'absent': {'missing'}, 'error': CODES}
                self.assertIn(root['reason'], expected[root['status']])
        order = []
        for candidate in result['candidates']:
            self.assertEqual(set(candidate), CANDIDATE_KEYS)
            self.assertIn(candidate['root_id'], ROOT_IDS)
            self.assertIn(candidate['kind'], KINDS)
            self.assertIs(type(candidate['path']), str)
            self.assertEqual(candidate['path_bytes_hex'], os.fsencode(candidate['path']).hex())
            name = bytes.fromhex(candidate['name_bytes_hex'])
            self.assertTrue(name.startswith(b'guard-') and len(name) > 6)
            self.assertEqual(candidate['name_bytes_hex'], name.hex())
            for key in ('dev', 'ino', 'mode', 'uid', 'size', 'mtime_ns', 'ctime_ns'):
                self.assertIs(type(candidate[key]), int)
            order.append((ROOT_IDS.index(candidate['root_id']), name))
        self.assertEqual(order, sorted(order))
        for issue in result['issues']:
            self.assertEqual(set(issue), {'code', 'root_id', 'path_bytes_hex', 'message'})
            self.assertIn(issue['code'], CODES)
            self.assertIn(issue['root_id'], ROOT_IDS + [None])
            self.assertIs(type(issue['message']), str)
            self.assertRegex(issue['message'], r'[^\x00-\x7f]')
            if issue['path_bytes_hex'] is not None:
                self.assertEqual(bytes.fromhex(issue['path_bytes_hex']).hex(), issue['path_bytes_hex'])
        issue_order = lambda item: (item['code'], -1 if item['root_id'] is None else ROOT_IDS.index(item['root_id']),
                                    '' if item['path_bytes_hex'] is None else item['path_bytes_hex'], item['message'])
        self.assertEqual(result['issues'], sorted(result['issues'], key=issue_order))

    def cli(self, args=None, env=None, cwd=None):
        args = ['scan', '--cwd', str(cwd or self.cwd)] if args is None else args
        cp = subprocess.run([sys.executable, '-I', '-B', str(HELPER)] + args,
                            env=self.env if env is None else env, cwd=str(self.base),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
        raw = cp.stdout
        self.assertTrue(raw.isascii(), raw)
        self.assertEqual(raw.count(b'\n'), 1, raw)
        result = json.loads(raw)
        self.assertEqual(raw, canonical(result) + b'\n')
        self.check_schema(cp.returncode, result)
        return cp.returncode, result

    def scan(self, expected, ack=None, **kwargs):
        args = ['scan', '--cwd', str(kwargs.pop('cwd', self.cwd))]
        if ack is not None:
            args += ['--acknowledged-list', ack]
        rc, result = self.cli(args, **kwargs)
        self.assertEqual(result['result'], expected)
        return result


class PendingCLITests(PendingFixture):
    def test_absent_empty_roots_and_no_creation(self):
        result = self.scan('none')
        self.assertEqual([r['status'] for r in result['roots']], ['absent'] * 3)
        self.assertFalse(any(p.exists() for p in self.roots))
        self.create_roots()
        result = self.scan('none')
        self.assertEqual([r['status'] for r in result['roots']], ['present'] * 3)
        self.assertEqual(result['candidates'], [])

    def test_unset_relative_and_no_roots(self):
        for xdg, home in [(None, str(self.home)), ('relative', str(self.home)),
                          (str(self.xdg), None), (str(self.xdg), 'relative'),
                          (None, None), ('relative', 'relative')]:
            with self.subTest(xdg=xdg, home=home):
                env = dict(self.env)
                for key, value in [('XDG_STATE_HOME', xdg), ('HOME', home)]:
                    if value is None:
                        env.pop(key, None)
                    else:
                        env[key] = value
                usable = any(value and os.path.isabs(value) for value in (xdg, home))
                result = self.scan('none' if usable else 'scan-unavailable', env=env)
                for root in result['roots']:
                    value = xdg if root['id'] == 'xdg_state' else home
                    if not value or not os.path.isabs(value):
                        self.assertEqual(root['status'], 'skipped')
                        self.assertEqual(root['reason'], 'unset' if value is None else 'relative')
                        self.assertIsNone(root['input_path'])
                if not usable:
                    self.assertIn('no-roots', [i['code'] for i in result['issues']])

    def test_all_roots_home_symlink_and_same_inode_alias(self):
        self.create_roots()
        for n, root in enumerate(self.roots):
            (root / ('guard-' + str(n))).mkdir()
        result = self.scan('confirmation-required')
        self.assertEqual([c['root_id'] for c in result['candidates']], ROOT_IDS)
        alias = self.base / 'home-alias'
        alias.symlink_to(self.home, target_is_directory=True)
        env = dict(self.env, HOME=str(alias), XDG_STATE_HOME=str(self.home / '.local/state'))
        result = self.scan('confirmation-required', env=env)
        self.assertEqual([r['canonical_id'] for r in result['roots']], ['xdg_state', 'xdg_state', 'home_cache'])
        self.assertEqual(len(result['candidates']), 2)
        self.assertNotEqual(result['roots'][0]['input_path_bytes_hex'], result['roots'][1]['input_path_bytes_hex'])
        self.assertEqual(result['roots'][0]['identity'], result['roots'][1]['identity'])

    def test_direct_names_kinds_and_non_utf8_canonical_output(self):
        self.create_roots()
        root = self.roots[0]
        (root / 'guard-directory').mkdir()
        (root / 'guard-file').write_bytes(b'plain sentinel')
        (root / 'guard-symlink').symlink_to(self.base / 'missing-target')
        os.mkfifo(str(root / 'guard-fifo'))
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(root / 'guard-socket'))
        for name in ('guard-', 'Guard-a', 'implement-a', 'other'):
            (root / name).mkdir()
            (root / name / 'guard-deep').mkdir()
        names = [b'guard-\xff', 'guard-日本語'.encode(), b'guard-a b', b'guard-\n',
                 b'guard-\xc2\x85', b'guard-*[abc]?']
        for name in names:
            Path(os.fsdecode(os.fsencode(root) + b'/' + name)).write_bytes(b'normal')
        result = self.scan('confirmation-required')
        by_name = {bytes.fromhex(c['name_bytes_hex']): c for c in result['candidates']}
        self.assertEqual(len(by_name), 5 + len(names))
        for kind in ('directory', 'file', 'symlink', 'fifo', 'socket'):
            self.assertEqual(by_name[('guard-' + kind).encode()]['kind'], kind)
        self.assertEqual(set(names) - set(by_name), set())

    def test_acknowledgement_changes_cwd_inode_name_root_and_count(self):
        self.create_roots()
        guard = self.roots[0] / 'guard-one'
        guard.mkdir()
        initial = self.scan('confirmation-required')['list_sha256']
        self.scan('acknowledged', ack=initial)
        self.scan('acknowledged', ack=initial.upper())
        other = self.base / 'other-cwd'
        other.mkdir()
        self.scan('list-changed', ack=initial, cwd=other)
        guard.rename(self.roots[0] / 'guard-renamed')
        renamed = self.scan('list-changed', ack=initial)['list_sha256']
        (self.roots[0] / 'guard-renamed').rename(self.base / 'old-guard')
        (self.roots[0] / 'guard-renamed').mkdir()
        replaced = self.scan('list-changed', ack=renamed)['list_sha256']
        (self.roots[0] / 'guard-added').mkdir()
        added = self.scan('list-changed', ack=replaced)['list_sha256']
        (self.roots[0] / 'guard-added').rmdir()
        self.scan('list-changed', ack=added)
        alternate = self.base / 'new-xdg'
        alternate.mkdir()
        self.scan('list-changed', ack=replaced, env=dict(self.env, XDG_STATE_HOME=str(alternate)))
        (self.roots[0] / 'guard-renamed').rmdir()
        self.scan('list-changed', ack=replaced)
        self.scan('none')

    def test_handwork_changes_times_same_identity_and_new_ack(self):
        self.create_roots()
        guard = self.roots[0] / 'guard-handwork'
        guard.mkdir()
        # 比較前だけmtimeを過去へ置く。変化は通常のmkdir/rmdir自身に起こさせる。
        os.utime(guard, ns=(1000000000, 1000000000))
        before = self.scan('confirmation-required')
        child = guard / '.work-fixture'
        child.mkdir()
        child.rmdir()
        after = self.scan('list-changed', ack=before['list_sha256'])
        self.assertNotEqual(before['candidates'][0]['mtime_ns'], after['candidates'][0]['mtime_ns'])
        for key in ('dev', 'ino', 'kind', 'name_bytes_hex'):
            self.assertEqual(before['candidates'][0][key], after['candidates'][0][key])
        self.scan('acknowledged', ack=after['list_sha256'])
        self.assertTrue(guard.is_dir())
        # 人の工程結果の意味はこのCLI試験では検証しない。

    def test_internal_contents_and_fixture_metadata_unchanged(self):
        self.create_roots()
        for name, data in [('guard-old', b'old format'), ('guard-invalid', b'not json'), ('guard-empty', b'')]:
            guard = self.roots[0] / name
            guard.mkdir()
            (guard / 'manifest').write_bytes(data)
            (guard / 'unreadable').write_bytes(b'nonsecret sentinel')
            (guard / 'unreadable').chmod(0)
        def snapshot():
            values = {}
            for path in self.base.rglob('*'):
                st = path.lstat()
                values[str(path)] = (st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns, st.st_ctime_ns,
                                     path.read_bytes() if path.is_file() and path.name != 'unreadable' else None)
            return values
        before = snapshot()
        result = self.scan('confirmation-required')
        self.assertEqual(len(result['candidates']), 3)
        self.assertEqual(before, snapshot())
        # 非読取の根拠は別のAPI境界試験とソース点検。前後不変だけでは主張しない。

    def test_root_file_broken_link_and_loop_stop(self):
        root = self.roots[0]
        for kind in ('file', 'broken', 'loop'):
            with self.subTest(kind=kind):
                if kind == 'file':
                    root.write_bytes(b'ordinary')
                elif kind == 'broken':
                    root.symlink_to(self.base / 'missing')
                else:
                    root.symlink_to(root.name)
                result = self.scan('scan-unavailable', ack='0' * 64)
                self.assertIn(result['issues'][0]['code'], {'not-directory', 'resolution-failed'})
                root.unlink()

    def test_entry_limit_and_candidate_limit_boundaries(self):
        self.create_roots()
        for n in range(4096):
            (self.roots[0] / ('entry-' + str(n))).touch()
        self.scan('none')
        (self.roots[0] / 'entry-over').touch()
        result = self.scan('scan-unavailable')
        self.assertIn('entry-limit', [i['code'] for i in result['issues']])
        for path in self.roots[0].iterdir():
            path.unlink()
        for n in range(128):
            (self.roots[n % 3] / ('guard-' + str(n))).mkdir()
        self.assertEqual(len(self.scan('confirmation-required')['candidates']), 128)
        (self.roots[2] / 'guard-over').mkdir()
        result = self.scan('scan-unavailable')
        self.assertIn('candidate-limit', [i['code'] for i in result['issues']])

    def test_usage_unknown_operations_ack_format_and_help(self):
        cases = [[], ['scan'], ['cleanup'], ['scan', '--cwd', str(self.base / 'missing')],
                 ['scan', '--cwd', str(HELPER)]]
        cases += [['scan', '--cwd', str(self.cwd), arg] for arg in
                  ('--cleanup', '--resume', '--kill', '--trust-state', '--ignore-state', '--limit')]
        cases += [['scan', '--cwd', str(self.cwd), '--acknowledged-list', value]
                  for value in ('', '0' * 63, '0' * 65, ' ' + '0' * 64, '0x' + '0' * 64, 'ａ' * 64)]
        for args in cases:
            with self.subTest(args=args):
                rc, result = self.cli(args)
                self.assertEqual(rc, 2)
                self.assertEqual(result['result'], 'usage')
        cp = subprocess.run([sys.executable, '-I', '-B', str(HELPER), 'scan', '--help'],
                            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(cp.returncode, 0)
        self.assertRegex(cp.stdout.decode(), r'[^\x00-\x7f]')
        self.assertIn('--cwd', cp.stdout.decode())
        with self.assertRaises(json.JSONDecodeError):
            json.loads(cp.stdout)


class PendingBoundaryTests(PendingFixture):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location('pending_contract_helper', HELPER)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.create_roots()

    def run_unit(self, expected, code=None):
        with mock.patch.dict(os.environ, self.env, clear=True):
            rc, result = self.module.run(['scan', '--cwd', str(self.cwd)])
        self.check_schema(rc, result)
        self.assertEqual(result['result'], expected)
        if code:
            self.assertIn(code, [i['code'] for i in result['issues']])
        return result

    @contextlib.contextmanager
    def patched_os(self, name, replacement):
        original = getattr(os, name)
        # 能力表は関数identityを使う。境界patch自身で偽の機能不足を作らない。
        with contextlib.ExitStack() as stack:
            for support in ('supports_fd', 'supports_dir_fd', 'supports_follow_symlinks'):
                values = getattr(os, support)
                if original in values:
                    stack.enter_context(mock.patch.object(os, support, values | {replacement}))
            stack.enter_context(mock.patch.object(os, name, replacement))
            yield

    def test_missing_capabilities_stop_before_scan(self):
        for support, function in [('supports_fd', os.scandir), ('supports_dir_fd', os.stat),
                                  ('supports_follow_symlinks', os.stat)]:
            with self.subTest(support=support), mock.patch.object(os, support, getattr(os, support) - {function}):
                result = self.run_unit('scan-unavailable', 'missing-feature')
                self.assertEqual(result['roots'], [])
        for attr in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_CLOEXEC', 'open', 'close', 'fstat', 'fsencode', 'fsdecode'):
            with self.subTest(attr=attr), mock.patch.object(os, attr, None):
                self.run_unit('scan-unavailable', 'missing-feature')
        with mock.patch.object(self.module.sys, 'version_info', (3, 7)):
            self.run_unit('scan-unavailable', 'unsupported-python')
        with mock.patch.object(os, 'name', 'nt'):
            self.run_unit('scan-unavailable', 'unsupported-platform')

    def test_runtime_unavailable_denied_and_unknown_exception(self):
        for error, expected, code in [(PermissionError('fixture denied'), 'scan-unavailable', 'unreadable'),
                                      (NotImplementedError('fixture unsupported'), 'scan-unavailable', 'missing-feature'),
                                      (RuntimeError('fixture internal'), 'internal', 'internal-error')]:
            def fail(*args, **kwargs):
                raise error
            with self.subTest(error=type(error).__name__), self.patched_os('scandir', fail):
                self.run_unit(expected, code)

    def test_time_limit_uses_monotonic(self):
        with mock.patch.object(self.module.time, 'monotonic', side_effect=[0.0] + [6.0] * 100):
            self.run_unit('scan-unavailable', 'time-limit')

    def test_candidate_disappears_or_metadata_changes(self):
        guard = self.roots[0] / 'guard-race'
        guard.mkdir()
        original = os.stat
        for mode in ('disappear', 'change'):
            seen = [0]
            def change(path, *args, **kwargs):
                st = original(path, *args, **kwargs)
                if os.fsencode(path).split(b'/')[-1] == b'guard-race':
                    self.assertFalse(kwargs.get('follow_symlinks', True))
                    self.assertIsNotNone(kwargs.get('dir_fd'))
                    seen[0] += 1
                    if mode == 'disappear':
                        raise FileNotFoundError('fixture vanished')
                    if seen[0] >= 2:
                        data = {key: getattr(st, key) for key in dir(st) if key.startswith('st_')}
                        data['st_ino'] += 1
                        return type('ChangedStat', (), data)()
                return st
            with self.subTest(mode=mode), self.patched_os('stat', change):
                self.run_unit('scan-unavailable', 'changed-during-scan')
            self.assertGreater(seen[0], 0)

    def test_candidate_internals_never_opened_or_scanned(self):
        guard = self.roots[0] / 'guard-private'
        guard.mkdir()
        (guard / 'manifest').write_bytes(b'nonsecret unread data')
        (guard / 'taskmd-body').write_bytes(b'not a task')
        (self.roots[0] / 'guard-link').symlink_to(guard)
        original_open, original_scandir = os.open, os.scandir
        original_fstat = os.fstat
        directory_id = (guard.stat().st_dev, guard.stat().st_ino)
        calls = []
        def open_boundary(path, flags, *args, **kwargs):
            raw = os.fsencode(path)
            calls.append(('open', raw))
            self.assertNotIn(b'guard-', raw)
            self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND), 0)
            return original_open(path, flags, *args, **kwargs)
        def scan_boundary(fd):
            self.assertIs(type(fd), int)
            st = original_fstat(fd)
            self.assertNotEqual((st.st_dev, st.st_ino), directory_id)
            calls.append(('scandir', (st.st_dev, st.st_ino)))
            return original_scandir(fd)
        with self.patched_os('open', open_boundary), self.patched_os('scandir', scan_boundary), \
             mock.patch.object(builtins, 'open', side_effect=AssertionError('body open forbidden')), \
             mock.patch.object(os, 'listdir', side_effect=AssertionError('unbounded listdir forbidden')), \
             mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('child forbidden')):
            self.run_unit('confirmation-required')
        self.assertEqual(len([c for c in calls if c[0] == 'scandir']), 3)

    def test_root_alias_is_enumerated_once(self):
        self.env['XDG_STATE_HOME'] = str(self.home / '.local/state')
        original = os.scandir
        identities = []
        def count(fd):
            st = os.fstat(fd)
            identities.append((st.st_dev, st.st_ino))
            return original(fd)
        with self.patched_os('scandir', count):
            self.run_unit('none')
        self.assertEqual(len(identities), 2)
        self.assertEqual(len(set(identities)), 2)

    def test_fixed_payload_hash_and_excluded_display_values(self):
        roots = []
        for root_id in ROOT_IDS:
            roots.append({'id': root_id, 'input_path': None, 'input_path_bytes_hex': None,
                          'resolved_path': None, 'resolved_path_bytes_hex': None,
                          'status': 'skipped', 'reason': 'unset', 'identity': None, 'canonical_id': None})
        roots[0].update(input_path='/state', input_path_bytes_hex='2f7374617465',
                        resolved_path='/state', resolved_path_bytes_hex='2f7374617465',
                        status='present', reason=None, identity={'dev': 7, 'ino': 11, 'mode': 16832, 'uid': 42},
                        canonical_id='xdg_state')
        roots[1].update(input_path='/alias', input_path_bytes_hex='2f616c696173',
                        resolved_path='/state', resolved_path_bytes_hex='2f7374617465',
                        status='present', reason=None, identity={'dev': 7, 'ino': 11, 'mode': 16832, 'uid': 42},
                        canonical_id='xdg_state')
        candidate = {'root_id': 'xdg_state', 'path': '/state/guard-x',
                     'path_bytes_hex': '2f73746174652f67756172642d78', 'name_bytes_hex': '67756172642d78',
                     'kind': 'directory', 'dev': 7, 'ino': 12, 'mode': 16832, 'uid': 42,
                     'size': 4096, 'mtime_ns': 100, 'ctime_ns': 200}
        result = {'schema_version': 1, 'cwd': '/cwd', 'cwd_bytes_hex': '2f637764',
                  'complete': True, 'result': 'confirmation-required', 'roots': roots,
                  'candidates': [candidate], 'issues': []}
        payload = expected_payload(result)
        self.assertEqual(set(payload), {'schema_version', 'cwd_bytes_hex', 'roots', 'candidates'})
        encoded = canonical(payload)
        self.assertNotEqual(encoded[-1:], b'\n')
        expected = hashlib.sha256(encoded).hexdigest()
        self.assertEqual(self.module.list_digest(result), expected)
        result.update(cwd='display changed', result='acknowledged', complete=False,
                      issues=[{'message': '表示だけ'}], list_sha256='unused')
        roots[0]['input_path'] = '表示だけ'
        roots[0]['resolved_path'] = '表示だけ'
        candidate['path'] = '表示だけ'
        self.assertEqual(self.module.list_digest(result), expected)
        roots[1]['input_path_bytes_hex'] = '2f616c69617332'
        self.assertNotEqual(self.module.list_digest(result), expected)

    def test_root_changes_while_fd_is_held(self):
        original = os.fstat
        wanted = (self.roots[0].stat().st_dev, self.roots[0].stat().st_ino)
        calls = [0]
        def change(fd):
            info = original(fd)
            if (info.st_dev, info.st_ino) == wanted:
                calls[0] += 1
                if calls[0] >= 2:
                    data = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                    data['st_ino'] += 1
                    return type('ChangedRoot', (), data)()
            return info
        with self.patched_os('fstat', change):
            self.run_unit('scan-unavailable', 'changed-during-scan')
        self.assertGreaterEqual(calls[0], 2)

    def test_special_file_classification_without_creating_devices(self):
        for mode, expected in [(stat.S_IFBLK, 'block'), (stat.S_IFCHR, 'character'), (0, 'other')]:
            with self.subTest(mode=mode):
                self.assertEqual(self.module.candidate_kind(mode | 0o600), expected)

    def test_missing_stat_fields_and_clock(self):
        fields = ['st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_size', 'st_mtime_ns', 'st_ctime_ns']
        for missing in fields:
            cls = type('PartialStat', (), {key: 1 for key in fields if key != missing})
            with self.subTest(missing=missing), mock.patch.object(os, 'stat_result', cls):
                self.run_unit('scan-unavailable', 'missing-feature')
        with mock.patch.object(self.module.time, 'monotonic', None):
            self.run_unit('scan-unavailable', 'missing-feature')

    def test_python38_syntax_and_no_process_or_write_entry(self):
        source = HELPER.read_text(encoding='utf-8')
        tree = ast.parse(source, feature_version=(3, 8))
        imports = {n.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for n in node.names}
        imports |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imports & {'subprocess', 'multiprocessing', 'pty'})
        forbidden = {'system', 'popen', 'spawn', 'fork', 'execve', 'kill', 'write', 'write_bytes',
                     'write_text', 'mkdir', 'makedirs', 'rename', 'replace', 'unlink', 'remove', 'rmdir',
                     'chmod', 'chown', 'utime', 'listdir', 'is_relative_to'}
        calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and not (node.func.attr == 'write' and isinstance(node.func.value, ast.Attribute)
                          and isinstance(node.func.value.value, ast.Name)
                          and node.func.value.value.id == 'sys'
                          and node.func.value.attr in ('stdout', 'stderr'))}
        self.assertFalse(calls & forbidden, calls & forbidden)


class PendingDocumentContractTests(unittest.TestCase):
    """文書位置と必須条件の退行検査。人の判断の意味は独立レビューで確かめる。"""
    def test_entry_before_target_body_git_and_preserved_implementation_paths(self):
        do = (SKILL / 'SKILL.md').read_text()
        ship = (SKILL.parent / 'ship-task/SKILL.md').read_text()
        for text in (do, ship):
            phase = text.index('## Phase 0:')
            entry = text.index('pending-implementation.py scan', phase)
            self.assertLess(entry, text.index('1. **', phase))
            self.assertIn('10/11/2/20・不正 JSON・起動不能なら停止', text)
            self.assertIn('PENDING_LIST_SHA256', text)
            self.assertIn('非 Git', text)
            self.assertIn('タスク本文', text)
            self.assertIn('環境', text[:phase])
        self.assertLess(do.index('環境の照合後、Phase 0'), do.index('**前提**(候補の選択'))
        self.assertIn('Phase 3 の take 後・Phase 4・差戻し・内蔵への引継ぎでは再走査しない', do)
        self.assertIn('タスク名や `--task` の有無を待たない', ship)
        # 5導線の意味は既存test_implementation_gitと独立レビューが扱う。
        self.assertIn('implementation-git.md', do)

    def test_full_human_confirmation_failure_and_handwork_contract(self):
        text = (SKILL / 'references/external-runners.md').read_text()
        contract = text[text.index('### 12-9.'):]
        required = ['全件について回答が必要', '「続けて」「全部OK」', '失われていれば推測や再算出をせず停止',
                    'PIDやプロセス名の一覧だけ', '失われた保護hashの新しい出典として使いません',
                    '無条件に繰り返さない', 'mtime/ctime変化だけでもlist_sha256は変わります',
                    '以前の回答・工程結果を流用しません', '候補が消えた場合も自動承認しません',
                    '同じship-taskからdo-taskへ入るときはscanを再実行', 'ログやprofileから回答を復元せず',
                    '未知キー、重複キー、キー不足、型違い', '結果と終了値の矛盾', '起動不能として停止',
                    '実行可能な skill dispatcher はない', 'AI が手順を守ることや OS による強制は保証しない']
        for term in required:
            with self.subTest(term=term):
                self.assertIn(term, contract)
        self.assertIn('Python 3.8', (SKILL / 'references/runtime-requirements.md').read_text())


if __name__ == '__main__':
    unittest.main()
