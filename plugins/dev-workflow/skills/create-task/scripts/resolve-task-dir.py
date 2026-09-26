#!/usr/bin/env python3
"""Resolve a project's task directory without changing the filesystem."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


# 状態名の列の正本は references/task-directory.md。candidate-keys.py がこの定義を取り込む(2 か所に置かない)
STATE_FILE = re.compile(r"^(?:進行中|完了|中断|保留|候補)_.+\.md$")
DEFAULT_TASK_DIR = "docs/tasks"
# init-project のテンプレートの置換漏れ。一重の {x} は正当なディレクトリ名としてありうるので対象外
PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")


class ResolutionError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1, candidates: list[str] | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.candidates = candidates


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_relative(value: str, root: Path) -> tuple[str, Path]:
    if not value:
        raise ResolutionError("task_dir は空文字にできません")
    raw = Path(value)
    if raw.is_absolute():
        raise ResolutionError("task_dir は管理プロジェクトルートからの相対パスで指定してください")

    candidate = root / Path(value)
    current_input = root
    try:
        for part in raw.parts:
            current_input = current_input / part
            if current_input.is_symlink() and not current_input.exists():
                raise ResolutionError("task_dir にリンク切れのシンボリックリンクが含まれています")
            if current_input.exists() and not current_input.is_dir():
                raise ResolutionError("task_dir またはその祖先が既存ファイルを指しています")
    except (OSError, ValueError) as exc:
        raise ResolutionError(f"task_dir を検査できません: {exc}") from exc
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResolutionError(f"task_dir を解決できません: {exc}") from exc
    if not _inside(resolved, root):
        raise ResolutionError("task_dir が管理プロジェクトの外を指しています")
    current = root
    for part in resolved.relative_to(root).parts:
        current = current / part
        if current.exists() and not current.is_dir():
            raise ResolutionError("task_dir またはその祖先が既存ファイルを指しています")
    normalized = resolved.relative_to(root).as_posix()
    return normalized or ".", resolved


def _placeholder_error(value: str, root: Path) -> ResolutionError:
    message = (
        f"task_dir にテンプレートの置換漏れ({{{{...}}}})が残っています: {value}。"
        "task_dir を管理プロジェクトルートからの相対パスに直すか、キーを消して検出に任せてください"
    )
    # 置換漏れのまま保存先として作られたパスが在れば、その後始末を案内する(存在を確かめるだけ)。
    # `{{X}}/../docs` のように `..` で置換漏れが消える値では、無関係な既存パスを案内しない
    raw = Path(value)
    if not raw.is_absolute():
        lexical = Path(os.path.normpath(root / raw))
        if _inside(lexical, root):
            shown = lexical.relative_to(root).as_posix()
            if PLACEHOLDER.search(shown) and os.path.lexists(lexical):
                message += f"。{shown} が既に在ります。中身を正しい保存先へ移してから削除してください"
    return ResolutionError(message)


def _has_state_file(directory: Path) -> bool:
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_file(follow_symlinks=False) and STATE_FILE.fullmatch(entry.name):
                    return True
    except OSError as exc:
        raise ResolutionError(f"タスク候補を読み取れません: {directory}: {exc}") from exc
    return False


def _candidate_directories(root: Path) -> list[str]:
    containers = [root, root / "docs", root / ".claude"]
    found: set[str] = set()
    for container in containers:
        if not container.exists():
            continue
        if container.is_symlink() or not container.is_dir():
            continue
        try:
            children = sorted(container.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ResolutionError(f"タスク候補の親ディレクトリを読み取れません: {container}: {exc}") from exc
        for child in children:
            if child.is_symlink() or not child.is_dir():
                continue
            if _has_state_file(child):
                found.add(child.relative_to(root).as_posix())
    return sorted(found)


def resolve(project_root: str, task_dir: str | None) -> dict[str, str]:
    root_input = Path(project_root)
    try:
        root = root_input.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResolutionError(f"管理プロジェクトルートを解決できません: {exc}") from exc
    if not root.is_dir():
        raise ResolutionError("管理プロジェクトルートがディレクトリではありません")

    if task_dir is not None:
        if PLACEHOLDER.search(task_dir):
            raise _placeholder_error(task_dir, root)
        normalized, path = _validate_relative(task_dir, root)
        return {"task_dir": normalized, "path": str(path), "source": "profile"}

    candidates = _candidate_directories(root)
    # 置換漏れのまま作られたディレクトリを検出で採用すると、キーを消しても同じ場所へ書き続ける
    leftovers = [candidate for candidate in candidates if PLACEHOLDER.search(candidate)]
    if leftovers:
        raise ResolutionError(
            "テンプレートの置換漏れ({{...}})の名前のディレクトリにタスクがあります: "
            f"{', '.join(leftovers)}。中身を正しい保存先へ移してから削除してください"
        )
    if len(candidates) > 1:
        raise ResolutionError(
            "タスクディレクトリの候補が複数あります。task_dir を明示してください",
            exit_code=2,
            candidates=candidates,
        )
    selected = candidates[0] if candidates else DEFAULT_TASK_DIR
    normalized, path = _validate_relative(selected, root)
    return {
        "task_dir": normalized,
        "path": str(path),
        "source": "detected" if candidates else "default",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--task-dir")
    args = parser.parse_args()
    try:
        result = resolve(args.project_root, args.task_dir)
    except ResolutionError as exc:
        payload: dict[str, object] = {"error": str(exc)}
        if exc.candidates is not None:
            payload["candidates"] = exc.candidates
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return exc.exit_code
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
