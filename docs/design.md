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
/understand-project ──→ /create-task ──→ /do-task ──→ /update-doc --task
 (把握。grasp にキャッシュ、  (種別判定+設計書生成。   (implementer 委託+機械検証   (完了タスク駆動の差分同期。
  毎セッション hook が促し)   --refactor = リファクタ分析)  +実動確認+独立レビュー)     タグ昇格・ADR・図・索引)

品質系(任意の時点): /tool-check = ツールによる機械検査(チェック 3 階層の 1+2。第 3 層の実動確認は do-task)
入力系(任意の時点): /reflect-decisions = 原資料(議事録・文字起こし・チャットログ・資料)から決定事項を
                    抽出 → 精査(裏取り・レビュー・確認)→ doc/(01/03/04/05/07)へ出典付き反映
                    (update-doc が「実コード→doc」なのに対しこちらは「人の決定→doc」。
                     実装が必要な決定・宿題は /create-task へチェーン提案)
出力系(任意の時点): /export-doc = doc/ をクライアント提出用に PDF / xlsx / HTML へ変換
                    (サニタイズ確認+機密検査。doc/ は不変。変換ツールは動的検出で縮退あり)
```

| skill | 責務 | 読む | 書く |
|---|---|---|---|
| init-project | 標準構成(CLAUDE.md / doc/ / task/ / profile)+ MCP セットアップ(opt)の生成 | 対象プロジェクト全体 | CLAUDE.md, doc/, task/, .claude/, .mcp.json |
| understand-project | プロジェクト把握。読み取り専用(キャッシュ除く) | profile, CLAUDE.md, メモリ / doc, コード | .claude/grasp.md(キャッシュのみ) |
| create-task | 種別判定・影響範囲調査済みのタスク設計書生成(--refactor = 対象発見型リファクタ分析) | コード全域 | task/進行中_*.md のみ |
| do-task | タスク実装(委託)〜機械検証・実動確認〜独立レビュー〜完了処理。検証のみモード可 | task MD, コード | ソースコード, task MD |
| update-doc | メモリ / CLAUDE.md / doc/ の実コード同期(--task = 完了タスク駆動。タグ昇格・ADR・索引) | コード全域, 完了タスク MD | メモリ, CLAUDE.md, doc/(索引含む) |
| tool-check | ツールによる機械検査(format/lint/typecheck/test/build)一括実行 | package.json 等 | 自動修正のみ |
| stack-research | 依存の実バージョンに固有の注意点・ベストプラクティス・脆弱性の Web 調査とノート生成 | マニフェスト, lockfile, Web | doc/06_stack-notes.md, doc/README.md |
| reflect-decisions | 議事録・文字起こし・チャットログ等の原資料から決定事項を抽出し、精査(原文裏取り・レビュー・ユーザー確認)を経て doc/ へ出典付きで反映(決定と未決の峻別・矛盾は対比確認)。未決・宿題はタスク化候補として報告 | 原資料, doc/, profile | doc/ 01・03〜05・07(02・06 は対象外), doc/README.md(索引) |
| export-doc | doc/ をクライアント提出用に PDF / xlsx / HTML へ変換。サニタイズ確認(地雷・管理タグ・内部記述)+機密検査+ Mermaid 画像化。ツールは動的検出・縮退あり | doc/, profile | export/(出力のみ。doc/ は不変) |

## 3. 固有情報の 3 層吸収アーキテクチャ(最重要原則)

**skill 本文は手順(how)だけを持ち、プロジェクト固有の事実(what)を一切ハードコードしない。** 事実は以下の 3 層で実行時に解決する。全 skill がこの順で解決すること。

| 層 | 手段 | 吸収する情報 |
|---|---|---|
| 1. プロファイル | `.claude/project-profile.yml` を Read(存在すれば) | リポジトリ構成 / 品質コマンド / area 定義 / メモリ名マップ / 正本の向き / 機能フラグ |
| 2. 動的検出 | Glob / ls / list_memories を実行時に叩く | パッケージマネージャ / 技術スタック / ディレクトリ構造 / 実在メモリ名 |
| 3. 権威参照 | CLAUDE.md(と doc/)を Read | プロジェクト固有原則 / 禁止事項 / 地雷 |

**profile が無くても必ず動く**こと(全項目にフォールバックを定義する)。これが汎用性の担保。

### 動的検出の標準ロジック

- パッケージマネージャ: package.json の `packageManager` フィールド → lockfile(pnpm-lock.yaml / bun.lock* / yarn.lock / package-lock.json)→ 既定 npm。PHP は composer.json、Python は pyproject.toml / requirements.txt、Go は go.mod、Rust は Cargo.toml
- 品質コマンド: profile の `quality` → package.json の scripts から `format` / `check` / `lint` / `type-check`|`typecheck` / `test` / `build` を存在検出 → 言語別既定(PHP: `./vendor/bin/pint --test` + `php artisan test`、Python: ruff/pytest 等)
- モノレポ: pnpm-workspace.yaml / package.json#workspaces から自動列挙
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
#   serena:    Serena メモリが正本、CLAUDE.md は薄型維持
#   docs:      doc/(02・05 等)が正本、メモリは探索用の要約(一方向同期)
#   claude-md: CLAUDE.md 自体が正本(小規模・メモリ未使用)
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

# 実行機能レベル(タスク系 skills の重さを調整)
features:
  impact_analysis: true      # create-task Phase 1.5(影響範囲調査)
  external_review: true      # create-task Phase 4.5(Opus+Sonnet 外部レビュー)
  reviewer_count: 1          # do-task のレビュアー数: 1 | 3
  review_models: [opus, sonnet]  # レビューに使うモデル群(省略時: 環境で利用可能なモデルから上位+標準を自動選定)
  implementer: internal      # internal | cursor(外部 CLI 委託)
  implementer_model: sonnet  # implementer サブエージェントのモデル(エイリアスのみ)

# ローカル環境の構築コマンド(環境構築を依頼されたとき doc/05 と併せて参照される)
setup_commands: []           # 例: ["docker compose up -d db", "pnpm db:migrate", "pnpm db:seed"]

# 地雷リストの場所の上書き(任意)。既定は doc/05_operations.md の「引き継ぎ・地雷」節
# (全ツール・人間から見える doc/ が正本。.claude/ 配下は Claude 専用になるため置かない)
known_facts_ref: docs/HANDOVER.md
```

## 5. 全 skill 共通の統一規約

調査で検出した不統一・矛盾を以下に統一する。**全 skill がこれに従う。**

1. **タスクファイル命名**: `task/進行中_{タスク名}.md` → 完了時に `git mv` で `task/完了_{タスク名}.md`。中断は `中断_`、保留は `保留_`
2. **skill 名**: `update-doc`(update-docs は使わない)。旧 refact / refactor は `create-task --refactor` に統合(分析 → タスク化 → do-task で実行)
3. **サブエージェント API**: `Agent` + `SendMessage` のみ。TeamCreate / TeamDelete は使わない(廃止方針・可搬性)。SendMessage は実験的機能(Agent Teams)由来のため利用可能を前提にせず、§5-17 の縮退プロトコルに従う
4. **モデル指定**: エイリアスのみ(`opus` / `sonnet` / `inherit`)。日付付きモデル ID・版数(「Opus 4.7」等)をハードコードしない
5. **`claude -p` / `claude --print` を Bash から起動しない**(別課金)。サブエージェントは必ず Agent ツール経由
6. **検証の主体**: スコープ縮小を検出する側(team-lead)は実装者と分離する。**規模に関わらず実装は implementer に委託し、team-lead は常に検証者に回る**(直接実装は §5-17 の環境縮退時のみ)。完了条件のコマンドは team-lead が自分で再実行する(implementer の自己申告を最終確認にしない)
7. **スコープ縮小検出 grep**(do-task の検証で使用): `段階的に実施|後続タスク|今回はスコープ外|のみ作成|次回対応|一旦`
8. **チェックリスト突合の機械化**: `grep -cE '^\s*- \[(x|X)\]'` で件数突合+diff との整合確認
9. **レビューの扱い**: 多モデルレビュー — **実行環境で利用可能なモデルから能力帯の異なる複数(上位+標準/軽量)を選び**、単一メッセージで並列スポーンする。Claude Code での現時点の目安は opus(上位)+ sonnet(標準)。profile の `features.review_models` があればそれを優先する。モデルを選べない環境では、観点(事実整合 / セキュリティ / 規約)を分けた複数レビュアーで多様性を確保する。**規模適応**: 変更対象が少数(目安 3 件以下)で矛盾の無い軽微な実行では、観点を分けた単一レビュアー(またはセルフレビュー)へ軽量化してよい(採用した編成を報告に明記する)。→ team-lead が各指摘を実コードで裏取りし valid / invalid / needs-user にトリアージ。指摘を盲信して自動反映しない。false positive は理由を記録
10. **レビューループのセーフティ**: 同一指摘が 2 回連続残存 → ユーザー確認。5 ラウンド超え → トークンコスト警告。`--max-review` / `--max-iter` は安全弁
11. **実コード優先の原則**: メモリ・ドキュメントと実コードが矛盾したら実コードを信じ、矛盾を必ず注記する。裏取りなしの推測でドキュメントを書かない
12. **機密保護**: `secret_paths` のファイルは読まない。存在の有無だけ報告。サブエージェントのログへの混入も検査対象
13. **読み取り専用エージェントの原則**: 調査・レビューは Explore(読み取り専用)、編集・コマンド実行は general-purpose に集約
14. **ログ永続化**: レビュー・検証の記録は `.claude/reviews/{role}-{タスク名}-iter{N}.md` に保存(タスク文脈を持たない skill は `{skill名}-iter{N}.md`)
15. **日本語運用**: skill 本文・報告・生成ドキュメントは日本語。コード・識別子・コミットメッセージ規約はプロジェクトに従う
16. **図の標準**: doc 内の図は Mermaid を第一標準とする(GitHub/GitLab ネイティブ描画・diff 可能・ビルド不要)。図種別→記法→置き場所とスタイル(方向統一・日本語ラベル・subgraph・1 図 1 関心事)は doc/README.md テンプレの図規約表に従う。ワイヤーフレームのみ ASCII コードブロック(ツール不要)または Figma リンク。D2 / PlantUML などレンダラー必須のツールを既定にしない。コードが正本の図(ER・画面遷移)は update-doc が同期する
17. **実行環境の縮退プロトコル**: サブエージェント関連機能は環境により使えないことがある。skill は利用可能なツールを確認し、次の 3 段階で自動縮退する(どの段階で実行したかを報告に明記する):
    - **フル**(Agent + SendMessage 利用可): 現行どおり。走行中エージェントへの STATUS 問い合わせ・差し戻しの追加指示が可能
    - **標準**(Agent のみ / SendMessage 不可): 委託は「起動 → 最終レポート」の一方向。差し戻し・追加指示は**前回成果物のパスを含めた新規 Agent 起動**で代替する。応答しないエージェントへの STATUS 問い合わせ(M1)は省略し、待機目安を超えたら再スポーン(M2)に直行する
    - **最小**(サブエージェント機構なし): 全工程を実行者自身が直列に行う。多モデルレビューは**観点を切り替えたセルフレビュー**(事実整合 → 契約 → セキュリティ → 規約を別パスで実施)+機械検証(diff 突合・grep・数値突合)に縮退する。「検証者と実装者の分離」は、フェーズを分けること・機械検証を必ず実行すること・品質ゲートを完了報告前に再実行することで最低限担保する
18. **キャッシュの規律**: skill が `.claude/` 配下に置く状態ファイル(把握キャッシュ `grasp.md`・レビューログ等)は揮発性キャッシュであり、次の 3 条件を必ず満たす: ①無くても全 skill の動作が同一(再計算のコストがかかるだけで、依存を作らない)②知識の正本(doc/ / メモリ / CLAUDE.md)に無い情報を溜めない(把握中の発見は正本への反映を促す)③gitignore 対象(共有しない)。「人間・他ツールが読むべき知識は doc/、Claude Code の動作状態は .claude/」の区分を崩さない
19. **チェックの 3 階層**: ① 静的検査(format / lint / typecheck)② 自動テスト ③ **実動確認**(実際に動かして変更フローを観察する)。①②はタスクに依存しない定型実行で /tool-check が担う。③はタスク種別に依存するため、create-task(完了条件を実行可能な確認手順として書く+task-types.md の実動確認列)と do-task(Phase 5.5)が担う。③を省略したときは必ず「未実施+理由」を明記する(サイレントスキップ禁止)

## 6. SKILL.md 執筆規約

- frontmatter: `name`(ディレクトリ名と一致・kebab-case)+ `description` は必須。ユーザーが打つ引数がある skill は `argument-hint`、ツールを制限すべき skill は `allowed-tools` を付ける
- **description は「何をするか+いつ使うか(トリガー語句)」を日本語で 150〜500 字(目安 350)**。Claude の自動呼び出し判断の材料になるため、ユーザーが言いそうな表現(「〜して」)を含める。上限 1,024 字・description+when_to_use 合計 1,536 字
- **SKILL.md は 500 行以下**。超える詳細は `references/*.md` に外出しし、本文から「いつ読むか」付きでリンクする(progressive disclosure)
- 実行可能な重い処理・決定的処理は `scripts/` に外出しして skill は薄いオーケストレーションに徹する(冪等に作る)
- プロジェクト固有の事実(パス・コマンド・スタック名・メモリ名)を本文に書かない(§3 の 3 層で解決)
- 冒頭に「原則」、末尾に「最終ゲート」(出力・完了前セルフチェック)を置く
- 関連 skill への導線(前提 skill / 後続 skill)を必ず書く

## 7. このリポジトリへの還元フロー

1. 各プロジェクトで skill に改善を加えたら、プロジェクト固有部分を profile / 動的検出に置き換えた形でこのリポジトリに反映する
2. `plugins/dev-workflow/` 配下を編集 → バージョンを plugin.json / marketplace.json で上げる → commit & push
3. 各プロジェクトでは `/plugin` の更新(marketplace update)で新版を取得
