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
setup.sh                            # plugin を使わない導入(コピー / symlink)
```

## skill 執筆の絶対規約(詳細は docs/design.md §5-6)

- プロジェクト固有の事実(パス・コマンド・スタック名・メモリ名)を skill 本文にハードコードしない。profile → 動的検出 → CLAUDE.md の 3 層で解決する
- `.claude/project-profile.yml` が無くても必ず動くこと(全項目フォールバック)
- SKILL.md は 500 行以下。description は「何を+いつ(トリガー語句)」を日本語 150〜350 字で
- サブエージェントは Agent + SendMessage のみ。モデルはエイリアス(opus / sonnet)のみ
- skill 本文・出力は日本語

## 検証(実装終了時に必ず実行)

```bash
# JSON 構文 + frontmatter YAML + 規約(行数・必須フィールド・禁止パターン)の一括検証
python3 scripts/validate.py

# Claude Code 本体によるプラグイン検証
claude plugin validate .
```
