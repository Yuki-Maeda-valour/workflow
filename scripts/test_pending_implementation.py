"""保護記録候補の公開契約。CLI実測と文書検査を分けて扱う。"""
import ast
import builtins
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import support_git
from support_cli import run_python
from support_load import load_script

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / 'plugins/dev-workflow/skills/do-task'
HELPER = SKILL / 'scripts/pending-implementation.py'
ROOT_IDS = ['xdg_state', 'home_state', 'home_cache']
ROOT_KEYS = {'id', 'input_path', 'input_path_bytes_hex', 'resolved_path',
             'resolved_path_bytes_hex', 'status', 'reason', 'identity', 'canonical_id'}
ROOT_HASH_KEYS = ROOT_KEYS - {'input_path', 'resolved_path'}
CANDIDATE_KEYS = {'root_id', 'path', 'path_bytes_hex', 'name_bytes_hex', 'kind',
                  'dev', 'ino', 'mode', 'uid', 'size', 'mtime_ns', 'ctime_ns',
                  'relation', 'reason', 'owner'}
OWNER_KEYS = {'top', 'top_bytes_hex', 'common', 'common_bytes_hex', 'taskdir', 'taskdir_bytes_hex'}
RELATIONS = {'confirmation-required', 'other-project'}
REASONS = {'no-mark', 'mark-invalid', 'guard-mismatch', 'owner-path-mismatch', 'git-unavailable',
           'overlap', 'overlap-unknown'}
# 印を読めて形が正しい候補だけがownerを持つ。
OWNER_READ_REASONS = {None, 'guard-mismatch', 'owner-path-mismatch', 'git-unavailable', 'overlap', 'overlap-unknown'}
MARK_KEYS = ('version', 'guard_dev', 'guard_ino', 'top', 'top_dev', 'top_ino', 'common', 'common_dev',
             'common_ino', 'taskdir', 'taskdir_dev', 'taskdir_ino', 'tmpdir')
OVERLAP_LINE = '重なりのある候補があるため、identity の確認値では続行できません。list_sha256 で確認してください。'
# list_sha256の入力へ写す候補の11項目。出力の候補に項目が増えても、この11項目だけを写す。
LIST_CANDIDATE_KEYS = ('root_id', 'path_bytes_hex', 'name_bytes_hex', 'kind', 'dev', 'ino',
                       'mode', 'uid', 'size', 'mtime_ns', 'ctime_ns')
# identity_sha256の入力へ写す候補の8項目。更新時刻・大きさ・権限は入れない。reasonは段階2で足した。
IDENTITY_CANDIDATE_KEYS = ('root_id', 'path_bytes_hex', 'name_bytes_hex', 'kind', 'dev', 'ino', 'uid', 'reason')
# identity_sha256の入力では、置き場のidentityから権限(mode)を外す。
IDENTITY_ROOT_KEYS = ('dev', 'ino', 'uid')
TOP_KEYS = {'schema_version', 'result', 'cwd', 'cwd_bytes_hex', 'complete',
            'roots', 'candidates', 'issues'}
DIGEST_KEYS = {'list_sha256', 'identity_sha256'}
CODES = {'invalid-arguments', 'invalid-cwd', 'unsupported-python',
         'unsupported-platform', 'missing-feature', 'no-roots',
         'resolution-failed', 'not-directory', 'unreadable',
         'changed-during-scan', 'entry-limit', 'candidate-limit',
         'time-limit', 'internal-error'}
RESULTS = {'none': 0, 'other-projects-only': 0, 'acknowledged': 0, 'confirmation-required': 10,
           'list-changed': 10, 'scan-unavailable': 11, 'usage': 2, 'internal': 20}
COMPLETE_RESULTS = ('none', 'other-projects-only', 'acknowledged', 'confirmation-required', 'list-changed')
KINDS = {'directory', 'file', 'symlink', 'fifo', 'socket', 'block', 'character', 'other'}


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def confirmation_candidates(result):
    # 確認値は「確認が要る」候補だけで計算する。別プロジェクトの記録は入れない。
    return [item for item in result['candidates'] if item['relation'] == 'confirmation-required']


def expected_payload(result):
    # D4から独立に指定する。製品のhash用関数を期待値生成に使わない。
    return {'schema_version': 1, 'cwd_bytes_hex': result['cwd_bytes_hex'],
            'roots': [{key: root[key] for key in ROOT_HASH_KEYS} for root in result['roots']],
            'candidates': [{key: item[key] for key in LIST_CANDIDATE_KEYS}
                           for item in confirmation_candidates(result)]}


def expected_identity_payload(result):
    # 実体だけの確認値の入力。製品のhash用関数を期待値生成に使わない。
    roots = []
    for root in result['roots']:
        item = {key: root[key] for key in ROOT_HASH_KEYS}
        if item['identity'] is not None:
            item['identity'] = {key: item['identity'][key] for key in IDENTITY_ROOT_KEYS}
        roots.append(item)
    return {'schema_version': 1, 'digest': 'identity', 'cwd_bytes_hex': result['cwd_bytes_hex'],
            'roots': roots,
            'candidates': [{key: item[key] for key in IDENTITY_CANDIDATE_KEYS}
                           for item in confirmation_candidates(result)]}


def fixed_result():
    # 確認値の入力を固定値で確かめるための結果。値はJSONに出る形だけを持つ。
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
                 'size': 4096, 'mtime_ns': 100, 'ctime_ns': 200,
                 'relation': 'confirmation-required', 'reason': 'no-mark', 'owner': None}
    return {'schema_version': 2, 'cwd': '/cwd', 'cwd_bytes_hex': '2f637764',
            'complete': True, 'result': 'confirmation-required', 'roots': roots,
            'candidates': [candidate], 'issues': []}


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
        # 今回のGitの置き場を決める環境変数は、試験ごとに明示して渡す。
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR'):
            self.env.pop(name, None)
        self.last_stderr = b''
        self.roots = [self.xdg / 'dev-workflow', self.home / '.local/state/dev-workflow',
                      self.home / '.cache/dev-workflow']

    def create_roots(self):
        for path in self.roots:
            path.mkdir(parents=True, exist_ok=True)

    def check_schema(self, rc, result):
        self.assertIs(type(result), dict)
        complete = result['result'] in COMPLETE_RESULTS
        self.assertEqual(set(result), TOP_KEYS | (DIGEST_KEYS if complete else set()))
        self.assertIs(type(result['schema_version']), int)
        self.assertEqual(result['schema_version'], 2)
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
            for key in DIGEST_KEYS:
                self.assertRegex(result[key], r'^[0-9a-f]{64}$')
            self.assertEqual(result['list_sha256'], hashlib.sha256(canonical(expected_payload(result))).hexdigest())
            self.assertEqual(result['identity_sha256'],
                             hashlib.sha256(canonical(expected_identity_payload(result))).hexdigest())
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
            self.assertIn(candidate['relation'], RELATIONS)
            self.assertIn(candidate['reason'], REASONS | {None})
            self.assertEqual(candidate['relation'] == 'other-project', candidate['reason'] is None)
            if candidate['reason'] in OWNER_READ_REASONS:
                self.assertIs(type(candidate['owner']), dict)
                self.assertEqual(set(candidate['owner']), OWNER_KEYS)
                for key in ('top', 'common', 'taskdir'):
                    self.assertIs(type(candidate['owner'][key]), str)
                    self.assertEqual(candidate['owner'][key + '_bytes_hex'], os.fsencode(candidate['owner'][key]).hex())
                    self.assertTrue(candidate['owner'][key].startswith('/'))
            else:
                self.assertIsNone(candidate['owner'])
            order.append((ROOT_IDS.index(candidate['root_id']), name))
        self.assertEqual(order, sorted(order))
        if complete:
            conflicts = confirmation_candidates(result)
            others = [c for c in result['candidates'] if c['relation'] == 'other-project']
            if result['result'] == 'none':
                self.assertEqual(result['candidates'], [])
            elif result['result'] == 'other-projects-only':
                self.assertEqual((len(conflicts), len(others) > 0), (0, True))
            elif result['result'] == 'confirmation-required':
                self.assertGreater(len(conflicts), 0)
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
        cp = run_python(str(HELPER), *args, flags=('-I', '-B'), env=self.env if env is None else env,
                        cwd=str(self.base), timeout=12, text=False, stdin=None)
        raw = cp.stdout
        self.last_stderr = cp.stderr
        self.assertTrue(raw.isascii(), raw)
        self.assertEqual(raw.count(b'\n'), 1, raw)
        result = json.loads(raw)
        self.assertEqual(raw, canonical(result) + b'\n')
        self.check_schema(cp.returncode, result)
        return cp.returncode, result

    def scan(self, expected, ack=None, ack_identities=None, **kwargs):
        args = ['scan', '--cwd', str(kwargs.pop('cwd', self.cwd))]
        if ack is not None:
            args += ['--acknowledged-list', ack]
        if ack_identities is not None:
            args += ['--acknowledged-identities', ack_identities]
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
        self.assertEqual(before['identity_sha256'], after['identity_sha256'])
        self.scan('acknowledged', ack=after['list_sha256'])
        self.assertTrue(guard.is_dir())
        # 人の工程結果の意味はこのCLI試験では検証しない。

    def test_identity_ack_ignores_time_size_and_mode_changes(self):
        self.create_roots()
        directory = self.roots[0] / 'guard-directory'
        directory.mkdir()
        regular = self.roots[1] / 'guard-file'
        regular.write_bytes(b'before')
        # 比較前だけ時刻と権限を決めておく。変化は通常の作成・削除・書込み・chmodに起こさせる。
        for path, mode in ((self.roots[0], 0o755), (directory, 0o755), (regular, 0o644)):
            path.chmod(mode)
            os.utime(path, ns=(1000000000, 1000000000))
        before = self.scan('confirmation-required')
        # 1. 更新時刻だけの変化: list_sha256は今までどおり変わり、identity_sha256は変わらない。
        child = directory / '.work-fixture'
        child.mkdir()
        child.rmdir()
        timed = self.scan('acknowledged', ack_identities=before['identity_sha256'])
        self.assertNotEqual(before['candidates'][0]['mtime_ns'], timed['candidates'][0]['mtime_ns'])
        self.assertNotEqual(before['list_sha256'], timed['list_sha256'])
        self.assertEqual(before['identity_sha256'], timed['identity_sha256'])
        self.scan('list-changed', ack=before['list_sha256'])
        # 2. 大きさと権限(置き場の権限を含む)の変化も、identity_sha256では同じ候補とみなす。
        regular.write_bytes(b'after, longer than before')
        directory.chmod(0o700)
        regular.chmod(0o600)
        self.roots[0].chmod(0o700)
        after = self.scan('acknowledged', ack_identities=before['identity_sha256'])
        self.scan('acknowledged', ack_identities=before['identity_sha256'].upper())
        self.assertNotEqual(before['candidates'][1]['size'], after['candidates'][1]['size'])
        for index in (0, 1):
            self.assertNotEqual(before['candidates'][index]['mode'], after['candidates'][index]['mode'])
            for key in IDENTITY_CANDIDATE_KEYS:
                self.assertEqual(before['candidates'][index][key], after['candidates'][index][key])
        self.assertNotEqual(before['roots'][0]['identity']['mode'], after['roots'][0]['identity']['mode'])
        self.assertEqual(before['identity_sha256'], after['identity_sha256'])
        self.scan('list-changed', ack=before['list_sha256'])
        self.scan('list-changed', ack=timed['list_sha256'])
        self.assertTrue(directory.is_dir())
        self.assertEqual(regular.read_bytes(), b'after, longer than before')

    def test_identity_ack_rejects_candidate_recreated_under_same_name(self):
        self.create_roots()
        guard = self.roots[0] / 'guard-moved'
        guard.mkdir()
        before = self.scan('confirmation-required')
        # 消さずに置き場の外へ動かす。番号の使い回しで同じ番号にならないようにする。
        guard.rename(self.base / 'moved-out')
        guard.mkdir()
        after = self.scan('list-changed', ack_identities=before['identity_sha256'])
        self.assertNotEqual(before['candidates'][0]['ino'], after['candidates'][0]['ino'])
        self.assertEqual(before['candidates'][0]['name_bytes_hex'], after['candidates'][0]['name_bytes_hex'])
        self.assertNotEqual(before['identity_sha256'], after['identity_sha256'])
        self.scan('list-changed', ack=before['list_sha256'])
        self.scan('acknowledged', ack_identities=after['identity_sha256'])

    def test_identity_ack_changes_with_cwd_name_kind_count_and_root(self):
        self.create_roots()
        guard = self.roots[0] / 'guard-one'
        guard.mkdir()
        initial = self.scan('confirmation-required')['identity_sha256']
        self.scan('acknowledged', ack_identities=initial)
        other = self.base / 'other-cwd'
        other.mkdir()
        self.scan('list-changed', ack_identities=initial, cwd=other)
        guard.rename(self.roots[0] / 'guard-renamed')
        renamed = self.scan('list-changed', ack_identities=initial)['identity_sha256']
        (self.roots[0] / 'guard-added').mkdir()
        added = self.scan('list-changed', ack_identities=renamed)['identity_sha256']
        (self.roots[0] / 'guard-added').rmdir()
        self.scan('list-changed', ack_identities=added)
        self.scan('acknowledged', ack_identities=renamed)
        alternate = self.base / 'new-xdg'
        alternate.mkdir()
        self.scan('list-changed', ack_identities=renamed, env=dict(self.env, XDG_STATE_HOME=str(alternate)))
        (self.roots[0] / 'guard-renamed').rename(self.base / 'old-guard')
        (self.roots[0] / 'guard-renamed').write_bytes(b'same name, other kind')
        self.scan('list-changed', ack_identities=renamed)
        (self.roots[0] / 'guard-renamed').unlink()
        self.scan('list-changed', ack_identities=renamed)
        self.scan('none')

    def test_both_acknowledgements_are_usage_error(self):
        self.create_roots()
        (self.roots[0] / 'guard-both').mkdir()
        first = self.scan('confirmation-required')
        listed, identity = first['list_sha256'], first['identity_sha256']
        cwd = str(self.cwd)
        cases = [['scan', '--cwd', cwd, '--acknowledged-list', listed, '--acknowledged-identities', identity],
                 ['scan', '--cwd', cwd, '--acknowledged-identities', identity, '--acknowledged-list', listed],
                 ['scan', '--cwd=' + cwd, '--acknowledged-list=' + listed, '--acknowledged-identities=' + identity],
                 ['scan', '--acknowledged-identities', identity, '--cwd', cwd, '--acknowledged-list', listed]]
        for args in cases:
            with self.subTest(args=args):
                rc, result = self.cli(args)
                self.assertEqual(rc, 2)
                self.assertEqual(result['result'], 'usage')
                self.assertEqual([i['code'] for i in result['issues']], ['invalid-arguments'])
                self.assertEqual(result['roots'], [])
                self.assertEqual(result['candidates'], [])
        # 片方だけなら、どちらも今の一覧で続行できる。
        self.scan('acknowledged', ack=listed)
        self.scan('acknowledged', ack_identities=identity)
        self.scan('list-changed', ack_identities=listed)
        self.scan('list-changed', ack=identity)

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
        for option in ('--acknowledged-list', '--acknowledged-identities'):
            cases += [['scan', '--cwd', str(self.cwd), option, value]
                      for value in ('', '0' * 63, '0' * 65, ' ' + '0' * 64, '0x' + '0' * 64, 'ａ' * 64)]
            cases.append(['scan', '--cwd', str(self.cwd), option, '0' * 64, option, '0' * 64])
            cases.append(['scan', '--cwd', str(self.cwd), option])
        for args in cases:
            with self.subTest(args=args):
                rc, result = self.cli(args)
                self.assertEqual(rc, 2)
                self.assertEqual(result['result'], 'usage')
        cp = subprocess.run([sys.executable, '-I', '-B', str(HELPER), 'scan', '--help'],
                            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(cp.returncode, 0)
        self.assertRegex(cp.stdout.decode(), r'[^\x00-\x7f]')
        for option in ('--cwd', '--acknowledged-list', '--acknowledged-identities'):
            self.assertIn(option, cp.stdout.decode())
        with self.assertRaises(json.JSONDecodeError):
            json.loads(cp.stdout)


def path_hex(path):
    return os.fsencode(str(path)).hex().encode()


def mark_bytes(values):
    # 印の形: 1行に1項目、`キー\t値`。パスは元のバイトの小文字16進、番号は十進。
    return b''.join(key.encode() + b'\t' + value + b'\n' for key, value in values.items())


def make_mark_values(guard, top, common, taskdir, tmpdir=None):
    def ident(path, follow=True):
        info = os.stat(str(path), follow_symlinks=follow)
        return str(info.st_dev).encode(), str(info.st_ino).encode()
    values = {'version': b'1'}
    values['guard_dev'], values['guard_ino'] = ident(guard, follow=False)
    for key, path in (('top', top), ('common', common), ('taskdir', taskdir)):
        values[key] = path_hex(path)
        values[key + '_dev'], values[key + '_ino'] = ident(path)
    values['tmpdir'] = b'' if tmpdir is None else path_hex(tmpdir)
    return values


def fake_stat(info, **changes):
    data = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
    data.update(changes)
    return type('ChangedStat', (), data)()


def tree_state(path):
    # 試験が作った記録と印の、名前・番号・種類・中身(atimeは除く)。
    items = {}
    for item in [path, *sorted(path.rglob('*'))] if path.is_dir() else [path]:
        info = item.lstat()
        items[str(item)] = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
                            item.read_bytes() if stat.S_ISREG(info.st_mode) else None)
    return items


class OwnerFixture(PendingFixture):
    """take を使う試験と、印の分類の試験。

    リポジトリと置き場は /tmp・/private/tmp・$TMPDIR の外、実の $HOME/.local/state/dev-workflow/ の下の
    専用のフォルダの中に作る。そこに既に在る記録は開かない・変えない。take の TMPDIR は専用のフォルダに向ける。
    """
    def setUp(self):
        super().setUp()
        real_home = Path(os.environ.get('HOME') or os.path.expanduser('~')).resolve()
        base = real_home / '.local/state/dev-workflow'
        excluded = {Path('/tmp').resolve(), Path(tempfile.gettempdir()).resolve()}
        if os.environ.get('TMPDIR'):
            excluded.add(Path(os.environ['TMPDIR']).resolve())
        for temp in excluded:
            if base == temp or temp in base.parents:
                self.skipTest('実HOMEの置き場が一時フォルダの下にあり、別プロジェクトの記録を作れない')
        # 置き場は、同時に流す implement-agent・implement-guard の selftest も「無ければ作る・空なら消す」で扱う。
        # 在ることを見てから中に作るまでの間に、ほかが空の置き場を消すことがあるので、そのときは作り直してやり直す。
        made_base = False
        for _ in range(5):
            try:
                base.mkdir(parents=True, mode=0o700)
                made_base = True
            except FileExistsError:
                pass
            try:
                owned = tempfile.mkdtemp(prefix='selftest-pending-', dir=str(base))
                break
            except FileNotFoundError:
                continue
        else:
            self.fail(f'{base} の中に専用のフォルダを作れない(5 回とも、作る前に置き場が消えた)')
        if made_base:
            self.addCleanup(self.remove_if_empty, base)
        self.owned = Path(owned).resolve()
        self.addCleanup(self.remove_owned)
        for temp in excluded:
            self.assertNotIn(temp, self.owned.parents)
        self.state = self.owned / 'state'
        self.repos = self.owned / 'repos'
        self.taketmp = self.owned / 'taketmp'
        for path in (self.state / 'dev-workflow', self.repos, self.taketmp):
            path.mkdir(parents=True, mode=0o700)
        self.xdg = self.state
        self.root = self.state / 'dev-workflow'
        self.roots[0] = self.root
        self.env.update(XDG_STATE_HOME=str(self.state), LC_ALL='C', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_PAGER='cat')
        self.take_env = dict(self.env, TMPDIR=str(self.taketmp))

    @staticmethod
    def remove_if_empty(path):
        try:
            path.rmdir()
        except OSError:
            pass

    def remove_owned(self):
        for path in [self.owned, *self.owned.rglob('*')]:
            try:
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(str(self.owned), ignore_errors=True)

    def git(self, cwd, *args, check=True, env=None):
        return support_git.git(*args, env=self.env if env is None else env, check=check, text=False, timeout=60,
                               options=('-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'init.defaultBranch=main',
                                        '-c', 'core.fsmonitor=', '-c', 'core.hooksPath=/dev/null',
                                        '-c', 'protocol.file.allow=always', '-C', str(cwd)))

    def make_repo(self, relative, parent=None):
        path = (parent or self.repos) / relative
        path.mkdir(parents=True)
        self.git(path, 'init', '-q')
        (path / 'seed.txt').write_bytes(b'seed\n')
        self.git(path, 'add', 'seed.txt')
        self.git(path, 'commit', '-qm', 'seed')
        (path / 'task').mkdir()
        (path / 'task/t.md').write_text('# タスク\n\n- [ ] one\n', encoding='utf-8')
        return path.resolve()

    def relative_worktree(self, repo, path):
        # Git 2.48以上なら --relative-paths で作る(この版で入ったと理解している。未確認)。
        # 古いGitでは通常のworktree addの後に、.gitファイルのgitdir:を相対パスへ書き換える。
        made = self.git(repo, 'worktree', 'add', '-q', '--relative-paths', str(path), check=False)
        if made.returncode != 0:
            self.git(repo, 'worktree', 'add', '-q', str(path))
            dotgit = path / '.git'
            target = dotgit.read_bytes()[len(b'gitdir: '):].strip()
            relative = os.path.relpath(os.fsdecode(target), str(path))
            dotgit.write_bytes(b'gitdir: ' + os.fsencode(relative) + b'\n')
        content = (path / '.git').read_bytes()
        self.assertTrue(content.startswith(b'gitdir: ') and not content[len(b'gitdir: '):].startswith(b'/'), content)
        admin = (path / os.fsdecode(content[len(b'gitdir: '):].strip())).resolve()
        self.assertFalse((admin / 'commondir').read_bytes().startswith(b'/'))
        return path.resolve()

    def take(self, repo, task='task/t.md'):
        result = subprocess.run(['bash', str(SKILL / 'scripts/implement-guard.sh'), 'take', '--cwd', str(repo),
                                 '--task-md', task], env=self.take_env, cwd=str(repo), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        fields = dict(line.split('=', 1) for line in result.stdout.decode('utf-8').splitlines())
        state = Path(fields['STATE_DIR'])
        self.assertEqual(state.parent, self.root)
        return fields, state, self.root / ('owner-' + state.name[len('guard-'):])

    def mark_values(self, guard, top, common, taskdir, tmpdir=None):
        values = make_mark_values(guard, top, common, taskdir, tmpdir)
        self.assertEqual(tuple(values), MARK_KEYS)
        return values

    def write_mark(self, guard, data):
        mark = guard.parent / ('owner-' + guard.name[len('guard-'):])
        if mark.is_symlink() or mark.exists():
            if mark.is_dir() and not mark.is_symlink():
                mark.rmdir()
            else:
                mark.unlink()
        mark.write_bytes(data)
        mark.chmod(0o600)
        return mark

    def record(self, name, top, common=None, taskdir=None, tmpdir=None, **override):
        guard = self.root / name
        if not guard.exists():
            guard.mkdir(mode=0o700)
        values = self.mark_values(guard, top, common or top / '.git', taskdir or top / 'task', tmpdir)
        values.update(override)
        return guard, self.write_mark(guard, mark_bytes(values))

    def only(self, result, name):
        found = [c for c in result['candidates'] if bytes.fromhex(c['name_bytes_hex']) == os.fsencode(name)]
        self.assertEqual(len(found), 1, result['candidates'])
        return found[0]

    def reason_of(self, cwd, name, expected_result, env=None, **kwargs):
        result = self.scan(expected_result, cwd=cwd, env=env, **kwargs)
        return self.only(result, name), result


class PendingOwnerTests(OwnerFixture):
    def test_mark_kinds_size_and_format_stop_with_reason(self):
        a = self.make_repo('a')
        b = self.make_repo('b')
        guard, mark = self.record('guard-m1', a)
        good = mark.read_bytes()
        candidate, result = self.reason_of(b, 'guard-m1', 'other-projects-only')
        self.assertEqual((candidate['relation'], candidate['reason']), ('other-project', None))
        self.assertEqual(candidate['owner'], {'top': str(a), 'top_bytes_hex': path_hex(a).decode(),
                                              'common': str(a / '.git'), 'common_bytes_hex': path_hex(a / '.git').decode(),
                                              'taskdir': str(a / 'task'), 'taskdir_bytes_hex': path_hex(a / 'task').decode()})
        self.assertIn('別プロジェクトの保護記録: 1 件'.encode('utf-8'), self.last_stderr)
        for secret in (os.fsencode(str(a)), b'guard-m1', b'owner-m1'):
            self.assertNotIn(secret, self.last_stderr)
        mark.unlink()
        candidate, result = self.reason_of(b, 'guard-m1', 'confirmation-required')
        self.assertEqual((candidate['relation'], candidate['reason'], candidate['owner']),
                         ('confirmation-required', 'no-mark', None))
        values = dict(line.split(b'\t', 1) for line in good.splitlines())
        values = {key.decode(): value for key, value in values.items()}
        broken = {
            'missing-key': mark_bytes({k: v for k, v in values.items() if k != 'tmpdir'}),
            'duplicate-key': good + b'version\t1\n',
            'unknown-key': good + b'extra\t1\n',
            'version': mark_bytes(dict(values, version=b'2')),
            'non-decimal': mark_bytes(dict(values, guard_dev=values['guard_dev'] + b'a')),
            'negative': mark_bytes(dict(values, top_ino=b'-' + values['top_ino'])),
            'upper-hex': mark_bytes(dict(values, top=values['top'].upper())),
            'odd-hex': mark_bytes(dict(values, top=values['top'] + b'0')),
            'relative-path': mark_bytes(dict(values, top=path_hex('relative/path'))),
            'empty-top': mark_bytes(dict(values, top=b'')),
            'nul-path': mark_bytes(dict(values, top=b'2f00')),
            'relative-tmpdir': mark_bytes(dict(values, tmpdir=path_hex('tmp'))),
            'crlf': good.replace(b'\n', b'\r\n'),
            'no-final-newline': good[:-1],
            'empty': b'',
            'too-large': good + b'#' * (64 * 1024),
        }
        for label, data in broken.items():
            with self.subTest(mark=label):
                self.write_mark(guard, data)
                candidate, result = self.reason_of(b, 'guard-m1', 'confirmation-required')
                self.assertEqual((candidate['reason'], candidate['owner']), ('mark-invalid', None))
        # 通常ファイルでない印: リンク・フォルダ・FIFO(開く所で止まらない)
        valid = self.owned / 'valid-mark'
        valid.write_bytes(good)
        mark = self.write_mark(guard, good)
        mark.unlink()
        mark.symlink_to(valid)
        candidate, result = self.reason_of(b, 'guard-m1', 'confirmation-required')
        self.assertEqual(candidate['reason'], 'mark-invalid')
        mark.unlink()
        mark.mkdir()
        candidate, result = self.reason_of(b, 'guard-m1', 'confirmation-required')
        self.assertEqual(candidate['reason'], 'mark-invalid')
        mark.rmdir()
        os.mkfifo(str(mark), 0o600)
        started = time.monotonic()
        candidate, result = self.reason_of(b, 'guard-m1', 'confirmation-required')
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(candidate['reason'], 'mark-invalid')
        mark.unlink()
        # 対照: 正しい印へ戻すと別プロジェクトの記録に戻る
        self.write_mark(guard, good)
        self.reason_of(b, 'guard-m1', 'other-projects-only')

    def test_numbers_of_record_top_common_and_taskdir_must_match(self):
        b = self.make_repo('b')
        a1, a2, a3 = (self.make_repo(name) for name in ('a1', 'a2', 'a3'))
        guard, mark = self.record('guard-n0', a1)
        self.reason_of(b, 'guard-n0', 'other-projects-only')
        # 記録を名前を変えて置き直す → guard-mismatch
        guard.rename(self.owned / 'old-guard')
        guard.mkdir(mode=0o700)
        candidate, result = self.reason_of(b, 'guard-n0', 'confirmation-required')
        self.assertEqual(candidate['reason'], 'guard-mismatch')
        self.assertEqual(candidate['owner']['top'], str(a1))
        # 最上位を名前を変えて置き直す → owner-path-mismatch
        self.record('guard-n1', a1)
        a1.rename(self.repos / 'a1-moved')
        self.make_repo('a1')
        candidate, result = self.reason_of(b, 'guard-n1', 'confirmation-required')
        self.assertEqual(candidate['reason'], 'owner-path-mismatch')
        # 共通の置き場を置き直す
        self.record('guard-n2', a2)
        (a2 / '.git').rename(a2 / 'old-git')
        self.git(a2, 'init', '-q')
        candidate, result = self.reason_of(b, 'guard-n2', 'confirmation-required')
        self.assertEqual(candidate['reason'], 'owner-path-mismatch')
        # タスク MD の在るフォルダを置き直す
        self.record('guard-n3', a3)
        (a3 / 'task').rename(a3 / 'old-task')
        (a3 / 'task').mkdir()
        candidate, result = self.reason_of(b, 'guard-n3', 'confirmation-required')
        self.assertEqual(candidate['reason'], 'owner-path-mismatch')
        # 印に書いたパスが今は無い
        gone = self.repos / 'gone'
        gone.mkdir()
        guard4, mark4 = self.record('guard-n4', gone, common=a3 / '.git', taskdir=a3 / 'task')
        gone.rmdir()
        candidate, result = self.reason_of(b, 'guard-n4', 'confirmation-required')
        self.assertEqual(candidate['reason'], 'owner-path-mismatch')

    def test_take_mark_lets_other_projects_continue(self):
        # 完了条件2: A で take、B と B の相対worktree W から scan すると、止めずに別プロジェクトと報告する。
        a = self.make_repo('A')
        b = self.make_repo('B')
        w = self.relative_worktree(b, self.repos / 'W')
        fields, state, mark = self.take(a)
        self.assertEqual(fields['OWNER_MARK'], 'yes')
        self.assertTrue(stat.S_ISREG(mark.lstat().st_mode))
        self.assertEqual(stat.S_IMODE(mark.lstat().st_mode), 0o600)
        # タスク MD を、別のファイルを作って名前で上書きし、続けて保留_ の名前に変える。
        replacement = a / 'task/t.md.new'
        replacement.write_text('# タスク\n\n- [x] one\n', encoding='utf-8')
        os.replace(str(replacement), str(a / 'task/t.md'))
        os.rename(str(a / 'task/t.md'), str(a / 'task/保留_t.md'))
        before = (tree_state(state), tree_state(mark))
        for cwd in (b, w, b / 'task'):
            with self.subTest(cwd=str(cwd)):
                candidate, result = self.reason_of(cwd, state.name, 'other-projects-only')
                self.assertEqual((candidate['relation'], candidate['reason']), ('other-project', None))
                self.assertEqual(candidate['owner']['top'], str(a))
                self.assertIn('別プロジェクトの保護記録: 1 件'.encode('utf-8'), self.last_stderr)
                for secret in (os.fsencode(str(a)), os.fsencode(state.name), os.fsencode(str(self.owned))):
                    self.assertNotIn(secret, self.last_stderr)
        self.assertEqual((tree_state(state), tree_state(mark)), before)

    def test_take_mark_still_stops_overlapping_or_unknown_work(self):
        # 完了条件3: 同じ記録でも、重なる作業・Gitの置き場を決められない作業・印の異常では止まる。
        outer = self.make_repo('outer')
        a = self.make_repo('A', parent=outer)
        a_wt = self.repos / 'A-wt'
        self.git(a, 'worktree', 'add', '-q', str(a_wt))
        b = self.make_repo('B')
        fields, state, mark = self.take(a)
        self.assertEqual(fields['OWNER_MARK'], 'yes')
        good = mark.read_bytes()
        name = state.name
        self.reason_of(b, name, 'other-projects-only')
        inner = self.make_repo('inner', parent=a)
        tmp_work = Path(tempfile.mkdtemp(prefix='pending-tmp-work-', dir='/tmp')).resolve()
        self.addCleanup(lambda: shutil.rmtree(str(tmp_work), ignore_errors=True))
        tmp_repo = self.make_repo('repo', parent=tmp_work)
        for cwd in (a, a / 'task', a_wt, outer, inner, tmp_repo):
            with self.subTest(cwd=str(cwd)):
                candidate, result = self.reason_of(cwd, name, 'confirmation-required')
                self.assertEqual(candidate['reason'], 'overlap')
        for variable, value in (('GIT_DIR', str(b / '.git')), ('GIT_WORK_TREE', str(b)),
                                ('GIT_COMMON_DIR', str(b / '.git'))):
            with self.subTest(variable=variable):
                candidate, result = self.reason_of(b, name, 'confirmation-required', env=dict(self.env, **{variable: value}))
                self.assertEqual(candidate['reason'], 'git-unavailable')
        head = (b / '.git/HEAD').read_bytes()
        (b / '.git/HEAD').write_bytes(b'not a head\n')
        candidate, result = self.reason_of(b, name, 'confirmation-required')
        self.assertEqual(candidate['reason'], 'git-unavailable')
        (b / '.git/HEAD').write_bytes(head)
        # 印を消した・壊した・リンクやFIFOに替えた
        mark.unlink()
        self.assertEqual(self.reason_of(b, name, 'confirmation-required')[0]['reason'], 'no-mark')
        self.write_mark(state, b'broken\n')
        self.assertEqual(self.reason_of(b, name, 'confirmation-required')[0]['reason'], 'mark-invalid')
        valid = self.owned / 'valid-mark'
        valid.write_bytes(good)
        mark.unlink()
        mark.symlink_to(valid)
        self.assertEqual(self.reason_of(b, name, 'confirmation-required')[0]['reason'], 'mark-invalid')
        mark.unlink()
        os.mkfifo(str(mark), 0o600)
        started = time.monotonic()
        self.assertEqual(self.reason_of(b, name, 'confirmation-required')[0]['reason'], 'mark-invalid')
        self.assertLess(time.monotonic() - started, 5)
        mark.unlink()
        self.write_mark(state, good)
        self.reason_of(b, name, 'other-projects-only')
        # A を別の名前に変えて、元のパスに新しいリポジトリを作る
        a.rename(outer / 'A-moved')
        self.make_repo('A', parent=outer)
        self.assertEqual(self.reason_of(b, name, 'confirmation-required')[0]['reason'], 'owner-path-mismatch')

    def test_take_in_outside_worktree_overlaps_through_common_only(self):
        # A の外に置いた worktree W_A で take した記録は、印の共通の置き場(A/.git)だけで A と重なる。
        # 最上位・タスク MD の在るフォルダ・take の TMPDIR は、A 本体とも A の別の worktree とも重ならない。
        a = self.make_repo('A')
        b = self.make_repo('B')
        wa = self.repos / 'W-A'
        self.git(a, 'worktree', 'add', '-q', str(wa))
        w2 = self.repos / 'W-2'
        self.git(a, 'worktree', 'add', '-q', str(w2))
        wa, w2 = wa.resolve(), w2.resolve()
        (wa / 'task').mkdir()
        (wa / 'task/t.md').write_text('# タスク\n\n- [ ] one\n', encoding='utf-8')
        fields, state, mark = self.take(wa)
        self.assertEqual(fields['OWNER_MARK'], 'yes')
        for path in (wa, wa / 'task', self.taketmp):
            self.assertNotIn(a, path.parents)
        candidate, result = self.reason_of(b, state.name, 'other-projects-only')
        self.assertEqual((candidate['owner']['top'], candidate['owner']['common']), (str(wa), str(a / '.git')))
        for cwd in (a, a / 'task', w2):
            with self.subTest(cwd=str(cwd)):
                candidate, result = self.reason_of(cwd, state.name, 'confirmation-required')
                self.assertEqual(candidate['reason'], 'overlap')

    def test_current_git_place_is_decided_without_git(self):
        a = self.make_repo('a')
        b = self.make_repo('b')
        guard, mark = self.record('guard-g', a)
        name = 'guard-g'
        # 対照: 下のフォルダ・相対のgitdir:とcommondirのworktree・空の環境変数は止めない
        w = self.relative_worktree(b, self.repos / 'w')
        for cwd in (b, b / 'task', w):
            self.assertIsNone(self.reason_of(cwd, name, 'other-projects-only')[0]['reason'])
        self.reason_of(b, name, 'other-projects-only', env=dict(self.env, GIT_DIR=''))
        detached = self.git(b, 'rev-parse', 'HEAD').stdout.strip()
        head = (b / '.git/HEAD').read_bytes()
        (b / '.git/HEAD').write_bytes(detached + b'\n')
        self.reason_of(b, name, 'other-projects-only')
        # 決められない: 壊れたHEAD・HEADが無い・HEADがリンク
        for label, change in (('garbage', lambda p: p.write_bytes(b'garbage\n')),
                              ('missing', lambda p: p.unlink()),
                              ('symlink', lambda p: (p.unlink(), p.symlink_to('ORIG_HEAD')))):
            with self.subTest(head=label):
                change(b / '.git/HEAD')
                self.assertEqual(self.reason_of(b, name, 'confirmation-required')[0]['reason'], 'git-unavailable')
                path = b / '.git/HEAD'
                if path.is_symlink() or path.exists():
                    path.unlink()
                path.write_bytes(head)
        self.reason_of(b, name, 'other-projects-only')
        # .git がリンク・中身の壊れた .git ファイル・指す先が無い .git ファイル・Git でない
        linked = self.repos / 'linked'
        linked.mkdir()
        (linked / '.git').symlink_to(b / '.git')
        garbage = self.repos / 'garbage'
        garbage.mkdir()
        (garbage / '.git').write_bytes(b'not a gitdir line\n')
        dangling = self.repos / 'dangling'
        dangling.mkdir()
        (dangling / '.git').write_bytes(b'gitdir: ../missing-admin\n')
        plain = self.repos / 'plain'
        plain.mkdir()
        no_objects = self.repos / 'no-objects'
        no_objects.mkdir()
        admin = self.repos / 'no-objects-admin'
        admin.mkdir()
        (admin / 'HEAD').write_bytes(b'ref: refs/heads/main\n')
        (admin / 'refs').mkdir()
        (no_objects / '.git').write_bytes(b'gitdir: ../no-objects-admin\n')
        for cwd in (linked, garbage, dangling, plain, no_objects):
            with self.subTest(cwd=cwd.name):
                self.assertEqual(self.reason_of(cwd, name, 'confirmation-required')[0]['reason'], 'git-unavailable')

    def test_overlap_alone_stops_even_when_common_differs(self):
        # 含む関係だけが当たる組(共通の置き場は違う)
        outer = self.make_repo('outer')
        a = self.make_repo('A', parent=outer)
        inner = self.make_repo('inner', parent=a)
        b = self.make_repo('b')
        unrelated = self.make_repo('unrelated')
        self.record('guard-a', a)
        for cwd in (outer, inner):
            with self.subTest(cwd=cwd.name):
                self.assertEqual(self.reason_of(cwd, 'guard-a', 'confirmation-required')[0]['reason'], 'overlap')
        self.assertIsNone(self.reason_of(unrelated, 'guard-a', 'other-projects-only')[0]['reason'])
        # --cwd が最上位の下で、記録の最上位が今回の最上位の中かつ --cwd の外
        t = self.make_repo('t')
        (t / 'sub').mkdir()
        nested = self.make_repo('x', parent=t)
        self.record('guard-x', nested)
        self.assertEqual(self.reason_of(t / 'sub', 'guard-x', 'confirmation-required')[0]['reason'], 'overlap')
        # タスク MD の在るフォルダが今回の作業の中
        (b / 'docs').mkdir()
        self.record('guard-t', unrelated, taskdir=b / 'docs')
        self.assertEqual(self.reason_of(b, 'guard-t', 'confirmation-required')[0]['reason'], 'overlap')
        # take のときの TMPDIR が今回の作業を含む
        self.record('guard-d', unrelated, tmpdir=self.repos)
        self.assertEqual(self.reason_of(b, 'guard-d', 'confirmation-required')[0]['reason'], 'overlap')
        # /tmp の下の作業
        tmp_work = Path(tempfile.mkdtemp(prefix='pending-tmp-work-', dir='/tmp')).resolve()
        self.addCleanup(lambda: shutil.rmtree(str(tmp_work), ignore_errors=True))
        tmp_repo = self.make_repo('repo', parent=tmp_work)
        self.assertEqual(self.reason_of(tmp_repo, 'guard-a', 'confirmation-required')[0]['reason'], 'overlap')

    def test_submodule_records_stop_both_directions(self):
        source = self.make_repo('sub-source')
        parent = self.make_repo('P')
        self.git(parent, 'submodule', 'add', '-q', str(source), 'S')
        self.git(parent, 'commit', '-qm', 'add submodule')
        sub = (parent / 'S').resolve()
        self.assertTrue((sub / '.git').is_file())
        unrelated = self.make_repo('unrelated')
        self.record('guard-in-sub', sub, common=parent / '.git/modules/S', taskdir=sub)
        self.record('guard-parent', parent)
        self.assertEqual(self.reason_of(parent, 'guard-in-sub', 'confirmation-required')[0]['reason'], 'overlap')
        self.assertEqual(self.reason_of(sub, 'guard-parent', 'confirmation-required')[0]['reason'], 'overlap')
        result = self.scan('other-projects-only', cwd=unrelated)
        self.assertEqual({c['reason'] for c in result['candidates']}, {None})

    def test_results_digests_and_identity_rules(self):
        a = self.make_repo('a')
        b = self.make_repo('b')
        self.record('guard-other', a)
        other_only = self.scan('other-projects-only', cwd=b)
        # 「確認が要る」候補が0件なら、確認値は空の一覧の値と比べる(置き場の記録は同じでなければならない)
        self.scan('acknowledged', cwd=b, ack=other_only['list_sha256'])
        self.scan('acknowledged', cwd=b, ack_identities=other_only['identity_sha256'])
        self.scan('list-changed', cwd=b, ack='0' * 64)
        # 確認が要る候補を足しても、別プロジェクトの記録は確認値に入らない
        (self.root / 'guard-plain').mkdir()
        mixed = self.scan('confirmation-required', cwd=b)
        self.assertEqual([c['relation'] for c in mixed['candidates']], ['other-project', 'confirmation-required'])
        (self.root / 'owner-other').rename(self.owned / 'owner-other-away')
        (self.root / 'guard-other').rename(self.owned / 'guard-other-away')
        alone = self.scan('confirmation-required', cwd=b)
        self.assertEqual((mixed['list_sha256'], mixed['identity_sha256']),
                         (alone['list_sha256'], alone['identity_sha256']))
        (self.owned / 'guard-other-away').rename(self.root / 'guard-other')
        (self.owned / 'owner-other-away').rename(self.root / 'owner-other')
        # 印のない候補を identity で控えた後に、重なる印が置かれると list-changed
        noted = self.scan('confirmation-required', cwd=b)
        self.scan('acknowledged', cwd=b, ack_identities=noted['identity_sha256'])
        self.assertNotIn(OVERLAP_LINE.encode('utf-8'), self.last_stderr)
        self.record('guard-plain', b)
        overlapped = self.scan('list-changed', cwd=b, ack_identities=noted['identity_sha256'])
        self.assertEqual(self.only(overlapped, 'guard-plain')['reason'], 'overlap')
        self.assertIn(OVERLAP_LINE.encode('utf-8'), self.last_stderr)
        # 重なりがあれば、値が一致しても identity の確認値では続行しない。list_sha256 なら続行できる
        self.scan('list-changed', cwd=b, ack_identities=overlapped['identity_sha256'])
        self.assertIn(OVERLAP_LINE.encode('utf-8'), self.last_stderr)
        self.scan('acknowledged', cwd=b, ack=overlapped['list_sha256'])
        self.assertNotIn(OVERLAP_LINE.encode('utf-8'), self.last_stderr)
        # 重なり以外の理由どうしの変化(no-mark から owner-path-mismatch)も list-changed
        (self.root / 'owner-plain').unlink()
        noted = self.scan('confirmation-required', cwd=b)
        self.assertEqual(self.only(noted, 'guard-plain')['reason'], 'no-mark')
        gone = self.repos / 'gone'
        gone.mkdir()
        self.record('guard-plain', gone, common=a / '.git', taskdir=a / 'task')
        gone.rmdir()
        changed = self.scan('list-changed', cwd=b, ack_identities=noted['identity_sha256'])
        self.assertEqual(self.only(changed, 'guard-plain')['reason'], 'owner-path-mismatch')
        self.assertNotIn(OVERLAP_LINE.encode('utf-8'), self.last_stderr)
        self.assertEqual(changed['list_sha256'], noted['list_sha256'])
        self.scan('acknowledged', cwd=b, ack=noted['list_sha256'])

    def test_unreadable_ancestor_is_overlap_unknown(self):
        if os.geteuid() == 0:
            self.skipTest('root は読めないフォルダを作れない')
        b = self.make_repo('b')
        locked = self.repos / 'locked'
        locked.mkdir()
        a = self.make_repo('a', parent=locked)
        self.record('guard-u', a)
        locked.chmod(0o111)
        self.addCleanup(locked.chmod, 0o700)
        candidate, result = self.reason_of(b, 'guard-u', 'confirmation-required')
        self.assertEqual(candidate['reason'], 'overlap-unknown')
        self.scan('list-changed', cwd=b, ack_identities=result['identity_sha256'])
        self.assertIn(OVERLAP_LINE.encode('utf-8'), self.last_stderr)
        self.scan('acknowledged', cwd=b, ack=result['list_sha256'])


class PendingBoundaryTests(PendingFixture):
    def setUp(self):
        super().setUp()
        self.module = load_script(HELPER, 'pending_contract_helper')
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
                                  ('supports_follow_symlinks', os.stat), ('supports_dir_fd', os.open)]:
            with self.subTest(support=support), mock.patch.object(os, support, getattr(os, support) - {function}):
                result = self.run_unit('scan-unavailable', 'missing-feature')
                self.assertEqual(result['roots'], [])
        for attr in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_CLOEXEC', 'O_NONBLOCK', 'open', 'close', 'fstat', 'read',
                     'fsencode', 'fsdecode'):
            with self.subTest(attr=attr), mock.patch.object(os, attr, None):
                self.run_unit('scan-unavailable', 'missing-feature')
        for version in ((3, 7), (3, 9, 18)):
            with self.subTest(version=version), mock.patch.object(self.module.sys, 'version_info', version):
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

    def run_any(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            rc, result = self.module.run(['scan', '--cwd', str(self.cwd)])
        self.check_schema(rc, result)
        return result

    def test_mark_owner_and_changes_while_reading(self):
        guard = self.roots[0] / 'guard-b'
        guard.mkdir()
        top = self.base / 'other'
        for sub in ('.git', 'task'):
            (top / sub).mkdir(parents=True)
        mark = self.roots[0] / 'owner-b'
        mark.write_bytes(mark_bytes(make_mark_values(guard, top, top / '.git', top / 'task')))
        mark.chmod(0o600)
        control = self.run_any()['candidates'][0]
        self.assertIn(control['reason'], OWNER_READ_REASONS)
        original = os.stat
        for mode, expected in (('uid', 'mark-invalid'), ('appear', 'no-mark'),
                               ('vanish', 'no-mark'), ('change', 'mark-invalid')):
            calls = [0]
            def change(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if os.fsencode(path) == b'owner-b':
                    self.assertFalse(kwargs.get('follow_symlinks', True))
                    self.assertIsNotNone(kwargs.get('dir_fd'))
                    calls[0] += 1
                    if mode == 'uid':
                        return fake_stat(info, st_uid=info.st_uid + 1)
                    if (mode, calls[0]) in (('appear', 1), ('vanish', 2)):
                        raise FileNotFoundError('fixture mark')
                    if (mode, calls[0]) == ('change', 2):
                        return fake_stat(info, st_ino=info.st_ino + 1)
                return info
            with self.subTest(mode=mode), self.patched_os('stat', change):
                candidate = self.run_any()['candidates'][0]
                self.assertEqual(candidate['reason'], expected)
                self.assertEqual(candidate['relation'], 'confirmation-required')
                self.assertGreaterEqual(calls[0], 1 if mode == 'appear' else 2)

    def run_args(self, extra):
        with mock.patch.dict(os.environ, self.env, clear=True):
            rc, result = self.module.run(['scan', '--cwd', str(self.cwd)] + extra)
        self.check_schema(rc, result)
        return result

    def test_unsupported_features_in_mark_and_git_readers_stop_with_11(self):
        # 印・.git・`..`の読み取りで、必要な機能の実行時の未対応(NotImplementedError・ENOSYS・ENOTSUP・EOPNOTSUPP)を
        # 通常の読み取りの失敗にせず11にする。確認値を渡しても解除できない。
        import errno
        guard = self.roots[0] / 'guard-f'
        guard.mkdir()
        top = self.base / 'other'
        for sub in ('.git', 'task'):
            (top / sub).mkdir(parents=True)
        mark = self.roots[0] / 'owner-f'
        mark.write_bytes(mark_bytes(make_mark_values(guard, top, top / '.git', top / 'task')))
        mark.chmod(0o600)
        normal = self.run_any()
        self.assertTrue(normal['complete'])
        self.assertIsNotNone(normal['candidates'][0]['owner'])
        acks = [[], ['--acknowledged-list', normal['list_sha256']],
                ['--acknowledged-identities', normal['identity_sha256']]]
        self.assertEqual(self.run_args(acks[1])['result'], 'acknowledged')
        errors = [NotImplementedError('fixture'), OSError(errno.ENOSYS, 'fixture'),
                  OSError(getattr(errno, 'ENOTSUP', errno.ENOSYS), 'fixture'),
                  OSError(getattr(errno, 'EOPNOTSUPP', errno.ENOSYS), 'fixture')]
        original_stat, original_open = os.stat, os.open
        for error in errors:
            def fail_read(*args, **kwargs):
                raise error
            def fail_stat(path, *args, **kwargs):
                if os.fsencode(path) in (b'owner-f', b'.git') and fail_stat.target == os.fsencode(path):
                    raise error
                return original_stat(path, *args, **kwargs)
            def fail_open(path, *args, **kwargs):
                if os.fsencode(path) == b'..':
                    raise error
                return original_open(path, *args, **kwargs)
            cases = [('read', lambda: mock.patch.object(os, 'read', fail_read)),
                     ('stat-mark', lambda: self.patched_os('stat', fail_stat)),
                     ('stat-dotgit', lambda: self.patched_os('stat', fail_stat)),
                     ('open-parent', lambda: self.patched_os('open', fail_open))]
            for label, patch in cases:
                fail_stat.target = {'stat-mark': b'owner-f', 'stat-dotgit': b'.git'}.get(label)
                for extra in acks:
                    with self.subTest(error=repr(error), reader=label, ack=extra[:1]), patch():
                        result = self.run_args(extra)
                        self.assertEqual(result['result'], 'scan-unavailable')
                        self.assertIn('missing-feature', [i['code'] for i in result['issues']])

    def test_unsupported_features_in_each_new_opener_stop_with_11(self):
        # 段階2で足した開く処理のそれぞれで、必要な機能の実行時の未対応を11へ伝える(1か所ずつ、ほかでは落ちない形で)。
        #   mark-open: 印の名前(owner-…)を開くos.open(read_small)
        #   top-open: 印のtopを開くos.open(relate)
        #   tmp-open: /tmpを開くos.open(temp_chain)
        #   parent-open: `..`を開くos.open(chain)。cwdをGitの最上位にして、resolve_scopeが上へたどらないようにする
        import errno
        support_git.git('-c', 'init.defaultBranch=main', 'init', '-q', str(self.cwd),
                        env=dict(self.env, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull),
                        text=False, timeout=60)
        self.assertTrue((self.cwd / '.git/HEAD').is_file())
        guard = self.roots[0] / 'guard-g'
        guard.mkdir()
        top = self.base / 'other'
        for sub in ('.git', 'task'):
            (top / sub).mkdir(parents=True)
        mark = self.roots[0] / 'owner-g'
        mark.write_bytes(mark_bytes(make_mark_values(guard, top, top / '.git', top / 'task')))
        mark.chmod(0o600)
        normal = self.run_any()
        self.assertTrue(normal['complete'])
        self.assertNotIn(normal['candidates'][0]['reason'], ('git-unavailable', 'mark-invalid', 'owner-path-mismatch'))
        acks = [[], ['--acknowledged-list', normal['list_sha256']],
                ['--acknowledged-identities', normal['identity_sha256']]]
        targets = {'mark-open': b'owner-g', 'top-open': os.fsencode(str(top)), 'tmp-open': b'/tmp', 'parent-open': b'..'}
        original_open = os.open
        for error in (NotImplementedError('fixture'), OSError(errno.ENOSYS, 'fixture'),
                      OSError(getattr(errno, 'ENOTSUP', errno.ENOSYS), 'fixture'),
                      OSError(getattr(errno, 'EOPNOTSUPP', errno.ENOSYS), 'fixture')):
            for label, target in targets.items():
                calls = [0]
                def fail_open(path, *args, **kwargs):
                    if os.fsencode(path) == target:
                        calls[0] += 1
                        raise error
                    return original_open(path, *args, **kwargs)
                for extra in acks:
                    with self.subTest(error=repr(error), opener=label, ack=extra[:1]), self.patched_os('open', fail_open):
                        result = self.run_args(extra)
                        self.assertEqual(result['result'], 'scan-unavailable')
                        self.assertIn('missing-feature', [i['code'] for i in result['issues']])
                self.assertGreater(calls[0], 0, label)

    def test_candidate_owner_change_during_scan_stops(self):
        # 候補の取り直しは番号・種類・持ち主を比べる。持ち主だけが途中で変わっても11にする。
        guard = self.roots[0] / 'guard-owner'
        guard.mkdir()
        original = os.stat
        calls = [0]
        def change(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if os.fsencode(path).split(b'/')[-1] == b'guard-owner':
                calls[0] += 1
                if calls[0] >= 2:
                    return fake_stat(info, st_uid=info.st_uid + 1)
            return info
        with self.patched_os('stat', change):
            self.run_unit('scan-unavailable', 'changed-during-scan')
        self.assertGreaterEqual(calls[0], 2)

    def test_final_recheck_compares_numbers_kind_and_owner_only(self):
        guard = self.roots[0] / 'guard-t'
        guard.mkdir()
        original = os.stat
        calls = [0]
        def change(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if os.fsencode(path).split(b'/')[-1] == b'guard-t':
                calls[0] += 1
                if calls[0] >= 2:
                    return fake_stat(info, st_mtime_ns=info.st_mtime_ns + 1, st_ctime_ns=info.st_ctime_ns + 1,
                                     st_size=info.st_size + 1, st_mode=info.st_mode ^ 0o007)
            return info
        with self.patched_os('stat', change):
            self.run_unit('confirmation-required')
        self.assertGreaterEqual(calls[0], 2)
        original_fstat = os.fstat
        wanted = (self.roots[0].stat().st_dev, self.roots[0].stat().st_ino)
        held = [0]
        def touch_root(fd):
            info = original_fstat(fd)
            if (info.st_dev, info.st_ino) == wanted:
                held[0] += 1
                if held[0] >= 2:
                    return fake_stat(info, st_mtime_ns=info.st_mtime_ns + 1, st_ctime_ns=info.st_ctime_ns + 1,
                                     st_mode=info.st_mode ^ 0o007, st_size=info.st_size + 1)
            return info
        with self.patched_os('fstat', touch_root):
            self.run_unit('confirmation-required')
        self.assertGreaterEqual(held[0], 2)

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
        result = fixed_result()
        roots, candidate = result['roots'], result['candidates'][0]
        payload = expected_payload(result)
        self.assertEqual(set(payload), {'schema_version', 'cwd_bytes_hex', 'roots', 'candidates'})
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(len(payload['candidates'][0]), 11)
        encoded = canonical(payload)
        self.assertNotEqual(encoded[-1:], b'\n')
        expected = hashlib.sha256(encoded).hexdigest()
        # list_sha256の入力の形(schema_version 1と候補の11項目)を変えていないことを、固定値でも確かめる。
        self.assertEqual(expected, 'c6846d4850a3703d5365dc73c53c70bb351d19a06aa118773f3816c1a929a990')
        self.assertEqual(self.module.list_digest(result), expected)
        identity_payload = expected_identity_payload(result)
        self.assertEqual(set(identity_payload), {'schema_version', 'digest', 'cwd_bytes_hex', 'roots', 'candidates'})
        self.assertEqual((identity_payload['schema_version'], identity_payload['digest']), (1, 'identity'))
        self.assertEqual(set(identity_payload['candidates'][0]), set(IDENTITY_CANDIDATE_KEYS))
        self.assertEqual(set(identity_payload['roots'][0]['identity']), set(IDENTITY_ROOT_KEYS))
        identity = hashlib.sha256(canonical(identity_payload)).hexdigest()
        self.assertNotEqual(identity, expected)
        self.assertEqual(self.module.identity_digest(result), identity)
        result.update(cwd='display changed', result='acknowledged', complete=False,
                      issues=[{'message': '表示だけ'}], list_sha256='unused', identity_sha256='unused')
        roots[0]['input_path'] = '表示だけ'
        roots[0]['resolved_path'] = '表示だけ'
        candidate['path'] = '表示だけ'
        self.assertEqual(self.module.list_digest(result), expected)
        self.assertEqual(self.module.identity_digest(result), identity)
        # 更新時刻・大きさ・権限はlist_sha256だけを変え、identity_sha256を変えない。
        for key, value in (('mtime_ns', 101), ('ctime_ns', 201), ('size', 4097), ('mode', 16832 | 0o077)):
            with self.subTest(key=key):
                changed = fixed_result()
                changed['candidates'][0][key] = value
                self.assertNotEqual(self.module.list_digest(changed), expected)
                self.assertEqual(self.module.identity_digest(changed), identity)
        changed = fixed_result()
        for root in changed['roots'][:2]:
            root['identity']['mode'] = 16832 | 0o077
        self.assertNotEqual(self.module.list_digest(changed), expected)
        self.assertEqual(self.module.identity_digest(changed), identity)
        # 実体・名前・置き場・cwd・持ち主の違いは、どちらの確認値も変える。
        for change in (lambda r: r['candidates'][0].update(ino=13), lambda r: r['candidates'][0].update(dev=8),
                       lambda r: r['candidates'][0].update(uid=43), lambda r: r['candidates'][0].update(kind='file'),
                       lambda r: r['candidates'][0].update(name_bytes_hex='67756172642d79'),
                       lambda r: r['candidates'][0].update(path_bytes_hex='2f73746174652f67756172642d79'),
                       lambda r: r['roots'][0]['identity'].update(ino=14),
                       lambda r: r['roots'][0]['identity'].update(uid=43),
                       lambda r: r.update(cwd_bytes_hex='2f637765'), lambda r: r.update(candidates=[])):
            with self.subTest(change=change):
                changed = fixed_result()
                change(changed)
                self.assertNotEqual(self.module.list_digest(changed), expected)
                self.assertNotEqual(self.module.identity_digest(changed), identity)
        roots[1]['input_path_bytes_hex'] = '2f616c69617332'
        self.assertNotEqual(self.module.list_digest(result), expected)
        self.assertNotEqual(self.module.identity_digest(result), identity)

    def test_digests_ignore_candidate_keys_added_later(self):
        # 候補のrelation・owner・未知の項目は、list_sha256とidentity_sha256の入力へ混ざらない。
        result = fixed_result()
        expected = hashlib.sha256(canonical(expected_payload(result))).hexdigest()
        identity = hashlib.sha256(canonical(expected_identity_payload(result))).hexdigest()
        self.assertEqual(self.module.list_digest(result), expected)
        self.assertEqual(self.module.identity_digest(result), identity)
        result['candidates'][0].update(owner={'top': '/a', 'top_bytes_hex': '2f61'}, unknown_later=1)
        self.assertEqual(self.module.list_digest(result), expected)
        self.assertEqual(self.module.identity_digest(result), identity)
        # reasonはidentity_sha256の入力にだけ入る(段階2)。list_sha256の入力の形は変えない。
        result['candidates'][0]['reason'] = 'overlap'
        self.assertEqual(self.module.list_digest(result), expected)
        self.assertNotEqual(self.module.identity_digest(result), identity)
        # 別プロジェクトの記録は、どちらの確認値にも入れない(空の一覧と同じ値になる)。
        other = fixed_result()
        other['candidates'][0].update(relation='other-project', reason=None)
        empty = fixed_result()
        empty['candidates'] = []
        self.assertEqual(self.module.list_digest(other), self.module.list_digest(empty))
        self.assertEqual(self.module.identity_digest(other), self.module.identity_digest(empty))
        self.assertNotEqual(self.module.list_digest(other), expected)

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

    def test_python310_syntax_and_no_process_or_write_entry(self):
        source = HELPER.read_text(encoding='utf-8')
        tree = ast.parse(source, feature_version=(3, 10))
        imports = {n.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for n in node.names}
        imports |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imports & {'subprocess', 'multiprocessing', 'pty'})
        forbidden = {'system', 'popen', 'spawn', 'fork', 'execve', 'kill', 'write', 'write_bytes',
                     'write_text', 'mkdir', 'makedirs', 'rename', 'replace', 'unlink', 'remove', 'rmdir',
                     'chmod', 'chown', 'utime', 'listdir'}
        calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and not (node.func.attr == 'write' and isinstance(node.func.value, ast.Attribute)
                          and isinstance(node.func.value.value, ast.Name)
                          and node.func.value.value.id == 'sys'
                          and node.func.value.attr in ('stdout', 'stderr'))}
        self.assertFalse(calls & forbidden, calls & forbidden)

    # ---- 判定と、その前後の継ぎ目を固定する特性テスト(一時ホームだけを使う) ----
    def make_top(self, top):
        # git のコマンドを使わず、今回の範囲として読める最小の形を作る。
        top = Path(top)
        (top / '.git/objects').mkdir(parents=True, exist_ok=True)
        (top / '.git/refs').mkdir(parents=True, exist_ok=True)
        (top / 'task').mkdir(parents=True, exist_ok=True)
        (top / '.git/HEAD').write_bytes(b'ref: refs/heads/main\n')
        return top

    def make_candidate(self, name, top=None, loop_tmpdir=False):
        guard = self.roots[0] / ('guard-' + name)
        guard.mkdir()
        if top is None:
            return guard
        tmpdir = None
        if loop_tmpdir:
            tmpdir = self.base / ('loop-' + name)
            tmpdir.symlink_to(tmpdir)  # 開くとELOOPになり、祖先をたどれない
        mark = self.roots[0] / ('owner-' + name)
        mark.write_bytes(mark_bytes(make_mark_values(guard, top, top / '.git', top / 'task', tmpdir)))
        mark.chmod(0o600)
        return guard

    def make_kind(self, name, kind):
        if kind == 'plain':
            return self.make_candidate(name)
        if kind == 'overlap':
            return self.make_candidate(name, self.make_top(self.cwd))
        top = self.make_top(self.base / ('top-' + name))
        return self.make_candidate(name, top, loop_tmpdir=(kind == 'unknown'))

    def reset_fixture(self):
        for root in self.roots:
            shutil.rmtree(root)
            root.mkdir(parents=True)
        for child in self.base.iterdir():
            if child.name in ('cwd', 'home', 'xdg'):
                continue
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)

    def call_main(self, extra=(), patches=()):
        self.make_top(self.cwd)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
            stack.enter_context(mock.patch.object(self.module, 'TEMP_DIRS', ()))
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = self.module.main(['scan', '--cwd', str(self.cwd)] + list(extra))
        result = json.loads(out.getvalue())
        self.assertEqual(out.getvalue().encode('ascii'), canonical(result) + b'\n')
        self.check_schema(rc, result)
        return rc, result, err.getvalue()

    def test_main_outcome_exit_code_and_stderr_are_pinned(self):
        other_note = lambda n: ('別プロジェクトの保護記録: ' + str(n) + ' 件。所属の印で別のプロジェクトと確かめたため、'
                                '止めません(名前とパスはJSONにだけ出します)。\n')
        confirm = '保護記録候補の一覧について、人の確認が必要です。\n'
        overlap = OVERLAP_LINE + '\n'
        cases = [
            ('a', [], None, 'none', 0, ''),
            ('b', [('a', 'other', None), ('b', 'other', None)], None, 'other-projects-only', 0, other_note(2)),
            ('c', [('a', 'other', None), ('b', 'plain', 'no-mark')], None, 'confirmation-required', 10,
             other_note(1) + confirm),
            ('d', [('a', 'other', None), ('b', 'overlap', 'overlap')], 'identity', 'list-changed', 10,
             other_note(1) + overlap + confirm),
            ('e', [('a', 'other', None), ('b', 'overlap', 'overlap')], 'list', 'acknowledged', 0, other_note(1)),
            ('f', [('a', 'other', None), ('b', 'plain', 'no-mark')], 'identity', 'acknowledged', 0, other_note(1)),
            ('g', [('a', 'unknown', 'overlap-unknown')], 'identity', 'list-changed', 10, overlap + confirm),
            ('h', [('a', 'plain', 'no-mark')], 'zeros', 'list-changed', 10, confirm),
        ]
        for label, candidates, ack, outcome, code, stderr in cases:
            with self.subTest(case=label):
                self.reset_fixture()
                for name, kind, _ in candidates:
                    self.make_kind(name, kind)
                rc, first, _ = self.call_main()
                self.assertEqual([c['reason'] for c in first['candidates']], [r for _, _, r in candidates])
                extra = {None: [], 'list': ['--acknowledged-list', first['list_sha256']],
                         'identity': ['--acknowledged-identities', first['identity_sha256']],
                         'zeros': ['--acknowledged-identities', '0' * 64]}[ack]
                rc, second, err = self.call_main(extra)
                self.assertEqual((second['result'], rc, err), (outcome, code, stderr))
                self.assertEqual((second['list_sha256'], second['identity_sha256']),
                                 (first['list_sha256'], first['identity_sha256']))

    def test_final_time_check_sits_between_digests_and_decision(self):
        self.make_kind('other', 'other')
        self.make_kind('plain', 'plain')
        original = self.module.identity_digest
        called = [False]

        def spy(result):
            value = original(result)
            called[0] = True
            return value
        patches = [mock.patch.object(self.module, 'identity_digest', spy),
                   mock.patch.object(self.module.time, 'monotonic', lambda: 6.0 if called[0] else 0.0)]
        rc, result, err = self.call_main(patches=patches)
        self.assertTrue(called[0])
        self.assertEqual((rc, result['result'], result['complete']), (11, 'scan-unavailable', False))
        self.assertNotIn('list_sha256', result)
        self.assertNotIn('identity_sha256', result)
        self.assertEqual(result['issues'], [{'code': 'time-limit', 'root_id': None, 'path_bytes_hex': None,
                                             'message': '走査全体の5秒の上限に達しました。'}])
        self.assertEqual([c['reason'] for c in result['candidates']], [None, 'no-mark'])
        self.assertEqual([r['status'] for r in result['roots']], ['present'] * 3)
        self.assertEqual(err, 'pending-implementation: time-limit 走査全体の5秒の上限に達しました。\n')

    def test_unexpected_failure_in_decision_becomes_internal(self):
        class Exploding:
            def __contains__(self, item):
                raise RuntimeError('fixture decision failure')
        self.make_kind('other', 'other')
        self.make_kind('overlap', 'overlap')
        rc, result, err = self.call_main(['--acknowledged-identities', '0' * 64],
                                         [mock.patch.object(self.module, 'OVERLAP_REASONS', Exploding())])
        self.assertEqual((rc, result['result'], result['complete']), (20, 'internal', False))
        self.assertNotIn('list_sha256', result)
        self.assertNotIn('identity_sha256', result)
        message = '想定外の失敗が起きました: RuntimeError'
        self.assertEqual(result['issues'], [{'code': 'internal-error', 'root_id': None,
                                             'path_bytes_hex': None, 'message': message}])
        self.assertEqual(err, 'pending-implementation: internal-error ' + message + '\n')

    def test_decide_outcome_is_pure_and_returns_exit_outcome_and_notes(self):
        result = fixed_result()
        overlap = dict(result['candidates'][0], name_bytes_hex='67756172642d79', reason='overlap')
        other = dict(result['candidates'][0], name_bytes_hex='67756172642d7a', relation='other-project',
                     reason=None)
        result['candidates'] += [overlap, other]
        digests = {'list': 'a' * 64, 'identity': 'b' * 64}
        note = ('別プロジェクトの保護記録: 1 件。所属の印で別のプロジェクトと確かめたため、'
                '止めません(名前とパスはJSONにだけ出します)。')
        cases = [({}, (10, 'confirmation-required', [note])),
                 ({'identity': 'b' * 64}, (10, 'list-changed', [note, OVERLAP_LINE])),
                 ({'list': 'a' * 64}, (0, 'acknowledged', [note])),
                 ({'list': 'c' * 64}, (10, 'list-changed', [note]))]

        def forbidden(*args, **kwargs):
            raise AssertionError('decide_outcome must not touch files or the clock')
        for acknowledged, expected in cases:
            with self.subTest(acknowledged=acknowledged):
                before = json.dumps(result, sort_keys=True)
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(os, 'open', forbidden), mock.patch.object(os, 'stat', forbidden), \
                     mock.patch.object(os, 'scandir', forbidden), mock.patch.object(builtins, 'open', forbidden), \
                     mock.patch.object(self.module.time, 'monotonic', forbidden), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    actual = self.module.decide_outcome(result, acknowledged, digests)
                self.assertEqual(actual, expected)
                self.assertEqual(json.dumps(result, sort_keys=True), before)
                self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))


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
            # 確認値の使い分け: 全候補が無関係なら実体確認値、1件でも関連なら一覧確認値。
            # 答えと値の組を1つの文字列で求め、組の入れ替えを検出する。
            for term in ('全候補が「今回と無関係」なら、`identity_sha256` を `PENDING_IDENTITY_SHA256` として控え、'
                         '`--acknowledged-identities` へ渡す',
                         '1 件でも「以前の関連作業」なら、`list_sha256` を `PENDING_LIST_SHA256` として控え、'
                         '`--acknowledged-list` へ渡す',
                         '2 つを同時に渡さない',
                         # 段階2: 重なりのある候補は「無関係」と答えても一覧確認値を使う。別プロジェクトの記録だけなら続ける。
                         '`reason` が `overlap` か `overlap-unknown` の候補が 1 件でもあれば、人が全候補を「今回と無関係」と答えても '
                         '`list_sha256` を使う',
                         '0/`other-projects-only`(別プロジェクトの記録だけ。無人でも同じ)',
                         '件数と記録の名前だけを書く。パスは書かない'):
                self.assertIn(term, text)
            self.assertIn('非 Git', text)
            self.assertIn('タスク本文', text)
            self.assertIn('環境', text[:phase])
        self.assertIn('`OWNER_MARK=no` なら、「この記録は、ほかのセッションの開始前の確認で止まりうる」と報告に書く', do)
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
                    '実行可能な skill dispatcher はない', 'AI が手順を守ることや OS による強制は保証しない',
                    # 段階1: 2つの確認値の使い分け・引き継ぎ・入力・限界。
                    '--acknowledged-identities', 'PENDING_IDENTITY_SHA256', 'schema_version 2の型と出力契約',
                    '2つの確認値は同時に渡せません',
                    # 答えと値の組を1つの文字列で求め、組の入れ替えを検出する。
                    '全候補について、人が「今回と無関係の記録である」と答えた場合だけ、'
                    '`identity_sha256`を`PENDING_IDENTITY_SHA256`として控えます。`--acknowledged-identities`に渡して',
                    '1件でも「以前の関連作業の記録である」と答えた場合は、今までどおり'
                    '`list_sha256`を`PENDING_LIST_SHA256`として控え、`--acknowledged-list`で照合します',
                    'どちらの確認値かを区別して引き継ぎ',
                    '`mtime_ns`、`ctime_ns`の11項目', '`dev`、`ino`、`uid`、`reason`の8項目',
                    '同じ実体の更新時刻・大きさ・権限の変化では変わりません', 'inodeの再利用',
                    # 段階2: 印・分類の規則と理由のコード・結果の表・#257の決定を変える範囲・限界。
                    'ただし、`reason`が`overlap`か`overlap-unknown`の候補が1件でもあれば、人が全候補を「今回と無関係」と答えても'
                    '`list_sha256`を使います',
                    '**所属の印(#291)**', '**今回の作業のGitの置き場**(Gitを起動しない)', '**候補ごとの判定**',
                    '`no-mark`', '`mark-invalid`', '`guard-mismatch`', '`owner-path-mismatch`', '`git-unavailable`',
                    '`overlap-unknown`', '**結果の決め方**', '| 0 | `other-projects-only` |',
                    '**#257の決定を変える範囲**', '**検出の限界(所属の印)**',
                    '一時フォルダ(`/tmp`など)の下の作業では、印のある記録も全部「確認が要る」(`overlap`)になります',
                    'Gitでない作業では、印のある記録も全部「確認が要る」(`git-unavailable`)になります']
        for term in required:
            with self.subTest(term=term):
                self.assertIn(term, contract)
        # list_sha256の入力は、候補の項目を名前で決める(表示用path以外の全キーではない)。
        self.assertNotIn('表示用`path`以外の全キー', contract)
        runtime = (SKILL / 'references/runtime-requirements.md').read_text()
        self.assertIn('Python 3.10', runtime)
        self.assertNotIn('Python 3.8', runtime)


if __name__ == '__main__':
    unittest.main()
