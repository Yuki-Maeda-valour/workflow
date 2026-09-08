#!/usr/bin/env bash
# 外部 CLI をレビュアーとして起動する共有アダプタ(dev-workflow)。
# 仕様は ../references/external-runners.md。呼び出し側(skill)が設定を解決し、
# このスクリプトには引数で渡す(**このスクリプトは YAML を読まない**)。
#
# 信頼モデル(external-runners.md §1・design §7-2):
#   `.claude/project-profile.yml` は commit される共有ファイルであり、リポジトリ側が書ける値から
#   実行されるコマンドが変わってはならない。したがって
#     - 既定表のランナー名(--runner)は「選ぶだけ」で任意コマンドにならない
#     - **既定表のランナーには --command / --readonly-flag を指定できない**(usage エラー)。
#       任意コマンド文字列を検査して「読み取り専用である」と保証するのは原理的に困難なため、
#       上書き経路そのものを既定表ランナーから外している(差し替えは {model} のみ)
#     - 既定表に無いランナーは --command + --readonly-flag が必須。**その保証はユーザー責任**
#
# 使い方:
#   bash review-agent.sh --runner <名前> --prompt-file <パス> [オプション]
#
# オプション:
#   --runner <名前>         ランナー名(既定表: cursor-agent / gemini / codex)
#   --command "<テンプレ>"  起動コマンド(空白区切り。{model} / {prompt} をプレースホルダとして使う)。
#                           **既定表に無いランナーでのみ指定でき、必須**(既定表ランナーに渡すと usage エラー)
#   --model <名前>          ランナーへ渡すモデル名(省略時はモデル指定フラグごと落とす)
#   --readonly-flag "<語>"  読み取り専用フラグ。**既定表に無いランナーでのみ指定でき、必須**
#   --prompt-file <パス>    レビュー依頼プロンプト(必須)
#   --target <パス>         レビュー対象(繰り返し可。プロンプト末尾に付記しログに残す)
#   --cwd <ディレクトリ>    ランナーの実行ディレクトリ(機密ガードの一時ツリーを渡す)
#   --probe-timeout <秒>    疎通プローブのタイムアウト(既定 60。0 は不可)
#   --run-timeout <秒>      本実行のタイムアウト(既定 600。0 は不可)
#   --log-file <パス>       ログ出力先(既定 .claude/reviews/reviewer-<runner>-iter<N>.md)
#   --dry-run               静的検査のみ行い、解決したコマンドを表示して終了(起動しない)
#
# 出力契約:
#   exit 0  stdout = 指摘 JSON(既存スキーマへ正規化済み)
#           jq も python3 も無い環境に限り stdout = 生出力・stderr に normalized:false
#   exit>0  stderr に "ERROR [理由コード] 説明"
#
# 終了コード: 2=usage 3=not-found 4=self-host 5=no-readonly 6=probe-failed
#             7=probe-timeout 8=run-failed 9=run-timeout 10=parse-failed
#             12=prompt-too-large 20=internal
#
# 空文字の扱い: --model "" / --target "" / --cwd "" / --log-file "" は「省略」として受理する。
#               --runner / --prompt-file、既定表に無いランナーの --command が空・未指定なら usage(2)。
#               既定表に無いランナーで --readonly-flag が空・未指定なら no-readonly(5)。
#
# 副作用: ログファイルの書き出しのみ(冪等)。ランナーは読み取り専用フラグを付けた形でのみ起動する。
#         ただし「そのフラグで本当に書き込まない」ことはランナー側の実装への信頼であり、
#         このスクリプトが保証できるものではない(保証範囲は external-runners.md §3 の表)。
#         機構として封じたい場合は --cwd に使い捨ての一時ツリーを渡す(同 §9)。
# --- end usage ---
set -eEuo pipefail

RUNNER=""
COMMAND_TMPL=""
MODEL=""
READONLY_FLAG_ARG=""
READONLY_FLAG=""
PROMPT_FILE=""
CWD=""
LOG_FILE=""
PROBE_TIMEOUT=60
RUN_TIMEOUT=600
HELP_TIMEOUT=20        # ヘルプ検査は短く抑える(合計滞在時間を Bash 上限に近づけないため)
KILL_GRACE=5           # TERM → KILL の猶予(3 経路で共通)
MAX_PROMPT_BYTES=100000  # argv 1 個の上限(Linux の MAX_ARG_STRLEN 128KiB)に対する安全側の閾値
DRY_RUN=0
TARGETS=()
CMD=()
RO_TOKENS=()

TMP_DIR=""
cleanup() { if [ -n "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi; }
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
    --command) need_val "$1" "$#"; COMMAND_TMPL="$2"; shift 2 ;;
    --model) need_val "$1" "$#"; MODEL="$2"; shift 2 ;;
    --readonly-flag) need_val "$1" "$#"; READONLY_FLAG_ARG="$2"; shift 2 ;;
    --prompt-file) need_val "$1" "$#"; PROMPT_FILE="$2"; shift 2 ;;
    --target) need_val "$1" "$#"; if [ -n "$2" ]; then TARGETS[${#TARGETS[@]}]="$2"; fi; shift 2 ;;
    --cwd) need_val "$1" "$#"; CWD="$2"; shift 2 ;;
    --probe-timeout) need_val "$1" "$#"; PROBE_TIMEOUT="$2"; shift 2 ;;
    --run-timeout) need_val "$1" "$#"; RUN_TIMEOUT="$2"; shift 2 ;;
    --log-file) need_val "$1" "$#"; LOG_FILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
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
if [ -n "$CWD" ] && [ ! -d "$CWD" ]; then fail_usage "--cwd が存在しない: $CWD"; fi
# 0 は GNU timeout では「無制限」、フォールバックでは「即 kill」で意味が反転するため受け付けない
case "$PROBE_TIMEOUT" in ''|*[!0-9]*) fail_usage "--probe-timeout は正の秒数" ;; esac
case "$RUN_TIMEOUT" in ''|*[!0-9]*) fail_usage "--run-timeout は正の秒数" ;; esac
[ "$PROBE_TIMEOUT" -gt 0 ] || fail_usage "--probe-timeout に 0 は指定できない(無制限/即 kill で意味が反転する)"
[ "$RUN_TIMEOUT" -gt 0 ] || fail_usage "--run-timeout に 0 は指定できない(無制限/即 kill で意味が反転する)"

# ── 既定表(信頼の基点。ここに無いランナーは --command + --readonly-flag が必須)──
# 出典と確認日は ../references/external-runners.md の表に記載する。
default_command() {
  case "$1" in
    cursor-agent) printf '%s' 'cursor-agent --mode ask --model {model} -p --output-format json' ;;
    gemini)       printf '%s' 'gemini --approval-mode plan -m {model} -o json -p {prompt}' ;;
    codex)        printf '%s' 'codex exec --sandbox read-only -m {model}' ;;
    *) return 1 ;;
  esac
}

default_readonly_flag() { # 「フラグ 値」のトークン列
  case "$1" in
    cursor-agent) printf '%s' '--mode ask' ;;
    gemini)       printf '%s' '--approval-mode plan' ;;
    codex)        printf '%s' '--sandbox read-only' ;;
    *) return 1 ;;
  esac
}

KNOWN=0
if default_command "$RUNNER" >/dev/null 2>&1; then KNOWN=1; fi

# ── 信頼モデルの適用(引数の受理条件)──
if [ "$KNOWN" -eq 1 ]; then
  # 既定表のランナーは既定コマンド・既定フラグをそのまま使う(差し替えは {model} のみ)。
  # 任意コマンド文字列の読み取り専用性を検査で保証するのは原理的に困難なため、上書き経路を持たない
  if [ -n "$COMMAND_TMPL" ]; then
    fail_usage "既定表のランナー '$RUNNER' には --command を指定できない(既定コマンドを使う。独自コマンドが要るなら既定表に無いランナー名を付けて --command + --readonly-flag で渡す)"
  fi
  if [ -n "$READONLY_FLAG_ARG" ]; then
    fail_usage "既定表のランナー '$RUNNER' には --readonly-flag を指定できない(既定値 '$(default_readonly_flag "$RUNNER")' を使う)"
  fi
  READONLY_FLAG="$(default_readonly_flag "$RUNNER")"
  TMPL="$(default_command "$RUNNER")"
  CMD_SOURCE="既定表"
else
  [ -n "$COMMAND_TMPL" ] || fail_usage "ランナー '$RUNNER' は既定表に無い。--command で起動コマンドを渡す(external-runners.md 参照)"
  [ -n "$READONLY_FLAG_ARG" ] || die 5 no-readonly "既定表に無いランナー '$RUNNER' では --readonly-flag が必須(読み取り専用を確立できないランナーは起動しない)"
  READONLY_FLAG="$READONLY_FLAG_ARG"
  TMPL="$COMMAND_TMPL"
  CMD_SOURCE="上書き(--command)"
fi
read -r -a RO_TOKENS <<<"$READONLY_FLAG"
[ "${#RO_TOKENS[@]}" -gt 0 ] || die 5 no-readonly "読み取り専用フラグが空"

# ホスト自身の CLI(design §5-5: 自ホストと同じ CLI をランナーとして起動しない)
# ベストエフォート判定。誤判定するときは DEV_WORKFLOW_HOST_CLI で上書きする。
host_cli() {
  if [ -n "${DEV_WORKFLOW_HOST_CLI:-}" ]; then printf '%s' "$DEV_WORKFLOW_HOST_CLI"; return 0; fi
  if [ -n "${CLAUDECODE:-}" ]; then printf '%s' 'claude'; return 0; fi
  if [ -n "${CODEX_SANDBOX:-}" ]; then printf '%s' 'codex'; return 0; fi
  if [ -n "${CURSOR_AGENT:-}" ]; then printf '%s' 'cursor-agent'; return 0; fi
  printf '%s' ''
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

# タイムアウト付き実行(3 経路すべて「TERM → KILL_GRACE 秒 → KILL」で統一)。124=タイムアウト
run_timeout() { # $1=秒 残り=コマンド
  rt_secs="$1"; shift
  rt_start="$(date +%s)"
  if [ -n "$TIMEOUT_BIN" ]; then
    rt_rc=0
    "$TIMEOUT_BIN" -k "$KILL_GRACE" "$rt_secs" "$@" || rt_rc=$?
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
  "$@" &
  rt_pid=$!
  set +m 2>/dev/null || true
  rt_waited=0
  while kill -0 "$rt_pid" 2>/dev/null; do
    if [ "$rt_waited" -ge "$rt_secs" ]; then
      kill_tree "$rt_pid"
      wait "$rt_pid" 2>/dev/null || true
      return 124
    fi
    sleep 1
    rt_waited=$((rt_waited + 1))
  done
  rt_rc=0
  wait "$rt_pid" || rt_rc=$?
  return "$rt_rc"
}

build_cmd() { # $1=プロンプト本文 → 配列 CMD を組む
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

# 読み取り専用フラグが argv トークン列として実在するか(表示用文字列の部分一致に頼らない)
readonly_in_argv() {
  ri_n=${#CMD[@]}
  ri_m=${#RO_TOKENS[@]}
  ri_i=0
  while [ $((ri_i + ri_m)) -le "$ri_n" ]; do
    ri_j=0
    ri_hit=1
    while [ "$ri_j" -lt "$ri_m" ]; do
      if [ "${CMD[$((ri_i + ri_j))]}" != "${RO_TOKENS[$ri_j]}" ]; then ri_hit=0; break; fi
      ri_j=$((ri_j + 1))
    done
    if [ "$ri_hit" -eq 1 ]; then return 0; fi
    ri_i=$((ri_i + 1))
  done
  if [ "$ri_m" -eq 2 ]; then
    ri_joined="${RO_TOKENS[0]}=${RO_TOKENS[1]}"
    for a in ${CMD[@]+"${CMD[@]}"}; do
      if [ "$a" = "$ri_joined" ]; then return 0; fi
    done
  fi
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

# ── ログの初期化(既定名は design §5-14 の {role}-iter{N} 形式)──
if [ -z "$LOG_FILE" ]; then
  LOG_DIR=".claude/reviews"
  mkdir -p "$LOG_DIR"
  iter=1
  while [ -e "$LOG_DIR/reviewer-${RUNNER}-iter${iter}.md" ]; do iter=$((iter + 1)); done
  LOG_FILE="$LOG_DIR/reviewer-${RUNNER}-iter${iter}.md"
else
  LOG_DIR="$(dirname "$LOG_FILE")"
  mkdir -p "$LOG_DIR"
fi

{
  printf '# 外部ランナー実行記録: %s\n\n' "$RUNNER"
  printf -- '- 実行日時: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  printf -- '- ランナー: %s(既定表: %s)\n' "$RUNNER" "$(if [ "$KNOWN" -eq 1 ]; then printf 'あり'; else printf 'なし'; fi)"
  printf -- '- コマンド出所: %s\n' "$CMD_SOURCE"
  printf -- '- 上書きの承認前提: %s\n' "$(if [ -n "$COMMAND_TMPL" ]; then printf 'ユーザーの明示指定(profile 由来ではない)'; else printf '(上書きなし)'; fi)"
  printf -- '- モデル: %s\n' "${MODEL:-(ランナー既定)}"
  printf -- '- 読み取り専用フラグ: %s\n' "$READONLY_FLAG"
  printf -- '- 実行ディレクトリ: %s\n' "${CWD:-$(pwd)}"
  printf -- '- プロンプトファイル: %s\n' "$PROMPT_FILE"
  printf -- '- タイムアウト: プローブ %s 秒 / 本実行 %s 秒(TERM → %s 秒 → KILL)\n' "$PROBE_TIMEOUT" "$RUN_TIMEOUT" "$KILL_GRACE"
  printf -- '- 渡した対象: %s\n' "$(if [ ${#TARGETS[@]} -gt 0 ]; then printf '%s ' ${TARGETS[@]+"${TARGETS[@]}"}; else printf '(--target 指定なし)'; fi)"
} >"$LOG_FILE"

if [ -n "$COMMAND_TMPL" ]; then
  echo "NOTE: 起動コマンドを上書きしています(profile 由来ではなく、ユーザーの明示指定であることが前提)" >&2
fi

# ── 判定 1: 存在 ──
build_cmd "<プロンプト>"
BIN="${CMD[0]}"
EFF_BIN="$(effective_bin)"
RESOLVED="$(quote_cmd)"
log_line "- 解決後のコマンド: \`$RESOLVED\`"
command -v "$BIN" >/dev/null 2>&1 || die 3 not-found "ランナー '$RUNNER' の実行ファイル '$BIN' が PATH に無い"

# ── 判定 2: 自ホストと別 CLI か(design §5-5)──
HOST_CLI="$(host_cli)"
if [ -n "$HOST_CLI" ]; then
  if [ "$HOST_CLI" = "$RUNNER" ] || [ "$HOST_CLI" = "$EFF_BIN" ] || [ "$HOST_CLI" = "$(basename "$BIN")" ]; then
    die 4 self-host "ホスト自身と同じ CLI('$HOST_CLI')はランナーにできない(二重課金の回避。ホストのエージェント機構を使う)"
  fi
fi
log_line "- ホスト CLI 判定(ベストエフォート): ${HOST_CLI:-(不明)}"

# ── 判定 3: 読み取り専用フラグの確立 ──
readonly_in_argv || die 5 no-readonly "起動コマンドの argv に読み取り専用フラグ '$READONLY_FLAG' が無い(上書きコマンドにも必須)"
log_line "- 読み取り専用フラグ(argv 照合): '$READONLY_FLAG' を確認"

if [ "$KNOWN" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
  RO_NAME="${RO_TOKENS[0]}"
  RO_VALUE="${RO_TOKENS[$((${#RO_TOKENS[@]} - 1))]}"
  TMP_DIR="$(mktemp -d)"
  HELP_OUT="$TMP_DIR/help.txt"
  : >"$HELP_OUT"
  help_rc=0
  run_timeout "$HELP_TIMEOUT" "$BIN" --help >>"$HELP_OUT" 2>&1 </dev/null || help_rc=$?
  if [ "$help_rc" -eq 124 ]; then
    die 7 probe-timeout "'$BIN --help' が ${HELP_TIMEOUT} 秒以内に応答しない(無応答)"
  fi
  help_found=0
  if grep -qw -- "$RO_NAME" "$HELP_OUT" 2>/dev/null && grep -qw -- "$RO_VALUE" "$HELP_OUT" 2>/dev/null; then help_found=1; fi
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
        if grep -qw -- "$RO_NAME" "$HELP_OUT" 2>/dev/null && grep -qw -- "$RO_VALUE" "$HELP_OUT" 2>/dev/null; then help_found=1; fi
        ;;
    esac
  fi
  if [ "$help_found" -ne 1 ]; then
    die 5 no-readonly "'$BIN' のヘルプに読み取り専用フラグ '$RO_NAME' と値 '$RO_VALUE' が見つからない(仕様変更の可能性。既定表を更新する)"
  fi
  log_line "- 読み取り専用フラグ(ヘルプ照合): '$RO_NAME' と値 '$RO_VALUE' を確認"
else
  log_line "- 読み取り専用フラグ(ヘルプ照合): 未実施(既定表に無いランナー、または --dry-run)"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  log_line "- dry-run: 静的検査のみ(起動しない)"
  printf '%s\n' "$RESOLVED"
  exit 0
fi

if [ -z "$TMP_DIR" ]; then TMP_DIR="$(mktemp -d)"; fi
PROBE_OUT="$TMP_DIR/probe.txt"
PROBE_ERR="$TMP_DIR/probe.err"
RAW_OUT="$TMP_DIR/raw.txt"
RAW_ERR="$TMP_DIR/raw.err"

# ── 判定 4: 疎通プローブ(タイムアウト内)──
build_cmd "ping と 1 語だけ返答してください。"
rc=0
( cd "${CWD:-.}" && run_timeout "$PROBE_TIMEOUT" "${CMD[@]}" </dev/null ) >"$PROBE_OUT" 2>"$PROBE_ERR" || rc=$?
if [ "$rc" -eq 124 ]; then
  log_block "プローブ標準エラー" "$PROBE_ERR"
  die 7 probe-timeout "ランナー '$RUNNER' がプローブに ${PROBE_TIMEOUT} 秒以内に応答しない(無応答)"
fi
if [ "$rc" -ne 0 ]; then
  log_block "プローブ標準エラー" "$PROBE_ERR"
  die 6 probe-failed "ランナー '$RUNNER' の疎通に失敗(終了コード $rc)"
fi
if [ ! -s "$PROBE_OUT" ]; then
  log_block "プローブ標準エラー" "$PROBE_ERR"
  die 6 probe-failed "ランナー '$RUNNER' の疎通で出力が空だった"
fi
log_line "- 疎通プローブ: OK"

# ── 本実行 ──
PROMPT_TEXT="$(cat "$PROMPT_FILE")"
if [ ${#TARGETS[@]} -gt 0 ]; then
  PROMPT_TEXT="$PROMPT_TEXT

## レビュー対象(このパスだけを読む)"
  for t in ${TARGETS[@]+"${TARGETS[@]}"}; do
    PROMPT_TEXT="$PROMPT_TEXT
- $t"
  done
fi
PROMPT_BYTES="$(printf '%s' "$PROMPT_TEXT" | wc -c | tr -d ' ')"
if [ "$PROMPT_BYTES" -gt "$MAX_PROMPT_BYTES" ]; then
  die 12 prompt-too-large "プロンプトが ${PROMPT_BYTES} バイトで上限 ${MAX_PROMPT_BYTES} を超える(argv 1 個の上限 128KiB)。diff を直接貼らずパスで渡す"
fi

build_cmd "$PROMPT_TEXT"
rc=0
( cd "${CWD:-.}" && run_timeout "$RUN_TIMEOUT" "${CMD[@]}" </dev/null ) >"$RAW_OUT" 2>"$RAW_ERR" || rc=$?
log_block "生出力" "$RAW_OUT"
if [ "$rc" -eq 124 ]; then
  log_block "標準エラー" "$RAW_ERR"
  die 9 run-timeout "ランナー '$RUNNER' が本実行で ${RUN_TIMEOUT} 秒以内に完了しない"
fi
if [ "$rc" -ne 0 ]; then
  log_block "標準エラー" "$RAW_ERR"
  die 8 run-failed "ランナー '$RUNNER' の実行が失敗(終了コード $rc)"
fi

# ── 出力の正規化(既存の指摘 JSON スキーマへ)──
NORM_OUT="$TMP_DIR/normalized.json"

normalize_with_python() {
  python3 - "$RAW_OUT" "$TMP_DIR/raw_verdict.txt" >"$NORM_OUT" <<'PY'
import json, re, sys

ENVELOPE_KEYS = ("result", "response", "output", "text", "content", "message", "stdout")


def candidates(text):
    yield text
    for m in re.finditer(r"```(?:json)?\s*(.+?)```", text, re.S):
        yield m.group(1)
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
            except ValueError:
                continue
            yield json.dumps(obj, ensure_ascii=False)


def find(obj, depth=0):
    if depth > 4:
        return None
    if isinstance(obj, dict):
        if "verdict" in obj or "issues" in obj:
            return obj
        keys = [k for k in ENVELOPE_KEYS if k in obj] + [k for k in obj if k not in ENVELOPE_KEYS]
        for k in keys:
            v = obj[k]
            got = find_in_text(v, depth + 1) if isinstance(v, str) else find(v, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = find_in_text(v, depth + 1) if isinstance(v, str) else find(v, depth + 1)
            if got:
                return got
    return None


def find_in_text(text, depth=0):
    if depth > 4 or not isinstance(text, str):
        return None
    for cand in candidates(text):
        cand = cand.strip()
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        got = find(obj, depth + 1)
        if got:
            return got
    return None


def as_text(v, default=""):
    """jq の tostring と同じ結果にそろえる(数値・真偽値も文字列化)"""
    if v is None:
        return default
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
found = find_in_text(raw)
if found is None:
    sys.exit(1)

issues = found.get("issues")
if not isinstance(issues, list):
    issues = []
norm = []
for it in issues:
    if not isinstance(it, dict):
        continue
    norm.append({
        "file": as_text(it.get("file") or it.get("path") or ""),
        "line": it.get("line") if it.get("line") is not None else None,
        "category": as_text(it.get("category") or ""),
        "severity": as_text(it.get("severity") or "minor"),
        "description": as_text(it.get("description") or it.get("issue") or ""),
        "suggestion": as_text(it.get("suggestion") or it.get("fix") or ""),
    })
raw_verdict = as_text(found.get("verdict"), "")
verdict = raw_verdict.upper()
if verdict not in ("APPROVED", "CHANGES_REQUESTED"):
    verdict = "CHANGES_REQUESTED" if norm else "APPROVED"
with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write(raw_verdict)
print(json.dumps({"verdict": verdict, "issues": norm}, ensure_ascii=False, indent=2))
PY
}

# jq 用の写像。issues が配列でない場合は空配列に落とし、verdict は列挙値へ寄せる
JQ_MAP='
  (if (.issues? | type) == "array" then .issues else [] end) as $iss
| ([ $iss[] | select(type == "object") |
     { file: ((.file // .path // "") | tostring),
       line: (.line? // null),
       category: ((.category // "") | tostring),
       severity: ((.severity // "minor") | tostring),
       description: ((.description // .issue // "") | tostring),
       suggestion: ((.suggestion // .fix // "") | tostring) } ]) as $norm
| ((.verdict? // "") | tostring | ascii_upcase) as $rawv
| { verdict: (if $rawv == "APPROVED" or $rawv == "CHANGES_REQUESTED" then $rawv
              elif ($norm | length) > 0 then "CHANGES_REQUESTED"
              else "APPROVED" end),
    issues: $norm }
'

# フェンス除去 + 最初の平衡した {...} を抜き出す(python3 が無い環境向けの近似)
extract_json_block() { # $1=入力ファイル $2=出力ファイル
  awk '
    /^[[:space:]]*```/ { next }
    { print }
  ' "$1" >"$2.defence"
  awk '
    BEGIN { depth = 0; started = 0 }
    {
      line = $0
      out = ""
      for (i = 1; i <= length(line); i++) {
        c = substr(line, i, 1)
        if (c == "{") { depth++; started = 1 }
        if (started) out = out c
        if (c == "}") { depth--; if (depth <= 0 && started) { print out; exit } }
      }
      if (started) print out
    }
  ' "$2.defence" >"$2"
  [ -s "$2" ]
}

jq_pick_and_map() { # $1=入力ファイル。成功時のみ NORM_OUT を作る
  # 複数ドキュメント(JSONL)でも「指摘 JSON らしい最初の 1 個」だけに絞る
  if ! jq -s 'map(select(type == "object" and (has("verdict") or has("issues")))) | first // empty' \
        <"$1" >"$TMP_DIR/picked.json" 2>>"$TMP_DIR/jq.err"; then
    return 1
  fi
  [ -s "$TMP_DIR/picked.json" ] || return 1
  jq -r '(.verdict? // "") | tostring' <"$TMP_DIR/picked.json" >"$TMP_DIR/raw_verdict.txt" 2>/dev/null || : >"$TMP_DIR/raw_verdict.txt"
  # jq の失敗(型不一致など)を必ず拾う。空出力のまま exit 0 にしない
  if ! jq "$JQ_MAP" <"$TMP_DIR/picked.json" >"$NORM_OUT" 2>>"$TMP_DIR/jq.err"; then
    return 1
  fi
  [ -s "$NORM_OUT" ] || return 1
  return 0
}

normalize_with_jq() {
  if jq_pick_and_map "$RAW_OUT"; then return 0; fi
  # 封筒(.result 等)の文字列を取り出して再挑戦
  jq_inner="$TMP_DIR/inner.txt"
  if jq -r -s 'map(select(type == "object" and (has("result") or has("response") or has("output") or has("text") or has("message") or has("content")))) | first // {} | (.result // .response // .output // .text // .message // .content // empty)' \
       <"$RAW_OUT" >"$jq_inner" 2>/dev/null && [ -s "$jq_inner" ]; then
    if extract_json_block "$jq_inner" "$TMP_DIR/inner.json" 2>/dev/null; then
      if jq_pick_and_map "$TMP_DIR/inner.json"; then return 0; fi
    fi
  fi
  # 生テキストからフェンス除去 + ブロック抽出
  if extract_json_block "$RAW_OUT" "$TMP_DIR/raw.json" 2>/dev/null; then
    if jq_pick_and_map "$TMP_DIR/raw.json"; then return 0; fi
  fi
  return 1
}

if command -v python3 >/dev/null 2>&1; then
  rc=0
  normalize_with_python || rc=$?
  if [ "$rc" -ne 0 ]; then
    die 10 parse-failed "ランナー '$RUNNER' の出力から指摘 JSON を取り出せない(生出力は $LOG_FILE)"
  fi
elif command -v jq >/dev/null 2>&1; then
  rc=0
  normalize_with_jq || rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ -s "$TMP_DIR/jq.err" ]; then log_block "jq のエラー出力" "$TMP_DIR/jq.err"; fi
    die 10 parse-failed "ランナー '$RUNNER' の出力から指摘 JSON を取り出せない(生出力は $LOG_FILE)"
  fi
else
  # 縮退はこの 1 条件だけ: jq も python3 も無い環境では正規化を諦めて生出力を返す
  log_line "- 正規化: normalized:false(jq / python3 が無い)"
  echo "normalized:false (jq / python3 が無いため正規化を省略。生出力をそのまま返す)" >&2
  cat "$RAW_OUT"
  exit 0
fi

# 出力契約の検証(どちらの正規化経路を通っても同じ形であること)。
# 正規化が空・壊れた形のまま exit 0 で返す事故を防ぐ最後の関門
norm_shape_ok() {
  [ -s "$NORM_OUT" ] || return 1
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
sys.exit(0 if isinstance(d, dict) and isinstance(d.get("verdict"), str) and isinstance(d.get("issues"), list) else 1)' "$NORM_OUT" 2>/dev/null
  else
    jq -e 'type == "object" and (.verdict | type) == "string" and (.issues | type) == "array"' >/dev/null 2>&1 <"$NORM_OUT"
  fi
}
if ! norm_shape_ok; then
  die 10 parse-failed "正規化結果が指摘 JSON の形になっていない(生出力は $LOG_FILE)"
fi

if [ -s "$TMP_DIR/raw_verdict.txt" ]; then
  RAW_VERDICT="$(cat "$TMP_DIR/raw_verdict.txt")"
  case "$(printf '%s' "$RAW_VERDICT" | tr '[:lower:]' '[:upper:]')" in
    APPROVED|CHANGES_REQUESTED) : ;;
    *) log_line "- 元の verdict: '$RAW_VERDICT'(列挙値ではないため issues の有無から導出した)" ;;
  esac
fi
log_block "正規化後 JSON" "$NORM_OUT"
log_line ""
log_line "**結果**: OK(正規化済み)"
cat "$NORM_OUT"
