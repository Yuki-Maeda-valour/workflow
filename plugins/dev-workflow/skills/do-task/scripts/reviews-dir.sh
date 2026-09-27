#!/usr/bin/env bash
# `.claude/reviews/` の作成と、基準時点の未追跡一覧の保存(dev-workflow)。
# 手順と停止の条件の正本は ../references/base-commit.md の ①〜③(設計は Issue #68 の D22 ②)。
# base-commit.md と do-task Phase 0 の手順 5 は、この 2 つの作業をこのスクリプトに寄せて呼ぶ(対話でも無人でも同じ)。
# スクリプトの中の書き込みは、ホストの保護パスの検査(リダイレクト先と、編集の自動許可が許すファイル操作の
# コマンドの引数)に掛からない。
#
# 使い方:
#   bash reviews-dir.sh ensure         --root <管理ルート>
#   bash reviews-dir.sh save-untracked --top <TOP> --root <管理ルート> --list <LIST>
#
# サブコマンド:
#   ensure          <管理ルート>/.claude と <管理ルート>/.claude/reviews を、階層ごとに「検査 → 無ければ作成」の
#                   順で用意する(base-commit.md ①)
#   save-untracked  ensure を行ってから、`git -C <TOP> ls-files -o --exclude-standard -z` の一覧を <LIST> へ
#                   原子的に公開し、stdout に一覧の sha256 を出す(base-commit.md ②・③)。
#                   <LIST> は <管理ルート>/.claude/reviews/ の直下のファイル
#   -h / --help     この使い方を出す
#
# 終了コード: 0=成功 2=使い方の誤り 3=検査で止まる(symlink・通常のディレクトリでない・<LIST> が symlink か
#             通常ファイルでない) 4=git の失敗 20=内部の失敗(公開の失敗を含む)・sha256 を計算する道具が無い
#             (tool-missing)。止まるときは stderr に "ERROR [理由コード] 説明"。
#             止まったときは何も公開しない(一時ファイルは消す)
#
# git は base-commit.md と同じ前置きで打ち、GIT_NO_LAZY_FETCH=1 を自分で付ける。呼び出し側が渡した
# 承認済みのフィルタを無効にする GIT_CONFIG_COUNT・GIT_CONFIG_KEY_<n>・GIT_CONFIG_VALUE_<n> は外さない。
# --- end usage ---
set -eEuo pipefail
unset CDPATH

export GIT_NO_LAZY_FETCH=1
GIT_PRE=(--no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false)

TMP=""
cleanup() { if [ -n "$TMP" ]; then rm -f -- "$TMP"; fi; }
trap cleanup EXIT
trap 'ec=$?; echo "ERROR [internal] 予期しない失敗(終了コード $ec・行 $LINENO)" >&2; trap - ERR; exit 20' ERR

usage() { sed -n '2,/^# --- end usage ---$/p' "$0" | sed '$d' >&2; }
fail_usage() { echo "ERROR [usage] $*" >&2; usage; trap - ERR; exit 2; }
die() { # $1=終了コード $2=理由コード 残り=説明
  local code="$1" reason="$2"
  shift 2
  echo "ERROR [$reason] $*" >&2
  trap - ERR
  exit "$code"
}
need_val() { if [ "$2" -lt 2 ]; then fail_usage "$1 に値が必要です"; fi; }

# sha256 の道具の解決順と取り出し方は diff-snapshot.sh と同じ(sha256sum → shasum -a 256 → openssl dgst -sha256)
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

# base-commit.md ①: 階層ごとに「検査 → 無ければ作成」(`mkdir -p` を先に呼ばない。symlink を辿って外に作らない)
ensure_dirs() { # $1=管理ルート
  local d
  for d in "$1/.claude" "$1/.claude/reviews"; do
    if [ -L "$d" ]; then
      die 3 symlink "$d が symlink(辿らずに止まる)"
    fi
    if [ ! -e "$d" ]; then
      mkdir -- "$d" || die 20 internal "$d を作れない"
    elif [ ! -d "$d" ]; then
      die 3 not-directory "$d が通常のディレクトリでない"
    fi
  done
}

[ $# -gt 0 ] || fail_usage "サブコマンドが必要です"
SUB="$1"
shift
ROOT=""
TOP=""
LIST=""
case "$SUB" in
  -h|--help) usage; exit 0 ;;
  ensure|save-untracked) : ;;
  *) fail_usage "不明なサブコマンド: $SUB" ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    --root) need_val "$1" "$#"; ROOT="$2"; shift 2 ;;
    --top) need_val "$1" "$#"; TOP="$2"; shift 2 ;;
    --list) need_val "$1" "$#"; LIST="$2"; shift 2 ;;
    *) fail_usage "不明な引数: $1" ;;
  esac
done
[ -n "$ROOT" ] || fail_usage "--root が必要です"
[ -d "$ROOT" ] || fail_usage "--root がディレクトリでない: $ROOT"

if [ "$SUB" = ensure ]; then
  [ -z "$TOP" ] && [ -z "$LIST" ] || fail_usage "ensure は --root だけを受け付ける"
  ensure_dirs "$ROOT"
  exit 0
fi

[ -n "$TOP" ] || fail_usage "--top が必要です"
[ -n "$LIST" ] || fail_usage "--list が必要です"
[ -d "$TOP" ] || fail_usage "--top がディレクトリでない: $TOP"
# LIST は <管理ルート>/.claude/reviews/ の直下(一時ファイルと同じディレクトリから rename(2) で公開するので、公開は原子的。② の検査の後に置き換えられて、公開先がディレクトリになっていれば失敗し、symlink になっていればリンクを辿らずにリンク自体を置き換える)
REVIEWS="$ROOT/.claude/reviews"
case "$LIST" in
  "$REVIEWS"/*) : ;;
  *) fail_usage "--list は $REVIEWS/ の直下のファイル: $LIST" ;;
esac
NAME="${LIST#"$REVIEWS"/}"
case "$NAME" in
  ""|*/*|.|..) fail_usage "--list は $REVIEWS/ の直下のファイル: $LIST" ;;
esac

ensure_dirs "$ROOT"
# base-commit.md ②: 既存のエントリが symlink か通常ファイル以外なら、何も書かずに止まる(FIFO で待たない・
# symlink を辿って外を上書きしない)
if [ -e "$LIST" ] || [ -L "$LIST" ]; then
  if [ -L "$LIST" ] || [ ! -f "$LIST" ]; then
    die 3 list-not-regular "$LIST が symlink か通常ファイルでない(何も書かずに止まる)"
  fi
fi
# 公開の手段と sha256 の道具は、一時ファイルを作る前に解決する(sha256 の道具が無ければ何も書かずに止まる)。
# 公開は rename(2) で行う: python3 が起動できれば os.replace、起動できなければ mv -f -T。
# mv が -T を持つかは前もって判定しない(--help の字面は実装ごとに違う)。持たなければ公開で失敗して止まる
PUBLISH=mv
if python3 -I -c 'import os' >/dev/null 2>&1; then PUBLISH=os.replace; fi
resolve_sha_cmd
[ -n "$SHA_CMD" ] || die 20 tool-missing "sha256 を計算する道具が無い(sha256sum・shasum・openssl のどれか)"
# base-commit.md ③: 一時ファイルへ書き、git の終了コードを確かめ、sha256 を一時ファイルから計算してから公開する
TMP="$(mktemp "$REVIEWS/.base-untracked.XXXXXX")" || die 20 internal "一時ファイルを作れない: $REVIEWS"
rc=0
git -C "$TOP" "${GIT_PRE[@]}" ls-files -o --exclude-standard -z >"$TMP" || rc=$?
[ "$rc" -eq 0 ] || die 4 git "git ls-files が失敗した(終了コード $rc。何も公開しない)"
SHA="$(sha256_file "$TMP")"
# 公開に失敗したら、失敗の理由(stderr の最初の 1 行)を添えて止まる。-I は利用者の site と PYTHON* の環境変数を
# 読まない。-c の後の引数は、- で始まってもパスとして渡る
NL=$'\n'
if [ "$PUBLISH" = os.replace ]; then
  reason="$(python3 -I -c 'import os, sys
try:
    os.replace(sys.argv[1], sys.argv[2])
except OSError as e:
    sys.stderr.write("%s\n" % (e.strerror or e))
    sys.exit(1)' "$TMP" "$LIST" 2>&1)" || die 20 internal "一覧を公開できない: $LIST(${reason%%"$NL"*})"
else
  reason="$(mv -f -T -- "$TMP" "$LIST" 2>&1)" \
    || die 20 internal "一覧を公開できない: $LIST(${reason%%"$NL"*}。python3 が無いので mv -f -T で公開した。-T を持たない mv では python3 が要る)"
fi
TMP=""
printf '%s\n' "$SHA"
