#!/usr/bin/env bash
# 外部 CLI を implementer として起動する専用アダプタ(dev-workflow)。
# 仕様は ../references/external-runners.md の §12(実装委託の契約)。**CLI 面の唯一の正本は §12-8** で、
# そこに載らないオプションは実装しない(契約とスクリプトの同期義務が成立しなくなるため)。
# 呼び出し側(skill)が設定を解決し、このスクリプトには引数で渡す(**このスクリプトは YAML を読まない**)。
#
# レビュー用の review-agent.sh とは**独立したスクリプト**であり、共通部分の切り出しはしていない
# (external-runners.md §11 の決定 9。既存の回帰テストを無傷に保つため)。
#
# 信頼モデル(external-runners.md §1・§12-6):
#   - 実装用の既定表(下記 default_command / default_writeflag / default_probe_flag)に**無いランナー名は
#     受け付けない**(usage エラー)。レビュー経路と違い、既定表外を受け付ける経路そのものを持たない
#   - 起動コマンド・書き込みフラグ・プローブ用フラグの**上書き引数を設けない**
#     (--command / --writeflag に相当する引数は存在しない)
#
# 責務分界(external-runners.md §5・§12-2・§12-3):
#   - **このスクリプトは git を呼ばない。** 作業ツリー/ git メタのスナップショット・未追跡ファイルの退避・
#     起動後の再取得と比較・diff の提示・引き継ぎの判定は、すべて**呼び出し側の責務**である
#   - このスクリプトは `--cwd` で「どこに書かせるか」を受け取るだけで、その場所の状態は見ない
#
# 使い方:
#   bash implement-agent.sh --runner <名前> --prompt-file <パス> --cwd <ディレクトリ> [オプション]
#
# オプション(§12-8 の表がそのまま契約。増やさない):
#   --runner <名前>         ランナー名。**実装用既定表にある名前のみ**(無い名前・空・未指定は usage)
#   --prompt-file <パス>    委託プロンプトのファイル(必須。空・未指定は usage)
#   --cwd <ディレクトリ>    本実行の作業ディレクトリ = 書き込み範囲(**必須**。空・未指定は usage)。
#                           profile の root(未設定なら管理ルート)を呼び出し側が渡す。
#                           レビュー経路は省略を許すが、実装経路は向きが反転して必須
#   --model <名前>          ランナーへ渡すモデル名(省略・空はモデル指定フラグごと落とす)
#   --log-file <パス>       ログ出力先(既定 .claude/reviews/implementer-<runner>-iter<N>.md)
#   --probe-timeout <秒>    疎通プローブのタイムアウト(既定 60。0 は不可)
#   --run-timeout <秒>      本実行のタイムアウト(既定 1800。0 は不可)
#   --dry-run               解決後コマンドを表示して終了(**起動しない**。承認提示に使う)
#
#   ※ --command / --writeflag は**設けない**(信頼モデルの帰結。§12-8)。
#      ヘルプ照合のタイムアウトにも引数を設けない(既定 20 秒で固定)。
#      引数エラー時は使い方を stderr に出す。
#
# 起動形態:
#   既定では 1 回の起動で最長およそ 1900 秒(ヘルプ照合 20×最大 2 + プローブ 60 + 本実行 1800 + kill 猶予)
#   かかり、ホストのコマンド実行ツールの既定・上限をいずれも超える。
#   **呼び出し側はこのスクリプトを背景実行で起動すること**(external-runners.md §8「滞在時間」・§12-6)。
#
# 出力契約(§12-8。**正規化はしない** — 実装の成果物は作業ツリーへの書き込みであって stdout ではない):
#   exit 0  stdout = 外部ランナーの生出力をそのまま
#   exit 0  --dry-run のとき stdout = 解決後の起動コマンド
#   exit>0  stdout = 得られた分の生出力 / stderr に "ERROR [理由コード] 説明"
#   成否の判定は終了コードと ERROR 行だけで行う(stdout の内容では判定しない)。
#   「成果が残っているか」は呼び出し側がスナップショット比較で判定する(§12-7)。
#
# 終了コード: 2=usage 3=not-found 4=self-host 6=probe-failed 7=probe-timeout
#             8=run-failed 9=run-timeout 11=no-writemode 12=prompt-too-large 20=internal
#             128+N=シグナル N での中止(TERM=143 / HUP=129 / INT=130。理由コードは aborted)
#             (5=no-readonly と 10=parse-failed はレビュー専用でこの経路では出ない。
#              引き継ぎ専用の終了コードは無い — 通常の失敗コードを返すだけで、引き継ぎの判定は
#              呼び出し側がスナップショット比較と合わせて行う。§12-7)
#
# 空文字の扱い: --model "" / --log-file "" は「省略」として受理する。
#               --runner / --prompt-file / --cwd が空・未指定なら usage(2)。
#
# 中止(§12-8・決定 41): TERM / HUP / INT を受けたら**走行中の外部ランナーの子プロセスを
#         kill_tree で終了させてから**終了する。呼び出し側は背景実行するため中止操作は
#         シグナルとしてこのスクリプトに届き、放置すると外部 CLI が書き込みモードのまま
#         孤児として走り続け、§12-3 の「起動後の 5 要素の再取得と比較」がまだ書いている
#         プロセスの傍らで行われることになる。
#
# 副作用: ログファイルの書き出しと、2 つの一時領域の作成・削除のみ。
#         **ヘルプ照合・プローブ・本実行の出力を受ける scratch は「保護領域」に置く
#         (ただしプローブ用の cwd は除き、そこは読み取り専用モードで起動するため
#         mktemp -d の既定である /tmp のままでよい)**(§12-2 の解決順
#         $XDG_STATE_HOME → $HOME/.local/state → $HOME/.cache。決定 40)。
#         /tmp は書き込みモードのサンドボックスから書けるため、生出力をそこに置くと
#         **記録対象のプロセス自身が生出力を偽造できる**(実測)。
#         **本実行は書き込みモードで起動するため、--cwd 配下は外部ランナーに書き換えられる。**
#         「そのモードで本当にその範囲しか書かない」ことはランナー側の実装への信頼であり、
#         このスクリプトが保証できるものではない(限界は external-runners.md §12-4)。
# --- end usage ---
set -eEuo pipefail

RUNNER=""
MODEL=""
PROMPT_FILE=""
CWD=""
LOG_FILE=""
PROBE_TIMEOUT=60         # §12-6 の実装用の既定値
RUN_TIMEOUT=1800         # §12-6 の実装用の既定値(レビュー経路の 600 秒は適用しない)
HELP_TIMEOUT=20          # ヘルプ照合は短く抑える(引数で変えられない。§12-8)
KILL_GRACE=5             # TERM → KILL の猶予(3 経路で共通)
MAX_PROMPT_BYTES=100000  # argv 1 個の上限(Linux の MAX_ARG_STRLEN 128KiB)に対する安全側の閾値
DRY_RUN=0
CMD=()
WRITE_TOKENS=()
PROBE_TOKENS=()

STATE_DIR=""    # 本実行の出力を受ける scratch(保護領域。決定 40)
CHILD_PID=""    # 走行中の外部ランナー(またはその timeout ラッパ)の PID(決定 41)
RAW_OUT=""      # 中止時に「得られた分の生出力」を流すために先に宣言しておく

# シグナル処理・中止報告では、リダイレクト中でも必ず元の stdout / stderr へ出す。
# 本実行中は stdout が RAW_OUT へ向いており、退避しないと `cat "$RAW_OUT"` が自分自身へ
# 追記して止まらなくなる(決定 41 の実装上の罠)
exec 9>&1 8>&2

cleanup() {
  if [ -n "$STATE_DIR" ]; then rm -rf "$STATE_DIR"; fi
}
trap cleanup EXIT
trap 'ec=$?; echo "ERROR [internal] 予期しない失敗(終了コード $ec・行 $LINENO)" >&2; exit 20' ERR

log_line() {
  if [ -n "$LOG_FILE" ]; then printf '%s\n' "$1" >>"$LOG_FILE"; fi
}

log_block() { # $1=見出し $2=本文ファイル
  if [ -n "$LOG_FILE" ]; then
    {
      printf '\n### %s\n\n```\n' "$1"
      cat "$2" 2>/dev/null || true
      printf '\n```\n'
    } >>"$LOG_FILE"
  fi
}

die() { # $1=終了コード $2=理由コード 残り=説明
  code="$1"; reason="$2"; shift 2
  log_line ""
  log_line "**結果**: ERROR [$reason] $*"
  echo "ERROR [$reason] $*" >&2
  trap - ERR
  exit "$code"
}

# ヘッダのコメント全体を使い方として出す(番兵で範囲が自動追従する)
usage() { sed -n '2,/^# --- end usage ---$/p' "$0" | sed '$d' >&2; }

need_val() { # $1=オプション名 $2=残り引数の個数(値そのものは空文字も許す。必須性は個別に検査する)
  if [ "$2" -lt 2 ]; then
    echo "ERROR [usage] $1 に値が必要です" >&2
    usage
    trap - ERR
    exit 2
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --runner) need_val "$1" "$#"; RUNNER="$2"; shift 2 ;;
    --prompt-file) need_val "$1" "$#"; PROMPT_FILE="$2"; shift 2 ;;
    --cwd) need_val "$1" "$#"; CWD="$2"; shift 2 ;;
    --model) need_val "$1" "$#"; MODEL="$2"; shift 2 ;;
    --log-file) need_val "$1" "$#"; LOG_FILE="$2"; shift 2 ;;
    --probe-timeout) need_val "$1" "$#"; PROBE_TIMEOUT="$2"; shift 2 ;;
    --run-timeout) need_val "$1" "$#"; RUN_TIMEOUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    # --command / --writeflag はここに存在しない(§12-8「設けない引数」)。渡されたら下の * で usage になる
    *) echo "ERROR [usage] 不明な引数: $1" >&2; usage; trap - ERR; exit 2 ;;
  esac
done

fail_usage() { echo "ERROR [usage] $*" >&2; trap - ERR; exit 2; }

[ -n "$RUNNER" ] || fail_usage "--runner が必要です"
case "$RUNNER" in
  *[!A-Za-z0-9._-]*|"") fail_usage "--runner は英数字・ドット・ハイフン・アンダースコアのみ: '$RUNNER'" ;;
esac
# モデル名は profile(リポジトリが書ける)由来になりうる。フラグへ化けない形だけを受け付ける
if [ -n "$MODEL" ]; then
  case "$MODEL" in
    -*) fail_usage "--model に '-' で始まる値は使えない(フラグへの化けを防ぐ): '$MODEL'" ;;
    *[!A-Za-z0-9._:@/+-]*) fail_usage "--model に使える文字は英数字と . _ : @ / + - のみ: '$MODEL'" ;;
  esac
fi
[ -n "$PROMPT_FILE" ] || fail_usage "--prompt-file が必要です"
[ -f "$PROMPT_FILE" ] || fail_usage "プロンプトファイルが無い: $PROMPT_FILE"
# 空・空白のみのプロンプトで書き込みモードを起動しない(argv が空文字になり、外部ランナーは
# 「何をすればよいか」を持たないまま作業ツリーへの書き込み権限だけを得る)
[ -s "$PROMPT_FILE" ] || fail_usage "プロンプトファイルが空: $PROMPT_FILE"
if ! LC_ALL=C grep -q '[^[:space:]]' "$PROMPT_FILE" 2>/dev/null; then
  fail_usage "プロンプトファイルが空白文字だけでできている: $PROMPT_FILE"
fi
# --cwd は必須(§12-1・§12-8)。実装では「どこに書くか」が本質なので暗黙の既定を持たせない
[ -n "$CWD" ] || fail_usage "--cwd が必要です(実装経路では省略できない。書き込み範囲を呼び出し側が明示する)"
[ -d "$CWD" ] || fail_usage "--cwd が存在しない: $CWD"
# `[ -d ]` は親の検索権限だけで通る。実際に cd できるかは検索(実行)ビットで決まるので、
# 「起動直前に cd が失敗して internal(20) に化ける」経路を潰して usage 側へ寄せる
[ -x "$CWD" ] || fail_usage "--cwd に検索(実行)権限が無くて移動できない: $CWD"
# 0 は GNU timeout では「無制限」、フォールバックでは「即 kill」で意味が反転するため受け付けない
case "$PROBE_TIMEOUT" in ''|*[!0-9]*) fail_usage "--probe-timeout は正の秒数" ;; esac
case "$RUN_TIMEOUT" in ''|*[!0-9]*) fail_usage "--run-timeout は正の秒数" ;; esac
[ "$PROBE_TIMEOUT" -gt 0 ] || fail_usage "--probe-timeout に 0 は指定できない(無制限/即 kill で意味が反転する)"
[ "$RUN_TIMEOUT" -gt 0 ] || fail_usage "--run-timeout に 0 は指定できない(無制限/即 kill で意味が反転する)"

# ── 実装用既定表(信頼の基点。ここに無いランナーは受け付けない)──
# **../references/external-runners.md §12-6 の実装用既定表と二重管理になる。どちらかを変えたら必ず両方を直す**
# (implement-agent-selftest.sh が §12-6 の節を特定して行を拾い、--dry-run の出力と照合する)。
# 出典と確認日は同表の「確認状況」欄に記載する。
default_command() { # 「起動コマンド」列({model} は --model の値に置換。無ければ直前のフラグごと落とす)
  case "$1" in
    codex) printf '%s' 'codex exec --sandbox workspace-write -m {model}' ;;
    *) return 1 ;;
  esac
}

default_writeflag() { # 「書き込みフラグ」列(本実行のモード)。「フラグ 値」のトークン列
  case "$1" in
    codex) printf '%s' '--sandbox workspace-write' ;;
    *) return 1 ;;
  esac
}

default_probe_flag() { # 「プローブ用の読み取り専用フラグ」列(判定 4′ のモード)。「フラグ 値」のトークン列
  case "$1" in
    codex) printf '%s' '--sandbox read-only' ;;
    *) return 1 ;;
  esac
}

# 既定表外は受け付ける経路そのものを持たない(レビュー経路との最大の差。§12-8)
if ! default_command "$RUNNER" >/dev/null 2>&1; then
  fail_usage "ランナー '$RUNNER' は実装用既定表に無い(実装経路には既定表外を受け付ける経路が無い。external-runners.md §12-6 を参照)"
fi
TMPL="$(default_command "$RUNNER")"
WRITE_FLAG="$(default_writeflag "$RUNNER")"
PROBE_FLAG="$(default_probe_flag "$RUNNER")"
read -r -a WRITE_TOKENS <<<"$WRITE_FLAG"
read -r -a PROBE_TOKENS <<<"$PROBE_FLAG"
# 既定表そのものの形が壊れているのはスクリプト側の退行であって「書き込み範囲が未確立」ではない。
# 11(no-writemode)を返すと呼び出し側がランナーの仕様変更と読み違えるので internal(20) に寄せる
[ "${#WRITE_TOKENS[@]}" -ge 2 ] || die 20 internal "実装用既定表の書き込みフラグが「フラグ 値」の形になっていない: '$WRITE_FLAG'"
[ "${#PROBE_TOKENS[@]}" -ge 2 ] || die 20 internal "実装用既定表のプローブ用フラグが「フラグ 値」の形になっていない: '$PROBE_FLAG'"
WRITE_NAME="${WRITE_TOKENS[0]}"
WRITE_VALUE="${WRITE_TOKENS[$((${#WRITE_TOKENS[@]} - 1))]}"

# ホスト自身の CLI(design §5-5: 自ホストと同じ CLI をランナーとして起動しない)
# ベストエフォート判定。誤判定するときは DEV_WORKFLOW_HOST_CLI で上書きする。
#
# **立っている指標を「すべて」候補に入れる**(決定 42)。「先に一致したものを host とする」方式は、
# 指標が複数立つ環境(プラグイン等でホストの環境変数が別ホストのセッションへ漏れる構成は現実的)で
# 後ろの指標を隠し、**自ホストを別ホストと誤判定して自分自身を起動してしまう**(実測)。
# 実装用既定表のエントリは 1 件しかなく、この判定が唯一の防波堤になっている。
# `DEV_WORKFLOW_HOST_CLI` による明示上書きだけは最優先で、指定されたらそれ「だけ」を候補にする。
host_cli_candidates() { # 1 行 1 候補で出す(0 件のこともある)
  if [ -n "${DEV_WORKFLOW_HOST_CLI:-}" ]; then printf '%s\n' "$DEV_WORKFLOW_HOST_CLI"; return 0; fi
  if [ -n "${CLAUDECODE:-}" ]; then printf '%s\n' 'claude'; fi
  if [ -n "${CODEX_SANDBOX:-}" ]; then printf '%s\n' 'codex'; fi
  if [ -n "${CURSOR_AGENT:-}" ]; then printf '%s\n' 'cursor-agent'; fi
}

TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1 && timeout -k 1 1 true >/dev/null 2>&1; then
  TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1 && gtimeout -k 1 1 true >/dev/null 2>&1; then
  TIMEOUT_BIN="gtimeout"
fi
kill_tree() { # $1=pid。TERM → KILL_GRACE 秒 → KILL(timeout -k と同じ振る舞い)
  # フォールバック経路では monitor モードで起動するため pid == プロセスグループ ID。
  # グループ・子プロセス・本体の 3 方向へ撃つ(いずれかが空振りしても残骸を残さない)
  kt_pid="$1"
  kill -TERM "-$kt_pid" 2>/dev/null || true
  pkill -TERM -P "$kt_pid" 2>/dev/null || true
  kill -TERM "$kt_pid" 2>/dev/null || true
  sleep "$KILL_GRACE"
  kill -KILL "-$kt_pid" 2>/dev/null || true
  pkill -KILL -P "$kt_pid" 2>/dev/null || true
  kill -KILL "$kt_pid" 2>/dev/null || true
}

# ── 中止(決定 41)──
# 呼び出し側は背景実行するので、中止操作は TERM としてこのスクリプトに届く。子を道連れに
# しないと、外部 CLI が書き込みモード + --cwd のまま孤児として走り続け、§12-3 が義務づける
# 「起動後の 5 要素の再取得と比較」を**まだ作業ツリーに書いているプロセスの傍らで**行うことになる。
on_signal() { # $1=シグナル名 $2=終了コード(128 + シグナル番号)
  os_sig="$1"; os_code="$2"
  trap - TERM HUP INT ERR
  if [ -n "$CHILD_PID" ]; then kill_tree "$CHILD_PID"; CHILD_PID=""; fi
  log_line ""
  log_line "**中止**: シグナル $os_sig を受信したため外部ランナーの子プロセスを終了させた(終了コード $os_code)"
  # 中止時点までに得られた生出力は残す(§12-8「失敗時は得られた分の生出力」。引き継ぎの手がかり)
  if [ -n "$RAW_OUT" ] && [ -s "$RAW_OUT" ]; then
    log_block "生出力(中止時点まで)" "$RAW_OUT"
    cat "$RAW_OUT" >&9 2>/dev/null || true
  fi
  echo "ERROR [aborted] シグナル $os_sig を受信したため中止した(外部ランナーの子プロセスは終了させた)" >&8
  exit "$os_code"   # EXIT trap が一時領域を後始末する
}
# 非対話 shell の非同期ジョブは SIGINT を無視するため、INT は「保険」として捕捉する
trap 'on_signal TERM 143' TERM
trap 'on_signal HUP 129' HUP
trap 'on_signal INT 130' INT

# タイムアウト付き実行(3 経路すべて「TERM → KILL_GRACE 秒 → KILL」で統一)。124=タイムアウト
run_timeout() { # $1=秒 残り=コマンド
  rt_secs="$1"; shift
  rt_start="$(date +%s)"
  if [ -n "$TIMEOUT_BIN" ]; then
    rt_rc=0
    # **前景で待たずに背景 + wait にする**(決定 41)。前景実行だと bash は waitpid で塞がり、
    # シグナルの trap が子の終了後にしか走らないため、子を道連れにできない。
    # `wait` は trap で中断されるので、CHILD_PID を掴んだまま中止経路へ入れる
    # 退避した 8/9 は子へ渡さない(外部ランナーに余計な fd を継承させない)
    "$TIMEOUT_BIN" -k "$KILL_GRACE" "$rt_secs" "$@" 8>&- 9>&- &
    CHILD_PID=$!
    wait "$CHILD_PID" || rt_rc=$?
    CHILD_PID=""
    # TERM を無視するプロセスは -k の KILL で落ちるため 137/143 で返る。
    # 経過時間が指定秒を超えていればタイムアウト(124)に正規化する
    case "$rt_rc" in
      137|143)
        if [ "$(( $(date +%s) - rt_start ))" -ge "$rt_secs" ]; then rt_rc=124; fi
        ;;
    esac
    return "$rt_rc"
  fi
  # monitor モードで起動すると、そのジョブが独立したプロセスグループのリーダーになる
  # (pgid == pid)。これでタイムアウト時にグループごと止められる
  set -m 2>/dev/null || true
  "$@" 8>&- 9>&- &
  rt_pid=$!
  CHILD_PID="$rt_pid"
  set +m 2>/dev/null || true
  rt_waited=0
  while kill -0 "$rt_pid" 2>/dev/null; do
    if [ "$rt_waited" -ge "$rt_secs" ]; then
      kill_tree "$rt_pid"
      wait "$rt_pid" 2>/dev/null || true
      CHILD_PID=""
      return 124
    fi
    sleep 1
    rt_waited=$((rt_waited + 1))
  done
  rt_rc=0
  wait "$rt_pid" || rt_rc=$?
  CHILD_PID=""
  return "$rt_rc"
}

build_cmd() { # $1=プロンプト本文 → 配列 CMD を組む(常に既定表の「起動コマンド」= 書き込みモード)
  bc_prompt="$1"
  CMD=()
  bc_placed=0
  read -r -a bc_words <<<"$TMPL"
  for w in ${bc_words[@]+"${bc_words[@]}"}; do
    case "$w" in
      '{model}')
        if [ -n "$MODEL" ]; then
          CMD[${#CMD[@]}]="$MODEL"
        else
          bc_n=${#CMD[@]}
          if [ "$bc_n" -gt 0 ]; then
            bc_last="${CMD[$((bc_n - 1))]}"
            case "$bc_last" in
              -*) unset "CMD[$((bc_n - 1))]"; CMD=(${CMD[@]+"${CMD[@]}"}) ;;
            esac
          fi
        fi
        ;;
      '{prompt}') CMD[${#CMD[@]}]="$bc_prompt"; bc_placed=1 ;;
      *) CMD[${#CMD[@]}]="$w" ;;
    esac
  done
  if [ "$bc_placed" -ne 1 ]; then CMD[${#CMD[@]}]="$bc_prompt"; fi
}

# 判定 4′ 用: CMD の中の「書き込みフラグ 値」を「プローブ用フラグ 値」へ**対で**差し替える。
# 同じフラグ名に別の値が入る形(モードだけを落とす)なので、名前だけ・値だけの置換にしない
apply_probe_mode() {
  ap_pname="${PROBE_TOKENS[0]}"
  ap_pvalue="${PROBE_TOKENS[$((${#PROBE_TOKENS[@]} - 1))]}"
  ap_out=()
  ap_i=0
  ap_n=${#CMD[@]}
  ap_done=0
  while [ "$ap_i" -lt "$ap_n" ]; do
    ap_tok="${CMD[$ap_i]}"
    if [ "$ap_tok" = "$WRITE_NAME" ] && [ $((ap_i + 1)) -lt "$ap_n" ] && [ "${CMD[$((ap_i + 1))]}" = "$WRITE_VALUE" ]; then
      ap_out[${#ap_out[@]}]="$ap_pname"
      ap_out[${#ap_out[@]}]="$ap_pvalue"
      ap_i=$((ap_i + 2))
      ap_done=1
      continue
    fi
    if [ "$ap_tok" = "$WRITE_NAME=$WRITE_VALUE" ]; then
      ap_out[${#ap_out[@]}]="$ap_pname=$ap_pvalue"
      ap_i=$((ap_i + 1))
      ap_done=1
      continue
    fi
    ap_out[${#ap_out[@]}]="$ap_tok"
    ap_i=$((ap_i + 1))
  done
  CMD=(${ap_out[@]+"${ap_out[@]}"})
  [ "$ap_done" -eq 1 ] || return 1
  return 0
}

quote_cmd() { # 表示・ログ用(実行には使わない)
  qc_out=""
  for a in ${CMD[@]+"${CMD[@]}"}; do
    case "$a" in
      *[[:space:]]*) qc_out="$qc_out \"$(printf '%s' "$a" | cut -c1-60)…\"" ;;
      *) qc_out="$qc_out $a" ;;
    esac
  done
  printf '%s' "${qc_out# }"
}

# フラグ + 値が argv トークン列として実在するか(表示用文字列の部分一致に頼らない)。
# 意味に依存しない純粋な文字列照合なので、読み取り専用にも書き込みモードにも同じ形で使える
tokens_in_argv() { # $1=フラグ名 $2=値
  ti_name="$1"; ti_value="$2"
  ti_n=${#CMD[@]}
  ti_i=0
  while [ $((ti_i + 1)) -lt "$ti_n" ]; do
    if [ "${CMD[$ti_i]}" = "$ti_name" ] && [ "${CMD[$((ti_i + 1))]}" = "$ti_value" ]; then return 0; fi
    ti_i=$((ti_i + 1))
  done
  for a in ${CMD[@]+"${CMD[@]}"}; do
    if [ "$a" = "$ti_name=$ti_value" ]; then return 0; fi
  done
  return 1
}

# 実効の実行ファイル名(env / npx などのラッパーを 1 段はがす。ベストエフォート)
effective_bin() {
  eb_i=0
  eb_n=${#CMD[@]}
  while [ "$eb_i" -lt "$eb_n" ]; do
    eb_tok="${CMD[$eb_i]}"
    case "$eb_tok" in
      -*) eb_i=$((eb_i + 1)); continue ;;
    esac
    if [ "$eb_i" -gt 0 ]; then
      case "$eb_tok" in
        *=*) eb_i=$((eb_i + 1)); continue ;;
      esac
    fi
    eb_b="$(basename "$eb_tok")"
    case "$eb_b" in
      env|npx|bunx|pnpx|nice|stdbuf|command|time|xargs) eb_i=$((eb_i + 1)); continue ;;
      *) printf '%s' "$eb_b"; return 0 ;;
    esac
  done
  printf '%s' "$(basename "${CMD[0]}")"
}

# ── プローブの cwd は本実行と同じ(決定 43)──
# 使い捨ての一時ディレクトリを cwd にすると、**信頼していないディレクトリでの非対話実行を
# 拒否する**ランナーでプローブが必ず失敗する(実測〈2026-09-17〉: codex が
# `Not inside a trusted directory and --skip-git-repo-check was not specified.` で終了コード 1)。
# プローブは読み取り専用モードなので、cwd が実ツリーでも書き込みは起きない。
# 決定 40 の保護領域(本実行の出力を受ける scratch)は下記のとおり維持する
PROBE_CWD="$CWD"

# ── 一時領域: 本実行の出力を受ける scratch = 保護領域(決定 40)──
# 解決順は §12-2 と同じ($XDG_STATE_HOME → $HOME/.local/state → $HOME/.cache)。
# 名前だけでは要件を満たす保証にならないので、**解決後の実体パス**が
# /tmp・$TMPDIR・--cwd(= 外部の書き込み範囲)の配下でないことを検査してから使う。
real_dir() { ( cd "$1" 2>/dev/null && pwd -P ) || return 1; }
under_dir() { # $1=判定するパス $2=親候補(どちらも実体パス)
  [ -n "$2" ] || return 1
  case "$1" in "$2"|"$2"/*) return 0 ;; esac
  return 1
}
EXCL_TMP="$(real_dir /tmp || true)"
EXCL_TMPDIR=""
if [ -n "${TMPDIR:-}" ]; then EXCL_TMPDIR="$(real_dir "$TMPDIR" || true)"; fi
EXCL_CWD="$(real_dir "$CWD")" || die 20 internal "--cwd の実体パスを解決できない: $CWD"
STATE_BASE=""
STATE_TRIED=""
for cand in "${XDG_STATE_HOME:-}" "${HOME:-}/.local/state" "${HOME:-}/.cache"; do
  case "$cand" in ""|"/.local/state"|"/.cache") continue ;; esac
  cand="$cand/dev-workflow"
  STATE_TRIED="$STATE_TRIED $cand"
  mkdir -p "$cand" 2>/dev/null || continue
  chmod 700 "$cand" 2>/dev/null || true   # 機密を含みうる生出力が group/other から読めないように
  cand_real="$(real_dir "$cand")" || continue
  if under_dir "$cand_real" "$EXCL_TMP" || under_dir "$cand_real" "$EXCL_TMPDIR" \
     || under_dir "$cand_real" "$EXCL_CWD"; then
    continue
  fi
  STATE_BASE="$cand"
  break
done
[ -n "$STATE_BASE" ] || die 20 internal "本実行の生出力を置く保護領域を外部の書き込み範囲外に解決できない(試した候補:${STATE_TRIED:- なし}。/tmp・\$TMPDIR・--cwd の配下は使えない。\$XDG_STATE_HOME を書き込み範囲外へ設定する)"
STATE_DIR="$(mktemp -d "$STATE_BASE/implement-XXXXXX")" || die 20 internal "保護領域に一時ディレクトリを作れない: $STATE_BASE"

# ── ログの初期化(既定名は §12-8 の implementer-{ランナー}-iter{N} 形式)──
# 置き場が作れないケースを事前に潰す(生の mkdir エラー + internal(20) では理由が伝わらない)
if [ -z "$LOG_FILE" ]; then
  LOG_DIR=".claude/reviews"
  mkdir -p "$LOG_DIR" 2>/dev/null \
    || fail_usage "既定のログ置き場 '$LOG_DIR' を作れない(カレントディレクトリに書けないなら --log-file で置き場を指定する)"
  iter=1
  while [ -e "$LOG_DIR/implementer-${RUNNER}-iter${iter}.md" ]; do iter=$((iter + 1)); done
  LOG_FILE="$LOG_DIR/implementer-${RUNNER}-iter${iter}.md"
else
  LOG_DIR="$(dirname "$LOG_FILE")"
  mkdir -p "$LOG_DIR" 2>/dev/null \
    || fail_usage "--log-file の置き場 '$LOG_DIR' を作れない: $LOG_FILE"
fi
# 本実行とプローブは cd してから起動する(子 PID を掴むためサブシェルを使えない。決定 41)。
# 相対パスのままだと cd 後にログとプロンプトを見失うので、先に絶対パスへ正規化する
ORIG_PWD="$PWD"
case "$LOG_FILE" in /*) : ;;
  *) LOG_FILE="$(cd "$(dirname "$LOG_FILE")" && pwd -P)/$(basename "$LOG_FILE")" ;;
esac
case "$PROMPT_FILE" in /*) : ;;
  *) PROMPT_FILE="$(cd "$(dirname "$PROMPT_FILE")" && pwd -P)/$(basename "$PROMPT_FILE")" ;;
esac

{
  printf '# 外部ランナー実行記録(implementer): %s\n\n' "$RUNNER"
  printf -- '- 実行日時: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  printf -- '- ランナー: %s(実装用既定表: あり。既定表外を受け付ける経路は無い)\n' "$RUNNER"
  printf -- '- コマンド出所: 実装用既定表(上書き経路なし)\n'
  printf -- '- モデル: %s\n' "${MODEL:-(ランナー既定)}"
  printf -- '- 書き込みフラグ(本実行): %s\n' "$WRITE_FLAG"
  printf -- '- プローブ用の読み取り専用フラグ(判定 4′): %s\n' "$PROBE_FLAG"
  printf -- '- 渡した --cwd(本実行の書き込み範囲): %s\n' "$CWD"
  printf -- '- プローブの cwd: %s(本実行と同じ。読み取り専用モードで打つので書き込みは起きない。決定 43)\n' "$PROBE_CWD"
  printf -- '- スクリプトの一時領域(保護領域): %s(本実行の生出力を受ける。外部の書き込み範囲外。終了時に削除する)\n' "$STATE_DIR"
  printf -- '- プロンプトファイル: %s\n' "$PROMPT_FILE"
  printf -- '- タイムアウト: ヘルプ照合 %s 秒 / プローブ %s 秒 / 本実行 %s 秒(TERM → %s 秒 → KILL)\n' \
    "$HELP_TIMEOUT" "$PROBE_TIMEOUT" "$RUN_TIMEOUT" "$KILL_GRACE"
  printf -- '- 中止の扱い: TERM / HUP / INT を受けたら外部ランナーの子プロセスを終了させてから終了する(終了コード 128+N)\n'
  printf -- '- 起動形態: 呼び出し側は背景実行で起動する(最長およそ 1900 秒でホストのコマンド実行ツールの上限を超えるため)\n'
  printf -- '- git 操作: このスクリプトは git を呼ばない(スナップショット・退避・起動後の比較・diff 提示・引き継ぎ判定は呼び出し側の責務)\n'
  printf -- '- 限界: プローブは読み取り専用モードで打つため、通っても本実行が書き込みモードで通る保証はない(判定 3′ のヘルプ照合と併せて補完する)\n'
} >"$LOG_FILE"

# ── プロンプト長(argv 上限)の検査 ──
# **外部 CLI を 1 回も起動する前に行う**(以前はプローブの後にあり、必ず落ちると分かっている
# 実行のためにプローブ 1 回分を課金していた)。ログの初期化後なので理由は記録に残る
PROMPT_BYTES="$(wc -c <"$PROMPT_FILE" | tr -d ' ')"
if [ "$PROMPT_BYTES" -gt "$MAX_PROMPT_BYTES" ]; then
  die 12 prompt-too-large "プロンプトが ${PROMPT_BYTES} バイトで上限 ${MAX_PROMPT_BYTES} を超える(argv 1 個の上限 128KiB)。diff を直接貼らずパスで渡す"
fi

# ── 判定 1: 存在 ──
build_cmd "<プロンプト>"
BIN="${CMD[0]}"
EFF_BIN="$(effective_bin)"
RESOLVED="$(quote_cmd)"
log_line "- 解決後のコマンド: \`$RESOLVED\`"
command -v "$BIN" >/dev/null 2>&1 || die 3 not-found "ランナー '$RUNNER' の実行ファイル '$BIN' が PATH に無い"
log_line "- 実行ファイルの存在(判定 1): '$BIN' を確認"

# ── 判定 2: 自ホストと別 CLI か(design §5-5)──
# 候補集合のどれか 1 つにでも当たったら拒否する(決定 42)
BIN_BASE="$(basename "$BIN")"
HOST_CANDIDATES=""
while IFS= read -r hc; do
  [ -n "$hc" ] || continue
  HOST_CANDIDATES="$HOST_CANDIDATES $hc"
  if [ "$hc" = "$RUNNER" ] || [ "$hc" = "$EFF_BIN" ] || [ "$hc" = "$BIN_BASE" ]; then
    die 4 self-host "ホスト自身と同じ CLI('$hc')はランナーにできない(二重課金の回避。ホストのエージェント機構を使う)"
  fi
done <<EOF
$(host_cli_candidates)
EOF
log_line "- ホスト CLI 判定(判定 2・ベストエフォート): 候補 =${HOST_CANDIDATES:- (不明)}(いずれもランナーと別 CLI であることを確認)"

# ── 判定 3′: モード確立(書き込み範囲)──
# 意味はレビュー経路の判定 3 と反転する: 「書き込まない保証」ではなく
# 「意図した書き込みモードで動いている(より広いモードに化けていない)確認」。
# 機構は同じ 2 段照合(①argv トークン列 ②ヘルプにフラグ名と値が実在する)。
if ! tokens_in_argv "$WRITE_NAME" "$WRITE_VALUE"; then
  die 11 no-writemode "起動コマンドの argv に書き込みフラグ '$WRITE_FLAG' が無い(実装用既定表の退行。書き込み範囲が確立できないため起動しない)"
fi
log_line "- 書き込み範囲(argv 照合): '$WRITE_FLAG' を確認"

if [ "$DRY_RUN" -eq 0 ]; then
  HELP_OUT="$STATE_DIR/help.txt"
  : >"$HELP_OUT"
  help_rc=0
  run_timeout "$HELP_TIMEOUT" "$BIN" --help >>"$HELP_OUT" 2>&1 </dev/null || help_rc=$?
  if [ "$help_rc" -eq 124 ]; then
    die 7 probe-timeout "'$BIN --help' が ${HELP_TIMEOUT} 秒以内に応答しない(無応答)"
  fi
  help_found=0
  if grep -qw -- "$WRITE_NAME" "$HELP_OUT" 2>/dev/null && grep -qw -- "$WRITE_VALUE" "$HELP_OUT" 2>/dev/null; then help_found=1; fi
  if [ "$help_found" -eq 0 ] && [ ${#CMD[@]} -gt 1 ]; then
    SUB="${CMD[1]}"
    case "$SUB" in
      -*) : ;;
      *)
        help_rc=0
        run_timeout "$HELP_TIMEOUT" "$BIN" "$SUB" --help >>"$HELP_OUT" 2>&1 </dev/null || help_rc=$?
        if [ "$help_rc" -eq 124 ]; then
          die 7 probe-timeout "'$BIN $SUB --help' が ${HELP_TIMEOUT} 秒以内に応答しない(無応答)"
        fi
        if grep -qw -- "$WRITE_NAME" "$HELP_OUT" 2>/dev/null && grep -qw -- "$WRITE_VALUE" "$HELP_OUT" 2>/dev/null; then help_found=1; fi
        ;;
    esac
  fi
  if [ "$help_found" -ne 1 ]; then
    die 11 no-writemode "'$BIN' のヘルプに書き込みフラグ '$WRITE_NAME' と値 '$WRITE_VALUE' が見つからない(仕様変更の可能性。実装用既定表を更新する)"
  fi
  log_line "- 書き込み範囲(ヘルプ照合): '$WRITE_NAME' と値 '$WRITE_VALUE' を確認"
else
  log_line "- 書き込み範囲(ヘルプ照合): 未実施(--dry-run)"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  log_line "- dry-run: 静的検査のみ(起動しない)"
  printf '%s\n' "$RESOLVED"
  exit 0
fi

# 本実行・プローブの出力は**保護領域**に置く(決定 40)。/tmp に置くと、記録の対象である
# 本実行のプロセス自身がこれらを書き換えられる = 生出力を偽造できる(実測)。
# 生出力は §12-8 が「引き継ぎ時に内蔵 implementer へ渡す唯一の手がかり」と位置づけた成果物
PROBE_OUT="$STATE_DIR/probe.txt"
PROBE_ERR="$STATE_DIR/probe.err"
RAW_OUT="$STATE_DIR/raw.txt"
RAW_ERR="$STATE_DIR/raw.err"

# ── 判定 4′: 疎通プローブ(読み取り専用モード・cwd は本実行と同じ)──
# 疎通確認に書き込み権限は要らないので、モード(権限)だけで絞る。cwd を分けない理由は
# 決定 43(上記)。§9-2 限界②〈cwd 外の絶対パスへの読み書き〉は cwd を分けても残るため、
# もともと cwd は限界②に対する防御になっていない。
build_cmd "ping と 1 語だけ返答してください。"
apply_probe_mode || die 20 internal "プローブ用モードへの差し替えに失敗した(書き込みフラグ '$WRITE_FLAG' が argv に見つからない)"
PROBE_RESOLVED="$(quote_cmd)"
log_line "- プローブの解決後コマンド: \`$PROBE_RESOLVED\`(cwd: $PROBE_CWD)"
rc=0
# サブシェルで包むと `run_timeout` が掴んだ子 PID が親から見えず、中止経路で道連れにできない
# (決定 41)。cd はこのシェル自身で行い、終わったら戻す
cd "$PROBE_CWD" || die 20 internal "プローブの cwd へ移動できない: $PROBE_CWD"
run_timeout "$PROBE_TIMEOUT" "${CMD[@]}" </dev/null >"$PROBE_OUT" 2>"$PROBE_ERR" || rc=$?
cd "$ORIG_PWD" || die 20 internal "元の作業ディレクトリへ戻れない: $ORIG_PWD"
# 失敗経路でも「得られた分の生出力」を残して返す(§12-8 の出力契約。プローブの失敗は
# 認証切れ・レート制限のことが多く、その理由は生出力側に出る)
probe_fail() { # $1=終了コード $2=理由コード 残り=説明
  pf_code="$1"; pf_reason="$2"; shift 2
  log_block "プローブ生出力" "$PROBE_OUT"
  log_block "プローブ標準エラー" "$PROBE_ERR"
  cat "$PROBE_OUT" 2>/dev/null || true
  die "$pf_code" "$pf_reason" "$@"
}
if [ "$rc" -eq 124 ]; then
  probe_fail 7 probe-timeout "ランナー '$RUNNER' がプローブに ${PROBE_TIMEOUT} 秒以内に応答しない(無応答)"
fi
if [ "$rc" -ne 0 ]; then
  probe_fail 6 probe-failed "ランナー '$RUNNER' の疎通に失敗(終了コード $rc)"
fi
if [ ! -s "$PROBE_OUT" ]; then
  probe_fail 6 probe-failed "ランナー '$RUNNER' の疎通で出力が空だった"
fi
log_line "- 疎通プローブ: OK(読み取り専用モード '$PROBE_FLAG'・cwd は本実行と同じ)"

# ── 本実行(書き込みモード + --cwd)──
# NUL バイトはコマンド置換でも argv でも運べず黙って落ちるので、落ちたことを記録に残す
PROMPT_RAW_BYTES="$(wc -c <"$PROMPT_FILE" | tr -d ' ')"
PROMPT_NONUL_BYTES="$(tr -d '\000' <"$PROMPT_FILE" | wc -c | tr -d ' ')"
if [ "$PROMPT_RAW_BYTES" -ne "$PROMPT_NONUL_BYTES" ]; then
  log_line "- 注意: プロンプトファイルに NUL バイトが含まれる($((PROMPT_RAW_BYTES - PROMPT_NONUL_BYTES)) 個)。argv には載らないため NUL を除いた本文が外部ランナーへ渡る"
  echo "NOTE: プロンプトファイルに NUL バイトが含まれる。argv に載らないため NUL を除いた本文が渡る" >&2
fi

PROMPT_TEXT="$(cat "$PROMPT_FILE")"
build_cmd "$PROMPT_TEXT"
log_line "- 本実行: 書き込みモード '$WRITE_FLAG'(cwd: $CWD)"
rc=0
# プローブと同じ理由でサブシェルを使わない(決定 41)
cd "$CWD" || die 20 internal "--cwd へ移動できない: $CWD"
run_timeout "$RUN_TIMEOUT" "${CMD[@]}" </dev/null >"$RAW_OUT" 2>"$RAW_ERR" || rc=$?
cd "$ORIG_PWD" || die 20 internal "元の作業ディレクトリへ戻れない: $ORIG_PWD"
# 生出力は必ず残す — 引き継ぎのときに「何をどこまでやって、なぜ止まったか」を知る唯一の手がかりになる
log_block "生出力" "$RAW_OUT"
if [ "$rc" -eq 124 ]; then
  log_block "標準エラー" "$RAW_ERR"
  cat "$RAW_OUT"
  die 9 run-timeout "ランナー '$RUNNER' が本実行で ${RUN_TIMEOUT} 秒以内に完了しない"
fi
if [ "$rc" -ne 0 ]; then
  log_block "標準エラー" "$RAW_ERR"
  cat "$RAW_OUT"
  die 8 run-failed "ランナー '$RUNNER' の実行が失敗(終了コード $rc)"
fi

log_line ""
log_line "**結果**: OK(生出力をそのまま返した。正規化はしない)"
# 成果物は --cwd 配下への書き込みであって stdout ではない。stdout は正規化せずそのまま流す
cat "$RAW_OUT"
