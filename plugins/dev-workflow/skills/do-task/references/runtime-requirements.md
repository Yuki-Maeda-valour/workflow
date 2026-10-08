# 必要環境と確認手順

Phase 0 の事前検査より前に、使用するシェルと道具を確認する。profile の有無によらず適用する。

## 実行環境

- Git 対象の内蔵 implementer へ委託する前は、次の確認を独立した bash で実行する。`$1` は解決した do-task の `scripts` ディレクトリとする。PATH で選んだ同じ Python 実体を使う。エントリポイントと通常の Git は起動しない。

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
  本文の照合にある「Python が無ければ報告して続行」の例外は適用しない。素の Git、shell での代行、外部実装への切り替えでは続行しない。環境の修復後に確認からやり直す。
  `prepare` または `run` で後から必要環境の喪失が判明しても、その読取りは停止して委託元へ報告する。既に動く担当や子孫の自動停止を保証しない。
  profile が無い場合・一部 skill だけの導入でも同じ条件とする。非 Git は既存の判定に従い、この要件を適用しない。引数と停止後の扱いは [implementation-git.md](implementation-git.md) を読む。

- `diff-snapshot.sh` と `implement-guard.sh` は連想配列を使うため **bash 4.0 以上**が必要。`--help` と `--precheck` にも同じ要件がかかる。`command -v bash` と `bash --version` で、PATH から選ぶ実体と版を確認する。ログインシェルの版だけでは判断しない。
- 本体の必要道具は GNU coreutils・GNU findutils・GNU grep。実行名 `realpath`・`readlink`・`sha256sum`・`sort`・`grep`・`find` などで PATH から選ぶ。特に `grep -z`・`sort -z` と、gitlink 検査の `find -mindepth/-maxdepth/-quit` が必要。導入しても別名や PATH の後ろにあれば選ばれないため、`command -v <実行名>` と `<実行名> --version` を記録する。
- `diff-snapshot-selftest.sh` は bash 4.0 以上と GNU coreutils・GNU findutils・GNU grep が必要。冒頭で `sha256sum`・`touch -d`・`find -maxdepth/-quit`・`head -c`・`grep -z`・`sort -z` を検査し、不足は rc 2 / `ERROR [requirements]` で止まる。後で時刻固定が失敗しても rc 2 で停止する。必要道具の不足と、時刻を固定できても stat キャッシュの隠蔽を再現できない正当な fixture 不成立を区別する。
- 回帰一式の必要環境は **Linux と GNU 系ツール**。無人ループ `loop.sh` は **Linux・bash 4.4 以上**が必要で、`setsid`・`flock`・`/proc` を使う。詳しくは [../../ship-task/references/loop.md](../../ship-task/references/loop.md) を読む。

## 品質コマンドの監督

`review-guard.py run-checks` は、[check-process.py](../../ship-task/scripts/check-process.py) から品質コマンドを起動する。
停止とコピー保持の手順は [review-protocol.md](review-protocol.md#品質コマンドの停止とコピーの保持)が正本。

- **Linux**: `subreaper`(親が終了した子孫を引き取る仕組み)、`/proc`、`pidfd`(特定のプロセスを指す参照)が必要。Python の `os.pidfd_open`・`signal.pidfd_send_signal` を使う。専用プロセスで既存の [loop-supervisor.py](../../ship-task/scripts/loop-supervisor.py) を使い、子孫の終了と回収を確認する。必要な機構を使えなければ停止し、自動で他 POSIX の方式へ切り替えない。
- **他の POSIX 環境**: 専用のプロセス群を停止し、その群の不在を確認する。群の番号を保持する親は、最後の停止信号まで回収しない。番号の再利用で別の処理を止めることを避ける。
  - 親の回収後は signal 0 による確認だけを行う。`EPERM` は不在とせず、既存の 5 秒の期限内で再確認する。`ESRCH` だけを不在とし、期限内に確認できなければコピーを保持して止まる。
- 他 POSIX で `setsid` などにより群の外へ移った子孫は保証外。切離しを行う検証には、Linux の監督か OS の隔離を使う。Linux でも、同じ利用者権限による監督の改変などを完全に隔離するものではない。
- Linux 上で他 POSIX の群方式を実行した回帰は通過した。これは macOS 実機の確認ではない。Python の追加パッケージは不要。

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
- 入力・状態・一時領域には、親を含め symlink(別のパスを指すリンク)のない物理パスを選ぶ。macOS の `/var`・`/tmp` の別名は拒否される場合がある。`TMPDIR` も物理パスの書き込み可能なディレクトリに選ぶ。 <!-- validate-allow: macOS の標準パスにあるリンクの注意を説明するため -->
- Apple Git の追加の system 設定が保持候補に入らない場合は、安全側に停止する。実機試験では子の環境だけに `GIT_CONFIG_NOSYSTEM=1` を指定すると通常操作が成功した。利用者の設定を変更せず、system 設定を外す影響を確認して実行環境を選ぶ。
- 無人ループとシェル回帰一式は引き続き Linux 専用。Linux 上の Darwin スタブの成功や Linux 専用試験の skip は、macOS の成功根拠にしない。

再確認は使い捨てリポジトリで次の順に行う。

1. 実機の OS 版、`command -v bash`・`bash --version`、GNU 道具の実行パスと版を記録し、PATH を選ぶ。
2. scratch リポジトリで do-task の事前検査 → snapshot(新規出力親 1 階層・複数階層・既存 symlink 拒否) → guard の `take` / `compare` / `cleanup` を確認する。create-task の保存先 resolver と本文 digest も scratch で確認する。
3. 標準 bash では do-task の bash 要件が明示されること、標準・導入 bash の両方で loop が Linux 専用エラー rc 20 となり、状態生成・git 操作・ホスト起動がないことを確認する。macOS 上で loop 本体や loop-selftest の成功は要求しない。
4. 品質検証は Python の版も記録する。同じ scratch で [review-protocol.md](review-protocol.md#reviewcommit-照合) の `start` → `take` → `seal` → `run-checks` を行う。終了値 0 と 7 の計画で、終了値と出力の hash が保たれることを確認する。
5. 次に、3 秒で自己終了する子を作る品質コマンドと、外側の scratch に印を書く後続コマンドを計画へ入れる。`--timeout 1` で、元の群が消えてから戻ること、後続の印が無いこと、コピーが削除されることを確認する。通常終了した親が子を残す場合も、次のコマンドと重複しないことを確かめる。
6. 子の起動を確認してから、検証の親へ `TERM`・`INT`・`HUP` をそれぞれ送る。群の停止、コピー保持、後続未起動、終了値 2 を確かめる。試験で起動した処理は、失敗時も自分で終了・回収する。

実機がなければ「実施不能(実機なし)／未確認」と記録し、上記の手順を引き継ぐ。
