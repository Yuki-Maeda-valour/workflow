# レビュープロトコル詳細(do-task Phase 6)

## review/commit 照合

commit が続くタスクでは `{ship-task の}scripts/review-guard.py` を使う。この節が CLI と呼出順序の正本。
対象集合と review 入力を別々に hash し、その組を `REVIEW_BINDING_SHA256` に結び付ける。
ソースの再 review と、review 返答を反映した最終 task 記録の照合を分ける。記録を追記するたびに再 review する必要はない。

### 共通引数と保持値

全入口に `--cwd <commit する Git root> --state <state> --exclude-ere <同じ ERE>` を渡す。
ERE は diff snapshot で解決した機密除外 union と同じ集合。複数なら `--exclude-ere` を繰り返す。
start 後の機密指定追加は、start の各 ERE を残し take へ新しい union ERE を追加する。削除・縮小は拒否する。
take 以降は同じ ERE 列を固定して全入口と snapshot に使う。新たな機密 path の開始時 hash を開示へ転記しない。
`start` 以外では各工程が指定する state を使い、`take` 以外には `--expect-state-sha256 <保持値>` も渡す。
全入口は内部で `diff-snapshot.sh --precheck` を実行し、0 以外なら内容を読む前に停止する。
対話で承認済みの疑いがある場合だけ `--accept <承認ダイジェスト>` を追加する。
承認 filter は precheck の NUL トークンを取り込み無効化し、promisor の遅延取得を止める。無人では承認しない。

state・入力の控え・結果の置き場は、事前に `reviews-dir.sh ensure` を通した `.claude/reviews/` 内の未使用パスにする。
すでに在る出力、FIFO、symlink、通常ファイルでない入力を受け付けない。各親要素も dirfd と O_NOFOLLOW で固定し、親の差替えを検査する。
通常ファイルは初期サイズを超えて読まず、1 件 64 MiB・読取ループ 15 秒を上限にする。超過は成功扱いにしない。state は commit に含めない。
各コマンドの出力 hash を**その実行直後にセッションへ保持**する。次工程で state ファイルから取り直してはならない。
保持値を失った場合は検証・review を再実行する。自己 hash だけで採用しない。
終了値は 0=成功、1=不一致または品質検査失敗、2=実行不能・不正入力。0 以外で後続の commit・公開へ進まない。

### 実装の経路

1. 実装開始前の Phase 0 で `start --task-md <対象 MD> [--quality-path <品質入口>]` を実行し、`START_SHA256` と開始 state を保持する。
   task_dir は対象 MD の親で固定。`候補_`・`進行中_`・`完了_`・`保留_`・`中断_` の全 MD を守る。
   対象以外の追加・削除・改名・変更を許さない。発見モードの候補追加は別契約のまま。
   開始前の未追跡、ignore 規則、ignored 未追跡、品質入口も控える。
2. 実装後、snapshot を作る**前**に、新規 state へ `take --task-md <対象 MD> --start-state <開始 state> --expect-start-sha256 <START_SHA256> --phase implementation` を実行する。
   必要な全 reviewer 名を `--reviewer <役割名>` の繰り返しで指定する(省略時は `reviewer` 1 名)。
   品質コマンド定義・入口を動的に解決し、標準の静的一覧に無い入口を `--quality-path <パス>` で補う。
   `STATE_SHA256`・`TARGET_SHA256`・`DISCLOSURES` を保持する。
3. 既存の diff snapshot 手順で snapshot と patch を作る。`seal --expect-state-sha256 <take の値> --review-input <snapshot> --review-input <patch>` を実行する。
   外部 reviewer 用に別の実入力を作る場合も、それを `--review-input` に追加する。
   生成の前後と seal の収集中に対象が変われば停止する。**seal が返した新しい `STATE_SHA256`** に更新し、
   `TARGET_SHA256`・`REVIEW_BINDING_SHA256` を保持する。reviewer が読むのはここで hash した実入力だけ。
4. `verify --mode pre-stage` を通し、snapshot・patch、対象 hash・binding、`DISCLOSURES` 全体を reviewer へ渡す。
   変更された品質定義・入口・test/selftest/検証器は、常時成功・skip の追加で検査を弱めていないかも確認させる。
   静的一覧は一般的なコマンド定義・テスト名・scripts 等と明示入口の和。動的 import や取得先の全依存を保証しない。
5. 検証担当は解決した必須品質コマンド全件を JSON の `commands` に `{ "name": "検査名", "argv": ["実行ファイル", "引数"] }` として保存し、外部 hash を保持する。
   `run-checks --checks-file <計画 JSON> --expect-checks-sha256 <計画の保持値> --out <結果 JSON>` を実行する。
   実際の終了値・stdout/stderr hash・コマンド列・binding を結果に固定し、`CHECKS_SHA256` を保持する。
   失敗を常時成功コマンドへ差し替えて通してはならない。必須 gate の選択は profile→動的検出→権威参照ファイルの責務。
6. reviewer の直接返答から、役割名・依頼時 binding・返答の `verdict`・`issues` を次の JSON に控え、その raw hash を保持する。
   タスクの記録に書かれた APPROVED を取り込まない。外部 reviewer の既存返答形式は変えず、検証担当が既知の役割名と依頼 binding を添える。

   ```json
   {"reviewer":"reviewer","review_binding_sha256":"依頼時の64桁hash","verdict":"APPROVED","issues":[]}
   ```

7. `attest --checks <結果 JSON> --expect-checks-sha256 <CHECKS_SHA256> --review-result <返答 JSON> --expect-review-sha256 <返答の保持値> --out <根拠 JSON>` を実行する。
   reviewer が複数なら返答と hash を同じ順序で繰り返す。必要な全役割が APPROVED、品質実行が全成功、binding が同じでなければ停止する。
   返る `EVIDENCE_SHA256` を保持する。根拠には ignore・品質の開示一覧も含む。
8. task を完了にするときは `finalize --evidence <根拠 JSON> --expect-evidence-sha256 <EVIDENCE_SHA256> --transition complete --out <期待 MD>` で期待バイトを作る。
   `EXPECTED_TASK_SHA256` を保持する。元 MD のモードを保ったまま、返された `TASK_PATH` に期待バイトを適用し旧パスを除く。
   元本文、既存記録、チェック状態はそのまま。許すのはヘッダのステータス 1 行と、既存の追加修正記録節への検証器記録だけ。
   本文 digest が変われば停止する。自由に書いた期待 MD を渡しても、元本文と根拠から再生成したバイトに一致しなければ拒否する。
9. stage 前は `verify --mode pre-stage`、stage 後と commit 直前は `verify --mode stage`、commit 後は `verify --mode commit` を実行する。
   stage/commit には `--evidence <根拠 JSON> --expect-evidence-sha256 <保持値>` が必須。
   改名済みなら、3 mode 全てに `--task-transition complete --expected-task <期待 MD> --expect-task-sha256 <保持値>` と同じ根拠引数を追加する。
   stage は対象集合だけを載せる。開始前の無関係な未追跡を取り込まない。task 改名なしの source commit も同じ mode で照合する。
   commit は seal 時 HEAD を単一の親とする 1 commit。内部で index の集合・mode・blob と commit tree を比較し、表示を削って判定を変えることはできない。

### ignore と clean checkout

ignored の通常生成物は path・種別・mode だけを控え、内容を読んで hash しない。大きな依存ディレクトリも内容 hash の対象にしない。
開始時と take 時に全 `.gitignore`・`.gitattributes`、実 git-dir の `info/exclude`、有効な `core.excludesFile`
(未指定なら XDG/HOME に基づく既定 global ignore)を控える。各規則の変更と新たな ignored path を `DISCLOSURES` に載せる。
機密 path は内容・内容 hash を読まない。規則や品質入口自身が機密除外されて照合できない場合は停止する。
既存の機密 index/tree は内容を取得せず OID を内部比較に使い、レビューや PR に値を出さない。

`run-checks` はレビュー対象の期待 tree から独立した一時リポジトリを作り、そこで計画の argv を順に実行する。
機密・既存未追跡・ignored 生成物・元の git 設定を持ち込まない。元の作業ツリーと index は変更しない。
正当な ignore 追加や build 生成物は、その開示を review し、この clean checkout で gate が通れば許す。
依存の準備が必要ならそのコマンドも計画に含める。復元不能な依存は成功に読み替えない。
品質コマンド自体を隔離する sandbox ではない。検証担当が確認したコマンドを使い、元 worktree の絶対パスや外部の生成物への依存を避ける。

### 品質コマンドの停止とコピーの保持

`run-checks` は、起動した処理の停止を確認してから次のコマンドへ進む。
検証用コピーを自動で削除するのも、停止を確認した後だけ。OS ごとの確認範囲は [必要環境](runtime-requirements.md#品質コマンドの監督)を参照する。

- 通常終了では、終了値が 0 でも非 0 でも、残る子孫を確認範囲に従って片付ける。終了値と標準出力・標準エラーの hash を記録し、次へ進む。
- 時間切れでは、停止を確認した後に `error=timeout` を記録する。その計画は打ち切り、終了値 1 を返す。
- 親の検証処理が `TERM`・`INT`・`HUP` の終了通知を受けると、監督へ停止を伝える。回収できた場合もコピーと診断を残し、後続を起動せず終了値 2 で止まる。
- 監督の異常終了、回収結果の欠落・不正、停止確認の期限超過でも、コピーと診断を保持する。保持先と理由を表示し、後続を起動せず終了値 2 で止まる。成功の検証結果は出さない。
- コマンドを起動できなかった場合は、起動済みの対象が無いと確認できたときだけ片付ける。成功には読み替えない。

`--timeout` はコマンドごとの秒数で、既定は 600。有限の正の整数だけを受け付ける。
標準出力・標準エラーは一時ファイルで受ける。停止後に長さを固定して hash を取る。
検証結果の `recovery_method`・`recovery_scope` に、回収方式と確認範囲を残す。
既存の成功記録を検査する `attest` の条件は変えない。

コピーを保持して止まったら、表示された場所と診断で残る処理を人が確認する。
その検証が起動した処理を終了し、安全を確認してから保持先を削除する。
検証は最初からやり直す。停止不明を成功扱いにしたり、残る処理が動くまま同じコピーで続行したりしない。

### 文書、保留、公開

- 文書は**実装 commit 後・update-doc の変更前**に、完了 task を使って別の `start` を取る。
  `take --phase doc`→snapshot→seal→review→run-checks→attest を新しい state で行う。
  task を改名せず、同じ `verify --mode pre-stage|stage|commit` で doc の単一 commit を検証する。
  実装 state を doc stage に流用しない。doc 変更が無ければ新しい commit は作らない。
- review 前に保留が必要になった場合も、元の開始 state から `take --phase hold` で未承認保存集合を作る。
  この phase は seal・APPROVED への昇格を許さない。`attest --hold-reason <対話点番号で始まる理由> --hold-next <人が次にすること> --out <根拠 JSON>` は `UNAPPROVED` を返す。
  finalize は template と同じ保留の行を 1 行だけ足し、UNAPPROVED と根拠を含める。
  `finalize --transition pending`、全 verify に `--task-transition pending` と期待バイト・根拠の保持値を渡し、同じ stage/commit 検査を通す。
  保留時に品質結果があれば attest に添付できるが、成功・承認とは表示しない。開始控えが無い場合は保存を成功扱いにしない。
- push 直前と PR 作成直前は最後の commit の state で `verify --mode commit` を打ち直す。
  `pr-evidence` に同じ state・根拠・必要なら task 遷移引数を渡し、その stdout を検証欄に使う。`--mode commit` 以外は拒否する。
  実装の根拠欄は実装 commit 検査直後に生成して hash を保持し、doc の根拠欄と並べる。最終 doc の start HEAD は検証済み実装 HEAD でなければならない。
  PR には実コマンド・終了値・review 返答・binding・ignore/品質開示を載せる。task 記録から検証・APPROVED を復元しない。
  実動確認の事実は検証担当が直接得て保持した結果だけを添え、実施不能は理由を示す。

### 管理ルートと source の root が別の場合

parent-child の対話では、source の Git root を `--cwd`、管理ルートを `--task-root`、管理側の対象 MD を `--task-md` に渡す。
`start` で task-root も固定する。以降省略時は保持した root を使い、別の指定への変更は拒否する。
task_dir は管理側で照合し、source の index/tree には管理側 MD を無理に入れない。
親側に Git が無くても task_dir の内容を照合できる。機密 ERE は各 root からの相対 path に当てる。

親側も commit する場合は、source commit と task の正規最終化を検証した後、親 Git root に別 state の start を取る。
完了済み task を入力にして親側の差分(最終 task と gitlink 等)を別 review し、task 遷移なしで親 commit を照合する。
同じ task に二つの異なる最終記録を生成しない。根拠は source と親の両方を PR に載せる。
無人の parent-child 拒否は既存契約のまま。

### task 本文が外部にある場合

既定は task MD。権威参照ファイルが Issue 等を正本にする場合に限り、trusted caller が本文を fresh に取得してレビュー用の控えへ保存する。
`start` の `--task-md` を `--task-body <控え> --expect-task-body-sha256 <直接保持値> --task-id <識別子> --task-dir <保護するディレクトリ>` に置き換える。
以降の**全入口**にも fresh な控えと `--expect-task-body-sha256` を渡す。helper は外部サービスへの問い合わせを行わない。
caller は取得成功・対象 ID・本文 digest を確認し、外部本文の古いキャッシュを fresh な取得として使わない。

控えは task 成果物でなく review 入力。task_dir の他の状態名 MD を保護し、ローカルの task MD を新設・改名しない。
`finalize` が生成する期待バイト・hash は同じ契約で、caller が外部本文を読み直して元の保持値と比較してから適用する。
適用後は外部本文を再取得し、新しい控えで `verify --task-transition complete`(保留なら pending)を行う。
改名が無くても元本文と許可記録の制約を省略しない。外部本文中の偽 APPROVED は review 返答に使わない。
文書は実装の検証済み外部本文で別 start を取り、改名なしの doc 経路を使う。

### 保証する範囲

保持 hash を持つ検証担当と、書換可能なリポジトリ・ログを分ける運用が前提。同じ UID の完全隔離は主張しない。
攻撃者が検証担当の文脈・実行コード・直接結果の保持値まで変更できれば偽の根拠を作れる。
ファイルを検査間だけ変えて戻す transient な競合も完全には排除できない。永続した変更・収集中の差替えは再読と hash 比較で検出する。
実装者の停止を確認し、review 前・stage 前後・commit 後・公開直前に照合する。旧実装の seal 第2観測、自由な期待 MD、
既存未追跡の混入、doc の通常 commit、ignore の後付け隠蔽は scratch 回帰で攻撃と正常対照を検証する。

## reviewer の起動

### 1 体構成(既定)

実装に関与していない reviewer(変更を確かめる AI。読み取り専用)を新規に起動する(`name` は `reviewer`)。**③ 外部ランナー(レビューや実装に使う外部の AI のコマンド)が宣言されている場合はこの 1 体も `reviewer-internal` を名乗る**(枡名の写像は [delegation-map.md](delegation-map.md) §2 (a)。下の「外部ランナー」節の「`reviewer-internal` は常に維持」がこの枠を指す)。渡すもの:

- diff(`.claude/reviews/diff-{TASK_NAME}-iter{ITER}.md`。**内蔵 reviewer はこのパスのまま読む**。**外部ランナー(③)にはこのパスを渡さない** —— この置き場は `.gitignore` の対象で、一時ツリー(外部のレビュアーに見せるために作る、ファイル一式の一時的な写し。HEAD + パッチ)に運ばれず外部レビュアーからは読めない(実測)。**外部ランナーには、external-runners.md §9-1 の手順 1 が同じ入力で一時ツリーの中に生成する `.review-snapshot.md` と `.review-diff.patch` を `--target` で渡す**(依頼文でもこの 2 つのファイル名で指す。一時ツリーに無いパスを書くと、外部レビュアーは読めずに推測でレビューすることになる)。基準コミット〈作業を始めた時点のコミット〉からの追跡差分 + index + 途中 commit + 未追跡の新規ファイルを含む。生成は do-task Phase 4 の手順 1 の `scripts/diff-snapshot.sh`。対象が git リポジトリでないときは diff の代わりに『基準なし・非 git』の旨と対象ファイル表のパスを渡し、レビュアーは実ファイルを読む)とタスク MD のパス
- レビュー観点(下記 6 カテゴリ)
- 人が読む文書(`doc/`・README・タスク MD)が diff にあるときは、[writing-for-people.md](writing-for-people.md) の 2 節(わかりやすさの決まり)の 8 項目の要点を依頼文に入れる(外部のレビュアーはプラグインのファイルを開けないため)
- 返答形式: `APPROVED` または指摘リスト JSON
- 無人(`--unattended`)では、[unattended-mode.md](../../ship-task/references/unattended-mode.md) の「委託するサブエージェント」の項の要点を、要約し直さずにそのまま委託(作業を別の AI に任せること)プロンプトに入れる(「拒否されたら打ち直さずに報告する」は要点の最後の行。3 体構成でも、checker〈設計書を点検する AI〉などほかの委託でも同じ)

### 3 体構成(--reviewers=3 / 高リスクタスク)

**単一メッセージで 3 体を並列起動**する(逐次起動しない)。各委託の `name` には下表の reviewer 名をそのまま付ける(フル段階では再レビュー・STATUS 問い合わせの宛先になる):

| reviewer | 狙い |
|---|---|
| reviewer-internal | プロジェクト規約・実装整合の視点 |
| reviewer-strong | 深い論理・設計面の見落とし |
| reviewer-alt | 別モデル視点による多様性 |

**モデルの選定・打ち切り・ベンダー多様性の扱いは [delegation-map.md](delegation-map.md) 軸 3・軸 10・§2(a) に従う。** 既定(外部ランナーの宣言が無いとき)はホスト内蔵のモデルのみで編成し、外部 CLI・他ベンダーのモデルを探しに行かない。**外部ランナーが宣言されている場合に限り**(`--runners` または profile の `features.runners`)、ベンダー横断の多様性(各社の最上位級)を同一ベンダー内の能力帯差より優先する(下の「外部ランナー」節)。宣言があっても利用不可のとき(未導入・認証切れ・レート制限・応答なし・読み取り専用未確立)は内蔵のみの編成に切り替え、切り替えた事実と理由を報告に明記する。**モデルを明示指定する枠には「読み取り専用。ファイルを編集しない」を指示で担保する**(軸 1)。**レビュアー同士を会話させない**(独立性が失われると多様性が意味を失う。指摘の突合〈2 つを照らし合わせて食い違いを探すこと〉は team-lead〈作業を進め、結果を確かめる側の AI〉が行う)。

**委託の解決の正本(ほかが合わせる元)は [delegation-map.md](delegation-map.md)**(役割語の一覧・派生名の体系・能力帯 → エイリアスの指定方法・体数 → 枡名の割り当て・並列起動の手段・解決順・ホストでの解決・縮退)。**本書が持つのは枡ごとの狙い(観点の割り当て)と do-task Phase 6 の起動手順だけ**(③ 宣言時の置換可否・内蔵の維持範囲は [external-runners.md](external-runners.md) §8)。定義を本書に増やさず、変更は解決表(役割の名前を、実際に使う AI の仕組みに対応づける表)を先に直して本書を追随させる。

### 外部ランナー(宣言時のみ・オプトイン)

`--runners=<名前,...>` 引数または profile の `features.runners` が**宣言されている場合に限り**、外部 CLI をレビュアーとして追加する。宣言が無ければこの節は一切実行しない(既定の編成は上のとおり)。

**契約の正本は [external-runners.md](external-runners.md)**(判定順序・終了コード・既定ランナー表・読み取り専用の保証範囲・ログ規約・機密ガードの手順)。ここでは再掲せず、レビュー編成として押さえる点だけ挙げる:

- **内蔵レビュアーを全滅させない**。`reviewer-internal` は常に維持し、外部ランナーは `reviewer-alt` 枡の置換または追加枡として足す(枡ごとの扱いは external-runners.md §8)。`--reviewers=1` + ランナー宣言 = 「内蔵 1 + 外部 N」
- **並列性**: 外部ランナーは Bash 同期実行になるため、**内蔵の委託と同一ターンに走らせるには**、ホストの背景実行の手段で起動する(手段は external-runners.md §8「並列性」)(「単一メッセージで並列スポーン」を維持する)。既定のタイムアウトのままでは Bash ツールの既定・上限をどちらも超えうる(見積もりは external-runners.md §8)
- **反復では全レビュアーを再確認する**。外部ランナーは**完了後の再依頼(軸 5)**ができず毎回新規起動になるため、valid 修正後は新規起動して APPROVED を得る
- 出力は下の「指摘 JSON 形式」へ正規化されて返るので、team-lead のトリアージ(指摘を扱いごとに振り分けること)はそのまま使う。記録は `.claude/reviews/` に残る(形式は external-runners.md §7)
- **出力形式の指示は review-agent.sh が付与する**(プロンプト末尾に固定ブロックを 1 回。呼び出し側はプロンプトファイルに出力形式の指示を付けない。文面は下の「指摘 JSON 形式」とその「制約:」行の写しで、同期義務は external-runners.md §6)
- 宣言されたのに使えないときは**エラーとして報告**し(サイレント縮退禁止)、内蔵編成に切り替えて続行する(`review-agent.sh` が置き場の理由の exit 2〈stderr の `ERROR [usage] ` の行に `既定のログ置き場` か `--log-file の置き場`〉で止まったときは内蔵に切り替えず、停止して報告する — [external-runners.md](external-runners.md) §10)

### 修正後の再レビュー(フル段階)

**完了後の再依頼(軸 5)が使える環境**では、修正後の再確認を**同じ reviewer 名へ再依頼**する(「指摘 #1〜#3 を修正した。新しい diff は {パス}。解消しているか判定して」)。前回のレビュー文脈が保たれるため差分だけを見ればよく、再スポーン(diff とタスク MD の読み直しから始まる)より速く判定がぶれない。宛先が失われている場合のみ新規起動にフォールバック(先の手段が使えないときに次の手段へ切り替えること)する。

**外部ランナーは完了後の再依頼ができない**(軸 5 の宛先にならない)ため、再レビューは毎回新規起動になる。valid 修正後は外部ランナーも再起動し、起動した全レビュアーの APPROVED を得る。

## レビュー観点(6 カテゴリ)

1. **機能保全**: タスク対象外の既存挙動を壊していないか(diff の巻き込み変更)
2. **契約整合**: 公開 API・型・スキーマの変更が呼び出し元まで一貫しているか。スコープ外との境界で「受理するが処理されない」がないか
3. **タスク充足**: タスク MD の各項目が実装で満たされているか(チェックボックスとの照合)
4. **テスト妥当性**: テストが実装に追従しているか。アサーションを弱めて通していないか(expect 削除・skip 追加は要注意)
5. **規約**: プロジェクトのコード規約(フォーマッタ設定・命名・ディレクトリ配置)に沿うか。diff に含まれる人が読む文書(`doc/`・README・タスク MD)は、依頼文に渡した書き方の要点(`writing-for-people.md` の 2 節の 8 項目)に沿うか
6. **セキュリティ / 機密**: 機密値のハードコード・ログ出力、入力検証の欠落、権限チェックの欠落

## 指摘 JSON 形式

```json
{
  "verdict": "CHANGES_REQUESTED",
  "issues": [
    {
      "file": "src/...",
      "line": 42,
      "category": "契約整合",
      "severity": "blocker | major | minor",
      "description": "何が問題か(具体的に)",
      "suggestion": "どう直すか"
    }
  ]
}
```

制約: verdict は APPROVED / CHANGES_REQUESTED のいずれか。severity は blocker / major / minor のいずれか。category は「機能保全」「契約整合」「タスク充足」「テスト妥当性」「規約」「セキュリティ / 機密」の 6 つのいずれか。

## team-lead トリアージ(必須)

reviewer の指摘を**盲信して自動反映しない**。各指摘を実コードで裏取り(実際のコードやファイルを読んで確かめること)して分類する:

- **valid**: 実コードで問題を確認できた → implementer(実装を受け持つ AI)への差し戻し(相手に返して、やり直してもらうこと)に含める
- **invalid**(false positive): 実コードでは問題ない → 理由を記録して却下(`.claude/reviews/triage-{TASK_NAME}-iter{ITER}.md`。書く直前に `bash {do-task の}scripts/reviews-dir.sh ensure --root <管理ルート>` を打ち、exit 0 以外なら記録を書かずに停止して報告する)
- **needs-user**: 仕様判断が必要 → ユーザーに確認(無人〈`--unattended`〉では保留 — [../../ship-task/references/unattended-mode.md](../../ship-task/references/unattended-mode.md) の D13)

minor のみが残った場合の扱い: 過剰修正で新たな問題を作るリスクと天秤にかけ、修正せず「残課題」として報告する選択を許す(その判断を記録する)。

## 差し戻しテンプレ(implementer への再委託)

**外部 implementer(③ 宣言時)は完了後の再依頼ができない**(軸 5 の宛先にならない。[delegation-map.md](delegation-map.md) 軸 5)ため、差し戻しは毎回新規起動になる(検証の仕組み自体〈2026-09-17 決定 7〉は変わらない)。内蔵 implementer への差し戻しは同じ委託先への再依頼、外部 implementer への差し戻しは新規起動時のプロンプトとして、いずれも次の内容を渡す:

```
前回の実装(iter{N})に対して以下の valid 指摘があった。修正してほしい。

## 修正対象(valid 指摘)
1. {file}:{line} [{severity}] {description} → {suggestion}

## 制約(再掲)
- タスク MD のスコープを変えない(縮小・拡大とも)
- 修正済みチェックボックスを勝手に外さない
- 品質ゲート({コマンド列})を実行して結果を報告する

## 前回からの引き継ぎ
- 変更済みファイル: {一覧}
- 触ってはいけない箇所: {invalid 指摘で確認済みの正当な実装}
```

## 反復の終了条件とセーフティ

- **収束条件は全 reviewer の APPROVED**。そこに至るまで Phase 3〜6 を反復し、**コストを理由にレビュー反復を打ち切らない**(design §5-10)
- 以下は**停止点ではなく判断を仰ぐ点**であり、到達しても自動では止めず、状況を報告してユーザーの判断を仰ぐ(無人では保留 — unattended-mode.md の D2):
  - 同一指摘が 2 iteration 連続で残存 → 自動修正が困難と判断し、ユーザーに状況と選択肢(手動対応 / スコープ調整 / 続行)を確認
  - iteration 5 回超え(--max-iter 既定)到達 → 残る指摘・反復回数・想定トークンコストを報告して継続方針を確認
- 反復のたびに ITER をインクリメントし、全記録を `.claude/reviews/` に残す(事後の再検証で追跡可能にする。在れば ITER の採番〈do-task Phase 0 の手順 5。再開かどうかに関わらない〉に使い、中断後の再開では最後のレビュー状態の確認にも使う。再開するかどうかの判定はタスク MD で行う — do-task Phase 0 の手順 7)

## 死活監視 M1〜M4(長時間サブエージェントの停止対策)

委託したサブエージェントから長時間(目安: implementer 15 分 / **reviewer 20 分**)応答がない場合:

> **reviewer の目安の根拠**: 外部ランナーの既定の**最大所要時間**はプローブ 60 秒 + 本実行 600 秒 + ヘルプ照合(20 秒 × 最大 2 回)+ 終了猶予で、**目安がそれより短いと M2 が正常な実行を二重起動する**。目安は常に**既定の最大所要時間より長く**保つ(既定値は `review-agent.sh` の `PROBE_TIMEOUT` / `RUN_TIMEOUT` / `HELP_TIMEOUT` / `KILL_GRACE`)。

- **M1 状態確認**(フル段階のみ): **生存確認(軸 7)**を行い、**走行中の問い合わせ(軸 6)**で `STATUS を 1〜2 行で返答して` と尋ねる。**標準・最小段階では M1 を省略し、外部 implementer(③ 宣言時)以外は待機目安を超えたら M2 へ直行する**(内蔵 implementer は、中止を伝える手段〈軸 6〉が無く走っているものを止められないので、M2 から M4 へ進む)。**外部 implementer には軸 6 の送達手段が無く問い合わせ自体ができないが、待機目安の経過だけでは M2(新規起動)へ進まない**([delegation-map.md](delegation-map.md) 軸 6。段階を下げて続けるときの正本は design §5-17)
- **M2 個別再委託**: 応答がなければ、役割ごとに次のとおり引き継ぐ。引き継ぎプロンプトには前回までの成果(diff・レビュー記録のパス)を含め、ITER はリセットしない
  - **内蔵 implementer**(編集を伴う): 旧 implementer が**走行中でないと確かめてから**引き継ぐ(対話では、再依頼の前に do-task の対話の本文の照合を通す。宛先喪失で新規に起動するときも、起動の前に同じ照合〈照らし合わせて確かめること〉を通す)
    - フル段階: 走行中の問い合わせの経路(軸 6)で中止を伝え、生存確認(軸 7)で走行中として出ない(完了として出る)ことを確かめてから、**同じ `name` へ再依頼(軸 5)して引き継ぐ**。宛先が失われている(軸 7 に無く、軸 6 の送達が失敗する)ときだけ新規に起動する(design §5-17 のフル段階と同じ条件)。派生名は足さない([delegation-map.md](delegation-map.md) §2 がサフィックスの系統を限るため)
    - 走行中として出る・走行中かを**判別できない**(一覧の鮮度は保証されない — [delegation-map.md](delegation-map.md) §7(軸 7 の行))とき、または標準・最小段階(中止を伝える軸 6 が無い)では、**引き継がずに M4 へ進む**(無人では失敗扱い — unattended-mode.md の D14)
    - 旧 implementer の報告が後から届いたら、黙って捨てずに扱い(採るか捨てるかと理由)を報告する
    - 理由: 応答が遅いだけの旧 implementer と新しい implementer が、同じ作業ツリーへ同時に書き込むのを避けるため(外部 implementer の除外と同じ理由)
  - **外部 implementer**(③ 宣言時): **外部 implementer は待機目安を超えても新規起動せず、終了・タイムアウトで停止した後に契約の比較と引き継ぎ条件の判定を経て内蔵が引き継ぐ**(走行中の外部プロセスと新規起動が同じ作業ツリーへ同時に書き込むのを避けるため。条件は [external-runners.md](external-runners.md) §12-3・§12-7、段階を下げて続けるときの正本は design §5-17)
  - **reviewer・checker・調べものを受け持つ AI(researcher)**(読み取りのみ): 失われたものとみなして新規に起動する(二重に起動しても作業ツリーは壊れない)。design §5-17 のフル段階の「宛先喪失のときだけ再スポーン」の例外にあたる
- **M3 環境調査**: 再委託も失敗する場合、git 状態・ロックファイル・依存の破損を確認する(環境要因の切り分け)
- **M4 エスカレーション**: M1〜M3 で回復しない場合、状況・試したこと・選択肢を整理してユーザーに報告する(無人では失敗扱い — unattended-mode.md の D14)

各リカバリは `.claude/reviews/recovery-{TASK_NAME}.md` に記録する(何が起き、どう回復したか)。書く直前に `bash {do-task の}scripts/reviews-dir.sh ensure --root <管理ルート>` を打ち、exit 0 以外なら記録を書かずに停止して報告する。
