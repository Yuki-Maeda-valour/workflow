#!/usr/bin/env bash
# review-agent.sh の回帰テスト(スタブのみ・外部 CLI 不要・ネットワーク不要)。
#
# 使い方:
#   bash review-agent-selftest.sh            # 全ケース実行
#   bash review-agent-selftest.sh -v         # 各ケースの出力も表示
#   REVIEW_AGENT=<パス> bash review-agent-selftest.sh   # 別の実装を対象にする(変異テスト用)
#
# 検証するのは「終了コードと出力の契約」「信頼モデルが破れないこと」「正規化 2 経路の一致」
# 「プロンプト組み立て(スキーマ指示の付与と冪等)」「ログの置き場の固定(L 節: 置き場の経路)」。
# 期待終了コード: 0=成功 2=usage 3=not-found 4=self-host 5=no-readonly 6=probe-failed
#                 7=probe-timeout 8=run-failed 9=run-timeout 10=parse-failed 12=prompt-too-large
#
# K 節(ログの fd)のケースは、制限した PATH で打ち、対象の一時領域($TMPDIR の下の mktemp -d)を
# このスイートの一時領域へ向ける(FIFO のケースが KILL で終わって対象の後始末が走らなくても、
# 系の一時領域に残らないように)。
#
# 終了コード: 0=全件 PASS / 1=FAIL あり
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${REVIEW_AGENT:-$SCRIPT_DIR/review-agent.sh}"
DOC="$SCRIPT_DIR/../references/external-runners.md"
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

[ -f "$TARGET" ] || { echo "ERROR: $TARGET が無い" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'leak_cleanup; rm -rf "$WORK"' EXIT
mkdir -p "$WORK/proj" "$WORK/bin" "$WORK/pathbin" "$WORK/sandbox-nopython" "$WORK/sandbox-notimeout"
cd "$WORK/proj"

PASS=0
FAIL=0
RESULTS=""
# 自ホスト判定を試験内で固定する(実行環境が Claude Code / Codex のどちらでも同じ結果にする)
export DEV_WORKFLOW_HOST_CLI="selftest-host"

ok()   { PASS=$((PASS + 1)); RESULTS="$RESULTS
PASS  $1"; }
ng()   { FAIL=$((FAIL + 1)); RESULTS="$RESULTS
FAIL  $1"; }

# ── スタブ群 ──
STUB_OK="$WORK/bin/stub-ok.sh"
cat >"$STUB_OK" <<'EOF'
#!/usr/bin/env bash
echo '{"verdict":"CHANGES_REQUESTED","issues":[{"file":"src/a.ts","line":42,"category":"契約整合","severity":"major","description":"説明","suggestion":"提案"}]}'
EOF
cat >"$WORK/bin/stub-envelope.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' '{"type":"result","result":"確認しました。\n\n```json\n{\"verdict\": \"APPROVED\", \"issues\": []}\n```\n"}'
EOF
cat >"$WORK/bin/stub-badjson.sh" <<'EOF'
#!/usr/bin/env bash
echo "JSON ではない自由文の返答"
EOF
cat >"$WORK/bin/stub-fail.sh" <<'EOF'
#!/usr/bin/env bash
echo "認証が必要です" >&2
exit 7
EOF
cat >"$WORK/bin/stub-hang.sh" <<'EOF'
#!/usr/bin/env bash
sleep 120
EOF
cat >"$WORK/bin/hangproc.sh" <<'EOF'
#!/usr/bin/env bash
trap '' TERM
while :; do sleep 1; done
EOF
cat >"$WORK/bin/stub-sideeffect.sh" <<'EOF'
#!/usr/bin/env bash
: >"$SELFTEST_SIDEEFFECT"
echo '{"verdict":"APPROVED","issues":[]}'
EOF
# プローブ(末尾引数に ping を含む)は成功し、本実行だけ失敗する / ハングする
cat >"$WORK/bin/stub-runfail.sh" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do case "$a" in *ping*) echo '{"verdict":"APPROVED","issues":[]}'; exit 0 ;; esac; done
echo "本実行だけ失敗する" >&2
exit 3
EOF
cat >"$WORK/bin/stub-runhang.sh" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do case "$a" in *ping*) echo '{"verdict":"APPROVED","issues":[]}'; exit 0 ;; esac; done
sleep 120
EOF
# issues が配列でない / JSONL(複数ドキュメント)— jq 経路の防御を突く入力
cat >"$WORK/bin/stub-issues-notarray.sh" <<'EOF'
#!/usr/bin/env bash
echo '{"verdict":"APPROVED","issues":"none"}'
EOF
cat >"$WORK/bin/stub-jsonl.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n%s\n' '{"type":"item"}' '{"verdict":"CHANGES_REQUESTED","issues":[{"file":"a.ts","line":1,"category":"規約","severity":"minor","description":"d","suggestion":"s"}]}'
EOF
# verdict が列挙値でない(issues の有無から導出させる)
cat >"$WORK/bin/stub-oddverdict.sh" <<'EOF'
#!/usr/bin/env bash
echo '{"verdict":"looks fine to me","issues":[]}'
EOF
# 実行されたら痕跡を残しつつ cwd と最後の引数(プロンプト)も記録する
cat >"$WORK/bin/stub-record.sh" <<'EOF'
#!/usr/bin/env bash
pwd >"$SELFTEST_RECORD_DIR/cwd.txt"
printf '%s' "${@: -1}" >"$SELFTEST_RECORD_DIR/lastarg.txt"
echo '{"verdict":"APPROVED","issues":[]}'
EOF

# ── スキーマ指示(SCHEMA_BLOCK)の出所 ──
# 差し替え不可の原本 $SCRIPT_DIR/review-agent.sh(REVIEW_AGENT で差し替わる $TARGET ではない)から
# 両端アンカー付きで定義本文を抽出する。抽出が空・先頭行が見出しでない・末尾行が制約文でない場合は
# 即終了する(空チェックだけでは、変異版で抽出が空になり `case … in *""` が常に一致する経路を塞げない)
SCHEMA_SRC="$SCRIPT_DIR/review-agent.sh"
SCHEMA_FILE="$WORK/schema-block.txt"
SCHEMA_HEAD='## 出力形式(review-agent.sh が付与)'
sed -n "/^SCHEMA_BLOCK=\"\$(cat <<'SCHEMA_EOF'$/,/^SCHEMA_EOF$/p" "$SCHEMA_SRC" | sed '1d;$d' >"$SCHEMA_FILE"
bail() { ng "$1"; printf '%s\n' "$RESULTS"; echo; echo "結果: PASS ${PASS} 件 / FAIL ${FAIL} 件(SCHEMA_BLOCK を抽出できないため中断)"; exit 1; }
[ -s "$SCHEMA_FILE" ] || bail "SCHEMA_BLOCK の抽出: 原本 $SCHEMA_SRC から定義本文を抽出できない(両端アンカーの行が無い)"
[ "$(head -n 1 "$SCHEMA_FILE")" = "$SCHEMA_HEAD" ] || bail "SCHEMA_BLOCK の抽出: 先頭行が見出し '$SCHEMA_HEAD' でない(実際: $(head -n 1 "$SCHEMA_FILE"))"
case "$(tail -n 1 "$SCHEMA_FILE")" in
  制約:*を返す。) : ;;
  *) bail "SCHEMA_BLOCK の抽出: 末尾行が制約文('制約:' で始まり 'を返す。' で終わる行)でない(実際: $(tail -n 1 "$SCHEMA_FILE"))" ;;
esac
ok "SCHEMA_BLOCK の抽出: 原本から両端アンカーで定義本文を取得($(wc -l <"$SCHEMA_FILE" | tr -d ' ') 行)"
SCHEMA_BLOCK_TEXT="$(cat "$SCHEMA_FILE")"   # $(…) は末尾の改行だけを落とすので review-agent.sh の値と一致する
export SELFTEST_SCHEMA_FILE="$SCHEMA_FILE"
schema_head_count() { grep -oF -- "$SCHEMA_HEAD" "$1" | wc -l | tr -d ' '; }   # grep -c は行数なので使わない

# スキーマ指示のゲートスタブ: 最後の引数が付与ブロック全体(見出し〜末尾行)で終わるときだけ指摘 JSON を
# 返し、それ以外(プローブの ping を含む)は非空の散文を stdout に出して exit 0(プローブを通す。空出力や
# 非ゼロは probe-failed(6)になり、逆ケースの期待 exit 10 が成立しない)。付与を壊すと本実行が散文になり
# parse-failed(10)で落ちる = 逆ケースをスタブ自身が内包する。cwd と最後の引数の記録は stub-record と同じ
cat >"$WORK/bin/stub-schema-gate.sh" <<'EOF'
#!/usr/bin/env bash
pwd >"$SELFTEST_RECORD_DIR/cwd.txt"
printf '%s' "${@: -1}" >"$SELFTEST_RECORD_DIR/lastarg.txt"
block="$(cat "$SELFTEST_SCHEMA_FILE")"
case "${@: -1}" in
  *"$block") echo '{"verdict":"APPROVED","issues":[]}' ;;
  *) echo "スキーマ指示が末尾に無いので散文で返す" ;;
esac
EOF
# プローブで終了コード 0・出力なし
cat >"$WORK/bin/stub-empty.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
# 先頭が封筒キーを持たないオブジェクトの JSONL(jq 経路の封筒抽出を突く)
cat >"$WORK/bin/stub-jsonl-envelope.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' '{"type":"system","subtype":"init"}'
printf '%s\n' '{"type":"result","result":"確認しました。\n\n```json\n{\"verdict\": \"APPROVED\", \"issues\": []}\n```\n"}'
EOF
chmod +x "$WORK/bin"/*.sh

# 既定表ランナーになりすますスタブ(PATH 先頭に置く)。--help には読み取り専用フラグと値を出す
make_runner_stub() { # $1=名前 $2=ヘルプに出す語(フラグ) $3=値
  cat >"$WORK/pathbin/$1" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  if [ "\$a" = "--help" ]; then
    echo "  $2 <mode>   (choices: \"$3\", \"other\")"
    exit 0
  fi
done
echo '{"verdict":"CHANGES_REQUESTED","issues":[{"file":"src/a.ts","line":42,"category":"契約整合","severity":"major","description":"説明","suggestion":"提案"}]}'
EOF
  chmod +x "$WORK/pathbin/$1"
}
make_runner_stub cursor-agent "--mode" "ask"
make_runner_stub gemini "--approval-mode" "plan"
make_runner_stub codex "--sandbox" "read-only"

# `<bin> --help` には出さず `<bin> exec --help` にだけ読み取り専用フラグを出すランナー
# (codex のようにサブコマンド側にしかフラグが載らない CLI の再現)
mkdir -p "$WORK/subhelp"
cat >"$WORK/subhelp/codex" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "--help" ]; then echo "Usage: codex <command>"; exit 0; fi
if [ "${1:-}" = "exec" ] && [ "${2:-}" = "--help" ]; then
  echo '  --sandbox <MODE>  (choices: "read-only", "workspace-write")'; exit 0
fi
echo '{"verdict":"CHANGES_REQUESTED","issues":[{"file":"src/a.ts","line":42,"category":"契約整合","severity":"major","description":"説明","suggestion":"提案"}]}'
EOF
chmod +x "$WORK/subhelp/codex"

# 信頼モデル検証用: PATH 先頭に置く「起動されたら痕跡を残す cursor-agent」。
# 拒否が退行すると実機ではなくこのスタブが動き、副作用マーカーで検出できる
mkdir -p "$WORK/evilbin" "$WORK/helphang"
cat >"$WORK/evilbin/cursor-agent" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then echo '  --mode <mode>  (choices: "ask", "other")'; exit 0; fi
done
: >"$SELFTEST_SIDEEFFECT"
echo '{"verdict":"APPROVED","issues":[]}'
EOF
# --help がハングする既定表ランナー(ヘルプ照合のタイムアウト経路)
cat >"$WORK/helphang/cursor-agent" <<'EOF'
#!/usr/bin/env bash
sleep 120
EOF
# ラッパー越しの自ホスト判定用
cat >"$WORK/pathbin/mytool" <<'EOF'
#!/usr/bin/env bash
: >"$SELFTEST_SIDEEFFECT"
echo '{"verdict":"APPROVED","issues":[]}'
EOF
# ワークスペース信頼を要求するランナー(cursor-agent の実挙動の再現)。
# --trust が argv に無ければ Workspace Trust Required で落ちる。--help 分岐は
# ヘルプ照合を通すため trust チェックより先に置く
mkdir -p "$WORK/trustbin"
cat >"$WORK/trustbin/cursor-agent" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then echo '  --mode <mode>  (choices: "ask", "other")'; exit 0; fi
done
for a in "$@"; do
  if [ "$a" = "--trust" ]; then
    echo '{"verdict":"CHANGES_REQUESTED","issues":[{"file":"src/a.ts","line":42,"category":"契約整合","severity":"major","description":"説明","suggestion":"提案"}]}'
    exit 0
  fi
done
echo "Workspace Trust Required" >&2
exit 1
EOF
chmod +x "$WORK/evilbin/cursor-agent" "$WORK/helphang/cursor-agent" "$WORK/pathbin/mytool" "$WORK/trustbin/cursor-agent"

echo "レビューしてください" >"$WORK/prompt.md"
SIDEEFFECT="$WORK/sideeffect.marker"
export SELFTEST_SIDEEFFECT="$SIDEEFFECT"

EXPECT_ISSUE_JSON='{
  "verdict": "CHANGES_REQUESTED",
  "issues": [
    {
      "file": "src/a.ts",
      "line": 42,
      "category": "契約整合",
      "severity": "major",
      "description": "説明",
      "suggestion": "提案"
    }
  ]
}'
EXPECT_APPROVED_JSON='{
  "verdict": "APPROVED",
  "issues": []
}'

# 制限付き PATH(sandbox)を作る。$2 以降を除外する
make_sandbox() { # $1=出力ディレクトリ 残り=除外するコマンド名
  sb="$1"; shift
  rm -f "$sb"/*
  for c in bash sh cat grep date dirname basename mkdir sed awk sleep rm mktemp cut wc tr head env ls pkill ps jq python3 timeout gtimeout; do
    skip=0
    for x in "$@"; do [ "$c" = "$x" ] && skip=1; done
    [ "$skip" -eq 1 ] && continue
    src="$(command -v "$c" 2>/dev/null)" || continue
    [ -n "$src" ] && ln -sf "$src" "$sb/$c"
  done
}
make_sandbox "$WORK/sandbox-nopython" python3
make_sandbox "$WORK/sandbox-notimeout" timeout gtimeout

leak_count() { ps -eo args 2>/dev/null | grep -c "^bash $WORK/bin/hangproc.sh"; }
leak_cleanup() {
  for s in hangproc swaphold; do
    ps -eo pid,args 2>/dev/null | awk -v s="bash $WORK/bin/$s.sh" '$0 ~ "[0-9] "s {print $1}' \
      | while read -r p; do kill -9 "$p" 2>/dev/null; done
  done
}

CASE_OUT="$WORK/case.out"
CASE_ERR="$WORK/case.err"

# ケースごとの外側ガード。実装が壊れてハングしても、スイートは止まらず FAIL になる
OUTER_TIMEOUT=90
OUTER_TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then OUTER_TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then OUTER_TIMEOUT_BIN="gtimeout"; fi
guard() {
  if [ -n "$OUTER_TIMEOUT_BIN" ]; then "$OUTER_TIMEOUT_BIN" -k 5 "$OUTER_TIMEOUT" "$@"; else "$@"; fi
}

run_agent() { # 残り=引数。stdout/stderr を分離して保存し、終了コードを返す
  rc=0
  # --cwd はレビュー経路で必須(一時ツリーで起動する契約)。個別のケースが
  # 明示しないときは $WORK を使う —— ここで補わないと、--cwd を検査する目的でない
  # ケースまで usage エラーで落ちる。**必須であること自体は下の専用ケースで検査する**。
  # **先頭に**補う: 末尾に足すと `run_agent --runner` のような値の欠落を見るケースが
  # 「--runner の値が --cwd」になり、別のエラーで落ちて判別力を失う
  ra_args=("$@")
  ra_has_cwd=0
  for ra_a in "$@"; do [ "$ra_a" = "--cwd" ] && ra_has_cwd=1; done
  if [ "$ra_has_cwd" -eq 0 ]; then ra_args=(--cwd "$WORK" "$@"); fi
  guard bash "$TARGET" "${ra_args[@]}" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  if [ "$VERBOSE" -eq 1 ]; then
    echo "--- args: $* (exit=$rc)"; echo "  stdout:"; sed 's/^/    /' "$CASE_OUT"; echo "  stderr:"; sed 's/^/    /' "$CASE_ERR"
  fi
  return "$rc"
}

check() { # $1=ケース名 $2=期待終了コード $3=実際 [$4=追加条件の説明(空なら無し)] [$5=追加条件の真偽 0/1]
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
  if [ "$(cat "$CASE_OUT")" = "$2" ]; then ok "$1: stdout が期待 JSON と一致"; else
    ng "$1: stdout が期待 JSON と一致"; echo "--- 実際の stdout ---" >&2; cat "$CASE_OUT" >&2; fi
}

assert_log_normalized() { # $1=ケース名 $2=ログファイル
  if grep -q '^### 正規化後 JSON' "$2" && grep -q '^### 生出力' "$2"; then
    ok "$1: ログに生出力と正規化後 JSON の節がある"
  else
    ng "$1: ログに生出力と正規化後 JSON の節がある"; fi
}

echo "===== review-agent.sh 自己テスト(対象: $TARGET)====="

# 1. 成功経路(未知ランナー)+ ログ・stdout の検証
run_agent --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --target src/a.ts --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log1.md"; rc=$?
if check "成功: 指摘 JSON を正規化" 0 "$rc"; then
  assert_stdout_equals "成功" "$EXPECT_ISSUE_JSON"
  assert_log_normalized "成功" "$WORK/log1.md"
fi
PY_OK_OUT="$(cat "$CASE_OUT")"

# 2. 封筒 + コードフェンス
run_agent --runner stubrunner --command "bash $WORK/bin/stub-envelope.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log2.md"; rc=$?
if check "成功: 封筒+フェンスの正規化" 0 "$rc"; then assert_stdout_equals "封筒+フェンス" "$EXPECT_APPROVED_JSON"; fi
PY_ENV_OUT="$(cat "$CASE_OUT")"

# 3. 不正 JSON
run_agent --runner stubrunner --command "bash $WORK/bin/stub-badjson.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log3.md"; rc=$?
check "失敗: 不正 JSON" 10 "$rc" && assert_error_format "不正 JSON"

# 4. 無応答(プローブ)
run_agent --runner stubrunner --command "bash $WORK/bin/stub-hang.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 2 --log-file "$WORK/log4.md"; rc=$?
check "失敗: 無応答(プローブタイムアウト)" 7 "$rc"

# 5. 疎通失敗
run_agent --runner stubrunner --command "bash $WORK/bin/stub-fail.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log5.md"; rc=$?
check "失敗: 疎通失敗" 6 "$rc"

# 6. 未検出
run_agent --runner stubrunner --command "no-such-cli-xyz --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --log-file "$WORK/log6.md"; rc=$?
check "失敗: 未検出" 3 "$rc"

# 7. 自ホスト拒否(CLAUDECODE=1・DEV_WORKFLOW_HOST_CLI は外す)
rc=0
guard env -u DEV_WORKFLOW_HOST_CLI CLAUDECODE=1 bash "$TARGET" --runner claude \
  --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --log-file "$WORK/log7.md" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
check "失敗: 自ホスト拒否" 4 "$rc"

# 8. 読み取り専用フラグが argv に無い
run_agent --runner stubrunner --command "bash $STUB_OK" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --log-file "$WORK/log8.md"; rc=$?
check "失敗: 読み取り専用フラグが argv に無い" 5 "$rc"

# 9. 未知ランナーで --readonly-flag 未指定
run_agent --runner stubrunner --command "bash $STUB_OK" --prompt-file "$WORK/prompt.md" --log-file "$WORK/log9.md"; rc=$?
check "失敗: 未知ランナーで読み取り専用フラグ未指定" 5 "$rc"

# 10-11. 信頼モデル: 既定表ランナーには --command / --readonly-flag を渡せない。
# どちらのケースも「拒否が退行したら実際に起動されて副作用マーカーが残る」形にしてある
# (PATH 先頭に副作用付きの cursor-agent スタブを置き、--command も副作用スタブを指す)
rm -f "$SIDEEFFECT"
rc=0
PATH="$WORK/evilbin:$PATH" guard bash "$TARGET" --runner cursor-agent \
  --command "bash $WORK/bin/stub-sideeffect.sh --mode ask {prompt}" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log10.md" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
check "信頼モデル: 既定表ランナーへの --command を拒否" 2 "$rc"
if [ -e "$SIDEEFFECT" ]; then ng "PoC 非再現(--command 経路で副作用が起きない)"; else ok "PoC 非再現(--command 経路で副作用が起きない)"; fi

rm -f "$SIDEEFFECT"
rc=0
PATH="$WORK/evilbin:$PATH" guard bash "$TARGET" --runner cursor-agent --readonly-flag "--readonly" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log11.md" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
check "信頼モデル: 既定表ランナーへの --readonly-flag を拒否" 2 "$rc"
if [ -e "$SIDEEFFECT" ]; then ng "PoC 非再現(--readonly-flag 経路で副作用が起きない)"; else ok "PoC 非再現(--readonly-flag 経路で副作用が起きない)"; fi

# 12. TERM を無視するスタブでもタイムアウトが成立し、プロセスを残さない(timeout -k 経路)
leak_cleanup; sleep 1
start=$(date +%s)
run_agent --runner stubrunner --command "bash $WORK/bin/hangproc.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 2 --log-file "$WORK/log12.md"; rc=$?
elapsed=$(( $(date +%s) - start ))
if [ "$rc" -eq 7 ] && [ "$elapsed" -lt 20 ]; then ok "TERM 無視スタブでもタイムアウト成立 (exit=$rc / ${elapsed}s)"; else
  ng "TERM 無視スタブでもタイムアウト成立 (期待 exit=7 かつ 20 秒未満 / 実際 exit=$rc ${elapsed}s)"; fi
sleep 2
if [ "$(leak_count)" -eq 0 ]; then ok "タイムアウト後にプロセスを残さない(timeout 経路)"; else
  ng "タイムアウト後にプロセスを残さない(timeout 経路・残存 $(leak_count) 件)"; leak_cleanup; fi

# 13-15. usage 系
run_agent --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 0 --log-file "$WORK/log13.md"; rc=$?
check "usage: --probe-timeout 0 を拒否" 2 "$rc"
run_agent --runner; rc=$?
check "usage: 値の無いオプション" 2 "$rc"
run_agent --runner "bad name" --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md"; rc=$?
check "usage: 不正なランナー名" 2 "$rc"
run_agent --runner cursor-agent --model "--force" --prompt-file "$WORK/prompt.md"; rc=$?
check "usage: '-' で始まるモデル名を拒否" 2 "$rc"

# 16. 空文字は「省略」として受理する(--model / --target)
run_agent --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --model "" --target "" --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log16.md"; rc=$?
check "空の --model / --target を省略として受理" 0 "$rc"

# 17. dry-run は起動しない
rm -f "$SIDEEFFECT"
run_agent --runner stubrunner --command "bash $WORK/bin/stub-sideeffect.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --dry-run --log-file "$WORK/log17.md"; rc=$?
check "dry-run: 静的検査のみ" 0 "$rc"
if [ -e "$SIDEEFFECT" ]; then ng "dry-run で起動していない"; else ok "dry-run で起動していない"; fi

# 18. プロンプト長超過
head -c 200000 /dev/zero | tr '\0' 'a' >"$WORK/big-prompt.md"
run_agent --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/big-prompt.md" --probe-timeout 10 --log-file "$WORK/log18.md"; rc=$?
check "失敗: プロンプト長超過" 12 "$rc"

# 18b. 大きさの判定の順序: プロンプトの大きさは、外部 CLI(ヘルプ照合・プローブ・本実行)を 1 回も起動する前に
#      判定する(--dry-run でも)。18 は終了コードしか見ていないので、起動の有無を見る
# (a) --command のランナー: 副作用のスタブを起動しない
rm -f "$SIDEEFFECT"
run_agent --runner stubrunner --command "bash $WORK/bin/stub-sideeffect.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/big-prompt.md" --probe-timeout 10 --log-file "$WORK/log18b.md"; rc=$?
check "大きさの判定の順序: --command のランナー: prompt-too-large" 12 "$rc"
if [ -e "$SIDEEFFECT" ]; then ng "大きさの判定の順序: --command のランナー: スタブを起動しない"; else
  ok "大きさの判定の順序: --command のランナー: スタブを起動しない"; fi
# (b) 既定表のランナー: 起動のたびに 1 行を記録するスタブで、起動が 0 回(ヘルプ照合もプローブも打たない)
mkdir -p "$WORK/sizebin"
cat >"$WORK/sizebin/cursor-agent" <<EOF
#!/usr/bin/env bash
echo called >>"\$SELFTEST_SIZE_CALLS"
exec bash "$WORK/pathbin/cursor-agent" "\$@"
EOF
chmod +x "$WORK/sizebin/cursor-agent"
SIZE_CALLS="$WORK/size-calls.txt"
rm -f "$SIZE_CALLS"
rc=0
SELFTEST_SIZE_CALLS="$SIZE_CALLS" PATH="$WORK/sizebin:$PATH" guard bash "$TARGET" --runner cursor-agent \
  --prompt-file "$WORK/big-prompt.md" --probe-timeout 10 --log-file "$WORK/log18c.md" --cwd "$WORK" \
  >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
check "大きさの判定の順序: 既定表のランナー: prompt-too-large" 12 "$rc"
size_calls="$(cat "$SIZE_CALLS" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$size_calls" -eq 0 ]; then ok "大きさの判定の順序: 既定表のランナー: 起動が 0 回"; else
  ng "大きさの判定の順序: 既定表のランナー: 起動が 0 回(実際 $size_calls 回)"; fi
# (c) --dry-run でも 12 で止まり、解決後のコマンドを出さない
run_agent --runner stubrunner --command "bash $WORK/bin/stub-sideeffect.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/big-prompt.md" --dry-run --log-file "$WORK/log18d.md"; rc=$?
check "大きさの判定の順序: --dry-run: prompt-too-large" 12 "$rc"
if [ ! -s "$CASE_OUT" ]; then ok "大きさの判定の順序: --dry-run: stdout は空"; else
  ng "大きさの判定の順序: --dry-run: stdout は空"; cat "$CASE_OUT" >&2; fi

# 19-20. 本実行だけ失敗 / 本実行だけハング
run_agent --runner stubrunner --command "bash $WORK/bin/stub-runfail.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --run-timeout 10 --log-file "$WORK/log19.md"; rc=$?
check "失敗: 本実行の失敗(run-failed)" 8 "$rc" && assert_error_format "run-failed"
run_agent --runner stubrunner --command "bash $WORK/bin/stub-runhang.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --run-timeout 2 --log-file "$WORK/log20.md"; rc=$?
check "失敗: 本実行のタイムアウト(run-timeout)" 9 "$rc"

# 21. 既定表ランナーの成功経路(PATH 先頭にランナー名のスタブを置く)
rc=0
PATH="$WORK/pathbin:$PATH" guard bash "$TARGET" --runner cursor-agent --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log21.md" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
if check "既定表ランナーの成功経路(ヘルプ照合 → 疎通 → 本実行)" 0 "$rc"; then
  assert_stdout_equals "既定表ランナー" "$EXPECT_ISSUE_JSON"
fi

# 22. 既定表(スクリプト)と仕様書 §4 の表が一致している
if [ -f "$DOC" ]; then
  for r in cursor-agent gemini codex; do
    doc_cmd="$(grep -E "^\| \`$r\` \|" "$DOC" | head -1 | awk -F'|' '{print $4}' | sed -e 's/^ *//' -e 's/ *$//' -e 's/^`//' -e 's/`$//')"
    [ -n "$doc_cmd" ] || { ng "既定表の同期: $r の行を仕様書から読めない"; continue; }
    expect="$(printf '%s' "$doc_cmd" | sed -E 's/ [^ ]+ \{model\}//')"
    case "$expect" in
      *'{prompt}'*) expect="$(printf '%s' "$expect" | sed -e 's/{prompt}/<プロンプト>/')" ;;
      *) expect="$expect <プロンプト>" ;;
    esac
    actual="$(PATH="$WORK/pathbin:$PATH" guard bash "$TARGET" --runner "$r" --prompt-file "$WORK/prompt.md" \
                --dry-run --log-file "$WORK/log22-$r.md" 2>/dev/null)"
    if [ "$actual" = "$expect" ]; then ok "既定表の同期: $r"; else
      ng "既定表の同期: $r(仕様書=\"$expect\" / 実装=\"$actual\")"; fi
  done
else
  ng "既定表の同期: 仕様書 $DOC が見つからない"
fi

# 23-26. jq 経路(python3 を PATH から外す)。python3 経路との一致も見る
jq_run() { # 残り=引数
  rc=0
  guard env -i HOME="$HOME" DEV_WORKFLOW_HOST_CLI="selftest-host" PATH="$WORK/sandbox-nopython" \
    bash "$TARGET" --cwd "$WORK" "$@" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  return "$rc"
}
jq_run --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log23.md"; rc=$?
if check "jq 経路: 成功" 0 "$rc"; then
  if [ "$(cat "$CASE_OUT")" = "$PY_OK_OUT" ]; then ok "jq 経路と python3 経路の出力が一致(成功)"; else
    ng "jq 経路と python3 経路の出力が一致(成功)"; echo "--- jq ---" >&2; cat "$CASE_OUT" >&2; echo "--- python3 ---" >&2; printf '%s\n' "$PY_OK_OUT" >&2; fi
fi

jq_run --runner stubrunner --command "bash $WORK/bin/stub-envelope.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log24.md"; rc=$?
if check "jq 経路: 封筒+フェンス" 0 "$rc"; then
  if [ "$(cat "$CASE_OUT")" = "$PY_ENV_OUT" ]; then ok "jq 経路と python3 経路の出力が一致(封筒+フェンス)"; else
    ng "jq 経路と python3 経路の出力が一致(封筒+フェンス)"; fi
fi

# issues が配列でない: jq の Cannot iterate over string を踏まず、空配列に落ちること
run_agent --runner stubrunner --command "bash $WORK/bin/stub-issues-notarray.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log25py.md"; rc_py=$?
py_out="$(cat "$CASE_OUT")"
jq_run --runner stubrunner --command "bash $WORK/bin/stub-issues-notarray.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log25jq.md"; rc_jq=$?
if [ "$rc_py" -eq 0 ] && [ "$rc_jq" -eq 0 ] && [ "$py_out" = "$EXPECT_APPROVED_JSON" ] && [ "$(cat "$CASE_OUT")" = "$EXPECT_APPROVED_JSON" ]; then
  ok "issues が配列でない出力を両経路とも空配列へ正規化"
else
  ng "issues が配列でない出力を両経路とも空配列へ正規化 (python3 exit=$rc_py / jq exit=$rc_jq)"
  echo "--- python3 ---" >&2; printf '%s\n' "$py_out" >&2; echo "--- jq ---" >&2; cat "$CASE_OUT" >&2
fi

# JSONL: 指摘 JSON らしい 1 個だけを拾い、verdict が 1 個であること
EXPECT_JSONL='{
  "verdict": "CHANGES_REQUESTED",
  "issues": [
    {
      "file": "a.ts",
      "line": 1,
      "category": "規約",
      "severity": "minor",
      "description": "d",
      "suggestion": "s"
    }
  ]
}'
run_agent --runner stubrunner --command "bash $WORK/bin/stub-jsonl.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log26py.md"; rc_py=$?
py_out="$(cat "$CASE_OUT")"
jq_run --runner stubrunner --command "bash $WORK/bin/stub-jsonl.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log26jq.md"; rc_jq=$?
if [ "$rc_py" -eq 0 ] && [ "$rc_jq" -eq 0 ] && [ "$py_out" = "$EXPECT_JSONL" ] && [ "$(cat "$CASE_OUT")" = "$EXPECT_JSONL" ]; then
  ok "JSONL 出力から指摘 JSON 1 個だけを両経路とも取り出す"
else
  ng "JSONL 出力から指摘 JSON 1 個だけを両経路とも取り出す (python3 exit=$rc_py / jq exit=$rc_jq)"
  echo "--- python3 ---" >&2; printf '%s\n' "$py_out" >&2; echo "--- jq ---" >&2; cat "$CASE_OUT" >&2
fi

# verdict が列挙値でない: 両経路とも issues の有無から導出し、元の値をログに残す
run_agent --runner stubrunner --command "bash $WORK/bin/stub-oddverdict.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log27py.md"; rc_py=$?
py_out="$(cat "$CASE_OUT")"
jq_run --runner stubrunner --command "bash $WORK/bin/stub-oddverdict.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log27jq.md"; rc_jq=$?
if [ "$rc_py" -eq 0 ] && [ "$rc_jq" -eq 0 ] && [ "$py_out" = "$EXPECT_APPROVED_JSON" ] && [ "$(cat "$CASE_OUT")" = "$EXPECT_APPROVED_JSON" ] \
   && grep -q '元の verdict' "$WORK/log27py.md" && grep -q '元の verdict' "$WORK/log27jq.md"; then
  ok "列挙値でない verdict を両経路とも導出し、元の値をログに残す"
else
  ng "列挙値でない verdict を両経路とも導出し、元の値をログに残す (python3 exit=$rc_py / jq exit=$rc_jq)"
fi

# 27. フォールバック経路(timeout / gtimeout が無い)でもタイムアウトが成立し、プロセスを残さない
leak_cleanup; sleep 1
start=$(date +%s)
rc=0
guard env -i HOME="$HOME" DEV_WORKFLOW_HOST_CLI="selftest-host" PATH="$WORK/sandbox-notimeout" \
  bash "$TARGET" --runner stubrunner --command "bash $WORK/bin/hangproc.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 3 --log-file "$WORK/log27fb.md" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
elapsed=$(( $(date +%s) - start ))
if [ "$rc" -eq 7 ] && [ "$elapsed" -lt 25 ]; then ok "フォールバック経路でもタイムアウト成立 (exit=$rc / ${elapsed}s)"; else
  ng "フォールバック経路でもタイムアウト成立 (期待 exit=7 かつ 25 秒未満 / 実際 exit=$rc ${elapsed}s)"; fi
sleep 2
if [ "$(leak_count)" -eq 0 ]; then ok "タイムアウト後にプロセスを残さない(フォールバック経路)"; else
  ng "タイムアウト後にプロセスを残さない(フォールバック経路・残存 $(leak_count) 件)"; leak_cleanup; fi

# ── 未踏分岐のケース(Task 8.3 ③)──

# 28. ラッパー(env)越しでも実効の実行ファイル名で自ホスト判定する
rm -f "$SIDEEFFECT"
rc=0
PATH="$WORK/pathbin:$PATH" guard env DEV_WORKFLOW_HOST_CLI="mytool" bash "$TARGET" --runner wrapped \
  --command "env FOO=1 mytool --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --log-file "$WORK/log28.md" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
check "自ホスト判定: env ラッパー越しでも実効の実行ファイルで判定" 4 "$rc"
if [ -e "$SIDEEFFECT" ]; then ng "自ホスト拒否で起動していない"; else ok "自ホスト拒否で起動していない"; fi

# 29. ラッパー越しの正常系(effective_bin が別ホストのときは素通りする)
run_agent --runner wrapped --command "env FOO=1 bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log29.md"; rc=$?
check "ラッパー越しの起動が成功する" 0 "$rc"

# 30. プローブが終了コード 0 でも出力が空なら疎通失敗
run_agent --runner stubrunner --command "bash $WORK/bin/stub-empty.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log30.md"; rc=$?
check "失敗: プローブの出力が空" 6 "$rc"

# 31. --target と --cwd がランナーへ正しく伝わる
export SELFTEST_RECORD_DIR="$WORK/record"; mkdir -p "$SELFTEST_RECORD_DIR" "$WORK/othercwd"
run_agent --runner stubrunner --command "bash $WORK/bin/stub-record.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --target "src/a.ts" --target "src/b.ts" --cwd "$WORK/othercwd" \
  --probe-timeout 10 --log-file "$WORK/log31.md"; rc=$?
if check "--target / --cwd の伝播" 0 "$rc"; then
  if grep -q "レビュー対象" "$SELFTEST_RECORD_DIR/lastarg.txt" && grep -q "src/a.ts" "$SELFTEST_RECORD_DIR/lastarg.txt" \
     && grep -q "src/b.ts" "$SELFTEST_RECORD_DIR/lastarg.txt"; then
    ok "--target がプロンプト末尾に付く"
  else
    ng "--target がプロンプト末尾に付く"; fi
  if [ "$(cat "$SELFTEST_RECORD_DIR/cwd.txt")" = "$WORK/othercwd" ]; then ok "--cwd がランナーの作業ディレクトリになる"; else
    ng "--cwd がランナーの作業ディレクトリになる(実際: $(cat "$SELFTEST_RECORD_DIR/cwd.txt"))"; fi
  if grep -q "src/a.ts" "$WORK/log31.md"; then ok "渡した対象がログに残る"; else ng "渡した対象がログに残る"; fi
fi

# 32. --model が既定コマンドの {model} に正しく入る
actual="$(PATH="$WORK/pathbin:$PATH" guard bash "$TARGET" --runner cursor-agent --model "some-model" \
            --prompt-file "$WORK/prompt.md" --dry-run --log-file "$WORK/log32.md" 2>/dev/null)"
case "$actual" in
  *"--model some-model"*) ok "--model が既定コマンドへ正しく挿入される" ;;
  *) ng "--model が既定コマンドへ正しく挿入される(実際: $actual)" ;;
esac

# 33. 読み取り専用フラグの `名前=値` 形も argv 照合で認める
run_agent --runner stubrunner --command "bash $STUB_OK --mode=ask" --readonly-flag "--mode ask" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log33.md"; rc=$?
check "読み取り専用フラグの --名前=値 形を認める" 0 "$rc"

# 34. 既定表ランナーの --help がハングしたら probe-timeout(no-readonly に化けない)
rc=0
PATH="$WORK/helphang:$PATH" guard bash "$TARGET" --runner cursor-agent --prompt-file "$WORK/prompt.md" \
  --log-file "$WORK/log34.md" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
check "失敗: ヘルプ照合のタイムアウト" 7 "$rc" && assert_error_format "ヘルプ照合タイムアウト"

# 35. 置き場を作れない: ログの置き場を作れないときは usage(2)で理由を返す(implement-agent-selftest.sh の
#     E4b・E4c と同じ形)。明示の --log-file の置き場
run_agent --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --log-file "/dev/null/nested/x.md"; rc=$?
if check "置き場を作れない: --log-file の置き場を作れないときは usage" 2 "$rc"; then
  if grep -qE '^ERROR \[usage\] ' "$CASE_ERR"; then ok "置き場を作れない: --log-file の置き場: stderr に ERROR [usage] が出る"; else
    ng "置き場を作れない: --log-file の置き場: stderr に ERROR [usage] が出る"; cat "$CASE_ERR" >&2; fi
  if grep -qF -- '--log-file の置き場' "$CASE_ERR"; then ok "置き場を作れない: --log-file の置き場: 理由の分かる文になっている"; else
    ng "置き場を作れない: --log-file の置き場: 理由の分かる文になっている"; cat "$CASE_ERR" >&2; fi
fi
#     既定の置き場(書けない cwd の .claude/reviews)。root では作れてしまうので飛ばす
if [ "$(id -u)" -ne 0 ]; then
  ROCWD="$WORK/ro-cwd"
  rm -rf "$ROCWD"; mkdir -p "$ROCWD"; chmod 555 "$ROCWD"
  rc=0
  ( cd "$ROCWD" && guard bash "$TARGET" --runner stubrunner --command "bash $STUB_OK --readonly-x" \
    --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" --cwd "$WORK" \
    --probe-timeout 10 --run-timeout 20 ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  chmod 755 "$ROCWD"
  if check "置き場を作れない: 既定の置き場を作れないときは usage" 2 "$rc"; then
    if grep -qE '^ERROR \[usage\] ' "$CASE_ERR"; then ok "置き場を作れない: 既定の置き場: stderr に ERROR [usage] が出る"; else
      ng "置き場を作れない: 既定の置き場: stderr に ERROR [usage] が出る"; cat "$CASE_ERR" >&2; fi
    if grep -qF -- '既定のログ置き場' "$CASE_ERR"; then ok "置き場を作れない: 既定の置き場: 理由の分かる文になっている"; else
      ng "置き場を作れない: 既定の置き場: 理由の分かる文になっている"; cat "$CASE_ERR" >&2; fi
  fi
else
  ok "置き場を作れない: 既定の置き場を作れないときは usage(root のため飛ばす)"
fi

# 35a. 開く前の ERR: ログを開く前の想定外の失敗は ERR trap が拾い、exit 20 で ERROR [internal] を 1 行だけ出し、
#      ログを作らない(黙って落ちない)。`dirname` だけを失敗させるスタブで、明示の分岐の
#      LOG_DIR="$(dirname …)" を失敗させる。空振りを防ぐため、ERROR [internal] の行の「行 N」が
#      対象スクリプトのその行の行番号と一致することを見る
OE_LINE="$(grep -nF 'LOG_DIR="$(dirname "$LOG_FILE")"' "$TARGET" | head -1 | cut -d: -f1)"
OE_BIN="$WORK/dirnamebin"
mkdir -p "$OE_BIN"
cat >"$OE_BIN/dirname" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$OE_BIN/dirname"
OE_LOG="$WORK/log-openerr.md"
rm -f "$OE_LOG"
rc=0
PATH="$OE_BIN:$PATH" guard bash "$TARGET" --runner stubrunner --command "bash $STUB_OK --readonly-x" \
  --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" --cwd "$WORK" --log-file "$OE_LOG" \
  >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
oe_got="$(grep -E '^ERROR \[internal\] ' "$CASE_ERR" | head -1 | sed -n 's/.*行 \([0-9][0-9]*\)).*/\1/p')"
if [ -n "$OE_LINE" ] && [ "$rc" -eq 20 ] && [ "$oe_got" = "$OE_LINE" ]; then
  ok "開く前の ERR: 想定外の失敗は exit 20(置き場の dirname の行) (exit=$rc)"
else
  ng "開く前の ERR: 想定外の失敗は exit 20(置き場の dirname の行)(実際 exit=$rc・行=${oe_got:-無し}(期待 exit=20・行 ${OE_LINE:-対象の行が見つからない}))"
  cat "$CASE_ERR" >&2
fi
oe_n="$(grep -cE '^ERROR \[internal\] ' "$CASE_ERR")"
if [ "$oe_n" -eq 1 ]; then ok "開く前の ERR: ERROR [internal] の行が 1 行"; else
  ng "開く前の ERR: ERROR [internal] の行が 1 行(実際 $oe_n 行)"; cat "$CASE_ERR" >&2; fi
if [ -e "$OE_LOG" ] || [ -L "$OE_LOG" ]; then ng "開く前の ERR: ログを作らない"; else ok "開く前の ERR: ログを作らない"; fi

# 35b(#100 D3)。ERR の出力先: 本実行の呼び出し(`run_timeout … || rc=$?`)から ` || rc=$?` を
# 外した写しに、`date +%s` だけを失敗させるスタブを当てる。本実行の stderr を一時ファイルへ
# 向けている間に ERR trap が発火しても、ERROR [internal] が元の stderr(fd 8)に残ることを見る。
# sed の区切りは # にする(対象行に | が含まれるため)。$ [ ] はメタ文字なのでエスケープする
# (${CMD[@]} の [ ] をそのまま埋めると GNU sed の BRE では黙って 0 件の置換になる)。
# `-i` は GNU sed 限定(BSD sed では失敗する)なので使わず、リダイレクトで写しを作る。
D3_TARGET="$WORK/d3-target.sh"
sed 's#run_timeout "\$RUN_TIMEOUT" "\${CMD\[@\]}" </dev/null >"\$RAW_OUT" 2>"\$RAW_ERR" || rc=\$?#run_timeout "$RUN_TIMEOUT" "${CMD[@]}" </dev/null >"$RAW_OUT" 2>"$RAW_ERR"#' "$TARGET" >"$D3_TARGET"
D3_OK=1; D3_MSG=""
# 空振りの治具で PASS しないように、外した後の行が 1・元の行が 0 であることを先に確かめる
if [ "$(grep -cxF 'run_timeout "$RUN_TIMEOUT" "${CMD[@]}" </dev/null >"$RAW_OUT" 2>"$RAW_ERR"' "$D3_TARGET")" -ne 1 ] \
  || [ "$(grep -cxF 'run_timeout "$RUN_TIMEOUT" "${CMD[@]}" </dev/null >"$RAW_OUT" 2>"$RAW_ERR" || rc=$?' "$D3_TARGET")" -ne 0 ]; then
  D3_OK=0; D3_MSG="写しの治具(本実行の呼び出しから || rc=\$? を外す)が当たらない"
fi
D3_LINE=""
if [ "$D3_OK" -eq 1 ]; then
  D3_LINE="$(grep -nF 'rt_start="$(date +%s)"' "$D3_TARGET" | head -1 | cut -d: -f1)"
  if [ -z "$D3_LINE" ]; then D3_OK=0; D3_MSG="写しに rt_start の行が見つからない"; fi
fi
rc=-1; D3_GOTLINE=""
if [ "$D3_OK" -eq 1 ]; then
  D3_DATESTUB="$WORK/d3-datestub"; mkdir -p "$D3_DATESTUB"
  D3_REALDATE="$(command -v date)"
  cat >"$D3_DATESTUB/date" <<EOF
#!/usr/bin/env bash
if [ "\$#" -eq 1 ] && [ "\$1" = "+%s" ]; then exit 1; fi
exec "$D3_REALDATE" "\$@"
EOF
  chmod +x "$D3_DATESTUB/date"
  rc=0
  PATH="$D3_DATESTUB:$PATH" guard bash "$D3_TARGET" --runner stubrunner \
    --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 \
    --log-file "$WORK/log35b.md" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  D3_ERRLINE="$(grep -E '^ERROR \[internal\] ' "$CASE_ERR" | head -1)"
  D3_GOTLINE="$(printf '%s' "$D3_ERRLINE" | sed -n 's/.*行 \([0-9][0-9]*\)).*/\1/p')"
  if [ "$rc" -ne 20 ] || [ -z "$D3_ERRLINE" ] || [ "$D3_GOTLINE" != "$D3_LINE" ]; then
    D3_OK=0; D3_MSG="実際 exit=$rc・行=${D3_GOTLINE:-無し}(期待 exit=20・行 $D3_LINE)"
  fi
fi
if [ "$D3_OK" -eq 1 ]; then
  ok "ERR の出力先: 本実行の stderr を一時ファイルへ向けている間に想定外の失敗が起きても ERROR [internal] が元の stderr に残る (exit=$rc)"
else
  ng "ERR の出力先: 本実行の stderr を一時ファイルへ向けている間に想定外の失敗が起きても ERROR [internal] が元の stderr に残る($D3_MSG)"
  cat "$CASE_ERR" >&2 2>/dev/null || true
fi

# 36. 先頭が封筒キーを持たない JSONL でも両経路が同じ結果になる
run_agent --runner stubrunner --command "bash $WORK/bin/stub-jsonl-envelope.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log36py.md"; rc_py=$?
py_out="$(cat "$CASE_OUT")"
jq_run --runner stubrunner --command "bash $WORK/bin/stub-jsonl-envelope.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --probe-timeout 10 --log-file "$WORK/log36jq.md"; rc_jq=$?
if [ "$rc_py" -eq 0 ] && [ "$rc_jq" -eq 0 ] && [ "$py_out" = "$EXPECT_APPROVED_JSON" ] && [ "$(cat "$CASE_OUT")" = "$EXPECT_APPROVED_JSON" ]; then
  ok "封筒が先頭でない JSONL を両経路とも同じに正規化"
else
  ng "封筒が先頭でない JSONL を両経路とも同じに正規化 (python3 exit=$rc_py / jq exit=$rc_jq)"
  echo "--- python3 ---" >&2; printf '%s\n' "$py_out" >&2; echo "--- jq ---" >&2; cat "$CASE_OUT" >&2
fi

# 37. サブコマンド側の --help でしか読み取り専用フラグを出さないランナーも通す
rc=0
PATH="$WORK/subhelp:$PATH" guard bash "$TARGET" --runner codex --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log37.md" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
if check "既定表ランナー: <bin> <sub> --help でのヘルプ照合" 0 "$rc"; then
  assert_stdout_equals "サブコマンドのヘルプ照合" "$EXPECT_ISSUE_JSON"
fi

# 38. 正規化結果の形が壊れていたら parse-failed で止まる(norm_shape_ok の実効性)
BROKEN="$WORK/broken-jqmap.sh"
sed 's/^    issues: \$norm }$/    issues: "not-an-array" }/' "$TARGET" >"$BROKEN"
if ! grep -q '"not-an-array"' "$BROKEN"; then
  ng "norm_shape_ok の検証: 壊した実装を作れない(JQ_MAP の形が変わった)"
else
  rc=0
  guard env -i HOME="$HOME" DEV_WORKFLOW_HOST_CLI="selftest-host" PATH="$WORK/sandbox-nopython" \
    bash "$BROKEN" --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --log-file "$WORK/log38.md" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  if [ "$rc" -eq 10 ]; then ok "壊れた正規化結果を parse-failed で止める (exit=$rc)"; else
    ng "壊れた正規化結果を parse-failed で止める (期待 exit=10 / 実際 exit=$rc)"; cat "$CASE_OUT" "$CASE_ERR" >&2; fi
fi

# 39. 既定コマンドの --trust でワークスペース信頼を要求するランナーが通る
rc=0
PATH="$WORK/trustbin:$PATH" guard bash "$TARGET" --runner cursor-agent --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --log-file "$WORK/log39.md" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
if check "既定コマンドの --trust でワークスペース信頼を通過する" 0 "$rc"; then
  assert_stdout_equals "--trust による成功経路" "$EXPECT_ISSUE_JSON"
fi

# 40. --trust を落とすと同じランナーが probe-failed で止まる(2026-09-09 決定 16 の再現)。
# 既定表のランナーには --command を渡せない(usage エラー)ため、既定表外のランナー名で再現する
rc=0
run_agent --runner stubrunner --command "bash $WORK/trustbin/cursor-agent --mode ask" \
  --readonly-flag "--mode ask" --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --log-file "$WORK/log40.md" || rc=$?
check "失敗: --trust 無しはワークスペース信頼で probe-failed" 6 "$rc"
# 因果の固定: 単に非ゼロ終了したのではなく、信頼要求で落ちたことをログで確認する
if grep -q 'Workspace Trust Required' "$WORK/log40.md"; then ok "--trust 無しの失敗理由がログに残る"; else
  ng "--trust 無しの失敗理由がログに残る"; cat "$WORK/log40.md" >&2; fi

# 41. 既定コマンドから --trust / --mode ask が消える退行を拾う
actual="$(PATH="$WORK/pathbin:$PATH" guard bash "$TARGET" --runner cursor-agent \
            --prompt-file "$WORK/prompt.md" --dry-run --log-file "$WORK/log41.md" 2>/dev/null)"
case "$actual" in
  *"--mode ask"*)
    case "$actual" in
      *"--trust"*) ok "既定コマンドが --mode ask と --trust を保持する" ;;
      *) ng "既定コマンドが --trust を保持する(実際: $actual)" ;;
    esac ;;
  *) ng "既定コマンドが --mode ask を保持する(実際: $actual)" ;;
esac

# ── プロンプト組み立て: スキーマ指示の付与と冪等(external-runners.md §6・§11)──
# 期待値は原本の文面退行も拾うため literal でハードコードする($SCHEMA_FILE から導出しない)
GATE_CMD="bash $WORK/bin/stub-schema-gate.sh --readonly-x"
rec_reset() { rm -f "$SELFTEST_RECORD_DIR/cwd.txt" "$SELFTEST_RECORD_DIR/lastarg.txt"; }
LASTARG="$SELFTEST_RECORD_DIR/lastarg.txt"

# 42. スキーマ指示の付与: 窓幅より短い通常の依頼文(クランプ分岐)でも末尾にブロックが 1 回付く
rec_reset
run_agent --runner stubrunner --command "$GATE_CMD" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --cwd "$WORK/othercwd" --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log42.md"; rc=$?
if check "スキーマ指示の付与: ブロック付きの本実行だけ JSON を返すゲートスタブが成功する" 0 "$rc"; then
  if grep -qF -- '## 出力形式(review-agent.sh が付与)' "$LASTARG"; then ok "スキーマ指示: 見出しがプロンプトに含まれる"; else
    ng "スキーマ指示: 見出しがプロンプトに含まれる"; fi
  if grep -qF -- '"verdict"' "$LASTARG" && grep -qF -- '"issues"' "$LASTARG"; then ok "スキーマ指示: \"verdict\" と \"issues\" が含まれる"; else
    ng "スキーマ指示: \"verdict\" と \"issues\" が含まれる"; fi
  if grep -qF -- '"suggestion"' "$LASTARG" && grep -qF -- 'blocker' "$LASTARG"; then ok "スキーマ指示: \"suggestion\" と severity の列挙値 blocker が含まれる"; else
    ng "スキーマ指示: \"suggestion\" と severity の列挙値 blocker が含まれる"; fi
  if grep -qF -- 'JSON だけを返す' "$LASTARG" && grep -qF -- '{"verdict":"APPROVED","issues":[]}' "$LASTARG"; then
    ok "スキーマ指示: 根因に直結する 2 文(JSON だけを返す / 指摘なしの形)が含まれる"
  else
    ng "スキーマ指示: 根因に直結する 2 文(JSON だけを返す / 指摘なしの形)が含まれる"; fi
  if grep -q '^- スキーマ指示: 付与$' "$WORK/log42.md"; then ok "スキーマ指示: 付与の別がログに残る(そのまま付与)"; else
    ng "スキーマ指示: 付与の別がログに残る(そのまま付与)"; cat "$WORK/log42.md" >&2; fi
fi

# 43. 冪等: 付与ブロック全体で終わる入力は除去してから付け直す(見出しは 1 回)
printf '%s\n\n%s\n' "レビューしてください" "$SCHEMA_BLOCK_TEXT" >"$WORK/prompt-with-block.md"
rec_reset
run_agent --runner stubrunner --command "$GATE_CMD" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt-with-block.md" --cwd "$WORK/othercwd" --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log43.md"; rc=$?
if check "スキーマ指示の冪等: ブロック付き入力でも成功する" 0 "$rc"; then
  n="$(schema_head_count "$LASTARG")"
  if [ "$n" -eq 1 ]; then ok "スキーマ指示の冪等: 見出しの出現が 1 回"; else ng "スキーマ指示の冪等: 見出しの出現が 1 回(実際 $n 回)"; fi
  if grep -q '^- スキーマ指示: 入力末尾の既存ブロックを除去して付与$' "$WORK/log43.md"; then ok "スキーマ指示の冪等: 除去して付与した旨がログに残る"; else
    ng "スキーマ指示の冪等: 除去して付与した旨がログに残る"; cat "$WORK/log43.md" >&2; fi
fi

# 44. ブロック付き入力 + --target + 末尾空白(スペース 2 + タブ 1 + 改行。$(cat) は改行しか落とさない)
#     → 対象一覧がブロックより前に 1 回だけ置かれる(除去 → --target 付記 → 最後に 1 回連結)
printf '%s\n\n%s  \t\n' "レビューしてください" "$SCHEMA_BLOCK_TEXT" >"$WORK/prompt-block-ws.md"
rec_reset
run_agent --runner stubrunner --command "$GATE_CMD" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt-block-ws.md" --target src/c.ts --cwd "$WORK/othercwd" --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log44.md"; rc=$?
if check "スキーマ指示 + --target + 末尾空白: 成功する" 0 "$rc"; then
  n="$(schema_head_count "$LASTARG")"
  if [ "$n" -eq 1 ]; then ok "スキーマ指示 + --target + 末尾空白: 見出しの出現が 1 回"; else
    ng "スキーマ指示 + --target + 末尾空白: 見出しの出現が 1 回(実際 $n 回)"; fi
  head_ln="$(grep -nF -- "$SCHEMA_HEAD" "$LASTARG" | cut -d: -f1 | head -1)"
  tgt_max="$(grep -nF -- 'src/c.ts' "$LASTARG" | cut -d: -f1 | sort -n | tail -1)"
  if [ -n "$head_ln" ] && [ -n "$tgt_max" ] && [ "$tgt_max" -lt "$head_ln" ]; then
    ok "スキーマ指示 + --target: 対象一覧(行 $tgt_max)が見出し(行 $head_ln)より前にある"
  else
    ng "スキーマ指示 + --target: 対象一覧が見出しより前にある(対象の最終行=${tgt_max:-なし} / 見出し行=${head_ln:-なし})"; fi
fi

# 45. 長い末尾空白(60,000 文字)でも停滞しない: 判定は末尾の窓(ブロック長 + 4 KiB)だけを見る。
#     ①〜③ の 3 件を固定順で評価する(① は独立に先に評価し、停滞はそれ自体が FAIL 行として出る)。
#     停滞の判定はケース専用の外側タイムアウト 20 秒(全文を 1 文字ずつトリムする実装は二乗で約 65 秒かかる)。
#     窓を超える空白は除去対象外なので見出しの出現回数は検証しない
{ printf '%s\n\n%s' "レビューしてください" "$SCHEMA_BLOCK_TEXT"; head -c 60000 /dev/zero | tr '\0' ' '; printf '\n'; } >"$WORK/prompt-long-ws.md"
rec_reset
if [ -n "$OUTER_TIMEOUT_BIN" ]; then
  rc=0
  "$OUTER_TIMEOUT_BIN" -k 5 20 bash "$TARGET" --runner stubrunner --command "$GATE_CMD" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt-long-ws.md" --cwd "$WORK/othercwd" --probe-timeout 10 --run-timeout 20 \
    --log-file "$WORK/log45.md" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  if [ "$rc" -ne 124 ]; then ok "長い末尾空白: 20 秒以内に完了する (exit=$rc)"; else
    ng "長い末尾空白: 20 秒以内に完了する(外側タイムアウト 20 秒で停滞 exit=124)"; fi
  if check "長い末尾空白: 成功する" 0 "$rc"; then
    case "$(cat "$LASTARG")" in
      *"$SCHEMA_BLOCK_TEXT") ok "長い末尾空白: プロンプトが付与ブロックで終わる" ;;
      *) ng "長い末尾空白: プロンプトが付与ブロックで終わる"; tail -c 300 "$LASTARG" >&2; echo >&2 ;;
    esac
  fi
else
  ok "長い末尾空白: 20 秒以内に完了する(timeout が無いため飛ばす)"
  ok "長い末尾空白: 成功する(timeout が無いため飛ばす)"
  ok "長い末尾空白: プロンプトが付与ブロックで終わる(timeout が無いため飛ばす)"
fi

# 46. 付与後に上限を超える境界: 本文が MAX_PROMPT_BYTES − 100 バイト(付与前は上限内)→ prompt-too-large。
#     付与ブロックのバイト数が上限判定に含まれる契約の裏付け
max_bytes="$(sed -n 's/^MAX_PROMPT_BYTES=\([0-9][0-9]*\).*/\1/p' "$TARGET" | head -1)"
if [ -z "$max_bytes" ]; then
  ng "付与後に上限を超える境界: $TARGET から MAX_PROMPT_BYTES を読めない"
else
  head -c "$((max_bytes - 100))" /dev/zero | tr '\0' 'a' >"$WORK/prompt-near-limit.md"
  run_agent --runner stubrunner --command "$GATE_CMD" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt-near-limit.md" --cwd "$WORK/othercwd" --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log46.md"; rc=$?
  check "付与後に上限を超える境界: prompt-too-large" 12 "$rc"
fi

# 47. 本文中の引用では省略しない: 見出し文字列を含む行が本文に 1 行あり、末尾はブロックで終わらない
#     (契約や diff の引用を模す)→ 付与され、見出しは 2 回(引用 + 付与)
printf '%s\n%s\n' "レビューしてください" "本文中の引用: 契約には ## 出力形式(review-agent.sh が付与) という見出しがある" >"$WORK/prompt-quoted-head.md"
rec_reset
run_agent --runner stubrunner --command "$GATE_CMD" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt-quoted-head.md" --cwd "$WORK/othercwd" --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log47.md"; rc=$?
if check "本文中の引用では省略しない: 成功する" 0 "$rc"; then
  n="$(schema_head_count "$LASTARG")"
  case "$(cat "$LASTARG")" in
    *"$SCHEMA_BLOCK_TEXT")
      if [ "$n" -eq 2 ]; then ok "本文中の引用では省略しない: 末尾にブロックがあり見出しは 2 回(引用 + 付与)"; else
        ng "本文中の引用では省略しない: 見出しの出現が 2 回(実際 $n 回)"; fi ;;
    *) ng "本文中の引用では省略しない: プロンプトが付与ブロックで終わる" ;;
  esac
fi

# 48. スキーマの同期検査: review-protocol.md「指摘 JSON 形式」節と SCHEMA_BLOCK の 4 集合が一致する
#     (包含ではなく一致。ブロック側だけに値が増えた退行も拾う)。抽出は固定書式に依存する
#     (「verdict は A / B のいずれか」「severity は A / B / C のいずれか」「category は「…」「…」の 6 つのいずれか」)。
#     書式を変えるときは契約側・ブロック側・この検査の 3 点を同時に直す
PROTO="$SCRIPT_DIR/../references/review-protocol.md"
json_issue_keys() { # $1=JSON ファイル → issues[0] のキーを 1 行 1 個で
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print("\n".join(d["issues"][0].keys()))' "$1" 2>/dev/null
  elif command -v jq >/dev/null 2>&1; then
    jq -r '.issues[0] | keys_unsorted[]' "$1" 2>/dev/null
  fi
}
enum_values() { # $1=制約文 $2=verdict|severity|category → 許容値を 1 行 1 個で
  case "$2" in
    verdict)  printf '%s\n' "$1" | sed -n 's/.*verdict は \(.*\) のいずれか。severity は .*/\1/p' | sed 's| / |\
|g' ;;
    severity) printf '%s\n' "$1" | sed -n 's/.*severity は \(.*\) のいずれか。category は.*/\1/p' | sed 's| / |\
|g' ;;
    category) printf '%s\n' "$1" | sed -n 's/.*category は\(「.*」\)の 6 つのいずれか.*/\1/p' | sed 's/」「/」\
「/g' | sed 's/^「//; s/」$//' ;;
  esac
}
sorted() { LC_ALL=C sort; }
if [ -f "$PROTO" ]; then
  sed -n '/^## 指摘 JSON 形式$/,/^## /p' "$PROTO" >"$WORK/proto-section.txt"
  sed -n '/^```json$/,/^```$/p' "$WORK/proto-section.txt" | sed '1d;$d' >"$WORK/proto-schema.json"
  proto_constraint="$(grep '^制約: ' "$WORK/proto-section.txt" | head -1)"
  sed -n '3p' "$SCHEMA_FILE" >"$WORK/block-schema.json"
  block_constraint="$(sed -n '4p' "$SCHEMA_FILE")"
  # (i) 6 キー
  pk="$(json_issue_keys "$WORK/proto-schema.json" | sorted)"; bk="$(json_issue_keys "$WORK/block-schema.json" | sorted)"
  if [ -n "$pk" ] && [ -n "$bk" ] && [ "$pk" = "$bk" ]; then ok "スキーマの同期: issues[0] のキー集合が一致($(printf '%s\n' "$bk" | wc -l | tr -d ' ') 個)"; else
    ng "スキーマの同期: issues[0] のキー集合が一致(契約=$(printf '%s' "$pk" | tr '\n' ' ') / ブロック=$(printf '%s' "$bk" | tr '\n' ' '))"; fi
  # (ii)〜(iv) 列挙値
  for kind in verdict severity category; do
    pv="$(enum_values "$proto_constraint" "$kind" | sorted)"; bv="$(enum_values "$block_constraint" "$kind" | sorted)"
    if [ -n "$pv" ] && [ -n "$bv" ] && [ "$pv" = "$bv" ]; then ok "スキーマの同期: $kind の許容値が集合一致($(printf '%s\n' "$bv" | wc -l | tr -d ' ') 個)"; else
      ng "スキーマの同期: $kind の許容値が集合一致(契約=$(printf '%s' "$pv" | tr '\n' ' ') / ブロック=$(printf '%s' "$bv" | tr '\n' ' '))"; fi
  done
  # (v) 観点見出し 6 語と制約行の category 6 語
  hv="$(sed -n '/^## レビュー観点/,/^## /p' "$PROTO" | sed -n 's/^[0-9]\. \*\*\(.*\)\*\*:.*/\1/p' | sorted)"
  pv="$(enum_values "$proto_constraint" category | sorted)"
  if [ -n "$hv" ] && [ -n "$pv" ] && [ "$hv" = "$pv" ]; then ok "スキーマの同期: 観点見出しと制約行の category が集合一致"; else
    ng "スキーマの同期: 観点見出しと制約行の category が集合一致(見出し=$(printf '%s' "$hv" | tr '\n' ' ') / 制約=$(printf '%s' "$pv" | tr '\n' ' '))"; fi
else
  ng "スキーマの同期: 契約 $PROTO が見つからない"
fi

# 既定のログ置き場が**書き込み不可**のとき、番号の衝突と区別して止める。
# noclobber の採番は「作成に失敗したら次の番号へ」なので、権限不足まで再試行の対象に
# すると**無限ループ**する(実測: 外側の timeout で exit=124 になる)。
# スタブのランナー(stubrunner)で打つ(--runner codex だと、制限しない PATH の実機の codex まで
# 進みうるため)。chmod 555 は root には効かないので、E4c(implement-agent-selftest.sh)と同じく
# root では飛ばす旨を ok で出す。
if [ "$(id -u)" -ne 0 ]; then
  ROLOG="$WORK/rolog"
  rm -rf "$ROLOG"; mkdir -p "$ROLOG/.claude/reviews"
  : > "$ROLOG/.claude/reviews/reviewer-stubrunner-iter1.md"
  chmod 555 "$ROLOG/.claude/reviews"
  rc=0
  ( cd "$ROLOG" && guard bash "$TARGET" --runner stubrunner --command "bash $STUB_OK --readonly-x" \
    --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" --cwd "$WORK" \
    --probe-timeout 10 --run-timeout 20 ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  chmod 755 "$ROLOG/.claude/reviews" 2>/dev/null
  if [ "$rc" -eq 2 ] || [ "$rc" -eq 20 ]; then
    ok "ログ置き場に書けない: 番号の衝突と区別して止まる (exit=$rc)"
  else
    ng "ログ置き場に書けない: 番号の衝突と区別して止まる(実際 exit=$rc。124 なら無限ループ)"
    cat "$CASE_ERR" >&2
  fi
  if grep -qE '^ERROR \[[a-z-]+\] ' "$CASE_ERR"; then
    ok "ログ置き場に書けない: stderr が ERROR [理由コード] 形式"
  else
    ng "ログ置き場に書けない: stderr が ERROR [理由コード] 形式"; cat "$CASE_ERR" >&2
  fi
else
  ok "ログ置き場に書けない: 番号の衝突と区別して止まる(root のため飛ばす)"
fi

# 採番(リンク先の無い symlink):(#100 D4)。`-e` は偽になるが、その名前のエントリが在るので
# 番号の衝突として次の番号へ進む(「置き場に書き込めない」と誤って止まらない)。
DANGLOG="$WORK/danglog"
rm -rf "$DANGLOG"; mkdir -p "$DANGLOG/.claude/reviews"
ln -s "./does-not-exist" "$DANGLOG/.claude/reviews/reviewer-stubrunner-iter1.md"
rc=0
( cd "$DANGLOG" && guard bash "$TARGET" --runner stubrunner --command "bash $STUB_OK --readonly-x" \
  --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" --cwd "$WORK" \
  --probe-timeout 10 --run-timeout 20 ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
if [ "$rc" -eq 0 ] && [ -f "$DANGLOG/.claude/reviews/reviewer-stubrunner-iter2.md" ] \
  && [ -L "$DANGLOG/.claude/reviews/reviewer-stubrunner-iter1.md" ] \
  && [ ! -e "$DANGLOG/.claude/reviews/reviewer-stubrunner-iter1.md" ]; then
  ok "採番(リンク先の無い symlink): 番号の衝突として次の番号へ進む (exit=$rc)"
else
  ng "採番(リンク先の無い symlink): 番号の衝突として次の番号へ進む(実際 exit=$rc)"
  cat "$CASE_ERR" >&2
fi

# `-` で始まるプロンプト(箇条書き・frontmatter)がオプションと誤認されない。
# スタブは codex と同じく、`--` より前にある未知の `-` 始まりの引数を拒否する —— 最後の引数を
# 記録するだけのスタブでは、`--` を挟まない実装でも同じ最後の引数が届いて判別できない。
cat >"$WORK/bin/stub-strictopt.sh" <<'EOF'
#!/usr/bin/env bash
seen_dd=0
for a in "$@"; do
  [ "$seen_dd" -eq 1 ] && continue
  case "$a" in
    --) seen_dd=1 ;;
    --readonly-x) : ;;
    -*) echo "error: unexpected argument '$a' found" >&2; exit 2 ;;
  esac
done
printf '%s' "${@: -2:1}" >"$SELFTEST_RECORD_DIR/penult.txt"
printf '%s' "${@: -1}" >"$SELFTEST_RECORD_DIR/lastarg.txt"
echo '{"verdict":"APPROVED","issues":[]}'
EOF
chmod +x "$WORK/bin/stub-strictopt.sh"
printf -- '- 箇条書きで始まるレビュー依頼\n- 2 行目\n' >"$WORK/prompt-dash.md"
rec_reset; rm -f "$SELFTEST_RECORD_DIR/penult.txt"
run_agent --runner stubrunner --command "bash $WORK/bin/stub-strictopt.sh --readonly-x" \
  --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt-dash.md" \
  --probe-timeout 10 --log-file "$WORK/log-dash.md"; rc=$?
if check "- 始まりのプロンプト: 拒否されずに通る" 0 "$rc"; then
  if [ "$(cat "$SELFTEST_RECORD_DIR/penult.txt" 2>/dev/null)" = "--" ]; then
    ok "- 始まりのプロンプト: 直前に -- が挟まる"
  else
    ng "- 始まりのプロンプト: 直前に -- が挟まる(実際: $(cat "$SELFTEST_RECORD_DIR/penult.txt" 2>/dev/null))"
  fi
  if grep -q '箇条書きで始まるレビュー依頼' "$SELFTEST_RECORD_DIR/lastarg.txt"; then
    ok "- 始まりのプロンプト: 内容が欠けずに最後の引数として届く"
  else
    ng "- 始まりのプロンプト: 内容が欠けずに最後の引数として届く"
  fi
fi
# `-` で始まらないプロンプトには `--` を挟まない(必要なときだけ足す)
rec_reset; rm -f "$SELFTEST_RECORD_DIR/penult.txt"
run_agent --runner stubrunner --command "bash $WORK/bin/stub-strictopt.sh --readonly-x" \
  --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --log-file "$WORK/log-nodash.md"; rc=$?
if check "通常のプロンプト: 通る" 0 "$rc"; then
  if [ "$(cat "$SELFTEST_RECORD_DIR/penult.txt" 2>/dev/null)" != "--" ]; then
    ok "通常のプロンプト: -- を挟まない"
  else
    ng "通常のプロンプト: -- を挟まない"
  fi
fi

# Gemini の prompt 値は `--prompt=<本文>` / `-p=<本文>` の等号形と、安全な分離形を受け付ける。
# 未知 option・重複 prompt・余分な位置引数を拒否し、プローブと本実行の両方で prompt の全文を
# 記録する。これで `-p {prompt}` の分離形へ戻す変異は、`-` 始まりの本実行本文で拒否される。プローブは固定
# ping、本実行はスキーマ付与後の本文全体として、それぞれ 1 argv として照合する。
cat >"$WORK/pathbin/gemini" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "--help" ]; then
    echo '  -p, --prompt <string>'
    echo '  --approval-mode <mode> (plan)'
    exit 0
  fi
done
prompt_count=0
model=""
while [ $# -gt 0 ]; do
  case "$1" in
    --approval-mode) [ "${2:-}" = plan ] || { echo 'bad approval mode' >&2; exit 2; }; shift 2 ;;
    -m) model="${2:-}"; [ -n "$model" ] || { echo 'missing model' >&2; exit 2; }; shift 2 ;;
    -o) [ "${2:-}" = json ] || { echo 'bad output mode' >&2; exit 2; }; shift 2 ;;
    --readonly-x) shift ;;
    --prompt=*) prompt="${1#--prompt=}"; prompt_count=$((prompt_count + 1)); shift ;;
    -p=*) prompt="${1#-p=}"; prompt_count=$((prompt_count + 1)); shift ;;
    --prompt|-p)
      [ $# -ge 2 ] || { echo "error: missing prompt after '$1'" >&2; exit 2; }
      case "$2" in -*) echo "error: option '$2' cannot be a separated prompt value" >&2; exit 2 ;; esac
      prompt="$2"; prompt_count=$((prompt_count + 1)); shift 2 ;;
    -*) echo "error: unknown option '$1'" >&2; exit 2 ;;
    *) echo "error: unexpected positional argument '$1'" >&2; exit 2 ;;
  esac
done
[ "$prompt_count" -eq 1 ] || { echo "error: prompt count is $prompt_count" >&2; exit 2; }
serial="$(cat "$SELFTEST_STRICT_SERIAL")"
serial=$((serial + 1)); printf '%s' "$serial" >"$SELFTEST_STRICT_SERIAL"
printf '%s' "$prompt" >"$SELFTEST_STRICT_RECORD_DIR/prompt-$serial.txt"
printf '%s' "$model" >"$SELFTEST_STRICT_RECORD_DIR/model-$serial.txt"
echo '{"verdict":"APPROVED","issues":[]}'
EOF
chmod +x "$WORK/pathbin/gemini"
STRICT_RECORD="$WORK/strict-prompt-record"; mkdir -p "$STRICT_RECORD"
STRICT_SERIAL="$WORK/strict-prompt-serial"; printf '0' >"$STRICT_SERIAL"
export SELFTEST_STRICT_RECORD_DIR="$STRICT_RECORD" SELFTEST_STRICT_SERIAL="$STRICT_SERIAL"

strict_prompt_case() { # $1=名前 $2=本文ファイル $3=runner $4=model(空可) [$5=command $6=readonly]
  sp_name="$1"; sp_file="$2"; sp_runner="$3"; sp_model="$4"; sp_command="${5:-}"; sp_readonly="${6:-}"
  sp_before="$(cat "$STRICT_SERIAL")"
  sp_expected="$WORK/strict-expected-$sp_name.txt"
  sp_body="$(cat "$sp_file")"  # review-agent.sh と同じく末尾改行を落としてから schema を付与する
  printf '%s\n\n%s' "$sp_body" "$SCHEMA_BLOCK_TEXT" >"$sp_expected"
  sp_args=(--runner "$sp_runner" --prompt-file "$sp_file" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 --log-file "$WORK/log-strict-$sp_name.md")
  [ -n "$sp_model" ] && sp_args+=(--model "$sp_model")
  [ -n "$sp_command" ] && sp_args+=(--command "$sp_command" --readonly-flag "$sp_readonly")
  rc=0
  PATH="$WORK/pathbin:$PATH" guard bash "$TARGET" "${sp_args[@]}" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  if check "Gemini 等号 prompt: $sp_name" 0 "$rc"; then
    sp_probe=$((sp_before + 1)); sp_run=$((sp_before + 2))
    if [ "$(cat "$STRICT_RECORD/prompt-$sp_probe.txt")" = 'ping と 1 語だけ返答してください。' ] \
      && cmp -s "$sp_expected" "$STRICT_RECORD/prompt-$sp_run.txt"; then
      ok "Gemini 等号 prompt: $sp_name: プローブ ping と本実行の本文+付与 schema が完全一致"
    else
      ng "Gemini 等号 prompt: $sp_name: プローブ ping と本実行の本文+付与 schema が完全一致"
    fi
    if [ -n "$sp_model" ] && [ "$(cat "$STRICT_RECORD/model-$sp_probe.txt")" = "$sp_model" ] \
      && [ "$(cat "$STRICT_RECORD/model-$sp_run.txt")" = "$sp_model" ]; then
      ok "Gemini 等号 prompt: $sp_name: モデルあり"
    elif [ -z "$sp_model" ] && [ ! -s "$STRICT_RECORD/model-$sp_probe.txt" ] && [ ! -s "$STRICT_RECORD/model-$sp_run.txt" ]; then
      ok "Gemini 等号 prompt: $sp_name: モデルなし"
    else
      ng "Gemini 等号 prompt: $sp_name: モデルの有無が一致"
    fi
  fi
}

printf '%s\n' '通常のレビュー本文' >"$WORK/prompt-strict-normal.md"
printf -- '- 箇条書きの本文\n- 2 行目\n' >"$WORK/prompt-strict-bullet.md"
printf '%s\n' '---' 'title: frontmatter' '---' '本文' >"$WORK/prompt-strict-frontmatter.md"
printf '%s\n' '--option-like 本文' >"$WORK/prompt-strict-option.md"
printf '%s\n' '- 日本語「引用」 "double quote" `backtick` $HOME; && | {model} {prompt}' '改行を含む本文' >"$WORK/prompt-strict-japanese.md"

# 先行 red: 旧分離形は通常の probe を通しても、`-` 始まりの本実行本文で拒否される。
strict_prompt_case normal "$WORK/prompt-strict-normal.md" gemini model-for-test
strict_prompt_case bullet "$WORK/prompt-strict-bullet.md" gemini model-for-test
strict_prompt_case frontmatter "$WORK/prompt-strict-frontmatter.md" gemini model-for-test
strict_prompt_case option "$WORK/prompt-strict-option.md" gemini model-for-test
strict_prompt_case japanese "$WORK/prompt-strict-japanese.md" gemini ""

# build_cmd のレビュー専用の完全一致拡張。短い `-p=` と長い `--prompt=` の両方を、既定表外の
# ランナーで通す(任意文字列の置換を広げない)。
strict_prompt_case short-equals "$WORK/prompt-strict-bullet.md" strict-short "" \
  "gemini --readonly-x -p={prompt}" "--readonly-x"
strict_prompt_case long-equals "$WORK/prompt-strict-option.md" strict-long "" \
  "gemini --readonly-x --prompt={prompt}" "--readonly-x"

# 明示負例: 旧分離形では probe の ping は通るが、箇条書きの本実行が option と解釈されて失敗する。
PATH="$WORK/pathbin:$PATH" run_agent --runner strict-old-split --command "gemini --readonly-x -p {prompt}" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt-strict-bullet.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 \
  --log-file "$WORK/log-strict-old-split.md"; rc=$?
check "Gemini 等号 prompt: 旧分離形 + 箇条書きは本実行で失敗" 8 "$rc"

# --cwd の必須化(R1)。run_agent は --cwd を補うので、ここでは**補わずに直接起動**して
# 「省略すると止まる」を見る。止めるのはログを作る前で、ランナーは起動しない
# (実リポジトリ直下での起動を機構として防ぐ)。--cwd "" は省略と同じ扱い
CWDREQ="$WORK/cwdreq"
for cv in omit empty; do
  rm -rf "$CWDREQ"; mkdir -p "$CWDREQ"; rm -f "$SIDEEFFECT"
  rc=0
  if [ "$cv" = omit ]; then
    ( cd "$CWDREQ" && exec bash "$TARGET" --runner stubrunner \
        --command "bash $WORK/bin/stub-sideeffect.sh --readonly-x" --readonly-flag "--readonly-x" \
        --prompt-file "$WORK/prompt.md" --probe-timeout 10 ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  else
    ( cd "$CWDREQ" && exec bash "$TARGET" --runner stubrunner --cwd "" \
        --command "bash $WORK/bin/stub-sideeffect.sh --readonly-x" --readonly-flag "--readonly-x" \
        --prompt-file "$WORK/prompt.md" --probe-timeout 10 ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  fi
  check "--cwd 必須($cv): usage で止まる" 2 "$rc"
  if grep -qE '^ERROR \[usage\] --cwd が必要' "$CASE_ERR"; then ok "--cwd 必須($cv): 理由が stderr に出る"; else
    ng "--cwd 必須($cv): 理由が stderr に出る"; cat "$CASE_ERR" >&2; fi
  if [ ! -e "$SIDEEFFECT" ]; then ok "--cwd 必須($cv): ランナーを起動しない"; else
    ng "--cwd 必須($cv): ランナーを起動しない(副作用ファイルが作られた)"; fi
  if [ -z "$(find "$CWDREQ" -mindepth 1 -print -quit 2>/dev/null)" ]; then
    ok "--cwd 必須($cv): 起動した場所にログを残さない"
  else
    ng "--cwd 必須($cv): 起動した場所にログを残さない(残存: $(find "$CWDREQ" -mindepth 1 | head -3 | tr '\n' ' '))"
  fi
done
# --dry-run は起動しないので --cwd が無くても通る
rc=0
( cd "$CWDREQ" && exec bash "$TARGET" --runner stubrunner \
    --command "bash $WORK/bin/stub-sideeffect.sh --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --dry-run ) \
  >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
check "--cwd 必須: --dry-run は --cwd が無くても通る" 0 "$rc"

# cd を親シェルで行う形にしたことの副作用を塞いだ 3 点
ENVCASE="$WORK/envcase"; rm -rf "$ENVCASE"; mkdir -p "$ENVCASE/sub" "$ENVCASE/reltmp"
# (1) export された CDPATH: 素の cd が行き先を stdout に出すと、ログパスが 2 行に化け、
#     stdout(指摘 JSON だけ)の出力契約も崩れる。既定のログ置き場と相対の --cwd で見る
rc=0
( cd "$ENVCASE" && exec env CDPATH=. bash "$TARGET" --runner stubrunner \
    --command "bash $WORK/bin/stub-record.sh --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd sub --probe-timeout 10 ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
if check "CDPATH を export した環境: 成功する" 0 "$rc"; then
  if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CASE_OUT" 2>/dev/null; then
    ok "CDPATH を export した環境: stdout は指摘 JSON だけ"
  else
    ng "CDPATH を export した環境: stdout は指摘 JSON だけ"; head -3 "$CASE_OUT" >&2
  fi
fi
# (2) 中に入れない --cwd(検索権限なし)は引数の誤り。cd の失敗が internal(不具合扱い)に化けない
mkdir -p "$ENVCASE/noexec"; chmod 600 "$ENVCASE/noexec"
run_agent --runner stubrunner --command "bash $WORK/bin/stub-record.sh --readonly-x" \
  --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" --cwd "$ENVCASE/noexec" --probe-timeout 10; rc=$?
chmod 700 "$ENVCASE/noexec"
if check "検索権限の無い --cwd: usage で止まる" 2 "$rc"; then
  if grep -qF 'に検索(実行)権限が無くて移動できない' "$CASE_ERR"; then ok "検索権限の無い --cwd: 理由が stderr に出る"; else
    ng "検索権限の無い --cwd: 理由が stderr に出る"; cat "$CASE_ERR" >&2; fi
fi
# (3) 相対の TMPDIR: 一時ファイルへのリダイレクトが cd の後で解決されてずれない
rc=0
( cd "$ENVCASE" && exec env TMPDIR=reltmp bash "$TARGET" --runner stubrunner \
    --command "bash $WORK/bin/stub-record.sh --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
check "相対の TMPDIR: 成功する" 0 "$rc"

# 中止(TERM / HUP): スクリプト自身へのシグナルで、走行中の外部 CLI の子を道連れにする。
# 呼び出し側は背景実行するので、中止操作はシグナルとして届く。子が生き残ると、
# 読み取り専用のはずのプロセスが残り続ける(implement-agent.sh には既にある保護)。
SIGDIR="$WORK/sig"; mkdir -p "$SIGDIR"
cat >"$SIGDIR/runproc.sh" <<'SH'
#!/usr/bin/env bash
trap '' TERM
sleep 120
SH
chmod +x "$SIGDIR/runproc.sh"
cat >"$SIGDIR/stub.sh" <<SH
#!/usr/bin/env bash
# プローブ(末尾引数に ping を含む)は即答し、本実行だけ子を残して待つ。
# 読み取り専用フラグはプローブにも本実行にも付くので、フラグの有無では見分けられない
for a in "\$@"; do case "\$a" in *ping*) echo '{"verdict":"APPROVED","issues":[]}'; exit 0 ;; esac; done
: >"$SIGDIR/started"   # touch はサンドボックスの PATH に無いことがあるので組み込みで作る
bash "$SIGDIR/runproc.sh"
SH
chmod +x "$SIGDIR/stub.sh"
sig_leak_count() { ps -eo args 2>/dev/null | grep -c "^bash $SIGDIR/runproc.sh"; }
sig_cleanup() {
  ps -eo pid,args 2>/dev/null | awk -v s="bash $SIGDIR/runproc.sh" '$0 ~ "[0-9] "s {print $1}' \
    | while read -r pp; do kill -9 "$pp" 2>/dev/null; done
}
# $1=ラベル $2=シグナル $3=期待終了コード $4=path(timeout|fallback) $5=log(abs|default)
sig_case() {
  sc_label="$1"; sc_sig="$2"; sc_want="$3"; sc_path="$4"; sc_log="$5"
  rm -f "$SIGDIR/started"
  sc_logcwd="$SIGDIR/logcwd-$sc_label"; rm -rf "$sc_logcwd"; mkdir -p "$sc_logcwd"
  sc_logargs=(--log-file "$WORK/log-sig-$sc_label.md")
  sc_logpath="$WORK/log-sig-$sc_label.md"
  if [ "$sc_log" = default ]; then
    # 既定のログ置き場は起動した場所からの相対パス。cd した後で中止されても見失わないこと
    sc_logargs=()
    sc_logpath="$sc_logcwd/.claude/reviews/reviewer-stubrunner-iter1.md"
  fi
  if [ "$sc_path" = fallback ]; then
    ( cd "$sc_logcwd" && exec env -i HOME="$HOME" DEV_WORKFLOW_HOST_CLI="selftest-host" \
        PATH="$WORK/sandbox-notimeout" bash "$TARGET" --runner stubrunner \
        --command "bash $SIGDIR/stub.sh --readonly-x" --readonly-flag "--readonly-x" \
        --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 60 \
        ${sc_logargs[@]+"${sc_logargs[@]}"} ) >"$CASE_OUT" 2>"$CASE_ERR" &
  else
    ( cd "$sc_logcwd" && exec bash "$TARGET" --runner stubrunner \
        --command "bash $SIGDIR/stub.sh --readonly-x" --readonly-flag "--readonly-x" \
        --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 60 \
        ${sc_logargs[@]+"${sc_logargs[@]}"} ) >"$CASE_OUT" 2>"$CASE_ERR" &
  fi
  sig_pid=$!
  waited=0
  while [ ! -e "$SIGDIR/started" ] && [ "$waited" -lt 40 ]; do sleep 1; waited=$((waited + 1)); done
  sleep 1
  if [ ! -e "$SIGDIR/started" ]; then
    ng "中止($sc_label): 本実行まで到達しない(治具の失敗)"; cat "$CASE_ERR" >&2
    kill -9 "$sig_pid" 2>/dev/null; wait "$sig_pid" 2>/dev/null; sig_cleanup; return 0
  fi
  kill -"$sc_sig" "$sig_pid" 2>/dev/null
  rc=0; wait "$sig_pid" || rc=$?
  sleep 2
  if [ "$rc" -eq "$sc_want" ]; then ok "中止($sc_label): 終了コード $sc_want で終わる"; else
    ng "中止($sc_label): 終了コード $sc_want で終わる(実際 exit=$rc)"; cat "$CASE_ERR" >&2; fi
  if [ "$(sig_leak_count)" -eq 0 ]; then ok "中止($sc_label): 外部ランナーの子を残さない"; else
    ng "中止($sc_label): 外部ランナーの子を残さない(残存 $(sig_leak_count) 件)"; sig_cleanup; fi
  # ERROR 行は元の stderr に出る(本実行の stderr を受ける一時ファイルへ消えない)
  if grep -qE '^ERROR \[aborted\] ' "$CASE_ERR"; then ok "中止($sc_label): stderr に ERROR [aborted] が出る"; else
    ng "中止($sc_label): stderr に ERROR [aborted] が出る"; cat "$CASE_ERR" >&2; fi
  if grep -qF '**中止**' "$sc_logpath" 2>/dev/null; then ok "中止($sc_label): ログに中止が残る"; else
    ng "中止($sc_label): ログに中止が残る($sc_logpath)"; fi
}
sig_case TERM TERM 143 timeout abs
sig_case HUP HUP 129 timeout abs
# timeout / gtimeout が無い環境(macOS 既定)の代替経路でも子を道連れにする
sig_case TERM-fallback TERM 143 fallback abs
# 既定のログ置き場(相対パス)で中止しても、cd 先の一時ツリー基準で見失わない
sig_case TERM-defaultlog TERM 143 timeout default
sig_cleanup

# 明示した --log-file のパスに既存のエントリが在れば、何も書かずに usage(2)で止まる。
# 呼び出し側がログの名前を決めると、前に動いた外部ランナーが、その名前に外のファイルを指す
# symlink やハードリンクを先に置ける。スタブのランナー(起動されたら痕跡を残す)と制限した PATH で打つ
mkdir -p "$WORK/sandbox-d4"
make_sandbox "$WORK/sandbox-d4"
D4_DIR="$WORK/d4"
rm -rf "$D4_DIR"; mkdir -p "$D4_DIR/outside" "$D4_DIR/logs"
D4_VICTIM="$D4_DIR/outside/victim.txt"
D4_VICTIM_TEXT="外のファイルの中身(書き換えられてはならない)"
d4_reset_victim() { printf '%s\n' "$D4_VICTIM_TEXT" >"$D4_VICTIM"; }
d4_victim_same() { [ "$(cat "$D4_VICTIM" 2>/dev/null)" = "$D4_VICTIM_TEXT" ]; }
d4_run() { # 残り=引数。制限した PATH で打つ
  rc=0
  guard env -i HOME="$HOME" DEV_WORKFLOW_HOST_CLI="selftest-host" PATH="$WORK/sandbox-d4" \
    SELFTEST_SIDEEFFECT="$SIDEEFFECT" bash "$TARGET" --cwd "$WORK" "$@" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  return "$rc"
}
D4_CMD="bash $WORK/bin/stub-sideeffect.sh --readonly-x"

# 外のファイルを指す symlink
d4_reset_victim
ln -s "$D4_VICTIM" "$D4_DIR/logs/symlink.md"
rm -f "$SIDEEFFECT"
d4_run --runner stubrunner --command "$D4_CMD" --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --run-timeout 20 --log-file "$D4_DIR/logs/symlink.md"; rc=$?
check "明示のログ(既存のエントリ): 外のファイルを指す symlink を usage で拒む" 2 "$rc"
if d4_victim_same; then ok "明示のログ(既存のエントリ): 外のファイルを指す symlink のリンク先の中身が変わらない"; else
  ng "明示のログ(既存のエントリ): 外のファイルを指す symlink のリンク先の中身が変わらない"; cat "$D4_VICTIM" >&2; fi
if [ -e "$SIDEEFFECT" ]; then ng "明示のログ(既存のエントリ): 外のファイルを指す symlink でランナーを起動しない"; else
  ok "明示のログ(既存のエントリ): 外のファイルを指す symlink でランナーを起動しない"; fi

# 通常ファイル(ハードリンクも通常ファイルに見える)
D4_REGULAR_TEXT="前に残したログ"
printf '%s\n' "$D4_REGULAR_TEXT" >"$D4_DIR/logs/regular.md"
d4_run --runner stubrunner --command "$D4_CMD" --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --run-timeout 20 --log-file "$D4_DIR/logs/regular.md"; rc=$?
check "明示のログ(既存のエントリ): 通常ファイルを usage で拒む" 2 "$rc"
if [ "$(cat "$D4_DIR/logs/regular.md" 2>/dev/null)" = "$D4_REGULAR_TEXT" ]; then
  ok "明示のログ(既存のエントリ): 通常ファイルの中身が変わらない"
else
  ng "明示のログ(既存のエントリ): 通常ファイルの中身が変わらない"; cat "$D4_DIR/logs/regular.md" >&2
fi

# リンク先の無い symlink(`-e` は偽になる)
D4_DANGLING_TARGET="$D4_DIR/outside/created-by-log.txt"
rm -f "$D4_DANGLING_TARGET"
ln -s "$D4_DANGLING_TARGET" "$D4_DIR/logs/dangling.md"
d4_run --runner stubrunner --command "$D4_CMD" --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --run-timeout 20 --log-file "$D4_DIR/logs/dangling.md"; rc=$?
check "明示のログ(既存のエントリ): リンク先の無い symlink を usage で拒む" 2 "$rc"
if [ -e "$D4_DANGLING_TARGET" ] || [ -L "$D4_DANGLING_TARGET" ]; then
  ng "明示のログ(既存のエントリ): リンク先の無い symlink のリンク先が作られない"
else
  ok "明示のログ(既存のエントリ): リンク先の無い symlink のリンク先が作られない"
fi

# ディレクトリ
mkdir -p "$D4_DIR/logs/dir.md"
d4_run --runner stubrunner --command "$D4_CMD" --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --run-timeout 20 --log-file "$D4_DIR/logs/dir.md"; rc=$?
check "明示のログ(既存のエントリ): ディレクトリを usage で拒む" 2 "$rc"

# ログの初期化より前に die する形(既定表に無いランナーを --command で渡し、--readonly-flag を
# 渡さない → no-readonly の die)で、外のファイルを指す symlink。明示した --log-file の既存のエントリは、
# 引数の検査の直後の早い検査が usage で止める。この検査が無いと、ログを開く位置まで進む形では FIFO で
# 待ち続け、この形では usage でなく 5(no-readonly の die)で返る。前提として、既存のエントリの無い名前を
# 渡すと、初期化より前の die で止まり、そのパスにエントリを作らない(ログを開く前の失敗はログに書かない)
# ことを先に確かめる(「開く前の失敗:」。この形が初期化より前の die を通らないと、1 つ目と同じケースになる)
d4_run --runner stubrunner --command "$D4_CMD" --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --log-file "$D4_DIR/logs/early-fresh.md"; rc=$?
check "開く前の失敗: 初期化より前の die(--readonly-flag の欠け)は exit 5 で止まる" 5 "$rc"
if grep -qE '^ERROR \[no-readonly\] ' "$CASE_ERR"; then ok "開く前の失敗: stderr に ERROR [no-readonly] が出る"; else
  ng "開く前の失敗: stderr に ERROR [no-readonly] が出る"; cat "$CASE_ERR" >&2; fi
if [ -e "$D4_DIR/logs/early-fresh.md" ] || [ -L "$D4_DIR/logs/early-fresh.md" ]; then
  ng "開く前の失敗: 明示したパスにエントリを作らない"; cat "$D4_DIR/logs/early-fresh.md" >&2 2>/dev/null
else
  ok "開く前の失敗: 明示したパスにエントリを作らない"
fi
d4_reset_victim
ln -s "$D4_VICTIM" "$D4_DIR/logs/symlink-early.md"
rm -f "$SIDEEFFECT"
d4_run --runner stubrunner --command "$D4_CMD" --prompt-file "$WORK/prompt.md" \
  --probe-timeout 10 --log-file "$D4_DIR/logs/symlink-early.md"; rc=$?
check "明示のログ(既存のエントリ): 初期化より前に die する形でも、外のファイルを指す symlink を usage で拒む" 2 "$rc"
if d4_victim_same; then ok "明示のログ(既存のエントリ): 初期化より前に die する形でも、リンク先の中身が変わらない"; else
  ng "明示のログ(既存のエントリ): 初期化より前に die する形でも、リンク先の中身が変わらない"; cat "$D4_VICTIM" >&2; fi

# ── K. ログは作るときに 1 回だけ開き、以後はその fd に書く ──
# ケース名の接頭辞: 走行中の差し替え / 既定名の置き場 / ERR のログ / ERR の生出力 / ERR の行数 / ログのパス /
# 子に渡す fd / POSIX モード / /dev/fd の判定が偽(「開く前の失敗:」は 1 つ前の節の前提の位置にある)。
# どれもスタブと制限した PATH で打ち、対象の一時領域($TMPDIR)はこのスイートの一時領域へ向ける。
# FIFO のケースは guard(TERM の後に KILL まで行う)で打ち、外側の時間を K_FIFO_TIMEOUT に縮める
# (パスで開き直す実装は、TERM の後も中止の処理が FIFO を開き直して待つので、KILL まで終わらない)。
# timeout か mkfifo が無い環境では、FIFO のケースを飛ばす(待ち続けて selftest が終わらないのを避ける)
K_DIR="$WORK/k"
rm -rf "$K_DIR"; mkdir -p "$K_DIR" "$K_DIR/tmp" "$K_DIR/wcbin" "$WORK/sandbox-k"
make_sandbox "$WORK/sandbox-k"
K_SANDBOX="$WORK/sandbox-k"
K_TMP="$K_DIR/tmp"
K_PATH="$K_SANDBOX"   # k_run の PATH(ケースごとにスタブのディレクトリを先頭に足す)
K_TARGET=""           # 空なら $TARGET
K_CWD=""              # 空ならスイートの cwd で打つ
K_EXTRA=()            # 追加の環境変数(`VAR=値` の並び)
K_FIFO_TIMEOUT=30
K_PREV_TIMEOUT="$OUTER_TIMEOUT"
REAL_LN="$(command -v ln)"
REAL_MKFIFO="$(command -v mkfifo 2>/dev/null || true)"
K_FIFO_OK=0
if [ -n "$OUTER_TIMEOUT_BIN" ] && [ -n "$REAL_MKFIFO" ]; then K_FIFO_OK=1; fi
K_VICTIM="$K_DIR/victim.txt"
K_VICTIM_TEXT="外のファイルの中身(書き換えられてはならない)"
k_reset_victim() { printf '%s\n' "$K_VICTIM_TEXT" >"$K_VICTIM"; }
k_victim_same() { [ "$(cat "$K_VICTIM" 2>/dev/null)" = "$K_VICTIM_TEXT" ]; }
k_run() { # 残り=引数。制限した PATH・このスイートの一時領域の TMPDIR で、guard を付けて打つ
  rc=0
  kr_target="${K_TARGET:-$TARGET}"
  if [ -n "$K_CWD" ]; then
    ( cd "$K_CWD" && guard env -i HOME="$HOME" DEV_WORKFLOW_HOST_CLI="selftest-host" PATH="$K_PATH" TMPDIR="$K_TMP" \
        ${K_EXTRA[@]+"${K_EXTRA[@]}"} bash "$kr_target" "$@" ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  else
    guard env -i HOME="$HOME" DEV_WORKFLOW_HOST_CLI="selftest-host" PATH="$K_PATH" TMPDIR="$K_TMP" \
      ${K_EXTRA[@]+"${K_EXTRA[@]}"} bash "$kr_target" "$@" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
  fi
  if [ "$VERBOSE" -eq 1 ]; then
    echo "--- args: $* (exit=$rc)"; echo "  stdout:"; sed 's/^/    /' "$CASE_OUT"; echo "  stderr:"; sed 's/^/    /' "$CASE_ERR"
  fi
  return "$rc"
}

# 本実行の間に、スクリプトが開いたログのパスを差し替えるスタブ(プローブは即答する)。
# 差し替えた後は長く眠らない。中止のケース(SELFTEST_SWAP_HOLD=1)だけ、中止されるまで居座る(最長 60 秒)
cat >"$WORK/bin/swaphold.sh" <<'EOF'
#!/usr/bin/env bash
i=0
while [ "$i" -lt 60 ]; do sleep 1; i=$((i + 1)); done
EOF
cat >"$WORK/bin/stub-swap.sh" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do case "\$a" in *ping*) echo '{"verdict":"APPROVED","issues":[]}'; exit 0 ;; esac; done
rm -f "\$SELFTEST_SWAP_LOG"
case "\$SELFTEST_SWAP_MODE" in
  symlink)  "$REAL_LN" -s "\$SELFTEST_SWAP_VICTIM" "\$SELFTEST_SWAP_LOG" ;;
  hardlink) "$REAL_LN" "\$SELFTEST_SWAP_VICTIM" "\$SELFTEST_SWAP_LOG" ;;
  fifo)     "$REAL_MKFIFO" "\$SELFTEST_SWAP_LOG" ;;
esac
echo '{"verdict":"APPROVED","issues":[]}'
if [ "\${SELFTEST_SWAP_HOLD:-0}" = 1 ]; then
  : >"\$SELFTEST_SWAP_MARKER"
  exec bash "$WORK/bin/swaphold.sh"
fi
exit 0
EOF
# 渡された fd 7 が開いていれば、そこへ偽の行を書くスタブ(ログの fd が外部 CLI に渡ると、ログを偽造できる)
cat >"$WORK/bin/stub-fd.sh" <<'EOF'
#!/usr/bin/env bash
{ printf 'FORGED: 外部 CLI がログの fd に書いた\n' >&7; } 2>/dev/null || true
echo '{"verdict":"APPROVED","issues":[]}'
EOF
# `wc` だけを失敗させるスタブ(最初の `wc` はプロンプト長の計測。ログを開いた後・外部 CLI を起動する前)
cat >"$K_DIR/wcbin/wc" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$WORK/bin/swaphold.sh" "$WORK/bin/stub-swap.sh" "$WORK/bin/stub-fd.sh" "$K_DIR/wcbin/wc"
K_SWAP_CMD="bash $WORK/bin/stub-swap.sh --readonly-x"

# 走行中の差し替え: (a) victim への symlink (b) victim へのハードリンク (c) FIFO。
# 開いたファイルに書き続けるので victim は変わらず、スタブどおり exit 0 で終わり、差し替えを NOTE で知らせる。
# (c) はログのパスを読むと FIFO で塞がるので、NOTE と終わったことだけを見る
k_swap_case() { # $1=ラベル $2=差し替えの形(symlink|hardlink|fifo)
  ks_label="$1"; ks_mode="$2"
  ks_log="$K_DIR/swap-$ks_mode.md"
  k_reset_victim
  K_EXTRA=("SELFTEST_SWAP_MODE=$ks_mode" "SELFTEST_SWAP_LOG=$ks_log" "SELFTEST_SWAP_VICTIM=$K_VICTIM")
  if [ "$ks_mode" = fifo ]; then OUTER_TIMEOUT="$K_FIFO_TIMEOUT"; fi
  k_run --runner stubrunner --command "$K_SWAP_CMD" --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" \
    --cwd "$WORK" --probe-timeout 10 --run-timeout 20 --log-file "$ks_log"; rc=$?
  OUTER_TIMEOUT="$K_PREV_TIMEOUT"
  K_EXTRA=()
  check "走行中の差し替え: $ks_label: スクリプトが終わる(スタブどおりの終了コード)" 0 "$rc"
  if grep -qF 'NOTE: ログのパスが走行中に差し替えられた:' "$CASE_ERR"; then
    ok "走行中の差し替え: $ks_label: stderr に差し替えの NOTE が出る"
  else
    ng "走行中の差し替え: $ks_label: stderr に差し替えの NOTE が出る"; cat "$CASE_ERR" >&2
  fi
  if [ "$ks_mode" != fifo ]; then
    if k_victim_same; then ok "走行中の差し替え: $ks_label: victim の中身が変わらない"; else
      ng "走行中の差し替え: $ks_label: victim の中身が変わらない"; cat "$K_VICTIM" >&2; fi
  fi
}
k_swap_case "(a) symlink" symlink
k_swap_case "(b) ハードリンク" hardlink
if [ "$K_FIFO_OK" -eq 1 ]; then
  k_swap_case "(c) FIFO" fifo
else
  ok "走行中の差し替え: (c) FIFO: timeout か mkfifo が無いため飛ばす"
fi

# (d) (a) の差し替えの後、本実行の間に TERM を送る。中止の処理は本実行の stderr のリダイレクトが
#     効いたまま走るので、差し替えの NOTE が消えずに元の stderr に出ることを見る
leak_cleanup
K_MARKER="$K_DIR/swap.marker"
rm -f "$K_MARKER"
k_reset_victim
kd_log="$K_DIR/swap-term.md"
env -i HOME="$HOME" DEV_WORKFLOW_HOST_CLI="selftest-host" PATH="$K_SANDBOX" TMPDIR="$K_TMP" \
  SELFTEST_SWAP_MODE=symlink SELFTEST_SWAP_LOG="$kd_log" SELFTEST_SWAP_VICTIM="$K_VICTIM" \
  SELFTEST_SWAP_HOLD=1 SELFTEST_SWAP_MARKER="$K_MARKER" \
  bash "$TARGET" --runner stubrunner --command "$K_SWAP_CMD" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 45 --log-file "$kd_log" \
  >"$CASE_OUT" 2>"$CASE_ERR" &
kd_pid=$!
waited=0
while [ ! -e "$K_MARKER" ] && [ "$waited" -lt 60 ]; do sleep 1; waited=$((waited + 1)); done
sleep 1
if [ ! -e "$K_MARKER" ]; then
  ng "走行中の差し替え: (d) 中止: 本実行まで到達しない(治具の失敗)"; cat "$CASE_ERR" >&2
  kill -9 "$kd_pid" 2>/dev/null; wait "$kd_pid" 2>/dev/null
else
  kill -TERM "$kd_pid" 2>/dev/null
  rc=0; wait "$kd_pid" || rc=$?
  if grep -qE '^ERROR \[aborted\] ' "$CASE_ERR"; then ok "走行中の差し替え: (d) 中止: stderr に ERROR [aborted] が出る"; else
    ng "走行中の差し替え: (d) 中止: stderr に ERROR [aborted] が出る(実際 exit=$rc)"; cat "$CASE_ERR" >&2; fi
  if grep -qF 'NOTE: ログのパスが走行中に差し替えられた:' "$CASE_ERR"; then
    ok "走行中の差し替え: (d) 中止: 差し替えの NOTE が中止の経路でも stderr に出る"
  else
    ng "走行中の差し替え: (d) 中止: 差し替えの NOTE が中止の経路でも stderr に出る"; cat "$CASE_ERR" >&2
  fi
  if k_victim_same; then ok "走行中の差し替え: (d) 中止: victim の中身が変わらない"; else
    ng "走行中の差し替え: (d) 中止: victim の中身が変わらない"; cat "$K_VICTIM" >&2; fi
fi
leak_cleanup

# 既定名の置き場: 既定の置き場の iter1 に (a) FIFO (b) /dev/null を指す symlink を置く。
# その番号の名前にエントリが在れば開かずに次の番号へ進むので、iter2 に通常ファイルのログを作って exit 0 で終わる
k_deflog_case() { # $1=ラベル $2=形(fifo|devnull)
  kl_label="$1"; kl_kind="$2"
  kl_dir="$K_DIR/deflog-$kl_kind"
  rm -rf "$kl_dir"; mkdir -p "$kl_dir/.claude/reviews"
  kl_iter1="$kl_dir/.claude/reviews/reviewer-stubrunner-iter1.md"
  kl_iter2="$kl_dir/.claude/reviews/reviewer-stubrunner-iter2.md"
  case "$kl_kind" in
    fifo) "$REAL_MKFIFO" "$kl_iter1" ;;
    devnull) "$REAL_LN" -s /dev/null "$kl_iter1" ;;
  esac
  K_CWD="$kl_dir"
  OUTER_TIMEOUT="$K_FIFO_TIMEOUT"
  k_run --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20; rc=$?
  OUTER_TIMEOUT="$K_PREV_TIMEOUT"
  K_CWD=""
  check "既定名の置き場: $kl_label: exit 0 で終わる" 0 "$rc"
  if [ -f "$kl_iter2" ] && [ ! -L "$kl_iter2" ]; then ok "既定名の置き場: $kl_label: iter2 が通常ファイル"; else
    ng "既定名の置き場: $kl_label: iter2 が通常ファイル"; fi
  kl_same=0
  case "$kl_kind" in
    fifo) if [ -p "$kl_iter1" ] && [ ! -L "$kl_iter1" ]; then kl_same=1; fi ;;
    devnull) if [ -L "$kl_iter1" ] && [ "$(readlink "$kl_iter1")" = /dev/null ]; then kl_same=1; fi ;;
  esac
  if [ "$kl_same" -eq 1 ]; then ok "既定名の置き場: $kl_label: iter1 はそのまま"; else
    ng "既定名の置き場: $kl_label: iter1 はそのまま"; fi
}
if [ "$K_FIFO_OK" -eq 1 ]; then
  k_deflog_case "(a) FIFO" fifo
else
  ok "既定名の置き場: (a) FIFO: timeout か mkfifo が無いため飛ばす"
fi
k_deflog_case "(b) /dev/null を指す symlink" devnull

# ERR のログ: `wc` だけを失敗させるスタブで、ログを開いた後・外部 CLI を起動する前(プロンプト長の計測)に
# 主シェルの ERR を起こす。ログにも結果行を書く
K_PATH="$K_DIR/wcbin:$K_SANDBOX"
k_run --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 --log-file "$K_DIR/err-log.md"; rc=$?
K_PATH="$K_SANDBOX"
if [ "$rc" -eq 20 ] && grep -qF '**結果**: ERROR [internal]' "$K_DIR/err-log.md" 2>/dev/null; then
  ok "ERR のログ: ログを開いた後の想定外の失敗は、ログに **結果**: ERROR [internal] の行を書く (exit=$rc)"
else
  ng "ERR のログ: ログを開いた後の想定外の失敗は、ログに **結果**: ERROR [internal] の行を書く(実際 exit=$rc)"
  cat "$CASE_ERR" >&2
fi

# ERR の生出力: 写しの本実行の行の直後に `false` を 1 行差し込み、生出力の後で ERR を起こす。
# 得られた分の生出力はログの節にだけ残す(stdout には出さない)。写しが当たったこと(差し込んだ行が 1 つ)を先に確かめる
K_RUN_LINE='run_timeout "$RUN_TIMEOUT" "${CMD[@]}" </dev/null >"$RAW_OUT" 2>"$RAW_ERR" || rc=$?'
K_ERRRAW_TARGET="$K_DIR/err-raw-target.sh"
awk -v anchor="$K_RUN_LINE" '{ print } $0 == anchor { print "false" }' "$TARGET" >"$K_ERRRAW_TARGET"
if [ "$(grep -cxF 'false' "$K_ERRRAW_TARGET")" -ne "$(( $(grep -cxF 'false' "$TARGET") + 1 ))" ]; then
  ng "ERR の生出力: 写しの治具(本実行の行の直後に false を差し込む)が当たらない"
else
  K_TARGET="$K_ERRRAW_TARGET"
  k_run --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 --log-file "$K_DIR/err-raw.md"; rc=$?
  K_TARGET=""
  if grep -qF '### 生出力(想定外の失敗の時点まで)' "$K_DIR/err-raw.md" 2>/dev/null; then
    ok "ERR の生出力: ログに「生出力(想定外の失敗の時点まで)」の節がある"
  else
    ng "ERR の生出力: ログに「生出力(想定外の失敗の時点まで)」の節がある(実際 exit=$rc)"; cat "$CASE_ERR" >&2
  fi
  if [ ! -s "$CASE_OUT" ]; then ok "ERR の生出力: stdout は空"; else
    ng "ERR の生出力: stdout は空"; cat "$CASE_OUT" >&2; fi
fi

# ERR の行数: #100 D3 の治具(サブシェルの中の失敗)。置換の終了コードが親へ伝わる形では、
# 親の ERR が同じ行で 1 回だけ報告する(終了コードの欄はサブシェルの値)
if [ -n "${D3_LINE:-}" ] && [ -n "${D3_DATESTUB:-}" ] && [ -d "$D3_DATESTUB" ]; then
  K_TARGET="$D3_TARGET"
  K_PATH="$D3_DATESTUB:$K_SANDBOX"
  k_run --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 --log-file "$K_DIR/err-lines.md"; rc=$?
  K_TARGET=""
  K_PATH="$K_SANDBOX"
  kn_lines="$(grep -cE '^ERROR \[internal\] ' "$CASE_ERR")"
  kn_first="$(grep -E '^ERROR \[internal\] ' "$CASE_ERR" | head -1)"
  kn_code="$(printf '%s' "$kn_first" | sed -n 's/.*終了コード \([0-9][0-9]*\)・行.*/\1/p')"
  kn_line="$(printf '%s' "$kn_first" | sed -n 's/.*行 \([0-9][0-9]*\)).*/\1/p')"
  if [ "$rc" -eq 20 ] && [ "$kn_lines" -eq 1 ] && [ "$kn_code" = 1 ] && [ "$kn_line" = "$D3_LINE" ]; then
    ok "ERR の行数: サブシェルの中の失敗は ERROR [internal] の行を 1 行だけ出す(終了コードの欄は 1・行は D3 と同じ) (exit=$rc)"
  else
    ng "ERR の行数: サブシェルの中の失敗は ERROR [internal] の行を 1 行だけ出す(実際 exit=$rc・行数=$kn_lines・終了コード=${kn_code:-無し}・行=${kn_line:-無し}(期待 exit=20・1 行・1・行 $D3_LINE))"
    cat "$CASE_ERR" >&2
  fi
else
  ng "ERR の行数: #100 D3 の治具が当たらない"
fi

# ログのパス: ログを開いたら、stderr に `NOTE: ログ: <絶対パス>` を 1 行出す(既定名・明示のどちらでも)
k_logpath_case() { # $1=ラベル $2=起動する場所 $3=実際のログのパス 残り=追加の引数
  kp_label="$1"; kp_cwd="$2"; kp_want="$3"; shift 3
  rm -rf "$kp_cwd"; mkdir -p "$kp_cwd"
  K_CWD="$kp_cwd"
  k_run --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 "$@"; rc=$?
  K_CWD=""
  kp_n="$(grep -c '^NOTE: ログ: ' "$CASE_ERR")"
  kp_note="$(sed -n 's/^NOTE: ログ: //p' "$CASE_ERR" | head -1)"
  kp_abs=0
  case "$kp_note" in /*) kp_abs=1 ;; esac
  if [ "$rc" -eq 0 ] && [ "$kp_n" -eq 1 ] && [ "$kp_abs" -eq 1 ] && [ -f "$kp_want" ] && [ "$kp_note" -ef "$kp_want" ]; then
    ok "ログのパス: $kp_label: stderr の NOTE: ログ: が実際のログの絶対パスを 1 行で示す"
  else
    ng "ログのパス: $kp_label: stderr の NOTE: ログ: が実際のログの絶対パスを 1 行で示す(実際 exit=$rc・行数=$kp_n・'$kp_note')"
    cat "$CASE_ERR" >&2
  fi
}
k_logpath_case "既定名" "$K_DIR/logpath-default" "$K_DIR/logpath-default/.claude/reviews/reviewer-stubrunner-iter1.md"
k_logpath_case "明示(相対パス)" "$K_DIR/logpath-explicit" "$K_DIR/logpath-explicit/rel/explicit.md" --log-file "rel/explicit.md"

# 子に渡す fd: 外部 CLI(プローブ・本実行)にログの fd を渡さない。スタブは fd 7 が開いていれば
# 偽の行を書く(パスで開き直す実装では fd 7 が無いので、この形の差は変異でだけ現れる)
k_run --runner stubrunner --command "bash $WORK/bin/stub-fd.sh --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 --log-file "$K_DIR/fd.md"; rc=$?
check "子に渡す fd: 本実行まで通る" 0 "$rc"
if [ -s "$K_DIR/fd.md" ] && ! grep -qF 'FORGED' "$K_DIR/fd.md"; then
  ok "子に渡す fd: 外部 CLI がログの fd に書けない(ログに FORGED が無い)"
else
  ng "子に渡す fd: 外部 CLI がログの fd に書けない(ログに FORGED が無い)"; cat "$K_DIR/fd.md" >&2 2>/dev/null
fi

# POSIX モード: POSIXLY_CORRECT の環境でも、特殊組み込みのリダイレクトの失敗でシェルが終わらず、
# 置き場に書けない既定名(ROLOG と同じ構成)を usage で止める。root は置き場に書けてしまうので飛ばす
if [ "$(id -u)" -ne 0 ]; then
  KX="$K_DIR/posix"
  rm -rf "$KX"; mkdir -p "$KX/.claude/reviews"
  : > "$KX/.claude/reviews/reviewer-stubrunner-iter1.md"
  chmod 555 "$KX/.claude/reviews"
  K_CWD="$KX"
  K_EXTRA=("POSIXLY_CORRECT=1")
  k_run --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20; rc=$?
  K_EXTRA=()
  K_CWD=""
  chmod 755 "$KX/.claude/reviews" 2>/dev/null
  check "POSIX モード: 置き場に書けない既定名は usage で止まる" 2 "$rc"
  if grep -qE '^ERROR \[usage\] ' "$CASE_ERR"; then ok "POSIX モード: stderr に ERROR [usage] が出る"; else
    ng "POSIX モード: stderr に ERROR [usage] が出る"; cat "$CASE_ERR" >&2; fi
else
  ok "POSIX モード: 置き場に書けない既定名は usage で止まる(root のため飛ばす)"
fi

# /dev/fd の判定が偽: 写しで `[ -f /dev/fd/7 ]` を false に替え、既定名で打つ。開いたものを通常ファイルと
# 確かめられないときは、既定名でも次の番号へ進まずに止まる(採番を繰り返して空のファイルを作り続けない)
K_DEVFD_TARGET="$K_DIR/devfd-false.sh"
sed 's#\[ -f /dev/fd/7 \]#false#g' "$TARGET" >"$K_DEVFD_TARGET"
kf_hits="$(grep -cF '[ -f /dev/fd/7 ]' "$TARGET")"
kf_left="$(grep -cF '[ -f /dev/fd/7 ]' "$K_DEVFD_TARGET")"
if [ "$kf_hits" -ge 1 ] && [ "$kf_left" -eq 0 ]; then
  KF="$K_DIR/devfd"
  rm -rf "$KF"; mkdir -p "$KF"
  K_TARGET="$K_DEVFD_TARGET"
  K_CWD="$KF"
  OUTER_TIMEOUT="$K_FIFO_TIMEOUT"
  k_run --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20; rc=$?
  OUTER_TIMEOUT="$K_PREV_TIMEOUT"
  K_CWD=""
  K_TARGET=""
  check "/dev/fd の判定が偽: 外側の timeout の中で usage で止まる" 2 "$rc"
  kf_count="$(find "$KF/.claude/reviews" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${kf_count:-0}" -le 1 ]; then ok "/dev/fd の判定が偽: 置き場のファイルが 1 つ以下(採番を繰り返さない)"; else
    ng "/dev/fd の判定が偽: 置き場のファイルが 1 つ以下(採番を繰り返さない)(実際 $kf_count 個)"; fi
  # ログのパス・置き場に関わる usage の失敗は、開けたものが通常ファイルでないときも置き場の語を含む(既定名・明示の両方)
  if grep -E '^ERROR \[usage\] ' "$CASE_ERR" | grep -qF -- '既定のログ置き場'; then
    ok "/dev/fd の判定が偽: 既定名: ERROR [usage] の行に「既定のログ置き場」がある"
  else
    ng "/dev/fd の判定が偽: 既定名: ERROR [usage] の行に「既定のログ置き場」がある"; cat "$CASE_ERR" >&2; fi
  K_TARGET="$K_DEVFD_TARGET"
  K_CWD="$KF"
  OUTER_TIMEOUT="$K_FIFO_TIMEOUT"
  k_run --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 --log-file "$KF/explicit/x.md"; rc=$?
  OUTER_TIMEOUT="$K_PREV_TIMEOUT"
  K_CWD=""
  K_TARGET=""
  check "/dev/fd の判定が偽: 明示の --log-file でも usage で止まる" 2 "$rc"
  if grep -E '^ERROR \[usage\] ' "$CASE_ERR" | grep -qF -- '--log-file の置き場'; then
    ok "/dev/fd の判定が偽: 明示の --log-file: ERROR [usage] の行に「--log-file の置き場」がある"
  else
    ng "/dev/fd の判定が偽: 明示の --log-file: ERROR [usage] の行に「--log-file の置き場」がある"; cat "$CASE_ERR" >&2; fi
else
  ng "/dev/fd の判定が偽: 写しの治具([ -f /dev/fd/7 ] を false に替える)が当たらない"
fi

# ── L. 置き場の経路: ログの置き場を物理パスで固定してから開く ──
# ケース名の接頭辞: 置き場の経路。既定名の置き場(起動時の cwd の .claude/reviews)と、起動時の cwd の中の明示の
# --log-file の置き場は、起動時の pwd -P を基点に、階層ごとに symlink を拒み、cd -P の後の pwd -P を照合して物理パスに
# 固定してから相対名で開く。基点の外の --log-file は信頼して mkdir -p で作る。
# 止まるケースは exit 2・stderr の ERROR [usage] の行(既定名なら「既定のログ置き場」、明示なら「--log-file の置き場」と、
# 「置き場の経路が symlink か差し替えられた: 」)・リンク先が空・ランナーを起動しない(副作用のスタブ)・NOTE: ログ: が無い。
# (d) は PATH の先頭に置いた mkdir の包み(K 節の wcbin と同じ置き方)で、検査と作成の間の差し替えを起こす。
# (f)・(g) は BASH_ENV で読み込ませた cd・pwd の関数で、検査と cd の間・固定と開く間の差し替えを決定的に起こす
# (対象で発火する・ファイルの印で 1 回だけ・条件の外では builtin を呼ぶだけ・対象の set -eEu と ERR trap の下で失敗しない)。
# 明示の --log-file では接頭辞の照合のサブシェルの pwd -P で先に発火するので、(f)・(g) は既定名で打つ。
# どれも k_run(制限した PATH・env -i)で打つ
L_DIR="$K_DIR/place"
rm -rf "$L_DIR"; mkdir -p "$L_DIR"
L_CWD="$L_DIR/cwd"; mkdir -p "$L_CWD"
L_MARK="$L_DIR/hook.mark"
REAL_MKDIR="$(command -v mkdir)"
REAL_RMDIR="$(command -v rmdir)"
REAL_MV="$(command -v mv)"
L_CMD="bash $WORK/bin/stub-sideeffect.sh --readonly-x"
L_EXTRA=()
l_run() { # $1=起動する場所 $2=--cwd 残り=追加の引数。副作用のスタブで打つ(起動したら $SIDEEFFECT が残る)
  lr_cwd="$1"; lr_runcwd="$2"; shift 2
  rm -f "$SIDEEFFECT"
  K_CWD="$lr_cwd"
  K_EXTRA=("SELFTEST_SIDEEFFECT=$SIDEEFFECT" ${L_EXTRA[@]+"${L_EXTRA[@]}"})
  k_run --runner stubrunner --command "$L_CMD" --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt.md" \
    --cwd "$lr_runcwd" --probe-timeout 10 --run-timeout 20 "$@"; rc=$?
  K_CWD=""; K_EXTRA=()
  return "$rc"
}
l_empty() { [ -z "$(find "$1" -mindepth 1 -print -quit 2>/dev/null)" ]; }
l_note_is() { # $1=ログの実体。stderr の NOTE: ログ: が絶対パスで、そのログを指す
  ln_note="$(sed -n 's/^NOTE: ログ: //p' "$CASE_ERR" | head -1)"
  case "$ln_note" in /*) [ -f "$1" ] && [ "$ln_note" -ef "$1" ] ;; *) return 1 ;; esac
}
l_assert_stopped() { # $1=ケース名 $2=実際の exit $3=置き場の語 $4=リンク先(空ならこの検査を飛ばす) [$5=在ってはならないパス]
  la_name="$1"; la_rc="$2"; la_word="$3"; la_out="$4"; la_absent="${5:-}"
  if [ "$la_rc" -eq 2 ] && grep -qE '^ERROR \[usage\] ' "$CASE_ERR"; then ok "$la_name: exit 2 で ERROR [usage] の行が出る"; else
    ng "$la_name: exit 2 で ERROR [usage] の行が出る(実際 exit=$la_rc)"; cat "$CASE_ERR" >&2; fi
  if grep -E '^ERROR \[usage\] ' "$CASE_ERR" | grep -qF -- "$la_word" \
     && grep -E '^ERROR \[usage\] ' "$CASE_ERR" | grep -qF -- '置き場の経路が symlink か差し替えられた: '; then
    ok "$la_name: 理由に「$la_word」と「置き場の経路が symlink か差し替えられた: 」がある"
  else
    ng "$la_name: 理由に「$la_word」と「置き場の経路が symlink か差し替えられた: 」がある"; cat "$CASE_ERR" >&2; fi
  if [ -n "$la_out" ]; then
    if l_empty "$la_out"; then ok "$la_name: リンク先が空"; else
      ng "$la_name: リンク先が空(残存: $(find "$la_out" -mindepth 1 | head -3 | tr '\n' ' '))"; fi
  fi
  if [ -n "$la_absent" ]; then
    if [ ! -e "$la_absent" ] && [ ! -L "$la_absent" ]; then ok "$la_name: 基点の直下に書かない"; else
      ng "$la_name: 基点の直下に書かない(残存: $la_absent)"; fi
  fi
  if [ ! -e "$SIDEEFFECT" ]; then ok "$la_name: ランナーを起動しない"; else ng "$la_name: ランナーを起動しない(副作用ファイルが作られた)"; fi
  if ! grep -q '^NOTE: ログ: ' "$CASE_ERR"; then ok "$la_name: NOTE: ログ: が無い"; else
    ng "$la_name: NOTE: ログ: が無い"; cat "$CASE_ERR" >&2; fi
}
# (f)・(g) のフック。$1=出力 $2=発火する側(cd|pwd) $3=cd の最後の引数(階層名) $4=置き場の物理パス $5=外のディレクトリ
l_make_hook() {
  cat >"$1" <<EOF
# selftest の (f)・(g) のフック(BASH_ENV で読み込ませる)。対象で発火し、ファイルの印で 1 回だけ。条件の外では builtin を呼ぶだけ。
# 対象の set -eEu と ERR trap の下で走るので、失敗しうるコマンドには || true を付ける
cd() {
  if [ "$2" = cd ] && [ \$# -gt 0 ] && { [ "\${!#}" = "$3" ] || [ "\${!#}" = "./$3" ]; } && [ ! -e "$L_MARK" ]; then
    : >"$L_MARK" 2>/dev/null || true
    "$REAL_RMDIR" -- "$3" 2>/dev/null || true
    "$REAL_LN" -s -- "$5" "$3" 2>/dev/null || true
  fi
  builtin cd "\$@"
}
pwd() {
  builtin pwd "\$@"
  if [ "$2" = pwd ] && [ ! -e "$L_MARK" ] && [ "\$(builtin pwd -P)" = "$4" ]; then
    : >"$L_MARK" 2>/dev/null || true
    "$REAL_MV" -- "$4" "$4.moved" 2>/dev/null || true
    "$REAL_LN" -s -- "$5" "$4" 2>/dev/null || true
  fi
  return 0
}
EOF
}

# (a) 既定名で .claude/reviews が外を指す symlink
la="$L_DIR/a"; mkdir -p "$la/proj/.claude" "$la/out"; "$REAL_LN" -s "$la/out" "$la/proj/.claude/reviews"
l_run "$la/proj" "$L_CWD"; rc=$?
l_assert_stopped "置き場の経路: (a) 既定名で .claude/reviews が外を指す symlink" "$rc" "既定のログ置き場" "$la/out"

# (b) 既定名で .claude が外を指す symlink
lb="$L_DIR/b"; mkdir -p "$lb/proj" "$lb/out"; "$REAL_LN" -s "$lb/out" "$lb/proj/.claude"
l_run "$lb/proj" "$L_CWD"; rc=$?
l_assert_stopped "置き場の経路: (b) 既定名で .claude が外を指す symlink" "$rc" "既定のログ置き場" "$lb/out"

# (c) 明示の --log-file(起動時の cwd の中)で置き場の経路に symlink
lc="$L_DIR/c"; mkdir -p "$lc/proj/.claude" "$lc/out"; "$REAL_LN" -s "$lc/out" "$lc/proj/.claude/reviews"
l_run "$lc/proj" "$L_CWD" --log-file "$lc/proj/.claude/reviews/reviewer-stubrunner-iter1.md"; rc=$?
l_assert_stopped "置き場の経路: (c) 明示の --log-file で置き場の経路に symlink" "$rc" "--log-file の置き場" "$lc/out"

# (c2) .claude/reviews が基点そのものを指す symlink + 明示の --log-file(最も深い一致でなく最初の一致で基点を決めるので、
#      .claude の後ろの reviews を検査して止まる。基点の直下に x.md を書かない)
lc2="$L_DIR/c2"; mkdir -p "$lc2/proj/.claude"; "$REAL_LN" -s "$lc2/proj" "$lc2/proj/.claude/reviews"
l_run "$lc2/proj" "$L_CWD" --log-file "$lc2/proj/.claude/reviews/x.md"; rc=$?
l_assert_stopped "置き場の経路: (c2) .claude/reviews が基点そのものを指す symlink + 明示の --log-file" "$rc" "--log-file の置き場" "" "$lc2/proj/x.md"

# (d) 検査と作成の間に置かれた symlink: PATH の先頭に置いた mkdir の包みが、作ろうとする置き場(mkdir -- reviews の形と
#     mkdir -p .claude/reviews の形の両方)に先に外を指す symlink を置いてから本物の mkdir を呼ぶ
ld="$L_DIR/d"; mkdir -p "$ld/proj/.claude" "$ld/out" "$ld/mkdirbin"
cat >"$ld/mkdirbin/mkdir" <<EOF
#!/usr/bin/env bash
if [ \$# -gt 0 ]; then
  case "\${!#}" in
    reviews|.claude/reviews)
      if [ ! -e "\${!#}" ] && [ ! -L "\${!#}" ]; then "$REAL_LN" -s -- "$ld/out" "\${!#}"; fi ;;
  esac
fi
exec "$REAL_MKDIR" "\$@"
EOF
chmod +x "$ld/mkdirbin/mkdir"
K_PATH="$ld/mkdirbin:$K_SANDBOX"
l_run "$ld/proj" "$L_CWD"; rc=$?
K_PATH="$K_SANDBOX"
ld_name="置き場の経路: (d) 検査と作成の間に置かれた symlink"
if [ -L "$ld/proj/.claude/reviews" ]; then ok "$ld_name: 治具(mkdir の包み)が置き場に symlink を置いた"; else
  ng "$ld_name: 治具(mkdir の包み)が置き場に symlink を置いた"; fi
l_assert_stopped "$ld_name" "$rc" "既定のログ置き場" "$ld/out"

# (f) 検査の後・cd の前の差し替え: cd の関数が、置き場の階層(reviews)へ入る呼び出しの前に、その階層を外を指す symlink に差し替える
lf="$L_DIR/f"; mkdir -p "$lf/proj/.claude/reviews" "$lf/out"
lf_phys="$(cd "$lf/proj" && pwd -P)"
l_make_hook "$lf/hook.sh" cd reviews "$lf_phys/.claude/reviews" "$lf/out"
rm -f "$L_MARK"
L_EXTRA=("BASH_ENV=$lf/hook.sh")
l_run "$lf/proj" "$L_CWD"; rc=$?
L_EXTRA=()
lf_name="置き場の経路: (f) 検査の後・cd の前の差し替え"
if [ -e "$L_MARK" ]; then ok "$lf_name: フックが発火した(cd の最後の引数が階層名)"; else
  ng "$lf_name: フックが発火した(cd の最後の引数が階層名)"; fi
l_assert_stopped "$lf_name" "$rc" "既定のログ置き場" "$lf/out"

# (g) 固定の後・開く前の差し替え: pwd の関数が、置き場の中で呼ばれた後に、置き場を退かして外を指す symlink を置く。
#     相対名で開くので、ログは退かした置き場に書かれ、外には書かれない
lg="$L_DIR/g"; mkdir -p "$lg/proj/.claude/reviews" "$lg/out"
lg_phys="$(cd "$lg/proj" && pwd -P)"
l_make_hook "$lg/hook.sh" pwd reviews "$lg_phys/.claude/reviews" "$lg/out"
rm -f "$L_MARK"
L_EXTRA=("BASH_ENV=$lg/hook.sh")
l_run "$lg/proj" "$L_CWD"; rc=$?
L_EXTRA=()
lg_name="置き場の経路: (g) 固定の後・開く前の差し替え"
check "$lg_name: スクリプトが終わる" 0 "$rc"
if [ -e "$L_MARK" ]; then ok "$lg_name: フックが発火した(pwd -P が置き場の物理パス)"; else
  ng "$lg_name: フックが発火した(pwd -P が置き場の物理パス)"; fi
if l_empty "$lg/out"; then ok "$lg_name: 外に書かない(リンク先が空)"; else
  ng "$lg_name: 外に書かない(リンク先が空)(残存: $(find "$lg/out" -mindepth 1 | head -3 | tr '\n' ' '))"; fi
if [ -s "$lg_phys/.claude/reviews.moved/reviewer-stubrunner-iter1.md" ]; then ok "$lg_name: ログは退かした置き場に書かれる"; else
  ng "$lg_name: ログは退かした置き場に書かれる"; ls -la "$lg_phys/.claude" >&2 2>/dev/null; fi

# (h) 基点の外の --log-file は開ける(基点の外は信頼するので、経路の symlink は辿る)
lh="$L_DIR/h"; mkdir -p "$lh/proj" "$lh/real"; "$REAL_LN" -s "$lh/real" "$lh/link"
l_run "$lh/proj" "$L_CWD" --log-file "$lh/link/logs/x.md"; rc=$?
lh_name="置き場の経路: (h) 基点の外の --log-file"
if check "$lh_name: 開ける" 0 "$rc"; then
  if [ -f "$lh/real/logs/x.md" ]; then ok "$lh_name: リンク先にログがある"; else ng "$lh_name: リンク先にログがある"; fi
  if l_note_is "$lh/real/logs/x.md"; then ok "$lh_name: NOTE: ログ: が絶対パスでそのログを指す"; else
    ng "$lh_name: NOTE: ログ: が絶対パスでそのログを指す"; cat "$CASE_ERR" >&2; fi
fi

# (i) 起動場所と --cwd を分けたときの既定名は、起動場所の .claude/reviews に出る(--cwd の下には作らない)
li="$L_DIR/i"; mkdir -p "$li/launch" "$li/cwd"
l_run "$li/launch" "$li/cwd"; rc=$?
li_name="置き場の経路: (i) 起動場所と --cwd を分けた既定名"
if check "$li_name: 成功する" 0 "$rc"; then
  if [ -f "$li/launch/.claude/reviews/reviewer-stubrunner-iter1.md" ]; then ok "$li_name: 起動場所の .claude/reviews に出る"; else
    ng "$li_name: 起動場所の .claude/reviews に出る"; fi
  if [ ! -e "$li/cwd/.claude" ] && [ ! -L "$li/cwd/.claude" ]; then ok "$li_name: --cwd の下には作らない"; else
    ng "$li_name: --cwd の下には作らない"; fi
fi

# (j) 相対の --log-file: plain.md・./x.md は起動時の cwd の直下、../x.md は起動時の cwd の親に開ける
lj="$L_DIR/j"; mkdir -p "$lj/proj"
for lj_spec in "plain.md|$lj/proj/plain.md" "./x.md|$lj/proj/x.md" "../x.md|$lj/x.md"; do
  lj_arg="${lj_spec%%|*}"; lj_want="${lj_spec#*|}"
  l_run "$lj/proj" "$L_CWD" --log-file "$lj_arg"; rc=$?
  lj_name="置き場の経路: (j) 相対の --log-file $lj_arg"
  if check "$lj_name: 開ける" 0 "$rc"; then
    if [ -f "$lj_want" ]; then ok "$lj_name: 置き場は $lj_want"; else ng "$lj_name: 置き場は $lj_want"; fi
    if l_note_is "$lj_want"; then ok "$lj_name: NOTE: ログ: が絶対パスでそのログを指す"; else
      ng "$lj_name: NOTE: ログ: が絶対パスでそのログを指す"; cat "$CASE_ERR" >&2; fi
  fi
done

# ログのパス・置き場に関わる usage の失敗は、どれも ERROR [usage] の行に置き場の語(既定名は「既定のログ置き場」、明示は
# 「--log-file の置き場」)を含む(呼び出し側はこの語で置き場の理由の停止と判定する)。exit 2・ランナーを起動しない・NOTE: ログ: が無い
l_assert_word() { # $1=ケース名 $2=実際の exit $3=置き場の語
  lw_name="$1"; lw_rc="$2"; lw_word="$3"
  if [ "$lw_rc" -eq 2 ] && grep -qE '^ERROR \[usage\] ' "$CASE_ERR"; then ok "$lw_name: exit 2 で ERROR [usage] の行が出る"; else
    ng "$lw_name: exit 2 で ERROR [usage] の行が出る(実際 exit=$lw_rc)"; cat "$CASE_ERR" >&2; fi
  if grep -E '^ERROR \[usage\] ' "$CASE_ERR" | grep -qF -- "$lw_word"; then ok "$lw_name: ERROR [usage] の行に「$lw_word」がある"; else
    ng "$lw_name: ERROR [usage] の行に「$lw_word」がある"; cat "$CASE_ERR" >&2; fi
  if [ ! -e "$SIDEEFFECT" ]; then ok "$lw_name: ランナーを起動しない"; else ng "$lw_name: ランナーを起動しない(副作用ファイルが作られた)"; fi
  if ! grep -q '^NOTE: ログ: ' "$CASE_ERR"; then ok "$lw_name: NOTE: ログ: が無い"; else
    ng "$lw_name: NOTE: ログ: が無い"; cat "$CASE_ERR" >&2; fi
}

# (k) 置き場が在って書けない(chmod 555): 既定名・明示の両方で止まる。root は書けてしまうので飛ばす
lk="$L_DIR/k"; mkdir -p "$lk/proj/.claude/reviews"
lk_name="置き場の経路: (k) 置き場が在って書けない"
if [ "$(id -u)" -ne 0 ]; then
  chmod 555 "$lk/proj/.claude/reviews"
  l_run "$lk/proj" "$L_CWD"; rc=$?
  l_assert_word "$lk_name(既定名)" "$rc" "既定のログ置き場"
  l_run "$lk/proj" "$L_CWD" --log-file "$lk/proj/.claude/reviews/x.md"; rc=$?
  l_assert_word "$lk_name(明示の --log-file)" "$rc" "--log-file の置き場"
  chmod 755 "$lk/proj/.claude/reviews"
else
  ok "$lk_name(既定名): root のため飛ばす"
  ok "$lk_name(明示の --log-file): root のため飛ばす"
fi

# (k2) 名前に既にエントリが在る: 引数の直後の早い検査は、語彙的に正規化したパスで見る(a/link/../x.md は a/x.md。
#      link の先を辿って proj/x.md を見ない)
lk2="$L_DIR/k2"; mkdir -p "$lk2/proj/a" "$lk2/proj/b"; : >"$lk2/proj/a/x.md"; "$REAL_LN" -s "$lk2/proj/b" "$lk2/proj/a/link"
l_run "$lk2/proj" "$L_CWD" --log-file "$lk2/proj/a/link/../x.md"; rc=$?
lk2_name="置き場の経路: (k2) 名前に既にエントリが在る(a/link/../x.md は a/x.md)"
l_assert_word "$lk2_name" "$rc" "--log-file の置き場"
if [ ! -e "$lk2/proj/x.md" ] && [ ! -L "$lk2/proj/x.md" ] && [ ! -s "$lk2/proj/a/x.md" ]; then
  ok "$lk2_name: リンク先の側にも既存の名前にも書かない"
else
  ng "$lk2_name: リンク先の側にも既存の名前にも書かない"; ls -la "$lk2/proj" "$lk2/proj/a" >&2; fi

# (k2′) (k2) の補い: 字面のパスの側の proj/x.md が在っても、正規化した a/x.md が無ければそこに開く(proj/x.md も b の側も触らない)
lk3="$L_DIR/k2p"; mkdir -p "$lk3/proj/a" "$lk3/proj/b"; printf 'keep\n' >"$lk3/proj/x.md"; "$REAL_LN" -s "$lk3/proj/b" "$lk3/proj/a/link"
l_run "$lk3/proj" "$L_CWD" --log-file "$lk3/proj/a/link/../x.md"; rc=$?
lk3_name="置き場の経路: (k2′) proj/x.md が在っても a/x.md に開く(a/link/../x.md は a/x.md)"
if check "$lk3_name: 開ける" 0 "$rc"; then
  if [ -f "$lk3/proj/a/x.md" ] && [ ! -L "$lk3/proj/a/x.md" ]; then ok "$lk3_name: a/x.md にログがある"; else ng "$lk3_name: a/x.md にログがある"; fi
  if l_note_is "$lk3/proj/a/x.md"; then ok "$lk3_name: NOTE: ログ: が a/x.md を指す"; else
    ng "$lk3_name: NOTE: ログ: が a/x.md を指す"; cat "$CASE_ERR" >&2; fi
  if [ "$(cat "$lk3/proj/x.md")" = keep ] && [ "$(wc -c <"$lk3/proj/x.md" | tr -d ' ')" -eq 5 ]; then
    ok "$lk3_name: proj/x.md の中身と大きさが変わらない"
  else
    ng "$lk3_name: proj/x.md の中身と大きさが変わらない"; head -c 200 "$lk3/proj/x.md" >&2; fi
  if l_empty "$lk3/proj/b"; then ok "$lk3_name: b の側に書かない"; else
    ng "$lk3_name: b の側に書かない(残存: $(find "$lk3/proj/b" -mindepth 1 | head -3 | tr '\n' ' '))"; fi
fi

echo
printf '%s\n' "$RESULTS"
echo
echo "結果: PASS ${PASS} 件 / FAIL ${FAIL} 件"
[ "$FAIL" -eq 0 ]
