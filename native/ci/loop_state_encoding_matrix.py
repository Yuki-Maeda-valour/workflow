#!/usr/bin/env python3
"""文字設定・aliases・handler・localeの承認差を個別期待で確認する。"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import check_loop_state as check
import loop_state_arguments as arguments
import loop_state_contract_cases as contracts

REASON='ERROR [loop-state] 状態観察の上限は正の整数でなければならない\n'
SETTINGS=[
 ('utf8',{},'utf-8'),('ascii',{'PYTHONIOENCODING':'ascii'},'ascii'),
 ('utf16',{'PYTHONIOENCODING':'utf-16'},'utf-16-le'),('utf16be',{'PYTHONIOENCODING':'utf-16-be'},'utf-16-be'),
 ('utf32',{'PYTHONIOENCODING':'utf-32'},'utf-32-le'),('cp932',{'PYTHONIOENCODING':'cp932'},'cp932'),
 ('cp932-alias',{'PYTHONIOENCODING':'ms932'},'cp932'),('shift-jis',{'PYTHONIOENCODING':'shift_jis'},'shift_jis'),
 ('shift-jis-alias',{'PYTHONIOENCODING':'sjis'},'shift_jis'),
 ('unknown-handler',{'PYTHONIOENCODING':'ascii:s2l_no_handler'},'ascii'),
 ('ascii-replace',{'PYTHONIOENCODING':'ascii:replace'},'ascii'),
 ('unknown-codec',{'PYTHONIOENCODING':'s2l_no_such_codec'},None),
 # 文字コードでない codec(12:39・22:24 の承認差。Current thread の値は比べない)。
 ('rot13',{'PYTHONIOENCODING':'rot13'},None),('hex',{'PYTHONIOENCODING':'hex'},None),
 ('invalid-utf8',{'PYTHONUTF8':'2'},None),
 ('ascii-filesystem',{'PYTHONUTF8':'0','PYTHONCOERCECLOCALE':'0','PYTHONIOENCODING':None,'LC_ALL':'C'},'ascii'),
 ('coercion',{'PYTHONUTF8':'0','PYTHONCOERCECLOCALE':'1','PYTHONIOENCODING':'utf-8','LC_ALL':None,'LANG':'C'},'utf-8'),
 ('nonutf8-locale',{'PYTHONUTF8':'0','PYTHONCOERCECLOCALE':'0','LC_ALL':'ja_JP.SJIS','PYTHONIOENCODING':'utf-8'},'utf-8'),
]

def old_startup(name,result):
    if result.returncode!=1 or result.stdout!=b'':return False
    if name=='invalid-utf8':return result.stderr==b'Fatal Python error: preconfig_init_utf8_mode: invalid PYTHONUTF8 environment variable value\nPython runtime state: preinitializing\n\n'
    if name in ('rot13','hex'):
        codec={'rot13':b'rot-13','hex':b'hex'}[name]
        pattern=(rb"Fatal Python error: init_sys_streams: can't initialize sys standard streams\nPython runtime state: core initialized\nLookupError: '"+codec+rb"' is not a text encoding; use codecs.open\(\) to handle arbitrary codecs\n\nCurrent thread 0x[0-9a-f]+ \(most recent call first\):\n  <no Python frame>\n")
        return re.fullmatch(pattern,result.stderr) is not None
    pattern=rb'Fatal Python error: init_stdio_encoding: failed to get the Python codec name of the stdio encoding\nPython runtime state: core initialized\nLookupError: unknown encoding: s2l_no_such_codec\n\nCurrent thread 0x[0-9a-f]+ \(most recent call first\):\n  <no Python frame>\n'
    return re.fullmatch(pattern,result.stderr) is not None

def run(fixture):
    started=time.monotonic();count=0;differences=0
    with tempfile.TemporaryDirectory(prefix='s2l-encoding-matrix-') as temporary:
        # Mac の一時の置き場(/var/folders)は /private へのリンク。旧版は祖先のリンクを辿らないので、実体のパスで比べる。
        root=Path(temporary).resolve();state=root/'state';state.write_bytes(b'{"schema":7,"stop_control":null}');out=root/'out'
        valid={'snapshot':['--out',out,'--top',root,'--common',root,'--repo-admin',root],'preflight':['--state',state,'--top',root,'--common',root,'--repo-admin',root],'derive-removed-worktree':['--state',state,'--out',out,'--removed-worktree','/removed','--own-worktree','/removed'],'stop-control':['--state',state],'state-digest':['--state',state],'state-digests':['--state',state],'compare':['--before',state,'--after',state]}
        assert set(valid)==check.COMMANDS
        for setting,delta,codec in SETTINGS:
            env=arguments.environment()
            for key,value in delta.items():
                if value is None:env.pop(os.fsencode(key),None)
                else:env[os.fsencode(key)]=os.fsencode(value)
            check.verify_child_environment(env,delta)
            for command,tail in valid.items():
                argv=[command,*map(os.fspath,tail),'--max-items','0'];old=contracts.invoke([sys.executable,'-B',str(check.LEGACY),*argv],env,root);new=contracts.invoke([str(fixture),*argv],env,root)
                assert new.returncode==20 and new.stdout==b'' and new.stderr==REASON.encode('utf-8'),f'ENCODING_FIXED_UTF8:{setting}/{command}'
                if codec is None:assert old_startup(setting,old),f'ENCODING_OLD_STARTUP:{setting}/{command}: {old!r}'
                else:assert old.returncode==20 and old.stdout==b'' and old.stderr==REASON.encode(codec,'backslashreplace'),f'ENCODING_OLD_CODEC:{setting}/{command}: {old!r}'
                assert not out.exists(),'ENCODING_LIMIT_NO_STATE';count+=1;differences+=int((old.returncode,old.stdout,old.stderr)!=(new.returncode,new.stdout,new.stderr))
        # PIPEと通常ファイルは旧UTF-16/32のBOM条件が異なる。両方の期待を固定する。
        digest=hashlib.sha256(state.read_bytes()).hexdigest()+'\n'
        for codec in ['utf-16','utf-32','cp932','ascii']:
            env=arguments.environment();env[b'PYTHONIOENCODING']=codec.encode()
            for engine,prefix in [('old',[sys.executable,'-B',str(check.LEGACY)]),('new',[str(fixture)])]:
                path=root/'regular-stdout'
                check.verify_child_environment(env,{'PYTHONIOENCODING':codec})
                with path.open('wb') as stream:
                    result=subprocess.run([*prefix,'state-digest','--state',str(state)],env=env,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.PIPE,timeout=8,check=False)
                expected=digest.encode(codec if engine=='old' else 'utf-8')
                assert result.returncode==0 and result.stderr==b'' and path.read_bytes()==expected,f'ENCODING_REGULAR_FILE:{codec}/{engine}'
        # 未知handlerは初期化時は通り、stdoutで非ASCIIを符号化する時に失敗する。
        left=root/'before';right=root/'after';left.write_bytes(contracts.canonical(contracts.BASE));right.write_bytes(contracts.canonical({**contracts.BASE,'config':['changed']}));env=arguments.environment();env[b'PYTHONIOENCODING']=b'ascii:s2l_no_handler'
        argv=['compare','--before',str(left),'--after',str(right)];old=contracts.invoke([sys.executable,'-B',str(check.LEGACY),*argv],env,root);new=contracts.invoke([str(fixture),*argv],env,root)
        source=str(check.LEGACY)
        expected=('Traceback (most recent call last):\n'+f'  File "{source}", line 2201, in <module>\n    raise SystemExit(main())\n'+f'  File "{source}", line 2194, in main\n    return compare(args)\n'+f'  File "{source}", line 2040, in compare\n    for line in diagnostic_diffs(before, after, required): print("差分: " + line)\n'+"LookupError: unknown error handler name 's2l_no_handler'\n").encode('ascii','backslashreplace')
        assert old.returncode==1 and old.stdout==b'' and old.stderr==expected,'ENCODING_UNKNOWN_HANDLER_WRITE'
        assert new.returncode==1 and new.stderr==b'' and new.stdout=='差分: config が変わった\n'.encode(),'ENCODING_UNKNOWN_HANDLER_IGNORED'
    assert count==126
    return {'encoding_matrix':count,'settings':len(SETTINGS),'regular_file':8,'unknown_handler_write':1,'approved_differences':differences,'commands':sorted(valid),'skipped':0,'seconds':round(time.monotonic()-started,3)}
