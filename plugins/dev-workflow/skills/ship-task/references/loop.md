# 無人ループ(`loop.sh`)

無人ループ `loop.sh` の契約の正本(ほかが合わせる元)。外側の while(起動の前提・キューと選定・1 周〈無人ループの 1 回分の実行〉の起動と片付け・判定・停止条件・朝の報告)と、周の中の保護パスへの書き込みを通す許可の仲介(無人の実行で、操作を許すかをその場で判定する仕組み。§9)を定める。`loop.sh` の usage には引数だけを書き、既定値と意味・停止条件・終了コード・報告・限界・実走の手順はこの文書に置く(同じ事実を 2 か所に書かない)。

- 周の中の ship-task(無人モード)の振る舞いの正本は [unattended-mode.md](unattended-mode.md)。この文書は周の外側だけを定める
- 発見モード(`--discover`。発見元〈改善の候補を見つける skill〉を回して `候補_` を積む — Issue #69)の外側は §11。周の中(発見の周)の正本は [discover-mode.md](discover-mode.md)
- 無人実行のメタ行・作成日の行・保留の行の書式と照合パターンは [../../create-task/references/task-template.md](../../create-task/references/task-template.md) の記法の規約、結末の行の照合パターンは unattended-mode.md §2 が正本
- ホスト CLI の起動の雛形(実行ファイルとフラグ)と、セッションを起動しない補助の CLI(`--version`・`--help`・認証の確認・プラグイン一覧)のコマンドは `loop.sh` の既定表に置く。ホストの事実と出典は design §7-3。この文書にはホスト CLI の名前とフラグを書かない
- 要点は design §2「無人ループ」、profile のキー名は design §4。決定の経緯は決定録 2026-09-23(#66)と Issue #68

## 1. 置き場所と起動

- 本体は `ship-task/scripts/loop.sh`、回帰テストは `ship-task/scripts/loop-selftest.sh`。task_dir の解決は兄弟参照の `create-task/scripts/resolve-task-dir.py` を呼ぶ
- 人のシェル・cron から呼ぶ。skill ではない(決定録 2026-09-23 の決定 1)。ホストのセッションの中から起動されたら止まる(§2 の 2)
- 対応する OS は Linux だけ(`setsid`・`flock`・`/proc` を使う)。bash 4.4 以上が必要。ほかの OS・古い bash では初期化前に止まる
- 起動できる配置は、プラグインのルート(`loop.sh` の物理パスから 4 階層上 = `scripts/` から 3 階層上。`.claude-plugin/plugin.json` の `name` が `dev-workflow`)の下にあるときだけ。このリポジトリの clone・導入先のキャッシュ・setup.sh の `--link` の配置(物理パスで clone のルートに解決されて起動する)が当たる。setup.sh の `--copy` の配置は plugin.json を置かないので止まる
- cron から呼ぶときは、利用者が持つこのリポジトリの clone のパスで呼ぶ。導入先のキャッシュは版ごとのパスで、プラグインを更新しても古い版が黙って走るため
  - cron の環境は PATH が短いので、ホスト CLI・gh・品質ゲート(書式・型・テスト・ビルドの自動の検査)のコマンドが見える PATH を crontab に書く。`HOME`・`XDG_STATE_HOME`・`XDG_CONFIG_HOME` は手動の起動と揃える(揃わないと状態ディレクトリが別になり、ロックと止めの印〈人が消すまで無人ループを止める印のファイル〉が共有されない。`XDG_CONFIG_HOME` が違うと、手動で書いたホスト CLI の証明が見つからず止まる)。
  - `HOME` は空でない絶対 NFC パス(NFC は、同じ文字を表す別の並びを一つの形にそろえる Unicode の正規化方式)にする。`CLAUDE_CONFIG_DIR` は未設定のときだけ `HOME/.claude` を使い、設定した空文字・相対パス・NFC でない値は受け付けない。先頭の `//` と `..` 成分は拒否し、内部の `//`・`./`・末尾の `/` は同じ物理パスへ正規化して受け付ける。`~` と環境変数の文字列は展開しない。ループ用の設定ディレクトリで起動するなら、cron にも同じ値を渡す。設定の `env` による読込先の変更は §9 で拒否する。
  - hook のコマンドを包む環境変数(`CLAUDE_CODE_SHELL_PREFIX`)は外して起動する。非空なら起動前に止まる(§2)
- `loop.sh` は自分のプラグインルートを、周のホスト CLI に必ず渡す(フラグは既定表)。導入済みの同名プラグインとの関係は §2 の 12

### 引数

数値はどれも正の整数で、先頭に 0 を付けず、999999999 以下(満たさなければ使い方の誤り)。

| 引数 | 既定 | 意味 |
|---|---|---|
| `--repo <パス>` | 現在のディレクトリ | 対象のリポジトリ。git の作業ツリーのトップで、bare でないこと |
| `--host <名前>` | 既定表の 1 行 | 既定表にあるホストの名前。既定表は対応ホストの 1 行だけ(Issue #68 の決定 1。対応ホストと、ほかのホストを載せない理由は design §7-3)。既定表に無ければ起動時に止まる |
| `--host-argv <トークン>`(繰り返し可) | なし | 実行ファイルと前置きの引数。許可するのは `--model=値`・`--effort=値`・`--max-budget-usd=値`・`--max-turns=値`・`--name=値`・`--verbose` だけ。値の型も検査する。未知・短い別名・束ね書き・重複は拒否する |
| `--only <名>`(繰り返し可) | なし | 拾うタスクをその {名} に絞る |
| `--discover` / `--discover=<名>[,<名>…]` | なし(実装モード) | 発見モードで起動する(§11)。値が無ければ既定の発見元の列。`--only` とは併用しない |
| `--dry-run` | ― | 対象の一覧と解決後の argv を出して終わる。周のセッションを起動しない(§2 の末尾)。ホスト CLI の証明(§2 の 12)が要る |
| `--prove-host` | ― | ホスト CLI の実体で、許可の仲介の hook が実際に効くことを 1 回のセッションで確かめ、証明を書いて終わる(§2 の 12 の「ホスト CLI の証明」)。周は回さない。`--dry-run`・`--discover`・`--only` とは併用しない(使い方の誤り)。人が明示して打つ |
| `--allowed-tools <値>`(繰り返し可) | なし | ホスト CLI の許可リストに渡す値(§4 の権限)。空の値と `-` で始まる値は使い方の誤り。繰り返したときは連結せず、許可リストのフラグ 1 つの後に、指定した順に 1 回分を 1 トークンとして並べて渡す(隔離と権限のフラグはその後ろ。§2 の 5) |
| `--allow-classifier` | ― | 分類器による自動承認(操作を自動で許すかを AI が判定する仕組み)の指定。使わない。指定すると §2 の 2 の直後に exit 20(`classifier`)で止まる(`--prove-host`・`--dry-run` を含む。#107 の H48) |
| `--mcp-config <ファイル>`(繰り返し可) | なし | 周のホスト CLI に渡す MCP の設定(利用者の側の値。§4 の隔離)。ファイルが無ければ使い方の誤り。在れば絶対パスにして渡す(周の cwd は worktree のため) |
| `--max-iterations <N>` | 5 | 最大周回数(§6) |
| `--max-consecutive-failures <N>` | 2 | 連続失敗の上限(§6) |
| `--time-budget <秒>` | 28800(8 時間) | ループ全体の時間予算(§6) |
| `--iteration-timeout <秒>` | 3600 | 1 周の壁時計の上限。超えた周は打ち切って失敗(時間切れ)にする(§4) |
| `--kill-grace <秒>` | 30 | 片付けの TERM から KILL までの猶予(§4) |
| `--net-timeout <秒>` | 60 | ネットワークに出うる git と補助の CLI のタイムアウト(§2 の前置き)。`worktree add` は 600 秒で固定で、この引数では変えない |
| `--stop-file <パス>` | `<対象>/.claude/loop.stop` | 停止ファイル(§6) |
| `--worktree-root <パス>` | `<対象の親ディレクトリ>/<対象の名前>.loop` | 周の worktree を作る場所(§3 の 2) |
| `--state-max-items <N>` | 100000 | 状態観察で記録する filesystem 項目の上限。超過は比較不能として停止 |
| `--state-max-bytes <N>` | 1073741824 (1 GiB) | 状態観察で読む通常ファイルの合計上限。機密・特殊ファイル本文は開かない |
| `--state-max-file-bytes <N>` | 67108864 (64 MiB) | 状態観察で読む通常ファイル 1 件の上限 |
| `--state-max-seconds <N>` | 60 | 状態観察 1 回の壁時計上限 |

### profile(`features.loop`)と実効値

- `features.loop` で変えられるのは数値の 4 項目だけ: `max_iterations`・`max_consecutive_failures`・`time_budget`・`iteration_timeout`(意味は上の表の同名の引数)
- 締める向き(値を小さくする)だけ効く。実効値 = min(既定値か起動引数の値, profile の値)。広げるのは起動引数だけ(リポジトリを書ける者が夜の予算を延ばせないように。design §4 の信頼モデル)
- `features.loop` の未知のキーは無視して報告する。起動の雛形の上書き・許可リスト・MCP の設定は profile では受け付けない(起動引数かホストの利用者設定だけ)
- **読み元**: 起動時に固定したデフォルトブランチの sha(§2 の 8)の commit から `.claude/project-profile.yml` を読む。`git ls-tree <sha> -- .claude/project-profile.yml` でモード 100644 の blob であることを確かめてから、その blob を読む。周の worktree と同じ中身になり、人の作業ツリーの未 commit の変更に左右されない
- YAML は PyYAML の `safe_load` で読む。profile が在って PyYAML が無ければ、起動時に止まる
- **null(値が無い)**: `features`・`features.loop`・各キーが null のときは、キーが無いときと同じく既定値を使う(`loop:` の下をすべてコメントにした profile も起動できるように)
- **起動時に止まる値**: YAML の構文エラー / `features` か `features.loop` が null でもマッピングでもない / null 以外で整数でない値(bool・小数・文字列を含む。PyYAML の `yes`・`true` は bool)/ 0 以下 / 999999999 を超える / 実効値で `time_budget` < `iteration_timeout`(1 周も回らない)
- `task_dir` は [task-directory.md](../../create-task/references/task-directory.md) の「優先順位 2」の型の規則で検査し、文字列なら `resolve-task-dir.py --task-dir=<値>` で渡す(キーが無い・null なら渡さない)。不正なら同じ規則どおり止まる。値は §3 の文字の制限も満たすこと
- `root` は `.` と `./` を parent-child でないとみなす。それ以外の文字列なら parent-child 構成として止まる(文字列でなければ不正な値)。キーが無い・null なら検査しない。ship-task の無人の前提と同じ

## 2. 起動と前提の検査

この順で行う。1 つでも満たさなければ、worktree を作らずに `ERROR [理由コード]` を出して止まる(exit 20。1 の使い方の誤りだけは exit 2)。理由コードと文言は `loop.sh` が持つ。

**最優先の環境検査**(引数・ホスト判定より前)

- `uname -s` で Linux を確認する。非 Linux と `uname` 自体の失敗は exit 20 / `ERROR [os]`。続いて bash 4.4 以上を確認し、Linux の旧 bash は exit 20 / `ERROR [bash-version]`。bash 3.2 でも読める構文で、未初期化の `die`・trap は使わず診断する。
- OS → bash の版 → `shopt`・配列・date・git・trap などの初期化の順。非対応環境では `--help`・不正引数より環境エラーを優先する。対応環境の `--help` は exit 0、不正引数は exit 2 を維持する。

**前置き**(環境検査を通った後に行う)

- `git` の有無を確かめ(無ければ理由コード `tool-missing`)、git のローカルな環境変数(`git rev-parse --local-env-vars` の列と `GIT_CONFIG_KEY_<n>`・`GIT_CONFIG_VALUE_<n>`)を外し、`GIT_NO_LAZY_FETCH=1` を付ける。`GIT_CONFIG_GLOBAL`・`GIT_CONFIG_SYSTEM`・`GIT_CONFIG_NOSYSTEM` は利用者の側の値として残す
- `loop.sh` 自身の git には [base-commit.md](../../do-task/references/base-commit.md) の前置き(`-c core.hooksPath=/dev/null -c core.fsmonitor=` ほか)を付ける。前置きが無いと `git worktree add` が post-checkout hook を実行する
- ネットワークに出うる git(`ls-remote`・`worktree add`〈LFS の checkout が取りに行く〉)は、`GIT_TERMINAL_PROMPT=0`・stdin を `/dev/null`・`setsid`・タイムアウト(`--net-timeout`。`worktree add` は 600 秒で固定)で打つ(夜中に認証を尋ねられて固まらないように)。以下「ネットワークの規則」と呼ぶ

**検査**

1. 引数の検査(使い方の誤りは exit 2。`--mcp-config` のファイルが無いときを含む)
2. ホストのセッションの中でない: [external-runners.md](../../do-task/references/external-runners.md) §3 判定 2 の環境変数の列のどれか 1 つでも立っていれば止まる。`DEV_WORKFLOW_HOST_CLI` は非空なら止まる向きに読む(判定 2 では「どのホストか」を知るための上書きだが、`loop.sh` には「ホストの中か」だけが要る)。判定はベストエフォートで、判定できないホストは素通りする(決定録 2026-09-23 の決定 12)。列が external-runners.md と一致することは `loop-selftest.sh` が照合(照らし合わせて確かめること)する
3. 道具(OS・bash は最優先で検査済み): `setsid`・`flock`・`python3`・`timeout`・`realpath`・`stat` がある(無ければ理由コード `tool-missing`。`git` は前置きで確かめる。ホスト CLI の実在は 12)。初回の状態取得で system config の既定パスを使うときは、同じ `git` の `git var GIT_CONFIG_SYSTEM` が実パスを返せることも要る。返せなければ周を始めず、利用者が `GIT_CONFIG_SYSTEM` に実パスを明示してからやり直す。
4. プラグインのルートと名前(§1)。兄弟の `host-argv.py`・`loop-supervisor.py`・`host-check.py`(12 のホスト CLI の証明と allowed-tools の検査)・`resolve-task-dir.py`・許可の仲介の `loop-permission.py`・`origin-repo.py`(10 の後の origin の URL の検査で打つ。周の skill も使う)・`git-config-digest.py`(周の skill がローカルの git 設定の照合に使う。`loop.sh` は打たない)が在る(無ければ理由コード `plugin-root`)。`python3` を絶対パスに解決する(`/` で始まらなければ `tool-missing`。許可の仲介の hook のコマンドに使う — §9)
5. `--host` が既定表にある。`host-argv.py` で既定または上書きの引数を検査する。実行ファイルの後ろは §1 の許可表だけを受け付ける。値は `=` で同じトークンに書く。`effort` は `low`・`medium`・`high`・`xhigh`・`max`、予算は非負の数、回数は正の整数に限る。継続・再開・遠隔実行・plugin 追加・hook 無効化を含む未知の引数、短い別名と束ね書き、重複、値の欠落は子の起動前に拒否する。この引数検査はホスト起動・認証・YAML 読取を行わない。隔離と判定に要る固定引数は従来どおり親が後ろに足す。
   - ホスト CLI へ渡す argv の順は、雛形か `--host-argv` のトークン → 許可リストの値(`--allowed-tools`。`-` で始まる値は使い方の誤り)→ MCP の設定 → 隔離と権限のフラグ(と、許可の仲介の hook を渡す設定を足す起動引数。§9)。隔離と権限のフラグを argv の最後に置く
   - 解決後の argv(`--allowed-tools` の値を含む)に全許可のフラグが無いこと
6. **起動前の管理パス検査**: 最初の通常 Git・設定読取より先に、入力 `--repo` の `.git` と admin/common の対応、状態ディレクトリと中断印を Git 無し・nofollow・有界に確認する。入力 repo の**絶対 path**に対応する binding は比較専用で、値から任意の path をたどらない。物理 TOP は binding の別の保持値である。binding の不一致・未知の印・旧形式の中断情報は Git を起動せず exit 20 とし、`inflight` と `stop-mark` を保全する。`--local-env-vars` の能力照会も対象 repo の cwd や利用者の global/system 設定を読ませない
   - 通常の cold 検査と状態ディレクトリの安全な配置に通った後だけ、入力 repo の絶対 path と admin/common の対応を排他的に binding する。正常な reinit や管理ディレクトリ移転は、旧状態を人が確認した後で該当 binding を作り直す。別 repo の binding や中断印で正常な起動を止めない
   - 同じ利用者権限で binding と全中断記録を同時に改竄・削除することは防げない。成立条件・影響・検出できる範囲と必要な運用は §10 に従う
6′. この検査に通った後、対象(`--repo`)が git の作業ツリーのトップで、bare でないことを Git で確かめる。`--repo` が linked worktree のときに 10 で比べる main の worktree も、ここで解決する(解決できなければ止まる)。人の作業場所の管理ディレクトリは `git rev-parse --path-format=absolute --git-dir` で求め、物理パスとして固定する。主 worktree では共有の管理ディレクトリと同じになる
7. 状態ディレクトリのロックを取る(§4 の状態ディレクトリ)。状態ディレクトリが人のチェックアウトの中にあれば止まる(`XDG_STATE_HOME` を外へ向ける)。ロックを取れなければ止まる(二重起動)。ロックの直後に、止めの印と周の途中の印を確かめる(§4 の「次の起動」。8 のデフォルトブランチの固定と 9 の profile の読み取りより前に行う)
8. デフォルトブランチを base-commit.md と同じ順で解決し、ローカルの `refs/heads/<DEF_NAME>` が在る。その sha を固定する(以後の周はこの sha から worktree を作る)
9. profile を読み、`features.loop` と `task_dir` を検査する(§1 の profile)。parent-child でない。PyYAML が要るときに無ければ止まる
10. 実効値を決める(§1)。worktree の置き場(`--worktree-root` か既定)が人のチェックアウトの中にあれば止まる(置き場は `..` と既存の親の symlink を解いた物理パスで比べる。`--repo` が linked worktree のときは、main の worktree とも比べる)
11. ローカルと origin の関係: `origin` があれば `git ls-remote origin refs/heads/<DEF_NAME>`(読み取りだけ。ネットワークの規則)で origin の sha を得る。同じか、ローカルが先行(origin の sha がローカルの祖先)なら続け、先行する commit を報告に列挙する(その commit は無人の周が開く PR に混ざる)。origin に `refs/heads/<DEF_NAME>` が無い・遅れ・分岐・判定できない(origin の sha がローカルに無い)なら止まる(exit 20)。`origin` が無ければ検査せずに報告する
12. ホスト CLI の実在: 実行ファイルを `command -v` で絶対パスに解決する(無い・`/` で始まらなければ理由コード `host-cli-missing`。PATH に `.` などがあると、周でリポジトリの中の同名の実行ファイルが起動されうるため)。補助の CLI と周の子の両方に、その実体の realpath(下の「ホスト CLI の証明」で控えたもの)を使う。周の子の起動に使う `env`・`setsid` も絶対パスに解決する(`/` で始まらなければ `tool-missing`)。続けて、雛形のフラグの 2 段照合(argv とホストの `--help`)。`--host-argv` は先に 5 の許可表で検査済みであり、help の照合によって未知の引数や短い別名を許可へ戻さない。導入済みの同名プラグイン: ホスト CLI のプラグイン一覧(補助の CLI)で有効な `dev-workflow` を探し、在って版が `loop.sh` のプラグインの版と違えば止まる(更新か無効化を案内する)。一覧を解析できなければ止まる(黙って素通りしない)。同じ版なら続けて報告する
   - help の照合は、固定引数と導入版との互換性の診断に使う。上書きした argv では、5 の許可表で先に受理した値付きの長い引数について、help に必須値の表記があれば `=` 形式を追加で確かめる。許可表にない引数や短い別名を受理するためには使わない。
   - **ホスト CLI の証明(#107 H31。#193)**: 実行ファイルを一度も起動しないうちに、`host-check.py identity` で実体(realpath・dev・ino・size・mtime・ctime・内容の sha256)を控える。以後の補助の CLI・周の子・`--prove-host` の確認は、すべてその realpath で起動する。起動の直前に同じ実体かを照合する(補助の CLI と周の子の起動の直前は stat の値〈dev・inode・size・mtime・ctime〉、周を始める前〈周の途中の印を置く前〉と確認の前後は内容の sha256 まで)。違えば起動しない(周を始める前は exit 20・`host-proof` で、周の途中の印を残さない。補助の CLI では `ERROR [host-proof]` を出して、呼んだ検査の理由〈多くは `environment`〉で止まる。どちらも終わりの環境の照合が通らないので、選定中の worktree は「選定中」の lock のまま残して報告する。人が確かめて片付ける)
   - 証明は、人が実 hook の拒否と初期応答の公開 skill 名を確かめた記録。実体(realpath・dev・ino・size・sha256)と起動の形(隔離・権限・hook・固定設定)の組ごとに1ファイルで、置き場は `${XDG_CONFIG_HOME:-$HOME/.config}/dev-workflow/loop/host-proofs/`。形式は version 2 とし、定義の要約(`component_sha256`)・固定設定と名前解決の要約(`policy_sha256`)・名前解決の版・初期応答の形式と公開名を記録する。要約には論理的な出所・配置・リンクの字面と経路・本文を含める。配布コピーの実行ごとの置き場、inode・時刻、状態ファイルの場所は含めない。同一内容の配布コピーでの再実行は再利用できるが、有効な導入先・定義・公開状態の変化は再取得が必要。任意の許可リスト・MCP 設定・上書き引数・ログインの全体を証明するものではない。裸の許可規則の問題(H47)は残る(§10)。
   - 証明が無い・旧形式・必須項目の欠落・形や実体の値の不一致なら、ホスト CLI を起動せずに exit 20(`host-proof`)。ホスト CLI を更新したとき(native の導入〈ホスト CLI を単体の実行ファイルとして入れる導入〉の自動更新を含む)も同じで、人が `--prove-host` で確かめ直すまで使わない。`--prove-host` は打った時点の実体を信頼する(偽の実体は確認を装える)ので、人は先に、更新が正規のものかを確かめる。確かめ方の例: native の導入なら、実体が `~/.local/share/claude/versions/<版>` のファイルそのもので、その版が `--version` の値と同じこと。npm の導入なら、導入元のパッケージと版。報告に出る実体の sha256 を控えておく。`loop.sh` は実体の真正性を確かめない(配布元の checksum などで人が確かめる手段があるかは未確認)
   - 版・help と有効 plugin の一覧を補助 CLI で取得する。一覧から定義を控え直して検査した後、版・help の出力、定義と固定設定の要約を証明と照合する。不一致はセッション前に exit 20(`host-proof`)。初期段階は実体・起動の形と証明の形式を検査し、有効一覧を得る前の値で最終照合を済ませた扱いにしない。毎周の直前にも定義と固定設定を導出し直して照合する。補助 CLI は端末の幅の変数(`COLUMNS`・`LINES`)を外し、同じ固定設定を付けて起動する。
   - **`--prove-host`**: 13 の疎通の後、使い捨てのディレクトリを周の worktree に見立て、同じ監督・固定設定で1回だけ起動する。この確認だけ詳細な初期応答を出す設定を加える。stdout の生データはファイルに置かず、上限4 MiBのパイプ入力として検査し、必要な項目だけを残す。初期応答と結果が各1件あり、`skills` が重複のない文字列の配列で、算出した公開 skill 名と過不足なく一致することが必要。従来の command は権限検査と呼出名には残し、この集合からは外す。さらに、cwd 外の指定先への `Write` について、仲介の記録の拒否・結果の同じ拒否・ファイルの不在を確かめる。保存する応答から account・id・自由な本文などを除く。前後の実体と環境も照合する。欠落・不明形式・不一致・超過・認証失敗・拒否の不成立・時間切れ(既定600秒か周の上限の短い方)は証明を書かず exit 20。成功は証明を書いて exit 0 とし、周を回さない。失敗時に古い証明を自動削除することはない。
   - `--allow-classifier` では、確認の `Write` を分類器が hook を通さずに許した(2.1.289 の実測。§8)。この形では証明を書けないので、`loop.sh` は `--allow-classifier` を §2 の 2 の直後に exit 20(`classifier`)で拒否する(確認が通るかに頼らない。auto を使えない起動では確認が手動のモードで通りうるため)
   - **追加定義と権限の検査(H34・H46)**: 最初の補助 CLI より前と、有効 plugin の一覧を保持した後に、§4 の定義・設定と `allowed-tools` を検査する。前者で分かる異常はホストを起動せず、後者の異常はセッションを起動せず止める。`--dry-run`・`--prove-host` も同じ検査を通る。定義や固定設定の異常は exit 20(`component-policy`)、広い規則や解釈不能は exit 20(`skill-grants`)。
13. 疎通: 最初の周の前に、セッションを起動しない認証の確認(補助の CLI)を打つ。終了コード 0 を「通る」とし、それ以外は止まる(認証切れなどの全体に及ぶ失敗で、ブレーカーが働くまでの周のタスクが除外されないように)
14. `refs/heads/<DEF_NAME>` の固定した sha を控える。共有の git ディレクトリの状態は各周の起動の直前に控える(§3 の 8)。印が無いときの、最後に照合に通った状態との差分は、ここで報告に出す(§4 の「次の起動」)
15. 起動時の報告(§7)。対象の一覧は、最初の周の選定(§3)で作って報告に足す

- 2 の直後に、ホストの読み込みを変える環境変数(`CLAUDE_CODE_SIMPLE`〈hook の自動の読み込みを飛ばし、保存したログイン(OAuth・keychain)を読まない起動に当たる〉・`CLAUDE_CODE_SAFE_MODE`〈hook などを読み込まない安全モード〉)が立っていれば、証明の形の外の起動になるので、ホスト CLI を起動せずに止まる(exit 20・`hooks-disabled`)。起動引数で渡す許可の仲介の hook がこの起動で動くかは未確認(事実と出典は design §7-3)
- その直後に、起動時の環境の `CLAUDE_CODE_SHELL_PREFIX` が非空なら止まる(exit 20・`shell-prefix`)。この値は、hook のコマンドを別のプログラムで包む指定であり、許可の仲介を確かめた起動と形が違う。値は出さず、外してから起動するよう案内する。未設定と空文字は通す。`--dry-run` と `--prove-host` も同じ検査を通し、`--help` は引数の検査で先に終了する。
- 既知の設定入口を変える `CLAUDE_CODE_REMOTE_SETTINGS_PATH`・`CLAUDE_CODE_MANAGED_SETTINGS_PATH`・`CLAUDE_CODE_MOCK_REMOTE_SETTINGS` が起動時に非空なら、exit 20(`host-config-source`)で止まる。
- 道具と配布元を確認した後、ホスト CLI を確認する前に、HOME と設定ディレクトリのパスを検査する。不正なら設定本文と補助のホスト CLI を読まず、exit 20(`host-config-path`)で止まる。既存の止めの印と `inflight` は設定本文を読む前に保全して停止する。印が無ければ利用者環境を控え、最初の補助のホスト CLI より前に §9 の設定を検査する。有効 plugin を控え直した後も同じ検査を行い、その利用者設定から許可リストを作る。通常・発見・`--dry-run`・`--prove-host` は共通の検査を通る。
- 補助の CLI(`--version`・`--help`・プラグイン一覧・認証の確認)は、cwd を状態ディレクトリにして(リポジトリの設定を読ませない)、stdin を `/dev/null`・`setsid`・タイムアウト(`--net-timeout`)で打つ。`--host-argv` で置き換えたときは、置き換え後の実行ファイルで打つ
- 追跡外のタスクの検査(決定録 2026-09-23 の決定 10)は、task_dir の解決に worktree が要るので、最初の周の §3 の 3a で行う(止まるときは worktree を消してから exit 20)。発見モードでは行わない(§11)
- **origin の URL の検査**(両方のモード。10 の後・11 の `ls-remote` より前): 周の前提(unattended-mode.md §1 のタスクの周・discover-mode.md §4)と同じ検査を起動時に行い、食い違う構成で周を失敗させ続けないようにする。どの理由にも URL の字面を出さない(認証情報を含みうる。stderr は状態ディレクトリの `origin-repo.err` に置く)
  - `python3 origin-repo.py --dir=<対象>`(discover-mode.md §3)を打つ。終了コードが 0 でない(使い方の誤り・git の失敗・Python の例外など)か、出力の JSON を読めなければ exit 20(理由コード `origin-url`)
  - origin があって `vcs` が真なら exit 20(`origin-vcs`。`remote.origin.vcs` を外す案内)。11 の `ls-remote` は vcs のヘルパーを通るので、その前に止めて、この案内に届くようにする
  - origin があって `same` が偽なら exit 20(`origin-url-mismatch`。fetch と push の URL を揃える案内)。`loop.sh` の `ls-remote` は fetch 側の URL を見るので、周の push 先と食い違わないように
  - origin が無ければ続ける(周は push しないので、実装モードで PR まで進む周と、発見モードで候補がある周の結末は `縮退`)。検査の結果は起動時の報告に出す(§7)
- `--dry-run` は 12 と許可の仲介の起動時の検査まで行い(12 の照合は `--version`・`--help`・プラグイン一覧だけで、セッションを起動しない。ホスト CLI の証明は要る)、13 を打たずに §3 の 2〜5(2a・3a を含む)で対象の一覧を出し、その worktree を消して終わる。周で起動するホスト CLI の解決後の argv と、周の子に渡す自動メモリ無効化(§4)と §9 の環境変数(周の worktree の分は仮の値)も出す(実走のプローブで使う)

## 3. キューと選定(周ごと)

1. 停止条件のうち周の前に確かめるもの(§6)を確かめる
2. 固定した sha から周の worktree を作る: `git worktree add --detach --lock --reason "dev-workflow-loop: 選定中" <worktree-root>/<実行 ID>-<周の番号> <sha>`(前置き・ネットワークの規則つき。失敗は終了コード 30)
   - `<worktree-root>` の既定は `<対象の親ディレクトリ>/<対象の名前>.loop`。人のチェックアウトの外に作るので、人の `git status` は変わらない。隠しディレクトリにしないのは、snap 版の gh が隠しディレクトリを読めないため
   - 2a. 状態ファイルの前提(実装モードと発見モードで同じ): 周の worktree で `.claude/reviews/x.md`・`.claude/grasp.md`・`.claude/.understand-project-done` を 1 つずつ `git check-ignore -q` で確かめる(`--no-index` を付けない — 付けると、追跡済みのファイルも ignore 済みと答える)。rc 1(ignore されていない。追跡済みも 1)のものがあれば、worktree を消して exit 20(理由コード `state-not-ignored`)。報告に、gitignore の断片(init-project が入れるもの)と、追跡済みなら `git rm --cached` を出す。rc が 0 と 1 以外(symlink の先・サブモジュールの中など、判定できないもの)は数えず、後の段に任せる(symlink は 2b で止まる。サブモジュールは止まらない)。理由: 周の中で書かれた状態ファイル(利用者の設定に UserPromptSubmit の hook があればそれが書く `.understand-project-done`・do-task・update-doc の記録)が未追跡か未 commit の変更として残ると、周の worktree を消せずに残る(§5 の消す手順)。発見モードでは、判定が未追跡・未 commit の無いことを求めるので、正しい周も失敗になる
   - 2b. worktree の `.claude/`・`.claude/reviews/` を §9 の ① のとおり作る(止まるときは終了コード 30・理由コード `reviews-dir`)
3. worktree の中で `resolve-task-dir.py --project-root <worktree> [--task-dir=<値>]` を呼び、task_dir(管理ルート相対)を得る(exit 1・2 は終了コード 30)。検出で決まった task_dir が文字の制限(下)に外れるか、保護パスの下にあれば(例 `.claude/tasks`。保護パスの判定は許可の仲介の hook と同じ列 — §9)、worktree を消して exit 20(理由コード `task-dir`)
   - 3a(最初の周だけ): 人のチェックアウトの同じ相対パスにある `進行中_*.md` のうち、通常ファイル(`[ -f ] && [ ! -L ]`)でヘッダにメタ行があるものが、固定した sha の tree に無いか、固定した sha の blob のヘッダにメタ行が無ければ(メタ行だけ未 commit)、一覧を出し、worktree を消して止まる(exit 20)。検出し直さない。index を書き換えうる `git status` は使わない
4. 候補: task_dir の `進行中_*.md` のうち、次をすべて満たすもの。読み飛ばしたタスクは、理由つきで報告に出す
   - 通常ファイル(symlink でない)。外れたものは理由つきで読み飛ばす
   - ヘッダ(最初の `## ` の見出しより前)に無人実行のメタ行がある(template の照合パターン)
   - タスク MD の相対パスが文字の制限を満たす
   - `refs/heads/task/{名}` も `refs/remotes/*/task/{名}` も無い。`origin` があれば `git ls-remote origin 'refs/heads/task/*'`(周ごとに 1 回。ネットワークの規則。失敗は終了コード 30)にも無い
   - `dev-workflow-loop: <そのタスク MD のパス>` の理由で locked の worktree が無い(残った worktree = 除外の印。決定録 2026-09-23 の決定 9)。同じ理由が複数件あっても全パスを保持し、1 件でも残っていれば読み飛ばす。読み飛ばしの理由には該当する全パスを示す
   - 同じ task_dir に `完了_{名}.md` / `保留_{名}.md` が無い
   - `--only` があれば、その名に含まれる
5. 並び: 作成日(template の作成日の照合パターン)の古い順、同じ日はファイル名順。作成日が無いものは最後に、ファイル名順(C ロケールのバイト順)。作成日が無いタスクは報告に出す
6. 候補が無ければ、worktree を消して(unlock → remove)ループを終える(exit 0。停止の理由は「キューが空」)
7. 先頭の 1 件を選び、worktree の lock の理由を `dev-workflow-loop: <タスク MD の管理ルート相対パス>` に付け替える(unlock → lock)
8. 共有の git ディレクトリと、人の作業場所の管理パス・`config.worktree` の状態を控える(§4 の照合の比べる元。周ごとに取り直す)

**文字の制限**: タスク MD の管理ルート相対パス全体(task_dir + ファイル名)は、空白・制御文字・シェルの記号(`` $ ` ; | & < > ( ) { } [ ] * ? ! ' " \ # ~ ^ % ``)を含まず、どの要素も `-` で始まらないものに限る(プロンプト・lock の理由・ブランチ名に入るため)。タスク名(`{名}`)も `-` で始まらないこと(ファイル名の要素は `進行中_` などの状態名で始まるので、要素の規則だけでは捉えられない)。`task/{名}` は `git check-ref-format --branch` も通ること。task_dir が外れれば起動時に止まり(profile の値は §2 の 9、検出で決まった値は上の 3)、ファイル名が外れれば理由つきで読み飛ばす。

**依存が未完のタスク**: 拾う前に除く前処理は置かない。拾うと ship-task の Phase 2 で保留になる(依存の記法に照合パターンが無い)。

## 4. 1 周

### 実行環境と中断印の信頼境界

起動時の現在の配布元を信頼の起点にし、plugin 全体(ファイル・空ディレクトリの集合、種別、mode、内容 hash)を控える。
`environment-guard.py` は親ディレクトリを含めて nofollow の fd で読み、各階層の同一性を再確認する。
通常ファイル 8 MiB・合計 64 MiB・項目 10,000・走査 15 秒が上限。通常でない項目、読取不能、読取中の変更は停止する。
大きな配布物も無制限に読み込まず、上限を超えた理由を示して止める。
Git 設定の解析は 1 呼出 3 秒、展開後の stdout も取得中に 8 MiB で打ち切り、stderr は保持しない。
例外の引数・設定値・本文は診断へ出さず、エラーの種類で報告する。

毎回新しいコピーを作り、元と同じ相対配置を保つ。過去の状態ディレクトリの helper は一切実行しない。
親が保持した SHA-256 と開いたコピーの bytes を照合し、その bytes をメモリ上で実行する。
照合器が改変された状態で自己検査する経路を避ける。各 helper の使用直前、子の起動直前、周の判定前、network Git の前に確認する。
ネットワークへ出る §2 の 11 は、有効 plugin を確定する 12・認証確認の 13 の後に実行する。
ホストへ渡す plugin と許可の仲介もコピーを使う。コピー作成後に元 helper を import/source しない。
権限判定 hook の command には固定 loader と保持 hash を埋め込み、各要求で照合した guard の bytes を実行する。
権限判定器も照合後に読んだ bytes の hash を確かめて実行し、検査失敗は明示的な deny を返す。

監視対象は利用者の host 設定、有効 plugin の一覧と実体・選択された marketplace 項目、利用者と Linux 管理側の skills/commands/agents、管理者設定とその設定ディレクトリ、
global/XDG/system の Git 設定と全 include/includeIf 先、既知の shell 設定、明示した MCP 設定。
Git の引用・継続行は Git 自身で解析し、include は自動で開かせず先に通常ファイルとして調べる。未成立の条件の include 先も控える。
利用者・Linux 管理側の skills 直下のディレクトリへの導入リンクは、字面・途中の親とリンク・最終ディレクトリの実体を保持して辿る。
絶対・相対・多段・途中のディレクトリリンクを順に解決し、最大40回を超えるリンクと循環を拒否する。
再照合では保持した実体を、新しい対象のディレクトリや本文を開く前に比べ、読取後にも照合する。
commands・agents・配布コピーの内部リンクは許さない。設定・Git include・shell ファイルの通常リンクは各 reader の既存条件で扱う。
存在しない既定設定と include 先も「無し」として保持し、追加を検出する。通常完走の後の新しい起動は、その時点を新たな信頼開始にするため、人の正常な設定更新を受け付ける。
利用者設定に加え、設定ディレクトリの `remote-settings.json` も控える。設定の共通 reader は、控えた内容 hash・mode・不在と関係する最終ファイルリンクを照合し、照合した同じ bytes だけを解析する。設定本文は状態と診断へ保存しない。単独の利用者設定・管理設定本体・cache の保持済み最終ファイルリンクは、リンクの連鎖と対象が同じときに使える。親ディレクトリのリンクと管理 drop-in tree 内のリンクは拒否する。

既知 shell の対象は HOME の profile/bash/zsh 起動ファイル、BASH_ENV/ENV/ZDOTDIR の指定先と system の起動設定。
そこから呼ぶ任意の外部 script、credential helper の実体、PATH の全実行ファイル、OS/Python 標準ライブラリはこの集合に含めない。
起動時から改変されている配布物も判定できない。信頼できる配布元・実行環境で開始する。

検査用コピーの起動は、親と3つの直接入口が保持する同じ固定本文で行う。 固定検査と hook の組立は Python の隔離起動(`-I`)で cwd・PYTHONPATH・利用者 site を探索しない。profile 読取など他の補助処理は従来どおり利用者 site の PyYAML を使える。親ディレクトリと末尾を nofollow で調べ、通常ファイルを1 MiB・15秒以内で読み、外部保持hashに一致したバイト列だけを実行する。未検査の参照文書や検査用コピーから、この固定本文を取得し直さない。

**同じ UID の限界**: コピーや控えだけの持続的な変更は外部保持 hash で拒否する。
検査の間だけ変えて元へ戻す操作は、内容と mode が戻れば検出できない(回帰で実測)。
コピーと控えと呼出元の保持値まで取り替えた場合、新しい信頼開始と区別できない(回帰で実測)。
親のメモリ・実行中の実体への攻撃、検査後から helper 使用までの差替えは、この UID 内で完全には防げない。
影響は誤った検査結果や未検査コードの実行。乱数名・0600・コピーは隔離ではない。
高い保証が必要なら、親/監査を別 UID に置き、OS の実行・書込隔離と外部監査を組み合わせる。

`inflight/` の meta・base・checksum は同じ状態領域にある未信頼入力である。次の起動はそれだけを根拠に process へ signal を送らず、worktree/ref を変更・削除もしない。印と成果物を保全して停止し、人が所有者と残ったプロセスを確認する。正常に完走した現実行は親が保持した PID/PGID だけを片付けるため、この停止規則で後片付けを緩めない。

終了時も、片付けや報告の Git を呼ぶ前に、親が保持した利用者環境を再照合する。拒否した場合は Git を再実行せず、保持した作業場所だけを報告する。通常終了の直前に変化を検出した場合も終了コード 20 で停止する。

Permission hook の permlog は報告用で、制御判断の根拠にしない。G1 は deny の kind、行の欠落・順序・重複を問わず同じ連続回数として数え、通常の連続失敗と厳しい方で停止する。

**起動**

- 毎周の直前に定義と固定設定を再導出し、起動時の要約と証明へ照合する。通れば worktree の中で、既定表の雛形でホスト CLI を `setsid` で起動する(1 周 = 新しいセッション)
- プロンプトは stdin で渡す: `/dev-workflow:ship-task --task=<タスク MD の管理ルート相対パス> --unattended`(許可リストのフラグが可変長の値を取るので、位置引数のプロンプトが食われうるため)
- 子の環境: `DEV_WORKFLOW_HOST_CLI` と `OLDPWD` を外す(`OLDPWD` は直前の cd で worktree の外を指し、周の中の `cd -` の行き先になるため)。周の印 `DEV_WORKFLOW_LOOP_ITER=<実行 ID>-<周の番号>` と、許可の仲介の 4 つの環境変数(§9)を付ける。ロックの fd を閉じる
- 自動メモリ: 実装・発見の両モードで、子の環境に `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` を必ず付ける。親の値が未設定・`0`・`1` のどれでも子は `1` になる。親環境・補助の CLI・利用者の設定ファイル・既存メモリは変更しない。`--dry-run` にもこの子の値を表示する。これはホストの自動メモリ機能を止める設定で、任意のファイル操作を禁じる仕組みではない(§10)。公式仕様と実測の区別は design §7-3。
- argv の最後の方に、許可の仲介の hook と導出した固定設定を合成して付ける(§9。並びは §2 の 5、フラグは既定表)。既存の仲介 hook と厳密な MCP 設定の起動は維持する
- stdout・stderr は状態ディレクトリの周のファイルへ向ける(パイプにしない。生き残りのプロセスがパイプを握ると、読む側が終わらないため)
- 周の上限(実効値)を過ぎたら片付けて、結末を「時間切れ」にする(ship-task は期限を知らない。許可待ちで固まった周もこれで打ち切られる — 決定録 2026-09-23 の決定 18)

**権限**(フラグは既定表)

- 既定は、編集の自動許可 + 確認は自動で拒否 + 利用者の許可リスト(ホストの利用者設定と `--allowed-tools`)
- 全許可のモードは使わない。全許可のフラグは、ホスト CLI へ渡す argv を走査して拒否する(起動時に止まる — §2 の 5)
- 分類器による自動承認(`--allow-classifier`)は使わない。指定すると起動時に止まる(§2 の 12・#107 の H48)
- 許可リストはホストの利用者設定か起動引数だけで受け付け、profile では受け付けない
- 保護パス(`.claude/`・`.git` など)への書き込みは、編集の自動許可でも許可リストでも通らず確認に回り、確認は自動で拒否になる。周の中の skill が状態ファイルに書く分だけを、許可の仲介(§9)で通す
- 推奨: 許可リストはコマンド単位で列挙する。Bash の許可を、ship-task の無人の周が使うコマンド(git・gh・python3・bash〈同梱スクリプト〉・判定と照合〈test・echo・sha256sum。unattended-mode.md の「許可の仲介の構文」。echo は真偽と終了コードを出力に出すため〉・読み取り系〈grep・sed・awk・find・wc・ls・cat・head・tail・sort・diff など〉・対象の品質ゲートのコマンド)に限って列挙する。周の報告に `G1`(許可の拒否)の保留が出たら足す。値の書式と実走で使った値は design §7-3
- 推奨: ホストによっては、検索のツール(Glob・Grep)が既定で無く、周の検索が Bash の呼び出しになって許可の仲介に届く。そのホストでは、`loop.sh` の `--allowed-tools`(起動引数。利用者の設定の allow では戻らない)にそのツールを名指す値を足すと、周と委託(作業を別の AI に任せること)したサブエージェントに戻る(足す値と実測は design §7-3)。読むだけの調査を Bash で打たずに済み、書き方の誤りによる拒否(G1)を減らす(unattended-mode.md の「読むだけの調査はツールを先に使う」)。足す値は design §7-3 の範囲を付けた形(名指しのため。公式文書の記載から、許可を広げないと推測する。実測はしていない〈#107 の H47 の残り〉。worktree の中の読み取りは承認が要らず、プラグインルートの下の読み取りは許可の仲介が通す — §9)。道具の名だけの規則(`Grep` など)は足さない。`Grep` では、cwd の外の読み取りも許可の仲介を通さずに通った(#193 の実ホスト。2.1.289。ほかの道具は未確認だが、同じ型とみなす。#107 の H47: 道具の名だけの許可規則による読み取りの拡大)

**隔離**

- リポジトリ側のホスト設定(プロジェクト・ローカルの設定〈許可規則・hook など〉と MCP 宣言)を読み込ませない起動を、既定表の既定にする(決定録 2026-09-23 の決定 16)。読ませない手段が無いホストでは無人ループを起動しない(既定表に載せない)
- この起動では利用者の側の MCP サーバーも外れる。周で MCP を使うなら `--mcp-config <ファイル>` で渡す(利用者の側の値。profile では受け付けない)。渡さなければ周では MCP が使えず、そのことを報告に出す(限界 — §10)
- リポジトリ側の skill・エージェント定義・コマンドも、リポジトリ側のホスト設定に含める。この起動で読まれないことは実走のプローブで確かめる(§8)。読まれると分かったら、デフォルトブランチの tree にそれらを持つリポジトリでは起動時に止まる検査を `loop.sh` に足し、再レビューしてから merge する(限界として受け入れない)
- CLAUDE.md・AGENTS.md・`.claude/rules/` は指示であって許可ではないので、この対象に含めない(読まれるかを記録するだけ)

**skill・command・agent の権限の追加**(#107 H34・H46。#193・#231)

- 公式文書は、skill の frontmatter の `allowed-tools` を「その skill を呼んだターンの間、確認なしで使える道具を足す」とする。command のファイルも同じ鍵を持てる
  - ヘッドレスの周は全体が 1 ターン。公式文書の記載からは、周のモデルがその skill を呼ぶと、周の残りで許可リストと許可の仲介の外へ許可が広がりうる
  - リポジトリ側の skill はこの起動で読まれない(上の隔離)。利用者の側の skill・command と、利用者の設定で有効な plugin は読まれる
- 実ホストでの確認(2.1.289。§8・design §7-3)
  - 利用者の skill の `allowed-tools` は、その skill をスラッシュコマンドで呼んだターンで、skill が指示したコマンドを許可の仲介の hook を通さずに通した。`Bash(python3:*)` と字面どおりの規則では `python3` のコマンドが、`Bash` では `touch` が通った
  - cwd の外への `touch` は、prefix・字面どおりの規則では通らず hook に届き、`Bash` の全体なら通った。許可がどの呼び出しで効くかはコマンドで分かれ、分けて確かめきれていない(このため、許可リストより広い規則は効くものとみなして止める)
  - `allowed-tools` を持つ skill を Skill の道具で呼ぶと、その呼び出し自体が確認に回った(持たない skill は回らない)。許可されると、同じく効いた
  - 今の許可の仲介は Skill の道具を「扱わないツール」で拒否する。このため、周のモデルが呼ぶ経路では効かなかった
  - この検査は、許可リストや仲介の判定が変わっても権限が広がらないようにする二重の守り
- 起動時(§2 の 12。`--dry-run`・`--prove-host` を含む)に、`host-check.py grants` が次を調べる。環境の控え(§4 の信頼境界)と同じバイトだけを読む
  - 対象の置き場: 配布 plugin のコピー、有効 plugin の導入先、利用者と Linux 管理側の `skills/`・`commands/`・`agents/`。有効 plugin の選択された marketplace 項目とその source も含む。無効 plugin と未選択の項目の全ツリーを無差別には読まない
  - 対象のファイル: `.md`(大文字小文字を区別しない)と、plugin のマニフェスト(`.claude-plugin/plugin.json`)
  - マニフェストと選択された marketplace 項目: `commands` の対応表の `allowedTools`・インラインの `content`・参照先の `source` も同じ判定に掛ける。両方に定義があれば、後勝ちを推測して検査を省かず、両方を調べる。`commands`・`skills`・`agents` が保持した root 外や未保持の本文を指す形、型や読み方を確定できない形は止める
- 実効許可リスト(包含の判定の根拠にする許可リスト)
  - `--allowed-tools` の値と、利用者の設定の `permissions.allow`(控えた内容と照らしてから読む)
  - `--allowed-tools` の値は、ホストと同じく ' ' と ',' で区切る。タブ・改行・入れ子の括弧で区切り方が分かれる値は止める
  - 設定の規則は字面のまま使う。前後に空白がある規則は、ホストが整えずに読む(2.1.289 を静的に読んだ。実ホストでは未確認)ので、何も許さないとみなして根拠にしない
- 包含の判定: skill の各規則が、実効許可リストの規則と同値か狭いときだけ通す
  - 道具の名だけの規則は、同じ名だけの規則にだけ含める。指定つきの規則(Bash 以外)は、同じ道具の名だけの規則か、字面が同じ規則に含める
  - Bash の規則には、字面どおりに一致する規則(exact)と、頭の語に一致する規則(prefix。`:*` と末尾の ` *` は同値)がある。exact は同じ exact にだけ含める(prefix は exec の包み・`find` の `-exec`/`-delete` を承認しないが、exact は承認できるため)。prefix は、同じ prefix か、語の単位で長い prefix だけを含める
  - 許可リストに Bash の全体(`Bash`)があれば、Bash の exact・prefix の規則は新しい許可にならないので通す(分類器の起動を除く)
  - `--allow-classifier` の起動は今は拒否する(§2 の 12)。包含の判定では、ホストが広い規則を外すので、許可リストのシェルの規則(exact を含む)と `Monitor` を根拠にしない
- 止める形
  - Bash の全体(`Bash`・`Bash(*)`)。許可リストに同じものがあっても止める
  - 未知の道具(公式文書の組み込みの道具と `mcp__<server>__<tool>` の形のほか)。委託の道具(サブエージェントを束ねる道具とその返答の道具を含む)も、未知の道具と同じく止める
  - 解釈できない形: シェルの記号・引用・`$`・途中の `*`・空白の崩れ・先頭の語が包み(`timeout`・`watch`・`find`・`xargs`・`env` など)か代入のもの・頭が入力の欄の名のもの(`Bash(description:*)` など)・括弧が 2 つ以上あるもの・単独の `/` で始まる path(定義した場所で起点が変わる)
  - 同じ規則の重複
- frontmatter(ファイルの先頭の `---` で挟んだ設定)の読み方
  - 最初の空でない行が `---` のファイルだけを、行の形で読む。先頭の BOM(ファイルの先頭の印の文字)・行末の CR・後ろの空白とタブは許す。終わりは次の `---` の行
  - ホストの実行ファイルから読んだ 2 通りの切り出し方(開きの後ろに任意の空白文字を許して行の途中の `---` で閉じる形と、閉じの前に改行を要する形)でも範囲を求める。行の形と食い違い、鍵の字面かエスケープ(`\`)があれば止める
  - H46 の追加定義の reader が UTF-8 でない本文を先に拒否する。許可規則だけの読み方の置換に頼って通さない
  - 鍵の字面は、`-`・`_` を除いた小文字の変種(`allowed_tools`・`AllowedTools`・`allowed--tools` など)も拾う。最上位の `allowed-tools` の鍵のほかに鍵の字面があれば、説明の文章の中でも止める(入れ子・フロー・最上位の字下げで読み方が分かれうるため)
  - `allowed-tools` の値は、1 行の値(空白かカンマ区切り。括弧の中は区切らない)・1 行の角括弧の列・字下げの揃った `- ` の列だけを読む。ホストの区切り方(静的に読んだ。括弧の中かどうかを真偽の 1 つで持つ)と結果が違う値は止める
  - 鍵の字面かエスケープのある frontmatter では、単独の CR・YAML 1.1 の改行(U+0085・U+2028・U+2029)・タブ・改ページがどこにあっても止める(タブか単独の CR で始まる行は、行の形では入れ子として読み飛ばすが、ホストの YAML は最上位の鍵として読みうる)。`allowed-tools` を持つ frontmatter では、注釈の行も止める
  - 次も止める: `allowed-tools` の値の `|`・`>` で始まる複数行の値・複数行にまたがる値・注釈つきの値・空白以外の空白文字と、`allowed-tools` の鍵の重複。鍵の字面かエスケープがある frontmatter では、引用やエスケープで組み立てうる鍵。鍵の字面がある frontmatter では、閉じない frontmatter
  - ここで説明した許可規則の parser は、ほかの鍵の値を許可規則として読まない。hook・agent の設定・公開状態などは、下記 H46 の別の厳しい reader で必ず調べる。こちらは PyYAML の有無に依存しない
  - PyYAML が使えれば、鍵かエスケープのある frontmatter を YAML としても読む。`allowed-tools` の規則が食い違えば止める。PyYAML が無ければ、この照合は行わない(報告に「YAML の照合: 無」と出す)
    - YAML として読めないとき: 鍵の字面があれば止める。鍵の字面が無ければ、ホストの読み直し(静的に読んだ。最上位の素の鍵の値を引用し直し、先頭のタブを空白にする)を写して読み直し、それでも読めなければ止める
    - PyYAML で読めることと、ホストの YAML で読めることは一致しない。読み方が分かれうる形は、上の行の形の規則で先に止める
- 利用者・Linux 管理側の `skills/` 直下のディレクトリへの導入リンクだけを例外として辿る。絶対・相対・多段・途中のディレクトリリンクは、`..` も順に解決し、親・リンクの位置と字面・最終対象を保持する。リンクは最大40回。循環・dangling・特殊ファイル・通常の内部リンクは拒否する
  - root や項目の真の不在も保持する。新設・親やリンクや対象の差替えは、新しい対象のディレクトリや本文を開く前に止める。本文は同じ fd で読み、前後で実体と内容を照合する。検査後に別の方法で開き直した本文は使わない
  - 同じ実体への複数の導入は読取りを共有し、論理的な出所はそれぞれ残す。累積上限は1ファイル8 MiB、全体64 MiB、10,000項目、15秒とし、一覧追加の走査でもリセットしない
  - `setup.sh --agents-global` の `~/.agents/skills` は、ホストが読まない置き場なので検査しない
- 止まったら(報告は出所の種類と固定した理由を示し、元のパス・command 名・規則・本文は出さない)
  - 許可リストより広いだけの規則: その plugin・skill を無効にするか、ループ用の設定ディレクトリ(`CLAUDE_CONFIG_DIR`)で起動する。または、同じ規則を `--allowed-tools` か利用者の設定の `permissions.allow` に足す(権限を広げるかは人が決める)
  - Bash の全体・未知の道具・解釈できない形・重複: 許可リストに足しても止まる。その skill を直すか無効にするか、ループ用の設定ディレクトリで起動する

**追加定義・固定設定と公開名**(#107 H46。#231)

- 照合済みの記述子から得た同じ本文だけを使う。H46 の検査に必要な記述子のない旧 state は拒否する。通常の環境照合の旧 state 互換とは分ける
- marketplace の registry と有効一覧から、選択された項目・source・導入先を一意に結ぶ。欠落・複数候補・不一致は推測せず止める。同期由来の `scope=synced` と marketplace のないローカル plugin は、それぞれ明示された条件で扱う。`@skills-dir` は保持済みの利用者・管理側 skills の直下の配置と manifest 名が一致するときだけ同じ定義へ統合する。未知の `@` 出所をこの例外で通さない
- skill・command・利用者と管理側の agent の非空の hook、plugin の非空の hook と `modules` は拒否する。空の容器は許すが、型や参照を確定できなければ止める。plugin hook の参照先も同じ保持 root 内に限る
- agent は、利用者・管理側の非空 MCP 宣言、`auto`・`bypassPermissions`・未知の権限モード、解決できない skill preload を拒否する。受け入れるモードは `default`・`manual`・`acceptEdits`・`plan`・`dontAsk`。plugin agent でホストが無視する項目の例外は、agent 専用と確認できた本文だけに適用し、同じ本文が skill・command としても読まれる場合には適用しない
- plugin MCP の宣言と標準の設定ファイルは保持した同じ root 内で調べる。外部・未保持・循環・重複・不明型の参照は止める。宣言を検査できても、その MCP の起動許可にはしない。周の厳密な MCP 設定を維持する
- 名前は、読み込む定義の対応表、呼出名と別名の解決先、利用者とモデルから呼べる名、初期応答に出る公開 skill 名に分ける。plugin の prefix、skill と command、管理側と利用者側の優先順位を解決する。出所・本文・公開条件が一致する重複だけを統合し、曖昧な衝突は止める。非公開の定義も権限検査と名前の占有から除かない。既存の `skillOverrides: off` を保持する
- `disableBundledSkills: true`、`syncClaudeAiSkills: false`、`syncClaudeAiPlugins: false` を固定する。値は真偽値に限り、`0`・`1`・文字列で代用しない。利用者設定・管理設定・cache の各保持元に競合値があれば拒否する
- 既知の組込み名 `doctor`・`design`・`plugin-authoring`・`checkup` は、同名の外部定義が名前の解決で優先される場合を除き、`skillOverrides` で off にする。`checkup` は有効な別名の一致も扱い、他の3名は主名で判定する。非公開の外部定義も判定から落とさず、正当な同名定義を一律に無効化しない。ホストの観察事実と未知の同名組込みを見分けられない限界は design §7-3 と §10

**片付け**(時間切れのとき・周が終わったとき・`loop.sh` がシグナルを受けたとき。判定より前に必ず行う)

1. `loop-supervisor.py` が Linux subreaper(孤児になった子孫を引き取る監督プロセス)になってから、ホストを新しいセッションで起動する。通常終了・時間切れ・TERM/INT/HUP・親の終了通知で、子孫の回収を行う。
2. 監督は自分の直接の子だけを対象に、親 PID と開始時刻を pidfd(プロセスの実体を指す保持参照)の取得前後で照合して TERM/KILL を送る。子の終了で引き取った孫も同じ手順で回収する。環境の印・セッション・dumpable に依存しない。
3. 親の loop も起動直後の監督の開始時刻を保持する。停止時は親 PID と開始時刻を照合し、pidfd に送る。整数 PID だけで signal を送らない。別 PID 実体への再利用と読取拒否を区別し、照合不能なら成功扱いにしない。
4. 監督が `waitpid(-1)` の ECHILD(所有する子が残っていない)を確認してから結果を書く。loop は起動した監督の exit 0 と保持した nonce に合う結果の両方を要求する。高速で正常終了した監督は Bash の終了結果と記録で確認する。読取不能・強制終了・期限超過では、不在を推測せず止める。
5. 回収対象への TERM の後、`--kill-grace` の猶予を経て KILL を送る。回収の上限を超えても判定や次の周へ進めない。selftest の `DEV_WORKFLOW_LOOP_TEST_LEFTOVER` は非空なら「残った」へ倒すだけで、安全側の停止を外さない。

**残ったときの終わり方**(呼び出し側ごと)。どれも照合(下の「共有の状態の照合」)はしない — 残ったプロセスがまだ書き換えうるため。止めの印の理由に「残ったプロセス」と確認できない理由を書き、周の途中の印と worktree を残す(残ったプロセスと次の周を並行させず、周の識別子の記録を上書きさせないため)。

- ループの最中(周の終わり・時間切れ): 判定(§5)と次の周へ進まず、止めの印を置いて exit 10
- シグナル(TERM・HUP・INT): 止めの印を置いて 128+シグナル番号
- EXIT の trap(内部の失敗): 止めの印を置いて 10
- 次の起動(下の「次の起動」): 止めの印を置いて(既にあればそのまま)exit 20

**シグナルと内部の失敗**

- `loop.sh` 自身が TERM・HUP・INT を受けたら、子を片付け、共有の状態と `refs/heads/<DEF_NAME>` を照合して差分を報告に書き(差分があれば止めの印を置く)、worktree を残して 128+シグナル番号で終わる
- 内部の失敗(`set -e` の ERR など)で周の途中に終わるときも、EXIT の trap で、子が残っていれば同じ手順(片付け → 照合 → 印 → 報告)を行ってから終わる。終了コードは、照合で差分を見つけて止めの印を置いたとき・片付けで残りがあったときは 10、それ以外の内部の失敗は 30 に揃える(失敗したコマンドの終了コードをそのまま返さない)

**共有の状態の照合**(片付けの直後、残りが無いとき。`loop.sh` の次の git〈§5 の `ls-remote` を含む〉より前)

状態観察は共有の全 ref(参照先 OID と symref の指す先。未解決の loose symref も含む)と、各 linked worktree の private `HEAD`・private `refs/`、全 worktree(パス・HEAD・lock の理由)、開始時点にある各 worktree の index 生バイトと追跡・未追跡・ignored を含むファイル木を控える。新しく作った当該周の worktree だけは内容走査から外すが、他の既存 worktree は外さない。通常ファイルは `lstat`→`O_NOFOLLOW|O_NONBLOCK` の open→同一 fd の hash→`fstat` で観察し、symlink は字面、directory は構造、FIFO/socket/device は種別・mode・device 番号だけを記録する。祖先 directory は解決用の inode・型・mode だけを照合するため、無関係な sibling の追加による時刻変化は停止理由にしない。`secret_paths` は、worktree の物理 path ごとに開始時から保持した glob と現在安全に読んだ glob の和集合を使う。正常完走した別実行の間に人の worktree を移動したときは、その checkout 固有の Git 管理ディレクトリで和集合を引き継ぐ。周の途中で当該周以外の人の worktree が追加・移動したときは、profile や本文を読む前に停止する。後で profile から削除・縮小しても既に機密として扱った path を hash しない。内容を開かず path・種別・metadata だけにし、機密ディレクトリの子も列挙するため追加・削除・型・size・時刻の変化は検出する。通常ファイルを同じ ctime まで保ったまま変えられるとは主張しない。限界は、機密 symlink の**作業ツリー外**の target の内容だけを変え、symlink 自体の字面・metadata が変わらない場合である。target は開かないので検出しない。機密は別の保護領域で管理する。

既定の上限は 100000 項目、通常ファイル合計 1 GiB、1 件 64 MiB、壁時計 60 秒である。Git の状態出力は保持する前に同じ合計上限で drain し、ディレクトリ名は項目上限を消費してから保持する。上限、到達する `include.path` と**有効な** `includeIf` の欠落・循環・読取不能・FIFO、または lstat/open/read の間の置換は検査不能として止まる。無効な `includeIf` の欠落は機械固有の通常設定として開かない。利用者だけが `loop.sh` の `--state-max-items`、`--state-max-bytes`、`--state-max-file-bytes`、`--state-max-seconds` で上限を広げられ、profile と子プロセスからは変えられない。旧形式の inflight は必須欄を現状で補完せず停止する。

- profile と設定 origin は、親ディレクトリを fd で保持してから `lstat`・`open`・`fstat` を行う。親ディレクトリの差替え、通常ファイル以外、読取中の変更は内容を採用せず停止する。設定 origin の**leaf symlink**は初回と正常完走した別実行の新しい基準では許し、link の字面・metadata と通常ファイル target の実体・digest を記録する。同じ実行中または inflight 再開の後観測では、実効設定の include target を Git が follow する呼出しより前に、保持済み origin graph を **各既存 worktree の文脈**で fd 経由で検査し、worktree の `.git` と admin の `commondir` も保持した共有管理パスへ照合してから Git を呼ぶ。全保持 origin と、common・各 worktree の `config.worktree`、利用者/system の候補 root の不在を含む候補集合を、parser を起動しない第1段階で完全一致させる。存在する候補は link 種別・字面・metadata・本文 digest を照合し、不在だった候補の新設は本文を開かず停止する。第2段階の `--file --no-includes` parser は repository 外と利用者/system config を外した環境で保持 fd だけを読む。保持済み通常 source に新しい include が加わった場合も、その target を開く前に停止する。基準に無い origin は通常ファイルも link も開かない。link の差替え・新設と、保持済み origin の本文・metadata の変更は停止し、Git が新しい設定層を有効にする前に比較不能として扱う。正常完走した別実行の新しい基準では、同じ target の本文変更を hash の差分として記録する。設定はまず `--no-includes` で宣言だけを安全に解析する。`includeIf` の有効性は、引用値・`gitdir`/`onbranch`/`hasconfig` を独自実装せず、到達先を持たない marker 設定を Git 自身に評価させる。`onbranch` は保持した HEAD と共通 ref の symbolic-ref 連鎖を no-follow で解決した**最終 branch**を写した隔離 Git dir、`hasconfig` は保持済み remote URL と repository 外の marker で評価し、未検査 target を条件評価に読ませない。全通常 source と有効な非 `hasconfig` graph の remote URL を先に集めてから `hasconfig` を反復評価し、全有効到達先を no-follow で確定した後にだけ実効設定を読む。同じ inode の origin でも字面と親 directory の文脈を別 node として保持し、循環は同じ inode と親 directory の組だけで判定する。各 worktree で Git が解決した実効設定の digest と、origin の種別・metadata・通常ファイル digest を両方比べる。
- state JSON も外部から書き換えられうる入力として扱う。比較時は parent fd から通常ファイルだけを `O_NOFOLLOW|O_NONBLOCK` で有界に読み、FIFO・symlink・不正 JSON・旧 schema は比較不能として停止する。出力は予測可能な `.tmp` 名を開かず、同じ parent fd 内の排他的な一時ファイルを fsync 後に置換する。
- 許す差は、当該周の worktree が保持した path と lock 理由のまま、`refs/heads/task/{名}` と HEAD が同一の commit へ進むこと、その commit と同じ OID の `refs/remotes/origin/task/{名}` が新設または進むことだけである。commit に伴う当該 worktree の clean index の生バイト変更もこの条件に限り許す。child が detached のまま commit したときは、その当該 worktree の限定差だけを判定器へ渡すが、正常の条件を満たせないので失敗として残す。他の ref、symref、worktree、index、ファイル、設定の差は名前の接頭辞だけで許さない。

- 共有の git ディレクトリ(`git rev-parse --git-common-dir`)を、その周の起動の直前の控え(§3 の 8)と比べる
  - `config`: `git config --file <パス> --no-includes --list -z` の項目の並びで比べ、どんな変化も許さない。周の skill は共有の `config` に書かない(無人の push は `-u` を付けず、作業ブランチは `--no-track` で作る — unattended-mode.md §7)
  - `config.worktree`: 同じく項目の並びで比べ、どんな変化も許さない
  - `hooks` の全木と `info/exclude`・`info/attributes`・`info/sparse-checkout`・`info/grafts`: 通常ファイル・symlink・特殊ファイル・不在を含めて控え、どんな変化も許さない
  - `hooks/`: ファイルの種類・モード・symlink の行き先・中身を比べる(実行権を付けるだけの変更も捉える)
  - `info/`: `exclude`・`attributes`・`sparse-checkout`・`grafts` だけをバイト列で比べる(`git gc` が作る `info/refs` などは比べない)
  - その周の worktree の `config.worktree`: 同じく項目の並びで比べ、どんな変化も許さない
- 親の Git 操作には `--no-optional-locks` を付け、判定や worktree 削除の確認に伴う不要な index 更新を抑える。必要な worktree 作成・lock・unlock・削除は実行し、未追跡ファイルがある正常な周は削除を強制せず残して次の周へ進む。index の比較を緩めず、人の index の変更は引き続き検出する。
- 人の作業場所の管理パス(`repo:git-dir`)と、その下の `config.worktree`(`repo:config.worktree`)も控える。設定は同じく項目の並びで比べ、ファイルの追加・削除・値変更・項目の並べ替えを検出する
  - `extensions.worktreeConfig` の有効・無効にかかわらず、不在も記録する。主 worktree も同じ形式にし、共有側との重複を許す
  - 管理パスは起動時に固定した物理パスを使う。実行用 worktree の設定とは分けて保持する
  - `extensions.worktreeConfig` により親が当該周の新しい管理領域へ複製する設定は、保持した親と正確な管理パスに結び付くときだけ許す。親が正常に消した当該領域の設定だけは次の周の保持集合から外す。未知の設定・link・別 worktree の追加・削除は許さない
  - 利用者の設定は自動復元・削除しない
- 設定の保持構造に差があれば、新しい値・新しい include 先を読まずに理由を分類して止める。通常の周では差分と証拠を残して exit 10、中断後の照合では止めの印を残して exit 20 とし、検査不能を成功や内部の失敗へ読み替えない
- `refs/heads/<DEF_NAME>` が起動時に固定した sha のままか
- 変わっていたら、差分を報告し、止めの印を置いて、§5 のネットワークの git を打たずに止まる(exit 10。§6)
- この状態 snapshot では、周が共有の `config` に書くと分かったら許しを足さず、書かない手順に skill を直す。これは共有状態を観察する規則であり、子の commit・push の hook を放置する意味ではない。#190 の共通 safe Git 前置きは [loop.sh の `GIT_PRE`](../scripts/loop.sh)・[unattended-mode.md の手順](unattended-mode.md)・[publish-guard.py の `SAFE_GIT_PREFIX`](../scripts/publish-guard.py) で `core.hooksPath=/dev/null` を渡して hook を無効化する。品質は hook ではなく format ゲートで確認する(#107 H7)

**状態ディレクトリ**

- 場所: `${XDG_STATE_HOME:-$HOME/.local/state}/dev-workflow/loop/<識別子>/`(人のチェックアウトの外)。識別子は起動前 helper が nofollow で得た common 管理パスの物理値のハッシュであり、通常 Git を起動する前にも同じ場所を比較できる(同じリポジトリの別の worktree から起動しても同じ場所・同じロックになる)。入力 repo の**絶対 path**に結び付く binding は、この子ディレクトリではなく `STATE_BASE/.repo-bind-<その path の sha256>.json` に置く。symlink の別名は別のキーで、物理 TOP は binding の別の保持値である
- ロック(`flock`)で二重起動を防ぐ。子にはロックの fd を渡さない
- 置くもの
  - 入力 repo の絶対 path と admin/common の対応を持つ binding。binding は cold 検査と状態配置の確認後にだけ `STATE_BASE` 直下へ排他的に作り、次の起動では比較専用に使う。現在の `.git` や `commondir` が差し替わって別の状態を選ぶ経路を許さない
  - 実行ごとの報告 `<実行 ID>/report.md` と、同じ `<実行 ID>/` の下の周のログ・`.claude/reviews` の写し(§5)
  - 最後に照合に通った状態(期待した状態)`last-verified.json`。人の管理パスと設定も含む。周の照合に通った after-state の生バイトと sha256 を親が保持し、通常終了・TERM などの後でも新たな snapshot を基準にしない。親が実際に削除できた当該周の own worktree だけは、`worktrees`・`worktree_contents`・`worktree_configs`・`secret_patterns` の四集合からその絶対 path の1件ずつを派生状態へ外す。派生状態と最終観察を self-ref 例外なしで厳密比較し、比較中の保持・派生・最終観察の hash 変化も拒否する。成功した最終観察の生バイトだけを atomic に昇格し、食い違いを見つけた状態は残さない。未信頼の `inflight` を安全に再開する根拠には使わず、次の起動では環境 bootstrap 前に中断印を保全して拒否する
  - 周の途中の印 `inflight/`(ディレクトリ): 周の起動の直前(§3 の 8 の後)に置く。中身は、周の識別子(`DEV_WORKFLOW_LOOP_ITER` の値)・タスクの {名}・§3 の 8 で控えた状態(人の管理パスと設定を含む)・その実行が固定したデフォルトブランチの名前と sha。**基準昇格段階**へ入った後は、上の状態昇格まで成功したときだけ消す。保持・派生・最終観察のいずれかが照合不能か書換えられたときは、`last-verified.json` を上書きせず、この印と証拠を残す。これより前の通常の周内照合で共有状態の差分(比較元の改変を含む)を見つけ、子の片付けが済んだときは、旧 `last-verified.json` を保ったまま stop-mark へ差分を移してこの印を消す
  - 止めの印 `stop-mark.md`: **通常の共有状態の照合**で食い違いを見つけて止まったとき(シグナル・EXIT の trap の経路で差分があったときを含む)と、残ったプロセスを止められなかったときに、理由と差分とともに置く。置くとき、その周の片付けが済んでいれば周の途中の印を消す(止めの印が差分を持つので役目が終わる)。済んでいなければ周の途中の印を残す(人が所有者・残った子・成果物を独立に確認する必要があるため)。起動前の管理パス検査が拒否したときは、比較の前なので `inflight` も `stop-mark` も消さずに exit 20 とする
- **昇格中のシグナル**: 通常周は判定後に own worktree の削除を始める前から、最終観察の厳密比較・`last-verified.json` の昇格・`inflight` の削除・active 状態の解除を1つの狭い完了区間で行う。TERM/HUP/INT の先着1件はこの区間だけ保留し、成功または stop-mark と旧基準・印の保全まで完了してから同じ終了処理へ渡す。比較・昇格が失敗した場合に signal より先に `inflight` や証拠を消さない
- 中断印は人が独立に所有を確認してから扱う。印の場所と消し方は報告と ERROR の文言に出る(§7)

**次の起動**(§2 の 7。ロックの直後、デフォルトブランチの固定・profile の読み取り・ネットワークの git より前)

- 止めの印または周の途中の印があれば exit 20。印の内容・checksum の一致を承認に使わず、signal・worktree 削除・unlock・ref 更新を行わない。
- 旧版の控え、別 worktree からの再開、設定が変わっていない中断でも同じ。人が実行の所有者・残ったプロセス・成果物を独立に確認してから印を扱う。
- 現在の supervisor が所有した子の不在を、保存 PID・印・checksum から補完しない。親または supervisor が SIGKILL された場合も、人が別の信頼領域から残存プロセスと共有状態を確認する。
- どちらの印も無ければ、最後に照合に通った状態と今の状態を比べ、差分があれば報告に出して続ける(実行と実行の間の変化は人の操作でもありうるため)
  - 人の設定変更・起動元の変更・旧 `last-verified.json` の不足も同じ扱い。次に照合に通ったときだけ、新形式の状態を保存する
  - 起動元変更時は前の起動元の設定を再読しない。中断の無い実行間の比較を、前の起動元の未変更の保証には使わない

## 5. 判定と後片付け

### review/commit 照合

無人の task 周の正常条件には、source と doc の各 `review-guard.py verify` が 0 であることを加える。
周の report には検証担当が直接得た `REVIEW_BINDING_SHA256`、照合結果、ignore/ignored 未追跡の変化、
品質コマンド・入口・test/selftest/検証器の変更一覧を残す。task MD の追加修正記録や reviewer 名だけから
APPROVED・実施済を再構成しない。guard の state と review input は `.claude/reviews/` の状態ファイルであり、
review 対象集合へ含めない。

guard の 0 以外、保持値の欠落、task の期待バイト不一致、対象外の状態名 MD の変更は失敗とする。
保留は、未承認保存専用の控えを機械生成して同じ照合を通したときだけ正常(保留)にする。

材料は、ホスト CLI の終了コード・stdout の JSON の結果テキストの中で結末の行の照合パターン(unattended-mode.md §2)に一致する**最後の行**・worktree の git の状態(前置きつき)。

**結末の行の選び方**(実装モードと発見モードで共通): 照合パターンは unattended-mode.md §2 の 5 値(`PR`・`縮退`・`保留`・`失敗扱い`・`候補なし`)で、一致する最後の行を結末とする。モードで取りえない値は失敗にする(実装モードの `候補なし` は下の表の失敗で、判定の表示は「結末の行が読めない」。発見モードの `保留` は §11)。前の行に別の値があっても、最後の行で決める。

stdout の JSON は、利用者の設定(`verbose`)によっては結果 1 つのオブジェクトではなく全メッセージの配列になる(実測)。`loop.sh` は、オブジェクトならそれを、配列なら最後の `type: "result"` の要素を結果として読む(どちらでもなければ「JSON が読めない」の失敗)。報告のホストの結果の拒否の欄(§7)は、配列ならすべての `type: "result"` の要素の `permission_denials` を順につなぐ(周の中でバックグラウンドに委託すると、結果の要素がターンごとに出て、拒否はそのターンの要素にだけ載る — 実測)。結末の行だけを信じず、git の状態と突き合わせる。

| 判定 | 条件(すべて) | 後片付け |
|---|---|---|
| 正常(PR) | 終了コード 0 / 結末 `PR` / HEAD が `refs/heads/task/{名}` / HEAD の tree に `完了_{名}.md` が在り `進行中_{名}.md` が無い / `origin` の `refs/heads/task/{名}` の sha(`ls-remote`。ネットワークの規則)= ローカルの HEAD | worktree を消す |
| 正常(縮退) | 終了コード 0 / 結末 `縮退` / HEAD が `refs/heads/task/{名}` / HEAD の tree に `完了_{名}.md` が在り `進行中_{名}.md` が無い | worktree を消す |
| 正常(保留) | 終了コード 0 / 結末 `保留` / HEAD が `refs/heads/task/{名}` / HEAD の tree に `保留_{名}.md` が在り `進行中_{名}.md` が無い / `保留_{名}.md` の追加修正記録の保留の行が、固定した sha の `進行中_{名}.md` より 1 行多い | worktree を消す |
| 失敗 | 上のどれにも当たらない(結末 `失敗扱い`・結末 `候補なし`〈実装モードでは取りえない〉・時間切れ・終了コードが 0 でない・結末の行が無い・JSON が読めない・結末と git の状態の食い違い・判定の `ls-remote` の失敗)。片付けでプロセスが残ったときは、判定に進まずに止まる(§4) | worktree を残す(lock の理由はそのまま) |

- 正常の 3 つは、どれも作業ブランチが残るので、以後そのタスクは拾われない。`縮退` も正常とする(commit は作業ブランチに在る)。gh の有無は起動時に確かめず、結末を朝の報告に出す
- 失敗の周の worktree は、lock の理由 `dev-workflow-loop: <タスク MD のパス>` のまま残り、以後そのタスクを拾わない除外の印になる。ループは次のタスクへ進む
- 消す手順: `.claude/reviews` が実体のディレクトリ(`[ -d ] && [ ! -L ]`)なら、symlink をたどらずに状態ディレクトリへ写す(上限 50 MB。超えたら写さずに報告。写しに失敗しても止めず、「写せなかった」と報告して続ける)→ unlock → `git worktree remove`(`--force` を付けない。ignore 済みのファイルだけなら成功し、それらは消える。状態ファイルの ignore は §3 の 2a で確かめる)
- 正常でも `remove` が拒否されたら(未追跡・未 commit が残った)、worktree を残し(lock し直す)、報告する。失敗には数えない
- 保留の周は、**最後の**保留の行の `<停止条件>` から対話点番号(人に確かめる場面の番号)を取り出して記録する(template の取り出しパターン。§6 の許可の拒否の保留の連続に使う)

## 6. 停止条件と終了コード

| 停止条件 | 既定 | 数え方・確かめ方 | 終了コード |
|---|---|---|---|
| 最大周回数 | 5 | 周の前(§3 の 1)に、起動した周の数(選定で終わった回は数えない)が上限に達していれば止まる | 0 |
| 時間予算 | 28800 秒 | 周の前に、ループ全体の残りが周の上限より短ければ止まる(周を途中で打ち切ると worktree が残り、失敗に数えることになるため) | 0 |
| 停止ファイル | `<対象>/.claude/loop.stop` | 周の前に在れば止まる(周の途中では打ち切らない。読むのは人のチェックアウトのパス) | 0 |
| キューが空 | ― | §3 の 6 | 0 |
| 連続失敗 | 2 | §5 の失敗が続いた数。正常で 0 に戻る。周の後に確かめる | 10 |
| 許可の拒否の保留の連続 | 2 | G1 の保留を種類によらず数える。通常の連続失敗にも入れ、厳しい方で止まる。ほかの結末で G1 の連続は 0 に戻る | 10 |
| 片付けで残ったプロセス | ― | ループの最中の周の片付け(§4)で、残ったプロセスが無いと確かめられない。止めの印を置き、周の途中の印を残す(シグナル・EXIT の trap・次の起動の経路では、§4 の「残ったときの終わり方」のとおり、それぞれ 128+n・10・20) | 10 |
| ホスト CLI の実体の変化 | ― | 周を始める前(周の途中の印を置く前)に、ホスト CLI の実体を内容の sha256 まで照らす(§2 の 12)。補助の CLI の前の照合で先に違いが見つかれば、呼んだ検査の理由(多くは `environment`)で止まる。選定中の worktree は「選定中」の lock のまま残して報告する | 20 |
| 共有の状態の変化 | ― | 周の片付けの直後(§4)に、共有の git ディレクトリ(その周の起動の直前の控えと比べる)と `refs/heads/<DEF_NAME>`(起動時の固定値と比べる)を照合する。変わっていたら差分を報告する | 10 |
| 途中の全体の失敗 | ― | 選定の `ls-remote` の失敗・`worktree add` の失敗・helper の exit 1・2・報告の書き込みの失敗・そのほかの内部の失敗(EXIT の trap で揃える) | 30 |

- 周の前の確かめ方は、最大周回数 → 時間予算 → 停止ファイルの順。当たったものを報告の理由にする
- 停止ファイルは止める向きにしか効かないので、リポジトリの中に置いてよい
- 人の checkout 内の停止ファイルは、開始時に保持した**正確な** path が不在から空の通常ファイルになった場合だけ、共有状態の照合で許す。既定と `--stop-file` の指定は同じ規則であり、必要な新規親 directory はその leaf へ一意に続くものだけに限る。既存 file の変更、nonempty・link・特殊型、親の差替え、sibling、他の人の worktree と当該周の child worktree の同名 path は通常どおり差分として止める。周の前から停止ファイルが在る場合に child を起動しない規則は変えない
- 連続失敗には §5 の失敗と G1 の保留を数える。ほかの正常終了で 0 に戻る。許可ログは参考表示だけで、偽 protected・削除・並べ替え・重複によって回数を減らさない。
- 周の途中の全体の失敗(認証切れなど)は、連続失敗か終了コード 30 で止まる

**終了コード**

| 終了コード | 意味 |
|---|---|
| 0 | 周の前の停止条件(最大周回数・時間予算・停止ファイル)かキューが空で終わった。`--dry-run` が一覧を出して終わった。`--prove-host` が証明を書いて終わった |
| 2 | 使い方の誤り(引数) |
| 10 | ループの途中で止まった(連続失敗・許可の拒否の保留の連続・片付けで残ったプロセス・共有の状態の変化・EXIT の trap で差分か残りがあった) |
| 20 | 起動時の検査で止まった(§2・§3 の 2a と 3 と 3a・§4 の「次の起動」)。ループの途中でも、周を始める前の検査(ホスト CLI の実体の照合・環境の照合・状態の控え)で止まれば 20 になる |
| 30 | 途中の全体の失敗(上の表) |
| 128+n | シグナルで止まった(TERM は 143・HUP は 129・INT は 130) |

## 7. 朝の報告

状態ディレクトリの `<実行 ID>/report.md` に書き、要約を stdout に出す。

- 起動時: 実効値・対象の一覧・読み飛ばしたタスクと理由・作成日が無いタスク・origin の URL の検査の結果(§2。origin が無ければ、周は push しないこと)・先行する commit・MCP の有無・導入済みの同名プラグインの版・ホスト CLI の実体と証明・skill と command の allowed-tools の検査の件数・許可の仲介の許可リストのうち使わない形の項目(§9)・profile で無視したキー・残った worktree(過去の実行の分を含む)・前の実行との間の共有の状態の差分
- 周ごと: タスク・結末・判定(§5)・PR の URL か保留の理由(対話点番号)・終了コード・所要時間・片付けの結果・共有の `config` の差分(照合に通った周は `無し`)・ホストの結果の拒否の欄(あれば。配列ならすべての結果の要素から — §5)・許可の仲介の記録(§9。参考表示。allow・deny の件数と種類、deny の行)・周のログのパス
- 止まった理由(§6)と、共有の状態が変わったときはその差分。止めの印を置いたときは、印のパス・置いた理由と差分・消し方の定型(差分を確かめ、必要なら元に戻してから、印のファイル `stop-mark.md` を消す)。exit 20・10 の ERROR の文言にも同じ案内を載せる
- 残った worktree(パス・lock の理由・その周の結末。過去の実行が残した分は「(過去の実行)」と、その実行の `report.md` のパスを示す)と、人の次の手順の定型(worktree を調べて消す: `git worktree unlock` → `git worktree remove`)。開始時・終了時とも同じ lock 理由の全件をパスごとに報告する。人は各 worktree を調べてから片付ける。既存の残存 worktree を自動では消さない
- 保留のタスクを再び回す手順の定型(unattended-mode.md §5 の補足・§8): デフォルトブランチ側の MD を直し、作業ブランチを消すと再び拾われる。ブランチの消し方は `git branch -D task/{名}`、push 済みなら `git push origin --delete task/{名}`(手元の追跡用 ref も消える)、リモートを別の手段(GitHub の画面など)で消したときは `git branch -dr origin/task/{名}`。読み飛ばしの理由が `refs/remotes` の追跡用 ref のときは、報告にこの 1 行を添える

## 8. 実走(受け入れ)

`loop.sh` の実 CLI での受け入れの手順の要点。一式(テスト用のリポジトリを組み立てるスクリプト・プローブ・結果を集めるスクリプト)は team-lead(作業を進め、結果を確かめる側の AI)が用意し、人が読んでから使う。合否は Issue #68 の完了条件 6、結果は Issue のコメント、実測した事実は design §7-3 に残す。

- **H50 の実測**: Linux の Claude Code 2.1.293 で、正常設定の `--prove-host` は拒否 hook を確認して証明を作り、`--dry-run` も通った。利用者設定と `remote-settings.json` の `env` に `CLAUDE_CONFIG_DIR`・`CLAUDE_CODE_SIMPLE`・`CLAUDE_CODE_SAFE_MODE`・`CLAUDE_CODE_SHELL_PREFIX` をそれぞれ置いた計8件は、ホスト CLI を1回も起動せず exit 20(`host-settings`)で止まった。これは loop の起動前検査の観察であり、cache の値がホストへ適用されたことや許可の素通りが成立したことを示さない。個人プランの cache 非適用の観察を他プランへ一般化しない。結果は [Issue #229](https://github.com/Yuki-Maeda-valour/workflow/issues/229)。

- **H46 の実測**: Linux の同じホスト版で、skill・agent・plugin の hook、mod、skill-folder plugin の広い規則、skill と agent の共用本文、字下げのない agent の MCP・preload の計8 fixture は、ホスト呼出し0件で exit 20 になった。固定する3つの真偽値の不正な設定も同じ位置で拒否した。選択された marketplace 項目だけにある広い規則は、補助 CLI の後、セッション0件で拒否した。正常な skill-folder plugin と agent では `--prove-host` と `--dry-run` が通り、宣言した plugin MCP は起動しなかった。既知の組込み名と同名の外部 skill も証明を作れた。各確認で設定・cache を復元し、fixture を撤去して子孫の停止を確認した。管理側は追加 root の fixture による検査で、実 system 設定は変更していない。証明を使う通常周や差替えの競合は回帰テストの範囲と区別する。結果と外部事実は [Issue #231](https://github.com/Yuki-Maeda-valour/workflow/issues/231)・design §7-3。

- **人が素の端末で行う**: ホストのセッションの中からはホストと同じ CLI を起動しない(design §5-5)。`loop.sh` もホストの中では止まる。GitHub には使い捨ての private リポジトリを 1 つ作り(作成と削除は人)、`$HOME` の下の隠しでない場所(`/tmp` でない)に clone する。merge の前に行う
- **確かめる 6 件**: X(PR)・Y(`needs-user` で保留)・Z(無人の前提を欠いて失敗扱い)・W(承認後の本文の変更で保留)・R(基準行つきの再開)・T(時間切れと、切り離したプロセスの片付け。`origin` の無いローカルのリポジトリで行う)。周の中の改竄は実 CLI では決定的に起こせないので含めない(#67 の scratch の通し確認で確かめ済み。攻撃経路は #107)
- **テスト用のリポジトリ**: init-project の gitignore の断片(状態ファイル 3 つ)を入れて commit しておく(§3 の 2a。無いと `--dry-run` も exit 20)
- **ホスト CLI の証明**: `--dry-run` も証明が要る。最初に、導入済みの dev-workflow を一時的に無効にして `--prove-host` を打つ(導入済みが有効で版が違うと、`--prove-host` も版の検査で止まる)
- **版の検査**: 導入済みの dev-workflow を有効に戻して `--dry-run` を打ち、版の違いの理由(`plugin-version`)で exit 20 になることを見る。exit 20 は理由コードまで確かめる(証明が無ければ `host-proof` で止まり、版の検査に届かない)。次に導入済みを一時的に無効にし(終わったら有効に戻す)、`--dry-run` が対象の一覧を出して exit 0 になることを見て、解決後の argv を控える
- **陽性対照**(控えと本番の前): 隔離なしの短いヘッドレス起動で、リポジトリ側の hook と MCP の印が両方できることを確かめてから、印を消す(できなければ仕掛けを直してやり直す)。続く短いプローブ(リポジトリ側の skill・エージェント定義・コマンドが読まれるか・許可の仲介・拒否の現れ方・起動引数と設定の優先・未ログインの返り方)も、隔離なし(陽性対照)と隔離ありを同じ操作で比べ、観測はホストが出す一覧・流れるメッセージで行う(モデルの答えに頼らない)。対照で作った印は、結果を保存してから消し、無いことを確かめてから本番を打つ。印はリポジトリと worktree の外に書かせ、メインの clone に副作用が残っていない(`git status --short` が空)ことを確かめる
- **許可の仲介のプローブ**: プローブ用のディレクトリを周の worktree に見立て、`--dry-run` が出す解決後の argv と環境変数で短く起動し(許可リストは本番で使う値そのもの)、do-task の Phase 0 と update-doc が行う操作(`reviews-dir.sh` の呼び出し・W への書き込み・`mkdir`・リダイレクト・`mv`・`rm`・`cd` の後の書き込み・`export` の文・事前検査を直接実行して終了コードをツールの結果で見る形・周の Bash の書き方の例〈保留のガードの `test -f .claude/grasp.md && echo yes || echo no`・`test ! -L .claude/grasp.md && echo yes || echo no`〈`grasp.md` が在る時点で打ち、期待は yes〉・`test -e .claude/reviews/x || test -L .claude/reviews/x && echo yes || echo no`〈`x` が無い時点で打ち、期待は no。偽が出力で見える〉/ 単独の呼び出しの終了コード(`bash -c 'exit 21'` のツールの結果に 21 が出る)/ 先頭が `/` のパターンを避けた形(task-template の本文の取り出しの grep を `[/]` で書いたもの・`sed -n '\%^## %p' AGENTS.md > .claude/reviews/sec2.txt`)/ `/dev/null` の形の `git -c core.hooksPath=/dev/null status --short 2>/dev/null > .claude/reviews/st4.txt` を含む〉)と、拒否されるべき操作を順に打たせる。旧い事前検査の受け方(一時ファイルへ受けて読み戻す)と、変数の展開を含む git(`TOP=$(git …)`・`git -C "$TOP" …`)と、字面どおりの `[ -f … ]`・`[ -e … ] || [ -L … ]` と、出力に出さない `test` の形(9 と同じ偽の状態で打ち、偽がモデルに見えるか)と、先頭が `/` の grep(`grep -oE '/ 本文: …'`)は、確認に回るかと hook の判定・`message` を記録するだけにする(合否に数えない)。陽性対照として、許可の仲介の設定(設定を足す起動引数)を外した同じ起動で W の中への書き込みが拒否されることを先に見る。流れるメッセージ・記録のファイル・ファイルの有無で見る。skill の手順の形(拒否されるべきものと記録だけの項目を除く)に 1 件でも拒否が出たら本番に進まず、記録を添えて team-lead に返す(設計と実装を直して再レビューしてから、プローブからやり直す)
- **本番**: 実行前の控えを取り、メインの clone で `--only` を 5 件(X・Y・Z・W・R)に絞って `--allowed-tools` と `--max-iterations 6` を付けて打つ。時間切れ用のリポジトリで T1(既定の周の上限)と T2(`--iteration-timeout 60`)を打つ。結果を集め、使い捨てのリポジトリを消して、導入済みの dev-workflow を有効に戻す
- **印の扱い**: 止めの印で止まったら、差分と報告を保存し、差分が周の操作によるものなら skill の手順を、デフォルトブランチの移動なら固定を見直す。見直しが要らない(人の操作による変化など)と判断したら、止めの印を消して打ち直す
- **やり直し**(許可リストの不足が出たとき — `G1` の保留でも、ブランチを作る前や `完了_` の後の拒否による失敗扱いでも): 報告の拒否の欄を見て許可リストに足し、足した値を記録する。そのタスクを次の順で除外から外し、同じ `--only` で打ち直す
  1. 周のログと、残った worktree の変更(`git -C <worktree> status --porcelain` と `git -C <worktree> diff`)を保存する
  2. 残った worktree があれば `git worktree unlock <パス>` → `git worktree remove <パス>`。未 commit・未追跡が残って拒否されたら、実走用の使い捨てのリポジトリに限って `git worktree remove --force <パス>`(普段の運用では、残りを調べてから人が決める)
  3. 作業ブランチを消す(§7 のブランチの消し方)
  4. `--dry-run --only <名>` で、そのタスクが対象の一覧に戻ったことを確かめる
  - 手順が通らないときは、ログを保存してから、使い捨てのリポジトリを組み立て直す。T2 の周が上限より前に終わって時間切れを見られなかったときも、周のログを保存してから時間切れ用のリポジトリを組み立て直し、値を下げてやり直す(同じリポジトリでは、作業ブランチか残った worktree があるので拾われない)
- **実測が前提と食い違ったら**(疎通の返り方・リポジトリ側の skill などが読まれるか・周が共有の `config` に書かないこと・起動引数と設定の優先・許可の仲介の効き方): `loop.sh` か skill を直し、再レビューしてから merge する
- **ホスト CLI の証明と allowed-tools(#193)**: ホスト CLI の写しを使い捨ての置き場に置き、専用の設定ディレクトリ(`CLAUDE_CONFIG_DIR`)に人がログインし、自動更新を止めて、`HOME`・状態ディレクトリ・証明の置き場を scratch へ向けて打つ。確かめること: 証明の無い実体・別の版の実体・同じ置き場の書き換えで、ホスト CLI を起動せずに止まる / 未ログインの `--prove-host` が認証の確認で止まる / `--prove-host` が実 hook の拒否を観測して証明を書き、`--dry-run` が通る / hook を渡さない起動と、hook の自動の読み込みを飛ばし、保存したログイン(OAuth・keychain)を読まない起動では、判定が証明を書かない / 権限を足す利用者の skill で起動前に止まる / 許可リストに含まれる skill と正規導入のリンクは通る。あわせて、skill の `allowed-tools` が同じターンで hook を通らずに効くこと(攻撃の成立)と、本番の許可の仲介のもとでの Skill の道具の扱いを、対照つきの直接の起動で記録する。結果と版は Issue #193 と design §7-3
- **発見モードと実装モードを毎晩回す**: 1 回の実行はどちらかのモードだけ(§11)。状態ディレクトリのロックが共通なので、並べて起動せず、cron の 1 行に順に書く(例 `bash <clone>/…/loop.sh --repo <対象> --discover; bash <clone>/…/loop.sh --repo <対象>`)。発見モードの実走は §11

### 自動メモリの隔離試験(H32)

- 設定ディレクトリ・リポジトリ・自動メモリの置き場を、すべて使い捨ての領域にする。設定ディレクトリの指定で利用者の設定を分離できない環境では、HOME も試験用に分離する。試験専用の設定へ `autoMemoryEnabled: true` と、絶対パスの `autoMemoryDirectory` を置く。実利用者の設定やメモリはコピーも変更もしない。
- 既存メモリに試験専用の合言葉を置き、ファイル一覧と内容のハッシュを控える。ホストのネスト起動判定の環境変数を外す。機能を別の理由で無効化する最小起動モードは使わない。版・OS・起動引数を記録する。
- 陽性対照は無効化変数が未設定または `0` の新しいセッション。合言葉をプロンプトに書かず、ツールで読み直させずに答えられるかを見る。続いて別の試験用の好みを記憶させ、メモリの更新を確認する。保存はモデルの判断を含むため、対照で保存されなければ保存の比較は判定不能とする。
- fixtureを元へ戻し、`1` の新しいセッションで同じ入力を試す。自動の読み込み・保存と、明示的なファイル操作をログで区別する。応答だけでなく、ツール呼び出しとメモリの前後比較で判断する。設定ファイルが不変であることも確認する。
- 環境の受け渡しは、Linux 上の `loop-selftest.sh` の `memory` 節で実装・発見×親未設定・`0`・`1`を確認する。これはスタブ試験であり、機能効果の試験とは別。
- **2026-10-06の結果**: 最初の macOS / Claude Code 2.1.289 の試行は未認証で実施不能だった。続く Linux / 同版の認証済み試験では、`0` で合言葉の自動読み込みと好みの保存を確認し、`1` では合言葉を答えず、保存のツール呼び出しもメモリの変更もなかった。設定は両方で不変。読み込み試験では全ツールを外し、保存試験では Read・Write・Edit だけを使った。設定・リポジトリ・メモリを隔離し、利用者の設定とメモリは使わず、認証だけを子環境へ渡した。これはその版・条件での機能確認であり、任意のファイル操作の禁止は保証しない。Linux の実ループと実ホストを組み合わせた通し実行、macOS での機能効果は未確認。記録は [Issue #151](https://github.com/Yuki-Maeda-valour/workflow/issues/151)。

## 9. 許可の仲介(保護パスへの書き込み)

ホストは、保護パス(`.claude/`・`.git` など。一覧と出典は design §7-3)への書き込みを、編集の自動許可でも許可リストでも通さず確認に回し、確認は自動で拒否になる(§4 の権限)。do-task・update-doc はレビューの記録を `.claude/reviews/` に書くので、そのままでは無人の周が `G1` で止まる。次の 3 つで、周の中の skill の状態ファイルへの書き込みを通す(Issue #68 の決定 7・D22)。全許可のモードは使わない。

**① 先に作る**

- `loop.sh` は、周の worktree を作った後・周を起動する前(§3 の 2b)に、worktree の `.claude/`・`.claude/reviews/` を base-commit.md の ① と同じ検査で作る: symlink なら止まる / 無ければ `-p` なしの `mkdir` / 在って通常のディレクトリでなければ止まる(止まるときは終了コード 30・理由コード `reviews-dir`)。`loop.sh` の書き込みは許可の外

**② スクリプトに寄せる**

- `.claude/reviews/` の作成と未追跡一覧の保存は、同梱の `do-task/scripts/reviews-dir.sh`(`ensure` / `save-untracked`)が行う(base-commit.md の ①〜③ と do-task Phase 0 の手順 5。対話でも同じ)。スクリプトの中の書き込みは、ホストの保護パスの検査(出力のリダイレクト先と、編集の自動許可が許すファイル操作のコマンドの引数)に掛からない。公開は rename(2)(python3 の `os.replace`、無ければ `mv -f -T`)。sha256 は `sha256sum`・`shasum`・`openssl` の順
- `reviews-dir.sh` の終了コード: 0 = 成功(`save-untracked` は stdout に一覧の sha256)/ 2 = 使い方の誤り(`--list` が `--root` の `<管理ルート>/.claude/reviews/` の直下でないときを含む)/ 3 = 検査で止まる(symlink・通常のディレクトリでない・`LIST` が symlink か通常ファイルでない)/ 4 = git の失敗 / 20 = 内部の失敗・sha256 を計算する道具が無い(tool-missing)。止まるときは何も公開せず(一時ファイルは消す)、stderr に `ERROR [理由コード]` を出す。呼び出し側(base-commit.md・do-task)は exit 0 以外で停止して報告する

**③ hook**

- `loop.sh` は、`PermissionRequest` の hook を持つ設定を、設定を足す起動引数に JSON 文字列で渡す(ファイルにしない — 周が書き換えられないように。argv の並びは §2 の 5、フラグの事実は既定表と design §7-3)。hook は確認に回る呼び出しで、自動の拒否の前に呼ばれ、allow を返せばその操作は許される。確認を自動で拒否する設定と編集の自動許可はそのまま
- hook の本体は同梱の `ship-task/scripts/loop-permission.py`。hook のコマンドは、起動時に解決した `python3` の絶対パスと `loop-permission.py` の絶対パスを、それぞれシェルのクォートで囲んで組み立てる
- 周の子の環境に渡すもの: `DEV_WORKFLOW_LOOP_WORKTREE`(周の worktree の物理パス)・`DEV_WORKFLOW_LOOP_PERMLOG`(判定の記録のファイル)・`DEV_WORKFLOW_LOOP_PLUGIN_ROOT`(プラグインルートの物理パス)・`DEV_WORKFLOW_LOOP_ALLOW`(下の許可リストを直した、種類つきの JSON 配列 `[{"kind": "all" | "prefix" | "exact", "words": […]}]`)
- **許す集合 W**(物理パス。親の symlink と `..` を解いて比べ、対象そのものが symlink なら W に入れない)
  - 周の worktree の `.claude/reviews/` の下(`.claude/reviews/` そのものは `mkdir` の作成先としてだけ。削除・移動の対象にしない)
  - 周の worktree の `.claude/grasp.md`・`.claude/.understand-project-done`
  - `.claude/` そのものは入れない(① で先に作るので要らない)
- **許可リスト**(hook が Bash のコマンドを照合する): `loop.sh` が起動時に、`--allowed-tools` の `Bash` の項目と、共通 reader で検査済みの利用者の `settings.json` の `permissions.allow` の `Bash` の項目を読んで直す。設定が読めないか不正なら、その項目だけを無視して続けず exit 20 で止める。設定ファイルの不在は許す。
  - `Bash` だけ = すべてのコマンド / 末尾の ` *` か `:*` = 単語の区切りでの接頭辞 / `*` を含まない = 完全一致(`Bash(npm run build)` は `npm run build --watch` に一致しない)/ ほかの形(途中の `*`・`Bash()`・`Bash( )`・`Bash(*)` など)= 使わない(起動時の報告に出す)。「すべて」になるのは `Bash` だけ(公式文書では `Bash(*)` は `Bash` と同じだが、許可の仲介は `Bash` だけを「すべて」とする)
  - 直すときは、`--allowed-tools` か利用者の設定の `permissions.allow` を直す(profile では受け付けない)
- **判定の要点**(決定的。入力の `tool_name`・`tool_input`・`cwd` と環境変数だけで決める。細部は `loop-permission.py` と Issue #68 の D22)
  - 書き込み・編集のツール(`Write`・`Edit`・`MultiEdit`・`NotebookEdit`)は、対象が W の中なら allow
  - 読み取りのツール(`Read`・`Glob`・`Grep`)は、対象がプラグインルートの下なら allow(skill の references を読むため)
  - Bash は、まず `tool_input.dangerouslyDisableSandbox` を調べる(H25、[Issue #172](https://github.com/Yuki-Maeda-valour/workflow/issues/172))。
    - 未指定または JSON の `false` だけを、通常のコマンド判定へ進める。`false` 自体が許可を与えるわけではない。
    - `true` はサンドボックスの無効化として拒否する。真偽値以外(null・数値・文字列・配列・オブジェクト)も型不正として拒否する。数値の `0`・`1` は真偽値として扱わない。
    - どちらの拒否も種類は `other`。コマンドの許可リスト・ファイル操作・特別な `cd` 形式より先に適用する。判定ログの `subject` にはコマンドを残す。
    - 説明(`description`)・タイムアウト(`timeout`)など、ほかの入力項目は従来の扱いを維持する。未知キーを一律には拒否しない。
  - 上の入力検査を通った Bash は、入力の `cwd` が周の worktree の下(自身を含む)で、コマンドが限定の構文(単語・引用符・バックスラッシュ、区切りは `&&`・`||`・`;`・`|`、決まった形のリダイレクト)で読めて、次をすべて満たすときだけ allow
    - 単引用符の外の `$`・バッククォート・`<(`・`>(`・`<<`・括弧と中括弧・改行・単独の `&` を含まない。引用符の外のグロブの文字(`*`・`?`・`[`)と未引用の `~` をどの単語にも含まない
    - コマンドの前の環境変数の代入と、`export` の後に代入だけが並ぶ文は、`CDPATH`(値は空)・`LC_ALL`・`LANG`・`GIT_NO_LAZY_FETCH`・`GIT_TERMINAL_PROMPT` の 5 つの名だけ(`GIT_DIR`・`GIT_CONFIG_*`・`GIT_PAGER` などは拒否する)
    - `cd` は次の 2 つの形だけ: `CDPATH= cd -P -- <パス> && pwd -P`(全体でこれだけ。パスは worktree かプラグインルートの中)/ コマンドの先頭の前置き `CDPATH= cd -P -- <P> && <続き>`(代入は `CDPATH=` の 1 つだけ・リダイレクト無し・<P> は worktree の中の在るディレクトリでグロブでない・前置きの区切りは `&&`・続きに `cd` を含まない。続きは <P> を作業ディレクトリとして、ここの規則で判定する。続きのうち最初の `||`・`;` より後ろは、<P> と元の作業ディレクトリ(入力の cwd)の両方で判定し、両方で通るときだけ許す。bash では `|` が最も強く、`&&` と `||` は同じ強さで左結合、`;` が最も弱いので、cd が実行時に失敗したときに動きうるのはそこから後ろだけ。2 回目の判定は状態を分けて行い、どちらかが other なら other、そうでなく protected があれば protected にする。ホストの Bash ツールの shell で `cd` が組み込みのとき、どちらで動いても、先に `cd` を 1 文で打ってから続きを打つのと同じか狭い — §10)。どちらの形でも、行き先の字面が `-` で始まるもの・`~` を含むもの(位置と引用の有無を問わない)は拒否する(`cd -P -- -` は `--` の後でも `$OLDPWD` へ移る。bash は `x=~/y`・`x=a:~/y` のような代入の形の引数の `=`・`:` の直後の `~` も展開し、`~'/…'` のように引用された文字が続く `~` は展開しないので、字面から行き先を写すとずれる)。2 つ目の `cd`・ほかの形の `cd`・プラグインルートへの前置きも拒否する
    - パスとして解ける単語(`--name=値` の値と代入の値を含む)が、worktree とプラグインルートの外(`/dev/null` を除く)か、保護パスの下で W の外を指さない
    - ファイル操作のコマンド(`mkdir`・`touch`・`rm`・`rmdir`・`mv`・`cp`・`tee`)は、列挙したオプションだけを受け付け、書き込み先がすべて worktree の中で、保護パスの下なら W の中(プラグインルートには書かない)。保護パスの下の削除・移動の元は、W の通常ファイルか `.claude/reviews/` の下のディレクトリ(そのものは除く)。再帰の削除は、対象と削除される子孫がすべて既存の削除規則を満たす場合だけ許す。worktree 自体と、子孫を検査できない場合は拒否する(下の H26 の規則)
    - `sed` は出力だけを行う短い script だけを許す。read/write/execute を行う script と未知の script は拒否する。安全な in-place は既存の書き込み先検査に通し、`--sandbox` を付けた安全な script(数値または `$` の単一 address・範囲を含む `p`/`d`、または限定した `s`)は許す
    - `find` は `-print`・`-name`・`-path`・`-type` などの読み取りだけを許す。`-delete`・`-exec`・`-ok`・`-fprint`・`-fls` の各系統は拒否する。`awk` は固定の品質確認 script だけを許し、任意の program は拒否する
    - `git` は固定の global option と `-c key=value`、列挙した subcommand・option だけを許す。基準確認の `rev-parse --verify --quiet`・`show --no-show-signature`・`ls-files --stage/--ignored`・`diff --no-relative`・`for-each-ref --format=`、origin の heads query と、`task/` 下の通常の Unicode 名を含む branch 作成は維持する。`--config-env`・未知の key・alias・外部 helper・共有状態を直接変える subcommand は拒否する。書き込み pathspec は通常ファイル 1 件だけを調べ、`.`・ディレクトリ・magic・glob・ファイル入力を拒否する。存在しない pathspec は index と HEAD tree の和集合が同じ通常ファイル 1 件に一致するときだけ復元を許す。status の読み取り用の除外 pathspec は維持する。`env` は `-S` を含む再解釈を安全に正規化できないため受け付けず、`command`・`bash -c` 経由の Git/GH 実行と path で隠した Git/GH は専用 grammar を迂回するため拒否する
    - `gh` は `repo view`・`auth status`・数値の PR 番号による `pr view --json state`・`pr create` だけを許す。PR は `--body-file -` と、reviews 下の非 symlink の通常ファイル 1 件からの単一の `<` だけを stdin にできる。pipe・here document・他の stdin・未知 flag・`api`・設定変更は拒否する
    - 上の専用検査を持つコマンドは、その閉じた grammar を通り、かつ許可リストの単語列にも一致するときだけ許す。それ以外の通常コマンドは、既存のパス検査と許可リスト照合を通す
    - リダイレクトの書き込み先は `/dev/null` か、worktree の中で保護パスの下なら W の中。読み込み元は worktree かプラグインルートの中
  - それ以外はすべて deny。判定できない入力(JSON が読めない・環境変数が無い)も deny。deny の `message` には、理由に続けて、「無人の周では打ち直さず、許可の拒否(G1)に従う」ことを固定の文で添える(unattended-mode.md の「許可の仲介の構文」。周の Bash は最初の呼び出しからその書き方で打つ)
- **未引用の `~` の拒否**([Issue #181](https://github.com/Yuki-Maeda-valour/workflow/issues/181)、#107 H38): Bash の語に未引用の `~` があれば、位置を問わず `deny(other)` にする。コマンド語・引数・代入・リダイレクトの先を、許可リストや個別のパス判定より先に検査する。
  - `x=~/z`・`x=a:~/z` のほか、先頭・語中・末尾も拒否する。引用部分に隣接する未引用の `~` や、空引用を添えた形も同じ。Bash が実際に展開する条件は再現しない。
  - 単引用・二重引用・バックスラッシュで保護した `~` は、リテラルとして既存のパス検査へ進める。引数と書き込み先には既存の保護判定を適用し、入力リダイレクトは既存の範囲検査を維持する。引用しただけで外部へのパスを許すことはない。
  - `cd` の行き先は既存の追加制約を維持する。位置と引用・エスケープの有無を問わず、`~` を含めば拒否する。Read・Write などの構造化されたパス入力は変更しない。
- **追跡しない移動と制御構文の拒否**([Issue #166](https://github.com/Yuki-Maeda-valour/workflow/issues/166)、#107 H37): 上の2種類の `cd` 形式以外に許可を広げない。全単純コマンドを先に調べ、次の構文は許可リストに一致しても `deny(other)` にする。
  - 直書きの `pushd`・`popd` と、`builtin`・`command` を介した `cd`・`pushd`・`popd`。`pushd -n` もディレクトリスタックを変えるため拒否する。ラッパーを重ねた形、`builtin --`、`command -p`(束ね・反復を含む)と `--` も調べる。
  - 単純コマンド先頭の、引用されていない予約語 `!`・`time`・`if`・`then`・`elif`・`else`・`fi`・`for`・`while`・`until`・`do`・`done`・`select`・`case`・`esac`・`in`・`function`・`coproc`。制御構文内の移動は追跡しない。空引用を含む引用・エスケープがある語は予約語として扱わない。
  - `&&`・`||`・`;`・`|` のどの位置でも、許可された `cd` 前置きの後でも同じ。実行されない分岐やパイプ内の移動も保守的に拒否する。
  - 移動しない `command -v`・`command -V` の照会、通常コマンドへのラッパー、単なる引数の移動語は、従来のパス検査・許可リスト判定に残す。ラッパーの無効なオプションも、移動を実行しない形なら通常判定に残す。実行成功を保証する判定ではない。
  - 既存の `cd` の移動先検査と、失敗後に実行されうる部分の二重判定、最終symlink保護は維持する。任意プログラムの内部動作、シェル関数・別名による置換、実行時のファイル差し替えまでを防ぐ隔離ではない(§10)。
- **短縮オプションに連結したパスの検査**([Issue #180](https://github.com/Yuki-Maeda-valour/workflow/issues/180)、#107 H24): `--` より前の、単一の `-` で始まる3文字以上の単語を調べる。3文字目以降の各位置から末尾までを値の候補とし、`.` で始まる候補・保護名・裸名の既存 symlink を既存のパス判定に渡す。`-o.git`・`-o.mcp.json` と束ね書きの `-ko.git`、ドットで始まらない保護名の連結も拒否する。
  - 引用やエスケープを外した単語で検査する。`/` を含む形は従来どおり判定不能として拒否し、`~` で始まる候補も、引用の有無にかかわらず展開せず `deny(other)` にする。
  - 通常の短縮フラグ・束ね書きは、候補が保護対象などに当たらなければ従来どおり判定する。`--` 以降は候補を切り出さず、単語全体を位置引数として調べる。たとえば `-- -o.git` は通常名として扱い、その名前の symlink が保護先を指せば拒否する。
  - コマンドごとのオプション仕様は解析しない。束ねたフラグの一部が保護名や既存 symlink に一致するなど、実際には値でない候補でも保守的に拒否することがある。ファイル操作の閉じたオプション集合・W の例外・長い `--name=値` の既存規則は維持する。任意プログラム内部のアクセスを防ぐ隔離ではない(§10)。
- **最終 symlink の保護判定**([Issue #159](https://github.com/Yuki-Maeda-valour/workflow/issues/159)、#107 H36): Bash の書き込み先とパスの単語は、リンクの置き場所と、最後のリンクまで辿った実体をそれぞれ worktree 相対で調べる。どちらかが保護パスで W の例外を満たさなければ拒否する。対象そのものが symlink なら、リンク先が W でも例外を与えない。多段・相対リンクと、まだ存在しない保護対象へのリンクも同じ規則を使う。
  - 引数・`--name=値`・許可された代入値は、`/` や `.` を含まない裸名でも、既存の symlink ならパスとして調べる。`--` の後ろは `-` 始まりでも位置引数として調べる。裸のコマンド名は PATH で探すため、この追加判定をしない。
  - `rm`・`rmdir`・`mv` の元はリンク自体の操作として、置き場所の削除・移動規則を維持する。ただし末尾 `/` 付きの再帰削除は、下の H26 の規則でリンク先を検査する。たとえば保護領域外のリンクが worktree 内の `.claude/settings.json` を指していても、リンク自体の削除・移動は許す。保護領域内のリンク自体は許さない。外部パスの範囲検査は従来どおりで、任意の外部リンクの削除許可は加えない。
  - 書き込み先だけが拒否理由なら `protected`、読むパスなら `other`。読み取りの候補を元の置き場所に結び付け、書き込み先・削除対象との既存の区別を保つ。この区別は同じコマンド内だけに適用し、別のコマンドによる削除・移動で読み取りの拒否を取り消さない。`<` の読み込み元は従来どおり範囲だけを検査する。検査後のリンク差し替えや、任意プログラムの内部アクセスまで防ぐ隔離ではない。
- **保護ディレクトリ内の cwd**([Issue #179](https://github.com/Yuki-Maeda-valour/workflow/issues/179)、#107 H27): 入力の cwd と、許可された `cd` 前置きの移動先を物理パスで判定する。保護パス内では、素の引数名・`--name=値` の値・許可された代入値も、存在の有無によらず cwd から解決して既存のパス検査へ渡す。`||`・`;` の後ろは、移動先と元の cwd の両方で同じ規則を使う。
  - 裸のコマンド名は PATH で探すため追加判定から除く。書き込み先だけが拒否理由なら `protected`、用途が確定しない引数の拒否は `other` とする。既存のリンク検査も維持する。
  - W の例外は維持する。`.claude/reviews` 内の cwd からの操作、通常 cwd から W を指定する操作、`.claude` からの `touch grasp.md`・`touch reviews/out`・`echo > reviews/out` は許す。`.claude/worktrees` の保護判定の除外も維持する。
  - 任意コマンドの引数の意味は推測しない。そのため `.claude` 内の `git checkout -- grasp.md` は `checkout`、`echo data > reviews/out` は `data` も保護パス候補となり、保守的に拒否する。通常 cwd の裸名を一律にパス扱いする変更はしない。短いオプションに連結した値の一般解析や、任意プログラム内部のアクセス・検査後の差し替えは保証しない(§10)。
- **再帰削除の対象と子孫の検査**([Issue #173](https://github.com/Yuki-Maeda-valour/workflow/issues/173)、#107 H26): `rm -r`・`-R` は、対象だけでなく削除される子孫にも既存の保護パスと W の規則を適用する。worktree 自体は `deny(other)`、許されない保護対象を含む場合は `deny(protected)` にする。絶対・相対パス、末尾 `/`、`.`・`..`、親リンクを解決した場所で判定する。
  - 子孫は明示的なスタックで列挙し、symlink の先を辿らない。末尾 `/` の無い最終リンクもリンク自体の削除として扱う。通常領域のリンクが保護先を指すだけなら、そのリンクを含む通常ディレクトリの削除は許す。保護領域内のリンク自体には W の例外を与えない。
  - 末尾 `/` 付きの最終リンクは実体を対象として検査する。GNU rm は `rm -rf link/`・`link//` でリンク先の内容を消し得るためで、多段リンクも同じ。元のリンクの置き場所とリンク先の両方が worktree 内でなければ拒否する。`link/.`・`link/..` も実体側で判定する。コマンドの実行成功を保証するものではない。
  - 保護対象の無い通常ディレクトリと、`.claude/reviews/` の下の既存の削除例外を維持する。reviews 自体は拒否し、`.claude/worktrees` の保護判定の除外も維持する。
  - 対象の情報取得、子孫の列挙・情報取得に失敗した場合は `deny(other)`。走査中の消失も検査不能として拒否する。最初から無い通常対象への従来の許可は維持する。ファイルの内容は読まない。検査後の差し替えを防ぐ隔離や、任意プログラム内部の削除の検査は追加しない(§10)。
- **記録**: hook は呼ばれるたびに、1 呼び出し 1 行の JSON を記録のファイル(状態ディレクトリの `<実行 ID>/iter-<周の番号>.permlog`)に足す。項目は `time`・`tool_name`・`cwd`(入力の)・`decision`(`allow` / `deny`)・`kind`(deny の種類。`protected` = 保護パスの下で W の外への書き込みだけが理由 / `other` = それ以外)・`reason`・`subject`(対象のパスかコマンドの先頭 500 文字)。実測のため allow も記録する。周ごとに allow・deny の数と deny の行を朝の報告に写す(§7)
- **`G1` の数え方**: 許可の仲介の記録は子が書ける参考記録なので、`G1` の保留は、拒否の記録の種類・欠落・順序にかかわらず、許可の拒否の保留の連続(§6)に数える(種類 `protected` だけの拒否も数える)。報告には種類を分けて出す
- **hook が動いていない疑い**: `loop.sh` は自動では判定しない。周の報告の許可の仲介の行(allow・deny の件数)で、deny が 0 件なのに `G1` の保留がある周は、人が見て、hook が動いているかを確かめる(§7)。allow も 0 件で「許可の参考記録を読めない」の行が出るなら、hook が一度も呼ばれていない疑いが強い
- **起動時の検査**(§2 のホスト CLI を確認する前): 最初の補助のホスト CLI より前と、有効 plugin を控え直した後に、利用者の `settings.json`・`remote-settings.json`・Linux の `/etc/claude-code/managed-settings.json`・`managed-settings.d` の直下にある非隠しの小文字 `.json` を検査する。設定が存在しなければ通すが、読取不能・不正な UTF-8/JSON・object でない本文・全階層の重複キー・NaN/Infinity のリテラル・深さ制限超過は止める。`env` は object、各値は文字列とする。
  - `env` の `CLAUDE_CONFIG_DIR`・`HOME`・`XDG_CONFIG_HOME`・`CLAUDE_CODE_SIMPLE`・`CLAUDE_CODE_SAFE_MODE`・`CLAUDE_CODE_SHELL_PREFIX` は、値を問わず存在すれば exit 20(`host-settings`)で止める。既知の設定入口を変える3つの環境変数は、起動時は非空、設定の `env` では存在するだけで exit 20(`host-config-source`)で止める。これら3つは配布実体の静的調査で確認した名前で、公式の対応フラグや現在有効な迂回手段とは扱わない。
  - top-level の `policyHelper`・`policyHelpers` は値を問わず拒否し、helper は実行しない。利用者・管理設定・cache の `disableAllHooks: true` と、管理設定・cache の `allowManagedHooksOnly: true` は exit 20(`hooks-disabled`)で止める。設定値や例外本文は診断へ出さない。
  - 試験用の `DEV_WORKFLOW_LOOP_TEST_MANAGED_DIR`(`loop-selftest.sh` が使う): その下の `managed-settings.json` と `managed-settings.d/*.json` を、見る管理設定に足すだけ(既定の置き場は必ず見る。止める向きにしか効かない)
- 効き方(保護パスの書き込みで hook が呼ばれること・リポジトリ側の設定を読ませない起動のもとで起動引数で渡す hook が効くこと・`reviews-dir.sh` が確認に回らないこと・利用者の側の hook と重なったときの挙動)は実走のプローブで確かめる(§8)

## 10. 受け入れる限界

- Linux 専用(`setsid`・`flock`・`/proc` を使う)。`inherit_errexit` を使うため bash 4.4 以上が必要。ほかの OS・古い bash では初期化前に止まる
- 片付けは監督の所有する子孫を、開始時刻・pidfd・waitpid で追跡する。印の削除・環境の変更・別セッション化は回収から外れる理由にならない。監督の強制終了や確認不能では、結果を成功として使わず止める
- 許可リストは誤操作を減らす仕組みで、隔離ではない。python3・bash を許した時点で実質のコード実行になり、周は利用者の権限で、利用者が書ける場所ならどこでも書ける。
  - `loop.sh` は共有の Git 状態に加え、§4 の配布物・利用者設定・Git 設定・既知 shell 設定の持続的変更を照合する。列挙外のファイルと、検査間の復元や親の保持値への攻撃は保証しない。 開始時に在る全 worktree の通常 path は内容 hash、機密 path は本文を開かない metadata の比較を維持する。新しい設定 origin と未確認の active graph は本文を採用する前に停止する。
- `--mcp-config` を渡さないと、周では MCP が使えない。MCP を前提にする工程(メモリの同期など)は各 skill の「無い場合」の経路で動く
- 同じ版でも内容が同一とは扱わず、有効 plugin の実体を開始時の信頼集合へ含める。コピーと導入済みの両方を照合し、開始後の変更を拒否する。開始時点で信頼できる内容かどうかは利用者が確認する。
- 実行中に人がデフォルトブランチを動かす(pull など)と、周の後の照合で `refs/heads/<DEF_NAME>` の変化として止まり、止めの印が残る。自分の操作による変化だと確かめてから、止めの印を消す
- fetch と push の URL が別のリポジトリを指すか、`remote.origin.vcs` があれば、起動時に exit 20 で止まり(§2 の origin の URL の検査)、どちらのモードも回らない(発見モードでは refactor も回らない)
- 次は `loop.sh` が見ない・塞がない(経路は #107 に記録する)
  - §4 の信頼集合に含まれない、リポジトリ外のファイルへの書き込み
  - 子が共有状態の照合を通った**後**に行う書き込み。親は検証済み after-state の hash を保持し、実削除した own worktree の四集合だけを派生して最終観察と比べるため、その最終観察まで持続した変更を新しい基準へ取り込まない。ただし最終観察後から atomic な昇格までの同一権限の競合を完全には検出せず、すでに終えた周へ巻き戻しはしない
  - 監督自体を強制終了した後の子孫や、同 UID による親の保持値・実行物の改変。non-dumpable の子孫自体は所有関係で回収するが、監督が攻撃されても回収できるとは主張しない
  - binding・`last-verified.json`・`inflight/`・stop-mark は同じ利用者の権限で書ける。no-follow の検査は管理入口・配置の差替えと旧形式を検出するが、内容の真正性を保証しない。scratch では `base.json` だけを現状 snapshot の形に置換すると、anchor・`last-verified.json`・meta が不変でも preflight が通ることを確認した。この結果だけで完全な再開が成功するとは主張しない。#192 は未信頼の `inflight` を環境 bootstrap 前に拒否して再開しないが、同じ利用者が binding と全 state を同時に削除・改竄すれば新規入力と区別できない。単独の書込み主体にし、reinit・移転・明示 rebind の前に人が旧 state と残った子を確認する
- ホスト CLI の証明(§2 の 12。#107 H31)の限界
  - 証明は署名ではない。同じ利用者の権限は、証明のファイルも実体も書き換えうる。検出できるのは、証明を取った後の更新・書き換え・差し替え(実体の値か出力が変わったもの)と、許可の仲介の hook が呼ばれない版(`--prove-host` が証明を書けない)
  - 結び付くのは入口のファイルだけ。shebang のスクリプト(先頭の行で、実行するプログラムを指定するスクリプト)の形のホスト(npm の導入など)では、interpreter(スクリプトを実行するプログラム)・同じパッケージのほかのファイル・`NODE_OPTIONS` などの環境は結び付かない(証明の `kind` に `script` と出る)。運用: ループには native の導入を使うか、interpreter とパッケージの置き場を人だけが書ける場所に置き、`NODE_OPTIONS` などを外して起動する。`kind` が `script` なら、それらを変えたときにも `--prove-host` を打ち直す
  - サーバ側の機能の切り替えも結び付かない。成り立つ条件: ホストがサーバから機能の切り替えを受け取り、hook や許可の判定の挙動が変わる。影響: 実体・版・help が同じままでも、証明を取った時と挙動が違いうる(許可の仲介が呼ばれなくなるなど)。検出: `loop.sh` は自動では判定しない。周の報告の許可の仲介の行(allow・deny の件数。子が書ける参考表示)で、deny が 0 件なのに `G1` の保留がある周を、人が見て疑う(§9)。素通りで許可される向きは、この行でも見えないことがある。運用: 周の報告の許可の仲介の記録で、hook が呼ばれていることを人が見る。疑いが出たら `--prove-host` を打ち直す。打ち直しが止まっても古い証明は残って使われ続けるので、証明のファイル(パスは起動時の報告の「ホスト CLI の実体」の行)を退ける(次の起動から exit 20・`host-proof` で止まる)。証明と定義の要約は起動時と毎周の直前に照らすが、同じ実体での server 側の変化はそれだけでは見分けられない。疑わしい実行中のループは停止ファイル(§6)で次の周の前に止める。走っている周も止めるなら `loop.sh` に TERM を送る(128+シグナル番号で終わる。残るものは受けた時点で分かれる。§4 の「残ったときの終わり方」「シグナルと内部の失敗」「昇格中のシグナル」のとおり。人が報告を確かめて片付ける)
  - 照合してから起動するまでの間の差し替え(同じ利用者の競合)は防げない。周の子は保持した realpath で起動し、周を始める前に内容の sha256 まで、起動の直前に stat の値で照らす。起動の直前(周の途中の印を置いた後)に環境の照合で止まったときは、ほかの環境の照合と同じく印と worktree が残り、人が確かめて片付ける
  - `--prove-host` は打った時点の実体を信頼する。実体が偽物なら、子の環境で受け取る記録の置き場に拒否の行を書き、結果の JSON を作って、確認を装える。確認の判定は、正規の実体が許可の仲介の hook を呼ぶことを確かめるもので、実体の真正性は確かめない
  - ホスト CLI を更新するたびと、`loop.sh` の起動の形(hook の設定の雛形など)が変わる版に上げたときに、人が `--prove-host` を打つ運用になる。native の導入は、版ごとの置き場(`~/.local/share/claude/versions/<版>`)に新しい版を置いてリンクを張り替える形と推測される(実ホストの確認は自動更新を止めて行ったので、更新の挙動は未確認)。その形なら、実行中のループは控えた版のまま続き、次の起動で証明を求める。同じ置き場の実体が書き換えられ・差し替えられ・消されたとき(npm の導入など)は、次の補助の CLI か周の子の起動の前に止まる。ループ用の環境では `DISABLE_AUTOUPDATER=1` で更新の時機を人が決めることを推奨する(事実と出典は design §7-3)
  - `--allow-classifier` の無人ループは起動しない(`loop.sh` が拒否する)。2.1.289 では、確認の `Write`(worktree の外)を分類器が許可の仲介の hook を通さずに許したため、証明を書けない(#107 の H48)
  - 任意の環境変数やログインの全体は証明に結び付かない。H46 の対象となる定義の論理的な root・配置・本文と、導出した固定設定は結び付く。HOME と設定ディレクトリの起動条件、保持した設定の `env` と helper は引き続き H50 の検査を通る。保持後に残る変更は次の照合で止まるが、同 UID による検査間の変更と復元は排除しない。
  - 正規の server 管理者は信頼範囲に残る。起動時・定期取得時の将来応答、cache に保存されない `-p` 用応答、その適用から次の照合までの作用は事前に固定も拒否もしない。cache の新設・変更は次の照合で止まるため、正当な更新でもループが止まる場合がある。同じ UID が検査間だけ改変して戻す競合は排除しない。macOS/Windows の管理設定は実装対象外で、WSL の Windows 管理設定継承も管理者信頼の範囲に残る。
  - 許可リストやログインの全体を証明するものではない。道具名だけの許可規則で仲介 hook を通らない呼出しが成立する H47 は未解決で、H46 の定義検査で塞いだとは扱わない。定義・公開状態・固定設定の変化は新しい証明を必要とするが、失敗した確認は古い証明を削除しない。検査対象外の変化に疑いがあれば、人が変更を戻すか証明を退け、実行中のループも止める。
  - 実ホストで行っていない確認(未確認): 1 回の起動の中でのリンクと対象の差し替え・npm(スクリプトの形)のホストの実体・証明を使った実際の周・同じ実体で `--help` の出力だけが変わる場合(単体の回帰と selftest だけ。実ホストでは版の違う実体に差し替えて止まることを見た)。
- 追加定義と権限の検査(§4。H34・H46)の限界
  - 検査対象の hook・mod・agent・選択された marketplace 項目は §4 の条件で拒否するが、利用者設定へ直接書いた hook の意味は解析しない。設定は保持して変更を検出するものの、開始時の正当性は利用者を信頼する。許可した定義本文の任意の実行を隔離する仕組みでもない
  - 管理側の既知の定義 root は検査対象だが、実 system 設定を変えた実ホスト確認は行っていない。追加 root の fixture の結果だけで、他 OS の管理設定や macOS の問題(#226)を解決済みにしない
  - 既知の組込み名は固定設定で止め、期待しない公開名は証明の初期応答で拒否する。ただし未知の組込み定義が外部定義と同じ名前なら、名前の集合だけでは出所を区別できない。ホスト実体の信頼範囲に残る。初期応答の確認は `--prove-host` の時点だけで、毎セッションの出所の保証ではない
  - 同期を止める固定設定は付けるが、同 UID の書込みや将来の正規 server 応答は排除しない。持続した追加・変更は次の照合で止まる。それまでの作用と、変更して戻す競合は防げない。保持値・reader・ホスト実体も同じ権限で攻撃できる限界は残る
  - 敏感な frontmatter は自前の厳しい reader で必ず検査し、型や読み方が不明なら拒否する。許可規則は PyYAML が使える場合に追加照合する。将来のホストの YAML や名前解決のすべてと一致する保証ではない。仕様が変わったら resolver と検証結果を見直し、証明を取り直す
  - 許可規則の包含は公式文書の照合の規則を前提にする。文書にない違いで、狭いと判定した規則が広く効く可能性は残る。解釈が分かれる形は拒否する。裸の許可規則の問題 H47 は別の課題として扱う
- 保護パスを変えるタスク(W の外の保護パス〈`.claude/settings.json`・`.husky/`・`.vscode/` など〉への書き込みが要るタスク)は、無人では完了しない。許可の仲介の hook が拒否し、`G1` の保留か失敗扱いになる。人が対話で行う
- 利用者の側に `PermissionRequest` の hook があると、許可の仲介の hook と重なって両方が動く(公式文書の hooks。挙動は実走で確かめる — §8)
- hook は隔離ではない。周は同じ利用者の権限で動くので、hook の本体も書き換えうる。固定 loader は次の要求で持続的変更を拒否するが、親やホストの保持値・実行環境そのものの変更は防がない。hook のすり抜けの形は #107 に積む
- H25 の拒否は、この hook に届く Bash 入力の既知のサンドボックス無効化項目だけが対象。サンドボックスを有効にする変更ではない。hook に届かない操作、別の hook による許可、未知の無効化項目は保証しない。
- 利用者の shell が `cd` を関数・alias で上書きしている(`cdable_vars` も同じ型)と、前置きの続きは上書きが移した先で動く。許可リストのコマンドの上書きと同じく、利用者の shell の設定は信頼の範囲
- profile で外部ランナー(レビューや実装に使う外部の AI のコマンド)のレビュー(`features.runners`)を宣言したリポジトリでは、無人の周で外部ランナーの一時ツリー(外部のレビュアーに見せるために作る、ファイル一式の一時的な写し)を作る手順(external-runners.md §9-1。`$` を含み、worktree の外に書く)が確認に回れば、許可の仲介の hook に拒否されうる(実走で確かめる)
- 保護パスの下の task_dir(例 `.claude/tasks`)は、無人ループの対象外。タスク MD のパスを Bash の引数に渡す操作(本文ダイジェスト〈本文から計算した短い値。本文が変わると値も変わる〉の算出・保留のガード・`git mv` など)を許可の仲介が拒否するため、`loop.sh` は最初の周の §3 の 3 で止まる。無人に回すなら task_dir を `docs/tasks` など保護パスの外へ移す
- 許可の仲介の記録は参考表示のみ。壊れた行・切詰め・欠落・symlink・FIFO・上限超過は読取不能として報告する。内容や読取不能で停止回数を緩めない。
- コマンドや出力を書き換える利用者の側の `PreToolUse` の hook(実例: コマンドを別のプログラムの呼び出しに書き換えるもの)は、周の中でも動く。許可の判定を飛ばすか、書き換えた形で許可の仲介に届く(許可リストに書き換え後の先頭の語が要る)。出力も書き換えうるので、無人の手順の照合(`-z` の一覧など)を崩しうる。出力を削る書き換えは、無人の照合を誤って通しうる(安全が弱まる向き)
- 自動メモリの機能は子起動時に無効化する(§4、#107 H32 / #151)。ただし、同じ置き場を通常の `Read`・`Write`・Bash で直接読み書きすることは防がない。利用者の hook、環境変数を消すか変更して起動した別プロセス、別種の永続メモリも保証の外。既存メモリは削除せず、変更の監視もしない。一般のファイル書き込みで残された内容は、以後の人のセッションへ持ち越されうる。`loop.sh` を通さない直接の skill 起動には、この無効化は付かない。
- 周の中のサブエージェントの G1 の順守(拒否の後に打ち直さない・報告する)は、機構では保証されない。打ち直した形が許可リストで通るときは、許可の仲介に届かない。周の報告の許可の仲介の記録と拒否の欄(§7)で、人が見つける
- 疎通(§2 の 13。セッションを起動しない認証の確認)は、OAuth の期限切れで更新できない認証を見抜けない(終了コード 0・ログイン済みの表示のまま、周は API のエラーで失敗する — design §7-3)。その場合は、連続失敗で止まるまでの周(既定 2 周)のタスクが、失敗の worktree を残して以後の選定から除外される(人が認証を直し、残った worktree を片付けてから打ち直す — §5・§7)
- ほかのアプリ(例: 別のエージェントのデスクトップアプリによるセッションの取り込み — design §7-3)が周の worktree・人のチェックアウトに書くことは防げず、書込元も識別しない。開始時に在る人の checkout への変更は、次の観察境界で通常 path は内容 hash、機密 path は metadata の差分として検出する。検証済み after-state と最終観察の strict 比較まで持続した変更は基準へ昇格しないが、最終観察後から atomic な昇格までの同一権限の競合を完全には検出しない。既に終えた周へ巻き戻しはしない。周の worktree は、正常でも消せなければ報告に残し、失敗ならそのまま残す(§5)
  - 発見モードでは、判定の前に周の worktree に書かれたものは未追跡の残りとして判定の失敗になり、worktree を調べて消すまで、その発見元を止める(実装モードの「正常なら残して報告」と違う — §11)。判定の後に書かれたものは、実装モードと同じく worktree を残して lock し直す
- 発見モード(§11)の限界
  - squash・rebase で merge したブランチと、閉じた PR のブランチは、消すまでその発見元を止め続ける(祖先にならないため。報告に片付けの定型を出す)。未 merge の前夜の候補のブランチがある発見元は回さないので、前夜の候補を merge しない限り、その発見元の新しい候補は出ない
  - 周が今夜の名でないブランチを push しても、`loop.sh` は見ない(判定が見るのは、周の worktree の HEAD と今夜の名のブランチだけ)
  - 重複の除去(既知の指摘キーとの突き合わせ)は、周の中の候補モードが行う([candidate-mode.md](../../create-task/references/candidate-mode.md))。`loop.sh` は候補の中身を見ない
  - 公開の確認(discover-mode.md §5)の後に公開に変えられた場合は防げない
  - 読むだけの調査をツールで行うことも、Bash の書き方と同じく指示で守るだけで、機構では保証しない。委託した 1 体の Bash が拒否されると、周全体が G1 で失敗し、人が片付けるまでその発見元は回らない(data-audit は調査の 4 体を並列に回す。#69 の実走の 2 晩目で起きた)
- 周に MCP のツールを渡すと(`--mcp-config`)、許可リストに無いツールは確認に回り、許可の仲介が「扱わないツール」で拒否する。発見モードの refactor の分析は、MCP を渡さなければ、Read と、Glob・Grep(許可リストで戻したとき)か Bash の `find`・`grep` の経路で動く

## 11. 発見モード(`--discover`)

発見ループ(Issue #69)の外側。周ごとに発見元を 1 つ選び、`/ship-task --discover=<発見元> --unattended` を回す。周の中(候補モードの呼び出し・照合・commit・push・PR・結末)の正本は [discover-mode.md](discover-mode.md)。この節には、実装モード(§2〜§7)との違いだけを書く。書いていない点は実装モードと同じ。

**引数**

- `--discover`(既定の発見元の列)か `--discover=<名>[,<名>…]`(引数の順)。発見元の列の正本は discover-mode.md §1。`loop.sh` の既定の列がそれと同じ(順も)ことは `loop-selftest.sh` が照らす
- 使い方の誤り(exit 2): 空の要素・重複・列の外の名・`--discover` を 2 回書く・`--only` との併用。値は `=` の形だけ(`--discover data-audit` の `data-audit` は「不明な引数」)
- 既定オフ: `--discover` が無ければ実装モード(§2〜§7)
- 1 回の実行はどちらかのモードだけ(両方を毎晩回す書き方は §8)

**起動の前提**(§2 の順のまま。違いだけ)

- 兄弟のスクリプトの検査(§2 の 4)と origin の URL の検査(§2 の 10 の後)は §2 のとおり(両方のモードに共通)
- 追跡外のタスクの検査(§3 の 3a)は行わない

**キューと選定**(§3 の置き換え。1〜3 は同じ)

- **状態ファイルの前提**: §3 の 2a(実装モードと同じ)
- §3 の 3 の task_dir(`候補_` を書く場所)が保護パスの下なら exit 20(実装モードと同じ)
- キュー: 発見元の列(引数の順)。1 発見元 = 1 周
- 読み飛ばし(上から見て、最初に当たった理由を報告する)
  1. この実行で回した(同じ実行で同じ発見元を 2 度回さない。周の報告にあるので、読み飛ばしには出さない)
  2. 今夜の名のブランチ `task/候補-<発見元>-<固定した sha の先頭 12 桁>`(discover-mode.md §2)が、ローカル(`refs/heads/`)・追跡用の ref(`refs/remotes/*/`)・origin(`refs/heads/`)のどれかにある(祖先かどうかに関わらない)
  3. 名が `task/候補-<発見元>-[0-9a-f]{12}` に完全一致するブランチ(同じ 3 か所)のうち、固定した sha の祖先でないもの(か、sha がローカルに無いもの)がある(= 未 merge の前夜の候補)
  4. lock の理由がちょうど `dev-workflow-loop: 候補:<発見元>` の worktree がある(失敗の周が残した除外の印)。同じ理由の全パスを読み飛ばしの理由に示し、各パスの `git worktree unlock` → `git worktree remove` を片付け案内に出す。空白などを含むパスは 1 引数として扱えるよう引用する。1 件だけ片付けても、同じ理由が残る間は読み飛ばす
  - 2・3 は、報告にブランチの消し方の定型を添える(ローカルは `git branch -D`、origin は `git push origin --delete`、追跡用の ref は `git branch -dr`)
  - origin の一覧は、選定ごとに 1 回の `git ls-remote origin 'refs/heads/task/*'`(ネットワークの規則。失敗は終了コード 30)
- 回せる発見元が無ければ、worktree を消して終える(exit 0。停止の理由は「キューが空」)
- 先頭の発見元を選び、lock の理由を `dev-workflow-loop: 候補:<発見元>` に付け替える(タスク MD のパスは `.md` で終わるので、実装モードの理由と重ならない)

**1 周**(§4 の違いだけ)

- プロンプト(stdin): `/dev-workflow:ship-task --discover=<発見元> --unattended`
- 周の途中の印の meta: `name=候補-<発見元>-<先頭 12 桁>`・`rel=候補:<発見元>`・`mode=discover`・`source=<発見元>`。meta は中断の説明用であり、再起動時の操作やモード判定の根拠にしない

**判定と後片付け**(§5 の置き換え。材料は終了コード・結末の行・git の状態)

- 共有状態の照合で、保持した exact ref と異なる遷移を見つけたときは、この判定より前に stop-mark を残して exit 10 とする。新しい Git 通信や PR の雛形は出さない。既に公開済みである可能性は人が確認するために報告へ残し、後段の候補なし・正常・失敗の coverage は、共有状態が正常な独立の対照で検証する。

共通の条件 G(すべて満たす):

- HEAD が `refs/heads/task/候補-<発見元>-<先頭 12 桁>`
- HEAD の親が固定した sha だけで、`git rev-list --count <sha>..HEAD` = 1
- `git diff --no-renames --raw -z <sha> HEAD` の全行が、状態 `A`・モード 100644・パスが `<task_dir>/候補_<名>.md`(task_dir の直下・§3 の文字の制限を満たす・`<名>` が空でなく `-` で始まらない)で、1 件以上
- 周の worktree に未追跡・未 commit が無い(`git status --porcelain=v1 -z --untracked-files=all` が空。ignore 済みは除く)

| 判定 | 条件(すべて) | 後片付け |
|---|---|---|
| 正常(PR) | 終了コード 0 / 結末 `PR` / G / origin の `refs/heads/<ブランチ>`(`ls-remote`)= HEAD | worktree を消す |
| 正常(縮退) | 終了コード 0 / 結末 `縮退` / G / origin があれば、1 回の `ls-remote` で、origin に今夜の名のブランチが無いか、その sha = HEAD(違う中身を push したまま、人に PR を作らせない) | worktree を消す |
| 正常(候補なし) | 終了コード 0 / 結末 `候補なし` / HEAD が detached で固定した sha のまま / ローカルに今夜の名のブランチが無い / origin があれば、1 回の `ls-remote` で origin にも無い / 周の worktree に未追跡・未 commit が無い | worktree を消す |
| 失敗 | 上のどれにも当たらない(結末 `失敗扱い`・`保留`・時間切れ・終了コードが 0 でない・結末の行が無い・JSON が読めない・`進行中_` などの変更・入れ子・既存ファイルの書き換え・名前空間の外・文字の制限に外れる名・未追跡の残り・origin の今夜の名のブランチが判定した HEAD と違う `縮退`・判定の `ls-remote` の失敗) | worktree を残す(lock の理由 `dev-workflow-loop: 候補:<発見元>`。以後その発見元は読み飛ばす) |

- 消す手順は実装モードと同じ(`.claude/reviews` を写す → unlock → `git worktree remove`〈`--force` なし〉)。拒否されたら(判定の後に何かが書かれた)、worktree を残して `dev-workflow-loop: 候補:<発見元>` で lock し直し、報告に出す(失敗には数えないが、以後その発見元は回さない — 決定録 2026-09-23 の決定 6「未 commit が残った周は以後拾わない」)
- origin の今夜の名のブランチが判定した HEAD と違う `縮退` は、報告に「origin のブランチが判定した中身と違う。PR を作らずに消す」を出す
- 失敗の周では、理由を問わず、origin に今夜の名のブランチがあるかを 1 回の `ls-remote` で確かめる。あれば「PR が開いている可能性がある。merge せずに閉じ、ブランチを消す」を報告に出す(2026-09-23 決定 13 の最後の歯止めは、人が merge しないこと)。`ls-remote` が失敗しても止めず、「確かめられない」と報告する
- 発見の周に保留は無い(discover-mode.md §9)。結末 `保留` は、前の行に別の値があっても最後の行なら失敗

**停止条件**(§6 の違いだけ)

- 許可の拒否の保留の連続は使わない(保留が無い)。許可の拒否(G1)の周は失敗扱いで終わり、連続失敗に数える
- ほかは同じ(連続失敗・共有の状態の変化・片付けで残ったプロセスなど)

**朝の報告**(§7 に足す。発見モードだけの項目)

- 起動時: 「モード: 発見(発見元の列・既定か引数か)」。最初の選定で、発見元の列(task_dir と今夜の名のブランチ)
- 周ごと: 発見元・今夜の名のブランチ・結末・判定・PR の URL(結末の詳細)・候補の件数とパス・`.claude/reviews` を写した先(候補モードの報告 `candidates-<発見元>.md`・data-audit の監査の報告 `data-audit-iter*.md`)・許可の仲介の要約
- `縮退` の周: push したか(判定の `ls-remote` で見た値。origin のブランチ = 判定した HEAD なら push 済み)で分ける。どちらも、処理するまでその発見元は回らないことを添える
  - push したとき: push 済みのブランチ・手で PR を作るコマンドの雛形 `gh pr create -R <HOST/OWNER/REPO> --head <ブランチ> --base <デフォルトブランチ>`・作らないときの消し方
    - `origin-repo.py` の `repo` が null か、`gh repo view` の終了コードが 0 以外のときは、push が成功しても PR を作らず `縮退` になる。公開手順は [discover-mode.md](discover-mode.md) §8 を参照する。
    - どちらの理由でも、`loop.sh` は `<HOST/OWNER/REPO>` を実名で自動補完しない。雛形を使う人が実名で埋める。
  - push していないとき(origin が無い・`--no-pr`): ローカルのブランチを merge するか、消す手順
- 終わり: 発見元ごとの要約・読み飛ばした発見元と理由(片付けの定型つき)・`縮退` の周のブランチ・片付けの定型(merge commit で merge して pull した後・squash や rebase で merge したとき・PR を閉じたときのブランチの消し方)・採用の手順(merge の後に人が `/create-task <候補_ のパス>`)
- `--dry-run`: 起動の前提と 1 回の選定(状態ファイルの前提を含む)を行い、発見元の列(task_dir と今夜の名のブランチ)と読み飛ばし(片付けの定型つき)を出して終わる

**MCP の許可**

- 周に MCP を既定では渡さない。`--mcp-config` を渡さなければ周に MCP は無く、refactor の分析は、Read と、Glob・Grep(許可リストで戻したとき)か Bash の `find`・`grep` の経路で動く
- MCP のツールを渡すと、許可リストに無いものは許可の仲介が「扱わないツール」で拒否する(§10)

**実走**(受け入れ。人が素の端末で行う。§8 の型に倣い、手順と合否は Issue #69 の実走)

- 使い捨ての private リポジトリの clone で、`--discover --dry-run`(発見元の列が data-audit → refactor で、読み飛ばしが無い)→ 1 晩目の `--discover`(2 周とも正常〈PR〉で、PR の差分が `A <task_dir>/候補_*.md` だけ)→ 続けて `--discover --dry-run`(2 つとも今夜の名で読み飛ばし、片付けの定型が出る)→ 朝に人が見送り・消す・merge → 2 晩目(既知の判定が働く)と見る
- `--discover` なしの `--dry-run` は、実装モードと同じ形(既定オフ)。発見モードの後に実装モードを回して、タスクの周が正常(PR)になること(結末の行の選び方を変えた後も通る)
- 公開の確認の陰性の対照(push 権の無い公開リポジトリの clone に、非公開の `upstream` を足した構成)で、data-audit の周が正常(候補なし)で終わり、ブランチも push の記録も無いこと

### 子孫の監督と引数検査の保証範囲(#204)

- loop は従来どおり Linux 専用である。監督には Linux subreaper・`/proc`・Python の `os.pidfd_open` と `signal.pidfd_send_signal` が必要である。必要な機構を使えない場合は子を起動しない。引数だけの `host-argv.py` は標準ライブラリだけで動き、PyYAML や認証を追加で要求しない。
- 通常の子・別セッション・環境消去・non-dumpable・二重 fork・孤児は、監督の所有関係で回収する。同じ UID の無関係なプロセスを広く走査して停止しない。
- 同じ UID が監督自体を KILL した場合、残存子孫の回収を保証しない。成功記録が無いため次の周へ進まず、途中の印と worktree を残す。親を KILL した場合は監督の親終了通知で回収を試みるが、再開の承認には使わない。
- 同じ UID が親の保持値・実行物・結果記録まで書き換える、または観測の間で戻す攻撃を隔離する仕組みではない。結果の nonce だけを秘密や改竄不能な証明とは扱わない。任意コードを敵対的に実行する環境では、別 UID や OS の隔離と外部からの監査を使う。
- 実ホストの PermissionRequest 動作と `allowed-tools` の権限確認は #193 で行った(§2 の 12・§4・§8)。#204 の引数・子孫の回帰成功で代替しない。
