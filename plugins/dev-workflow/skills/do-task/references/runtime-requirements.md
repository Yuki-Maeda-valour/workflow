# 必要環境と確認手順

Phase 0 の事前検査より前に、使用するシェルと道具を確認する。profile の有無によらず適用する。

## 実行環境

- 開始前の `pending-implementation.py scan` は **Python 3.8 以上・POSIX** が必要。標準ライブラリだけを使う。非 Git・内蔵・検証のみ・profile なしでも適用する。
  - `os.open`・`os.close`・`os.fstat`、`O_DIRECTORY`・`O_NOFOLLOW`・`O_CLOEXEC`、fd(開いたディレクトリを指す番号)を受ける `os.scandir` が必要。
  - `os.stat` の `dir_fd` と `follow_symlinks=False`、`st_dev`・`st_ino`・`st_mode`・`st_uid`・`st_size`・`st_mtime_ns`・`st_ctime_ns`、`os.fsencode`・`os.fsdecode`・`time.monotonic` も必要。
  - helper は起動時の機能検査と実利用時の未対応を 11/`scan-unavailable` にする。`listdir` や shell での代行へ切り替えない。
  - Python 不在・helper 不在・起動不能・解釈失敗なら停止する。JSON を受け取れた扱いにせず、検査を省いて続行しない。引数・結果・人の確認の正本(ほかが合わせる元)は [external-runners.md](external-runners.md) §12-9。
  - Python 3.8 互換の構文・API を保つ。3.8 実機が無ければ未確認とし、別版での成功を実測としない。既存の macOS 実測はこの新しい helper の実機確認を含まない。

- Git 対象の内蔵 implementer(実装を受け持つ AI)へ委託(作業を別の AI に任せること)する前は、次の確認を独立した bash で実行する。`$1` は解決した do-task の `scripts` ディレクトリとする。PATH で選んだ同じ Python 実体を使う。エントリポイントと通常の Git は起動しない。

<!-- implementation-git-runtime-check:start -->
```bash
implementation_scripts=$1
implementation_python=$(command -v python3) || { echo 'ERROR: python3 が無い' >&2; exit 20; }
"$implementation_python" -I -B - "$implementation_scripts" <<'PYTHON' || exit 20
import argparse, ast, hashlib, json, os, pathlib, re, subprocess, sys
if sys.version_info[0] != 3:
    raise RuntimeError("Python 3 が必要")
print("Python", sys.version.split()[0])
root = pathlib.Path(sys.argv[1])
for name in ("implementation-git.py", "diff-snapshot.sh"):
    path = root / name
    if not path.is_file() or not os.access(str(path), os.R_OK):
        raise RuntimeError("依存ファイルを読めない: " + name)
    source = path.read_text(encoding="utf-8")
    if name.endswith(".py"):
        ast.parse(source, filename=str(path))
PYTHON
```
<!-- implementation-git-runtime-check:end -->

  使う標準ライブラリが増えたら、この import 一覧も更新する。現在の `--precheck` は他の同梱 helper を呼ばないため、必須ファイルは上の2本とする。
  `secret-profiles.py` は通常の snapshot の `--secret-profile-ref` 用であり、この入口の必要条件には含めない。事前検査は bash 経由で読むため実行ビットは要求しない。bash・GNU 道具は下記の既存確認も通す。
  Python の不在・起動不能・import や構文確認の失敗、依存の不在・読込不能・起動不能では、理由を報告する。内蔵 implementer の起動・再依頼をせず停止する。
  本文の照合(照らし合わせて確かめること)にある「Python が無ければ報告して続行」の例外は適用しない。素の Git、shell での代行、外部実装への切り替えでは続行しない。環境の修復後に確認からやり直す。
  `prepare` または `run` で後から必要環境の喪失が判明しても、その読取りは停止して委託元へ報告する。既に動く担当や子孫の自動停止を保証しない。
  profile が無い場合・一部 skill だけの導入でも同じ条件とする。非 Git は既存の判定に従い、この要件を適用しない。引数と停止後の扱いは [implementation-git.md](implementation-git.md) を読む。

- `diff-snapshot.sh` と `implement-guard.sh` は連想配列を使うため **bash 4.0 以上**が必要。`--help` と `--precheck` にも同じ要件がかかる。`command -v bash` と `bash --version` で、PATH から選ぶ実体と版を確認する。ログインシェルの版だけでは判断しない。
- 本体の必要道具は GNU coreutils・GNU findutils・GNU grep。実行名 `realpath`・`readlink`・`sha256sum`・`sort`・`grep`・`find` などで PATH から選ぶ。特に `grep -z`・`sort -z` と、gitlink 検査の `find -mindepth/-maxdepth/-quit` が必要。導入しても別名や PATH の後ろにあれば選ばれないため、`command -v <実行名>` と `<実行名> --version` を記録する。
- `diff-snapshot-selftest.sh` は bash 4.0 以上と GNU coreutils・GNU findutils・GNU grep が必要。冒頭で `sha256sum`・`touch -d`・`find -maxdepth/-quit`・`head -c`・`grep -z`・`sort -z` を検査し、不足は rc 2 / `ERROR [requirements]` で止まる。後で時刻固定が失敗しても rc 2 で停止する。必要道具の不足と、時刻を固定できても stat キャッシュの隠蔽を再現できない正当な fixture 不成立を区別する。
- 回帰一式の必要環境は **Linux と GNU 系ツール**。無人ループ `loop.sh` は **Linux・bash 4.4 以上**が必要で、`setsid`・`flock`・`/proc` を使う。詳しくは [../../ship-task/references/loop.md](../../ship-task/references/loop.md) を読む。

## 設定を戻さずに使う診断

[diagnostic-git.md](diagnostic-git.md) の `diagnostic-git.py` は Python 3.10 以上・POSIX を必要とする。既存の内蔵5操作と開始前の候補走査の下限は変えない。

- 信頼する配布コピーの絶対パスへ `python3 -I` を使う。同梱モジュールも同じコピーの絶対パスから読み、作業場所や `PYTHONPATH` の同名ファイルを使わない。
- Git は既存の安全用前置きと版要件、GNU `grep` は `-E -z -f` と `LC_ALL=C` が必要。実体を固定する。
- `os.O_NOFOLLOW`・`O_DIRECTORY`・`O_NONBLOCK`、bytes の名前、`open/stat/readlink` の `dir_fd`、`fstat`、fd からのディレクトリ列挙、単調時計が必要。
- `subprocess.Popen` と直接子の停止・回収、私有pipeの有限送受信、`signal.pthread_sigmask` と TERM・INT・HUP が必要。Git より前に存在と専用領域の正常操作を検査し、不足は元データを変えず終了20で止める。
- profile が1つでもあれば既存 `secret-profiles.py` と同じ PyYAML が必要。すべて不在なら PyYAML を要求しない。
- OS 名だけで拒否しない。新しい診断の macOS 実機は未確認であり、下記の既存実測には含めない。
- 全操作300秒・Git/grep各30秒・直接子の TERM5秒＋KILL5秒・後始末5秒を使う。最終出力も残り時間に含む。GNU `timeout` は診断の新しい必須条件にしない。
- 完成応答だけを直接子へ渡し、私有pipeを監督する。共有する stdout/stderr の flags は変更しない。無応答・非0・不正応答は結果不明として停止する。
- 通常の読み手停止は監督で打ち切る。OS の入出力が戻らない場合、同じ利用者権限での改変、元のオブジェクト保存先の同時更新、任意子孫の完全停止は保証しない。

## タスク本文の復元

`implement-guard.sh restore-taskmd` は Python 3 と同梱 `restore-taskmd.py` を追加で必要とする。他のサブコマンドの要件は変えない。OS 名で拒否せず、次の機能を起動時と専用領域内の試験で確認する。

- `os.O_DIRECTORY`・`O_NOFOLLOW`・`O_NONBLOCK`・`O_CLOEXEC`、`open`・`stat`・`mkdir`・`unlink`・`rmdir` の `dir_fd`、`stat` の `follow_symlinks=False`、`fstat`、`replace` の `src_dir_fd` / `dst_dir_fd`。
- `subprocess.Popen` の `close_fds=True` / `pass_fds`、直接子の `poll` / `wait` / `terminate` / `kill`、`signal.pthread_sigmask` と TERM・INT・HUP、単調時計、`resource.RLIMIT_FSIZE` / `setrlimit`。
- GNU `cp` の固定した実体。GNU coreutils 8.30 以上が必要(`--reflink=never` を受け付ける版。coreutils の NEWS による)。`cp` には `--reflink=never`(複製の共有をしない指定)を渡す。`--version` を8KiB・5秒で検査する。fd 別名は `/proc/self/fd`、次に `/dev/fd` を試す。開いた通常ファイルと同じ device/inode を指すことと、短いコピーの内容・mode・inode を確認する。 <!-- validate-allow: OS が提供する fd 別名を必要機能として明記するため -->
  - `--reflink=never` を渡す理由: GNU `cp` 9 系は、複製の共有を禁じないと、書き込みの上限(`RLIMIT_FSIZE`)の位置でカーネルのコピーを呼び、`SIGXFSZ`(書き込みの上限を越えたときの信号)で止まる。上限は緩めない。
  - 確かめた版(2026-11-01、[Issue #285](https://github.com/Yuki-Maeda-valour/workflow/issues/285) で直した後): Linux の 8.32(Ubuntu 22.04)と 9.7(Debian trixie)で復元できる。macOS の 9.12 は `fd-copy-unavailable` で止まり、対象を変えない。
  - 確かめていない版: Linux の 8.30・8.31・9.0〜9.6・9.8 以降と、8.30 より前。
  - 8.30 より前の `cp` は `--reflink=never` を受け付けない(NEWS による)。そのときは、どの別名でも試しのコピーが失敗し、`fd-copy-unavailable` で止まる。
    - この止まる動きは、`--reflink=never` を断って終了値 1 で終わる `cp` の代わりの実行物の試験(`scripts/test_restore_taskmd.py`)で確かめた。8.30 より前の実機では未確認。
- 必須 API や実行物が無ければ、対象を変えず `runtime-unavailable` で停止する。fd 別名を通した短いコピーが、どの別名でも成り立たなければ、対象を変えず `fd-copy-unavailable` で停止する。対象親内の試験失敗も対象へ公開しない。一時物の残存・回収状態は `TEMP` で報告する。通常のパスコピーへ戻さない。
- Linux の実測は Python 3.10.12。mode・同一所有者の uid/gid・ns mtime・利用可能な通常の user xattr・POSIX ACL を GNU `cp -a` と比較する。読取り後の atime 不変、特権属性、異なる所有者、SELinux 属性の新たな保証はしない。
- macOS では、`/dev/fd/<番号>` を `stat` で調べた device 番号が、開いたファイル(`fstat`)と違う(inode は同じ)。
  - 補助の `restore-taskmd.py` は、専用領域内の試験でこの食い違いを見つけ、`cp` を起動する前に `fd-copy-unavailable` で止まり、対象を変えない(#285 で直した後に Mac の実機で確認)。
  - GNU `cp` を直接起動しても「コピー中に差し替えられた」と断る(`cp: skipping file '/dev/fd/3', as it was replaced while being copied`)。
  - 通常のパスでのコピーへは戻さない。そのため、正常な復元は macOS では確かめられない。実機の確認の記録は [Issue #271](https://github.com/Yuki-Maeda-valour/workflow/issues/271) に置く。Linux の別名切替試験は macOS の実測に数えない。
- Python パッケージの追加はない。API の有無と実際の操作を検査し、不足は無変更で止める。構文・API の不要な版引上げはしない。

公開入口全体は GNU `timeout` で必ず監督する。次の関数を呼出側で定義する。第1引数は同梱 `implement-guard.sh`、残りは既存の各引数とする。`timeout` が無ければ `gtimeout` を選ぶ。GNU 版・必要オプションを確認できなければ復元を起動しない。

<!-- restore-taskmd-supervision:start -->
```bash
restore_taskmd_supervised() {
  local supervisor banner probe guard_script
  guard_script="$1"; shift
  supervisor="$(type -P timeout)" || supervisor="$(type -P gtimeout)" || {
    printf '%s\n' '復元を起動しない: GNU timeout が必要です。' >&2
    return 125
  }
  banner="$("$supervisor" --version 2>/dev/null)" || return 125
  case "$banner" in 'timeout (GNU coreutils)'*) ;; *) return 125 ;; esac
  probe="$(type -P true)" || return 125
  "$supervisor" --signal=TERM --kill-after=5s 1s "$probe" </dev/null >/dev/null 2>&1 || return 125
  "$supervisor" --signal=TERM --kill-after=5s 330s bash "$guard_script" restore-taskmd "$@"
}
```
<!-- restore-taskmd-supervision:end -->

呼出しは `restore_taskmd_supervised <implement-guard.sh> --cwd <管理ルート> --state <保護領域> --manifest-sha256 <hex> --snapshot-sha256 <hex>` とし、各値を独立した引数で渡す。shell 文字列へ組み立てず、`--foreground` を足さない。
330秒後の TERM と追加5秒後の KILL は、今回起動したプロセス群だけを対象とする。保存 PID や他の利用者プロセスを探索しない。124・137・監督の失敗・欠落/不正な応答は `TOUCHED=unknown / TEMP=unknown` として止め、自動再実行や推測した一時物の削除をしない。通常の pipe 送出待ちも打ち切る。カーネル I/O が KILL でも終了しない場合は335秒以内の停止確認を保証せず、停止未確認として保持する。

結果形式、300秒の作業予算、子の停止・回収、同じ UID の競合限界は [external-runners.md](external-runners.md) §12-2 が正本。

## 品質コマンドの監督

`review-guard.py run-checks` は、[check-process.py](../../ship-task/scripts/check-process.py) から品質コマンドを起動する。
停止とコピー保持の手順は [review-protocol.md](review-protocol.md#品質コマンドの停止とコピーの保持)が正本。

- **Linux**: `subreaper`(親が終了した子孫を引き取る仕組み)、`/proc`、`pidfd`(特定のプロセスを指す参照)が必要。Python の `os.pidfd_open`・`signal.pidfd_send_signal` を使う。専用プロセスで既存の [loop-supervisor.py](../../ship-task/scripts/loop-supervisor.py) を使い、子孫の終了と回収を確認する。必要な機構を使えなければ停止し、自動で他 POSIX の方式へ切り替えない。
- **他の POSIX 環境**: 専用のプロセス群を停止し、その群の不在を確認する。群の番号を保持する親は、最後の停止信号まで回収しない。番号の再利用で別の処理を止めることを避ける。
  - 親の回収後は signal 0 による確認だけを行う。`EPERM` は不在とせず、既存の 5 秒の期限内で再確認する。`ESRCH` だけを不在とし、期限内に確認できなければコピーを保持して止まる。
  - 検証の親は、群の停止(最後の `SIGKILL`・anchor の回収・不在の確認)を終えるまで、anchor(品質コマンドを起動し、群の停止まで残る補助プロセス)の通知(anchor が品質コマンドの終了値を書いて知らせるパイプ)の読み口(読む側)を閉じない。
    - anchor は、品質コマンドが `TERM`(停止の信号)で終わった後も、通知を書いて `SIGKILL` まで残る。
    - 群の全員が `TERM` で終わっても、時間切れは時間切れになる。検証の親への `TERM`・`INT`・`HUP` は、停止の信号を受けた結果(`終了通知 N を受けた`)になる。([Issue #289](https://github.com/Yuki-Maeda-valour/workflow/issues/289) で直した)
  - 限界: 次の 3 つの場合がある。どの場合も、`EPERM` を群の不在とは扱わない。
    - anchor 自身が失われた(異常終了・外からの強制終了)後に、群の全員が `TERM` で終わる場合: macOS は群への `SIGKILL` を `EPERM` で返し、診断の文が `Operation not permitted` になる。コピーを保持して終了値 2 で止まる結果は変わらない。
    - anchor の起動中(`TERM`・`INT`・`HUP` を受け流す処理を置く前。Python の起動中)に、検証の親が停止の信号を受ける場合: `TERM` で anchor 自身が終わり、同じく診断の文が `Operation not permitted` になる。終了値 2・コピー保持は、停止の信号の本来の結果と同じ。
    - `TERM` から `SIGKILL` までの待ち(既定 0.2 秒)の間に、検証の親が強制終了される(`SIGKILL` など)場合: 群への `SIGKILL` が送られない。anchor と、`TERM` を無視した群の一員(品質コマンドやその子孫)が、親を失ったまま残る。
      - anchor は `TERM`・`INT`・`HUP` では止まらない。
      - 通常終了の経路でも、品質コマンドが自分で終わった後から `SIGKILL` までの間に検証の親が強制終了されると、同じことが起きる。
      - 検証の親が終わるので、コピーは消えない。保持先も診断も表示されない。見つけ方と止め方は次のとおり。
      - 残った anchor と群: anchor のコマンド行は `check-process.py` と `--anchor-fd` を含み、親は 1(launchd)。群の番号は anchor の番号と同じ。同じ群に、`TERM` を無視した品質コマンドやその子孫が残る場合がある。
      - 残った処理は、群の番号ではなく、一員を 1 つずつ止める。`ps -axo pid,ppid,pgid,command` で、群の番号が anchor の番号と同じで、コマンド行がその検証のもの(anchor は `check-process.py` と `--anchor-fd` を含む。品質コマンドは計画のコマンド)であることを確かめてから、その番号へ `kill -KILL` を送る。anchor は `TERM`・`INT`・`HUP` では止まらない。
      - 群の番号で送らない理由: anchor が通知を書き終えたかを外から確かめられず、確かめてから送るまでの間に anchor が終わって群の番号が再利用されうるため。
      - 1 つずつ送る場合も、確かめてから送るまでの間に番号が再利用されうる(ほかの手動の停止と同じ限界)。
      - 送った後に、その群の番号の一員が残っていないことを `ps` で確かめる。
      - 消えなかったコピー: `TMPDIR` の下の `review-guard-clean-*`(検証用コピー)と `review-guard-checks-*`(出力の置き場)。その検証が起動した処理の停止を確かめてから消す。
      - そのうえで、[review-protocol.md](review-protocol.md#品質コマンドの停止とコピーの保持) の「品質コマンドの停止とコピーの保持」に従い、検証を最初からやり直す。
- 他 POSIX で `setsid` などにより群の外へ移った子孫は保証外。切離しを行う検証には、Linux の監督か OS の隔離を使う。Linux でも、同じ利用者権限による監督の改変などを完全に隔離するものではない。
- 検証用コピーを作る `git commit` には `-c maintenance.auto=false -c gc.auto=0` を渡し、Git の裏の保守処理を起動させない。([Issue #292](https://github.com/Yuki-Maeda-valour/workflow/issues/292) で直した)
  - 裏の保守処理: `git commit` が終わる前に起動する `git maintenance run --auto`(古い Git では `gc --auto`)。親から離れて動き続ける。
  - 保守処理は、1 つずつのファイルで置かれたオブジェクト(loose object)を、1 つのパック(まとめたファイル)へ詰め直す。
  - それまでは、保守処理が検証用コピーの `.git` へ書き、検証用コピーの削除が `Directory not empty` で落ちることがあった。`run-checks` は終了値 2 で止まり、受領書(結果の JSON)を書かず、検証用コピーが残った。Mac の Git 2.54 で確かめた。
  - Linux の Git 2.51・2.34 でも、レビューした中身の loose object が多いとき(`.git/objects/17` に 28 個以上。全体でおよそ 7,000 個以上)に同じことが起きうる。同じ設定で防ぐ。
  - 設定は引数で渡し、その 1 回の `git commit` にだけ効かせる。品質コマンドに渡す環境変数と、検証用コピーの `.git/config` は変えない。
  - 限界: 品質コマンドが自分で検証用コピーの中で `git commit` などを打つと、同じ保守処理が起きる。macOS など Linux 以外の POSIX では、群の外の子孫と同じく保証外。Linux の監督は、親が終わった子孫として止める見込み(未確認)。
  - 限界: Git 2.29 より前の Git は未確認(`gc.auto=0` も渡すが、動きは確かめていない)。
- Linux 上で他 POSIX の群方式を実行した回帰は通過した。これは macOS 実機の確認ではない。macOS 実機の回帰は、下の「macOS の実測と再確認手順」の回帰の手順で回す。Python の追加パッケージは不要。

## パス解決の代替と限界

- snapshot の `abs_maybe` は `realpath -m` → `readlink -f` → `cd -P` の順で試す。`cd -P` はディレクトリ専用で、通常ファイルや不存在の末尾には同じ結果を保証しない。ただし通常の出力親は `mkdir -p` の後に正規化するため、新規の親がこの差だけで失敗するとは限らない。
- guard の `real_path` は通常ファイルを扱うため `cd -P` を代替にしない。`realpath` と `readlink -f` の両方が使えず正規化できなければ `take` は rc 30 / `ERROR [taskmd]` で安全側に停止する。
- これらは既存の best-effort の代替。bash だけを導入しても BSD コマンド全体の互換性を保証しない。必要道具が不足する場合は実行を止め、選ばれた実体を確認する。

## macOS の実測と再確認手順

[Issue #226](https://github.com/Yuki-Maeda-valour/workflow/issues/226) で macOS 26.6.2 / arm64 の実機を確認した。
Python 3.14.4、Git 2.54.0 / Apple Git-157、Bash 5.3.20、GNU coreutils 9.12・findutils 4.11・grep 3.12 を使用した。
実行条件・結果・独立レビューは同 Issue に記録する。ほかの OS 版や BSD コマンド全般の互換性は保証しない。

- 通常の終了値 0 / 7 と出力の hash、残った元の群の停止、無関係な処理の生存を実測した。
- macOS では群の停止直後の不在確認が一時的に `EPERM` となり、直後に `ESRCH` へ変わる場合があった。旧実装はコピーを保持して停止した。初回の成功だけで安定動作と判断せず、修正後も下記の手順で再確認する。
- `setsid` で群外へ移った子は、元の群の停止とコピー削除の後も動いた。群外の子孫と同じ利用者権限での完全な隔離は保証しない。
- 入力・状態・一時領域には、親を含め symlink(別のパスを指すリンク)のない物理パスを選ぶ。macOS の `/var`・`/tmp` の別名は拒否される場合がある。 <!-- validate-allow: macOS の標準パスにあるリンクの注意を説明するため -->
  - 手で確かめるときは、今までどおり `TMPDIR` も物理パスの書き込み可能なディレクトリに選ぶ。
  - 下の「品質コマンドの監督と検証用コピーの回帰を Mac で回す手順」の試験は、一時領域の名前を自分で物理パスに解決して使う。`TMPDIR` の指定は要らない。製品のリンクの親の拒否は変えない。
- Apple Git の追加の system 設定: `implement-guard.sh take` は、`git version` が Apple Git の形を返し、`git --man-path` から導入先を求められる Git では、system 設定を外さずに通る。([Issue #241](https://github.com/Yuki-Maeda-valour/workflow/issues/241))
  - 仕組み: Apple Git は、system 設定が有効(`GIT_CONFIG_NOSYSTEM` が未設定か偽)のとき、`/etc/gitconfig` より先に `<Git の導入先>/share/git-core/gitconfig` を読む。`GIT_CONFIG_SYSTEM` の値(`/dev/null`・空・任意のファイル)には関係なく読む。
  - 候補にする条件: `git version` の出力が `git version X (Apple Git-N)` の形(`N` は `137.1` のように小数点を含んでもよい)。`take` は `git --man-path` の値から末尾の `/share/man` を外して導入先を求め、その下の `share/git-core/gitconfig` を保持候補(起動前に控えて、後で変化を照らす設定ファイルの一覧)に入れる。ファイルが無くても入れる(後から作られたら検出するため)。
  - 受け付ける `git --man-path` の値は、改行を含まず `/…/share/man` の形のものだけ。空・相対パス・末尾が `/share/man/`・導入先が空になる値・コマンドの失敗は、終了値 20 で止める。
  - `GIT_CONFIG_NOSYSTEM` が真(`1`・`true` など)なら入れない。`GIT_CONFIG_NOSYSTEM`・`GIT_CONFIG_SYSTEM` と利用者の Git 設定は書き換えない。system 設定を自動で外すことも、未知の読込先を後から許可することもしない。
  - 候補にした追加の設定は、ほかの候補と同じく、内容変更・新設・削除・リンクへの差し替えを、Git を呼ぶ前に検出する(`config-check` と `compare` は終了値 33)。
  - 追加の設定を正当に変えたときは、保護領域を `cleanup` で消してから `take` し直す。
  - 実機で確かめた構成(2026-10-30): macOS 26.6.2 / arm64、Git 2.54.0(Apple Git-157)。`xcode-select -p` は `/Library/Developer/CommandLineTools`。`/usr/bin/git` は実体を選んで起動する小さなプログラムで、実体は `/Library/Developer/CommandLineTools/usr/bin/git`。読込先は `/Library/Developer/CommandLineTools/usr/share/git-core/gitconfig`。本体を別の場所 `P/bin/git` へ複製すると `P/share/git-core/gitconfig` を読み、`git --man-path` も `P/share/man` を返した。
  - 未確認の構成: Xcode.app の Git(この Mac に Xcode.app は無い)、Apple Git-157 以外の版、Intel(x86_64)の Mac。Git 2.42 より前の Apple Git も未確認。
  - 保持候補に無い読込先を Git が読むとき、または Apple Git の導入先・system 設定の場所を求められないとき、`take` は終了値 20 で止める。診断の 1 行目は `ERROR [internal] 設定の出典を安全に収集・保存できない: <原因>`。原因は、読込先のパス・出典の種類・Apple Git の導入先を確定できない・system 設定の場所を問い合わせられない、のどれか。続く `NOTE:` の行に、選べる実行条件を出す。設定の値・本文と Git の stderr は出さない。
    - 条件 1: その読込先が system 設定として読まれるなら、system 設定を外す影響を確かめたうえで、`take` から `cleanup` までを同じ `GIT_CONFIG_NOSYSTEM=1` の環境で実行する。
    - 条件 2: 設定の読込先をすべて保持候補にできる Git を `PATH` で選ぶ。
    - 条件 3(system 設定の場所を問い合わせられないときだけ): `/etc/gitconfig` などの system 設定を読まなくてよいなら、`take` から `cleanup` までを同じ `GIT_CONFIG_SYSTEM=/dev/null` の環境で実行する。
  - `git var GIT_CONFIG_SYSTEM` が無い Git(2.42 より前と推測。未確認)の Apple Git: system 設定を有効のまま既定の環境で `take` を通すことはできず、終了値 20 で止まる。`GIT_CONFIG_SYSTEM=/dev/null` を指定すれば通る。その場合も追加の設定は候補に入る(古い Apple Git の実機では未確認)。
  - 限界: `take` のときに求めた場所だけを照らす。`take` の後に Git の実体を切り替える操作(`xcode-select`・`DEVELOPER_DIR`・`PATH` の変更)は検出しない。切り替えたときは `cleanup` してから `take` し直す。これは既存の `git var GIT_CONFIG_SYSTEM` と同じ限界。
  - 追加の属性ファイル(`share/git-core/gitattributes`)は設定の出典に影響しないため扱わない。
- `implement-guard-selftest.sh` を Mac で回す手順(2026-10-30 に確認):
  - 環境: GNU coreutils・findutils・grep を `PATH` の先頭に置く。`sed` は BSD 版(`/usr/bin/sed`)のまま使える。`TMPDIR` は symlink を含まない物理パスにする。`GIT_CONFIG_NOSYSTEM` は指定せず、system 設定は有効のまま回す。
  - 実機の分岐: ケース `M26` は、Apple Git の Mac では実機の分岐を動かし、Apple Git を模した分岐は `NOTE:` の行を出して省く(数えない)。Apple Git でない環境では逆になる。
  - 結果: 下の「Mac の結果」に書く。シェル回帰一式の正式な必要環境は Linux のままで、Mac の結果は Linux の代わりにしない。
- Mac の結果(Issue #241、2026-10-30、macOS 26.6.2 / arm64、Git 2.54.0〈Apple Git-157〉、Bash 5.3.20、GNU coreutils 9.12・findutils 4.11.0・grep 3.12、system 設定は有効のまま): FAIL 44 行(下の 2 群だけ)。PASS の件数と所要時間は Issue #241 の検証の記録に書く。
  - `M11`・`M25`・`M26`・`M27` と、変異 `x`・`y`・`z`・`w`(わざと壊した写しで試験が落ちるかを見る試験)の行に FAIL は無い。`M26` の実機の分岐は `NOTE:` ではなく PASS の行で動く。
  - 残る FAIL 44 行は、Apple Git の設定とは別の原因で、修正前から落ちていたもの。
    - `restore-taskmd`(`J1`・`J2`・`J3`・`J6`・`J8`・`B10`・`B12`・`G7`・`M6` の 40 行): 2026-11-01 に [Issue #285](https://github.com/Yuki-Maeda-valour/workflow/issues/285) で Linux 側を直した。macOS は `fd-copy-unavailable` で止まるので、この 40 行は残る(macOS の扱いは [#271](https://github.com/Yuki-Maeda-valour/workflow/issues/271) が確かめる)。原因は環境で違う。
      - Mac(GNU coreutils 9.12。直す前〈2026-10-30〉の記録): `/proc` が無く、`/dev/fd` の別名で `cp: skipping file '/dev/fd/3', as it was replaced while being copied` になる。書き込みの上限の有無には関係しない。
        - この文言は `cp` を直接起動したときのもの。補助は直す前も、`cp` を起動する前の別名の確認(device 番号の食い違い)で止まっていた(終了値 33・`REASON=runtime-unavailable`)。
      - Linux(Debian trixie・GNU coreutils 9.7。直す前〈2026-10-30〉の記録): 書き込みの上限を付けた `cp -aL` が `SIGXFSZ` で止まり、38 行が落ちる。直した後は、復元系のケース(`--only J1,…,J10,B10,B12,G7,M6`)の FAIL が 0 件になった(PASS 198 件)。
      - `J8` は、非 UTF-8 の名前を APFS が作れないことも重なり、Mac では 2 行多い。
    - `M17` の 4 行: 200 MiB の出力の試験が、外側の 90 秒で時間切れになっていた。
      - 2026-10-31 に [Issue #286](https://github.com/Yuki-Maeda-valour/workflow/issues/286) で直した。直した後の Mac の `M17` は 4 件 PASS で、3 回とも 3〜4 秒で終わった。直した後の Mac の FAIL は 40 行(`restore-taskmd` 系だけ)。
  - 修正前の Mac の FAIL 64 行(スイートを `GIT_CONFIG_NOSYSTEM=1` で回した基準。system 設定を有効のままの修正前の実行は、Issue #241 の 2026-10-26 のコメントで FAIL 812 件)のうち、`M11`・`M25` の 19 行と変異 `w` の 1 行(BSD 版の `sed` が原因)が直り、新しく落ちた行は無い。
- 品質コマンドの監督と検証用コピーの回帰を Mac で回す手順([Issue #239](https://github.com/Yuki-Maeda-valour/workflow/issues/239)):
  - 対象: `scripts/test_check_process.py` と `scripts/test_review_guard.py`。実プロセスと実 CLI(`start → take → seal → run-checks`)で、終了値 0 / 7 と出力の hash、親の終了・時間切れ・`TERM`・`INT`・`HUP`・群の外へ移る子、検証用コピーの削除と保持を確かめる。
  - コマンド: リポジトリの最上位で次を実行する。`<scratch>` は記録を置く作業用のフォルダ。`-v` の結果は標準エラーに出るので、標準エラーもログへ送る。

    ```bash
    python3 -m unittest -v scripts.test_check_process scripts.test_review_guard 2>&1 | tee <scratch>/run.log
    ```

  - 必要環境: Python 3・Git・`PATH` で選ばれる `bash` が 4.0 以上(実 CLI がまず `diff-snapshot.sh --precheck` を起動する。Mac 標準の `/bin/bash` 3.2 では止まる)。`command -v bash` と `bash --version` を記録する。
    - `TMPDIR` の指定は要らない。
    - GNU 系ツールは要らない。2026-10-31 の実行は、`PATH` で選ばれる `grep`・`sed`・`sort`・`find`・`realpath` が BSD 版のままで成功した。
  - 期待の件数(2 本の合計 94 件):
    - macOS: `Ran 94 tests`、最後の行が `OK (skipped=12)`。飛ばし 12 件はすべて `Linux 専用: `(`CheckProcessLinuxTest` 10 件・`CheckLifecycleTest` 2 件)。成功 82 件。
    - Linux: `Ran 94 tests`、最後の行が `OK (skipped=13)`。飛ばし 13 件はすべて `macOS 専用: `(`CheckProcessDarwinTest` 9 件・`CheckLifecycleTest` 3 件・`CliProcessLifecycleTest` 1 件)。成功 81 件。
  - 所要時間の目安: 4〜5 分(2026-10-31、macOS 26.6.2 / arm64 の 4 回の実行で 250〜276 秒)。
  - 数え方:
    - `grep -c "skipped 'Linux 専用: " <scratch>/run.log` と `grep -c "skipped 'macOS 専用: " <scratch>/run.log` で、OS ごとの飛ばしを数える。
    - 最後の行と `Ran N tests` の N を記録する。成功の数は、N から飛ばしを引いた数。
    - 1 件の試験の中で複数の通り方(`TERM`・`INT`・`HUP` など)を順に回すものは、1 件と数える。
    - 最後の行の `failures=` は、落ちた `subTest` を 1 回ずつ数える。落ちたときは、`failures=` の数と、落ちた試験の名前と `subTest` の値の両方を記録する。
    - `Linux 専用: ` の飛ばしを、Mac の成功の根拠にしない。
  - 試験の領域の名前(頭が `wf-proc-test-`)は、次のクラスの試験が起動するプロセス(実 CLI・anchor・品質コマンドとその子と孫・無関係なプロセス)のコマンド行に入る: `CliProcessLifecycleTest`・`CheckLifecycleTest`・`CheckProcessDarwinTest`・`CheckProcessCommonTest`。実行の後に `ps -axww -o pid=,pgid=,command= | grep -F wf-proc-test- | grep -v grep` を打ち、出力が無いことを確かめる。
    - 次のプロセスのコマンド行には入らないので、この確認では見つからない: `prove()` など、製品の置き場(`check-process.py` のパス)・品質コマンドの引数・`TMPDIR` のどれにも試験の領域を使わない `run-checks` が起動する anchor と品質コマンド、製品が中で短く起動する `grep`。
    - ただし、`ReviewGuardTest` の `test_cli_run_checks_keeps_exit_codes_and_output_hashes` と `test_run_checks_clean_copy_commit_starts_no_background_maintenance` の `run-checks` は、anchor(Linux では監督)と品質コマンドがこの確認で見つかる。
      - この 2 件は、品質コマンドの引数に試験の領域のパスを渡し、`TMPDIR` も試験の領域に置く。
      - anchor と Linux の監督のコマンド行は、品質コマンドの引数をそのまま含む。
    - ただし、`ReviewGuardTest` の `test_run_checks_inside_reviewed_code_does_not_create_bytecode` の `run-checks` は、anchor(Linux では監督)だけがこの確認で見つかる。品質コマンドは見つからない。
      - この試験は、製品を試験の領域へ写し、写した `review-guard.py` で `run-checks` を回す。
      - `review-guard.py` は、自分と同じフォルダの `check-process.py` を使う。
      - anchor と Linux の監督のコマンド行は、その `check-process.py` の絶対パスを含む。
  - anchor の消失を確かめる試験は、群に `TERM` を無視する子を置く。上の「品質コマンドの監督」の限界のとおり、anchor が失われた後に群の全員が `TERM` で終わると、診断の文が `Operation not permitted` に替わるため。
  - 時間切れ・停止の信号を確かめる試験の、`TERM` を無視する子は、`SIGKILL` で止まることを確かめるために置く。
  - #289 で直した不具合の回帰の試験(`CheckProcessDarwinTest` の `test_issue289_term_obeying_timeout_is_timed_out`・`test_issue289_term_obeying_parent_signal_is_interrupted`):
    - 群の全員が `TERM` で終わる品質コマンドで、時間切れは `timed_out` が真になることを確かめる。検証の親への `TERM`・`INT`・`HUP` は `Interrupted`(`終了通知 N を受けた`)になることを確かめる。
    - どちらも、anchor が `SIGKILL` まで残る(anchor の終了値が `-9`)ことを確かめる。
    - 読み口を先に閉じる形(直す前)へ戻ると、回収の失敗(`Operation not permitted`)で落ちる。
    - 直す前(v4.28.3)の再現の実測(2026-10-31、macOS 26.6.2 / arm64、`run_command(..., method='posix')`): `grace=.05` で、`time.sleep(3)` を期限 0.3 秒で 3 回中 3 回。`TERM`・`INT`・`HUP` で各 1 回中 1 回。時間切れと停止の信号で 25 回中 25 回(CPU に 16 本の負荷をかけても 25 回中 25 回)。`grace` が 0.2 秒で 8 回中 8 回。
  - Linux の後始末の順の試験(`CheckProcessLinuxTest` の `test_posix_term_obeying_command_keeps_anchor_until_kill`): Linux は終了済みだけの群への `SIGKILL` を拒まないので、anchor の終了値で順を確かめる。直す前は 97、直した後は `-9`。
  - #292 で直した不具合の回帰の試験(`ReviewGuardTest` の `test_run_checks_clean_copy_commit_starts_no_background_maintenance`):
    - 詰め直しが必ず起きる中身で `run-checks` を回し、検証用コピーの `.git/objects` が変わらないことを確かめる。
      - 中身は、blob(ファイルの中身のオブジェクト)の名前が `17` で始まるファイル 28 個。
      - 品質コマンドが 3 秒の間、検証用コピーのパックの有無と `.git/objects/17` の数を見る。
    - 受領書の終了値 0、`run-checks` の終了値 0、検証用コピーと出力の置き場が残らないことも確かめる。
    - Mac と Linux の両方で動く(飛ばしの印を付けない)。
    - 直す前(v4.28.4)は落ちる。落ちる理由は次のどれか。
      - 受領書の終了値 5: 品質コマンドの起動の時点で、保守処理がもう詰め直していた。
      - 受領書の終了値 4: 品質コマンドが見ている間に、詰め直しが始まった。
      - `run-checks` の終了値 2: 検証用コピーの削除が `Directory not empty` で落ちた。
    - 直す前の実測(2026-10-31): Mac(Git 2.54.0〈Apple Git-157〉)と Linux の容器(Ubuntu 22.04・Git 2.51.0)で各 3 回中 3 回、受領書の終了値 5 で落ちた。
    - 受領書の終了値 3 は前提の崩れ(`.git/objects/17` が 28 個より少ない)。黙って通らない。
  - 結果は Issue #239 に記録する。
- 無人ループとシェル回帰一式は引き続き Linux 専用。Linux 上の Darwin スタブの成功や Linux 専用試験の skip は、macOS の成功根拠にしない。

再確認は使い捨てリポジトリで次の順に行う。

1. 実機の OS 版、`command -v bash`・`bash --version`、GNU 道具の実行パスと版を記録し、PATH を選ぶ。
2. scratch リポジトリで do-task の事前検査 → snapshot(新規出力親 1 階層・複数階層・既存 symlink 拒否) → guard の `take` / `compare` / `cleanup` を確認する。create-task の保存先 resolver と本文 digest も scratch で確認する。system 設定は有効のまま(`GIT_CONFIG_NOSYSTEM` を指定しない)で確かめ、`take` が終了値 0 で通ること、`snapshot/config-origins.txt` に Apple Git の追加の設定の行があることも見る。
3. 標準 bash では do-task の bash 要件が明示されること、標準・導入 bash の両方で loop が Linux 専用エラー rc 20 となり、状態生成・git 操作・ホスト起動がないことを確認する。macOS 上で loop 本体や loop-selftest の成功は要求しない。
4. 品質検証(手順 4〜6)は、上の「品質コマンドの監督と検証用コピーの回帰を Mac で回す手順」のコマンドで確かめられる。Python の版も記録する。手で行う確認は、回帰で足りないとき(新しい OS 版の初回など)の補助として残す。手で行うときは、同じ scratch で [review-protocol.md](review-protocol.md#reviewcommit-照合) の `start` → `take` → `seal` → `run-checks` を行う。終了値 0 と 7 の計画で、終了値と出力の hash が保たれることを確認する。
5. 次に、3 秒で自己終了する子を作る品質コマンドと、外側の scratch に印を書く後続コマンドを計画へ入れる。`--timeout 1` で、元の群が消えてから戻ること、後続の印が無いこと、コピーが削除されることを確認する。通常終了した親が子を残す場合も、次のコマンドと重複しないことを確かめる。
6. 子の起動を確認してから、検証の親へ `TERM`・`INT`・`HUP` をそれぞれ送る。群の停止、コピー保持、後続未起動、終了値 2 を確かめる。試験で起動した処理は、失敗時も自分で終了・回収する。

実機がなければ「実施不能(実機なし)／未確認」と記録し、上記の手順を引き継ぐ。
