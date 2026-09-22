#!/usr/bin/env bash
# review-agent.sh の回帰テスト(スタブのみ・外部 CLI 不要・ネットワーク不要)。
#
# 使い方:
#   bash review-agent-selftest.sh            # 全ケース実行
#   bash review-agent-selftest.sh -v         # 各ケースの出力も表示
#   REVIEW_AGENT=<パス> bash review-agent-selftest.sh   # 別の実装を対象にする(変異テスト用)
#
# 検証するのは「終了コードと出力の契約」「信頼モデルが破れないこと」「正規化 2 経路の一致」
# 「プロンプト組み立て(スキーマ指示の付与と冪等)」。
# 期待終了コード: 0=成功 2=usage 3=not-found 4=self-host 5=no-readonly 6=probe-failed
#                 7=probe-timeout 8=run-failed 9=run-timeout 10=parse-failed 12=prompt-too-large
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
  ps -eo pid,args 2>/dev/null | awk -v s="bash $WORK/bin/hangproc.sh" '$0 ~ "[0-9] "s {print $1}' \
    | while read -r p; do kill -9 "$p" 2>/dev/null; done
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
  # ケースまで usage エラーで落ちる。**必須であること自体は専用のケースで検査する**
  ra_args=("$@")
  ra_has_cwd=0
  for ra_a in "$@"; do [ "$ra_a" = "--cwd" ] && ra_has_cwd=1; done
  if [ "$ra_has_cwd" -eq 0 ]; then ra_args[${#ra_args[@]}]="--cwd"; ra_args[${#ra_args[@]}]="$WORK"; fi
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
    bash "$TARGET" "$@" --cwd "$WORK" >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
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

# 35. 想定外の失敗は ERR trap が拾って exit 20 にする(黙って落ちない)
run_agent --runner stubrunner --command "bash $STUB_OK --readonly-x" --readonly-flag "--readonly-x" \
  --prompt-file "$WORK/prompt.md" --log-file "/dev/null/nested/x.md"; rc=$?
if [ "$rc" -eq 20 ] && grep -q '^ERROR \[internal\]' "$CASE_ERR"; then ok "想定外の失敗を ERR trap が exit 20 で報告 (exit=$rc)"; else
  ng "想定外の失敗を ERR trap が exit 20 で報告 (実際 exit=$rc)"; cat "$CASE_ERR" >&2; fi

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

# 40. --trust を落とすと同じランナーが probe-failed で止まる(決定 16 の再現)。
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
ROLOG="$WORK/rolog"
rm -rf "$ROLOG"; mkdir -p "$ROLOG/.claude/reviews"
: > "$ROLOG/.claude/reviews/reviewer-codex-iter1.md"
chmod 555 "$ROLOG/.claude/reviews"
rc=0
( cd "$ROLOG" && guard bash "$TARGET" --runner codex --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 20 ) >"$CASE_OUT" 2>"$CASE_ERR" || rc=$?
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

# `-` で始まるプロンプト(箇条書き・frontmatter)がオプションと誤認されない。
# `{prompt}` を持たないテンプレでは末尾に足すため、`--` を挟まないとランナーが拒否する
# (実測: codex は `error: unexpected argument '- ' found` で落ち、`-- - ` を使えと案内する)。
printf -- '- 箇条書きで始まるレビュー依頼\n- 2 行目\n' >"$WORK/prompt-dash.md"
rec_reset
run_agent --runner stubrunner --command "bash $WORK/bin/stub-record.sh --readonly-x" \
  --readonly-flag "--readonly-x" --prompt-file "$WORK/prompt-dash.md" \
  --probe-timeout 10 --log-file "$WORK/log-dash.md"; rc=$?
if check "- 始まりのプロンプト: そのまま最後の引数として渡る" 0 "$rc"; then
  if grep -q '箇条書きで始まるレビュー依頼' "$SELFTEST_RECORD_DIR/lastarg.txt"; then
    ok "- 始まりのプロンプト: 内容が欠けずに渡る"
  else
    ng "- 始まりのプロンプト: 内容が欠けずに渡る"; head -c 200 "$SELFTEST_RECORD_DIR/lastarg.txt" >&2
  fi
fi

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
touch "$SIGDIR/started"
bash "$SIGDIR/runproc.sh"
SH
chmod +x "$SIGDIR/stub.sh"
sig_leak_count() { ps -eo args 2>/dev/null | grep -c "^bash $SIGDIR/runproc.sh"; }
sig_cleanup() {
  ps -eo pid,args 2>/dev/null | awk -v s="bash $SIGDIR/runproc.sh" '$0 ~ "[0-9] "s {print $1}' \
    | while read -r pp; do kill -9 "$pp" 2>/dev/null; done
}
for spec in "TERM:143" "HUP:129"; do
  sig="${spec%%:*}"; want="${spec##*:}"
  rm -f "$SIGDIR/started"
  bash "$TARGET" --runner stubrunner --command "bash $SIGDIR/stub.sh --readonly-x" --readonly-flag "--readonly-x" \
    --prompt-file "$WORK/prompt.md" --cwd "$WORK" --probe-timeout 10 --run-timeout 60 \
    --log-file "$WORK/log-sig-$sig.md" >"$CASE_OUT" 2>"$CASE_ERR" &
  sig_pid=$!
  waited=0
  while [ ! -e "$SIGDIR/started" ] && [ "$waited" -lt 40 ]; do sleep 1; waited=$((waited + 1)); done
  sleep 1
  if [ ! -e "$SIGDIR/started" ]; then
    ng "中止($sig): 本実行まで到達しない(治具の失敗)"
    kill -9 "$sig_pid" 2>/dev/null; wait "$sig_pid" 2>/dev/null; sig_cleanup; continue
  fi
  kill -"$sig" "$sig_pid" 2>/dev/null
  rc=0; wait "$sig_pid" || rc=$?
  sleep 2
  if [ "$rc" -eq "$want" ]; then ok "中止($sig): 終了コード $want で終わる"; else
    ng "中止($sig): 終了コード $want で終わる(実際 exit=$rc)"; cat "$CASE_ERR" >&2; fi
  if [ "$(sig_leak_count)" -eq 0 ]; then ok "中止($sig): 外部ランナーの子を残さない"; else
    ng "中止($sig): 外部ランナーの子を残さない(残存 $(sig_leak_count) 件)"; sig_cleanup; fi
  if grep -qF '**中止**' "$WORK/log-sig-$sig.md"; then ok "中止($sig): ログに中止が残る"; else
    ng "中止($sig): ログに中止が残る"; fi
done
sig_cleanup

echo
printf '%s\n' "$RESULTS"
echo
echo "結果: PASS ${PASS} 件 / FAIL ${FAIL} 件"
[ "$FAIL" -eq 0 ]
