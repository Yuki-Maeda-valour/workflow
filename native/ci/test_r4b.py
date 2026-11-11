#!/usr/bin/env python3
"""R4B比較器が破損・別例外・別終了値・機密を拒否する検査。"""
import dataclasses
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

subject = None


def get_subject():
    global subject
    if subject is None:
        spec=importlib.util.spec_from_file_location('r4b_subject',Path(__file__).with_name('check_r4b.py'))
        subject=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=subject
        spec.loader.exec_module(subject)
    return subject


class Comparator(unittest.TestCase):
    def setUp(self):
        self.m=get_subject()

    def result(self, code=0, out=b'', err=b'', timeout=False):
        return self.m.RunResult(code,out,err,timeout)

    def test_normal_control_then_every_field_mutation(self):
        good=self.result(out=b'fixed\n')
        self.m.same_result(good,good)
        for change in [dict(returncode=1),dict(stdout=b'other'),dict(stderr=b'error'),dict(timed_out=True)]:
            with self.assertRaises(AssertionError):self.m.same_result(good,dataclasses.replace(good,**change))
        for result in [self.result(1),self.result(-15),self.result(2,err=b'Traceback: bad')]:
            # 両側が同じ異常結果でも固定期待は必ず拒む。
            with self.assertRaises(AssertionError):self.m.fixed(result,0,b'',b'')

    def test_seven_json_fields_are_not_normalized(self):
        good=self.m.origin_json()
        import json
        baseline=json.loads(good)
        for key in baseline:
            value=dict(baseline);value[key]='corrupted'
            with self.assertRaises(AssertionError):
                self.m.same_result(self.result(out=good),self.result(out=(json.dumps(value)+'\n').encode()))
        with self.assertRaises(AssertionError):
            self.m.same_result(self.result(out=good),self.result(out=json.dumps(baseline,separators=(',',':')).encode()))

    def test_decode_control_and_reject_foreign_exception_code_and_secrets(self):
        try:b'\xff'.decode('utf-8')
        except UnicodeDecodeError as error:expected=self.m.origin_traceback(error,decode_stream='stdout').encode()
        old=self.result(1,err=expected)
        new=self.result(1,err='git の出力を読めない\n'.encode())
        self.m.approved_decode(old,new)
        for candidate in [self.result(2,err=old.stderr),self.result(1,out=b'partial',err=old.stderr),
                          self.result(1,err=b'ValueError: bad\n'),self.result(1,err=old.stderr+b'ValueError: bad\n'),
                          self.result(1,err=self.m.SECRET+old.stderr),self.result(1,err=old.stderr,timeout=True)]:
            with self.assertRaises(AssertionError):self.m.approved_decode(candidate,new)
        for change in [dict(returncode=2),dict(stdout=b'partial'),dict(stderr=b'dev-workflow: invalid\n'),
                       dict(stderr=new.stderr+self.m.SECRET),dict(stderr=new.stderr+b'\n'),dict(timed_out=True)]:
            with self.assertRaises(AssertionError):self.m.approved_decode(old,dataclasses.replace(new,**change))

    def test_output_control_and_reject_foreign_exception_code_diagnostic_and_secret(self):
        old=self.result(120,err=b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\nOSError: [Errno 28] No space left on device\n")
        new=self.result(1,err='dev-workflow: 標準出力に書けません: No space left on device (os error 28)\n'.encode())
        check=lambda a,b:self.m.approved_output(a,b,old_code=120,exception=b'OSError: [Errno 28]')
        check(old,new)
        for change in [dict(returncode=1),dict(stdout=b'extra'),dict(stderr=old.stderr+b'ValueError: bad\n'),dict(stderr=b'ValueError: bad'),dict(stderr=old.stderr+self.m.SECRET),dict(timed_out=True)]:
            with self.assertRaises(AssertionError):check(dataclasses.replace(old,**change),new)
        for change in [dict(returncode=0),dict(stdout=b'extra'),dict(stderr=new.stderr+b'extra\n'),dict(stderr=b'OSError: wrong'),dict(stderr=new.stderr+self.m.SECRET),dict(timed_out=True)]:
            with self.assertRaises(AssertionError):check(old,dataclasses.replace(new,**change))
        self.m.approved_output(self.result(120),self.result(1),old_code=120,exception=None,broken_diagnostic=True)
        with self.assertRaises(AssertionError):
            self.m.approved_output(old,new,old_code=120,exception=b'OSError: [Errno 28]',broken_diagnostic=True)

    def test_individual_encoding_and_path_expectations_reject_every_mutation(self):
        old=self.result(0,out=b'sha256:'+b'a'*64+b'\n')
        new=self.result(0,out=b'sha256:'+b'b'*64+b'\n')
        args=((0,old.stdout,b''),(0,new.stdout,b''),b'git\n',b'git\n',b'git\n',b'git\n')
        self.m.fixed_pair(old,new,*args)
        for side in (0,1):
            for change in (dict(returncode=1),dict(stdout=b'sha256:'+b'c'*64+b'\n'),dict(stdout=new.stdout+b'extra'),dict(stderr=b'ValueError: bad'),dict(stderr=self.m.SECRET),dict(timed_out=True)):
                observed=[old,new];observed[side]=dataclasses.replace(observed[side],**change)
                with self.assertRaises(AssertionError):self.m.fixed_pair(*observed,*args)
        mismatch='git 設定のダイジェストが --expect と一致しない\n'.encode()
        old=self.result();new=self.result(1,err=mismatch)
        args=((0,b'',b''),(1,b'',mismatch),b'git\n',b'git\n',b'git\n',b'git\n')
        self.m.fixed_pair(old,new,*args)
        for candidate in (self.result(),self.result(1,out=b'extra',err=mismatch),self.result(1,err=mismatch+b'extra'),self.result(1,err=b'wrong')):
            with self.assertRaises(AssertionError):self.m.fixed_pair(old,candidate,*args)
        for position in (2,3):
            changed=list(args);changed[position]=b'wrong order'
            with self.assertRaises(AssertionError):self.m.fixed_pair(old,new,*changed)
        payload=self.m.origin_json(reason='origin が無い')
        for setting in self.m.ENCODINGS:
            expected=self.m.old_print_expectation(payload,setting)
            self.assertEqual(len(expected),3)
            if setting[2]=='utf-8':self.assertEqual(expected,(0,payload,b''))
        for wrong in (payload+b'extra',payload.replace(b'false',b'true',1),payload.replace('無'.encode(),b'\\u7121')):
            with self.assertRaises(AssertionError):self.m.fixed(self.result(out=wrong),0,payload,b'')

    def test_fault_observation_rejects_extra_exception_code_and_partial_output(self):
        import json
        expected=dict(code=1,exception=None,stdout='par',stderr='fixed diagnostic\n')
        encode=lambda value:self.result(out=(json.dumps(value)+'\n').encode())
        self.m.fault_observation(encode(expected),expected)
        for key,value in [('code',0),('code',True),('exception','ValueError'),('stdout','par-extra'),('stderr','fixed diagnostic\nextra'),('stderr',self.m.SECRET.decode())]:
            mutated=dict(expected);mutated[key]=value
            with self.assertRaises(AssertionError):self.m.fault_observation(encode(mutated),expected)
        for result in [self.result(1,out=b'{}'),self.result(out=b'{}',err=b'bad'),self.result(out=b'{}',timeout=True),self.result(out=b'{"code":1,"code":1}')]:
            with self.assertRaises(AssertionError):self.m.fault_observation(result,expected)

    def test_argument_classification_diagnostics_columns_and_raw_mutations_are_rejected(self):
        old=self.result(2,err=self.m.argument_error('origin-repo','argument --dir: expected one argument')[2])
        new=self.result(out=self.m.origin_json(reason='origin が無い'))
        expected_old=(old.returncode,old.stdout,old.stderr)
        expected_new=(new.returncode,new.stdout,new.stderr)
        raw=(b'--dir','-١'.encode(),b'80')
        before=[{'kind':'directory','mtime':1,'inode':2}]
        args=(expected_old,expected_new,b'',b'git -C raw\n',b'',b'git -C raw\n',raw,raw,before,before)
        self.m.argument_contract(old,new,*args)
        for side in (0,1):
            for change in (dict(returncode=0 if side==0 else 2),dict(stdout=b'extra'),dict(stderr=b'ValueError: bad\n'),dict(stderr=b'wrong escaping \\udcc3'),dict(stderr=self.m.SECRET),dict(timed_out=True)):
                results=[old,new];results[side]=dataclasses.replace(results[side],**change)
                with self.assertRaises(AssertionError):self.m.argument_contract(*results,*args)
        for index,value in ((2,b'git unexpected\n'),(3,b''),(3,b'git extra\ngit -C raw\n'),(7,(b'--dir',b'-1',b'80')),(7,(b'--dir','-١'.encode(),b'30')),(9,[{'kind':'directory','mtime':2,'inode':2}])):
            changed=list(args);changed[index]=value
            with self.assertRaises(AssertionError):self.m.argument_contract(old,new,*changed)
        # 正しい呼出が2つある対照から、逆順・欠落・余分な呼出を拒む。
        sequence=b'git remote\ngit config\n'
        good=self.result()
        call_args=((0,b'',b''),(0,b'',b''),sequence,sequence,sequence,sequence,raw,raw,before,before)
        self.m.argument_contract(good,good,*call_args)
        for broken in (b'git config\ngit remote\n',b'git remote\n',sequence+b'git extra\n'):
            changed=list(call_args);changed[3]=broken
            with self.assertRaises(AssertionError):self.m.argument_contract(good,good,*changed)
        help30=self.m.ARGUMENT_HELP['origin-repo',30].encode()
        help80=self.m.ARGUMENT_HELP['origin-repo',80].encode()
        self.m.fixed(self.result(out=help30),0,help30,b'')
        for broken in (help80,help30.replace(b'        show',b'       show'),help30+b'extra'):
            with self.assertRaises(AssertionError):self.m.fixed(self.result(out=broken),0,help30,b'')
        expected=self.m.argument_error('origin-repo',"argument -h/--help: ignored explicit argument 'é'")[2]
        for broken in (expected.replace('é'.encode(),b'\\udcc3\\udca9'),expected+b'extra',expected+self.m.SECRET):
            with self.assertRaises(AssertionError):self.m.fixed(self.result(2,err=broken),2,b'',expected)

    def test_terminal_fixed_expectations_reject_width_line_endings_and_other_failures(self):
        for command in self.m.COMMANDS:
            for columns in (30,60,80):
                for help_mode in (False,True):
                    if help_mode:good=self.result(out=self.m.ARGUMENT_HELP[command,columns].replace('\n','\r\n').encode())
                    else:
                        expected=self.m.argument_error(command,'the following arguments are required: --dir',width=columns)
                        good=self.result(*expected)
                    kwargs=dict(command=command,columns=columns,help_mode=help_mode)
                    self.m.terminal_contract(good,good,**kwargs)
                    other_width=80 if columns==30 else 30
                    with self.assertRaises(AssertionError):self.m.terminal_contract(good,good,**dict(kwargs,columns=other_width))
                    for change in (dict(returncode=1),dict(stdout=good.stdout+b'extra'),dict(stderr=good.stderr+b'error'),dict(timed_out=True)):
                        with self.assertRaises(AssertionError):self.m.terminal_contract(good,dataclasses.replace(good,**change),**kwargs)
                    if help_mode:
                        broken=dataclasses.replace(good,stdout=good.stdout.replace(b'\r\n',b'\n'))
                        with self.assertRaises(AssertionError):self.m.terminal_contract(good,broken,**kwargs)

    def test_locale_effective_eight_values_are_fixed_for_linux_and_darwin(self):
        for platform,errors in (('linux','surrogateescape'),('darwin','strict')):
            with self.subTest(platform=platform), mock.patch.object(sys,'platform',platform):
                spec=importlib.util.spec_from_file_location('r4b_encoding_'+platform,Path(__file__).with_name('check_r4b.py'))
                module=importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                setting=next(item for item in module.ENCODINGS if item[0]=='locale-utf8')
                self.assertEqual(setting[1]['LC_ALL'],'en_US.UTF-8' if platform=='darwin' else 'C.UTF-8')
                self.assertEqual(setting[3],errors)
                expected=['utf-8',errors,'utf-8','backslashreplace','utf-8','utf-8','surrogateescape',0]
                def probe(values):return self.result(out=module.json.dumps(values).encode())
                with mock.patch.object(module,'run_command',return_value=probe(expected)):
                    self.assertEqual(module.effective_encoding({},setting),'utf-8')
                for index,replacement in enumerate(('ascii','replace','ascii','strict','ascii','ascii','strict',1)):
                    changed=list(expected);changed[index]=replacement
                    with mock.patch.object(module,'run_command',return_value=probe(changed)):
                        with self.assertRaisesRegex(AssertionError,'actual=.*expected='):module.effective_encoding({},setting)
                changed=list(expected);changed[1]=module.SECRET.decode()
                with mock.patch.object(module,'run_command',return_value=probe(changed)):
                    with self.assertRaises(AssertionError) as failure:module.effective_encoding({},setting)
                self.assertNotIn(module.SECRET.decode(),str(failure.exception))
                for index in (0,2,4,5):
                    changed=list(expected);changed[index]=module.SECRET.decode()
                    with mock.patch.object(module,'run_command',return_value=probe(changed)):
                        with self.assertRaises(AssertionError) as failure:module.effective_encoding({},setting)
                    self.assertEqual(str(failure.exception),'実効文字コードのcodec名が不正')
                    diagnostic=''.join(module.traceback.format_exception(type(failure.exception),failure.exception,failure.exception.__traceback__))
                    self.assertNotIn(module.SECRET.decode(),diagnostic)


    def test_pty_helper_controls_real_stdout_width_without_normalizing_bytes(self):
        env=dict(os.environ,PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
        for width in (30,80):
            result=self.m.run_pty([sys.executable,'-B','-c','import os; print(os.get_terminal_size(1).columns)'],columns=width,env=env)
            with self.subTest(width=width):
                self.assertEqual(result,self.result(out=(str(width)+'\r\n').encode()))

    def test_pty_helper_sets_output_flags_before_spawn(self):
        original=self.m.pty.openpty
        def different_defaults():
            master,slave=original()
            attributes=self.m.termios.tcgetattr(slave)
            attributes[1] &= ~(self.m.termios.OPOST | self.m.termios.ONLCR)
            self.m.termios.tcsetattr(slave,self.m.termios.TCSANOW,attributes)
            return master,slave
        with mock.patch.object(self.m.pty,'openpty',side_effect=different_defaults):
            result=self.m.run_pty([sys.executable,'-B','-c',"import os; os.write(1,b'a\\nb\\r\\n')"],columns=30,env=dict(os.environ))
        self.assertEqual(result,self.result(out=b'a\r\nb\r\r\n'))

    def test_pty_helper_drains_bytes_before_each_platform_end_condition(self):
        for timeout,ending in ((False,b''),(False,OSError(self.m.errno.EIO,'closed')),(False,BlockingIOError(self.m.errno.EAGAIN,'drained')),(True,BlockingIOError(self.m.errno.EAGAIN,'drained'))):
            with self.subTest(timeout=timeout,ending=repr(ending)):
                process=mock.Mock(returncode=0)
                process.communicate.side_effect=[self.m.subprocess.TimeoutExpired('unused',10),(None,b'')] if timeout else [(None,b'')]
                with mock.patch.object(self.m.subprocess,'Popen',return_value=process), mock.patch.object(self.m.os,'killpg') as kill, mock.patch.object(self.m.os,'read',side_effect=[b'30\r',b'\n',ending]):
                    result=self.m.run_pty(['unused'],columns=30,env=dict(os.environ))
                self.assertEqual(result,self.result(out=b'30\r\n',timeout=timeout))
                self.assertEqual(kill.call_count,int(timeout))

    def test_pty_helper_keeps_slave_open_until_real_output_is_drained(self):
        original_open=self.m.pty.openpty
        original_read=self.m.os.read
        descriptors=[]
        def open_pair():
            pair=original_open()
            descriptors.extend(pair)
            return pair
        def read_while_slave_open(fd,size):
            if descriptors and fd==descriptors[0]:
                # 最後のslave closeによる出力破棄を、Linuxでも見逃さない。
                os.fstat(descriptors[1])
            return original_read(fd,size)
        with mock.patch.object(self.m.pty,'openpty',side_effect=open_pair), mock.patch.object(self.m.os,'read',side_effect=read_while_slave_open):
            result=self.m.run_pty([sys.executable,'-B','-c',"import os; os.write(1,b'kept\\r\\n')"],columns=30,env=dict(os.environ))
        self.assertEqual(result,self.result(out=b'kept\r\r\n'))
        for fd in descriptors:
            with self.assertRaises(OSError):os.fstat(fd)

        # slaveが最後に閉じられると未読キューを失う端末を模す。
        descriptors.clear()
        queued=[b'kept\r',b'\r\n']
        def read_discarded_queue(fd,size):
            try:os.fstat(descriptors[1])
            except OSError:
                queued.clear()
                return b''
            if queued:return queued.pop(0)
            raise BlockingIOError(self.m.errno.EAGAIN,'drained')
        process=mock.Mock(returncode=0)
        process.communicate.return_value=(None,b'')
        with mock.patch.object(self.m.pty,'openpty',side_effect=open_pair), mock.patch.object(self.m.subprocess,'Popen',return_value=process), mock.patch.object(self.m.os,'read',side_effect=read_discarded_queue):
            result=self.m.run_pty(['unused'],columns=30,env=dict(os.environ))
        self.assertEqual(result,self.result(out=b'kept\r\r\n'))
        for fd in descriptors:
            with self.assertRaises(OSError):os.fstat(fd)

    def test_input_and_call_order_corruption_are_not_equal(self):
        self.m.unchanged([{'mode':1}],[{'mode':1}])
        for altered in [[{'mode':2}],[{'mtime':2}],[{'inode':2}],[]]:
            with self.assertRaises(AssertionError):self.m.unchanged([{'mode':1}],altered)
        with self.assertRaises(AssertionError):self.m.same_result(self.result(out=b'git\nssh\n'),self.result(out=b'ssh\ngit\n'))

    def test_fixture_missing_empty_relative_nonfile_and_nonexecutable_fail(self):
        for path in (None,'','relative','/r4b-absent-binary'):
            with self.assertRaises(ValueError):self.m.fixture_path(path)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):self.m.fixture_path(temp)
            path=Path(temp)/'fixture';path.write_bytes(b'x');path.chmod(0o644)
            with self.assertRaises(ValueError):self.m.fixture_path(str(path))
            path.chmod(0o755)
            self.assertEqual(path,self.m.fixture_path(str(path)))
            with self.assertRaises(OSError):self.m.run_command([str(path)],cwd=temp)

    def test_zero_missing_and_failed_cases_rejected(self):
        with self.assertRaises(AssertionError):self.m.run_suite(unittest.TestSuite())
        class Failure(unittest.TestCase):
            def test_broken(self):self.fail('deliberate')
        with mock.patch('sys.stderr',io.StringIO()):
            with self.assertRaises(AssertionError):self.m.run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Failure))
        suite=self.m.existing_suite()
        self.assertGreater(suite.countTestCases(),0)
        with mock.patch.object(unittest.TestLoader,'getTestCaseNames',return_value=[]):
            with self.assertRaises(AssertionError):self.m.existing_suite()


if __name__=='__main__':unittest.main()
