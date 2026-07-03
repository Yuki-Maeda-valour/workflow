# workflow — 汎用開発ワークフロー skills 集

複数の実プロジェクトで実証されたベスト要素を統合した、**どのプロジェクトでも使える** Claude Code skills 集。
新しいプロジェクトを始めるとき、ここから skills を導入すれば、タスク駆動開発のワークフロー一式がすぐ使える。

- プロジェクト固有情報(パス・コマンド・スタック)は skill にハードコードされていない。`.claude/project-profile.yml` +実行時自動検出+ CLAUDE.md の 3 層で吸収する(profile が無くても動く)
- 設計の詳細・規約・出典は [docs/design.md](docs/design.md)

## skills 一覧(10 種)

| skill | 用途 | 呼び出し例 |
|---|---|---|
| `/init-project` | 新規/既存プロジェクトに標準構成(CLAUDE.md / profile / doc / task / gitignore / permissions / MCP)を導入。完了時に stack-research をチェーン提案 | プロジェクト開始時に一度 |
| `/understand-project` | プロジェクト把握(読み取り専用)。`--quick / --area / --deep`。結果は grasp キャッシュに保存され、変更が無ければ次回は即答 | セッション開始時(hook が自動促し) |
| `/stack-research` | 依存バージョン固有のアンチパターン・ベストプラクティス・脆弱性を Web 調査し doc/06 に出典付き生成。プロジェクトに実在する問題はタスク化をチェーン提案 | init 直後・依存更新後(`--update`) |
| `/create-task` | 種別判定(9 種)・影響範囲調査・図解付きのタスク設計書を `task/進行中_*.md` に生成。`--refactor` で対象発見型のリファクタ分析 | 「〜をタスク化して」「リファクタして」 |
| `/do-task` | タスク設計書を実装(implementer 委託)・機械検証・実動確認・独立レビュー・完了処理。中断再開可。「検証だけ」も可 | 「タスクをやって」「続きをやって」 |
| `/update-doc` | メモリ / CLAUDE.md / doc を実コードと同期。`--task` で完了タスク駆動の差分同期(要件タグ昇格・ADR・図・索引まで) | タスク完了後の締め |
| `/discuss-spec` | テーマ単位の壁打ちで仕様を対話決定(論点分解 → 選択肢・推奨 → 合意)。決定録を生成して /reflect-decisions へチェーン | 「壁打ちしたい」「仕様を相談して決めたい」 |
| `/reflect-decisions` | 議事録・文字起こし・チャットログ等から決定事項を抽出し、精査(裏取り・レビュー・確認)を経て要件定義(doc/03)・ADR(doc/04)等へ出典付きで反映。未決・宿題は /create-task へチェーン提案 | 「議事録を反映して」「決まったことを反映して」 |
| `/export-doc` | doc をクライアント提出用に PDF / xlsx / HTML へ変換。内部情報のサニタイズ確認・機密検査・Mermaid 図の画像化付き。doc 自体は変更しない | 「PDF にして」「エクセルで出して」 |
| `/tool-check` | ツールによる機械検査(format/lint/typecheck/test/build)一括実行 | コミット前 |

推奨サイクル: `/init-project`(初回。→ stack-research へチェーン)→ `/understand-project`(毎セッション hook が促し)→ `/create-task` → `/do-task` → `/update-doc --task`

## 導入方法

### A. plugin marketplace(推奨)

バージョン管理・更新配布・名前空間(`dev-workflow:skill名`)が付く公式の共有方式。

```
# Claude Code のセッション内で(どのプロジェクトからでも一度だけ)
/plugin marketplace add ~/dev/workflow                   # ローカルパス(clone 先に合わせる)
#   または
/plugin marketplace add Yuki-Maeda-valour/workflow        # GitHub 経由

# プラグインをインストール(ユーザー全体で有効化できる)
/plugin install dev-workflow@valour-workflow
```

更新の取り込み: このリポジトリを更新(commit)した後、`/plugin marketplace update valour-workflow`(GitHub 経由で使っている場合は push も必要)。

### B. setup.sh(プロジェクト単位のコピー / symlink)

プラグインを使わず、対象プロジェクトの `.claude/skills/` に直接置く方式。

```bash
cd ~/dev/workflow
./setup.sh --link ~/dev/新プロジェクト    # symlink(この repo の更新が即反映)
./setup.sh --copy ~/dev/新プロジェクト    # コピー(プロジェクト側で独自改変する場合)
./setup.sh --global                      # ~/.claude/skills に symlink(全プロジェクト共通)
```

### C. 新プロジェクトの立ち上げ(導入後)

```
/init-project            # 標準構成一式を生成(対話で MCP・hook まで。完了時に stack-research をチェーン提案)
/understand-project      # 把握(以後は hook が毎セッション自動で促す)
```

## 既存プロジェクトとの共存・移行

- 既存プロジェクトの同名 skill(`.claude/skills/` 配下)はプロジェクト版が優先される。プラグイン版は `dev-workflow:名前` の名前空間で常に呼べる
- 旧来の独自 skills から移行する場合は旧 skill を削除し、プロジェクト固有の内容(パス・コマンド・地雷)は `.claude/project-profile.yml` と doc/05 の「引き継ぎ・地雷」へ移す。書き方は [docs/design.md §4](docs/design.md)

## このリポジトリへの還元

各プロジェクトで skill を改善したら、固有部分を profile / 動的検出に置き換えて `plugins/dev-workflow/` に反映 → バージョンを上げて commit → 各プロジェクトで更新を取得。詳細は [docs/design.md §7](docs/design.md)。

## 検証

```bash
python3 scripts/validate.py       # frontmatter / 規約 / JSON の一括検証
claude plugin validate .          # Claude Code 本体による検証
```
