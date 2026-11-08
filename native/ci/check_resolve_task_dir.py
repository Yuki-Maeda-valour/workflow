#!/usr/bin/env python3
"""Python 3.10 の旧版と、今回作った resolve-task-dir の結果と不変性を比較する。"""

import errno
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tempfile

from check_probe import load_support
from source_input_hash import ROOT


DISPLAY_NAME = "dev-workflow resolve-task-dir"
LEGACY = ROOT / "plugins/dev-workflow/skills/create-task/scripts/resolve-task-dir.py"
# argv[0] の表示名だけをそろえる。argparse と main の中身は旧版のまま使う。
LEGACY_MAIN = (
    "import importlib.util, sys; "
    "spec = importlib.util.spec_from_file_location('legacy_task_dir', sys.argv[1]); "
    "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
    f"sys.argv = [{DISPLAY_NAME!r}, *sys.argv[2:]]; "
    "raise SystemExit(module.main())"
)


def require_python():
    if sys.version_info[:2] != (3, 10):
        raise ValueError(f"比較には Python 3.10 が必要です。実行中: {platform.python_version()}")


def legacy_command(args):
    return [sys.executable, "-B", "-c", LEGACY_MAIN, str(LEGACY), *args]


def state(root, directory, name="進行中_作業.md"):
    folder = root / directory
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"# task\nunchanged\0\xff\n")


def link(root, name, target):
    (root / name).symlink_to(target)


def cases():
    # 旧版の 29 テストが確かめる性質を、独立した初期コピーで比較する。
    yield "default", lambda r: (r / "task").mkdir(), [], 0
    yield "old-directory", lambda r: state(r, "task"), [], 0
    for status in ("進行中", "完了", "中断", "保留", "候補"):
        yield "state-" + status, lambda r, s=status: state(r, "task", s + "_項目.md"), [], 0
    yield "same-candidate", lambda r: (state(r, "task"), state(r, "task", "候補_監査.md")), [], 0
    yield "two-candidate-states", lambda r: (state(r, "docs/candidates", "候補_監査.md"), state(r, "task")), [], 2
    yield "empty-and-deep", lambda r: (state(r, "task", "進行中_.md"), state(r, "docs/archive/deep")), [], 0
    for directory in ("work", "docs/work", ".claude/work"):
        yield "container-" + directory, lambda r, d=directory: state(r, d), [], 0
    yield "sorted", lambda r: (state(r, "z-tasks"), state(r, "docs/a-tasks")), [], 2
    for value in ("作業 記録", "-tasks"):
        yield "priority-" + value, lambda r: state(r, "task"), ["--task-dir=" + value], 0
    for value in ("", "/outside", "../outside", "file", "file/..", "file-link/.."):
        yield "invalid-" + value, lambda r: ((r / "file").touch(), link(r, "file-link", "file")), ["--task-dir=" + value], 1
    yield "internal-link", lambda r: ((r / "inside").mkdir(), link(r, "internal-link", "inside")), ["--task-dir=internal-link"], 0
    yield "external-link", lambda r: link(r, "external-link", "/"), ["--task-dir=external-link"], 1
    yield "link-parent", lambda r: ((r / "a/b").mkdir(parents=True), link(r, "link", "a/b")), ["--task-dir=link/../tasks"], 0
    yield "no-directory-links", lambda r: (state(r, "real/deep"), link(r, "linked-tasks", "real/deep")), [], 0
    yield "no-state-links", lambda r: ((r / "task").mkdir(), (r / "進行中_実体.md").touch(), link(r, "task/進行中_リンク.md", "../進行中_実体.md")), [], 0
    yield "default-file", lambda r: ((r / "docs").mkdir(), (r / "docs/tasks").touch()), [], 1
    yield "default-ancestor-file", lambda r: (r / "docs").touch(), [], 1
    yield "default-external-link", lambda r: link(r, "docs", "/"), [], 1
    yield "loop", lambda r: link(r, "loop", "loop"), ["--task-dir=loop/tasks"], 1
    yield "broken", lambda r: link(r, "broken", "missing"), ["--task-dir=broken/tasks"], 1
    yield "source-independent", lambda r: ((r / "source").mkdir(), state(r, "tasks")), [], 0
    yield "read-only", lambda r: (r / "data").write_bytes(b"unchanged\0\xff"), [], 0
    for value in ("{{TASK_DIR}}", "docs/{{TASK_DIR}}", "{x}", "a{b}c"):
        yield "placeholder-" + value, lambda r: None, ["--task-dir=" + value], 1 if "{{" in value else 0
    yield "placeholder-existing", lambda r: state(r, "{{TASK_DIR}}"), ["--task-dir={{TASK_DIR}}"], 1
    yield "placeholder-detection", lambda r: state(r, "{{TASK_DIR}}"), [], 1
    yield "placeholder-before-multiple", lambda r: (state(r, "{{TASK_DIR}}"), state(r, "docs/{{TASK_DIR}}"), state(r, "task")), [], 1
    for value in ("{{X}}/..", "{{X}}/../docs", "docs/{{X}}/..", "../{{X}}"):
        yield "placeholder-unrelated-" + value, lambda r: (r / "docs").mkdir(), ["--task-dir=" + value], 1
    # コピーした旧版でも動く性質。比較元自身をコピーし、同じ本体を実行する。
    yield "copied-distribution", lambda r: shutil.copy2(LEGACY, r / "resolve-task-dir.py"), [], 0
    for value in ("missing/../x", ".", "a//./b/..", "not-created", "{{a{b}}", "{{}}", "{{x\ny}}"):
        yield "path-boundary-" + value, lambda r: None, ["--task-dir=" + value], 1 if value in ("{{}}", "{{x\ny}}") else 0
    yield "state-newline", lambda r: (state(r, "task", "進行中_a\nb.md"), state(r, "other", "候補_x.md\n")), [], 0
    yield "controls", lambda r: state(r, '引用"\\\n\t名'), [], 0
    yield "duplicate-candidate", lambda r: (state(r, "docs"), state(r, "docs/tasks")), [], 2
    yield "relative-sibling-root", lambda r: None, ["@root", "../native"], 0
    yield "relative-multiple-parent-root", lambda r: (r / "a").mkdir(), ["@root", "a/../../native"], 0
    yield "relative-parent-root", lambda r: None, ["@root", ".."], 0
    yield "relative-parent-components", lambda r: (r / "a").mkdir(), ["@root", "a/../.."], 0
    yield "root-file", lambda r: (r / "file").touch(), ["@root", "file"], 1
    yield "root-absent", lambda r: None, ["@root", "missing/.."], 1
    yield "root-link", lambda r: ((r / "real").mkdir(), link(r, "root-link", "real")), ["@root", "root-link"], 0
    yield "root-loop", lambda r: link(r, "root-link", "root-link"), ["@root", "root-link"], 1
    yield "root-broken", lambda r: link(r, "root-link", "missing"), ["@root", "root-link"], 1
    yield "unicode-order", lambda r: (state(r, "\ue000"), state(r, "\U00010000"), state(r, "日本語")), [], 2


def parser_cases():
    for args, code in (
        ([], 2), (["--project-root"], 2), (["--unknown"], 2),
        (["--unknown", "--help"], 0), (["-hh"], 0), (["-hhh"], 0),
        (["-hx"], 2), (["-h="], 2), (["--help=x"], 2),
        (["--task-dir", "--help"], 2), (["--help", "--task-dir"], 0),
        (["--help", "--"], 0), (["--h"], 0), (["--p", ".", "--t=x"], 0),
        (["--project-root", ".", "--project-root", ".", "--task-dir=a", "--task-dir=b"], 0),
        (["--project-root=.", "--task-dir="], 1), (["--project-root=.", "--task-dir", "-3.5"], 0),
        (["--project-root=.", "--task-dir", "-tasks"], 2),
        (["--project-root=.", "--task-dir=-tasks"], 0),
        (["--project-root=.", "--", "--task-dir=x"], 2),
        (["--project-root=.", "extra", "--unknown"], 2),
        (["--project-root=.", "--task-dir", "-with space"], 0),
        (["--project-root=.", b"--unknown\xff"], 2),
    ):
        yield args, code
    for value in ("-h", "--help"):
        yield [value], 0
    for value in ("\u2028", "\u2029", "\u200b", "\ue000", "\u00ad", "\u0378", "\U0001fffe", "a'\"b", "\n\t"):
        yield ["--help=" + value], 2
        yield ["--project-root", "missing-" + value], 1
    yield ["--project-root", b"missing-\xff"], 1
    for number in ("-1", "-1.25", "-.5", "-١٢", "-١.٢٥", "-.٥"):
        for suffix, expected in (("\n", 0), ("\r", 2), ("\n\n", 2), ("\r\n", 2)):
            yield ["--project-root", ".", "--task-dir", number + suffix], expected
    yield ["--project-root", ".", "--task-dir", "-1"], 0
    yield ["--project-root", ".", "--task-dir", "-"], 0
    yield ["--project-root", ".", "--task-dir", "--"], 2


def check_case(support, binary, initial, work, args, expected, *, columns=80, copied=False):
    environment = {**os.environ, "COLUMNS": str(columns), "PYTHONDONTWRITEBYTECODE": "1"}
    old_cwd, new_cwd = work / "legacy", work / "native"

    def equal_with_roots(old, new):
        return old.replace(os.fsencode(old_cwd), os.fsencode(new_cwd)) == new

    legacy = legacy_command(args)
    if copied:
        legacy[4] = "resolve-task-dir.py"
    comparison = support.compare_runs(legacy, [str(binary), "resolve-task-dir", *args],
        initial_tree=initial, work_dir=work, env=environment,
        allow_differences={"stdout": equal_with_roots, "stderr": equal_with_roots})
    if not comparison.matches or comparison.legacy.returncode != expected:
        raise ValueError(f"比較が不一致です: {work.name}: {args!r}: {comparison.differences}\n"
                         f"旧版={comparison.legacy}\nRust={comparison.native}")
    if not (comparison.before == comparison.legacy_tree == comparison.native_tree):
        raise ValueError(f"実行が初期のファイルを変更しました: {work.name}")


def legacy_frame(statement, function, *, previous=None):
    """旧版の既知の行だけを使い、Python 3.10 の traceback のフレームを組み立てる。"""
    lines = LEGACY.read_text(encoding="utf-8").splitlines()
    found = [number for number, line in enumerate(lines, 1) if line.strip() == statement
             and (previous is None or (number > 1 and lines[number - 2].strip() == previous))]
    if len(found) != 1:
        raise ValueError(f"旧版の既知の出力経路を特定できません: {statement}")
    return f'  File "{LEGACY}", line {found[0]}, in {function}\n    {statement}\n'


def non_utf8_expectation(kind, folder):
    """解決処理を呼ばず、固定した入力に対する期待値を独立に作る。"""
    root = folder.resolve(strict=True)
    if kind == "explicit":
        args = ["--project-root", ".", b"--task-dir=x\xff"]
        payload = {"path": str(root / os.fsdecode(b"x\xff")), "source": "profile", "task_dir": os.fsdecode(b"x\xff")}
        return args, payload, 0
    if kind == "root":
        args = ["--project-root", b"\xff"]
        payload = {"path": str(root / os.fsdecode(b"\xff") / "docs/tasks"), "source": "default", "task_dir": "docs/tasks"}
        return args, payload, 0
    if kind == "candidates":
        # Python の文字の比較順。無効なバイトは U+DCFF として比較される。
        payload = {"candidates": [os.fsdecode(b"\xff"), "\ue000", "\U00010000"],
                   "error": "タスクディレクトリの候補が複数あります。task_dir を明示してください"}
        return ["--project-root", "."], payload, 2
    raise ValueError(f"未知の非 UTF-8 ケースです: {kind}")


def strict_exception(payload, code):
    """期待 JSON の出力時だけに発生する例外と、既知の呼び出し元を完全一致させる。"""
    expected_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        expected_text.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        # __str__ は encoding・対象文字・開始/終了位置・reason を含む。
        diagnostic = "UnicodeEncodeError: " + str(error) + "\n"
    else:
        raise ValueError("期待 JSON に非 UTF-8 のパスがありません")
    header = "Traceback (most recent call last):\n"
    context = ""
    if code == 2:
        context = (header + legacy_frame("result = resolve(args.project_root, args.task_dir)", "main")
                   + legacy_frame("raise ResolutionError(", "resolve", previous="if len(candidates) > 1:")
                   + "legacy_task_dir.ResolutionError: " + payload["error"] + "\n\n"
                   + "During handling of the above exception, another exception occurred:\n\n")
    elif code != 0:
        raise ValueError(f"未知の非 UTF-8 の終了値です: {code}")
    variable = "payload" if code == 2 else "result"
    statement = f"print(json.dumps({variable}, ensure_ascii=False, sort_keys=True))"
    return (context + header + '  File "<string>", line 1, in <module>\n'
            + legacy_frame(statement, "main") + diagnostic).encode("utf-8", "backslashreplace")


def check_non_utf8_results(old, new, *, mode, payloads, code, folders):
    expected = [(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8", "surrogateescape")
                for payload in payloads]
    if new.timed_out or new.returncode != code or new.stderr or new.stdout != expected[1]:
        raise ValueError(f"Rust の非 UTF-8 出力・終了値・出力先が不正です: {new}")
    if mode == "strict":
        if old.timed_out or old.returncode != 1 or old.stdout or old.stderr != strict_exception(payloads[0], code):
            raise ValueError(f"旧版の JSON 出力時の UnicodeEncodeError・終了値・出力先が不正です: {old}")
    elif mode == "surrogateescape":
        if (old.timed_out or old.returncode != code or old.stderr or old.stdout != expected[0]
                or old.stdout.replace(os.fsencode(folders[0].resolve()), os.fsencode(folders[1].resolve())) != new.stdout):
            raise ValueError(f"非 UTF-8 の surrogateescape の比較が不一致です: {old}: {new}")
    else:
        raise ValueError(f"未知の非 UTF-8 の出力モードです: {mode}")


def check_non_utf8_case(support, binary, initial, work, kind, mode):
    before = support.snapshot_tree(initial)
    work.mkdir()
    folders = [work / side for side in ("legacy", "native")]
    for folder in folders:
        shutil.copytree(initial, folder, symlinks=True)
        if support.snapshot_tree(folder) != before:
            raise ValueError("非 UTF-8 比較の初期コピーが違います")
    environment = {**os.environ, "COLUMNS": "80", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8:" + mode}
    expectations = [non_utf8_expectation(kind, folder) for folder in folders]
    args, _, code = expectations[0]
    legacy = legacy_command(args)
    # import・argv・cwd・環境と Python の起動オプションを本実行と同じにする。
    probe = legacy.copy()
    probe[3] = LEGACY_MAIN.replace("raise SystemExit(module.main())",
                                 "print(module.json.dumps([sys.stdout.encoding, sys.stdout.errors]))")
    try:
        observed = support.run_command(probe, cwd=folders[0], env=environment)
        expected_stream = (json.dumps(["utf-8", mode]) + "\n").encode("ascii")
        if observed.timed_out or observed.returncode != 0 or observed.stderr or observed.stdout != expected_stream:
            raise ValueError(f"旧版の標準出力の encoding・errors が指定と違います: {observed}")
        old = support.run_command(legacy, cwd=folders[0], env=environment)
        new = support.run_command([str(binary), "resolve-task-dir", *args], cwd=folders[1], env=environment)
        check_non_utf8_results(old, new, mode=mode, payloads=[item[1] for item in expectations], code=code, folders=folders)
    finally:
        for folder in [initial, *folders]:
            if support.snapshot_tree(folder) != before:
                raise ValueError(f"非 UTF-8 比較で初期のファイルが変わりました: {folder.name}")


def non_utf8_cases(support, binary, parent):
    total = 0
    initial = parent / "non-utf8-explicit"
    initial.mkdir()
    state(initial, "unchanged")
    # 実在名を作れない OS でも、生バイトの明示値は両モードで必ず検査する。
    for mode in ("strict", "surrogateescape"):
        check_non_utf8_case(support, binary, initial, parent / ("non-utf8-explicit-" + mode), "explicit", mode)
        total += 1
    initial = parent / "non-utf8-existing"
    initial.mkdir()
    try:
        state(initial, os.fsdecode(b"\xff"))
    except OSError as error:
        if platform.system() != "Darwin" or error.errno != errno.EILSEQ:
            raise
        print("非 UTF-8 の実在ルート・候補名: APFS が名前の作成を EILSEQ で拒否。両モードを省略。明示値の両モードは実行済み")
        return total
    state(initial, "\ue000")
    state(initial, "\U00010000")
    for kind in ("root", "candidates"):
        for mode in ("strict", "surrogateescape"):
            check_non_utf8_case(support, binary, initial, parent / ("non-utf8-" + kind + "-" + mode), kind, mode)
            total += 1
    return total


def permission_cases(support, binary, parent):
    if os.geteuid() == 0:
        raise ValueError("権限拒否の比較は通常利用者での実行が必須です。root では合格にできません")
    for name, selected, mode in (("candidate", "locked", 0), ("container", ".", 0o100)):
        initial = parent / ("permission-initial-" + name)
        initial.mkdir()
        state(initial, "locked")
        before = support.snapshot_tree(initial)
        folders = [parent / ("permission-" + name + "-" + side) for side in ("legacy", "native")]
        for folder in folders:
            shutil.copytree(initial, folder, symlinks=True)
            if support.snapshot_tree(folder) != before:
                raise ValueError("権限比較の初期コピーが違います")
        results = []
        original_modes = {folder: stat.S_IMODE(os.lstat(folder / selected).st_mode) for folder in folders}
        try:
            for folder in folders:
                (folder / selected).chmod(mode)
            for folder, command in zip(folders, (legacy_command(["--project-root", "."]),
                    [str(binary), "resolve-task-dir", "--project-root", "."])):
                restricted = os.lstat(folder / selected)
                result = support.run_command(command, cwd=folder, env={**os.environ, "COLUMNS": "80"})
                after = os.lstat(folder / selected)
                if (stat.S_IFMT(restricted.st_mode), stat.S_IMODE(restricted.st_mode)) != (stat.S_IFMT(after.st_mode), stat.S_IMODE(after.st_mode)):
                    raise ValueError("権限比較中に種類か制限中の権限が変わりました")
                results.append(result)
        finally:
            for folder in folders:
                (folder / selected).chmod(original_modes[folder])
        for folder in folders:
            if support.snapshot_tree(folder) != before:
                raise ValueError("権限比較で初期のファイルが変わりました")
        old, new = results
        old_output = old.stdout.replace(os.fsencode(folders[0]), os.fsencode(folders[1]))
        if (old.timed_out or new.timed_out or old.returncode != 1 or new.returncode != 1
                or old_output != new.stdout or old.stderr != new.stderr or old.stderr):
            raise ValueError(f"権限拒否の比較が不一致です: {name}: {results}")


def permission_child_metadata_case(support, binary, parent):
    """利用者が承認した root 0400 の表示差だけを、通常の一致判定とは別に確かめる。"""
    if os.geteuid() == 0:
        raise ValueError("子の情報の権限拒否は通常利用者での実行が必須です。root では合格にできません")
    initial = parent / "permission-initial-child-metadata"
    initial.mkdir()
    state(initial, "child")
    before = support.snapshot_tree(initial)
    folders = [parent / ("permission-child-metadata-" + side) for side in ("legacy", "native")]
    for folder in folders:
        shutil.copytree(initial, folder, symlinks=True)
        if support.snapshot_tree(folder) != before:
            raise ValueError("子の情報の権限比較の初期コピーが違います")
    original_modes = {folder: stat.S_IMODE(os.lstat(folder).st_mode) for folder in folders}
    targets = [folder.resolve(strict=True) / "child" for folder in folders]
    results = []
    try:
        for folder in folders:
            folder.chmod(0o400)
            if os.listdir(folder) != ["child"]:
                raise ValueError("0400 の管理ルートの一覧を読めません")
            try:
                os.stat(folder / "child")
            except PermissionError as error:
                if error.errno != errno.EACCES or error.filename != str(folder / "child"):
                    raise ValueError("子の情報の拒否が EACCES または対象パスと一致しません") from error
            else:
                raise ValueError("0400 の管理ルートで子の情報の権限拒否を再現できません")
        commands = (legacy_command(["--project-root", str(folders[0])]),
                    [str(binary), "resolve-task-dir", "--project-root", str(folders[1])])
        for folder, command in zip(folders, commands):
            restricted = os.lstat(folder)
            # cwd には実行権限を残した親を使う。制限したルートへ chdir しない。
            result = support.run_command(command, cwd=parent, env={**os.environ, "COLUMNS": "80"})
            after = os.lstat(folder)
            if (stat.S_IFMT(restricted.st_mode), stat.S_IMODE(restricted.st_mode)) != (stat.S_IFMT(after.st_mode), stat.S_IMODE(after.st_mode)):
                raise ValueError("子の情報の比較中に種類か制限中の権限が変わりました")
            if not stat.S_ISDIR(after.st_mode) or stat.S_IMODE(after.st_mode) != 0o400:
                raise ValueError("子の情報の比較の管理ルートがディレクトリまたは 0400 ではありません")
            results.append(result)
    finally:
        for folder in folders:
            folder.chmod(original_modes[folder])
        for folder in folders:
            if support.snapshot_tree(folder) != before:
                raise ValueError("子の情報の権限比較で初期のファイルが変わりました")
    old, new = results
    errors = [str(PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))) for target in targets]
    expected_exception = ("PermissionError: " + errors[0] + "\n").encode("utf-8")
    if (old.timed_out or old.returncode != 1 or old.stdout
            or not old.stderr.startswith(b"Traceback (most recent call last):\n")
            or not old.stderr.endswith(expected_exception)):
        raise ValueError(f"旧版の子の情報の PermissionError・EACCES・対象パス・出力先が不正です: {old}")
    expected_json = (json.dumps({"error": errors[1]}, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if new.timed_out or new.returncode != 1 or new.stderr or new.stdout != expected_json:
        raise ValueError(f"Rust の子の情報の JSON・対象パス・出力先が不正です: {new}")


def check_resolve_task_dir(support):
    require_python()
    binary = support.require_native_binary().resolve()
    total = 0
    with tempfile.TemporaryDirectory(prefix="native-task-dir-") as temporary:
        parent = Path(temporary)
        for name, setup, extra, expected in cases():
            initial = parent / f"initial-{total}"
            initial.mkdir()
            # 外へのリンクは、両コピーの外に同じ実体を用意する。
            setup(initial)
            if extra[:1] == ["@root"]:
                args = ["--project-root", extra[1]]
            else:
                args = ["--project-root", ".", *extra]
            # work_dir の内部に置くと compare_runs の mkdir と衝突するので、
            # 外部リンク用の ../outside は初期コピーの外の不在を使う。
            check_case(support, binary, initial, parent / f"case-{total}", args, expected,
                       copied=name == "copied-distribution")
            total += 1
        initial = parent / "arguments"
        initial.mkdir()
        for columns in (5, 10, 40, 80, 100, "1_00", "١٠٠", "invalid", 0):
            for args, expected in parser_cases():
                check_case(support, binary, initial, parent / f"case-{total}", args, expected, columns=columns)
                total += 1
        total += non_utf8_cases(support, binary, parent)
        permission_cases(support, binary, parent)
        total += 2
        permission_child_metadata_case(support, binary, parent)
        total += 1
    return total


def main():
    try:
        total = check_resolve_task_dir(load_support())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"resolve-task-dir: 合格 ({total} 件 / Python {platform.python_version()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
