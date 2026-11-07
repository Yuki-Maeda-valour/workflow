# 取り込みと品質ゲートの流し直し(ship-task の Phase 4.5)

## 1. この文書の位置づけ

ship-task の Phase 4.5(PR を開く前に、取り込み先の最新を作業ブランチへ取り込み、品質ゲート〈書式・型・テスト・ビルドの自動の検査〉を流し直す工程)の正本(ほかが合わせる元)。

- 取り込みの後のレビューは、reviewer(変更を確かめる AI)が行う。修正と衝突の解決は、implementer(実装を受け持つ AI)に任せる。team-lead(作業を進め、結果を確かめる側の AI)は実装しない。
- 取り込み先から見た作業の差分が、レビューした差分と同じなら、補助スクリプト `integrate-guard.py` が機械で確かめて続ける。衝突したとき・差分が変わるときは、レビューし直す。
- 権威参照ファイル(AI への指示をまとめたプロジェクトのファイル)に、見出し `PR の前の手順` の節があれば、取り込みの後にその手順を行う。
- 記号:
  - D: レビュー済みの最後の commit(Phase 4 の後の作業ブランチの先頭)
  - B: 取り込み先(PR の base。デフォルトブランチ)の最新の commit
  - MB: D と B の共通の祖先
  - M: 取り込みの commit
  - P: 取り込みの後のレビューの commit(レビューし直しと、PR の前の手順のファイルの変更をまとめたもの。変更が無ければ空の commit)
- 作業ブランチへ取り込み先を merge(別に進めた変更を取り込むこと)するのは、この Phase だけ。取り込み先へは merge しない(原則 5)。
- 関連する文書:
  - 補助スクリプトの契約は §4。
  - レビューの CLI は [review-protocol.md](../../do-task/references/review-protocol.md) の最初の節。
  - implementer に任せる前の準備は [implementation-git.md](../../do-task/references/implementation-git.md)。
  - Issue 方式の保持値は [github-issue.md](../../do-task/references/github-issue.md) §5〜6。

## 2. 行うときと行わないとき

- 行うのは、対話で、Phase 5 で push・PR をする実行だけ。
- 行わないとき(理由を報告に書く):
  - `--unattended`・`--discover`・`--no-pr`・`--design-only`
  - Phase 0 の 5 で push・PR をしないと決めたとき
  - parent-child(管理ルートと source のルートが別の構成)
  - 手順 0 の `repo` が null のとき
  - 無人のタスクのイテレーション(無人ループの 1 回分の実行)は、PR 本文にも書く(§8)。発見のイテレーションの PR 本文は [discover-mode.md](discover-mode.md) §8 のまま変えない。
- 行わないときに、権威参照ファイルに `PR の前の手順` の節があれば、節の手順は行えない手順として扱う。
  - 対話では、手順 12 のとおり PR を開かずに止める。
  - 無人のタスクのイテレーションは、PR 本文に「PR の前の手順を行っていない(節あり)」と書き、merge の前に人が行う。
- 無人のタスクのイテレーションで行わない理由:
  - 許可の仲介(無人の実行で、操作を許すかをその場で判定する仕組み)が、取り込みに要る fetch と merge を拒否する。
  - イテレーションの共有の状態の照合(照らし合わせて確かめること)が、取り込み先の追跡の参照が変わると止まる。
  - イテレーションの取り込み先は、起動時に固定した SHA(commit を指すハッシュ値)で、イテレーションの中で進まない。
  - 取り込み先が進んだかは、人が merge の前に確かめる。PR 本文には作業ブランチの作成時の SHA を書く。
- parent-child で行わない理由: source と親のそれぞれに状態の記録が要り、親側の commit の照合も足す必要がある。

## 3. 手順

0. 公開するルートで `origin-repo.py` を実行し、判定を保持する(Phase 5 の 1 でも使う)。
   - `repo` が null なら、Phase 4.5 を行わない。
   - Issue 方式は、保持した Issue の `repo` と同じことも確かめる([github-issue.md](../../do-task/references/github-issue.md) §6)。
1. 取り込みの前の根拠: 最後の commit の状態の記録で `verify --mode commit` と `pr-evidence` を行い、出力と D を保持する。
   - この状態の記録の入口には、元の ERE の列(その記録を作ったときの列。E0)を渡す。
   - Issue 方式は先に本文を取り直し、R(本文ダイジェスト〈本文から計算した短い値。本文が変わると値も変わる〉)と生の本文を確かめる。
2. `integrate-guard.py merge` を、手順 0 の `repo` と、`review-guard.py` に渡すのと同じ機密の除外の ERE で実行する。
   - 標準出力の JSON を `.claude/reviews/` のファイルに書き、その SHA-256 を保持する。
   - `--exclude-ere` には E0 を渡す。JSON の `exclude_ere`(E0 の後ろに、B の profile にだけある新しい機密の glob の ERE を足した列。E1)を今の列とし、`added_globs` を手順 5 の判断のために保持する。どの処理にどの列を渡すかは §11。
   - 起動の字面(引数はすべて書く。`<ERE>` には E0 を渡す):

     ```
     python3 {ship-task の}scripts/integrate-guard.py merge --dir=<管理ルート> --branch=<作業ブランチ> --reviewed-sha=<D> --base=<デフォルトブランチ> --repo=<repo> --exclude-ere=<ERE>
     ```

   - `up-to-date`・`merged` は手順 4 へ、`needs-review` は手順 3 へ進む。
   - 終了コード 1・2 は止める。理由を報告し、人が取り込む。
     - 事前検査で承認が要るもの(filter・include・textconv・インデックスの印など)が見つかったとき。
     - 取り込み先がそれを足したとき。
     - merge driver の設定があるとき。
     - 作業ツリーの名前のぶつかり・作業が取り込み先に含まれるとき。
     - 取り込みの途中の状態が残ったとき(時間切れを含む。§9)。
   - 終了コード 4(`secret-overlap`)は、機密のファイルの取り込みをレビューできないので止める。そのパスの名前(中身は書かない)を示して、人が取り込むよう知らせる。
   - `merged` と `needs-review` のときは、Phase 0 の 4 の「レビュー差分の外で PR に入る commit」の一覧を、取り込みの後の `refs/remotes/origin/<デフォルトブランチ>` と比べて求め直す。
3. 衝突の解決と M(`needs-review` のときだけ):
   - `binary_conflicts` が空でなければ止める(印の無い衝突は人が解く)。
   - 理由が `conflict` なら、§5 の依頼 A で implementer に衝突を解かせる。
     - 片方が消し、片方が変えた衝突(インデックスの段 2 か段 3 が無い)は、`binary_conflicts` に入らない。残すか消すかを解かせる。
     - 戻ったら、§11 の「作り直し」で今の列を作り直してから、`stage-resolved` を実行する。
     - 終了コード 1 なら、理由を添えて差し戻す(相手に返して、やり直してもらうこと)。同じ理由で 2 回続けて通らなければ止める。
     - 起動の字面:

       ```
       python3 {ship-task の}scripts/integrate-guard.py stage-resolved --dir=<管理ルート> --branch=<作業ブランチ> --reviewed-sha=<D> --base-sha=<B> --exclude-ere=<今の列> --merge-result=<merge の JSON のファイル> --expect-merge-result-sha256=<保持した値>
       ```

   - 衝突の項目が 0 件になったら(`diff-changed`・`multiple-merge-bases` は初めから 0 件)、`commit` で M を作る。引数は `stage-resolved` と同じ字面で、サブコマンド名だけを変える。
     - 終了コード 4(`secret-overlap`)は、手順 2 の終了コード 4 と同じく止める。
     - M は、手順 8 でレビューし直すまでレビュー前。
     - 起動の字面:

       ```
       python3 {ship-task の}scripts/integrate-guard.py commit --dir=<管理ルート> --branch=<作業ブランチ> --reviewed-sha=<D> --base-sha=<B> --exclude-ere=<今の列> --merge-result=<merge の JSON のファイル> --expect-merge-result-sha256=<保持した値>
       ```

4. 権威参照ファイルの見出しが `PR の前の手順` の節を、取り込んだ後の作業ツリー(M か D)から読む。
   - 見出しの深さは問わない。最初の 1 つを使う。節は、次の同じか浅い見出しの前まで。
   - D の作業ツリーの節と違えば、そのことを報告に書く。
5. 次の 2 つを分けて決める。
   - 節の手順を行うか: 節があれば、取り込みの結果(`up-to-date` を含む)を問わず、手順 7 で必ず行う。履歴を読む確かめだけ(と人が行う手順)の節でも、確かめを流し、落ちたら公開しない。
   - 状態の記録・レビュー・品質の検査(手順 6・8・9)が要るか: 次のどれかのとき要る。
     - M がある(`merged` か `needs-review`)。
     - 節がファイルを変える手順を持つ。
     - 節が作業の中身を検査するコマンドを持つ。
     - `merge` で新しい機密の glob が足された(`added_globs` が空でない。`up-to-date` で節が無いときも)。
   - どれも無ければ、手順 6・8・9 を飛ばす(最後の `run-checks` が同じ tree で流してある)。
   - 飛ばしたときに、手順 7 で節の確かめがレビューを求める終了コード(§6)を返したら、そこで手順 6 を行い(確かめはファイルを変えないので、作業ツリーは HEAD のまま)、手順 8・9 へ進む。
6. (状態の記録が要るとき)`integrate-guard.py worktree` を `--baseline` なしで実行して、作業ツリーが HEAD と同じことを確かめる。
   - 終了コード 0 以外は止める。
   - 出力の JSON を `.claude/reviews/` のファイルに書き、その SHA-256 を保持する(基準 U0)。
   - HEAD(M か D)で新しい `start` を取る。
7. 節の手順を、書かれた順に、§6 の分け方で行う。
   - 節に、取り込み・状態の記録・レビュー・commit の手順が書いてあっても、この手順の同じ所で 1 回だけ行う(二度しない)。
   - 依頼 B・C を出すたびに、その対象のパスを「許すパス」に足して保持する(手順 6 の `start` の後に出した依頼 B・C の対象の和)。
8. (状態の記録が要るとき)`take --phase doc` を取る。
   - `take` の直前に、§11 の「作り直し」で今の列を作り直す。続けて `integrate-guard.py worktree` を、基準 U0(`--baseline` とその SHA-256)・許すパス(`--allow-path` の繰り返し。無ければ付けない)・今の列で実行し、終了コード 0 を確かめる。
   - `take` には今の列を渡す。`take` の後は、その状態の記録の入口(`seal`・`run-checks`・`attest`・`verify`・`pr-evidence`)に、その列をそのまま渡す。
   - スナップショット(ある時点の内容をそのまま控えたもの)は、HEAD を基準にした差分(節のファイルの変更。無ければ空)にする。
     - `diff-snapshot.sh` には、`take` に渡したのと同じ列を `--exclude` の繰り返しで渡す。`--secret-profile-ref` は渡さない(その時点の profile から作り直されて、状態の記録の列と食い違わないようにする)。
     - `needs-review` だったか、節の確かめが求めるときは、B を基準にした差分(取り込み先から見た作業の差分の全体)と、`merge` の JSON も足す。
     - 準備ができたら `seal` する。
   - レビューが要るのは、`needs-review` だった・節のファイルの変更がある・節の確かめが求める、のどれかのとき。
     - reviewer の編成は、Phase 3 の do-task の実装のレビューと同じ(`--reviewers` を透過する)。起動は [review-protocol.md](../../do-task/references/review-protocol.md) の「reviewer の起動」に従う。
     - レビューし直しと節の変更は、1 回のレビューにまとめる。
   - 有効と確かめた指摘は、§5 の依頼 B で implementer に差し戻す。
     - 戻ったら §5 の「戻った後の確かめ」を通し、同じ `start` から `take`・レビュー・検査をやり直す。
     - 反復の上限と判断を仰ぐ点は、review-protocol.md の「反復の終了条件とセーフティ」と同じ。未収束は止める。
9. (状態の記録が要るとき)全員 APPROVED の後(レビューが要らないときはすぐ)に `run-checks` で流し直す。
   - 計画は、取り込んだ後の profile・設定・権威参照ファイル(M か D の作業ツリー)から作る。do-task と同じ順(profile の `quality` → 動的検出 → 権威参照ファイル)で必須の品質コマンドを解決し直し、タスクのチェックリストのコマンドと、節の作業の中身を検査するコマンドを足す。取り込んだ後の profile や設定が必須の検査を足したときも流すため。
   - 前の計画との差(足された・変わった・消えたコマンド)を報告と PR 本文に書く。前の計画にあって、解決し直した計画に無いコマンドは流さない(取り込んだ後の決まりに従う)。
   - `--timeout` は §6 の決まった 1 行の秒数。無ければ既定の 600。終了コード 0 以外は止める。
   - レビューをしたときは、`attest`→ステージ→`verify --mode stage`→commit(P)→`verify --mode commit`→`pr-evidence` を行う。変わったファイルが無ければ、P は空の commit(`--allow-empty`)にする。レビューをしないときは commit しない。
   - 実動確認(実際に動かして、変更どおりに動くかを見る確認)はしない。取り込みと節の手順は、タスクの完了条件を変えない。
10. 節の、P を前提にする確かめ(§6)を、作業ツリーで実行する。
    - P が無いときは、手順 9 の後に流す。手順 9 を飛ばしたときは手順 7 の後に流す。
    - 終了コード 0 以外は止める。
11. 公開する SHA を決める: P があれば P、無く M があれば M、どちらも無ければ D。
12. 節に ship-task が行えない手順が残るときは、Phase 5 へ進まずに止める(PR を開かない。原則 4)。
    - §9 の報告に、人が行う手順と、公開に要る値を書く。
    - PR 本文の下書き(§8)も `.claude/reviews/` に書いて、そのパスを報告する。

## 4. `integrate-guard.py` の契約

契約の正本は `scripts/integrate-guard.py` とそのテスト(`scripts/test_integrate_guard.py`)。ここは、手順が頼る要点だけを書く。

**サブコマンドと引数**(必須。`--exclude-ere` は 1 つ以上で、繰り返せる):

| サブコマンド | 引数 |
|---|---|
| `merge` | `--dir`・`--branch`・`--reviewed-sha`・`--base`・`--repo`(host/owner/name の形)・`--exclude-ere` |
| `stage-resolved`・`commit` | `--dir`・`--branch`・`--reviewed-sha`・`--base-sha`・`--exclude-ere`・`--merge-result`・`--expect-merge-result-sha256` |
| `check` | `--dir`・`--sha`・`--reviewed-sha`・`--base-sha`・`--exclude-ere` |
| `worktree` | `--dir`・`--branch`・`--head`・`--exclude-ere`。任意で `--baseline` と `--expect-baseline-sha256`(2 つは一緒に渡す)・`--allow-path`(繰り返せる) |
| `exclude` | `--dir`・`--reviewed-sha`・`--base-sha`・`--previous`・`--expect-previous-sha256`(`--exclude-ere` は取らない。列は `--previous` の中から読む) |

**出力**: 標準出力に JSON を 1 行。欄は `result`・`reason`・`base`・`base_sha`・`reviewed_sha`・`merge_base`・`head`・`overlap`・`problem_paths`・`binary_conflicts`・`index_sha256`・`changed_paths`・`untracked`・`exclude_ere`・`exclude_globs`・`added_globs`(使わない欄は `null` か空の一覧)。

- 結果(`result`): `merged`(差分が同じ。M を作った)・`up-to-date`(取り込む commit が無い)・`needs-review`(衝突・差分が変わる。merge の途中のまま)・`resolved`(`stage-resolved`。衝突の項目が 0 件)・`committed`(`commit`)・`match`・`mismatch`(`check`)・`secret-overlap`・`excluded`(`exclude`)・`ok`(`worktree`)・`error`(止まったときの JSON)。
- `needs-review` の理由(`reason`): `conflict`(衝突)・`diff-changed`(衝突は無いが、取り込み先から見た作業の差分が変わる)・`multiple-merge-bases`(共通の祖先が複数)。
- `exclude` は、`reviewed_sha`・`base_sha`(`--previous` から受け継ぐ。引数と同じ値)と、足した後の `exclude_globs`・`exclude_ere`、`added_globs` を必ず書く。次の `exclude` の `--previous` に、この出力を渡すため。
- パスの一覧は切らずに全件を書く。後の判定(`stage-resolved`・`commit`・`worktree`)は、このファイルの全件を使う。人に見せる報告と PR 本文では、先頭の 100 件と件数だけを書く(§8・§9)。
- `index_sha256` は、衝突の外のインデックスの要約。`stage-resolved` と `commit` が、merge の直後から変わっていないことを確かめる(衝突の外でステージした変更が、レビューされずに M に入るのを防ぐ)。

**終了コード**:

| サブコマンド | 終了コード |
|---|---|
| `merge` | 0 = `merged` か `up-to-date`。3 = `needs-review`(merge の途中のまま。`MERGE_HEAD` = B)。4 = `secret-overlap`(機密の除外に当たるパスに、レビューも照合もできない変更が入る。何も変えないか、merge を中断する。`problem_paths` はそのパス)。1 = 前提・事前検査・origin・fetch・名前のぶつかり・merge driver の設定・時間切れ・git の失敗と、作業が取り込み先に含まれるとき(D が B の祖先で B と違うとき。D = B は `up-to-date`。何も変えないか、merge を中断する)。2 = 使い方の誤り |
| `stage-resolved` | 0 = `resolved`。1 = 前提・事前検査の失敗・衝突の印が残る・衝突の外が変わった・印の無い衝突・通常ファイルでない衝突。2 = 使い方の誤り |
| `commit` | 0 = `committed`。4 = `secret-overlap`。1 = 前提・事前検査・git の失敗。2 = 使い方の誤り |
| `check` | 0 = `match`。1 = `mismatch`・事前検査・算出の失敗。2 = 使い方の誤り |
| `worktree` | 0 = 確かめに通った。1 = 通らない(事前検査を含む)。2 = 使い方の誤り |
| `exclude` | 0 = 列を出した。4 = `secret-overlap`(新しい glob に当たるパスで、HEAD の項目が D の項目とも B の項目とも違う)。1 = 事前検査・profile を読めない・ERE に変えられない・`--previous` が合わない。2 = 使い方の誤り |

`needs-review` を返す終了コード 3 は `merge` だけ。止めるときの文は、標準エラーに出る。

**列の渡し方**: 機密の除外の列は、要素ごとに順を変えずに `--exclude-ere` を繰り返して渡す(1 つにまとめない。[review-protocol.md](../../do-task/references/review-protocol.md) の「共通引数と保持値」と同じ)。`stage-resolved`・`commit`・`worktree` は、列の要素ごとに 1 つずつ比べる。

**機密の除外の列**: `merge` は、B を取った後、B の tree の profile にだけある新しい機密の glob(パスの型)を ERE(パスに当てる拡張正規表現)にして、渡した列(E0)の後ろに足す。`exclude` は、前の JSON の `exclude_globs` に無い、今の作業ツリーの profile の glob を足す。どちらも列を減らさず、順を変えない。足した glob は `added_globs` に書く。

- `stage-resolved`・`commit` は、渡した `--exclude-ere` が merge の結果のファイルの `exclude_ere` をすべて含むことを確かめる。
- `worktree` は、`--baseline` を付けたとき、基準の JSON の `exclude_ere` をすべて含むことを確かめる。

**事前検査**: `diff-snapshot.sh --precheck` を `--accept` なしで通す。

- どのサブコマンドも、作業ツリーや保存済みの中身を読む前に行う。`merge` は merge の後にも行い、通らなければ merge を中断する。
- 止めるときの文: `事前検査が止めた`・`取り込みを始められない`(作業ツリーは変わっていない)・`取り込みの途中の状態が残った`・`取り込み先が足すファイルと同じ名前のものが作業ツリーにある`・`印の無い衝突`・`作業ツリーの中身が HEAD と違う`。

**merge の起動と時間切れ**:

- merge driver(`merge.default`・`merge.<名>.driver`)の設定があれば、取り込みをせずに終了コード 1 で止める(人が取り込む。設定したコマンドが merge の中で動くため)。
- merge の起動には `--no-verify-signatures` を付ける(署名の確かめを起動しない)。
- merge と、その中断の時間切れ:
  - 時間切れで `MERGE_HEAD` があれば、衝突の有無によらず、中断を試して終了コード 1 で止める。
  - `MERGE_HEAD` が無い時間切れと、中断の時間切れは、`取り込みの途中の状態が残った`(終了コード 1)。
- 環境変数 `INTEGRATE_GUARD_NET_TIMEOUT` は、テストで時間切れを試すために、fetch・merge・merge の中断・M の commit の上限 120 秒を短くするだけ。半角の 1〜3 桁の 1〜120 だけを受け付け、ほかは `usage:` と終了コード 2。この手順からは設定しない。

**git に付けるオプション**: どの呼び出しも、`publish-guard.py` の `SAFE_GIT_PREFIX` と同じ中身の定数を先頭に置く。

- fetch・merge・commit には、`rerere.enabled=false` と `merge.conflictStyle=diff3` も付ける(衝突の印を、共通の祖先の版を含む形にそろえる)。commit は `--cleanup=strip` で行う。
- 作業ツリーを読む操作と、作業ツリーからステージする操作には、`core.filemode=true` と `core.symlinks=true` も付ける。

**取り込みの照合の 5 つの条件**:

1. M の親が順に D と B の 2 つだけ(`check` だけ)。
2. 共通の祖先がちょうど 1 つ。
3. B と M の差分のパスの集まりが、作業の差分 W のパスの集まりと同じ。
4. 取り込み先の差分 X に無いパスは、差分の行が W と同じ。
5. X にもあるパスは、差分の本文(`--text` で取る)から `index ` の行を除き、`@@` の行を `@@` にして、Python の中で比べて同じ。

**作業ツリーの読み方**: ルートから親の各階層を辿って開き、リンクを辿らない(`review-guard.py` の読み方と同じ)。作業ツリーの中身が HEAD の blob と違うときは、何も書き換えずに止める(§10)。

## 5. 内蔵の implementer への依頼

役割語(AI の役割の名前)で委託(作業を別の AI に任せること)し、ホストへの対応づけは、do-task の解決表(役割の名前を、実際に使う AI の仕組みに対応づける表)に従う([delegation-map.md](../../do-task/references/delegation-map.md))。外部の implementer は使わない。

**共通の前提**(どの依頼も、委託と差し戻しの再依頼のたびに、直前に行う):

- [implementation-git.md](../../do-task/references/implementation-git.md) の必要環境の確認を通し、`implementation-git.py prepare --cwd <ルート> [--accept <このセッションで最後に承認した値>]` を実行する。衝突の途中でも動く。
- 返った作業ディレクトリ・承認値・照合値の 3 つと、入口のパスを依頼に渡す。implementer は、Git の状態・差分・履歴を `run` で読む。
- どれかに失敗したら、委託・再依頼せずに止める。別の AI を使えない環境では、委託せずに止める(レビューも別の AI でできないため)。
- 依頼に必ず入れること:
  - 返答の先頭の 1 行に、自分のモデルの名前と版(`モデル: <名前と版>`。分からなければ `モデル: 未確認`)。
  - 機密ファイル(`secret_paths`)を読まない・報告に値を含めない。
  - Git の書き込み(ステージ・commit・merge・reset など)をしない。
  - 依頼した対象のファイルのほかを変えない。

**依頼 A**(衝突の解決。手順 3):

- 対象は `problem_paths` のファイルだけ。機密の除外に当たるパスは、`merge` が終了コード 4 で先に止めるので来ない。来たら委託せずに止める。
- 衝突の印(`<<<<<<<`・`|||||||`・`=======`・`>>>>>>>`)の 3 つの版(作業・共通の祖先・取り込み先)を読み、作業の意図と取り込み先の意図の両方を残して、印を消す。
- 片方が消し、片方が変えた衝突(印が無い)は、タスクの本文と取り込み先の変更から、残すか消すかを決める。
- 読む材料は、タスクの本文(設計書)と、D と B の件名。
- 返すことは、解いたファイルと、衝突ごとの解き方(1 行ずつ)。解けない所があれば、その理由。

**依頼 B**(M の後のレビューの修正。手順 8):

- 対象は、有効と確かめた指摘と、その指摘の対象のファイル。衝突の外・作業の差分・`diff-changed` と `multiple-merge-bases` の組み合わせへの指摘を含む。
- 返すことは、直したファイルと、指摘ごとの直し方。

**依頼 C**(節のファイルを変える手順。手順 7):

- 対象は、節の該当する手順の文と、変えるファイル。
- 返すことは、変えたファイルと、手順ごとに何をしたか。

**戻った後の確かめ**(依頼 B・C):

- 先に §11 の「作り直し」で今の列を作り直す。
- 続けて `integrate-guard.py worktree` を、今の列・基準 U0 と、手順 6 の `start` の後に出した依頼 B・C の対象の和を `--allow-path` に渡して実行し、終了コード 0 を確かめる。
  - 確かめること: HEAD とブランチが変わらない・ステージしていない・変わったパスと新しい未追跡のパスがどれも許すパスの中にある・機密のパスを変えていない。
  - 前の依頼で変えたファイルを、後の確かめで拒まない。新しいファイルを足す修正(テストのファイルを足すなど)も、そのパスが依頼の対象にあれば許す。
- 通らなければ差し戻す。
- 依頼 A は `stage-resolved` で確かめる。

## 6. PR の前の手順の行い方

節の手順を、次の 5 つに分けて行う。どれに当たるかは、節の文から team-lead が決める。

- ファイルを変える手順(版を上げるなど): §5 の依頼 C で implementer に任せる。手順 8・9 でレビューして P にする。
- 作業の中身を検査するコマンド(テストなど): `run-checks` の計画に足す。`run-checks` は、レビューした中身だけを持つ 1 commit の一時のリポジトリで、`argv` をシェルを通さずに起動する。
  - 上限の秒数は、節の中の決まった 1 行だけから読む。形は `run-checks の上限: <秒> 秒`(行頭の `- ` は付けてもよい。秒は 1 以上の整数)。
  - 照合パターン(`grep -E`): `^(- )?run-checks の上限: [1-9][0-9]* 秒[[:space:]]*$`。最初の 1 行を使う。ほかの文の秒数(表の測った時間など)は使わない。無ければ既定の 600 秒。
- リポジトリの履歴(origin・HEAD・ほかの commit)を読む確かめ: `run-checks` のコピーには履歴が無いので、作業ツリーで実行する。
  - 節が「レビュー済みの SHA」を求めるときは D を渡す。
  - P の前に流す確かめは、節がレビューを求める終了コード(例: 作業の差分が違うときの 2)と書いたもののほかは、0 以外なら止める。確かめの結果が、レビューに足すもの(作業の差分の全体など)を決めるときは、それに従う。
  - P を前提にする確かめは、手順 10 で P の後に流す。
- PR 本文に書く欄: §8 に足す。持っていない値(受領書に残らないコマンドの出力・件数など)は作らず、「受領書に残らない」と書いて、完了報告の「残っていること」に書く。
- ship-task が行えない手順(そのまま起動できる `argv` が節に無い・環境の用意を人に任せる・手で行うと書いてある): 行わない。手順 12 で止め、`人が行う手順` として報告する。

## 7. push の直前の照合

Phase 5 の 1 の前に行う。

- `merged` の M があれば(P があってもなくても): `integrate-guard.py check --dir=<管理ルート> --sha=<M> --reviewed-sha=<D> --base-sha=<B> --exclude-ere=<今の列>` が終了コード 0。`<今の列>` は、手順 2 の merge の JSON の `exclude_ere` に、§11 の作り直しで足した後の列。
- P があれば: 手順 6 の状態の記録で `verify --mode commit`(根拠つき)。
- P が無く、手順 6 の状態の記録があれば: `integrate-guard.py worktree --dir=<管理ルート> --branch=<作業ブランチ> --head=<M か D> --exclude-ere=<今の列> --baseline=<U0 のファイル> --expect-baseline-sha256=<保持した値>`(`--allow-path` なし)が終了コード 0 で、その状態の記録で `verify --mode pre-stage`(その記録の `take` の列で)。取り込む commit が無く(HEAD = D)、節の検査のコマンドだけを流したときも、新しい機密の glob だけが足されたときも同じ。このときは、最後の commit の状態の記録の照合をしない(新しい機密の指定が足された回は、古い列 E0 で、新しく機密になったファイルの中身を読み直すことになる。新しい記録で commit の照合をしても、記録の HEAD の上の新しい commit 1 つを求める `review-guard.py` の決まりに通らない)。
- Phase 4.5 の状態の記録(手順 6)も P も無ければ: 最後の commit の状態の記録で `verify --mode commit`(今と同じ。その記録の列 E0 で)。
- `publish-guard.py` の `--sha` には、公開する SHA を渡す。`publish-guard.py` は、作業ブランチの参照と作成した PR の `head.sha` が `--sha` と同じことだけを照らす。

## 8. PR 本文に書くこと

`## 取り込み` の節に書く。

- 取り込み先の名前と B。
- 結果(次のどれか):
  - 取り込んだ(差分が同じ。M)。
  - 衝突を解いて取り込み、レビューし直した(M・P・衝突したファイル・解き方)。
  - 取り込みでレビューし直した(差分が変わったファイル)。
  - 取り込む commit が無かった。
  - 無人のタスクのイテレーションでは行っていない(作業ブランチの作成時の SHA。節があれば「PR の前の手順を行っていない(節あり)」)。
- 両方が変えたファイル(コードブロック)。パスの一覧は、先頭の 100 件と件数を書く(判定には `merge` の JSON のファイルの全件を使う)。
- 流し直した品質ゲート: 受領書の各コマンドの名前・`argv`・終了コード。
  - 受領書のハッシュ値が保持した値と一致すると確かめてから書く。書き換えられるタスクの記録からは書かない。
  - 件数などの出力は受領書に残らないと書く。前の計画との差も書く。
- 取り込みの後のレビュー: P の `pr-evidence`。
- PR の前の手順: 節の有無・行った手順・P・履歴を読む確かめの終了コード・節が求める欄。

## 9. 止まったときの報告

- 書くこと(パスの一覧は先頭の 100 件と件数):
  - D・B・M・P の SHA、止まった理由と落ちたコマンド、作業ブランチの先頭。
  - 手順 1 で保持した `pr-evidence` の出力。
  - 各状態の記録の CHECKS・EVIDENCE のハッシュ値(保持した値)。
  - 公開する SHA、PR 本文の下書きのパス、人が行う手順。
- merge の途中で止まったときは、その状態(解いたファイルを含む)を残し、取り消し方(merge の `--abort`)と、解いて続けるときの手順を書く。`integrate-guard.py merge` が `取り込みの途中の状態が残った` で止まったときも、作業ツリーを確かめるまで何も消さずに、そのことを書く。
- 続け方の選択肢を書く:
  - 同じセッションで保持した値が残っていれば、作業ブランチを D に戻して(作業ツリーに変更が無いことを確かめてから)、Phase 4.5 を初めからもう一度行える。`integrate-guard.py merge` は、作業ブランチの先頭が D であることを求めるため。
  - 人が行う手順だけが残ったとき(節がそう書くリポジトリのふつうの流れ)は、人がその手順を行い、報告の値(公開する SHA・PR 本文の下書き・`publish-guard.py` に渡す値)で公開する。権威参照ファイルの手順に従う。
- M が作業ブランチに残ったまま `/do-task` で直すときは、基準コミット(作業を始めた時点のコミット)からの差分に取り込み先の変更が入ることを書く。
- Issue 方式は [github-issue.md](../../do-task/references/github-issue.md) §6 の push より前の失敗として扱う。M か P が作業ブランチにあれば、同じ状態の記録での Phase 5 のやり直しはできない(最後の commit の状態の記録の照合が通らない)。

## 10. 限界

- 取り込みの照合は「作業の差分が変わらない」ことだけを確かめる。組み合わせの正しさは、品質ゲートが確かめる範囲だけ。
- 事前検査で承認が要るもの(filter・include・textconv・インデックスの印など)を使うリポジトリと、取り込み先がそれを足したときは、取り込みをしない(事前検査を `--accept` なしで、merge の前と後に通すため)。
- merge driver の設定(`merge.default`・`merge.<名>.driver`)があるときも、取り込みをしない。設定したコマンドが merge の中で動くため。人が取り込む。
- 印の無い衝突(`-merge`・`merge=binary` などのバイナリ)は、ship-task では解かずに止める。
- 衝突の印の検査は既定の長さの印だけを見る(`conflict-marker-size` の属性は見ない)。見逃した印は、レビューと品質ゲートが捉える範囲だけ。
- 取り込み先が足した属性で、global の設定にある filter は、merge の後の事前検査で中断する前に、merge の中で 1 回動きうる(事前検査は merge の後にしか、その属性を見られないため)。
- `LFS の実体を取り出したリポジトリ` と、改行を変換するリポジトリ(`core.autocrlf` など。global の設定を含む)では、取り込みを公開まで進めない(fetch の前か、手順 6 の `worktree` で止める)。
  - `merge` が fetch の前に止めるのは、変換や filter が作業ツリーの中身に既に出ていて、commit の中身と違うときだけ。
  - 設定を後から足したとき(作業ツリーは LF のまま)や、取り込み先が `eol`・`text` の属性を足したときは、`merge` は止めずに M を作る。その後の手順 6 の `worktree` が終了コード 1 で止める。§9 のとおり、作業ブランチを D に戻して人が確かめる。
  - サブモジュールの中身は照らさない。
  - LFS(大きなファイルを別の置き場に置く git の仕組み)の実体は取り出さない(安全用のオプションで LFS の変換を止めるため)。
- 時間切れの扱いは §4 のとおり。取り込みの途中の状態が残ったときは、人が確かめる。
- `run-checks` はコマンドを 1 本ずつ流すので、重いコマンドが多いと時間は足し算になる。

## 11. 機密の除外の ERE の列

- 列は、順と中身を変えずに後ろへ足すだけにする(減らさない。`start` の後の機密指定の追加と同じ扱い。[review-protocol.md](../../do-task/references/review-protocol.md) の「共通引数と保持値」)。
- E0: Phase 4.5 の前の状態の記録の列。手順 1 の照合と、§7 の最後の箇条(Phase 4.5 の状態の記録も P も無いとき)の照合に渡す。Phase 4.5 の状態の記録があるときは、E0 の照合を公開の前に行わない。
- E1: `merge` の JSON の `exclude_ere`。E0 の後ろに、B の profile にだけある新しい機密の glob(`added_globs`)の ERE を足したもの。`exclude_globs` も保持する。
  - 足すかどうかは glob で決める。E0 はまとめた 1 本の ERE なので、ERE の字面では比べられない。
- 作り直し: implementer から戻った後(依頼 A・B・C)と、`take` の前に、`integrate-guard.py exclude` を実行する。
  - 起動の字面:

    ```
    python3 {ship-task の}scripts/integrate-guard.py exclude --dir=<管理ルート> --reviewed-sha=<D> --base-sha=<B> --previous=<前の merge か exclude の JSON のファイル> --expect-previous-sha256=<保持した値>
    ```

  - 出力の JSON をファイルに書いて SHA-256 を保持し、その `exclude_ere` を今の列にする(今の作業ツリーの profile に新しい glob が足されていれば、後ろに足される)。
  - 終了コード 4 は止める(後から機密の指定に当たったパスの中身が、取り込みで D とも B とも違う)。
- 手順 3 からの `integrate-guard.py` の各サブコマンド(`check` を含む)と、手順 6 からの状態の記録には、今の列を渡す。
  - 状態の記録の列は、その `take` の後は固定する(`review-guard.py` は、入口に渡す列が保存した列と完全に同じことを求める)。
  - 後で列が増えたら、同じ `start` から `take` をやり直す。
- `diff-snapshot.sh` には、`take` に渡したのと同じ列を `--exclude` で渡し、`--secret-profile-ref` は渡さない。
- 機密の指定が足されたパスの中身は、どの処理も読まず、検査用のコピーにも入れない。そのパスを変えた修正は、`worktree` の確かめで止まる(機密のパスを変えた)。
