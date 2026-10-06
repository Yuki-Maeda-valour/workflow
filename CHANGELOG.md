# Changelog

v4.2.0 以前は commit 履歴を参照。

## v4.22.0

### 変更

- skill の本文が決める、人に出す報告の型(見出し・項目名・質問・選択肢)の内部の言葉を、ふつうの言葉にした。字面を残す決まった語と名前には、型の中に短い説明を添えた
- `/understand-project` のサマリーの見出し「🔺 ドリフト検出」を「🔺 文書や設定と実際との食い違い」に、`/tool-check` の結果の表の列名「ゲート」を「検査」に変えた
- 設計書(`docs/design.md` §6)に、報告の型の言葉の決まりを足した(references が決める型は、言葉の整理の Issue で同じ決まりを当てる)

## v4.21.3

### 修正

- `loop.sh` が、値を取る短いフラグの単独・束ね書き・値の連結形を、子セッションの起動前に拒否する([子 Issue #174](https://github.com/Yuki-Maeda-valour/workflow/issues/174)、親 #107 の H28)。後ろに足す固定フラグの値消費を防ぐ。
- 長い `--名前=値` と既存の禁止フラグ検査は維持する。短い引数は `=` の後ろも保守的に検査する。保証範囲は [loop.md §2](plugins/dev-workflow/skills/ship-task/references/loop.md#2-起動と前提の検査) を参照。
- help・引数解析のスタブを使う全体回帰で、拒否理由と子セッション未起動を確認する。

## v4.21.2

### 修正

- `loop-permission.py` が、worktree 自体や、許されない保護対象を含むディレクトリの再帰削除を拒否する([子 Issue #173](https://github.com/Yuki-Maeda-valour/workflow/issues/173)、親 #107 の H26)。対象と子孫に既存の削除規則を適用し、列挙・情報取得に失敗した場合も許可しない。
- 絶対・相対パス、末尾 `/`、`.`・`..` を解決して検査する。H36 のリンク自体の削除判定と通常削除・W の例外を維持し、末尾 `/` 付きリンクの再帰削除ではリンク先を検査する。子孫のリンク先は辿らない。
- scratch での hook・実削除の確認と、検査不能・深いディレクトリ・worktree とプラグイン間のリンクの回帰試験を追加した。

## v4.21.1

### 修正

- `loop-permission.py` が、Bash 入力のサンドボックス無効化指定と、その項目の不正な型を拒否する([子 Issue #172](https://github.com/Yuki-Maeda-valour/workflow/issues/172)、親 #107 の H25)。コマンドが許可対象でも拒否し、未指定・真偽値の偽だけを通常判定へ進める。
- 通常の説明・タイムアウトなどの入力は維持する。実際の hook 入出力と判定ログを回帰検証する。拒否範囲と限界は [loop.md §9](plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み) を参照。

## v4.21.0

### 変更

- skill 本文の人に聞く場面を「質問で確認する」「複数選択の質問」などと書き、ホストの道具の名前を外した
- 解決表(`do-task/references/delegation-map.md`)に質問の節を足し、ホストの道具への対応づけを置いた
- 書き方の正本(`do-task/references/writing-for-people.md`)の「質問と選択肢」に、1 回の数の確かめ方と、質問の仕組みが無いホストでの聞き方を足した。仕組みが無いホストでは、すべての skill が、質問と同じ内容を文で聞き、答えを待つ。1 回の質問で聞くと決めた問いは文でもまとめ、前の答えで次の問いが変わるときは 1 つずつ聞く。聞く順とまとめ方は各 skill の決まりが先
- 検証器 `scripts/validate.py` のホスト CLI 語の検査に `AskUserQuestion`・`multiSelect` を足した(大小を問わない)

### 移行方法

- 独自の skill に `AskUserQuestion`・`multiSelect`(大小を問わない。UI 部品の名前 `MultiSelect` も含む)を書いた fork は、`validate.py` が ERROR を出す。ホストに依らない言い方に書き換えるか、正当な記述なら理由つきの `<!-- validate-allow: 理由 -->` で外す

## v4.20.6

### 修正

- `loop-permission.py` が、`builtin`・`command` 経由の移動と `pushd`・`popd` を許可リストより先に拒否する([子 Issue #166](https://github.com/Yuki-Maeda-valour/workflow/issues/166)、親 #107 の H37)。重ねたラッパー・オプションと、複合コマンドの全位置を調べる。
- `!`・`time` の前置きや `if`・`for` など、移動を追跡しない制御構文も拒否する。空引用を含む引用語、情報照会、通常操作、既存の2種類の `cd` とH36のsymlink保護は維持する。
- scratchの実Bashによる書換えの陽性対照と、同じコマンドの実hook入力を回帰検証する。拒否範囲と限界は [loop.md §9](plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み) を参照。

## v4.20.5

### 修正

- `task-digest.py` が対応外の字下げのフェンスを算出不能として拒否する([子 Issue #168](https://github.com/Yuki-Maeda-valour/workflow/issues/168)、親 #107 の H10)。フェンスの外で、半角スペース 4 個以上かタブを含む字下げの直後に、同じバッククォートかチルダが 3 個以上続く行を検査する。閉鎖の有無や追加修正記録の節への配置で検査を逃れない。
- 正常な半角スペース 0〜3 個の囲みの計算値と、囲みの中の字下げ文字列を保つ。未閉鎖の拒否・通常ファイル限定の読み込み・既知キー収集と候補検証の JSON 契約も維持する。拒否条件と期待出力の書き方は [task-template.md の記法の規約](plugins/dev-workflow/skills/create-task/references/task-template.md) を参照。

## v4.20.4

### 修正

- `loop.sh` が、人の linked worktree の `config.worktree` の追加・削除・値変更・項目の並べ替えを検出する([子 Issue #167](https://github.com/Yuki-Maeda-valour/workflow/issues/167)、親 #107 の H20)。通常終了・シグナル終了・中断後の再起動で既存の停止規則を適用する。利用者の設定は自動復元・削除しない。
- 中断後に起動元を変えた場合と、人の設定の比較元が無い旧 `inflight` は停止し、前の起動元の設定確認を促す。中断の無い実行間の変更や旧 `last-verified.json` は報告して続ける。詳しい再開手順と限界は [loop.md §4・§10](plugins/dev-workflow/skills/ship-task/references/loop.md) を参照。

## v4.20.3

### 修正

- `origin-repo.py` が SSH のホスト鍵検証を外した構成を信頼判定から除外する([子 Issue #161](https://github.com/Yuki-Maeda-valour/workflow/issues/161)、親 #107 の H44)。検証設定・照合名・ローカル宛先の検証省略と、`accept-new` の先頭保存先を確認する。必要な設定が欠落・空・重複した場合や取得に失敗した場合も拒否する。
- 通常の SSH・HTTPS と JSON 形式を保ち、理由に設定値や認証情報を出さない。判定は設定に限り、実際の鍵内容や保存成功は検査しない。保証範囲は [discover-mode.md §3・§10](plugins/dev-workflow/skills/ship-task/references/discover-mode.md) を参照。

## v4.20.2

### 修正

- `loop.sh` で、同じ lock 理由の worktree が複数あると報告から漏れる問題を修正した([子 Issue #160](https://github.com/Yuki-Maeda-valour/workflow/issues/160)、親 #107 の H42)。開始・終了の報告、実装／発見モードの読み飛ばし理由、発見モードの片付け案内に全パスを出す。
- NUL 区切りの読取りを維持する。空白・改行を含むパスを分割せず保持し、同じ理由の worktree が 1 件でも残れば読み飛ばす。既存の残存 worktree を自動削除する処理は追加しない。
- 実 worktree を使う回帰で、0 件・1 件・同理由の複数件・異なる理由・空白／改行パス・再読取りと候補への復帰を確認する。

## v4.20.1

### 修正

- `loop-permission.py` が最終 symlink の実体も保護判定する。無害な名前のリンクを経由する保護パスへの Bash の読み書きを拒否する([子 Issue #159](https://github.com/Yuki-Maeda-valour/workflow/issues/159)、親 #107 の H36)。裸名の既存リンクも引数・オプション値・代入値で検査する。`--` 後の `-` 始まりのリンクも調べ、別コマンドの削除・移動で読み取りの拒否が取り消されないようにする。
- リンク自体の削除・移動と、通常パス・状態ファイル領域 W の許可を保つ。多段・相対・不存在対象・worktree 外のリンクと、拒否の種類を回帰検証する。保証範囲は [loop.md §9](plugins/dev-workflow/skills/ship-task/references/loop.md#9-許可の仲介保護パスへの書き込み) を参照。

## v4.20.0

### 変更

- 12 の skill の本文の言葉をそろえた。手順と条件は変えていない
- 置き換えた語: 守る値 → 控える値・対話点 → 人に確かめる場面・報告点 → 判断を仰ぐ点
- 「縮退」を意味ごとに分けた: 段階を下げて続ける・内蔵に切り替える・代わりの手段に切り替える。結末の値の `縮退` は変わらない
- 内部の言葉には、各 skill の本文で初めて出す所に言い換えの括弧を添えた
- 200 字を超える長い行は、1 文 1 つの箇条書きに分けた。条件と結果は同じ行に置いた。収まらないときは括弧の前後で分け、それでも収まらないときだけ条件の下に 1 段深い行を置いた
- 無人の周の「失敗扱い」の説明を、止まる前にできたもの(commit・push 済みのブランチなど)は残る形に直した
- 言い換え表(`do-task/references/writing-for-people.md`)に、skill 本文での言い方を足した
- 検証器 `scripts/validate.py` に、SKILL.md の本文の行の長さの検査を足した(WARN)

### 移行方法

- 独自の skill を持つ fork では、本文に 200 字を超える行があると `validate.py` が WARN を出す(終了コードは変わらない)。分けるか、そのままにする。分け方の決まりは `docs/design.md` §6 の「行の長さ」

## v4.19.6

### 修正

- `diff-snapshot.sh` の診断表示で、UTF-8 の C1 制御文字(U+0080〜U+009F)が残る問題を修正した。既存の表示用関数が処理する疑い行・承認済み項目とfilter名の NOTE・`pager.*` 無効化の NOTE・promisor表示で、C1を各1個の `?` に置き換える([子Issue #152](https://github.com/Yuki-Maeda-valour/workflow/issues/152)、親 #77 の H2)。
- 通常の UTF-8 と既存の ASCII 制御文字の置換を保つ。承認ダイジェストの計算元、filter無効化用トークン、比較する内容、patch、終了コード、Bash下限と依存は変えない。
- 全32文字の回帰と、旧処理・表示経路の置換漏れを検出する変異確認を追加した。共通関数を通らない別の診断と通常本文の表示は今回の保証外。詳細は [外部ランナー契約 §9-1](plugins/dev-workflow/skills/do-task/references/external-runners.md)を参照。

## v4.19.5

- `loop.sh` の実装・発見の子環境で `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` を強制する(#107 H32 / #151)。親が未設定・`0`・`1` のどれでも子を無効化する。親環境・利用者設定・既存メモリは変更しない。`--dry-run` の子環境表示も同期する。
- 子への受け渡し6ケースと補助CLIの親値保持、設定・メモリ不変を回帰で確認する。自動メモリ機能の無効化は、任意のファイル書き込み禁止とは区別する。
- Linux の隔離環境で Claude Code 2.1.289 の自動メモリ読み込み・保存への効果を確認した。実ループと実ホストを組み合わせた通し実行、macOS での効果は未確認。確認条件と保証範囲は `loop.md` §8・§10。

## v4.19.4

### 修正

- `task-digest.py` は、パス入力の FIFO(名前付きパイプ)など通常ファイル以外を速やかに拒否する(終了コード 1・stdout は空・理由は stderr)。事前検査の後に FIFO へ差し替えられても停止しないよう、非ブロッキングで開いて実体を再検査し、同じ実体から読む([子 Issue #150](https://github.com/Yuki-Maeda-valour/workflow/issues/150)、親 #107 の H11)。
- 通常ファイルとそのリンクの計算値、`-` の stdin パイプ入力、未閉鎖フェンスの拒否(#141)を維持する。特殊ファイルを入力したい場合は、内容を通常ファイルへ保存するか、意図して stdin へ渡す。

## v4.19.3

### 変更

- `diff-snapshot.sh` が、index の gitlink に対応する `.git` 名エントリの無い非空ディレクトリ、パス解決不能または列挙不能なディレクトリを検出して exit 22 で停止する。未追跡内容がレビューから黙って漏れる H1 を修正した（[子 Issue #140](https://github.com/Yuki-Maeda-valour/workflow/issues/140)、親 #77 の H1 のみ）。事前検査と通常生成に共通で、通常生成は診断のみ・patch 非公開となる。承認・除外指定・基準時点の未追跡一覧で回避できない。
- 初期化済みサブモジュールの gitlink 更新と、未初期化・deinit 済みの空または不存在のディレクトリは従来どおり扱う。`.git` 名エントリの真正性・並行書き換え・サブモジュール内部の未コミット内容は今回の保証範囲に含めない。

- gitlink の親や symlink の先に検索権限がない場合を、不存在として通さず停止する。真に不存在の未初期化 submodule は従来どおり許可する。

### 移行方法

- H1 の診断で止まった場合は、内容を保全して誤った gitlink 登録を修正するか、正当なサブモジュールを復旧してから再検査する。`--accept` による復帰はできない。詳細は [外部ランナー契約 §9-1](plugins/dev-workflow/skills/do-task/references/external-runners.md) を参照。

## v4.19.2

### 修正

- 本文ダイジェスト(`task-digest.py`)は、末尾まで閉じないコードフェンスを追加修正記録の節の内外を問わず拒否する(exit 1・stdout は空・理由は stderr)。記録節の未閉鎖フェンスで後続本文の変更がダイジェストから消える問題(#107 H9)を修正した。正常に閉じたフェンス・記録節の除外・既存の正規化とダイジェスト値は変えない

## v4.19.1

### 修正・必要環境

- `loop.sh` は初期化より前に OS → bash 版を検査する。非 Linux・OS 判定失敗は rc 20 / `ERROR [os]`、Linux の bash 4.4 未満は rc 20 / `ERROR [bash-version]`。対応環境の help=0・不正引数=2 は維持する。
- `diff-snapshot-selftest.sh` は bash 4.0 以上と GNU coreutils・findutils・grep の不足を起動前に rc 2 で診断する。`touch -d` の失敗は fixture 不成立による skip と区別して停止する。
- do-task Phase 0 の必要環境・PATH の選択・パス解決の代替の限界を reference にまとめた。回帰一式は Linux と GNU 系ツールを前提とし、loop の Linux 専用は維持する。
- Linux の Darwin スタブと bash 3.2 / 4.3 / 4.4 実体で起動境界を確認。macOS 実機は未確認(実機なし)で、確認手順を reference に残した。

## v4.19.0

### 変更

- 全 skill が、人が読む文(会話の応答・質問・PR と Issue の本文・作る文書・コミットメッセージ)を、共通の書き方の決まり `do-task/references/writing-for-people.md` に沿って書く。プラグインを更新した時点から効く。決まりは、わかりやすさの決まり 8 項目・優先順位・言い換え表・字面を変えない行と語(手順が書式を決めている行と語)・口調の決め方・質問と選択肢の書き方からなる
- 質問の推奨の選択肢には「(推奨)」と書く。`/discuss-spec` の「(Recommended)」もこれにそろえた
- 人が読む文書が diff にあるときのレビュー・`/create-task` の checker の検査・`/ship-task` の最終ゲート(PR 本文)で、この決まりに沿うかを確かめる
- `/init-project`(`--yes` を含む)は、AGENTS.md に「応答の書き方」節を入れる。節は、書き方の決まりの要点と、応答の口調からなる。口調の既定は報告調(段落の先頭に「告。」「問。」などの短い印を置く口調)。質問で、標準(です・ます)と「プロジェクトの指示ファイルに口調を入れない」も選べる
- 権威参照ファイル(AGENTS.md。無ければ CLAUDE.md)に口調を決めた節(「応答の書き方」節か、口調を決めた別の節)が無いプロジェクトでは、skill は口調を決めない。ほかの指示に口調があればそれに従い、無ければ、です・ます で書く
- `/init-project` が既存の CLAUDE.md に AGENTS.md の import 行(AGENTS.md を読み込む行)を書き足すときは、1 行目に入れる(既存の記述がすべて import 行より下になる)
- `/init-project` が互換形の CLAUDE.md(AGENTS.md の import 行を持つ CLAUDE.md)を移行するとき、import 行より下の追記に口調を決めた節があれば、その節を `.claude/rules/claude-code.md` ではなく AGENTS.md へ移すよう勧める。`.claude/rules/claude-code.md` へ移すと、skill がその節を読まず、「応答の書き方」節から指せないため
- `/init-project` だけを導入した構成(共通の書き方の決まりのファイルに届かない)では、既存の指示ファイルへの「応答の書き方」節の追記を提案しない
- `/init-project` の質問は決まった順に並べる。ホストの質問の仕組みが 1 回に受け付ける数を超えると、何回かに分けて聞く
- 検証器 `scripts/validate.py` は、各 SKILL.md の `## 原則` の節に `do-task/references/writing-for-people.md` へのリンクが無いと ERROR にする。独自の skill を持つ fork は、その skill の `## 原則` の節にリンクの 1 行が要る

### 移行方法

- 既存のプロジェクトは、`/init-project` を打ち直すと「応答の書き方」節の追記の提案を受けられる
- 権威参照ファイル(AGENTS.md と CLAUDE.md が両方あるときの CLAUDE.md は、import 行より下)に口調を決めた別の節がもうあれば、その節は書き換えない。「応答の書き方」節には、決まりの文と、その節を指す 1 行が入る
- 特定の AI ツールだけが読む設定や、個人の設定で口調を決めているときは、口調の質問で「プロジェクトの指示ファイルに口調を入れない」を選ぶ
- 口調を変えたいときは、口調を決めた節を書き換える
- 「応答の書き方」節の中身は、`/init-project` を打ち直しても置き換わらない。決まりを更新したいときは、手で直す

## v4.18.1

### 修正

- Gemini のレビュー既定コマンドで、プロンプトを `--prompt=<本文>` の 1 引数として渡すようにした。箇条書き・frontmatter など `-` で始まる本文がフラグとして解釈されず、本文を変えずに渡せる。レビュー用のコマンド組み立ては `--prompt={prompt}` と `-p={prompt}` の完全一致も 1 引数に展開する

## v4.18.0

### 変更

- 無人の push(タスクの周・発見の周)を `-u` なしで打つ(`git push --no-follow-tags --recurse-submodules=no origin 'refs/heads/<作業ブランチ>:refs/heads/<作業ブランチ>'`)。利用者の設定の `branch.autoSetupRebase`(`always`・`remote`)のもとで、`-u` が共有の `config` に `branch.<作業ブランチ>.rebase=true` も書き、PR まで進んだ周のたびに `loop.sh` の周の後の照合で止まっていたため。無人の周は作業ブランチの追跡を使わない(PR は `--head` で作る)
- `loop.sh` の周の後の照合は、共有の `config` への追加を許さない(`-u` の push が書く `branch.task/{名}.remote`・`.merge` も差分になる)。周ごとの報告は「許した」の一覧を出さず、照合に通った周では `- 共有の config の差分: 無し` だけを出す
- 候補の PR の片付けの案内(`loop.sh` の朝の報告・loop.md)を「merge commit で merge して pull した後」にした。発見の周の PR 本文の「人がすること」も「merge して pull した後に、作業ブランチを消す」にした
- 無人の周の PR 本文(タスクの周・発見の周)に、手元で直して push するときの手段(`git push origin <作業ブランチ>`)を載せる

### 後方互換を破る変更

- 無人の周が push した作業ブランチに upstream(追跡)が付かない。引数なしの `git push` は、`push.default` が既定の `simple` で `push.autoSetupRemote` が無ければ通らない(`push.autoSetupRemote=true` なら通り、追跡を共有の `config` に書く)。`git branch -d <作業ブランチ>` は、手元のデフォルトブランチに merge を pull した後でないと通らない
- 周の中で共有の `config` に項目が足されると(`-u` の push を含む)、`loop.sh` が周の後の照合で止まる(exit 10・`shared-state`)
- 旧版の周が push の後に途中で止まり、周の途中の印が残っていると、新しい版の次の起動が `inflight-diff` で止まる(exit 20)

### 移行方法

- 手元で作業ブランチを直して push するときは `git push origin <作業ブランチ>` で打つ。追跡を付ける操作(`git push -u`・`git branch --set-upstream-to`・`gh pr checkout`・`push.autoSetupRemote=true` のもとでの引数なしの `git push`)は共有の `config` に書きうるので、ループが動いていないときに限る
- 旧版の印で止まったら、差分が `branch.task/{名}.remote`・`.merge`(・`.rebase`)だけと確かめてから、止めの印を消す(loop.md §4・§7)

## v4.17.0

### 変更

- 無人の push(タスクの周・発見の周)を、完全な refspec と `--no-follow-tags --recurse-submodules=no` で打つ(`git push --no-follow-tags --recurse-submodules=no -u origin 'refs/heads/<作業ブランチ>:refs/heads/<作業ブランチ>'`)。利用者の設定の `remote.origin.push` の写像・`push.followTags`・`push.recurseSubmodules` で、送り先の ref と送る ref が変わらないようにするため
- 無人の作業ブランチを `git switch --no-track -c <作業ブランチ>` で作る(利用者の設定の `branch.autoSetupMerge` で共有の `config` に項目が足されないように)
- 無人の周の中で、ローカルの git 設定のダイジェストを周の開始時の値と照らす。時点は、do-task の implementer から戻るたびと Phase 7 の手順 1 の前・ship-task の各 commit の手順の最初(stage の前)・commit と push のコマンドの直前(`git-config-digest.py --expect=<守る値> && git commit …`・`… && git push …` の 1 行で打つ)。通らなければ失敗扱い(G2)。ダイジェストを算出する `ship-task/scripts/git-config-digest.py --dir=<ディレクトリ> [--expect=sha256:<64 桁>]` を新設した(共有の `config` と `config.worktree` を include を辿らずに項目の並びで読み、置き場の絶対パスと合わせて sha256 を取る。git は 10 秒で打ち切る。設定の値は出力しない。終了コードは 0 = 算出した〈一致した〉/ 1 = 不一致 / 2 = 算出できない)
- push の直前に、origin の判定(`origin-repo.py` の `origin`・`same`・`vcs`・`repo`・`form`)を打ち直し、周の開始時の値と照らす(タスクの周・発見の周)
- タスクの周の前提に、ローカルの git 設定のダイジェストの算出と、発見の周と同じ origin の URL の検査を入れた。PR は、PR の作成先を `gh repo view '<repo>' --json name -q .name` で確かめてから、`gh pr create -R '<repo>'` で push 先(origin)のリポジトリに作る(`<repo>` は `origin-repo.py` の `repo`)
- `loop.sh` の実装モードも、起動時に兄弟の `origin-repo.py` と origin の URL を検査し、兄弟の `git-config-digest.py` が在ることを確かめる(発見モードと同じ理由コード)。起動時の報告の origin の行を両モードで出す

### 後方互換を破る変更

- タスクの周(`--task=<タスク MD> --unattended`)の前提に origin の URL が入る: fetch と push の URL がそれぞれ 1 つで同じリポジトリを指し、`remote.origin.vcs` が無いこと。満たさなければ失敗扱い(G3)
- 単独の `/do-task --unattended` の前提に、`{ship-task の}scripts/git-config-digest.py` が在り、管理ルートでローカルの git 設定のダイジェストを算出できる(exit 0)ことが入る。満たさなければ、候補の選択より前に失敗扱い
- `loop.sh` の実装モードも、起動時に origin の URL を検査する(fetch と push の食い違い・`remote.origin.vcs`・兄弟のスクリプトの欠けで exit 20)
- タスクの周の PR は push 先(origin)のリポジトリに開く。fork の運用で、gh の既定のリポジトリ(upstream など)に開いていた PR は開かない
- origin の判定の `repo` が読めない push 先(ssh の設定の別名・`ssh.github.com` の 443・ローカルのパス・TLS の検証を外した https など)では、タスクの周は push も PR もせずに結末 `縮退` で終わる
- 周の中でローカルの git 設定(共有の `config`・`config.worktree`)が変わると、失敗扱い(G2)になる。周の中で設定を書くスクリプト(husky の prepare・`git lfs install --local` など)と、周の間の人の操作(同じリポジトリでの `git push -u`・追跡つきの `git switch`・`gh pr checkout` など)も含む

### 移行方法

- origin の fetch と push の URL を 1 つずつにして、同じリポジトリに揃える。`remote.origin.vcs` を外す
- ssh の設定の別名は、`git@github.com:OWNER/REPO` か https の形にする
- upstream への PR は人が作る
- 周の中で git の設定を書くスクリプト(husky の prepare など)は、人のチェックアウトで先に打っておく(同じ値の書き直しは、ダイジェストを変えない)
- 無人ループの周の間は、同じリポジトリの共有の設定を変える操作をしない

## v4.16.1

### 変更

- 無人の周が使う `secret_paths`(実装の周の commit の除外対象・発見の周の task_dir と候補の検査)を、do-task の diff スナップショットと同じく、基準側の profile と現在の profile の和集合で読むと定めた(作業ツリーの profile を狭めるだけで機密ファイルを commit の集合に入れられないように)

## v4.16.0

### 変更

- `review-agent.sh`・`implement-agent.sh` は、ログの置き場を物理パスで固定してから開く。既定名の基点は起動時の `pwd -P`(置き場は起動時の cwd の `.claude/reviews`)。明示した `--log-file` は、置き場を語彙的に正規化してから、基点(review は起動時の `pwd -P`、implement は起動時の `pwd -P` と `--cwd` の実体)に最初に一致した接頭辞より後ろの階層を、階層ごとに symlink の検査と `cd -P`・`pwd -P` の照合で固定し、置き場の中で相対名で開く。基点の外の `--log-file` は開ける(信頼する)。止まるときは usage(exit 2)で、ランナーを起動せず、stderr の `ERROR [usage] ` の行に `既定のログ置き場` か `--log-file の置き場` と、symlink・照合の不一致なら `置き場の経路が symlink か差し替えられた: <物理パス>` を出す(置き場を作れない・書けない・名前に既にエントリが在るときも同じ語を含む)。`NOTE: ログ:` は、基点の中なら固定した置き場の物理パス、基点の外なら語彙的に正規化した絶対パスで出す
- do-task の外部 implementer の手順は、`--dry-run` と本実行の直前に `reviews-dir.sh ensure --root <管理ルート>` を打ち、共通工程の最後(④)にも打つ。起動の直前の検査が 0 以外のとき・スクリプトが置き場の理由の exit 2 で止まったときは、内蔵 implementer へ縮退せずに停止する。④ が 0 以外は比較不能(人の判断)
- team-lead が `.claude/reviews/` に記録を書く 9 か所(do-task の Phase 6・トリアージ・リカバリ・create-task・update-doc・init-project・reflect-decisions・data-audit・候補モード)は、書く直前に同じ `reviews-dir.sh ensure` を打ち、0 以外なら書かずに停止する
- レビュー経路(`review-agent.sh`)が置き場の理由の exit 2 で止まったときは、内蔵編成に縮退せず停止する(external-runners.md §10)
- 中止・タイムアウトの文言を実態に合わせた(usage・ログ・stderr・external-runners.md): プロセスグループと直接の子に終了のシグナルを送り、止まったことは確かめない。external-runners.md の既知の限界に、team-lead の記録の名前が予測できることと、外部ランナーが残す常駐プロセスの 2 件を足した(14 件)。design §7-3 に、ホストの書き込みツールの挙動(名前の symlink・FIFO の拒否・ハードリンクの置き換え・親ディレクトリの symlink を辿る)を記録した
- review・implement の selftest に `置き場の経路:` のケースを足した(既定名・明示の `--log-file`・基点そのものを指す symlink・検査と作成の間・検査と `cd` の間・固定と開く間の差し替え・基点の外・相対の `--log-file`・書けない置き場・名前に既にエントリが在る〈`..` を語彙的に解いた名前で見る〉)

### 後方互換を破る変更

- 起動時の cwd の `.claude` か `.claude/reviews`、または明示した `--log-file` の基点より後ろの経路が symlink なら、両スクリプトが usage(exit 2)で止まる(リンク先には作らない)。起動時の cwd と `--cwd` の間に symlink の階層がある呼び方も止まる。明示した `--log-file` の `..` は語彙的に解く(`a/link/../x.md` は `a/x.md`)
- skill は、置き場(`.claude`・`.claude/reviews`)が symlink か通常のディレクトリでなければ、記録を書かずに止まる
- do-task の外部 implementer とレビュー経路は、スクリプトが置き場の理由で止まったとき、内蔵へ縮退せずに止まる

### 移行方法

- 置き場(`.claude`・`.claude/reviews`)を実ディレクトリにする(symlink を消し、リンク先にある記録を移す)。スクリプトを直接呼ぶ場合は、`--cwd` に cd してから打つか、`--log-file` を `--cwd` の実体パスか基点の外で渡す

## v4.15.2

### 変更

- skill 本文・references・同梱スクリプトが番号だけで引いていた決定(11 ファイル・74 件)に、出典を付けた(決定録の日付。Issue #68 で決めたものは `#68 の決定 N`)。implement-agent.sh と implement-guard.sh の使い方の表示、外部 implementer のログのプローブの cwd の行、selftest の 3 つの検査の名前は字面が変わる。処理・終了コード・検査の合否は変わらない

## v4.15.1

### 変更

- external-runners.md が番号だけで引いていた決定に、出典の決定録の日付を付けた(2026-09-09 と 2026-09-17 の決定が番号だけで混在し、2026-09-23 の決定とも番号が重なるため)。挙動・手順は変わらない

## v4.15.0

### 変更

- `loop.sh` の実装モードも、状態ファイル(`.claude/reviews/`・`.claude/grasp.md`・`.claude/.understand-project-done`)が ignore されていて追跡されていないことを、周の worktree を作った直後に確かめる。外れていれば exit 20(`state-not-ignored`)で止まり、gitignore の断片を報告に出す
- この検査は、`git check-ignore` が判定できないもの(`.claude` か `.claude/reviews` が symlink・`.claude` がサブモジュール)を数えず、後の検査に任せる(両モード)

### 後方互換を破る変更

- 状態ファイルを ignore していない、または追跡しているリポジトリでは、実装モードの `loop.sh` が起動しない(`--dry-run` も exit 20)
- 発見モードで `.claude` か `.claude/reviews` が symlink の worktree は、ほかの状態ファイルが外れていなければ exit 30(`reviews-dir`)で止まる(exit 20・`state-not-ignored` ではない)。`.claude` がサブモジュールのリポジトリは、起動時に止まらず、発見の周の前提も通る

### 移行方法

- init-project の gitignore の断片(`.claude/reviews/`・`.claude/grasp.md`・`.claude/.understand-project-done`)を `.gitignore` に足して commit する。追跡済みのものは `git rm --cached -- <パス>` で索引から外して commit する。前の実行が残した周の worktree は、`git -C <パス> status --porcelain --untracked-files=all` で残りが状態ファイルだけであることを確かめてから、`git worktree unlock <パス>` → `git worktree remove --force <パス>` で片付ける(その worktree の `.gitignore` は古いままなので、`--force` なしでは拒否される。ほかの変更が残っていれば、中を調べてから決める)

## v4.14.0

### 変更

- ship-task の Phase 0 の 3 は、do-task の Phase 0 の 3 と同じ字面の `git status` で未コミット変更を確かめる。状態ファイル(`.claude/reviews/`・`.claude/grasp.md`・`.claude/settings.local.json`・`.claude/.understand-project-done`)の変更と、サブモジュールの中の未コミット変更は確認の対象にしない(gitlink の変更は対象)。状態ファイルを ignore していないリポジトリでも、無人の周が S1 で失敗扱いにならない
- do-task の Phase 0 の 3 の `git status` も同じ字面で、`GIT_LITERAL_PATHSPECS` と `status.showUntrackedFiles` の設定に左右されない
- 対話の ship-task の実装 commit・doc commit は、状態ファイルを stage しない。commit の直前に index に在れば、外すか残すかを確認する

## v4.13.0

### 変更

- python3 が起動できる環境では、`-T` の無い `mv`(BSD など)でも、新規着手の基準の記録(`reviews-dir.sh save-untracked`)が exit 20 で止まらない。`sha256sum` の無い環境では `shasum`・`openssl` で代わりに計算する(macOS の実機では未確認)
- `reviews-dir.sh save-untracked` は、未追跡一覧を rename(2)(python3 の `os.replace`、無ければ `mv -f -T`)で公開し、公開に失敗したときは理由の 1 行を `ERROR [internal]` の行に添える(`mv -f -T` で失敗したときは、`-T` を持たない `mv` では python3 が要ることも書く)。sha256 を計算する道具(`sha256sum`・`shasum`・`openssl`)がどれも無ければ、`ERROR [tool-missing]`(exit 20)で止まる
- review-agent.sh は、プロンプトの大きさを外部 CLI を起動する前に判定する(`--dry-run` でも)。レビュー経路で `prompt-too-large`(12)が出たら、diff を貼らずにパスで渡すように直して 1 回だけ打ち直し、それでも 12 なら内蔵編成で続けて報告する(external-runners.md §10)
- 実装経路で `prompt-too-large`(12)が出たら、`/do-task` は外部 CLI を起動する前の失敗として報告し、内蔵 implementer で続ける(打ち直さない。external-runners.md §12-5・do-task の Phase 3 の手順 6)
- review-agent.sh は、ログの置き場を作れないとき、usage(exit 2)で理由を返す(implement-agent.sh と同じ)

### 後方互換を破る変更

- `review-agent.sh` は、ログの置き場(既定の `.claude/reviews/`・明示した `--log-file` の置き場)を作れないとき、exit 2(`usage`)で終わる(今までは exit 20・`internal`)
- `review-agent.sh` は、プロンプトが大きすぎれば、判定 1〜4(未検出・自ホスト・読み取り専用・疎通)より前に exit 12(`prompt-too-large`)で終わる。`--dry-run` でも 12 で、解決後の起動コマンドを出さない(今までは、`--dry-run` は exit 0 でコマンドを出し、それ以外は判定とプローブの後に 12 で終わった)

### 移行方法

- review-agent.sh の終了コードで分岐している呼び出し側は、置き場を作れないときの 2(usage)と、`--dry-run` でプロンプトが大きすぎるときの 12 を扱う。python3 が無く、`-T` の無い `mv` の環境では、python3 を入れる

## v4.12.0

### 変更

- 外部ランナーのスクリプト(`review-agent.sh`・`implement-agent.sh`)は、ログを作るときに 1 回だけ開き(noclobber)、以後の書き込みはすべてその fd に行う。走行中にログのパスが symlink・ハードリンク・FIFO に差し替えられても、外のファイルへ書かず、FIFO で待ち続けない。開いたものが通常ファイルでなければ usage(exit 2)で止まる。外部 CLI にはログの fd を渡さない
- ログに結果を書く前に、ログのパスが開いたファイルと違っていれば、stderr に `NOTE: ログのパスが走行中に差し替えられた: <パス>` を 1 回出す(終了コードは変わらない)
- 既定名のログの採番は、その番号の名前にエントリ(FIFO・symlink・`/dev/null` を指す symlink を含む)が在れば、次の番号へ進む。置き場に FIFO があっても待ち続けず、ログが `/dev/null` に消えない
- ログを開いたら、stderr に `NOTE: ログ: <絶対パス>` を出す。`do-task/references/external-runners.md` §10 の報告の「記録」欄には、この行のパスを書く(行が無ければ欄を省く)
- 想定外の失敗(`ERROR [internal]`・exit 20)は、ログを開いた後なら、ログにも結果行 `**結果**: ERROR [internal] …` と、得られた分の生出力(`生出力(想定外の失敗の時点まで)` などの節)を残す。`implement-agent.sh` は、中止と同じく生出力を stdout にも流す。代入・単独のコマンドの形の置換の中の失敗でも、`ERROR [internal]` の行は 1 行だけ出る(引数・heredoc の中の置換の失敗は報告しない — external-runners.md §12-6 の既知の限界)
- 両スクリプトは、`POSIXLY_CORRECT` のある環境や `bash --posix` で起動されても、bash の既定の意味で動く(ログの置き場に書けないときは、`ERROR` の行なしに rc 1 で終わらず、usage(exit 2)で止まる)
- `review-agent.sh` は、ログの初期化の最中に `TERM`・`HUP`・`INT` を受けても、`ERROR [aborted]` を出して 128+N で終わる

### 後方互換を破る変更

- ログの初期化より前の失敗(`implement-agent.sh` の実装用既定表の形の崩れ・`--cwd` の実体パスを解決できない・保護領域を解決できない / 作れない、`review-agent.sh` の `--readonly-flag` の欠け)は、明示した `--log-file` を作らず、stderr だけに出す(今までは明示したログを作って結果行を書いた)

### 移行方法

- 外部ランナーのログのパスは、置き場の最新の名前から推測せず、stderr の `NOTE: ログ: <絶対パス>` から取る。この行が無い失敗(ログを開く前の失敗)ではログを作らないので、§10 の「記録」欄を省く(失敗の理由は stderr の `ERROR [理由コード]` 行に出る)

## v4.11.0

### 変更

- `/do-task` の新規着手の条件 ③(`do-task/references/base-commit.md`)は、`.claude/reviews/` の直下に、ファイル名が `-{TASK_NAME}-iter<数字>.md` で終わる名前(`create-task-` で始まるものを除く)か `recovery-{TASK_NAME}.md` が在るときだけ不成立になる。名前は、タスク名をグロブ・正規表現に埋め込まずに文字列として比べる。タスク名の無い名前(外部ランナーの既定名 `implementer-{ランナー}-iter{N}.md` など)は数えないので、同じチェックアウトに別のタスクの外部 implementer のログがあっても、新規着手が『基準不明』にならない
- `/do-task` の ITER は、再開かどうかに関わらず、条件 ③ の 1 つ目の項に当たる名前の番号の最大値+1(無ければ 1)から始める。前の走行のログがあると、`--max-iter` の報告点はその分早く来る
- `/do-task` は外部 implementer の本実行に `--log-file <管理ルート>/.claude/reviews/implementer-{ランナー}-{TASK_NAME}-iter{ITER}.md` を渡す(承認の提示の `--dry-run` には渡さない)。外部 implementer の起動は ITER ごとに 1 回で、同じ ITER で既に外部を起動していれば(Phase 5・5.5 からの差し戻しや、比較不能から人の判断でもう一度起動するとき)、ITER を 1 つ進めてから起動する。こうして進めた ITER も `--max-iter` の報告点に数える
- 外部ランナーのスクリプト(`review-agent.sh`・`implement-agent.sh`)は、明示した `--log-file` のパスに既にエントリ(通常ファイル・symlink・リンク先の無い symlink・ディレクトリ・FIFO)が在れば、何も書かずに usage(exit 2)で止まる(`--dry-run` でも)。前の反復の外部ランナーが、次の反復のログ名に外のファイルを指すリンクを先に置いても、リンク先に書かないため
- レビュー経路の外部ランナーのログは、skill の手順からは `--log-file` を渡さず、既定名 `reviewer-{ランナー}-iter{N}.md` で残す(`do-task/references/external-runners.md` §7)。design §5-14 の記録は team-lead が残し、このログはその生出力の控えになる

### 後方互換を破る変更

- `review-agent.sh`・`implement-agent.sh` に、既にエントリが在るパスを `--log-file` で渡すと exit 2 になる(今までは切り詰めて書いた)

### 移行方法

- 条件 ③ は、タスク名の無い外部ランナーのログを自タスクのものとして数えない(この版より前の `/do-task` が残した `implementer-{ランナー}-iter{N}.md` など。名前の末尾がたまたまタスク名と一致するときは数える — base-commit.md の限界)。基準行が残っていれば影響は無い
- この版より前に、タスク名つきの名前で残した設計レビューの外部ログ(`reviewer-{ランナー}-{タスク名}-iter{N}.md`)は、条件 ③ に数えられる。そのタスクを新規着手すると『基準不明』になるので、ログを置き場の外へ移す
- `--log-file` に既存のパスを渡す呼び出しは exit 2 になる。同じパスを使い回していたら、番号を進める

## v4.10.1

### 変更

- 外部ランナーのスクリプト(review-agent.sh・implement-agent.sh)の既定のログの採番は、リンク先の無い symlink を番号の衝突として扱い、次の番号で作る(今までは「置き場に書き込めない」と誤って止まった)

## v4.10.0

### 変更

- 対話の `/do-task` で、内蔵 implementer への委託ごとに、委託の直前に控えた本文ダイジェストと、implementer の報告を受けた Phase 4 の最初とで照らし合わせ、タスク MD の本文が変わっていたら(`- [x]` の印の変更を除き)ユーザーに確認する(失敗扱いにはしない)。委託プロンプトは、対話・無人を問わず「タスク MD はチェックボックスの更新だけにし、本文を変えない」を常に含める。`- [x]` の全件突合を明記した(今までは「抜き打ちで数件」)
- `/do-task` に `保留_`・`中断_` のパスを渡すと、書き換えずに停止して `進行中_` へ戻すよう案内する。中断時の `保留_` への改名は、ユーザーの合意があれば `/do-task` が行う(`/ship-task` から呼ばれたときは提案も改名もしない)
- `/update-doc` は `--task` 省略時、このセッションで `/do-task` が完了にしたタスクが 1 つならそれを使い、無ければ `完了_*.md` を並べてユーザーに選ばせる(git log・更新時刻での自動選択をやめた)。`--task` と `--analyze-only` は併用できず、併用されたら停止してどちらか一方を指定するよう案内する
- レビューの合格語を `APPROVED` に統一した(`PASS`・`PASS(valid 0)` の表記を削除)。`/update-doc` は報告の最後に `レビュー判定: <APPROVED | 未収束 | 未完了>` の 1 行を必ず書き、`/ship-task` の Phase 4 はその行を読んで続行可否を判定する
- `/do-task` の M2(個別再委託)は、内蔵 implementer が走行中でないと確かめてから引き継ぐ。フル段階では中止を伝えて生存確認で完了として出ることを確かめたうえで同じ name へ再依頼し、宛先が失われているときだけ新規に起動する。判別できない・標準・最小段階では引き継がず M4 へ進む(無人では失敗扱い)
- `/create-task` は profile の `features.external_review: false` を `--no-review` と同じく無効化して報告する(独立レビューは省略しない)

## v4.9.0

### 変更

- 発見ループを足した(既定はオフ)。`loop.sh --discover[=<発見元>,…]` は、発見元(`data-audit`・`refactor`)を 1 周ずつ、新しいヘッドレスセッションの `/ship-task --discover=<発見元> --unattended` で回す。発見元の候補モードが書いた `候補_` のタスク MD だけを、作業ブランチ `task/候補-<発見元>-<起点の sha の先頭 12 桁>` に 1 commit して PR を開く(1 晩・1 発見元ごとに 1 本)。実装はしない(`進行中_` を機械が作らない)。候補が 0 件なら、ブランチも PR も作らずに結末 `候補なし` で終わる。今夜の名のブランチか未 merge の候補のブランチがある発見元・失敗の周の worktree が残っている発見元は回さない。発見モードは、状態ファイル(`.claude/reviews/`・`.claude/grasp.md`・`.claude/.understand-project-done`)が ignore されていて追跡されていないこと、origin の fetch と push の URL が同じリポジトリを指し `remote.origin.vcs` が無いことが前提で、満たさなければ起動時に止まる(exit 20)。正本は `ship-task/references/discover-mode.md` と `loop.md` の「発見モード」
- 状態名に `候補_` を足した(発見ループの採用前の候補)。保存先の検出(`create-task/scripts/resolve-task-dir.py`)は `候補_` の MD も数えるので、`候補_` だけを置いたディレクトリも保存先の候補になる(`候補_` を別の場所に持つプロジェクトでは、保存先の検出の結果が変わりうる)。`進行中_` を選ぶ処理(`/do-task` の自動選択・`loop.sh` の実装モード)は `候補_` を拾わない
- `/data-audit --candidates`・`/create-task --refactor --candidates`(候補モード)を足した。承認を待たずに、1 指摘 = 1 つの `候補_` のタスク MD を書く(上限は data-audit 10・refactor 5。`--max-candidates` で変えられる)。ヘッダの指摘キーで、保存先の全状態の MD と照らして既知の指摘を積まない(`完了_` と一致したら回帰の疑いとして報告する)。refactor の候補モードは、機密のログ・URL の露出と doc/06 の既知の脆弱性から来た指摘を候補にしない。`--candidates` の無い呼び出しは今までどおり(承認 → チェーン)。正本は `create-task/references/candidate-mode.md`、既知のキーの収集と候補の検査は `create-task/scripts/candidate-keys.py`
- `/create-task <候補_ のパス>` で候補を採用する経路を足した。指摘を実コードで確かめ直し、`候補_{名}` を `進行中_{名}` に改名して(追跡済みなら `git mv`。stage だけで commit しない)設計を書き足す。発見元と指摘キーの行はヘッダに引き継ぐ。ヘッダに見送りの行(`> **見送り**: YYYY-MM-DD — <理由>`)がある候補と `--unattended` では止まる。`/do-task` は `候補_` のパスを渡されたら止まり、`/create-task` での採用を案内する
- `ship-task/scripts/origin-repo.py` を足した。origin の fetch と push の URL が同じリポジトリを指すか、公開の確認と PR に使える push 先(https、または ssh の上書きの無い `git@github.com`)かを JSON で返す。URL の字面は出力しない
- 無人の周(`loop.sh`)の読むだけの調査は、ツールを先に使う: 中身は Read で読み、一覧・検索は Glob・Grep のツールがあればそれを使う(`ship-task/references/unattended-mode.md`。委託するサブエージェントの要点にも足した)。worktree の中を読むこれらのツールは許可の仲介に掛からないので、Bash の書き方の誤りによる拒否(G1)が起きにくくなる(指示で守るもので、機構では保証しない)。Glob・Grep を既定で持たないホストのために、`loop.md` §4 の許可リストの推奨に、`--allowed-tools` でそのツールを名指す値を足した(値は `docs/design.md` §7-3)
- 無人モードの結末の行の照合パターンを `^無人の周の結果: (PR|縮退|保留|失敗扱い|候補なし) — ` にした。`loop.sh` は、実装モードでもこのパターンに一致する最後の行を結末として読み、そのモードで取りえない値(実装モードの `候補なし`)は失敗と判定する(今までは 4 値のパターンで読んでいた)

## v4.8.0

### 変更

- 無人ループ `ship-task/scripts/loop.sh` を足した(人のシェル・cron から、このリポジトリの clone のパスで起動する。既定はオフで、起動しなければ挙動は変わらない)。メタ行 `> **無人実行**: 可` がある `進行中_` を作成日の古い順に拾い、周ごとにローカルのデフォルトブランチから使い捨ての worktree を作って、新しいヘッドレスセッションで `/ship-task --task=<タスク MD> --unattended` を回す。作業ブランチか残った worktree があるタスクは拾わない。止まる条件は最大周回数・連続失敗・時間予算・停止ファイル・許可の拒否の保留の連続・共有の git の状態の変化など。時間上限を超えた周は打ち切って worktree を残す。リポジトリ側のホスト設定を読ませずに起動し、全許可のモードは使わない(許可リストはホストの利用者設定か `--allowed-tools`)。周の子の環境から `OLDPWD` を外す(周の中の `cd -` の行き先にさせない)。朝の報告のホストの結果の拒否の欄は、周の中でバックグラウンドに委託して結果がターンごとに出るときも、すべてのターンの拒否を集める。対象ホストは Claude Code だけ・Linux 専用。profile の `features.loop` に `max_iterations`・`max_consecutive_failures`・`time_budget`・`iteration_timeout` を足した(値を小さくする向きだけ効く)。契約の正本は `ship-task/references/loop.md`、回帰テストは `ship-task/scripts/loop-selftest.sh`
- `loop.sh` の許可の仲介: 保護パス(`.claude/`・`.git` など)への書き込みは、編集の自動許可でも許可リストでも通らず確認に回り、無人の周では拒否になる。`loop.sh` は周を起動する前に worktree の `.claude/reviews/` を作り、`PermissionRequest` の hook(`ship-task/scripts/loop-permission.py`)を渡して、その周の worktree の状態ファイル(`.claude/reviews/` の下・`.claude/grasp.md`・`.claude/.understand-project-done`)への書き込みだけを許す。ほかは拒否して種類つきで記録し、朝の報告に写す。hook は、Bash の先頭の `CDPATH= cd -P -- <worktree の中のディレクトリ> && <続き>` を受け付け、続きをそのディレクトリを作業ディレクトリとして判定する(最初の `||`・`;` より後ろは、元の作業ディレクトリからも判定する。行き先が `-` で始まるか `~` を含むものは受け付けない)。hook を無効にする設定(`disableAllHooks`・`allowManagedHooksOnly`)があると起動しない。保護パスを変えるタスクは無人では完了しない。task_dir が保護パスの下(`.claude/tasks` など)のリポジトリでは、最初の周の選定で止まる
- `/do-task` の `.claude/reviews/` の作成と、基準時点の未追跡一覧の保存を、同梱のスクリプト `do-task/scripts/reviews-dir.sh`(`ensure` / `save-untracked`)で行う(対話でも無人でも)。検査・停止の条件・基準行に書く sha256 は今までと同じ
- `/do-task` の事前検査(`diff-snapshot.sh --precheck`)は、`--accept` を付けないとき(最初の実行。無人では常に)一時ファイルに受けずに直接実行し、終了コードだけを見る(対話でも。結果は同じ)。`--accept` を付けて打ち直すときは今までどおり一時ファイルに受ける
- 無人モード(`ship-task/references/unattended-mode.md`)に、`loop.sh` の周の Bash の書き方を足した: `$(…)`・変数の展開・heredoc・改行を使わず解決した値を書く、本文のファイルは `.claude/reviews/` の下に置いて `<` か `-F` で渡す、など(最初の呼び出しからこの書き方で打つ)。許可の仲介の hook に拒否されたら、打ち直さずに許可の拒否(G1)に従う(hook の拒否の理由にもそう添える)
- 無人(`--unattended`)で `/do-task`・`/update-doc` がサブエージェントに委託するとき、委託プロンプトに `ship-task/references/unattended-mode.md` の「委託するサブエージェント」の項の要点(周の Bash の書き方の具体の値)を要約し直さずにそのまま入れる。委託先が許可の拒否を報告したときも、メインは打ち直さずに許可の拒否(G1)に従う
- タスク MD の記法の規約(`create-task/references/task-template.md`)に、作成日の行の照合パターンと、保留の行の `<停止条件>` の書式(対話点番号で始め、直後は空白か `:`)と取り出しパターンを足した。無人モードの `/ship-task` はこの書式で保留の行を書く(既存のタスク MD は変わらない)
- 無人モードの結末の行の照合パターン(`^無人の周の結果: (PR|縮退|保留|失敗扱い) — `)を `ship-task/references/unattended-mode.md` §2 に足した
- `/ship-task` は PR 本文を stdin で gh に渡す(`--body-file -`。対話でも無人でも)。PR の中身は変わらない
- `/understand-project` を呼んだ後も、同じターンで Edit・NotebookEdit が使える(frontmatter の `disallowed-tools` を外した。呼んだ後のターンの残りにも効き、`/ship-task` などが続けて行う編集まで止めていた)。読み取り専用は本文の原則で守る

## v4.7.0

### 変更

- `/do-task` の外部 implementer(`features.implementer`)に、外部に解決しない条件を 2 つ足した。A: タスク MD がファイルでない(課題管理システムの本文など。本文の写しを `--task-md` に渡さない)。B: そのセッションで最後に承認した一覧に filter がある(承認の場所は問わない)。どちらも Phase 3 に入るたび(ITER ごと)に、外部の手順の手順 1 で承認より前に判定し、当たれば内蔵 implementer で走って理由を報告する。B は手順 1 で `diff-snapshot.sh --precheck` を打ち直して判定する(rc 22 なら人の承認を求め、承認されなければ停止する)。git でないプロジェクトも、承認を求めずにここで内蔵に決まる(これまでは承認の後に `take` の `not-git` で縮退していた)
- 外部に解決されたときは、`implement-guard.sh take` の後、外部を起動する前に、保護領域のパスと 2 つのダイジェストを報告に出す。値を埋めた 3 本のコマンド行(`--precheck`・`compare`・`taskmd-diff`)と、手順 1 の `--precheck` の NOTE「無効化して実行」の行も出す。値を失ったら、このセッションのユーザーの発話からだけ受け取る(受け取れなければ比較不能)
- 別のセッションで再開するときの手順を定めた(`do-task/references/external-runners.md` §12-2 の受け入れた限界 (a))。外部のプロセスが残っていないことを確かめ、報告の値で `--precheck` → `compare` → `taskmd-diff` を通してから /do-task で再開し、完了処理の後に `cleanup` を打つ
- 起動後の共通工程で、`--precheck` の stdout に `GIT_CONFIG_COUNT` が出たら `compare` を打たず(`taskmd-diff` は打つ)、比較不能として人の判断へ回す。再開は、外部が足した設定・属性・hooks をファイルの編集で戻してから、`--precheck` から打ち直す
- 共通工程の `--precheck` と Phase 4 の diff スナップショットの `--accept` は、Phase 0 の値ではなく、そのセッションで最後に承認した値を付ける(差し戻しのたびに同じ承認を求めない)
- `/ship-task` の Phase 0 で先取りした外部実装委託の承認は、/do-task が上の条件で内蔵に縮退したときは使わず、そのことを報告する

## v4.6.0

### 変更

- `/ship-task` に、既存のタスク MD から始める入口 `--task=<タスク MD>` を足した。Phase 1(/create-task)を飛ばし、Phase 2 より前に作業ブランチを作ってから、続行判定・実装・doc 同期・PR まで回す。`--design-only`・`--refactor`・`--compact`・`--light` とは併用できない
- `/ship-task` に無人モード `--unattended`(`--task` が必須)を足した。対話点で止まらず、「自動で答える / 保留 / 失敗扱い」のどれかに倒す。保留は `保留_` への改名と保留の行(追加修正記録の 1 行)を作業ブランチに 1 commit し、失敗扱いは何も書き換えずに止まる。`完了_` への改名後の停止は失敗扱い。完了報告の最後に `無人の周の結果: <結末> — <理由か URL>` を書く。正本は `ship-task/references/unattended-mode.md`。`/do-task`・`/update-doc` にも `--unattended` を足した(単独で呼ぶときは対象のパスが必須)
- 無人モードの改竄ガード: 周の中で本文ダイジェスト・作業ブランチ・HEAD・基準行・未追跡一覧を保持し、`/do-task` の Phase 4 の各回と Phase 7 の前、`/ship-task` の各 commit の直前・直後(tree の一致を含む)と push の前に照合する。1 つでも通らなければ失敗扱い
- 本文ダイジェスト(`create-task/scripts/task-digest.py`)を新設した。タスク MD から、ステータス・基準コミット・無人実行のヘッダの行と追加修正記録の節を除き、正規化して sha256 の先頭 16 桁を出す。定義の正本は `create-task/references/task-template.md` の記法の規約
- `/create-task` の設計レビューの行に `本文: <本文ダイジェスト | 算出不能>` を足した(`needs-user` の前)。`/ship-task` の Phase 2 に「6. 本文の照合」を足した
- `/ship-task` は detached HEAD をデフォルトブランチと同じに扱う。作業ブランチを作ったら、ブランチ名・作成時の HEAD の sha・起点の確認・レビュー差分の外で PR に入る commit の一覧を報告に残し、一覧が空でなければ PR 本文にも載せる
- 新規着手の判定(`do-task/references/base-commit.md`)の条件 ⑤ に例外を足した。同じセッションの `/ship-task` が作ったばかりの作業ブランチ(起点がローカルのデフォルトブランチの履歴の中にある)は、ローカルが origin より先行していても新規着手になる
- 追加修正記録の節の範囲は、コードフェンスの中の `## ` の行で切れなくなった。「記録あり」(新規着手の条件 ② と再開判定の (C))から保留の行を除いた
- テンプレートのヘッダに任意のメタ行 `> **無人実行**: {可 …}` を足した(無人ループに拾わせるときだけ人が付ける)
- 無人では外部 implementer を使わず、確認が要る外部レビュアー(`secret_paths` があるときの確認・`--command` の明示承認)も起動しない(内蔵で続行して報告する)

### 後方互換を破る変更

- `/ship-task` の Phase 2 で本文を照合し、承認の後に本文、またはステータス・基準コミット・無人実行以外のヘッダの行(`> **関連**:` など)が変わっていたら停止する(対話でも。設計レビューのやり直しが要る)。人がタスク MD を commit するときの pre-commit hook の整形も、これに当たる
- ローカルのデフォルトブランチが origin より先行しているとき、`/ship-task` が作った作業ブランチでは、`/do-task` の『基準不明』の問いの代わりに、先行する commit の一覧を示して続行を確かめる。origin/HEAD の無い構成(`git init` → `push -u`)で、これまで問わずに進んでいた場面でも問う

### 移行方法

- 既存のタスク MD を無人に回すには、/create-task で設計レビューをやり直して `本文` を足し、ヘッダにメタ行 `> **無人実行**: 可` を付ける。設計レビューの行が無い・`本文` が無い MD は、無人では保留になる
- v4.5.0 の書式の行(`本文` 無し)は、対話では報告して続行する
- 無人ループに回すプロジェクトでは、task_dir を書き込み型の format ゲートの対象から外し、commit のときに内容を書き換える hook(lint-staged の整形など)は無人の周では無効にする(書き換えない構成にしてもよい)。そのままだと、本文ダイジェストと commit の直後の tree の照合で、毎周が失敗扱いになる

## v4.5.0

### 変更

- `/create-task` は、設計レビューの結果を**タスク MD の追加修正記録に 1 行で残す**(設計レビューの行。結末を問わず `APPROVED` / `未収束` / `未完了` のどれかを書き、設計をやり直すときは先に `未完了` を足す)。書式・節の範囲・照合パターンの正本は `create-task/references/task-template.md` の記法の規約。合格語は「全レビュアー PASS」から「全レビュアー APPROVED」に揃えた
- `/ship-task` の Phase 2(needs-user・設計の独立レビュー)は、追加修正記録の節の中の**最後の設計レビューの行**で判定する。`.claude/reviews/` のログには頼らないので、別の worktree・別の PC でも判定できる
- `/do-task` の再開判定は、タスク MD の印(手順 5 に入る前からの基準コミット行 / `- [x]` / 追加修正記録に設計レビューの行以外の記録がある)の**いずれか**で行う。`.claude/reviews/` のログの有無は引き金にしない。git でないプロジェクトでも `- [x]` か記録で再開できる。検証のみモードは再開しない
- 新規着手の判定(`do-task/references/base-commit.md`)の条件 ② を「追加修正記録に、設計レビューの行のほかに記録が無い」に改めた(見出し・段落・表も記録として数える)。これで、短縮形式のタスクで死んでいた自動の新規着手の経路が戻る(v4.5.0 以降に作ったタスク。それより前のタスク MD は移行方法を参照)。do-task は追加修正記録を節の中に書く(`## ` の見出しを足さない)。条件 ③(ログの有無)は、ログが有るときだけ保守側に倒す例外として残した

### 移行方法

- v4.5.0 より前に作ったタスク MD には設計レビューの行が無い。既存のタスク MD で `/ship-task` の Phase 2 を通すときは、/create-task で再レビューして設計レビューの行を足す(`/ship-task` は Phase 1 で `/create-task` を回すので、通常の経路では影響しない)
- 追加修正記録に create-task・ship-task の自由記述の記録(メモ・見出し・段落)を持つ既存のタスク MD は、`/do-task` で再開モードに入り、新規着手の条件 ② は成り立たない(安全側。新規着手として扱いたい場合は、その記録を本文へ移す)

## v4.4.0

### 変更

- タスク保存先の解決で、`task_dir` にテンプレートの置換漏れ(`{{...}}`)が残っていれば停止する。v4.3.1 で `/init-project` だけが塞いでいた経路を、正本(`create-task/references/task-directory.md`)で全 skill に揃えた。`/init-project` は profile を補完する側なので、従来どおり未指定として扱い補完案を出す(判定は「値が `{{...}}` の形」から「値に `{{...}}` が残る」に揃えた)
- 外部ランナー: `review-agent.sh` の `--cwd` を必須化(`--dry-run` を除く)/ `review-agent.sh` が打ち切り(TERM・HUP・INT)で外部 CLI の子を止め、`ERROR [aborted]` を元の stderr へ出す。`review-agent.sh`・`implement-agent.sh` の両方で、`{prompt}` を持たないテンプレートの末尾にプロンプトを足すとき、`-` で始まるなら直前に `--` を挟む。ログの採番は `noclobber` で競合させない
- reviewer の死活監視の目安を 10 分から 20 分に(外部ランナーの既定の最大所要時間より長くする)
- 外部ランナーのレビューに渡すのは、一時ツリー内に作る `.review-snapshot.md`・`.review-diff.patch`(`--target` で渡す)と明記した(gitignore 対象のレビュー記録は渡さない)
- `/data-audit --quick` をエージェント単位に統一 / `/stack-research` のタスク化の確認を独立レビューの後へ移す

### 後方互換を破る変更

- `task_dir` に `{{...}}` を含む profile では、`/create-task`・`/do-task`・`/update-doc`・`/ship-task` が保存先の解決で停止する(`/do-task`・`/update-doc` にタスク MD を明示したときは保存先を解決しないので止まらない)。これまでは `{{TASK_DIR}}/` を作って進んでいた。`task_dir` が未指定のときは、管理ルート直下・`docs/` 直下・`.claude/` 直下に `{{...}}` を含む名前のタスクディレクトリがあれば停止する(`/init-project` もここで止まる)
- `review-agent.sh` は `--cwd` が無いと usage エラーで止まる(skill の手順は一時ツリーを `--cwd` で渡す)

### 移行方法

- `.claude/project-profile.yml` の `task_dir` を管理ルートからの相対パスに直すか、キーを消して検出に任せる
- `{{TASK_DIR}}/` のような置換漏れの名前のディレクトリが既に在れば、中身を正しい保存先へ移してから削除する
- `review-agent.sh` を直接呼んでいる場合は、`do-task/references/external-runners.md` §9-1 の手順で一時ツリーを作り `--cwd` で渡す

## v4.3.1

### 変更

- `/init-project` が `.claude/project-profile.yml` の `{{...}}` 残存(テンプレートの置換漏れ)を検査する — 既存 profile は Phase 1 の読み込み時に項目名を挙げて報告し、生成物は Phase 4 の自己確認で見る。`task_dir` が未置換のとき、**`/init-project` は**その値を保存先に採用しない(他の skill から使う経路は未対応)

## v4.3.0

### 変更

- 標準構成が `AGENTS.md` のみに(Claude Code 固有の指示は `.claude/rules/claude-code.md`)

### 後方互換を破る変更

- `source_of_truth` の値 `claude-md` を廃止し `agents-md` に改名した。有効な 3 値(`serena` / `docs` / `agents-md`)以外では skill が停止する

### 移行方法

- `.claude/project-profile.yml` の `source_of_truth: claude-md` を `source_of_truth: agents-md` に置換する
- `@AGENTS.md` を import する `CLAUDE.md` を持つ既存の構成(互換形)は引き続き正常。完成形への移行は `/init-project` の再実行で提案される
