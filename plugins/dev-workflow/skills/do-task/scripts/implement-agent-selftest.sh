#!/usr/bin/env bash
# implement-agent.sh の回帰テスト(スタブのみ・外部 CLI 不要・ネットワーク不要)。
# review-agent-selftest.sh とは独立したスイートで、互いのファイルには触れない
# (../references/external-runners.md §11 の決定 9)。
#
# 使い方:
#   bash implement-agent-selftest.sh            # 全ケース実行
#   bash implement-agent-selftest.sh -v         # 各ケースの出力も表示
#   IMPLEMENT_AGENT=<パス> bash implement-agent-selftest.sh   # 別の実装を対象にする(変異テスト用)
#
# 検証するのは「終了コードと出力の契約(§12-8)」「信頼モデルが破れないこと(既定表外・上書き引数を
# 受け付けないこと)」「プローブと本実行のモード・cwd の分離(§12-1 の判定 4′)」
# 「実装用既定表(§12-6)と実装の同期」、および**書き込み権限が付いて初めて要る機構**
# (決定 40〜42: シグナル中止で子を残さない / 生出力を本実行から偽造されない / 自ホスト判定が
# 指標を隠さない)。**出力の正規化は実装経路に無いので検証しない。**
#
# 期待終了コード: 0=成功 2=usage 3=not-found 4=self-host 6=probe-failed 7=probe-timeout
#                 8=run-failed 9=run-timeout 11=no-writemode 12=prompt-too-large 20=internal
#                 128+N=シグナル N での中止(TERM=143 / HUP=129 / INT=130)
#
# 安全策: 実 CLI を絶対に起動しないため、全ケースを「スタブディレクトリ + 制限 PATH」で走らせる
#         (制限 PATH には核となるコマンドの symlink しか置かないので、実機の同名 CLI は見えない)。
#
# 終了コード: 0=全件 PASS / 1=FAIL あり
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${IMPLEMENT_AGENT:-$SCRIPT_DIR/implement-agent.sh}"
DOC="$SCRIPT_DIR/../references/external-runners.md"
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

[ -f "$TARGET" ] || { echo "ERROR: $TARGET が無い" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'leak_cleanup; rm -rf "$WORK"' EXIT
mkdir -p "$WORK/proj" "$WORK/bin" "$WORK/safebin" "$WORK/target" "$WORK/record" "$WORK/empty"
cd "$WORK/proj"

PASS=0
FAIL=0
RESULTS=""

ok()   { PASS=$((PASS + 1)); RESULTS="$RESULTS
PASS  $1"; }
ng()   { FAIL=$((FAIL + 1)); RESULTS="$RESULTS
FAIL  $1"; }

# ── 制限 PATH(実機の CLI が絶対に見えないようにする)──
make_sandbox() { # $1=出力ディレクトリ 残り=除外するコマンド名
  sb="$1"; shift
  rm -f "$sb"/*
  for c in bash sh cat grep date dirname basename mkdir chmod sed awk sleep rm mktemp cut wc tr head env ls pkill ps timeout gtimeout; do
    skip=0
    for x in "$@"; do [ "$c" = "$x" ] && skip=1; done
    [ "$skip" -eq 1 ] && continue
    src="$(command -v "$c" 2>/dev/null)" || continue
    [ -n "$src" ] && ln -sf "$src" "$sb/$c"
  done
}
make_sandbox "$WORK/safebin"
SAFEPATH="$WORK/safebin"

PROMPT="$WORK/prompt.md"
echo "タスク MD に従って実装してください" >"$PROMPT"
CWD_TARGET="$WORK/target"
RECORD_DIR="$WORK/record"
SIDEEFFECT="$WORK/sideeffect.marker"

# ── スタブ群(すべて `codex` という名前で各ディレクトリに置き、PATH 先頭で切り替える)──
# ヘルプの出し方は実機の形を模す(`-s, --sandbox` と possible values 行)。
HELP_BODY='  -s, --sandbox <SANDBOX_MODE>
          [possible values: read-only, workspace-write, danger-full-access]'

# 1) 記録型(追記): プローブと本実行の 2 回を追える。既存の review 側 stub-record.sh は
#    `pwd > cwd.txt` の上書きなので 2 回を追えない — ここが本スイート固有の治具
mkdir -p "$WORK/pathbin"
cat >"$WORK/pathbin/codex" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then
    echo '  -s, --sandbox <SANDBOX_MODE>'
    echo '          [possible values: read-only, workspace-write, danger-full-access]'
    exit 0
  fi
done
mode=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--sandbox" ]; then mode="$a"; fi
  prev="$a"
done
if [ -n "${SELFTEST_RECORD_DIR:-}" ]; then
  # 1 起動につき 1 行を**追記**する(上書きしない)。プローブと本実行の 2 回を追うための治具。
  # 最後の引数は切り詰めない — マルチバイトの途中で切ると不正バイト列になり、
  # 読み出し側の grep が「binary file matches」に倒れて照合が空振りするため。
  # argv は**末尾のプロンプトを除く全トークン**を空白で繋いだもの。フラグ「名」の変更を
  # 検出するために要る(値だけを見る mode では `--sandbox` → `--mode` の改名を素通りする)
  printf 'mode=%s\tcwd=%s\tmodel=%s\targv=%s\tlastarg=%s\n' "$mode" "$(pwd)" "$(
    prev=""; m="(none)"
    for a in "$@"; do if [ "$prev" = "-m" ]; then m="$a"; fi; prev="$a"; done
    printf '%s' "$m")" "$(
    n=$#; i=0; acc=""
    for a in "$@"; do i=$((i + 1)); [ "$i" -eq "$n" ] && break; acc="$acc $a"; done
    printf '%s' "${acc# }" | tr '\n\t' '  ')" "$(printf '%s' "${@: -1}" | tr '\n' ' ')" \
    >>"$SELFTEST_RECORD_DIR/calls.tsv"
fi
echo "外部 implementer の生出力: mode=$mode"
EOF

# 2) ヘルプに書き込みフラグが出ない(判定 3′ のヘルプ照合が落ちる)
mkdir -p "$WORK/nohelp"
cat >"$WORK/nohelp/codex" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then
    echo 'Usage: codex [OPTIONS] [PROMPT]'
    echo '  -m, --model <MODEL>'
    exit 0
  fi
done
echo "外部 implementer の生出力(ヘルプに書き込みフラグが無いランナー)"
EOF

# 3) `<bin> --help` には出さず `<bin> exec --help` にだけ出す(サブコマンド側のヘルプ照合)
mkdir -p "$WORK/subhelp"
cat >"$WORK/subhelp/codex" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "--help" ]; then echo "Usage: codex <command>"; exit 0; fi
if [ "${1:-}" = "exec" ] && [ "${2:-}" = "--help" ]; then
  echo '  -s, --sandbox <SANDBOX_MODE>'
  echo '          [possible values: read-only, workspace-write, danger-full-access]'
  exit 0
fi
mode=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--sandbox" ]; then mode="$a"; fi
  prev="$a"
done
echo "外部 implementer の生出力: mode=$mode"
EOF

# 4) --help がハングする(ヘルプ照合のタイムアウト経路)
mkdir -p "$WORK/helphang"
cat >"$WORK/helphang/codex" <<'EOF'
#!/usr/bin/env bash
sleep 120
EOF

# 5) プローブ(読み取り専用)だけ失敗 / 空出力 / ハング
mkdir -p "$WORK/probefail" "$WORK/probeempty" "$WORK/probehang"
for d in probefail probeempty probehang; do
  cat >"$WORK/$d/codex" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then
    echo '  -s, --sandbox <SANDBOX_MODE>'
    echo '          [possible values: read-only, workspace-write, danger-full-access]'
    exit 0
  fi
done
mode=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--sandbox" ]; then mode="$a"; fi
  prev="$a"
done
if [ "$mode" = "read-only" ]; then
  case "$SELFTEST_PROBE_BEHAVIOR" in
    # 失敗時も stdout に手がかりを出す(§12-8「失敗時は得られた分の生出力」の検査に使う)
    fail)  echo "認証トークンの期限が切れています"; echo "認証が必要です" >&2; exit 7 ;;
    empty) exit 0 ;;
    hang)  sleep 120 ;;
  esac
fi
echo "外部 implementer の生出力: mode=$mode"
EOF
done

# 6) プローブは通り、本実行(書き込みモード)だけ失敗 / ハング
mkdir -p "$WORK/runfail" "$WORK/runhang"
cat >"$WORK/runfail/codex" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then
    echo '  -s, --sandbox <SANDBOX_MODE>'
    echo '          [possible values: read-only, workspace-write, danger-full-access]'
    exit 0
  fi
done
mode=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--sandbox" ]; then mode="$a"; fi
  prev="$a"
done
if [ "$mode" = "read-only" ]; then echo "pong"; exit 0; fi
echo "途中まで書いて力尽きた"
echo "本実行だけ失敗する" >&2
exit 3
EOF
cat >"$WORK/runhang/codex" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then
    echo '  -s, --sandbox <SANDBOX_MODE>'
    echo '          [possible values: read-only, workspace-write, danger-full-access]'
    exit 0
  fi
done
mode=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--sandbox" ]; then mode="$a"; fi
  prev="$a"
done
if [ "$mode" = "read-only" ]; then echo "pong"; exit 0; fi
sleep 120
EOF

# 7) 拒否が退行したら起動されて痕跡が残るスタブ(信頼モデルの PoC)。
#    実装用既定表に無い名前でも「万一受け付けたら動いてしまう」形にしておく
mkdir -p "$WORK/evilbin"
for n in codex gemini cursor-agent stubrunner; do
  cat >"$WORK/evilbin/$n" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then
    echo '  -s, --sandbox <SANDBOX_MODE>'
    echo '          [possible values: read-only, workspace-write, danger-full-access]'
    exit 0
  fi
done
: >"$SELFTEST_SIDEEFFECT"
echo "起動されてしまった"
EOF
done

# 8) TERM を無視して居座るプロセス(タイムアウトの実効性)
cat >"$WORK/bin/hangproc.sh" <<'EOF'
#!/usr/bin/env bash
trap '' TERM
while :; do sleep 1; done
EOF
mkdir -p "$WORK/termignore"
cat >"$WORK/termignore/codex" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  if [ "\$a" = "--help" ]; then
    echo '  -s, --sandbox <SANDBOX_MODE>'
    echo '          [possible values: read-only, workspace-write, danger-full-access]'
    exit 0
  fi
done
exec bash "$WORK/bin/hangproc.sh"
EOF

# 9) スクリプト自身へのシグナル(決定 41)。本実行に入ったことを marker で知らせてから、
#    TERM を無視して居座る子へ exec で化ける。「script を撃つと子も死ぬか」を測る治具
cat >"$WORK/bin/runproc.sh" <<'EOF'
#!/usr/bin/env bash
trap '' TERM
while :; do sleep 1; done
EOF
mkdir -p "$WORK/sigstub"
cat >"$WORK/sigstub/codex" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  if [ "\$a" = "--help" ]; then
    echo '  -s, --sandbox <SANDBOX_MODE>'
    echo '          [possible values: read-only, workspace-write, danger-full-access]'
    exit 0
  fi
done
mode=""; prev=""
for a in "\$@"; do
  if [ "\$prev" = "--sandbox" ]; then mode="\$a"; fi
  prev="\$a"
done
if [ "\$mode" = "read-only" ]; then echo "pong"; exit 0; fi
echo "途中まで書いた"
: >"\$SELFTEST_RUN_MARKER"
exec bash "$WORK/bin/runproc.sh"
EOF

# 10) 一時領域の改竄(決定 40 の PoC)。本実行から「スクリプトの scratch らしき場所」を
#     総当たりで上書きしようとする。保護領域に置いてあれば届かない。
#     ⚠ この治具は意図的に雑(`/tmp/tmp.*/raw.txt` を総当たり)なので、**このスイートを
#        他の dev-workflow 自己テストと同時に走らせない**(相手の一時ファイルまで汚す)
mkdir -p "$WORK/forge"
cat >"$WORK/forge/codex" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then
    echo '  -s, --sandbox <SANDBOX_MODE>'
    echo '          [possible values: read-only, workspace-write, danger-full-access]'
    exit 0
  fi
done
mode=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--sandbox" ]; then mode="$a"; fi
  prev="$a"
done
if [ "$mode" = "read-only" ]; then echo "pong"; exit 0; fi
echo "本物の生出力(実装は途中で止まった)"
for d in /tmp "${TMPDIR:-/tmp}"; do
  for f in "$d"/tmp.*/raw.txt "$d"/tmp.*/raw.err "$d"/tmp.*/probe.txt; do
    [ -f "$f" ] && printf 'FORGED: 全部終わりました\n' >"$f"
  done
done
exit 0
EOF

# 11) 成果を --cwd に残したまま失敗 / ハングする(引き継ぎ専用の終了コードが無いことの検査)
mkdir -p "$WORK/partial" "$WORK/partialhang"
for d in partial partialhang; do
  cat >"$WORK/$d/codex" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  if [ "\$a" = "--help" ]; then
    echo '  -s, --sandbox <SANDBOX_MODE>'
    echo '          [possible values: read-only, workspace-write, danger-full-access]'
    exit 0
  fi
done
mode=""; prev=""
for a in "\$@"; do
  if [ "\$prev" = "--sandbox" ]; then mode="\$a"; fi
  prev="\$a"
done
if [ "\$mode" = "read-only" ]; then echo "pong"; exit 0; fi
echo "半分だけ実装した" >"\$SELFTEST_ARTIFACT"
echo "途中まで書いた"
$([ "$d" = "partialhang" ] && echo 'sleep 120' || echo 'exit 3')
EOF
done

chmod +x "$WORK/bin"/*.sh "$WORK/pathbin/codex" "$WORK/nohelp/codex" "$WORK/subhelp/codex" \
  "$WORK/helphang/codex" "$WORK/probefail/codex" "$WORK/probeempty/codex" "$WORK/probehang/codex" \
  "$WORK/runfail/codex" "$WORK/runhang/codex" "$WORK/termignore/codex" "$WORK/sigstub/codex" \
  "$WORK/forge/codex" "$WORK/partial/codex" "$WORK/partialhang/codex" "$WORK/evilbin"/*

leak_count() { ps -eo args 2>/dev/null | grep -c "^bash $WORK/bin/hangproc.sh"; }
sig_leak_count() { ps -eo args 2>/dev/null | grep -c "^bash $WORK/bin/runproc.sh"; }
leak_cleanup() {
  for s in hangproc runproc; do
    ps -eo pid,args 2>/dev/null | awk -v s="bash $WORK/bin/$s.sh" '$0 ~ "[0-9] "s {print $1}' \
      | while read -r p; do kill -9 "$p" 2>/dev/null; done
  done
}

CASE_OUT="$WORK/case.out"
CASE_ERR="$WORK/case.err"
STUB_DIR="$WORK/pathbin"
PROBE_BEHAVIOR="none"

# ケースごとの外側ガード。実装が壊れてハングしても、スイートは止まらず FAIL になる
OUTER_TIMEOUT=90
OUTER_TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then OUTER_TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then OUTER_TIMEOUT_BIN="gtimeout"; fi
guard() {
  if [ -n "$OUTER_TIMEOUT_BIN" ]; then "$OUTER_TIMEOUT_BIN" -k 5 "$OUTER_TIMEOUT" "$@"; else "$@"; fi
}

RUN_MARKER="$WORK/run.marker"
ARTIFACT="$CWD_TARGET/partial-artifact.txt"
AGENT_CWD=""          # 空ならスイートの cwd($WORK/proj)で走らせる
EXTRA_ENV=()          # 追加・上書きしたい環境変数(`VAR=値` の並び)

agent_env() { # 共通の env 前置き(配列として展開する)
  printf '%s\n' "PATH=$STUB_DIR:$SAFEPATH" "DEV_WORKFLOW_HOST_CLI=selftest-host" \
    "SELFTEST_RECORD_DIR=$RECORD_DIR" "SELFTEST_SIDEEFFECT=$SIDEEFFECT" \
    "SELFTEST_PROBE_BEHAVIOR=$PROBE_BEHAVIOR" "SELFTEST_RUN_MARKER=$RUN_MARKER" \
    "SELFTEST_ARTIFACT=$ARTIFACT"
}

run_script() { # $1=対象スクリプト 残り=引数。$STUB_DIR を PATH 先頭に置き、制限 PATH で走らせる
  rs_target="$1"; shift
  rs_env=()
  while IFS= read -r e; do rs_env[${#rs_env[@]}]="$e"; done <<<"$(agent_env)"
  rc=0
  if [ -n "$AGENT_CWD" ]; then
    ( cd "$AGENT_CWD" && guard env "${rs_env[@]}" ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"} \
      bash "$rs_target" "$@" ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  else
    guard env "${rs_env[@]}" ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"} \
      bash "$rs_target" "$@" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  fi
  return "$rc"
}

run_agent() { # 残り=引数
  rc=0
  run_script "$TARGET" "$@" || rc=$?
  if [ "$VERBOSE" -eq 1 ]; then
    echo "--- args: $* (exit=$rc)"; echo "  stdout:"; sed 's/^/    /' "$CASE_OUT"; echo "  stderr:"; sed 's/^/    /' "$CASE_ERR"
  fi
  return "$rc"
}

run_mutant() { # $1=変異版のパス 残り=引数
  mu="$1"; shift
  rc=0
  run_script "$mu" "$@" || rc=$?
  return "$rc"
}

check() { # $1=ケース名 $2=期待終了コード $3=実際
  name="$1"; want="$2"; got="$3"
  if [ "$got" -ne "$want" ]; then
    ng "$name (期待 exit=$want / 実際 exit=$got)"
    echo "--- $name の stderr ---" >&2; cat "$CASE_ERR" >&2
    return 1
  fi
  ok "$name (exit=$got)"
  return 0
}

assert_error_format() { # $1=ケース名。stderr が ERROR [理由コード] 形式か
  if grep -qE '^ERROR \[[a-z-]+\] ' "$CASE_ERR"; then ok "$1: stderr が ERROR [理由コード] 形式"; else
    ng "$1: stderr が ERROR [理由コード] 形式"; echo "--- stderr ---" >&2; cat "$CASE_ERR" >&2; fi
}

assert_stdout_equals() { # $1=ケース名 $2=期待文字列
  if [ "$(cat "$CASE_OUT")" = "$2" ]; then ok "$1: stdout が期待どおり"; else
    ng "$1: stdout が期待どおり"; echo "--- 実際の stdout ---" >&2; cat "$CASE_OUT" >&2; fi
}

reset_record() { rm -rf "$RECORD_DIR"; mkdir -p "$RECORD_DIR"; }
calls_file() { printf '%s' "$RECORD_DIR/calls.tsv"; }
call_field() { # $1=行番号(1 始まり) $2=フィールド名
  sed -n "${1}p" "$(calls_file)" 2>/dev/null | tr '\t' '\n' | grep -a "^$2=" | sed "s/^$2=//"
}

echo "===== implement-agent.sh 自己テスト(対象: $TARGET)====="

# ── A. 起動構文(§12-8)の契約 ──

# A1. 必須引数がすべて揃った最小形は成功する(= 契約の必須 3 つだけで動く)
reset_record
STUB_DIR="$WORK/pathbin"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-a1.md"; rc=$?
if check "成功: 必須引数の最小形(生出力をそのまま返す)" 0 "$rc"; then
  assert_stdout_equals "成功" "外部 implementer の生出力: mode=workspace-write"
fi

# A2. §12-8 の任意引数をすべて同時に渡しても受理される(引数表との一致)
reset_record
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --model "some-model" --log-file "$WORK/log-a2.md" --probe-timeout 10 --run-timeout 20; rc=$?
if check "§12-8 の任意引数を全て同時に受理する" 0 "$rc"; then
  if [ "$(call_field 2 model)" = "some-model" ]; then ok "--model が本実行の argv に入る"; else
    ng "--model が本実行の argv に入る(実際: $(call_field 2 model))"; fi
fi

# A3. --cwd の省略は usage(レビュー経路との最大の差)
rm -f "$SIDEEFFECT"
STUB_DIR="$WORK/evilbin"
run_agent --runner codex --prompt-file "$PROMPT" --probe-timeout 10 --log-file "$WORK/log-a3.md"; rc=$?
check "usage: --cwd の省略を拒否" 2 "$rc" && assert_error_format "--cwd 省略"
if [ -e "$SIDEEFFECT" ]; then ng "PoC 非再現(--cwd 省略で起動しない)"; else ok "PoC 非再現(--cwd 省略で起動しない)"; fi

# A4. --cwd が空文字でも usage(空を「省略」として受理しない)
run_agent --runner codex --prompt-file "$PROMPT" --cwd "" --log-file "$WORK/log-a4.md"; rc=$?
check "usage: 空の --cwd を拒否" 2 "$rc"

# A5. --cwd が存在しないディレクトリなら usage
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$WORK/no-such-dir" --log-file "$WORK/log-a5.md"; rc=$?
check "usage: 存在しない --cwd を拒否" 2 "$rc"

# A5b. `[ -d ]` は通るが検索権限が無くて cd できない --cwd も usage に寄せる(テストの穴 8)。
#      root は権限検査を素通りするので飛ばす
if [ "$(id -u)" -ne 0 ]; then
  mkdir -p "$WORK/noexec-cwd"; chmod 600 "$WORK/noexec-cwd"
  run_agent --runner codex --prompt-file "$PROMPT" --cwd "$WORK/noexec-cwd" --log-file "$WORK/log-a5b.md"; rc=$?
  chmod 755 "$WORK/noexec-cwd"
  if check "usage: cd できない --cwd を internal でなく usage に分類" 2 "$rc"; then
    assert_error_format "cd できない --cwd"
  fi
else
  ok "usage: cd できない --cwd を internal でなく usage に分類(root のため飛ばす)"
fi

# A6. --prompt-file の省略・空・不在
run_agent --runner codex --cwd "$CWD_TARGET" --log-file "$WORK/log-a6.md"; rc=$?
check "usage: --prompt-file の省略を拒否" 2 "$rc"
run_agent --runner codex --prompt-file "" --cwd "$CWD_TARGET" --log-file "$WORK/log-a6b.md"; rc=$?
check "usage: 空の --prompt-file を拒否" 2 "$rc"
run_agent --runner codex --prompt-file "$WORK/no-such-prompt.md" --cwd "$CWD_TARGET"; rc=$?
check "usage: 存在しない --prompt-file を拒否" 2 "$rc"

# A6b. 空・空白のみのプロンプトファイルを書き込みモードで起動しない(テストの穴 9)
: >"$WORK/empty-prompt.md"
printf '\n\n\n' >"$WORK/blank-prompt.md"
printf ' \t\n  \n' >"$WORK/space-prompt.md"
for pf in empty-prompt blank-prompt space-prompt; do
  rm -f "$SIDEEFFECT"
  STUB_DIR="$WORK/evilbin"
  run_agent --runner codex --prompt-file "$WORK/$pf.md" --cwd "$CWD_TARGET" \
    --probe-timeout 10 --log-file "$WORK/log-a6-$pf.md"; rc=$?
  check "usage: 中身の無いプロンプトファイル($pf)を拒否" 2 "$rc"
  if [ -e "$SIDEEFFECT" ]; then ng "PoC 非再現($pf で起動しない)"; else ok "PoC 非再現($pf で起動しない)"; fi
done
STUB_DIR="$WORK/pathbin"

# A7. --runner の省略・空・不正文字
run_agent --prompt-file "$PROMPT" --cwd "$CWD_TARGET"; rc=$?
check "usage: --runner の省略を拒否" 2 "$rc"
run_agent --runner "" --prompt-file "$PROMPT" --cwd "$CWD_TARGET"; rc=$?
check "usage: 空の --runner を拒否" 2 "$rc"
run_agent --runner "bad name" --prompt-file "$PROMPT" --cwd "$CWD_TARGET"; rc=$?
check "usage: 不正な文字を含むランナー名を拒否" 2 "$rc"

# A8. 値の無いオプション
run_agent --runner; rc=$?
check "usage: 値の無いオプション" 2 "$rc"

# A9. タイムアウト 0 は指定不可(両方)
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --probe-timeout 0; rc=$?
check "usage: --probe-timeout 0 を拒否" 2 "$rc"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --run-timeout 0; rc=$?
check "usage: --run-timeout 0 を拒否" 2 "$rc"

# A10. --model の信頼モデル(フラグへの化けを防ぐ)
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --model "--dangerous"; rc=$?
check "usage: '-' で始まるモデル名を拒否" 2 "$rc"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --model "a b"; rc=$?
check "usage: 空白を含むモデル名を拒否" 2 "$rc"

# ── B. 信頼モデル: 既定表外と上書き引数を受け付ける経路が存在しない ──

# B1. 実装用既定表に無いランナー名は usage(レビュー用既定表の名前でも受け付けない = 別表であることの証明)。
#     §11 が「レビュー用既定表にある名前を含む」と書く以上、そこにある名前は全て並べる
for r in stubrunner gemini cursor-agent; do
  rm -f "$SIDEEFFECT"
  STUB_DIR="$WORK/evilbin"
  run_agent --runner "$r" --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --log-file "$WORK/log-b1-$r.md"; rc=$?
  check "信頼モデル: 既定表外のランナー名 '$r' を usage で拒否" 2 "$rc"
  if [ -e "$SIDEEFFECT" ]; then ng "PoC 非再現($r で副作用が起きない)"; else ok "PoC 非再現($r で副作用が起きない)"; fi
done

# B2. --command / --writeflag に相当する引数は存在しない(渡したら usage・副作用なし)
for opt in --command --writeflag --readonly-flag; do
  rm -f "$SIDEEFFECT"
  STUB_DIR="$WORK/evilbin"
  run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
    "$opt" "bash $WORK/evilbin/codex --sandbox workspace-write" --log-file "$WORK/log-b2.md"; rc=$?
  check "信頼モデル: $opt を持たない(usage で拒否)" 2 "$rc"
  if [ -e "$SIDEEFFECT" ]; then ng "PoC 非再現($opt 経路で副作用が起きない)"; else ok "PoC 非再現($opt 経路で副作用が起きない)"; fi
done

# ── C. 判定 1〜3′(存在・自ホスト・モード確立)──

# C1. 未検出(制限 PATH のみでスタブを置かない)
STUB_DIR="$WORK/empty"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --log-file "$WORK/log-c1.md"; rc=$?
check "失敗: 未検出(not-found)" 3 "$rc" && assert_error_format "not-found"

# C2. 自ホスト拒否
rm -f "$SIDEEFFECT"
rc=0
guard env PATH="$WORK/evilbin:$SAFEPATH" DEV_WORKFLOW_HOST_CLI="codex" SELFTEST_SIDEEFFECT="$SIDEEFFECT" \
  bash "$TARGET" --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --log-file "$WORK/log-c2.md" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
check "失敗: 自ホスト拒否(self-host)" 4 "$rc"
if [ -e "$SIDEEFFECT" ]; then ng "自ホスト拒否で起動していない"; else ok "自ホスト拒否で起動していない"; fi

# C3. 判定 3′: ヘルプに書き込みフラグが無い → no-writemode(11)。5(no-readonly)に化けない
STUB_DIR="$WORK/nohelp"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --log-file "$WORK/log-c3.md"; rc=$?
if check "失敗: モード未確立(ヘルプ照合)= no-writemode" 11 "$rc"; then
  assert_error_format "no-writemode"
  if grep -q 'no-writemode' "$CASE_ERR"; then ok "理由コードが no-writemode"; else ng "理由コードが no-writemode"; fi
  if grep -q '読み取り専用' "$CASE_ERR"; then
    ng "判定 3′ のエラー文が書き込み範囲の語になっている(「読み取り専用」が混入)"
  else
    ok "判定 3′ のエラー文が書き込み範囲の語になっている"
  fi
fi

# C4. サブコマンド側の --help でしか出さないランナーも通る
STUB_DIR="$WORK/subhelp"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-c4.md"; rc=$?
check "モード確立: <bin> <sub> --help でのヘルプ照合" 0 "$rc"

# C5. --help がハングしたら probe-timeout(11 に化けない)
STUB_DIR="$WORK/helphang"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --log-file "$WORK/log-c5.md"; rc=$?
check "失敗: ヘルプ照合のタイムアウト" 7 "$rc" && assert_error_format "ヘルプ照合タイムアウト"

# ── D. 判定 4′(疎通プローブ)と本実行の分離 ──

# D1. プローブは読み取り専用モード + 一時ディレクトリ、本実行は書き込みモード + --cwd
reset_record
STUB_DIR="$WORK/pathbin"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-d1.md"; rc=$?
if check "プローブと本実行の 2 回が記録される" 0 "$rc"; then
  n="$(wc -l <"$(calls_file)" 2>/dev/null | tr -d ' ')"
  if [ "${n:-0}" -eq 2 ]; then ok "外部 CLI の起動は 2 回(プローブ + 本実行)"; else
    ng "外部 CLI の起動は 2 回(実際 ${n:-0} 回)"; cat "$(calls_file)" >&2; fi
  probe_mode="$(call_field 1 mode)"; probe_cwd="$(call_field 1 cwd)"; probe_arg="$(call_field 1 lastarg)"
  run_mode="$(call_field 2 mode)";  run_cwd="$(call_field 2 cwd)";  run_arg="$(call_field 2 lastarg)"
  if [ "$probe_mode" = "read-only" ]; then ok "プローブは読み取り専用モードで起動する"; else
    ng "プローブは読み取り専用モードで起動する(実際: '$probe_mode')"; fi
  if [ "$run_mode" = "workspace-write" ]; then ok "本実行は書き込みモードで起動する"; else
    ng "本実行は書き込みモードで起動する(実際: '$run_mode')"; fi
  if [ "$run_cwd" = "$CWD_TARGET" ]; then ok "本実行の cwd は --cwd で渡したディレクトリ"; else
    ng "本実行の cwd は --cwd で渡したディレクトリ(実際: '$run_cwd')"; fi
  if [ -n "$probe_cwd" ] && [ "$probe_cwd" != "$CWD_TARGET" ] && [ "$probe_cwd" != "$WORK/proj" ]; then
    ok "プローブの cwd は --cwd ともスクリプトの起動場所とも別"
  else
    ng "プローブの cwd は --cwd ともスクリプトの起動場所とも別(実際: '$probe_cwd')"; fi
  if [ -n "$probe_cwd" ] && [ ! -e "$probe_cwd" ]; then ok "プローブ用の一時ディレクトリは終了時に消える"; else
    ng "プローブ用の一時ディレクトリは終了時に消える(残存: '$probe_cwd')"; fi
  case "$probe_arg" in *ping*) ok "プローブには短いプロンプトを渡す" ;; *) ng "プローブには短いプロンプトを渡す(実際: '$probe_arg')" ;; esac
  case "$run_arg" in *実装*) ok "本実行には --prompt-file の内容を渡す" ;; *) ng "本実行には --prompt-file の内容を渡す(実際: '$run_arg')" ;; esac
fi

# D2. ログに判定結果・解決後コマンド・渡した cwd・生出力が残る(§12-8「スクリプトが残すもの」)
for pat in '解決後のコマンド' '渡した --cwd' '実行ファイルの存在(判定 1)' 'ホスト CLI 判定(判定 2' \
           '書き込み範囲(argv 照合)' '書き込み範囲(ヘルプ照合)' '疎通プローブ: OK' '### 生出力' '背景実行'; do
  if grep -qF -- "$pat" "$WORK/log-d1.md"; then ok "ログに「$pat」が残る"; else
    ng "ログに「$pat」が残る"; fi
done

# D3. プローブ失敗 / 空出力 / 無応答
STUB_DIR="$WORK/probefail"; PROBE_BEHAVIOR="fail"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --log-file "$WORK/log-d3a.md"; rc=$?
if check "失敗: 疎通失敗(probe-failed)" 6 "$rc"; then
  # §12-8 の「失敗時は得られた分の生出力」はプローブ失敗にも掛かる(理由は生出力側に出る)
  if grep -qF '認証トークンの期限が切れています' "$CASE_OUT"; then ok "プローブ失敗時も得られた分の生出力を stdout に流す"; else
    ng "プローブ失敗時も得られた分の生出力を stdout に流す"; cat "$CASE_OUT" >&2; fi
  if grep -qF '認証トークンの期限が切れています' "$WORK/log-d3a.md"; then ok "プローブ失敗時も生出力をログに残す"; else
    ng "プローブ失敗時も生出力をログに残す"; fi
fi
STUB_DIR="$WORK/probeempty"; PROBE_BEHAVIOR="empty"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --log-file "$WORK/log-d3b.md"; rc=$?
check "失敗: プローブの出力が空(probe-failed)" 6 "$rc"
STUB_DIR="$WORK/probehang"; PROBE_BEHAVIOR="hang"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 2 --log-file "$WORK/log-d3c.md"; rc=$?
if check "失敗: プローブの無応答(probe-timeout)" 7 "$rc"; then
  if grep -qF 'プローブ生出力' "$WORK/log-d3c.md"; then ok "プローブ無応答時も生出力の枠をログに残す"; else
    ng "プローブ無応答時も生出力の枠をログに残す"; fi
fi
PROBE_BEHAVIOR="none"

# ── E. 本実行の終了コード契約 ──

# E1. 本実行だけ失敗 → run-failed(8)。引き継ぎ専用コードは無く通常の失敗コードを返す
STUB_DIR="$WORK/runfail"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-e1.md"; rc=$?
if check "失敗: 本実行の失敗(run-failed)" 8 "$rc"; then
  assert_error_format "run-failed"
  if grep -qF '途中まで書いて力尽きた' "$WORK/log-e1.md"; then ok "失敗時も生出力をログに残す(引き継ぎの手がかり)"; else
    ng "失敗時も生出力をログに残す(引き継ぎの手がかり)"; fi
  if grep -qF '途中まで書いて力尽きた' "$CASE_OUT"; then ok "失敗時も得られた分の生出力を stdout に流す"; else
    ng "失敗時も得られた分の生出力を stdout に流す"; fi
fi

# E2. 本実行だけハング → run-timeout(9)
STUB_DIR="$WORK/runhang"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 2 --log-file "$WORK/log-e2.md"; rc=$?
check "失敗: 本実行のタイムアウト(run-timeout)" 9 "$rc"

# E3. プロンプト長超過 → prompt-too-large(12)
head -c 200000 /dev/zero | tr '\0' 'a' >"$WORK/big-prompt.md"
STUB_DIR="$WORK/pathbin"
run_agent --runner codex --prompt-file "$WORK/big-prompt.md" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --log-file "$WORK/log-e3.md"; rc=$?
check "失敗: プロンプト長超過(prompt-too-large)" 12 "$rc"

# E4. 想定外の失敗は ERR trap が exit 20 にする(ログの置き場は作れるが、ログファイル自体が
#     ディレクトリで書けない — 事前検査では捕まえない「本当に想定外」の形)
mkdir -p "$WORK/logfile-is-a-dir"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --log-file "$WORK/logfile-is-a-dir"; rc=$?
if [ "$rc" -eq 20 ] && grep -q '^ERROR \[internal\]' "$CASE_ERR"; then ok "想定外の失敗を ERR trap が exit 20 で報告 (exit=$rc)"; else
  ng "想定外の失敗を ERR trap が exit 20 で報告 (実際 exit=$rc)"; cat "$CASE_ERR" >&2; fi

# E4b. ログの置き場が作れないケースは事前検査で usage(2)。生の mkdir エラー + internal(20) に倒さない
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --log-file "/dev/null/nested/x.md"; rc=$?
if check "usage: --log-file の置き場を作れない" 2 "$rc"; then
  assert_error_format "ログ置き場を作れない"
  if grep -qF -- '--log-file の置き場' "$CASE_ERR"; then ok "ログ置き場のエラーが理由の分かる文になっている"; else
    ng "ログ置き場のエラーが理由の分かる文になっている"; cat "$CASE_ERR" >&2; fi
fi

# E4c. 既定のログ置き場(.claude/reviews)が作れないときも usage(2)。root では作れてしまうので飛ばす
if [ "$(id -u)" -ne 0 ]; then
  mkdir -p "$WORK/ro-cwd"; chmod 555 "$WORK/ro-cwd"
  AGENT_CWD="$WORK/ro-cwd"
  run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET"; rc=$?
  AGENT_CWD=""
  chmod 755 "$WORK/ro-cwd"
  if check "usage: 既定のログ置き場を作れない" 2 "$rc"; then
    if grep -qF -- '既定のログ置き場' "$CASE_ERR"; then ok "既定ログ置き場のエラーが理由の分かる文になっている"; else
      ng "既定ログ置き場のエラーが理由の分かる文になっている"; cat "$CASE_ERR" >&2; fi
  fi
else
  ok "usage: 既定のログ置き場を作れない(root のため飛ばす)"
fi

# E5. TERM を無視するプロセスでもタイムアウトが成立し、残骸を残さない
leak_cleanup; sleep 1
STUB_DIR="$WORK/termignore"
start=$(date +%s)
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 2 --log-file "$WORK/log-e5.md"; rc=$?
elapsed=$(( $(date +%s) - start ))
if [ "$rc" -eq 7 ] && [ "$elapsed" -lt 30 ]; then ok "TERM 無視スタブでもタイムアウト成立 (exit=$rc / ${elapsed}s)"; else
  ng "TERM 無視スタブでもタイムアウト成立 (期待 exit=7 かつ 30 秒未満 / 実際 exit=$rc ${elapsed}s)"; fi
sleep 2
if [ "$(leak_count)" -eq 0 ]; then ok "タイムアウト後にプロセスを残さない"; else
  ng "タイムアウト後にプロセスを残さない(残存 $(leak_count) 件)"; leak_cleanup; fi

# ── F. --dry-run と実装用既定表(§12-6)の同期 ──

# F0. 既定表の照合に使う「実測の argv」を取り直す(--model 付き。テストの穴 5・6)
reset_record
STUB_DIR="$WORK/pathbin"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --model "sync-model" \
  --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-f0.md"; rc=$?
check "既定表の同期: --model 付きで 2 回起動する" 0 "$rc"
if [ "$(call_field 1 model)" = "sync-model" ]; then ok "--model がプローブの argv にも入る"; else
  ng "--model がプローブの argv にも入る(実際: $(call_field 1 model))"; fi

# F1. --dry-run は起動しない
rm -f "$SIDEEFFECT"
STUB_DIR="$WORK/evilbin"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --dry-run --log-file "$WORK/log-f1.md"; rc=$?
check "dry-run: 静的検査のみ" 0 "$rc"
if [ -e "$SIDEEFFECT" ]; then ng "dry-run で起動していない"; else ok "dry-run で起動していない"; fi
DRY_OUT="$(cat "$CASE_OUT")"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" --dry-run --model "sync-model" \
  --log-file "$WORK/log-f1b.md" >/dev/null 2>&1
DRY_OUT_MODEL="$(cat "$CASE_OUT")"

# F2. --dry-run の解決後コマンドが §12-6 の実装用既定表の「起動コマンド」列と一致する。
# 節を特定してから行を拾う(§4 の表と誤照合しないため。head -1 方式は踏襲しない)
if [ -f "$DOC" ]; then
  SEC="$WORK/sec-12-6.md"
  awk '/^### 12-6\./{f=1;next} /^### /{if(f)exit} f' "$DOC" >"$SEC"
  if [ ! -s "$SEC" ]; then
    ng "既定表の同期: 仕様書から §12-6 の節を取り出せない"
  else
    row="$(grep -E '^\| `codex` \|' "$SEC" | head -1)"
    if [ -z "$row" ]; then
      ng "既定表の同期: §12-6 に codex の行が無い"
    else
      trim() { sed -e 's/^ *//' -e 's/ *$//' -e 's/^`//' -e 's/`$//'; }
      doc_cmd="$(printf '%s' "$row" | awk -F'|' '{print $4}' | trim)"
      doc_write="$(printf '%s' "$row" | awk -F'|' '{print $5}' | trim)"
      doc_probe="$(printf '%s' "$row" | awk -F'|' '{print $6}' | trim)"
      expect="$(printf '%s' "$doc_cmd" | sed -E 's/ [^ ]+ \{model\}//')"
      case "$expect" in
        *'{prompt}'*) expect="$(printf '%s' "$expect" | sed -e 's/{prompt}/<プロンプト>/')" ;;
        *) expect="$expect <プロンプト>" ;;
      esac
      if [ "$DRY_OUT" = "$expect" ]; then ok "既定表の同期: 起動コマンド列と --dry-run 出力が一致"; else
        ng "既定表の同期: 起動コマンド列と --dry-run 出力が一致(仕様書=\"$expect\" / 実装=\"$DRY_OUT\")"; fi
      # --model 付きの解決後コマンド(テストの穴 5): {model} が実名に置換され、直前のフラグも残る
      expect_model="$(printf '%s' "$doc_cmd" | sed -e 's/{model}/sync-model/')"
      case "$expect_model" in
        *'{prompt}'*) expect_model="$(printf '%s' "$expect_model" | sed -e 's/{prompt}/<プロンプト>/')" ;;
        *) expect_model="$expect_model <プロンプト>" ;;
      esac
      if [ "$DRY_OUT_MODEL" = "$expect_model" ]; then ok "既定表の同期: --model 付きの解決後コマンドが起動コマンド列と一致"; else
        ng "既定表の同期: --model 付きの解決後コマンドが起動コマンド列と一致(仕様書=\"$expect_model\" / 実装=\"$DRY_OUT_MODEL\")"; fi
      case "$DRY_OUT" in
        *"$doc_write"*) ok "既定表の同期: 書き込みフラグ列が解決後コマンドに含まれる" ;;
        *) ng "既定表の同期: 書き込みフラグ列が解決後コマンドに含まれる(仕様書=\"$doc_write\")" ;;
      esac
      # 「フラグ 値」の対が**丸ごと**実測の argv に載るかを見る。末尾の値だけを比べる方式は
      # フラグ名の改名(--sandbox → --mode)を素通りし、値が空でも PASS してしまう
      for pair in "write:$doc_write:2" "probe:$doc_probe:1"; do
        kind="${pair%%:*}"; rest="${pair#*:}"; flag="${rest%:*}"; row="${rest##*:}"
        [ "$kind" = "write" ] && label="書き込みフラグ列と本実行の実測 argv" || label="プローブ用の読み取り専用フラグ列とプローブの実測 argv"
        if [ -z "$flag" ]; then
          ng "既定表の同期: $label(仕様書の列が空)"
        elif [ "$flag" = "${flag% *}" ]; then
          ng "既定表の同期: $label(仕様書の列が「フラグ 値」の形でない: \"$flag\")"
        else
          got_argv="$(call_field "$row" argv)"
          case " $got_argv " in
            *" $flag "*) ok "既定表の同期: $label が一致" ;;
            *) ng "既定表の同期: $label が一致(仕様書=\"$flag\" / 実測 argv=\"$got_argv\")" ;;
          esac
        fi
      done
    fi
  fi
else
  ng "既定表の同期: 仕様書 $DOC が見つからない"
fi

# F3. 契約側の実体化(§12-6 の仮置き注記の除去・§11 と冒頭の実ファイル名)
if [ -f "$DOC" ]; then
  if grep -qF 'エントリは #51-B で追加する' "$DOC"; then
    ng "契約: §12-6 の「エントリは #51-B で追加する」注記が除去されている"
  else
    ok "契約: §12-6 の「エントリは #51-B で追加する」注記が除去されている"
  fi
  for f in implement-agent.sh implement-agent-selftest.sh; do
    n="$(grep -cF "$f" "$DOC")"
    if [ "${n:-0}" -ge 2 ]; then ok "契約: $f が仕様書に実名で載る(${n} 箇所)"; else
      ng "契約: $f が仕様書に実名で載る(冒頭の同期義務と §11 の 2 箇所以上。実際 ${n:-0} 箇所)"; fi
  done
fi

# ── G. 変異テスト(検査の実効性の裏取り)──
# 「PASS したから検査が働いている」とは言えないので、検査を壊すと結果が変わることを実測する。

# G1. 変異 A: 実装用既定表の起動コマンドから書き込みフラグを落とす → argv 照合が 11 で捕まえる
MUT_A="$WORK/mut-a.sh"
sed 's|codex exec --sandbox workspace-write -m {model}|codex exec -m {model}|' "$TARGET" >"$MUT_A"
if ! grep -qF "codex exec -m {model}" "$MUT_A"; then
  ng "変異テスト: 既定表を壊した版を作れない(起動コマンド列の形が変わった)"
else
  STUB_DIR="$WORK/pathbin"
  run_mutant "$MUT_A" --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
    --probe-timeout 10 --log-file "$WORK/log-g1.md"; rc=$?
  if [ "$rc" -eq 11 ]; then ok "変異 A: 既定表から書き込みフラグが消えると argv 照合が止める (exit=$rc)"; else
    ng "変異 A: 既定表から書き込みフラグが消えると argv 照合が止める (期待 exit=11 / 実際 exit=$rc)"; cat "$CASE_ERR" >&2; fi

  # G2. 変異 B: 変異 A に加えて argv 照合そのものを無効化 → 11 が出なくなる
  #     (= 11 を出していたのが argv 照合であることの証明)
  MUT_B="$WORK/mut-b.sh"
  sed 's|^if ! tokens_in_argv |if false \&\& ! tokens_in_argv |' "$MUT_A" >"$MUT_B"
  if ! grep -q '^if false && ! tokens_in_argv ' "$MUT_B"; then
    ng "変異テスト: argv 照合を無効化した版を作れない(判定 3′-argv の形が変わった)"
  else
    run_mutant "$MUT_B" --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
      --probe-timeout 10 --log-file "$WORK/log-g2.md"; rc=$?
    if [ "$rc" -ne 11 ]; then ok "変異 B: argv 照合を消すと 11 が出なくなる (exit=$rc)"; else
      ng "変異 B: argv 照合を消すと 11 が出なくなる (exit=11 のまま = 検査以外の何かが立っている)"; fi
  fi
fi

# G3. 変異 C: ヘルプ照合の拒否を無効化 → ヘルプに書き込みフラグが無くても 11 が出なくなる
MUT_C="$WORK/mut-c.sh"
sed 's|^  if \[ "$help_found" -ne 1 \]; then$|  if false; then|' "$TARGET" >"$MUT_C"
if ! grep -q '^  if false; then$' "$MUT_C"; then
  ng "変異テスト: ヘルプ照合を無効化した版を作れない(判定 3′-help の形が変わった)"
else
  STUB_DIR="$WORK/nohelp"
  run_mutant "$MUT_C" --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
    --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-g3.md"; rc=$?
  if [ "$rc" -ne 11 ]; then ok "変異 C: ヘルプ照合を消すと 11 が出なくなる (exit=$rc)"; else
    ng "変異 C: ヘルプ照合を消すと 11 が出なくなる (exit=11 のまま = 検査以外の何かが立っている)"; fi
fi

# G4. 変異 D: 既定表の書き込みフラグ列を「フラグ 値」でない形に壊す → 11 ではなく 20(internal)。
#     11 は「ランナーの仕様が変わって書き込み範囲を確立できない」の意味なので、
#     こちらのスクリプト側の退行に流用すると呼び出し側を誤誘導する
MUT_D="$WORK/mut-d.sh"
sed "s|codex) printf '%s' '--sandbox workspace-write' ;;|codex) printf '%s' '--sandbox' ;;|" "$TARGET" >"$MUT_D"
if ! grep -qF "codex) printf '%s' '--sandbox' ;;" "$MUT_D"; then
  ng "変異テスト: 既定表の書き込みフラグ列を壊した版を作れない(default_writeflag の形が変わった)"
else
  STUB_DIR="$WORK/pathbin"
  run_mutant "$MUT_D" --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
    --probe-timeout 10 --log-file "$WORK/log-g4.md"; rc=$?
  if [ "$rc" -eq 20 ]; then ok "変異 D: 既定表の形が壊れたら internal(20)(11 に化けない) (exit=$rc)"; else
    ng "変異 D: 既定表の形が壊れたら internal(20)(11 に化けない) (期待 exit=20 / 実際 exit=$rc)"; cat "$CASE_ERR" >&2; fi
fi
STUB_DIR="$WORK/pathbin"

# ── H. 書き込み権限が付くことで初めて要る機構(決定 40〜42)──

# H1. 決定 41: スクリプト自身への TERM / HUP / INT で、走行中の外部 CLI の子を道連れにする。
#     呼び出し側は §12-6 の帰結として背景実行するので、中止操作は**シグナルとして届く**。
#     子が生き残ると、書き込みモードのまま走るプロセスの傍らで §12-3 の再取得・比較を行うことになる
leak_cleanup; sleep 1
STUB_DIR="$WORK/sigstub"
# INT は「保険」。**非対話 shell の非同期ジョブは SIGINT を SIG_IGN で受け継ぎ、bash は
# 「起動時に無視されていたシグナル」には trap を張れない**(POSIX の規定)。つまり INT が
# 効くかどうかは起動側の文脈で決まり、スクリプト側の実装では動かせない。先に同じ起動の形で
# 測り、張れない文脈ではケースを飛ばす(FAIL にすると実装のせいでない失敗を報告してしまう)
cat >"$WORK/bin/inttrap.sh" <<'EOF'
#!/usr/bin/env bash
trap 'exit 130' INT
sleep 10 &
wait $!
exit 0
EOF
chmod +x "$WORK/bin/inttrap.sh"
bash "$WORK/bin/inttrap.sh" & int_probe_pid=$!
sleep 1; kill -INT "$int_probe_pid" 2>/dev/null
int_probe_rc=0; wait "$int_probe_pid" || int_probe_rc=$?
kill -9 "$int_probe_pid" 2>/dev/null
INT_TRAPPABLE=0
[ "$int_probe_rc" -eq 130 ] && INT_TRAPPABLE=1
for spec in "TERM:143" "HUP:129" "INT:130"; do
  sig="${spec%%:*}"; want="${spec##*:}"
  if [ "$sig" = "INT" ] && [ "$INT_TRAPPABLE" -eq 0 ]; then
    ok "中止(INT): この起動文脈では SIGINT が SIG_IGN で継承され trap を張れないため飛ばす(bash の制約。TERM / HUP が本線)"
    continue
  fi
  rm -f "$RUN_MARKER"
  rs_env=()
  while IFS= read -r e; do rs_env[${#rs_env[@]}]="$e"; done <<<"$(agent_env)"
  # --run-timeout は「シグナルが効かなかったとき、いつまでも待たずに FAIL に落ちる」ための保険。
  # 正常な経路ではタイムアウト前にシグナルで止まる
  env "${rs_env[@]}" bash "$TARGET" --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
    --probe-timeout 10 --run-timeout 45 --log-file "$WORK/log-h1-$sig.md" \
    >"$CASE_OUT" 2>"$CASE_ERR" &
  agent_pid=$!
  waited=0
  while [ ! -e "$RUN_MARKER" ] && [ "$waited" -lt 60 ]; do sleep 1; waited=$((waited + 1)); done
  sleep 1
  if [ ! -e "$RUN_MARKER" ]; then
    ng "中止($sig): 本実行まで到達しない(治具の失敗)"
    kill -9 "$agent_pid" 2>/dev/null
    wait "$agent_pid" 2>/dev/null
    leak_cleanup
    continue
  fi
  kill -"$sig" "$agent_pid" 2>/dev/null
  rc=0; wait "$agent_pid" || rc=$?
  sleep 2
  if [ "$rc" -eq "$want" ]; then ok "中止($sig): 終了コード $want で終わる"; else
    ng "中止($sig): 終了コード $want で終わる(実際 exit=$rc)"; cat "$CASE_ERR" >&2; fi
  if [ "$(sig_leak_count)" -eq 0 ]; then ok "中止($sig): 外部ランナーの子を残さない"; else
    ng "中止($sig): 外部ランナーの子を残さない(残存 $(sig_leak_count) 件)"; leak_cleanup; fi
  if grep -qE '^ERROR \[[a-z-]+\] ' "$CASE_ERR"; then ok "中止($sig): stderr が ERROR [理由コード] 形式"; else
    ng "中止($sig): stderr が ERROR [理由コード] 形式"; cat "$CASE_ERR" >&2; fi
  if grep -qF '**中止**' "$WORK/log-h1-$sig.md"; then ok "中止($sig): ログに中止が残る"; else
    ng "中止($sig): ログに中止が残る"; fi
  if grep -qF '途中まで書いた' "$CASE_OUT"; then ok "中止($sig): 中止時点までの生出力を stdout に流す"; else
    ng "中止($sig): 中止時点までの生出力を stdout に流す"; cat "$CASE_OUT" >&2; fi
done
leak_cleanup

# H2. 決定 40: 本実行の出力を受ける scratch は「外部の書き込み範囲外」の保護領域にある。
#     /tmp に置くと、記録の対象である本実行のプロセス自身が生出力を偽造できる(PoC)
STUB_DIR="$WORK/forge"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-h2.md"; rc=$?
if check "一時領域: 本実行から scratch を書き換えようとしても成功する" 0 "$rc"; then
  if grep -qF 'FORGED' "$CASE_OUT"; then
    ng "一時領域: 生出力が本実行から偽造されない(stdout が汚染された)"; cat "$CASE_OUT" >&2
  else
    ok "一時領域: 生出力が本実行から偽造されない(stdout)"
  fi
  if grep -qF 'FORGED' "$WORK/log-h2.md"; then
    ng "一時領域: 生出力が本実行から偽造されない(ログが汚染された)"
  else
    ok "一時領域: 生出力が本実行から偽造されない(ログ)"
  fi
  assert_stdout_equals "一時領域: 本物の生出力が返る" "本物の生出力(実装は途中で止まった)"
fi
# 保護領域の実体パスが /tmp・$TMPDIR・--cwd の配下でないこと(ログに残る解決結果で測る)
scratch="$(sed -n 's/^- スクリプトの一時領域(保護領域): \(.*\)(本実行の生出力を受ける.*$/\1/p' "$WORK/log-h2.md")"
if [ -z "$scratch" ]; then
  ng "一時領域: 保護領域の解決先がログに残る"
else
  ok "一時領域: 保護領域の解決先がログに残る"
  bad=0
  for d in /tmp "${TMPDIR:-/tmp}" "$CWD_TARGET"; do
    case "$scratch" in "$d"/*) bad=1 ;; esac
  done
  if [ "$bad" -eq 0 ]; then ok "一時領域: 保護領域が /tmp・\$TMPDIR・--cwd の配下でない($scratch)"; else
    ng "一時領域: 保護領域が /tmp・\$TMPDIR・--cwd の配下でない(実際: $scratch)"; fi
  if [ -e "$scratch" ]; then ng "一時領域: 保護領域は終了時に消える(残存: $scratch)"; else
    ok "一時領域: 保護領域は終了時に消える"; fi
fi

# H3. 保護領域を書き込み範囲外に解決できないときは**起動しない**(§12-2 の縮退③と同型)。
#     HOME も XDG_STATE_HOME も TMPDIR 配下に倒して、候補が全滅する状況を作る
rm -f "$SIDEEFFECT"
STUB_DIR="$WORK/evilbin"
mkdir -p "$WORK/fakehome"
EXTRA_ENV=("HOME=$WORK/fakehome" "XDG_STATE_HOME=$WORK/fakehome/state" "TMPDIR=$WORK")
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --log-file "$WORK/log-h3.md"; rc=$?
EXTRA_ENV=()
if check "一時領域: 保護領域を解決できないと起動しない" 20 "$rc"; then
  assert_error_format "保護領域の解決失敗"
  if grep -qF '保護領域' "$CASE_ERR"; then ok "保護領域の解決失敗が理由の分かる文になっている"; else
    ng "保護領域の解決失敗が理由の分かる文になっている"; cat "$CASE_ERR" >&2; fi
fi
if [ -e "$SIDEEFFECT" ]; then ng "PoC 非再現(保護領域を解決できないときは起動しない)"; else
  ok "PoC 非再現(保護領域を解決できないときは起動しない)"; fi

# H4. 決定 42: 自ホスト判定は「立っている指標すべて」を候補にする。
#     先に一致したものを host とする方式だと、指標が 2 つ立つ環境で後ろの指標が隠れて
#     自分自身を起動してしまう(実装用既定表は 1 件しかなく、この判定が唯一の防波堤)
host_case() { # $1=ケース名 $2=期待終了コード 残り=環境変数
  hc_name="$1"; hc_want="$2"; shift 2
  rm -f "$SIDEEFFECT"
  rc=0
  guard env -u CLAUDECODE -u CODEX_SANDBOX -u CURSOR_AGENT -u DEV_WORKFLOW_HOST_CLI \
    PATH="$WORK/evilbin:$SAFEPATH" SELFTEST_SIDEEFFECT="$SIDEEFFECT" \
    SELFTEST_PROBE_BEHAVIOR="none" SELFTEST_RECORD_DIR="$RECORD_DIR" \
    "$@" bash "$TARGET" --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
    --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-h4.md" \
    >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  check "$hc_name" "$hc_want" "$rc"
  if [ "$hc_want" -eq 4 ]; then
    if [ -e "$SIDEEFFECT" ]; then ng "$hc_name: 起動していない"; else ok "$hc_name: 起動していない"; fi
  fi
}
host_case "自ホスト判定: 同一 CLI の指標だけが立つ" 4 CODEX_SANDBOX=seatbelt
host_case "自ホスト判定: 別 CLI の指標が先に立っても隠れない" 4 CLAUDECODE=1 CODEX_SANDBOX=seatbelt
host_case "自ホスト判定: 3 指標が立っても隠れない" 4 CLAUDECODE=1 CURSOR_AGENT=1 CODEX_SANDBOX=seatbelt
host_case "自ホスト判定: 別 CLI の指標だけなら通す" 0 CLAUDECODE=1
host_case "自ホスト判定: 明示上書きは最優先(他の指標を無視する)" 0 \
  DEV_WORKFLOW_HOST_CLI=selftest-host CODEX_SANDBOX=seatbelt

# ── I. 残りの契約(既定ログ名・特殊なパス・引き継ぎ専用コードの不在)──

# I1. --log-file を省略した**成功**ケース。既定名 implementer-{ランナー}-iter{N} を自動採番する
STUB_DIR="$WORK/pathbin"
mkdir -p "$WORK/autolog"
rm -rf "$WORK/autolog/.claude"
AGENT_CWD="$WORK/autolog"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 20; rc1=$?
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 20; rc2=$?
AGENT_CWD=""
if [ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ]; then ok "既定ログ名: --log-file を省略しても成功する"; else
  ng "既定ログ名: --log-file を省略しても成功する(実際 exit=$rc1 / $rc2)"; cat "$CASE_ERR" >&2; fi
for n in 1 2; do
  if [ -f "$WORK/autolog/.claude/reviews/implementer-codex-iter${n}.md" ]; then
    ok "既定ログ名: implementer-codex-iter${n}.md が自動採番される"
  else
    ng "既定ログ名: implementer-codex-iter${n}.md が自動採番される(実際: $(ls "$WORK/autolog/.claude/reviews" 2>/dev/null | tr '\n' ' '))"
  fi
done

# I2. 空白・改行・グロブ文字を含むパス(cwd は記録治具が TSV 1 行で持てるよう改行を入れない)
ODD_CWD="$WORK/od d*cwd[x]"
ODD_PROMPT="$WORK/od d*pr
ompt[x].md"
ODD_LOG="$WORK/od d*log[x]/im
pl.md"
mkdir -p "$ODD_CWD" "$(dirname "$ODD_LOG")"
cp "$PROMPT" "$ODD_PROMPT"
reset_record
run_agent --runner codex --prompt-file "$ODD_PROMPT" --cwd "$ODD_CWD" \
  --probe-timeout 10 --run-timeout 20 --log-file "$ODD_LOG"; rc=$?
if check "特殊なパス: 空白・改行・グロブ文字を含むパスで成功する" 0 "$rc"; then
  if [ -f "$ODD_LOG" ]; then ok "特殊なパス: 指定どおりの場所にログが出る"; else
    ng "特殊なパス: 指定どおりの場所にログが出る"; fi
  if [ "$(call_field 2 cwd)" = "$ODD_CWD" ]; then ok "特殊なパス: 本実行の cwd がそのまま渡る"; else
    ng "特殊なパス: 本実行の cwd がそのまま渡る(実際: '$(call_field 2 cwd)')"; fi
fi

# I3. 引き継ぎ専用の終了コードが無いこと(§12-7)。
#     「成果が --cwd に残っている」状態でも、返るのは通常の失敗コードだけ
rm -f "$ARTIFACT"
STUB_DIR="$WORK/partial"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-i3a.md"; rc=$?
if check "引き継ぎ: 成果を残して失敗しても run-failed(8)" 8 "$rc"; then
  if [ -f "$ARTIFACT" ]; then ok "引き継ぎ: 実際に --cwd へ成果が残っている(前提の確認)"; else
    ng "引き継ぎ: 実際に --cwd へ成果が残っている(前提の確認)"; fi
fi
rm -f "$ARTIFACT"
STUB_DIR="$WORK/partialhang"
run_agent --runner codex --prompt-file "$PROMPT" --cwd "$CWD_TARGET" \
  --probe-timeout 10 --run-timeout 2 --log-file "$WORK/log-i3b.md"; rc=$?
if check "引き継ぎ: 成果を残してタイムアウトしても run-timeout(9)" 9 "$rc"; then
  if [ -f "$ARTIFACT" ]; then ok "引き継ぎ: タイムアウト時も --cwd へ成果が残っている(前提の確認)"; else
    ng "引き継ぎ: タイムアウト時も --cwd へ成果が残っている(前提の確認)"; fi
fi
rm -f "$ARTIFACT"
# 実装が返しうる失敗コードの集合そのものを見る(引き継ぎ専用コードを足したら増える)
DIE_CODES="$(grep -oE '\bdie [0-9]+' "$TARGET" | awk '{print $2}' | sort -u | tr '\n' ' ')"
UNKNOWN=""
for c in $DIE_CODES; do
  case "$c" in 3|4|6|7|8|9|11|12|20) : ;; *) UNKNOWN="$UNKNOWN $c" ;; esac
done
if [ -z "$UNKNOWN" ]; then ok "引き継ぎ: §6 の終了コード表に無い失敗コードを持たない(実装が返すのは $DIE_CODES)"; else
  ng "引き継ぎ: §6 の終了コード表に無い失敗コードを持たない(表外:$UNKNOWN)"; fi
STUB_DIR="$WORK/pathbin"

echo
printf '%s\n' "$RESULTS"
echo
echo "結果: PASS ${PASS} 件 / FAIL ${FAIL} 件"
[ "$FAIL" -eq 0 ]
