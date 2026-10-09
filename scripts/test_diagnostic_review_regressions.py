"""#260 独立レビューR1/R2: 履歴の完全一致と診断一時物の配置。"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from diagnostic_test_support import DiagnosticFixture, DIAGNOSTIC, inventory

TEMP_DRIVER = r'''
import importlib.util,json,os,pathlib,sys,tempfile
helper,report,arguments=sys.argv[1:]
spec=importlib.util.spec_from_file_location('diagnostic_entry',helper)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=os.mkdir;created=[]
def mkdir(path,*args,**kw):
 if kw.get('dir_fd') is None:created.append(os.fsdecode(os.path.realpath(path)))
 return original(path,*args,**kw)
os.mkdir=mkdir
rc=m.main(json.loads(arguments))
pathlib.Path(report).write_text(json.dumps({'created':created,'rc':rc}))
sys.exit(rc)
'''

class DiagnosticReviewRegressions(DiagnosticFixture):
    def history(self,*scope,limit=20):
        response=self.observe('log',*scope,'--limit',str(limit))
        return base64.b64decode(response['data']['bytes_b64']),response

    def test_r1_directory_exact_path_never_selects_secret_descendant_history(self):
        self.profile(['dir/secret.txt'])
        (self.repo/'dir').mkdir(); (self.repo/'dir/public.txt').write_text('public')
        (self.repo/'dir/secret.txt').write_text('secret fixture');self.commit('initial directory')
        (self.repo/'dir/secret.txt').write_text('second secret fixture');self.commit('SECRET_ONLY_COMMIT')
        self.begin();raw,response=self.history('--path','dir')
        self.assertEqual(raw,b'');self.assertNotIn(b'SECRET_ONLY_COMMIT',raw)
        raw,response=self.history('--under','dir')
        self.assertIn(b'initial directory',raw);self.assertNotIn(b'SECRET_ONLY_COMMIT',raw)
        self.assertGreater(response['data']['excluded_count'],0)

    def test_r1_type_transitions_deleted_files_and_limit_preserve_exact_history(self):
        self.profile(['dir/secret.txt'])
        (self.repo/'dir').write_text('file first');self.commit('EXACT_FIRST')
        (self.repo/'dir').unlink();(self.repo/'dir').mkdir()
        (self.repo/'dir/secret.txt').write_text('secret fixture');self.commit('FILE_TO_DIRECTORY')
        (self.repo/'dir/secret.txt').write_text('secret update');self.commit('SECRET_ONLY_MIDDLE')
        (self.repo/'dir/secret.txt').unlink();(self.repo/'dir').rmdir();(self.repo/'dir').write_text('file last')
        self.commit('DIRECTORY_TO_FILE')
        (self.repo/'gone.txt').write_text('deleted fixture');self.commit('DELETED_FILE_ADD')
        (self.repo/'gone.txt').unlink();self.commit('DELETED_FILE_REMOVE')
        self.begin();raw,_=self.history('--path','dir')
        for message in (b'EXACT_FIRST',b'FILE_TO_DIRECTORY',b'DIRECTORY_TO_FILE'):self.assertIn(message,raw)
        self.assertNotIn(b'SECRET_ONLY_MIDDLE',raw)
        raw,_=self.history('--path','dir',limit=1)
        self.assertIn(b'DIRECTORY_TO_FILE',raw);self.assertNotIn(b'EXACT_FIRST',raw)
        raw,_=self.history('--path','gone.txt')
        self.assertIn(b'DELETED_FILE_ADD',raw);self.assertIn(b'DELETED_FILE_REMOVE',raw)

    def test_r1_normal_merge_history_limit_and_fixed_formats_match_native_git(self):
        (self.repo/'selected.txt').write_text('initial')
        self.commit('selected base')
        branch=self.git('symbolic-ref','--short','HEAD').stdout.decode().strip()
        self.git('checkout','-qb','topic')
        (self.repo/'selected.txt').write_text('topic change');self.commit('selected topic')
        self.git('checkout','-q',branch)
        (self.repo/'ordinary.txt').write_text('main only');self.commit('main metadata')
        self.git('merge','--no-ff','-m','merge topic','topic')
        self.begin()
        before=inventory(self.repo),inventory(self.state)
        for fmt in ('oneline','fuller'):
            for limit in (1,2,20):
                with self.subTest(format=fmt,limit=limit):
                    expected=self.git('log','--no-show-signature','--no-decorate','--no-notes',
                        '--no-patch','--no-color','--format='+fmt,'--max-count='+str(limit),
                        'HEAD','--','selected.txt').stdout
                    response=self.observe('log','--path','selected.txt','--format',fmt,'--limit',str(limit))
                    self.assertEqual(base64.b64decode(response['data']['bytes_b64']),expected)
        self.assertEqual((inventory(self.repo),inventory(self.state)),before)

    def test_r2_private_temp_never_created_inside_protected_physical_roots(self):
        self.begin();self.extend('files','--under','.','--kind','all')
        link=self.work/'inside-link';link.symlink_to(self.repo,target_is_directory=True)
        external=self.work/'external';external.mkdir()
        outside_link=self.work/'outside-link';outside_link.symlink_to(external,target_is_directory=True)
        roots=[self.repo,self.state,self.repo/'.git']
        for candidate in [self.repo,self.state,self.repo/'.git',link,external,self.work,outside_link]:
            with self.subTest(candidate=candidate):
                before=inventory(self.repo),inventory(self.state)
                report=self.work/'temp-report.json'
                result=subprocess.run([sys.executable,'-I','-B','-c',TEMP_DRIVER,str(DIAGNOSTIC),str(report),
                    json.dumps(['run',*self.common,'--plan',str(self.latest),'--plan-sha256',self.latest_hash])],
                    cwd=self.work,env=dict(self.env,TMPDIR=str(candidate)),capture_output=True,timeout=30)
                details=json.loads(report.read_text())
                for created in details['created']:
                    actual=Path(created)
                    self.assertFalse(any(actual==root or root in actual.parents for root in roots),created)
                self.assertEqual((inventory(self.repo),inventory(self.state)),before)
                if candidate in (external,self.work,outside_link):
                    response=self.success(result,'observed')
                    self.assertTrue(any(Path(p).parent==candidate.resolve() for p in details['created']))
                    self.assertFalse(any(b'diagnostic-' in base64.b64decode(r['path_b64']) for r in response['data']['records']))
                elif result.returncode:
                    self.failure(result,20)
                else:self.success(result,'observed')

    def test_r2_linked_worktree_external_gitdir_and_common_are_protected(self):
        original=self.repo
        linked=self.work/'linked'
        self.git('worktree','add','-qb','linked',str(linked))
        self.repo=linked;(linked/'task').mkdir();(linked/'task/t.md').write_text('# fixture')
        self.begin();self.extend('refs')
        common=original/'.git';gitdir=common/'worktrees/linked'
        roots=[linked,self.state,common,gitdir]
        before=inventory(original),inventory(linked),inventory(self.state)
        for candidate in (common,gitdir):
            with self.subTest(candidate=candidate):
                report=self.work/'linked-temp-report.json'
                result=subprocess.run([sys.executable,'-I','-B','-c',TEMP_DRIVER,str(DIAGNOSTIC),str(report),
                    json.dumps(['run',*self.common,'--plan',str(self.latest),'--plan-sha256',self.latest_hash])],
                    cwd=self.work,env=dict(self.env,TMPDIR=str(candidate)),capture_output=True,timeout=30)
                for created in json.loads(report.read_text())['created']:
                    actual=Path(created)
                    self.assertFalse(any(actual==root or root in actual.parents for root in roots),created)
                if result.returncode:self.failure(result,20)
                else:self.success(result,'observed')
                self.assertEqual((inventory(original),inventory(linked),inventory(self.state)),before)

    def assert_disjoint_merge_history(self,parent_count):
        self.profile(['dir/secret.txt'])
        (self.repo/'dir').mkdir()
        selected=['dir/'+name for name in ('a','b','c')[:parent_count]]
        for path in selected:(self.repo/path).write_text('base')
        (self.repo/'dir/secret.txt').write_text('synthetic secret')
        self.commit('selected common base')
        base=self.git('rev-parse','HEAD').stdout.decode().strip()
        main=self.git('symbolic-ref','--short','HEAD').stdout.decode().strip()
        topics=[]
        for index,path in enumerate(selected[1:],1):
            topic='topic'+str(index);topics.append(topic)
            self.git('checkout','-qb',topic,base)
            (self.repo/path).write_text('topic change '+str(index))
            self.commit('selected topic '+str(index))
        self.git('checkout','-q',main)
        (self.repo/selected[0]).write_text('main change');self.commit('selected main')
        self.git('merge','--no-ff','-m','DISJOINT_MERGE',*topics)
        merged=self.git('rev-parse','HEAD').stdout.decode().strip()
        (self.repo/'dir/secret.txt').write_text('synthetic secret update')
        self.commit('SECRET_ONLY_AFTER_MERGE')
        self.begin();before=inventory(self.repo),inventory(self.state)
        scopes=[([arg for path in selected for arg in ('--path',path)],selected)]
        scopes.append((['--under','dir'],selected))
        scopes.append((['--path',selected[0]],[selected[0]]))
        for scope,paths in scopes:
            for fmt in ('oneline','fuller'):
                for limit in (1,2,20):
                    with self.subTest(parents=parent_count,scope=scope,format=fmt,limit=limit):
                        expected=self.git('log','--no-show-signature','--no-decorate','--no-notes',
                            '--no-patch','--no-color','--format='+fmt,'--max-count='+str(limit),
                            'HEAD','--',*paths).stdout
                        if len(paths)>1:self.assertIn(merged.encode(),expected)
                        response=self.observe('log',*scope,'--format',fmt,'--limit',str(limit))
                        actual=base64.b64decode(response['data']['bytes_b64'])
                        self.assertEqual(actual,expected)
                        self.assertNotIn(b'SECRET_ONLY_AFTER_MERGE',actual)
        self.assertEqual((inventory(self.repo),inventory(self.state)),before)

    def test_r3_two_parent_disjoint_merge_matches_native_history(self):
        self.assert_disjoint_merge_history(2)

    def test_r3_three_parent_disjoint_merge_matches_native_history(self):
        self.assert_disjoint_merge_history(3)

if __name__=='__main__':unittest.main()
