# CLAUDE.md

## このリポジトリについて

どのプロジェクトでも使える汎用開発ワークフロー skills 集。plugin marketplace として配布する。
**設計書 `docs/design.md` が正本。** skill の追加・変更前に必ず読むこと。

## 構造

```
.claude-plugin/marketplace.json     # マーケットプレイス定義
plugins/dev-workflow/
├── .claude-plugin/plugin.json      # プラグイン定義
└── skills/<name>/SKILL.md          # 各 skill(+ references/ + scripts/ + templates/)
docs/design.md                      # 設計書(3 層吸収・統一規約・執筆規約・出典)
docs/minutes/                       # 壁打ち・決定録(design の出典)
setup.sh                            # plugin を使わない導入(コピー / symlink)
```

## skill 執筆の絶対規約(詳細は docs/design.md §5-6)

- プロジェクト固有の事実(パス・コマンド・スタック名・メモリ名)を skill 本文にハードコードしない。profile → 動的検出 → 権威参照ファイル(AGENTS.md)の 3 層で解決する
- `.claude/project-profile.yml` が無くても必ず動くこと(全項目フォールバック)
- SKILL.md は 500 行以下。description は「何を+いつ(トリガー語句)」を日本語 150〜500 字(目安 350)で
- 委託は役割語で書き、ホスト機構への解決は解決表 [`do-task/references/delegation-map.md`](plugins/dev-workflow/skills/do-task/references/delegation-map.md) に従う(方針は design §5 前文・§7-5)。Claude Code での API・エージェント種別・モデルエイリアスの指定方法は解決表が正本で、**この CLAUDE.md には列挙しない**。**v4.0.0(2026-09-10)で移行が完了し、design §5 / §7-5 の要約併記も終了した** — 委託の語(ホスト固有の委託機構名・モデルエイリアス)は skill 本文・design のどちらにも**新たに足さない**。ホストの事実が必要なときは解決表(指定方法)か design §7-3(前提とする外部事実)へ置く
- skill 本文・出力は日本語
- skill 本文を変更するレビューでは「ホスト結合の混入」(役割語ではなくホスト機構名で委託を書いていないか)を観点に含める(design §6 が正本。対象範囲・許容リストとの関係はそちらを参照。詳細をここに列挙しない)

## 検証(実装終了時に必ず実行)

```bash
# JSON 構文 + frontmatter YAML + 規約(行数・必須フィールド・禁止パターン・委託の語・リンク)の一括検証
python3 scripts/validate.py

# Claude Code 本体によるプラグイン検証
claude plugin validate .

# シェルスクリプトを触ったとき
bash -n setup.sh
bash -n plugins/dev-workflow/skills/do-task/scripts/review-agent.sh
bash plugins/dev-workflow/skills/do-task/scripts/review-agent-selftest.sh   # 外部ランナー起動の回帰テスト(必須)
```
