# Changelog

v4.2.0 以前は commit 履歴を参照。

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
