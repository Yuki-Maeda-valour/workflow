#!/bin/sh
# 絶対パスをビルドする機械によらない名前に置き換える。
set -eu

native_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
repo_root=$(dirname -- "$native_root")
cargo_root=${CARGO_HOME:-"$HOME/.cargo"}
compiler=$1
shift

set -- "$@" "--remap-path-prefix=$repo_root=/dev-workflow" \
    "--remap-path-prefix=$cargo_root=/cargo"
if [ -n "${CARGO_TARGET_DIR:-}" ]; then
    set -- "$@" "--remap-path-prefix=$CARGO_TARGET_DIR=/target"
fi
if [ -n "${OUT_DIR:-}" ]; then
    set -- "$@" "--remap-path-prefix=$OUT_DIR=/out"
fi
exec "$compiler" "$@"
