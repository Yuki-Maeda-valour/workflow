---
name: update-doc
description: プロジェクトのドキュメント類(Serena メモリ / 権威参照ファイル AGENTS.md / doc 配下)を実コードと同期させる。「ドキュメント更新して」「メモリを最新化して」「docs を同期して」と言われたとき、タスク完了後の締めとして、または /understand-project がドリフトを検出したときに使う。タスク完了直後は完了タスク MD を入力に変更範囲だけを軽量同期し(--task、省略時は、このセッションで完了したタスクか、完了タスクの候補から選ぶ)、要件タグの昇格([決]→[実])・ADR 追記・図・索引まで doc 統一構成を一貫して更新する。実コード裏取りと能力帯の異なる複数モデルの並列レビュー付き。--analyze-only は全量監査の差分報告のみ。update-docs という旧名の依頼もこのスキルで扱う。
argument-hint: "[--task=<完了タスクMD> | --analyze-only | --memory-only | --specific=<name>] [--runners=<名前,...>] [--yes] [--unattended]"
---

# update-doc — ドキュメント・メモリの実コード同期

## 無人モード(`--unattended`)

`--unattended` の入口では、原則・モード判定・helper 参照・対象文書の読取より先に、親の保持値を検査する。親の保持値が無い単独入口だけが、現在の配布元から新しい控えと検査用コピーを作る。

- 親が `state`・`sha256`・`guard`・`guard_sha256` とコピーの plugin ルートを渡したときは、5 値を継承する。1値でも渡されていれば親ありとして扱う。1 つでも無い、hash が違う、または `verify` が失敗したときは停止する。新しい `bootstrap` で成功へ置き換えない。
  - `loop.sh` の子は、親の `DEV_WORKFLOW_ENV_STATE`・`DEV_WORKFLOW_ENV_SHA256`・`DEV_WORKFLOW_ENV_GUARD`・`DEV_WORKFLOW_ENV_GUARD_SHA256`・`DEV_WORKFLOW_LOOP_PLUGIN_ROOT` を、この親の保持値として使う。
- 親の保持値が無い単独の無人入口は、現在の配布元の `../ship-task/scripts/environment-guard.py` を `python3 -B` で一度だけ使う。`bootstrap --root <pluginルート> --output <外部の新規ディレクトリ> --inventory <有効plugin一覧JSON>` を渡す。
  - 使用ホストの設定ファイル・設定ディレクトリ・skill/command の保存先を動的に解決し、`--setting`・`--settings-dir`・`--skills-dir` へすべて渡す。追加の Git/shell 設定は `--config`・`--shell` へ渡す。
  - 一覧は正式な手段で確定した `installPath` 付き配列とする。取得不能なら失敗扱いにする。出力の `state`・`sha256`・`guard`・`guard_sha256`・`plugin` とコピーの plugin ルートを保持する。
- **どちらの入口も、文書を読む前に照合する**。下の固定本文で guard の保持hashと環境を照合し、exit 0 のときだけコピー内の文書を読む。
- 以後の helper 実行・文書読取はコピーだけを使う。使用直前にも同じ hash と `verify` の照合を行い、元の helper を import/source しない。
  - 詳細・有効 plugin 一覧の再照合・子への継承は、コピー内の [../ship-task/references/unattended-mode.md](../ship-task/references/unattended-mode.md) §0 に従う。

固定本文はこの入口を信頼して読み込んだ時点の字面を保持し、別ファイルから読み直さない。下の4つの値だけを親または今回の bootstrap の保持値へ置き換える。本文への追加・変更はしない。
Python の隔離起動(`-I`)で cwd・PYTHONPATH・利用者 site の同名モジュールを読まない。親ディレクトリと末尾をリンクを辿らず開き、通常ファイルを1 MiB・15秒以内で読み、保持hashと一致した同じバイト列だけを実行する。拒否時は終了コード20で停止する。

<!-- environment-loader:begin -->
```bash
python3 -I -B -c 'import os,sys,stat,re,hashlib,json,signal

def load_guard(expected, path):
    if not re.fullmatch("[a-f0-9]{64}", expected):
        raise RuntimeError("hash")
    parts = path.split("/")
    if not path.startswith("/") or len(parts) > 129 or any(p in ("", ".", "..") for p in parts[1:]):
        raise RuntimeError("path")
    def expired(*unused):
        raise RuntimeError("timeout")
    def identity(st):
        return st.st_dev, st.st_ino, st.st_mode
    def version(st):
        return identity(st), st.st_size, st.st_mtime_ns, st.st_ctime_ns
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 15)
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for name in parts[1:-1]:
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise RuntimeError("directory")
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            if identity(before) != identity(os.fstat(fd)):
                raise RuntimeError("directory changed")
        before = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1048576:
            raise RuntimeError("file")
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if version(before) != version(os.fstat(child)):
                raise RuntimeError("file changed")
            raw = b""
            while len(raw) < before.st_size:
                chunk = os.read(child, min(65536, before.st_size - len(raw)))
                if not chunk:
                    raise RuntimeError("short read")
                raw += chunk
            if os.read(child, 1) or version(before) != version(os.fstat(child)):
                raise RuntimeError("file changed")
            if version(before) != version(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)):
                raise RuntimeError("path changed")
            if hashlib.sha256(raw).hexdigest() != expected:
                raise RuntimeError("hash mismatch")
            return raw
        finally:
            os.close(child)
    finally:
        if fd is not None:
            os.close(fd)
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

args = []
try:
    expected, path, *args = sys.argv[1:]
    raw = load_guard(expected, path)
    sys.argv = [path, *args]
    exec(compile(raw, path, "exec"), {"__name__":"__main__", "__file__":path})
except (Exception, SystemExit) as exc:
    if isinstance(exc, SystemExit) and exc.code in (0, None):
        raise
    if args[:1] == ["hook"]:
        print(json.dumps({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"deny","message":"環境の保持値との照合に失敗"}}}))
        raise SystemExit(0)
    print("ERROR [environment-guard] 保持した検査用コピーを安全に実行できない", file=sys.stderr)
    raise SystemExit(20)
' '<guard_sha256>' '<guard>' verify --state '<state>' --expect-sha256 '<sha256>'
```
<!-- environment-loader:end -->

## 原則

1. **実コード裏取り(実際のコードやファイルを読んで確かめること)**。ドキュメントに書く内容は必ず実コード・実設定で確認する。前のドキュメントからの転記や推測で書かない
2. **カテゴリ別の正本(ほかが合わせる元)を守る**。
   - コード理解系(概要・構造・技術・規約・コマンド)の正本は profile の `source_of_truth` に従う。
   - **要件+タグ(doc/03)・設計判断 ADR と図(doc/04)・運用と地雷(doc/05)は、設定に関わらず常に doc/ が正本**。
   - `doc/06_stack-notes.md` は /stack-research、`doc/07_plan.md`(見積もり・スケジュール)は /reflect-decisions の管轄 — このスキルはどちらも**読むだけで書き換えない**(依存が変わっていたら /stack-research --update、計画の変更は /reflect-decisions を案内する)
3. **薄い権威参照ファイル(AI への指示をまとめたプロジェクトのファイル)**。
   - 正本は `AGENTS.md`(無ければ未移行の `CLAUDE.md`)。
   - 要点+参照の薄型を維持し(最も厳しいホストの上限 32 KiB 以内)、詳細は正本(メモリまたは doc/)に置く。
   - `CLAUDE.md` が `@AGENTS.md` を import する**互換形(AGENTS.md を読み込む行を持つ CLAUDE.md)**では、**`CLAUDE.md` に内容を複写せず `AGENTS.md` 側だけを更新する**(design §7-4)
4. **コードは触らない**。このスキルの変更対象はドキュメント類のみ。コード品質ゲート(書式・型・テスト・ビルドの自動の検査)は実行不要(ドキュメントだけの変更のため)。ただしリンク検査は行う
5. 機密ファイルの値をドキュメントに書かない(存在と用途だけ記す)
6. **委託(作業を別の AI に任せること)の解決は解決表(役割の名前を、実際に使う AI の仕組みに対応づける表)に従う**。役割語(AI の役割の名前。`researcher`〈調べものを受け持つ AI〉 / `implementer`〈実装を受け持つ AI〉 / `reviewer`〈変更を確かめる AI〉 / `checker`〈設計書を点検する AI〉)から、
   - ホスト機構への解決(派生名・属性軸・解決順・段階判定の手段)は [../do-task/references/delegation-map.md](../do-task/references/delegation-map.md) を参照する(本文に現れる API 名・モデルエイリアスはホスト = Claude Code での解決)
7. **人が読む文(報告・質問・PR と Issue の本文・作る文書・コミットメッセージ)を書く前に [../do-task/references/writing-for-people.md](../do-task/references/writing-for-people.md) を読み、それに従う**
   - わかりやすさの決まり・言い換え表・字面を変えない行と語・口調の決め方。このファイルに届かないときは、権威参照ファイルの「応答の書き方」節と、口調の決まりを書いた節に従い、届かないことを報告に書く

## 2 つの実行モード

| モード | いつ | 範囲 |
|---|---|---|
| **タスク後の差分同期(最頻・軽量)** | /do-task 完了直後 | 完了タスク MD から更新対象を導出し、関係する文書・メモリだけを同期 |
| **全量監査** | 定期・/understand-project のドリフト(文書や設定と実際との食い違い)検出後・久しぶりの実行 | 全ドキュメント vs 実態(`--analyze-only` で差分報告のみも可) |

モード判定: `--task=<パス>` があれば、管理プロジェクトルート相対として最優先し、profile の `task_dir` が不正でも保存先を再解決しない(差分同期)。

- `--analyze-only` はモードを変える指定(全量監査の差分報告だけ。入力を選ばない)で、`--task` の差分同期と両立しないので、**`--task` と `--analyze-only` を併用されたら停止し、どちらか一方を指定するよう案内する**
- `--task` も `--analyze-only` も無ければ、
  - [../create-task/references/task-directory.md](../create-task/references/task-directory.md) に従って保存先を解決する。
  - 型の検証に失敗したとき、または保存先の解決がエラー(無効指定・置換漏れの名前の候補・**保存先のディレクトリの候補が複数** など)になったときは停止する
- 解決できたら入力を選ぶ
  - このセッションで /do-task が完了にしたタスク MD が 1 つなら、それを使う(差分同期)。2 つ以上なら並べてユーザーに選ばせる
  - 無ければ、直下の `完了_*.md` を並べてユーザーに選ばせる(1 件でも確認する。全量監査も選べる)。**git log・更新時刻では自動で選ばない**(`完了_` は未 commit のことが多く、更新時刻は完了の順を保証しない)
  - `完了_` が 1 件も無ければ**全量監査**
- `--memory-only`・`--specific` は更新の対象を絞るだけで、入力の選び方は上のとおり
- 無人(`--unattended`)は `--task` が必須(オプション表の `--unattended` の行)

## オプション

| オプション | 内容 |
|---|---|
| `--task=<パス>` | 指定した完了タスク MD を入力に差分同期(省略時の選び方はモード判定) |
| `--analyze-only` | 全量監査の差分と監査結果の報告のみ(更新しない) |
| `--memory-only` | Serena メモリのみ更新(権威参照ファイル / doc を触らない) |
| `--specific=<name>` | 特定メモリ・特定ファイルのみ |
| `--no-review` | 互換入力。独立レビュー必須のため無効化して報告する |
| `--runners=<名前,...>` | 外部 CLI をレビュアーとして追加(オプトイン。既定は内蔵のみ)。→ [../do-task/references/external-runners.md](../do-task/references/external-runners.md) |
| `--yes` | 更新内容の事前確認をスキップ(差分提示 → 即適用) |
| `--max-review=<N>` | レビュー反復の上限(既定: 無制限+セーフティ) |
| `--unattended` | 無人モード(/ship-task の無人モードが渡す)。`--yes` を含む。`--task` が無ければ、候補の選択より前に失敗扱いにする。判断を仰ぐ点では止まり「未承認」(レビュー判定が `APPROVED` でない)で返す(U1・U2。正本は [../ship-task/references/unattended-mode.md](../ship-task/references/unattended-mode.md))。`loop.sh` の周(無人ループの 1 回分の実行)では、この skill の `$` を含むコマンドの例を字面どおりに打たず、同書の「`loop.sh` の周の Bash の書き方」で打つ |

## Phase 1: 入力と現状把握

1. 管理プロジェクトルートを固定してモードを判定する(上記)。**差分同期**では完了タスク MD を読み、更新の種(スコープ / 変更ファイル / 図解 / 技術的考慮事項の設計判断 / 追加修正記録の発見事項)を抽出する。`.claude/grasp.md` は参照索引としてのみ使い、今回同期する領域の現在のコード・設定・依存先の確認を省略しない
2. `.claude/project-profile.yml` から `source_of_truth`(既定 serena)・`memory_map`・`root` を解決する。
   - `source_of_truth` に値があり(**空 = null・空文字 以外は、型を問わず「値がある」**)、有効な 3 値(`serena` / `docs` / `agents-md`)のいずれでもないときは、ここで停止して有効な 3 値と直す場所(`.claude/project-profile.yml`)を案内する
   - (既定へフォールバック〈先の手段が使えないときに次の手段へ切り替えること〉しない。profile が無い・項目が無い・値が空の場合の既定 serena は従来どおり)
3. **Serena がある場合**: `get_current_config` でアクティブプロジェクト確認(違えば `activate_project`)→ `list_memories` で実在メモリを動的列挙(固定名・固定数を仮定しない)→ 読む(差分同期では関連カテゴリのみ、全量監査では全部)
4. doc/(または docs/)の索引(README.md)と関連文書を読む。**権威参照ファイルを読む**(`AGENTS.md` があればそれ → 無ければ `CLAUDE.md`。両方あれば `AGENTS.md` が正本。design §3 の検出順)

## Phase 2: 実態調査と更新対象マップ

変更種別ごとに「実コードで裏取りする対象」と「更新する文書・メモリ」を対応させる(差分同期ではタスク MD から該当行だけを選ぶ):

| 変更種別 | 調査対象(実コード) | 更新する文書・メモリ |
|---|---|---|
| 依存・スタック変更 | マニフェスト・lockfile | 02(技術スタック)/ tech 系メモリ。**06 は書かず /stack-research --update を案内** |
| DB スキーマ変更 | schema 定義・migrations | 04(データモデル+ **ER 図**)/ structure 系メモリ |
| API・ルーティング変更 | routes / handlers / server actions | 04(API 設計+ **シーケンス図**)/ 該当メモリ |
| 画面の追加・変更 | 画面・ルーティング実装 | 04(画面設計+ **画面遷移図**) |
| ステータス・状態機械 | 状態遷移の実装 | 04(**状態遷移図**) |
| 構造変更 | ディレクトリ実測 | 02(構造・データフロー図)/ structure メモリ |
| 規約変更 | フォーマッタ・tsconfig 等 | 規約の置き場(source_of_truth 従属)+ 権威参照ファイルの要点 |
| コマンド変更 | scripts | 05・権威参照ファイルの Quick Commands / commands メモリ |
| インフラ・デプロイ変更 | 設定・デプロイ手順 | 05(**デプロイ構成図**)/ 02(システム構成図) |
| **機能の実装完了** | 完了タスク MD のスコープ | **03 の該当要件のタグを `[決]`→`[実]` へ昇格**(部分実装は昇格せず備考に状況を注記) |
| **設計判断の発生** | タスクの技術的考慮事項・「該当なし・新規パターン」の判断 | **04 の ADR 表に追記**(日付 / 決定 / 理由 / 却下した代替案) |
| **地雷・運用知見の発見** | 追加修正記録・レビュー記録 | 05 の「引き継ぎ・地雷」(恒久的なもののみ) |

## Phase 3: 差分検出と監査

1. **差分検出**: ドキュメント記述 vs 実態を突合(2 つを照らし合わせて食い違いを探すこと)し、「追加すべき / 更新すべき / 削除すべき(陳腐化)」に分類。各項目に実コードの根拠(パス:行番号)を付ける
   - **doc 先行の保護**: 03 で `[決]` かつ備考に「実装未追従」がある行は、/reflect-decisions が会議決定を実装に先行して反映した正当な状態。実コードと食い違っていても乖離・陳腐化として扱わず、巻き戻し・削除・`[実]` への変更をしない(実装は /create-task → /do-task の完了後、差分同期で昇格する)
2. **監査(全量監査モードのみ)**: 次の 3 点も検出する
   - 未管理: 実態に存在するがどのドキュメントにも書かれていない重要事項
   - 内容乖離: ドキュメント間(メモリ vs 権威参照ファイル vs doc/)の矛盾
   - **AI への指示をまとめたプロジェクトのファイルが読まれない構成**: [../understand-project/references/authority-file-drift.md](../understand-project/references/authority-file-drift.md) の「(2) ドリフト判定」に従う。
     - **reference に到達できない構成では、この検査を無効化して報告する**(design §6。外部ランナー〈レビューや実装に使う外部の AI のコマンド〉を内蔵に切り替えたときの報告と同じ書式)
   - 参照切れ: ドキュメントが指すパス・ファイルの不存在
3. `--analyze-only` はここで報告して終了(更新推奨リスト+本実行の案内)

## Phase 4: 更新実行

原則 2 の**カテゴリ別正本**に従って更新する:

- **コード理解系**(概要・構造・技術・規約・コマンド)は `source_of_truth` の向きで:
  - **serena**(既定): メモリを更新(最新化 / 新規 add / 陳腐化 delete の提案)→ 権威参照ファイルは薄型のまま要点のみ追従 → doc 02/05 の重複箇所は要約レベルで追従
  - **docs**: doc/(02・05 等)を正本として更新 → メモリは探索用の要約として一方向同期 → 権威参照ファイルは参照を追従
  - **agents-md**: 権威参照ファイルを直接更新(小規模・メモリ / doc 未整備の構成。書き込み先は正本 = `AGENTS.md`)
  - いずれの向きでも、**書き込み先の権威参照ファイルは `AGENTS.md`**(未移行のプロジェクトのみ `CLAUDE.md`)。import 1 行の `CLAUDE.md` には書き足さない
- **常に doc/ が正本のカテゴリ**(source_of_truth に関わらず):
  - **要件タグの昇格**: 完了タスクのスコープと 03 を突合し、実装が完了した要件を `[実]` へ。スコープ外・部分実装・備考「実装未追従」の行(完了タスクで解消を確認できるまで)は昇格しない(備考に状況を注記)
  - **ADR 追記**: タスク中の設計判断を 04 の ADR 表へ 1 判断 1 行(大きな判断は `04_design/adr-YYYYMMDD-*.md` へ切り出し)
  - **図の同期**: Phase 2 マップの図列に従い、実態と乖離した図を更新(記法は doc/README.md の図規約)
  - **地雷の追記**: 恒久的な発見のみ 05 へ(一時的なものは書かない)
- **索引の更新**: 内容を変更した文書について、doc/README.md の状態絵文字・最終更新日を更新する

`--yes` でなければ、適用前に更新内容の一覧(対象 / 変更概要)を提示して確認を取る(`--unattended` は `--yes` を含む — U1)。適用は 1 ファイルずつ、根拠と共に。

## Phase 5: 独立レビューループ(常に実行)

1. **能力帯の異なる複数レビュアーを単一メッセージで並列起動**する。各エージェントに `reviewer-strong` / `reviewer-alt`(3 体目以降は `reviewer-alt2` / `reviewer-alt3` …)の `name` を付ける。更新後のドキュメント一式と「合格基準」を渡し、指摘リスト JSON で返させる
   - 合格基準: 実コードとの整合 / 網羅性(今回の変更範囲)/ 古い情報の不在 / ドキュメント間の無矛盾 / **タグ・ADR・図・索引の整合** / フォーマット規約
   - 無人(`--unattended`)では、[unattended-mode.md](../ship-task/references/unattended-mode.md) の「委託するサブエージェント」の項の要点を、要約し直さずにそのまま委託プロンプトに入れる(「拒否されたら打ち直さずに報告する」は要点の最後の行。レビュアー以外に委託するときも同じ)
   - **外部ランナー(宣言時のみ・オプトイン)**: `--runners=<名前,...>` または profile の `features.runners` が宣言されている場合に限り、外部 CLI レビュアーを追加する(宣言が無ければ内蔵編成のみで、外部 CLI を探しに行かない)。
     - 手順・判定・終了コード・機密ガードの契約は [../do-task/references/external-runners.md](../do-task/references/external-runners.md) が正本(ここでは再掲しない)。参照先が存在しない構成(skill を単体でコピーした部分導入)では外部ランナーを無効化して報告する
   - **委託の解決(役割語 → 実行バックエンド)**: 役割語の解決は [../do-task/references/delegation-map.md](../do-task/references/delegation-map.md) が正本。解決表に到達できない、または独立レビュアーを起動できない場合はレビュー未完了を報告し、更新完了として扱わない
2. team-lead(作業を進め、結果を確かめる側の AI)が各指摘を**実コードで裏取り**して valid / invalid / needs-user にトリアージ(指摘を扱いごとに振り分けること。盲信禁止、invalid は理由記録)
3. valid を修正 → 再レビュー。**フル段階(design §5-17)では、修正後の再レビューを同じレビュアー名へ再依頼する**(再スポーンしない — 前回のレビュー文脈が保たれ、差分だけを見て判定できる)。宛先が失われている場合のみ新規起動にフォールバックする
4. セーフティ(design §5-10): **収束条件は全 reviewer の APPROVED**。
   - 同一指摘 2 回連続残存 → ユーザー確認 / 5 ラウンド超え → トークンコスト警告 / `--max-review` 到達 → いずれも**停止ではなく判断を仰ぐ点**であり、状況を報告して判断を仰ぐ。
   - `--unattended` では判断を仰ぐ点で止まり、「未承認」(レビュー判定が `APPROVED` でない)で返す(U2。/ship-task は失敗扱いにする)
5. 記録: `.claude/reviews/update-doc-iter{N}.md`。
   - 書く直前に `bash {do-task の}scripts/reviews-dir.sh ensure --root <管理ルート>` を打ち、exit 0 以外なら記録を書かずに停止して報告する(do-task の scripts に届かない構成〈skill の単体コピー〉では、検査を省いたことを報告に書く)

## Phase 6: 最終チェック

1. **リンク・参照検査**: `python3 {このスキルの}scripts/check_links.py <doc ディレクトリ or 対象ファイル...>` を実行し、Markdown 相対リンク・記載パスの切れを検出(スクリプトが使えない環境では Grep で代替)
2. **把握キャッシュの無効化**: ドキュメント・メモリを更新した場合、`.claude/grasp.md` を削除する(前回要約と参照索引を今回の一次情報に合わせて作り直すため。次回の /understand-project が再把握して作り直す)
3. 更新サマリーを報告: 更新したドキュメント一覧 / 主な変更点(タグ昇格・ADR 追記を含む)/ 削除(陳腐化)したもの / レビュー反復回数 / 残った needs-user(人の判断が要る指摘)の項目。
   - **報告の最後に、必ず 1 行 `レビュー判定: <APPROVED | 未収束 | 未完了>` を書く**(ship-task が読む)。
   - `APPROVED` = 全レビュアーの valid 指摘 0 / `未収束` = 反復しても指摘が残った・判断を仰ぐ点で止まった(打ち切りを選んだ場合を含む)/ `未完了` = レビュアーを起動できない・途中で止まった。
   - **止まるときも、報告の最後に必ずこの 1 行を書く**(Phase 5 の 1 のレビュー未完了・判断を仰ぐ点・無人の停止を含む)。
   - 語は設計レビューの行([../create-task/references/task-template.md](../create-task/references/task-template.md) の記法の規約)と同じ。
   - update-doc には checker が無いので、`APPROVED` はレビュアーだけで決まる

## 最終ゲート(完了報告前セルフチェック)

- [ ] すべての更新内容に実コードの根拠がある
- [ ] カテゴリ別正本(コード理解系 = source_of_truth / 03・04・05 = doc 固定)に逆らう更新をしていない
- [ ] 実装完了した要件のタグを昇格した(該当タスクがある場合。部分実装を昇格していない)
- [ ] 更新した文書の索引(doc/README.md)の状態・最終更新日を更新した
- [ ] `doc/06_stack-notes.md`・`doc/07_plan.md` を書き換えていない(依存変更は /stack-research、計画変更は /reflect-decisions を案内した)
- [ ] 備考「実装未追従」の `[決]` 行を巻き戻し・削除・昇格していない
- [ ] 権威参照ファイルを肥大化させていない(詳細は正本へ。32 KiB 以内)。import 1 行の `CLAUDE.md` に内容を書き足していない
- [ ] 全量監査では AI への指示をまとめたプロジェクトのファイルが読まれない構成を検出したら注記したか(reference に到達できない場合は無効化を報告したか)
- [ ] `source_of_truth` が有効な 3 値以外のまま、既定へフォールバックして続行していない
- [ ] 機密値を書いていない。ソースコードを変更していない。リンク検査を実行した

## 関連スキル

- 事前把握: /understand-project(--deep でドリフト検出)
- タスク完了の流れ: /do-task → /update-doc --task(差分同期)
- 設計から PR まで通しで回す: /ship-task(このスキルを最終工程として内部で実行し、doc 更新を別 commit にして PR に載せる)
- 依存バージョン起因の更新: /stack-research --update(06 はこちら)
- 会議決定の反映: /reflect-decisions(人の決定 → doc。07 と「実装未追従」の `[決]` 行はこちらが書く)
- クライアント提出用の出力: /export-doc(doc を PDF / xlsx へ変換)
- ドキュメント構成自体が無い: /init-project
