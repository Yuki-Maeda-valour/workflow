#!/usr/bin/env bash
# 無人ループ(dev-workflow)。無人実行のメタ行がある `進行中_` を 1 件ずつ、周ごとの使い捨ての worktree と
# 新しいヘッドレスのセッションで /ship-task の無人モードに回す。人のシェル・cron から呼ぶ(skill ではない)。
# `--discover` では、発見元を 1 周ずつ /ship-task の発見の周に回して `候補_` を積む(loop.md §11)。
# **契約(既定値と意味・停止条件・終了コード・報告・限界・実走の手順)の正本は ../references/loop.md**。
# ここには引数と必要環境を書く。Linux・bash 4.4 以上が必要(setsid・flock・/proc を使う)。
#
# 使い方:
#   bash loop.sh [オプション]
#
# オプション:
#   --repo <ディレクトリ>            対象のリポジトリ(作業ツリーのトップ。既定は現在のディレクトリ)
#   --host <名前>                    既定表のホスト名(既定 claude)
#   --host-argv <トークン>           雛形(実行ファイルと前置きのフラグ)を置き換える。繰り返し可
#   --only <名>                      対象をタスク名で絞る。繰り返し可
#   --discover[=<名>[,<名>…]]        発見モード(発見元を 1 つずつ回す。値が無ければ既定の列)。--only と併用しない
#   --dry-run                        対象の一覧と解決後の argv を出して終わる(セッションを起動しない)
#   --prove-host                     ホスト CLI の実体で許可の仲介の hook が効くことを実際に確かめ、証明を書いて終わる
#   --allowed-tools <値>             ホスト CLI に渡す許可リスト。繰り返し可
#   --allow-classifier               分類器による自動承認(今は起動しない。#107 の H48)
#   --mcp-config <ファイル>          周に渡す MCP の設定。繰り返し可
#   --max-iterations <N>             最大周回数
#   --max-consecutive-failures <N>   連続失敗の上限
#   --time-budget <秒>               ループ全体の時間予算
#   --iteration-timeout <秒>         周の時間上限
#   --kill-grace <秒>                片付けの TERM → KILL の猶予
#   --net-timeout <秒>               ネットワークに出うる git と補助の CLI のタイムアウト
#   --stop-file <パス>               停止ファイル
#   --worktree-root <ディレクトリ>   周の worktree を作る場所
#   --state-max-items <N>            状態観察の項目上限(既定 100000)
#   --state-max-bytes <N>            状態観察の通常ファイル総読取上限(既定 1 GiB)
#   --state-max-file-bytes <N>       状態観察の通常ファイル1件上限(既定 64 MiB)
#   --state-max-seconds <N>          状態観察の壁時計上限(既定 60 秒)
#   -h, --help                       この使い方を出す
#
# 終了コード: 0 / 2 / 10 / 20 / 30 / 128+N(意味は loop.md)
# --- end usage ---
# OS → bash の版 → 初期化。bash 3.2 でも読める範囲だけで診断し、trap/die はまだ使わない。
if ! OS_NAME="$(uname -s 2>/dev/null)"; then
  printf '%s\n' 'ERROR [os] OS を判定できない(uname -s が失敗)。loop.sh は Linux だけに対応する' >&2
  exit 20
fi
if [ "$OS_NAME" != Linux ]; then
  printf 'ERROR [os] Linux でない(%s)。loop.sh は Linux だけに対応する(setsid・flock・/proc を使う)\n' "$OS_NAME" >&2
  exit 20
fi
if [ "${BASH_VERSINFO[0]}" -lt 4 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -lt 4 ]; }; then
  printf '%s\n' 'ERROR [bash-version] bash 4.4 以上が必要(inherit_errexit を使う)。PATH 上の bash の実体と版を確認する' >&2
  exit 20
fi
set -eEuo pipefail
shopt -s inherit_errexit
# export された CDPATH があると、素の `cd` が行き先を stdout へ出す。このスクリプトの `cd` はすべて明示パス
unset CDPATH
# 呼出元の値とexport属性を受け継がず、初回の信頼コピーだけを保持する。
unset TASK_TEXT_CODE TASK_TEXT_HELD
TASK_TEXT_CODE=""
TASK_TEXT_HELD=0

LOOP_START="$(date +%s)"
TAB=$'\t'

# ── 状態(シグナル・EXIT の trap が読むので、最初に宣言する)──
# ロックの fd は 7 に固定する(子・補助の CLI・ネットワークの git には `7>&-` で渡さない — D18)
EXPLICIT_EXIT=0   # finish / die・usage を通って終わるとき 1(EXIT の trap は内部の失敗だけを扱う)
ERR_LINE=""
REPORT=""
RUN_DIR=""
RUN_ID=""
STATE=""
TOP=""
COMMON=""
REPO_GIT_DIR=""
DEF_NAME=""
DEF_SHA=""
STOP_MARK=""
INFLIGHT=""
LAST_VERIFIED=""
FRESH_CONFIG_BASELINE=0 # 正常完走した別実行間の config link は新しい基準として受け直す
SEL_WT=""         # 選定中の worktree(周を起動する前。終わるときに消す)
SEL_TASK_DIR=""
TASK_DIR=""
ITER_ACTIVE=0     # 周の途中(周の途中の印を置いてから、片付けと照合が済むまで)
ITER_SEQ=""
ITER_ID=""
ITER_PGID=""
CHILD_PID=""
CHILD_START=""
SUPERVISOR_RESULT=""
SUPERVISOR_NONCE=""
SUPERVISOR_RC=97
ITER_WT=""
ITER_WTADMIN="-"
ITER_NAME=""
ITER_REL=""
ITER_BASE=""
ITER_BASE_SHA=""  # 比べる元のファイルの sha256(照合の前にファイルと突き合わせる)
ITER_VERIFIED=""      # verify_iteration が通した生の after state
ITER_VERIFIED_SHA=""  # その生 state を親が保持した sha256
ITER_PERMLOG=""
CLEANUP_LEFT=""
VERIFY_DIFF=""
SNAPSHOT_DIFF=""
ABORT_CODE=""
STOP_MARK_WRITTEN=""
STATE_GIT_UNSAFE=0  # 状態検査の拒否後は、報告・後片付けのためにも Git で設定を再読しない
PROMOTION_CRITICAL=0 # verified state を last-verified へ昇格する不可分区間
DEFERRED_SIGNAL_NAME=""
DEFERRED_SIGNAL_CODE=""
STOP_CONTROL_WORKTREE=""
STOP_CONTROL_REL=""
STOP_CONTROL_NEW_DIRS=()
REMOVED_PARENT_WORKTREES=()
HELD_NAMES=()
TRACKING_SKIPPED=()
STARTUP_NOTES=()
SKIPS=()
CANDIDATES=()
declare -A LOCKED_REASON=()  # worktree のパス → lock の理由(同じ理由の全件を保持)
declare -A SKIP_REPORTED=()
declare -A TRACKING_SEEN=()   # 追跡用の ref で読み飛ばしたタスク(名前で重複を除く)
declare -A WT_OUTCOME=()      # この実行で残した worktree → その周の結末(判定)
# 発見モード(loop.md §11)
ITER_SOURCE=""    # 周の発見元
ITER_TITLE=""     # 報告の周の見出し(実装モードは ITER_REL と同じ)
ITER_PROMPT=""
ITER_META_EXTRA=""
DISC_PATHS=""     # 判定で読んだ候補のパス(改行区切り)
DISC_PUSHED=""    # 正常(縮退)の判定で見た push の有無(yes = origin のブランチ = HEAD / no = origin に無い・origin が無い)
DISC_ORIGIN_DIFF=0  # 縮退の判定で、origin の今夜の名のブランチが判定した HEAD と違った
DISC_QUEUE=()     # この選定で回せる発見元(引数の順)
DISC_REFS=()      # 「<sha><TAB><ref>」。origin の分は ref を「origin:refs/heads/…」にする
DISC_DEGRADED=()  # 縮退で終わった周の「<発見元><TAB><ブランチ><TAB><push の有無>」
declare -A DISC_DONE=()       # この実行で回した発見元
declare -A DISC_RESULT=()     # 発見元 → その周の判定と結末
declare -A DISC_SKIP=()       # 発見元 → 読み飛ばしの理由
declare -A DISC_CLEAN=()      # 発見元 → 片付けの定型(改行区切り)
declare -A DISC_SEEN=()

# ── 引数 ──
REPO=""
HOST="claude"
HOST_ARGV_OVERRIDE=()
ONLY=()
DRY_RUN=0
ALLOWED_TOOLS=()
ALLOW_CLASSIFIER=0
MCP_CONFIGS=()
ARG_MAX_ITER=""
ARG_MAX_FAIL=""
ARG_BUDGET=""
ARG_ITER_TIMEOUT=""
KILL_GRACE=30
NET_TIMEOUT=60
WORKTREE_ADD_TIMEOUT=600   # worktree add は LFS の checkout が取りに行くので長め(設計 §2)
STOP_FILE=""
WT_ROOT=""
STATE_MAX_ITEMS=100000
STATE_MAX_BYTES=1073741824
STATE_MAX_FILE_BYTES=67108864
STATE_MAX_SECONDS=60
DISCOVER=0          # 発見モード(--discover)
PROVE_HOST=0        # --prove-host(実 hook の確認と証明の書き込みだけを行う)
FIXED_POLICY='{}' # component検査後の同期・組込み停止。active再bindでも保持する
HOST_EXEC=""        # 確かめたホスト CLI の実体(realpath)。補助の CLI・周の子・確認はすべてこれで起動する
HOST_IDENTITY=""    # その実体の値(realpath・dev・ino・size・mtime・ctime・sha256 の JSON)
HOST_STAT=""        # その実体の stat の値(補助の CLI ごとの軽い照合に使う)
HOST_SHAPE=""       # 証明に結び付ける起動の形(loop.sh が足すフラグ。可変の値は置き換える)
PROOF_STORE=""      # 証明の置き場(人の信頼の記録。状態ディレクトリとは別)
DISCOVER_ARG=""
DISCOVER_FROM_ARG=0
DISCOVER_SOURCES=()
DISCOVER_SRC_DESC=""

# 発見モードの発見元の列(既定の順)。**ship-task/references/discover-mode.md §1 の「発見元の列」と同じにする**
# (loop-selftest.sh が照合する)。列の外の名は使い方の誤り
DISCOVER_DEFAULT=(data-audit refactor)

# 既定値(#68 の決定 4)。profile は締める向きにだけ効く
DEF_MAX_ITER=5
DEF_MAX_FAIL=2
DEF_BUDGET=28800
DEF_ITER_TIMEOUT=3600
MAX_G1_HOLDS=2              # 許可の拒否(G1)の保留が続いたら止まる数(D6。profile・引数では変えない)
REVIEWS_COPY_LIMIT=52428800 # .claude/reviews を状態ディレクトリへ写す上限(50 MB)

# ホストのセッションの中の判定に使う環境変数の列(D7)。
# **do-task/references/external-runners.md §3 判定 2 の列と同じにする**(loop-selftest.sh が一致を照合する)
HOST_SESSION_VARS=(DEV_WORKFLOW_HOST_CLI CLAUDECODE CODEX_SANDBOX CURSOR_AGENT)

# 全 git 呼び出しの前置き(base-commit.md と同じ。hook・fsmonitor・置換参照を効かせない)
GIT_PRE=(--no-pager --no-replace-objects --no-optional-locks -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.ignoreStat=false -c commit.gpgSign=false -c push.gpgSign=false -c filter.lfs.smudge= -c filter.lfs.clean= -c filter.lfs.process= -c filter.lfs.required=false)

# ═══════════════════════════════ 関数 ═══════════════════════════════

usage() { sed -n '2,/^# --- end usage ---$/p' "$0" | sed '$d' >&2; }

fail_usage() { echo "ERROR [usage] $*" >&2; usage; EXPLICIT_EXIT=1; exit 2; }

need_val() { # $1=オプション名 $2=残り引数の個数
  if [ "$2" -lt 2 ]; then fail_usage "$1 に値が必要です"; fi
}

pos_int() { # $1=オプション名 $2=値(正の十進整数だけを受け付ける)
  case "$2" in
    ''|*[!0-9]*|0*) fail_usage "$1 は正の整数(先頭に 0 を付けない): '$2'" ;;
  esac
  [ "${#2}" -le 9 ] || fail_usage "$1 が大きすぎる: '$2'"
}

abs_path() { # $1=パス → 現在のディレクトリを基準にした絶対パス(存在しなくてよい。正規化はしない)
  case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$PWD" "$1" ;; esac
}

under() { case "$1" in "$2"|"$2"/*) return 0 ;; esac; return 1; }

# 人のチェックアウト(--repo のトップと main の worktree)の中か。$1 は物理パス(realpath -m で正規化したもの)
inside_checkout() { under "$1" "$TOP" || under "$1" "$MAIN_WT"; }

G() { git "${GIT_PRE[@]}" "$@"; }

# 待ち。ロックの fd を閉じて打つ(loop.sh が KILL されたとき、残った sleep がロックを握り続けないように)
nap() { sleep "$1" 7>&-; }

# 別セッションで起動して、終わったらそのセッションのプロセスグループの残りを止める(時間切れで git が
# 先に終わっても、TERM を無視した子孫〈ssh など〉が残りうる)
run_detached() { # $1=秒 残り=コマンド
  local secs="$1" pid rc=0
  shift
  setsid timeout -k 5 "$secs" "$@" </dev/null 7>&- &
  pid=$!
  wait "$pid" || rc=$?
  kill -KILL -- "-$pid" 2>/dev/null || true
  return "$rc"
}

# ネットワークに出うる git(ls-remote・worktree add): 端末から切り離し、認証を尋ねず、時間で打ち切る
net_git() { # $1=秒 残り=git の引数
  local secs="$1"
  shift
  verify_environment || die 20 environment "network Git の前に環境が変わった"
  GIT_TERMINAL_PROMPT=0 run_detached "$secs" git "${GIT_PRE[@]}" "$@"
}

# ── Python の補助(YAML・JSON・スナップショット・/proc の走査)──
# cwd を / にして起動する(`-c` は cwd を import の探索先に入れるので、リポジトリの中の同名モジュールを拾わない)。
# 渡すパスはすべて絶対パスにする。終了コード 9 は補助の想定外の失敗
read -r -d '' PY_HELPER <<'PY' || true
import difflib, hashlib, json, os, re, stat, subprocess, sys, unicodedata

# unattended-mode.md §2 の 5 値のパターン(両モード共通。モードで取りえない値は判定で失敗にする — loop.md §5)
OUTCOME = re.compile(r"^無人の周の結果: (PR|縮退|保留|失敗扱い|候補なし) — (.*)$")
PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")
D21_BAD = set("$`;|&<>(){}[]*?!'\"\\#~^%")
LOOP_KEYS = ("max_iterations", "max_consecutive_failures", "time_budget", "iteration_timeout")


def fail(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def d21_problem(text, what):
    for ch in text:
        if ch.isspace() or unicodedata.category(ch) == "Cc":
            return f"{what} に空白か制御文字がある"
        if ch in D21_BAD:
            return f"{what} にシェルの記号 {ch!r} がある"
    for comp in text.split("/"):
        if comp.startswith("-"):
            return f"{what} に '-' で始まる要素がある"
    return None


def cmd_d21(rel, name):
    problem = d21_problem(rel, "パス")
    if problem is None and name is not None:
        if name == "":
            problem = "タスク名が空"
        elif name.startswith("-"):
            problem = "タスク名が '-' で始まる"
        else:
            problem = d21_problem(name, "タスク名")
    if problem:
        fail(1, problem)


def cmd_plugin_json(path):
    try:
        data = json.load(open(path, "rb"))
        name, version = data["name"], data["version"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        fail(1, f"plugin.json を読めない: {exc}")
    if not isinstance(name, str) or not isinstance(version, str):
        fail(1, "plugin.json の name・version が文字列でない")
    print(name)
    print(version)


def cmd_profile(path):
    try:
        import yaml
    except ImportError:
        fail(3, "PyYAML が無い")
    try:
        data = yaml.safe_load(open(path, "rb"))
    except yaml.YAMLError as exc:
        fail(1, f"YAML の構文エラー: {exc}")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        fail(1, "profile の最上位がマッピングでない")
    root = data.get("root")
    if root is not None:
        if not isinstance(root, str):
            fail(1, "root が文字列でない")
        if root not in (".", "./"):
            fail(4, f"root が '.' でない(parent-child 構成): {root}")
    loop = {}
    features = data.get("features")
    if features is not None:
        if not isinstance(features, dict):
            fail(1, "features がマッピングでない")
        value = features.get("loop")
        if value is not None:
            if not isinstance(value, dict):
                fail(1, "features.loop がマッピングでない")
            loop = value
    for key in LOOP_KEYS:
        value = loop.get(key)
        if value is None:
            continue
        # bool は int の派生なので type で比べる(PyYAML の yes・true は bool)
        if type(value) is not int:
            fail(1, f"features.loop.{key} が整数でない: {value!r}({type(value).__name__})")
        if value <= 0:
            fail(1, f"features.loop.{key} が 0 以下: {value}")
        if value > 999999999:
            fail(1, f"features.loop.{key} が大きすぎる: {value}")
        print(f"{key}={value}")
    unknown = sorted(str(k) for k in loop if k not in LOOP_KEYS)
    if unknown:
        print("unknown=" + ",".join(re.sub(r"[\x00-\x1f\x7f]", "?", k) for k in unknown))
    if "task_dir" in data and data["task_dir"] is not None:
        value = data["task_dir"]
        # task-directory.md の「優先順位 2」の型の規則(残りは resolve-task-dir.py が worktree の中で検査する)
        if not isinstance(value, str):
            fail(1, f"task_dir が文字列でない: {value!r}")
        if value == "":
            fail(1, "task_dir が空文字")
        if value.startswith("/"):
            fail(1, "task_dir が絶対パス")
        if PLACEHOLDER.search(value):
            fail(1, f"task_dir にテンプレートの置換漏れ({{{{...}}}})がある: {value}")
        if os.path.normpath(value).split("/")[0] == "..":
            fail(1, f"task_dir が管理ルートの外を指す: {value}")
        problem = d21_problem(value, "task_dir")
        if problem:
            fail(5, f"task_dir が D21 に外れる({problem}): {value!r}")
        print("task_dir=" + value)


def cmd_help_check(path, *tokens):
    text = open(path, "rb").read().decode("utf-8", "replace")
    missing = [t for t in tokens if not re.search(r"(?<![A-Za-z0-9_-])" + re.escape(t) + r"(?![A-Za-z0-9_-])", text)]
    for t in missing:
        print(t)
    sys.exit(1 if missing else 0)


def cmd_plugins(path, mine):
    # `plugin list --json` の出力の形(#68 の実走で測った — design §7-3): トップは配列で、要素のキーは
    # `id`(`<名>@<marketplace>`)・`version`・`scope`・`enabled`・`installPath`・`installedAt`・`lastUpdated`
    # (`name` のキーは無い)。読み方はこの形より広く取る: 配列(か、配列を持つオブジェクト)の要素から
    # name(無ければ id の @ の前)・version・enabled を読む。読めない形は「解析できない」にして止める
    # (黙って素通りしない)
    try:
        data = json.load(open(path, "rb"))
    except (OSError, ValueError):
        sys.exit(2)
    items = data if isinstance(data, list) else None
    if isinstance(data, dict):
        for key in ("plugins", "installed", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    if items is None:
        sys.exit(2)
    found = []
    for item in items:
        if not isinstance(item, dict):
            sys.exit(2)
        name = item.get("name")
        if not isinstance(name, str):
            ident = item.get("id")
            if not isinstance(ident, str):
                sys.exit(2)
            name = ident.split("@", 1)[0]
        if name != "dev-workflow":
            continue
        if item.get("enabled", True) is False:
            continue
        version = item.get("version")
        if not isinstance(version, str):
            sys.exit(2)
        found.append(version)
    if not found:
        print("none")
    elif all(v == mine for v in found):
        print("same " + mine)
    else:
        print("mismatch " + ",".join(sorted(set(found))))


def one_line(text, limit=500):
    return re.sub(r"[\x00-\x1f\x7f]", " ", text)[:limit]


def cmd_result(path):
    raw = open(path, "rb").read().decode("utf-8", "replace")
    try:
        obj = json.loads(raw)
    except ValueError:
        print("json=bad")
        return
    # --output-format json の出力は、結果の 1 つのオブジェクトか、全メッセージの配列(利用者の設定の verbose が
    # 効いたとき。最後の要素が type: "result")。配列なら最後の type: "result" の要素を結果として読む。
    # 拒否の欄は、配列ならすべての type: "result" の要素から順につなぐ(周の中でバックグラウンドに委託すると、
    # result の要素がターンごとに出て、拒否はそのターンの要素にだけ載り、最後の要素の欄は空になる — 実測)
    results = [obj]
    if isinstance(obj, list):
        results = [e for e in obj if isinstance(e, dict) and e.get("type") == "result"]
        obj = results[-1] if results else None
    if not isinstance(obj, dict):
        print("json=bad")
        return
    print("json=ok")
    text = obj.get("result") if isinstance(obj.get("result"), str) else ""
    last = None
    for line in text.split("\n"):
        m = OUTCOME.match(line.rstrip("\r"))
        if m:
            last = m
    if last:
        print("outcome=" + last.group(1))
        print("detail=" + one_line(last.group(2)))
    # 拒否の鍵("denial" を含む鍵)ごとに、値を要素の順に集める。鍵を持つ要素が 1 つならその値のまま(オブジェクトの
    # ときと同じ形)。複数なら、どれもリストのときはつないだリスト、そうでなければ値を順に並べたリストにする
    collected = {}
    for e in results:
        for k, v in e.items():
            if "denial" in str(k).lower():
                collected.setdefault(k, []).append(v)
    denials = {}
    for k, vs in collected.items():
        if len(vs) == 1:
            denials[k] = vs[0]
        elif all(isinstance(v, list) for v in vs):
            denials[k] = [x for v in vs for x in v]
        else:
            denials[k] = vs
    if denials:
        print("denials=" + one_line(json.dumps(denials, ensure_ascii=False), 2000))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fs_entry(full, rel):
    st = os.lstat(full)
    if stat.S_ISLNK(st.st_mode):
        return [rel, "l", "", os.readlink(full)]
    mode = oct(stat.S_IMODE(st.st_mode))
    if stat.S_ISDIR(st.st_mode):
        return [rel, "d", mode, ""]
    if stat.S_ISREG(st.st_mode):
        return [rel, "f", mode, sha256_file(full)]
    return [rel, "o", oct(st.st_mode), ""]


def snap_config(git, path):
    if not os.path.lexists(path):
        return {"state": "absent"}
    proc = subprocess.run(git + ["config", "--file", path, "--no-includes", "--list", "-z"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        try:
            digest = sha256_file(path)
        except OSError:
            digest = ""
        return {"state": f"error:{proc.returncode}", "digest": digest}
    entries = []
    for rec in proc.stdout.split(b"\0"):
        if rec == b"":
            continue
        key, sep, value = rec.partition(b"\n")
        entries.append([key.decode("utf-8", "surrogateescape"),
                        value.decode("utf-8", "surrogateescape") if sep else None])
    return {"state": "ok", "entries": entries}


def snap_hooks(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"state": "absent"}
    if stat.S_ISLNK(st.st_mode):
        return {"state": "link", "target": os.readlink(path)}
    if not stat.S_ISDIR(st.st_mode):
        return {"state": "other", "mode": oct(st.st_mode)}
    entries = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames.sort()
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            entries.append(fs_entry(full, os.path.relpath(full, path)))
    entries.sort()
    return {"state": "dir", "mode": oct(stat.S_IMODE(st.st_mode)), "entries": entries}


def snap_info(path):
    entries = []
    for name in ("exclude", "attributes", "sparse-checkout", "grafts"):
        full = os.path.join(path, name)
        if not os.path.lexists(full):
            entries.append([name, "absent", "", ""])
            continue
        e = fs_entry(full, name)
        e[2] = ""  # info/ はバイト列だけを比べる(モードは比べない)
        entries.append(e)
    return {"state": "ok", "entries": entries}


def cmd_snapshot(out, common, repoadmin, wtadmin, *git):
    git = list(git)
    snap = {
        # --repo の管理パスも保存する。別の worktree で中断後に起動したとき、
        # 設定が同じでも、前の起動元を照合できたとは扱わない(H20)。
        "repo:git-dir": {"state": "ok", "path": repoadmin},
        "repo:config.worktree": snap_config(git, os.path.join(repoadmin, "config.worktree")),
        "config": snap_config(git, os.path.join(common, "config")),
        "config.worktree": snap_config(git, os.path.join(common, "config.worktree")),
        "hooks": snap_hooks(os.path.join(common, "hooks")),
        "info": snap_info(os.path.join(common, "info")),
    }
    if wtadmin != "-":
        snap["wt:config.worktree"] = snap_config(git, os.path.join(wtadmin, "config.worktree"))
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="ascii") as fh:
        json.dump(snap, fh, ensure_ascii=True, sort_keys=True)
    os.replace(tmp, out)


def show(value):
    return one_line(json.dumps(value, ensure_ascii=False), 300)


def entry_text(key, value):
    return key if value is None else f"{key}={value}"


def cmd_compare(base_path, cur_path):
    base = json.load(open(base_path, encoding="ascii"))
    cur = json.load(open(cur_path, encoding="ascii"))
    diffs = []
    for key in sorted(set(base) | set(cur)):
        bv, cv = base.get(key), cur.get(key)
        if bv == cv:
            continue
        if key == "repo:git-dir":
            reason = "保存済み状態に人の管理パスが無い" if bv is None else "人の管理パスが変わった"
            diffs.append(f"{key}: {show(bv)} → {show(cv)} ({reason}。前の起動元の設定を確認する。未変更とは判定しない)")
            continue
        if key == "repo:config.worktree" and bv is None:
            diffs.append(f"{key}: 保存済み状態に人の設定の控えが無い → {show(cv)}"
                         " (前の起動元の設定を確認する。現在の設定で中断前の基準を補完しない)")
            continue
        if bv and cv and bv.get("state") == cv.get("state") and "entries" in bv and "entries" in cv:
            be = [tuple(e) for e in bv["entries"]]
            ce = [tuple(e) for e in cv["entries"]]
            # config・config.worktree は項目の並びで比べ、追加・削除・値・並べ替えのどの変化も差分にする(D14・#134)
            if key in ("config", "config.worktree", "wt:config.worktree", "repo:config.worktree"):
                bl = [entry_text(*e) for e in be]
                cl = [entry_text(*e) for e in ce]
                for line in difflib.unified_diff(bl, cl, lineterm="", n=0):
                    if not line.startswith(("---", "+++", "@@")):
                        diffs.append(f"{key}: {one_line(line, 300)}")
            else:
                bset, cset = set(be), set(ce)
                for e in sorted(bset - cset):
                    diffs.append(f"{key}: - {show(list(e))}")
                for e in sorted(cset - bset):
                    diffs.append(f"{key}: + {show(list(e))}")
            if bv.get("mode") != cv.get("mode"):
                diffs.append(f"{key}: モード {bv.get('mode')} → {cv.get('mode')}")
            continue
        diffs.append(f"{key}: {show(bv)} → {show(cv)}")
    for line in diffs:
        print("差分: " + line)
    sys.exit(1 if diffs else 0)


def supervisor_identity(pid):
    # proc の PID は再利用される。親 PID と開始時刻を同じ read で照合する。
    fd = os.open(f"/proc/{pid}/stat", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise ValueError("supervisor の状態が上限を超えた")
        fields = raw[raw.rindex(b")") + 2:].split()
        return int(fields[1]), int(fields[19]), fields[0]
    finally:
        os.close(fd)


def cmd_supervisor_control(mode, pid, parent, start=""):
    import signal
    pid, parent = int(pid), int(parent)
    if pid <= 1 or parent <= 1 or mode not in ("capture", "alive", "term", "kill"):
        fail(9, "supervisor の照合引数が不正")
    try:
        before = supervisor_identity(pid)
        if before[0] != parent or before[2] == b"Z":
            sys.exit(3)
        if mode == "capture":
            print(before[1]); return
        if before[1] != int(start):
            sys.exit(3)
        if mode == "alive":
            return
        fd = os.pidfd_open(pid)
        try:
            if supervisor_identity(pid) != before:
                sys.exit(3)
            signal.pidfd_send_signal(fd, signal.SIGTERM if mode == "term" else signal.SIGKILL)
        finally:
            os.close(fd)
    except (ProcessLookupError, FileNotFoundError):
        # 消滅と読取拒否を区別する。PermissionError 等は不明として上位で止める。
        sys.exit(3)


def cmd_supervisor_result(path, nonce):
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
            fail(1, "supervisor の結果が通常ファイルでない")
        raw = os.read(fd, 4097)
        data = json.loads(raw)
        if data.get("nonce") != nonce or data.get("clean") is not True or type(data.get("returncode")) is not int or type(data.get("timed_out")) is not bool:
            fail(1, "supervisor の結果が保持値と違う")
        print(data["returncode"], int(data["timed_out"]))
    finally:
        os.close(fd)


def cmd_json_get(key):
    try:
        value = json.load(sys.stdin)[key]
    except (ValueError, KeyError, TypeError):
        sys.exit(1)
    if not isinstance(value, str):
        sys.exit(1)
    print(value)


def cmd_json_value(key):
    print(json.dumps(json.load(sys.stdin)[key], ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def cmd_init_names():
    value = json.load(sys.stdin)
    if not isinstance(value, dict) or not isinstance(value.get("public_names"), list):
        fail(1, "init の証拠が不正")
    for name in value["public_names"]:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9:_-]*", name):
            fail(1, "init の公開名が不正")
        print(name)


HELP_OPT = re.compile(r"^  ((?:-[A-Za-z0-9]|--[A-Za-z0-9][A-Za-z0-9-]*)(?:, (?:-[A-Za-z0-9]|--[A-Za-z0-9][A-Za-z0-9-]*))*)"
                      r"(?: (<[^>]*>|\[[^\]]*\]))?")


def cmd_help_values(path):
    # --help の定義の行(先頭の空白が 2 つ)で `<…>` の値の表記を持つフラグの名を 1 行ずつ出す(D20 の値を取るフラグの表)
    text = open(path, "rb").read().decode("utf-8", "replace")
    names = set()
    for line in text.split("\n"):
        m = HELP_OPT.match(line)
        if m and m.group(2) and m.group(2).startswith("<"):
            names.update(n.strip() for n in m.group(1).split(","))
    for n in sorted(names):
        print(n)


def cmd_hook_settings(python_bin, script, guard_sha, guard, state, state_sha, fixed="{}"):
    # command 自体に保持した loader と hash を置き、改変された helper を先に実行しない。
    def quote(p):
        return "'" + p.replace("'", "'\\''") + "'"
    argv = [python_bin, '-I', '-B', '-c', ENVIRONMENT_LOADER, guard_sha, guard,
            'hook', '--state', state, '--expect-sha256', state_sha,
            '--path', 'skills/ship-task/scripts/loop-permission.py']
    settings = {"hooks": {"PermissionRequest": [{"matcher": "*", "hooks": [
        {"type": "command", "command": ' '.join(map(quote, argv))}]}]}}
    policy = json.loads(fixed)
    if not isinstance(policy, dict) or set(policy) - {"disableBundledSkills", "syncClaudeAiSkills", "syncClaudeAiPlugins", "skillOverrides"}:
        fail(1, "固定設定の形が違う")
    settings.update(policy)
    print(json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def cmd_permlog(path):
    # 子が書ける参考表示。内容・欠落・エラーのどれも制御判断に使わない。
    allow = deny = 0
    kinds, lines = {}, []
    issue = ''
    fds = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY); fds.append(fd)
        parts = os.path.abspath(path).split('/')[1:]
        for name in parts[:-1]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd); fds.append(fd)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd); fds.append(fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 1048576:
            raise ValueError('通常ファイルでないか読取上限を超える')
        raw = b''
        while len(raw) < st.st_size:
            chunk = os.read(fd, min(65536, st.st_size - len(raw)))
            if not chunk: raise ValueError('読取中に切り詰められた')
            raw += chunk
        after = os.fstat(fd)
        if os.read(fd, 1) or (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns):
            raise ValueError('読取中に変わった')
        if raw and not raw.endswith(b'\n'):
            raise ValueError('末尾の行が途中で切れている')
        records = raw.decode('utf-8').splitlines()
        if len(records) > 10000: raise ValueError('行数上限を超える')
        for raw_line in records:
            e = json.loads(raw_line)
            if not isinstance(e, dict) or e.get('decision') not in ('allow', 'deny'):
                raise ValueError('行の形式が違う')
            if e['decision'] == 'allow':
                allow += 1
            else:
                deny += 1
                kind = e.get('kind', 'other')
                if not isinstance(kind, str): raise ValueError('kind が文字列でない')
                kind = one_line(kind, 100)
                kinds[kind] = kinds.get(kind, 0) + 1
                if len(lines) < 100:
                    lines.append(one_line(f"{e.get('tool_name')} [{kind}] {e.get('reason')} — {e.get('subject')}", 600))
    except (OSError, ValueError, UnicodeError) as exc:
        issue = one_line(str(exc), 300)
    finally:
        for fd in reversed(fds): os.close(fd)
    print(f'allow={allow}')
    print(f'deny={deny}')
    print('kinds=' + ','.join(f'{k}:{v}' for k,v in sorted(kinds.items())))
    if issue: print('reference-error=' + issue)
    for line in lines: print('line=' + line)


def cmd_origin_json():
    # origin-repo.py の出力(discover-mode.md §3)を読む。欄が欠けるか型が違えば 1(URL の字面は出力に無い)
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except ValueError:
        sys.exit(1)
    if not isinstance(data, dict) or not all(isinstance(data.get(k), bool) for k in ("origin", "same", "vcs")):
        sys.exit(1)
    reason = data.get("reason")
    repo = data.get("repo")
    if repo is not None and not isinstance(repo, str):
        sys.exit(1)
    for key in ("origin", "same", "vcs"):
        print(f"{key}={1 if data[key] else 0}")
    print("reason=" + one_line(reason if isinstance(reason, str) else "", 300))
    print("repo=" + (repo or ""))


def cmd_candiff(task_dir):
    # 発見の周の差分(`git diff --no-renames --raw -z <固定した sha> HEAD`)を stdin で読む。全行が状態 A・モード 100644・
    # task_dir の直下の `候補_<名>.md`(<名> が空でない・D21)で 1 件以上なら、パスを 1 行ずつ出す。外れれば理由を出して 1
    parts = sys.stdin.buffer.read().split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    prefix = "" if task_dir == "." else task_dir + "/"
    paths, i = [], 0
    while i < len(parts):
        head = parts[i].decode("utf-8", "surrogateescape")
        fields = head[1:].split(" ") if head.startswith(":") else []
        if len(fields) != 5 or i + 1 >= len(parts):
            fail(1, "差分の形を読めない")
        path = parts[i + 1].decode("utf-8", "surrogateescape")
        i += 2
        shown = one_line(path, 300)
        if fields[4] != "A":
            fail(1, f"追加でない変更がある({fields[4]} {shown})")
        if fields[1] != "100644":
            fail(1, f"モードが 100644 でない({fields[1]} {shown})")
        rest = path[len(prefix):] if path.startswith(prefix) else None
        if rest is None or "/" in rest:
            fail(1, f"task_dir の直下でない({shown})")
        if not (rest.startswith("候補_") and rest.endswith(".md")):
            fail(1, f"名前が 候補_<名>.md でない({shown})")
        name = rest[len("候補_"):-len(".md")]
        if name == "":
            fail(1, f"<名> が空({shown})")
        problem = d21_problem(path, "パス")
        if problem is None and name.startswith("-"):
            problem = "<名> が '-' で始まる"
        if problem:
            fail(1, f"D21 に外れる — {problem}({shown})")
        paths.append(path)
    if not paths:
        fail(1, "候補の追加が無い")
    for p in paths:
        print(p)


ENVIRONMENT_LOADER = 'import os,sys,stat,re,hashlib,json,signal\n\ndef load_guard(expected, path):\n    if not re.fullmatch("[a-f0-9]{64}", expected):\n        raise RuntimeError("hash")\n    parts = path.split("/")\n    if not path.startswith("/") or len(parts) > 129 or any(p in ("", ".", "..") for p in parts[1:]):\n        raise RuntimeError("path")\n    def expired(*unused):\n        raise RuntimeError("timeout")\n    def identity(st):\n        return st.st_dev, st.st_ino, st.st_mode\n    def version(st):\n        return identity(st), st.st_size, st.st_mtime_ns, st.st_ctime_ns\n    previous = signal.signal(signal.SIGALRM, expired)\n    signal.setitimer(signal.ITIMER_REAL, 15)\n    fd = None\n    try:\n        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)\n        for name in parts[1:-1]:\n            before = os.stat(name, dir_fd=fd, follow_symlinks=False)\n            if not stat.S_ISDIR(before.st_mode):\n                raise RuntimeError("directory")\n            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)\n            os.close(fd); fd = child\n            if identity(before) != identity(os.fstat(fd)):\n                raise RuntimeError("directory changed")\n        before = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)\n        if not stat.S_ISREG(before.st_mode) or before.st_size > 1048576:\n            raise RuntimeError("file")\n        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)\n        try:\n            if version(before) != version(os.fstat(child)):\n                raise RuntimeError("file changed")\n            raw = b""\n            while len(raw) < before.st_size:\n                chunk = os.read(child, min(65536, before.st_size - len(raw)))\n                if not chunk:\n                    raise RuntimeError("short read")\n                raw += chunk\n            if os.read(child, 1) or version(before) != version(os.fstat(child)):\n                raise RuntimeError("file changed")\n            if version(before) != version(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)):\n                raise RuntimeError("path changed")\n            if hashlib.sha256(raw).hexdigest() != expected:\n                raise RuntimeError("hash mismatch")\n            return raw\n        finally:\n            os.close(child)\n    finally:\n        if fd is not None:\n            os.close(fd)\n        signal.setitimer(signal.ITIMER_REAL, 0)\n        signal.signal(signal.SIGALRM, previous)\n\nargs = []\ntry:\n    expected, path, *args = sys.argv[1:]\n    raw = load_guard(expected, path)\n    sys.argv = [path, *args]\n    exec(compile(raw, path, "exec"), {"__name__":"__main__", "__file__":path})\nexcept (Exception, SystemExit) as exc:\n    if isinstance(exc, SystemExit) and exc.code in (0, None):\n        raise\n    if args[:1] == ["hook"]:\n        print(json.dumps({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"deny","message":"環境の保持値との照合に失敗"}}}))\n        raise SystemExit(0)\n    print("ERROR [environment-guard] 保持した検査用コピーを安全に実行できない", file=sys.stderr)\n    raise SystemExit(20)\n'

def cmd_environment_run(expected, path, *args):
    sys.argv = ['environment-loader', expected, path, *args]
    exec(compile(ENVIRONMENT_LOADER, '<environment-loader>', 'exec'), {'__name__':'__main__'})


COMMANDS = {
    "environment-run": cmd_environment_run,
    "d21": lambda rel, *name: cmd_d21(rel, name[0] if name else None),
    "plugin-json": cmd_plugin_json, "profile": cmd_profile, "help-check": cmd_help_check,
    "plugins": cmd_plugins, "result": cmd_result, "snapshot": cmd_snapshot, "compare": cmd_compare,
    "json-value": cmd_json_value, "init-names": cmd_init_names,
    "supervisor-control": cmd_supervisor_control, "supervisor-result": cmd_supervisor_result, "json-get": cmd_json_get, "help-values": cmd_help_values,
    "hook-settings": cmd_hook_settings, "permlog": cmd_permlog,
    "origin-json": cmd_origin_json, "candiff": cmd_candiff,
}
try:
    COMMANDS[sys.argv[1]](*sys.argv[2:])
except SystemExit:
    raise
except Exception as exc:  # 想定外の失敗は 9(呼び出し側は「差分あり」や成功に読み替えない)
    print(f"補助の失敗: {exc!r}", file=sys.stderr)
    sys.exit(9)
PY
py() {
  local isolation=() code="$PY_HELPER"
  case "${1:-}" in
    taskinfo|holdcount|holdcode)
      [ "${TASK_TEXT_HELD:-0}" = 1 ] || die 20 environment "本文解析を保持していない"
      code="$TASK_TEXT_CODE" ;;
  esac
  # 固定検査と hook の組立は stdlib だけを使い、cwd/PYTHONPATH/user-site を探索しない。
  # profile の PyYAML など、ほかの補助処理の利用者 site は従来どおり使う。
  case "${1:-}" in environment-run|hook-settings) isolation=(-I) ;; esac
  ( cd / && exec python3 "${isolation[@]}" -c "$code" "$@" 7>&- )
}

# ── 報告(状態ディレクトリの <実行 ID>/report.md。要約は stdout)──
rep() { printf '%s\n' "$@" >>"$REPORT"; }
say() { printf '%s\n' "$@"; }

die() { # $1=終了コード $2=理由コード 残り=説明
  local code="$1" reason="$2"; shift 2
  printf 'ERROR [%s] %s\n' "$reason" "$*" >&2
  if [ -n "$REPORT" ]; then printf -- '- ERROR [%s] %s\n' "$reason" "$*" >>"$REPORT" 2>/dev/null || true; fi
  finish "$code" "ERROR [$reason] $*"
}

stop_mark_guide() {
  if [ "$STATE_GIT_UNSAFE" = 1 ] && [ -d "$INFLIGHT" ]; then
    printf '止めの印: %s(未確認の設定・子・作業場所・中断記録を人が確認する。印だけを消しても再開しない)' "$STOP_MARK"
  else
    printf '止めの印: %s(差分を確かめ、必要なら元に戻してから、このファイルを消すと次の起動が続く)' "$STOP_MARK"
  fi
}

# 止めの印を置く。$3=1 なら周の途中の印を残す(片付けが済んでいないとき。止めの印を消した後の起動で、
# 外部から確認して再開する)。0 なら周の途中の印を消す(止めの印が差分を持つので役目が終わる)
place_stop_mark() { # $1=理由 $2=差分(複数行) $3=周の途中の印を残すか
  local tmp="$STOP_MARK.tmp.$$"
  {
    printf '# 無人ループの止めの印\n\n'
    printf -- '- 置いた日時: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf -- '- 実行 ID: %s\n' "$RUN_ID"
    printf -- '- 理由: %s\n' "$1"
    [ -z "$ITER_ID" ] || printf -- '- 周: %s(%s)\n' "$ITER_ID" "$ITER_REL"
    printf '\n## 差分\n\n'
    if [ -n "$2" ]; then printf '%s\n' "$2"; else printf '(無し)\n'; fi
    printf '\n## 消し方\n\n'
    if [ "$3" = 1 ] && [ "$STATE_GIT_UNSAFE" = 1 ]; then
      printf '設定・管理構造を確認できないため、子と中断記録(%s)を自動で片付けていない。\n' "$INFLIGHT"
      printf '人が実際の子・作業場所・差分を確認して片付け、中断記録を証拠として退避する。その後で止めの印を外す。印だけを消しても再開しない。\n'
    else
      printf '差分を確かめ、必要なら元に戻してから、このファイルを消す: rm -- %q\n' "$STOP_MARK"
    fi
    if [ "$3" = 1 ] && [ "$STATE_GIT_UNSAFE" != 1 ]; then
      printf '周の途中の印(%s)は残す。別の信頼領域から子の不在と共有状態を確認するまで再開しない\n' "$INFLIGHT"
    fi
  } >"$tmp"
  mv -f -- "$tmp" "$STOP_MARK"
  STOP_MARK_WRITTEN=1
  if [ "$3" != 1 ]; then rm -rf -- "$INFLIGHT"; fi
}

take_snapshot() { # $1=出力 $2=その周の worktree の管理ディレクトリ(無ければ -) $3=保持済みstate $4=別実行間のconfig基準を更新するか $5=親削除記録を使わない
  local own=() prior=() fresh=() control=() removed=() directory path rc=0 err
  SNAPSHOT_DIFF=""
  verify_environment || die 20 environment "状態観察 helper の直前に環境が変わった"
  [ -z "$ITER_WT" ] || own=(--exclude-worktree "$ITER_WT")
  [ -z "${3:-}" ] || prior=(--secret-patterns-from "$3")
  [ "${4:-0}" != 1 ] || fresh=(--fresh-config-baseline)
  if [ -n "$STOP_CONTROL_WORKTREE" ]; then
    control=(--stop-control-worktree "$STOP_CONTROL_WORKTREE" --stop-control-rel "$STOP_CONTROL_REL")
    for directory in "${STOP_CONTROL_NEW_DIRS[@]}"; do control+=(--stop-control-new-dir "$directory"); done
  fi
  if [ "$2" = - ] && [ "${5:-0}" != 1 ]; then
    for path in "${REMOVED_PARENT_WORKTREES[@]}"; do removed+=(--removed-parent-worktree "$path"); done
  fi
  err="$RUN_DIR/loop-state-snapshot.err"
  "$PY_ABS" "$LOOP_STATE_PY" snapshot --out "$1" --top "$TOP" --common "$COMMON" --repo-admin "$REPO_GIT_DIR" --wt-admin "$2" "${own[@]}" \
    "${prior[@]}" "${fresh[@]}" "${control[@]}" "${removed[@]}" \
    --max-items "$STATE_MAX_ITEMS" --max-bytes "$STATE_MAX_BYTES" --max-file-bytes "$STATE_MAX_FILE_BYTES" --max-seconds "$STATE_MAX_SECONDS" \
    2>"$err" || rc=$?
  if [ "$rc" -ne 0 ]; then
    STATE_GIT_UNSAFE=1
    if grep -qE 'config|origin|git-dir|commondir|HEAD|worktree' "$err" 2>/dev/null; then
      SNAPSHOT_DIFF="差分: 保持済み config または worktree の構造が周の途中で変わった(内容は出さない)"
    else
      SNAPSHOT_DIFF="差分: 今の状態を安全に控えられない(照合できない)"
    fi
    cat "$err" >&2 2>/dev/null || true
    return "$rc"
  fi
  return 0
}

environment_call() {
  py environment-run "$ENV_GUARD_SHA" "$TRUSTED_ENV_GUARD" "$@"
}
host_settings_call() { # $1=host-settings | host-allowlist, 残り=reader の引数
  local command="$1" err rc=0 code
  shift
  err="$(mktemp "$ENV_TEMP/host-settings.XXXXXXXX.err")" || die 20 host-settings "ホスト設定の診断先を作れない"
  HOST_SETTINGS_REPLY="$(environment_call "$command" --state "$ENVIRONMENT_STATE" --expect-sha256 "$ENVIRONMENT_SHA" "$@" 2>"$err")" || rc=$?
  [ "$rc" -eq 0 ] && return 0
  code="$(sed -n 's/^ERROR \[\(host-config-path\|host-settings\|host-config-source\|hooks-disabled\)\].*/\1/p' "$err" | head -n 1)"
  case "$code" in host-config-path|host-settings|host-config-source|hooks-disabled) : ;; *) code=host-settings ;; esac
  die 20 "$code" "ホスト設定を安全に検査できない"
}
verify_environment() {
  [ -n "${ENVIRONMENT_SHA:-}" ] || return 0
  environment_call verify --state "$ENVIRONMENT_STATE" --expect-sha256 "$ENVIRONMENT_SHA" || { STATE_GIT_UNSAFE=1; return 1; }
  if [ "${ENV_INVENTORY_READY:-0}" -eq 1 ]; then
    local current
    current="$(mktemp "$ENV_TEMP/plugins-current.XXXXXXXX")" || { STATE_GIT_UNSAFE=1; return 1; }
    aux "$current" "${AUX_PLUGINS[@]}" || { STATE_GIT_UNSAFE=1; return 1; }
    environment_call verify --state "$ENVIRONMENT_STATE" --expect-sha256 "$ENVIRONMENT_SHA" --current-inventory "$current" || { STATE_GIT_UNSAFE=1; return 1; }
  fi
}
hold_task_text() {
  local candidate
  # Bashへ格納する前にNUL等を拒否する。末尾改行も照合し、read失敗を隠さない。
  candidate="$(environment_call read --state "$ENVIRONMENT_STATE" --expect-sha256 "$ENVIRONMENT_SHA" \
    --path skills/ship-task/scripts/loop-task-text.py | (cd / && "$PY_ABS" -I -c '
import sys
raw = sys.stdin.buffer.read()
if not raw or b"\0" in raw:
    sys.exit(20)
try:
    raw.decode("utf-8")
except UnicodeError:
    sys.exit(20)
sys.stdout.buffer.write(raw)
' 7>&-) && printf '.')" || die 20 environment "本文解析のコピーを読めない"
  candidate="${candidate%.}"
  if [ "$TASK_TEXT_HELD" = 1 ]; then
    [ "$candidate" = "$TASK_TEXT_CODE" ] || die 20 environment "本文解析のコピーが初回と異なる"
  else
    TASK_TEXT_CODE="$candidate"
    TASK_TEXT_HELD=1
  fi
}
bind_environment() { # bootstrap の出力だけから保持する。過去の状態から復元しない。
  local receipt="$1"
  ENVIRONMENT_STATE="$(printf '%s' "$receipt" | py json-get state)"
  ENVIRONMENT_SHA="$(printf '%s' "$receipt" | py json-get sha256)"
  TRUSTED_ENV_GUARD="$(printf '%s' "$receipt" | py json-get guard)"
  ENV_GUARD_SHA="$(printf '%s' "$receipt" | py json-get guard_sha256)"
  PLUGIN_ROOT="$(printf '%s' "$receipt" | py json-get plugin)"
  hold_task_text
  RESOLVER="$PLUGIN_ROOT/skills/create-task/scripts/resolve-task-dir.py"
  PERM_SCRIPT="$PLUGIN_ROOT/skills/ship-task/scripts/loop-permission.py"
  ORIGIN_REPO_PY="$PLUGIN_ROOT/skills/ship-task/scripts/origin-repo.py"
  GIT_CONFIG_DIGEST_PY="$PLUGIN_ROOT/skills/ship-task/scripts/git-config-digest.py"
  LOOP_STATE_PY="$PLUGIN_ROOT/skills/ship-task/scripts/loop-state.py"
  LOOP_STARTUP_PY="$PLUGIN_ROOT/skills/ship-task/scripts/loop-startup.py"
  HOST_ARGV_PY="$PLUGIN_ROOT/skills/ship-task/scripts/host-argv.py"
  SUPERVISOR="$PLUGIN_ROOT/skills/ship-task/scripts/loop-supervisor.py"
  HOST_CHECK_PY="$PLUGIN_ROOT/skills/ship-task/scripts/host-check.py"
  for f in "$RESOLVER" "$PERM_SCRIPT" "$ORIGIN_REPO_PY" "$GIT_CONFIG_DIGEST_PY" "$LOOP_STATE_PY" "$LOOP_STARTUP_PY" "$HOST_ARGV_PY" "$SUPERVISOR" "$HOST_CHECK_PY"; do
    [ -f "$f" ] || die 20 plugin-root "${f##*/} が無い: $f"
  done
  HOOK_SETTINGS="$(py hook-settings "$PY_ABS" "$PERM_SCRIPT" "$ENV_GUARD_SHA" "$TRUSTED_ENV_GUARD" "$ENVIRONMENT_STATE" "$ENVIRONMENT_SHA" "$FIXED_POLICY")"
}

# H46: 設定と定義は、補助CLIより前とactive一覧の確定後に同じreaderで検査する。
component_check() {
  local err="${RUN_DIR:-$ENV_TEMP}/component-policy.err" rc=0
  local args=() v
  for v in ${ALLOWED_TOOLS[@]+"${ALLOWED_TOOLS[@]}"}; do args+=(--allowed-tools "$v"); done
  COMPONENT_POLICY="$("$PY_ABS" -I -B "$HOST_CHECK_PY" component-policy --state "$ENVIRONMENT_STATE" \
    --expect-sha256 "$ENVIRONMENT_SHA" "${args[@]}" 2>"$err")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    cat "$err" >&2
    die 20 component-policy "有効な定義と固定設定を安全に検査できない"
  fi
  COMPONENT_SHA="$(printf '%s' "$COMPONENT_POLICY" | py json-get component_sha256)" || die 20 component-policy "定義の保持値が不正"
  POLICY_SHA="$(printf '%s' "$COMPONENT_POLICY" | py json-get policy_sha256)" || die 20 component-policy "固定設定の保持値が不正"
  FIXED_POLICY="$(printf '%s' "$COMPONENT_POLICY" | py json-value fixed)" || die 20 component-policy "固定設定を読めない"
  COMPONENT_RESOLVER="$(printf '%s' "$COMPONENT_POLICY" | py json-value resolver_version)" || die 20 component-policy "名前解決の版を読めない"
  HOOK_SETTINGS="$(py hook-settings "$PY_ABS" "$PERM_SCRIPT" "$ENV_GUARD_SHA" "$TRUSTED_ENV_GUARD" \
    "$ENVIRONMENT_STATE" "$ENVIRONMENT_SHA" "$FIXED_POLICY")" || die 20 component-policy "hook と固定設定を組み立てられない"
  [ "$ALLOW_CLASSIFIER" -eq 0 ] || args+=(--classifier)
  err="${RUN_DIR:-$ENV_TEMP}/skill-grants.err"; rc=0
  GRANTS_OUT="$("$PY_ABS" -I -B "$HOST_CHECK_PY" grants --state "$ENVIRONMENT_STATE" --expect-sha256 "$ENVIRONMENT_SHA" \
    --user-settings "$USER_SETTINGS" "${args[@]}" 2>"$err")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    cat "$err" >&2
    if [ -n "$REPORT" ]; then
      rep "" "## skill・command の allowed-tools(止まった理由)" ""
      sed 's/^/    /' "$err" >>"$REPORT"
    fi
    die 20 skill-grants "skill・command・agent の許可規則が許可リストより広いか、解釈できない"
  fi
  GRANTS_FILES="$(printf '%s' "$GRANTS_OUT" | sed -n 's/^files=\([0-9]*\) .*/\1/p')"
  case "$GRANTS_OUT" in *" yaml=on") GRANTS_YAML="有" ;; *) GRANTS_YAML="無(PyYAML を読めない。自前の厳しい読み方だけ)" ;; esac
}

match_component_proof() {
  local rc=0
  PROOF_INFO="$("$PY_ABS" -I -B "$HOST_CHECK_PY" proof-match --store "$PROOF_STORE" --identity "$HOST_IDENTITY" --shape "$HOST_SHAPE" \
    --version-file "$RUN_DIR/version.txt" --help-file "$RUN_DIR/help.txt" \
    --component-sha256 "$COMPONENT_SHA" --policy-sha256 "$POLICY_SHA" 2>"$RUN_DIR/host-proof.err")" || rc=$?
  [ "$rc" -eq 0 ] || die 20 host-proof "$(head -c 400 "$RUN_DIR/host-proof.err" | tr '\n' ' ')。$PROVE_HINT"
}

recheck_component_proof() {
  local component="$COMPONENT_SHA" policy="$POLICY_SHA" fixed="$FIXED_POLICY"
  component_check
  [ "$component" = "$COMPONENT_SHA" ] && [ "$policy" = "$POLICY_SHA" ] && [ "$fixed" = "$FIXED_POLICY" ] \
    || die 20 host-proof "起動直前に検査済みの定義または固定設定が変わった"
  match_component_proof
}

# 状態 JSON の生バイトを、親ディレクトリから nofollow で開く helper にだけ読ませて
# digest 化する。シェルの `<state` は link/FIFO を開いてしまうため、ここで使わない。
state_digest() { # $1=parent-held state pathname
  local value
  verify_environment || return 1
  value="$("$PY_ABS" "$LOOP_STATE_PY" state-digest --state "$1" \
    --max-items "$STATE_MAX_ITEMS" --max-bytes "$STATE_MAX_BYTES" \
    --max-file-bytes "$STATE_MAX_FILE_BYTES" --max-seconds "$STATE_MAX_SECONDS")" || return 1
  if [[ ! "$value" =~ ^[0-9a-f]{64}$ ]]; then return 1; fi
  printf '%s\n' "$value"
}

# 止止ファイルは状態観察の一部であり、任意パスの除外ではない。人の checkout の
# physical TOP 直下で、開始時に葉が無いときだけ、helper へ狭い descriptor を渡す。
plan_stop_control() { # $1=中断前state（あれば、保持済み descriptor だけを復元する）
  local prior="${1:-}" rel part path prefix missing=0 held_fd held_pid
  local -a held=()
  STOP_CONTROL_WORKTREE=""; STOP_CONTROL_REL=""; STOP_CONTROL_NEW_DIRS=()
  case "$STOP_FILE" in "$TOP"/*) rel="${STOP_FILE#"$TOP"/}" ;; *) return 0 ;; esac
  case "$rel" in ""|/*|*"//"*|.|..|*/.|*/..|../*|*"/../"*) return 0 ;; esac
  IFS=/ read -r -a _stop_parts <<<"$rel"
  path="$TOP"
  for (( _stop_i=0; _stop_i<${#_stop_parts[@]}-1; _stop_i++ )); do
    part="${_stop_parts[$_stop_i]}"
    case "$part" in ""|.|..) return 0 ;; esac
    path="$path/$part"; prefix="${path#"$TOP"/}"
    if [ "$missing" = 1 ]; then
      STOP_CONTROL_NEW_DIRS+=("$prefix")
    elif [ -e "$path" ] || [ -L "$path" ]; then
      :
    else
      missing=1; STOP_CONTROL_NEW_DIRS+=("$prefix")
    fi
  done
  if [ -e "$STOP_FILE" ] || [ -L "$STOP_FILE" ]; then
    # On an interrupted run the leaf may be the exact empty control file that
    # was created after its held baseline.  Restore only that held descriptor;
    # any other path, malformed held state, link, sibling, or nonempty leaf is
    # still observed by loop-state and cannot become a broad exclusion.
    [ -n "$prior" ] || { STOP_CONTROL_NEW_DIRS=(); return 0; }
    # `mapfile` reports a successful EOF even when process substitution
    # failed.  Wait for the bounded reader explicitly before accepting any
    # descriptor from the saved state.
    verify_environment || die 20 environment "停止ファイルの基準を読む直前に環境が変わった"
    exec {held_fd}< <("$PY_ABS" "$LOOP_STATE_PY" stop-control --state "$prior" \
      --max-items "$STATE_MAX_ITEMS" --max-bytes "$STATE_MAX_BYTES" --max-file-bytes "$STATE_MAX_FILE_BYTES" --max-seconds "$STATE_MAX_SECONDS")
    held_pid=$!
    mapfile -d '' -u "$held_fd" -t held
    if ! wait "$held_pid"; then
      exec {held_fd}<&-
      die 20 state-snapshot "中断前の停止ファイル基準を安全に読めない"
    fi
    exec {held_fd}<&-
    if [ "${#held[@]}" -lt 2 ] || [ "${held[0]}" != "$TOP" ] || [ "${held[1]}" != "$rel" ]; then
      STOP_CONTROL_NEW_DIRS=()
      return 0
    fi
    STOP_CONTROL_WORKTREE="${held[0]}"; STOP_CONTROL_REL="${held[1]}"
    STOP_CONTROL_NEW_DIRS=("${held[@]:2}")
    return 0
  fi
  STOP_CONTROL_WORKTREE="$TOP"; STOP_CONTROL_REL="$rel"
}

save_last_verified() { # $1=verify済み生state $2=verify時のsha256。照合に通った観測だけを昇格する
  local verified="${1:-}" verified_sha="${2:-}" now_sha expected expected_sha observed observed_sha out rc=0 removed
  SNAPSHOT_DIFF=""
  if [ -z "$verified" ] || [ -z "$verified_sha" ] || [ ! -f "$verified" ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 検証済みの状態を失ったため最後に照合に通った状態を更新しない"
    return 1
  fi
  now_sha="$(state_digest "$verified" || true)"
  if [ -z "$now_sha" ] || [ "$now_sha" != "$verified_sha" ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 検証済みの状態が照合後に書き換わったため最後に照合に通った状態を更新しない"
    return 1
  fi
  expected="$verified"
  if [ "${#REMOVED_PARENT_WORKTREES[@]}" -gt 1 ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 親が削除した worktree の記録が基準と一致しない"
    return 1
  elif [ "${#REMOVED_PARENT_WORKTREES[@]}" -eq 1 ]; then
    removed="${REMOVED_PARENT_WORKTREES[0]}"
    expected="$RUN_DIR/iter-${ITER_SEQ:-0}.expected-after-remove.json"
    verify_environment || die 20 environment "検証済み状態を派生する直前に環境が変わった"
    if ! "$PY_ABS" "$LOOP_STATE_PY" derive-removed-worktree --state "$verified" --out "$expected" --removed-worktree "$removed" --own-worktree "$ITER_WT" \
      --max-items "$STATE_MAX_ITEMS" --max-bytes "$STATE_MAX_BYTES" --max-file-bytes "$STATE_MAX_FILE_BYTES" --max-seconds "$STATE_MAX_SECONDS"; then
      STATE_GIT_UNSAFE=1
      SNAPSHOT_DIFF="差分: 親が削除した worktree の検証済み基準を安全に作れない"
      return 1
    fi
  fi
  expected_sha="$(state_digest "$expected" || true)"
  if [ -z "$expected_sha" ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 検証済みの期待状態を保持できない"
    return 1
  fi
  # The verified source is parent-held.  Check it again after the helper has
  # consumed it, before observing the post-removal repository once.
  now_sha="$(state_digest "$verified" || true)"
  if [ -z "$now_sha" ] || [ "$now_sha" != "$verified_sha" ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 検証済みの状態が基準の作成中に書き換わったため最後に照合に通った状態を更新しない"
    return 1
  fi
  observed="$RUN_DIR/iter-${ITER_SEQ:-0}.final-observed.json"
  # expected already incorporates the only permitted parent removal.  Do not
  # pass that removal into snapshot again: it must remain a strict comparison.
  if ! take_snapshot "$observed" - "$expected" 0 1; then return 1; fi
  observed_sha="$(state_digest "$observed" || true)"
  if [ -z "$observed_sha" ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 最終観察の状態を保持できない"
    return 1
  fi
  now_sha="$(state_digest "$verified" || true)"
  if [ -z "$now_sha" ] || [ "$now_sha" != "$verified_sha" ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 検証済みの状態が最終観察中に書き換わったため最後に照合に通った状態を更新しない"
    return 1
  fi
  verify_environment || die 20 environment "基準昇格の照合直前に環境が変わった"
  out="$("$PY_ABS" "$LOOP_STATE_PY" compare --before "$expected" --after "$observed" \
    --max-items "$STATE_MAX_ITEMS" --max-bytes "$STATE_MAX_BYTES" --max-file-bytes "$STATE_MAX_FILE_BYTES" --max-seconds "$STATE_MAX_SECONDS")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="$(printf '%s\n' "$out" | sed -n '/^差分: /p')"
    [ -n "$SNAPSHOT_DIFF" ] || SNAPSHOT_DIFF="差分: 検証後の状態を厳密に照合できない"
    return 1
  fi
  now_sha="$(state_digest "$expected" || true)"
  if [ -z "$now_sha" ] || [ "$now_sha" != "$expected_sha" ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 期待状態が最終照合中に書き換わったため最後に照合に通った状態を更新しない"
    return 1
  fi
  now_sha="$(state_digest "$observed" || true)"
  if [ -z "$now_sha" ] || [ "$now_sha" != "$observed_sha" ]; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 最終観察の状態が照合中に書き換わったため最後に照合に通った状態を更新しない"
    return 1
  fi
  if ! mv -f -- "$observed" "$LAST_VERIFIED"; then
    STATE_GIT_UNSAFE=1
    SNAPSHOT_DIFF="差分: 照合済みの状態を最後に照合に通った状態へ安全に昇格できない"
    return 1
  fi
  # 削除の許可はこの照合で消費する。次の周や別の控えへ引き継がない。
  REMOVED_PARENT_WORKTREES=()
}

# 周の supervisor を止め、waitpid による不在証明を確かめる。
cleanup_iteration() {
  local waited=0 result control_rc=0
  CLEANUP_LEFT=""
  if [ -n "$CHILD_PID" ]; then
    if [ -z "$CHILD_START" ]; then
      CLEANUP_LEFT="supervisor の開始時刻を照合できない"
      return
    fi
    py supervisor-control term "$CHILD_PID" "$$" "$CHILD_START" || control_rc=$?
    if [ "$control_rc" -ne 0 ] && [ "$control_rc" -ne 3 ]; then
      CLEANUP_LEFT="supervisor の所有関係を照合できない"
      return
    fi
    while [ "$waited" -lt "$(((KILL_GRACE + 9) * 10))" ]; do
      control_rc=0
      py supervisor-control alive "$CHILD_PID" "$$" "$CHILD_START" || control_rc=$?
      [ "$control_rc" -ne 3 ] || break
      if [ "$control_rc" -ne 0 ]; then
        CLEANUP_LEFT="supervisor の生存を照合できない"
        return
      fi
      if [ "$waited" -eq "$(((KILL_GRACE + 7) * 10))" ]; then
        control_rc=0
        py supervisor-control kill "$CHILD_PID" "$$" "$CHILD_START" || control_rc=$?
        if [ "$control_rc" -ne 0 ] && [ "$control_rc" -ne 3 ]; then
          CLEANUP_LEFT="supervisor の停止対象を照合できない"
          return
        fi
      fi
      "$PY_ABS" -c 'import time; time.sleep(.1)' || break
      waited=$((waited + 1))
    done
    if [ "$control_rc" -ne 3 ]; then
      CLEANUP_LEFT="supervisor の停止を期限内に確認できない"
      return
    fi
    SUPERVISOR_RC=0
    wait "$CHILD_PID" 2>/dev/null || SUPERVISOR_RC=$?
    CHILD_PID=""; CHILD_START=""
  fi
  if [ "$SUPERVISOR_RC" -ne 0 ] || [ -z "$SUPERVISOR_RESULT" ]; then
    CLEANUP_LEFT="supervisor の不在証明が無い"
    return
  fi
  if ! result="$(py supervisor-result "$SUPERVISOR_RESULT" "$SUPERVISOR_NONCE")"; then
    CLEANUP_LEFT="supervisor の回収結果を照合できない"
    return
  fi
  read -r rc timed_out <<<"$result"
  if [ -n "${DEV_WORKFLOW_LOOP_TEST_LEFTOVER:-}" ]; then CLEANUP_LEFT="(試験用のフック)"; fi
}

# 照合(D14・D8): 周の起動の直前の控えと、今の共有の git ディレクトリの状態・refs/heads/<DEF> を比べる。
# 差分は VERIFY_DIFF に入れる(共有の config への追加を含め、どの変化も差分)
verify_iteration() {
  local cur="$RUN_DIR/iter-$ITER_SEQ.after.json" cur_sha="" out="" rc=0 now
  VERIFY_DIFF=""
  ITER_VERIFIED=""
  ITER_VERIFIED_SHA=""
  if ! verify_environment; then
    VERIFY_DIFF="差分: plugin または利用者設定の照合に失敗した"
    return 0
  fi
  # 比べる元が周の起動の直前に取ったままか(食い違えば、共有の状態の変化と同じく止める)
  if [ -z "$ITER_BASE_SHA" ] || [ "$(state_digest "$ITER_BASE" || true)" != "$ITER_BASE_SHA" ]; then
    STATE_GIT_UNSAFE=1
    VERIFY_DIFF="差分: 比べる元($ITER_BASE)が周の起動の直前に取ったものと違う(書き換えられた)"
    return 0
  fi
  if ! take_snapshot "$cur" "$ITER_WTADMIN" "$ITER_BASE"; then
    VERIFY_DIFF="${SNAPSHOT_DIFF:-差分: 今の状態を控えられない(照合できない)}"
    return 0
  fi
  cur_sha="$(state_digest "$cur" || true)"
  if [ -z "$cur_sha" ]; then
    STATE_GIT_UNSAFE=1
    VERIFY_DIFF="差分: 照合後の状態を保持できない"
    return 0
  fi
  verify_environment || die 20 environment "状態比較 helper の直前に環境が変わった"
  out="$("$PY_ABS" "$LOOP_STATE_PY" compare --before "$ITER_BASE" --after "$cur" --self-worktree "$ITER_WT" --self-ref "refs/heads/task/$ITER_NAME" \
    --max-items "$STATE_MAX_ITEMS" --max-bytes "$STATE_MAX_BYTES" --max-file-bytes "$STATE_MAX_FILE_BYTES" --max-seconds "$STATE_MAX_SECONDS")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    VERIFY_DIFF="$(printf '%s\n' "$out" | sed -n '/^差分: /p')"
    [ -n "$VERIFY_DIFF" ] || VERIFY_DIFF="差分: 照合できない(補助の終了コード $rc)"
    STATE_GIT_UNSAFE=1
    return 0
  fi
  VERIFY_DIFF="$(printf '%s\n' "$out" | sed -n '/^差分: /p')"
  # A strict helper difference is enough to stop.  Do not run rev-parse
  # afterwards: it would reload a config graph that the comparison just marked
  # unsafe.
  if [ -n "$VERIFY_DIFF" ]; then STATE_GIT_UNSAFE=1; return 0; fi
  if [ "$(state_digest "$ITER_BASE" || true)" != "$ITER_BASE_SHA" ] || \
     [ "$(state_digest "$cur" || true)" != "$cur_sha" ]; then
    STATE_GIT_UNSAFE=1
    VERIFY_DIFF="差分: 照合中に状態の控えが書き換わった"
    return 0
  fi
  now="$(G -C "$TOP" rev-parse -q --verify "refs/heads/$DEF_NAME" 2>/dev/null || true)"
  if [ "$now" != "$DEF_SHA" ]; then
    VERIFY_DIFF="${VERIFY_DIFF:+$VERIFY_DIFF
}差分: refs/heads/$DEF_NAME: $DEF_SHA → ${now:-(無い)}"
  fi
  if [ -n "$VERIFY_DIFF" ]; then
    STATE_GIT_UNSAFE=1
    return 0
  fi
  # compare() normalizes only its in-memory copies.  This file is the exact
  # after-observation that passed verification and is the sole source from
  # which a later last-verified state may be derived.
  ITER_VERIFIED="$cur"
  ITER_VERIFIED_SHA="$cur_sha"
  if [ "$(state_digest "$cur" || true)" != "$ITER_VERIFIED_SHA" ]; then
    STATE_GIT_UNSAFE=1
    ITER_VERIFIED=""
    ITER_VERIFIED_SHA=""
    VERIFY_DIFF="差分: 照合済みの状態が照合後に書き換わった"
  fi
}

# 周の途中で終わるとき(シグナル・EXIT の trap)の手順: 片付け → 照合 → 印 → 報告。
# ABORT_CODE に終了コードを入れる(このシェルで呼ぶ。$( ) の中で呼ばない)
abort_iteration() { # $1=きっかけ $2=残りがあるときのコード $3=差分があるときのコード $4=どちらも無いときのコード
  local worktree_state="worktree は残す"
  [ -z "$ITER_WT" ] || WT_OUTCOME[$ITER_WT]="周 $ITER_COUNT: $1 で止まった(判定していない)"
  cleanup_iteration
  if [ -n "$CLEANUP_LEFT" ]; then
    # 残ったプロセスがまだ書き換えうるので照合しない。周の途中の印と worktree を残す
    place_stop_mark "残ったプロセス($1 の片付けで止められなかった: $CLEANUP_LEFT)" "" 1
    rep "- $1: 片付けで残ったプロセス: $CLEANUP_LEFT(照合はしない。止めの印と周の途中の印を残した)" 2>/dev/null || true
    ABORT_CODE="$2"
    return 0
  fi
  # Once the child has been verified, that after-state is the only valid
  # promotion source.  In particular, the parent may already have removed the
  # exact own checkout.  Re-verifying ITER_BASE here would mistake that
  # permitted removal for a human change and discard the in-flight evidence.
  if [ -z "$ITER_VERIFIED" ] || [ -z "$ITER_VERIFIED_SHA" ]; then
    verify_iteration
  fi
  if [ -n "$VERIFY_DIFF" ]; then
    place_stop_mark "共有の状態の変化($1 の後の照合)" "$VERIFY_DIFF" 0
    rep "- $1: 共有の状態の変化(止めの印を置いた)" "$VERIFY_DIFF" 2>/dev/null || true
    ABORT_CODE="$3"
  else
    [ "${#REMOVED_PARENT_WORKTREES[@]}" -eq 0 ] || worktree_state="worktree は親が削除済み"
    if save_last_verified "$ITER_VERIFIED" "$ITER_VERIFIED_SHA"; then
      rm -rf -- "$INFLIGHT"
      rep "- $1: 子を片付けた。照合に通った($worktree_state)" 2>/dev/null || true
      ABORT_CODE="$4"
    else
      place_stop_mark "共有の状態の変化($1 の後の基準昇格)" "${SNAPSHOT_DIFF:-差分: 最後に照合に通った状態を更新できない}" 1
      rep "- $1: 基準を昇格できない(止めの印と周の途中の印を残した)" "${SNAPSHOT_DIFF:-}" 2>/dev/null || true
      ABORT_CODE="$3"
    fi
  fi
  ITER_ACTIVE=0
}

remove_selection_worktree() {
  [ -n "$SEL_WT" ] || return 0
  local wt="$SEL_WT"
  SEL_WT=""
  G -C "$TOP" worktree unlock "$wt" >/dev/null 2>&1 || true
  G -C "$TOP" worktree remove "$wt" >/dev/null 2>&1 || true
}

load_worktrees() { # LOCKED_REASON[worktree のパス]=lock の理由
  local line cur="" f="$RUN_DIR/worktrees.z"
  LOCKED_REASON=()
  G -C "$TOP" worktree list --porcelain -z >"$f"
  while IFS= read -r -d '' line; do
    case "$line" in
      "worktree "*) cur="${line#worktree }" ;;
      "locked "*) LOCKED_REASON["$cur"]="${line#locked }" ;;
    esac
  done <"$f"
}

# 理由が完全一致するパスを、呼び出し元の配列へ返す。区切り文字で連結・分割しない。
find_locked_worktrees() { # $1=理由 $2=出力先の配列名
  local reason="$1" path
  local -n matches="$2"
  matches=()
  for path in "${!LOCKED_REASON[@]}"; do
    [ "${LOCKED_REASON[$path]}" != "$reason" ] || matches+=("$path")
  done
}

# 表示だけを 1 行にまとめる。空白・改行を含むパスも境界が分かる形にする。
format_worktree_paths() {
  local path separator=""
  for path in "$@"; do
    printf '%s%q' "$separator" "$path"
    separator=', '
  done
}

# 残った worktree のその周の結末。この実行の分は判定を、過去の実行の分は「(過去の実行)」とその実行の
# report.md を示す(worktree の名前は <実行 ID>-<周の番号>)
describe_left_wt() { # $1=worktree のパス
  local base run rp
  if [ -n "${WT_OUTCOME[$1]:-}" ]; then printf '%s' "${WT_OUTCOME[$1]}"; return 0; fi
  base="${1##*/}"
  run="${base%-*}"
  rp="$STATE/$run/report.md"
  if [ "$run" = "$RUN_ID" ]; then
    printf 'この実行(結末は記録されていない)'
  elif [ -f "$rp" ]; then
    printf '(過去の実行)結末はその実行の報告を見る: %s' "$rp"
  else
    printf '(過去の実行)その実行の報告が見つからない'
  fi
}

report_left_worktrees() {
  local path reason found=0
  if [ "$STATE_GIT_UNSAFE" = 1 ]; then
    rep "" "## 保持済みの worktree 情報" ""
    rep "設定を安全に読み直せないため Git は再実行していない。以下は親が保持した情報で、現在の存在・lock を再確認した一覧ではない。"
    for path in "${!LOCKED_REASON[@]}"; do
      reason="${LOCKED_REASON[$path]}"
      case "$reason" in
        "dev-workflow-loop: "*) rep "- $path(保持した lock の理由: $reason)" ;;
      esac
    done
    for path in "${!WT_OUTCOME[@]}"; do
      rep "- $path — ${WT_OUTCOME[$path]}"
    done
    [ -z "$ITER_WT" ] || rep "- 中断した周の保持パス: $ITER_WT"
    [ -z "$SEL_WT" ] || rep "- 選定中の保持パス: $SEL_WT(自動削除していない)"
    rep "" "人の次の手順: 変更された設定と保持した作業場所を確認する。push・PR が既に作成されている可能性も外部から確認し、未確認の内容を merge しない。"
    return 0
  fi
  load_worktrees || return 0
  for path in "${!LOCKED_REASON[@]}"; do
    reason="${LOCKED_REASON[$path]}"
    case "$reason" in
      "dev-workflow-loop: "*)
        if [ "$found" -eq 0 ]; then rep "" "## 残った worktree" ""; found=1; fi
        rep "- $path(lock の理由: $reason)— 結末: $(describe_left_wt "$path")"
        ;;
    esac
  done
  if [ "$found" -eq 1 ]; then
    rep "" "人の次の手順: worktree を調べてから消す(\`git worktree unlock <パス>\` → \`git worktree remove <パス>\`)。消すとそのタスクは再び拾われうる"
  fi
}

finish() { # $1=終了コード $2=止まった理由
  local code="$1" why="$2" n
  trap - TERM HUP INT
  # command substitution 内の失敗フラグは親へ戻らない。終了時の Git より前に
  # 親自身が保持済み環境を照合し、照合後に続いている変更もここで止める。
  if [ "$STATE_GIT_UNSAFE" != 1 ] && [ -n "${ENVIRONMENT_SHA:-}" ]; then
    if ! verify_environment; then
      STATE_GIT_UNSAFE=1
      [ "$code" -ne 0 ] || code=20
      why="$why / environment-changed: 終了前の環境照合に失敗した"
    fi
  fi
  if [ "$STATE_GIT_UNSAFE" != 1 ]; then remove_selection_worktree; fi
  if [ -n "$REPORT" ]; then
    {
      rep "" "## 終わり" ""
      rep "- 止まった理由: $why" "- 終了コード: $code"
      if [ -e "$STOP_MARK" ] && [ "$code" -ne 0 ]; then rep "- $(stop_mark_guide)"; fi
      report_left_worktrees
      if [ "${#HELD_NAMES[@]}" -gt 0 ]; then
        rep "" "## 保留のタスクを再び回す手順" ""
        rep "デフォルトブランチ側の MD を直し(設計レビューのやり直しを含む)、作業ブランチを消すと再び拾われる(unattended-mode.md §5 の補足・§8)"
        for n in "${HELD_NAMES[@]}"; do
          rep "- task/$n: \`git branch -D task/$n\`。push 済みなら \`git push origin --delete task/$n\`(手元の追跡用 ref も消える)。リモートを別の手段(GitHub の画面など)で消したときは \`git branch -dr origin/task/$n\`"
        done
      fi
      if [ "${#TRACKING_SKIPPED[@]}" -gt 0 ]; then
        rep "" "## 追跡用の ref で読み飛ばしたタスク" ""
        rep "リモートのブランチを別の手段(GitHub の画面など)で消した後も、手元の追跡用の ref が残ると読み飛ばされる。再び回すなら、ブランチを消したことを確かめてから追跡用の ref を消す"
        for n in "${TRACKING_SKIPPED[@]}"; do
          rep "- task/$n: \`git branch -dr origin/task/$n\`(ローカルに作業ブランチがあれば \`git branch -D task/$n\` も)"
        done
      fi
      report_discover_end
    } 2>/dev/null || { [ "$code" -ne 0 ] || code=30; }
  fi
  say "止まった理由: $why"
  [ -z "$REPORT" ] || say "報告: $REPORT"
  if [ "$code" -ne 0 ] && [ -n "$STOP_MARK" ] && [ -e "$STOP_MARK" ]; then say "$(stop_mark_guide)"; fi
  EXPLICIT_EXIT=1
  exit "$code"
}

begin_promotion_critical() {
  DEFERRED_SIGNAL_NAME="" DEFERRED_SIGNAL_CODE=""
  PROMOTION_CRITICAL=1
}

end_promotion_critical() {
  local name code
  PROMOTION_CRITICAL=0
  name="$DEFERRED_SIGNAL_NAME"
  code="$DEFERRED_SIGNAL_CODE"
  DEFERRED_SIGNAL_NAME="" DEFERRED_SIGNAL_CODE=""
  # The original traps are still installed while a deferred handler returns.
  # Deliver the first signal only after either the promotion or its preserved
  # failure state is complete; on_signal finishes the process and does not
  # return on this path.
  [ -z "$name" ] || on_signal "$name" "$code"
}

on_signal() { # $1=シグナル名 $2=終了コード(128 + シグナル番号)
  local name="$1" code="$2"
  if [ "$PROMOTION_CRITICAL" = 1 ]; then
    if [ -z "$DEFERRED_SIGNAL_NAME" ]; then
      DEFERRED_SIGNAL_NAME="$name" DEFERRED_SIGNAL_CODE="$code"
    fi
    return 0
  fi
  trap - TERM HUP INT
  set +e
  [ -z "$REPORT" ] || rep "" "- シグナル $name を受けた" 2>/dev/null
  if [ "$ITER_ACTIVE" = 1 ]; then
    abort_iteration "シグナル $name" "$code" "$code" "$code"
  fi
  echo "ERROR [aborted] シグナル $name を受けたので止まった" >&2
  finish "$code" "シグナル $name"
}

on_exit() {
  local rc=$? code=30
  set +e
  trap - ERR TERM HUP INT
  [ "$EXPLICIT_EXIT" = 1 ] && return 0
  # 内部の失敗(set -e など)。失敗したコマンドの終了コードをそのまま返さず、30(照合の差分・残りは 10)に揃える
  echo "ERROR [internal] 予期しない失敗(終了コード $rc・行 ${ERR_LINE:-?})" >&2
  [ -z "$REPORT" ] || rep "" "- ERROR [internal] 予期しない失敗(終了コード $rc・行 ${ERR_LINE:-?})" 2>/dev/null
  if [ "$ITER_ACTIVE" = 1 ]; then
    abort_iteration "内部の失敗" 10 10 30
    code="$ABORT_CODE"
  fi
  finish "$code" "内部の失敗"
}

# ── 既定表(ホスト名 → 雛形。信頼の基点。既定表は Claude Code の 1 行だけ — #68 の決定 1)──
# 雛形 = 実行ファイルと前置きのフラグ(`--host-argv` で置き換えられる)。隔離と判定に要るフラグ
# (ISOLATION・--plugin-dir・--permission-mode・--permission-prompts・--mcp-config・--allowedTools)は
# loop.sh が必ず後ろに足す(D20)。解決後の形(既定):
#   claude -p --output-format json --setting-sources user --strict-mcp-config --plugin-dir <プラグインルート> --permission-mode acceptEdits --permission-prompts none  # <!-- validate-allow: 無人ループの既定の雛形。人のシェル・cron から起動する(design §5-5 の対象外。§2) -->
# 事実の出典は design §7-3
load_host_table() { # $1=ホスト名
  case "$1" in
    claude)  # <!-- validate-allow: 既定表の雛形の行(ホスト CLI の実行ファイル名) -->
      TEMPLATE=(claude)
      ISOLATION=(-p --output-format json --setting-sources user --strict-mcp-config)
      PLUGIN_DIR_FLAG=--plugin-dir
      PERM_MODE_FLAG=--permission-mode
      PERM_EDITS=(--permission-mode acceptEdits)
      PERM_CLASSIFIER=(--permission-mode auto)
      PERM_PROMPTS=(--permission-prompts none)
      MCP_FLAG=--mcp-config
      ALLOWED_FLAG=--allowedTools
      # 許可の仲介(D22 ③): PermissionRequest の hook を持つ設定を JSON 文字列で渡すフラグ
      SETTINGS_FLAG=--settings
      # 全許可のフラグ名と、--permission-mode の全許可の値(#68 の決定 3)・分類器の値(無人ループでは使わない。#107 H48)
      FULL_PERMISSION_NAMES=(--dangerously-skip-permissions --allow-dangerously-skip-permissions)
      FULL_PERMISSION_VALUE=bypassPermissions
      CLASSIFIER_VALUE=auto
      # 補助の CLI(セッションを起動しない。cwd は状態ディレクトリ)
      AUX_HELP=(--help)
      AUX_VERSION=(--version)
      AUX_AUTH=(auth status)
      AUX_PLUGINS=(plugin list --json)
      ;;
    codex|cursor-agent)
      die 20 host-unsupported "ホスト '$1' は既定表に無い(対象は Claude Code だけ。リポジトリ側の設定を読ませない手段か、全許可なしで commit・push する手段が無い。根拠は design §7-3)"
      ;;
    *)
      die 20 host-unknown "ホスト '$1' は既定表に無い(既定表: claude)"
      ;;
  esac
}

# §2 の 2: ホストのセッションの中でない(D7。ベストエフォート)。
# 列のどれか 1 つでも立っていれば止まる。DEV_WORKFLOW_HOST_CLI は非空なら止まる向きに読む
check_host_session() {
  local v
  for v in "${HOST_SESSION_VARS[@]}"; do
    if [ -n "${!v:-}" ]; then
      die 20 host-session "ホストのセッションの中から起動された($v が立っている)。人のシェル・cron から起動する"
    fi
  done
}

# D20/H28: 実行ファイル以外は閉じた許可表で型ごとに検査する。
check_host_argv() {
  verify_environment || die 20 environment "起動引数検査の直前に環境が変わった"
  "$PY_ABS" "$HOST_ARGV_PY" -- "${HOST_ARGV[@]}" || die 20 host-argv "--host-argv は model・effort・budget・max-turns・name の = 形式と --verbose だけを受け付ける"
}

in_lines() { # $1=語 $2=ファイル(1 行 1 語)→ 在れば 0
  local v
  while IFS= read -r v; do [ "$v" != "$1" ] || return 0; done <"$2"
  return 1
}

# D20・H28: 表(--help の `<…>` 表記)にある長名は `--名前=値` の形だけ。
# 短い引数は `=` の後ろを含む全体を文字単位で調べ、値を取る短名を含めば拒否する。
check_host_argv_values() { # $1=表のファイル(1 行 1 名)
  local tok name chars i short
  [ "${#HOST_ARGV_OVERRIDE[@]}" -gt 0 ] || return 0
  for tok in "${HOST_ARGV[@]:1}"; do
    case "$tok" in
      --*)
        name="${tok%%=*}"
        if in_lines "$name" "$1"; then
          case "$tok" in
            *=*) : ;;
            *) die 20 host-argv "--host-argv の '$tok' は値を取るフラグ。値は --名前=値 の形で書く" ;;
          esac
        fi ;;
      -*)
        chars="${tok#-}"
        for ((i = 0; i < ${#chars}; i++)); do
          short="-${chars:i:1}"
          if in_lines "$short" "$1"; then
            die 20 host-argv "--host-argv の '$tok' は値を取る短いフラグ '$short' を含むため拒否する。値は --名前=値 の形で書く"
          fi
        done ;;
    esac
  done
}

# 並び: 雛形(か --host-argv)→ 許可リスト → MCP の設定 → 隔離・権限のフラグ(と D22 の --settings)。最後に
# 置き、可変長の値を取るフラグ(許可リスト・MCP の設定)に食われないようにする
build_child_argv() {
  CHILD_ARGV=("${HOST_ARGV[@]}")
  if [ "${#ALLOWED_TOOLS[@]}" -gt 0 ]; then CHILD_ARGV+=("$ALLOWED_FLAG" "${ALLOWED_TOOLS[@]}"); fi
  if [ "${#MCP_CONFIGS[@]}" -gt 0 ]; then CHILD_ARGV+=("$MCP_FLAG" "${MCP_CONFIGS[@]}"); fi
  CHILD_ARGV+=("${ISOLATION[@]}" "$PLUGIN_DIR_FLAG" "$PLUGIN_ROOT")
  if [ "$ALLOW_CLASSIFIER" -eq 1 ]; then CHILD_ARGV+=("${PERM_CLASSIFIER[@]}"); else CHILD_ARGV+=("${PERM_EDITS[@]}"); fi
  CHILD_ARGV+=("${PERM_PROMPTS[@]}")
  # 許可の仲介の hook(D22 ③)。ファイルにしない(周が書き換えられないように)。argv の最後
  CHILD_ARGV+=("$SETTINGS_FLAG" "$HOOK_SETTINGS")
}

# H31: 証明に結び付ける起動の形。loop.sh が必ず足す隔離・権限・hook のフラグ(と権限のモード)を
# build_child_argv と同じ並びで並べ、実行ごとに変わる値(プラグインのコピー・hook の設定)は置き換える。
# H47 の CLI 許可リストと保持設定の allow は initial-policy の要約へ束縛する。
# MCP の設定・--host-argv の上書き(model・effort など。host-argv.py の許可表)は形へ含めない。
build_child_shape() {
  local shape=("${ISOLATION[@]}" "$PLUGIN_DIR_FLAG" "<plugin>") template
  if [ "$ALLOW_CLASSIFIER" -eq 1 ]; then shape+=("${PERM_CLASSIFIER[@]}"); else shape+=("${PERM_EDITS[@]}"); fi
  # hook の設定の雛形(loader と hook の並び)の要約。パスと保持値を固定の印にして算出する
  template="$(py hook-settings "<python>" "<script>" "$(printf '0%.0s' {1..64})" "<guard>" "<state>" "$(printf '0%.0s' {1..64})" "$FIXED_POLICY" | sha256sum)"
  shape+=("${PERM_PROMPTS[@]}" "$SETTINGS_FLAG" "<hook:${template:0:16}>")
  shape+=("<initial-policy:$INITIAL_POLICY_SHA>" "<aux-settings:$FIXED_POLICY>")
  HOST_SHAPE="$(quote_argv "${shape[@]}")"
}

# H31: 起動する直前に、ホスト CLI の実体が確かめた時と同じかを照合する。既定は stat の値(dev・inode・種類と
# 権限・size・mtime・ctime。書き換えれば ctime が、差し替えれば inode が変わる)だけを比べ、補助の CLI ごとに
# Python を起動しない。--hash では helper が realpath・stat の値・内容の sha256 まで照らす
host_stat() { stat -c '%d|%i|%f|%s|%y|%z' -- "$HOST_EXEC" 2>/dev/null; }
host_same() {
  [ -n "$HOST_EXEC" ] && [ -n "$HOST_IDENTITY" ] && [ -n "$HOST_STAT" ] || return 1
  [ "$(host_stat)" = "$HOST_STAT" ] || return 1
  [ "${1:-}" = --hash ] || return 0
  "$PY_ABS" -I -B "$HOST_CHECK_PY" same --host "$HOST_EXEC" --identity "$HOST_IDENTITY" --hash 7>&-
}

# #68 の決定 3: 全許可のフラグを、ホスト CLI へ渡す argv 全体(--host-argv・--allowed-tools 経由を含む)で走査して拒否する
scan_full_permission() {
  local n="${#CHILD_ARGV[@]}" i=0 tok name value bad
  while [ "$i" -lt "$n" ]; do
    tok="${CHILD_ARGV[$i]}"
    name="${tok%%=*}"
    for bad in "${FULL_PERMISSION_NAMES[@]}"; do
      [ "$name" != "$bad" ] || die 20 full-permission "全許可のフラグが argv にある('$tok')。全許可のモードは使わない"
    done
    [ "$tok" != "$FULL_PERMISSION_VALUE" ] || die 20 full-permission "全許可のモードの値が argv にある('$tok')"
    if [ "$name" = "$PERM_MODE_FLAG" ]; then
      if [ "$tok" != "$name" ]; then value="${tok#*=}"; else value="${CHILD_ARGV[$((i + 1))]:-}"; fi
      [ "$value" != "$FULL_PERMISSION_VALUE" ] || die 20 full-permission "全許可のモードが argv にある('$name $value')"
      if [ "$value" = "$CLASSIFIER_VALUE" ] && [ "$ALLOW_CLASSIFIER" -ne 1 ]; then
        die 20 classifier "分類器による自動承認('$name $value')は無人ループでは使わない(許可の仲介を通らない書き込みがある。#107 H48)"
      fi
    fi
    i=$((i + 1))
  done
}

quote_argv() { local out="" a; for a in "$@"; do out="$out $(printf '%q' "$a")"; done; printf '%s' "${out# }"; }

# 補助の CLI(--version・--help・auth status・plugin list)。上書き後の実行ファイルで、cwd を状態ディレクトリにして打つ
# (リポジトリの設定を読ませない)。セッションは起動しない
# stdout と stderr は別のファイルへ向ける(警告が stderr に出ても、一覧の解析は stdout だけで行う)。
# 実行ファイルは起動時に控えた実体(HOST_EXEC。H31)を使い、打つたびに同じ実体かを照合する
aux() { # $1=stdout のファイル 残り=補助の CLI の引数(stderr は <stdout のファイル>.err)
  local out="$1"
  shift
  if ! host_same; then
    echo "ERROR [host-proof] 補助の CLI を打つ直前に、ホスト CLI の実体が控えた時と違う(${HOST_EXEC:-未確認})" >&2
    return 1
  fi
  # 端末の幅の変数で --help の折り返しが変わらないよう、外して打つ(証明の help の照合を安定させる)
  # (env を挟まない。env は `=` を含むパスを代入として読み、控えた実体を起動しないことがある)
  ( cd "$STATE" && unset COLUMNS LINES && run_detached "$NET_TIMEOUT" "$HOST_EXEC" "$SETTINGS_FLAG" "$FIXED_POLICY" "$@" >"$out" 2>"$out.err" )
}

# ── H31: 実 hook の確認(--prove-host)──
# 周の子と同じ argv・環境変数・監督で、使い捨てのディレクトリから 1 回だけ起動する。cwd の外の決まったパスへ
# Write を 1 回させ、許可の仲介の hook が呼ばれて拒否したこと(判定の記録・結果の拒否の欄・ファイルが無いこと)を
# 確かめたときだけ、実体・版・help・起動の形と結び付けた証明を書く
prove_host() {
  local dir target permlog nonce rc=0 child_rc child_to judged path timeout=600 probe_pid
  local sanitizer_pid sanitizer_rc=0 probe_stream pipe_in pipe_out init_evidence name
  local proof_names=()
  local allow_args=() allow_value
  for allow_value in ${ALLOWED_TOOLS[@]+"${ALLOWED_TOOLS[@]}"}; do allow_args+=(--allowed-tools "$allow_value"); done
  [ "$ITER_TIMEOUT" -ge "$timeout" ] || timeout="$ITER_TIMEOUT"
  dir="$(mktemp -d "$RUN_DIR/host-probe.XXXXXXXX")"
  mkdir "$dir/work" "$dir/outside"
  target="$dir/outside/dev-workflow-host-probe.txt"
  permlog="$dir/permlog"
  printf '%s\n' "This is an automated permission check run by the dev-workflow loop. Use the Write tool exactly once to create the file $target with the content 'dev-workflow host probe'. Do not use any other tool, do not retry, and do not ask questions. If the write is denied, reply with the single word DENIED." >"$dir/prompt"
  rep "" "## ホスト CLI の実 hook の確認(--prove-host)" "" "- 実体: $HOST_EXEC(sha256 $HOST_SHA・$HOST_KIND)" "- 起動の形: $HOST_SHAPE" "- 確認の記録: $dir"
  verify_environment || die 20 environment "実 hook の確認の直前に環境が変わった"
  host_same --hash || die 20 host-proof "実 hook の確認の直前に、ホスト CLI の実体が控えた時と違う"
  nonce="$("$PY_ABS" -c 'import secrets; print(secrets.token_hex(32))')"
  # raw stdout は匿名pipeだけを通す。縮約済みのinit/resultだけを保存する。
  # sanitizerとsupervisorをloopの別々の子にし、親終了時の回収を維持する。
  coproc H46_SANITIZER {
    exec "$PY_ABS" -I -B "$HOST_CHECK_PY" init-sanitize --state "$ENVIRONMENT_STATE" \
      --expect-sha256 "$ENVIRONMENT_SHA" "${allow_args[@]}" >"$dir/out.json" 2>"$dir/sanitize.err" 7>&-
  }
  sanitizer_pid="$H46_SANITIZER_PID"
  pipe_in="${H46_SANITIZER[1]}"; pipe_out="${H46_SANITIZER[0]}"
  exec {probe_stream}>&"$pipe_in"
  exec {pipe_in}>&- {pipe_out}<&-
  # 背景で起動して wait で待つ(前面で待つと、loop.sh だけに届いたシグナルの処理が確認の終わりまで遅れる)。
  # loop.sh が止まると、監督は親の終了通知で子孫を回収する
  (
    exec 7>&-
    cd "$dir/work"
    exec "$ENV_BIN" -u DEV_WORKFLOW_HOST_CLI -u OLDPWD DEV_WORKFLOW_LOOP_ITER="$RUN_ID-host-probe" \
      DEV_WORKFLOW_LOOP_WORKTREE="$dir/work" DEV_WORKFLOW_LOOP_PERMLOG="$permlog" \
      DEV_WORKFLOW_ENV_STATE="$ENVIRONMENT_STATE" DEV_WORKFLOW_ENV_SHA256="$ENVIRONMENT_SHA" \
      DEV_WORKFLOW_ENV_GUARD="$TRUSTED_ENV_GUARD" DEV_WORKFLOW_ENV_GUARD_SHA256="$ENV_GUARD_SHA" \
      DEV_WORKFLOW_LOOP_PLUGIN_ROOT="$PLUGIN_ROOT" DEV_WORKFLOW_LOOP_ALLOW="$ALLOW_JSON" \
      CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
      "$PY_ABS" "$SUPERVISOR" --result "$dir/supervisor.json" --nonce "$nonce" \
      --timeout "$timeout" --grace "$KILL_GRACE" -- "${CHILD_ARGV[@]}" --verbose \
      <"$dir/prompt" 1>&"$probe_stream" 2>"$dir/err"
  ) &
  probe_pid=$!
  exec {probe_stream}>&-
  wait "$probe_pid" || rc=$?
  wait "$sanitizer_pid" || sanitizer_rc=$?
  [ "$rc" -eq 0 ] || die 20 host-proof "実 hook の確認の子の回収を確かめられない(監督の終了コード $rc。$dir)"
  read -r child_rc child_to < <(py supervisor-result "$dir/supervisor.json" "$nonce") \
    || die 20 host-proof "実 hook の確認の監督の結果が保持値と違う($dir)"
  [ "$child_rc" -eq 0 ] && [ "$child_to" -eq 0 ] || die 20 host-proof "実 hook の確認の子が失敗または時間切れになった。証明は書かない"
  [ "$sanitizer_rc" -eq 0 ] || die 20 host-proof "init と結果を安全に縮約できない。証明は書かない"
  host_same --hash || die 20 host-proof "実 hook の確認の前後で、ホスト CLI の実体が変わった"
  verify_environment || die 20 environment "実 hook の確認の後に環境が変わった"
  rc=0
  judged="$("$PY_ABS" -I -B "$HOST_CHECK_PY" probe-judge --out "$dir/out.json" --permlog "$permlog" --target "$target" 2>"$dir/judge.err")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    die 20 host-proof "実 hook の確認に通らない: $(head -c 400 "$dir/judge.err" | tr '\n' ' ')(子の終了コード $child_rc・時間切れ $child_to。記録: $dir)。証明は書かない"
  fi
  init_evidence="$(py json-value init <"$dir/out.json")" || die 20 host-proof "init の証拠を読めない"
  while IFS= read -r name; do [ -z "$name" ] || proof_names+=(--public-name "$name"); done \
    < <(printf '%s' "$init_evidence" | py init-names)
  rc=0
  path="$("$PY_ABS" -I -B "$HOST_CHECK_PY" proof-write --store "$PROOF_STORE" --identity "$HOST_IDENTITY" --shape "$HOST_SHAPE" \
    --version-file "$RUN_DIR/version.txt" --help-file "$RUN_DIR/help.txt" --probe "$judged" --plugin-version "$PLUGIN_VERSION" \
    --component-sha256 "$COMPONENT_SHA" --policy-sha256 "$POLICY_SHA" --resolver-version "$COMPONENT_RESOLVER" \
    --init-evidence "$init_evidence" "${proof_names[@]}" 2>"$dir/write.err")" || rc=$?
  [ "$rc" -eq 0 ] || die 20 host-proof "証明を書けない: $(head -c 300 "$dir/write.err" | tr '\n' ' ')"
  rep "- 判定: 許可の仲介の hook が確認の Write を拒否した(子の終了コード $child_rc)" "- 証明: $path"
  say "ホスト CLI の証明を書いた: $path"
  finish 0 "--prove-host(許可の仲介の hook が実際に効くことを確かめ、証明を書いた)"
}

# ── 次の起動(§4。ロックの直後、デフォルトブランチの固定・profile の読み取り・ネットワークの git より前)──
handle_marks_at_start() {
  local out rc now cur diff cur_sha base_sha
  # 止めの印があれば、人が差分を確かめて消すまで起動しない。周の途中の印もあれば、先にその周のプロセスを止める
  if [ -e "$STOP_MARK" ] || [ -L "$STOP_MARK" ]; then
    # inflight は同じ UID が書ける状態領域であり、そこだけの iter を
    # 根拠に signal を送らない。止めの印がある再開は人が確認する。
    [ ! -d "$INFLIGHT" ] || rep "- 周の途中の印は未信頼のため signal せず保全した($INFLIGHT)"
    rep "- 止めの印は未信頼のため内容を実行・自動採用せず保全した: $STOP_MARK"
    die 20 stop-mark "止めの印がある。$(stop_mark_guide)"
  fi
  if [ -e "$INFLIGHT" ] || [ -L "$INFLIGHT" ]; then
    die 20 inflight-untrusted "周の途中の印は未信頼($INFLIGHT)。signal・削除・unlock・ref 更新をせず保全した。人が所有者と成果物を確認する"
  fi
  # どちらの印も無ければ、最後に照合に通った状態と比べて、差分は報告に出して続ける
  # (実行と実行の間の変化は人の操作でもありうるため)
  if [ -f "$LAST_VERIFIED" ]; then
    cur="$RUN_DIR/start-now.json"
    if ! take_snapshot "$cur" - "$LAST_VERIFIED" 1; then
      die 20 state-snapshot "最後に照合に通った状態を安全に更新できない。${SNAPSHOT_DIFF:-状態を控えられない}"
    fi
    FRESH_CONFIG_BASELINE=1
    rc=0
    verify_environment || die 20 environment "状態比較 helper の直前に環境が変わった"
    out="$("$PY_ABS" "$LOOP_STATE_PY" compare --before "$LAST_VERIFIED" --after "$cur" \
      --max-items "$STATE_MAX_ITEMS" --max-bytes "$STATE_MAX_BYTES" --max-file-bytes "$STATE_MAX_FILE_BYTES" --max-seconds "$STATE_MAX_SECONDS")" || rc=$?
    [ "$rc" -le 1 ] || die 30 internal "最後に照合に通った状態との比較に失敗した(補助の終了コード $rc)"
    diff="$(printf '%s\n' "$out" | sed -n '/^差分: /p')"
    if [ -n "$diff" ]; then
      STARTUP_NOTES+=("最後に照合に通った状態からの差分(実行と実行の間の変化。止めずに続ける):" "$diff")
    fi
  fi
}

# ── §3: キューと選定 ──
join_rel() { # $1=task_dir $2=ファイル名 → 管理ルート相対パス
  if [ "$1" = . ]; then printf '%s' "$2"; else printf '%s/%s' "$1" "$2"; fi
}

has_meta() { # $1=ファイル(- なら stdin)
  local info
  info="$(py taskinfo "$1")" || return 1
  case "$info" in *meta=1*) return 0 ;; esac
  return 1
}

# 追跡外のタスクの検査(2026-09-23 決定 10。最初の周の 3a)。人のチェックアウトを読むだけで、index を書き換えうる
# git status は使わない。固定した sha の tree に無いか、その blob のヘッダにメタ行が無ければ止まる
check_untracked_tasks() {
  local dir="$TOP/$TASK_DIR" p f rel bad=() typ
  [ -d "$dir" ] || return 0
  for p in "$dir"/進行中_*.md; do
    [ -f "$p" ] && [ ! -L "$p" ] || continue
    has_meta "$p" || continue
    f="${p##*/}"
    rel="$(join_rel "$TASK_DIR" "$f")"
    if ! G -C "$TOP" cat-file -e "$DEF_SHA:$rel" 2>/dev/null; then
      bad+=("$rel(固定した sha の tree に無い)")
      continue
    fi
    typ="$(G -C "$TOP" cat-file -t "$DEF_SHA:$rel")"
    if [ "$typ" != blob ] || ! G -C "$TOP" cat-file blob "$DEF_SHA:$rel" | has_meta -; then
      bad+=("$rel(メタ行が commit に無い。メタ行だけ未 commit)")
    fi
  done
  if [ "${#bad[@]}" -gt 0 ]; then
    rep "" "## 追跡外のタスク(起動時に止まった)" ""
    for p in "${bad[@]}"; do rep "- $p"; done
    remove_selection_worktree
    die 20 untracked-task "無人実行のメタ行があるのに追跡外(か、メタ行だけ未 commit)のタスク MD がある: ${bad[*]}。commit してから起動する"
  fi
}

note_skip() { # $1=相対パス $2=理由(同じ組は 1 回だけ報告する)
  local key="$1$TAB$2"
  SKIPS+=("$1: $2")
  [ -z "${SKIP_REPORTED[$key]:-}" ] || return 0
  SKIP_REPORTED[$key]=1
  NEW_SKIPS+=("$1: $2")
}

skip_reason() { # $1=worktree $2=ファイル名 → 読み飛ばす理由(無ければ空)
  local wt="$1" f="$2" p name rel info meta ref r o hit locked_paths=()
  p="$wt/$TASK_DIR/$f"
  name="${f#進行中_}"
  name="${name%.md}"
  rel="$(join_rel "$TASK_DIR" "$f")"
  info="$(py taskinfo "$p")"
  meta="$(printf '%s\n' "$info" | sed -n 's/^meta=//p')"
  if [ "$meta" != 1 ]; then printf '無人実行のメタ行が無い'; return 0; fi
  if ! py d21 "$rel" "$name" 2>"$RUN_DIR/d21.err" >/dev/null; then
    printf 'パスの文字が D21 に外れる(%s)' "$(cat "$RUN_DIR/d21.err")"; return 0
  fi
  if ! G -C "$TOP" check-ref-format --branch "task/$name" >/dev/null 2>&1; then
    printf 'task/%s がブランチ名にならない(D21)' "$name"; return 0
  fi
  if G -C "$TOP" show-ref --verify --quiet "refs/heads/task/$name"; then
    printf '作業ブランチ task/%s がローカルにある' "$name"; return 0
  fi
  r=""
  while IFS= read -r ref; do
    case "$ref" in refs/remotes/*/task/"$name") r="$ref"; break ;; esac
  done <"$RUN_DIR/remote-refs.txt"
  if [ -n "$r" ]; then
    printf '追跡用の ref %s がある(リモートを別の手段で消したときは `git branch -dr %s`)' "$r" "${r#refs/remotes/}"
    return 0
  fi
  if [ -n "${REMOTE_TASKS[$name]:-}" ]; then
    printf '作業ブランチ task/%s が origin にある' "$name"; return 0
  fi
  find_locked_worktrees "dev-workflow-loop: $rel" locked_paths
  if [ "${#locked_paths[@]}" -gt 0 ]; then
    printf '前の周が残した worktree がある(%s)' "$(format_worktree_paths "${locked_paths[@]}")"; return 0
  fi
  if [ -e "$wt/$TASK_DIR/完了_$name.md" ] || [ -L "$wt/$TASK_DIR/完了_$name.md" ] \
     || [ -e "$wt/$TASK_DIR/保留_$name.md" ] || [ -L "$wt/$TASK_DIR/保留_$name.md" ]; then
    printf '同じ task_dir に 完了_%s.md か 保留_%s.md がある' "$name" "$name"; return 0
  fi
  if [ "${#ONLY[@]}" -gt 0 ]; then
    hit=0
    for o in "${ONLY[@]}"; do [ "$o" != "$name" ] || hit=1; done
    if [ "$hit" -eq 0 ]; then printf -- '--only の外'; return 0; fi
  fi
  return 0
}

# D22 ①: 周の worktree の .claude/・.claude/reviews/ を、base-commit.md ① と同じ検査で作る(階層ごとに
# 「検査 → 無ければ作成」。symlink なら止める・無ければ -p なしの mkdir・在って通常のディレクトリでなければ止める)。
# loop.sh の書き込みは許可の外。止まるときは終了コード 30
prepare_reviews_dir() { # $1=周の worktree
  local d
  for d in "$1/.claude" "$1/.claude/reviews"; do
    if [ -L "$d" ]; then die 30 reviews-dir "周の worktree の $d が symlink(辿らずに止まる)"; fi
    if [ ! -e "$d" ]; then
      mkdir -- "$d" || die 30 reviews-dir "周の worktree に $d を作れない"
    elif [ ! -d "$d" ]; then
      die 30 reviews-dir "周の worktree の $d が通常のディレクトリでない"
    fi
  done
}

SEQ=0
FIRST_SELECTION=1
declare -A REMOTE_TASKS=()

# 無人ループの前提(loop.md §3 の 2a): 状態ファイル 3 つが周の worktree で ignore されていて、追跡されていない。
# `.claude/reviews/` を作る前に確かめる。`--no-index` を付けない(付けると追跡済みのファイルも ignore 済みと答える)
check_state_ignored() { # $1=周の worktree
  local p rc bad=() tracked=()
  for p in .claude/reviews/x.md .claude/grasp.md .claude/.understand-project-done; do
    rc=0
    G -C "$1" check-ignore -q -- "$p" >/dev/null 2>&1 || rc=$?
    # 0 は ignore 済み・1 は外れ。0 と 1 以外(symlink の先・サブモジュールの中など、ignore の有無を判定できないもの)は数えず、後の段(2b の置き場の検査など)に任せる
    if [ "$rc" -eq 1 ]; then
      bad+=("$p")
      if G -C "$1" ls-files --error-unmatch -- "$p" >/dev/null 2>&1; then tracked+=("$p"); fi
    fi
  done
  [ "${#bad[@]}" -gt 0 ] || return 0
  rep "" "## 状態ファイルが ignore されていない(起動時に止まった)" ""
  for p in "${bad[@]}"; do rep "- $p"; done
  rep "" "無人ループは、状態ファイル 3 つが ignore されていて追跡されていないことを前提にする(周の中で書かれた状態ファイルが未追跡か未 commit の変更として残ると、周の worktree を消せずに残る。発見モードでは、判定が未追跡・未 commit の無いことを求めるので、正しい周も失敗になる)。.gitignore に次を足して commit する(init-project の gitignore の断片と同じ):" "" \
    '```' ".claude/reviews/" ".claude/grasp.md" ".claude/.understand-project-done" '```'
  if [ "${#tracked[@]}" -gt 0 ]; then
    rep "" "追跡済みのものは、索引から外して commit する:"
    for p in "${tracked[@]}"; do rep "- \`git rm --cached -- $p\`"; done
  fi
  remove_selection_worktree
  die 20 state-not-ignored "状態ファイルが ignore されていないか追跡済み: ${bad[*]}${tracked[*]:+(追跡済み: ${tracked[*]}。git rm --cached で外す)}。.gitignore に足して commit してから起動する"
}

# 選定中の worktree を作り、task_dir を解決する(§3 の 2〜3。実装モードと発見モードで共通)
make_selection_worktree() {
  local wt out rc
  SEQ=$((SEQ + 1))
  wt="$WT_ROOT/$RUN_ID-$SEQ"
  mkdir -p "$WT_ROOT"
  # 2. 固定した sha から周の worktree を作る(前置き・ネットワークの規則つき)
  rc=0
  net_git "$WORKTREE_ADD_TIMEOUT" -C "$TOP" worktree add --detach --lock --reason "dev-workflow-loop: 選定中" "$wt" "$DEF_SHA" \
    >>"$RUN_DIR/worktree.log" 2>&1 || rc=$?
  [ "$rc" -eq 0 ] || die 30 worktree-add "worktree を作れない(終了コード $rc。$RUN_DIR/worktree.log)"
  SEL_WT="$wt"
  # 状態ファイルの ignore の検査(両モード。.claude/reviews/ を作る前。外れたら worktree を消して exit 20)
  check_state_ignored "$wt"
  # 2b. worktree の .claude/・.claude/reviews/ を先に作る(D22 ①)
  prepare_reviews_dir "$wt"
  # 3. task_dir(worktree の中で解決する。profile の文字列は --task-dir=<値> で渡す)
  rc=0
  if [ "$TASK_DIR_SET" -eq 1 ]; then
    verify_environment || die 20 environment "resolver の直前に環境が変わった"
    out="$(cd / && python3 -B "$RESOLVER" --project-root "$wt" --task-dir="$TASK_DIR_VALUE" 7>&-)" || rc=$?
  else
    verify_environment || die 20 environment "resolver の直前に環境が変わった"
    out="$(cd / && python3 -B "$RESOLVER" --project-root "$wt" 7>&-)" || rc=$?
  fi
  [ "$rc" -eq 0 ] || die 30 task-dir-helper "task_dir を解決できない(resolve-task-dir.py の終了コード $rc: $(printf '%s' "$out" | head -c 300))"
  TASK_DIR="$(printf '%s' "$out" | py json-get task_dir)" || die 30 task-dir-helper "resolve-task-dir.py の出力を読めない"
  if ! py d21 "$TASK_DIR" 2>"$RUN_DIR/d21.err" >/dev/null; then
    remove_selection_worktree
    die 20 task-dir "解決した task_dir が D21 に外れる($(cat "$RUN_DIR/d21.err")): $TASK_DIR"
  fi
  # 保護パス(許可の仲介の hook と同じ列。.claude/worktrees の下を除く)の下の task_dir は止まる(無人の周の skill が
  # タスク MD を Bash の引数に渡す操作を、hook が拒否するため)。選定の worktree を消してから exit 20
  rc=0
  verify_environment || die 20 environment "保護パス検査の直前に環境が変わった"
  ( cd / && exec "$PY_ABS" -B "$PERM_SCRIPT" --is-protected "$TASK_DIR" 7>&- ) || rc=$?
  case "$rc" in
    0) remove_selection_worktree
       die 20 task-dir "解決した task_dir が保護パスの下にある: $TASK_DIR(無人の周ではタスク MD を扱えない。task_dir を保護パスの外へ移す)" ;;
    1) : ;;
    *) die 30 internal "task_dir が保護パスの下かを判定できない(終了コード $rc)" ;;
  esac
  SEL_TASK_DIR="$TASK_DIR"
}

select_task() { # 選定中の worktree を作り、CANDIDATES を並べる
  local wt out rc p f name reason line lines=() date ref
  make_selection_worktree
  wt="$SEL_WT"
  # 3a(最初の周だけ)
  if [ "$FIRST_SELECTION" -eq 1 ]; then check_untracked_tasks; fi
  # 4. 候補
  REMOTE_TASKS=()
  if [ "$HAS_ORIGIN" -eq 1 ]; then
    rc=0
    out="$(net_git "$NET_TIMEOUT" -C "$TOP" ls-remote origin 'refs/heads/task/*' 2>>"$RUN_DIR/ls-remote.err")" || rc=$?
    [ "$rc" -eq 0 ] || die 30 ls-remote "origin の task/* を読めない(git ls-remote の終了コード $rc)"
    while IFS="$TAB" read -r _ ref; do
      case "$ref" in refs/heads/task/*) REMOTE_TASKS["${ref#refs/heads/task/}"]=1 ;; esac
    done <<<"$out"
  fi
  load_worktrees
  G -C "$TOP" for-each-ref --format='%(refname)' refs/remotes/ >"$RUN_DIR/remote-refs.txt"
  SKIPS=()
  NEW_SKIPS=()
  for p in "$wt/$TASK_DIR"/進行中_*.md; do
    [ -e "$p" ] || [ -L "$p" ] || continue   # glob が何にも一致しなかった
    f="${p##*/}"
    name="${f#進行中_}"
    name="${name%.md}"
    # 通常ファイルでないもの(symlink・ディレクトリなど)は黙って外さず、理由つきで読み飛ばす
    if [ ! -f "$p" ] || [ -L "$p" ]; then
      note_skip "$(join_rel "$TASK_DIR" "$f")" "通常ファイルでない(symlink など)"
      continue
    fi
    reason="$(skip_reason "$wt" "$f")"
    if [ -n "$reason" ]; then
      note_skip "$(join_rel "$TASK_DIR" "$f")" "$reason"
      case "$reason" in
        追跡用の*)
          if [ -z "${TRACKING_SEEN[$name]:-}" ]; then TRACKING_SEEN[$name]=1; TRACKING_SKIPPED+=("$name"); fi
          ;;
      esac
      continue
    fi
    date="$(py taskinfo "$p" | sed -n 's/^date=//p')"
    # 5. 並び: 作成日の古い順 → 同じ日はファイル名順 → 作成日なしは最後にファイル名順(C ロケール)
    # 空の欄を作らない(タブは IFS の空白なので、read で空の欄が詰められる)
    if [ -n "$date" ]; then lines+=("0$TAB$date$TAB$f$TAB$name"); else lines+=("1$TAB-$TAB$f$TAB$name"); fi
  done
  CANDIDATES=()
  if [ "${#lines[@]}" -gt 0 ]; then
    while IFS= read -r line; do CANDIDATES+=("$line"); done \
      < <(printf '%s\n' "${lines[@]}" | LC_ALL=C sort -t "$TAB" -k1,1 -k2,2 -k3,3)
  fi
  if [ "${#NEW_SKIPS[@]}" -gt 0 ]; then
    rep "" "### 読み飛ばし(選定 $SEQ)" ""
    for line in "${NEW_SKIPS[@]}"; do rep "- $line"; done
  fi
  if [ "$FIRST_SELECTION" -eq 1 ]; then
    rep "" "## 対象の一覧(task_dir: $TASK_DIR)" ""
    if [ "${#CANDIDATES[@]}" -eq 0 ]; then rep "(無し)"; fi
    for line in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
      IFS="$TAB" read -r _ date f name <<<"$line"
      if [ "$date" = - ]; then rep "- $(join_rel "$TASK_DIR" "$f")(作成日なし)"; else rep "- $(join_rel "$TASK_DIR" "$f")(作成日 $date)"; fi
    done
  fi
  FIRST_SELECTION=0
}

# ── 発見モードの選定と読み飛ばし(loop.md §11)──
clean_cmd() { # $1=DISC_REFS の ref → そのブランチの消し方(1 行)
  case "$1" in
    origin:refs/heads/*) printf 'git push origin --delete %s' "${1#origin:refs/heads/}" ;;
    refs/heads/*) printf 'git branch -D %s' "${1#refs/heads/}" ;;
    refs/remotes/*) printf 'git branch -dr %s' "${1#refs/remotes/}" ;;
  esac
}

# 読み飛ばしの ② 今夜の名 → ③ 未 merge → ④ lock の理由(① この実行で回した、は呼び出し側で見る)。
# → SKIP_WHY(読み飛ばす理由。無ければ空)・SKIP_CLEAN(片付けの定型。改行区切り)
discover_skip() { # $1=発見元
  local s="$1" tonight="task/候補-$1-${DEF_SHA:0:12}" line sha ref re path quoted_path hits=() cmds=() locked_paths=()
  SKIP_WHY=""; SKIP_CLEAN=""
  # ② 今夜の名のブランチ(ローカル・追跡用の ref・origin のどこか。祖先かどうかに関わらない)
  for line in ${DISC_REFS[@]+"${DISC_REFS[@]}"}; do
    sha="${line%%"$TAB"*}"; ref="${line#*"$TAB"}"
    case "$ref" in
      "refs/heads/$tonight"|"origin:refs/heads/$tonight"|refs/remotes/*/"$tonight") hits+=("$ref"); cmds+=("$(clean_cmd "$ref")") ;;
    esac
  done
  if [ "${#hits[@]}" -gt 0 ]; then
    SKIP_WHY="今夜の名のブランチ $tonight がある(${hits[*]})"
    SKIP_CLEAN="$(printf '%s\n' "${cmds[@]}")"
    return 0
  fi
  # ③ 名が task/候補-<発見元>-<12 桁> に完全一致し、固定した sha の祖先でない(か、sha がローカルに無い)ブランチ = 未 merge
  re="^(origin:refs/heads/|refs/heads/|refs/remotes/.+/)task/候補-$s-[0-9a-f]{12}\$"
  for line in ${DISC_REFS[@]+"${DISC_REFS[@]}"}; do
    sha="${line%%"$TAB"*}"; ref="${line#*"$TAB"}"
    [[ "$ref" =~ $re ]] || continue
    if G -C "$TOP" cat-file -e "$sha^{commit}" 2>/dev/null && G -C "$TOP" merge-base --is-ancestor "$sha" "$DEF_SHA" 2>/dev/null; then
      continue
    fi
    hits+=("$ref"); cmds+=("$(clean_cmd "$ref")")
  done
  if [ "${#hits[@]}" -gt 0 ]; then
    SKIP_WHY="未 merge の候補のブランチがある(${hits[*]})"
    SKIP_CLEAN="$(printf '%s\n' "${cmds[@]}")"
    return 0
  fi
  # ④ 失敗の周が残した除外の印
  find_locked_worktrees "dev-workflow-loop: 候補:$s" locked_paths
  if [ "${#locked_paths[@]}" -gt 0 ]; then
    SKIP_WHY="前の周が残した worktree がある($(format_worktree_paths "${locked_paths[@]}"))"
    for path in "${locked_paths[@]}"; do
      printf -v quoted_path '%q' "$path"
      cmds+=("git worktree unlock $quoted_path → git worktree remove $quoted_path(調べてから)")
    done
    SKIP_CLEAN="$(printf '%s\n' "${cmds[@]}")"
  fi
  return 0
}

select_discover() { # 選定中の worktree を作り、DISC_QUEUE(回せる発見元)を並べる
  local s out rc sha ref line
  make_selection_worktree
  DISC_REFS=()
  # origin の task/* の一覧(選定ごとに 1 回。ネットワークの規則。失敗は終了コード 30)
  if [ "$HAS_ORIGIN" -eq 1 ]; then
    rc=0
    out="$(net_git "$NET_TIMEOUT" -C "$TOP" ls-remote origin 'refs/heads/task/*' 2>>"$RUN_DIR/ls-remote.err")" || rc=$?
    [ "$rc" -eq 0 ] || die 30 ls-remote "origin の task/* を読めない(git ls-remote の終了コード $rc)"
    while IFS="$TAB" read -r sha ref; do
      case "$ref" in refs/heads/task/*) DISC_REFS+=("$sha${TAB}origin:$ref") ;; esac
    done <<<"$out"
  fi
  G -C "$TOP" for-each-ref --format='%(objectname)%09%(refname)' refs/heads/task refs/remotes >"$RUN_DIR/disc-refs.txt"
  while IFS="$TAB" read -r sha ref; do
    [ -z "$ref" ] || DISC_REFS+=("$sha$TAB$ref")
  done <"$RUN_DIR/disc-refs.txt"
  load_worktrees
  SKIPS=()
  NEW_SKIPS=()
  DISC_QUEUE=()
  for s in "${DISCOVER_SOURCES[@]}"; do
    [ -z "${DISC_DONE[$s]:-}" ] || continue   # ① この実行で回した(周の報告にあるので、読み飛ばしには出さない)
    discover_skip "$s"
    if [ -n "$SKIP_WHY" ]; then
      note_skip "$s" "$SKIP_WHY"
      if [ -z "${DISC_SKIP[$s]:-}" ] || [ "${DISC_SKIP[$s]}" != "$SKIP_WHY" ]; then
        DISC_SKIP[$s]="$SKIP_WHY"
        DISC_CLEAN[$s]="$SKIP_CLEAN"
      fi
      continue
    fi
    DISC_QUEUE+=("$s")
  done
  if [ "${#NEW_SKIPS[@]}" -gt 0 ]; then
    rep "" "### 読み飛ばし(選定 $SEQ)" ""
    for line in "${NEW_SKIPS[@]}"; do
      rep "- $line"
      s="${line%%: *}"
      while IFS= read -r ref; do [ -z "$ref" ] || rep "  - 片付け: \`$ref\`"; done <<<"${DISC_CLEAN[$s]:-}"
    done
  fi
  if [ "$FIRST_SELECTION" -eq 1 ]; then
    rep "" "## 発見元の列(task_dir: $TASK_DIR)" ""
    if [ "${#DISC_QUEUE[@]}" -eq 0 ]; then rep "(無し)"; fi
    for s in ${DISC_QUEUE[@]+"${DISC_QUEUE[@]}"}; do rep "- $s(ブランチ task/候補-$s-${DEF_SHA:0:12})"; done
  fi
  FIRST_SELECTION=0
}

# origin の refs/heads/<ブランチ> の sha(1 回の ls-remote。ネットワークの規則)→ ORIGIN_BRANCH_SHA(無ければ空)。
# ls-remote が失敗したら 1(終了コードは ORIGIN_LS_RC)
origin_branch_sha() { # $1=ブランチ(refs/heads/ の後ろ)
  local out sha ref
  ORIGIN_BRANCH_SHA=""
  ORIGIN_LS_RC=0
  out="$(net_git "$NET_TIMEOUT" -C "$TOP" ls-remote origin "refs/heads/$1" 2>>"$RUN_DIR/ls-remote.err")" || ORIGIN_LS_RC=$?
  [ "$ORIGIN_LS_RC" -eq 0 ] || return 1
  while IFS="$TAB" read -r sha ref; do [ "$ref" != "refs/heads/$1" ] || ORIGIN_BRANCH_SHA="$sha"; done <<<"$out"
  return 0
}

# ── §6: 周の前の停止条件(最大周回数 → 時間予算 → 停止ファイル)──
check_stop_before_iteration() {
  local now remaining
  if [ "$ITER_COUNT" -ge "$MAX_ITER" ]; then finish 0 "最大周回数($MAX_ITER)に達した"; fi
  now="$(date +%s)"
  remaining=$((BUDGET - (now - LOOP_START)))
  if [ "$remaining" -lt "$ITER_TIMEOUT" ]; then finish 0 "時間予算(残り ${remaining} 秒 < 周の上限 ${ITER_TIMEOUT} 秒)"; fi
  if [ -e "$STOP_FILE" ] || [ -L "$STOP_FILE" ]; then finish 0 "停止ファイルがある($STOP_FILE)"; fi
}

# ── §5: 判定 ──
has_in_head() { G -C "$ITER_WT" cat-file -e "HEAD:$1" 2>/dev/null; }

judge() { # $1=終了コード $2=時間切れか → JUDGE・JUDGE_OK・OUTCOME・DETAIL・HOLD_CODE・DENIALS
  local rc="$1" timed_out="$2" res line head done_rel hold_rel old new rsha lsha out sha ref
  JUDGE=""; JUDGE_OK=0; OUTCOME=""; DETAIL=""; HOLD_CODE=""; DENIALS=""
  res="$(py result "$RUN_DIR/iter-$ITER_SEQ.out" 2>/dev/null || printf 'json=bad\n')"
  while IFS= read -r line; do
    case "$line" in
      outcome=*) OUTCOME="${line#outcome=}" ;;
      detail=*) DETAIL="${line#detail=}" ;;
      denials=*) DENIALS="${line#denials=}" ;;
    esac
  done <<<"$res"
  if [ "$timed_out" -eq 1 ]; then JUDGE="失敗(時間切れ)"; return 0; fi
  if [ "$rc" -ne 0 ]; then JUDGE="失敗(終了コード $rc)"; return 0; fi
  case "$res" in *json=ok*) : ;; *) JUDGE="失敗(JSON が読めない)"; return 0 ;; esac
  if [ -z "$OUTCOME" ]; then JUDGE="失敗(結末の行が無い)"; return 0; fi
  if [ "$OUTCOME" = 失敗扱い ]; then JUDGE="失敗(結末 失敗扱い)"; return 0; fi
  # 結末の行だけを信じず、git の状態と突き合わせる
  head="$(G -C "$ITER_WT" symbolic-ref --quiet HEAD 2>/dev/null || true)"
  if [ "$head" != "refs/heads/task/$ITER_NAME" ]; then
    JUDGE="失敗(食い違い: HEAD が refs/heads/task/$ITER_NAME でない — ${head:-detached})"; return 0
  fi
  done_rel="$(join_rel "$SEL_TASK_DIR" "完了_$ITER_NAME.md")"
  hold_rel="$(join_rel "$SEL_TASK_DIR" "保留_$ITER_NAME.md")"
  if has_in_head "$ITER_REL"; then JUDGE="失敗(食い違い: HEAD の tree に $ITER_REL が残っている)"; return 0; fi
  case "$OUTCOME" in
    PR|縮退)
      if ! has_in_head "$done_rel"; then JUDGE="失敗(食い違い: HEAD の tree に $done_rel が無い)"; return 0; fi
      if [ "$OUTCOME" = PR ]; then
        if [ "$HAS_ORIGIN" -ne 1 ]; then JUDGE="失敗(食い違い: 結末 PR だが origin が無い)"; return 0; fi
        lsha="$(G -C "$ITER_WT" rev-parse HEAD)"
        rc=0
        out="$(net_git "$NET_TIMEOUT" -C "$TOP" ls-remote origin "refs/heads/task/$ITER_NAME" 2>>"$RUN_DIR/ls-remote.err")" || rc=$?
        if [ "$rc" -ne 0 ]; then JUDGE="失敗(判定の ls-remote が失敗した: 終了コード $rc)"; return 0; fi
        rsha=""
        while IFS="$TAB" read -r sha ref; do [ "$ref" != "refs/heads/task/$ITER_NAME" ] || rsha="$sha"; done <<<"$out"
        if [ "$rsha" != "$lsha" ]; then
          JUDGE="失敗(食い違い: origin の task/$ITER_NAME(${rsha:-無い})≠ HEAD $lsha)"; return 0
        fi
        JUDGE="正常(PR)"
      else
        JUDGE="正常(縮退)"
      fi
      ;;
    保留)
      if ! has_in_head "$hold_rel"; then JUDGE="失敗(食い違い: HEAD の tree に $hold_rel が無い)"; return 0; fi
      old="$(G -C "$TOP" cat-file blob "$DEF_SHA:$ITER_REL" | py holdcount -)"
      new="$(G -C "$ITER_WT" cat-file blob "HEAD:$hold_rel" | py holdcount -)"
      if [ "$new" -ne $((old + 1)) ]; then
        JUDGE="失敗(食い違い: 保留の行が 1 行増えていない — 前 $old・後 $new)"; return 0
      fi
      # 最後の保留の行の停止条件から対話点番号を取り出す(T5 の取り出しパターン。D6)
      HOLD_CODE="$(G -C "$ITER_WT" cat-file blob "HEAD:$hold_rel" | py holdcode -)"
      JUDGE="正常(保留)"
      ;;
    *) JUDGE="失敗(結末の行が読めない)"; return 0 ;;
  esac
  JUDGE_OK=1
}

# 周の worktree に未追跡・未 commit が無い(ignore 済みは除く)→ 0。あるか、読めなければ 1(ITER_WT_DIRTY に最初の項目)
iter_wt_clean() {
  local f="$RUN_DIR/iter-$ITER_SEQ.status"
  ITER_WT_DIRTY=""
  if ! G -C "$ITER_WT" status --porcelain=v1 -z --untracked-files=all >"$f" 2>/dev/null; then
    ITER_WT_DIRTY="(git status が失敗した)"
    return 1
  fi
  [ -s "$f" ] || return 0
  ITER_WT_DIRTY="$(head -c 300 "$f" | tr '\0' ' ')"
  return 1
}

# 発見の周の判定(loop.md §11)。材料は終了コード・結末の行・git の状態 → JUDGE・JUDGE_OK・OUTCOME・DETAIL・DISC_PATHS
judge_discover() { # $1=終了コード $2=時間切れか
  local rc="$1" timed_out="$2" res line head parents count out lsha branch="task/$ITER_NAME"
  JUDGE=""; JUDGE_OK=0; OUTCOME=""; DETAIL=""; HOLD_CODE=""; DENIALS=""; DISC_PATHS=""; DISC_PUSHED=""; DISC_ORIGIN_DIFF=0
  res="$(py result "$RUN_DIR/iter-$ITER_SEQ.out" 2>/dev/null || printf 'json=bad\n')"
  while IFS= read -r line; do
    case "$line" in
      outcome=*) OUTCOME="${line#outcome=}" ;;
      detail=*) DETAIL="${line#detail=}" ;;
      denials=*) DENIALS="${line#denials=}" ;;
    esac
  done <<<"$res"
  if [ "$timed_out" -eq 1 ]; then JUDGE="失敗(時間切れ)"; return 0; fi
  if [ "$rc" -ne 0 ]; then JUDGE="失敗(終了コード $rc)"; return 0; fi
  case "$res" in *json=ok*) : ;; *) JUDGE="失敗(JSON が読めない)"; return 0 ;; esac
  if [ -z "$OUTCOME" ]; then JUDGE="失敗(結末の行が無い)"; return 0; fi
  case "$OUTCOME" in
    失敗扱い) JUDGE="失敗(結末 失敗扱い)"; return 0 ;;
    保留) JUDGE="失敗(結末 保留 は発見の周では取りえない)"; return 0 ;;
    PR|縮退)
      # 共通の条件 G: 今夜の名のブランチの上で、固定した sha の上に 1 commit・task_dir の直下の新しい 候補_ だけ・清潔
      head="$(G -C "$ITER_WT" symbolic-ref --quiet HEAD 2>/dev/null || true)"
      if [ "$head" != "refs/heads/$branch" ]; then
        JUDGE="失敗(食い違い: HEAD が refs/heads/$branch でない — ${head:-detached})"; return 0
      fi
      parents="$(G -C "$ITER_WT" rev-parse 'HEAD^@' 2>/dev/null || true)"
      count="$(G -C "$ITER_WT" rev-list --count "$DEF_SHA..HEAD" 2>/dev/null || true)"
      if [ "$parents" != "$DEF_SHA" ] || [ "$count" != 1 ]; then
        JUDGE="失敗(食い違い: HEAD が固定した sha の上の 1 commit でない — 固定した sha からの commit ${count:-?} 個)"; return 0
      fi
      if ! G -C "$ITER_WT" diff --no-renames --raw -z "$DEF_SHA" HEAD >"$RUN_DIR/iter-$ITER_SEQ.diff" 2>/dev/null; then
        JUDGE="失敗(固定した sha との差分を取れない)"; return 0
      fi
      if ! out="$(py candiff "$SEL_TASK_DIR" <"$RUN_DIR/iter-$ITER_SEQ.diff" 2>&1)"; then
        out="${out//$'\n'/ }"
        JUDGE="失敗(食い違い: ${out:0:400})"; return 0
      fi
      DISC_PATHS="$out"
      if ! iter_wt_clean; then
        JUDGE="失敗(食い違い: 作業ツリーに未追跡か未 commit が残った — $ITER_WT_DIRTY)"; return 0
      fi
      if [ "$OUTCOME" = PR ]; then
        if [ "$HAS_ORIGIN" -ne 1 ]; then JUDGE="失敗(食い違い: 結末 PR だが origin が無い)"; return 0; fi
        lsha="$(G -C "$ITER_WT" rev-parse HEAD)"
        if ! origin_branch_sha "$branch"; then JUDGE="失敗(判定の ls-remote が失敗した: 終了コード $ORIGIN_LS_RC)"; return 0; fi
        if [ "$ORIGIN_BRANCH_SHA" != "$lsha" ]; then
          JUDGE="失敗(食い違い: origin の $branch(${ORIGIN_BRANCH_SHA:-無い})≠ HEAD $lsha)"; return 0
        fi
        JUDGE="正常(PR)"
      else
        # 縮退: origin に今夜の名のブランチがあれば、その sha = 判定した HEAD(違う中身を push して、人に PR を作らせない)。
        # ls-remote の失敗は、正常(候補なし)と同じく失敗
        DISC_PUSHED=no
        if [ "$HAS_ORIGIN" -eq 1 ]; then
          if ! origin_branch_sha "$branch"; then JUDGE="失敗(判定の ls-remote が失敗した: 終了コード $ORIGIN_LS_RC)"; return 0; fi
          if [ -n "$ORIGIN_BRANCH_SHA" ]; then
            lsha="$(G -C "$ITER_WT" rev-parse HEAD)"
            if [ "$ORIGIN_BRANCH_SHA" != "$lsha" ]; then
              DISC_ORIGIN_DIFF=1
              JUDGE="失敗(食い違い: 結末 縮退 だが origin の $branch($ORIGIN_BRANCH_SHA)≠ HEAD $lsha)"; return 0
            fi
            DISC_PUSHED=yes
          fi
        fi
        JUDGE="正常(縮退)"
      fi
      ;;
    候補なし)
      head="$(G -C "$ITER_WT" symbolic-ref --quiet HEAD 2>/dev/null || true)"
      if [ -n "$head" ]; then JUDGE="失敗(食い違い: 結末 候補なし だが HEAD が detached でない — $head)"; return 0; fi
      if [ "$(G -C "$ITER_WT" rev-parse HEAD 2>/dev/null || true)" != "$DEF_SHA" ]; then
        JUDGE="失敗(食い違い: 結末 候補なし だが HEAD が固定した sha でない)"; return 0
      fi
      if G -C "$TOP" show-ref --verify --quiet "refs/heads/$branch"; then
        JUDGE="失敗(食い違い: 結末 候補なし だがローカルに $branch がある)"; return 0
      fi
      if [ "$HAS_ORIGIN" -eq 1 ]; then
        if ! origin_branch_sha "$branch"; then JUDGE="失敗(判定の ls-remote が失敗した: 終了コード $ORIGIN_LS_RC)"; return 0; fi
        if [ -n "$ORIGIN_BRANCH_SHA" ]; then
          JUDGE="失敗(食い違い: 結末 候補なし だが origin に $branch がある)"; return 0
        fi
      fi
      if ! iter_wt_clean; then
        JUDGE="失敗(食い違い: 作業ツリーに未追跡か未 commit が残った — $ITER_WT_DIRTY)"; return 0
      fi
      JUDGE="正常(候補なし)"
      ;;
    *) JUDGE="失敗(結末の行が読めない)"; return 0 ;;
  esac
  JUDGE_OK=1
}

# 発見の周の判定の後の報告(候補・報告の写し・縮退の手順・失敗の周の PR の可能性)。worktree の後片付けの後に呼ぶ
discover_after_iteration() {
  local branch="task/$ITER_NAME" s="$ITER_SOURCE" rv p pushed="" found=0
  rv="$RUN_DIR/iter-$ITER_SEQ-reviews"
  [ -d "$rv" ] || rv="$ITER_WT/.claude/reviews"
  if [ -f "$rv/candidates-$s.md" ]; then rep "- 候補モードの報告: $rv/candidates-$s.md"; found=1; fi
  for p in "$rv"/data-audit-iter*.md; do
    [ -f "$p" ] || continue
    rep "- data-audit の監査の報告: $p"; found=1
  done
  [ "$found" -eq 1 ] || rep "- 候補モードの報告: 見つからない(周の中で書かれなかったか、写せなかった)"
  if [ "$JUDGE" = "正常(縮退)" ]; then
    pushed="$DISC_PUSHED"   # 判定の ls-remote で見た(origin のブランチ = 判定した HEAD なら yes)
    DISC_DEGRADED+=("$s$TAB$branch$TAB$pushed")
    rep "- 縮退の後の人の手順(処理するまで、発見元 $s は回らない — 未 merge の候補のブランチとして読み飛ばす):"
    if [ "$pushed" = yes ]; then
      rep "  - push 済みのブランチ: $branch(origin。判定した HEAD と同じ sha)"
      rep "  - 手で PR を作る(<HOST/OWNER/REPO> は人が実名で埋める。loop.sh は実名を自動で埋めない): \`gh pr create -R <HOST/OWNER/REPO> --head $branch --base $DEF_NAME\`" \
        "  - PR を作らないときの消し方: \`git push origin --delete $branch\` と \`git branch -D $branch\`"
    else
      if [ "$HAS_ORIGIN" -eq 1 ]; then
        rep "  - push していないとき(origin に $branch が無い): ローカルのブランチを merge するか、消す"
      else
        rep "  - origin が無く、push していない: ローカルのブランチ $branch を merge するか、消す"
      fi
      rep "    - merge する: デフォルトブランチ($DEF_NAME)の上で \`git merge $branch\` → \`git branch -d $branch\`" \
        "    - 消す: \`git branch -D $branch\`"
    fi
  fi
  if [ "$DISC_ORIGIN_DIFF" -eq 1 ]; then
    rep "- origin の $branch が判定した中身(HEAD)と違う: PR を作らずに消す(\`git push origin --delete $branch\`)。開いた PR があれば merge せずに閉じる"
  fi
  if [ "$JUDGE_OK" -ne 1 ] && [ "$HAS_ORIGIN" -eq 1 ]; then
    # 失敗の周: 理由を問わず、origin に今夜の名のブランチがあるかを 1 回の ls-remote で確かめる(失敗しても止めない)
    if origin_branch_sha "$branch"; then
      if [ -n "$ORIGIN_BRANCH_SHA" ]; then
        rep "- origin に $branch がある: PR が開いている可能性がある。merge せずに閉じ、ブランチを消す(\`git push origin --delete $branch\`)"
      else
        rep "- origin に $branch は無い"
      fi
    else
      rep "- origin に $branch があるかを確かめられない(ls-remote の終了コード $ORIGIN_LS_RC)。PR が開いていたら、merge せずに閉じ、ブランチを消す"
    fi
  fi
}

# 発見モードの朝の報告の終わり(finish から呼ぶ。選定が 1 回以上あったときだけ)
report_discover_end() {
  local s line f b p
  [ "$DISCOVER" -eq 1 ] && [ "$SEQ" -gt 0 ] || return 0
  rep "" "## 発見元ごとの要約" ""
  for s in "${DISCOVER_SOURCES[@]}"; do
    if [ -n "${DISC_RESULT[$s]:-}" ]; then
      rep "- $s: ${DISC_RESULT[$s]}"
    elif [ -n "${DISC_SKIP[$s]:-}" ]; then
      rep "- $s: 読み飛ばし — ${DISC_SKIP[$s]}"
      while IFS= read -r line; do [ -z "$line" ] || rep "  - 片付け: \`$line\`"; done <<<"${DISC_CLEAN[$s]:-}"
    else
      rep "- $s: 回していない"
    fi
  done
  if [ "${#DISC_DEGRADED[@]}" -gt 0 ]; then
    rep "" "## 縮退の周のブランチ(処理するまで、その発見元は回らない)" ""
    for line in "${DISC_DEGRADED[@]}"; do
      IFS="$TAB" read -r f b p <<<"$line"
      case "$p" in
        yes) rep "- $f: $b(push 済み。PR を手で作るか、消す — 周の報告の手順)" ;;
        *) rep "- $f: $b(push していない。ローカルのブランチを merge するか、消す — 周の報告の手順)" ;;
      esac
    done
  fi
  rep "" "## 候補の PR の片付けと採用の手順" ""
  rep "- merge commit で merge して pull した後: 作業ブランチを消す(\`git branch -d task/候補-<発見元>-<sha>\`・\`git push origin --delete task/候補-<発見元>-<sha>\`)。デフォルトブランチの祖先になるので、消す前でも読み飛ばしには数えない" \
    "- squash・rebase で merge したとき: ブランチの sha がデフォルトブランチの祖先にならないので、消すまで、その発見元は回らない(\`git branch -D task/候補-<発見元>-<sha>\`・\`git push origin --delete task/候補-<発見元>-<sha>\`。手元の追跡用の ref が残れば \`git branch -dr origin/task/候補-<発見元>-<sha>\`)" \
    "- PR を閉じたとき(merge しない): 上と同じく、消すまで、その発見元は回らない。候補は task_dir に入らないので、同じ指摘がまた出うる" \
    "- 採用: merge して pull した後に、人が対話で \`/create-task <候補_ のパス>\` を打つ(見送りの行がある候補は止まる)" \
    "- 失敗の周が残した worktree(lock の理由 \`dev-workflow-loop: 候補:<発見元>\`)は、調べてから消すまで、その発見元は回らない"
}

# 許可の仲介の判定の記録(PERMLOG)を読み、周ごとの deny を報告に写す → PERM_DENY・PERM_KINDS
read_permlog() {
  local out line
  PERM_ALLOW=0; PERM_DENY=0; PERM_KINDS=""
  out="$(py permlog "$ITER_PERMLOG")"
  while IFS= read -r line; do
    case "$line" in
      allow=*) PERM_ALLOW="${line#allow=}" ;;
      deny=*) PERM_DENY="${line#deny=}" ;;
      kinds=*) PERM_KINDS="${line#kinds=}" ;;
    esac
  done <<<"$out"
  while IFS= read -r line; do
    case "$line" in reference-error=*) rep "- 許可の参考記録を読めない(制御判断には使わない): ${line#reference-error=}" ;; esac
  done <<<"$out"
  rep "- 許可の仲介(D22、参考表示のみ): allow $PERM_ALLOW 件・deny $PERM_DENY 件${PERM_KINDS:+(種類: $PERM_KINDS)}。記録: $ITER_PERMLOG"
  if [ "$PERM_DENY" -gt 0 ]; then
    printf '%s\n' "$out" | sed -n 's/^line=/  - deny: /p' >>"$REPORT"
  fi
}

# 正常の周の後片付け: .claude/reviews を写す → unlock → remove(--force なし)。拒否されたら残して lock し直す
remove_iteration_worktree() {
  local rv="$ITER_WT/.claude/reviews" size err="$RUN_DIR/iter-$ITER_SEQ.remove.err"
  if [ -d "$rv" ] && [ ! -L "$rv" ]; then
    size="$(du -sb -- "$rv" 2>/dev/null | cut -f1 || true)"
    if [ -z "$size" ]; then
      rep "- .claude/reviews: 大きさを測れないので写さない"
    elif [ "$size" -le "$REVIEWS_COPY_LIMIT" ]; then
      # symlink はたどらずに写す(cp -a は symlink を symlink のまま写す)。写しに失敗しても周は止めない
      if cp -a -- "$rv" "$RUN_DIR/iter-$ITER_SEQ-reviews" 2>"$RUN_DIR/iter-$ITER_SEQ-reviews.err"; then
        rep "- .claude/reviews を写した: $RUN_DIR/iter-$ITER_SEQ-reviews"
      else
        rep "- .claude/reviews を写せなかった(途中まで写ったものは $RUN_DIR/iter-$ITER_SEQ-reviews。$(head -c 300 "$RUN_DIR/iter-$ITER_SEQ-reviews.err" | tr '\n' ' '))"
      fi
    else
      rep "- .claude/reviews は上限(50 MB)を超えるので写さない($size バイト)"
    fi
  fi
  G -C "$TOP" worktree unlock "$ITER_WT" >/dev/null 2>&1 || true
  if G -C "$TOP" worktree remove "$ITER_WT" >/dev/null 2>"$err"; then
    rep "- worktree: 消した"
    REMOVED_PARENT_WORKTREES+=("$ITER_WT")
  else
    G -C "$TOP" worktree lock --reason "dev-workflow-loop: $ITER_REL" "$ITER_WT" >/dev/null 2>&1 || true
    WT_OUTCOME[$ITER_WT]="周 $ITER_COUNT: $JUDGE(worktree を消せなかった。未追跡・未 commit が残った)"
    rep "- worktree: 消せなかったので残した(未追跡・未 commit が残った。失敗には数えない — D4): $ITER_WT($(head -c 300 "$err" | tr '\n' ' '))"
  fi
}

# ── §4: 1 周 ──
ITER_COUNT=0
CONSEC_FAIL=0
CONSEC_G1=0
run_iteration() { # 候補の先頭の 1 件(発見モードでは発見元の列の先頭)を回す
  local date f name start now rc timed_out pid dur
  if [ "$DISCOVER" -eq 1 ]; then
    # 発見モード: 名 = 今夜の名のブランチの task/ の後ろ・lock の理由は「候補:<発見元>」(loop.md §11)
    ITER_SOURCE="${DISC_QUEUE[0]}"
    ITER_NAME="候補-$ITER_SOURCE-${DEF_SHA:0:12}"
    ITER_REL="候補:$ITER_SOURCE"
    ITER_TITLE="発見元 $ITER_SOURCE"
    ITER_PROMPT="/dev-workflow:ship-task --discover=$ITER_SOURCE --unattended"
    ITER_META_EXTRA="mode=discover
source=$ITER_SOURCE
"
    DISC_DONE[$ITER_SOURCE]=1
  else
    IFS="$TAB" read -r _ date f name <<<"${CANDIDATES[0]}"
    ITER_NAME="$name"
    ITER_REL="$(join_rel "$SEL_TASK_DIR" "$f")"
    ITER_TITLE="$ITER_REL"
    ITER_PROMPT="/dev-workflow:ship-task --task=$ITER_REL --unattended"
    ITER_META_EXTRA=""
  fi
  # H31: 周の worktree の lock を付け替え、周の途中の印を置く前に、ホスト CLI の実体を内容の sha256 まで照らす
  # (止まっても周の途中の印は残らない。終わりの環境の照合も実体の違いで通らないので、選定中の worktree は
  # 「選定中」の lock のまま残して報告する。この後の起動の直前は、環境の照合の中の stat で照らす)
  host_same --hash || die 20 host-proof "周を始める前に、ホスト CLI の実体($HOST_EXEC)が確かめた時と違う(更新・書き換え・差し替え)。$PROVE_HINT"
  recheck_component_proof
  ITER_WT="$SEL_WT"
  ITER_SEQ="$SEQ"
  ITER_ID="$RUN_ID-$SEQ"
  ITER_COUNT=$((ITER_COUNT + 1))
  # 7. lock の理由を付け替える(残った worktree = 除外の印)
  G -C "$TOP" worktree unlock "$ITER_WT" >/dev/null
  G -C "$TOP" worktree lock --reason "dev-workflow-loop: $ITER_REL" "$ITER_WT" >/dev/null
  SEL_WT=""
  ITER_WTADMIN="$(G -C "$ITER_WT" rev-parse --path-format=absolute --git-dir)"
  ITER_PERMLOG="$RUN_DIR/iter-$ITER_SEQ.permlog"   # 許可の仲介の判定の記録(D22 ③。hook が 1 行ずつ足す)
  # 8. 共有の git ディレクトリの状態を控える(周の後の照合の比べる元。周ごとに取り直す)
  ITER_BASE="$RUN_DIR/iter-$ITER_SEQ.base.json"
  # 初回には保存済み state は無い。存在した周だけ、開始時からの
  # secret_paths の和を引き継ぐ(初回に未作成パスを補助へ渡さない)。
  if [ -f "$LAST_VERIFIED" ]; then
    if ! take_snapshot "$ITER_BASE" "$ITER_WTADMIN" "$LAST_VERIFIED" "$FRESH_CONFIG_BASELINE"; then
      die 20 state-snapshot "周の開始直前の状態を安全に控えられない。${SNAPSHOT_DIFF:-状態を控えられない}"
    fi
  else
    if ! take_snapshot "$ITER_BASE" "$ITER_WTADMIN"; then
      die 20 state-snapshot "周の開始直前の状態を安全に控えられない。${SNAPSHOT_DIFF:-状態を控えられない}"
    fi
  fi
  FRESH_CONFIG_BASELINE=0
  # 比べる元の sha256 をシェルの変数に持つ(周の中で状態ディレクトリのファイルを書き換えられても気づく)
  if ! ITER_BASE_SHA="$(state_digest "$ITER_BASE")"; then
    # The digest rejection itself means that a later finish/report Git call
    # must not reload a config graph changed alongside this state pathname.
    STATE_GIT_UNSAFE=1
    die 20 state-snapshot "周の開始直前の状態を安全に保持できない"
  fi
  verify_environment || die 20 environment "子の起動直前に plugin または利用者設定が変わった"
  # 周の途中の印(周の起動の直前に置く)
  rm -rf -- "$STATE/inflight.tmp"
  mkdir "$STATE/inflight.tmp"
  cp -- "$ITER_BASE" "$STATE/inflight.tmp/base.json"
  printf 'iter=%s\nname=%s\nrel=%s\ndef_name=%s\ndef_sha=%s\nwtadmin=%s\nwt=%s\nrun=%s\n' \
    "$ITER_ID" "$ITER_NAME" "$ITER_REL" "$DEF_NAME" "$DEF_SHA" "$ITER_WTADMIN" "$ITER_WT" "$RUN_ID" \
    >"$STATE/inflight.tmp/meta"
  printf '%s' "$ITER_META_EXTRA" >>"$STATE/inflight.tmp/meta"
  mv -T -- "$STATE/inflight.tmp" "$INFLIGHT"
  # A prior iteration may have promoted a verified after-state.  It is never
  # evidence for this child: a signal before this child's verification must
  # run the ordinary cleanup and fresh verification path.
  ITER_VERIFIED=""
  ITER_VERIFIED_SHA=""
  VERIFY_DIFF=""
  ITER_ACTIVE=1
  rep "" "### 周 $ITER_COUNT: $ITER_TITLE" "" "- 周の識別子: $ITER_ID" "- worktree: $ITER_WT"
  [ "$DISCOVER" -eq 0 ] || rep "- 今夜の名のブランチ: task/$ITER_NAME"
  printf '%s\n' "$ITER_PROMPT" >"$RUN_DIR/iter-$ITER_SEQ.prompt"
  start="$(date +%s)"
  # 子: supervisor が別セッションで起動し、子孫を回収する。DEV_WORKFLOW_HOST_CLI を外し、
  # 周の印を付け、ロックの fd を閉じる。プロンプトは stdin、出力はファイルへ(パイプにしない)。
  # OLDPWD も外す(直前の cd で worktree の外を指す。周の中の `cd -` の行き先にさせない)
  # H32: 自動メモリは周の子だけで無効にする。親の値や利用者の設定・既存メモリは変更しない。
  SUPERVISOR_RESULT="$RUN_DIR/iter-$ITER_SEQ.supervisor.json"
  SUPERVISOR_NONCE="$("$PY_ABS" -c 'import secrets; print(secrets.token_hex(32))')"
  SUPERVISOR_RC=97
  verify_environment || die 20 environment "監督プロセスの起動直前に環境が変わった"
  (
    exec 7>&-
    cd "$ITER_WT"
    exec "$ENV_BIN" -u DEV_WORKFLOW_HOST_CLI -u OLDPWD DEV_WORKFLOW_LOOP_ITER="$ITER_ID" \
      DEV_WORKFLOW_LOOP_WORKTREE="$ITER_WT" DEV_WORKFLOW_LOOP_PERMLOG="$ITER_PERMLOG" \
      DEV_WORKFLOW_ENV_STATE="$ENVIRONMENT_STATE" DEV_WORKFLOW_ENV_SHA256="$ENVIRONMENT_SHA" \
      DEV_WORKFLOW_ENV_GUARD="$TRUSTED_ENV_GUARD" DEV_WORKFLOW_ENV_GUARD_SHA256="$ENV_GUARD_SHA" \
      DEV_WORKFLOW_LOOP_PLUGIN_ROOT="$PLUGIN_ROOT" DEV_WORKFLOW_LOOP_ALLOW="$ALLOW_JSON" \
      DEV_WORKFLOW_LOOP_PUSH_REPO="$O_REPO" DEV_WORKFLOW_LOOP_PUSH_REF="refs/heads/task/$ITER_NAME" \
      CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
      "$PY_ABS" "$SUPERVISOR" --result "$SUPERVISOR_RESULT" --nonce "$SUPERVISOR_NONCE" \
      --timeout "$ITER_TIMEOUT" --grace "$KILL_GRACE" -- "${CHILD_ARGV[@]}" \
      <"$RUN_DIR/iter-$ITER_SEQ.prompt" >"$RUN_DIR/iter-$ITER_SEQ.out" 2>"$RUN_DIR/iter-$ITER_SEQ.err"
  ) &
  pid=$!
  CHILD_PID="$pid"
  CHILD_START=""
  ITER_PGID="$pid"
  timed_out=0
  local control_rc=0
  CHILD_START="$(py supervisor-control capture "$pid" "$$")" || control_rc=$?
  if [ "$control_rc" -ne 0 ] && [ "$control_rc" -ne 3 ]; then
    timed_out=1
  else
    while [ "$control_rc" -eq 0 ]; do
      control_rc=0
      py supervisor-control alive "$pid" "$$" "$CHILD_START" || control_rc=$?
      [ "$control_rc" -ne 3 ] || break
      if [ "$control_rc" -ne 0 ]; then timed_out=1; break; fi
      now="$(date +%s)"
      if [ $((now - start)) -ge "$((ITER_TIMEOUT + KILL_GRACE + 7))" ]; then timed_out=1; break; fi
      nap 1
    done
  fi
  rc=0
  if [ "$timed_out" -eq 0 ]; then SUPERVISOR_RC=0; wait "$pid" || SUPERVISOR_RC=$?; CHILD_PID=""; CHILD_START=""; fi
  # 片付け(時間切れでも正常に終わっても、判定より前に必ず行う)。止めた子についての bash の通知
  # (「Killed」など)は周のログへ向ける
  cleanup_iteration 2>>"$RUN_DIR/iter-$ITER_SEQ.err"
  dur=$(( $(date +%s) - start ))
  if [ "$timed_out" -eq 1 ]; then rep "- 終了コード: 時間切れ(${ITER_TIMEOUT} 秒)"; else rep "- 終了コード: $rc"; fi
  rep "- 所要時間: ${dur} 秒" "- ログ: $RUN_DIR/iter-$ITER_SEQ.out・$RUN_DIR/iter-$ITER_SEQ.err"
  if [ -n "$CLEANUP_LEFT" ]; then
    # 残ったプロセスがまだ書き換えうるので、照合も判定もせず、次の周へ進まない
    WT_OUTCOME[$ITER_WT]="周 $ITER_COUNT: 片付けで残ったプロセスがあり止まった(判定していない)"
    place_stop_mark "残ったプロセス(周 $ITER_ID の片付けで止められなかった: $CLEANUP_LEFT)" "" 1
    rep "- 片付け: 残ったプロセス $CLEANUP_LEFT(照合・判定をせずに止まる。周の途中の印と worktree を残す)"
    die 10 leftover "周 $ITER_ID の片付けでプロセスが残った($CLEANUP_LEFT)。$(stop_mark_guide)"
  fi
  rep "- 片付け: 残ったプロセスは無い"
  # 照合(D14・D8)。§5 のネットワークの git より前
  verify_iteration
  if [ -n "$VERIFY_DIFF" ]; then
    WT_OUTCOME[$ITER_WT]="周 $ITER_COUNT: 共有の状態の変化で止まった(判定していない)"
    place_stop_mark "共有の状態の変化(周 $ITER_ID の後の照合)" "$VERIFY_DIFF" 0
    ITER_ACTIVE=0
    rep "- 共有の状態の変化:" "$VERIFY_DIFF"
    die 10 shared-state "周 $ITER_ID の後に共有の git の状態が変わった。$(stop_mark_guide)"
  fi
  rep "- 共有の config の差分: 無し"
  # §5 の判定と後片付け(発見モードは §11 の判定)
  if [ "$DISCOVER" -eq 1 ]; then judge_discover "$rc" "$timed_out"; else judge "$rc" "$timed_out"; fi
  rep "- 結末: ${OUTCOME:-(無し)}${DETAIL:+ — $DETAIL}" "- 判定: $JUDGE"
  [ -z "$HOLD_CODE" ] || rep "- 保留の停止条件の対話点番号: $HOLD_CODE"
  [ -z "$DENIALS" ] || rep "- ホストの結果の拒否の欄: $DENIALS"
  if [ "$DISCOVER" -eq 1 ]; then
    DISC_RESULT[$ITER_SOURCE]="$JUDGE — 結末 ${OUTCOME:-(無し)}${DETAIL:+ — $DETAIL}"
    if [ -n "$DISC_PATHS" ]; then
      rep "- 候補: $(printf '%s\n' "$DISC_PATHS" | wc -l | tr -d ' ') 件"
      while IFS= read -r f; do rep "  - $f"; done <<<"$DISC_PATHS"
    fi
  fi
  read_permlog
  begin_promotion_critical
  if [ "$JUDGE_OK" -eq 1 ]; then
    remove_iteration_worktree
    if [ "$OUTCOME" = 保留 ] && [ "$HOLD_CODE" = G1 ]; then
      CONSEC_FAIL=$((CONSEC_FAIL + 1))
    else
      CONSEC_FAIL=0
    fi
    if [ "$OUTCOME" = 保留 ]; then HELD_NAMES+=("$ITER_NAME"); fi
  else
    WT_OUTCOME[$ITER_WT]="周 $ITER_COUNT: $JUDGE"
    rep "- worktree: 残した(lock の理由: dev-workflow-loop: $ITER_REL)"
    CONSEC_FAIL=$((CONSEC_FAIL + 1))
  fi
  # A normal PR/候補なしの周では own worktree を先に消してから基準を更新する。
  # 消した admin の config.worktree/origin を次の周の preflight へ残すと、
  # Git が正しく作成・削除したものまで未検査の欠落として止まってしまう。
  # 残した worktree はここでなお観察され、次の起動でも厳密に照合される。
  if ! save_last_verified "$ITER_VERIFIED" "$ITER_VERIFIED_SHA"; then
    WT_OUTCOME[$ITER_WT]="周 $ITER_COUNT: 基準を昇格できず止まった"
    place_stop_mark "共有の状態の変化(周 $ITER_ID の後の基準昇格)" "${SNAPSHOT_DIFF:-差分: 最後に照合に通った状態を更新できない}" 1
    rep "- 基準を昇格できない:" "${SNAPSHOT_DIFF:-}"
    ITER_ACTIVE=0
    end_promotion_critical
    die 10 shared-state "周 $ITER_ID の後の状態を基準へ昇格できない。$(stop_mark_guide)"
  fi
  rm -rf -- "$INFLIGHT"
  ITER_ACTIVE=0
  end_promotion_critical
  [ "$DISCOVER" -eq 0 ] || discover_after_iteration
  if [ "$JUDGE_OK" -eq 1 ] && [ "$OUTCOME" = 保留 ] && [ "$HOLD_CODE" = G1 ]; then
    # PERMLOG は子が書ける参考記録であり、種類・欠落・削除・順序で
    # ブレーカーを緩めない。protected だけの deny も同じ G1 と数える。
    CONSEC_G1=$((CONSEC_G1 + 1))
    rep "- G1 の保留: 拒否記録の種類にかかわらず連続 $CONSEC_G1 回として数える"
  else
    CONSEC_G1=0
  fi
  say "周 $ITER_COUNT: $ITER_TITLE → $JUDGE"
  ITER_ID=""; ITER_PGID=""
  # §6: 周の後の停止条件
  if [ "$CONSEC_FAIL" -ge "$MAX_FAIL" ]; then die 10 consecutive-failures "連続失敗($CONSEC_FAIL 回)"; fi
  if [ "$CONSEC_G1" -ge "$MAX_G1_HOLDS" ]; then
    die 10 g1-holds "許可の拒否(G1)の保留が $CONSEC_G1 回続いた(許可リストを見直す)"
  fi
}

# ═══════════════════════════════ 本体 ═══════════════════════════════

trap 'ERR_LINE=$LINENO' ERR
trap on_exit EXIT
trap 'on_signal TERM 143' TERM
trap 'on_signal HUP 129' HUP
trap 'on_signal INT 130' INT

# ── 最初に: git のローカルな環境変数を外す(§2)──
# `git rev-parse --local-env-vars` の列(GIT_DIR・GIT_CONFIG_PARAMETERS・GIT_CONFIG_COUNT など)と
# GIT_CONFIG_KEY_<n>・GIT_CONFIG_VALUE_<n>。GIT_CONFIG_GLOBAL・GIT_CONFIG_SYSTEM・GIT_CONFIG_NOSYSTEM は
# 利用者の側の値として残す
command -v git >/dev/null 2>&1 || die 20 tool-missing "git が PATH に無い"
LOCAL_ENV_VARS="$(
  # この能力照会自身が cwd や利用者設定を読まないよう、全 Git 環境を
  # 外した repository 外の文脈で実行する。返された列は親へ適用する。
  for startup_env in $(compgen -e); do
    case "$startup_env" in GIT_*) unset "$startup_env" ;; esac
  done
  export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
  cd /
  G rev-parse --local-env-vars
)"
for v in $LOCAL_ENV_VARS; do unset "$v"; done
for v in $(compgen -e); do
  case "$v" in GIT_CONFIG_KEY_*|GIT_CONFIG_VALUE_*) unset "$v" ;; esac
done
export GIT_NO_LAZY_FETCH=1

# ── §2 の 1: 引数の検査(使い方の誤りは exit 2)──
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) need_val "$1" "$#"; REPO="$2"; shift 2 ;;
    --host) need_val "$1" "$#"; HOST="$2"; shift 2 ;;
    --host-argv) need_val "$1" "$#"; HOST_ARGV_OVERRIDE+=("$2"); shift 2 ;;
    --only) need_val "$1" "$#"; ONLY+=("$2"); shift 2 ;;
    # 発見モード。値は `=` の形だけ(`--discover data-audit` の data-audit は下の「不明な引数」になる)
    --discover|--discover=*)
      [ "$DISCOVER" -eq 0 ] || fail_usage "--discover を 2 回書いた"
      DISCOVER=1
      case "$1" in --discover=*) DISCOVER_FROM_ARG=1; DISCOVER_ARG="${1#--discover=}" ;; esac
      shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --prove-host) PROVE_HOST=1; shift ;;
    --allowed-tools)
      need_val "$1" "$#"
      # 値が `-` で始まると、ホスト CLI にフラグとして読まれる
      case "$2" in -*) fail_usage "--allowed-tools の値は '-' で始められない: '$2'" ;; esac
      ALLOWED_TOOLS+=("$2"); shift 2 ;;
    --allow-classifier) ALLOW_CLASSIFIER=1; shift ;;
    --mcp-config) need_val "$1" "$#"; MCP_CONFIGS+=("$2"); shift 2 ;;
    --max-iterations) need_val "$1" "$#"; pos_int "$1" "$2"; ARG_MAX_ITER="$2"; shift 2 ;;
    --max-consecutive-failures) need_val "$1" "$#"; pos_int "$1" "$2"; ARG_MAX_FAIL="$2"; shift 2 ;;
    --time-budget) need_val "$1" "$#"; pos_int "$1" "$2"; ARG_BUDGET="$2"; shift 2 ;;
    --iteration-timeout) need_val "$1" "$#"; pos_int "$1" "$2"; ARG_ITER_TIMEOUT="$2"; shift 2 ;;
    --kill-grace) need_val "$1" "$#"; pos_int "$1" "$2"; KILL_GRACE="$2"; shift 2 ;;
    --net-timeout) need_val "$1" "$#"; pos_int "$1" "$2"; NET_TIMEOUT="$2"; shift 2 ;;
    --stop-file) need_val "$1" "$#"; STOP_FILE="$2"; shift 2 ;;
    --worktree-root) need_val "$1" "$#"; WT_ROOT="$2"; shift 2 ;;
    --state-max-items) need_val "$1" "$#"; pos_int "$1" "$2"; STATE_MAX_ITEMS="$2"; shift 2 ;;
    --state-max-bytes) need_val "$1" "$#"; pos_int "$1" "$2"; STATE_MAX_BYTES="$2"; shift 2 ;;
    --state-max-file-bytes) need_val "$1" "$#"; pos_int "$1" "$2"; STATE_MAX_FILE_BYTES="$2"; shift 2 ;;
    --state-max-seconds) need_val "$1" "$#"; pos_int "$1" "$2"; STATE_MAX_SECONDS="$2"; shift 2 ;;
    -h|--help) usage; EXPLICIT_EXIT=1; exit 0 ;;
    *) fail_usage "不明な引数: $1" ;;
  esac
done
[ -n "$HOST" ] || fail_usage "--host が空"
if [ "$PROVE_HOST" -eq 1 ]; then
  # 実 hook の確認だけを行う。周の選定・発見と混ぜない
  [ "$DRY_RUN" -eq 0 ] || fail_usage "--prove-host と --dry-run は併用できない(--prove-host はセッションを 1 回起動する)"
  [ "$DISCOVER" -eq 0 ] || fail_usage "--prove-host と --discover は併用できない"
  [ "${#ONLY[@]}" -eq 0 ] || fail_usage "--prove-host と --only は併用できない"
fi
for v in ${ONLY[@]+"${ONLY[@]}"}; do [ -n "$v" ] || fail_usage "--only が空"; done
if [ "$DISCOVER" -eq 1 ]; then
  # 発見元の列(loop.md §11): 空の要素・重複・未知の名前・--only との併用は使い方の誤り
  [ "${#ONLY[@]}" -eq 0 ] || fail_usage "--discover と --only は併用できない(1 回の実行はどちらかのモードだけ)"
  if [ "$DISCOVER_FROM_ARG" -eq 1 ]; then
    case ",$DISCOVER_ARG," in *,,*) fail_usage "--discover= の値に空の要素がある: '$DISCOVER_ARG'" ;; esac
    case "$DISCOVER_ARG" in *[!a-z0-9,-]*) fail_usage "--discover= の値に使えない文字がある: '$DISCOVER_ARG'(列: ${DISCOVER_DEFAULT[*]})" ;; esac
    IFS=, read -r -a DISCOVER_SOURCES <<<"$DISCOVER_ARG"
    DISCOVER_SRC_DESC="引数"
  else
    DISCOVER_SOURCES=("${DISCOVER_DEFAULT[@]}")
    DISCOVER_SRC_DESC="既定"
  fi
  for v in "${DISCOVER_SOURCES[@]}"; do
    known=0
    for k in "${DISCOVER_DEFAULT[@]}"; do [ "$k" != "$v" ] || known=1; done
    [ "$known" -eq 1 ] || fail_usage "--discover の未知の発見元: '$v'(列: ${DISCOVER_DEFAULT[*]})"
    [ -z "${DISC_SEEN[$v]:-}" ] || fail_usage "--discover の発見元が重複している: '$v'"
    DISC_SEEN[$v]=1
  done
fi
for v in ${ALLOWED_TOOLS[@]+"${ALLOWED_TOOLS[@]}"}; do [ -n "$v" ] || fail_usage "--allowed-tools が空"; done
for v in ${HOST_ARGV_OVERRIDE[@]+"${HOST_ARGV_OVERRIDE[@]}"}; do [ -n "$v" ] || fail_usage "--host-argv が空"; done
i=0
while [ "$i" -lt "${#MCP_CONFIGS[@]}" ]; do
  f="${MCP_CONFIGS[$i]}"
  { [ -n "$f" ] && [ -f "$f" ]; } || fail_usage "--mcp-config のファイルが無い: '$f'"
  # 周の cwd は worktree なので、絶対パスにして渡す
  MCP_CONFIGS[$i]="$(cd -P -- "$(dirname -- "$f")" && pwd -P)/$(basename -- "$f")"
  i=$((i + 1))
done
[ -z "$STOP_FILE" ] || STOP_FILE="$(abs_path "$STOP_FILE")"
[ -z "$WT_ROOT" ] || WT_ROOT="$(abs_path "$WT_ROOT")"
[ -z "$REPO" ] || REPO="$(abs_path "$REPO")"

# ── §2 の 2: ホストのセッションの中でない(D7)──
check_host_session
# 子のホスト CLI には渡さない(D7)
unset DEV_WORKFLOW_HOST_CLI
# H31: ホストの読み込みを変える環境変数(--bare 相当の CLAUDE_CODE_SIMPLE・CLAUDE_CODE_SAFE_MODE)が立っていれば、
# hook の自動の読み込みと認証が変わり、証明を取った起動の形の外になるので起動しない(子へ持ち越さない)
for v in CLAUDE_CODE_SIMPLE CLAUDE_CODE_SAFE_MODE; do
  [ -z "${!v:-}" ] || die 20 hooks-disabled "環境変数 $v が立っている(ホストが hook などの自動の読み込みを止め、証明を取った起動の形の外になる)。外してから起動する"
done
for v in CLAUDE_CODE_REMOTE_SETTINGS_PATH CLAUDE_CODE_MANAGED_SETTINGS_PATH CLAUDE_CODE_MOCK_REMOTE_SETTINGS; do
  [ -z "${!v:-}" ] || die 20 host-config-source "環境変数 $v が立っている(ホストの設定入口を変える)。外してから起動する"
done
# H49: hook の起動を別のプログラムで包む指定は、許可の仲介を確かめた起動の形の外になるので受け付けない。
[ -z "${CLAUDE_CODE_SHELL_PREFIX:-}" ] || die 20 shell-prefix "環境変数 CLAUDE_CODE_SHELL_PREFIX が立っている(hook の起動を別のプログラムで包む)。外してから起動する"
# #107 H48: 分類器の自動承認では、許可の仲介の hook を通らずに worktree の外へ書けた(Claude Code 2.1.289 の実測)。
# H31 の証明を書けない形なので、--allow-classifier の無人ループは起動しない(--prove-host・--dry-run を含む)
[ "$ALLOW_CLASSIFIER" -eq 0 ] || die 20 classifier "--allow-classifier の無人ループは起動しない。分類器の自動承認では、許可の仲介の hook を通らずに worktree の外へ書けた(Claude Code 2.1.289 の実測。#107 の H48)。既定の編集の自動許可で起動する"

# ── §2 の 3: 道具(OS/bash は初期化前に検査済み) ──
for t in setsid flock python3 timeout realpath stat; do
  command -v "$t" >/dev/null 2>&1 || die 20 tool-missing "$t が PATH に無い"
done

# ── §2 の 4: プラグインのルートと名前(D11)──
# 自身の物理パスから 4 階層上(scripts/ から 3 階層上)。setup.sh の --link の配置は clone に解決される
SELF="$(readlink -f -- "${BASH_SOURCE[0]}")"
PLUGIN_ROOT="$(dirname -- "$(dirname -- "$(dirname -- "$(dirname -- "$SELF")")")")"
ORIGINAL_PLUGIN_ROOT="$PLUGIN_ROOT"
PY_ABS="$(command -v python3)"
case "$PY_ABS" in /*) : ;; *) die 20 tool-missing "python3 を絶対パスに解決できない" ;; esac
# 設定本文を読む前に、cwd に依存しない host 用の HOME/config だけを検査する。
ENV_GUARD_PY="$PLUGIN_ROOT/skills/ship-task/scripts/environment-guard.py"
HOST_PATHS_OUT="$(cd / && "$PY_ABS" -B "$ENV_GUARD_PY" host-paths)" \
  || die 20 host-config-path "HOME または CLAUDE_CONFIG_DIR のパスが起動条件を満たさない"
# 初回配布元の helper で管理入口と印だけを確認する。Git・利用者設定・
# 保存した周の本文は読まない。未信頼の印から実行や信頼集合を復活させない。
STATE_GIT_UNSAFE=1
REPO="${REPO:-$PWD}"
STATE_BASE="${XDG_STATE_HOME:-${HOME:?HOME が無い}/.local/state}/dev-workflow/loop"
LOOP_STARTUP_PY="$ORIGINAL_PLUGIN_ROOT/skills/ship-task/scripts/loop-startup.py"
EARLY_STARTUP="$("$PY_ABS" -B "$LOOP_STARTUP_PY" --repo "$REPO" --state-base "$STATE_BASE")" \
  || die 20 startup-state "環境を読む前の起動先確認に失敗した。保存状態の stop-mark.md・inflight を含め、人が設定・中断記録・残った子を確認するまで印を外して再開しない"
mapfile -t EARLY_MARKERS < <(printf '%s' "$EARLY_STARTUP" | "$PY_ABS" -c '
import json,sys
s=json.load(sys.stdin)
print(s["locked"])
print(s["markers"]["stop_mark"])
print(s["markers"]["inflight"])
')
[ "${#EARLY_MARKERS[@]}" -eq 3 ] || die 20 startup-state "起動先の印を読めない"
[ "${EARLY_MARKERS[0]}" != True ] || die 20 locked "同じリポジトリの loop.sh が動いている(既存のロック)"
[ "${EARLY_MARKERS[1]}" = absent ] || die 20 stop-mark "stop-mark.md は未信頼。内容や利用者設定を読まず保存して停止する。人が設定・中断記録・残った子を確認するまで印を外して再開しない"
[ "${EARLY_MARKERS[2]}" = absent ] || die 20 inflight-untrusted "周の途中の印は未信頼。利用者設定を読まず signal・削除・unlock・ref 更新をせず保全した"
# 現在の配布元を起動時だけ信頼する。過去の STATE 内コードは実行しない。
ENV_TEMP="$(mktemp -d)" || die 20 environment "現在の実行のコピー先を作れない"
ENV_BOOT_ARGS=()
for f in ${MCP_CONFIGS[@]+"${MCP_CONFIGS[@]}"}; do ENV_BOOT_ARGS+=(--setting "$f"); done
# selftest 用の管理設定ルートも、通常の管理設定と同じ記述子として保持する。
if [ -n "${DEV_WORKFLOW_LOOP_TEST_MANAGED_DIR:-}" ]; then
  ENV_BOOT_ARGS+=(--managed-dir "$DEV_WORKFLOW_LOOP_TEST_MANAGED_DIR")
fi
ENV_RECEIPT="$(cd / && "$PY_ABS" -B "$ENV_GUARD_PY" bootstrap --root "$ORIGINAL_PLUGIN_ROOT" --output "$ENV_TEMP/start" "${ENV_BOOT_ARGS[@]}")" \
  || die 20 environment "配布物・利用者環境を安全に控えられない"
bind_environment "$ENV_RECEIPT"
export PYTHONDONTWRITEBYTECODE=1
verify_environment || die 20 environment "開始時の環境を照合できない"
# 最初の host 補助呼出しより前に、保持した設定 bytes だけを検査する。stop-mark
# の先行拒否後なので、未信頼の保存状態にある設定を読むことはない。
host_settings_call host-settings
HOST_SETTINGS_OUT="$HOST_SETTINGS_REPLY"
USER_SETTINGS="$(printf '%s' "$HOST_SETTINGS_OUT" | py json-get user_settings)" \
  || die 20 host-settings "保持した利用者設定のパスを読めない"
PLUGIN_JSON="$PLUGIN_ROOT/.claude-plugin/plugin.json"
PJ="$(py plugin-json "$PLUGIN_JSON")" || die 20 plugin-root "plugin.json を読めない"
PLUGIN_NAME="$(printf '%s\n' "$PJ" | sed -n 1p)"
PLUGIN_VERSION="$(printf '%s\n' "$PJ" | sed -n 2p)"
[ "$PLUGIN_NAME" = dev-workflow ] || die 20 plugin-root "プラグインの名前が dev-workflow でない"
for f in "$RESOLVER" "$PERM_SCRIPT" "$ORIGIN_REPO_PY" "$GIT_CONFIG_DIGEST_PY" "$LOOP_STATE_PY" "$LOOP_STARTUP_PY" "$HOST_ARGV_PY" "$SUPERVISOR" "$HOST_CHECK_PY"; do
  [ -f "$f" ] || die 20 plugin-root "${f##*/} が無い: $f"
done

component_check
INITIAL_POLICY_SHA="$POLICY_SHA"
INITIAL_FIXED_POLICY="$FIXED_POLICY"

verify_environment || die 20 environment "監督機構の診断直前に環境が変わった"
"$PY_ABS" "$SUPERVISOR" --check || die 20 supervisor-runtime "子の監督に必要な Linux subreaper・pidfd を使えない"

# ── §2 の 5: 既定表・--host-argv(D20)・全許可のフラグ(#68 の決定 3)──
load_host_table "$HOST"
if [ "${#HOST_ARGV_OVERRIDE[@]}" -gt 0 ]; then
  HOST_ARGV=("${HOST_ARGV_OVERRIDE[@]}")
  HOST_ARGV_SOURCE="起動引数 --host-argv"
  check_host_argv
else
  HOST_ARGV=("${TEMPLATE[@]}")
  HOST_ARGV_SOURCE="既定表"
  check_host_argv
fi
build_child_argv
scan_full_permission
RESOLVED_ARGV="$(quote_argv "${CHILD_ARGV[@]}")"
HOST_BIN="${HOST_ARGV[0]}"

# ── §2 の 6: Git より前に現在の管理入口と中断印だけを観察する ──
REPO="${REPO:-$PWD}"
[ -d "$REPO" ] || die 20 not-git "--repo がディレクトリでない: $REPO"
REPO_PHYS="$(cd -P -- "$REPO" && pwd -P)"
STATE_BASE="${XDG_STATE_HOME:-${HOME:?HOME が無い}/.local/state}/dev-workflow/loop"
STATE_GIT_UNSAFE=1
verify_environment || die 20 environment "起動先確認 helper の直前に環境が変わった"
STARTUP_JSON="$("$PY_ABS" "$LOOP_STARTUP_PY" --repo "$REPO" --state-base "$STATE_BASE")" \
  || die 20 startup-state "Git を使う前の起動先確認に失敗した。保存状態の stop-mark.md・inflight を含め、人が設定・中断記録・残った子を確認するまで印を外して再開しない"
mapfile -d '' -t STARTUP_FIELDS < <(printf '%s' "$STARTUP_JSON" | "$PY_ABS" -c '
import json,sys
s=json.load(sys.stdin)
for k in ("top","repo_admin","common","state_id","state_path","bound","management_sha256"):
 sys.stdout.buffer.write(str(s[k]).encode()+b"\0")
for k in ("inflight","stop_mark"):
 sys.stdout.buffer.write(s["markers"][k].encode()+b"\0")
sys.stdout.buffer.write(str(s["locked"]).encode()+b"\0")
')
[ "${#STARTUP_FIELDS[@]}" -eq 10 ] || die 20 startup-state "起動先確認の出力を読めない"
TOP="${STARTUP_FIELDS[0]}"; REPO_GIT_DIR="${STARTUP_FIELDS[1]}"; COMMON="${STARTUP_FIELDS[2]}"
STATE_ID="${STARTUP_FIELDS[3]}"; STATE_PLAN="${STARTUP_FIELDS[4]}"
STARTUP_MARKED=0
if [ "${STARTUP_FIELDS[7]}" != absent ] || [ "${STARTUP_FIELDS[8]}" != absent ]; then STARTUP_MARKED=1; fi
STATE_READY=0
prepare_startup_state() {
  chmod 700 "$STATE"
  exec 7>>"$STATE/lock"
  flock -n 7 || die 20 locked "同じリポジトリの loop.sh が動いている(ロック: $STATE/lock)"

  RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
  RUN_DIR="$STATE/$RUN_ID"
  mkdir "$RUN_DIR"
  REPORT="$RUN_DIR/report.md"
  {
    printf '# 無人ループの報告(%s)\n\n' "$RUN_ID"
    printf -- '- 対象: %s\n' "$TOP"
    printf -- '- 開始: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf -- '- 状態ディレクトリ: %s\n' "$STATE"
    [ "$DRY_RUN" -eq 0 ] || printf -- '- --dry-run(セッションを起動しない)\n'
  } >"$REPORT"
  STOP_MARK="$STATE/stop-mark.md"
  INFLIGHT="$STATE/inflight"
  LAST_VERIFIED="$STATE/last-verified.json"
  STATE_READY=1
}
if [ "${STARTUP_FIELDS[5]}" = True ]; then
  # 初回に配置検査を通した同一 state だけを使う。前の実行の終了との競合を
  # 避けるため、ロック取得後に印を再観察してから通常 Git の可否を決める。
  STATE="$STATE_PLAN"
  [ -d "$STATE" ] || die 20 startup-state "保持した状態の置き場が無い"
  under "$STATE" "$TOP" && die 20 state-dir "状態ディレクトリが人のチェックアウトの中にある"
  prepare_startup_state
  verify_environment || die 20 environment "中断印の再観察前に環境が変わった"
  STARTUP_LATEST="$("$PY_ABS" "$LOOP_STARTUP_PY" --repo "$REPO" --state-base "$STATE_BASE")" \
    || die 20 startup-state "ロック取得後に起動先の対応を確認できない。保存状態の stop-mark.md・inflight を含め、人が設定・中断記録・残った子を確認するまで印を外して再開しない"
  mapfile -d '' -t STARTUP_MARKERS < <(printf '%s' "$STARTUP_LATEST" | "$PY_ABS" -c '
import json,sys
s=json.load(sys.stdin)
for k in ("inflight","stop_mark"):
 sys.stdout.buffer.write(s["markers"][k].encode()+b"\0")
')
  [ "${#STARTUP_MARKERS[@]}" -eq 2 ] || die 20 startup-state "中断印の再観察を読めない"
  STARTUP_FIELDS[7]="${STARTUP_MARKERS[0]}"; STARTUP_FIELDS[8]="${STARTUP_MARKERS[1]}"
  if [ "${STARTUP_FIELDS[7]}" != absent ] || [ "${STARTUP_FIELDS[8]}" != absent ]; then STARTUP_MARKED=1; fi
fi
if [ "$STARTUP_MARKED" -eq 1 ]; then
  [ "${STARTUP_FIELDS[9]}" != True ] || die 20 locked "同じリポジトリの loop.sh が動いている(既存のロック)"
  [ "${STARTUP_FIELDS[8]}" = absent ] || die 20 stop-mark "stop-mark.md は未信頼。内容と成果物を保全した。人が設定・中断記録・残った子を確認するまで印を外して再開しない"
  die 20 inflight-untrusted "周の途中の印は未信頼。signal・削除・unlock・ref 更新をせず保全した"
fi
# 中断記録が無い起動だけが通常 Git へ進む。
G -C "$REPO_PHYS" rev-parse --git-dir >/dev/null 2>&1 || die 20 not-git "git リポジトリでない: $REPO_PHYS"
[ "$(G -C "$REPO_PHYS" rev-parse --is-bare-repository 2>/dev/null || true)" = false ] || die 20 bare "bare リポジトリには使えない: $REPO_PHYS"
TOP_RAW="$(G -C "$REPO_PHYS" rev-parse --show-toplevel 2>/dev/null)" || die 20 not-git "作業ツリーを持たない: $REPO_PHYS"
TOP="$(cd -P -- "$TOP_RAW" && pwd -P)"
[ "$TOP" = "$REPO_PHYS" ] || die 20 not-toplevel "--repo が作業ツリーのトップでない(トップは $TOP)"
COMMON_RAW="$(G -C "$TOP" rev-parse --path-format=absolute --git-common-dir)"
COMMON="$(cd -P -- "$COMMON_RAW" && pwd -P)"
# --repo が指す人の worktree の管理パスを固定する。拡張が無効でも設定の不在/追加を控える(H20)。
REPO_GIT_DIR_RAW="$(G -C "$TOP" rev-parse --path-format=absolute --git-dir)"
REPO_GIT_DIR="$(cd -P -- "$REPO_GIT_DIR_RAW" && pwd -P)"
# main の worktree(--repo が linked worktree のとき、人のチェックアウトはこちらにもある)
MAIN_WT=""
while IFS= read -r -d '' line; do
  case "$line" in "worktree "*) MAIN_WT="${line#worktree }"; break ;; esac
done < <(G -C "$TOP" worktree list --porcelain -z)
[ -n "$MAIN_WT" ] || die 20 not-git "main の worktree を解決できない: $TOP"
MAIN_WT="$(realpath -m -- "$MAIN_WT")"


[ "$TOP" = "${STARTUP_FIELDS[0]}" ] && [ "$REPO_GIT_DIR" = "${STARTUP_FIELDS[1]}" ] && [ "$COMMON" = "${STARTUP_FIELDS[2]}" ] \
  || die 20 startup-state "Git の解決結果が起動前の管理入口と一致しない"
# ── §2 の 7: 状態の配置検査を通してから起動先の対応を排他的に固定する ──
if inside_checkout "$STATE_PLAN"; then die 20 state-dir "状態ディレクトリが人のチェックアウトの中にある($STATE_PLAN)。XDG_STATE_HOME を外へ向ける"; fi
if [ "$STATE_READY" -eq 0 ]; then
  mkdir -p "$STATE_PLAN" || die 20 state-dir "状態ディレクトリを作れない: $STATE_PLAN"
  STATE="$(cd -P -- "$STATE_PLAN" && pwd -P)"
  if inside_checkout "$STATE"; then die 20 state-dir "状態ディレクトリが人のチェックアウトの中にある($STATE)。XDG_STATE_HOME を外へ向ける"; fi
  prepare_startup_state
fi
verify_environment || die 20 environment "起動先の対応を固定する前に環境が変わった"
"$PY_ABS" "$LOOP_STARTUP_PY" --repo "$REPO" --state-base "$STATE_BASE" --bind \
  --expect-top "$TOP" --expect-repo-admin "$REPO_GIT_DIR" --expect-common "$COMMON" \
  --expect-management-sha256 "${STARTUP_FIELDS[6]}" >/dev/null \
  || die 20 startup-state "検証した起動先の対応を保存できない"
STATE_GIT_UNSAFE=0
STOP_FILE="${STOP_FILE:-$TOP/.claude/loop.stop}"
plan_stop_control
# ロック取得中に現れた印も無視しない。
if [ "$STARTUP_MARKED" -eq 0 ]; then handle_marks_at_start; fi

# ── §2 の 8: デフォルトブランチ(base-commit.md と同じ順)とその sha の固定(D8)──
if DEF_SYM="$(G -C "$TOP" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null)"; then
  DEF_NAME="${DEF_SYM#refs/remotes/origin/}"
elif G -C "$TOP" show-ref --verify --quiet refs/heads/main; then
  DEF_NAME=main
elif G -C "$TOP" show-ref --verify --quiet refs/heads/master; then
  DEF_NAME=master
else
  die 20 no-default-branch "デフォルトブランチを解決できない(refs/remotes/origin/HEAD・main・master が無い)"
fi
G -C "$TOP" show-ref --verify --quiet "refs/heads/$DEF_NAME" \
  || die 20 no-default-branch "ローカルのデフォルトブランチ refs/heads/$DEF_NAME が無い"
DEF_SHA="$(G -C "$TOP" rev-parse --verify "refs/heads/$DEF_NAME^{commit}")"

# ── §2 の 9: profile(D1・D2・D15・D21)──
# 固定した sha の commit から読む(周の worktree と同じ中身。人の作業ツリーの未 commit の変更に左右されない)
PROFILE_PATH=.claude/project-profile.yml
P_MAX_ITER=""; P_MAX_FAIL=""; P_BUDGET=""; P_ITER_TIMEOUT=""; P_UNKNOWN=""
TASK_DIR_SET=0; TASK_DIR_VALUE=""; PROFILE_STATE="無し(全項目を既定値で解決)"
LS_TREE="$(G -C "$TOP" ls-tree "$DEF_SHA" -- "$PROFILE_PATH")"
if [ -n "$LS_TREE" ]; then
  P_MODE="${LS_TREE%% *}"
  P_REST="${LS_TREE#* }"
  P_TYPE="${P_REST%% *}"
  P_OID="${P_REST#* }"
  P_OID="${P_OID%%"$TAB"*}"
  { [ "$P_MODE" = 100644 ] && [ "$P_TYPE" = blob ]; } \
    || die 20 profile-mode "$PROFILE_PATH がモード 100644 の blob でない($P_MODE $P_TYPE)"
  G -C "$TOP" cat-file blob "$P_OID" >"$RUN_DIR/profile.yml"
  rc=0
  PROFILE_OUT="$(py profile "$RUN_DIR/profile.yml" 2>"$RUN_DIR/profile.err")" || rc=$?
  case "$rc" in
    0) : ;;
    3) die 20 no-pyyaml "profile があるのに PyYAML が無い(python3 -m pip install pyyaml などで入れる)" ;;
    4) die 20 parent-child "parent-child 構成では起動しない($(cat "$RUN_DIR/profile.err"))" ;;
    5) die 20 task-dir "$(cat "$RUN_DIR/profile.err")" ;;
    *) die 20 profile-invalid "profile の値が不正: $(cat "$RUN_DIR/profile.err")" ;;
  esac
  while IFS= read -r line; do
    case "$line" in
      max_iterations=*) P_MAX_ITER="${line#*=}" ;;
      max_consecutive_failures=*) P_MAX_FAIL="${line#*=}" ;;
      time_budget=*) P_BUDGET="${line#*=}" ;;
      iteration_timeout=*) P_ITER_TIMEOUT="${line#*=}" ;;
      unknown=*) P_UNKNOWN="${line#*=}" ;;
      task_dir=*) TASK_DIR_SET=1; TASK_DIR_VALUE="${line#*=}" ;;
    esac
  done <<<"$PROFILE_OUT"
  PROFILE_STATE="$DEF_NAME の commit $DEF_SHA から読んだ"
fi

# ── §2 の 10: 実効値(#68 の決定 4: min(既定値か起動引数の値, profile の値)。profile は締める向きだけ)──
effective() { # $1=既定値 $2=起動引数 $3=profile → 「実効値 出所」
  local base="$1" src="既定"
  if [ -n "$2" ]; then base="$2"; src="起動引数"; fi
  if [ -n "$3" ] && [ "$3" -lt "$base" ]; then printf '%s profile' "$3"; else printf '%s %s' "$base" "$src"; fi
}
read -r MAX_ITER MAX_ITER_SRC <<<"$(effective "$DEF_MAX_ITER" "$ARG_MAX_ITER" "$P_MAX_ITER")"
read -r MAX_FAIL MAX_FAIL_SRC <<<"$(effective "$DEF_MAX_FAIL" "$ARG_MAX_FAIL" "$P_MAX_FAIL")"
read -r BUDGET BUDGET_SRC <<<"$(effective "$DEF_BUDGET" "$ARG_BUDGET" "$P_BUDGET")"
read -r ITER_TIMEOUT ITER_TIMEOUT_SRC <<<"$(effective "$DEF_ITER_TIMEOUT" "$ARG_ITER_TIMEOUT" "$P_ITER_TIMEOUT")"
[ "$BUDGET" -ge "$ITER_TIMEOUT" ] \
  || die 20 budget "実効値で time_budget($BUDGET)< iteration_timeout($ITER_TIMEOUT)。1 周も回らない"

WT_ROOT="${WT_ROOT:-$(dirname -- "$TOP")/$(basename -- "$TOP").loop}"
# `..` と既存の親の symlink を物理パスへ正規化してから、人のチェックアウト(物理パス)と比べる
WT_ROOT="$(realpath -m -- "$WT_ROOT")"
if inside_checkout "$WT_ROOT"; then die 20 worktree-root "worktree の置き場が人のチェックアウトの中にある($WT_ROOT)"; fi

# ── origin の URL の検査(両方のモード。loop.md §2。起動時の最初の ls-remote〈§2 の 11〉より前 — vcs のヘルパーが失敗する
# 構成で、ls-remote の失敗ではなく origin-vcs の案内に届くように)。どの理由にも URL の字面を出さない ──
rc=0
verify_environment || die 20 environment "origin の検査直前に環境が変わった"
ORIGIN_OUT="$(cd / && exec python3 -B "$ORIGIN_REPO_PY" --dir="$TOP" 7>&- 2>"$RUN_DIR/origin-repo.err")" || rc=$?
[ "$rc" -eq 0 ] || die 20 origin-url "origin の URL を読めない(origin-repo.py の終了コード $rc。stderr は $RUN_DIR/origin-repo.err)"
ORIGIN_INFO="$(printf '%s' "$ORIGIN_OUT" | py origin-json)" || die 20 origin-url "origin の URL を読めない(origin-repo.py の出力を解析できない)"
O_ORIGIN="$(printf '%s\n' "$ORIGIN_INFO" | sed -n 's/^origin=//p')"
O_SAME="$(printf '%s\n' "$ORIGIN_INFO" | sed -n 's/^same=//p')"
O_VCS="$(printf '%s\n' "$ORIGIN_INFO" | sed -n 's/^vcs=//p')"
O_REASON="$(printf '%s\n' "$ORIGIN_INFO" | sed -n 's/^reason=//p')"
O_REPO="$(printf '%s\n' "$ORIGIN_INFO" | sed -n 's/^repo=//p')"
if [ "$O_ORIGIN" = 1 ] && [ "$O_VCS" = 1 ]; then
  die 20 origin-vcs "origin に remote.origin.vcs がある(push・ls-remote が git-remote-<vcs> のヘルパーを通り、URL の字面と送り先が離れるので、無人ループでは使えない)。\`git config --unset remote.origin.vcs\` で外してから起動する"
fi
if [ "$O_ORIGIN" = 1 ] && [ "$O_SAME" != 1 ]; then
  die 20 origin-url-mismatch "origin の fetch と push の URL が同じリポジトリを指さない(${O_REASON:-理由なし})。fetch と push の URL をそれぞれ 1 つにし、同じリポジトリに揃えてから起動する(remote.origin.pushurl・pushInsteadOf を見直す)"
fi
if [ "$O_ORIGIN" = 1 ]; then
  ORIGIN_URL_STATE="検査に通った(fetch と push が同じリポジトリ)"
elif [ "$DISCOVER" -eq 1 ]; then
  ORIGIN_URL_STATE="origin が無い(push しないので、候補があれば結末は 縮退)"
else
  ORIGIN_URL_STATE="origin が無い(周は push しないので、結末は 縮退)"
fi

# ── §2 の 12: ホスト CLI の実在と雛形のフラグの 2 段照合(argv と --help)・導入済みの同名プラグインの版(D11)──
command -v "$HOST_BIN" >/dev/null 2>&1 || die 20 host-cli-missing "ホスト CLI '$HOST_BIN' が PATH に無い"
# 実行ファイルを起動時に絶対パスへ解決し、補助の CLI と周の子の両方に使う(PATH に `.` などの相対の要素が
# あると、周の cwd〈worktree〉の中の同名のファイルが起動されうる)
HOST_BIN_ABS="$(command -v "$HOST_BIN")"
case "$HOST_BIN_ABS" in
  /*) : ;;
  *) die 20 host-cli-missing "ホスト CLI '$HOST_BIN' を絶対パスに解決できない('$HOST_BIN_ABS')。PATH の相対の要素を外すか、--host-argv に絶対パスを渡す" ;;
esac
# H31: ホスト CLI を一度も起動しないうちに実体(realpath・dev・ino・内容の sha256)を控え、以後の起動はすべて
# その実体のパス(HOST_EXEC)で行う。証明(人が --prove-host で実 hook の拒否を確かめた記録)が無ければ起動しない
verify_environment || die 20 environment "ホスト CLI の実体を控える直前に環境が変わった"
rc=0
HOST_IDENTITY="$("$PY_ABS" -I -B "$HOST_CHECK_PY" identity --host "$HOST_BIN_ABS" 2>"$RUN_DIR/host-identity.err")" || rc=$?
[ "$rc" -eq 0 ] || die 20 host-proof "ホスト CLI の実体を控えられない($(head -c 300 "$RUN_DIR/host-identity.err" | tr '\n' ' '))"
HOST_EXEC="$(printf '%s' "$HOST_IDENTITY" | py json-get realpath)" || die 20 host-proof "ホスト CLI の実体の値を読めない"
HOST_SHA="$(printf '%s' "$HOST_IDENTITY" | py json-get sha256)" || die 20 host-proof "ホスト CLI の実体の値を読めない"
HOST_KIND="$(printf '%s' "$HOST_IDENTITY" | py json-get kind)" || die 20 host-proof "ホスト CLI の実体の値を読めない"
# stat の値は、内容の sha256 まで照らす照合の前に控える(控えた後に変わっていれば、続く照合で止まる)
HOST_STAT="$(host_stat)" || die 20 host-proof "ホスト CLI の実体の stat を控えられない"
host_same --hash || die 20 host-proof "ホスト CLI の実体を控える間に変わった"
case "${XDG_CONFIG_HOME:-}" in /*) PROOF_STORE="$XDG_CONFIG_HOME" ;; *) PROOF_STORE="$HOME/.config" ;; esac
PROOF_STORE="$(realpath -m -- "$PROOF_STORE/dev-workflow/loop/host-proofs")"
CHILD_ARGV[0]="$HOST_EXEC"
RESOLVED_ARGV="$(quote_argv "${CHILD_ARGV[@]}")"
build_child_shape
PROVE_HINT="ホスト CLI の更新が正規のもの(版・導入元・置き場)かを人が確かめてから、同じ引数(--repo・--host-argv・--allowed-tools・--mcp-config)に --prove-host を足して 1 回打ち、許可の仲介の hook が実際に効くことを確かめてから起動する(--prove-host は打った時点の実体を信頼する)"
if [ "$PROVE_HOST" -eq 0 ]; then
  verify_environment || die 20 environment "ホスト CLI の証明を読む直前に環境が変わった"
  rc=0
  "$PY_ABS" -I -B "$HOST_CHECK_PY" proof-get --store "$PROOF_STORE" --identity "$HOST_IDENTITY" --shape "$HOST_SHAPE" \
    >/dev/null 2>"$RUN_DIR/host-proof.err" || rc=$?
  case "$rc" in
    0) : ;;
    3) die 20 host-proof "このホスト CLI の実体($HOST_EXEC・sha256 ${HOST_SHA:0:16}…)と起動の形の証明が無い(ホスト CLI を更新したときも同じ)。ホスト CLI を起動せずに止まる。$PROVE_HINT" ;;
    *) die 20 host-proof "$(head -c 400 "$RUN_DIR/host-proof.err" | tr '\n' ' ')。ホスト CLI を起動せずに止まる。$PROVE_HINT" ;;
  esac
fi
# 周の子の起動に使う env・setsid も、周の cwd に左右されないよう絶対パスにしておく
ENV_BIN="$(command -v env)"
SETSID_BIN="$(command -v setsid)"
for t in "$ENV_BIN" "$SETSID_BIN"; do
  case "$t" in /*) : ;; *) die 20 tool-missing "env・setsid を絶対パスに解決できない('$ENV_BIN' '$SETSID_BIN')" ;; esac
done
rc=0
aux "$RUN_DIR/version.txt" "${AUX_VERSION[@]}" || rc=$?
[ "$rc" -eq 0 ] || die 20 host-proof "'$HOST_BIN ${AUX_VERSION[*]}' が失敗した(終了コード $rc)"
rc=0
aux "$RUN_DIR/help.txt" "${AUX_HELP[@]}" || rc=$?
[ "$rc" -eq 0 ] || die 20 help-mismatch "'$HOST_BIN ${AUX_HELP[*]}' が失敗した(終了コード $rc)"
HELP_CHECK=("${ISOLATION[@]}" "$PLUGIN_DIR_FLAG" "$PERM_MODE_FLAG" "${PERM_PROMPTS[0]}" "$SETTINGS_FLAG")
[ "$PROVE_HOST" -eq 0 ] || HELP_CHECK+=(--verbose)
if [ "$ALLOW_CLASSIFIER" -eq 1 ]; then HELP_CHECK+=("$CLASSIFIER_VALUE"); else HELP_CHECK+=("${PERM_EDITS[1]}"); fi
[ "${#MCP_CONFIGS[@]}" -eq 0 ] || HELP_CHECK+=("$MCP_FLAG")
[ "${#ALLOWED_TOOLS[@]}" -eq 0 ] || HELP_CHECK+=("$ALLOWED_FLAG")
# 値のうち、一般語(user・none)は照合しない
HELP_TOKENS=()
for t in "${HELP_CHECK[@]}"; do
  case "$t" in user|none) : ;; *) HELP_TOKENS+=("$t") ;; esac
done
rc=0
MISSING="$(py help-check "$RUN_DIR/help.txt" "${HELP_TOKENS[@]}")" || rc=$?
[ "$rc" -eq 0 ] || die 20 help-mismatch "'$HOST_BIN --help' に雛形のフラグが無い: $(printf '%s' "$MISSING" | tr '\n' ' ')(仕様変更の可能性。既定表を見直す)"
# D20: 値を取るフラグの表(--help の `<…>` の値の表記から作る)で --host-argv を照合する
py help-values "$RUN_DIR/help.txt" >"$RUN_DIR/help-values.txt"
check_host_argv_values "$RUN_DIR/help-values.txt"
PROOF_INFO="未確認(--prove-host で確かめる)"
rc=0
aux "$RUN_DIR/plugins.json" "${AUX_PLUGINS[@]}" || rc=$?
[ "$rc" -eq 0 ] || die 20 plugin-list "'$HOST_BIN ${AUX_PLUGINS[*]}' が失敗した(終了コード $rc)"
rc=0
PLUGINS_OUT="$(py plugins "$RUN_DIR/plugins.json" "$PLUGIN_VERSION")" || rc=$?
[ "$rc" -eq 0 ] || die 20 plugin-list "導入済みのプラグインの一覧を解析できない($RUN_DIR/plugins.json)"
case "$PLUGINS_OUT" in
  none) INSTALLED_STATE="有効な dev-workflow は導入されていない" ;;
  "same "*) INSTALLED_STATE="同じ版 ${PLUGINS_OUT#same } が導入済み(一覧と実体を監視する)" ;;
  "mismatch "*) die 20 plugin-version "導入済みの dev-workflow の版(${PLUGINS_OUT#mismatch })が loop.sh のプラグインの版($PLUGIN_VERSION)と違う。導入済みを更新するか、無効にしてから起動する" ;;
  *) die 20 plugin-list "導入済みのプラグインの一覧を解析できない" ;;
esac

verify_environment || die 20 environment "有効 plugin を控える前に開始時の環境が変わった"
ENV_RECEIPT="$(environment_call bootstrap --root "$ORIGINAL_PLUGIN_ROOT" --output "$ENV_TEMP/active" --inventory "$RUN_DIR/plugins.json" "${ENV_BOOT_ARGS[@]}")" \
  || die 20 environment "有効 plugin の実体を控えられない"
verify_environment || die 20 environment "有効 plugin の控えの作成中に開始時の環境が変わった"
bind_environment "$ENV_RECEIPT"
ENV_INVENTORY_READY=1
verify_environment || die 20 environment "有効 plugin の一覧または実体が変わった"
host_settings_call host-settings
ACTIVE_HOST_SETTINGS_OUT="$HOST_SETTINGS_REPLY"
ACTIVE_USER_SETTINGS="$(printf '%s' "$ACTIVE_HOST_SETTINGS_OUT" | py json-get user_settings)" \
  || die 20 host-settings "active bootstrap 後の利用者設定のパスを読めない"
[ "$ACTIVE_USER_SETTINGS" = "$USER_SETTINGS" ] || die 20 host-config-path "active bootstrap 後に利用者設定のパスが変わった"
component_check
[ "$FIXED_POLICY" = "$INITIAL_FIXED_POLICY" ] || die 20 component-policy "有効pluginの確定後に固定設定が変わった"
build_child_argv
CHILD_ARGV[0]="$HOST_EXEC"
RESOLVED_ARGV="$(quote_argv "${CHILD_ARGV[@]}")"
SHAPE_BEFORE="$HOST_SHAPE"
build_child_shape
[ "$HOST_SHAPE" = "$SHAPE_BEFORE" ] || die 20 host-proof "起動の形が証明の照合の後に変わった"

# ── 許可の仲介(D22 ③)の許可リスト ──
# 設定の hook 可否は最初の aux より前の host-settings が検査済み。ここは同じ
# 保持 reader が返した permissions.allow と引数だけを JSON へ直す。
ALLOW_ARGS=()
for v in ${ALLOWED_TOOLS[@]+"${ALLOWED_TOOLS[@]}"}; do ALLOW_ARGS+=(--allowed-tools "$v"); done
host_settings_call host-allowlist "${ALLOW_ARGS[@]}"
ALLOW_OUT="$HOST_SETTINGS_REPLY"
ALLOW_JSON="$(printf '%s\n' "$ALLOW_OUT" | sed -n 1p)"
ALLOW_UNUSED="$(printf '%s\n' "$ALLOW_OUT" | sed -n 's/^unused=//p')"

# active component の全出所が確定した時点で、証明の必須digestを照合する。
[ "$PROVE_HOST" -eq 1 ] || match_component_proof

# ── §2 の 13: 疎通(D10。--dry-run では打たない)──
if [ "$DRY_RUN" -eq 0 ]; then
  rc=0
  aux "$RUN_DIR/auth.txt" "${AUX_AUTH[@]}" || rc=$?
  [ "$rc" -eq 0 ] || die 20 auth "認証の確認('$HOST_BIN ${AUX_AUTH[*]}')が通らない(終了コード $rc。$RUN_DIR/auth.txt)"
fi
[ "$PROVE_HOST" -eq 0 ] || prove_host

# ── §2 の 11: 2026-09-23 決定 19 の帰結(D8)──
LEADING=""
ORIGIN_STATE=""
HAS_ORIGIN=0
if [ -n "$(G -C "$TOP" config --get remote.origin.url 2>/dev/null || true)" ]; then HAS_ORIGIN=1; fi
if [ "$HAS_ORIGIN" -eq 1 ]; then
  rc=0
  LS_OUT="$(net_git "$NET_TIMEOUT" -C "$TOP" ls-remote origin "refs/heads/$DEF_NAME" 2>"$RUN_DIR/ls-remote.err")" || rc=$?
  [ "$rc" -eq 0 ] || die 20 ls-remote "origin の $DEF_NAME を読めない(git ls-remote の終了コード $rc。${NET_TIMEOUT} 秒で打ち切る。$(head -c 300 "$RUN_DIR/ls-remote.err" | tr '\n' ' '))"
  ORIGIN_SHA=""
  while IFS="$TAB" read -r sha ref; do
    if [ "$ref" = "refs/heads/$DEF_NAME" ]; then ORIGIN_SHA="$sha"; fi
  done <<<"$LS_OUT"
  [ -n "$ORIGIN_SHA" ] || die 20 origin-unknown "origin に refs/heads/$DEF_NAME が無い(ローカルとの前後を判定できない)"
  if [ "$ORIGIN_SHA" = "$DEF_SHA" ]; then
    ORIGIN_STATE="origin と同じ"
  else
    G -C "$TOP" cat-file -e "$ORIGIN_SHA^{commit}" 2>/dev/null \
      || die 20 origin-unknown "origin の $DEF_NAME($ORIGIN_SHA)がローカルに無い(遅れているか分岐している。fetch して確かめる)"
    if G -C "$TOP" merge-base --is-ancestor "$ORIGIN_SHA" "$DEF_SHA"; then
      ORIGIN_STATE="ローカルが origin より先行"
      LEADING="$(G -C "$TOP" log --no-show-signature --format='%h %s' "$ORIGIN_SHA..$DEF_SHA")"
    elif G -C "$TOP" merge-base --is-ancestor "$DEF_SHA" "$ORIGIN_SHA"; then
      die 20 behind "ローカルの $DEF_NAME が origin より遅れている(更新してから起動する)"
    else
      die 20 diverged "ローカルの $DEF_NAME と origin が分岐している"
    fi
  fi
else
  ORIGIN_STATE="origin が無い(検査しない)"
fi


# ── §2 の 14・15: 起動時の報告(DEF の sha は上で固定した。共有の状態は各周の起動の直前に控える)──
if [ "${#MCP_CONFIGS[@]}" -gt 0 ]; then
  MCP_STATE="渡す(${MCP_CONFIGS[*]})"
else
  MCP_STATE="渡さない(周では MCP が使えない。MCP を使う手順は「無い場合」の経路になる)"
fi
if [ "$DISCOVER" -eq 1 ]; then
  DISCOVER_LIST="$(IFS=,; printf '%s' "${DISCOVER_SOURCES[*]}")"
  rep "- モード: 発見(発見元の列: $DISCOVER_LIST・$DISCOVER_SRC_DESC。loop.md §11)"
  say "モード: 発見(発見元の列: $DISCOVER_LIST・$DISCOVER_SRC_DESC)"
fi
rep "- origin の URL: $ORIGIN_URL_STATE"
rep "- ホスト: $HOST(雛形の出所: $HOST_ARGV_SOURCE)"
rep "- 解決後の argv: $RESOLVED_ARGV" "- プロンプトは stdin で渡す"
rep "- 実効値: max_iterations=$MAX_ITER($MAX_ITER_SRC) max_consecutive_failures=$MAX_FAIL($MAX_FAIL_SRC) time_budget=$BUDGET($BUDGET_SRC) iteration_timeout=$ITER_TIMEOUT($ITER_TIMEOUT_SRC) kill_grace=$KILL_GRACE net_timeout=$NET_TIMEOUT"
rep "- profile: $PROFILE_STATE"
[ -z "$P_UNKNOWN" ] || rep "- features.loop の未知のキーは無視した: $P_UNKNOWN"
rep "- デフォルトブランチ: $DEF_NAME @ $DEF_SHA(周はこの sha から worktree を作る)" "- origin: $ORIGIN_STATE"
if [ -n "$LEADING" ]; then
  rep "- 先行する commit(無人の周の PR に混ざる):"
  while IFS= read -r l; do rep "  - $l"; done <<<"$LEADING"
fi
rep "- MCP: $MCP_STATE"
rep "- 許可の仲介(D22): hook $PERM_SCRIPT(python3: $PY_ABS)・許可リスト $ALLOW_JSON(利用者の設定 $USER_SETTINGS は保持した設定 reader で検査済み)"
if [ -n "$ALLOW_UNUSED" ]; then
  rep "- 許可リストの規則のうち使わない形(途中の * など):"
  while IFS= read -r l; do rep "  - $l"; done <<<"$ALLOW_UNUSED"
fi
rep "- 導入済みの同名プラグイン: $INSTALLED_STATE(loop.sh のプラグイン: $PLUGIN_ROOT・版 $PLUGIN_VERSION)"
rep "- ホスト CLI の実体: $HOST_EXEC(sha256 $HOST_SHA・$HOST_KIND)。証明: $(printf '%s' "$PROOF_INFO" | tr '\t' ' ')"
rep "- skill・command の allowed-tools: 検査したファイル(.md と plugin のマニフェスト) ${GRANTS_FILES} 件。許可リストより広い規則は無い(YAML の照合: $GRANTS_YAML)"
rep "- 停止ファイル: $STOP_FILE" "- worktree の置き場: $WT_ROOT"
for note in ${STARTUP_NOTES[@]+"${STARTUP_NOTES[@]}"}; do rep "- $note"; done
load_worktrees
for path in "${!LOCKED_REASON[@]}"; do
  reason="${LOCKED_REASON[$path]}"
  case "$reason" in
    "dev-workflow-loop: "*) rep "- 残った worktree(過去の実行の分を含む): $path($reason)— 結末: $(describe_left_wt "$path")" ;;
  esac
done
say "解決後の argv: $RESOLVED_ARGV"
if [ "$DRY_RUN" -eq 1 ]; then
  # 実走のプローブで使う、周の子の環境(D22。周の worktree とその周の値は仮の値)
  say "周の子の環境(D22。<…> は周ごとの値):" \
    "  DEV_WORKFLOW_LOOP_WORKTREE=<周の worktree の物理パス($WT_ROOT/<実行 ID>-<周の番号>)>" \
    "  DEV_WORKFLOW_LOOP_PERMLOG=<状態ディレクトリの周の記録($STATE/<実行 ID>/iter-<周の番号>.permlog)>" \
    "  DEV_WORKFLOW_LOOP_PLUGIN_ROOT=$PLUGIN_ROOT" \
    "  DEV_WORKFLOW_LOOP_ALLOW=$ALLOW_JSON" \
    "  CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"
  if [ -n "$ALLOW_UNUSED" ]; then say "許可リストの規則のうち使わない形:" "$ALLOW_UNUSED"; fi
fi
if [ -n "$LEADING" ]; then say "先行する commit(ローカルが origin より先行):" "$LEADING"; fi
for note in ${STARTUP_NOTES[@]+"${STARTUP_NOTES[@]}"}; do say "$note"; done

# ── §3〜§6: ループ ──
while :; do
  if [ "$DRY_RUN" -eq 0 ]; then check_stop_before_iteration; fi
  if [ "$DISCOVER" -eq 1 ]; then
    # 発見モード(loop.md §11): 発見元の列を引数の順に 1 つずつ回す
    select_discover
    if [ "$DRY_RUN" -eq 1 ]; then
      say "発見元の列(task_dir: $TASK_DIR):"
      for s in ${DISC_QUEUE[@]+"${DISC_QUEUE[@]}"}; do say "  $s(ブランチ task/候補-$s-${DEF_SHA:0:12})"; done
      if [ "${#SKIPS[@]}" -gt 0 ]; then
        say "読み飛ばし:"
        for line in "${SKIPS[@]}"; do
          say "  $line"
          s="${line%%: *}"
          while IFS= read -r l; do [ -z "$l" ] || say "    片付け: \`$l\`"; done <<<"${DISC_CLEAN[$s]:-}"
        done
      fi
      finish 0 "--dry-run(発見元の列と読み飛ばしを出して終わる)"
    fi
    if [ "${#DISC_QUEUE[@]}" -eq 0 ]; then finish 0 "キューが空"; fi
    run_iteration
    continue
  fi
  select_task
  if [ "$DRY_RUN" -eq 1 ]; then
    say "対象の一覧(task_dir: $TASK_DIR):"
    for line in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
      IFS="$TAB" read -r _ _ f _ <<<"$line"
      say "  $(join_rel "$TASK_DIR" "$f")"
    done
    if [ "${#SKIPS[@]}" -gt 0 ]; then
      say "読み飛ばし:"
      for line in "${SKIPS[@]}"; do say "  $line"; done
    fi
    finish 0 "--dry-run(対象の一覧を出して終わる)"
  fi
  if [ "${#CANDIDATES[@]}" -eq 0 ]; then finish 0 "キューが空"; fi
  run_iteration
done
