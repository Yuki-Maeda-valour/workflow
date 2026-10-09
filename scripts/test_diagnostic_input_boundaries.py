"""#260: 開始出典・管理座標・profile・保持計画の実入口境界。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from diagnostic_test_support import DIAGNOSTIC, GUARD, DiagnosticFixture, strict_json

MIB = 1024 * 1024


class DiagnosticInputBoundaries(DiagnosticFixture):
    def saved_plan(self, plan, name='boundary.json', size=None):
        raw = json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()
        if size is not None:
            self.assertLessEqual(len(raw), size)
            raw += b' ' * (size - len(raw))
        path = self.plan_dir / name
        path.write_bytes(raw); path.chmod(0o600)
        return path, hashlib.sha256(raw).hexdigest()

    def run_plan(self, path, digest):
        return self.cli('run', *self.common, '--plan', path, '--plan-sha256', digest)

    def test_four_caller_start_payloads_preserve_base_start_and_branch_provenance(self):
        base = self.start_oid
        (self.repo / 'seed.txt').write_text('invocation start\n')
        start = self.commit('start')
        (self.repo / 'seed.txt').write_text('recorded branch start\n')
        branch = self.commit('branch')
        for index, (caller, kind, recorded) in enumerate([
            ('direct', 'new-invocation', None), ('ship-existing', 'new-invocation', None),
            ('workflow-new', 'new-invocation', branch), ('confirmed-resume', 'resume-invocation', branch)]):
            with self.subTest(caller=caller):
                state, fields = self.take()
                args = ['--cwd', self.repo, '--state', state,
                    '--manifest-sha256', fields['MANIFEST_SHA256'],
                    '--snapshot-sha256', fields['SNAPSHOT_SHA256'], '--context-id', '0',
                    '--base-ref', base, '--start-ref', start, '--start-kind', kind]
                if recorded: args += ['--branch-ref', recorded]
                output = self.plan_dir / ('caller-%d.json' % index)
                self.success(self.cli('plan', *args, '--out', output), 'plan-created')
                policy = strict_json(output.read_bytes())['policies'][0]
                sources = {s['role']: s for s in policy['sources']}
                self.assertEqual(sources['base']['commit_oid'], base)
                self.assertEqual(sources['start']['commit_oid'], start)
                self.assertEqual(policy['start_kind'], kind)
                self.assertEqual(policy['branch_provenance'], 'workflow-recorded' if recorded else 'not-recorded')
                if recorded: self.assertEqual(sources['branch']['commit_oid'], recorded)
                else: self.assertNotIn('branch', sources)

    def management_begin(self):
        management = self.repo / 'pkg[1]*'; management.mkdir()
        (management / '.claude').mkdir()
        (management / '.claude/project-profile.yml').write_text('secret_paths:\n  - relative.key\n')
        for prefix in (self.repo, management):
            for name in ('relative.key', 'glob.key', 'ere.key', 'visible.txt'):
                (prefix / name).write_text('PUBLIC_FIXTURE')
        self.commit()
        result = subprocess.run(['bash', str(GUARD), 'take', '--cwd', str(management),
            '--task-md', str(self.repo / 'task/t.md')], env=self.env, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = dict(x.split('=', 1) for x in result.stdout.decode().splitlines())
        self.state = Path(fields['STATE_DIR'])
        self.binding = {'manifest_sha256': fields['MANIFEST_SHA256'], 'snapshot_sha256': fields['SNAPSHOT_SHA256']}
        self.common = ['--cwd', str(management), '--state', str(self.state),
            '--manifest-sha256', fields['MANIFEST_SHA256'], '--snapshot-sha256', fields['SNAPSHOT_SHA256'], '--context-id', '0']
        self.latest = self.plan_dir / 'management.json'; self.plan_number = 0
        response = self.success(self.cli('plan', *self.common, '--base-ref', self.base_oid,
            '--start-ref', self.start_oid, '--start-kind', 'new-invocation',
            '--exclude-root', 'management', '--exclude-glob', '/glob.key', '--exclude', '^ere[.]key$',
            '--out', self.latest), 'plan-created')
        self.latest_hash = response['plan_sha256']

    def test_management_relative_glob_and_ere_keep_coordinates_and_require_policy(self):
        self.management_begin()
        names = self.records(self.observe('files', '--under', '.', '--kind', 'all'))
        for name in ('relative.key', 'glob.key', 'ere.key'):
            self.assertIn(name.encode(), names)
            self.assertNotIn(('pkg[1]*/'+name).encode(), names)
        self.assertIn(b'pkg[1]*/visible.txt', names)
        plan = strict_json(self.latest.read_bytes())
        plan['policies'] = [p for p in plan['policies'] if p['root_kind'] != 'management']
        path, digest = self.saved_plan(plan)
        self.failure(self.run_plan(path, digest), 22, 'missing-management-policy')

    def test_extra_only_profile_and_profile_self_exclusion(self):
        self.profile(['extra-only', '.claude/project-profile.yml'])
        extra = self.commit('extra profile')
        self.profile([])
        current = self.commit('profile removed')
        (self.repo / 'extra-only').write_text('HIDDEN_EXTRA_FIXTURE')
        self.begin('--profile-ref', extra, base=current, start=current)
        result = self.observe('files', '--under', '.', '--kind', 'all')
        self.assertNotIn(b'extra-only', self.records(result))
        self.assertNotIn(b'.claude/project-profile.yml', self.records(result))
        self.assertIn(b'seed.txt', self.records(result))
        self.assertNotIn(b'HIDDEN_EXTRA_FIXTURE', json.dumps(result).encode())
        roles = [s for s in strict_json(self.latest.read_bytes())['policies'][0]['sources'] if s['role']=='extra']
        self.assertEqual([s['commit_oid'] for s in roles], [extra])
        self.extend('show', '--path', '.claude/project-profile.yml', '--format', 'blob')
        self.failure(self.run_plan(self.latest, self.latest_hash), 23, 'excluded-selection')

    def test_profile_invalid_forms_and_one_mib_boundary_do_not_weaken_exclusions(self):
        self.begin()
        path = self.profile([])
        prefix = b'secret_paths: []\n#'
        path.write_bytes(prefix+b'x'*(MIB-len(prefix)))
        self.observe('refs')
        good = path.read_bytes()
        for number, bad in enumerate([b'secret_paths: [\xff]\n',
                b'secret_paths: []\nsecret_paths: [seed.txt]\n', good+b'x', None]):
            with self.subTest(case=number):
                if bad is None:
                    path.unlink(); os.mkfifo(path)
                else: path.write_bytes(bad)
                result = self.cli('extend-plan', *self.common, '--plan', self.latest,
                    '--plan-sha256', self.latest_hash, '--out', self.plan_dir / ('invalid-profile-%d' % number), 'refs')
                self.failure(result, 23, 'profile-invalid')
                if bad is None: path.unlink()
                path.write_bytes(good)
        self.observe('refs')

    def test_head_and_selected_ref_stale_fail_before_worktree_body_open(self):
        self.begin()
        for changed in ('head', 'ref'):
            with self.subTest(changed=changed):
                if changed=='ref': self.git('update-ref', 'refs/heads/selected', self.start_oid)
                args = [] if changed=='head' else ['--left', 'refs/heads/selected']
                self.extend('diff', '--path', 'seed.txt', *args)
                parent = self.git('rev-parse', 'HEAD').stdout.decode().strip()
                self.git('commit', '--allow-empty', '-qm', 'new head')
                new = self.git('rev-parse', 'HEAD').stdout.decode().strip()
                if changed=='ref':
                    self.git('update-ref', 'refs/heads/selected', new)
                    self.git('update-ref', 'HEAD', parent)
                driver = r'''
import importlib.util,os,sys
spec=importlib.util.spec_from_file_location('diagnostic_entry',sys.argv[1]); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
target=os.stat(sys.argv[2]); original=os.open
opened=[]
def tracked(*args,**kwargs):
 fd=original(*args,**kwargs)
 s=os.fstat(fd)
 if (s.st_dev,s.st_ino)==(target.st_dev,target.st_ino): opened.append(True)
 return fd
check=m.R.Runtime.check_runtime
def checked(rt):
 check(rt);os.open=tracked
m.R.Runtime.check_runtime=checked
rc=m.main(sys.argv[3:])
if opened: raise SystemExit(91)
raise SystemExit(rc)
'''
                result = subprocess.run([sys.executable, '-I', '-S', '-c', driver,
                    str(DIAGNOSTIC), str(self.repo / 'seed.txt'), 'run', *self.common,
                    '--plan', str(self.latest), '--plan-sha256', self.latest_hash],
                    env=self.env, capture_output=True, timeout=30)
                self.failure(result, 22, 'preparation-stale')
                self.observe('diff', '--path', 'seed.txt', *args)

    def test_plan_sixteen_mib_and_source_two_thousand_boundaries(self):
        self.begin(); self.extend('refs')
        plan = strict_json(self.latest.read_bytes())
        path, digest = self.saved_plan(plan, size=16*MIB)
        self.success(self.run_plan(path, digest), 'observed')
        path, digest = self.saved_plan(plan, size=16*MIB+1)
        self.failure(self.run_plan(path, digest), 20, 'file-limit')
        sources = plan['policies'][0]['sources']
        for number in range(2000-len(sources)):
            sources.append({'role':'extra','commit_oid':format(number+1,'040x'),
                'profile_blob_oid':None,'profile_sha256':None,'absent':True})
        path, digest = self.saved_plan(plan)
        self.success(self.run_plan(path, digest), 'observed')
        sources.append({'role':'extra','commit_oid':'f'*40,'profile_blob_oid':None,'profile_sha256':None,'absent':True})
        path, digest = self.saved_plan(plan)
        self.failure(self.run_plan(path, digest), 20, 'plan-limit')

    def test_state_snapshot_entry_sixteen_mib_boundary(self):
        self.begin(); self.extend('refs')
        # 意図した境界fixtureの外側hashだけを独立計算。製品に再採用手段を追加しない。
        extra = self.state / 'snapshot/boundary.bin'
        for size in (16*MIB, 16*MIB+1):
            extra.write_bytes(b'x'*size)
            aggregate = b''.join(hashlib.sha256(p.read_bytes()).hexdigest().encode()+b'  '+os.fsencode(p.name)+b'\n'
                for p in sorted((self.state/'snapshot').iterdir(), key=lambda p:os.fsencode(p.name)) if not p.name.startswith('.'))
            snapshot = hashlib.sha256(aggregate).hexdigest()
            common = self.common.copy(); common[common.index('--snapshot-sha256')+1] = snapshot
            result = self.cli('plan', *common, '--base-ref', self.base_oid, '--start-ref', self.start_oid,
                '--start-kind', 'new-invocation', '--out', self.plan_dir / ('state-%d' % size))
            if size==16*MIB: self.success(result, 'plan-created')
            else: self.failure(result, 20, 'file-limit')

    def test_ere_single_and_aggregate_utf8_byte_boundaries(self):
        self.begin(); self.extend('refs')
        plan = strict_json(self.latest.read_bytes()); policy = plan['policies'][0]
        def expression(size, index=0):
            piece = '^never%d$\n' % index
            return piece*(size//len(piece)) + 'z'*(size%len(piece))
        policy['eres'] = [expression(65536)]
        path, digest = self.saved_plan(plan)
        self.success(self.run_plan(path, digest), 'observed')
        policy['eres'] = [expression(65537)]
        path, digest = self.saved_plan(plan)
        self.failure(self.run_plan(path, digest), 20, 'expression-limit')
        remaining = MIB-sum(len(x.encode()) for x in policy['globs'])
        policy['eres'] = []
        while remaining:
            size = min(65536, remaining)
            policy['eres'].append(expression(size, len(policy['eres'])))
            remaining -= size
        path, digest = self.saved_plan(plan)
        self.success(self.run_plan(path, digest), 'observed')
        policy['eres'][-1] += 'z'
        path, digest = self.saved_plan(plan)
        self.failure(self.run_plan(path, digest), 20, 'plan-limit')

    def test_ref_name_1024_bytes_and_management_depth40(self):
        self.begin()
        def refname(size):
            prefix = 'refs/heads/'
            while size-len(prefix)>201: prefix += 'a'*200+'/'
            return prefix+'z'*(size-len(prefix))
        for size in (1024,1025):
            name = refname(size); path = self.repo/'.git'/name
            path.parent.mkdir(parents=True,exist_ok=True); path.write_text(self.start_oid+'\n')
            result = self.cli('extend-plan', *self.common, '--plan', self.latest,
                '--plan-sha256', self.latest_hash, '--out', self.plan_dir/('ref-%d'%size), 'refs')
            if size==1024: self.success(result, 'plan-extended')
            else: self.failure(result, 23)
            path.unlink()
        directory = self.repo/'.git/refs'
        for _ in range(40): directory /= 'd'; directory.mkdir()
        (directory/'leaf').write_text(self.start_oid+'\n')
        self.observe('refs')
        (directory/'deeper').mkdir()
        self.failure(self.cli('extend-plan', *self.common, '--plan', self.latest,
            '--plan-sha256', self.latest_hash, '--out', self.plan_dir/'deep-fail', 'refs'),20,'management-depth-limit')

    def test_worktree_depth128_is_allowed_but129_is_rejected(self):
        self.begin()
        directory = self.repo
        for _ in range(128): directory /= 'd'; directory.mkdir()
        (directory/'leaf').write_text('visible')
        result = self.observe('files','--under','.','--kind','all')
        self.assertIn(os.fsencode((directory/'leaf').relative_to(self.repo)),self.records(result))
        (directory/'deeper').mkdir()
        self.extend('files','--under','.','--kind','all')
        self.failure(self.run_plan(self.latest,self.latest_hash),20,'worktree-depth-limit')


if __name__ == '__main__':
    unittest.main()
