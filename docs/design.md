# dev-workflow 設計書

このリポジトリの skills 集がどう設計されているか、skill を追加・改修するときに必ず守る規約は何かを定義する。複数の実プロジェクトで実証されたベスト要素を統合したものが本 skills 集である。

## 1. 目的

- どのプロジェクトでも(技術スタック・リポジトリ構成を問わず)使える汎用ワークフロー skills を 1 か所で管理する
- 新プロジェクト開始時に `/plugin marketplace add` 一発で全 skills を導入できるようにする
- 各プロジェクトで育った改善をここに還元し、全プロジェクトへ配布する

do-task / ship-task の新しい開始では、対象タスク本文の採用と対象 Git 操作より前に、外部実装の保護記録らしい候補を有限に列挙する。記録の外の所属の印(take が書く、パスと番号だけのファイル)で、別のプロジェクトの記録と確かめられた候補では止めない。それ以外の候補があれば停止し、全件を人が具体的に確認した同じ一覧だけで続行する(全件が無関係なら実体の確認値、関連する記録があるか重なりのある候補があれば一覧の確認値で照合する。#291)。Python 3.10 以上と必要な POSIX 機能が無い場合も停止する。詳細の唯一の正本は [external-runners.md](../plugins/dev-workflow/skills/do-task/references/external-runners.md) §12-9。候補内部・保持hashを読み直さず、比較・復元・削除・プロセス停止を自動実行しない。印の真正性や子孫の停止を証明せず、候補なし・別プロジェクトの記録だけ・一覧一致も安全証明ではない。

内蔵 implementer が Git の状態を読むときは、do-task の専用読取り入口で、承認済み filter の無効化設定・物理 root・承認値を束ねて渡す。入口は5種類の読取りだけを扱い、毎回の事前検査、無効化の実効値、委託元から渡された照合値を確認する。品質コマンド内の Git、利用者環境全体、未停止プロセス、外部実装の比較不能からの復帰はこの責務に含めない。

## 2. コアサイクルと skills 責務マップ

```
/init-project(初回のみ: 標準構成を生成)──チェーン提案──→ /stack-research
      │                                    (依存バージョン固有の調査 → doc/06。
      ▼                                     実在する問題はタスク化をチェーン提案──→ /create-task)
/understand-project ──→ /create-task ──→ /do-task ──→ /update-doc --task ──→ PR
 (把握。grasp は参照索引、  (種別判定+設計書生成。   (implementer 委託+機械検証   (完了タスク駆動の差分同期。
  毎セッション hook が促し)   --refactor = リファクタ分析)  +実動確認+独立レビュー)     タグ昇格・ADR・図・索引)
                             └──────────────── /ship-task(一気通貫)────────────────┘
                              薄いオーケストレーター。上の 3 工程を順に実行し、作業ブランチ作成・
                              実装 commit・doc commit・push・PR 作成まで出す。既定は走り切り、
                              停止条件(要件不明の残存・品質ゲート赤・スコープ縮小・レビュー未収束・
                              実動確認の結果待ち など)に当たったときだけ停止する

品質系(任意の時点): /tool-check = ツールによる機械検査(チェック 3 階層の 1+2。第 3 層の実動確認は do-task)
                    /data-audit = データ境界監査(機密露出・認可欠如・過剰取得を 3 層で検査、裏取り済み指摘を提案)
                    (承認された指摘はタスク化をチェーン提案──→ /create-task)
入力系(任意の時点): /discuss-spec = テーマ単位の壁打ちで仕様を決め、決定録を生成(doc は書かない)──チェーン──→
                    /reflect-decisions = 原資料(議事録・文字起こし・チャットログ・資料・壁打ち決定録)から
                    決定事項を抽出 → 精査(裏取り・レビュー・確認)→ doc/(01/03/04/05/07)へ出典付き反映
                    (update-doc が「実コード→doc」なのに対しこちらは「人の決定→doc」。
                     実装が必要な決定・宿題は /create-task へチェーン提案)
整理系(任意の時点): /cleanup = 作業の残り(worktree・作業用コンテナ・作業ファイル)を読み取りで棚卸しし、
                    分類 → 承認 → `git worktree remove` で片付ける(方針は §5-26)
出力系(任意の時点): /export-doc = doc/ をクライアント提出用に PDF / xlsx / HTML へ変換
                    (サニタイズ確認+機密検査。doc/ は不変。変換ツールは動的検出し、無ければ代わりの手段に切り替える)
無人運用(任意・既定オフ): loop.sh = 人のシェル / cron から起動する同梱スクリプト(skill ではない)。
                    明示した 進行中_ を 1 イテレーション = 新しいヘッドレスセッションで /ship-task(無人モード)に通す。
                    発見モード(--discover)は、発見元(/data-audit・/create-task --refactor の分析)の候補モードを
                    イテレーションごとに 1 つずつ回し、候補_ だけを PR で届ける(採用は人が /create-task <候補_> に通す)
```

| skill | 責務 | 読む | 書く |
|---|---|---|---|
| init-project | 標準構成(権威参照ファイル(AGENTS.md)/ doc/ / task_dir / profile)+ MCP セットアップ(opt)の生成 | 対象プロジェクト全体 | AGENTS.md, doc/, task_dir, .claude/, .mcp.json, .codex/config.toml, `.gitignore`(既存に無い行の追記), `README.md`(新規プロジェクトで無いときだけ)(`CLAUDE.md` は生成しない。既存の `CLAUDE.md` の扱いは §7-4) |
| understand-project | プロジェクト把握。読み取り専用(grasp は参照索引) | profile, 権威参照ファイル, メモリ / doc, コード | .claude/grasp.md(前回要約・参照索引のみ) |
| create-task | 種別判定・影響範囲調査済みのタスク設計書生成(--refactor = 対象発見型リファクタ分析。`--refactor --candidates` = 無人でも回る候補モード。`候補_` のパスを渡すと採用の経路) | コード全域 | 解決した task_dir の進行中_*.md のみ(候補モードでは新しい `候補_*.md` だけ。採用では `候補_` → `進行中_` の `git mv`〈stage だけ〉と書き直し) |
| ship-task | create-task → do-task → update-doc --task を通しで実行し、ブランチ・commit・push・PR まで出す薄いオーケストレーター(工程の中身は持たない)。`--discover` = 発見元の候補モードを回して `候補_` を PR で届ける無人専用の入口 | 各工程の成果物 | 3 工程の書き込み+ git ブランチ / commit / PR(`--discover` では `候補_` だけの 1 commit と PR) |
| do-task | タスク実装(委託)〜機械検証・実動確認〜独立レビュー〜完了処理。検証のみモード可 | task MD, コード | ソースコード, task MD |
| update-doc | メモリ / 権威参照ファイル / doc/ の実コード同期(--task = 完了タスク駆動。タグ昇格・ADR・索引) | コード全域, 完了タスク MD | メモリ, 権威参照ファイル, doc/(索引含む) |
| tool-check | ツールによる機械検査(format/lint/typecheck/test/build)一括実行 | package.json 等 | 自動修正のみ |
| stack-research | 依存の実バージョンに固有の注意点・ベストプラクティス・脆弱性の Web 調査とノート生成 | マニフェスト, lockfile, Web | doc/06_stack-notes.md, doc/README.md |
| data-audit | サーバー境界を越えるデータの監査(機密露出・認可欠如・IDOR・過剰取得・DB 防御)。読み取り専用で裏取り済み指摘を提案し、承認分を /create-task へチェーン(`--candidates` = 承認を待たずに `候補_` を書く候補モード) | コード全域, スキーマ, profile, doc/06 | .claude/reviews/(レポートのみ。候補モードでは task_dir の新しい `候補_*.md` も) |
| reflect-decisions | 議事録・文字起こし・チャットログ等の原資料から決定事項を抽出し、精査(原文裏取り・レビュー・ユーザー確認)を経て doc/ へ出典付きで反映(決定と未決をはっきり分ける・矛盾は対比確認)。未決・宿題はタスク化候補として報告 | 原資料, doc/, profile | doc/ 01・03〜05・07(02・06 は対象外), doc/README.md(索引) |
| discuss-spec | テーマ単位の壁打ちでユーザーと仕様を決める(論点分解 → 選択肢・推奨提示 → 1 論点ずつ合意)。結論を決定 / 暫定 / 未決 / 却下にはっきり分けた決定録を生成し /reflect-decisions へチェーン | doc/, 実装コード, profile | 決定録(doc/minutes/ 等)のみ。doc/ は書かない |
| export-doc | doc/ をクライアント提出用に PDF / xlsx / HTML へ変換。サニタイズ確認(地雷・管理タグ・内部記述)+機密検査+ Mermaid 画像化。ツールは動的検出し、無ければ代わりの手段に切り替える | doc/, profile | export/(出力のみ。doc/ は不変) |
| cleanup | 作業の残り(worktree・作業用コンテナ・作業ファイル)の棚卸しと片付け。読み取りで集め、「削除してよい / 確認が必要 / 残す」に分類し、承認を得て `git worktree remove`(`--force` なし)で消す。ブランチは消さない | `git worktree list`, コンテナ一覧, プロセスの作業ディレクトリ, 作業ファイル | 承認を得た worktree・コンテナ・ボリューム・作業ファイルの削除と、`git worktree prune` のみ |

「書く」は各 skill の成果物。他の skill が工程の副産物として書く `.claude/` の状態ファイル(create-task などによる把握キャッシュ `grasp.md` の更新、update-doc などによる削除、`.claude/reviews/` の記録)は §5-18 の規律のもとで書き、この列には載せない(`grasp.md` を成果物とする understand-project と、レポートを成果物とする data-audit は載せる)

### 無人ループ(loop.sh。[決定録 2026-09-23](minutes/2026-09-23_壁打ち_ループエンジニアリング.md))

外側の while を skill にせず、同梱スクリプト `loop.sh` が回す(while を skill にすると 1 つのセッションの中で回り、「1 イテレーション = 新しい文脈」が崩れる)。skill の数と責務は変えない。ship-task の入口 `--task=<タスク MD>` と無人モード `--unattended`・完了条件の改竄ガードは #67 で実装した(無人モードの正本は [`ship-task/references/unattended-mode.md`](../plugins/dev-workflow/skills/ship-task/references/unattended-mode.md))。外側の while の `loop.sh`(ブレーカー・selftest を含む)は #68 で実装した(契約の正本は [`ship-task/references/loop.md`](../plugins/dev-workflow/skills/ship-task/references/loop.md))。発見ループ(`loop.sh --discover`・ship-task の `--discover`・発見元の候補モード)は #69 で実装した(正本は [`ship-task/references/discover-mode.md`](../plugins/dev-workflow/skills/ship-task/references/discover-mode.md) と [`create-task/references/candidate-mode.md`](../plugins/dev-workflow/skills/create-task/references/candidate-mode.md))。

- **本文ダイジェストの構造検査**: 末尾まで閉じないコードフェンスは、追加修正記録の節の内外を問わず算出不能にする。記録節に置いた未閉鎖フェンスが後続本文を除外範囲へ取り込む経路(#107 H9)を、正常なダイジェストを返す前に拒否する。
  - フェンスの外で、半角スペース 4 個以上かタブを含む字下げの直後に、同じバッククォートかチルダが 3 個以上続く行も算出不能にする([#168](https://github.com/Yuki-Maeda-valour/workflow/issues/168)、#107 H10)。閉鎖の有無にかかわらず、記録節を除外する前に検査する。対応外の字下げの内側で、空行やチェック状態の変更を見逃す経路を防ぐ。
  - 正常な半角スペース 0〜3 個の囲みの計算値と、囲みの中の字下げ文字列は保つ。既知キー収集・候補検証の既定動作も保つ。Markdown 全体の解析は行わない。フェンスの判定・除外・正規化と期待出力の書き方の正本は [task-template.md の記法の規約](../plugins/dev-workflow/skills/create-task/references/task-template.md)。
- **人の linked worktree の設定(H20)**: `loop.sh` は `--repo` の管理ディレクトリを物理パスで固定し、その下の `config.worktree` を不在も含めて控える。通常・シグナル終了・中断後の再起動で追加・削除・値変更・項目の並べ替えを検出する。`extensions.worktreeConfig` により親がそのイテレーションの新しい管理領域へ複製する設定は、保持した親と正確な管理パスに結び付くときだけ許し、親が正常に削除した当該設定だけを次のイテレーションの保持集合から外す。未知の設定・link・別 worktree は許さない。中断後の起動元変更と、比較元を持たない旧状態は停止する。中断の無い実行間の差分は従来どおり報告して続ける。利用者の設定は自動復元・削除しない。起動元変更・旧状態の扱いと保証範囲の正本は [loop.md §4・§10](../plugins/dev-workflow/skills/ship-task/references/loop.md)。設計・検証記録は [Issue #167](https://github.com/Yuki-Maeda-valour/workflow/issues/167)。
- **無人ループの状態観察(H16〜H19)**: `loop.sh` は共有の全 ref と未解決 symref、各 linked worktree の private `HEAD`/`refs/`、全既存 worktree の index・ファイル木・実効 Git 設定と include origin、共有管理領域の hooks と info の入力を、有界の no-follow 観察で比較する。Git の出力とディレクトリ名も保持前に同じ観察予算を消費する。通常ファイルは内容 hash、機密 path は本文を開かない metadata-only とし、worktree path ごとに開始時からの `secret_paths` glob と現在の glob の和を保持する。正常完走の間に人の worktree が移動したときはその Git 管理ディレクトリで同じ和を引き継ぎ、イテレーションの途中の人の worktree 追加・移動は本文観察前に止めるため、後の profile 縮小・削除で本文 hash へ戻さない。機密 symlink の作業ツリー外 target を変えた場合だけは link 自体が不変なら検出しない。この限界を隠さず、機密を別の保護領域に置く。include は実効設定の target を Git が follow する呼出しより先に保持済み graph を各 worktree 文脈で no-follow 再検査し、worktree の `.git` と admin の `commondir` を保持した共有管理パスへ照合する。全保持 origin とcommon・各 worktreeの `config.worktree`・利用者/system候補rootの不在を含む候補集合をGit無しの第1段階で完全一致させる。system の既定候補は初回に同じ Git の `git var GIT_CONFIG_SYSTEM` が返す実パスを保持し、取得不能ならイテレーションを始めず `GIT_CONFIG_SYSTEM` の実パス明示を求める。存在する候補は構造・metadata・digestを照合し不在候補の新設は本文を開かず停止する。第2段階の `--file --no-includes` parser は repository 外で利用者/system config を外して保持 fd だけを読む。`onbranch` は保持 HEAD と共通 ref の symbolic-ref 連鎖から得た最終 branch の隔離 Git dir、`hasconfig` は保持 remote URL と repository 外の marker により Git 自身に評価させ、条件評価の起動時に未検査 target を読ませない。有効な到達先の欠落・特殊ファイルは実効設定を読む前に停止し、leaf symlink は開始時に link 字面と target 実体を記録して通常構成を維持する。イテレーションの中では link 差替え・新しい include origin・保持済み origin の内容変更を target 本文より先に停止し、正常完走した別実行の新しい基準で同一 target の内容変化を hash 差分として記録する。同 inode の origin でも字面と親 directory 文脈を保持し、同じ組の再帰だけを循環として止める。無効な条件付き include の欠落は通常設定として保持する。状態 JSON の旧形式・特殊ファイル・読取不能・上限超過は補完せず停止する。人の checkout 内の停止ファイルは、開始時に保持した exact path が不在から空の通常ファイルになり、必要な新規親 directory がその leaf だけへ続く場合に限り許す。既定・利用者指定を問わず、既存 file の変更・nonempty・link・特殊型・親差替え・sibling・他 worktree の同名 path は差分のままにする。そのイテレーションだけの worktree/ref/clean index の遷移は厳密な OID と path の照合で扱い、保持した exact ref に反する発見のイテレーションは判定より前に止め、既に公開済みかを人が確認するまで新しい Git 通信や PR の雛形を出さない。detached commit は判定へ渡して失敗として残す。書き込んだ者は識別しない。イテレーションの検証済み after-state は生バイトと hash を保持し、親が実際に削除した exact own worktree の `worktrees`・`worktree_contents`・`worktree_configs`・`secret_patterns` の四集合だけを派生した期待状態と最終観察を self-ref 例外なしで厳密に比べる。最終観察まで持続した変更は新基準へ昇格せず、保持・派生・観察の hash 変化も停止する。最終観察後から atomic 昇格までの同一権限の競合は完全には検出しない。通常のイテレーションは own worktree 削除前から、最終比較・昇格・`inflight` 削除・active 状態解除までを狭い完了区間にし、TERM/HUP/INT は成功または保全済み失敗の後に配達する。既知の graph 外の利用者・host・shell 設定やリポジトリ外ファイルは観察しない。契約・上限と詳細な保証範囲は [loop.md §4](../plugins/dev-workflow/skills/ship-task/references/loop.md#4-1-回のイテレーション) を正本とする。設計・検証記録は [Issue #191](https://github.com/Yuki-Maeda-valour/workflow/issues/191)。
- **起動前の状態束縛(H16〜H19)**: `loop.sh` は最初の通常 Git・設定読取より先に、入力 repo の絶対 path、`.git`、admin/common と状態ディレクトリの対応を Git 無し・nofollow・有界に検査する。cold 検査と状態配置が安全に通った後だけ、その path の sha256 名を持つ `STATE_BASE/.repo-bind-*.json` を排他的に作り、再開では比較専用に使う。物理 TOP は別に保持し、symlink の別名は別の binding キーになる。binding の値から任意 path をたどらず、現在の `.git`/`commondir` の差替えで別の state を選ぶ操作、未知の marker と旧形式の中断情報は Git 前に exit 20 として `inflight`/`stop-mark` を保全する。拒否後の終了報告と選定 worktree の片付けも Git を再実行しない。no-follow は管理入口・配置の差替えを検出するもので、整合した state 内容の真正性は保証しない。#192 は未信頼の `inflight` を環境 bootstrap 前に拒否する。通常完走後の状態昇格を保存前に再観測しても、未信頼の `inflight` を安全に再開する保証は単体では得られず、この拒否規則との統合後にだけ成立する。`inflight` が残る拒否は、印だけを消さず、人がイテレーションのプロセス・中断記録・差分を確認して片付ける。設定の通常実行間の診断は key/value を出さず hash と構造だけを示す。正常な reinit・管理ディレクトリ移転は、旧 state を人が確認した後の明示的な binding 再作成で扱う。別 repo の binding は無関係な起動を止めない。同じ利用者権限で binding と全状態を同時に改竄・削除することは防げないため、成立条件・影響・検出範囲・運用を実測とともに示し、完全隔離を主張しない。正本は [loop.md §2・§4](../plugins/dev-workflow/skills/ship-task/references/loop.md)。
- **SSH のホスト鍵検証(H44)**: `origin-repo.py` は、ホスト名・ポートに加え、鍵検証と保存先・照合名・ローカルの接続先の検証省略を確認する。検証を外す構成と、判定に必要な情報の欠落・取得失敗では `repo: null` と理由を返す。通常の SSH・HTTPS の返却形式は維持する。判定表と、実際の鍵内容や保存成功を検査しない限界の正本は [discover-mode.md §3・§10](../plugins/dev-workflow/skills/ship-task/references/discover-mode.md)。設計・検証記録は [Issue #161](https://github.com/Yuki-Maeda-valour/workflow/issues/161)。
- **発見の push-only 縮退(H6・H39)**: 発見モードは、origin が一意で fetch/push が同じ送信先なら、PR の作成先を読めない場合も保持した完全 SHA だけを送信する。開始時の effective push URL は本文を出さず SHA-256 として保持し、helper は直前の origin・設定・URL digest を照合する。URLをargvへ再入力せず origin 名へ exact refspec を送り、送信後は同じ送信経路への dry-run で remote SHA を確かめ、push-only では gh を呼ばない。URL変更・複数URL・照合不能は停止する。通常の repo 固定 PR 経路、対話追跡、無人 no-u は保つ。手順と限界の正本は [discover-mode.md §8](../plugins/dev-workflow/skills/ship-task/references/discover-mode.md#8-commitpushpr)。
- **本文ダイジェストのパス入力**: 通常ファイル以外は、終了コード 1・stdout は空・理由は stderr で速やかに拒否する([#150](https://github.com/Yuki-Maeda-valour/workflow/issues/150)、#107 H11)。事前の種類検査だけでなく、待機しない方法で開いた実体を再検査し、同じ実体から読む。検査後の FIFO 差し替えで書き手を待つ経路を防ぐ。通常ファイルへのリンク・stdin のパイプ入力・正常入力の計算値は維持する。契約と保証範囲の正本は [task-template.md の記法の規約](../plugins/dev-workflow/skills/create-task/references/task-template.md)。
- **無人の起動引数と子孫の監督(H21・H29)**: `host-argv.py` は既定と上書きの引数を閉じた許可表で検査する。通常の model・effort・予算・回数・name・verbose を保ち、短い別名・束ね書き・未知・重複を拒否する。H28 の値消費防止もこの表で維持する。引数検査は認証・PyYAML・plugin 走査を行わない。`loop-supervisor.py` は Linux subreaper で孤児を引き取り、親 PID・開始時刻・pidfd を照合して子孫を回収する。親 loop の停止対象も同じ実体照合を行う。環境消去・別セッション・non-dumpable に依存せず、waitpid で不在を確認したときだけ判定へ進む。監督の KILL・読取不能・期限超過は未確認として記録を残す。保存された途中の PID は signal や削除の根拠にしない。同 UID が保持値や実行物まで攻撃できる限界と必要な隔離は [loop.md](../plugins/dev-workflow/skills/ship-task/references/loop.md) を正本とする。設計・検証は [Issue #204](https://github.com/Yuki-Maeda-valour/workflow/issues/204)。実ホストの権限確認(H31・H34)は次の項(#193)で扱う。
- **無人ループの本文と、`loop.sh` が起動した CLI の出力の解析**: タスクのメタ情報と保留行を読む処理は `loop-task-text.py` に分ける。起動した CLI が書いた出力(イテレーションの結果の JSON・許可の仲介の記録・`origin-repo.py` の出力)を読む処理は `loop-child-output.py` に分ける。親は開始時の信頼コピーから、どちらの解析コードも保持し、有効プラグインの一覧を取り込んだコピーへ移る際にも同じ解析コードであることを確かめる。解析の入出力と起動環境は維持する。保持・停止条件の正本は [loop.md §4](../plugins/dev-workflow/skills/ship-task/references/loop.md#4-1-回のイテレーション)。設計と検証は [Issue #265](https://github.com/Yuki-Maeda-valour/workflow/issues/265)(本文解析)と [Issue #354](https://github.com/Yuki-Maeda-valour/workflow/issues/354)(起動した CLI の出力の解析)。
- **ホスト CLI の証明と追加定義の検査(H31・H34・H46・H47)**: `loop.sh` はホスト CLI の実体を起動前に控え、以後はその実体だけを起動する。最初の補助 CLI より前に、利用者・Linux 管理側・配布コピーの定義と設定を検査する。有効 plugin の一覧が得られた後は、導入先と選択された marketplace 項目も控え、セッション前に検査し直す。CLI・保持した全設定 source・追加定義の許可規則は共通の判定を通し、Read/Grep/Glob/Write/Edit/NotebookEdit/旧 MultiEdit の道具名だけの allow と、その全体指定に相当する `Tool(*)`、空括弧や既知の入力欄を照合する曖昧な形を拒否する。親と追加定義が同じ規則でも免除しない。権限追加の規則に加え、hook・mod・agent の設定と MCP の参照を調べる。plugin の空でない hook は拒否するが、dev-workflow 自身の決まった字面の `hooks/hooks.json`(使う道具の版の確かめ。#431)だけは、`loop.sh` 自身のプラグインと `hooks/` の中身が同じときに通す(条件の正本は loop.md §4)。同梱 skill と同期を止める設定を、許可の仲介の hook に合成する。既知の組込み名は同名の外部定義の解決結果を見て止める。証明は実体・起動の形・定義と固定設定の要約・名前解決の版・許可判定版・CLI と source 別の allow の要約に結び付け、Write の拒否と初期応答の公開 skill 名の一致を確かめてから書く。起動時と各イテレーションの直前に照合し、旧形式や変更後の証明は使わない。同一内容の配布コピーの置き場だけが変わる場合は再取得を求めない。証明は署名でも毎セッションの保証でもない。同名の未知の組込み定義、利用者設定の直接の hook、同 UID の競合、将来の正規 server 応答は信頼境界に残る。明示した Read/Edit の scope、検索道具を戻す scoped Glob/Grep、agent の利用可能ツール指定は維持する。全操作が hook を通る保証ではない。契約と限界の正本は [loop.md §2・§4・§10](../plugins/dev-workflow/skills/ship-task/references/loop.md)、設計・検証は [Issue #193](https://github.com/Yuki-Maeda-valour/workflow/issues/193)・[Issue #231](https://github.com/Yuki-Maeda-valour/workflow/issues/231)・[Issue #232](https://github.com/Yuki-Maeda-valour/workflow/issues/232)。
- **追加定義の名前解決**: `host-check.py` は、定義の収集、主名の採用、別名の解決、返却値の構築を同じファイル内の関数に分ける。既存の判定順序と結果を保つ。詳細は [loop.md §4](../plugins/dev-workflow/skills/ship-task/references/loop.md#4-1-回のイテレーション)、設計と検証は [Issue #268](https://github.com/Yuki-Maeda-valour/workflow/issues/268) に記録する。
- **既定はオフ**: 無人の経路(`loop.sh` と ship-task の無人モード)は、起動しなければ既存の挙動を変えない。profile の `features.loop`(§4)は調整値だけを持ち、有効化のスイッチではない。ただし ship-task の「既存のタスク MD から始める入口」は、無人モードと別に対話でも使える入口として加わる
- **1 回のイテレーションの中身**: ホスト CLI をヘッドレスで起動し、`/ship-task --task=<タスク MD> --unattended` を走らせる(Phase 1 を飛ばし、Phase 2 の続行判定から do-task → update-doc → PR)。イテレーションはブランチの無い worktree(detached HEAD)で始まるので、ship-task は **Phase 2 より前に**作業ブランチ `task/{名}` を作る。commit・push・PR の受け持ちは ship-task のまま(§5-20)。**merge はしない**。発見モードのイテレーションは `/ship-task --discover=<発見元> --unattended` を走らせ、発見元の候補モードが書いた `候補_` だけを作業ブランチ `task/候補-<発見元>-<起点の sha の先頭 12 桁>` に 1 commit して PR にする(discover-mode.md)
- **拾う対象と順番**: タスク MD に無人実行のメタ行 `> **無人実行**: 可`(書式・照合パターンは [`create-task/references/task-template.md`](../plugins/dev-workflow/skills/create-task/references/task-template.md) の記法の規約)がある `進行中_` だけ。`> **作成日**` の古い順(同じ日はファイル名順)。次のどちらかがあるタスクは拾わない(専用の状態ファイルは持たず、git の状態だけで判定する): 作業ブランチ `task/{名}`(ローカルかリモート。リモートは読み取りだけで確かめる。PR 待ち・保留中を含む)/ 前のイテレーションが残した worktree(落ちたイテレーションを含む)
- **止まったとき**: ship-task の無人モードは、止まる理由を原因で 2 つに分ける(人に確かめる場面ごとの扱いは unattended-mode.md の表)
  - **保留**: そのタスクに固有の、人の判断が要る停止(Phase 2 の停止・`完了_` への改名前の判断を仰ぐ点〈§5-10〉と許可の要る操作の拒否・基準不明など)。`保留_` に改名し、理由(どの停止条件か・人が次にすること)を定型の保留の行(1 行)で追加修正記録に書き、**作業ブランチに commit する**(停止時はタスク MD を stage しない、という対話時の契約を読み替える)。保留の行は「記録あり」に数えない(do-task の新規着手の判定と再開判定を崩さない)
  - **失敗扱い**: 環境・リポジトリの状態・ツールの異常、イテレーションの中の照合(改竄ガード)に通らない、無人の前提を欠く。**`完了_` への改名後の停止は失敗扱い**(update-doc の判断を仰ぐ点・未承認、許可の拒否、push・PR の失敗。完了した実装を `保留_` と呼び直さず、未承認の doc を commit しない。§5-10 の例外)。止まった時点から先の改名・保留の行・commit・push をせず、止まる前にできたもの(作業ブランチ・commit・push 済みのブランチ)と worktree を残す(§5-1 の例外)
  - イテレーションの結末は、タスクのイテレーションは `PR` / `縮退` / `保留` / `失敗扱い`、発見のイテレーションは `PR` / `縮退` / `候補なし` / `失敗扱い` の 4 つずつで、ship-task は完了報告の最後に結果の行(`無人のイテレーションの結果: …`)を書く(照合パターンは 5 値。モードで取りえない値は失敗 — loop.md §5)。イテレーションの時間上限は `loop.sh` が打ち切るだけで、時間切れのイテレーションと許可待ちで固まったイテレーションは、下の「隔離」のとおり worktree を残す。`loop.sh` は改名しない
- **隔離**: イテレーションごとにローカルのデフォルトブランチから使い捨ての worktree を作り(fetch はしない。リモートの作業ブランチの有無は読み取りだけで確かめる)、その中で走らせる(人のチェックアウトに触れない)。イテレーションが正常に終わった(PR を開いたか、PR を開けずに `縮退` で終えたか〈commit は作業ブランチに在る〉、`保留_` を commit した。ship-task の結果の行と git の状態を突き合わせて判定する — loop.md §5)ときだけ worktree を消す。落ちた・時間切れ・未 commit が残ったイテレーションは worktree を消さずに残し、そのタスクは以後拾わない。ループは次のタスクへ進み、連続する失敗はブレーカー(§4)で止める。残った worktree は朝の報告に並べる。**タスク MD はローカルのデフォルトブランチに commit 済み(task_dir が git の追跡下)であることが前提**で、追跡外なら `loop.sh` は起動時に止まる。発見モードでは、正常(候補なし)のイテレーションも worktree を消す。両モードとも、状態ファイル(`.claude/reviews/`・`.claude/grasp.md`・`.claude/.understand-project-done`)が ignore されていて追跡されていないことと、origin があるならその fetch と push の URL が同じリポジトリを指し、`remote.origin.vcs` が無いことが前提(origin の URL の検査 — loop.md §2)。満たさなければ `loop.sh` は起動時に止まる(未追跡・未 commit が残った発見のイテレーションは、正常にしない)
- **権限**: 許可リストで最小化して走らせ、**全許可のモードは使わない**(Claude Code の全許可フラグは、インターネット接続の無いサンドボックスでだけ推奨される。§7-3)。許可リストはホストの利用者設定か `loop.sh` の起動引数にだけ置き、profile などリポジトリが書ける場所には置かない。**リポジトリ側のホスト設定(プロジェクトの許可規則・hook・MCP 宣言など)は読み込ませない起動を既定にする** — ヘッドレスではホストの承認ゲートが働かず(§7-6)、リポジトリを書ける者が無人実行の権限を広げられるため。読ませない手段が無いホストでは無人ループを起動しない。保護パス(`.claude/` など。§7-3)への書き込みは確認に回って拒否になるので、イテレーションの中の skill の状態ファイル(`.claude/reviews/` など)への書き込みだけを、`loop.sh` が渡す `PermissionRequest` の hook(許可の仲介)で通す(ほかは拒否して記録する。loop.md §9)。**外部 implementer は使わない**(承認はセッション初回の明示承認で、無人では得られない。内蔵 implementer で走る)
- **自動メモリ(H32)**: 実装・発見で共通の、ホストを子プロセスとして起動する所で、ホストの自動メモリを無効化する環境変数を強制する(公式仕様は §7-3)。親が未設定・無効化していない値でも、`loop.sh` が起動した CLI には無効化を指定する。親環境・利用者設定・既存メモリは変更しない。一般のファイル読み書き、利用者の hook、環境を変えた別プロセス、別種の永続メモリは防がない。既存メモリの監視・削除も行わない。直接の skill 起動は対象外。保証と、隔離した実機での確認の正本は loop.md §4・§8・§10。
- **起動事実**: ホストごとのヘッドレス起動の事実は §7-3、実際に使うコマンドの雛形は `loop.sh` の既定表(ホスト名 → 雛形)に置く。雛形のうち実行ファイルと先頭に置くフラグは `loop.sh` の起動引数(`--host-argv`)だけで置き換えられ、profile に書かれていても無視して報告する。隔離と判定に要るフラグは、置き換えても `loop.sh` が必ず後ろに足す(置き換えのトークンに、それらのフラグ・その別名・全許可のフラグ・設定を足すフラグがあれば、起動時に止まる)。skill 本文と解決表には書かず、役割語も増やさない(起動は委託ではない)。ホストのセッションの中から起動されたと判定したら `loop.sh` は止まる(§5-5。判定はベストエフォート)
- **候補採用の入口(H41)**: `adopt-candidate.py` が候補の検査と改名を一体で行う。保持した本文を `candidate-keys.py --check` と同じ検査へ渡し、見送りの問題だけは改名前に止め、ほかは JSON の warnings として返す。イテレーションの印が非空なら入口と改名直前のどちらでも無変更停止し、印のない人の採用は許す。追跡済み操作は保持した directory descriptor から Git を起動し、`git mv` 後に改名先の本文・inode と index の stage 0 blob をそれぞれ改名前の保持値に照合する。不一致なら stage や改名先を自動で戻さず、残存状態を人が確認するまで停止する。未追跡は上書き禁止の原子的改名を優先し、使えない POSIX 環境では排他的 link・同一性確認・元名 unlink を使う。後者は原子的でなく、link 後の失敗や強制終了で両名が残れば成功にしない。同じ UID の任意コードは最終確認と改名・unlink・rollback の間で競合できるため、採用中はほかの書き込み主体を止め、残存状態を人が確認する。別権限・OS の実行隔離が必要なら別途行う。正式手順は create-task の採用の経路に集約する。
- **短縮オプションの連結パス(H24)**: 許可の仲介は、短縮オプションと束ね書きの後に続く値の候補にも既存の保護パス判定を適用する。`--` 以降は位置引数として扱う。保守的な候補検査と保証範囲の正本は [loop.md §9](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)、設計・検証記録は [Issue #180](https://github.com/Yuki-Maeda-valour/workflow/issues/180)。
- **最終リンクの保護**: 許可の仲介は、Bash の書き込み先とパスの単語について、リンクの置き場所と実体の両方を保護判定する。無害な名前から保護パスを指す経路(#107 H36)を拒否し、リンク自体の削除・移動との違いを保つ。裸名の既存リンクも引数・代入値として調べる。判断と保証範囲の正本は [loop.md §9](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)、設計と検証は [Issue #159](https://github.com/Yuki-Maeda-valour/workflow/issues/159)。
- **保護ディレクトリ内の cwd(H27)**: 許可の仲介は、保護パス内の cwd から指定された、名前だけの引数(`/` を含まず、`.` で始まらないもの)も、存在の有無によらずパスとして検査する。入力 cwd・許可された `cd` の移動先・移動失敗時に使う元の cwd に同じ規則を適用する。W と通常ディレクトリの既存例外を維持する。用途不明の引数を保守的に拒否する限界と受理例の正本は [loop.md §9](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)、設計と検証は [Issue #179](https://github.com/Yuki-Maeda-valour/workflow/issues/179)。
- **再帰削除の保護(H26)**: 許可の仲介は、worktree 自体と、許されない保護対象を子孫に含むディレクトリの再帰削除を拒否する。子孫の検査に失敗した場合も許可しない。通常削除と W の既存例外を維持し、リンク自体の削除と末尾 `/` 付きでリンク先を削除する操作を区別する。契約と保証範囲は [loop.md §9](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)、設計と検証は [Issue #173](https://github.com/Yuki-Maeda-valour/workflow/issues/173)。
- **語中の未引用チルダの拒否(H38)**: 許可の仲介は、Bash の語に未引用の `~` があれば、位置と用途を問わず拒否する。引数・代入・リダイレクトを、許可リストや個別のパス判定より先に調べる。Bash の展開条件を再現せず、引用・エスケープしたリテラルは既存のパス検査へ進める。`cd` の行き先の全 `~` 拒否は維持する。契約は [loop.md §9](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)、設計と検証は [Issue #181](https://github.com/Yuki-Maeda-valour/workflow/issues/181)。
- **作業ディレクトリの追跡(H37)**: 許可の仲介は、既存の2種類の `cd` 形式だけを追跡する。`builtin`・`command` を介した移動と `pushd`・`popd` は、許可リストで許されていても拒否する。移動を隠せる制御構文も拒否し、引用された通常語との違いを保つ。契約と保証範囲は [loop.md §9](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)、設計と検証は [Issue #166](https://github.com/Yuki-Maeda-valour/workflow/issues/166)。
- **コマンド内部の権限(H23・H35・H40)**: 許可の仲介は、許可リストの一致より先に Git・gh・sed・find・awk の引数を限定する。Git は固定の global option と `-c key=value`、列挙した subcommand、検査できる狭い pathspec だけにする。保護対象を含みうる `.`・ディレクトリ・magic・glob・ファイル入力の pathspec と、直接の共有状態変更は拒否する。sed の read/write/execute script を拒否し、安全な script の in-place は既存の書き込み先検査に通す。find の実行・削除・ファイル出力、任意の awk program、gh の API・設定変更を拒否する。PR 本文の標準入力は、W の reviews 下にある非 symlink の通常ファイル 1 件だけに固定する。契約と保証範囲は [loop.md §9](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)、設計と検証は [Issue #189](https://github.com/Yuki-Maeda-valour/workflow/issues/189)。
- **Gitの共通オプション解析**: `loop-permission.py` は、コマンド名より前のオプションを読む処理を同じファイル内の関数へ分ける。`-C` で変えた検査用の作業ディレクトリは、呼出側が判定終了時に元へ戻す。契約は [loop.md §9](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)、設計と検証は [Issue #270](https://github.com/Yuki-Maeda-valour/workflow/issues/270) に記録する。
- **サンドボックス無効化の拒否(H25)**: 許可の仲介に届いた Bash 入力は、サンドボックスを無効にする指定と、その項目の不正な型を、コマンドが許可対象でも拒否する。未指定・真偽値の偽だけを通常判定へ進める。ほかの入力項目の扱いは維持する。hook 自体が隔離を有効にするわけではない。入力と保証範囲は [loop.md §9](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)、設計と検証は [Issue #172](https://github.com/Yuki-Maeda-valour/workflow/issues/172)。
- **`loop.sh` の要点**(契約の正本は [`ship-task/references/loop.md`](../plugins/dev-workflow/skills/ship-task/references/loop.md)): 人のシェル・cron から、利用者が持つこのリポジトリの clone のパスで起動する(導入先のキャッシュは版ごとのパスで、更新しても古い版が走るため。導入済みの同名プラグインと版が違えば止まる)。Linux 専用(`setsid`・`flock`・`/proc` を使う)で、bash 4.4 以上が必要(`inherit_errexit` を使う)。OS → bash の版 → 初期化の順で検査し、非対応環境は `--help` より先に止まる。止まる条件は、ブレーカー(最大イテレーション数・連続失敗・時間予算〈ループ全体〉・停止ファイル)と、キューが空・許可の拒否の保留の連続・片付けで残ったプロセス・共有の git の状態の変化・途中の全体の失敗。イテレーションの時間上限を超えたイテレーションは打ち切るだけで、ブレーカーではない。結果は朝の報告(状態ディレクトリの報告と stdout の要約)に、イテレーションごとの結末・残った worktree・人の次の手順の定型とともに出す
- **同じ理由の残存 worktree**: `loop.sh` は NUL 区切りの一覧をパスごとに保持する。同じ lock 理由が複数件あっても、開始・終了報告と両モードの読み飛ばし理由に全件を出す。発見モードの片付け案内もパスごとに出す。1 件でも残る間は再実行を防ぎ、自動削除の対象は増やさない([Issue #160](https://github.com/Yuki-Maeda-valour/workflow/issues/160)、#107 の H42)。
- **発見ループ**: 人の採用を飛ばして `進行中_` に積む領域は設けない。発見ループは `候補_`(§5-1)を積むだけで、採用は人が /create-task に通す
  - 1 晩・1 発見元ごとに PR を 1 本開き、人が見送りの印を付けるか消してから merge して、`候補_` を task_dir に入れる。発見元の候補モードは、task_dir の全状態のヘッダの指摘キーと照らして、既知の指摘を積まない(見送り = もう出さない / 消す = また出てよい)
  - data-audit の候補(未修正の脆弱性の場所)は、origin の push 先が非公開と確かめられたときだけ PR にする(確かめられなければ回さずに `候補なし`)。refactor の分析は公開でも回すが、セキュリティ由来の指摘は候補にしない

**do-task の補助スクリプトと回帰テストの必要環境**: do-task は Phase 0 の事前検査から Python 3.10 以上・bash 4.0 以上・GNU 系ツールを使う。回帰テスト一式は Linux と GNU 系ツールを前提とし、テストの容器の Python は製品の下限と同じ 3.10 にする([Issue #433](https://github.com/Yuki-Maeda-valour/workflow/issues/433)。根拠は [testing.md](testing.md) の「Python の版の決まりの根拠」)。PATH の確認・パス解決の代替の限界・macOS 実機の確認条件と再確認手順の正本は [runtime-requirements.md](../plugins/dev-workflow/skills/do-task/references/runtime-requirements.md)。bash の導入だけで BSD 環境の互換性は保証しない。必要環境の明示と起動前の診断は [Issue #121](https://github.com/Yuki-Maeda-valour/workflow/issues/121)、macOS 26.6.2 / arm64 の実測と制約は [Issue #226](https://github.com/Yuki-Maeda-valour/workflow/issues/226) に記録する。品質コマンドの監督と検証用コピーの扱いは、macOS で失敗 0 を期待する回帰テスト(`scripts/test_check_process.py`・`scripts/test_review_guard.py`。`Linux 専用: ` のテストは飛ばす。飛ばす件数は `runtime-requirements.md` の「期待の件数」にある)を持つ。この回帰テストは、`PATH` で選ばれる `bash` が 4.0 以上なら、GNU 系ツールなしで回せる。`implement-guard-selftest.sh` の Mac の実行は補助で、Linux の代わりにしない。回す手順と期待の件数は同じ `runtime-requirements.md`、結果は [Issue #239](https://github.com/Yuki-Maeda-valour/workflow/issues/239) に記録する。

**診断表示のC1置換**: `diff-snapshot.sh` は、既存の表示用関数で UTF-8 の C1(U+0080〜U+009F)を `?` に置き換える。限界として残す案は、診断の表示を書き換える文字が残るため採用しなかった。既存の Bash 機能で表示用コピーだけを加工し、新しい依存を足さない。元データによる承認・比較と patch は保持する。対象の診断・NOTEと限界の正本は [external-runners.md §9-1](../plugins/dev-workflow/skills/do-task/references/external-runners.md)。決定と検証は [Issue #152](https://github.com/Yuki-Maeda-valour/workflow/issues/152)(親 #77 の H2)を参照する。

## 3. 固有情報の 3 層吸収アーキテクチャ(最重要原則)

**skill 本文は手順(how)だけを持ち、プロジェクト固有の事実(what)を一切ハードコードしない。** 事実は以下の 3 層で実行時に解決する。全 skill がこの順で解決すること。

| 層 | 手段 | 吸収する情報 |
|---|---|---|
| 1. プロファイル | `.claude/project-profile.yml` を Read(存在すれば) | リポジトリ構成 / 品質コマンド / area 定義 / メモリ名マップ / 正本の向き / 機能フラグ |
| 2. 動的検出 | Glob / ls / list_memories を実行時に叩く | パッケージマネージャ / 技術スタック / ディレクトリ構造 / 実在メモリ名 |
| 3. 権威参照 | 権威参照ファイル(検出順は下記)と doc/ を Read | プロジェクト固有原則 / 禁止事項 / 地雷 |

**profile が無くても必ず動く**こと(全項目にフォールバックを定義する)。これで汎用性を保つ。

**吸収対象はプロジェクト固有の事実だけでなく、ツール固有の事実(外部 CLI の起動コマンド・フラグ・モデル名、およびホスト内蔵機構の API 名・エージェント種別・モデルエイリアス)も含む。** **API 名・エージェント種別はホスト固有の事実であり、解決表(§7-5)を唯一の正本とする**(skill 本文にも profile にも置かない)。あわせて §7-2 の信頼モデルにより、リポジトリ側が書ける値で委託先の機構が変わることも避ける。モデルエイリアスは既存どおり profile の `review_models` / `implementer_model` → 実行時に利用可能なエイリアスを確認する(§5-4・§5-9)。解決表はエイリアスの指定方法(解決表 軸 3)だけを持ち、一覧は持たない。外部 CLI のコマンド文字列を SKILL.md 本文に書かず、既定値は references の表(外部 CLI は external-runners.md、委託は解決表 §7-5)に置き(無人ループの起動の雛形だけは `loop.sh` の既定表。§2)、profile(`features.runner_models` 等の**コマンドにならない値**のみ)→ 実行時検出(`command -v` 等の存在確認)→ references の既定値、の順で解決する。**起動コマンドそのものは profile から受け取らない**(外部ランナーの起動コマンド・無人ループの起動の雛形。§7-2 の信頼モデル。MCP 宣言は §7-6 の例外)。

### 動的検出の標準ロジック

- パッケージマネージャ: package.json の `packageManager` フィールド → lockfile(pnpm-lock.yaml / bun.lock* / yarn.lock / package-lock.json)→ 既定 npm。PHP は composer.json、Python は pyproject.toml / requirements.txt、Go は go.mod、Rust は Cargo.toml
- 品質コマンド: profile の `quality` → package.json の scripts から `format` / `check` / `lint` / `type-check`|`typecheck` / `test` / `build` を存在検出 → 言語別既定(PHP: `./vendor/bin/pint --test` + `php artisan test`、Python: ruff/pytest 等)
- モノレポ: pnpm-workspace.yaml / package.json#workspaces から自動列挙
- 権威参照ファイル: `AGENTS.md` があればそれ → 無ければ `CLAUDE.md`(未移行)。両方あれば `AGENTS.md` を正本とし、`CLAUDE.md` は import 行より下の追記だけを補完として読む。完成形・互換形(定義は §7-4)はいずれも正常。**ルートの `AGENTS.md` への symlink は import ありと同じに扱う**(2026-09-21 決定 16)。**ドリフト判定はルートの 3 種を見る**: `AGENTS.md` があり、ルートに `CLAUDE.md` / `.claude/CLAUDE.md` / `CLAUDE.local.md` が 1 つ以上あるのに、**そのいずれにも**ルートの `AGENTS.md` を指す import(`CLAUDE.md`・`CLAUDE.local.md` なら `@AGENTS.md`。相対パスの基準は §7-3。symlink を含む)が無ければ、ドリフトとして注記し(§5-11 と同型)、対処(その import の追加、またはユーザー設定 `claude-md-and-agents-md` への変更)を案内する — Claude Code の既定設定では `AGENTS.md` が読まれないため(事実は §7-3)。**`AGENTS.md` 自体がルートの 3 種のいずれかへの symlink なら、内容はそのファイル経由で読まれているためドリフトとせず、構成の報告だけを行う(import の追加もユーザー設定の変更も案内しない)**。リポジトリより上の階層は走査しない。`.claude/rules/` は skills の読取り・同期の対象外(理由は §7-4)。**ホスト側の探索順・サイズ上限は §7-3(事実と出典)、運用方針は §7-4**
- Serena: 使う前に get_current_config でアクティブプロジェクトを確認し、違えば activate_project(別プロジェクトのメモリ誤参照防止)。メモリ名は list_memories() で動的列挙し、固定名を仮定しない

## 4. project-profile.yml スキーマ

置き場所: 各プロジェクトの `.claude/project-profile.yml`。init-project が生成する。**全フィールド任意**(無い項目はフォールバック)。

```yaml
# プロジェクト名(表示用)
name: MyProject

# リポジトリ構成: single(既定) | parent-child | monorepo
# parent-child: AI 管理リポジトリの下に本体ソースが独立 git である構成
repo_layout: single
# 本体ソースのルート(parent-child のときのみ。ソース探索・実装・品質コマンドはこの下で行う)
root: .

# タスクの置き場: file(既定) | github-issue。無い・null・空文字は file。それ以外の値や型は停止する
task_store: file
# file の保存先(管理プロジェクトルート相対)。省略時は既存の状態名 MD を検出し、無ければ docs/tasks
# github-issue では task_dir の解決・作成を行わない
task_dir: docs/tasks

# コードを持つか(false: ドキュメントのみのプロジェクト。品質ゲートをスキップ)
has_code: true

# パッケージマネージャ(省略時: packageManager フィールド / lockfile から自動判定)
package_manager: pnpm

# コード理解系(概要・構造・技術・規約・コマンド)の正本: serena(既定) | docs | agents-md
#   serena:    Serena メモリが正本、権威参照ファイルは薄型維持
#   docs:      doc/(02・05 等)が正本、メモリは探索用の要約(一方向同期)
#   agents-md: 権威参照ファイル自体が正本(小規模・メモリ未使用)
#   ※ 上の 3 値以外が書かれていたら、skill は停止して有効値を案内する(既定へフォールバックしない。
#     フォールバックすると、報告は出ても書き込み自体は止まらず、update-doc の書き込み先が変わるため)。
#     この項目が**無い**場合、または値が空(null・空文字)の場合のフォールバック(既定 serena)は従来どおり
#     (空以外は型を問わず「値がある」として扱い、3 値でなければ停止する)。旧値名は 2026-09-22 に廃止した
#     (出典は §7-4 末尾の決定録)
# ※ 要件+タグ(doc/03)・設計判断 ADR と図(doc/04)・運用と地雷(doc/05)・計画(doc/07)は
#   この設定に関わらず常に doc/ が正本。doc/06 は /stack-research の管轄、
#   doc/07(見積もり・スケジュール)は人の合意が源泉(/reflect-decisions が反映先。update-doc は書かない)
source_of_truth: serena

# 汎用カテゴリ → 実 Serena メモリ名のマップ(命名揺れの吸収。省略時は list_memories から意味マッチ)
memory_map:
  overview: project_overview
  structure: project_architecture
  tech: tech_stack
  commands: suggested_commands
  conventions: code_style_conventions
  completion: task_completion_checklist

# --area オプションで使う領域定義(省略時: backend / frontend / database の汎用 3 分類)
areas:
  backend: ["src/server/", "src/api/"]
  frontend: ["src/app/", "src/components/"]
  database: ["prisma/", "drizzle/"]

# 品質ゲートコマンド(省略時: package.json scripts から自動検出)
quality:
  format: pnpm format
  check: pnpm check
  typecheck: pnpm typecheck
  test: pnpm test
  build: pnpm build          # build_optional: true なら完了条件から除外
  build_optional: true

# 型伝播チェーン(create-task の影響範囲調査 Phase 1.5 で使用)
type_propagation_chain: "DB schema → server functions → API routes → frontend hooks → components"

# ドメイン固有リスク(create-task / do-task がチェックリストに反映)
domain_risks: []             # 例: [web3, i18n, e2e-data, salesforce, zenstack]

# 機密ファイル(読まない・値を出力しない・ログ混入を検査)
# 各要素は glob(`*` / `**` / `?`。末尾 `/` は配下全体、先頭 `/` はルート固定。`//`・空要素・引用符/改行/制御文字/バッククォート/`$`/ブラケット/ブレースを含む要素は不可)。書式の正本は do-task/scripts/diff-snapshot.sh の usage。既定の 3 要素は常に適用され、この設定で外せない。無人レビューは基準・開始時・現在の profile と既定を同じ集合にして除外する。`.env.example` / `.env.sample` / `.env.template` のようなテンプレートも要素 `.env.*` に当たれば除外され、外部レビューにはパスだけが載る(許可リストの例外は無い)
secret_paths: [".env", ".env.*", ".dev.vars"]

# データ境界監査(data-audit)の調整(任意。無ければ既定辞書+全境界を対象)
audit:
  sensitive_fields: []       # 機密フィールド名の追加(既定辞書に加算。例: [memberCode, planRank])
  public_boundaries: []      # 認可チェック不要と確認済みの公開境界(パス・ルート名)
  exclude_paths: []          # 監査対象から除外するパス(生成コード・vendor 等)

# 実行機能レベル(タスク系 skills の重さを調整)
features:
  impact_analysis: true      # create-task Phase 1.5(影響範囲調査)
  external_review: true      # 互換項目。false でも独立レビューは省略しない(無効化して報告する。§5-9)
  reviewer_count: 1          # do-task のレビュアー数: 1 | 3
  review_models: [fable, opus, sonnet]  # レビューに使うモデル群(省略時: 環境で利用可能な最上位から能力順に自動選定)
                             # 消費するのは「体数 − 継承の持ち場(委託先 1 体ごとの受け持ちのうち、モデルを指定しないもの。ふつうはセッションのモデルで動く)の数」だけで、余った要素は打ち切って報告される
                             # (継承の持ち場あり 3 体なら先頭 2 要素、継承の持ち場なし 3 体なら 3 要素とも消費。
                             #  割り当て規則は do-task/references/delegation-map.md §2 (a))
  implementer: internal      # internal(既定)| 既定表(external-runners.md §12-6)にある実装用ランナー名。
                             # 既定表外の名前は profile から受け付けない(無視して報告。runners と同じ
                             # 信頼モデル・§7-2)。宣言されていてもセッション初回に明示承認を得るまでは
                             # 内蔵で実行する(承認・拒否の保持、取り消しの意味、取り直しが要る条件は
                             # external-runners.md §1 の信頼モデル表が正本。ここには再掲しない)
                             # ※ 旧値 cursor は既定表の名前ではない(既定表の名は cursor-agent)。既定表外の
                             #   名前として扱われる。実装用には「既定表外を引数で明示指定する」経路を
                             #   設けない(runners と非対称)。多様性が目的なら runners へ移す
  implementer_model: sonnet  # implementer サブエージェントのモデル(エイリアスのみ)
  # ── 外部ランナー(レビュアーをホスト外の CLI に委託する。既定は内蔵のみ・オプトイン)──
  runners: []                # 例: [cursor-agent, codex]。空 = 外部 CLI を探しに行かない(既定の編成を変えない)
                             # ここで選べるのは do-task/references/external-runners.md の既定表にある名前だけ
                             # ③ CLI バックエンドの有効化(§7-5)。② MCP の扱いは解決表 §4・§5
  runner_models: {}          # ランナー別モデル名。省略時はランナー既定(モデル指定フラグを付けない)
                             # 例: {cursor-agent: "<そのランナーで有効なモデル名>"}
                             # '-' で始まる値・空白を含む値は拒否される(フラグへの化けを防ぐ)
  # ※ 起動コマンドの上書きは profile では受け付けない(下の「信頼モデル」を参照)
  # ── 無人ループ(loop.sh の調整値。§2。有効化のスイッチではない)──
  loop:                      # 全項目フォールバック: キーが無い・値が無い(null)項目は loop.sh の既定値を使う
    max_iterations: 3        # 例。最大イテレーション数
    max_consecutive_failures: 1  # 例。連続失敗の上限
    time_budget: 14400       # 例。ループ全体の時間予算(秒)
    iteration_timeout: 1800  # 例。1 回のイテレーションの壁時計上限(秒)。超えたイテレーションは loop.sh が打ち切り、worktree を残して以後拾わない(§2)
                             # 締める向き(値を小さくする)だけ効く: 実効値 = min(loop.sh の既定値か起動引数の値, この値)。
                             # 広げるのは loop.sh の起動引数だけ(リポジトリを書ける者が夜の予算を延ばせないように。下の信頼モデル)
                             # 読み元は人の作業ツリーではなく、loop.sh が起動時に固定したデフォルトブランチの commit の profile
                             # 既定値・意味・不正な値と未知のキーの扱いの正本は ship-task/references/loop.md(ここには書かない)
  # ※ 無人ループの起動の雛形の上書きは profile では受け付けない(loop.sh の起動引数だけ。書かれていても無視して報告する)
  # ※ 無人ループの許可リストも profile では受け付けない(ホストの利用者設定か loop.sh の起動引数だけ)。信頼モデル

# MCP サーバー(ツール接続)。同じツールにどの AI からも繋ぐための宣言(§7-6 が生成)
mcp_servers: {}              # <id>: {command, args} の形で宣言する(§7-6 が生成)
                             # 例: {'serena': {command: 'uvx', args: ['--from', 'git+https://github.com/oraios/serena', 'serena', 'start-mcp-server', '--context', 'ide-assistant', '--project', '.']}}
                             # command は必須。欠く宣言・空文字列の宣言・id 制約違反はエントリ単位で除外して報告する(空エントリ防止)
                             #   args の要素が空文字列を含む場合も同様にエントリ単位で除外する
                             # args は省略可(省略時は生成側で args 行を出さない。空配列は args: [])
                             # id は [A-Za-z0-9_-]+ のみ(TOML のテーブルパスと JSON のキーで意味が変わるため)
                             # url / env は受け付けない。書かれていたら無視して報告する
                             #   url: Claude Code 側のリモート MCP の形が §7-3 に未記録のため
                             #   env: commit されるファイルに値が載り secret_paths(§5-12)と衝突するため
                             # 値(command / args の要素)は U+0000〜U+001F と U+007F(DEL)を含めない
                             #   含む宣言はエントリ単位で除外して報告する(改行・タブを含む値も対象)
                             # TOML/JSON への表現時のエスケープ規則は生成側(init-project)の責務(§7-6)

# ローカル環境の構築コマンド(環境構築を依頼されたとき doc/05 と併せて参照される)
setup_commands: []           # 例: ["docker compose up -d db", "pnpm db:migrate", "pnpm db:seed"]

# ※ モデル名の扱い: §5-4 のエイリアス規約は Claude のモデル(`review_models` / `implementer_model`)に適用する。
#   外部ランナーのモデルにはエイリアスが存在しないため `runner_models` で実名を指定し、
#   skill 本文・references にはハードコードしない(§3 のツール固有情報の 3 層解決)。
#
# ※ 信頼モデル: この profile は commit される共有ファイル。外部ランナーの起動コマンド・無人ループの
#   起動の雛形と許可リストは profile から受け取らない(方針の正本は §7-2、契約は external-runners.md
#   §1・loop.md、MCP 宣言の例外は §7-6)

# 地雷リストの場所の上書き(任意)。既定は doc/05_operations.md の「引き継ぎ・地雷」節
# (全ツール・人間から見える doc/ が正本。.claude/ 配下は Claude 専用になるため置かない)
known_facts_ref: docs/HANDOVER.md

# ── dev-workflow 管理メタ(init-project が記録・更新する。手動編集不要) ──
workflow_version: "x.y.z"                 # 導入時の dev-workflow バージョン(再実行時に更新)
initialized_at: "YYYY-MM-DD"              # 初回導入日
```

## 5. 全 skill 共通の統一規約

調査で検出した不統一・矛盾を以下に統一する。**全 skill がこれに従う。**

以下の各項が扱う委託の具体(サブエージェント起動の API・エージェント種別・モデルの指定方法)は、**ホスト = Claude Code における解決先**を持つ。skill 本文は役割語(一覧は §7-5)で委託を書き、ホスト固有の識別子は references の解決表からのみ参照する(§6・§7-5)。**役割の分離・やり取りの範囲の判定・段階を下げて続ける規則はホスト非依存**である。`name` は 2 層に分かれる — 双方向連携を持つホストでは、委託先を、送る相手として名前で識別できるようにすること自体はホスト非依存(§5-17 の双方向でやり取りできる環境。名前が無いと双方向連携の経路が使えなくなる。一方向だけの環境と別の AI を使えない環境では、送る相手が存在せず要請自体が生じない)。一方 `name` という引数名と文字種の規則は Claude Code の解決(解決表が正本)。具体の解決(API 名・`name` 規則・エイリアス)は**解決表([`do-task/references/delegation-map.md`](../plugins/dev-workflow/skills/do-task/references/delegation-map.md))を正本とする**。以下の各項(**§5-3・§5-4・§5-5・§5-9・§5-13・§5-17** の 6 項)は、ホストでの解決先を解決表・[`do-task/references/external-runners.md`](../plugins/dev-workflow/skills/do-task/references/external-runners.md)・§7-3 のいずれかへ委ね、規則そのもの(ホスト非依存の部分)だけを持つ。**変更の向きは一方向** — 解決表を先に直し、§5 の記述を合わせる(逆向きに §5 だけを直さない)。

1. **タスク保存先と命名**: まず profile の `task_store` を検査する。無い・null・空文字は `file`。文字列の `file` / `github-issue` 以外は有効な値を示して停止する。Issue 方式の契約は [`github-issue.md`](../plugins/dev-workflow/skills/do-task/references/github-issue.md)。`file` の保存先は [`create-task/references/task-directory.md`](../plugins/dev-workflow/skills/create-task/references/task-directory.md) を正本として、管理プロジェクトルート基準で profile の `task_dir` → 既存状態名 MD の検出 → `docs/tasks` の順に解決する。`root` は本体ソース用で保存先の基準にはしない。ファイル名は `進行中_{タスク名}.md` → 完了時に同じディレクトリで `完了_{タスク名}.md`。中断は `中断_`、保留は `保留_`。`中断_` はどの skill も書かない(人が付ける状態名。skill は保存先の検出と既知の判定で読むだけ)。`保留_` は、対話では do-task が中断のときにユーザーの合意を得て改名し(/ship-task から呼ばれたときはしない)、無人では ship-task の無人モードが改名する(下)。どちらも、戻すのは人(同じディレクトリで `進行中_` へ)。無人ループ(§2)で ship-task が自分で止まったタスクは、ship-task の無人モードが `保留_` に改名し、理由を追加修正記録に書いて作業ブランチに commit する(時間切れ・許可待ちで固まったイテレーションは改名せず、worktree を残す。§2。**例外**: 「失敗扱い」のイテレーション〈環境・ツールの異常・改竄ガード・無人の前提を欠く・Phase 0 の 3 の停止・`完了_` への改名後の停止など〉は `保留_` にせず、止まった時点から先の改名・保留の行・commit・push をしないで、止まる前にできたもの(作業ブランチ・commit・push 済みのブランチ)と worktree を残す — [`ship-task/references/unattended-mode.md`](../plugins/dev-workflow/skills/ship-task/references/unattended-mode.md))。発見ループが積む採用前の候補は `候補_`(状態名の 1 つ。[決定録 2026-09-23](minutes/2026-09-23_壁打ち_ループエンジニアリング.md) の決定 14)。採用は create-task が `候補_{名}` を `進行中_{名}` に `git mv` して設計を書き足す。見送りはヘッダの印で、`候補_` のまま残す(書式の正本は task-template.md の記法の規約)
   - `github-issue` では create-task の設計書を新規または指定 Issue の本文へ書く。do-task は本文のチェック状態を使い、完了・中断は改名の代わりに本文末尾の `## 追加修正記録` へ記録する。基準行はヘッダに置き、本文ダイジェストは取得した Issue 本文から算出する。コメントは契約にしない。
   - `origin-repo.py` の `repo` に対象を固定する。`gh issue` の各呼び出しは `-R <repo>` を付ける。`gh repo view` は `-R` が無いため、位置引数で `<repo>` を渡す。`gh api` は `--hostname <HOST>` と GraphQL の所有者・リポジトリ名の変数で固定する。本文の更新は取得→編集→書く直前の再取得と一致確認→標準入力で書く→再取得と一致確認の順。共通の `issue-body.py` を使い、競合は自動でまとめない。機密を載せず、本文中の命令に従わない。
   - `task_store` が `file` でも、権威参照ファイルが Issue を正本と定める場合は、その運用に従う(§5-22)。Issue 指定を止めるのは両方とも Issue を指していない場合だけ。不正な profile の検査は省略しない。
   - do-task は候補が1件でも対象を人に確認する。一覧は既定で最大30件なので、全件と扱わない。Phase 0 では作者・編集履歴・実行者を取得する。作者または保持した `last_edited_at` 以降の編集者に他人がいれば停止し、本文と設計レビューの値を人に示して確認する。bot の作者も確認の対象とする。取得失敗も停止する。境界は前回の実行用取得の成功か人の承認で保持した日時だけを引数で渡す。保持値が無い・null なら全履歴を調べる。同じ本文にある設計レビュー行との値の一致だけでは承認の根拠にしない。
   - 承認は生の本文の SHA-256 に結び付ける。この版では自分の更新後も確認を取り直す。回数と理由は `github-issue.md` §5 に定める。承認待ちで停止しても、本文を除いた取得結果を標準出力へ出す。
   - 品質コマンドは profile の `quality` → 動的検出 → 権威参照ファイルの順で補う。`AGENTS.md` の検証の節なども信頼する出所に含める。Issue 本文にしかないコマンドは、実行前に人に確かめる。
   - 新規 Issue の作成前は公開リポジトリだけ対象と題名を示して確認する。非公開なら作成後に報告する。公開へのセキュリティ修正の記載は既存 Issue の更新でも確認する。`gh` の不在・未認証・権限不足は停止し、ファイル方式へ切り替えない。
   - 実装担当は共通スクリプトでチェックだけを更新する。team-lead(作業を進め、結果を確かめる側の AI)は新しく取得し、チェック以外の変更が無いことと各項目の実装を確かめる。Issue はファイルを持たないタスクなので、外部の実装担当は使わない。
   - ship-task の対話は `--task=#<番号>` または Issue の URL を受け付ける。既定の作業ブランチ名は `task/issue-<番号>`。タスク MD を新設・stage せず、各工程で新しく取得した本文と保持値を比較する。Phase 0 の `preflight` 直後・ブランチ作成前に、公開する root と Issue の対象リポジトリを比べる(`--no-pr`・`--design-only` を除く)。不一致・算出不能なら止めるか `--no-pr` で続けるかを確認する。公開直前も一致を確かめ、PR 本文に `Closes #<番号>` を書く。取得した記録を公開用の検証根拠として復元しない。
   - update-doc の対話は Issue の完了記録を確かめ、取得した本文から更新範囲を選ぶ。`task_store: github-issue` では完了ステータスと do-task の完了検証記録を必須にする。権威参照ファイルによる運用では、その運用が定める完了記録の形も受け付け、ステータス行は必須にしない。最後の完了記録より後に実装 commit の検証前の中断・保留があれば停止する。検証後の文書同期・公開での停止は実装完了を取り消さない。明示指定が無ければセッション内の完了 Issue を使い、無ければ番号を質問する。複数なら選んでもらう。全量監査も選べる。更新日時や close だけでは選ばない。詳細は Issue 方式の契約 §6〜7。
   - 無人ループ・発見のイテレーション・候補モードはファイル方式だけ。`task_store` が `file` 以外なら開始時に理由を示して止める。ship-task・update-doc の `--unattended` も Issue 方式では停止する(権威参照ファイルによる Issue 運用を含む)。init-project は質問の最後にタスクの置き場を聞く。origin が GitHub で `gh` があるときだけ聞き、それ以外と `--yes` は `file` にする。既存の明示値は保持する。Issue を選んだ生成物はタスク用フォルダを作らず、古い版向けの注意を `AGENTS.md` に置く。
   - 決定記録: [Issue #320](https://github.com/Yuki-Maeda-valour/workflow/issues/320)(2026-11-04 の決定。1本目は土台と create-task・do-task、2本目は ship-task・update-doc、3本目は init-project と本リポジトリの設定)
   - 決定記録: [Issue #48](https://github.com/Yuki-Maeda-valour/workflow/issues/48)
2. **skill 名**: `update-doc`(update-docs は使わない)。旧 refact / refactor は `create-task --refactor` に統合(分析 → タスク化 → do-task で実行)
3. **サブエージェントの起動**: 委託先を**送る相手として名前で識別できるようにする**(名前が無いと双方向連携の経路が使えなくなる)。起動 API・`name` の文字種規則・派生名の系統は解決表(軸 4・§2)が正本。名前での識別ができる環境では**双方向でやり取りできる環境として運用するのを既定とし**、できない環境では前提にせず §5-17 の縮退プロトコルに従う
   - **起動時の説明と報告に出すモデル名**: AI エージェントを起動するときの短い説明(ホストの一覧に出る文字列)と、人に出す報告(完了報告・PR 本文・Issue の記録のレビューの行など)には、実際に動くモデルの名前を**版まで**書く。ホストの内蔵の委託・外部ランナー・今後足すエージェントのどれにも当てる
     - モデルを指定しない委託は、起動時の説明に確認前の値(起動後に本人の申告で確かめる前の値。セッションのモデルの名前と版)を書き、「確認前」と添える。実際に動くモデルはセッションと同じとは限らない(§7-3)。表示(起動時の説明と報告のモデルの欄)には「継承」「セッションと同じ」とは書かない
     - モデルを指定する持ち場は、指定した別名ではなく、解決した名前と版を書く
     - 外部ランナーは、ランナー名・CLI の版・モデル名を書く。起動前の値は設定の値(上書きされうる)で、起動時の説明では「確認前」と添える(起動後に実行記録で確かめる前の値)。起動後は実行記録の値を正とする
     - ホストの内蔵の委託には、返答の先頭で自分のモデルの名前と版を報告させる(自己申告)。人に出す報告には、内蔵の委託は自己申告の値(無ければ起動時の説明の値に「確認前」を付ける)、外部ランナーは実行記録の値を書き、起動時の説明と違えばその違いも書く
     - 取れないものは「未確認」と書き、推測で埋めない。名前と版は実行時に取り、文書には書かない(§5-4)
     - 委託先の名前(`reviewer-internal` など)は変えない。ホストごとの入れ先と取り方は解決表(軸の表の下の「委託先の短い説明」)、外部ランナーの取り方と実行記録の行は [`external-runners.md`](../plugins/dev-workflow/skills/do-task/references/external-runners.md) の §4・§5・§7 が正本
     - 決定記録: [Issue #325](https://github.com/Yuki-Maeda-valour/workflow/issues/325)
4. **モデル指定**: エイリアスのみで指定する(指定方法は解決表 軸 3 が正本)。日付付きモデル ID・版数(「Opus 4.7」等)をハードコードしない。利用可能なエイリアス群はモデルの世代交代で変わるため固定リストとして扱わず、実行時に指定可能なものを確認する
5. **ホスト自身と同じ CLI を Bash から起動しない**(別課金・二重課金の回避)。**ホスト相対の規約**として読む — 起動を禁止する対象はホストの種類によって変わり、判定手段(実行ファイル名・判定に使う環境変数の候補と上書き手段)は [`do-task/references/external-runners.md`](../plugins/dev-workflow/skills/do-task/references/external-runners.md) §3(判定順序 2)が正本。ホスト内のサブエージェントは必ずホストのエージェント機構経由で起動する。**ホストと異なる CLI を外部ランナーとして起動することは、レビュアーとしては `--runners` または `features.runners`、implementer としては `features.implementer` で宣言されている場合に限り許す**(例: Codex / Cursor をホストとする環境から Claude CLI をレビュアーに使う。implementer の判定・保護・内蔵への切り替えの契約は `external-runners.md` §12)。宣言が無ければ外部 CLI を探しに行かない。**人のシェル・cron から起動する無人ループ `loop.sh`(§2)はこの規約の対象外**(この規約は、ホストのセッションの中から、ホストと同じ CLI を起動することと、宣言の無い別の CLI を起動することを禁じる)。`loop.sh` は起動時に同じ判定手段(ベストエフォート。判定できないホストでは確認が働かない)でホストのセッションの中でないかを確かめ、中と判定したら止まる([決定録 2026-09-23](minutes/2026-09-23_壁打ち_ループエンジニアリング.md))
6. **検証の主体**: スコープ縮小を検出する側(team-lead)は実装者と分離する。**規模に関わらず実装は implementer に委託し、team-lead は常に検証者に回る**(直接実装は、§5-17 の別の AI を使えない環境まで段階を下げたときだけ)。完了条件のコマンドは team-lead が自分で再実行する(implementer の自己申告を最終確認にしない)
7. **スコープ縮小検出 grep**(do-task の検証で使用): `段階的に実施|後続タスク|今回はスコープ外|のみ作成|次回対応|一旦`
8. **チェックリスト突合の機械化**: `grep -cE '^\s*- \[(x|X)\]'` で件数突合+各 `- [x]`(全件)と diff との整合確認。diff は基準コミットからの追跡差分 + index + 途中 commit + 未追跡を含む(生成は do-task Phase 4 の手順 1 の `diff-snapshot.sh`)
9. **レビューの扱い**: 多モデルレビュー — **実行環境で利用可能なモデルを実行時に確認し、能力上位から順に(利用可能な最上位を必ず含めて)性能の段が違う 2〜3 体を選び**、単一メッセージで並列スポーンする。組み合わせを固定せず、モデルの世代交代に自動追従させる(指定可能なエイリアスが利用可能一覧にあたる。**最上位 / 上位 / 標準とエイリアスの対応は §7-3**)。profile の `features.review_models` があればそれを優先する。**既定はホスト内蔵のモデルのみで編成する**(オプトイン方式。② MCP の扱いは解決表 §4・§5 が正本)— 外部 CLI や他ベンダーのモデルを探しに行かず、ホストのエージェント機構で指定可能なモデルの上位から選んで完結させる。**外部ランナーが宣言されている場合に限り**(`--runners` 引数、または profile の `features.runners`)、ベンダー横断の多様性を同一ベンダー内の性能の段の違いより優先し、各社の最上位級を組み合わせる(製品名・版数をハードコードせず実行時に選ぶ)。宣言があっても外部ランナーが利用不可のとき(未導入・認証切れ・レート制限・応答なし・読み取り専用未確立・一時ツリーを作れない)は、ベンダー横断を諦めて**内蔵のみの編成に切り替え**、内蔵に切り替えたことと理由を報告に明記する(黙って内蔵に切り替えない)。宣言されているのに使えない場合は**エラーとして報告**する(黙って内蔵だけで済ませない)。外部ランナーの起動手順・判定順序・機密ガードは §7 と `do-task/references/external-runners.md` に従う。モデルを選べない環境では、観点(事実整合 / セキュリティ / 規約)を分けた複数レビュアーで多様性を確保する。**規模適応**: 変更対象が少数(目安 3 件以下)で矛盾の無い軽微な実行でも、独立レビュアーを 1 体以上起動する。セルフレビューは調査・機械検証を補助できても独立レビューの代替にしない。`--no-review`、profile のレビュー省略設定(`features.external_review: false`)は互換入力として受け付けるが無効化して理由を報告する。通常の `--light` 単独では影響範囲調査を省略できるが、`--compact` の実行では `--light` と profile の影響調査省略設定を無効化し、通常形式へ戻っても影響範囲調査を必須にする。独立レビュアーまたは解決表が使えない場合はレビュー未完了を報告し、完了承認・後続公開へ進めない。→ team-lead が各指摘を実コードで裏取りし valid / invalid / needs-user にトリアージ。指摘を盲信して自動反映しない。false positive は理由を記録
10. **レビューループのセーフティ**: **収束条件は全 reviewer の APPROVED**。コストを理由にレビュー反復を打ち切らない。同一指摘の 2 回連続残存・5 ラウンド超え・`--max-review` / `--max-iter` 到達は**停止ではなく判断を仰ぐ点**であり、状況を報告してユーザーに判断を求める。**無人モード(§2 の無人ループ)では、判断を仰ぐ点に達したら `保留_` にして人に返す** — 判断を仰ぐ手段が `保留_` になるだけで、未収束のまま PR を出さず、無人で続行もしない(基準は下げない)。**例外: `完了_` への改名後の停止は失敗扱い**(update-doc の判断を仰ぐ点など。完了した実装を `保留_` と呼び直さず、未承認の doc を commit しない。§2)。イテレーションごとの壁時計の上限を超えたイテレーションは、`loop.sh` が打ち切り、§2 のとおり worktree を残して以後拾わない([決定録 2026-09-23](minutes/2026-09-23_壁打ち_ループエンジニアリング.md))
11. **実コード優先の原則**: メモリ・ドキュメントと実コードが矛盾したら実コードを信じ、矛盾を必ず注記する。裏取りなしの推測でドキュメントを書かない
12. **機密保護**: `secret_paths` のファイルは読まない(**外部 implementer には作業ツリーごと見えるためこの保証は及ばず、明示承認で扱う**。契約は `external-runners.md` §12-4)。存在の有無だけ報告。サブエージェントのログへの混入も検査対象
13. **読み取り専用エージェントの原則**: 調査・レビューは読み取り専用の種別、編集・コマンド実行は編集可能な種別に集約する(種別名は解決表 軸 1 が正本)。**読み取りのみでも追加ツール(Web 取得等)やモデルの明示指定が要る委託は編集可能な種別を使い、読み取り専用は指示で守らせる**(種別選択の決定順は解決表の軸 1、要請は軸 3・軸 9)
14. **ログ永続化**: レビュー・検証の記録は `.claude/reviews/{role}-{タスク名}-iter{N}.md` に保存(タスク文脈を持たない skill は `{skill名}-iter{N}.md`)。**別のセッション・別の worktree の工程が判定に使う結果・印は、タスク MD(追加修正記録・ヘッダ)に残す** — create-task の設計レビューの行(ship-task の Phase 2 が読む)と、do-task の基準コミット行・追加修正記録(再開判定が読む)。無人モードでは、ship-task が止まった理由を**保留の行**(1 行の定型。「記録あり」に数えない)で追加修正記録に残す。発見ループの指摘キーと見送りの印は、`候補_` のヘッダ(採用の後は `進行中_` などのヘッダ)に残す(既知の判定が全状態のヘッダを読むため)。設計レビューの行の `本文` は承認時の本文ダイジェスト(算出は `create-task/scripts/task-digest.py`。定義の正本は [`create-task/references/task-template.md`](../plugins/dev-workflow/skills/create-task/references/task-template.md) の記法の規約)で、行と設計の中身を結び付ける。`.claude/reviews/` のログはキャッシュで、在れば使う(§5-18 ①)。例外として、base-commit.md の新規着手の条件 ③ は、ログが有るときだけ安全な側(『基準不明』)と判定する(無いときの判定は同じ。理由は base-commit.md)。do-task の ITER の採番も例外で、ログが有ればその番号の最大値+1 から始める(前の走行のログと名前が重ならないようにするため。無いときは 1 から。ログが有ると `--max-iter` の判断を仰ぐ点が早く来る向きになる。定義は do-task の Phase 0 の手順 5)
15. **日本語運用**: skill 本文・報告・生成ドキュメントは日本語。コード・識別子・コミットメッセージ規約はプロジェクトに従う。人が読む文の書き方(長さ・構成・言葉の選び方・口調)は §5-25
16. **図の標準**: doc 内の図は Mermaid を第一標準とする(GitHub/GitLab ネイティブ描画・diff 可能・ビルド不要)。図種別→記法→置き場所とスタイル(方向統一・日本語ラベル・subgraph・1 図 1 関心事)は doc/README.md テンプレの図規約表に従う。ワイヤーフレームのみ ASCII コードブロック(ツール不要)または Figma リンク。D2 / PlantUML などレンダラー必須のツールを既定にしない。コードが正本の図(ER・画面遷移)は update-doc が同期する
17. **実行環境のやり取りの範囲の判定と縮退プロトコル**: サブエージェント関連機能は環境により使える範囲が違う。skill は**着手時にツールの実在でやり取りの範囲を判定し**(推測しない・ユーザーに聞かない。判定手段は解決表 軸 8 が正本)、判定結果を報告に明記する。**使えるのに下の段階で回さない**:
    - **双方向でやり取りできる環境**: 委託は双方向。**起動時に `name` を付け**(`implementer` / `reviewer-strong` 等)、次を積極的に使う:
      1. **完了後の追加依頼**: 差し戻し・再レビューは**同じ name への再依頼(軸 5)**。エージェントの文脈が保たれるため、再スポーン(文脈ゼロからの再調査)より速く・ぶれない。**これが双方向でやり取りできる環境の最大の利得なので、レビュー反復では必ずこちらを使う**
      2. **走行中の STATUS 問い合わせ**: **外部 implementer(下の項)を除き**、待機目安(implementer 15 分 / reviewer 20 分。**reviewer の目安は外部ランナーの既定の最大所要時間〈プローブ + 本実行 + ヘルプ照合 + 終了猶予、約 12 分〉より長く保つ** —— 短いと正常に走っている外部レビューを二重起動する)を超えたら生存を確認し(軸 7)、状態を尋ねる(軸 6)
      3. **走行中の前提是正・中止**: 委託後に前提の誤り(対象の取り違え・スコープ解釈違い)に気づいたら完了を待たずに訂正する。不要になった委託は中止を伝える(黙って結果を捨てない)
      - 送る相手が失われている(軸 7 の生存確認に無く、軸 6 の送達が失敗する)ときだけ、一方向だけの環境と同じ再スポーンにフォールバックする(読み取りのみの役割の M2 は例外 — review-protocol.md)
      - **レビュアー同士を会話させない**。独立性が失われると多モデルレビューの多様性が意味を失う。指摘の突合は必ず team-lead が行う
    - **一方向だけの環境**: 委託は「起動 → 最終レポート」の一方向。差し戻し・追加指示は**前回成果物のパスを含めた新規起動**で代替する。応答しない委託先への STATUS 問い合わせ(M1)は省略し、待機目安を超えたら再スポーン(M2)に直行する(内蔵 implementer は、中止を伝える手段〈軸 6〉が無く走っているものを止められないので、再スポーンせず M4 へ。review-protocol.md の M2)
    - **別の AI を使えない環境**(サブエージェント機構なし): 調査・機械検証は実行者自身が直列に行えるが、独立レビューの代替にはならない。独立レビュアーを解決できないためレビュー未完了として報告し、完了承認・後続公開へ進めない
    - **外部レビュアー(③。`--runners` / `features.runners` の宣言時)は 3 つの環境と直交する任意の追加**(§7。② MCP の扱いは解決表 §4・§5)。やり取りの範囲の判定はあくまで**ホスト内蔵のサブエージェント機構**の話であり、外部 CLI レビュアーは `--runners` / `features.runners` が宣言されたときだけ編成に加える。**内蔵レビュアーを全滅させない** — `reviewer-internal` は常に維持する。外部ランナーは会話継続できないため、valid 修正後の再レビューは毎回新規起動し、起動した全レビュアーの APPROVED を得る
    - **外部 implementer(`features.implementer` 宣言時)は軸 6(走行中の STATUS 問い合わせ・前提是正・中止)が成立しない**(送達手段が無いため。待機目安 15 分の STATUS 問い合わせはホスト内蔵のサブエージェントが前提)。**差し戻しは毎回新規起動**になる(外部は会話継続できないため)。**ただし待機目安 15 分を超えても外部 implementer を再起動しない**(M1 を飛ばして M2 の再スポーンへ直行しない) — 正常に走行中の外部プロセスと新規起動が同じ作業ツリーへ同時に書き込むためで、**終了・タイムアウトによって外部プロセスが停止した後に、起動前後の比較と引き継ぎ条件の判定(`external-runners.md` §12-3・§12-7)を経て内蔵 implementer が引き継ぐ**(本実行の既定タイムアウトは待機目安より長い。値は同 §12-6)。外部 implementer の宣言時は、双方向でやり取りできる環境でも、implementer の持ち場だけが一方向だけの環境と同じ扱い(一方向委託)になる。判定・終了コード・機密ガードの契約は `external-runners.md` §12 が正本(**軸 6 が成り立たないときに段階を下げて続ける扱いの正本はここ**であり、解決表・契約側は参照だけを持つ)
18. **キャッシュの規律**: skill が `.claude/` 配下に置く状態ファイル(把握キャッシュ `grasp.md`・レビューログ等)は揮発性キャッシュであり、次の 3 条件を必ず満たす: ①無くても全 skill の動作が同一(再計算のコストがかかるだけで、依存を作らない。例外: §5-14 — base-commit.md の新規着手の条件 ③ と do-task の ITER の採番)②知識の正本(doc/ / メモリ / 権威参照ファイル)に無い情報を溜めない(把握中の発見は正本への反映を促す)③gitignore 対象(共有しない)。`grasp.md` は前回要約と参照索引だけに使い、HEAD・過去レベル・累積記録を理由に現在の規約・profile・関連文書・設定・対象・依存先の読取りを省略しない。毎工程で関連領域の一次情報を読み、全リポジトリ通読は求めない。「人間・他ツールが読むべき知識は doc/、Claude Code の動作状態は .claude/」の区分を崩さない(**ホストが自動で読む指示ファイル `.claude/rules/` は状態ファイルではなく commit して共有する** — §7-4。リポジトリの外の置き場は下の段落)
    - **リポジトリの外の置き場**: 外部 implementer の保護領域と出力置き場(`external-runners.md` §12-2。外部 implementer の書き込み範囲の外に置く)と、`loop.sh` の状態ディレクトリ(`loop.md`。人のチェックアウトの外に置く)は、doc/ と `.claude/` に続く第 3 の置き場で、上の 3 条件の対象外である(揮発キャッシュではない。失うと比較・停止・報告ができない)。場所・権限・保持・削除の正本はそれぞれの書で、ここにパスや規則をコピーしない。`loop.sh` のイテレーションの worktree は、人のチェックアウトから隔てた使い捨てのチェックアウト([決定録 2026-09-23](minutes/2026-09-23_壁打ち_ループエンジニアリング.md) の決定 6。§2 の「隔離」)で、この置き場には入れない
    - do-task / ship-task の開始前の候補走査は、この保護領域の存在を人へ知らせる停止専用の入口。レビュー用キャッシュの有無では省略しない。2 つの確認値(一覧確認値・実体確認値)は会話内だけで保持し、保護記録の保持hashや新しい永続台帳の代わりにしない。記録の外の所属の印は take が書き cleanup が消す、パスと番号だけの印で、所属を証明する台帳ではない。手動工程で一覧が変わった場合の人の再確認も `external-runners.md` §12-9 に従う。
19. **チェックの 3 階層**: ① 静的検査(format / lint / typecheck)② 自動テスト ③ **実動確認**(実際に動かして変更フローを観察する)。①②はタスクに依存しない定型実行で /tool-check が担う。③はタスク種別に依存するため、create-task(完了条件を実行可能な確認手順として書く+task-types.md の実動確認列)と do-task(Phase 5.5)が担う。③の状態は「実施済 / 実施不能(確認を実行できない、またはユーザーが確認しないと答えた。理由付き)/ 結果待ち(ユーザーの対応を待っている)」の 3 つで、状態ごとの /do-task の完了可否と /ship-task の PR 可否は do-task の Phase 5.5 の表が正本(確認を黙って飛ばさない)
20. **commit・push・PR の受け持ちの規律**: ブランチ作成・commit・push・PR 作成を能動的に行うのは `/ship-task` のみ(`/do-task` はユーザーが `--branch` 等で明示指定したときだけブランチを作る。コミットは従来どおり求められた場合のみ。**求められていないのに `/do-task` が index に触れるのは、Phase 7 で追跡済みのタスク MD を `git mv` で改名するときのリネームの stage と、中断のときにユーザーの合意を得て追跡済みのタスク MD を `保留_` へ `git mv` するときのリネームの stage の 2 つだけ**(未追跡のタスク MD は `mv` で改名し index に触れない。どちらも commit はしない)。**`/create-task` も、採用の経路で追跡済みの `候補_{名}` を `進行中_{名}` に `git mv` するときだけ index に触れる**(stage だけ。未追跡なら `mv`。commit はしない)。**外部 implementer にはこの規律が及ばない** — 外部委託は規約の外にいる実行主体を生み、機構では git 操作を止められないため、委託プロンプトでの禁止と実行後の検証〈`external-runners.md` §12-3〉で扱う)。**取り込み先(PR の base)への merge は決して行わない**(`/ship-task` の Phase 4.5 で、取り込み先を作業ブランチへ merge するのは除く)。PR は品質ゲートが緑・レビューが全員 APPROVED・実動確認が「実施済」か「実施不能」(§5-19)のときだけ開き、そうでないときはブランチと commit を残して停止する。対話で PR を開くときは、その前に取り込み先の最新を作業ブランチへ取り込み、取り込んだ状態で品質ゲートを流し直す(Phase 4.5。手順の正本は [integration.md](../plugins/dev-workflow/skills/ship-task/references/integration.md))。衝突したときは、衝突を解いてレビューし直す。衝突しなくても、取り込むとレビューした差分が変わるときはレビューし直す。取り込みの後の品質ゲートが赤・レビューが未収束なら、PR を開かずに停止する。無人のイテレーション・発見のイテレーション・`--no-pr`・parent-child などでは取り込まない(行わないときの全体は integration.md §2)。**例外: 発見のイテレーションの PR は、discover-mode.md の照合にすべて通ったときだけ開く**(commit の集合は task_dir の新しい `候補_*.md` だけ。実装を含まない候補の PR なので、品質ゲート・レビュー・実動確認は採用の後の /create-task・/do-task で行う)。commit メッセージ規約は `git log` から推定してプロジェクトに合わせ、実装と doc は別 commit に分ける。**無人ループの `loop.sh`(§2)は、ブランチ作成・commit・push・PR を行わない**。git への書き込みはイテレーションごとの使い捨て worktree の作成・削除・lock・unlock だけで、ブランチ作成・commit・push・PR はイテレーションの中の ship-task が行う(無人モードで止まったときは、保留なら保留の手順の 1 commit〈`保留_` への改名と保留の行〉だけを作り、失敗扱いなら、止まった時点から commit しない(それまでの commit は残る)。対話の停止で実装 commit だけ残す動作は、無人では行わない。§2)
21. **チェーン実行時の責務分界**: 複数 skill を連鎖させる skill(/ship-task)は**工程の中身を再定義しない**。**例外**: /ship-task は、外部 implementer の承認を Phase 0 で先取りする([決定録 2026-09-17](minutes/2026-09-17_壁打ち_implementerの外部委託.md) の決定 10。既定は走り切るので、工程の途中で承認待ちにしない)。承認の規則は /do-task と `external-runners.md` §12 のままで、得られなくても内蔵で走り切り、無人では求めない。順序・工程間の続行判定・commit・push・PR だけを持ち、各工程の手順・品質基準は元の skill に委ねる。**もう 1 つの例外**: Phase 4.5(取り込み・衝突の解決・レビューし直し・権威参照ファイルの「PR の前の手順」と、それらを内蔵の implementer に任せること)は、/ship-task が自分の工程として行う(手順の正本は ship-task の `references/integration.md`。レビューと照合の CLI は /do-task の `review-protocol.md` のまま)。連鎖元から呼ばれた skill は、ユーザーへの次アクション提案(チェーン提案)を出さない(呼び出し元が判断するため)。無人モードでは、/ship-task が /do-task・/update-doc に `--unattended` を透過し、/ship-task が呼ぶ skill は止まるときに理由を保留と失敗扱いに分けて返す(改名・commit はしない)。発見のイテレーションでは、発見元の候補モード(`/data-audit --candidates`・`/create-task --refactor --candidates`)が結果の行(`候補モードの結果:`)を返し、改名・commit・チェーンの提案をしない
22. **リポジトリ自身の Issue 運用**: このリポジトリは `.claude/project-profile.yml` の `task_store: github-issue` を選ぶ。設定はこの1項目だけとし、ほかの項目の既定は変えない。手順の正本は [`github-issue.md`](../plugins/dev-workflow/skills/do-task/references/github-issue.md)。ローカルのタスク成果物は新設しない。配布スキルの既定はファイル方式のままにする。
    - 現在の版は `github-issue.md` §6・§7 を使う。`task_store: github-issue` では、template の完了ステータス行と do-task の完了記録が引き続き必須。過去の記録に不足があれば自動で完了とせず、不足を示して止める。
    - 開いた Issue も、ヘッダに `> **ステータス**: 🚧 進行中` の行が1行必要。無ければ ship-task は実装前に停止する。do-task を単独で完了しても、その後の update-doc が停止する。
    - 行が無ければ、コードフェンスの外の最初の `## ` より前に、上の1行を行頭から追加する。[`task-digest.py`](../plugins/dev-workflow/skills/create-task/scripts/task-digest.py) は、この位置のステータス行を本文ダイジェストから除く。既存の設計レビュー行と値が一致していた本文に、この1行だけを追加しても値は変わらず、設計レビュー行はそのまま使える。Issue 本文の再取得と承認は `github-issue.md` に従って取り直す。
    - `AGENTS.md` には、現在の版への切り替えの案内も書く。設定を読まない古い版向けには、Issue 本文を正本とすること・ローカルのタスク成果物を作らないこと・完了記録の形を残す。`AGENTS.md` が定める形は、`- <YYYY-MM-DD> ` で始まり、「未完了」「中断」「停止」を含まず、「完了」と「結果 APPROVED」を含む行。例: `- 2026-11-04 完了(反復 5 回): 結果 APPROVED`。この形は権威参照ファイルの経路で使い、現在の profile の経路へ例外を広げない。
    - ほかのプロジェクト向けの権威参照ファイルによる Issue 運用は残す。ship-task・update-doc の `--unattended` は、権威参照ファイルによる Issue 運用でも停止する。無人ループは本リポジトリの `task_store` を読んで起動時に停止する。
23. **独立レビューの適用範囲**: プロジェクトのコード・設定・正本文書を変更する経路に適用する。調査だけ・機械検査だけ・会話出力、派生資料を出す export-doc と決定録を作る discuss-spec には適用しない。発見元の候補モードが書く `候補_` も対象外(設計の手前の候補で、採用のときに create-task の checker と設計レビューが必ず走る。team-lead の裏取りは省かない)
24. **`scripts/validate.py`(規約どおりかを検査するスクリプト)の必要環境・免除と可変語**:
    - **YAML検証の必要環境**: frontmatterの構文検査には、[requirements-dev.txt](../requirements-dev.txt) のPyYAMLを使う。同じファイルの markdown-it-py(と、それが依存する mdurl)は、§6 の言葉の機械検査が Markdown を解釈するのに使い、使えなければ同じく導入案内付きの ERROR にする。未導入・読み込みの失敗・`safe_load`が呼び出せない場合は、起動時にERRORと導入案内を1回出して終了コード1で停止する。正規表現による簡易読み取りへ切り替えない。依存が使える場合のYAML構文エラーも不合格とする。準備と検証コマンドの正本は [AGENTS.mdの検証節](../AGENTS.md#検証実装終了時に必ず実行)。配布skillが実行時に使う任意のYAML読取は、この開発依存契約に含めない。
    - **免除は明示マーカーだけ**: 禁止パターン検査の行単位の免除は、行内の `<!-- validate-allow: 理由 -->` に限る(**理由の記述が必須**)。行単位で効き、コードフェンスの内外は問わない。**効くのは禁止パターン検査とホスト CLI 語検査(§7-7-1)の 2 つ**で、委託の語(§7-7)・解決表の除外の不変条件は免除規則を持たない —— **役割語へ書き換えて消せる語には免除を設けない**という線引きで、MCP サーバ名のように**生成する設定の識別子そのもので書き換えて消せない**語があるカテゴリにだけ免除を置く(**免除を持たない側の語**はマーカーでは消えず、役割語へ書き換えて消す)。以前は「禁止 / 使わない / しない / 廃止 / ではなく」を含む行を一律に免除していたが、普通の文に「〜しない」が入っているだけで混入が見逃されていた
    - **周辺プロジェクト名は環境変数で渡す**: `WORKFLOW_PROJECT_NAMES` に名前の一覧(カンマ区切り)を渡したときだけ検査し、**未設定なら 1 語も足さない**。実行環境のディレクトリを列挙しないので、検査の結果は渡した値だけで決まり、同じリポジトリならどの PC でも再現する(固有名をこのリポジトリに書かないことを優先した決定。既定で保護が外れることは受け入れ、運用で渡す)。照合は**名前の端が英数字・`_` のときだけ境界を付ける**形で行う —— `\b` は日本語と `_` を単語文字として扱うため日本語直結を取りこぼし、逆に固定で両側へ境界を付けるとハイフンで終わる名前で境界の意味が反転して取りこぼす(§7-7-1 の `Agent` と文字集合が非対称な理由もそこにある)
25. **人が読む文の書き方**: 人が読む文(会話の応答・質問・PR と Issue の本文・作る文書・コミットメッセージ)は、短く・わかりやすく・共通の言葉で書く。**正本は [`do-task/references/writing-for-people.md`](../plugins/dev-workflow/skills/do-task/references/writing-for-people.md)**(わかりやすさの決まり 8 項目・言い換え表・字面を変えない行と語・口調の正本の決め方・質問と選択肢)。ここには要点だけを置く。決まりを変えるときは、正本を先に直す
    - **2 つの層**: わかりやすさ(結論が先・1 文に 1 つ・平易な言葉・共通の言い換え)は、人が読む文すべてに効く。口調(話し方の型。マーカー・文末など)は、人との会話の応答だけに当てる。完了報告のコピー用の枠には当てない
    - **対象と効かせ方**: 文の種類ごとに決める。Issue と PR の本文・作る文書には、わかりやすさの決まりをすべて効かせ、口調は当てない。コミットメッセージとタイトルは、プロジェクトの規約(言語・接頭辞・長さ)が先。AI への依頼文とその返答は、決まった書式を守る正確さだけ
    - **優先順位**: 決まった書式 > 正確さ > 簡潔さ > 口調。失敗と危険の知らせは、短くしても省かない
    - **番号の説明**: 人に出す報告・質問・選択肢・Issue と PR の本文で、番号が理由・判定・指摘そのものになるときは「番号: 中身」で一言添える。参照先だけを示す番号と、手順の本文・相互参照の番号は残してよい
    - **字面を変えない行と語**: 別の工程が字面で読む行(結果の行・ヘッダの行・見出し・チェックボックスの行など)は、行全体の字面を守り、同じ行に何も足さない(マーカーも付けない)。決まった語(判定の語・結末の値・状態名の頭・要件タグ・指摘 JSON の鍵と値)は、文の中でも言い換えずに使う。説明は添えてよい(言い換え表に載る語には、表の説明を添える)
    - **口調の正本の決め方**: 権威参照ファイルの「応答の書き方」節の口調の記述 → その節が「口調は「<ファイル>」の「<見出し>」の節に従う」の行で指す節 → 口調を決めた別の節、の順に見て、最初に決まったものに従う。どれでも決まらなければ、skill は口調を決めない(ほかの指示に口調の決まりがあればそれに従い、無ければ、です・ます)。口調の型(標準・報告調)は、init-project が「応答の書き方」節に書き込む。報告調の印(マーカー)は、隅付き括弧で囲む形(`【告】` など。句点を付けない・太字にしない)。口調を決めた別の節を見つける手がかりの正規表現は、句点で終わる旧い形の印にも一致させる(「応答の書き方」節の外に、人が旧い形で書いた節を見落とさないため。[Issue #382](https://github.com/Yuki-Maeda-valour/workflow/issues/382))
    - 各 SKILL.md の `## 原則` に、正本を指す 1 行を置く(§6)
    - **完了報告の型**: 正本の「8. 完了報告の型」に従い、区切り線と見出しの行・まとめ・7 項目のコピー用の枠・必要な依頼の順に書く。枠は 10 行前後とし、段落の印を付けない。書式が決まった最後の行は枠の外の最後に保つ。ship-task の中で呼ぶ skill は枠を出さず、最後に ship-task が 1 つだけ出す。確認の内訳は PR 本文などに残す。ほかの skill は報告手順で型を参照するか、使わない理由を書く([Issue #319](https://github.com/Yuki-Maeda-valour/workflow/issues/319))
      - 区切り線と見出しの行: 報告の先頭に、区切り線 `---` の行と、見出し `## 【<結果>】<対象>` の行を置く。`<結果>` は枠の「完了」欄と同じ値、`<対象>` は枠の 1 行目の対象と同じ。区切り線の前は空行にする(応答の先頭なら要らない。前の行が文だと、Markdown がその文を見出しにする)。見出しの `【】` は結果を示す字で、口調の印ではない。見出しは報告に 1 回だけ置き、ship-task の中で呼ぶ skill は出さない。型を使わない skill の報告と、途中の質問・進捗の報告には付けない。作業全体を終えたことを、ふつうの段落と見分けやすくするため([Issue #382](https://github.com/Yuki-Maeda-valour/workflow/issues/382))
      - 同じ決まりを、`AGENTS.md` と init-project の `AGENTS.md` の雛形の「応答の書き方」節にも 1 行で置く(skill を使わない会話にも効かせるため)。8 節の見出しの決まりを変えるときは、この 2 つも直す
    - **ファイルへのリンク**: 人との会話の応答でファイルを示すときは、人が開けるリンクを添える(正本の「9. ファイルへのリンク」)。リンクの文字はパスの字面のまま書く。字面を変えない行・完了報告のコピー用の枠・Issue と PR の本文・作る文書には足さない。ホストごとの形は解決表 §9 に、その事実は §7-3 に置き、skill 本文にはホストのリンクの書き方を書かない([Issue #335](https://github.com/Yuki-Maeda-valour/workflow/issues/335))
    - **メモリの呼び分け**: AI が自分で書き残す記憶は「自動メモリ」、プロジェクトの知識として残すもの(Serena のメモリなど)は「プロジェクトのメモリ」と書く。人に出す文では、どちらか分からない「メモリ」「メモ」だけを書かない(正本の言い換え表)。文書の中の「メモリ」は、意味が 1 つに決まっている所は変えず、どちらとも読める所だけを書き分ける。呼び方はホストの日本語の文書に合わせた(§7-3)。利用者が選んだ([Issue #335](https://github.com/Yuki-Maeda-valour/workflow/issues/335))
    - 決定記録: [Issue #137](https://github.com/Yuki-Maeda-valour/workflow/issues/137)
26. **worktree と作業用コンテナの扱い**: 1 つのエージェントの作業はリポジトリ本体で行う。worktree は、複数のエージェントが同じリポジトリを並行して触るときだけ作る(「作らない」にはしない。並行作業には隔離が要る)
    - **置き場所**: `<repo>/.claude/worktrees/<名前>` の 1 系統にそろえる。リポジトリの隣や `/tmp` に作らない
    - **片付け**: PR がマージされた worktree は、作った側が片付けるか、片付けの案内を出す。片付けは `git worktree remove <パス>`(`--force` なし)。`--force` なしでも、無視されたファイル(`.env`・ローカル DB・ビルド成果物)は消えるので、片付ける前に有無を確かめ、中を作業ディレクトリにしているプロセスがあれば消さない。案内にも同じ確かめを添え、確かめを含めて棚卸しするのが `/cleanup` である。ローカルのブランチもリモートのブランチも、ここでは消さない。worktree を片付けるときは、その中を作業ディレクトリにしているプロセスを先に報告する
    - **置き場所の無視**: `.claude/worktrees/` は git が無視する(`.gitignore` か `.git/info/exclude`)。作る側は、作る前に `git check-ignore` で確かめる。無視されていなければ、対話では作業を始める前(基準コミットを控える前)に `.git/info/exclude` に足すか人に確かめる。無人では書かず、worktree を作らずリポジトリ本体で作業する(無人ループがイテレーションの中の `info/exclude` の変化を許さないため)。`/init-project` の `.gitignore` の整備の対象に含める
    - **例外(この方針の対象外)**: 機構が作って自動で消す使い捨てのツリー。無人ループのイテレーションの worktree(既定の置き場は `<対象の親>/<名前>.loop`。理由は [loop.md §3](../plugins/dev-workflow/skills/ship-task/references/loop.md))と、外部レビューの一時ツリー([external-runners.md §9-1](../plugins/dev-workflow/skills/do-task/references/external-runners.md))。既存の記述は変えない
    - **作業用コンテナ**: 現在、skill がコンテナを起こす記述は無い。今後 skill がコンテナを起こすときは、`--rm` を基本にし、残す必要があるときは `--label dev-workflow.task=<タスク>` を付けて持ち主が分かるようにする
    - **導線**: ship-task の完了報告が、このタスクで作った worktree の片付けの案内を出す。まとめて棚卸しして消すのは `/cleanup`(読み取りで集める → 「削除してよい / 確認が必要 / 残す」に分類 → 承認 → `git worktree remove`。使用中のものと、ほかにコピーの無いものは個別に確認)
    - 決定記録: [Issue #284](https://github.com/Yuki-Maeda-valour/workflow/issues/284)

### review と commit・検証根拠の固定

[review-guard.py](../plugins/dev-workflow/skills/ship-task/scripts/review-guard.py) は実装開始時の task_dir・未追跡・ignore・品質入口を控え、
review 前後の対象集合と実入力を hash で結び付ける。検証担当が state hash を外部に保持し、
機械照合で index/blob/commit tree を比べる。詳細な CLI・順序・mode は
[review-protocol.md](../plugins/dev-workflow/skills/do-task/references/review-protocol.md#reviewcommit-照合)が正本。

最終 task は元本文と直接検証結果から作る期待バイトだけを許し、他の状態名 MD を保護する。
source、doc、未承認保留は別の phase。文書は実装 commit 後に別 start を取り、改名なしの commit を検証する。
開始前の無関係な未追跡を stage 集合へ取り込まない。ignore の全有効入力と ignored path の変化、
品質定義・入口・テスト・検証のスクリプトの変更を review と PR に開示し、独立した clean checkout で実検証する。
品質入口の静的検出は動的依存の網羅を保証しない。

`/ship-task` の Phase 4.5 で取り込んだ後は、doc と同じ経路で 1 つの commit を照合する([Issue #373](https://github.com/Yuki-Maeda-valour/workflow/issues/373))。
取り込みの前に、最後の commit の state で `verify --mode commit` と `pr-evidence` を行い、出力を保持する(取り込みの commit が載ると、この state の照合は通らなくなるため)。
取り込みの後に state が要るとき(取り込みの commit がある・権威参照ファイルの「PR の前の手順」の節がファイルを変えるか検査のコマンドを持つ・新しい機密の glob が足された・節の履歴を読む確かめがレビューを求めた)は、
HEAD(取り込みの commit か、レビュー済みの最後の commit)で別 start を取り、`take --phase doc`→snapshot→`seal`→(要るときは review)→`run-checks` を行う。
review をしたときは `attest`→stage→commit とし、doc と同じ mode で検証する。
この commit(取り込みの後の review の commit。変わったファイルが無ければ空の commit)は、state の HEAD(取り込みの commit か、レビュー済みの最後の commit)の上の 1 つなので、親がちょうど 1 つの commit を求める照合のまま通る。
差分が変わらない取り込みの commit は、`integrate-guard.py check` が、取り込み先から見た作業の差分がレビューした差分と同じかを照らす。
push の直前の照合は、取り込みの後の review の commit があればその state で `verify --mode commit`、
無く Phase 4.5 の state があれば `integrate-guard.py worktree` とその state の `verify --mode pre-stage`、
どちらも無ければ最後の commit の state で `verify --mode commit`。
正本は [integration.md](../plugins/dev-workflow/skills/ship-task/references/integration.md) §7。

品質コマンドは、起動した処理の停止を確認してから次へ進み、検証用コピーを削除する。
時間切れは計画を打ち切る。中断や停止確認の失敗ではコピーを保持する([Issue #220](https://github.com/Yuki-Maeda-valour/workflow/issues/220))。
落ちたコマンド(終了値が 0 以外か時間切れ)の出力は、末尾 1 MiB を、8 バイト以上の環境変数の値と検証用コピーの場所を決まった字に置き換えてから、結果の JSON の隣に残す([Issue #368](https://github.com/Yuki-Maeda-valour/workflow/issues/368))。
停止・回復手順は [review-protocol.md](../plugins/dev-workflow/skills/do-task/references/review-protocol.md#品質コマンドの停止とコピーの保持)、
Linux と他 POSIX の必要機構・保証範囲は [runtime-requirements.md](../plugins/dev-workflow/skills/do-task/references/runtime-requirements.md#品質コマンドの監督)を正本とする。
他 POSIX の不在確認で一時的な `EPERM` が返っても、不在とは扱わない。親の回収後は signal 0 だけで既存の 5 秒の期限内に再確認し、`ESRCH` の確認なしにコピーを削除しない。
macOS で、品質コマンドの実行中に時間切れになったとき、または検証の親(品質コマンドを監督する `run-checks` の処理自身)が `TERM`・`INT`・`HUP` を受けたとき、プロセスグループ(信号をまとめて送れるプロセスの集まり)の全員が停止の `TERM` で終わっても、時間切れ・中断として報告する([Issue #289](https://github.com/Yuki-Maeda-valour/workflow/issues/289) で直した。それまでは回収の失敗〈`Operation not permitted`〉で止まっていた)。検証の親が、プロセスグループの停止を確かめるまで、anchor(品質コマンドを起動し、プロセスグループの停止まで残る補助プロセス)の通知(anchor が品質コマンドの終了値を書いて知らせるパイプ)の読み口(読む側)を閉じない。残る限界は 3 つ(anchor が失われた後にプロセスグループの全員が `TERM` で終わる場合・anchor の起動中の停止の信号・`TERM` から `SIGKILL` までの間の検証の親の強制終了)。中身と、残った処理の見つけ方・止め方は `runtime-requirements.md` の「品質コマンドの監督」、保持したコピーの片付けは `review-protocol.md` の「品質コマンドの停止とコピーの保持」にある。
検証用コピーを作る `git commit` には `-c maintenance.auto=false -c gc.auto=0` を渡し、Git の裏の保守処理(`git maintenance run --auto`。親から離れて動き、1 つずつのファイルで置かれたオブジェクトをパック〈まとめたファイル〉へ詰め直す)を起動させない([Issue #292](https://github.com/Yuki-Maeda-valour/workflow/issues/292) で直した。それまでは、保守処理の書き込みが検証用コピーの削除と重なり、`Directory not empty` で止まることがあった。Mac の Git 2.54 で確かめた。Linux の Git 2.51・2.34 でも、レビューした中身のオブジェクトが多いと起きうる)。設定はその 1 回の `git commit` にだけ効き、品質コマンドの環境と検証用コピーの `.git/config` は変えない。品質コマンドが自分で実行する `git commit` の保守処理は、Linux 以外の POSIX では保証外(Linux の監督は子孫として止める見込みで、未確認)。Git 2.29 より前の Git は未確認(`gc.auto=0` も渡すが、動きは確かめていない。中身は `runtime-requirements.md` の「品質コマンドの監督」)。

PR の検証欄は直接実行したコマンドの構造化結果と reviewer の直接返答から生成する。
task/Issue 記録中の APPROVED は根拠にしない。外部本文を正本とする構成では、trusted caller が fresh な本文の控えと
保持 hash、任意の保護する task_dir を渡す外部本文入口を使う。task_dir が無ければ省略し、ローカルのタスク一覧は空として扱う。指定した場合の保護とファイル方式の保護は維持する。既定の task MD 方式を変えず、外部サービスの読書きは caller が担う。
同一 UID の完全隔離や、検査間だけ変えて戻す競合の排除は主張しない。実装者停止と工程ごとの再照合が前提となる。

### 無人ループの信頼境界

無人入口は loop の有無によらず、起動時の配布物全体と利用者環境を控え、毎回新しいコピーを作る。
ホストの HOME と設定ディレクトリは、設定本文を読む前に起動条件を検査する。利用者設定・Linux 管理設定・remote 設定を控え、最初の補助ホスト CLI より前に、共通 reader が照合した bytes だけを解析する。設定の `env` による読み込み先・hook の動作の後置き変更と不確定な `policyHelper` の入口を拒否する。将来の正規 server 応答は管理者の信頼範囲に残す。対象・正常なリンク・停止理由・実測範囲は [loop.md §9・§10](../plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み)。
環境照合器と控えの SHA-256 は呼出元が保持し、helper・文書の使用直前とイテレーションの判定前・network Git 前に照合する。
有効 plugin の一覧を確定した後の 1 回の照合は、環境照合器を 1 回だけ起動し、補助ホスト CLI の前と後に照合する。2 回目の照合の後に環境照合器の本体を保持 hash で照らし直す(#391。詳細は loop.md §4)。
過去の状態領域のコードを実行せず、コピーの後に元 helper を import/source しない。
inflight と同じ領域の checksum は再開承認に使わず、所有を確認できない再開では signal・削除・unlock・ref 更新を行わない。
permlog は参考表示だけとし、G1 は全種類を通常失敗と G1 連続の双方へ数え、厳しい方で止める。
監視集合・有限読取・直接入口(ship-task・do-task・update-doc)・正常な起動間更新・同じ UID の観測間復元/親の保持値への攻撃の限界は loop.md §4 と無人契約 §0 を正本とする。
  - 3つの直接入口は、信頼して読み込んだ SKILL 本文の固定 Python 検査を保持する。loop の親も同じ本文を保持する。親と末尾を nofollow で開き、1 MiB・15秒以内の通常ファイルから保持hashと一致したバイト列だけを実行する。検査前の参照文書読取・raw hash 読取・guard の直接起動へ置き換えない。
別 UID・OS 隔離・外部監査を組み合わせる。コピーと 0600 だけで隔離されたとは扱わない。

## 6. SKILL.md 執筆規約

- frontmatter: `name`(ディレクトリ名と一致・kebab-case)+ `description` は必須。ユーザーが入力する引数がある skill は `argument-hint` を付ける。skill の frontmatter に `allowed-tools`・`disallowed-tools` を付けない(呼んだ後のターンの残りに効き、`disallowed-tools` は呼び出し側を止め、`allowed-tools` は確認を飛ばす許可を足しうる — §7-3。ツールの制約は本文の原則で書く)
- **description は「何をするか+いつ使うか(トリガー語句)」を日本語で 150〜500 字(目安 350)**(**150 字未満・500 字超は `scripts/validate.py` が ERROR で落とす**。上限 1024 字は別の桁の歯止め)。Claude の自動呼び出し判断の材料になるため、ユーザーが言いそうな表現(「〜して」)を含める。上限 1,024 字・description+when_to_use 合計 1,536 字
- **SKILL.md は 500 行以下**。超える詳細は `references/*.md` に外出しし、本文から「いつ読むか」付きでリンクする(progressive disclosure)
- 実行可能な重い処理・決定的処理は `scripts/` に外出しして skill は薄いオーケストレーションに徹する(冪等〈何度実行しても同じ結果になる〉に作る)
- **共有アセット(複数 skill が使うスクリプト・references)は所有 skill の配下に置き、他 skill からは兄弟参照 `../<所有 skill>/...` で解決する**。プラグインルート(`plugins/dev-workflow/scripts/` 等)には置かない — setup.sh 経由の `.claude/skills` 配置でも `.agents/skills` 配置でもプラグインルートが存在せず、3 配布形態のうち 2 つで消えるため。プラグインルートを指す環境変数にも依存しない(ホスト依存になり §7 と両立しない)。兄弟参照は**全 skill が同じ親ディレクトリへ一括配置されていること**を前提とする。skill を 1 本だけ取り出す部分導入など解決できない構成では、**当該機能を無効化して報告する**(探索を広げず、その機能をあきらめる)
  - 例外: プラグインの hook(`plugins/dev-workflow/hooks/`。使う道具の版を確かめて知らせる。#431)は skill の資産ではなく、プラグインとして導入したときだけ動く部品なので、プラグインルートに置く。skill はこれに頼らない。`setup.sh` の導入では、hook の代わりに `setup.sh` が導入の最後に同じ確かめを流す
  - 例外: 共通の起動入口と Rust の配布物だけは `plugins/dev-workflow/bin/dev-workflow/` に置く。段階 0b(#443)から `setup.sh` も skills の兄弟の `bin/dev-workflow/` へ同じ版を一括配置するため、コピー・リンク・プラグインの導入で失われない。段階 1b(#450)の保存先解決は、使っている skill と同じ配布の入口を使う。リンク導入は物理的な配布元、コピー導入は配置先を基準にする。任意の共有スクリプトへこの例外を広げず、環境変数でプラグインの場所を指定しない。
- プロジェクト固有の事実(パス・コマンド・スタック名・メモリ名)を本文に書かない(§3 の 3 層で解決)
- 冒頭に「原則」、末尾に「最終ゲート」(出力・完了前セルフチェック)を置く
- 関連 skill への導線(前提 skill / 後続 skill)を必ず書く
- **委託は役割語で書き、ホスト機構への解決は references の解決表([`do-task/references/delegation-map.md`](../plugins/dev-workflow/skills/do-task/references/delegation-map.md))に集約する**(§5 前文・§7-5)。skill 本文にホスト固有の API 名・モデルエイリアスを書かない(**語彙・検査範囲・除外集合・移行の状況は §7-7 が正本**。新規混入は `scripts/validate.py` が ERROR で止める)。人に質問する道具の名前も書かない(対応づけは解決表 §8)。人への報告でファイルを示すときの、ホストのリンクの書き方も書かない(ホストごとの形は解決表 §9)
- **skill 本文を変更するレビューでは「ホスト結合の混入」(役割語ではなくホスト機構名で委託を書いていないか)を観点に含める**(全 skill が対象。§7-7 の許容リストは v4.0.0 で空になった)
- **各 SKILL.md の `## 原則` に、人が読む文の書き方の正本([`do-task/references/writing-for-people.md`](../plugins/dev-workflow/skills/do-task/references/writing-for-people.md))を指す 1 行を置く**(リンクの形で置く。`validate.py` の検査 9 が確かめる。§5-25)
- **skill の変更をレビューするときは、SKILL.md と references の報告の型と、その変更が書く文が writing-for-people.md に沿うかを観点に含める**(報告の型が下の「報告の型の言葉」に沿うか、内部の言葉が下の「skill 本文の内部の言葉」「置き換える語」「「縮退」」に沿うか、番号が理由・判定・指摘になる所に中身が添えてあるか、決まった行と語の字面が保たれているかを含む)
- **報告の型の言葉**: skill 本文と references が決める、人に出す報告の型(見出し・表の見出しの行・太字の項目名・ヘッダの項目名・報告の項目の並び・決まった質問の文・選択肢・`{…}` の中の「例:」「推奨:」の字面)の言葉は、次の 4 項目のとおりに書く。この決まりは、下の「skill 本文の内部の言葉」(27 語は字面を残す)より先に当たり、型の中の説明は「言い換えは、各 SKILL.md で語ごとに 1 か所だけ」に数えない
- 対象の言葉: 言い換え表(`do-task/references/writing-for-people.md` の 4 節)の内部の言葉のうち、下の表の 27 語と、決まった語 7 語(縮退・失敗扱い・保留・needs-user・APPROVED・未収束・未完了)・置き換える語 4 語(下の「置き換える語」の行の 4 語)・ふつうの日本語の語 3 語(結末・段階・候補)・ファイルや設定の名前 3 語(profile・task_dir・grasp)をあわせた 44 語と、その略(品質ゲートを略した「ゲート」)。言い換え表の意味で使う所だけに当てる。ふつうの意味で使う所と、言い換え表の 2 列目・下の表の言い換えの字面の中に出る語(縮退を言い換えた「段階を下げて」の「段階」など)は対象にしない。開発の一般の言葉(commit・push・merge・PR・ブランチ・worktree・diff・リポジトリ・テスト・lint・型チェック・CI・hook・selftest)と、言い換え表に無い言葉と、44 語の外で言い換え表に載る言葉も対象にしない
- 書き方: 対象の言葉は、言い換え表の 2 列目(括弧と「。」から後は除いてよい)か下の表の言い換えの字面に置き換えるか、語の直後に括弧でその字面を添える(言い換えの字面の後に括弧で語を置く形もよい)。表の見出しの行では、その字面の末尾の名詞に縮めてよい(品質ゲート →「検査」)。決まった語(4 節の表で決まった語とした語)は置き換えず、型の中で語の直後に括弧で説明を添える。5 節の (i) の行と、決まった語を値として書く所には何も足さない(説明が要るなら別の行に置く)
- 名前: ほかの所が名前で指す語句(見出し・項目の名前など)は、指す所がすべて同じファイル(その SKILL.md、またはその references の 1 本)の中にあるときは、言い換えた名前に変え、指す所も(型の外でも)同じ変更で直す。言い換えた名前は、指す中身を表す言い方にしてよく、同じ中身を指すほかの名前があれば、その言い方にそろえる。ほかのファイル(ほかの skill・references・design・テスト・スクリプト)が指す名前は字面を残し、型の中で名前の直後に括弧で説明を添える。ただし、下の「置き換える語」と「文書の中で置き換える語」の語を含む名前は、ほかのファイルが指していても改名し、指す所もすべて同じ変更で直す
- 対象にしない所: 型が中身を AI に任せる所(書く中身の説明の `{…}`・「該当項目」など)・選択肢の「→」の後と、選んだ後の既定の言い方(選んだ後に AI がすること)・項目を書く条件(太字の後ろの括弧。「(あれば)」など)・本文の手順の文・skill の手順の見出しと名札(質問の名前の太字を含む)。そこに AI が書く言葉は、実行時の決まり(4 節)で言い換えを添える
- **番号だけの呼び方**: 人に出す報告・質問・選択肢・Issue と PR の本文で、番号が理由・判定・指摘そのものになるときは「番号: 中身」で一言添える(例: `G3: 無人の前提を欠く`、`S4: 承認の後に本文が変わった`)。D・S・U・G・M・H の番号、決定 N、軸 N、①〜⑳・条件 ③、§N-N などに当てる。S・D・U・G の中身は、そのイテレーションで当たった理由を一言で書く。`ship-task/references/unattended-mode.md` §4 の表の行を目安にする。S4 なら、当たった続行判定の項目の名前から書く(例: `6 本文の照合` → `承認の後に本文が変わった`)。保留の行の停止条件と、結果の行の理由を番号で示すときも同じ形で書き、番号を先に置く。参照先を示すだけの番号(「正本は loop.md §4」「#107 の H48」)と、手順の本文・相互参照の番号は残してよい。`validate.py` の検査 12 は、skill の文書・本書・README の結果の行と保留の行の例に限り、理由が番号だけなら ERROR にする(コードフェンスとインラインのコードの中も含む。免除の印は使わない)。すべての番号に説明を求める検査は作らない。決定録と CHANGELOG の過去の版は変えない
- **skill 本文の内部の言葉**: 言い換え表(`do-task/references/writing-for-people.md` の 4 節)の内部の言葉のうち、次の 27 語は字面を残し、SKILL.md ごとに本文で初めて出す所に決まった言い換えを括弧で添える(言い換えの字面は下の表)。初めて出す所は、frontmatter・見出し・コードフェンス・インラインのコード(1 行の中の `` `…` ``。改行をまたがない)・リンクの先・HTML のコメント(`<!-- … -->`)・字面を残す名前の中を除いた本文で最初に出る所。英字の 5 語(team-lead・implementer・reviewer・checker・researcher)は、前後が英数字・`_`・`-` でない所だけを数え、インラインのコードがその語だけのとき(`` `reviewer` ``)はコードの中でも数える
- 選び方: 下の表の 27 語で固定する。決まった語(縮退・失敗扱い・保留・needs-user・APPROVED・未収束・未完了)・置き換える語・ふつうの日本語の語(結末・段階・候補)・ファイルや設定の名前(profile・task_dir・grasp)は含めない。言い換え表に語を足しても、27 語と 44 語は増やさない(足した語は、実行時に人に出す文で、言い換え表の 4 節のとおりに言い換える)
- 添え方: 語(と、続く漢字・カタカナ・英数字の連なり 8 字まで)の直後に「(」か「〈」を置き、言い換えの字面を続ける。語がインラインのコードのときは、閉じのバッククォートの直後に置く(例: `` `reviewer`(変更を確かめる AI) ``)。既にある括弧には、その頭に言い換えを入れてよい(例: 「裏取り(実際のコードやファイルを読んで確かめること。path:行)」)。言い換えの字面の後に「(語)」を置く形もよい(語はコードのままでよい)。言い換えは、各 SKILL.md で語ごとに 1 か所だけ
- **名前は字面のまま**: references・design が名前として決めた語句(見出し・規則の名前・手順の名前・終了コードや結末の名前)を指すときは、その字面のまま書く。「縮退」の言葉の分け方は名前の中には当てず、初めて出す所を数えるときもその中は数えない。下の「置き換える語」と「文書の中で置き換える語」の語は名前の中にも残さない — 名前のほうを改名し、指す所も同じ変更で直したうえで、改名した名前を字面のまま書く
- **置き換える語**: 守る値 →「控える値」・対話点 →「人に確かめる場面」(対話点番号 →「人に確かめる場面の番号」)・報告点 →「判断を仰ぐ点」・止めの印 →「止める印」。名前(見出し・太字の項目名・手順の名前)と、コマンドの字面の中のプレースホルダ(`<控える値>` など)も新しい語で書き、名前で指す所も同じ変更で直す。前の 4 語は skill の文書と本書では書かず、`validate.py` の検査 11 が ERROR にする(除外は、言い換え表 `writing-for-people.md` と、この行だけ。この行で除くのは、この 4 語だけ。免除の印は使わない)。利用者の依頼(#318)による追加で、4 語の数は変えない
- **文書の中で置き換える語**: 「枡」→「持ち場」(委託先 1 体ごとの受け持ち。主にレビュアー。implementer も 1 つの持ち場として数える)・「継承枠」→「継承の持ち場」(モデルを指定しない持ち場。ふつうはセッションのモデルで動く)。同じ意味の「枠」(明示指定枠・単一枠・追加枠など)も「持ち場」と書く。持ち場の名前 `internal` / `strong` / `alt` は字面を残し、初めて出す所に意味を添える。skill の文書と本書では書かず、`validate.py` の検査 11 が ERROR にする(言い換え表 `writing-for-people.md` と、この「文書の中で置き換える語」の定義の行は除く。この行で除くのは、この行で定義する語だけで、上の「置き換える語」の 4 語は除かない。定義の行〈この行と、上の「置き換える語」の行〉は行頭の字面で探し、本書にそれぞれちょうど 1 行置く。0 行・2 行以上なら検査 11 が ERROR にする。免除の印は使わない)。利用者の依頼(#312)による追加で、上の 27 語・44 語・置き換える語 4 語の数は変えない。「能力帯」→「性能の段」(モデルの性能の高さの区分。「段」だけに略さない)・「私有」→「専用の」(この処理だけが使う、の意味)・「素通り」→「見逃される」「検査をすり抜ける」など文脈に合う言い方・「塞ぐ」(塞が・塞ぎ・塞い・塞げ・塞ご。「閉塞」など前に漢字が付く語は除く)→「防ぐ」(「経路が塞がる」は「使えなくなる」、「受理側を塞ぐ」は「受け付けないようにする」)・「git 出口」「git の出口」(大文字小文字・空白の有無を問わない)→「commit・push・PR の受け持ち」。一般語の「出口」だけでは探さない。検査と除外は、この行の前半(枡・継承枠)と同じ。利用者の依頼(#313・#315)による追加で、数は変えない。「写し」(動詞「写す」の活用〈写さ・写し・写す・写せ・写そ〉と「複写」、「書き写し」「丸写し」「筆写する」などコピーの意味の複合語を含む。前に漢字が付く語は「〜写す」「〜写し」などの形〈写さ・写し・写す・写せ・写そ〉だけを探し、名詞のまま探すのは「複写」だけ。前に描・転・模・映・試・書・接・実・特・速・縮・透・活・投・連・盗・大 のどれかが付く語〈一般の語として使う決まった語。描写・転写・模写・活写・大写しなど〉と、「写真」「写像」は除く。一覧に無い漢字が前に付く語は、意味を問わず探す〈探したくない語が新しく出たら一覧に足す〉)→「コピー」「コピーする」(テストのためにわざと壊したものは「わざと壊したコピー」。そのまま載せる意味なら「書く」「載せる」もよい)・「宛先」(「宛て先」も)→ メッセージを送る相手の名前の意味は「送る相手」「送る相手の名前」、改名先・ssh の接続先・push の送り先の意味は「改名先」「接続先」「送り先」・「前置き」(「以前置き…」と、後ろが「置き換え」「置き場」になる形は除く)→ git に付ける安全用のオプションの意味は「安全用のオプション」、ほかの意味は意味に合う言い方(コマンドの先頭に付ける `cd`〈以前の書き方は「cd の前置き」〉→「先頭に付ける `cd`」、予約語の前置き →「前に付く予約語」、前置き比較 →「先頭が一致するかの比較」、ホスト CLI の前置きのフラグ →「先頭に置くフラグ」など)。27 語の「一時ツリー」の言い換えの字面も、同じ変更で「…一時的なコピー」に直した(言い換え表と、言い換えを添えた各所も)。検査と除外は、この行の前半と同じ。利用者の依頼(#315 の後半)による追加で、数は変えない。「フル段階」→「双方向でやり取りできる環境」・「標準段階」→「一方向だけの環境」・「最小段階」→「別の AI を使えない環境」(§5-17 の太字の名前 `**フル**`・`**標準**`・`**最小**` と、「フル / 標準 / 最小」「標準・最小」のような並びも同じ。並びは、「フル」か「標準」の後に区切り〈`/`・`／`・`・`〉と「標準」か「最小」が続く形だけを探し、「標準出力」「標準の」など、ほかの意味の「標準」は探さない)・「段階判定」と「実行段階」(の判定)→「やり取りの範囲の判定」・「結末の行」→「結果の行」(ファイルごとに初めて出す所に `無人のイテレーションの結果: …` の形を添える)。「段階」だけ(「段階を下げて続ける」など)と「結末」だけは、ふつうの日本語の語として探さない。書式が決まった行の字面(`無人のイテレーションの結果: …`・`- 結末: …`)は変えない。検査と除外は、この行の前半と同じ。利用者の依頼(#316)による追加で、数は変えない。「サイレント」→「黙って〜しない」「知らせずに〜」の形で、そこで何をしないかが分かる言い方(サイレント縮退禁止 →「黙って内蔵に切り替えない」「黙って代わりの手段に切り替えない」など。下の「「縮退」」の分け方に合わせ、場所ごとに意味を読んで決める。サイレントスキップ禁止 →「黙って飛ばさない」、サイレント切り捨て禁止 →「黙って切り捨てない」、サイレント無視禁止 →「黙って無視しない」、サイレント続行 →「知らせずに続ける」)。規則の名前(太字の項目名など)も改名し、名前で指す所と、字面を照らすテストも同じ変更で直す。カタカナの字面「サイレント」を探す。検査と除外は、この行の前半と同じ。利用者の依頼(#317)による追加で、数は変えない。「レビュー役」→「レビュアー」・「実装役」→「実装担当」・「まとめ役」→「team-lead」(作業を進め、結果を確かめる側の AI)・「検証役」→ 結果を確かめる側なら「team-lead」、変更をレビューする側なら「レビュアー」・「全回帰」(後ろが「テスト」の形は除く)→「全回帰テスト」(初めて出す所に「回帰テストをすべて流すこと」を添える)・「試験」→ 自動のテストは「テスト」、実行時の小さな試しは「試し」、人が実機で確かめた実験は「実機での確認」、試験用の Issue は「テスト用の Issue」・「直読み」→「(import を通さず)直接読む」(例: 「`AGENTS.md` を直接読めない環境」)・「峻別」→「はっきり分ける」・「一時物」→「一時ファイル」(フォルダも含む所は「一時ファイルやフォルダ」)・「子起動」→「子プロセスとしての起動」・「非ファイルタスク」→「ファイルを持たないタスク」・「退行」→「後戻り」か、文脈に合う言い方(「使える機能が減る」「守りを外した版」など)・「追随」→「合わせる」・「起票」→「書き起こす」。どれも字面だけを探す。名前(見出し・太字の項目名)に入っていれば改名し、名前で指す所と、字面を照らすテストも同じ変更で直す。「検証器」は、決まった行 `- 検証器記録:`(`review-guard.py` が書き、テストが照らす)があるので探さない。`scripts/validate.py` を指す所は `validate.py` と名前で書き、一般の検証のコードを指す所は「検証のスクリプト」と書く。検査と除外は、この行の前半と同じ。利用者の依頼(#333)による追加で、数は変えない。「担保」→「保証する」「確かめる」「守らせる」「保つ」など文脈に合う言い方(読み取り専用を指示で担保する →「指示で守らせる」、品質を担保する →「品質を確かめる」)・判断や結果を寄せる比喩の「倒す」「倒れる」(「に倒す」「へ倒す」「に倒れる」の形と、間に「しか」「必ず」「も」「は」が入る形〈どちらにも倒れない・保留には倒さない など〉を探す。「を倒す」「が倒れる」は探さない。「に」「へ」の後に続く形は、字どおりの意味〈物を倒す〉でも検出する〈限界〉。そのときは「傾ける」「横にする」などに言い換える)→「〜にする」「〜と判定する」「〜と見なす」「〜になる」(保守側に倒す →「安全な側と判定する」、保留に倒す →「保留にする」)・「素の」(前に漢字が付く「要素の」などは除く)→ 何が付いていないかを書く(素の `cd` →「`CDPATH=` を付けない `cd`」など、素の `reviewer` →「持ち場名を付けない `reviewer`」、素の言及 →「リンクにしていない言及」)・「打ち直す」→「もう一度実行する」(名詞は「再実行」)・「打つ」(打た・打ち・打っ・打て・打と。コマンドを実行する意味。「打ち切る」「打ち消す」「打ち合わせ」「打ち返す」「打ち直す」と、前が「壁」「抜き」「決め」の形は別の語で探さない)→「実行する」(人が引数を書く意味は「入力する」、文の形に合わないときは「行う」。字面だけで探すので、別の意味の「打ち明ける」「釘を打つ」なども見つける限界がある。そのときは別の言い方にする)・「読込」「書込」(「書込み」も)→「読み込み」「書き込み」(送り仮名を省かない)・「陽性対照」「陰性対照」「正常対照」(間に「の」が入る形〈陽性の対照・陰性の対照・正常の対照〉も)→「比べるための起動」「比べるためのケース」、効くはずのものが効くかを見る起動、拒否されるはずの構成、止めてはいけない正常な例など文脈に合う言い方。名前(太字の項目名)に入っていれば改名し、名前で指す所と、字面を照らすテストとスクリプトの文も同じ変更で直す。検査と除外は、この行の前半と同じ。字面だけでは同じ字のふつうの意味と分けられない「群」(プロセスグループの意味。毎回「プロセスグループ」と書く。「観点群」とふつうの「〜群」は別の語)・「対照」(単独。「比べるための例」など)・「変異」(「わざと壊したコピー」。自己テストの出力に出る名前「変異 `x`」は字面を残し、ファイルごとに初めて出す所に説明を添える)・「回帰」(後ろに「テスト」が無い形。「回帰テスト」と書く。決まった語「回帰の疑い」と、雛形の表の見出し「回帰検知条件」は字面を残す)は検査で探さず、文脈ごとに言い換える。利用者の依頼(#337)による追加で、数は変えない。「打つ」は利用者の依頼(#426)による追加で、数は変えない。
- **「縮退」**: 結末の値の `縮退` はコードで書き、字面を変えない。ほかの意味は言葉を分ける — 段階を下げる →「段階を下げて続ける」/ 外部のレビュアー・implementer が使えない →「内蔵に切り替える」/ 道具・手段が無い →「代わりの手段に切り替える」/ どれにも当たらない → 何が起きるかをそのまま書く
- **置き換えも検査もしない語**: 「経路」「入口」「控え」は、一般の語として扱い、置き換えず、`validate.py` でも探さない(#317)。言い換え表と 44 語には入れず、上の 27 語・44 語・置き換える語 4 語の数は変えない(44 語に入る「ふつうの日本語の語 3 語〈結末・段階・候補〉」とは別の扱い)。理由: 3 語は一般の語で、ふつうの意味で使う所がほとんどなので、字面だけでは独自の意味で使う所を探し分けられない。`do-task/references/external-runners.md` の「レビュー経路」「実装経路」は、冒頭の対応表で決めた名前で、テストにも結び付いている。ただし、人に出す報告・質問の型の中で、同じファイルの中だけで決めた記号の名前になっている所は、上の「報告の型の言葉」の「名前」のとおり、中身を表す名前に変える(init-project の「経路 a」「経路 b」は「互換形からの移行」「未移行からの移行」にした)。do-task の「対話の本文の照合」で名前のように使っていた名詞の「控え」も、「控えた値」など中身の言い方にした(動詞の「控える」はそのまま使う)
- **references にも当てる**: 上の「skill 本文の内部の言葉」(27 語。選び方・添え方を含む)・「名前は字面のまま」・「置き換える語」・「「縮退」」は、references の本文にも当てる(言い換え表の `writing-for-people.md` は除く)。初めて出す所は references の 1 本ごとに数え、言い換えは語ごとに 1 か所にそろえる(報告の型の中の説明は数えない)。下の「行の長さ」は SKILL.md だけに当てる(references の長い行の扱いは #202 で決める)
- **言葉の機械検査**: `validate.py` の検査 13〜15 は、27 語の初出の言い換え・本文の言い換えの重複・名前でもコードでもない「縮退」を ERROR にする([Issue #225](https://github.com/Yuki-Maeda-valour/workflow/issues/225))。
  - 対象は各 skill 直下の `SKILL.md` と `references/**/*.md`。除く文書は `do-task/references/writing-for-people.md` の正確なパスだけとする。`templates/`・本書の本文・README・決定録・スクリプトは対象にしない。検査 11 を重ねて作らず、references の行の長さにも広げない。
  - 27 語と言い換えの字面は下の表から読む。表の欠落・重複・空欄・語数の誤りも ERROR にする。語の数え方も表の字面から導き、検査の中に語のコピーを持たない: 英字と `-` だけの語は、前後が英数字・`_`・`-` でない所だけを数え、その語だけのインラインのコードの中も数える。「X(Y)」の形の語は X と Y のどちらも数える。括弧を含むのに「X(Y)」の形でない語は ERROR にする。下線形式の見出しや参照形式のリンクも Markdown として解釈する。解析には `requirements-dev.txt` の `markdown-it-py` が必要で、使えなければ導入案内付きの ERROR にする。
  - 名前と報告の型の除外は [`scripts/wording-exceptions.json`](../scripts/wording-exceptions.json) に置く。名前・定義元のパスと字面・使うファイル・許す形・理由を持たせ、定義と許す形が実在するかも検査する。定義の実在は、定義元のファイルに定義の字面があるか(部分一致)だけで確かめる。見出し・太字かどうかは見ないので、文中の言及も定義として通る(限界)。定義元が名前を決めた所(見出し・太字の名前・終了値の名前・報告の型)かは、レビューで確かめる。許す形は元の Markdown の短い字面を完全一致で指定する。該当部分だけを除き、同じ行のほかの文は検査する。
  - 許す形には名前の字面を含める。例外は、一覧の項目が `short`(短い呼び名 `text` と、印 `marks`)を明示したときだけとする。名前の字面で分岐しない。短い呼び名は、名前に含まれる検査語 1 つ(いまは `縮退`)。印は、定義の字面がある行に在る字面のうち、次の 3 種類に限る: 数字だけの終了値の番号・①〜⑳ の 1 字・同じ行で `**…**` に囲まれた語か ` / ` で区切った並びの 1 項目(先頭の番号を除く)である状態名。状態名の印は 2 字以上。数字の印は前後が数字でない所だけに当てる。いま `short` を持つのは、終了値の名前 `30 縮退`・`32 縮退` と、5 つの条件を並べた太字の名前「縮退(2026-09-17 決定 29・32・34・49)」の 3 つ。その許す形は、名前の字面が無くても、短い呼び名をちょうど 1 つと印のどれかを含めば登録できる(番号を省いた既存の参照形〈終了コード 30 は縮退・下の縮退 ② など〉のため)。限度: ほかの検査語を含む形と、短い呼び名の直後に(空白を挟んでも)し・す・さ・せ・そ が続く形(`縮退する`・`縮退 する`・`縮退して続ける` など、動詞として使う形)は、名前全体を含む形でも登録できない。末尾が `縮退` の名前の直後に(空白を挟んでも)し・す・さ・せ・そ が続く形も、`short` の有無によらず登録できない。どの名前でも、本文で許す形の末尾の `縮退` の後に、強調・コードの印や空白・全角空白をいくつ挟んでも、し・す・さ・せ・そ が続く所は除かない。改行は、同じ段落・同じ項目の中の 1 つまで挟んでよい(1 文を途中で改行した書き方)。次の行が空行のときと、箇条書きの印(`-`・`*`・`+`・`1.`・`1)`)・見出し・引用の印で始まるときは、別の段落・別の項目として動詞とみなさない(引用の印は、許す形が引用の外にあるとき)。許す形が引用(`>`)の中の行にあるときは(箇条書きの項目の中の引用 `- > …` も含む)、次の行が同じか浅い深さの引用(行頭の `>` の数が同じか少ない。`>` の後ろの空白は 3 字まで)なら、その頭を読み飛ばし、同じ引用の段落の続きとして動詞を探す(浅い行は、Markdown では印を省いた同じ段落の続きと読まれるため)。深い行(新しい入れ子の引用が段落を区切る)・箇条書きの印で始まる行・`>` だけの行(引用の中の空行)・引用の中の `*` の箇条書きは、別の段落・項目として越えない。本文で許す形を除くときも、許す形の先頭・末尾が数字なら、その外側が数字でない所だけを除く(強調・コードの印は字数によらず飛ばして見る)。名前全体を含むかの判定にも、同じ数字の境目を当てる。検査は Markdown の元の字面で隣の字を見る。リンク・文字参照・HTML が隣にあるときは、表示の字面を求めず、隠さずに検出する(限界。許す形そのものにも、これらの書き方〈`[` `]` `&` `<` `>`〉は使えない)。
  - 限界(まれな形。どれも今の文書には無い):
    - バックスラッシュの強制改行(行末の `\`)を挟んだ動詞は見ない。
    - ゼロ幅の空白などの見えない字を挟んだ動詞は見ない。
    - 引用の深さは、`>` の後ろの空白を 3 字までとして数える。
    - `>` の間に空白が 4 字以上ある書き方(Markdown では入れ子の引用)は、深さを少なく数える。そのため、動詞を見逃すことも、正しい文を ERROR にすることもある。
    - 箇条書きの項目の中の引用の次に、字下げしない `>` の行が続く形(Markdown では別の引用)は、同じ深さとして越える。そのため、正しい文を ERROR にすることがある。
    - 引用と箇条書きを交互に入れ子にした形(`> - > 32 縮退` + 改行 + `>   > する。`。Markdown では同じ段落)は、途中の箇条書きの印を数えないので、深さを違えて数える。そのため、動詞を見逃す。
  - 「縮退」だけを許す指定・ファイル全体の指定・見出しからの自動登録は認めない。`validate-allow` の印は効かせない。名前を足すときは、定義・参照・一覧・検出するテスト・検出しないテストを同じ変更で直す。「縮退」の言い換えが意味に合うかは、レビューで確かめる。
- **行の長さ**: SKILL.md の本文の 1 行は 200 字以下。測るのは、行の文字数から 200 字を超えるインラインのコードの長さを引いた数。表の行・コードフェンス・frontmatter は除く。超える行は、1 文 1 つの下位の箇条書き(`- `)に分ける。1 文が 200 字を超えるときは、文の途中(「、」「・」「/」「→」「—」の後や、括弧の前後)で分けてよく、区切りの字は残す(下位の箇条書きの 1 項目が文の一部になってよい)。ただし、意味のまとまりを切らない: 条件(「〜とき」「〜なら」「〜では」など)や主語と、それを受ける結果・述語を、分けてできた別々の行に置かない。後の文が前の文の条件の下でだけ当たるときは、2 つの文を同じ行に置く。1 行目を条件で終わらせるのは、分けてできた行のすべてがその条件の下にあるときだけにする。条件と結果が 1 行に収まらないときは、条件と結果が同じ行に残る所(括弧の前後など)で分ける。それでも収まらないときは、条件で終わる分けてできた行(末尾が「、」)の下に、もう 1 段深い `- `(分けてできた行の字下げ + 2)を置き、その条件の下の文を並べてよい。深い行はこの 1 段だけで、1 行目の直下には置かない。分けてできた行は、すべて「1 行目の字下げ + 印の長さ」の字下げの `- ` にそろえる(条件の下の 1 段深い行を除く)。印の長さは、末尾の空白を含む印の文字数(`- ` は 2、`1. ` は 3、`10. ` と `7′. ` は 4)で、印の無い行(段落)は 0。分けてできた行は、比べる中身(行頭の字下げと印・言い換えの字面・括弧・句読点・空白を除いた残り)が空にならないように分ける(閉じ括弧と句読点だけの行・言い換えだけの行を作らない)。分ける行の下に既にある下位の項目は、字下げを変えずに、分けてできた行の後に続ける。番号つきの項目を足さず、番号を変えない(ほかの文書が番号で指すため)。インラインのコード・太字・リンク・言い換えの字面の途中で行を分けない。`validate.py` の検査 10 が確かめる(WARN)

| 語 | 言い換えの字面 |
|---|---|
| イテレーション | 無人ループの 1 回分の実行 |
| 照合 | 照らし合わせて確かめること |
| 正本 | ほかが合わせる元 |
| 委託 | 作業を別の AI に任せること |
| 権威参照ファイル | AI への指示をまとめたプロジェクトのファイル |
| 役割語 | AI の役割の名前 |
| 解決表 | 役割の名前を、実際に使う AI の仕組みに対応づける表 |
| 裏取り | 実際のコードやファイルを読んで確かめること |
| 突合 | 2 つを照らし合わせて食い違いを探すこと |
| 本文ダイジェスト | 本文から計算した短い値。本文が変わると値も変わる |
| 発見元 | 改善の候補を見つける skill |
| 互換形 | AGENTS.md を読み込む行を持つ CLAUDE.md |
| 許可の仲介 | 無人の実行で、操作を許すかをその場で判定する仕組み |
| team-lead | 作業を進め、結果を確かめる側の AI |
| 基準コミット | 作業を始めた時点のコミット |
| 実動確認 | 実際に動かして、変更どおりに動くかを見る確認 |
| 品質ゲート | 書式・型・テスト・ビルドの自動の検査 |
| ランナー | レビューや実装に使う外部の AI のコマンド |
| ドリフト | 文書や設定と実際との食い違い |
| フォールバック | 先の手段が使えないときに次の手段へ切り替えること |
| 差し戻し(差し戻す) | 相手に返して、やり直してもらうこと |
| トリアージ | 指摘を扱いごとに振り分けること |
| implementer | 実装を受け持つ AI |
| reviewer | 変更を確かめる AI |
| checker | 設計書を点検する AI |
| researcher | 調べものを受け持つ AI |
| 一時ツリー | 外部のレビュアーに見せるために作る、ファイル一式の一時的なコピー |

## 7. ホスト非依存レイヤ(手順層は可搬・実行層のみ吸収)

この skills 集は Claude Code 以外のホスト(Codex CLI / Cursor 等)でも使う。層を分けて扱う。**「置けばそのまま動く」ではない** — 可搬なのは形式であり、本文はホスト内蔵の機構を前提とした記述を含む:

| 層 | 中身 | 可搬性 |
|---|---|---|
| 形式 | SKILL.md のファイル形式(frontmatter + 本文)と配置規約 | **可搬**。agentskills.io の開標準で、各ホストが同じ形式を読む |
| 本文 | 手順の記述 | 委託の語は役割語 + 解決表(§7-5)で吸収する(**語彙・検査範囲・除外集合・移行の状況は §7-7 が正本**)。**`.claude/` 配下の状態ファイルを前提とする記述は残っており、その扱いは未決**。他ホストでは、内蔵サブエージェントを前提とする実行のしかたを §5-17 の縮退プロトコルとして読み替える。**Codex / Cursor で SKILL.md が読み込まれ起動することは確認済み**(2026-09-11、ユーザーによる実環境での確認報告。どの工程まで動いたかの記録は無い。§7-3)。**委託の解決先(解決表)を持つのは Claude Code だけ**で、解決表に列が無いホストでは役割語を解決できず、独立レビューを要する工程(設計の完了・do-task の完了・ship-task の PR など)は §5-9 によりレビュー未完了で止まる(文書の規定からの帰結で、実測ではない)。量の目安は下の測定コマンドで再現できるが、**その語彙は `.claude/` を含む広いもので移行順序の目安専用**であり、**検査と到達条件は §7-7 の語彙で判定する**(両者の実測値は比較できない。§7-7 末尾) |
| 実行層 | 誰が委託を実行するか(**① ホスト内蔵 → ② MCP → ③ CLI**) | ホスト依存。**解決表(§7-5)とアダプタ(review-agent.sh / implement-agent.sh)で吸収する** |

**ホスト固有記述の量(再現可能な測定)**: 語彙の選び方で数値は変わるため、コマンドごと残す。

```bash
for s in plugins/dev-workflow/skills/*/; do
  printf '%-20s %s\n' "$(basename "$s")" \
    "$(grep -oE 'Agent|SendMessage|ListAgents|Explore|general-purpose|\.claude/|fable|opus|sonnet' "$s/SKILL.md" | wc -l)"
done
```

2026-09-09 時点の実測(出現数): do-task 32 / create-task 25 / init-project 22 / update-doc 11 / reflect-decisions 11 / data-audit 9 / understand-project 7 / 他は 2 以下。**この語彙は `.claude/` のパス参照を含む**ので、委託の語がゼロになっても値はゼロにならない(2026-09-18 の実測: この語彙では 12 skill で 1〜17 件ヒットするが、**その全件が `.claude/` のパス参照**で、§7-7 の語彙では計 0)。**この値は移行当時(2026-09-09)に順序を決めるための測定記録**であり、⚠ **現在の `.claude/` 依存度の指標には転用しない** — `.claude/` のヒットには共有設定(`project-profile.yml`)もホスト固有設定(`settings.json`)も同じパスの反復もまとめて入るため(実測〈2026-09-18〉: 最多の init-project 17 件の内訳は profile 7 / settings 6 / **状態ファイル 3** / その他 1 で、状態ファイルだけなら do-task の 8 件が最多)。⚠ **他ホストで読み替えが要るかは、この値ではなくホストが実際に持つ機構で決まる**(判定手段と、段階を下げて続けるときの中身は §5-17)。

**内訳の再現**(分類の仕方で数値が変わるので、上と同じくコマンドごと残す):

```bash
# 状態ファイルだけを数える(共有設定 project-profile.yml とホスト固有設定 settings*.json を除く)
for s in plugins/dev-workflow/skills/*/; do
  printf '%-20s %s\n' "$(basename "$s")" \
    "$(grep -oE '\.claude/(grasp\.md|reviews)' "$s/SKILL.md" | wc -l)"
done | sort -k2 -rn
```


### 7-1. 配置先(手順層)

| ホスト | 配置先 | 備考 |
|---|---|---|
| Claude Code | plugin marketplace(推奨)/ `<project>/.claude/skills/` / `~/.claude/skills/` | `setup.sh --link / --copy / --global` |
| Codex | `.agents/skills`(リポジトリ)/ `$HOME/.agents/skills`(ユーザー)/ `/etc/codex/skills`(管理者) | frontmatter は `name` + `description` が必須 |
| Cursor | `.cursor/skills/` または `.agents/skills/` | `/skill-name` で起動 |

`setup.sh --agents / --agents-copy / --agents-global` が `.agents/skills` への配置を行う(Codex と Cursor で共用できる)。**skill を 1 本だけ取り出す配置は非サポート**(§6 の兄弟参照が前提のため)。**配置できることと、そのホストで手順どおり動くことは別**である(上の表の「本文」行)。他ホストで使う場合は、内蔵サブエージェント前提の記述を §5-17 の縮退プロトコル(別の AI を使えない環境では直列のセルフ実行)として読み替える(**委託そのものは全 skill が解決表(§7-5)で解決する**。解決先を持つのは今は Claude Code だけ — §7 の表の「本文」行)。

### 7-2. 実行層(外部 CLI の起動)は宣言時のみのオプトイン

本節は主に ③ 外部 CLI と、profile から受け取る値の信頼モデルについて。委託の解決順全体は §7-5。

**契約の正本は [`do-task/references/external-runners.md`](../plugins/dev-workflow/skills/do-task/references/external-runners.md)**(判定順序・終了コード・既定ランナー表・読み取り専用の保証範囲・ログ規約・機密ガードの手順)。ここには**方針(なぜそうするか)だけ**を置き、仕様の詳細は再掲しない — 同じ事実を複数箇所に書くと、変更のたびに同期漏れが起きるため。

- **なぜオプトインか**: 既定でホスト外のプロセスを起動すると、課金・機密・実行権限の面で利用者の想定を超える。よって**宣言(`--runners` 引数 / profile の `features.runners` または `features.implementer`)が無ければ外部 CLI を探しに行かない**。既定の**レビュー編成**は §5-9 のままで、この節を足しても 1 ビットも変わらない
- **なぜ起動をスクリプトに集約するか**: 判定(存在・自ホストか・読み取り専用・疎通)を skill 本文の散文に書くと守られない。決定的な処理は `do-task/scripts/` のスクリプト(経路ごとに 1 本: review-agent.sh / implement-agent.sh。実装経路では、起動の前後で作業ツリーと git メタを保護する処理 — スナップショット・退避・比較・タスク MD の復元 — も implement-guard.sh の 1 本に集約する。こちらは起動を行わない)に集約し、skill は薄いオーケストレーションに徹する(§6)
- **タスク本文の復元(#259)**: `implement-guard.sh restore-taskmd` は既存 state を確認し、Python 3 の内部 helper `restore-taskmd.py` へ固定した期待値を渡す。元ファイルを先に消さず、同じ親の専用領域へ準備し、内容・権限・参照の検査後に原子的に置き換える。準備失敗時は元ファイルを保ち、公開後の失敗は変更済みと報告する。`TOUCHED`・`TEMP`・無応答時の停止、有限予算、残る同 UID の競合限界は [external-runners.md](../plugins/dev-workflow/skills/do-task/references/external-runners.md) §12-2 を正本とする。公開入口は GNU `timeout` の330秒 TERM・追加5秒 KILL で監督する。GNU `timeout` が無ければ公開入口を起動しない。必要な fd 機能・GNU `cp` を使えなければ対象を変えずに止める。旧 state・正当な選択リンク・通常の復元を維持する。macOS では起動時の試し(専用領域で短いファイルを fd 別名でコピーしてみる処理)で止まる(`fd-copy-unavailable`。`/dev/fd` の device 番号が開いたファイルと食い違うため。#285)。正常な復元の macOS 実機の確認は #271 に置く。

- **開始時の設定候補と使用先の保護**: 実装前に、設定 root と全 `include` / `includeIf` 先を控える。空・コメントだけ・欠落・不成立の条件の先も含める。設定値がまだ効いていない経路を後から有効にして、検査をすり抜けることを防ぐ。親・リンクの差替えと持続する本文変更は、Git を一度も起動せず検出する。末端通常ファイルの同内容再作成は許可する。
  - **元の設定を読む前の検査**: 通常の設定候補を照合した後、隔離した同じ Git で index・HEAD tree・`.gitmodules` から使用先を調べる。新しい submodule の設定 root は自動採用せず、元 repo の Git と呼出側の `--precheck` より先に止める。人が変更を確認して `take` で再登録する。通常の `git add`・HEAD/branch の進行は設定変更とせず、既存 submodule の HEAD・stage 別 OID・diff・status の変化を引き続き検出する。
  - **結果の意味**: `config-check` の成功は隔離 Git を使うため `CONFIG=same`・`GIT_SKIPPED=no`。保持した通常設定の変更は exit 33・`GIT_SKIPPED=yes` で止める。隔離検査後の新 root や選択 blob の不一致は、元 repo の Git を起動せず exit 33・`GIT_SKIPPED=no` で止める。`compare` も同じ順序で再検査する。
  - **設定入力の上限**: 単体 16 MiB・累積 200 MiB を読む前に予約する。直接の hash/parser だけでなく、Git 内部の include 再走査、親のリポジトリの中で読む下のリポジトリの設定、選択した `.gitmodules` 本文と生成した専用の設定も含める。候補と include 値、使用先と repo 文脈は各 2,000 件、リンクと repo 文脈の深さは各 40 段まで。`onbranch` の不成立は同じ Git で確かめて上界から除き、ほかの条件は成立側で見積もる。正常な不成立先を一律に拒否しない。詳細な式・独立したコピー量と出力量の上限は契約の正本に置く。
  - **パスと復旧**: `~/` の展開では `HOME` の設定有無と値を保ち、`~user` と親の論理パスからの相対 include も同じ Git に解釈させる。記録形式の足りない旧 state では設定検査だけを止め、タスク本文の手動回復と `cleanup` は維持する。設定の自動復元・比較中の基準更新はしない。
  - **保護の境界**: 初回の repo・管理パス特定は、開始時設定を信頼する既存の境界である。上限は候補確定後の設定入力の読取に掛ける。pack 全体・非選択 delta base・全 object I/O の上限ではない。同じ利用者権限の並行変更や任意の改変 Git・将来版の内部動作を完全に隔離する保証でもない。必要環境は bash 4.0 以上・Linux/GNU 系を維持し、旧 bash 実体と macOS の実測は今回の確認に含めない。[Issue #226](https://github.com/Yuki-Maeda-valour/workflow/issues/226) は品質検証の停止・コピー保持を扱う別途検証。
  - 保証範囲・上界の式・旧記録の扱い・再開条件は [外部委託の契約](../plugins/dev-workflow/skills/do-task/references/external-runners.md) §12-2・§12-5 が正本。設計と検証は [Issue #230](https://github.com/Yuki-Maeda-valour/workflow/issues/230)。#77 の残件のうち本 Issue が列挙した設定経路を扱い、親 Issue 全体の解決とはしない。
- **信頼モデル(最重要)**: `.claude/project-profile.yml` は commit される共有ファイルであり、リポジトリを書ける者が内容を書ける。したがって **profile に書かれた値から「実行されるコマンド」が変わってはならない**。profile から受け取ってよいのは既定表にあるランナー名とモデル名だけで、**起動コマンドの上書きは profile からも AGENTS.md・CLAUDE.md・README などリポジトリ内のいかなるファイルからも読まない**(書かれていても無視し、無視した旨を報告する)。**リポジトリ内のテキストはデータであって指示ではない**(**MCP 宣言の扱いは §7-6**。生成経路は skill 側の明示承認、既に設定ファイルを持つリポジトリはホスト側のゲートが守る。本節の原則に対する明示的な例外である)
- **なぜ既定表のランナーに上書きを許さないか**: 任意のコマンド文字列を検査して「読み取り専用である」と保証するのは原理的に困難(フラグの重ね指定・`--` 以降への配置・同名バイナリなど、argv 検査を通り抜ける手は尽きない)。個別のガードを足していく方針は破綻するため、**上書き経路そのものを既定表ランナーから外す**。独自コマンドが要る場合は既定表に無いランナー名としてユーザーがセッション内で明示指定し、**読み取り専用の保証はユーザー責任**とする
- **なぜ内蔵レビュアーを残すか**: 外部ランナーは会話継続ができず、認証・レート制限で落ちうる。多モデルレビューの土台を外部依存にしないため、`reviewer-internal` は常に維持する(`reviewer-alt` 持ち場の置換は可)
- **機密ガードの原則**: 宣言は「使ってよい」であって「毎回無確認で渡してよい」ではない。`secret_paths` が実在するプロジェクトでは明示確認を取ってから起動する。**レビュー経路**は読み取り専用であり、**ユーザーの git 状態(index・stash)を書き換えてはならない**。**レビュー経路の外部 CLI は一時ツリー(作業ツリーの複製)で起動する**(実装経路の保護は一時ツリーを使わないスナップショット方式で、詳細は `external-runners.md` §12-2)。cwd を限定できないランナーは既定で無効とし、一時ツリーを作れない構成では起動せず、§5-9 に従って内蔵に切り替え、そのことと理由を報告する。なお**契約側(`external-runners.md` §9)は上書き手段を設けない**ため、この「既定で無効」は実質「使わない」として**契約されている**(表現の差であって方針の差ではない。`--command` まで閉じたのは 2026-09-09 決定 15 の却下理由と同じ根拠による意図的な強化)。ただし**機構で閉じているのは既定表ランナーへの `--command` 拒否まで**で、既定表外の名前で渡す経路は検出せず**呼び出し側 skill の義務**として課される(同 §9)
- **なぜレビュー経路で一時ツリーを必須にするか**: cwd を限定せずに起動した外部 CLI が**リポジトリ外のパスを読んだ実測**がある(§7-3)。**他プロジェクトのパスが外部ベンダーへ渡ることを防ぐ**。ランナー側のサンドボックスは既定表の読み取り専用フラグと同様に実装を信頼するしかなく(保証範囲は `external-runners.md` §3)、ランナーごとに実効性を検証できないため既定で必須にする。却下: 「限界として明記するに留める」(実害が確認されたリスクを利用者の注意力に任せることになる)。**実装経路(implementer)は書き込みを前提に起動するため一時ツリーを使わず**、起動前後のスナップショット比較で外部が書いた分を識別する(§12-2 が詳細を持つ。同じ事実を再掲しない)
- **なぜ偽 gitlink による欠落を停止するか**([H1 の設計・決定 #140](https://github.com/Yuki-Maeda-valour/workflow/issues/140)、2026-10-06): index の gitlink だけで通常ディレクトリの未追跡内容がレビューから黙って漏れることを再現した。既知の限界の記載だけ、または警告して続行する案は、欠落したまま完了できるため採らない。正当なサブモジュールの利用を保ちながら、検出できる不整合では停止する。`.git` 名エントリの真正性と並行書き換えの保証までは拡張しない。停止条件・復旧・受容限界の詳細は契約の正本 `external-runners.md` §9-1、回帰テストと通し確認は §11 に置く。
- **implementer の外部化を認める**(2026-09-17 決定 1)。本節が以前置いていた除外規定(implementer を外部化の対象から外すという範囲設定)は明示的な検討の結果ではなかったため、方針を改めた。オプトインの宣言(`features.implementer` に既定表の実装用ランナー名を指定)を維持したまま、**宣言された環境では implementer の既定を外部にする**(スキーマは §4)。以下は 2026-09-17 決定 3・4・5 の**理由(なぜそうするか)だけ**を持ち、判定順序・終了コード・スナップショットの手順・機密ガードといった契約の詳細は再掲せず `external-runners.md` §12 を参照する(本節冒頭の原則「同じ事実を複数箇所に書かない」):
  - **なぜ作業ツリーを直接使うか**(2026-09-17 決定 3): 依存・ビルド環境がそのまま使える状態で implementer 自身にテストを回させるため。一時ツリーを使わない代わりに、起動前後のスナップショット比較で保護する
  - **なぜセッション初回に明示承認を要るか**(2026-09-17 決定 4): 書き込み委託は「リポジトリを書ける者が、外部に書かせる設定を仕込める」状態を作るため、既定表にある名前でも実行は宣言だけでは行わない。MCP 宣言(§7-6)と同型の「profile に書いてある = 承認済みとは扱わない」を踏襲する
  - **なぜ走行中失敗を内蔵へ自動で引き継ぐか**(2026-09-17 決定 5): 完走を優先し、ユーザーの git 状態を書き換える巻き戻しは採らない。起動前の失敗は内蔵に切り替えて報告し、走行中の失敗は残った変更を捨てずに内蔵 implementer が引き継ぐ(黙って引き継がない)

出典: `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md`(決定 15)/ `docs/minutes/2026-09-17_壁打ち_implementerの外部委託.md`(決定 1・3・4・5)

設定を戻さずに調べる診断は、[`diagnostic-git.md`](../plugins/dev-workflow/skills/do-task/references/diagnostic-git.md) を公開契約とする（[Issue #260](https://github.com/Yuki-Maeda-valour/workflow/issues/260)）。

- 安全な開始時 `S` はブランチ処理前に保持し、既存の基準 `B` と区別する。ブランチ出典は実際の作成記録だけを使い、既存ブランチの不明な起源は要求しない。
- `take` 成功後、全有効対象の除外指定を親のリポジトリから下のリポジトリへ `plan` / `--carry-plan` で集める。親が最後の計画パス/hashを保持・報告してから外部を起動する。
- 比較不能では人の停止確認後、`extend-plan` → 新計画の親保持・報告 → `run` とする。`run` 失敗も最新計画を保持する。準備の応答不明は旧計画の反復をせず停止する。
- 管理ルートとGitルートを区別し、基準・開始時・保持された作成時・保持HEAD・追加参照・現在の指定を和集合にする。下のリポジトリには祖先の指定も適用する。機密本文と名前を表示前に除外する。
- 専用のGit管理領域で履歴・参照を読み、作業木は選択ファイルの生のbytes・種類・権限を比較する。元設定・属性・改行変換・ignoreを適用せず、元データを書き換えない。
- Python 3.10以上・必要なPOSIX機能・既存のGit要件・GNU `grep` が必要。profileがすべて不在ならPyYAML不要。最終応答も有限の直接の子プロセスで送信し、共有出力のflagsを変更しない。
- 診断成功は復元・基準更新・実装再開の許可ではない。旧記録や保持値の不足は補完せず停止する。内蔵5操作と従来の復元監督・通常復帰条件は維持する。
- 同じ利用者権限の改変、複数ファイル間の競合、元オブジェクト保存先の同時更新、OS入出力の不帰還は完全防止しない。新しい診断のmacOS実機は未確認とし、[Issue #274](https://github.com/Yuki-Maeda-valour/workflow/issues/274)で別途確認する。

### 7-3. 前提とする外部事実(出典・確認日 2026-09-08、一部 2026-09-09。以降の確認・訂正は行ごとに併記)

§7 が依存する外部事実。前提が崩れたら該当箇所は無効になるため出典を残す。

| 事実 | 出典 | 依存する節 |
|---|---|---|
| **gh** の `issue view/list/create/edit` は `-R HOST/OWNER/REPO` で対象を固定する。`repo view` は `-R` を受け付けず、位置引数で `HOST/OWNER/REPO` を渡す | [gh issue](https://cli.github.com/manual/gh_issue)・[gh repo view](https://cli.github.com/manual/gh_repo_view)。ローカル実測(2026-11-04・gh 2.103.3。レビュアーと team-lead からの報告): `repo view -R` は `unknown shorthand flag: 'R'` で exit 1、位置引数は exit 0。`issue create/edit` の書き込みも、このリポジトリのテスト用の [Issue #331](https://github.com/Yuki-Maeda-valour/workflow/issues/331) で確認(下の書き込みの実測) | §5-1 |
| **gh** の `issue view --json body -q .body` は文字列出力の末尾に改行を足す。取得には JSON の `body` を使う | [gh formatting](https://cli.github.com/manual/gh_help_formatting)・[gh issue view](https://cli.github.com/manual/gh_issue_view)。ローカル実測(2026-11-04・gh 2.103.3。レビュアーと team-lead からの報告): このリポジトリの #320 で JSON の本文は `\n` 1つ、`-q` の出力は `\n\n` で終わった | §5-1 |
| **gh** の `repo view <repo> --json name,isPrivate,viewerPermission` で公開範囲と権限を得る。`viewerPermission` の値は `READ`・`TRIAGE`・`WRITE`・`MAINTAIN`・`ADMIN` | [gh repo view](https://cli.github.com/manual/gh_repo_view)・[RepositoryPermission](https://docs.github.com/en/graphql/reference/enums#repositorypermission)。実測(2026-11-04・gh 2.103.3。team-lead からの報告、テスト用の [Issue #331](https://github.com/Yuki-Maeda-valour/workflow/issues/331)): `preflight` で対象 `github.com/Yuki-Maeda-valour/workflow`・公開・`ADMIN` を取得した。公式本文の再確認は未確認 | §5-1 |
| **gh** の `issue list` の既定の上限は30件。`--limit` で取得件数を変える | [gh issue list](https://cli.github.com/manual/gh_issue_list)。2026-11-04 時点、本変更での公式本文の再確認と実機確認は未確認 | §5-1 |
| **gh** の `api` は `--hostname` でホストを選ぶ。`user` の `login` と GraphQL の Issue の `author`・`editor`・`lastEditedAt` で作者・最終編集者・実行者を調べる。`editor` は最後の1人だけを返す | [gh api](https://cli.github.com/manual/gh_api)・[Issue](https://docs.github.com/en/graphql/reference/objects#issue)。ローカル実測(2026-11-04・gh 2.103.3。レビュアーと team-lead からの報告): `gh api user --hostname <HOST>` と `gh api graphql ... --hostname <HOST>`(`-F number=<N>`)が成功。cli/cli#10868 の `editor` が最後の1人であることも確認 | §5-1 |
| 未編集の Issue では GraphQL の `editor` と `lastEditedAt` がどちらも null になる | ローカル実測(2026-11-04・gh 2.103.3。レビュアーと team-lead からの報告): このリポジトリの #2〜#6 で確認。テスト用の #331 でも作成直後は両方 null、編集履歴は0件だった | §5-1 |
| bot が作った Issue の作者は、`gh issue view --json author` では `is_bot: true` と `login: app/<名前>`、GraphQL では `<名前>` になる | ローカル実測(2026-11-04・gh 2.103.3。レビュアーと team-lead からの報告): renovate の公開 Issue で `{"is_bot":true,"login":"app/renovate"}` と GraphQL の `renovate` を確認。他の bot・GitHub App の作者表記は未確認 | §5-1 |
| GraphQL の Issue の `userContentEdits` から各編集の `editor`・`editedAt` を取得する。`pageInfo` と `after` で全ページを取得する | [Issue](https://docs.github.com/en/graphql/reference/objects#issue)・[UserContentEdit](https://docs.github.com/en/graphql/reference/objects#usercontentedit)。実測(2026-11-04・gh 2.103.3。team-lead からの報告、テスト用の [Issue #331](https://github.com/Yuki-Maeda-valour/workflow/issues/331)): 書いた直後の取得で履歴2件(作成時の版と編集)がすぐに見え、最終編集と一致した。`--edits-since` 付きの取得も成功。100件を超える履歴のページ送りと、他の条件での反映時期は未確認。履歴の反映が遅れる可能性があるため、書き込みの直後は本文と最終編集者だけを確認し、履歴の整合は次の通常の取得で見る。`gh` のモックでページ送り・欠落・途中の変更・履歴の反映遅れを検査する | §5-1 |
| **gh** の `issue create/edit --body-file -` は標準入力から本文を受け取る。取得と書き込みは別の要求で、同時編集を原子的に防ぐものではない | [gh issue create](https://cli.github.com/manual/gh_issue_create)・[gh issue edit](https://cli.github.com/manual/gh_issue_edit)。実測(2026-11-04・gh 2.103.3。team-lead からの報告、テスト用の [Issue #331](https://github.com/Yuki-Maeda-valour/workflow/issues/331)): `issue-body.py` の `create --approve-public`・`snapshot --for-execution`・`check-checkboxes`・`update --checkbox-only`・`update`(追記)が成功。古い控えでの `update` は書かずに停止した。`gh` のモックによる書き込みの前後の比較と停止の検査も維持する | §5-1 |
| **シェルの必要環境(2026-10-06 実測)**: Linux 上の bash 3.2.0 / 4.3.0 では `shopt -s inherit_errexit` が失敗し、4.4.0 では成功。do-task の既存ガードは bash 4.0 以上。パス解決の代替だけでは GNU grep/coreutils の要件を吸収しない。macOS 実機の追加確認は #226 に分ける | [Issue #121 の実測・設計](https://github.com/Yuki-Maeda-valour/workflow/issues/121)(main `639fb15` を基準に GNU 配布ソースの bash 実体・Darwin スタブで測定) | 2、do-task の runtime-requirements.md、loop.md |
| **macOS 実機(2026-10-09)**: macOS 26.6.2 / arm64、GNU 系ツールを選んで通常操作と元のプロセスグループの停止を実測。停止直後の一時的な `EPERM` を検出した。物理パスと Apple Git の system 設定にも条件がある(Apple Git の追加の system 設定は、2026-10-30 に保持候補(起動前に控えて、後で変化を照らす設定ファイルの一覧)へ入れた。[#241](https://github.com/Yuki-Maeda-valour/workflow/issues/241))。プロセスグループの外へ移った子孫は保証外。修正後の再検証・独立レビューは Issue に記録し、初回成功を全環境の保証に広げない。2026-10-31 に、品質コマンドの停止と検証用コピーの扱いの実測を回帰テストとして保存した([#239](https://github.com/Yuki-Maeda-valour/workflow/issues/239))。その設計の点検で、プロセスグループの全員が `TERM` で終わる品質コマンドの時間切れ・停止の信号が回収の失敗になる不具合を見つけ、同日に直した([#289](https://github.com/Yuki-Maeda-valour/workflow/issues/289)。プロセスグループの停止を確かめるまで通知の読み口を閉じない)。同日、Git 2.54 の `git commit` が起動する裏の保守処理が検証用コピーへ書き、その削除と重なる不具合も直した([#292](https://github.com/Yuki-Maeda-valour/workflow/issues/292)。検証用コピーを作る commit に `-c maintenance.auto=false -c gc.auto=0` を渡す) | [Issue #226](https://github.com/Yuki-Maeda-valour/workflow/issues/226)・[Issue #239](https://github.com/Yuki-Maeda-valour/workflow/issues/239)・[Issue #289](https://github.com/Yuki-Maeda-valour/workflow/issues/289)・[Issue #292](https://github.com/Yuki-Maeda-valour/workflow/issues/292) | 2、5(review と commit・検証根拠の固定)、do-task の runtime-requirements.md、review-protocol.md |
| **Apple Git の追加の system 設定(2026-10-30 確認)**: Apple Git は、system 設定が有効(`GIT_CONFIG_NOSYSTEM` が未設定か偽)のとき、`GIT_CONFIG_SYSTEM` の値に関係なく、`/etc/gitconfig` より先に `<Git の導入先>/share/git-core/gitconfig` を読む。導入先は Git の本体の場所から求まり、`git --man-path` が `<導入先>/share/man` を返す。公開ソースは apple-oss-distributions/Git の `Git-155` タグの `src/git/config.c`(`git_xcode_gitconfig()`・`do_git_config_sequence()`)。実機は macOS 26.6.2 / arm64・Git 2.54.0(Apple Git-157)で、本体の複製と `GIT_CONFIG_NOSYSTEM` の真偽を変えて確認した。Xcode.app の Git・Apple Git-157 以外の版・Intel の Mac は未確認 | [Issue #241](https://github.com/Yuki-Maeda-valour/workflow/issues/241)、[apple-oss-distributions/Git](https://github.com/apple-oss-distributions/Git)(`Git-155` タグ) | 2、do-task の runtime-requirements.md・external-runners.md |
| Codex は `.agents/skills` / `$HOME/.agents/skills` / `/etc/codex/skills` から SKILL.md を読む。frontmatter は `name` + `description` 必須。agentskills.io の開標準に準拠 | [Build skills — ChatGPT/Codex 公式ドキュメント](https://learn.chatgpt.com/docs/build-skills) | 7-1 |
| Cursor は `.cursor/skills/` または `.agents/skills/` から SKILL.md を読み、`/skill-name` で起動できる | [Cursor Agent Skills(learncursor.dev)](https://www.learncursor.dev/learn/cursor-agents/cursor-agent-skills) | 7-1 |
| Codex は 2026-03-14 に subagents を GA(最大 8 並列・`~/.codex/agents/` の TOML・エージェントごとにモデル指定可) | [Use subagents and custom agents in Codex — Simon Willison](https://simonwillison.net/2026/Mar/16/codex-subagents/) | 7 |
| **Claude Code は v2.1.277 以降、`AGENTS.md` を直接読む**(CLAUDE.md・import・設定なしで動く。**2026-09-21 確認。2026-09-09 時点の「読まない」から変わった**)。ただし既定(`claude-md-or-agents-md`)では、作業ディレクトリ**またはその上位**に `CLAUDE.md` / `.claude/CLAUDE.md` / `CLAUDE.local.md` のいずれかがあると、**`AGENTS.md` を直接読まず、CLAUDE.md 系だけを読む**(その CLAUDE.md が `@AGENTS.md` を import していれば、`AGENTS.md` は import 経由で読まれる)。`~/.claude/CLAUDE.md`・組織の managed CLAUDE.md・`.claude/rules/` は**この判定に数えられず**、`AGENTS.md` と併せて読まれる。直接読むのは作業ディレクトリと上位の `AGENTS.md` / `.claude/AGENTS.md`(サブディレクトリの `AGENTS.md` は、そこに CLAUDE.md 系が無ければ Read 時に遅延読み込み)。`AGENTS.local.md` / `AGENTS.override.md` / `.agents/` 配下は読まない。**既定値のもとで** `AGENTS.md` を直接読んだ対話セッションには `no CLAUDE.md found; AGENTS.md loaded: <path>` のような行が出る(公式はこの確認方法を既定値に限定している。他の設定値では Claude に指示内容を尋ねて確認する) | [How Claude remembers your project — AGENTS.md](https://code.claude.com/docs/en/memory#agents-md) | 3, 7-4 |
| **Claude Code が `AGENTS.md` を直接読めないセッションがある**: v2.1.277 未満 / feature flag を取得しないセッション(Amazon Bedrock 等の第三者プロバイダ・telemetry 無効など。全条件は公式のリンク先)/ 対応版へのインストール・アップグレード直後の初回セッション / `disableAllHooks`・`allowManagedHooksOnly` の設定 / 内蔵 `agents-md` プラグインの無効化。このとき CLAUDE.md 系だけを読み、`AGENTS.md` は読まれない(**出典ページに警告表示の記載は無い**。これらのセッションでは `/config` に **Project instructions** の項目自体が出ず、それで判別できる。このセッションでの `.claude/rules/` の扱いは出典ページに明記なし)。公式の対処は CLAUDE.md からの `@AGENTS.md` import | 同上(2026-09-21 確認) | 7-4 |
| Claude Code の読み込み対象は **Project instructions** 設定で切り替える(`claude-md-or-agents-md` 既定 / `claude-md-and-agents-md` = 両方を読む / `claude-md` / `managed-only`)。`/config`・`~/.claude/settings.json`・`--settings`・managed settings でのみ効き、**プロジェクト設定・ローカル設定に書いても無視される**(リポジトリ側から切り替えを強制できない) | 同上(2026-09-21 確認) | 3, 7-4 |
| Claude Code が直接読んだ `AGENTS.md` は CLAUDE.md と次の点で違う: `/memory` と `/context` の一覧に出ない / `InstructionsLoaded` hook が発火しない(CLAUDE.md が import・symlink する `AGENTS.md` では従来どおり発火する)/ `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` を設定して `--add-dir` したディレクトリの `AGENTS.md` は読まれない(CLAUDE.md は読まれる)/ 作業ディレクトリ外への `@path` import は承認済みのときだけ読まれる(確認は出ない)。`@AGENTS.md` を import する CLAUDE.md は**残してよく、設定値に関わらず二重読みは起きない**(公式: 他に内容が無ければ削除可。`AGENTS.md` を直接読めないセッションがあるなら残す) | 同上(2026-09-21 確認) | 7-4 |
| Claude Code の `@path` import は公式機能(相対・絶対パス可・最大 4 段。`AGENTS.md` の中でも展開される)。**相対パスは import を書いたファイルの位置から解決される**(したがって `.claude/CLAUDE.md` に書いた `@AGENTS.md` は `.claude/AGENTS.md` を指し、ルートの `AGENTS.md` を指すには `@../AGENTS.md` になる — 公式の記述からの導出・未実測)。symlink も可だが **Windows では管理者権限か開発者モードが必要**なため import が推奨。CLAUDE.md は 200 行以内が目安、4 MiB 超はスキップ、import 先も起動時に全量ロード | [How Claude remembers your project](https://code.claude.com/docs/en/memory)(2026-09-09 確認・2026-09-21 再確認) | 3, 7-4 |
| Codex は AGENTS.md をネイティブに読む。グローバル `~/.codex/AGENTS.md` → プロジェクトルート(通常は Git ルート。見つからなければ cwd のみ)から cwd まで。各階層で `AGENTS.override.md` → `AGENTS.md` → `project_doc_fallback_filenames` の順。root から下へ連結し近い方が上書き | [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | 7-4 |
| Codex は連結後の合計が `project_doc_max_bytes`(既定 32 KiB・設定で変更可)に達すると、以降のファイルを追加しない。公式の対処は上限を上げるか入れ子へ分割 | 同上 | 7-4 |
| Cursor は AGENTS.md をプロジェクトルートとサブディレクトリで読む。位置づけは `.cursor/rules` の**簡易な代替**。入れ子は親と結合し、より具体的な方が優先 | [Rules \| Cursor Docs](https://cursor.com/docs/rules) | 7-4 |
| **Cursor CLI** はプロジェクトルートの `AGENTS.md` と `CLAUDE.md` を読み、`.cursor/rules` と併せてルールとして適用する | [Using Agent in CLI \| Cursor Docs](https://cursor.com/docs/cli/using) | 7-4 |
| Claude Code は `.mcp.json` のプロジェクトスコープサーバーを**対話セッションでのみ**承認を求め、`claude -p` / Agent SDK / クラウド実行では**承認なしに読む**。承認はサーバー名単位(`enabledMcpjsonServers` / `disabledMcpjsonServers`。未承認は `⏸ Pending approval`)。**リポジトリにコミットされた一括承認設定は未信頼フォルダでは無視される**。`.mcp.json` は VCS に入れる前提 | [Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp) | 7-6 |
| `.mcp.json` の stdio サーバーは、トップレベルキー **`mcpServers`** の下にサーバー名をキーとするオブジェクトを置く。各エントリのキーは `command`(**必須**)/ `args`(省略可・配列)/ `env`(省略可・オブジェクト)/ `type`(省略可・既定 stdio)。`command` / `args` / `env` は `${VAR}` 展開に対応する | [Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp)(確認 2026-09-27) | 7-6 |
| Codex はプロジェクトスコープの設定(`.codex/config.toml`)を**信頼済みプロジェクトのときだけ**読む。MCP は `[mcp_servers.<id>]`(`command` / `args` / `env` / `url` / `cwd` / `enabled` / `startup_timeout_sec` 等)。カスタムエージェントは `agents.<name>.config_file` / `agents.<name>.description` で**ロールを宣言**し、**定義本体は `config_file` が指す別の TOML 設定層**に置く(相対パスは宣言した設定ファイルから解決される)。プロジェクト層で無視されるキーの列挙に `agents` は無い | [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) | 7-6 |
| **Codex** の `codex review` を cwd を限定せずに起動したとき、カレントリポジトリ外の兄弟ディレクトリのファイルパスが出力に含まれた(既定サンドボックス〈restricted fs〉は読み取りを制限しなかった。一時ツリー下での越境は未確認) | ローカル実測(2026-09-09・codex 0.153.4) | 7-2 |
| **Cursor CLI** は**ワークスペースの信頼を要求する**。`--trust` を付けないと `Workspace Trust Required` で終了コード 1 になり非対話実行できない。`--trust` はワークスペース信頼のみを与えるもので、コマンド許可の `-f, --force` とは**別物**(`--help` の記載: `--trust`=Trust the current workspace without prompting / `-f, --force`=Force allow commands unless explicitly denied)。読み取り専用は `--mode ask` が保ったまま | ローカル実測(2026-09-09。1 文目は `--trust` 無しでの非対話実行、フラグの意味は `cursor-agent --help`) | 7-2 |
| **Codex** の `codex review` は `--sandbox` も cwd 系フラグ(`-C` / `--cd`)も受け付けない(`-C` を渡すと `error: unexpected argument` で parse に失敗する。option は `-c` / `--strict-config` / `--enable` / `--disable` / `--uncommitted` / `--base` / `--commit` / `--title` と `-h, --help`)。一方 `codex exec` は `-C, --cd <DIR>` と `-s, --sandbox <SANDBOX_MODE>`(`[possible values: read-only, workspace-write, danger-full-access]`)を持つ — **`--cwd` という名前のフラグは持たない**(`--cwd` は `review-agent.sh` 側の引数名)。`codex --help`(トップレベル)にも `-s, --sandbox` と `read-only` が語として存在する。**除外の基準はフラグの有無ではなく「レビュー対象を呼び出し側が与えられない」点**であり、判定への影響は `external-runners.md` §3・§9 に従う | ローカル実測(2026-09-09・codex 0.153.4) | 7-2 |
| **Claude Code** のモデルの性能の段(最上位 / 上位 / 標準)とエイリアスの対応。§5-9 の多モデルレビュー編成・解決表 軸 3 の、持ち場が 1 つのときの既定(「implementer のように、モデルを指定する持ち場が 1 つだけのとき〈§2(a) の集合に属さないもの〉は標準の性能の段を選ぶ」)が参照する。現時点の目安: fable(最上位)+ opus(上位)+ sonnet(標準)。利用可能なエイリアス群はモデルの世代交代で変わるため固定リストとして扱わず、実行時に指定可能なものを確認する | ローカル実測・実行時確認(2026-09-10) | 5-9 |
| **Claude Code** のセッションのモデルの名前と版: ホストがセッションに渡すシステムの情報に、実行中のモデルの名前と版・モデル ID と、系統ごとの最新のモデル(名前と版・モデル ID)の一覧が載る。委託先(サブエージェント)のシステムの情報にも、委託先自身のモデルの名前と版が載る。§5-3 の「起動時の説明と報告に出すモデル名」で、モデルを指定しない委託の確認前の値と、別名の解決(別名と最新の版の対応)と、委託先の自己申告に使う。別名で指定した委託が、この一覧の同じ系統の版で動くことは、ホストの文書では**未確認**(確かめられないときは別名に「(版は未確認)」を添える — 解決表の「委託先の短い説明」) | このリポジトリの作業セッションと、その委託先のシステムの情報で確認(2026-11-04。[Issue #325](https://github.com/Yuki-Maeda-valour/workflow/issues/325)) | 5-3 |
| **Claude Code** の、モデルを指定しない委託のモデルの決まり方: エージェント定義のモデル → 設定した既定のサブエージェントのモデル → 親(セッション)のモデルの順で決まる。したがって、セッションと同じとは限らない。2026-11-04 にこの作業機で、モデルを指定しない読み取り専用の委託が、自己申告でセッションと同じモデル名を返した(エージェント定義や設定で変わりうる) | ホストの委託の道具の説明(モデルの引数の説明。2026-11-04 確認)と、この作業機での委託の自己申告(2026-11-04。[Issue #325](https://github.com/Yuki-Maeda-valour/workflow/issues/325)) | 5-3 |
| **`codex exec resume` は `-s/--sandbox` も `-C/--cd` も受け付けない**(`error: unexpected argument '-s' found` / 同 `'-C'`)。ヘルプにも両フラグと `workspace-write` の語が存在しない | ローカル実測(2026-09-17・codex-cli 0.153.4) | 7-2 |
| `codex --help`(トップレベル)に `-s, --sandbox` と `workspace-write` が実在する(既存の 2 段照合〈argv + ヘルプ〉の機構をそのまま流用できる根拠) | ローカル実測(2026-09-17・codex-cli 0.153.4) | 7-2 |
| **既定の `workspace-write` サンドボックスは `/tmp` と `$TMPDIR` を書き込み可能にしている**(`exclude_slash_tmp` / `exclude_tmpdir_env_var` という除外オプションの存在が証拠)。`$HOME/.cache` 等(リポジトリ外 かつ tmp 外)への書き込みは `Read-only file system` になる | ローカル実測(2026-09-17・codex-cli 0.153.4) | 7-2 |
| **`git hash-object` のクリーンフィルタ(`.gitattributes` の `text` / `eol`、`core.autocrlf`)はリポジトリ内でのみ適用される**。退避先で素朴に計算すると同一バイト列でも値が変わり改竄ゼロでも不一致になる。**だから git のハッシュを使わず生バイトハッシュ(`sha256sum` 相当)を使う**(2026-09-17 決定 39。リポジトリ内でも退避先でも同じ値になり、クリーンフィルタで消える変更も検出できる) | ローカル実測(2026-09-17・git 2.55.0) | 7-2 |
| **実効サンドボックスモードは argv とヘルプだけでは確定できない**(config / permission profile / managed config で上書きされうる)。ローカルの `~/.codex/config.toml` が `sandbox_mode = "danger-full-access"` だった。**2026-09-23 の実測では `sandbox_mode = "workspace-write"`** だった | ローカル実測(2026-09-17・codex-cli 0.153.4。2026-09-23 再確認) | 7-2 |
| **Codex** の設定の優先順位(強い順): CLI フラグ・`-c` → プロジェクトの `.codex/config.toml`(信頼済みのときだけ)→ profile → ユーザー設定 → cloud-managed → system → built-in defaults。`--sandbox workspace-write` は `sandbox_mode` より強い | [Config basics — Codex 公式ドキュメント](https://learn.chatgpt.com/docs/config-file/config-basic)(旧 URL からは 308 で転送される。2026-09-23 確認) | 7-2 |
| **Codex** の `codex exec --sandbox workspace-write` は、設定ファイルの `sandbox_mode` が広い値でも、作業フォルダの外(ホームフォルダ)への書き込みを拒否した(`--sandbox` が `sandbox_mode` より強い、の実機の確かめ)。確かめたのは `sandbox_mode` の上書きを防ぐことだけで、`sandbox_workspace_write.writable_roots`・管理された権限の設定・信頼済みのプロジェクトの設定で書き込み範囲が広がる道は残る。開始前の確認が、所属の印で別のプロジェクトの記録を見分ける前提(外部の実装担当は `--cwd`・`/tmp`・`$TMPDIR` の外へ書かない。external-runners.md §12-2 の注意事項〈35a の前提〉・§12-9-3)の根拠 | ローカル実測(2026-10-31・codex-cli 0.163.0。#291 の設計で、team-lead〈作業全体を進め、結果を確かめる側の AI〉が確かめた) | 7-2 |
| **Codex** の CLI(`--sandbox`)では固定されないキーがある。例: `sandbox_workspace_write.writable_roots`・`network_access`。permission profile(`default_permissions`・`[permissions.<name>]`。Beta)は、旧来の sandbox 設定(`sandbox_mode`・`[sandbox_workspace_write]`)と併用しない。読み込んだ設定ファイルのどれかに `sandbox_mode` がある・`--sandbox` を渡した・選んだ config profile が `sandbox_mode` を持つ、のどれかなら、`default_permissions` ではなく旧来の sandbox 設定を使う。例外は managed の `allowed_permission_profiles`(permission profile を使わせる)。未実測(external-runners.md §12-4 の限界 ⑧) | [Permissions — Codex 公式ドキュメント](https://learn.chatgpt.com/docs/permissions)・[Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) の `default_permissions`(2026-09-24 確認) | 7-2 |
| **Codex** の既定の `workspace-write` でも、書き込み可能な root の下の保護パスは読み取り専用: `<writable_root>/.git` はディレクトリでもファイルでも読み取り専用で、`.git` がポインタファイル(`gitdir: …`)なら解決先も読み取り専用。`.agents` と `.codex` は、ディレクトリとして在るときに読み取り専用(再帰的)。0.153.4 では未実測(external-runners.md §12-4 の限界 ⑧) | [Agent approvals & security — Codex 公式ドキュメント](https://learn.chatgpt.com/docs/agent-approvals-security) の「Protected paths in writable roots」(2026-09-24 確認) | 7-2 |
| **Codex** の `codex exec` は `--output-schema <FILE>`(final response の JSON Schema)を持つ。既定表では採用しない(理由は external-runners.md §4) | ローカル実測(2026-09-18・codex-cli 0.153.4) | 7-2 |
| **Codex** の `codex exec` は、出力の先頭に `OpenAI Codex v<版>` の行と `model: <名前>` の行を出す(プローブの出力にも出る)。`--model`(`-m`)を渡さないときのモデルは設定の値で、利用者の設定 `~/.codex/config.toml` の `model` は、CLI のフラグ・`-c`・プロジェクトの `.codex/config.toml`(信頼済みのとき)・profile で上書きされうる(上の「**Codex** の設定の優先順位」の行)。起動前に見る値は確認前の値で、起動後は実行記録の「モデル」の行を正とする。2026-11-04 の実測では、この 2 行は標準エラーに出た(`review-agent.sh`・`implement-agent.sh` は標準エラー → 標準出力の順に両方を見る)。external-runners.md §4・§12-6 の「プローブの出力から読む形」の根拠 | ローカル実測(2026-11-04・Codex CLI 0.154.0。`review-agent.sh` の実行記録に、モデルと CLI の版を「プローブの標準エラーから読んだ」行が残った。[Issue #325](https://github.com/Yuki-Maeda-valour/workflow/issues/325)) | 5-3・7-2 |
| **Claude Code の `AGENTS.md` の直接の読み込みと `.claude/rules/` の読み込みは併存する**(実測)。上位に CLAUDE.md 系が無い clone で対話セッションを起動すると `agents-md: no CLAUDE.md found; AGENTS.md loaded: <path>` の行が出て、**同じセッションで** `AGENTS.md` と `.claude/rules/claude-code.md` の双方に入れた乱数の合言葉を、ツール呼び出し無しで出所つきで即答した。**未解明**: 同じ clone の 1 回目のセッションでは `AGENTS.md loaded` の行が出ず(rules の合言葉には答えた)、`AGENTS.md` を書き換えた後の 2 回目で出た。原因は特定していない | ローカル実測(2026-09-22・Claude Code v2.1.278・既定値) | 7-4 |
| **Claude Code** のヘッドレス起動: `-p, --print` で非対話に実行して終了する(ワークスペースの信頼ダイアログは出ない。ヘルプは信頼するディレクトリでだけ使うよう注記する。`-p` では、検証に失敗した設定ファイルは黙って無視されるとヘルプに記載がある)。許可の絞り込みは `--allowedTools` / `--disallowedTools`(例 `"Bash(git *) Edit"`)と `--permission-mode`(`acceptEdits` / `auto` / `bypassPermissions` / `manual` / `dontAsk` / `plan`)。`--permission-prompts none` を付けると、`--print` で許可を求める操作は自動で拒否される(拒否の現れ方は下の `--permission-mode` の行で実測した)。`--dangerously-skip-permissions`(全許可)と `--allow-dangerously-skip-permissions` は、ヘルプが**インターネット接続の無いサンドボックスでだけ推奨**する。読み込む設定の出所は `--setting-sources`(`user` / `project` / `local` のカンマ区切り)で選べ、`--strict-mcp-config` は `--mcp-config` 以外の MCP 設定を無視し、`--safe-mode` はカスタマイズ(CLAUDE.md・skill・プラグイン・hook・MCP サーバーなど)を読まずに起動する。`--restricted` は利用者・プロジェクト・ローカルの設定ファイルを読まず(managed settings と `--settings` は効く)、`bypassPermissions` を拒否する。ただし `--tools` で名前を挙げない限り、Bash などコードを実行するツールと WebFetch を外す。MCP サーバーは `--strict-mcp-config` を併せないと読まれ、settings・git・ツール設定ファイルへの書き込みは人か設定済みの承認ハンドラだけが承認できる(ここまでの設定まわりのフラグのうち、`--setting-sources user` と `--strict-mcp-config` の効き方は下の 2026-09-23 決定 16・D13 の行で実測した。**ほかは効き方は未実測**)。`--max-budget-usd` で API 呼び出しの金額に上限をかけられる(`--print` のときだけ) | ローカル実測(2026-09-23・Claude Code 2.1.280 の `--help`) | 2(無人ループ) |
| **Codex** のヘッドレス起動: `codex exec [PROMPT]`。サンドボックスは `-s, --sandbox`(`read-only` / `workspace-write` / `danger-full-access`)。`--approve-for-me` は、承認の要求を `workspace-write` サンドボックスを使う自動審査へ回す。`--dangerously-bypass-approvals-and-sandbox` は確認とサンドボックスを外す(ヘルプは、外部で隔離された環境だけを想定すると注記する)。`--ignore-rules` は利用者とプロジェクトの execpolicy の `.rules` を読まない。`--dangerously-bypass-hook-trust` は hook の信頼の確認を飛ばす。**許可リストを渡す専用のフラグは `exec --help` に無い**(設定値は `-c key=value` で上書きでき、ヘルプの例に `sandbox_permissions` がある) | ローカル実測(2026-09-23・codex-cli 0.153.4 の `exec --help`) | 2(無人ループ) |
| **Cursor CLI** のヘッドレス起動: `cursor-agent -p, --print`(ヘルプは、書き込みとシェルを含む全ツールにアクセスできると注記する)。コマンドの許可は `-f, --force`(別名 `--yolo`)、ワークスペースの信頼は `--trust`(両者は別物。既存の行を参照)。ほかに許可に関わるのは `--sandbox`(`enabled` / `disabled`)・`--auto-review`(安全なツール呼び出しをサーバー側の分類器が自動で実行し、残りは確認を求める)・`--approve-mcps`(MCP サーバーをすべて自動承認)。**許可リストを渡すフラグは `--help` に無い** | ローカル実測(2026-09-23・cursor-agent 2026.09.08 の `--help`) | 2(無人ループ) |
| **無人ループの既定表に Codex・Cursor CLI を載せない根拠**(#68 の決定 1): Codex は既定の `workspace-write` で、書き込み可能な root の下の `.git` を読み取り専用にし(上の保護パスの行)、ネットワークも既定で閉じる。無人のイテレーションの commit・push に全許可が要る見込みで、決定録 2026-09-23 の決定 7(全許可を使わない)と両立しない。Cursor CLI の `--help` には、リポジトリ側の設定を読ませないフラグが無い(決定録 2026-09-23 の決定 16 の手段が無い。フラグの一覧は上の Cursor CLI の行) | [Agent approvals & security — Codex 公式ドキュメント](https://learn.chatgpt.com/docs/agent-approvals-security)(「network access turned off」・保護パス。2026-09-24 確認)・ローカル実測(2026-09-24・cursor-agent 2026.09.08 の `--help`) | 2(無人ループ) |
| **Claude Code** の marketplace から導入したプラグインは、版ごとのディレクトリ(`~/.claude/plugins/cache/<marketplace>/<プラグイン>/<版>/`)に置かれ、旧版のディレクトリも残る(この PC では `valour-workflow/dev-workflow/` の下に 3.7.0・4.0.0・4.2.0)。導入先のパスで `loop.sh` を呼ぶと、プラグインを更新しても古い版が走る(導出) | ローカル実測(2026-09-24) | 2(無人ループ) |
| **Claude Code** の `--plugin-dir <path>` は、そのセッションに限ってディレクトリか .zip からプラグインを読み込む(繰り返し可。プラグインを並べたフォルダを渡すと、下のディレクトリをそれぞれ読む)。`--output-format` は `--print` のときだけ効き、`json` は単一の結果、`stream-json` は逐次の流れ(ヘルプの記載: `"json" (single result)`。利用者の設定の `verbose` のもとでは全メッセージの配列になる — 下の JSON の結果の行)。`--strict-mcp-config` は `--mcp-config` 以外の MCP 設定をすべて無視する(2026-09-23 決定 16 の 2 つのフラグを併せた起動で、利用者の側の MCP〈claude.ai のコネクタ 1 件〉も外れたことを実測した — 下の D13 の行。`--strict-mcp-config` 単独の効き方は未実測) | ローカル実測(2026-09-23・Claude Code 2.1.280 の `--help`) | 2(無人ループ) |
| **Claude Code** の補助のサブコマンド(セッションを起動しない): `auth status` は認証の状態を出す(出力は `--json` が既定で、`--text` もある)。ログイン済みは exit 0・`loggedIn: true`・`authMethod: claude.ai`。未ログイン(`CLAUDE_CONFIG_DIR` で資格情報を外した)は exit 1・`loggedIn: false`・`authMethod: none`(渡した空のディレクトリに `.claude.json`・`backups` ができる)。**OAuth の期限切れで更新できない認証は見抜けない**: exit 0・`loggedIn: true` のまま、その後のセッションは「OAuth session expired and could not be refreshed」で失敗した(result は `is_error: true`・`terminal_reason: api_error`)。`plugin list --json` はインストール済みのプラグインの一覧を出す。トップは配列で、要素のキーは `id`(`<名>@<marketplace>`)・`version`・`scope`・`enabled`・`installPath`(導入先のキャッシュのパスで、版を含む)・`installedAt`・`lastUpdated`。`name` のキーは無い | ローカル実測(2026-09-24・Claude Code 2.1.281 の `auth status --help`・`plugin list --help`)・ローカル実測(2026-09-25〜26・Claude Code 2.1.281。#68 の実走。プローブ P0・P4・手順 3) | 2(無人ループ) |
| **Claude Code** は、Bash ツールのコマンドを呼び出しごとに別のセッションで起動する(そのシェルは PGID = SID = 自分の PID で、ホストのプロセスグループと別)。したがって、ホスト CLI のプロセスグループへのシグナルは、ツールのコマンドが起動した子孫に届かない(導出。`loop.sh` の片付けが、イテレーションの印の環境変数でプロセスを探す理由) | ローカル実測(2026-09-24・Claude Code 2.1.281。`ps -o pid,pgid,sid`) | 2(無人ループ) |
| linked worktree で `git push -u origin <ブランチ>` を実行すると、共有の `config`(`git rev-parse --git-common-dir` の下)に `branch.<ブランチ>.remote=origin` と `branch.<ブランチ>.merge=refs/heads/<ブランチ>` が書かれる。`git gc` は共有の `info/refs` を作る。linked worktree からは、共有の `refs/heads/*`・`config`・`hooks/` を書き換えられる。#68 の実走では、共有の `config` にイテレーションが書いたのは、PR になったイテレーションの `git push -u` によるこの 2 項目(`branch.task/{名}.remote`・`branch.task/{名}.merge`)だけだった | ローカル実測(2026-09-24・git 2.55.0)・ローカル実測(2026-09-25〜26・Claude Code 2.1.281。#68 の実走。手順 7 の X・R) | 2(無人ループ) |
| **Claude Code** の保護パス: `.git`・`.config/git`・`.vscode`・`.idea`・`.husky`・`.cargo`・`.devcontainer`・`.yarn`・`.mvn`・`.claude`(`.claude/worktrees` を除く)のディレクトリと、`.gitconfig`・`.gitmodules`・シェルの設定(`.bashrc`・`.zshrc`・`.profile` など)・`.npmrc`・`.pre-commit-config.yaml`・`.mcp.json`・`.claude.json` などのファイルへの書き込みは、`default`・`acceptEdits` では確認に回り、`dontAsk` では拒否、`auto` では分類器、`bypassPermissions` では許可。settings の `permissions.allow` は保護パスへの書き込みを事前に許可しない(安全の検査が allow ルールより先に走る)。`acceptEdits` が自動で許すファイル操作のコマンド(`mkdir`・`touch`・`mv`・`cp` など)も、保護パスへの書き込みは確認に回る。Bash の出力のリダイレクト先(`>`・`>>`・`2>`)と `tee` の書き込み先も、保護パスの検査に掛かる(スクリプトの中の書き込みはコマンドの行に現れないので掛からない — 導出)。無人のイテレーションの `.claude/reviews/` への書き込みが確認に回り、`--permission-prompts none` で拒否になる根拠(#68 の決定 7) | [Choose a permission mode](https://code.claude.com/docs/en/permission-modes) の「Protected paths」・[Configure permissions](https://code.claude.com/docs/en/permissions) の出力のリダイレクトと `tee` の検査(2026-09-24 確認) | 2(無人ループ) |
| **Claude Code** の自動メモリは `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` で無効化できる。`loop.sh` は、ホストを子プロセスとして起動するときの共通の環境で指定する。機能無効化は任意のファイル操作の禁止ではない。Linux / 2.1.289 の認証済みの実機での確認で設定・リポジトリ・メモリを隔離し、`0` では自動読み込みと保存、`1` では読み込み・保存が行われないことを確認した。実ループと実ホストを組み合わせた通し実行、macOS での機能効果は未確認。下の 2.1.281 の書き込み観測は無効化を入れる前の記録 | [公式 memory](https://code.claude.com/docs/en/memory#enable-or-disable-auto-memory)(2026-10-06 確認)、[Issue #151](https://github.com/Yuki-Maeda-valour/workflow/issues/151)(同日・Linux の隔離した実機での確認。条件と手順は loop.md §8) | 2(無人ループ) |
| **Claude Code** の `PermissionRequest` の hook: 確認に回る呼び出しで呼ばれ、`tool_name`・`tool_input`(共通の入力の `cwd` も)を受け取り、`hookSpecificOutput.decision.behavior` に `allow` / `deny` を返せる。確認を出せないセッション(非対話のバックグラウンドのサブエージェントなど)でも呼ばれ、どの hook も判断を返さなければ拒否になる。一致する hook は並列にすべて動く(利用者の側の hook とも重なる)。`disableAllHooks: true` は hook を止め(管理者設定の hook は管理者設定からだけ止められる)、管理者設定の `allowManagedHooksOnly` は利用者・プロジェクト・ローカル・プラグインの hook を止める。`--permission-prompts none` のもとでは、確認に回る操作は `PermissionRequest` の hook が許さない限り拒否される(許可ルール・`PermissionRequest` の hook・permission mode が先に判断し、何も解決しないものだけを拒否する)。`--permission-prompt-tool <MCP のツール>` でも確認に答えさせられるが、最初のターンの前にその MCP サーバーの接続を待つ(既定 30 秒まで)。#68 の許可の仲介(D22)は hook で行い、`--permission-prompt-tool` は使わない(接続待ちが要らず、`--permission-prompts none` を保てるため)。効き方の実測は下の D22 の行(並列に動くこと・判断を返さない hook のときの拒否・`disableAllHooks`・`allowManagedHooksOnly`・`--permission-prompt-tool` の効き方は未実測) | [Hooks reference](https://code.claude.com/docs/en/hooks)・[Run Claude Code programmatically](https://code.claude.com/docs/en/headless) の「Turn off permission prompts in unattended runs」・[CLI reference](https://code.claude.com/docs/en/cli-reference)(2026-09-24 確認) | 2(無人ループ) |
| **Claude Code** の skill の frontmatter の `disallowed-tools` は、その skill を呼んだ後のターンの残りにも効き、呼び出し側の skill の `Edit` を止める。`-p` の実行は全体が 1 ターンなので、以後の `Edit` がすべて「Permission to use Edit has been denied.」で拒否される(`non_execution_kind: permission-rule`。許可の仲介の hook は呼ばれない)。このため understand-project は frontmatter で `Edit`・`NotebookEdit` を禁じず、本文の原則で読み取り専用を守る。公式文書は、`disallowed-tools` を「その skill が有効な間、使えるツールから外す(次のメッセージを送ると解ける)」、`allowed-tools` を「その skill を呼んだターンの間、確認なしで使えるツール(次のメッセージを送ると消える。ほかのツールは許可の設定のまま)」とする。`allowed-tools` は、skill を呼んだ同じターンで、skill が指示した Bash の呼び出しを hook を通さずに通すことを #193 で実測した(下の 2.1.289 の行。skill の指示と関係の無い呼び出しで効くかは分けて確かめていない) | ローカル実測(2026-09-25・Claude Code 2.1.281。#68 のプローブ P6〈understand-project を呼ばないイテレーションでは `Edit`・`Write` がすべて通る〉・P7〈`Edit` → understand-project → `Edit` で 2 回目だけ拒否〉)・[Extend Claude with skills](https://code.claude.com/docs/en/skills) の frontmatter の項(2026-09-25 確認) | 6・2(無人ループ) |
| **Claude Code** には、プラグインの導入・更新・有効化の時に動く hook が無い(hook が動く時は 33 個。`Setup` は `--init-only` などの起動の引数で動く)。導入・更新の時に自動で動くのは Node.js の依存の導入だけで、プラグインのコードは動かさない。新しい版は、次のセッションの起動か `/reload-plugins` で読み込まれる。`SessionStart` の hook の、JSON でない標準出力は Claude にだけ渡り、JSON の `systemMessage` は利用者に見せる。hook には `CLAUDE_PLUGIN_ROOT`・`CLAUDE_PLUGIN_DATA` が渡る。command の hook の時間の上限の既定は 600 秒 | [Hooks reference](https://code.claude.com/docs/en/hooks)・[Plugin loading reference](https://code.claude.com/docs/en/plugins/loading)・[Plugin manifest reference](https://code.claude.com/docs/en/plugins-reference)(2026-11-07 確認。Claude Code 2.1.319) | 2(無人ループの hook の例外)・道具の版の確かめ(runtime-requirements.md。#431) |
| **Claude Code** の `--bare` は、設定と導入済み plugin の hook・skill・plugin・MCP・自動メモリ・CLAUDE.md の自動の読み込みを飛ばし、OAuth と keychain を読まない(API キーか `--settings` の `apiKeyHelper` が要る)。公式文書は、`--bare` を scripted・SDK の呼び出しに推奨し、将来 `-p` の既定にするとする(原文: "`--bare` is the recommended mode for scripted and SDK calls, and will become the default for `-p` in a future release.")。ホスト CLI の更新だけで、認証が外れうる(#193 の実測では「Not logged in」で失敗した)。起動引数(`--settings`)で渡す許可の仲介の hook がこの起動で動くかは、公式文書に無い(bare で読み込める手段の表は `--settings` を挙げる。2.1.289 の実行ファイルを静的に読むと、bare では設定ファイル・起動引数・管理設定・plugin の hook が動かないとする説明文がある。実ホストでは認証の失敗で観察できず未確認。#107 H31)。`loop.sh` は `--version`・`--help` の出力と実 hook の確認を証明に結び付け、変われば確かめ直すまで使わない | [Run Claude Code programmatically](https://code.claude.com/docs/en/headless) の「Start faster with bare mode」(2026-10-07 確認) | 2(無人ループ) |
| **Claude Code** の環境変数 `CLAUDE_CODE_SIMPLE` と `CLAUDE_CODE_SAFE_MODE` は、どちらもホストの読み込みを変える(hook の自動の読み込みを止める)。`CLAUDE_CODE_SIMPLE=1` は `--bare` と同じで、hook・skill・plugin・MCP・CLAUDE.md の自動の読み込みを止め、OAuth と keychain を読まない(原文: "Disables auto-discovery of hooks, skills, custom commands, subagents, installed plugins, MCP servers, auto memory, and CLAUDE.md." / "Equivalent to passing `--bare`")。`CLAUDE_CODE_SAFE_MODE=1` は安全モードで、CLAUDE.md・skill・plugin・hook・MCP・自動メモリなどを読み込まない(管理設定の hook は残る。子プロセスに引き継がれる)。起動引数で渡す hook が動くかは原文に無い(未確認)。`loop.sh` は起動時の非空を exit 20(`hooks-disabled`)で止め、利用者・Linux 管理設定・cache の `env` にあるこの2キーは、最初の補助ホスト CLI より前に値を問わず exit 20(`host-settings`)で拒否する(#229) | [Environment variables](https://code.claude.com/docs/en/env-vars) の `CLAUDE_CODE_SIMPLE`・`CLAUDE_CODE_SAFE_MODE` の行(2026-10-08 確認) | 2(無人ループ) |
| **Claude Code** の環境変数 `CLAUDE_CODE_SHELL_PREFIX` は、ホストが起動するシェルのコマンド(Bash の道具・hook のコマンド・stdio の MCP サーバの起動など)を包む。PowerShell の hook と exec の形の hook は包まない。`loop.sh` は起動時の非空を exit 20(`shell-prefix`)で止め、利用者・Linux 管理設定・cache の `env` にあるこのキーは、最初の補助ホスト CLI より前に値を問わず exit 20(`host-settings`)で拒否する(#229)。残る実測範囲は [loop.md §10](../plugins/dev-workflow/skills/ship-task/references/loop.md#10-受け入れる限界) | [Environment variables](https://code.claude.com/docs/en/env-vars) の `CLAUDE_CODE_SHELL_PREFIX` の行(2026-10-08 確認) | 2(無人ループ) |
| **Claude Code** の環境変数 `CLAUDE_CONFIG_DIR` は設定ディレクトリ(既定は `~/.claude`)を置き換え、シェル・利用者の設定・管理設定の `env` で設定できる(原文: "Set it in your shell, user settings, or managed settings. Ignored in project and local settings")。公式文書は、設定の `env` の値をプロセスの環境に書き、シェルの値を置き換えるとする(多くのセッションで。例外は settings-reference)。`loop.sh` はキー不在だけ `HOME/.claude` を使い、HOME とともに空でない絶対 NFC パス(NFC は、同じ文字を表す別の並びを一つの形にそろえる Unicode の正規化方式)を要求する。先頭の `//` と `..` 成分は拒否し、内部の `//`・`./`・末尾の `/` は正規化して受け付ける。設定の `env` による読み込み先の変更は、最初の補助ホスト CLI より前に値を問わず exit 20(`host-settings`)で拒否する。ホスト公式の能力と loop が受け入れる条件は別であり、残る将来配信・実測範囲は [loop.md §10](../plugins/dev-workflow/skills/ship-task/references/loop.md#10-受け入れる限界)に記す | [Environment variables](https://code.claude.com/docs/en/env-vars) の `CLAUDE_CONFIG_DIR` の行と「Precedence」の項(2026-10-08 確認) | 2(無人ループ) |
| **Claude Code** の Linux 管理設定は `managed-settings.json` と `managed-settings.d` の非隠し直下 `.json` を扱う。設定の `env` は管理ソース間でもキー単位で補われるため、loop は最終マージだけでなく各保持元を調べる。server 管理設定は起動時と定期的に取得され、`remote-settings.json` の控えを使うが、未取得の応答や cache に保存されない `-p` 用応答はローカル控えだけでは固定できない。`policyHelper` の出力は管理設定を置き換え、server 設定が外れたときに途中で動く場合もある。loop は保持した設定の helper キーを拒否するが、正規 server 管理者を信頼する契約は維持する | [Deploy managed settings](https://code.claude.com/docs/en/managed-settings)・[Configure server-managed settings](https://code.claude.com/docs/en/server-managed-settings)(2026-10-08 確認) | 2(無人ループ) |
| H50 の Linux 実測(Claude Code 2.1.293): 正常設定の `--prove-host` と `--dry-run` は通った。利用者設定と cache の各4禁止キー、計8件は loop が exit 20(`host-settings`)で拒否し、ホスト CLI の呼出しは0件だった。個人プランでの cache 非適用の観察があるため、ホストへの設定適用や許可のすり抜けの実証ではない。他 OS・他プラン・未取得の管理設定へ一般化しない | [Issue #229](https://github.com/Yuki-Maeda-valour/workflow/issues/229)(2026-10-08 実測) | 2(無人ループ) |
| H46 の Linux 実測(Claude Code 2.1.293): 危険な追加定義8件と固定設定3キーの不正な値は、ホスト呼出し0件で拒否した。選択された marketplace 項目の権限追加は補助 CLI の後、セッション0件で拒否した。正常な skill-folder plugin・agent と既知の組込み名と同名の外部 skill は証明を作れた。正常な dry-run も通り、正常 fixture の plugin MCP は起動しなかった。設定・cache を復元し、子孫の停止と fixture の撤去を確認した。管理側は追加 root の fixture で調べ、実 system 設定は変更していない。他 OS・将来の server 応答へ一般化しない | [Issue #231](https://github.com/Yuki-Maeda-valour/workflow/issues/231)(2026-10-08 実測) | 2(無人ループ) |
| Claude Code 2.1.293 では、従来の plugin command(通常・指定 directory・指定 file・対応表の inline/source)は初期応答の `skills` に出ず、`slash_commands` に出た。loop は command を権限検査と呼出名の解決に含め、証明で比較する公開 skill 名から外す。名前だけでは、同名の未知の組込み定義と外部定義の出所を区別できない | [Issue #231](https://github.com/Yuki-Maeda-valour/workflow/issues/231)(2026-10-08 実測) | 2(無人ループ) |
| H47 の 2.1.293 調査では、CLI/settings の裸の Read/Grep/Glob/Write/Edit/NotebookEdit が outside 呼出しを hook なしで通した。比べるために先行 Read を揃えた Edit/NotebookEdit、cwd Read、scope 内の Read と範囲外の拒否、検索道具の復帰と outside の拒否を区別した。道具未提供・未呼出し・先行 Read 不成立を成功に数えない。旧 MultiEdit は互換 parser の回帰テストの対象で、現行ホストの実呼出しを確認したとは扱わない | [Issue #232](https://github.com/Yuki-Maeda-valour/workflow/issues/232)(2026-10-08 調査。実装後の独立検証は同 Issue の記録を参照) | 2(無人ループ) |
| 同じホスト実体の静的解析では、許可規則の括弧内がちょうど `*` の形を道具名だけの規則へ変換する。H47 はこの形も拒否する。既知の入力欄を使う `Read(file_path:*)` 等を path と推定せず拒否するが、任意の `identifier:` を一律に入力欄扱いしない。通常の path の字面・起点は変えない | [Issue #232](https://github.com/Yuki-Maeda-valour/workflow/issues/232)(2026-10-08 静的解析・回帰テスト) | 2(無人ループ) |
| Linux の管理側 command は、2.1.293 の静的な loader 解析で `/etc/claude-code/.claude/commands` を policySettings として読み込む。v4.24.6 ではこの root の保持が漏れていたため、H47 で固定 root と追加 managed root の commands を保持対象へ追加した。正常 command・名前の優先順位・危険な権限と hook の拒否・証明変更の検知は追加 root の fixture で確認する。実 system root を変更した実ホスト確認ではない | [Issue #231](https://github.com/Yuki-Maeda-valour/workflow/issues/231)・[Issue #232](https://github.com/Yuki-Maeda-valour/workflow/issues/232)(2026-10-08 静的解析・回帰テスト) | 2(無人ループ) | <!-- validate-allow: 検査対象となるホストの固定管理パスを外部事実として記録する -->
| **Claude Code** の環境変数 `DISABLE_AUTOUPDATER=1` は自動の更新を止める(原文: "Set to `1` to disable automatic background updates. Manual `claude update` still works.")。native の導入(ホスト CLI を単体の実行ファイルとして入れる導入)の実体は `~/.local/share/claude/versions/<版>` のファイルそのもの(#193 の実ホスト確認で、2.1.289 の ELF を見た)。更新の後の置き場の形は未確認 | [Environment variables](https://code.claude.com/docs/en/env-vars) の `DISABLE_AUTOUPDATER` の行(2026-10-07 確認)・ローカル実測(Issue #193) | 2(無人ループ) |
| **Claude Code** の plugin の mod(ホストの中で動く JavaScript の関数を hook として登録する plugin)が `tool.check` を扱うと、許可規則と `PreToolUse` の hook が決めた後に答え、その答えで置き換えられる(原文: "A mod you install that handles `tool.check` answers after the rules and the `PreToolUse` hooks have decided, and its answer can replace theirs")。管理設定のある PC か、Team・Enterprise のプランのログインでは、既定で deny 規則が mod より強い。それ以外では、mod は deny 規則が拒否する呼び出しも承認できる。`loop.sh` は保持した有効な定義の非空の `modules` をセッション前に拒否する(検査範囲と限界は loop.md §4・§10。#231) | [Configure permissions](https://code.claude.com/docs/en/permissions) の「Extend permissions with hooks」・[Hooks reference](https://code.claude.com/docs/en/hooks) の冒頭(mod の定義。2026-10-07 確認) | 2(無人ループ) |
| **Claude Code** の skill の `allowed-tools` は、その skill を呼んだターンの間、確認なしで使える道具を足す(次のメッセージで消える。ほかの道具は許可の設定のまま)。値は空白かカンマ区切りの文字列か YAML の列。frontmatter は、開きの `---` がファイルの 1 行目のときだけ読み、YAML を解析できなければ項目なしで読み込む(文書の記載。#193 のレビューで 2.1.289 の実行ファイルを静的に読むと、開きの後ろに任意の空白文字を許して行の途中の `---` で閉じる切り出し方と、閉じの前に改行を要する切り出し方の 2 つの正規表現、鍵を `-`・`_` を除いた小文字で比べる表、解析に失敗したときに値を引用して読み直す処理、`allowed-tools` の値を括弧の中かどうかを真偽の 1 つで持って ' ' と ',' で区切る関数(入れ子の括弧では、内側の `)` の後ろで切れる)、command の `.md` を大文字小文字を区別せずに読む走査、設定の許可規則を整えずに読む処理、plugin のマニフェストの `commands` の対応表の `allowedTools`・インラインの `content` を command の `allowed-tools`・frontmatter に合成する処理があった。どれを skill に使うかは一部未確認)。ワークスペースの信頼はこの鍵を制限せず、未信頼のフォルダの `-p` でもプロジェクトの skill の `allowed-tools` を適用する。管理設定の `allowManagedPermissionRulesOnly` は、プロジェクト・個人の skill の `allowed-tools` を無視させる(v2.1.282 以降)。個人・プロジェクトの skill のフォルダは symlink でもよく、対象の `SKILL.md` を読む。claude.ai の skill は `~/.claude/skills/synced/` に落とされ、セッション中も約 10 分ごとに同期される。利用者の設定の `syncClaudeAiSkills: false` で同期を止める。ホストは、bare の起動を除き、skill のディレクトリ(`~/.claude/skills/` など)の追加・編集・削除をセッションの中で拾う(原文: "Claude Code watches skill directories for file changes, except in bare mode." / "Claude Code picks up the change within the current session, without a restart.") | [Extend Claude with skills](https://code.claude.com/docs/en/skills)(「Pre-approve tools for a skill」・frontmatter の項・「Where skills live」・同期の項。2026-10-07 確認) | 2(無人ループ) |
| **Claude Code** の Bash の許可規則は、複合コマンドを分けた各部分に当てる(`&&`・`||`・`;`・`|`・`|&`・`&`・改行。後ろの無い `&&` は分けずに解析できない扱い)。照合の前に包み(`timeout`・`time`・`nice`・`nohup`・`stdbuf`・`command`・`builtin`・`noglob`・フラグの無い `xargs`)と、安全と分かっている環境変数の代入を外す。exec の包み(`watch`・`setsid`・`ionice`・`flock`)と `find` の `-exec`・`-delete` は prefix の規則では承認されず、全体の字面の exact の規則で承認する。末尾の ` *` と `:*` は同値で、裸のコマンドにも当たる。`*` の前の字面は書いたとおりに照合する。単独の `/` で始まる path の規則は、定義した場所で起点が変わる(利用者の設定は `~/.claude`、起動引数は作業ディレクトリ)。`//` は絶対パス。許可規則の道具の名の glob は `mcp__<server>__` の後ろだけ | [Configure permissions](https://code.claude.com/docs/en/permissions)(Wildcard patterns・Compound commands・Wrappers・path の表・Tool name wildcards。2026-10-07 確認) | 2(無人ループ。H34 の包含の判定) |
| **Claude Code** の auto(分類器)のモードに入ると、任意のコード実行を許す広い許可規則(`Bash(*)`・ワイルドカードつきの interpreter(スクリプトを実行するプログラム)・パッケージマネージャの run・委託の道具・`Monitor`)を外す。狭い規則(`Bash(npm test)` など)は残る | [Choose a permission mode](https://code.claude.com/docs/en/permission-modes) の auto の項(2026-10-07 確認) | 2(無人ループ。H34 の `--allow-classifier`) |
| **Claude Code** の許可の仲介の hook と skill の `allowed-tools` の実測(#193。H31・H34): イテレーションと同じ起動(`loop.sh --prove-host`。比べるために直接実行した起動は `--plugin-dir` を除く同じフラグ: `-p --output-format json --setting-sources user --strict-mcp-config --permission-mode acceptEdits --permission-prompts none --settings <hook>`)で、cwd の外への `Write` は `PermissionRequest` の hook に届き、hook の拒否で書かれなかった(記録・結果の拒否の欄・ファイルの無さがそろう)。hook を渡さない起動では、同じ `Write` が自動で拒否され(拒否の欄には載る)、hook の記録は無い。`--bare` は OAuth を読まず「Not logged in」で失敗した。`--permission-mode auto` では、同じ `Write` を分類器が hook を通さずに許し、ファイルが書かれた(このため `loop.sh` は `--allow-classifier` を拒否する。#107 の H48)。利用者の skill の `allowed-tools`(`Bash(python3:*)`・exact・`Bash`)は、スラッシュコマンドで呼んだターンで hook を通らずにコマンドを通した(比べるための、`allowed-tools` を持たない skill では hook に届いて拒否)。`allowed-tools` を持つ skill を Skill の道具で呼ぶと、その呼び出しが `PermissionRequest` に回る(持たない skill は回らない)。許可されると同じく効いた。`loop-permission.py` は Skill を「扱わないツール」で拒否した。cwd の外に書く `touch` は、`Bash(touch:*)` を足しても hook に回った(`Bash` の全体なら通った)。リポジトリ側の skill は `--setting-sources user` で読まれなかった(隔離なしでは読まれた)。`--allowedTools Grep` を足すと、cwd の外の `Grep` が hook を通らずに通った | ローカル実測(2026-10-07・Claude Code 2.1.289 の native のコピー。専用の `CLAUDE_CONFIG_DIR` に claude.ai でログイン。scratch の HOME・リポジトリ。記録は Issue #193) | 2(無人ループ) |
| **Claude Code** の `--setting-sources user`・`--strict-mcp-config`(2026-09-23 決定 16 の手段。#68 の D12)は、リポジトリ側の hook と `.mcp.json` の MCP を効かせない。比べるための起動(隔離のフラグなし。効くはずのものが効くかを見る)では、リポジトリ側の hook(SessionStart・UserPromptSubmit)と `.mcp.json` の MCP がそれぞれ残す印が両方できた。隔離のフラグつきの本番 5 回では、どちらの印もできなかった。利用者の設定(`~/.claude/settings.json`)の hook は、このフラグのもとでも動く(下の D22 の行) | ローカル実測(2026-09-25〜26・Claude Code 2.1.281。#68 の実走。プローブ P0・手順 7) | 2(無人ループ) |
| **Claude Code** のリポジトリ側の skill・エージェント定義・コマンド(#68 の D13)は、`--setting-sources user` のもとでは読まれない(起動時の init のメッセージの一覧で確かめた)。隔離なしでは、`.claude/skills/` の skill が `skills` と `slash_commands` に、`.claude/agents/` の定義が `agents` に、`.claude/commands/` のコマンドが `slash_commands` に出る。隔離あり(2026-09-23 決定 16 のフラグ。`--plugin-dir` つき)では 3 つとも出ず、`mcp_servers` は 0 件(利用者の側の MCP も外れる)、`plugins` には `--plugin-dir` の作業ツリー(`dev-workflow@inline`)が出る。CLAUDE.md・`.claude/rules/` は、隔離なしでは読まれ(入れた合言葉を答えた)、隔離ありでは読まれない。あわせて、プロジェクトの設定を無効にすると、下の階層の `.claude/skills` を探す処理が飛ばされる(実行ファイルのログの文言) | ローカル実測(2026-09-25〜26・Claude Code 2.1.281。#68 の実走。プローブ P1a・P1b・P1)。最後の 1 文は #68 の設計レビューでの checker の調べ(実行ファイルのログの文言) | 2(無人ループ) |
| **Claude Code** の `-p --output-format json` の結果の拒否の欄 `permission_denials` は `{tool_name, tool_use_id, tool_input}` の配列で、`PermissionRequest` の hook の拒否も入る(system の `permission_denied` のメッセージの `decision_reason_type` は、hook の拒否で `hook`、`--permission-prompts none` による自動の拒否で `asyncAgent`)。利用者の設定の `"verbose": true` は `--setting-sources user` のもとでも読まれ、出力は結果 1 つのオブジェクトではなく全メッセージの配列(最後の要素が `type: "result"`)になる。イテレーションの中でバックグラウンドに委託すると、`type: "result"` の要素がターンごとに出て(2 つ目以降は `origin.kind: task-notification`)、拒否はそのターンの要素にだけ載る | ローカル実測(2026-09-25〜26・Claude Code 2.1.281。#68 の実走。プローブ P2b・P3、本番の 1 回目・3 回目、手順 8 の T1) | 2(無人ループ) |
| **Claude Code** の `--permission-mode` と設定の `defaultMode` は、起動引数が勝つ(`--settings` で `defaultMode: bypassPermissions` を渡したとき、`--permission-mode` なしの init の `permissionMode` は `bypassPermissions`、`--permission-mode acceptEdits` つきは `acceptEdits`)。`--permission-prompts none` のもとで確認に回った呼び出しは、「no approval surface in this session; permission request denied automatically」で拒否される(ツールの結果は `is_error: true`。モデルへの文言は再試行しないよう求める) | ローカル実測(2026-09-25〜26・Claude Code 2.1.281。#68 の実走。プローブ P2a・P2b) | 2(無人ループ) |
| `loop.sh` の時間切れと片付け(Claude Code のイテレーション): イテレーションの中で `setsid` で切り離した孫プロセス(確かめるために置いた仕掛けの 2 つ)は、イテレーションの終わりの片付けで止まっていた。`--iteration-timeout 60` のイテレーションは 60 秒で打ち切られ(所要 61 秒)、worktree は lock つきで残り、イテレーションの印を持つプロセスは残らなかった | ローカル実測(2026-09-25〜26・Claude Code 2.1.281。#68 の実走。手順 8 の T1・T2) | 2(無人ループ) |
| #68 の実走の許可リスト(`loop.sh` の `--allowed-tools`): git・gh・rtk・python3・bash・test・sha256sum・読み取り系・echo など。**Claude Code** では利用者の設定の `permissions.allow` も和として効く(`Bash(curl:*)` があると、許可リストに無い `curl --version` も確認に回らずに通った)。許可リストに無いコマンド(`dd`)は確認に回り、許可の仲介が拒否した。`rtk` は、利用者の側の hook が `git` などを `rtk …` に書き換えて許可の仲介に届くため、本番の 1 回目の後に足した | ローカル実測(2026-09-25〜26・Claude Code 2.1.281。#68 の実走。プローブ P3・P5、本番の 1 回目) | 2(無人ループ) |
| **Claude Code** の `PermissionRequest` の hook(#68 の D22 の許可の仲介)の効き方: `--setting-sources user` のもとでも `--settings` の hook が呼ばれる。保護パス(`.claude/reviews/` など)への書き込みは確認に回って hook が判定し、hook が無い、比べるための起動では「no approval surface …」で拒否されて記録も残らない。`reviews-dir.sh` の呼び出しは確認に回らずに通る。`CDPATH= cd -P -- … && pwd -P` は確認に回って hook が判定し、`CDPATH=` も `-P` も付けない `cd` は確認に回らない。入力の `cwd` は `cd` で移った先で、`CDPATH=` も `-P` も付けない `cd` の移動も次の呼び出しに残る(`pwd` が移った先を出す)。イテレーションの中の委託(前景・バックグラウンドのサブエージェント)の Bash も同じ hook を通る。`test`・`[` の偽(終了コード 1)は「Condition is false」(`returnCodeInterpretation`)として扱われ、ツールの結果は `is_error: false`・出力なしで、モデルに偽が見えない(単独の呼び出しのほかの終了コード〈21〉は見える。真偽は `&& echo yes || echo no` で出力に出す)。利用者の側の hook はイテレーションの中でも動く: PreToolUse の hook(rtk)は、扱える形のコマンドを `rtk …` に書き換えて allow を返し(許可の判定と許可の仲介の hook を飛ばす)、リダイレクトつき・`test` などは素通しし、扱いきれない複合のコマンドは書き換えだけして通常の判定に回す(許可の仲介には書き換えた形で届く)。利用者の設定の SessionStart の hook は `.claude/.understand-project-done` を消し、UserPromptSubmit の hook は `CLAUDE.md` か `.claude/project-profile.yml` があるときに作り直して、understand-project を促す文をイテレーションのモデルに入れる。hook の書き込みはツールを通らないので許可の仲介に掛からない。自動メモリの置き場への書き込みは `Write` のツールを通るが、確認に回らずに通るので許可の仲介に掛からない(本番の 1 回目のイテレーション 2) | ローカル実測(2026-09-25〜26・Claude Code 2.1.281。#68 の実走。プローブ P3・P5、本番の 1 回目・3 回目・4 回目) | 2(無人ループ) |
| **Codex Desktop**(Codex のデスクトップアプリ)は、Claude Code のセッションを取り込むときに、リポジトリ側の Claude Code の設定(`.claude/settings.json` の hook・`.mcp.json`)を Codex の形に変換した `.codex/config.toml`・`.codex/hooks.json` を、取り込んだセッションの cwd(イテレーションの worktree)と、同じリポジトリの人のチェックアウトに書いた。プローブ用の別のディレクトリには `AGENTS.md`(CLAUDE.md からのコピー)・`.agents/skills/…`・`.codex/agents/…` を書いた(どれも未追跡)。`loop.sh` では防げない | ローカル実測(2026-09-26・#68 の実走の 4 回目と 5 回目の間) | 2(無人ループ) |
| **gh** の対象のリポジトリ: 引数なしの `gh repo view`・`gh pr create` は、gh が選ぶ既定のリポジトリ(`upstream`・`github` の名のリモートが origin より優先・`GH_REPO`・`gh repo set-default`)を対象にする(scratch の clone で、origin=cli/cli・upstream=git/git のとき、引数なしの `gh repo view` は git/git を返した)。`gh repo view <引数>`・`-R <引数>` は、https・`ssh://`・scp の形の URL と `HOST/OWNER/REPO` を受け付け、引数は `GH_REPO`・`GH_HOST` より優先される。#69 の発見のイテレーションが公開の確認と PR を、#133 のタスクのイテレーションが PR の作成先の確かめ(`gh repo view '<repo>'`)と PR(ship-task の Phase 5 の `gh pr create -R '<repo>'`。`<repo>` は unattended-mode.md §7 の控える値の origin の判定の `repo`)を、push 先に結び付ける根拠(`origin-repo.py` の `repo` を引数で渡す) | ローカル実測(2026-09-26・gh 2.74.0) | 2(無人ループ・発見ループ)・ship-task の Phase 5(無人のタスクのイテレーションの PR) |
| **OpenSSH** の `ssh -G <接続先>` は、接続せずに設定を解いた結果(`hostname`・`port` など)を出す。`Match user` の条件は、接続先にユーザー名を付けたとき(`git@github.com`)だけ効く(`Match user git` で `Port 2222` の設定では、`ssh -G -- git@github.com` は port 2222、`ssh -G -- github.com` は port 22)。`Match exec` のコマンドは `-G` でも実行される。git の実際の接続は、ほかにも `core.sshCommand`・`GIT_SSH_COMMAND`・`GIT_SSH`・`remote.<名>.vcs` で変わる。#69 が ssh の push 先を、上書きの無い `git@github.com` に限った根拠 | ローカル実測(2026-09-26・OpenSSH 8.9p1・git 2.55.0) | 2(発見ループ) |
| **OpenSSH のホスト鍵設定(H44)**: `StrictHostKeyChecking no/off` は変更鍵を許し、`yes`・`ask`・`accept-new` は拒否する。`ssh -G` は yes/no を true/false で出す。新しい鍵の保存先は User の先頭だけで、書き込みの失敗は接続を必ずしも止めない。User の単独 `none` は 0 本へ変換され、未知鍵は拒否される。`HostKeyAlias` は鍵の照合名を変え、`NoHostAuthenticationForLocalhost yes` はローカルの接続先の鍵検証を省きうる。`KnownHostsCommand` や DNS の検証結果は `-G` だけでは分からない | [ssh_config(5)](https://man.openbsd.org/ssh_config)・[ssh.c](https://github.com/openssh/openssh-portable/blob/master/ssh.c)・[sshconnect.c](https://github.com/openssh/openssh-portable/blob/master/sshconnect.c)(2026-10-06 確認)。OpenSSH 8.9p1 の隔離した `-G -F` 設定で出力を確認([Issue #161](https://github.com/Yuki-Maeda-valour/workflow/issues/161)) | 2(H44)・discover-mode.md §3・§10 |
| **Claude Code** の読み取り専用のツール(Read・Glob・Grep)は、作業ディレクトリの中では承認が要らない(公式の表の原文: "Read-only \| File reads, Grep \| No, within the working directory and additional directories")。そのため `loop.sh` のイテレーションの worktree の中を読むこれらのツールは確認に回らず、許可の仲介の hook に届かない(#69 の実走の 2 晩目で、data-audit のイテレーションのメインが Read を 19 回使い、許可の仲介の記録に Read は 0 件だった。届いたのは Write と Bash だけ)。同じ実走で、委託した調査のサブエージェントが Bash の `find . -path './.git' …`・引用符の外のグロブを実行して拒否され、イテレーションが G1 で失敗した。無人のイテレーションの読むだけの調査を、先にツールで行う根拠(unattended-mode.md) | [Configure permissions](https://code.claude.com/docs/en/permissions) の「Permission system」の表(2026-09-26 確認)・ローカル実測(2026-09-26・Claude Code 2.1.281。#69 の実走) | 2(無人ループ) |
| **Claude Code** は、Linux・macOS・WSL では Glob・Grep のツールを既定のツールの集合から外す(原文: "On macOS, Linux, and WSL, Claude Code leaves Glob and Grep out of the default tool set, and Claude searches with find and grep through the Bash tool instead.")。その検索は埋め込みの bfs・ugrep で動き、hook と許可の規則には Bash の呼び出しとして届く。2 つが戻るのは、起動時に `--tools` か `--allowedTools` で Glob か Grep を名指したとき(`--allowedTools` でどちらか 1 つを名指せば両方が戻る。設定ファイルの allow の規則では戻らない)などに限る。ファイルの許可の判定は `Read(path)`・`Edit(path)` の規則だけで行い、`Glob` のパスの規則は受け付けても判定に使わない(`--allowedTools` で渡した `Glob` の規則は起動時の警告の例外。permissions の原文: "If you write a path rule for `Write`, `NotebookEdit`, `Glob`, or the legacy `MultiEdit` tool instead, Claude Code accepts the rule but never consults it, and warns at startup, except for a `Glob` rule passed in `--allowedTools`.")。`Grep` のパスの規則の扱いは文書に無い。`loop.sh` の `--allowed-tools` では、例えば `Glob(docs/**)` のように括弧を付けて検索道具を戻す。括弧の内容を許可するファイル範囲と説明しない。2.1.293 の H47 調査では scoped Glob による Glob/Grep の復帰と outside 呼出しの hook 拒否を観察した。scoped Grep 単独でも指定内/外が hook で拒否された。道具名だけの allow と `Tool(*)` は H47 の起動前検査で拒否する。道具の名だけの規則(`Grep`)を足すと、cwd の外の `Grep` が許可の仲介の hook を通らずに通った(#193 の実ホスト。2.1.289。#107 の H47)。#69 の実走で、この値を足したイテレーションの初期化のツールに Glob・Grep の 2 つが入り、委託した調査のサブエージェント 4 体も Glob・Grep・Read だけを使って Bash を 1 回も実行しなかった(メインは Glob 3・Grep 6・Bash 47、拒否 0。許可の仲介の記録に Glob・Grep は 0 件)。足す前の 1〜2 晩目のイテレーションの記録では、Glob・Grep の使用は 0 件で、調査のサブエージェントの Bash が拒否されてイテレーションが失敗した | [Tools reference](https://code.claude.com/docs/en/tools-reference) の「Glob tool behavior」・[Configure permissions](https://code.claude.com/docs/en/permissions) の「Read and Edit」(2026-09-26 確認)・ローカル実測(2026-09-26・Claude Code 2.1.281。#69 の実走の 2 晩目の再実行)・[Issue #232](https://github.com/Yuki-Maeda-valour/workflow/issues/232)(2026-10-08 調査) | 2(無人ループ) |
| **Claude Code** の委託先の一覧を取る手段(解決表 軸 7)は、行ごとに状態を出す(走行中は `running`、完了は `completed`) | ローカル実測(2026-09-27・Claude Code 2.1.283。#90 の設計レビューで走らせた委託の一覧で、走行中と完了が別の状態で出た) | 5-17(解決表 軸 7) |
| **Claude Code** の書き込みツールの挙動(4 つ): 名前が symlink(リンク先の有無を問わない)・FIFO のとき、Write・Edit は**拒否する**(リンク先は変わらない・止まらない)/ 名前がハードリンクのとき、Read の後の Write は**新しい inode に置き換える**(もう一方の名前の中身は変わらない。Read すると先に置かれた中身が文脈に入る)/ **親ディレクトリが symlink のとき、Write は辿る**(警告なし)/ Bash のリダイレクト(`>`)は symlink を辿り、ハードリンクにはその場で書き、FIFO で止まる。外部 implementer が予測できる名前(`.claude/reviews/` の記録)に先に置いたものの扱いの根拠 | ローカル実測(2026-09-28・#118 の現状分析 2。checker も再実測) | external-runners.md §12-6 の既知の限界 |
| **git** の push の送り先の ref: dst を書かない refspec は `remote.<name>.push` の写像に従う。`remote.origin.push = refs/heads/task/x:refs/heads/main` のもとで `git push -u origin task/x` を実行すると `task/x -> main` になり、origin の main が更新された(写像がローカルの設定でも global の設定でも同じ)。完全な refspec(`refs/heads/task/x:refs/heads/task/x`)は写像に従わず、`push.default`・`remote.pushDefault`・`branch.<b>.pushRemote` を変えても origin の `task/x` だけに送った(`remote.origin.mirror=true` のもとでは rc 128)。公式文書の `<refspec>` の項の原文: "unless the `remote.<repository>.push` configuration specifies a different `<dst>`"。無人の push を完全な refspec で実行する根拠(unattended-mode.md §7) | [git-push — Git 公式ドキュメント](https://git-scm.com/docs/git-push) の OPTIONS の `<refspec>`(2026-09-30 確認。ローカルの `man git-push`〈git 2.55.0〉も同じ文)・ローカル実測(2026-09-28・git 2.55.0。Issue #133 の現状分析 1。2026-09-30 に写像と完全な refspec を再実測) | 2(無人ループ・発見ループ) |
| **git** の `push.followTags`: `true`(ローカルでも global でも)のもとでは、完全な refspec の push でも、送る ref から届く注釈つきタグのうち origin に無いものが送られた。`--no-follow-tags` を付けると送られなかった。公式文書の `push.followTags` の項の原文: "You may override this configuration at time of push by specifying `--no-follow-tags`."。無人の push に `--no-follow-tags` を付ける根拠(unattended-mode.md §7) | [git-push — Git 公式ドキュメント](https://git-scm.com/docs/git-push) の `--follow-tags` と CONFIGURATION の `push.followTags`(2026-09-30 確認。ローカルの `man git-push`〈git 2.55.0〉も同じ)・ローカル実測(2026-09-28・git 2.55.0。Issue #133 の現状分析 1。2026-09-30 に global の設定で再実測) | 2(無人ループ・発見ループ) |
| **git** の `git config --worktree`: linked worktree のあるリポジトリで `extensions.worktreeConfig` が無いと、`git config --worktree --list` は rc 128 で、git の出力の原文は "fatal: `--worktree` cannot be used with multiple working trees unless the config extension worktreeConfig is enabled."(改行を空白にした)。linked worktree の中でも、main の作業ツリーでも同じだった。linked worktree が無ければ文書どおり rc 0 で、`--local` と同じ `config` を読み書きした。公式文書の `--worktree` の項の、拡張が無いときの原文は "If not it’s the same as `--local`." で、linked worktree があるときの失敗は git-config・git-worktree の頁に見つからない(この失敗は実測だけ)。`git-config-digest.py`(置き場は `<git-dir>/config.worktree`。`git rev-parse --git-path config.worktree` の値も欄に入れる)と `loop.sh` のイテレーションの後の照合が、`--worktree` を使わずに `config.worktree` を `--file` で読む根拠 | ローカル実測(2026-09-28・git 2.55.0。Issue #133 の現状分析 4。2026-09-30 に main の作業ツリーと linked worktree の無いリポジトリも含めて再実測)・[git-config — Git 公式ドキュメント](https://git-scm.com/docs/git-config) の `--worktree`・[git-worktree](https://git-scm.com/docs/git-worktree) の CONFIGURATION FILE(2026-09-30 確認。ローカルの `man git-config`〈git 2.55.0〉の `--worktree` も同じ文) | 2(無人ループ・発見ループ) |
| **git** は設定ファイルを読んで止まる: 共有の `config`、`extensions.worktreeConfig` があるときの `config.worktree`、`include.path` の先のどれかが FIFO だと、linked worktree から実行した `git rev-parse --git-common-dir`・`git rev-parse --git-path config.worktree` は戻らなかった(`timeout 5` で rc 124)。実測だけ(公式文書の記述は見つけていない)。`git-config-digest.py` が git の子プロセスを 10 秒で打ち切り、前提と push の直前に、git の打ち切りを持たない `origin-repo.py` より先に実行する根拠(unattended-mode.md §1・§7) | ローカル実測(2026-09-28・git 2.55.0。Issue #133 の現状分析 4。2026-09-30 に 3 つとも再実測) | 2(無人ループ・発見ループ) |
| **Git の設定読取**: Linux の Git 2.51.0 と Ubuntu Git 2.34.1(package `1:2.34.1-1ubuntu1.17`)で、固定した builtin の設定再走査・setup・下のリポジトリの refs の読取経路をソースで調べ、隔離した環境での回帰テストと syscall の読取観察を照合した。2.51 の `hasconfig` は remote URL の収集時に最終不成立の先も読み、下のリポジトリの `onbranch` も版によって参照文脈が異なるため、最終的に有効な出典だけの計数では足りない。`implement-guard.sh` は下のリポジトリと生成設定も起動ごとに予約する。12 MiB の不成立 `onbranch` 先や不成立循環、既存 submodule の通常操作を、止めてはいけない正常な例に含めた。2.34.1 は設定パスを返す `git var` が使えないため、取得が必要な既定/非空 system 設定は失敗側に止める。無効化の `GIT_CONFIG_NOSYSTEM`、明示空とリテラル `/dev/null` はこの機能を求めない。確認した版と入力以外の全 Git 版・改変版や全 object I/O の保証に広げない | [Issue #230](https://github.com/Yuki-Maeda-valour/workflow/issues/230)(承認設計・Git ソース調査・Linux の隔離した環境での回帰テスト。最終の独立検証結果は同 Issue の記録を参照) | 7-2・external-runners.md §12-2 |
| **git** の push の `-u` と共有の `config`: linked worktree から `-u` なしの完全な refspec の push(`git push --no-follow-tags --recurse-submodules=no origin 'refs/heads/task/x:refs/heads/task/x'`)を実行すると、global の設定に `branch.autoSetupRebase`(`always`・`remote`・`local`・`never`)か、`push.autoSetupRemote=true` と `push.default=current` があっても(`always` とこの 2 つを併せても)、共有の `config` に `branch.*` を書かなかった。origin の `refs/heads/task/x` と remote-tracking ref は、どれも HEAD と同じになった。同じ push に `-u` を付けると `branch.task/x.remote=origin`・`branch.task/x.merge=refs/heads/task/x` を書き、`branch.autoSetupRebase` が `always`・`remote` のときは `branch.task/x.rebase=true` も書いた(`local`・`never`・設定なし・`push.autoSetupRemote=true` と `push.default=current` では 2 項目)。無人の push に `-u` を付けず、`loop.sh` のイテレーションの後の照合が共有の `config` への追加を許さない根拠(unattended-mode.md §7・loop.md §4) | ローカル実測(2026-09-30・git 2.55.0。Issue #134 の「何が起きているか」〈`-u` つきで `local`・`never` が 2 項目〉・現状分析 5・完了条件 1・追加修正記録〈完了条件 1 を `local`・`never` まで広げた実測: `-u` なしで 0 項目・`-u` つきで 2 項目〉。bare の origin と linked worktree・global と system の設定を scratch へ向けた) | 2(無人ループ・発見ループ) |
| **Claude Code** の質問の仕組み(`AskUserQuestion`)の 1 回の上限と、推奨の印の慣例: 1 回の呼び出しで質問は 1〜4 問、各質問の選択肢は 2〜4 個。公式文書の原文: "Question limits: each `AskUserQuestion` call supports 1-4 questions with 2-4 options each"(質問の形式の表の `options` も "Array of 2-4 choices")。推奨の選択肢は先頭に置き、名前の末尾に「(Recommended)」を付ける慣例がある(ツールの定義の記載。出典の頁を「Recommended」で探したが、この慣例の記載は見つからなかった)。skill は数を本文に書かず、実行時にその仕組みの定義で確かめる(init-project の質問の区切り方)。skill 本文は「質問で確認する」などと書き、道具への対応づけは解決表 §8 に置く。人が読む選択肢の印は「(推奨)」にそろえる(writing-for-people.md の質問と選択肢) | [Handle approvals and user input](https://code.claude.com/docs/en/agent-sdk/user-input) の「Limitations」と質問の形式の表(2026-10-06 確認)・ローカルの観察(2026-10-06・Claude Code 2.1.289 の質問のツールの定義: 1 回に 1〜4 問・選択肢 2〜4 個・推奨の選択肢は先頭に置き、名前の末尾に「(Recommended)」を付ける) | 5-25・init-project の Phase 2 |
| **Claude Code** の応答の中のファイルを開ける形(ターミナル〈CLI〉と、表示のアプリ): ホストがセッションに渡すシステムの情報は、`パス:行番号` の形の参照は多くのターミナルでクリックできるとし、その形を `` ` `` で囲んで示す。応答の文は Markdown として表示する、とも書く。作業ディレクトリの外にある一時ファイル用のフォルダのファイルは、絶対パスを 1 つながりで書くよう求める(短くしたパスは作業ディレクトリから読まれ、行き先が無くなるため)。ほかの、作業ディレクトリの外のファイルも同じと推測される。全画面の表示では、Cmd(macOS)か Ctrl(Linux・Windows)を押しながら URL かファイルのパスをクリックすると開き、道具の出力のパス(Edit・Write の後に出るものなど)は、ファイルマネージャでそのファイルを選んだ状態で開く。Linux と WSL では、`org.freedesktop.FileManager1` の D-Bus のつなぎ口を持つファイルマネージャが無いと、ファイルのパスをクリックしても何も開かない(原文: "needs a file manager that provides the `org.freedesktop.FileManager1` D-Bus interface. Without one, nothing opens")。利用者の報告(2026-11-05): Orca という表示のアプリで Claude Code を使う画面では、会話に出した次の 5 つの形が、すべてクリックで開けた — (1) Markdown のリンクの形(表示名を角括弧、絶対パスを丸括弧で続ける形。作業ディレクトリの外のファイル)(2) `` ` `` で囲んだ `絶対パス:行` (3) 囲まない絶対パス (4) `file://` の URL (5) `` ` `` で囲んだ、作業ディレクトリからの相対の `パス:行`。これは利用者の 1 つの環境での観察で、Claude Code の版は**未確認**。また、Claude Desktop では、プロジェクトの外のパスが開けなかったことが過去にあった(利用者の過去の経験。条件は**未確認**)。どのターミナルで開けるか・`:行番号` でその行へ移るか・Orca のほかの表示で Markdown のリンクの形で手元のファイルが開くか・Web・IDE の拡張での扱いは**未確認**(IDE の拡張の公式文書が開けると書くのは、ファイルを送る道具で送ったファイルの名前だけで、ふつうの応答の中のリンクは書いていない) | このリポジトリの作業セッションと、その委託先のシステムの情報で確認(2026-11-04。Claude Code 2.1.318〈この機械の `claude --version` の値。2026-11-05 確認〉。[Issue #335](https://github.com/Yuki-Maeda-valour/workflow/issues/335))・[Fullscreen rendering](https://code.claude.com/docs/en/fullscreen) の「Use the mouse」・[VS Code](https://code.claude.com/docs/en/vs-code) の「Files Claude sends you」(2026-11-04 確認)・利用者の報告(2026-11-05。Orca での 5 つの形と、Claude Desktop の過去の経験) | 5-25・解決表 §9 |
| **Claude Code** の日本語の文書の、メモリの呼び方: AI が自分で書き残す記憶を「自動メモリ」と呼ぶ(原文: 「**自動メモリ**: あなたの修正と好みに基づいて Claude が自分自身で書くメモ」「自動メモリはマシンローカルです」)。CLAUDE.md などの指示のファイルも「メモリファイル」と呼ぶ(「プロジェクトレベルのメモリファイル」など)。長音は「メモリ」 | [Claude があなたのプロジェクトを記憶する方法](https://code.claude.com/docs/ja/memory)(2026-11-04 確認。[Issue #335](https://github.com/Yuki-Maeda-valour/workflow/issues/335)。調べた経緯は [Issue #333](https://github.com/Yuki-Maeda-valour/workflow/issues/333)) | 5-25・writing-for-people.md の言い換え表 |

**検証済みの範囲**: 実ホスト(Codex / Cursor)での **SKILL.md の読み込み・起動を確認済み**(どの工程まで動いたかの記録は無い)(2026-09-11、ユーザーによる実環境での確認報告)。**CLI ランナーとしての疎通は両 CLI で実測済み**(2026-09-09: codex 0.153.4 を導入・cursor-agent は認証済みで実レビューを取得)。**Codex のエージェント定義本体の置き場所**は公式に明記が無い(上記の `~/.codex/agents/` の TOML がこれにあたると見られるが**未確認**)。

### 7-4. 権威参照ファイル(AGENTS.md)

**正本は `AGENTS.md`、標準構成は `AGENTS.md` のみ**。Codex / Cursor に加えて Claude Code も `AGENTS.md` を直接読むようになったため(v2.1.277 以降。事実と出典は §7-3)、`CLAUDE.md` による橋渡しを標準構成から外した(2026-09-22)。

| ホスト | AGENTS.md | 経路 |
|---|---|---|
| Codex | ネイティブに読む | そのまま |
| Cursor | ネイティブに読む | そのまま。`.cursor/rules` は生成しない(併存できるが実体を 1 つに保つ) |
| Claude Code | **直接読む**(作業ディレクトリまたはその上位に CLAUDE.md 系が無いとき。§7-3) | そのまま。互換形では `CLAUDE.md` の import 経由で読む(互換形を使う場面は下の運用) |

**正常な構成は 2 つ**(検出とドリフト判定の手順は §3):

- **完成形**: `AGENTS.md` のみ(`CLAUDE.md` を持たない)
- **互換形**: `AGENTS.md` + `@AGENTS.md` を import する `CLAUDE.md`(import の下にホスト固有の追記を持つものを含む)。v3.9.0〜v4.2.0 の標準構成であり、`AGENTS.md` を直接読めない環境の橋渡しでもある。二重読みも欠落も起きないため **skills は警告しない**

**正本の選択と、互換形の橋渡しに import を使う理由**(symlink・CLAUDE.md 正本・コピーを採らない理由。2026-09-09 から変更なし):

- **symlink**: Windows では管理者権限か開発者モードが必要(Claude Code 公式が import を推奨)。コピーを伴う配布・チェックアウトでは実体化されて意図が崩れる(本リポジトリでも review-agent.sh の配布で同型の問題を踏んだ)
- **CLAUDE.md を正本にする**: Codex 側に import 構文があるか未確認
- **両方に同じ内容を置く**: ドリフトの再生産。同じ事実が複数箇所にあると同期漏れが起きる(§7-2 と同じ理由)

**運用**:

- **サイズ**: 権威参照ファイルは**最も厳しいホストの上限**(現状 Codex の `project_doc_max_bytes` 既定 32 KiB。実値と出典は §7-3)に収まるよう薄く保つ
- **生成既定**: `/init-project` は `AGENTS.md` のみを生成する。既に `AGENTS.md` だけがあるプロジェクトにも `CLAUDE.md` を足さない
- **ホスト固有の指示の置き場**: `AGENTS.md` には他ホストでも通じる記述だけを置く。Claude Code 固有の指示は `.claude/rules/*.md`(**既定のファイル名は `claude-code.md`**。2026-09-21 決定 13)に置く(`AGENTS.md` の直接の読み込みを止めず、併せて読まれる — 既定値のもと。§7-3。**併存は 2026-09-22 に実機で確認した**〈Claude Code v2.1.278・既定値・上位に CLAUDE.md 系が無い clone。`AGENTS.md loaded` の行と、両ファイルに入れた乱数の合言葉の即答で確認〉)。`.claude/rules/` はホストが自動で読む**指示ファイル**であり、commit して共有する — §5-18 の状態ファイル(揮発性キャッシュ・gitignore 対象)には当たらない。skills がここを読取り・同期の対象外とする(§3)のは、ホストが自動で読むうえ、skill 本文のホスト結合を増やさないため。承知した欠点: rules 内のコマンドが陳腐化しても `/update-doc` では拾えない
- **互換形と Cursor CLI**: Cursor CLI は CLAUDE.md も読む(§7-3)ため、互換形の `CLAUDE.md` に他ホストで有害・無意味になる指示は書かない。`@path` import を展開する記載は Cursor 側に無く(**未検証**)、展開しなければ import 1 行の `CLAUDE.md` は**内容が重複しない**
- **導入済みプロジェクト(互換形)の移行**: **`/init-project` だけが、承認制で完成形への移行を提案する**(他の skill は提案しない)。import のみの `CLAUDE.md` は削除、import + 追記の `CLAUDE.md` は追記を `.claude/rules/` へ移してから削除(追記の中の口調を決めた節は、移行内容の提示で `AGENTS.md` 行きを勧める — `.claude/rules/` へ移すと skill が読まず、「応答の書き方」節から指せないため。§5-25)、**ルートの `AGENTS.md` への symlink の `CLAUDE.md` はリンクの削除だけを行う**(移す内容は無い。2026-09-21 決定 16)。**それ以外のファイルへの symlink は解決先の内容に応じて上の 2 分岐に従い、リンク先は書き換えない**。削除の前に「`AGENTS.md` を直接読めない環境で使う人がいないか」を確認し、いれば残す(`CLAUDE.md` への外部参照があるときは、下の「移行の規律」の提示を経てユーザーが判断する)
- **未移行プロジェクト(`CLAUDE.md` のみ)の移行**: `/init-project` は既存 `CLAUDE.md` を検出したら AGENTS.md への移行を提案する。移行後の既定は完成形(記述を `AGENTS.md` へ移して `CLAUDE.md` を削除。Claude Code 固有の記述だけ `.claude/rules/` へ)。`AGENTS.md` を直接読めない環境で使う人がいる、または `CLAUDE.md` への外部参照(他ツール・CI)があるときだけ、互換形を選べる。互換形を選んだときも、Claude Code 固有の記述は `.claude/rules/` へ移す(`CLAUDE.md` は import 1 行になる。この規則は移行で作る互換形に適用し、既存の互換形の扱いは上の定義のまま。**`AGENTS.md` を直接読めない環境でも `.claude/rules/` が読まれることは公式記載からの推定で未実測** — 決定録の「実装時の確認事項」)
- **移行の規律**(上の 2 つに共通): ユーザーの**明示承認**を得てから実行し、**`--yes` でも省略しない**。**他ツール・CI が `CLAUDE.md` を直接参照している場合に壊れるため、移行提案時に参照の一覧を提示する**。**import を持たない他の CLAUDE.md 系(`CLAUDE.local.md` / `.claude/CLAUDE.md`)がルートに残り、削除後の最終状態が §3 のドリフト判定に当たるなら、承認前に警告する。当たらなくても残るファイルの一覧は提示する。残るファイルには書き込まない**(2026-09-21 決定 15)。これは `/init-project` の「既存を壊さない」原則(`init-project/SKILL.md`)に対する、**ユーザーの明示承認を条件とする例外**である
- **`AGENTS.md` を直接読めない環境**: 検出はせず**案内のみ**。`/init-project` の完了報告と `AGENTS.md` テンプレート冒頭に、`AGENTS.md` を直接読むかの確認方法と互換形の作り方を書く(確認方法の事実と限定は §7-3)。dev-workflow のフローでは `/understand-project` が権威参照ファイルを必ず Read するため、欠落するのは skills を使わないセッションに限られる
- **「PR の前の手順」の節**: `/ship-task` は、PR を開く前の Phase 4.5 で、取り込んだ後の作業ツリーの権威参照ファイルから、見出しが「PR の前の手順」の節を読む([Issue #373](https://github.com/Yuki-Maeda-valour/workflow/issues/373))
  - 節は任意で、無いプロジェクトでは何もしない。`/init-project` の雛形にも入れない
  - 見出しの名前は ship-task が決め、手順の中身(版を上げる・検査のコマンドなど)はプロジェクトが書く。skill 本文にはプロジェクト固有の手順を書かない(§3)。「応答の書き方」節(§5-25)と同じく、skill が名前で指す節である
  - 節の手順は書かれた順に行う。ファイルを変える手順は implementer に任せ、取り込みの後の review に通す。行い方の正本は ship-task の `references/integration.md` §6
  - 節に ship-task が行えない手順(人が行う手順)が残るときは、PR を開かずに止め、その手順と公開に要る値を報告する。Phase 4.5 を行わない実行(`--no-pr`・parent-child・`repo` が null など)で節があるときも、対話では同じく止める
  - 無人のタスクのイテレーションは、取り込みも節の手順も行わない。そのことを PR 本文に書き、merge の前に人が行う。発見のイテレーションの PR 本文は変えない
- **版**: この改訂への追従は **v4.3.0** で出した。マイナーでの旧値廃止(§4 の `source_of_truth`)は §8 の「後方互換な機能追加はマイナー」と緊張するが、影響範囲の小ささ(旧値を持つのは超小規模構成の profile のみ)を理由に承知の上で選択した(§8 の基準は変えない)。移行の注記は README と `CHANGELOG.md` に置く

出典: `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md`(決定 5・6・13、派生判断 17・18)/ `docs/minutes/2026-09-21_壁打ち_権威参照ファイルの一本化.md`(決定 1〜6・8〜17 と帰結。決定 4・16 の反映先は §3、決定 8・9・17 は §4。2026-09-09 決定 5 の後半を変更)

### 7-5. 委託の解決(どのバックエンドで実行するか)

この節は委託の解決(どのバックエンドで実行するか)を扱う。③ の宣言方法・信頼モデル・implementer の扱いは §7-2 が正本であり再掲しない。ここでの ② はレビュー等の**委託先**としての MCP であり、**ツール接続としての MCP(§7-6)とは別概念**。

- **解決の規定は解決表が正本**(解決順・profile が宣言できる範囲・既定・② の未定・報告義務・解決できないときの縮退・役割語の一覧)。**本節は再掲しない**
- **解決表の置き場**: [`do-task/references/delegation-map.md`](../plugins/dev-workflow/skills/do-task/references/delegation-map.md)(共有アセットとして所有 skill の配下に置き、他 skill は兄弟参照で解決する。§6)。**役割語の一覧・派生名の体系・属性軸・解決順・ホストでの解決・解決できないときの縮退は解決表が正本**であり、本節が自ら持つのは原則(下の「なぜそうするか」)である。**本節の要約併記は v4.0.0(移行完了。§7-7)で終了した** — §7-2 の原則(同じ事実を複数箇所に書かない)に戻し、規定は解決表だけが持つ。**変更の向きは一方向** — 解決表を先に直す(**役割語を増やすときは解決表 §1 に足す**。本節に一覧を生やさない)

**なぜそうするか**:

- **なぜ役割語 + 解決表か** — §3 が禁じる「事実のハードコード」をツール軸でやっている状態の解消。新ホストが増えても解決表にホストの列を 1 つ足すだけで済む。却下: 「役割語 + 既定を括弧書き」(本文にホスト名が残り中途半端)/「ホスト別分岐を本文に書く」(500 行制限を圧迫し、ドリフトの主因を再生産)
- **なぜ順序固定・有効化のみか** — §5-17 のやり取りの範囲の判定と同型で既存の設計哲学に収まる。profile 無しでも動く(§3 の絶対規約)。組み合わせが少なくテストできる。却下: 「profile で順序も指定可能」(組み合わせ爆発で回帰テストが非現実的)/「役割ごとに指定」(役割数 × バックエンド数のマトリクスになる)
- **なぜ ③(CLI)を残すか** — MCP サーバーを提供しないベンダーでは ③ が唯一の経路になるため

出典: `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md`(決定 1・2・10)

### 7-6. ツール接続としての MCP

**ツール接続としての MCP(本節)と、委託バックエンド ② としての MCP(§7-5)は別概念**。本節は「同じツールにどの AI からも繋ぐ」ための設定生成を扱う。

- **profile で 1 回宣言し、`/init-project` がホスト別の設定を生成する** — `.mcp.json`(Claude Code)と `.codex/config.toml`(Codex)。**スキーマ(キー名・値の形)は §4 の `mcp_servers` を正本とする**(同じ事実を 2 箇所に書かない。§7-2)
- **「1 宣言 → 2 形式」の等価性は完全ではない** — `.codex/config.toml` は**コメント・キー順序・整形を持ち全体 Write が既存を壊す**ため(書き込み手段の実在も前提にしない。§3)append-only で、**不一致の置換**ができず、**他記法が混在するファイルへの追記**もできない(後者は TOML パーサを検出できた環境では解消する)。**限界の一覧と報告義務は生成手順の正本(下記の `mcp-config-generation.md`)**
- **MCP サーバー自体は同梱しない**(理由は下の「なぜそうするか」)
- **完了時の案内は、生成・変更した設定ファイルに応じて出す**。ホストごとの説明と共通の説明を分け、片方だけを生成した場合も必要な説明を欠かさない([Issue #195](https://github.com/Yuki-Maeda-valour/workflow/issues/195))。
- **Serena を自分で設定する選択には、設定先と貼り付け用の設定例を案内する**。案内の手順は [`init-project/SKILL.md`](../plugins/dev-workflow/skills/init-project/SKILL.md) Phase 4-5 に置く。この案内で設定ファイルは書き込まず、既存の生成・承認の規則は変えない([Issue #196](https://github.com/Yuki-Maeda-valour/workflow/issues/196))。
- **生成手順の正本は [`init-project/references/mcp-config-generation.md`](../plugins/dev-workflow/skills/init-project/references/mcp-config-generation.md)**(`.codex/config.toml` の append-only・既存 id の判定と内容比較・退避条件・改行終端・生成後の 2 段階検証とロールバック・貼り付け用スニペット)。本節は方針だけを持ち、手順は再掲しない(§7-2)
- **Codex へ生成するのは `.codex/config.toml` の MCP のみで、カスタムエージェント定義は生成しない** — エージェント編成は §7-5 の ① としてホストに任せるため。公式の定義機構はプロジェクト層でも宣言できると読めるので、「ユーザースコープにしか住まない」ことは理由にしない(事実と出典は §7-3)。なお `codex agents` **サブコマンド**はセッション閲覧であり(実測)、定義は設定ファイル側の機構で行う(§7-3)

**信頼モデル**: §7-2 の信頼モデルの明示的な例外(方針は §7-2)。`/init-project` は解決後の宣言(サーバー名・コマンド・引数)を提示して**ユーザーのセッション内明示承認**を得てから設定ファイルへ生成する。「profile に書かれている = 承認済み」とは扱わない。**宣言が変わるたびに承認を取り直し、`--yes` でも省略しない**(明示承認の要件を運用に当てはめた派生規則。権威参照ファイルの移行と同じ扱い)。

**ゲートは経路ごとに適用範囲が違う**:

- **生成経路(profile → 設定ファイル)**: skill 側の明示承認が、ホストと実行モードによらず働く。**承認を得られない実行(非対話)では生成せず、理由を報告する**(§5-9)
- **設定ファイルを既に持つリポジトリ**(クローン直後・敵対的リポジトリ): 生成を経ないため skill 側の承認は介在せず、**ホスト側のゲートだけが防御になる**。Claude Code は対話セッションでのみ承認を求め(リポジトリにコミットされた一括承認設定は未信頼フォルダでは無視される)、`claude -p` / Agent SDK / クラウド実行では承認なしに読む。Codex はプロジェクトスコープの設定を信頼済みプロジェクトのときだけ読む(§7-3)

したがって **skill 側の承認とホスト側のゲートは互いの代替にならない**。生成した宣言は最終的にホストで実行されるため、これは §7-2 の原則に対する**明示的な例外**である。skill 側の承認が守るのは生成経路だけで、既に設定ファイルを持つリポジトリはこの skill の管轄外(ホスト側のプロジェクト信頼に委ねる)。

**なぜそうするか**:

- **なぜ profile 宣言 → ホスト別生成か** — 「同じ MCP をどの AI からも」を実現する最小の形。§3 の 3 層吸収と同型(1 つの事実 → ホスト別に展開)で、skills 集という性格を保てる。却下: 「検出と案内のみ(決定前の方式)」(AI 非依存化に寄与せず人間の規律依存になる)/「MCP サーバーも同梱」(別プロダクトになり、本文のホスト結合を何も解決しない)
- **なぜ信頼モデルを適用するか** — `.mcp.json` / `.codex/config.toml` / profile はいずれも commit される共有ファイルで、敵対的リポジトリが任意の MCP サーバー(= 任意のコマンド)を宣言できる。同型の任意コード実行を PoC で再現済み
- **なぜカスタムエージェント定義を生成しないか** — 却下: 「承認制でユーザースコープへ生成」(全プロジェクトに影響する変更を 1 プロジェクトの初期化が行う。定義形式の実物も未確認)

出典: `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md`(決定 3・4・11)

### 7-7. 移行方針(段階移行と後戻りの防止)

この節は本文のホスト結合を解消していく移行の、単位・順序・後戻りの防止・版の刻みを扱う。

- **単位**: 基盤(解決表 + §6 の規約)を先に入れ、その後は skill 単位で移行する
- **順序**: 本文のホスト結合が多い skill から。量は §7 の測定コマンドで着手時に測り直す(語彙の選び方で数値が変わるため、順序を固定列として持たない。現行の測定コマンドは `.claude/` 等を含む広い語彙だが、順序の目安はこれで足り、検査と到達条件は下の「委託の語」で判定する)。決定時点の測定(決定録。範囲・語彙の明記は無い)では init-project が最多、§7 のコマンド(SKILL.md のみ・9 トークン)では do-task が最多
- **委託の語**: 移行の対象・検査・到達条件で使うホスト固有語は `Agent` / `SendMessage` / `ListAgents` / `Explore` / `general-purpose` / モデルエイリアス。**`.claude/` 配下の状態ファイルは別論点(未決)**であり、この語彙に含めない
- **検査範囲**: 各 skill の **skill 直下のファイル(画像を除く)・`references/` 配下・`scripts/` 配下**(`SKILL.md` と `references/` は 2026-09-09 のユーザー判断、`scripts/` と skill 直下への拡張は 2026-09-10 の追加)。**skill 直下を `SKILL.md` に限定しない**のは、`README.md` のようなレイアウト外のファイルに委託の語を置くと到達条件をすり抜けるため。`SKILL.md` だけを対象にすると、references に残るホスト結合(実測で `do-task/references/review-protocol.md` に 18 箇所)が到達条件をすり抜け、v4.0.0 に達してもホスト結合が残る。**`scripts/` を加える理由**: `scripts/` はモデル名を `--model` 引数と `{model}` プレースホルダで外から受け取る設計で、ホスト内蔵の委託機構(`Agent` / `SendMessage` / `ListAgents` / `Explore` / `general-purpose`)は shell / python から呼べないため、呼び出しとして現れることはない(実測 0 件)。スクリプトがホスト内蔵機構との関係を説明する必要が生じた場合は、`external-runners.md`(除外 2 本の 1 つ)を参照する形にし、スクリプト側で機構名を再掲しない — **正当な必要は参照で満たす**のであり、再掲は §7-2 のコピーにあたる。**拡張子の扱いは非対称**: **`references/` は design がドキュメント専用と定めたディレクトリで**ファイル集合が閉じているのでホワイトリスト(`.md`)で絞れる一方、**`scripts/` は任意言語のコードが入り**ファイル集合が開いているのでブラックリスト(画像だけ除外)しか成立しない。副理由として、`references/` の `.md` 限定は #7 が定めた `.bak` 退避イディオムの治具を巻き込まないためでもある。**`templates/` 配下は対象外**とし、理由は 3 ファイルそれぞれ次のとおり — `project-profile.yml.template` は **profile こそがモデルエイリアスの置き場**である(§3)/ `AGENTS.md.template` は**利用者プロジェクトへ生成される成果物**であって skill 本文ではない(**検査の管轄外という意味であり、ホスト固有語を持ってよいという意味ではない。生成物は複数ホストから読まれる(§7-3 の実測)ため、2026-09-10 に役割語と能力の記述へ移行済み。`AGENTS.md` を直接読むかの案内(§7-4)は明示的な例外。⚠ 生成先に解決表は無いので、skill 本文の移行と違って解決表を参照せず能力で書く**) / `specialist-agent.md.template` は **Claude Code 固有のエージェント定義 frontmatter そのもの**で、ホスト非依存化は §7-6 の管轄であり本節の語彙とは別論点
- **検査対象外ファイル(ここが正本)**: [`do-task/references/delegation-map.md`](../plugins/dev-workflow/skills/do-task/references/delegation-map.md)(解決表)と [`do-task/references/external-runners.md`](../plugins/dev-workflow/skills/do-task/references/external-runners.md) の **2 本**。**ホスト固有の解決を持つことがそのファイルの役割**であるため。ファイル側の自己申告は補助に留め、**判別の正本はこの除外集合**とする(自己申告マーカーは検査が判別できる契約にならず、判別規則が設計書に残らない)。`external-runners.md` の該当 2 箇所は ③ CLI と ① 内蔵の相互作用を説明する契約本文であり、役割語化すると意味を失う。この除外集合が無いと下の到達条件(references もゼロ)は到達不能になる。**除外の不変条件**: 解決表は委託の語の検査対象外だが、**モデルエイリアス名だけは自ら 0 件に保つ**(§3 の「解決表はエイリアスの一覧を持たず、指定方法だけを持つ」)。この不変条件は `scripts/validate.py` の機械検査で保つ
- **語彙の限界**: モデルエイリアス 4 語は**単語境界を持たない部分一致**なので、英単語の一部(`affable` / `opuscule` / `sonnets` 等)で**誤検出しうる**。単語境界を付けない理由は、**日本語文字が `\w` に含まれるため `\bopus\b` が『モデルはopus』を取りこぼす**(偽陰性 = 到達条件をすり抜ける危険側)一方、**誤検出は ERROR で落ちる安全側**であること。誤検出が出たら**その語を書き換えるか除外集合に足す**(行単位の免除規則は持たない)。あわせて**大文字小文字を区別する**ため、大文字で書かれた同種の衝突(`Agent` を含む文書名など)は見逃されない。実測(2026-09-10): `IGNORECASE` にすると `SKILL.md` の件数は **73 → 75**(増分 2 件は `init-project/SKILL.md` の概念語 `specialist agent` とファイル名 `specialist-agent.md.template`)
- **後戻りの防止は機械検査とレビュー観点の両方**(理由は下の「なぜそうするか」): `scripts/validate.py` が**全 skill の `SKILL.md`・`references/`・`scripts/` を検査し、未移行 skill を許容リストで除外する**(2026-09-10 のユーザー判断。許容リストは `scripts/validate.py` が持つ。自己申告マーカーではない。未移行 skill = 許容リストに載っている skill。上の除外集合と `templates/` は検査対象から外す)。移行のたびに許容リストから削る。削り忘れは `scripts/validate.py` の逆検査が WARN で知らせる。あわせてこのリポジトリの skill 変更レビューの観点に「ホスト結合の混入」を加える(§6)
- **版の刻み**: 基盤は 3.9.0(解決表が入ってから公開する)。移行は 3.9.x 系で刻み、**委託の語が全 skill の `SKILL.md`・`references/`・`scripts/`(上の除外集合を除く)でゼロになった時点**(= 許容リストが空の状態で `scripts/validate.py` が通る)で **v4.0.0** とする。移行途中の利用者は 3.9.x を使い続けても壊れない

**v4.0.0 に到達した(2026-09-10)**: 到達時の実測は `SKILL.md` **0**(基準 73)/ `references/`(検査対象外の 2 本を除く)**0**(基準 21)/ `scripts/` **0**(基準 0)= **計 0**(基準 94)。許容リストは `set()` で空。**移行は skill ごとに 7 本の PR** で行った — stack-research 1 / data-audit 3 / init-project 7 / update-doc 8 / reflect-decisions 8 / create-task 25 / do-task 42。⚠ **許容リストの定数は空でも残す** — `check_delegation_words()` と `check_migration_allowlist_staleness()` が参照するので、消すと後戻りを防ぐ検査が壊れる。⚠ **到達条件は「委託の語 0」だけでは足りない** — 0 件は「検査が何も見ていない」でも達成されるので、公開前に**委託の語を 1 つ含む治具 skill を置いて ERROR + exit=1 になることを実測する**(#7 で確立した方式。治具は消し忘れると exit=1 で落ちるので必ず気づく)。あわせて **§5 前文・§7-5 の要約併記も v4.0.0 で終了した**(§7-2 の原則に復帰。ホストの事実は解決表 か §7-3 が持つ)

**委託の語による実測(2026-09-09・上の「委託の語」の語彙)**: `SKILL.md` **73** / `references/`(検査対象外の 2 本を除く)**21** / `scripts/` **0** / 計 **94**。内訳は do-task 42(24 + 18)/ create-task 25(22 + 3)/ update-doc 8 / reflect-decisions 8 / init-project 7 / data-audit 3 / stack-research 1。

```bash
V='\bAgent\b|SendMessage|ListAgents|Explore|general-purpose|fable|opus|sonnet|haiku'
for s in plugins/dev-workflow/skills/*/; do grep -oE "$V" "$s/SKILL.md"; done | wc -l          # → 73
grep -rh -oE "$V" plugins/dev-workflow/skills/*/references/ \
  --include='*.md' --exclude='delegation-map.md' --exclude='external-runners.md' | wc -l        # → 21
for s in plugins/dev-workflow/skills/*/scripts; do [ -d "$s" ] && grep -rhoE "$V" "$s"; done | wc -l   # → 0
```

**この値は §7 前文の測定コマンドとその実測値とは別物であり、比較できない**(§7 側は `.claude/` を含む広い語彙で `haiku` を持たず、`\b` 境界も付けていない)。§7 側は移行順序の目安として現行のまま据え置き、**検査と到達条件は本節の語彙と除外集合だけで判定する**。

**エイリアスの世代交代に合わせる更新**: 検査語彙のモデルエイリアスは**スナップショット**であり、世の中に新エイリアスが出たら**次の 5 箇所を同時に更新する**。① 上のコードブロックの測定コマンドの語彙 → ② `scripts/validate.py` の `_MODEL_ALIASES` → ③ 同 `FORBIDDEN_PATTERNS` の日付付きモデル ID → ④ 同 版数 → ⑤ §7-3 の「最上位 / 上位 / 標準とエイリアスの対応」の表。**この順で直す**(design を先に直し、コードを合わせる。§5 の解決表と同じ向き)。⚠ **③④ から ① ② を導出しない** — ③④ は日付付き ID・版数を検出する正規表現で `mythos` を含み、**エイリアス集合の列挙ではない**。**導出はしないが同時に更新する**。⚠ **機械化しない**: 更新のきっかけが外部事象なので、① と ② を突き合わせる検査を足しても**両方が古いまま通る**。⚠ **§5-4 の「固定リストとして扱わない」とは別の話**(あちらは実行時に指定できるエイリアス集合)

**なぜそうするか**:

- **なぜ基盤先行 + skill 単位か** — 各回で検証でき、失敗しても影響が 1 skill に限定される。18 ファイルの変更が 5 反復・66 件の指摘を生んだ実績から分割の価値は実証済み。却下: 「一括(1 タスク・1 PR)」(レビュー不能な規模)/「役割種別で横断」(1 回の変更が全 skill に及び、skill ごとの文脈を見る作業と相性が悪い)
- **なぜ機械検査とレビュー観点の両方か** — 機械検査は測定可能な不変条件になり、段階移行と相性がよい(全 skill を対象にしたまま、未移行 skill を許容リストで除外できる)。リンク検査の拡張で同手法が効くことを実測済み。ただし語彙一致では拾えない文脈依存の結合が残るため、レビュー観点も併用する
- **なぜ完了をメジャーで示すか** — 「AI 非依存の到達点」を版で示せる。却下: 「すべてマイナー」(完了の節目がなく移行が終わらない)/「v4.0.0-rc から」(rc が長期化すると本番利用をためらわれる。このリポジトリに rc 運用の前例がない)

出典: `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md`(決定 7・8・9 とその補足)

- **このカテゴリに免除規則は無い**: §5-24 の免除マーカーは委託の語検査には効かない。委託の語(ホスト内蔵の委託機構名・モデルエイリアス)は**役割語へ書き換えれば必ず消せる**ので、例外を書く必要がないため。書き換えて消せない語を持つ §7-7-1 とはここが分かれる

### 7-7-1. ホスト CLI 語(委託の語とは別カテゴリ。2026-09-17 追加)

上の「委託の語」は `Agent` / `SendMessage` / `ListAgents` / `Explore` / `general-purpose` / モデルエイリアスというホスト**内蔵**の委託機構名を対象にする。**このカテゴリは、ホスト内蔵の委託機構名以外のホスト結合を広く扱う**(外部 CLI 固有の語に加えて、MCP サーバ名・ホストのツール引数名・ホストの質問の道具名と引数名も含む)。それとは別に、implementer を外部 CLI(Codex / cursor-agent / Gemini)へ委託する経路(#51)を作るにあたり、**ランナー名・サンドボックスモード名・コマンド形・プラグイン/subagent 名**という外部 CLI 固有の語が skill 本文に混入する経路が新たに生まれたため、`scripts/validate.py` に別カテゴリ(`_HOST_CLI_WORDS` / `check_host_cli_words()`)を新設した。**既存の「委託の語」検査(`_DELEGATION_WORDS`)は変更しない**(語彙・スコープ・除外集合のいずれも 1 ビットも変えていない)。

- **対象語(限定を同じ文に置く: 「全域」ではなく検査範囲(下の「検査範囲」— skill 直下 + `references/` 配下の `*.md`、除外 2 本を除く)の実測が 0 件の語だけを入れる。**正当に具体名を持つ行を免除マーカー(§5-24)で外した後に 0 件であればよい**。§7 の測定コマンドと同じ考え方だが範囲が違う)**: `scripts/` / `templates/` を含めた全域では `cursor-agent` が **26 件**(`scripts/` 25 + `templates/` 1)・`gemini` が **6 件**(すべて `scripts/`)実在する(実測 2026-09-17)が、いずれも検査範囲外にあるため対象語として選べる。この **7 系統 14 語**はランナー名(`cursor-agent` / `gemini`)・サンドボックスモード名(`workspace-write` / `danger-full-access`)・コマンド形(`codex exec` / `codex review` / `codex mcp`)・プラグイン/subagent 名(`codex-plugin-cc` / `codex-rescue`)・MCP サーバ名(`claude-in-chrome` / `chrome-devtools`)・ホストのツール引数名(`run_in_background`)・ホストの質問の道具名と引数名(`AskUserQuestion` / `multiSelect`。実測 2026-10-06・Issue #148: 検査範囲で 0 件。skill ツリー〈scripts/・templates/ を含む〉では、除外した解決表の質問の節のほかは 0 件)
- **検査範囲**: 各 skill の **skill 直下のファイル(画像を除く)全部** + **`references/` 配下の `*.md`**(委託の語検査と同じ判断 — skill 直下を `SKILL.md` に限定しないのは、`README.md` のようなレイアウト外のファイルに置くと到達条件をすり抜けるため。既存との差分は「`scripts/` を対象外にする」点だけ)。**`scripts/`(および `templates/`)を対象外にする前提は委託の語検査と共通だが、結論は逆**: 前提(`scripts/` はモデル名や CLI 名を `--model` 引数のように外から受け取る実行層のアダプタである。design §7-2)は共有するが、委託の語検査はこの前提から「ホスト**内蔵**の委託機構名は shell / python から呼べず呼び出しとして現れない(実測 0 件)、だから `scripts/` を対象に含めてよい」と結論し、このカテゴリは同じ前提から「`scripts/` は**外部 CLI** の名前を正当に持つ場所だから対象外にする」と逆の結論になる。`templates/` も同じ理由(CLI 名を正当に持ちうる)で対象外
- **除外ファイル**: 委託の語検査と同じ 2 本(`do-task/references/delegation-map.md` / `do-task/references/external-runners.md`)を共有する。3 本目には増やさない
- **大小の扱いは既存カテゴリと異なる**: このカテゴリは **`re.IGNORECASE` で大小を無視する**。理由は `Codex に実装を委託する` のような**大文字始まりの製品名・コマンド名が最も混入しやすい書き方**であること。既存の「委託の語」は大文字小文字を区別したままなので(§7-7 の「語彙の限界」)、**同じ検査基盤の中に大小の扱いが異なる 2 カテゴリが並存する**。実測(2026-09-17): `Codex exec` / `Cursor-agent` / `Gemini` のような大文字始まりの用例は skill ツリー全域(検査範囲 + `scripts/` + `templates/`)で 0 件のため、`IGNORECASE` にしても誤検出しない。**2026-09-17 決定 23 は「コマンド形・サンドボックスモード名・プラグイン/subagent 名」の 3 系統だけ大小無視と書いているが、ランナー名も同じ理由(大文字始まりの製品名・コマンド名が最も混入しやすい書き方であること。2026-09-17 決定 23 の根拠文自体が `Gemini` を例に挙げている)で含める** — 実装は 7 系統 14 語すべてに `IGNORECASE` を適用する
- **境界の扱い(2026-10-06・Issue #101)**: 通常 12 語(ランナー名・サンドボックスモード名・コマンド形・プラグイン/subagent 名・ホストのツール引数名・ホストの質問の道具名と引数名)は、左右に `[A-Za-z0-9_-]` が隣接しない **ASCII 境界**を付ける。先頭・末尾、日本語(助詞を含む)・空白・句読点・引用符・`/`・`.`・`@` は境界になる。`Geminiに実装を委託する` / `Codex execで委託` / `cursor-agentで実装` を検出し、`my-cursor-agent-wrapper` / `geminibot` / `workspace-writer` / `x-codex-rescue-y` は検出しない。語本体の大小無視は維持し、**境界の文字クラスだけ `(?-i:...)` で大小無視を無効化する** — `re.IGNORECASE` の `[A-Za-z]` は ASCII 外の `İ` / `ı` / `ſ` / `K` にも一致するため。`é` を含む ASCII 外の文字は境界として扱う。Unicode の `\b` は日本語直結を取りこぼすため使わない。コマンド形の `\s+` は複数空白・タブ・全角空白・改行を引き続き許容し、ファイル全体から検索する。一律の `re.ASCII` はこの空白の契約を変えるので付けない。跨行の一致は開始行の行番号・免除マーカーで判定する。委託の語(§7-7)・周辺プロジェクト名(§5-24)は別の境界契約を維持する
- **MCP サーバ名と境界の限界**: MCP 2 語(`claude-in-chrome` / `chrome-devtools`)は左右とも境界の無い部分一致を維持する。`chrome-devtools-mcp` / `chrome-devtools-mcp@latest` のほか、未列挙の接頭・接尾の派生名も検出するため、派生名を別語として追加しない(語数に数えない)。`my-chrome-devtools-wrapper` / `claude-in-chrome-helper` のような合成例も検出する(実在パッケージと主張するものではない)。MCP 語を含む無関係な別語の誤検出は受容し、正当な記述は **理由付き行マーカー**で免除する。一方、通常語の `Gemini2` / `gemini_bot` / `cursor-agent-wrapper` は非検出となる。全ホスト関連パッケージの意味的な判別は保証しない。**§5-24 の免除マーカーはこのカテゴリには効く**(委託の語や除外の不変条件には効かない) — MCP サーバ名は生成する設定の識別子そのもので、役割語へ書き換えて消せないため。免除は一致開始行だけに効き、理由なし・空白だけの理由では免除しない。**ファイルごと除外にはしない**(その先で新しく混入しても捕まらなくなる)
- **質問の道具名と案内(2026-10-06・Issue #148)**: `AskUserQuestion` / `multiSelect` の ERROR は、案内が解決表の質問の節(`delegation-map.md` §8)を指し、ほかの語の案内(external-runners.md へ移す)と分ける。案内は、どのパターンに一致したかで選ぶ。`multiSelect` は大小を無視して照らすので、UI 部品の名前 `MultiSelect` / `multiselect` にも一致する。正当な記述は理由つきのマーカーで外す
- **既知の限界(3 件。理由は各々同じ文に書く)**: ① **単独の `codex` は検査語に入れない** — 設定ファイルパス形 `.codex/config.toml`(実測: `SKILL.md` 21 件 + `references/` 6 件、いずれもパス形)と衝突するため。コマンド形 3 種(`codex exec` / `codex review` / `codex mcp`)で CLI としての混入は検出できる。② **`read-only` は検査語に入れない** — 「読み取り専用」の英語表記という一般語と衝突するため。サンドボックスモード名は 3 値のうち 2 値(`workspace-write` / `danger-full-access`)を検出すれば、モード指定を skill 本文に書く行為自体は検出できる。③ **単独の製品名(`Codex` / `Cursor` / `Claude Code`)は検査語に入れない** — `init-project` に正当な散文用例がある(実測: `Codex` が `SKILL.md` 4 件・`references/` 3 件、`Cursor` が `SKILL.md` 2 件)ため。コマンド形・サンドボックスモード名・プラグイン名を大小無視で検出するため、実際に委託を書こうとすれば多くの場合いずれかに触れるが、**製品名だけの散文は検出されない検出漏れを承知の上で選択している**
- **到達条件**: 配布物の ERROR 0 だけでは取りこぼしなしの証明にならない。既存 skill の一時コピーへ fixture を置き、7 系統それぞれの正例でホスト CLI ERROR 1 件・他カテゴリ ERROR 0 件・exit=1、Issue #101 の誤検出 4 例で全 ERROR 0 件・exit=0 を確認する(回帰テストは、一致が改行をまたがないケースを空白でない区切りの行で並べて 1 回で流し、ケースの行ごとの件数・ほかのカテゴリの ERROR 0 件・終了コードを確かめてよい。一致と免除の印はケースの行に閉じるので、1 ケースずつ流したときと同じことを確かめる。改行をまたぐコマンド形は 1 ケースずつ流す — [Issue #385](https://github.com/Yuki-Maeda-valour/workflow/issues/385))。全 14 語の単独・大小混在・日本語前後直結・句読点等の境界、通常 12 語の英字・数字・`_`・`-` の左だけ／右だけの隣接による非検出、ASCII 外の隣接による検出、MCP 派生名、コマンド内の空白と跨行の開始行判定を回帰テストで固定する。理由付きマーカー・理由なしマーカー・別行マーカー、単独製品名・`.codex/config.toml` 等の非検出、`references/*.md`・skill 直下の `README.md` と非 Markdown ファイルの検出、範囲外と既存除外 2 本の非検出も確認する。修正前の部分一致へ戻した、わざと壊したコピーで誤検出 4 例がすべて失敗することに加え、MCP の境界追加・Unicode `\b` 化・左右片側の境界削除・検査や大小無視の削除・ファイル全体の免除・`SKILL.md` 限定への後戻りも対応するテストで検出する。同じ配布物・走査範囲の免除前／後の `(ファイル, 開始行, 一致文字列)` を修正前後で比較し、意図した陰性以外の消失が無いことを確認する。fixture とわざと壊したコピーを除去し、最後に配布物の検証と全回帰テスト(回帰テストをすべて流すこと)を通し、`git status --short` に fixture 由来のエントリを残さない。質問の道具名と引数名は、案内が解決表の質問の節を指すこと・ほかの語の案内が変わらないこと・大小の変種・マーカー・範囲も、同じテストの中の、比べるための検出するケースと並べて確かめる

出典: `docs/minutes/2026-09-17_壁打ち_implementerの外部委託.md`(決定 23・24)・Issue #54・[Issue #101](https://github.com/Yuki-Maeda-valour/workflow/issues/101)(2026-10-06 のユーザー決定・設計)・[Issue #148](https://github.com/Yuki-Maeda-valour/workflow/issues/148)

## 8. このリポジトリへの還元フロー

1. 各プロジェクトで skill に改善を加えたら、プロジェクト固有部分を profile / 動的検出に置き換えた形でこのリポジトリに反映する
2. `plugins/dev-workflow/` 配下を編集して commit → main を取り込む → バージョンを plugin.json / marketplace.json で上げる(配るプラグインを変えた PR だけ)→ 全回帰テスト(回帰テストをすべて流すこと)を流す → PR を開く → merge。順序と、PR の本文に書く記録は、[AGENTS.md](../AGENTS.md) の「PR から merge までの順序」「PR の前の手順」の節が正本
3. 各プロジェクトでは `/plugin` の更新(marketplace update)で新版を取得

- 版の刻み: 後方互換な機能追加はマイナー、内部の移行・修正はパッチ、AI 非依存の到達点はメジャーで示す(委託の語の移行の版の刻み〈3.9.x → v4.0.0。2026-09-10 に到達〉は §7-7)
- Rust への移行の版の刻み(2026-11-07 に利用者が決めた。計画は [Issue #400](https://github.com/Yuki-Maeda-valour/workflow/issues/400)):
  - 移行の完了は v5.0.0 で示す。v5.0.0 は #400 の段階 7 で出す。段階 7 では、既定を Rust 版に切り替え、製品の Python 版と bash 版を外す。
  - v4 の間(v4.x)は、利用者の既定を Python・bash の旧版のままにする。Rust 版を選ぶ環境変数は `DEV_WORKFLOW_ENGINE` に決めた(2026-11-08。値の扱いは下の節)。段階 0b から試用の入口と実行ファイルを同梱する。段階 0〜6 の配布は、v4 のマイナーかパッチで出す。
  - v4 の最後の版の commit に git の tag を付け、「Python の最速系」(Python 版を速くし終えた最後の版)として残す。
  - 利用者の要件は v5.0.0 で変わる。python3・PyYAML が要らなくなる。Linux カーネル 5.6 以上・macOS 11 以上が要る。
  - なぜ移行の途中で既定を切り替えないか: v4 の途中で、利用者の要件(python3・PyYAML・bash・OS の版)を変えないため。
    - 例外: python3 の下限だけは、v4 の途中の #433 で 3.10 にそろえた(2026-11-07 に利用者が決めた)。理由は 2 つ。製品の一部(`diagnostic-*.py` など)が、既に 3.10 以上を要していた。テストの容器の Python が 3.10 で、下限の版でテストを流すため([Issue #433](https://github.com/Yuki-Maeda-valour/workflow/issues/433)。根拠は [testing.md](testing.md) の「Python の版の決まりの根拠」)。
  - なぜ v5.0.0 を移行の完了にするか: §7-7 の「完了をメジャーで示す」に合わせるため。
  - 却下: 「最初に Rust 版を配る版を v5.0.0 にする」。利用者が最初に挙げた案。v5.0.0 を移行の完了の節目にするため、採らなかった。

### Rust 移行の実装と配布(2026-11-08 決定)

- PR(変更を本流へ取り込む前に確かめてもらう依頼)を、実装の PR と配る PR に分ける。段階 2〜4 は並列に進める。出典は [Issue #400 の「進め方」の節](https://github.com/Yuki-Maeda-valour/workflow/issues/400)(2026-11-08)。
- 段階 0a([Issue #442](https://github.com/Yuki-Maeda-valour/workflow/issues/442))では、`native/`・`.github/workflows/`・検査の補助を作った。`plugins/`・`.claude-plugin/marketplace.json`・`setup.sh` は変えず、版と `CHANGELOG.md` も変えなかった。
- 段階 0b([Issue #443](https://github.com/Yuki-Maeda-valour/workflow/issues/443))から、実行ファイル 3 本と入口を同梱する。人が CI(変更を送るたびに自動で検査する仕組み)の成果物を取り込む。配布を開始する版は、main 取り込み後の v4 の次のマイナーとする。
- 段階 1a([Issue #448](https://github.com/Yuki-Maeda-valour/workflow/issues/448))では、開発用ソースに `resolve-task-dir` と旧版との比較を追加した。この段階では配布物を変えなかった。
- 段階 1b([Issue #450](https://github.com/Yuki-Maeda-valour/workflow/issues/450))では、新コマンドを含む 3 本と検査情報を取り込み、対象の skill と `loop.sh` を共通入口へつなぐ。配布元のソースと、入口を含む公開する SHA の検査は分ける。出所と検査手順は [testing.md](testing.md#段階-1b-の配布と導入の検査) に記す。
- 利用者向けに Rust を自動導入する処理は加えない。v4 の利用者の Python・PyYAML・bash の要件と既存の呼び出し方は維持する。
- [Issue #283](https://github.com/Yuki-Maeda-valour/workflow/issues/283)は、段階 2 で Git 設定の観測を比べるための入力と性質を固定する。廃止済みの #246・#282 の分割計画は再開しない。
  - 製品と既存の拒否条件は変えず、読み取り順序、処理量と時間の上限、変更の再検査、返す値のテストを追加する。
  - Python 内部だけの観察と、移行時に引き継ぐ性質は分ける。Rust 版との比較は未実施である。既存テストとの対応と、不具合を入れた独立コピーの確認方法は [testing.md](testing.md#git-設定の観測を移行へ引き継ぐテスト283) に置く。

`DEV_WORKFLOW_ENGINE` は、段階 0b から同梱する `bin/dev-workflow/launch.sh` が読む。段階 1b では保存先の解決をこの入口へ通す。既定は旧 Python のまま維持する。

| 値 | 1b の入口で選ぶ動作 |
|---|---|
| 未設定 | `resolve-task-dir` は同梱の旧 Python。それ以外の未登録コマンドは終了 2 |
| `legacy` | 未設定と同じ |
| `rust` | Rust 版 |
| 空文字・ほかの値 | 設定の誤りとして終了 2 |

- 切り替えは profile(プロジェクトの設定ファイル)から読まない。Rust 版が使えないときも、自動で旧版へ戻さない。
- 製品の実行ファイルのパスを指定する環境変数は作らない。`DEV_WORKFLOW_TEST_NATIVE_BINARY` は、検査対象の実行ファイルをテストへ明示するためだけに使う。製品の入口は読まない。
- 段階 1b の配布物は `resolve-task-dir` と、段階 0b の `--version`・`--help`・`probe --json`・`probe --self-sha256` を持つ。Rust 実行ファイル自体は Git・外部コマンド・ネットワーク・設定ファイルを使わず、作業フォルダを変えない。
- sh の入口は、Rust の選択時に OS・CPU・最低 OS と、選んだ 1 本のハッシュを外部の道具で検査してから起動する。旧版は同じ配布の `resolve-task-dir.py` を `python3 -B` で起動し、Rust 用の条件を要求しない。条件と試し方は [runtime-requirements.md](../plugins/dev-workflow/skills/do-task/references/runtime-requirements.md#rust-版を試す条件段階-1b) にある。
- `probe` に旧版は無い。入口の `--version`・`--help` も、未設定・`legacy` では終了 2 になる。試しのコマンドが通っても、旧版との同等性や OS の保護機能を確認したとは扱わない。

### タスク保存先の解決(段階 1a)

- `dev-workflow resolve-task-dir --project-root ROOT [--task-dir PATH]` は、旧版の `resolve-task-dir.py` と同じ優先順位で保存先を読み取りだけで解決する。引数・出力・終了値の元になる約束は [task-directory.md](../plugins/dev-workflow/skills/create-task/references/task-directory.md) にある。
- 成功は JSON の標準出力と終了 0、解決失敗は JSON の標準出力と終了 1、候補複数は JSON の標準出力と終了 2 を返す。これらの標準エラーは空にする。引数の誤りは、旧版の `argparse` と同じ順序で検査する。
- 一覧は読めても子の情報を取得する権限がない場合は、旧版との出力の差を認める。旧版は未処理の `PermissionError` を標準エラーへ出し、標準出力は空で終了 1 になる。Rust は同じ対象の権限エラーを JSON の標準出力へ出し、標準エラーは空で終了 1 になる。
  - 利用者が 2026-11-08 に承認した差である。子を持つ管理ルートを `0400` にした専用ケースで検査する。出典は [Issue #448 の技術的考慮事項](https://github.com/Yuki-Maeda-valour/workflow/issues/448)。
- 非 UTF-8 のパスが出力に含まれ、旧版の標準出力が `utf-8:strict` の場合にも差を認める。旧版は `main` の JSON 出力時に未処理の `UnicodeEncodeError` を標準エラーへ出し、標準出力は空で終了 1 になる。
  - Rust は元のバイトを保持した標準出力と、解決結果の終了値を返す。標準エラーは空にする。旧 Python と Rust 本体を変更せず、この出力を維持する。
  - 利用者が 2026-11-09 に承認した差である。`utf-8:strict` と `utf-8:surrogateescape` の専用ケースで検査する。出典は [Issue #448 の技術的考慮事項](https://github.com/Yuki-Maeda-valour/workflow/issues/448)。
- 権限と非 UTF-8 の専用ケースは、通常の一致判定に混ぜない。通常の比較で認める差は、実行名の表示と独立コピーの絶対ルートだけである。ほかの出力と終了値の完全一致を保つ。
- 比較は Python 3.10 系と通常利用者で実行する。Linux 2 種類・Mac 2 種類の CI は Python 3.10.11 を使う。実行前後のファイルが変わらないことも確かめる。詳しい検査は [testing.md](testing.md#タスク保存先の比較段階-1a) にある。
- 旧 Python の実装は残す。段階 1b は配布と入口の接続だけを変更し、この節の解決契約と承認済みの差を維持する。入口は引数・作業場所・標準入出力・終了値を加工せず渡す。
- `loop.sh` は保持したプラグインの入口を使い、起動直前の環境確認、作業場所 `/`、fd 7 の閉鎖、解決失敗を終了 30 にする扱いを維持する。成功後の文字制限・保護パスの確認も変えない。

### 共通の終了値と入出力(C0a)

[Issue #453](https://github.com/Yuki-Maeda-valour/workflow/issues/453)で、C0の最初の共通部品を開発用ソースへ実装した。
配布物と呼び出し元は段階 1b のまま。C0aの完了は、C0全体や段階 2 の完了を意味しない。

| 依存順 | 対象 | 状態 |
|---|---|---|
| C0a | 数値の終了値、途中出力と `flush`、生バイトの入出力 | 実装済み。未配布 |
| C0b | 処理量と時間の上限、開いたファイル実体の保持、再検査、必要な比較補助 | #456で実装済み。未配布 |
| C0c | 単一子の実行と回収、数値の終了とシグナル終了の区別、必要な比較補助 | #458で実装済み。未配布 |

- `ExitCode` は0〜255を無変換で保持する。数値の終了値をシグナル終了へ変換する処理は加えない。
- `CommandIo` は借用した入力1本と出力2本だけを使う。ファイルやプロセスを開かない。
  - 入力は指定された固定長バッファへ1回だけ読む。先読み・再試行・行解析・文字変換をしない。短読・EOF・入力エラーをそのまま返し、終了値はコマンドが決める。
  - 途中出力は生バイトを書き切り、同じ出力先の `flush` が成功してから返す。呼出順に出力するが、別パイプの読取り側が観測する順序までは保証しない。
- 最初の出力失敗を保持する。コマンドがエラーを無視しても、以後の通常出力と最終出力は出さず、実行入口は終了1を返す。
  - 標準出力の失敗では、既存形式の診断を標準エラーへ一度だけ試みる。標準エラーの失敗では追加診断を出さない。既に出たバイト列は取り消せない。
- `execute` は実際の標準入出力を同じ期間だけ借りる。`execute_to` は借用先を指定する。通常終了時の最終出力は、既存の `emit_to` による標準出力→標準エラーの順を維持する。
- CLIの登録表へ入出力を渡す。`probe` と `resolve-task-dir` は短い変換関数で既存の本体へつなぐ。公開するコマンドと引数は増やさない。
- 共通のCargo設定、CLI入口と登録表、coreの公開する型とテストの変更担当は、タスクごとに1名へ固定する。管理担当と独立レビュー担当は実装担当から分ける。
- コマンド固有の走査規則・保存形式・拒否条件は共通層へ移さない。旧Python、既存の期待値、通常の比較条件も変えない。#448の承認済みの差は保存先の専用比較だけに限定する。
- C0aは処理全体の期限、処理量の上限、ファイル保護、子プロセスの監督を保証しない。出力失敗を無視したコマンドを強制中断する機能も持たない。
- APIの詳細は [native/README.md](../native/README.md#終了コードと入出力c0a)、検査と未確認の条件は [testing.md](testing.md#共通の終了値と入出力の検査c0a) に記す。

### 処理量と時間の上限・保持実体の検査(C0b)

[Issue #456](https://github.com/Yuki-Maeda-valour/workflow/issues/456)で、開発用の共通部品と専用比較を実装した。
配布と既存の呼び出し元は段階 1b・プラグイン v4.53.0 のまま。単一子の実行と回収は下のC0cに記す。

- `core::budget` の `Budget` は、呼出元が決めた上限と絶対期限を保持する。既定値・リセット・期限の延長は持たない。
  - 項目数と総バイト数は加算後に上限を検査する。拒否した場合も消費量を戻さない。整数の桁あふれは保持し、後続の両方の加算を拒否する。
  - 期限確認は期限との等値を許す。残時間の取得は正の値だけを許す。加算・1ファイル上限・期限の確認を分け、呼出元が旧版と同じ順に組み合わせる。
- `platform::fs` の `HeldDir`・`HeldFile` は、開いた同じ実体を指すOSの識別子(fd)を所有する。開くときはリンクを辿らず、直前の属性と開いた実体を比べる。
  - `Full` は実体・種類・権限・容量・更新時刻・属性変更時刻を比べる。`DirectoryPath` は実体・種類・権限だけを比べ、子の増減による祖先の容量と時刻の変化を許す。
  - 絶対パスは `.`・`..` を保ったまま各段を辿る。指定ルートへの閉じ込めやパスの正規化は行わない。
  - `HeldFile::begin_read` は、読取り前に使用開始を記録する。本文を読む公開経路は排他的な `ReadSession` だけ。未読・EOF・拒否のいずれでも再使用を許さない。
- `core::fs::read_verified` は同じ `&mut Budget` を借用する。使用開始→宣言サイズの上限→本文読取り→同じ実体の `Full` 再検査の順に進む。
  - 毎回65536バイトを要求し、短読後も続ける。非空の読取りでは期限確認→総量加算→1ファイル上限の順を守る。項目数は加算しない。
  - 宣言サイズで総量を予約せず、実際の読取り長との一致も要求しない。失敗時に途中本文は返さず、消費量は保持する。
  - 最初の拒否後も再呼出しは `AlreadyRead` を返す。追加のOS操作と予算消費は0である。
- 期限は処理の途中で確認する方式である。停止中のOS呼出しを強制中断する保証はない。`NONBLOCK` でFIFOの書き手待ちは防ぐが、ファイルシステムの停止までは防げない。C0cの単一子実行も同じ限界を持つ。
- 親のパスを差し替えた後も、保持した旧親の本文を読める。最終の親パス再検査を一律には追加しない。名前の再検査が必要な呼出元だけが `HeldDir::verify_entry` を使う。
- 製品CLIは増やさない。検査用の接続では、C0aの `execute`・`CommandIo`・`ExitCode` と既存の比較補助を再利用する。

#### #454・R4Sへ引き継ぐ境界

[Issue #454](https://github.com/Yuki-Maeda-valour/workflow/issues/454)の読取り・上限・再検査の要件は、次の範囲まで共通部品で満たした。
R4Sの取得済みバイト列を扱う純粋部品は、下の [R4Sの節](#loop-state用の純粋部品r4s)に記す。C0bからの接続と、S2Lの用途別の状態観測・走査は後続の作業である。

| 今回実装・検査した範囲 | 後続の呼出元が決めて守る範囲 |
|---|---|
| 保持した親から同じ実体を読み、開く前後と読取り後の属性を比べる | profileと状態読取りへの接続、対象ごとの比較項目と検査順序 |
| 同じ予算の累積値と絶対期限を引き継ぐ | コマンド全体での作成位置、既定値、走査とGit操作の消費量 |
| `ENOENT` だけを不在とし、`ENOTDIR`・権限エラーなどを区別する | `.claude` やprofileの不在で既定指定を返す条件、固定の診断文 |
| 共通の既定上限を設けず、1 MiBを超える入力もloopの許容範囲で比較する | 各用途の上限を指定する。`secret-profiles` の1 MiBを流用しない。loopの1ファイル64 MiBも共通の既定値にしない |
| 親差替え後に旧本文を返す検査と、明示的な名前の再検査を分ける | 状態の観測全体の後段走査で必要な判定 |
| 検査用の接続は保持した親とファイルのスコープを終え、本文の `Vec<u8>` だけを返す | S2Lでも親fdをYAML解析前に閉じる。R4Sは取得済みbytesだけを解析する |

環境確認用の読取りが持つ総量予約・予定長一致・親全段の自動再検査は、このloop型の読取りへ統合していない。
YAML解析・機密指定・状態保存・走査・子プロセス監督・配布の変更は含まない。
公開APIの詳細は [native/README.md](../native/README.md#処理量と時間の上限保持実体の検査c0b)、固定期待値・旧版との比較・未確認条件は [testing.md](testing.md#処理量と保持実体の検査c0b)に記す。

### 単一子の実行と回収(C0c)

[Issue #458](https://github.com/Yuki-Maeda-valour/workflow/issues/458)で、直接起動する子1個の共通部品と専用比較を開発用ソースへ実装した。
配布物・既存の呼び出し元・旧Python・版・CHANGELOGは変更しない。子孫全体の監督を受け持つR4Qは後続である。

- `core::process::run_child` は、呼出元の `Command`・固定stdin・同じ `&mut Budget`・実行と回収の絶対期限・取消要求を借りる。下位で予算や期限を作り直さない。
- `platform::process` は標準入力・標準出力・標準エラーの3本のパイプを所有する。待たずに読み書きできる設定と `poll` で同時に進める。引数・環境・作業フォルダは呼出元が決める。
- 出力はconsumer(受け取ったバイト列を処理する呼出元の関数)へ渡す。C0aの `CommandIo` へ直接書ける。数値終了はC0aの `ExitCode` を使い、`ChildStatus::Signaled` と区別する。
- stdout/stderr別の上限、保持する途中出力、予算の計数順はconsumerが決める。共通部は項目数・総量・1ファイルの量を自動加算しない。旧loopの超過前拒否と、加算後に拒否する用途を一律化しない。
- stdinの途中閉鎖は `BrokenPipePolicy::Reject` と `Close` を明示的に選ぶ。前者は `InputClosed`、後者は入力を閉じて出力と回収を続ける。送信済み長と早期閉鎖を保持する。
- `RunFailure` は最初の原因、回収済みの終了状態、kill/waitの失敗、回収期限切れ、未回収の `Child` を分けて返す。取消・期限・OS・consumerの原因を後始末の結果で置き換えない。
- 失敗時は親側の3本を閉じ、未回収で生存を確認できた直接子へSIGKILLを1回試みる。回収済みの子へ信号を送らない。
  - cleanupの絶対期限が既に切れていても、初回の非待機確認、必要なKILL、直後の非待機確認だけは行う。通常の待機・再試行では期限を確認し直す。
  - `try_wait` の `EINTR`/`EAGAIN` 以外の失敗後は、開いたパイプの閉鎖以外のOS操作を追加しない。`pending_child` の所有権を呼出元へ返す。呼出元は保持して明示的に回収し、破棄だけを回収と扱わない。
- 成功には直接子の回収と全パイプの終了の両方が必要である。孫が出力パイプを保持すれば元の実行期限で失敗する。パイプを閉じて生存する孫は成功後も残り得る。
- 期限と取消は処理の合間に確認する。consumerの停止やpanic、停止中のOS呼出し、実行順の遅延を期限で中断する機能はない。親死亡後の子の停止・回収も保証しない。親のSIGTERM/SIGINT/SIGHUPを自動で捕捉・再送しない。
- 別の回収処理やSIGCHLDの自動回収との競合、子孫の停止と不在証明は対象外である。R4Qの監督は後続である。YAML解析と機密指定は下のR4Sの純粋部品が扱うが、C0cからの接続と#454の用途別の入出力規則は未実装である。

型と停止順序は [native/README.md](../native/README.md#単一子の実行と回収c0c)、必須比較と保証を確かめた範囲は [testing.md](testing.md#単一子の実行と回収の検査c0c)に記す。

### loop-state用の純粋部品(R4S)

[Issue #454](https://github.com/Yuki-Maeda-valour/workflow/issues/454)は調査と互換条件の記録である。
[Issue #462](https://github.com/Yuki-Maeda-valour/workflow/issues/462)で、R4Sのうちloop-state用YAML解析・機密指定・glob照合を開発用ソースへ実装した。
`core::loop_state`は渡された入力だけを使う。ファイル・Git・環境・時刻を読み取らず、外部Pythonを起動しない。

```text
取得済みbytes → UTF-8検査 → ノード作成 → 全ノードの明示重複検査
  → SafeLoader相当の構築 → 全要素の型検査 → glob検査 → 既定3件＋初出順

パスの整数列 ＋ 指定の整数列 → Python 3.10の規則によるglob照合
```

#### 文字表現とYAMLの採用部品

- `CodepointString`は非公開の`Vec<u32>`に、Unicodeの文字番号を順序どおり保持する。0〜0x10FFFFを受け、surrogate(UTF-16の代用符号の値)も失わない。2個のsurrogateを1文字へ合成しない。
- `try_from_codepoints`は値域外だけ`InvalidCodepoint { index, value }`を返す。空列・NULは文字型では受け、用途別に検査する。`String`やJSON文字列へ変換して一致を判定しない。
- YAMLのイベント解析には、`saphyr-parser 0.1.2`の局所修正版`workflow-yaml-parser 0.1.0`を採用した。元版のままでは、surrogate・Unicode改行・アンカー・構文条件・深さ制限が旧版と異なるためである。
- 上流commitは`478b9f76addf0687478113e0dd3a1dba86e4d274`。元crateのSHA-256は`f54c7c96aa2abeb55dbdcda75287722d5e454f020aaa817895d4d62b9785073a`である。
- `MIT OR Apache-2.0`のライセンス原文を保持する。直接依存は`arraydeque =0.5.1`と`thiserror =2.0.21`へ固定し、両方の既定機能を無効にする。間接依存は`Cargo.lock`で固定する。
- `Parser::new_from_iter`を使い、文字列用の最適化経路は使わない。スカラー値の整数列、旧版の文字・構文条件、EOF停止を局所修正した。上流の固定深さ制限と、環境を読む診断出力は含めない。
- この部品はイベントだけを作る。YAML 1.1の型解決と構築は`core::loop_state`が行う。元版・変更ファイル・ライセンス・更新手順は [UPSTREAM.md](../native/crates/yaml-parser/UPSTREAM.md)に残す。更新時も旧版との比較と追加の境界検査を省かない。

#### 受理条件と失敗の順序

1. 入力bytesを厳密なUTF-8と旧Readerの許容文字で検査する。先頭BOMは1個だけ除く。単一文書を解析し、alias(別の場所のノードへの参照)は同じ番号で保持する。
2. 全ノードの各mappingで、str tagの明示キー`secret_paths`が重複すれば拒否する。merge(別のmappingからの取り込み)と構築より前に調べる。使用しない子も訪問する。
3. PyYAML 6.0.3のSafeLoaderと同じ順序で構築する。未知tag、不正binary・日付、通常mappingのキーにできない値の拒否を維持する。SafeLoaderが構築しない子を一律検査しない。
4. 最上位がmappingなら`secret_paths`を得る。不在や最上位がmapping以外なら空配列とする。全要素が空でない文字列かを調べてから、glob(パスのパターン)を検査する。
5. 既定の`.env`・`.env.*`・`.dev.vars`を先頭に置く。完全一致の重複だけを除き、初出順を保つ。パスの正規化は加えない。

- `profile_patterns`の解析・明示重複・構築の失敗は`UnreadableYaml`。非配列・非文字列・空文字列は`NotStringList`。不正な指定は`InvalidGlob`である。
- `ProfileError::message()`は順に`secret_paths を安全に読めない`、`secret_paths が文字列配列でない`、`secret_paths に不正な glob がある`を返す。入力本文を理由へ足さない。
- `//`、`/`だけの指定、U+0000〜001F・007F、`'`・`"`・バッククォート・`$`・`[`・`]`・`{`・`}`を含む指定は拒否する。空白・`.`・`..`・バックスラッシュ・surrogateは独自に拒否しない。
- 明示値はmergeより優先する。merge配列は先頭が優先し、複数mergeキーは旧版の最後が優先する。自己merge・相互mergeも旧構築結果に従う。
- 型解決はYAML 1.1のままである。`yes`・`ON`はbool、`012`・`1:20`はint、`08`・`1e3`はstrになる。引用された値でも、非特定tagの`!`があれば型解決する。
- `LegacyIntLimit`は旧Pythonの整数変換設定を呼出元から受ける。0、または640〜2,147,483,647だけを受け、0は制限なしである。無効な設定は`InvalidLegacyIntLimit`で返す。環境からの既定値や新しい4300桁制限は設けない。YAML版の数字にも同じ設定を使う。

#### glob照合と承認済みの差

| 指定 | 旧Pythonと同じ意味 |
|---|---|
| `*` / `?` | `/`以外を0個以上 / 1文字番号。LFも対象 |
| `**` | LF以外を0個以上。`/`も対象 |
| `**/` | 空、またはLFを跨がない接頭辞と末尾`/` |
| 先頭`/` | rootからだけ一致。無ければLFを跨がない接頭辞の`/`直後も候補 |
| 末尾`/` | 対象自身、または`/`で始まるLFを跨がない子孫 |
| 終端 | 文字列末尾、または最後のLF1個の直前 |

- 指定の前後の`/`を全て除いてから照合の要素へ分ける。先頭・末尾の判定には除く前の指定を使う。空や`/`だけの指定は読み飛ばす。
- 大小文字はCPython 3.10・Unicode 13.0.0の1419組の固定表を使う。Rust標準の小文字化や正規表現の既定動作へ置き換えない。`secret_path`は任意の指定をそのまま照合し、機密指定の検査を追加しない。
- 唯一の承認済み差は、Pythonの実行環境の再帰上限だけで拒否される有限な入力を、Rustが反復処理で受理することである。2026-11-09の利用者の判断を#462へ記録した。新しい深さ・容量の上限は設けない。
- 固定入力`constructor/merge_chain/1100`では、旧版の`RecursionError`による拒否と、Rustの既定3件の受理を別々の期待値で確認する。scalarの自己参照、任意の`RecursionError`、未知の失敗へ許可を広げない。#448で承認した別用途の差も転用しない。
- ファイルの不在判定、C0b/C0cへの接続、S2Lの全7コマンド・状態保存・走査、`secret-profiles`等の他用途、R4Qの移植は含めない。R4A/R4Bの移植は別の節に記す。公開CLI・配布物・呼出元・版・`CHANGELOG.md`は変更しない。

公開APIは [native/README.md](../native/README.md#loop-state用の純粋部品r4s)、固定期待・旧Python・Rustの比較と未確認条件は [testing.md](testing.md#loop-state用の純粋部品の検査r4s)に記す。

### 本文の正規化と確認用の値(R4A task-digest)

[Issue #460](https://github.com/Yuki-Maeda-valour/workflow/issues/460)で、`task-digest.py`と同じ本文正規化・確認用の値・引数・入出力を開発用Rustソースへ実装した。
製品CLIの登録表とhelpへは接続していない。配布と既存の呼び出し元は段階1b・プラグインv4.53.0のままである。

- `cli/src/commands/task_digest/`が、UTF-8の確認、本文の正規化、SHA-256の先頭16桁、引数の診断、専用の出力処理を持つ。
- 先頭BOMは1個だけ除く。CRLF・CRをLFへ変え、LFで分割する。各行末の半角スペースとTABだけを除く。
- フェンス全体を検査してから、外側にちょうど1個ある`## 追加修正記録`の節を除く。最初の外側の節より前の可変ヘッダー3種、外側の空行も除く。
- 外側のチェック済み行を未チェックへ変える。字下げはPython3.10の正規表現の空白集合を使う。残る行をLFで連結し、末尾LFを加えずに値を計算する。
- フェンスは旧版独自の規則を保つ。未閉鎖と対応外の外側の字下げを拒否する。Unicodeの正規化や一般的なMarkdown解析は加えない。
- `platform::task_digest`は、リンクを辿る`stat`、`O_NONBLOCK`でのopen、同じfdの`fstat`、同じ実体からの読み取りを行う。通常ファイルへの親・末端リンクを許す。
- 開く前にFIFOへ変われば待たずに拒否する。開いた後のパス変更では元の実体を読む。inode一致、読み取り後の再検査、量や時間の上限は加えない。
- C0aの`CommandIo`と出力の型を使う。共通の`execute_to`へは空の成功出力をsinkへ渡し、実際の出力と終了値は専用処理が決める。C0bのリンク拒否は流用しない。
- 成功は値とLF・終了0、読み取り・UTF-8・構造の拒否は終了1、引数の拒否は終了2である。通常の出力失敗は旧Pythonの診断と終了120を保つ。書き込み・flush・部分出力を分ける。
- `COLUMNS`はPython3.10の整数変換へ合わせる。数字の桁数は符号とunderscoreを除き、Unicode数字と先頭0を含む。既定4300桁を超えればstdoutの端末幅へ戻る。
- 有効な`PYTHONINTMAXSTRDIGITS`の0(無制限)または640以上を反映する。Python起動前の無効設定、Pythonの`-X`と実行中の上限変更APIは再実装しない。
- 端末幅取得だけに既存`rustix`の`termios`を追加する。外部依存の版と自作のunsafeは増やしていない。

#### 承認済みの特殊な入出力3条件

2026-11-09の利用者承認に基づき、次の条件だけ例外の詳細表示を固定診断へ変える。
終了1と空の標準出力は維持する。任意の例外や#448・R4Sの承認済み差へ広げない。

- 書き手を開いた非同期stdinにデータがなく、読み取りが待ちを要求する場合は、`算出できない: 非同期の標準入力にデータがない`とLFを出す。
- バッファなし出力で成功値のstdoutが切断したパイプなら、`算出できない: 標準出力のパイプが閉じている`とLFを出す。
- バッファなし出力でLinuxの`/dev/full`への最初のwriteが`ENOSPC`なら、`算出できない: 標準出力の空き容量がない`とLFを出す。
- stdoutの2条件は、元のstderrが空・成功値・書き込み済み0・最初のwrite失敗に限る。`/dev/full`は実際に書く同じfdの文字デバイスmajor=1・minor=7で確認する。任意writer、別デバイス、flush失敗、部分出力へ広げない。
- `PYTHONUNBUFFERED`は生バイトを旧CPython3.10と同じ規則で読む。未設定・空・先頭ASCII空白と符号を許した末尾までの整数0だけが無効である。末尾空白、非UTF-8、非ゼロ値は有効である。
- stderrの書き込み失敗は通常時120・バッファなし時1を保つ。固定診断は通常のstderrと同じUTF-8・ASCIIの処理を通す。

APIは [native/README.md](../native/README.md#本文の正規化と確認用の値r4a-task-digest)、固定期待との比較と手元実測は [testing.md](testing.md#task-digestの比較r4a)に記す。

### origin判定とGit設定の確認値(R4B)

[Issue #461](https://github.com/Yuki-Maeda-valour/workflow/issues/461)で、`origin-repo.py`と`git-config-digest.py`を開発用Rustソースへ移した。
製品CLIの登録表・help・既存呼出元へは未接続であり、未配布である。配布は段階1b・プラグインv4.53.0のまま。

- `cli/src/commands/origin_repo/`がoriginを判定する。JSONは`origin`・`same`・`repo`・`form`・`vcs`・`reason`・`push_url_sha256`の順の7項目を保つ。
- Gitは`remote`→`remote.origin.vcs`→fetch URL→push URL→同一先の判定→必要なTLSまたはSSH設定確認の順に実行する。URL書換えはGitに任せ、拒否後の追加起動を行わない。
- fetch/pushが各1本のときだけpush URLのSHA-256を保持する。認証情報を除いた送信先だけを`repo`へ返し、URL・Git/SSH出力・設定値を診断へ転記しない。
- `cli/src/commands/git_config_digest/`は、Gitが返した3つの所在と、共有`config`・各作業ツリーの`config.worktree`を記述する。
- `lstat`・`readlink`・リンク先の`stat`で不在・通常ファイル・リンク・その他を分ける。ENOENT/ENOTDIRは不在とし、リンク先が通常ファイルの場合だけ項目を読む。
- `git config --file <path> --no-includes --list -z`の順序、重複、値なし/空値を保つ。include宣言は材料へ含めるが、この読取りでは展開しない。
- パス・リンク字面・設定値をASCIIエスケープし、キーをソートした空白なしJSONのSHA-256を作る。表示は`sha256:`と小文字64桁・末尾LF、終了0である。
- `--expect`一致は無出力・終了0、不一致は`git 設定のダイジェストが --expect と一致しない`とLF・終了1。不正形式と読取り失敗は終了2とする。
- `platform::r4b_process::capture`はstdinを`/dev/null`にし、stdout/stderrを同時に読む。originのGitは無期限、SSH設定の展開とdigestの各Gitはそれぞれ10秒である。全体を10秒にはしない。
- 時間切れでは直接子だけをkill/waitし、親のパイプを閉じる。孫のEOFは待たず、子孫全体の停止は保証しない。回収用の新しい期限・容量制限は加えない。C0cへ一律に接続しない。
- 引数の省略・重複・短縮・未知引数は旧argparseの順序を維持する。有効な`COLUMNS`を優先し、未設定・無効・0・負値ではstdoutの実端末幅を使う。端末取得の失敗時だけ80へ戻す。

#### R4Bに限る承認済みの差

出典は#461の2026-11-10の利用者判断である。旧Python・既存テスト・UTF-8設定の通常比較は変更しない。他コマンドの承認差は流用しない。

1. originのGit出力が厳密なUTF-8として読めない場合は、空stdout・終了1・UTF-8の`git の出力を読めない`とLFを返す。Git失敗の終了2へ混ぜない。SSHの解釈失敗は既存の拒否理由を含むJSONと終了0を維持する。
2. stdout/stderrの書き込み・flush故障はC0aで終了1とする。stdout故障の診断は`dev-workflow: 標準出力に書けません: <OSのエラー>`とLFである。診断先も故障すれば診断なしを許し、途中stdoutを空とは定めない。
3. Pythonの文字コード設定だけに由来する文字解釈は、引数・診断・`COLUMNS`・確認値材料のパス/リンク字面を含め、UTF-8と`surrogateescape`へ固定する。不正バイトを保持する扱いであり、originのGit/SSHの厳密なUTF-8検査を緩めない。
   - stdout/stderrは常にUTF-8とする。`PYTHONIOENCODING`・`PYTHONUTF8`・`PYTHONCOERCECLOCALE`・localeによる旧版の例外、エスケープ、分類、折返し、確認値、`--expect`、呼出列の差を個別期待へ固定する。
   - ファイル操作、Git引数、環境変数の生バイトを維持する。非UTF-8のファイル名設定でパス由来の確認値が変わり、Rustへ旧値を渡した場合も、既存の不一致診断と終了1を使う。

専用exampleの`r4b_contract`だけが2モジュールを起動し、C0aの`CommandOutput`・`emit`を使う。
Linux2種・Mac2種のCIは各ビルド2回と署名後Macの両CPUで専用比較を必須とする。検査用実行物は転送用だけに含め、配布物へ入れない。

```text
旧Python2本 ─────┐
                 ├─ 同じ物理入力 → 固定期待・出力・呼出順・入力無変更を確認
r4b_contract ────┘
```

APIは [native/README.md](../native/README.md#origin判定とgit設定の確認値r4b)、検査手順と手元実測は [testing.md](testing.md#r4bの比較)に記す。

### タスク本文の復元の内部部品(R4R)

[Issue #473](https://github.com/Yuki-Maeda-valour/workflow/issues/473)で、旧helper `plugins/dev-workflow/skills/do-task/scripts/restore-taskmd.py` と同じ結果を返す内部部品を、開発用Rustソースへ移した。
製品CLIの登録表・help・既存呼出元(`implement-guard.sh`・SKILL.md など)へは未接続であり、未配布である。配布は段階1b・プラグインv4.53.0のまま。
段階3の `implement-guard restore-taskmd`(S3I-A #474)が同じプロセスで呼ぶ予定である。独立コマンド `restore-taskmd` の登録は段階4に残す。

- 引数は `restore` と5つの組(`--source`・`--destination`・`--expected-mode`・`--expected-sha256`・`--cp`)を1回ずつ受け、バイト列で判定する。違えば TERM・INT・HUP の処理を設定せず、`REASON=internal-argument` と終了値33を返す。
- 作業は300秒とする。期限はC0bの `Budget::remaining` で見て、期限ちょうども拒否する。子の停止は TERM 後5秒・KILL 後5秒、後片付けは5秒、監視の間隔は0.05秒である。
- 読み取りの単位は65536バイト、応答の上限は4 MiB、`cp --version` の出力の上限は8192バイトである。
- 退避本文と復元先は、`/` から親の各段をリンクを辿らずに開く。復元先と同じ親に mode 0700 の `.restore-taskmd-<16進32桁>` を作る。
- fd の別名(`/proc/self/fd`、次に `/dev/fd`)で短いコピーを試し、GNU `cp -aL --reflink=never` で準備し、`renameat` で公開する。
  - GNU cp は、旧helperと同じく、`--version` の確認・別名ごとの短いコピー(全部で0〜2回)・本体のコピーの場面で起動する。2026-11-12 02:54の決定(管理担当が利用者の委任で決めた)の文:「R4R #473(restore-taskmd の部品)は、旧版どおり GNU cp を起動してよい。」
    - 決定の理由(要約): `cp --archive` 相当のコピー(mode・時刻・ACL・拡張属性)・cp の診断・Mac の `fd-copy-unavailable` の停止の契約は、Rust の中で同じ結果を作れない(OS ごとの `unsafe` が増える)。そのため「Rust の中で同じ結果を作れないもの」に当たる。本文の復元は呼ばれる回数が少なく、起動の回数を減らす目的にも反しない。
  - このため、2026-11-11 23:19の決定(Rust の中で同じ結果を作れる道具は起動しない。道具の例に cp を含む)は、R4Rの cp には当てない。
- 処理の順は、旧helperの関数の順のまま保つ。失敗から理由への対応と、`TOUCHED` と `TEMP` の決まりも旧helperと同じにする。
- 応答の行の形とC風引用は、旧helperと同じバイト列にする。
  - C風引用は、パスに制御文字(0〜31番と127番)・`"`・`\` があるときだけ、全体の前後に `"` を付ける。
  - 7〜13番は `\a`・`\b`・`\t`・`\n`・`\v`・`\f`・`\r`、`"` は `\"`、`\` は `\\` に書き換える。ほかの制御文字は `\` と8進3桁にする。UTF-8でないバイトはそのまま残す。
- `--cp` は Python 3.10 の `os.path.realpath`(strict でない)と同じ規則で、文字列の上で解決する。リンクの連鎖は991段まで解決し、992段で旧helperの `RecursionError` と同じく `runtime-unavailable` にする。`/bin` がリンクの機械で `/bin/cp` を経るときは、1段多く数える。
- コピーの子は、1回開いた `/dev/null` を0〜2番に使う。`--version` の子は、1番を出力を読むパイプにし、0番と2番に同じ `/dev/null` を使う。
  - どちらの子にも、0〜2番と、コピーの子に渡す2本のほかの fd を継がせない。`SIGPIPE`・`SIGXFSZ` を既定に戻し、mask は親のもの、環境は親の環境のまま継ぐ。
  - 子の準備と exec の失敗は、起動の失敗(`runtime-unavailable`)にする。子の状態の取得の `ECHILD` は、終了値0で終わったと扱う。

#### 旧Pythonの実行時の性質

Rustでも同じにする。CPython 3.10.12のソースと、旧helperの出力先を差し替える実測で確かめた。

- 起動時に `SIGXFSZ` を無視する。`SIGPIPE` はRustの実行時が無視する。書き込みの上限を超えた書き込みは、信号で終わらず `EFBIG` になる。
- PEP 538 の `LC_CTYPE` の書き換えを、部品の開始時にプロセスの環境へ行う。既にあれば同じ位置で置き換え、無ければ末尾に足す。子はその並びのまま継ぐ。
  - Linux(muslで組む)は環境変数だけで決める。`LC_ALL` が空で、`LC_CTYPE`(空なら `LANG`)が空・`C`・`POSIX` なら `C.UTF-8` にする。
  - Mac は旧Pythonと同じlibcで決める。`setlocale(LC_CTYPE, "")` の後の名前が `C` と等しいときだけ書き換える。`POSIX` は含めない。値は `C.UTF-8`・`C.utf8`・`UTF-8` のうち、`nl_langinfo(CODESET)` が空でない最初の名前である。
- `EINTR` は、旧helperが呼ぶPythonの関数ごとに、再試行するかを合わせる。CPython 3.10.12の `posixmodule.c` で確かめた表は、`native/crates/cli/src/commands/restore_taskmd/mod.rs` の先頭にある。
- TERM・INT・HUP の処理は `SA_ONSTACK` だけで設定し、`SA_RESTART` を付けない。`sa_mask` は空である。
- `os.urandom` が `getrandom`(Macは `getentropy`)の ENOSYS・EPERM で `/dev/urandom` に切り替え、その fd を開いたまま保持する動きも合わせる。
- 応答は、Pythonの溜めて書く出力と同じ規則で書く。溜め場所の大きさBは、stdout の `st_blksize`(1以下なら8192)である。
  - 明示の flush の後、Pythonの終了時と同じ順に4つを行う。(1) `flush_io` の flush(失敗を表示しない)。(2) `flush_std_files` の flush(失敗したら2行を出し、終了値を120に決める)。(3) TERM・INT・HUP を既定へ戻す。(4) 閉じる時の2回の flush(失敗を表示しない)。
  - 書く試みは、明示の flush から数えて全部で5回である。実測(2026-11-11、CPython 3.10.12)では、EIO が1・2回なら33・stderrなし・応答は書かれる。3・4回なら120・2行・書かれる。5回なら120・2行・書かれない。
  - 応答がBより長いときに直接の書き込みが失敗すると33を返し、終了時には何も出さない。

#### R4Rでは比べず、S3I-A #474で決めること

Pythonの実行時だけに由来し、検査専用入口では旧版と同じにできない4件である。R4Rは製品の入口を持たないので、ここでは差を認めない。
比較器が旧helperだけを流して記録するのは2つである。下の2の `LANG=xx_YY.UTF-8` は、記録した値を固定期待と照らす。下の3の待たずに書く設定のパイプの `EAGAIN` は、Linuxで結果を表示するだけで照らさない。
ほかはR4Rで記録していないので、S3I-A #474で測る。

1. 起動時に閉じた標準の fd(0〜2)と、フォルダなどの標準の fd。利用者の決定(2026-11-11 17:45)の範囲で扱う。
2. 読み込めない地域設定の名前(`LANG=xx_YY.UTF-8` など)と、`C.UTF-8` の無い環境での `LC_CTYPE`(Linux)。Macは同じlibcで決めるので比べる。
3. UTF-8でない地域設定での書き出しの失敗の1行目、`native/crates/cli/src/commands/restore_taskmd/response.rs` の `describe_errno` が文を持つ6つ(EPIPE・ENOSPC・EIO・EBADF・EFBIG・EDQUOT)のほかの errno の2行目、待たずに書く設定のパイプの `EAGAIN`。
4. 引数の誤りの応答を書く間の SIGINT と、Pythonの起動の途中に届いた信号。

#### 比較から外す1項目(`__CF_USER_TEXT_ENCODING`)

認めた差は、2026-11-12 02:41の決定の`__CF_USER_TEXT_ENCODING`の1件だけである。Macの旧Pythonが子の環境に足す変数を、比較から外す。ほかのコマンドで認めた差は持ち込まない。

- 実測(PR #483のCI・run 43067201954・Macの2機種): 環境を明示して渡した件で、旧helperの子(`cp --version`)にだけ`__CF_USER_TEXT_ENCODING=0x1F5:0x0:0x0`が付いた。Rustの子には付かなかった。
- 原因は、旧Pythonが読み込むmacOSの部品(CoreFoundation)が、環境にこの変数が無いと足すためと推定される。macOS 15のCoreFoundationの中身は未確認である。
- 決定(管理担当が利用者の委任で決めた。すべてのコマンドに当てる)の文:「旧版の処理系や OS の部品が、旧版の子の環境へ自分で足す値(例: Mac の旧 Python〈CoreFoundation〉が子に足す `__CF_USER_TEXT_ENCODING=0x<uid>:0x0:0x0`)は、Rust では再現しない。比較では、その変数だけを子の環境の比較から外す(値と有無を記録する)。」
- Rustはこの変数を足さない。子の環境のほかの値と並びは、旧版に合わせたままである。

`check_r4r.py` は、次のすべてを満たすときだけ、旧の側のcpの子の環境からこの1項目を除いてから比べる(`exclude_cf_encoding`)。

- Mac(darwin)で流している。
- 旧helperを起動したときに渡した環境に、この変数が無い。
  - `env` を指定した起動では、渡した `env` の中身で決める。指定しない起動(環境を受け継ぐ起動)では、起動の直前の比較器の`os.environ`で決める。
  - 渡した環境を記録していないときは、除かない。
- 旧の側のcpの子の環境に、この名前の項目がちょうど1個ある。
- その値が、`0x`と比較器のuidの16進(`format(os.getuid(), 'X')`。大文字・先頭に0を付けない。uid 0は`0`)と`:`を続けた文字列で始まる。小文字の16進など、ほかの形は除かない。

次のときは除かない。旧とRustが違えば不一致にする。

- Linuxで流しているとき。
- 渡した環境にあるとき。旧とRustが同じ値を継ぐので、そのまま比べる。
- Rustの側。渡していないのにRustの子に現れたら、不一致にする。
- ほかの名前・形の違う値・2個以上のとき。

除いたときは、比べる前に、cpの記録ごとに1行を出す。Macの実行の始め(比較器の自己検査より前)には、比較器の`os.environ`にこの変数があるかを1行出す。行の形は次のとおりである。

```text
R4R: 記録(__CF_USER_TEXT_ENCODING を旧の cp の子の環境から除いた): cp <回> 回目: 値 '<値>' / 渡した環境に なし / 旧の子に あり(1 個) / Rust の子に <なし|あり(<数> 個)|記録なし>
R4R: 記録(Mac の比較器の os.environ の __CF_USER_TEXT_ENCODING): <なし|あり(値 '<値>')>
```

- 既存テストの16件の組(旧helperとRustを順に流す組)も、同じ関数(`compare_observed`)で比べる。この組はcpの代わりの観測器を使わないので、cpの記録は両方0件で、除く項目は無い。
- MacのCIでの確かめ(PR #483のCI・run 43085911008・commit `0a44e83`): Mac 2機種の `build` が成功し、`check_r4r.py` は2回のビルドとも `R4R: 合格` だった。除外の記録の行は各回7行で、値は`'0x1F5:0x0:0x0'`・渡した環境に なし・Rust の子に なし だった。Macの実機は未確認である。

```text
旧helper(python3 -I -B restore-taskmd.py restore …) ─┐
                                                    ├─ 同じパスの配置・同じ argv → 終了値・stdout・stderr・ファイル・実体の関係・cp の観測・子の回収を比べる
r4r_contract restore … ──────────────────────────────┘
```

APIは [native/README.md](../native/README.md#タスク本文の復元の内部部品r4r)、比較の手順と件数は [testing.md](testing.md#r4rの比較)に記す。

### loop-stateの状態観測7コマンド(S2L)

[Issue #468](https://github.com/Yuki-Maeda-valour/workflow/issues/468)で、`loop-state.py`の7コマンドを開発用Rustソースへ移した。
製品CLIの登録表・help・配布物・`loop.sh`などの既存呼出元へは未接続であり、未配布である。配布は段階1b・プラグインv4.53.0のまま。
試せる入口は、開発用exampleの`loop_state_fixture`だけである。

- 対象は`snapshot`・`stop-control`・`preflight`・`derive-removed-worktree`・`state-digest`・`state-digests`・`compare`。引数・既定値・help・誤引数の診断は、旧`argparse`と同じ順に解析する。
- 終了値は旧版どおり。`Stop`(旧版が想定した停止)は終了20、`compare`の差は終了1、誤引数は終了2である。捕まえない例外は、旧版と同じ形の呼出履歴(traceback)と終了1を出す。
- 状態は`schema7`の12欄で保存する。ASCIIのエスケープ・キー順・区切りの空白なし・末尾LFなしで書き、孤立したsurrogateも失わない。
- 状態の読取りは旧`json.loads(bytes)`の規則を保つ。UTF-8/16/32とBOM、重複キーの最後の値、非有限数、任意精度の整数、`7.0 == 7`などの比較である。
- 保存は`.<名前>.tmp.<pid>`をmode 0600で排他的に作り、flush・fsyncの後に同じ親フォルダからreplaceする。失敗したときに残すファイルと消す順も旧版に合わせた。
- `state-digest`・`state-digests`はJSONを解析せず、生バイトのSHA-256を返す。直接の`OSError`を固定の理由へ変えるのは`state-digests`だけである。

#### 拒否の順序と機密の本文

```text
保存状態 → 管理先(.git・commondir)と候補の確認 → 全出典をGitなしで一致確認
  → 保持したfdを隔離したGitへ渡して解析 → include・条件の判定 → 通常のGit
  → 機密指定の累積 → 属性を先に調べる走査 → 状態の保存
```

- 既存の基準を固定する実行の途中では、保存時に無かった候補と未知のinclude先を、本文を開く前に拒否する。拒否した後にGitや設定を読み直さない。設定値を診断へ転記しない。
- 機密に当たるファイルは属性だけを記録する。機密のリンクの字面も読まない。lstatの後に種類と機密を判定し、必要な通常ファイルだけ本文を読む。
- profileは保持した親から読み、親を閉じてからYAMLを解析する。過去と現在の機密指定を初出順で累積する。
- 保存済みの出典・候補を固定する検査の例外は、旧版と同じく、初回・`--fresh-config-baseline`・親が指定した新しい自分のworktreeの`config.worktree`に限る。停止ファイルの例外も、旧版と同じ正確な指定に限る。
- 捕まえる例外の型・Stopの文・フレームは、旧版の`try`/`except`をすべて対照表で照らして合わせた。パスは旧版と同じく、os.pathの結果の文字列を字面どおりに比べる。

#### 保持fdと単一子の接続

- Gitの子は`platform::loop_state::LoopChild`で起動する。stdinとstderrはDEVNULL、stdoutだけパイプにし、直接の子だけを回収する。`index_snapshot`は3本ともDEVNULLの別の起動を使う。
- 子へ渡すファイルは、保持した通常ファイルか、`MemoryFile`(memfdで作るメモリ上の匿名ファイル)だけである。`/proc/self/fd/<番号>`の組み立てはplatformの中に閉じ、fdの番号を外へ出さない。
- 許可したfdだけを子へ引き継ぎ、無関係のfdは閉じる。親の読取り位置・CLOEXEC・所有権は変えない。
- 子の時間は、10秒と共有の残り時間の早い側である。超過したら、旧版の順で直接の子をkill・waitし、stdoutを閉じる。
- C0cの`DirectChild`・`run_child`には接続しない。パイプの数と回収期限の契約が違うためである。C0cのソースとテストは変えていない。
- 起動する外部の道具はGitだけである(23:19の決定)。

#### 再帰の文脈

- `core::loop_state::legacy_recursion`の`LegacyRecursionContext`は、旧CLIの`<module> → main → snapshot → profile_patterns`の呼出しの深さを表す(`recursion_limit=1000`・`entry_depth=4`)。
- `profile_patterns_for_loop`は、PyYAMLのcompose・重複検査・2回目のcompose・構築・mergeの各段階と、CPython 3.10の一時的な加算を数える。旧版が`RecursionError`で拒否する所で、同じ拒否をする。既存の`profile_patterns`(R4S)は変えていない。
- R4Sの再帰上限の承認済み差(有限な入力を受け付ける)は、S2Lへ転用しない。
- refsとファイルの走査・realpath・状態のJSONの再帰も、旧版が`RecursionError`になる境界を比較で確かめた。Rust自身のスタックの深さや新しい固定の上限は使わない。

#### OSの限界

- Macには`/proc/self/fd`が無い。旧版は、`/proc/self/fd/<番号>`を渡す`git config --file`の解析が失敗するので、`snapshot`・`preflight`を「git の状態読取に失敗した」で拒否する。
  - 前の状態の無い`snapshot`は、その解析の前に、worktreeの一覧と、停止ファイルの確かめ(停止ファイルの引数があるときの`stop_control_from_args`の`os.stat`)を行う。
  - 続けて、名前の順で最初のworktreeだけについて、`rev-parse`・`secret_paths`の読取り・`diff-index`・`config.worktree`とprivate refsの読取り・configの出典の一覧(`git config --show-origin`)を行う。2つ目より後のworktreeには届かない。
  - 前の状態を使う`snapshot`は、worktreeの一覧の前に、保存したconfigの確かめの中のその解析で拒否する。
  - `preflight`も、保存したconfigの確かめの中のその解析で拒否する。
  - Macの旧Pythonには`os.memfd_create`も無い。ただし、memfdを使うincludeIfの条件の判定は、その解析の後なので届かない。
- Rustも同じ所で拒否し、一時ファイルや`/dev/fd`へ切り替えない。
- Linuxの成功を、Macの成功として扱わない。Macでの実際の動作は未確認であり、PRのCIで確かめる。

#### 承認された差と全コマンド共通の決定

- 承認された差は、2026-11-11 12:39の利用者回答による文字コードの固定だけである。R4Bと同じ範囲で、7コマンドすべてに当てる。
  - `PYTHONIOENCODING`・`PYTHONUTF8`・`PYTHONCOERCECLOCALE`・locale・ファイル名の文字コード設定に由来する解釈を、UTF-8と`surrogateescape`へ固定する。Rustはこれらの設定を読まない。
  - ファイル操作・Gitの引数・環境変数・保存する状態の生バイトは変えない。整数変換の上限・端末幅・バッファの扱い・再帰は旧版どおりである。
- 全コマンド共通の利用者の決定(2026-11-11の17:45・17:58〈21:35に広げた〉・22:24・22:26・22:31・22:38・23:19と、2026-11-12の00:28・00:35・00:42)は、#468の本文の「全コマンド共通の決定の適用」の節に書いた範囲だけを当てた。
  - 出典は決定のファイルである: `rust-decision-startup-fatal-diagnostic.txt`・`rust-decision-script-path-in-diagnostic.txt`・`rust-decision-encoding-utf8.txt`・`rust-decision-runtime-env.txt`・`rust-decision-external-commands.txt`・`rust-decision-failure-modes.txt`・`rust-decision-bash-tools.txt`。
  - 当てたもの: 起動の失敗の`Current thread`の値の形式検査、導入先の固定名`<CPython3.10>/…`と旧スクリプトの固定名`loop-state.py`、本家CPython 3.10.11の行番号、SIGINT・SIGTERM、時間切れの秒の欄、保存の途中の書き込みの失敗、`onbranch`の一時の置き場、Git以外を起動しないこと。
  - 比較の前提の外にしたもの: 記憶域の不足、処理系の資源の上限(RLIMIT_NOFILEなど)で決まる失敗、決定が挙げたPythonの起動を変える環境変数の効果。
- 実装とレビューの間に、旧版との差を新たに認めた所は無い。見つけた差は、すべて旧版へ合わせて直した。
- R4S・R4A・R4B・#448の承認済み差は転用しない。

```text
旧Python(本家3.10.11) ─┐
                        ├─ 同じ物理入力 → 生出力・終了値・状態ファイル・入力無変更を確認
loop_state_fixture ─────┘
```

APIは [native/README.md](../native/README.md#loop-stateの状態観測7コマンドs2l)、比較の手順・件数・時間は [testing.md](testing.md#s2lの比較)に記す。

### R4U(check_links)の文書のリンク検査

[Issue #480](https://github.com/Yuki-Maeda-valour/workflow/issues/480)で、旧版 `plugins/dev-workflow/skills/update-doc/scripts/check_links.py`(93行)と同じ結果を返す専用の部品を、開発用Rustソースへ実装した。
製品CLIの登録表(`native/crates/cli/src/commands/mod.rs`)・help・既存の呼び出し元(update-doc の `SKILL.md`)へは未接続であり、未配布である。配布は段階1b・プラグインv4.53.0のまま。
旧版・`plugins/` の下のテスト・版・`CHANGELOG.md` は変えていない。
旧版の誤検出も変えない。例は、複数行にまたがる `[` から数える行番号と、バッククォートの組の取り違えである。直すなら別のタスクにする。

- `cli/src/commands/check_links/` が専用の処理を持つ。製品の実行物には入らない。検査用の実行物(`native/crates/cli/tests/check_links.rs`・`native/crates/cli/tests/check_links_units.rs`)だけが `#[path]` で読み込む。
- OS の呼び出しは `platform::check_links` に置く。cli 側の処理は `#![forbid(unsafe_code)]` を保つ。
- C0a から使うのは `ExitCode` の型だけである。`CommandIo`・`execute_to`・`emit_to` は、書き込みの時点と失敗の扱いが旧版と違うので使わない。C0b の保持実体・リンクの拒否と、C0c の子の実行は使わない。
- 外部の crate・`rustix` の機能・依存の版は増やしていない。`libc` の crate も使わない(OS の呼び出しの宣言の仕方は [C0b で採用した OS 呼出しの部品](#c0b-で採用した-os-呼出しの部品))。
- 旧版との比較は、CI の必須ジョブ `r4u` で流す。`build` の `cargo test --locked` では流さない(2026-11-12 12:59 の決定。Task 3.1c)。

#### 旧版と同じにする動作(要点)

細かい契約の正本は、Issue #480 の「維持する動作」の節である。ここには要点だけを置く。
「本家 3.10.11」は、CPython の公式の v3.10.11 のソースと同じ中身の Python を指す。

- 引数
  - 0個なら、旧版の説明の文(docstring)を stdout へ出して終了2。`PYTHONOPTIMIZE` が2以上なら、旧版と同じく `None` と改行を出す。
  - 1個以上なら、すべてを対象として順に扱う。`-h`・`--`・空文字も対象である。
  - 引数のバイト列は変えない。対象は pathlib の `PurePosixPath` と同じ規則で整えてから、OS の呼び出しにも使う(`a.md/` は `a.md` になる)。
  - 判定は `is_dir` → `is_file` の順で、どちらもリンクを辿る stat を使う。どちらでもない対象は、その時点で stdout へ `WARN: 対象が見つかりません: <引数>` を出す。この行は件数に数えない。
- 走査(`Path.rglob("*.md")`)
  - 本家 3.10.11 の `pathlib.py` と同じ順・同じ OS の呼び出しで行う。パスは文字列を連結した完全なパスで渡す。ディレクトリの fd から辿る呼び出し(openat など)にしない。
  - 集めたパスは、部品の列として Unicode の符号位置の順に並べる。UTF-8 でない名前は、surrogateescape(読めないバイトを U+DC80〜U+DCFF の文字に置く Python の扱い)の値で比べる。
  - 並べた後に、部品のどれかが除外の7個(`.git`・`node_modules`・`dist`・`build`・`.next`・`vendor`・`__pycache__`)と同じパスを外す。対象のパス自身の部品も数える。直接渡したファイルには当てない。
  - 権限の誤り(`PermissionError`)のディレクトリは黙って飛ばす。深さが Python の再帰の上限に当たると、旧版と同じ `RecursionError` の呼出履歴を出して終了1(本家 3.10.11 の Linux では深さ990から)。
- 2種類の照合
  - リンク `\[[^\]]*\]\(([^)\s]+)\)` と、コード中のパス `` `([A-Za-z0-9_.@/-]+/[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10})` `` の2つである。
  - 正規表現の外部の部品を使わず、旧版の後戻りと同じ一致を手で組んだ。空白は Python 3.10 の `\s` と同じ集合を使う。
  - 行番号は、一致の始まり(`[` か開きのバッククォートの位置)の行である。
  - 決まった乱数の種で作った12051件の入力で、旧版の関数の結果と比べる。
- 読み取り・解決・存在の確かめ
  - 本文は `open(path, "r", encoding="utf-8", errors="replace")` と同じに読む。読めないバイトは Python 3.10 と同じ範囲で U+FFFD にする。CRLF と CR は LF にそろえ、BOM は残す。FIFO は書き手が来るまで待つ。
  - `/` で始まらないリンクは、本家 3.10.11 の `resolve()`(strict でない)と同じ規則で解決してから、`exists()` で確かめる。`/` で始まるリンクは解決しない。
  - コード中のパスは、ファイルの親の祖先の先頭3個に連結したパスも順に探す。その後、必ず元のパスの `exists()` を確かめる。例外の出る時点を旧版と同じにするためである。
  - OS の誤りの例外は、旧版と同じ種類と本文にする。Linux の OS の文は glibc の文の固定表を使い、Mac は OS の文を使う。配布物が使う musl の文は使わない。パスの repr は Python 3.10(Unicode 13.0)に合わせる。
- 出力と終了値
  - 最後に `検査ファイル数: <数>`・`WARN  <内容>`・`ERROR <内容>`・`結果: ERROR <数> 件 / WARN <数> 件` の順に出す。ERROR が1件以上なら終了1、無ければ終了0。
  - 例外で止まったときは、旧版と同じ呼出履歴を stderr へ出して終了1。終わるときの書き出しが失敗したら終了120。
  - 呼出履歴の `File` の欄は、旧スクリプトを `check_links.py`、標準ライブラリを `pathlib.py`・`posixpath.py`・`genericpath.py` の固定名で出す。行番号とソースの行は、本家 3.10.11 の値である。
- 書き込みの区切りと故障
  - stdout と stderr は、本家 3.10.11 の `TextIOWrapper` と `BufferedWriter` と同じ時点・区切り・中身で書く。
  - stdout は、パイプかファイルなら溜めてから書く。`TextIOWrapper` の区切りは8192バイト、`BufferedWriter` の大きさは stdout の `st_blksize`(1以下なら8192)である。端末なら行ごとに書く。stderr は常に行ごとに書く。
  - `PYTHONUNBUFFERED` は、[R4A](#本文の正規化と確認用の値r4a-task-digest) と同じ規則で有効かを決める。有効なら、書くたびに出す。
  - 終わるときは、旧版と同じ4つの順に行う。(1) stderr・stdout の書き出し(失敗は黙って捨てる)。(2) 呼出履歴。(3) stdout・stderr の書き出し(失敗なら終了120)。(4) 閉じるときの書き出し。
  - 書き込みの失敗は、旧版と同じ例外・呼出履歴・書き込みの試行の回数・終了値にする。対象は、閉じたパイプの EPIPE・`/dev/full` の ENOSPC・`RLIMIT_FSIZE` の途中までの書き込みと EFBIG・待たずに書く設定の EAGAIN である。
  - 呼出履歴を出している間の失敗と SIGINT で、どの部品が欠けるかは、本家 3.10.11 で測った表(`native/fixtures/r4u/step2.json` の `table`。95行)に合わせる。
  - stdout と stderr への書き込みは、すべて libc の `write` を通す。比較器が共有ライブラリを差し込み、記録と故障の注入をするためである。製品の処理の中に、検査専用の分岐は置かない。
- 起動の失敗
  - 旧 Python と同じ順に調べる。`PYTHONHASHSEED` の不正 → `PYTHONINTMAXSTRDIGITS` の不正 → stdin・stdout・stderr がディレクトリか、の順である。
  - どれかに当たれば、旧版と同じ `Fatal Python error: ...` の診断を fd 2 へ直接書き、終了1。診断の `Current thread 0x<16桁>` の値は、実行中のスレッドの `pthread_self()` の値から作る。
  - SIGINT を捕まえる処理を置くのは、2つの環境変数の確かめの後で、標準入出力の確かめの前である(本家 3.10.11 の起動の順)。
  - 起動の前に閉じていた fd 0〜2 への出力は、旧版と同じく黙って捨てる。
- 信号
  - SIGINT は、起動時に既定の動作のときだけ捕まえる。`SA_RESTART` は付けない。
  - 捕まえたら旧版と同じ所で中断し、stdout を書き出して `KeyboardInterrupt` の呼出履歴を出す。最後に既定の動作へ戻し、自分へ SIGINT を送って終わる。
  - 引数0個の応答を書く間と、起動の失敗の診断を書く間に届いた SIGINT も、旧版と同じ結果にする(Task 3.1b。下の 08:55 の項)。
  - SIGTERM は既定の動作で終わる。SIGPIPE と SIGXFSZ は、旧 Python と同じ時点で無視にする。
- 文字コード
  - Python の文字コードの設定(`PYTHONIOENCODING`・`PYTHONUTF8`・`PYTHONCOERCECLOCALE`・地域設定)は読まない。引数・パス・stdout は生のバイトを保ち、stderr は UTF-8 と backslashreplace にする。

#### 実装の途中で決めたこと(2026-11-12。管理担当)

- SIGPIPE の扱いを main の前に控える(03:50)
  - 旧 Python は、`PYTHONHASHSEED`・`PYTHONINTMAXSTRDIGITS` の不正の診断を、親から受け継いだ SIGPIPE の扱いのまま書く。stderr が閉じたパイプなら、旧版は SIGPIPE で終わる。
  - Rust の起動の処理は、main の前に SIGPIPE を無視にする。そのままでは終了1になり、旧版と違う。旧版に合わせて直せる差なので、差を認めずに直した。
  - main の前に元の扱いを控え、この2つの診断を書く間は、元の扱いに戻す。旧 Python が無視にする時点(SIGXFSZ と同じ所)から無視にする。
  - 必須のケースに、親が SIGPIPE を既定にしたときと、無視にしたときの2つを足した。
- main の前の処理は検査用の実行物だけに置く(04:20)
  - 控える関数 `platform::check_links::capture_before_main` は platform に置く。
  - main の前に呼ぶ static(Linux は `.init_array`、Mac は `__mod_init_func`)は、検査用の実行物 `native/crates/cli/tests/check_links.rs` だけに置く。製品の実行物(`dev-workflow`)は変えない。
  - 同じ控えで、起動の前に閉じていた fd 0〜2 も、Python の `is_valid_fd` と同じ方法で記録する。Rust の起動の処理が閉じた fd 0〜2 へ開く `/dev/null` と、呼出元が渡した `/dev/null` を見分けるためである。
  - 控えが無いまま入口の `run` を呼ぶと、何も書かずに panic する(呼出元の誤り)。
  - 04:20 の決定は、後で製品へつなぐときに、配布の担当が製品の入口に同じ static を置くとした。08:55 の決定は製品の入口(`main.rs`)を変えないと定めるので、つなぐときに照らし、止まって聞く(下の 08:55 の項)。
- 呼出履歴を出している間の部品ごとの表(Task 2.3 の最初の作業)
  - 実装担当が本家 3.10.11 で測った。入力は3つ(1段の呼出履歴・前の例外とつながった2段の呼出履歴・`RecursionError`)。故障は4つ(SIGINT・1回だけの失敗・続く失敗・`RLIMIT_FSIZE` の途中で切る)。
  - 全95行を `step2.json` の `table` に置いた。管理担当が設計レビューの実測の例と照らし、すべて一致した。違いは1点で、`PYTHONUNBUFFERED` でも2段の区切りは1回の write だった。
- Mac の確かめ(07:55)
  - push 用の CI の Native(run 43114986888・commit 91bd35d)で、Mac 2種類とも、旧版と Rust の試しの書き込みが記録に現れた。
  - `RLIMIT_FSIZE` の動きは Linux と同じだった(上限まで書き、その後は EFBIG)。
  - 2種類の Mac が測った固定期待(482件)は、機械の欄のほかは同じだった。これを `native/fixtures/r4u/cases-macos.json` に置いた。
- SIGINT のケースを足す(Task 3.1b。commit 80b8cc2・841eb03)
  - 08:55 の決定の当て方を照らし、足りないケースを Task 3.1b で必須のケースに足した。どれも信号の組に入る。
  - 足したのは、引数0個の応答を書く間の SIGINT の6件(`signal-args-none-sigint-{pipe,unbuffered,pty}-{1,last}`)と、起動の失敗の診断を書く間の SIGINT の2件(`signal-env-hashseed-bad-word-sigint`・`signal-stdio-stdin-dir-sigint`)である。
  - FIFO の待ちの途中の SIGINT の7件には、送った時点の確かめ(呼出履歴の最後のフレーム)を足した。
  - 起動の失敗の2件の旧版の実測は、設計の推定どおりだった。`PYTHONHASHSEED` が不正なときは SIGINT で終わり、stderr は `Fatal Python error: ` の20バイトだった。stdin がディレクトリのときは、終了値1で診断の全文を出した。
  - Mac の固定期待には、push 用の CI の run 43152122842(commit 80b8cc2)のログから8件を足した(commit 841eb03)。比較器は、固定期待に無いケースだけを旧版(本家 3.10.11)で測ってログへ出す。`--extract-fixture` は、その値を既存の固定期待へ足す。Rust の出力は使っていない。既存の482件の項目は変えていない。
  - 当て方の中身は、下の 06:58・08:55 の項にある。
- 比較を build から必須ジョブ `r4u` へ移す(Task 3.1c。commit 841eb03)
  - 2026-11-12 12:59 の決定に合わせた。中身は下の 12:59 の項にある。

#### 全コマンド共通の決定の当て方

出典は #400 のコメントである。決定の時刻と #400 のコメントの見出しの対応は、Issue #480 の表にある。
R4U の実装と検査だけに当てる。ほかのコマンドの差へは広げない。広げる必要が出たら止まって聞く。

| 決定の時刻 | 中身 | R4Uでの当て方 |
|---|---|---|
| 2026-11-11 17:45・17:58 | 起動の失敗の `Current thread` の値・診断の旧スクリプトのパス | 値は位置と形式(16桁の16進)だけを比べる。旧スクリプトのパスは `check_links.py` の固定名にする |
| 2026-11-11 21:35 | 固定名を、標準ライブラリなど導入先のパスへ広げる | `pathlib.py`・`posixpath.py`・`genericpath.py` を固定名にする。比較器は3本の SHA-256 を本家 3.10.11 と比べ、違うファイルの欄を含むケースを失敗にする |
| 2026-11-11 22:24・22:26 | 文字コードの固定・標準ライブラリの行番号・途中で切れる出力 | UTF-8 と surrogateescape に固定する(stderr は backslashreplace)。設定で旧版が変わる条件(`PYTHONIOENCODING=utf-8` と UTF-8 でない名前など)は、設定ごとの別の期待で比べる。標準ライブラリの行番号は本家 3.10.11 の値にする。途中で切れる出力は、22:38 の行と同じ作り方で期待を作る |
| 2026-11-11 22:31 | 旧 Python の実行環境・起動を変える環境変数・実行ごとに変わる出力(後の決定では「22:35 の決定」と呼ぶ。同じ決定) | `PYTHONPATH` など一覧の変数と、sitecustomize の効果は再現しない。比較器は子の環境を、許すものを並べて一から作る。SIGINT は安定した部分を、SIGTERM は全バイトを比べる |
| 2026-11-11 22:38 | 固定名で短くなった出力と、途中までの書き込み | 固定名の欄を含む書き込みが途中で切れるケースは、[#471](https://github.com/Yuki-Maeda-valour/workflow/issues/471) の設計の (ア)(イ) と同じ作り方で Rust の期待を作る |
| 2026-11-11 23:19 | 外部の道具を起動しない | 当たる所が無い。旧版も Rust も子を起動しない |
| 2026-11-12 00:28 | 書き込みの途中の失敗・記憶域の不足・一覧に無い Python の環境変数 | 書き込みの区切りは旧版と同じなので、失敗の位置は実装に依らない。このため、`Current thread` の値の欄のほかは、全部の行と write の記録を比べる。記憶域の不足は比較の前提の外。一覧に無い変数(`PYTHONMALLOCSTATS` など)は再現しない。`PYTHONOPTIMIZE` と不正な `PYTHONHASHSEED` は再現する |
| 2026-11-12 00:35 | 中間の書き込みの故障・道具の版・bash の setlocale の警告 | 当たる所が無い。中間のファイルを書かず、bash も外部の道具も使わない |
| 2026-11-12 00:42 | 旧版の処理系自体の資源の上限 | `RLIMIT_AS`・`RLIMIT_NOFILE`・`RLIMIT_CPU`・`RLIMIT_STACK` などは比較の前提の外。比較器は設定せず、受け継いだ値を記録する。`RLIMIT_FSIZE` の書き込みの故障は旧版に合わせる |
| 2026-11-12 02:41 | 旧版の処理系や OS の部品が、子の環境へ自分で足す値 | Mac で、比較器が渡していない `__CF_USER_TEXT_ENCODING`(値が `0x<uid の大文字の16進>:0x…:0x…` の形)だけを、環境の確かめから外して記録する。旧版と Rust の両方の記録に同じ条件を当てる。出力の比べ方は変えない |
| 2026-11-12 06:58 | 競り合い・実行の時刻・プロセスの番号で変わる値の比べ方 | 旧版は一時ファイル・時刻・プロセスの番号を出さない。信号は順序を決めて送るので、06:58 の緩め(残る物の範囲で比べる)は使わない(下の項) |
| 2026-11-12 08:55 | 閉じた fd 0〜2・子へ渡す LC_CTYPE・strerror の表・EAGAIN と起動の途中の信号・bash の ERR の表示・書き出しの失敗 | 下の項のとおり。子へ渡す LC_CTYPE と bash の2項目は当たらない(子を起動せず、bash を使わない) |
| 2026-11-12 09:58 | bash が環境から読み込むほかの変数は再現しない | 当たる所が無い。bash を使わない |
| 2026-11-12 11:57 | 共有ファイルは作業ブランチの上で並行して編集してよい・マージは準備ができた順に1本ずつ | 共有ファイル(この節を含む5つの文書・`native/crates/cli/Cargo.toml`・`native/crates/platform/src/lib.rs`)と、12:59 の決定で変えた CI の4ファイル(`.github/workflows/native.yml`・`native/ci/build.py`・`native/ci/check_workflow.py`・`native/ci/scripts/test_native_ci.py`)は、作業ブランチの上で足すだけにした |
| 2026-11-12 12:59 | 新しいコマンドの比較は build と verify-universal に入れず、コマンドごとの新しい必須ジョブで流す | 比較を必須ジョブ `r4u`(build の後・4機種)へ移した。build には単体テスト(`check_links_units`)と clippy だけが残る(下の項) |

「比較の前提の外」は、旧版と Rust の結果の違いを失敗にしないことを指す。

06:58 の当て方(信号を送る時点):

- 信号は、共有ライブラリが決まった write の直後に送るか、比較器が子の FIFO の待ちに入ったことを確かめてから送る。待ちの確かめは、Linux が `/proc/<pid>/wchan`、Mac がスレッドの状態である。
- Mac の待ちの確かめは推定である(動くスレッドが0で、Unix の呼び出しの回数が0.3秒変わらない)。
- 早すぎた送信のうち、SIGINT の処理を置く前の送信は、最後の行が `KeyboardInterrupt` にならず失敗する。置いた後で FIFO を開く前の送信は、比べる欄では見分けられない。
- これを補うため、FIFO の待ちの途中の SIGINT の7件(`signal-fifo-sigint-*` の6件と `traceback:read-fifo-sigint`)で、呼出履歴の最後のフレームが本家の標準ライブラリの `pathlib.py` の open の行であることを確かめる(Task 3.1b)。比べる欄は 22:35 の決定のまま変えない。
  - このフレームは、送った時点が open の行の中(open の呼び出しの直前の一瞬か、FIFO の待ち)だったことだけを示す。FIFO の待ちに入った後に送った主な根拠は、Linux の wchan と Mac の推定である。
  - open の呼び出しに入った後で、FIFO の待ちに入る前に届いたときは、待ちが EINTR で戻らず、時間切れで失敗する(CPython のコードからの推定)。
- SIGTERM は、待ちの前に WARN を書き出さない形では、送る時点で出力が変わらない。WARN を先に書き出す形(`PYTHONUNBUFFERED`・疑似端末)では、早すぎる送信は固定期待の WARN の行と合わずに失敗する(ケースの作りからの理由。早すぎる送信を起こしての実測はしていない)。

08:55 の当て方:

- 閉じた fd 0〜2
  - 決定は、Rust の標準の初期化(閉じた0〜2を `/dev/null` で開き直す)をそのまま使い、旧版との差を認め、製品の入口(`main.rs`)を変えないと定める。
  - R4U は製品の入口を変えず、標準の初期化もそのまま動く。検査用の実行物だけが、標準の初期化より前に動く控えの処理(`.init_array`・`__mod_init_func` の static)で元の状態を控え、旧版と同じ結果を出す。決定が認めた差を使わないだけで、決定を変えない。
  - Rust の本処理は、控えが済んだことを要求する。製品へつなぐときの入口の扱いは、そのときに 08:55 の決定と照らし、止まって聞く(#480 の範囲の外)。
- strerror(OS の誤りの文)
  - Linux は Rust が固定表を持つ。表は作業機の glibc 2.35 から作った。容器の Ubuntu 24.04(glibc 2.39)の文と、errno 1〜133 ですべて同じことを確かめた(2026-11-12)。表に無い番号は、glibc と同じ `Unknown error <n>` を出す。
  - Mac は実行時に OS の strerror を呼ぶ。決定の表は CI の macOS の文なので、CI の macOS では表と同じ文になる(Mac 2種類の CI の比較で、共有ライブラリで当てた書き込みの失敗のケースの文が旧版と一致した)。ほかの Mac では、その機械の旧版と同じ文になる(決定より厳しい側)。
- EAGAIN
  - 決定は、読み手の速さで変わる実行ごとの出力として、安定した部分だけを比べると定める。
  - R4U のケースは、読み手のいない満杯の非ブロッキングのパイプを渡すか、共有ライブラリで決まった時点に当てる。時点が読み手の速さに依らないので、緩めを使わず全部を比べる(決定より厳しい側)。
- 起動の途中に届いた信号
  - 起動の正常な経路には write も待ちも無く、時点を決めて送れない。このため、正常な経路を狙うケースは作らない。
  - 起動の失敗の経路は、診断を fd 2 へ書く。その1回目の write の直後に SIGINT を送るケースを足した(Task 3.1b)。対象は、値の確かめの側(`PYTHONHASHSEED` が不正。`signal-env-hashseed-bad-word-sigint`)と、標準入出力の確かめの側(stdin がディレクトリ。`signal-stdio-stdin-dir-sigint`)の2つである。
  - この2つは、時点が write で決まり、Python の呼出履歴を出さない。このため、22:35 の緩め(安定した部分だけ)を使わず、stdout・stderr・終了値・信号・write の記録の全部を比べる(決定より厳しい側)。旧版が SIGINT で終わっても、最後の行が `KeyboardInterrupt` であることは求めない。
  - 診断の `Current thread` の値の欄だけは、17:45 の決定のとおり形式を確かめて伏せる(既存の `stdio-stdin-dir` のケースと同じ。write の記録も同じ)。
  - 旧版の結果は、固定期待を作るときの実測で確かめ、設計の推定どおりだった。値の確かめの側は、SIGINT の処理を置く前なので SIGINT で終わった。標準入出力の確かめの側は、置いた後なので印が立つだけで、終了1になった。
- 引数の誤りの応答を書く間の SIGINT
  - 旧版の引数0個の応答(docstring と終了2)を書く間に、共有ライブラリが stdout の1回目と最後の write の直後に SIGINT を送るケースを足した(Task 3.1b。バッファあり・`PYTHONUNBUFFERED`・疑似端末の6件。`signal-args-none-sigint-{pipe,unbuffered,pty}-{1,last}`)。
  - 比べ方は 22:35 の決定の SIGINT の扱いである。旧版が SIGINT で終わったときは、中断の比べ方にする。
- 書き出しの失敗の文
  - `Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>` の `encoding` は `utf-8` にする(上の文字コードの固定)。

12:59 の当て方:

- R4U の比較(`native/crates/cli/tests/check_links.rs` が起動する比較器 `check_r4u.py` と自己検査 `test_r4u.py`)を、build の `cargo test --locked` から外し、新しい必須ジョブ `r4u` で流す(Task 3.1c)。
  - ジョブ `r4u` は build の後に、Linux 2種類・Mac 2種類の4機種で流れる。中では `python3 -B native/ci/build.py r4u --platform <機械>` が `cargo test --locked -p dev-workflow-cli --test check_links -- --r4u-compare` を流す。
  - テストの実行物は、`--r4u-compare` を受けたときだけ比較器と自己検査を起動する。受けないとき(`--r4u-compare` を付けない `cargo test --locked`)は、`R4U の比較は流していない: ` で始まる1行を出して終了0にする。この1行は比較の合格に数えない。
  - この1行は、比較器の結果の行の頭(`R4U の比較: `)とは別の文字列にした。build のログにこの頭が無いことで、build で比較を流していないことを確かめられる。
  - build に残るのは、単体テスト(`check_links_units`)と `cargo clippy --all-targets` の検査だけである。
- 検査対象は、機械ごとに作る動的リンクのテストの実行物である。配布物でも example でもない。Mac 共通の実行物を作らないので、combine の後の Mac 2機種のジョブは作らない。
- verify-universal は R4U を流していないので変えない。既存のジョブの上限も変えない。ジョブ `r4u` の上限は30分である。最初の実測(run 43157591744)では、最も長い Mac x86_64 でも3分21秒だった。
- 「必須」の意味: main には保護の設定が無い。マージの条件は「PR 用の CI の全ジョブが同じ attempt で成功」なので、ジョブ `r4u` の失敗はマージを止める。R4E #482 の `r4e` の形に合わせ、`distribution` の `needs` には入れない。
- ジョブを足すのに合わせ、`native/ci/check_workflow.py` の許す形と必須の数え方も同じ変更で直した(12:59 の決定の2項目め)。

#### 比べ方

```text
旧Python(本家 3.10.11) ─┐                      ┌ 固定期待(native/fixtures/r4u/)
                         ├─ 同じ入力・同じ環境 ─┼ stdout・stderr・終了値・信号
検査用の Rust 実行物 ────┘  (同じ共有ライブラリ  ├ write(2) の区切りと中身(記録)
                              で記録する)       └ 入力の無変更・新しいファイルが無いこと
```

- 比較器 `native/ci/check_r4u.py` が、旧版・固定期待・Rust の3者を比べる。`cargo test --locked -p dev-workflow-cli --test check_links -- --r4u-compare` で流れ、CI では必須ジョブ `r4u` で流れる。`--r4u-compare` を付けない `cargo test --locked` では流れない。
- 標準ライブラリの行番号を比べるので、本家 3.10.11 と同じ中身の Python が要る。作業機では `R4U_PYTHON` で渡す。
- 必須のケースは508件である。Mac で行わなくてよいケースは、閉じた一覧(`/dev/full` が無い・UTF-8 でない名前を作れない・旧版の試しの書き込みが記録に現れない)だけとする。件数と理由を出し、成功に数えない。
- 信号を送る時点の確かめと、Task 3.1b で足したケースの比べ方は、上の 06:58・08:55 の当て方にある。

API は [native/README.md](../native/README.md#r4ucheck_linksの文書のリンク検査)、比較の流し方と件数は [testing.md](testing.md#check_links-の比較r4u)に記す。

### Rust の構成とビルド入力(段階 0a)

- `native/Cargo.toml` は、`native/` 内の `crates/cli`・`crates/core`・`crates/platform`・R4Sの `crates/yaml-parser` をまとめる。公開コマンドへ移植する機能は `native/crates/cli/src/commands/<名前>/mod.rs` に追加する。試しの `probe.rs` は単独ファイルとする。
- Rust は `native/rust-toolchain.toml` で `1.99.1` に固定する。`rustfmt`・`clippy` を用意し、`Cargo.lock` を共有する。内部の版 `0.1.0` は配布プラグインの版と分ける。
- `platform` 以外の自作 crate(Rust の部品単位)では `#![forbid(unsafe_code)]` を使う。`platform` は `#![deny(unsafe_op_in_unsafe_fn)]` を使い、安全な関数だけを外へ公開する。
  - 例外は、R4U の `platform::check_links::capture_before_main` だけである。main の前に static から呼ぶ形でだけ使う([C0b で採用した OS 呼出しの部品](#c0b-で採用した-os-呼出しの部品))。
- ビルドの設定は `native/Cargo.toml`・`native/.cargo/`・`native/ci/` に置く。`.github/workflows/native.yml` の実行コマンドは `native/ci/` のスクリプトを呼ぶだけにする。
- ビルド先は `CARGO_TARGET_DIR` でリポジトリの外に置く。導入と必須の検査は [AGENTS.md の Rust の検査](../AGENTS.md#rust-の検査段階-0a)、4 種類の CI と比較の補助は [testing.md](testing.md#rust-の開発と検査段階-0a) に記す。
- ソース入力の SHA256(内容の違いを確かめるための値)は、`native/` の Git 追跡ファイルだけから作る。パス順に、リポジトリ直下からの相対パス・Git のモード・作業中の本文を、それぞれの長さと一緒に計算へ入れる。
  - 通常ファイルは本文を読み、リンクは参照先の名前を読む。生成物・未追跡ファイル・`native/` の外の変更は含めない。
  - `native/crates/core/source_input.rs` と `native/ci/source_input_hash.py` が同じ形で計算する。`check_probe.py` は実行ファイルの値を Python の計算値と比べる。
  - `native/crates/core/build.rs` は Git でリポジトリ直下と追跡一覧を求める。Git が無い・一覧が取れない・空の場合は止める。追跡ファイル全部と Git の index(次の commit に載せる内容の置き場)を再計算の監視対象にする。
- 実行ファイルには入力の SHA256 と Rust の版を埋め込む。commit の SHA・時刻・CI の実行番号は入れない。同じソース・依存・道具・設定なら、異なる commit でも同じバイト列を作る方針とする。
- `build-info.json` は `schema_version`・`source_commit`・`source_input_sha256`・`cargo_lock_sha256`・`rustc` を持つ。`targets` に対象ごとの道具と設定、`ci` に実行番号と URL、`artifacts` に 3 本の相対パスと SHA256 を記録する。
- `native/ci/make_dist.py` は、3 本と `SHA256SUMS`・`build-info.json` の 5 ファイルを作る。`scripts/validate.py` の `check_native_bundle(path, "artifact")` に通してから、CI が `native-dist.tar` にまとめる。
- 0b の配布物は `plugins/dev-workflow/bin/dev-workflow/` に置き、`launch.sh` を足した 6 ファイルとする。`check_native_bundle(path, "distribution")` は実行せずに形式・権限・容量・ハッシュ・記録の一致を検査する。
  - 0b から配布フォルダを必須にする。`check_native_distribution()` は、フォルダの欠落・一組の不足・リンクの異常を ERROR にする。
  - 検査は配布物の内部の一致だけを求める。開発中の `native/` と古い配布物の違いは不正にしない。配布へ取り込むときは `source_input_hash.py` を使い、ビルド元の入力と 3 本を比べる。
  - 静的な形式検査だけで、入口の動作を確認したとは扱わない。0b では `launch.sh` の実行テストと、配布した版の導入確認も行う。

### Rust で使う部品の候補(段階 0 では依存に加えない)

- 段階 0 のYAML候補は `saphyr-parser`だった。R4Sでは局所修正版とPyYAMLとの比較を実装した。採用した版と条件は [R4Sの節](#文字表現とyamlの採用部品)にある。0a の `probe` は YAML を読まない。
- OS を呼ぶ部品の候補は `rustix`。Linux の権限を制限する Landlock には `landlock`、macOS で変化の通知を受ける kqueue には `nix` を候補とする。
- これらは候補の記録だけで、段階 0 では依存に追加しない。0a の直接依存は `serde`・`serde_json`・`sha2` だけ。出典は [Issue #442 の Task 3.1](https://github.com/Yuki-Maeda-valour/workflow/issues/442)。

#### C0b で採用した OS 呼出しの部品

- 2026-11-09のC0bで `rustix` を採用した。段階 0 の候補記録は上に残す。`native/Cargo.toml` で `=1.1.6` に固定し、`platform` だけに `fs` と既定の `std` を追加した。
- R4Aのtask-digestではstdoutの端末幅取得に`termios`を加えた。
- C0cでは匿名パイプの待機確認に `event` を加えた。起動・単一子KILL・回収は `std::process` を使う。`rustix` の `process`・ネットワーク機能は有効にせず、依存の版と自作のunsafeは増やさない。
- R4R(#473)では、既存の `Cargo.lock` の版の `libc 0.2.191` を `platform` の直接依存に加え、R4Rの自作の `unsafe` を `native/crates/platform/src/restore_taskmd.rs` だけに置いた。cli と検査専用入口は `unsafe` を使わない。
  - 旧helperの `_posixsubprocess` と同じ子の準備が要るためである。子だけの `RLIMIT_FSIZE`、`close_range` か列挙した fd への exec の時に閉じる印、`SIGPIPE`・`SIGXFSZ` の既定、mask、`execv` は、exec の前に子の中で動く。`std::process::Command` の `pre_exec` と `libc` でしか書けない。
  - std の `execvp` は ENOEXEC で `/bin/sh` へ切り替えるので、旧helperと同じ `execv` を自分で呼ぶ。
  - 最初の信号だけを記録する処理(`sigaction`)と mask、子への信号、乱数、Mac の地域設定の判定(`setlocale`・`nl_langinfo`)、環境の `LC_CTYPE` の書き換えも、同じ理由で同じファイルに置く。
  - `rustix` の機能と、依存の版は増やしていない。
- S2L(#468)では、依存を増やさず、自作の `unsafe` とCの関数の宣言(`extern "C"`)を `native/crates/platform/src/loop_state.rs`・`loop_state_signal.rs`・`loop_state_watch.rs` と、テストの `loop_state_tests.rs` に置いた。cli・core と検査専用入口の `loop_state_fixture` は `unsafe` を使わない。
  - `loop_state.rs`: Gitの子の準備を `pre_exec` の中で行う。旧CPythonの`subprocess`と同じく `SIGPIPE`・`SIGXFSZ` を既定へ戻し、許したfdだけを引き継ぐ(Linuxは `close_range` の `syscall`、使えなければディレクトリの列挙。`signal`・`fcntl`・`getrlimit`・`syscall`・`open`・`close` と、Macの `__getdirentries64` を直接呼ぶ)。
    - `syscall`: Linuxの `close_range`(番号436)と、fdの列挙の `getdents64`(x86_64は217、arm64は61)に使う。
    - `__getdirentries64`: Macのfdの列挙(`/dev/fd`)に使う。Apple Libcの非公開のexportで、公開APIではない。Macでの実際のリンクと動作は、まだCIで確かめていない(PRのCIで確かめる)。
  - `loop_state_signal.rs`: 起動時の信号の初期化(`signal`・`siginterrupt`・`sigaction`)、起動の失敗の診断に使う主スレッドの識別子(`pthread_self`)、SIGINTによる終了(`kill`・`getpid`)。
  - `loop_state_watch.rs`: Linuxの `inotify`(`inotify_init1`・`inotify_add_watch`)で、テストが持つフォルダのopenだけを監視する(製品の経路では使わない)。
- R4U(#480)は `libc` の crate を使わない。`rustix` の今の機能に無い呼び出し(`sigaction`・`kill`・`getpid`・`pthread_self` など)と libc の `write` を、`native/crates/platform/src/check_links.rs` の中で `unsafe extern "C"` として宣言し、安全な関数で包んだ。`sigaction` の構造体は OS ごとに宣言する。
  - stdout と stderr への書き込みに libc の `write` を使うのは、Linux の `rustix` が libc を通らない(`linux_raw`)ためである。libc を通らないと、比較器が共有ライブラリを差し込んで、記録も故障の注入もできない。
  - 例外が1つある。main の前に static から呼ぶ `capture_before_main` だけは、`pub unsafe extern "C" fn` として公開する。直接は呼ばない。
  - 検査用の実行物 `native/crates/cli/tests/check_links.rs` は `#![deny(unsafe_code)]` の中で、main の前に呼ぶ static の `link_section` のためだけに `unsafe_code` を許す。cli の `commands/check_links/` は `#![forbid(unsafe_code)]` のまま。
  - `rustix` の機能と、依存の版は増やしていない。
- [公式の `openat`](https://docs.rs/rustix/1.1.6/rustix/fs/fn.openat.html)と [公式の `statat`](https://docs.rs/rustix/1.1.6/rustix/fs/fn.statat.html)は、保持した親の識別子を受ける。所有する型で閉鎖を管理でき、自作の危険なOS呼出しを増やさずに済むため選んだ。
- 依存の根拠は [1.1.6のパッケージ](https://docs.rs/crate/rustix/1.1.6/source/Cargo.toml.orig)と、配布ソースの [OS呼出し実装](https://github.com/bytecodealliance/rustix/blob/4a563ad698b2c20ff848515821023d2265a788d0/src/backend/libc/fs/syscalls.rs)・[ビルド設定](https://github.com/bytecodealliance/rustix/blob/4a563ad698b2c20ff848515821023d2265a788d0/build.rs)である。Linuxは `linux_raw`、Macは `libc` の既定を使う。
- `Cargo.lock` に必要な間接依存を加え、既存依存の版は上げていない。Rustは1.99.1のまま。YAMLは後のR4Sで局所修正版を採用した。Landlock・kqueueの部品は未採用である。
- Mac向けのソースや型検査だけでは動作を確認したことにしない。Linux2種・Mac2種のCIで実ファイル検査を必須にする。C0bのMac実機と最低OSでの動作は未確認である。

### 公開経路の安全用のオプション

公開、commit、index 操作を含む Git 呼出は hook、fsmonitor、署名、filter を無効化する共通の安全用のオプションを使い、品質確認を hook に委ねない。公開 helper は保持した完全 SHA を送信し、origin・設定・remote SHA・作成 PR を照合する。同じ OS 利用者の任意プログラムを隔離するものではないため、保持値と結果は worktree 外で照合する。
