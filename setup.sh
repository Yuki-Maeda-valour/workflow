#!/usr/bin/env bash
# workflow skills を対象プロジェクト(または ~/.claude)へ導入する補助スクリプト。
# 推奨は plugin marketplace 方式(README 参照)。これはプラグインを使わない場合の代替。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_SRC="$REPO_DIR/plugins/dev-workflow/skills"

usage() {
  cat <<'EOF'
使い方:
  ./setup.sh --link <プロジェクトパス>   skill ごとに symlink を張る(repo 更新が即反映)
  ./setup.sh --copy <プロジェクトパス>   skill をコピーする(独自改変する場合)
  ./setup.sh --global                    ~/.claude/skills に symlink(全プロジェクト共通)
  ./setup.sh --list                      含まれる skills を表示

  他ホスト(Codex / Cursor)向け — .agents/skills へ配置する:
  ./setup.sh --agents <プロジェクトパス>        <パス>/.agents/skills に symlink
  ./setup.sh --agents-copy <プロジェクトパス>   <パス>/.agents/skills にコピー
  ./setup.sh --agents-global                    ~/.agents/skills に symlink

オプション:
  --force    既存の同名 skill を上書き(既定: スキップして警告)

注意:
  - <プロジェクトパス> は事前に存在している必要がある(存在しないとエラー終了する)
  - skill を 1 本だけ取り出す配置は非サポート。skill 間の兄弟参照
    (../do-task/... など)が解決できず、外部ランナー等の機能が無効化される

推奨導入(plugin marketplace 方式)は Claude Code 内で:
  /plugin marketplace add <このリポジトリのパス or GitHub repo>
  /plugin install dev-workflow@valour-workflow
EOF
}

FORCE=0
MODE=""
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --link|--copy) MODE="${1#--}"; TARGET="${2:-}"; shift 2 ;;
    --agents|--agents-copy) MODE="${1#--}"; TARGET="${2:-}"; shift 2 ;;
    --global) MODE="global"; shift ;;
    --agents-global) MODE="agents-global"; shift ;;
    --list) MODE="list"; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "不明な引数: $1" >&2; usage; exit 2 ;;
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
  exit 0
fi

case "$MODE" in
  link|copy)
    [[ -n "$TARGET" ]] || { echo "ERROR: プロジェクトパスを指定してください" >&2; usage; exit 2; }
    [[ -d "$TARGET" ]] || { echo "ERROR: $TARGET が存在しません" >&2; exit 1; }
    DEST="$TARGET/.claude/skills"
    ;;
  agents|agents-copy)
    # Codex(.agents/skills)/ Cursor(.agents/skills or .cursor/skills)向けの配置
    [[ -n "$TARGET" ]] || { echo "ERROR: プロジェクトパスを指定してください" >&2; usage; exit 2; }
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

mkdir -p "$DEST"
installed=0
skipped=0

for src in "$SKILLS_SRC"/*/; do
  name="$(basename "$src")"
  dest="$DEST/$name"
  if [[ -e "$dest" || -L "$dest" ]]; then
    if [[ "$FORCE" == "1" ]]; then
      rm -rf "$dest"
    else
      echo "SKIP: $name(既存。--force で上書き)"
      skipped=$((skipped + 1))
      continue
    fi
  fi
  if [[ "$MODE" == "link" ]]; then
    ln -s "${src%/}" "$dest"
    echo "LINK: $name -> $dest"
  else
    cp -r "${src%/}" "$dest"
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
