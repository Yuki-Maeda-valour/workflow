"""#260: 保存・本文非読取・起動環境・確定時刻の独立した境界回帰。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest

from diagnostic_test_support import DiagnosticFixture, DIAGNOSTIC, SCRIPTS, inventory


# 注入は試験プロセス内だけ。公開製品にhook/envを追加しない。
DRIVER = r'''
import builtins,contextlib,importlib.util,io,json,os,pathlib,signal,stat,sys
from unittest import mock
helper,phase,report,target,arguments=sys.argv[1:]
spec=importlib.util.spec_from_file_location('diagnostic_entry',helper)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
args=json.loads(arguments); events=[]
original_open=os.open; original_write=os.write; original_read=os.read; original_fsync=os.fsync
original_builtin=builtins.open; original_io=io.open
with contextlib.ExitStack() as patches:
 if phase in ('write-error','write-zero','fsync-file','fsync-parent','hash-mismatch','publish-control'):
  original_publish=m.P.publish
  def publish(plan,out,state):
   fd_target=[None]; corrupted=[False]
   def opened(path,flags,*a,**kw):
    fd=original_open(path,flags,*a,**kw)
    if os.fsencode(path)==os.fsencode(os.path.basename(out)) and flags & os.O_CREAT:
     fd_target[0]=fd;events.append('created')
    return fd
   def written(fd,data):
    if fd==fd_target[0]:
     events.append('write')
     if phase=='write-error':raise OSError('fixture write failure')
     if phase=='write-zero':return 0
    return original_write(fd,data)
   def synced(fd):
    kind='file' if fd==fd_target[0] else 'parent';events.append('fsync-'+kind)
    if phase=='fsync-'+kind:raise OSError('fixture fsync failure')
    return original_fsync(fd)
   def read(fd,count):
    data=original_read(fd,count)
    if fd==fd_target[0] and data:
     events.append('same-fd-read')
     if phase=='hash-mismatch' and not corrupted[0]:
      corrupted[0]=True;return bytes([data[0]^1])+data[1:]
    return data
   with mock.patch.object(os,'open',side_effect=opened),mock.patch.object(os,'write',side_effect=written),mock.patch.object(os,'fsync',side_effect=synced),mock.patch.object(os,'read',side_effect=read):
    return original_publish(plan,out,state)
  patches.enter_context(mock.patch.object(m.P,'publish',side_effect=publish))
 elif phase=='body-recorder':
  identity=os.stat(target); original_check=m.R.Runtime.check_runtime
  def is_target(path,kw):
   if isinstance(path,int):return False
   try:s=os.stat(path,dir_fd=kw.get('dir_fd'),follow_symlinks=False)
   except (OSError,TypeError):return False
   return (s.st_dev,s.st_ino)==(identity.st_dev,identity.st_ino)
  def opened(path,flags,*a,**kw):
   if is_target(path,kw) and not flags & os.O_DIRECTORY:events.append('os-open-body')
   return original_open(path,flags,*a,**kw)
  def builtin(path,*a,**kw):
   if is_target(path,{}):events.append('builtins-open-body')
   return original_builtin(path,*a,**kw)
  def io_open(path,*a,**kw):
   if is_target(path,{}):events.append('io-open-body')
   return original_io(path,*a,**kw)
  def checked(runtime):
   original_check(runtime)
   patches.enter_context(mock.patch.object(os,'open',side_effect=opened))
   patches.enter_context(mock.patch.object(builtins,'open',side_effect=builtin))
   patches.enter_context(mock.patch.object(io,'open',side_effect=io_open))
  patches.enter_context(mock.patch.object(m.R.Runtime,'check_runtime',checked))
 elif phase.startswith('missing-'):
  name=phase[8:]
  if name in ('git','grep'):
   original_which=m.R.shutil.which
   patches.enter_context(mock.patch.object(m.R.shutil,'which',side_effect=lambda tool:None if tool==name else original_which(tool)))
  elif name=='fd-scandir':patches.enter_context(mock.patch.object(os,'supports_fd',set()))
  elif name=='nofollow':
   patches.enter_context(mock.patch.object(os,'O_NOFOLLOW',None));del os.O_NOFOLLOW
  elif name=='mask':patches.enter_context(mock.patch.object(signal,'pthread_sigmask',None))
  elif name=='dirfd':patches.enter_context(mock.patch.object(os,'supports_dir_fd',set()))
 rc=m.main(args)
with original_builtin(report,'w') as out:json.dump({'rc':rc,'events':events},out)
sys.exit(rc)
'''

MASK_DRIVER = r'''
import importlib.util,json,pathlib,signal,sys
runtime,writer,report,phase=sys.argv[1:]
spec=importlib.util.spec_from_file_location('diagnostic_runtime',runtime)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
now=[0.0];r=m.Runtime(m.Budget(clock=lambda:now[0],seconds=1));original=signal.pthread_sigmask
masks=[]
def mask(how,signals):
 result=original(how,signals);masks.append(how)
 if how==signal.SIG_BLOCK:now[0]=1.0 if phase=='expired' else .999
 return result
m.signal.pthread_sigmask=mask
rc=r.emit(b'{"complete":true}\n',0,writer)
pathlib.Path(report).write_text(json.dumps({'rc':rc,'children':[p.poll() for p in r.children], 'masks':masks}))
sys.exit(rc)
'''


def fifo_inventory(root):
    """自作fixtureのFIFOを開かず、bytes/modeの不変性だけ確認。"""
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        key = os.fsencode(path.relative_to(root))
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            result[key] = ('regular', mode, path.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            result[key] = ('directory', mode)
        elif stat.S_ISFIFO(info.st_mode):
            result[key] = ('fifo', mode)
        elif stat.S_ISLNK(info.st_mode):
            result[key] = ('symlink', mode, os.fsencode(os.readlink(path)))
        else:
            raise AssertionError('unexpected fixture type')
    return result


class DiagnosticIOBoundaries(DiagnosticFixture):
    def injected(self, phase, args, target=''):
        report = self.work / 'injection-report.json'
        result = subprocess.run([sys.executable, '-I', '-B', '-c', DRIVER,
            str(DIAGNOSTIC), phase, str(report), str(target), json.dumps(list(map(str, args)))],
            env=self.env, cwd=self.work, capture_output=True, timeout=30)
        self.assertTrue(report.is_file(), result.stderr)
        details = json.loads(report.read_text())
        self.assertEqual(details['rc'], result.returncode)
        return result, details

    def test_v03_publish_failures_keep_stdout_empty_and_originals_unchanged(self):
        self.begin()
        before = inventory(self.repo), inventory(self.state), self.latest.read_bytes()
        for phase in ('write-error', 'write-zero', 'fsync-file', 'fsync-parent', 'hash-mismatch', 'publish-control'):
            with self.subTest(phase=phase):
                out = self.plan_dir / (phase + '.json')
                args = ['extend-plan', *self.common, '--plan', self.latest,
                        '--plan-sha256', self.latest_hash, '--out', out, 'refs']
                result, report = self.injected(phase, args)
                self.assertIn('created', report['events'])
                if phase == 'publish-control':
                    response = self.success(result, 'plan-extended')
                    self.assertEqual(response['plan_sha256'], hashlib.sha256(out.read_bytes()).hexdigest())
                    self.assertIn('fsync-file', report['events'])
                    self.assertIn('same-fd-read', report['events'])
                    self.assertIn('fsync-parent', report['events'])
                else:
                    self.failure(result, 20)
                    self.assertNotIn(b'fixture', result.stderr)
                    if phase == 'hash-mismatch':
                        self.assertIn('same-fd-read', report['events'])
                self.assertEqual((inventory(self.repo), inventory(self.state), self.latest.read_bytes()), before)

    def test_v12_files_never_opens_selected_body_and_status_control_does(self):
        self.begin()
        target = self.repo / 'seed.txt'
        before = inventory(self.repo), inventory(self.state)
        self.extend('files', '--path', 'seed.txt')
        args = ['run', *self.common, '--plan', self.latest, '--plan-sha256', self.latest_hash]
        result, report = self.injected('body-recorder', args, target)
        response = self.success(result, 'observed')
        endpoint = self.records(response)[b'seed.txt']['endpoint']
        self.assertEqual(endpoint['kind'], 'regular')
        self.assertEqual(endpoint['size'], len(b'public seed\n'))
        self.assertIsNone(endpoint['sha256'])
        self.assertEqual(report['events'], [])
        self.extend('status', '--path', 'seed.txt')
        result, report = self.injected('body-recorder',
            ['run', *self.common, '--plan', self.latest, '--plan-sha256', self.latest_hash], target)
        self.success(result, 'observed')
        self.assertIn('os-open-body', report['events'], 'recorder正常対照が本文openを検知しない')
        self.assertEqual((inventory(self.repo), inventory(self.state)), before)

    def test_v02_snapshot_fifo_is_rejected_without_opening_it(self):
        self.begin()
        target = self.state / 'snapshot/4-meta.txt'
        original = target.read_bytes(); mode = stat.S_IMODE(target.stat().st_mode)
        target.unlink(); os.mkfifo(target, mode)
        try:
            before = fifo_inventory(self.state), inventory(self.repo)
            result = subprocess.run([sys.executable, '-I', '-B', str(DIAGNOSTIC), 'run',
                *self.common, '--plan', str(self.latest), '--plan-sha256', self.latest_hash],
                cwd=self.work, env=self.env, capture_output=True, timeout=10)
            self.failure(result, 23, 'not-regular')
            self.assertEqual((fifo_inventory(self.state), inventory(self.repo)), before)
        finally:
            target.unlink(); target.write_bytes(original); target.chmod(mode)
        self.observe('refs')

    def test_v01_missing_runtime_apis_git_and_grep_stop_before_observation(self):
        self.begin()
        before = inventory(self.repo), inventory(self.state), self.latest.read_bytes()
        args = ['extend-plan', *self.common, '--plan', self.latest, '--plan-sha256',
                self.latest_hash, '--out', self.plan_dir / 'uncreated.json', 'refs']
        for phase in ('missing-fd-scandir', 'missing-dirfd', 'missing-nofollow', 'missing-mask', 'missing-git', 'missing-grep'):
            with self.subTest(phase=phase):
                result, _ = self.injected(phase, args)
                self.failure(result, 20, 'runtime-unavailable')
                self.assertFalse((self.plan_dir / 'uncreated.json').exists())
                self.assertEqual((inventory(self.repo), inventory(self.state), self.latest.read_bytes()), before)
        self.observe('refs')

    def test_v01_same_named_cwd_and_pythonpath_modules_are_not_executed(self):
        self.begin()
        self.extend('refs')
        marker = self.work / 'unexpected-module-marker'
        poison = 'from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("harmless marker")\n'
        untrusted = self.work / 'untrusted'
        untrusted.mkdir()
        names = [p.name for p in SCRIPTS.glob('diagnostic-*.py')] + ['secret-profiles.py', 'implementation-git.py']
        for name in names:
            (self.work / name).write_text(poison)
            (untrusted / name).write_text(poison)
            (untrusted / name.replace('-', '_')).write_text(poison)
        env = dict(self.env, PYTHONPATH=str(untrusted))
        before = inventory(self.repo), inventory(self.state)
        response = self.success(self.cli('run', *self.common, '--plan', self.latest,
                                '--plan-sha256', self.latest_hash, env=env), 'observed')
        self.assertEqual(response['data']['variant'], 'refs-v1')
        self.assertFalse(marker.exists())
        self.assertEqual((inventory(self.repo), inventory(self.state)), before)

    def test_v01_missing_sibling_helper_is_runtime_failure_without_traceback(self):
        self.begin()
        trusted = self.work / 'trusted-copy'
        trusted.mkdir()
        for source in SCRIPTS.glob('*.py'):
            if source.name != 'diagnostic-paths.py':
                shutil.copyfile(source, trusted / source.name)
        before = inventory(self.repo), inventory(self.state)
        result = subprocess.run([sys.executable, '-I', '-B', str(trusted / 'diagnostic-git.py'),
            'run', *self.common, '--plan', str(self.latest), '--plan-sha256', self.latest_hash],
            env=self.env, cwd=self.work, capture_output=True, timeout=10)
        self.assertEqual((inventory(self.repo), inventory(self.state)), before)
        self.failure(result, 20, 'runtime-unavailable')

    def test_v01_runtime_output_and_other_helper_load_failures_stop_without_fallback(self):
        self.begin()
        before = inventory(self.repo), inventory(self.state)
        for name in ('diagnostic-runtime.py', 'diagnostic-output.py', 'diagnostic-views.py'):
            for failure in ('missing', 'syntax-error'):
                with self.subTest(helper=name, failure=failure):
                    trusted = self.work / (name + '-' + failure)
                    trusted.mkdir()
                    for source in SCRIPTS.glob('*.py'):
                        if source.name != name:
                            shutil.copyfile(source, trusted / source.name)
                    if failure == 'syntax-error':
                        (trusted / name).write_text('def broken(:\n')
                    result = subprocess.run([sys.executable, '-I', '-B', str(trusted / 'diagnostic-git.py'),
                        'run', *self.common, '--plan', str(self.latest), '--plan-sha256', self.latest_hash],
                        env=self.env, cwd=self.work, capture_output=True, timeout=10)
                    self.assertEqual((inventory(self.repo), inventory(self.state)), before)
                    self.assertEqual(result.returncode, 20, result.stderr)
                    self.assertEqual(result.stdout, b'')
                    self.assertNotIn(b'Traceback', result.stderr)
                    self.assertNotIn(b'broken', result.stderr)
                    if name in ('diagnostic-runtime.py', 'diagnostic-output.py'):
                        self.assertEqual(result.stderr, b'', '監督/送出不能時に代替同期出力をしない')
                    else:
                        self.failure(result, 20, 'runtime-unavailable')
        self.observe('refs')

    def test_v27_deadline_reached_inside_confirmation_mask_never_commits_success(self):
        for phase, expected in [('control', 0), ('expired', 20)]:
            with self.subTest(phase=phase):
                report = self.work / ('mask-' + phase + '.json')
                result = subprocess.run([sys.executable, '-I', '-B', '-c', MASK_DRIVER,
                    str(SCRIPTS / 'diagnostic-runtime.py'), str(SCRIPTS / 'diagnostic-output.py'),
                    str(report), phase], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(result.stdout, b'{"complete":true}\n')
                self.assertEqual(result.stderr, b'')
                details = json.loads(report.read_text())
                self.assertEqual(details['rc'], expected)
                self.assertEqual(details['children'], [0])
                self.assertEqual(len(details['masks']), 2)


if __name__ == '__main__':
    unittest.main()
