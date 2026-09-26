# 発見の周(`/ship-task --discover=<発見元> --unattended`)

ship-task の発見の周の正本。発見の周は、発見元の候補モードが task_dir に書いた `候補_` だけを 1 commit にして、PR を開く。設計・実装・品質ゲート・レビューは行わない(採用の後の /create-task・/do-task で行う)。

- 無人モードの一般(一般則・G1〜G4・`loop.sh` の周の Bash の書き方・委託するサブエージェントの要点・失敗扱いの止まり方)は [unattended-mode.md](unattended-mode.md) が正本。この文書は、発見の周に固有の前提・工程・照合・結末だけを定める
- 候補モード(発見元の側の手順・結果の行・報告)と指摘キーの正本は [../../create-task/references/candidate-mode.md](../../create-task/references/candidate-mode.md)。`候補_` の書式は [../../create-task/references/task-template.md](../../create-task/references/task-template.md) の記法の規約
- 周の外側(発見元の選定・読み飛ばし・判定・朝の報告)は [loop.md](loop.md) の「発見モード」
- 要点は design §2「無人ループ」。決定の経緯は決定録 2026-09-23(#66)と Issue #69

## 1. 引数と最初の Read

- **無人専用**: `--discover=<発見元>` は `--unattended` と一緒にだけ受け付ける。`--unattended` が無ければ、何もせずに停止し、「対話で発見するなら各発見元を直接呼ぶ」と案内する
- **発見元の列**: `data-audit`・`refactor`
  - `loop.sh` の `--discover` が受け付ける名は、この列と同じにする(`loop-selftest.sh` が照らす)。列の外の値は前提の欠け(§4)
  - 呼ぶ候補モード: `data-audit` は `/data-audit --candidates --unattended`、`refactor` は `/create-task --refactor --candidates --unattended`
  - 値は、`候補_` の発見元の行・`candidate-keys.py --check --source=<発見元>`・作業ブランチの名(§2)・`.claude/reviews/` のファイル名に、そのまま使う
- **最初の Read**: unattended-mode.md とこの文書を Read し、その結果を受け取るまで、ほかのツール(特に Bash)を同じ応答に並べて呼ばない(ship-task の原則 6 と同じ)

## 2. 作業ブランチの名

- `task/候補-<発見元>-<S0 の先頭 12 桁>`(例 `task/候補-data-audit-819fcc33d0c0`)。S0 は開始時の HEAD の sha(§4)
- ブランチ名の規約(`task/...`)に収まる。S0 は周の worktree の起点(`loop.sh` が固定したデフォルトブランチの sha)なので、`loop.sh` はこの名を前もって知っていて、判定で照らせる
- 一意になる理由: 候補の PR を merge(merge commit・squash・rebase のどれでも)すると、デフォルトブランチが進み、次の晩の S0 が変わる。今夜の名のブランチが既にある発見元と、未 merge の候補のブランチがある発見元は、`loop.sh` が回さない(loop.md の発見モード)
- 同名のローカルのブランチがあれば、前提の欠け(§4)
- タスク名は `候補-` で始めない(create-task の命名)ので、実装の周の `task/{名}` と重ならない

## 3. origin の URL の読み方(`origin-repo.py`)

`python3 {ship-task の}scripts/origin-repo.py --dir=<ディレクトリ>` は、origin の URL を読んで、公開の確認と PR に使える push 先を決める。読み取り専用(git と `ssh -G` を打つが、何も書かない)。`loop.sh` の起動時(loop.md の発見モード)と、発見の周の前提(§4)・公開の確認(§5)・PR(§8)で使う。

- 出力: stdout に JSON `{"origin": <真偽>, "same": <真偽>, "repo": <"HOST/OWNER/REPO" | null>, "form": <"https" | "ssh" | "scp" | "other" | null>, "vcs": <真偽>, "reason": <文>}`
- 終了コード: 0 = 判定できた(JSON を読む)/ 2 = 使い方の誤り(`--dir` が無い・ディレクトリでない)・git が失敗した
- **URL の字面を出力しない**(認証情報を含みうる — design §5-12)。`reason` は人向けの短い文で、URL を入れない。判定は各欄で行い、`reason` の文言に頼らない

**判定の順**

1. `git -C <ディレクトリ> remote` に origin が無ければ、`origin` は偽(`same`・`vcs` は偽、`repo`・`form` は null)
2. `git config --get remote.origin.vcs` が空でなければ、`vcs` を真・`same` を偽・`repo` を null にする(`form` も null)。push・ls-remote が `git-remote-<vcs>` のヘルパーを通り、get-url の字面と送り先が離れるため
3. `git remote get-url --all origin`(fetch)と `git remote get-url --push --all origin`(push。`insteadOf`・`pushInsteadOf` は展開済み)が、それぞれちょうど 1 行であること。外れたら `same` は偽・`repo` は null(`form` も null)
4. `form` は push 側の形(下の 3 つの形のどれにも読めなければ `other`)
5. `same` を決める。偽なら `repo` は null
6. `repo` を決める(下の許す形の列挙)

**字面の読み方**(ssh の設定を解かない)。次の 3 つの形だけを読む。

- `https://[userinfo@]HOST/OWNER/REPO[.git][/]`
- `ssh://[user@]HOST/OWNER/REPO[.git][/]`
- scp の形 `[user@]HOST:OWNER/REPO[.git]`(HOST に `/` を含まず、パスが `/` で始まらない)

OWNER・REPO はそれぞれ 1 段で `[A-Za-z0-9._-]+`。ポートを明示した形は読まない。読めた URL の直した値は `HOST/OWNER/REPO`(HOST は小文字。userinfo・`.git`・末尾の `/` を落とす)。

**`same`**(fetch と push が同じリポジトリか。`loop.sh` の `ls-remote origin` は fetch 側を見るので、push 先と食い違わないように)

- 両方が読めたら、直した値が一致するか。HOST が `github.com` のときは、OWNER・REPO も小文字にしてから比べる(GitHub は大小を区別しない)。ほかのホストは字面のまま(安全側)
- どちらかが読めなければ、2 つの URL の字面が一致するか

**`repo`**(公開の確認と PR に使う push 先。許す形の列挙): push 側が次のどちらかのときだけ、push 側の直した値にする(OWNER・REPO の大小は字面のまま)。ほかは null。

- https の形。ただし、TLS の検証を外していれば null(`git config --type=bool --get-urlmatch http.sslverify <push の URL>` が `false`、または環境変数 `GIT_SSL_NO_VERIFY` がある。設定が無い〈rc 1〉なら検証ありと読む)。https の送り先の正しさは、TLS の検証が担保するため
- scp か `ssh://` の形で、ユーザー名が `git`・HOST が `github.com`。さらに次をすべて満たす
  - ssh を上書きする設定が無い: 環境変数 `GIT_SSH_COMMAND`・`GIT_SSH` が無く、`git config --get core.sshCommand` が空
  - `ssh -G -- git@github.com`(タイムアウト 10 秒・stdin は `/dev/null`。引数は固定)の出力の `hostname` の行が `github.com`、`port` の行が `22`

ssh の設定の別名(`github-work` など)・`ssh.github.com` の 443・ほかのユーザー名・ssh の上書きは、許さない。git の実際の接続を再現しきれないため(`Match user` のように、ユーザー名の有無だけでポートが変わる構成がある)。

- `ssh -G` は接続しない。ただし、利用者の ssh の設定の `Match exec` のコマンドは実行されうる(利用者の設定は信頼の範囲 — §10)
- ProxyCommand・ProxyJump は、`%h:%p` への中継として信頼する(ホスト鍵の検証が github.com への到達を担保する)
- 利用者の git・ssh の設定は信頼の範囲。この列挙は、送り先を URL の字面から離す主な経路を外すが、すべての設定を網羅はしない(§10)

## 4. 前提

ship-task の Phase 0 の 2′ で、ブランチを作る前に、何も書き換えずにすべて確かめる。1 つでも欠けたら失敗扱い(G3)。報告に、欠けた項目と直し方を書く。

- 併用しない引数が無い: `--task`・`--branch`・`--design-only`・`--refactor`・`--compact`・`--light`・`--no-review`・`--reviewers`・`--runners`・`--max-iter`(`--no-pr` は許す。そのときの結末は `縮退`)
- 発見元が §1 の列にある
- parent-child 構成でない(管理ルート = profile の `root`。タスクの周と同じ — unattended-mode.md §1)
- 開始時の HEAD が、デフォルトブランチか detached HEAD で、ローカルのデフォルトブランチの履歴の中にある(判定と起点の確認は、タスクの周と同じ — unattended-mode.md §1)。S0 = 開始時の HEAD の sha と、開始時の ref(`git symbolic-ref --quiet HEAD` の値か、detached HEAD)を控える
- 作業ツリーが清潔(ship-task の Phase 0 の 3 と同じ。無人では S1)
- **状態ファイルが ignore されていて、追跡されていない**: `.claude/reviews/x.md`・`.claude/grasp.md`・`.claude/.understand-project-done` の 3 つを、それぞれ `git check-ignore -q -- <パス>` で確かめ、すべて rc 0
  - `--no-index` を付けない(付けると、追跡済みのファイルも ignore 済みと答える)
  - `.claude/reviews/x.md` は `.claude/reviews/` の下を表す名で、在らなくてよい
  - 外れたら、gitignore の断片(init-project が入れるもの)と、追跡済みなら `git rm --cached` を案内する
  - 理由: 照合(§7)と `loop.sh` の判定は「未追跡・未 commit が無い」を求めるので、状態ファイルが ignore されていないと、正しい周も失敗になる
- **origin の URL**: `python3 {ship-task の}scripts/origin-repo.py --dir=<管理ルート>`(§3)が exit 0 で、`origin` が真なら次の 2 つを満たす。`origin` が偽なら満たしたとみなす(push しないので、候補があれば結末は `縮退`)
  - `vcs` が偽。真なら、欠けの理由を `remote.origin.vcs`(設定を外す案内)にする。fetch と push の食い違いの理由にしない(`loop.sh` の理由コード `origin-vcs` と揃える)
  - `same` が真(fetch と push の URL がそれぞれ 1 つで、同じリポジトリを指す)。偽なら、fetch と push の URL を揃える案内をする
  - exit 0 でなければ、「origin の URL を読めない」を理由にする。どの理由にも URL の字面を書かない
- **task_dir**: helper(`resolve-task-dir.py`)で解決できる(exit 1・2 は欠け)。保護パスの下でない(列は unattended-mode.md の「委託するサブエージェント」の項)。`secret_paths` に当たらない。`git check-ignore --no-index -q -- '<task_dir>/候補_x.md'` が rc 1(ignore 済みの場所では commit できない)
- 同名の作業ブランチ(§2)がローカルに無い

## 5. 公開の確認(data-audit の周だけ)

前提を満たした後・発見元を呼ぶ前に行う。refactor の周では行わない(refactor の候補モードはセキュリティ由来の指摘を候補にしないので、公開のリポジトリでも回す — candidate-mode.md)。data-audit の候補(未修正の脆弱性の場所と筋書き)は、origin が非公開と確かめられたときだけ PR にする。

1. origin-repo.py の `origin` が偽なら、確かめずに続ける(push しないので、候補があれば結末は `縮退`。候補はローカルのブランチだけに残る)
2. `repo` が null なら「確かめられない」。gh を打たない(ローカルのパスは許可の仲介に拒否され、G1 で失敗扱いになるため。URL の字面を渡すと、認証情報が記録に写りうるため)
3. `gh repo view '<repo>' --json isPrivate -q .isPrivate` の出力がちょうど `true` なら続ける。`false`・エラー・それ以外の出力なら「確かめられない」
   - repo は引数で渡す。gh の既定のリポジトリの解決(`upstream` などのリモート・`GH_REPO`・`gh repo set-default`)に頼ると、確認の対象が push 先と食い違いうる。`HOST/OWNER/REPO` の形を受け付けることと、引数が `GH_REPO` より優先されることは実測した(design §7-3)
4. 「確かめられない」ときは、発見元を呼ばずに、結末 `候補なし — data-audit: origin が非公開と確かめられないため回さなかった(<理由の要約>)` で終わる。何も書かず、ブランチも作らない(失敗扱いではない)
5. PR は `gh pr create -R '<repo>'` で作る(§8)

- 許可の拒否(G1)で git・gh が打てないときは、「確かめられない」ではなく失敗扱い(発見の周の G1 は常に失敗扱い — §9)

## 6. 工程

ship-task の Phase 0 の 1(把握)・2(profile 解決)は、そのまま行う。2′ で前提(§4)を確かめ、data-audit なら公開の確認(§5)を行う。Phase 0 の 3〜6 と Phase 1〜4 は、次の工程に差し替える(清潔性は前提に含む。作業ブランチは工程 D3 で作る。origin と gh は §4・§5・§8 で確かめる。実装の委託は無い)。

- **工程 D1**: 発見元の候補モードを呼ぶ(§1 の対応。`--unattended` を必ず渡す)。候補モードは、task_dir に新しい `候補_*.md` を書き、書いたパスの一覧と結果の行(`候補モードの結果: …`。書式は candidate-mode.md)を返す。改名・commit・チェーンの提案はしない
- **工程 D2**: 結果の検証(§7 の表の「工程 D2」の行)。通らなければ失敗扱い(G2)
- **工程 D3**: 候補の集合 C が空なら、ブランチ・commit・PR を作らずに、結末 `候補なし` で終わる。1 件以上なら、次を行う
  1. 作業ブランチ(§2)を `git switch -c <作業ブランチ>` で作る
  2. ship-task の Phase 0 の 4 の 4 項目(ブランチ名・作成時の HEAD の sha・起点の確認・先行する commit の一覧)を報告に残す。一覧は確かめずに、報告と PR 本文に載せる(S8)
  3. 候補だけを stage して、1 commit する(§8。直前・直後の照合 — §7)
- **Phase 5**: 照合(§7)→ push → PR(§8)

## 7. 周の中で守る値と照合(改竄ガード)

**守る値**(セッション文脈に保持し、報告にも書く): 発見元・task_dir・S0・開始時の ref・候補の集合 C・作業ブランチ名・最後に知る HEAD・控えた tree・origin-repo.py の `repo`(認証情報を含まない)。値を失ったら失敗扱い(G4)。発見の周は、本文ダイジェスト R を持たない(タスク MD が無い)。

- 最後に知る HEAD は、ブランチを作ったときに S0 にし、commit の直後の照合に通ったら、その HEAD に更新する
- 照合の git 呼び出しには、[base-commit.md](../../do-task/references/base-commit.md) の前置き(`--no-pager --no-replace-objects` ほか)を付ける(unattended-mode.md §7 と同じ)

**照合**: 1 つでも通らなければ失敗扱い(G2)。算出できない(helper の exit 1・2 など)も、通らないとみなす。

| 時点 | 照合 |
|---|---|
| 工程 D2(発見元から戻った直後) | ① HEAD の ref が開始時のまま・`HEAD` = S0 ② index が空(`git diff --cached --name-only -z` が空) ③ `git status --porcelain=v1 -z --untracked-files=all` の項目が、すべて `?? <task_dir>/候補_<名>.md`(状態ファイルは ignore 済みなので出ない) ④ ③ の候補の集合 C = 発見元が返した一覧で、結果の行の種類と件数が C と合う(`書き出し` なら N = C の件数で 1 以上、`候補なし` なら C が空。`失敗扱い` か、結果の行が無ければ失敗扱い) ⑤ C の各ファイル: 通常ファイルで symlink でない・`{名}` が空でなく、`-` で始まらず、[loop.md](loop.md) §3 の文字の制限と `git check-ref-format --branch 'task/{名}'` を満たす・同じディレクトリに同じ `{名}` の別の状態名の MD が無い・`python3 {create-task の}scripts/candidate-keys.py --check --source=<S> <パス>` が exit 0・`secret_paths` に当たらない |
| ブランチを作った直後 | 現在のブランチ = 作業ブランチ・`HEAD` = S0 |
| stage の直前 | 現在のブランチ = 作業ブランチ・`HEAD` = S0・`git status --porcelain=v1 -z --untracked-files=all` の項目が、ちょうど C の `??` だけ(工程 D2 の後に増えていない) |
| commit の直前(stage の後) | 現在のブランチ = 作業ブランチ・`HEAD` = 最後に知る HEAD・`git diff --cached --name-status -z --no-renames` が C の `A` だけ・`git status --porcelain=v1 -z --untracked-files=all` の項目が、ちょうど C の `A `(未追跡と、stage していない変更が無い)。通ったら `git write-tree` を控える |
| commit の直後 | `HEAD^{tree}` = 控えた tree・`git diff --name-status -z --no-renames S0 HEAD` が C の `A` だけ・`git rev-list --count S0..HEAD` = 1・C の各パスを `git show HEAD:./<パス> \| python3 {create-task の}scripts/candidate-keys.py --check --source=<S> -` で確かめる・現在のブランチ = 作業ブランチ・`git status --porcelain=v1 -z --untracked-files=all` が空 |
| push の直前 | unattended-mode.md §7 の「push の直前」と同じ(`refs/heads/<作業ブランチ>` = 最後に知る HEAD・現在のブランチ = 作業ブランチ) |

- 表の `<S>` は発見元。「現在のブランチ」は `git symbolic-ref --quiet HEAD` で見る
- 失敗扱いで報告に添えるもの(commit の直後の tree の不一致なら、控えた tree と `HEAD^{tree}` の差分)は unattended-mode.md §6

## 8. commit・push・PR

- **stage**: C の各パスを `git add -- ':(literal)<パス>'` で足すだけ
- **commit**: 1 本。メッセージは `git log --oneline -20` の流儀に合わせる(例 `chore: 発見の候補 — data-audit(3 件)`)。`Write` で `.claude/reviews/discover-<発見元>-msg.md` に置き、`git commit -F .claude/reviews/discover-<発見元>-msg.md` で渡す
- **push**: origin があり、`--no-pr` でなければ、push の直前の照合(§7)の後に `git push -u origin <作業ブランチ>`。共有の `config` に `branch.<作業ブランチ>.remote`・`.merge` が付く(`loop.sh` が許す — loop.md の発見モード)。push の失敗・拒否は失敗扱い
- **PR の前の確かめ**: `repo` が null か、`gh repo view '<R>' --json name -q .name` が rc 0 でなければ(active なアカウントでそのリポジトリを読めない)、PR を作らずに結末 `縮退`(push はする)
  - `gh auth status` では決めない(複数のアカウントのどれかに問題があるだけで rc 1 になり、active なアカウントがリポジトリに触れるかも見ない)
- **PR**: `gh pr create -R '<R>' --base <デフォルトブランチ> --head <作業ブランチ> --title '<タイトル>' --body-file - < .claude/reviews/discover-<発見元>-pr.md`
  - R は origin-repo.py の `repo`(`HOST/OWNER/REPO`)。refactor の周も同じ R を使う(gh の既定のリポジトリの解決に頼らない — §5)
  - タイトルは `候補: <発見元>(<N> 件)`。draft にせず、アサインもしない
  - PR 作成の失敗・拒否は失敗扱い
- **`縮退`**: origin が無い・`--no-pr`(どちらも push しない)/ `repo` が null・`gh repo view` が通らない(push はする)。報告に、理由と push の有無を書く。手で PR を作るコマンドの雛形と、作らないときの消し方は、`loop.sh` の朝の報告に出る(loop.md の発見モード)

**PR 本文**: 候補のファイルと、発見元の報告(`.claude/reviews/candidates-<発見元>.md`)からの転記だけ。推測で書かない。`Write` で `.claude/reviews/discover-<発見元>-pr.md` に置く。

1. 発見ループの候補であること(merge すると task_dir に `候補_` として入るが、実装はされない)
2. 候補の一覧(ファイル・指摘 1 行・場所・深刻度と確度かスコア・指摘キー)
3. 人がすること
   - 要らない候補は、見送りの行を足すか、消してから merge する(消す = また出てよい / 見送り = もう出さない)。写して使える見送りの行の字面 `> **見送り**: YYYY-MM-DD — <理由>` を載せる(区切りは `—`)
   - 見送り・既知は、同じ観点群・同じ場所(refactor はファイル)の以後の指摘を、すべて止める
   - PR を閉じるだけだと、また出る
   - merge したら、作業ブランチを消す
   - 採用は、merge の後に `/create-task <候補_ のパス>`
4. 走査の範囲・未監査と理由・既知として除いた件数・回帰の疑い。data-audit の PR だけ、除外(誤検知の除去)の件数も載せる。refactor の PR には除外の件数を出さない(公開の PR で、未修正のセキュリティの問題があることを知らせないため — candidate-mode.md)
5. 先行する commit の一覧(空でなければ。件名はコードブロックに入れる)
6. 「設計レビュー・品質ゲートは、採用の後の /create-task・/do-task で行う」

## 9. 結末

- 完了報告の最後に、結末の行 `無人の周の結果: <結末> — <詳細>` を書く(照合パターンは unattended-mode.md §2)
- 発見の周の結末は `PR`・`縮退`・`候補なし`・`失敗扱い` の 4 つ。`保留` は出さない
- 詳細には、書き出し・既知・回帰の疑いの件数を入れる(例 `無人の周の結果: 候補なし — data-audit: 新しい候補 0 件(既知 2 件・回帰の疑い 0 件)`)。`PR` なら URL も入れる。refactor の周では、除外の件数を入れない

**保留は無い**

- 発見の周には改名の先が無く、人の判断は PR の上で行う
- 許可の拒否(G1)は、常に失敗扱い(保留のガードを満たしえない)。`loop.sh` では、連続失敗のブレーカーで止まる

**失敗扱いで残るもの**(止まり方は unattended-mode.md §6。どれも worktree は残り、`loop.sh` が以後その発見元を回さない)

- 前提の検査で止まった: 何も無い
- 発見元の途中・工程 D2 で止まった: 未追跡の候補
- ブランチの作成の後・commit の前: ブランチと候補
- commit の後(照合・push・PR の失敗): ブランチと commit

**原則 4 の読み替え**: 発見の周の「緑」は、§7 の照合にすべて通ったこと。品質ゲート・レビュー・実動確認は、採用の後の /create-task・/do-task で行う(実装を含まない候補の PR への、design §5-20 の例外)。

## 10. 受け入れる限界

- 候補の中身の正しさ・機密の値の混入・候補の文面に仕込まれた指示は、機構では守らない。人が PR で読み、採用のときに create-task が実コードで裏取りする(candidate-mode.md の限界)
- 公開の確認の後に公開に変えられた場合は、防げない。PR は、非公開のリポジトリの中に留まる前提。周の許可リストが `gh` を丸ごと許すと、周の中で公開に変える操作も通る(#107)
- origin の push 先が §3 の許す形(https、または上書きの無い `git@github.com`)でないか、gh で isPrivate を読めないホストなら、data-audit の発見は回らない(毎晩 `候補なし` と報告する)。refactor は、PR を作らずに `縮退` になる(ssh の設定の別名・`ssh.github.com` の 443・ポートの明示・ローカルのパス・TLS の検証を外した構成など)。`縮退` で push したブランチを処理するまで、refactor は回らない
- 利用者の git・ssh の設定は信頼の範囲。§3 は、送り先を URL の字面から離す主な経路を外すが、網羅はしない。`ssh -G` は、利用者の ssh の設定の `Match exec` を実行しうる
- fetch と push の URL が、字面から直した値で別のリポジトリか、`remote.origin.vcs` があれば、発見の周は前提で失敗扱いになる(`loop.sh` は起動時に exit 20 で止まる。refactor も回らない)
- `/ship-task <候補_ のパス>`(説明として渡す)は、採用の入口として保証しない。採用は `/create-task <候補_ のパス>` で行ってから、`/ship-task --task=<進行中_ のパス>` を使う
- 周の外側の限界(未 merge の前夜の PR・squash や rebase で merge したブランチ・閉じた PR のブランチが、その発見元を止め続けること、ほかのアプリの書き込みなど)は loop.md §10
