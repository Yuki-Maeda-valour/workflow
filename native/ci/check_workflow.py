#!/usr/bin/env python3
"""全ワークフローを JSON 形式の YAML に限定して安全条件を検査する。"""

import argparse
from pathlib import Path
import re
import sys

from make_dist import read_json
from source_input_hash import ROOT


# 2026-11-08、gh api のタグの解決・commit・action.yml で確認した。
VERIFIED_ACTIONS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
}
SOURCE_REF = "${{ github.event.pull_request.head.sha || github.sha }}"
SOURCE_REPO = "${{ github.event.pull_request.head.repo.full_name || github.repository }}"
ALLOWED_EXPRESSIONS = {SOURCE_REF, SOURCE_REPO, "${{ matrix.platform }}", "${{ matrix.runner }}", "${{ runner.temp }}"}
RUNNERS = {"linux-x86_64": "ubuntu-24.04", "linux-arm64": "ubuntu-24.04-arm",
           "macos-x86_64": "macos-15-intel", "macos-arm64": "macos-15"}
POLICY_COMMAND = "python3 -B native/ci/check_workflow.py"
SCRIPT_COMMAND = re.compile(
    r"python3 -B native/ci/[a-z_]+\.py(?: (?:[a-zA-Z0-9_./-]+|'\$\{\{ matrix\.platform \}\}'))*"
)


def check_document(document):
    if not isinstance(document, dict):
        return ["ワークフローは object が必要です"]
    problems = []
    events = document.get("on")
    if not isinstance(events, dict) or set(events) != {"pull_request", "push", "workflow_dispatch"}:
        problems.append("イベントは pull_request・push・workflow_dispatch の 3 つに限ります")
    if document.get("permissions") != {"contents": "read"}:
        problems.append("permissions は contents: read に限ります")

    def inspect(value):
        if isinstance(value, str):
            for expression in re.findall(r"\$\{\{.*?\}\}", value, re.DOTALL):
                if expression not in ALLOWED_EXPRESSIONS:
                    problems.append("確認していない式は使えません: " + expression)
            if re.search(r"\bsecrets\b|\bgithub\s*(?:\.\s*token\b|\[\s*['\"]token['\"]\s*\])", value, re.IGNORECASE):
                problems.append("秘密情報をワークフローへ渡せません")
            return
        if isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key == "permissions" and child != {"contents": "read"}:
                    problems.append("工程で権限を変更できません")
                if key in {"secrets", "container", "services", "continue-on-error", "if", "shell", "working-directory"}:
                    problems.append(f"検査対象外の設定は使えません: {key}")
                if key in {"env", "defaults"}:
                    problems.append("環境・実行の設定は native/ci/ に置いてください")
                inspect(child)

    inspect(document)
    jobs = document.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return problems + ["jobs がありません"]
    for name, job in jobs.items():
        if not isinstance(job, dict) or "uses" in job:
            problems.append(f"{name}: 再利用ワークフローは検査対象外です")
            continue
        strategy = job.get("strategy")
        if strategy is not None:
            matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
            rows = matrix.get("include") if isinstance(matrix, dict) else None
            if not isinstance(rows, list) or not rows or set(matrix) != {"include"}:
                problems.append(f"{name}: matrix は確認済みの機械の include 一覧に限ります")
            elif any(not isinstance(row, dict) or set(row) != {"platform", "runner"}
                     or not isinstance(row.get("platform"), str)
                     or RUNNERS.get(row["platform"]) != row["runner"] for row in rows):
                problems.append(f"{name}: matrix の対象名と機械が確認済みの組ではありません")
        steps = job.get("steps")
        if not isinstance(steps, list) or len(steps) < 2:
            problems.append(f"{name}: checkout と最初の検査がありません")
            continue
        if not isinstance(steps[0], dict) or not str(steps[0].get("uses", "")).startswith("actions/checkout@"):
            problems.append(f"{name}: 最初に checkout が必要です")
        if not isinstance(steps[1], dict) or steps[1].get("run") != POLICY_COMMAND:
            problems.append(f"{name}: checkout の直後に check_workflow.py が必要です")
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                problems.append(f"{name}: step は object が必要です")
                continue
            if set(step) - {"name", "run", "uses", "with"}:
                problems.append(f"{name}: step の設定は name・run・uses・with に限ります")
            if "continue-on-error" in step or (index < 2 and "if" in step):
                problems.append(f"{name}: 必須の検査を飛ばす設定は使えません")
            if ("run" in step) == ("uses" in step):
                problems.append(f"{name}: step には run か uses のどちらか 1 つが必要です")
            if "run" in step:
                command = step["run"]
                if not isinstance(command, str) or not SCRIPT_COMMAND.fullmatch(command):
                    problems.append(f"{name}: run は native/ci/ のスクリプトを 1 本呼ぶだけにしてください")
            if "uses" not in step:
                continue
            uses = step["uses"]
            match = re.fullmatch(r"([a-zA-Z0-9_.-]+/[a-zA-Z0-9_./-]+)@([0-9a-f]{40})", str(uses))
            if match is None:
                problems.append(f"{name}: Action は完全な commit SHA で固定してください")
                continue
            action, sha = match.groups()
            if "cache" in action.lower():
                problems.append(f"{name}: キャッシュの Action は使えません")
            if VERIFIED_ACTIONS.get(action) != sha:
                problems.append(f"{name}: commit と action.yml の確認記録がない Action です: {uses}")
            if action == "actions/checkout":
                settings = step.get("with", {})
                if not isinstance(settings, dict):
                    problems.append(f"{name}: checkout の設定が不正です")
                    continue
                if settings.get("persist-credentials") is not False:
                    problems.append(f"{name}: persist-credentials: false が必要です")
                if settings.get("ref") != SOURCE_REF or settings.get("repository") != SOURCE_REPO:
                    problems.append(f"{name}: PR のソース SHA とリポジトリを明示してください")
    return problems


def check_workflows(root=ROOT):
    folder = Path(root) / ".github/workflows"
    files = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])
    if not files:
        return [".github/workflows/ に検査するファイルがありません"]
    problems = []
    for path in files:
        if path.is_symlink() or not path.is_file():
            problems.append(f"{path.name}: 通常ファイルが必要です")
            continue
        try:
            errors = check_document(read_json(path))
        except (OSError, ValueError) as error:
            errors = [f"JSON 形式の YAML が必要です(重複キーも拒否します): {error}"]
        problems.extend(f"{path.name}: {error}" for error in errors)
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    problems = check_workflows(args.root)
    for problem in problems:
        print("ERROR: " + problem, file=sys.stderr)
    if not problems:
        print("ワークフロー: 合格")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
