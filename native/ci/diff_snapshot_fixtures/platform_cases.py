"""実際のファイルシステムの名前・実体・時刻と、Macのsystem設定を検査する。"""
import os
from pathlib import Path
import platform
import subprocess
import tempfile

from .compare import run, same_result, unchanged

IDS = ('non-utf8-name','case-distinct','hardlink','inode-replacement','nanosecond-time','apple-system')


def compare_fixture(fixture, binary, legacy, *, codes=(0,)):
    values=[]
    for command in (['bash',str(legacy)],[str(binary)]):
        fixture.clean_outputs();before=fixture.input_state()
        result=run([*command,*fixture.normal(),'--patch-out',str(fixture.patch)],cwd=fixture.root,
                   env=fixture.env,report=fixture.out,patch=fixture.patch)
        unchanged(before,fixture.input_state())
        if result.returncode not in codes:raise AssertionError('実環境の旧停止契約と終了値が違います')
        values.append(result)
    same_result(*values)
    return values[0].returncode


def run_cases(binary, *, fixture_type, legacy):
    result=[]
    system=platform.system()
    with tempfile.TemporaryDirectory(prefix='s3d-platform-') as temporary:
        for name in IDS:
            root=Path(temporary)/name;root.mkdir();fixture=fixture_type(root)
            item={'id':'platform-'+name,'system':system}
            file=fixture.repo/'seed.txt'
            if name=='non-utf8-name':
                raw=os.fsencode(fixture.repo)+b'/bad\xff'
                try:
                    fd=os.open(raw,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                    os.write(fd,b'non-utf8 filename\n');os.close(fd)
                except OSError as error:
                    item.update(status='input-uncreatable',errno=error.errno)
                    result.append(item);continue
            elif name=='case-distinct':
                lower=fixture.repo/'case.txt';upper=fixture.repo/'CASE.TXT'
                lower.write_bytes(b'lower\n')
                try:
                    fd=os.open(upper,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                    os.write(fd,b'upper\n');os.close(fd)
                except FileExistsError:
                    item.update(status='input-uncreatable',reason='case-insensitive-filesystem')
                    result.append(item);continue
            elif name=='hardlink':
                os.link(file,fixture.repo/'linked.txt')
                if file.stat().st_ino!=(fixture.repo/'linked.txt').stat().st_ino:raise AssertionError('hardlink実体が同じでありません')
            elif name=='inode-replacement':
                before=file.stat();replacement=fixture.repo/'replacement'
                replacement.write_bytes(b'replaced\n');os.replace(replacement,file)
                after=file.stat()
                if (before.st_dev,before.st_ino)==(after.st_dev,after.st_ino):raise AssertionError('inode差替えを再現できません')
                item['inode_changed']=True
            elif name=='nanosecond-time':
                stamp=1_500_000_000_123456789
                os.utime(file,ns=(stamp,stamp))
                actual=file.stat().st_mtime_ns
                if actual!=stamp:
                    item.update(status='input-uncreatable',reason='nanosecond-rounded',requested=stamp,actual=actual)
                    result.append(item);continue
                item['nanoseconds_preserved']=True
            elif name=='apple-system':
                if system!='Darwin':
                    item.update(status='not-this-os');result.append(item);continue
                actual='/usr/bin/git'
                version=subprocess.check_output([actual,'version'])
                if 'Apple Git' not in version.decode():raise AssertionError('実Apple Gitを比較していません')
                tools=root/'apple-git';tools.mkdir();(tools/'git').symlink_to(actual)
                fixture.env['PATH']=str(tools)+os.pathsep+fixture.env['PATH']
                # 普通のfixtureの無効化だけを外し、利用者のglobal設定は引き続き隔離する。
                fixture.env.pop('GIT_CONFIG_SYSTEM',None);fixture.env.pop('GIT_CONFIG_NOSYSTEM',None)
                item['git']=actual;item['version']=version.decode().strip()
            try:item.update(status='compared',returncode=compare_fixture(fixture,binary,legacy,codes=(0,22) if name=='apple-system' else (0,)))
            except Exception as error:raise AssertionError(f'platform-{name}: {error}') from error
            if name=='inode-replacement':
                if b'+replaced\n' not in fixture.patch.read_bytes():raise AssertionError('差替え後の本文がpatchにありません')
            result.append(item)
    if [item['id'] for item in result]!=['platform-'+name for name in IDS]:raise AssertionError('実環境ケースが欠けています')
    return result
