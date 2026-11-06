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
- 作業全体を終える会話の報告は、区切り線 `---` の行と、見出し `## 【<結果>】<対象>` の行から始める。区切り線の前は空行にする(応答の先頭なら要らない)。`<結果>` は `完了`・`途中で止まった`・`失敗` のどれか(検証だけの報告では、検証の合否)。見出しの `【】` は段落の印ではない。見出しは最後の報告に 1 回だけ置く。skill が報告の形を決めているときは、skill の形に従う

### 口調(報告調)

人との会話の応答は、無機質な報告調で書く。段落の先頭に、次のマーカー(段落の種類を示す短い印)を置く。

| マーカー | 使う場面 | 紛らわしいマーカーとの使い分け |
|---|---|---|
| 【是】 | 肯定・同意・確認 | 否定や訂正は「【否】」 |
| 【否】 | 否定・訂正 | 起きた失敗やエラーを知らせるのは「【異常】」 |
| 【告】 | 事実・結果の報告 | まだ終わっていない作業は「【経過】」 |
| 【解】 | 説明・解説 | 事実や結果そのものは「【告】」 |
| 【答】 | 質問への直接の答え | Yes/No で答えられる問いは「【是】」か「【否】」 |
| 【提案】 | 選択肢と推奨を示す | 答えを待つのは「【問】」。両方のときは「【提案】」で案を並べ、最後に「【問】」で聞く |
| 【警告】 | これから起こりうる危険・注意点 | もう起きた失敗やエラーは「【異常】」 |
| 【問】 | 人に答えや判断を求める(進めてよいか・どちらにするか) | 選択肢を示すだけなら「【提案】」。人の手が要るなら「【依頼】」 |
| 【依頼】 | 人に操作をしてもらう(コマンドを打つ・削除する・merge する・設定を変える) | 答えるだけでよいなら「【問】」 |
| 【経過】 | 作業の途中の報告・待っている状態 | 終わった結果は「【告】」 |
| 【異常】 | もう起きた失敗・エラー(テストが落ちた・コマンドが失敗した) | これから起こりうる危険は「【警告】」 |

置き方:
- マーカーは隅付き括弧で囲み、後ろに句点を付けない。太字にしない。本文は括弧の直後に続ける(例: `【告】テストはすべて通りました。`)
- セクションの冒頭か短答の先頭に置く。1 段落に 1 つだけ置く。迷ったら「【告】」
- Yes/No の問いには「【是】」「【否】」で即答する
- 情報の提示には「【告】」「【解】」を使う
- 手順が書式を決めている行(結果の行・判定の行・ファイルの一覧)には付けず、行頭を変えない

文末:
- 結論・要約の文末に「〜と告げます」「〜と報告します」を使う。1〜3 文に 1 回ほどにし、すべての文には付けない
- 事実は「〜です」で書く
- 不確実な情報は「〜と推測されます」と書く

誤りは「【否】」で明確に正す。

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
scripts/                            # 規約の検査スクリプト(validate.py)と、言葉の検査の除外の一覧(wording-exceptions.json)と、全回帰テストを 1 つのコマンドで流すスクリプト(run-all.py)と、その部品(回帰テストを並べて流す run-tests.py・loop-selftest.sh を節ごとに分けて流す run-loop-selftest.py・共通の処理の run_units.py)と、新しい git の容器で流す入口(run-all-container.sh・container/Dockerfile)と、回帰テスト(test_*.py。validate.py・タスク保存先の解決・Issue 本文の書き換え・タスクの置き場・本文ダイジェスト・既知のキーの収集・origin の URL の読み方・ローカルの git 設定のダイジェスト・シェルの必要環境・書き方の決まりのコピーの一致・完了報告の型)
docs/design.md                      # 設計書(3 層吸収・統一規約・執筆規約・出典)
docs/testing.md                     # テストの流し方の詳しい説明と時間の記録(AGENTS.md の「検証」の節から指す)
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
python3 -B scripts/run-all.py   # 全回帰テスト(unittest と 5 本の selftest)の全部。最後の行が「結果: 合格 / <秒> 秒」で、終了コードが 0 なら合格。git が古い機械では bash scripts/run-all-container.sh

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
bash -n scripts/run-all-container.sh
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/ship-task/scripts/loop-permission.py   # 構文検査(__pycache__ を作らない)
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/ship-task/scripts/origin-repo.py
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/ship-task/scripts/git-config-digest.py
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/ship-task/scripts/host-check.py
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/create-task/scripts/candidate-keys.py
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/create-task/scripts/resolve-task-store.py
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" plugins/dev-workflow/skills/do-task/scripts/restore-taskmd.py
python3 -c 'import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path("plugins/dev-workflow/skills/do-task/scripts").glob("*.py")]'   # 診断入口と内部モジュールを含む全構文
python3 -c "import ast,sys; [ast.parse(open(p).read()) for p in sys.argv[1:]]" scripts/run_units.py scripts/run-all.py scripts/run-tests.py scripts/run-loop-selftest.py
```

`*-selftest.sh` はスタブか scratch リポジトリだけで動く(外部 CLI もネットワークも不要)。

`implement-guard-selftest.sh` は、単位ごとに流せる。

- `--list`: `--only` に渡せる単位を、1 行に 1 つ出す(78 行。群 12・M のケース 27・N のケース 13・L の変異 26)。何も作らない。
- `--only <単位>`: その単位だけを流す。変異は 1 つずつ流せる(`--only L-<記号>`)。
- `--check-units <単位,単位,...>`: 渡した単位が、全ケースをちょうど 1 回ずつ覆うかを見る(0 か 1 で終わる)。何も作らない。
- 単位を足すときは、`MUT_IDS`(変異の記号)と `ALL_CASES`(ケース ID)を直す。`--list` が覆いを確かめ、崩れていれば終了コード 1 になる。

全回帰テスト(merge の前)は、1 つのコマンドで流す。

- `python3 -B scripts/run-all.py`
  - 6 種類のテスト(unittest・loop-selftest・review-agent・implement-agent・diff-snapshot・implement-guard の selftest)を流す。共通の枠(同時に動かす数。既定は使える CPU の数)で重い順に流すのは 4 種(unittest・loop-selftest・implement-guard-selftest・diff-snapshot-selftest)。review-agent と implement-agent の selftest は、待ちが主なので枠を使わず、最初に始める。
  - `~/.local/state/dev-workflow` が無ければ、自分で作る。
- 2.36 より古い git(この作業機の 2.34.1 など)では、loop 系のテストが変更と関係なく落ちる。新しい git の容器で流す: `bash scripts/run-all-container.sh`
  - 2.42 より古いと、`run-all.py` が警告を出す(製品の `loop.sh` が 2.42 以上の機能を使うため)。
  - 全部が通ると確かめたのは 2.56.0。2.36〜2.55 で全部通るかは未確認。根拠は [docs/testing.md](docs/testing.md) の「git の版の警告の根拠」。
  - 作業ツリーの変更を含めて流すときは `--worktree`、commit を流すときは `--commit <rev>`(既定は HEAD)。
  - docker の既定の context が違う機械では、`DOCKER_CONTEXT=<名前>` を付ける。
- 合格の条件: 最後の行が `結果: 合格 / <秒> 秒` で、終了コードが 0。
- 落ちたときは、`異常:` の行と、最後にまとめて出る失敗の出力を見る。
- 時間切れが出たとき(混んだ機械で起きる)は、`-j <数>` を減らすか、ほかの重い処理が止まるのを待って流し直す。
  - `時間切れ: <秒> 秒の上限に当たった` は、`LOOP_STARTUP_CASE_TIMEOUT=<秒>` で延ばせる(既定 300 秒)。
  - judge の節の `期待 0・実際 124` か `期待 0・実際 137` は、`RUN_TIMEOUT=<秒>` で延ばせる(`run-all.py` は 600 秒を足す)。
  - 容器の入口では、`LOOP_SELFTEST_TIMEOUT`・`RUN_TIMEOUT`・`LOOP_STARTUP_CASE_TIMEOUT` をホストで設定すれば、容器へ渡る(ほかの環境変数は渡らない)。
  - 容器の入口で枠を減らすときは、`bash scripts/run-all-container.sh … -- -j <数>` と書く。
- 止めた後に、別のプロセスグループの `loop.sh` などが残ることがある。そのときは、pid を指定して止める。
  - `残った一時フォルダ(<節>。確かめてから消す): <パス>` の行が出たら、中を確かめてから消す。
- 作業の途中は、変えた所に関係するものだけを流す。
  - unittest: `python3 -B scripts/run-tests.py <テストのファイル>`(例: `test_validate.py`)。関係するファイルは `grep -l '<変えたファイルの名前>' scripts/test_*.py` で探す。
  - loop-selftest: `python3 -B scripts/run-loop-selftest.py <節>`(例: `perm d22`)。節の名前は `grep '^if want' plugins/dev-workflow/skills/ship-task/scripts/loop-selftest.sh` で見る。
  - 4 本の selftest: `bash plugins/dev-workflow/skills/do-task/scripts/<名前>-selftest.sh`(implement-guard は `--only <ケース>`。`--list` で単位を出し、変異は `--only L-<記号>` で 1 つずつ流せる)。
- 流す時点(PR を開く前)と、結果の PR の本文への書き方は、「PR の前の手順」の節に従う。
- 詳しい説明(単位・重み・容器・2 つのランナー・テストを足すときの決まり・時間の記録)は [docs/testing.md](docs/testing.md) にある。

本体にプラグイン検証コマンドを持つホストでは、それも実行する(コマンドはホスト固有のため、各ホスト固有の指示ファイル側に追記する。Claude Code は `.claude/rules/claude-code.md`)。

## PR から merge までの順序

PR を開く前に、merge する状態(main を取り込み、要るときは版を上げた状態)で全回帰テストを流し、結果を PR の本文に書く。次の順に進める。

- 版を上げ、`CHANGELOG.md` に節を足すのは、配るプラグイン(`plugins/` の下・`.claude-plugin/marketplace.json`・プラグインを使わない導入の `setup.sh`)を変えた PR だけ。変えない PR は、版も `CHANGELOG.md` も変えない(例: #344・#355・#363・#369)。
- この 2 つの節で打つ git のコマンドには、すべて Git の共通の安全用のオプション([base-commit.md](plugins/dev-workflow/skills/do-task/references/base-commit.md)。`publish-guard.py` の `SAFE_GIT_PREFIX` と同じ並び)を付ける。下の `git <安全用のオプション> …` は、その意味。確かめのコマンドの中では、関数 `g` が付ける。
- review-guard の状態の記録を取らずに作った PR(ship-task も do-task も使わずに作った PR。例: #339・#342・#363・#369)では、review-guard の手順(1 の `pr-evidence`・「PR の前の手順」の 2 の `start` から `pr-evidence` まで・順序の 4 と 6 の `verify --mode commit` と `pr-evidence`)を行わない。状態の記録を失った PR には当てない(人に知らせる)。
  - `<レビュー済み SHA>`(1 回目)と記録の確かめの `first` には、取り込む前の作業ブランチの先頭を渡す。
  - 版の変更(配るものを変えた PR)と、要るときの作業の差分の全体(衝突したときと、「PR の前の手順」の 1 が `違う:` のとき)は、ふつうのレビューに通す。ふつうのレビューは、`diff-snapshot.sh` の `--base HEAD` と `--base origin/main` のスナップショットをレビュアーに渡し、全員の判定を APPROVED にすること。`diff-snapshot.sh` は、「PR の前の手順」の 2 と同じく、main の版を取り出して流す。スナップショットには、profile(プロジェクトの設定ファイル。`.claude/project-profile.yml`)の `secret_paths`(無ければ既定の `.env`・`.env.*`・`.dev.vars`)を `--exclude-glob` で 1 つずつ渡す([diff-snapshot-call.md](plugins/dev-workflow/skills/do-task/references/diff-snapshot-call.md) のとおり)。
  - 本文の review/commit 照合の欄には「状態の記録なし」と書き、並べてレビュアーのモデルの名前と版と判定を書く。レビューのために作った commit(版を上げる commit など)は、PR の前の commit として記録の確かめの `ps` に入れる。

1. 実装の commit と文書の commit まで済ませる。
   - 配るプラグインを変えた PR は、`CHANGELOG.md` の項目を `## (次の版)` の見出しの下に書く。版の数字は、まだ決めない。
   - 最後にレビューを通した commit の SHA(コミットのハッシュ値)を、レビュー済み SHA(1 回目)として控える。ふつうは文書の commit。文書の変更が無く、文書の commit を作らなかったときは、実装の commit。
   - 文書の commit を作ったときは、main を取り込む前に、その状態の記録で `review-guard.py` の `verify --mode commit` と `pr-evidence` を行い、標準出力を PR の本文の下書きに入れる。実装の分は、実装の commit の直後に取ったものを使う。
   - 取り込んだ後は、HEAD の親と中身が変わるので、文書の状態の記録では `verify --mode commit` も `pr-evidence` も一致しない。
   - 控えた値(状態の記録のハッシュ値など)を失ったら、`pr-evidence` を作り直さない。検証のやり直しが要ると、人に知らせる。
2. main を取り込む。
   - `git <安全用のオプション> fetch origin main` の後、`git <安全用のオプション> merge-base --is-ancestor origin/main HEAD` が終了コード 0 なら、取り込む物は無い。3 へ進む。
   - そうでなければ、`git <安全用のオプション> merge --no-ff -m 'Merge main into <ブランチ> (#<番号>)' origin/main` で取り込む。
   - 衝突したら、衝突したファイルの名前を控えてから(`git <安全用のオプション> diff --name-only --diff-filter=U`)、すべて解いて取り込む。
   - `CHANGELOG.md` の先頭の衝突は、自分の節を、main の最新の版の節の上に置いて解く。main の節は変えない。
   - 版の 2 つのファイルの `"version"` の行の衝突は、main の側の値を取って解く。前の回に版を付けた後に、main が別の版へ進んだときに起きる(例: #356 の 2 回目の取り込み)。版は「PR の前の手順」の 2 で付け直す。
   - 衝突しなかったときは、git が作った取り込みの commit に手を加えない。
   - 衝突を解いた所は、レビューを通っていない。だから、衝突したときは、「PR の前の手順」の 2 で、作業の差分の全体をレビューし直す。
3. 「PR の前の手順」の節を行う(取り込みの確かめ・版を上げてレビューに通す・版の確かめ・全回帰テスト・PR の本文の欄)。
4. PR を開く。
   - `publish-guard.py` の `--sha` には、全回帰テストを流した commit(作業ブランチの先頭)の SHA を渡す。ふつうは、「PR の前の手順」の 2 で作る PR の前の commit(版を上げるか、作業の差分の全体をレビューし直して、レビューに通した commit)。
   - ship-task の Phase 4.5 を行った回(状態の記録〈`integration.md` の手順 6〉がある回)は、送る直前の照合を、P があってもなくても `integration.md` §7 のとおりに行う。古い記録の `verify --mode commit` と `pr-evidence` はしない。
     - `merged` の M があれば、`integrate-guard.py check`(`--sha=<M> --reviewed-sha=<D> --base-sha=<B> --exclude-ere=<今の列>`)が終了コード 0。
     - P があれば、手順 6 の記録で `verify --mode commit`。
     - P が無ければ、`integrate-guard.py worktree` と、その記録の `verify --mode pre-stage`。
   - Phase 4.5 を行わなかった回は、次のとおり。
     - [Issue 方式の契約](plugins/dev-workflow/skills/do-task/references/github-issue.md) §6 の、送る直前の `verify --mode commit` と `pr-evidence`(最後の commit の状態の記録で行う)は、PR の前の commit の状態の記録で行う。
     - PR の前の commit が無いとき(「PR の前の手順」の 2 を行わなかった回)は、先頭で分ける。
       - 先頭がレビュー済み SHA(1 回目)なら、その状態の記録で `verify --mode commit` と `pr-evidence` を行う。
       - 先頭が取り込みの commit なら、照合は通らない。「PR の前の手順」の 1 の `同じ:` の行と、その先頭を `last` に渡して合格した記録の確かめ ④ の行で代え、本文の「レビュー差分の外で PR に入る commit」の欄に書く。
   - 先頭でない commit の SHA を渡すと、ブランチの先頭と一致しないので、送る前に止まる。
   - PR の本文は、ship-task の Phase 5 の 3 の欄に、「PR の前の手順」の 5 の欄を足して作る。
   - Issue 方式の公開の前の確かめ(Issue 本文を取り直して R と比べることと、`origin-repo.py` の `repo` と Issue の `repo` が同じかの確かめ)は、Issue 方式の契約 §6 のとおり行う。
5. merge の直前に `git <安全用のオプション> fetch origin main` をして、次の 2 つを確かめる。
   - PR の元の SHA(`gh pr view <番号> -R <リポジトリ> --json headRefOid -q .headRefOid`)が、全回帰テストを流した commit(記録の確かめ ④ の `last`)と同じ。
   - `git <安全用のオプション> merge-base --is-ancestor origin/main <その SHA>` が終了コード 0(PR を開いた後に、main が進んでいない)。
   - 2 つとも成り立てば、merge は `gh pr merge <番号> -R <リポジトリ> --merge --match-head-commit <全回帰テストを流した commit の SHA>` で行う。
6. 5 のどちらかが成り立たなければ、merge しない。2 と 3 をやり直し、送り(push)、PR の本文を直してから、5 に戻る。
   - 送る前に、「PR の前の手順」の 5 の記録の確かめ ④ を流し直し、4 と同じ確かめをする: Issue 方式の契約 §6 の確かめ(Issue 本文を取り直して R と比べる・`origin-repo.py` の `repo` と Issue の `repo` が同じか)と、最後の commit の照合(この回に PR の前の commit を作ったら、その状態の記録で `verify --mode commit` と `pr-evidence` をやり直す。作らなかったら、4 の分かれ道のとおりに代える)。
   - 送るのは `python3 -B plugins/dev-workflow/skills/ship-task/scripts/publish-guard.py --dir <ルート> --sha <全回帰テストを流した commit の SHA> --branch <ブランチ> --config-digest <控える値> --push-only --push-url-digest <origin-repo.py の push_url_sha256>`。`<控える値>` は、`git-config-digest.py --dir <ルート>` の出力。
   - 出力が `PUSH_ONLY` で終了コード 0 なら、送った SHA と、送った先のブランチの SHA が一致している(`publish-guard.py` が、SHA を指定して送り、送った後に確かめる)。
   - 書き直すのは「全回帰テスト(merge する状態)」と「レビュー差分の外で PR に入る commit」の 2 つの欄。「review/commit 照合(PR の前の変更)」の欄は、回ごとに足す。ほかの欄は変えない。
   - 直した本文は、`gh pr edit <番号> -R <リポジトリ> --body-file -` に標準入力で渡す。

- 2 つの PR を並べて進めるときは、版を上げる PR どうしは、どちらも main の版の次の版を付けて PR を開く。先に merge した PR が、その版を使う。後の PR は、5 で main が進んだと分かるので、6 で版を付け直す(例: #360 が 4.43.4 を使い、#361 は 4.43.5 になった)。
- ship-task で進めるときは、その版の ship-task が、PR の前に main を取り込む工程(Phase 4.5)を持つかで分ける。
  - 持つ版は、`--no-pr` を付けずに走らせる。ship-task は、取り込み・衝突の解決とレビューし直し・「PR の前の手順」の 1〜3 を行い、全回帰テスト(人が行う手順)の前で、PR を開かずに止まる。止まった後に、報告の値(公開する SHA・PR の本文の下書きのパス・保持した `pr-evidence`)を確かめ、「PR の前の手順」の 4・5 と、この節の 4 から後を手で行う。
  - 持たない版は、`--no-pr` を付けて走らせる。止まった後に、1 の `pr-evidence` と、2 から後を、この節のとおり手で行う。

## PR の前の手順

main を取り込んだ後、PR を開く前に行う(順序は「PR から merge までの順序」の節)。PR を開いた後に main が進み、取り込み直したときも、同じことを行う。
- 従うのは、取り込んだ後の作業ツリーにあるこの節(main がこの節を変えていたら、変えた後の決まり)。
- 3 つの確かめのコマンドは、最初の git の呼び出しより前に `GIT_NO_LAZY_FETCH=1` を設定する。欠けた git の物を自動で取りに行く構成(promisor)で、`remote.<名>.uploadpack` のコマンドが動かないようにするため。この変数は git 2.45 以上で効く([base-commit.md](plugins/dev-workflow/skills/do-task/references/base-commit.md))。
  - 最初の git の呼び出しで、promisor の構成が無いことを、git の物を読まずに設定で確かめる(2.45 より古い git でも、物を読む前に止めるため)。あれば `不合格 ⓪:`。
  - 次に、取り込み先の main(`origin/main`)の版の `diff-snapshot.sh` を一時フォルダに取り出し、`diff-snapshot.sh --cwd . --precheck` を流す。止まるか、出力があれば `不合格 ⓪:`。
  - 作業ツリーの `diff-snapshot.sh` は、そのまま流さない。まだ確かめていない commit(`reviewed`・`checked`・`first`)のものも流さない。取り込みの後の作業ツリーと、取り込みの commit は、まだ確かめていない中身なので(取り込みや衝突の解き方で書き換わりうる)。merge 済みの main の版は、レビューと全回帰テストを通っている。

### 1. 取り込みを確かめる

次のコマンドを、作業ブランチを取り出したリポジトリの直下で流す。`<レビュー済み SHA>` は次のとおり。
- 1 回目は、レビュー済み SHA(1 回目)。ふつうは文書の commit。文書の commit が無ければ実装の commit。状態の記録を取らずに作った PR は、取り込む前の作業ブランチの先頭。
- やり直しでは、前の回に 2 で作った commit。前の回に 2 を行わなかったときは、前の回のレビュー済み SHA をそのまま使う(取り込みの commit は親が 2 つなので、⓪ で止まる)。

```bash
reviewed=<レビュー済み SHA> bash -s <<'EOF'
set -o pipefail
fail() { echo "不合格 $1"; exit 1; }
g() { git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.ignoreStat=false -c commit.gpgSign=false -c push.gpgSign=false -c filter.lfs.smudge= -c filter.lfs.clean= -c filter.lfs.process= -c filter.lfs.required=false "$@"; }
t=$(mktemp -d) || fail "⓪: 一時フォルダを作れない"
trap 'rm -rf "$t"' EXIT
export GIT_NO_LAZY_FETCH=1
rc=0; g config --get-regexp '^(extensions\.partialclone|remote\..*\.promisor)$' > "$t/promisor" || rc=$?
[ "$rc" = 1 ] || fail "⓪: 欠けた物を自動で取りに行く構成(promisor)があるか、git の設定を読めない"
main=$(g rev-parse --verify origin/main) || fail "⓪: origin/main が無い"
sd=plugins/dev-workflow/skills/do-task/scripts
g cat-file blob "$main:$sd/diff-snapshot.sh" > "$t/ds.sh" || fail "⓪: main の diff-snapshot.sh を取れない"
bash "$t/ds.sh" --cwd . --precheck > "$t/pre" || fail "⓪: diff-snapshot.sh --precheck が止めた(上の出力を読む)"
[ ! -s "$t/pre" ] || fail "⓪: diff-snapshot.sh --precheck が、承認の要る構成を見つけた"
take() { local name=$1; shift; "$@" > "$t/$name" || fail "⓪: 比べる中身を取れない($name)"; }
reviewed=$(g rev-parse --verify "$reviewed^{commit}") || fail "⓪: レビュー済み SHA が commit でない"
head=$(g rev-parse --verify HEAD) || fail "⓪: HEAD が無い"
base=$(g merge-base "$reviewed" "$main") || fail "⓪: レビュー済み SHA と main の共通の祖先が無い"
v=(plugins/dev-workflow/.claude-plugin/plugin.json .claude-plugin/marketplace.json)
x=(":(exclude)${v[0]}" ":(exclude)${v[1]}" ":(exclude)CHANGELOG.md")
work() { g diff --text --no-ext-diff --no-textconv --no-color --no-renames -U3 "$1" "$2" -- . "${x[@]}" | sed -E '/^index /d; s/^@@.*/@@/'; }
nov() { g show "$1:$2" | sed -E '/^[[:space:]]*"version": "[0-9]+\.[0-9]+\.[0-9]+",?$/d'; }
kind() { g ls-tree "$1" -- "$2" | cut -f1 | cut -d' ' -f1-2; }
udiff() { diff -u "$1" "$2" | sed -E '1,2d; s/^@@.*/@@/'; local s=("${PIPESTATUS[@]}"); [ "${s[0]}" -le 1 ] && [ "${s[1]}" = 0 ]; }
shift_of() { if [ "$(cat "$t/$1")" = "$(cat "$t/$2")" ]; then echo same; else echo "$(cat "$t/$1") -> $(cat "$t/$2")"; fi; }
[ "$(g rev-list --parents -n 1 "$reviewed" | wc -w)" = 2 ] || fail "⓪: レビュー済み SHA が、親が 1 つの commit でない"
g merge-base --is-ancestor "$reviewed" "$head" && g merge-base --is-ancestor "$main" "$head" \
  || fail "⓪: 作業ブランチの先頭が、レビュー済み SHA か main を含まない"
g cat-file blob "$main:$sd/secret-profiles.py" > "$t/sp.py" || fail "⓪: main の secret-profiles.py を取れない"
g rev-list --first-parent --parents "$head" "^$main" > "$t/chain" || fail "⓪: 作業ブランチの commit の一覧を取れない"
r=(--ref "$base" --ref "$reviewed" --ref "$main" --ref "$head")
for c in $(cat "$t/chain"); do r+=(--ref "$c"); done
python3 -B "$t/sp.py" --cwd . "${r[@]}" > "$t/sp" || fail "⓪: 機密のパスの集合を作れない"
a=(); while IFS= read -r -d '' p; do a+=(--exclude-glob "$p"); done < "$t/sp"
bash "$t/ds.sh" --print-exclude-ere "${a[@]}" > "$t/ere" || fail "⓪: 機密のパスの正規表現を作れない"
echo "機密の除外: $(cat "$t/ere")"
take p1 g diff --name-only -z --no-renames "$base" "$reviewed"; take p2 g diff --name-only -z --no-renames "$main" "$head"
rc=0; cat "$t/p1" "$t/p2" | LC_ALL=C grep -Eiz -- "$(cat "$t/ere")" > "$t/hit" || rc=$?
[ "$rc" = 1 ] || { tr '\n\0' '?\n' < "$t/hit"; fail "⓪: 機密のパス(上の行)に変更がある。中身を読まずに止めた"; }
take w1 work "$base" "$reviewed"; take w2 work "$main" "$head"
vl() { g show "$1:$2" | sed -n -E '/^[[:space:]]*"version": "[0-9]+\.[0-9]+\.[0-9]+",?$/p'; }
vbad=
for i in 0 1; do
  for c in base reviewed main head; do take "n_${c}_$i" nov "${!c}" "${v[$i]}"; take "k_${c}_$i" kind "${!c}" "${v[$i]}"; take "vl_${c}_$i" vl "${!c}" "${v[$i]}"; done
  if cmp -s "$t/vl_main_$i" "$t/vl_base_$i"; then from=reviewed; else from=main; fi
  cmp -s "$t/vl_head_$i" "$t/vl_${from}_$i" || vbad=yes
  take "m1_$i" udiff "$t/n_base_$i" "$t/n_reviewed_$i"; take "m2_$i" udiff "$t/n_main_$i" "$t/n_head_$i"
  shift_of "k_base_$i" "k_reviewed_$i" > "$t/s1_$i"; shift_of "k_main_$i" "k_head_$i" > "$t/s2_$i"
done
for c in base reviewed main head; do take "c_$c" g show "${!c}:CHANGELOG.md"; take "kc_$c" kind "${!c}" CHANGELOG.md; done
shift_of kc_base kc_reviewed > "$t/s1_c"; shift_of kc_main kc_head > "$t/s2_c"
top() { awk '/^## /{n++} n == 1' "$t/c_$1" | sed -E 's/^## v[0-9]+\.[0-9]+\.[0-9]+$/## (次の版)/'; }
rest() { awk '/^## /{n++} n != 1' "$t/c_$1"; }
same() { cmp -s "$t/$1" "$t/$2"; }
hbad=
if ! same c_base c_reviewed && same vl_main_0 vl_base_0 && same vl_main_1 vl_base_1; then
  [ "$(grep -m 1 '^## ' "$t/c_reviewed")" = "$(grep -m 1 '^## ' "$t/c_head")" ] || hbad=yes
fi
if same c_base c_reviewed; then
  log() { same c_main c_head; }
else
  take top_r top reviewed; take top_h top head; take rest_r rest reviewed; take rest_h rest head
  log() { same top_r top_h && same rest_r c_base && same rest_h c_main; }
fi
if same w1 w2 && same m1_0 m2_0 && same m1_1 m2_1 && same s1_0 s2_0 && same s1_1 s2_1 && same s1_c s2_c && [ -z "$vbad" ] && [ -z "$hbad" ] && log; then
  echo "同じ: 取り込みの後も、レビューした作業の差分は同じ(レビュー済み $reviewed・先頭 $head・main $main)"
else
  [ -z "$vbad" ] || echo "取り込みの commit の版の行が、main の値ともレビュー済み SHA の値とも合わない(取り込みに版の変更が入った)"
  [ -z "$hbad" ] || echo "main は版を変えていないのに、取り込みの commit で自分の節の見出しが変わった"
  diff "$t/w1" "$t/w2"
  echo "違う: 取り込みで、レビューした作業の差分が変わった。2 で作業の差分の全体をレビューし直す(レビュー済み $reviewed・先頭 $head・main $main)"
  exit 2
fi
EOF
```

- ⓪ レビュー済み SHA が親の 1 つの commit で、作業ブランチの先頭が、レビュー済み SHA と main を含む。違えば、最後の行が `不合格 ⓪:` で、終了コードが 1。PR を開かず、人に知らせる(最後の行が、下の続け方の 2 つの行のどちらかと全体で同じときを除く)。
- ① レビューした作業の差分(main から分かれた所からレビュー済み SHA まで)と、PR が main に足す作業の差分が同じか。同じなら最後の行が `同じ:` で終了コードが 0、違えば `違う:` で終了コードが 2。最後の行に、比べたレビュー済み SHA と先頭と main の SHA を出す。
  - 作業の差分は、変わった行と、前後 3 行の文脈を比べる。`@@` の行は `@@` だけにして、行番号と関数の見出しは比べない。main が上の行を変えて行番号がずれても「同じ」になり、行が別の所へ動けば「違う」になる。`--text` を付けて、2 進のファイルも中身で比べる。
  - git の設定(テキストへの変換・外部の diff・色・`diff.context`・名前の変更の検出)で結果が変わらないように、`--no-ext-diff --no-textconv --no-color --no-renames -U3` を付ける。
  - 版の 2 つのファイルは、`"version"` の行を消してから比べる(版の行は回ごとに変わるため。作業が `"description"` などを変えたときは、その行を比べる)。
  - 版の 3 行は、main が(main から分かれた所から)版を変えていれば main の値と、変えていなければレビュー済み SHA の値と同じかも見る。取り込みの commit に版の変更を混ぜると、「違う」になる。
  - main が版を変えていないときは、自分の節の見出しも、レビュー済み SHA と同じかを見る(見出しの版を読み替えない)。取り込みの commit で見出しだけを次の版にしても、「違う」になる。
  - 版の 2 つのファイルと `CHANGELOG.md` は、ファイルの種類と実行権限も、中身とは別に比べる(作業での変わり方と、PR での変わり方が同じか)。
  - 比べる中身は、一時フォルダに取ってから比べる(`set -o pipefail`)。git が失敗して中身を取れなければ、空どうしを比べずに `不合格 ⓪:` で止まる。
  - 機密のパスは、中身を取る前に除く。集合は、レビューに渡すスナップショットと同じ(既定の `.env`・`.env.*`・`.dev.vars` と、`.claude/project-profile.yml` の `secret_paths`。作業ツリーと、main から分かれた所・レビュー済み SHA・main・先頭と、先頭から first-parent でたどる commit のうち main に無いもの〈レビュー済み SHA の後の取り込みを含む〉と、その親すべて〈作業ブランチが main から分かれた所と、過去に取り込んだ main〉の profile の和集合)。やり直しの回も、前の回までの指定が集合から消えない(人が控える物は無い)。main の版から取り出した `secret-profiles.py` と `diff-snapshot.sh --print-exclude-ere` で作る。作業の差分か PR の差分で、このパスのどれかが変わっていれば、中身を読まずにパスだけを並べて `不合格 ⓪:` で止まる。使った正規表現は `機密の除外: ` の行に出す(2 のレビュー入力で使う)。
  - `CHANGELOG.md` は、差分では比べない(先頭の節の見出しが、回ごとに変わるため)。
  - レビュー済み SHA が `CHANGELOG.md` に節を足したときは、自分の節(最初の `## ` の節。見出しの版は `(次の版)` と見なす)がレビューしたときと同じで、ほかの所が取り込んだ main と同じことを確かめる。足していないとき(配るプラグインを変えない PR)は、`CHANGELOG.md` の全体が main と同じことを確かめる。
- 取り込みで衝突したか、① が「違う」なら、2 のレビューに、作業の差分の全体を足す(レビューし直す)。
- 最後の行が `不合格 ⓪: 機密のパス(上の行)に変更がある。中身を読まずに止めた` のときは、① の代わりに、2 で作業の差分の全体をレビューし直す(スナップショットは、機密のファイルをパスだけで出す)。3 の `<1 で確かめた先頭の SHA>` には、取り込みの commit(取り込まなかったときは、レビュー済み SHA の commit。つまり作業ブランチの先頭)を渡す。2 のレビュー入力では、1 の出力の `機密の除外: ` の行の正規表現も除く(2 の機密の除外の行)。最後の行が `不合格 ⓪: 機密のパスの集合を作れない`(作業ブランチの途中の commit の profile が読めない)ときも同じく進める。そのときは `機密の除外: ` の行が無いので、2 で取り出す main の skill 一式の `secret-profiles.py --cwd . --ref …` を、1 と同じ ref の組(`git <安全用のオプション> rev-list --first-parent --parents HEAD ^origin/main` が出す SHA・`origin/main`・`HEAD`・レビュー済み SHA・main から分かれた所)から読めない commit を外して流し、出た要素と、人が読んだその commit の `secret_paths` と、既定の `.env`・`.env.*`・`.dev.vars` を、1 つずつ `--exclude-glob` にして `diff-snapshot.sh --print-exclude-ere` に渡した正規表現を使う。ほかの `不合格 ⓪:`(`不合格 ⓪: 機密のパスの正規表現を作れない` を含む)は、PR を開かず、人に知らせる。
- 最後の行の「先頭」の SHA を、3 で使うので控える。
- レビュー済み SHA に、レビューを通っていない commit を渡しても、この確かめは見分けない。それは 5 の記録の確かめ ④ が止める。

### 2. 版を上げて、レビューに通す

版を上げる変更(と、1 で決めたときは作業の差分の全体)を、文書と同じ手順でレビューに通す。

- 配るプラグインを変えた PR は、版を上げる。変えない PR は、版を変えない(取り込みで衝突したか、1 の ① が「違う」ときだけ、作業の差分の全体をレビューし直す)。
- 版を変える前に、新しい `start` を取る。HEAD は、取り込みの commit(取り込まなかったときは、レビュー済み SHA の commit)。
  - Issue 方式は、[Issue 方式の契約](plugins/dev-workflow/skills/do-task/references/github-issue.md) §6(review/commit の全入口)のとおり、Issue 本文を取り直して R と比べてから、外部の本文として渡す。
  - review-protocol.md の「最終 doc の `start` HEAD は検証済み実装 HEAD でなければならない」は、この 2 では、取り込みの commit(取り込まなかったときは、レビュー済み SHA の commit)と読む。取り込みの commit は、1 の確かめと、要るときのレビューし直しで、レビューに結び付ける。
- 版は、取り込んだ main の版の次にする。刻みは `docs/design.md` §8 に従う(後方互換な機能追加はマイナー、内部の移行・修正はパッチ)。
- 変える所は 4 か所: `plugins/dev-workflow/.claude-plugin/plugin.json` の `"version"`(1 か所)・`.claude-plugin/marketplace.json` の `"version"`(2 か所)・`CHANGELOG.md` の自分の節の見出し(`## (次の版)` か、前の回に付けた `## v<版>` を、`## v<版>` にする)。
- 作業ブランチの版が、もう main の版の次なら、版は変えない(main が版を上げずに進んだとき)。版の行がレビュー済み SHA の値と同じかは、1 の ① が確かめる。取り込みの commit だけが版を変えていれば ① が「違う」になるので、2 で、その版の変更を含む作業の差分の全体をレビューし直し、PR の前の commit を作る。
- 順序は、`take --phase doc` → レビューに渡すファイルを作る → `seal` → `verify --mode pre-stage` → レビュー → `run-checks` → `attest` → ステージ → `verify --mode stage` → commit → `verify --mode commit` → `pr-evidence`。引数と細かい順序の正本は、[review-protocol.md](plugins/dev-workflow/skills/do-task/references/review-protocol.md) の「文書、保留、公開」の節。
  - レビューに渡すファイルは、既存の diff スナップショットの手順([diff-snapshot-call.md](plugins/dev-workflow/skills/do-task/references/diff-snapshot-call.md))で作る。`git diff` は、追跡していない新しいファイルを出さず、機密の除外もしないので使わない。
  - 2 で流すスクリプトは、作業ツリーのものを流さず、取り込み先の main の版を一時フォルダに取り出して流す(1 と同じ理由)。`review-guard.py` は、隣の `diff-snapshot.sh`・`check-process.py` と create-task の `task-digest.py` も読むので、skill の一式を取り出す。先に `export GIT_NO_LAZY_FETCH=1` を実行し、`git <安全用のオプション> config --get-regexp '^(extensions\.partialclone|remote\..*\.promisor)$'` が何も出さずに終了コード 1 で終わることを確かめてから、`d=$(mktemp -d) && git <安全用のオプション> archive origin/main plugins/dev-workflow/skills | tar -x -C "$d"` で取り出す。
  - `start` から `pr-evidence` までの `review-guard.py` は、`python3 -B "$d/plugins/dev-workflow/skills/ship-task/scripts/review-guard.py"` で流す。順序の節の 4 と 6 で、取り込んだ後に流す `review-guard.py` も同じ。
  - 版の変更: `bash "$d/plugins/dev-workflow/skills/do-task/scripts/diff-snapshot.sh" --cwd . --base HEAD --out .claude/reviews/<名>-pr-before.md --exclude <take と同じ ERE>`。
  - 1 で決めたときは、作業の差分の全体: 同じコマンドの `--base origin/main` で、`--out .claude/reviews/<名>-whole.md`。
  - 機密の除外: 2 の `review-guard.py` のすべての入口に、1 の出力の `機密の除外: ` の行の正規表現を、もう 1 つの `--exclude-ere` として足す。両方のスナップショットにも `--exclude '<その正規表現>'` を足す。1 と同じ ref(作業ブランチの commit と、その親〈分かれた所と過去に取り込んだ main〉を含む)の profile から作った集合なので、共通の祖先の設定にだけ残る機密の指定も、やり直しの回で外れない。
  - `run-checks` の計画には、`python3 -B scripts/validate.py` と、ホストのプラグイン検証(「検証」の節の最後の段落)を入れる。この 2 つだけなので、上限は既定の 600 秒で足りる。
- レビュアーが全員 APPROVED なら、変えたファイルを `git <安全用のオプション> add` でステージし、`verify --mode stage` を通して、`git <安全用のオプション> commit` で 1 つの commit にする。メッセージは `chore: 版を <版> にする (#<番号>)`。この commit を、PR の前の commit と呼ぶ。
- 版を変えず、作業の差分の全体だけをレビューし直したときは、`git <安全用のオプション> commit --allow-empty -m 'chore: 取り込みの後の作業の差分をレビューし直す (#<番号>)'` にする。review/commit 照合を、この commit に結び付けるため。
- 版を変えず、レビューし直すことも要らないときは、2 を行わない。
- commit の後に `verify --mode commit` と `pr-evidence` を行い、標準出力を、見出し `## review/commit 照合(PR の前の変更・<回> 回目)` の欄に入れる。

### 3. 版を確かめる

次のコマンドを、2 の後に(2 を行わなかったときは 1 の後に)、リポジトリの直下で流す。`<1 で確かめた先頭の SHA>` は、1 の最後の行の「先頭」の SHA(取り込んだときは取り込みの commit、取り込まなかったときはレビュー済み SHA)。最後の行が `合格:` で始まり、終了コードが 0 なら合格。

```bash
checked=<1 で確かめた先頭の SHA> bash -s <<'EOF'
set -o pipefail
fail() { echo "不合格 $1"; exit 1; }
g() { git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.ignoreStat=false -c commit.gpgSign=false -c push.gpgSign=false -c filter.lfs.smudge= -c filter.lfs.clean= -c filter.lfs.process= -c filter.lfs.required=false "$@"; }
t=$(mktemp -d) || fail "⓪: 一時フォルダを作れない"
trap 'rm -rf "$t"' EXIT
export GIT_NO_LAZY_FETCH=1
rc=0; g config --get-regexp '^(extensions\.partialclone|remote\..*\.promisor)$' > "$t/promisor" || rc=$?
[ "$rc" = 1 ] || fail "⓪: 欠けた物を自動で取りに行く構成(promisor)があるか、git の設定を読めない"
main=$(g rev-parse --verify origin/main) || fail "⓪: origin/main が無い"
sd=plugins/dev-workflow/skills/do-task/scripts
g cat-file blob "$main:$sd/diff-snapshot.sh" > "$t/ds.sh" || fail "⓪: main の diff-snapshot.sh を取れない"
bash "$t/ds.sh" --cwd . --precheck > "$t/pre" || fail "⓪: diff-snapshot.sh --precheck が止めた(上の出力を読む)"
[ ! -s "$t/pre" ] || fail "⓪: diff-snapshot.sh --precheck が、承認の要る構成を見つけた"
take() { local name=$1; shift; "$@" > "$t/$name" || fail "⓪: 比べる中身を取れない($name)"; }
checked=$(g rev-parse --verify "$checked^{commit}") || fail "⓪: 1 で確かめた先頭が commit でない"
head=$(g rev-parse --verify HEAD) || fail "⓪: HEAD が無い"
g merge-base --is-ancestor "$main" "$head" || fail "⓪: 作業ブランチの先頭が main を含まない"
[ "$head" = "$checked" ] || { [ "$(g rev-list --parents -n 1 "$head" | wc -w)" = 2 ] && [ "$(g rev-parse "$head^")" = "$checked" ]; } \
  || fail "⓪: 作業ブランチの先頭が、1 で確かめた commit でも、その上の 1 つの commit でもない"
v=(plugins/dev-workflow/.claude-plugin/plugin.json .claude-plugin/marketplace.json)
nov() { g show "$1:$2" | sed -E '/^[[:space:]]*"version": "[0-9]+\.[0-9]+\.[0-9]+",?$/d'; }
kind() { g ls-tree "$1" -- "$2" | cut -f1 | cut -d' ' -f1-2; }
for c in main head; do
  take "j_$c" g show "${!c}:${v[0]}"
  take "c_$c" g show "${!c}:CHANGELOG.md"
  take "kc_$c" kind "${!c}" CHANGELOG.md
  for i in 0 1; do take "n_${c}_$i" nov "${!c}" "${v[$i]}"; take "k_${c}_$i" kind "${!c}" "${v[$i]}"; done
done
ver() { sed -n -E 's/^[[:space:]]*"version": "([0-9]+\.[0-9]+\.[0-9]+)",?$/\1/p' "$t/j_$1"; }
old=$(ver main); new=$(ver head)
[ -n "$old" ] && [ -n "$new" ] || fail "⓪: 版を読めない"
take vd g diff --no-ext-diff --no-textconv --no-color -U0 "$main" "$head" -- "${v[@]}"
lines=$(grep -E '^[-+][[:space:]]*"version": "' "$t/vd" | sed -E 's/^([-+])[[:space:]]*"version": "([0-9.]+)",?$/\1\2/' | LC_ALL=C sort | uniq -c | awk '{print $1, $2}')
cmp -s "$t/k_main_0" "$t/k_head_0" && cmp -s "$t/k_main_1" "$t/k_head_1" && cmp -s "$t/kc_main" "$t/kc_head" \
  || fail "②: 版のファイルか CHANGELOG.md の種類か実行権限が変わった"
dist=no
g diff --quiet --no-ext-diff --no-textconv "$main" "$head" -- plugins .claude-plugin/marketplace.json setup.sh ":(exclude)${v[0]}" ":(exclude)${v[1]}"
case $? in 0) ;; 1) dist=yes ;; *) fail "⓪: 配るものの差を取れない" ;; esac
cmp -s "$t/n_main_0" "$t/n_head_0" && cmp -s "$t/n_main_1" "$t/n_head_1" || dist=yes
if [ "$dist" = no ]; then
  [ -z "$lines" ] && [ "$old" = "$new" ] || fail "②: 配るプラグインを変えていないのに、版が変わった"
  cmp -s "$t/c_main" "$t/c_head" || fail "③: 配るプラグインを変えていないのに、CHANGELOG.md が変わった"
  echo "合格: 配るプラグインを変えていないので、版は $old のまま(先頭 $head・main $main)"
  exit 0
fi
[ "$lines" = "$(printf '3 +%s\n3 -%s' "$new" "$old")" ] || fail "②: \"version\" の行の差が、main の版から新しい版への 3 行でない"
awk -v o="$old" -v n="$new" 'BEGIN { split(o, a, "."); split(n, b, ".")
  exit !((b[1] == a[1] && b[2] == a[2] && b[3] == a[3] + 1) || (b[1] == a[1] && b[2] == a[2] + 1 && b[3] == 0) \
    || (b[1] == a[1] + 1 && b[2] == 0 && b[3] == 0)) }' || fail "③: 版 $new が、main の版 $old の次でない"
[ "$(grep -m 1 '^## ' "$t/c_head")" = "## v$new" ] || fail "③: CHANGELOG.md の最初の見出しが ## v$new でない"
echo "合格: 版 $old から $new(先頭 $head・main $main)"
EOF
```

- ⓪ 作業ブランチの先頭が、1 で確かめた commit そのものか、その上の 1 つの commit(親が 1 つで、それが 1 で確かめた commit)。1 の後に別の commit が入っていれば不合格。
- 配るプラグインを変えた PR かは、main から先頭までに、`plugins/` の下・`.claude-plugin/marketplace.json`・`setup.sh` が、`"version"` の行のほかで変わったかで決める。`.claude-plugin/` の中のほかのファイルだけを変えた PR は、配るプラグインを変えていない。
- 配るプラグインを変えた PR:
  - ② main から作業ブランチの先頭までの `"version"` の行の差が、main の版から新しい版への 3 行(`plugin.json` 1 行・`marketplace.json` 2 行)。版の 2 つのファイルのほかの行は見ない(1 の ① と、2 のレビューで見る)。
  - ③ 版が main の版の次で、`CHANGELOG.md` の最初の見出しがその版。
- 配るプラグインを変えない PR: ② 版が main と同じで、③ `CHANGELOG.md` が main と同じ。
- どちらの PR でも、版の 2 つのファイルと `CHANGELOG.md` の種類と実行権限が main と同じこと(②)。版や `CHANGELOG.md` を読めなければ `不合格 ⓪:`。
- 不合格なら、PR を開かない。
  - 2 の commit があるときだけ、まだ送っていないので、`git <安全用のオプション> reset --keep HEAD^` で外し、新しい `start` から 2 をやり直す。
  - 2 を行わなかった回の ②③ の不合格と、⓪ の不合格は、reset せずに人に知らせる(外すと、取り込みの commit かレビュー済み SHA の commit を外してしまう)。ただし、次の場合を除く。
  - 配るものを変えない PR が `CHANGELOG.md` に節を足していたとき(③)は、2 の手順(新しい `start` → スナップショット → レビュー → commit)で、節を消す変更を PR の前の commit として通し、記録の確かめ ④ の `ps` に入れる。それから 3 を流し直す。

### 4. 全回帰テストを流す

- 全回帰テスト(「検証」の節の `run-all.py`)を、人が流す(人が行う手順。ship-task は流さない)。
  - `bash scripts/run-all-container.sh --commit <3 で確かめた先頭の SHA>` で流す。入口が `git archive <SHA>` で取り出し、`git init` と 1 回の commit をしたもの(`review-guard.py` の `run-checks` が作る検証用のコピーと同じ形)で流す。commit していない変更を混ぜない。
  - 2.36 より古い git では、loop 系のテストが変更と関係なく落ちる(2.36〜2.55 で全部通るかは未確認。根拠は `docs/testing.md`)。入口は新しい git の容器で流す。
  - 上限は 3600 秒(入口の `--timeout` の既定)。`docs/testing.md` の時間の記録で最も長かった全体の 2832 秒(2026-11-05 の 7 回目)より長くした。
  - 終了コード 124 は、時間切れで落ちたこと。理由を書いて流し直す。
  - 落ちたときは、理由を調べてから流し直してよい(時間切れなど。「検証」の節)。流し直すのは `run-all.py` の回(全部を流し直す)。流し直したら、前の回の結果と理由を本文に残す。
- 1 の最後の行が `不合格 ⓪:`(1 の続け方の 2 つの行は除く)でなく(取り込みで衝突したとき・`違う:` のとき・その 2 つの行のときは、2 で作業の差分の全体をレビューし直した後)、3 の最後の行が `合格:` で、`run-all.py` が終了コード 0 で、5 の記録の確かめ ④ の最後の行が `合格:` のときだけ、PR を開く。

### 5. PR の本文の欄を書く

見出し `## 全回帰テスト(merge する状態)` の欄に、次の行を書く。

```text
- 流した commit: `<SHA>`(取り込んだ main: `<SHA>`・版 <X.Y.Z>)
- 流した環境: <`git --version` の出力> / <`python3 --version` の出力>
- `run-all.py`: 終了コード <数> / <最後の `結果:` の行>
- 内訳: <`結果(` で始まる行>
- 流し直し: なし
```

- SHA は 40 桁で書く。
- 終了コードは、容器の入口の終了コード(`run-all.py` と同じ。時間切れは 124)。
- 「最後の `結果:` の行」の所には、`run-all.py` が最後に出す、`結果:` で始まる行を、字面を変えずに書く。
- 「内訳」は、`run-all.py` が出す `結果(` で始まる 6 行を、出た順に 1 行ずつ `- 内訳: ` を付けて、字面を変えずに書く。件数はこの行にあり、省かない。
- 流し直したら、`- 流し直し: なし` の代わりに、回ごとに `- 流し直し <回>: <理由>。前の回は <前の回の終了コードと結果の行>` の行を書く。
  - 前の回に結果の行が出なかったとき(時間切れで止めたときなど。`run-all.py` は、止められると最後の `結果:` の行を出さない)は、`<前の回の終了コードと結果の行>` の所に `終了コード <数> / 結果の行なし(<理由>)` と書く。
  - 最後に流した回の内訳の行の件数は、省かない。

見出し `## レビュー差分の外で PR に入る commit` の欄に、次の行を書く。

```text
- レビュー済み SHA(1 回目): `<SHA>`
- 取り込み <回>: main `<SHA>` / <1 のコマンドの最後の行> / 衝突: <なし | 衝突したファイルの名前> / レビューし直し: <しない | した>
- 版の確かめ <回>: <3 のコマンドの最後の行>
- 記録の確かめ: <下のコマンドの最後の行>
```

- 取り込みと版の確かめの行は、回ごとに 1 行ずつ書く。
- 記録の確かめ ④: 次のコマンドを、PR を開く直前(と、6 で送る直前)に流す。最後の行が `合格:` で始まり、終了コードが 0 なら合格。
  - `<レビュー済み SHA(1 回目)>` は、初回の値から変えない。
  - `<PR の前の commit の SHA>` には、2 で作った commit の SHA を、回ごとにすべて空白で区切って並べる(無ければ空にする)。
  - `<3 で確かめた先頭の SHA>` には、最後の回の 3 の結果の行の「先頭」の SHA を渡す。全回帰テストを流す commit と、PR の元の SHA も、この先頭と同じにする。

```bash
first=<レビュー済み SHA(1 回目)> ps='<PR の前の commit の SHA>' last=<3 で確かめた先頭の SHA> bash -s <<'EOF'
set -o pipefail
fail() { echo "不合格 $1"; exit 1; }
g() { git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.ignoreStat=false -c commit.gpgSign=false -c push.gpgSign=false -c filter.lfs.smudge= -c filter.lfs.clean= -c filter.lfs.process= -c filter.lfs.required=false "$@"; }
t=$(mktemp -d) || fail "⓪: 一時フォルダを作れない"
trap 'rm -rf "$t"' EXIT
export GIT_NO_LAZY_FETCH=1
rc=0; g config --get-regexp '^(extensions\.partialclone|remote\..*\.promisor)$' > "$t/promisor" || rc=$?
[ "$rc" = 1 ] || fail "⓪: 欠けた物を自動で取りに行く構成(promisor)があるか、git の設定を読めない"
main=$(g rev-parse --verify origin/main) || fail "④: origin/main が無い"
sd=plugins/dev-workflow/skills/do-task/scripts
g cat-file blob "$main:$sd/diff-snapshot.sh" > "$t/ds.sh" || fail "⓪: main の diff-snapshot.sh を取れない"
bash "$t/ds.sh" --cwd . --precheck > "$t/pre" || fail "⓪: diff-snapshot.sh --precheck が止めた(上の出力を読む)"
[ ! -s "$t/pre" ] || fail "⓪: diff-snapshot.sh --precheck が、承認の要る構成を見つけた"
first=$(g rev-parse --verify "$first^{commit}") || fail "④: レビュー済み SHA(1 回目)が commit でない"
last=$(g rev-parse --verify "$last^{commit}") || fail "④: 3 で確かめた先頭が commit でない"
head=$(g rev-parse --verify HEAD) || fail "④: HEAD が無い"
[ "$head" = "$last" ] || fail "④: 作業ブランチの先頭が、3 で確かめた先頭でない"
g merge-base --is-ancestor "$first" "$head" || fail "④: 作業ブランチの先頭が、レビュー済み SHA(1 回目)を含まない"
for p in $ps; do g rev-parse --verify "$p^{commit}" || echo "読めない SHA: $p"; done > "$t/want0" || fail "④: PR の前の commit の SHA を読めない"
LC_ALL=C sort "$t/want0" > "$t/want"
g rev-list --no-merges "$head" "^$main" "^$first" > "$t/got0" || fail "④: commit の一覧を取れない"
LC_ALL=C sort "$t/got0" > "$t/got"
g log --no-show-signature --no-decorate --format='%H %s' "$head" "^$main" "^$first" || fail "④: commit の一覧を取れない"
cmp -s "$t/got" "$t/want" || fail "④: main にもレビュー済み SHA(1 回目)にも無い、取り込みでない commit が、PR の前の commit と同じでない"
echo "合格: main にもレビュー済み SHA(1 回目)にも無い、取り込みでない commit は、PR の前の commit だけ(先頭 $head・main $main)"
EOF
```

- ④ 作業ブランチの先頭が 3 で確かめた先頭と同じで、先頭から届く commit のうち、main(取り込んだ取り込み先)にもレビュー済み SHA(1 回目)にも届かない、取り込みでない commit が、2 で作った PR の前の commit とちょうど同じ。取り込みの 2 つ目の親の側にある commit も数える。1 の後に、レビューを通っていない commit を足したとき(別のブランチを取り込んで入れたときも)と、3 の後に別の取り込みや commit を足したときは不合格。
- 不合格なら、PR を開かず、人に知らせる。
- コマンドが出す一覧(`git log --no-show-signature …` の出力。取り込みの commit も含む)を、欄の最後にコードブロックに入れて書く(件名は、リポジトリを書ける者が決めた文字列なので)。2 で作った commit の行は、頭に `P ` を付ける。
- ship-task の Phase 0 の一覧(作業ブランチの起点より前の commit)があるときは、同じ欄に並べて書く。

見出し `## review/commit 照合(PR の前の変更・<回> 回目)` の欄は、2 の commit を作った回だけ書く。文書の commit の欄(1)も、文書の commit を作ったときだけ書く。

## このリポジトリの Issue 運用

- 置き場の設定は [`.claude/project-profile.yml`](.claude/project-profile.yml) の `task_store: github-issue` が正本です。手順は [Issue 方式の契約](plugins/dev-workflow/skills/do-task/references/github-issue.md) に従います。
- この設定を読まない古い版でも、作業記録の正本は GitHub Issue 本文です。ローカルのタスク成果物は新設しません。
- 古い版向けの完了記録の形を下に残します。現在の版では、Issue 方式の契約が定めるステータス行と完了記録が必須です。
- 開いた Issue も、ヘッダに `> **ステータス**: 🚧 進行中` の行が1行必要です。無ければ ship-task は実装前に停止します。do-task を単独で完了しても、その後の update-doc が停止します。
- 行が無ければ、コードフェンスの外の最初の `## ` より前に、上の1行を行頭から追加します。[`task-digest.py`](plugins/dev-workflow/skills/create-task/scripts/task-digest.py) は、この位置のステータス行を本文ダイジェスト(設計書の本文から計算した短い値)から除きます。
- 既存の設計レビュー行と値が一致していた本文に、この1行だけを追加しても値は変わりません。設計レビュー行はそのまま使えます。Issue 本文の再取得と承認は、Issue 方式の契約に従って取り直します。
- 完了の記録は `## 追加修正記録` の節に、`- <YYYY-MM-DD> ` で始まり、「未完了」「中断」「停止」を含まず、「完了」と「結果 APPROVED」を含む行で書く。
