#!/usr/bin/env bash
# loop.sh の回帰テスト(スタブのみ。実物のホスト CLI を起動しない。ネットワーク不要)。
# 契約の正本は ../references/loop.md(設計は Issue #68 の完了条件 2)。
#
# 使い方:
#   bash loop-selftest.sh            # 全ケース実行
#   bash loop-selftest.sh -v         # 失敗したケースの出力も表示
#   LOOP_SH=<パス> bash loop-selftest.sh   # 別の実装を対象にする(変異テスト用)
#   LOOP_PERMISSION=<パス> / REVIEWS_DIR_SH=<パス>   # 許可の仲介の hook・reviews-dir.sh を差し替える(同上)
#
# 安全策:
#   - ホスト CLI はスタブ(PATH の先頭の `claude`)だけを使う。loop.sh を打つ前に毎回、渡す PATH で
#     `command -v claude` がスタブを指す(か、どこにも無い)ことを確かめ、違えば全体を中止する
#   - loop.sh は `env -i` で起動する(CLAUDECODE などのホストの指標と DEV_WORKFLOW_HOST_CLI を外す。
#     判定そのものを確かめる項目だけ、指標を足して打つ)。HOME・XDG_STATE_HOME・GIT_CONFIG_GLOBAL・
#     GIT_CONFIG_SYSTEM は scratch へ向ける。PATH は scratch の制限 PATH(核のコマンドの symlink)とスタブだけ
#   - リモートは scratch の bare リポジトリ。ssh のスタブ(GIT_SSH_COMMAND)がローカルで git-upload-pack /
#     git-receive-pack を起動し、呼び出しを記録する(ネットワークの git が打たれたかをこれで見る)
#   - 全体を timeout で包む。scratch と XDG_STATE_HOME の下のものは終わったら消す。印つきのプロセスが
#     残っていないことも確かめる
#
# 終了コード: 0=全件 PASS / 1=FAIL あり / 3=中止(PATH の claude がスタブでない)
set -uo pipefail
if [ -z "${LOOP_SELFTEST_INNER:-}" ]; then
  LOOP_SELFTEST_INNER=1 exec timeout -k 10 "${LOOP_SELFTEST_TIMEOUT:-1500}" bash "$0" "$@"
fi
unset CDPATH

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET="${LOOP_SH:-$SCRIPT_DIR/loop.sh}"
PERM_SRC="${LOOP_PERMISSION:-$SCRIPT_DIR/loop-permission.py}"
PLUGIN_SRC="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
DOC_ER="$PLUGIN_SRC/skills/do-task/references/external-runners.md"
REVIEWS_DIR_SH="${REVIEWS_DIR_SH:-$PLUGIN_SRC/skills/do-task/scripts/reviews-dir.sh}"
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1
[ -f "$TARGET" ] || { echo "ERROR: $TARGET が無い" >&2; exit 1; }

W="$(mktemp -d)"
W="$(cd "$W" && pwd -P)"
TAG="dwloop-selftest-$$-$RANDOM"
PASS=0
FAIL=0
RESULTS=""
RUNS=0

ok() { PASS=$((PASS + 1)); RESULTS="$RESULTS
PASS  $1"; }
ng() {
  FAIL=$((FAIL + 1)); RESULTS="$RESULTS
FAIL  $1"
  if [ "$VERBOSE" -eq 1 ] && [ -n "${OUT:-}" ] && [ -f "$OUT" ]; then
    RESULTS="$RESULTS
$(sed 's/^/      | /' "$OUT" | tail -40)"
  fi
}
check() { if [ "$2" = "$3" ]; then ok "$1"; else ng "$1(期待 $2・実際 $3)"; fi; }
has() { if grep -qF -- "$3" "$2" 2>/dev/null; then ok "$1"; else ng "$1('$3' が無い)"; fi; }
hasnt() { if grep -qF -- "$3" "$2" 2>/dev/null; then ng "$1('$3' がある)"; else ok "$1"; fi; }
yes_() { if "$@" >/dev/null 2>&1; then return 0; fi; return 1; }
t() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else ng "$label"; fi; }
f() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ng "$label"; else ok "$label"; fi; }

proc_alive() { # $1=pid(ゾンビは死んでいるとみなす)
  local st
  [ -n "$1" ] && [ -r "/proc/$1/stat" ] || return 1
  st="$(sed -E 's/.*\) ([A-Za-z]).*/\1/' "/proc/$1/stat" 2>/dev/null)" || return 1
  case "$st" in Z|X|"") return 1 ;; esac
  return 0
}
wait_dead() { # $1=pid $2=秒
  local i=0
  while proc_alive "$1" && [ "$i" -lt $(( ${2:-5} * 10 )) ]; do sleep 0.1; i=$((i + 1)); done
  ! proc_alive "$1"
}
wait_file() { # $1=パス $2=秒
  local i=0
  while [ ! -e "$1" ] && [ "$i" -lt $(( ${2:-30} * 10 )) ]; do sleep 0.1; i=$((i + 1)); done
  [ -e "$1" ]
}
tagged_pids() { # 印(SELFTEST_TAG)を持つ生きたプロセス
  python3 - "$TAG" "$$" <<'PY'
import os, sys
want = b"\0SELFTEST_TAG=" + sys.argv[1].encode() + b"\0"
skip = {sys.argv[2], str(os.getpid()), str(os.getppid())}
for name in os.listdir("/proc"):
    if not name.isdigit() or name in skip:
        continue
    try:
        raw = open(f"/proc/{name}/stat", "rb").read()
        if raw[raw.rindex(b")") + 2:].split()[0] in (b"Z", b"X"):
            continue
        if (b"\0" + open(f"/proc/{name}/environ", "rb").read() + b"\0").find(want) >= 0:
            print(name)
    except (OSError, ValueError, IndexError):
        pass
PY
}

# 未信頼設定による再開拒否の後、人が実体を確認して片付ける操作を scratch で模擬する。
# production の meta/PID は使わず、この selftest 自身の TAG を持つプロセスだけを止める。
confirm_rejected_inflight() { # $1=この試験が作った state $2=ラベル
  local state="$1" label="$2" p held_pids
  held_pids="$(tagged_pids)"
  for p in $held_pids; do kill -KILL "$p" 2>/dev/null || true; done
  for p in $held_pids; do
    if wait_dead "$p" 5; then ok "$label: 人の確認後に試験用の子を片付けた"; else ng "$label: 試験用の子が残った"; fi
  done
  # 検査対象の証拠は別名で保存し、確認済みの中断印だけを外す。
  mv "$state/inflight" "$REC/confirmed-inflight"
  rm -f "$state/stop-mark.md"
}

cleanup_all() {
  local p
  for p in $(tagged_pids 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
  if [ -n "${LOOP_SELFTEST_KEEP:-}" ]; then echo "scratch を残した: $W" >&2; else rm -rf "$W"; fi
}
trap cleanup_all EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# ── scratch の環境(このスクリプト自身の git もここへ向ける)──
for v in $(git rev-parse --local-env-vars); do unset "$v"; done
unset GIT_CONFIG_NOSYSTEM
mkdir -p "$W/home" "$W/state" "$W/out" "$W/rec" "$W/repos" "$W/tmp" "$W/cwd"
export HOME="$W/home"
export GIT_CONFIG_GLOBAL="$W/gitconfig"
export GIT_CONFIG_SYSTEM="$W/gitconfig-system"
export GIT_TERMINAL_PROMPT=0
export SELFTEST_REC="$W/rec/setup"
mkdir -p "$SELFTEST_REC"
: >"$GIT_CONFIG_SYSTEM"
cat >"$GIT_CONFIG_GLOBAL" <<'EOF'
[user]
	name = loop-selftest
	email = loop-selftest@example.invalid
[init]
	defaultBranch = main
[advice]
	detachedHead = false
[gc]
	auto = 0
EOF

# ── 制限 PATH(実物の CLI が見えないように、核のコマンドの symlink だけを置く)──
SAFEBIN="$W/safebin"
mkdir -p "$SAFEBIN"
for c in bash sh env cat grep sed awk sort uniq head tail wc tr cut mkdir rm rmdir cp mv ln chmod touch date \
         sleep dirname basename readlink realpath mktemp stat find du sha256sum python3 git git-upload-pack \
         git-receive-pack setsid flock timeout uname id kill ps printf test tee xargs comm diff cmp od true \
         false seq ls pwd nohup; do
  src="$(command -v "$c" 2>/dev/null)" || continue
  case "$src" in /*) ln -sf "$src" "$SAFEBIN/$c" ;; esac
done
REAL_SLEEP="$(command -v sleep)"

STUBBIN="$W/stubbin"
ALTBIN="$W/altbin"
mkdir -p "$STUBBIN" "$ALTBIN" "$W/unamebin" "$W/failsleepbin" "$W/noyaml"

# publish-guard の公開後照合だけを通す gh。本文は stdin で受け、作成と REST 照合の argv を記録する。
DP_GH="$W/docpush-gh"
mkdir -p "$DP_GH"
cat >"$DP_GH/gh" <<'STUB'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"${SELFTEST_REC:?}/docpush-gh.log"
case "${1:-} ${2:-}" in
  'pr create')
    body="$(cat)"
    [ "$body" = 'review fixture' ] || exit 64
    printf '%s\n' 'https://github.com/o/r/pull/42'
    ;;
  'api --hostname')
    [ "${3:-}" = github.com ] || exit 64
    [ "${4:-}" = repos/o/r/pulls/42 ] || exit 64
    printf '%s\n' '{"html_url":"https://github.com/o/r/pull/42","number":42,"head":{"ref":"task/docpush-a","sha":"'"${SELFTEST_HELD_SHA:?}"'","repo":{"full_name":"o/r","owner":{"login":"o"}}},"base":{"ref":"main","repo":{"full_name":"o/r"}}}'
    ;;
  *) exit 64 ;;
esac
STUB
chmod +x "$DP_GH/gh"

# origin-repo.py には canonical HTTPS のまま読ませる。公開 helper と loop.sh が実際に打つ
# push / ls-remote だけを scratch bare remote へ写し、network 実行を伴う通常経路を保つ。
DP_GIT="$W/docpush-git"
mkdir -p "$DP_GIT"
cat >"$DP_GIT/git" <<'STUB'
#!/usr/bin/env bash
set -eu
network=0
for arg in "$@"; do
  case "$arg" in push|ls-remote) network=1 ;; esac
done
if [ "$network" = 1 ]; then
  exec "${SELFTEST_REAL_GIT:?}" -c "url.sshstub:${SELFTEST_PUBLISH_BARE:?}.insteadOf=https://github.com/o/r.git" "$@"
fi
exec "${SELFTEST_REAL_GIT:?}" "$@"
STUB
chmod +x "$DP_GIT/git"

# スタブの --help(雛形のフラグと別名をすべて載せる。形は実物の --help を模す)
cat >"$W/help.txt" <<'EOF'
Usage: claude [options] [command] [prompt]

Options:
  -p, --print                           Print response and exit
  --output-format <format>              Output format (choices: "text", "json", "stream-json")
  --setting-sources <sources>           Comma-separated list of setting sources (user, project, local)
  --strict-mcp-config                   Only use MCP servers from --mcp-config
  --plugin-dir <paths...>               Load plugins from directories
  --permission-mode <mode>              Permission mode (choices: "acceptEdits", "auto", "bypassPermissions", "manual", "plan")
  --permission-prompts <mode>           How to handle permission prompts (none)
  --mcp-config <configs...>             Load MCP servers from JSON files
  --allowedTools, --allowed-tools <tools...>
  --disallowedTools, --disallowed-tools <tools...>
  --settings <file-or-json>             Additional settings
  --model <model>                       Model for the current session
  -n, --name <name>                     Set a display name
  -z, --label <label>                   Set a label
  -d, --debug [filter]                  Enable debug mode
  --verbose                             Override verbose mode
  --dangerously-skip-permissions        Bypass all permission checks
  --allow-dangerously-skip-permissions  Enable bypassing as an option
  -h, --help                            Display help

Commands:
  auth status
  plugin list --json
EOF
grep -v -- '--permission-prompts' "$W/help.txt" >"$W/help-missing.txt"

# スタブのホスト CLI。補助の CLI(--help・auth status・plugin list --json)に答え、-p では stdin の
# プロンプトからタスク名を読み、名前の接頭辞(最初の `-` の前)で振る舞いを変える
cat >"$STUBBIN/claude" <<'STUB'
#!/usr/bin/env bash
REC="${SELFTEST_REC:?}"
mkdir -p "$REC"
me="$(basename "$0")"
case "${1:-}" in --help|auth|plugin) env >"$REC/env-aux-${1}" ;; esac
case "${1:-}" in
  --version) printf '%s --version\n' "$me" >>"$REC/aux.log"; echo "${SELFTEST_HOST_VERSION:-2.1.289 (stub)}"; exit 0 ;;
  --help) printf '%s --help\n' "$me" >>"$REC/aux.log"; cat "${SELFTEST_HELP_FILE:?}"; exit 0 ;;
  auth)
    printf '%s %s\n' "$me" "$*" >>"$REC/aux.log"
    # H31: 起動時の検査の後に実体を書き換える(周の途中の差し替えの模擬)
    #   内容は「パス<TAB>形」。append: 末尾に足す / ctime: 同じ大きさで 2 行目を書き換え、mtime を戻す / inode: 同じ大きさ・
    #   同じ mtime の別の inode に差し替える。書き換え後の 2 行目は、起動されると印を残す行
    if [ -f "$REC/mutate-on-auth" ]; then
      IFS=$'\t' read -r mpath mmode <"$REC/mutate-on-auth"
      rm -f "$REC/mutate-on-auth"
      case "${mmode:-append}" in
        append) printf '# mutated\n' >>"$mpath" ;;
        ctime|inode)
          python3 - "$mpath" "$mmode" <<'PY'
import os, sys
path, mode = sys.argv[1:]
mark = 'echo replaced >>"${SELFTEST_REC:?}/replaced-ran"'
st = os.stat(path)
lines = open(path, 'rb').read().split(b'\n')
assert len(lines[1]) == len(mark), 'pad'
lines[1] = mark.encode()
data = b'\n'.join(lines)
target = path if mode == 'ctime' else path + '.new'
with open(target, 'r+b' if mode == 'ctime' else 'wb') as f:
    f.write(data)
if mode == 'inode':
    os.chmod(target, st.st_mode & 0o7777)
os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
if mode == 'inode':
    os.replace(target, path)
PY
          ;;
      esac
    fi
    echo "logged in (stub)"; exit "${SELFTEST_AUTH_RC:-0}" ;;
  plugin)
    printf '%s %s\n' "$me" "$*" >>"$REC/aux.log"
    # 出力は SELFTEST_PLUGINS_FILE の中身。試験データは実物の形(#68 の実走で測った — design §7-3。トップは配列で、
    # 要素は id〈<名>@<marketplace>〉・version・scope・enabled・installPath・installedAt・lastUpdated。name は無い)で書く。
    # name の形と {"plugins": …} の形は、loop.sh の読み方の寛容さの試験として 1 つずつ渡す
    [ -z "${SELFTEST_PLUGINS_STDERR:-}" ] || echo "warning: plugin cache is stale (stub)" >&2
    if [ -n "${SELFTEST_PLUGINS_FILE:-}" ]; then cat "$SELFTEST_PLUGINS_FILE"; else echo '[]'; fi
    exit 0 ;;
esac
# 子起動は解析の前に記録する。固定フラグを値として消費し、解析に失敗した呼び出しも残す。
printf '%s\n' "$me" >>"$REC/child-starts.log"
is_p=0
if [ "${SELFTEST_PARSE_ARGV:-}" = 1 ]; then
  # H28 専用の解析。短い必須値は残りの文字か次のトークンを取り、フラグも値として消費する。
  python3 - "$REC/parsed.json" "$@" <<'PY'
import json, sys
args = sys.argv[2:]
values = {"--name": "name", "--label": "label", "--model": "model",
          "--output-format": "output-format", "--setting-sources": "setting-sources",
          "--plugin-dir": "plugin-dir", "--permission-mode": "permission-mode",
          "--permission-prompts": "permission-prompts", "--settings": "settings"}
short_values = {"n": "name", "z": "label"}
parsed = {}
i = 0
while i < len(args):
    arg = args[i]
    i += 1
    if arg.startswith("--"):
        name, sep, value = arg.partition("=")
        if name in values:
            if not sep:
                value = args[i] if i < len(args) else None
                i += 1
            parsed[values[name]] = value
        elif name in ("--allowedTools", "--mcp-config"):
            items = [value] if sep else []
            while i < len(args) and not args[i].startswith("-"):
                items.append(args[i])
                i += 1
            parsed[name[2:]] = items
        else:
            parsed[name[2:]] = True
    elif arg.startswith("-"):
        chars = arg[1:]
        for j, char in enumerate(chars):
            if char in short_values:
                value = chars[j + 1:].lstrip("=")
                if not value:
                    value = args[i] if i < len(args) else None
                    i += 1
                parsed[short_values[char]] = value
                break
            parsed["print" if char == "p" else char] = True
with open(sys.argv[1], "w", encoding="utf-8") as out:
    json.dump(parsed, out)
sys.exit(0 if parsed.get("print") else 64)
PY
  parse_rc=$?
  [ "$parse_rc" -eq 0 ] || exit "$parse_rc"
  is_p=1
else
  for a in "$@"; do [ "$a" = -p ] && is_p=1; done
fi
[ "$is_p" -eq 1 ] || { echo "stub: unexpected invocation: $*" >&2; exit 64; }
prompt="$(cat)"
# H20: 人の設定だけを変える。対象は治具が明示した scratch のファイルに限る。
human_config_change() {
  case "${SELFTEST_HUMAN_ACTION:?}" in
    add|value) git config --file "${SELFTEST_HUMAN_CONFIG:?}" selftest.human after ;;
    delete) rm -- "${SELFTEST_HUMAN_CONFIG:?}" ;;
    reorder) printf '[selftest]\n\tb = 2\n\ta = 1\n' >"${SELFTEST_HUMAN_CONFIG:?}" ;;
  esac
}
case "$prompt" in
  *"automated permission check run by the dev-workflow loop"*)
    # H31: 実 hook の確認(--prove-host)の模擬。SELFTEST_PROBE_MODE で振る舞いを選ぶ
    #   hook(既定): --settings の PermissionRequest の hook を実際に呼び、その判定に従う(loop-permission.py を通す)
    #   skip: hook を呼ばずに拒否する(設定の hook を読まない起動の模擬)/ allow: hook を呼ばずに書く(許可リストで通る模擬)
    #   autherr: 認証の失敗を返す / noattempt: Write を試みない
    printf '%s\n' "$0" "$@" >"$REC/argv-probe"
    env >"$REC/env-probe"
    target="$(printf '%s' "$prompt" | sed -n 's/.*create the file \(.*\) with the content.*/\1/p')"
    settings=""; prev=""
    for a in "$@"; do [ "$prev" != --settings ] || settings="$a"; prev="$a"; done
    decision=deny
    case "${SELFTEST_PROBE_MODE:-hook}" in
      hook)
        cmd="$(printf '%s' "$settings" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hooks"]["PermissionRequest"][0]["hooks"][0]["command"])')"
        decision="$(python3 -c 'import json,os,sys; print(json.dumps({"session_id":"stub","hook_event_name":"PermissionRequest","tool_name":"Write","tool_input":{"file_path":sys.argv[1],"content":"dev-workflow host probe"},"cwd":os.getcwd(),"permission_mode":"acceptEdits"}))' "$target" \
          | sh -c "$cmd" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["decision"]["behavior"])')"
        ;;
      allow) decision=allow ;;
      autherr) echo '{"type":"result","subtype":"error","is_error":true,"result":"Not logged in (stub)","permission_denials":[]}'; exit 1 ;;
      noattempt) echo '{"type":"result","subtype":"success","is_error":false,"result":"I will not write files.","permission_denials":[]}'; exit 0 ;;
    esac
    if [ "$decision" = allow ]; then
      printf 'dev-workflow host probe' >"$target"
      echo '{"type":"result","subtype":"success","is_error":false,"result":"written","permission_denials":[]}'
    else
      python3 -c 'import json,sys; print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"DENIED","permission_denials":[{"tool_name":"Write","tool_use_id":"stub","tool_input":{"file_path":sys.argv[1],"content":"dev-workflow host probe"}}]}))' "$target"
    fi
    exit 0 ;;
  *--discover=*)
    # 発見モードの周(loop.md §11)。発見元ごとの振る舞いは環境変数 SELFTEST_DISC_DA(data-audit)・SELFTEST_DISC_RF
    # (refactor)で選ぶ(既定は none = 候補なし)。記録のファイルの名は disc-<発見元>
    src="${prompt#*--discover=}"; src="${src%%[[:space:]]*}"
    case "$src" in data-audit) beh="${SELFTEST_DISC_DA:-none}" ;; refactor) beh="${SELFTEST_DISC_RF:-none}" ;; *) beh=none ;; esac
    rn="disc-$src"
    printf '%s\n' "$0" "$@" >"$REC/argv-$rn"
    printf '%s' "$prompt" >"$REC/prompt-$rn"
    printf '%s' "${DEV_WORKFLOW_LOOP_ITER:-}" >"$REC/iter-$rn"
    env >"$REC/env-$rn"
    pwd -P >"$REC/cwd-$rn"
    echo "$$" >"$REC/pid-$rn"
    printf '%s\n' "$rn" >>"$REC/calls.log"
    if [ -f "$REC/watch.pid" ]; then
      wp="$(cat "$REC/watch.pid")"; st="$(sed -E 's/.*\) ([A-Za-z]).*/\1/' "/proc/$wp/stat" 2>/dev/null)"
      case "$st" in ""|Z|X) echo dead ;; *) echo alive ;; esac >"$REC/watch-at-$rn"
    fi
    exec 3>>"$REC/stub-git.err"
    g() { git "$@" 2>&3; }
    tdir="${SELFTEST_DISC_TDIR:-docs/tasks}"
    base="$(git rev-parse HEAD)"
    br="task/候補-$src-${base:0:12}"
    dres() { # $1=結末の行(改行で複数行も)
      python3 -c 'import json,sys; print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "発見の周の完了報告(スタブ)\n" + sys.argv[1] + "\n", "permission_denials": []}, ensure_ascii=False))' "$1"
    }
    cand() { # $1=task_dir 相対のファイル名 → 候補の書式のファイルを書いて stage する
      mkdir -p "$(dirname -- "$tdir/$1")"
      printf '# %s\n\n> **ステータス**: 候補\n> **発見元**: %s\n> **指摘キー**: %s:file:%s\n\n## 指摘\n\nstub\n' "$1" "$src" "$src" "$1" >"$tdir/$1"
      g add -- "$tdir/$1"
    }
    dbr() { g checkout -q -b "$br"; }
    dcommit() { g commit -q -m "候補 $src"; }
    dpush() { g push -q origin "$br"; }
    dpushu() { g push -q -u origin "$br"; }   # 共有の config に branch.<作業ブランチ>.* を書く push(照合で止まる)
    dlinger() { # TERM を無視して居座る(同じプロセスグループに孫、別セッションに印つきの子孫も置く)
      bash -c 'trap "" TERM; while :; do sleep 1; done' </dev/null >/dev/null 2>&1 &
      echo $! >>"$REC/spawned-$rn"
      setsid bash -c 'trap "" TERM; while :; do sleep 1; done' </dev/null >/dev/null 2>&1 &
      echo $! >>"$REC/spawned-$rn"
      trap '' TERM
      touch "$REC/started-$rn"
      while :; do sleep 1; done
    }
    PRL="無人の周の結果: PR — https://example.invalid/pr/$src"
    case "$beh" in
      pr) dbr; cand "候補_$src-a.md"; cand "候補_$src-b.md"; dcommit; dpush; dres "$PRL" ;;
      degrade) dbr; cand "候補_$src-a.md"; dcommit; dres "無人の周の結果: 縮退 — --no-pr" ;;
      degradepush) dbr; cand "候補_$src-a.md"; dcommit; dpush; dres "無人の周の結果: 縮退 — repo が null" ;;
      none) dres "無人の周の結果: 候補なし — $src: 新しい候補 0 件(既知 0 件・回帰の疑い 0 件)" ;;
      inprog) dbr; cand "候補_$src-a.md"; cand "進行中_$src-a.md"; dcommit; dpush; dres "$PRL" ;;
      modify) dbr; cand "候補_$src-a.md"; echo x >>README.md; g add README.md; dcommit; dpush; dres "$PRL" ;;
      modcand) dbr; cand "候補_$src-a.md"; echo x >>"$tdir/候補_old.md"; g add -- "$tdir/候補_old.md"; dcommit; dpush; dres "$PRL" ;;
      nested) dbr; cand "sub/候補_$src-a.md"; dcommit; dpush; dres "$PRL" ;;
      outside) dbr; cand "memo-$src.md"; dcommit; dpush; dres "$PRL" ;;
      d21) dbr; cand "候補_bad name.md"; dcommit; dpush; dres "$PRL" ;;
      emptyname) dbr; cand "候補_.md"; dcommit; dpush; dres "$PRL" ;;
      mode) dbr; cand "候補_$src-a.md"; chmod +x -- "$tdir/候補_$src-a.md"; g update-index --chmod=+x -- "$tdir/候補_$src-a.md"; dcommit; dpush; dres "$PRL" ;;
      twocommits) dbr; cand "候補_$src-a.md"; dcommit; cand "候補_$src-b.md"; dcommit; dpush; dres "$PRL" ;;
      untracked) dbr; cand "候補_$src-a.md"; dcommit; dpush; echo x >"leftover-$src.txt"; dres "$PRL" ;;
      otherbranch) g checkout -q -b "task/候補-$src-000000000000"; cand "候補_$src-a.md"; dcommit; dres "無人の周の結果: 縮退 — x" ;;
      fail) dres "無人の周の結果: 失敗扱い — G3 前提を欠く" ;;
      pushfail) dbr; cand "候補_$src-a.md"; dcommit; dpush; dres "無人の周の結果: 失敗扱い — PR 作成の失敗" ;;
      hold) dres "無人の周の結果: 保留 — S4" ;;
      holdlast) dres $'無人の周の結果: 候補なし — 途中\n無人の周の結果: 保留 — 最後' ;;
      prunpushed) dbr; cand "候補_$src-a.md"; dcommit; dres "$PRL" ;;
      pushdel) dbr; cand "候補_$src-a.md"; dcommit; dpush; g checkout -q --detach "$base"; g branch -q -D "$br"
               dres "無人の周の結果: 候補なし — 今夜の名を push してローカルを消した" ;;
      hang) dlinger ;;
      pushsleep) dbr; cand "候補_$src-a.md"; dcommit; dpush; dlinger ;;
      pushu) dbr; cand "候補_$src-a.md"; dcommit; dpushu; dres "$PRL" ;;
      pushusleep) dbr; cand "候補_$src-a.md"; dcommit; dpushu; dlinger ;;
      humancfg) human_config_change; dres "無人の周の結果: 候補なし — H20" ;;
      cfgchange) dbr; cand "候補_$src-a.md"; dcommit; g config selftest.tampered yes; dres "無人の周の結果: 縮退 — tamper" ;;
      late) # 判定後の remove の直前に Git スタブが未追跡を置く。
            dbr; cand "候補_$src-a.md"; dcommit
            printf '%s\n' "$PWD" >"$REC/late-target"
            dres "無人の周の結果: 縮退 — late" ;;
      pushdiff) # push した中身(進行中_ を含む)と違う HEAD(候補_ だけ)で 縮退
                dbr; cand "候補_$src-a.md"; cand "進行中_$src-a.md"; dcommit; dpush
                g reset -q --hard "$base"; cand "候補_$src-a.md"; dcommit; dres "無人の周の結果: 縮退 — push した中身と違う" ;;
      dashname) dbr; cand "候補_-x.md"; dcommit; dpush; dres "$PRL" ;;
      noneuntracked) echo x >"leftover-$src.txt"; dres "無人の周の結果: 候補なし — 未追跡を残した" ;;
      noneotherbranch) g checkout -q -b "task/other-$src"; dres "無人の周の結果: 候補なし — 別のブランチに居る" ;;
      nonelocal) g branch -q "$br"; dres "無人の周の結果: 候補なし — 今夜の名をローカルにだけ作った" ;;
      *) dres "無人の周の結果: 失敗扱い — スタブの未知の振る舞い $beh" ;;
    esac
    printf -- '--- stub-end %s\n' "$rn" >>"$REC/ssh.log"
    exit 0 ;;
esac
task="${prompt#*--task=}"; task="${task%% *}"
tdir="$(dirname -- "$task")"
name="$(basename -- "$task")"; name="${name#進行中_}"; name="${name%.md}"
beh="${name%%-*}"
ARGV_ALL=("$@")
printf '%s\n' "$0" "$@" >"$REC/argv-$name"
printf '%s' "$prompt" >"$REC/prompt-$name"
printf '%s' "${DEV_WORKFLOW_LOOP_ITER:-}" >"$REC/iter-$name"
env >"$REC/env-$name"
pwd -P >"$REC/cwd-$name"
if [ -d .claude/reviews ] && [ ! -L .claude ] && [ ! -L .claude/reviews ]; then echo yes; else echo no; fi >"$REC/reviews-$name"
for fd in /proc/$$/fd/*; do readlink "$fd"; done >"$REC/fd-$name" 2>/dev/null
echo "$$" >"$REC/pid-$name"
printf '%s\n' "$name" >>"$REC/calls.log"
if [ -f "$REC/watch.pid" ]; then
  wp="$(cat "$REC/watch.pid")"; st="$(sed -E 's/.*\) ([A-Za-z]).*/\1/' "/proc/$wp/stat" 2>/dev/null)"
  case "$st" in ""|Z|X) echo dead ;; *) echo alive ;; esac >"$REC/watch-at-$name"
fi
exec 3>>"$REC/stub-git.err"
g() { git "$@" 2>&3; }
common="$(git rev-parse --path-format=absolute --git-common-dir)"
md="$tdir/進行中_$name.md"
result() { # $1=結末の行(空なら書かない)。SELFTEST_OUTPUT_ARRAY が立てば、全メッセージの配列の形で出す
  # (利用者の設定の verbose が効いたときの実物の形: 最後の要素が type: "result"。拒否の欄もその要素に載る)。
  # 値: 1・arr = 途中に古い result(別の結末・拒否の欄)を含み、最後が result / tail = 最後の result の後に
  # result でない要素 / empty = 空の配列 / noresult = result の無い配列 / bg = 結果の要素がターンごとに出る形
  # (周の中でバックグラウンドに委託したときの実物の形: 前の result〈origin 無し〉にだけ拒否の欄が載り、最後の
  # result〈origin が task-notification〉の欄は空)/ byname = タスク名の最後の「-」の後で選ぶ
  local mode="${SELFTEST_OUTPUT_ARRAY:-}"
  [ "$mode" != byname ] || mode="${name##*-}"
  OUTMODE="$mode" python3 -c '
import json, os, sys
t = "無人の周の完了報告(スタブ)\n" + (sys.argv[1] and sys.argv[1] + "\n")
res = {"type": "result", "subtype": "success", "is_error": False, "result": t, "permission_denials": []}
mode = os.environ.get("OUTMODE", "")
head = [{"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "無人の周の結果: PR — 途中の発言(結果ではない)"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "古い結果(最後の result ではない)\n無人の周の結果: PR — 古い",
         "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "stub-denied-old"}}]}]
if mode in ("1", "arr", "tail"):
    res["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": "stub-denied-array"}}]
    out = head + [res]
    if mode == "tail":
        out.append({"type": "system", "subtype": "after-result"})
elif mode == "bg":
    first = {"type": "result", "subtype": "success", "is_error": False,
             "result": "最初のターンの結果(最後の result ではない)\n無人の周の結果: 失敗扱い — 最初のターン",
             "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "stub-denied-bg"}}]}
    res["origin"] = {"kind": "task-notification"}
    out = [head[0], first, head[1], res]
elif mode == "empty":
    out = []
elif mode == "noresult":
    out = head[:2]
else:
    out = res
print(json.dumps(out, ensure_ascii=False))' "$1"
}
branch() { g checkout -q -b "task/$name"; }
# 受け取った --settings の JSON から許可の仲介の hook のコマンドを取り出し、ホスト CLI と同じく sh で打つ
hookcall() { # $1=tool_name $2=tool_input の JSON
  local settings="" prev="" a cmd input
  for a in "${ARGV_ALL[@]}"; do [ "$prev" = --settings ] && settings="$a"; prev="$a"; done
  cmd="$(printf '%s' "$settings" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hooks"]["PermissionRequest"][0]["hooks"][0]["command"])')"
  input="$(python3 -c 'import json,os,sys; print(json.dumps({"hook_event_name":"PermissionRequest","tool_name":sys.argv[1],"tool_input":json.loads(sys.argv[2]),"cwd":os.getcwd()}))' "$1" "$2")"
  printf '%s' "$input" | sh -c "$cmd" >>"$REC/hookout-$name" 2>&1
  printf '\n' >>"$REC/hookout-$name"
}
done_commit() { g mv "$md" "$tdir/完了_$name.md"; echo "impl $name" >"impl-$name.txt"; g add "impl-$name.txt"; g commit -q -m "impl $name"; }
push() { g push -q origin "task/$name"; g gc -q; }
pushu() { g push -q -u origin "task/$name"; g gc -q; }   # 共有の config に branch.task/{名}.* を書く push(照合で止まる)
hold_commit() { # $1=停止条件(空なら保留の行を足さない)
  if [ -n "$1" ]; then
    printf -- '- **保留**(ship-task・2026-09-24): %s — 本文を直し、/create-task で設計レビューをやり直す\n' "$1" >>"$md"
  fi
  g mv "$md" "$tdir/保留_$name.md"; g add "$tdir/保留_$name.md"; g commit -q -m "hold $name"
}
spawn_marked() { # 周の印を持ち TERM を無視する子孫を別セッションで起動する
  setsid bash -c 'trap "" TERM; while :; do sleep 1; done' </dev/null >/dev/null 2>&1 &
  echo $! >>"$REC/spawned-$name"
}
linger() { # TERM を無視して居座る(同じプロセスグループに孫も置く)
  bash -c 'trap "" TERM; while :; do sleep 1; done' </dev/null >/dev/null 2>&1 &
  echo $! >>"$REC/spawned-$name"
  spawn_marked
  trap '' TERM
  touch "$REC/started-$name"
  while :; do sleep 1; done
}
end() { printf -- '--- stub-end %s\n' "$name" >>"$REC/ssh.log"; }
# 環境と参考ログの攻撃 fixture。helper は起動済みの親が検査する。
case "${SELFTEST_ENV_ATTACK:-}" in
  source) printf 'raise SystemExit(0)\n' >"$SELFTEST_PLUGIN_SOURCE/skills/create-task/scripts/resolve-task-dir.py" ;;
  skill) printf 'changed\n' >"$SELFTEST_PLUGIN_SOURCE/skills/ship-task/SKILL.md" ;;
  copy) printf 'raise SystemExit(0)\n' >"$DEV_WORKFLOW_LOOP_PLUGIN_ROOT/skills/create-task/scripts/resolve-task-dir.py" ;;
  guard) printf 'raise SystemExit(0)\n' >"$DEV_WORKFLOW_ENV_GUARD" ;;
  user) mkdir -p "$HOME/.claude"; printf '{}\n' >"$HOME/.claude/settings.json" ;;
  global) printf '[user]\nname = changed\n' >>"$GIT_CONFIG_GLOBAL" ;;
  fake) printf '{"decision":"deny","kind":"protected"}\n' >"$DEV_WORKFLOW_LOOP_PERMLOG" ;;
  delete) hookcall Bash '{"command":"curl --version"}'; rm -f "$DEV_WORKFLOW_LOOP_PERMLOG" ;;
  duplicate) printf '{"decision":"deny","kind":"protected"}\n{"decision":"deny","kind":"protected"}\n' >"$DEV_WORKFLOW_LOOP_PERMLOG" ;;
  reorder) printf '{"decision":"allow"}\n{"decision":"deny","kind":"protected"}\n' >"$DEV_WORKFLOW_LOOP_PERMLOG" ;;
  malformed) printf '{broken\n' >"$DEV_WORKFLOW_LOOP_PERMLOG" ;;
  truncated) printf '{"decision":"deny"}' >"$DEV_WORKFLOW_LOOP_PERMLOG" ;;
  fifo) python3 -c 'import os,sys; os.mkfifo(sys.argv[1])' "$DEV_WORKFLOW_LOOP_PERMLOG" ;;
  symlink) ln -s "$SELFTEST_LOG_OUTSIDE" "$DEV_WORKFLOW_LOOP_PERMLOG" ;;
esac
case "$beh" in
  pr) branch; done_commit; push; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  degrade) branch; done_commit; result "無人の周の結果: 縮退 — gh が無い" ;;
  hold|holds4) branch; hold_commit "S4 needs-user が残っている"; result "無人の周の結果: 保留 — S4" ;;
  holdg1) branch; hold_commit "G1 許可の拒否"; result "無人の周の結果: 保留 — G1" ;;
  holdg1c) branch; hold_commit "G1: 許可の拒否"; result "無人の周の結果: 保留 — G1" ;;
  holdg1prot) hookcall Write "{\"file_path\": \"$PWD/.claude/settings.json\"}"
              branch; hold_commit "G1 保護パスへの書き込みの拒否"; result "無人の周の結果: 保留 — G1" ;;
  holdg1oth) hookcall Bash '{"command": "curl --version"}'
             branch; hold_commit "G1 許可の拒否"; result "無人の周の結果: 保留 — G1" ;;
  hookcall) hookcall Write "{\"file_path\": \"$PWD/.claude/reviews/probe.txt\"}"
            hookcall Write "{\"file_path\": \"$PWD/.claude/settings.json\"}"
            hookcall Bash '{"command": "make test > .claude/reviews/t.txt"}'
            branch; done_commit; push; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  holdold) branch; hold_commit "S4 本文の変更"; result "無人の周の結果: 保留 — S4" ;;
  holdnocode) branch; hold_commit "承認待ち"; result "無人の周の結果: 保留 — 承認待ち" ;;
  holdnoline) branch; hold_commit ""; result "無人の周の結果: 保留 — 行なし" ;;
  fail) branch; result "無人の周の結果: 失敗扱い — G3 前提を欠く" ;;
  none) branch; done_commit; push; result "" ;;
  rcnz) branch; done_commit; push; result "無人の周の結果: PR — https://example.invalid/pr/$name"; end; exit 3 ;;
  prunpushed) branch; done_commit; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  both) branch; cp "$md" "$tdir/完了_$name.md"; g add "$tdir/完了_$name.md"; g commit -q -m "both $name"; push
        result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  untracked) branch; done_commit; push; echo x >"leftover-$name.txt"; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  ignored) branch; done_commit; push; echo x >"left.ignored"; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  reviews) branch; done_commit; push; mkdir -p .claude/reviews; echo r >.claude/reviews/r.md
           result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  symreviews) branch; done_commit; push; mkdir -p .claude "$REC/outside-reviews"; echo s >"$REC/outside-reviews/s.md"
              rm -rf .claude/reviews
              ln -s "$REC/outside-reviews" .claude/reviews; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  badjson) branch; done_commit; push; echo "not json" ;;
  slow) sleep "${SELFTEST_SLOW:-5}"; branch; done_commit; push; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  stopfile) branch; done_commit; push; mkdir -p "$(dirname "${SELFTEST_STOP_FILE:?}")"; touch "$SELFTEST_STOP_FILE"; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  leak) branch; done_commit; push; spawn_marked; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  hang) linger ;;
  longsleep) linger ;;
  cfgsleep) g config selftest.tampered yes; linger ;;
  defsleep) branch; done_commit; g update-ref refs/heads/main HEAD; linger ;;
  pushsleep) branch; done_commit; push; linger ;;
  pushusleep) branch; done_commit; pushu; linger ;;
  crash) touch "${SELFTEST_SLEEP_TRIGGER:?}"; linger ;;
  crashcfg) g config selftest.tampered yes; touch "${SELFTEST_SLEEP_TRIGGER:?}"; linger ;;
  humancfg) branch; done_commit; human_config_change; result "無人の周の結果: 縮退 — H20" ;;
  cfgadd) branch; done_commit; g config selftest.added yes; result "無人の周の結果: 縮退 — tamper" ;;
  cfgpushremote) branch; done_commit; push; g config "branch.task/$name.pushRemote" evil; result "無人の周の結果: PR — x" ;;
  cfgremote) branch; done_commit; push; g config "branch.task/$name.remote" other; result "無人の周の結果: PR — x" ;;
  pushu) branch; done_commit; pushu; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  docpush) # 文書の無人公開 helper(SELFTEST_PUSH_CMD。プレースホルダは治具が置き換える)を gh stub まで打つ。
           branch; done_commit; git config --get branch.autoSetupRebase >"$REC/asr-$name" 2>&3
           mkdir -p .claude/reviews; printf 'review fixture\n' >.claude/reviews/docpush-body.md
           SELFTEST_BRANCH="task/$name" SELFTEST_HELD_SHA="$(git rev-parse HEAD)"
           SELFTEST_CONFIG_DIGEST="$(python3 "${DEV_WORKFLOW_LOOP_PLUGIN_ROOT:?}/skills/ship-task/scripts/git-config-digest.py" --dir="$PWD")"
           export SELFTEST_BRANCH SELFTEST_HELD_SHA SELFTEST_CONFIG_DIGEST
           if ! eval "${SELFTEST_PUSH_CMD:?}" >&3 2>&3; then exit 3; fi
           g gc -q
           result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  cfgreorder) branch; done_commit
              python3 - "$common/config" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
s = s.replace("\ta = 1\n\tb = 2\n", "\tb = 2\n\ta = 1\n")
open(p, "w").write(s)
PY
              result "無人の周の結果: 縮退 — tamper" ;;
  hookchmod) branch; done_commit; chmod +x "$common/hooks/selftest-hook"; result "無人の周の結果: 縮退 — tamper" ;;
  infoexclude) branch; done_commit; echo tampered >>"$common/info/exclude"; result "無人の周の結果: 縮退 — tamper" ;;
  wtconfig) branch; done_commit; g config --worktree selftest.wt yes; result "無人の周の結果: 縮退 — tamper" ;;
  defmove) branch; done_commit; g update-ref refs/heads/main HEAD; result "無人の周の結果: 縮退 — tamper" ;;
  basetamper) branch; done_commit; g config selftest.tampered yes
              # 周の起動の直前に取った比べる元を、書き換えた後の状態に合わせて書き直す(照合をすり抜けようとする)
              for b in "${XDG_STATE_HOME:?}"/dev-workflow/loop/*/*/iter-*.base.json; do
                before="$(sha256sum <"$b" | cut -d' ' -f1)"
                python3 - "$b" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["common_config"]["entries"].append(["selftest.tampered", "yes"])
json.dump(d, open(p, "w"))
PY
                after="$(sha256sum <"$b" | cut -d' ' -f1)"
                [ "$before" != "$after" ] || exit 91
              done
              result "無人の周の結果: 縮退 — tamper" ;;
  badreviews) branch; done_commit; push; mkdir -p .claude/reviews; echo r >.claude/reviews/unreadable.md
              chmod 000 .claude/reviews/unreadable.md; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
  candlast) branch; done_commit; push   # 実装の周の最後の結末の行が 候補なし(実装モードでは取りえない)
            result $'無人の周の結果: PR — https://example.invalid/pr/'"$name"$'\n無人の周の結果: 候補なし — 実装の周では取りえない' ;;
  nobranch) done_commit; result "無人の周の結果: 縮退 — detached" ;;
  nodone) branch; g rm -q "$md"; echo "impl" >"impl-$name.txt"; g add "impl-$name.txt"; g commit -q -m "nodone $name"; result "無人の周の結果: 縮退 — no done" ;;
  holdnofile) branch; done_commit; result "無人の周の結果: 保留 — S4" ;;
  *) branch; done_commit; push; result "無人の周の結果: PR — https://example.invalid/pr/$name" ;;
esac
end
exit 0
STUB
chmod +x "$STUBBIN/claude"
cp "$STUBBIN/claude" "$ALTBIN/claude-alt"
chmod +x "$ALTBIN/claude-alt"

# ssh のスタブ(GIT_SSH_COMMAND)。呼び出しを記録し、ローカルで git-upload-pack などを起動する。
# SELFTEST_SSH_HANG が立っていれば、TERM を無視して応答しない(固まる ssh)
cat >"$STUBBIN/ssh-stub" <<'EOF'
#!/usr/bin/env bash
# ssh の種類を調べる呼び出し(-G)には成功で答える(OpenSSH とみなされる)
[ "${1:-}" = -G ] && exit 0
printf '%s\n' "$*" >>"${SELFTEST_REC:?}/ssh.log"
if [ -n "${SELFTEST_SSH_HANG:-}" ]; then trap '' TERM; while :; do sleep 1; done; fi
exec sh -c "${@: -1}"
EOF
chmod +x "$STUBBIN/ssh-stub"
export GIT_SSH_COMMAND="$STUBBIN/ssh-stub"

# uname のスタブ(Linux でない)
printf '#!/bin/sh\necho Darwin\n' >"$W/unamebin/uname"
chmod +x "$W/unamebin/uname"
# sleep のスタブ(内部の失敗を起こす): 引き金のファイルがあり、周の印を持たない(= loop.sh 自身)なら失敗する
cat >"$W/failsleepbin/sleep" <<EOF
#!/bin/sh
if [ -e "\${SELFTEST_SLEEP_TRIGGER:-/nonexistent}" ] && [ -z "\${DEV_WORKFLOW_LOOP_ITER:-}" ]; then exit 1; fi
exec "$REAL_SLEEP" "\$@"
EOF
chmod +x "$W/failsleepbin/sleep"
# PyYAML を隠す(import yaml を失敗させる)
echo 'raise ImportError("blocked by loop-selftest")' >"$W/noyaml/yaml.py"

# remove の拒否の正常対照。FIFO や生き残りの子孫に同期を依存させない。
REAL_GIT="$(command -v git)"
cat >"$STUBBIN/git" <<EOF
#!/usr/bin/env bash
saw=0
for arg in "\$@"; do
  if [ "\$saw" = 1 ] && [ "\$arg" = remove ]; then saw=2; break; fi
  [ "\$arg" != worktree ] || saw=1
done
if [ "\$saw" = 2 ] && [ -f "\${SELFTEST_REC:-}/late-target" ]; then
  target="\$(cat "\$SELFTEST_REC/late-target")"
  if [ "\${@: -1}" = "\$target" ]; then
    echo late >"\$target/late-untracked.txt"
    rm -f "\$SELFTEST_REC/late-target"
  fi
fi
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$STUBBIN/git"

# ── scratch のプラグイン(対象の loop.sh を置く。変異版もここに写して打つ)──
PLUG="$W/plugin"
mkdir -p "$PLUG/.claude-plugin" "$PLUG/skills/ship-task/scripts" "$PLUG/skills/create-task/scripts"
cp "$PLUGIN_SRC/.claude-plugin/plugin.json" "$PLUG/.claude-plugin/plugin.json"
cp "$TARGET" "$PLUG/skills/ship-task/scripts/loop.sh"
cp "$SCRIPT_DIR/loop-state.py" "$PLUG/skills/ship-task/scripts/loop-state.py"
cp "$SCRIPT_DIR/loop-startup.py" "$PLUG/skills/ship-task/scripts/loop-startup.py"
cp "$PLUGIN_SRC/skills/create-task/scripts/resolve-task-dir.py" "$PLUG/skills/create-task/scripts/resolve-task-dir.py"
cp "$PERM_SRC" "$PLUG/skills/ship-task/scripts/loop-permission.py"
cp "$SCRIPT_DIR/environment-guard.py" "$PLUG/skills/ship-task/scripts/environment-guard.py"
cp "$SCRIPT_DIR/host-argv.py" "$SCRIPT_DIR/loop-supervisor.py" "$SCRIPT_DIR/host-check.py" "$PLUG/skills/ship-task/scripts/"
cp "$SCRIPT_DIR/origin-repo.py" "$PLUG/skills/ship-task/scripts/origin-repo.py"   # 起動時の origin の URL の検査で使う(両方のモード。loop.md §2)
cp "$SCRIPT_DIR/git-config-digest.py" "$PLUG/skills/ship-task/scripts/git-config-digest.py"   # 在ることを起動時に確かめる(両方のモード。loop.md §2 の 4)
cp "$SCRIPT_DIR/publish-guard.py" "$PLUG/skills/ship-task/scripts/publish-guard.py"
LOOP="$PLUG/skills/ship-task/scripts/loop.sh"
PLUGIN_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$PLUG/.claude-plugin/plugin.json")"

# ── loop.sh の起動(env -i。打つ前に PATH の claude がスタブであることを確かめる)──
guard_path() { # $1=PATH
  local found
  found="$(cd "$W/cwd" && env -i PATH="$1" bash -c 'command -v claude' 2>/dev/null || true)"
  case "$found" in ""|"$STUBBIN/claude") return 0 ;; esac
  echo "中止: PATH の claude がスタブでない($found)。実物のホスト CLI を起動しないため止める" >&2
  exit 3
}
REC="$W/rec/default"
LOOP_BIN="$LOOP"
build_env() { # 使い方: build_env <状態名> [VAR=値 ...] → ENV_ARGS
  local state="$1" path="$STUBBIN:$SAFEBIN" v
  shift
  ENV_ARGS=()
  for v in "$@"; do case "$v" in PATH=*) path="${v#PATH=}" ;; esac; done
  guard_path "$path"
  ENV_ARGS=(HOME="$W/home" PATH="$path" XDG_STATE_HOME="$W/state/$state" XDG_CONFIG_HOME="$W/home/.config" GIT_CONFIG_GLOBAL="$GIT_CONFIG_GLOBAL"
    GIT_CONFIG_SYSTEM="$GIT_CONFIG_SYSTEM" GIT_SSH_COMMAND="$STUBBIN/ssh-stub" LANG=C.UTF-8
    SELFTEST_REC="$REC" SELFTEST_TAG="$TAG" SELFTEST_HELP_FILE="$W/help.txt" TMPDIR="$W/tmp")
  for v in "$@"; do case "$v" in PATH=*) : ;; *) ENV_ARGS+=("$v") ;; esac; done
}
# run_loop <状態名> [VAR=値 ...] -- [loop.sh の引数 ...] → RC と OUT(stdout・stderr)
run_loop() {
  local state="$1" envs=()
  shift
  while [ $# -gt 0 ] && [ "$1" != -- ]; do envs+=("$1"); shift; done
  [ $# -gt 0 ] && shift
  build_env "$state" ${envs[@]+"${envs[@]}"}
  RUNS=$((RUNS + 1))
  OUT="$W/out/$RUNS.txt"
  ( cd "$W/cwd" && exec env -i "${ENV_ARGS[@]}" timeout -k 5 "${RUN_TIMEOUT:-150}" bash "$LOOP_BIN" "$@" ) >"$OUT" 2>&1
  RC=$?
}
# start_bg: run_loop と同じ引数で背景に起動する → BG_PID・BG_OUT
start_bg() {
  local state="$1" envs=()
  shift
  while [ $# -gt 0 ] && [ "$1" != -- ]; do envs+=("$1"); shift; done
  [ $# -gt 0 ] && shift
  build_env "$state" ${envs[@]+"${envs[@]}"}
  RUNS=$((RUNS + 1))
  BG_OUT="$W/out/$RUNS.txt"
  ( cd "$W/cwd" && exec env -i "${ENV_ARGS[@]}" bash "$LOOP_BIN" "$@" ) >"$BG_OUT" 2>&1 &
  BG_PID=$!
  ( "$REAL_SLEEP" 150; kill -KILL "$BG_PID" 2>/dev/null ) >/dev/null 2>&1 &
  BG_DOG=$!
}
wait_bg() {
  wait "$BG_PID"; BG_RC=$?
  pkill -P "$BG_DOG" 2>/dev/null; kill "$BG_DOG" 2>/dev/null; wait "$BG_DOG" 2>/dev/null
  OUT="$BG_OUT"
}
report_of() { sed -n 's/^報告: //p' "$1" | tail -1; }
latest_report() { # $1=状態名 → 最後に作られた報告(KILL した実行は「報告:」を出さないので、状態ディレクトリから探す)
  local sd
  sd="$(state_dir "$1")" || return 1
  ls -1t "$sd"/*/report.md 2>/dev/null | head -1
}
state_dir() { # $1=状態名 → 状態ディレクトリ(1 つだけのはず)
  local d
  for d in "$W/state/$1"/dev-workflow/loop/*/; do printf '%s' "${d%/}"; return 0; done
  return 1
}

# ── scratch のリポジトリ ──
newrec() { REC="$W/rec/$1"; rm -rf "$REC"; mkdir -p "$REC"; }
G() { git "$@" >/dev/null 2>&1; }
# newrepo <名> [noorigin] → R(作業ツリー)・B(bare のリモート)。状態ファイル 3 つ(loop.md §3 の 2a)を ignore する
newrepo() {
  R="$W/repos/$1"
  B="$W/repos/$1.remote.git"
  rm -rf "$R" "$B" "$R.loop"
  git init -q -b main "$R"
  printf '*.ignored\n.claude/reviews\n.claude/grasp.md\n.claude/.understand-project-done\n' >"$R/.gitignore"
  echo "# $1" >"$R/README.md"
  mkdir -p "$R/docs/tasks"
  G -C "$R" add -A
  G -C "$R" commit -q -m init
  if [ "${2:-}" != noorigin ]; then
    git init -q --bare -b main "$B"
    G -C "$R" remote add origin "sshstub:$B"
    SELFTEST_REC="$W/rec/setup" G -C "$R" push -q -u origin main
    G -C "$R" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main
  fi
}
# addtask <名> [作成日|-] [meta 1|0] [task_dir] [追加修正記録に足す行]
addtask() {
  local name="$1" date="${2:--}" meta="${3:-1}" tdir="${4:-docs/tasks}" extra="${5:-}"
  mkdir -p "$R/$tdir"
  {
    printf '# %s\n\n> **ステータス**: 🚧 進行中\n' "$name"
    [ "$date" = - ] || printf '> **作成日**: %s\n' "$date"
    [ "$meta" != 1 ] || printf '> **無人実行**: 可\n'
    printf '\n## 概要\n\nloop-selftest のタスク。\n\n## 追加修正記録\n\n'
    printf -- '- **設計レビュー**(create-task・2026-01-01): APPROVED — checker: 指摘 0 / 反復 1 回 / 編成: checker / 本文: sha256:0000000000000000 / needs-user: なし\n'
    [ -z "$extra" ] || printf '%s\n' "$extra"
  } >"$R/$tdir/進行中_$name.md"
}
commit() { # [push しないなら localonly]
  G -C "$R" add -A
  G -C "$R" commit -q -m "${2:-tasks}"
  if [ "${1:-}" != localonly ] && git -C "$R" remote get-url origin >/dev/null 2>&1; then
    SELFTEST_REC="$W/rec/setup" G -C "$R" push -q origin main
  fi
}
calls() { cat "$REC/calls.log" 2>/dev/null | tr '\n' ' ' | sed 's/ $//'; }
locked_reason_of() { # $1=相対パス → その理由で locked の worktree があれば 0(-z でないと理由が引用される)
  git -C "$R" worktree list --porcelain -z | tr '\0' '\n' | grep -qxF "locked dev-workflow-loop: $1"
}
wt_count() { git -C "$R" worktree list --porcelain -z | tr '\0' '\n' | grep -c '^worktree '; }
COMMON_ARGS=(--kill-grace 2 --net-timeout 10)
# doc_push_cmd → 「OK <コマンド>」か「NG <理由>」。ship-task/SKILL.md の無人公開 helper の字面を
# 取り出し、scratch の保持 SHA・設定 digest・ブランチへ置き換える。helper 自身を gh stub まで実行する。
# 取り出しは UW_CMDS と同じ(バッククォートの中・ちょうど 1 種類)。ハードコードしない(-B で __pycache__ を作らない)
doc_push_cmd() {
  python3 -B - "$PLUGIN_SRC/skills/ship-task/SKILL.md" "$PLUG/skills/ship-task/" <<'PY' 2>&1
import re, sys
path, skill_dir = sys.argv[1], sys.argv[2]
try:
    text = open(path, encoding="utf-8").read()
except OSError as exc:
    print("NG SKILL.md を読めない: %s" % exc)
    sys.exit(0)
hits = sorted(set(re.findall(r"`(python3 \{ship-task の\}scripts/publish-guard\.py [^`]+)`", text)))
if len(hits) != 1:
    print("NG 字面がちょうど 1 種類でない(%d 種類)" % len(hits))
    sys.exit(0)
cmd = hits[0].replace("{ship-task の}", skill_dir)
subs = {
    "<管理ルート>": '"$PWD"',
    "<保持したレビュー済み SHA>": '"$SELFTEST_HELD_SHA"',
    "<作業ブランチ>": '"$SELFTEST_BRANCH"',
    "<repo>": "github.com/o/r",
    "<デフォルトブランチ>": "main",
    "<守る値>": '"$SELFTEST_CONFIG_DIGEST"',
    "<名>": "docpush-body.md",
    "<タスク名>": "docpush",
}
for old, new in subs.items():
    cmd = cmd.replace(old, new)
left = re.findall(r"<[^<>\s]+>|\{[^{}]*\}", cmd)
if left:
    print("NG 置き換えられないプレースホルダ: %s" % " ".join(left))
elif "\t" in cmd or "\n" in cmd:
    print("NG 字面にタブか改行がある")
else:
    print("OK " + cmd)
PY
}

echo "loop-selftest: 対象 $TARGET(scratch $W)"

# ════════════════ 治具: スタブのホスト CLI の証明(H31。以後の起動の前提)════════════════
# loop.sh は証明(実 hook の拒否を確かめた記録)の無いホスト CLI を起動しない。スタブは --settings の hook を
# 実際に呼んで判定に従うので、--prove-host を通常どおり打って証明を作る(scripts/test_loop_startup.py も
# この前半を使う)。旧実装(--prove-host が無い)と比べるときも続けられるよう、失敗は記録だけにする
newrepo provefix
newrec provefix
PROOF_DIR="$W/home/.config/dev-workflow/loop/host-proofs"
run_loop prove-default -- --repo "$R" --prove-host "${COMMON_ARGS[@]}"
check "治具: スタブの証明を作れる(default)" 0 "$RC"
# LOOP_SELFTEST_ONLY=<節,節,...> で節を絞れる(host shell-prefix sync stops link argv h28 memory order skip locked judge breakers signals kill hooks perm d22 between discover)
want() { [ -z "${LOOP_SELFTEST_ONLY:-}" ] && return 0; case ",$LOOP_SELFTEST_ONLY," in *",$1,"*) return 0 ;; esac; return 1; }

# ════════════════ 治具: 上書きの実行ファイルの証明(H31)════════════════
run_loop prove-alt -- --repo "$R" --prove-host --host-argv "$ALTBIN/claude-alt" "${COMMON_ARGS[@]}"
check "治具: スタブの証明を作れる(alt)" 0 "$RC"

# ════════════════ 同期: 判定 2 の環境変数の列(D7)════════════════
# 文書の §3 判定 2 の項目(その行と下位の行)から、バッククォートで囲んだ大文字の環境変数名だけを取り出す
DOC_VARS="$(awk '/^2\. \*\*自ホストと別 CLI か\*\*/{f=1;print;next} f&&/^[[:space:]]/{print;next} {f=0}' "$DOC_ER" | grep -oE '`[A-Z][A-Z0-9_]*`' | tr -d '`' | sort -u | tr '\n' ' ')"
if want sync; then
LOOP_VARS="$(grep -E '^HOST_SESSION_VARS=\(' "$TARGET" | sed -E 's/^HOST_SESSION_VARS=\((.*)\).*/\1/' | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ' ')"
if [ -n "$DOC_VARS" ] && [ "$DOC_VARS" = "$LOOP_VARS" ]; then
  ok "同期: 判定 2 の環境変数の列が external-runners.md と一致する($DOC_VARS)"
else
  ng "同期: 判定 2 の環境変数の列が external-runners.md と一致する(文書: '$DOC_VARS' / loop.sh: '$LOOP_VARS')"
fi
case " $DOC_VARS " in *" DEV_WORKFLOW_HOST_CLI "*" CLAUDECODE "*|*" CLAUDECODE "*" DEV_WORKFLOW_HOST_CLI "*) ok "同期: 文書の列を取り出せた" ;;
  *) ng "同期: 文書の列を取り出せた('$DOC_VARS')" ;; esac

fi

# ════════════════ 起動時に止まる ════════════════
if want stops; then
newrepo stops
addtask pr-a 2026-01-01
commit
newrec stops
# ホストの中(external-runners.md の列の指標ごと・DEV_WORKFLOW_HOST_CLI)
for v in $DOC_VARS; do
  : >"$REC/aux.log"
  run_loop hs "$v=1" -- --repo "$R" --dry-run
  check "ホストの中($v)で止まる: 終了コード" 20 "$RC"
  has "ホストの中($v)で止まる: 理由" "$OUT" "[host-session]"
  if [ -s "$REC/aux.log" ]; then ng "ホストの中($v): 補助の CLI も起動しない"; else ok "ホストの中($v): 補助の CLI も起動しない"; fi
done
run_loop hs DEV_WORKFLOW_HOST_CLI=claude -- --repo "$R" --dry-run
check "DEV_WORKFLOW_HOST_CLI が非空なら止まる" 20 "$RC"
run_loop hs "PATH=$W/unamebin:$STUBBIN:$SAFEBIN" -- --repo "$R" --dry-run
check "Linux でない(uname のスタブ)で止まる" 20 "$RC"
has "Linux でない: 理由" "$OUT" "[os]"
# コピーの配置(setup.sh --copy)・プラグインの名前が違う
mkdir -p "$W/copyproj/.claude/skills"
cp -r "$PLUG/skills/ship-task" "$W/copyproj/.claude/skills/ship-task"
LOOP_BIN="$W/copyproj/.claude/skills/ship-task/scripts/loop.sh"
run_loop hs -- --repo "$R" --dry-run
check "コピーの配置(plugin.json が無い)で止まる" 20 "$RC"
has "コピーの配置: 理由" "$OUT" "[plugin-root]"
mkdir -p "$W/plugin-other"
cp -r "$PLUG/." "$W/plugin-other/"
python3 - "$W/plugin-other/.claude-plugin/plugin.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["name"] = "other-plugin"; json.dump(d, open(p, "w"))
PY
LOOP_BIN="$W/plugin-other/skills/ship-task/scripts/loop.sh"
run_loop hs -- --repo "$R" --dry-run
check "プラグインのルートの名前が違うと止まる" 20 "$RC"
has "名前が違う: 理由" "$OUT" "[plugin-root]"
LOOP_BIN="$LOOP"
# 既定表外のホスト
for h in codex cursor-agent; do
  run_loop hs -- --repo "$R" --dry-run --host "$h"
  check "既定表外のホスト($h)で止まる" 20 "$RC"
  has "既定表外のホスト($h): 理由" "$OUT" "[host-unsupported]"
done
run_loop hs -- --repo "$R" --dry-run --host foo
check "未知のホストで止まる" 20 "$RC"
has "未知のホスト: 理由" "$OUT" "[host-unknown]"
# --host-argv に隔離・判定のフラグ(= つきの形・別名を含む)・--settings
for tok in -p --print --output-format=text --setting-sources=project --strict-mcp-config --plugin-dir=/x \
           --permission-mode=plan --permission-prompts --mcp-config --allowedTools=Read --allowed-tools \
           --disallowedTools --disallowed-tools=Bash --settings --settings=x.json -cp \
           -- --bg --background -w --worktree=x -dw -c=p \
           --continue -c --resume=old -rOld -cv --remote --cloud=id --plugin-url=url --bare --safe-mode --unknown; do
  run_loop hs -- --repo "$R" --dry-run --host-argv "$ALTBIN/claude-alt" --host-argv "$tok" --host-argv --model=m1
  check "--host-argv の '$tok' で止まる" 20 "$RC"
  has "--host-argv の '$tok': 理由" "$OUT" "[host-argv]"
done
# 値は --名前=値 の形だけ(フラグでないトークン・最後のトークンが = の無いフラグ)
run_loop hs -- --repo "$R" --dry-run --host-argv "$ALTBIN/claude-alt" --host-argv --model --host-argv m1
check "--host-argv にフラグでないトークン(値を別に書く)があると止まる" 20 "$RC"
has "--host-argv のフラグでないトークン: 理由" "$OUT" "[host-argv]"
run_loop hs -- --repo "$R" --dry-run --host-argv "$ALTBIN/claude-alt" --host-argv --verbose --host-argv --model
check "--host-argv の値を取るフラグ(--help の <…> の表)を = 無しで書くと止まる" 20 "$RC"
has "--host-argv の値を取るフラグ: 理由" "$OUT" "[host-argv]"
run_loop hs -- --repo "$R" --dry-run --host-argv "$ALTBIN/claude-alt" --host-argv -n=x
check "--host-argv の値を取る短いフラグは止まる" 20 "$RC"
run_loop hs -- --repo "$R" --dry-run --host-argv "$ALTBIN/claude-alt" --host-argv --model=m1 --host-argv --verbose --host-argv --debug
check "--host-argv の表に無いフラグは値を取らなくても拒否する" 20 "$RC"
# 全許可のフラグ(--host-argv 経由・--allowed-tools 経由)
for tok in --dangerously-skip-permissions --allow-dangerously-skip-permissions=true; do
  run_loop hs -- --repo "$R" --dry-run --host-argv "$ALTBIN/claude-alt" --host-argv "$tok" --host-argv --model=m1
  check "全許可のフラグ('$tok'・--host-argv 経由)で止まる" 20 "$RC"
  has "全許可のフラグ('$tok'): 理由" "$OUT" "[host-argv]"
done
run_loop hs -- --repo "$R" --dry-run --allowed-tools Read --allowed-tools bypassPermissions
check "全許可のモードの値(--allowed-tools 経由)で止まる" 20 "$RC"
has "全許可のモードの値(--allowed-tools): 理由" "$OUT" "[full-permission]"
# --allowed-tools の値が - で始まれば使い方の誤り(フラグとして読まれるため)
for tok in --dangerously-skip-permissions --permission-mode=bypassPermissions --permission-mode=auto -p; do
  run_loop hs -- --repo "$R" --dry-run --allowed-tools Read --allowed-tools "$tok"
  check "--allowed-tools の値 '$tok'('-' で始まる)は使い方の誤り" 2 "$RC"
  has "--allowed-tools の値 '$tok': 理由" "$OUT" "[usage]"
done
# git でない・bare・トップでない
mkdir -p "$W/notgit"
run_loop hs -- --repo "$W/notgit" --dry-run
check "git でないと止まる" 20 "$RC"
run_loop hs -- --repo "$B" --dry-run
check "bare で止まる" 20 "$RC"
has "bare: 理由" "$OUT" "[bare]"
run_loop hs -- --repo "$R/docs" --dry-run
check "作業ツリーのトップでないと止まる" 20 "$RC"
# ホスト CLI が無い(スタブも実物も PATH に無い)
run_loop hs "PATH=$SAFEBIN" -- --repo "$R" --dry-run
check "ホスト CLI が無いと止まる" 20 "$RC"
has "ホスト CLI が無い: 理由" "$OUT" "[host-cli-missing]"
# --help に雛形のフラグが無い(2 段照合の 2 段目)
run_loop hs "SELFTEST_HELP_FILE=$W/help-missing.txt" -- --repo "$R" --dry-run
check "--help に雛形のフラグが無いと止まる" 20 "$RC"
has "--help の照合: 理由" "$OUT" "[help-mismatch]"
# 導入済みの同名プラグインの版の違い・解析できない・同じ版・無効。試験データは実物の形(トップは配列。要素は id・
# version・scope・enabled・installPath・installedAt・lastUpdated で、name は無い。installPath には実際の配布物を置いて内容も監視する)
plugin_item() { # $1=id $2=version $3=enabled(true|false) → 実物の形の要素 1 つ
  mkdir -p "$W/cache/market/${1%%@*}/$2"
  printf '{"id":"%s","version":"%s","scope":"user","enabled":%s,"installPath":"%s/cache/market/%s/%s","installedAt":"(dummy)","lastUpdated":"(dummy)"}' \
    "$1" "$2" "$3" "$W" "${1%%@*}" "$2"
}
printf '[%s]\n' "$(plugin_item dev-workflow@market 0.0.1 true)" >"$W/plugins-old.json"
printf 'not json\n' >"$W/plugins-bad.json"
printf '[%s,%s]\n' "$(plugin_item dev-workflow@market "$PLUGIN_VERSION" true)" "$(plugin_item other@market 1 true)" >"$W/plugins-same.json"
printf '[%s]\n' "$(plugin_item dev-workflow@market 0.0.1 false)" >"$W/plugins-disabled.json"
# 寛容さの試験: name の形・{"plugins": …} の形(実物の形ではないが、読める)
printf '[{"name":"dev-workflow","version":"0.0.1","enabled":true}]\n' >"$W/plugins-name.json"
printf '{"plugins":[{"id":"dev-workflow@market","version":"0.0.1"}]}\n' >"$W/plugins-obj.json"
run_loop hs "SELFTEST_PLUGINS_FILE=$W/plugins-old.json" -- --repo "$R" --dry-run
check "導入済みの同名プラグインの版が違うと止まる(実物の形: id)" 20 "$RC"
has "版の違い: 理由" "$OUT" "[plugin-version]"
run_loop hs "SELFTEST_PLUGINS_FILE=$W/plugins-name.json" -- --repo "$R" --dry-run
check "導入済みの版の違い(寛容さ: name の形)でも止まる" 20 "$RC"
run_loop hs "SELFTEST_PLUGINS_FILE=$W/plugins-obj.json" -- --repo "$R" --dry-run
check "導入済みの版の違い(寛容さ: {\"plugins\": …} の形)でも止まる" 20 "$RC"
run_loop hs "SELFTEST_PLUGINS_FILE=$W/plugins-bad.json" -- --repo "$R" --dry-run
check "plugin list --json を解析できないと止まる" 20 "$RC"
has "解析できない: 理由" "$OUT" "[plugin-list]"
run_loop hs "SELFTEST_PLUGINS_FILE=$W/plugins-same.json" SELFTEST_PLUGINS_STDERR=1 -- --repo "$R" --dry-run
check "同じ版なら続ける(stderr に警告があっても、一覧は stdout だけで解析する)" 0 "$RC"
has "同じ版: 報告に出る" "$(report_of "$OUT")" "同じ版 $PLUGIN_VERSION"
run_loop hs "SELFTEST_PLUGINS_FILE=$W/plugins-disabled.json" -- --repo "$R" --dry-run
check "無効の同名プラグインは版を問わない" 0 "$RC"
# 疎通の失敗(--dry-run でないとき)
: >"$REC/calls.log"
run_loop hs SELFTEST_AUTH_RC=1 -- --repo "$R" "${COMMON_ARGS[@]}"
check "疎通(auth status)の失敗で止まる" 20 "$RC"
has "疎通の失敗: 理由" "$OUT" "[auth]"
check "疎通の失敗: -p を起動しない" "" "$(calls)"
# ローカルのデフォルトブランチが無い
newrepo nodef
G -C "$R" checkout -q -b trunk
G -C "$R" branch -q -D main
run_loop nodef -- --repo "$R" --dry-run
check "ローカルのデフォルトブランチが無いと止まる" 20 "$RC"
has "デフォルトブランチ: 理由" "$OUT" "[no-default-branch]"
# parent-child
newrepo pc
mkdir -p "$R/.claude"
printf 'root: sub\n' >"$R/.claude/project-profile.yml"
addtask pr-a
commit
run_loop pc -- --repo "$R" --dry-run
check "parent-child で止まる" 20 "$RC"
has "parent-child: 理由" "$OUT" "[parent-child]"

# profile の不正な値(固定した sha の commit から読む。profile は起動のたびに commit し直す)
newrepo prof
addtask pr-a
commit
profile_case() { # $1=ラベル $2=期待する終了コード $3=理由(空なら見ない) $4=profile の中身
  printf '%s\n' "$4" >"$R/.claude/project-profile.yml"
  commit localonly "profile $1"
  run_loop prof -- --repo "$R" --dry-run
  check "profile($1): 終了コード" "$2" "$RC"
  [ -z "$3" ] || has "profile($1): 理由" "$OUT" "$3"
}
mkdir -p "$R/.claude"
profile_case "構文エラー" 20 "[profile-invalid]" 'features: [unclosed'
profile_case "features がマッピングでない" 20 "[profile-invalid]" 'features: [1, 2]'
profile_case "features.loop がマッピングでない" 20 "[profile-invalid]" $'features:\n  loop: 5'
profile_case "bool(yes)" 20 "[profile-invalid]" $'features:\n  loop:\n    max_iterations: yes'
profile_case "bool(true)" 20 "[profile-invalid]" $'features:\n  loop:\n    max_consecutive_failures: true'
profile_case "小数" 20 "[profile-invalid]" $'features:\n  loop:\n    max_iterations: 1.5'
profile_case "文字列" 20 "[profile-invalid]" $'features:\n  loop:\n    max_iterations: "5"'
profile_case "0" 20 "[profile-invalid]" $'features:\n  loop:\n    time_budget: 0'
profile_case "負" 20 "[profile-invalid]" $'features:\n  loop:\n    iteration_timeout: -5'
profile_case "time_budget < iteration_timeout" 20 "[budget]" $'features:\n  loop:\n    time_budget: 100'
profile_case "task_dir が文字列でない" 20 "[profile-invalid]" 'task_dir: 5'
profile_case "task_dir が空" 20 "[profile-invalid]" 'task_dir: ""'
profile_case "task_dir が絶対パス" 20 "[profile-invalid]" 'task_dir: /tmp/x'
profile_case "task_dir の置換漏れ" 20 "[profile-invalid]" 'task_dir: "{{TASK_DIR}}"'
profile_case "task_dir が D21 に外れる(空白)" 20 "[task-dir]" 'task_dir: "docs/my tasks"'
profile_case "task_dir が D21 に外れる(先頭の -)" 20 "[task-dir]" 'task_dir: "-x/tasks"'
profile_case "値の無い項目は既定値" 0 "" $'features:\n  loop:\n    max_iterations:\n  other: 1'
has "値の無い項目は既定値: 実効値" "$(report_of "$OUT")" "max_iterations=5(既定)"
profile_case "未知のキーは無視して報告する" 0 "" $'features:\n  loop:\n    host_argv: [x]\n    allowed_tools: y'
has "未知のキー: 報告" "$(report_of "$OUT")" "未知のキーは無視した: allowed_tools,host_argv"
# PyYAML が無いのに profile がある / profile が無ければ PyYAML は要らない
run_loop prof "PYTHONPATH=$W/noyaml" -- --repo "$R" --dry-run
check "PyYAML が無いのに profile があると止まる" 20 "$RC"
has "PyYAML が無い: 理由" "$OUT" "[no-pyyaml]"
newrepo noprof
addtask pr-a
commit
run_loop noprof "PYTHONPATH=$W/noyaml" -- --repo "$R" --dry-run
check "profile が無ければ PyYAML が無くても続ける" 0 "$RC"
# 検出で決まった task_dir が D21 に外れる(worktree を消して exit 20)
newrepo detd21
addtask pr-a - 1 "docs/my tasks"
rm -rf "$R/docs/tasks"
commit
run_loop detd21 -- --repo "$R" --dry-run
check "検出で決まった task_dir が D21 に外れると止まる" 20 "$RC"
has "検出の task_dir: 理由" "$OUT" "[task-dir]"
check "検出の task_dir: worktree を消す" 1 "$(wt_count)"

# 追跡外のタスク(人の作業ツリーにだけあるメタ行つき MD / メタ行だけ未 commit)
newrepo untr
addtask pr-a
addtask meta-later - 0
commit
addtask pr-untracked
run_loop untr -- --repo "$R" --dry-run
check "人の作業ツリーにだけあるメタ行つき MD で止まる" 20 "$RC"
has "追跡外: 理由" "$OUT" "[untracked-task]"
has "追跡外: 一覧に出る" "$OUT" "進行中_pr-untracked.md"
check "追跡外: worktree を消す" 1 "$(wt_count)"
rm -f "$R/docs/tasks/進行中_pr-untracked.md"
addtask meta-later - 1
run_loop untr -- --repo "$R" --dry-run
check "メタ行だけ未 commit で止まる" 20 "$RC"
has "メタ行だけ未 commit: 一覧に出る" "$OUT" "進行中_meta-later.md"
G -C "$R" checkout -q -- docs/tasks/進行中_meta-later.md

# 2026-09-23 決定 19 の帰結(D8): 遅れ・分岐・origin の sha がローカルに無い・ls-remote の失敗と固まり
newrepo d8
addtask pr-a
commit
git clone -q "sshstub:$B" "$W/repos/d8-other" >/dev/null 2>&1
echo other >"$W/repos/d8-other/other.txt"
G -C "$W/repos/d8-other" add -A
G -C "$W/repos/d8-other" commit -q -m other
SELFTEST_REC="$W/rec/setup" G -C "$W/repos/d8-other" push -q origin main
run_loop d8 -- --repo "$R" --dry-run
check "origin の sha がローカルに無いと止まる" 20 "$RC"
has "判定できない: 理由" "$OUT" "[origin-unknown]"
SELFTEST_REC="$W/rec/setup" G -C "$R" fetch -q origin
run_loop d8 -- --repo "$R" --dry-run
check "遅れで止まる" 20 "$RC"
has "遅れ: 理由" "$OUT" "[behind]"
echo local >"$R/local.txt"
commit localonly "local diverge"
run_loop d8 -- --repo "$R" --dry-run
check "分岐で止まる" 20 "$RC"
has "分岐: 理由" "$OUT" "[diverged]"
newrepo lsfail
addtask pr-a
commit
G -C "$R" remote set-url origin "sshstub:$W/repos/nonexistent.git"
run_loop lsfail -- --repo "$R" --dry-run
check "ls-remote の失敗で止まる" 20 "$RC"
has "ls-remote の失敗: 理由" "$OUT" "[ls-remote]"
newrepo lshang
addtask pr-a
commit
s0="$(date +%s)"
run_loop lshang SELFTEST_SSH_HANG=1 -- --repo "$R" --dry-run --net-timeout 2
check "ls-remote が固まる(応答しない ssh)と止まる" 20 "$RC"
has "固まる ssh: 理由" "$OUT" "[ls-remote]"
if [ $(( $(date +%s) - s0 )) -lt 40 ]; then ok "固まる ssh: --net-timeout で打ち切る"; else ng "固まる ssh: --net-timeout で打ち切る"; fi

# worktree の置き場・状態ディレクトリが人のチェックアウトの中(symlink・.. 経由・linked worktree の main を含む)
newrepo inside
addtask pr-a
commit
G -C "$R" worktree add -q -b lnk "$W/repos/inside-lnk"
ln -s "$R" "$W/alias-inside"
mkdir -p "$W/repos/sibling"
st_before="$(GIT_OPTIONAL_LOCKS=0 git -C "$R" status --short)"
newrec inside
run_loop inside -- --repo "$R" --dry-run --worktree-root "$W/alias-inside/wt"
check "worktree の置き場(symlink 経由で人のチェックアウトの中)で止まる" 20 "$RC"
has "worktree の置き場(symlink): 理由" "$OUT" "[worktree-root]"
run_loop inside -- --repo "$R" --dry-run --worktree-root "$W/repos/sibling/../inside/wt"
check "worktree の置き場(.. 経由で人のチェックアウトの中)で止まる" 20 "$RC"
has "worktree の置き場(..): 理由" "$OUT" "[worktree-root]"
run_loop inside -- --repo "$W/repos/inside-lnk" --dry-run --worktree-root "$R/wt"
check "worktree の置き場(linked worktree から起動し、main の worktree の中)で止まる" 20 "$RC"
has "worktree の置き場(main の worktree): 理由" "$OUT" "[worktree-root]"
f "worktree の置き場: 人のチェックアウトの中に作らない" test -e "$R/wt"
run_loop inside "XDG_STATE_HOME=$W/alias-inside/xdg" -- --repo "$R" --dry-run
check "状態ディレクトリ(symlink 経由で人のチェックアウトの中)で止まる" 20 "$RC"
has "状態ディレクトリ: 理由" "$OUT" "[state-dir]"
f "状態ディレクトリ: 拒否したら作らない" test -e "$R/xdg"
INSIDE_ID="$(printf '%s' "$(cd "$R/.git" && pwd -P)" | sha256sum | cut -c1-16)"
mkdir -p "$R/xdg2/dev-workflow/loop/$INSIDE_ID"
chmod 755 "$R/xdg2/dev-workflow/loop/$INSIDE_ID"
run_loop inside "XDG_STATE_HOME=$R/xdg2" -- --repo "$R" --dry-run
check "状態ディレクトリ(既存・人のチェックアウトの中)で止まる" 20 "$RC"
check "状態ディレクトリ: 拒否したら既存のモードを変えない" 755 "$(stat -c %a "$R/xdg2/dev-workflow/loop/$INSIDE_ID")"
f "状態ディレクトリ: 拒否したらロックを作らない" test -e "$R/xdg2/dev-workflow/loop/$INSIDE_ID/lock"
rm -rf "$R/xdg2"
check "人のチェックアウト: 置き場の検査の前後で git status --short が変わらない" "$st_before" "$(GIT_OPTIONAL_LOCKS=0 git -C "$R" status --short)"

# 二重起動(別の worktree から起動しても同じロック)+ シグナル TERM(143)
newrepo dup
addtask longsleep-a 2026-01-01
addtask pr-b 2026-01-02
commit
G -C "$R" worktree add -q -b other "$W/repos/dup-wt2"
newrec dup
start_bg dup -- --repo "$R" "${COMMON_ARGS[@]}"
if wait_file "$REC/started-longsleep-a" 30; then
  run_loop dup -- --repo "$W/repos/dup-wt2" --dry-run
  check "二重起動(別の worktree から)で止まる" 20 "$RC"
  has "二重起動: 理由" "$OUT" "[locked]"
  kill -TERM "$BG_PID"
  wait_bg
  check "シグナル TERM で 143" 143 "$BG_RC"
  for p in $(cat "$REC/pid-longsleep-a" "$REC/spawned-longsleep-a" 2>/dev/null); do
    if wait_dead "$p" 5; then ok "TERM: 子と子孫が片付く(pid $p)"; else ng "TERM: 子と子孫が片付く(pid $p が残った)"; fi
  done
  t "TERM: worktree が残る(除外の印)" locked_reason_of "docs/tasks/進行中_longsleep-a.md"
  SD="$(state_dir dup)"
  f "TERM: 差分が無ければ止めの印を置かない" test -e "$SD/stop-mark.md"
  f "TERM: 差分が無ければ周の途中の印を消す" test -e "$SD/inflight"
  hasnt "子がロックの fd を持たない" "$REC/fd-longsleep-a" "$SD/lock"
  has "子の fd の記録がある" "$REC/fd-longsleep-a" "/"
  hasnt "TERM: 次の候補を起動しない" "$REC/calls.log" "pr-b"
else
  ng "二重起動: 背景の loop.sh の周が始まらない"
  kill -KILL "$BG_PID" 2>/dev/null; wait_bg
fi

fi

# ════════════════ 起動する: setup.sh の --link の配置 ════════════════
if want link; then
newrepo link
addtask pr-a
commit
newrec link
mkdir -p "$W/linkproj/.claude/skills"
ln -s "$PLUG/skills/ship-task" "$W/linkproj/.claude/skills/ship-task"
LOOP_BIN="$W/linkproj/.claude/skills/ship-task/scripts/loop.sh"
run_loop link -- --repo "$R" --dry-run
LOOP_BIN="$LOOP"
check "--link の配置から起動する(clone のルートに解決される)" 0 "$RC"
has "--link: --plugin-dir が検証済みコピー" "$OUT" "/active/plugin --permission-mode"

fi

# ════════════════ 自動メモリ(H32。実物の読み書きの試験ではなく、子への受け渡し)════════════════
if want memory; then
for memory_mode in implementation discover; do
  for memory_parent in unset 0 1; do
    memory_case="memory-$memory_mode-$memory_parent"
    newrepo "$memory_case"
    addtask pr-memory 2026-01-01
    commit
    memory_home="$W/$memory_case-home"
    memory_settings="$memory_home/.claude/settings.json"
    memory_file="$memory_home/.claude/projects/-fixture/memory/MEMORY.md"
    mkdir -p "$(dirname "$memory_file")"
    printf '{"autoMemoryEnabled":true}\n' >"$memory_settings"
    printf '# Existing memory\n\nH32 fixture: preserve these bytes.\n' >"$memory_file"
    cp "$memory_settings" "$W/$memory_case-settings.before"
    cp "$memory_file" "$W/$memory_case-memory.before"
    memory_env=("HOME=$memory_home")
    if [ "$memory_parent" != unset ]; then memory_env+=("CLAUDE_CODE_DISABLE_AUTO_MEMORY=$memory_parent"); fi
    memory_args=()
    memory_child=pr-memory
    if [ "$memory_mode" = discover ]; then
      memory_args=(--discover=data-audit)
      memory_child=disc-data-audit
    fi
    newrec "$memory_case-dry"
    run_loop "$memory_case-dry" "${memory_env[@]}" -- --repo "$R" --dry-run "${memory_args[@]}"
    check "H32($memory_mode・親=$memory_parent): dry-run が成功" 0 "$RC"
    has "H32($memory_mode・親=$memory_parent): dry-run は子の無効化を表示" "$OUT" 'CLAUDE_CODE_DISABLE_AUTO_MEMORY=1'
    check "H32($memory_mode・親=$memory_parent): dry-run は子を起動しない" "" "$(calls)"
    newrec "$memory_case"
    run_loop "$memory_case" "${memory_env[@]}" -- --repo "$R" "${memory_args[@]}" "${COMMON_ARGS[@]}"
    check "H32($memory_mode・親=$memory_parent): 周が成功" 0 "$RC"
    check "H32($memory_mode・親=$memory_parent): 対象の子を起動" "$memory_child" "$(calls)"
    t "H32($memory_mode・親=$memory_parent): 子は自動メモリを無効化" grep -qx 'CLAUDE_CODE_DISABLE_AUTO_MEMORY=1' "$REC/env-$memory_child"
    # 補助 CLI は loop.sh 自身から起動する。ここへの漏出も調べ、親の shell が不変という自明な確認で済ませない。
    for memory_aux in --help auth plugin; do
      t "H32($memory_mode・親=$memory_parent): 補助 $memory_aux の環境を取得" test -s "$REC/env-aux-$memory_aux"
      if [ "$memory_parent" = unset ]; then
        f "H32($memory_mode・親=$memory_parent): 補助 $memory_aux は未設定を保持" grep -q '^CLAUDE_CODE_DISABLE_AUTO_MEMORY=' "$REC/env-aux-$memory_aux"
      else
        t "H32($memory_mode・親=$memory_parent): 補助 $memory_aux は親の値を保持" grep -qx "CLAUDE_CODE_DISABLE_AUTO_MEMORY=$memory_parent" "$REC/env-aux-$memory_aux"
      fi
    done
    for memory_fixture in settings memory; do
      if [ "$memory_fixture" = settings ]; then memory_actual="$memory_settings"; else memory_actual="$memory_file"; fi
      t "H32($memory_mode・親=$memory_parent): $memory_fixture の内容をバイト単位で保持" python3 -c \
        'import pathlib,sys; sys.exit(pathlib.Path(sys.argv[1]).read_bytes() != pathlib.Path(sys.argv[2]).read_bytes())' \
        "$W/$memory_case-$memory_fixture.before" "$memory_actual"
    done
  done
done
fi

# ════════════════ H28: 短い値付きフラグ ════════════════
if want h28; then
# スタブの陽性対照。-p が値になると、解析失敗でも起動記録を必ず残す。
for swallowed in -p --allowedTools; do
  newrec "h28-control-${swallowed#-}"
  build_env h28-control SELFTEST_PARSE_ARGV=1
  env -i "${ENV_ARGS[@]}" "$ALTBIN/claude-alt" -cn "$swallowed" Read </dev/null >"$W/out/h28-control.txt" 2>&1
  check "H28 スタブ: $swallowed を値として消費すると印字モードを失う" 64 "$?"
  check "H28 スタブ: 解析前の子起動記録($swallowed)" claude-alt "$(cat "$REC/child-starts.log" 2>/dev/null)"
  check "H28 スタブ: -n の値が $swallowed" "$swallowed" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$REC/parsed.json" 2>/dev/null)"
  if [ "$swallowed" = --allowedTools ]; then
    f "H28 スタブ: 消費された許可リストは本来の引数として残らない" \
      python3 -c 'import json,sys; sys.exit("allowedTools" not in json.load(open(sys.argv[1])))' "$REC/parsed.json"
  fi
done

newrepo h28
H28_BAD=(-n -cn -nc -cn=demo -ndemo -cndemo -n=demo -c=n -z -cz -zc -cz=demo -zdemo -czdemo -z=demo -c=z --name)
for i in "${!H28_BAD[@]}"; do addtask "pr-h28-bad-$i"; done
addtask pr-h28-separated
for i in 0 1 2 3; do addtask "pr-h28-good-$i"; done
commit
for i in "${!H28_BAD[@]}"; do
  tok="${H28_BAD[$i]}"
  newrec "h28-bad-$i"
  run_loop "h28-bad-$i" SELFTEST_PARSE_ARGV=1 -- --repo "$R" --only "pr-h28-bad-$i" \
    --host-argv "$ALTBIN/claude-alt" --host-argv "$tok" "${COMMON_ARGS[@]}"
  check "H28 '$tok': 終了コード20" 20 "$RC"
  has "H28 '$tok': 拒否理由" "$OUT" "[host-argv]"
  check "H28 '$tok': 解析前の子起動記録が空" "" "$(cat "$REC/child-starts.log" 2>/dev/null)"
  check "H28 '$tok': 対象タスクを起動しない" "" "$(calls)"
done
newrec h28-separated
run_loop h28-separated SELFTEST_PARSE_ARGV=1 -- --repo "$R" --only pr-h28-separated \
  --host-argv "$ALTBIN/claude-alt" --host-argv --name --host-argv demo "${COMMON_ARGS[@]}"
check "H28 '--name demo': 終了コード20" 20 "$RC"
has "H28 '--name demo': 拒否理由" "$OUT" "[host-argv]"
check "H28 '--name demo': 解析前の子起動記録が空" "" "$(cat "$REC/child-starts.log" 2>/dev/null)"
check "H28 '--name demo': 対象タスクを起動しない" "" "$(calls)"

# 値に n がある長い引数、許可した単独フラグ、既定起動を解析して確認する。
H28_GOOD=(--verbose --name=demo --name=n default)
for i in "${!H28_GOOD[@]}"; do
  tok="${H28_GOOD[$i]}"
  args=()
  [ "$tok" = default ] || args=(--host-argv "$ALTBIN/claude-alt" --host-argv "$tok")
  newrec "h28-good-$i"
  run_loop "h28-good-$i" SELFTEST_PARSE_ARGV=1 -- --repo "$R" --only "pr-h28-good-$i" \
    "${args[@]}" --allowed-tools Read "${COMMON_ARGS[@]}"
  check "H28 '$tok': 1 周回って終わる" 0 "$RC"
  check "H28 '$tok': 対象タスクを起動する" "pr-h28-good-$i" "$(calls)"
  check "H28 '$tok': 解析前の子起動記録が1行" 1 "$(wc -l <"$REC/child-starts.log" 2>/dev/null)"
  t "H28 '$tok': 許可リストと固定フラグの役割を保つ" python3 - "$REC/parsed.json" "$PLUG" "$tok" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
expected = {"allowedTools": ["Read"], "print": True, "output-format": "json",
            "setting-sources": "user", "strict-mcp-config": True,
            "permission-mode": "acceptEdits", "permission-prompts": "none"}
assert all(p.get(key) == value for key, value in expected.items()), p
from pathlib import Path
copy = Path(p['plugin-dir']); state = json.loads((copy.parent / 'environment.json').read_text())
assert state['copy'] == str(copy) and state['root'] == sys.argv[2], state
assert "PermissionRequest" in json.loads(p["settings"])["hooks"], p
if sys.argv[3].startswith("--name="):
    assert p.get("name") == sys.argv[3].split("=", 1)[1], p
elif sys.argv[3] == "--verbose":
    assert p.get("verbose") is True, p
PY
done
fi

# ════════════════ --dry-run・argv ════════════════
if want argv; then
newrepo argv
addtask pr-argv 2026-01-01
addtask pr-second 2026-01-02
commit
newrec argv
run_loop argv -- --repo "$R" --dry-run
check "--dry-run: 終了コード 0" 0 "$RC"
check "--dry-run: -p を起動しない" "" "$(calls)"
check "--dry-run: worktree が残らない" 1 "$(wt_count)"
DRY_COPY="$(sed -n 's/.*--plugin-dir \([^ ]*\) --permission-mode.*/\1/p' "$OUT" | head -1)"
t "--dry-run: コピーの状態控えがある" test -f "${DRY_COPY%/plugin}/environment.json"
has "--dry-run: 解決後の argv が出る(実行ファイルは絶対パス)" "$OUT" "解決後の argv: $STUBBIN/claude -p --output-format json --setting-sources user --strict-mcp-config --plugin-dir $DRY_COPY --permission-mode acceptEdits --permission-prompts none"  # <!-- validate-allow: loop.sh --dry-run が出す解決後の argv の文字列を照合する(起動はしない) -->
has "--dry-run: 対象の一覧が出る" "$OUT" "docs/tasks/進行中_pr-argv.md"
hasnt "--dry-run: 疎通(auth status)を打たない" "$REC/aux.log" "auth status"
has "--dry-run: --help で照合する" "$REC/aux.log" "claude --help"
has "--dry-run: plugin list で照合する" "$REC/aux.log" "claude plugin list --json"
printf '{"mcpServers":{}}\n' >"$W/mcp.json"
run_loop argv DEV_WORKFLOW_HOST_CLI= OLDPWD="$W/oldpwd-marker" -- --repo "$R" --only pr-argv --allowed-tools 'Bash(git:*)' --allowed-tools Read \
  --mcp-config "$W/mcp.json" "${COMMON_ARGS[@]}"
check "argv: 1 周回って終わる" 0 "$RC"
A="$REC/argv-pr-argv"
ARGV_COPY="$(python3 - "$A" "$PLUG" <<'PY2'
import json,sys
from pathlib import Path
tokens=Path(sys.argv[1]).read_text().splitlines(); copy=Path(tokens[tokens.index('--plugin-dir')+1])
state=json.loads((copy.parent/'environment.json').read_text())
assert state['copy']==str(copy) and state['root']==sys.argv[2]
print(copy)
PY2
)"
argv_has_seq() { # $1..=連続するトークン
  python3 - "$A" "$@" <<'PY'
import sys
toks = open(sys.argv[1], encoding="utf-8").read().split("\n")[1:]
want = sys.argv[2:]
sys.exit(0 if any(toks[i:i + len(want)] == want for i in range(len(toks))) else 1)
PY
}
t "argv: -p --output-format json" argv_has_seq -p --output-format json
t "argv: --setting-sources user(2026-09-23 決定 16)" argv_has_seq --setting-sources user
t "argv: --strict-mcp-config(2026-09-23 決定 16)" argv_has_seq --strict-mcp-config
t "argv: --plugin-dir <プラグインルート>" argv_has_seq --plugin-dir "$ARGV_COPY"
t "argv: --permission-mode acceptEdits" argv_has_seq --permission-mode acceptEdits
t "argv: --permission-prompts none" argv_has_seq --permission-prompts none
t "argv: --mcp-config <絶対パス>" argv_has_seq --mcp-config "$W/mcp.json"
t "argv: --allowedTools <値>" argv_has_seq --allowedTools 'Bash(git:*)' Read
t "argv: 許可リスト → MCP の設定 → 隔離・権限のフラグ(最後)の並び" argv_has_seq --allowedTools 'Bash(git:*)' Read \
  --mcp-config "$W/mcp.json" -p --output-format json --setting-sources user --strict-mcp-config --plugin-dir "$ARGV_COPY" \
  --permission-mode acceptEdits --permission-prompts none
t "argv: 権限のフラグの後に許可の仲介の --settings(argv の最後)" argv_has_seq --permission-prompts none --settings \
  "$(sed '/^$/d' "$A" | tail -1)"
t "argv: hook は保持 hash の loader と固定権限判定器を実行する" python3 - "$A" "$SAFEBIN/python3" "$ARGV_COPY" <<'PY2'
import json,sys,shlex
from pathlib import Path
args=Path(sys.argv[1]).read_text().splitlines()
settings=json.loads(args[args.index('--settings')+1])
cmd=shlex.split(settings['hooks']['PermissionRequest'][0]['hooks'][0]['command'])
assert cmd[:4]==[sys.argv[2],'-I','-B','-c'],cmd
assert 'hashlib.sha256(raw).hexdigest() != expected' in cmd[4]
assert cmd[7:]==['hook','--state',str(Path(sys.argv[3]).parent/'environment.json'),'--expect-sha256',cmd[11],'--path','skills/ship-task/scripts/loop-permission.py'],cmd
assert len(cmd[5])==64 and len(cmd[11])==64
PY2

hasnt "argv: 全許可のフラグが載らない" "$A" "--dangerously-skip-permissions"
hasnt "argv: 全許可のモードが載らない" "$A" "bypassPermissions"
check "argv: プロンプトが stdin で届く" "/dev-workflow:ship-task --task=docs/tasks/進行中_pr-argv.md --unattended" "$(cat "$REC/prompt-pr-argv" 2>/dev/null)"
f "argv: プロンプトを位置引数で渡さない" grep -qF -- "--unattended" "$A"
has "子の環境: 周の印がある" "$REC/env-pr-argv" "DEV_WORKFLOW_LOOP_ITER="
f "子の環境: DEV_WORKFLOW_HOST_CLI を外す" grep -q '^DEV_WORKFLOW_HOST_CLI=' "$REC/env-pr-argv"
f "子の環境: OLDPWD を外す(OLDPWD を立てて起動しても、loop.sh の cd の後でも渡らない)" grep -q '^OLDPWD=' "$REC/env-pr-argv"
case "$(cat "$REC/cwd-pr-argv" 2>/dev/null)" in "$R.loop/"*) ok "子の cwd は使い捨ての worktree" ;; *) ng "子の cwd は使い捨ての worktree($(cat "$REC/cwd-pr-argv" 2>/dev/null))" ;; esac
# --host-argv で置き換えても、隔離のフラグは後ろに載り、補助の CLI も上書き後の実行ファイルで打たれる
: >"$REC/aux.log"
run_loop argv -- --repo "$R" --only pr-second --host-argv "$ALTBIN/claude-alt" --host-argv --model=m1 "${COMMON_ARGS[@]}"
check "--host-argv: 1 周回って終わる" 0 "$RC"
A="$REC/argv-pr-second"
check "--host-argv: 実行ファイルが置き換わる" "$ALTBIN/claude-alt" "$(head -1 "$A" 2>/dev/null)"
t "--host-argv: 前置きのフラグの後ろに隔離のフラグが載る" argv_has_seq --model=m1 -p --output-format json --setting-sources user --strict-mcp-config
has "--host-argv: --help も上書き後で打つ" "$REC/aux.log" "claude-alt --help"
has "--host-argv: plugin list も上書き後で打つ" "$REC/aux.log" "claude-alt plugin list --json"
has "--host-argv: auth status も上書き後で打つ" "$REC/aux.log" "claude-alt auth status"
f "--host-argv: 既定の実行ファイルで補助の CLI を打たない" grep -q '^claude ' "$REC/aux.log"
# PATH に `.` があっても、実行ファイルは起動時に絶対パスへ解決する(周の cwd〈worktree〉の中の claude を起動しない)
newrepo dotpath
addtask pr-a
printf '#!/bin/sh\necho pwned >"$SELFTEST_REC/pwned"\ncat >/dev/null\nexit 0\n' >"$R/claude"
chmod +x "$R/claude"
commit
newrec dotpath
run_loop dotpath "PATH=.:$STUBBIN:$SAFEBIN" -- --repo "$R" "${COMMON_ARGS[@]}"
check "PATH に . があっても 1 周回って終わる" 0 "$RC"
f "PATH に . があっても、リポジトリの中の claude を起動しない" test -e "$REC/pwned"
check "PATH に . があっても、スタブ(絶対パス)を起動する" "pr-a" "$(calls)"
has "PATH に . があっても: 解決後の argv は絶対パス" "$OUT" "解決後の argv: $STUBBIN/claude "

fi

# ════════════════ 並び・task_dir・先行・環境 ════════════════
if want order; then
newrepo order
addtask pr-b 2026-01-02
addtask pr-c 2026-01-01
addtask pr-a 2026-01-02
addtask pr-z
addtask pr-y
commit
newrec order
run_loop order -- --repo "$R" --dry-run
got="$(sed -n '/^対象の一覧/,/^止まった理由/p' "$OUT" | sed -n 's/^  docs\/tasks\/進行中_\(.*\)\.md$/\1/p' | tr '\n' ' ')"
check "並び: 作成日の古い順・同じ日はファイル名順・作成日なしは最後" "pr-c pr-a pr-b pr-y pr-z " "$got"

newrepo tdir
addtask pr-work - 1 work/tasks
addtask pr-docs - 1 docs/tasks
mkdir -p "$R/.claude"
printf 'task_dir: work/tasks\n' >"$R/.claude/project-profile.yml"
commit
run_loop tdir -- --repo "$R" --dry-run
check "task_dir: profile の task_dir で起動する" 0 "$RC"
has "task_dir: そこから拾う" "$OUT" "work/tasks/進行中_pr-work.md"
hasnt "task_dir: 別の場所は拾わない" "$OUT" "docs/tasks/進行中_pr-docs.md"

newrepo lead
addtask pr-a
commit
echo lead >"$R/lead.txt"
commit localonly "leading-commit-subject"
run_loop lead -- --repo "$R" --dry-run
check "先行: ローカルが origin より先行でも続ける" 0 "$RC"
has "先行: 先行する commit を列挙する" "$OUT" "leading-commit-subject"

# 環境: GIT_DIR などのローカルな環境変数が効かず、GIT_CONFIG_SYSTEM の目印は loop.sh の git に効く
newrepo envr
addtask pr-a
addtask pr-remoteonly
commit
git clone -q --bare "$B" "$W/repos/envr-alt.git" >/dev/null 2>&1
git -C "$W/repos/envr-alt.git" branch -q task/pr-remoteonly main
newrepo envother
R="$W/repos/envr"
B="$W/repos/envr.remote.git"
run_loop envr GIT_DIR="$W/repos/envother/.git" GIT_WORK_TREE="$W/repos/envother" GIT_INDEX_FILE="$W/nonexistent-index" \
  GIT_CONFIG_COUNT=1 "GIT_CONFIG_KEY_0=url.sshstub:$W/repos/envr-alt.git.insteadOf" "GIT_CONFIG_VALUE_0=sshstub:$B" \
  -- --repo "$R" --dry-run
check "環境: GIT_DIR・GIT_WORK_TREE などが効かない" 0 "$RC"
has "環境: 対象のリポジトリのタスクを拾う" "$OUT" "docs/tasks/進行中_pr-a.md"
f "環境: GIT_CONFIG_KEY_<n> は効かない" grep -qF "作業ブランチ task/pr-remoteonly が origin にある" "$OUT"
printf '[url "sshstub:%s"]\n\tinsteadOf = sshstub:%s\n' "$W/repos/envr-alt.git" "$B" >"$W/gitconfig-system-envr"
run_loop envr "GIT_CONFIG_SYSTEM=$W/gitconfig-system-envr" -- --repo "$R" --dry-run
check "環境: GIT_CONFIG_SYSTEM つきで起動する" 0 "$RC"
has "環境: GIT_CONFIG_SYSTEM の目印は loop.sh の git に効く" "$OUT" "作業ブランチ task/pr-remoteonly が origin にある"

fi

# ════════════════ 読み飛ばし ════════════════
if want skip; then
newrepo skip
addtask pr-ok
addtask nometa - 0
addtask 'bad name'
addtask 'x$(id)'
addtask -lead
addtask localbr
addtask remotebr
addtask trackref
addtask lockedwt
addtask donecoex
cp "$R/docs/tasks/進行中_donecoex.md" "$R/docs/tasks/完了_donecoex.md"
addtask holdcoex
cp "$R/docs/tasks/進行中_holdcoex.md" "$R/docs/tasks/保留_holdcoex.md"
addtask onlyout
ln -s 進行中_pr-ok.md "$R/docs/tasks/進行中_symlinked.md"
commit
G -C "$R" branch task/localbr
SELFTEST_REC="$W/rec/setup" G -C "$R" push -q origin main:refs/heads/task/remotebr
G -C "$R" update-ref -d refs/remotes/origin/task/remotebr
G -C "$R" update-ref refs/remotes/origin/task/trackref HEAD
G -C "$R" worktree add -q --detach --lock --reason "dev-workflow-loop: docs/tasks/進行中_lockedwt.md" "$W/repos/skip-lockedwt" HEAD
newrec skip
ONLY_ARGS=()
for n in pr-ok nometa 'bad name' 'x$(id)' -lead localbr remotebr trackref lockedwt donecoex holdcoex symlinked; do ONLY_ARGS+=(--only "$n"); done
run_loop skip -- --repo "$R" "${ONLY_ARGS[@]}" "${COMMON_ARGS[@]}"
check "読み飛ばし: 終了コード 0" 0 "$RC"
check "読み飛ばし: 候補だけを回す(ほかはスタブの -p を起動しない)" "pr-ok" "$(calls)"
RP="$(report_of "$OUT")"
has "読み飛ばし: メタ行なし" "$RP" "進行中_nometa.md: 無人実行のメタ行が無い"
has "読み飛ばし: 空白(D21)" "$RP" "進行中_bad name.md: パスの文字が D21"
has "読み飛ばし: \$( (D21)" "$RP" '進行中_x$(id).md: パスの文字が D21'
has "読み飛ばし: 先頭の -(D21)" "$RP" "進行中_-lead.md: パスの文字が D21"
has "読み飛ばし: ローカルの task/{名}" "$RP" "進行中_localbr.md: 作業ブランチ task/localbr がローカルにある"
has "読み飛ばし: リモートの task/{名}" "$RP" "進行中_remotebr.md: 作業ブランチ task/remotebr が origin にある"
has "読み飛ばし: 追跡用の ref" "$RP" "進行中_trackref.md: 追跡用の ref refs/remotes/origin/task/trackref"
has "読み飛ばし: 追跡用の ref の消し方を添える" "$RP" "git branch -dr origin/task/trackref"
has "読み飛ばし: locked の worktree" "$RP" "進行中_lockedwt.md: 前の周が残した worktree"
has "読み飛ばし: 完了_ の同居" "$RP" "進行中_donecoex.md: 同じ task_dir に"
has "読み飛ばし: 保留_ の同居" "$RP" "進行中_holdcoex.md: 同じ task_dir に"
has "読み飛ばし: --only の外" "$RP" "進行中_onlyout.md: --only の外"
has "読み飛ばし: 通常ファイルでない(symlink)は理由つきで出す" "$RP" "進行中_symlinked.md: 通常ファイルでない"
check "追跡用の ref の案内は 1 回だけ(選定が 2 回あっても重複しない)" 1 "$(grep -c '^- task/trackref: ' "$RP")"
has "追跡用の ref で読み飛ばしたタスクは、保留のタスクと別の見出し" "$RP" "## 追跡用の ref で読み飛ばしたタスク"
hasnt "追跡用の ref だけなら、保留の見出しを出さない" "$RP" "## 保留のタスクを再び回す手順"

fi

# ════════════════ 同じ理由で残った worktree(H42)════════════════
if want locked; then
# 実 worktree の NUL 区切りを対象の関数へそのまま渡す。同じプロセスで再読取りも確かめる。
newrepo locked-read noorigin
mkdir -p "$W/locked-read"
python3 - "$TARGET" "$W/locked-read/functions.sh" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
start = text.index("load_worktrees() {")
end = text.index("\nfinish() {", start)
with open(sys.argv[2], "w", encoding="utf-8") as out:
    out.write("\n".join(re.findall(r"^declare -A LOCKED_[^\n]+", text, re.M)) + "\n")
    out.write(text[start:end])
PY
LOCK_READ_OUT="$W/locked-read/result.txt"
bash -s -- "$W/locked-read/functions.sh" "$R" "$W/locked-read" <<'SH' >"$LOCK_READ_OUT" 2>&1
set -euo pipefail
source "$1"
TOP="$2"; RUN_DIR="$3"; STATE="$3"; RUN_ID=current; STATE_GIT_UNSAFE=0
declare -A WT_OUTCOME=()
G() { git "$@"; }
rep() { printf '%s\n' "$@"; }
a="$RUN_DIR/same space"; b="$RUN_DIR/"$'same\nnewline'; c="$RUN_DIR/other"
reason='dev-workflow-loop: docs/tasks/進行中_same.md'
report_left_worktrees >"$RUN_DIR/zero.txt"
[ ! -s "$RUN_DIR/zero.txt" ]
git -C "$TOP" worktree add -q --detach --lock --reason "$reason" "$a" HEAD
WT_OUTCOME["$a"]='結末 A'
report_left_worktrees >"$RUN_DIR/one.txt"
git -C "$TOP" worktree add -q --detach --lock --reason "$reason" "$b" HEAD
git -C "$TOP" worktree add -q --detach --lock --reason 'dev-workflow-loop: 候補:refactor' "$c" HEAD
WT_OUTCOME["$b"]='結末 B'; WT_OUTCOME["$c"]='結末 C'
report_left_worktrees >"$RUN_DIR/many.txt"
# 同理由の 1 件を unlock しても残りの 1 件を保持する。実体は消さない。
git -C "$TOP" worktree unlock "$a"
report_left_worktrees >"$RUN_DIR/remaining.txt"
git -C "$TOP" worktree unlock "$b"
git -C "$TOP" worktree unlock "$c"
report_left_worktrees >"$RUN_DIR/zero-again.txt"
[ ! -s "$RUN_DIR/zero-again.txt" ]
[ -d "$a" ] && [ -d "$b" ] && [ -d "$c" ]
SH
check "H42: NUL 読取り・再読取りの実行" 0 "$?"
python3 - "$W/locked-read" <<'PY' >"$W/locked-read/check.txt" 2>&1
from pathlib import Path
import sys
root = Path(sys.argv[1])
a, b, c = (str(root / x) for x in ("same space", "same\nnewline", "other"))
reason = "dev-workflow-loop: docs/tasks/進行中_same.md"
rows = {a: f"- {a}(lock の理由: {reason})— 結末: 結末 A",
        b: f"- {b}(lock の理由: {reason})— 結末: 結末 B",
        c: f"- {c}(lock の理由: dev-workflow-loop: 候補:refactor)— 結末: 結末 C"}
for name, paths in [("zero", []), ("one", [a]), ("many", [a, b, c]),
                    ("remaining", [b, c]), ("zero-again", [])]:
    text = (root / f"{name}.txt").read_text()
    assert text.count("(lock の理由: ") == len(paths), (name, text)
    for path, row in rows.items():
        assert (row in text) == (path in paths), (name, path, text)
print("0/1/同理由2/異理由/空白/改行/個別結末/再読取り: PASS")
PY
check "H42: NUL 読取りで全件・各結末を保持し古い lock を消す" 0 "$?"

# 両モードを実際に起動する。残った worktree は一切変更せず、対象の周を起動しない。
for lm in task discover; do
  newrepo "locked-$lm"
  addtask locked
  commit
  newrec "locked-$lm"
  lp1="$W/repos/locked-$lm-a space"
  lp2="$W/repos/locked-$lm-b"
  lp3="$W/repos/locked-$lm-other"
  lp4="$W/repos/locked-$lm-foreign"
  lp5="$W/repos/locked-$lm-no-reason"
  if [ "$lm" = task ]; then
    lr='dev-workflow-loop: docs/tasks/進行中_locked.md'
    lo='dev-workflow-loop: docs/tasks/進行中_locked.md-x'
    LARGS=()
  else
    lr='dev-workflow-loop: 候補:refactor'
    lo='dev-workflow-loop: 候補:refactor-x'
    LARGS=(--discover=refactor)
  fi
  # 0 件では報告しない。dry-run なので候補の周は起動しない。
  run_loop "locked-$lm" -- --repo "$R" --dry-run "${LARGS[@]}"
  check "H42($lm): 0 件で終了 0" 0 "$RC"
  hasnt "H42($lm): 0 件で残存報告なし" "$(report_of "$OUT")" "残った worktree"
  G -C "$R" worktree add -q --detach --lock --reason "$lr" "$lp1" HEAD
  run_loop "locked-$lm" -- --repo "$R" "${LARGS[@]}"
  check "H42($lm): 1 件で読み飛ばす" 0 "$RC"
  has "H42($lm): 1 件の開始報告" "$(report_of "$OUT")" "残った worktree(過去の実行の分を含む): $lp1($lr)"
  G -C "$R" worktree add -q --detach --lock --reason "$lr" "$lp2" HEAD
  G -C "$R" worktree add -q --detach --lock --reason "$lo" "$lp3" HEAD
  G -C "$R" worktree add -q --detach --lock --reason 'foreign lock' "$lp4" HEAD
  G -C "$R" worktree add -q --detach --lock "$lp5" HEAD
  git -C "$R" worktree list --porcelain -z >"$W/locked-$lm-before.z"
  run_loop "locked-$lm" -- --repo "$R" "${LARGS[@]}"
  check "H42($lm): 同理由複数で終了 0" 0 "$RC"
  check "H42($lm): 対象の周を起動しない" "" "$(calls)"
  RP="$(report_of "$OUT")"
  for lp in "$lp1" "$lp2" "$lp3"; do
    if [ "$lp" = "$lp3" ]; then expected_reason="$lo"; else expected_reason="$lr"; fi
    has "H42($lm): 開始に全件($lp)" "$RP" "残った worktree(過去の実行の分を含む): $lp($expected_reason)— 結末:"
    has "H42($lm): 終了に全件($lp)" "$RP" "- $lp(lock の理由: $expected_reason)— 結末:"
  done
  # 読み飛ばしの理由だけを抽出して照合する。開始/終了報告にあるだけでは成功にしない。
  sed -n '/前の周が残した worktree がある/p' "$RP" >"$W/locked-$lm-skip.txt"
  for lp in "$lp1" "$lp2"; do
    printf -v quoted_lp '%q' "$lp"
    has "H42($lm): 読み飛ばしに全件($lp)" "$W/locked-$lm-skip.txt" "$quoted_lp"
    if [ "$lm" = discover ]; then
      has "H42($lm): 各パスの片付け($lp)" "$RP" "git worktree unlock $quoted_lp → git worktree remove $quoted_lp(調べてから)"
    fi
  done
  hasnt "H42($lm): 理由の部分一致で混ぜない" "$W/locked-$lm-skip.txt" "$lp3"
  hasnt "H42($lm): 非 loop 理由は報告しない" "$RP" "$lp4"
  hasnt "H42($lm): 理由なしの lock は報告しない" "$RP" "$lp5"
  git -C "$R" worktree list --porcelain -z >"$W/locked-$lm-after.z"
  t "H42($lm): 既存 worktree と lock を変更しない" cmp -s "$W/locked-$lm-before.z" "$W/locked-$lm-after.z"
  for lp in "$lp1" "$lp2" "$lp3" "$lp4" "$lp5"; do t "H42($lm): 実体を残す($lp)" test -d "$lp"; done
  G -C "$R" worktree unlock "$lp1"
  run_loop "locked-$lm" -- --repo "$R" "${LARGS[@]}"
  check "H42($lm): 1 件片付けても残りで読み飛ばす" 0 "$RC"
  check "H42($lm): 残り 1 件でも対象の周を起動しない" "" "$(calls)"
  has "H42($lm): 残り 1 件を報告" "$(report_of "$OUT")" "$lp2(lock の理由: $lr)"
  hasnt "H42($lm): unlock 済みは次の報告に残らない" "$(report_of "$OUT")" "$lp1"
  G -C "$R" worktree unlock "$lp2"
  run_loop "locked-$lm" -- --repo "$R" --dry-run "${LARGS[@]}"
  check "H42($lm): 該当 lock が 0 件なら選定できる" 0 "$RC"
  hasnt "H42($lm): 異なる理由だけでは読み飛ばさない" "$(report_of "$OUT")" "前の周が残した worktree がある"
  if [ "$lm" = task ]; then
    has "H42($lm): 対象タスクが候補に戻る" "$OUT" "  docs/tasks/進行中_locked.md"
  else
    has "H42($lm): 対象の発見元が候補に戻る" "$OUT" "  refactor(ブランチ task/候補-refactor-"
  fi
done
fi

# ════════════════ 判定・片付け・人のチェックアウト ════════════════
if want judge; then
newrepo judge
for n in pr degrade hold fail none hang rcnz prunpushed both untracked ignored reviews symreviews badjson leak badreviews; do addtask "$n-j"; done
addtask holdnoline-j - 1 docs/tasks "- **保留**(ship-task・2026-01-02): S4 古い保留の行 — 本文を直す"
commit
newrec judge
echo human >"$R/human-untracked.txt"
echo changed >>"$R/README.md"
idx_before="$(sha256sum "$R/.git/index" | cut -d' ' -f1)"
st_before="$(GIT_OPTIONAL_LOCKS=0 git -C "$R" status --short)"
run_loop judge -- --repo "$R" --max-iterations 50 --max-consecutive-failures 50 --iteration-timeout 4 "${COMMON_ARGS[@]}"
idx_after="$(sha256sum "$R/.git/index" | cut -d' ' -f1)"
st_after="$(GIT_OPTIONAL_LOCKS=0 git -C "$R" status --short)"
check "判定: 全周を回ってキューが空で終わる" 0 "$RC"
check "人のチェックアウト: git status --short が変わらない" "$st_before" "$st_after"
check "人のチェックアウト: .git/index のバイト列が変わらない" "$idx_before" "$idx_after"
judged() { # $1=名 $2=判定 $3=worktree(removed|kept)
  local rel="docs/tasks/進行中_$1.md"
  has "判定($1): $2" "$OUT" "$rel → $2"
  if [ "$3" = kept ]; then
    t "判定($1): worktree を残す" locked_reason_of "$rel"
  else
    f "判定($1): worktree を消す" locked_reason_of "$rel"
  fi
}
judged pr-j "正常(PR)" removed
judged degrade-j "正常(縮退)" removed
judged hold-j "正常(保留)" removed
t "判定(hold-j): 改名と保留の行が作業ブランチに commit されている" git -C "$R" cat-file -e "task/hold-j:docs/tasks/保留_hold-j.md"
judged fail-j "失敗(結末 失敗扱い)" kept
judged none-j "失敗(結末の行が無い)" kept
judged hang-j "失敗(時間切れ)" kept
judged rcnz-j "失敗(終了コード 3)" kept
judged prunpushed-j "失敗(食い違い: origin の task/prunpushed-j" kept
judged both-j "失敗(食い違い: HEAD の tree に docs/tasks/進行中_both-j.md が残っている)" kept
judged holdnoline-j "失敗(食い違い: 保留の行が 1 行増えていない" kept
judged untracked-j "正常(PR)" kept
judged ignored-j "正常(PR)" removed
judged reviews-j "正常(PR)" removed
judged symreviews-j "正常(PR)" removed
judged badjson-j "失敗(JSON が読めない)" kept
judged leak-j "正常(PR)" removed
judged badreviews-j "正常(PR)" removed
RP="$(report_of "$OUT")"
RD="$(dirname "$RP")"
cp_ok=0; cp_sym=0
for d in "$RD"/iter-*-reviews; do
  [ -e "$d" ] || continue
  [ -f "$d/r.md" ] && cp_ok=1
  [ -e "$d/s.md" ] && cp_sym=1
done
check "判定: .claude/reviews(実体)を状態ディレクトリへ写す" 1 "$cp_ok"
check "判定: .claude/reviews が symlink のときは写さない" 0 "$cp_sym"
has "判定: 未追跡が残った周は worktree を残して報告する" "$RP" "消せなかったので残した"
# 片付け: 時間切れで TERM を無視する孫と、setsid で別セッションにした印つきの子孫まで打ち切られる
for p in $(cat "$REC/pid-hang-j" "$REC/spawned-hang-j" 2>/dev/null); do
  if wait_dead "$p" 3; then ok "片付け(時間切れ): pid $p が止まる"; else ng "片付け(時間切れ): pid $p が止まる"; fi
done
check "片付け(時間切れ): 孫と別セッションの子孫を記録した" 2 "$(wc -l <"$REC/spawned-hang-j" 2>/dev/null | tr -d ' ')"
for p in $(cat "$REC/spawned-leak-j" 2>/dev/null); do
  if wait_dead "$p" 3; then ok "片付け(正常な周): 印つきの残りのプロセスが止まる"; else ng "片付け(正常な周): 印つきの残りのプロセスが止まる(pid $p)"; fi
done
has "判定: 照合に通った周は共有の config の差分が無いことを報告に出す" "$RP" "- 共有の config の差分: 無し"
hasnt "判定: 報告に許した項目の一覧を出さない" "$RP" "許した"
has "判定: .claude/reviews を写せなくても止めずに報告して続ける" "$RP" ".claude/reviews を写せなかった"
# 報告の「残った worktree」に、その周の結末を添える(この実行の分)・過去の実行の分は報告のパスを示す
has "残った worktree: この実行の分はその周の結末を添える" "$RP" "(lock の理由: dev-workflow-loop: docs/tasks/進行中_fail-j.md)— 結末: 周 "
has "残った worktree: 結末の判定" "$RP" "失敗(結末 失敗扱い)"
has "残った worktree: 消せなかった周の結末" "$RP" "worktree を消せなかった"
run_loop judge -- --repo "$R" --dry-run
has "残った worktree: 過去の実行の分は(過去の実行)とその実行の報告を示す" "$(report_of "$OUT")" "(過去の実行)結末はその実行の報告を見る: $RP"
# §5 の git の照合(HEAD が task/{名}・HEAD の tree の 完了_・保留_)
newrepo xjudge
for n in nobranch nodone; do addtask "$n-j"; done
commit
newrec xjudge
run_loop xjudge -- --repo "$R" --max-consecutive-failures 50 "${COMMON_ARGS[@]}"
check "判定(git の照合): キューが空で終わる" 0 "$RC"
has "判定(nobranch-j): HEAD が task/{名} でない" "$OUT" "docs/tasks/進行中_nobranch-j.md → 失敗(食い違い: HEAD が refs/heads/task/nobranch-j でない"
has "判定(nodone-j): HEAD の tree に 完了_ が無い" "$OUT" "docs/tasks/進行中_nodone-j.md → 失敗(食い違い: HEAD の tree に docs/tasks/完了_nodone-j.md が無い)"
newrepo xjudge2
addtask holdnofile-j
commit
newrec xjudge2
run_loop xjudge2 -- --repo "$R" "${COMMON_ARGS[@]}"
check "判定(holdnofile-j): キューが空で終わる" 0 "$RC"
has "判定(holdnofile-j): HEAD の tree に 保留_ が無い" "$OUT" "docs/tasks/進行中_holdnofile-j.md → 失敗(食い違い: HEAD の tree に docs/tasks/保留_holdnofile-j.md が無い)"

# 出力が全メッセージの配列の形(利用者の設定の verbose が効いたとき)でも、最後の type: "result" の要素で同じ判定になる
newrepo jarray
for n in pr hold fail; do addtask "$n-arr"; done
commit
newrec jarray
run_loop jarray SELFTEST_OUTPUT_ARRAY=1 -- --repo "$R" --max-consecutive-failures 5 "${COMMON_ARGS[@]}"
check "配列の形: 全周を回ってキューが空で終わる" 0 "$RC"
has "配列の形: 正常(PR)" "$OUT" "docs/tasks/進行中_pr-arr.md → 正常(PR)"
has "配列の形: 正常(保留)" "$OUT" "docs/tasks/進行中_hold-arr.md → 正常(保留)"
has "配列の形: 失敗(結末 失敗扱い)(JSON が読めない、にならない)" "$OUT" "docs/tasks/進行中_fail-arr.md → 失敗(結末 失敗扱い)"
has "配列の形: 拒否の欄を最後の result の要素から写す" "$(report_of "$OUT")" "stub-denied-array"
has "配列の形: 前の result の要素の拒否の欄も写す" "$(report_of "$OUT")" "stub-denied-old"
# 末尾の要素が result でない・空の配列・result の無い配列
newrepo jarray2
for n in hold-tail fail-tail pr-empty pr-noresult; do addtask "$n"; done
commit
newrec jarray2
run_loop jarray2 SELFTEST_OUTPUT_ARRAY=byname -- --repo "$R" --max-consecutive-failures 5 "${COMMON_ARGS[@]}"
check "配列の形(2): 全周を回ってキューが空で終わる" 0 "$RC"
has "配列の形: 末尾が result でなくても、最後の result の要素で判定する(保留)" "$OUT" "docs/tasks/進行中_hold-tail.md → 正常(保留)"
has "配列の形: 末尾が result でなくても、最後の result の要素で判定する(失敗扱い)" "$OUT" "docs/tasks/進行中_fail-tail.md → 失敗(結末 失敗扱い)"
has "配列の形: 空の配列は JSON が読めない" "$OUT" "docs/tasks/進行中_pr-empty.md → 失敗(JSON が読めない)"
has "配列の形: result の無い配列は JSON が読めない" "$OUT" "docs/tasks/進行中_pr-noresult.md → 失敗(JSON が読めない)"
# 結果の要素がターンごとに出る形(周の中でバックグラウンドに委託したとき): 拒否の欄は前の result の要素にだけ載り、
# 最後の result の欄は空。拒否はすべての result の要素から写し、結末は最後の result の要素から読む
newrepo jarray3
for n in pr-bg hold-bg; do addtask "$n"; done
commit
newrec jarray3
run_loop jarray3 SELFTEST_OUTPUT_ARRAY=byname -- --repo "$R" --max-consecutive-failures 5 "${COMMON_ARGS[@]}"
check "配列の形(3): 全周を回ってキューが空で終わる" 0 "$RC"
has "配列の形: 結果の要素が複数でも、結末は最後の result の要素から読む(PR)" "$OUT" "docs/tasks/進行中_pr-bg.md → 正常(PR)"
has "配列の形: 結果の要素が複数でも、結末は最後の result の要素から読む(保留)" "$OUT" "docs/tasks/進行中_hold-bg.md → 正常(保留)"
has "配列の形: 最後の result の拒否の欄が空でも、前の result の要素の拒否を写す" "$(report_of "$OUT")" "stub-denied-bg"
# 人の作業ツリーの未 commit の profile が効かない(D1)+ 締める向き(profile が小さい)
newrepo d1
mkdir -p "$R/.claude"
printf 'features:\n  loop:\n    max_iterations: 1\n' >"$R/.claude/project-profile.yml"
addtask pr-a 2026-01-01
addtask pr-b 2026-01-02
commit
printf 'features:\n  loop:\n    max_iterations: yes\n' >"$R/.claude/project-profile.yml"
newrec d1
run_loop d1 -- --repo "$R" "${COMMON_ARGS[@]}"
check "D1: 人の作業ツリーの未 commit の profile が効かない" 0 "$RC"
check "締める向き: profile の小さい値が効く(1 周)" "pr-a" "$(calls)"
has "締める向き: 最大周回数で止まる" "$OUT" "最大周回数"
has "締める向き: 実効値の出所は profile" "$(report_of "$OUT")" "max_iterations=1(profile)"
G -C "$R" checkout -q -- .claude/project-profile.yml
# 締める向き(profile が大きい → 既定値・起動引数は広げられる・profile は起動引数も締める)
newrepo tight
mkdir -p "$R/.claude"
printf 'features:\n  loop:\n    max_iterations: 100\n    max_consecutive_failures: 6\n' >"$R/.claude/project-profile.yml"
addtask pr-a
commit
run_loop tight -- --repo "$R" --dry-run
has "締める向き: profile が既定より大きいときは既定値" "$(report_of "$OUT")" "max_iterations=5(既定)"
run_loop tight -- --repo "$R" --dry-run --max-iterations 7 --max-consecutive-failures 9 --time-budget 99999
has "締める向き: 起動引数は広げられる" "$(report_of "$OUT")" "max_iterations=7(起動引数)"
has "締める向き: profile は起動引数も締める" "$(report_of "$OUT")" "max_consecutive_failures=6(profile)"
has "締める向き: 時間予算も起動引数で広げられる" "$(report_of "$OUT")" "time_budget=99999(起動引数)"

fi

# ════════════════ ブレーカーと停止条件 ════════════════
if want breakers; then
newrepo maxit
addtask pr-a 2026-01-01; addtask pr-b 2026-01-02; addtask pr-c 2026-01-03
commit
newrec maxit
run_loop maxit -- --repo "$R" --max-iterations 2 "${COMMON_ARGS[@]}"
check "最大周回数: 終了コード 0" 0 "$RC"
check "最大周回数: 2 周で止まる" "pr-a pr-b" "$(calls)"
has "最大周回数: 理由" "$OUT" "止まった理由: 最大周回数"

newrepo budget
addtask slow-a 2026-01-01; addtask slow-b 2026-01-02
commit
newrec budget
run_loop budget SELFTEST_SLOW=5 -- --repo "$R" --time-budget 10 --iteration-timeout 6 "${COMMON_ARGS[@]}"
check "時間予算: 終了コード 0" 0 "$RC"
check "時間予算: 残りが周の上限より短ければ次の周を始めない" "slow-a" "$(calls)"
has "時間予算: 理由" "$OUT" "止まった理由: 時間予算"

newrepo stopf
addtask stopfile-a 2026-01-01; addtask pr-b 2026-01-02
commit
newrec stopf
run_loop stopf "SELFTEST_STOP_FILE=$R/.claude/loop.stop" -- --repo "$R" "${COMMON_ARGS[@]}"
check "停止ファイル: 終了コード 0" 0 "$RC"
check "停止ファイル: 周の途中では打ち切らず、次の周の前に止まる" "stopfile-a" "$(calls)"
has "停止ファイル: 理由" "$OUT" "止まった理由: 停止ファイル"
rm -f "$R/.claude/loop.stop"
touch "$W/custom.stop"
run_loop stopf -- --repo "$R" --stop-file "$W/custom.stop" "${COMMON_ARGS[@]}"
check "停止ファイル(--stop-file): 最初の周の前に止まる" 0 "$RC"
check "停止ファイル(--stop-file): -p を起動しない" "stopfile-a" "$(calls)"

newrepo cfail
addtask fail-a 2026-01-01; addtask fail-b 2026-01-02; addtask fail-c 2026-01-03
commit
newrec cfail
run_loop cfail -- --repo "$R" "${COMMON_ARGS[@]}"
check "連続失敗: 終了コード 10" 10 "$RC"
has "連続失敗: 理由" "$OUT" "[consecutive-failures]"
check "連続失敗: 失敗の周の後に次のタスクのスタブが起動され、2 回で止まる" "fail-a fail-b" "$(calls)"
newrepo cfail2
addtask fail-a 2026-01-01; addtask pr-b 2026-01-02; addtask fail-c 2026-01-03; addtask pr-d 2026-01-04
commit
newrec cfail2
run_loop cfail2 -- --repo "$R" "${COMMON_ARGS[@]}"
check "連続失敗: 正常で 0 に戻る" 0 "$RC"
check "連続失敗: 正常を挟めば全周を回る" "fail-a pr-b fail-c pr-d" "$(calls)"

newrepo g1
addtask holdg1-a 2026-01-01; addtask holdg1c-b 2026-01-02; addtask pr-c 2026-01-03
commit
newrec g1
run_loop g1 -- --repo "$R" "${COMMON_ARGS[@]}"
check "G1 の保留の連続: 終了コード 10(G1 …・G1: … の両方を数える)" 10 "$RC"
has "G1 の保留の連続: 理由" "$OUT" "[consecutive-failures]"
check "G1 の保留の連続: 2 回で止まる" "holdg1-a holdg1c-b" "$(calls)"
newrepo g1b
addtask holdg1-a 2026-01-01
addtask holdold-b 2026-01-02 1 docs/tasks "- **保留**(ship-task・2026-01-01): G1 古い許可の拒否 — 許可リストを見直す"
addtask holdg1-c 2026-01-03; addtask pr-d 2026-01-04
commit
newrec g1b
run_loop g1b -- --repo "$R" "${COMMON_ARGS[@]}"
check "G1 の取り出し: 古い G1 の行が残っていても最後の行(S4)で判定する" 0 "$RC"
check "G1 の取り出し: ほかの結末で 0 に戻り全周を回る" "holdg1-a holdold-b holdg1-c pr-d" "$(calls)"
# 旧い G1 の行 + 番号の無い最新の行 → 最後の行で判定するので G1 に数えない(前の行の番号を返さない)
newrepo g1d
addtask holdg1-a 2026-01-01
addtask holdnocode-b 2026-01-02 1 docs/tasks "- **保留**(ship-task・2026-01-01): G1 古い許可の拒否 — 許可リストを見直す"
addtask holdg1-c 2026-01-03; addtask pr-d 2026-01-04
commit
newrec g1d
run_loop g1d -- --repo "$R" "${COMMON_ARGS[@]}"
check "G1 の取り出し: 最後の保留の行に番号が無ければ、古い G1 の行があっても G1 に数えない" 0 "$RC"
check "G1 の取り出し: 番号の無い最新の行で連続が 0 に戻り全周を回る" "holdg1-a holdnocode-b holdg1-c pr-d" "$(calls)"
newrepo g1c
addtask holds4-a 2026-01-01; addtask holds4-b 2026-01-02; addtask pr-c 2026-01-03
commit
newrec g1c
run_loop g1c -- --repo "$R" "${COMMON_ARGS[@]}"
check "G1 の取り出し: S4 の保留は数えない" 0 "$RC"
check "G1 の取り出し: S4 の保留が続いても全周を回る" "holds4-a holds4-b pr-c" "$(calls)"

# 正常な周で止まらない(比べる元を周ごとに取り直す): PR → PR → 保留
newrepo normal
addtask pr-a 2026-01-01; addtask pr-b 2026-01-02; addtask hold-c 2026-01-03
commit
newrec normal
run_loop normal -- --repo "$R" "${COMMON_ARGS[@]}"
check "正常な周で止まらない: push と gc を打っても次の周へ進む" 0 "$RC"
check "正常な周で止まらない: PR → PR → 保留" "pr-a pr-b hold-c" "$(calls)"
has "正常な周で止まらない: キューが空で終わる" "$OUT" "止まった理由: キューが空"
check "正常な周: 共有の config に branch.task/pr-a.* が無い" "" "$(git config --file "$R/.git/config" --get-regexp '^branch\.task/pr-a\.' 2>/dev/null)"
# 文書の無人公開 helper(SKILL.md の字面)を、利用者の設定の branch.autoSetupRebase = always のもとで打つ周:
# 共有の config に branch.* を書かず、照合に通って次の周へ進む。設定はこの周だけ GIT_CONFIG_SYSTEM で渡す(治具の
# identity・init.defaultBranch・gc.auto は global の $W/gitconfig にしか無いので GIT_CONFIG_GLOBAL は差し替えない。
# 共有の $W/gitconfig は書き換えない)
DP_CMD="$(doc_push_cmd)"
case "$DP_CMD" in
  "OK "*)
    DP_CMD="${DP_CMD#OK }"
    printf '[branch]\n\tautoSetupRebase = always\n' >"$W/gitconfig-system-asr"
    newrepo docpush
    # origin-repo.py には公開先を GitHub として読ませ、Git の送信だけ scratch bare remote へ差し替える。
    # helper が exact-SHA push と gh の作成後 REST 照合を実行する対照になる。
    G -C "$R" remote set-url origin https://github.com/o/r.git
    addtask docpush-a 2026-01-01; addtask pr-b 2026-01-02
    commit
    newrec docpush
    DP_BEFORE="$(git config --file "$R/.git/config" --get-regexp '^branch\.' 2>/dev/null)"
    run_loop docpush "PATH=$DP_GIT:$DP_GH:$STUBBIN:$SAFEBIN" "GIT_CONFIG_SYSTEM=$W/gitconfig-system-asr" "SELFTEST_REAL_GIT=$SAFEBIN/git" "SELFTEST_PUBLISH_BARE=$B" "SELFTEST_PUSH_CMD=$DP_CMD" -- --repo "$R" "${COMMON_ARGS[@]}"
    check "文書の push(autoSetupRebase = always): 周の中で設定が効いている(前提)" always "$(cat "$REC/asr-docpush-a" 2>/dev/null)"
    check "文書の push(autoSetupRebase = always): 照合に通り、キューが空で終わる" 0 "$RC"
    check "文書の push(autoSetupRebase = always): 次の周へ進む" "docpush-a pr-b" "$(calls)"
    has "文書の push(autoSetupRebase = always): 判定は正常(PR)" "$OUT" "docs/tasks/進行中_docpush-a.md → 正常(PR)"
    t "文書の push(autoSetupRebase = always): origin に作業ブランチがある" git -C "$B" rev-parse --verify -q refs/heads/task/docpush-a
    has "文書の push(autoSetupRebase = always): helper が gh で PR を作る" "$REC/docpush-gh.log" 'pr create -R github.com/o/r --base main --head task/docpush-a'
    has "文書の push(autoSetupRebase = always): helper が作成後 REST を照合する" "$REC/docpush-gh.log" 'api --hostname github.com repos/o/r/pulls/42'
    check "文書の push(autoSetupRebase = always): 共有の config の branch.* が増えない" "$DP_BEFORE" \
      "$(git config --file "$R/.git/config" --get-regexp '^branch\.' 2>/dev/null)"
    ;;
  *) ng "文書の push(autoSetupRebase = always): SKILL.md から無人の push を取り出す(${DP_CMD#NG })" ;;
esac

# 共有の git ディレクトリの変化(周の後に照合して止まる。§5 のネットワークの git を打たない)
shared_case() { # $1=振る舞い $2=ラベル [$3=準備]
  newrepo "sh-$1"
  G -C "$R" config selftest.a 1
  G -C "$R" config selftest.b 2
  G -C "$R" config extensions.worktreeConfig true
  printf '#!/bin/sh\nexit 0\n' >"$R/.git/hooks/selftest-hook"
  chmod 644 "$R/.git/hooks/selftest-hook"
  addtask "$1-a" 2026-01-01; addtask pr-b 2026-01-02
  commit
  newrec "sh-$1"
  : >"$REC/ssh.log"
  run_loop "sh-$1" -- --repo "$R" "${COMMON_ARGS[@]}"
  check "共有の状態($2): 終了コード 10" 10 "$RC"
  has "共有の状態($2): 理由" "$OUT" "[shared-state]"
  check "共有の状態($2): 次の周へ進まない" "$1-a" "$(calls)"
  if sed -n '/^--- stub-end/,$p' "$REC/ssh.log" | grep -q 'git-upload-pack'; then
    ng "共有の状態($2): §5 のネットワークの git(ls-remote)を打たない"
  else
    ok "共有の状態($2): §5 のネットワークの git(ls-remote)を打たない"
  fi
  t "共有の状態($2): 止めの印を置く" test -f "$(state_dir "sh-$1")/stop-mark.md"
}
shared_case cfgadd "config に項目を足す"
shared_case cfgpushremote "branch.task/{名}.pushRemote を足す"
shared_case cfgremote "branch.task/{名}.remote の値が origin でない"
shared_case pushu "-u つきの push が branch.task/{名}.remote・.merge を足す"
has "共有の状態(-u つきの push): 設定の 構造差分を報告する" "$(report_of "$OUT")" "保持済み config または worktree の構造が周の途中で変わった"
hasnt "共有の状態(-u つきの push): 設定値を報告しない" "$(report_of "$OUT")" "branch.task/pushu-a.merge"
shared_case cfgreorder "既存の項目の並べ替え"
shared_case hookchmod "hooks の既存のファイルに実行権を付ける"
shared_case infoexclude "info/exclude"
shared_case wtconfig "その周の worktree の config.worktree"
shared_case defmove "refs/heads/<DEF> の移動"
has "共有の状態(refs/heads/<DEF>): 差分を報告する" "$(report_of "$OUT")" "差分: refs/heads/main"
shared_case basetamper "比べる元の書き換え(周の中で状態ディレクトリの控えを書き直す)"
has "共有の状態(比べる元の書き換え): 理由を報告する" "$(report_of "$OUT")" "比べる元"
# ループの照合で止まった後、止めの印を消すと、最後に照合に通った状態(食い違いを見つけた状態ではない)との
# 差分を報告して続ける
newrepo xloopdiff
addtask pr-a 2026-01-01; addtask cfgadd-b 2026-01-02; addtask pr-c 2026-01-03
commit
newrec xloopdiff
run_loop xloopdiff -- --repo "$R" "${COMMON_ARGS[@]}"
check "ループの照合で止まる: 10" 10 "$RC"
rm -f "$(state_dir xloopdiff)/stop-mark.md"
run_loop xloopdiff -- --repo "$R" "${COMMON_ARGS[@]}"
check "ループの照合の後: 止めの印を消すと続く" 0 "$RC"
has "ループの照合の後: 食い違いを見つけた状態を残さない(digest 分類を報告する)" "$(report_of "$OUT")" "config の内容が変わった(sha256:"
hasnt "ループの照合の後: 変更した設定値を報告しない" "$(report_of "$OUT")" "selftest.added=yes"

fi

# ════════════════ シグナル ════════════════
if want signals; then
# HUP(129)
newrepo hup
addtask longsleep-a 2026-01-01; addtask pr-b 2026-01-02
commit
newrec hup
start_bg hup -- --repo "$R" "${COMMON_ARGS[@]}"
if wait_file "$REC/started-longsleep-a" 30; then
  kill -HUP "$BG_PID"; wait_bg
  check "シグナル HUP で 129" 129 "$BG_RC"
  for p in $(cat "$REC/pid-longsleep-a" "$REC/spawned-longsleep-a" 2>/dev/null); do
    if wait_dead "$p" 5; then ok "HUP: 子が片付く(pid $p)"; else ng "HUP: 子が片付く(pid $p)"; fi
  done
  t "HUP: worktree が残る" locked_reason_of "docs/tasks/進行中_longsleep-a.md"
else
  ng "HUP: 周が始まらない"; kill -KILL "$BG_PID" 2>/dev/null; wait_bg
fi
# 周の中で共有の config を変えてから TERM → 差分を報告・止めの印 → 次の起動は ls-remote を打たずに 20 →
# 印を消すと、最後に照合に通った状態との差分を報告して続ける
newrepo sigcfg
addtask pr-a 2026-01-01; addtask cfgsleep-b 2026-01-02; addtask pr-c 2026-01-03
commit
newrec sigcfg
start_bg sigcfg -- --repo "$R" "${COMMON_ARGS[@]}"
if wait_file "$REC/started-cfgsleep-b" 30; then
  kill -TERM "$BG_PID"; wait_bg
  check "TERM(config を変えた周): 143" 143 "$BG_RC"
  has "TERM(config を変えた周): 設定の 構造差分を報告する" "$(report_of "$BG_OUT")" "保持済み config または worktree の構造が周の途中で変わった"
  hasnt "TERM(config を変えた周): 設定値を報告しない" "$(report_of "$BG_OUT")" "selftest.tampered=yes"
  SD="$(state_dir sigcfg)"
  t "TERM(config を変えた周): 止めの印が残る" test -f "$SD/stop-mark.md"
  : >"$REC/ssh.log"
  run_loop sigcfg -- --repo "$R" "${COMMON_ARGS[@]}"
  check "止めの印: 次の起動は 20" 20 "$RC"
  has "止めの印: 理由と消し方" "$OUT" "stop-mark.md"
  f "止めの印: ネットワークの git(ls-remote)を打たない" grep -q 'git-upload-pack' "$REC/ssh.log"
  rm -f "$SD/stop-mark.md"
  run_loop sigcfg -- --repo "$R" "${COMMON_ARGS[@]}"
  check "止めの印を消すと続く" 0 "$RC"
  has "止めの印を消すと: 最後に照合に通った状態との差分を digest で報告する" "$(report_of "$OUT")" "config の内容が変わった(sha256:"
  hasnt "止めの印を消すと: 設定値を報告しない" "$(report_of "$OUT")" "selftest.tampered=yes"
  has "止めの印を消すと: 次のタスクを回す" "$REC/calls.log" "pr-c"
else
  ng "TERM(config を変えた周): 周が始まらない"; kill -KILL "$BG_PID" 2>/dev/null; wait_bg
fi

fi

stop_fixture_children() {
  local p pg selfpg
  selfpg="$(ps -o pgid= -p $$ | tr -d ' ')"
  for p in $(cat "$REC"/pid-* "$REC"/spawned-* 2>/dev/null); do
    [ -r "/proc/$p/stat" ] || continue
    pg="$(sed -E 's/.*\) //' "/proc/$p/stat" | cut -d' ' -f3)"
    if [ -n "$pg" ] && [ "$pg" != "$selfpg" ] && [ "$pg" -gt 1 ]; then kill -KILL -- "-$pg" 2>/dev/null || true; fi
    kill -KILL "$p" 2>/dev/null || true
  done
}
# ════════════════ 周の途中で落ちた(loop.sh だけを KILL)════════════════
if want kill; then
kill_case() { # 元の中断条件を保ち、全て未信頼停止を求める。
  local behavior="$1" state="k-$1" held before
  newrepo "$state"
  addtask "$behavior-a" 2026-01-01; addtask pr-b 2026-01-02
  commit; newrec "$state"
  start_bg "$state" -- --repo "$R" "${COMMON_ARGS[@]}"
  if ! wait_file "$REC/started-$behavior-a" 30; then
    ng "KILL($behavior): 周が始まらない"; kill -KILL "$BG_PID" 2>/dev/null; wait_bg; return
  fi
  kill -KILL "$BG_PID"; wait_bg
  CHILD="$(cat "$REC/pid-$behavior-a")"
  if wait_dead "$CHILD" 7; then ok "KILL($behavior): supervisor が親の死後も子を回収する"; else ng "KILL($behavior): supervisor が親の死後も子を回収する"; fi
  for p in $(cat "$REC/spawned-$behavior-a" 2>/dev/null); do
    if wait_dead "$p" 3; then ok "KILL($behavior): 別セッションの子孫も回収する"; else ng "KILL($behavior): 別セッションの子孫も回収する(pid $p)"; fi
  done
  SD="$(state_dir "$state")"
  held="$(sha256sum "$SD/inflight/meta" "$SD/inflight/base.json")"
  before="$(G -C "$R" show-ref; G -C "$R" worktree list --porcelain)"
  : >"$REC/ssh.log"
  run_loop "$state" -- --repo "$R" "${COMMON_ARGS[@]}"
  check "KILL($behavior): 未信頼中断で20" 20 "$RC"
  has "KILL($behavior): 理由" "$OUT" '[inflight-untrusted]'
  check "KILL($behavior): 控えを変更しない" "$held" "$(sha256sum "$SD/inflight/meta" "$SD/inflight/base.json")"
  check "KILL($behavior): ref/worktree/lockを変更しない" "$before" "$(G -C "$R" show-ref; G -C "$R" worktree list --porcelain)"
  check "KILL($behavior): 次へ進まない" "$behavior-a" "$(calls)"
  f "KILL($behavior): network Gitなし" grep -q git-upload-pack "$REC/ssh.log"
  t "KILL($behavior): worktree の lock を残す" locked_reason_of "docs/tasks/進行中_$behavior-a.md"
  rm -f "$SD/stop-mark.md"
  run_loop "$state" -- --repo "$R" "${COMMON_ARGS[@]}"
  check "KILL($behavior): stop-mark だけ消しても再開しない" 20 "$RC"
  stop_fixture_children
}
for behavior in cfgsleep defsleep longsleep pushsleep pushusleep; do kill_case "$behavior"; done
# 同じ印を持つ無関係なプロセスと偽 checksum は所有根拠にならない。
newrepo forged192; addtask pr-a; commit; newrec forged192
run_loop forged192 -- --repo "$R" --dry-run
SD="$(state_dir forged192)"; mkdir "$SD/inflight"
printf 'iter=forged192\nname=pr-a\n' >"$SD/inflight/meta"
printf '{}\n' >"$SD/inflight/base.json"
sha256sum "$SD/inflight/meta" >"$SD/inflight/checksum"
env SELFTEST_TAG="$TAG" DEV_WORKFLOW_LOOP_ITER=forged192 setsid sleep 60 &
MANUAL=$!; sleep 0.1
before="$(G -C "$R" show-ref; G -C "$R" worktree list --porcelain)"
held="$(sha256sum "$SD/inflight/meta" "$SD/inflight/base.json" "$SD/inflight/checksum")"
run_loop forged192 -- --repo "$R" "${COMMON_ARGS[@]}"
check '偽 inflight/checksum:20' 20 "$RC"
t '偽 inflight/checksum: 無関係な同UIDプロセス生存' proc_alive "$MANUAL"
check '偽 inflight/checksum: ref/worktree不変' "$before" "$(G -C "$R" show-ref; G -C "$R" worktree list --porcelain)"
check '偽 inflight/checksum: 記録不変' "$held" "$(sha256sum "$SD/inflight/meta" "$SD/inflight/base.json" "$SD/inflight/checksum")"
kill -KILL -- "-$MANUAL" 2>/dev/null || true; wait "$MANUAL" 2>/dev/null || true

# supervisor だけが強制終了した場合も loop は成功判定と次の周を止める。
newrepo ksuperonly
addtask longsleep-a 2026-01-01; addtask pr-b 2026-01-02
commit
newrec ksuperonly
start_bg ksuperonly -- --repo "$R" "${COMMON_ARGS[@]}"
if wait_file "$REC/started-longsleep-a" 30; then
  CHILD="$(cat "$REC/pid-longsleep-a")"
  SUP_PID="$(python3 - "$CHILD" <<'PID'
import sys
raw=open('/proc/'+sys.argv[1]+'/stat','rb').read()
print(int(raw[raw.rindex(b')')+2:].split()[1]))
PID
)"
  kill -KILL "$SUP_PID"; wait_bg
  check "supervisor だけ KILL: loop は 10 で停止" 10 "$BG_RC"
  SD="$(state_dir ksuperonly)"
  has "supervisor だけ KILL: 不在証明が無い" "$SD/stop-mark.md" "不在証明"
  t "supervisor だけ KILL: inflight を残す" test -d "$SD/inflight"
  hasnt "supervisor だけ KILL: 次の周へ進まない" "$REC/calls.log" "pr-b"
  kill -KILL -- "-$CHILD" 2>/dev/null || true
  for p in $(cat "$REC/spawned-longsleep-a" 2>/dev/null); do kill -KILL -- "-$p" 2>/dev/null || kill -KILL "$p" 2>/dev/null || true; done
else
  ng "supervisor だけ KILL: 周が始まらない"; kill -KILL "$BG_PID" 2>/dev/null; wait_bg
fi

# supervisor も loop も強制終了したとき、次の起動は不在証明を補完しない。
newrepo kbothers
addtask longsleep-a 2026-01-01; addtask pr-b 2026-01-02
commit
newrec kbothers
start_bg kbothers -- --repo "$R" "${COMMON_ARGS[@]}"
if wait_file "$REC/started-longsleep-a" 30; then
  CHILD="$(cat "$REC/pid-longsleep-a")"
  SUP_PID="$(python3 - "$CHILD" <<'PID'
import sys
raw=open('/proc/'+sys.argv[1]+'/stat','rb').read()
print(int(raw[raw.rindex(b')')+2:].split()[1]))
PID
)"
  kill -KILL "$SUP_PID" "$BG_PID"; wait_bg
  SD="$(state_dir kbothers)"
  run_loop kbothers -- --repo "$R" "${COMMON_ARGS[@]}"
  check "監督側も KILL: 次の起動は停止" 20 "$RC"
  t "監督側も KILL: inflight を残す" test -d "$SD/inflight"
  hasnt "監督側も KILL: 次の周を起動しない" "$REC/calls.log" "pr-b"
  # supervisor は既に死んだ。テストが起動を記録したこの子の group だけを回収する。
  kill -KILL -- "-$CHILD" 2>/dev/null || true
  for p in $(cat "$REC/spawned-longsleep-a" 2>/dev/null); do kill -KILL "$p" 2>/dev/null || true; done
else
  ng "監督側も KILL: 周が始まらない"; kill -KILL "$BG_PID" 2>/dev/null; wait_bg
fi

# 偽の meta の印と一致する、今回の supervisor が所有していない同 UID を止めない。
ITER="$(cat "$REC/iter-longsleep-a")"
env SELFTEST_TAG="$TAG" DEV_WORKFLOW_LOOP_ITER="$ITER" setsid bash -c 'trap "" TERM; while :; do sleep 1; done' </dev/null >/dev/null 2>&1 &
MANUAL=$!
sleep .3
BEFORE_META="$(sha256sum <"$SD/inflight/meta")"
run_loop kbothers -- --repo "$R" "${COMMON_ARGS[@]}"
check "未信頼 meta: 20 で停止" 20 "$RC"
t "未信頼 meta: 無関係な同 UID のプロセスを止めない" proc_alive "$MANUAL"
check "未信頼 meta: 記録は不変" "$BEFORE_META" "$(sha256sum <"$SD/inflight/meta")"
kill -KILL -- "-$MANUAL" 2>/dev/null || true
wait "$MANUAL" 2>/dev/null || true

fi

# ════════════════ シグナル・内部の失敗と残り(試験用のフック)════════════════
if want hooks; then
hook_signal_case() { # $1=シグナル $2=期待する終了コード
  newrepo "hs-$1"
  addtask cfgsleep-a 2026-01-01; addtask pr-b 2026-01-02
  commit
  newrec "hs-$1"
  start_bg "hs-$1" DEV_WORKFLOW_LOOP_TEST_LEFTOVER=1 -- --repo "$R" "${COMMON_ARGS[@]}"
  if wait_file "$REC/started-cfgsleep-a" 30; then
    kill "-$1" "$BG_PID"; wait_bg
    check "フック + $1: 終了コード" "$2" "$BG_RC"
    SD="$(state_dir "hs-$1")"
    has "フック + $1: 止めの印の理由は「残ったプロセス」" "$SD/stop-mark.md" "残ったプロセス"
    hasnt "フック + $1: 照合をしない(差分を載せない)" "$SD/stop-mark.md" "selftest.tampered"
    t "フック + $1: 周の途中の印が残る" test -d "$SD/inflight"
  else
    ng "フック + $1: 周が始まらない"; kill -KILL "$BG_PID" 2>/dev/null; wait_bg
  fi
}
hook_signal_case TERM 143
hook_signal_case HUP 129
newrepo hookint
addtask crash-a 2026-01-01; addtask pr-b 2026-01-02
commit
newrec hookint
run_loop hookint "PATH=$STUBBIN:$W/failsleepbin:$SAFEBIN" "SELFTEST_SLEEP_TRIGGER=$REC/trigger" DEV_WORKFLOW_LOOP_TEST_LEFTOVER=1 \
  -- --repo "$R" "${COMMON_ARGS[@]}"
check "フック + 内部の失敗: 10" 10 "$RC"
SD="$(state_dir hookint)"
has "フック + 内部の失敗: 止めの印の理由は「残ったプロセス」" "$SD/stop-mark.md" "残ったプロセス"
t "フック + 内部の失敗: 周の途中の印が残る" test -d "$SD/inflight"

# ループの最中の片付けの失敗(試験用のフック)→ 判定と次の周へ進まずに 10 →
# 両方の印がある状態で起動すると、子と中断記録を保全して 20。人の確認より先に子へ信号を送らない。
newrepo midleft
addtask pr-a 2026-01-01; addtask pr-b 2026-01-02
commit
newrec midleft
run_loop midleft DEV_WORKFLOW_LOOP_TEST_LEFTOVER=1 -- --repo "$R" "${COMMON_ARGS[@]}"
check "ループの最中の片付けの失敗: 10" 10 "$RC"
has "ループの最中の片付けの失敗: 理由" "$OUT" "[leftover]"
check "ループの最中の片付けの失敗: 次の候補の -p を起動しない" "pr-a" "$(calls)"
SD="$(state_dir midleft)"
t "ループの最中の片付けの失敗: 止めの印が残る" test -f "$SD/stop-mark.md"
ITER="$(cat "$REC/iter-pr-a")"
has "ループの最中の片付けの失敗: 周の途中の印(その周の識別子)が残る" "$SD/inflight/meta" "iter=$ITER"
t "ループの最中の片付けの失敗: worktree が残る" locked_reason_of "docs/tasks/進行中_pr-a.md"
hasnt "ループの最中の片付けの失敗: 判定しない" "$OUT" "→ 正常"
env SELFTEST_TAG="$TAG" DEV_WORKFLOW_LOOP_ITER="$ITER" setsid bash -c 'trap "" TERM; while :; do sleep 1; done' </dev/null >/dev/null 2>&1 &
MANUAL=$!
sleep 0.3
cp "$SD/inflight/meta" "$REC/held-meta"
cp "$SD/inflight/base.json" "$REC/held-base.json"
cp "$SD/stop-mark.md" "$REC/held-stop-mark.md"
run_loop midleft -- --repo "$R" "${COMMON_ARGS[@]}"
check "両方の印がある起動: 20" 20 "$RC"
t "両方の印がある起動: 確認前の子を保全する" proc_alive "$MANUAL"
t "両方の印がある起動: 中断記録を保全する" cmp -s "$REC/held-meta" "$SD/inflight/meta"
t "両方の印がある起動: 比較基準を保全する" cmp -s "$REC/held-base.json" "$SD/inflight/base.json"
t "両方の印がある起動: 停止印を保全する" cmp -s "$REC/held-stop-mark.md" "$SD/stop-mark.md"
# fixture が起動した既知の PID/プロセスグループだけを人の確認後の片付けとして止める。
kill -KILL -- "-$MANUAL" 2>/dev/null || true
wait "$MANUAL" 2>/dev/null || true

# 内部の失敗(周の途中で loop.sh の sleep を失敗させる)→ 子が片付き、30(照合で差分があれば 10)
newrepo internal
addtask crash-a 2026-01-01; addtask pr-b 2026-01-02
commit
newrec internal
run_loop internal "PATH=$STUBBIN:$W/failsleepbin:$SAFEBIN" "SELFTEST_SLEEP_TRIGGER=$REC/trigger" -- --repo "$R" "${COMMON_ARGS[@]}"
check "内部の失敗: 30" 30 "$RC"
has "内部の失敗: 理由" "$OUT" "[internal]"
for p in $(cat "$REC/pid-crash-a" "$REC/spawned-crash-a" 2>/dev/null); do
  if wait_dead "$p" 3; then ok "内部の失敗: 子が片付く(pid $p)"; else ng "内部の失敗: 子が片付く(pid $p)"; fi
done
SD="$(state_dir internal)"
f "内部の失敗: 差分が無ければ止めの印を置かない" test -e "$SD/stop-mark.md"
f "内部の失敗: 周の途中の印を消す" test -e "$SD/inflight"
has "内部の失敗: 報告に書く" "$(report_of "$OUT")" "内部の失敗"
newrepo internal2
addtask crashcfg-a 2026-01-01; addtask pr-b 2026-01-02
commit
newrec internal2
run_loop internal2 "PATH=$STUBBIN:$W/failsleepbin:$SAFEBIN" "SELFTEST_SLEEP_TRIGGER=$REC/trigger" -- --repo "$R" "${COMMON_ARGS[@]}"
check "内部の失敗(照合で差分): 10" 10 "$RC"
SD="$(state_dir internal2)"
has "内部の失敗(照合で差分): 止めの印に 構造差分" "$SD/stop-mark.md" "保持済み config または worktree の構造が周の途中で変わった"
hasnt "内部の失敗(照合で差分): 設定値を印に出さない" "$SD/stop-mark.md" "selftest.tampered=yes"

fi

# ════════════════ 許可の仲介(D22 ③ の hook・② の reviews-dir.sh)════════════════
if want perm; then
# 周の worktree に見立てたディレクトリとプラグインルート(hook は入力の JSON と環境変数だけで判定する)
PW="$W/permwt"
PP="$W/permplug"
rm -rf "$PW" "$PP" "$W/permout"
mkdir -p "$PW/.claude/reviews/d" "$PW/src" "$PW/.git" "$PW/.claude/tasks" "$PP/skills/x" "$W/permout"
echo a >"$PW/.claude/reviews/a"
echo pr >"$PW/.claude/reviews/pr.md"
echo msg >"$PW/.claude/reviews/msg.txt"
echo s >"$PW/.claude/reviews/settings.json"
echo g >"$PW/.claude/grasp.md"
echo r >"$PP/skills/x/ref.md"
echo x >"$PW/src/x"
ln -s "$W/permout" "$PW/.claude/reviews/escape"
ln -s "$PW/.claude/reviews/a" "$PW/.claude/reviews/link"
PERMLOG="$W/perm.log"
: >"$PERMLOG"
PERM_ALLOW_JSON='[{"kind":"prefix","words":["git"]},{"kind":"prefix","words":["sed"]},{"kind":"prefix","words":["bash"]},{"kind":"prefix","words":["gh"]},{"kind":"prefix","words":["cat"]},{"kind":"exact","words":["npm","run","build"]},{"kind":"exact","words":["make","test"]}]'
PERM_BIN="$PP/loop-permission.py"
cp "$PERM_SRC" "$PERM_BIN"
perm() { # $1=tool_name $2=tool_input の JSON [$3=cwd] → PDEC(allow|deny)・PKIND・POUT(hook の出力)
  local out before
  before="$(wc -l <"$PERMLOG")"
  out="$(python3 -c 'import json,sys; print(json.dumps({"hook_event_name":"PermissionRequest","tool_name":sys.argv[1],"tool_input":json.loads(sys.argv[2]),"cwd":sys.argv[3]}))' "$1" "$2" "${3:-$PW}" \
    | env -i HOME="${PHOME:-$W/home}" PATH="$SAFEBIN" DEV_WORKFLOW_LOOP_WORKTREE="${PWT:-$PW}" DEV_WORKFLOW_LOOP_PERMLOG="$PERMLOG" \
        DEV_WORKFLOW_LOOP_PLUGIN_ROOT="${PPR:-$PP}" \
        DEV_WORKFLOW_LOOP_ALLOW="${PALLOW:-$PERM_ALLOW_JSON}" python3 "$PERM_BIN")"
  PRC=$?
  PLINES=$(( $(wc -l <"$PERMLOG") - before ))
  PDEC="$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin)["hookSpecificOutput"]; assert d["hookEventName"]=="PermissionRequest"; print(d["decision"]["behavior"])' 2>/dev/null || echo broken)"
  PKIND="$(tail -1 "$PERMLOG" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("kind"))' 2>/dev/null)"
  POUT="$out"
}
decision_field() { # $1=キー → 直前の hook の出力の decision のそのキー(無ければ「(無い)」)
  printf '%s' "$POUT" | python3 -c 'import json,sys; d=json.load(sys.stdin)["hookSpecificOutput"]["decision"]; print(d.get(sys.argv[1], "(無い)"))' "$1" 2>/dev/null
}
pa() { perm "$2" "$3" "${4:-}"; check "hook allow: $1" allow "$PDEC"; }
pd() { perm "$2" "$3" "${4:-}"; check "hook deny: $1" deny "$PDEC"; }
pdk() { perm "$3" "$4" "${5:-}"; check "hook deny: $1" deny "$PDEC"; check "hook deny の種類: $1" "$2" "$PKIND"; }
bash_in() { python3 -c 'import json,sys; print(json.dumps({"command": sys.argv[1]}))' "$1"; }
bash_in_extra() { # $1=command $2=追加する JSON object
  python3 -c 'import json,sys; d={"command": sys.argv[1]}; d.update(json.loads(sys.argv[2])); print(json.dumps(d))' "$1" "$2"
}
h25_log_field() { tail -1 "$PERMLOG" | python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
file_in() { python3 -c 'import json,sys; print(json.dumps({"file_path": sys.argv[1]}))' "$1"; }
# allow
pa "W の中への Write(.claude/reviews/x.md)" Write "$(file_in "$PW/.claude/reviews/x.md")"
pa "W の中への Edit(.claude/grasp.md)" Edit "$(file_in "$PW/.claude/grasp.md")"
pa "export GIT_NO_LAZY_FETCH=1 && git … > .claude/reviews/…" Bash "$(bash_in 'export GIT_NO_LAZY_FETCH=1 && git status --short > .claude/reviews/st.txt')"
pa "利用者の設定だけで許したコマンド(make test)の W へのリダイレクト" Bash "$(bash_in 'make test > .claude/reviews/t.txt')"
pa "入力の cwd が worktree の下のディレクトリのときの W への書き込み" Bash "$(bash_in 'git status --short > ../.claude/reviews/sub.txt')" "$PW/src"
pa "CDPATH= cd -P -- <worktree の中> && pwd -P" Bash "$(bash_in 'CDPATH= cd -P -- src && pwd -P')"
pa "CDPATH= cd -P -- <プラグインルートの中> && pwd -P" Bash "$(bash_in "CDPATH= cd -P -- $PP/skills && pwd -P")"
pa "プラグインルートの下の Read" Read "$(file_in "$PP/skills/x/ref.md")"
# H25: Bash の sandbox 無効化指定は、コマンドの許可判定より先に厳密な boolean として検査する。
perm Bash "$(bash_in_extra 'git status' '{"description":"status","timeout":1000}')"
check "H25 未指定: allow" allow "$PDEC"
check "H25 未指定: kind は null" None "$PKIND"
check "H25 未指定: reason は空" "" "$(h25_log_field reason)"
check "H25 未指定: message は無い" "(無い)" "$(decision_field message)"
perm Bash "$(bash_in_extra 'git status' '{"dangerouslyDisableSandbox":false,"description":"status","timeout":1000}')"
check "H25 false(description/timeout を維持): allow" allow "$PDEC"
check "H25 false: kind は null" None "$PKIND"
check "H25 false: reason は空" "" "$(h25_log_field reason)"
check "H25 false: message は無い" "(無い)" "$(decision_field message)"
for H25_CASE in \
  'prefix|git status > .claude/reviews/x' \
  'exact|npm run build' \
  'file-op|touch .claude/reviews/h25' \
  'cd|CDPATH= cd -P -- src && pwd -P'; do
  H25_LABEL="${H25_CASE%%|*}"
  H25_COMMAND="${H25_CASE#*|}"
  perm Bash "$(bash_in_extra "$H25_COMMAND" '{"dangerouslyDisableSandbox":true,"description":"h25","timeout":1000}')"
  check "H25 true($H25_LABEL): deny" deny "$PDEC"
  check "H25 true($H25_LABEL): 種類" other "$PKIND"
  check "H25 true($H25_LABEL): 終了コード" 0 "$PRC"
  check "H25 true($H25_LABEL): 1 呼び出し 1 行" 1 "$PLINES"
  check "H25 true($H25_LABEL): tool_name" Bash "$(h25_log_field tool_name)"
  check "H25 true($H25_LABEL): cwd" "$PW" "$(h25_log_field cwd)"
  check "H25 true($H25_LABEL): subject" "$H25_COMMAND" "$(h25_log_field subject)"
  check "H25 true($H25_LABEL): reason" "dangerouslyDisableSandbox が true" "$(h25_log_field reason)"
  case "$(decision_field message)" in *"dangerouslyDisableSandbox"*"回り込"*"G1"*) ok "H25 true($H25_LABEL): 理由と固定の文" ;;
    *) ng "H25 true($H25_LABEL): 理由と固定の文" ;; esac
  case "$(h25_log_field reason)" in *"回り込"*|*"G1"*) ng "H25 true($H25_LABEL): ログの reason に固定の文を入れない" ;;
    *) ok "H25 true($H25_LABEL): ログの reason に固定の文を入れない" ;; esac
done
PALLOW='[{"kind":"all","words":[]}]'
perm Bash "$(bash_in_extra 'unknown-command' '{"dangerouslyDisableSandbox":true}')"
check "H25 true(all): deny" deny "$PDEC"
check "H25 true(all): 種類" other "$PKIND"
check "H25 true(all): 終了コード" 0 "$PRC"
check "H25 true(all): 1 呼び出し 1 行" 1 "$PLINES"
check "H25 true(all): tool_name" Bash "$(h25_log_field tool_name)"
check "H25 true(all): cwd" "$PW" "$(h25_log_field cwd)"
check "H25 true(all): subject" unknown-command "$(h25_log_field subject)"
check "H25 true(all): reason" "dangerouslyDisableSandbox が true" "$(h25_log_field reason)"
PALLOW=""
for H25_VALUE in 'null' '0' '1' '0.0' '""' '"false"' '"true"' '[]' '{}'; do
  perm Bash "$(bash_in_extra 'git status' "{\"dangerouslyDisableSandbox\":$H25_VALUE}")"
  check "H25 非 boolean($H25_VALUE): deny" deny "$PDEC"
  check "H25 非 boolean($H25_VALUE): 種類" other "$PKIND"
  check "H25 非 boolean($H25_VALUE): 終了コード" 0 "$PRC"
  check "H25 非 boolean($H25_VALUE): 1 呼び出し 1 行" 1 "$PLINES"
  check "H25 非 boolean($H25_VALUE): 型不正の理由" "dangerouslyDisableSandbox が boolean でない" "$(h25_log_field reason)"
done
perm Bash "$(bash_in_extra 'curl --version' '{"dangerouslyDisableSandbox":false}')"
check "H25 false: 既存の other は deny" deny "$PDEC"
check "H25 false: 既存の other を維持" other "$PKIND"
perm Bash "$(bash_in_extra 'git status > .git/h25' '{"dangerouslyDisableSandbox":false}')"
check "H25 false: 既存の protected は deny" deny "$PDEC"
check "H25 false: 既存の protected を維持" protected "$PKIND"
# H38: 未引用の ~ は位置と構文を問わず入口で拒否する。引用・エスケープした ~ は通常のパス検査へ進む。
mkdir -p -- "$PW/h38/~" "$PW/x=~" "$PW/x=a:~" "$W/permout/h38"
printf 'changed\n' >"$PW/f.txt"
PALLOW='[{"kind":"all","words":[]}]'
for H38_CASE in \
  '引数の = 後|cp f.txt x=~/z' \
  '引数の : 後|cp f.txt x=a:~/z' \
  '先頭|cat ~/z' \
  '語中|cat h38/a~b' \
  '末尾|cat h38/z~' \
  'スラッシュ後|cat h38/~/z' \
  '先頭の単引用に隣接|cat '\''h38/'\''~/z' \
  '先頭の二重引用に隣接|cat "h38/"~/z' \
  '後ろの単引用に隣接|cat h38/~'\''/z'\''' \
  '後ろの二重引用に隣接|cat h38/~"/z"' \
  '空単引用の後|cat '\'''\''~/z' \
  '空二重引用の後|cat ""~/z' \
  '空単引用の前|cat h38/z~'\'''\''' \
  '空二重引用の前|cat h38/z~""' \
  'コマンド語の先頭|~/tool' \
  'コマンド語の語中|tool~name' \
  'コマンド語の末尾|tool~' \
  '先頭代入|LANG=~/z git status' \
  '先頭代入の : 後|LANG=a:~/z git status' \
  '先頭代入の語中|LANG=a~b git status' \
  '代入だけ|LANG=~/z' \
  '代入だけの末尾|LANG=z~' \
  'export|export LANG=~/z' \
  'export の : 後|export LANG=a:~/z' \
  '入力リダイレクト|cat < h38/~/z' \
  '入力リダイレクトの = 後|cat < x=~/z' \
  '出力リダイレクト|echo changed > h38/~/z' \
  '追記リダイレクト|echo changed >> x=a:~/z' \
  '両方の出力リダイレクト|echo changed &> h38/z~' \
  '複合 &&|git status && cp f.txt x=~/z' \
  '複合 OR|git status || cp f.txt x=a:~/z' \
  '複合 ;|git status ; cat h38/z~' \
  '複合パイプ|cat f.txt | cat h38/a~b' \
  '保護パス判定より先|echo ~ > .git/h38'; do
  pdk "H38 未引用: ${H38_CASE%%|*}" other Bash "$(bash_in "${H38_CASE#*|}")"
done
h38_run() { # 既存の scratch HOME を継承し、実 Bash を制限 PATH で実行する。
  timeout -k 2 5 bash -c 'cd "$1" && exec env PATH="$2" bash --noprofile --norc -c "$3"' \
    _ "$PW" "$SAFEBIN" "$1"
}
# = と : の後の ~ が展開されたパスの HOME 部分を scratch 内に模す。末尾だけ外部役へリンクする。
for H38_PREFIX in 'x=' 'x=a:'; do
  mkdir -p -- "$PW/$H38_PREFIX${HOME%/*}"
  ln -s -- "$W/permout/h38" "$PW/$H38_PREFIX$HOME"
  H38_COMMAND="cp f.txt $H38_PREFIX~/z"
  printf 'before\n' >"$W/permout/h38/z"
  h38_run "$H38_COMMAND" >/dev/null 2>&1; H38_RC=$?
  check "H38 陽性対照: $H38_COMMAND は実 Bash で完走する" 0 "$H38_RC"
  check "H38 陽性対照: $H38_COMMAND は外部役を書き換える" changed "$(cat "$W/permout/h38/z")"
  printf 'before\n' >"$W/permout/h38/z"
  pdk "H38 実行ゲート: $H38_COMMAND" other Bash "$(bash_in "$H38_COMMAND")"
  if [ "$PDEC" = allow ]; then h38_run "$H38_COMMAND" >/dev/null 2>&1; fi
  check "H38 拒否後: $H38_COMMAND の外部役が不変" before "$(cat "$W/permout/h38/z")"
  rm -f -- "$PW/$H38_PREFIX$HOME"
  rm -rf -- "$PW/$H38_PREFIX"  # 後続の cd 回帰が作る同名のパスを残さない。
done
for H38_LITERAL in "'h38/~/z'" '"h38/~/z"' 'h38/\~/z' "h38/'~'/z" 'h38/"~"/z' "h38/''\~/z" 'h38/""\~/z'; do
  printf 'before\n' >"$PW/h38/~/z"
  H38_COMMAND="cp f.txt $H38_LITERAL"
  pa "H38 リテラル: $H38_LITERAL" Bash "$(bash_in "$H38_COMMAND")"
  H38_RC=not-run
  if [ "$PDEC" = allow ]; then h38_run "$H38_COMMAND" >/dev/null 2>&1; H38_RC=$?; fi
  check "H38 リテラル: $H38_LITERAL は実 Bash で完走する" 0 "$H38_RC"
  check "H38 リテラル: $H38_LITERAL の字面の場所へ書く" changed "$(cat "$PW/h38/~/z")"
done
ln -s -- "$W/permout/h38" "$PW/h38/~/outside"
ln -s -- "$PW/.claude/settings.json" "$PW/h38/~/protected"
printf 'before\n' >"$PW/.claude/settings.json"
for H38_LITERAL in "'h38/~/outside/z'" '"h38/~/outside/z"' 'h38/\~/outside/z'; do
  pdk "H38 リテラル外部: 引数 $H38_LITERAL" other Bash "$(bash_in "cat $H38_LITERAL")"
  pdk "H38 リテラル外部: 書き込み $H38_LITERAL" other Bash "$(bash_in "cp f.txt $H38_LITERAL")"
  pdk "H38 リテラル外部: 入力 $H38_LITERAL" other Bash "$(bash_in "cat < $H38_LITERAL")"
  pdk "H38 リテラル外部: 出力 $H38_LITERAL" other Bash "$(bash_in "echo changed > $H38_LITERAL")"
done
for H38_LITERAL in "'h38/~/protected'" '"h38/~/protected"' 'h38/\~/protected'; do
  pdk "H38 リテラル保護: 引数 $H38_LITERAL" other Bash "$(bash_in "cat $H38_LITERAL")"
  pdk "H38 リテラル保護: 書き込み $H38_LITERAL" protected Bash "$(bash_in "cp f.txt $H38_LITERAL")"
  pdk "H38 リテラル保護: 出力 $H38_LITERAL" protected Bash "$(bash_in "echo changed > $H38_LITERAL")"
  H38_COMMAND="cat < $H38_LITERAL > 'h38/~/input'"
  pa "H38 リテラル保護: 入力は既存の範囲検査 $H38_LITERAL" Bash "$(bash_in "$H38_COMMAND")"
  H38_RC=not-run
  if [ "$PDEC" = allow ]; then h38_run "$H38_COMMAND" >/dev/null 2>&1; H38_RC=$?; fi
  check "H38 リテラル保護: scratch の入力を実 Bash で読める $H38_LITERAL" 0 "$H38_RC"
  check "H38 リテラル保護: scratch の入力内容 $H38_LITERAL" before "$(cat "$PW/h38/~/input")"
done
pa "H38 引用した先頭代入" Bash "$(bash_in "LANG='h38/~/z' git status")"
pa "H38 引用した代入だけ" Bash "$(bash_in 'LANG="h38/~/z"')"
pa "H38 エスケープした export" Bash "$(bash_in 'export LANG=h38/\~/z')"
pa "H38 引用したコマンド語は通常判定" Bash "$(bash_in "'tool~'")"
pa "H38 エスケープしたコマンド語は通常判定" Bash "$(bash_in 'tool\~')"
for H38_LITERAL in "'h38/~'" '"h38/~"' 'h38/\~' "'x=~/z'" '"x=a:~/z"'; do
  pdk "H38 cd の既存制約: $H38_LITERAL && pwd" other Bash "$(bash_in "CDPATH= cd -P -- $H38_LITERAL && pwd -P")"
  pdk "H38 cd の既存制約: $H38_LITERAL && git" other Bash "$(bash_in "CDPATH= cd -P -- $H38_LITERAL && git status")"
done
pa "H38 構造化 Write の ~ はリテラル" Write "$(file_in "$PW/.claude/reviews/h38~")"
pa "H38 構造化 Read の ~ はリテラル" Read "$(file_in "$PP/skills/x/h38~")"
PALLOW=""
# H37: 実 Bash は追跡しない組み込み移動の後に symlink の先を書き換えられる。hook は全許可でも入口で止める。
mkdir -p "$PW/sub"
printf 'before\n' >"$PW/.claude/settings.json"
ln -s ../.claude/settings.json "$PW/sub/local.txt"
h37_run() { # $1=同じ文字列で hook と実 Bash に渡す攻撃 $2=stdin(任意)
  local command="$1" input="${2:-}"
  if [ -n "$input" ]; then
    printf '%s' "$input" | timeout -k 2 5 bash -c 'cd "$1" && exec env -i HOME="$2" PATH="$3" bash --noprofile --norc -c "$4"' \
      _ "$PW" "$W/home" "$SAFEBIN" "$command"
  else
    timeout -k 2 5 bash -c 'cd "$1" && exec env -i HOME="$2" PATH="$3" bash --noprofile --norc -c "$4"' \
      _ "$PW" "$W/home" "$SAFEBIN" "$command"
  fi
}
h37_attack() { # $1=表示名 $2=攻撃 $3=stdin(任意)。陽性対照→復元→hook→allow時だけ実行→不変を同じ文字列で確認する。
  local label="$1" command="$2" input="${3:-}" rc
  printf 'before\n' >"$PW/.claude/settings.json"
  h37_run "$command" "$input" >/dev/null 2>&1; rc=$?
  check "H37 陽性対照: $label は実 Bash で完走する" 0 "$rc"
  check "H37 陽性対照: $label は symlink の先を書き換える" changed "$(cat "$PW/.claude/settings.json")"
  printf 'before\n' >"$PW/.claude/settings.json"
  pdk "H37: $label を全許可でも拒否する" other Bash "$(bash_in "$command")"
  if [ "$PDEC" = allow ]; then h37_run "$command" "$input" >/dev/null 2>&1; fi
  check "H37: $label の deny 後に symlink の先が不変" before "$(cat "$PW/.claude/settings.json")"
}
PALLOW='[{"kind":"all","words":[]}]'
h37_attack "builtin cd" 'builtin cd sub && echo changed > local.txt'
h37_attack "command cd" 'command cd sub && echo changed > local.txt'
h37_attack "pushd" 'pushd sub >/dev/null && echo changed > local.txt'
h37_attack "popd(pushd -n で準備)" 'pushd -n sub >/dev/null && popd >/dev/null && echo changed > local.txt'
h37_attack "深い command/builtin" 'command -p -- command -pp -- builtin -- cd sub && echo changed > local.txt'
h37_attack "予約語 ! time" '! time builtin cd sub; echo changed > local.txt'
h37_attack "許可 cd 前置きの後の builtin cd" 'CDPATH= cd -P -- src && builtin cd ../sub && echo changed > local.txt'
h37_attack "if" 'if builtin cd sub; then echo changed > local.txt; fi'
h37_attack "通常コマンド後の if" 'echo ok && if builtin cd sub; then echo changed > local.txt; fi'
h37_attack "while" 'while builtin cd sub; do echo changed > local.txt; break; done'
h37_attack "until" 'until builtin cd sub; do break; done; echo changed > local.txt'
h37_attack "for" 'for x in once; do builtin cd sub; echo changed > local.txt; done'
h37_attack "elif" 'if false; then :; elif builtin cd sub; then echo changed > local.txt; fi'
h37_attack "select" 'select x in once; do builtin cd sub; echo changed > local.txt; break; done' $'1\n'
h37_attack "許可 cd 前置き後の if" 'CDPATH= cd -P -- src && if builtin cd ../sub; then echo changed > local.txt; fi'
for H37_QUOTED in "t''ime true" "''time true" "!'' true" "\\time true" "'if' true"; do
  pa "H37: 引用・エスケープした予約語は通常判定($H37_QUOTED)" Bash "$(bash_in "$H37_QUOTED")"
done
pa "H37: builtin echo は通常操作として allow" Bash "$(bash_in 'builtin echo normal')"
pa "H37: command -p echo は通常操作として allow" Bash "$(bash_in 'command -p echo normal')"
check "H37: builtin echo は実 Bash で実行できる" normal "$(h37_run 'builtin echo normal')"
check "H37: command -p echo は実 Bash で実行できる" normal "$(h37_run 'command -p echo normal')"
pa "H37: 通常操作は全許可で allow" Bash "$(bash_in 'echo normal > src/h37.txt')"
if [ "$PDEC" = allow ]; then h37_run 'echo normal > src/h37.txt' >/dev/null 2>&1; fi
check "H37: allow の通常操作は実行できる" normal "$(cat "$PW/src/h37.txt")"
PALLOW=""
# 保護パスの判定は worktree のルートからの相対で見る: `.claude` の段を含む場所(導入先のキャッシュに似せた置き場)に
# プラグインルートを置いても、その中を読むのは保護パスに当たらない
PPC="$W/home/.claude/plugins/cache/m/dev-workflow/9.9.9"
mkdir -p "$PPC/skills/x"
echo s >"$PPC/skills/x/SKILL.md"
PPR="$PPC"
pa "プラグインルートが .claude の段の下にあっても、その中を読む(cat <プラグインルート>/skills/x/SKILL.md)" Bash "$(bash_in "cat $PPC/skills/x/SKILL.md")"
pa "プラグインルートが .claude の段の下にあっても、その中を読む(Read)" Read "$(file_in "$PPC/skills/x/SKILL.md")"
PPR=""
# パスとして読める単語は、単引用符の中でも・パターンでも、パスとして判定される: `.` で始まるパターンは保護パスの名を
# 指すので deny(other)、`[.]` で書けば allow(許可リストに grep があるとき)
PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a.append({"kind": "prefix", "words": ["grep"]}); print(json.dumps(a))')"
pdk "パターンでも . で始まる語はパスとして判定される(grep -F '.mcp.json' src/x)" other Bash "$(bash_in "grep -F '.mcp.json' src/x")"
pa "パターンの . を [.] で書く(grep '[.]mcp[.]json' src/x)" Bash "$(bash_in "grep '[.]mcp[.]json' src/x")"
PALLOW=""
# 状態ファイルを外す git status(do-task・ship-task の Phase 0 の 3): do-task/SKILL.md の `git --no-literal-pathspecs status …`
# の字面に、base-commit.md の前置きを git の直後に置いたコマンドを、cwd が worktree のルートでも src/ でも許す。
# 字面の `[.]claude` を `.claude` にすると、pathspec がパスとして保護パスの下を指すので拒否する(`[.]` で書く理由)。
# 字面は文書から取り出す(ハードコードしない。-B で __pycache__ を作らない)。取り出せなければ FAIL にする
SF_CMD="$(python3 -B -c '
import re, sys
def one(path, pattern):
    name = path.rsplit("/", 1)[-1]
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as exc:
        return None, "%s を読めない: %s" % (name, exc)
    hits = sorted(set(re.findall(pattern, text)))
    if len(hits) != 1:
        return None, "%s の字面がちょうど 1 種類でない(%d 種類)" % (name, len(hits))
    return hits[0], None
st, e1 = one(sys.argv[1], r"`(git --no-literal-pathspecs status [^`]+)`")
pre, e2 = one(sys.argv[2], r"`(git --no-pager --no-replace-objects [^`]*-c filter\.lfs\.required=false)`")
if e1 or e2:
    print("NG " + " / ".join(e for e in (e1, e2) if e))
else:
    print("OK " + pre + st[len("git"):])
' "$PLUGIN_SRC/skills/do-task/SKILL.md" "$PLUGIN_SRC/skills/do-task/references/base-commit.md" 2>&1)"
case "$SF_CMD" in
  "OK "*)
    SF_CMD="${SF_CMD#OK }"
    pa "状態ファイルを外す git status: do-task の字面に前置きを置いたコマンド(cwd が worktree のルート)" Bash "$(bash_in "$SF_CMD")"
    pa "状態ファイルを外す git status: do-task の字面に前置きを置いたコマンド(cwd が src/)" Bash "$(bash_in "$SF_CMD")" "$PW/src"
    pd "状態ファイルを外す git status: 字面の [.]claude を .claude にすると保護パスの下を指す" Bash \
      "$(bash_in "${SF_CMD//\[.\]claude/.claude}")"
    pd "状態ファイルを外す git status: 字面の [.]claude を .claude にすると保護パスの下を指す(cwd が src/)" Bash \
      "$(bash_in "${SF_CMD//\[.\]claude/.claude}")" "$PW/src"
    ;;
  *) ng "状態ファイルを外す git status: do-task/SKILL.md と base-commit.md から字面を取り出す(${SF_CMD#NG })" ;;
esac
# 無人の周の git・公開 helper の字面: ship-task の文書から取り出し、プレースホルダを置き換えて hook に掛ける。
# hook に掛ける字面は文書から取り出す(ハードコードしない。-B で __pycache__ を作らない)。文書ごとにちょうど 1 種類を求め、
# 取り出せなければ FAIL にする。python3 の呼び出しは、許可リストに python3 を足して掛ける(推奨の列は python3 を含む — loop.md §4)。
# 公開は direct push ではなく helper に集約する。無人の helper は --set-upstream を受けず、保持 SHA・固定 repo/base・
# 設定 digest・reviews 内の本文を必ず渡す。helper の実行と gh の作成後 REST 照合は上の docpush 対照で行う。
printf 'review fixture\n' >"$PW/.claude/reviews/m.md"
printf 'discover commit fixture\n' >"$PW/.claude/reviews/discover-data-audit-msg.md"
# 文書から取り出す helper は loop が渡す plugin root に置く。任意の外部 Python ではないことも同時に検査する。
PPR="$PLUG"
UW_CMDS="$(python3 -B - "$PLUGIN_SRC/skills/ship-task" "$PLUG/skills/ship-task/" "$PW" <<'PY' 2>&1
import re, sys
root, st_dir, wt = sys.argv[1], sys.argv[2], sys.argv[3]
digest = r"python3 \{ship-task の\}scripts/git-config-digest\.py [^`]*?&& "
publish = r"python3 \{ship-task の\}scripts/publish-guard\.py [^`]+"
items = [
    ("無人の公開 helper", "SKILL.md", publish),
    ("無人のブランチ作成", "SKILL.md", r"git switch --no-track -c [^`]+"),
    ("無人のブランチ作成", "references/discover-mode.md", r"git switch --no-track -c [^`]+"),
    ("照合つきの commit", "references/unattended-mode.md", digest + r"git commit [^`]+"),
    ("照合つきの commit", "references/discover-mode.md", digest + r"git commit [^`]+"),
    ("PR の作成先の確かめ", "SKILL.md", r"gh repo view [^`]+"),
]
subs = [("{ship-task の}", st_dir), ("<管理ルート>", wt), ("<守る値>", "sha256:" + "0123456789abcdef" * 4),
        ("<保持したレビュー済み SHA>", "0123456789abcdef" * 4),
        ("<repo>", "github.com/o/r"), ("<デフォルトブランチ>", "main"), ("<タスク名>", "日本語のタスク"),
        ("<名>", "m.md"), ("<発見元>", "data-audit")]
discover = open(root + "/references/discover-mode.md", encoding="utf-8").read()
discover_required = ("unattended-mode.md §7 の「push の直前」と同じ", "publish-guard.py",
                     "保持したレビュー済み完全 SHA", "push_url_sha256", "--push-only", "`-u` は使わず")
if all(value in discover for value in discover_required):
    print("YES\t発見公開(discover-mode.md)が共通 publish-guard 手順を参照し固定 SHA/URL digest・push-only・no-u を維持\t-")
else:
    print("NG\t発見公開(discover-mode.md)が共通 publish-guard 手順を参照し固定 SHA/URL digest・push-only・no-u を維持\t必要な契約語が無い")
for label, rel, pat in items:
    name = rel.rsplit("/", 1)[-1]
    try:
        text = open(root + "/" + rel, encoding="utf-8").read()
    except OSError as exc:
        print("NG\t%s(%s)を文書から取り出す\t%s を読めない: %s" % (label, name, name, exc))
        continue
    hits = sorted(set(re.findall("`(" + pat + ")`", text)))
    if len(hits) != 1:
        print("NG\t%s(%s)を文書から取り出す\t字面がちょうど 1 種類でない(%d 種類)" % (label, name, len(hits)))
        continue
    tmpl = hits[0]
    if label == "無人の公開 helper":
        required = ("--sha=<保持したレビュー済み SHA>", "--branch=<作業ブランチ>", "--repo=<repo>",
                    "--base=<デフォルトブランチ>", "--config-digest=<守る値>", "--body-file=.claude/reviews/<名>")
        tag = "無人の公開 helper(%s)が保持 SHA・固定 repo/base・digest・reviews 本文を渡し -u を付けない" % name
        if all(value in tmpl for value in required) and "--set-upstream" not in tmpl:
            print("YES\t%s\t-" % tag)
        else:
            print("NG\t%s\t字面が違う: %s" % (tag, tmpl))
    for k, v in subs:
        tmpl = tmpl.replace(k, v)
    for b in (["task/x", "task/日本語"] if "<作業ブランチ>" in tmpl else [None]):
        cmd = tmpl if b is None else tmpl.replace("<作業ブランチ>", b)
        tag = "%s(%s%s)" % (label, name, "" if b is None else "・" + b)
        left = re.findall(r"<[^<>\s]+>|\{[^{}]*\}", cmd)
        if left:
            print("NG\t%sを置き換える\t置き換えられないプレースホルダ: %s" % (tag, " ".join(left)))
        elif "\t" in cmd or "\n" in cmd:
            print("NG\t%sを置き換える\t字面にタブか改行がある" % tag)
        else:
            print("OK\t%s\t%s" % (tag, cmd))
PY
)"
UW_PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a.append({"kind": "prefix", "words": ["python3"]}); print(json.dumps(a))')"
UW_CTRL=""
while IFS=$'\t' read -r st tag body; do
  case "$st" in
    OK)
      PALLOW="$UW_PALLOW"
      pa "無人の周の字面: $tag" Bash "$(bash_in "$body")"
      PALLOW=""
      case "$body" in python3\ *) [ -n "$UW_CTRL" ] || UW_CTRL="$body" ;; esac
      ;;
    YES) ok "無人の周の字面: $tag" ;;
    NG) ng "無人の周の字面: $tag($body)" ;;
    *) ng "無人の周の字面: 文書から取り出す(読めない出力: $st $tag $body)" ;;
  esac
done <<<"$UW_CMDS"
# 掛け方の対照: 許可リストに python3 が無ければ、照合つきの行は拒否される(allow が許可リストの python3 による)
if [ -n "$UW_CTRL" ]; then
  pd "無人の周の字面: 許可リストに python3 が無ければ照合つきの行を拒否する(掛け方の対照)" Bash "$(bash_in "$UW_CTRL")"
  case "$(decision_field message)" in
    *"許可リストに無いコマンド: python3 "*) ok "無人の周の字面: 掛け方の対照の拒否の理由は python3 の許可リスト" ;;
    *) ng "無人の周の字面: 掛け方の対照の拒否の理由は python3 の許可リスト($(decision_field message | head -c 200))" ;;
  esac
else
  ng "無人の周の字面: 掛け方の対照(python3 で始まる字面を取り出せない)"
fi
PPR=""
pa "Bash の mkdir .claude/reviews/sub" Bash "$(bash_in 'mkdir .claude/reviews/sub')"
pa "Bash の git status --short > .claude/reviews/st.txt(git が許可リストにある)" Bash "$(bash_in 'git status --short > .claude/reviews/st.txt')"
pa "Bash の rm -f .claude/grasp.md" Bash "$(bash_in 'rm -f .claude/grasp.md')"
pa "Bash の mv .claude/reviews/a .claude/reviews/b" Bash "$(bash_in 'mv .claude/reviews/a .claude/reviews/b')"
pa "Bash の CDPATH= git …" Bash "$(bash_in 'CDPATH= git status --short')"
pa "Bash の 2>/dev/null・2>&1・引用符の中のグロブ" Bash "$(bash_in "git log --format='%H *' 2>/dev/null && git status 2>&1")"
pa "Bash の rm -r .claude/reviews/d(.claude/reviews/ の下のディレクトリ)" Bash "$(bash_in 'rm -r .claude/reviews/d')"
# deny(種類つき)
pdk ".claude/settings.json への Write" protected Write "$(file_in "$PW/.claude/settings.json")"
pdk ".git/config への Write" protected Write "$(file_in "$PW/.git/config")"
pdk ".mcp.json への Write" protected Write "$(file_in "$PW/.mcp.json")"
pdk "worktree の外への Write" other Write "$(file_in "$W/permout/x.md")"
pdk ".. で外へ出るパス" other Write "$(file_in "$PW/.claude/reviews/../../../permout/x.md")"
pdk "symlink で外へ出るパス" other Write "$(file_in "$PW/.claude/reviews/escape/x.md")"
pd "対象そのものが symlink" Write "$(file_in "$PW/.claude/reviews/link")"
pdk "許可リストに無いコマンド(curl …)" other Bash "$(bash_in 'curl --version')"
pd "\$(" Bash "$(bash_in 'git log $(id)')"
pd "単引用符の外の \$(変数の展開)" Bash "$(bash_in 'git log $HOME')"
pd "バッククォート" Bash "$(bash_in 'git log `id`')"
pd "<<" Bash "$(bash_in 'git status <<EOF')"
pd "決まった形でない cd" Bash "$(bash_in 'cd src && git status')"
# cd の前置き(v37): 最初のコマンドが決まった形の `CDPATH= cd -P -- <P>`(P は周の worktree の中のディレクトリ)で、次の
# 区切りが && なら、続きを P を作業ディレクトリとして通常の規則で判定する。ほかの cd の使い方は今までどおり拒否する
echo t >"$PW/src/x.txt"
PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a.append({"kind": "prefix", "words": ["python3"]}); print(json.dumps(a))')"
pa "cd の前置き: CDPATH= cd -P -- <worktree> && python3 -m unittest discover -s tests -v" Bash \
  "$(bash_in "CDPATH= cd -P -- $PW && python3 -m unittest discover -s tests -v")"
# 続きは P を作業ディレクトリとして解く(v38: 入力の cwd〈worktree〉から解くと結果が変わる対で縛る)
pa "cd の前置き: 続きの相対パスは P から解く(CDPATH= cd -P -- <worktree>/src && cat ../src/x.txt)" Bash \
  "$(bash_in "CDPATH= cd -P -- $PW/src && cat ../src/x.txt")"
pa "cd の前置き: 続きのリダイレクトの先も P から解く(CDPATH= cd -P -- src && git status > ../.claude/reviews/p.txt)" Bash \
  "$(bash_in 'CDPATH= cd -P -- src && git status > ../.claude/reviews/p.txt')"
pa "cd の前置き: 続きの区切りに | を使える(CDPATH= cd -P -- src && git status | cat)" Bash \
  "$(bash_in 'CDPATH= cd -P -- src && git status | cat')"
# H27: 保護 cwd の裸引数も検査するため、status の拒否(other)を保護書き込み(protected)より優先する
pdk "cd の前置き: 保護 cwd の裸引数と保護書き込み(CDPATH= cd -P -- .claude && git status > settings.json)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude && git status > settings.json')"
pdk "cd の前置き: 書き込み先だけが保護パス(CDPATH= cd -P -- .claude && git > settings.json)" protected Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude && git > settings.json')"
pdk "cd の前置き: プラグインルートへの前置き(CDPATH= cd -P -- <プラグインルート> && cat x)" other Bash \
  "$(bash_in "CDPATH= cd -P -- $PP && cat x")"
pdk "cd の前置き: 2 つ目の cd(CDPATH= cd -P -- <worktree> && cd sub && ls)" other Bash \
  "$(bash_in "CDPATH= cd -P -- $PW && cd sub && ls")"
pdk "cd の前置き: && 以外の区切り(CDPATH= cd -P -- <worktree> ; python3 -m unittest)" other Bash \
  "$(bash_in "CDPATH= cd -P -- $PW ; python3 -m unittest")"
pdk "cd の前置き: 決まった形でない cd(cd <worktree> && python3 -m unittest)" other Bash \
  "$(bash_in "cd $PW && python3 -m unittest")"
pdk "cd の前置き: 続きの書き込み先は通常の規則(CDPATH= cd -P -- <worktree> && python3 x > /tmp/y)" other Bash \
  "$(bash_in "CDPATH= cd -P -- $PW && python3 x > /tmp/y")"
pdk "cd の前置き: P が存在しない" other Bash "$(bash_in "CDPATH= cd -P -- $PW/nosuch && python3 -m unittest")"
pdk "cd の前置き: P がファイル" other Bash "$(bash_in "CDPATH= cd -P -- $PW/src/x.txt && python3 -m unittest")"
# v38: 行き先が - で始まる cd(`cd -P -- -` は `--` の後でも $OLDPWD へ移る)と、前置きの後ろの ;・||(cd が実行時に
# 失敗すると、後ろは元の cwd で動く)を拒否する。worktree に「-」という名のディレクトリを置いても同じ
mkdir -p -- "$PW/-"
pdk "cd の行き先が - で始まる(CDPATH= cd -P -- - && git status)" other Bash "$(bash_in 'CDPATH= cd -P -- - && git status')"
pdk "cd の行き先が - で始まる(CDPATH= cd -P -- - && pwd -P)" other Bash "$(bash_in 'CDPATH= cd -P -- - && pwd -P')"
# v39: 前置きの後ろの最初の ||・; より後ろは、P と入力の cwd の両方を作業ディレクトリとして判定する(cd が実行時に
# 失敗すると、そこから後ろは元の cwd で動くため)。どちらでも通れば allow、どちらかで拒否なら拒否
PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a += [{"kind": "prefix", "words": [w]} for w in ("python3", "test", "echo")]; print(json.dumps(a))')"
pdk "cd の前置きの後ろの || より後ろは入力の cwd からも解く(… src && git status || cat ../src/x.txt)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- src && git status || cat ../src/x.txt')"
pdk "cd の前置きの後ろの ; より後ろは入力の cwd からも解く(… src && git status ; cat ../src/x.txt)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- src && git status ; cat ../src/x.txt')"
pdk "cd の前置きの後ろの裸引数も P から解く(… .claude && git status || echo x > settings.json)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude && git status || echo x > settings.json')"
pdk "cd の前置きの後ろの || より後ろは P からも解く(… .claude && test || echo > settings.json)" protected Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude && test || echo > settings.json')"
pdk "cd の前置きの後ろは最初の || で分ける(… src && git status || cat ../src/x.txt ; git log)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- src && git status || cat ../src/x.txt ; git log')"
# P から解くと W(.claude/reviews/ の下)、入力の cwd から解くと保護パスになる書き込み先で、2 回目の判定の種類を縛る
mkdir -p -- "$PW/.claude/reviews"
pa "cd の前置きの後ろの書き込み先が P から W(CDPATH= cd -P -- .claude/reviews && echo x > .claude/settings.json)" Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude/reviews && echo x > .claude/settings.json')"
pdk "入力の cwd の側の拒否は種類をそのまま返す(… .claude/reviews && git status || echo x > .claude/settings.json)" protected Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude/reviews && git status || echo x > .claude/settings.json')"
pa "cd の前置きの後ろに && echo ok || echo ng(CDPATH= cd -P -- <worktree> && python3 -m unittest discover -s tests -v && echo ok || echo ng)" Bash \
  "$(bash_in "CDPATH= cd -P -- $PW && python3 -m unittest discover -s tests -v && echo ok || echo ng")"
pa "cd の前置きの後ろに test … && echo yes || echo no(CDPATH= cd -P -- src && test -f x && echo yes || echo no)" Bash \
  "$(bash_in 'CDPATH= cd -P -- src && test -f x && echo yes || echo no')"
pa "cd の前置きの後ろの ; より後ろが両方で通る(CDPATH= cd -P -- src && git status ; git log)" Bash \
  "$(bash_in 'CDPATH= cd -P -- src && git status ; git log')"
# v39: 行き先の字面が ~ で始まる cd を拒否する(bash は ~'/…' のように最初の引用符の外の / までに引用された文字が
# あると ~ を展開しないが、HOME から解くとずれる)。HOME から解くと worktree の中、bash の解き方では外を指す形で縛る
mkdir -p -- "$PW/~/src" "$PW/a/b" "$PW/permout"
PHOME="$PW/a/b"
pdk "cd の行き先が ~ で始まる(CDPATH= cd -P -- ~'/../../permout' && git status)" other Bash \
  "$(bash_in "CDPATH= cd -P -- ~'/../../permout' && git status")"
pdk "cd の行き先が ~ で始まる(CDPATH= cd -P -- ~'/../../permout' && pwd -P)" other Bash \
  "$(bash_in "CDPATH= cd -P -- ~'/../../permout' && pwd -P")"
pdk "cd の行き先が ~ で始まる(全体を引用: CDPATH= cd -P -- '~'/src && git status)" other Bash \
  "$(bash_in "CDPATH= cd -P -- '~'/src && git status")"
PHOME=""
# v40: 最初の ||・; より後ろの二重判定は、1 回目(P から)に保留の拒否があっても 2 回目(入力の cwd から)を必ず行い、
# どちらかに other があれば other、そうでなく protected があれば protected にする(echo は PALLOW、cat は
# PERM_ALLOW_JSON にある)
# v41: 2 つの判定は、その場で投げた拒否も受け止めてどちらも最後まで行い、理由は両方をつなぐ(記録の reason で縛る)
preason() { tail -1 "$PERMLOG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["reason"])' 2>/dev/null; }
both_reasons() { # $1=ラベル。直前の判定の記録の reason に、保護パスへの書き込みと worktree の外の両方の理由があるか
  case "$(preason)" in
    *"保護パスの下で W の外"*"worktree とプラグインルートの外"*|*"worktree とプラグインルートの外"*"保護パスの下で W の外"*) ok "$1" ;;
    *) ng "$1($(preason))" ;;
  esac
}
pdk "二重判定: 1 回目が protected・2 回目が other なら other(… .claude && echo x > settings.json || cat ../x.txt)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude && echo x > settings.json || cat ../x.txt')"
both_reasons "二重判定の理由は両方をつなぐ(… .claude && echo x > settings.json || cat ../x.txt)"
pdk "二重判定: 1 回目が protected・2 回目が other なら other(… .claude && echo x > settings.json ; cat ../x.txt)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude && echo x > settings.json ; cat ../x.txt')"
both_reasons "二重判定の理由は両方をつなぐ(… .claude && echo x > settings.json ; cat ../x.txt)"
# v41: 1 回目に溜めた protected・2 回目に溜めた other(どちらもその場で投げない)でも other にする
pdk "二重判定: 1 回目に溜めた protected・2 回目に溜めた other なら other(… .claude/reviews && echo x > ../settings.json || cat .git/HEAD)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude/reviews && echo x > ../settings.json || cat .git/HEAD')"
pdk "二重判定: 1 回目に溜めた protected・2 回目に溜めた other なら other(… .claude/reviews && echo x > ../settings.json ; cat .git/HEAD)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude/reviews && echo x > ../settings.json ; cat .git/HEAD')"
# v42: 各判定の理由をそれぞれ 240 字までに切ってからつなぐ(合計は 500 字を超えない)。1 回目の理由が上限に届く形
# (180 字の名のファイルへの書き込みを 3 つ)でも、2 回目の理由(worktree の外)が記録に残る
LN="$(python3 -c 'print("n" * 180)')"
pdk "二重判定: 1 回目の理由が長くても other(… .claude && echo x > <180 字>1 && … && echo x > <180 字>3 || cat ../x.txt)" other Bash \
  "$(bash_in "CDPATH= cd -P -- .claude && echo x > ${LN}1 && echo x > ${LN}2 && echo x > ${LN}3 || cat ../x.txt")"
both_reasons "二重判定: 1 回目の理由が長くても 2 回目の理由が残る(… .claude && echo x > <180 字>1 && … || cat ../x.txt)"
RLEN="$(preason | python3 -c 'import sys; print(len(sys.stdin.read().rstrip("\n")))')"
if [ "$RLEN" -le 500 ] && [ "$RLEN" -gt 240 ]; then ok "二重判定の理由は 500 字を超えない($RLEN 字)"; else ng "二重判定の理由は 500 字を超えない($RLEN 字)"; fi
# 両方が保留の拒否(1 回目は書き込みでない単語の other、2 回目は書き込み先の protected)でも other を優先する
pdk "二重判定: 1 回目が保留の other・2 回目が保留の protected なら other(… .claude/reviews && cat ../settings.json || echo x > .claude/settings.json)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- .claude/reviews && cat ../settings.json || echo x > .claude/settings.json')"
# v40: 行き先の ~ は位置を問わない(bash は cd の引数の x=~/y・x=a:~/y のような代入の形の = と : の直後の ~ も
# HOME に展開する)。字面では worktree の中の空のディレクトリ、HOME から解くと worktree の外を指す形で縛る
# (x= と x=a: の下に HOME の最初の段と同じ名前の、worktree の外への symlink を置く)
PHOME="$W/permhome"
mkdir -p -- "$PHOME/y" "$PW/x=~/y" "$PW/x=a:~/y" "$PW/x=" "$PW/x=a:"
H1="${PHOME#/}"; H1="${H1%%/*}"
ln -s -- "/$H1" "$PW/x=/$H1"
ln -s -- "/$H1" "$PW/x=a:/$H1"
check "前提: bash は cd の x=~/y の ~ を HOME に展開して worktree の外へ移る" "$PHOME/y" \
  "$(env -i -C "$PW" HOME="$PHOME" PATH="$PATH" bash -c 'CDPATH= cd -P -- x=~/y && pwd -P' 2>/dev/null)"
check "前提: bash は cd の x=a:~/y の ~ を HOME に展開して worktree の外へ移る" "$PHOME/y" \
  "$(env -i -C "$PW" HOME="$PHOME" PATH="$PATH" bash -c 'CDPATH= cd -P -- x=a:~/y && pwd -P' 2>/dev/null)"
pdk "cd の行き先の = の直後の ~(CDPATH= cd -P -- x=~/y && git status)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- x=~/y && git status')"
pdk "cd の行き先の : の直後の ~(CDPATH= cd -P -- x=a:~/y && pwd -P)" other Bash \
  "$(bash_in 'CDPATH= cd -P -- x=a:~/y && pwd -P')"
rm -f -- "$PW/x=/$H1" "$PW/x=a:/$H1"   # worktree の外への symlink を後の検査に残さない
PHOME=""
PALLOW=""
pd "改行" Bash "$(bash_in "$(printf 'git status\ngit log')")"
pd "括弧" Bash "$(bash_in '(git status)')"
pd "単独の &" Bash "$(bash_in 'git status & git log')"
pdk "入力の cwd が worktree の外" other Bash "$(bash_in 'git status')" "$W/permout"
pd "引用符の外のグロブ(find . -name *)" Bash "$(bash_in 'find . -name *')"
pd "引用符の外のグロブ(許可リストにあるコマンドでも: git add *)" Bash "$(bash_in 'git add *')"
pa "sed(-i なし)は許可リストで判定する" Bash "$(bash_in 'sed -n 1p src/x')"
pdk "sed -i … .git/config(書き込み先が保護パス)" protected Bash "$(bash_in 'sed -i s/a/b/ .git/config')"
pdk "書き込みでない単語が保護パスの下(sed -n 1p .git/config)" other Bash "$(bash_in 'sed -n 1p .git/config')"
pdk "書き込みでない単語が保護パスの下(cp の元 .git/config)" other Bash "$(bash_in 'cp .git/config src/y')"
pdk "書き込みでない代入の値が保護パスの下(LANG=.git)" other Bash "$(bash_in 'LANG=.git git status')"
pd "sed -i … ~/.bashrc" Bash "$(bash_in 'sed -i s/a/b/ ~/.bashrc')"
pdk "rm -rf .claude" protected Bash "$(bash_in 'rm -rf .claude')"
pdk "mv .claude x" protected Bash "$(bash_in 'mv .claude x')"
pdk "H26: worktree 自体の絶対パスを rm -rf" other Bash "$(bash_in "rm -rf $PW")"
mkdir -p -- "$PW/h26/protected/.claude" "$PW/h26/git/.config/git" "$PW/h26/dotgit/.git" "$PW/h26/name" \
  "$PW/h26/ordinary/d" "$PW/.claude/worktrees/h26" "$PW/.claude/reviews/h26"
echo protected >"$PW/h26/protected/.claude/settings.json"
echo protected >"$PW/h26/name/.mcp.json"
echo ordinary >"$PW/h26/ordinary/d/file"
ln -s -- "$PW/.claude/settings.json" "$PW/h26/ordinary/protected-link"
echo worktree >"$PW/.claude/worktrees/h26/file"
echo review >"$PW/.claude/reviews/h26/file"
pdk "H26: .claude/settings.json を含む子孫" protected Bash "$(bash_in 'rm -rf h26/protected')"
pdk "H26: .config/git を含む子孫" protected Bash "$(bash_in 'rm -rf h26/git')"
pdk "H26: .git を含む子孫" protected Bash "$(bash_in 'rm -rf h26/dotgit')"
pdk "H26: 保護ファイル名を含む子孫" protected Bash "$(bash_in 'rm -rf h26/name')"
pa "H26: 通常木の末尾 /・.." Bash "$(bash_in 'rm -rf h26/ordinary/../ordinary/')"
pa "H26: .claude/worktrees 下の通常木" Bash "$(bash_in 'rm -rf .claude/worktrees/h26')"
pa "H26: reviews 下の通常木" Bash "$(bash_in 'rm -rf .claude/reviews/h26')"
ln -s -- "h26/ordinary" "$PW/h26-link"
ln -s -- "h26-link" "$PW/h26-multi-link"
ln -s -- ".claude/reviews/h26" "$PW/h26-review-link"
ln -s -- ".claude/settings.json" "$PW/h26-protected-link"
ln -s -- "." "$PW/h26-worktree-link"
pa "H26: 最終 symlink 自体の削除は H36 のまま" Bash "$(bash_in 'rm -rf h26-protected-link')"
pa "H26: 末尾 / の通常先と多段リンク" Bash "$(bash_in 'rm -rf h26-multi-link//')"
pa "H26: 末尾 / の W の先" Bash "$(bash_in 'rm -rf h26-review-link/')"
pdk "H26: 末尾 / の保護先" protected Bash "$(bash_in 'rm -rf h26-protected-link/')"
pdk "H26: 末尾 / の worktree 先" other Bash "$(bash_in 'rm -rf h26-worktree-link/')"
ln -s -- "$PW/h26/ordinary" "$PP/h26-plugin-link"
pdk "H26: plugin_root 内の通常先へのリンク末尾 /" other Bash "$(bash_in "rm -rf $PP/h26-plugin-link/")"
pdk "H26: plugin_root 内の通常先へのリンク末尾 //" other Bash "$(bash_in "rm -rf $PP/h26-plugin-link//")"
mkdir -p -- "$PP/h26-plugin-target"
echo plugin >"$PP/h26-plugin-target/file"
ln -s -- "$PP/h26-plugin-target" "$PW/h26-worktree-link-to-plugin"
pa "H26: worktree 内から plugin_root へのリンク自体の削除は H36 のまま" Bash "$(bash_in 'rm -rf h26-worktree-link-to-plugin')"
pdk "H26: worktree 内から plugin_root へのリンク末尾 /" other Bash "$(bash_in 'rm -rf h26-worktree-link-to-plugin/')"
pdk "H26: worktree 内から plugin_root へのリンク末尾 //" other Bash "$(bash_in 'rm -rf h26-worktree-link-to-plugin//')"
check "H26: 拒否した plugin_root 側のリンク先は scratch で不変" 0 "$(test -f "$PP/h26-plugin-target/file"; echo $?)"
rm -rf -- "$PW/h26/ordinary"
check "H26: 許可した通常木だけを scratch で削除" 1 "$(test -e "$PW/h26/ordinary"; echo $?)"
check "H26: 通常木内のリンク先の保護ファイルは残る" 0 "$(test -f "$PW/.claude/settings.json"; echo $?)"
mkdir -p -- "$PW/h26-target/d"
echo target >"$PW/h26-target/d/file"
ln -s -- "h26-target" "$PW/h26-exec-link"
pa "H26: 末尾 / の通常リンク先を実行前に許可" Bash "$(bash_in 'rm -rf h26-exec-link/')"
rm -rf -- "$PW/h26-exec-link/"
check "H26: 末尾 / のリンク先だけを scratch で削除" 1 "$(test -e "$PW/h26-target/d/file"; echo $?)"
check "H26: 末尾 / のリンク自体は scratch に残る" 0 "$(test -L "$PW/h26-exec-link"; echo $?)"
pdk "cp .claude/reviews/settings.json .claude/" protected Bash "$(bash_in 'cp .claude/reviews/settings.json .claude/')"
pdk "mkdir .claude(.claude そのものは W に入れない)" protected Bash "$(bash_in 'mkdir .claude')"
pdk "リダイレクトの先が保護パス(> .git/x)" protected Bash "$(bash_in 'git status > .git/x')"
pd "rm -r .claude/reviews(そのものは消さない)" Bash "$(bash_in 'rm -r .claude/reviews')"
pd "完全一致の規則 Bash(npm run build) に対する npm run build --watch" Bash "$(bash_in 'npm run build --watch')"
pd "GIT_DIR=… git …" Bash "$(bash_in 'GIT_DIR=x git status')"
pd "GIT_WORK_TREE=… git …" Bash "$(bash_in 'GIT_WORK_TREE=x git status')"
pd "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=… git …" Bash "$(bash_in 'GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.x GIT_CONFIG_VALUE_0=y git status')"
pd "GIT_PAGER=… git …" Bash "$(bash_in 'GIT_PAGER=cat git log')"
pd "export GIT_DIR=…" Bash "$(bash_in 'export GIT_DIR=x')"
pd "CDPATH に値" Bash "$(bash_in 'CDPATH=/tmp git status')"
pd "cp --target-directory=.claude x" Bash "$(bash_in 'cp --target-directory=.claude x')"
pd "sed -i.bak … .claude/grasp.md" Bash "$(bash_in 'sed -i.bak s/a/b/ .claude/grasp.md')"
pd "sed --in-place=.bak … .claude/grasp.md" Bash "$(bash_in 'sed --in-place=.bak s/a/b/ .claude/grasp.md')"
pd "列挙に無いオプション(mkdir -p src/d)" Bash "$(bash_in 'mkdir -p src/d')"
pd "列挙に無いオプション(rm -d src/x)" Bash "$(bash_in 'rm -d src/x')"
pd "プラグインルートへの書き込み(sed -i …)" Bash "$(bash_in "sed -i s/a/b/ $PP/skills/x/ref.md")"
pd "プラグインルートへの書き込み(rm …)" Bash "$(bash_in "rm $PP/skills/x/ref.md")"
pd "プラグインルートへの書き込み(cp x <プラグインルート>/)" Bash "$(bash_in "cp src/x $PP/skills/")"
pd "--name=値 の値が外を指す(git --git-dir=…)" Bash "$(bash_in "git --git-dir=$W/permout status")"
pd "パスの引数が外を指す(git -C ..)" Bash "$(bash_in 'git -C .. status')"
pd "リダイレクトの先が外" Bash "$(bash_in "git status > $W/permout/x")"
pdk "保護パスの下のタスク MD への Write(タスク MD は W に入れない — v20)" protected Write "$(file_in "$PW/.claude/tasks/完了_t.md")"
pdk "保護パスの外の worktree の中への Write も W の外なら deny(③ (a))" other Write "$(file_in "$PW/src/new.py")"
pdk "tee /dev/null(ファイル操作の対象に /dev/null は使えない)" other Bash "$(bash_in 'tee /dev/null')"
pdk "git status;(末尾の区切り)" other Bash "$(bash_in 'git status;')"
pd "プラグインルートの外の Read" Read "$(file_in "$W/permout/x")"
pd "扱わないツール" WebFetch '{"url": "https://example.invalid"}'
# 物理パスへの解決: 入力の cwd・Read の対象・環境変数の worktree(symlink を辿って比べる)
ln -s "$W/permout" "$PP/outlink"
ln -s "$PW" "$W/pwlink"
pdk "入力の cwd が symlink で worktree の外を指す" other Bash "$(bash_in 'git status')" "$PW/.claude/reviews/escape"
pd "プラグインルートの下の symlink で外を指す Read" Read "$(file_in "$PP/outlink/x")"
PWT="$W/pwlink"
pa "環境変数の worktree が symlink でも物理パスで比べる(W の中への Write)" Write "$(file_in "$PW/.claude/reviews/x.md")"
PWT=""
# v16 の §8 の受け方(D22 ④ の書き方)
pa "事前検査を直接実行(bash <プラグインルート>/…/diff-snapshot.sh --cwd <worktree> --precheck)" Bash \
  "$(bash_in "bash $PP/skills/do-task/scripts/diff-snapshot.sh --cwd $PW --precheck")"
pa "git hash-object .claude/reviews/msg.txt" Bash "$(bash_in 'git hash-object .claude/reviews/msg.txt')"
pa "cat < .claude/reviews/msg.txt" Bash "$(bash_in 'cat < .claude/reviews/msg.txt')"
pa "gh pr create --title t --body-file - < .claude/reviews/pr.md" Bash "$(bash_in 'gh pr create --title t --body-file - < .claude/reviews/pr.md')"
pa "git commit -F .claude/reviews/msg.txt" Bash "$(bash_in 'git commit -F .claude/reviews/msg.txt')"
pdk "H35: Git alias の -c を拒否" other Bash "$(bash_in "git -c alias.pwn='!touch .claude/settings.json' pwn")"
pdk "H35: Git symbolic-ref の更新を拒否" other Bash "$(bash_in 'git symbolic-ref refs/remotes/origin/HEAD refs/heads/evil')"
pdk "H35: Git tag の更新を拒否" other Bash "$(bash_in 'git tag -f x HEAD')"
pdk "H35: Git remote の更新を拒否" other Bash "$(bash_in 'git remote rename origin evil')"
pdk "H35: Git add の file pathspec を拒否" other Bash "$(bash_in 'git add --pathspec-from-file=src/x')"
pdk "H35: Git grep の pager option を拒否" other Bash "$(bash_in "git grep --open-files-in-pager='touch .claude/settings.json' x")"
pdk "H35: Git cat-file の textconv を拒否" other Bash "$(bash_in 'git cat-file --filters HEAD:src/x')"
pdk "H35: Git diff の output option を拒否" other Bash "$(bash_in 'git diff --output=.claude/settings.json')"
pdk "H35: Git checkout の magic pathspec を拒否" other Bash "$(bash_in "git checkout -- ':(glob)**/.claude/*'")"
pdk "H35: Git restore の全体操作を拒否" other Bash "$(bash_in 'git restore .')"
pdk "H35: Git clean を拒否" other Bash "$(bash_in 'git clean -fd')"
pdk "H35: Git branch の作成を拒否" other Bash "$(bash_in 'git branch accidental')"
pdk "H35: Git reflog の削除を拒否" other Bash "$(bash_in 'git reflog drop HEAD')"
pdk "H35: env 経由の Git clean を拒否" other Bash "$(bash_in 'env git clean -fd')"
pdk "H35: command 経由の Git reset を拒否" other Bash "$(bash_in 'command git reset --hard')"
pdk "H35: bash -c 経由の Git clean を拒否" other Bash "$(bash_in "bash -c 'git clean -fd'")"
pa "H35: Git symbolic-ref の HEAD query を許す" Bash "$(bash_in 'git symbolic-ref --quiet HEAD')"
pa "H35: Git remote の query を許す" Bash "$(bash_in 'git remote get-url --all origin')"
pa "H35: Git switch -c を許す" Bash "$(bash_in 'git switch -c task/h35-normal')"
pa "H35: Git switch --no-track -c を許す" Bash "$(bash_in 'git switch --no-track -c task/h35-unattended')"
pa "H35: base-commit の verify query を許す" Bash \
  "$(bash_in "git -c core.splitIndex=false -c core.filemode=true -c core.symlinks=true rev-parse --verify --quiet 'HEAD^{commit}'")"
pa "H35: index 除外確認の diff --no-relative を許す" Bash \
  "$(bash_in "git --no-literal-pathspecs diff --cached --name-only --no-relative -- ':(top,glob)**/[.]claude/reviews/**'")"
pa "H35: refs の format query を許す" Bash \
  "$(bash_in "git for-each-ref --format='%(objectname)%09%(refname)' refs/heads/task refs/remotes")"
pa "H35: 日本語 task branch を許す" Bash "$(bash_in 'git switch --no-track -c task/候補-日本語')"
pa "H35: 空 tree の hash-object を許す" Bash "$(bash_in 'git hash-object -t tree /dev/null')"
pa "H35: 連結した安全な -c を許す" Bash "$(bash_in 'git -ccore.hooksPath=/dev/null status --short')"
pdk "H23: sed の read script を拒否" other Bash "$(bash_in "sed -n '1r .claude/settings.json' src/x")"
pdk "H23: sed の execute flag を拒否" other Bash "$(bash_in "sed -n 's/x/y/e' src/x")"
pa "H23: sed --sandbox の print script を許す" Bash "$(bash_in "sed --sandbox -n '1,3p' src/x")"
pdk "H23: find -exec を拒否" other Bash "$(bash_in 'find . -exec touch .claude/settings.json \;')"
pdk "H23: find -delete を拒否" other Bash "$(bash_in 'find . -delete')"
PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a.append({"kind":"prefix","words":["find"]}); print(json.dumps(a))')"
pa "H23: find の読み取り action を許す" Bash "$(bash_in "find . -type f -name '*.txt' -print")"
PALLOW=""
pdk "H23: 任意 awk program を拒否" other Bash "$(bash_in "awk 'BEGIN { system(\"touch .claude/settings.json\") }'")"
pdk "H40: gh api を拒否" other Bash "$(bash_in 'gh api repos/example/example')"
pdk "H40: gh repo edit を拒否" other Bash "$(bash_in 'gh repo edit --visibility public')"
pdk "H40: gh の .env stdin を拒否" other Bash "$(bash_in 'gh pr create --title t --body-file - < .env')"
pdk "H40: gh の複数 stdin を拒否" other Bash "$(bash_in 'gh pr create --title t --body-file - < .claude/reviews/pr.md < src/x')"
pdk "H40: gh の pipe stdin を拒否" other Bash "$(bash_in 'cat .claude/reviews/pr.md | gh pr create --title t --body-file -')"
pa "H40: gh pr view の state query を許す" Bash "$(bash_in 'gh pr view 1 --json state')"
pdk "旧い受け方(tmp=\"\$(mktemp)\" && …)" other Bash "$(bash_in "tmp=\"\$(mktemp)\" && bash $PP/skills/do-task/scripts/diff-snapshot.sh --cwd $PW --precheck > \"\$tmp\"")"
pdk "TOP=\$(git rev-parse --show-toplevel)" other Bash "$(bash_in 'TOP=$(git rev-parse --show-toplevel)')"
pdk "git -C \"\$TOP\" rev-parse HEAD" other Bash "$(bash_in 'git -C "$TOP" rev-parse HEAD')"
# v18・v19: test の検査・git -c の値・2>/dev/null の後のリダイレクト
# 許可リストに test を足した形(この 4 項目だけ)
PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a.append({"kind": "prefix", "words": ["test"]}); print(json.dumps(a))')"
pa "test -f .claude/grasp.md(test が許可リストにあるとき)" Bash "$(bash_in 'test -f .claude/grasp.md')"
pa "test ! -L .claude/grasp.md(同)" Bash "$(bash_in 'test ! -L .claude/grasp.md')"
pa "test -e .claude/reviews/x || test -L .claude/reviews/x(同)" Bash "$(bash_in 'test -e .claude/reviews/x || test -L .claude/reviews/x')"
pdk "[ -f .claude/grasp.md ](引用符の外の [)" other Bash "$(bash_in '[ -f .claude/grasp.md ]')"
PALLOW=""
pa "git -c core.hooksPath=/dev/null status --short 2>/dev/null > .claude/reviews/st4.txt" Bash \
  "$(bash_in 'git -c core.hooksPath=/dev/null status --short 2>/dev/null > .claude/reviews/st4.txt')"
pdk "mkdir -p .claude/reviews/sub(列挙に無いオプション)" other Bash "$(bash_in 'mkdir -p .claude/reviews/sub')"
pdk "test -f .claude/grasp.md(test が許可リストに無いとき)" other Bash "$(bash_in 'test -f .claude/grasp.md')"
case "$(tail -1 "$PERMLOG")" in *"許可リストに無いコマンド"*) ok "test が許可リストに無いときの理由は「許可リストに無いコマンド」" ;;
  *) ng "test が許可リストに無いときの理由は「許可リストに無いコマンド」" ;; esac
# D22 ④ の対応表: (1)〜(10) の各項に、従った例(allow)と外れた例(deny)を 1 つ以上ずつ置く
# (許可リストは #68 の決定 5 の形。git・sed・bash・gh・cat・test・sha256sum など)
PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a += [{"kind": "prefix", "words": ["test"]}, {"kind": "prefix", "words": ["sha256sum"]}]; print(json.dumps(a))')"
pa "④ (1) 従う: \$ は単引用符の中だけ(git log --format='%H \$x')" Bash "$(bash_in "git log --format='%H \$x'")"
pd "④ (1) 外れる: 二重引用符の中の \$(git log \"\$HOME\")" Bash "$(bash_in 'git log "$HOME"')"
pa "④ (2) 従う: # は単語の先頭でなく引用符の中(git log --grep='#1')" Bash "$(bash_in "git log --grep='#1'")"
pd "④ (2) 外れる: コメント(git status # x)" Bash "$(bash_in 'git status # x')"
pd "④ (2) 外れる: here-string(git hash-object --stdin <<<x)" Bash "$(bash_in 'git hash-object --stdin <<<x')"
pa "④ (3) 従う: 区切りは && だけ(git status && git log)" Bash "$(bash_in 'git status && git log')"
pd "④ (3) 外れる: 末尾の区切り(git status &&)" Bash "$(bash_in 'git status &&')"
pd "④ (3) 外れる: サブシェル((git status))" Bash "$(bash_in '(git status)')"
pa "④ (4) 従う: test と、単引用符で囲んだ中括弧(git rev-parse 'HEAD^{tree}')" Bash "$(bash_in "test -f .claude/grasp.md && git rev-parse 'HEAD^{tree}'")"
pd "④ (4) 外れる: [ … ]" Bash "$(bash_in '[ -f .claude/grasp.md ]')"
pd "④ (4) 外れる: 引用符の外の中括弧(git rev-parse HEAD^{tree})" Bash "$(bash_in 'git rev-parse HEAD^{tree}')"
pa "④ (5) 従う: 5 つの名の代入(LC_ALL=C git status)" Bash "$(bash_in 'LC_ALL=C git status')"
pd "④ (5) 外れる: 5 つの名でない代入(GIT_EDITOR=true git commit)" Bash "$(bash_in 'GIT_EDITOR=true git commit')"
pa "④ (6) 従う: cd は決まった形の 1 文だけ" Bash "$(bash_in 'CDPATH= cd -P -- src && pwd -P')"
pa "④ (6) 従う: cd の前置き(CDPATH= cd -P -- src && git status)" Bash "$(bash_in 'CDPATH= cd -P -- src && git status')"
pd "④ (6) 外れる: 2 つ目の cd(CDPATH= cd -P -- src && cd .. && git status)" Bash "$(bash_in 'CDPATH= cd -P -- src && cd .. && git status')"
pa "④ (6) 従う: 別のディレクトリの git は git -C <パス>" Bash "$(bash_in "git -C $PW/src status")"
pa "④ (7) 従う: リダイレクトの先に /dev/null・読むパスは worktree の中" Bash "$(bash_in 'git diff -- src/x > /dev/null')"
pdk "④ (7) 外れる: ファイル操作の対象に /dev/null(tee /dev/null)" other Bash "$(bash_in 'tee /dev/null')"
pdk "④ (7) 外れる: 読むだけでも保護パスの下は W の中だけ(git log -- .git/HEAD)" other Bash "$(bash_in 'git log -- .git/HEAD')"
pd "④ (7) 外れる: /tmp を使う" Bash "$(bash_in 'git status > /tmp/x')"
pd "④ (7) 外れる: 短いオプションにパスをつなげる(git log -o/tmp/x)" Bash "$(bash_in 'git log -o/tmp/x')"
pa "④ (8) 従う: 列のオプションだけ(rm -f .claude/grasp.md)" Bash "$(bash_in 'rm -f .claude/grasp.md')"
pd "④ (8) 外れる: mkdir -p" Bash "$(bash_in 'mkdir -p .claude/reviews/sub')"
pd "④ (8) 外れる: sed のオプションを束ねる(sed -ni …)" Bash "$(bash_in 'sed -ni s/a/b/ src/x')"
pa "④ (9) 従う: 許可リストに一致するコマンド(sha256sum .claude/reviews/a)" Bash "$(bash_in 'sha256sum .claude/reviews/a')"
pd "④ (9) 外れる: 許可リストに無いコマンド(curl --version)" Bash "$(bash_in 'curl --version')"
pa "④ (10) 従う: 本文のファイルを < で渡す(gh pr create … --body-file - < .claude/reviews/pr.md)" Bash "$(bash_in 'gh pr create --title t --body-file - < .claude/reviews/pr.md')"
pa "④ (10) 従う: -F で渡す(git commit -F .claude/reviews/msg.txt)" Bash "$(bash_in 'git commit -F .claude/reviews/msg.txt')"
pd "④ (10) 外れる: 一時ファイルを worktree の外に置く(git commit -F /tmp/msg)" Bash "$(bash_in 'git commit -F /tmp/msg')"
# unattended-mode.md の「委託するサブエージェント」の項(サブエージェントはこの項の要点だけを受け取る)の列の行が、許可の
# 仲介の実装の列と両向きで一致する。見るのは列の行の主部のバッククォート語だけ(括弧の中の補足の語は入れない):
#   「保護パスのディレクトリ」の行 = PROTECTED_DIRS と `.config/git`(is_protected の別の分岐)。`.config/git` と、括弧の
#     中の `.claude/worktrees`(除外。これも別の分岐)は、読み込んだ is_protected の挙動と両向きに照らす
#   「保護パスのファイル名」の行 = PROTECTED_FILES / 代入を許す名の行(「は、」から「の N つの名だけ」まで)= ASSIGN_NAMES。
#     N も len(ASSIGN_NAMES) と照らす
# 実装の列はモジュールを読み込んで取る(ハードコードしない。-B で __pycache__ を作らない)
UM_DOC="$PLUGIN_SRC/skills/ship-task/references/unattended-mode.md"
UM_DIFF="$(python3 -B -c '
import importlib.util, re, sys
sys.dont_write_bytecode = True
try:
    spec = importlib.util.spec_from_file_location("loop_permission", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
except Exception as exc:
    print("(実装の列を読めない: %s)" % exc)
    sys.exit(0)
text = open(sys.argv[2], encoding="utf-8").read()
start = text.find("**委託するサブエージェント**")
end = text.find("**反復の数え方**", start) if start >= 0 else -1
if start < 0 or end < 0:
    print("(項の範囲が見つからない)")
    sys.exit(0)
lines = text[start:end].split("\n")
def main_words(body):
    # 括弧の中(補足)を落としてから、バッククォート語を取る
    prev = None
    while prev != body:
        prev, body = body, re.sub(r"[(][^()]*[)]", "", body)
    return re.findall(r"`([^`]+)`", body)
def one(prefix):
    hits = [l for l in lines if l.lstrip().startswith(prefix)]
    return hits[0] if len(hits) == 1 else None
problems = []
dirs_line = one("- 保護パスのディレクトリ")
files_line = one("- 保護パスのファイル名")
assign_line = one("- コマンドの前の環境変数の代入と")
if not dirs_line or not files_line or not assign_line:
    print("(列の行がちょうど 1 つずつ見つからない)")
    sys.exit(0)
checks = []
dirs_body = dirs_line.split(": ", 1)[1] if ": " in dirs_line else ""
dir_words = main_words(dirs_body)
# 別の分岐 1: `.config/git`(主部にある ⇔ 実装が .config/git の下を保護し、.config の下のほかは保護しない)
doc_cfg = ".config/git" in dir_words
impl_cfg = mod.is_protected(".config/git/x") and not mod.is_protected(".config/x")
if doc_cfg != impl_cfg:
    problems.append(".config/git: 文書 %s・実装 %s" % ("あり" if doc_cfg else "なし", "あり" if impl_cfg else "なし"))
if doc_cfg:
    dir_words.remove(".config/git")
# 別の分岐 2: `.claude/worktrees` の除外(括弧の中にある ⇔ 実装が .claude/worktrees の下を除き、.claude の下のほかは保護する)
parens = re.findall(r"[(]([^()]*)[)]", dirs_body)
doc_wt = any("`.claude/worktrees`" in p for p in parens)
impl_wt = (not mod.is_protected(".claude/worktrees/x")) and mod.is_protected(".claude/x")
if doc_wt != impl_wt:
    problems.append(".claude/worktrees の除外: 文書 %s・実装 %s" % ("あり" if doc_wt else "なし", "あり" if impl_wt else "なし"))
checks.append(("ディレクトリ", set(dir_words), set(mod.PROTECTED_DIRS)))
checks.append(("ファイル名", set(main_words(files_line.split(": ", 1)[1] if ": " in files_line else "")), set(mod.PROTECTED_FILES)))
m = re.search(r"は、(.*?)の ([0-9]+) つの名だけ", assign_line)
checks.append(("代入を許す名", set(main_words(m.group(1))) if m else set(), set(mod.ASSIGN_NAMES)))
if not m:
    problems.append("代入を許す名の行に「の N つの名だけ」が無い")
elif int(m.group(2)) != len(mod.ASSIGN_NAMES):
    problems.append("代入を許す名の数: 文書 %s・実装 %d" % (m.group(2), len(mod.ASSIGN_NAMES)))
for label, doc, impl in checks:
    if doc - impl:
        problems.append("%s: 文書にだけある名: %s" % (label, " ".join(sorted(doc - impl))))
    if impl - doc:
        problems.append("%s: 実装にだけある名: %s" % (label, " ".join(sorted(impl - doc))))
print(" / ".join(problems))
' "$PERM_SRC" "$UM_DOC" 2>&1)"
if [ -z "$UM_DIFF" ]; then ok "委託するサブエージェントの項の列の行が、実装の保護パスと代入を許す名の列と両向きで一致する"
else ng "委託するサブエージェントの項の列の行が、実装の保護パスと代入を許す名の列と両向きで一致する($UM_DIFF)"; fi
# v22: 有無の確かめを && と || で表す(test・echo が許可リストにあるとき)
PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a += [{"kind": "prefix", "words": ["test"]}, {"kind": "prefix", "words": ["echo"]}]; print(json.dumps(a))')"
pa "④ (3)・(4) 従う: test -f .claude/grasp.md && echo yes || echo no(test・echo が許可リストにある)" Bash \
  "$(bash_in 'test -f .claude/grasp.md && echo yes || echo no')"
pa "④ (3)・(4) 従う: test -e … || test -L … && echo yes || echo no(同)" Bash \
  "$(bash_in 'test -e .claude/reviews/x || test -L .claude/reviews/x && echo yes || echo no')"
PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a.append({"kind": "prefix", "words": ["test"]}); print(json.dumps(a))')"
pdk "④ (9) 外れる: echo が許可リストに無いときの test -f … && echo yes || echo no" other Bash \
  "$(bash_in 'test -f .claude/grasp.md && echo yes || echo no')"
case "$(tail -1 "$PERMLOG")" in *"許可リストに無いコマンド: echo"*) ok "echo が許可リストに無いときの理由は「許可リストに無いコマンド: echo」" ;;
  *) ng "echo が許可リストに無いときの理由は「許可リストに無いコマンド: echo」" ;; esac
PALLOW=""
# v23: 本文ダイジェストの取り出し・節の取り出し・終了コードの確かめ(grep・sed・bash が許可リストにあるとき)
PALLOW="$(printf '%s' "$PERM_ALLOW_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); a += [{"kind": "prefix", "words": ["grep"]}]; print(json.dumps(a))')"
pa "④ (7) 従う: パターンの / を [/] で書く(grep -oE '[/] 本文: …' … > .claude/reviews/d.txt)" Bash \
  "$(bash_in "grep -oE '[/] 本文: (sha256:[0-9a-f]{16}|算出不能) [/] needs-user: ' docs/tasks/進行中_x.md > .claude/reviews/d.txt")"
pa "④ (7) 従う: sed の区切りを / 以外にする(sed -n '\\%^## %p' AGENTS.md > .claude/reviews/sec2.txt)" Bash \
  "$(bash_in "sed -n '\\%^## %p' AGENTS.md > .claude/reviews/sec2.txt")"
pa "④ (10) 従う: 終了コードはツールの結果で見る(bash -c 'exit 21')" Bash "$(bash_in "bash -c 'exit 21'")"
pdk "④ (7) 外れる: 先頭が / のパターンはパスとして外を指す(grep -oE '/ 本文: …')" other Bash \
  "$(bash_in "grep -oE '/ 本文: (sha256:[0-9a-f]{16}|算出不能) / needs-user: ' docs/tasks/進行中_x.md > .claude/reviews/d.txt")"
PALLOW=""
# deny の message: 理由と固定の文(無人の周では打ち直さず G1 に従う)。allow には message が無い。PERMLOG の reason には
# 固定の文を入れない
perm Bash "$(bash_in 'curl --version')"
DMSG="$(decision_field message)"
case "$DMSG" in *"許可リストに無いコマンド"*) ok "deny の message に理由がある" ;; *) ng "deny の message に理由がある($DMSG)" ;; esac
case "$DMSG" in *"回り込"*"G1"*) ok "deny の message に固定の文(回り込まず G1 に従う)がある" ;;
  *) ng "deny の message に固定の文(回り込まず G1 に従う)がある($DMSG)" ;; esac
LREASON="$(tail -1 "$PERMLOG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["reason"])' 2>/dev/null)"
case "$LREASON" in *"回り込"*|*"G1"*) ng "PERMLOG の reason に固定の文を入れない($LREASON)" ;;
  *"許可リストに無いコマンド"*) ok "PERMLOG の reason に固定の文を入れない(理由だけ)" ;; *) ng "PERMLOG の reason に理由がある($LREASON)" ;; esac
perm Write "$(file_in "$PW/.claude/reviews/x.md")"
check "allow の出力に message が無い" "(無い)" "$(decision_field message)"
# 読めない JSON・環境変数が無い
out="$(printf 'not json' | env -i PATH="$SAFEBIN" DEV_WORKFLOW_LOOP_WORKTREE="$PW" DEV_WORKFLOW_LOOP_PERMLOG="$PERMLOG" \
  DEV_WORKFLOW_LOOP_PLUGIN_ROOT="$PP" DEV_WORKFLOW_LOOP_ALLOW="$PERM_ALLOW_JSON" python3 "$PERM_BIN")"
case "$out" in *'"behavior": "deny"'*) ok "hook deny: 読めない JSON" ;; *) ng "hook deny: 読めない JSON($out)" ;; esac
out="$(bash_in 'git status' | python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":json.load(sys.stdin),"cwd":sys.argv[1]}))' "$PW" \
  | env -i PATH="$SAFEBIN" DEV_WORKFLOW_LOOP_PERMLOG="$PERMLOG" python3 "$PERM_BIN")"
case "$out" in *'"behavior": "deny"'*) ok "hook deny: 環境変数が無い" ;; *) ng "hook deny: 環境変数が無い($out)" ;; esac
# 記録: allow も deny も 1 呼び出し 1 行、判定つき
python3 - "$PERMLOG" <<'PY' && ok "hook の記録: allow と deny が判定・種類つきで 1 行ずつある" || ng "hook の記録: allow と deny が判定・種類つきで 1 行ずつある"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
assert any(r["decision"] == "allow" for r in rows)
assert any(r["decision"] == "deny" and r["kind"] == "protected" for r in rows)
assert any(r["decision"] == "deny" and r["kind"] == "other" for r in rows)
assert all({"time", "tool_name", "cwd", "decision", "kind", "reason", "subject"} <= set(r) for r in rows)
PY

# reviews-dir.sh(D22 ②。base-commit.md ①〜③ の検査と原子的な公開)
RD="$REVIEWS_DIR_SH"
RDW="$W/rdw"
rm -rf "$RDW"
mkdir -p "$RDW/ok" "$RDW/outside"
bash "$RD" ensure --root "$RDW/ok" >/dev/null 2>&1
check "reviews-dir ensure: 作る" 0 "$?"
t "reviews-dir ensure: .claude/reviews ができる" test -d "$RDW/ok/.claude/reviews"
mkdir -p "$RDW/sym"
ln -s "$RDW/outside" "$RDW/sym/.claude"
bash "$RD" ensure --root "$RDW/sym" >/dev/null 2>&1
check "reviews-dir ensure: .claude が symlink なら止まる" 3 "$?"
f "reviews-dir ensure: symlink の先に作らない" test -e "$RDW/outside/reviews"
mkdir -p "$RDW/file"
echo x >"$RDW/file/.claude"
bash "$RD" ensure --root "$RDW/file" >/dev/null 2>&1
check "reviews-dir ensure: .claude が通常のディレクトリでなければ止まる" 3 "$?"
mkdir -p "$RDW/sym2/.claude"
ln -s "$RDW/outside" "$RDW/sym2/.claude/reviews"
bash "$RD" ensure --root "$RDW/sym2" >/dev/null 2>&1
check "reviews-dir ensure: .claude/reviews が symlink なら止まる" 3 "$?"
# save-untracked
RR="$RDW/repo"
git init -q "$RR"
printf '#!/bin/sh\necho ran >>"%s/fsmonitor.marker"\nexit 1\n' "$RDW" >"$RDW/fsmonitor.sh"
chmod +x "$RDW/fsmonitor.sh"
G -C "$RR" config core.fsmonitor "$RDW/fsmonitor.sh"
echo u >"$RR/untracked.txt"
echo e >"$RR/excluded.txt"
printf 'excluded.txt\n' >"$RDW/excludes"
LISTF="$RR/.claude/reviews/base-t-untracked.z"
sha="$(env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.excludesFile GIT_CONFIG_VALUE_0="$RDW/excludes" \
  bash "$RD" save-untracked --top "$RR" --root "$RR" --list "$LISTF" 2>/dev/null)"
check "reviews-dir save-untracked: 成功" 0 "$?"
check "reviews-dir save-untracked: stdout は一覧の sha256" "$(sha256sum <"$LISTF" 2>/dev/null | cut -d' ' -f1)" "$sha"
if tr '\0' '\n' <"$LISTF" | grep -qx 'untracked.txt'; then ok "reviews-dir save-untracked: 未追跡を一覧にする(NUL 区切り)"; else ng "reviews-dir save-untracked: 未追跡を一覧にする(NUL 区切り)"; fi
f "reviews-dir save-untracked: core.fsmonitor のスクリプトを実行しない" test -e "$RDW/fsmonitor.marker"
if tr '\0' '\n' <"$LISTF" | grep -qx 'excluded.txt'; then
  ng "reviews-dir save-untracked: 渡された GIT_CONFIG_* を外さない(承認済みのフィルタの無効化が効く)"
else
  ok "reviews-dir save-untracked: 渡された GIT_CONFIG_* を外さない(承認済みのフィルタの無効化が効く)"
fi
bash "$RD" save-untracked --top "$RR" --root "$RR" --list "$LISTF" >/dev/null 2>&1
check "reviews-dir save-untracked: 既存の通常ファイルは置き換える" 0 "$?"
rm -f "$LISTF"
ln -s "$RDW/outside/target" "$LISTF"
bash "$RD" save-untracked --top "$RR" --root "$RR" --list "$LISTF" >/dev/null 2>&1
check "reviews-dir save-untracked: LIST が symlink なら止まる" 3 "$?"
f "reviews-dir save-untracked: symlink の先を書かない" test -e "$RDW/outside/target"
rm -f "$LISTF"
mkfifo "$LISTF"
timeout 20 bash "$RD" save-untracked --top "$RR" --root "$RR" --list "$LISTF" >/dev/null 2>&1
check "reviews-dir save-untracked: LIST が通常ファイルでない(FIFO)なら止まる" 3 "$?"
rm -f "$LISTF"
bash "$RD" save-untracked --top "$RR" --root "$RR" --list "$RDW/elsewhere.z" >/dev/null 2>&1
check "reviews-dir save-untracked: LIST が .claude/reviews/ の直下でなければ使い方の誤り" 2 "$?"
mkdir -p "$RDW/notrepo"
bash "$RD" save-untracked --top "$RDW/notrepo" --root "$RDW/notrepo" --list "$RDW/notrepo/.claude/reviews/x.z" >/dev/null 2>&1
check "reviews-dir save-untracked: git の失敗" 4 "$?"
f "reviews-dir save-untracked: git の失敗では何も公開しない" test -e "$RDW/notrepo/.claude/reviews/x.z"
if ls -A "$RDW/notrepo/.claude/reviews" "$RR/.claude/reviews" 2>/dev/null | grep -q '^\.base-untracked\.'; then
  ng "reviews-dir save-untracked: 一時ファイルを残さない"
else
  ok "reviews-dir save-untracked: 一時ファイルを残さない"
fi

# reviews-dir の移植性: 公開は rename(2)(python3 の os.replace、無ければ mv -f -T)、sha256 は
# sha256sum → shasum -a 256 → openssl dgst -sha256 の順。道具を選んだ PATH(reviews-dir.sh と git が使う道具の
# symlink だけを並べた scratch の bin)で打ち、外す道具は置かない。BSD の mv を模すラッパは、-f・-i・-n・-v・-h・--
# 以外の - で始まる引数(-T・--help など)を illegal option と exit 64 で拒み、ほかは本物の mv に渡す。
# 競合のケースは、git ls-files の直後に公開先を差し替える git のラッパで打つ(② の検査の後の置き換え)
RDP="$W/rdp"
rm -rf "$RDP"
mkdir -p "$RDP/outside"
RDP_REAL_MV="$(command -v mv)"
RDP_REAL_GIT="$(command -v git)"
RDP_REAL_MKDIR="$(command -v mkdir)"
RDP_REAL_LN="$(command -v ln)"
cat >"$RDP/mv-bsd" <<EOF
#!/bin/sh
for a in "\$@"; do
  case "\$a" in
    --) break ;;
    -f|-i|-n|-v|-h) : ;;
    -*) echo "mv: illegal option -- \${a#-}" >&2; echo "usage: mv [-f | -i | -n] [-hv] source target" >&2; exit 64 ;;
  esac
done
exec "$RDP_REAL_MV" "\$@"
EOF
cat >"$RDP/git-race" <<EOF
#!/bin/sh
"$RDP_REAL_GIT" "\$@"
rc=\$?
for a in "\$@"; do
  if [ "\$a" = ls-files ]; then
    case "\${RDP_RACE:-}" in
      dir) "$RDP_REAL_MKDIR" -- "\$RDP_RACE_LIST" ;;
      symlink) "$RDP_REAL_LN" -s -- "\$RDP_RACE_TARGET" "\$RDP_RACE_LIST" ;;
    esac
  fi
done
exit \$rc
EOF
chmod +x "$RDP/mv-bsd" "$RDP/git-race"
rdp_bin() { # $1=名前 残り=置く道具(mv-bsd はラッパを mv に、git-race はラッパを git に置く)→ bin のパス
  local d="$RDP/bin-$1" c src
  shift
  rm -rf "$d"
  mkdir -p "$d"
  for c in bash sed mktemp rm mkdir cut "$@"; do
    case "$c" in
      mv-bsd) ln -s "$RDP/mv-bsd" "$d/mv" ;;
      git-race) ln -s "$RDP/git-race" "$d/git" ;;
      *)
        src="$(command -v "$c" 2>/dev/null)" || continue
        case "$src" in /*) ln -s "$src" "$d/$c" ;; esac
        ;;
    esac
  done
  printf '%s' "$d"
}
RDP_REPO="$RDP/repo"
git init -q "$RDP_REPO"
echo u >"$RDP_REPO/untracked.txt"
RDP_REVIEWS="$RDP_REPO/.claude/reviews"
RDP_OUT="$RDP/out"
RDP_ERR="$RDP/err"
rdp_run() { # $1=bin $2=LIST の名前 残り=足す環境変数(VAR=値)。stdout は RDP_OUT、stderr は RDP_ERR
  local bin="$1" name="$2"
  shift 2
  env PATH="$bin" "$@" "$bin/bash" "$RD" save-untracked --top "$RDP_REPO" --root "$RDP_REPO" \
    --list "$RDP_REVIEWS/$name" >"$RDP_OUT" 2>"$RDP_ERR"
}
rdp_sha() { python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1" 2>/dev/null; }
rdp_listed() { # $1=ケース名 $2=LIST。一覧が通常ファイルとして公開され、stdout の sha256 が一覧の中身と同じ
  local want got
  if [ -f "$2" ] && [ ! -L "$2" ] && tr '\0' '\n' <"$2" | grep -qx 'untracked.txt'; then
    ok "$1: 一覧が通常ファイルとして公開される"
  else
    ng "$1: 一覧が通常ファイルとして公開される"
  fi
  want="$(rdp_sha "$2")"
  got="$(cat "$RDP_OUT" 2>/dev/null)"
  if [ -n "$want" ] && [ "$got" = "$want" ]; then ok "$1: stdout は一覧の sha256"; else
    ng "$1: stdout は一覧の sha256(stdout '$got'・一覧 '${want:-無し}')"; fi
}
rdp_no_tmp() { # $1=ケース名
  if ls -A "$RDP_REVIEWS" 2>/dev/null | grep -q '^\.base-untracked\.'; then ng "$1: 一時ファイルを残さない"; else
    ok "$1: 一時ファイルを残さない"; fi
}
rdp_nothing() { # $1=ケース名 $2=LIST。一覧も一時ファイルも残らない
  if [ -e "$2" ] || [ -L "$2" ]; then ng "$1: 一覧を公開しない"; else ok "$1: 一覧を公開しない"; fi
  rdp_no_tmp "$1"
}
RP="reviews-dir の移植性"
B="$(rdp_bin bsd python3 mv-bsd sha256sum git)"
rdp_run "$B" bsd.z
check "$RP: BSD の mv: 成功" 0 "$?"
rdp_listed "$RP: BSD の mv" "$RDP_REVIEWS/bsd.z"
B="$(rdp_bin nopy-gnu mv sha256sum git)"
rdp_run "$B" nopy-gnu.z
check "$RP: python3 なし・GNU の mv: 成功" 0 "$?"
rdp_listed "$RP: python3 なし・GNU の mv" "$RDP_REVIEWS/nopy-gnu.z"
B="$(rdp_bin nopy-bsd mv-bsd sha256sum git)"
rdp_run "$B" nopy-bsd.z
check "$RP: python3 なし・BSD の mv: 止まる" 20 "$?"
if grep -E '^ERROR \[internal\] ' "$RDP_ERR" | grep -qF 'python3'; then
  ok "$RP: python3 なし・BSD の mv: ERROR [internal] の行に python3 がある"
else
  ng "$RP: python3 なし・BSD の mv: ERROR [internal] の行に python3 がある($(head -1 "$RDP_ERR"))"
fi
rdp_nothing "$RP: python3 なし・BSD の mv" "$RDP_REVIEWS/nopy-bsd.z"
if command -v shasum >/dev/null 2>&1; then
  B="$(rdp_bin shasum python3 mv shasum perl git)"
  rdp_run "$B" shasum.z
  check "$RP: shasum: 成功" 0 "$?"
  rdp_listed "$RP: shasum" "$RDP_REVIEWS/shasum.z"
else
  ok "$RP: shasum: shasum が無いため飛ばす"
fi
if command -v openssl >/dev/null 2>&1; then
  B="$(rdp_bin openssl python3 mv openssl git)"
  rdp_run "$B" openssl.z
  check "$RP: openssl: 成功" 0 "$?"
  rdp_listed "$RP: openssl" "$RDP_REVIEWS/openssl.z"
else
  ok "$RP: openssl: openssl が無いため飛ばす"
fi
B="$(rdp_bin nosha python3 mv git)"
rdp_run "$B" nosha.z
check "$RP: sha256 の道具なし: 止まる" 20 "$?"
if grep -qE '^ERROR \[tool-missing\] ' "$RDP_ERR"; then ok "$RP: sha256 の道具なし: ERROR [tool-missing] が出る"; else
  ng "$RP: sha256 の道具なし: ERROR [tool-missing] が出る($(head -1 "$RDP_ERR"))"; fi
rdp_nothing "$RP: sha256 の道具なし" "$RDP_REVIEWS/nosha.z"
B="$(rdp_bin race python3 mv sha256sum git-race)"
rdp_run "$B" race-dir.z RDP_RACE=dir RDP_RACE_LIST="$RDP_REVIEWS/race-dir.z"
check "$RP: 競合でディレクトリ: 止まる" 20 "$?"
if [ -d "$RDP_REVIEWS/race-dir.z" ] && [ -z "$(ls -A "$RDP_REVIEWS/race-dir.z" 2>/dev/null)" ]; then
  ok "$RP: 競合でディレクトリ: ディレクトリの中に一覧が入らない"
else
  ng "$RP: 競合でディレクトリ: ディレクトリの中に一覧が入らない"
fi
rdp_no_tmp "$RP: 競合でディレクトリ"
rdp_run "$B" race-sym.z RDP_RACE=symlink RDP_RACE_LIST="$RDP_REVIEWS/race-sym.z" RDP_RACE_TARGET="$RDP/outside"
check "$RP: 競合で symlink: 成功" 0 "$?"
rdp_listed "$RP: 競合で symlink" "$RDP_REVIEWS/race-sym.z"
if [ -z "$(ls -A "$RDP/outside" 2>/dev/null)" ]; then ok "$RP: 競合で symlink: 外のディレクトリに一覧を書かない"; else
  ng "$RP: 競合で symlink: 外のディレクトリに一覧を書かない"; fi
B="$(rdp_bin plain python3 mv sha256sum git)"
mkdir -p "$RDP_REVIEWS/isdir.z"
rdp_run "$B" isdir.z
check "$RP: 公開先がディレクトリ: 止まる" 3 "$?"
if grep -qE '^ERROR \[list-not-regular\] ' "$RDP_ERR"; then ok "$RP: 公開先がディレクトリ: ERROR [list-not-regular] が出る"; else
  ng "$RP: 公開先がディレクトリ: ERROR [list-not-regular] が出る($(head -1 "$RDP_ERR"))"; fi
fi

# ════════════════ 許可の仲介の loop.sh の側(D22 ①・③・起動時の検査・D6)════════════════
if want d22; then
# 周の worktree の .claude/reviews/ は周の起動の前にある / 子の環境の 5 つ / hook のコマンドは空白を含む配置でも 1 つ
newrepo d22
addtask hookcall-a
commit
newrec d22
mkdir -p "$W/ccd"
printf '{"permissions": {"allow": ["Bash(make test)", "Bash(git * main)", "Read"]}}\n' >"$W/ccd/settings.json"
SPACEPLUG="$W/plug dir"
rm -rf "$SPACEPLUG"
cp -r "$PLUG" "$SPACEPLUG"
LOOP_BIN="$SPACEPLUG/skills/ship-task/scripts/loop.sh"
mkdir -p "$W/tmp space"
run_loop d22 "CLAUDE_CONFIG_DIR=$W/ccd" "TMPDIR=$W/tmp space" -- --repo "$R" --allowed-tools 'Bash(git:*)' "${COMMON_ARGS[@]}"
LOOP_BIN="$LOOP"
check "D22: 1 周回って終わる(プラグインルートに空白を含む配置)" 0 "$RC"
check "D22 ①: 周の worktree の .claude/reviews/ が周の起動の前にある" yes "$(cat "$REC/reviews-hookcall-a" 2>/dev/null)"
E="$REC/env-hookcall-a"
check "D22: 子の環境の DEV_WORKFLOW_LOOP_WORKTREE は周の worktree の物理パス" "$(cat "$REC/cwd-hookcall-a" 2>/dev/null)" "$(sed -n 's/^DEV_WORKFLOW_LOOP_WORKTREE=//p' "$E")"
has "D22: 子の環境の DEV_WORKFLOW_LOOP_PERMLOG" "$E" "DEV_WORKFLOW_LOOP_PERMLOG=$W/state/d22/"
has "D22: 子の環境の DEV_WORKFLOW_LOOP_PLUGIN_ROOT は現在のコピー" "$E" "DEV_WORKFLOW_LOOP_PLUGIN_ROOT=$W/tmp space/"
f "D22: 子の環境に DEV_WORKFLOW_LOOP_TASKMD は無い(task MD の特別許可は無い)" grep -q '^DEV_WORKFLOW_LOOP_TASKMD=' "$E"
has "D22: 許可リストに --allowed-tools の Bash の規則(接頭辞)" "$E" '{"kind": "prefix", "words": ["git"]}'
has "D22: 許可リストに利用者の設定の Bash の規則(完全一致)" "$E" '{"kind": "exact", "words": ["make", "test"]}'
hasnt "D22: 途中の * の規則は使わない" "$E" '"main"'
has "D22: 使わない規則を報告に出す" "$(report_of "$OUT")" "Bash(git * main)"
HO="$REC/hookout-hookcall-a"
check "D22 ③: hook のコマンドは空白を含むプラグインルートでも 1 つの実行ファイルになる(3 回とも判定を返す)" 3 "$(grep -c '"hookEventName": "PermissionRequest"' "$HO" 2>/dev/null)"
check "D22 ③: W の中への Write を許す" allow "$(sed -n 1p "$HO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["decision"]["behavior"])' 2>/dev/null)"
check "D22 ③: .claude/settings.json への Write を拒否する" deny "$(sed -n 3p "$HO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["decision"]["behavior"])' 2>/dev/null)"
check "D22 ③: 利用者の設定だけで許したコマンドの W へのリダイレクトを許す" allow "$(sed -n 5p "$HO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["decision"]["behavior"])' 2>/dev/null)"
has "D22: 朝の報告に周ごとの deny を写す" "$(report_of "$OUT")" "deny: Write [protected]"
# .claude が symlink なら終了コード 30
newrepo d22sym
mkdir -p "$W/d22sym-outside"
ln -s "$W/d22sym-outside" "$R/.claude"
addtask pr-a
commit
run_loop d22sym -- --repo "$R" --dry-run
check "D22 ①: 周の worktree の .claude が symlink なら 30" 30 "$RC"
has "D22 ①: 理由" "$OUT" "[reviews-dir]"
f "D22 ①: symlink の先に reviews を作らない" test -e "$W/d22sym-outside/reviews"
# --dry-run は子の環境の 4 つも出す(d22 のリポジトリで打つ)
R="$W/repos/d22"
run_loop d22 "CLAUDE_CONFIG_DIR=$W/ccd" -- --repo "$R" --dry-run
check "--dry-run(子の環境の表示): 終了コード 0" 0 "$RC"
for v in WORKTREE PERMLOG PLUGIN_ROOT ALLOW; do has "--dry-run: DEV_WORKFLOW_LOOP_$v を出す" "$OUT" "DEV_WORKFLOW_LOOP_$v="; done
hasnt "--dry-run: DEV_WORKFLOW_LOOP_TASKMD は出さない" "$OUT" "DEV_WORKFLOW_LOOP_TASKMD="
# 許可リスト: 「すべて」は Bash だけ。Bash()・Bash( )・Bash(*) は使わない形として報告に出す
run_loop d22 -- --repo "$R" --dry-run --allowed-tools 'Bash()' --allowed-tools 'Bash( )' --allowed-tools 'Bash(*)'
check "許可リスト: Bash()・Bash( )・Bash(*) のとき起動する" 0 "$RC"
check "許可リスト: Bash()・Bash( )・Bash(*) は all にならない(許可リストが空)" "  DEV_WORKFLOW_LOOP_ALLOW=[]" "$(grep '^  DEV_WORKFLOW_LOOP_ALLOW=' "$OUT")"
for rule in 'Bash()' 'Bash( )' 'Bash(*)'; do has "許可リスト: $rule は使わない形として出る" "$OUT" "$rule(--allowed-tools)"; done
run_loop d22 -- --repo "$R" --dry-run --allowed-tools Bash
has "許可リスト: Bash だけは all" "$OUT" 'DEV_WORKFLOW_LOOP_ALLOW=[{"kind": "all", "words": []}]'
# task_dir が保護パスの下(.claude/tasks)なら、最初の周の §3 の 3 で exit 20(選定の worktree を消してから)
newrepo d22prot
addtask pr-a - 1 .claude/tasks
rm -rf "$R/docs/tasks"
commit
newrec d22prot
run_loop d22prot -- --repo "$R" "${COMMON_ARGS[@]}"
check "保護パスの下の task_dir(.claude/tasks)で止まる" 20 "$RC"
has "保護パスの下の task_dir: 理由" "$OUT" "[task-dir]"
check "保護パスの下の task_dir: スタブの -p を起動しない" "" "$(calls)"
check "保護パスの下の task_dir: worktree が残らない(git worktree list に出ない)" 1 "$(wt_count)"
check "保護パスの下の task_dir: worktree が残らない(置き場にディレクトリが無い)" "" "$(ls -A "$R.loop" 2>/dev/null)"
# hook を無効にする設定があれば起動時に止まる
mkdir -p "$W/home-dah/.claude" "$W/ccd-dah" "$W/managed1" "$W/managed2/managed-settings.d"
printf '{"disableAllHooks": true}\n' >"$W/home-dah/.claude/settings.json"
printf '{"disableAllHooks": true}\n' >"$W/ccd-dah/settings.json"
printf '{"allowManagedHooksOnly": true}\n' >"$W/managed1/managed-settings.json"
printf '{"disableAllHooks": true}\n' >"$W/managed2/managed-settings.d/10-x.json"
run_loop d22 "HOME=$W/home-dah" -- --repo "$R" --dry-run
check "利用者の設定(~/.claude)の disableAllHooks: true で止まる" 20 "$RC"
has "disableAllHooks: 理由" "$OUT" "[hooks-disabled]"
run_loop d22 "CLAUDE_CONFIG_DIR=$W/ccd-dah" -- --repo "$R" --dry-run
check "利用者の設定(CLAUDE_CONFIG_DIR)の disableAllHooks: true で止まる" 20 "$RC"
run_loop d22 "DEV_WORKFLOW_LOOP_TEST_MANAGED_DIR=$W/managed1" -- --repo "$R" --dry-run
check "管理者設定の allowManagedHooksOnly: true で止まる" 20 "$RC"
run_loop d22 "DEV_WORKFLOW_LOOP_TEST_MANAGED_DIR=$W/managed2" -- --repo "$R" --dry-run
check "管理者設定(managed-settings.d)の disableAllHooks: true で止まる" 20 "$RC"
# D6: protected も欠落も同じ G1 に数える。参考ログで制限を緩めない。
newrepo g1prot
addtask holdg1prot-a 2026-01-01; addtask holdg1prot-b 2026-01-02; addtask pr-c 2026-01-03
commit
newrec g1prot
run_loop g1prot -- --repo "$R" "${COMMON_ARGS[@]}"
check "D6: 正常な保護パス拒否も G1 に数える" 10 "$RC"
check "D6: 正常な保護パス拒否も2回で止まる" "holdg1prot-a holdg1prot-b" "$(calls)"
has "D6: 記録の種類によらず計数" "$(report_of "$OUT")" "種類にかかわらず"
newrepo g1oth
addtask holdg1oth-a 2026-01-01; addtask holdg1-b 2026-01-02; addtask pr-c 2026-01-03
commit
newrec g1oth
run_loop g1oth -- --repo "$R" "${COMMON_ARGS[@]}"
check "D6: 保護パス以外の拒否の G1 は数える(2 回で止まる)" 10 "$RC"
has "D6: 欠落は参考記録の読取不能として報告" "$(report_of "$OUT")" "参考記録を読めない"
fi

# ════════════════ 実行と実行の間の変化 ════════════════
if want between; then
newrepo between
addtask pr-a 2026-01-01; addtask pr-b 2026-01-02
commit
newrec between
run_loop between -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
check "実行の間の変化(準備): 1 周目" 0 "$RC"
G -C "$R" config selftest.between yes
run_loop between -- --repo "$R" "${COMMON_ARGS[@]}"
check "実行と実行の間の変化: 止まらずに続ける" 0 "$RC"
has "実行と実行の間の変化: 値を出さずdigest差分を報告する" "$(report_of "$OUT")" "config の内容が変わった(sha256:"
hasnt "実行と実行の間の変化: 設定値を出さない" "$(report_of "$OUT")" "selftest.between=yes"
has "実行と実行の間の変化: 次のタスクを回す" "$REC/calls.log" "pr-b"

fi

# ════════════════ 発見モード(--discover。loop.md §11・Issue #69)════════════════
if want discover; then
# 発見元の列が discover-mode.md §1 の「発見元の列」の行と同じ(順も)
DM_DOC="$PLUGIN_SRC/skills/ship-task/references/discover-mode.md"
DISC_DIFF="$(python3 -B - "$DM_DOC" "$TARGET" <<'PY' 2>&1
import re, sys
try:
    doc = open(sys.argv[1], encoding="utf-8").read()
except OSError as exc:
    print("(discover-mode.md を読めない: %s)" % exc)
    sys.exit(0)
rows = [l for l in doc.split("\n") if l.startswith("- **発見元の列**: ")]
if len(rows) != 1:
    print("(文書の「発見元の列」の行がちょうど 1 つでない: %d)" % len(rows))
    sys.exit(0)
doc_list = re.findall(r"`([^`]+)`", rows[0])
found = re.findall(r"^DISCOVER_DEFAULT=\(([^)]*)\)", open(sys.argv[2], encoding="utf-8").read(), re.M)
if len(found) != 1:
    print("(loop.sh の DISCOVER_DEFAULT の行がちょうど 1 つでない: %d)" % len(found))
    sys.exit(0)
loop_list = found[0].split()
if not doc_list or doc_list != loop_list:
    print("文書: %s / loop.sh: %s" % (" ".join(doc_list), " ".join(loop_list)))
PY
)"
if [ -z "$DISC_DIFF" ]; then ok "同期: 発見元の列が discover-mode.md と一致する(順も)"
else ng "同期: 発見元の列が discover-mode.md と一致する(順も)($DISC_DIFF)"; fi

# newdisc <名> [noorigin] → R・B。状態ファイル 3 つを ignore し、task_dir(docs/tasks)に既存の 候補_old.md を置く
newdisc() {
  newrepo "$1" "${2:-}"
  printf '.claude/grasp.md\n.claude/.understand-project-done\n' >>"$R/.gitignore"
  mkdir -p "$R/docs/tasks"
  printf '# old\n\n> **ステータス**: 候補\n> **発見元**: refactor\n> **指摘キー**: refactor:file:old.py\n\n## 指摘\n\nold\n' \
    >"$R/docs/tasks/候補_old.md"
  commit
}
dqueue() { # 直前の --dry-run の出力の「発見元の列」(空白区切り)
  sed -n '/^発見元の列/,/^止まった理由/p' "$OUT" | sed -n 's/^  \([a-z-]*\)(ブランチ .*/\1/p' | tr '\n' ' '
}
dargv_has_seq() { # $1=argv の記録 $2..=連続するトークン
  python3 - "$@" <<'PY'
import sys
toks = open(sys.argv[1], encoding="utf-8").read().split("\n")[1:]
want = sys.argv[2:]
sys.exit(0 if any(toks[i:i + len(want)] == want for i in range(len(toks))) else 1)
PY
}

# ── 引数(使い方の誤りは exit 2)・--dry-run・プロンプト ──
newdisc dargs
newrec dargs
for a in "--discover=" "--discover=data-audit," "--discover=,refactor" "--discover=data-audit,data-audit" \
         "--discover=foo" "--discover=Data-audit"; do
  run_loop dargs -- --repo "$R" --dry-run "$a"
  check "発見モードの引数の誤り('$a'): 終了コード 2" 2 "$RC"
  has "発見モードの引数の誤り('$a'): 理由" "$OUT" "[usage]"
done
run_loop dargs -- --repo "$R" --dry-run --discover --discover=refactor
check "発見モードの引数の誤り(--discover を 2 回): 終了コード 2" 2 "$RC"
run_loop dargs -- --repo "$R" --dry-run --discover --only pr-a
check "発見モードの引数の誤り(--only と併用): 終了コード 2" 2 "$RC"
run_loop dargs -- --repo "$R" --dry-run --discover data-audit
check "発見モードの引数の誤り(値を = で付けない): 終了コード 2" 2 "$RC"
has "発見モードの引数の誤り(値を = で付けない): 不明な引数" "$OUT" "不明な引数: data-audit"
D12="$(git -C "$R" rev-parse main | cut -c1-12)"
run_loop dargs -- --repo "$R" --dry-run --discover
check "発見モードの --dry-run: 終了コード 0" 0 "$RC"
check "発見モードの --dry-run: -p を起動しない" "" "$(calls)"
check "発見モードの --dry-run: worktree が残らない" 1 "$(wt_count)"
check "発見モードの --dry-run: 既定の列を既定の順に出す" "data-audit refactor " "$(dqueue)"
has "発見モードの --dry-run: 今夜の名のブランチを出す" "$OUT" "data-audit(ブランチ task/候補-data-audit-$D12)"
has "発見モードの --dry-run: task_dir を出す" "$OUT" "発見元の列(task_dir: docs/tasks)"
hasnt "発見モードの --dry-run: 読み飛ばしが無い" "$OUT" "読み飛ばし:"
has "発見モード: 報告にモードの行(既定)" "$(report_of "$OUT")" "モード: 発見(発見元の列: data-audit,refactor・既定"
has "発見モード: 報告に origin の URL の行" "$(report_of "$OUT")" "- origin の URL: 検査に通った(fetch と push が同じリポジトリ)"
run_loop dargs -- --repo "$R" --dry-run --discover=refactor,data-audit
check "発見モードの --dry-run: 引数の列を引数の順に出す" "refactor data-audit " "$(dqueue)"
has "発見モード: 報告にモードの行(引数)" "$(report_of "$OUT")" "モード: 発見(発見元の列: refactor,data-audit・引数"
run_loop dargs -- --repo "$R" --dry-run --discover=refactor
check "発見モードの --dry-run: 引数で 1 つに絞る" "refactor " "$(dqueue)"

# ── 起動の前提: origin の URL(fetch と push・vcs・origin-repo.py の失敗)・兄弟のスクリプト・状態ファイルの ignore ──
# origin の URL の検査と兄弟のスクリプトの検査は、両方のモードで行う(loop.md §2)
newdisc dmis
G -C "$R" remote set-url --push origin "sshstub:$W/repos/elsewhere-secret.git"
newrec dmis
for m in 発見 実装; do
  if [ "$m" = 発見 ]; then run_loop dmis -- --repo "$R" --dry-run --discover; else run_loop dmis -- --repo "$R" --dry-run; fi
  check "${m}モード: fetch と push の URL が別のリポジトリなら 20" 20 "$RC"
  has "${m}モード: fetch と push の URL が別: 理由" "$OUT" "[origin-url-mismatch]"
  hasnt "${m}モード: fetch と push の URL が別: URL の字面を出さない(stdout・stderr)" "$OUT" "elsewhere-secret"
  hasnt "${m}モード: fetch と push の URL が別: URL の字面を出さない(報告)" "$(report_of "$OUT")" "elsewhere-secret"
  check "${m}モード: fetch と push の URL が別: worktree を作らない" 1 "$(wt_count)"
done
# remote.origin.vcs: 起動時の最初の ls-remote より前に origin-vcs で止まる(vcs のヘルパーを起動しない — M4)
printf '#!/bin/sh\necho "$0 $*" >>"${SELFTEST_REC:?}/vcs.log"\nexit 1\n' >"$STUBBIN/git-remote-selftestvcs"
chmod +x "$STUBBIN/git-remote-selftestvcs"
newdisc dvcs
G -C "$R" config remote.origin.vcs selftestvcs
for m in 発見 実装; do
  newrec "dvcs-$m"
  if [ "$m" = 発見 ]; then run_loop dvcs -- --repo "$R" --dry-run --discover; else run_loop dvcs -- --repo "$R" --dry-run; fi
  check "${m}モード: remote.origin.vcs があれば 20" 20 "$RC"
  has "${m}モード: remote.origin.vcs: 理由は origin-vcs" "$OUT" "[origin-vcs]"
  f "${m}モード: remote.origin.vcs: ls-remote を打たない(vcs のヘルパーが起動されない)" test -e "$REC/vcs.log"
done
# 陽性対照: 同じ構成・loop.sh と同じ環境で ls-remote を直接打つと、vcs のヘルパーが起動される(上の「起動されない」が
# スタブに届かないことによる空振りでない)
newrec dvcs-対照
build_env dvcs
( cd "$W/cwd" && exec env -i "${ENV_ARGS[@]}" git -C "$R" ls-remote origin ) >/dev/null 2>&1
t "前提: remote.origin.vcs の構成で ls-remote を打つと vcs のヘルパーが起動される(陽性対照)" test -e "$REC/vcs.log"
# origin-repo.py の失敗(exit 2・例外・読めない出力)と、兄弟の origin-repo.py・git-config-digest.py が無いとき(plugin-root)
newdisc dor
newrec dor
or_variant() { # $1=名 $2=兄弟のスクリプトの中身(空なら消す)[$3=ファイル名。既定は origin-repo.py]→ LOOP_BIN
  local d="$W/plugin-or-$1" fn="${3:-origin-repo.py}"
  rm -rf "$d"
  mkdir -p "$d"
  cp -r "$PLUG/." "$d/"
  if [ -z "$2" ]; then rm -f "$d/skills/ship-task/scripts/$fn"
  else printf '%s\n' "$2" >"$d/skills/ship-task/scripts/$fn"; fi
  LOOP_BIN="$d/skills/ship-task/scripts/loop.sh"
}
or_run() { # $1=モード(発見|実装)
  if [ "$1" = 発見 ]; then run_loop dor -- --repo "$R" --dry-run --discover; else run_loop dor -- --repo "$R" --dry-run; fi
}
for m in 発見 実装; do
  or_variant exit2 'import sys; sys.exit(2)'
  or_run "$m"
  check "${m}モード: origin-repo.py の exit 2 で 20" 20 "$RC"
  has "${m}モード: origin-repo.py の exit 2: 理由" "$OUT" "[origin-url]"
  or_variant raise 'raise RuntimeError("selftest")'
  or_run "$m"
  check "${m}モード: origin-repo.py の例外(exit 1)で 20" 20 "$RC"
  has "${m}モード: origin-repo.py の例外: 理由" "$OUT" "[origin-url]"
  or_variant badjson 'print("not json")'
  or_run "$m"
  check "${m}モード: origin-repo.py の出力が読めなければ 20" 20 "$RC"
  has "${m}モード: origin-repo.py の出力が読めない: 理由" "$OUT" "[origin-url]"
  or_variant missing ''
  or_run "$m"
  check "${m}モード: origin-repo.py が無ければ 20" 20 "$RC"
  has "${m}モード: origin-repo.py が無い: 理由" "$OUT" "[plugin-root]"
  has "${m}モード: origin-repo.py が無い: 名を出す" "$OUT" "origin-repo.py が無い"
  or_variant gcdmissing '' git-config-digest.py
  or_run "$m"
  check "${m}モード: git-config-digest.py が無ければ 20" 20 "$RC"
  has "${m}モード: git-config-digest.py が無い: 理由" "$OUT" "[plugin-root]"
  has "${m}モード: git-config-digest.py が無い: 名を出す" "$OUT" "git-config-digest.py が無い"
done
LOOP_BIN="$LOOP"
# 起動時の報告の origin の URL の行(実装モード。origin がある構成と無い構成 — loop.md §7)
newrepo orep
addtask pr-a
commit
newrec orep
run_loop orep -- --repo "$R" --dry-run
check "実装モード: origin がある構成で起動する" 0 "$RC"
has "実装モード: 報告に origin の URL の行(origin がある)" "$(report_of "$OUT")" "- origin の URL: 検査に通った(fetch と push が同じリポジトリ)"
newrepo orepn noorigin
addtask pr-a
commit
newrec orepn
run_loop orepn -- --repo "$R" --dry-run
check "実装モード: origin が無い構成で起動する" 0 "$RC"
has "実装モード: 報告に origin の URL の行(origin が無い)" "$(report_of "$OUT")" "- origin の URL: origin が無い(周は push しないので、結末は 縮退)"
# 状態ファイルが ignore されていない / .claude/grasp.md が追跡済み(worktree を消して 20)
newrepo dign
printf '*.ignored\n.claude/reviews\n' >"$R/.gitignore"   # .claude/grasp.md・.claude/.understand-project-done を ignore しない土台
commit
newrec dign
run_loop dign -- --repo "$R" --dry-run --discover
check "発見モード: 状態ファイルが ignore されていなければ 20" 20 "$RC"
has "発見モード: 状態ファイルが ignore されていない: 理由" "$OUT" "[state-not-ignored]"
has "発見モード: 状態ファイルが ignore されていない: gitignore の断片を報告に出す" "$(report_of "$OUT")" "init-project の gitignore の断片と同じ"
check "発見モード: 状態ファイルが ignore されていない: worktree を消す" 1 "$(wt_count)"
newdisc dtrk
mkdir -p "$R/.claude"
echo g >"$R/.claude/grasp.md"
G -C "$R" add -f .claude/grasp.md
commit
run_loop dtrk -- --repo "$R" --dry-run --discover
check "発見モード: .claude/grasp.md が追跡済みなら 20(ignore の検査に --no-index を付けない)" 20 "$RC"
has "発見モード: 追跡済みの grasp.md: 理由" "$OUT" "[state-not-ignored]"
has "発見モード: 追跡済みの grasp.md: git rm --cached を案内する" "$(report_of "$OUT")" "git rm --cached -- .claude/grasp.md"
check "発見モード: 追跡済みの grasp.md: worktree を消す" 1 "$(wt_count)"
# 状態ファイルの前提(loop.md §3 の 2a)は実装モードでも同じ(worktree を消して 20)
newrepo stign
printf '*.ignored\n.claude/reviews\n' >"$R/.gitignore"   # .claude/grasp.md・.claude/.understand-project-done を ignore しない土台
addtask pr-a
commit
newrec stign
run_loop stign -- --repo "$R" --dry-run
check "状態ファイルの前提(実装モード): 状態ファイルが ignore されていなければ 20" 20 "$RC"
has "状態ファイルの前提(実装モード): 状態ファイルが ignore されていない: 理由" "$OUT" "[state-not-ignored]"
has "状態ファイルの前提(実装モード): 状態ファイルが ignore されていない: gitignore の断片を報告に出す" "$(report_of "$OUT")" "init-project の gitignore の断片と同じ"
check "状態ファイルの前提(実装モード): 状態ファイルが ignore されていない: worktree を消す" 1 "$(wt_count)"
newrepo sttrk
addtask pr-a
mkdir -p "$R/.claude"
echo g >"$R/.claude/grasp.md"
G -C "$R" add -f .claude/grasp.md
commit
newrec sttrk
run_loop sttrk -- --repo "$R" --dry-run
check "状態ファイルの前提(実装モード): .claude/grasp.md が追跡済みなら 20" 20 "$RC"
has "状態ファイルの前提(実装モード): 追跡済みの grasp.md: git rm --cached を案内する" "$(report_of "$OUT")" "git rm --cached -- .claude/grasp.md"
hasnt "状態ファイルの前提(実装モード): 追跡済みの grasp.md: ignore 済みの .claude/.understand-project-done を外れの一覧に出さない" "$(report_of "$OUT")" "- .claude/.understand-project-done"
newrepo stok
addtask pr-a
commit
newrec stok
run_loop stok -- --repo "$R" --dry-run
check "状態ファイルの前提(実装モード): 3 つとも ignore されていれば起動する" 0 "$RC"
# check-ignore が判定できないもの(rc が 0 と 1 以外: symlink の先・サブモジュールの中)は数えず、後の段に任せる
newdisc stsym
mkdir -p "$W/stsym-outside"
ln -s "$W/stsym-outside" "$R/.claude"
commit
newrec stsym
run_loop stsym -- --repo "$R" --dry-run --discover
check "状態ファイルの前提(判定できない): 発見モードで .claude が symlink なら 2b の 30" 30 "$RC"
has "状態ファイルの前提(判定できない): 発見モードで .claude が symlink: 理由" "$OUT" "[reviews-dir]"
newrepo strsym
addtask pr-a
mkdir -p "$R/.claude" "$W/strsym-outside"
ln -s "$W/strsym-outside" "$R/.claude/reviews"
G -C "$R" add -f .claude/reviews
commit
newrec strsym
run_loop strsym -- --repo "$R" --dry-run
check "状態ファイルの前提(判定できない): 実装モードで .claude/reviews が symlink なら 2b の 30" 30 "$RC"
has "状態ファイルの前提(判定できない): 実装モードで .claude/reviews が symlink: 理由" "$OUT" "[reviews-dir]"
newrepo stprio
printf '*.ignored\n.claude/reviews\n.claude/.understand-project-done\n' >"$R/.gitignore"   # .claude/grasp.md を ignore しない土台
addtask pr-a
mkdir -p "$R/.claude" "$W/stprio-outside"
ln -s "$W/stprio-outside" "$R/.claude/reviews"
G -C "$R" add -f .claude/reviews
commit
newrec stprio
run_loop stprio -- --repo "$R" --dry-run
check "状態ファイルの前提(判定できない): .claude/reviews が symlink でも、ほかの状態ファイルが外れていれば 20(2a は 2b より先)" 20 "$RC"
has "状態ファイルの前提(判定できない): .claude/reviews が symlink で grasp.md が外れる: 理由" "$OUT" "[state-not-ignored]"
newrepo stsub
addtask pr-a
mkdir -p "$R/.claude"
G -C "$R" update-index --add --cacheinfo "160000,$(git -C "$R" rev-parse HEAD),.claude"   # .claude を gitlink にする
commit
newrec stsub
run_loop stsub -- --repo "$R" --dry-run
check "状態ファイルの前提(判定できない): 実装モードで .claude がサブモジュールなら起動する" 0 "$RC"

# ── 読み飛ばし(--dry-run の発見元の列と読み飛ばしで見る)──
newdisc dskip
newrec dskip
DEF="$(git -C "$R" rev-parse main)"
D12="${DEF:0:12}"
INIT="$(git -C "$R" rev-list --max-parents=0 main)"                # 祖先(merge 済み)
SIDE="$(git -C "$R" commit-tree 'main^{tree}' -p main -m side)"   # 祖先でない(未 merge)
dskip_run() { run_loop dskip -- --repo "$R" --dry-run --discover; }
G -C "$R" branch "task/候補-data-audit-aaaaaaaaaaaa" "$SIDE"
dskip_run
check "読み飛ばし(発見): ローカルの未 merge の候補のブランチ" "refactor " "$(dqueue)"
has "読み飛ばし(発見): ローカルの未 merge: 理由" "$OUT" "data-audit: 未 merge の候補のブランチがある(refs/heads/task/候補-data-audit-aaaaaaaaaaaa)"
has "読み飛ばし(発見): ローカルの未 merge: 片付けの定型" "$OUT" "git branch -D task/候補-data-audit-aaaaaaaaaaaa"
G -C "$R" branch -D "task/候補-data-audit-aaaaaaaaaaaa"
G -C "$R" update-ref "refs/remotes/origin/task/候補-refactor-bbbbbbbbbbbb" "$SIDE"
dskip_run
check "読み飛ばし(発見): 追跡用の ref の未 merge の候補のブランチ" "data-audit " "$(dqueue)"
has "読み飛ばし(発見): 追跡用の ref の未 merge: 片付けの定型" "$OUT" "git branch -dr origin/task/候補-refactor-bbbbbbbbbbbb"
G -C "$R" update-ref -d "refs/remotes/origin/task/候補-refactor-bbbbbbbbbbbb"
ORPH="$(git -C "$B" commit-tree 'main^{tree}' -m only-in-origin)"
git -C "$B" update-ref "refs/heads/task/候補-data-audit-cccccccccccc" "$ORPH"
f "前提: origin にだけある候補のブランチの sha はローカルに無い" git -C "$R" cat-file -e "$ORPH^{commit}"
dskip_run
check "読み飛ばし(発見): origin の未 merge(sha がローカルに無い)の候補のブランチ" "refactor " "$(dqueue)"
has "読み飛ばし(発見): origin の未 merge: 片付けの定型" "$OUT" "git push origin --delete task/候補-data-audit-cccccccccccc"
git -C "$B" update-ref -d "refs/heads/task/候補-data-audit-cccccccccccc"
G -C "$R" branch "task/候補-data-audit-dddddddddddd" "$INIT"
G -C "$R" update-ref "refs/remotes/origin/task/候補-refactor-dddddddddddd" "$INIT"
git -C "$B" update-ref "refs/heads/task/候補-refactor-ffffffffffff" "$INIT"
dskip_run
check "読み飛ばし(発見): merge 済み(祖先)の候補のブランチは拾う" "data-audit refactor " "$(dqueue)"
G -C "$R" branch -D "task/候補-data-audit-dddddddddddd"
G -C "$R" update-ref -d "refs/remotes/origin/task/候補-refactor-dddddddddddd"
git -C "$B" update-ref -d "refs/heads/task/候補-refactor-ffffffffffff"
G -C "$R" branch "task/候補-data-audit-$D12" "$DEF"
git -C "$B" update-ref "refs/heads/task/候補-refactor-$D12" "$DEF"
dskip_run
check "読み飛ばし(発見): 今夜の名のブランチは祖先でも読み飛ばす(ローカル・origin)" "" "$(dqueue)"
has "読み飛ばし(発見): 今夜の名(ローカル): 理由" "$OUT" "data-audit: 今夜の名のブランチ task/候補-data-audit-$D12 がある(refs/heads/task/候補-data-audit-$D12)"
has "読み飛ばし(発見): 今夜の名(origin): 理由" "$OUT" "refactor: 今夜の名のブランチ task/候補-refactor-$D12 がある(origin:refs/heads/task/候補-refactor-$D12)"
has "読み飛ばし(発見): 今夜の名: 片付けの定型" "$OUT" "git push origin --delete task/候補-refactor-$D12"
has "読み飛ばし(発見): 報告にも片付けの定型" "$(report_of "$OUT")" "片付け: \`git branch -D task/候補-data-audit-$D12\`"
G -C "$R" branch -D "task/候補-data-audit-$D12"
git -C "$B" update-ref -d "refs/heads/task/候補-refactor-$D12"
for n in "task/候補-data-audit-eeeeeeeeeeee0" "task/候補-data-audit-eeeee" "task/候補-data-audit-EEEEEEEEEEEE" \
         "task/候補-data-audit-x-eeeeeeeeeeee" "task/x候補-data-audit-eeeeeeeeeeee" "task/候補-refactor-eeeeeeeeeeee-x"; do
  G -C "$R" branch "$n" "$SIDE"
done
dskip_run
check "読み飛ばし(発見): 名が完全一致しない候補のブランチは数えない" "data-audit refactor " "$(dqueue)"
for n in "task/候補-data-audit-eeeeeeeeeeee0" "task/候補-data-audit-eeeee" "task/候補-data-audit-EEEEEEEEEEEE" \
         "task/候補-data-audit-x-eeeeeeeeeeee" "task/x候補-data-audit-eeeeeeeeeeee" "task/候補-refactor-eeeeeeeeeeee-x"; do
  G -C "$R" branch -D "$n"
done
G -C "$R" worktree add -q --detach --lock --reason "dev-workflow-loop: 候補:refactor" "$W/repos/dskip-lock" HEAD
G -C "$R" worktree add -q --detach --lock --reason "dev-workflow-loop: 候補:data-audit-x" "$W/repos/dskip-lock2" HEAD
dskip_run
check "読み飛ばし(発見): lock の理由がちょうど 候補:<発見元> の worktree" "data-audit " "$(dqueue)"
has "読み飛ばし(発見): lock: 理由" "$OUT" "refactor: 前の周が残した worktree がある($W/repos/dskip-lock)"

# ── 判定の各場合と worktree の扱い(1 回の実行で data-audit・refactor の 2 周)──
DA_ARGS=(--max-consecutive-failures 50 "${COMMON_ARGS[@]}")
disc_pair() { # $1=名 $2=data-audit の振る舞い $3=refactor の振る舞い [$4=noorigin] 残り=loop.sh の追加の引数
  local name="$1" da="$2" rf="$3" org="${4:-}"
  shift 4 2>/dev/null || shift $#
  newdisc "dp-$name" "$org"
  newrec "dp-$name"
  D12="$(git -C "$R" rev-parse main | cut -c1-12)"
  run_loop "dp-$name" "SELFTEST_DISC_DA=$da" "SELFTEST_DISC_RF=$rf" -- --repo "$R" --discover "${DA_ARGS[@]}" "$@"
  RP="$(report_of "$OUT")"
}
djudged() { # $1=発見元 $2=判定(先頭一致) $3=removed|kept $4=振る舞い
  has "発見の判定($4): $2" "$OUT" "発見元 $1 → $2"
  if [ "$3" = kept ]; then
    t "発見の判定($4): worktree を残す(lock の理由 候補:$1)" locked_reason_of "候補:$1"
  else
    f "発見の判定($4): worktree を消す" locked_reason_of "候補:$1"
  fi
}
# H18 の共有 ref 防御は、候補の形を調べる判定器より先に働く。
# 異なる branch / remote-tracking OID は後続の発見元へ進めず停止し、証拠を残す。
dshared() { # $1=停止した発見元 $2=振る舞い $3=起動済みの発見元列
  check "発見の共有状態($2): 10 で止まる" 10 "$RC"
  has "発見の共有状態($2): 理由" "$OUT" "[shared-state]"
  has "発見の共有状態($2): 差分を記録" "$RP" "差分:"
  t "発見の共有状態($2): 当該 worktree を保持" locked_reason_of "候補:$1"
  check "発見の共有状態($2): 次の発見元を起動しない" "$3" "$(calls)"
  t "発見の共有状態($2): 止めの印を保持" test -f "$(dirname "$(dirname "$RP")")/stop-mark.md"
}
disc_pair pr pr degrade
check "発見の周(pr・degrade): キューが空で終わる" 0 "$RC"
check "発見の周: 発見元を列の順に 1 回ずつ回す" "disc-data-audit disc-refactor" "$(calls)"
djudged data-audit "正常(PR)" removed pr
djudged refactor "正常(縮退)" removed degrade
t "発見の周(pr): origin に今夜の名のブランチがある" git -C "$B" rev-parse --verify -q "refs/heads/task/候補-data-audit-$D12"
check "発見の周: 共有の config に branch.task/*(今夜の名のブランチ)が無い" "" "$(git config --file "$R/.git/config" --get-regexp '^branch\.task/' 2>/dev/null)"
has "発見の周: 候補の件数を報告に出す" "$RP" "- 候補: 2 件"
has "発見の周: 候補のパスを報告に出す" "$RP" "  - docs/tasks/候補_data-audit-a.md"
has "発見の周: 今夜の名のブランチを報告に出す" "$RP" "今夜の名のブランチ: task/候補-data-audit-$D12"
has "発見の周(縮退・push していない): ローカルのブランチの手順" "$RP" "push していないとき(origin に task/候補-refactor-$D12 が無い)"
has "発見の周(縮退・push していない): 消し方" "$RP" "git branch -D task/候補-refactor-$D12"
has "発見の周(縮退): 処理するまでその発見元は回らない" "$RP" "処理するまで、発見元 refactor は回らない"
check "発見の周: プロンプトの字面" "/dev-workflow:ship-task --discover=data-audit --unattended" "$(cat "$REC/prompt-disc-data-audit" 2>/dev/null)"
t "発見の周: argv の隔離・権限のフラグは実装モードと同じ" dargv_has_seq "$REC/argv-disc-data-audit" -p --output-format json \
  --setting-sources user --strict-mcp-config --plugin-dir "$(sed -n '/^--plugin-dir$/{n;p;}' "$REC/argv-disc-data-audit")" --permission-mode acceptEdits --permission-prompts none --settings
f "発見の周: プロンプトを位置引数で渡さない" grep -qF -- "--discover=" "$REC/argv-disc-data-audit"
has "発見の周: 子の環境に周の印" "$REC/env-disc-data-audit" "DEV_WORKFLOW_LOOP_ITER="
has "発見の周: 要約" "$RP" "## 発見元ごとの要約"
has "発見の周: 採用の手順" "$RP" "/create-task <候補_ のパス>"
has "発見の周: 片付けの定型(squash・rebase)" "$RP" "squash・rebase で merge したとき"
has "発見の周: 片付けの定型(閉じた)" "$RP" "PR を閉じたとき"
disc_pair degpush degradepush none
djudged data-audit "正常(縮退)" removed degradepush
djudged refactor "正常(候補なし)" removed none
has "発見の周(縮退・push 済み): push 済みのブランチ" "$RP" "push 済みのブランチ: task/候補-data-audit-$D12"
has "発見の周(縮退・push 済み): PR を作るコマンドの雛形" "$RP" "gh pr create -R <HOST/OWNER/REPO> --head task/候補-data-audit-$D12"
has "発見の周(縮退・push 済み): 作らないときの消し方" "$RP" "git push origin --delete task/候補-data-audit-$D12"
has "発見の周(縮退・push 済み): 処理するまでその発見元は回らない" "$RP" "処理するまで、発見元 data-audit は回らない"
f "発見の周(候補なし): 今夜の名のブランチを作らない" git -C "$R" rev-parse --verify -q "refs/heads/task/候補-refactor-$D12"
has "発見の周(候補なし): 候補モードの報告の写し先を出す" "$RP" "候補モードの報告: "
disc_pair noorg degrade pr noorigin
djudged data-audit "正常(縮退)" removed "degrade・origin 無し"
djudged refactor "失敗(食い違い: 結末 PR だが origin が無い)" kept "pr・origin 無し"
has "発見の周(縮退・origin 無し): ローカルの手順" "$RP" "origin が無く、push していない: ローカルのブランチ task/候補-data-audit-$D12 を merge するか、消す"
has "発見の周(縮退・origin 無し): merge の手順" "$RP" "git merge task/候補-data-audit-$D12"
hasnt "発見の周(縮退・origin 無し): PR の雛形を出さない" "$RP" "gh pr create -R"
disc_pair inprog inprog modcand
djudged data-audit "失敗(食い違い: 名前が 候補_<名>.md でない(docs/tasks/進行中_data-audit-a.md)" kept "進行中_ を足す"
djudged refactor "失敗(食い違い: 追加でない変更がある(M docs/tasks/候補_old.md)" kept "既存の 候補_ を書き換える"
disc_pair nested nested outside
djudged data-audit "失敗(食い違い: task_dir の直下でない(docs/tasks/sub/候補_data-audit-a.md)" kept "入れ子"
djudged refactor "失敗(食い違い: 名前が 候補_<名>.md でない(docs/tasks/memo-refactor.md)" kept "名前空間の外"
disc_pair modify modify d21
djudged data-audit "失敗(食い違い: 追加でない変更がある(M README.md)" kept "既存ファイルの書き換え"
djudged refactor "失敗(食い違い: D21 に外れる" kept "D21 に外れる名"
disc_pair name emptyname mode
djudged data-audit "失敗(食い違い: <名> が空" kept "名が空"
djudged refactor "失敗(食い違い: モードが 100644 でない(100755" kept "モード"
disc_pair commits twocommits untracked
djudged data-audit "失敗(食い違い: HEAD が固定した sha の上の 1 commit でない" kept "2 commit"
djudged refactor "失敗(食い違い: 作業ツリーに未追跡か未 commit が残った" kept "未追跡の残り"
disc_pair branch otherbranch fail
dshared data-audit "今夜の名でないブランチ" "disc-data-audit"
# 前の共有状態違反では未起動になる後段も、独立した正常共有状態で判定する。
disc_pair fail none fail
djudged data-audit "正常(候補なし)" removed "失敗扱いの前の正常な周"
djudged refactor "失敗(結末 失敗扱い)" kept "失敗扱い"
has "失敗の周(origin に今夜の名が無い): 報告" "$RP" "origin に task/候補-refactor-$D12 は無い"
disc_pair hold hold holdlast
djudged data-audit "失敗(結末 保留 は発見の周では取りえない)" kept "保留"
djudged refactor "失敗(結末 保留 は発見の周では取りえない)" kept "前に 候補なし・最後に 保留"
has "結末の行の選び方(発見): 最後の行を読む" "$RP" "- 結末: 保留 — 最後"
disc_pair late late prunpushed
djudged data-audit "正常(縮退)" kept "判定の後に未追跡が書かれた"
has "発見の周(remove の拒否): 報告" "$RP" "消せなかったので残した"
djudged refactor "失敗(食い違い: origin の task/候補-refactor-$D12(無い)" kept "PR だが push していない"
disc_pair pushdel pushdel pushfail
dshared data-audit "push 後に今夜の名のローカル branch を削除" "disc-data-audit"
t "共有状態停止(push 後の削除): remote の証拠を保持" git -C "$B" rev-parse --verify -q "refs/heads/task/候補-data-audit-$D12"
disc_pair pushfail none pushfail
djudged data-audit "正常(候補なし)" removed "push 後の失敗扱いの前の正常な周"
djudged refactor "失敗(結末 失敗扱い)" kept "push の後の失敗扱い"
has "失敗の周: PR が開いている可能性(失敗扱い)" "$RP" "origin に task/候補-refactor-$D12 がある: PR が開いている可能性がある。merge せずに閉じ、ブランチを消す"
disc_pair pushdiff pushdiff dashname
dshared data-audit "push した中身と異なる HEAD" "disc-data-audit"
has "発見の共有状態(remote OID): 一致しない理由" "$RP" "自分の remote-tracking ref の期待状態に一致しない"
hasnt "発見の共有状態(remote OID): PR の雛形を出さない" "$RP" "gh pr create -R"
disc_pair dashname none dashname
djudged data-audit "正常(候補なし)" removed "D21 違反の前の正常な周"
djudged refactor "失敗(食い違い: D21 に外れる — <名> が '-' で始まる" kept "名が - で始まる"
disc_pair noneclean noneuntracked noneotherbranch
djudged data-audit "失敗(食い違い: 作業ツリーに未追跡か未 commit が残った" kept "候補なし で未追跡を残す"
dshared refactor "候補なし で別のブランチに居る" "disc-data-audit disc-refactor"
disc_pair nonelocal nonelocal none
dshared data-audit "候補なし で今夜の名をローカルにだけ作る" "disc-data-audit"
disc_pair none none none
djudged data-audit "正常(候補なし)" removed "変更のない候補なし"
djudged refactor "正常(候補なし)" removed "変更のない候補なしの次の周"
disc_pair hang hang none "" --iteration-timeout 4
djudged data-audit "失敗(時間切れ)" kept "時間切れ"
djudged refactor "正常(候補なし)" removed "時間切れの後の周"
for p in $(cat "$REC/pid-disc-data-audit" "$REC/spawned-disc-data-audit" 2>/dev/null); do
  if wait_dead "$p" 3; then ok "発見の周(時間切れ): pid $p が止まる"; else ng "発見の周(時間切れ): pid $p が止まる"; fi
done
# 連続失敗・同じ実行で 2 度回さない・共有の config の変化
newdisc dcf
newrec dcf
run_loop dcf SELFTEST_DISC_DA=fail SELFTEST_DISC_RF=fail -- --repo "$R" --discover "${COMMON_ARGS[@]}"
check "発見の周の連続失敗: 終了コード 10" 10 "$RC"
has "発見の周の連続失敗: 理由" "$OUT" "[consecutive-failures]"
check "発見の周の連続失敗: 2 周で止まる" "disc-data-audit disc-refactor" "$(calls)"
newdisc donce
newrec donce
run_loop donce SELFTEST_DISC_DA=none SELFTEST_DISC_RF=none -- --repo "$R" --discover --max-iterations 10 "${COMMON_ARGS[@]}"
check "発見の周: 同じ実行で同じ発見元を 2 度回さない(キューが空で終わる)" 0 "$RC"
check "発見の周: 同じ実行で同じ発見元を 2 度回さない(各 1 回)" "disc-data-audit disc-refactor" "$(calls)"
has "発見の周: 同じ実行で同じ発見元を 2 度回さない(理由)" "$OUT" "止まった理由: キューが空"
newdisc dcfg
newrec dcfg
run_loop dcfg SELFTEST_DISC_DA=cfgchange SELFTEST_DISC_RF=none -- --repo "$R" --discover "${COMMON_ARGS[@]}"
check "発見の周(共有の config の変化): 終了コード 10" 10 "$RC"
has "発見の周(共有の config の変化): 理由" "$OUT" "[shared-state]"
check "発見の周(共有の config の変化): 次の周へ進まない" "disc-data-audit" "$(calls)"
newdisc dpushu
newrec dpushu
D12="$(git -C "$R" rev-parse main | cut -c1-12)"
run_loop dpushu SELFTEST_DISC_DA=pushu SELFTEST_DISC_RF=none -- --repo "$R" --discover "${COMMON_ARGS[@]}"
check "発見の周(-u つきの push): 終了コード 10" 10 "$RC"
has "発見の周(-u つきの push): 理由" "$OUT" "[shared-state]"
check "発見の周(-u つきの push): 次の周へ進まない" "disc-data-audit" "$(calls)"
has "発見の周(-u つきの push): 構造差分を報告する" "$(report_of "$OUT")" "保持済み config または worktree の構造が周の途中で変わった"
hasnt "発見の周(-u つきの push): 設定値を報告しない" "$(report_of "$OUT")" "branch.task/候補-data-audit-$D12.merge"
# 周の途中の印(meta に mode・source・name)と、push の後の KILL の後の起動(共有の config に書かない push なら続ける)
newdisc dkill
newrec dkill
D12="$(git -C "$R" rev-parse main | cut -c1-12)"
start_bg dkill SELFTEST_DISC_DA=pushsleep SELFTEST_DISC_RF=none -- --repo "$R" --discover "${COMMON_ARGS[@]}"
if wait_file "$REC/started-disc-data-audit" 30; then
  sleep 0.5
  SD="$(state_dir dkill)"
  has "発見の周の途中の印: mode=discover" "$SD/inflight/meta" "mode=discover"
  has "発見の周の途中の印: source=<発見元>" "$SD/inflight/meta" "source=data-audit"
  has "発見の周の途中の印: name=今夜の名" "$SD/inflight/meta" "name=候補-data-audit-$D12"
  has "発見の周の途中の印: rel=候補:<発見元>" "$SD/inflight/meta" "rel=候補:data-audit"
  kill -KILL "$BG_PID"; wait_bg
  CHILD="$(cat "$REC/pid-disc-data-audit")"
  t "発見の周の KILL: loop.sh だけを止めると子が残る(前提)" proc_alive "$CHILD"
  run_loop dkill SELFTEST_DISC_RF=none -- --repo "$R" --discover "${COMMON_ARGS[@]}"
  check "発見の周の KILL の後の起動: 未信頼印で止まる" 20 "$RC"
  has "発見の周の KILL の後の起動: 理由" "$OUT" '[inflight-untrusted]'
  t "発見の周の KILL の後の起動: 印を保全" test -d "$SD/inflight"
  hasnt "発見の周の KILL の後の起動: 他の発見元を回さない" "$REC/calls.log" disc-refactor
  stop_fixture_children

else
  ng "発見の周の KILL: 周が始まらない"; kill -KILL "$BG_PID" 2>/dev/null; wait_bg
fi
# -u つきの push の後の KILL の後の起動(共有の config に今夜の名のブランチの branch.* を書いた周 → 20・止めの印)
newdisc dkillu
newrec dkillu
D12="$(git -C "$R" rev-parse main | cut -c1-12)"
start_bg dkillu SELFTEST_DISC_DA=pushusleep SELFTEST_DISC_RF=none -- --repo "$R" --discover "${COMMON_ARGS[@]}"
if wait_file "$REC/started-disc-data-audit" 30; then
  sleep 0.5
  SD="$(state_dir dkillu)"
  kill -KILL "$BG_PID"; wait_bg
  CHILD="$(cat "$REC/pid-disc-data-audit")"
  t "発見の周の KILL(-u つきの push の後): loop.sh だけを止めると子が残る(前提)" proc_alive "$CHILD"
  run_loop dkillu SELFTEST_DISC_RF=none -- --repo "$R" --discover "${COMMON_ARGS[@]}"
  check "発見の周の KILL(-u つきの push の後): 次の起動の終了コード" 20 "$RC"
  has "発見の周の KILL(-u つきの push の後): 理由" "$OUT" '[inflight-untrusted]'
  t "発見の周の KILL(-u つきの push の後): 印を保全" test -d "$SD/inflight"
  hasnt "発見の周の KILL(-u つきの push の後): 他の発見元を回さない" "$REC/calls.log" disc-refactor
  t "発見の周の KILL(-u つきの push の後): 未確認の子を保全する" proc_alive "$CHILD"
  confirm_rejected_inflight "$SD" "発見の周の KILL(-u つきの push の後)"
  run_loop dkillu SELFTEST_DISC_RF=none -- --repo "$R" --discover "${COMMON_ARGS[@]}"
  check "発見の周の KILL(-u つきの push の後): 人が残存物を確認した後は続く" 0 "$RC"

else
  ng "発見の周の KILL(-u つきの push の後): 周が始まらない"; kill -KILL "$BG_PID" 2>/dev/null; wait_bg
fi

# ── 結末の行の選び方(実装モード)・実装モードが 候補_ を拾わない・候補_ だけのディレクトリの検出 ──
newrepo dimpl
addtask candlast-a
commit
newrec dimpl
run_loop dimpl -- --repo "$R" "${COMMON_ARGS[@]}"
has "結末の行の選び方(実装): 最後の行が 候補なし なら失敗" "$OUT" "docs/tasks/進行中_candlast-a.md → 失敗(結末の行が読めない)"
newrepo dimpl2
addtask pr-a
printf '# c\n\n> **ステータス**: 候補\n> **無人実行**: 可\n> **発見元**: refactor\n> **指摘キー**: refactor:file:c.py\n\n## 指摘\n\nc\n' \
  >"$R/docs/tasks/候補_pr-c.md"
commit
newrec dimpl2
run_loop dimpl2 -- --repo "$R" --dry-run
has "実装モードは 候補_ を拾わない: 進行中_ は一覧に出る" "$OUT" "docs/tasks/進行中_pr-a.md"
hasnt "実装モードは 候補_ を拾わない(メタ行があっても)" "$OUT" "候補_pr-c.md"
run_loop dimpl2 -- --repo "$R" "${COMMON_ARGS[@]}"
check "実装モードは 候補_ を拾わない: 回すのは 進行中_ だけ" "pr-a" "$(calls)"
newrepo dcand
printf '.claude/grasp.md\n.claude/.understand-project-done\n' >>"$R/.gitignore"
mkdir -p "$R/cands"
printf '# c\n\n> **ステータス**: 候補\n> **発見元**: refactor\n> **指摘キー**: refactor:file:c.py\n\n## 指摘\n\nc\n' >"$R/cands/候補_c.md"
commit
newrec dcand
run_loop dcand -- --repo "$R" --dry-run --discover
check "候補_ だけのディレクトリ: 起動する" 0 "$RC"
has "候補_ だけのディレクトリが task_dir に検出される" "$OUT" "発見元の列(task_dir: cands)"

fi

# ════════════════ 人の linked worktree の設定(H20)════════════════
if want humanconfig; then
hc_setup() { # $1=名 $2=最初のタスク $3=初期設定 $4=main|linked [$5=拡張]
  HC_NAME="hc-$1"
  newrepo "$HC_NAME"
  addtask "$2-a" 2026-01-01; addtask pr-b 2026-01-02
  commit
  [ "${5:-on}" = off ] || G -C "$R" config extensions.worktreeConfig true
  HC_REPO="$R"
  if [ "$4" = linked ]; then
    HC_REPO="$W/repos/$HC_NAME human"
    G -C "$R" worktree add -q --detach "$HC_REPO" HEAD
  fi
  HC_CONFIG="$(git -C "$HC_REPO" rev-parse --absolute-git-dir)/config.worktree"
  case "$3" in
    present) git -C "$R" config --file "$HC_CONFIG" selftest.human before ;;
    reorder) printf '[selftest]\n\ta = 1\n\tb = 2\n' >"$HC_CONFIG" ;;
  esac
  newrec "$HC_NAME"
}
hc_change() {
  case "$1" in
    add|value) git -C "$R" config --file "$HC_CONFIG" selftest.human after ;;
    delete) rm -- "$HC_CONFIG" ;;
  esac
}
hc_fingerprint() {
  if [ -f "$HC_CONFIG" ]; then sha256sum <"$HC_CONFIG"; else printf absent; fi
}
hc_preserved() { # $1=変更方法
  case "$1" in
    add|value) check "H20 $HC_NAME: 設定を復元しない" after "$(git -C "$R" config --file "$HC_CONFIG" --get selftest.human)" ;;
    delete) f "H20 $HC_NAME: 設定を作り直さない" test -e "$HC_CONFIG" ;;
    reorder) check "H20 $HC_NAME: 順序を復元しない" $'selftest.b=2\nselftest.a=1' "$(git -C "$R" config --file "$HC_CONFIG" --list)" ;;
  esac
}
hc_strip_saved() { # 旧版と同じ保存形式にする(設定そのものは変更しない)
  python3 - "$1" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d.pop("repo:git-dir", None)
d.pop("repo:config.worktree", None)
d.pop("repo_admin", None)
json.dump(d, open(p, "w"))
PY
}
# 通常終了。拡張なしで後から置かれたファイルと、順序だけの変更も見る。
for action in add delete value reorder inactive; do
  initial=present; extension=on; change="$action"
  case "$action" in add) initial=absent ;; reorder) initial=reorder ;; inactive) initial=absent; extension=off; change=add ;; esac
  hc_setup "normal-$action" humancfg "$initial" linked "$extension"
  run_loop "$HC_NAME" "SELFTEST_HUMAN_CONFIG=$HC_CONFIG" "SELFTEST_HUMAN_ACTION=$change" -- --repo "$HC_REPO" "${COMMON_ARGS[@]}"
  check "H20 $HC_NAME: 終了コード" 10 "$RC"
  has "H20 $HC_NAME: 理由" "$OUT" '[shared-state]'
  check "H20 $HC_NAME: 次のタスクへ進まない" humancfg-a "$(calls)"
  SD="$(state_dir "$HC_NAME")"
  has "H20 $HC_NAME: 停止印に保持済み設定の構造差分" "$SD/stop-mark.md" '保持済み config'
  has "H20 $HC_NAME: 報告に保持済み設定の構造差分" "$(report_of "$OUT")" '保持済み config'
  hasnt "H20 $HC_NAME: 変更後の設定値を出さない" "$SD/stop-mark.md" selftest.human
  if sed -n '/^--- stub-end/,$p' "$REC/ssh.log" | grep -q git-upload-pack; then
    ng "H20 $HC_NAME: 後続ネットワークを打たない"
  else
    ok "H20 $HC_NAME: 後続ネットワークを打たない"
  fi
  hc_preserved "$change"
done
# 設定あり/なし・主/linked の変更なしはどれも 2 周を完了する。
for location in main linked; do
  for initial in absent present; do
    hc_setup "normal-$location-$initial" pr "$initial" "$location"
    HC_BEFORE="$(hc_fingerprint)"
    run_loop "$HC_NAME" -- --repo "$HC_REPO" "${COMMON_ARGS[@]}"
    check "H20 $HC_NAME: 変更なしは成功" 0 "$RC"
    check "H20 $HC_NAME: 次のタスクを実行" 'pr-a pr-b' "$(calls)"
    check "H20 $HC_NAME: 設定に書かない" "$HC_BEFORE" "$(hc_fingerprint)"
  done
done
# 中断後の追加/削除/値変更、無変更、起動元変更、旧形式。TERM も同じ照合を通す。
for action in add delete value unchanged absent switch legacy term; do
  initial=present
  case "$action" in add|absent) initial=absent ;; esac
  hc_setup "restart-$action" longsleep "$initial" linked
  HC_OTHER="$W/repos/$HC_NAME other"
  if [ "$action" = switch ]; then
    G -C "$R" worktree add -q --detach "$HC_OTHER" HEAD
    git -C "$R" config --file "$(git -C "$HC_OTHER" rev-parse --absolute-git-dir)/config.worktree" selftest.human before
  fi
  start_bg "$HC_NAME" -- --repo "$HC_REPO" "${COMMON_ARGS[@]}"
  if ! wait_file "$REC/started-longsleep-a" 30; then
    ng "H20 $HC_NAME: 中断する周が始まらない"; kill -KILL "$BG_PID" 2>/dev/null; wait_bg; continue
  fi
  SD="$(state_dir "$HC_NAME")"
  CHILD="$(cat "$REC/pid-longsleep-a")"
  if [ "$action" = term ]; then
    hc_change value
    kill -TERM "$BG_PID"; wait_bg
    check "H20 TERM: 143" 143 "$BG_RC"
    has "H20 TERM: 停止印に保持済み設定の構造差分" "$SD/stop-mark.md" '保持済み config'
    check "H20 TERM: 次のタスクへ進まない" longsleep-a "$(calls)"
    hc_preserved value
    has "H20 TERM: 報告に保持済み設定の構造差分" "$(report_of "$OUT")" '保持済み config'
    : >"$REC/ssh.log"
    run_loop "$HC_NAME" -- --repo "$HC_REPO" "${COMMON_ARGS[@]}"
    check "H20 TERM: 停止印がある再起動は20" 20 "$RC"
    has "H20 TERM: 再起動の理由" "$OUT" '[stop-mark]'
    f "H20 TERM: 再起動後にネットワークを打たない" grep -q git-upload-pack "$REC/ssh.log"
    check "H20 TERM: 再起動後も次のタスクへ進まない" longsleep-a "$(calls)"
    continue
  fi
  kill -KILL "$BG_PID"; wait_bg
  case "$action" in
    add|delete|value) hc_change "$action" ;;
    switch) HC_REPO="$HC_OTHER" ;;
    legacy) hc_strip_saved "$SD/inflight/base.json" ;;
  esac
  HC_BEFORE="$(hc_fingerprint)"
  cp "$SD/inflight/meta" "$REC/held-meta"
  cp "$SD/inflight/base.json" "$REC/held-base.json"
  : >"$REC/ssh.log"
  run_loop "$HC_NAME" -- --repo "$HC_REPO" "${COMMON_ARGS[@]}"
  check "H20 $HC_NAME: 中断は未変更でも20" 20 "$RC"
  has "H20 $HC_NAME: 理由" "$OUT" '[inflight-untrusted]'
  check "H20 $HC_NAME: 次のタスクへ進まない" longsleep-a "$(calls)"
  t "H20 $HC_NAME: 中断印を保全" test -d "$SD/inflight"
  f "H20 $HC_NAME: 後続ネットワークを打たない" grep -q git-upload-pack "$REC/ssh.log"
  t "H20 $HC_NAME: 未確認の子を保全する" proc_alive "$CHILD"
  t "H20 $HC_NAME: 中断記録を保全する" cmp -s "$REC/held-meta" "$SD/inflight/meta"
  t "H20 $HC_NAME: 比較基準を保全する" cmp -s "$REC/held-base.json" "$SD/inflight/base.json"
  confirm_rejected_inflight "$SD" "H20 $HC_NAME"
  if [ "$action" = unchanged ] || [ "$action" = absent ]; then
    run_loop "$HC_NAME" -- --repo "$HC_REPO" "${COMMON_ARGS[@]}"
    check "H20 $HC_NAME: 人の確認後は正常に次周へ進む" 0 "$RC"
    check "H20 $HC_NAME: 確認後に次のタスクを実行" 'longsleep-a pr-b' "$(calls)"
  fi
  check "H20 $HC_NAME: 人の設定を変更しない" "$HC_BEFORE" "$(hc_fingerprint)"
done
# 正常実行間は既存どおり報告だけで続く。旧形式を黙って無視しない。
for action in value switch legacy; do
  hc_setup "between-$action" pr present linked
  run_loop "$HC_NAME" -- --repo "$HC_REPO" --max-iterations 1 "${COMMON_ARGS[@]}"
  check "H20 $HC_NAME: 最初の周" 0 "$RC"
  SD="$(state_dir "$HC_NAME")"
  case "$action" in
    value) hc_change value ;;
    switch) HC_REPO="$R" ;;
    legacy) hc_strip_saved "$SD/last-verified.json" ;;
  esac
  HC_BEFORE="$(hc_fingerprint)"
  run_loop "$HC_NAME" -- --repo "$HC_REPO" "${COMMON_ARGS[@]}"
  check "H20 $HC_NAME: 正常実行間は続く" 0 "$RC"
  check "H20 $HC_NAME: 次のタスクを実行" 'pr-a pr-b' "$(calls)"
  has "H20 $HC_NAME: 差分を報告" "$(report_of "$OUT")" '最後に照合に通った状態からの差分'
  has "H20 $HC_NAME: 人の管理パスか設定を報告" "$(report_of "$OUT")" 'repo:'
  check "H20 $HC_NAME: 人の設定を変更しない" "$HC_BEFORE" "$(hc_fingerprint)"
done
# 発見モードも共用の照合を通り、次の発見元へ進まない。
hc_setup discover pr present linked
run_loop "$HC_NAME" SELFTEST_DISC_DA=humancfg "SELFTEST_HUMAN_CONFIG=$HC_CONFIG" SELFTEST_HUMAN_ACTION=value -- --repo "$HC_REPO" --discover "${COMMON_ARGS[@]}"
check "H20 発見: 停止" 10 "$RC"
check "H20 発見: 次の発見元へ進まない" disc-data-audit "$(calls)"
has "H20 発見: 停止印に人の設定の構造差分" "$(state_dir "$HC_NAME")/stop-mark.md" '保持済み config'
hasnt "H20 発見: 設定値を出さない" "$(state_dir "$HC_NAME")/stop-mark.md" selftest.human
hc_preserved value
fi

# ════════════════ 配布物・利用者環境・参考ログ(#192) ════════════════
if want environment; then
for attack in source skill copy guard user global; do
  newrepo "env-$attack"; addtask pr-a 2026-01-01; addtask pr-b 2026-01-02; commit; newrec "env-$attack"
  cp "$PLUG/skills/create-task/scripts/resolve-task-dir.py" "$W/resolver-save"
  cp "$GIT_CONFIG_GLOBAL" "$W/global-save"
  if [ -e "$W/home/.claude/settings.json" ]; then cp "$W/home/.claude/settings.json" "$W/user-save"; else rm -f "$W/user-save"; fi
  run_loop "env-$attack" "SELFTEST_ENV_ATTACK=$attack" "SELFTEST_PLUGIN_SOURCE=$PLUG" -- --repo "$R" "${COMMON_ARGS[@]}"
  check "環境改変($attack): 停止" 10 "$RC"
  check "環境改変($attack): 次の周なし" pr-a "$(calls)"
  has "環境改変($attack): 環境の照合失敗" "$(report_of "$OUT")" 'plugin または利用者設定の照合に失敗'
  if sed -n '/^--- stub-end/,$p' "$REC/ssh.log" | grep -q git-upload-pack; then ng "環境改変($attack): 後続networkなし"; else ok "環境改変($attack): 後続networkなし"; fi
  cp "$W/resolver-save" "$PLUG/skills/create-task/scripts/resolve-task-dir.py"
  rm -f "$PLUG/skills/ship-task/SKILL.md"
  cp "$W/global-save" "$GIT_CONFIG_GLOBAL"
  if [ -e "$W/user-save" ]; then cp "$W/user-save" "$W/home/.claude/settings.json"; else rm -f "$W/home/.claude/settings.json"; fi
done
# 初回の正常完走と、起動間の設定更新は再利用コピーを使わず成功する。
newrepo env-normal; addtask pr-a 2026-01-01; addtask pr-b 2026-01-02; commit; newrec env-normal
run_loop env-normal -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
check '環境正常: 最初の周' 0 "$RC"
SD="$(state_dir env-normal)"
printf 'raise SystemExit(77)\n' >"$SD/environment-guard.py"
printf 'forged\n' >"$SD/environment.json"
mkdir -p "$W/home/.claude"; printf '{}\n' >"$W/home/.claude/settings.json"
run_loop env-normal -- --repo "$R" "${COMMON_ARGS[@]}"
check '環境正常: 起動間設定更新と旧STATE偽helperを再利用しない' 0 "$RC"
check '環境正常: 次のタスクを実行' 'pr-a pr-b' "$(calls)"
rm -f "$W/home/.claude/settings.json"
# 攻撃と正常保留の全てに同じブレーカーを適用する。
for attack in fake delete duplicate reorder malformed truncated fifo symlink; do
  newrepo "log-$attack"; addtask holdg1-a 2026-01-01; addtask holdg1-b 2026-01-02; addtask pr-c 2026-01-03; commit; newrec "log-$attack"
  printf unchanged >"$W/outside-log"
  run_loop "log-$attack" "SELFTEST_ENV_ATTACK=$attack" "SELFTEST_LOG_OUTSIDE=$W/outside-log" -- --repo "$R" --max-consecutive-failures 50 "${COMMON_ARGS[@]}"
  check "参考ログ($attack):2回で10" 10 "$RC"
  check "参考ログ($attack):計数を緩めない" 'holdg1-a holdg1-b' "$(calls)"
  check "参考ログ($attack):外部ファイル不変" unchanged "$(cat "$W/outside-log")"
done
newrepo log-stricter; addtask holdg1-a 2026-01-01; addtask pr-b 2026-01-02; commit; newrec log-stricter
run_loop log-stricter SELFTEST_ENV_ATTACK=fake -- --repo "$R" --max-consecutive-failures 1 "${COMMON_ARGS[@]}"
check 'G1: 通常ブレーカー1が厳しければ1回で10' 10 "$RC"
check 'G1: 通常ブレーカー1の後は回らない' holdg1-a "$(calls)"
fi

# ════════════════ H31・H34: ホスト CLI の証明と allowed-tools(#193)════════════════
if want host; then
newrepo hostproof
addtask pr-a 2026-01-01
commit
H31BIN="$W/h31bin"
mkdir -p "$H31BIN"
h31_copy() { # $1=名 → 中身はスタブと同じ、別の実体(証明は別になる)
  cp "$STUBBIN/claude" "$H31BIN/$1"
  chmod +x "$H31BIN/$1"
}
proofs() { find "$PROOF_DIR" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l; }
# 正常: 証明があれば起動前の検査に通り、報告に実体と証明が出る
newrec h31-normal
run_loop h31 -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
check "H31 正常: 証明があれば --dry-run が通る" 0 "$RC"
has "H31 正常: 報告に実体と証明が出る" "$(latest_report h31)" "ホスト CLI の実体: $STUBBIN/claude"
has "H31 正常: --version を打つ" "$REC/aux.log" "claude --version"
# 証明が無い実体は、一度も起動せずに止まる
h31_copy fresh
newrec h31-fresh
run_loop h31 -- --repo "$R" --dry-run --host-argv "$H31BIN/fresh" "${COMMON_ARGS[@]}"
check "H31 証明の無い実体で止まる" 20 "$RC"
has "H31 証明の無い実体: 理由" "$OUT" "[host-proof]"
hasnt "H31 証明の無い実体: 起動しない" "$REC/aux.log" "fresh"
# --prove-host: hook が拒否したときだけ証明を書く(認証の失敗・hook が呼ばれない・書けた・試みない は書かない)
before="$(proofs)"
for mode in skip allow autherr noattempt; do
  newrec "h31-probe-$mode"
  run_loop h31 "SELFTEST_PROBE_MODE=$mode" -- --repo "$R" --prove-host --host-argv "$H31BIN/fresh" "${COMMON_ARGS[@]}"
  check "H31 確認($mode)で証明を書かない: 終了コード" 20 "$RC"
  has "H31 確認($mode): 理由" "$OUT" "[host-proof]"
  check "H31 確認($mode): 証明が増えない" "$before" "$(proofs)"
done
newrec h31-auth
run_loop h31 SELFTEST_AUTH_RC=1 -- --repo "$R" --prove-host --host-argv "$H31BIN/fresh" "${COMMON_ARGS[@]}"
check "H31 認証の失敗で証明を書かない" 20 "$RC"
has "H31 認証の失敗: 理由" "$OUT" "[auth]"
check "H31 認証の失敗: 証明が増えない" "$before" "$(proofs)"
newrec h31-prove
run_loop h31 -- --repo "$R" --prove-host --host-argv "$H31BIN/fresh" "${COMMON_ARGS[@]}"
check "H31 hook が拒否すれば証明を書く" 0 "$RC"
has "H31 証明を書いた: 報告" "$OUT" "ホスト CLI の証明を書いた:"
check "H31 証明が 1 つ増える" "$((before + 1))" "$(proofs)"
has "H31 確認は周と同じ hook の設定で起動する" "$REC/argv-probe" "--settings"
has "H31 確認は許可の仲介の記録を残す" "$(ls -d "$(state_dir h31)"/*/host-probe.*/permlog | tail -1)" '"decision": "deny"'
run_loop h31 -- --repo "$R" --dry-run --host-argv "$H31BIN/fresh" "${COMMON_ARGS[@]}"
check "H31 証明の後は --dry-run が通る" 0 "$RC"
# 版・help の変化(実体は同じ)
newrec h31-version
run_loop h31 "SELFTEST_HOST_VERSION=2.1.999 (stub)" -- --repo "$R" --dry-run --host-argv "$H31BIN/fresh" "${COMMON_ARGS[@]}"
check "H31 版が変われば止まる" 20 "$RC"
has "H31 版の変化: 理由" "$OUT" "--version の出力が確認した時と違う"
{ cat "$W/help.txt"; printf '  --bare                                Minimal mode (default for -p)\n'; } >"$W/help-changed.txt"
newrec h31-help
run_loop h31 "SELFTEST_HELP_FILE=$W/help-changed.txt" -- --repo "$R" --dry-run --host-argv "$H31BIN/fresh" "${COMMON_ARGS[@]}"
check "H31 help が変われば止まる" 20 "$RC"
has "H31 help の変化: 理由" "$OUT" "--help の出力が確認した時と違う"
# 分類器の起動(#107 H48): 許可の仲介を通らない書き込みがあるので、証明を書かず、起動もしない
newrec h31-classifier
for mode in --prove-host --dry-run; do
  run_loop h31 -- --repo "$R" "$mode" --allow-classifier "${COMMON_ARGS[@]}"
  check "H31 分類器の起動($mode)は止まる" 20 "$RC"
  has "H31 分類器の起動($mode): 理由" "$OUT" "[classifier]"
done
check "H31 分類器の起動: ホスト CLI を起動しない" "" "$(cat "$REC/aux.log" 2>/dev/null)"
# 同じ inode の書き換え・別の inode への差し替え: 置き換わった実体を起動せずに止まる
for how in rewrite replace; do
  h31_copy "$how"
  run_loop h31 -- --repo "$R" --prove-host --host-argv "$H31BIN/$how" "${COMMON_ARGS[@]}"
  check "H31 $how: 先に証明を書く" 0 "$RC"
  # 置き換わった実体は、起動されると最初の行で印を残す(どの分岐でも exit する前)
  { head -1 "$STUBBIN/claude"; printf 'echo replaced >>"${SELFTEST_REC:?}/replaced-ran"\n'; tail -n +2 "$STUBBIN/claude"; } >"$W/h31-$how.body"
  if [ "$how" = rewrite ]; then
    ino_before="$(stat -c %i "$H31BIN/$how")"
    cat "$W/h31-$how.body" >"$H31BIN/$how"   # 同じ inode のまま中身を書き換える
    check "H31 rewrite: inode を保つ" "$ino_before" "$(stat -c %i "$H31BIN/$how")"
  else
    cp "$W/h31-$how.body" "$H31BIN/$how.new"
    chmod +x "$H31BIN/$how.new"
    mv -f "$H31BIN/$how.new" "$H31BIN/$how"
  fi
  newrec "h31-$how"
  run_loop h31 -- --repo "$R" --dry-run --host-argv "$H31BIN/$how" "${COMMON_ARGS[@]}"
  check "H31 $how: 止まる" 20 "$RC"
  has "H31 $how: 理由" "$OUT" "[host-proof]"
  f "H31 $how: 置き換わった実体を起動しない" test -e "$REC/replaced-ran"
  hasnt "H31 $how: 補助の CLI も打たない" "$REC/aux.log" "$how"
done
# 対照: 置き換わった実体が起動されれば印が残る(上の検査が空でないこと)
( cd "$W/cwd" && env -i PATH="$STUBBIN:$SAFEBIN" SELFTEST_REC="$REC" SELFTEST_HELP_FILE="$W/help.txt" "$H31BIN/replace" --version ) >/dev/null 2>&1
t "H31 対照: 置き換わった実体を起動すると印が残る" test -e "$REC/replaced-ran"
# 周の途中の差し替え: 起動時の検査の後に実体が変わったら、周の子を起動しない
h31_copy midrun
run_loop h31 -- --repo "$R" --prove-host --host-argv "$H31BIN/midrun" "${COMMON_ARGS[@]}"
check "H31 周の途中: 先に証明を書く" 0 "$RC"
newrec h31-midrun
printf '%s\n' "$H31BIN/midrun" >"$REC/mutate-on-auth"
run_loop h31 -- --repo "$R" --host-argv "$H31BIN/midrun" "${COMMON_ARGS[@]}"
check "H31 周の途中で実体が変われば止まる" 20 "$RC"
has "H31 周の途中: 理由" "$OUT" "[host-proof]"
check "H31 周の途中: 周の子を起動しない" "" "$(cat "$REC/child-starts.log" 2>/dev/null)"
# 同じ大きさ・同じ mtime の書き換え(ctime だけが変わる)と、同じ大きさ・同じ mtime の別 inode への差し替え。
# 次の補助の CLI の前に止まり、置き換わった実体を起動しない(stat の照合が ctime・inode を見ていること)
mark_len="$(printf '%s' 'echo replaced >>"${SELFTEST_REC:?}/replaced-ran"' | wc -c)"
for how in ctime inode; do
  { head -1 "$STUBBIN/claude"; printf '#%*s\n' "$((mark_len - 1))" ''; tail -n +2 "$STUBBIN/claude"; } >"$H31BIN/mid-$how"
  chmod +x "$H31BIN/mid-$how"
  # 状態名を分ける(片方の失敗で残った印が、もう片方の起動を止めないように)
  run_loop "h31mid-$how" -- --repo "$R" --prove-host --host-argv "$H31BIN/mid-$how" "${COMMON_ARGS[@]}"
  check "H31 周の途中($how): 先に証明を書く" 0 "$RC"
  newrec "h31-mid-$how"
  printf '%s\t%s\n' "$H31BIN/mid-$how" "$how" >"$REC/mutate-on-auth"
  size_before="$(stat -c %s "$H31BIN/mid-$how")"
  run_loop "h31mid-$how" -- --repo "$R" --host-argv "$H31BIN/mid-$how" "${COMMON_ARGS[@]}"
  check "H31 周の途中($how): 大きさは変わっていない" "$size_before" "$(stat -c %s "$H31BIN/mid-$how")"
  check "H31 周の途中($how): 止まる" 20 "$RC"
  has "H31 周の途中($how): 理由" "$OUT" "[host-proof]"
  f "H31 周の途中($how): 置き換わった実体を起動しない" test -e "$REC/replaced-ran"
  check "H31 周の途中($how): 周の子を起動しない" "" "$(cat "$REC/child-starts.log" 2>/dev/null)"
done
# hook を読ませない環境変数が立っていれば、ホスト CLI を起動せずに止まる
newrec h31-simple
for v in CLAUDE_CODE_SIMPLE CLAUDE_CODE_SAFE_MODE; do
  run_loop h31 "$v=1" -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
  check "H31 $v が立っていれば止まる" 20 "$RC"
  has "H31 $v: 理由" "$OUT" "[hooks-disabled]"
done
check "H31 hook を読ませない環境変数: ホスト CLI を起動しない" "" "$(cat "$REC/aux.log" 2>/dev/null)"
# 使い方の誤り
run_loop h31 -- --repo "$R" --prove-host --dry-run
check "H31 --prove-host と --dry-run は併用できない" 2 "$RC"

# H34: skill・command の allowed-tools(利用者の設定ディレクトリは試験ごとに分ける)
h34_cfg() { # $1=名 → H34CFG(空の設定ディレクトリ)
  H34CFG="$W/h34cfg-$1"
  mkdir -p "$H34CFG/skills"
}
h34_skill() { # $1=置き場の skills $2=名 $3=frontmatter の行
  mkdir -p "$1/$2"
  printf -- '---\nname: %s\ndescription: h34\n%s\n---\nbody\n' "$2" "$3" >"$1/$2/SKILL.md"
}
h34_cfg safe
h34_skill "$H34CFG/skills" reader 'allowed-tools: Read'
newrec h34-safe
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" -- --repo "$R" --dry-run --allowed-tools Read "${COMMON_ARGS[@]}"
check "H34 許可リストに含まれる skill は通る" 0 "$RC"
has "H34 通った: 報告" "$(latest_report h34)" "skill・command の allowed-tools: 検査したファイル(.md と plugin のマニフェスト)"
h34_cfg wide
h34_skill "$H34CFG/skills" runner 'allowed-tools: Bash(node:*)'
newrec h34-wide
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" -- --repo "$R" --dry-run --allowed-tools Read "${COMMON_ARGS[@]}"
check "H34 権限を足す skill で止まる" 20 "$RC"
has "H34 権限を足す skill: 理由" "$OUT" "[skill-grants]"
has "H34 権限を足す skill: ファイルと規則を出す" "$OUT" "runner/SKILL.md: allowed-tools の Bash(node:*) が許可リストより広い"
h34_cfg cmd
mkdir -p "$H34CFG/commands"
printf -- '---\nallowed-tools: Write\n---\n' >"$H34CFG/commands/w.md"
newrec h34-cmd
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
check "H34 権限を足す command で止まる" 20 "$RC"
has "H34 権限を足す command: 理由" "$OUT" "[skill-grants]"
has "H34 権限を足す command: ファイルと規則" "$OUT" "commands/w.md: allowed-tools の Write が許可リストより広い"
# 有効な plugin の installPath(plugin 一覧の形は実物と同じ)
h34_cfg plug
mkdir -p "$W/h34plug/commands"
printf -- '---\ndescription: x\nallowed-tools: Bash(node:*), AskUserQuestion\n---\n' >"$W/h34plug/commands/rescue.md"
printf '[{"id":"codex@m","version":"1.0","scope":"user","enabled":true,"installPath":"%s"}]\n' "$W/h34plug" >"$W/h34plug.json"
newrec h34-plug
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" "SELFTEST_PLUGINS_FILE=$W/h34plug.json" -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
check "H34 有効な plugin の command で止まる" 20 "$RC"
has "H34 plugin: ファイルを出す" "$OUT" "$W/h34plug/commands/rescue.md"
printf '[{"id":"codex@m","version":"1.0","scope":"user","enabled":false,"installPath":"%s"}]\n' "$W/h34plug" >"$W/h34plug-off.json"
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" "SELFTEST_PLUGINS_FILE=$W/h34plug-off.json" -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
check "H34 無効の plugin は見ない" 0 "$RC"
# 同じ exact は通り、許可リストに無ければ止まる(分類器の起動の包含は単体の回帰で確かめる。起動は H48 で止まる)
h34_cfg exact
h34_skill "$H34CFG/skills" status 'allowed-tools: Bash(git status)'
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" -- --repo "$R" --dry-run --allowed-tools 'Bash(git status)' "${COMMON_ARGS[@]}"
check "H34 同じ exact の skill は通る" 0 "$RC"
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" -- --repo "$R" --dry-run --allowed-tools 'Bash(git status --short)' "${COMMON_ARGS[@]}"
check "H34 違う exact では止まる" 20 "$RC"
has "H34 違う exact: 理由" "$OUT" "[skill-grants]"
has "H34 違う exact: ファイルと規則" "$OUT" "status/SKILL.md: allowed-tools の Bash(git status) が許可リストより広い"
# 正規導入(setup.sh --global の形のリンク)は通る。リンク先の frontmatter が権限を足せば止まる
h34_cfg link
mkdir -p "$W/h34clone/skills"
h34_skill "$W/h34clone/skills" do-task 'allowed-tools: Read'
h34_skill "$W/h34clone/skills" evil 'allowed-tools: Bash(rm *)'
ln -s "$W/h34clone/skills/do-task" "$H34CFG/skills/do-task"
newrec h34-link
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" -- --repo "$R" --dry-run --allowed-tools Read "${COMMON_ARGS[@]}"
check "H34 正規導入のリンクは通る" 0 "$RC"
ln -s "$W/h34clone/skills/evil" "$H34CFG/skills/evil"
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" -- --repo "$R" --dry-run --allowed-tools Read "${COMMON_ARGS[@]}"
check "H34 正規導入でも権限を足すリンク先なら止まる" 20 "$RC"
has "H34 リンク先: 理由" "$OUT" "[skill-grants]"
# 特殊ファイル・内側のリンクは、検査の前に控えの段階で止まる
h34_cfg fifo
mkfifo "$H34CFG/skills/pipe.md"
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
check "H34 FIFO で止まる" 20 "$RC"
has "H34 FIFO: 理由" "$OUT" "[environment]"
h34_cfg inner
h34_skill "$H34CFG/skills" in 'allowed-tools: Read'
ln -s "$W/h34clone/skills/evil/SKILL.md" "$H34CFG/skills/in/other.md"
run_loop h34 "CLAUDE_CONFIG_DIR=$H34CFG" -- --repo "$R" --dry-run --allowed-tools Read "${COMMON_ARGS[@]}"
check "H34 内側のリンクで止まる" 20 "$RC"
has "H34 内側のリンク: 理由" "$OUT" "[environment]"
fi

# ════════════════ H49: 起動環境の shell prefix(#227) ════════════════
if want shell-prefix; then
# 既存の証明、対象リポジトリ、状態領域をそれぞれ固定して、拒否がホスト呼出し・状態生成より前であることを確かめる。
h49_proof_fingerprint() {
  find "$PROOF_DIR" -type f -printf '%P\n' 2>/dev/null | LC_ALL=C sort | while IFS= read -r f; do
    printf '%s ' "$f"
    sha256sum "$PROOF_DIR/$f"
  done | sha256sum
}
h49_repo_fingerprint() {
  {
    git -C "$R" rev-parse HEAD
    git -C "$R" rev-parse 'HEAD^{tree}'
    git -C "$R" for-each-ref --format='%(refname) %(objectname)'
    git -C "$R" status --porcelain=v1 -z | sha256sum
    find "$R" -path "$R/.git" -prune -o -type f -printf '%P\0' | LC_ALL=C sort -z | while IFS= read -r -d '' f; do
      printf '%s ' "$f"
      sha256sum "$R/$f"
    done
  } | sha256sum
}
h49_no_side_effects() { # $1=ラベル $2=状態名 $3=証明の指紋 $4=リポジトリの指紋
  f "$1: 状態・ロック・報告を作らない" test -e "$W/state/$2"
  check "$1: ホストの補助呼出しをしない" "" "$(cat "$REC/aux.log" 2>/dev/null)"
  check "$1: タスクを実行しない" "" "$(calls)"
  check "$1: 既存証明を変えない" "$3" "$(h49_proof_fingerprint)"
  check "$1: リポジトリの HEAD・ref・内容を変えない" "$4" "$(h49_repo_fingerprint)"
}

newrepo h49-reject
addtask pr-a 2026-01-01
commit
H49_PROOF_BEFORE="$(h49_proof_fingerprint)"
H49_REPO_BEFORE="$(h49_repo_fingerprint)"
# `0`、空白、タブ、改行・CR・ESC を含む値も、空でなければ同じ起動時検査で拒否する。
H49_VALUES=(ordinary 0 ' ' $'\t' $'line\ncarriage\rescape\e')
for i in "${!H49_VALUES[@]}"; do
  for mode in normal dry prove; do
    state="h49-reject-$i-$mode"
    newrec "$state"
    args=(--repo "$R")
    case "$mode" in dry) args+=(--dry-run) ;; prove) args+=(--prove-host) ;; esac
    run_loop "$state" "CLAUDE_CODE_SHELL_PREFIX=${H49_VALUES[$i]}" -- "${args[@]}" "${COMMON_ARGS[@]}"
    check "H49 非空($i/$mode): 終了コード" 20 "$RC"
    has "H49 非空($i/$mode): 理由" "$OUT" "[shell-prefix]"
    h49_no_side_effects "H49 非空($i/$mode)" "$state" "$H49_PROOF_BEFORE" "$H49_REPO_BEFORE"
  done
done
# 診断は値を出さず、変数名と解除方法だけを伝える。制御文字を含む値も文字列として実行しない。
H49_SECRET=$'H49-secret-value\nwith-tab\t-cr\r-and-escape\e'
newrec h49-secret
run_loop h49-secret "CLAUDE_CODE_SHELL_PREFIX=$H49_SECRET" -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
check "H49 診断: 終了コード" 20 "$RC"
has "H49 診断: 変数名" "$OUT" "CLAUDE_CODE_SHELL_PREFIX"
has "H49 診断: 外してから起動する案内" "$OUT" "外してから起動する"
hasnt "H49 診断: 値を出さない" "$OUT" "$H49_SECRET"
hasnt "H49 診断: タブを出さない" "$OUT" $'\t'
hasnt "H49 診断: CR を出さない" "$OUT" $'\r'
hasnt "H49 診断: ESC を出さない" "$OUT" $'\e'
h49_no_side_effects "H49 診断" h49-secret "$H49_PROOF_BEFORE" "$H49_REPO_BEFORE"

# 未設定と明示的な空文字は従来どおり通す。通常起動はタスク 1 件を実行する。
for kind in unset empty; do
  envs=()
  [ "$kind" != empty ] || envs=(CLAUDE_CODE_SHELL_PREFIX=)
  newrepo "h49-normal-$kind"
  addtask pr-a 2026-01-01
  commit
  newrec "h49-normal-$kind"
  run_loop "h49-normal-$kind" "${envs[@]}" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
  check "H49 $kind: 通常起動" 0 "$RC"
  check "H49 $kind: タスクを 1 件実行" pr-a "$(calls)"

  newrepo "h49-dry-$kind"
  addtask pr-a 2026-01-01
  commit
  newrec "h49-dry-$kind"
  run_loop "h49-dry-$kind" "${envs[@]}" -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
  check "H49 $kind: --dry-run" 0 "$RC"

  newrepo "h49-prove-$kind"
  addtask pr-a 2026-01-01
  commit
  newrec "h49-prove-$kind"
  run_loop "h49-prove-$kind" "${envs[@]}" -- --repo "$R" --prove-host "${COMMON_ARGS[@]}"
  check "H49 $kind: --prove-host" 0 "$RC"
  has "H49 $kind: 証明を書いた" "$OUT" "ホスト CLI の証明を書いた:"
done

# help は環境変数の検査より先に終了し、未設定・空・非空の全てで副作用を起こさない。
for kind in unset empty nonempty; do
  envs=()
  case "$kind" in empty) envs=(CLAUDE_CODE_SHELL_PREFIX=) ;; nonempty) envs=(CLAUDE_CODE_SHELL_PREFIX=H49-help-secret) ;; esac
  state="h49-help-$kind"
  newrec "$state"
  H49_HELP_PROOF_BEFORE="$(h49_proof_fingerprint)"
  run_loop "$state" "${envs[@]}" -- --help
  check "H49 help($kind): 終了コード" 0 "$RC"
  has "H49 help($kind): 使い方" "$OUT" "使い方:"
  if [ "$kind" = nonempty ]; then hasnt "H49 help($kind): 値を出さない" "$OUT" H49-help-secret; fi
  f "H49 help($kind): 状態を作らない" test -e "$W/state/$state"
  check "H49 help($kind): ホストを呼ばない" "" "$(cat "$REC/aux.log" 2>/dev/null)"
  check "H49 help($kind): 証明を変えない" "$H49_HELP_PROOF_BEFORE" "$(h49_proof_fingerprint)"
done
fi

# ════════════════ H50: 設定ルートと設定 env(#229)════════════════
if want h50; then
newrepo h50
addtask pr-a 2026-01-01
commit
H50CFG="$W/h50-config"
H50MANAGED="$W/h50-managed"
mkdir -p "$H50CFG" "$H50MANAGED/managed-settings.d"
h50_proof_fingerprint() {
  find "$PROOF_DIR" -type f -printf '%P\n' 2>/dev/null | LC_ALL=C sort | while IFS= read -r f; do
    printf '%s ' "$f"; sha256sum "$PROOF_DIR/$f"
  done | sha256sum
}
h50_repo_fingerprint() {
  {
    git -C "$R" rev-parse HEAD
    git -C "$R" rev-parse 'HEAD^{tree}'
    git -C "$R" for-each-ref --format='%(refname) %(objectname)'
    git -C "$R" status --porcelain=v1 -z | sha256sum
    find "$R" -path "$R/.git" -prune -o -type f -printf '%P\0' | LC_ALL=C sort -z | while IFS= read -r -d '' f; do
      printf '%s ' "$f"; sha256sum "$R/$f"
    done
  } | sha256sum
}
h50_no_side_effects() { # $1=label $2=state $3=proof $4=repo
  f "$1: 状態・ロック・報告を作らない" test -e "$W/state/$2"
  check "$1: 補助の host CLI を呼ばない" "" "$(cat "$REC/aux.log" 2>/dev/null)"
  check "$1: タスクを実行しない" "" "$(calls)"
  check "$1: 子プロセスを残さない" "" "$(tagged_pids | tr '\n' ' ')"
  check "$1: 既存証明を変えない" "$3" "$(h50_proof_fingerprint)"
  check "$1: リポジトリの HEAD・ref・内容を変えない" "$4" "$(h50_repo_fingerprint)"
}
H50_PROOF_BEFORE="$(h50_proof_fingerprint)"
H50_REPO_BEFORE="$(h50_repo_fingerprint)"
h50_rejected() { # $1=名前, 残り=run_loop へ渡す環境
  local name="$1" mode state; shift
  for mode in normal discover dry prove; do
    state="h50-$name-$mode"
    newrec "$state"
    args=(--repo "$R")
    case "$mode" in
      discover) args+=(--discover) ;;
      dry) args+=(--dry-run) ;;
      prove) args+=(--prove-host) ;;
    esac
    run_loop "$state" "CLAUDE_CONFIG_DIR=$H50CFG" "$@" -- "${args[@]}" "${COMMON_ARGS[@]}"
    check "H50 $name/$mode: 終了コード" 20 "$RC"
    h50_no_side_effects "H50 $name/$mode" "$state" "$H50_PROOF_BEFORE" "$H50_REPO_BEFORE"
  done
}
h50_path_rejected() { # $1=名前, 残り=run_loop の環境
  local name="$1" mode state; shift
  for mode in normal discover dry prove; do
    state="h50-path-$name-$mode"
    newrec "$state"
    args=(--repo "$R")
    case "$mode" in discover) args+=(--discover) ;; dry) args+=(--dry-run) ;; prove) args+=(--prove-host) ;; esac
    run_loop "$state" "$@" -- "${args[@]}" "${COMMON_ARGS[@]}"
    check "H50 path $name/$mode: 終了コード" 20 "$RC"
    has "H50 path $name/$mode: 理由" "$OUT" "[host-config-path]"
    h50_no_side_effects "H50 path $name/$mode" "$state" "$H50_PROOF_BEFORE" "$H50_REPO_BEFORE"
  done
}
h50_path_rejected config-relative CLAUDE_CONFIG_DIR=relative
h50_path_rejected config-empty CLAUDE_CONFIG_DIR=
h50_path_rejected home-relative HOME=relative
for key in CLAUDE_CONFIG_DIR HOME XDG_CONFIG_HOME CLAUDE_CODE_SIMPLE CLAUDE_CODE_SAFE_MODE CLAUDE_CODE_SHELL_PREFIX; do
  printf '{"env":{"%s":"H50-secret"}}' "$key" >"$H50CFG/settings.json"
  h50_rejected "user-$key"
done
printf '{"policyHelper":"H50-secret"}' >"$H50CFG/settings.json"
h50_rejected user-helper
rm -f "$H50CFG/settings.json"
for key in CLAUDE_CODE_REMOTE_SETTINGS_PATH CLAUDE_CODE_MANAGED_SETTINGS_PATH CLAUDE_CODE_MOCK_REMOTE_SETTINGS; do
  h50_rejected "startup-$key" "$key=H50-secret"
done
printf '{"env":{"CLAUDE_CODE_SAFE_MODE":"H50-secret"}}' >"$H50MANAGED/managed-settings.d/a.json"
h50_rejected managed-dropin "DEV_WORKFLOW_LOOP_TEST_MANAGED_DIR=$H50MANAGED"
rm -f "$H50MANAGED/managed-settings.d/a.json"
printf '{"env":{"CLAUDE_CODE_SHELL_PREFIX":"H50-secret"}}' >"$H50CFG/remote-settings.json"
h50_rejected cache
rm -f "$H50CFG/remote-settings.json"
# 正常設定は通常・発見・dry・prove の各入口で通す。通常は実際に 1 件を処理し、
# prove は既存証明と別の状態で hook の確認をする。
newrepo h50-normal
addtask pr-a 2026-01-01
commit
newrec h50-normal
run_loop h50-normal "CLAUDE_CONFIG_DIR=$H50CFG" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
check "H50 正常設定: 通常" 0 "$RC"
check "H50 正常設定: 通常はタスクを 1 件実行" pr-a "$(calls)"
newrepo h50-discover
addtask pr-a 2026-01-01
commit
newrec h50-discover
run_loop h50-discover "CLAUDE_CONFIG_DIR=$H50CFG" -- --repo "$R" --discover --max-iterations 1 "${COMMON_ARGS[@]}"
check "H50 正常設定: 発見" 0 "$RC"
newrepo h50-dry
addtask pr-a 2026-01-01
commit
newrec h50-dry
run_loop h50-dry "CLAUDE_CONFIG_DIR=$H50CFG" -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
check "H50 正常設定: --dry-run" 0 "$RC"
newrepo h50-prove
addtask pr-a 2026-01-01
commit
newrec h50-prove
run_loop h50-prove "CLAUDE_CONFIG_DIR=$H50CFG" -- --repo "$R" --prove-host "${COMMON_ARGS[@]}"
check "H50 正常設定: --prove-host" 0 "$RC"
has "H50 正常設定: 証明を書いた" "$OUT" "ホスト CLI の証明を書いた:"
for key in CLAUDE_CODE_REMOTE_SETTINGS_PATH CLAUDE_CODE_MANAGED_SETTINGS_PATH CLAUDE_CODE_MOCK_REMOTE_SETTINGS; do
  newrec "h50-empty-$key"
  run_loop "h50-empty-$key" "CLAUDE_CONFIG_DIR=$H50CFG" "$key=" -- --repo "$R" --dry-run "${COMMON_ARGS[@]}"
  check "H50 $key の空値: --dry-run" 0 "$RC"
done
newrec h50-help
printf '{"env":{"CLAUDE_CONFIG_DIR":"H50-secret"}}' >"$H50CFG/settings.json"
H50_HELP_PROOF_BEFORE="$(h50_proof_fingerprint)"
run_loop h50-help "CLAUDE_CONFIG_DIR=$H50CFG" -- --help
check "H50 help: 終了コード" 0 "$RC"
has "H50 help: 使い方" "$OUT" "使い方:"
hasnt "H50 help: 値を出さない" "$OUT" H50-secret
f "H50 help: 状態を作らない" test -e "$W/state/h50-help"
check "H50 help: host CLI を呼ばない" "" "$(cat "$REC/aux.log" 2>/dev/null)"
check "H50 help: 証明を変えない" "$H50_HELP_PROOF_BEFORE" "$(h50_proof_fingerprint)"
fi

# ════════════════ 後片付け ════════════════
# 短命のもの(git の後始末など)が消えるのを少し待ってから確かめる
i=0
LEFT="$(tagged_pids | tr '\n' ' ')"
while [ -n "$LEFT" ] && [ "$i" -lt 20 ]; do sleep 0.5; i=$((i + 1)); LEFT="$(tagged_pids | tr '\n' ' ')"; done
if [ -z "$LEFT" ]; then
  ok "終わり: 印つきのプロセスが残っていない"
else
  DESC=""
  for p in $LEFT; do DESC="$DESC [$p: $(tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null | cut -c1-120)]"; done
  ng "終わり: 印つきのプロセスが残っていない(残り:$DESC)"
fi

echo
printf '%s\n' "$RESULTS"
echo
echo "結果: PASS ${PASS} 件 / FAIL ${FAIL} 件"
[ "$FAIL" -eq 0 ]
