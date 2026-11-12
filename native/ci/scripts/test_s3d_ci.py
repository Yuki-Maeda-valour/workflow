"""S3Dの必須比較・型記録・配布除外・道具の変更を検出する。"""
import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build
import check_workflow
import s3d_ci
import make_dist
import source_input_hash as source_hash


class S3DCiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='s3d-ci-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_every_build_tar_requires_one_typed_report_and_both_complete_comparisons(self):
        # 比較は build の外の s3d ジョブで、4 機種の 2 回のビルドのそれぞれに流す(2026-11-12 12:59 の決定)。
        for name in build.TARGETS:
            for failure in (None, 'tools', 'typed', 'comparison'):
                with self.subTest(name=name, failure=failure), contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(sys, 'argv', ['build.py', 's3d', '--platform', name]))
                    stack.enter_context(mock.patch.object(build, 'workspace', return_value=self.root))
                    stack.enter_context(mock.patch.object(build, 'target_config', return_value=('target', None)))
                    extract = stack.enter_context(mock.patch.object(build, 'extract_build', return_value=(self.root, {})))
                    tools = stack.enter_context(mock.patch.object(s3d_ci, 'prepare_tools'))
                    typed = stack.enter_context(mock.patch.object(s3d_ci, 'typed_report', return_value=self.root/'typed.json'))
                    probe = stack.enter_context(mock.patch.object(s3d_ci, 'probe'))
                    error = subprocess.CalledProcessError(1, 'mandatory S3D comparison')
                    if failure == 'tools': tools.side_effect = error
                    if failure == 'typed': typed.side_effect = error
                    if failure == 'comparison': probe.side_effect = error
                    self.assertEqual(build.main(), int(failure is not None))
                    tools.assert_called_once_with(name)
                    if failure is None:
                        extract.assert_called_once_with(name, self.root/'downloads', self.root)
                        typed.assert_called_once()
                        self.assertEqual(typed.call_args.args[0], name)
                        self.assertEqual([call.args[0] for call in probe.call_args_list],
                                         [self.root/'1/diff_snapshot_contract', self.root/'2/diff_snapshot_contract'])
                        self.assertTrue(all(call.args[1] == self.root/'typed.json' for call in probe.call_args_list))
                    elif failure in ('tools', 'typed'):
                        probe.assert_not_called()
        with mock.patch.object(sys, 'argv', ['build.py', 's3d']), self.assertRaises(SystemExit):
            build.main()

    def test_build_and_reproduce_do_not_run_the_comparison(self):
        for operation in ('build', 'reproduce'):
            with self.subTest(operation=operation), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(sys, 'argv', ['build.py', operation, '--platform', 'linux-x86_64']))
                for function, value in (('workspace', self.root), ('quality', None), ('reproduce', self.root), ('pack_tar', None),
                                        ('probe', None), ('r4b_probe', None), ('r4r_probe', None)):
                    stack.enter_context(mock.patch.object(build, function, return_value=value))
                typed = stack.enter_context(mock.patch.object(s3d_ci, 'typed_report'))
                probe = stack.enter_context(mock.patch.object(s3d_ci, 'probe'))
                self.assertEqual(build.main(), 0)
                typed.assert_not_called()
                probe.assert_not_called()

    def test_signed_binary_is_verified_then_compared_on_each_real_cpu(self):
        for name in build.S3D_UNIVERSAL_PLATFORMS:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(build, 'target_config', return_value=('target', None)))
                stack.enter_context(mock.patch.object(build, 'extract_universal', return_value=(self.root, {})))
                run = stack.enter_context(mock.patch.object(build, 'run', return_value='arm64 x86_64'))
                stack.enter_context(mock.patch.object(s3d_ci, 'prepare_tools'))
                typed = stack.enter_context(mock.patch.object(s3d_ci, 'typed_report', return_value=self.root/'typed.json'))
                compare = stack.enter_context(mock.patch.object(s3d_ci, 'probe'))
                build.s3d_universal(name, self.root, self.root)
                build.target_config.assert_called_once_with(name)
                compare.assert_called_once()
                self.assertEqual(compare.call_args.args[:2], (self.root/'diff_snapshot_contract', self.root/'typed.json'))
                self.assertEqual(typed.call_args.args[0], name)
                verified = [call.args[0][-1] for call in run.call_args_list if call.args[0][:2] == ['codesign', '--verify']]
                self.assertEqual(verified, [self.root/'diff_snapshot_contract'])
                run.return_value = 'x86_64'
                with self.assertRaisesRegex(ValueError, 'CPU'):
                    build.s3d_universal(name, self.root, self.root)
                run.return_value = 'arm64 x86_64'
                compare.side_effect = subprocess.CalledProcessError(1, 'S3D')
                with self.assertRaises(subprocess.CalledProcessError):
                    build.s3d_universal(name, self.root, self.root)
        for name in ('linux-x86_64', 'linux-arm64'):
            with self.assertRaisesRegex(ValueError, 'Mac'):
                build.s3d_universal(name, self.root, self.root)
        # verify-universal は署名と既存の検査だけを行い、S3D の比較は s3d-universal のジョブで流す。
        with mock.patch.object(build, 'extract_universal', return_value=(self.root, {})), \
                mock.patch.object(build, 'run', return_value='arm64 x86_64') as run, \
                mock.patch.object(build, 'probe'), mock.patch.object(build, 'r4b_probe'), mock.patch.object(build, 'r4r_probe'), \
                mock.patch.object(s3d_ci, 'probe') as compare:
            build.verify_universal(self.root, self.root)
            compare.assert_not_called()
            verified = [call.args[0][-1] for call in run.call_args_list if call.args[0][:2] == ['codesign', '--verify']]
            self.assertIn(self.root/'diff_snapshot_contract', verified)

    def test_quality_runs_the_example_unit_test_only_on_linux(self):
        unit = ['cargo', 'test', '--locked', '-p', 'dev-workflow-cli', '--example', 'diff_snapshot_contract']
        quiet = lambda args, *, env=None, capture=False: '' if capture else None
        for name in build.TARGETS:
            with self.subTest(name=name), \
                    mock.patch.object(build, 'build_environment', return_value={'CARGO_TARGET_DIR': 'quality'}), \
                    mock.patch.object(build, 'run', side_effect=quiet) as run, \
                    mock.patch.object(s3d_ci, 'prepare_tools') as prepare, mock.patch.object(s3d_ci, 'execute') as execute, \
                    mock.patch.object(s3d_ci, 'environment', return_value={'PATH': 'tools', 'DEV_WORKFLOW_S3D_PYTHON': 'python'}):
                build.quality(name, self.root)
                calls = [call for call in run.call_args_list if call.args[0] == unit]
                # build では道具を入れない(Mac の brew は build の時間の枠を越えるため。2026-11-12 12:59 の決定)。
                prepare.assert_not_called()
                execute.assert_not_called()
                if name.startswith('linux-'):
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0].kwargs['env'], {'CARGO_TARGET_DIR': 'quality', 'PATH': 'tools', 'DEV_WORKFLOW_S3D_PYTHON': 'python'})
                    self.assertNotIn('DEV_WORKFLOW_S3D_TYPED_REPORT', calls[0].kwargs['env'])
                else:
                    # Mac の単体検査は s3d ジョブの型検査の記録(同じ example の cargo test)が兼ねる。
                    self.assertEqual(calls, [])
        for name in ('macos-x86_64', 'macos-arm64'):
            with self.subTest(name=name), mock.patch.object(s3d_ci, 'environment') as environment, \
                    mock.patch.object(s3d_ci, 'execute') as execute, self.assertRaisesRegex(ValueError, 'Linux'):
                s3d_ci.unit_test(name, self.root, {}, run=mock.Mock())
            environment.assert_not_called()
            execute.assert_not_called()
        def failing(args, *, env=None, capture=False):
            if 'diff_snapshot_contract' in args:
                raise subprocess.CalledProcessError(1, 'unit')
            return '' if capture else None
        with mock.patch.object(build, 'build_environment', return_value={}), mock.patch.object(build, 'run', side_effect=failing), \
                mock.patch.object(s3d_ci, 'environment', return_value={'PATH': 'p', 'DEV_WORKFLOW_S3D_PYTHON': 'python'}):
            with self.assertRaises(subprocess.CalledProcessError):
                build.quality('linux-x86_64', self.root)
        # prepare は S3D の道具を入れない(S3D の道具は比較のジョブだけで入れる)。
        with mock.patch.object(build, 'target_config', return_value=('target', None)), mock.patch.object(build, 'run'), \
                mock.patch.object(s3d_ci, 'prepare_tools') as prepare:
            build.prepare('linux-x86_64')
            prepare.assert_not_called()

    def test_every_platform_runs_the_example_tests_in_the_comparison_job(self):
        # Mac の build では単体検査を流さないので、s3d ジョブの型検査の記録が 4 機種すべてで流れることを確かめる。
        for name in build.TARGETS:
            env = {'CARGO_TARGET_DIR': 'unused'}
            def run(argv, *, env):
                self.assertEqual(argv, ['cargo', 'test', '--locked', '-p', 'dev-workflow-cli', '--example', 'diff_snapshot_contract',
                                        '--', '--nocapture'])
                Path(env['DEV_WORKFLOW_S3D_TYPED_REPORT']).write_text('{}')
            with self.subTest(name=name), \
                    mock.patch.object(s3d_ci, 'environment', return_value={'PATH': 'tools', 'DEV_WORKFLOW_S3D_PYTHON': 'python'}):
                report = s3d_ci.typed_report(name, self.root, root=self.root, build_environment=lambda *_: dict(env), run=run)
                self.assertTrue(report.is_file())

    def test_ci_script_tests_include_the_s3d_tests(self):
        # policy ジョブの build.py test は、既存の CI のテストに加えてこのファイルも流す。
        suites = []
        with mock.patch.object(sys, 'argv', ['build.py', 'test']), \
                mock.patch.object(build.unittest, 'TextTestRunner') as runner:
            runner.return_value.run.side_effect = lambda suite: suites.append(suite) or mock.Mock(wasSuccessful=lambda: True)
            self.assertEqual(build.main(), 0)
        def ids(suite):
            for item in suite:
                yield from ids(item) if isinstance(item, unittest.TestSuite) else [item.id()]
        found = list(ids(suites[0]))
        self.assertTrue(any(name.startswith('test_s3d_ci.S3DCiTests.') for name in found))
        self.assertTrue(any(name.startswith('test_s3d_ci.S3DWorkflowTests.') for name in found))
        self.assertTrue(any(name.startswith('test_native_ci.') for name in found))

    def test_example_is_transferred_and_never_distributed(self):
        self.assertIn('diff_snapshot_contract',build.CI_BINARIES)
        self.assertIn('diff_snapshot_contract',build.EXAMPLES)
        self.assertIn('diff_snapshot_contract',build.UNIVERSAL_FILES)
        self.assertTrue({'1/diff_snapshot_contract','2/diff_snapshot_contract'}<=set(build.BUILD_FILES))
        self.assertFalse(any('diff_snapshot' in path for path in make_dist.BINARY_PATHS))

    def test_comparison_has_no_subset_and_binds_binary_and_typed_report(self):
        run=mock.Mock(side_effect=lambda argv,**kwargs:Path(argv[-1]).write_text(json.dumps(self.report())))
        with mock.patch.object(s3d_ci,'environment',return_value={'PATH':'fixture'}),mock.patch.object(s3d_ci.platform,'system',return_value='Linux'):
            s3d_ci.probe(self.root/'binary',self.root/'typed.json',self.root,root=self.root,run=run)
        argv=run.call_args.args[0];env=run.call_args.kwargs['env']
        self.assertEqual(argv[:3],[sys.executable,'-B',self.root/'native/ci/check_diff_snapshot.py'])
        self.assertNotIn('--only',argv)
        self.assertEqual(env['DEV_WORKFLOW_TEST_DIFF_SNAPSHOT_BINARY'],str(self.root/'binary'))
        self.assertEqual(env['DEV_WORKFLOW_S3D_TYPED_REPORT'],str(self.root/'typed.json'))
        self.assertEqual(run.call_args.kwargs['timeout'],3600)
        run.side_effect=subprocess.CalledProcessError(1,'S3D')
        with mock.patch.object(s3d_ci,'environment',return_value={}), self.assertRaises(subprocess.CalledProcessError):
            s3d_ci.probe(self.root/'binary',self.root/'typed.json',self.root,root=self.root,run=run)

    def test_typed_report_must_be_created_from_fresh_target(self):
        report=self.root/'s3d-typed.json';report.write_text('stale')
        def run(argv,*,env):
            self.assertFalse(report.exists())
            self.assertEqual(list(Path(env['CARGO_TARGET_DIR']).iterdir()),[])
            self.assertEqual(argv,['cargo','test','--locked','-p','dev-workflow-cli','--example','diff_snapshot_contract','--','--nocapture'])
            self.assertEqual(env['SOURCE_DATE_EPOCH'],'0')
        with mock.patch.object(s3d_ci,'environment',return_value={'PATH':'fixture','DEV_WORKFLOW_S3D_PYTHON':sys.executable}):
            with self.assertRaisesRegex(RuntimeError,'型検査の記録'):
                s3d_ci.typed_report('linux-x86_64',self.root,root=self.root,build_environment=lambda *_:{'SOURCE_DATE_EPOCH':'0'},run=run)

    def test_tool_preparation_does_not_install_into_git_configuration(self):
        with mock.patch.object(s3d_ci.shutil,'which',return_value='/usr/bin/tool'),mock.patch.object(s3d_ci,'execute') as execute:
            s3d_ci.prepare_tools('macos-arm64')
        self.assertEqual(execute.call_args_list,[mock.call(['brew','install','bash','grep','coreutils','findutils'])])
        # 比較のジョブのLinuxは、git-lfsとstraceが無ければ入れる。
        with mock.patch.object(s3d_ci.shutil,'which',side_effect=lambda tool:'/usr/bin/git' if tool=='git' else None),mock.patch.object(s3d_ci,'execute') as execute:
            s3d_ci.prepare_tools('linux-arm64')
            self.assertEqual(execute.call_args_list,[mock.call(['sudo','apt-get','update']),mock.call(['sudo','apt-get','install','-y','git-lfs','strace'])])
        with mock.patch.object(s3d_ci.shutil,'which',side_effect=['/usr/bin/git','/usr/bin/git-lfs','/other/git']),mock.patch.object(s3d_ci,'execute'),self.assertRaisesRegex(RuntimeError,'Gitの実体'):
            s3d_ci.prepare_tools('macos-arm64')


    def report(self, **changes):
        versions = {'bash': '5.2.21', 'coreutils': '9.4', 'findutils': '4.9.0', 'sed': '4.9', 'grep': '3.11', 'glibc': '2.39'}
        value = {'tool_versions': versions, 'tool_versions_base': True, 'not_run': [], 'version_failures': [],
                 'version_fields': {'review': {'review-a': ['tool-message']}}, 'version_dependent': ['review-a'],
                 'scope': 'full-external'}
        value.update(changes)
        return value

    def test_report_records_versions_and_linux_runs_on_the_base_versions(self):
        summary = s3d_ci.check_report(self.report(), 'Linux')
        self.assertEqual(summary['tool_versions_base'], True)
        self.assertEqual(summary['not_run'], {})
        for changes in ({'tool_versions_base': False}, {'not_run': [{'id': 'partial-out-file', 'reason': 'strace'}]},
                        {'version_failures': ['review-a']}, {'tool_versions': {'bash': '5.2.21'}},
                        {'scope': 'review'}, {'not_run': None}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                s3d_ci.check_report(self.report(**changes), 'Linux')
        bad = self.report()
        del bad['version_fields']
        with self.assertRaises(RuntimeError):
            s3d_ci.check_report(bad, 'Linux')

    def test_mac_records_version_fields_and_strace_cases_as_not_run(self):
        mac = self.report(tool_versions_base=False, tool_versions=dict(self.report()['tool_versions'], glibc=None),
                          not_run=[{'id': 'review-a', 'reason': 'version'}, {'id': 'partial-out-file', 'reason': 'strace'}])
        self.assertEqual(s3d_ci.check_report(mac, 'Darwin')['not_run'], {'version': 1, 'strace': 1})
        for changes in ({'not_run': [{'id': 'partial-out-file', 'reason': 'strace'}]},
                        {'not_run': [{'id': 'review-a', 'reason': 'version'}, {'id': 'x', 'reason': 'other'}]},
                        {'version_failures': ['review-a']}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                s3d_ci.check_report(dict(mac, **changes), 'Darwin')

    def test_mac_allows_repeated_known_reasons_and_rejects_any_unknown_reason(self):
        # 理由ごとの件数に上限は無い(同じ理由を何件でも記録できる)。許すのはMAC_NOT_RUNの理由だけ。
        mac = self.report(tool_versions_base=False, tool_versions=dict(self.report()['tool_versions'], glibc=None))
        ok = [{'id': 'review-a', 'reason': 'version'}, {'id': 'a', 'reason': 'strace'}, {'id': 'b', 'reason': 'strace'}]
        self.assertEqual(s3d_ci.check_report(dict(mac, not_run=ok), 'Darwin')['not_run'], {'version': 1, 'strace': 2})
        for extra in ({'id': 'x', 'reason': 'other'}, {'id': 'y', 'reason': None}):
            with self.subTest(extra=extra), self.assertRaises(RuntimeError):
                s3d_ci.check_report(dict(mac, not_run=[*ok, extra]), 'Darwin')

    def test_proc_cases_are_not_run_only_on_mac(self):
        # 別のプロセスの/proc/PID/memを使う2件は、/procの無いMacだけで未実施にできる。
        proc = [{'id': 'pr2-proc-pre-list', 'reason': 'proc'}, {'id': 'pr2-proc-tracked', 'reason': 'proc'}]
        mac = self.report(tool_versions_base=False, tool_versions=dict(self.report()['tool_versions'], glibc=None),
                          not_run=[{'id': 'review-a', 'reason': 'version'}, *proc])
        self.assertEqual(s3d_ci.check_report(mac, 'Darwin')['not_run'], {'version': 1, 'proc': 2})
        with self.assertRaises(RuntimeError):
            s3d_ci.check_report(self.report(not_run=proc), 'Linux')

    def test_probe_checks_the_written_report_and_limits_cover_two_comparisons(self):
        output = self.root / 's3d-bin-comparison.json'
        def run(argv, *, env, timeout):
            output.write_text(json.dumps(self.report()))
        with mock.patch.object(s3d_ci, 'environment', return_value={}), mock.patch.object(s3d_ci.platform, 'system', return_value='Linux'):
            s3d_ci.probe(self.root / 'bin/binary', self.root / 'typed.json', self.root, root=self.root, run=run)
            def stale(argv, *, env, timeout):
                self.assertFalse(output.exists())
                output.write_text(json.dumps(self.report(tool_versions_base=False)))
            with self.assertRaisesRegex(RuntimeError, '基準の版'):
                s3d_ci.probe(self.root / 'bin/binary', self.root / 'typed.json', self.root, root=self.root, run=stale)
        # 1回の上限は実測(いちばん遅い値)の2倍より長い。
        self.assertGreaterEqual(s3d_ci.COMPARISON_LIMIT_SECONDS, 2 * s3d_ci.MEASURED_SECONDS)
        # 外部の比較は上限つきで流し、時間切れは失敗にする。
        with mock.patch.object(s3d_ci.subprocess, 'run', side_effect=subprocess.TimeoutExpired('S3D', 1)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                s3d_ci.run_limited(['compare'], env={}, timeout=s3d_ci.COMPARISON_LIMIT_SECONDS)
        self.assertEqual(run.call_args.kwargs['timeout'], s3d_ci.COMPARISON_LIMIT_SECONDS)
        self.assertTrue(run.call_args.kwargs['check'])


class S3DWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.document = make_dist.read_json(source_hash.ROOT / '.github/workflows/native.yml')

    def jobs_running(self, document, operation):
        return [name for name, job in document['jobs'].items()
                if any(check_workflow.native_build_command(step.get('run')) == operation for step in job['steps'])]

    def test_comparison_jobs_are_separate_and_cover_every_platform(self):
        self.assertEqual(check_workflow.check_document(self.document), [])
        jobs = self.document['jobs']
        self.assertEqual(self.jobs_running(self.document, 's3d'), ['s3d'])
        self.assertEqual(self.jobs_running(self.document, 's3d-universal'), ['s3d-universal'])
        self.assertEqual(jobs['s3d']['needs'], 'build')
        self.assertEqual(jobs['s3d-universal']['needs'], 'combine')
        platforms = lambda job: {row['platform'] for row in jobs[job]['strategy']['matrix']['include']}
        self.assertEqual(platforms('s3d'), set(build.TARGETS))
        self.assertEqual(platforms('s3d-universal'), set(build.S3D_UNIVERSAL_PLATFORMS))
        self.assertEqual(jobs['s3d']['steps'][4]['with']['name'], 'native-build-${{ matrix.platform }}')
        self.assertEqual(jobs['s3d-universal']['steps'][4]['with']['name'], 'native-universal')
        # 既存の build と verify-universal の上限は変えない。
        self.assertEqual((jobs['build']['timeout-minutes'], jobs['verify-universal']['timeout-minutes']), (60, 30))

    def test_comparison_jobs_cannot_be_dropped_or_reduced(self):
        for job, platform_name in (('s3d', 'linux-arm64'), ('s3d', 'macos-x86_64'), ('s3d-universal', 'macos-arm64')):
            changed = copy.deepcopy(self.document)
            rows = changed['jobs'][job]['strategy']['matrix']['include']
            rows[:] = [row for row in rows if row['platform'] != platform_name]
            with self.subTest(job=job, platform=platform_name):
                self.assertTrue(any('S3D' in problem for problem in check_workflow.check_document(changed)))
        for job in ('s3d', 's3d-universal'):
            for mutation in ('remove-job', 'remove-step', 'fixed-platform', 'duplicate', 'option-first', 'timeout', 'python'):
                changed = copy.deepcopy(self.document)
                steps = changed['jobs'][job]['steps']
                if mutation == 'remove-job':
                    changed['jobs'].pop(job)
                elif mutation == 'remove-step':
                    steps.pop()
                elif mutation == 'fixed-platform':
                    steps[-1]['run'] = f'python3 -B native/ci/build.py {job} --platform linux-x86_64'
                elif mutation == 'duplicate':
                    changed['jobs'][job + '-again'] = copy.deepcopy(changed['jobs'][job])
                elif mutation == 'option-first':
                    steps[-1]['run'] = f"python3 -B native/ci/build.py --platform '${{{{ matrix.platform }}}}' {job}"
                elif mutation == 'timeout':
                    changed['jobs'][job]['timeout-minutes'] -= 1
                elif mutation == 'python':
                    steps.pop(2)
                with self.subTest(job=job, mutation=mutation):
                    self.assertTrue(check_workflow.check_document(changed))
        # build を流さないワークフロー(配布の確かめなど)には求めない。
        dist = make_dist.read_json(source_hash.ROOT / '.github/workflows/dist-check.yml')
        self.assertEqual(check_workflow.check_s3d_jobs(dist['jobs']), [])

    def test_linux_comparisons_run_on_the_base_versions(self):
        self.assertEqual(check_workflow.S3D_BASE_RUNNERS, {'linux-x86_64': 'ubuntu-24.04', 'linux-arm64': 'ubuntu-24.04-arm'})
        base = json.loads((source_hash.ROOT / 'native/ci/diff_snapshot_fixtures/launch_sites.json').read_text())['base_versions']
        self.assertEqual(base, {'bash': '5.2.21', 'coreutils': '9.4', 'findutils': '4.9.0', 'sed': '4.9',
                                'grep': '3.11', 'glibc': '2.39'})
        with mock.patch.dict(check_workflow.RUNNERS, {'linux-x86_64': 'ubuntu-22.04'}):
            changed = copy.deepcopy(self.document)
            for job in changed['jobs'].values():
                for row in job.get('strategy', {}).get('matrix', {}).get('include', []):
                    if row['platform'] == 'linux-x86_64':
                        row['runner'] = 'ubuntu-22.04'
            self.assertTrue(any('基準の版' in problem for problem in check_workflow.check_document(changed)))

    def test_job_limits_cover_the_inner_limits_of_every_comparison(self):
        # 上限 = 比較 1 回の上限 × 回数 + 準備。内側の上限(時間切れは失敗)で先に止まる。
        self.assertEqual(set(check_workflow.S3D_TIMEOUTS), set(s3d_ci.JOB_COMPARISONS))
        for job, count in s3d_ci.JOB_COMPARISONS.items():
            minutes = self.document['jobs'][job]['timeout-minutes']
            self.assertEqual(minutes, check_workflow.S3D_TIMEOUTS[job])
            self.assertGreaterEqual(minutes * 60, count * s3d_ci.COMPARISON_LIMIT_SECONDS + s3d_ci.JOB_OVERHEAD_SECONDS)
            self.assertLessEqual(minutes, 360)


if __name__=='__main__':unittest.main()
