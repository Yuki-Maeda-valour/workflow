#!/usr/bin/env bash
# 外部 implementer の起動の前後で、作業ツリーと git メタ状態を保護する専用スクリプト(dev-workflow)。
# 仕様は ../references/external-runners.md の §12-2(作業ツリーとメタ状態の保護)・§12-3(外部の git
# 操作への対処)・§12-7(引き継ぎの発火条件)。implement-agent.sh(外部 implementer の起動)と対で使う:
# 起動の前に take、起動の後に compare → taskmd-diff →(引き継ぎなら)restore-taskmd、完了処理の後に
# cleanup。呼び出し側は起動と、終了値・合法な応答の照合による分岐を行う。
# diff-snapshot.sh(レビュー用の diff スナップショット)とは別物で、共通部分の切り出しもしていない
# (external-runners.md §11 の 2026-09-17 決定 9。必要な部品は複製して持つ)。
#
# 使い方:
#   bash implement-guard.sh take           --cwd <dir> --task-md <path>
#   bash implement-guard.sh config-check   --cwd <dir> --state <dir> --manifest-sha256 <hex> --snapshot-sha256 <hex>
#   bash implement-guard.sh compare        --cwd <dir> --state <dir> --manifest-sha256 <hex> --snapshot-sha256 <hex> --run-rc <n>
#   bash implement-guard.sh taskmd-diff    --cwd <dir> --state <dir> --manifest-sha256 <hex> --snapshot-sha256 <hex>
#   restore_taskmd_supervised implement-guard.sh --cwd <dir> --state <dir> --manifest-sha256 <hex> --snapshot-sha256 <hex>
#   bash implement-guard.sh cleanup        --state <dir>
#
# 復元の前に ../references/runtime-requirements.md の「タスク本文の復元」にある
# restore_taskmd_supervised 関数を呼出側で定義し、上の例で呼ぶ。関数を利用できなければ起動しない。
# 関数は timeout、無ければ gtimeout の GNU 版・必要オプションを確認し、確認できなければ起動しない。
# 公開入口全体を330秒 TERM・追加5秒 KILLで監督する。終了値と合法な応答を照合し、不明なら止める。
#
# サブコマンド:
#   take            起動前のスナップショット(5 要素)と退避を保護領域に作る
#   compare         起動後に 5 要素を取り直して比較し、結末(正常終了 / 引き継ぎ / 縮退 / 比較不能)を決める
#   taskmd-diff     タスク MD の変化を「チェック状態の更新だけ」と「要件本文の変更」に分ける
#   restore-taskmd  タスク MD だけを起動前の内容へ戻す(実ツリーに書くのはこのサブコマンドだけ)
#   cleanup         保護領域を消す
#
# 引数:
#   --cwd <dir>              管理ルート。リポジトリの toplevel かその配下。toplevel は自前で解決し、
#                            **スナップショットの対象は toplevel の全体**、パスは toplevel 相対で扱う。
#                            compare / taskmd-diff / restore-taskmd では take のときと同じ値を渡す
#                            (toplevel などは保護領域の記録を使う。違う場所を渡したら exit 2)
#   --task-md <path>         タスク MD。--cwd 相対か絶対パス。symlink なら実体を辿って本文を守る
#   --state <dir>            take が stdout に出した STATE_DIR
#   --manifest-sha256 <hex>  take が出した MANIFEST_SHA256(呼び出し側がセッション文脈に保持した値)
#   --snapshot-sha256 <hex>  take が出した SNAPSHOT_SHA256(同上)
#   --run-rc <n>             implement-agent.sh の終了コードをそのまま渡す(0 以上の十進整数)
#   -h / --help              この使い方を出す
#
# 終了コード(**ここに無いコード・2・20 が返ったら、呼び出し側は終了コードと stderr を報告して停止する。
#             「変化なし」や成功に読み替えない**):
#   0   成功(take: 作成した / config-check: 設定が同じ / compare: 正常終了 / taskmd-diff: 変化なしかチェック状態の更新だけ /
#       restore-taskmd: 戻した / cleanup: 消した)
#   2   usage(引数の不備。cleanup の --state が保護領域の guard-* でないときもここ — 何も消さない)
#   20  internal(予期しない失敗。take は作りかけの保護領域を消す)
#   30  縮退(take のみ。外部委託を行わない)。stderr に `ERROR [<理由コード>] 説明`:
#         not-git            --cwd が git リポジトリの中でない(解決した toplevel が --cwd を含まない場合も)
#         unmerged           index がコンフリクト中
#         no-protected-area  保護領域を外部の書き込み範囲外に解決できない
#         over-limit         退避の上限(2000 件・200 MiB)を超えた
#         taskmd             タスク MD の実体を確定できない(存在しない・リンク切れ・ループ・通常ファイル
#                            でない・読めない)、または退避・ハッシュ・マニフェスト登録に失敗した
#   31  引き継ぎ(compare。--run-rc が 0 以外で、作業ツリーか git メタに変化がある)
#   32  縮退(compare。--run-rc が 0 以外で、どちらにも変化が無い)
#   33  比較不能(config-check・compare)/ 照合・復元の失敗(taskmd-diff・restore-taskmd)。人の判断を仰ぐ
#   34  要件本文の変更(taskmd-diff)
#   35  選択パスと実体の対応の変化(taskmd-diff)
#
# stdout(機械可読の `KEY=VALUE` 行。値は 1 行。**パスの値は C 風引用** — タブ・改行・その他の制御文字・
#         `"`・`\` を含むか先頭が `"` のときだけ `"…"` で囲み `\t` `\n` `\\` `\"`、その他の制御文字は
#         8 進 `\ooo`。診断は stderr の `NOTE:` / `ERROR [理由コード]`):
#   config-check:
#     CONFIG=same GIT_SKIPPED=no(隔離Gitで使用先を検査した場合)
#     33 は RESULT=incomparable、REASON、実際のGit呼出に応じたGIT_SKIPPED。
#   take:
#     STATE_DIR=<保護領域のパス>  MANIFEST_SHA256=<hex>  SNAPSHOT_SHA256=<hex>
#     ENTRIES=<未追跡エントリの件数>  BYTES=<退避対象の総バイト数>
#     UNREADABLE=<読めなかったエントリ数。パスは stderr に `NOTE: unreadable <パス>`>
#     TASKMD_REAL=<タスク MD の実体パス>
#   compare:
#     RESULT=normal|takeover|fallback|incomparable
#     WORKTREE_CHANGED=yes|no|unknown   (①②③ の変化)
#     GITMETA_CHANGED=yes|no|unknown    (④⑤ の変化)
#     CHANGE=<分類>\t<詳細>             (変化 1 件ごとに 1 行)
#       tracked-diff / status / submodule   <起動前の記録ファイルのパス>\t<現在値の sha256>
#       untracked-added / untracked-removed / untracked-changed   <toplevel 相対パス>
#                               (removed は「未追跡の対象集合から消えた」= 削除・追跡化・ignore 化。
#                                タスク MD の実体〈マニフェストの T 行〉の変化もこの分類で出し、
#                                そのときのパスは実体の絶対パス)
#       head / branch / index-tree   <旧値> -> <新値>(index が unmerged なら値は `(unmerged)`)
#       refs                    `+ <sha> <ref 名>`(増えた)/ `- <sha> <ref 名>`(消えた)/
#                               `~ <ref 名> <旧 sha> -> <新 sha>`(変わった)
#       stash                   `+ <reflog の行>` / `- <reflog の行>`
#       hooks / config          <変わったファイルのパス>
#     BASE=<退避コピーのパス>\t<ok|unverified>
#                               未追跡の changed / removed の直後に出す。unverified は退避コピーが実体照合を
#                               通らなかった(または退避が無い)= 「変更の有無」は言えるが「内容差分は提示不能」
#     RESULT=incomparable のとき:
#       REASON=manifest-digest|snapshot-digest|state-missing|hooks-changed|config-changed|config-origin-missing|retake-failed
#       GIT_SKIPPED=yes|no     (yes = git を 1 回も呼んでいない。retake-failed は git を呼んだ後の失敗なら no)
#   taskmd-diff:
#     TASKMD=same|checks-only|body-changed|mapping-changed
#     DOD=<起動前のタスク MD のコピーのパス>(以後の工程が完了条件の基準として読む)
#     exit 33 のとき REASON=digest|taskmd-body
#   restore-taskmd:
#     RESTORED=yes|no
#     TOUCHED=none|copied|replaced|unknown
#     TEMP=none|removed|retained|unknown
#     REASON=<理由コード>(非成功時だけ)
#     TEMP_PATH=<C 風引用した専用領域>(TEMP=retained のときだけ)
#     既存対象は準備した通常ファイルで原子的に replaced。不在からの作成は copied。
#     準備失敗は対象を保つ。公開後の失敗は変更済みと報告し、自動で戻さない。
#     成功は exit 0 / RESTORED=yes / copied または replaced / TEMP=removed の全行一致。
#     不正・欠落・重複した応答は caller が unknown と扱い、引継ぎを停止する。
#   REASON(taskmd-diff・restore-taskmd)の意味:
#     digest         保護領域の検査かダイジェスト照合に失敗した
#     taskmd-body    退避した本文の種類・モード・sha256 が記録と一致しない
#     dest-symlink / dest-dir / dest-special  復元先が通常ファイルか不存在でない
#     internal-argument / runtime-unavailable  内部引数・必要機能が不成立
#     prepare-failed / copy-failed / replace-failed  準備・コピー・公開の失敗
#     verify-failed / cleanup-failed  照合・自分の専用一時物の回収の失敗
#     budget-exceeded / child-unreaped  有限予算・直接コピー子の回収の失敗
#     interrupted    捕捉した TERM/INT/HUP(exit 20)。通常の復元失敗は exit 33
#
# 保護領域:
#   解決順は `$XDG_STATE_HOME` → `$HOME/.local/state` → `$HOME/.cache`。解決後の実体パスが `/tmp`・
#   `$TMPDIR`・toplevel の配下なら次の候補へ進み、全滅なら縮退 `no-protected-area`。
#   **候補はまだ存在しなくてよい**。その場合は存在する最も深い親を物理的に解決し、まだ無い分は
#   語彙的に正規化して(`.` は捨て、`..` は 1 つ前の要素と相殺する)繋いだ絶対パスで判定してから作る。
#   **棄却する候補には mkdir も chmod もしない**(相殺しきれない `..` が残る候補も棄却する)。
#   `<候補>/dev-workflow/guard-XXXXXX/` を 0700 で作る。レイアウト:
#     snapshot/1-diff.bin  snapshot/2-status.z  snapshot/4-meta.txt  snapshot/5-stash.txt
#     snapshot/taskmd.txt(選択パス・実体パス・種別)、snapshot/config-origins.txt(設定候補の記録)
#     snapshot/config-contexts.txt(使用先・設定blob)、snapshot/6-contexts.txt(子のHEAD/index/diff/status)
#     manifest.tsv         files/<toplevel 相対パス>(退避コピー)
#     taskmd-body          (タスク MD の実体のコピー。追跡済み・ignore 済み・リポジトリ外でも必ず)
#   **追跡ファイルの差分と未追跡ファイルの平文複製が入る。** 消すのは cleanup だけ。
#
# マニフェスト(manifest.tsv):
#   1 行 1 エントリのタブ区切り `種別\tモード\t値\tパス`。並びは LC_ALL=C のパス順。
#     T  タスク MD の実体(必ず先頭行。値 = sha256、パス = 実体の絶対パス)
#     f  通常ファイル(値 = **退避コピー側の**生バイトの sha256)
#     l  symlink(値 = リンク文字列。辿らない)
#     o  その他(FIFO・ソケット・ネスト repo のディレクトリ等。値 = `-`)
#     u  読めない(値 = `-`)
#   パスとリンク文字列は上と同じ C 風引用。モードは `stat -c %a`、使えなければ `stat -f %Lp`。
#
# ダイジェスト:
#   MANIFEST_SHA256 = manifest.tsv の sha256
#   SNAPSHOT_SHA256 = snapshot/ の各ファイルの「sha256 + 空白 2 個 + ファイル名」を名前順に並べたものの sha256
#
# 5 要素の取り方(全 git 呼び出しに前置き `--no-pager --no-replace-objects -c core.quotePath=false
#   -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false`。
#   除外 = `<--cwd の toplevel 相対パス>/.claude/reviews`。①② は pathspec、③ は同じ錨で外す):
#   ① `diff --binary --no-ext-diff --no-textconv --submodule=short --ignore-submodules=dirty <HEAD または空ツリー>`(`-c diff.autoRefreshIndex=false
#      -c core.ignoreStat=false` つき。コミット 0 の repo は空ツリーと比べる)
#   ② `status --porcelain=v1 -z -uall --ignore-submodules=dirty`(`GIT_OPTIONAL_LOCKS=0`)
#   ③ マニフェスト = `ls-files -o --exclude-standard -z` のうち除外の配下でないもの + タスク MD の実体。
#      **タスク MD は除外より優先する**(pathspec の除外は打ち消せないので、除外の配下に在るタスク MD は
#      ①② には現れず、③ の T 行が単独で担う)
#   ④ HEAD / index の tree(**使い捨ての index** で `write-tree`)/ ブランチ名 / refs 全体 /
#      hooks ディレクトリ(git が実際に hook を探す場所)の全エントリのダイジェスト /
#      `config` と `config.worktree` の sha256(無いファイルは `(無し)`)と、開始時にキーを提供した
#      file origin の経路・構成要素の種別/リンク字面/device/inode・実体の sha256。値は新規記録に含めない。
#      親とリンクの識別子は厳密に照合。末端通常ファイルの同内容の再作成は許可する。
#   ⑤ stash: `refs/stash` が在れば `reflog show --format='%H %gd %gs' refs/stash` の全体、無ければ `stash 無し`
#
# git の状態を変えない:
#   index・HEAD・refs・stash・config に書かない。`write-tree` は使い捨ての index に対して行い、
#   `status` は `GIT_OPTIONAL_LOCKS=0`、`diff` は `diff.autoRefreshIndex=false` で index の書き直しを止める。
#
# 設定されたプログラムを実行しない:
#   compare は、保護領域の検査 → ダイジェスト照合 → **hooks と config の検査(記録済みの実体パスからの
#   ファイル読み取りだけ)** を終えるまで git を 1 回も呼ばない。hooks か config が変わっていたら
#   `incomparable`(hooks-changed / config-changed)で止まり、以後の git も打たない。
#   出典記録の無い旧 state の compare は config-origin-missing・33。他サブコマンドは従来どおり。
#   空/欠落/非成立 include と既知 root の新設も候補記録で照合する。
#   初回管理パス特定前の設定と、検査中だけの並行変更は開始時信頼境界の外である。
#   設定値を含まないのは新規出典記録と診断だけ。既存の生 diff・未追跡退避の範囲は変えない。
#   taskmd-diff と restore-taskmd は git を呼ばない。
#
# 前提:
#   bash 4.0 以上(連想配列を使う)。sha256 は `sha256sum` → `shasum` → `openssl` の順に探す。
# --- end usage ---
set -eEuo pipefail
export LC_ALL=C
# 連想配列を使うので bash 4.0 以上が要る(引数解析より前に見る)
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
  echo "ERROR [usage] bash 4.0 以上が必要" >&2
  exit 2
fi
# promisor remote の遅延取得を止める(取得は `remote.<名>.uploadpack` で任意コマンドを起動できる)
export GIT_NO_LAZY_FETCH=1
# index の日和見的な書き直し(stat 更新・untracked cache)を止める。② の status のため
export GIT_OPTIONAL_LOCKS=0
# 呼び出し元の環境から別のリポジトリ・別の index を掴まない(対象は常に --cwd から解決する)
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_PREFIX
# このスクリプトは stdin を使わない。開いた stdin を子プロセスに継がせない
exec </dev/null
# 保護領域に置くファイルは所有者だけが読めるようにする(退避コピーのモードは cp -a が元のまま保つ)
umask 077

MAX_ENTRIES=2000          # 退避の件数の上限(§12-2)
MAX_BYTES=209715200       # 退避の総サイズの上限(200 MiB)
REVIEWS_DIR='.claude/reviews'   # ログの置き場(①②③ から外す唯一の場所)

# ── git の前置き(全 git 呼び出しに付ける)──
GIT_PRE=(
  --no-pager --no-replace-objects
  -c core.quotePath=false
  -c core.fsmonitor= # MUT:c
  -c core.hooksPath=/dev/null
  -c core.ignoreCase=false
  -c core.splitIndex=false
)
# hooks ディレクトリの解決の 1 呼び出しだけに使う前置き(`-c core.hooksPath=/dev/null` を外したもの)。
# 付けたままだと `--git-path hooks` が /dev/null を返し、hook の設置を永久に検出できない
GIT_PRE_HOOKS=(--no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.ignoreCase=false -c core.splitIndex=false)
# ① の diff に足す設定
DIFF_CFG=(
  -c diff.autoRefreshIndex=false # MUT:l
  -c core.ignoreStat=false
)
# タスク MD のチェックマークだけを潰す正規化(行頭 = 字下げと `-` `*` `+` の箇条書きを含む)
NORM_SED='s/^([[:space:]]*[-*+][[:space:]]+\[)[xX](\])/\1 \2/' # MUT:f

# ── 引数の受け皿と状態 ──
SUB=""
CWD=""
TASK_MD=""
STATE_ARG=""
MAN_SHA=""
SNAP_SHA=""
RUN_RC=""
GIVEN=" "

TOP=""
CWD_PHYS=""
EXCL=""
STATE=""            # 検査を通った --state の実体パス
STATE_BASE=""
STATE_TRIED=""
NEW_STATE=""        # take が作成中の保護領域(完了するまでは失敗・シグナルで消す)
TAKE_DONE=0
WORK=""             # スクリプト自身の一時領域(保護領域の中に置く。/tmp は外部から書ける)
SHA_CMD=""
STAT_FLAVOR=""
EXCL_TMP=""
EXCL_TMPDIR=""
GIT_USED=0          # compare が git を 1 回でも呼んだか(GIT_SKIPPED の根拠)
TOUCHED_STATE="none"
TASKMD_SEL=""
TASKMD_REAL=""
TASKMD_KIND=""
TASKMD_REL=""       # 実体の toplevel 相対パス(リポジトリ外なら空)
TASKMD_SEL_REL=""   # 選択パスの toplevel 相対パス(リポジトリ外なら空)
CHANGES=()
WT_CHANGED=0
META_CHANGED=0
FS_HOOKS_CHANGED=0
FS_CONFIG_CHANGED=0
COLLECT_ERR=""
COLLECT_UNMERGED=0
ROW_KIND=""; ROW_MODE=""; ROW_VAL=""
UNQ=""
declare -A META=()
declare -A TM=()
declare -A REPORTED=()

cleanup_tmp() {
  if [ -n "${WORK:-}" ]; then rm -rf -- "$WORK"; fi
  if [ -n "${NEW_STATE:-}" ] && [ "${TAKE_DONE:-0}" -eq 0 ]; then rm -rf -- "$NEW_STATE"; fi
}
on_signal() {
  if [ "$SUB" = restore-taskmd ]; then trap '' TERM INT HUP; fi
  cleanup_tmp
  if [ "$SUB" = restore-taskmd ]; then printf 'RESTORED=no\nTOUCHED=%s\nTEMP=none\nREASON=interrupted\n' "$TOUCHED_STATE"; fi
  trap - EXIT
  exit 20
}
trap cleanup_tmp EXIT
trap on_signal TERM INT HUP
trap 'ec=$?; echo "ERROR [internal] 予期しない失敗(終了コード $ec・行 $LINENO)" >&2; exit 20' ERR

# ヘッダのコメント全体を使い方として出す(番兵で範囲が自動追従する)
usage() { sed -n '2,/^# --- end usage ---$/p' "$0" | sed '$d' >&2; }

fail_usage() { echo "ERROR [usage] $*" >&2; trap - ERR; exit 2; }
fail_internal() { echo "ERROR [internal] $*" >&2; trap - ERR; exit 20; }
arg_error() { echo "ERROR [usage] $*" >&2; usage; trap - ERR; exit 2; }
degrade() { # $1=理由コード 残り=説明(take の縮退。作りかけの保護領域は EXIT で消える)
  local reason="$1"; shift
  echo "ERROR [$reason] $*" >&2
  trap - ERR
  exit 30
}

need_val() { # $1=オプション名 $2=残り引数の個数
  if [ "$2" -lt 2 ]; then
    echo "ERROR [usage] $1 に値が必要です" >&2
    usage
    trap - ERR
    exit 2
  fi
}

# ── sha256(コマンドの解決順は 3 形式だけ吸収する)──
resolve_sha_cmd() {
  if command -v sha256sum >/dev/null 2>&1; then SHA_CMD=sha256sum
  elif command -v shasum >/dev/null 2>&1; then SHA_CMD=shasum
  elif command -v openssl >/dev/null 2>&1; then SHA_CMD=openssl
  else SHA_CMD=""; fi
}
sha256_file() { # $1=ファイル → 16 進を stdout へ
  case "$SHA_CMD" in
    sha256sum) sha256sum <"$1" | cut -d' ' -f1 ;;
    shasum) shasum -a 256 <"$1" | cut -d' ' -f1 ;;
    openssl) openssl dgst -sha256 <"$1" | sed 's/.*[= ]//' ;;
    *) return 1 ;;
  esac
}
sha256_stdin() { # stdin → 16 進を stdout へ(一時ファイルを作らない)
  case "$SHA_CMD" in
    sha256sum) sha256sum | cut -d' ' -f1 ;;
    shasum) shasum -a 256 | cut -d' ' -f1 ;;
    openssl) openssl dgst -sha256 | sed 's/.*[= ]//' ;;
    *) return 1 ;;
  esac
}
sha256_str() { printf '%s' "$1" | sha256_stdin; }

# ── モードとサイズ(stat の 2 形式だけ吸収する。起動時に 1 回だけ判定する)──
resolve_stat() {
  if stat -c %a -- / >/dev/null 2>&1; then STAT_FLAVOR=gnu
  elif stat -f %Lp -- / >/dev/null 2>&1; then STAT_FLAVOR=bsd
  else STAT_FLAVOR=""; fi
}
stat_mode() { # $1=パス(symlink は辿らない)→ 8 進のモード
  case "$STAT_FLAVOR" in
    gnu) stat -c %a -- "$1" ;;
    bsd) stat -f %Lp -- "$1" ;;
    *) return 1 ;;
  esac
}
stat_size() { # $1=パス → バイト数
  case "$STAT_FLAVOR" in
    gnu) stat -c %s -- "$1" ;;
    bsd) stat -f %z -- "$1" ;;
    *) return 1 ;;
  esac
}
resolve_tools() {
  resolve_sha_cmd
  [ -n "$SHA_CMD" ] || fail_internal "sha256 を計算できない(sha256sum / shasum / openssl が無い)"
  resolve_stat
  [ -n "$STAT_FLAVOR" ] || fail_internal "モードを取得できない(stat -c %a も stat -f %Lp も使えない)"
}

# ── C 風引用(git の core.quotePath と同じ規則)──
needs_quote() {
  case "$1" in
    '"'*) return 0 ;;
    *'"'*|*'\'*) return 0 ;;
    *[[:cntrl:]]*) return 0 ;;
  esac
  return 1
}
cquote() { # $1=パス → C 風引用した文字列
  local s="$1" out='"' i ch code
  for (( i = 0; i < ${#s}; i++ )); do
    ch="${s:i:1}"
    case "$ch" in
      '"') out="$out\\\"" ;;
      '\') out="$out\\\\" ;;
      $'\a') out="$out\\a" ;;
      $'\b') out="$out\\b" ;;
      $'\t') out="$out\\t" ;;
      $'\n') out="$out\\n" ;;
      $'\v') out="$out\\v" ;;
      $'\f') out="$out\\f" ;;
      $'\r') out="$out\\r" ;;
      *)
        case "$ch" in
          [[:cntrl:]]) printf -v code '\\%03o' "'$ch"; out="$out$code" ;;
          *) out="$out$ch" ;;
        esac
        ;;
    esac
  done
  printf '%s"' "$out"
}
path_field() { if needs_quote "$1"; then cquote "$1"; else printf '%s' "$1"; fi; }

# 表示の無害化(制御文字を ? に置き換える。比較は置換前のバイト列で行い、報告行にだけ掛ける)
sanitize() { printf '%s' "$1" | LC_ALL=C tr '[:cntrl:]' '?'; }

# C 風引用の逆変換(保護領域の記録を読み戻すときに使う)。結果は UNQ に入れる
# (コマンド置換を通すと末尾の改行が落ちるため)
cunquote() { # $1="…" の形の文字列
  local s="$1" out="" i=0 ch nx oct
  s="${s#\"}"
  s="${s%\"}"
  while [ "$i" -lt "${#s}" ]; do
    ch="${s:i:1}"
    if [ "$ch" != '\' ]; then out="$out$ch"; i=$((i + 1)); continue; fi
    nx="${s:i+1:1}"
    case "$nx" in
      a) out="$out"$'\a'; i=$((i + 2)) ;;
      b) out="$out"$'\b'; i=$((i + 2)) ;;
      t) out="$out"$'\t'; i=$((i + 2)) ;;
      n) out="$out"$'\n'; i=$((i + 2)) ;;
      v) out="$out"$'\v'; i=$((i + 2)) ;;
      f) out="$out"$'\f'; i=$((i + 2)) ;;
      r) out="$out"$'\r'; i=$((i + 2)) ;;
      [0-7]) oct="${s:i+1:3}"; printf -v ch "\\$oct"; out="$out$ch"; i=$((i + 4)) ;;
      *) out="$out$nx"; i=$((i + 2)) ;;
    esac
  done
  UNQ="$out"
}
field_value() { case "$1" in '"'*) cunquote "$1" ;; *) UNQ="$1" ;; esac; }

# ── パスの受け取り(末尾の改行を落とさない)──
# **コマンド置換 `$(…)` は末尾の改行をすべて落とす。** 名前の末尾に改行を持つファイルは
# 珍しいが作れてしまい、落としたパスは**別のファイル**を指す(指定されたタスク MD を守らず、
# 復元が無関係なファイルを上書きする)。そこでパスを返す経路はコマンド置換で受けず、
# 出力の後ろに終了コードを足して末尾の改行を守り、コマンドが足す改行 1 個だけを外す。
# 結果は `RP`(パス解決)・`CAP`(生の取得)に入れ、戻り値で成否を返す。
CAP=""
RP=""
capture_path() { # $1…=実行するコマンド → CAP に stdout(末尾の改行 1 個を落としたもの)。戻り値はそのコマンドのもの
  local out rc
  out="$("$@" 2>/dev/null; printf 'r%s' "$?")"
  rc="${out##*r}"          # 末尾に足した終了コード(出力に 'r' があっても最後のものが取れる)
  out="${out%r*}"
  CAP="${out%$'\n'}"
  case "$rc" in ''|*[!0-9]*) return 1 ;; esac
  return "$rc"
}
pwd_of() { CDPATH= cd -P -- "$1" >/dev/null 2>&1 && pwd -P; }   # capture_path 経由で使う
split_path() { # $1=パス → SP_DIR(親)と SP_BASE(末尾の要素)に分ける(basename / dirname の代わり)
  case "$1" in
    */?*) SP_BASE="${1##*/}"; SP_DIR="${1%/*}"; [ -n "$SP_DIR" ] || SP_DIR="/" ;;
    /) SP_BASE=""; SP_DIR="/" ;;
    *) SP_BASE="$1"; SP_DIR="." ;;
  esac
}
SP_DIR=""; SP_BASE=""; NREST=""

# 絶対化・正規化(realpath → readlink -f → cd + pwd -P)
abs_existing_dir() { # $1=存在するディレクトリ → RP に実体パス(解決できなければ 1)
  RP=""
  if command -v realpath >/dev/null 2>&1; then
    if capture_path realpath -- "$1" && [ -n "$CAP" ]; then RP="$CAP"; return 0; fi
  fi
  if command -v readlink >/dev/null 2>&1; then
    if capture_path readlink -f -- "$1" && [ -n "$CAP" ]; then RP="$CAP"; return 0; fi
  fi
  if capture_path pwd_of "$1" && [ -n "$CAP" ]; then RP="$CAP"; return 0; fi
  return 1
}
real_path() { # $1=パス(symlink は辿る)→ RP に実体パス。存在と種別は呼び出し側が確かめる
  RP=""
  if command -v realpath >/dev/null 2>&1; then
    if capture_path realpath -- "$1" && [ -n "$CAP" ]; then RP="$CAP"; return 0; fi
  fi
  if command -v readlink >/dev/null 2>&1; then
    if capture_path readlink -f -- "$1" && [ -n "$CAP" ]; then RP="$CAP"; return 0; fi
  fi
  return 1
}
read_link() { # $1=symlink → RP にリンク文字列(末尾の改行も保つ)
  local lnk
  RP=""
  lnk="$(readlink -- "$1" 2>/dev/null; printf 'r%s' "$?")"
  case "${lnk##*r}" in 0) : ;; *) return 1 ;; esac
  lnk="${lnk%r*}"
  RP="${lnk%$'\n'}"
  return 0
}

# ── 保護領域の解決(§12-2。名前だけでは保証にならないので解決後の実体パスを検査する)──
real_dir() { capture_path pwd_of "$1" && [ -n "$CAP" ] || return 1; RP="$CAP"; }
under_dir() { # $1=判定するパス $2=親候補(どちらも実体パス)
  [ -n "$2" ] || return 1
  case "$1" in "$2"|"$2"/*) return 0 ;; esac
  return 1
}
init_excl() {
  EXCL_TMP=""
  if real_dir /tmp; then EXCL_TMP="$RP"; fi
  EXCL_TMPDIR=""
  if [ -n "${TMPDIR:-}" ] && real_dir "$TMPDIR"; then EXCL_TMPDIR="$RP"; fi
}
forbidden_base() { # $1=実体パス → 0: 保護領域に使えない場所
  under_dir "$1" "$EXCL_TMP" || under_dir "$1" "$EXCL_TMPDIR" || under_dir "$1" "$TOP"
}
norm_rest() { # $1=まだ存在しない相対部分 → NREST に語彙的に正規化したもの
  # **この部分はまだ存在しない = symlink も無い**ので、`..` を語彙的に相殺してよい
  # (存在する側は real_dir が物理的に解決済み)。相殺しきれない `..` が残る候補は、
  # 実在する親より上を指すことになるので**副作用なしで棄却する**
  local rest="$1" seg out=""
  NREST=""
  while [ -n "$rest" ]; do
    seg="${rest%%/*}"
    if [ "$seg" = "$rest" ]; then rest=""; else rest="${rest#*/}"; fi
    case "$seg" in
      ''|.) continue ;;
      ..)
        [ -n "$out" ] || return 1
        case "$out" in
          */*) out="${out%/*}" ;;
          *) out="" ;;
        esac ;;
      *) if [ -n "$out" ]; then out="$out/$seg"; else out="$seg"; fi ;;
    esac
  done
  NREST="$out"
  return 0
}
resolve_future_path() { # $1=絶対パス(まだ無くてよい)→ RP に実体パス
  # **mkdir より前に判定する**ため、存在する最も深い親まで遡って実体パスを解決し、
  # まだ無い分を正規化してから後ろに継ぎ足す(棄却する候補に何も作らないため)。
  # 正規化しないと `<TOP の親>/new/../<TOP の名前>/dev-workflow` のような候補が
  # 前置き比較をすり抜け、作業ツリーの中に mkdir してしまう
  local p="$1" rest="" base
  while :; do
    if [ -d "$p" ]; then
      real_dir "$p" || return 1
      if [ -n "$rest" ]; then
        norm_rest "$rest" || return 1
        rest="$NREST" # MUT:m
        if [ -n "$rest" ]; then RP="$RP/$rest"; fi
      fi
      return 0
    fi
    if [ -e "$p" ] || [ -L "$p" ]; then return 1; fi   # 通常ファイル・リンク切れ(そもそも作れない)
    case "$p" in /|.|"") return 1 ;; esac
    split_path "$p"
    base="$SP_BASE"
    p="$SP_DIR"
    if [ -n "$rest" ]; then rest="$base/$rest"; else rest="$base"; fi
  done
}
resolve_state_base() { # take 用。STATE_BASE に `<候補>/dev-workflow` の実体パスを入れる
  local cand cand_want cand_real
  STATE_BASE=""
  STATE_TRIED=""
  for cand in "${XDG_STATE_HOME:-}" "${HOME:-}/.local/state" "${HOME:-}/.cache"; do
    case "$cand" in ""|"/.local/state"|"/.cache") continue ;; esac
    case "$cand" in /*) : ;; *) continue ;; esac   # 相対パスの候補は使わない
    cand="$cand/dev-workflow"
    STATE_TRIED="$STATE_TRIED $cand"
    # **棄却する候補には mkdir も chmod もしない**(実ツリーに書くのは restore-taskmd だけ。
    # 既に在る dev-workflow の権限も変えない)
    resolve_future_path "$cand" || continue
    cand_want="$RP"
    if forbidden_base "$cand_want"; then continue; fi
    # **作るのは正規化した絶対パス**(`..` を含む元の綴りに mkdir -p を掛けると、
    # 打ち消される途中の要素まで作ってしまう)
    mkdir -p -- "$cand_want" 2>/dev/null || continue
    chmod 700 "$cand_want" 2>/dev/null || true   # 平文複製が group/other から読めないように
    # 作る途中で差し替えられていないことを、作った後にもう一度確かめる
    real_dir "$cand_want" || continue
    cand_real="$RP"
    if forbidden_base "$cand_real"; then continue; fi
    STATE_BASE="$cand_real"
    break
  done
  if [ -z "$STATE_BASE" ]; then
    degrade no-protected-area "保護領域を外部の書き込み範囲外に解決できない(試した候補:${STATE_TRIED:- なし}。/tmp・\$TMPDIR・toplevel の配下は使えない。\$XDG_STATE_HOME を書き込み範囲外へ設定する)"
  fi
}
check_state_dir() { # $1=--state の値 → 0 なら STATE に実体パスを入れる(保護領域の候補配下の guard-* で、symlink でない)
  local s="$1" base parent_real cand cand_real
  while [ "$s" != "/" ] && [ "${s%/}" != "$s" ]; do s="${s%/}"; done
  [ -n "$s" ] || return 1
  if [ -L "$s" ] || [ ! -d "$s" ]; then return 1; fi
  split_path "$s"
  base="$SP_BASE"
  case "$base" in guard-?*) : ;; *) return 1 ;; esac
  real_dir "$SP_DIR" || return 1
  parent_real="$RP"
  for cand in "${XDG_STATE_HOME:-}" "${HOME:-}/.local/state" "${HOME:-}/.cache"; do
    case "$cand" in ""|"/.local/state"|"/.cache") continue ;; esac
    case "$cand" in /*) : ;; *) continue ;; esac
    # **take と同じ解決を通す**(`resolve_state_base` が正規化して採用した候補を、後続の
    # サブコマンドが同じ場所として認識できなくなるため。`cd` だけで解決すると、未作成の
    # 中間を打ち消す `..` を含む綴りの候補が一致せず、`state-missing` で比較不能になり、
    # `cleanup` も 2 を返して保護領域(平文複製)が残る)
    resolve_future_path "$cand/dev-workflow" || continue
    cand_real="$RP"
    if under_dir "$cand_real" "$EXCL_TMP" || under_dir "$cand_real" "$EXCL_TMPDIR"; then continue; fi
    if [ "$parent_real" = "$cand_real" ]; then
      STATE="$cand_real/$base"
      return 0
    fi
  done
  return 1
}

# ── ダイジェスト ──
snapshot_digest() { # $1=snapshot ディレクトリ → 16 進を stdout へ(通常ファイル以外が混じっていたら失敗)
  local d="$1" f h
  {
    for f in "$d"/*; do
      if [ ! -e "$f" ] && [ ! -L "$f" ]; then continue; fi
      if [ -L "$f" ] || [ ! -f "$f" ]; then exit 1; fi
      h="$(sha256_file "$f")" || exit 1
      printf '%s  %s\n' "$h" "${f##*/}"
    done
  } | sha256_stdin
}

# ── 比較の対象か(除外 = ログの置き場。**タスク MD は除外より優先する**。2026-09-17 決定 50)──
in_scope() { # $1=toplevel 相対パス → 0: 対象 / 1: 除外
  if [ "$1" = "$TASKMD_REL" ] || [ "$1" = "$TASKMD_SEL_REL" ]; then return 0; fi # MUT:g
  case "$1" in "$EXCL"|"$EXCL"/*) return 1 ;; esac
  return 0
}

# ── 設定候補(値は記録しない)──
# 実効 origin ではなく、root と条件を評価しない include の全候補を控える。比較側は
# この記録だけをファイル操作で照合するので、外部実装後の最初の Git より前に止まれる。
# 親の識別子に時刻やサイズを含めない(通常のファイル追加でディレクトリは更新される)。
stat_identity() {
  case "$STAT_FLAVOR" in
    gnu) stat -c '%d:%i' -- "$1" ;;
    bsd) stat -f '%d:%i' -- "$1" ;;
    *) return 1 ;;
  esac
}
ORIGIN_MODE=""
ORIGIN_CHANGED=""
CAND_TARGET=""
CAND_MISSING_ACCEPTED=0
CONFIG_READ_BYTES=0
config_target_size() { # Git/parserは論理名が末端symlinkでもtargetの本文を読む
  case "$STAT_FLAVOR" in
    gnu) stat -Lc %s -- "$1" ;;
    bsd) stat -Lf %z -- "$1" ;;
    *) return 1 ;;
  esac
}
reserve_config_read() { # $1=これから本文を読む通常 file。親シェルで予約してから読む
  # command substitution の中で加算すると親へ戻らないので、hash / parser の呼出側が直接呼ぶ。
  local size
  size="$(config_target_size "$1" 2>/dev/null)" || return 1
  [ "$size" -le $((16 * 1024 * 1024)) ] || return 1
  reserve_config_bytes "$size"
}
# 読取の割当表(同じ操作を別の欄へ再加算しない):
# 通常候補 hash/probe/parser・root digest -> reserve_config_read(1本文/操作)
# 選択blob -> metadataはF内32N、実体化/転送/hash/parserは4M
# 私有設定root・marker・depth -> F=16Ggen+Dgen+32N(本実装の非Git本文読取Dgenは0)
# 実repo各Git -> 16Gmain+Σ3Gchild+2M(F=0)、子形式検証は子refs無しの16G
# index/refコピーとselector出力 -> 独立した各200MiB。設定本文とは重ねない。
CONFIG_LIMIT=$((200 * 1024 * 1024))
CONFIG_FILE_LIMIT=$((16 * 1024 * 1024))
sat_config_add() { # $1+$2を上限+1で飽和させ、SATへ返す
  local a="$1" b="$2" cap=$((CONFIG_LIMIT + 1))
  case "$a:$b" in *[!0-9:]*|:*|*:) return 1 ;; esac
  if [ "$a" -ge "$cap" ] || [ "$b" -ge "$cap" ] || [ "$a" -gt "$((cap - b))" ]; then SAT="$cap"
  else SAT=$((a + b)); fi
}
sat_config_mul() { # $1*$2。乗算より前に桁あふれを防ぐ
  local a="$1" b="$2" cap=$((CONFIG_LIMIT + 1))
  case "$a:$b" in *[!0-9:]*|:*|*:) return 1 ;; esac
  if [ "$a" -eq 0 ] || [ "$b" -eq 0 ]; then SAT=0
  elif [ "$a" -ge "$cap" ] || [ "$b" -ge "$cap" ] || [ "$a" -gt "$((cap / b))" ]; then SAT="$cap"
  else SAT=$((a * b)); fi
}
reserve_config_bytes() { # 直接 read と Git の予定読取を同じ親シェルで数える
  sat_config_add "$CONFIG_READ_BYTES" "$1" || return 1
  CONFIG_READ_BYTES="$SAT"
  [ "$CONFIG_READ_BYTES" -le "$CONFIG_LIMIT" ] || return 1 # MUT:p
}
generated_config_cost() { # Ggen、Dgen、Nmeta → F。実入力の予約とは重ねない
  local g="$1" d="$2" n="$3" total
  sat_config_mul "$g" 16 || return 1; total="$SAT"
  sat_config_add "$total" "$d" || return 1; total="$SAT"
  sat_config_mul "$n" 32 || return 1
  sat_config_add "$total" "$SAT" || return 1
  GENERATED_COST="$SAT"
}
reserve_generated_config() {
  generated_config_cost "$1" "$2" "$3" || return 1
  reserve_config_bytes "$GENERATED_COST" # MUT:s
}
write_generated_config() { # 内容はLC_ALL=Cのbyte数で制限し、本文読取は後のFで予約する
  local target="$1" content="$2" LC_ALL=C
  [ "${#content}" -le "$CONFIG_FILE_LIMIT" ] || return 1
  printf '%s' "$content" >"$target"
}
# Git が実設定を読む前に、その include 展開の保守上界を予約する。
# 候補の集合ではなく辺の出現回数で数える。同じ include 先を18回指定すれば18回分となる。
declare -A CONFIG_NODE=() CONFIG_SIZE=()
CONFIG_ROOTS=(); CONFIG_QUEUE=(); CONFIG_QUEUE_KIND=()
CONFIG_EDGE_FROM=(); CONFIG_EDGE_TO=(); CONFIG_EDGE_KEY=()
CONFIG_GIT_COST=0
init_config_graph() {
  CONFIG_QUEUE=(); CONFIG_QUEUE_KIND=(); CONFIG_NODE=(); CONFIG_SIZE=()
  CONFIG_EDGE_FROM=(); CONFIG_EDGE_TO=(); CONFIG_EDGE_KEY=()
}
init_config_parser() {
  mkdir -p -- "$CONFIG_PARSE_CWD/repo.git/objects" "$CONFIG_PARSE_CWD/repo.git/refs" || return 1
  # scratch 自身の設定は空。親にある実リポジトリを発見させない。
  : >"$CONFIG_PARSE_CWD/repo.git/config" || return 1
  printf 'ref: refs/heads/guard\n' >"$CONFIG_PARSE_CWD/repo.git/HEAD" || return 1
}
isolated_config_git() {
  # --type=pathの ~/ 展開は元Gitと同じHOMEで行う。設定選択は下の固定値で隔離する。
  local home_env=()
  if [ -n "${HOME+x}" ]; then home_env=("HOME=$HOME"); fi # MUT:w
  env -i PATH="$PATH" LC_ALL=C ${home_env[@]+"${home_env[@]}"} \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_COUNT=0 \
    GIT_DIR="$CONFIG_PARSE_CWD/repo.git" GIT_NO_LAZY_FETCH=1 GIT_TERMINAL_PROMPT=0 \
    git "${GIT_PRE[@]}" -C "$CONFIG_PARSE_CWD" "$@" 2>/dev/null
}
probe_config_depth() { # 既知の直列fixture。上書き前後を別の生成graphとして予約する
  [ "${CONFIG_DEPTH_PROBED:-0}" -eq 0 ] || return 0
  local n rc=0 graph=0 content LC_ALL=C
  for ((n=0; n<11; n++)); do
    printf -v content '[include]\npath = depth-%s\n' "$((n + 1))"
    write_generated_config "$CONFIG_PARSE_CWD/depth-$n" "$content" || return 1
    sat_config_add "$graph" "${#content}" || return 1; graph="$SAT"
  done
  content=$'[guard]\ndepth = reached\n'
  write_generated_config "$CONFIG_PARSE_CWD/depth-11" "$content" || return 1
  sat_config_add "$graph" "${#content}" || return 1; graph="$SAT"
  reserve_generated_config "$graph" 0 0 || return 1
  GIT_USED=1
  if isolated_config_git config --file "$CONFIG_PARSE_CWD/depth-0" --includes --get guard.depth >"$WORK/depth.out"; then rc=0; else rc=$?; fi
  [ "$rc" -ne 0 ] && [ ! -s "$WORK/depth.out" ] || return 1
  # 次のgraphではdepth-11を読まず、depth-10を小さな終端へ置き換える。
  graph=0
  for ((n=0; n<10; n++)); do
    printf -v content '[include]\npath = depth-%s\n' "$((n + 1))"
    sat_config_add "$graph" "${#content}" || return 1; graph="$SAT"
  done
  content=$'[guard]\ndepth = reached\n'
  write_generated_config "$CONFIG_PARSE_CWD/depth-10" "$content" || return 1
  sat_config_add "$graph" "${#content}" || return 1
  reserve_generated_config "$SAT" 0 0 || return 1
  isolated_config_git config --file "$CONFIG_PARSE_CWD/depth-0" --includes --get guard.depth >"$WORK/depth.out" || return 1
  [ "$(cat "$WORK/depth.out")" = reached ] || return 1
  CONFIG_DEPTH_PROBED=1
}

held_config_branch() { # 実Gitを起動せず、HEADと最終symbolic-refの名前だけを保持する
  local p="$CONFIG_GITDIR/HEAD" raw ref n part rest current
  local -A seen=()
  CONFIG_BRANCH=""
  [ ! -e "$CONFIG_COMMON/reftable" ] || return 1
  for ((n=0; n<40; n++)); do
    # reftableや未知の管理形態は条件を成立側に倒す。既知の通常refだけを読む。
    if [ ! -e "$p" ] && [ ! -L "$p" ]; then
      [ "$n" -gt 0 ] || return 1
      return 0
    fi
    [ -f "$p" ] && [ ! -L "$p" ] && [ -r "$p" ] || return 1
    reserve_config_read "$p" || return 1
    raw="$(cat -- "$p")" || return 1
    case "$raw" in ref:*) ref="${raw#ref:}" ;; *) return 0 ;; esac
    while [[ "$ref" = [[:space:]]* ]]; do ref="${ref#?}"; done
    while [[ "$ref" = *[[:space:]] ]]; do ref="${ref%?}"; done
    case "$ref" in refs/heads/*) : ;; *) return 1 ;; esac
    case "$ref" in *$'\n'*|*'..'*|*'//'*|*/|*'\\'*) return 1 ;; esac
    [ -z "${seen[":$ref"]+x}" ] || return 1
    seen[":$ref"]=1; CONFIG_BRANCH="$ref"
    # 親symlinkは追わない。判断できなければonbranchを過少に見積もらない。
    rest="$ref"; current="$CONFIG_COMMON"
    while [ -n "$rest" ]; do
      part="${rest%%/*}"; if [ "$part" = "$rest" ]; then rest=""; else rest="${rest#*/}"; fi
      current="$current/$part"
      [ ! -L "$current" ] || return 1
      if [ -n "$rest" ] && [ -e "$current" ]; then [ -d "$current" ] || return 1; fi
    done
    p="$CONFIG_COMMON/$ref"
  done
  return 1
}
config_edge_active() { # keyの値はログにもstateにも書かない。判定不能な条件は常に成立側。
  local key="$1" condition escaped rc=0 content marker graph LC_ALL=C
  CONFIG_ACTIVE=1
  case "$key" in includeif.onbranch:*.path) : ;; *) return 0 ;; esac
  [ "$CONFIG_BRANCH_KNOWN" -eq 1 ] || return 0
  if [ -z "$CONFIG_BRANCH" ]; then CONFIG_ACTIVE=0; return 0; fi
  condition="${key#includeif.}"; condition="${condition%.path}"
  escaped="${condition//\\/\\\\}"; escaped="${escaped//\"/\\\"}"
  printf -v content '[includeIf "%s"]\npath = marker\n' "$escaped"
  marker=$'[guard]\ncondition = active\n'
  write_generated_config "$CONFIG_PARSE_CWD/condition" "$content" || return 1
  write_generated_config "$CONFIG_PARSE_CWD/marker" "$marker" || return 1
  sat_config_add "${#content}" "${#marker}" || return 1; graph="$SAT"
  reserve_generated_config "$graph" 0 0 || return 1
  # --file はscratchだけを読み、実include先には触れない。
  if isolated_config_git config --file "$CONFIG_PARSE_CWD/condition" --includes --get guard.condition >"$WORK/condition.out"; then rc=0; else rc=$?; fi
  case "$rc" in
    0) [ "$(cat "$WORK/condition.out")" = active ] || return 1 ;;
    1) [ ! -s "$WORK/condition.out" ] || return 1; CONFIG_ACTIVE=0 ;;
    *) return 1 ;;
  esac
}
plan_config_git() {
  local i p node from to level value total=0 cap=$((200 * 1024 * 1024 + 1)) branch_rc=0
  local -A previous=() next=() active=() condition_result=()
  probe_config_depth || return 1
  CONFIG_BRANCH_KNOWN=1
  held_config_branch || branch_rc=$?
  [ "$branch_rc" -eq 0 ] || CONFIG_BRANCH_KNOWN=0
  if [ "$CONFIG_BRANCH_KNOWN" -eq 1 ] && [ -n "$CONFIG_BRANCH" ]; then
    printf 'ref: %s\n' "$CONFIG_BRANCH" >"$CONFIG_PARSE_CWD/repo.git/HEAD" || return 1
  fi
  for ((i=0; i<${#CONFIG_EDGE_KEY[@]}; i++)); do
    p="${CONFIG_EDGE_KEY[$i]}"
    if [ -z "${condition_result[":$p"]+x}" ]; then
      config_edge_active "$p" || return 1
      condition_result[":$p"]="$CONFIG_ACTIVE"
    fi
    active[":$i"]="${condition_result[":$p"]}"
  done
  # leafから10段を動的計算。循環も有限回で止まり、桁あふれの前に飽和させる。
  for node in "${!CONFIG_SIZE[@]}"; do previous["$node"]="${CONFIG_SIZE[$node]}"; done
  for ((level=0; level<10; level++)); do
    next=()
    for node in "${!CONFIG_SIZE[@]}"; do next["$node"]="${CONFIG_SIZE[$node]}"; done
    for ((i=0; i<${#CONFIG_EDGE_FROM[@]}; i++)); do
      [ "${active[":$i"]}" -eq 1 ] || continue
      from=":${CONFIG_EDGE_FROM[$i]}"; p="${CONFIG_EDGE_TO[$i]}"; to="${CONFIG_NODE[":$p"]:-}"
      [ -n "$to" ] || continue
      value=$(( ${next[$from]} + ${previous[":$to"]} ))
      [ "$value" -le "$cap" ] || value="$cap"
      next["$from"]="$value"
    done
    previous=()
    for node in "${!next[@]}"; do previous["$node"]="${next[$node]}"; done
  done
  for p in "${CONFIG_ROOTS[@]}"; do
    node="${CONFIG_NODE[":$p"]:-}"; [ -n "$node" ] || continue
    total=$((total + ${previous[":$node"]}))
    [ "$total" -le "$cap" ] || total="$cap"
  done
  # 起動時trace・保護設定・trace対象設定・refs初期化cache・明示取得の5走査は
  # hasconfigの全条件成立側の再走査を各1回含む。setupのroot直接読取も足し、
  # root展開16回分を保守予約する(Git 2.51ソースと実測による根拠は外部ランナー契約)。
  CONFIG_GRAPH_COST="$total"
  sat_config_mul "$total" 16 || return 1; CONFIG_GIT_COST="$SAT"
  return 0
}
reserve_config_git() { # 呼出群を始める前に、全呼出分を親シェルで先に予約する
  sat_config_mul "$CONFIG_GIT_COST" "$1" || return 1
  reserve_config_bytes "$SAT" || return 1 # MUT:r
  return 0
}
load_config_plan_roots() { # snapshot照合済みの計画メタを読む。事前検査の副作用には依存しない。
  local line p admins=0 roots=0
  CONFIG_ROOTS=()
  while IFS= read -r line; do
    case "$line" in
      plan-admin$'\t'*)
        admins=$((admins + 1)); [ "$admins" -eq 1 ] || return 1
        p="${line#*$'\t'}"; field_value "${p%%$'\t'*}"; CONFIG_GITDIR="$UNQ"
        field_value "${p#*$'\t'}"; CONFIG_COMMON="$UNQ"
        case "$CONFIG_GITDIR" in /*) : ;; *) return 1 ;; esac
        case "$CONFIG_COMMON" in /*) : ;; *) return 1 ;; esac ;;
      plan-root$'\t'*)
        roots=$((roots + 1)); [ "$roots" -le 2000 ] || return 1
        field_value "${line#*$'\t'}"
        case "$UNQ" in /*) : ;; *) return 1 ;; esac
        CONFIG_ROOTS[${#CONFIG_ROOTS[@]}]="$UNQ" ;;
    esac
  done <"$STATE/snapshot/config-origins.txt"
  [ "$admins" -eq 1 ] && [ "$roots" -gt 0 ]
}
rebuild_config_plan() { # 事前照合済みのrootから、隔離parserだけで現在のbranch用計画を作り直す
  local n=0 p kind
  local -A CONFIG_SEEN=() CONFIG_ORIGIN_SEEN=() CONFIG_PARSED=()
  load_config_plan_roots || return 1
  init_config_graph
  CONFIG_RECORD="$WORK/config-plan-record"; : >"$CONFIG_RECORD" || return 1
  ORIGIN_MODE=take; CONFIG_PARSE_CWD="$WORK/config-parse"
  CONFIG_PARSE_BYTES=0; CONFIG_VALUES=0; CONFIG_PARSE_SEQ=0
  init_config_parser || return 1
  for p in "${CTX_ROOT_PATHS[@]}"; do enqueue_candidate "$p" || return 1; done
  while [ "$n" -lt "${#CONFIG_QUEUE[@]}" ]; do
    p="${CONFIG_QUEUE[$n]}"; n=$((n + 1)); CAND_KIND=
    candidate_walk "$p" || return 1
    if [ "$CAND_KIND" = file ]; then parse_candidate_includes "$CAND_LOGICAL" "$CAND_HAD_LINK" || return 1; fi
  done
  plan_config_git
}

# ── 使用先の隔離取得。通常設定の照合が済むまでこの層のGitも呼ばない ──
CTX_WT=(); CTX_GD=(); CTX_COMMON=(); CTX_PARENT=(); CTX_DEPTH=(); CTX_FORMAT=(); CTX_REFS=(); CTX_HELD_HEAD=(); CTX_HEAD=(); CTX_MSIZE=(); CTX_MSOURCE=(); CTX_MOID=(); CTX_MHASH=(); CTX_MAIN_COST=(); CTX_ROOT_PATHS=()
declare -A CTX_BY_WT=() CTX_ROOT_LIST=() CTX_ROOT_SEEN=() CTX_PAIR_COST=() CTX_CHILDREN=() CTX_HELD_BLOB=()
SELECT_COPY_BYTES=0; SELECT_OUTPUT_BYTES=0; SELECT_PATH_COUNT=0; SELECT_SEQ=0
SELECT_MODE=take; SELECT_PRIVATE=; SELECT_CURRENT=0; SELECT_OBJECTS=; SELECT_GENERATED=0
context_reset() {
  CTX_WT=(); CTX_GD=(); CTX_COMMON=(); CTX_PARENT=(); CTX_DEPTH=(); CTX_FORMAT=(); CTX_REFS=(); CTX_HELD_HEAD=(); CTX_HEAD=(); CTX_MSIZE=(); CTX_MSOURCE=(); CTX_MOID=(); CTX_MHASH=(); CTX_MAIN_COST=(); CTX_ROOT_PATHS=()
  CTX_ACTIVE=(); CTX_BY_WT=(); CTX_ROOT_LIST=(); CTX_ROOT_SEEN=(); CTX_PAIR_COST=(); CTX_CHILDREN=(); CTX_HELD_BLOB=()
  SELECT_COPY_BYTES=0; SELECT_OUTPUT_BYTES=0; SELECT_PATH_COUNT=0; SELECT_SEQ=0
}
selector_charge_path() {
  SELECT_PATH_COUNT=$((SELECT_PATH_COUNT + 1))
  [ "$SELECT_PATH_COUNT" -le 2000 ]
}
selector_charge_copy() {
  local n="$1"
  [ "$n" -le "$((CONFIG_LIMIT - SELECT_COPY_BYTES))" ] || return 1
  SELECT_COPY_BYTES=$((SELECT_COPY_BYTES + n))
}
selector_copy_file() { # 観察対象をコピーする。設定の不変候補へは混ぜない
  local src="$1" dst="$2" n
  [ -f "$src" ] && [ -r "$src" ] || return 1
  n="$(config_target_size "$src" 2>/dev/null)" || return 1
  selector_charge_copy "$n" || return 1
  cp -L -- "$src" "$dst" 2>/dev/null
}
selector_copy_tree() { # refs/reftableの観察用コピー。深さ・件数・byteを別枠で制限する
  local src="$1" dst="$2" depth="$3" p name
  [ "$depth" -le 40 ] || return 1
  [ -d "$src" ] && [ -r "$src" ] && [ -x "$src" ] || return 1
  mkdir -p -- "$dst" || return 1
  for p in "$src"/* "$src"/.[!.]* "$src"/..?*; do
    [ -e "$p" ] || { [ ! -L "$p" ] || return 1; continue; }
    name="${p##*/}"
    if [ -d "$p" ]; then selector_copy_tree "$p" "$dst/$name" "$((depth + 1))" || return 1
    else selector_copy_file "$p" "$dst/$name" || return 1; fi
  done
}
selector_git_raw() { # envを白紙にして元repoの設定・trace・fetchを選ばせない
  env -i PATH="$PATH" LC_ALL=C HOME="$WORK/selector-home" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_COUNT=0 \
    GIT_DIR="$SELECT_PRIVATE" GIT_COMMON_DIR="$SELECT_PRIVATE" GIT_INDEX_FILE="$SELECT_PRIVATE/index" \
    GIT_OBJECT_DIRECTORY="$SELECT_OBJECTS" GIT_ALTERNATE_OBJECT_DIRECTORIES="${SELECT_ALTERNATES:-}" GIT_NO_LAZY_FETCH=1 GIT_TERMINAL_PROMPT=0 GIT_OPTIONAL_LOCKS=0 \
    git "${GIT_PRE[@]}" -C "${CTX_WT[$SELECT_CURRENT]}" "$@"
}
selector_git() { # 出力は独立した200MiB枠。Fだけをここで1回予約する
  local out="$1" meta="$2" rc=0 size remaining
  shift 2
  reserve_generated_config "$SELECT_GENERATED" 0 "$meta" || return 125
  remaining=$((CONFIG_LIMIT - SELECT_OUTPUT_BYTES)); [ "$remaining" -ge 0 ] || return 125
  GIT_USED=1
  bounded_config_stdout "$out" "$remaining" selector "$@" || rc=$?
  size="$(stat_size "$out" 2>/dev/null)" || return 125
  [ "$size" -le "$remaining" ] || return 125
  SELECT_OUTPUT_BYTES=$((SELECT_OUTPUT_BYTES + size))
  return "$rc"
}
context_config_value() { # 既に候補検査したrootをincludeなしで読む
  local file="$1" key="$2" rc=0
  CONFIG_VALUE=
  [ -e "$file" ] || return 0
  reserve_config_read "$file" || return 1
  if bounded_config_parse "$WORK/context-value.z" config --file "$file" --no-includes --null --get "$key"; then rc=0; else rc=$?; fi
  if [ "$rc" -eq 1 ] && [ ! -s "$WORK/context-value.z" ]; then return 0; fi
  [ "$rc" -eq 0 ] || return 1
  IFS= read -r -d '' CONFIG_VALUE <"$WORK/context-value.z" || return 1
}
selector_prepare() { # 管理pathは保持済み。HEAD/ref/indexは現在値をコピーする
  local id="$1" gd common p content hashfmt refs size LC_ALL=C
  gd="${CTX_GD[$id]}"; common="${CTX_COMMON[$id]}"
  SELECT_CURRENT="$id"; SELECT_SEQ=$((SELECT_SEQ + 1)); SELECT_PRIVATE="$WORK/selector-$SELECT_SEQ.git"
  SELECT_OBJECTS="$common/objects"; SELECT_ALTERNATES=
  if [ "$id" -eq 0 ]; then
    if [ -n "${GIT_OBJECT_DIRECTORY:-}" ]; then
      case "$GIT_OBJECT_DIRECTORY" in /*) SELECT_OBJECTS="$GIT_OBJECT_DIRECTORY" ;; *) SELECT_OBJECTS="$TOP/$GIT_OBJECT_DIRECTORY" ;; esac
    fi
    SELECT_ALTERNATES="${GIT_ALTERNATE_OBJECT_DIRECTORIES:-}"
  fi
  mkdir -p -- "$SELECT_PRIVATE/objects" "$SELECT_PRIVATE/refs" "$WORK/selector-home" || return 1
  hashfmt="${CTX_FORMAT[$id]}"; refs="${CTX_REFS[$id]}"
  content=
  if [ "$hashfmt" = sha256 ] || [ "$refs" = reftable ]; then
    content=$'[core]\nrepositoryformatversion = 1\n[extensions]\n'
    if [ "$hashfmt" = sha256 ]; then content+=$'objectformat = sha256\n'; fi
    if [ "$refs" = reftable ]; then content+=$'refstorage = reftable\n'; fi
  fi
  write_generated_config "$SELECT_PRIVATE/config" "$content" || return 1
  SELECT_GENERATED="${#content}"
  selector_copy_file "$gd/HEAD" "$SELECT_PRIVATE/HEAD" || return 1
  if [ -e "$gd/index" ] || [ -L "$gd/index" ]; then selector_copy_file "$gd/index" "$SELECT_PRIVATE/index" || return 1; fi
  for p in "$gd"/sharedindex.*; do
    [ -e "$p" ] || { [ ! -L "$p" ] || return 1; continue; }
    selector_copy_file "$p" "$SELECT_PRIVATE/${p##*/}" || return 1
  done
  if [ -d "$common/refs" ]; then selector_copy_tree "$common/refs" "$SELECT_PRIVATE/refs" 0 || return 1; fi
  if [ "$gd" != "$common" ] && [ -d "$gd/refs" ]; then selector_copy_tree "$gd/refs" "$SELECT_PRIVATE/refs" 0 || return 1; fi
  if [ -e "$common/packed-refs" ] || [ -L "$common/packed-refs" ]; then selector_copy_file "$common/packed-refs" "$SELECT_PRIVATE/packed-refs" || return 1; fi
  if [ "$refs" = reftable ]; then selector_copy_tree "$common/reftable" "$SELECT_PRIVATE/reftable" 0 || return 1; fi
}
context_root() { # $1=context-pair $2=候補root。記録用の整数一覧と探索queueを更新する
  local pair="$1" p="$2" n
  [ -z "${CTX_ROOT_SEEN["$pair:$p"]+x}" ] || return 0
  CTX_ROOT_SEEN["$pair:$p"]=1
  n="${#CTX_ROOT_PATHS[@]}"; CTX_ROOT_PATHS[$n]="$p"
  CTX_ROOT_LIST["$pair"]="${CTX_ROOT_LIST["$pair"]:-} $n"
  enqueue_candidate "$p"
}
context_env_root() { # 相対環境pathはそのGit processのcwdへ結び付ける
  local pair="$1" cwd="$2" p="$3"
  [ -n "$p" ] && [ "$p" != /dev/null ] || return 0
  case "$p" in /*) : ;; *) p="$cwd/$p" ;; esac
  context_root "$pair" "$p"
}
context_roots() { # $1=repo id $2=process cwd id。暗黙の子ref読取も別文脈にする
  local id="$1" owner="$2" pair="$1:$2" cwd="${CTX_WT[$2]}" gd="${CTX_GD[$1]}" common="${CTX_COMMON[$1]}"
  context_root "$pair" "$gd/config" || return 1
  context_root "$pair" "$gd/config.worktree" || return 1
  context_root "$pair" "$common/config" || return 1
  context_root "$pair" "$common/config.worktree" || return 1
  if [ -n "${GIT_CONFIG_GLOBAL+x}" ]; then context_env_root "$pair" "$cwd" "$GIT_CONFIG_GLOBAL" || return 1
  else
    if [ -n "${HOME+x}" ]; then context_env_root "$pair" "$cwd" "$HOME/.gitconfig" || return 1; fi
    if [ -n "${XDG_CONFIG_HOME:-}" ]; then context_env_root "$pair" "$cwd" "$XDG_CONFIG_HOME/git/config" || return 1
    elif [ -n "${HOME+x}" ]; then context_env_root "$pair" "$cwd" "$HOME/.config/git/config" || return 1; fi
  fi
  if [ "${CONFIG_SYSTEM_ENABLED:-0}" -eq 1 ]; then
    context_env_root "$pair" "$cwd" "$CONFIG_SYSTEM_SELECTED" || return 1
    context_env_root "$pair" "$cwd" "${GIT_CONFIG_SYSTEM:-}" || return 1
  fi
  context_env_root "$pair" "$cwd" "${GIT_CONFIG:-}"
}
drain_context_candidates() { # collect中の動的scopeの候補集合を使い、追加された分だけ辿る
  local p kind n
  while [ "$CONFIG_DRAINED" -lt "${#CONFIG_QUEUE[@]}" ]; do
    n="$CONFIG_DRAINED"; CONFIG_DRAINED=$((CONFIG_DRAINED + 1))
    p="${CONFIG_QUEUE[$n]}"; kind="${CONFIG_QUEUE_KIND[$n]}"
    printf 'candidate\t%s\n' "$(path_field "$p")" >>"$CONFIG_RECORD" || return 1
    CAND_KIND=; CAND_LOGICAL=; CAND_REAL=; CAND_PARSE_FILE=0
    [ "$kind" = admin ] || CAND_PARSE_FILE=1
    candidate_walk "$p" || return 1
    if [ "$kind" != admin ] && [ "$CAND_KIND" = file ]; then parse_candidate_includes "$CAND_LOGICAL" "$CAND_HAD_LINK" || return 1; fi
  done
}
context_format() {
  local id="$1" root="${CTX_COMMON[$1]}/config"
  context_config_value "$root" extensions.objectformat || return 1
  case "$CONFIG_VALUE" in ''|sha1) CTX_FORMAT[$id]=sha1 ;; sha256) CTX_FORMAT[$id]=sha256 ;; *) return 1 ;; esac
  context_config_value "$root" extensions.refstorage || return 1
  case "$CONFIG_VALUE" in ''|files) CTX_REFS[$id]=files ;; reftable) CTX_REFS[$id]=reftable ;; *) return 1 ;; esac
}
CTX_ACTIVE=()
declare -A MODULE_NAME=()
context_admin_read() { # 候補として保持してから、管理入口の字面を読む
  local p="$1"
  enqueue_admin_candidate "$p" || return 1
  drain_context_candidates || return 1
  ADMIN_TEXT=
  [ -e "$p" ] || return 0
  reserve_config_read "$p" || return 1
  ADMIN_TEXT="$(cat -- "$p")" || return 1
}
context_resolve_child() { # $1=parent $2=worktree $3=.gitmodulesのname。失敗した直接入口も保持する
  local parent="$1" wt="$2" name="$3" entry="$2/.git" raw gd common
  CHILD_GD=; CHILD_COMMON=; CHILD_ACTIVE=0
  enqueue_admin_entry "$entry" || return 1
  drain_context_candidates || return 1
  if [ -d "$entry" ]; then gd="$entry"; CHILD_ACTIVE=1
  elif [ -f "$entry" ]; then
    reserve_config_read "$entry" || return 1; raw="$(cat -- "$entry")" || return 1
    case "$raw" in 'gitdir: '*) gd="${raw#gitdir: }" ;; *) return 1 ;; esac
    [ -n "$gd" ] && [[ "$gd" != *$'\n'* ]] || return 1
    case "$gd" in /*) : ;; *) gd="$wt/$gd" ;; esac
    CHILD_ACTIVE=1
  else
    [ -n "$name" ] || return 0
    case "/$name/" in */../*|*'\'*) return 1 ;; esac
    gd="${CTX_COMMON[$parent]}/modules/$name"
  fi
  enqueue_admin_entry "$gd" || return 1
  drain_context_candidates || return 1
  if [ -d "$gd" ]; then real_dir "$gd" || return 1; gd="$RP"
  elif [ -e "$gd" ] || [ -L "$gd" ] || [ "$CHILD_ACTIVE" -eq 1 ]; then return 1; fi
  context_admin_read "$gd/commondir" || return 1
  common="$gd"
  if [ -n "$ADMIN_TEXT" ]; then
    case "$ADMIN_TEXT" in *$'\n'*) return 1 ;; /*) common="$ADMIN_TEXT" ;; *) common="$gd/$ADMIN_TEXT" ;; esac
    enqueue_admin_entry "$common" || return 1
    drain_context_candidates || return 1
    real_dir "$common" || return 1; common="$RP"
  fi
  CHILD_GD="$gd"; CHILD_COMMON="$common"
}
context_modules() { # selector stage/treeの控えを入力に、Gitと同じ優先順で設定sourceを選ぶ
  local id="$1" stage_file="$2" tree_file="$3" file="${CTX_WT[$1]}/.gitmodules" row meta path mode oid stage unmerged=0 index_oid= head_oid= source= size=0 hash= rc=0 text key value name oldsource oldoid oldhash
  oldsource="${CTX_MSOURCE[$id]:-none}"; oldoid="${CTX_MOID[$id]:--}"; oldhash="${CTX_MHASH[$id]:--}"
  MODULE_NAME=()
  while IFS= read -r -d '' row; do
    meta="${row%%$'\t'*}"; path="${row#*$'\t'}"
    [ "$path" = .gitmodules ] || continue
    read -r mode oid stage <<<"$meta"
    if [ "$stage" != 0 ]; then unmerged=1; else index_oid="$oid"; fi
  done <"$stage_file"
  if [ "$unmerged" -eq 1 ]; then
    CTX_MSIZE[$id]=0; CTX_MSOURCE[$id]=unmerged; CTX_MOID[$id]=-; CTX_MHASH[$id]=-
    return 0
  fi
  if [ -e "$file" ] || [ -L "$file" ]; then source=file
  elif [ -n "$index_oid" ]; then source=blob; oid="$index_oid"
  else
    while IFS= read -r -d '' row; do
      path="${row#*$'\t'}"; [ "$path" = .gitmodules ] || continue
      meta="${row%%$'\t'*}"; read -r mode text head_oid <<<"$meta"
    done <"$tree_file"
    if [ -n "$head_oid" ]; then source=blob; oid="$head_oid"; else source=none; fi
  fi
  if [ "$source" = none ]; then CTX_MSIZE[$id]=0; CTX_MSOURCE[$id]=none; CTX_MOID[$id]=-; CTX_MHASH[$id]=-; return 0; fi
  if [ "$source" = blob ]; then
    if [ "$SELECT_MODE" = compare ] && { [ "$oldsource" != blob ] || [ "$oldoid" != "$oid" ]; }; then ORIGIN_CHANGED="${CTX_WT[$id]}/.gitmodules"; return 1; fi
    # literal OIDだけを渡す。--batch-checkは設定本文を実体化しない。
    case "$oid" in *[!0-9a-f]*|'') return 1 ;; esac
    selector_git "$WORK/blob-type" 1 cat-file -t "$oid" || return 1
    [ "$(cat "$WORK/blob-type")" = blob ] || return 1
    selector_git "$WORK/blob-size" 1 cat-file -s "$oid" || return 1
    size="$(cat "$WORK/blob-size")"; case "$size" in ''|*[!0-9]*) return 1 ;; esac
    [ "${#size}" -le "${#CONFIG_FILE_LIMIT}" ] && [ "$size" -le "$CONFIG_FILE_LIMIT" ] || return 1
    sat_config_mul "$size" 4 || return 1
    reserve_config_bytes "$SAT" || return 1
    file="$WORK/module-$id.config"
    selector_git "$file" 0 cat-file blob "$oid" || return 1
    [ "$(stat_size "$file")" = "$size" ] || return 1
    hash="$(sha256_file "$file")" || return 1
    if [ "$SELECT_MODE" = compare ] && [ "$oldhash" != "$hash" ]; then ORIGIN_CHANGED="${CTX_WT[$id]}/.gitmodules"; return 1; fi
  else
    [ -f "$file" ] && [ -r "$file" ] || return 1
    size="$(config_target_size "$file")" || return 1
    reserve_config_read "$file" || return 1; hash="$(sha256_file "$file")" || return 1
    reserve_config_read "$file" || return 1
    oid=-
  fi
  rc=0
  if bounded_config_parse "$WORK/module-paths.z" config --file "$file" --no-includes --null --get-regexp '^submodule\..*\.path$'; then rc=0; else rc=$?; fi
  if [ "$rc" -ne 0 ]; then [ "$rc" -eq 1 ] && [ ! -s "$WORK/module-paths.z" ] || return 1; fi
  while IFS= read -r -d '' row; do
    case "$row" in *$'\n'*) : ;; *) return 1 ;; esac
    key="${row%%$'\n'*}"; value="${row#*$'\n'}"
    name="${key#submodule.}"; name="${name%.path}"
    [ -n "$name" ] && [ -n "$value" ] || return 1
    case "/$name/" in */../*|*'\'*) return 1 ;; esac
    MODULE_NAME[":$value"]="$name"
  done <"$WORK/module-paths.z"
  CTX_MSIZE[$id]="$size"; CTX_MSOURCE[$id]="$source"; CTX_MOID[$id]="$oid"; CTX_MHASH[$id]="$hash"
}
context_new() { # $1=worktree $2=gitdir $3=common $4=parent $5=active
  local id="${#CTX_WT[@]}" depth=0
  [ "$id" -lt 2000 ] || return 1
  if [ "$4" -ge 0 ]; then depth=$(( ${CTX_DEPTH[$4]} + 1 )); fi
  [ "$depth" -le 40 ] || return 1
  CTX_WT[$id]="$1"; CTX_GD[$id]="$2"; CTX_COMMON[$id]="$3"; CTX_PARENT[$id]="$4"; CTX_ACTIVE[$id]="$5"; CTX_DEPTH[$id]="$depth"
  CTX_FORMAT[$id]=sha1; CTX_REFS[$id]=files; CTX_HELD_HEAD[$id]=-; CTX_HEAD[$id]=-; CTX_MSIZE[$id]=0; CTX_MSOURCE[$id]=none; CTX_MOID[$id]=-; CTX_MHASH[$id]=-
  CTX_BY_WT[":$1"]="$id"; NEW_CONTEXT="$id"
}
context_select() { # index/current+held HEADから使用pathを列挙する。追加rootは採用しない
  local id="$1" row path meta mode oid stage head rc=0 tree out held wt child name i
  local -A paths=() gitlinks=()
  [ "${CTX_ACTIVE[$id]}" -eq 1 ] || return 0
  selector_prepare "$id" || return 1
  out="$WORK/context-$id-stage.z"
  selector_git "$out" 0 ls-files --stage -z || return 1
  rc=0
  if selector_git "$WORK/context-$id-head" 0 rev-parse --verify --quiet HEAD; then rc=0; else rc=$?; fi
  case "$rc" in 0) head="$(cat "$WORK/context-$id-head")" ;; 1) head=- ;; *) return 1 ;; esac
  case "$head" in -) : ;; *[!0-9a-f]*|'') return 1 ;; esac
  CTX_HEAD[$id]="$head"
  [ "$SELECT_MODE" != take ] || CTX_HELD_HEAD[$id]="$head"
  tree="$WORK/context-$id-tree.z"; : >"$tree"
  if [ "$head" != - ]; then selector_git "$tree" 0 ls-tree -r -z "$head" || return 1; fi
  while IFS= read -r -d '' row; do
    case "$row" in *$'\t'*) : ;; *) return 1 ;; esac
    meta="${row%%$'\t'*}"; path="${row#*$'\t'}"; read -r mode oid stage <<<"$meta"
    paths[":$path"]="$path"; if [ "$mode" = 160000 ]; then gitlinks[":$path"]=1; fi
  done <"$out"
  for i in current held; do
    if [ "$i" = current ]; then out="$tree"
    else
      held="${CTX_HELD_HEAD[$id]}"; [ "$held" != - ] && [ "$held" != "$head" ] || continue
      out="$WORK/context-$id-held-tree.z"; selector_git "$out" 0 ls-tree -r -z "$held" || return 1
    fi
    while IFS= read -r -d '' row; do
      case "$row" in *$'\t'*) : ;; *) return 1 ;; esac
      meta="${row%%$'\t'*}"; path="${row#*$'\t'}"; read -r mode stage oid <<<"$meta"
      paths[":$path"]="$path"; if [ "$mode" = 160000 ]; then gitlinks[":$path"]=1; fi
    done <"$out"
  done
  # worktree sourceの内容は通常候補へ入れ、開始後の変更を全Git前に検出する。
  if [ "$SELECT_MODE" = take ]; then enqueue_admin_candidate "${CTX_WT[$id]}/.gitmodules" || return 1; drain_context_candidates || return 1; fi
  context_modules "$id" "$WORK/context-$id-stage.z" "$tree" || return 1
  CTX_CHILDREN[$id]=
  for i in "${!paths[@]}"; do
    path="${paths[$i]}"
    case "$path" in ''|/*|../*|*/../*|*/..|./*|*/./*) return 1 ;; esac
    wt="${CTX_WT[$id]}/$path"
    if [ -z "${gitlinks[":$path"]+x}" ]; then
      [ -d "$wt" ] && { [ -e "$wt/.git" ] || [ -L "$wt/.git" ]; } || continue
    fi
    selector_charge_path || return 1
    child="${CTX_BY_WT[":$wt"]:-}"
    if [ -z "$child" ]; then
      if [ "$SELECT_MODE" = compare ]; then ORIGIN_CHANGED="$wt/.git"; return 1; fi # MUT:t
      name="${MODULE_NAME[":$path"]:-}"
      context_resolve_child "$id" "$wt" "$name" || return 1
      context_new "$wt" "$CHILD_GD" "$CHILD_COMMON" "$id" "$CHILD_ACTIVE" || return 1
      child="$NEW_CONTEXT"
      if [ -n "$CHILD_GD" ]; then
        context_roots "$child" "$child" || return 1
        context_roots "$child" "$id" || return 1
        drain_context_candidates || return 1
        if [ -e "${CTX_COMMON[$child]}/config" ]; then context_format "$child" || return 1; fi
      fi
    fi
    CTX_CHILDREN[$id]="${CTX_CHILDREN[$id]} $child"
  done
}
context_save() {
  local out="$1" id pair n
  printf 'context-plan-v1\n' >"$out" || return 1
  for ((id=0; id<${#CTX_WT[@]}; id++)); do
    printf 'context\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$id" "${CTX_PARENT[$id]}" "${CTX_DEPTH[$id]}" "${CTX_ACTIVE[$id]}" \
      "$(path_field "${CTX_WT[$id]}")" "$(path_field "${CTX_GD[$id]:--}")" "$(path_field "${CTX_COMMON[$id]:--}")" \
      "${CTX_FORMAT[$id]}" "${CTX_REFS[$id]}" "${CTX_HELD_HEAD[$id]}" "${CTX_MSIZE[$id]}" \
      "${CTX_MSOURCE[$id]}" "${CTX_MOID[$id]}" "${CTX_MHASH[$id]}" >>"$out" || return 1
  done
  for pair in "${!CTX_ROOT_LIST[@]}"; do
    for n in ${CTX_ROOT_LIST[$pair]}; do
      printf 'root\t%s\t%s\n' "$pair" "$(path_field "${CTX_ROOT_PATHS[$n]}")" >>"$out" || return 1
    done
  done
}
context_load() {
  local file="$STATE/snapshot/config-contexts.txt" line tag id parent depth active wt gd common fmt refs head size source oid hash pair p n
  [ -f "$file" ] && [ ! -L "$file" ] || return 2
  context_reset
  IFS= read -r line <"$file"; [ "$line" = context-plan-v1 ] || return 2
  while IFS=$'\t' read -r tag id parent depth active wt gd common fmt refs head size source oid hash; do
    case "$tag" in
      context-plan-v1) continue ;;
      context)
        [ "$id" = "${#CTX_WT[@]}" ] || return 1
        case "$id:$depth:$active:$size" in *[!0-9:]*|*::*|:*|*:) return 1 ;; esac
        [ "$id" -lt 2000 ] && [ "$depth" -le 40 ] && [ "$active" -le 1 ] && [ "$size" -le "$CONFIG_FILE_LIMIT" ] || return 1
        case "$parent" in -1) [ "$id" = 0 ] || return 1 ;; *[!0-9]*|'') return 1 ;; *) [ "$parent" -lt "$id" ] || return 1 ;; esac
        field_value "$wt"; wt="$UNQ"; field_value "$gd"; gd="$UNQ"; field_value "$common"; common="$UNQ"
        case "$wt" in /*) : ;; *) return 1 ;; esac
        [ "$gd" != - ] || gd=; [ "$common" != - ] || common=
        context_new "$wt" "$gd" "$common" "$parent" "$active" || return 1
        case "$fmt:$refs" in sha1:files|sha256:files|sha1:reftable|sha256:reftable) : ;; *) return 1 ;; esac
        CTX_FORMAT[$id]="$fmt"; CTX_REFS[$id]="$refs"; CTX_HELD_HEAD[$id]="$head"; CTX_MSIZE[$id]="$size"; CTX_MSOURCE[$id]="$source"; CTX_MOID[$id]="$oid"; CTX_MHASH[$id]="$hash" ;;
      root)
        pair="$id"; field_value "$parent"; p="$UNQ"
        case "$pair" in *[!0-9:]*|:*|*:) return 1 ;; esac
        case "$p" in /*) : ;; *) return 1 ;; esac
        n="${#CTX_ROOT_PATHS[@]}"; CTX_ROOT_PATHS[$n]="$p"; CTX_ROOT_LIST[$pair]="${CTX_ROOT_LIST[$pair]:-} $n" ;;
      *) return 1 ;;
    esac
  done <"$file"
  [ "${#CTX_WT[@]}" -gt 0 ] && [ "${CTX_WT[0]}" = "$TOP" ]
}
context_plan_costs() { # graphを共有し、root集合とbranchを文脈ごとに選ぶ
  local id owner pair n cost total child keep_gd="$CONFIG_GITDIR" keep_common="$CONFIG_COMMON"
  local original_roots=("${CONFIG_ROOTS[@]}")
  for pair in "${!CTX_ROOT_LIST[@]}"; do
    id="${pair%%:*}"; owner="${pair#*:}"
    CONFIG_ROOTS=()
    for n in ${CTX_ROOT_LIST[$pair]}; do CONFIG_ROOTS[${#CONFIG_ROOTS[@]}]="${CTX_ROOT_PATHS[$n]}"; done
    CONFIG_GITDIR="${CTX_GD[$id]}"; CONFIG_COMMON="${CTX_COMMON[$id]}"
    plan_config_git || return 1; cost="$CONFIG_GRAPH_COST"
    if [ "$id" != "$owner" ]; then
      # 旧版のonbranchは親repoを参照する。両文脈の上界を採り版文字列へ依存しない。
      CONFIG_GITDIR="${CTX_GD[$owner]}"; CONFIG_COMMON="${CTX_COMMON[$owner]}"
      plan_config_git || return 1
      [ "$cost" -ge "$CONFIG_GRAPH_COST" ] || cost="$CONFIG_GRAPH_COST"
    fi
    CTX_PAIR_COST[$pair]="$cost"
  done
  for ((id=0; id<${#CTX_WT[@]}; id++)); do
    cost="${CTX_PAIR_COST["$id:$id"]:-0}"
    sat_config_mul "$cost" 16 || return 1; total="$SAT"
    for child in ${CTX_CHILDREN[$id]:-}; do
      [ -n "${CTX_GD[$child]}" ] && [ -d "${CTX_GD[$child]}" ] || continue
      sat_config_mul "${CTX_PAIR_COST["$child:$id"]:-0}" 3 || return 1
      sat_config_add "$total" "$SAT" || return 1; total="$SAT" # MUT:v
    done
    sat_config_mul "${CTX_MSIZE[$id]}" 2 || return 1
    sat_config_add "$total" "$SAT" || return 1
    CTX_MAIN_COST[$id]="$SAT"
  done
  CONFIG_ROOTS=("${original_roots[@]}"); CONFIG_GITDIR="$keep_gd"; CONFIG_COMMON="$keep_common"
  CONFIG_GIT_COST="${CTX_MAIN_COST[0]}"
}
context_reserve_command() { # 単一real command総額。F=0の実repo呼出しにも同じ共有予算
  reserve_config_bytes "${CTX_MAIN_COST[$1]}" # MUT:u
}
context_validate_formats() { # 成功cacheの3G式を使う前に単一repoのsetupを確かめる
  local id
  for ((id=1; id<${#CTX_WT[@]}; id++)); do
    [ -n "${CTX_GD[$id]}" ] && [ -d "${CTX_GD[$id]}" ] || continue
    # このrev-parseは子refsを触らない。親builtin用の3G項を付けず16Gだけ予約する。
    sat_config_add "${CTX_PAIR_COST["$id:$id"]:-0}" "${CTX_PAIR_COST["$id:${CTX_PARENT[$id]}"]:-0}" || return 1
    sat_config_mul "$SAT" 16 || return 1
    reserve_config_bytes "$SAT" || return 1
    GIT_USED=1
    env -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES git "${GIT_PRE[@]}" --git-dir="${CTX_GD[$id]}" -C "${CTX_WT[${CTX_PARENT[$id]}]}" rev-parse --is-bare-repository >"$WORK/context-format" 2>/dev/null || return 1
    case "$(cat "$WORK/context-format")" in true|false) : ;; *) return 1 ;; esac
  done
}
collect_context_plan() {
  local id n p main_roots=("${CONFIG_ROOTS[@]}")
  context_reset; SELECT_MODE=take
  context_new "$TOP" "$CONFIG_GITDIR" "$CONFIG_COMMON" -1 1 || return 1
  for p in "${main_roots[@]}"; do context_root 0:0 "$p" || return 1; done
  context_format 0 || return 1
  id=0
  while [ "$id" -lt "${#CTX_WT[@]}" ]; do context_select "$id" || return 1; id=$((id + 1)); done
  context_plan_costs || return 1
  context_validate_formats || return 1
  context_save "$NEW_STATE/snapshot/config-contexts.txt"
}
check_context_plan() { # 既知通常設定は呼出前にGit0で照合済み。ここからは隔離Gitだけ。
  local id rc=0
  context_load || return $?
  SELECT_MODE=compare
  CONFIG_PARSE_CWD="$WORK/config-parse"; init_config_parser || return 1
  CONFIG_PARSE_BYTES=0; CONFIG_PARSE_SEQ=0
  for ((id=0; id<${#CTX_WT[@]}; id++)); do context_select "$id" || return 1; done
  rebuild_config_plan || return 1
  context_plan_costs || return 1
  # config-checkは元repo Gitを起動しない。形式検証はtake時に済み、通常設定は不変。
  return 0
}
context_real_git() { # 子repoのODB環境は親から持ち込まない
  local id="$1"; shift
  env -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES \
    git "${GIT_PRE[@]}" -C "${CTX_WT[$id]}" "$@"
}
collect_context_snapshots() { # 親から隠したdirty情報を子自身の観測で補う
  local out="$1" id base rc file hash part
  : >"$out/6-contexts.txt" || return 1
  for ((id=1; id<${#CTX_WT[@]}; id++)); do
    [ "${CTX_ACTIVE[$id]}" -eq 1 ] || continue
    printf 'context\t%s\n' "$(path_field "${CTX_WT[$id]}")" >>"$out/6-contexts.txt"
    printf 'head\t%s\n' "${CTX_HEAD[$id]}" >>"$out/6-contexts.txt"
    # stage別OIDは隔離indexから取得済み。同じMM状態のstaged A→Bも検出する。
    hash="$(sha256_file "$WORK/context-$id-stage.z")" || return 1
    printf 'index\t%s\n' "$hash" >>"$out/6-contexts.txt"
    base="${CTX_HEAD[$id]}"
    if [ "$base" = - ]; then
      context_reserve_command "$id" || return 1
      base="$(context_real_git "$id" hash-object -t tree /dev/null 2>/dev/null)" || return 1
    fi
    context_reserve_command "$id" || return 1
    file="$WORK/context-diff.bin"
    context_real_git "$id" "${DIFF_CFG[@]}" diff --binary --no-ext-diff --no-textconv --submodule=short --ignore-submodules=dirty "$base" -- . >"$file" 2>/dev/null || return 1
    hash="$(sha256_file "$file")" || return 1; printf 'diff\t%s\n' "$hash" >>"$out/6-contexts.txt"
    context_reserve_command "$id" || return 1
    file="$WORK/context-status.z"
    context_real_git "$id" -c core.ignoreStat=false status --porcelain=v1 -z -uall --ignore-submodules=dirty -- . >"$file" 2>/dev/null || return 1
    hash="$(sha256_file "$file")" || return 1; printf 'status\t%s\n' "$hash" >>"$out/6-contexts.txt"
  done
}

origin_row() { # take は出力、compare は FD 4 の次の記録と一致するかだけを見る
  local expected actual="$1"
  if [ "$ORIGIN_MODE" = take ]; then printf '%s\n' "$actual"
  else
    IFS= read -r expected <&4 || return 1
    if [ "${2:-}" = f ]; then
      # Git の branch -D も root config を同内容で再作成する。末端通常ファイル
      # だけは inode を照合せず、種別・経路と、この後の読取可否/生 hash で確かめる。
      case "$expected" in node$'\t'f$'\t'*$'\t'*) : ;; *) return 1 ;; esac
      expected="${expected#*$'\t'}"; expected="${expected#*$'\t'}"; expected="${expected#*$'\t'}"
      actual="${actual#*$'\t'}"; actual="${actual#*$'\t'}"; actual="${actual#*$'\t'}"
    fi
    [ "$expected" = "$actual" ]
  fi
}
origin_walk() { # $1=元の絶対パス。各 component を照合してから次の先へ進む
  local rest="${1#/}" p=/ part kind ident link links=0 h
  case "$1" in /*) : ;; *) return 1 ;; esac
  while :; do
    link=-
    if [ -L "$p" ]; then
      kind=l
      read_link "$p" || return 1
      link="$RP"
    elif [ -d "$p" ]; then kind=d
    elif [ -f "$p" ]; then kind=f
    else return 1
    fi
    ident="$(stat_identity "$p" 2>/dev/null)" || return 1
    [ -n "$ident" ] || return 1
    origin_row "node"$'\t'"$kind"$'\t'"$ident"$'\t'"$(path_field "$link")"$'\t'"$(path_field "$p")" "$kind" || return 1
    # symlink は字面と lstat の照合後に展開する。実体パスへ先に置き換えると、途中の
    # リンクや親の差替えが消える。.. もリンクを展開してから OS と同じ順に辿る。
    if [ "$kind" = l ]; then
      links=$((links + 1))
      [ "$links" -le 40 ] && [ -n "$link" ] || return 1
      if [ -n "$rest" ]; then link="$link/$rest"; fi
      case "$link" in
        /*) p=/; rest="${link#/}" ;;
        *) split_path "$p"; p="$SP_DIR"; rest="$link" ;;
      esac
      continue
    fi
    if [ -z "$rest" ]; then
      [ "$kind" = f ] && [ -r "$p" ] || return 1
      reserve_config_read "$p" || return 1
      h="$(sha256_file "$p" 2>/dev/null)" && [ -n "$h" ] || return 1
      origin_row "data"$'\t'"$h" || return 1
      return 0
    fi
    [ "$kind" = d ] || return 1
    part="${rest%%/*}"
    if [ "$part" = "$rest" ]; then rest=""; else rest="${rest#*/}"; fi
    # 重複 / は経路を変えない。空の末尾 / で通常ファイルを受理しないため、Git が
    # file origin として返した経路だけを入力にする(比較時も保存済みの同じ経路)。
    [ -n "$part" ] || continue
    p="${p%/}/$part"
  done
}
config_env_row() { # $1=環境名。値を保存せず設定/未設定と hash だけを控える
  local name="$1" value="" state=unset
  if [ -n "${!name+x}" ]; then value="${!name}"; state=set; fi
  printf 'env\t%s\t%s\t%s\n' "$name" "$state" "$(sha256_str "$name:$state:$value")"
}
missing_leaf_still_missing() { # $1=開始時に無かった最初のパス。現在の resolved missing まで通常 directory だけを許す
  local p="$1" rest part current="$CAND_CURRENT_MISSING"
  case "$current" in "$p"|"$p"/*) : ;; *) return 1 ;; esac
  [ ! -e "$current" ] && [ ! -L "$current" ] || return 1
  if [ -e "$p" ] || [ -L "$p" ]; then [ -d "$p" ] && [ ! -L "$p" ] || return 1; fi
  rest="${current#"$p"}"
  while [ -n "$rest" ]; do
    rest="${rest#/}"; [ -n "$rest" ] || break
    part="${rest%%/*}"; if [ "$part" = "$rest" ]; then rest=""; else rest="${rest#*/}"; fi
    p="$p/$part"
    if [ -e "$p" ] || [ -L "$p" ]; then [ -d "$p" ] && [ ! -L "$p" ] || return 1; fi
  done
  return 0
}
candidate_row() {
  local actual="$1" expected
  if [ "$ORIGIN_MODE" = take ]; then printf '%s\n' "$actual" >>"$CONFIG_RECORD"; return 0; fi
  IFS= read -r expected <&4 || return 1
  if [ "$expected" = "$actual" ]; then return 0; fi
  # 末端の通常 file は同じ内容なら atomic replace を許す。親 directory と link は
  # 置換先の本文を開く前に厳密比較する。
  case "$expected"$'\n'"$actual" in node$'\t'f$'\t'*$'\n'node$'\t'f$'\t'*)
    expected="${expected#*$'\t'}"; expected="${expected#*$'\t'}"; expected="${expected#*$'\t'}"
    actual="${actual#*$'\t'}"; actual="${actual#*$'\t'}"; actual="${actual#*$'\t'}"
    [ "$expected" = "$actual" ] && return 0 ;;
  esac
  # 開始時に無かった親の下へ通常 directory だけを作り、末端をまだ作らない操作は許す。
  case "$expected" in missing$'\t'*)
    field_value "${expected#*$'\t'}"
    CAND_MISSING_FIRST="$UNQ"
    case "$actual" in
      node$'\t'd$'\t'*)
        # 最初に欠けていた親が通常 directory として作られた。以後の node は記録に無いため、
        # candidate_walk 側で同じ条件を保ったまま末端 missing まで進める。
        field_value "${actual##*$'\t'}"; CAND_CURRENT_MISSING="$UNQ"
        case "$CAND_CURRENT_MISSING" in "$CAND_MISSING_FIRST"|"$CAND_MISSING_FIRST"/*) : ;; *) return 1 ;; esac
        [ -d "$CAND_CURRENT_MISSING" ] && [ ! -L "$CAND_CURRENT_MISSING" ] || return 1
        CAND_MISSING_PENDING=1; return 0 ;;
      missing$'\t'*)
        field_value "${actual#*$'\t'}"; CAND_CURRENT_MISSING="$UNQ"
        if missing_leaf_still_missing "$CAND_MISSING_FIRST"; then CAND_MISSING_ACCEPTED=1; return 0; fi ;;
    esac ;;
  esac
  return 1
}
candidate_walk() { # $1=候補の絶対パス。通常 file / 不在だけを受け入れる
  local rest="${1#/}" p=/ part kind ident link links=0 h
  # 候補の字面は Git が相対 include を解決する親文脈でもある。経路を辿った物理実体は
  # 実効 origin との対応付け専用にし、この論理 path を置き換えない。
  CAND_TARGET="$1"; CAND_LOGICAL="$1"; CAND_MISSING_ACCEPTED=0; CAND_MISSING_PENDING=0; CAND_MISSING_FIRST=""; CAND_HAD_LINK=0
  case "$1" in /*) : ;; *) return 1 ;; esac
  while :; do
    link=-
    if [ -L "$p" ]; then kind=l; read_link "$p" || return 1; link="$RP"
    elif [ -d "$p" ]; then kind=d
    elif [ -f "$p" ]; then kind=f
    elif [ -e "$p" ]; then return 1
    else
      if [ "$CAND_MISSING_PENDING" -eq 1 ]; then
        CAND_CURRENT_MISSING="$p"
        missing_leaf_still_missing "$CAND_MISSING_FIRST" || return 1
        CAND_MISSING_ACCEPTED=1
      else
        candidate_row "missing"$'\t'"$(path_field "$p")" || return 1
      fi
      return 0
    fi
    if [ "$CAND_MISSING_PENDING" -eq 1 ]; then
      case "$p" in "$CAND_MISSING_FIRST"|"$CAND_MISSING_FIRST"/*) : ;; *) return 1 ;; esac
      [ "$kind" = d ] && [ ! -L "$p" ] || return 1
    else
      ident="$(stat_identity "$p" 2>/dev/null)" || return 1
      candidate_row "node"$'\t'"$kind"$'\t'"$ident"$'\t'"$(path_field "$link")"$'\t'"$(path_field "$p")" || return 1
    fi
    [ "$CAND_MISSING_ACCEPTED" -eq 0 ] || return 0
    if [ "$kind" = l ]; then
      links=$((links + 1)); [ "$links" -le 40 ] && [ -n "$link" ] || return 1
      if [ -n "$rest" ]; then link="$link/$rest"; fi
      case "$link" in /*) p=/; rest="${link#/}" ;; *) split_path "$p"; p="$SP_DIR"; rest="$link" ;; esac
      continue
    fi
    if [ -z "$rest" ]; then
      [ "$kind" = f ] && [ -r "$p" ] || return 1
      reserve_config_read "$p" || return 1
      h="$(sha256_file "$p" 2>/dev/null)" && [ -n "$h" ] || return 1
      candidate_row "data"$'\t'"$h" || return 1
      # `p` は論理経路から symlink を順に展開した綴りなので `..` を残しうる。
      # Git の show-origin と照合する物理集合には canonical な実体だけを入れる。
      real_path "$p" || return 1
      CAND_KIND=file; CAND_REAL="$RP"; CAND_HAD_LINK="$links"
      # Git が directory symlink の下の config を物理 origin で返しても、
      # 開始時に同じ鎖を記録した candidate として照合できるよう対応付ける。
      if [ "$ORIGIN_MODE" = take ]; then
        # Git は show-origin で、symlink を辿った後にも `..` を残した綴りを返す版がある。
        # どちらもこの候補を全経路検査して得た同じ実体なので、展開後の綴りと canonical な
        # 実体を物理 origin の許可集合に入れる。候補キューの重複制御には使わない。
        CONFIG_ORIGIN_SEEN[":$p"]=1
        CONFIG_ORIGIN_SEEN[":$CAND_REAL"]=1
      fi
      return 0
    fi
    [ "$kind" = d ] || return 1
    part="${rest%%/*}"; if [ "$part" = "$rest" ]; then rest=""; else rest="${rest#*/}"; fi
    [ -n "$part" ] || continue
    p="${p%/}/$part"
  done
}
candidate_probe() { # $1=候補 $2=一時記録。解析中に同じ内容・構造であったことを確かめる
  local keep_record="$CONFIG_RECORD" keep_mode="$ORIGIN_MODE" keep_kind="${CAND_PARSE_FILE:-0}" rc=0
  CONFIG_RECORD="$2"; ORIGIN_MODE=take; CAND_PARSE_FILE=1
  : >"$CONFIG_RECORD" || rc=1
  if [ "$rc" -eq 0 ]; then candidate_walk "$1" || rc=$?; fi
  CONFIG_RECORD="$keep_record"; ORIGIN_MODE="$keep_mode"; CAND_PARSE_FILE="$keep_kind"
  return "$rc"
}
enqueue_candidate() { # $1=path。候補値自体は記録せず path と構造/hash を控える
  local p="$1"
  case "$p" in /*) : ;; *) return 1 ;; esac
  [ -z "${CONFIG_SEEN[":$p"]+x}" ] || return 0
  [ "${#CONFIG_QUEUE[@]}" -lt 2000 ] || return 1
  CONFIG_SEEN[":$p"]=1
  CONFIG_QUEUE[${#CONFIG_QUEUE[@]}]="$p"
  CONFIG_QUEUE_KIND[${#CONFIG_QUEUE_KIND[@]}]=config
}
enqueue_admin_candidate() { # .git / commondir 等は設定として解析せず対応だけを控える
  local p="$1"
  case "$p" in /*) : ;; *) return 1 ;; esac
  [ -z "${CONFIG_SEEN[":$p"]+x}" ] || return 0
  [ "${#CONFIG_QUEUE[@]}" -lt 2000 ] || return 1
  CONFIG_SEEN[":$p"]=1
  CONFIG_QUEUE[${#CONFIG_QUEUE[@]}]="$p"
  CONFIG_QUEUE_KIND[${#CONFIG_QUEUE_KIND[@]}]=admin
}
enqueue_admin_entry() { # directory は存在しない番兵を末尾にして、その directory 自体を候補に固定する
  local p="$1"
  # directory symlink も番兵まで歩く。入口 link・各 target directory を
  # candidate_walk が記録するため、後の鎖/target/parent 差替えは Git 前に止まる。
  if [ -d "$p" ]; then
    enqueue_admin_candidate "$p/.implement-guard-admin-entry" || return 1
  else
    enqueue_admin_candidate "$p" || return 1
  fi
}
enqueue_config_path() { # 相対の環境値は Git が使う cwd と toplevel の両方の文脈を控える
  case "$1" in
    /*) enqueue_candidate "$1" ;;
    *) enqueue_candidate "$CWD_PHYS/$1" || return 1
       [ "$CWD_PHYS" = "$TOP" ] || enqueue_candidate "$TOP/$1" ;;
  esac
}
bounded_config_parse() { # $1=stdout 保存先、残り=git config の引数。書込み中に総量上限を適用する
  local out="$1" remaining size rc=0
  shift
  remaining=$((200 * 1024 * 1024 - CONFIG_PARSE_BYTES))
  [ "$remaining" -gt 0 ] || return 125
  GIT_USED=1
  bounded_config_stdout "$out" "$remaining" isolated "$@" || rc=$?
  size="$(stat_size "$out" 2>/dev/null)" || return 125
  [ "$size" -le "$remaining" ] || return 125
  CONFIG_PARSE_BYTES=$((CONFIG_PARSE_BYTES + size))
  return "$rc"
}
bounded_effective_origins() { # 実効 origin も候補解析と同じ総量上限内で、全量を書き出す前に止める
  local out="$1" remaining size rc=0
  reserve_config_git 1 || return 125
  remaining=$((200 * 1024 * 1024 - CONFIG_PARSE_BYTES))
  [ "$remaining" -gt 0 ] || return 125
  bounded_config_stdout "$out" "$remaining" effective config --null --show-origin --name-only --includes --list || rc=$?
  size="$(stat_size "$out" 2>/dev/null)" || return 125
  [ "$size" -le "$remaining" ] || return 125
  CONFIG_PARSE_BYTES=$((CONFIG_PARSE_BYTES + size))
  return "$rc"
}
bounded_config_stdout() { # $1=保存先 $2=正確な byte 上限 $3=isolated|effective 残り=git config 引数
  # `ulimit -f` は環境により 512/1024 byte block で、byte 上限に使えない。
  # fifo から最大 64KiB ずつ、超過検知 1 byte を順に読む。主出力は上限を越えない。
  local out="$1" limit="$2" mode="$3" fifo probe writer writer_rc=0 rc=0 before after chunk
  shift 3
  fifo="$WORK/config-stream-$CONFIG_PARSE_SEQ-$$"; probe="$fifo-over"
  rm -f -- "$fifo" "$probe"; mkfifo "$fifo" || return 125
  : >"$out" || { rm -f -- "$fifo"; return 125; }
  if [ "$mode" = selector ]; then
    ( selector_git_raw "$@" >"$fifo" 2>/dev/null ) &
  elif [ "$mode" = isolated ]; then
    ( isolated_config_git "$@" >"$fifo" 2>/dev/null ) &
  else
    ( git "${GIT_PRE[@]}" -C "$TOP" "$@" >"$fifo" 2>/dev/null ) &
  fi
  writer=$!
  # FIFO を一度だけ開いた FD で続けて読む。dd ごとに開き直すと、短い出力では writer が先に閉じて
  # 次の open が待ち続ける。
  exec 6<"$fifo" || { kill "$writer" 2>/dev/null || :; wait "$writer" 2>/dev/null || :; rm -f -- "$fifo" "$probe"; return 125; }
  # `iflag=fullblock` / `status=none` は GNU 固有なので使わない。1 回を最大 64KiB にして
  # 実際に増えた byte 数で繰り返すと、pipe の短い read でも主出力は limit を越えない。
  while [ "$rc" -eq 0 ]; do
    before="$(stat_size "$out" 2>/dev/null)" || { rc=125; break; }
    [ "$before" -lt "$limit" ] || break
    chunk=$((limit - before)); [ "$chunk" -le 65536 ] || chunk=65536
    dd bs="$chunk" count=1 <&6 >>"$out" 2>/dev/null || { rc=125; break; }
    after="$(stat_size "$out" 2>/dev/null)" || { rc=125; break; }
    [ "$after" -le "$limit" ] || { rc=125; break; }
    [ "$after" -gt "$before" ] || break
  done
  if [ "$rc" -eq 0 ]; then dd bs=1 count=1 <&6 >"$probe" 2>/dev/null || rc=125; fi
  exec 6<&-
  if wait "$writer"; then writer_rc=0; else writer_rc=$?; fi
  if [ "$rc" -eq 0 ]; then rc="$writer_rc"; fi
  if [ "$rc" -eq 0 ] && [ -s "$probe" ]; then rc=125; fi
  rm -f -- "$fifo" "$probe"
  return "$rc"
}
parse_candidate_includes() { # $1=通常ファイル $2=経路上の symlink 数。値は NUL のまま読み、Git stderr を出さない
  local f="$1" linked="${2:-0}" key value parent rc=0 before after bytes=0 before_walk after_walk seq parse_key file_ident parent_ident
  local -A queried=()
  # 同じ file でも相対 include は親 directory の文脈で変わる。file とその親の device/inode を対にし、
  # symlink を何重にも通る同一文脈の cycle だけを畳む(M4 の異なる alias 親は inode が違うので残る)。
  parent="${f%/*}"; [ -n "$parent" ] || parent=/
  file_ident="$(stat_identity "$f" 2>/dev/null)" || return 1
  parent_ident="$(stat_identity "$parent" 2>/dev/null)" || return 1
  parse_key="file:$file_ident@$parent_ident"
  CONFIG_NODE[":$f"]="$parse_key"
  [ -z "${CONFIG_PARSED[":$parse_key"]+x}" ] || return 0
  CONFIG_PARSED[":$parse_key"]=1
  CONFIG_SIZE[":$parse_key"]="$(config_target_size "$f")" || return 1
  reserve_config_read "$f" || return 1
  before="$(sha256_file "$f" 2>/dev/null)" || return 1
  CONFIG_PARSE_SEQ=$((CONFIG_PARSE_SEQ + 1)); seq="$CONFIG_PARSE_SEQ"
  before_walk="$WORK/config-before-$seq"; after_walk="$WORK/config-after-$seq"
  candidate_probe "$f" "$before_walk" || return 1
  reserve_config_read "$f" || return 1
  if bounded_config_parse "$WORK/config-keys.z" config --file "$f" --no-includes --null --name-only \
       --get-regexp '^(include\.path|includeif\..*\.path)$'; then rc=0; else rc=$?; fi
  if [ "$rc" -ne 0 ]; then
    # rc 1 と空出力だけが include 無し。他は構文または読取の失敗である。
    [ "$rc" -eq 1 ] && [ ! -s "$WORK/config-keys.z" ] || return 1
  else
    while :; do
      key=""
      if ! IFS= read -r -d '' key <&3; then [ -z "$key" ] || return 1; break; fi
      [ -n "$key" ] || return 1
      [ -z "${queried[$key]+x}" ] || continue
      queried[$key]=1
      rc=0
      reserve_config_read "$f" || return 1
      if bounded_config_parse "$WORK/config-values.z" config --file "$f" --no-includes --null --type=path \
           --get-all "$key"; then rc=0; else rc=$?; fi
      [ "$rc" -eq 0 ] || return 1
      while :; do
        value=""
        if ! IFS= read -r -d '' value <&5; then [ -z "$value" ] || return 1; break; fi
        [ -n "$value" ] || return 1
        CONFIG_VALUES=$((CONFIG_VALUES + 1)); bytes=$((bytes + ${#value}))
        [ "$CONFIG_VALUES" -le 2000 ] && [ "$bytes" -le $((200 * 1024 * 1024)) ] || return 1
        case "$value" in /*) : ;; *) value="$parent/$value" ;; esac
        enqueue_candidate "$value" || return 1
        CONFIG_EDGE_FROM[${#CONFIG_EDGE_FROM[@]}]="$parse_key"
        CONFIG_EDGE_TO[${#CONFIG_EDGE_TO[@]}]="$value"
        CONFIG_EDGE_KEY[${#CONFIG_EDGE_KEY[@]}]="$key"
      done 5<"$WORK/config-values.z"
    done 3<"$WORK/config-keys.z"
  fi
  reserve_config_read "$f" || return 1
  after="$(sha256_file "$f" 2>/dev/null)" || return 1
  [ "$before" = "$after" ] || return 1
  candidate_probe "$f" "$after_walk" || return 1
  cmp -s "$before_walk" "$after_walk"
}
add_config_roots() {
  local v p common gitdir system rc=0 i count="${GIT_CONFIG_COUNT:-0}" bool=""
  CONFIG_SYSTEM_ENABLED=0; CONFIG_SYSTEM_SELECTED=
  for v in HOME XDG_CONFIG_HOME GIT_CONFIG GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM GIT_CONFIG_NOSYSTEM GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS; do config_env_row "$v"; done
  case "$count" in ''|*[!0-9]*) return 1 ;; esac
  for ((i=0; i<count; i++)); do config_env_row "GIT_CONFIG_KEY_$i"; config_env_row "GIT_CONFIG_VALUE_$i"; done
  capture_path git "${GIT_PRE[@]}" -C "$TOP" rev-parse --git-path config || rc=$?
  [ "$rc" -eq 0 ] && [ -n "$CAP" ] || return 1; p="$CAP"; case "$p" in /*) : ;; *) p="$TOP/$p" ;; esac; enqueue_candidate "$p" || return 1
  rc=0; capture_path git "${GIT_PRE[@]}" -C "$TOP" rev-parse --git-path config.worktree || rc=$?
  [ "$rc" -eq 0 ] && [ -n "$CAP" ] || return 1; p="$CAP"; case "$p" in /*) : ;; *) p="$TOP/$p" ;; esac; enqueue_candidate "$p" || return 1
  rc=0; capture_path git "${GIT_PRE[@]}" -C "$TOP" rev-parse --git-common-dir || rc=$?
  [ "$rc" -eq 0 ] && [ -n "$CAP" ] || return 1; common="$CAP"; case "$common" in /*) : ;; *) common="$TOP/$common" ;; esac; enqueue_candidate "$common/config" || return 1; enqueue_candidate "$common/config.worktree" || return 1
  rc=0; capture_path git "${GIT_PRE[@]}" -C "$TOP" rev-parse --git-dir || rc=$?
  [ "$rc" -eq 0 ] && [ -n "$CAP" ] || return 1; gitdir="$CAP"; case "$gitdir" in /*) : ;; *) gitdir="$TOP/$gitdir" ;; esac; enqueue_candidate "$gitdir/config" || return 1; enqueue_candidate "$gitdir/config.worktree" || return 1
  CONFIG_GITDIR="$gitdir"; CONFIG_COMMON="$common"
  printf 'plan-admin\t%s\t%s\n' "$(path_field "$gitdir")" "$(path_field "$common")"
  # worktree の入口、admin の commondir、common/admin の対応も候補として固定する。
  # directory 自体は設定として解析しない。番兵の不存在と親の識別子だけを記録する。
  enqueue_admin_entry "$TOP/.git" || return 1
  enqueue_admin_entry "$gitdir" || return 1
  enqueue_admin_candidate "$gitdir/commondir" || return 1
  enqueue_admin_entry "$common" || return 1
  if [ -n "${GIT_CONFIG_GLOBAL+x}" ]; then
    [ -z "$GIT_CONFIG_GLOBAL" ] || { [ "$GIT_CONFIG_GLOBAL" = /dev/null ] || enqueue_config_path "$GIT_CONFIG_GLOBAL"; } # MUT:o
  else
    if [ -n "${HOME+x}" ]; then enqueue_config_path "$HOME/.gitconfig" || return 1; fi
    if [ -n "${XDG_CONFIG_HOME:-}" ]; then enqueue_config_path "$XDG_CONFIG_HOME/git/config" || return 1
    elif [ -n "${HOME+x}" ]; then enqueue_config_path "$HOME/.config/git/config" || return 1; fi
  fi
  if [ -z "${GIT_CONFIG_NOSYSTEM+x}" ]; then system=1
  else
    rc=0
    if bool="$(env -u GIT_CONFIG -u GIT_CONFIG_GLOBAL -u GIT_CONFIG_SYSTEM -u GIT_CONFIG_NOSYSTEM \
      GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_COUNT=0 \
      GIT_DIR="$CONFIG_PARSE_CWD/repo.git" git "${GIT_PRE[@]}" -C "$CONFIG_PARSE_CWD" -c "config.guard.bool=$GIT_CONFIG_NOSYSTEM" config --type=bool --get config.guard.bool 2>/dev/null)"; then rc=0; else rc=$?; fi
    [ "$rc" -eq 0 ] || return 1
    case "$bool" in true) system=0 ;; false) system=1 ;; *) return 1 ;; esac
  fi
  if [ "$system" -eq 1 ] && [ "${GIT_CONFIG_SYSTEM:-}" != /dev/null ] && { [ -z "${GIT_CONFIG_SYSTEM+x}" ] || [ -n "$GIT_CONFIG_SYSTEM" ]; }; then
    rc=0; capture_path git "${GIT_PRE[@]}" -C "$TOP" var GIT_CONFIG_SYSTEM || rc=$?
    [ "$rc" -eq 0 ] && [ -n "$CAP" ] || return 1
    CONFIG_SYSTEM_ENABLED=1; CONFIG_SYSTEM_SELECTED="$CAP"
    [ "$CAP" = /dev/null ] || enqueue_config_path "$CAP" || return 1
    if [ -n "${GIT_CONFIG_SYSTEM:-}" ] && [ "$GIT_CONFIG_SYSTEM" != /dev/null ]; then enqueue_config_path "$GIT_CONFIG_SYSTEM" || return 1; fi
  fi
  if [ -n "${GIT_CONFIG:-}" ] && [ "$GIT_CONFIG" != /dev/null ]; then
    enqueue_config_path "$GIT_CONFIG" || return 1
  fi
}
verify_effective_origins() { # 全候補を控えた後に、実効 origin と safe 前置きの command key を照合する
  local origin key item pending=0 p expected actual pair_count=0
  local command_keys=() seen_commands=()
  for item in "${GIT_PRE[@]}"; do
    if [ "$pending" -eq 1 ]; then
      key="${item%%=*}"
      command_keys[${#command_keys[@]}]="${key,,}"
      pending=0
    elif [ "$item" = -c ]; then pending=1; fi
  done
  bounded_effective_origins "$WORK/effective-origins.z" || return 1
  while :; do
    origin=""
    if ! IFS= read -r -d '' origin <&3; then [ -z "$origin" ] || return 1; break; fi
    key=""
    IFS= read -r -d '' key <&3 || return 1
    [ -n "$origin" ] && [ -n "$key" ] || return 1
    pair_count=$((pair_count + 1))
    case "$origin" in
      file:*)
        p="${origin#file:}"; [ -n "$p" ] || return 1
        case "$p" in /*) : ;; *) p="$TOP/$p" ;; esac
        # Git は論理 candidate の綴り(.git の入口 link を含む)と、展開後の物理 origin の
        # いずれでも返しうる。前者は候補キュー、後者は candidate_walk 済みの実体集合で受ける。
        { [ -n "${CONFIG_SEEN[":$p"]+x}" ] || [ -n "${CONFIG_ORIGIN_SEEN[":$p"]+x}" ]; } || return 1 ;;
      'command line:') seen_commands[${#seen_commands[@]}]="${key,,}" ;;
      *) return 1 ;;
    esac
  done 3<"$WORK/effective-origins.z"
  [ "$pair_count" -gt 0 ] || return 1
  [ "${#seen_commands[@]}" -eq "${#command_keys[@]}" ] || return 1
  for ((pending=0; pending<${#command_keys[@]}; pending++)); do
    [ "${seen_commands[$pending]}" = "${command_keys[$pending]}" ] || return 1
  done
}
collect_config_origins() { # $1=保存先。root と全 include 候補を保存する
  local n=0 p kind
  # 論理候補の重複と、Git が返す物理 origin の許可集合は別物である。前者を物理実体で
  # 畳むと、同じ file を異なる alias 親で読む相対 include を取りこぼす。
  CONFIG_RECORD="$1"; init_config_graph
  declare -A CONFIG_SEEN=() CONFIG_ORIGIN_SEEN=() CONFIG_PARSED=()
  ORIGIN_MODE=take
  printf 'config-origins-v3\n' >"$CONFIG_RECORD" || return 1
  CONFIG_PARSE_CWD="$WORK/config-parse"
  init_config_parser || return 1
  CONFIG_READ_BYTES=0; CONFIG_PARSE_BYTES=0; CONFIG_VALUES=0; CONFIG_PARSE_SEQ=0
  add_config_roots >>"$CONFIG_RECORD" || return 1
  for ((n=0; n<${#CONFIG_QUEUE[@]}; n++)); do
    [ "${CONFIG_QUEUE_KIND[$n]}" = config ] || continue
    CONFIG_ROOTS[${#CONFIG_ROOTS[@]}]="${CONFIG_QUEUE[$n]}"
    printf 'plan-root\t%s\n' "$(path_field "${CONFIG_QUEUE[$n]}")" >>"$CONFIG_RECORD" || return 1
  done
  n=0
  while [ "$n" -lt "${#CONFIG_QUEUE[@]}" ]; do
    p="${CONFIG_QUEUE[$n]}"; kind="${CONFIG_QUEUE_KIND[$n]}"; n=$((n + 1)); [ "$n" -le 2000 ] || return 1
    printf 'candidate\t%s\n' "$(path_field "$p")" >>"$CONFIG_RECORD" || return 1
    CAND_KIND=; CAND_LOGICAL=; CAND_REAL=; CAND_PARSE_FILE=0
    [ "$kind" = admin ] || CAND_PARSE_FILE=1
    candidate_walk "$p" || return 1
    [ "$kind" = admin ] || { [ "$CAND_KIND" = file ] && parse_candidate_includes "$CAND_LOGICAL" "$CAND_HAD_LINK" || [ -z "$CAND_KIND" ] || return 1; } # MUT:q
  done
  CONFIG_DRAINED="${#CONFIG_QUEUE[@]}"
  collect_context_plan || return 1
  verify_effective_origins || return 1
}
verify_config_origins() { # FD 4 は呼び出し側が開く。Git を呼ばない
  local line p name state digest actual plan_admin=0 plan_roots=0
  IFS= read -r line <&4 && [ "$line" = config-origins-v3 ] || return 2
  ORIGIN_MODE=compare
  while IFS= read -r line <&4; do
    case "$line" in
      env$'\t'*$'\t'*$'\t'*)
        IFS=$'\t' read -r _ name state digest <<<"$line"
        actual="$(config_env_row "$name")"; [ "$actual" = "$line" ] || { ORIGIN_CHANGED="environment"; return 1; } ;;
      plan-admin$'\t'*)
        plan_admin=$((plan_admin + 1)); [ "$plan_admin" -eq 1 ] || return 1
        p="${line#*$'\t'}"; field_value "${p%%$'\t'*}"; CONFIG_GITDIR="$UNQ"
        field_value "${p#*$'\t'}"; CONFIG_COMMON="$UNQ"
        case "$CONFIG_GITDIR" in /*) : ;; *) return 1 ;; esac
        case "$CONFIG_COMMON" in /*) : ;; *) return 1 ;; esac ;;
      plan-root$'\t'*)
        plan_roots=$((plan_roots + 1)); [ "$plan_roots" -le 2000 ] || return 1
        field_value "${line#*$'\t'}"
        case "$UNQ" in /*) : ;; *) return 1 ;; esac
        CONFIG_ROOTS[${#CONFIG_ROOTS[@]}]="$UNQ" ;;
      candidate$'\t'?*)
        field_value "${line#*$'\t'}"; p="$UNQ"; ORIGIN_CHANGED="$p"; candidate_walk "$p" || return 1 ;;
      *) return 1 ;;
    esac
  done
  [ "$plan_admin" -eq 1 ] && [ "$plan_roots" -gt 0 ] || return 2
  return 0
}
check_config_origins() { # 0=same 1=changed 2=old/missing
  local record="$STATE/snapshot/config-origins.txt"
  if [ ! -f "$record" ] || [ -L "$record" ] || [ ! -f "$STATE/snapshot/config-contexts.txt" ]; then return 2; fi
  CONFIG_READ_BYTES=0; CONFIG_ROOTS=()
  ORIGIN_CHANGED=""
  verify_config_origins 4<"$record"; return $?
}

# ── hooks と config の記録(**ファイルの読み取りだけ**で作る。git を呼ばない)──
hooks_listing() { # $1=hooks ディレクトリの実体パス → `hook\t種別\tモード\tsha256\t名前` を stdout へ
  local dir="$1" p name kind mode val
  while IFS= read -r -d '' p <&3; do
    name="${p#"$dir"/}"
    val="-"
    if [ -L "$p" ]; then kind=l; if read_link "$p"; then val="$(sha256_str "$RP")" || val="-"; else val="-"; fi
    elif [ -d "$p" ]; then kind=d
    elif [ -f "$p" ]; then
      if [ -r "$p" ] && val="$(sha256_file "$p")" && [ -n "$val" ]; then kind=f; else kind=u; val="-"; fi
    else kind=o
    fi
    mode="$(stat_mode "$p" 2>/dev/null)" || mode="-"
    printf 'hook\t%s\t%s\t%s\t%s\n' "$kind" "$mode" "$val" "$(path_field "$name")"
  done 3< <(find "$dir" -mindepth 1 -print0 2>/dev/null | LC_ALL=C sort -z) </dev/null
}
config_file_digest() { # $1=設定 root → CONFIG_DIGEST に sha256 か状態を入れる
  local h
  CONFIG_DIGEST="(読めない)"
  if [ ! -e "$1" ] && [ ! -L "$1" ]; then CONFIG_DIGEST="(無し)"; return 0; fi
  if [ -f "$1" ] && [ -r "$1" ] && reserve_config_read "$1" && h="$(sha256_file "$1")" && [ -n "$h" ]; then
    CONFIG_DIGEST="$h"
  fi
}
fs_meta_lines() { # $1=hooks のパス $2=config のパス $3=config.worktree のパス(いずれも絶対)→ stdout
  local hp="$1" hreal="(無し)" hdig="(無し)" list="$WORK/hooks.list" key rest config_digest config_worktree_digest
  : >"$list"
  if [ -e "$hp" ] || [ -L "$hp" ]; then
    if real_path "$hp"; then hreal="$RP"; else hreal="(無し)"; fi
    if [ -d "$hp" ] && [ "$hreal" != "(無し)" ]; then
      hooks_listing "$hreal" >"$list"
      hdig="$(sha256_file "$list")" || hdig=""
      [ -n "$hdig" ] || hdig="(計算できない)"
    else
      hdig="(ディレクトリでない)"
    fi
  fi
  printf 'hooks-path\t%s\n' "$(path_field "$hp")"
  printf 'hooks-real\t%s\n' "$(path_field "$hreal")"
  printf 'hooks-digest\t%s\n' "$hdig"
  cat "$list"
  if [ -n "${4:-}" ]; then
    # origin が変わった場合は root も読み直さず、hooks だけ独立して照合する。
    while IFS=$'\t' read -r key rest; do
      case "$key" in config-path|config-digest|config-worktree-path|config-worktree-digest) printf '%s\t%s\n' "$key" "$rest" ;; esac
    done <"$4"
    return 0
  fi
  config_file_digest "$2"; config_digest="$CONFIG_DIGEST"
  config_file_digest "$3"; config_worktree_digest="$CONFIG_DIGEST"
  printf 'config-path\t%s\n' "$(path_field "$2")"
  printf 'config-digest\t%s\n' "$config_digest"
  printf 'config-worktree-path\t%s\n' "$(path_field "$3")"
  printf 'config-worktree-digest\t%s\n' "$config_worktree_digest"
}
fs_meta_filter() { # $1=4-meta.txt の形式のファイル → hooks と config の行だけを stdout へ
  local key rest
  while IFS=$'\t' read -r key rest; do
    case "$key" in
      hooks-path|hooks-real|hooks-digest|hook|config-path|config-digest|config-worktree-path|config-worktree-digest)
        printf '%s\t%s\n' "$key" "$rest" ;;
    esac
  done <"$1"
}
add_change() { CHANGES[${#CHANGES[@]}]="CHANGE=$1"$'\t'"$2"; }
diff_fs_meta() { # $1=旧 $2=新(fs_meta_filter の出力)→ CHANGE=hooks / CHANGE=config を積む
  local key a b c d k hdir
  declare -A so=() sn=() ho=() hn=()
  local horder=() norder=()
  while IFS=$'\t' read -r key a b c d; do
    if [ "$key" = hook ]; then k=":$d"; ho[$k]="$a $b $c"; horder[${#horder[@]}]="$d"; else so[$key]="$a"; fi
  done <"$1"
  while IFS=$'\t' read -r key a b c d; do
    if [ "$key" = hook ]; then k=":$d"; hn[$k]="$a $b $c"; norder[${#norder[@]}]="$d"; else sn[$key]="$a"; fi
  done <"$2"
  field_value "${sn[hooks-real]:-}"; hdir="$UNQ"
  if [ "$hdir" = "(無し)" ] || [ -z "$hdir" ]; then field_value "${sn[hooks-path]:-}"; hdir="$UNQ"; fi
  local hooks_hit=0
  for d in ${horder[@]+"${horder[@]}"}; do
    k=":$d"
    if [ -z "${hn[$k]+x}" ] || [ "${hn[$k]}" != "${ho[$k]}" ]; then
      field_value "$d"; add_change hooks "$(path_field "$hdir/$UNQ")"; hooks_hit=1
    fi
  done
  for d in ${norder[@]+"${norder[@]}"}; do
    k=":$d"
    if [ -z "${ho[$k]+x}" ]; then field_value "$d"; add_change hooks "$(path_field "$hdir/$UNQ")"; hooks_hit=1; fi
  done
  if [ "${so[hooks-path]:-}" != "${sn[hooks-path]:-}" ] || [ "${so[hooks-real]:-}" != "${sn[hooks-real]:-}" ] \
     || [ "${so[hooks-digest]:-}" != "${sn[hooks-digest]:-}" ]; then
    FS_HOOKS_CHANGED=1
    if [ "$hooks_hit" -eq 0 ]; then add_change hooks "${sn[hooks-path]:-}"; fi
  fi
  if [ "$hooks_hit" -eq 1 ]; then FS_HOOKS_CHANGED=1; fi
  if [ "${so[config-path]:-}" != "${sn[config-path]:-}" ] || [ "${so[config-digest]:-}" != "${sn[config-digest]:-}" ]; then
    FS_CONFIG_CHANGED=1; add_change config "${sn[config-path]:-}"
  fi
  if [ "${so[config-worktree-path]:-}" != "${sn[config-worktree-path]:-}" ] \
     || [ "${so[config-worktree-digest]:-}" != "${sn[config-worktree-digest]:-}" ]; then
    FS_CONFIG_CHANGED=1; add_change config "${sn[config-worktree-path]:-}"
  fi
}

# ── 5 要素のうち git で取るもの(①②④⑤)を $1 へ書く ──
# **この関数は `||` の左辺で呼ぶので、中では errexit が効かない。失敗は 1 つずつ明示的に拾う。**
git_path_abs() { # $1=前置きの種類(hooks|std)$2=名前 → 絶対パスを stdout へ
  local which="$1" name="$2" fmt p rc
  for fmt in "--path-format=absolute" ""; do
    reserve_config_git 1 || return 1
    rc=0
    if [ "$which" = hooks ]; then
      capture_path git "${GIT_PRE_HOOKS[@]}" rev-parse $fmt --git-path "$name" || rc=$? # MUT:j
    else
      capture_path git "${GIT_PRE[@]}" rev-parse $fmt --git-path "$name" || rc=$?
    fi
    p="$CAP"
    # `--path-format` を知らない古い git は、その引数を 1 行目にそのまま出して rc 0 で返す
    # (パス自体が `-` で始まることは無いので、これだけで見分けられる)
    case "$p" in -*) rc=1 ;; esac
    if [ "$rc" -eq 0 ] && [ -n "$p" ]; then
      case "$p" in /*) RP="$p" ;; *) RP="$TOP/$p" ;; esac
      return 0
    fi
  done
  return 1
}
collect_git() { # $1=出力先ディレクトリ $2=take|compare → 0 / 1(COLLECT_ERR に理由)。unmerged は COLLECT_UNMERGED=1
  local out="$1" how="$2" rc head tree branch base hooks_p cfg_p cfgw_p line
  COLLECT_ERR=""
  COLLECT_UNMERGED=0


  # ④ HEAD(コミット 0 の repo は値として記録する。縮退にしない)
  rc=0
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  head="$(git "${GIT_PRE[@]}" rev-parse --verify --quiet HEAD 2>/dev/null)" || rc=$?
  case "$rc" in
    0) if [ -z "$head" ]; then COLLECT_ERR="rev-parse HEAD が空を返した"; return 1; fi ;;
    1) head="(HEAD 無し)" ;;
    *) COLLECT_ERR="rev-parse --verify HEAD が失敗した(rc=$rc)"; return 1 ;;
  esac

  # ④ index の tree。ユーザーの index は読むだけにして、使い捨ての index で write-tree する
  # (素の write-tree は cache-tree を書き込んで .git/index のバイト列を変える)
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  if ! git "${GIT_PRE[@]}" ls-files -s -z >"$WORK/stage.z" 2>"$WORK/git.err"; then
    COLLECT_ERR="ls-files -s が失敗した"; return 1
  fi
  rm -f -- "$WORK/index.tmp"
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  if ! GIT_INDEX_FILE="$WORK/index.tmp" git "${GIT_PRE[@]}" update-index -z --index-info <"$WORK/stage.z" 2>"$WORK/git.err"; then
    COLLECT_ERR="使い捨ての index を作れない"; return 1
  fi
  rc=0
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  tree="$(GIT_INDEX_FILE="$WORK/index.tmp" git "${GIT_PRE[@]}" write-tree 2>"$WORK/git.err")" || rc=$? # MUT:d
  if [ "$rc" -ne 0 ] || [ -z "$tree" ]; then
    reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
    if ! git "${GIT_PRE[@]}" ls-files -u -z >"$WORK/unmerged.z" 2>/dev/null; then
      COLLECT_ERR="write-tree が失敗した(rc=$rc)"; return 1
    fi
    if [ ! -s "$WORK/unmerged.z" ]; then COLLECT_ERR="write-tree が失敗した(rc=$rc)"; return 1; fi
    COLLECT_UNMERGED=1
    tree="(unmerged)"
    if [ "$how" = take ]; then return 0; fi   # take は ①②⑤ へ進まず縮退する
  fi

  # ④ ブランチ名と refs 全体(同コミット別ブランチへの切替・新しい ref を検出する)
  rc=0
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  branch="$(git "${GIT_PRE[@]}" symbolic-ref -q HEAD 2>/dev/null)" || rc=$?
  case "$rc" in
    0) : ;;
    1) branch="(detached)" ;;
    *) COLLECT_ERR="symbolic-ref HEAD が失敗した(rc=$rc)"; return 1 ;;
  esac
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  if ! git "${GIT_PRE[@]}" for-each-ref --format='%(objectname) %(refname)' >"$WORK/refs.txt" 2>"$WORK/git.err"; then
    COLLECT_ERR="for-each-ref が失敗した"; return 1
  fi

  # ④ hooks と config の場所(linked worktree では common dir 側。リテラルの .git/hooks は使えない)
  if ! git_path_abs hooks hooks; then COLLECT_ERR="hooks ディレクトリを解決できない"; return 1; fi
  hooks_p="$RP"
  if ! git_path_abs std config; then COLLECT_ERR="config のパスを解決できない"; return 1; fi
  cfg_p="$RP"
  if ! git_path_abs std config.worktree; then COLLECT_ERR="config.worktree のパスを解決できない"; return 1; fi
  cfgw_p="$RP"

  {
    printf 'toplevel\t%s\n' "$(path_field "$TOP")"
    printf 'cwd\t%s\n' "$(path_field "$CWD_PHYS")"
    printf 'exclude\t%s\n' "$(path_field "$EXCL")"
    printf 'head\t%s\n' "$head"
    printf 'index-tree\t%s\n' "$tree"
    printf 'branch\t%s\n' "$branch"
    fs_meta_lines "$hooks_p" "$cfg_p" "$cfgw_p"
    while IFS= read -r line; do printf 'ref\t%s\n' "$line"; done <"$WORK/refs.txt"
  } >"$out/4-meta.txt" || { COLLECT_ERR="4-meta.txt を書けない"; return 1; }

  # ① 追跡分の未コミット差分
  base="HEAD"
  if [ "$head" = "(HEAD 無し)" ]; then reserve_config_git 1 || return 1; base="$(git "${GIT_PRE[@]}" hash-object -t tree /dev/null 2>/dev/null)" || base=""; fi # MUT:h
  if [ -z "$base" ]; then COLLECT_ERR="空ツリーの ID を計算できない"; return 1; fi
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  if ! git "${GIT_PRE[@]}" "${DIFF_CFG[@]}" diff --binary --no-ext-diff --no-textconv --submodule=short --ignore-submodules=dirty "$base" -- . ":(exclude,literal)$EXCL" >"$out/1-diff.bin" 2>"$WORK/git.err"; then
    COLLECT_ERR="diff が失敗した: $(head -n 1 "$WORK/git.err" 2>/dev/null | tr '[:cntrl:]' '?')"; return 1
  fi

  # ② 全状態の一覧
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  if ! GIT_OPTIONAL_LOCKS=0 git "${GIT_PRE[@]}" -c core.ignoreStat=false status --porcelain=v1 -z -uall --ignore-submodules=dirty -- . ":(exclude,literal)$EXCL" >"$out/2-status.z" 2>"$WORK/git.err"; then
    COLLECT_ERR="status が失敗した: $(head -n 1 "$WORK/git.err" 2>/dev/null | tr '[:cntrl:]' '?')"; return 1
  fi

  # ⑤ stash の二段取得(書式は固定する。既定の書式は log.date / core.abbrev で変わる)
  rc=0
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  git "${GIT_PRE[@]}" rev-parse --verify --quiet refs/stash >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0)
      reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
      if ! git "${GIT_PRE[@]}" reflog show --format='%H %gd %gs' refs/stash >"$out/5-stash.txt" 2>"$WORK/git.err"; then
        COLLECT_ERR="reflog show refs/stash が失敗した"; return 1
      fi ;;
    1) printf 'stash 無し\n' >"$out/5-stash.txt" || { COLLECT_ERR="5-stash.txt を書けない"; return 1; } ;;
    *) COLLECT_ERR="rev-parse refs/stash が失敗した(rc=$rc)"; return 1 ;;
  esac
  collect_context_snapshots "$out" || { COLLECT_ERR="子リポジトリの状態を取得できない"; return 1; }
  return 0
}

# ③ の対象集合を NUL 区切り・LC_ALL=C のパス順で $1 へ書く(同じく `||` の左辺で呼ぶ)
enumerate_untracked() {
  local out="$1" f
  reserve_config_git 1 || { COLLECT_ERR="設定本文の読取予算を超える"; return 1; }
  if ! git "${GIT_PRE[@]}" ls-files -o --exclude-standard -z >"$WORK/untracked.raw.z" 2>"$WORK/git.err"; then
    COLLECT_ERR="ls-files -o が失敗した"; return 1
  fi
  # 一覧は FD 3 から読み、ループ本体の stdin は /dev/null にする
  while IFS= read -r -d '' f <&3; do
    if in_scope "$f"; then printf '%s\0' "$f"; fi
  done 3<"$WORK/untracked.raw.z" </dev/null >"$WORK/untracked.sel.z"
  if ! LC_ALL=C sort -z <"$WORK/untracked.sel.z" >"$out"; then COLLECT_ERR="一覧を並べ替えられない"; return 1; fi
  return 0
}

# ── マニフェストの 1 エントリ(種別を見てから内容を読む)──
declare -A MADE_DIR=()
build_row() { # $1=toplevel 相対パス $2=take(退避して退避コピー側をハッシュ)|compare(実ファイルをハッシュ)
  local f="$1" how="$2" p="./$1" dest ddir lnk h
  ROW_KIND=u
  ROW_VAL="-"
  ROW_MODE="$(stat_mode "$p" 2>/dev/null)" || ROW_MODE="-"
  case "$f" in */) ROW_KIND=o; return 0 ;; esac   # ネスト repo のディレクトリ
  dest="$NEW_STATE/files/$f"
  if [ "$how" = take ]; then
    ddir="${dest%/*}"
    if [ -z "${MADE_DIR[":$ddir"]+x}" ]; then
      mkdir -p -- "$ddir" 2>/dev/null || return 0
      MADE_DIR[":$ddir"]=1
    fi
  fi
  if [ -L "$p" ]; then
    read_link "$p" || return 0
    lnk="$RP"
    if [ "$how" = take ] && ! cp -a -- "$p" "$dest" 2>/dev/null; then return 0; fi
    ROW_KIND=l
    ROW_VAL="$(path_field "$lnk")"
  elif [ ! -e "$p" ]; then return 0          # 列挙後に消えた
  elif [ ! -f "$p" ]; then ROW_KIND=o
  elif [ ! -r "$p" ]; then return 0
  else
    if [ "$how" = take ]; then
      if ! cp -a -- "$p" "$dest" 2>/dev/null; then rm -f -- "$dest" 2>/dev/null || true; return 0; fi
      h="$(sha256_file "$dest" 2>/dev/null)" || h=""      # 退避コピー側の生バイト(2026-09-17 決定 39)
    else
      h="$(sha256_file "$p" 2>/dev/null)" || h=""
    fi
    if [ -z "$h" ]; then return 0; fi
    ROW_KIND=f
    ROW_VAL="$h"
  fi
  return 0
}
verify_backup() { # $1=toplevel 相対パス $2=種別 $3=モード $4=値 → 退避コピーが記録どおりなら 0
  local b="$STATE/files/$1" m h
  case "$2" in
    f)
      if [ -L "$b" ] || [ ! -f "$b" ]; then return 1; fi
      m="$(stat_mode "$b" 2>/dev/null)" || return 1
      [ "$m" = "$3" ] || return 1
      h="$(sha256_file "$b" 2>/dev/null)" || return 1
      [ "$h" = "$4" ] || return 1 ;;
    l)
      [ -L "$b" ] || return 1
      read_link "$b" || return 1
      [ "$(path_field "$RP")" = "$4" ] || return 1 ;;
    *) return 1 ;;
  esac
  return 0
}
add_base() { # $1=toplevel 相対パス $2=種別 $3=モード $4=値
  local st=unverified
  if verify_backup "$1" "$2" "$3" "$4"; then st=ok; fi
  CHANGES[${#CHANGES[@]}]="BASE=$(path_field "$STATE/files/$1")"$'\t'"$st"
}

# ── 保護領域を開く(compare / taskmd-diff / restore-taskmd の共通の入口。git を呼ばない)──
state_fail() { # $1=manifest-digest|snapshot-digest|state-missing 残り=説明
  local reason="$1"; shift
  case "$SUB" in
    config-check) echo "ERROR [$reason] $*" >&2; printf 'RESULT=incomparable\nREASON=%s\nGIT_SKIPPED=yes\n' "$reason" ;;
    compare) echo "ERROR [$reason] $*" >&2; emit_incomparable "$reason" ;;
    taskmd-diff) echo "ERROR [digest] $*" >&2; printf 'REASON=digest\n' ;;
    restore-taskmd) echo "ERROR [digest] $*" >&2; printf 'RESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=digest\n' ;;
  esac
  trap - ERR
  exit 33
}
verify_digests() {
  local m s
  m="$(sha256_file "$STATE/manifest.tsv" 2>/dev/null)" || m=""
  if [ -z "$m" ] || [ "$m" != "${MAN_SHA,,}" ]; then state_fail manifest-digest "マニフェストのダイジェストが一致しない(保護領域の記録を信頼できない)"; fi
  s="$(snapshot_digest "$STATE/snapshot" 2>/dev/null)" || s=""
  if [ -z "$s" ] || [ "$s" != "${SNAP_SHA,,}" ]; then state_fail snapshot-digest "スナップショット本体のダイジェストが一致しない(保護領域の記録を信頼できない)"; fi
}
open_state() {
  local m
  if ! check_state_dir "$STATE_ARG"; then state_fail state-missing "--state が保護領域の guard-* でない・無い・symlink: $(path_field "$STATE_ARG")"; fi
  m="$(stat_mode "$STATE" 2>/dev/null)" || m=""
  if [ "$m" != 700 ]; then state_fail state-missing "--state のモードが 0700 でない: ${m:-不明}"; fi
  if [ -L "$STATE/manifest.tsv" ] || [ ! -f "$STATE/manifest.tsv" ] || [ -L "$STATE/snapshot" ] || [ ! -d "$STATE/snapshot" ]; then
    state_fail state-missing "--state に manifest.tsv / snapshot が無い"
  fi
  verify_digests # MUT:a
}
load_meta() { # ダイジェスト照合を通った記録から toplevel などを読む(git を呼ばない)
  local key val
  META=()
  while IFS=$'\t' read -r key val; do
    case "$key" in
      toplevel|cwd|exclude|hooks-path|config-path|config-worktree-path) field_value "$val"; META[$key]="$UNQ" ;;
      head|index-tree|branch) META[$key]="$val" ;;
    esac
  done <"$STATE/snapshot/4-meta.txt"
  TM=()
  while IFS=$'\t' read -r key val; do
    case "$key" in
      selected|real) field_value "$val"; TM[$key]="$UNQ" ;;
      kind) TM[$key]="$val" ;;
    esac
  done <"$STATE/snapshot/taskmd.txt"
  if [ -z "${META[toplevel]:-}" ] || [ -z "${META[cwd]:-}" ] || [ -z "${META[exclude]:-}" ] \
     || [ -z "${META[hooks-path]:-}" ] || [ -z "${META[config-path]:-}" ] || [ -z "${META[config-worktree-path]:-}" ] \
     || [ -z "${TM[selected]:-}" ] || [ -z "${TM[real]:-}" ] || [ -z "${TM[kind]:-}" ]; then
    fail_internal "保護領域の記録に必要な項目が無い"
  fi
  TOP="${META[toplevel]}"
  CWD_PHYS="${META[cwd]}"
  EXCL="${META[exclude]}"
  TASKMD_SEL="${TM[selected]}"
  TASKMD_REAL="${TM[real]}"
  TASKMD_KIND="${TM[kind]}"
  set_taskmd_rel
  # --cwd の取り違えを拾う(存在するのに記録と違う場所なら usage)
  local now
  if [ -d "$CWD" ]; then
    now=""
    if abs_existing_dir "$CWD"; then now="$RP"; fi
    if [ -n "$now" ] && [ "$now" != "$CWD_PHYS" ]; then
      fail_usage "--cwd が take のときと違う(記録: $(path_field "$CWD_PHYS") / 指定: $(path_field "$now"))"
    fi
  else
    echo "NOTE: --cwd が存在しない(保護領域の記録 $(path_field "$CWD_PHYS") を使う)" >&2
  fi
}
set_taskmd_rel() {
  TASKMD_REL=""
  TASKMD_SEL_REL=""
  case "$TASKMD_REAL" in "$TOP"/*) TASKMD_REL="${TASKMD_REAL#"$TOP"/}" ;; esac
  case "$TASKMD_SEL" in "$TOP"/*) TASKMD_SEL_REL="${TASKMD_SEL#"$TOP"/}" ;; esac
  # 空のままだと in_scope の比較が空文字どうしで当たるので、当たらない値にしておく
  [ -n "$TASKMD_REL" ] || TASKMD_REL=$'\001'
  [ -n "$TASKMD_SEL_REL" ] || TASKMD_SEL_REL=$'\001'
}
verify_taskmd_body() { # マニフェストの T 行と taskmd-body を突き合わせる → 0 / 1。T_MODE・T_SHA・T_PATH を設定
  local kind mode val pf m h
  T_MODE=""; T_SHA=""; T_PATH=""
  IFS=$'\t' read -r kind mode val pf <"$STATE/manifest.tsv" || return 1
  [ "$kind" = T ] || return 1
  field_value "$pf"
  T_MODE="$mode"; T_SHA="$val"; T_PATH="$UNQ"
  [ "$T_PATH" = "$TASKMD_REAL" ] || return 1
  if [ -L "$STATE/taskmd-body" ] || [ ! -f "$STATE/taskmd-body" ]; then return 1; fi
  m="$(stat_mode "$STATE/taskmd-body" 2>/dev/null)" || return 1
  [ "$m" = "$T_MODE" ] || return 1
  h="$(sha256_file "$STATE/taskmd-body" 2>/dev/null)" || return 1
  [ "$h" = "$T_SHA" ] || return 1
  return 0
}
T_MODE=""; T_SHA=""; T_PATH=""

# ═════════════════════════ take ═════════════════════════
cmd_take() {
  local rc f sz n_ent=0 total=0 unreadable=0 t_kind t_sha t_mode pf man_sha snap_sha

  resolve_tools
  init_excl

  # toplevel の自前解決(--cwd は toplevel かその配下)
  [ -d "$CWD" ] || fail_usage "--cwd が存在しない: $CWD"
  local top_raw=""
  rc=0
  capture_path git "${GIT_PRE[@]}" -C "$CWD" rev-parse --show-toplevel || rc=$?
  top_raw="$CAP"
  if [ "$rc" -ne 0 ] || [ -z "$top_raw" ]; then degrade not-git "--cwd が git リポジトリの中でない: $CWD"; fi
  abs_existing_dir "$top_raw" || degrade not-git "toplevel の物理パスを解決できない: $top_raw"
  TOP="$RP"
  abs_existing_dir "$CWD" || degrade not-git "--cwd の物理パスを解決できない: $CWD"
  CWD_PHYS="$RP"
  case "${CWD_PHYS:-/dev/null/none}" in
    "$TOP"|"$TOP"/*) : ;;
    *) degrade not-git "解決した toplevel が --cwd を含まない(toplevel: $TOP / --cwd: ${CWD_PHYS:-不明})" ;;
  esac
  if [ "$CWD_PHYS" = "$TOP" ]; then EXCL="$REVIEWS_DIR"; else EXCL="${CWD_PHYS#"$TOP"/}/$REVIEWS_DIR"; fi

  # 保護領域(以後の一時ファイルもこの中に置く)
  resolve_state_base
  NEW_STATE="$(mktemp -d "$STATE_BASE/guard-XXXXXX" 2>/dev/null)" || { NEW_STATE=""; degrade no-protected-area "保護領域にディレクトリを作れない: $STATE_BASE"; }
  chmod 700 "$NEW_STATE"
  WORK="$NEW_STATE/.work"
  mkdir -- "$WORK" "$NEW_STATE/snapshot" "$NEW_STATE/files"

  # タスク MD の実体の確定(symlink なら辿る。2026-09-17 決定 50)
  local sel="$TASK_MD" parent base
  case "$sel" in /*) : ;; *) sel="$CWD_PHYS/$sel" ;; esac
  split_path "$sel"
  base="$SP_BASE"
  abs_existing_dir "$SP_DIR" || degrade taskmd "タスク MD の親ディレクトリが無い: $TASK_MD"
  parent="$RP"
  TASKMD_SEL="$parent/$base"
  if [ -L "$TASKMD_SEL" ]; then TASKMD_KIND=symlink; else TASKMD_KIND=file; fi
  [ -e "$TASKMD_SEL" ] || degrade taskmd "タスク MD が存在しない・リンク切れ・ループ: $TASK_MD"
  real_path "$TASKMD_SEL" || degrade taskmd "タスク MD の実体パスを確定できない: $TASK_MD"
  TASKMD_REAL="$RP"
  [ -n "$TASKMD_REAL" ] || degrade taskmd "タスク MD の実体パスを確定できない: $TASK_MD"
  if [ -L "$TASKMD_REAL" ] || [ ! -f "$TASKMD_REAL" ]; then degrade taskmd "タスク MD の実体が通常ファイルでない: $TASKMD_REAL"; fi
  [ -r "$TASKMD_REAL" ] || degrade taskmd "タスク MD の実体が読めない: $TASKMD_REAL"
  set_taskmd_rel
  {
    printf 'selected\t%s\n' "$(path_field "$TASKMD_SEL")"
    printf 'real\t%s\n' "$(path_field "$TASKMD_REAL")"
    printf 'kind\t%s\n' "$TASKMD_KIND"
  } >"$NEW_STATE/snapshot/taskmd.txt"

  CDPATH= cd -P -- "$TOP"

  if ! collect_config_origins "$NEW_STATE/snapshot/config-origins.txt"; then
    fail_internal "設定の出典を安全に収集・保存できない"
  fi

  # ④ → ①②⑤(unmerged は ④ で検出して縮退する)
  rc=0
  collect_git "$NEW_STATE/snapshot" take || rc=$?
  if [ "$rc" -ne 0 ]; then fail_internal "スナップショットを取れない: $COLLECT_ERR"; fi
  if [ "$COLLECT_UNMERGED" -eq 1 ]; then degrade unmerged "index がコンフリクト中(unmerged のエントリがある)"; fi

  # ③ の列挙と上限検査(超えた時点で止める。作りかけの保護領域は EXIT で消える)
  rc=0
  enumerate_untracked "$WORK/untracked.z" || rc=$?
  if [ "$rc" -ne 0 ]; then fail_internal "未追跡ファイルを列挙できない: $COLLECT_ERR"; fi
  while IFS= read -r -d '' f <&3; do
    n_ent=$((n_ent + 1))
    case "$f" in
      */) : ;;
      *)
        if [ ! -L "./$f" ] && [ -f "./$f" ]; then
          sz="$(stat_size "./$f" 2>/dev/null)" || sz=0
          case "$sz" in ''|*[!0-9]*) sz=0 ;; esac
          total=$((total + sz))
        fi ;;
    esac
    if [ "$n_ent" -gt "$MAX_ENTRIES" ] || [ "$total" -gt "$MAX_BYTES" ]; then degrade over-limit "退避の上限を超えた(上限: $MAX_ENTRIES 件・$MAX_BYTES バイト。.gitignore が薄い可能性がある)"; fi # MUT:i
  done 3<"$WORK/untracked.z" </dev/null

  # 退避: まずタスク MD の実体(追跡済み・ignore 済み・リポジトリ外でも必ず。2026-09-17 決定 46・49)
  t_kind=u; t_sha="-"; t_mode="-"
  if cp -a -- "$TASKMD_REAL" "$NEW_STATE/taskmd-body" 2>"$WORK/cp.err"; then
    if t_sha="$(sha256_file "$NEW_STATE/taskmd-body" 2>/dev/null)" && [ -n "$t_sha" ] \
       && t_mode="$(stat_mode "$NEW_STATE/taskmd-body" 2>/dev/null)" && [ -n "$t_mode" ]; then
      t_kind=T
    else
      t_sha="-"; t_mode="-"
    fi
  fi
  if [ "$t_kind" != T ]; then degrade taskmd "タスク MD の退避・ハッシュに失敗した(復元元が無いまま起動しない): $TASKMD_REAL"; fi # MUT:k

  # 退避とマニフェスト(エントリ単位の失敗は u として記録して続行する)
  printf '%s\t%s\t%s\t%s\n' "$t_kind" "$t_mode" "$t_sha" "$(path_field "$TASKMD_REAL")" >"$NEW_STATE/manifest.tsv"
  while IFS= read -r -d '' f <&3; do
    build_row "$f" take
    if needs_quote "$f"; then pf="$(cquote "$f")"; else pf="$f"; fi
    printf '%s\t%s\t%s\t%s\n' "$ROW_KIND" "$ROW_MODE" "$ROW_VAL" "$pf" >>"$NEW_STATE/manifest.tsv"
    if [ "$ROW_KIND" = u ]; then
      unreadable=$((unreadable + 1))
      echo "NOTE: unreadable $pf" >&2
    fi
  done 3<"$WORK/untracked.z" </dev/null

  # ダイジェスト
  rm -rf -- "$WORK"
  WORK=""
  man_sha="$(sha256_file "$NEW_STATE/manifest.tsv")"
  snap_sha="$(snapshot_digest "$NEW_STATE/snapshot")"
  if [ -z "$man_sha" ] || [ -z "$snap_sha" ]; then fail_internal "ダイジェストを計算できない"; fi

  printf 'STATE_DIR=%s\n' "$(path_field "$NEW_STATE")"
  printf 'MANIFEST_SHA256=%s\n' "$man_sha"
  printf 'SNAPSHOT_SHA256=%s\n' "$snap_sha"
  printf 'ENTRIES=%s\n' "$n_ent"
  printf 'BYTES=%s\n' "$total"
  printf 'UNREADABLE=%s\n' "$unreadable"
  printf 'TASKMD_REAL=%s\n' "$(path_field "$TASKMD_REAL")"
  TAKE_DONE=1
}

# ═════════════════════════ compare ═════════════════════════
emit_incomparable() { # $1=REASON(stdout を出して exit 33)
  local c gm=unknown
  case "$1" in hooks-changed|config-changed) gm=yes ;; esac
  printf 'RESULT=incomparable\nWORKTREE_CHANGED=unknown\nGITMETA_CHANGED=%s\n' "$gm"
  printf 'REASON=%s\n' "$1"
  if [ "$GIT_USED" -eq 0 ]; then printf 'GIT_SKIPPED=yes\n'; else printf 'GIT_SKIPPED=no\n'; fi
  for c in ${CHANGES[@]+"${CHANGES[@]}"}; do printf '%s\n' "$c"; done
  trap - ERR
  exit 33
}
check_hooks_config() { # 通常設定とhooksをGit0で照合し、その後に隔離selectorを検査する
  local config_rc=0
  check_config_origins || config_rc=$? # MUT:n
  if [ "$config_rc" -eq 2 ]; then
    echo 'ERROR [config-origin-missing] 開始時の設定候補記録が無い。旧 state は比較できない' >&2
    emit_incomparable config-origin-missing
  elif [ "$config_rc" -ne 0 ]; then
    FS_CONFIG_CHANGED=1
    add_change config "$(path_field "${ORIGIN_CHANGED:-unknown}")"
  fi
  local held=""
  if [ "$FS_CONFIG_CHANGED" -eq 1 ]; then held="$STATE/snapshot/4-meta.txt"; fi
  fs_meta_filter "$STATE/snapshot/4-meta.txt" >"$WORK/fsmeta.old"
  fs_meta_lines "${META[hooks-path]}" "${META[config-path]}" "${META[config-worktree-path]}" "$held" >"$WORK/fsmeta.raw"
  fs_meta_filter "$WORK/fsmeta.raw" >"$WORK/fsmeta.new"
  if [ "$FS_CONFIG_CHANGED" -eq 0 ] && cmp -s "$WORK/fsmeta.old" "$WORK/fsmeta.new"; then
    check_context_plan || { add_change config "$(path_field "${ORIGIN_CHANGED:-unknown}")"; emit_incomparable config-changed; }
    return 0
  fi
  diff_fs_meta "$WORK/fsmeta.old" "$WORK/fsmeta.new"
  if [ "$FS_HOOKS_CHANGED" -eq 1 ]; then
    echo "ERROR [hooks-changed] hooks が起動前から変わっている(以後の git を打たない)" >&2
    emit_incomparable hooks-changed
  fi
  echo "ERROR [config-changed] リポジトリの config が起動前から変わっている(以後の git を打たない)" >&2
  emit_incomparable config-changed
}
cmd_config_check() {
  local config_rc=0
  resolve_tools
  init_excl
  open_state
  WORK="$(mktemp -d "$STATE/.work-XXXXXX")" || fail_internal "保護領域に一時ディレクトリを作れない"
  load_meta
  check_config_origins || config_rc=$?
  if [ "$config_rc" -eq 2 ]; then
    echo 'ERROR [config-origin-missing] 開始時の設定候補記録が無い。旧 state は比較できない' >&2
    printf 'RESULT=incomparable\nREASON=config-origin-missing\nGIT_SKIPPED=yes\n'
    trap - ERR; exit 33
  fi
  if [ "$config_rc" -ne 0 ]; then
    echo 'ERROR [config-changed] 起動前の設定候補が変わっている(以後の git を打たない)' >&2
    printf 'RESULT=incomparable\nREASON=config-changed\nGIT_SKIPPED=yes\n'
    printf 'CHANGE=config\t%s\n' "$(path_field "${ORIGIN_CHANGED:-unknown}")"
    trap - ERR; exit 33
  fi
  if ! check_context_plan; then
    printf 'RESULT=incomparable\nREASON=config-changed\n'
    if [ "$GIT_USED" -eq 0 ]; then printf 'GIT_SKIPPED=yes\n'; else printf 'GIT_SKIPPED=no\n'; fi
    printf 'CHANGE=config\t%s\n' "$(path_field "${ORIGIN_CHANGED:-unknown}")"
    trap - ERR; exit 33
  fi
  printf 'CONFIG=same\nGIT_SKIPPED=no\n'
  trap - ERR
  exit 0
}
retake_elements() { # 5 要素のうち git で取るものを take と同じ取り方で取り直す
  local rc=0
  mkdir -- "$WORK/new"
  if ! CDPATH= cd -P -- "$TOP" 2>/dev/null; then
    echo "ERROR [retake-failed] toplevel へ移動できない: $(path_field "$TOP")" >&2
    emit_incomparable retake-failed
  fi
  collect_git "$WORK/new" compare || rc=$?
  if [ "$rc" -eq 0 ]; then enumerate_untracked "$WORK/untracked.z" || rc=$?; fi
  if [ "$rc" -ne 0 ]; then
    echo "ERROR [retake-failed] 5 要素を取り直せない: $COLLECT_ERR" >&2
    emit_incomparable retake-failed
  fi
}
compare_scalar() { # $1=分類 $2=4-meta.txt のキー
  local old="${META[$2]:-}" new="${NEWMETA[$2]:-}"
  if [ "$old" != "$new" ]; then add_change "$1" "$old -> $new"; META_CHANGED=1; fi
}
declare -A NEWMETA=()
compare_meta() {
  local key val name sha k
  declare -A ro=() rn=()
  local oorder=() norder=()
  NEWMETA=()
  while IFS=$'\t' read -r key val; do
    case "$key" in
      head|index-tree|branch) NEWMETA[$key]="$val" ;;
      ref) sha="${val%% *}"; name="${val#* }"; k=":$name"; rn[$k]="$sha"; norder[${#norder[@]}]="$name" ;;
    esac
  done <"$WORK/new/4-meta.txt"
  while IFS=$'\t' read -r key val; do
    case "$key" in
      ref) sha="${val%% *}"; name="${val#* }"; k=":$name"; ro[$k]="$sha"; oorder[${#oorder[@]}]="$name" ;;
    esac
  done <"$STATE/snapshot/4-meta.txt"
  compare_scalar head head
  compare_scalar index-tree index-tree
  compare_scalar branch branch
  for name in ${oorder[@]+"${oorder[@]}"}; do
    k=":$name"
    if [ -z "${rn[$k]+x}" ]; then add_change refs "- ${ro[$k]} $name"; META_CHANGED=1
    elif [ "${rn[$k]}" != "${ro[$k]}" ]; then add_change refs "~ $name ${ro[$k]} -> ${rn[$k]}"; META_CHANGED=1
    fi
  done
  for name in ${norder[@]+"${norder[@]}"}; do
    k=":$name"
    if [ -z "${ro[$k]+x}" ]; then add_change refs "+ ${rn[$k]} $name"; META_CHANGED=1; fi
  done
  # hooks と config(検査の後から取り直しまでの間に変わった分)
  fs_meta_filter "$STATE/snapshot/4-meta.txt" >"$WORK/fsmeta.old2"
  fs_meta_filter "$WORK/new/4-meta.txt" >"$WORK/fsmeta.new2"
  if ! cmp -s "$WORK/fsmeta.old2" "$WORK/fsmeta.new2"; then
    diff_fs_meta "$WORK/fsmeta.old2" "$WORK/fsmeta.new2"
    META_CHANGED=1
  fi
}
compare_stash() {
  local line h k hit=0
  declare -A so=() sn=()
  if cmp -s "$STATE/snapshot/5-stash.txt" "$WORK/new/5-stash.txt"; then return 0; fi
  META_CHANGED=1
  while IFS= read -r line; do h="${line%% *}"; sn[":$h"]=1; done <"$WORK/new/5-stash.txt"
  while IFS= read -r line; do
    h="${line%% *}"; k=":$h"; so[$k]=1
    if [ -z "${sn[$k]+x}" ]; then add_change stash "- $(sanitize "$line")"; hit=1; fi
  done <"$STATE/snapshot/5-stash.txt"
  while IFS= read -r line; do
    h="${line%% *}"; k=":$h"
    if [ -z "${so[$k]+x}" ]; then add_change stash "+ $(sanitize "$line")"; hit=1; fi
  done <"$WORK/new/5-stash.txt"
  if [ "$hit" -eq 0 ]; then add_change stash "~ 並びだけが変わった"; fi
}
compare_manifest() { # 対象集合 = 現在の列挙 ∪ マニフェスト
  local kind mode val pf f key cur raw
  declare -A old=() seen=()
  local oorder=()
  while IFS=$'\t' read -r kind mode val pf; do
    if [ "$kind" = T ]; then continue; fi
    key=":$pf"
    old[$key]="$kind"$'\t'"$mode"$'\t'"$val"
    oorder[${#oorder[@]}]="$pf"
  done <"$STATE/manifest.tsv"
  while IFS= read -r -d '' f <&3; do
    if needs_quote "$f"; then pf="$(cquote "$f")"; else pf="$f"; fi
    key=":$pf"
    if [ -z "${old[$key]+x}" ]; then
      add_change untracked-added "$pf"; WT_CHANGED=1; REPORTED[$key]=1
      continue
    fi
    seen[$key]=1
    build_row "$f" compare
    cur="$ROW_KIND"$'\t'"$ROW_MODE"$'\t'"$ROW_VAL"
    if [ "$cur" != "${old[$key]}" ]; then
      add_change untracked-changed "$pf"; WT_CHANGED=1; REPORTED[$key]=1
      IFS=$'\t' read -r kind mode val <<<"${old[$key]}"
      add_base "$f" "$kind" "$mode" "$val"
    fi
  done 3<"$WORK/untracked.z" </dev/null
  for pf in ${oorder[@]+"${oorder[@]}"}; do
    key=":$pf"
    if [ -n "${seen[$key]+x}" ]; then continue; fi
    field_value "$pf"; raw="$UNQ"
    if ! in_scope "$raw"; then continue; fi
    add_change untracked-removed "$pf"; WT_CHANGED=1; REPORTED[$key]=1
    IFS=$'\t' read -r kind mode val <<<"${old[$key]}"
    add_base "$raw" "$kind" "$mode" "$val"
  done
}
compare_taskmd_row() { # マニフェストの T 行(タスク MD の実体)。除外の配下に在っても見る
  local m h cls="" st=unverified key
  if [ "$TASKMD_REL" != $'\001' ]; then
    if ! in_scope "$TASKMD_REL"; then return 0; fi
    key=":$(path_field "$TASKMD_REL")"
    if [ -n "${REPORTED[$key]+x}" ]; then return 0; fi   # 未追跡のエントリとして報告済み
  fi
  if ! verify_taskmd_body; then
    # 退避コピーを信頼できない。変化の有無は T 行の値で判定する(2026-09-17 決定 47)
    :
  else
    st=ok
  fi
  if [ -z "$T_SHA" ]; then return 0; fi
  if [ ! -e "$TASKMD_REAL" ] && [ ! -L "$TASKMD_REAL" ]; then cls=untracked-removed
  elif [ -L "$TASKMD_REAL" ] || [ ! -f "$TASKMD_REAL" ]; then cls=untracked-changed
  else
    m="$(stat_mode "$TASKMD_REAL" 2>/dev/null)" || m="-"
    h="$(sha256_file "$TASKMD_REAL" 2>/dev/null)" || h="-"
    if [ "$m" != "$T_MODE" ] || [ "$h" != "$T_SHA" ]; then cls=untracked-changed; fi
  fi
  if [ -n "$cls" ]; then
    add_change "$cls" "$(path_field "$TASKMD_REAL")"; WT_CHANGED=1
    CHANGES[${#CHANGES[@]}]="BASE=$(path_field "$STATE/taskmd-body")"$'\t'"$st"
  fi
}
cmd_compare() {
  local c h result code

  resolve_tools
  init_excl
  # ① 保護領域の検査 ② ダイジェスト照合 ③ hooks と config の検査 — ここまで git を 1 回も呼ばない
  open_state
  WORK="$(mktemp -d "$STATE/.work-XXXXXX")" || fail_internal "保護領域に一時ディレクトリを作れない"
  load_meta
  check_hooks_config # MUT:b
  # ④ 元repo Gitによる5要素と、明示した子の状態の再取得
  retake_elements # ANCHOR:retake

  # ①② は記録ファイルとのバイト比較
  if ! cmp -s "$STATE/snapshot/1-diff.bin" "$WORK/new/1-diff.bin"; then
    h="$(sha256_file "$WORK/new/1-diff.bin")"
    add_change tracked-diff "$(path_field "$STATE/snapshot/1-diff.bin")"$'\t'"$h"; WT_CHANGED=1
  fi
  if ! cmp -s "$STATE/snapshot/2-status.z" "$WORK/new/2-status.z"; then
    h="$(sha256_file "$WORK/new/2-status.z")"
    add_change status "$(path_field "$STATE/snapshot/2-status.z")"$'\t'"$h"; WT_CHANGED=1
  fi
  if ! cmp -s "$STATE/snapshot/6-contexts.txt" "$WORK/new/6-contexts.txt"; then
    h="$(sha256_file "$WORK/new/6-contexts.txt")"
    add_change submodule "$(path_field "$STATE/snapshot/6-contexts.txt")"$'\t'"$h"; WT_CHANGED=1
  fi
  # ③(⑤ 未追跡の対象集合の突き合わせ)と T 行
  compare_manifest
  compare_taskmd_row
  # ④⑤
  compare_meta
  compare_stash

  # ⑥ 決定表(§12-7)
  if [ "$RUN_RC" -eq 0 ]; then result=normal; code=0
  elif [ "$WT_CHANGED" -eq 1 ] || [ "$META_CHANGED" -eq 1 ]; then result=takeover; code=31
  else result=fallback; code=32
  fi
  printf 'RESULT=%s\n' "$result"
  if [ "$WT_CHANGED" -eq 1 ]; then printf 'WORKTREE_CHANGED=yes\n'; else printf 'WORKTREE_CHANGED=no\n'; fi
  if [ "$META_CHANGED" -eq 1 ]; then printf 'GITMETA_CHANGED=yes\n'; else printf 'GITMETA_CHANGED=no\n'; fi
  for c in ${CHANGES[@]+"${CHANGES[@]}"}; do printf '%s\n' "$c"; done
  case "$result" in
    takeover) echo "NOTE: 引き継ぎ(--run-rc=$RUN_RC で、作業ツリーか git メタに変化がある)" >&2 ;;
    fallback) echo "NOTE: 縮退(--run-rc=$RUN_RC で、作業ツリーにも git メタにも変化が無い)" >&2 ;;
  esac
  trap - ERR
  exit "$code"
}

# ═════════════════════════ taskmd-diff ═════════════════════════
cmd_taskmd_diff() {
  local kind_now real_now="" s1 s2

  resolve_tools
  init_excl
  open_state
  WORK="$(mktemp -d "$STATE/.work-XXXXXX")" || fail_internal "保護領域に一時ディレクトリを作れない"
  load_meta
  if ! verify_taskmd_body; then
    echo "ERROR [taskmd-body] 退避したタスク MD が記録と一致しない(完了条件の基準を信頼できない)" >&2
    printf 'REASON=taskmd-body\n'
    trap - ERR
    exit 33
  fi

  # 選択パス → 実体パスの対応の再確定(別の実体・リンク切れ・通常ファイルへの置換・symlink への置換)
  if [ -L "$TASKMD_SEL" ]; then kind_now=symlink; else kind_now=file; fi
  if [ -e "$TASKMD_SEL" ] && real_path "$TASKMD_SEL"; then real_now="$RP"; fi
  if [ "$kind_now" != "$TASKMD_KIND" ] || [ -z "$real_now" ] || [ "$real_now" != "$TASKMD_REAL" ] \
     || [ -L "$real_now" ] || [ ! -f "$real_now" ]; then
    echo "NOTE: タスク MD の選択パスと実体の対応が変わった(記録: $TASKMD_KIND → $(path_field "$TASKMD_REAL") / 現在: $kind_now → $(path_field "${real_now:-(確定できない)}"))" >&2
    printf 'TASKMD=mapping-changed\nDOD=%s\n' "$(path_field "$STATE/taskmd-body")"
    trap - ERR
    exit 35
  fi

  if [ -r "$real_now" ] && cmp -s "$STATE/taskmd-body" "$real_now"; then
    printf 'TASKMD=same\nDOD=%s\n' "$(path_field "$STATE/taskmd-body")"
    return 0
  fi
  # チェックマークだけを潰す正規化を両者に掛けて比べる(それ以外は一切正規化しない — 見逃すより止める)。
  # 正規化は 1 バイトを 1 バイトに置き換えるだけなので、サイズが違えば本文の変更
  if [ -r "$real_now" ]; then
    s1="$(stat_size "$STATE/taskmd-body")" || s1="a"
    s2="$(stat_size "$real_now")" || s2="b"
    LC_ALL=C sed -E "$NORM_SED" <"$STATE/taskmd-body" >"$WORK/before.norm"
    LC_ALL=C sed -E "$NORM_SED" <"$real_now" >"$WORK/after.norm"
    if [ "$s1" = "$s2" ] && cmp -s "$WORK/before.norm" "$WORK/after.norm"; then
      printf 'TASKMD=checks-only\nDOD=%s\n' "$(path_field "$STATE/taskmd-body")"
      return 0
    fi
  fi
  echo "NOTE: タスク MD の要件本文が変わっている(チェック状態の更新だけではない)" >&2
  printf 'TASKMD=body-changed\nDOD=%s\n' "$(path_field "$STATE/taskmd-body")"
  trap - ERR
  exit 34
}

# ═════════════════════════ restore-taskmd ═════════════════════════
restore_fail() { # $1=REASON 残り=説明。helper 起動前の結果だけを報告する
  local reason="$1"; shift
  echo "ERROR [$reason] $*" >&2
  printf 'RESTORED=no\nTOUCHED=%s\nTEMP=%s\nREASON=%s\n' "$TOUCHED_STATE" "${RESTORE_TEMP_STATE:-none}" "$reason"
  trap - ERR
  exit 33
}
restore_exec_cleanup() {
  # この入口は WORK / NEW_STATE を作らない。由来・作成時 inode を保持していない
  # 非空値を名前だけで削除しない。将来の変更で所有物を増やす場合はここも更新する。
  if [ -n "${WORK:-}" ] || [ -n "${NEW_STATE:-}" ]; then
    trap - EXIT ERR TERM INT HUP
    RESTORE_TEMP_STATE=unknown
    restore_fail cleanup-failed "復元入口が所有を確認できない一時物を保持したため、引渡しを停止した"
  fi
  WORK=""; NEW_STATE=""
}
cmd_restore_taskmd() {
  local kind mode val pf extra python_path cp_path helper helper_dir

  resolve_tools
  init_excl
  open_state
  load_meta
  # T 行の期待値だけを読む。退避本文は helper が同じ fd と有限予算で読む。
  if ! IFS=$'\t' read -r kind mode val pf extra <"$STATE/manifest.tsv"; then
    restore_fail taskmd-body "退避した本文の記録を読めない"
  fi
  if [ "$kind" != T ] || [ -n "$extra" ] || [[ ! "$mode" =~ ^[0-7]{1,4}$ ]] || [[ ! "$val" =~ ^[0-9a-f]{64}$ ]]; then
    restore_fail taskmd-body "退避した本文の記録形式が不正"
  fi
  field_value "$pf"
  T_MODE="$mode"; T_SHA="$val"; T_PATH="$UNQ"
  [ "$T_PATH" = "$TASKMD_REAL" ] || restore_fail taskmd-body "退避した本文の実体パスが記録と一致しない"

  python_path="$(command -v python3)" || restore_fail runtime-unavailable "復元に必要な Python 3 が無い"
  real_path "$python_path" || restore_fail runtime-unavailable "Python 3 の実体を取得できない"
  python_path="$RP"
  cp_path="$(command -v cp)" || restore_fail runtime-unavailable "復元に必要な GNU cp が無い"
  real_path "$cp_path" || restore_fail runtime-unavailable "cp の実体を取得できない"
  cp_path="$RP"
  [ -f "$python_path" ] && [ -x "$python_path" ] && [ -f "$cp_path" ] && [ -x "$cp_path" ] || restore_fail runtime-unavailable "必要な実行物を起動できない"
  split_path "${BASH_SOURCE[0]}"
  abs_existing_dir "$SP_DIR" || restore_fail runtime-unavailable "同梱 helper の場所を取得できない"
  helper_dir="$RP"
  helper="$helper_dir/restore-taskmd.py"
  [ -f "$helper" ] && [ -r "$helper" ] && [ ! -L "$helper" ] || restore_fail runtime-unavailable "同梱 helper を読めない"
  if ! "$python_path" -I -B -c 'import runpy,sys; runpy.run_path(sys.argv[1], run_name="restore_taskmd_check")' "$helper" >/dev/null 2>&1; then
    restore_fail runtime-unavailable "Python の必要機能か同梱 helper を読み込めない"
  fi
  restore_exec_cleanup
  # 成功した exec の後は helper だけが結果を所有する。失敗時だけ shell が報告する。
  trap - EXIT ERR TERM INT HUP
  shopt -s execfail
  # 非対話 bash では if の中でも exec 失敗が errexit を発火する版がある。
  # execfail と併せてこの引渡し区間だけ解除し、失敗は必ず固定形式へ変換する。
  set +e
  exec "$python_path" -I -B "$helper" restore --source "$STATE/taskmd-body" \
      --destination "$TASKMD_REAL" --expected-mode "$T_MODE" --expected-sha256 "$T_SHA" --cp "$cp_path"
  # 成功した exec はここへ戻らない。
  restore_fail runtime-unavailable "復元 helper を起動できない"
}

# ═════════════════════════ cleanup ═════════════════════════
cmd_cleanup() {
  init_excl
  if ! check_state_dir "$STATE_ARG"; then
    fail_usage "--state が保護領域の guard-* でない・無い・symlink(何も消していない): $(path_field "$STATE_ARG")"
  fi
  rm -rf -- "$STATE" || fail_internal "保護領域を消せない: $(path_field "$STATE")"
  echo "NOTE: 保護領域を消した: $(path_field "$STATE")" >&2
}

# ── 引数解析 ──
if [ $# -eq 0 ]; then arg_error "サブコマンドが必要です(take / config-check / compare / taskmd-diff / restore-taskmd / cleanup)"; fi
case "$1" in
  take|config-check|compare|taskmd-diff|restore-taskmd|cleanup) SUB="$1"; shift ;;
  -h|--help) usage; trap - ERR; exit 0 ;;
  *) arg_error "不明なサブコマンド: $1" ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    --cwd) need_val "$1" "$#"; CWD="$2"; GIVEN="$GIVEN$1 "; shift 2 ;;
    --task-md) need_val "$1" "$#"; TASK_MD="$2"; GIVEN="$GIVEN$1 "; shift 2 ;;
    --state) need_val "$1" "$#"; STATE_ARG="$2"; GIVEN="$GIVEN$1 "; shift 2 ;;
    --manifest-sha256) need_val "$1" "$#"; MAN_SHA="$2"; GIVEN="$GIVEN$1 "; shift 2 ;;
    --snapshot-sha256) need_val "$1" "$#"; SNAP_SHA="$2"; GIVEN="$GIVEN$1 "; shift 2 ;;
    --run-rc) need_val "$1" "$#"; RUN_RC="$2"; GIVEN="$GIVEN$1 "; shift 2 ;;
    -h|--help) usage; trap - ERR; exit 0 ;;
    *) arg_error "不明な引数: $1" ;;
  esac
done
case "$SUB" in
  take) ALLOWED=" --cwd --task-md " ;;
  config-check) ALLOWED=" --cwd --state --manifest-sha256 --snapshot-sha256 " ;;
  compare) ALLOWED=" --cwd --state --manifest-sha256 --snapshot-sha256 --run-rc " ;;
  taskmd-diff|restore-taskmd) ALLOWED=" --cwd --state --manifest-sha256 --snapshot-sha256 " ;;
  cleanup) ALLOWED=" --state " ;;
esac
for opt in $GIVEN; do
  case "$ALLOWED" in *" $opt "*) : ;; *) arg_error "$SUB では $opt を指定できない" ;; esac
done
for opt in $ALLOWED; do
  case "$GIVEN" in *" $opt "*) : ;; *) arg_error "$SUB には $opt が必要です" ;; esac
done
case " $GIVEN" in *" --cwd "*) [ -n "$CWD" ] || arg_error "--cwd が空です" ;; esac
case " $GIVEN" in *" --task-md "*) [ -n "$TASK_MD" ] || arg_error "--task-md が空です" ;; esac
case " $GIVEN" in *" --state "*) [ -n "$STATE_ARG" ] || arg_error "--state が空です" ;; esac
case " $GIVEN" in *" --manifest-sha256 "*) [ -n "$MAN_SHA" ] || arg_error "--manifest-sha256 が空です" ;; esac
case " $GIVEN" in *" --snapshot-sha256 "*) [ -n "$SNAP_SHA" ] || arg_error "--snapshot-sha256 が空です" ;; esac
if [ "$SUB" = compare ]; then
  case "$RUN_RC" in ''|*[!0-9]*) arg_error "--run-rc は 0 以上の十進整数: '$RUN_RC'" ;; esac
fi

case "$SUB" in
  take) cmd_take ;;
  config-check) cmd_config_check ;;
  compare) cmd_compare ;;
  taskmd-diff) cmd_taskmd_diff ;;
  restore-taskmd) cmd_restore_taskmd ;;
  cleanup) cmd_cleanup ;;
esac
trap - ERR
exit 0
