"""テストの共通の補助: テストの側の git の実行(#388)。

テストではない(名前を test_ で始めないので、discover は拾わない)。
discover の形(`python3 -m unittest discover -s scripts -p <ファイル>`)と run-tests.py では、
scripts/ が読み込みの起点になるので `import support_git` で読み込める。
dotted の形(`python3 -m unittest scripts.<名前>`)で流すファイルは、次のように読み込む
(test_check_process.py・test_review_guard.py の書き方):

    try:
        import support_git
    except ModuleNotFoundError as error:
        if error.name != 'support_git':
            raise
        from scripts import support_git

標準ライブラリだけを使い、ほかの共通の補助を読み込まない。
"""
import subprocess


# git が裏で動かす保守の処理(git maintenance・git gc の自動の実行)を止める。
# commit などの後に裏で詰め直しが続くと、試験の一時領域へ書き続け、混んだ機械でファイルを取り合って落ちる(#292)。
# autoDetach=false は、保守がそれでも始まったときに、裏で続けずに終えてから戻すため。
# 製品が中で流す git は変えない。リポジトリの設定ファイルにも書かない(-c で渡すだけ)。
QUIET = ('-c', 'maintenance.auto=false', '-c', 'gc.auto=0', '-c', 'maintenance.autoDetach=false')


class GitError(subprocess.CalledProcessError):
    """終了コードが 0 でない git。失敗の文に標準エラーを含める。"""

    def __str__(self):
        error = self.stderr
        if isinstance(error, bytes):
            error = error.decode('utf-8', 'replace')
        return super().__str__() + '\n標準エラー: ' + (error or '').rstrip()


def git(*args, cwd=None, env=None, check=True, text=True, binary='git', options=(), timeout=None,
        input=None, stdin=subprocess.DEVNULL):
    """[binary, *QUIET, *options, *args] を流し、標準出力と標準エラーを受け取った結果を返す。

    input を渡したときは stdin を使わない。check なら、終了コードが 0 でないときに GitError を出す。
    """
    feed = {'input': input} if input is not None else {'stdin': stdin}
    done = subprocess.run([binary, *QUIET, *options, *args], cwd=cwd, env=env, text=text,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, **feed)
    if check and done.returncode:
        raise GitError(done.returncode, done.args, done.stdout, done.stderr)
    return done
