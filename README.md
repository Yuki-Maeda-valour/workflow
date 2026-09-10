# workflow — 汎用開発ワークフロー skills 集

複数の実プロジェクトで実証されたベスト要素を統合した、**どのプロジェクトでも使える** Claude Code skills 集。
新しいプロジェクトを始めるとき、ここから skills を導入すれば、タスク駆動開発のワークフロー一式がすぐ使える。

- プロジェクト固有情報(パス・コマンド・スタック)は skill にハードコードされていない。`.claude/project-profile.yml` +実行時自動検出+ CLAUDE.md の 3 層で吸収する(profile が無くても動く)
- 設計の詳細・規約・出典は [docs/design.md](docs/design.md)

## skills 一覧(12 種)

| skill | 用途 | 呼び出し例 |
|---|---|---|
| `/init-project` | 新規/既存プロジェクトに標準構成(権威参照ファイル(AGENTS.md)+ CLAUDE.md の import / profile / doc / task / gitignore / permissions / MCP)を導入。完了時に stack-research をチェーン提案 | プロジェクト開始時に一度 |
| `/understand-project` | プロジェクト把握(読み取り専用)。`--quick / --area / --deep`。結果は grasp キャッシュに保存され、変更が無ければ次回は即答 | セッション開始時(hook が自動促し) |
| `/stack-research` | 依存バージョン固有のアンチパターン・ベストプラクティス・脆弱性を Web 調査し doc/06 に出典付き生成。プロジェクトに実在する問題はタスク化をチェーン提案 | init 直後・依存更新後(`--update`) |
| `/create-task` | 種別判定(9 種)・影響範囲調査・図解付きのタスク設計書を `task/進行中_*.md` に生成。`--refactor` で対象発見型のリファクタ分析 | 「〜をタスク化して」「リファクタして」 |
| `/do-task` | タスク設計書を実装(implementer 委託)・機械検証・実動確認・独立レビュー・完了処理。中断再開可。「検証だけ」も可 | 「タスクをやって」「続きをやって」 |
| `/update-doc` | メモリ / 権威参照ファイル / doc を実コードと同期。`--task` で完了タスク駆動の差分同期(要件タグ昇格・ADR・図・索引まで) | タスク完了後の締め |
| `/ship-task` | 上の 3 工程(設計 → 実装 → doc 同期)を 1 コマンドで通し、作業ブランチ・実装/doc の commit・push・PR 作成まで出す。既定は走り切り、要件不明・品質ゲート赤・レビュー未収束でのみ停止 | 「まるっとやって」「設計から PR まで一気に」 |
| `/discuss-spec` | テーマ単位の壁打ちで仕様を対話決定(論点分解 → 選択肢・推奨 → 合意)。決定録を生成して /reflect-decisions へチェーン | 「壁打ちしたい」「仕様を相談して決めたい」 |
| `/reflect-decisions` | 議事録・文字起こし・チャットログ等から決定事項を抽出し、精査(裏取り・レビュー・確認)を経て要件定義(doc/03)・ADR(doc/04)等へ出典付きで反映。未決・宿題は /create-task へチェーン提案 | 「議事録を反映して」「決まったことを反映して」 |
| `/export-doc` | doc をクライアント提出用に PDF / xlsx / HTML へ変換。内部情報のサニタイズ確認・機密検査・Mermaid 図の画像化付き。doc 自体は変更しない | 「PDF にして」「エクセルで出して」 |
| `/tool-check` | ツールによる機械検査(format/lint/typecheck/test/build)一括実行 | コミット前 |
| `/data-audit` | データ境界監査(読み取り専用)。機密露出・認可欠如・IDOR・過剰取得・DB 防御不足を 3 層(frontend / backend / database)で検査し、裏取り済み指摘を提案。承認分は /create-task へチェーン | 「データが漏れていないか調べて」「セキュリティ監査して」 |

推奨サイクル: `/init-project`(初回。→ stack-research へチェーン)→ `/understand-project`(毎セッション hook が促し)→ `/create-task` → `/do-task` → `/update-doc --task`
一気通貫で回す場合: `/understand-project` → `/ship-task <タスク内容>`(設計 → 実装 → doc 同期 → PR。工程の中身は上の 3 スキルそのもの)

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

### D. 他ホスト(Codex / Cursor)で使う

SKILL.md は agentskills.io の開標準で、Claude Code 以外のホストも同じ形式を読む。**可搬なのは「SKILL.md の形式」であって「置けばそのまま動く」ではない** — 本文はホスト内蔵の機構(`Agent` / `SendMessage` / `Explore` / モデルエイリアス / `.claude/` の状態ファイル)を前提とした記述を含み、他ホストでは読み替えが要る(委託の語は役割語 + 解決表で吸収していく方針。design §7-5)。

```bash
cd ~/dev/workflow
mkdir -p ~/dev/新プロジェクト                     # 配置先は事前に存在している必要がある
./setup.sh --agents ~/dev/新プロジェクト          # <パス>/.agents/skills に symlink
./setup.sh --agents-copy ~/dev/新プロジェクト     # 同じ場所にコピー(独自改変する場合)
./setup.sh --agents-global                       # ~/.agents/skills に symlink(全プロジェクト共通)
```

- **読み込み先の出典(確認日 2026-09-08)**: Codex は `.agents/skills`(リポジトリ)/ `$HOME/.agents/skills`(ユーザー)/ `/etc/codex/skills`(管理者)から読み、frontmatter は `name` + `description` が必須 — [Build skills(ChatGPT/Codex 公式)](https://learn.chatgpt.com/docs/build-skills)。Cursor は `.cursor/skills/` または `.agents/skills/` から読み `/skill-name` で起動 — [Cursor Agent Skills](https://www.learncursor.dev/learn/cursor-agents/cursor-agent-skills)。Codex の subagents は 2026-03-14 に GA — [解説記事](https://simonwillison.net/2026/Mar/16/codex-subagents/)
- **検証済み(2026-09-11)**: Codex / Cursor での SKILL.md の読み込み・動作を確認済み(ユーザーによる実環境での確認報告)。
- **ホスト固有記述の量**: サブエージェント委託を多用する skill(`/do-task`・`/create-task`・`/init-project` が特に多く、`/update-doc`・`/reflect-decisions` が続く)ほど読み替えが要る。測定コマンドと実測値は [docs/design.md](docs/design.md) §7 に置いてある。内蔵サブエージェントが無い環境では同 §5-17 の縮退プロトコル(最小段階=観点を分けた直列セルフレビュー)として読み替える
- **外部 CLI レビュアーはオプトイン**: `--runners=<名前,...>` か profile の `features.runners` を宣言したときだけ起動する。宣言が無ければホスト内蔵のレビュアーだけを使い、外部 CLI を探しに行かない(既定の挙動は従来と同じ)。**契約の正本**(使えるランナー・判定順序・終了コード・機密ガード)は [external-runners.md](plugins/dev-workflow/skills/do-task/references/external-runners.md) の 1 ファイルだけで、README や design はそれを参照する
- **委託はホスト非依存に書く(移行中)**: skill 本文は役割語(`researcher` / `implementer` / `reviewer` / `checker`)で委託を書き、どのバックエンドの何で実行するかは 1 箇所に集約する。**解決の正本**(役割語の一覧・派生名の体系・属性軸・解決順・ホストでの解決・縮退)は [delegation-map.md](plugins/dev-workflow/skills/do-task/references/delegation-map.md) の 1 ファイルだけで、README や design はそれを参照する。既存 skill 本文にはホスト固有語が残っており、移行の単位・順序・到達条件は [docs/design.md](docs/design.md) §7-7
- **MCP のツール接続は宣言駆動**: profile の `mcp_servers` で 1 回宣言し、`/init-project` が `.mcp.json`(Claude Code)と `.codex/config.toml`(Codex)の 2 形式を生成する。**生成前に解決後の宣言を提示して明示承認を得る**(信頼モデルに対する明示的な例外。`--yes` でも省略せず、応答を受け取れない実行では生成しない)。**生成手順の正本**(append-only・既存 id の判定と内容比較・退避条件・生成後の検証とロールバック)は [mcp-config-generation.md](plugins/dev-workflow/skills/init-project/references/mcp-config-generation.md) の 1 ファイルだけで、README や design はそれを参照する。**宣言のスキーマ**(`<id>: {command, args}`。`url` / `env` は受け付けない)は [docs/design.md](docs/design.md) §4
- **skill を 1 本だけ取り出す配置は非サポート**: skill 間の兄弟参照(`../do-task/...`)が解決できず、外部ランナー等の機能が無効化される
- **`features.implementer: cursor` を設定している場合**: これは未実装(起動コマンドが未定義)で、宣言しても内部 implementer で動作する。レビューのベンダー横断が目的だった場合は `features.runners` へ移行する(ベンダー横断は runners を宣言したときのみ有効)

## 既存プロジェクトとの共存・移行

- 既存プロジェクトの同名 skill(`.claude/skills/` 配下)はプロジェクト版が優先される。プラグイン版は `dev-workflow:名前` の名前空間で常に呼べる
- 旧来の独自 skills から移行する場合は旧 skill を削除し、プロジェクト固有の内容(パス・コマンド・地雷)は `.claude/project-profile.yml` と doc/05 の「引き継ぎ・地雷」へ移す。書き方は [docs/design.md §4](docs/design.md)

## このリポジトリへの還元

各プロジェクトで skill を改善したら、固有部分を profile / 動的検出に置き換えて `plugins/dev-workflow/` に反映 → バージョンを上げて commit → 各プロジェクトで更新を取得。詳細は [docs/design.md §8](docs/design.md)。

## 検証

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'  # 検証器の回帰テスト
python3 scripts/validate.py       # frontmatter / 規約(委託の語を含む)/ リンク / JSON の一括検証
claude plugin validate .          # Claude Code 本体による検証
bash -n setup.sh                  # シェル構文
bash plugins/dev-workflow/skills/do-task/scripts/review-agent-selftest.sh
                                  # 外部ランナー起動スクリプトの回帰テスト(スタブのみ・外部 CLI 不要)
```

`validate.py` は `~/dev` 配下のディレクトリ名からプロジェクト固有名の混入を検査します。`base` など、検証器の一般語除外集合に含まれる名前は対象外です。回帰テストは一時ホームとリポジトリのコピーを使い、一般語の除外と固有名・ユーザー絶対パスの検出を確認します。
