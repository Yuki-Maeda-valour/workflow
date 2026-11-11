#!/usr/bin/env python3
"""S2L source review r1 の値、原因連鎖、walk境界を恒久照合する。"""
from __future__ import annotations
import copy
import os
from pathlib import Path
import sys
from loop_state_contract_cases import BASE, canonical
import loop_state_arguments as arguments
from loop_state_deep_fixture import DeepTree


def json_cases():
    # CPython3.10 / json.loads(bytes), UTF8+surrogateescape. Each raw
    # input is reused by both executables; expected exception text is fixed.
    return [
        ('empty', b'', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'),
        ('whitespace', b' \n\t\r', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 2 column 3 (char 4)'),
        ('not-json', b'not-json', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'),
        ('trailing', b'{} x', None, False, 1, 1, 'json.decoder.JSONDecodeError: Extra data: line 1 column 4 (char 3)'),
        ('unterminated', b'{"a":"x', None, False, 1, 1, 'json.decoder.JSONDecodeError: Unterminated string starting at: line 1 column 6 (char 5)'),
        ('control', b'{"a":"x\nz"}', None, False, 1, 1, 'json.decoder.JSONDecodeError: Invalid control character at: line 1 column 8 (char 7)'),
        ('escape', b'{"a":"\\q"}', None, False, 1, 1, 'json.decoder.JSONDecodeError: Invalid \\escape: line 1 column 7 (char 6)'),
        ('unicode-escape', b'{"a":"\\u12Q4"}', None, False, 1, 1, 'json.decoder.JSONDecodeError: Invalid \\uXXXX escape: line 1 column 8 (char 7)'),
        ('property', b'{a:1}', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'),
        ('colon', b'{"a" 1}', None, False, 1, 1, "json.decoder.JSONDecodeError: Expecting ':' delimiter: line 1 column 6 (char 5)"),
        ('comma-object', b'{"a":1 "b":2}', None, False, 1, 1, "json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 8 (char 7)"),
        ('comma-array', b'{"a":[1 2]}', None, False, 1, 1, "json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 9 (char 8)"),
        ('leading-zero', b'{"a":01}', None, False, 1, 1, "json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 7 (char 6)"),
        ('position-nonascii', b'{"\xc3\xa9":0,\n"\xf0\x9f\x98\x80":?}', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 2 column 5 (char 12)'),
        ('position-cr', b'\r\r?', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 1 column 3 (char 2)'),
        ('position-crlf', b'\r\n?', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 2 column 1 (char 2)'),
        ('utf8-invalid-start', b'\xff', None, False, 1, 1, "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"),
        ('utf8-invalid-continuation', b'{"a":"\xc3("}', None, False, 1, 1, "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc3 in position 6: invalid continuation byte"),
        ('utf8-truncated', b'{"a":"\xe2\x82', None, False, 1, 1, "UnicodeDecodeError: 'utf-8' codec can't decode bytes in position 6-7: unexpected end of data"),
        ('utf8-out-of-range', b'{"a":"\xf4\x90\x80\x80"}', None, False, 1, 1, "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xf4 in position 6: invalid continuation byte"),
        ('utf16le-truncated', b'\xff\xfe{\x00}', None, False, 1, 1, "UnicodeDecodeError: 'utf-16-le' codec can't decode byte 0x7d in position 4: truncated data"),
        ('utf16be-truncated', b'\xfe\xff\x00{}', None, False, 1, 1, "UnicodeDecodeError: 'utf-16-be' codec can't decode byte 0x7d in position 4: truncated data"),
        ('utf32le-truncated', b'\xff\xfe\x00\x00{\x00\x00', None, False, 1, 1, "UnicodeDecodeError: 'utf-32-le' codec can't decode bytes in position 4-6: truncated data"),
        ('utf32be-out-of-range', b'\x00\x00\xfe\xff\x00\x11\x00\x00', None, False, 1, 1, "UnicodeDecodeError: 'utf-32-be' codec can't decode bytes in position 4-7: code point not in range(0x110000)"),
        ('bom-utf8-only', b'\xef\xbb\xbf', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'),
        ('bom-utf16-only', b'\xff\xfe', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'),
        ('bom-utf32-only', b'\xff\xfe\x00\x00', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'),
        ('bom-utf8-double', b'\xef\xbb\xbf\xef\xbb\xbf{}', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'),
        ('utf16le-no-bom-syntax', b'{\x00\n\x00"\x00\xe9\x00"\x00:\x00?\x00}\x00', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 2 column 5 (char 6)'),
        ('utf32be-no-bom-syntax', b'\x00\x00\x00{\x00\x00\x00\n\x00\x00\x00"\x00\x01\xf6\x00\x00\x00\x00"\x00\x00\x00:\x00\x00\x00?\x00\x00\x00}', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 2 column 5 (char 6)'),
        ('deep-escape-988', b'{"x":'+b'['*988+b'"\\x"'+b']'*988+b'}', None, False, 1, 1, 'json.decoder.JSONDecodeError: Invalid \\escape: line 1 column 995 (char 994)'),
        ('deep-escape-989', b'{"x":'+b'['*989+b'"\\x"'+b']'*989+b'}', None, False, 1, 1, 'RecursionError: maximum recursion depth exceeded while calling a Python object'),
        ('deep-escape-990', b'{"x":'+b'['*990+b'"\\x"'+b']'*990+b'}', None, False, 1, 1, 'RecursionError: maximum recursion depth exceeded'),
        ('deep-escape-991', b'{"x":'+b'['*991+b'"\\x"'+b']'*991+b'}', None, False, 1, 1, 'RecursionError: maximum recursion depth exceeded while calling a Python object'),
        ('deep-unknown-991', b'{"x":'+b'['*991+b'x'+b']'*991+b'}', None, False, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 1 column 997 (char 996)'),
        ('deep-delimiter-988', b'{"x":'+b'['*988+b'[0 0]'+b']'*988+b'}', None, False, 1, 1, 'RecursionError: maximum recursion depth exceeded while calling a Python object'),
        ('deep-null-991', b'{"x":'+b'['*991+b'null'+b']'*991+b'}', None, False, 1, 1, None),
        ('deep-null-992', b'{"x":'+b'['*992+b'null'+b']'*992+b'}', None, False, 1, 1, 'RecursionError: maximum recursion depth exceeded while decoding a JSON array from a unicode string'),
        ('integer-640', b'{"x":'+b'9'*640+b'}', '640', False, 1, 1, None),
        ('integer-641', b'{"x":'+b'9'*641+b'}', '640', False, 1, 1, 'ValueError: Exceeds the limit (640) for integer string conversion: value has 641 digits; use sys.set_int_max_str_digits() to increase the limit'),
        ('integer-default-4301', b'{"x":'+b'9'*4301+b'}', None, False, 1, 1, 'ValueError: Exceeds the limit (4300) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit'),
        ('integer-disabled-4301', b'{"x":'+b'9'*4301+b'}', '0', False, 1, 1, None),
        ('non-object', b'[]', None, False, 1, 1, None),
        ('valid-full-schema', (canonical(BASE).decode('ascii')).encode('utf-8','surrogatepass'), None, False, 0, 0, None),
        ('valid-utf8-bom', (canonical(BASE).decode('ascii')).encode('utf-8-sig','surrogatepass'), None, False, 0, 0, None),
        ('valid-utf16-bom', (canonical(BASE).decode('ascii')).encode('utf-16','surrogatepass'), None, False, 0, 0, None),
        ('valid-surrogatepass-utf8', ((canonical(BASE)[:-1]+b',"x":"'+b'\xed\xa0\x80'+b'"}').decode('utf-8','surrogatepass')).encode('utf-8','surrogatepass'), None, False, 0, 0, None),
        ('after-empty', b'', None, True, 1, 1, 'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'),
        ('after-utf8-invalid', b'\xff', None, True, 1, 1, "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"),
        ('after-integer-641', b'{"x":'+b'9'*641+b'}', '640', True, 1, 1, 'ValueError: Exceeds the limit (640) for integer string conversion: value has 641 digits; use sys.set_int_max_str_digits() to increase the limit'),
        ('utf8-bom-invalid-body', b'\xef\xbb\xbf\xff', None, False, 1, 1, "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"),
        ('utf8-partial-bom', b'\xef\xbb', None, False, 1, 1, "UnicodeDecodeError: 'utf-8' codec can't decode bytes in position 0-1: unexpected end of data"),
        ('valid-surrogatepass-utf16', ((canonical(BASE)[:-1]+b',"x":"'+b'\xed\xa0\x80'+b'"}').decode('utf-8','surrogatepass')).encode('utf-16','surrogatepass'), None, False, 0, 0, None),
        ('valid-surrogatepass-utf32', ((canonical(BASE)[:-1]+b',"x":"'+b'\xed\xa0\x80'+b'"}').decode('utf-8','surrogatepass')).encode('utf-32','surrogatepass'), None, False, 0, 0, None),
        ('valid-utf32be-bom', b'\0\0\xfe\xff'+(canonical(BASE).decode('ascii')).encode('utf-32-be','surrogatepass'), None, False, 0, 0, None),
    ]


def json_causes(pair):
    folder=pair.work/'r1-json';folder.mkdir();left=folder/'before';right=folder/'after'
    for name,raw,limit,after,normal_exit,fault_exit,cause in json_cases():
        left.write_bytes(canonical(BASE) if after else raw);right.write_bytes(raw)
        identities=[arguments.identity(p) for p in (left,right)]
        argv=['compare','--before',left,'--after',right]
        delta={'PYTHONUNBUFFERED':'1','PYTHONINTMAXSTRDIGITS':str(limit) if limit is not None else '4300'}
        routes=['pipe','closed-pipe']+(['full']if sys.platform.startswith('linux')else[])
        for route in routes:
            expected=normal_exit if route=='pipe' else fault_exit
            result,_=pair.run('R1-json-'+name+'-'+route,argv,expected,delta=delta,group='r1_json',out_route=route)
            if route!='pipe' and cause is not None:
                assert cause.encode('utf-8') in result.stderr,'R1_JSON_FIXED_CAUSE'
                if cause.startswith('RecursionError:'):
                    assert b'__main__.Stop:' not in result.stderr,'R1_RECURSION_NOT_CAUGHT'
                else:
                    assert b'The above exception was the direct cause' in result.stderr and b'During handling of the above exception' in result.stderr,'R1_JSON_CAUSE_CHAIN'
        assert identities==[arguments.identity(p)for p in (left,right)],'R1_JSON_INPUT_CHANGED'


def self_pair(reference='refs/heads/self'):
    before=copy.deepcopy(BASE)
    before.update(worktrees=[{'worktree':'/self','branch':reference,'HEAD':'a','locked':''}],
                  worktree_contents=[{'path':'/self','index':{'head_clean':True}}],
                  worktree_configs=[{'path':'/self','private_refs':{'head':{'value':'a'}}}],
                  refs=[{'name':reference,'oid':'a','symref':''}])
    after=copy.deepcopy(before);after['worktrees'][0]['HEAD']='b';after['refs'][0]['oid']='b'
    after['worktree_configs'][0]['private_refs']['head']={'value':'b'}
    return before,after


def compare_values(pair):
    folder=pair.work/'r1-compare';folder.mkdir();left=folder/'before';right=folder/'after'
    def run(name,before,after,expected=1,reference=None):
        left.write_bytes(canonical(before));right.write_bytes(canonical(after))
        identities=[arguments.identity(p)for p in (left,right)]
        argv=['compare','--before',left,'--after',right]
        if reference is not None:argv+=['--self-worktree','/self','--self-ref',reference]
        result,_=pair.run('R1-'+name,argv,expected,group='r1_compare')
        assert identities==[arguments.identity(p)for p in (left,right)],'R1_COMPARE_INPUT_CHANGED'
        return result
    for suffix in ['\udcff','\ud800','a\ud800']:
        before=copy.deepcopy(BASE);after=copy.deepcopy(BASE)
        before['refs']=[{'name':'refs/heads/'+suffix,'oid':'a'}]
        after['refs']=[{'name':'refs/heads/'+suffix,'oid':'b'}]
        result=run('ref-surrogate-'+ascii(suffix),before,after)
        if suffix=='\udcff':assert b'refs/heads/\xff: ref' in result.stdout,'R1_REF_RAW_BYTE'
        else:assert result.stdout=='差分: refs が変わった\n'.encode() and b'UnicodeEncodeError:' in result.stderr,'R1_REF_PARTIAL_PRINT'
    for last_changed in [False,True]:
        before=copy.deepcopy(BASE);after=copy.deepcopy(BASE)
        before['refs']=[{'name':'refs/heads/a','oid':'a'},{'name':'refs/heads/a','oid':'z'}]
        after['refs']=copy.deepcopy(before['refs']);after['refs'][int(last_changed)]['oid']='b'
        result=run('duplicate-last-'+str(last_changed),before,after)
        assert (b'refs/heads/a: ref' in result.stdout)==last_changed,'R1_DUPLICATE_LAST'
    reference='refs/heads/raw-\udcff';before,after=self_pair(reference)
    remote='refs/remotes/origin/raw-\udcff'
    before['refs'].append({'name':remote,'oid':'a','symref':''});after['refs'].append({'name':remote,'oid':'b','symref':''})
    run('self-remote-surrogate',before,after,0,reference)
    for value in [None,0,True,'x',{}, {'x':0},[{}],[None]]:
        before,after=self_pair();after['worktree_configs']=value
        result=run('after-config-'+repr(value),before,after,reference='refs/heads/self')
        if value not in ({},):assert b'TypeError:' in result.stderr or b'KeyError:' in result.stderr,'R1_AFTER_CONFIG_EXCEPTION'
    for name,rows in [('empty',[]),('matched-before-invalid',[{'path':'/self','private_refs':{'head':{'value':'b'}}},None]),('invalid-before-match',[None,{'path':'/self','private_refs':{'head':{'value':'b'}}}])]:
        before,after=self_pair();after['worktree_configs']=rows
        run('after-config-'+name,before,after,reference='refs/heads/self')
    for value in [None,0,[],True,'x']:
        before,after=self_pair();after['worktree_contents'][0]['index']=value
        result=run('index-'+repr(value),before,after,reference='refs/heads/self')
        assert b'AttributeError:' in result.stderr and result.stdout==b'','R1_INDEX_ATTRIBUTE_ERROR'
    before,after=self_pair();del after['worktree_contents'][0]['index']
    result=run('index-missing-default',before,after,reference='refs/heads/self');assert not result.stderr,'R1_INDEX_DEFAULT'
    before,after=self_pair();after['worktree_contents'][0]['index']=None;after['worktrees'][0]['branch']='refs/heads/other'
    result=run('index-earlier-short-circuit',before,after,reference='refs/heads/self');assert not result.stderr,'R1_INDEX_SHORT_CIRCUIT'


def walks(pair):
    folder=pair.work/'r1-walk';folder.mkdir();previous=pair.env
    pair.env={k:v for k,v in previous.items()if not k.startswith(b'GIT_')}
    pair.env.update({b'HOME':os.fsencode(folder),b'XDG_CONFIG_HOME':os.fsencode(folder/'xdg'),b'GIT_CONFIG_GLOBAL':b'/dev/null',b'GIT_CONFIG_NOSYSTEM':b'1'})
    contexts=[('ordinary',None,[(990,False,0),(991,False,1),(989,True,0),(990,True,1)]),
              ('secret',['d'],[(992,False,0),(993,False,1),(992,True,0),(993,True,1)]),
              ('hooks',None,[(991,False,0),(992,False,1),(989,True,0),(990,True,1),(991,True,1)]),
              ('empty-profile',[],[(990,False,0),(991,False,1),(989,True,0),(990,True,1)])]
    try:
        for context,patterns,cases in contexts:
            repo=folder/context;repo.mkdir()
            for tail in [['-c','init.defaultBranch=main','init','-q'],['-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','baseline']]:
                result=arguments.run(['git','-C',str(repo),*tail],pair.env,folder);assert result.returncode==0,result.stderr
            if patterns is not None:
                (repo/'.claude').mkdir();(repo/'.claude/project-profile.yml').write_text('secret_paths: '+__import__('json').dumps(patterns)+'\n')
            anchor=repo/'.git/hooks'if context=='hooks'else repo
            deep=DeepTree(anchor,995)
            try:
                for depth,leaf,expected in cases:
                    held=folder/'held-tail';deep.detach(depth,held)
                    out=folder/'out'
                    try:
                        if leaf:deep.leaf(depth,b'leaf body')
                        identities=deep.identity(depth,leaf)
                        result,saved=pair.run(f'R1-walk-{context}-{depth}-{leaf}',['snapshot','--out',out,'--top',repo,'--common',repo/'.git','--repo-admin',repo/'.git'],20 if sys.platform=='darwin'else expected,output=out,group='r1_walk')
                        if sys.platform=='darwin':assert saved is None and result.returncode==20,'R1_MAC_WALK_NOT_REACHED'
                        elif expected:
                            assert saved is None and b'RecursionError:' in result.stderr and result.stdout==b'','R1_WALK_RECURSION_NO_SAVE'
                        else:assert saved is not None and saved[1]==0o600,'R1_WALK_STATE_CREATED'
                        assert identities==deep.identity(depth,leaf),'R1_WALK_BODY_UNCHANGED'
                    finally:
                        if leaf:deep.leaf(depth,None)
                        deep.restore(depth,held)
            finally:deep.close()
    finally:pair.env=previous


def state_stops(pair):
    folder=pair.work/'r1-state-stop';folder.mkdir();good=folder/'good';good.write_bytes(canonical(BASE))
    directory=folder/'directory';directory.mkdir();fifo=folder/'fifo';os.mkfifo(fifo)
    link=folder/'link';link.symlink_to(good);large=folder/'large';large.write_bytes(canonical(BASE));tiny=folder/'tiny';tiny.write_bytes(b'{}');parent_link=folder/'parent-link';parent_link.symlink_to(directory)
    paths=[directory,fifo,link,large,good,tiny,parent_link]
    def identities():
        return [(p.lstat().st_ino,p.lstat().st_mode,p.lstat().st_size,p.lstat().st_mtime_ns,p.lstat().st_ctime_ns)for p in paths]
    before=identities()
    for name,path in [('directory',directory),('fifo',fifo),('link',link),('size',large),('parent-missing',folder/'absent'/'state'),('parent-file',good/'state'),('parent-link',parent_link/'state')]:
        for after in [False,True]:
            argv=['compare','--before',tiny if name=='size'and after else good if after else path,'--after',path if after else good]
            if name=='size':argv+=['--max-bytes','2'if after else '1']
            for route in ['pipe','closed-pipe']+(['full']if sys.platform.startswith('linux')else[]):
                result,_=pair.run(f'R1-state-stop-{name}-{after}-{route}',argv,1,delta={'PYTHONUNBUFFERED':'1'},group='r1_state_stop',out_route=route)
                if route!='pipe':
                    assert b'in read_state_bytes\n' in result.stderr and b'in read_state\n' in result.stderr,'R1_STATE_STOP_TRACE'
                    assert (b'line 1962, in compare'if after else b'line 1961, in compare')in result.stderr,'R1_STATE_STOP_READ_ORDER'
    assert before==identities(),'R1_STATE_STOP_INPUT_CHANGED'
