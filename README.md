# workflow — 汎用開発ワークフロー skills 集

複数の実プロジェクトで実証されたベスト要素を統合した、**どのプロジェクトでも使える** Claude Code skills 集。
新しいプロジェクトを始めるとき、ここから skills を導入すれば、タスク駆動開発のワークフロー一式がすぐ使える。

- プロジェクト固有情報(パス・コマンド・スタック)は skill にハードコードされていない。`.claude/project-profile.yml` +実行時自動検出+ 権威参照ファイル(AI への指示をまとめたプロジェクトのファイル。AGENTS.md)の 3 層で吸収する(profile〈プロジェクトの設定ファイル〉が無くても動く)
- 設計の詳細・規約・出典は [docs/design.md](docs/design.md)
- どんなに小さい変更でも独立レビューを必須とし、セルフレビューや機械チェックだけで完了・公開しない
- タスク保存先は profile の `task_dir` を優先し、未指定なら既存の状態名 MD を持つディレクトリを検出する。候補が無ければ `docs/tasks`、複数なら選択を求める。空の旧 `task/` は候補にしない

## skills 一覧(12 種)

| skill | 用途 | 呼び出し例 |
|---|---|---|
| `/init-project` | 新規/既存プロジェクトに標準構成(権威参照ファイル(AGENTS.md)/ profile / doc / 解決したタスク保存先 / gitignore / permissions / MCP)を導入。AGENTS.md には「応答の書き方」節(人が読む文の書き方の決まりと、応答の口調)を入れ、口調は「応答の口調」の質問で選ぶ(既定は報告調。段落の先頭に「告。」などの短い印を置く口調)。完了時に stack-research をチェーン提案 | プロジェクト開始時に一度 |
| `/understand-project` | プロジェクト把握(読み取り専用)。`--quick / --area / --deep`。grasp(プロジェクトを把握した結果の控え)は前回要約と参照索引に使い、毎回現在の一次情報を確認 | セッション開始時(hook〈決まったときに自動で動く処理〉が自動促し) |
| `/stack-research` | 依存バージョン固有のアンチパターン・ベストプラクティス・脆弱性を Web 調査し doc/06 に出典付き生成。プロジェクトに実在する問題はタスク化をチェーン提案 | init 直後・依存更新後(`--update`) |
| `/create-task` | 種別判定(9 種)・影響範囲調査・図解付きのタスク設計書を解決した保存先の `進行中_*.md` に生成。`--compact` は軽微変更の記録だけを短縮し品質工程を維持、`--refactor` で対象発見型のリファクタ分析(`--refactor --candidates` は発見ループの候補モード)。`候補_` のパスを渡すと、その候補(見つけた改善案のうち、まだ採用していないもの)を `進行中_` に採用して設計する | 「〜をタスク化して」「リファクタして」 |
| `/do-task` | タスク設計書を実装(implementer〈実装を受け持つ AI〉に委託〈作業を別の AI に任せること〉)・機械検証・実動確認(実際に動かして、変更どおりに動くかを見る確認)・独立レビュー・完了処理。中断再開可。「検証だけ」も可 | 「タスクをやって」「続きをやって」 |
| `/update-doc` | メモリ / 権威参照ファイル / doc を実コードと同期。`--task` で完了タスク駆動の差分同期(要件タグ昇格・ADR・図・索引まで) | タスク完了後の締め |
| `/ship-task` | 上の 3 工程(設計 → 実装 → doc 同期)を 1 コマンドで通し、作業ブランチ〈本流と分けて変更を進める、作業の流れ〉・実装/doc の commit〈変更を記録として保存すること〉・push〈記録した変更を、共有の置き場に送ること〉・PR〈変更を本流に取り込む前に、確かめてもらう依頼〉作成まで出す。既定は走り切り、停止条件(要件不明・品質ゲート〈書式・型・テスト・ビルドの自動の検査〉が赤・レビュー未収束〈レビューを繰り返しても、直す点が残った〉・実動確認の結果待ち など)に当たったときだけ停止。設計済みのタスク MD からは `--task=<タスク MD>`(Phase 2 から)、無人ループの 1 周(1 回分の実行。タスク 1 件を、新しいセッションで始めから終わりまで回す)は `--task=<タスク MD> --unattended`(`--discover` が無ければ `--task` が必須。人に確かめる場面で止まらず、保留(人の判断を待つために止める)か失敗扱い(続けられないので、その場で止まる)に倒す)。発見ループの 1 周は `--discover=<発見元> --unattended`(候補の `候補_` だけを PR で届ける) | 「まるっとやって」「設計から PR まで一気に」 |
| `/discuss-spec` | テーマ単位の壁打ちで仕様を対話決定(論点分解 → 選択肢・推奨 → 合意)。決定録を生成して /reflect-decisions へチェーン | 「壁打ちしたい」「仕様を相談して決めたい」 |
| `/reflect-decisions` | 議事録・文字起こし・チャットログ等から決定事項を抽出し、精査(裏取り〈実際のコードやファイルを読んで確かめること〉・レビュー・確認)を経て要件定義(doc/03)・ADR(doc/04)等へ出典付きで反映。未決・宿題は /create-task へチェーン提案 | 「議事録を反映して」「決まったことを反映して」 |
| `/export-doc` | doc をクライアント提出用に PDF / xlsx / HTML へ変換。内部情報のサニタイズ確認・機密検査・Mermaid 図の画像化付き。doc 自体は変更しない | 「PDF にして」「エクセルで出して」 |
| `/tool-check` | ツールによる機械検査(format/lint/typecheck/test/build)一括実行 | コミット前 |
| `/data-audit` | データ境界監査(読み取り専用)。機密露出・認可欠如・IDOR・過剰取得・DB 防御不足を 3 層(frontend / backend / database)で検査し、裏取り済み指摘を提案。承認分は /create-task へチェーン。`--candidates` は承認を待たずに `候補_` を書く発見ループの候補モード | 「データが漏れていないか調べて」「セキュリティ監査して」 |

推奨サイクル: `/init-project`(初回。→ stack-research へチェーン)→ `/understand-project`(毎セッション hook が促し)→ `/create-task` → `/do-task` → `/update-doc --task`
一気通貫で回す場合: `/understand-project` → `/ship-task <タスク内容>`(設計 → 実装 → doc 同期 → PR。工程の中身は上の 3 スキルそのもの)。設計済みのタスク MD から回すなら `/ship-task --task=<タスク MD>`

## 導入方法

do-task の補助スクリプトは bash 4.0 以上と GNU 系ツールを使います。実行前に [必要環境と確認手順](plugins/dev-workflow/skills/do-task/references/runtime-requirements.md) で PATH 上の実体を確認してください。macOS 実機は未確認で、同文書に確認手順を残しています。

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

更新の取り込み: このリポジトリ(ファイルと変更の記録をまとめて保管する場所)を更新(commit)した後、`/plugin marketplace update valour-workflow`(GitHub 経由で使っている場合は push も必要)。

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
/init-project            # 標準構成一式を生成(対話で応答の口調・MCP・hook まで。AGENTS.md に「応答の書き方」節を入れる。完了時に stack-research をチェーン提案)
/understand-project      # 把握(以後は hook が毎セッション自動で促す)
```

### D. 他ホスト(Codex / Cursor)で使う

SKILL.md は agentskills.io の開標準で、Claude Code 以外のホストも同じ形式を読む。
- **可搬なのは「SKILL.md の形式」であって「置けばそのまま動く」ではない** — 本文は `.claude/` 配下の状態ファイルを前提とする記述を含み、また**ホストがサブエージェント機構を持つかどうかで実行のしかたが変わる**ため、他ホストでは §5-17 の縮退プロトコル(使える機構に合わせて、実行の段階を下げて続ける決まり)として読み替えが要る
- (**委託そのものは役割語(AI の役割の名前)+ 解決表(役割の名前を、実際に使う AI の仕組みに対応づける表)で解決する**。design §7-5・移行の状況は §7-7。解決先を持つのは今は Claude Code だけで、他ホストでは独立レビューを要する工程が完了しない(文書の規定からの帰結で、実測ではない — design §7))。

```bash
cd ~/dev/workflow
mkdir -p ~/dev/新プロジェクト                     # 配置先は事前に存在している必要がある
./setup.sh --agents ~/dev/新プロジェクト          # <パス>/.agents/skills に symlink
./setup.sh --agents-copy ~/dev/新プロジェクト     # 同じ場所にコピー(独自改変する場合)
./setup.sh --agents-global                       # ~/.agents/skills に symlink(全プロジェクト共通)
```

- **読み込み先の出典(確認日 2026-09-08)**: Codex は `.agents/skills`(リポジトリ)/ `$HOME/.agents/skills`(ユーザー)/ `/etc/codex/skills`(管理者)から読み、frontmatter は `name` + `description` が必須 —
  - [Build skills(ChatGPT/Codex 公式)](https://learn.chatgpt.com/docs/build-skills)。
  - Cursor は `.cursor/skills/` または `.agents/skills/` から読み `/skill-name` で起動 — [Cursor Agent Skills](https://www.learncursor.dev/learn/cursor-agents/cursor-agent-skills)。
  - Codex の subagents は 2026-03-14 に GA — [解説記事](https://simonwillison.net/2026/Mar/16/codex-subagents/)
- **確認済み(2026-09-11)**: Codex / Cursor での SKILL.md の読み込み・起動(ユーザーによる実環境での確認報告。どの工程まで動いたかの記録は無い)。
- **ホスト固有記述の量**: 測定コマンドと実測値は [docs/design.md](docs/design.md) §7 に置いてある(**その語彙は `.claude/` のパス参照を含む**)。
  - **読み替えが要るかはこの値ではなくホストが実際に持つ機構で決まる**(判定手段と、段階を下げて続けるときの中身は design §5-17)。
  - 独立レビューを実施できない環境ではレビュー未完了(レビューを最後までできなかった)として扱い、完了承認・公開へ進めない
- **外部 CLI のレビュアーと implementer はオプトイン**: `--runners=<名前,...>` か profile の `features.runners`(レビュアー)/ `features.implementer`(実装。既定表にある実装用のランナー〈レビューや実装に使う外部の AI のコマンド〉の名前)を宣言したときだけ起動する。
  - 宣言が無ければホスト内蔵だけを使い、外部 CLI を探しに行かない(既定の挙動は従来と同じ)。
  - **implementer 側だけセッション初回に明示承認が要る**(書き込み委託のため。MCP 宣言〈design §7-6〉と同型)。
  - **契約の正本**(ほかが合わせる元。使えるランナー・判定順序・終了コード・機密ガード)は [external-runners.md](plugins/dev-workflow/skills/do-task/references/external-runners.md) の 1 ファイルだけで、README や design はそれを参照する
- **委託はホスト非依存に書く**: skill 本文は役割語(`researcher`〈調べものを受け持つ AI〉/ `implementer`〈実装を受け持つ AI〉/ `reviewer`〈変更を確かめる AI〉/ `checker`〈設計書を点検する AI〉)で委託を書き、どのバックエンドの何で実行するかは 1 箇所に集約する。
  - **解決の正本**(役割語の一覧・派生名の体系・属性軸・解決順・ホストでの解決・解決できないときにすること)は [delegation-map.md](plugins/dev-workflow/skills/do-task/references/delegation-map.md) の 1 ファイルだけで、README や design はそれを参照する。
  - **移行は完了している**(新規混入は `scripts/validate.py` が ERROR で止める)。
  - **語彙・検査範囲・除外集合・到達時の実測・残る論点は [docs/design.md](docs/design.md) §7-7 が正本**
- **人に聞く場面もホスト非依存に書く**: skill 本文は「質問で確認する」「複数選択の質問」などと書き、ホストの質問の道具への対応づけは [delegation-map.md](plugins/dev-workflow/skills/do-task/references/delegation-map.md) の §8 に置く。
  - 質問の仕組みが無いホストでは、同じ内容を文で聞く(聞き方の決まりの正本は [writing-for-people.md](plugins/dev-workflow/skills/do-task/references/writing-for-people.md) の 7 節)。
  - `AskUserQuestion`・`multiSelect`(大小を問わない)の新規混入は、`scripts/validate.py` のホスト CLI 語検査(ホストに結びつく名前を見つける検査)が ERROR で止める(語彙・検査範囲・除外は [docs/design.md](docs/design.md) §7-7-1 が正本)
- **MCP のツール接続は宣言駆動**: profile の `mcp_servers` で 1 回宣言し、`/init-project` が `.mcp.json`(Claude Code)と `.codex/config.toml`(Codex)の 2 形式を生成する。
  - **生成前に解決後の宣言を提示して明示承認を得る**(信頼モデルに対する明示的な例外。`--yes` でも省略せず、応答を受け取れない実行では生成しない)。
  - **生成手順の正本**(append-only・既存 id の判定と内容比較・退避条件・生成後の検証とロールバック)は [mcp-config-generation.md](plugins/dev-workflow/skills/init-project/references/mcp-config-generation.md) の 1 ファイルだけで、
  - README や design はそれを参照する。
  - **宣言のスキーマ**(`<id>: {command, args}`。`url` / `env` は受け付けない)は [docs/design.md](docs/design.md) §4
- **skill を 1 本だけ取り出す配置は非サポート**: skill 間の兄弟参照(`../do-task/...`)が解決できず、外部ランナー等の機能が無効化される
- **`features.implementer`**: `internal`(既定)| 既定表にある実装用ランナー名。
  - 既定表外の名前は無視して報告する(`runners` と同じ信頼モデル)。
  - 宣言されていてもセッション初回に明示承認を得るまでは内蔵で実行する。
  - 既定表エントリと skill 本文の配線は **v4.2.0 で完了**しており、宣言すれば実際に外部で実装される(判定・作業ツリー保護・内蔵に切り替えること・引き継ぎは上記の契約が正本)。
  - 旧値 `cursor` は既定表の名前ではない(既定表の名は `cursor-agent`)ため既定表外として扱われる。
  - レビューのベンダー横断が目的だった場合は `features.runners` へ移行する(ベンダー横断は runners を宣言したときのみ有効)

## 無人ループ(`loop.sh`。任意・既定オフ)

メタ行 `> **無人実行**: 可` を付けた `進行中_` のタスク MD を、`/ship-task` の無人モードで 1 件ずつ回す同梱スクリプト。
- 1 周 = 新しいヘッドレスセッションで、周ごとに使い捨ての worktree(同じリポジトリを別のフォルダに取り出した作業場所)を作る(人のチェックアウトには触れない。merge〈別に進めた変更を、本流に取り込むこと〉はしない)。
- **契約の正本**(引数・既定値・停止条件・終了コード・報告・限界)は [loop.md](plugins/dev-workflow/skills/ship-task/references/loop.md) の 1 ファイルだけで、README や design はそれを参照する。

```bash
# 最初とホスト CLI を更新したときに、人のシェルから 1 回: 許可の仲介(無人の実行で、操作を許すかをその場で判定する仕組み)の hook が実際に効くことを確かめ、証明を書く
bash ~/dev/workflow/plugins/dev-workflow/skills/ship-task/scripts/loop.sh \
  --repo ~/dev/対象プロジェクト --allowed-tools '<許可リスト>' --prove-host
# 人のシェルから(まず --dry-run で対象の一覧と、周で起動するコマンドを確かめる)
bash ~/dev/workflow/plugins/dev-workflow/skills/ship-task/scripts/loop.sh \
  --repo ~/dev/対象プロジェクト --allowed-tools '<許可リスト>' --dry-run
bash ~/dev/workflow/plugins/dev-workflow/skills/ship-task/scripts/loop.sh \
  --repo ~/dev/対象プロジェクト --allowed-tools '<許可リスト>'

# cron から(例: 毎晩 1 時)。cron の PATH は短いので、ホスト CLI・gh・品質ゲートのコマンドが見える PATH を書く
# (crontab の変数の行では $HOME が展開されないので、<利用者> を埋めた絶対パスで書く)
PATH=/home/<利用者>/.local/bin:/snap/bin:/usr/local/bin:/usr/bin:/bin
0 1 * * * bash $HOME/dev/workflow/plugins/dev-workflow/skills/ship-task/scripts/loop.sh --repo $HOME/dev/対象プロジェクト --allowed-tools '<許可リスト>'
```

- **起動の場所**: 人のシェルか cron から、利用者が持つこのリポジトリの clone のパスで呼ぶ。導入先のキャッシュは版ごとのパスなので、そこから呼ぶとプラグインを更新しても古い版が走る。ホストのセッションの中から起動されたと判定したら止まる
- **cron と手動の起動で環境を揃える**: `HOME`・`XDG_STATE_HOME`・`XDG_CONFIG_HOME` を同じにする。`HOME`・`XDG_STATE_HOME` が揃わないと状態ディレクトリが別になり、ロックと止めの印(人が消すまで無人ループを止める印のファイル)が共有されない(二重起動を防げず、止めの印があっても cron の起動が進む)。`XDG_CONFIG_HOME` が揃わないと、手動で書いたホスト CLI の証明が見つからず exit 20(`host-proof`)で止まる。ループ用の `CLAUDE_CONFIG_DIR` で起動するなら、cron にも同じ値を渡す(正本は loop.md §1)。PATH は上の例のように crontab に書く
- **ホスト CLI の証明**: `loop.sh` は、`--prove-host` で確かめた実体(実行ファイルの中身と置き場)と起動の形(`loop.sh` が足す隔離・権限・hook のフラグ)でしかホスト CLI を起動しない(`--dry-run` も)。この版へ初めて上げたとき(証明がまだ無い)・ホスト CLI を更新したとき(自動更新を含む)・`loop.sh` の起動の形が変わる版に上げたときは、`--prove-host` を打つ(打つまで `--dry-run` も止まる)。打つ前に、更新が正規のものかを確かめる(例: native の導入(ホスト CLI を単体の実行ファイルとして入れる導入)なら、実体が `~/.local/share/claude/versions/<版>` のファイルそのもので、その版が `--version` の値と同じこと)。`--prove-host` は打った時点の実体を信頼する。`--allow-classifier` の形は、分類器(操作を自動で許すかを AI が判定する仕組み)が許可の仲介を通さずに書き込みを許した(Claude Code 2.1.289 の実測)ため証明を書けず、今は起動しない。証明は署名ではない(正本は loop.md §2 の 12・§10)
- **skill・plugin の allowed-tools**: 有効な plugin・利用者の skill と command の `allowed-tools` が許可リストより広いと、起動時に止まる(`--dry-run` も)。その plugin・skill を無効にするか、ループ用の設定ディレクトリ(`CLAUDE_CONFIG_DIR`)で起動する。許可リストより広いだけの規則なら、同じ規則を許可リストに足してもよい(人が決める。Bash の全体・解釈できない形などは足しても止まる。正本は loop.md §4)
- **導入済みの版との関係**: `loop.sh` は自分が置かれたプラグイン(clone)を周のセッションに渡す。導入済みの `dev-workflow` が有効で版が違えば、起動時に止まる(導入済みを更新するか、無効にする)。同じ版なら中身が同じとみなす
- **許可リスト**: 全許可のモードは使わない。
  - 許可リストはホストの利用者設定か `--allowed-tools` で渡し(profile では受け付けない)、コマンド単位で列挙する(git・gh・python3・bash・判定と照合〈照らし合わせて確かめること。test・echo・sha256sum〉・読み取り系・品質ゲートのコマンドなど)。
  - 許可リストは誤操作を減らす仕組みで、隔離ではない。
  - 実走で使った値は [docs/design.md](docs/design.md) §7-3 に記録する
- **自動メモリ**: 実装・発見とも、子セッションの自動メモリを無効化する環境変数を強制する。利用者設定や既存メモリは変更しない。任意のファイル書き込みを防ぐものではない。保証範囲と実機確認の残りは [loop.md](plugins/dev-workflow/skills/ship-task/references/loop.md) §4・§8・§10。
- **許可の仲介**(無人の実行で、操作を許すかをその場で判定する仕組み): 保護パス(`.claude/` など)への書き込みは確認に回って拒否になるので、`loop.sh` が渡す hook が、周の中の状態ファイル(`.claude/reviews/` など)への書き込みだけを通す。
  - hook が Bash のコマンドを照合する許可リストも、`--allowed-tools` か利用者の設定(`permissions.allow`)から作るので、許可リストはこのどちらかに置く。
  - hook を無効にする設定(`disableAllHooks`・管理者設定の `allowManagedHooksOnly`)があると、`loop.sh` は起動しない
- **Linux 専用・bash 4.4 以上**: `setsid`・`flock`・`/proc` と `inherit_errexit` を使う。OS → bash の版 → 初期化の順に検査し、非対応環境は `--help` より先に止まる
- **状態ファイルの ignore**: 状態ファイル 3 つ(`.claude/reviews/`・`.claude/grasp.md`・`.claude/.understand-project-done`)を ignore し(追跡していれば `git rm --cached` で外し)、commit しておく(init-project の gitignore の断片)。
  - 無いと起動時に止まる(`--dry-run` も exit 20。正本は loop.md §3 の 2a)
- **origin の URL**: origin があれば、fetch と push の URL をそれぞれ 1 つにして同じリポジトリに揃え、`remote.origin.vcs` を置かない。満たさなければ起動時に止まる(`--dry-run` も exit 20。両モード)。
  - origin が無ければ起動し、周は push しない(実装モードで PR まで進む周と、発見モードで候補がある周の結末〈終わり方〉は `縮退`〈作業と commit はできたが、PR を開けなかった〉)。
  - 正本は loop.md §2 の「origin の URL の検査」
- 結果(周ごとの結末・残った worktree・人の次の手順)は朝の報告に出る。停止条件・報告の場所・保留のタスクを再び回す手順は loop.md
- **発見モード**(`--discover`。既定オフ): 発見元(改善の候補を見つける skill。`/data-audit`・`/create-task --refactor` の分析)の候補モードを 1 周ずつ回し、指摘を `候補_` のタスク MD にして、1 晩・1 発見元ごとに PR を 1 本開く。
  - 実装はしない(`進行中_` を機械が作らない)。
  - 人は要らない候補に見送りの行を足すか消してから merge し、採用するものを `/create-task <候補_ のパス>` に通す。
  - data-audit の候補は、origin の push 先が非公開と確かめられたときだけ PR にする。
  - 前提(状態ファイルの ignore と origin の URL は上の項目)と手順の正本は loop.md の「発見モード」と [discover-mode.md](plugins/dev-workflow/skills/ship-task/references/discover-mode.md)
  ```bash
  bash ~/dev/workflow/plugins/dev-workflow/skills/ship-task/scripts/loop.sh \
    --repo ~/dev/対象プロジェクト --allowed-tools '<許可リスト>' --discover --dry-run
  ```

## 既存プロジェクトとの共存・移行

- 既存プロジェクトの同名 skill(`.claude/skills/` 配下)はプロジェクト版が優先される。プラグイン版は `dev-workflow:名前` の名前空間で常に呼べる
- 旧来の独自 skills から移行する場合は旧 skill を削除し、プロジェクト固有の内容(パス・コマンド・地雷)は `.claude/project-profile.yml` と doc/05 の「引き継ぎ・地雷」へ移す。書き方は [docs/design.md §4](docs/design.md)
- **v4.3.0 への移行**: profile の `source_of_truth: claude-md` は `agents-md` に置換する(置換しないと skill が停止する)。
  - 標準構成が `AGENTS.md` のみになった — `@AGENTS.md` を import する `CLAUDE.md` を持つ既存の構成(互換形)は引き続き正常で、`/init-project` の再実行で完成形への移行を提案する

## このリポジトリへの還元

各プロジェクトで skill を改善したら、固有部分を profile / 動的検出に置き換えて `plugins/dev-workflow/` に反映 → バージョンを上げて commit → 各プロジェクトで更新を取得。詳細は [docs/design.md §8](docs/design.md)。

このリポジトリ自身では作業記録を GitHub Issue に残し、ローカルのタスク成果物を新設しない。これは配布スキルの task ファイル方式には影響しない。

## 検証

検証コマンドの一覧は [AGENTS.md](AGENTS.md) の検証節にあります。同じ一覧を 2 か所に置くと同期漏れが起きるため、このリポジトリでは `AGENTS.md` の 1 か所だけが持ちます。

シェル回帰一式の必要環境は Linux と GNU 系ツールです。GNU コマンドの不足を確認する回帰も含みます。詳細は [必要環境と確認手順](plugins/dev-workflow/skills/do-task/references/runtime-requirements.md) を参照してください。

ホスト固有の検証コマンドは各ホストの指示ファイルにあります — Claude Code なら [.claude/rules/claude-code.md](.claude/rules/claude-code.md) です。

`validate.py` は環境変数 `WORKFLOW_PROJECT_NAMES` に名前の一覧(カンマ区切り)を渡したときだけ、その名前の混入を検査します(**未設定なら検査しません**。検査の結果が渡した値だけで決まるようにするためで、実行環境のディレクトリは読みません)。
- `base` など、検証器の一般語除外集合に含まれる名前は渡しても対象外です。
- 回帰テスト(動きを自動で確かめるプログラム)はリポジトリのコピーと一時ホームを使い、各検査について「わざと壊した写しで ERROR(行の長さの検査は WARN)になる」「正しい写しでは出ない」の両方向を確認します。
