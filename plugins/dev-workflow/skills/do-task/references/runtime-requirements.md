# 必要環境と確認手順

Phase 0 の事前検査より前に、使用するシェルと道具を確認する。profile の有無によらず適用する。

## 実行環境

- `diff-snapshot.sh` と `implement-guard.sh` は連想配列を使うため **bash 4.0 以上**が必要。`--help` と `--precheck` にも同じ要件がかかる。`command -v bash` と `bash --version` で、PATH から選ぶ実体と版を確認する。ログインシェルの版だけでは判断しない。
- 本体の必要道具は GNU coreutils・GNU findutils・GNU grep。実行名 `realpath`・`readlink`・`sha256sum`・`sort`・`grep`・`find` などで PATH から選ぶ。特に `grep -z`・`sort -z` と、gitlink 検査の `find -mindepth/-maxdepth/-quit` が必要。導入しても別名や PATH の後ろにあれば選ばれないため、`command -v <実行名>` と `<実行名> --version` を記録する。
- `diff-snapshot-selftest.sh` は bash 4.0 以上と GNU coreutils・GNU findutils・GNU grep が必要。冒頭で `sha256sum`・`touch -d`・`find -maxdepth/-quit`・`head -c`・`grep -z`・`sort -z` を検査し、不足は rc 2 / `ERROR [requirements]` で止まる。後で時刻固定が失敗しても rc 2 で停止する。必要道具の不足と、時刻を固定できても stat キャッシュの隠蔽を再現できない正当な fixture 不成立を区別する。
- 回帰一式の必要環境は **Linux と GNU 系ツール**。無人ループ `loop.sh` は **Linux・bash 4.4 以上**が必要で、`setsid`・`flock`・`/proc` を使う。詳しくは [../../ship-task/references/loop.md](../../ship-task/references/loop.md) を読む。

## パス解決の代替と限界

- snapshot の `abs_maybe` は `realpath -m` → `readlink -f` → `cd -P` の順で試す。`cd -P` はディレクトリ専用で、通常ファイルや不存在の末尾には同じ結果を保証しない。ただし通常の出力親は `mkdir -p` の後に正規化するため、新規の親がこの差だけで失敗するとは限らない。
- guard の `real_path` は通常ファイルを扱うため `cd -P` を代替にしない。`realpath` と `readlink -f` の両方が使えず正規化できなければ `take` は rc 30 / `ERROR [taskmd]` で安全側に停止する。
- これらは既存の best-effort の代替。bash だけを導入しても BSD コマンド全体の互換性を保証しない。必要道具が不足する場合は実行を止め、選ばれた実体を確認する。

## macOS で残す確認

macOS 実機は未確認。Linux 上の Darwin スタブと bash 3.2 / 4.3 / 4.4 実体による測定は、macOS 実機の確認ではない。

1. 実機の OS 版、`command -v bash`・`bash --version`、GNU 道具の実行パスと版を記録し、PATH を選ぶ。
2. scratch リポジトリで do-task の事前検査 → snapshot(新規出力親 1 階層・複数階層・既存 symlink 拒否) → guard の `take` / `compare` / `cleanup` を確認する。create-task の保存先 resolver と本文 digest も scratch で確認する。
3. 標準 bash では do-task の bash 要件が明示されること、標準・導入 bash の両方で loop が Linux 専用エラー rc 20 となり、状態生成・git 操作・ホスト起動がないことを確認する。macOS 上で loop 本体や loop-selftest の成功は要求しない。

実機がなければ「実施不能(実機なし)／未確認」と記録し、上記の手順を引き継ぐ。
