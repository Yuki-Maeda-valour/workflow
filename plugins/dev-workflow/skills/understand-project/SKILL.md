---
name: understand-project
description: 作業開始前にプロジェクトの全体像(目的・構造・技術スタック・規約・開発コマンド)を把握する読み取り専用スキル。「プロジェクトを把握して」「プロジェクト理解して」「全体像を教えて」「このプロジェクトは何?」と言われたとき、新しいセッションで最初の実作業に入る前、または他のスキル(create-task / do-task)の前提として使う。Serena メモリ・CLAUDE.md・実コードの 3 系統を突き合わせ、矛盾があれば実コードを優先して注記する。--quick(メモリのみ 30 秒)、--area=<領域>(特定領域を深掘り)、--deep(設定・規約まで全確認)を選択可能。結果は .claude/grasp.md にキャッシュされ、コードに変更が無ければ次回は再調査せず即答する。
argument-hint: "[--quick | --area=<name> | --deep]"
disallowed-tools: Edit, NotebookEdit
---

# understand-project — プロジェクト把握(読み取り専用)

## 原則

1. **読み取り専用(キャッシュを除く)**。コード・ドキュメント・設定を一切変更しない。唯一の書き込みは把握キャッシュ `.claude/grasp.md` のみ(design §5-18 の規律に従う)
2. **実コード優先**。メモリやドキュメントと実コード・設定が矛盾したら、実コードを事実として採用し、矛盾を必ず出力に注記する(ドキュメントの修正は /update-doc の責務)
3. **機密保護**。`.env` `.env.*` `.dev.vars` など機密ファイル(profile の `secret_paths`)は開かない。存在の有無だけ報告し、値は絶対に出力しない
4. **動的解決**。ディレクトリ名・メモリ名・コマンドを推測や記憶で決めつけず、必ず実行時に列挙・検出する
5. **時間対効果と段階的深化**。セッション初手は --quick 相当で十分(全量調査は --deep が明示されたときだけ)。精度が必要な後続 skill(/create-task 等)が、その時点でタスクの対象領域に絞って自動的に深掘りする

## オプション

| オプション | 内容 | 目安 |
|---|---|---|
| (なし) | 標準把握: プロファイル+メモリ+構造の実測+サマリー | 1〜3 分 |
| `--quick` | プロファイル+知識の正本(Serena メモリ / 無ければ doc/ の索引と主要文書 / それも無ければ CLAUDE.md)のみ。構造実測なし | 30 秒 |
| `--area=<name>` | 指定領域に絞って深掘り(定義は profile の `areas`、無ければ backend / frontend / database) | 1〜2 分 |
| `--deep` | 標準+設定ファイル精読+ドキュメントドリフト検出 | 5〜10 分 |

## キャッシュ(.claude/grasp.md)

- **実行冒頭で確認**: `.claude/grasp.md` があり、記録された HEAD と `git rev-parse HEAD` が一致し、記録の把握レベルが今回の要求以上なら、**再調査せずキャッシュのサマリーを提示して終了**する。working tree が dirty の場合は `git status --short` の変更ファイルを注記に添え、**変更に lockfile が含まれるときは stack-research 鮮度チェックだけ追加実行**する。HEAD 不一致・レベル不足・git 無しなら通常フローへ進み、末尾でキャッシュを更新する
- **把握レベルの順序**: `quick < 標準 < deep`。`area:{X}` は「標準+X 領域の深掘り」として扱い、**同一領域の要求のみ再利用可**。別領域の要求には標準部分を再利用し、不足領域だけ追加深掘りして grasp に**追記累積**する(レベル表記は `標準+area:X,Y` の形式。/create-task の段階的深化による更新も同じ形式で追記する)
- **実行末尾で更新**: Phase 5 のサマリー+メタ(把握レベル / 日時 / HEAD / 未確認事項)を `.claude/grasp.md` に Write する(gitignore 対象)
- **キャッシュの規律**(design §5-18): grasp.md が無くても動作は同一(再把握するだけ)。知識の正本(doc/ / メモリ / CLAUDE.md)に無い情報をここに溜めない — 把握中の発見(ドリフト・地雷)は /update-doc での正本反映を促す

## Phase 0: コンテキスト源の解決

固有情報は次の 3 層で解決する。上から順に読み、後の層は前の層を補完する。

### 0-1. プロジェクトプロファイル(あれば)

`.claude/project-profile.yml` を Read。存在すれば以下を得る:
- `repo_layout` / `root`: **parent-child 構成(AI 管理リポジトリの下に本体ソースが独立 git)の場合、以降の探索・コマンド実行はすべて `root` 配下を対象にする**
- `has_code`: false ならコード分析をスキップし、ドキュメント構成の把握に切り替える
- `memory_map` / `areas` / `quality` / `source_of_truth` / `known_facts_ref`

地雷リスト(`known_facts_ref` が指すファイル、未指定なら `doc/05_operations.md` の「引き継ぎ・地雷」節)があれば必ず読み、サマリーの「⚠️ 絶対に忘れない事項」に転記する。`doc/06_stack-notes.md`(または docs/ 配下。/stack-research が生成)があれば読み、バージョン固有の注意点も同じく「⚠️」へ反映する。

### 0-2. CLAUDE.md(必ず)

プロジェクトルートの CLAUDE.md を Read。基本指示・Quick Commands・タスク完了条件・プロジェクト固有の禁止事項を把握する。モノレポでサブディレクトリに CLAUDE.md がある場合(`Glob: **/CLAUDE.md`、node_modules 除外)、作業対象に近いものも読む。

### 0-3. 知識の正本(Serena メモリ または doc/)

**Serena MCP ツール(`mcp__serena__*`)が利用可能な場合**:
1. `get_current_config` でアクティブプロジェクトを確認。**このプロジェクトと違う場合は `activate_project` で切り替える**(別プロジェクトのメモリ誤参照防止)。`check_onboarding_performed` が未実施ならその旨をサマリーに記載し、onboarding は提案に留める
2. `list_memories` で実在メモリを動的列挙(固定名・固定数を仮定しない)
3. profile の `memory_map` があればそれに従い、無ければメモリ名から意味的に「概要 / 構造 / 技術 / コマンド / 規約 / 完了条件」に対応するものを選んで `read_memory`。--quick ではこのカテゴリのみ、標準では関連するもの全部を読む

**Serena を使わない場合(正本 = docs / claude-md、または MCP 不在)**: 同じカテゴリを `doc/`(または `docs/`)から得る:

| カテゴリ | 読む文書 |
|---|---|
| 全体像・各文書の状態 | `README.md`(索引) |
| 概要・スコープ・用語 | `01_overview.md` |
| 構造・技術 | `02_architecture.md` |
| 要件・設計 | `03_requirements.md` / `04_design.md`(標準以上のみ) |
| 計画・期限(見積もり・マイルストーン) | `07_plan.md`(標準以上のみ・あれば) |
| コマンド・完了条件・地雷 | `05_operations.md`(+ CLAUDE.md の Quick Commands) |

--quick では索引+ 01 + 05 に絞る。doc/ も無ければ 0-2 の CLAUDE.md と Phase 1 の実測だけで把握する(このスキルは Serena 必須ではない)。

## Phase 1: 技術スタック検出(--quick 以外)

マニフェストを Glob で横断検出して読む(存在するものだけ):

| マニフェスト | 判明すること |
|---|---|
| package.json | Node 系スタック・scripts・`packageManager` |
| pnpm-workspace.yaml / package.json#workspaces | モノレポ構成とパッケージ一覧 |
| pyproject.toml / requirements.txt | Python 系 |
| go.mod / Cargo.toml / composer.json / Gemfile / pom.xml / build.gradle | Go / Rust / PHP / Ruby / Java |

- パッケージマネージャ判定: `packageManager` フィールド → lockfile(pnpm-lock.yaml / bun.lock* / yarn.lock / package-lock.json)→ 既定 npm
- DB/ORM 推定: prisma/・drizzle.config.*・schema.zmodel・supabase/・migrations/ の存在で判定
- 品質コマンド解決: profile の `quality` → package.json scripts(format / check / lint / type-check|typecheck / test / build)→ 言語別既定
- **stack-research の鮮度チェック**: `doc/06_stack-notes.md` があれば、記録された lockfile ハッシュと現在(`git hash-object <lockfile>`)を比較し、不一致ならサマリーに「⚠️ 依存が変わっています → /stack-research --update を推奨」を含める

## Phase 2: ディレクトリ構造の実測(--quick 以外)

`ls` / Glob でルート直下と主要ディレクトリ(src/ app/ 等、深さ 2 まで)を実測する。固定のディレクトリ名を仮定せず、実在するものだけをサマリーに載せる。モノレポは workspace 定義から各パッケージの役割を 1 行ずつ。

`--area=<name>` のときは該当領域のパス配下のみ、ファイル一覧+主要ファイルの冒頭(エントリポイント・ルーティング・スキーマ)まで読む。モノレポでは workspace パッケージ名をそのまま area 名として指定できる(profile の `areas` 定義があればそちらを優先)。

## Phase 3: 規約・設定の精読(--deep のみ)

biome.json / .prettierrc* / eslint.config.* / tsconfig.json / ruff.toml / .editorconfig 等を存在検出して読み、インデント・クォート・strict 設定などの規約を抽出する。doc/ または docs/ があれば構成(ファイル名一覧と各役割)を把握する。

## Phase 4: ドキュメントドリフト検出(--deep のみ)

メモリ・CLAUDE.md の記述と実測の間の乖離を 4 観点でチェックする:

| 観点 | 方法 |
|---|---|
| 技術スタックの乖離 | メモリ記載のライブラリ/バージョン vs manifest 実物 |
| コマンドの乖離 | 記載コマンド vs scripts 実在 |
| 構造の乖離 | 記載ディレクトリ vs 実在 |
| 参照切れ | 記載ファイルパスの存在確認 |

乖離はサマリーの「🔺 ドリフト検出」に列挙し、/update-doc の実行を提案する(このスキルでは直さない)。

## Phase 5: サマリー出力

以下のテンプレートで報告する(検出できなかった節は省略):

```markdown
# プロジェクト把握: {プロジェクト名}

> 把握レベル: {quick | 標準 | area:{名} | deep} / 実行: {日時} / HEAD: {ハッシュ先頭 12 桁}
> 未確認事項: {このレベルで確認していないこと。例: quick → 構造実測・規約・ドリフト未確認}

## 🎯 目的
{1〜3 行}

## 🏗️ アーキテクチャ / 🛠️ 技術スタック
{スタック、DB/ORM、主要ライブラリ。出典(メモリ or 実測)を併記}

## 📁 構造
{実測ディレクトリと役割。parent-child なら本体ルートを明示}

## 📋 規約
{命名・フォーマッタ設定・言語ポリシーなど}

## ⚡ 開発コマンド
{dev / format / lint / typecheck / test / build。PM 込みの実行形}

## ✅ タスク完了条件
{CLAUDE.md・メモリ・profile から解決した品質ゲート}

## ⚠️ 絶対に忘れない事項
{known_facts / CLAUDE.md の禁止事項・地雷}

## ❗ 実コードとの矛盾(あれば)
{「メモリでは X だが実コードは Y。実コードを優先」形式}

## 🔺 ドリフト検出(--deep 時)
{Phase 4 の結果。/update-doc を提案}

## 次のアクション
{推奨: /create-task(新規タスク)、/update-doc(ドリフトあり)、Serena onboarding(未実施)}
```

`--area` のときは該当領域の詳細(主要ファイル・データフロー・関連メモリ)に置き換える。

**分量規律**: --quick は各節 1〜3 行・全体 1 画面以内に圧縮する(書ききれない詳細は「未確認事項」に回す)。標準以上でも網羅より「次のアクションの判断に必要な密度」を優先する。

## 最終ゲート(出力前セルフチェック)

- [ ] コード・ドキュメント・設定を変更していない(書き込みは `.claude/grasp.md` のみ)
- [ ] grasp.md に正本(doc/ / メモリ / CLAUDE.md)へ無い情報を溜めていない(発見は /update-doc を促した)
- [ ] 機密ファイルの値を出力していない
- [ ] メモリ・ドキュメント由来の情報と実測由来の情報を区別できている
- [ ] 矛盾を見つけた場合、実コード優先で注記した
- [ ] parent-child / モノレポの場合、対象ルートを明示した

## 関連スキル

- 把握した内容でタスクを設計する → `/create-task`
- ドリフトを検出した → `/update-doc`
- 環境がまだ動かない → `doc/05_operations.md` の手順(と profile の `setup_commands`)を基に環境構築を依頼
- 標準構成(profile / CLAUDE.md)自体が無い → `/init-project`
