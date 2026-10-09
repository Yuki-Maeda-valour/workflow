"""#260: 元の設定を使わない起動、除外計画の安全な出力、正常な追加出典。"""
import base64
import hashlib
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import unittest

from diagnostic_test_support import DIAGNOSTIC, DiagnosticFixture, inventory, strict_json


class DiagnosticProtection(DiagnosticFixture):
    def test_non_ascii_effective_exclusion_hash_is_utf8(self):
        self.profile(['秘密.txt', '資料/*.key'])
        self.begin()
        self.observe('refs')  # extendが独立したensure_ascii=Falseのhashと照合する。

    def test_first_git_and_every_git_use_private_admin_and_worktree(self):
        recorder = self.work / 'bin'; recorder.mkdir()
        trace = self.work / 'git-events.jsonl'
        script = recorder / 'git'
        script.write_text('#!' + sys.executable + '\n' +
            'import json,os,sys\n' +
            'keys=("GIT_DIR","GIT_WORK_TREE","GIT_COMMON_DIR","GIT_CONFIG_GLOBAL",'
            '"GIT_CONFIG_SYSTEM","GIT_CONFIG_COUNT","HOME")\n' +
            'with open(' + repr(str(trace)) + ',"a") as f: f.write(json.dumps('
            '{"argv":sys.argv[1:],"cwd":os.getcwd(),"env":{k:os.environ.get(k) for k in keys}})+"\\n")\n' +
            'os.execv(' + repr(self.git_binary) + ',[' + repr(self.git_binary) + ',*sys.argv[1:]])\n')
        script.chmod(0o755)
        diagnostic_env = dict(self.env, PATH=str(recorder) + os.pathsep + self.env['PATH'],
                              GIT_TRACE=str(self.work / 'forbidden-trace'),
                              GIT_NAMESPACE='unexpected-namespace')
        original_cli = self.cli
        self.cli = lambda *args, **kw: original_cli(*args, env=diagnostic_env, **kw)
        self.begin()
        self.observe('status', '--path', 'seed.txt')
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertTrue(events)
        for event in events:
            with self.subTest(argv=event['argv']):
                self.assertFalse(Path(event['cwd']).is_relative_to(self.repo))
                self.assertNotEqual(event['env']['HOME'], str(self.home))
                for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR'):
                    self.assertIsNotNone(event['env'][name])
                    self.assertFalse(Path(event['env'][name]).is_relative_to(self.repo))
                self.assertNotIn(str(self.repo), event['argv'])
        self.assertFalse((self.work / 'forbidden-trace').exists())

    def test_plan_out_rejects_repo_state_symlink_and_public_parent(self):
        self.begin()
        link = self.plan_dir / 'link'; link.symlink_to(self.work / 'target')
        public = self.protected / 'public'; public.mkdir(mode=0o755)
        targets = [self.repo / 'plan.json', self.state / 'plan.json', link, public / 'plan.json']
        for target in targets:
            with self.subTest(target=target):
                result = self.cli('extend-plan', *self.common, '--plan', self.latest,
                    '--plan-sha256', self.latest_hash, '--out', target, 'refs')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')
                if target != link: self.assertFalse(target.exists())
        self.assertTrue(link.is_symlink())
        self.assertFalse((self.work / 'target').exists())

    def test_plan_symlink_not_followed_even_with_matching_content_hash(self):
        self.begin()
        link = self.plan_dir / 'linked-plan'; link.symlink_to(self.latest)
        self.failure(self.cli('run', *self.common, '--plan', link,
                             '--plan-sha256', self.latest_hash), 22)

    def test_operation_commit_profile_and_parent_profile_are_collected(self):
        self.profile(['old-secret'])
        (self.repo / 'old-secret').write_bytes(b'SECRET_FROM_FIRST_PARENT')
        old = self.commit('first parent')
        self.profile(['new-secret'])
        (self.repo / 'new-secret').write_bytes(b'SECRET_FROM_SHOW_COMMIT')
        shown = self.commit('shown')
        self.profile([])
        current = self.commit('current')
        self.begin(start=current, base=current)
        result = self.observe('show', '--rev', shown, '--under', '.', '--format', 'patch')
        names = self.records(result)
        self.assertNotIn(b'old-secret', names)
        self.assertNotIn(b'new-secret', names)
        self.assertNotIn('SECRET_FROM_', json.dumps(result))
        plan = strict_json(self.latest.read_bytes())
        sources = plan['policies'][0]['sources']
        self.assertTrue(any(s['commit_oid'] == old for s in sources))
        self.assertTrue(any(s['commit_oid'] == shown for s in sources))

    def test_additional_exclusions_require_coordinate_and_keep_normal_public_file(self):
        self.begin('--exclude-root', 'context', '--exclude-glob', 'hidden.*', '--exclude', '^excluded$')
        (self.repo / 'hidden.txt').write_text('secret')
        (self.repo / 'excluded').write_text('secret')
        result = self.observe('files', '--under', '.', '--kind', 'all')
        names = self.records(result)
        self.assertIn(b'seed.txt', names)
        self.assertNotIn(b'hidden.txt', names)
        self.assertNotIn(b'excluded', names)

    def test_multiline_additional_ere_preserves_gnu_pattern_lines(self):
        self.begin('--exclude-root', 'context', '--exclude', '^alpha$\n^beta$')
        for name in ('alpha', 'beta', 'alphabet'):
            (self.repo / name).write_text('ordinary fixture')
        result = self.observe('files', '--under', '.', '--kind', 'all')
        self.assertNotIn(b'alpha', self.records(result))
        self.assertNotIn(b'beta', self.records(result))
        self.assertIn(b'alphabet', self.records(result))
        self.assertIn(b'seed.txt', self.records(result))

    def test_no_coordinate_for_extra_expression_is_invalid(self):
        state, fields = self.take()
        self.failure(self.cli('plan', '--cwd', self.repo, '--state', state,
            '--manifest-sha256', fields['MANIFEST_SHA256'], '--snapshot-sha256', fields['SNAPSHOT_SHA256'],
            '--context-id', '0', '--base-ref', self.base_oid, '--start-ref', self.start_oid,
            '--start-kind', 'new-invocation', '--exclude-glob', '*.key', '--out', self.plan_dir / 'bad'), 2)

    def test_scope_and_path_limits_are_explicit(self):
        self.begin()
        for n, args in enumerate([['--path', 'a'*4097], [arg for i in range(4097) for arg in ('--path', 'p%d' % i)]]):
            with self.subTest(n=n):
                result = self.cli('extend-plan', *self.common, '--plan', self.latest,
                    '--plan-sha256', self.latest_hash, '--out', self.plan_dir / ('limit%d' % n), 'files', *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')

    def test_latest_plan_keeps_exclusion_after_run_failure(self):
        self.begin()
        self.profile(['retained-secret'])
        (self.repo / 'retained-secret').write_text('do not expose')
        os.mkfifo(self.repo / 'failure-pipe')
        self.extend('diff', '--path', 'failure-pipe')
        held_path, held_hash = self.latest, self.latest_hash
        self.failure(self.cli('run', *self.common, '--plan', held_path,
                             '--plan-sha256', held_hash), 23)
        self.assertEqual(hashlib.sha256(held_path.read_bytes()).hexdigest(), held_hash)
        self.profile([])
        result = self.observe('files', '--path', 'retained-secret', '--kind', 'all')
        self.assertEqual(result['data']['records'], [])
        self.assertIn('paths-excluded', result['warnings'])

    def test_prepared_plan_rejects_missing_policy_sources_and_wrong_types(self):
        self.begin()
        self.observe('refs')
        original = strict_json(self.latest.read_bytes())
        changes = [('no-policy', lambda p: p.update(policies=[]), 22),
                   ('no-sources', lambda p: p['policies'][0].update(sources=[]), 22),
                   ('bool-context', lambda p: p.update(context_id=True), 2),
                   ('bad-globs', lambda p: p['policies'][0].update(globs=[False]), 2),
                   ('bad-absent', lambda p: p['policies'][0]['sources'][0].update(absent=1), 2),
                   ('unknown-role', lambda p: p['policies'][0]['sources'][0].update(role='unknown'), 2)]
        for name, mutate, rc in changes:
            with self.subTest(change=name):
                plan = json.loads(json.dumps(original)); mutate(plan)
                path = self.plan_dir / (name + '.json')
                raw = json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()
                path.write_bytes(raw); path.chmod(0o600)
                self.failure(self.cli('run', *self.common, '--plan', path,
                    '--plan-sha256', hashlib.sha256(raw).hexdigest()), rc)


    def test_published_plan_is_retained_when_success_output_is_interrupted(self):
        self.begin()
        from diagnostic_test_support import DIAGNOSTIC, inventory
        old_raw = self.latest.read_bytes()
        before_state = inventory(self.state)
        for operation in ('plan', 'extend-plan'):
            with self.subTest(operation=operation):
                output = self.plan_dir / ('interrupted-' + operation + '.json')
                if operation == 'plan':
                    args = ['plan', *self.common, '--base-ref', self.base_oid,
                            '--start-ref', self.start_oid, '--start-kind', 'new-invocation', '--out', str(output)]
                else:
                    args = ['extend-plan', *self.common, '--plan', str(self.latest),
                            '--plan-sha256', self.latest_hash, '--out', str(output), 'refs']
                readfd, writefd = os.pipe()
                process = None
                try:
                    os.set_blocking(writefd, False)
                    try:
                        while True: os.write(writefd, b'f'*4096)
                    except BlockingIOError: pass
                    os.set_blocking(writefd, True)
                    process = subprocess.Popen([sys.executable, '-I', str(DIAGNOSTIC), *args],
                        env=self.env, cwd=self.work, stdin=subprocess.DEVNULL,
                        stdout=writefd, stderr=subprocess.PIPE)
                    deadline = time.monotonic()+10
                    while not output.exists() and process.poll() is None and time.monotonic()<deadline:
                        time.sleep(.01)
                    self.assertTrue(output.exists(), '成功応答前に公開したplanが必要')
                    self.assertIsNone(process.poll())
                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=12), 20)
                    self.assertTrue(output.is_file())
                    self.assertEqual(self.latest.read_bytes(), old_raw)
                    self.assertEqual(inventory(self.state), before_state)
                    self.assertNotIn(b'Traceback', process.stderr.read())
                    # 残存FILEをhash再取得して信用値へ採用したり、旧planで再試行しない。
                finally:
                    if process is not None:
                        if process.poll() is None: process.kill(); process.wait()
                        process.stderr.close()
                    os.close(readfd); os.close(writefd)



    def test_postcheck_cleanup_json_and_preoutput_stop_never_emit_success(self):
        self.begin(); self.extend('refs')
        before = (inventory(self.repo), inventory(self.state), inventory(self.plan_dir))
        driver = r"""
import importlib.util,sys
spec=importlib.util.spec_from_file_location('diagnostic_entry',sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
phase=sys.argv[2]
if phase in ('postcheck','stop'):
    original=module.R.Reader.verify
    def verify(reader):
        original(reader)
        if phase=='stop': reader.budget.stopped=True
        else: module.R.fail('source-changed')
    module.R.Reader.verify=verify
elif phase=='cleanup':
    original=module.R.Runtime.cleanup
    def cleanup(runtime):
        if not getattr(runtime,'test_failed_once',False):
            runtime.test_failed_once=True; module.R.fail('cleanup')
        return original(runtime)
    module.R.Runtime.cleanup=cleanup
elif phase=='json':
    original=module.R.Runtime.response_bytes
    def response(runtime,value):
        runtime.budget.limits['json']=1
        return original(runtime,value)
    module.R.Runtime.response_bytes=response
raise SystemExit(module.main(sys.argv[3:]))
"""
        for phase in ('postcheck', 'cleanup', 'json', 'stop', 'normal'):
            with self.subTest(phase=phase):
                result = subprocess.run([sys.executable, '-I', '-S', '-c', driver,
                    str(DIAGNOSTIC), phase, 'run', *self.common, '--plan', str(self.latest),
                    '--plan-sha256', self.latest_hash], env=self.env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                if phase == 'normal': self.success(result, 'observed')
                else:
                    self.assertEqual(result.returncode, 20, result.stderr)
                    self.assertEqual(result.stdout, b'')
                    self.assertNotIn(b'Traceback', result.stderr)
                self.assertEqual(before, (inventory(self.repo), inventory(self.state), inventory(self.plan_dir)))


if __name__ == '__main__':
    unittest.main()
