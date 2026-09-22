#!/usr/bin/env bash
# implement-guard.sh の回帰テスト(scratch リポジトリだけを使う。外部 CLI 不要・ネットワーク不要)。
# diff-snapshot-selftest.sh / implement-agent-selftest.sh とは独立したスイートで、互いのファイルには
# 触れない(../references/external-runners.md §11 の決定 9)。
#
# 使い方:
#   bash implement-guard-selftest.sh              # 全ケース(A〜K)+ 変異テスト(L)
#   bash implement-guard-selftest.sh -v           # 各起動の終了コード・stdout・stderr も表示
#   bash implement-guard-selftest.sh --only B1,G  # 指定したケースだけ(1 文字なら群の全体)。
#                                                 # L を含めない限り変異テストは回さない
#   IMPLEMENT_GUARD=<パス> bash implement-guard-selftest.sh   # 別の実装を対象にする(変異テスト用)
#
# 全起動に外側タイムアウトを掛ける(既定 90 秒・重い起動 180 秒)。正常時は待たないので所要は変わらない。
# 時間切れ(timeout の 124 / 137)は、対象の internal エラー(20)と混ざらないように別の文言で報告する。
#
# 検証するのは「終了コードと stdout の契約」「git の状態を変えないこと(.git/index のバイト列)」
# 「リポジトリに設定されたプログラムを実行しないこと(痕跡スタブ・git の呼び出し記録)」
# 「改竄・削除・差し替えの検出」「タスク MD の保護と復元」「保護領域の後始末」。
# 期待終了コード: 0=成功 2=usage 20=internal 30=縮退(take) 31=引き継ぎ 32=縮退(compare)
#                 33=比較不能・照合や復元の失敗 34=要件本文の変更 35=選択パスと実体の対応の変化
#
# ケース名の先頭はケース ID(A1〜K2・L)。検出の類型に当たるケース名には固定のタグ
# `[類型:<名前>]` が入る(内容変更 / 完了条件書き換え / 削除 / symlink差し替え / 日本語パス /
# 空白入りパス / unbornHEAD / stash / 別ブランチ切替)。
#
# 変異テスト(L・13 個): 対象の写しに sed で変異を 1 つ当て(対象側の目印 `# MUT:a`〜`# MUT:m`)、
# `IMPLEMENT_GUARD=<写し>` でこのスイート自身を `--only <対応するケース>` で回す。**対応するケースが
# FAIL し、スイートが非ゼロで終わること**が PASS の条件。目印が無い・sed が空振りした変異は
# それ自体を FAIL にする。変異版でも対照ケース(E1)は PASS すること(壊れ方が変異に固有であること)も見る。
#
# 保護領域: 対象は保護領域を /tmp・$TMPDIR の外にしか作らない。成功ケースのために、実
# `$HOME/.local/state/dev-workflow/` の下へ `selftest-guard-XXXXXX` を作って `XDG_STATE_HOME` を
# そこへ向け、終了時(EXIT)に必ず消す。そこに既に在る他のファイルには触れない。
#
# (変異テストの子スイートは、親が作った保護領域を `IMPLEMENT_GUARD_SELFTEST_PROT` で借りる。子は作らず・
# 消さないので、子が強制終了されても実 `$HOME` に残骸が出ない。この変数は内部用で、手では設定しない。)
#
# 前提: bash 4.0 以上(対象のスクリプトが連想配列を使う)。git は PATH にあるもの。
#
# 終了コード: 0=全件 PASS / 1=FAIL あり
set -uo pipefail

SELF_PATH="${BASH_SOURCE[0]}"
case "$SELF_PATH" in /*) : ;; *) SELF_PATH="$PWD/$SELF_PATH" ;; esac
SCRIPT_DIR="$(dirname -- "$SELF_PATH")"
TARGET="${IMPLEMENT_GUARD:-$SCRIPT_DIR/implement-guard.sh}"
case "$TARGET" in /*) : ;; *) TARGET="$PWD/$TARGET" ;; esac
VERBOSE=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    -v) VERBOSE=1; shift ;;
    --only)
      [ $# -ge 2 ] || { echo "ERROR: --only に値が必要" >&2; exit 1; }
      ONLY="$2"; shift 2 ;;
    *) echo "ERROR: 不明な引数: $1" >&2; exit 1 ;;
  esac
done

[ -f "$TARGET" ] || { echo "ERROR: $TARGET が無い" >&2; exit 1; }

REAL_GIT="$(command -v git)"
[ -n "$REAL_GIT" ] || { echo "ERROR: git が無い" >&2; exit 1; }
REAL_CP="$(command -v cp)"
[ -n "$REAL_CP" ] || { echo "ERROR: cp が無い" >&2; exit 1; }

REAL_HOME="${HOME:-}"
if [ -z "$REAL_HOME" ] || [ ! -d "$REAL_HOME" ]; then
  echo "ERROR: HOME が無い(保護領域を置けない)" >&2
  exit 1
fi

# ── 後始末(scratch と、このスイートが作った保護領域だけを消す)──
WORK=""
PROT=""
OWN_PROT=0
MADE_BASE=0
PROT_BASE="$REAL_HOME/.local/state/dev-workflow"
cleanup_all() {
  if [ -n "$WORK" ]; then
    chmod -R u+rwX -- "$WORK" 2>/dev/null || true
    rm -rf -- "$WORK"
  fi
  if [ "$OWN_PROT" -eq 1 ]; then
    case "$PROT" in
      "$PROT_BASE"/selftest-guard-?*)
        chmod -R u+rwX -- "$PROT" 2>/dev/null || true
        rm -rf -- "$PROT" ;;
    esac
    if [ "$MADE_BASE" -eq 1 ]; then rmdir -- "$PROT_BASE" 2>/dev/null || true; fi
  fi
}
trap cleanup_all EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

phys_dir() { # $1=存在するディレクトリ → 物理パス
  if command -v realpath >/dev/null 2>&1 && realpath -- "$1" 2>/dev/null; then return 0; fi
  if command -v readlink >/dev/null 2>&1 && readlink -f -- "$1" 2>/dev/null; then return 0; fi
  ( CDPATH= cd -P -- "$1" 2>/dev/null && pwd -P )
}

WORK="$(mktemp -d)" || { echo "ERROR: scratch を作れない" >&2; exit 1; }
cd "$WORK" || exit 1
WORK="$(pwd -P)"                       # 対象が記録する物理パスと照合できるように正規化する
cd "$WORK" || exit 1
[ -n "$WORK" ] && [ "$PWD" = "$WORK" ] || { echo "ERROR: scratch へ移動できない" >&2; exit 1; }
mkdir -p "$WORK/home" "$WORK/xdg" "$WORK/stubs" "$WORK/mut"

# 保護領域。変異テストの子として起動されたときは、親が作ったものを借りる(子は作らず・消さない。
# 子が強制終了されても実 $HOME に残骸が出ないように)
if [ -n "${IMPLEMENT_GUARD_SELFTEST_PROT:-}" ]; then
  case "$IMPLEMENT_GUARD_SELFTEST_PROT" in
    */dev-workflow/selftest-guard-?*) : ;;
    *) echo "ERROR: IMPLEMENT_GUARD_SELFTEST_PROT が selftest 用の保護領域でない" >&2; exit 1 ;;
  esac
  [ -d "$IMPLEMENT_GUARD_SELFTEST_PROT" ] || { echo "ERROR: 借りる保護領域が無い" >&2; exit 1; }
  PROT="$IMPLEMENT_GUARD_SELFTEST_PROT"
else
  if [ ! -d "$PROT_BASE" ]; then
    mkdir -p -- "$PROT_BASE" || { echo "ERROR: $PROT_BASE を作れない" >&2; exit 1; }
    MADE_BASE=1
    chmod 700 -- "$PROT_BASE" 2>/dev/null || true
  fi
  OWN_PROT=1
  PROT="$(mktemp -d "$PROT_BASE/selftest-guard-XXXXXX")" || { PROT=""; echo "ERROR: selftest 用の保護領域を作れない" >&2; exit 1; }
fi
PROT_REAL="$(phys_dir "$PROT")"
[ -n "$PROT_REAL" ] || { echo "ERROR: 保護領域の物理パスを解決できない" >&2; exit 1; }
TMP_REAL="$(phys_dir /tmp 2>/dev/null || true)"
TMPDIR_REAL=""
if [ -n "${TMPDIR:-}" ]; then TMPDIR_REAL="$(phys_dir "$TMPDIR" 2>/dev/null || true)"; fi
for d in "$TMP_REAL" "$TMPDIR_REAL"; do
  [ -n "$d" ] || continue
  case "$PROT_REAL" in "$d"|"$d"/*)
    echo "ERROR: \$HOME/.local/state が /tmp・\$TMPDIR の配下に在る($PROT_REAL)。成功ケースを回せない" >&2
    exit 1 ;;
  esac
done

# 隔離(ユーザーの設定を一切読ませない)。git は無視設定・属性の既定値をホームと XDG の下から
# 読むので、そこも scratch へ向ける。保護領域だけは XDG_STATE_HOME で /tmp の外へ向ける
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_PAGER=cat
export LC_ALL=C
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_PREFIX GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS 2>/dev/null || true
export GIT_CEILING_DIRECTORIES="$WORK"   # scratch の外のリポジトリを絶対に掴まない
export HOME="$WORK/home"
export XDG_CONFIG_HOME="$WORK/xdg"
export XDG_STATE_HOME="$PROT"

IS_ROOT=0
[ "$(id -u)" = 0 ] && IS_ROOT=1

TAB=$'\t'
PASS=0
FAIL=0
RESULTS=""
CID=""      # 実行中のケース ID(ケース名の先頭に付く)
TAG=""      # 実行中のケースの類型タグ
ok() { PASS=$((PASS + 1)); RESULTS="$RESULTS
PASS  $CID $TAG$1"; }
ng() { FAIL=$((FAIL + 1)); RESULTS="$RESULTS
FAIL  $CID $TAG$1"; }

# ── 汎用アサーション ──
ckt() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else ng "$d"; fi; }
ckf() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ng "$d"; else ok "$d"; fi; }
flat() { local s="${1//$'\n'/\\n}"; printf '%s' "${s//$'\t'/\\t}"; }   # 結果の 1 行を崩さない(改行・タブを見える形にする)
ckeq() { if [ "$2" = "$3" ]; then ok "$1"; else ng "$1(実際: $(flat "$2") / 期待: $(flat "$3"))"; fi; }
ckne() { if [ "$2" != "$3" ]; then ok "$1"; else ng "$1(実際: $(flat "$2"))"; fi; }
inf() { grep -qF -e "$2" -- "$1"; }      # $1=ファイル $2=部分文字列(先頭が - でも安全)
inx() { grep -qxF -e "$2" -- "$1"; }     # $1=ファイル $2=行全体
ine() { grep -qE -e "$2" -- "$1"; }      # $1=ファイル $2=拡張正規表現

# ── 対象の起動(全起動が外側タイムアウト + stdin /dev/null の guard() を通る)──
CASE_OUT="$WORK/case.out"
CASE_ERR="$WORK/case.err"
RC=0
GUARD_BIN=""
if command -v timeout >/dev/null 2>&1; then GUARD_BIN=timeout
elif command -v gtimeout >/dev/null 2>&1; then GUARD_BIN=gtimeout; fi
# 外側タイムアウトは「止まらない実装」を落とすための保険で、正常時は待たないので長めでよい。
# 短いと**並走の負荷でたまたま遅れた起動**が落ち、対象の契約(TERM → exit 20)と混ざって
# 「internal エラー」に見える。時間切れは timeout の 124(KILL まで要ったときは 137)で見分ける
T_RUN=90         # 通常の起動
T_SLOW=180       # 重い起動(上限超過の検査。2000 件・200 MiB を数える)
T_MUT=360        # 変異の子スイート(ケース数件ぶん)
LAST_T=""
TIMED_OUT=0      # 直前の起動が外側タイムアウトで落ちたか
guard() { LAST_T="$T_RUN"; if [ -n "$GUARD_BIN" ]; then "$GUARD_BIN" -k 5 "$T_RUN" "$@"; else "$@"; fi; }
guard_t() { local t="$1"; shift; LAST_T="$t"; if [ -n "$GUARD_BIN" ]; then "$GUARD_BIN" -k 5 "$t" "$@"; else "$@"; fi; }
# 対象の終了コードは 0 / 2 / 20 / 30〜35 に閉じているので、124・137 は外側タイムアウトしかない
mark_timeout() {
  TIMED_OUT=0
  [ -n "$GUARD_BIN" ] || return 0
  case "$RC" in 124|137) TIMED_OUT=1 ;; esac
}

show() {
  [ "$VERBOSE" -eq 1 ] || return 0
  echo "=== [$CID] $* → rc=$RC"
  echo "--- stdout"; cat "$CASE_OUT"
  echo "--- stderr"; cat "$CASE_ERR"
}
# 対象の stdin は常に /dev/null(スイート自身の stdin が開いたままでも待たない)
run() { RC=0; guard bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?; mark_timeout; show "$@"; }
# 重い起動用(上限超過。変異版では退避まで進むので長めに待つ)
run_slow() { RC=0; guard_t "$T_SLOW" bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?; mark_timeout; show "$@"; }
# PATH の先頭にスタブ置き場を足して起動する
runp() { local p="$1"; shift; RC=0; guard env PATH="$p:$PATH" bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?; mark_timeout; show "$@"; }
# 環境変数を足して起動する: rune VAR=値 … -- 引数…
rune() {
  local envs=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs[${#envs[@]}]="$1"; shift; done
  shift
  RC=0
  guard env "${envs[@]}" bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?
  mark_timeout
  show "$@"
}

rc_is() { # 時間切れは「期待コードとの不一致」ではなく、外側タイムアウトと分かる形で出す
  if [ "$RC" = "$2" ]; then ok "$1"
  elif [ "$TIMED_OUT" -eq 1 ]; then ng "$1(**外側タイムアウト**: ${LAST_T:-?} 秒で打ち切られた〈rc=$RC〉。実装の不具合ではなく実行が遅い可能性 — 期待: $2)"
  else ng "$1(実際: $RC / 期待: $2)"; fi
}
out_line() { if inx "$CASE_OUT" "$2"; then ok "$1"; else ng "$1(stdout に行が無い: $(flat "$2"))"; fi; }
out_has() { if inf "$CASE_OUT" "$2"; then ok "$1"; else ng "$1(stdout に無い: $(flat "$2"))"; fi; }
out_re() { if ine "$CASE_OUT" "$2"; then ok "$1"; else ng "$1(stdout が一致しない: $(flat "$2"))"; fi; }
out_lacks() { if inf "$CASE_OUT" "$2"; then ng "$1(stdout に在る: $(flat "$2"))"; else ok "$1"; fi; }
err_has() { if inf "$CASE_ERR" "$2"; then ok "$1"; else ng "$1(stderr に無い: $(flat "$2"))"; fi; }
out_count() { grep -c -e "$1" -- "$CASE_OUT" || true; }   # $1=基本正規表現 → 一致した行数

# ── 観測用の小道具(sha256・モード・サイズは 2〜3 形式だけ吸収する)──
if command -v sha256sum >/dev/null 2>&1; then sha_of() { sha256sum <"$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then sha_of() { shasum -a 256 <"$1" | cut -d' ' -f1; }
else sha_of() { openssl dgst -sha256 <"$1" | sed 's/.*[= ]//'; }; fi
if stat -c %a -- / >/dev/null 2>&1; then mode_of() { stat -c %a -- "$1"; }
else mode_of() { stat -f %Lp -- "$1"; }; fi
size_of() { wc -c <"$1" | tr -d ' '; }
nstates() { find "$PROT" -maxdepth 2 -name 'guard-*' 2>/dev/null | wc -l | tr -d ' '; }   # 保護領域の数

# ── scratch リポジトリ ──
# 準備・観測側の git も fsmonitor / フックを起動しない(痕跡の検査が観測側の発火で汚れないため)
GIT() { local d="$1"; shift; git -c user.email=t@t -c user.name=t -c init.defaultBranch=main -c core.fsmonitor= -c core.hooksPath=/dev/null -C "$d" "$@"; }
# index を書き直さない観測(status は日和見的に index を書き直すので、観測自体が結果を汚す)
status_of() { GIT_OPTIONAL_LOCKS=0 GIT "$1" status --porcelain=v1 -uall; }
R=""
mkrepo() { # $1=$WORK 配下の名前 残り=git init の追加引数 → R
  local n="$1"
  shift
  R="$WORK/$n"
  rm -rf -- "$R"
  mkdir -p -- "$R"
  git -c init.defaultBranch=main init -q "$@" -- "$R"
}
TASK_BODY='# タスク

## 実装
- [ ] one
  * [ ] two
+ [x] three

## 完了条件
- [ ] テストが通る
'
write_task() { printf '%s' "$TASK_BODY" >"$1"; }
base_repo() { # $1=名前 残り=git init の追加引数 → seed commit つきの R(task/t.md は未追跡)
  mkrepo "$@"
  printf 'seed\n' >"$R/seed.txt"
  GIT "$R" add seed.txt
  GIT "$R" commit -q -m seed
  mkdir -p "$R/task"
  write_task "$R/task/t.md"
}

# ── take の結果を以後の起動へ渡す ──
STATE_DIR=""
MAN=""
SNAP=""
ARGS=()
take() { # $1=--cwd $2=--task-md
  run take --cwd "$1" --task-md "$2"
  STATE_DIR="$(sed -n 's/^STATE_DIR=//p' "$CASE_OUT")"
  MAN="$(sed -n 's/^MANIFEST_SHA256=//p' "$CASE_OUT")"
  SNAP="$(sed -n 's/^SNAPSHOT_SHA256=//p' "$CASE_OUT")"
  # take が失敗したときは、以後の起動が usage ではなく state-missing で落ちるようにしておく
  ARGS=(--cwd "$1" --state "${STATE_DIR:-$PROT/dev-workflow/guard-none}" --manifest-sha256 "${MAN:-0}" --snapshot-sha256 "${SNAP:-0}")
}
flip() { case "$1" in *0) printf '%s1' "${1%?}" ;; *) printf '%s0' "${1%?}" ;; esac; }   # 末尾 1 桁を変える

# ── スタブ置き場 ──
STUBS="$WORK/stubs"
TRACE="$WORK/trace.log"
GITLOG="$WORK/gitcalls.log"
: >"$TRACE"
: >"$GITLOG"
# 実行されたら痕跡を書く汎用スタブ(fsmonitor・diff.external・フックに使う)。stdin は読まない。
# 痕跡の置き場は環境変数ではなく埋め込みにする(環境が渡らずに「痕跡 0」になる偽陰性を避ける)
cat >"$STUBS/trace.sh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$0 \$*" >>"$TRACE"
exit 0
EOF
chmod +x "$STUBS/trace.sh"
# タスク MD の退避・復元のコピーだけを失敗させる cp(他のコピーは本物へ渡す)
mkdir -p "$STUBS/cpfail"
cat >"$STUBS/cpfail/cp" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  case "\$a" in taskmd-body|*/taskmd-body) exit 1 ;; esac
done
exec "$REAL_CP" "\$@"
EOF
chmod +x "$STUBS/cpfail/cp"
# git の呼び出しを記録してから本物へ渡すラッパ
mkdir -p "$STUBS/gitlog"
cat >"$STUBS/gitlog/git" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"$GITLOG"
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$STUBS/gitlog/git"
# status だけを失敗させる git(5 要素の再取得の途中で git が想定外に失敗する状況を作る)
mkdir -p "$STUBS/gitfail"
cat >"$STUBS/gitfail/git" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  case "\$a" in status) exit 128 ;; esac
done
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$STUBS/gitfail/git"
trace_n() { wc -l <"$TRACE" | tr -d ' '; }
gitlog_n() { wc -l <"$GITLOG" | tr -d ' '; }

# ════════════════════════ A 起動構文 ════════════════════════
case_A1() {
  CID=A1; TAG=""
  local n0
  n0="$(nstates)"
  run
  rc_is "サブコマンドなし: exit 2" 2
  err_has "サブコマンドなし: usage が stderr に出る" '使い方:'
  err_has "サブコマンドなし: usage にサブコマンドの一覧が出る" 'restore-taskmd'
  ckeq "サブコマンドなし: stdout は空" "$(size_of "$CASE_OUT")" 0
  run bogus
  rc_is "不明なサブコマンド: exit 2" 2
  err_has "不明なサブコマンド: usage が stderr に出る" '使い方:'
  ckeq "起動構文の誤りでは保護領域を作らない" "$(nstates)" "$n0"
}
case_A2() {
  CID=A2; TAG=""
  local n0
  n0="$(nstates)"
  run take --cwd "$WORK" --task-md x --bogus y
  rc_is "不明な引数: exit 2" 2
  err_has "不明な引数: usage が stderr に出る" '使い方:'
  run take --cwd "$WORK" --task-md x --state y
  rc_is "サブコマンドに無い引数(take に --state): exit 2" 2
  run take --cwd "$WORK"
  rc_is "必須の引数が無い(take に --task-md なし): exit 2" 2
  run compare --cwd "$WORK" --state x --manifest-sha256 a --snapshot-sha256 b
  rc_is "必須の引数が無い(compare に --run-rc なし): exit 2" 2
  run cleanup
  rc_is "必須の引数が無い(cleanup に --state なし): exit 2" 2
  ckeq "引数の誤りでは保護領域を作らない" "$(nstates)" "$n0"
}
case_A3() {
  CID=A3; TAG=""
  run take --cwd
  rc_is "値の欠落(--cwd が末尾): exit 2" 2
  err_has "値の欠落: usage が stderr に出る" '使い方:'
  run take --cwd "$WORK" --task-md
  rc_is "値の欠落(--task-md が末尾): exit 2" 2
  run take --cwd "" --task-md x
  rc_is "空の値(--cwd): exit 2" 2
  run cleanup --state ""
  rc_is "空の値(--state): exit 2" 2
}
case_A4() {
  CID=A4; TAG=""
  local v
  for v in abc -1 1.5 '' ' 1'; do
    run compare --cwd "$WORK" --state x --manifest-sha256 a --snapshot-sha256 b --run-rc "$v"
    rc_is "--run-rc が数でない('$v'): exit 2" 2
  done
  err_has "--run-rc が数でない: usage が stderr に出る" '使い方:'
}
case_A5() {
  CID=A5; TAG=""
  run --help
  rc_is "--help: exit 0" 0
  err_has "--help: usage が stderr に出る" '使い方:'
  err_has "--help: 終了コードの表が出る" '終了コード'
}

# ════════════════════════ B take の正常系 ════════════════════════
case_B1() { # 変異 (l) の検出先
  CID=B1; TAG=""
  local i0 i1 i2
  base_repo b1
  printf 'tracked\n' >"$R/a.txt"
  GIT "$R" add a.txt
  GIT "$R" commit -q -m a
  # 同内容で書き直して mtime だけ変える(stat だけ変わった追跡ファイル)
  printf 'tracked\n' >"$R/a.txt"
  touch -t 203101010000 "$R/a.txt"
  i0="$(sha_of "$R/.git/index")"
  take "$R" task/t.md
  rc_is "stat だけ変わった追跡ファイル: take が exit 0" 0
  i1="$(sha_of "$R/.git/index")"
  ckeq "stat だけ変わった追跡ファイル: take の前後で .git/index のバイト列が不変" "$i1" "$i0"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "stat だけ変わった追跡ファイル: 変化なし → 32" 32
  i2="$(sha_of "$R/.git/index")"
  ckeq "stat だけ変わった追跡ファイル: compare の前後で .git/index のバイト列が不変" "$i2" "$i0"
  ckeq "stat だけ変わった追跡ファイル: ① の差分は空のまま" "$(size_of "$STATE_DIR/snapshot/1-diff.bin")" 0
}
case_B2() {
  CID=B2; TAG=""
  base_repo b2
  mkdir -p "$R/pkg/.claude/reviews" "$R/pkg/src"
  printf 'top\n' >"$R/top-untracked.txt"
  write_task "$R/pkg/.claude/reviews/task.md"
  printf 'old log\n' >"$R/pkg/.claude/reviews/run1.md"
  take "$R/pkg" .claude/reviews/task.md
  rc_is "--cwd が toplevel の配下: take が exit 0" 0
  ckt "--cwd が toplevel の配下: 除外の錨が <--cwd>/.claude/reviews になる" inx "$STATE_DIR/snapshot/4-meta.txt" "exclude${TAB}pkg/.claude/reviews"
  ckt "--cwd が toplevel の配下: toplevel を記録する" inx "$STATE_DIR/snapshot/4-meta.txt" "toplevel${TAB}$R"
  ckt "--cwd が toplevel の配下: toplevel 全体が対象(--cwd の外の未追跡も載る)" ine "$STATE_DIR/manifest.tsv" "^f${TAB}[0-7]+${TAB}[0-9a-f]{64}${TAB}top-untracked\\.txt\$"
  ckt "--cwd が toplevel の配下: 同じ配下のタスク MD は載る" ine "$STATE_DIR/manifest.tsv" "${TAB}pkg/\\.claude/reviews/task\\.md\$"
  ckf "--cwd が toplevel の配下: 同じ配下のログは載らない" inf "$STATE_DIR/manifest.tsv" 'run1.md'
  printf 'log\n' >"$R/pkg/.claude/reviews/run2.md"
  printf 'more\n' >>"$R/pkg/.claude/reviews/run1.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "<--cwd>/.claude/reviews/ にログだけを足して rc 1 → 32" 32
  out_line "<--cwd>/.claude/reviews/ のログだけ: WORKTREE_CHANGED=no" 'WORKTREE_CHANGED=no'
  printf -- '- [ ] 別の要件\n' >>"$R/pkg/.claude/reviews/task.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "同じ配下のタスク MD は引き続き比較対象(本文の変更 → 31)" 31
  out_line "同じ配下のタスク MD の変更が CHANGE= に出る" "CHANGE=untracked-changed${TAB}pkg/.claude/reviews/task.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "同じ配下のタスク MD の本文の変更 → taskmd-diff が 34" 34
  write_task "$R/pkg/.claude/reviews/task.md"
  mkdir -p "$R/.claude/reviews"
  printf 'x\n' >"$R/.claude/reviews/y.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "toplevel 直下の .claude/reviews は除外しない(--cwd の錨で外す)→ 31" 31
  out_line "toplevel 直下の .claude/reviews の追加が CHANGE= に出る" "CHANGE=untracked-added${TAB}.claude/reviews/y.md"

  # --cwd の相対パスに glob 文字が入っても、除外は字面どおりに効く
  base_repo b2g
  mkdir -p "$R/pkg[1]*/.claude/reviews" "$R/pkg1/.claude/reviews"
  take "$R/pkg[1]*" "$R/task/t.md"
  rc_is "--cwd に glob 文字([ ] *): take が exit 0" 0
  printf 'log\n' >"$R/pkg[1]*/.claude/reviews/run.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "--cwd に glob 文字: <--cwd>/.claude/reviews/ のログだけ → 32" 32
  printf 'x\n' >"$R/pkg1/.claude/reviews/not-excluded.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "--cwd に glob 文字: glob として一致するだけの別ディレクトリは除外しない → 31" 31
  out_line "--cwd に glob 文字: 別ディレクトリの追加が CHANGE= に出る" "CHANGE=untracked-added${TAB}pkg1/.claude/reviews/not-excluded.md"
}
case_B3() { # 変異 (c) の検出先
  CID=B3; TAG=""
  base_repo b3
  printf 'tracked\n' >"$R/a.txt"
  GIT "$R" add a.txt
  GIT "$R" commit -q -m a
  printf 'modified\n' >"$R/a.txt"      # diff.external が呼ばれうるように追跡の変更を置く
  GIT "$R" config core.fsmonitor "$STUBS/trace.sh"
  GIT "$R" config diff.external "$STUBS/trace.sh"
  mkdir -p "$R/.git/hooks"
  cp "$STUBS/trace.sh" "$R/.git/hooks/post-index-change"
  : >"$TRACE"
  take "$R" task/t.md
  rc_is "定常状態(仕込みは take の前): take が exit 0" 0
  ckeq "定常状態: take は設定されたプログラムを実行しない(痕跡 0)" "$(trace_n)" 0
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "定常状態: config・hooks は不変なので compare は git を打って exit 0" 0
  out_line "定常状態: RESULT=normal" 'RESULT=normal'
  ckeq "定常状態: compare は設定されたプログラムを実行しない(痕跡 0)" "$(trace_n)" 0
  # 対照: 前置きなしの git なら痕跡スタブは動く(「痕跡 0」が空振りでないこと)
  git -C "$R" status --porcelain >/dev/null 2>&1 || true
  ckne "定常状態(対照): 前置きなしの git status では痕跡スタブが動く" "$(trace_n)" 0
  : >"$TRACE"
}
case_B4() {
  CID=B4; TAG=""
  local s0 s1 m
  base_repo b4
  GIT "$R" add task/t.md
  GIT "$R" commit -q -m task
  s0="$(status_of "$R")"
  take "$R" "$R/task/t.md"      # --task-md は絶対パスでも受ける
  rc_is "clean: take が exit 0" 0
  ckeq "clean: stdout は 7 キー" "$(cut -d= -f1 <"$CASE_OUT" | LC_ALL=C sort | tr '\n' ' ')" \
    'BYTES ENTRIES MANIFEST_SHA256 SNAPSHOT_SHA256 STATE_DIR TASKMD_REAL UNREADABLE '
  out_re "clean: MANIFEST_SHA256 は 16 進 64 桁" '^MANIFEST_SHA256=[0-9a-f]{64}$'
  out_re "clean: SNAPSHOT_SHA256 は 16 進 64 桁" '^SNAPSHOT_SHA256=[0-9a-f]{64}$'
  out_line "clean: ENTRIES=0" 'ENTRIES=0'
  out_line "clean: BYTES=0" 'BYTES=0'
  out_line "clean: UNREADABLE=0" 'UNREADABLE=0'
  out_line "clean: TASKMD_REAL が実体の絶対パス" "TASKMD_REAL=$R/task/t.md"
  case "$STATE_DIR" in
    "$PROT_REAL"/dev-workflow/guard-?*) ok "clean: STATE_DIR が保護領域の dev-workflow/guard-* に在る(/tmp の外)" ;;
    *) ng "clean: STATE_DIR が保護領域の dev-workflow/guard-* に在る(実際: $STATE_DIR)" ;;
  esac
  m="$(mode_of "$STATE_DIR" 2>/dev/null || true)"
  ckeq "clean: STATE_DIR が 0700" "$m" 700
  ckeq "clean: マニフェストは T 行だけ" "$(wc -l <"$STATE_DIR/manifest.tsv" | tr -d ' ')" 1
  ckeq "clean: T 行の値はタスク MD の sha256・パスは実体" "$(cut -f1,3,4 <"$STATE_DIR/manifest.tsv")" \
    "T${TAB}$(sha_of "$R/task/t.md")${TAB}$R/task/t.md"
  ckt "clean: taskmd-body が実体と同じ内容" cmp -s "$STATE_DIR/taskmd-body" "$R/task/t.md"
  ckeq "clean: MANIFEST_SHA256 は manifest.tsv の sha256" "$MAN" "$(sha_of "$STATE_DIR/manifest.tsv")"
  ckeq "clean: 保護領域の直下のレイアウト(作業用の一時領域を残さない)" "$(ls -A "$STATE_DIR" | LC_ALL=C sort | tr '\n' ' ')" \
    'files manifest.tsv snapshot taskmd-body '
  ckeq "clean: snapshot のレイアウト" "$(ls "$STATE_DIR/snapshot" | LC_ALL=C sort | tr '\n' ' ')" \
    '1-diff.bin 2-status.z 4-meta.txt 5-stash.txt taskmd.txt '
  ckt "clean: 4-meta.txt に head・index-tree・branch・hooks・config が在る" \
    ine "$STATE_DIR/snapshot/4-meta.txt" "^branch${TAB}refs/heads/main\$"
  ckt "clean: 4-meta.txt に refs 全体が在る" ine "$STATE_DIR/snapshot/4-meta.txt" "^ref${TAB}[0-9a-f]{40} refs/heads/main\$"
  ckt "clean: 4-meta.txt に config のダイジェストが在る" ine "$STATE_DIR/snapshot/4-meta.txt" "^config-digest${TAB}[0-9a-f]{64}\$"
  ckt "clean: 4-meta.txt に config.worktree(無し)が在る" inx "$STATE_DIR/snapshot/4-meta.txt" "config-worktree-digest${TAB}(無し)"
  ckt "clean: 4-meta.txt に hooks のダイジェストが在る" ine "$STATE_DIR/snapshot/4-meta.txt" "^hooks-digest${TAB}[0-9a-f]{64}\$"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "clean: compare が exit 0" 0
  s1="$(status_of "$R")"
  ckeq "clean: git status が take・compare の前後で不変" "$s1" "$s0"
  ckeq "clean: git status は空のまま" "$s1" ""
  # --cwd は相対パスでも受ける(呼び出し側の cwd が管理ルートのとき)
  RC=0
  ( cd "$R" && guard bash "$TARGET" take --cwd . --task-md task/t.md </dev/null >"$CASE_OUT" 2>"$CASE_ERR" ) || RC=$?
  mark_timeout
  show take --cwd . --task-md task/t.md
  rc_is "clean: --cwd が相対パス(.)でも take が exit 0" 0
  out_line "clean: --cwd が相対パスでも TASKMD_REAL は絶対パス" "TASKMD_REAL=$R/task/t.md"
  STATE_DIR="$(sed -n 's/^STATE_DIR=//p' "$CASE_OUT")"
  MAN="$(sed -n 's/^MANIFEST_SHA256=//p' "$CASE_OUT")"
  SNAP="$(sed -n 's/^SNAPSHOT_SHA256=//p' "$CASE_OUT")"
  RC=0
  ( cd "$R/task" && guard bash "$TARGET" compare --cwd .. --state "${STATE_DIR:-$PROT/dev-workflow/guard-none}" \
      --manifest-sha256 "${MAN:-0}" --snapshot-sha256 "${SNAP:-0}" --run-rc 1 </dev/null >"$CASE_OUT" 2>"$CASE_ERR" ) || RC=$?
  mark_timeout
  show compare --cwd ..
  rc_is "clean: --cwd が相対パス(..)でも compare は同じ管理ルートとして扱う → 32" 32
}
case_B5() { # 変異 (d) の検出先
  CID=B5; TAG=""
  local i0 i1 i2 i3 s0 s1 c0 r0
  base_repo b5
  printf 'tracked\n' >"$R/a.txt"
  GIT "$R" add a.txt
  GIT "$R" commit -q -m a
  printf 'modified\n' >"$R/a.txt"              # 追跡の変更(未 stage)
  printf 'staged new\n' >"$R/staged.txt"
  GIT "$R" add staged.txt                      # stage 済みの新規(cache-tree が無効な index になる)
  printf 'untracked\n' >"$R/u.txt"             # 未追跡
  chmod 644 "$R/u.txt"
  s0="$(status_of "$R")"
  i0="$(sha_of "$R/.git/index")"
  c0="$(sha_of "$R/.git/config")"
  r0="$(GIT "$R" for-each-ref | sha_of /dev/stdin)"
  take "$R" task/t.md
  rc_is "dirty: take が exit 0" 0
  i1="$(sha_of "$R/.git/index")"
  ckeq "dirty: index 不変 — take の前後で .git/index のバイト列が同じ" "$i1" "$i0"
  out_line "dirty: ENTRIES は未追跡の件数" 'ENTRIES=2'
  out_line "dirty: BYTES は未追跡の通常ファイルの総バイト数" "BYTES=$(( $(size_of "$R/u.txt") + $(size_of "$R/task/t.md") ))"
  ckt "dirty: マニフェストに未追跡の f 行(モード・退避コピー側の sha256)" \
    inx "$STATE_DIR/manifest.tsv" "f${TAB}644${TAB}$(sha_of "$R/u.txt")${TAB}u.txt"
  ckt "dirty: 退避コピーの内容が一致(u.txt)" cmp -s "$STATE_DIR/files/u.txt" "$R/u.txt"
  ckt "dirty: 退避コピーの内容が一致(task/t.md)" cmp -s "$STATE_DIR/files/task/t.md" "$R/task/t.md"
  ckf "dirty: 追跡ファイルは退避しない" test -e "$STATE_DIR/files/a.txt"
  ckt "dirty: ① に追跡の変更が入る" inf "$STATE_DIR/snapshot/1-diff.bin" '+modified'
  ckt "dirty: ① に stage 済みの新規が入る" inf "$STATE_DIR/snapshot/1-diff.bin" '+staged new'
  ckeq "dirty: ② に 3 種の状態が入る" "$(tr '\0' '\n' <"$STATE_DIR/snapshot/2-status.z" | LC_ALL=C sort | tr '\n' '|')" \
    ' M a.txt|?? task/t.md|?? u.txt|A  staged.txt|'
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "dirty: 変化なし → 32" 32
  i2="$(sha_of "$R/.git/index")"
  ckeq "dirty: index 不変 — compare の前後で .git/index のバイト列が同じ" "$i2" "$i0"
  run taskmd-diff "${ARGS[@]}"
  rc_is "dirty: taskmd-diff が exit 0" 0
  i3="$(sha_of "$R/.git/index")"
  ckeq "dirty: index 不変 — taskmd-diff の前後で .git/index のバイト列が同じ" "$i3" "$i0"
  s1="$(status_of "$R")"
  ckeq "dirty: git status が不変" "$s1" "$s0"
  ckeq "dirty: .git/config が不変" "$(sha_of "$R/.git/config")" "$c0"
  ckeq "dirty: refs が不変" "$(GIT "$R" for-each-ref | sha_of /dev/stdin)" "$r0"
}
case_B6() {
  CID=B6; TAG="[類型:日本語パス] "
  base_repo b6
  rm -rf -- "$R/task"
  mkdir -p "$R/資料" "$R/タスク"
  write_task "$R/タスク/進行中_日本語.md"
  printf 'メモ\n' >"$R/資料/メモ.txt"
  take "$R" 'タスク/進行中_日本語.md'
  rc_is "take が exit 0" 0
  out_line "TASKMD_REAL が日本語のまま出る" "TASKMD_REAL=$R/タスク/進行中_日本語.md"
  ckt "マニフェストに日本語のパスが生のまま載る(8 進エスケープにしない)" ine "$STATE_DIR/manifest.tsv" "^f${TAB}[0-7]+${TAB}[0-9a-f]{64}${TAB}資料/メモ\\.txt\$"
  ckt "退避コピーの内容が一致" cmp -s "$STATE_DIR/files/資料/メモ.txt" "$R/資料/メモ.txt"
  printf '書き換え\n' >"$R/資料/メモ.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "日本語パスの内容変更 → 31" 31
  out_line "CHANGE= に日本語のパスが出る" "CHANGE=untracked-changed${TAB}資料/メモ.txt"
  out_line "BASE= が退避コピーを指す(ok)" "BASE=$STATE_DIR/files/資料/メモ.txt${TAB}ok"
  run taskmd-diff "${ARGS[@]}"
  rc_is "日本語パスのタスク MD: taskmd-diff が exit 0" 0
  out_line "日本語パスのタスク MD: TASKMD=same" 'TASKMD=same'
}
case_B7() {
  CID=B7; TAG="[類型:空白入りパス] "
  local ctl tabf
  ctl="$(printf 'ct\001rl.txt')"
  tabf="$(printf 'tab\there.txt')"
  base_repo b7
  rm -rf -- "$R/task"
  mkdir -p "$R/sp ace dir" "$R/my task"
  write_task "$R/my task/進行中 タスク.md"
  printf 's\n' >"$R/sp ace dir/file name.txt"
  printf 'd\n' >"$R/-dash.txt"
  printf 'c\n' >"$R/$ctl"
  printf 't\n' >"$R/$tabf"
  printf 'q\n' >"$R/q\"uo\\te.txt"
  take "$R" 'my task/進行中 タスク.md'
  rc_is "take が exit 0" 0
  out_line "ENTRIES=6" 'ENTRIES=6'
  ckt "引用符とバックスラッシュのパスは C 風引用で載る" inf "$STATE_DIR/manifest.tsv" "${TAB}\"q\\\"uo\\\\te.txt\""
  ckt "退避コピーの内容が一致(引用符とバックスラッシュ)" cmp -s "$STATE_DIR/files/q\"uo\\te.txt" "$R/q\"uo\\te.txt"
  ckt "空白入りのパスは生のまま載る" ine "$STATE_DIR/manifest.tsv" "${TAB}sp ace dir/file name\\.txt\$"
  ckt "先頭ハイフンのパスは生のまま載る" ine "$STATE_DIR/manifest.tsv" "${TAB}-dash\\.txt\$"
  ckt "制御文字のパスは C 風引用で 1 行に載る" inf "$STATE_DIR/manifest.tsv" "${TAB}\"ct\\001rl.txt\""
  ckt "タブのパスは C 風引用で 1 行に載る" inf "$STATE_DIR/manifest.tsv" "${TAB}\"tab\\there.txt\""
  ckt "退避コピーの内容が一致(空白入り)" cmp -s "$STATE_DIR/files/sp ace dir/file name.txt" "$R/sp ace dir/file name.txt"
  ckt "退避コピーの内容が一致(先頭ハイフン)" cmp -s "$STATE_DIR/files/-dash.txt" "$R/-dash.txt"
  ckt "退避コピーの内容が一致(制御文字)" cmp -s "$STATE_DIR/files/$ctl" "$R/$ctl"
  ckt "退避コピーの内容が一致(タスク MD)" cmp -s "$STATE_DIR/taskmd-body" "$R/my task/進行中 タスク.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "変化なし → 32" 32
  printf 's2\n' >"$R/sp ace dir/file name.txt"
  printf 'c2\n' >"$R/$ctl"
  printf 't2\n' >"$R/$tabf"
  rm -f -- "$R/-dash.txt"
  printf 'q2\n' >"$R/q\"uo\\te.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "特殊なパスの変更と削除 → 31" 31
  out_line "引用符とバックスラッシュのパスの変更が C 風引用で出る" "CHANGE=untracked-changed${TAB}\"q\\\"uo\\\\te.txt\""
  out_line "引用符とバックスラッシュのパスの BASE= も C 風引用で 1 行" "BASE=\"$STATE_DIR/files/q\\\"uo\\\\te.txt\"${TAB}ok"
  out_line "空白入りのパスの変更が出る" "CHANGE=untracked-changed${TAB}sp ace dir/file name.txt"
  out_line "制御文字のパスの変更が C 風引用で出る" "CHANGE=untracked-changed${TAB}\"ct\\001rl.txt\""
  out_line "タブのパスの変更が C 風引用で出る" "CHANGE=untracked-changed${TAB}\"tab\\there.txt\""
  out_line "先頭ハイフンのパスの削除が出る" "CHANGE=untracked-removed${TAB}-dash.txt"
  out_line "先頭ハイフンのパスの BASE= が ok" "BASE=$STATE_DIR/files/-dash.txt${TAB}ok"
}
case_B8() {
  CID=B8; TAG=""
  base_repo b8
  printf 'outside secret\n' >"$WORK/b8-outside.txt"
  ln -s "$WORK/b8-outside.txt" "$R/out-link"
  ln -s nowhere "$R/broken-link"
  git -c init.defaultBranch=main init -q -- "$R/nested"
  printf 'n\n' >"$R/nested/x.txt"
  GIT "$R/nested" add x.txt
  GIT "$R/nested" commit -q -m nested
  take "$R" task/t.md
  rc_is "未追跡の symlink: take が exit 0" 0
  ckt "未追跡の symlink: l 行にリンク文字列を記録する" ine "$STATE_DIR/manifest.tsv" "^l${TAB}[0-7]+${TAB}$WORK/b8-outside\\.txt${TAB}out-link\$"
  ckt "未追跡の symlink: リンク切れも l 行" ine "$STATE_DIR/manifest.tsv" "^l${TAB}[0-7]+${TAB}nowhere${TAB}broken-link\$"
  ckt "未追跡の symlink: 退避コピーは symlink のまま(辿らない)" test -L "$STATE_DIR/files/out-link"
  ckeq "未追跡の symlink: 退避コピーのリンク文字列が同じ" "$(readlink "$STATE_DIR/files/out-link" 2>/dev/null || true)" "$WORK/b8-outside.txt"
  out_line "未追跡の symlink: リンク先の大きさを BYTES に数えない" "BYTES=$(size_of "$R/task/t.md")"
  out_line "未追跡の symlink: UNREADABLE=0" 'UNREADABLE=0'
  ckt "ネスト repo のディレクトリは o 行(値は -)" ine "$STATE_DIR/manifest.tsv" "^o${TAB}[0-7]+${TAB}-${TAB}nested/\$"
  printf 'changed outside\n' >>"$WORK/b8-outside.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "未追跡の symlink: リンク先の中身だけの変化は変化にしない(辿らない)→ 32" 32
}
case_B9() {
  CID=B9; TAG="[類型:stash] "
  local n0
  base_repo b9
  printf 'tracked\n' >"$R/a.txt"
  GIT "$R" add a.txt
  GIT "$R" commit -q -m a
  take "$R" task/t.md
  rc_is "stash 無し: take が exit 0" 0
  ckeq "stash 無し: ⑤ は固定の 1 行" "$(cat "$STATE_DIR/snapshot/5-stash.txt")" 'stash 無し'
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "stash 無し: 変化なし → 32" 32
  printf 'wip\n' >>"$R/a.txt"
  GIT "$R" stash push -q
  n0="$(GIT "$R" stash list | wc -l | tr -d ' ')"
  take "$R" task/t.md
  rc_is "stash あり: take が exit 0" 0
  ckt "stash あり: ⑤ は固定の書式(%H %gd %gs)" ine "$STATE_DIR/snapshot/5-stash.txt" '^[0-9a-f]{40} stash@\{0\} '
  ckeq "stash あり: ⑤ の行数は stash の件数" "$(wc -l <"$STATE_DIR/snapshot/5-stash.txt" | tr -d ' ')" 1
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "stash あり: 変化なし → 32" 32
  ckeq "stash あり: take・compare は stash を増減させない" "$(GIT "$R" stash list | wc -l | tr -d ' ')" "$n0"
}
b10_check() { # $1=説明 $2=--task-md $3=実体の絶対パス $4=CHANGE= に出るパス $5=BASE= の退避コピー(STATE_DIR 相対)[$6=compare の stdout に在るはずの ERE]
  take "$R" "$2"
  rc_is "タスク MD($1): take が exit 0" 0
  out_line "タスク MD($1): TASKMD_REAL が実体を指す" "TASKMD_REAL=$3"
  ckeq "タスク MD($1): マニフェストの先頭行が T 行(パスは実体)" "$(head -n 1 "$STATE_DIR/manifest.tsv" 2>/dev/null | cut -f1,4)" "T${TAB}$3"
  ckt "タスク MD($1): taskmd-body が実体と同じ内容" cmp -s "$STATE_DIR/taskmd-body" "$3"
  printf -- '- [ ] 足された要件\n' >>"$3"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "タスク MD($1): 本文の変更を compare が検出する → 31" 31
  out_line "タスク MD($1): CHANGE= に出る" "CHANGE=untracked-changed${TAB}$4"
  out_line "タスク MD($1): BASE= が ok" "BASE=$STATE_DIR/$5${TAB}ok"
  if [ -n "${6:-}" ]; then out_re "タスク MD($1): compare の stdout に $6" "$6"; fi
  run taskmd-diff "${ARGS[@]}"
  rc_is "タスク MD($1): taskmd-diff が 34" 34
}
case_B10() {
  CID=B10; TAG=""
  base_repo b10a
  GIT "$R" add task/t.md
  GIT "$R" commit -q -m task
  b10_check "追跡済み" task/t.md "$R/task/t.md" "$R/task/t.md" taskmd-body "^CHANGE=tracked-diff${TAB}"

  base_repo b10b
  printf 'task/\n' >"$R/.gitignore"
  GIT "$R" add .gitignore
  GIT "$R" commit -q -m ignore
  b10_check "ignore 済み" task/t.md "$R/task/t.md" "$R/task/t.md" taskmd-body
  ckeq "タスク MD(ignore 済み): マニフェストは T 行だけ(f 行に載らない)" "$(wc -l <"$STATE_DIR/manifest.tsv" | tr -d ' ')" 1

  base_repo b10c
  b10_check "未追跡" task/t.md "$R/task/t.md" task/t.md files/task/t.md

  base_repo b10d
  rm -rf -- "$R/task"
  mkdir -p "$R/.claude/reviews"
  write_task "$R/.claude/reviews/task.md"
  printf 'log\n' >"$R/.claude/reviews/run.md"
  b10_check ".claude/reviews/ 配下" .claude/reviews/task.md "$R/.claude/reviews/task.md" .claude/reviews/task.md files/.claude/reviews/task.md
  ckf "タスク MD(.claude/reviews/ 配下): 同じ場所のログは載らない" inf "$STATE_DIR/manifest.tsv" 'run.md'

  base_repo b10e
  printf 'notes/\n' >"$R/.gitignore"
  GIT "$R" add .gitignore
  GIT "$R" commit -q -m ignore
  mkdir -p "$R/notes"
  write_task "$R/notes/real.md"
  rm -f -- "$R/task/t.md"
  ln -s ../notes/real.md "$R/task/link.md"
  b10_check "symlink(実体は ignore 済みの通常ファイル)" task/link.md "$R/notes/real.md" "$R/notes/real.md" taskmd-body
  ckt "タスク MD(symlink): 選択パス・実体パス・種別を記録する" inx "$STATE_DIR/snapshot/taskmd.txt" "kind${TAB}symlink"
  ckt "タスク MD(symlink): 選択パスは symlink の側" inx "$STATE_DIR/snapshot/taskmd.txt" "selected${TAB}$R/task/link.md"
  ckt "タスク MD(symlink): 未追跡の symlink 自体は l 行" ine "$STATE_DIR/manifest.tsv" "^l${TAB}[0-7]+${TAB}\\.\\./notes/real\\.md${TAB}task/link\\.md\$"

  base_repo b10f
  mkdir -p "$WORK/b10f-outside"
  write_task "$WORK/b10f-outside/task.md"
  b10_check "リポジトリの外" "$WORK/b10f-outside/task.md" "$WORK/b10f-outside/task.md" "$WORK/b10f-outside/task.md" taskmd-body
  run restore-taskmd "${ARGS[@]}"
  rc_is "タスク MD(リポジトリの外): restore-taskmd で戻る" 0
  ckeq "タスク MD(リポジトリの外): 起動前の内容に戻る" "$(cat "$WORK/b10f-outside/task.md")" "$(printf '%s' "$TASK_BODY")"
}

case_B11() {
  CID=B11; TAG=""
  local fh="$PROT/b11-home" fh2="$PROT/b11-home2" sd
  base_repo b11
  mkdir -p "$fh" "$fh2/.local" "$WORK/b11-tmpstate"
  # 候補 2: XDG_STATE_HOME が無ければ $HOME/.local/state(selftest 用の保護領域の中に偽の HOME を置く)
  rune -u XDG_STATE_HOME HOME="$fh" -- take --cwd "$R" --task-md task/t.md
  rc_is "保護領域の解決順: XDG_STATE_HOME が無い → take が exit 0" 0
  sd="$(sed -n 's/^STATE_DIR=//p' "$CASE_OUT")"
  case "$sd" in
    "$PROT_REAL"/b11-home/.local/state/dev-workflow/guard-?*) ok "保護領域の解決順: \$HOME/.local/state/dev-workflow/ に作る" ;;
    *) ng "保護領域の解決順: \$HOME/.local/state/dev-workflow/ に作る(実際: $sd)" ;;
  esac
  # 候補 1 が /tmp 配下なら次の候補へ進む
  rune XDG_STATE_HOME="$WORK/b11-xdg" HOME="$fh" -- take --cwd "$R" --task-md task/t.md
  rc_is "保護領域の解決順: XDG_STATE_HOME が /tmp 配下 → 次の候補で exit 0" 0
  sd="$(sed -n 's/^STATE_DIR=//p' "$CASE_OUT")"
  case "$sd" in
    "$PROT_REAL"/b11-home/.local/state/dev-workflow/guard-?*) ok "保護領域の解決順: /tmp 配下の候補を飛ばして \$HOME/.local/state に作る" ;;
    *) ng "保護領域の解決順: /tmp 配下の候補を飛ばして \$HOME/.local/state に作る(実際: $sd)" ;;
  esac
  ckeq "保護領域の解決順: 飛ばした /tmp 配下の候補に guard-* を作らない" \
    "$(find "$WORK/b11-xdg" -name 'guard-*' 2>/dev/null | wc -l | tr -d ' ')" 0
  # 候補 2 の実体が /tmp 配下(symlink)なら、名前ではなく実体パスで判定して候補 3 へ進む
  ln -s "$WORK/b11-tmpstate" "$fh2/.local/state"
  rune -u XDG_STATE_HOME HOME="$fh2" -- take --cwd "$R" --task-md task/t.md
  rc_is "保護領域の解決順: \$HOME/.local/state の実体が /tmp 配下 → \$HOME/.cache で exit 0" 0
  sd="$(sed -n 's/^STATE_DIR=//p' "$CASE_OUT")"
  case "$sd" in
    "$PROT_REAL"/b11-home2/.cache/dev-workflow/guard-?*) ok "保護領域の解決順: 実体パスで判定して \$HOME/.cache/dev-workflow/ に作る" ;;
    *) ng "保護領域の解決順: 実体パスで判定して \$HOME/.cache/dev-workflow/ に作る(実際: $sd)" ;;
  esac
  ckeq "保護領域の解決順: symlink の先(/tmp 配下)に guard-* を作らない" \
    "$(find "$WORK/b11-tmpstate" -name 'guard-*' 2>/dev/null | wc -l | tr -d ' ')" 0
  MAN="$(sed -n 's/^MANIFEST_SHA256=//p' "$CASE_OUT")"
  SNAP="$(sed -n 's/^SNAPSHOT_SHA256=//p' "$CASE_OUT")"
  rune -u XDG_STATE_HOME HOME="$fh2" -- compare --cwd "$R" --state "${sd:-$PROT/dev-workflow/guard-none}" \
    --manifest-sha256 "${MAN:-0}" --snapshot-sha256 "${SNAP:-0}" --run-rc 1
  rc_is "保護領域の解決順: 同じ環境の compare は \$HOME/.cache の保護領域を受ける → 32" 32
  rune -u XDG_STATE_HOME HOME="$fh2" -- cleanup --state "${sd:-$PROT/dev-workflow/guard-none}"
  rc_is "保護領域の解決順: 同じ環境の cleanup で消える" 0
}

case_B12() {
  CID=B12; TAG=""
  local nl=$'\n' real decoy
  base_repo b12
  # 名前が literal な改行で終わるタスク MD と、改行の無い同名ファイルを並べる。
  # パスをコマンド置換で受け渡すと末尾の改行が落ちて「別のファイル」を指す
  real="$R/task/t.md$nl"
  decoy="$R/task/t.md"
  write_task "$real"
  printf 'DECOY\n\n- [ ] これは別のファイル\n' >"$decoy"
  take "$R" "task/t.md$nl"
  rc_is "名前が改行で終わるタスク MD: take が exit 0" 0
  out_line "名前が改行で終わるタスク MD: TASKMD_REAL が C 風引用の 1 行で出る" "TASKMD_REAL=\"$R/task/t.md\\n\""
  ckt "名前が改行で終わるタスク MD: 退避したのは指定した方(改行つき)" cmp -s "$STATE_DIR/taskmd-body" "$real"
  ckf "名前が改行で終わるタスク MD: 改行の無い同名ファイルを退避していない" cmp -s "$STATE_DIR/taskmd-body" "$decoy"
  ckeq "名前が改行で終わるタスク MD: マニフェストの T 行は引用した実体パス" \
    "$(head -n 1 "$STATE_DIR/manifest.tsv" 2>/dev/null | cut -f1,4)" "T${TAB}\"$R/task/t.md\\n\""
  out_line "名前が改行で終わるタスク MD: 未追跡は 2 件(改行つきと改行なし)" 'ENTRIES=2'
  # 改行の無い方だけを書き換えても、タスク MD は変わっていない
  printf 'DECOY 2\n\n- [x] これは別のファイル\n' >"$decoy"
  run taskmd-diff "${ARGS[@]}"
  rc_is "名前が改行で終わるタスク MD: 改行の無い方だけの変更では taskmd-diff が 0" 0
  out_line "名前が改行で終わるタスク MD: TASKMD=same(別のファイルを見ていない)" 'TASKMD=same'
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "名前が改行で終わるタスク MD: 改行の無い方の変更も未追跡の変化として出る → 31" 31
  out_line "名前が改行で終わるタスク MD: 改行の無い方は引用なしのパスで出る" "CHANGE=untracked-changed${TAB}task/t.md"
  # 指定した方(改行つき)を書き換える
  printf '%s' "$TASK_BODY" | sed 's/テストが通る/テストを書く/' >"$real"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "名前が改行で終わるタスク MD: 指定した方の変更 → 31" 31
  out_line "名前が改行で終わるタスク MD: 指定した方は C 風引用の 1 行で出る" "CHANGE=untracked-changed${TAB}\"task/t.md\\n\""
  out_line "名前が改行で終わるタスク MD: BASE= も C 風引用の 1 行" "BASE=\"$STATE_DIR/files/task/t.md\\n\"${TAB}ok"
  ckeq "名前が改行で終わるタスク MD: stdout の全行が KEY=VALUE の形" "$(grep -c -v -E '^[A-Z_]+=' -- "$CASE_OUT" || true)" 0
  run taskmd-diff "${ARGS[@]}"
  rc_is "名前が改行で終わるタスク MD: 指定した方の本文の変更 → 34" 34
  run restore-taskmd "${ARGS[@]}"
  restored_ok "名前が改行で終わるタスク MD" "$real"
  ckeq "名前が改行で終わるタスク MD: 改行の無い同名ファイルは無傷" "$(cat "$decoy")" "$(printf 'DECOY 2\n\n- [x] これは別のファイル')"
}

# ════════════════════════ C take の縮退 ════════════════════════
no_state_left() { # $1=説明 $2=起動前の保護領域の数
  ckeq "$1: 縮退後に保護領域が残らない" "$(nstates)" "$2"
  ckeq "$1: stdout に STATE_DIR を出さない" "$(out_count '^STATE_DIR=')" 0
}
case_C1() { # 変異 (k) の検出先
  CID=C1; TAG=""
  local n0
  base_repo c1
  printf 'u\n' >"$R/u.txt"
  n0="$(nstates)"
  runp "$STUBS/cpfail" take --cwd "$R" --task-md task/t.md
  rc_is "タスク MD の退避の失敗(cp が失敗): exit 30(u で続行しない)" 30
  err_has "タスク MD の退避の失敗: 理由コード taskmd" 'ERROR [taskmd]'
  no_state_left "タスク MD の退避の失敗" "$n0"
  # 対照: スタブが無ければ同じ repo で take は通る(30 の原因が cp の失敗であること)
  take "$R" task/t.md
  rc_is "タスク MD の退避の失敗(対照): スタブなしなら exit 0" 0
}
case_C2() {
  CID=C2; TAG=""
  local n0
  mkdir -p "$WORK/c2"
  write_task "$WORK/c2/t.md"
  n0="$(nstates)"
  run take --cwd "$WORK/c2" --task-md t.md
  rc_is "非 git: exit 30" 30
  err_has "非 git: 理由コード not-git" 'ERROR [not-git]'
  no_state_left "非 git" "$n0"
}
case_C3() {
  CID=C3; TAG=""
  local n0
  base_repo c3
  mkdir -p "$WORK/c3-other"
  GIT "$R" config core.worktree "$WORK/c3-other"
  n0="$(nstates)"
  run take --cwd "$R" --task-md task/t.md
  rc_is "toplevel が --cwd を含まない(core.worktree が別の場所): exit 30" 30
  err_has "toplevel が --cwd を含まない: 理由コード not-git" 'ERROR [not-git]'
  no_state_left "toplevel が --cwd を含まない" "$n0"
}
mk_conflict() { # R に main と other の衝突する commit を作る(merge はまだしない)
  printf 'base\n' >"$R/a.txt"
  GIT "$R" add a.txt
  GIT "$R" commit -q -m a
  GIT "$R" checkout -q -b other
  printf 'other\n' >"$R/a.txt"
  GIT "$R" commit -q -a -m other
  GIT "$R" checkout -q main
  printf 'main\n' >"$R/a.txt"
  GIT "$R" commit -q -a -m main
}
case_C4() {
  CID=C4; TAG=""
  local n0
  base_repo c4
  mk_conflict
  GIT "$R" merge other >/dev/null 2>&1 || true
  ckne "unmerged(前提): index に unmerged のエントリが在る" "$(GIT "$R" ls-files -u | wc -l | tr -d ' ')" 0
  n0="$(nstates)"
  run take --cwd "$R" --task-md task/t.md
  rc_is "unmerged: exit 30" 30
  err_has "unmerged: 理由コード unmerged" 'ERROR [unmerged]'
  no_state_left "unmerged" "$n0"
}
case_C5() {
  CID=C5; TAG=""
  local n0
  base_repo c5
  mkdir -p "$WORK/c5-home"
  n0="$(nstates)"
  rune HOME="$WORK/c5-home" XDG_STATE_HOME="$WORK/c5-state" -- take --cwd "$R" --task-md task/t.md
  rc_is "保護領域を解決できない(HOME・XDG_STATE_HOME が /tmp 配下): exit 30" 30
  err_has "保護領域を解決できない: 理由コード no-protected-area" 'ERROR [no-protected-area]'
  no_state_left "保護領域を解決できない" "$n0"
  ckeq "保護領域を解決できない: /tmp 配下の候補にも guard-* を作らない" \
    "$(find "$WORK/c5-home" "$WORK/c5-state" -name 'guard-*' 2>/dev/null | wc -l | tr -d ' ')" 0
  # toplevel の配下も候補にしない
  rune HOME="$R/home-in-repo" XDG_STATE_HOME="$R/state-in-repo" -- take --cwd "$R" --task-md task/t.md
  rc_is "保護領域を解決できない(候補が toplevel の配下): exit 30" 30
  err_has "保護領域を解決できない(toplevel の配下): 理由コード no-protected-area" 'ERROR [no-protected-area]'
  no_state_left "保護領域を解決できない(toplevel の配下)" "$n0"
  # 棄却した候補には何も作らない(作業ツリーの中に dev-workflow / guard-* を残さない)
  ckeq "棄却した候補(toplevel の配下)に dev-workflow も guard-* も作らない" \
    "$(find "$R" \( -name 'dev-workflow' -o -name 'guard-*' \) 2>/dev/null | wc -l | tr -d ' ')" 0
  ckf "棄却した候補(toplevel の配下)のディレクトリ自体も作らない(XDG_STATE_HOME)" test -e "$R/state-in-repo"
  ckf "棄却した候補(toplevel の配下)のディレクトリ自体も作らない(HOME)" test -e "$R/home-in-repo"
  ckeq "棄却した候補(toplevel の配下): 作業ツリーに未追跡ファイルが増えない" \
    "$(status_of "$R" | grep -c '^??' || true)" 1
}
case_C6() {
  CID=C6; TAG=""
  local n0 i
  base_repo c6
  GIT "$R" add task/t.md
  GIT "$R" commit -q -m task
  mkdir -p "$R/many"
  i=0
  while [ "$i" -lt 2001 ]; do : >"$R/many/f$i"; i=$((i + 1)); done
  n0="$(nstates)"
  run_slow take --cwd "$R" --task-md task/t.md
  rc_is "上限超過(件数 2001): exit 30" 30
  err_has "上限超過(件数): 理由コード over-limit" 'ERROR [over-limit]'
  no_state_left "上限超過(件数)" "$n0"
}
case_C7() { # 変異 (i) の検出先(件数の側は、上限検査を外した写しだと 2001 件の退避に数十秒かかるので使わない)
  CID=C7; TAG=""
  local n0
  base_repo c7
  dd if=/dev/zero of="$R/big.bin" bs=1 count=0 seek=209715201 2>/dev/null   # 200 MiB + 1 バイト(疎なファイル)
  ckeq "上限超過(前提): 200 MiB を 1 バイト超えるファイルが在る" "$(size_of "$R/big.bin")" 209715201
  n0="$(nstates)"
  run_slow take --cwd "$R" --task-md task/t.md
  rc_is "上限超過(サイズ 200 MiB 超): exit 30" 30
  err_has "上限超過(サイズ): 理由コード over-limit" 'ERROR [over-limit]'
  no_state_left "上限超過(サイズ)" "$n0"
}
case_C8() {
  CID=C8; TAG=""
  local n0 t
  base_repo c8
  ln -s none.md "$R/task/broken.md"
  ln -s loop.md "$R/task/loop.md"
  mkdir -p "$R/task/dir.md"
  n0="$(nstates)"
  for t in task/broken.md:リンク切れ task/loop.md:ループ task/dir.md:ディレクトリ task/nothing.md:存在しない nodir/t.md:親ディレクトリが無い; do
    run take --cwd "$R" --task-md "${t%%:*}"
    rc_is "タスク MD が${t#*:}: exit 30" 30
    err_has "タスク MD が${t#*:}: 理由コード taskmd" 'ERROR [taskmd]'
  done
  if [ "$IS_ROOT" -eq 1 ]; then
    ok "タスク MD が読めない: exit 30(root のため飛ばす — root は読めてしまう)"
  else
    chmod 000 "$R/task/t.md"
    run take --cwd "$R" --task-md task/t.md
    rc_is "タスク MD が読めない: exit 30" 30
    err_has "タスク MD が読めない: 理由コード taskmd" 'ERROR [taskmd]'
    chmod 644 "$R/task/t.md"
  fi
  no_state_left "タスク MD を確定できない" "$n0"
}

case_C9() { # 変異 (m) の検出先
  CID=C9; TAG=""
  local base sd n0
  # このケースの候補は **/tmp の外**に要る(/tmp 配下だと tmpdir の除外が先に効いて、
  # toplevel 配下かどうかの判定をすり抜けても分からない)。selftest 用の保護領域の下に作り、
  # ケースの終わりに必ず消す
  if [ -z "$PROT_REAL" ] || [ ! -d "$PROT_REAL" ]; then
    ok "未作成の中間パス + ..: 保護領域を使えないため飛ばす"
    return 0
  fi
  base="$PROT_REAL/c9"
  rm -rf -- "$base"
  mkdir -p "$base/home" || { ok "未作成の中間パス + ..: 作業場所を作れないため飛ばす"; return 0; }
  R="$base/repo"
  mkdir -p "$R"
  git -c init.defaultBranch=main init -q -- "$R"
  printf 'seed\n' >"$R/seed.txt"
  GIT "$R" add seed.txt
  GIT "$R" commit -q -m seed
  mkdir -p "$R/task"
  write_task "$R/task/t.md"
  n0="$(nstates)"
  # `<base>/new` は存在しない。`..` で toplevel の配下へ戻る綴りが、前置きの比較をすり抜けて
  # 作業ツリーの中へ mkdir しないこと(mkdir -p は打ち消される途中の要素まで作ってしまう)
  rune HOME="$base/home" XDG_STATE_HOME="$base/new/../repo" -- take --cwd "$R" --task-md task/t.md
  rc_is "未作成の中間パス + ..: 後続の候補に落ちて exit 0" 0
  ckf "未作成の中間パス + ..: 存在しない中間ディレクトリを作らない" test -e "$base/new"
  ckf "未作成の中間パス + ..: 作業ツリーの中に dev-workflow を作らない" test -e "$R/dev-workflow"
  ckeq "未作成の中間パス + ..: 作業ツリーに dev-workflow も guard-* も残らない" \
    "$(find "$R" \( -name 'dev-workflow' -o -name 'guard-*' \) 2>/dev/null | wc -l | tr -d ' ')" 0
  ckeq "未作成の中間パス + ..: 作業ツリーに未追跡ファイルが増えない" \
    "$(status_of "$R" | grep -c '^??' || true)" 1
  sd="$(sed -n 's/^STATE_DIR=//p' "$CASE_OUT")"
  case "$sd" in
    "$base"/home/.local/state/dev-workflow/guard-?*) ok "未作成の中間パス + ..: 保護領域は次の候補(\$HOME/.local/state)に作る" ;;
    *) ng "未作成の中間パス + ..: 保護領域は次の候補(\$HOME/.local/state)に作る(実際: $sd)" ;;
  esac
  ckeq "未作成の中間パス + ..: selftest 用の保護領域の直下は増えない" "$(nstates)" "$n0"
  rm -rf -- "$base"
  ckf "未作成の中間パス + ..: ケースの作業場所を消した" test -e "$base"
}

case_C10() { # 変異 (m) の検出先
  CID=C10; TAG=""
  local base sd xdg
  # C9 と対になるケース。C9 は「相殺しきれず toplevel の配下へ戻る → 棄却」、こちらは
  # 「相殺できて禁止領域の外に収まる → **採用**され、後続のサブコマンドも同じ綴りで通る」を見る。
  # 同じく **/tmp の外**に要るので、selftest 用の保護領域の下に作ってケースの終わりに消す
  if [ -z "$PROT_REAL" ] || [ ! -d "$PROT_REAL" ]; then
    ok "打ち消せる .. + 未作成の中間: 保護領域を使えないため飛ばす"
    return 0
  fi
  base="$PROT_REAL/c10"
  rm -rf -- "$base"
  mkdir -p "$base/safe" "$base/home" || { ok "打ち消せる .. + 未作成の中間: 作業場所を作れないため飛ばす"; return 0; }
  R="$base/repo"
  mkdir -p "$R"
  git -c init.defaultBranch=main init -q -- "$R"
  printf 'seed\n' >"$R/seed.txt"
  GIT "$R" add seed.txt
  GIT "$R" commit -q -m seed
  mkdir -p "$R/task"
  write_task "$R/task/t.md"
  # `<base>/safe/new` は存在しない。打ち消すと `<base>/safe/state`(toplevel の外)に収まる
  xdg="$base/safe/new/../state"
  rune HOME="$base/home" XDG_STATE_HOME="$xdg" -- take --cwd "$R" --task-md task/t.md
  rc_is "打ち消せる .. + 未作成の中間: take が exit 0" 0
  sd="$(sed -n 's/^STATE_DIR=//p' "$CASE_OUT")"
  case "$sd" in
    "$base"/safe/state/dev-workflow/guard-?*) ok "打ち消せる .. + 未作成の中間: 相殺した先に保護領域を作る" ;;
    *) ng "打ち消せる .. + 未作成の中間: 相殺した先に保護領域を作る(実際: $sd)" ;;
  esac
  ckf "打ち消せる .. + 未作成の中間: 打ち消される中間ディレクトリを作らない" test -e "$base/safe/new"
  ckf "打ち消せる .. + 未作成の中間: 後続の候補(\$HOME/.local/state)へは落ちない" test -e "$base/home/.local"
  MAN="$(sed -n 's/^MANIFEST_SHA256=//p' "$CASE_OUT")"
  SNAP="$(sed -n 's/^SNAPSHOT_SHA256=//p' "$CASE_OUT")"
  ARGS=(--cwd "$R" --state "${sd:-$PROT/dev-workflow/guard-none}" --manifest-sha256 "${MAN:-0}" --snapshot-sha256 "${SNAP:-0}")
  # **後続のサブコマンドが同じ綴りの XDG_STATE_HOME で保護領域を再認識できること**
  rune HOME="$base/home" XDG_STATE_HOME="$xdg" -- compare "${ARGS[@]}" --run-rc 0
  rc_is "打ち消せる .. + 未作成の中間: compare が exit 0(state-missing にしない)" 0
  out_line "打ち消せる .. + 未作成の中間: RESULT=normal" 'RESULT=normal'
  out_lacks "打ち消せる .. + 未作成の中間: compare が state-missing で止まらない" 'REASON=state-missing'
  rune HOME="$base/home" XDG_STATE_HOME="$xdg" -- taskmd-diff "${ARGS[@]}"
  rc_is "打ち消せる .. + 未作成の中間: taskmd-diff が exit 0" 0
  out_line "打ち消せる .. + 未作成の中間: TASKMD=same" 'TASKMD=same'
  rune HOME="$base/home" XDG_STATE_HOME="$xdg" -- cleanup --state "${sd:-$PROT/dev-workflow/guard-none}"
  rc_is "打ち消せる .. + 未作成の中間: cleanup が exit 0(2 で何も消さないにしない)" 0
  ckf "打ち消せる .. + 未作成の中間: 保護領域(平文複製を含む)が消える" test -e "$sd"
  rm -rf -- "$base"
  ckf "打ち消せる .. + 未作成の中間: ケースの作業場所を消した" test -e "$base"
}

# ════════════════════════ D 穴 ════════════════════════
case_D1() { # 変異 (h) の検出先
  CID=D1; TAG="[類型:unbornHEAD] "
  mkrepo d1
  mkdir -p "$R/task"
  write_task "$R/task/t.md"
  printf 'staged\n' >"$R/s.txt"
  GIT "$R" add s.txt
  ckf "(前提)HEAD が無い" GIT "$R" rev-parse --verify --quiet HEAD
  take "$R" task/t.md
  rc_is "take が exit 0(縮退にしない)" 0
  ckt "④ に HEAD 無しを値として記録する" inx "$STATE_DIR/snapshot/4-meta.txt" "head${TAB}(HEAD 無し)"
  ckt "① は空ツリーとの差分(stage 済みの内容が入る)" inf "$STATE_DIR/snapshot/1-diff.bin" '+staged'
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "compare: 変化なし → 32" 32
  printf 'n\n' >"$R/new.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "compare: ファイル追加を検出 → 31" 31
  out_line "compare: 追加が CHANGE= に出る" "CHANGE=untracked-added${TAB}new.txt"
  printf 'staged 2\n' >"$R/s.txt"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "compare: rc 0 なら exit 0" 0
  out_re "compare: stage 済みファイルの変更が追跡差分に出る" "^CHANGE=tracked-diff${TAB}"
  out_lacks "compare: 再取得の失敗にしない" 'REASON='
  GIT "$R" commit -q -m first
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "compare: 起動後に最初の commit が作られる → 31" 31
  out_line "compare: CHANGE=head の旧値が (HEAD 無し)" "CHANGE=head${TAB}(HEAD 無し) -> $(GIT "$R" rev-parse HEAD)"
}
case_D2() { # 変異 (j) の検出先
  CID=D2; TAG=""
  local main lw
  base_repo d2-main
  main="$R"
  lw="$WORK/d2-lw"
  GIT "$main" worktree add -q -b wt "$lw" >/dev/null 2>&1
  mkdir -p "$lw/task"
  write_task "$lw/task/t.md"
  take "$lw" task/t.md
  rc_is "linked worktree: take が exit 0" 0
  ckt "linked worktree: toplevel は linked worktree の側" inx "$STATE_DIR/snapshot/4-meta.txt" "toplevel${TAB}$lw"
  ckt "linked worktree: hooks は common dir から取る" inx "$STATE_DIR/snapshot/4-meta.txt" "hooks-path${TAB}$main/.git/hooks"
  ckt "linked worktree: config は common dir から取る" inx "$STATE_DIR/snapshot/4-meta.txt" "config-path${TAB}$main/.git/config"
  ckt "linked worktree: config.worktree は worktree ごとの場所" inx "$STATE_DIR/snapshot/4-meta.txt" "config-worktree-path${TAB}$main/.git/worktrees/d2-lw/config.worktree"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "linked worktree: 変化なし → 0" 0
  printf '#!/bin/sh\nexit 0\n' >"$main/.git/hooks/pre-commit"
  chmod +x "$main/.git/hooks/pre-commit"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "linked worktree: common dir への hook の設置 → 33" 33
  out_line "linked worktree: REASON=hooks-changed" 'REASON=hooks-changed'
  out_line "linked worktree: 変わった hook のパスが出る" "CHANGE=hooks${TAB}$main/.git/hooks/pre-commit"
  rm -f -- "$main/.git/hooks/pre-commit"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "linked worktree: hook を元へ戻すと再び比較できる → 0" 0
  printf '[alias]\n\tx = status\n' >>"$main/.git/config"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "linked worktree: common dir の config の変更 → 33" 33
  out_line "linked worktree: REASON=config-changed" 'REASON=config-changed'
}
case_D3() { # 変異 (j) の検出先
  CID=D3; TAG=""
  base_repo d3
  mkdir -p "$WORK/d3-hooks"
  GIT "$R" config core.hooksPath "$WORK/d3-hooks"
  take "$R" task/t.md
  rc_is "core.hooksPath が別ディレクトリ: take が exit 0" 0
  ckt "core.hooksPath が別ディレクトリ: 記録されるのはそのディレクトリ" inx "$STATE_DIR/snapshot/4-meta.txt" "hooks-path${TAB}$WORK/d3-hooks"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "core.hooksPath が別ディレクトリ: 変化なし → 0" 0
  printf '#!/bin/sh\nexit 0\n' >"$WORK/d3-hooks/pre-push"
  chmod +x "$WORK/d3-hooks/pre-push"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "core.hooksPath が別ディレクトリ: そこへの hook の設置 → 33" 33
  out_line "core.hooksPath が別ディレクトリ: REASON=hooks-changed" 'REASON=hooks-changed'
  out_line "core.hooksPath が別ディレクトリ: 変わった hook のパスが出る" "CHANGE=hooks${TAB}$WORK/d3-hooks/pre-push"

  # 作業ツリー内の追跡ディレクトリが hooks のとき、そこの編集は比較不能に倒れる(安全側)
  base_repo d3b
  mkdir -p "$R/.githooks"
  printf '#!/bin/sh\nexit 0\n' >"$R/.githooks/pre-commit"
  GIT "$R" add .githooks/pre-commit
  GIT "$R" commit -q -m hooks
  GIT "$R" config core.hooksPath .githooks
  take "$R" task/t.md
  rc_is "core.hooksPath が作業ツリー内(相対): take が exit 0" 0
  ckt "core.hooksPath が作業ツリー内(相対): 絶対パスで記録する" inx "$STATE_DIR/snapshot/4-meta.txt" "hooks-path${TAB}$R/.githooks"
  printf '#!/bin/sh\nexit 1\n' >"$R/.githooks/pre-commit"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "core.hooksPath が作業ツリー内(相対): hook の編集 → 33" 33
  out_line "core.hooksPath が作業ツリー内(相対): REASON=hooks-changed" 'REASON=hooks-changed'
}
case_D4() {
  CID=D4; TAG=""
  base_repo d4 --template=
  ckf "(前提)hooks ディレクトリが無い" test -e "$R/.git/hooks"
  take "$R" task/t.md
  rc_is "hooks ディレクトリが無い repo: take が exit 0" 0
  ckt "hooks ディレクトリが無い repo: ダイジェストは (無し)" inx "$STATE_DIR/snapshot/4-meta.txt" "hooks-digest${TAB}(無し)"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "hooks ディレクトリが無い repo: 変化なし → 0" 0
  mkdir -p "$R/.git/hooks"
  printf '#!/bin/sh\nexit 0\n' >"$R/.git/hooks/pre-commit"
  chmod +x "$R/.git/hooks/pre-commit"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "hooks ディレクトリが無い repo: 起動後に hook が置かれる → 33" 33
  out_line "hooks ディレクトリが無い repo: REASON=hooks-changed" 'REASON=hooks-changed'
  out_line "hooks ディレクトリが無い repo: GIT_SKIPPED=yes" 'GIT_SKIPPED=yes'

  base_repo d4b
  GIT "$R" config core.hooksPath /dev/null
  take "$R" task/t.md
  rc_is "core.hooksPath=/dev/null の repo: take が exit 0(20 に落ちない)" 0
  ckt "core.hooksPath=/dev/null の repo: ダイジェストは (ディレクトリでない)" inx "$STATE_DIR/snapshot/4-meta.txt" "hooks-digest${TAB}(ディレクトリでない)"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "core.hooksPath=/dev/null の repo: 変化なし → 0" 0
}
case_D5() {
  CID=D5; TAG=""
  base_repo d5
  GIT "$R" config extensions.worktreeConfig true
  take "$R" task/t.md
  rc_is "main worktree + extensions.worktreeConfig: take が exit 0" 0
  ckt "main worktree + extensions.worktreeConfig: config.worktree を常に記録する" inx "$STATE_DIR/snapshot/4-meta.txt" "config-worktree-path${TAB}$R/.git/config.worktree"
  printf '[core]\n\tfsmonitor = %s\n' "$STUBS/trace.sh" >"$R/.git/config.worktree"
  : >"$TRACE"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "main worktree + extensions.worktreeConfig: config.worktree の書き換え → 33" 33
  out_line "main worktree + extensions.worktreeConfig: REASON=config-changed" 'REASON=config-changed'
  out_line "main worktree + extensions.worktreeConfig: GIT_SKIPPED=yes" 'GIT_SKIPPED=yes'
  out_line "main worktree + extensions.worktreeConfig: 変わったファイルのパスが出る" "CHANGE=config${TAB}$R/.git/config.worktree"
  ckeq "main worktree + extensions.worktreeConfig: 仕込まれた fsmonitor を起動しない(痕跡 0)" "$(trace_n)" 0
}
case_D6() {
  CID=D6; TAG=""
  base_repo d6
  if [ "$IS_ROOT" -eq 1 ]; then
    ok "読めない未追跡エントリは u で記録して続行(root のため飛ばす — root は読めてしまう)"
    return 0
  fi
  printf 'secret\n' >"$R/noread.bin"
  printf 'u\n' >"$R/u.txt"
  chmod 000 "$R/noread.bin"
  take "$R" task/t.md
  rc_is "読めない未追跡エントリ: take が exit 0(続行する)" 0
  out_line "読めない未追跡エントリ: UNREADABLE=1" 'UNREADABLE=1'
  err_has "読めない未追跡エントリ: パスが stderr に出る" 'NOTE: unreadable noread.bin'
  ckt "読めない未追跡エントリ: u 行(値は -)で記録する" ine "$STATE_DIR/manifest.tsv" "^u${TAB}[0-7]+${TAB}-${TAB}noread\\.bin\$"
  ckt "読めない未追跡エントリ: 他のエントリは退避する" cmp -s "$STATE_DIR/files/u.txt" "$R/u.txt"
  ckf "読めない未追跡エントリ: 退避コピーは無い" test -e "$STATE_DIR/files/noread.bin"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "読めない未追跡エントリ: 変化なし → 32" 32
  chmod 644 "$R/noread.bin"
}

# ════════════════════════ E compare の決定表 ════════════════════════
e_repo() { # $1=名前 → 追跡 a.txt / 未追跡 u.txt・lnk(→ seed.txt)・task/t.md を置いて take する
  base_repo "$1"
  printf 'tracked\n' >"$R/a.txt"
  GIT "$R" add a.txt
  GIT "$R" commit -q -m a
  printf 'untracked\n' >"$R/u.txt"
  chmod 644 "$R/u.txt"
  ln -s seed.txt "$R/lnk"
  take "$R" task/t.md
}
case_E1() { # 変異テストの対照ケース
  CID=E1; TAG=""
  e_repo e1
  rc_is "(前提)take が exit 0" 0
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "変化なし + rc 0 → 0" 0
  out_line "変化なし + rc 0: RESULT=normal" 'RESULT=normal'
  out_line "変化なし + rc 0: WORKTREE_CHANGED=no" 'WORKTREE_CHANGED=no'
  out_line "変化なし + rc 0: GITMETA_CHANGED=no" 'GITMETA_CHANGED=no'
  ckeq "変化なし + rc 0: CHANGE= を出さない" "$(out_count '^CHANGE=')" 0
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "変化なし + rc 1 → 32" 32
  out_line "変化なし + rc 1: RESULT=fallback" 'RESULT=fallback'
  ckeq "変化なし + rc 1: CHANGE= を出さない" "$(out_count '^CHANGE=')" 0
  run compare "${ARGS[@]}" --run-rc 143
  rc_is "変化なし + rc 143(シグナルでの中止)→ 32" 32
  ckeq "compare は作業用の一時領域を保護領域に残さない" "$(find "$STATE_DIR" -maxdepth 1 -name '.work*' | wc -l | tr -d ' ')" 0
}
case_E2() {
  CID=E2; TAG="[類型:内容変更] "
  e_repo e2
  printf 'tracked changed\n' >"$R/a.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "追跡ファイルの内容変更 + rc 1 → 31" 31
  out_line "追跡ファイルの内容変更: RESULT=takeover" 'RESULT=takeover'
  out_line "追跡ファイルの内容変更: WORKTREE_CHANGED=yes" 'WORKTREE_CHANGED=yes'
  out_line "追跡ファイルの内容変更: GITMETA_CHANGED=no" 'GITMETA_CHANGED=no'
  out_re "追跡ファイルの内容変更: tracked-diff に起動前の記録のパスと現在値の sha256" "^CHANGE=tracked-diff${TAB}.*/snapshot/1-diff\\.bin${TAB}[0-9a-f]{64}\$"
  out_re "追跡ファイルの内容変更: status にも出る" "^CHANGE=status${TAB}.*/snapshot/2-status\\.z${TAB}[0-9a-f]{64}\$"
  printf 'tracked\n' >"$R/a.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "追跡ファイルを同じ内容へ戻す → 32(stat だけの違いは変化にしない)" 32
  printf 'untracked changed\n' >"$R/u.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "未追跡ファイルの内容変更 + rc 1 → 31" 31
  out_line "未追跡ファイルの内容変更: CHANGE=untracked-changed" "CHANGE=untracked-changed${TAB}u.txt"
  out_line "未追跡ファイルの内容変更: BASE= が退避コピーを指す(ok)" "BASE=$STATE_DIR/files/u.txt${TAB}ok"
  ckeq "未追跡ファイルの内容変更: 退避コピーは起動前の内容のまま" "$(cat "$STATE_DIR/files/u.txt")" 'untracked'
  out_lacks "未追跡ファイルの内容変更: status は変わらない(マニフェストだけが検出する)" 'CHANGE=status'
}
case_E3() {
  CID=E3; TAG="[類型:削除] "
  e_repo e3
  rm -f -- "$R/u.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "未追跡ファイルの削除 + rc 1 → 31" 31
  out_line "未追跡ファイルの削除: CHANGE=untracked-removed" "CHANGE=untracked-removed${TAB}u.txt"
  out_line "未追跡ファイルの削除: BASE= が退避コピーを指す(ok)" "BASE=$STATE_DIR/files/u.txt${TAB}ok"
  cp -p "$STATE_DIR/files/u.txt" "$R/u.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "退避コピーから戻すと変化なし → 32" 32
  rm -f -- "$R/a.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "追跡ファイルの削除 + rc 1 → 31" 31
  out_re "追跡ファイルの削除: tracked-diff に出る" "^CHANGE=tracked-diff${TAB}"
  out_re "追跡ファイルの削除: status に出る" "^CHANGE=status${TAB}"
}
case_E4() {
  CID=E4; TAG="[類型:symlink差し替え] "
  e_repo e4
  ckt "(前提)マニフェストに l 行が在る" ine "$STATE_DIR/manifest.tsv" "^l${TAB}[0-7]+${TAB}seed\\.txt${TAB}lnk\$"
  ln -sfn a.txt "$R/lnk"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "未追跡 symlink のリンク先変更 + rc 1 → 31" 31
  out_line "未追跡 symlink のリンク先変更: CHANGE=untracked-changed" "CHANGE=untracked-changed${TAB}lnk"
  out_line "未追跡 symlink のリンク先変更: BASE= が ok" "BASE=$STATE_DIR/files/lnk${TAB}ok"
  rm -f -- "$R/lnk"
  printf 'seed\n' >"$R/lnk"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "symlink → 同内容の通常ファイルへの差し替え → 31" 31
  out_line "symlink → 通常ファイル: CHANGE=untracked-changed" "CHANGE=untracked-changed${TAB}lnk"
  rm -f -- "$R/lnk"
  ln -s seed.txt "$R/lnk"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "symlink を元へ戻すと変化なし → 32" 32
  printf 'untracked\n' >"$WORK/e4-elsewhere.txt"
  rm -f -- "$R/u.txt"
  ln -s "$WORK/e4-elsewhere.txt" "$R/u.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "通常ファイル → 同内容を指す symlink への差し替え → 31(辿らない)" 31
  out_line "通常ファイル → symlink: CHANGE=untracked-changed" "CHANGE=untracked-changed${TAB}u.txt"
}
case_E5() {
  CID=E5; TAG=""
  e_repo e5
  mkdir -p "$R/src"
  printf 'new\n' >"$R/src/new.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "未追跡の追加 + rc 1 → 31" 31
  out_line "未追跡の追加: CHANGE=untracked-added" "CHANGE=untracked-added${TAB}src/new.txt"
  out_re "未追跡の追加: status にも出る" "^CHANGE=status${TAB}"
}
case_E6() {
  CID=E6; TAG=""
  e_repo e6
  chmod 755 "$R/u.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "未追跡ファイルのモードだけの変更 + rc 1 → 31" 31
  out_line "モードだけの変更: CHANGE=untracked-changed" "CHANGE=untracked-changed${TAB}u.txt"
  out_line "モードだけの変更: BASE= が ok" "BASE=$STATE_DIR/files/u.txt${TAB}ok"
  chmod 644 "$R/u.txt"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "モードを戻すと変化なし → 32" 32
}
case_E7() {
  CID=E7; TAG=""
  e_repo e7
  printf 'untracked changed\n' >"$R/u.txt"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "rc 0 + 変化 → exit 0" 0
  out_line "rc 0 + 変化: RESULT=normal" 'RESULT=normal'
  out_line "rc 0 + 変化: WORKTREE_CHANGED=yes" 'WORKTREE_CHANGED=yes'
  out_line "rc 0 + 変化: CHANGE= が出る" "CHANGE=untracked-changed${TAB}u.txt"
}
case_E8() {
  CID=E8; TAG=""
  base_repo e8
  mkdir -p "$R/.claude/reviews"
  printf 'old log\n' >"$R/.claude/reviews/run1.md"
  take "$R" task/t.md
  rc_is "(前提)take が exit 0" 0
  ckf ".claude/reviews/ 配下はマニフェストに載らない" inf "$STATE_DIR/manifest.tsv" '.claude/reviews'
  printf 'more\n' >>"$R/.claude/reviews/run1.md"
  printf 'new log\n' >"$R/.claude/reviews/run2.md"
  rm -f -- "$R/.claude/reviews/none.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is ".claude/reviews/ 配下だけの変化 → 変化なし(32)" 32
  out_line ".claude/reviews/ 配下だけの変化: WORKTREE_CHANGED=no" 'WORKTREE_CHANGED=no'
  ckeq ".claude/reviews/ 配下だけの変化: CHANGE= を出さない" "$(out_count '^CHANGE=')" 0
}
case_E9() { # 変異 (g) の検出先
  CID=E9; TAG=""
  # 未追跡のタスク MD(除外より優先して f 行に載る)
  base_repo e9a
  rm -rf -- "$R/task"
  mkdir -p "$R/.claude/reviews"
  write_task "$R/.claude/reviews/task.md"
  printf 'log\n' >"$R/.claude/reviews/run.md"
  take "$R" .claude/reviews/task.md
  rc_is "タスク MD が .claude/reviews/ 配下(未追跡): take が exit 0" 0
  ckt "タスク MD が .claude/reviews/ 配下(未追跡): 除外より優先してマニフェストに載る" ine "$STATE_DIR/manifest.tsv" "^f${TAB}[0-7]+${TAB}[0-9a-f]{64}${TAB}\\.claude/reviews/task\\.md\$"
  printf 'log 2\n' >>"$R/.claude/reviews/run.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "タスク MD が .claude/reviews/ 配下(未追跡): ログだけの変化 → 32" 32
  printf -- '- [ ] 足された要件\n' >>"$R/.claude/reviews/task.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "タスク MD が .claude/reviews/ 配下(未追跡): 本文の変更を compare が検出 → 31" 31
  out_line "タスク MD が .claude/reviews/ 配下(未追跡): CHANGE=untracked-changed" "CHANGE=untracked-changed${TAB}.claude/reviews/task.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "タスク MD が .claude/reviews/ 配下(未追跡): taskmd-diff が 34" 34

  # 追跡済みのタスク MD(pathspec の除外で ①② に現れないので、T 行が単独で担う)
  base_repo e9b
  rm -rf -- "$R/task"
  mkdir -p "$R/.claude/reviews"
  write_task "$R/.claude/reviews/task.md"
  GIT "$R" add -f .claude/reviews/task.md
  GIT "$R" commit -q -m task
  take "$R" .claude/reviews/task.md
  rc_is "タスク MD が .claude/reviews/ 配下(追跡済み): take が exit 0" 0
  printf -- '- [ ] 足された要件\n' >>"$R/.claude/reviews/task.md"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "タスク MD が .claude/reviews/ 配下(追跡済み): 本文の変更を T 行が検出 → 31" 31
  out_line "タスク MD が .claude/reviews/ 配下(追跡済み): T 行の変化は実体の絶対パスで出る" "CHANGE=untracked-changed${TAB}$R/.claude/reviews/task.md"
  out_line "タスク MD が .claude/reviews/ 配下(追跡済み): BASE= は taskmd-body(ok)" "BASE=$STATE_DIR/taskmd-body${TAB}ok"
  out_lacks "タスク MD が .claude/reviews/ 配下(追跡済み): ①② には現れない" 'CHANGE=tracked-diff'
  run taskmd-diff "${ARGS[@]}"
  rc_is "タスク MD が .claude/reviews/ 配下(追跡済み): taskmd-diff が 34" 34
}
case_E10() {
  CID=E10; TAG=""
  local nl
  nl="$(printf 'new\nline.txt')"
  e_repo e10
  printf 'x\n' >"$R/$nl"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "起動後に改行入りのファイル名が作られる → 31" 31
  out_line "改行入りのファイル名: C 風引用の 1 行で出る" "CHANGE=untracked-added${TAB}\"new\\nline.txt\""
  ckeq "改行入りのファイル名: stdout の全行が KEY=VALUE の形" "$(grep -c -v -E '^[A-Z_]+=' -- "$CASE_OUT" || true)" 0
  ckeq "改行入りのファイル名: untracked-added は 1 行だけ" "$(out_count '^CHANGE=untracked-added')" 1
}

# ════════════════════════ F git メタ ════════════════════════
case_F1() {
  CID=F1; TAG=""
  local h0 h1
  e_repo f1
  h0="$(GIT "$R" rev-parse HEAD)"
  printf 'committed\n' >>"$R/a.txt"
  GIT "$R" commit -q -a -m external
  h1="$(GIT "$R" rev-parse HEAD)"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "起動後に commit した + rc 1 → 31" 31
  out_line "commit した: CHANGE=head(旧値 -> 新値)" "CHANGE=head${TAB}$h0 -> $h1"
  out_line "commit した: ref の変化に ref 名が出る" "CHANGE=refs${TAB}~ refs/heads/main $h0 -> $h1"
  out_line "commit した: GITMETA_CHANGED=yes" 'GITMETA_CHANGED=yes'
  out_line "commit した: 作業ツリーは HEAD と同じまま(WORKTREE_CHANGED=no)" 'WORKTREE_CHANGED=no'
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "起動後に commit した + rc 0 → 0(CHANGE= は出る)" 0
  out_re "commit した + rc 0: CHANGE=head が出る" "^CHANGE=head${TAB}"
}
case_F2() {
  CID=F2; TAG=""
  e_repo f2
  GIT "$R" add u.txt
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "起動後に git add した + rc 1 → 31" 31
  out_re "git add した: CHANGE=index-tree(旧値 -> 新値)" "^CHANGE=index-tree${TAB}[0-9a-f]{40} -> [0-9a-f]{40}\$"
  out_line "git add した: GITMETA_CHANGED=yes" 'GITMETA_CHANGED=yes'
  out_line "git add した: 未追跡の対象集合から消えた" "CHANGE=untracked-removed${TAB}u.txt"
}
case_F3() {
  CID=F3; TAG="[類型:別ブランチ切替] "
  base_repo f3
  GIT "$R" branch same                     # 同じコミットを指す別ブランチ(起動前から在る)
  take "$R" task/t.md
  rc_is "(前提)take が exit 0" 0
  GIT "$R" checkout -q same
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "同コミット別ブランチへの切替 + rc 1 → 31" 31
  out_line "同コミット別ブランチへの切替: CHANGE=branch(旧値 -> 新値)" "CHANGE=branch${TAB}refs/heads/main -> refs/heads/same"
  out_line "同コミット別ブランチへの切替: GITMETA_CHANGED=yes" 'GITMETA_CHANGED=yes'
  out_line "同コミット別ブランチへの切替: WORKTREE_CHANGED=no(5 要素の ①②③ では見えない)" 'WORKTREE_CHANGED=no'
  ckeq "同コミット別ブランチへの切替: 変化はブランチ名だけ" "$(out_count '^CHANGE=')" 1
  GIT "$R" checkout -q --detach
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "detached HEAD への切替 + rc 1 → 31" 31
  out_line "detached HEAD への切替: CHANGE=branch" "CHANGE=branch${TAB}refs/heads/main -> (detached)"
  GIT "$R" checkout -q main
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "元のブランチへ戻すと変化なし → 32" 32
}
case_F4() {
  CID=F4; TAG=""
  local h
  e_repo f4
  h="$(GIT "$R" rev-parse HEAD)"
  GIT "$R" update-ref refs/heads/evil HEAD
  GIT "$R" tag marker
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "起動後に新しい ref + rc 1 → 31" 31
  out_line "新しい ref: CHANGE=refs に増えた ref 名が出る(ブランチ)" "CHANGE=refs${TAB}+ $h refs/heads/evil"
  out_line "新しい ref: CHANGE=refs に増えた ref 名が出る(タグ)" "CHANGE=refs${TAB}+ $h refs/tags/marker"
  out_line "新しい ref: GITMETA_CHANGED=yes" 'GITMETA_CHANGED=yes'
  out_line "新しい ref: WORKTREE_CHANGED=no" 'WORKTREE_CHANGED=no'
  GIT "$R" update-ref -d refs/heads/evil
  GIT "$R" tag -d marker >/dev/null
  GIT "$R" branch -q gone
  take "$R" task/t.md
  GIT "$R" branch -q -D gone
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "起動後に ref が消えた + rc 1 → 31" 31
  out_line "ref が消えた: CHANGE=refs に消えた ref 名が出る" "CHANGE=refs${TAB}- $h refs/heads/gone"
}
case_F5() {
  CID=F5; TAG="[類型:stash] "
  local deep
  e_repo f5pre
  # 起動前から stash が 2 件在る状態で取り直す
  printf 'wip 1\n' >>"$R/a.txt"
  GIT "$R" stash push -q
  printf 'wip 2\n' >>"$R/a.txt"
  GIT "$R" stash push -q
  take "$R" task/t.md
  rc_is "(前提)stash 2 件で take が exit 0" 0
  deep="$(GIT "$R" rev-parse 'stash@{1}')"
  printf 'wip 3\n' >>"$R/a.txt"
  GIT "$R" stash push -q
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "起動後に stash push + rc 1 → 31" 31
  out_re "stash push: CHANGE=stash に増えた行が出る" "^CHANGE=stash${TAB}\\+ [0-9a-f]{40} stash@\\{0\\} "
  out_line "stash push: GITMETA_CHANGED=yes" 'GITMETA_CHANGED=yes'
  out_line "stash push: 作業ツリーは元のまま(WORKTREE_CHANGED=no)" 'WORKTREE_CHANGED=no'
  GIT "$R" stash drop -q 'stash@{0}'
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "足した stash を捨てる → stash の一覧は起動前と同じ" 32
  GIT "$R" stash drop -q 'stash@{1}'
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "深い位置の stash drop + rc 1 → 31" 31
  out_re "深い位置の stash drop: CHANGE=stash に消えた行が出る" "^CHANGE=stash${TAB}- $deep "
  out_lacks "深い位置の stash drop: refs/stash の先端は変わらない" 'CHANGE=refs'
}
case_F6() {
  CID=F6; TAG=""
  base_repo f6
  mk_conflict
  take "$R" task/t.md
  rc_is "(前提)merge の前の take が exit 0" 0
  GIT "$R" merge other >/dev/null 2>&1 || true
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "起動後に index が unmerged になる + rc 1 → 31(33 にしない)" 31
  out_re "起動後に unmerged: CHANGE=index-tree の新値が (unmerged)" "^CHANGE=index-tree${TAB}[0-9a-f]{40} -> \\(unmerged\\)\$"
  out_line "起動後に unmerged: GITMETA_CHANGED=yes" 'GITMETA_CHANGED=yes'
  out_lacks "起動後に unmerged: 再取得の失敗にしない" 'REASON='
}

# ════════════════════════ G 比較不能 ════════════════════════
incomparable() { # $1=説明 $2=REASON(直前の run の結果を見る)
  rc_is "$1 → 33" 33
  out_line "$1: RESULT=incomparable" 'RESULT=incomparable'
  out_line "$1: REASON=$2" "REASON=$2"
  out_line "$1: GIT_SKIPPED=yes" 'GIT_SKIPPED=yes'
  out_line "$1: WORKTREE_CHANGED=unknown(「変化なし」と読ませない)" 'WORKTREE_CHANGED=unknown'
}
case_G1() { # 変異 (a) の検出先
  CID=G1; TAG=""
  e_repo g1
  printf ' ' >>"$STATE_DIR/manifest.tsv"
  run compare "${ARGS[@]}" --run-rc 0
  incomparable "マニフェストを 1 バイト変える" manifest-digest
  out_line "マニフェストを 1 バイト変える: GITMETA_CHANGED=unknown" 'GITMETA_CHANGED=unknown'
  run taskmd-diff "${ARGS[@]}"
  rc_is "マニフェストを 1 バイト変える: taskmd-diff も 33" 33
  out_line "マニフェストを 1 バイト変える: taskmd-diff は REASON=digest" 'REASON=digest'
  run restore-taskmd "${ARGS[@]}"
  rc_is "マニフェストを 1 バイト変える: restore-taskmd も 33" 33
  out_line "マニフェストを 1 バイト変える: restore-taskmd は実ツリーに触れない" 'TOUCHED=none'
}
case_G2() { # 変異 (a) の検出先
  CID=G2; TAG=""
  local f
  e_repo g2
  for f in 1-diff.bin 2-status.z 4-meta.txt 5-stash.txt taskmd.txt; do
    cp -p "$STATE_DIR/snapshot/$f" "$WORK/g2-keep"
    printf 'x' >>"$STATE_DIR/snapshot/$f"
    run compare "${ARGS[@]}" --run-rc 0
    incomparable "snapshot の $f を変える" snapshot-digest
    cp -p "$WORK/g2-keep" "$STATE_DIR/snapshot/$f"
  done
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "snapshot を元へ戻すと比較できる → 0" 0
  printf 'x\n' >"$STATE_DIR/snapshot/9-extra.txt"
  run compare "${ARGS[@]}" --run-rc 0
  incomparable "snapshot にファイルを足す" snapshot-digest
}
case_G3() { # 変異 (a) の検出先
  CID=G3; TAG=""
  e_repo g3
  run compare --cwd "$R" --state "$STATE_DIR" --manifest-sha256 "$(flip "$MAN")" --snapshot-sha256 "$SNAP" --run-rc 0
  incomparable "渡すダイジェストが違う(マニフェスト)" manifest-digest
  run compare --cwd "$R" --state "$STATE_DIR" --manifest-sha256 "$MAN" --snapshot-sha256 "$(flip "$SNAP")" --run-rc 0
  incomparable "渡すダイジェストが違う(スナップショット)" snapshot-digest
  run compare --cwd "$R" --state "$STATE_DIR" --manifest-sha256 "$SNAP" --snapshot-sha256 "$MAN" --run-rc 1
  incomparable "渡すダイジェストが違う(2 つを取り違えた)" manifest-digest
}
case_G4() {
  CID=G4; TAG=""
  e_repo g4
  run compare --cwd "$R" --state "$PROT/dev-workflow/guard-none" --manifest-sha256 "$MAN" --snapshot-sha256 "$SNAP" --run-rc 0
  incomparable "--state が無い" state-missing
  ln -s "$STATE_DIR" "$PROT/dev-workflow/guard-link-g4"
  run compare --cwd "$R" --state "$PROT/dev-workflow/guard-link-g4" --manifest-sha256 "$MAN" --snapshot-sha256 "$SNAP" --run-rc 0
  incomparable "--state が symlink" state-missing
  run taskmd-diff --cwd "$R" --state "$PROT/dev-workflow/guard-link-g4" --manifest-sha256 "$MAN" --snapshot-sha256 "$SNAP"
  rc_is "--state が symlink: taskmd-diff も 33" 33
  rm -f -- "$PROT/dev-workflow/guard-link-g4"
  mkdir -p "$WORK/g4-out"
  cp -Rp "$STATE_DIR" "$WORK/g4-out/guard-copy"
  run compare --cwd "$R" --state "$WORK/g4-out/guard-copy" --manifest-sha256 "$MAN" --snapshot-sha256 "$SNAP" --run-rc 0
  incomparable "--state が保護領域の外(/tmp 配下の写し)" state-missing
  chmod 755 "$STATE_DIR"
  run compare "${ARGS[@]}" --run-rc 0
  incomparable "--state が 0700 でない" state-missing
  chmod 700 "$STATE_DIR"
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "--state を 0700 へ戻すと比較できる → 0" 0
}
plant_in_config() { # R の .git/config に痕跡スタブを仕込む(git config を使わず直接書く)
  printf '[core]\n\tfsmonitor = %s\n[diff]\n\texternal = %s\n' "$STUBS/trace.sh" "$STUBS/trace.sh" >>"$R/.git/config"
}
case_G5() {
  CID=G5; TAG=""
  e_repo g5
  printf 'tracked changed\n' >"$R/a.txt"
  mkdir -p "$R/.git/hooks"
  cp "$STUBS/trace.sh" "$R/.git/hooks/post-index-change"
  cp "$STUBS/trace.sh" "$R/.git/hooks/pre-commit"
  : >"$TRACE"
  run compare "${ARGS[@]}" --run-rc 1
  incomparable "起動後の hook の設置" hooks-changed
  out_line "起動後の hook の設置: GITMETA_CHANGED=yes(変化は確定している)" 'GITMETA_CHANGED=yes'
  out_line "起動後の hook の設置: 変わった hook のパスが出る" "CHANGE=hooks${TAB}$R/.git/hooks/post-index-change"
  ckeq "起動後の hook の設置: 仕込まれた hook を起動しない(痕跡 0)" "$(trace_n)" 0
  # 既に在る hook の書き換え・モードだけの変更も同じ
  rm -f -- "$R/.git/hooks/post-index-change" "$R/.git/hooks/pre-commit"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "hook を取り除くと比較できる → 31(追跡の変更が残っている)" 31
  if [ -f "$R/.git/hooks/pre-commit.sample" ]; then
    chmod 644 "$R/.git/hooks/pre-commit.sample"
    run compare "${ARGS[@]}" --run-rc 1
    incomparable "既に在る hooks のエントリのモードだけを変える" hooks-changed
  else
    ok "既に在る hooks のエントリのモードだけを変える(サンプル hook が無い環境のため飛ばす)"
  fi
}
case_G6() {
  CID=G6; TAG=""
  e_repo g6
  printf 'tracked changed\n' >"$R/a.txt"
  plant_in_config
  : >"$TRACE"
  run compare "${ARGS[@]}" --run-rc 1
  incomparable "起動後の .git/config の変更" config-changed
  out_line "起動後の .git/config の変更: GITMETA_CHANGED=yes(変化は確定している)" 'GITMETA_CHANGED=yes'
  out_line "起動後の .git/config の変更: 変わったファイルのパスが出る" "CHANGE=config${TAB}$R/.git/config"
  ckeq "起動後の .git/config の変更: 仕込まれた fsmonitor・diff.external を起動しない(痕跡 0)" "$(trace_n)" 0
  ckeq "起動後の .git/config の変更: 33 で止まっても作業用の一時領域を保護領域に残さない" \
    "$(find "$STATE_DIR" -maxdepth 1 -name '.work*' | wc -l | tr -d ' ')" 0
  # hook と config の両方が変わったとき
  cp "$STUBS/trace.sh" "$R/.git/hooks/post-index-change"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "hook と config の両方が変わる → 33" 33
  out_line "hook と config の両方が変わる: GIT_SKIPPED=yes" 'GIT_SKIPPED=yes'
  out_re "hook と config の両方が変わる: CHANGE=hooks が出る" "^CHANGE=hooks${TAB}"
  out_re "hook と config の両方が変わる: CHANGE=config が出る" "^CHANGE=config${TAB}"
  ckeq "hook と config の両方が変わる: 痕跡 0" "$(trace_n)" 0
}
case_G7() { # 変異 (b) の検出先
  CID=G7; TAG=""
  e_repo g7c
  # 対照: 変化が無ければ compare は git を打つ(ラッパが呼び出しを記録できていること)
  : >"$GITLOG"
  runp "$STUBS/gitlog" compare "${ARGS[@]}" --run-rc 0
  rc_is "順序の検査(対照): 変化なしの compare は exit 0" 0
  ckne "順序の検査(対照): git のラッパが呼び出しを記録している" "$(gitlog_n)" 0
  plant_in_config
  : >"$GITLOG"
  : >"$TRACE"
  runp "$STUBS/gitlog" compare "${ARGS[@]}" --run-rc 0
  rc_is "順序の検査(config-changed): exit 33" 33
  out_line "順序の検査(config-changed): REASON=config-changed" 'REASON=config-changed'
  ckeq "順序の検査(config-changed): git の呼び出しが 0 回" "$(gitlog_n)" 0
  out_line "順序の検査(config-changed): GIT_SKIPPED=yes" 'GIT_SKIPPED=yes'
  ckeq "順序の検査(config-changed): 痕跡 0" "$(trace_n)" 0

  e_repo g7h
  mkdir -p "$R/.git/hooks"
  cp "$STUBS/trace.sh" "$R/.git/hooks/post-index-change"
  : >"$GITLOG"
  : >"$TRACE"
  runp "$STUBS/gitlog" compare "${ARGS[@]}" --run-rc 0
  rc_is "順序の検査(hooks-changed): exit 33" 33
  out_line "順序の検査(hooks-changed): REASON=hooks-changed" 'REASON=hooks-changed'
  ckeq "順序の検査(hooks-changed): git の呼び出しが 0 回" "$(gitlog_n)" 0
  out_line "順序の検査(hooks-changed): GIT_SKIPPED=yes" 'GIT_SKIPPED=yes'
  ckeq "順序の検査(hooks-changed): 痕跡 0" "$(trace_n)" 0

  # taskmd-diff・restore-taskmd は git を呼ばない
  e_repo g7t
  printf -- '- [x] one\n' >>"$R/task/t.md"
  : >"$GITLOG"
  runp "$STUBS/gitlog" taskmd-diff "${ARGS[@]}"
  rc_is "順序の検査(taskmd-diff): exit 34" 34
  runp "$STUBS/gitlog" restore-taskmd "${ARGS[@]}"
  rc_is "順序の検査(restore-taskmd): exit 0" 0
  ckeq "順序の検査: taskmd-diff・restore-taskmd は git を 1 回も呼ばない" "$(gitlog_n)" 0
}

case_G8() {
  CID=G8; TAG=""
  local n0
  e_repo g8
  runp "$STUBS/gitfail" compare "${ARGS[@]}" --run-rc 1
  rc_is "再取得の途中で git が失敗する(status が rc 128)→ 33" 33
  out_line "再取得の途中で git が失敗: RESULT=incomparable" 'RESULT=incomparable'
  out_line "再取得の途中で git が失敗: REASON=retake-failed" 'REASON=retake-failed'
  out_line "再取得の途中で git が失敗: GIT_SKIPPED=no(git を打った後の失敗)" 'GIT_SKIPPED=no'
  out_line "再取得の途中で git が失敗: WORKTREE_CHANGED=unknown(縮退の 32 にしない)" 'WORKTREE_CHANGED=unknown'
  out_line "再取得の途中で git が失敗: GITMETA_CHANGED=unknown" 'GITMETA_CHANGED=unknown'
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "再取得の途中で git が失敗(対照): スタブなしなら 32" 32
  n0="$(nstates)"
  runp "$STUBS/gitfail" take --cwd "$R" --task-md task/t.md
  rc_is "take の途中で git が失敗する → 20(成功にも縮退にもしない)" 20
  ckeq "take の途中で git が失敗: 作りかけの保護領域が残らない" "$(nstates)" "$n0"
  ckeq "take の途中で git が失敗: stdout に STATE_DIR を出さない" "$(out_count '^STATE_DIR=')" 0
}

# ════════════════════════ H 退避物の実体照合 ════════════════════════
case_H1() {
  CID=H1; TAG=""
  e_repo h1
  printf 'tampered backup\n' >"$STATE_DIR/files/u.txt"
  rm -f -- "$STATE_DIR/files/lnk"
  ln -s elsewhere "$STATE_DIR/files/lnk"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "退避コピーの中身だけを変える: ダイジェストは一致し、変化なし → 32" 32
  printf 'changed by external\n' >"$R/u.txt"
  ln -sfn a.txt "$R/lnk"
  run compare "${ARGS[@]}" --run-rc 1
  rc_is "退避コピーの中身だけを変える + 実ファイルの変更 → 31(比較不能にはしない)" 31
  out_line "退避コピーの中身だけを変える: 変更の有無は言える" "CHANGE=untracked-changed${TAB}u.txt"
  out_line "退避コピーの中身だけを変える: BASE= は unverified(内容差分は提示不能)" "BASE=$STATE_DIR/files/u.txt${TAB}unverified"
  out_line "退避コピーの symlink を差し替える: BASE= は unverified" "BASE=$STATE_DIR/files/lnk${TAB}unverified"
  rm -f -- "$STATE_DIR/files/u.txt"
  run compare "${ARGS[@]}" --run-rc 1
  out_line "退避コピーを消す: BASE= は unverified" "BASE=$STATE_DIR/files/u.txt${TAB}unverified"
}
case_H2() {
  CID=H2; TAG=""
  base_repo h2
  take "$R" task/t.md
  rc_is "(前提)take が exit 0" 0
  printf -- '- [x] one\n' >"$R/task/t.md"
  printf 'tampered\n' >"$STATE_DIR/taskmd-body"
  run taskmd-diff "${ARGS[@]}"
  rc_is "タスク MD の退避コピーを変える: taskmd-diff が 33" 33
  out_line "タスク MD の退避コピーを変える: taskmd-diff は REASON=taskmd-body" 'REASON=taskmd-body'
  out_lacks "タスク MD の退避コピーを変える: taskmd-diff は DOD= を出さない(基準を信頼できない)" 'DOD='
  run restore-taskmd "${ARGS[@]}"
  rc_is "タスク MD の退避コピーを変える: restore-taskmd が 33" 33
  out_line "タスク MD の退避コピーを変える: restore-taskmd は REASON=taskmd-body" 'REASON=taskmd-body'
  out_line "タスク MD の退避コピーを変える: RESTORED=no" 'RESTORED=no'
  out_line "タスク MD の退避コピーを変える: TOUCHED=none" 'TOUCHED=none'
  ckeq "タスク MD の退避コピーを変える: 実ツリーに触れない" "$(cat "$R/task/t.md")" '- [x] one'
}

# ════════════════════════ I taskmd-diff ════════════════════════
dod_is_original() { # $1=説明(直前の run の DOD= が起動前の内容を指す)
  local dod
  dod="$(sed -n 's/^DOD=//p' "$CASE_OUT")"
  if [ -n "$dod" ] && [ -f "$dod" ] && [ "$(cat "$dod")" = "$(printf '%s' "$TASK_BODY")" ]; then ok "$1"; else ng "$1(DOD=$dod)"; fi
}
case_I1() {
  CID=I1; TAG=""
  base_repo i1
  take "$R" task/t.md
  run taskmd-diff "${ARGS[@]}"
  rc_is "変化なし → 0" 0
  out_line "変化なし: TASKMD=same" 'TASKMD=same'
  out_line "変化なし: DOD= は保護領域の taskmd-body" "DOD=$STATE_DIR/taskmd-body"
  dod_is_original "変化なし: DOD= が起動前の内容を指す"
}
case_I2() {
  CID=I2; TAG=""
  base_repo i2
  take "$R" task/t.md
  printf '%s' "$TASK_BODY" | sed -e 's/^- \[ \] one$/- [x] one/' -e 's/^  \* \[ \] two$/  * [X] two/' \
    -e 's/^+ \[x\] three$/+ [ ] three/' -e 's/^- \[ \] テストが通る$/- [x] テストが通る/' >"$R/task/t.md"
  ckf "(前提)タスク MD が書き換わっている" cmp -s "$R/task/t.md" "$STATE_DIR/taskmd-body"
  run taskmd-diff "${ARGS[@]}"
  rc_is "チェックだけ更新(- [ ]→[x]・字下げ + * の [X]・+ の [x]→[ ])→ 0" 0
  out_line "チェックだけ更新: TASKMD=checks-only" 'TASKMD=checks-only'
  dod_is_original "チェックだけ更新: DOD= が起動前の内容を指す(外部が書いたタスク MD ではない)"
}
case_I3() {
  CID=I3; TAG="[類型:完了条件書き換え] "
  base_repo i3
  take "$R" task/t.md
  printf '%s' "$TASK_BODY" | sed 's/テストが通る/テストを書く/' >"$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "完了条件の書き換え → 34" 34
  out_line "完了条件の書き換え: TASKMD=body-changed" 'TASKMD=body-changed'
  dod_is_original "完了条件の書き換え: DOD= が起動前の内容を指す"
  printf '%s' "$TASK_BODY" | sed -e 's/テストが通る/テストを書く/' -e 's/^- \[ \] one$/- [x] one/' >"$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "完了条件の書き換え + チェックの更新 → 34(チェックに紛れさせても止める)" 34
  { printf '%s' "$TASK_BODY"; printf -- '- [x] 足された完了条件\n'; } >"$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "行の追加 → 34" 34
  printf '%s' "$TASK_BODY" | sed '/テストが通る/d' >"$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "行の削除 → 34" 34
  printf '%s' "$TASK_BODY" | sed 's/^- \[ \] one$/- [ ] one /' >"$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "空白だけの変更(行末)→ 34" 34
  printf '%s' "$TASK_BODY" | sed 's/^  \* \[ \] two$/ * [ ] two/' >"$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "空白だけの変更(字下げ)→ 34" 34
  write_task "$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "元の内容へ戻す → 0" 0
  out_line "元の内容へ戻す: TASKMD=same" 'TASKMD=same'
}
case_I4() { # 変異 (f) の検出先
  CID=I4; TAG=""
  base_repo i4
  take "$R" task/t.md
  printf '%s' "$TASK_BODY" | sed 's/^- \[ \] one$/- [-] one/' >"$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "[x] 以外の字への書き換え(- [ ] → - [-])→ 34" 34
  out_line "- [ ] → - [-]: TASKMD=body-changed" 'TASKMD=body-changed'
  printf '%s' "$TASK_BODY" | sed 's/^- \[ \] one$/- [a] one/' >"$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "[x] 以外の字への書き換え(- [ ] → - [a])→ 34" 34
  out_line "- [ ] → - [a]: TASKMD=body-changed" 'TASKMD=body-changed'
}
case_I5() {
  CID=I5; TAG=""
  base_repo i5
  mkdir -p "$R/notes"
  write_task "$R/notes/A.md"
  write_task "$R/notes/B.md"
  rm -f -- "$R/task/t.md"
  ln -s ../notes/A.md "$R/task/link.md"
  take "$R" task/link.md
  rc_is "(前提)symlink のタスク MD で take が exit 0" 0
  run taskmd-diff "${ARGS[@]}"
  rc_is "symlink のタスク MD: 変化なし → 0" 0
  ln -sfn ../notes/B.md "$R/task/link.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "symlink の張り替え(同内容の別の実体)→ 35" 35
  out_line "symlink の張り替え: TASKMD=mapping-changed" 'TASKMD=mapping-changed'
  out_line "symlink の張り替え: DOD= は出る" "DOD=$STATE_DIR/taskmd-body"
  rm -f -- "$R/task/link.md"
  ln -s ../notes/none.md "$R/task/link.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "リンク切れ → 35" 35
  rm -f -- "$R/task/link.md"
  write_task "$R/task/link.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "symlink → 同内容の通常ファイルへの置換 → 35" 35
  rm -f -- "$R/task/link.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "選択パスが消えた → 35" 35

  base_repo i5b
  take "$R" task/t.md
  cp -p "$R/task/t.md" "$R/moved.md"
  rm -f -- "$R/task/t.md"
  ln -s ../moved.md "$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "通常ファイル → 同内容を指す symlink への置換 → 35" 35
  out_line "通常ファイル → symlink: TASKMD=mapping-changed" 'TASKMD=mapping-changed'
  rm -f -- "$R/task/t.md"
  run taskmd-diff "${ARGS[@]}"
  rc_is "通常ファイルのタスク MD が消えた → 35" 35
  run compare "${ARGS[@]}" --run-rc 1
  out_line "通常ファイルのタスク MD が消えた: compare は untracked-removed" "CHANGE=untracked-removed${TAB}task/t.md"
}

# ════════════════════════ J restore-taskmd ════════════════════════
restored_ok() { # $1=説明 $2=復元先 [$3=期待する TOUCHED=(既定 deleted+copied。復元先がもともと
                #  無い経路は copied — 消していないものを消したと報告しないこと自体が検査の対象なので、
                #  どちらでも通る書き方にしない)](直前の run の結果を見る)
  local touched="${3:-deleted+copied}"
  rc_is "$1: exit 0" 0
  out_line "$1: RESTORED=yes" 'RESTORED=yes'
  out_line "$1: TOUCHED=$touched" "TOUCHED=$touched"   # 行全体の一致なので別の値では通らない
  ckeq "$1: 起動前の内容に戻る" "$(cat "$2")" "$(printf '%s' "$TASK_BODY")"
}
case_J1() {
  CID=J1; TAG=""
  base_repo j1
  GIT "$R" add task/t.md
  GIT "$R" commit -q -m task
  chmod 640 "$R/task/t.md"
  take "$R" task/t.md
  printf '%s' "$TASK_BODY" | sed 's/\[ \]/[x]/' >"$R/task/t.md"
  chmod 600 "$R/task/t.md"
  run restore-taskmd "${ARGS[@]}"
  restored_ok "外部がチェックを書き換えたタスク MD(追跡済み)" "$R/task/t.md"
  ckeq "外部がチェックを書き換えたタスク MD(追跡済み): モードも戻る" "$(mode_of "$R/task/t.md")" 640
  run taskmd-diff "${ARGS[@]}"
  out_line "復元の後は taskmd-diff が same" 'TASKMD=same'
}
case_J2() {
  CID=J2; TAG=""
  base_repo j2
  printf 'u\n' >"$R/u.txt"
  take "$R" task/t.md
  printf '%s' "$TASK_BODY" | sed 's/\[ \]/[x]/' >"$R/task/t.md"
  printf 'u by external\n' >"$R/u.txt"
  run restore-taskmd "${ARGS[@]}"
  restored_ok "外部がチェックを書き換えたタスク MD(未追跡)" "$R/task/t.md"
  ckeq "他の未追跡ファイルは復元しない(タスク MD だけを戻す)" "$(cat "$R/u.txt")" 'u by external'
  rm -f -- "$R/task/t.md"
  run restore-taskmd "${ARGS[@]}"
  # 復元先がもともと無いので削除は行わない(消していないものを deleted と報告しない)
  restored_ok "外部が消したタスク MD" "$R/task/t.md" copied
}
case_J3() {
  CID=J3; TAG=""
  base_repo j3
  mkdir -p "$R/notes"
  write_task "$R/notes/real.md"
  rm -f -- "$R/task/t.md"
  ln -s ../notes/real.md "$R/task/link.md"
  take "$R" task/link.md
  printf '%s' "$TASK_BODY" | sed 's/\[ \]/[x]/' >"$R/notes/real.md"
  run restore-taskmd "${ARGS[@]}"
  restored_ok "symlink 経由のタスク MD" "$R/notes/real.md"
  ckt "symlink 経由のタスク MD: 選択パスは symlink のまま" test -L "$R/task/link.md"
  ckeq "symlink 経由のタスク MD: リンク文字列は変わらない" "$(readlink "$R/task/link.md")" '../notes/real.md'
}
case_J4() { # 変異 (e) の検出先
  CID=J4; TAG=""
  base_repo j4
  take "$R" task/t.md
  mkdir -p "$WORK/j4-elsewhere"
  printf 'victim\n' >"$WORK/j4-elsewhere/t.md"
  mv "$R/task" "$R/task.orig"
  ln -s "$WORK/j4-elsewhere" "$R/task"
  run restore-taskmd "${ARGS[@]}"
  rc_is "復元先の親が symlink に差し替えられている → 33" 33
  out_line "復元先の親が symlink: RESTORED=no" 'RESTORED=no'
  out_line "復元先の親が symlink: TOUCHED=none" 'TOUCHED=none'
  out_line "復元先の親が symlink: REASON=dest-symlink" 'REASON=dest-symlink'
  ckeq "復元先の親が symlink: リンク先の同名ファイルが無傷" "$(cat "$WORK/j4-elsewhere/t.md" 2>/dev/null || true)" 'victim'
  rm -f -- "$R/task"
  mv "$R/task.orig" "$R/task"
  # 復元先そのものが symlink
  printf 'victim 2\n' >"$WORK/j4-victim2.md"
  rm -f -- "$R/task/t.md"
  ln -s "$WORK/j4-victim2.md" "$R/task/t.md"
  run restore-taskmd "${ARGS[@]}"
  rc_is "復元先そのものが symlink → 33" 33
  out_line "復元先そのものが symlink: TOUCHED=none" 'TOUCHED=none'
  out_line "復元先そのものが symlink: REASON=dest-symlink" 'REASON=dest-symlink'
  ckeq "復元先そのものが symlink: リンク先が無傷" "$(cat "$WORK/j4-victim2.md" 2>/dev/null || true)" 'victim 2'
}
case_J5() {
  CID=J5; TAG=""
  base_repo j5
  take "$R" task/t.md
  rm -f -- "$R/task/t.md"
  mkdir -p "$R/task/t.md"
  printf 'inner\n' >"$R/task/t.md/inner.txt"
  run restore-taskmd "${ARGS[@]}"
  rc_is "復元先がディレクトリに置き換えられている → 33" 33
  out_line "復元先がディレクトリ: TOUCHED=none" 'TOUCHED=none'
  out_line "復元先がディレクトリ: REASON=dest-dir" 'REASON=dest-dir'
  ckeq "復元先がディレクトリ: 中身に触れない" "$(cat "$R/task/t.md/inner.txt" 2>/dev/null || true)" 'inner'
}
case_J6() {
  CID=J6; TAG=""
  base_repo j6
  take "$R" task/t.md
  printf '%s' "$TASK_BODY" | sed 's/\[ \]/[x]/' >"$R/task/t.md"
  runp "$STUBS/cpfail" restore-taskmd "${ARGS[@]}"
  rc_is "削除の後でコピーだけが失敗する → 33" 33
  out_line "削除の後でコピーだけが失敗: RESTORED=no" 'RESTORED=no'
  out_line "削除の後でコピーだけが失敗: TOUCHED=deleted(実ツリーは既に変わっている)" 'TOUCHED=deleted'
  out_line "削除の後でコピーだけが失敗: REASON=copy-failed" 'REASON=copy-failed'
  ckf "削除の後でコピーだけが失敗: 実ツリーの当該エントリは無くなっている" test -e "$R/task/t.md"
  run restore-taskmd "${ARGS[@]}"
  # 前の起動が消したままなので、再実行では削除を行わない
  restored_ok "コピーの失敗の後、スタブなしで再実行すると戻る" "$R/task/t.md" copied
}

case_J7() {
  CID=J7; TAG=""
  base_repo j7
  take "$R" task/t.md
  printf '%s' "$TASK_BODY" | sed 's/\[ \]/[x]/' >"$R/task/t.md"
  if [ "$IS_ROOT" -eq 1 ]; then
    ok "同名エントリの削除に失敗する → 33 TOUCHED=none(root のため飛ばす — root は書き込み不可のディレクトリからも消せる)"
    return 0
  fi
  chmod 555 "$R/task"
  run restore-taskmd "${ARGS[@]}"
  chmod 755 "$R/task"
  rc_is "同名エントリの削除に失敗する(親ディレクトリが書き込み不可)→ 33" 33
  out_line "同名エントリの削除に失敗: TOUCHED=none" 'TOUCHED=none'
  out_line "同名エントリの削除に失敗: REASON=delete-failed" 'REASON=delete-failed'
  ckt "同名エントリの削除に失敗: 実ツリーのエントリは残る" test -f "$R/task/t.md"
}

# ════════════════════════ K cleanup ════════════════════════
case_K1() {
  CID=K1; TAG=""
  local n0
  base_repo k1
  printf 'u\n' >"$R/u.txt"
  n0="$(nstates)"
  take "$R" task/t.md
  ckeq "(前提)take が保護領域を 1 つ作る" "$(nstates)" "$((n0 + 1))"
  run cleanup --state "$STATE_DIR/"      # 末尾のスラッシュつきでも受ける
  rc_is "cleanup: exit 0" 0
  ckf "cleanup: 保護領域が消える" test -e "$STATE_DIR"
  ckeq "cleanup: 保護領域の数が元に戻る" "$(nstates)" "$n0"
  run cleanup --state "$STATE_DIR"
  rc_is "cleanup: 既に無い保護領域 → 2" 2
  run compare "${ARGS[@]}" --run-rc 0
  rc_is "cleanup の後の compare → 33(state-missing)" 33
}
case_K2() {
  CID=K2; TAG=""
  base_repo k2
  take "$R" task/t.md
  mkdir -p "$WORK/k2-out/guard-outside"
  printf 'keep\n' >"$WORK/k2-out/guard-outside/keep.txt"
  run cleanup --state "$WORK/k2-out/guard-outside"
  rc_is "保護領域の外のパス → 2" 2
  ckt "保護領域の外のパス: 何も消さない" test -f "$WORK/k2-out/guard-outside/keep.txt"
  ln -s "$STATE_DIR" "$PROT/dev-workflow/guard-link-k2"
  run cleanup --state "$PROT/dev-workflow/guard-link-k2"
  rc_is "symlink → 2" 2
  ckt "symlink: リンク先の保護領域は残る" test -f "$STATE_DIR/manifest.tsv"
  ckt "symlink: symlink 自体も残る" test -L "$PROT/dev-workflow/guard-link-k2"
  rm -f -- "$PROT/dev-workflow/guard-link-k2"
  mkdir -p "$PROT/dev-workflow/other-k2"
  run cleanup --state "$PROT/dev-workflow/other-k2"
  rc_is "保護領域の中でも guard-* でない名前 → 2" 2
  ckt "guard-* でない名前: 何も消さない" test -d "$PROT/dev-workflow/other-k2"
  rmdir -- "$PROT/dev-workflow/other-k2"
  run cleanup --state "$PROT/dev-workflow"
  rc_is "保護領域の親そのもの → 2" 2
  ckt "保護領域の親そのもの: 何も消さない" test -f "$STATE_DIR/manifest.tsv"
  run cleanup --state "$STATE_DIR"
  rc_is "正しい --state なら消える → 0" 0
}

# ════════════════════════ L 変異テスト ════════════════════════
MUT_TABLE=""
mkvariant() { # $1=出力パス 残り=sed 式(対象の写しに 1 回の sed でまとめて当てる)
  local o="$1" e args=()
  shift
  for e in "$@"; do args[${#args[@]}]="-e"; args[${#args[@]}]="$e"; done
  sed "${args[@]}" "$TARGET" >"$o"
}
mutant() { # $1=記号 $2=説明 $3=FAIL を期待するケース ID(空白区切り)残り=sed 式
  local id="$1" desc="$2" expect="$3" v out n c rc=0 only total per firsts t0="$SECONDS"
  shift 3
  v="$WORK/mut/$id.sh"
  out="$WORK/mut/$id.out"
  n="$(grep -c -- "# MUT:$id\$" "$TARGET" || true)"
  ckeq "変異($id) 目印 # MUT:$id が対象に 1 行だけ在る" "$n" 1
  mkvariant "$v" "$@"
  ckf "変異($id) sed が空振りしていない(写しが対象と違う)" cmp -s "$TARGET" "$v"
  ckt "変異($id) 写しが bash -n を通る" bash -n "$v"
  only="$(printf '%s' "$expect" | tr ' ' ','),E1"
  guard_t "$T_MUT" env HOME="$REAL_HOME" IMPLEMENT_GUARD="$v" IMPLEMENT_GUARD_SELFTEST_PROT="$PROT" \
    bash "$SELF_PATH" --only "$only" </dev/null >"$out" 2>&1 || rc=$?
  if [ "$VERBOSE" -eq 1 ]; then echo "=== [L] 変異($id) の子スイート → rc=$rc"; cat "$out"; fi
  # 子スイートが時間切れで落ちた非ゼロは「変異を検出した」ではない
  case "$rc" in
    124|137) ng "変異($id) $desc: 子スイートが外側タイムアウトしていない(rc=$rc・$T_MUT 秒で打ち切られた。実行が遅い可能性)" ;;
    *) : ;;
  esac
  ckne "変異($id) $desc: スイートが非ゼロで終わる" "$rc" 0
  for c in $expect; do
    if grep -q -- "^FAIL  $c " "$out"; then ok "変異($id) $desc: ケース $c が FAIL する"
    else ng "変異($id) $desc: ケース $c が FAIL する(FAIL の行が無い)"; fi
  done
  if grep -q -- '^PASS  E1 ' "$out" && ! grep -q -- '^FAIL  E1 ' "$out"; then ok "変異($id) 対照ケース E1 は写しでも PASS する(壊れ方が変異に固有)"
  else ng "変異($id) 対照ケース E1 は写しでも PASS する"; fi
  # 表: ケース ID ごとの FAIL 件数と、各ケースで最初に FAIL した行(全文は -v で出る)
  total="$(grep -c -- '^FAIL  ' "$out" || true)"
  per=""
  firsts=""
  for c in $(grep -- '^FAIL  ' "$out" | cut -d' ' -f3 | LC_ALL=C sort -u); do
    per="$per $c=$(grep -c -- "^FAIL  $c " "$out" || true)"
    firsts="$firsts
      $(grep -m 1 -- "^FAIL  $c " "$out" | sed 's/^FAIL  //')"
  done
  MUT_TABLE="$MUT_TABLE
  ($id) $desc
      → 子スイートの終了コード $rc / FAIL $total 件(${per# })/ $((SECONDS - t0)) 秒${firsts:-
      (FAIL したケースなし)}"
}
case_L() {
  CID=L; TAG=""
  local lb la
  mutant a "ダイジェスト照合を外す" "G1 G2 G3" '/# MUT:a$/d'
  mutant b "hooks / config の検査を git の再取得より後ろへ動かす" "G7" '/# MUT:b$/{h;d;}' '/# ANCHOR:retake$/G'
  lb="$(grep -n -- '# MUT:b$' "$WORK/mut/b.sh" | cut -d: -f1 | head -n 1)"
  la="$(grep -n -- '# ANCHOR:retake$' "$WORK/mut/b.sh" | cut -d: -f1 | head -n 1)"
  if [ -n "$lb" ] && [ -n "$la" ] && [ "$lb" -eq "$((la + 1))" ]; then ok "変異(b) 検査の行が再取得の行の直後へ動いている(消えただけではない)"
  else ng "変異(b) 検査の行が再取得の行の直後へ動いている(検査: ${lb:-無し} 行 / 再取得: ${la:-無し} 行)"; fi
  mutant c "前置きの -c core.fsmonitor= を外す" "B3" '/# MUT:c$/d'
  mutant d "使い捨て index をやめる" "B5" '/# MUT:d$/s/GIT_INDEX_FILE="[^"]*" //'
  mutant e "復元先の階層検査を外す" "J4" '/# MUT:e$/d'
  mutant f "正規化で [xX] 以外も潰す" "I4" '/# MUT:f$/s/\[xX\]/./'
  mutant g ".claude/reviews/ の除外をタスク MD より優先する" "E9" '/# MUT:g$/d'
  mutant h "unborn で空ツリーに切り替えない" "D1" '/# MUT:h$/d'
  mutant i "上限検査を外す" "C7" '/# MUT:i$/d'
  mutant j "hooks ディレクトリの解決に -c core.hooksPath=/dev/null つきの前置きを使う" "D2 D3" '/# MUT:j$/s/GIT_PRE_HOOKS/GIT_PRE/'
  mutant k "タスク MD の退避の失敗を u で続行する" "C1" '/# MUT:k$/d'
  mutant l "① から -c diff.autoRefreshIndex=false を外す" "B1" '/# MUT:l$/d'
  mutant m "保護領域の候補の .. の正規化を外す" "C9 C10" '/# MUT:m$/d'
}

# ════════════════════════ 実行 ════════════════════════
ALL_CASES="A1 A2 A3 A4 A5 B1 B2 B3 B4 B5 B6 B7 B8 B9 B10 B11 B12 C1 C2 C3 C4 C5 C6 C7 C8 C9 C10 D1 D2 D3 D4 D5 D6
E1 E2 E3 E4 E5 E6 E7 E8 E9 E10 F1 F2 F3 F4 F5 F6 G1 G2 G3 G4 G5 G6 G7 G8 H1 H2 I1 I2 I3 I4 I5
J1 J2 J3 J4 J5 J6 J7 K1 K2 L"
want() { # $1=ケース ID → 0 なら実行する(--only の 1 文字は群の全体、それ以外は完全一致)
  local id
  [ -n "$ONLY" ] || return 0
  for id in ${ONLY//,/ }; do
    if [ "$id" = "$1" ]; then return 0; fi
    if [ "${#id}" -eq 1 ] && [ "$id" != L ] && [ "${1:0:1}" = "$id" ]; then return 0; fi
  done
  return 1
}
if [ -n "$ONLY" ]; then
  for id in ${ONLY//,/ }; do
    hit=0
    for c in $ALL_CASES; do
      if [ "$id" = "$c" ] || { [ "${#id}" -eq 1 ] && [ "${c:0:1}" = "$id" ]; }; then hit=1; fi
    done
    [ "$hit" -eq 1 ] || { echo "ERROR: --only のケース ID が無い: $id" >&2; exit 1; }
  done
fi
START_SECONDS="$SECONDS"
for c in $ALL_CASES; do
  if want "$c"; then "case_$c"; fi
done

echo
printf '%s\n' "$RESULTS"
if [ -n "$MUT_TABLE" ]; then
  echo
  echo "変異テスト(変異ごとに、写しを対象にした子スイートで FAIL したケース):"
  printf '%s\n' "$MUT_TABLE"
fi
echo
echo "所要時間: $((SECONDS - START_SECONDS)) 秒"
echo "結果: PASS ${PASS} 件 / FAIL ${FAIL} 件"
[ "$FAIL" -eq 0 ]
