"""テストの共通の補助: 製品のスクリプトの読み込みと、__pycache__ を除いたコピー(#388)。

テストではない(名前を test_ で始めないので、discover は拾わない)。
discover の形(`python3 -m unittest discover -s scripts -p <ファイル>`)と run-tests.py では、
scripts/ が読み込みの起点になるので `import support_load` で読み込める。
dotted の形(`python3 -m unittest scripts.<名前>`)で流すファイルは、次のように読み込む
(test_check_process.py・test_review_guard.py の書き方):

    try:
        import support_load
    except ModuleNotFoundError as error:
        if error.name != 'support_load':
            raise
        from scripts import support_load

標準ライブラリだけを使い、ほかの共通の補助を読み込まない。
"""
import importlib.util
from pathlib import Path
import shutil
import sys


def load_script(path, name=None, *, register=False):
    """製品のスクリプトを、__pycache__ を書かずに読み込んだモジュールを返す。

    name の既定は、ファイル名(拡張子を除く)の - を _ にしたもの。
    配る木に .pyc を残さないため、読み込む間だけ sys.dont_write_bytecode を真にし、前の値に戻す。
    register なら sys.modules に登録する(読み込みに失敗したら外す)。
    """
    path = Path(path)
    if name is None:
        name = path.stem.replace('-', '_')
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    if register:
        sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if register:
            sys.modules.pop(name, None)
        raise
    finally:
        sys.dont_write_bytecode = previous
    return module


def copy_clean(source, dest, *extra_ignore):
    """__pycache__ と、渡した型(*.pyc など)を除いて source を dest へ写し、dest を返す。

    テストが起動する python(-B なし)が配る木へ __pycache__ を書くことがあり、
    書きかけのファイルに当たって落ちうるため(run-tests.py の CHILD_FLAGS の注記)。
    """
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns('__pycache__', *extra_ignore))
    return dest
