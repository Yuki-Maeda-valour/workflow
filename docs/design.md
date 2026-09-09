# dev-workflow 設計書

このリポジトリの skills 集がどう設計されているか、skill を追加・改修するときに必ず守る規約は何かを定義する。複数の実プロジェクトで実証されたベスト要素を統合したものが本 skills 集である。

## 1. 目的

- どのプロジェクトでも(技術スタック・リポジトリ構成を問わず)使える汎用ワークフロー skills を 1 か所で管理する
- 新プロジェクト開始時に `/plugin marketplace add` 一発で全 skills を導入できるようにする
- 各プロジェクトで育った改善をここに還元し、全プロジェクトへ配布する

## 2. コアサイクルと skills 責務マップ

```
/init-project(初回のみ: 標準構成を生成)──チェーン提案──→ /stack-research
      │                                    (依存バージョン固有の調査 → doc/06。
      ▼                                     実在する問題はタスク化をチェーン提案──→ /create-task)
/understand-project ──→ /create-task ──→ /do-task ──→ /update-doc --task ──→ PR
 (把握。grasp にキャッシュ、  (種別判定+設計書生成。   (implementer 委託+機械検証   (完了タスク駆動の差分同期。
  毎セッション hook が促し)   --refactor = リファクタ分析)  +実動確認+独立レビュー)     タグ昇格・ADR・図・索引)
                             └──────────────── /ship-task(一気通貫)────────────────┘
                              薄いオーケストレーター。上の 3 工程を順に実行し、作業ブランチ作成・
                              実装 commit・doc commit・push・PR 作成まで出す。既定は走り切り、
                              要件不明の残存・品質ゲート赤・スコープ縮小・レビュー未収束でのみ停止する

品質系(任意の時点): /tool-check = ツールによる機械検査(チェック 3 階層の 1+2。第 3 層の実動確認は do-task)
                    /data-audit = データ境界監査(機密露出・認可欠如・過剰取得を 3 層で検査、裏取り済み指摘を提案)
                    (承認された指摘はタスク化をチェーン提案──→ /create-task)
入力系(任意の時点): /discuss-spec = テーマ単位の壁打ちで仕様を決め、決定録を生成(doc は書かない)──チェーン──→
                    /reflect-decisions = 原資料(議事録・文字起こし・チャットログ・資料・壁打ち決定録)から
                    決定事項を抽出 → 精査(裏取り・レビュー・確認)→ doc/(01/03/04/05/07)へ出典付き反映
                    (update-doc が「実コード→doc」なのに対しこちらは「人の決定→doc」。
                     実装が必要な決定・宿題は /create-task へチェーン提案)
出力系(任意の時点): /export-doc = doc/ をクライアント提出用に PDF / xlsx / HTML へ変換
                    (サニタイズ確認+機密検査。doc/ は不変。変換ツールは動的検出で縮退あり)
```

| skill | 責務 | 読む | 書く |
|---|---|---|---|
| init-project | 標準構成(権威参照ファイル(AGENTS.md)+ CLAUDE.md の import 1 行 / doc/ / task/ / profile)+ MCP セットアップ(opt)の生成 | 対象プロジェクト全体 | AGENTS.md, CLAUDE.md, doc/, task/, .claude/, .mcp.json, .codex/config.toml |
| understand-project | プロジェクト把握。読み取り専用(キャッシュ除く) | profile, 権威参照ファイル, メモリ / doc, コード | .claude/grasp.md(キャッシュのみ) |
| create-task | 種別判定・影響範囲調査済みのタスク設計書生成(--refactor = 対象発見型リファクタ分析) | コード全域 | task/進行中_*.md のみ |
| ship-task | create-task → do-task → update-doc --task を通しで実行し、ブランチ・commit・push・PR まで出す薄いオーケストレーター(工程の中身は持たない) | 各工程の成果物 | 3 工程の書き込み+ git ブランチ / commit / PR |
| do-task | タスク実装(委託)〜機械検証・実動確認〜独立レビュー〜完了処理。検証のみモード可 | task MD, コード | ソースコード, task MD |
| update-doc | メモリ / 権威参照ファイル / doc/ の実コード同期(--task = 完了タスク駆動。タグ昇格・ADR・索引) | コード全域, 完了タスク MD | メモリ, 権威参照ファイル, doc/(索引含む) |
| tool-check | ツールによる機械検査(format/lint/typecheck/test/build)一括実行 | package.json 等 | 自動修正のみ |
| stack-research | 依存の実バージョンに固有の注意点・ベストプラクティス・脆弱性の Web 調査とノート生成 | マニフェスト, lockfile, Web | doc/06_stack-notes.md, doc/README.md |
| data-audit | サーバー境界を越えるデータの監査(機密露出・認可欠如・IDOR・過剰取得・DB 防御)。読み取り専用で裏取り済み指摘を提案し、承認分を /create-task へチェーン | コード全域, スキーマ, profile, doc/06 | .claude/reviews/(レポートのみ) |
| reflect-decisions | 議事録・文字起こし・チャットログ等の原資料から決定事項を抽出し、精査(原文裏取り・レビュー・ユーザー確認)を経て doc/ へ出典付きで反映(決定と未決の峻別・矛盾は対比確認)。未決・宿題はタスク化候補として報告 | 原資料, doc/, profile | doc/ 01・03〜05・07(02・06 は対象外), doc/README.md(索引) |
| discuss-spec | テーマ単位の壁打ちでユーザーと仕様を決める(論点分解 → 選択肢・推奨提示 → 1 論点ずつ合意)。結論を決定 / 暫定 / 未決 / 却下に峻別した決定録を生成し /reflect-decisions へチェーン | doc/, 実装コード, profile | 決定録(doc/minutes/ 等)のみ。doc/ は書かない |
| export-doc | doc/ をクライアント提出用に PDF / xlsx / HTML へ変換。サニタイズ確認(地雷・管理タグ・内部記述)+機密検査+ Mermaid 画像化。ツールは動的検出・縮退あり | doc/, profile | export/(出力のみ。doc/ は不変) |

## 3. 固有情報の 3 層吸収アーキテクチャ(最重要原則)

**skill 本文は手順(how)だけを持ち、プロジェクト固有の事実(what)を一切ハードコードしない。** 事実は以下の 3 層で実行時に解決する。全 skill がこの順で解決すること。

| 層 | 手段 | 吸収する情報 |
|---|---|---|
| 1. プロファイル | `.claude/project-profile.yml` を Read(存在すれば) | リポジトリ構成 / 品質コマンド / area 定義 / メモリ名マップ / 正本の向き / 機能フラグ |
| 2. 動的検出 | Glob / ls / list_memories を実行時に叩く | パッケージマネージャ / 技術スタック / ディレクトリ構造 / 実在メモリ名 |
| 3. 権威参照 | 権威参照ファイル(検出順は下記)と doc/ を Read | プロジェクト固有原則 / 禁止事項 / 地雷 |

**profile が無くても必ず動く**こと(全項目にフォールバックを定義する)。これが汎用性の担保。

**吸収対象はプロジェクト固有の事実だけでなく、ツール固有の事実(外部 CLI の起動コマンド・フラグ・モデル名、およびホスト内蔵機構の API 名・エージェント種別・モデルエイリアス)も含む。** **API 名・エージェント種別はホスト固有の事実であり、解決表(§7-5)を唯一の正本とする**(skill 本文にも profile にも置かない)。あわせて §7-2 の信頼モデルにより、リポジトリ側が書ける値で委託先の機構が変わることも避ける。モデルエイリアスは既存どおり profile の `review_models` / `implementer_model` → 実行時に利用可能なエイリアスを確認する(§5-4・§5-9)。解決表はエイリアスの指定方法(Claude Code なら Agent ツールの `model` 引数)だけを持ち、一覧は持たない。外部 CLI のコマンド文字列を SKILL.md 本文に書かず、既定値は references の表(外部 CLI は external-runners.md、委託は解決表 §7-5)に置き、profile(`features.runner_models` 等の**コマンドにならない値**のみ)→ 実行時検出(`command -v` 等の存在確認)→ references の既定値、の順で解決する。**起動コマンドそのものは profile から受け取らない**(外部ランナーの起動コマンド。§7-2 の信頼モデル。MCP 宣言は §7-6 の例外)。

### 動的検出の標準ロジック

- パッケージマネージャ: package.json の `packageManager` フィールド → lockfile(pnpm-lock.yaml / bun.lock* / yarn.lock / package-lock.json)→ 既定 npm。PHP は composer.json、Python は pyproject.toml / requirements.txt、Go は go.mod、Rust は Cargo.toml
- 品質コマンド: profile の `quality` → package.json の scripts から `format` / `check` / `lint` / `type-check`|`typecheck` / `test` / `build` を存在検出 → 言語別既定(PHP: `./vendor/bin/pint --test` + `php artisan test`、Python: ruff/pytest 等)
- モノレポ: pnpm-workspace.yaml / package.json#workspaces から自動列挙
- 権威参照ファイル: `AGENTS.md` があればそれ → 無ければ `CLAUDE.md`(未移行)。両方あれば `AGENTS.md` を正本とし、`CLAUDE.md` が `@AGENTS.md` の import を含まなければドリフトとして注記する(§5-11 と同型)。import に加えてホスト固有の追記を持つ構成は正常。**ホスト側の探索順・サイズ上限は §7-3(事実と出典)、運用方針は §7-4**
- Serena: 使う前に get_current_config でアクティブプロジェクトを確認し、違えば activate_project(別プロジェクトのメモリ誤参照防止)。メモリ名は list_memories() で動的列挙し、固定名を仮定しない

## 4. project-profile.yml スキーマ

置き場所: 各プロジェクトの `.claude/project-profile.yml`。init-project が生成する。**全フィールド任意**(無い項目はフォールバック)。

```yaml
# プロジェクト名(表示用)
name: MyProject

# リポジトリ構成: single(既定) | parent-child | monorepo
# parent-child: AI 管理リポジトリの下に本体ソースが独立 git である構成
repo_layout: single
# 本体ソースのルート(parent-child のときのみ。探索・コマンド実行は必ずこの下で行う)
root: .

# コードを持つか(false: ドキュメントのみのプロジェクト。品質ゲートをスキップ)
has_code: true

# パッケージマネージャ(省略時: packageManager フィールド / lockfile から自動判定)
package_manager: pnpm

# コード理解系(概要・構造・技術・規約・コマンド)の正本: serena(既定) | docs | claude-md
#   serena:    Serena メモリが正本、権威参照ファイルは薄型維持
#   docs:      doc/(02・05 等)が正本、メモリは探索用の要約(一方向同期)
#   claude-md: 権威参照ファイル自体が正本(小規模・メモリ未使用)。値名 claude-md は据え置き(改名は未決)
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
  build: pnpm build          # optional: true を付けると完了条件から除外
  build_optional: true

# 型伝播チェーン(create-task の影響範囲調査 Phase 1.5 で使用)
type_propagation_chain: "DB schema → server functions → API routes → frontend hooks → components"

# ドメイン固有リスク(create-task / do-task がチェックリストに反映)
domain_risks: []             # 例: [web3, i18n, e2e-data, salesforce, zenstack]

# 機密ファイル(読まない・値を出力しない・ログ混入を検査)
secret_paths: [".env", ".env.*", ".dev.vars"]

# データ境界監査(data-audit)の調整(任意。無ければ既定辞書+全境界を対象)
audit:
  sensitive_fields: []       # 機密フィールド名の追加(既定辞書に加算。例: [memberCode, planRank])
  public_boundaries: []      # 認可チェック不要と確認済みの公開境界(パス・ルート名)
  exclude_paths: []          # 監査対象から除外するパス(生成コード・vendor 等)

# 実行機能レベル(タスク系 skills の重さを調整)
features:
  impact_analysis: true      # create-task Phase 1.5(影響範囲調査)
  external_review: true      # create-task Phase 4.5(多モデル外部レビュー)
  reviewer_count: 1          # do-task のレビュアー数: 1 | 3
  review_models: [fable, opus, sonnet]  # レビューに使うモデル群(省略時: 環境で利用可能な最上位から能力順に自動選定)
  implementer: internal      # internal | cursor(外部 CLI 委託)。cursor は未実装(起動コマンド未定義)で、
                             # 宣言しても内部 implementer で動作する。レビューの多様性が要るなら runners を使う
  implementer_model: sonnet  # implementer サブエージェントのモデル(エイリアスのみ)
  # ── 外部ランナー(レビュアーをホスト外の CLI に委託する。既定は内蔵のみ・オプトイン)──
  runners: []                # 例: [cursor-agent, codex]。空 = 外部 CLI を探しに行かない(既定の編成を変えない)
                             # ここで選べるのは do-task/references/external-runners.md の既定表にある名前だけ
                             # ③ CLI バックエンドの有効化(§7-5)。② MCP の有効化キーは未定で、決まるまで ① 既定から変えられない
  runner_models: {}          # ランナー別モデル名。省略時はランナー既定(モデル指定フラグを付けない)
                             # 例: {cursor-agent: "<そのランナーで有効なモデル名>"}
                             # '-' で始まる値・空白を含む値は拒否される(フラグへの化けを防ぐ)
  # ※ 起動コマンドの上書きは profile では受け付けない(下の「信頼モデル」を参照)

# MCP サーバー(ツール接続)。宣言のキー名と値の形は未定で、決まるまで生成しない(§7-6)

# ローカル環境の構築コマンド(環境構築を依頼されたとき doc/05 と併せて参照される)
setup_commands: []           # 例: ["docker compose up -d db", "pnpm db:migrate", "pnpm db:seed"]

# ※ モデル名の扱い: §5-4 のエイリアス規約は Claude のモデル(`review_models` / `implementer_model`)に適用する。
#   外部ランナーのモデルにはエイリアスが存在しないため `runner_models` で実名を指定し、
#   skill 本文・references にはハードコードしない(§3 のツール固有情報の 3 層解決)。
#
# ※ 信頼モデル: この profile は commit される共有ファイルであり、リポジトリを書ける者が内容を書ける。
#   したがって「profile に書いてある = ユーザーの意思」とは扱わず、**リポジトリ側が書ける値から
#   実行されるコマンドが変わってはならない**。外部ランナーの起動コマンド上書きは profile では
#   受け付けず(書かれていても無視し、無視した旨を報告する)、セッション内でユーザーが明示指定した
#   ときだけ有効になる。既定表のランナーでは実行ファイル名と読み取り専用フラグを既定値で固定する。
#   MCP サーバー宣言はこの原則に対する明示的な例外であり、生成前に解決後の宣言を提示して
#   ユーザーの明示承認を得る(§7-6)。

# 地雷リストの場所の上書き(任意)。既定は doc/05_operations.md の「引き継ぎ・地雷」節
# (全ツール・人間から見える doc/ が正本。.claude/ 配下は Claude 専用になるため置かない)
known_facts_ref: docs/HANDOVER.md
```

## 5. 全 skill 共通の統一規約

調査で検出した不統一・矛盾を以下に統一する。**全 skill がこれに従う。**

以下の各項に現れる `Agent` / `SendMessage` / `ListAgents` / `Explore` / `general-purpose` / モデルエイリアスは、**ホスト = Claude Code における解決先**である。skill 本文は役割語(一覧は §7-5)で委託を書き、これらの語は references の解決表からのみ参照する(§6・§7-5)。**役割の分離・段階判定と縮退の規則はホスト非依存**である。`name` は 2 層に分かれる — 双方向連携を持つホストでは、委託先を宛先として識別可能にすること自体はホスト非依存(§5-17 フル段階。名前が無いと双方向連携の経路が塞がる。標準・最小段階では宛先が存在せず要請自体が生じない)。一方 `name` という引数名と文字種の規則は Claude Code の解決(解決表が正本)。具体の解決(API 名・`name` 規則・エイリアス)は解決表を正本とし、**解決表が入るまでは §5 の記述が暫定の正本**とする。

1. **タスクファイル命名**: `task/進行中_{タスク名}.md` → 完了時に `git mv` で `task/完了_{タスク名}.md`。中断は `中断_`、保留は `保留_`
2. **skill 名**: `update-doc`(update-docs は使わない)。旧 refact / refactor は `create-task --refactor` に統合(分析 → タスク化 → do-task で実行)
3. **サブエージェント API**: `Agent` + `SendMessage` + `ListAgents` のみ。TeamCreate / TeamDelete は使わない(廃止方針。チームはセッション単位の暗黙チームで足り、明示生成は不要)。**Agent 起動時は必ず `name` を付ける**(SendMessage の宛先になる。名前が無いと双方向連携の経路が塞がる)。`name` は**半角英数・ハイフン・アンダースコアのみ**で役割が分かる短い語にする(日本語は使えない。例: `implementer` / `reviewer-opus` / `scan-exposure`)。SendMessage が使える環境(Claude Code で Agent Teams 有効)では**フル段階での運用を既定とし**、使えない環境では前提にせず §5-17 の縮退プロトコルに従う
4. **モデル指定**: エイリアスのみ(`fable` / `opus` / `sonnet` / `inherit`)。日付付きモデル ID・版数(「Opus 4.7」等)をハードコードしない。利用可能なエイリアス群はモデルの世代交代で変わるため固定リストとして扱わず、実行時に Agent ツールの `model` パラメータで指定可能なものを確認する
5. **ホスト自身と同じ CLI を Bash から起動しない**(別課金・二重課金の回避)。ホストが Claude Code なら `claude -p` / `claude --print` の Bash 起動を禁止、ホストが Codex なら `codex exec` の起動を禁止、という**ホスト相対の規約**として読む。ホスト内のサブエージェントは必ずホストのエージェント機構(Claude Code なら Agent ツール)経由で起動する。**ホストと異なる CLI をレビュアー(外部ランナー)として起動することは、`--runners` または `features.runners` で宣言されている場合に限り許す**(例: Codex / Cursor をホストとする環境から Claude CLI をレビュアーに使う)。宣言が無ければ外部 CLI を探しに行かない。ホスト判定は環境変数で行う(Claude Code は `CLAUDECODE` の存在で判定。他ホストの判定方法は未検証のため、`external-runners.md` に候補と上書き手段を置く)
6. **検証の主体**: スコープ縮小を検出する側(team-lead)は実装者と分離する。**規模に関わらず実装は implementer に委託し、team-lead は常に検証者に回る**(直接実装は §5-17 の環境縮退時のみ)。完了条件のコマンドは team-lead が自分で再実行する(implementer の自己申告を最終確認にしない)
7. **スコープ縮小検出 grep**(do-task の検証で使用): `段階的に実施|後続タスク|今回はスコープ外|のみ作成|次回対応|一旦`
8. **チェックリスト突合の機械化**: `grep -cE '^\s*- \[(x|X)\]'` で件数突合+diff との整合確認
9. **レビューの扱い**: 多モデルレビュー — **実行環境で利用可能なモデルを実行時に確認し、能力上位から順に(利用可能な最上位を必ず含めて)能力帯の異なる 2〜3 体を選び**、単一メッセージで並列スポーンする。組み合わせを固定せず、モデルの世代交代に自動追従させる(Claude Code では Agent ツールの `model` で指定可能なエイリアスが利用可能一覧にあたる。現時点の目安: fable(最上位)+ opus(上位)+ sonnet(標準))。profile の `features.review_models` があればそれを優先する。**既定はホスト内蔵のモデルのみで編成する**(オプトイン方式。② MCP は §7-5。有効化手段が未定のため、現時点の編成は ① と、宣言時のみの ③ で構成される)— 外部 CLI や他ベンダーのモデルを探しに行かず、ホストのエージェント機構で指定可能なモデル(Claude Code なら Agent ツールのエイリアス)の上位から選んで完結させる。**外部ランナーが宣言されている場合に限り**(`--runners` 引数、または profile の `features.runners`)、ベンダー横断の多様性を同一ベンダー内の能力帯差より優先し、各社の最上位級を組み合わせる(製品名・版数をハードコードせず実行時に選ぶ)。宣言があっても外部ランナーが利用不可のとき(未導入・認証切れ・レート制限・応答なし・読み取り専用未確立・一時ツリーを作れない)は、ベンダー横断を諦めて**内蔵のみの編成に縮退**し、縮退したことと理由を報告に明記する(サイレント縮退禁止)。宣言されているのに使えない場合は**エラーとして報告**する(黙って内蔵だけで済ませない)。外部ランナーの起動手順・判定順序・機密ガードは §7 と `do-task/references/external-runners.md` に従う。モデルを選べない環境では、観点(事実整合 / セキュリティ / 規約)を分けた複数レビュアーで多様性を確保する。**規模適応**: 変更対象が少数(目安 3 件以下)で矛盾の無い軽微な実行では、観点を分けた単一レビュアー(またはセルフレビュー)へ軽量化してよい(採用した編成を報告に明記する)。→ team-lead が各指摘を実コードで裏取りし valid / invalid / needs-user にトリアージ。指摘を盲信して自動反映しない。false positive は理由を記録
10. **レビューループのセーフティ**: **収束条件は全 reviewer の APPROVED**。コストを理由にレビュー反復を打ち切らない。同一指摘の 2 回連続残存・5 ラウンド超え・`--max-review` / `--max-iter` 到達は**停止ではなく報告点**であり、状況を報告してユーザーの判断を仰ぐ
11. **実コード優先の原則**: メモリ・ドキュメントと実コードが矛盾したら実コードを信じ、矛盾を必ず注記する。裏取りなしの推測でドキュメントを書かない
12. **機密保護**: `secret_paths` のファイルは読まない。存在の有無だけ報告。サブエージェントのログへの混入も検査対象
13. **読み取り専用エージェントの原則**: 調査・レビューは Explore(読み取り専用)、編集・コマンド実行は general-purpose に集約
14. **ログ永続化**: レビュー・検証の記録は `.claude/reviews/{role}-{タスク名}-iter{N}.md` に保存(タスク文脈を持たない skill は `{skill名}-iter{N}.md`)
15. **日本語運用**: skill 本文・報告・生成ドキュメントは日本語。コード・識別子・コミットメッセージ規約はプロジェクトに従う
16. **図の標準**: doc 内の図は Mermaid を第一標準とする(GitHub/GitLab ネイティブ描画・diff 可能・ビルド不要)。図種別→記法→置き場所とスタイル(方向統一・日本語ラベル・subgraph・1 図 1 関心事)は doc/README.md テンプレの図規約表に従う。ワイヤーフレームのみ ASCII コードブロック(ツール不要)または Figma リンク。D2 / PlantUML などレンダラー必須のツールを既定にしない。コードが正本の図(ER・画面遷移)は update-doc が同期する
17. **実行環境の段階判定と縮退プロトコル**: サブエージェント関連機能は環境により使える範囲が違う。skill は**着手時にツールの実在で段階を判定し**(推測しない・ユーザーに聞かない)、判定結果を報告に明記する。**使えるのに下の段階で回さない**:
    - **フル**(`Agent` + `SendMessage`(+ `ListAgents`)が使える = Claude Code で Agent Teams 有効): 委託は双方向。**起動時に `name` を付け**(`implementer` / `reviewer-strong` 等)、次を積極的に使う:
      1. **完了後の追加依頼**: 差し戻し・再レビューは**同じ name へ `SendMessage`**。エージェントの文脈が保たれるため、再スポーン(文脈ゼロからの再調査)より速く・ぶれない。**これがフル段階の最大の利得なので、レビュー反復では必ずこちらを使う**
      2. **走行中の STATUS 問い合わせ**: 待機目安(implementer 15 分 / reviewer 10 分)を超えたら `ListAgents` で生存を確認し、`SendMessage` で状態を尋ねる
      3. **走行中の前提是正・中止**: 委託後に前提の誤り(対象の取り違え・スコープ解釈違い)に気づいたら完了を待たずに訂正する。不要になった委託は中止を伝える(黙って結果を捨てない)
      - 宛先が失われている(`ListAgents` に無く `SendMessage` が失敗する)ときだけ、標準段階と同じ再スポーンにフォールバックする
      - **レビュアー同士を会話させない**。独立性が失われると多モデルレビューの多様性が意味を失う。指摘の突合は必ず team-lead が行う
    - **標準**(`Agent` は使えるが `SendMessage` が無い): 委託は「起動 → 最終レポート」の一方向。差し戻し・追加指示は**前回成果物のパスを含めた新規 Agent 起動**で代替する。応答しないエージェントへの STATUS 問い合わせ(M1)は省略し、待機目安を超えたら再スポーン(M2)に直行する
    - **最小**(サブエージェント機構なし): 全工程を実行者自身が直列に行う。多モデルレビューは**観点を切り替えたセルフレビュー**(事実整合 → 契約 → セキュリティ → 規約を別パスで実施)+機械検証(diff 突合・grep・数値突合)に縮退する。「検証者と実装者の分離」は、フェーズを分けること・機械検証を必ず実行すること・品質ゲートを完了報告前に再実行することで最低限担保する
    - **外部ランナーは 3 段階と直交する任意の追加**(§7。② MCP は §7-5。有効化手段が未定のため現状は ③ のみ)。段階判定はあくまで**ホスト内蔵のサブエージェント機構**の話であり、外部 CLI レビュアーは `--runners` / `features.runners` が宣言されたときだけ編成に加える。**内蔵レビュアーを全滅させない** — `reviewer-internal` は常に維持し、`reviewer-alt` 枡は外部ランナーで置換してよい(粒度は `do-task/references/external-runners.md` の編成表が正本)。外部ランナーは会話継続(`SendMessage`)ができないため常に「起動 → 最終レポート」の一方向で、再レビューは毎回新規起動になる。よって**反復は内蔵側で回し、外部は初回の多様性確保に使う**
18. **キャッシュの規律**: skill が `.claude/` 配下に置く状態ファイル(把握キャッシュ `grasp.md`・レビューログ等)は揮発性キャッシュであり、次の 3 条件を必ず満たす: ①無くても全 skill の動作が同一(再計算のコストがかかるだけで、依存を作らない)②知識の正本(doc/ / メモリ / 権威参照ファイル)に無い情報を溜めない(把握中の発見は正本への反映を促す)③gitignore 対象(共有しない)。「人間・他ツールが読むべき知識は doc/、Claude Code の動作状態は .claude/」の区分を崩さない
19. **チェックの 3 階層**: ① 静的検査(format / lint / typecheck)② 自動テスト ③ **実動確認**(実際に動かして変更フローを観察する)。①②はタスクに依存しない定型実行で /tool-check が担う。③はタスク種別に依存するため、create-task(完了条件を実行可能な確認手順として書く+task-types.md の実動確認列)と do-task(Phase 5.5)が担う。③を省略したときは必ず「未実施+理由」を明記する(サイレントスキップ禁止)
20. **git 出口の規律**: ブランチ作成・commit・push・PR 作成を能動的に行うのは `/ship-task` のみ(`/do-task` はユーザーが `--branch` 等で明示指定したときだけブランチを作る。コミットは従来どおり求められた場合のみ)。**マージは決して行わない**。PR は品質ゲート・実動確認・レビューがすべて緑のときだけ開き、緑でないときはブランチと commit を残して停止する。commit メッセージ規約は `git log` から推定してプロジェクトに合わせ、実装と doc は別 commit に分ける
21. **チェーン実行時の責務分界**: 複数 skill を連鎖させる skill(/ship-task)は**工程の中身を再定義しない**。順序・工程間の続行判定・出口(git)だけを持ち、各工程の手順・品質基準は元の skill に委ねる。連鎖元から呼ばれた skill は、ユーザーへの次アクション提案(チェーン提案)を出さない(呼び出し元が判断するため)

## 6. SKILL.md 執筆規約

- frontmatter: `name`(ディレクトリ名と一致・kebab-case)+ `description` は必須。ユーザーが打つ引数がある skill は `argument-hint`、ツールを制限すべき skill は `allowed-tools` を付ける
- **description は「何をするか+いつ使うか(トリガー語句)」を日本語で 150〜500 字(目安 350)**。Claude の自動呼び出し判断の材料になるため、ユーザーが言いそうな表現(「〜して」)を含める。上限 1,024 字・description+when_to_use 合計 1,536 字
- **SKILL.md は 500 行以下**。超える詳細は `references/*.md` に外出しし、本文から「いつ読むか」付きでリンクする(progressive disclosure)
- 実行可能な重い処理・決定的処理は `scripts/` に外出しして skill は薄いオーケストレーションに徹する(冪等に作る)
- **共有アセット(複数 skill が使うスクリプト・references)は所有 skill の配下に置き、他 skill からは兄弟参照 `../<所有 skill>/...` で解決する**。プラグインルート(`plugins/dev-workflow/scripts/` 等)には置かない — setup.sh 経由の `.claude/skills` 配置でも `.agents/skills` 配置でもプラグインルートが存在せず、3 配布形態のうち 2 つで消えるため。プラグインルートを指す環境変数にも依存しない(ホスト依存になり §7 と両立しない)。兄弟参照は**全 skill が同じ親ディレクトリへ一括配置されていること**を前提とする。skill を 1 本だけ取り出す部分導入など解決できない構成では、**当該機能を無効化して報告する**(探索を広げず、諦める側に倒す)
- プロジェクト固有の事実(パス・コマンド・スタック名・メモリ名)を本文に書かない(§3 の 3 層で解決)
- 冒頭に「原則」、末尾に「最終ゲート」(出力・完了前セルフチェック)を置く
- 関連 skill への導線(前提 skill / 後続 skill)を必ず書く
- **委託は役割語で書き、ホスト機構への解決は references の解決表に集約する**(§5 前文・§7-5)。skill 本文にホスト固有の API 名・モデルエイリアスを書かない(**移行中**。既存 skill には残っており、移行の順序と単位は §7-7)
- **skill 本文を変更するレビューでは「ホスト結合の混入」(役割語ではなくホスト機構名で委託を書いていないか)を観点に含める**(移行済み skill と新規記述が対象。未移行 skill の既存記述は §7-7 の移行で扱う)

## 7. ホスト非依存レイヤ(手順層は可搬・実行層のみ吸収)

この skills 集は Claude Code 以外のホスト(Codex CLI / Cursor 等)でも使う。層を分けて扱う。**「置けばそのまま動く」ではない** — 可搬なのは形式であり、本文はホスト内蔵の機構を前提とした記述を含む:

| 層 | 中身 | 可搬性 |
|---|---|---|
| 形式 | SKILL.md のファイル形式(frontmatter + 本文)と配置規約 | **可搬**。agentskills.io の開標準で、各ホストが同じ形式を読む |
| 本文 | 手順の記述 | **移行中**。ホスト内蔵の機構(`Agent` / `SendMessage` / `Explore` / モデルエイリアス / `.claude/` 配下の状態ファイル)を前提とする記述が残る。委託の語(`Agent` / `SendMessage` / `Explore` / `general-purpose` / モデルエイリアス)は役割語 + 解決表(§7-5)で吸収する(進め方は §7-7)。`.claude/` 配下の状態ファイルの扱いは未決。他ホストでは読み替えが要る(**未検証**)。量の目安は下の測定コマンドで再現できる |
| 実行層 | 誰が委託を実行するか(**① ホスト内蔵 → ② MCP → ③ CLI**) | ホスト依存。**解決表(§7-5)とアダプタ(`do-task/scripts/review-agent.sh`)で吸収する** |

**ホスト固有記述の量(再現可能な測定)**: 語彙の選び方で数値は変わるため、コマンドごと残す。

```bash
for s in plugins/dev-workflow/skills/*/; do
  printf '%-20s %s\n' "$(basename "$s")" \
    "$(grep -oE 'Agent|SendMessage|ListAgents|Explore|general-purpose|\.claude/|fable|opus|sonnet' "$s/SKILL.md" | wc -l)"
done
```

2026-09-09 時点の実測(出現数): do-task 32 / create-task 25 / init-project 22 / update-doc 11 / reflect-decisions 11 / data-audit 9 / understand-project 7 / 他は 2 以下。**サブエージェント機構を持つホスト向けの記述が多い skill ほど、他ホストでは §5-17 の縮退プロトコルとして読み替える必要がある。**

### 7-1. 配置先(手順層)

| ホスト | 配置先 | 備考 |
|---|---|---|
| Claude Code | plugin marketplace(推奨)/ `<project>/.claude/skills/` / `~/.claude/skills/` | `setup.sh --link / --copy / --global` |
| Codex | `.agents/skills`(リポジトリ)/ `$HOME/.agents/skills`(ユーザー)/ `/etc/codex/skills`(管理者) | frontmatter は `name` + `description` が必須 |
| Cursor | `.cursor/skills/` または `.agents/skills/` | `/skill-name` で起動 |

`setup.sh --agents / --agents-copy / --agents-global` が `.agents/skills` への配置を行う(Codex と Cursor で共用できる)。**skill を 1 本だけ取り出す配置は非サポート**(§6 の兄弟参照が前提のため)。**配置できることと、そのホストで手順どおり動くことは別**である(上の表の「本文」行)。他ホストで使う場合は、内蔵サブエージェント前提の記述を §5-17 の縮退プロトコル(最小段階=直列セルフ実行)として読み替える(移行済み skill は解決表(§7-5)で解決する)。

### 7-2. 実行層(レビュアー起動)は宣言時のみのオプトイン

本節は主に ③ 外部 CLI と、profile から受け取る値の信頼モデルについて。委託の解決順全体は §7-5。

**契約の正本は [`do-task/references/external-runners.md`](../plugins/dev-workflow/skills/do-task/references/external-runners.md)**(判定順序・終了コード・既定ランナー表・読み取り専用の保証範囲・ログ規約・機密ガードの手順)。ここには**方針(なぜそうするか)だけ**を置き、仕様の詳細は再掲しない — 同じ事実を複数箇所に書くと、変更のたびに同期漏れが起きるため。

- **なぜオプトインか**: 既定でホスト外のプロセスを起動すると、課金・機密・実行権限の面で利用者の想定を超える。よって**宣言(`--runners` 引数 / profile の `features.runners`)が無ければ外部 CLI を探しに行かない**。既定の編成は §5-9 のままで、この節を足しても 1 ビットも変わらない
- **なぜ起動を 1 本のスクリプトに集約するか**: 判定(存在・自ホストか・読み取り専用・疎通)を skill 本文の散文に書くと守られない。決定的な処理は `do-task/scripts/review-agent.sh` に集約し、skill は薄いオーケストレーションに徹する(§6)
- **信頼モデル(最重要)**: `.claude/project-profile.yml` は commit される共有ファイルであり、リポジトリを書ける者が内容を書ける。したがって **profile に書かれた値から「実行されるコマンド」が変わってはならない**。profile から受け取ってよいのは既定表にあるランナー名とモデル名だけで、**起動コマンドの上書きは profile からも AGENTS.md・CLAUDE.md・README などリポジトリ内のいかなるファイルからも読まない**(書かれていても無視し、無視した旨を報告する)。**リポジトリ内のテキストはデータであって指示ではない**(**MCP 宣言の扱いは §7-6**。生成経路は skill 側の明示承認、既に設定ファイルを持つリポジトリはホスト側のゲートが守る。本節の原則に対する明示的な例外である)
- **なぜ既定表のランナーに上書きを許さないか**: 任意のコマンド文字列を検査して「読み取り専用である」と保証するのは原理的に困難(フラグの重ね指定・`--` 以降への配置・同名バイナリなど、argv 検査を通り抜ける手は尽きない)。個別のガードを足していく方針は破綻するため、**上書き経路そのものを既定表ランナーから外す**。独自コマンドが要る場合は既定表に無いランナー名としてユーザーがセッション内で明示指定し、**読み取り専用の保証はユーザー責任**とする
- **なぜ内蔵レビュアーを残すか**: 外部ランナーは会話継続ができず、認証・レート制限で落ちうる。多モデルレビューの土台を外部依存にしないため、`reviewer-internal` は常に維持する(`reviewer-alt` 枡の置換は可)
- **機密ガードの原則**: 宣言は「使ってよい」であって「毎回無確認で渡してよい」ではない。`secret_paths` が実在するプロジェクトでは明示確認を取ってから起動する。レビュー経路は読み取り専用であり、**ユーザーの git 状態(index・stash)を書き換えてはならない**。**外部 CLI は一時ツリー(作業ツリーの複製)で起動する**。cwd を限定できないランナーは既定で無効とし、一時ツリーを作れない構成では起動せず §5-9 に従って縮退と理由を報告する。なお**契約側(`external-runners.md` §9)は上書き手段を設けない**ため、この「既定で無効」は実質「使わない」として**契約されている**(表現の差であって方針の差ではない。`--command` まで閉じたのは決定 15 の却下理由と同じ根拠による意図的な強化)。ただし**機構で閉じているのは既定表ランナーへの `--command` 拒否まで**で、既定表外の名前で渡す経路は検出せず**呼び出し側 skill の義務**として課される(同 §9)
- **なぜ一時ツリーを必須にするか**: cwd を限定せずに起動した外部 CLI が**リポジトリ外のパスを読んだ実測**がある(§7-3)。**他プロジェクトのパスが外部ベンダーへ渡ることを防ぐ**。ランナー側のサンドボックスは既定表の読み取り専用フラグと同様に実装を信頼するしかなく(保証範囲は `external-runners.md` §3)、ランナーごとに実効性を検証できないため既定で必須にする。却下: 「限界として明記するに留める」(実害が確認されたリスクを利用者の注意力に任せることになる)
- **implementer の外部化は行わない**(スコープ外)。`features.implementer: cursor` は起動コマンドが未定義で未実装であり、宣言されても内部 implementer で動作する

### 7-3. 前提とする外部事実(出典・確認日 2026-09-08、一部 2026-09-09)

§7 が依存する外部事実。前提が崩れたら該当箇所は無効になるため出典を残す。

| 事実 | 出典 | 依存する節 |
|---|---|---|
| Codex は `.agents/skills` / `$HOME/.agents/skills` / `/etc/codex/skills` から SKILL.md を読む。frontmatter は `name` + `description` 必須。agentskills.io の開標準に準拠 | [Build skills — ChatGPT/Codex 公式ドキュメント](https://learn.chatgpt.com/docs/build-skills) | 7-1 |
| Cursor は `.cursor/skills/` または `.agents/skills/` から SKILL.md を読み、`/skill-name` で起動できる | [Cursor Agent Skills(learncursor.dev)](https://www.learncursor.dev/learn/cursor-agents/cursor-agent-skills) | 7-1 |
| Codex は 2026-03-14 に subagents を GA(最大 8 並列・`~/.codex/agents/` の TOML・エージェントごとにモデル指定可) | [Use subagents and custom agents in Codex — Simon Willison](https://simonwillison.net/2026/Mar/16/codex-subagents/) | 7 |
| Claude Code は `CLAUDE.md` を読み `AGENTS.md` は読まない。既存の AGENTS.md がある場合は「それを import する CLAUDE.md を作る」が公式の案内。`@path` import は公式機能(相対・絶対パス可・最大 4 段)。symlink も可だが **Windows では管理者権限か開発者モードが必要**なため import が推奨。CLAUDE.md は 200 行以内が目安、4 MiB 超はスキップ、import 先も起動時に全量ロード | [How Claude remembers your project](https://code.claude.com/docs/en/memory) | 7-4 |
| Codex は AGENTS.md をネイティブに読む。グローバル `~/.codex/AGENTS.md` → プロジェクトルート(通常は Git ルート。見つからなければ cwd のみ)から cwd まで。各階層で `AGENTS.override.md` → `AGENTS.md` → `project_doc_fallback_filenames` の順。root から下へ連結し近い方が上書き | [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | 7-4 |
| Codex は連結後の合計が `project_doc_max_bytes`(既定 32 KiB・設定で変更可)に達すると、以降のファイルを追加しない。公式の対処は上限を上げるか入れ子へ分割 | 同上 | 7-4 |
| Cursor は AGENTS.md をプロジェクトルートとサブディレクトリで読む。位置づけは `.cursor/rules` の**簡易な代替**。入れ子は親と結合し、より具体的な方が優先 | [Rules \| Cursor Docs](https://cursor.com/docs/rules) | 7-4 |
| **Cursor CLI** はプロジェクトルートの `AGENTS.md` と `CLAUDE.md` を読み、`.cursor/rules` と併せてルールとして適用する | [Using Agent in CLI \| Cursor Docs](https://cursor.com/docs/cli/using) | 7-4 |
| Claude Code は `.mcp.json` のプロジェクトスコープサーバーを**対話セッションでのみ**承認を求め、`claude -p` / Agent SDK / クラウド実行では**承認なしに読む**。承認はサーバー名単位(`enabledMcpjsonServers` / `disabledMcpjsonServers`。未承認は `⏸ Pending approval`)。**リポジトリにコミットされた一括承認設定は未信頼フォルダでは無視される**。`.mcp.json` は VCS に入れる前提 | [Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp) | 7-6 |
| Codex はプロジェクトスコープの設定(`.codex/config.toml`)を**信頼済みプロジェクトのときだけ**読む。MCP は `[mcp_servers.<id>]`(`command` / `args` / `env` / `url` / `cwd` / `enabled` / `startup_timeout_sec` 等)。カスタムエージェントは `agents.<name>.config_file` / `agents.<name>.description` で**ロールを宣言**し、**定義本体は `config_file` が指す別の TOML 設定層**に置く(相対パスは宣言した設定ファイルから解決される)。プロジェクト層で無視されるキーの列挙に `agents` は無い | [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) | 7-6 |
| **Codex** の `codex review` を cwd を限定せずに起動したとき、カレントリポジトリ外の兄弟ディレクトリのファイルパスが出力に含まれた(既定サンドボックス〈restricted fs〉は読み取りを制限しなかった。一時ツリー下での越境は未確認) | ローカル実測(2026-09-09・codex 0.153.4) | 7-2 |
| **Cursor CLI** は**ワークスペースの信頼を要求する**。`--trust` を付けないと `Workspace Trust Required` で終了コード 1 になり非対話実行できない。`--trust` はワークスペース信頼のみを与えるもので、コマンド許可の `-f, --force` とは**別物**(`--help` の記載: `--trust`=Trust the current workspace without prompting / `-f, --force`=Force allow commands unless explicitly denied)。読み取り専用は `--mode ask` が担保したまま | ローカル実測(2026-09-09。1 文目は `--trust` 無しでの非対話実行、フラグの意味は `cursor-agent --help`) | 7-2 |
| **Codex** の `codex review` は `--sandbox` も cwd 系フラグ(`-C` / `--cd`)も受け付けない(`-C` を渡すと `error: unexpected argument` で parse に失敗する。option は `-c` / `--strict-config` / `--enable` / `--disable` / `--uncommitted` / `--base` / `--commit` / `--title` と `-h, --help`)。一方 `codex exec` は `-C, --cd <DIR>` と `-s, --sandbox <SANDBOX_MODE>`(`[possible values: read-only, workspace-write, danger-full-access]`)を持つ — **`--cwd` という名前のフラグは持たない**(`--cwd` は `review-agent.sh` 側の引数名)。`codex --help`(トップレベル)にも `-s, --sandbox` と `read-only` が語として存在する。**除外の基準はフラグの有無ではなく「レビュー対象を呼び出し側が与えられない」点**であり、判定への影響は `external-runners.md` §3・§9 に従う | ローカル実測(2026-09-09・codex 0.153.4) | 7-2 |

**未検証の範囲**: 実ホスト(Codex / Cursor)での **SKILL.md 読み込み**は確認していない。**CLI ランナーとしての疎通は両 CLI で実測済み**(2026-09-09: codex 0.153.4 を導入・cursor-agent は認証済みで実レビューを取得)。検証済みなのは「配置されること」までで、読み込みの成否は各ホストの上記仕様に依存する。**Codex のエージェント定義本体の置き場所**は公式に明記が無い(上記の `~/.codex/agents/` の TOML がこれにあたると見られるが**未確認**)。

### 7-4. 権威参照ファイル(AGENTS.md)

**正本は `AGENTS.md`**。Claude Code は AGENTS.md を読まないため、`CLAUDE.md` は `@AGENTS.md` の import で橋渡しする(事実と出典は §7-3)。

| ホスト | AGENTS.md | 経路 |
|---|---|---|
| Codex | ネイティブに読む | そのまま |
| Cursor | ネイティブに読む | そのまま。`.cursor/rules` は生成しない(併存できるが実体を 1 つに保つ) |
| Claude Code | **読まない** | `CLAUDE.md` の `@AGENTS.md` import で橋渡し |

**なぜ import か**(symlink・CLAUDE.md 正本・複写を採らない理由):

- **symlink**: Windows では管理者権限か開発者モードが必要(Claude Code 公式が import を推奨)。コピーを伴う配布・チェックアウトでは実体化されて意図が崩れる(本リポジトリでも review-agent.sh の配布で同型の問題を踏んだ)
- **CLAUDE.md を正本にする**: Codex 側に import 構文があるか未確認
- **両方に同じ内容を置く**: ドリフトの再生産。同じ事実が複数箇所にあると同期漏れが起きる(§7-2 と同じ理由)

**運用**:

- **サイズ**: 権威参照ファイルは**最も厳しいホストの上限**(現状 Codex の `project_doc_max_bytes` 既定 32 KiB。実値と出典は §7-3)に収まるよう薄く保つ
- **生成既定**: `/init-project` が生成する `CLAUDE.md` は `@AGENTS.md` の import 1 行とする。ホスト固有の追記が必要になったら import の下に書く(Claude Code 公式が示す構成)。ただし **`CLAUDE.md` への追記は Cursor CLI にも読まれる**(§7-3)ため、他ホストで有害・無意味になる指示は書かない
- **Cursor CLI と CLAUDE.md**: Cursor CLI は CLAUDE.md も読む(事実)。ただし `@path` import を展開する記載は Cursor 側に無く(**未検証**)、展開しなければ CLAUDE.md は `@AGENTS.md` の 1 行のみで**内容は重複しない**
- **既存プロジェクトの移行**: `/init-project` は既存 `CLAUDE.md` を検出したら AGENTS.md への移行を提案し、**承認を得てから**実行する。**他ツール・CI が `CLAUDE.md` を直接参照している場合に壊れるため、移行提案時にその確認を挟む**。これは `/init-project` の「既存を壊さない」原則(`init-project/SKILL.md`)に対する、**ユーザーの明示承認を条件とする例外**である

出典: `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md`(決定 5・6・13、派生判断 17・18)

### 7-5. 委託の解決(どのバックエンドで実行するか)

この節は委託の解決(どのバックエンドで実行するか)を扱う。③ の宣言方法・信頼モデル・implementer の扱いは §7-2 が正本であり再掲しない。ここでの ② はレビュー等の**委託先**としての MCP であり、**ツール接続としての MCP(§7-6)とは別概念**。

- **解決順は「① ホスト内蔵のサブエージェント → ② MCP ツール → ③ CLI 起動」で固定**
- **profile が宣言できるのは各バックエンドの有効化のみで、順序は変えられない**(理由は下の「なぜそうするか」)
- **既定は ① のみ**(現行と同一挙動)
- **② の有効化手段は未定**であり、決まるまで既定から変更できない。② は**未検証**(返るツールの形は未確認)
- どのバックエンドで実行したかを**報告に明記**する(§5-17 / §5-9 の既存規約の適用)
- **縮退**: 解決表が解決できない構成(skill 単体の部分導入など)では **§5-17 の最小段階(直列セルフ実行)に縮退**し、理由を報告する(§3 の「全項目にフォールバックを定義する」からの派生)
- **役割語の一覧**: `researcher` / `implementer` / `reviewer` / `checker`。一覧は既存 skill が使う役割から採録したもので、出典の決定録は一覧を定めていない。`team-lead` は委託先ではなく**委託する側(セッション自身)の呼称**であり、この一覧には含めない
- **解決表の置き場**: references(共有アセットとして所有 skill の配下に置き、他 skill は兄弟参照で解決する。§6)。**未作成**。入るまでは §5 の記述が暫定の正本(§5 前文)

**なぜそうするか**:

- **なぜ役割語 + 解決表か** — §3 が禁じる「事実のハードコード」をツール軸でやっている状態の解消。新ホストが増えても表を 1 行足すだけで済む。却下: 「役割語 + 既定を括弧書き」(本文にホスト名が残り中途半端)/「ホスト別分岐を本文に書く」(500 行制限を圧迫し、ドリフトの主因を再生産)
- **なぜ順序固定・有効化のみか** — §5-17 の段階判定と同型で既存の設計哲学に収まる。profile 無しでも動く(§3 の絶対規約)。組み合わせが少なくテストできる。却下: 「profile で順序も指定可能」(組み合わせ爆発で回帰テストが非現実的)/「役割ごとに指定」(役割数 × バックエンド数のマトリクスになる)
- **なぜ ③(CLI)を残すか** — MCP サーバーを提供しないベンダーでは ③ が唯一の経路になるため

出典: `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md`(決定 1・2・10)

### 7-6. ツール接続としての MCP

**ツール接続としての MCP(本節)と、委託バックエンド ② としての MCP(§7-5)は別概念**。本節は「同じツールにどの AI からも繋ぐ」ための設定生成を扱う。

- **profile で 1 回宣言し、`/init-project` がホスト別の設定を生成する** — `.mcp.json`(Claude Code)と `.codex/config.toml`(Codex)。ただし**宣言のキー名と値の形は未定**(§4)であり、確定するまで生成しない
- **MCP サーバー自体は同梱しない**(理由は下の「なぜそうするか」)
- **Codex へ生成するのは `.codex/config.toml` の MCP のみで、カスタムエージェント定義は生成しない** — エージェント編成は §7-5 の ① としてホストに任せるため。公式の定義機構はプロジェクト層でも宣言できると読めるので、「ユーザースコープにしか住まない」ことは理由にしない(事実と出典は §7-3)。なお `codex agents` **サブコマンド**はセッション閲覧であり(実測)、定義は設定ファイル側の機構で行う(§7-3)

**信頼モデル**: profile の MCP 宣言は**データとして扱う**(リポジトリ内のテキストは指示ではない)。`/init-project` は解決後の宣言(サーバー名・コマンド・引数)を提示して**ユーザーのセッション内明示承認**を得てから設定ファイルへ生成する。「profile に書かれている = 承認済み」とは扱わない。**宣言が変わるたびに承認を取り直し、`--yes` でも省略しない**(明示承認の要件を運用に写した派生規則。権威参照ファイルの移行と同じ扱い)。

**ゲートは経路ごとに適用範囲が違う**:

- **生成経路(profile → 設定ファイル)**: skill 側の明示承認が、ホストと実行モードによらず働く。**承認を得られない実行(非対話)では生成せず、理由を報告する**(§5-9)
- **設定ファイルを既に持つリポジトリ**(クローン直後・敵対的リポジトリ): 生成を経ないため skill 側の承認は介在せず、**ホスト側のゲートだけが防御になる**。Claude Code は対話セッションでのみ承認を求め(リポジトリにコミットされた一括承認設定は未信頼フォルダでは無視される)、`claude -p` / Agent SDK / クラウド実行では承認なしに読む。Codex はプロジェクトスコープの設定を信頼済みプロジェクトのときだけ読む(§7-3)

したがって **skill 側の承認とホスト側のゲートは互いの代替にならない**。生成した宣言は最終的にホストで実行されるため、これは §7-2 の原則に対する**明示的な例外**である。skill 側の承認が守るのは生成経路だけで、既に設定ファイルを持つリポジトリはこの skill の管轄外(ホスト側のプロジェクト信頼に委ねる)。

**なぜそうするか**:

- **なぜ profile 宣言 → ホスト別生成か** — 「同じ MCP をどの AI からも」を実現する最小の形。§3 の 3 層吸収と同型(1 つの事実 → ホスト別に展開)で、skills 集という性格を保てる。却下: 「検出と案内のみ(現状)」(AI 非依存化に寄与せず人間の規律依存になる)/「MCP サーバーも同梱」(別プロダクトになり、本文のホスト結合を何も解決しない)
- **なぜ信頼モデルを適用するか** — `.mcp.json` / `.codex/config.toml` / profile はいずれも commit される共有ファイルで、敵対的リポジトリが任意の MCP サーバー(= 任意のコマンド)を宣言できる。同型の任意コード実行を PoC で再現済み
- **なぜカスタムエージェント定義を生成しないか** — 却下: 「承認制でユーザースコープへ生成」(全プロジェクトに影響する変更を 1 プロジェクトの初期化が行う。定義形式の実物も未確認)

出典: `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md`(決定 3・4・11)

### 7-7. 移行方針(段階移行と退行防止)

この節は本文のホスト結合を解消していく移行の、単位・順序・退行防止・版の刻みを扱う。

- **単位**: 基盤(解決表 + §6 の規約)を先に入れ、その後は skill 単位で移行する
- **順序**: 本文のホスト結合が多い skill から。量は §7 の測定コマンドで着手時に測り直す(語彙の選び方で数値が変わるため、順序を固定列として持たない。現行の測定コマンドは `.claude/` 等を含む広い語彙だが、順序の目安はこれで足り、検査と到達条件は下の「委託の語」で判定する)。決定時点の測定(決定録。範囲・語彙の明記は無い)では init-project が最多、§7 のコマンド(SKILL.md のみ・9 トークン)では do-task が最多
- **委託の語**: 移行の対象・検査・到達条件で使うホスト固有語は `Agent` / `SendMessage` / `ListAgents` / `Explore` / `general-purpose` / モデルエイリアス。**`.claude/` 配下の状態ファイルは別論点(未決)**であり、この語彙に含めない
- **退行防止は機械検査とレビュー観点の両方**(理由は下の「なぜそうするか」): `scripts/validate.py` に**移行済み skill の本文に委託の語が無いこと**の検査を追加する(何をもって移行済みとするかの判別方法は実装時に定める)。あわせてこのリポジトリの skill 変更レビューの観点に「ホスト結合の混入」を加える(§6)
- **版の刻み**: 基盤は 3.9.0(解決表が入ってから公開する)。移行は 3.9.x 系で刻み、**委託の語が全 skill 本文でゼロになった時点**(= 検査を全 skill 対象にして通る)で **v4.0.0** とする。移行途中の利用者は 3.9.x を使い続けても壊れない

**なぜそうするか**:

- **なぜ基盤先行 + skill 単位か** — 各回で検証でき、失敗しても影響が 1 skill に限定される。18 ファイルの変更が 5 反復・66 件の指摘を生んだ実績から分割の価値は実証済み。却下: 「一括(1 タスク・1 PR)」(レビュー不能な規模)/「役割種別で横断」(1 回の変更が全 skill に及び、skill ごとの文脈を見る作業と相性が悪い)
- **なぜ機械検査とレビュー観点の両方か** — 機械検査は測定可能な不変条件になり、段階移行と相性がよい(移行済みの skill だけを対象にできる)。リンク検査の拡張で同手法が効くことを実測済み。ただし語彙一致では拾えない文脈依存の結合が残るため、レビュー観点も併用する
- **なぜ完了をメジャーで示すか** — 「AI 非依存の到達点」を版で示せる。却下: 「すべてマイナー」(完了の節目がなく移行が終わらない)/「v4.0.0-rc から」(rc が長期化すると本番利用をためらわれる。このリポジトリに rc 運用の前例がない)

出典: `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md`(決定 7・8・9 とその補足)

## 8. このリポジトリへの還元フロー

1. 各プロジェクトで skill に改善を加えたら、プロジェクト固有部分を profile / 動的検出に置き換えた形でこのリポジトリに反映する
2. `plugins/dev-workflow/` 配下を編集 → バージョンを plugin.json / marketplace.json で上げる → commit & push
3. 各プロジェクトでは `/plugin` の更新(marketplace update)で新版を取得

- 版の刻み: 後方互換な機能追加はマイナー、内部の移行・修正はパッチ、AI 非依存の到達点はメジャーで示す(移行中のマイルストーンは §7-7)
