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
TOP_KEYS = {"name", "on", "concurrency", "permissions", "jobs"}
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
# R4G(#479)の比較のジョブの工程全体の上限(分)。根拠は native/ci/README.md の R4G の節(実測と見込み)。
R4G_TIMEOUTS = {"r4g": 60, "r4g-universal": 30}
# push 用の流れは main だけ。作業ブランチは PR の pull_request 用で検査する(#495)。
PUSH_EVENT = {"branches": ["main"]}
# 打ち切るのは、同じ PR の古い SHA の pull_request 用の流れだけ。
# main の push と手で起動した流れは、組の名前の最後が run ごとに違う番号なので、打ち切らない。
CONCURRENCY = {
    "group": "${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}",
    "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
}
# S3D(#472)の比較は、build と verify-universal の時間の枠に入らないので、別のジョブで流す。
# s3d は build の tar の 2 回分を 4 機種で、s3d-universal は署名後の Mac のファイルを両 CPU で比較する。
S3D_OPERATIONS = {"s3d": set(RUNNERS), "s3d-universal": {"macos-x86_64", "macos-arm64"}}
# 上限は、比較 1 回の上限(Mac は 120 分・Linux は 60 分。s3d_ci の COMPARISON_LIMITS)× 回数(2・1)に、
# 準備の 30 分を足す(内側の上限で先に止まる)。Mac の 120 分は、run 45133632158 の Mac x86_64 の比較 1 回の
# 最長 3,901 秒の約 1.85 倍である。
# s3d は 2 × 120 + 30 = 270 分、s3d-universal は 1 × 120 + 30 = 150 分(GitHub の 1 ジョブの上限 360 分の内)。
S3D_TIMEOUTS = {"s3d": 270, "s3d-universal": 150}
# S3D の版に依る欄の基準は CI の Ubuntu 24.04 の版。Linux の比較はこの機械で流す。
S3D_BASE_RUNNERS = {"linux-x86_64": "ubuntu-24.04", "linux-arm64": "ubuntu-24.04-arm"}



def native_build_command(command):
    # build.pyの位置引数を先頭に置く正規形だけをCIで許す。
    # argparseが受け付けるオプション先行形や「--」による別表記は許さない。
    if not isinstance(command, str):
        return None
    # R4U(#480)・R4D(#481)・R4E(#482)と S3I-A(#474)の比較は、build の時間の枠の外のジョブで流す。
    # R4G(#479)の比較も、別のジョブ(r4g・r4g-universal)で流す(2026-11-12 12:59 の決定)。
    match = re.match(r"python3 -B native/ci/build\.py (prepare|build|reproduce|combine|verify-universal|package|test|r4u|r4d1|r4e|r4e-universal|implement-guard|implement-guard-universal|r4g|r4g-universal|r4c|r4c-universal)(?: |$)", command)
    # 上の行の目印(test_native_ci.py の R4U_ENTRIES)と重ならないよう、接頭辞と操作名を分けて書く。
    match = match or re.match(r"python3 -B native/ci/build\.py" r" (s3d-universal|s3d)(?: |$)", command)
    # R4A 残り 5 本(#471)の比較も、別のジョブ(r4a-rest・r4a-rest-universal)で流す。
    match = match or re.match(r"python3 -B native/ci/build\.py" r" (r4a-rest-universal|r4a-rest)(?: |$)", command)
    # S2E(#470)の比較も、別のジョブ(s2e・s2e-universal)で流す。
    match = match or re.match(r"python3 -B native/ci/build\.py" r" (s2e-universal|s2e)(?: |$)", command)
    return match[1] if match else None


# R4A 残り 5 本(#471)の比較の操作の名前(build.py の位置引数)。
R4A_REST_OPERATIONS = {"r4a-rest", "r4a-rest-universal"}


# native.yml に必須のジョブ。ジョブを足すときは、この一覧の末尾へ足す(足し忘れで検査が通らなくなる)。
REQUIRED_NATIVE_JOBS = (
    "policy",
    "build",
    "combine",
    "verify-universal",
    "distribution",
    "r4u",
    "r4d1",
    "r4e",
    "r4e-universal",
    "implement-guard",
    "implement-guard-universal",
    "r4g",
    "r4g-universal",
    "s3d",
    "s3d-universal",
    "r4a-rest",
    "r4a-rest-universal",
    "r4c",
    "r4c-universal",
    "s2e",
    "s2e-universal",
)
# 比較のジョブ(R4E #482・S3I-A #474): (ジョブ名, build.py の位置引数, matrix の機種の集合, needs)。
COMPARISON_JOBS = (
    ("r4e", "r4e", set(RUNNERS), "build"),
    ("r4e-universal", "r4e-universal", {"macos-x86_64", "macos-arm64"}, "combine"),
    ("implement-guard", "implement-guard", set(RUNNERS), "build"),
    ("implement-guard-universal", "implement-guard-universal", {"macos-x86_64", "macos-arm64"}, "combine"),
    # R4A 残り 5 本(#471)
    ("r4a-rest", "r4a-rest", set(RUNNERS), "build"),
    ("r4a-rest-universal", "r4a-rest-universal", {"macos-x86_64", "macos-arm64"}, "combine"),
)
# R4G(#479)の比較のジョブ: (ジョブ名, build.py の位置引数, matrix の機種の集合, needs)。
R4G_JOBS = (
    ("r4g", "r4g", set(RUNNERS), "build"),
    ("r4g-universal", "r4g-universal", {"macos-x86_64", "macos-arm64"}, "combine"),
)
# R4C(#478)の比較のジョブ。
R4C_JOBS = (
    ("r4c", "r4c", set(RUNNERS), "build"),
    ("r4c-universal", "r4c-universal", {"macos-x86_64", "macos-arm64"}, "combine"),
)
# S2E(#470)の比較のジョブ。
S2E_JOBS = (
    ("s2e", "s2e", set(RUNNERS), "build"),
    ("s2e-universal", "s2e-universal", {"macos-x86_64", "macos-arm64"}, "combine"),
)
# 上限(分)は S3D の形で決める: 内側の上限(build.py の S2E_TIMEOUT = 3000 秒 = 50 分)× 比較の回数 + 準備(約 5 分)< ジョブの上限。
# s2e は 2 回(2 回目は比較器自身の検査を省く)で 50 × 2 + 5 = 105 < 120 分、s2e-universal は 1 回で 50 + 5 = 55 < 60 分。
# 内側の上限が先に効く。ジョブの上限は既存の build(120 分)・verify-universal(60 分)と同じ枠にした。
# 50 分の根拠(docs/testing.md の記録): R4E は手元の 40〜60 秒に対し CI の Mac x86_64 が 315 秒(約 5.3〜7.9 倍。CI の Linux x86_64 の
# 98 秒に対し 3.2 倍)、R4A の残りは Mac x86_64 298.8 秒 / Linux x86_64 117.1 秒 = 2.55 倍。手元の Mac の経路の模擬 326 秒に
# 5.3〜7.9 倍を当てると 1 回約 29〜43 分(推測)。倍率の出典と見積りは native/ci/README.md の S2E の節。CI の実測で見直す。
S2E_TIMEOUTS = {"s2e": 120, "s2e-universal": 60}


def check_native_required(document):
    """native.yml だけに当てる追加の判定(必須のジョブと、R4E・S3I-A・R4G の比較のジョブの工程・機種・needs)。"""
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), dict):
        return []
    jobs = document["jobs"]
    problems = [f"必須のジョブがありません: {name}" for name in REQUIRED_NATIVE_JOBS if name not in jobs]
    # 比較のジョブの一覧を足すときは、この並びに足す。
    for name, subcommand, platforms, needs in (*COMPARISON_JOBS, *R4G_JOBS, *R4C_JOBS, *S2E_JOBS):
        job = jobs.get(name)
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        steps = steps if isinstance(steps, list) else []
        expected = f"python3 -B native/ci/build.py {subcommand} --platform '${{{{ matrix.platform }}}}'"
        found = any(
            isinstance(step, dict) and native_build_command(step.get("run")) == subcommand and step.get("run") == expected
            for step in steps
        )
        if not found:
            problems.append(f"{name}: build.py {subcommand} の工程が必要です")
        strategy = job.get("strategy")
        include = strategy.get("matrix", {}).get("include") if isinstance(strategy, dict) and isinstance(strategy.get("matrix"), dict) else None
        rows = include if isinstance(include, list) else []
        got = [row.get("platform") for row in rows if isinstance(row, dict)]
        if len(got) != len(platforms) or set(got) != platforms:
            problems.append(f"{name}: matrix の機種は {sorted(platforms)} の {len(platforms)} 機種が必要です")
        if job.get("needs") != needs:
            problems.append(f"{name}: needs は {needs} が必要です")
    return problems


def check_document(document):
    if not isinstance(document, dict):
        return ["ワークフローは object が必要です"]
    problems = []
    if set(document) - TOP_KEYS:
        problems.append("トップの設定は name・on・concurrency・permissions・jobs に限ります")
    events = document.get("on")
    if not isinstance(events, dict) or set(events) != {"pull_request", "push", "workflow_dispatch"}:
        problems.append("イベントは pull_request・push・workflow_dispatch の 3 つに限ります")
    else:
        if any(key != "push" and value != {} and value is not None for key, value in events.items()):
            problems.append("イベントの設定は空の object または null に限ります")
        if events["push"] != PUSH_EVENT:
            problems.append('push は branches: ["main"] だけに限ります')
    if document.get("concurrency") != CONCURRENCY:
        problems.append("concurrency は決めた 1 つの形に限ります")
    if "name" in document and (not isinstance(document["name"], str) or not document["name"]):
        problems.append("name は空でない文字列に限ります")
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

    inspect({key: value for key, value in document.items() if key != "concurrency"})
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
        native_timeouts.update(S3D_TIMEOUTS)
        # R4D-1 の比較は build の後の専用ジョブで流す。上限は 30 分。
        native_timeouts["r4d1"] = 30
        # R4Gは時間の枠に入らないので、別のジョブ(r4g・r4g-universal)で比較する(2026-11-12 12:59 の決定)。
        native_timeouts.update(R4G_TIMEOUTS)
        # R4Eも時間の枠に入らないので、別のジョブ(r4e・r4e-universal)で比較する。
        native_timeouts.update({"r4e": 30, "r4e-universal": 30})
        # S3I-Aの比較のジョブ(手元の所要時間の 2 倍以上。2026-11-12 12:59 の決定)。
        native_timeouts.update({"implement-guard": 45, "implement-guard-universal": 20})
        # R4A 残り 5 本は、1 回の比較の上限 900 秒 × 比較の回数 + 比較器の自己検査(900 秒)+ 準備と余裕で決める(r4a-rest は 2 回で 60 分、r4a-rest-universal は 1 回で 45 分)。
        native_timeouts.update({"r4a-rest": 60, "r4a-rest-universal": 45})
        # R4C(#478)の比較も別のジョブ(r4c・r4c-universal)で流す。
        native_timeouts.update({"r4c": 30, "r4c-universal": 30})
        # S2E(#470)の比較も別のジョブ(s2e・s2e-universal)で流す。
        native_timeouts.update(S2E_TIMEOUTS)
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
                              and native_build_command(step.get("run")) in {"build", "reproduce", "verify-universal", "r4u", "r4d1", "r4e", "r4e-universal", "implement-guard", "implement-guard-universal", "r4c", "r4c-universal"}]
        comparison_indices += [index for index, step in enumerate(steps) if isinstance(step, dict)
                               and native_build_command(step.get("run")) in R4G_TIMEOUTS]
        comparison_indices += [index for index, step in enumerate(steps) if isinstance(step, dict)
                               and native_build_command(step.get("run")) in S3D_OPERATIONS]
        comparison_indices += [index for index, step in enumerate(steps) if isinstance(step, dict)
                               and native_build_command(step.get("run")) in R4A_REST_OPERATIONS]
        comparison_indices += [index for index, step in enumerate(steps) if isinstance(step, dict)
                               and native_build_command(step.get("run")) in S2E_TIMEOUTS]
        python_indices = [index for index, step in enumerate(steps) if isinstance(step, dict)
                          and str(step.get("uses", "")).startswith("actions/setup-python@")]
        if comparison_indices and (len(python_indices) != 1 or not 2 <= python_indices[0] < min(comparison_indices)):
            problems.append(f"{name}: 比較の前、checkout 直後の検査の後に Python 3.10.11 が必要です")
        for index in comparison_indices:
            if native_build_command(steps[index].get("run")) in R4A_REST_OPERATIONS:
                prepares = [i for i, step in enumerate(steps) if isinstance(step, dict)
                            and native_build_command(step.get("run")) == "prepare"]
                if len(prepares) != 1 or not python_indices or not python_indices[0] < prepares[0] < index:
                    problems.append(f"{name}: 比較の前に、Python の後で prepare を 1 回流す必要があります(rustc の記録を照らすため)")
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
    problems.extend(check_s3d_jobs(jobs))
    return problems


def check_s3d_jobs(jobs):
    """build を流すワークフローでは、S3D の比較のジョブを必須にする(機種をそろえ、省けない形に限る)。"""
    found = {operation: [] for operation in S3D_OPERATIONS}
    builds = False
    for job in jobs.values():
        steps = job.get("steps") if isinstance(job, dict) else None
        for step in steps if isinstance(steps, list) else []:
            operation = native_build_command(step.get("run") if isinstance(step, dict) else None)
            builds = builds or operation == "build"
            if operation in found:
                found[operation].append((job, step["run"]))
    if not builds:
        return []
    problems = []
    if any(RUNNERS.get(platform) != runner for platform, runner in S3D_BASE_RUNNERS.items()):
        problems.append("S3D の Linux の比較は基準の版の機械(Ubuntu 24.04)で流します")
    for operation, platforms in S3D_OPERATIONS.items():
        if len(found[operation]) != 1:
            problems.append(f"S3D の比較のジョブ({operation})が 1 つ必要です")
            continue
        job, command = found[operation][0]
        matrix = job.get("strategy", {}).get("matrix", {}) if isinstance(job.get("strategy"), dict) else {}
        rows = matrix.get("include") if isinstance(matrix, dict) else None
        listed = [row.get("platform") for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        if sorted(listed) != sorted(platforms) or job.get("runs-on") != "${{ matrix.runner }}":
            problems.append(f"S3D の比較を流す機械がそろっていません({operation})")
        if command != f"python3 -B native/ci/build.py {operation} --platform '${{{{ matrix.platform }}}}'":
            problems.append(f"S3D の比較は matrix の機械ごとに流してください({operation})")
    return problems


def overlap_problems(file_name, document, names):
    # 組の名前は大文字と小文字を区別しない。名前が重なると、同じ PR の 2 本が互いを打ち切る。
    # name が無いときの GitHub の既定の名前は、ファイルのパス。
    if not isinstance(document, dict):
        return []
    name = document.get("name", f".github/workflows/{file_name}")
    if not isinstance(name, str) or not name:
        return []
    earlier = names.setdefault(name.casefold(), file_name)
    if earlier == file_name:
        return []
    return [f"{file_name}: ワークフローの名前が重なっています: {name}({earlier} と同じ)"]


def check_workflows(root=ROOT):
    folder = Path(root) / ".github/workflows"
    files = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])
    if not files:
        return [".github/workflows/ に検査するファイルがありません"]
    problems = []
    names = {}
    for path in files:
        if path.is_symlink() or not path.is_file():
            problems.append(f"{path.name}: 通常ファイルが必要です")
            continue
        try:
            document = read_json(path)
            errors = check_document(document)
        except (OSError, ValueError) as error:
            document = None
            errors = [f"JSON 形式の YAML が必要です(重複キーも拒否します): {error}"]
        if path.name == "native.yml":
            try:
                errors = [*errors, *check_native_required(read_json(path))]
            except (OSError, ValueError):
                pass
        problems.extend(f"{path.name}: {error}" for error in errors)
        problems.extend(overlap_problems(path.name, document, names))
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
