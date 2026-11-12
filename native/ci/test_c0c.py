#!/usr/bin/env python3
"""C0cの比較器が誤った結果や同じ失敗を拒否することを検査する。"""
import copy
import re
import subprocess
import tempfile
import errno
import importlib.util
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('c0c_checker',Path(__file__).with_name('check_c0c.py'))
checker=importlib.util.module_from_spec(spec);sys.modules[spec.name]=checker;spec.loader.exec_module(checker)
from support_native import RunResult

# 子と接続の中継の経路が読んではいけない重いモジュール。
HEAVY_MODULES={'unittest','unittest.mock','asyncio','ctypes','tempfile','argparse'}

class Observations(unittest.TestCase):
    def setUp(self):self.good=checker.expected('sample',b'\0\xff')
    def run_result(self,value=None,**changes):
        data=dict(returncode=0,stdout=checker.encode(self.good if value is None else value),stderr=b'',timed_out=False)
        data.update(changes);return RunResult(**data)
    def rejects(self,value):
        with self.assertRaises((ValueError,TypeError)):
            checker.verify_run(self.run_result(value),'sample',self.good)
    def test_good_and_explicit_failure(self):
        self.assertEqual(checker.verify_run(self.run_result(),'sample',self.good),self.good)
        failure=checker.expected('sample',cause='spawn',os_errno=errno.ENOENT,spawned=False)
        self.assertEqual(checker.verify_run(self.run_result(failure),'sample',failure),failure)
    def test_identical_unexpected_failure_is_rejected(self):
        bad=checker.expected('sample',cause='deadline',signal_number=9)
        with self.assertRaises(ValueError):checker.verify_pair(self.run_result(bad),self.run_result(bad),'sample',self.good)
    def test_result_failure_stderr_and_timeout(self):
        for changes in [dict(returncode=1),dict(stderr=b'unexpected'),dict(timed_out=True),dict(stdout=b'not JSON')]:
            with self.assertRaises((ValueError,TypeError)):checker.verify_run(self.run_result(**changes),'sample',self.good)
    def test_missing_extra_keys_at_every_level(self):
        for name in [None,'status','cleanup','output','usage','input']:
            for missing in [False,True]:
                bad=copy.deepcopy(self.good);part=bad if name is None else bad[name]
                if missing:part.pop(next(iter(part)))
                else:part['extra']=1
                self.rejects(bad)
    def test_duplicate_keys(self):
        for token in [b'"version":1',b'"bytes":2',b'"written":0',b'"kind":"exit"']:
            raw=checker.encode(self.good).replace(token,token+b','+token)
            with self.assertRaises(ValueError):checker.verify_run(self.run_result(stdout=raw),'sample',self.good)
    def test_bool_float_negative_string_are_not_integers(self):
        for bad_value in [True,False,1.0,-1,'1',None,[],{}]:
            for group,key in [('usage','items'),('usage','bytes'),('status','value'),('input','written')]:
                bad=copy.deepcopy(self.good);bad[group][key]=bad_value;self.rejects(bad)
    def test_wrong_bytes_and_encoding(self):
        for value in ['00','FF','0','gg',0,None]:
            bad=copy.deepcopy(self.good);bad['output']['stdout_hex']=value;self.rejects(bad)
    def test_wrong_numeric_and_signal_status(self):
        for status in [dict(kind='exit',value=237),dict(kind='signal',value=9),dict(kind='signal',value=0),dict(kind='exit',value=256)]:
            bad=copy.deepcopy(self.good);bad['status']=status;self.rejects(bad)
    def test_wrong_deadline_case_and_usage(self):
        for field,value in [('deadline_unchanged',False),('case','another')]:
            bad=copy.deepcopy(self.good);bad[field]=value;self.rejects(bad)
        for key in ['items','bytes']:
            bad=copy.deepcopy(self.good);bad['usage'][key]+=1;self.rejects(bad)
    def test_failed_cleanup_never_passes(self):
        for key,value in [('expired',True),('pending',True),('reaped',False),('kill_errno',1),('wait_errno',10)]:
            bad=copy.deepcopy(self.good);bad['cleanup'][key]=value;self.rejects(bad)
    def test_present_child_never_passes(self):
        for value in [False,None,1,'true']:
            bad=copy.deepcopy(self.good);bad['direct_child_absent']=value;self.rejects(bad)
    def test_wrong_error_kind_errno_and_shape(self):
        wanted=checker.expected('sample',cause='spawn',os_errno=errno.ENOENT,spawned=False)
        for key,value in [('kind','mystery'),('errno',errno.EACCES),('stream','stdout'),('consumer','refused')]:
            bad=copy.deepcopy(wanted);bad['cause'][key]=value
            with self.assertRaises(ValueError):checker.verify_run(self.run_result(bad),'sample',wanted)
    def test_poll_transients_are_not_permanent_failures(self):
        for code in [errno.EAGAIN,errno.EINTR]:
            bad=checker.expected('sample',cause='poll',os_errno=code,signal_number=9)
            with self.assertRaises(ValueError):checker.verify_run(self.run_result(bad),'sample',bad)
    def test_wait_error_requires_unreaped_ownership(self):
        bad=checker.expected('sample',cause='wait',os_errno=errno.ECHILD,spawned=False)
        with self.assertRaises(ValueError):checker.verify_run(self.run_result(bad),'sample',bad)
    def test_partial_input_has_fixed_strict_bounds(self):
        wanted=checker.expected('sample',cause='input-closed',signal_number=9,early=True)
        for n in [True,-1,10,11]:
            bad=copy.deepcopy(wanted);bad['input']['written']=n
            with self.assertRaises(ValueError):checker.verify_run(self.run_result(bad),'sample',wanted,10,partial_input=True)
    def test_mutation_is_rejected_by_case_runner(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);wanted=checker.expected('sample',cause='spawn',os_errno=errno.ENOENT,spawned=False)
            with mock.patch.object(checker,'run_command',return_value=self.run_result(wanted)), mock.patch.object(checker,'tree',side_effect=[{'file':'old'},{'file':'new'}]), mock.patch.object(checker,'cleanup',return_value=[]):
                with self.assertRaisesRegex(ValueError,'入力が変更'):
                    checker.run_case(path/'fixture','sample',{'input_hex':''},wanted,path,path/'control')
    def test_identical_outer_timeouts_are_rejected(self):
        result=self.run_result(timed_out=True)
        with self.assertRaises(ValueError):checker.verify_pair(result,result,'sample',self.good)
    def test_cause_missing_extra_and_duplicate_keys(self):
        wanted=checker.expected('sample',cause='spawn',os_errno=errno.ENOENT,spawned=False)
        for missing in [True,False]:
            bad=copy.deepcopy(wanted)
            if missing:bad['cause'].pop('errno')
            else:bad['cause']['extra']=1
            with self.assertRaises(ValueError):checker.verify_run(self.run_result(bad),'sample',wanted)
        raw=checker.encode(wanted).replace(b'"kind":"spawn"',b'"kind":"spawn","kind":"spawn"')
        with self.assertRaises(ValueError):checker.verify_run(self.run_result(stdout=raw),'sample',wanted)
    def test_input_modification_is_observed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'file';path.write_bytes(b'old');before=checker.tree(directory)
            path.write_bytes(b'new');self.assertNotEqual(before,checker.tree(directory))

    def check_unreaped(self, *, delay=False, repeat=1):
        with tempfile.TemporaryDirectory() as directory:
            control=Path(directory)/'control'
            script="import os,time; os.write(1,b'x'); "+('time.sleep(.15)' if delay else 'pass')
            child=subprocess.Popen([sys.executable,'-B','-c',script],stdout=subprocess.PIPE)
            try:
                child.stdout.read(1 if delay else -1)
                control.write_text(checker.json.dumps({'pid':child.pid}))
                connection=mock.Mock();connection.poll.return_value=None
                observed=[]
                with self.assertRaises(AssertionError):
                    for iteration in range(repeat):
                        observed.append(iteration)
                        checker.observe_return(connection,{'returned':iteration},iteration,control,True)
                self.assertEqual(observed,[0])
            finally:
                child.wait(timeout=5);child.stdout.close()
            checker.assert_absent_now(child.pid)

    def test_unreaped_child_is_rejected_while_connection_lives(self):
        self.check_unreaped()

    def test_later_natural_exit_does_not_repair_return_observation(self):
        self.check_unreaped(delay=True)

    def test_first_of_two_unreaped_returns_stops_before_second(self):
        self.check_unreaped(repeat=2)

    def test_return_requires_sequence_and_live_connection(self):
        connection=mock.Mock();connection.poll.return_value=None
        with tempfile.TemporaryDirectory() as directory:
            control=Path(directory)/'absent'
            with self.assertRaises(ValueError):
                checker.observe_return(connection,{'returned':1},0,control,False)
            connection.poll.return_value=0
            with self.assertRaises(ValueError):
                checker.observe_return(connection,{'returned':0},0,control,False)

    def test_non_utf8_legacy_only_spawn_failure_is_rejected(self):
        for name in ['raw-name','raw-executable']:
            wanted=checker.expected(name,stdout=b'raw-\xff')
            old=checker.expected(name,cause='spawn',os_errno=errno.ENOENT,spawned=False)
            with self.assertRaises(ValueError):
                checker.verify_pair(self.run_result(old),self.run_result(wanted),name,wanted)

    def test_non_utf8_legacy_only_wrong_bytes_are_rejected(self):
        for name in ['raw-name','raw-executable']:
            wanted=checker.expected(name,stdout=b'raw-\xff')
            old=checker.expected(name,stdout=b'raw-?')
            with self.assertRaises(ValueError):
                checker.verify_pair(self.run_result(old),self.run_result(wanted),name,wanted)

    def test_missing_record_is_rejected_with_reason(self):
        connection=mock.Mock();connection.poll.return_value=None
        with tempfile.TemporaryDirectory() as directory:
            control=Path(directory)/'deadline-output.json'
            with self.assertRaises(ValueError) as caught:
                checker.observe_return(connection,{'returned':0},0,control,True)
            self.assertNotIsInstance(caught.exception,FileNotFoundError)
            message=str(caught.exception)
            self.assertIn('子の PID の記録が無いまま返却された',message);self.assertIn('deadline-output.json',message)
            self.assertNotIn('ms',message)
            sent=time.monotonic_ns()-1_500_000_000
            with self.assertRaises(ValueError) as caught:
                checker.observe_return(connection,{'returned':0},0,control,True,sent_ns=sent,execution_ms=2000)
            self.assertNotIsInstance(caught.exception,FileNotFoundError)
            message=str(caught.exception)
            self.assertIn('子の PID の記録が無いまま返却された',message);self.assertIn('deadline-output.json',message)
            self.assertIn('execution_ms=2000',message)
            elapsed=re.search(r'(-?[0-9]+\.[0-9]) ms',message)
            self.assertIsNotNone(elapsed,message);self.assertGreaterEqual(float(elapsed.group(1)),1500)
            # 記録が無いこと以外の読み取りの失敗は、そのまま上へ出す。
            control.mkdir()
            with self.assertRaises(IsADirectoryError):
                checker.observe_return(connection,{'returned':0},0,control,True,sent_ns=sent,execution_ms=2000)

    def case_with_latency(self,directory,wanted,*,latency=None,record=False):
        path=Path(directory);control=path/'sample.json'
        if record:
            done=subprocess.Popen([sys.executable,'-B','-c','pass']);done.wait(timeout=30)
            control.write_text(checker.json.dumps({'pid':done.pid}))
        if latency is not None:
            control.with_suffix('.latency').write_text(str(latency))
        with mock.patch.object(checker,'run_command',return_value=self.run_result(wanted)), mock.patch.object(checker,'tree',return_value={'file':'same'}), mock.patch.object(checker,'cleanup',return_value=[]):
            return checker.run_case(path/'fixture','sample',{'input_hex':''},wanted,path,control)

    def test_spawned_case_requires_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.case_with_latency(directory,self.good,latency=-3,record=True),-3)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'^'+re.escape('記録までの時間が無い: sample.json')+'$'):
                self.case_with_latency(directory,self.good,record=True)

    def test_unspawned_case_rejects_latency(self):
        wanted=checker.expected('sample',cause='spawn',os_errno=errno.ENOENT,spawned=False)
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(self.case_with_latency(directory,wanted))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'^'+re.escape('記録までの時間があってはいけない: sample.json')+'$'):
                self.case_with_latency(directory,wanted,latency=5)

    def light_run(self,directory,arguments,data=b''):
        process=subprocess.Popen([sys.executable,'-B','-X','importtime',str(Path(__file__).with_name('check_c0c.py'))]+arguments,
                                 cwd=directory,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            stdout,stderr=process.communicate(data,timeout=60)
        except BaseException:
            process.kill();process.communicate();raise
        # 各行の最後の列(| の後の、字下げを除いたモジュール名)を、そのまま名前として集める。
        names={line.rsplit('|',1)[1].strip() for line in stderr.decode(errors='replace').splitlines()
               if line.startswith('import time:') and '|' in line}
        return process,stdout,stderr,names

    def test_child_path_does_not_import_heavy_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            record=Path(directory)/'child.json'
            process,stdout,stderr,names=self.light_run(directory,['--child',str(record),'{"code":0}'])
            self.assertEqual(process.returncode,0,stderr[-2000:])
            self.assertEqual(checker.json.loads(record.read_text())['pid'],process.pid)
            self.assertIn('support_proc',names)
            self.assertEqual(names&HEAVY_MODULES,set())

    def test_fixture_driver_path_does_not_import_heavy_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture=root/'fixture'
            fixture.write_text('#!/bin/sh\nread -r line\necho final\n');fixture.chmod(0o755)
            control=root/'control';control.mkdir();record=control/'sample.json'
            plan=checker.plan_for(root,record,{});plan['repeat']=0
            process,stdout,stderr,names=self.light_run(root,['--fixture-driver',str(fixture),'sample',str(record),'1'],checker.encode(plan))
            self.assertEqual(process.returncode,0,stderr[-2000:])
            self.assertEqual(stdout,b'final\n')
            self.assertNotIn(b'Traceback',stderr)
            self.assertIn('support_proc',names)
            self.assertEqual(names&HEAVY_MODULES,set())

    def test_deadline_cases_use_named_limit(self):
        self.assertEqual(checker.DEADLINE_CASE_MS,2000)
        found={}
        for name,config,changes,_ in checker.cases():
            if changes.get('execution_ms',0)>0:
                plan=checker.plan_for('/',Path('/')/(name+'.json'),config);plan.update(changes)
                found[name]=plan['execution_ms']
                self.assertGreaterEqual(plan['cleanup_ms']-plan['execution_ms'],1000,name)
        self.assertEqual(found,dict.fromkeys(['deadline-close-outputs','deadline-output','stdin-stall','grandchild-holds-pipe'],2000))

    def test_latency_summary_line(self):
        pairs=[('a',1_000_000),('d1',4_000_000),('b',4_000_000),('i1',2_500_000),('d2',7_200_000),('i2',7_200_000)]
        self.assertEqual(checker.latency_summary(pairs,{'d1','d2'},{'i1','d2','i2'}),
                         'C0c 記録までの時間: 入力なし 測定 3 件 / 中央 4.0 ms / 最大 4.0 ms(d1) / 入力あり 測定 3 件 / 最大 7.2 ms(d2)'
                         ' / 期限のケース 2 件の最大 7.2 ms / 期限 2000 ms')

    def test_latency_summary_without_values_even_count_and_negative(self):
        summary=checker.latency_summary
        self.assertEqual(summary([],{'d'},{'i'}),'C0c 記録までの時間: 入力なし 測定 0 件 / 入力あり 測定 0 件 / 期限のケース 0 件 / 期限 2000 ms')
        self.assertEqual(summary([('i',3_000_000)],{'d'},{'i'}),
                         'C0c 記録までの時間: 入力なし 測定 0 件 / 入力あり 測定 1 件 / 最大 3.0 ms(i) / 期限のケース 0 件 / 期限 2000 ms')
        self.assertEqual(summary([('a',1_000_000),('b',2_000_000),('c',3_000_000),('e',8_000_000)],{'d'},{'i'}),
                         'C0c 記録までの時間: 入力なし 測定 4 件 / 中央 2.5 ms / 最大 8.0 ms(e) / 入力あり 測定 0 件 / 期限のケース 0 件 / 期限 2000 ms')
        self.assertEqual(summary([('d',-2_000_000),('a',-5_000_000)],{'d'},set()),
                         'C0c 記録までの時間: 入力なし 測定 2 件 / 中央 -3.5 ms / 最大 -2.0 ms(d) / 入力あり 測定 0 件'
                         ' / 期限のケース 1 件の最大 -2.0 ms / 期限 2000 ms')
        self.assertEqual(summary([('a',-40_000)],set(),set()),
                         'C0c 記録までの時間: 入力なし 測定 1 件 / 中央 -0.0 ms / 最大 -0.0 ms(a) / 入力あり 測定 0 件 / 期限のケース 0 件 / 期限 2000 ms')

if __name__=='__main__':unittest.main()
