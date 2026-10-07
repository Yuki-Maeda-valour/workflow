# AGENTS.md

## このリポジトリについて

どのプロジェクトでも使える汎用開発ワークフロー skills 集。plugin marketplace として配布する。
**設計書 `docs/design.md` が正本。** skill の追加・変更前に必ず読むこと。

## 応答の書き方
人が読む文(会話の応答・質問・Issue と PR の本文・作る文書・コミットメッセージ)は、次の決まりで書く。

1. 結論を最初に書く
2. 1 文に 1 つのことだけ書く。短く切る
3. 箇条書きを中心にする
4. 専門用語・内部の言葉は使わない。使うときは、その会話(その文書)で初めて出すときに平易な言い換えを添える。コードの名前・コマンド・パスは `` ` `` で囲んでそのまま書く(字面を変えない行と語は除く)
5. 人にしてほしいこと(答え・操作)があるときは、最初か最後に分けて書く
6. 「対応する」「整理する」のような、何をしたか分からない言葉を避ける
7. 数字と事実で書く。確かめていない事実は「未確認」と書き、推測は推測と分かるように書く
8. 長い説明は求められたときだけ書く

- 優先順位: 決まった書式 > 正確さ > 簡潔さ > 口調。短くするために事実を落とさない。失敗と危険の知らせは短くしても省かない
- コミットメッセージとタイトルは、プロジェクトの規約(言語・接頭辞・長さ)が先
- 用語集(`doc/01_overview.md` など)があれば、プロジェクトの言葉はそれに合わせる
- 手順が書式を決めている行は字面を変えず、同じ行に何も足さない。決まった語は文の中でも字面を変えずに使う

### 口調(報告調)

人との会話の応答は、無機質な報告調で書く。段落の先頭に、次のマーカー(段落の種類を示す短い印)を置く。

| マーカー | 使う場面 | 紛らわしいマーカーとの使い分け |
|---|---|---|
| 是。 | 肯定・同意・確認 | 否定や訂正は「否。」 |
| 否。 | 否定・訂正 | 起きた失敗やエラーを知らせるのは「異常。」 |
| 告。 | 事実・結果の報告 | まだ終わっていない作業は「経過。」 |
| 解。 | 説明・解説 | 事実や結果そのものは「告。」 |
| 答。 | 質問への直接の答え | Yes/No で答えられる問いは「是。」か「否。」 |
| 提案。 | 選択肢と推奨を示す | 答えを待つのは「問。」。両方のときは「提案。」で案を並べ、最後に「問。」で聞く |
| 警告。 | これから起こりうる危険・注意点 | もう起きた失敗やエラーは「異常。」 |
| 問。 | 人に答えや判断を求める(進めてよいか・どちらにするか) | 選択肢を示すだけなら「提案。」。人の手が要るなら「依頼。」 |
| 依頼。 | 人に操作をしてもらう(コマンドを打つ・削除する・merge する・設定を変える) | 答えるだけでよいなら「問。」 |
| 経過。 | 作業の途中の報告・待っている状態 | 終わった結果は「告。」 |
| 異常。 | もう起きた失敗・エラー(テストが落ちた・コマンドが失敗した) | これから起こりうる危険は「警告。」 |

置き方:
- セクションの冒頭か短答の先頭に置く。1 段落に 1 つだけ置く。迷ったら「告。」
- Yes/No の問いには「是。」「否。」で即答する
- 情報の提示には「告。」「解。」を使う
- 手順が書式を決めている行(結果の行・判定の行・ファイルの一覧)には付けず、行頭を変えない

文末:
- 結論・要約の文末に「〜と告げます」「〜と報告します」を使う。1〜3 文に 1 回ほどにし、すべての文には付けない
- 事実は「〜です」で書く
- 不確実な情報は「〜と推測されます」と書く

誤りは「否。」で明確に正す。

当てる範囲は、人との会話の応答だけ。次には当てない:
- Issue・PR・コミットメッセージ・作る文書・メモリ
- AI への依頼文と、その返答
- コード・コメント・ログ・エラー出力・設定ファイル
- 障害やセキュリティの急ぎのやり取り

## 構造

```
.claude-plugin/marketplace.json     # マーケットプレイス定義
plugins/dev-workflow/
├── .claude-plugin/plugin.json      # プラグイン定義
└── skills/<name>/SKILL.md          # 各 skill(+ references/ + scripts/ + templates/)
scripts/                            # 規約の検証器(validate.py)と回帰テスト(test_*.py。検証器・タスク保存先の解決・本文ダイジェスト・既知のキーの収集・origin の URL の読み方・ローカルの git 設定のダイジェスト・シェルの必要環境・書き方の決まりの写しの一致)
docs/design.md                      # 設計書(3 層吸収・統一規約・執筆規約・出典)
docs/minutes/                       # 壁打ち・決定録(design の出典)
.claude/rules/claude-code.md        # Claude Code 固有の指示(他ホストには無関係。commit して共有する)
setup.sh                            # plugin を使わない導入(コピー / symlink)
```

## skill 執筆の絶対規約(詳細は docs/design.md §5-6)

- プロジェクト固有の事実(パス・コマンド・スタック名・メモリ名)を skill 本文にハードコードしない。profile → 動的検出 → 権威参照ファイル(AGENTS.md)の 3 層で解決する
- `.claude/project-profile.yml` が無くても必ず動くこと(全項目フォールバック)
- SKILL.md は 500 行以下。description は「何を+いつ(トリガー語句)」を日本語 150〜500 字(目安 350)で(**どちらも満たさないと `validate.py` が ERROR で落とす**)
- SKILL.md の本文の 1 行は 200 字以下(表の行・コードフェンス・frontmatter を除く。超えると `validate.py` が WARN)。超える行の分け方(1 文 1 つの下位の箇条書きにする。条件と結果は同じ行に置く。収まらないときは括弧の前後で分け、それでも収まらないときだけ条件の下に 1 段深い行を置く)と、内部の言葉(言い換えを添える 27 語・置き換える語・「縮退」の分け方)と、報告の型の言葉(人に出す報告の型〈見出し・表の見出しの行・項目名・質問・選択肢〉に出る内部の言葉の書き方)は design §6 にある
- 禁止パターン(絶対パス・廃止 API・モデル ID 等)を文書に**例示として書く必要があるとき**は、その行に `<!-- validate-allow: 理由 -->` を置く(理由の記述が必須)。**救うのは禁止パターン検査とホスト CLI 語検査の 2 つ**で、委託の語はマーカーでは消えない(役割語へ書き換える)。ホスト CLI 語に効くのは、MCP サーバ名のように**生成する設定の識別子そのもので書き換えて消せない**語があるため。マーカーの**理由文にモデルエイリアスを書かない**
- 委託は役割語で書き、ホスト機構への解決は解決表 [`do-task/references/delegation-map.md`](plugins/dev-workflow/skills/do-task/references/delegation-map.md) に従う(方針は design §5 前文・§7-5)。ホストごとの API・エージェント種別・モデルエイリアスの指定方法は解決表が正本で、**この AGENTS.md には列挙しない**。**v4.0.0(2026-09-10)で移行が完了し、design §5 / §7-5 の要約併記も終了した** — 委託の語(ホスト固有の委託機構名・モデルエイリアス)は skill 本文・design のどちらにも**新たに足さない**。ホストの事実が必要なときは解決表(指定方法)か design §7-3(前提とする外部事実)へ置く
- 人に質問する場面は「質問で確認する」などと書き、ホストの道具の名前を書かない(対応づけは解決表 §8。`validate.py` のホスト CLI 語検査が ERROR で止める)
- skill 本文・出力は日本語
- 各 SKILL.md の `## 原則` の節に、人が読む文の書き方の正本 [`do-task/references/writing-for-people.md`](plugins/dev-workflow/skills/do-task/references/writing-for-people.md) を指す 1 行を、リンクの形で置く(**無いと `validate.py` が ERROR で落とす**。素の言及・節の外・コードフェンスの中は数えない)
- skill 本文を変更するレビューでは「ホスト結合の混入」(役割語ではなくホスト機構名で委託を書いていないか)を観点に含める(design §6 が正本。対象範囲・許容リストとの関係はそちらを参照。詳細をここに列挙しない)
- skill 本文を変更するレビューでは、SKILL.md の報告の型と、その変更が書く文が [`writing-for-people.md`](plugins/dev-workflow/skills/do-task/references/writing-for-people.md) に沿うかも観点に含める(design §6 が正本)

## 検証(実装終了時に必ず実行)

シェル回帰一式は **Linux と GNU 系ツール**で実行する。do-task の補助スクリプトは bash 4.0 以上、loop は Linux・bash 4.4 以上が必要。PATH の選択・パス解決の代替・macOS の未確認事項は [必要環境と確認手順](plugins/dev-workflow/skills/do-task/references/runtime-requirements.md) を参照する。

`test_shell_requirements.py` の旧 bash 境界は `SHELL_REQUIREMENTS_BASH_3_2`・`SHELL_REQUIREMENTS_BASH_4_3`・`SHELL_REQUIREMENTS_BASH_4_4` に各実体の絶対パスを渡して検証する。不在なら該当ケースを明示的に skip する(互換モードで代用しない)。bash の下限・起動順を変更するときは 3 版すべてで検証する。

```bash
# JSON 構文 + frontmatter YAML + 規約(行数・必須フィールド・禁止パターン・委託の語・リンク・原則の節の書き方の正本へのリンク・行の長さ)+ 配布メタの一括検証
python3 scripts/validate.py   # 結果の行が ERROR 0・WARN 0 で合格(WARN も直す)
python3 -m unittest discover -s scripts -p 'test_*.py'   # scripts/ の回帰テスト(検証器・タスク保存先の解決・本文ダイジェスト・既知のキーの収集・origin の URL の読み方・ローカルの git 設定のダイジェスト・書き方の決まりの写しの一致)

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
bash -n plugins/dev-workflow/skills/do-task/scripts/diff-snapshot-selftest.sh
bash -n plugins/dev-workflow/skills/do-task/scripts/implement-guard.sh
bash -n plugins/dev-workflow/skills/ship-task/scripts/loop.sh
bash -n plugins/dev-workflow/skills/ship-task/scripts/loop-selftest.sh
bash -n plugins/dev-workflow/skills/do-task/scripts/reviews-dir.sh
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/ship-task/scripts/loop-permission.py   # 構文検査(__pycache__ を作らない)
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/ship-task/scripts/origin-repo.py
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/ship-task/scripts/git-config-digest.py
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/ship-task/scripts/host-check.py
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/create-task/scripts/candidate-keys.py
bash plugins/dev-workflow/skills/do-task/scripts/review-agent-selftest.sh      # レビュー委託の起動の回帰テスト(必須)
bash plugins/dev-workflow/skills/do-task/scripts/implement-agent-selftest.sh   # 実装委託の起動の回帰テスト(必須)
bash plugins/dev-workflow/skills/do-task/scripts/diff-snapshot-selftest.sh     # diff スナップショットの回帰テスト(必須)
bash plugins/dev-workflow/skills/do-task/scripts/implement-guard-selftest.sh   # 実装委託の前後の保護の回帰テスト(必須)
bash plugins/dev-workflow/skills/ship-task/scripts/loop-selftest.sh           # 無人ループ(loop.sh)の回帰テスト(必須)
```

`*-selftest.sh` はスタブか scratch リポジトリだけで動く(外部 CLI もネットワークも不要)。

本体にプラグイン検証コマンドを持つホストでは、それも実行する(コマンドはホスト固有のため、各ホスト固有の指示ファイル側に追記する。Claude Code は `.claude/rules/claude-code.md`)。

## このリポジトリの Issue 運用

- このリポジトリ自身の作業記録は GitHub Issue 本文・コメントに残し、ローカルのタスク成果物は新設しない。配布スキルの既定の task ファイル方式は変更しない
- create-task の設計成果物は指定 Issue、do-task の対象も指定 Issue とする。未指定で複数候補なら選択を確認する
- チェック状態・レビューには Issue 本文と diff を使い、完了リネームの代わりに検証結果を Issue へ記録する。中断時の `保留_` への改名も行わず、中断の理由を Issue に記録する。update-doc はその完了記録を入力にする
- ship-task の commit にタスク MD は含めず、PR 本文に Issue をリンクする
- do-task の基準コミット行(書式は `> **基準コミット**: <sha>[ / 未追跡一覧: <sha256>]`。**書式の正本は do-task/SKILL.md Phase 0 の手順 5**)は対象 Issue 本文のヘッダ引用ブロックに追記する(全文置換前に取得した本文の中身と行数を検査する)
- 設計レビューの行と do-task の検証結果の記録は、Issue 本文の末尾の `## 追加修正記録` 節(見出しはレベル 2)に置く(無ければ作る。設計レビューの行の書式は create-task/references/task-template.md の記法の規約)。基準コミット行と同じく、全文置換の前に取得した本文の中身と行数を検査する
- 設計レビューの行の `本文` と、do-task の対話の本文の照合(委託の直前の控え・Phase 4 で照らす値)は、Issue 本文から算出する(`gh issue view <N> --json body -q .body | python3 plugins/dev-workflow/skills/create-task/scripts/task-digest.py -`)
