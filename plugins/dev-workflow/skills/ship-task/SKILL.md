---
name: ship-task
description: タスクの設計から実装・ドキュメント同期・PR 作成までを 1 コマンドで一気通貫に実行する。「まるっとやって」「タスクを作って実装から PR まで一気に」「一気通貫でやって」「設計から PR まで通して」と言われたときに使う。/create-task で設計書を作り、要件に不明が残らなければ /do-task で実装・検証・レビュー、/update-doc --task で doc を同期し、作業ブランチで commit・push して PR を開く。設計の情報が欠ければ設計書だけ返し、品質ゲート赤・スコープ縮小・レビュー未収束では停止して PR を作らない。既存のタスク MD から回すときは --task、無人ループの周は --unattended(対話点で止まらず、保留か失敗扱いに倒す)、発見ループの周は --discover=<発見元> --unattended(発見元が積んだ候補_ だけを PR にする)を使う。工程を個別に回したいときは各スキルを直接呼ぶ。
argument-hint: "<タスク内容の説明> | --task=<タスク MD> [--unattended] [--design-only] [--no-pr] [--branch=<名前>] [--refactor [対象]] [--compact] [--reviewers=1|3] [--runners=<名前,...>] | --discover=<発見元> --unattended [--no-pr]"
---

# ship-task — 設計 → 実装 → doc 同期 → PR の一気通貫実行

## 原則

1. **薄いオーケストレーターに徹する**。工程の中身は既存スキル(/create-task・/do-task・/update-doc)が持つ。このスキルは順序・続行判定・git 出口だけを担い、各スキルの手順を再定義しない
2. **既定は走り切る**。工程間でいちいち確認を取らない。止まるのは下の「停止条件」に該当したときだけ(サイレント続行も、無意味な確認も避ける)
3. **要件不明のまま実装しない**。設計に必要な情報が欠けている場合は /create-task の中で確認する。それでも不明が残るなら、設計書を成果物として返し実装へ進まない(誤った物を全力で作らないため)
4. **PR は「緑」でしか開かない**。ここでの緑は、品質ゲートが緑・レビューが全員 APPROVED・実動確認が「実施済」か「実施不能」(状態の定義は /do-task の Phase 5.5 の表)の 3 つが揃うこと。揃わない状態で PR を作らない。停止時は作業ブランチと commit を残し、何が未達かを報告する。発見の周(`--discover`)の「緑」は、[references/discover-mode.md](references/discover-mode.md) の照合にすべて通ったこと(品質ゲート・レビュー・実動確認は、採用の後の /create-task・/do-task で行う。design §5-20 の例外)
5. **git 操作の範囲を明示する**。ブランチ作成・commit・push・PR 作成はこのスキルの責務だが、マージはしない。レビュアーの自動アサインもしない
6. **無人モードは正本に従う**。`--unattended` のときは、対話点で止まらず「自動で答える / 保留 / 失敗扱い」のどれかに倒す。対話点ごとの扱い・保留の手順・周の中の照合・結末・限界は [references/unattended-mode.md](references/unattended-mode.md) が正本(以下の各所には 1 行の分岐だけを置く)。発見の周に固有の前提・工程・照合・結末は references/discover-mode.md が正本。`--unattended` のときは、最初に references/unattended-mode.md を Read し(`--discover` もあれば references/discover-mode.md も同じ応答で Read し)、その結果を受け取るまで、ほかのツール(特に Bash)を同じ応答に並べて呼ばない(特に「`loop.sh` の周の Bash の書き方」。読む前に打った Bash が許可の仲介に拒否されると、打ち直さずに失敗扱いになる)

## オプション

| オプション | 内容 |
|---|---|
| (なし) | 設計 → 実装 → doc 同期 → PR まで全工程 |
| `--task=<タスク MD>` | 既存のタスク MD(管理ルート相対。`進行中_{名}.md` の通常ファイル)から始める。Phase 1 を飛ばし、Phase 0 で作業ブランチを作ってから Phase 2 へ進む。`--design-only`・`--refactor`・`--compact`・`--light` とは併用できない(create-task のための引数。指定されたら停止する) |
| `--unattended` | 無人モード(無人ループの 1 周)。`--task` か `--discover` が必須。前提・対話点ごとの扱いは [references/unattended-mode.md](references/unattended-mode.md) |
| `--discover=<発見元>` | 発見の周(無人ループの発見モードの 1 周)。発見元の候補モードが書いた `候補_` だけを commit して PR を開く。`--unattended` と一緒にだけ受け付ける(無ければ停止する)。発見元の列・前提・併用しない引数・工程・照合・結末は [references/discover-mode.md](references/discover-mode.md) |
| `--design-only` | Phase 1 のみ(= /create-task 単体と同じ)。設計書を返して終了 |
| `--no-pr` | Phase 5 の push / PR 作成を行わない(ブランチ + commit まで) |
| `--branch=<名前>` | 作業ブランチ名を明示(省略時は `task/{タスク名}`) |
| `--refactor [対象]` | /create-task の対象発見型リファクタモードで設計する |
| `--compact` | /create-task の短縮設計を要求する。起点で `--no-review` と profile のレビュー省略設定、`--light` と profile の影響調査省略設定を無効化して報告する。通常の `--light` 単独では影響範囲調査を省略できるが、compact の実行は通常形式へ戻っても維持する。/update-doc へ `--no-review` を渡さない |
| `--reviewers=1\|3` | /do-task のレビュアー数を指定 |
| `--runners=<名前,...>` | 外部 CLI レビュアー(オプトイン)を /create-task・/do-task・/update-doc へ透過。既定は内蔵のみ |

`--light` `--no-review` `--max-iter` など各スキル固有の引数は、そのまま該当工程へ透過的に渡す。通常の `--light` は影響範囲調査だけを省略できる。`--compact` の実行では `--light` と profile の影響調査省略設定、`--no-review` と profile のレビュー省略設定を無効化し、通常形式へ戻っても維持する。後段の /update-doc へ `--no-review` を渡さない。

## Phase 0: 前提と作業ブランチ

1. **把握**: `.claude/grasp.md` は参照索引として確認し、毎回、現在の profile・権威参照ファイル・関連文書・設定・対象領域と依存先を読む。前回の把握状況を理由に省略しない(深化は /create-task の Phase 0 に任せる)
2. **profile 解決**: `.claude/project-profile.yml` から `root`・`quality`・`features` を得る(無ければ動的検出)
2′. **入口と無人の前提**(`--task` / `--unattended` / `--discover` のとき。手順 3 より前に、何も書き換えずに検査する):
   - `--task`: パスが管理ルート相対で、ファイル名が `進行中_{名}.md` の通常ファイルであること。併用できない引数(オプション表)が無いこと。満たさなければ停止する(無人では失敗扱い)
   - `--discover`: `--unattended` が無ければ停止する(対話で発見するなら各発見元を直接呼ぶよう案内する)。あれば、発見の周の前提(併用しない引数・状態ファイルの ignore・origin の URL・task_dir・同名の作業ブランチなど。一覧は references/discover-mode.md §4)を確かめ、1 つでも欠けたら失敗扱い(G3。ブランチを作らない)。以後の手順 3〜6 と Phase 1〜5 は、discover-mode.md の工程(公開の確認 → 工程 D1〜D3 → Phase 5)に差し替える
   - `--unattended`(`--discover` が無いとき): `--task` が無ければ失敗扱い。`--branch` が無い・parent-child 構成でない(管理ルート = `root`)・開始時の HEAD がデフォルトブランチか detached HEAD(ローカルのデフォルトブランチの履歴の中)・メタ行がある・タスク MD が git の追跡下にある、を検査する(一覧と理由は references/unattended-mode.md §1)。1 つでも欠けたら失敗扱い(G3。ブランチを作らない)。最後に本文ダイジェスト R を `python3 {create-task の}scripts/task-digest.py <タスク MD>` で算出して保持する(exit 0 以外は失敗扱い)。以後、周の中で守る値(同 §7)を失ったら失敗扱い(G4)
3. **作業ツリーの清潔性**: `git status --short` を確認。無関係な未コミット変更があれば、PR にそれが混ざる旨を警告して続行可否を確認する(ここは安全のため必ず確認する)。無人では確認せず失敗扱い(S1)
4. **作業ブランチ**: 現在のブランチを `git symbolic-ref --quiet HEAD` で見る(rc 1 = detached HEAD。detached HEAD では `git branch --show-current` が空になり、名前の照合では決まらない)。`DEF_REF`・`DEF_NAME` は [../do-task/references/base-commit.md](../do-task/references/base-commit.md) と同じ手順で求める(`refs/remotes/origin/HEAD` → `refs/heads/main` → `refs/heads/master`)。**デフォルトブランチにいるかは、現在のブランチ名(`git branch --show-current`)で判定する**(従来と同じ): `refs/remotes/origin/HEAD` を解決できたら(`git symbolic-ref --quiet refs/remotes/origin/HEAD`)、その名前(`DEF_NAME`)との一致だけで判定する。解決できなければ、`main` / `master` のどちらかであれば「いる」とする(origin/HEAD が無く main と master が両方あるリポジトリで、master の上を作業ブランチと取り違えないため)。`DEF_REF`・`DEF_NAME` は、この判定のほか、下の起点の確認・一覧の算出に使う。これらの git 呼び出しの前置きも base-commit.md と同じ
   - `--branch` がある → 現在のブランチに関わらず、**ここで**その名前で作成して切り替える(`git switch -c <名前>`。従来どおり。無人は 2′ で `--branch` を禁じている)
   - `--branch` が無く、デフォルトブランチか detached HEAD にいる → 作業ブランチ `task/{タスク名}` を作る(`git switch -c task/{タスク名}`)。`--task` のときはタスク名(ファイル名の `{名}`)が決まっているので、**ここで作る**(Phase 2 より前)。それ以外は Phase 1 の後に作るので、ここでは判定だけ行う
   - `--branch` が無く、既に作業ブランチにいる → そのまま使う(新規作成しない。`--task` のときも同じ)。無人では 2′ で失敗扱い済み
   - 作れなければ停止する(同名のブランチがある等。無人では失敗扱い)
   - **作ったら、次の 4 項目を報告に残す**(/do-task の新規着手の条件 ⑤ の例外が読む — base-commit.md): ブランチ名 / 作成時の HEAD の sha / 起点の確認(その sha がローカルのデフォルトブランチの履歴の中にあるか。`git rev-list --count refs/heads/<DEF_NAME>..<sha>` が rc 0 で `0` を出せば「中にある」) / 起点が中にあるときだけ、`git rev-list refs/remotes/origin/<DEF_NAME>..<sha>` の一覧(レビュー差分の外で PR に入る commit。PR の base の remote-tracking ref と比べる。`refs/remotes/origin/<DEF_NAME>` が無ければ、比べられないことを報告する)
   - **一覧の確認(S8)**: 一覧が空でなければ、base-commit.md の代替基準の候補の提示と同じ形式(`git log --no-show-signature --no-decorate --format='%H %s'`。同じ置換と上限)で示し、続行するかを確かめる。「続行しない」なら停止する。ブランチは残し、手動の再開(/do-task で基準を指定する、またはブランチを消して先行する commit を整理してからやり直す)と、このブランチには条件 ⑤ の例外を当てないことを報告に残す。無人では確かめず、報告と PR 本文に載せて続ける。起点の確認に失敗したときは一覧を出さない(do-task の『基準不明』の提示に任せ、同じ commit について 2 回問わない)
5. リモートと `gh` の有無を先に確認しておく(`git remote` / `gh auth status`)。無い場合は Phase 5 が縮退することを**この時点で報告**する(最後まで走ってから「PR を作れません」と言わない)
6. **外部実装委託の承認を先取りする**: profile によって実装の委託先が外部に解決される場合(解決順・parent-child での縮退・提示内容・保持と取り消しの規則はすべて /do-task の Phase 0・Phase 3 と [../do-task/references/external-runners.md](../do-task/references/external-runners.md) §12 に従う)、**Phase 3 まで待たずにこの時点で承認を取る**(原則 2「既定は走り切る」を保つため、工程の途中で承認待ちにしない)。承認はセッション内で 1 回の判断として保持され、Phase 3 の /do-task がそれを再利用するので**同じ承認を 2 回求めない**。**承認が得られなくても停止しない** — 内蔵 implementer で走り切り、その旨を報告する(停止条件には加えない)。**先取りした承認は、/do-task の Phase 3 の外部の手順の手順 1(承認より先に判定する縮退)で内蔵に縮退したとき(条件 A: タスク MD がファイルでない・git でない・条件 B: 最後に承認した一覧に filter がある — external-runners.md §12-1)は使わず、そのことを報告する**(先取りの承認は /do-task の事前検査より先にあるので、承認を取った後にこの縮退に当たることがある)。**このための引数は設けない**(承認は発話でのみ行う)。無人では承認を求めず、内蔵 implementer で走り、そのことを報告する(S2)

## Phase 1: 設計(/create-task)

`--task` のときは実行せず、Phase 2 へ進む(S3)。

`--discover` のときは実行しない(references/discover-mode.md の工程 D1 で、発見元の候補モードを呼ぶ)。

`--refactor` や `--light` 等の引数を渡して **/create-task をそのまま実行**する。生成物は解決した保存先の `進行中_{タスク名}.md`。

- 設計に必要な情報が不足している場合は、/create-task の規定どおり **AskUserQuestion で確認する**(推測で埋めない)
- Phase 0 の 4 でまだ作っていない(デフォルトブランチか detached HEAD にいた)ときは、タスク名が確定したら作業ブランチを作成・切り替え(`git switch -c task/{タスク名}`)、Phase 0 の 4 の 4 項目の報告と一覧の確認(S8)を行う
- /create-task が返した**実際のタスク MD パス**を保持し、以降の /do-task・/update-doc・commit・PR に同じパスを渡す。保存先を推測・再構築しない([../create-task/references/task-directory.md](../create-task/references/task-directory.md))。`--task` のときは、渡されたパスを同じように扱う。改名の後(/do-task の `完了_`、無人の保留の `保留_`)は、同じディレクトリの改名後のパスを使う(旧いパスは commit の後の照合で読めない)
- /create-task が「複数タスクへの分割」を提案した場合は、**一気通貫を中止して分割案を報告する**(このスキルは 1 タスク 1 PR を単位にする)

## Phase 2: 続行判定ゲート(唯一の自動停止判断)

設計書とその完了報告を機械的に検査し、**実装に進めるかを判定する**。以下のいずれかに該当したら Phase 3 へ進まず、設計書を成果物として返して終了する(理由と、判断に必要な情報を添える)。

1. **要件不明の残存**: タスク MD に `grep -nE '未確定|要確認|要ヒアリング|TBD|ユーザー確認待ち|いずれか(を)?選択'` がヒットし、それが**実装の分岐を左右する**もの(仕様・値・対象範囲・期待挙動)である
   - 「実装時に判断」レベル(命名・内部構造・ログ文言など、どちらでも要件を満たす選択)は停止理由にしない
2. **needs-user の残存**: タスク MD の追加修正記録の節の中の最後の設計レビューの行で、needs-user が `なし` でない(行の書式・節の範囲・照合パターンは [../create-task/references/task-template.md](../create-task/references/task-template.md) の記法の規約が正本)
3. **完了条件が検証不能**: 完了条件が「① 操作 ② 期待される観察結果」の実行可能な手順になっておらず、実動確認の可否を判定できない
4. **前提タスク未完**: タスク MD の概要に「依存: 完了_X の後」があり、その依存が未完了
5. **設計の独立レビュー未完了**: 追加修正記録の節の中の最後の設計レビューの行が無い、または判定が `APPROVED` でない(`未収束` / `未完了`)。行は照合パターンで探し、節の外(本文の例など)の行は拾わない。`.claude/reviews/` のログには頼らない(別の worktree・別の PC には無い — design §5-18 ①)
6. **本文の照合**: 最後の設計レビューの行の `本文`(取り出しのパターンは記法の規約)と、現在の本文ダイジェスト(`python3 {create-task の}scripts/task-digest.py <タスク MD>`)が一致しない(承認の後に本文、またはステータス・基準コミット・無人実行以外のヘッダの行が変わった)。行に `本文` が無い(v4.5.0 の書式)・`本文: 算出不能`・現在の値を算出できない(構造・スクリプトや Python が無い)ときは、その旨を報告して続行する

判定結果(続行 / 停止 + 該当項目)を**必ず報告に残す**。停止時は「この情報が決まれば /ship-task を再開できる」形で不明点を列挙し、`/do-task {実際のタスクMDパス}` からの手動再開の導線も示す。

**無人では**、6 は R と照合する。1〜6 のどれかに当たったら保留にする(S4。1 は「実装の分岐を左右しない」と言い切れないヒットも保留に倒す。6 は `本文` が無い・`算出不能` のときも保留)。保留の手順は references/unattended-mode.md §5。

`--design-only` の場合はこの Phase を実行せず、設計書を返して終了する。

`--discover` のときはこの Phase を実行しない(`候補_` は設計の手前。設計と設計レビューは、採用の後の /create-task で行う)。

## Phase 3: 実装(/do-task)

`--discover` のときは実行しない(実装は、採用の後の /do-task で行う)。

実際のタスク MD パスを対象に **/do-task を実行**する(`--reviewers` / `--runners` 等は透過。無人では `--unattended` も渡す)。ブランチは作成済みのため `--branch` は渡さない。

/do-task が以下で終わった場合は **Phase 4 へ進まず停止**する(実装 commit だけ残し、PR は作らない)。無人では、停止時に実装 commit だけ残すことはしない。do-task が返した分類に従い、保留か失敗扱いにする(S5):

- 品質ゲートが赤のまま
- スコープ縮小を検出して差し戻しが解決しない
- レビューが未収束(全 reviewer APPROVED に達していない)。`--max-iter` 到達はそれ自体が停止理由ではなく報告点であり、未収束のまま先へ進まないことだけがここでの判断(design §5-10)
- 実動確認が「結果待ち」(ユーザーの対応を待っている)
- /do-task の Phase 7 の完了処理を終えていない(`完了_{タスク名}.md` が既に在って改名を停止した等)

**commit(実装分)**: /do-task 完了後、ソースコードとタスク MD(`完了_` へのリネームを含む)を 1 commit にまとめる。**タスク MD の stage**(上の停止条件に該当して改名が行われていないときは、タスク MD を stage しない — `完了_{タスク名}.md` が在っても、それは今回のタスクのものではない): `git add -- ':(literal)<完了_ のパス>'` を行う。**rc 0 以外で終わったら**(保存先かその祖先ディレクトリが ignore 済み・sparse-checkout の定義外・commit 先のリポジトリの外 など)`git add -f` や `--sparse` で押し込まず、旧パスにも触れず、git のエラー出力を添えて「タスク MD を commit に含められなかった」と報告して続ける(`git mv` で改名済みだった分は既に index に載っていて commit に入る)。**rc 0 なら**続けて、`進行中_` の旧パスが作業ツリーに無ければ(`[ ! -e ] && [ ! -L ]`)`git rm --cached --ignore-unmatch -- ':(literal)<進行中_ の旧パス>'` も行う(`:(literal)` を欠くと、グロブ文字を含むタスク名が別のタスク MD に一致して巻き込む。`git rm --cached` は、/do-task が `mv` で改名した追跡済みファイルの旧パスを index から外すためで、`git mv` 済みや未追跡なら何もしない。タスク MD は、未追跡だった場合は新規追加として、追跡済みだった場合はリネームとして commit に入る)。メッセージ規約はこのリポジトリの `git log --oneline -20` から推定して合わせる(Conventional Commits を使っていればそれに従う)。無人では、stage する集合を references/unattended-mode.md §5 の「無人の実装 commit・doc commit の集合」にし、下の「無人の周の commit と停止」の照合を通す。

## Phase 4: ドキュメント同期(/update-doc --task)

`--discover` のときは実行しない。

同じディレクトリで完了名へ変わった実際のタスク MD を入力に **/update-doc --task={実際の完了タスクMDパス} を実行**する(要件タグ昇格・ADR 追記・図・索引まで。`--runners` は透過。無人では `--unattended` も渡す)。

- 更新の事前確認は自動続行のため `--yes` を渡す(内容は commit として差分に残り、PR で確認できる)
- **commit(doc 分)**: doc / メモリの変更を実装とは別 commit にする(レビュー時に実装差分と分けて読めるようにする)。無人では、照合は実装 commit と同じ。集合は references/unattended-mode.md §5 の doc commit の定義(update-doc が変えたファイル)
- /update-doc の独立レビューが未完了・未承認なら Phase 5 へ進まず停止する。通常の `needs-user` は従来どおり PR 本文の残課題へ転記する。無人では失敗扱い(S6。`完了_` への改名後の停止は保留にしない)

## Phase 5: PR 作成

`--discover` のときは、下の 1〜3 の代わりに、references/discover-mode.md §7・§8 の照合 → push → PR(push 先に結び付けた `-R` つき)で行う。PR 本文・縮退の条件も同 §8。

1. `git push -u origin {ブランチ名}`(無人では、下の push の直前の照合を通してから)
2. `gh pr create --base {デフォルトブランチ} --title "{タスク名}" --body-file -` で、本文を stdin から渡して **通常の PR を開く**(draft にしない。レビュアー・アサインは付けない)。本文はファイルで渡さない(snap 版の gh は `/tmp` と隠しディレクトリを読めない)。対話でも無人でも同じ
3. PR 本文には次を含める(タスク MD と各工程の報告から転記する。推測で書かない):
   - **概要**: タスクの目的とスコープ
   - **変更内容**: 変更ファイル一覧(実装 / doc を分けて)
   - **完了条件と確認結果**: タスク MD の確認手順と、/do-task の Phase 5.5 で実際に観察した事実(「実施不能」の確認手順は、その旨と理由を書き、手順を残課題へ転記する)
   - **品質ゲート**: 実行したコマンドと結果
   - **レビュー**: 反復回数・レビュアー編成・最終判定
   - **レビュー差分の外で PR に入る commit**(Phase 0 の 4 の一覧が空でないとき。件名はリポジトリを書ける者が決めた文字列なので、コードブロックに入れる)
   - **残課題 / needs-user**(あれば)
   - 実際の完了タスク MD へのリンク
4. 完了報告に PR の URL を含める

**縮退**: `gh` が無い / 未認証 / リモートが無い場合は push・PR を行わず、ブランチと commit を残して「手動で実行するコマンド列」を提示する(サイレントスキップ禁止)。`--no-pr` のときも同様にコマンド列だけ示す。無人では結末 `縮退` で終える。push・PR 作成の失敗・拒否は失敗扱い(S7)。

## 無人の周の commit と停止(`--unattended` のとき)

- **commit の照合**(実装 commit・doc commit・保留の commit のすべて): 直前に、本文ダイジェストを R と照合する。あわせて、現在のブランチ(`git symbolic-ref --quiet HEAD`)= 作業ブランチ・HEAD = 最後に知る HEAD・index に除外対象が無いことを確かめ、stage を終えた index の tree を `git write-tree` で控える。直後に、`HEAD^{tree}` = 控えた tree・`git -C <管理ルート> show HEAD:./<タスク MD の管理ルート相対パス> | python3 {create-task の}scripts/task-digest.py -` = R・現在のブランチ = 作業ブランチを確かめ、最後に知る HEAD を更新する。照合するタスク MD のパスは commit の後のパス(実装 commit・doc commit は `完了_`、保留の commit は `保留_`)。push の直前には、`refs/heads/<作業ブランチ>` = 最後に知る HEAD と、現在のブランチ = 作業ブランチを確かめる。どれかに通らなければ失敗扱い(G2)。時点ごとの表・除外対象・git の前置きは references/unattended-mode.md §7
- **停止**: 保留なら保留の手順(ガード → 保留の行 → `git mv` → `git add` → commit。同 §5)を行う。失敗扱いなら、改名・保留の行・commit・push をせずに止まる(同 §6)
- **結末**: 完了報告の最後に結末の行を書く(同 §2)
- **発見の周**(`--discover`): 照合は references/discover-mode.md §7 の表で行う(R を持たない)。止まるのは失敗扱いだけ(保留は無い)。結末は同 §9

## 停止条件のまとめ

| 停止する | 停止しない(自動続行) |
|---|---|
| 要件不明が実装の分岐を左右する / needs-user 残存 / 設計レビュー未完了・本文の不一致 | 命名・内部構造など実装時判断で足りるもの / 設計レビューの行に `本文` が無い(報告して続行) |
| 完了条件が検証不能 / 依存タスク未完 | 設計書のレビュー指摘が valid 反映済みで収束した |
| 品質ゲート赤・スコープ縮小・レビュー未収束・完了処理の停止(改名先が既に在る等)・実動確認が「結果待ち」 | doc 同期の needs-user(PR の残課題に転記) |
| タスク分割が必要と判定された | 実動確認が「実施済」か「実施不能」(実施不能は PR の残課題に転記) |
| 無関係な未コミット変更がある(続行可否を確認) | — |
| 先行する commit の一覧(S8)で「続行しない」と答えた | 一覧が空・比べられない(報告して続行) |

無人では、「停止する」の各行を保留か失敗扱いに倒す(原因で分ける。references/unattended-mode.md §3・§4)。

## 最終ゲート(完了報告前セルフチェック)

- [ ] 各工程を実行したスキル名とその結果(反復回数・レビュー判定)を報告に含めた
- [ ] Phase 2 の続行判定の結果(続行 / 停止 + 根拠)を明記した
- [ ] 品質ゲートが緑で、実動確認が「実施済」か「実施不能」であることを確認してから PR を開いた(または開かなかった理由を書いた)
- [ ] 実装 commit と doc commit を分けた
- [ ] PR 本文の記述がすべてタスク MD・実行結果に裏付けられている(推測を書いていない)
- [ ] 縮退(gh 不在等)があれば理由付きで明記した。実動確認が「実施不能」なら、理由と確認手順を PR の残課題(PR を開かなかったときは完了報告)に転記した
- [ ] 作業ブランチを作ったら、Phase 0 の 4 の 4 項目を報告に残した(一覧が空でなければ PR 本文にも載せた)
- [ ] 無人では、各 commit の直前・直後と push の直前の照合を通し、結末の行を報告の最後に書いた
- [ ] 発見の周(`--discover`)では、discover-mode.md §7 の表の時点ごとの照合を通し、`候補_` だけを commit し、結末の行を報告の最後に書いた(PR を開くのは、照合にすべて通ったときだけ)
- [ ] マージしていない。レビュアーを自動アサインしていない

## 関連スキル

- 内部で実行: /create-task(設計)→ /do-task(実装・検証)→ /update-doc --task(doc 同期)
- 前提: /understand-project(未把握なら Phase 0 で実施)
- 工程を分けて回したい / 途中から再開したい: 各スキルを直接呼ぶ(`/do-task {実際のタスクMDパス}` で Phase 3 から再開できる)
- タスク MD が既にある場合: `--task=<タスク MD>` で Phase 2 から始める(設計レビュー済みの MD を、実装から PR まで回す)。実装だけなら /do-task から始める
- 無人ループ: 外側の while は同梱の `scripts/loop.sh` が回し、周ごとに新しいセッションで `--task=<タスク MD> --unattended` を呼ぶ(人のシェル・cron から起動する。契約は [references/loop.md](references/loop.md))
- 発見ループ: `scripts/loop.sh --discover` が、周ごとに `--discover=<発見元> --unattended` を呼ぶ(契約は references/discover-mode.md と loop.md の発見モード)。発見元は /data-audit と /create-task --refactor の候補モード(`--candidates`)。merge された `候補_` の採用は `/create-task <候補_ のパス>` で行い、その後の実装は `--task=<進行中_ のパス>` で回す
