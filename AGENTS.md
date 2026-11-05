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
- Issue・PR・コミットメッセージ・作る文書・プロジェクトのメモリ・自動メモリ
- AI への依頼文と、その返答
- コード・コメント・ログ・エラー出力・設定ファイル
- 障害やセキュリティの急ぎのやり取り

## 構造

```
.claude-plugin/marketplace.json     # マーケットプレイス定義
plugins/dev-workflow/
├── .claude-plugin/plugin.json      # プラグイン定義
└── skills/<name>/SKILL.md          # 各 skill(+ references/ + scripts/ + templates/)
scripts/                            # 規約の検査スクリプト(validate.py)と、言葉の検査の除外の一覧(wording-exceptions.json)と、回帰テストを並べて流すスクリプト(run-tests.py)と、回帰テスト(test_*.py。validate.py・タスク保存先の解決・Issue 本文の書き換え・タスクの置き場・本文ダイジェスト・既知のキーの収集・origin の URL の読み方・ローカルの git 設定のダイジェスト・シェルの必要環境・書き方の決まりのコピーの一致・完了報告の型)
docs/design.md                      # 設計書(3 層吸収・統一規約・執筆規約・出典)
docs/minutes/                       # 壁打ち・決定録(design の出典)
.claude/rules/claude-code.md        # Claude Code 固有の指示(他ホストには無関係。commit して共有する)
setup.sh                            # plugin を使わない導入(コピー / symlink)
```

## skill 執筆の絶対規約(詳細は docs/design.md §5-6)

- プロジェクト固有の事実(パス・コマンド・スタック名・メモリ名)を skill 本文にハードコードしない。profile → 動的検出 → 権威参照ファイル(AGENTS.md)の 3 層で解決する
- `.claude/project-profile.yml` が無くても必ず動くこと(全項目フォールバック)
- SKILL.md は 500 行以下。description は「何を+いつ(トリガー語句)」を日本語 150〜500 字(目安 350)で(**どちらも満たさないと `validate.py` が ERROR で落とす**)
- SKILL.md の本文の 1 行は 200 字以下(表の行・コードフェンス・frontmatter を除く。超えると `validate.py` が WARN)。超える行の分け方(1 文 1 つの下位の箇条書きにする。条件と結果は同じ行に置く。収まらないときは括弧の前後で分け、それでも収まらないときだけ条件の下に 1 段深い行を置く)と、内部の言葉(言い換えを添える 27 語・置き換える語・「縮退」の分け方)と、報告の型の言葉(人に出す報告の型〈見出し・表の見出しの行・項目名・質問・選択肢〉に出る内部の言葉の書き方)は design §6 にある。内部の言葉の決まり(27 語・名前は字面のまま・置き換える語・「縮退」)は references にも当てる(言い換え表の `writing-for-people.md` は除く。行の長さの決まりは SKILL.md だけ)
- 禁止パターン(絶対パス・廃止 API・モデル ID 等)を文書に**例示として書く必要があるとき**は、その行に `<!-- validate-allow: 理由 -->` を置く(理由の記述が必須)。**救うのは禁止パターン検査とホスト CLI 語検査の 2 つ**で、委託の語はマーカーでは消えない(役割語へ書き換える)。ホスト CLI 語に効くのは、MCP サーバ名のように**生成する設定の識別子そのもので書き換えて消せない**語があるため。マーカーの**理由文にモデルエイリアスを書かない**
- 委託は役割語で書き、ホスト機構への解決は解決表 [`do-task/references/delegation-map.md`](plugins/dev-workflow/skills/do-task/references/delegation-map.md) に従う(方針は design §5 前文・§7-5)。ホストごとの API・エージェント種別・モデルエイリアスの指定方法は解決表が正本で、**この AGENTS.md には列挙しない**。**v4.0.0(2026-09-10)で移行が完了し、design §5 / §7-5 の要約併記も終了した** — 委託の語(ホスト固有の委託機構名・モデルエイリアス)は skill 本文・design のどちらにも**新たに足さない**。ホストの事実が必要なときは解決表(指定方法)か design §7-3(前提とする外部事実)へ置く
- 人に質問する場面は「質問で確認する」などと書き、ホストの道具の名前を書かない(対応づけは解決表 §8。`validate.py` のホスト CLI 語検査が ERROR で止める)
- skill 本文・出力は日本語
- 各 SKILL.md の `## 原則` の節に、人が読む文の書き方の正本 [`do-task/references/writing-for-people.md`](plugins/dev-workflow/skills/do-task/references/writing-for-people.md) を指す 1 行を、リンクの形で置く(**無いと `validate.py` が ERROR で落とす**。リンクにしていない言及・節の外・コードフェンスの中は数えない)
- skill 本文を変更するレビューでは「ホスト結合の混入」(役割語ではなくホスト機構名で委託を書いていないか)を観点に含める(design §6 が正本。対象範囲・許容リストとの関係はそちらを参照。詳細をここに列挙しない)
- skill 本文と references を変更するレビューでは、SKILL.md と references の報告の型と、内部の言葉(27 語・置き換える語・「縮退」)と、その変更が書く文が [`writing-for-people.md`](plugins/dev-workflow/skills/do-task/references/writing-for-people.md) に沿うかも観点に含める(design §6 が正本)
- 27 語の初出の言い換え・本文の言い換えの重複・名前でもコードでもない「縮退」は `validate.py` の検査 13〜15 が ERROR にする。名前と報告の型の除外は `scripts/wording-exceptions.json` に置く。名前を足すときは、定義・参照・一覧・検出するテスト・検出しないテストを同じ変更で直す。範囲と数え方は design §6 に従う

## 検証(実装終了時に必ず実行)

`validate.py` のYAML構文検査と、言葉の検査で Markdown を解釈する処理には、[requirements-dev.txt](requirements-dev.txt) の開発依存が必要です。未導入・読み込みの失敗・必要な機能の欠落は、導入案内付きのERRORとなり、終了コード1で停止します。簡易な読み取りへ切り替えて合格にはしません。

検証に使うPythonへ、次のコマンドで依存を導入します。導入と検証には同じPythonを使ってください。全回帰テスト(回帰テストをすべて流すこと)はLinux/GNUの通常Python環境で実行し、venv(Pythonの依存を分ける環境)を有効化しません。利用者別の依存を読む順序を確かめる回帰テストがあるためです。

```bash
python3 -m pip install -r requirements-dev.txt
```

`validate.py` 単体は、リポジトリ外の新しいvenvでも確認することを推奨します。リポジトリ直下で、venvを有効化せずPythonのパスを明示して実行します。

```bash
validation_venv="$(mktemp -d)"
python3 -m venv "$validation_venv"
"$validation_venv/bin/python" -m pip install -r requirements-dev.txt
"$validation_venv/bin/python" scripts/validate.py
```

`validate.py` 自身はパッケージをインストールしません。一時環境を使い終えたら、自分が作成した環境だけを片付けてください。

シェルの回帰テスト一式は **Linux と GNU 系ツール**で実行する。do-task の補助スクリプトは bash 4.0 以上、loop は Linux・bash 4.4 以上が必要。PATH の選択・パス解決の代替・macOS の未確認事項は [必要環境と確認手順](plugins/dev-workflow/skills/do-task/references/runtime-requirements.md) を参照する。

`test_shell_requirements.py` の旧 bash 境界は `SHELL_REQUIREMENTS_BASH_3_2`・`SHELL_REQUIREMENTS_BASH_4_3`・`SHELL_REQUIREMENTS_BASH_4_4` に各実体の絶対パスを渡して検証する。不在なら該当ケースを明示的に skip する(互換モードで代用しない)。bash の下限・起動順を変更するときは 3 版すべてで検証する。

```bash
# JSON 構文 + frontmatter YAML + 規約(行数・必須フィールド・禁止パターン・委託の語・リンク・原則の節の書き方の正本へのリンク・行の長さ・置き換える語〈枡・継承枠・能力帯・私有・素通り・塞ぐ・git 出口・写し・宛先・前置き・フル段階・標準段階・最小段階・段階判定・実行段階・結末の行・フル / 標準 / 最小の並びと太字・サイレント・レビュー役・実装役・まとめ役・検証役・全回帰(後ろがテストの形を除く)・試験・直読み・峻別・一時物・子起動・非ファイルタスク・退行・追随・起票・担保・倒す(「に倒す」などの形)・素の(前に漢字の無い形)・打ち直す・読込・書込・陽性対照・陰性対照・正常対照(間に「の」が入る形も)・止めの印・守る値・対話点・報告点〉・番号だけの理由(結果の行・保留の行の例)・27 語の初出の言い換え・言い換えの重複・名前でもコードでもない縮退)+ 配布メタの一括検証
python3 scripts/validate.py   # 結果の行が ERROR 0・WARN 0 で合格(WARN も直す)
python3 -B scripts/run-tests.py   # scripts/ の回帰テストの全件。結果の行が「失敗 0 件 / エラー 0 件」で、終了コードが 0 なら合格(テストの中身: validate.py・タスク保存先の解決・Issue 本文の書き換え・タスクの置き場・本文ダイジェスト・既知のキーの収集・origin の URL の読み方・ローカルの git 設定のダイジェスト・書き方の決まりのコピーの一致・完了報告の型)

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
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/create-task/scripts/resolve-task-store.py
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/do-task/scripts/restore-taskmd.py
python3 -c 'import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path("plugins/dev-workflow/skills/do-task/scripts").glob("*.py")]'   # 診断入口と内部モジュールを含む全構文
bash plugins/dev-workflow/skills/do-task/scripts/review-agent-selftest.sh      # レビュー委託の起動の回帰テスト(必須)
bash plugins/dev-workflow/skills/do-task/scripts/implement-agent-selftest.sh   # 実装委託の起動の回帰テスト(必須)
bash plugins/dev-workflow/skills/do-task/scripts/diff-snapshot-selftest.sh     # diff スナップショットの回帰テスト(必須)
bash plugins/dev-workflow/skills/do-task/scripts/implement-guard-selftest.sh   # 実装委託の前後の保護の回帰テスト(必須)
bash plugins/dev-workflow/skills/ship-task/scripts/loop-selftest.sh           # 無人ループ(loop.sh)の回帰テスト(必須)
```

`*-selftest.sh` はスタブか scratch リポジトリだけで動く(外部 CLI もネットワークも不要)。

回帰テスト(`scripts/test_*.py`)は `scripts/run-tests.py` で流す。

- 作業の途中は、変えたファイルに関係するテストのファイルだけを流す。例: `python3 -B scripts/run-tests.py test_validate.py test_writing_rules.py`
  - 関係するテストのファイルは、`grep -l '<変えたファイルの名前>' scripts/test_*.py` で探す。
- merge の前には、引数を付けずに全件を流す。
- テストのファイルごとに別のプロセスを起動する。同時に流すのは、使える CPU の数の半分まで。数は `-j <数>` で変える。
  - 数を増やしすぎると、`test_loop_startup.py` のテストが中の時間切れ(100 秒)で落ちる。落ちたら数を減らして流し直す。
- 重いファイルは、その中のテストを複数のプロセスに分けて流す。分ける数は `run-tests.py` の `SHARDS` にある。
- 失敗したプロセスの出力は、最後にまとめて出る。
- 件数と結果は、1 つのプロセスで順に流す `python3 -m unittest discover -s scripts -p 'test_*.py'` と同じ。
- テストのファイルを足すときは、ほかのテストと同じパスの一時フォルダ・ポートを使わない。`HOME` の下に作るときは、一意な名前のフォルダにする。同時に流すと取り合うため。
- シェル回帰一式(`*-selftest.sh`)は、`run-tests.py` と同時に流してよい。
  - 全回帰の時間は `loop-selftest.sh` で決まる。いちばん先に始める。
  - `loop-selftest.sh` には `LOOP_SELFTEST_TIMEOUT=6000 RUN_TIMEOUT=300` を付ける。ほかの負荷がある機械では、既定の時間切れ(全体 1500 秒・1 回の起動 150 秒)に当たる。
    - 下の時間は、どれもこの 2 つを付けて測った。
    - 既定の上限のままだと、混んだ機械では変更前の main でも、時間切れ(rc 124)や「判定」の群の FAIL 15 件になった(2026-11-05、別の作業での実測)。
  - 同時に流す前に、`mkdir -p ~/.local/state/dev-workflow` で置き場を作っておく。
    - `test_pending_implementation.py` と implement-agent・implement-guard の selftest は、この置き場を「無ければ作り、空なら消す」。
    - 置き場が無いまま同時に流すと、一方が消した直後に、他方が中に作れず落ちることがある(既知の取り合い。selftest の側は直していない)。
  - 2026-11-05 の容器(ほかのコンテナが 1〜4 個動く中)の時間:
    - `unittest discover` と 5 本を同時に流すと、全体 3821 秒(unittest 3088 秒)。
    - `run-tests.py` と 5 本を同時に流すと、全体 3847 秒(unittest 513 秒)。
    - どちらも全体の時間は `loop-selftest.sh` が決めた。`loop-selftest.sh` を単独で流すと 3718 秒。

本体にプラグイン検証コマンドを持つホストでは、それも実行する(コマンドはホスト固有のため、各ホスト固有の指示ファイル側に追記する。Claude Code は `.claude/rules/claude-code.md`)。

## このリポジトリの Issue 運用

- 置き場の設定は [`.claude/project-profile.yml`](.claude/project-profile.yml) の `task_store: github-issue` が正本です。手順は [Issue 方式の契約](plugins/dev-workflow/skills/do-task/references/github-issue.md) に従います。
- この設定を読まない古い版でも、作業記録の正本は GitHub Issue 本文です。ローカルのタスク成果物は新設しません。
- 古い版向けの完了記録の形を下に残します。現在の版では、Issue 方式の契約が定めるステータス行と完了記録が必須です。
- 開いた Issue も、ヘッダに `> **ステータス**: 🚧 進行中` の行が1行必要です。無ければ ship-task は実装前に停止します。do-task を単独で完了しても、その後の update-doc が停止します。
- 行が無ければ、コードフェンスの外の最初の `## ` より前に、上の1行を行頭から追加します。[`task-digest.py`](plugins/dev-workflow/skills/create-task/scripts/task-digest.py) は、この位置のステータス行を本文ダイジェスト(設計書の本文から計算した短い値)から除きます。
- 既存の設計レビュー行と値が一致していた本文に、この1行だけを追加しても値は変わりません。設計レビュー行はそのまま使えます。Issue 本文の再取得と承認は、Issue 方式の契約に従って取り直します。
- 完了の記録は `## 追加修正記録` の節に、`- <YYYY-MM-DD> ` で始まり、「未完了」「中断」「停止」を含まず、「完了」と「結果 APPROVED」を含む行で書く。
