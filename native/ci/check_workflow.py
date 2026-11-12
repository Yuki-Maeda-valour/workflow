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
    "actions/setup-python": "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
}
ACTION_INPUTS = {
    "actions/setup-python": {"python-version"},
    "actions/checkout": {"repository", "ref", "persist-credentials", "fetch-depth"},
    "actions/upload-artifact": {"name", "path", "if-no-files-found", "retention-days"},
    "actions/download-artifact": {"name", "pattern", "path", "merge-multiple"},
}
TOP_KEYS = {"name", "on", "permissions", "jobs"}
JOB_KEYS = {"needs", "strategy", "runs-on", "timeout-minutes", "steps"}
SOURCE_REF = "${{ github.event.pull_request.head.sha || github.sha }}"
SOURCE_REPO = "${{ github.event.pull_request.head.repo.full_name || github.repository }}"
ALLOWED_EXPRESSIONS = {SOURCE_REF, SOURCE_REPO, "${{ matrix.platform }}", "${{ matrix.runner }}", "${{ runner.temp }}"}
RUNNERS = {"linux-x86_64": "ubuntu-24.04", "linux-arm64": "ubuntu-24.04-arm",
           "macos-x86_64": "macos-15-intel", "macos-arm64": "macos-15"}
POLICY_COMMAND = "python3 -B native/ci/check_workflow.py"
SCRIPT_COMMAND = re.compile(
    r"python3 -B native/ci/[a-z_]+\.py(?: (?:[a-zA-Z0-9_./-]+|'\$\{\{ matrix\.platform \}\}'))*"
)
# 段階 0b(#443)の配布の確かめ。配布 commit の浅い clone で、配った入口を起動する。
# native/ の外に置く(native/ はソース入力なので、0b では変えない)。この 1 行の字面だけを許す。
DIST_LAUNCH_COMMAND = "python3 -B scripts/check_dist_launch.py --root ."



def native_build_command(command):
    # build.pyの位置引数を先頭に置く正規形だけをCIで許す。
    # argparseが受け付けるオプション先行形や「--」による別表記は許さない。
    if not isinstance(command, str):
        return None
    match = re.match(r"python3 -B native/ci/build\.py (prepare|build|reproduce|combine|verify-universal|package|test|r4u|r4d1)(?: |$)", command)
    return match[1] if match else None


def check_document(document):
    if not isinstance(document, dict):
        return ["ワークフローは object が必要です"]
    problems = []
    if set(document) - TOP_KEYS:
        problems.append("トップの設定は name・on・permissions・jobs に限ります")
    events = document.get("on")
    if not isinstance(events, dict) or set(events) != {"pull_request", "push", "workflow_dispatch"}:
        problems.append("イベントは pull_request・push・workflow_dispatch の 3 つに限ります")
    elif any(value != {} and value is not None for value in events.values()):
        problems.append("イベントの設定は空の object または null に限ります")
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
        if set(job) - JOB_KEYS:
            problems.append(f"{name}: job の設定は needs・strategy・runs-on・timeout-minutes・steps に限ります")
        # R4Bはbuildで2回、署名後は各CPUで1回比較する。外側で打ち切らない。
        # S2L(#468)はbuildの各回の比較とqualityの単体・変異の検査、署名後の各CPUの比較を足す。
        # R4Rのpush用run 43085900763でMac Intelのbuildが48.2分・verify-universalが26.6分を使ったため、120分・60分にする。
        # R4U(#480)の比較は build の枠に入れず、ジョブ r4u で流す(2026-11-12 12:59 の決定)。
        native_timeouts = {"build": 120, "verify-universal": 60, "r4u": 30}
        # R4D-1 の比較は build の後の専用ジョブで流す。上限は 30 分。
        native_timeouts["r4d1"] = 30
        steps = job.get("steps")
        for step in steps if isinstance(steps, list) else []:
            command = step.get("run") if isinstance(step, dict) else None
            operation = native_build_command(command)
            if operation in native_timeouts:
                required = native_timeouts[operation]
                timeout = job.get("timeout-minutes")
                if type(timeout) is not int or timeout != required:
                    problems.append(f"{name}: timeout-minutes は {required} が必要です")
        strategy = job.get("strategy")
        valid_matrix = False
        if "strategy" in job:
            if not isinstance(strategy, dict) or set(strategy) - {"fail-fast", "matrix"}:
                problems.append(f"{name}: strategy の設定は fail-fast・matrix に限ります")
            if isinstance(strategy, dict) and "fail-fast" in strategy and not isinstance(strategy["fail-fast"], bool):
                problems.append(f"{name}: fail-fast は boolean が必要です")
            matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
            rows = matrix.get("include") if isinstance(matrix, dict) else None
            if not isinstance(rows, list) or not rows or set(matrix) != {"include"}:
                problems.append(f"{name}: matrix は確認済みの機械の include 一覧に限ります")
            elif any(not isinstance(row, dict) or set(row) != {"platform", "runner"}
                     or not isinstance(row.get("platform"), str)
                     or row["platform"] not in RUNNERS
                     or RUNNERS[row["platform"]] != row["runner"] for row in rows):
                problems.append(f"{name}: matrix の対象名と機械が確認済みの組ではありません")
            else:
                valid_matrix = True
        runner = job.get("runs-on")
        if not isinstance(runner, str) or not (
                runner in RUNNERS.values() or (runner == "${{ matrix.runner }}" and valid_matrix)):
            problems.append(f"{name}: runs-on は確認済みの GitHub の機械か、その matrix.runner に限ります")
        steps = job.get("steps")
        if not isinstance(steps, list) or len(steps) < 2:
            problems.append(f"{name}: checkout と最初の検査がありません")
            continue
        if not isinstance(steps[0], dict) or not str(steps[0].get("uses", "")).startswith("actions/checkout@"):
            problems.append(f"{name}: 最初に checkout が必要です")
        if not isinstance(steps[1], dict) or steps[1].get("run") != POLICY_COMMAND:
            problems.append(f"{name}: checkout の直後に check_workflow.py が必要です")
        comparison_indices = [index for index, step in enumerate(steps) if isinstance(step, dict)
                              and native_build_command(step.get("run")) in {"build", "reproduce", "verify-universal", "r4u", "r4d1"}]
        python_indices = [index for index, step in enumerate(steps) if isinstance(step, dict)
                          and str(step.get("uses", "")).startswith("actions/setup-python@")]
        if comparison_indices and (len(python_indices) != 1 or not 2 <= python_indices[0] < min(comparison_indices)):
            problems.append(f"{name}: 比較の前、checkout 直後の検査の後に Python 3.10.11 が必要です")
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
                if isinstance(command, str) and command.startswith("python3 -B native/ci/build.py ") and native_build_command(command) is None:
                    problems.append(f"{name}: build.py はコマンド名をオプションより先に指定してください")
                if not isinstance(command, str) or not (
                        SCRIPT_COMMAND.fullmatch(command) or command == DIST_LAUNCH_COMMAND):
                    problems.append(f"{name}: run は native/ci/ のスクリプトを 1 本呼ぶか、"
                                    f"配布の確かめ({DIST_LAUNCH_COMMAND})を呼ぶだけにしてください")
            if "uses" not in step:
                if "with" in step:
                    problems.append(f"{name}: with は Action だけに指定できます")
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
            settings = step.get("with", {})
            if not isinstance(settings, dict):
                problems.append(f"{name}: {action} の with は object が必要です")
                continue
            if set(settings) - ACTION_INPUTS.get(action, set()):
                problems.append(f"{name}: {action} の with に許可していないキーがあります")
            if action == "actions/setup-python" and settings != {"python-version": "3.10.11"}:
                problems.append(f"{name}: 比較用 Python は 3.10.11 だけ指定してください")
            if action == "actions/checkout":
                # 0b の浅い clone は 1 だけを許す。履歴全体や式での指定は不要。
                if "fetch-depth" in settings and (type(settings["fetch-depth"]) is not int or settings["fetch-depth"] != 1):
                    problems.append(f"{name}: fetch-depth は整数の 1 だけ指定できます")
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
