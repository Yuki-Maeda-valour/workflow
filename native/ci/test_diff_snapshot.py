#!/usr/bin/env python3
"""S3D比較器の意図的な不具合を検出する。Rustビルドは要求しない。"""
import dataclasses
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import check_diff_snapshot as subject
from diff_snapshot_fixtures import legacy_adapter
from diff_snapshot_fixtures.diagnostics import SOURCE_LINES, INTERNAL, SURROGATE_FRAMES, APPROVED_DIFFERENCES, bash_locations, compare_surrogate, compare_unknown_codec, thread_field, typed_report
from diff_snapshot_fixtures.compare import AuditEvent, Result, audit_reads, binary_path, exact_ids, fixed, report_time, same_result, unchanged
from diff_snapshot_fixtures.read_audit import parse_trace


class Manifest(unittest.TestCase):
    def test_all_inputs_and_individual_moved_assertions_are_fixed(self):
        data = subject.manifest()
        self.assertEqual(71, len(data['groups']))
        self.assertEqual(342, len(data['vectors']))
        self.assertEqual(225, sum(item['executor'] == 'external' for item in data['vectors']))
        self.assertEqual(117, sum(item['executor'] == 'typed' for item in data['vectors']))
        self.assertEqual(47, len(data['typed_assertions']))

    def test_same_count_missing_duplicate_and_replacement_rejected(self):
        exact_ids(['a', 'b'], ['b', 'a'])
        for actual in ([], ['a'], ['a', 'a'], ['a', 'c'], ['a', 'b', 'c']):
            with self.subTest(actual=actual), self.assertRaises(AssertionError):
                exact_ids(['a', 'b'], actual)
        with self.assertRaises(AssertionError):
            exact_ids(['a', 'a'], ['a'])

    def test_manifest_mutations_fail_before_execution(self):
        value = subject.manifest()
        original=Path.read_text
        for change in ('drop', 'replace', 'approve', 'source', 'moved', 'group','additional','fd','output','coverage','legacy-id','typed-extra','entry','review','partial'):
            bad = json.loads(json.dumps(value))
            if change == 'drop':
                bad['vectors'].pop()
            elif change == 'replace':
                bad['vectors'][0]['expected'] = 1
            elif change == 'approve':
                bad['approved_differences'] = ['anything']
            elif change == 'source':
                bad['source_sha256'] = '0' * 64
            elif change == 'moved':
                bad['typed_assertions'].pop()
            elif change=='group':
                bad['groups'][0]['id'] = bad['groups'][1]['id']
            elif change=='additional':bad['additional_external'].pop()
            elif change=='fd':bad['initial_fds'].pop()
            elif change=='output':bad['output_faults'][0]['code']=0
            elif change=='coverage':bad['groups'][0]['case_ids'].pop()
            elif change=='legacy-id':bad['legacy_assertion_ids'][0]='legacy-9999-01'
            elif change=='typed-extra':bad['typed_extra_required'].pop()
            elif change=='entry':bad['entry_signals'].pop()
            elif change=='review':bad['review_cases'].pop()
            elif change=='partial':bad['partial_outputs'][0]['size']+=1
            def read(path,*args,**kwargs):
                return json.dumps(bad) if path==subject.FIXTURES/'cases.json' else original(path,*args,**kwargs)
            with self.subTest(change=change), mock.patch.object(Path,'read_text',autospec=True,side_effect=read), self.assertRaises(AssertionError):
                subject.manifest()

    def test_binary_missing_empty_relative_directory_nonexecuting(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            entry = base / 'binary'
            entry.write_bytes(b'#!/bin/sh\nexit 0\n')
            for path in (None, '', 'relative', str(base), str(base / 'missing'), str(entry)):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    binary_path(path)
            entry.chmod(0o700)
            self.assertEqual(entry, binary_path(str(entry)))


class Comparison(unittest.TestCase):
    def setUp(self):
        self.time = datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc).timestamp()
        report = '# diff\n- 生成日時: 2026-11-11 00:00:00\n\n## 追跡差分\nbody\n'.encode()
        self.good = Result(0, b'out\0', b'err\xff', report, b'patch\xff', self.time, self.time + 1)

    def test_every_output_field_is_compared_as_bytes(self):
        same_result(self.good, self.good)
        for changes in ({'returncode': 1}, {'stdout': b'out\n'}, {'stderr': b'err'},
                        {'report': self.good.report.replace(b'body', b'other')}, {'patch': b'other'},
                        {'report': None}, {'timed_out': True}):
            with self.subTest(changes=list(changes)), self.assertRaises(AssertionError):
                same_result(self.good, dataclasses.replace(self.good, **changes))

    def test_same_wrong_results_do_not_satisfy_fixed_expectation(self):
        for bad in (Result(20, b'', b''), Result(-15, b'', b''), Result(0, b'bad', b'')):
            with self.assertRaises(AssertionError):
                fixed(bad, 0, stdout=b'', stderr=b'')

    def test_timestamp_only_header_once_and_inside_execution_window(self):
        later = dataclasses.replace(self.good, report=self.good.report.replace(b'00:00:00', b'00:00:01'))
        same_result(self.good, later)
        values = (self.good.report.replace(b'00:00:00', b'00:00:02'),
                  self.good.report.replace(b'00:00:00', b'invalid'),
                  self.good.report.replace(b'\n## ', b'\n- generated: missing\n## ').replace('- 生成日時'.encode(), b'- other'),
                  self.good.report.replace(b'\n## ', '\n- 生成日時: 2026-11-11 00:00:00\n## '.encode()))
        for report in values:
            with self.assertRaises(AssertionError):
                report_time(report, self.time, self.time + 1)
        # 本文内の同じ文字列は正規化しない。
        a = dataclasses.replace(self.good, report=self.good.report + b'00:00:00\n')
        b = dataclasses.replace(a, report=a.report[:-9] + b'00:00:01\n')
        with self.assertRaises(AssertionError):
            same_result(a, b)

    def test_secrets_are_rejected_before_any_normalization(self):
        marker = b'RAW_SECRET'
        for field in ('stdout', 'stderr', 'report', 'patch'):
            bad = dataclasses.replace(self.good, **{field: getattr(self.good, field) + marker})
            for old, new in ((self.good, bad), (bad, bad)):
                with self.subTest(field=field), self.assertRaises(AssertionError):
                    same_result(old, new, markers=(marker,))

    def test_input_inode_mode_times_and_content_are_not_normalized(self):
        good = (1, 2, 0o100644, 4, 1234567891, 1234567892, b'bytes')
        unchanged(good, good)
        for index in range(len(good)):
            bad = list(good)
            bad[index] = b'other' if index == 6 else bad[index] + 1
            with self.assertRaises(AssertionError):
                unchanged(good, tuple(bad))


class DiagnosticBoundaries(unittest.TestCase):
    def traceback(self, helper=b'/scratch/secret-profiles.py'):
        result = b'Traceback (most recent call last):\n'
        for (line, name), source in zip(SURROGATE_FRAMES, SOURCE_LINES):
            result += b'  File "' + helper + f'", line {line}, in {name}\n'.encode() + source
        return result + b"UnicodeEncodeError: 'utf-8' codec can't encode character '\\ud800' in position 0: surrogates not allowed\n" + INTERNAL

    def test_only_three_approved_paths_are_mapped(self):
        old = Result(20, b'', self.traceback())
        new = dataclasses.replace(old, stderr=self.traceback(b'secret-profiles.py'))
        compare_surrogate(old, new, helper='/scratch/secret-profiles.py')
        for mutated in (old.stderr.replace(b'/scratch/secret-profiles.py', b'elsewhere', 1),
                        old.stderr + b'/scratch/secret-profiles.py',
                        old.stderr.replace(b'UnicodeEncodeError', b'ValueError'),
                        old.stderr.replace(b'line 279', b'line 280', 1),
                        old.stderr.replace(b'in main', b'in other', 1)):
            with self.assertRaises(AssertionError):
                compare_surrogate(dataclasses.replace(old, stderr=mutated), new, helper='/scratch/secret-profiles.py')
        for mutated in (new.stderr.replace(b'position 0', b'position 1'),
                        new.stderr.replace(b'ud800', b'ud801'),
                        new.stderr.replace(b'UnicodeEncodeError', b'ValueError'),
                        new.stderr + b'extra\n'):
            with self.assertRaises(AssertionError):
                compare_surrogate(old, dataclasses.replace(new, stderr=mutated), helper='/scratch/secret-profiles.py')
        for change in ({'returncode': 1}, {'stdout': b'x'}, {'report': b''}, {'patch': b''}):
            with self.assertRaises(AssertionError):
                compare_surrogate(old, dataclasses.replace(new, **change), helper='/scratch/secret-profiles.py')

    def test_thread_only_one_exact_field_and_all_other_bytes(self):
        message = b"Fatal Python error: init_stdio_encoding: failed to get the Python codec name of the stdio encoding\nPython runtime state: core initialized\nLookupError: unknown encoding: no-such-codec\n\nCurrent thread 0x00007fff12345678 (most recent call first):\n  <no Python frame>\n" + INTERNAL
        old = Result(20, b'', message)
        new = dataclasses.replace(old, stderr=message.replace(b'7fff12345678', b'7abc98765432'))
        compare_unknown_codec(old, new)
        for bad in (message.replace(b'00007fff12345678', b'0'),
                    message.replace(b'LookupError', b'ValueError'),
                    message.replace(b'<no Python frame>', b'<changed Python frame>'),
                    message.replace(b'\n\nCurrent thread', b'\nCurrent thread'),
                    message + b'Current thread 0x00007fff12345678\n'):
            with self.assertRaises(AssertionError):
                thread_field(bad)
        with self.assertRaises(AssertionError):
            compare_unknown_codec(old, dataclasses.replace(new, stderr=new.stderr.replace(b'no-such-codec', b'other-codec')))

    def test_typed_report_binds_source_manifest_and_exact_ids(self):
        good = {'schema': 1, 'source_sha256': '1'*64, 'manifest_sha256': '2'*64, 'passed': ['case-one']}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'report.json'
            def check(value):
                path.write_text(json.dumps(value))
                return typed_report(path, source_sha256='1'*64, manifest_sha256='2'*64, expected_ids=['case-one'])
            self.assertEqual(good, check(good))
            for key, value in (('source_sha256', '0'*64), ('manifest_sha256', '0'*64), ('passed', []),
                               ('passed', ['case-one']*2), ('schema', 2)):
                with self.assertRaises(AssertionError):
                    check(dict(good, **{key: value}))

    def test_bash_location_only_fixed_header_and_all_other_paths_preserved(self):
        invocation=b'/fixture/diff-snapshot.sh'
        detail=b'printf: write error: Broken pipe'
        rows=[{'position':0,'line':567,'detail':detail}]
        old=invocation+b': line 567: '+detail+b'\nERROR [internal] line 567\n'
        expected=b'diff-snapshot.sh: line 567: '+detail+b'\nERROR [internal] line 567\n'
        self.assertEqual(expected,bash_locations(old,invocation=invocation,rows=rows))
        for bad in (old.replace(b'line 567',b'line 568',1),old.replace(b'printf',b'echo',1),
                    old+old.splitlines(keepends=True)[0],old.splitlines(keepends=True)[1],
                    b'prefix\n'+old,old.replace(b'Broken pipe',b'Bad file descriptor')):
            with self.assertRaises(AssertionError):
                bash_locations(bad,invocation=invocation,rows=rows)
        # 本文に同じ字面があっても消さない。入力の所在地ではない。
        suffix=b'target file: '+invocation+b'\n'
        self.assertEqual(expected+suffix,bash_locations(old+suffix,invocation=invocation,rows=rows))
        with self.assertRaises(AssertionError):
            same_result(Result(20,b'',expected+suffix),Result(20,b'',expected+suffix.replace(invocation,b'diff-snapshot.sh')))


class ReadAudit(unittest.TestCase):
    def setUp(self):
        self.git = b'/usr/bin/git'
        self.secret = b'/fixture/.env'
        self.call = AuditEvent('enumerate', 'Spawn', child_pid=20, executable=self.git,
                               argv=(b'git', b'diff', b'--name-only', b'HEAD'))
        self.open = AuditEvent('enumerate', 'Open', path=self.secret, fd=5, executable=self.git)
        self.read = dataclasses.replace(self.open, operation='Read', byte_count=8)
        self.good = [self.call, self.open, self.read]

    def check(self, new):
        audit_reads(self.good, new, {self.secret}, git_executables={self.git})

    def test_control_and_added_rust_open_read_hash_are_rejected(self):
        self.check(self.good)
        for op in ('Open', 'Read', 'Hash', 'Map'):
            mutant = AuditEvent('classified', op, path=self.secret, byte_count=8,
                                executable=b'/binary/diff_snapshot_contract')
            with self.subTest(op=op), self.assertRaises(AssertionError):
                self.check(self.good + [mutant])

    def test_extra_git_comparison_order_argument_and_targets_rejected(self):
        for mutant in (self.good + [self.call],
                       [dataclasses.replace(self.call, argv=self.call.argv + (b'.env',)), self.open, self.read],
                       [self.call, self.open, self.read, self.read],
                       [self.call, self.open, dataclasses.replace(self.read, byte_count=9)],
                       [self.call, self.open, dataclasses.replace(self.read, phase='after-classification')]):
            with self.assertRaises(AssertionError):
                self.check(mutant)
        second = dataclasses.replace(self.call, argv=(b'git', b'diff', b'--stat'))
        with self.assertRaises(AssertionError):
            audit_reads([self.call, second], [second, self.call], {self.secret}, git_executables={self.git})

    def test_alias_of_secret_inode_and_unknown_operations_rejected(self):
        alias = AuditEvent('classified', 'Read', path=b'/different/path', device=1, inode=2,
                           byte_count=8, executable=b'/binary/diff_snapshot_contract')
        with self.assertRaises(AssertionError):
            audit_reads([], [alias], {(1, 2)}, git_executables={self.git})
        with self.assertRaises(AssertionError):
            self.check([dataclasses.replace(self.open, operation='Unknown')])
        with self.assertRaises(AssertionError):
            audit_reads([], [], set(), git_executables={self.git})

    def test_trace_fd_lifetime_dup_read_and_no_buffer_logging(self):
        trace = b'''10 execve("/usr/bin/git", ["git", "diff", "--name-only"], 0x123 /* vars */) = 0
10 openat(AT_FDCWD</fixture>, ".env", O_RDONLY) = 3</fixture/.env>
10 dup(3</fixture/.env>) = 5</fixture/.env>
10 close(3</fixture/.env>) = 0
10 read(0x5, 0x123456, 0x1000) = 0x8
10 close(5</fixture/.env>) = 0
10 read(0x5, 0x123456, 0x1000) = -1 EBADF (Bad file descriptor)
10 +++ exited with 0 +++
'''
        parse = lambda raw: parse_trace(raw, cwd=b'/fixture', secret_identities={self.secret: (1, 2)}, git_executables={self.git})
        events = parse(trace)
        self.assertEqual(['Spawn', 'Open', 'Read'], [event.operation for event in events])
        self.assertEqual(8, events[-1].byte_count)
        self.assertEqual(5, events[-1].fd)
        for bad in (trace.replace(b'read(0x5, 0x123456', b'read(0x5, "SECRET"', 1),
                    trace.replace(b'["git", "diff", "--name-only"]', b'["git", ...]'),
                    trace + b'10 read(0x5, 0x123, 0x1000 <unfinished ...>\n'):
            with self.assertRaises(AssertionError):
                parse(bad)

    def test_trace_relative_failed_open_non_git_and_mmap(self):
        trace = b'''10 execve("/binary/diff_snapshot_contract", ["diff_snapshot_contract"], 0x123 /* vars */) = 0
10 openat(AT_FDCWD</fixture>, ".env", O_RDONLY) = -1 EACCES (Permission denied)
11 execve("/usr/bin/git", ["git", "diff", "--name-only"], 0x123 /* vars */) = 0
11 openat(AT_FDCWD</fixture>, ".env", O_RDONLY) = 3</fixture/.env>
11 mmap(NULL, 4096, PROT_READ, MAP_PRIVATE, 3</fixture/.env>, 0) = 0x123
11 +++ exited with 0 +++
10 +++ exited with 0 +++
'''
        events = parse_trace(trace, cwd=b'/elsewhere', secret_identities={self.secret: (1, 2)}, git_executables={self.git})
        self.assertEqual(['Spawn', 'Open', 'Open', 'Map'], [event.operation for event in events])
        self.assertEqual(4096, events[-1].byte_count)
        with self.assertRaises(AssertionError):
            audit_reads(self.good, events, {self.secret}, git_executables={self.git})


    def test_clone_inheritance_before_parent_resume_fchdir_and_alias_inode(self):
        trace = b'''10 execve("/usr/bin/git", ["git", "diff"], 0x123 /* vars */) = 0
10 openat(AT_FDCWD</fixture>, "/fixture", O_RDONLY|O_DIRECTORY) = 4</fixture>
10 fchdir(4</fixture>) = 0
10 openat(AT_FDCWD</fixture>, "alias", O_RDONLY) = 3</fixture/alias>
10 clone(child_stack=NULL, flags=SIGCHLD <unfinished ...>
11 execve("/binary/mutant", ["mutant"], 0x123 /* vars */) = 0
11 dup(3</fixture/alias>) = 5</fixture/alias>
11 close(3</fixture/alias>) = 0
11 read(0x5, 0x123456, 0x1000) = 0x8
10 <... clone resumed>) = 11
11 +++ exited with 0 +++
10 +++ exited with 0 +++
'''
        events = parse_trace(trace, cwd=b'/elsewhere', secret_identities={self.secret: (1, 2), b'/fixture/alias': (1, 2)}, git_executables={self.git})
        reads = [event for event in events if event.operation == 'Read']
        self.assertEqual(1, len(reads))
        self.assertEqual(self.secret, reads[0].path)
        self.assertEqual((1, 2), (reads[0].device, reads[0].inode))
        self.assertEqual(b'/binary/mutant', reads[0].executable)
        with self.assertRaises(AssertionError):
            audit_reads(self.good, events, {self.secret, (1, 2)}, git_executables={self.git})


class LegacyAdapter(unittest.TestCase):
    def setUp(self):
        self.source = legacy_adapter.verified_source(subject.SELFTEST)

    def test_sha_and_unique_boundaries_are_required(self):
        with self.assertRaises(AssertionError):
            legacy_adapter.adapt(self.source + '\n', result_log='/tmp/not-written')
        first = legacy_adapter.MOVED_BLOCKS[0][1]
        with self.assertRaises(AssertionError):
            legacy_adapter.block_ranges(self.source + first + '\n')
        with self.assertRaises(AssertionError):
            legacy_adapter.block_ranges(self.source.replace(first, '# absent'))

    def test_copy_preserves_line_count_and_unmoved_assertions(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = legacy_adapter.adapt(self.source, result_log=Path(temporary) / 'results')
            self.assertEqual(4022, len(output.splitlines()))
            removed = {line for _, start, end in legacy_adapter.block_ranges(self.source) for line in range(start, end)}
            for number, (old, new) in enumerate(zip(self.source.splitlines(), output.splitlines())):
                if number not in removed and old.startswith(('ckt ', 'ckf ', 'ckeq ', 'ckne ')):
                    self.assertEqual(old, new)
            path = Path(temporary) / 'adapter.sh'
            path.write_text(output)
            self.assertEqual(0, subprocess.run(['bash', '-n', str(path)], check=False).returncode)

    def test_wrapper_passes_raw_arguments_and_same_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / 'target.py'
            target.write_text('import os,sys\nprint(os.getpid())\nsys.stdout.buffer.write(b"\\0".join(os.fsencode(x) for x in sys.argv[1:])+b"\\0")\n')
            wrapper = base / 'wrapper.sh'
            log = base / 'pids'
            legacy_adapter.wrapper(wrapper, [sys.executable, str(target)], pid_log=log)
            args = [b'', b'--', b'a\nb', b'bad\xff', b'a b']
            done = subprocess.run([b'bash', os.fsencode(wrapper), *args], stdout=subprocess.PIPE, check=False)
            expected = b'\0'.join(args) + b'\0'
            self.assertTrue(done.stdout.startswith(expected))
            self.assertEqual(log.read_bytes()[:-1], done.stdout[len(expected):].strip())

    def test_end_missing_failure_skip_and_loop_identity(self):
        # 数字とNULを明確に分け、八進escapeの解釈へ依存させない。
        raw = b'\0'.join((b'338', b'pass', b'a', b'338', b'pass', b'b', b'end', b'1', b'1', b'1', b''))
        value = legacy_adapter.observations(raw)
        self.assertEqual(['legacy-0338-01', 'legacy-0338-02'], list(value))
        for bad in (b'', raw[:-1], raw.replace(b'end\x001', b'end\x000'), raw.replace(b'pass', b'unknown', 1)):
            with self.assertRaises(AssertionError):
                legacy_adapter.observations(bad)
        failure = legacy_adapter.observations(raw.replace(b'pass', b'fail', 1))
        self.assertEqual('fail', failure['legacy-0338-01'][0])
        skipped = legacy_adapter.observations(raw.replace(b'a\0', 'rootのため飛ばす\0'.encode(), 1))
        self.assertEqual('skipped', skipped['legacy-0338-01'][0])




class TypedEvidence(unittest.TestCase):
    def test_secret_alias_inode_and_missing_read_hash_are_checked(self):
        from diff_snapshot_fixtures import typed_audit as audit
        value={'secret':{'path_hex':b'/fixture/.env'.hex(),'dev':1,'ino':2},'events':[
            {'operation':operation,'path_hex':b'/fixture/alias'.hex(),'dev':1,'ino':2,'bytes':17,'fd':3,'phase':'after-classification-mutant'}
            for operation in ('Open','Read','Hash')],'detected':True}
        audit.validate_secret(value,reject='hash')
        with self.assertRaises(AssertionError):audit.validate_secret(value)
        for operation in ('Read','Hash'):
            bad=json.loads(json.dumps(value));bad['events']=[e for e in bad['events'] if e['operation']!=operation]
            with self.assertRaises(AssertionError):audit.validate_secret(bad,reject='hash')
        bad=json.loads(json.dumps(value));bad['events'][1]['ino']=3;bad['events'][2]['ino']=3
        with self.assertRaises(AssertionError):audit.validate_secret(bad,reject='hash')
        bad=json.loads(json.dumps(value));bad['detected']=False
        with self.assertRaises(AssertionError):audit.validate_secret(bad,reject='hash')
        for field,wrong in (('bytes',0),('fd',None),('phase','before-classification')):
            bad=json.loads(json.dumps(value));bad['events'][1][field]=wrong
            with self.assertRaises(AssertionError):audit.validate_secret(bad,reject='hash')

    def test_git_arguments_only_map_root_after_C(self):
        from diff_snapshot_fixtures import typed_audit as audit
        event={'operation':'Spawn','child_pid':20,'dev':1,'ino':2,'path_hex':b'/usr/bin/git'.hex(),
               'arguments_hex':[v.hex() for v in (b'git',b'-C',b'/fixture',b'diff',b'HEAD')]}
        expected=[(b'/usr/bin/git',(b'git',b'-C',b'<fixture-root>',b'diff',b'HEAD'))]
        self.assertEqual(expected,audit.git_calls([event],b'/fixture'))
        for bad in (dict(event,child_pid=None),dict(event,ino=None),dict(event,arguments_hex=[v.hex() for v in (b'git',b'/fixture')])):
            with self.assertRaises(AssertionError):audit.git_calls([bad],b'/fixture')
        changed=dict(event,arguments_hex=[v.hex() for v in (b'git',b'-C',b'/fixture',b'diff',b'HEAD~1')])
        self.assertNotEqual(expected,audit.git_calls([changed],b'/fixture'))

    def test_leaf_same_tick_and_metadata_change_have_different_expected_results(self):
        import hashlib
        from diff_snapshot_fixtures.profile_races import validate_leaf
        good={'action':'leaf-replace','point':'profile-reading','held_metadata_changed':False,'error':None,
              'value':{'size':131072,'sha256':hashlib.sha256(b'a'*131072).hexdigest()}}
        validate_leaf(good)
        with self.assertRaises(AssertionError):validate_leaf(dict(good,held_metadata_changed=True))
        changed=dict(good,held_metadata_changed=True,value=None,error='現在の profile が読取中に変更された')
        validate_leaf(changed)
        with self.assertRaises(AssertionError):validate_leaf(dict(changed,held_metadata_changed=False))
        with self.assertRaises(AssertionError):validate_leaf(dict(good,action='leaf-replace-mtime'))
        with self.assertRaises(AssertionError):validate_leaf(dict(good,point='profile-inspected'))

    def test_signal_actual_number_and_waiting_child_are_required(self):
        from diff_snapshot_fixtures.publication import verify_signal
        case={'signal':'HUP','state':'child-wait'}
        good={'code':20,'received':1,'child_completed':True,'captured_stderr_hex':''}
        verify_signal(case,good)
        for changes in ({'received':True},{'received':None},{'received':15},{'child_completed':False},{'captured_stderr_hex':'00'}):
            with self.assertRaises(AssertionError):verify_signal(case,dict(good,**changes))

    def test_fixed_thread_keeps_location_count_width_and_entire_body(self):
        from diff_snapshot_fixtures.fd_cases import fixed_thread
        before=b'Fatal fixture\n\n';after=b'  <no Python frame>\nERROR [internal] failure\n'
        raw=before+b'Current thread 0x0123456789abcdef (most recent call first):\n'+after
        fixed_thread(raw,before,after)
        for changed in (raw+raw,raw.replace(b'0123456789abcdef',b'1234'),raw.replace(b'Fatal fixture',b'Fatal other'),raw.replace(b'failure',b'changed'),raw.replace(b'\n\n',b'\n')):
            with self.assertRaises(AssertionError):fixed_thread(changed,before,after)


class EntrySignals(unittest.TestCase):
    """入口の信号待機の判定が、後続の外部呼出し・公開・片付け漏れを見逃さないこと。"""
    def observation(self,signal_name,**changes):
        from diff_snapshot_fixtures.entry_signals import verify
        case={'id':'entry-signal-git-'+signal_name,'tool':'git','signal':signal_name}
        stopped=signal_name!='none'
        calls=[{'tool':'grep','args_hex':['2d45']},{'tool':'git','args_hex':['72657670']}]
        if not stopped:calls.append({'tool':'realpath','args_hex':['2d2d']})
        values=dict(result=Result(20 if stopped else 0,b'',b'',None if stopped else b'report',None if stopped else b'patch'),
                    calls=calls,notifications=[{'event':'waiting','pid':7},{'event':'completed','pid':7,'code':0}],
                    leftovers=[],root_before=[b'repo'],root_after=[b'repo'])
        values.update(changes)
        return verify(case,values['result'],values['calls'],values['notifications'],leftovers=values['leftovers'],
                      root_before=values['root_before'],root_after=values['root_after'])

    def test_good_observations_keep_only_the_prefix_through_target(self):
        stopped=self.observation('TERM')
        self.assertEqual(2,len(stopped['calls_through_target']))
        self.assertFalse(stopped['reached_later_calls'])
        control=self.observation('none')
        self.assertTrue(control['reached_later_calls'])
        self.assertEqual(stopped['calls_through_target'],control['calls_through_target'])

    def test_later_calls_publication_files_and_leftovers_are_rejected(self):
        later=[{'tool':'grep','args_hex':['2d45']},{'tool':'git','args_hex':['72657670']},{'tool':'mktemp','args_hex':['2d64']}]
        for changes in ({'calls':later},
                        {'result':Result(20,b'',b'',b'report',None)},
                        {'result':Result(20,b'',b'',None,b'patch')},
                        {'root_after':[b'.diff-snapshot.abc',b'repo']},
                        {'leftovers':[b'tmp.abc']},
                        {'result':Result(0,b'',b'',None,None)},
                        {'result':Result(20,b'',b'ERROR [internal] x\n',None,None)},
                        {'result':Result(20,b'x',b'',None,None)},
                        {'notifications':[{'event':'waiting','pid':7}]},
                        {'notifications':[{'event':'waiting','pid':7},{'event':'completed','pid':8,'code':0}]},
                        {'calls':[{'tool':'grep','args_hex':['2d45']}]}):
            with self.subTest(changes=changes),self.assertRaises(AssertionError):self.observation('HUP',**changes)

    def test_control_must_reach_later_calls_and_publish(self):
        early=[{'tool':'grep','args_hex':['2d45']},{'tool':'git','args_hex':['72657670']}]
        for changes in ({'calls':early},{'result':Result(0,b'',b'',None,b'patch')},{'result':Result(0,b'',b'',b'report',None)},
                        {'leftovers':[b'tmp.abc']},{'result':Result(20,b'',b'',b'report',b'patch')}):
            with self.subTest(changes=changes),self.assertRaises(AssertionError):self.observation('none',**changes)


class ProfileEntrySignals(unittest.TestCase):
    """helperが最後まで動いた後に親が止まること、後続の外部呼出しが無いこと。"""
    def observe(self,signal_name,**changes):
        from diff_snapshot_fixtures.entry_signals import verify
        case={'id':'entry-signal-profile-'+signal_name,'tool':'git','signal':signal_name,'match':'--show-object-format','profile':True}
        target={'tool':'git','args_hex':[b'rev-parse'.hex(),b'--show-object-format'.hex()]}
        calls=[{'tool':'grep','args_hex':[]},target,{'tool':'git','args_hex':[b'hash-object'.hex()]}]
        stderr=b'' if signal_name!='none' else 'ERROR [internal] secret profile の和集合を作れない\n'.encode()
        values=dict(result=Result(20,b'',stderr,None,None),calls=calls,
                    notifications=[{'event':'waiting','pid':3},{'event':'completed','pid':3,'code':0}],
                    leftovers=[],root_before=[b'repo'],root_after=[b'repo'])
        values.update(changes)
        return verify(case,values['result'],values['calls'],values['notifications'],leftovers=values['leftovers'],
                      root_before=values['root_before'],root_after=values['root_after'])

    def test_helper_git_calls_continue_but_nothing_after(self):
        self.assertEqual(2,len(self.observe('TERM')['calls_from_target']))
        self.observe('none')
        target={'tool':'git','args_hex':[b'rev-parse'.hex(),b'--show-object-format'.hex()]}
        for changes in ({'result':Result(20,b'','ERROR [secret-profile] x\nERROR [internal] secret profile の和集合を作れない\n'.encode(),None,None)},
                        {'result':Result(0,b'',b'',None,None)},
                        {'calls':[target,{'tool':'grep','args_hex':[]}]},
                        {'calls':[target,target]},
                        {'leftovers':[b'tmp.x']},
                        {'result':Result(20,b'',b'',b'report',None)}):
            with self.subTest(changes=changes),self.assertRaises(AssertionError):self.observe('HUP',**changes)


class PartialOutput(unittest.TestCase):
    """所在地欄を固定名へ置き換えてから同じbyte数で切る。他の欄や順序は変えない。"""
    def test_location_is_mapped_before_cutting(self):
        from diff_snapshot_fixtures.review_cases import cut_location
        rows=[{'position':0,'line':567,'detail':b'printf: write error: File too large'}]
        clean=b'/long/path/diff-snapshot.sh: line 567: printf: write error: File too large\nERROR [internal] x\n'
        self.assertEqual(b'diff-snapshot.sh: li',cut_location(clean,invocation='/long/path/diff-snapshot.sh',rows=rows,size=20))
        self.assertEqual(b'diff-snapshot.sh: line 567: printf: write error: File too large\nERROR [internal] x\n',
                         cut_location(clean,invocation='/long/path/diff-snapshot.sh',rows=rows,size=500))
        for bad in (clean.replace(b'567',b'568',1),clean.replace(b'File too large',b'Broken pipe'),b'x\n'+clean,clean+clean):
            with self.subTest(bad=bad),self.assertRaises(AssertionError):
                cut_location(bad,invocation='/long/path/diff-snapshot.sh',rows=rows,size=20)

    def test_mapping_is_not_a_prefix_strip(self):
        from diff_snapshot_fixtures.review_cases import cut_location
        rows=[{'position':0,'line':567,'detail':b'printf: write error: File too large'}]
        clean=b'/p/diff-snapshot.sh: line 567: printf: write error: File too large\n'
        with self.assertRaises(AssertionError):
            cut_location(clean,invocation='/other/diff-snapshot.sh',rows=rows,size=10)


class Utf8Fixed(unittest.TestCase):
    """UTF-8固定の印・参照の環境・実効の値を誤って広げる変異を拒否する。"""
    def test_marks_follow_the_observed_difference(self):
        from diff_snapshot_fixtures import utf8_fixed as u
        same=Result(20,b'',b'x',None,None);other=Result(20,b'',b'y',None,None)
        self.assertIs(u.expectation('c',same,other,marked=True),other)
        self.assertIs(u.expectation('c',same,same,marked=False),same)
        for marked,a,b in ((True,same,same),(False,same,other)):
            with self.subTest(marked=marked),self.assertRaises(AssertionError):u.expectation('c',a,b,marked=marked)
        self.assertNotIn('raw-env-pythonintmaxstrdigits-ff-current-0',u.MARKED)
        self.assertNotIn('surrogate-01',u.MARKED)
        self.assertNotIn('fd-profile-0-directory-default',u.MARKED)

    def test_reference_changes_only_the_three_settings(self):
        from diff_snapshot_fixtures import utf8_fixed as u
        env={'PYTHONIOENCODING':'ascii','LC_ALL':'C','PYTHONINTMAXSTRDIGITS':'640'}
        ref=u.reference_env(env)
        self.assertEqual(ref,{'LC_ALL':'C','PYTHONINTMAXSTRDIGITS':'640','PYTHONUTF8':'1','PYTHONCOERCECLOCALE':'0'})
        u.check_difference(env,ref)
        for bad in (dict(ref,LC_ALL='C.UTF-8'),dict(ref,PYTHONINTMAXSTRDIGITS='0'),env):
            with self.subTest(bad=bad),self.assertRaises(AssertionError):u.check_difference(env,bad)

    def test_effective_values_are_required(self):
        from diff_snapshot_fixtures import utf8_fixed as u
        def runner(values):
            return lambda *a,**k: subprocess.CompletedProcess(a,0,json.dumps(values).encode(),b'')
        u.check_effective({'A':'1'},runner=runner(u.EXPECTED_EFFECTIVE))
        for i in range(len(u.EXPECTED_EFFECTIVE)):
            wrong=list(u.EXPECTED_EFFECTIVE);wrong[i]=0 if i==0 else 'ascii'
            with self.subTest(i=i),self.assertRaises(AssertionError):u.check_effective({'B':str(i)},runner=runner(wrong))

    def test_thread_field_only_in_the_fixed_format(self):
        from diff_snapshot_fixtures import utf8_fixed as u
        a=Result(20,b'',b'Current thread 0x00007f0000000001 (most recent call first):\n',None,None)
        b=Result(20,b'',b'Current thread 0x00007f0000000002 (most recent call first):\n',None,None)
        u.same_with_thread(a,b,same_result)
        with self.assertRaises(AssertionError):
            u.same_with_thread(a,Result(20,b'',b'Current thread 0x7f02 (most recent call first):\n',None,None),same_result)


class LoaderInitializer(unittest.TestCase):
    """main前の処理は開発用exampleだけに置き、製品が使うplatformには置かない。"""
    def test_only_the_example_registers_startup_state(self):
        native=Path(__file__).resolve().parents[1]
        markers=('.init_array','__mod_init_func','link_section')
        for path in sorted((native/'crates/platform/src').rglob('*.rs')):
            text=path.read_text()
            with self.subTest(path=path.name):
                self.assertFalse(any(marker in text for marker in markers))
        example=(native/'crates/cli/examples/diff_snapshot_contract.rs').read_text()
        self.assertEqual(1,example.count('.init_array'))
        self.assertEqual(1,example.count('remember_startup_state'))
        for path in sorted((native/'crates/cli/src').rglob('*.rs')):
            with self.subTest(path=path.name):
                self.assertNotIn('remember_startup_state',path.read_text())


class DynamicAudit(unittest.TestCase):
    def test_runtime_hardlink_deleted_after_read_keeps_identity(self):
        raw=b'10 execve("/usr/bin/git", ["git", "diff"], 0) = 0\n10 link("/repo/.env", "/repo/alias") = 0\n10 execve("/bin/reader", ["reader"], 0) = 0\n10 openat(AT_FDCWD, "/repo/alias", O_RDONLY) = 3</repo/alias>\n10 read(0x3, 0x1000, 0x40) = 0x12\n'
        events=parse_trace(raw,cwd='/repo',secret_identities={b'/repo/.env':(1,2)},git_executables={b'/usr/bin/git'})
        body=[event for event in events if event.operation=='Read']
        self.assertEqual([(b'/repo/.env',1,2,18)],[(v.path,v.device,v.inode,v.byte_count) for v in body])
        with self.assertRaises(AssertionError):audit_reads(events[:1],events,{(1,2)},git_executables={b'/usr/bin/git'})

    def test_unannotated_relative_parent_fd_is_used(self):
        raw=b'10 execve("/usr/bin/git", ["git", "diff"], 0) = 0\n10 openat(AT_FDCWD, "/repo/nested", O_RDONLY|O_DIRECTORY) = 4\n10 openat(4, ".env", O_RDONLY) = 5\n10 read(0x5, 0x1000, 0x40) = 0x12\n'
        events=parse_trace(raw,cwd='/repo',secret_identities={b'/repo/nested/.env':(1,2)},git_executables={b'/usr/bin/git'})
        self.assertEqual(1,len([event for event in events if event.operation=='Read']))


class RawReadAnnotations(unittest.TestCase):
    def trace(self,call):
        return b'10 execve("/usr/bin/git", ["git", "diff"], 0) = 0\n10 openat(AT_FDCWD, "/repo/.env", O_RDONLY) = 3</repo/.env>\n10 '+call+b'\n'
    def parse(self,call,identities=None):
        return parse_trace(self.trace(call),cwd='/repo',secret_identities=identities or {b'/repo/.env':(1,2)},git_executables={b'/usr/bin/git'})
    def test_raw_read_and_pread_accept_verified_fd_annotation(self):
        for name in (b'read',b'pread64'):
            for descriptor in (b'0x3',b'3',b'0x3</repo/.env>',b'3</repo/.env>'):
                args=b', 0x1000, 0x40'+(b', 0' if name==b'pread64' else b'')
                events=self.parse(name+b'('+descriptor+args+b') = 0x12')
                reads=[e for e in events if e.operation=='Read']
                self.assertEqual([(3,18,1,2)],[(e.fd,e.byte_count,e.device,e.inode) for e in reads])
    def test_annotation_alias_requires_same_observed_inode(self):
        call=b'read(0x3</repo/alias>, 0x1000, 0x40) = 0x12'
        self.parse(call,{b'/repo/.env':(1,2),b'/repo/alias':(1,2)})
        for identities in ({b'/repo/.env':(1,2)},{b'/repo/.env':(1,2),b'/repo/alias':(1,3)}):
            with self.assertRaises(AssertionError):self.parse(call,identities)
    def test_unknown_fd_wrong_path_and_buffer_logging_are_rejected(self):
        for call in (b'read(0x4</repo/.env>, 0x1000, 0x40) = 0x12',
                     b'read(0x3</repo/public>, 0x1000, 0x40) = 0x12',
                     b'read(0x3</repo/.env>, "SYNTHETIC BODY", 0x40) = 14',
                     b'pread64(3</repo/.env>, "SYNTHETIC BODY", 16, 0) = 14'):
            with self.assertRaises(AssertionError):self.parse(call)


class LaunchSites(unittest.TestCase):
    """起動場所の表: 別の版の文・起動しない道具の起動・数の取り違えを拒否する。"""
    def setUp(self):
        from diff_snapshot_fixtures import launch_sites
        self.sites = launch_sites
        self.table = launch_sites.load()
        self.measured = {d['recipe']: d['measured'] for s in self.table['sites'] for d in s.get('diagnostics', ())}
        self.measured['redirect-trap'] = ('<D>/trap.sh: line 4: <D>/unreadable/x: Permission denied\n'
                                          'ERROR [internal] 予期しない失敗(終了コード 1・行 4)\n'
                                          'ERROR [internal] 予期しない失敗(終了コード 20・行 4)\n')
        self.measured['rm-trap-normal'] = ("rm: cannot remove '<D>/rr/inner/f': Permission denied\n"
                                           'ERROR [internal] 予期しない失敗(終了コード 1・行 3)\n')

    def test_table_matches_legacy_and_measurement(self):
        self.sites.check_static(self.table, subject.LEGACY.read_text())
        self.assertEqual(self.sites.check_measured(self.table, self.measured), [])

    def test_rejects_other_versions_text(self):
        old = dict(self.measured, **{'cp-fsize': "cp: error writing '<D>/big.tmp': File too large\n"})
        self.assertEqual(self.sites.check_measured(self.table, old), ['site-50-cp:write'])
        musl = dict(self.measured, **{'redirect': 'bash: line 1: <D>/unreadable/x: Permission denied (os error 13)\n'})
        self.assertTrue(self.sites.check_measured(self.table, musl))

    def test_rejects_template_drift(self):
        import json
        mutant = json.loads(json.dumps(self.table))
        mutant['sites'][-4]['diagnostics'][0]['template'] = 'mv: cannot move {source} to {dest}: Access denied\n'
        with self.assertRaises(AssertionError):
            self.sites.check_static(mutant, subject.LEGACY.read_text())

    def test_rejects_printf_line_that_does_not_run_printf(self):
        import json
        mutant = json.loads(json.dumps(self.table))
        site = next(s for s in mutant['sites'] if s['id'] == 'site-58-printf')
        site['lines'] = [977, 1248]
        with self.assertRaises(AssertionError):
            self.sites.check_static(mutant, subject.LEGACY.read_text())

    def test_rejects_hidden_launch(self):
        git = ('git', (b'--no-pager', b'ls-files', b'-z'), {})
        self.assertEqual(self.sites.check_launches(self.table, [git], [git]), 1)
        for tool in ('date', 'mktemp', 'cp', 'python3', 'sh'):
            with self.assertRaises(AssertionError):
                self.sites.check_launches(self.table, [git, (tool, (b'x',), {})], [git])
        with self.assertRaises(AssertionError):
            self.sites.check_launches(self.table, [('git', (b'gc',), {})], [git])

    def test_base_versions(self):
        base = dict(self.table['base_versions'])
        self.assertTrue(self.sites.is_base(self.table, base)[0])
        self.assertFalse(self.sites.is_base(self.table, dict(base, coreutils='8.32'))[0])
        self.assertFalse(self.sites.is_base(self.table, dict(base, bash=None))[0])


class Prereview2(unittest.TestCase):
    """先行点検2の群: 地点ごとの型検査のIDと、追加の群の件数・IDの形。"""
    def test_cases_and_typed_ids_are_in_the_extra_group(self):
        from diff_snapshot_fixtures import fixed_extra, launch_sites, prereview2
        ids = [case['id'] for case in fixed_extra.cases() if case['group'] == prereview2.GROUP]
        self.assertEqual(len(ids), fixed_extra.EXTRA_GROUPS[prereview2.GROUP])
        self.assertEqual(len(set(ids)), len(ids))
        typed = {case['typed'] for case in fixed_extra.cases() if case['group'] == prereview2.GROUP
                 and case['kind'] == 'typed'}
        self.assertEqual(typed, set(launch_sites.READ_TYPED_IDS))
        self.assertTrue(all(i.startswith('pr2-') for i in ids))
        self.assertEqual({'pr2-' + kind for kind in prereview2.PROC_KINDS} - set(ids), set())


class FixedExtra(unittest.TestCase):
    def test_group_counts_and_manifest(self):
        from diff_snapshot_fixtures import fixed_extra
        self.assertEqual(sum(fixed_extra.check_counts().values()) + 17, 126)
        self.assertEqual(json.loads((subject.FIXTURES / 'cases.json').read_text())['fixed_extra'], fixed_extra.cases())

    def test_pending_is_not_success(self):
        from diff_snapshot_fixtures import fixed_extra
        with mock.patch.object(fixed_extra, 'run_case', side_effect=NotImplementedError('x')):
            outcome = fixed_extra.run_cases('unused', fixture_type=lambda root: None, legacy=None,
                                            finished_ids=set(), typed_ids=set())
        self.assertEqual(outcome['done'], [])
        self.assertEqual(len(outcome['pending']), 109 + sum(fixed_extra.EXTRA_GROUPS.values()))


class StageC4(unittest.TestCase):
    """区切り4: 書込みが切れた地点の観測・22:38の3つの場合の期待・型で注入した時刻の比較。"""
    def test_strace_cuts_are_ordered_and_skip_pipes(self):
        from diff_snapshot_fixtures import write_cuts
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'trace.10').write_text('1.000001 write(3</t/x/untracked.z>, ""..., 4096) = 4096\n'
                                             '1.000002 write(3</t/x/untracked.z>, ""..., 4096) = 100\n'
                                             '1.000003 write(1<pipe:[1]>, ""..., 10) = -1 EPIPE (Broken pipe)\n')
            (folder / 'trace.11').write_text('1.000004 write(4</t/x/attrin.z>, ""..., 50) = -1 EFBIG (File too large)\n'
                                             '0.900000 writev(2</t/x/err>, [{iov_base=""..., iov_len=5}, '
                                             '{iov_base=""..., iov_len=6}], 2) = 7\n'
                                             '0.950000 write(5</dev/null>, ""..., 9) = 1\n'
                                             '1.000005 copy_file_range(3</t/x/untracked.z>, NULL, 1</t/x/out.z>, NULL, '
                                             '9223372035781033984, 0) = -1 EFBIG (File too large)\n'
                                             '1.000006 copy_file_range(3</t/x/a>, NULL, 4</t/x/b>, NULL, 99, 0) = 9\n')
            cuts = write_cuts.parse(folder)
        self.assertEqual([(Path(c['path']).name, c['requested'], c['returned'], c['errno']) for c in cuts],
                         [('err', 11, 7, None), ('untracked.z', 4096, 100, None), ('attrin.z', 50, None, 'EFBIG'),
                          ('out.z', 9223372035781033984, None, 'EFBIG')])

    def test_three_cases_differ_and_cover_the_diagnostic_group(self):
        from diff_snapshot_fixtures import partial_outputs as p
        cases = p.cases()
        self.assertEqual(len(cases), 17)
        self.assertEqual({c['plan'] for c in cases if c['mode'] in ('value', 'warning')}, {1, 2, 3})
        self.assertEqual(json.loads((subject.FIXTURES / 'cases.json').read_text())['partial_outputs'], cases)
        line = b'/long/path/diff-snapshot.sh: line 567: printf: write error: File too large\n'
        clean = Result(20, b'', line + b'tail\n')
        old = Result(1, b'', clean.stderr[:40])
        mapping = lambda raw: raw.replace(b'/long/path/', b'')
        mapped = mapping(clean.stderr)
        values = {kind: p.expected_for(kind, old, clean, mapped, 40, mapping) for kind in (1, 2, 3)}
        self.assertEqual(values[2], (20, b'', mapped))
        self.assertEqual(values[3], (1, b'', mapped[:40]))
        self.assertEqual(len(set(values.values())), 3)

    def test_injected_timeout_is_compared_with_the_legacy_result(self):
        from diff_snapshot_fixtures import fixed_extra
        old = Result(20, b'', 'ERROR [secret-profile] profile の git 読取が時間内に完了しない\n'.encode())
        good = dict(case_id='profile-timeout-injected', code=20, stdout_hex='', stderr_hex=old.stderr.hex(),
                    published=False)
        with mock.patch.dict(fixed_extra.TIMEOUT_OBSERVED, {'legacy': old}, clear=True):
            self.assertTrue(fixed_extra.typed_timeout([good]))
            self.assertFalse(fixed_extra.typed_timeout([]))
            for change in (dict(code=1), dict(stderr_hex=b'other\n'.hex()), dict(published=True)):
                with self.assertRaises(AssertionError):
                    fixed_extra.typed_timeout([dict(good, **change)])
        with mock.patch.dict(fixed_extra.TIMEOUT_OBSERVED, {}, clear=True):
            self.assertFalse(fixed_extra.typed_timeout([good]))


class PathReduction(unittest.TestCase):
    def test_wrapper_reduces_path_and_mutant_runs_hidden_tool(self):
        from diff_snapshot_fixtures import path_reduction
        with tempfile.TemporaryDirectory() as root:
            folder = path_reduction.reduced_folder(root, os.environ['PATH'])
            self.assertEqual(sorted(os.listdir(folder)), ['git', 'grep'])
            target = Path(root) / 'show-path'
            target.write_text('#!/bin/sh\nprintf %s "$PATH"\n')
            target.chmod(0o700)
            done = subprocess.run([str(path_reduction.wrapper(root, target, folder))], stdout=subprocess.PIPE, check=True)
            self.assertEqual(done.stdout, str(folder).encode())
            mutant = path_reduction.wrapper(root, target, folder, hidden_tool='date')
            self.assertEqual(subprocess.run([str(mutant)], stdout=subprocess.DEVNULL, check=False).returncode, 99)



class StageC5(unittest.TestCase):
    """区切り5: 閉じた環境・版に依る欄のmanifest・D5の比較器の変異・Macの02:41と00:42の決定。"""

    def closed(self):
        from diff_snapshot_fixtures import closed_env
        return mock.patch.dict(closed_env._state, {'closed': True, 'removed': [], 'path': os.environ['PATH']})

    def test_comparator_environment_is_closed_and_ambient_leaks_are_rejected(self):
        from diff_snapshot_fixtures import closed_env
        ambient = {'PYTHONPATH': '/x', 'LANG': 'ja_JP.UTF-8', 'GIT_TRACE': '1', 'BASH_ENV': '/x', 'COLUMNS': '7',
                   'LC_CTYPE': 'C.UTF-8', 'USER': 'someone'}
        with mock.patch.dict(os.environ, ambient), mock.patch.dict(closed_env._state, {}):
            removed = closed_env.close_process()
            self.assertEqual(set(os.environ), {'PATH', 'TMPDIR'} & set(os.environ) | {'PATH'})
            self.assertTrue(set(ambient) <= set(removed))
            with tempfile.TemporaryDirectory() as root:
                env = subject.environment(Path(root))
                self.assertEqual(set(env), {'PATH', 'HOME', 'TMPDIR', *closed_env.SPEC['fixed']})
                self.assertEqual(env['HOME'], str(Path(root) / 'home'))
                self.assertEqual(env['TMPDIR'], str(Path(root) / 'tmp'))
                closed_env.check(env)
                self.assertIn('copy-ambient', closed_env.self_check(Path(root) / 'self'))
                # 変異: 縮める前の環境(印を置いた周囲の変数)を写して組み立てる。
                leaky = dict({name: closed_env.CANARY for name in closed_env.CANARY_NAMES}, **env)
                with self.assertRaisesRegex(AssertionError, '漏れました'):
                    closed_env.check(leaky)
                with self.assertRaisesRegex(AssertionError, '許可一覧に無い'):
                    closed_env.check(dict(env, SSH_AUTH_SOCK='/x'))
                (Path(env['HOME']) / '.gitconfig').write_text('[core]\n')
                with self.assertRaisesRegex(AssertionError, 'HOMEの中身'):
                    closed_env.check(env)
                digest = closed_env.record(env, root)
                self.assertEqual(bytes.fromhex(closed_env.ENVIRONMENTS[digest][b'HOME'.hex()]), b'<ROOT>/home')

    def test_launch_before_closing_is_rejected(self):
        from diff_snapshot_fixtures import closed_env, compare
        with mock.patch.dict(closed_env._state, {'closed': False}):
            with self.assertRaisesRegex(AssertionError, '縮める前'):
                compare.run(['true'], cwd='/', env={'PATH': os.environ['PATH']})

    def test_version_fields_marks_must_match_manifest(self):
        from diff_snapshot_fixtures import compare
        self.assertEqual(compare.version_kinds(b'mktemp: failed\n'), {'tool-message'})
        self.assertEqual(compare.version_kinds(b'/w/diff-snapshot.sh: line 9: x\nTerminated\n'), {'bash-message'})
        self.assertEqual(compare.version_kinds(b'bash: warning: setlocale: LC_ALL: cannot change locale\n'),
                         {'bash-startup-warning'})
        with mock.patch.object(compare, 'VERSION_HITS', {}), mock.patch.object(compare, 'CURRENT_SCOPE', ['review']):
            compare.note_versions(['bash', '/x/diff-snapshot.sh'], '/t/review-a', b'cp: x\n')
            compare.note_field('review-b:SHLVL', 'SHLVL')
            compare.check_versions({'review': {'review-a': ['tool-message'], 'review-b:SHLVL': ['SHLVL']}}, ['review'])
            with self.assertRaisesRegex(AssertionError, '抜けか誤り'):
                compare.check_versions({'review': {'review-a': ['tool-message']}}, ['review'])
            with self.assertRaisesRegex(AssertionError, '抜けか誤り'):
                compare.check_versions({'review': {'review-a': ['tool-message'], 'review-b:SHLVL': ['SHLVL'],
                                                   'review-c': ['bash-message']}}, ['review'])
            with self.assertRaises(AssertionError):
                compare.note_field('x', 'other')
        old = Result(1, b'', b'mkdir: cannot create\nERROR\n', case='c')
        new = Result(1, b'', b"mkdir: cannot create directory 'x'\nERROR\n", case='c')
        with mock.patch.dict(compare.VERSION_TOLERANT, {'names': None}):
            with self.assertRaises(AssertionError):
                same_result(old, new, generated_report=False)
        with mock.patch.dict(compare.VERSION_TOLERANT, {'names': {'c'}}), mock.patch.object(compare, 'VERSION_MASKED', set()):
            same_result(old, new, generated_report=False)
            self.assertEqual(compare.VERSION_MASKED, {'c'})
            with self.assertRaises(AssertionError):
                same_result(old, dataclasses.replace(new, stderr=new.stderr + b'other\n'), generated_report=False)

    def test_d5_judge_rejects_the_four_comparator_mutants(self):
        from diff_snapshot_fixtures import fixed_extra
        normal = Result(0, b'', b'', None, None)
        stopped = Result(20, b'', b'x: line 1: python3: command not found\n', None, None)
        case = dict(id='d5-python-missing-current', condition='python-missing', mark='d5', cause='python-missing')
        good = dict(normal=normal, legacy_launches=1, stopped=stopped,
                    rust={'yaml': (normal, 0), 'plain': (normal, 0), None: (normal, 0)})
        fixed_extra.d5_judge(case, good)
        for marked, values in ((case, dict(good, rust=dict(good['rust'], yaml=(normal, 1)))),
                               (case, dict(good, rust={'yaml': (normal, 0)})),
                               (case, dict(good, stopped=normal)),
                               (dict(case, cause='pyyaml-missing'), good)):
            with self.assertRaises(AssertionError):
                fixed_extra.d5_judge(marked, values)
        control = dict(case, condition='no-profile', mark=None, cause=None)
        fixed_extra.d5_judge(control, dict(good, stopped=normal))
        with self.assertRaises(AssertionError):
            fixed_extra.d5_judge(dict(control, mark='d5', cause='python-missing'), dict(good, stopped=normal))
        manifest = {case['id']: case for case in fixed_extra.cases() if case['group'] == 'd5'}
        self.assertEqual({k: (v['mark'], v['cause']) for k, v in manifest.items()},
                         {'d5-python-missing-current': ('d5', 'python-missing'),
                          'd5-python-missing-ref': ('d5', 'python-missing'),
                          'd5-pyyaml-missing-current': ('d5', 'pyyaml-missing'),
                          'd5-pyyaml-missing-ref': ('d5', 'pyyaml-missing'),
                          'd5-helper-missing': ('d5', 'helper-missing'),
                          'd5-no-profile-control': (None, None)})

    def test_mac_cf_text_encoding_is_removed_only_from_profile_git(self):
        from diff_snapshot_fixtures import review_cases
        calls = [('git', (b'--no-pager', b'rev-parse', b'--show-object-format'), {b'__CF_USER_TEXT_ENCODING': b'0x1F5'}),
                 ('git', (b'--no-pager', b'version'), {b'__CF_USER_TEXT_ENCODING': b'0x1F5'})]
        with mock.patch.object(review_cases, 'CF_RECORDS', []):
            self.assertIs(review_cases.cf_text_encoding('c', calls, system='Linux'), calls)
            done = review_cases.cf_text_encoding('c', calls, system='Darwin')
            self.assertEqual([env for _, _, env in done], [{}, {b'__CF_USER_TEXT_ENCODING': b'0x1F5'}])
            self.assertEqual(review_cases.CF_RECORDS[0]['present'], True)
            self.assertEqual(review_cases.CF_RECORDS[0]['value'], '0x1F5')

    def test_only_fsize_limit_is_set_and_inherited_limits_are_recorded(self):
        import re
        limits = subject.inherited_limits()
        self.assertTrue({'RLIMIT_AS', 'RLIMIT_NOFILE', 'RLIMIT_CPU', 'RLIMIT_STACK'} <= set(limits))
        for path in [subject.FIXTURES.parent / 'check_diff_snapshot.py', *subject.FIXTURES.glob('*.py')]:
            text = path.read_text()
            for found in re.findall(r'setrlimit\(resource\.(RLIMIT_[A-Z]+)', text) + re.findall(r'ulimit -([a-z])', text):
                self.assertIn(found, ('RLIMIT_FSIZE', 'f'), path.name)

    def test_job_report_sites_cover_each_shape(self):
        from diff_snapshot_fixtures import fixed_extra, group_signals
        reported = {area for area, site in group_signals.SITES.items() if site[3]}
        self.assertEqual(set(group_signals.SITES) - reported, {'comsub-git', 'comsub-pipe-git'})
        ids = [case['id'] for case in fixed_extra.cases() if case['group'] == 'group-signal-site']
        self.assertEqual(len(ids), 3 * len(group_signals.SITES))
        for area in reported:
            for signum in ('TERM', 'HUP'):
                self.assertEqual(fixed_extra.VERSION_DEPENDENT[f'group-signal-site-{area}-{signum}'], 'bash-message')



class BashBehaviorVariables(unittest.TestCase):
    """2026-11-12 09:58 追加の決定 3: bashが環境から読んで動きを変える変数を、比較の入力に使わない。"""

    SOURCE = '\n'.join([
        '#define BASHFUNC_PREFIX\t\t"BASH_FUNC_"',
        '#define BASHFUNC_SUFFIX\t\t"%%"',
        'void',
        'initialize_shell_variables (env, privmode)',
        '     char **env;',
        '{',
        '  temp_var = set_if_not ("PATH", DEFAULT_PATH_VALUE);',
        '  set_pwd ();',
        '  sv_funcnest ("FUNCNEST");',
        '}',
        'static void',
        'set_pwd ()',
        '{',
        '  temp_var = find_variable ("PWD");',
        '}',
        'static struct name_and_function special_vars[] = {',
        '  { "FUNCNEST", sv_funcnest },',
        '#if defined (READLINE)',
        '  { "TERM", sv_terminal },',
        '#endif',
        '  { (char *)0, (sh_sv_func_t *)0 }',
        '};',
        ''])

    def synthetic(self):
        import copy
        from diff_snapshot_fixtures import bash_behavior_vars as vars_
        spec = copy.deepcopy(vars_.SPEC)
        spec['source'].update(functions=['initialize_shell_variables', 'set_pwd'], helpers=['set_if_not', 'find_variable'],
                              entries=[])
        spec['source']['entries'] = vars_.extract(self.SOURCE, spec)
        spec.update(outside_variables_c=[], behavior=[dict(name='FUNCNEST', values='all', effect='x'),
                                                      dict(name='BASH_FUNC_<名>%%', values='all', effect='x')],
                    kept=[dict(name='PATH', reason='required-input'), dict(name='PWD', reason='child-env'),
                          dict(name='TERM', reason='interactive')])
        return vars_, spec

    def test_fixture_is_consistent_and_names_the_decided_variables(self):
        from diff_snapshot_fixtures import bash_behavior_vars as vars_
        names = vars_.check_classification()
        self.assertEqual(27, len(names))
        self.assertEqual(63, len(vars_.SPEC['kept']))
        self.assertEqual(128, len(vars_.SPEC['source']['entries']))
        for name in ('BASH_ENV', 'BASH_FUNC_<名>%%', 'SHELLOPTS', 'BASHOPTS', 'POSIXLY_CORRECT',
                     'FUNCNEST', 'GLOBIGNORE', 'EXECIGNORE', 'BASH_COMPAT', 'TMOUT'):
            self.assertIn(name, names)
        kept = {item['name'] for item in vars_.SPEC['kept']}
        # 起動時の警告(ロケール)と子へ渡る値は一覧に入れない(これまでの決定どおり再現する)。
        for name in ('LANG', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'PWD', 'OLDPWD', 'SHLVL', '_', 'PATH', 'HOME', 'TZ',
                     'EPOCHSECONDS', 'EPOCHREALTIME', 'COLUMNS'):
            self.assertIn(name, kept)
        self.assertEqual(64, len(vars_.SPEC['source']['files']['variables.c']))
        self.assertEqual(21, len(vars_.SPEC['source']['patches']))

    def test_classification_mismatch_with_source_is_rejected(self):
        import copy
        from diff_snapshot_fixtures import bash_behavior_vars as vars_
        spec = copy.deepcopy(vars_.SPEC)
        spec['kept'] = [item for item in spec['kept'] if item['name'] != 'TZ']
        with self.assertRaisesRegex(AssertionError, r"足りない.*'TZ'"):
            vars_.check_classification(spec)
        spec = copy.deepcopy(vars_.SPEC)
        spec['behavior'] = [item for item in spec['behavior'] if item['name'] != 'FUNCNEST']
        with self.assertRaisesRegex(AssertionError, r"足りない.*'FUNCNEST'"):
            vars_.check_classification(spec)
        spec = copy.deepcopy(vars_.SPEC)
        spec['behavior'].append(dict(name='CDPATH', values='all', effect='x'))
        with self.assertRaisesRegex(AssertionError, r"余分.*'CDPATH'"):
            vars_.check_classification(spec)
        spec = copy.deepcopy(vars_.SPEC)
        spec['kept'].append(dict(name='FUNCNEST', reason='child-env'))
        with self.assertRaisesRegex(AssertionError, '両方'):
            vars_.check_classification(spec)
        spec = copy.deepcopy(vars_.SPEC)
        spec['source']['entries'] = [e for e in spec['source']['entries'] if e['name'] != 'POSIX_PEDANTIC']
        with self.assertRaisesRegex(AssertionError, r"余分.*'POSIX_PEDANTIC'"):
            vars_.check_classification(spec)

    def test_extractor_detects_missing_and_extra_names_in_source(self):
        vars_, spec = self.synthetic()
        self.assertEqual({'BASH_FUNC_<名>%%', 'FUNCNEST', 'PATH', 'PWD', 'TERM'}, vars_.names_of(spec['source']['entries']))
        self.assertEqual('defined (READLINE)', next(e['guard'] for e in spec['source']['entries'] if e['name'] == 'TERM'))
        vars_.compare_entries(vars_.extract(self.SOURCE, spec), spec)
        vars_.check_classification(spec)
        added = self.SOURCE.replace('  { "FUNCNEST", sv_funcnest },', '  { "FUNCNEST", sv_funcnest },\n  { "GLOBIGNORE", sv_globignore },')
        with self.assertRaisesRegex(AssertionError, r"足りない \[\('GLOBIGNORE'"):
            vars_.compare_entries(vars_.extract(added, spec), spec)
        removed = self.SOURCE.replace('  { "TERM", sv_terminal },\n', '')
        with self.assertRaisesRegex(AssertionError, r"余分 \[\('TERM'"):
            vars_.compare_entries(vars_.extract(removed, spec), spec)
        moved = self.SOURCE.replace('void\ninitialize', '\nvoid\ninitialize')
        with self.assertRaisesRegex(AssertionError, '行・条件・並び'):
            vars_.compare_entries(vars_.extract(moved, spec), spec)
        unknown = self.SOURCE.replace('  set_pwd ();', '  set_pwd ();\n  set_other ();') + 'static void\nset_other ()\n{\n}\n'
        with self.assertRaisesRegex(AssertionError, '一覧の外の variables.c の関数を呼びます: set_other'):
            vars_.extract(unknown, spec)
        helper = self.SOURCE + 'SHELL_VAR *\nset_if_not (name, value)\n{\n  v = find_variable ("HIDDEN");\n}\n'
        with self.assertRaisesRegex(AssertionError, '補助の関数が名前の文字列で変数を読みます: set_if_not'):
            vars_.extract(helper, spec)
        with self.assertRaisesRegex(AssertionError, '初期化の関数がありません: set_pwd'):
            vars_.extract(self.SOURCE.replace('set_pwd ()\n{', 'set_pwd ();\n{'), spec)

    @unittest.skipUnless(os.environ.get('DEV_WORKFLOW_S3D_BASH_SOURCE'),
                         'bash-5.2 に 001〜021 を当てたソースの場所(DEV_WORKFLOW_S3D_BASH_SOURCE)が無い')
    def test_fixture_matches_real_bash_source(self):
        from diff_snapshot_fixtures import bash_behavior_vars as vars_
        self.assertEqual(27, len(vars_.verify_source(os.environ['DEV_WORKFLOW_S3D_BASH_SOURCE'])))

    def test_value_conditions_follow_bash(self):
        from diff_snapshot_fixtures import bash_behavior_vars as vars_
        for value in ('31', '3.1', '42', '5.1', '52', '5.2'):
            self.assertEqual('BASH_COMPAT', vars_.matches('BASH_COMPAT', value), value)
        # 範囲の外・形の違う値・空は、起動時の警告か既定の段で、再現の対象(一覧に当たらない)。
        for value in ('30', '3.0', '53', '5.3', '99', '5.10', '051', 'abc', '', ' 51', '51 '):
            self.assertIsNone(vars_.matches('BASH_COMPAT', value), value)
        for value in ('2', ' 7', '+3', '-1', '\t10', '99'):
            self.assertEqual('BASH_XTRACEFD', vars_.matches('BASH_XTRACEFD', value), value)
        for value in ('', 'abc', '2x', '0x1', '2 '):
            self.assertIsNone(vars_.matches('BASH_XTRACEFD', value), value)
        for name in ('BASH_FUNC_f%%', 'BASH_FUNC_%%', 'BASH_FUNC_a b%%', b'BASH_FUNC_\xff%%'):
            self.assertEqual('BASH_FUNC_<名>%%', vars_.matches(name, 'x'), name)
        for name in ('BASH_FUNC_f%', 'XBASH_FUNC_f%%', 'BASH_FUNC_f%%x', 'BASH_ARRAY_f%%', 'FUNCNESTX', 'funcnest'):
            self.assertIsNone(vars_.matches(name, 'x'), name)
        self.assertEqual(['FUNCNEST', 'TMOUT'], vars_.found({'TMOUT': '1', 'FUNCNEST': '1', 'PATH': '/bin'}))
        with self.assertRaisesRegex(AssertionError, r"動きを変える変数.*\['FUNCNEST'\]"):
            vars_.check({'PATH': '/bin', 'FUNCNEST': '1'})
        with self.assertRaisesRegex(AssertionError, '許可一覧がbashの動きを変える変数'):
            vars_.check_allowed({'PATH', 'GLOBIGNORE'}, ())
        import re as re_
        with self.assertRaisesRegex(AssertionError, 'EXECIGNORE'):
            vars_.check_allowed({'PATH'}, (re_.compile('EXEC[A-Z]+'),))
        with self.assertRaisesRegex(AssertionError, 'BASH_FUNC_x%%'):
            vars_.check_allowed({'BASH_FUNC_x%%'}, ())

    def test_case_inputs_and_allowed_names_do_not_use_the_variables(self):
        from diff_snapshot_fixtures import bash_behavior_vars as vars_, closed_env
        vars_.check_allowed(closed_env.NAMES, closed_env.PATTERNS)
        names = set()

        def walk(value):
            if isinstance(value, dict):
                if value.get('group') == 'startup-env':
                    return
                for key, item in value.items():
                    if key in ('env', 'environment', 'extra_env', 'set_env') and isinstance(item, dict):
                        names.update(item)
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        stored = json.loads((Path(subject.__file__).parent / 'diff_snapshot_fixtures/cases.json').read_text())
        walk(stored)
        self.assertTrue(names)
        self.assertEqual([], vars_.found({name: '51' for name in names}))
        # 例外の群: ケースが明示した一覧の変数は、警告だけになる値(固定の警告がある)か空だけ。
        startup = [case for case in stored['fixed_extra'] if case['group'] == 'startup-env']
        self.assertEqual(35, len(startup))
        for case in startup:
            self.assertTrue(set(case['env']) <= closed_env.EXCEPTABLE | {'SHLVL', 'LC_ALL'}, case['id'])
            for name, value in case['env'].items():
                if name in vars_.NAMES and value:
                    self.assertIn(f'bash: {name}: ', case['warning'], case['id'])

    def test_closed_environment_drops_ambient_behavior_variables(self):
        from diff_snapshot_fixtures import bash_behavior_vars as vars_, closed_env
        ambient = dict(vars_.samples(), FUNCNEST='1', GLOBIGNORE='*', EXECIGNORE='*', BASH_COMPAT='5.1', TMOUT='1',
                       BASH_XTRACEFD='2', SHELLOPTS='xtrace', POSIX_PEDANTIC='1', BASH_ENV='/x')
        ambient['BASH_FUNC_f%%'] = '() { :; }'
        self.assertEqual(len(vars_.NAMES), len(vars_.found(ambient)))
        with mock.patch.dict(os.environ, ambient), mock.patch.dict(closed_env._state, {}):
            removed = closed_env.close_process()
            self.assertTrue(set(ambient) <= set(removed))
            self.assertEqual([], vars_.found(os.environ))
            with tempfile.TemporaryDirectory() as root:
                env = subject.environment(Path(root))
                self.assertEqual([], vars_.found(env))
                closed_env.check(env)
                rejected = closed_env.self_check(Path(root) / 'self')
                self.assertEqual(sorted('behavior-' + name for name in vars_.samples()),
                                 sorted(name for name in rejected if name.startswith('behavior-')))
                self.assertTrue({'leak-FUNCNEST', 'leak-BASH_FUNC_s3d_canary%%', 'leak-TMOUT'} <= set(rejected))
                # 変異: 縮める前の周囲の変数を写して組み立てる。
                with self.assertRaisesRegex(AssertionError, '動きを変える変数'):
                    closed_env.check(dict(ambient, **env))
                # ケースが許可一覧に足して明示しても、一覧の変数は入力に使えない。
                with mock.patch.object(closed_env, 'NAMES', closed_env.NAMES | {'FUNCNEST', 'TMOUT'}):
                    for name in ('FUNCNEST', 'TMOUT'):
                        with self.assertRaisesRegex(AssertionError, f'動きを変える変数.*{name}'):
                            closed_env.check(dict(env, **{name: '1'}))
                    closed_env.check(dict(env))
                # 起動時の警告だけになる値は一覧に当たらない(許可一覧の外なので、比較器は別の理由で止める)。
                with self.assertRaisesRegex(AssertionError, '許可一覧に無い'):
                    closed_env.check(dict(env, BASH_COMPAT='99'))
                self.assertIn('bash_behavior_vars', closed_env.report())



class StartupEnv(unittest.TestCase):
    """09:58 決定 3 の後の指示: 起動時の警告と、実行中に値を作る変数が子へ渡る値の群。"""

    def test_case_names_allow_only_the_declared_variables_for_one_case(self):
        from diff_snapshot_fixtures import closed_env
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(closed_env._state, {'closed': True}):
            env = closed_env.base(Path(root))
            with self.assertRaisesRegex(AssertionError, '動きを変える変数'):
                closed_env.check(dict(env, BASH_XTRACEFD='99'))
            with self.assertRaisesRegex(AssertionError, '許可一覧に無い'):
                closed_env.check(dict(env, BASH_COMPAT='99'))
            with closed_env.case_names(['BASH_XTRACEFD', 'BASH_COMPAT', 'SECONDS']):
                closed_env.check(dict(env, BASH_XTRACEFD='99', BASH_COMPAT='99', SECONDS='1'))
                # 明示していない一覧の変数は、例外のケースでも入力に使えない。
                with self.assertRaisesRegex(AssertionError, '動きを変える変数'):
                    closed_env.check(dict(env, FUNCNEST='1'))
                with self.assertRaisesRegex(AssertionError, '重ねて'):
                    with closed_env.case_names(['SECONDS']):
                        pass
            self.assertEqual(set(), closed_env._CASE)
            with self.assertRaisesRegex(AssertionError, '足せない変数'):
                with closed_env.case_names(['FUNCNEST']):
                    pass
            with self.assertRaisesRegex(AssertionError, '動きを変える変数'):
                closed_env.check(dict(env, BASH_XTRACEFD='99'))

    def test_cases_are_registered_and_fixed(self):
        from diff_snapshot_fixtures import fixed_extra, startup_env
        items = startup_env.cases()
        self.assertEqual(35, len(items))
        self.assertEqual(len(items), fixed_extra.EXTRA_GROUPS[startup_env.GROUP])
        self.assertEqual(len(items), len({case['id'] for case in items}))
        fixed_extra.check_counts()
        order = next(case for case in items if case['id'] == 'startup-env-order')['warning'].splitlines()
        prefixes = ('bash: warning: shell level', 'bash: BASH_XTRACEFD:', 'bash: BASH_COMPAT:', 'bash: warning: setlocale:')
        self.assertEqual(len(prefixes), len(order))
        for line, prefix in zip(order, prefixes):
            self.assertTrue(line.startswith(prefix), line)
        dynamic = [case for case in items if case['variant'] == 'dynamic']
        self.assertEqual(3, len(dynamic))
        for case in dynamic:
            self.assertEqual(set(startup_env.DYNAMIC), set(case['env']))
            self.assertIn('LINENO', case['env'])
        lineno = [case for case in items if case['variant'] == 'lineno']
        self.assertEqual(list(startup_env.LINENO_SETUPS), [case['setup'] for case in lineno])
        for case in lineno:
            self.assertEqual({'LINENO': '7'}, case['env'])

    def test_warning_head_and_time_values_are_checked(self):
        from diff_snapshot_fixtures import startup_env
        case = {'warning': 'bash: BASH_COMPAT: 99: compatibility value out of range\n'}

        def result(stderr):
            return mock.Mock(stderr=stderr)
        startup_env._check_warning(case, result(case['warning'].encode() + b'ERROR [usage] x\n'))
        for stderr in (b'', b'bash: BASH_COMPAT: 9: compatibility value out of range\n',
                       case['warning'].encode() + b'bash: BASH_XTRACEFD: x: invalid value for trace file descriptor\n'):
            with self.assertRaisesRegex(AssertionError, '固定の文'):
                startup_env._check_warning(case, result(stderr))
        startup_env._check_warning({'warning': 'bash: BASH_XTRACEFD: <FD>: cannot open as FILE\n'},
                                   result(b'bash: BASH_XTRACEFD: 9: cannot open as FILE\n'), 9)
        calls = [('git', (b'a',), {b'EPOCHSECONDS': b'1000', b'EPOCHREALTIME': b'1000.500000', b'SECONDS': b'100'})]
        masked = startup_env._timed(calls, 999.9, 1001.0)
        self.assertEqual({b'EPOCHSECONDS': b'<TIME>', b'EPOCHREALTIME': b'<TIME>', b'SECONDS': b'100'}, masked[0][2])
        for name, value in ((b'EPOCHSECONDS', b'9'), (b'EPOCHSECONDS', b'1000.0'), (b'EPOCHREALTIME', b'1000.5'),
                            (b'EPOCHREALTIME', b'2000.000000'), (b'EPOCHSECONDS', b'')):
            with self.subTest(name=name, value=value), self.assertRaises(AssertionError):
                startup_env._timed([('git', (), {name: value})], 999.9, 1001.0)


if __name__ == '__main__':
    unittest.main()
