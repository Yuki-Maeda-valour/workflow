---
name: init-project
description: 新規・既存どちらのプロジェクトにも dev-workflow の標準構成(薄型 CLAUDE.md / .claude/project-profile.yml / doc/ 一式 / task/ ・任意で understand-project 強制 hook・specialist agent・MCP セットアップ)を導入する初期化 skill。技術スタック・パッケージマネージャ・リポジトリ構成・MCP 利用可否を自動検出し、「Serena を使うか」(= ドキュメント正本の向き)を起点とした対話で必要な MCP(ブラウザ分析 / デザイン連携)まで確認してから生成する。doc/ は規模に関わらず統一構成(索引 + 01〜05・07 計画〈見積もり・スケジュール。未定のまま設置可〉の番号付き文書)。生成した軸ドキュメントは能力帯の異なる複数モデルの並列レビューで合格まで検証する。「プロジェクトを初期化して」「標準構成を入れて」「ワークフローを導入して」「開発環境をセットアップして」「CLAUDE.md を整備して」等で使う。既存 CLAUDE.md は上書きせず差分提案する。
argument-hint: "[対象パス] [--yes]"
---

# init-project — 標準構成の導入

新しいプロジェクト(または未整備の既存プロジェクト)に dev-workflow のコアサイクルを回すための最小構成を生成する。以降の `/understand-project` → `/create-task` → `/do-task` → `/update-doc` の入口となる。

## 原則

- **本 skill は手順(how)だけを持つ。** 生成する事実(what)は Phase 1 の検出とユーザー確認で解決し、プロジェクト固有の値をハードコードしない。
- **既存を壊さない。** CLAUDE.md は既存があれば上書きせず差分提案。`.claude/settings.json` は既存キーを保ってマージ。既存 doc は上書きしない。
- **構成の設置だけを行う。** 既存プロジェクトのソースコード・既存ドキュメントの中身の改善(リファクタ・修正・書き直し)には踏み込まない。気づいた改善点があっても報告に「後続候補」として列挙するに留め、実施は /understand-project → /stack-research → /create-task(リファクタは --refactor)→ /do-task のサイクルに委ねる。
- **profile が無くても他 skill が動く。** ここで生成する `.claude/project-profile.yml` は各 skill の検出を助ける補助であり、必須ではない。最小限の検出値だけ埋める。
- **テンプレは Read → 置換 → Write。** テンプレートは本スキルの `templates/`(この SKILL.md と同階層)に置いてある。Read し、`{{PLACEHOLDER}}` を検出値に置換してから対象パスへ Write する。値が不明な箇所は「（〜を記述）」等のガイド文を残したまま出力してよい。

## 引数

- 第 1 引数: 対象パス(省略時はカレントディレクトリ)。以降これを `<root>` と呼ぶ。
- `--yes`: Phase 2 の対話を省略し推奨既定で進める(既定: Serena は検出結果に従う〈使用可能なら正本 serena・無ければ正本 docs〉・hook 無・agents 無・MCP は設定せず検出結果の案内のみ)。

---

## Phase 1: 検出(読み取りのみ)

`<root>` を対象に以下を調べ、結果を要約として保持する。

**1. 新規 / 既存 / 再実行の判定**
- ソースファイル数・`package.json` 等のマニフェスト有無・`.git` 有無を確認する。
- マニフェストもソースもほぼ無い → **新規**(テンプレの雛形をそのまま出す)。コードがある → **既存**(検出値でテンプレを埋め、CLAUDE.md は差分提案に切り替える)。
- profile に `workflow_version` が既にある → **再実行(アップデート)モード**: 現在の標準構成テンプレ一式と既存物を突合し、**新標準で増えた・変わった項目だけ**を差分提案する(既存の記述・過去に回答済みの選択は変えない)。完了時に `workflow_version` を現バージョンへ更新する。

**2. 技術スタック・パッケージマネージャ**(design 3 層の「動的検出」)
- PM: `package.json` の `packageManager` フィールド → lockfile(`pnpm-lock.yaml` / `bun.lock*` / `yarn.lock` / `package-lock.json`)→ 既定 `npm`。PHP は `composer.json`、Python は `pyproject.toml` / `requirements.txt`、Go は `go.mod`、Rust は `Cargo.toml`。
- スタック: マニフェストの依存・主要設定ファイルから言語 / フレームワーク / DB / Lint を推定する。
- コード有無: マニフェストもソースも無ければ `has_code: false`(ドキュメントのみ。品質ゲートをスキップ)。

**3. リポジトリ構成**
- `pnpm-workspace.yaml` / `package.json` の `workspaces` → **monorepo**。
- `<root>` 直下に `.git` が無く、子ディレクトリ側に本体の git / マニフェストがある(AI 管理リポジトリ下に本体が独立) → **parent-child**(`root` を本体側に向ける)。
- どちらでもない → **single**。

**4. 品質コマンド**
- `package.json` の `scripts` から `format` / `check` / `lint` / `type-check`|`typecheck` / `test` / `build` を存在検出する。
- 無ければ言語別既定(例: PHP `./vendor/bin/pint --test` + `php artisan test`、Python `ruff check` + `pytest`)。判定できなければ空にし、CLAUDE.md 側にプレースホルダを残す。

**5. 既存構成の有無**
- `CLAUDE.md` / `.claude/project-profile.yml` / `.claude/settings.json` / `doc/`(または `docs/`)/ `task/` / `.serena/` / `.gitignore` / `README.md` の有無を控える。以降の「上書きしない」判断に使う。

**6. MCP の利用可否**
- **Serena**: `mcp__serena__*` ツールが現在使えるか。使えない場合は既存 `.mcp.json` の serena エントリ・`.serena/` の有無も確認する。
- **ブラウザ分析**(API・画面の実測分析用): `mcp__claude-in-chrome__*` または `mcp__chrome-devtools__*` が使えるか。
- **デザイン連携**: Figma 等のデザイン系 MCP ツールが使えるか。

**7. 既存 Serena メモリの列挙(既存プロジェクト移行時)**
- Serena が使用可能で `.serena/memories/` がある場合、`list_memories` で実在メモリを列挙し、汎用カテゴリ(overview / structure / tech / commands / conventions / completion)への対応を名前の意味から推定する(命名揺れの吸収)。Phase 3 の profile 生成で `memory_map` の提案に使う。

検出結果(新規/既存・PM・スタック・構成・品質コマンド・既存物・MCP)を短く提示してから Phase 2 へ進む。

---

## Phase 2: 対話確認

`--yes` が指定されていればこの Phase を飛ばし、推奨既定を採用する。そうでなければ **AskUserQuestion 1 回(最大 4 問)にまとめて**確認する(逐次に分けない。起点の質問+検出・文脈から該当する残りの質問を同時に提示し、検出結果を踏まえて推奨選択肢に印を付ける)。

**起点の質問: Serena を使うか(= ドキュメント正本の向きが決まる)**

- 検出で**使用可能(設定済み)** → 質問せず「使う・正本 = serena」を既定にする(結果提示時にその旨を明示し、変更したければ言ってもらう)。
- **未設定** → 「Serena を使いますか?」を聞く:
  - 使う(`.mcp.json` に設定する) → 正本 = serena。Phase 3 で MCP 設定を生成
  - 使う(設定は自分で行う) → 正本 = serena。設定手順を Phase 4 で案内
  - 使わない → 正本 = docs
- 例外(補足として提示。該当時のみ):
  - Serena は**コード探索専用**にして正本は docs にしたい(人間も読む文書を正本にする) → 正本 = docs のハイブリッド
  - メモリも doc/ も本格運用しない超小規模 → 正本 = claude-md

**残りの質問(該当するものだけ)**

| 決めること | 選択肢 | 既定(推奨) |
|---|---|---|
| understand-project 強制 hook | 導入する / しない | しない(opt-in) |
| specialist agents 生成 | する(検出スタックに応じた雛形) / しない | しない(opt-in) |
| ブラウザ分析 MCP(API・画面の実測分析を使う予定) | 使う / 使わない | 使わない(必要時に後から) |
| デザイン連携 MCP(Figma 等を使うか) | 設定手順を案内 / スキップ | スキップ |

**MCP の分岐ルール**

- **Serena**: 起点の質問に統合済み(設定済みなら質問しない)。
- **ブラウザ分析**: プロジェクトで API・画面分析を使わないならスキップ。使う場合、`claude-in-chrome` が使えるなら追加設定不要(そのまま観測できる)。使えなければ `chrome-devtools` MCP の設定を促す。Claude Code 以外のツールを併用するメンバーにも `chrome-devtools` MCP を案内する(`.mcp.json` はツール非依存で共有できる)。
- **デザイン連携**: Figma 等を使うプロジェクトなら該当 MCP の設定手順を促す。使わなければスキップ。

- doc/ の構成は質問しない(規模に関わらず統一構成で生成する。Phase 3 参照)。
- 正本が `serena` でも `.serena/` が無い場合は「初回に Serena の onboarding が必要」である旨を Phase 4 の案内に含める。
- `has_code: false` のときは起点の質問(正本)のみ尋ね、hook / agents / 品質関連・MCP は既定でスキップする。

---

## Phase 3: 生成

検出値と確認結果で以下を生成する。各テンプレは `templates/` から Read → `{{PLACEHOLDER}}` 置換 → Write。

**1. CLAUDE.md**(`templates/CLAUDE.md.template`)
- 置換: `{{PROJECT_NAME}}`(ディレクトリ名 or 検出名) / `{{OVERVIEW}}`(検出できた概要 or ガイド文) / `{{PM}}` / `{{COMMANDS}}`(検出した品質・開発コマンドを bash 行で) / `{{COMPLETION_CHECK}}`(完了時に回す最小コマンド、例 `` `pnpm check` と `pnpm typecheck` ``) / `{{DOC_REFERENCES}}`。
- `{{DOC_REFERENCES}}` は正本で切り替える。serena → 主要メモリ名の箇条書き(`project_overview` / `code_style_conventions` / `suggested_commands` 等、存在は Phase 4 で onboarding 前提)。docs → 生成した `doc/` ファイルへのリンク箇条書き。
- **既存 CLAUDE.md がある場合は上書きしない。** テンプレの 7 セクションと突き合わせ、不足しているセクションだけを「追記提案」として提示し、ユーザーの承認を得てから追記する。既存の記述は書き換えない。

**2. .claude/project-profile.yml**(`templates/project-profile.yml.template`)
- 置換: `{{PROJECT_NAME}}` / `{{REPO_LAYOUT}}` / `{{ROOT}}`(single・monorepo は `.`、parent-child は本体パス) / `{{HAS_CODE}}` / `{{PACKAGE_MANAGER}}` / `{{SOURCE_OF_TRUTH}}` / `{{QUALITY_BLOCK}}` / `{{WORKFLOW_VERSION}}`(このスキルが属するプラグインの `.claude-plugin/plugin.json` の version を Read して埋める。取得できない導入形態〈コピー導入等〉では管理メタ 2 行を省略) / `{{DATE}}`(今日の日付)。
- Phase 1-7 で既存メモリを列挙した場合、推定した対応表を `memory_map` として有効化した形で提案する(確定は生成内容の提示時にユーザーが確認)。
- `{{QUALITY_BLOCK}}` は検出した品質コマンドを 2 スペースインデントの `key: value` で列挙(例 `  format: pnpm format`)。build はロジック依存が薄いプロジェクトなら `build_optional: true` を添える。検出ゼロなら `{}` にして自動検出へ委ねる旨のコメントを残す。
- 既存の profile があれば上書きせず、差分(検出で埋められる未設定項目)を提案する。

**3. doc/ 一式(規模に関わらず統一構成)**
- `templates/doc/` の 7 テンプレートを `doc/` 直下へ生成する。置換は `{{PROJECT_NAME}}` / `{{DATE}}`(今日の日付) / `{{TECH_STACK}}`(02 のみ、検出スタックの箇条書き):

| ファイル | 内容 |
|---|---|
| `README.md` | 索引・ステータス凡例・分割ルール(全文書の状態が一目で分かる) |
| `01_overview.md` | 目的・背景・スコープ・用語集 |
| `02_architecture.md` | 技術スタック・システム構成・データフロー / 型伝播 |
| `03_requirements.md` | 機能・非機能要件(`[実][決][将][前]` タグ) |
| `04_design.md` | 設計方針・データモデル・コンポーネント設計・ADR |
| `05_operations.md` | 環境構築・環境変数(キー名のみ)・デプロイ・引き継ぎ |
| `07_plan.md` | 見積もり・マイルストーン・スケジュール(計画の正本) |

- **07 は未定のまま設置してよい**: 見積もり・スケジュールが決まっていなくても「（未定）」プレースホルダ付きでファイルを置く(欠番の 06 は /stack-research が生成する)。合意が出るたびに /reflect-decisions が出典付きで埋めていく。
- 小さく始めて**同じ構造のまま育てる**: 1 ファイルが約 300 行を超えたら同番号のディレクトリへ分割する(規則は README.md.template に記載済み。番号体系は変えない)。
- **既存の doc / docs があるファイルは上書きしない**(無いファイルだけ足す)。既存プロジェクトに別構成の doc がある場合は、統一構成への対応表を提示するに留める(移行は提案のみ)。

**4. task/**
- `task/.gitkeep` を作る(タスクファイルは `進行中_{名}.md` → 完了時 `git mv` で `完了_{名}.md`。中断は `中断_`、保留は `保留_`)。

**5. `.claude/settings.json`(permissions 初期セット+ opt: understand-project 強制 hook)**
- **permissions 初期セット(常時)**: Phase 1 で検出した品質コマンド・db 系 scripts に対応する `permissions.allow` エントリを生成する(例: `Bash(pnpm check:*)` `Bash(pnpm test:*)` — **実在する scripts の実行形のみ**。推測でパターンを作らない)。追加する一覧を提示してから書き込む。サイクル(/check・/do-task)実行時の許可プロンプトを減らすのが目的。
- **hook(選択されたときのみ)**: `templates/settings-hooks.json.template` を Read してマージ。
- `.claude/settings.json` が無ければ新規 Write。**既存があれば壊さずマージ**: 既存 JSON を Read し、`permissions.allow` 配列・`hooks.*` 配列に**無いエントリだけ**追記する。他のキーは一切変更しない。マージ結果全体を Write する。

**6.（opt）specialist agents**
- 選択されたときのみ。検出スタックに応じ 1〜3 体、`templates/specialist-agent.md.template` から `.claude/agents/<name>.md` を生成。
- 置換: `{{AGENT_NAME}}`(kebab-case、例 `ui-specialist` / `backend-specialist` / `db-specialist`)/ `{{AGENT_TITLE}}` / `{{AGENT_DESCRIPTION}}`(担当を 1 行で)/ `{{ONE_LINE_ROLE}}`。担当範囲・技術スタック・実装ルールは検出結果で埋め、不明な行はガイド文を残す。

**7.（opt）MCP 設定(`.mcp.json`)**
- 選択されたときのみ。プロジェクトスコープの `.mcp.json` に必要なエントリを追加する。**既存の `.mcp.json` は Read してマージ**する(同名サーバーがあれば追加しない。他のエントリは変更しない)。
- 代表エントリ(コマンド・引数は変わりうるため、**導入時に各公式ドキュメントの最新手順を確認**し、差異があればそちらを優先):
  - Serena(要 `uv`。無ければ生成せず導入手順の案内に切替): `"serena": { "command": "uvx", "args": ["--from", "git+https://github.com/oraios/serena", "serena", "start-mcp-server", "--context", "ide-assistant", "--project", "."] }`
  - chrome-devtools(要 Node): `"chrome-devtools": { "command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"] }`
- Figma 等のデザイン MCP はアプリ側の有効化が必要なため、**`.mcp.json` を生成せず設定手順を案内**する(例: Figma デスクトップアプリで Dev Mode MCP Server を有効化 → 表示されたエンドポイントを `.mcp.json` に登録)。

**8. `.gitignore` の整備と git init**
- `templates/gitignore.snippet` を Read し、**既存 `.gitignore` に無い行だけ**追記する(無ければ新規作成)。対象: `.claude/settings.local.json` / `.claude/reviews/`(skill のレビューログ)/ hook の state ファイル / `.env` 系(`!.env.example` は共有)。
- `.env` 系が**既に git 管理されている**場合は、追記だけでは除外されないため警告し、対応(`git rm --cached` 等)はユーザーに委ねる。
- `.git` が無い場合(新規): タスク運用(`git mv` による `進行中_` → `完了_` リネーム)が git 前提であることを伝え、`git init` を提案する(不要と言われたら task 運用の制約を案内する)。

**9.（新規プロジェクトのみ）プロジェクト README.md**
- ルートに README.md が無い場合のみ、`templates/project-README.md.template` から生成する(doc/ への参照+開発コマンドの薄い雛形。置換: `{{PROJECT_NAME}}` / `{{OVERVIEW}}` / `{{COMMANDS}}`)。既存 README には触れない。

---

## Phase 3.5: 生成物レビュー(合格まで反復)

ここで生成・変更した軸ドキュメント(CLAUDE.md とその差分提案 / profile / doc/ 一式 / settings・mcp のマージ結果 / 初期メモリを整備した場合はそれも)は**以後の開発全体の判断基準になる**ため、多モデル・多角レビューを行い、合格するまで完了しない。

1. **レビュアー編成**: 実行環境で利用可能なモデルから**能力帯の異なる 2〜3 体**を単一メッセージで並列 Agent 起動する(Claude Code の目安: opus + sonnet。profile の `features.review_models` があれば優先。モデルを選べない環境では観点を分けた複数レビュアーで多様性を確保)。全員読み取り専用で起動する。
2. **観点の分担**:
   - **事実整合**: 記載した PM・コマンド・スタック・パスが実プロジェクトと一致するか(マニフェスト・lockfile・実ディレクトリで裏取り)
   - **内部整合**: CLAUDE.md ↔ profile ↔ doc/ の間に矛盾・重複・食い違いがないか
   - **完全性と過不足**: プレースホルダの置換漏れ / 過剰生成 / 既存記述の破壊がないか
   - **規約**: 薄型 CLAUDE.md・機密値なし・タスク命名(`進行中_`/`完了_`)等の統一規約との整合
3. **トリアージ**: 指摘は team-lead が実ファイルで裏取りし、valid のみ修正に反映(盲信しない。false positive は理由を記録)。
4. **合格まで反復**: 全レビュアー PASS(valid 指摘 0)になるまで修正 → 再レビューを続ける。同一指摘が 2 回連続残存・5 ラウンド超過の場合は**勝手に打ち切らず**、状況を報告してユーザーの指示を仰ぐ(未合格のまま完了報告しない)。
5. 記録: `.claude/reviews/init-project-iter{N}.md`。

---

## Phase 4: 検証と案内

**1. 生成物一覧を表として提示**する(パス / 新規作成 or 追記提案 / 概要)。上書きを避けた既存物も「既存のため据え置き」と明示する。

**2. 妥当性の自己確認**
- `.claude/project-profile.yml` が最小構成(name / repo_layout / has_code / source_of_truth)を満たすか。
- CLAUDE.md の `{{...}}` が置換済みか(意図的に残したガイド文以外にプレースホルダが残っていないか)。
- hook をマージした場合、`.claude/settings.json` が有効な JSON か(必要なら再 Read で確認)。
- `.mcp.json` を生成・マージした場合、有効な JSON で既存エントリが保持されているか。

**3. 初期コミットの提案(git リポジトリの場合)**
- レビュー合格済みの生成物一式について、`chore: dev-workflow 標準構成を導入 (v{バージョン})` のようなコミットを**提案**する(実行はユーザー承認後。勝手にコミットしない)。導入時点のスナップショットになり、以後の変更追跡とロールバック地点として機能する。

**4. stack-research のチェーン実行確認(`has_code: true` の場合のみ)**
- 「**続けて /stack-research を実行しますか?**」を AskUserQuestion で確認する(検出した依存バージョンに固有のアンチパターン・ベストプラクティス・セキュリティ注意点を Web 調査して `doc/06_stack-notes.md` に生成する。Web 調査のため数分かかる旨を添える)。
- **Yes → この場で /stack-research をチェーン実行**する(依存検出は stack-research 自身が行うため前提不要)。No → 後からいつでも `/stack-research` で実行できること、依存更新後は `--update` で差分再調査できることを案内する。
- `--yes` 指定時は確認せず案内のみに留める(勝手に数分の Web 調査を開始しない)。

**5. 次のステップを案内**
- まず `/understand-project` を実行してプロジェクト全体像を把握する。
- 以降のサイクル: `/create-task`(タスク設計)→ `/do-task`(実装・検証・レビュー)→ `/update-doc`(ドキュメント同期)。機械検査だけなら `/tool-check`。
- 正本が serena で `.serena/` が未整備なら、Serena の onboarding(プロジェクト有効化 + メモリ作成)を先に済ませ、続けて /update-doc で初期メモリを整備するよう案内する(メモリも多モデルレビューのループで品質担保される)。
- `.mcp.json` を生成・変更した場合、次回セッション起動時に MCP サーバーの承認が求められる旨を伝える。正本 = serena なのに Serena を設定しなかった場合は、把握・同期が浅くなることを明示的に警告する。
- doc / CLAUDE.md の `{{...}}` ガイド文が残る箇所は、`/understand-project` 後に実コードを根拠として埋めるとよい、と伝える。

---

## 最終ゲート(出力前セルフチェック)

- [ ] テンプレは `templates/` から Read し、値をハードコードせず置換して Write したか。
- [ ] 既存の CLAUDE.md / profile / doc / settings.json / .mcp.json / .gitignore / README を上書きせず、差分提案またはマージ(無い行・無いファイルのみ追加)で扱ったか。
- [ ] ソースコード・既存ドキュメントの中身を変更していないか(このスキルの成果物は構成ファイルの設置のみ)。
- [ ] 生成物レビュー(Phase 3.5)を全レビュアー PASS まで実施したか(未合格のまま完了報告していないか)。
- [ ] 再実行モードでは、既存の記述・過去の選択を変えずに新標準の差分だけを提案し、workflow_version を更新したか。
- [ ] MCP は「検出 → 設定済みならスキップ → 必要時のみ質問」の順で扱い、不要な設定を押し付けていないか。
- [ ] 生成した profile は他 skill のフォールバックを壊さない(最小構成が埋まっている)か。
- [ ] `task/.gitkeep` を作り、命名規約(`進行中_` / `完了_`)を CLAUDE.md か案内で伝えたか。
- [ ] 生成物一覧と次ステップ(`/understand-project` からのサイクル)を日本語で提示したか。

## 関連 skill

- 後続: `/understand-project`(初期化直後に必ず実行)→ `/create-task` → `/do-task` → `/update-doc`。
- 補助: 機械検査の単発実行は `/tool-check`。環境構築は `doc/05_operations.md` の手順を基に依頼する。
