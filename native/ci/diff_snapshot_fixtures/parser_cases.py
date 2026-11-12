"""不足値以外の空値・重複・組合せ・生バイト・十進整数を固定する。"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from .compare import exact_ids

OPTIONS=('cwd','base','out','exclude-glob','exclude','secret-profile-ref','secret-profile-union-sha256',
         'pre-untracked','pre-untracked-sha256','include-untracked','max-untracked-bytes','patch-out','patch-base','accept')
VALUES={'cwd':'@repo','base':'@base','out':'@out','exclude-glob':'.env','exclude':r'\.env$',
        'secret-profile-ref':'@base','secret-profile-union-sha256':hashlib.sha256(b'.env\0.env.*\0.dev.vars\0').hexdigest(),
        'pre-untracked':'@pre','pre-untracked-sha256':hashlib.sha256(b'').hexdigest(),
        'include-untracked':'included.txt','max-untracked-bytes':'8388608','patch-out':'@patch','patch-base':'@base','accept':'a'*64}

def definitions():
    result=[]
    for option in OPTIONS:
        for shape in ('empty','duplicate','precheck'):
            result.append({'id':f'parser-{shape}-{option}','shape':shape,'option':option})
    for option in ('exclude-glob','exclude','include-untracked','base','accept','secret-profile-ref','secret-profile-union-sha256'):
        result.append({'id':f'parser-raw-{option}','shape':'raw','option':option})
    for number in ('0','00','0001','1','8388608','1048576','9223372036854775807','9223372036854775808',
                   '18446744073709551615','18446744073709551616','9'*200,'+1',' 1','1 ','0x10','1e3','1\n2'):
        result.append({'id':f'parser-integer-{len(result)}','shape':'integer','value':number})
    for args in (['--precheck','--print-exclude-ere'],['--print-exclude-ere','--out',''],
                 ['--precheck','--max-untracked-bytes','8388608'],['--precheck','--max-untracked-bytes','0'],
                 ['--precheck','--base',''],['--precheck','--out',''],['--precheck','--secret-profile-ref',''],
                 ['--precheck','--pre-untracked',''],['--precheck','--include-untracked',''],
                 ['--print-exclude-ere','--exclude',''],['--print-exclude-ere','--exclude-glob',''],
                 ['--exclude-glob','secrets/','--exclude','^tail|head$']):
        result.append({'id':f'parser-combination-{len(result)}','shape':'combination','args':args})
    return result


def args(case,fixture):
    pre=fixture.root/'pre.nul';pre.write_bytes(b'')
    replacements={'@repo':str(fixture.repo),'@base':fixture.base,'@out':str(fixture.out),'@patch':str(fixture.patch),'@pre':str(pre)}
    def resolve(value):return replacements.get(value,value)
    option=case.get('option');shape=case['shape'];normal=fixture.normal()
    if shape=='combination':
        if case['args'][0] in ('--precheck','--print-exclude-ere'):
            return ['--cwd',str(fixture.repo),*case['args']]
        return normal+case['args']
    if shape=='integer':return normal+['--max-untracked-bytes',case['value']]
    value=resolve(VALUES[option])
    if shape=='precheck':return ['--cwd',str(fixture.repo),'--precheck','--'+option,value]
    if shape=='empty':return normal+['--'+option,'']
    if shape=='raw':return normal+['--'+option,os.fsdecode(b'bad\xff')]
    if shape=='duplicate':return normal+['--'+option,value,'--'+option,value]
    raise AssertionError('未知のparser追加入力です')


def cases():
    rows=json.loads(Path(__file__).with_name('parser.json').read_text())
    if [dict(item,expected=None) for item in rows] != [dict(item,expected=None) for item in definitions()]:
        raise AssertionError('parser追加入力の集合が変わりました')
    if any(type(item['expected']) is not int for item in rows):raise AssertionError('parserの固定終了値がありません')
    return rows


def run_cases(binary,*,fixture_type):
    done=[];rows=cases()
    with tempfile.TemporaryDirectory(prefix='s3d-parser-') as temporary:
        for case in rows:
            root=Path(temporary)/case['id'];root.mkdir();fixture=fixture_type(root)
            try:fixture.pair(binary,args(case,fixture),code=case['expected'])
            except Exception as error:raise AssertionError(f'{case["id"]}: {error}') from error
            done.append(case['id'])
    exact_ids([case['id'] for case in rows],done)
    return done
