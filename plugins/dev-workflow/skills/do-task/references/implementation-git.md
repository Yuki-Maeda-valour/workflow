# 内蔵 implementer の読取り入口

内蔵 implementer(実装を受け持つ AI)が Git リポジトリの状態・差分・履歴を読むときは、`scripts/implementation-git.py` を使う。承認済みの filter を実行せず、作業を別の AI に任せること(委託)を行う側が確認した対象と無効化設定を別の実行環境へ渡す。

## 委託前の確認と引き渡し

必要環境の正本(ほかが合わせる元)は [runtime-requirements.md](runtime-requirements.md) とする。Git 対象では、その実行可能な確認を委託直前に通す。Python 3・標準ライブラリ・構文・入口・事前検査・同梱依存が利用不能なら、内蔵担当を起動・再依頼せず停止する。本文ダイジェスト(本文から計算した短い値。本文が変わると値も変わる)の Python 不在の例外は適用しない。素の Git・shell での代行・外部実装への切り替えで続行しない。非 Git は既存判定と運用を維持する。

公開する操作は次の2つだけである。`--help` は各操作の引数を表示する。

```text
python3 <入口> prepare --cwd <root> [--accept <最後に承認した値>]
python3 <入口> run --cwd <同じroot> [--accept <同じ承認値>] --expect-context-sha256 <控えた値> <操作> <専用引数>
```

| 引数 | 契約 |
|---|---|
| `--cwd` | 必須。存在するリポジトリのルート。物理パスに直し、Git の toplevel と一致を確認 |
| `--accept` | 任意。既存手順で利用者が最後に承認した一覧の値。未指定は `null` |
| `--expect-context-sha256` | `run` で必須、`prepare` では不可。小文字16進64桁 |

`prepare` は通常の読取りをせず、事前検査と実効値を確認する。成功時だけ stdout に JSON を1件出す。項目は `version=1`、物理 `cwd`、`accept`、`context_sha256` である。承認そのものは作らない。同じセッションで得た承認は再利用し、毎回利用者へ聞き直さない。

委託元は直接得た結果を会話に保持し、入口のパスと3つの値を委託文へ渡す。担当は `run` に渡して読み、承認値と期待値をリポジトリのファイルや過去ログから補わない。NUL 出力も再利用しない。期待値を失ったら委託元から再送する。保持が無ければ通常の承認手順へ戻す。

| 委託の場面 | 直前の条件 |
|---|---|
| 初回 | 必要環境確認 → `prepare` → 本文の照合(照らし合わせて確かめること)に使う値と共に渡す |
| 条件 B | 最後の承認値で確認 → `prepare`。外部担当は起動しない |
| 同じ担当への再依頼 | 新しく確認 → `prepare`。古い担当の環境を信用しない |
| 外部失敗後 | 既存の設定検査・比較・タスク保護と引き継ぎ許可が先。比較不能や判断待ちから進まない |
| M2 | 旧担当の停止と本文照合が先。その後に確認 → `prepare` |

5経路すべてで `prepare` 成功後だけ起動・再依頼する。`run` の rc 22 は委託元へ返し、新しい一覧を利用者が明示承認した場合だけ新しい `prepare` で再開する。担当が自分で承認値を拾って更新しない。外部実装からの復帰に固有の制約は維持する。

## 毎回の確認

1. 引数・符号化・対象ディレクトリを検査する。不正な入力は Git 起動前に拒否する。
2. 親環境のコピーから `GIT_CONFIG_COUNT`、`GIT_CONFIG_KEY_*`、`GIT_CONFIG_VALUE_*`、`GIT_CONFIG_PARAMETERS` と4種の pathspec 環境変数を除く。親環境は変えない。
3. `GIT_DIR`、`GIT_WORK_TREE`、`GIT_COMMON_DIR`、`GIT_INDEX_FILE`、`GIT_OBJECT_DIRECTORY`、`GIT_ALTERNATE_OBJECT_DIRECTORIES` が非空なら拒否する。
4. `GIT_NO_LAZY_FETCH=1`、`GIT_OPTIONAL_LOCKS=0`、`GIT_TERMINAL_PROMPT=0` を設定して既存の事前検査を起動する。rc 0 の stdout だけを解析する。
5. NUL 終端、厳密な UTF-8、変数名の許可集合と重複、COUNT と欠けのない添字、KEY/VALUE の対を確認する。空出力は設定なしとする。
6. `filter.<名>.clean/smudge/process/required` だけを受理する。名前ごとに4項目がそろい、値は空・空・空・`false` でなければならない。意味上のキーの重複も拒否する。
7. `run` は物理 cwd・accept・設定辞書の全体を期待値と照合する。不一致は実効値確認と読取りの前に止める。
8. 検証済みの原 KEY/VALUE を同じ子環境へ適用する。その環境をすべての内部 Git に明示し、4項目の実効値と rc 0 を確認する。root 確認、リビジョン解決、本体の読取りにも同じ環境を使う。

設定辞書は `filter.<名>.<項目>` から値への対応であり、添字とトークン順を含めない。照合値は `{"version":1,"cwd":物理パス,"accept":受領値またはnull,"filters":設定辞書}` を `json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))` で正規化し、改行・BOM 無しの UTF-8 へ符号化した SHA-256 とする。同じ集合の順序変更は同値であり、集合・承認値・物理 cwd の変更は不一致となる。代替文字への置換や孤立 surrogate は許さない。

filter 名を表示用 NOTE から組み立てず、`eval`、shell 展開、名前を埋める `-c` を使わない。コマンド経由の `GIT_CONFIG_*` は入力としてサポートしない。通常の設定ファイルは既存の事前検査の対象範囲に従う。

## 5つの読取り

| 操作 | 専用引数 | 既定 |
|---|---|---|
| `status` | `--format short\|porcelain`、`--untracked normal\|all\|no`、反復 `--path` | short・normal |
| `diff` | `--cached`、`--base <rev>`、`--target <rev>`、`--format patch\|stat\|name-only\|name-status`、`--context <0〜100>`、反復 `--path` | 未stage patch・前後3行 |
| `log` | `--from <rev>`、`--to <rev>`、`--limit <1〜1000>`、`--format oneline\|fuller`、反復 `--path` | HEAD の先頭20件・oneline |
| `show` | `--rev <rev>`、`--format patch\|stat\|name-only`、単一 `--file <path>` | HEAD の patch |
| `ls-files` | `--mode tracked\|untracked`、反復 `--path` | tracked |

- `diff --cached` と `--target` は併用不可。target は base 必須。base 単独は commit と作業ツリー、cached と base は commit と index の比較。
- log の from は除外側、to は到達側。from 単独は to=HEAD。表示専用で `--no-patch` を強制する。
- show の file と format は併用不可。解決済み commit と相対 path から object 指定を作り、種類検査が rc 0 かつ blob の場合だけ内容を出す。tree・commit・解決不能は拒否する。symlink は blob の内容を表示し、リンク先を辿らない。
- rev は1〜1024文字、先頭 `-`・NUL・改行を拒否。`rev-parse --verify --end-of-options <rev>^{commit}` で commit ID へ解決する。
- path はルート相対リテラル。空・絶対パス・`..` 要素を拒否し、空白・日本語は許可する。4操作の path は反復でき、省略時は全体。`--` と `:(literal)` を強制し、大文字小文字もそのまま照合する。show の file は object 内のパスなので pathspec と区別する。
- path 以外の全オプションは1回限り。同じ値、`--name=value` と分離形の混在、cached フラグの重複も拒否する。
- 未知の操作、書込み、`-c`、`--config-env`、`--output`、`--ext-diff`、`--textconv`、`--exec`、shell 文字列を受け付けない。汎用引数や command は無い。

安全用の前置きは [base-commit.md](base-commit.md) の共通固定値と一致させる。status と diff は `--ignore-submodules=dirty`、diff と show は `--no-ext-diff --no-textconv --submodule=short --no-color`、log と show は `--no-show-signature --no-decorate --no-color` を強制する。利用者の引数から取り消せない。

## 出力・停止・限界

- run の stdout は通常の Git 出力だけ。注意と失敗は stderr。prepare の失敗時は JSON を出さない。
- rc は成功0、入力不正2、起動・解析・Git 失敗20、承認情報不一致または事前検査の拒否22。事前検査の2/20/22は維持し、未知の rc は20にして元の値を stderr に残す。
- 待ち時間は事前検査120秒、内部確認と本体 Git は各30秒。時間切れを空出力の成功にしない。Git の失敗時は部分的な stdout があり得る。非0の結果を成功した観察として使わない。
- 書込み・設定変更・ネットワーク取得、品質コマンド内の任意の Git は対象外。既存の snapshot・保護・完了条件を置き換えない。
- HOME・PATH・利用者環境全体の固定や隔離、未停止プロセスの終了、比較不能からの復帰、別セッション再開は保証しない。検査と読取りの間の設定変更の競合も排除しない。
- 正式入口の利用は手順の義務であり、任意の直接 Git を OS の権限で封じない。正規 filter の無効化の試験を、LFS の追加調査や攻撃再現と扱わない。
- 出力の機密除外は行わない。既存の機密ファイル除外と報告への値の混入禁止を維持する。無効化後は生の作業ツリーが見え、通常の filter 使用時と差分が変わり得る。
- object が無ければ失敗し、ネットワーク取得で復旧しない。5操作以外の読取りが必要なら契約を別途設計する。
