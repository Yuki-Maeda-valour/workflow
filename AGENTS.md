# AGENTS.md

## このリポジトリについて

どのプロジェクトでも使える汎用開発ワークフロー skills 集。plugin marketplace として配布する。
**設計書 `docs/design.md` が正本。** skill の追加・変更前に必ず読むこと。

## 構造

```
.claude-plugin/marketplace.json     # マーケットプレイス定義
plugins/dev-workflow/
├── .claude-plugin/plugin.json      # プラグイン定義
└── skills/<name>/SKILL.md          # 各 skill(+ references/ + scripts/ + templates/)
scripts/                            # 規約の検証器(validate.py)と回帰テスト(test_*.py。検証器とタスク保存先の解決)
docs/design.md                      # 設計書(3 層吸収・統一規約・執筆規約・出典)
docs/minutes/                       # 壁打ち・決定録(design の出典)
.claude/rules/claude-code.md        # Claude Code 固有の指示(他ホストには無関係。commit して共有する)
setup.sh                            # plugin を使わない導入(コピー / symlink)
```

## skill 執筆の絶対規約(詳細は docs/design.md §5-6)

- プロジェクト固有の事実(パス・コマンド・スタック名・メモリ名)を skill 本文にハードコードしない。profile → 動的検出 → 権威参照ファイル(AGENTS.md)の 3 層で解決する
- `.claude/project-profile.yml` が無くても必ず動くこと(全項目フォールバック)
- SKILL.md は 500 行以下。description は「何を+いつ(トリガー語句)」を日本語 150〜500 字(目安 350)で(**どちらも満たさないと `validate.py` が ERROR で落とす**)
- 禁止パターン(絶対パス・廃止 API・モデル ID 等)を文書に**例示として書く必要があるとき**は、その行に `<!-- validate-allow: 理由 -->` を置く(理由の記述が必須)。**救うのは禁止パターン検査だけ**で、委託の語・ホスト CLI 語はマーカーでは消えない(役割語へ書き換える)。マーカーの**理由文にモデルエイリアスを書かない**
- 委託は役割語で書き、ホスト機構への解決は解決表 [`do-task/references/delegation-map.md`](plugins/dev-workflow/skills/do-task/references/delegation-map.md) に従う(方針は design §5 前文・§7-5)。ホストごとの API・エージェント種別・モデルエイリアスの指定方法は解決表が正本で、**この AGENTS.md には列挙しない**。**v4.0.0(2026-09-10)で移行が完了し、design §5 / §7-5 の要約併記も終了した** — 委託の語(ホスト固有の委託機構名・モデルエイリアス)は skill 本文・design のどちらにも**新たに足さない**。ホストの事実が必要なときは解決表(指定方法)か design §7-3(前提とする外部事実)へ置く
- skill 本文・出力は日本語
- skill 本文を変更するレビューでは「ホスト結合の混入」(役割語ではなくホスト機構名で委託を書いていないか)を観点に含める(design §6 が正本。対象範囲・許容リストとの関係はそちらを参照。詳細をここに列挙しない)

## 検証(実装終了時に必ず実行)

```bash
# JSON 構文 + frontmatter YAML + 規約(行数・必須フィールド・禁止パターン・委託の語・リンク)+ 配布メタの一括検証
python3 scripts/validate.py
python3 -m unittest discover -s scripts -p 'test_*.py'   # scripts/ の回帰テスト(検証器・タスク保存先の解決)

# 任意: 周辺プロジェクト名の混入も見る(渡した名前だけを検査する。品質ゲートには含めない
# —— 合否がリポジトリ外のデータで決まらないようにするため)。誤検出が出た語は渡さないか、
# validate.py の一般語除外集合に足す
# (`-name '[!.]*'` で隠しディレクトリを外す。渡すと `.` 始まりの名前が普通のパス中に一致する。
#  `-printf` は GNU find の拡張)
WORKFLOW_PROJECT_NAMES=$(find ~/dev -maxdepth 1 -mindepth 1 -type d -name '[!.]*' -printf '%f,') \
  python3 scripts/validate.py

# シェルスクリプトを触ったとき
bash -n setup.sh
bash -n plugins/dev-workflow/skills/do-task/scripts/review-agent.sh
bash -n plugins/dev-workflow/skills/do-task/scripts/implement-agent.sh
bash -n plugins/dev-workflow/skills/do-task/scripts/diff-snapshot.sh
bash -n plugins/dev-workflow/skills/do-task/scripts/implement-guard.sh
bash plugins/dev-workflow/skills/do-task/scripts/review-agent-selftest.sh      # レビュー委託の起動の回帰テスト(必須)
bash plugins/dev-workflow/skills/do-task/scripts/implement-agent-selftest.sh   # 実装委託の起動の回帰テスト(必須)
bash plugins/dev-workflow/skills/do-task/scripts/diff-snapshot-selftest.sh     # diff スナップショットの回帰テスト(必須)
bash plugins/dev-workflow/skills/do-task/scripts/implement-guard-selftest.sh   # 実装委託の前後の保護の回帰テスト(必須)
```

`*-selftest.sh` はスタブか scratch リポジトリだけで動く(外部 CLI もネットワークも不要)。

本体にプラグイン検証コマンドを持つホストでは、それも実行する(コマンドはホスト固有のため、各ホスト固有の指示ファイル側に追記する。Claude Code は `.claude/rules/claude-code.md`)。

## このリポジトリの Issue 運用

- このリポジトリ自身の作業記録は GitHub Issue 本文・コメントに残し、ローカルのタスク成果物は新設しない。配布スキルの既定の task ファイル方式は変更しない
- create-task の設計成果物は指定 Issue、do-task の対象も指定 Issue とする。未指定で複数候補なら選択を確認する
- チェック状態・レビューには Issue 本文と diff を使い、完了リネームの代わりに検証結果を Issue へ記録する。update-doc はその完了記録を入力にする
- ship-task の commit にタスク MD は含めず、PR 本文に Issue をリンクする
- do-task の基準コミット行(書式は `> **基準コミット**: <sha>[ / 未追跡一覧: <sha256>]`。**書式の正本は do-task/SKILL.md Phase 0 の手順 5**)は対象 Issue 本文のヘッダ引用ブロックに追記する(全文置換前に取得した本文の中身と行数を検査する)
