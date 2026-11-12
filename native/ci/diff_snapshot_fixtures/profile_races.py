"""旧profile読取り関数のOS操作だけに差替えを注入し、Rust型結果と比べる。"""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
from .compare import exact_ids

POINTS=('inspected','opened','reading','read')
ACTIONS=('parent-replace','leaf-replace','leaf-replace-mtime','overwrite','mode','grow')


def ids():return [f'profile-{point}-{action}' for point in POINTS for action in ACTIONS]


class Operations:
    def __init__(self,root,point,action):
        self.root,self.point,self.action=root,point,action
        self.changes,self.fstats,self.reads=0,0,0
        self.held_metadata_changed=None
    def __getattr__(self,name):return getattr(os,name)
    def change(self,point):
        if self.point!=point or self.changes:return
        self.changes+=1;path=self.root/'.claude/project-profile.yml'
        if self.action=='parent-replace':
            (self.root/'.claude').rename(self.root/'old-parent');path.parent.mkdir();path.write_bytes(b'REPLACEMENT')
        elif self.action in ('leaf-replace','leaf-replace-mtime'):
            fd=os.open(path,os.O_RDONLY)
            def metadata():
                value=os.fstat(fd)
                return tuple(getattr(value,key) for key in ('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns'))
            try:
                before=metadata()
                if self.action=='leaf-replace-mtime':os.utime(path,ns=(path.stat().st_atime_ns,0))
                replacement=self.root/'replacement';replacement.write_bytes(b'REPLACEMENT');os.replace(replacement,path)
                self.held_metadata_changed=before!=metadata()
            finally:os.close(fd)
        elif self.action=='overwrite':path.write_bytes(b'CHANGED')
        elif self.action=='mode':path.chmod(0o400)
        elif self.action=='grow':
            with path.open('r+b') as stream:stream.truncate(1048577)
        else:raise AssertionError('未知のprofile差替えです')
    def lstat(self,*args,**kwargs):
        value=os.lstat(*args,**kwargs);self.change('inspected');return value
    def fstat(self,*args,**kwargs):
        value=os.fstat(*args,**kwargs);self.fstats+=1
        if self.fstats==1:self.change('opened')
        return value
    def read(self,*args,**kwargs):
        self.reads+=1
        if self.reads==2:self.change('reading')
        value=os.read(*args,**kwargs)
        if not value:self.change('read')
        return value


def observe(root,point,action,helper):
    path=root/'.claude/project-profile.yml';path.parent.mkdir();path.write_bytes(b'a'*131072)
    spec=importlib.util.spec_from_file_location('s3d_profile_race',helper)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    operations=Operations(root,point,action);module.os=operations
    value,error=None,None
    try:
        data=module.current_profile(root)
        if data is None:raise AssertionError('profileが存在しない結果になりました')
        value={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    except module.ProfileError as failure:error=str(failure)
    if operations.changes!=1:raise AssertionError('profile差替え地点へ1回到達していません')
    return {'case_id':f'profile-{point}-{action}','point':'profile-'+point,'action':action,
            'changes':operations.changes,'held_metadata_changed':operations.held_metadata_changed,'value':value,'error':error}


def validate_leaf(value):
    action=value['action'];point=value['point']
    if action not in ('leaf-replace','leaf-replace-mtime'):
        if value.get('held_metadata_changed') is not None:raise AssertionError('leaf以外にmetadata差を設定しました')
        return
    changed=value.get('held_metadata_changed')
    if type(changed) is not bool:raise AssertionError('保持実体のmetadata差を記録していません')
    if action=='leaf-replace-mtime' and not changed:raise AssertionError('mtime変更を検出していません')
    expected_error=('現在の profile が読取中に差し替わった' if point=='profile-inspected'
                    else '現在の profile が読取中に変更された' if changed else None)
    expected_value=None if expected_error else {'size':131072,'sha256':hashlib.sha256(b'a'*131072).hexdigest()}
    if value['error']!=expected_error or value['value']!=expected_value:
        raise AssertionError('保持実体のmetadata差とprofile読取り結果が一致しません')


def run_cases(*,helper,typed_observations):
    exact_ids(ids(),[value['case_id'] for value in typed_observations]);typed={v['case_id']:v for v in typed_observations};done=[]
    with tempfile.TemporaryDirectory(prefix='s3d-profile-races-') as temporary:
        for point in POINTS:
            for action in ACTIONS:
                root=Path(temporary)/f'{point}-{action}';root.mkdir()
                observed=observe(root,point,action,helper)
                other=typed[observed['case_id']]
                validate_leaf(observed);validate_leaf(other)
                if action=='leaf-replace':
                    keys=('case_id','point','action','changes')
                    if any(observed[key]!=other[key] for key in keys):raise AssertionError('leaf同期点の記録が違います')
                elif observed!=other:raise AssertionError(f'{observed["case_id"]}: 旧新版のprofile差替え結果が違います')
                done.append({'case_id':observed['case_id'],'legacy':observed,'rust':other})
    return done
