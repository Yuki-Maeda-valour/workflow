---
name: do-task
description: 解決した保存先のタスク設計書(進行中_*.md)を実装し、検証・レビュー・完了処理まで行う実行スキル。「タスクをやって」「実装して」「タスクを進めて」「続きをやって」と言われたとき、/create-task で作った設計書を実装に移すときに使う。規模判定に基づき researcher / implementer / reviewer のサブエージェントを編成し、team-lead(このセッション)が diff とチェックリストの機械突合でスコープ縮小を検出、品質ゲートを自ら再実行して完了を判定する。完了時はタスクファイルを 完了_ にリネームし追加修正記録を追記する。「検証だけして」と依頼された場合は検証フェーズのみを実行し、修正せず合否を報告する。
argument-hint: "[タスクMDパス(省略時: 解決した保存先の進行中_*.md から選択)] [--max-iter=<N>] [--reviewers=1|3] [--runners=<名前,...>] [--branch[=<名前>]] [--unattended]"
---

# do-task — タスクの実装・検証・完了処理

## 原則

1. **検証の分離**。
   - スコープ縮小・偽完了を検出する側(team-lead〈作業を進め、結果を確かめる側の AI〉 = このセッション)は実装を受け持つ AI(implementer)と分離する。
   - implementer の自己申告を最終確認にしない。
   - **規模に関わらず実装は implementer に委託(作業を別の AI に任せること)し、team-lead は常に検証者に回る**(team-lead による直接実装は、サブエージェント機構が使えず、段階を最小に下げたときだけの代替)
2. **完了条件は team-lead が再実行**。品質ゲート(書式・型・テスト・ビルドの自動の検査)コマンドは implementer の報告に関わらず team-lead 自身がもう一度実行して緑を確認する
3. **タスク MD が契約**。実装範囲の拡大・縮小は勝手に行わない。設計と実態がずれたらタスク MD を更新するか、ユーザーに確認する(対話では、内蔵 implementer の走行中は直さず、照らした後に直す — Phase 3)。無人ではタスク MD を更新せず保留(D1)
4. **反復は仕組みで安全に**。レビュー・修正の反復は APPROVED まで続けるのが基本動作。`--max-iter`(既定 5)は安全弁で、到達したら状況を報告して指示を仰ぐ(無人では保留 — D2)
5. **委託は役割語(AI の役割の名前)で行う**(`researcher`〈調べものを受け持つ AI〉 / `implementer` / `reviewer`〈変更を確かめる AI〉 / `checker`〈設計書を点検する AI〉)。
   - **起動時に `name` を必ず付ける**(完了後の再依頼・状態確認の宛先になる)。
   - ホスト自身と同じ CLI を Bash から起動しない(design §5-5)。
   - **調査・レビュー・検証は読み取り専用の委託、実装は編集を伴う委託**にする。
   - 無人(`--unattended`)では、どの役割(researcher・implementer・reviewer・checker)の委託でも、[unattended-mode.md](../ship-task/references/unattended-mode.md) の「委託するサブエージェント」の項の要点を、要約し直さずにそのまま委託プロンプトに入れる
   - (「拒否されたら打ち直さずに報告する」は要点の最後の行)
6. **実行段階の判定**(design §5-17): 着手時にツールの実在で段階を決める。独立レビュアーまたは解決表(役割の名前を、実際に使う AI の仕組みに対応づける表)を使えない場合はレビュー未完了を報告し、完了承認・後続公開へ進めない。セルフ実行と機械検証は独立レビューの代替にしない。無人では失敗扱い(D18)
7. **委託の解決は解決表に従う**。役割語(`researcher` / `implementer` / `reviewer` / `checker`)からホスト機構への解決(派生名・属性軸・解決順・段階判定の手段)は [references/delegation-map.md](references/delegation-map.md) が正本(ほかが合わせる元)
8. **人が読む文(報告・質問・PR と Issue の本文・作る文書・コミットメッセージ)を書く前に [references/writing-for-people.md](references/writing-for-people.md) を読み、それに従う**
   - わかりやすさの決まり・言い換え表・字面を変えない行と語・口調の決め方。このファイルに届かないときは、権威参照ファイル(AI への指示をまとめたプロジェクトのファイル)の「応答の書き方」節と、口調の決まりを書いた節に従い、届かないことを報告に書く

## 検証のみモード

「検証だけして」「本当に終わってるか確認して」のように**検証のみを依頼された場合**は、Phase 0(対象確定)→ Phase 4〜5.5〜6 を実行し、**修正はせず**合否・根拠・残課題を報告して終了する(修正に進むかはユーザーの判断に委ねる)。
- 対象は `進行中_` でも `完了_` でもよい(完了済みタスクの事後確認に使える)。
- `--unattended` とは併用しない(失敗扱い — D3)。

## 無人モード(`--unattended`)

/ship-task の無人モードが渡す(単独で呼んでもよい)。
- **文書を読む前の入口**: 親の保持値が1つでも渡された入口は、state・sha256・guard・guard_sha256・plugin の5値を継承する。欠けていれば停止し、新しい控えに置き換えない。
  下の固定本文で guard の保持hashと環境を照合し、exit 0 のときだけコピー内の文書を読む。
- 親の保持値が全て無い単独入口だけは、現在の配布元の `../ship-task/scripts/environment-guard.py` を `python3 -B` で一度使う。
  引数は `bootstrap --root <pluginルート> --output <外部の新規ディレクトリ> --inventory <有効plugin一覧JSON>`。
  使用ホストの設定ファイル・設定ディレクトリ・skill/command の保存先も動的に解決し、`--setting`・`--settings-dir`・`--skills-dir` へ渡す。
  一覧は正式な手段で確定した `installPath` 付き配列。取得不能なら失敗扱い。返る `state`・`sha256`・`guard`・`guard_sha256`・`plugin` を保持し、上と同じ照合を行う。
- 以後の helper 呼出・文書読取はコピーだけを使い、毎回直前に照合する。詳細はコピー内の無人契約 §0 に従う。
- 人に確かめる場面ごとの扱い(D1〜D19)・周の中で守る値と照合の表・限界の正本は [../ship-task/references/unattended-mode.md](../ship-task/references/unattended-mode.md)。
- 以下の各所には 1 行の分岐だけを置く。
- `loop.sh` の周(無人ループの 1 回分の実行)では、この skill と references(base-commit.md・diff-snapshot-call.md など)の `$` を含むコマンドの例を字面どおりに打たず、unattended-mode.md の「`loop.sh` の周の Bash の書き方」で打つ。

- 下の前提に含む Git 調査より先に、環境の照合後、Phase 0 の「開始前の確認」を通す。
- **前提**(候補の選択より前に検査する。満たさなければ失敗扱い): タスク MD のパスがある(D4)/
  - parent-child 構成でない(照合〈照らし合わせて確かめること〉をどのリポジトリで行うかが決まらないため)/
  - 検証のみモードでない(D3)/
  - `{ship-task の}scripts/git-config-digest.py` があり、`python3 {ship-task の}scripts/git-config-digest.py --dir=<管理ルート>` が exit 0(ローカルの git 設定のダイジェストを算出できる)
- **止まるとき**は、理由を「保留」(このタスクに固有の、人の判断が要る)と「失敗扱い」(環境・ツールの異常・照合に通らない・`完了_` への改名後)に分けて返す。改名・commit はしない(ship-task が行う)
- **本文だけを契約とする**: ヘッダ・追加修正記録の注記で完了条件を変えない。原則 3 のタスク MD の更新もしない(D1)
- **控える値**(セッション文脈に保持し、報告にも書く): R = 本文ダイジェスト(本文から計算した短い値。本文が変わると値も変わる。Phase 0 で `python3 {create-task の}scripts/task-digest.py <タスク MD>` で算出。exit 0 以外は失敗扱い)/
  - 作業ブランチ(Phase 0 の手順 3 の後の `git symbolic-ref --quiet HEAD`。detached HEAD なら「無し」)/
  - git 設定のダイジェスト(Phase 0 の手順 3 の後〈`--branch` でブランチを作った後〉に `python3 {ship-task の}scripts/git-config-digest.py --dir=<管理ルート>` で算出。exit 0 以外は失敗扱い。ship-task から呼ばれたときも自分で算出した値を持つ)/
  - 開始時の HEAD /
  - 基準の sha と未追跡一覧の状態(Phase 0 の手順 5 で基準が決まったとき。再開で基準行を再利用したときも同じ)。
  - 失ったら失敗扱い(G4)。
  - ship-task を経ずに単独で呼ばれたときは、R を最後の設計レビューの行の `本文` とも照合し、不一致・行が無い・`本文` が無い・`算出不能` なら保留(承認後の本文の変更は人の判断。ship-task の S4 と同じ。単独なので、改名・commit は誰もしない)
- **照合**: Phase 4 の各回と Phase 7 の手順 1 の前に、本文ダイジェストを R と照合し、git 設定のダイジェストを `python3 {ship-task の}scripts/git-config-digest.py --dir=<管理ルート> --expect=<守る値>` で照らす(exit 0 だけが通る。1 = 不一致・2 = 算出できない)。
  - 現在のブランチ・HEAD・基準行・未追跡一覧もあわせて見る(表は unattended-mode.md §7)。
  - 通らなければ失敗扱い(G2)
- **反復の数え方**: Phase 5・5.5 からの差し戻し(相手に返して、やり直してもらうこと)も ITER を増やし、`--max-iter` の判断を仰ぐ点に数える(判断を仰ぐ点に届かないまま、時間切れまで回らないように)

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

## Phase 0: 前提と対象確定

**開始前の確認**: ファイルシステムで管理ルートを固定し、権威参照ファイルと必要な契約を読む。
[references/external-runners.md](references/external-runners.md) §12-9 に従い、`python3 {do-task の}scripts/pending-implementation.py scan --cwd <管理ルート>` を実行する。
- 対象タスク本文を完了条件として採用する前、対象 repo の Git 調査・事前検査より前に行う。検証のみ・内蔵・非 Git・Issue 本文などの非ファイルタスクでも省略しない。
- 0/`none`、または全候補への人の具体的な確認後の 0/`acknowledged` だけで続行する。10/11/2/20・不正 JSON・起動不能なら停止する。無人は失敗扱いとし、候補・タスク本文を変更しない。
- 同じ ship-task から入るときも再走査する。その会話で確認済みの同じ cwd と一覧に限り、`PENDING_LIST_SHA256` を `--acknowledged-list` へ渡せる。ログから復元しない。
- この入口は Phase 0 だけ。Phase 3 の take 後・Phase 4・差戻し・内蔵への引継ぎでは再走査しない。同じ実行の反復で Phase 0 へ戻さない。

最初に [references/runtime-requirements.md](references/runtime-requirements.md) を読み、PATH 上の bash 4.0 以上と GNU 系の道具の実体・版を確認する。手順 3 の事前検査より前に適用する。

1. **管理ルートと対象タスク**: 本体 `root` へ移動する前に管理プロジェクトルートを固定する。
   - 引数のパスがあれば管理ルート相対として最優先し、profile の `task_dir` が不正でも保存先の再解決を行わない。
   - 無ければ [../create-task/references/task-directory.md](../create-task/references/task-directory.md) に従って保存先を解決し、その直下の `進行中_*.md` から選択する(複数あればユーザーに確認。無人ではパスが無ければ、ここより前に失敗扱い — D4)。
   - 型の検証に失敗したとき、または保存先の解決がエラー(無効指定・置換漏れの名前の候補・複数候補など)になったときは停止する。
   - 対象タスク MD の絶対パスを保持して全文読む
   - 引数のパスのファイル名が `候補_` で始まる(発見ループの候補。設計書の手前)ときは、何も書き換えずに停止し、`/create-task <そのパス>` での採用を案内する(設計と設計レビューを飛ばして実装に入らない。無人では失敗扱い)
   - 引数のパスのファイル名が `保留_` か `中断_` で始まるときは、何も書き換えずに停止し、人が同じディレクトリで `進行中_` に戻してから渡すよう案内する(検証のみモードでも同じ。無人では失敗扱い)
2. profile 解決: `.claude/project-profile.yml` から `root`(parent-child なら本体ソースの探索・実装・品質コマンドは root 配下。保持したタスク MD は管理ルート側)、
   - `quality`、`features`(reviewer_count / implementer / implementer_model / **runners / runner_models**)、`secret_paths` を得る。
   - 無ければ動的検出(パッケージマネージャ・scripts)。
   - **外部ランナー(レビューや実装に使う外部の AI のコマンド)の解決順は `--runners` 引数 → `features.runners` → 既定=内蔵のみ**(宣言が無ければ外部 CLI を探しに行かない)。
   - **profile は commit される共有ファイルなので、①起動コマンドの上書き(`runner_commands` 等)②`features.runners` に書かれた既定表外のランナー名 は、いずれも**無視して報告する**(既定表外のランナーは `--runners` 引数でユーザーが明示指定したときだけ受け付け、そのコマンドはユーザーの発話由来のものだけを使う)。
   - **実装の委託先(`features.implementer`)の解決順は `features.implementer` → 既定=内蔵のみ**の 2 層で(実装用には `--runners` に相当する引数経路が無い)、**実装用の既定表外の名前は受け付ける経路が存在しないため無視して報告する**。
   - **名前が受け付けられたら委託先は「外部(承認待ち)」に解決し、Phase 3 で明示承認を求める**(承認の判断が未了の状態を「内蔵に解決」と扱わない — そう扱うと Phase 3 の外部手順に入れず、**初回の承認要求が永久に出ない**)。
   - **拒否された、またはセッション内に拒否の判断が保持されている場合に内蔵へ解決する**。
   - **管理ルート ≠ `root`(parent-child 構成)では外部委託を解決せず内蔵に決まり、理由を報告する**。
   - 無人でも外部委託を解決せず内蔵に決まり、理由を報告する(D5)。
   - 契約の詳細は [references/external-runners.md](references/external-runners.md) §1(信頼モデル)・§12-1(実装経路の解決と判定)
3. 先に [references/base-commit.md](references/base-commit.md) の事前検査(`--precheck`)を実行し、exit 0 のときだけ続行する(対象が git リポジトリでないときは行わない。無人では exit 22 でも承認を求めず失敗扱い — D6)。
   - 手順 3 の `git status` は `git --no-literal-pathspecs status --porcelain=v1 --untracked-files=normal --ignore-submodules=dirty -- ':(top,exclude,glob)**/[.]claude/reviews/**' ':(top,exclude,glob)**/[.]claude/grasp.md' ':(top,exclude,glob)**/[.]claude/settings.local.json' ':(top,exclude,glob)**/[.]claude/.understand-project-done'` で、base-commit.md の前置きを `git` の直後に置き、base-commit.md の `TOP`(`rev-parse --show-toplevel`)で打つ。
   - サブモジュール内の未コミット変更(superproject の commit に入らない)と、状態ファイル(diff-snapshot.sh の既定除外と同じ 4 つ。どの深さでも。未追跡・stage 済み・追跡済みの変更のどれも)は出ない。見るのは、それ以外の変更と gitlink の変更(ルートからの相対パスで出る)。
   - `[.]` は、無人の周の許可の仲介(無人の実行で、操作を許すかをその場で判定する仕組み)が `.claude` をパスと読まないための書き方(glob の中で `.` に一致する)。
   - `git status` を確認。
   - 未コミット変更が既にある場合は、タスクと無関係な差分が混ざる旨を警告し、続行可否を確認する(無人では失敗扱い — D7)。
   - **ユーザーがブランチ作成を指定した場合のみ**(`--branch` または口頭指示)、着手前にブランチを作成して切り替える(名前省略時は `task/{タスク名}`)。指定が無ければ現在のブランチのまま進める(能動的な提案はしない)
4. `.claude/grasp.md` は参照索引としてのみ使い、毎回、現在の profile・権威参照ファイル・関連文書・設定・タスク対象と依存先を読む。前回の調査記録も再探索や説明の作り直しを減らす参考に限り、読取りを省略しない。短縮記録なら短縮判定と品質維持契約を読み、実装と検証の分離・独立レビューを省略しない
5. 先に、タスク MD のヘッダに基準コミット(作業を始めた時点のコミット)行が既にあるかを読んでおく(手順 7 の再開判定が使う)。
   - `.claude/reviews/` を `bash {do-task の}scripts/reviews-dir.sh ensure --root <管理ルート>` で作る(`.claude`・`.claude/reviews` が symlink か、在って通常のディレクトリでなければ、作らずに exit 0 以外で終わるので、停止して報告する。
   - 階層ごとに検査してから `-p` なしの `mkdir` — references/base-commit.md の ①。対話でも無人でも同じ。基準時点の未追跡一覧の保存も同じスクリプトの `save-untracked` で行う — 同書)。
   - `TASK_NAME`(ファイル名から)と ITER を決める。
   - ITER は [references/base-commit.md](references/base-commit.md) の条件 ③ の 1 つ目の項に当たる名前の、番号の最大値+1(無ければ 1)。番号は同書の比べ方で取り出す。定義と比べ方は同書。
   - 再開かどうかに関わらずこの値から始める(前の走行のログと名前が重ならないようにするため。`--max-iter` の判断を仰ぐ点もこの ITER の値で見るので、前の走行のログがあると早く来る)。
   - **実行段階を判定する**(design §5-17): 走行中の問い合わせ・生存確認ができるかを**ツールの実在で確認**し(判定手段は軸 8)、フル / 標準 / 最小 のどれで走るかを決めて以降の委託方式に反映する。
   - **基準の記録**: 基準行の書式は `> **基準コミット**: <sha>[ / 未追跡一覧: <sha256>]`(sha は常に完全形)。
   - 新規着手と確認できれば対象リポジトリの HEAD(無ければ空ツリー)と基準時点の未追跡一覧を記録し、そうでなければ『基準不明』として実装開始前の commit を代替基準に確認する。
   - 新規着手の 5 条件・自動候補の提示とユーザー指定による採用・到達不能な基準行の扱い・検証のみモード・ファイルでないタスク MD の運用は [references/base-commit.md](references/base-commit.md) が正本で、Phase 0 の手順 7(再開判定)より先に読む
   - (同じセッションの /ship-task が作った作業ブランチの例外も同書の条件 ⑤)。
   - 無人では『基準不明』を保留にする(git の失敗で基準不明になったときは失敗扱い — D8)。
   - 基準が決まったら、基準の sha と未追跡一覧の状態を控える値として保持する
6. commit が続く周では、実装前に [review-protocol.md](references/review-protocol.md) の `review-guard.py start` を取り、開始 hash を保持する。タスク MD のチェックボックス総数を記録: `grep -cE '^\s*- \[[ xX]\]' {タスクMD}`
7. **再開判定**(「続きをやって」対応): 次の**いずれか**に当たれば**再開モード**で入る —
   - (A) 手順 5 に入る前から、タスク MD のヘッダに基準コミット行があった(手順 5 は新規着手でも基準行を書くので、手順 5 の前に読んでおいた有無を使う)/
   - (B) タスク MD に `- [x]` がある /
   - (C) 追加修正記録に「記録あり」(設計レビューの行・保留の行を除いた行)がある(「記録あり」の定義と節の範囲は [../create-task/references/task-template.md](../create-task/references/task-template.md) の記法の規約)。
   - どれもタスク MD の中の印で、`.claude/reviews/` のログの有無は引き金にしない(別の worktree・別の PC にはログが無い — design §5-18 ①)。
   - 『基準不明』だけでは再開にしない(デフォルトブランチ上の新規着手や積み重ねブランチでも基準不明になる)。
   - 検証のみモードは再開モードに入らない。
   - 再開モードでは、
     - まず Phase 4 の機械検証(diff・チェックリスト突合〈2 つを照らし合わせて食い違いを探すこと〉)を先に実行して「実際にどこまで終わっているか」を復元し(チェック状態の自己申告を信じない)、残タスクの実装を続行する。
     - `.claude/reviews/` に同タスクの do-task のログ(手順 5 と同じ集合)があれば、最後の iter のレビュー状態をそこで確かめる。無ければ Phase 6 のレビューをやり直す前提で進める。
     - 復元結果(完了済み / 未完了 / チェック済みだが実装なし)を報告してから進む

## Phase 1: 規模判定と編成

| 規模 | 目安 | 編成 |
|---|---|---|
| 小・中 | 1〜5 ファイル | implementer + reviewer |
| 大 | 6 ファイル以上 / 契約変更 / migration | researcher + implementer + reviewer |

- **規模が小さくても team-lead は実装しない**(検証者と実装者の分離を常に保つ)。直接実装への簡略化は、サブエージェント機構が使えず、段階を最小に下げたとき(design §5-17)だけの代替
- reviewer 数: `--reviewers` 引数 → profile の `features.reviewer_count` → 既定 1。大規模・高リスク(契約変更・DB・決済系)では 3 を推奨する

## Phase 2: 事前コンテキスト(大規模、または /create-task の調査記録が無いとき)

researcher に、タスク MD の対象ファイル群の現状・既存パターン・注意点を調査させ、100〜200 語の要約+参照パスで返させる。`.claude/grasp.md` と /create-task 時の調査記録は参照索引として渡してよいが、現在の対象ファイル・依存先・規約の読取りを省略させない。

## Phase 3: implementer 委託

**implementer への委託プロンプトを準備する**(ここでは起動しない — **起動は下の「内蔵に解決されたとき」と「外部に解決されたとき」の手順 4 だけが行う**。無条件の起動命令をここに置くと、外部委託で必要な承認・スナップショット・退避を飛ばして起動しうる)。
- **モデルは、内蔵に解決されたときに限り** profile の `implementer_model` に従う(**外部に解決されたときのモデルは [references/external-runners.md](references/external-runners.md) §12-6 の実装用既定表が正本**)。
- 単一枠の能力帯は軸 3。
- **`name` は `implementer`**(**内蔵で実行するフル段階に限り**、以降の差し戻し・STATUS 問い合わせの宛先になる。**外部 implementer は宛先にならない** — 軸 5)。
- 委託プロンプトに必ず含めるもの:

- タスク MD の全文パスと「このタスク MD のチェックリストが完了の定義(DoD)である」こと
- タスク MD の「参考実装」節のパターンを踏襲すること(構成・命名・エラー処理・テストの書き方を既存に合わせる)
- タスク MD の「図解」節があれば実装の設計指針として扱うこと(実装が図と乖離しそうなら止めて報告する。黙って図と違う構造にしない)
- **スコープ縮小の禁止**: 「一部のみ実装」「段階的に実施」への言い換えを禁止。実装できない事情が出たら中断して報告する(勝手に縮めない)
- 完了したタスクはタスク MD のチェックボックスを `- [x]` に更新すること
- 品質ゲート(解決済みコマンド列)を自分でも実行し、結果を報告に含めること
- 機密ファイル(`secret_paths`)を読まない・報告に値を含めないこと(**外部 implementer にはこの指示に保証が無く、明示承認〈references/external-runners.md §12-4〉が唯一の防御である**)
- `.claude/grasp.md`(あれば)のパス — 前回要約と参照索引として使う。ただし現在の対象ファイル・依存先・規約を読むこと
- タスク MD はチェックボックスの更新だけにし、本文を変えない(無人では、変えると周が失敗扱いになる)
- 無人では次も含める: 「`commit` / `stash` / `branch` / `reset` / `push` / `config`(`git config` で設定を書く)をしない(すると周が失敗扱いになる)」。あわせて原則 5 の無人の項に従う

実装の委託先は Phase 0 の解決結果に従う。
- **内蔵に解決されたとき**(宣言なし / 既定表外の名前 / parent-child / 承認を拒否済み / 条件 A・B〈下の手順 1 で ITER ごとに判定する〉/ 無人)は、
  - **対話(`--unattended` なし)では、委託の直前(起動・差し戻しの再依頼・外部の手順から内蔵に切り替えるときや引き継ぎで起動するとき・M2 で引き継ぐとき)に、本文ダイジェストを `python3 {create-task の}scripts/task-digest.py <タスク MD>` で算出して控える**。
  - Git 対象の内蔵 implementer には、同じ直前に [references/implementation-git.md](references/implementation-git.md) の必要環境確認と `prepare` を通し、返った cwd・承認値・照合値・入口のパスを渡す。失敗時は起動・再依頼せず停止し、非 Git は既存経路を維持する。
  - (**対話の本文の照合**。**M2 で引き継ぐときは、控える前に前の控えと照らす** — 旧 implementer が書いた分を新しい控えに取り込まないため。照らし方は Phase 4 の冒頭)。
  - **走行中の間、team-lead はタスク MD の本文を直さない**(前提の誤りは走行中の前提是正〈解決表の軸 6〉で implementer に伝え、本文は報告を受けて照らした後に直す)。そのうえで**ここで内蔵 implementer を起動して Phase 4 へ進む**。
- **外部(承認待ちを含む)に解決されたときは起動せず、下の手順へ進む**。

長時間応答がない場合の死活監視は、**内蔵 implementer では**(M2 で引き継ぐのは、旧 implementer が**走行中でない**と確かめてから) [references/review-protocol.md](references/review-protocol.md) の M1〜M4 に従う
- (**外部 implementer には走行中の問い合わせ・生存確認の送達手段が無く M1〜M4 がそのままでは成立せず、待機目安の経過だけでは新規起動しない** — 停止後の引き継ぎ条件は references/external-runners.md §12-7、段階の下げ方の定義は design §5-17)。

### 実装の委託先が外部に解決されたとき

無人では外部に解決しないので、この節は通らない(D5・D9)。

**手順の詳細(スナップショットの 5 要素・マニフェスト・保護領域・退避・実体照合・復元・縮退条件・引き継ぎの発火条件)は [references/external-runners.md](references/external-runners.md) §12 が正本で、その手順は `{do-task の}scripts/implement-guard.sh` が実行する**。
- ここには順序と、終了コードによる分岐と、team-lead の義務だけを書く(同じ事実を 2 箇所に置かない)。
- **どのサブコマンドも、下に書いた終了コード以外(2 / 20 / 表に無いコード)が返ったら、終了コードと stderr をそのまま報告して停止する**(「変化なし」や成功に読み替えない。保護領域は消さない)。
- **滞在時間が長いため背景実行で起動する**(軸 2・§12-6)。

1. **承認より先に判定する縮退**(無駄な承認要求を出さないために承認より前に置く。Phase 3 に入るたび〈ITER ごと〉にここで判定し、Phase 0 の結果として読まない。定義と判定表は §12-1): 次の順に当て、当たれば外部委託を行わず内蔵 implementer で実装し、理由を報告する —
   - parent-child(管理ルート ≠ `root`)→ 条件 A: タスク MD がファイルでない(本文の写しを `--task-md` に渡さない)→ git でない(Phase 0 と同じ判定。事前検査を打たない)→ 条件 B: そのセッションで最後に承認した一覧に filter がある。
   - 条件 B は `bash {do-task の}scripts/diff-snapshot.sh --cwd <管理ルート> --precheck [--accept <最後に承認した値>]` を打ち直して判定する(stdout の受け方は base-commit.md の事前検査と同じ):
   - rc 0 で stdout に `GIT_CONFIG_COUNT` が出たら当たる / 出なければ次の手順へ /
   - rc 22 は Phase 0 と同じく人の承認を仰ぎ、承認されたらその値を最後に承認した値として打ち直す(承認されなければ停止)/ それ以外は終了コードと stderr を報告して停止する。
   - rc 0 のときの stderr の `NOTE: 無効化して実行:` か `NOTE: promisor 構成:` で始まる行(すべて)は、手順 3 の報告に出すために保持する
2. **セッション初回の明示承認**: 解決後の起動内容(`implement-agent.sh` の `--dry-run` の出力。`--dry-run` には `--log-file` を渡さない)を提示し、**外部に `secret_paths` を含む作業ツリー全体が見えること**(§12-4)と、**保護領域にローカルの平文複製が作られること**(§12-2)を明示して承認を得る。
   - **引数では省略できない**(このスキルに承認を省く引数を設けない)。
   - **承認・拒否ともセッション内で 1 回の判断として保持し、取り消しは保持中の判断を破棄して未判断に戻す**(次の委託時に再度承認を求める)。
   - **解決後の起動内容、または `secret_paths` にマッチするファイル集合が変わったら取り直す**。
   - 承認が得られなければ内蔵 implementer で実装し、理由を報告する。
   - **`--dry-run` を打つ直前に** `bash {do-task の}scripts/reviews-dir.sh ensure --root <管理ルート>` を打つ(exit 0 以外なら、外部を起動せず・内蔵 implementer に切り替えず・置き場に記録を書かずに、停止して報告する。手順 6 の比較不能から人の判断で外部をもう一度起動するときも同じ)。
   - `--dry-run` が exit 2 で、stderr の `ERROR [usage] ` の行に `既定のログ置き場` か `--log-file の置き場` を含むとき(置き場の経路が symlink か差し替えられた・置き場を作れない・書けない・名前に既にエントリが在る。ランナーは起動していない)も、同じく停止して報告する
   - (手順 6 の「外部 CLI を起動する前の失敗」に入れず、内蔵に切り替えない — §12-7)
3. **起動前のスナップショットと退避**: `bash {do-task の}scripts/implement-guard.sh take --cwd <管理ルート> --task-md <タスク MD>` を実行する。
   - **exit 0** なら、stdout の `STATE_DIR`・`MANIFEST_SHA256`・`SNAPSHOT_SHA256` を**セッション文脈に保持し**(以後のサブコマンドに毎回渡す)、**手順 4 の起動より前に、保護領域のパスとダイジェスト 2 つを報告に出す**
   - (外部が書き換えられないのは、セッション文脈と、起動より前に人に示した報告である — §12-2 の二重防御)。
   - 同じ報告に、値を埋めた 4 本のコマンド行(手順 5 の ① `config-check`・② `--precheck`・③ `compare`・④ `taskmd-diff`。解決済みのスクリプトの絶対パス・`--cwd`・`--state`・2 つのダイジェスト・最後に承認した値を埋める)と、
   - 手順 1 の `--precheck` の stderr の `NOTE: 無効化して実行:` か `NOTE: promisor 構成:` で始まる行(すべて。無ければ「無し」。手順 6 の比較不能からの再開で照合に使う — §12-5 の ⑥)を出す。
   - 別のセッションで再開するときの手順も 1 行で指す(この値で手順 5 の共通工程を先に通す — §12-2 の受け入れた限界 (a))。
   - `UNREADABLE` が 0 でなければ、stderr の `NOTE: unreadable` のパスが検出の死角になったことを報告する。
   - **exit 30 は縮退** — stderr の理由コード(`not-git` / `unmerged` / `no-protected-area` / `over-limit` / `taskmd`)を報告し、外部委託を行わず内蔵 implementer で実装する。
   - **clean は要求しない**(dirty のまま委託する)
4. **起動**: 上の共通項目に加えて、委託プロンプトで **`commit` / `stash` / `branch` / `reset` / `push` を禁止**して起動する(§12-3。機構に頼れないため、禁止と実行後の検証の 2 段で対処する)。
   - 本実行には `--log-file <管理ルート>/.claude/reviews/implementer-{ランナー}-{TASK_NAME}-iter{ITER}.md` を渡す(ITER は do-task の反復番号)。
   - 本実行の直前に `bash {do-task の}scripts/reviews-dir.sh ensure --root <管理ルート>` を打ち、exit 0 以外なら手順 2 と同じく停止して報告する(外部を起動しない・内蔵に切り替えない・置き場に記録を書かない)。
   - `implement-agent.sh` が exit 2 で、stderr の `ERROR [usage] ` の行に `既定のログ置き場` か `--log-file の置き場` を含むとき(置き場の経路が symlink か差し替えられた・置き場を作れない・書けない・名前に既にエントリが在る。ランナーは起動していない)も、同じく停止して報告し、内蔵に切り替えない(§12-7)。
   - **外部 implementer の起動は ITER ごとに 1 回**で、同じ ITER で既に外部を起動していれば(Phase 5・5.5 からの差し戻しや、手順 6 の比較不能から人の判断でもう一度起動するとき)、ITER を 1 つ進めてから起動する(同じ名前のログが在ると、`implement-agent.sh` は usage〈exit 2〉で止まる — §12-8)。
   - こうして進めた ITER も `--max-iter` の判断を仰ぐ点に数える
5. **共通工程(外部 CLI を起動した後は成功・失敗を問わず必ず通す。分岐より前に置く。モード確立・疎通の判定で落ちた経路も外部 CLI を起動済みなのでここを通る)**: 次の順に実行する(§12-2・§12-3)
   - ① `implement-guard.sh config-check --cwd <管理ルート> --state <STATE_DIR> --manifest-sha256 <値> --snapshot-sha256 <値>`。
     - 既知設定は Git 無しで照合し、その後に隔離 Git で子リポジトリの使用先を検査する。成功時の `GIT_SKIPPED=no` はこの隔離検査を表す。新規 submodule の管理 root は自動採用せず、33 で停止して再登録を必要とする(§12-2)。
     - **exit 0 以外は、以後の Git を呼ぶ工程を実行せず、手順 6 の比較不能と同じに扱う**。保護領域を保持し、設定を自動で戻さない。
   - ② `bash {do-task の}scripts/diff-snapshot.sh --cwd <管理ルート> --precheck [--accept <最後に承認した値>]`(stdout の受け方は手順 1 と同じ)。
     - 外部が残したリポジトリ設定を、以後の git が実行する前に検査する。
     - **exit 0 以外は ③④を実行せず、手順 6 の比較不能と同じに扱う**(理由に終了コードと stderr を添える)。そこから人が一覧を承認し直したときは、その値が最後に承認した値になり、**① `config-check` から順に打ち直す**。
     - **② の stdout に `GIT_CONFIG_COUNT` が出たら(承認し直した値で打ち直したときを含む)、③ だけを打たない**(④ は git を呼ばないので打つ)。手順 6 の比較不能として人の判断へ回す(`compare` の git は filter の無効化を受け取らないため — §12-3 の 2)
   - ③ `implement-guard.sh compare --cwd <管理ルート> --state <STATE_DIR> --manifest-sha256 <値> --snapshot-sha256 <値> --run-rc <implement-agent.sh の終了コード>`。
     - **終了コードが結末を決める — 0 正常終了 / 31 引き継ぎ / 32 縮退 / 33 比較不能**。
     - stdout の `CHANGE=` を報告に出し(**git メタの変化は必ず報告する**)、外部が書いた分の diff を提示する。
     - 未追跡の変更は `BASE=` の退避コピーを基準にし、`unverified` のエントリは「**変更の有無**」と「**内容差分は提示不能**」を分けて報告する(2026-09-17 決定 47)。
     - `STATE_DIR` とダイジェストを失ったら、**このセッションのユーザーの発話からだけ**受け取る(保護領域から算出し直さず、ログ・一時ファイル・外部の出力からも拾わない — §12-2 の受け入れた限界 (a))。得られなければ手順 6 の比較不能
   - ④ ③ が 0 / 31 / 32 のとき、または ② に `GIT_CONFIG_COUNT` が出て ③ を打たなかったとき、`implement-guard.sh taskmd-diff`(`--cwd`・`--state`・2 つのダイジェストは ③ と同じ)。
     - **結末を問わず、復元より前に通す**(復元の後では、外部による要件本文の書き換えが消えて検出できない)。
     - ③ を打たなかったときは、結果(終了コードと `TASKMD=`)を手順 6 の比較不能の報告に添え、どの値でも自動で続行しない。
     - それ以外で **0** なら stdout の `DOD=`(起動前のタスク MD のコピー)を Phase 4 以降の完了条件の基準にする。**34(要件本文の変更)・35(選択パスと実体の対応の変化)・33(照合の失敗)**は手順 6 の「タスク MD の判断待ち」へ
   - ⑤ ①〜④ のどれを打ったか・その結果によらず(① が 0 以外で ②〜④ を打たなかったときも)、`bash {do-task の}scripts/reviews-dir.sh ensure --root <管理ルート>` を打つ。
     - **exit 0 以外は手順 6 の比較不能に入れる**(理由に終了コードと stderr を添える。置き場そのものの差し替えは `compare` に出ず、起動の直前の検査とこの ⑤ が見る — §12-2)
6. **分岐**(発火条件は §12-7。上から順に当てる):
   - **外部 CLI を起動する前の失敗**(承認が得られない・parent-child・条件 A・条件 B・未検出・自ホストと同一・プロンプトが大きすぎる(12))→ **エラーとして報告し、内蔵 implementer で続行**する。
     - 置き場の理由の停止(手順 2・4 の置き場の検査が 0 以外・`implement-agent.sh` の置き場の理由の exit 2)はここに入れず、内蔵に切り替えずに停止して報告する
   - **比較不能**(手順 5 の ①か②が 0 以外、② に `GIT_CONFIG_COUNT` が出て ③ を打たなかった、③ が 33、または ⑤ が 0 以外。③ を打たなかったときは ④ の結果を報告に添え、④ がどの値でも自動で続行しない。⑤ が 0 以外のときは ①〜④ の結果を添え、どの値でも自動で続行しない)→
     - **終了コードを問わず、自動復元も、内蔵への自動の切り替えも行わず**、保護領域とログのパスを提示して**人の判断を仰ぐ**。
     - **「変化なし」と読み替えて内蔵に切り替えない**(外部が書いた分を見落としたまま内蔵に最初からやり直させる最悪の経路になる)
   - **タスク MD の判断待ち**(手順 5 の ④ が 34・35・33)→ **③ の結末を問わず、復元も引き継ぎも Phase 4 への続行も行わず**、何が変わったか(stdout の `TASKMD=`)と保護領域のパスを提示して**人の判断を仰ぐ**(要件本文の変更は、ユーザーの承認なしに起きてはならない)
   - **走行中の失敗**(③ が 31 = 終了コードが 0 以外、かつ作業ツリーまたは git メタのいずれかに変化がある)→ **巻き戻さず内蔵 implementer が引き継ぐ**。引き継ぎの手順は下記
   - **終了コードが 0 以外だが、どちらにも変化が無い**(③ が 32)→ 起動前失敗と同じく内蔵に切り替える(内蔵で続行)
   - **正常終了**(③ が 0)→ 外部が書いた分の diff を提示して Phase 4 へ
7. **引き継ぎの手順**: ①**外部ランナーのログファイルのパスを内蔵 implementer へ必ず渡す**(何をどこまでやって、なぜ止まったかは diff より生出力に出る)
   - ②`implement-guard.sh restore-taskmd`(引数は手順 5 の ④ と同じ)で、**タスク MD だけを起動前の内容へ戻す**(外部が更新したチェックボックスを信用しない。**他の未追跡ファイルは復元しない** — 手順 6 の「**巻き戻さず引き継ぐ**」と衝突し、外部が途中まで書いた実装成果を消す。2026-09-17 決定 45)。
   - **exit 33 なら自動で引き継がず**、stdout の `TOUCHED=`(実ツリーに対して既に行ったこと)を添えて人の判断を仰ぐ(信頼できる完了条件が無いまま引き継がない。2026-09-17 決定 46)
   - ③**外部が改変・削除した他の未追跡ファイルは、手順 5 の `CHANGE=` と `BASE=` で差分を提示するだけに留める**(復元するかは内蔵 implementer とユーザーが判断する)
   - ④タイムアウトの kill は**書き込み途中のファイルを残しうる**
7′. **タスク MD の保護(結末を問わず必ず通す。2026-09-17 決定 51)**: 手順 5 の ④ がこれに当たる。**Phase 4 以降が完了条件として読むのは `DOD=` のコピーで、外部が書いたタスク MD ではない**(正常終了でも省かない。チェック状態の更新と要件本文の変更の分け方、選択パスと実体の対応の確認、それぞれの到達条件は §12-2 が正本)
8. **報告(サイレント縮退禁止)**: どちらの担い手で実装したか・承認の有無・起動前に内蔵に切り替えたか走行中に引き継いだか**比較不能またはタスク MD の判断待ちで止めたか**・残っていた変更・保護領域とログのパスを必ず報告する
   - (テンプレは §12-5。**比較不能には専用テンプレがあり、タスク MD の判断待ちにも同じテンプレを使う** —
   - 何が一致しなかったか・判定できなくなった要素・**以後の自動処理を停止したことと、その時点までに実ツリーへ何をしたか**(`restore-taskmd` の `TOUCHED=`。「自動復元を行っていない」と一律に報告しない。2026-09-17 決定 49)・ユーザーに求める判断・再開の条件。2026-09-17 決定 48)。
   - 保護領域は **Phase 7 の完了処理の後に `implement-guard.sh cleanup --state <STATE_DIR>` で消す**(完了条件の基準を保護領域から読むため。正常終了・引き継ぎ・縮退・`take` の後の起動前失敗のどれでも)。**消さないのは比較不能とタスク MD の判断待ちだけ**で、報告後までパスを提示する(§12-2)

## Phase 4: team-lead 検証(スコープ縮小・偽完了の検出)

implementer の完了報告を受けたら、team-lead 自身が以下を機械的に突合する(無人では、各回の最初に「無人モード」の節の照合を行う。通らなければ失敗扱い)。

**対話の本文の照合**(対話〈`--unattended` なし〉で内蔵 implementer に委託したときだけ行う。無人の R との照合とは別の照合):
- implementer の報告を受けたら、この Phase 4 の最初に `python3 {create-task の}scripts/task-digest.py <タスク MD>` を算出し直し、Phase 3 の委託の直前に控えた値と照らす。
- **照らしたら控えを捨てる**(次の委託の直前にだけ取り直す)。
- 一致しなければ、本文が変わったこと(タスク MD が追跡済みなら `git diff -- <タスク MD>` の出力を添える)を示してユーザーに確認し、答え(変更を受け入れてタスク MD を契約として直す / 元に戻す)に従う(原則 3)。**失敗扱いにしない**。
- 控えか今の値を算出できない(exit 0 以外・スクリプトや Python が無い)ときは、その旨を報告して続ける(照合できなかったことを完了報告に残す)。
- 控えが無い Phase 4(再開モードの最初の機械検証・検証のみモード・外部 implementer の後の Phase 4)では照らさない。

1. **diff スナップショット**: `bash {do-task の}scripts/diff-snapshot.sh` で `.claude/reviews/diff-{TASK_NAME}-iter{ITER}.md` を生成する
   - (基準コミットからの追跡差分 + index + 途中 commit + 未追跡。secret_paths・`.claude/` の状態ファイル・基準時点から在った未追跡は除外)。
   - 無人では基準・開始時・現在の profile 集合を `--secret-profile-ref` で固定し、同じ集合を内蔵レビュー・外部 patch・commit 除外にも渡す。
   - **exit 0 か 21 のときだけ Phase 4 の突合へ進む** — それ以外(2 / 4 / 20 / 22 と契約表に無いコード)は終了コードと stderr をそのまま報告して停止する(無人では失敗扱い — D10・D19)。
   - 対象が git リポジトリでないときは生成できないので、その旨を報告し、対象ファイルを直接読んで突合し、Phase 6 の独立レビューへは diff の代わりに『基準なし・非 git』の旨と対象ファイル表のパスを渡す(タスク MD に基準行があるのに git リポジトリでないと判定されたら、続行せず停止して報告する)。
   - 呼び出し引数の組み立て・`secret_paths` 要素の内容ガード・ダイジェスト不一致時の再実行・`- [x]` に対応する変更が見当たらないときの確認手順は [references/diff-snapshot-call.md](references/diff-snapshot-call.md) が正本
   - **commit が続く周では** snapshot 生成の前後を `review-guard.py take` と `seal` で挟む。
   - snapshot・patch と同じ review 入力、除外 ERE、対象集合を固定する。seal 後の 3 hash と開示一覧を保持し、Phase 6 の reviewer へ渡す。
   - 機密除外・state・review 入力・照合不能時の停止は [references/diff-snapshot-call.md](references/diff-snapshot-call.md) が正本
2. **チェックリスト突合**: `grep -cE '^\s*- \[(x|X)\]' {タスクMD}` の完了数と Phase 0 の総数を比較。未完了が残るのに完了報告されていないか。各 `- [x]` に対応する変更が diff に実在するか(**全件**。`- [x]` ごとに、対応する変更のパスか、diff を伴わない項目〈品質ゲートの実行など〉である旨を報告に並べる)
3. **スコープ縮小 grep**: implementer の報告とタスク MD 追記に対して `grep -E '段階的に実施|後続タスク|今回はスコープ外|のみ作成|次回対応|一旦'` を実行。ヒットしたら設計時のスコープと突合し、縮小なら差し戻す
4. **数値突合**: タスク MD に「N 件の〜を…」とあれば実際に数える(Grep -c 等)
5. **契約整合**: スコープ外項目について「受理するが処理されない」不整合が生まれていないか実コードで確認する

問題があれば implementer に差し戻す(ITER をインクリメント。差し戻しテンプレは references/review-protocol.md)。
- **フル段階かつ内蔵 implementer のときは同じ宛先へ再依頼して `implementer` に直接戻す**(実装文脈が保たれ、再調査のやり直しが消える)。
- **標準段階、および外部 implementer では**前回成果物のパスを含めた新規委託で代替する — **外部 implementer は段階を問わず宛先にならないため、差し戻しは毎回新規起動になり**(軸 5)、前回の diff とレビュー指摘を毎回渡し直す。
- **検証手順そのものは担い手によって変わらない**(変わるのは差し戻しの手段だけ)。

## Phase 5: 完了条件の再実行

commit が続く周では、[review-protocol.md](references/review-protocol.md) の `run-checks` で必須 gate 全件を直接実行し、構造化結果と hash を保持する。
clean checkout の検査を実行結果として使う。作業ツリー側でも検査して内容が変わった場合は、Phase 4 から対象を固定し直す。

タスク MD のチェックリストにある品質ゲート(format / check / typecheck / test、必要なら build)を **team-lead 自身が実行**し、全て緑を確認する。ロジック変更を含むのにテストが 1 件も追加・更新されていない場合は妥当性を確認する。

失敗したら**原因を切り分けてから**対処する(切り分け結果は記録に残す):
- **実装起因**(コードの誤り・テストの不整合)→ Phase 3 へ差し戻し
- **環境起因**(DB 未起動・依存未導入・ポート衝突・env 不足)→ implementer に差し戻さず、環境を整えて再実行する(自力で直せない場合はユーザーに報告。無人では失敗扱い — D11)

## Phase 5.5: 実動確認(タスク種別に応じて)

機械検査(静的+テスト)が緑でも、**実際に動かして変更フローを観察するまで完了としない**(チェック 3 階層の第 3 層。design §5-19。例外は下の表の「実施不能」だけ)。タスク MD の完了条件(実行可能な確認手順)と種別の実動確認(実際に動かして、変更どおりに動くかを見る確認。task-types.md の列)に従って実行する:

- **確認ツールは実行時検出**: dev サーバー / E2E ランナー(playwright.config 等の設定検出)/ HTTP クライアント(curl 等)/ ブラウザ MCP(profile の `mcp_servers` 宣言、または実行時に使えるもの)。特定ツールをハードコードしない
- **ブラウザでの目視確認**: ブラウザ MCP があれば team-lead が操作して観察する(勝手に「確認済み」にしない。確認を実行できないときは下の表と既定に従う)
- 実行した確認と**観察事実**(何を操作し何が表示されたか)を追加修正記録・完了報告に含める
- **確認の状態は 3 つ**(下の表)。確認手順ごとに付け、追加修正記録・完了報告に書く(サイレントスキップ禁止)。タスクの状態は、結果待ちが 1 つでもあれば結果待ち、無くて実施不能が 1 つでもあれば実施不能、すべて実施済なら実施済
- 小規模タスク(文言修正等)でも最低限「変更が実際に反映されている」ことは確認する(ほかの確認手順が実施不能でも、これは行う。これすら実行できないときは、その旨を実施不能の理由に書く)

| 状態 | いつ | /do-task の完了 | /ship-task の PR |
|---|---|---|---|
| **実施済** | 確認手順を実行し、期待どおりの観察事実を記録した | できる | 開ける |
| **実施不能** | 確認を実行できない(手段が無い・手段を動かす環境を自力で整えられない)、またはユーザーが確認しないと答えた。理由 = 探した手段や試した復旧と無かったもの、またはユーザーの回答 | できる。「実動確認(実際に動かして、変更どおりに動くかを見る確認): 実施不能({理由})」と明記する | 開ける。PR 本文に実施不能と理由を書き、確認手順を残課題へ転記する |
| **結果待ち** | ユーザーの対応(確認の実施、または環境の復旧)を待っている | できない(Phase 7 の中断として扱う) | 開かない(停止) |

- **確認を実行できないときの既定**: 対話できる実行では、確認手順(環境起因なら復旧の依頼)をユーザーに伝えて**結果待ち**にする。ユーザーが確認できない・確認しないと答えたら、その回答を理由に実施不能にする。**確認を提示して応答を受け取れない実行**では、依頼せず**実施不能**にする(この判定は運用上の義務で、機構の保証ではない)
- team-lead の判断だけで実施不能にできるのは、確認を実行できないときだけ。手段が在り環境も整うのに実行しなかった確認は、実施不能にしない
- **確認が失敗したとき**(実行できたが観察結果が期待と違う)は、Phase 5 と同じく原因を切り分ける。実装起因は Phase 3 へ差し戻し、環境起因は環境を整えて再実行する(自力で整えられなければ上の既定に従う)

## Phase 6: 独立レビュー

Phase 4 で固定した `REVIEW_BINDING_SHA256`、対象集合の hash、ignore/ignored 未追跡の差を reviewer の入力に含める。
品質コマンドと入口・test/selftest/検証器の変更一覧も渡す。`APPROVED` は、その固定集合だけに対する返答として扱う。
改名・commit・PR の前の照合と保留の扱いは [references/review-protocol.md](references/review-protocol.md) が正本。

[references/review-protocol.md](references/review-protocol.md) に従う。要点:

- reviewer は実装に関与していない読み取り専用の委託を新規起動(1 体)。`--reviewers=3` では `reviewer-internal` / `reviewer-strong` / `reviewer-alt` の 3 体を単一メッセージで並列起動する(枡割り当ては §2(a)、能力帯の解決は軸 3)
- reviewer には diff・タスク MD・レビュー観点(6 カテゴリ: 機能保全 / 契約整合 / タスク充足 / テスト妥当性 / 規約 / セキュリティ・機密)を渡し、APPROVED または指摘リスト(JSON)を返させる
- **team-lead が各指摘を実コードで裏取り(実際のコードやファイルを読んで確かめること)してトリアージ(指摘を扱いごとに振り分けること)**(valid / invalid / needs-user)。invalid は理由を記録。valid のみ修正へ
- **外部ランナー(オプトイン)**: `--runners` または `features.runners` が**宣言されている場合に限り**、外部 CLI レビュアーを追加する。
  - 手順・編成・判定・終了コードの契約はすべて [references/external-runners.md](references/external-runners.md) が正本(ここでは再掲しない)。
  - 宣言が無ければ内蔵編成のみで、外部 CLI を探しに行かない。
  - 宣言されたのに使えないときはエラーとして報告し、内蔵編成に切り替えて続行する(`review-agent.sh` が置き場の理由の exit 2〈stderr の `ERROR [usage] ` の行に `既定のログ置き場` か `--log-file の置き場`〉で止まったときは内蔵に切り替えず、停止して報告する — 同書 §10)。
  - 確認が要る外部レビュアー(`secret_paths` があるときの確認・`--command` の明示承認)は、無人では起動せず内蔵編成で続行して報告する(D15)
- **委託の解決(役割語 → 実行バックエンド)**: 解決表に到達できない、または独立レビュアーを起動できない場合はレビュー未完了を報告し、完了承認・後続公開へ進めない
- **収束条件は全 reviewer の APPROVED**。そこに至るまで Phase 3〜6 を反復し、コストを理由に打ち切らない(design §5-10)。同一指摘 2 回連続残存・`--max-iter`(既定 5)到達は**停止点ではなく判断を仰ぐ点**で、状況を報告してユーザーの判断を仰ぐ(無人では保留 — D2。needs-user も保留 — D13)
- 記録: `.claude/reviews/{role}-{TASK_NAME}-iter{ITER}.md`。
  - 書く直前に `bash {do-task の}scripts/reviews-dir.sh ensure --root <管理ルート>` を打ち、exit 0 以外なら記録を書かずに停止して報告する(トリアージ・リカバリの記録も同じ — references/review-protocol.md)

## Phase 7: 完了処理

1. タスク MD の全チェックボックスが `- [x]` であることを最終確認(無人では、その前に「無人モード」の節の照合を行う。format ゲートなどが本文を変えたまま完了にしないため)
2. 独立レビュアーが 1 名以上いて全員 APPROVED であることを確認する。未承認なら完了状態・リネームへ進まず、中断記録に理由を残す(無人では保留として返す — D16)
3. 改名先(対象タスク MD と同じディレクトリ。以下 `<dir>`)の `<dir>/完了_{タスク名}.md` が既に在れば(`[ -e ] || [ -L ]`)、追記・ステータス更新・改名のどれも行わず停止し、既存ファイルのパスを添えて報告する
   - (上書きも連番もしない — 素の `mv` は黙って上書きする。この停止ではタスク MD を変更しない — 中断時の追加修正記録への追記もしない。無人では失敗扱い — D17)
4. **commit が続く周**は、この手順 4〜6 の代わりに [review-protocol.md](references/review-protocol.md) の attest→finalize→期待バイト適用を行う。
   - 元のモードを保つファイル改名で index をまだ変えず、stage 前の照合を通す。記録の任意追記と `git mv` は使わない。
   - 以下の 4〜6 は commit が続かない単独実行だけ。外部本文の正本ではローカル MD を新設せず、同 reference の外部本文経路に従う。
   **追加修正記録**をタスク MD の追加修正記録の節の中に追記する(`## ` の見出しを足して節の外に書かない — 再開判定と新規着手の判定は節の中だけを見る。節の範囲は ../create-task/references/task-template.md の記法の規約):
   - 日付 / 使用スキル(do-task)/ 反復回数 / reviewer 結果(3 体なら内訳)/ 品質ゲート実行結果 / 特記事項
5. ステータス行を `> **ステータス**: ✅ 完了({YYYY-MM-DD})` に更新
6. 対象タスク MD と同じディレクトリ内で `進行中_{タスク名}.md` を `完了_{タスク名}.md` に改名する。
   - 別の保存先へ移動しない。
   - **手段はタスク MD が追跡済みかで分ける**(`<前置き>` は [references/base-commit.md](references/base-commit.md) が全 git 呼び出しに定めるオプション列 — `git` に続く `--no-pager` 以降。
   - 事前検査とその export は含まない。外部 implementer が残したローカル設定の `core.fsmonitor` やフックを起動しないため):
   - `git -C <dir> <前置き> ls-files --error-unmatch -- ':(literal)進行中_{タスク名}.md'` が **rc 0**(追跡済み。stage 済みの新規を含む)→ `git -C <dir> <前置き> mv -- '進行中_{タスク名}.md' '完了_{タスク名}.md'`。
     - stage されるのはリネームだけで、commit はしない。
     - この `git mv` が失敗したとき(sparse-checkout の定義外など)は `mv` に切り替えず停止して報告する(追記とステータス更新は済んでいて、残るのは改名だけである旨を添える)
   - **それ以外** → `mv -- '<dir>/進行中_{タスク名}.md' '<dir>/完了_{タスク名}.md'`(rc 1 = 未追跡・ignore 済み。未追跡への `git mv` は `fatal: not under version control` で失敗する)。
     - rc が 0 でも 1 でもないとき(非 git・git の異常)は、追跡済みかを確かめられなかった旨と rc を完了報告に添える
7. 完了報告: 変更ファイル一覧 / 書式・型・テスト・ビルドの自動の検査の結果 / レビュー概要 / 残課題(あれば)/ 次の提案(`/update-doc --task=<完了_ のパス>` でドキュメント同期、コミットの提案 — **コミットはユーザーが求めた場合のみ**)

中断する場合(ユーザー判断待ち・ブロッカー)は、
- ステータスを 🚧 のまま進捗を追加修正記録の節の中に書き、何がブロッカーかを報告する。
- 長期保留なら `保留_` へのリネームを提案し、**ユーザーの合意があれば改名する**(改名先の確認と手段は手順 3・6 と同じ。commit はしない)。
- **/ship-task から呼ばれたときは提案も改名もしない**(ship-task の停止はタスク MD を stage しない)。
- 再開するときは、人が同じディレクトリで `進行中_` に戻してから /do-task に渡す。
- 無人では提案せず保留として返す(改名と commit は ship-task — D16)。

## 最終ゲート(完了報告前セルフチェック)

- [ ] 品質ゲートを team-lead 自身が実行して緑を確認した
- [ ] 実動確認の状態が「実施済」(観察事実を記録)か「実施不能」(理由を明記)である(「結果待ち」のまま完了にしていない)
- [ ] チェックリスト突合・スコープ縮小 grep を実施した
- [ ] 独立レビュアーが 1 名以上いて、全 reviewer が APPROVED である
- [ ] タスクファイルをリネームし、追加修正記録を追記した
- [ ] 実装内容とタスク MD の内容が一致している(どちらかだけの変更がない)

## 関連スキル

- 前提: /create-task(タスク MD が無い場合はまず設計書を作る)
- 完了後: /update-doc --task(完了タスク駆動の差分同期)
- 設計から PR まで通しで回す: /ship-task(このスキルを実装工程として内部で実行し、doc 同期と PR 作成まで続ける)

## Git の共通安全前置き

read・index/worktree の変更・commit・network の全 Git 呼出は、base-commit.md の safe Git 前置きを付ける。hook、fsmonitor、署名、LFS filter を無効化し、品質確認は hook に委ねない。

filter を使う既存リポジトリは、前置きで一般化しない。base-commit.md の precheck が承認した filter だけを明示的に無効化してから実行する。
