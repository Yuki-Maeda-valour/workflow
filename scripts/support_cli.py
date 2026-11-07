"""テストの共通の補助: CLI(コマンドとして起動する入口)の実行(#388)。

テストではない(名前を test_ で始めないので、discover は拾わない)。
discover の形(`python3 -m unittest discover -s scripts -p <ファイル>`)と run-tests.py では、
scripts/ が読み込みの起点になるので `import support_cli` で読み込める。
dotted の形(`python3 -m unittest scripts.<名前>`)で流すファイルは、次のように読み込む
(test_check_process.py・test_review_guard.py の書き方):

    try:
        import support_cli
    except ModuleNotFoundError as error:
        if error.name != 'support_cli':
            raise
        from scripts import support_cli

標準ライブラリだけを使い、ほかの共通の補助を読み込まない。
"""
import contextlib
import io
import os
import subprocess
import sys
import traceback
from unittest import mock


def run(argv, *, env=None, cwd=None, timeout=None, text=True, input=None, stdin=subprocess.DEVNULL):
    """argv を子プロセスで流し、標準出力と標準エラーを受け取った結果を返す。終了コードで例外を出さない。

    input を渡したときは stdin を使わない。
    """
    feed = {'input': input} if input is not None else {'stdin': stdin}
    return subprocess.run(argv, env=env, cwd=cwd, timeout=timeout, text=text,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **feed)


def run_python(script, *args, flags=(), **kwargs):
    """[sys.executable, *flags, script, *args] を run() で流す。"""
    return run([sys.executable, *flags, script, *args], **kwargs)


def describe(done):
    """終了コード・標準出力・標準エラーをまとめた、確かめが外れたときの文。"""
    def show(value):
        if isinstance(value, bytes):
            value = value.decode('utf-8', 'replace')
        return (value or '').rstrip('\n')
    return (f'終了コード {done.returncode}\n--- 標準出力 ---\n{show(done.stdout)}'
            f'\n--- 標準エラー ---\n{show(done.stderr)}')


def call_main(main, argv0, argv, *, env=None):
    """CLI の main を同じプロセスで呼び、子プロセスで流したときと同じ形の結果を返す。

    main は引数なしで呼ぶ(引数を取る main は、呼び出し側で lambda: module.main(argv) のように包む)。
    呼ぶ間だけ os.environ を env のコピー(None なら今の環境のコピー)に、sys.argv を [argv0, *argv] に差し替え、
    標準出力と標準エラーを文字列で受け取る(bytes が要る呼び出しには使わない)。
    終了コードは子プロセスと同じにする: 戻り値の int はそのまま、None は 0。SystemExit は int ならその値、
    None なら 0、それ以外は値を標準エラーに出して 1。捕まえない例外は traceback を標準エラーに出して 1。
    cwd・umask・合図の受け方は変えない。
    """
    argv = list(argv)
    # patch.dict は値を読む前に os.environ を消すので、os.environ そのものではなくコピーを渡す
    environ = dict(os.environ if env is None else env)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, environ, clear=True), \
            mock.patch.object(sys, 'argv', [argv0, *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            value = main()
            code = 0 if value is None else value
        except SystemExit as exc:
            code = exc.code
            if code is None:
                code = 0
            elif not isinstance(code, int):
                print(code, file=sys.stderr)
                code = 1
        except Exception:
            traceback.print_exc()
            code = 1
    return subprocess.CompletedProcess([argv0, *argv], code, out.getvalue(), err.getvalue())
