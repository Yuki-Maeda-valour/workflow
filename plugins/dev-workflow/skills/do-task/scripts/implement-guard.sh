#!/usr/bin/env bash
# 外部 implementer の起動の前後で、作業ツリーと git メタ状態を保護する専用スクリプト(dev-workflow)。
# 仕様は ../references/external-runners.md の §12-2(作業ツリーとメタ状態の保護)・§12-3(外部の git
# 操作への対処)・§12-7(引き継ぎの発火条件)。implement-agent.sh(外部 implementer の起動)と対で使う:
# 起動の前に take、起動の後に compare → taskmd-diff →(引き継ぎなら)restore-taskmd、完了処理の後に
# cleanup。呼び出し側は起動と、終了コードによる分岐だけを行う。
# diff-snapshot.sh(レビュー用の diff スナップショット)とは別物で、共通部分の切り出しもしていない
# (external-runners.md §11 の決定 9。必要な部品は複製して持つ)。
#
# 使い方:
#   bash implement-guard.sh take           --cwd <dir> --task-md <path>
#   bash implement-guard.sh compare        --cwd <dir> --state <dir> --manifest-sha256 <hex> --snapshot-sha256 <hex> --run-rc <n>
#   bash implement-guard.sh taskmd-diff    --cwd <dir> --state <dir> --manifest-sha256 <hex> --snapshot-sha256 <hex>
#   bash implement-guard.sh restore-taskmd --cwd <dir> --state <dir> --manifest-sha256 <hex> --snapshot-sha256 <hex>
#   bash implement-guard.sh cleanup        --state <dir>
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
#   0   成功(take: 作成した / compare: 正常終了 / taskmd-diff: 変化なしかチェック状態の更新だけ /
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
#   33  比較不能(compare)/ 照合・復元の失敗(taskmd-diff・restore-taskmd)。人の判断を仰ぐ
#   34  要件本文の変更(taskmd-diff)
#   35  選択パスと実体の対応の変化(taskmd-diff)
#
# stdout(機械可読の `KEY=VALUE` 行。値は 1 行。**パスの値は C 風引用** — タブ・改行・その他の制御文字・
#         `"`・`\` を含むか先頭が `"` のときだけ `"…"` で囲み `\t` `\n` `\\` `\"`、その他の制御文字は
#         8 進 `\ooo`。診断は stderr の `NOTE:` / `ERROR [理由コード]`):
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
#       tracked-diff / status   <起動前の記録ファイルのパス>\t<現在値の sha256>
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
#       REASON=manifest-digest|snapshot-digest|state-missing|hooks-changed|config-changed|retake-failed
#       GIT_SKIPPED=yes|no     (yes = git を 1 回も呼んでいない。retake-failed は git を呼んだ後の失敗なら no)
#   taskmd-diff:
#     TASKMD=same|checks-only|body-changed|mapping-changed
#     DOD=<起動前のタスク MD のコピーのパス>(以後の工程が完了条件の基準として読む)
#     exit 33 のとき REASON=digest|taskmd-body
#   restore-taskmd:
#     RESTORED=yes|no
#     TOUCHED=none|copied|deleted|deleted+copied
#                               (実ツリーに対して行ったこと。「削除してから cp -a」なので、削除の後の
#                                コピーの失敗時は実ツリーが既に変わっている。**復元先がもともと存在
#                                しなければ削除を行わないので copied**)
#     exit 33 のとき REASON=digest|taskmd-body|dest-symlink|dest-dir|delete-failed|copy-failed|verify-failed
#   REASON(taskmd-diff・restore-taskmd)の意味:
#     digest         保護領域の検査かダイジェスト照合に失敗した
#     taskmd-body    退避したタスク MD の実体照合(種別・モード・sha256)に失敗した
#     dest-symlink   復元先の親ディレクトリの実体パスが記録と一致しない(途中の階層が symlink に
#                    差し替えられた・消えた)、または復元先そのものが symlink
#     dest-dir       復元先がディレクトリに置き換えられている
#     delete-failed  同名エントリの削除に失敗した(TOUCHED=none)
#     copy-failed    cp -a に失敗した(TOUCHED=deleted。復元先がもともと無ければ none)
#     verify-failed  復元後の sha256 が一致しない(TOUCHED=deleted+copied。同じく copied)
#
# 保護領域:
#   解決順は `$XDG_STATE_HOME` → `$HOME/.local/state` → `$HOME/.cache`。解決後の実体パスが `/tmp`・
#   `$TMPDIR`・toplevel の配下なら次の候補へ進み、全滅なら縮退 `no-protected-area`。
#   **候補はまだ存在しなくてよい**。その場合は存在する最も深い親を物理的に解決し、まだ無い分は
#   語彙的に正規化して(`.` は捨て、`..` は 1 つ前の要素と相殺する)繋いだ絶対パスで判定してから作る。
#   **棄却する候補には mkdir も chmod もしない**(相殺しきれない `..` が残る候補も棄却する)。
#   `<候補>/dev-workflow/guard-XXXXXX/` を 0700 で作る。レイアウト:
#     snapshot/1-diff.bin  snapshot/2-status.z  snapshot/4-meta.txt  snapshot/5-stash.txt
#     snapshot/taskmd.txt(選択パス・実体パス・種別)
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
#   ① `diff --binary --no-ext-diff --no-textconv <HEAD または空ツリー>`(`-c diff.autoRefreshIndex=false
#      -c core.ignoreStat=false` つき。コミット 0 の repo は空ツリーと比べる)
#   ② `status --porcelain=v1 -z -uall --ignore-submodules=dirty`(`GIT_OPTIONAL_LOCKS=0`)
#   ③ マニフェスト = `ls-files -o --exclude-standard -z` のうち除外の配下でないもの + タスク MD の実体。
#      **タスク MD は除外より優先する**(pathspec の除外は打ち消せないので、除外の配下に在るタスク MD は
#      ①② には現れず、③ の T 行が単独で担う)
#   ④ HEAD / index の tree(**使い捨ての index** で `write-tree`)/ ブランチ名 / refs 全体 /
#      hooks ディレクトリ(git が実際に hook を探す場所)の全エントリのダイジェスト /
#      `config` と `config.worktree` の sha256(無いファイルは `(無し)`)
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
  cleanup_tmp
  if [ "$SUB" = restore-taskmd ]; then printf 'RESTORED=no\nTOUCHED=%s\n' "$TOUCHED_STATE"; fi
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

# ── 比較の対象か(除外 = ログの置き場。**タスク MD は除外より優先する**。決定 50)──
in_scope() { # $1=toplevel 相対パス → 0: 対象 / 1: 除外
  if [ "$1" = "$TASKMD_REL" ] || [ "$1" = "$TASKMD_SEL_REL" ]; then return 0; fi # MUT:g
  case "$1" in "$EXCL"|"$EXCL"/*) return 1 ;; esac
  return 0
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
file_digest() { # $1=パス → sha256 か `(無し)` か `(読めない)`
  local h
  if [ ! -e "$1" ] && [ ! -L "$1" ]; then printf '(無し)'; return 0; fi
  if [ -f "$1" ] && [ -r "$1" ] && h="$(sha256_file "$1")" && [ -n "$h" ]; then printf '%s' "$h"; return 0; fi
  printf '(読めない)'
}
fs_meta_lines() { # $1=hooks のパス $2=config のパス $3=config.worktree のパス(いずれも絶対)→ stdout
  local hp="$1" hreal="(無し)" hdig="(無し)" list="$WORK/hooks.list"
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
  printf 'config-path\t%s\n' "$(path_field "$2")"
  printf 'config-digest\t%s\n' "$(file_digest "$2")"
  printf 'config-worktree-path\t%s\n' "$(path_field "$3")"
  printf 'config-worktree-digest\t%s\n' "$(file_digest "$3")"
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
  head="$(git "${GIT_PRE[@]}" rev-parse --verify --quiet HEAD 2>/dev/null)" || rc=$?
  case "$rc" in
    0) if [ -z "$head" ]; then COLLECT_ERR="rev-parse HEAD が空を返した"; return 1; fi ;;
    1) head="(HEAD 無し)" ;;
    *) COLLECT_ERR="rev-parse --verify HEAD が失敗した(rc=$rc)"; return 1 ;;
  esac

  # ④ index の tree。ユーザーの index は読むだけにして、使い捨ての index で write-tree する
  # (素の write-tree は cache-tree を書き込んで .git/index のバイト列を変える)
  if ! git "${GIT_PRE[@]}" ls-files -s -z >"$WORK/stage.z" 2>"$WORK/git.err"; then
    COLLECT_ERR="ls-files -s が失敗した"; return 1
  fi
  rm -f -- "$WORK/index.tmp"
  if ! GIT_INDEX_FILE="$WORK/index.tmp" git "${GIT_PRE[@]}" update-index -z --index-info <"$WORK/stage.z" 2>"$WORK/git.err"; then
    COLLECT_ERR="使い捨ての index を作れない"; return 1
  fi
  rc=0
  tree="$(GIT_INDEX_FILE="$WORK/index.tmp" git "${GIT_PRE[@]}" write-tree 2>"$WORK/git.err")" || rc=$? # MUT:d
  if [ "$rc" -ne 0 ] || [ -z "$tree" ]; then
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
  branch="$(git "${GIT_PRE[@]}" symbolic-ref -q HEAD 2>/dev/null)" || rc=$?
  case "$rc" in
    0) : ;;
    1) branch="(detached)" ;;
    *) COLLECT_ERR="symbolic-ref HEAD が失敗した(rc=$rc)"; return 1 ;;
  esac
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
  if [ "$head" = "(HEAD 無し)" ]; then base="$(git "${GIT_PRE[@]}" hash-object -t tree /dev/null 2>/dev/null)" || base=""; fi # MUT:h
  if [ -z "$base" ]; then COLLECT_ERR="空ツリーの ID を計算できない"; return 1; fi
  if ! git "${GIT_PRE[@]}" "${DIFF_CFG[@]}" diff --binary --no-ext-diff --no-textconv "$base" -- . ":(exclude,literal)$EXCL" >"$out/1-diff.bin" 2>"$WORK/git.err"; then
    COLLECT_ERR="diff が失敗した: $(head -n 1 "$WORK/git.err" 2>/dev/null | tr '[:cntrl:]' '?')"; return 1
  fi

  # ② 全状態の一覧
  if ! GIT_OPTIONAL_LOCKS=0 git "${GIT_PRE[@]}" -c core.ignoreStat=false status --porcelain=v1 -z -uall --ignore-submodules=dirty -- . ":(exclude,literal)$EXCL" >"$out/2-status.z" 2>"$WORK/git.err"; then
    COLLECT_ERR="status が失敗した: $(head -n 1 "$WORK/git.err" 2>/dev/null | tr '[:cntrl:]' '?')"; return 1
  fi

  # ⑤ stash の二段取得(書式は固定する。既定の書式は log.date / core.abbrev で変わる)
  rc=0
  git "${GIT_PRE[@]}" rev-parse --verify --quiet refs/stash >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0)
      if ! git "${GIT_PRE[@]}" reflog show --format='%H %gd %gs' refs/stash >"$out/5-stash.txt" 2>"$WORK/git.err"; then
        COLLECT_ERR="reflog show refs/stash が失敗した"; return 1
      fi ;;
    1) printf 'stash 無し\n' >"$out/5-stash.txt" || { COLLECT_ERR="5-stash.txt を書けない"; return 1; } ;;
    *) COLLECT_ERR="rev-parse refs/stash が失敗した(rc=$rc)"; return 1 ;;
  esac
  return 0
}

# ③ の対象集合を NUL 区切り・LC_ALL=C のパス順で $1 へ書く(同じく `||` の左辺で呼ぶ)
enumerate_untracked() {
  local out="$1" f
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
      h="$(sha256_file "$dest" 2>/dev/null)" || h=""      # 退避コピー側の生バイト(決定 39)
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
    compare) echo "ERROR [$reason] $*" >&2; emit_incomparable "$reason" ;;
    taskmd-diff) echo "ERROR [digest] $*" >&2; printf 'REASON=digest\n' ;;
    restore-taskmd) echo "ERROR [digest] $*" >&2; printf 'RESTORED=no\nTOUCHED=none\nREASON=digest\n' ;;
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

  # タスク MD の実体の確定(symlink なら辿る。決定 50)
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

  # 退避: まずタスク MD の実体(追跡済み・ignore 済み・リポジトリ外でも必ず。決定 46・49)
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
check_hooks_config() { # 記録済みの実体パスから**ファイルの読み取りだけ**で再計算する(git を呼ばない)
  fs_meta_filter "$STATE/snapshot/4-meta.txt" >"$WORK/fsmeta.old"
  fs_meta_lines "${META[hooks-path]}" "${META[config-path]}" "${META[config-worktree-path]}" >"$WORK/fsmeta.raw"
  fs_meta_filter "$WORK/fsmeta.raw" >"$WORK/fsmeta.new"
  if cmp -s "$WORK/fsmeta.old" "$WORK/fsmeta.new"; then return 0; fi
  diff_fs_meta "$WORK/fsmeta.old" "$WORK/fsmeta.new"
  if [ "$FS_HOOKS_CHANGED" -eq 1 ]; then
    echo "ERROR [hooks-changed] hooks が起動前から変わっている(以後の git を打たない)" >&2
    emit_incomparable hooks-changed
  fi
  echo "ERROR [config-changed] リポジトリの config が起動前から変わっている(以後の git を打たない)" >&2
  emit_incomparable config-changed
}
retake_elements() { # 5 要素のうち git で取るものを take と同じ取り方で取り直す
  local rc=0
  mkdir -- "$WORK/new"
  if ! CDPATH= cd -P -- "$TOP" 2>/dev/null; then
    echo "ERROR [retake-failed] toplevel へ移動できない: $(path_field "$TOP")" >&2
    emit_incomparable retake-failed
  fi
  GIT_USED=1
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
    # 退避コピーを信頼できない。変化の有無は T 行の値で判定する(決定 47)
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
  # ④ 5 要素の再取得(ここから先は git を呼ぶ)
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
restore_fail() { # $1=REASON 残り=説明
  local reason="$1"; shift
  echo "ERROR [$reason] $*" >&2
  printf 'RESTORED=no\nTOUCHED=%s\nREASON=%s\n' "$TOUCHED_STATE" "$reason"
  trap - ERR
  exit 33
}
check_restore_dest() { # 復元先の検査(退避物の実体照合では防げない — 照合の対象は復元先の経路ではない)
  local dest="$TASKMD_REAL" parent parent_now=""
  split_path "$dest"
  parent="$SP_DIR"
  if abs_existing_dir "$parent"; then parent_now="$RP"; fi
  if [ -z "$parent_now" ] || [ "$parent_now" != "$parent" ]; then
    restore_fail dest-symlink "復元先の親ディレクトリの実体パスが記録と一致しない(途中の階層が symlink に差し替えられたか、消えた): $(path_field "$parent")"
  fi
  if [ -L "$dest" ]; then restore_fail dest-symlink "復元先が symlink になっている: $(path_field "$dest")"; fi
  if [ -d "$dest" ]; then restore_fail dest-dir "復元先がディレクトリに置き換えられている: $(path_field "$dest")"; fi
}
cmd_restore_taskmd() {
  local h

  resolve_tools
  init_excl
  open_state
  load_meta
  if ! verify_taskmd_body; then restore_fail taskmd-body "退避したタスク MD が記録と一致しない(改変された退避物で実ツリーを上書きしない)"; fi
  check_restore_dest # MUT:e
  # 同名エントリを削除してから cp -a(種別衝突で cp が黙って別の場所へ入れるのを防ぐ)。
  # **もともと存在しなければ削除しない** — TOUCHED= は「その時点までに実ツリーへ何をしたか」を
  # 人へ伝える値なので、消していないものを消したと報告しない
  if [ -e "$TASKMD_REAL" ] || [ -L "$TASKMD_REAL" ]; then
    if ! rm -f -- "$TASKMD_REAL" 2>/dev/null; then restore_fail delete-failed "復元先の同名エントリを削除できない: $(path_field "$TASKMD_REAL")"; fi
    TOUCHED_STATE="deleted"
  fi
  if ! cp -a -- "$STATE/taskmd-body" "$TASKMD_REAL" 2>/dev/null; then restore_fail copy-failed "退避コピーからの復元に失敗した(TOUCHED= が、その時点までに実ツリーへ行ったことを表す): $(path_field "$TASKMD_REAL")"; fi
  if [ "$TOUCHED_STATE" = deleted ]; then TOUCHED_STATE="deleted+copied"; else TOUCHED_STATE="copied"; fi
  h="$(sha256_file "$TASKMD_REAL" 2>/dev/null)" || h=""
  if [ "$h" != "$T_SHA" ]; then restore_fail verify-failed "復元後の sha256 が記録と一致しない: $(path_field "$TASKMD_REAL")"; fi
  printf 'RESTORED=yes\nTOUCHED=%s\n' "$TOUCHED_STATE"
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
if [ $# -eq 0 ]; then arg_error "サブコマンドが必要です(take / compare / taskmd-diff / restore-taskmd / cleanup)"; fi
case "$1" in
  take|compare|taskmd-diff|restore-taskmd|cleanup) SUB="$1"; shift ;;
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
  compare) cmd_compare ;;
  taskmd-diff) cmd_taskmd_diff ;;
  restore-taskmd) cmd_restore_taskmd ;;
  cleanup) cmd_cleanup ;;
esac
trap - ERR
exit 0
