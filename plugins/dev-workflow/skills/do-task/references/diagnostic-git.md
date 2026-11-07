# 設定を戻さずに使う読取り専用の診断

比較不能で止まった後、保持した保護記録と除外計画から現在の差分・履歴・参照を調べる。
診断が成功しても復元・実装再開・基準の取り直しを承認しない。結果を示し、人の判断待ちへ戻る。
元の設定・属性・改行変換・ignoreを適用しない。選んだファイルの生のバイト列・種類・権限を比較する。

## 1. 必要環境と入力の保持

- Python 3.10以上・POSIXと、[runtime-requirements.md](runtime-requirements.md) の診断に必要な機能を使う。
- `python3 -I` で信頼する配布コピーの `diagnostic-git.py` を絶対パスから起動する。作業場所にある同じ名前のモジュールを読み込まない。
- 外部実装と子孫が現在の作業へ書き続けないことは、人が既存の手順で確認する。診断は停止の証明を行わない。
- `take` が直接返した `STATE_DIR`・`MANIFEST_SHA256`・`SNAPSHOT_SHA256` を会話と起動前報告に保持する。
- 除外計画のパスと実バイト列のSHA256も保持する。後から記録を再計算して信用値へ置き換えない。
- 候補を探す `pending-implementation.py` の一覧値は、保護記録や計画の真正性を示す値ではない。

## 2. 開始時の値と起動前の計画

Bは確定済みのタスク基準。Sは今回の安全な開始時のHEAD(いま取り出しているコミット)であり、過去のブランチ作成時点ではない。
Bの決め方は [base-commit.md](base-commit.md) のままとする。基準不明を診断準備で迂回しない。

| 開始経路 | 保持する値 |
|---|---|
| 直接do-task | 既存事前検査後、ブランチ処理・実装前にSを一度保持する。Bは既存規則で確定する |
| ship-taskで既存ブランチを使う | ブランチ判定・設計・実装前にSを保持し、do-taskへ渡す。do-taskで取り直さない |
| 今回ブランチを作る | Sとは別に、実際に作成時報告へ残したOID(Git のオブジェクトを指すハッシュ値)だけを、任意のブランチの出典(役割 `branch`)へ渡す |
| 人が承認した新たな開始 | Bは既存行・代替基準規則を使う。今回のSを保持し `resume-invocation` とする |

通常のGitによるSの取得は、安全な開始時だけに行う。
比較不能から `prepare`・`precheck`・`take` を呼び直して診断の基準にしない。
過去のブランチ作成時のOIDを、HEAD・reflog(参照の移動の記録)・merge-base(2 つのコミットの共通の祖先)から推定しない。
作成記録が無い場合は `not-recorded` で正常に進める。
以前のSや追加指定を保持していれば、その出典と座標を引き継いで減らさない。
同じ保護記録の計画は `--carry-plan` で引き継げる。異なる保護記録の計画を流用しない。

外部の起動前に、次の順で実行する。

1. `take` が成功し、直接返った保護記録のパスと2つのハッシュ値を保持する。
2. 保持された有効なGit対象を親のリポジトリから下のリポジトリの順に `plan` へ渡す。
3. 最初以外は、それまでの最新の計画パスとハッシュ値を `--carry-plan` に渡す。
4. 各成功応答で、終了コードを0と、応答を§7の形の厳密なJSONと、応答の `plan_sha256` を生成ファイルのハッシュ値と照合(照らし合わせて確かめること)する。
5. 最後の計画が、全有効対象の除外の指定を含むことを照合する。
6. 最新のパスとハッシュ値、B・S・任意のブランチの出典、追加指定の座標を起動前報告へ残してから外部実装を起動する。

**診断計画の作成失敗**は、`plan` の失敗・途絶・不正応答を指す。PyYAML不足・不正なprofile・出力先の競合も含む。
外部実装を起動せず、保護記録と残存計画を保持し、人の判断を待つ。外部が未起動でも通常完了後の削除条件に含めない。
残存ファイルからハッシュ値を取り直して採用しない。

## 3. 公開する3つの操作

各値は独立した引数として渡す。コマンド文字列を組み立てて実行しない。
`ROOT` は保持した管理ルート、`STATE` は保護記録、`N` は保持された有効なGit対象の番号。
OIDは、前工程で確認して保持した完全な小文字の値を使う。

```text
python3 -I diagnostic-git.py plan --cwd ROOT --state STATE --manifest-sha256 HEX --snapshot-sha256 HEX --context-id N --base-ref B --start-ref S --start-kind new-invocation --out NEWFILE
python3 -I diagnostic-git.py extend-plan --cwd ROOT --state STATE --manifest-sha256 HEX --snapshot-sha256 HEX --context-id N --plan FILE --plan-sha256 HEX --out NEWFILE OPERATION [専用引数]
python3 -I diagnostic-git.py run --cwd ROOT --state STATE --manifest-sha256 HEX --snapshot-sha256 HEX --context-id N --plan FILE --plan-sha256 HEX
```

`plan` の追加引数:

- `--branch-ref OID`: 実際に保持したブランチ作成時OIDだけ。
- `--profile-ref OID`: 保持した追加出典。複数指定できる。
- `--carry-plan FILE HEX`: 同じ保護記録の最新計画。1回だけ指定できる。
- `--exclude-root management/context`: 追加の `--exclude-glob GLOB` / `--exclude ERE` を渡すときは必須。
- `--start-kind new-invocation/resume-invocation`: 今回の開始の種類。
- `--context-start`: 独立タスクではない内包された下のリポジトリに限り、B/S/start-kind/branch-refの代わりに使う。
  - 親の必要な除外の指定は、すべて `--carry-plan` で受け取る。下のリポジトリの出典の役割 `current` と `held-head` を保持する。親のOIDを下のリポジトリへ使い回さない。

出力先は、保護記録とリポジトリの外にある、権限0700の専用の親ディレクトリの下の新規ファイル。
通常ファイルを権限0600で排他的に作る。書き出し(flush・fsync)の後、同じfd(開いたファイルの番号)でハッシュ値を照合してから成功応答を返す。
既存ファイル・symlink・特殊ファイルへ上書きしない。

## 4. 除外する名前と座標

既定の `.env`・`.env.*`・`.dev.vars` と、出典の役割 `current`・`base`・`start`・`held-head`・任意の `branch`・追加出典の指定を和集合にする。
既定の除外(`.claude/` の `reviews/`・`grasp.md`・`settings.local.json`・`.understand-project-done`)と `.git` 境界も維持する。
profile本文や他の設定値は計画と診断へ保存しない。profileが全て無い場合はPyYAMLを要求しない。

- worktree 側の除外の指定: 対象リポジトリの `.claude/project-profile.yml`。リポジトリからの相対名へ式を適用する。
- 管理側の除外の指定: 管理ルートがリポジトリ内のサブフォルダなら、そのprofileも別に使う。
  - 管理ルート配下だけで、そのルート相対名へ式を適用する。
  - 参照するcommitでも、同じ相対パスにあるprofileを取得する。式の文字列を置換して座標を変えない。
- 管理ルートとworktreeが同じなら、除外の指定は1つ。管理側の追加引数は対象番号0だけで受け付ける。
- 下のリポジトリの診断にも、選択対象と全祖先の除外の指定をすべて適用する。別対象で観測した追加指定も、元の除外の指定へ蓄積する。
- glob(`*` などで名前を選ぶ書き方)の変換とGNU `grep -E -z -f` の意味は、既存の `diff-snapshot.sh` の扱いに合わせる。非UTF-8の実名はバイト列で照合する。
- 機密判定を本文読取り・境界名の公開より先に行う。除外名は件数へ置換し、削除差分に変換しない。

## 5. 診断の準備と結果の採用

1. 最新計画を `extend-plan` に渡し、操作と専用引数を指定する。
2. 現在profile・現在HEAD・操作が参照するcommitのprofileを追加する。既存指定は削らない。
3. 終了コード0と完全な `plan-extended` 応答を照合する。入力した計画と新しく生成した計画のハッシュ値・保持値と、古い除外の指定がすべて残っていることも照合する。
4. 新しい計画のパスとハッシュ値を先に保持して報告する。
5. その計画を `run` に渡す。`run` に操作や引数を追加しない。
6. 結果を示し、人の判断待ちへ戻る。成功を再開の許可にしない。

`run` は使用したprofileと参照の指紋を、本文診断より前に照合する。
変化があれば22/`preparation-stale`。最新計画を入力に準備をやり直す。
`run` の失敗では受領済みの最新派生計画を保持する。古い計画へ戻らない。

`extend-plan` の失敗・途絶・不正応答では追加指定を保持できた保証がない。
保護記録と残存計画を保持し、人の判断を待つ。診断計画の作成失敗と同じく、自動削除しない。
旧計画で `run` や `extend-plan` を反復せず、人の証拠照合へ戻る。
残存計画からハッシュ値を取り直す、自動で起動前計画を作り直す、を行わない。

## 6. 診断できる操作

| 操作 | 引数と既定値 | 結果 |
|---|---|---|
| `status` | 範囲が必須。`--untracked no/names`、既定は `names` | HEAD・インデックス(ステージした内容を置く所)・作業ツリーの生データの比較 |
| `files` | 範囲が必須。`--kind tracked/untracked/all`、既定は `tracked` | 本文を読まない名前・種類・ファイルモード(種類と権限の値)・サイズ |
| `diff` | 範囲が必須。`--left HEAD/index/REV`、既定は `index`。`--right index/worktree/REV`、既定は `worktree`。`--format records/patch`、既定は `records`。`--context 0..100`、既定は3 | 2つの対象を比較。同一対象は拒否 |
| `show` | 範囲が必須。`--rev REV`、既定は `HEAD`。`--format records/patch/blob`、既定は `patch` | commitと第1親の差分。`blob` は単一のパスだけ |
| `log` | 範囲は任意。`--to REV`、既定は `HEAD`。`--limit 1..1000`、既定は20。`--format oneline/fuller`、既定は `oneline` | パッチのない固定形式の履歴 |
| `refs` | 追加引数なし | 共有の参照と、選んだworktree固有の参照の一覧 |
| `stashes` | `--limit 1..1000`、既定は20 | 現在取得できるstash(一時退避した変更)の履歴 |

範囲(応答の `scope`)は、`--path P` の完全一致か `--under P` の配下。複数指定できる。
パスは相対の実名で、Git の特殊な書き方(magic)やglobとして展開しない。先頭 `-` や `:(...)` も実名として扱う。
絶対名・空要素・`.` / `..` 要素・NUL(値が0のバイト)は拒否する。`--under .` だけはルート全体を選べる。
普通のディレクトリをパスで直接選んだときは、`files` だけがメタデータを1件返す。比較は23/`directory-selection` で止める。
REVは `HEAD`、完全な小文字のcommit OID、又は `refs/` から始まる完全な参照名。演算子・リビジョンの範囲(`A..B` など)・先頭 `-` は拒否する。

- 次の形式を、保持した形式のまま扱う: SHA-1・SHA-256、参照の保存形式(files・reftable)、追加の worktree(linked worktree)、分割(split)・部分(sparse)のインデックス、浅い履歴(shallow)、通常のファイルの alternates(別の場所のオブジェクトを読む設定)。
- まだコミットの無いブランチ(unborn)では、`status` の空のツリー、`log` の空の履歴、正当な空の参照・stashを許す。`show` の対象なしは23。
- 衝突が未解決のインデックスは、`status`・`files` でステージ番号を示す。インデックスを含む `diff` は23/`unmerged-index`。
- skip-worktree(作業ツリーへ取り出さない印)が付き、実体が無いものは `not-materialized`。削除のパッチにせず、`records` を残す。
  - 実体があれば通常比較する。オブジェクトどうしの `show`・`diff` へ、未展開の扱いを持ち込まない。
- symlinkはリンク先の文字列だけを読む。親のsymlinkを辿る選択は止める。
- 入れ子のリポジトリとsubmodule(別のリポジトリを組み込んだ所)の内部へ再帰しない。保持された下のリポジトリの対象を別に指定して診断する。
- FIFO(名前付きパイプ)等は、`files` でメタデータだけを返す。内容比較は止める。巨大な未追跡本文も名前一覧では読まない。
- ignore・属性・フィルター(Git がファイルを読み書きするときに内容を変える仕組み)を適用しない。改名の検出も行わない。パッチは専用の `a` / `b` だけから作る。
- 有効な未解決のシンボリック参照(symref。別の参照を指す参照)を `unresolved` として残す。stashの参照があるのに履歴が欠ければ23/`incomplete-metadata`。

## 7. 応答と停止

stdout(標準出力)は完了時のJSON1件とLF(改行)。途中のGit出力を流さない。未知・重複のキーや不正な型は成功として受け取らない。

- plan: `version:1,status:plan-created,plan_path_b64,plan_sha256,context_id,state_binding`。
- extend-plan: 上記の `status` を `plan-extended` にし、`input_plan_sha256,effective_exclusions_sha256` を追加。
- run: `version:1,status:observed,operation,context_id,view_policy:raw-selected-v1,scope,data,warnings,effective_exclusions_sha256,plan_sha256,baseline_record_updated:false,resume_authorized:false`。

パスと生の出力はbase64で元のバイト列を保つ。機密の範囲は `path_b64:null,redacted:true`。
`effective_exclusions_sha256` は、除外の指定の集合を、キーの順に並べた(key sort)UTF-8の詰めたJSON(compact JSON)で符号化したSHA256。
非ASCII文字をASCIIエスケープに置き換えた値のハッシュ値ではない。

| 終了コード | 意味 |
|---|---|
| 0 | 後検査・後始末・全応答送出を完了した |
| 2 | 引数又はスキーマ(決まった形)が不正 |
| 20 | 必要機能・I/O・時間・量・子プロセスの回収・後始末の失敗 |
| 22 | 保持値・入口・計画の不一致 |
| 23 | 必要な管理データ・形式・操作対象を扱えない |

送出前の失敗ではstdoutは空。送出途中の失敗・期限・シグナルでは、受領済みの断片が残り得る。
完全なJSONが届いても終了コードが非0なら成功ではない。
診断のstderr(標準エラー出力)は、固定の理由(reason)と、値を含まない説明だけ。回収失敗時は `TEMP_PATH_B64` 又は `TEMP_PATH_UNKNOWN=1` で残存を示す。
出力が詰まった場合はstderr自身も欠け得る。無応答・不正応答・非0は結果不明として止める。

## 8. 有限の処理と限界

| 対象 | 上限 |
|---|---|
| 管理用のコピー / 管理出力 / 各操作のGit出力 | 各200MiB |
| 管理用の項目 / 深さ | 2000 / 40 |
| 保護記録・計画 / profile | 16MiB / 1MiB |
| 範囲 / パス / 参照 | 4096件 / 4096バイト / 1024バイト |
| 作業ツリーの項目 / 深さ | 200000 / 128 |
| 本文の読み出し / パッチの比較用のペアのコピー | 1GiB / 200MiB |
| 最終JSON / 全子プロセスのstderr | 280MiB / 1MiB |
| 全操作 / Git・grepの子プロセス1回 | 300秒 / 30秒 |
| 直接の子プロセスの停止・回収 / 後始末 | TERM5秒＋KILL5秒 / 5秒 |

期限と量を各段階で共有し、出力や反復で再開始しない。管理2000件を作業ツリーの件数へ転用しない。
完成JSONと後検査・後始末の後、直接の子プロセス1個だけが最終stdout又はstderrを書く。
親は専用のpipe(プロセスの間で出力を渡す管)と残り時間を監督する。受け継いだ stdout/stderr のフラグ(開き方の設定)は変更しない。
送出する子プロセスの終了コード0と全バイトの送信の後、短い間だけシグナルを止め(mask)、停止要求と期限を再確認して終了コードを確定する。
確定前の中断は20。確定後の中断や、止めたシグナルを戻す処理の失敗でも、確定値を変えず、二度目の応答を出さない。
送出の失敗の後に、別の書き手や同期の出力で再報告しない。正当な大きい出力も残り予算内なら許す。
診断にGNU `timeout` を追加要求しない。既存の復元監督関数は変更しない。

元の設定ファイル(config)をGitの起動設定として読まず、最初のGitから専用の管理領域だけを使う。
元のリポジトリ・保護記録・入力の計画へ書かず、atime(最後に読んだ時刻)の変化は内容の変更と区別する。
部分インデックス(sparse index)のリポジトリでは、Git が専用の領域でインデックスを読むときに展開し、元の `objects` と、その alternates の連なりの置き場(別のリポジトリのこともある)にある、既存のオブジェクトを持つファイル(loose か pack)の更新時刻(mtime・ctime)を新しくすることがある。
中身・ファイルモード・オブジェクトの数は変えない。展開で Git が新しく書くオブジェクトは、専用の `objects` へ置く(元の `objects` は alternates で読むだけ。§12)。
時刻の更新まで止めるには、部分インデックスかどうかの判定・インデックスの読み方の変更・Git の版の分けが要るので、例外として扱う。
各ファイルと列挙集合の前後を検査するが、複数のファイルを一度にまとめて取ったスナップショットではない。
同じUID(利用者の番号)による改変、検査間に戻される変更、元のオブジェクトの置き場(ODB)の同時更新、任意子孫の完全停止は保証しない。
SIGKILL(強制終了のシグナル)・停電・カーネルI/O不帰還は期限内の自力回復を保証しない。
通常pipeの読み手停止はこの限界に含めず、直接の子プロセスの監督で打ち切る。

## 9. 別セッションと旧記録

[external-runners.md](external-runners.md) §12-9の人による停止確認を先に行う。
人の起動前報告から、保護記録・2つのハッシュ値・最新計画のパスとハッシュ値を受け取ってから、準備と診断へ進む。
候補の発見を認証や再開承認にしない。診断後も既存の復元・再開の判断は別に必要。
古い保護記録の対象の情報(context)の欠落・起動前計画なし・保持値喪失は22で止める。
新しい `take` や現在のprofileから補完しない。古い保護記録の既存の復元手順と、通常内蔵5読取りは維持する。
診断成功だけで保護記録・入力の計画を削除しない。比較不能・タスク MD の判断待ち・診断計画の作成失敗は、人の判断まで保持する。
通常完了時の既存の `cleanup` の条件は [external-runners.md](external-runners.md) §12-2 に従う。

## 10. 保存計画の厳密な形式

未知のキー・重複したキー・未知の版(`version`)・不正な型は拒否する。整数の欄へ真偽値(bool)を代用しない。
文字列はUTF-8。OS上の名前だけは元のバイト列をbase64へ変換する。
SHA256は64桁、OIDは保持形式の40桁又は64桁の小文字の16進数とする。
空のリポジトリでは、その形式の既知の空ツリーOIDだけを基準の特例として認める。任意のツリーをcommitとして渡せない。

計画のトップレベルは次のキーだけを持つ。

| キー | 型・値 |
|---|---|
| `version` | 整数 `1` |
| `state_binding` | `{manifest_sha256,snapshot_sha256}`。起動前に親が保持した2値 |
| `context_id` | 保持された有効対象の整数。`run` の選択対象と一致する |
| `management_binding` | `{context_id:0,relative_prefix_b64,identity_chain}` |
| `parent_plan_sha256` | 初回はnull。引き継いだ(`--carry-plan`)計画か、派生元の計画のハッシュ値 |
| `prepared_request` | `plan` ではnull。`extend-plan` は下記の準備済み要求 |
| `source_checks` | 初回は空配列。準備に用いた各指定元の指紋 |
| `policies` | 下記の除外指定の配列 |
| `default_policy` | 文字列 `snapshot-v1` |

`management_binding.relative_prefix_b64` は、対象0から管理ルートへの相対パスのバイト列。同じ場所なら空のバイト列。
`identity_chain` は、その物理経路の `{component_b64,dev,ino,type:"directory"}` 配列。
`dev`・`ino` は整数。`plan` / 引き継ぎ(`--carry-plan`) / `extend-plan` / `run` で同じ物理経路を確認する。
親ディレクトリのmtime変更だけでは同一性を失ったと扱わない。

各 `policies` 要素は次のキーだけを持つ。

| キー | 型・値 |
|---|---|
| `context_id` | 有効対象の整数 |
| `root_kind` | `worktree` 又は `management` |
| `root_prefix_b64` | 対象の作業ツリーから指定元のルートへの相対パスのバイト列。`worktree` 側は空のバイト列 |
| `history_origin` | タスク自身は `task`、内包された下のリポジトリは `context-start` |
| `start_kind` | `task` は `new-invocation` / `resume-invocation`、`context-start` はnull |
| `branch_provenance` | `workflow-recorded` / `not-recorded`。ブランチの出典(`branch`)の有無と一致 |
| `sources` | 下記の出典の配列 |
| `globs`, `eres` | 検証済みUTF-8文字列の配列。順序付きで重複を除く |

主キーは `(context_id,root_prefix_b64)`。同じ主キーの重複は拒否する。
出典は `{role,commit_oid,profile_blob_oid,profile_sha256,absent}` だけを持つ。
`role` は `current/base/start/branch/held-head/extra/observed-current/observed-head/observed-ref`。
`absent` は真偽値。不在時の `profile_sha256` はnull。
`commit_oid` は現在ファイルの `current` / `observed-current` と空のリポジトリの場合だけnull。
`profile_blob_oid` は不在又は現在ファイルの場合だけnull。

- 全指定に初回の `current`・`held-head` を各1件必須とし、置換しない。空のリポジトリでも役割と不在を省かない。
- `task` は `base`・`start` も各1件必須。`branch` は保持した記録がある場合だけ1件。`extra` は0件以上。
- `observed-*` は `extend-plan` で加えた出典。同じ指定内で、役割・OID・profileのハッシュ値が同じなら重複追加しない。
- 元の全指定・出典・式は、引き継ぎ(`--carry-plan`)と派生先へ残す。別の保護記録、同じ出典の矛盾、縮小、保護記録・管理ルートとの結び付け(`state_binding`・`management_binding`)の不一致は拒否する。
- 計画は16MiB、出典は最大2000件、式1個は64KiB、式全体は1MiBまで。
- `--context-start` は対象番号が0より大きく、独立タスクのGitルートではない下のリポジトリだけ。親の必要な全指定を `--carry-plan` で先に渡す。
- 下のリポジトリの過去の作成履歴まで網羅したとは表示しない。得られている歴史OIDは `--profile-ref` で追加する。

`prepared_request` は `{operation,options,resolved_revisions}`。
`options` は §6 の専用のキーだけを既定値込みで持つ。`--path` / `--under` は `path_b64` / `under_b64` 配列へ変換する。
ほかの名前はハイフンをアンダースコア(`_`)へ変える。未使用の操作のキーは混ぜない。
`resolved_revisions` は使用する役割名から完全OIDへの対応表。正当な未作成HEADだけnullを許す。

`source_checks` は、選択対象と全祖先の必要な指定ごとに1件の配列。
各要素は `{context_id,root_prefix_b64,current_profile,head,resolved_refs}` だけを持つ。
管理側の指定を省略できない。現在のprofileの場所は、保持した相対パスと固定の `.claude/project-profile.yml` から決める。

- `current_profile` は `{absent,sha256,metadata}`。不在時は後2値null。
- `metadata` は `{dev,ino,type,mode,size,mtime_ns,ctime_ns}`。`type` は `regular`、ほかは整数。
- `head` は `{symbolic_target_b64,oid}`。分離HEADの参照先、未作成HEADのOIDはそれぞれnull。
- `resolved_refs` は `{name_b64,oid}` の配列。
- `run` は指紋を先に照合する。新しいYAMLを解析して追加する処理ではない。不一致は22/`preparation-stale`。

除外集合の確認値 `effective_exclusions_sha256` は次の形から作る。

```json
{"policies":[{"context_id":0,"root_kind":"worktree","root_prefix_b64":"","globs":[],"eres":[]}],"default_policy":"snapshot-v1"}
```

各指定を、対象番号と、復号した相対パスのバイト列の順に並べる。`root_kind`・式は実際の値を使う。
キーの順に並べたUTF-8の詰めたJSONで符号化した、実際のバイト列のSHA256とする。非ASCII文字をASCIIエスケープへ置換しない。

## 11. 観測応答の厳密な形式

§7 の `scope` は `{kind:"path"|"under",path_b64,redacted:false}` の配列。
機密一致なら、同じ `kind` の `{kind,path_b64:null,redacted:true}` に置換する。
非機密のパスと出力本文はbase64文字列で保持する。名前のUTF-8変換による置換はしない。

`warnings` は次の定義済みコードだけを辞書順で並べる。

| コード | 条件 |
|---|---|
| `settings-not-applied` | 常に必須 |
| `ignore-not-applied` | 作業ツリーを読む操作と `files` |
| `nested-content-not-observed` | 下のリポジトリの境界がある |
| `sparse-not-materialized` | `status`・`diff` に未展開の記録(record)がある、又は `files` のskip-worktree対象が `absent` |
| `paths-excluded` | 除外がある |

印(skip-worktree)だけあって実体がある場合と、オブジェクトだけの `show`・`diff` には `sparse-not-materialized` を付けない。

比較する片側の値を `endpoint` と呼ぶ。必須のキーは `{kind,git_mode,os_mode,size,sha256,oid}`。
`kind` は `absent/regular/symlink/gitlink/directory/fifo/socket/device`。
得られない値はnull。ただし `absent` の `size` は整数0。内容の `size` は生のバイト長、`gitlink` はnull。
`git_mode` はGitのファイルモードの文字列、`os_mode` は実際の権限の整数。オブジェクトの `os_mode` はnull。
作業ツリーの所有者の実行ビットから作るGitのファイルモードと、実際のファイルモードを別に示す。Gitのファイルモードのない特殊なファイル・ディレクトリはnull。
symlinkの `sha256` は、リンク先の文字列のバイト列のハッシュ値。リンク先の本文ではない。
本文を読まない `files` の `sha256` はnull。ステージ番号は整数、ファイルモードとOIDはGitの形式を使う。

各 `data` は `variant` に応じた次のキーだけを持つ。

| `variant` | 必須のキーと記録(record)の形 |
|---|---|
| `status-v1` | `variant,records,excluded_count,boundaries`。記録は `{path_b64,head,index,worktree,index_flags,stages,staged_comparison,worktree_comparison}` |
| `files-v1` | `variant,records,excluded_count,boundaries`。記録は `{path_b64,tracked,endpoint,index_flags,stages}`。`tracked` はインデックスにあるかの真偽値 |
| `diff-v1` | `variant,left,right,records,excluded_count,boundaries`。記録は `{path_b64,left,right,comparison,patch_b64}` |
| `blob-v1` | `variant,path_b64,endpoint,bytes_b64,excluded_count` |
| `history-v1` | `variant,format,bytes_b64,limit,unborn,excluded_count`。`unborn` は真偽値、`limit` は指定した整数 |
| `refs-v1` | `variant,records`。記録は `{name_b64,oid,symbolic_target_b64,resolution}` |
| `stashes-v1` | `variant,bytes_b64,limit`。`limit` は指定した整数 |

`records`・`boundaries`・`stages`・`index_flags` は配列。`excluded_count` は0以上の整数。
`index_flags` は `skip-worktree` / `assume-unchanged`。`stages` は `{stage,mode,oid}` の配列。
`status` の未解決のインデックスの `endpoint` はnullとし、各ステージを残す。
`boundaries` は `{path_b64,reason}`。`reason` は `nested-repository/submodule/special-file`。
機密境界の名前は出さず除外件数へ加える。

- `status` の `comparison` は `same/added/deleted/modified/type-changed/unmerged/not-materialized/not-observed/untracked`。
- `diff` の `comparison` は `added/deleted/modified/type-changed/not-materialized`。`same` は記録を省く。
- `diff` の `left` / `right` は `index` / `worktree` / `commit:<完全OID>` / `empty-tree:<完全OID>`。可変の参照名を応答の比較元にしない。
- `records` 形式の `patch_b64` はnull。未展開は `patch` 形式でもnullで、削除のパッチを作らず記録を残す。
- 比較可能な全パスが `same` なら `records` は空。全パスが未展開でも空扱いせず、全記録と警告(`warnings`)を返して終了コード0。
- `blob`(ファイルの中身のオブジェクト)の機密のパスは23/`excluded-selection`。通常の不存在も23だが、別の理由(`reason`)。通常のblob又はsymlinkのバイト列だけを返す。
- `history` は固定の `oneline`・`fuller` 形式の全バイト列。署名・装飾・notes・パッチ・色を無効にする。範囲がすべて除外なら空のバイト列を返し、引数なしの全履歴へ広げない。
- `refs` で、シンボリック参照でないものの `symbolic_target_b64` はnull。解決済みは `resolution:"resolved"`、未解決のシンボリック参照はOIDがnullで `unresolved`。有効な未解決参照を一覧から落とさない。循環・不正な参照・読取失敗は23。
- `stashes` は固定 `reflog show --format=oneline --no-decorate --no-abbrev` の全バイト列。参照があるのに必要履歴がなければ23/`incomplete-metadata`。失われた履歴の完全復元は保証しない。

パッチは、専用の通常ファイル `a` / `b` に比較するバイト列だけを置き、固定した `git diff --no-index --no-ext-diff --no-textconv --no-color --no-renames --unified=N -- a b` で作る。
差分ありの終了コード1だけを正常とする。元のパスはGitへ渡さず、応答の記録で対応付ける。
空ファイルの追加・削除、ファイルモードのみ、種類変更は記録を正とする。バイナリは、その通知とハッシュ値・サイズで示す。
完全な適用可能なパッチや通常Gitとの表示一致を保証しない。

## 12. 専用の管理領域と対応形式

最初のGitより前に、保持した保護記録・マニフェストとスナップショット・`context-plan-v1`・入力計画をGit無しで検査する。
現在の設定本文の変更だけで拒否しないが、別のリポジトリへ誘導する物理入口の変更は止める。
専用の0700領域へGit管理領域・空の作業場所・HOME・限定設定を作る。
現在の `config`・`config.worktree`・`hooks`・`include` はコピーせず、保持したSHA-1/SHA-256と、参照の保存形式(files・reftable)と、固定値だけを生成する。
Gitの作業ディレクトリと `GIT_WORK_TREE` は、専用の空ディレクトリ。元の作業ツリーをGitへ渡さない。

| 管理データ | 保持する内容 |
|---|---|
| HEAD/index | 選んだworktreeの現在値。正当な不存在と読取不能を区別 |
| sharedindex | 分割インデックス(split index)が参照する必要ファイル。不足は停止 |
| refs/packed-refs | 共有ディレクトリ(common)の共有参照と、選んだworktree固有の参照。選んでいないworktreeのHEADは使わない |
| reftable | 管理一覧と必要テーブルを同時に取得。勝手に `files` 形式へ変換しない |
| shallow | 共有ディレクトリの履歴境界。読取不能を不在扱いしない |
| stash | `files` 形式は `refs` と `logs`、reftableは保存の仕組み(backend)の中の履歴。履歴欠落を空成功にしない |
| objects | 保持した物理位置と、通常のファイルの alternates。全オブジェクトをコピーしない |

子プロセスの環境は白紙から作り、専用のHOME・空のシステム全体の設定(system)と利用者全体の設定(global)・`GIT_CONFIG_NOSYSTEM`・`GIT_CONFIG_COUNT=0`、専用の `GIT_DIR/GIT_COMMON_DIR/GIT_INDEX_FILE` と固定のオブジェクト保存先を渡す。
既存の安全用のオプションを全Gitに適用し、日和見的更新・遅延取得・対話を止める。親のトレース出力・ページャー・名前空間・追加設定は暗黙継承しない。
元の `objects` は、専用の `objects` の `info/alternates` で読むだけにし、Git が書くオブジェクトは専用の側へ置く。部分インデックスのリポジトリの時刻の例外は §8。
未保持の `GIT_OBJECT_DIRECTORY/GIT_ALTERNATE_OBJECT_DIRECTORIES` 環境指定は扱わない(23/`unsupported-object-environment`)。通常のファイルの alternates は扱う。

コピー対象の前後で種類・デバイス番号とinode番号(ファイルの実体の番号)・ファイルモード・サイズ・mtime/ctimeと内容のハッシュ値、列挙集合を確認する。
保持された正当なリンクは同じ実体との対応を確認する。任意のディレクトリへのリンクを辿らない。
検出した差替え・増減・読取不能を警告だけで成功へ進めない。
対応形式が実行Gitにない場合は23で停止し、未検証の環境を成功と表示しない。
