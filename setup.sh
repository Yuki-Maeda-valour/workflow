#!/usr/bin/env bash
# workflow skills と共通実行ファイルを対象プロジェクト(または ~/.claude)へ導入する。
# 推奨は plugin marketplace 方式(README 参照)。これはプラグインを使わない場合の代替。
set -euo pipefail

REPO_DIR="$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILLS_SRC="$REPO_DIR/plugins/dev-workflow/skills"
BIN_SRC="$REPO_DIR/plugins/dev-workflow/bin/dev-workflow"

usage() {
  cat <<'EOF'
使い方:
  ./setup.sh --link <プロジェクトパス>   skill ごとに symlink を張る(repo 更新が即反映)
  ./setup.sh --copy <プロジェクトパス>   skill をコピーする(独自改変する場合)
  ./setup.sh --global                    ~/.claude/skills に symlink(全プロジェクト共通)
  ./setup.sh --list                      skills と共通実行ファイルの一覧を表示

  他ホスト(Codex / Cursor)向け — .agents/skills へ配置する:
  ./setup.sh --agents <プロジェクトパス>        <パス>/.agents/skills に symlink
  ./setup.sh --agents-copy <プロジェクトパス>   <パス>/.agents/skills にコピー
  ./setup.sh --agents-global                    ~/.agents/skills に symlink

オプション:
  --force    skills と bin/dev-workflow をそろえて上書き

注意:
  - 導入モードと --list は、1 回にいずれか 1 つだけ指定する(同じ指定の反復も不可)
  - --link / --copy / --agents / --agents-copy の直後には空でないパスが必要
  - - で始まるパスは ./-project のように ./ を付けるか、絶対パスで指定する
  - <プロジェクトパス> は事前に存在している必要がある(存在しないとエラー終了する)
  - 共通実行ファイルは skills の兄弟の bin/dev-workflow へ、同じ方式で置く
  - --force なしでは、既存の内容・権限が同じか、同じ配布元へのリンクなら残す
    異なる版やリンク切れがあれば、何も変えず終了 1。--force で入れ直せる
  - 配布元に bin/dev-workflow が無いときは、その旨を出して skills だけを置く
  - skill を 1 本だけ取り出す配置は非サポート。skill 間の兄弟参照
    (../do-task/... など)が解決できず、外部ランナー等の機能が無効化される

推奨導入(plugin marketplace 方式)は Claude Code 内で:
  /plugin marketplace add <このリポジトリのパス or GitHub repo>
  /plugin install dev-workflow@valour-workflow
EOF
}

usage_error() {
  echo "$1" >&2
  usage
  exit 2
}

FORCE=0
MODE=""
TARGET=""

select_mode() {
  if [[ -n "$MODE" ]]; then
    usage_error "ERROR: モードは 1 つだけ指定してください: --$MODE と $1"
  fi
  MODE="${1#--}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --link|--copy|--agents|--agents-copy)
      if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == -* ]]; then
        usage_error "ERROR: $1 の直後にプロジェクトパスが必要です"
      fi
      select_mode "$1"
      TARGET="$2"
      shift 2
      ;;
    --global|--agents-global|--list) select_mode "$1"; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage_error "不明な引数: $1" ;;
  esac
done

[[ -d "$SKILLS_SRC" ]] || { echo "ERROR: $SKILLS_SRC が見つかりません" >&2; exit 1; }

if [[ "$MODE" == "list" ]]; then
  echo "含まれる skills:"
  for d in "$SKILLS_SRC"/*/; do
    name="$(basename "$d")"
    desc="$(grep -m1 '^description:' "$d/SKILL.md" 2>/dev/null | cut -c 14-90 | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null || true)"
    printf '  %-20s %s\n' "$name" "$desc..."
  done
  if [[ -d "$BIN_SRC" ]]; then
    echo "共通実行ファイル: bin/dev-workflow/ (launch.sh と配布物一式)"
  else
    echo "NOTE: 配布元に bin/dev-workflow/ がありません。導入時は skills だけを置きます。"
  fi
  exit 0
fi

case "$MODE" in
  link|copy)
    [[ -n "$TARGET" ]] || usage_error "ERROR: プロジェクトパスを指定してください"
    [[ -d "$TARGET" ]] || { echo "ERROR: $TARGET が存在しません" >&2; exit 1; }
    DEST="$TARGET/.claude/skills"
    ;;
  agents|agents-copy)
    # Codex(.agents/skills)/ Cursor(.agents/skills or .cursor/skills)向けの配置
    [[ -n "$TARGET" ]] || usage_error "ERROR: プロジェクトパスを指定してください"
    [[ -d "$TARGET" ]] || { echo "ERROR: $TARGET が存在しません" >&2; exit 1; }
    DEST="$TARGET/.agents/skills"
    if [[ "$MODE" == "agents-copy" ]]; then MODE="copy"; else MODE="link"; fi
    ;;
  global)
    DEST="$HOME/.claude/skills"
    MODE="link"
    ;;
  agents-global)
    DEST="$HOME/.agents/skills"
    MODE="link"
    ;;
  *) usage; exit 2 ;;
esac

# Bash 3.2 の組込みだけで既存の親を物理パスにする。
# 未作成の末尾は残すが、リンク切れや通常ファイルを親として扱わない。
physical_directory() {
  local probe="$1" suffix="" leaf
  [[ "$probe" == /* ]] || probe="$PWD/$probe"
  while [[ ! -d "$probe" ]]; do
    if [[ -e "$probe" || -L "$probe" ]]; then
      echo "ERROR: 導入パスを解決できません: $1" >&2
      return 1
    fi
    leaf="${probe##*/}"
    suffix="/$leaf$suffix"
    probe="${probe%/*}"
    [[ -n "$probe" ]] || probe="/"
  done
  # 最後の / を目印にし、改行で終わるディレクトリ名も保持する。
  if ! PHYSICAL_DIR="$(CDPATH= cd -P -- "$probe" && printf '%s/' "$PWD")"; then
    echo "ERROR: 導入パスを解決できません: $1" >&2
    return 1
  fi
  PHYSICAL_DIR="${PHYSICAL_DIR%/}"
  PHYSICAL_DIR="${PHYSICAL_DIR%/}$suffix"
  [[ -n "$PHYSICAL_DIR" ]] || PHYSICAL_DIR="/"
}

within_directory() {
  local ancestor="$1"
  # cd -P でも大小文字を区別しない FS の綴りはそろわない。
  # 未作成の末尾から既存の親まで戻り、名前でなく実体を比べる。
  while :; do
    [[ "$ancestor" -ef "$2" ]] && return 0
    [[ "$ancestor" != / ]] || return 1
    ancestor="${ancestor%/*}"
    [[ -n "$ancestor" ]] || ancestor="/"
  done
}

# skills と bin の全件を、mkdir・削除・コピーより前に検査する。
# 親のリンクはたどる一方、rm が削除する最終リンクはたどらない。
physical_directory "$SKILLS_SRC"
SOURCE_ROOT="$PHYSICAL_DIR"
physical_directory "$DEST"
DEST_ROOT="$PHYSICAL_DIR"
SOURCES=("$SKILLS_SRC"/*/)
SOURCE_DIRS=("$SOURCE_ROOT")
DESTINATIONS=()
PARENTS=("$DEST_ROOT")
NAMES=()
for src in "${SOURCES[@]}"; do
  src="${src%/}"
  physical_directory "$src"
  SOURCE_DIRS+=("$PHYSICAL_DIR")
  name="${src##*/}"
  DESTINATIONS+=("${DEST_ROOT%/}/$name")
  NAMES+=("$name")
done
if [[ -d "$BIN_SRC" ]]; then
  physical_directory "$BIN_SRC"
  SOURCE_DIRS+=("$PHYSICAL_DIR")
  physical_directory "${DEST%/*}/bin"
  PARENTS+=("$PHYSICAL_DIR")
  SOURCES+=("$BIN_SRC")
  DESTINATIONS+=("${PHYSICAL_DIR%/}/dev-workflow")
  NAMES+=("bin/dev-workflow")
elif [[ -e "$BIN_SRC" || -L "$BIN_SRC" ]]; then
  echo "ERROR: 配布元を解決できません: $BIN_SRC" >&2
  exit 1
else
  echo "NOTE: 配布元に bin/dev-workflow/ がありません。skills だけを置きます。"
fi
for parent in "${PARENTS[@]}"; do
  for source_dir in "${SOURCE_DIRS[@]}"; do
    if within_directory "$parent" "$source_dir"; then
      echo "ERROR: 導入先が配布元と重なっています: $parent" >&2
      exit 1
    fi
  done
done
for dest in "${DESTINATIONS[@]}"; do
  if [[ -d "$dest" && ! -L "$dest" ]]; then
    for source_dir in "${SOURCE_DIRS[@]}"; do
      if within_directory "$source_dir" "$dest"; then
        echo "ERROR: 導入先の削除で配布元が失われます: $dest" >&2
        exit 1
      fi
    done
    for parent in "${PARENTS[@]}"; do
      if within_directory "$parent" "$dest"; then
        echo "ERROR: 管理する導入先どうしが重なっています: $dest" >&2
        exit 1
      fi
    done
  fi
done
for ((i=0; i<${#DESTINATIONS[@]}; i++)); do
  for ((j=i+1; j<${#DESTINATIONS[@]}; j++)); do
    if [[ "${DESTINATIONS[i]}" == "${DESTINATIONS[j]}" ]] ||
       [[ ! -L "${DESTINATIONS[i]}" && ! -L "${DESTINATIONS[j]}" &&
          "${DESTINATIONS[i]}" -ef "${DESTINATIONS[j]}" ]]; then
      echo "ERROR: 管理する導入先どうしが重なっています: ${DESTINATIONS[i]}" >&2
      exit 1
    fi
  done
done

# ファイルの本文だけでなく、項目名・種類・権限・内部のリンク先も比べる。
# stat の GNU/BSD の差を避け、既存要件の Python(標準ライブラリだけ)を使う。
# 最終リンクの同一性はリンク先の実体で判定し、別の配布元のリンクは残さない。
if ! python3 -I -B - "$FORCE" "$MODE" "${#SOURCES[@]}" "${SOURCES[@]}" "${DESTINATIONS[@]}" <<'PYTHON'
import os
from pathlib import Path
import stat
import sys


def same_tree(source, destination):
    left, right = source.lstat(), destination.lstat()
    if left.st_mode != right.st_mode:
        return False
    if stat.S_ISLNK(left.st_mode):
        return os.readlink(source) == os.readlink(destination)
    if stat.S_ISDIR(left.st_mode):
        names = sorted(os.listdir(source))
        return names == sorted(os.listdir(destination)) and all(
            same_tree(source / name, destination / name) for name in names)
    if stat.S_ISREG(left.st_mode):
        if left.st_size != right.st_size:
            return False
        with source.open('rb') as a, destination.open('rb') as b:
            while True:
                chunk = a.read(1024 * 1024)
                if chunk != b.read(1024 * 1024):
                    return False
                if not chunk:
                    return True
    return False


def require_access(path, mode):
    if not os.access(path, mode):
        raise PermissionError(f'読み取り・配置に必要な権限がありません: {path}')


def check_tree_access(path, copying):
    # リンク自体のコピー・削除では、リンク先の権限は要求しない。
    if path.is_symlink():
        return
    if path.is_dir():
        require_access(path, os.R_OK | os.X_OK | (0 if copying else os.W_OK))
        for child in path.iterdir():
            check_tree_access(child, copying)
    elif copying:
        require_access(path, os.R_OK)


force, mode, count = sys.argv[1] == '1', sys.argv[2], int(sys.argv[3])
for source, destination in zip(sys.argv[4:4 + count], sys.argv[4 + count:]):
    source, destination = Path(source), Path(destination)
    try:
        exists = destination.exists() or destination.is_symlink()
        if exists and not force:
            if destination.is_symlink():
                if not destination.exists():
                    raise ValueError('導入先のリンク切れ。--force で一式を置き換えてください')
                same = source.samefile(destination)
            else:
                same = same_tree(source.resolve(strict=True), destination)
            if not same:
                raise ValueError('配布元と内容・権限またはリンク先が異なります(版の混在)。'
                                 '--force で一式を置き換えてください')
            continue
        parent = destination.parent
        while not parent.exists():
            parent = parent.parent
        require_access(parent, os.W_OK | os.X_OK)
        if exists:
            check_tree_access(destination, copying=False)
        if mode == 'copy':
            check_tree_access(source.resolve(strict=True), copying=True)
    except (OSError, RuntimeError, ValueError) as error:
        print(f'ERROR: {destination}: {error}', file=sys.stderr)
        sys.exit(1)
PYTHON
then
  echo "ERROR: 導入前の検査に失敗しました。配置は変更していません。" >&2
  exit 1
fi

mkdir -p "${PARENTS[@]}"
installed=0
skipped=0

for ((i=0; i<${#SOURCES[@]}; i++)); do
  src="${SOURCES[i]%/}"
  name="${NAMES[i]}"
  dest="${DESTINATIONS[i]}"
  if [[ -e "$dest" || -L "$dest" ]]; then
    if [[ "$FORCE" == "1" ]]; then
      rm -rf "$dest"
    else
      echo "SKIP: $name(配布元と一致)"
      skipped=$((skipped + 1))
      continue
    fi
  fi
  if [[ "$MODE" == "link" ]]; then
    ln -s "${src%/}" "$dest"
    echo "LINK: $name -> $dest"
  else
    cp -pR "$src/." "$dest"
    echo "COPY: $name -> $dest"
  fi
  installed=$((installed + 1))
done

echo
echo "完了: ${installed} 件導入 / ${skipped} 件スキップ(導入先: $DEST)"
if [[ "$DEST" == */.agents/skills ]]; then
  echo "次のステップ: 対象ホスト(Codex / Cursor)で /init-project を実行して標準構成を生成してください。"
  echo "  ※ 外部 CLI レビュアーは既定では起動しません(profile の features.runners または --runners で宣言したときのみ)。"
else
  echo "次のステップ: 対象プロジェクトの Claude Code で /init-project を実行して標準構成を生成してください。"
fi

# 使う道具の版を確かめて、表で知らせる(止めない。道具と最低の版は plugins/dev-workflow/hooks/tool-requirements.tsv)。
TOOL_CHECK="$REPO_DIR/plugins/dev-workflow/hooks/tool-versions.sh"
echo
if [[ -f "$TOOL_CHECK" ]]; then
  sh "$TOOL_CHECK" || echo "NOTE: 道具の版を確かめられませんでした(終了コード $?)。手で確かめるには: sh \"$TOOL_CHECK\""
else
  echo "NOTE: 道具の版を確かめるスクリプトが無いので、確かめませんでした: $TOOL_CHECK"
fi
