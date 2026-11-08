#!/bin/sh
# 共通の入口。設定と配布物を確かめた後、選んだ 1 本だけを起動する。

fail() {
    printf 'ERROR [%s] %s\n' "$1" "$2" >&2
    exit "$3"
}

# 未設定と空文字は別の設定として扱う。旧版では Rust 用の道具を使わない。
if [ "${DEV_WORKFLOW_ENGINE+x}" != x ]; then
    engine=legacy
else
    engine=$DEV_WORKFLOW_ENGINE
fi
case $engine in
    legacy)
        [ "${1-}" = resolve-task-dir ] ||
            fail legacy-unavailable '旧版の未登録コマンドです。試用には DEV_WORKFLOW_ENGINE=rust を指定してください。' 2
        ;;
    rust) ;;
    *) fail engine 'DEV_WORKFLOW_ENGINE は未設定・legacy・rust のいずれかにしてください。' 2 ;;
esac

# cd は子のシェルで行い、呼び出した人の作業場所と CDPATH を変えない。
case $0 in
    */*) parent=${0%/*}; [ -n "$parent" ] || parent=/ ;;
    *) parent=. ;;
esac
case $parent in /*) ;; *) parent=./$parent ;; esac
bundle=$(CDPATH='' cd -P "$parent" 2>/dev/null && pwd -P) ||
    fail location '入口の物理的な位置を読み取れません。' 1

if [ "$engine" = legacy ]; then
    legacy=$bundle/../../skills/create-task/scripts/resolve-task-dir.py
    [ -f "$legacy" ] && [ -r "$legacy" ] ||
        fail legacy-script '旧版のスクリプトが無いか、読み取れません。' 1
    command -v python3 >/dev/null 2>&1 || fail python 'python3 がありません。' 1
    shift
    exec python3 -B "$legacy" "$@"
fi

os=$(uname -s 2>/dev/null) || fail platform 'OS を読み取れません。' 1
cpu=$(uname -m 2>/dev/null) || fail platform 'CPU を読み取れません。' 1
case $os/$cpu in
    Linux/x86_64) relative=linux-x86_64/dev-workflow ;;
    Linux/aarch64|Linux/arm64) relative=linux-arm64/dev-workflow ;;
    Darwin/x86_64|Darwin/arm64) relative=macos-universal/dev-workflow ;;
    *) fail platform 'この OS と CPU の組み合わせは使えません。' 1 ;;
esac

case $os in
    Linux) version=$(uname -r 2>/dev/null) || fail os-version 'Linux カーネルの版を読み取れません。' 1 ;;
    Darwin) version=$(sw_vers -productVersion 2>/dev/null) || fail os-version 'macOS の版を読み取れません。' 1 ;;
esac
# 数字をシェルの式として評価しない。Linux の配布元による接尾辞も許す。
if ! printf '%s\n' "$version" | LC_ALL=C awk -v os="$os" '
    NR != 1 { bad = 1 }
    {
        if (os == "Linux") {
            if ($0 !~ /^[0-9]+\.[0-9]+(\.[0-9]+)*([-+][a-zA-Z0-9_.+-]*)?$/) bad = 1
            split($0, parts, /[.+-]/)
            if (parts[1] + 0 < 5 || (parts[1] + 0 == 5 && parts[2] + 0 < 6)) bad = 1
        } else {
            if ($0 !~ /^[0-9]+(\.[0-9]+)*$/) bad = 1
            split($0, parts, /\./)
            if (parts[1] + 0 < 11) bad = 1
        }
    }
    END { if (bad || NR != 1) exit 1 }
' 2>/dev/null; then
    fail os-version 'OS の版を確認できないか、下限未満です。Linux 5.6 以上・macOS 11 以上が必要です。' 1
fi

checksums=$bundle/SHA256SUMS
if [ -L "$checksums" ] || [ ! -f "$checksums" ] || [ ! -r "$checksums" ]; then
    fail manifest 'SHA256SUMS はリンクでない、読み取り可能な通常ファイルが必要です。' 1
fi
for directory in linux-x86_64 linux-arm64 macos-universal; do
    if [ -L "$bundle/$directory" ] || [ -L "$bundle/$directory/dev-workflow" ]; then
        fail symlink '配布物内の OS と CPU のフォルダ・実行ファイルにリンクは使えません。' 1
    fi
done
binary=$bundle/$relative
if [ ! -f "$binary" ] || [ ! -x "$binary" ]; then
    fail binary '選んだ実行ファイルが無いか、通常ファイルでないか、実行できません。' 1
fi

# 固定 3 行の合計は 278 バイト。awk が区別しない末尾改行の欠落も拒否する。
size=$(wc -c < "$checksums" 2>/dev/null) || fail manifest 'SHA256SUMS を読み取れません。' 1
if ! [ "$size" -eq 278 ] 2>/dev/null; then
    fail manifest 'SHA256SUMS は固定の 3 行・小文字 SHA256・空白 2 個・LF の形が必要です。' 1
fi
expected=$(LC_ALL=C awk -v selected="$relative" '
    BEGIN {
        paths[1] = "linux-x86_64/dev-workflow"
        paths[2] = "linux-arm64/dev-workflow"
        paths[3] = "macos-universal/dev-workflow"
    }
    {
        hash = substr($0, 1, 64)
        if (NR > 3 || length($0) != 66 + length(paths[NR]) ||
            hash ~ /[^0-9a-f]/ || substr($0, 65) != "  " paths[NR]) bad = 1
        if (paths[NR] == selected) selected_hash = hash
    }
    END {
        if (bad || NR != 3 || length(selected_hash) != 64) exit 1
        print selected_hash
    }
' "$checksums" 2>/dev/null) ||
    fail manifest 'SHA256SUMS は固定の 3 行・小文字 SHA256・空白 2 個・LF の形が必要です。' 1

if [ "$os" = Linux ] && command -v sha256sum >/dev/null 2>&1; then
    hash_tool=sha256sum
elif [ "$os" = Darwin ] && command -v shasum >/dev/null 2>&1; then
    hash_tool=shasum
elif command -v openssl >/dev/null 2>&1; then
    hash_tool=openssl
else
    fail hash-tool 'SHA256 の道具がありません。Linux は sha256sum、Mac は shasum、代わりに openssl が必要です。' 1
fi

hash_input() {
    case $hash_tool in
        sha256sum) sha256sum ;;
        shasum) shasum -a 256 ;;
        openssl) openssl dgst -sha256 ;;
    esac
    hash_status=$?
    # POSIX sh のパイプでも道具の終了結果を確かめる。出力はシェル変数に
    # 入れず、そのまま awk へ渡す(NUL や余分な末尾改行を取り除かない)。
    printf '\nlaunch-status:%s\n' "$hash_status"
}
actual=$(hash_input 2>/dev/null < "$binary" | LC_ALL=C awk -v tool="$hash_tool" '
    NR == 1 {
        if (tool == "openssl") {
            if ($0 !~ /^SHA(2-)?256\(stdin\)= /) bad = 1
            sub(/^SHA(2-)?256\(stdin\)= /, "")
            hash = $0
        } else {
            if (substr($0, 65) != "  -" && substr($0, 65) != " *-") bad = 1
            hash = substr($0, 1, 64)
        }
        if (length(hash) != 64 || hash ~ /[^0-9a-fA-F]/) bad = 1
    }
    NR == 2 {
        if ($0 == "") gap = 1
        else if ($0 == "launch-status:0") success = 1
        else bad = 1
    }
    NR == 3 {
        if (gap && $0 == "launch-status:0") success = 1
        else bad = 1
    }
    NR > 3 { bad = 1 }
    END {
        if (bad || !success) exit 1
        print tolower(hash)
    }
' 2>/dev/null) || fail hash 'SHA256 の計算に失敗したか、道具の出力が不正です。' 1
if [ "$actual" != "$expected" ]; then
    fail hash-mismatch '実行ファイルの SHA256 が SHA256SUMS と一致しません。確認済みの配布物を入れ直してください。' 1
fi

# 引数の境界・作業場所・標準入出力・実行後の終了結果をそのまま渡す。
exec "$binary" "$@"
