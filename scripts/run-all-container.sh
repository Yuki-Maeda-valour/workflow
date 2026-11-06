#!/usr/bin/env bash
# 全回帰テスト(python3 -B scripts/run-all.py)を、新しい git の入った容器で流す入口。
#
# 使い方(リポジトリのどこから実行してもよい):
#   bash scripts/run-all-container.sh [--commit <rev> | --worktree] [--image <名前>] [--cpuset-cpus <一覧>]
#                                     [--name <名前>] [--timeout <秒>] [--log-dir <ホストのフォルダ>] [-- <run-all.py の引数>…]
#
# - 既定は `--commit HEAD`: `git archive <rev>` を標準入力で容器へ渡す。commit していない変更は混ぜない。
# - `--worktree`: 追跡しているファイルと、無視されていない未追跡のファイル(実在するものだけ)を渡す。
#   無視されたファイル(.env など)は渡さない。
# - 容器の中で、中身を ~/w に展開し、git init と 1 回の commit をしてから(review-guard.py の検証用のコピーと同じ形)、
#   `timeout <秒> python3 -B scripts/run-all.py <引数>` を流す。`--timeout 0` なら timeout を付けない(既定 3600)。
# - イメージは、Dockerfile と requirements-dev.txt の中身の sha256 の先頭 12 桁を名前に入れる
#   (workflow-test:run-all-<12 桁>)。無ければ作る。`--image` を渡せば、そのイメージを使う(作らない)。
# - 重みの記録は、ホストの ${XDG_CACHE_HOME:-~/.cache}/workflow-run-all を容器へ取り付けて、回をまたいで使う。
# - ホストの環境変数のうち、LOOP_SELFTEST_TIMEOUT・RUN_TIMEOUT・LOOP_STARTUP_CASE_TIMEOUT の 3 つだけを、
#   ホストで設定されているものに限って容器の run-all.py へ渡す(`docker run -e NAME`。値はコマンド行に出さない)。
#   ほかの環境変数は渡さない。渡した名前は、`渡す環境変数: <名前の並び>`(無ければ `なし`)の行で出す。
# - docker の既定の context が違う機械では、`DOCKER_CONTEXT=<名前> bash scripts/run-all-container.sh …` で流す。
#
# 終了コード: 容器の中のコマンドの終了コード(時間切れは 124)。引数の誤りは 2。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

die_usage() {
  echo "ERROR: $1" >&2
  echo "使い方は bash scripts/run-all-container.sh --help を見る" >&2
  exit 2
}

MODE=""
REV="HEAD"
IMAGE=""
CPUSET=""
NAME=""
TIMEOUT=3600
LOG_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --commit)
      [ $# -ge 2 ] || die_usage "--commit に値が必要"
      [ -z "$MODE" ] || die_usage "--commit と --worktree は、どちらか 1 つだけ"
      MODE=commit; REV="$2"; shift 2 ;;
    --worktree)
      [ -z "$MODE" ] || die_usage "--commit と --worktree は、どちらか 1 つだけ"
      MODE=worktree; shift ;;
    --image)
      [ $# -ge 2 ] || die_usage "--image に値が必要"
      IMAGE="$2"; shift 2 ;;
    --cpuset-cpus)
      [ $# -ge 2 ] || die_usage "--cpuset-cpus に値が必要"
      CPUSET="$2"; shift 2 ;;
    --name)
      [ $# -ge 2 ] || die_usage "--name に値が必要"
      NAME="$2"; shift 2 ;;
    --timeout)
      [ $# -ge 2 ] || die_usage "--timeout に値が必要"
      TIMEOUT="$2"; shift 2 ;;
    --log-dir)
      [ $# -ge 2 ] || die_usage "--log-dir に値が必要"
      LOG_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    *) die_usage "不明な引数: $1" ;;
  esac
done
MODE="${MODE:-commit}"
case "$TIMEOUT" in ''|*[!0-9]*) die_usage "--timeout は 0 以上の整数(秒)にする: $TIMEOUT" ;; esac
if [ -n "$LOG_DIR" ]; then
  [ -d "$LOG_DIR" ] || die_usage "--log-dir のフォルダが無い: $LOG_DIR"
  LOG_DIR="$(cd "$LOG_DIR" && pwd)"
fi
if [ "$MODE" = commit ]; then
  git -C "$ROOT" rev-parse --verify --quiet "$REV^{commit}" >/dev/null || die_usage "--commit の rev が commit にならない: $REV"
fi
command -v docker >/dev/null || { echo "ERROR: docker が無い" >&2; exit 1; }

DOCKERFILE="$ROOT/scripts/container/Dockerfile"
REQUIREMENTS="$ROOT/requirements-dev.txt"

# イメージ: 指定が無ければ、Dockerfile と requirements-dev.txt の中身から名前を決め、無ければ作る。
if [ -z "$IMAGE" ]; then
  digest="$(cat "$DOCKERFILE" "$REQUIREMENTS" | sha256sum | cut -c1-12)"
  IMAGE="workflow-test:run-all-$digest"
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    context="$(mktemp -d)"
    trap 'rm -rf "$context"' EXIT
    # リポジトリ全体は送らず、Dockerfile と requirements-dev.txt だけを置いて作る。
    cp "$DOCKERFILE" "$context/Dockerfile"
    cp "$REQUIREMENTS" "$context/requirements-dev.txt"
    echo "イメージを作る: $IMAGE" >&2
    docker build -t "$IMAGE" "$context" >&2
  fi
fi
echo "イメージ: $IMAGE"

# 容器へ渡す中身(tar)を標準出力へ出す。
emit_archive() {
  if [ "$MODE" = commit ]; then
    git -C "$ROOT" archive "$REV"
  else
    # 追跡しているファイルと、無視されていない未追跡のファイルのうち、実在するパスだけ(消した追跡ファイルを除く)。
    git -C "$ROOT" ls-files -z --cached --others --exclude-standard | {
      while IFS= read -r -d '' path; do
        if [ -e "$ROOT/$path" ] || [ -L "$ROOT/$path" ]; then printf '%s\0' "$path"; fi
      done
    } | tar -C "$ROOT" --null --no-recursion -T - -cf -
  fi
}

args=(--rm -i --init --tmpfs /tmp:rw,exec,nosuid,nodev,size=16g)
[ -z "$CPUSET" ] || args+=(--cpuset-cpus "$CPUSET")
[ -z "$NAME" ] || args+=(--name "$NAME")

# 重みの記録: ホストのフォルダを取り付ける(作れなければ、容器の中だけで記録する)。
cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/workflow-run-all"
if mkdir -p "$cache_dir" 2>/dev/null; then
  args+=(-v "$cache_dir:/home/tester/.cache/workflow-run-all")
else
  echo "注意: 重みの記録のフォルダを作れない($cache_dir)。容器の中だけで記録する"
fi
# ホストの環境変数のうち、決まった 3 つだけを渡す(設定されているものだけ。値はコマンド行に出さない)。
passed=()
for name in LOOP_SELFTEST_TIMEOUT RUN_TIMEOUT LOOP_STARTUP_CASE_TIMEOUT; do
  if [ -n "${!name+x}" ]; then
    args+=(-e "$name")
    passed+=("$name")
  fi
done
echo "渡す環境変数: ${passed[*]:-なし}"
run_args=("$@")
if [ -n "$LOG_DIR" ]; then
  args+=(-v "$LOG_DIR:/home/tester/run-all-logs")
  run_args+=(--log-dir /home/tester/run-all-logs)
fi

# 容器の中で流す手順。第 1 引数は timeout の秒数(0 なら付けない)、残りは run-all.py の引数。
read -r -d '' INNER <<'SCRIPT' || true
set -euo pipefail
limit="$1"; shift
mkdir "$HOME/w"
tar -x -C "$HOME/w"
cd "$HOME/w"
# 検証用のコピーと同じ形にする(review-guard.py の run-checks)。
git init -q
git add -f --all
git -c user.name=run-all -c user.email=run-all@example.invalid -c core.hooksPath=/dev/null \
    -c maintenance.auto=false -c gc.auto=0 commit -qm "run-all"
mkdir -p "$HOME/.local/state/dev-workflow"
git --version
python3 --version
echo "容器の /tmp: $(findmnt -no FSTYPE,OPTIONS /tmp)"
probe="$(mktemp /tmp/exec-probe.XXXXXX)"
printf '#!/bin/sh\nexit 0\n' >"$probe"
chmod +x "$probe"
if "$probe"; then
  echo "容器の /tmp での実行: できる"
  rm -f "$probe"
else
  echo "容器の /tmp での実行: できない"
  exit 1
fi
if [ "$limit" = 0 ]; then
  exec python3 -B scripts/run-all.py "$@"
fi
exec timeout "$limit" python3 -B scripts/run-all.py "$@"
SCRIPT

# 中身を作る側の失敗を見逃さないように、両方の終了コードを取る。
set +e
emit_archive | docker run "${args[@]}" "$IMAGE" bash -c "$INNER" run-all "$TIMEOUT" ${run_args[@]+"${run_args[@]}"}
codes=("${PIPESTATUS[@]}")
set -e
container="${codes[$((${#codes[@]} - 1))]}"
producer=0
for ((i = 0; i < ${#codes[@]} - 1; i++)); do
  [ "${codes[$i]}" -eq 0 ] || producer="${codes[$i]}"
done
if [ "$container" -ne 0 ]; then
  exit "$container"
fi
if [ "$producer" -ne 0 ]; then
  echo "ERROR: 容器へ渡す中身を作れなかった(終了コード $producer)" >&2
  exit 1
fi
