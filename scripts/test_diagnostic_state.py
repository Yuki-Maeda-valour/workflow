"""#260: 実takeの記録形式と保持値の入口の回帰。"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from diagnostic_test_support import DIAGNOSTIC, DiagnosticFixture, inventory


class DiagnosticStateTests(DiagnosticFixture):
    def arguments(self, state, fields):
        return ['--cwd', str(self.repo), '--state', str(state),
                '--manifest-sha256', fields['MANIFEST_SHA256'],
                '--snapshot-sha256', fields['SNAPSHOT_SHA256'], '--context-id', '0',
                '--base-ref', self.base_oid, '--start-ref', self.start_oid,
                '--start-kind', 'new-invocation']

    def altered_snapshot_hash(self, state):
        # 試験が意図的に作る旧形式・不正形式にも正しい外側hashを付け、
        # 外側hashの不一致だけでなく内部schemaを検査する。製品の採用手順では使わない。
        parts = []
        for path in sorted((state / 'snapshot').iterdir(), key=lambda p: os.fsencode(p.name)):
            if not path.name.startswith('.'):
                parts.append(hashlib.sha256(path.read_bytes()).hexdigest().encode()+b'  '+os.fsencode(path.name)+b'\n')
        return hashlib.sha256(b''.join(parts)).hexdigest()

    def test_context_missing_unknown_record_truncation_and_wrong_id_are_rejected(self):
        state, fields = self.take()
        path = state / 'snapshot/config-contexts.txt'; original = path.read_bytes()
        rows = original.splitlines(keepends=True)
        context = next(i for i, row in enumerate(rows) if row.startswith(b'context\t'))
        columns = rows[context].rstrip(b'\n').split(b'\t')
        wrong_id = columns.copy(); wrong_id[1] = b'5'
        quote = columns.copy(); quote[5] = b'"unterminated'
        mutations = [None, original+b'unknown\tvalue\n', original[:-1],
                     b''.join(rows[:context]+[b'\t'.join(wrong_id)+b'\n']+rows[context+1:]),
                     b''.join(rows[:context]+[b'\t'.join(quote)+b'\n']+rows[context+1:])]
        for n, content in enumerate(mutations):
            with self.subTest(case=n):
                if content is None: path.unlink()
                else: path.write_bytes(content)
                fields['SNAPSHOT_SHA256'] = self.altered_snapshot_hash(state)
                self.failure(self.cli('plan', *self.arguments(state, fields),
                                     '--out', self.plan_dir / ('bad-state%d' % n)), 22)
                path.write_bytes(original)

    def test_unknown_origin_record_is_rejected_with_valid_outer_hash(self):
        state, fields = self.take()
        with (state / 'snapshot/config-origins.txt').open('ab') as out:
            out.write(b'unknown\tvalue\n')
        fields['SNAPSHOT_SHA256'] = self.altered_snapshot_hash(state)
        self.failure(self.cli('plan', *self.arguments(state, fields),
                             '--out', self.plan_dir / 'bad-origins'), 22)

    def test_state_symlink_and_insecure_mode_are_rejected(self):
        state, fields = self.take()
        link = state.parent / 'guard-link'; link.symlink_to(state, target_is_directory=True)
        self.failure(self.cli('plan', *self.arguments(link, fields),
                             '--out', self.plan_dir / 'linked-state'), 22)
        state.chmod(0o755)
        self.failure(self.cli('plan', *self.arguments(state, fields),
                             '--out', self.plan_dir / 'public-state'), 22)

    def test_replaced_repository_management_identity_is_not_adopted(self):
        self.begin()
        self.extend('refs')
        old = self.work / 'old-repo'
        self.repo.rename(old)
        shutil.copytree(old, self.repo)
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
                             '--plan-sha256', self.latest_hash), 22)


    def owner_probe(self, target, changed, *arguments):
        # 実所有者は変えず、この試験の対象inodeについて返るstatだけを置換する。
        # 起動能力検査は未変更のAPIで通し、その後の本物CLI入口を使う。
        driver = r"""
import importlib.util, os, sys
spec=importlib.util.spec_from_file_location('diagnostic_entry',sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
held=os.lstat(sys.argv[2]); identity=(held.st_dev,held.st_ino)
changed=sys.argv[3]=='changed'
class Foreign:
    def __init__(self, real): self.real=real
    def __getattr__(self, name):
        return self.real.st_uid+1 if name=='st_uid' else getattr(self.real,name)
def wrap(original):
    def replacement(*args, **kwargs):
        value=original(*args,**kwargs)
        return Foreign(value) if changed and (value.st_dev,value.st_ino)==identity else value
    return replacement
check=module.R.Runtime.check_runtime
def checked(runtime):
    check(runtime)
    for name in ('stat','lstat','fstat'):
        setattr(os,name,wrap(getattr(os,name)))
module.R.Runtime.check_runtime=checked
raise SystemExit(module.main(sys.argv[4:]))
"""
        return subprocess.run([sys.executable, '-I', '-S', '-c', driver,
            str(DIAGNOSTIC), str(target), 'changed' if changed else 'same',
            *map(str, arguments)], env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30)

    def test_state_and_input_plan_owners_are_effective_uid(self):
        self.begin(); self.extend('refs')
        args = ['run', *self.common, '--plan', self.latest,
                '--plan-sha256', self.latest_hash]
        before = (inventory(self.repo), inventory(self.state), inventory(self.plan_dir))
        for target in (self.state, self.latest, self.latest.parent):
            with self.subTest(target=str(target)):
                self.failure(self.owner_probe(target, True, *args), 22)
                self.success(self.owner_probe(target, False, *args), 'observed')
        self.assertEqual(before, (inventory(self.repo), inventory(self.state), inventory(self.plan_dir)))

    def test_output_direct_parent_owner_is_checked_without_restricting_ancestors(self):
        self.begin()
        output_parent = self.protected / 'output'; output_parent.mkdir(mode=0o700)
        output = output_parent / 'new-plan.json'
        args = ['extend-plan', *self.common, '--plan', self.latest,
                '--plan-sha256', self.latest_hash, '--out', output, 'refs']
        before = (inventory(self.repo), inventory(self.state), inventory(self.plan_dir))
        self.failure(self.owner_probe(output_parent, True, *args), 2)
        self.assertFalse(output.exists())
        self.success(self.owner_probe(output_parent, False, *args), 'plan-extended')
        # 一般祖先と元repoのfileは他UIDであることだけを理由に拒否しない。
        self.extend('files', '--path', 'seed.txt')
        run = ['run', *self.common, '--plan', self.latest,
               '--plan-sha256', self.latest_hash]
        for target in (self.protected, self.repo / 'seed.txt'):
            with self.subTest(unrestricted=str(target)):
                self.success(self.owner_probe(target, True, *run), 'observed')
        self.assertEqual(before[:2], (inventory(self.repo), inventory(self.state)))


if __name__ == '__main__':
    unittest.main()
