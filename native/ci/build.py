#!/usr/bin/env python3
"""ビルド・比較・Mac の結合・成果物の作成を行う CI の入口。"""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from binary_tools import check_static_elf, pack_tar, sha256, unpack_tar
from make_dist import BINARY_PATHS, make_dist, read_json, write_json
from source_input_hash import ROOT, git, source_input_hash
import s3d_ci


TOOLCHAIN = "1.99.1"
TARGETS = {
    "linux-x86_64": ("x86_64-unknown-linux-musl", "Linux", "x86_64", 62),
    "linux-arm64": ("aarch64-unknown-linux-musl", "Linux", "aarch64", 183),
    "macos-x86_64": ("x86_64-apple-darwin", "Darwin", "x86_64", None),
    "macos-arm64": ("aarch64-apple-darwin", "Darwin", "arm64", None),
}
SIGN_ID = "org.dev-workflow.native"
SOURCE_DATE_EPOCH = "0"
CI_BINARIES = ("dev-workflow", "r4b_contract", "r4r_contract", "loop_state_fixture", "r4e_contract", "implement_guard_contract", "r4g_contract")
# 検査専用入口(cargo の example)。配布物には入れない。
EXAMPLES = ("r4b_contract", "r4r_contract", "loop_state_fixture", "r4e_contract", "implement_guard_contract", "r4g_contract")
# S3D(#472)の検査専用入口。比較は build の外の別ジョブ(s3d・s3d-universal)で流す。
CI_BINARIES += ("diff_snapshot_contract",)
CI_BINARIES += ("r4a_rest_contract",)
EXAMPLES += ("diff_snapshot_contract",)
EXAMPLES += ("r4a_rest_contract",)
# R4C(#478)の検査専用入口。比較は build の外の別ジョブ(r4c・r4c-universal)で流す。
# R4C の example が取り込んだ S3D・R4A のモジュールの名前(単体検査を R4C の example の検査から外す `--skip` の値)。
ADOPT_IMPORTED_SKIPS = ("diff_snapshot::", "candidate_keys::", "r4a_rest_support::", "resolve_task_dir_definition::", "task_digest_definition::")
CI_BINARIES += ("adopt_candidate_contract",)
EXAMPLES += ("adopt_candidate_contract",)
BUILD_FILES = tuple(f"{number}/{name}" for number in (1, 2) for name in CI_BINARIES)
UNIVERSAL_FILES = CI_BINARIES
# Rust 1.99.1 の src/ci/docker/scripts/musl-toolchain.sh / musl.sh で確認。
MUSL_VERSION = "1.2.5"
# R4D-1 の比較(build の後の専用ジョブ)。検査で小さくできるように名前を付ける。
R4D1_TIMEOUT = 1800
R4D1_JOIN_TIMEOUT = 10
R4D1_TEST = "rust_matches_the_legacy_reftable_reader_on_every_input"
R4D1_SUMMARY = re.compile(
    r"test result: ok\. 1 passed; 0 failed; 0 ignored; 0 measured; [0-9]+ filtered out; "
    r"finished in [0-9]+(\.[0-9]+)?s"
)


# R4G の専用比較(check_r4g.py)の1回の上限。手元の Linux x86_64 で約11分、CI の機械では長くなる。
R4G_TIMEOUT = 3600
# Ubuntu 24.04 の AppArmor による、利用者の名前空間の制限(R4G の /proc を隠す組が使う)。
R4G_USERNS_RESTRICT = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def run(args, *, env=None, capture=False, timeout=1800, cwd=None):
    print("+ " + " ".join(map(str, args)), file=sys.stderr, flush=True)
    result = subprocess.run(
        list(map(str, args)), cwd=cwd or ROOT / "native", env=env, check=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else None,
        text=capture, timeout=timeout,
    )
    return result.stdout.strip() if capture else None


def stream_command(args, *, env, timeout, join_timeout, cwd=None):
    """標準出力を 1 行ずつ書き出しながら控える。標準エラーはそのまま流す。控えた文字列を返す。"""
    print("+ " + " ".join(map(str, args)), file=sys.stderr, flush=True)
    child = subprocess.Popen(
        list(map(str, args)), cwd=cwd or ROOT / "native", env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
    )
    lines = []

    def read():
        for raw in iter(child.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            lines.append(line)
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except (OSError, ValueError, UnicodeError):
                # 書き出せなくても、控えと読み取りは続ける(要約の行を読み落とさない)。
                pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        reader.join(join_timeout)
        if not reader.is_alive():
            child.stdout.close()
        raise subprocess.TimeoutExpired(list(map(str, args)), timeout, output="".join(lines))
    reader.join(join_timeout)
    alive = reader.is_alive()
    if not alive:
        child.stdout.close()
    output = "".join(lines)
    if code != 0:
        raise subprocess.CalledProcessError(code, list(map(str, args)), output=output)
    if alive:
        raise ValueError("比較の標準出力を読み切れませんでした(要約の行を確かめられません)")
    return output


def check_r4d1_output(output):
    lines = output.split("\n")
    if lines.count(f"test {R4D1_TEST} ... ok") != 1:
        raise ValueError(f"比較のテスト {R4D1_TEST} の ok の行がちょうど 1 行ではありません")
    summary = [line for line in lines if line.startswith("test result:")]
    if len(summary) != 1 or not R4D1_SUMMARY.fullmatch(summary[0]):
        raise ValueError("test result の行が、比較のテスト 1 件だけの合格ではありません")


def r4d1(name, work):
    """R4D-1 の旧版との比較を、quality() と同じ環境で 1 回流し、1 件の合格を確かめる。"""
    target_config(name)
    with tempfile.TemporaryDirectory(prefix="r4d1-", dir=work) as temporary:
        env = build_environment(Path(temporary), name)
        output = stream_command(
            ["cargo", "test", "--locked", "-p", "dev-workflow-cli", "--test", "diagnostic_reftable",
             "--", "--ignored", "--exact", R4D1_TEST],
            env=env, timeout=R4D1_TIMEOUT, join_timeout=R4D1_JOIN_TIMEOUT, cwd=ROOT / "native",
        )
    check_r4d1_output(output)


def workspace():
    value = os.environ.get("CARGO_TARGET_DIR")
    if value is None:
        temporary = os.environ.get("RUNNER_TEMP")
        if not temporary:
            raise ValueError("CARGO_TARGET_DIR または RUNNER_TEMP に外部の出力先を指定してください")
        value = str(Path(temporary) / "native-ci")
    path = Path(value).resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError("ビルド先はリポジトリの外にしてください")
    path.mkdir(parents=True, exist_ok=True)
    return path


def target_config(name):
    target, system, arch, machine = TARGETS[name]
    if platform.system() != system or platform.machine() != arch:
        raise ValueError(f"{name} は同じ OS・CPU の機械で実行してください")
    return target, machine


def build_environment(target_dir, name):
    env = os.environ.copy()
    # CI へ追加された設定が、native/ の記録を迂回しないようにする。
    for key in env:
        if (key in {"RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"}
                or key.startswith("CARGO_PROFILE_") or key.startswith("CARGO_BUILD_RUST")
                or (key.startswith("CARGO_TARGET_") and key != "CARGO_TARGET_DIR")):
            raise ValueError(f"ビルド設定の外部上書きは認めません: {key}")
    env.update(CARGO_TARGET_DIR=str(target_dir), SOURCE_DATE_EPOCH=SOURCE_DATE_EPOCH, TZ="UTC", LC_ALL="C")
    if name.startswith("macos-"):
        env["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
        env["SDKROOT"] = run(["xcrun", "--sdk", "macosx", "--show-sdk-path"], capture=True)
        env["ZERO_AR_DATE"] = "1"
    return env


def prepare(name):
    target, _ = target_config(name)
    run([sys.executable, "-m", "pip", "install", "-r", ROOT / "requirements-dev.txt"])
    # CI の Linux arm64 の機械には、入れかけの 1.99.1 が残っていることがある(#509)。入れる前に消す。
    # 手元の機械では、入れてある 1.99.1 を消さない。
    if name == "linux-arm64" and os.environ.get("GITHUB_ACTIONS") == "true":
        remove_installed_toolchain()
    run(["rustup", "toolchain", "install", TOOLCHAIN, "--profile", "minimal", "--component", "rustfmt,clippy"])
    run(["rustup", "target", "add", "--toolchain", TOOLCHAIN, target])


def remove_installed_toolchain():
    """入れかけの 1.99.1 が残っていると、rustup の入れ直しが残ったファイルとぶつかって失敗する。
    一覧にあれば、消す前の状態を 1 行で記録に出してから、アンインストールする。
    作業フォルダの指定(native/rust-toolchain.toml)で rustup が自動で入れ直さないよう、
    リポジトリ直下で、自動導入を止めて流す。"""
    env = os.environ.copy()
    env["RUSTUP_AUTO_INSTALL"] = "0"
    run(["rustup", "--version"], env=env, cwd=ROOT)
    listing = run(["rustup", "toolchain", "list"], env=env, cwd=ROOT, capture=True)
    names = [line.split()[0] for line in listing.splitlines() if line.split()]
    names = [name for name in names if name == TOOLCHAIN or name.startswith(TOOLCHAIN + "-")]
    if not names:
        return
    home = run(["rustup", "show", "home"], env=env, cwd=ROOT, capture=True)
    for name in names:
        print(toolchain_state(home, name), file=sys.stderr, flush=True)
    run(["rustup", "toolchain", "uninstall", TOOLCHAIN], env=env, cwd=ROOT)


def toolchain_state(home, name):
    """消す前の道具一式の状態を 1 行で返す(入れかけになる理由を後で確かめる材料。読めない項目は「読めない」と出して続ける)。"""
    base = Path(home) / "toolchains" / name
    rustlib = base / "lib" / "rustlib"

    def mtime(path):
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            return "読めない"

    def present(path):
        """あれば True、無ければ False、通れない(権限など)なら None。"""
        try:
            return path.exists()
        except OSError:
            return None

    def components():
        path = rustlib / "components"
        found = present(path)
        if found is None:
            return "読めない"
        if not found:
            return "無し"
        try:
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, UnicodeError):
            return "読めない"
        return f"{len(lines)} 行" if lines else "空"

    def config():
        path = rustlib / "multirust-config.toml"
        found = present(path)
        if found is None:
            return "読めない"
        if not found:
            return "無し"
        try:
            packages = re.findall(r'^\s*pkg\s*=\s*"([^"]*)"', path.read_text(encoding="utf-8"), re.MULTILINE)
        except (OSError, UnicodeError):
            return "読めない"
        return "あり(部品: " + (",".join(packages) or "なし") + ")"

    def exists(path):
        found = present(path)
        return "読めない" if found is None else "あり" if found else "無し"

    return (
        f"toolchain={name} components={components()} multirust-config.toml={config()} "
        f"bin/cargo-clippy={exists(base / 'bin' / 'cargo-clippy')} "
        f"update-hashes/{name}={exists(Path(home) / 'update-hashes' / name)} "
        f"更新時刻(UTC): 道具一式={mtime(base)} multirust-config.toml={mtime(rustlib / 'multirust-config.toml')} "
        f"components={mtime(rustlib / 'components')}"
    )


def prepare_r4g_linux():
    """R4G の専用比較を Linux で必須の形で流す準備(r4g・r4g-universal のジョブの始めだけ。build・reproduce・
    distribution では行わない)。strace の記録を必須にする(無ければ入れる)。/proc を隠す組
    (新しい利用者とマウントの名前空間)のため、GitHub の Ubuntu 24.04 が AppArmor で止めている利用者の名前空間を許す。"""
    if shutil.which("strace") is None:
        run(["sudo", "apt-get", "update"])
        run(["sudo", "apt-get", "install", "-y", "--no-install-recommends", "strace"])
    if R4G_USERNS_RESTRICT.exists() and R4G_USERNS_RESTRICT.read_text(encoding="ascii").strip() != "0":
        run(["sudo", "sysctl", "-w", "kernel.apparmor_restrict_unprivileged_userns=0"])


def identity():
    return {
        "schema_version": 1,
        "source_commit": git(ROOT, "rev-parse", "HEAD").decode().strip(),
        "source_input_sha256": source_input_hash(ROOT),
        "cargo_lock_sha256": sha256(ROOT / "native/Cargo.lock"),
        "rustc": run(["rustc", "--version"], capture=True),
    }


def tool_record(name, env):
    target, _ = target_config(name)
    details = {
        "target": target, "cargo": run(["cargo", "--version"], capture=True),
        "rustc_verbose": run(["rustc", "--version", "--verbose"], capture=True),
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "cargo_manifest": (ROOT / "native/Cargo.toml").read_text(encoding="utf-8"),
        "cargo_config": (ROOT / "native/.cargo/config.toml").read_text(encoding="utf-8"),
        "path_remapping": "native/.cargo/config.toml + native/.cargo/rustc-wrapper.sh",
    }
    if name.startswith("linux-"):
        # 1.99.1 の両 musl target は gnu-cc。既定の cc が選ぶ ld を記録する。
        linker_name = run(["cc", "-print-prog-name=ld"], capture=True)
        linker = shutil.which(linker_name)
        if linker is None:
            raise ValueError("cc が使うリンカーが見つかりません")
        details["linker"] = run([linker, "--version"], capture=True)
        details["linker_sha256"] = sha256(Path(linker))
        details["cc"] = run(["cc", "--version"], capture=True)
        libdir = Path(run(["rustc", "--print", "target-libdir", "--target", target], capture=True))
        libraries = sorted((libdir / "self-contained").glob("*"))
        if not libraries:
            raise ValueError("Rust に付属する musl の入力が見つかりません")
        details["musl"] = {
            "version": MUSL_VERSION, "provider": "rust-std " + TOOLCHAIN,
            "source": "https://github.com/rust-lang/rust/tree/1.99.1/src/ci/docker/scripts",
            "files": {p.name: sha256(p) for p in libraries if p.is_file()},
        }
        details["sdk"] = "Rust の musl 向け標準ライブラリに付属"
        details["linking"] = "Rust の既定の self-contained 静的リンク。C の追加依存なし"
    else:
        details["linker"] = run(["xcrun", "ld", "-version_details"], capture=True)
        details["clang"] = run(["xcrun", "clang", "--version"], capture=True)
        details["xcode"] = run(["xcodebuild", "-version"], capture=True)
        details["sdk"] = run(["xcrun", "--sdk", "macosx", "--show-sdk-version"], capture=True)
        details["sdk_build"] = run(["xcrun", "--sdk", "macosx", "--show-sdk-build-version"], capture=True)
        details["deployment_target"] = env["MACOSX_DEPLOYMENT_TARGET"]
        details["zero_ar_date"] = env["ZERO_AR_DATE"]
    return details


def quality(name, work):
    with tempfile.TemporaryDirectory(prefix="quality-", dir=work) as temporary:
        env = build_environment(Path(temporary), name)
        run(["cargo", "fmt", "--check"], env=env)
        run(["cargo", "clippy", "--all-targets", "--locked", "--", "-D", "warnings"], env=env)
        run(["cargo", "test", "--locked"], env=env)
        run(["cargo", "test", "--locked", "-p", "dev-workflow-cli", "--example", "r4b_contract"], env=env)
        run(["cargo", "test", "--locked", "-p", "dev-workflow-cli", "--example", "r4r_contract"], env=env)
        run(["cargo", "test", "--locked", "-p", "dev-workflow-cli", "--example", "r4g_contract"], env=env)
        run(["cargo", "test", "--locked", "-p", "dev-workflow-cli", "--example", "r4e_contract"], env=env)
        run(["cargo", "test", "--locked", "-p", "dev-workflow-cli", "--example", "implement_guard_contract"], env=env)
        # R4A 残り 5 本の専用 example の単体検査(専用比較は、新しい必須ジョブ r4a-rest・r4a-rest-universal で流す)。
        run(["cargo", "test", "--locked", "-p", "dev-workflow-cli", "--example", "r4a_rest_contract"], env=env)
        run(["cargo", "test", "--locked", "-p", "dev-workflow-platform", "--example", "r4a_rest_platform"], env=env)
        r4r_map(env, Path(temporary))
        # 同じソースの各OS品質工程で一度。release各回はCLI比較と自己検査を行う。
        run([sys.executable, "-B", ROOT / "native/ci/check_loop_state_units.py"], env=env)
        run([sys.executable, "-B", ROOT / "native/ci/check_loop_state_mutations.py"], env=env)
        # S3D の example の単体検査。全体の比較は s3d・s3d-universal のジョブで流す。
        # Linux だけで流す(12:59 の決定の CI の時間。Mac は brew が要るので、s3d ジョブの型検査の記録で兼ねる)。
        if name.startswith("linux-"):
            s3d_ci.unit_test(name, work, env, run=run)
        # 取り込んだ S3D のモジュールの単体検査(diff_snapshot::)は s3d の検査に、取り込んだ R4A のモジュールの単体検査
        # (candidate_keys・r4a_rest_support・resolve_task_dir_definition・task_digest_definition。`ADOPT_IMPORTED_SKIPS`)は
        # R4A の example の検査に残し、R4C の example の単体検査からは外す。
        run(["cargo", "test", "--locked", "-p", "dev-workflow-cli", "--example", "adopt_candidate_contract", "--",
             *[argument for module in ADOPT_IMPORTED_SKIPS for argument in ("--skip", module)]], env=env)


def r4r_map(env, folder):
    """R4R の対応表(旧テスト 46 件 → Rust の単体テスト)を、この機械で流す一覧と照らす。"""
    test = ["cargo", "test", "--locked", "-p", "dev-workflow-cli", "--example", "r4r_contract", "--"]
    listed = folder / "r4r-list.txt"
    ignored = folder / "r4r-ignored.txt"
    listed.write_text(run([*test, "--list"], env=env, capture=True) + "\n", encoding="utf-8")
    ignored.write_text(run([*test, "--list", "--ignored"], env=env, capture=True) + "\n", encoding="utf-8")
    run([sys.executable, "-B", ROOT / "native/ci/check_r4r.py", "--map-list", listed, "--map-ignored", ignored])


def reproduce(name, work):
    target, machine = target_config(name)
    before = identity()
    if not before["rustc"].startswith("rustc " + TOOLCHAIN + " "):
        raise ValueError(f"Rust {TOOLCHAIN} が必要です")
    stage = Path(tempfile.mkdtemp(prefix=name + "-", dir=work))
    tools_before = None
    for number in (1, 2):
        # Cargo の出力先は必ず空にする。成果物の取り出し後にその出力先を消す。
        with tempfile.TemporaryDirectory(prefix=f"build-{number}-", dir=work) as temporary:
            env = build_environment(Path(temporary), name)
            details = tool_record(name, env)
            if tools_before is not None and details != tools_before:
                raise ValueError("2 回のビルドで道具の記録が変わりました")
            tools_before = details
            run(["cargo", "build", "--release", "--locked", "-p", "dev-workflow-cli", "--bin", "dev-workflow",
                 *[item for name in EXAMPLES for item in ("--example", name)], "--target", target], env=env)
            (stage / str(number)).mkdir()
            for filename in CI_BINARIES:
                source = Path(temporary) / target / "release" / (f"examples/{filename}" if filename in EXAMPLES else filename)
                destination = stage / str(number) / filename
                shutil.copyfile(source, destination)
                destination.chmod(0o755)
                if machine is not None:
                    check_static_elf(destination, machine)
    if machine is not None:
        for filename in CI_BINARIES:
            run(["cmp", stage / "1" / filename, stage / "2" / filename])
    if identity() != before:
        raise ValueError("ビルド中にソース・依存・Rust の記録が変わりました")
    record = {**before, "targets": {target: tools_before}, "files": {
        relative: sha256(stage / relative) for relative in BUILD_FILES
    }}
    write_json(stage / "record.json", record)
    return stage


def probe(binary):
    env = {**os.environ, "DEV_WORKFLOW_TEST_NATIVE_BINARY": str(binary)}
    run([sys.executable, "-B", ROOT / "native/ci/check_probe.py"], env=env)
    run([sys.executable, "-B", ROOT / "native/ci/check_resolve_task_dir.py"], env=env)


def r4b_probe(fixture):
    run([sys.executable, "-B", ROOT / "native/ci/check_r4b.py", "--fixture", fixture])


def r4r_probe(fixture):
    run([sys.executable, "-B", ROOT / "native/ci/check_r4r.py", "--fixture", fixture])


def r4g_probe(fixture):
    """R4G の専用比較。Linux は strace の記録と、/proc を隠す組(利用者の名前空間)を必須にする(--require-strace・
    --require-userns)。既存テストが一時フォルダの下に作る
    AF_UNIX のソケットのパスは Mac で103バイトまでなので、短い一時フォルダを TMPDIR に渡す。"""
    linux = platform.system() == "Linux"
    with tempfile.TemporaryDirectory(prefix="r4g", dir="/tmp") as temporary:
        env = {**os.environ, "TMPDIR": temporary}
        run([sys.executable, "-B", ROOT / "native/ci/check_r4g.py", "--fixture", fixture,
             *(["--require-strace", "--require-userns"] if linux else [])], env=env, timeout=R4G_TIMEOUT)


def loop_state_probe(fixture):
    run([sys.executable, "-B", ROOT / "native/ci/check_loop_state.py", "--fixture", fixture])
    run([sys.executable, "-B", ROOT / "native/ci/test_loop_state.py", "--fixture", fixture])


def r4a_rest_probe(fixture, selftest=True):
    """R4A 残り 5 本の専用比較。旧版は、この Python(CI の setup-python の 3.10.11)で動かす(`$R4A_REST_PYTHON`)。
    接続待ちが 1 件でも残れば失敗にする(`--strict-pending`)。未実施は、固定の一覧(Linux 以外の OS で確かめられない case)の
    とおりのときだけ終了 0 で、成功には数えず件数を出す。一覧と違う未実施・cc が無い・差込みの効きの期待違いは失敗(0 以外)。"""
    python = sys.executable
    env = {**os.environ, "R4A_REST_PYTHON": python}
    command = [python, "-B", ROOT / "native/ci/check_r4a_rest.py", "--fixture", fixture, "--strict-pending"]
    if not selftest:
        command.append("--no-selftest")  # 比較器の自己検査は、ジョブの中で 1 回だけ流す(2 回目の比較では省く)
    run(command, env=env)


def r4a_rest_build(name, downloads, work):
    """build の後の機種ごとの必須ジョブ: 同じ機種の 2 回のビルド(`1/`・`2/`)の両方の専用 example を比べる。"""
    target_config(name)  # 同じ OS・CPU の機械でだけ流す
    folder, _ = extract_build(name, downloads, work)
    for number in (1, 2):
        r4a_rest_probe(folder / str(number) / "r4a_rest_contract", selftest=number == 1)


R4A_REST_UNIVERSAL_PLATFORMS = ("macos-x86_64", "macos-arm64")


def r4a_rest_universal(name, downloads, work):
    """combine の後の Mac 2 機種の必須ジョブ: 署名後の Mac 共通の実行物を、各 CPU で比べる(署名し直さない)。"""
    if name not in R4A_REST_UNIVERSAL_PLATFORMS:
        raise ValueError("r4a-rest-universal は Mac の 2 機種で実行してください")
    target_config(name)  # 同じ OS・CPU の機械でだけ流す
    folder, _ = extract_universal(downloads, work)
    fixture = folder / "r4a_rest_contract"
    run(["codesign", "--verify", "--strict", "--verbose=2", fixture])
    if sorted(run(["lipo", "-archs", fixture], capture=True).split()) != ["arm64", "x86_64"]:
        raise ValueError("Mac の最終ファイルの CPU が違います")
    r4a_rest_probe(fixture)


def adopt_probe(fixture, diff_snapshot_fixture, env=None, *, require_userns=False):
    # --evidence: 機種で変わる旧版の診断の生出力を、この機種のログへ残す(固定期待は証拠を見てから作る)。
    # --diff-snapshot-fixture: 同じ成果物の diff_snapshot_contract(argv[0] で入る子のモードの基準)。
    # env: 旧版の事前検査(bash と GNU の道具)を動かす PATH(S3D と同じ道具の準備の後)。
    # --require-userns: Linux の CI では、/proc を隠す組(利用者の名前空間)を作れなければ、未実施にせず失敗にする
    # (R4G と同じ。prepare_r4c が AppArmor の制限を外すので、作れる前提)。手元とほかの機械は未実施のまま。
    command = [sys.executable, "-B", ROOT / "native/ci/check_adopt_candidate.py", "--fixture", fixture,
               "--diff-snapshot-fixture", diff_snapshot_fixture, "--evidence"]
    if require_userns:
        command.append("--require-userns")
    run(command, env=env)


def read_record(folder):
    record = read_json(folder / "record.json")
    current = identity()
    if any(record.get(key) != value for key, value in current.items()):
        raise ValueError("転送された成果物のビルド元が一致しません")
    expected = set(UNIVERSAL_FILES if record.get("signing") else BUILD_FILES)
    actual = {str(p.relative_to(folder)) for p in folder.rglob("*") if not p.is_dir()}
    if actual != expected | {"record.json"} or set(record["files"]) != expected:
        raise ValueError("転送された実行ファイルの一覧が一致しません")
    for relative, digest in record["files"].items():
        if sha256(folder / relative) != digest or (folder / relative).stat().st_mode & 0o777 != 0o755:
            raise ValueError("転送された実行ファイルのハッシュまたは権限が違います")
    return record


def extract_build(name, downloads, work):
    parent = Path(tempfile.mkdtemp(prefix="received-", dir=work))
    folder = unpack_tar(downloads / (name + ".tar"), parent / name, [*BUILD_FILES, "record.json"])
    return folder, read_record(folder)


def sign(path):
    run(["codesign", "--force", "--sign", "-", "--identifier", SIGN_ID, "--timestamp=none", path])
    run(["codesign", "--verify", "--strict", "--verbose=2", path])
    arches = run(["lipo", "-archs", path], capture=True).split()
    if sorted(arches) != ["arm64", "x86_64"]:
        raise ValueError("Mac の最終ファイルに 2 種類の CPU がありません")


def combine(downloads, work):
    left, left_info = extract_build("macos-x86_64", downloads, work)
    right, right_info = extract_build("macos-arm64", downloads, work)
    stage = Path(tempfile.mkdtemp(prefix="universal-", dir=work))
    outputs = {}
    for filename in CI_BINARIES:
        files=[]
        for number in (1, 2):
            output = stage / str(number) / filename
            output.parent.mkdir(exist_ok=True)
            run(["lipo", "-create", left / str(number) / filename, right / str(number) / filename, "-output", output])
            output.chmod(0o755)
            sign(output)
            files.append(output)
        run(["cmp", *files])
        outputs[filename]=files[0]
    record = {**identity(), "targets": {**left_info["targets"], **right_info["targets"]},
              "signing": {"identifier": SIGN_ID, "timestamp": "none", "identity": "ad-hoc",
                          "codesign_sha256": sha256(Path(shutil.which("codesign"))),
                          "macos": run(["sw_vers"], capture=True),
                          "xcode": run(["xcodebuild", "-version"], capture=True)},
              "files": {name: sha256(path) for name,path in outputs.items()}}
    for name,path in outputs.items():
        shutil.copy2(path, stage / name)
    write_json(stage / "record.json", record)
    pack_tar(stage, work / "macos-universal.tar", [*UNIVERSAL_FILES, "record.json"])


def extract_universal(downloads, work):
    parent = Path(tempfile.mkdtemp(prefix="received-macos-", dir=work))
    folder = unpack_tar(downloads / "macos-universal.tar", parent / "bundle", [*UNIVERSAL_FILES, "record.json"])
    return folder, read_record(folder)


def verify_universal(downloads, work):
    folder, _ = extract_universal(downloads, work)
    for filename in CI_BINARIES:
        run(["codesign", "--verify", "--strict", "--verbose=2", folder / filename])
        if sorted(run(["lipo", "-archs", folder / filename], capture=True).split()) != ["arm64", "x86_64"]:
            raise ValueError("Mac の最終ファイルの CPU が違います")
    # 署名後の同じ4実行物を、各CPUの必須ジョブで検査する。
    probe(folder / "dev-workflow")
    r4b_probe(folder / "r4b_contract")
    r4r_probe(folder / "r4r_contract")
    loop_state_probe(folder / "loop_state_fixture")


# S3D(#472)の比較は、build と verify-universal の時間の枠に入らないので、別のジョブで流す。
S3D_UNIVERSAL_PLATFORMS = ("macos-x86_64", "macos-arm64")


def s3d(name, downloads, work):
    """build の tar を受け取り、型検査の記録を作ってから、2 回のビルドの diff_snapshot_contract のそれぞれで全体を比較する。"""
    target_config(name)
    s3d_ci.prepare_tools(name)
    folder, _ = extract_build(name, downloads, work)
    report = s3d_ci.typed_report(name, work, root=ROOT, build_environment=build_environment, run=run)
    for number in (1, 2):
        s3d_ci.probe(folder / str(number) / "diff_snapshot_contract", report, work, root=ROOT)


def s3d_universal(name, downloads, work):
    """署名後の Mac の tar を受け取り、署名と CPU を確かめてから、この機械の CPU で全体を比較する。"""
    if name not in S3D_UNIVERSAL_PLATFORMS:
        raise ValueError("s3d-universal は Mac の 2 機種で実行してください")
    target_config(name)
    s3d_ci.prepare_tools(name)
    folder, _ = extract_universal(downloads, work)
    fixture = folder / "diff_snapshot_contract"
    run(["codesign", "--verify", "--strict", "--verbose=2", fixture])
    if sorted(run(["lipo", "-archs", fixture], capture=True).split()) != ["arm64", "x86_64"]:
        raise ValueError("Mac の最終ファイルの CPU が違います")
    report = s3d_ci.typed_report(name, work, root=ROOT, build_environment=build_environment, run=run)
    s3d_ci.probe(fixture, report, work, root=ROOT)


def prepare_r4c(name, work):
    """R4C の比較の準備。旧 adopt は Git があるケースで旧 diff-snapshot.sh を bash で動かすので、S3D と同じ道具の準備
    (Mac は brew の bash・GNU の道具・git-lfs)と GNU の道具だけの PATH を使う。Linux は、/proc を隠す組
    (利用者の名前空間)を GitHub の Ubuntu 24.04 の AppArmor の制限から外す。返す環境は比較器へ渡す。"""
    s3d_ci.prepare_tools(name)
    if name.startswith("linux-") and R4G_USERNS_RESTRICT.exists() and R4G_USERNS_RESTRICT.read_text(encoding="ascii").strip() != "0":
        run(["sudo", "sysctl", "-w", "kernel.apparmor_restrict_unprivileged_userns=0"])
    return s3d_ci.environment(work)


def r4c(platform, downloads, work):
    """R4C の比較。build の 2 回分の実行物を、build とは別のジョブで検査する(CI の時間を分けるため)。"""
    env = prepare_r4c(platform, work)
    folder, _ = extract_build(platform, downloads, work)
    for number in (1, 2):
        adopt_probe(folder / f"{number}/adopt_candidate_contract", folder / f"{number}/diff_snapshot_contract", env,
                    require_userns=platform.startswith("linux-"))


def r4c_universal(platform, downloads, work):
    """R4C の比較。署名後の Mac 共通の実行物を、各 CPU のジョブで検査する。"""
    env = prepare_r4c(platform, work)
    folder, _ = extract_universal(downloads, work)
    adopt_probe(folder / "adopt_candidate_contract", folder / "diff_snapshot_contract", env)


def package(downloads, work):
    binaries, targets = [], {}
    for name in ("linux-x86_64", "linux-arm64"):
        folder, record = extract_build(name, downloads, work)
        for filename in CI_BINARIES:
            run(["cmp", folder / "1" / filename, folder / "2" / filename])
            check_static_elf(folder / "1" / filename, TARGETS[name][3])
        binaries.append(folder / "1/dev-workflow")
        targets.update(record["targets"])
    folder, record = extract_universal(downloads, work)
    binaries.append(folder / "dev-workflow")
    targets.update(record["targets"])
    metadata = {**identity(), "targets": targets, "signing": record["signing"], "ci": {
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_url": f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
    }}
    parent = Path(tempfile.mkdtemp(prefix="distribution-", dir=work))
    bundle = make_dist(binaries, metadata, parent / "bundle")
    pack_tar(bundle, work / "native-dist.tar", [*BINARY_PATHS, "SHA256SUMS", "build-info.json"])


# R4U(#480)の比較は、build と verify-universal の時間の枠に入れず、別のジョブ r4u で流す(2026-11-12 12:59 の
# 決定)。build の成果物は受け取らず、ジョブの中で検査用の実行物(cargo test の check_links)を作る。
R4U_LEGACY_PYTHON = "3.10.11"
R4U_COMMAND = ("cargo", "test", "--locked", "-p", "dev-workflow-cli", "--test", "check_links", "--", "--r4u-compare")


def r4u(name, work):
    """Rust 1.99.1 と本家 3.10.11 の Python を確かめ、その Python を R4U_PYTHON に渡して R4U の比較を流す。"""
    target_config(name)
    rustc = run(["rustc", "--version"], capture=True)
    if not rustc.startswith("rustc " + TOOLCHAIN + " "):
        raise ValueError(f"R4U の比較は Rust {TOOLCHAIN} が必要です: {rustc}")
    implementation, version = platform.python_implementation(), platform.python_version()
    if implementation != "CPython" or version != R4U_LEGACY_PYTHON:
        raise ValueError(f"R4U の旧版用の Python は CPython {R4U_LEGACY_PYTHON} が必要です: {implementation} {version}")
    with tempfile.TemporaryDirectory(prefix="r4u-", dir=work) as temporary:
        env = build_environment(Path(temporary), name)
        env["R4U_PYTHON"] = sys.executable
        run(list(R4U_COMMAND), env=env)


# R4G(#479)の比較は、build と verify-universal の時間の枠に入らないので、別のジョブ(r4g・r4g-universal)で流す
# (2026-11-12 12:59 の決定)。build に残すのは、r4g_contract のビルド(tar に入れる)と example の単体検査だけ。
R4G_UNIVERSAL_PLATFORMS = ("macos-x86_64", "macos-arm64")


def r4g_prepare(name):
    """機械を確かめ、Linux では strace と利用者の名前空間を用意する(比較器は Linux で両方を必須にする)。"""
    target_config(name)
    if name.startswith("linux-"):
        prepare_r4g_linux()


def r4g(name, downloads, work):
    """build の tar を受け取り、2 回のビルドの r4g_contract のそれぞれで比較する。"""
    r4g_prepare(name)
    folder, _ = extract_build(name, downloads, work)
    for number in (1, 2):
        r4g_probe(folder / str(number) / "r4g_contract")


def r4g_universal(name, downloads, work):
    """署名後の Mac の tar を受け取り、署名と CPU を確かめてから、r4g_contract で比較する。"""
    if name not in R4G_UNIVERSAL_PLATFORMS:
        raise ValueError("r4g-universal は Mac の 2 機種で実行してください")
    r4g_prepare(name)
    folder, _ = extract_universal(downloads, work)
    fixture = folder / "r4g_contract"
    run(["codesign", "--verify", "--strict", "--verbose=2", fixture])
    if sorted(run(["lipo", "-archs", fixture], capture=True).split()) != ["arm64", "x86_64"]:
        raise ValueError("Mac の最終ファイルの CPU が違います")
    r4g_probe(fixture)


# R4E(#482)の比較は、build と verify-universal の時間の枠に入らないので、別のジョブで流す。
R4E_LEGACY_PYTHON = "3.10.11"
R4E_UNIVERSAL_PLATFORMS = ("macos-x86_64", "macos-arm64")


def r4e_strace(work):
    """Linux では strace を入れ、/bin/true の起動を記録できることを最初に確かめる。

    比較器も Linux では strace を必須にする(外部の道具を起動しないことの確かめ)。
    """
    run(["sudo", "apt-get", "update"])
    run(["sudo", "apt-get", "install", "-y", "strace"])
    folder = Path(tempfile.mkdtemp(prefix="r4e-strace-", dir=work))
    log = folder / "probe.txt"
    run(["strace", "-f", "-e", "trace=execve", "-o", log, "/bin/true"])
    if sum(b"execve(" in line for line in log.read_bytes().splitlines()) != 1:
        raise ValueError("strace の確かめで、/bin/true の execve が 1 行ではありません")


def r4e_legacy_venv(work):
    """旧版用の venv を、実行中の CPython 3.10.11 とハッシュつきの依存の一覧から作る。"""
    implementation, version = platform.python_implementation(), platform.python_version()
    if implementation != "CPython" or version != R4E_LEGACY_PYTHON:
        raise ValueError(f"R4E の旧版用の Python は CPython {R4E_LEGACY_PYTHON} が必要です: {implementation} {version}")
    folder = Path(tempfile.mkdtemp(prefix="r4e-venv-", dir=work))
    run([sys.executable, "-m", "venv", folder])
    python = folder / "bin/python"
    run([python, "-m", "pip", "install", "--no-deps", "--require-hashes",
         "-r", ROOT / "native/ci/r4e-requirements.txt"])
    return python


def r4e_prepare(name, work):
    """機械を確かめ、Linux では strace を確かめてから、旧版用の venv を作る。"""
    target_config(name)
    if name.startswith("linux-"):
        r4e_strace(work)
    return r4e_legacy_venv(work)


def r4e_probe(fixture, python, work):
    """check_r4e.py の比較を必須で流す。最後の行(合格: <件数> 件…)は CI の記録に残る。"""
    temporary = Path(tempfile.mkdtemp(prefix="r4e-compare-", dir=work))
    run([sys.executable, "-B", ROOT / "native/ci/check_r4e.py", "--fixture", fixture,
         "--legacy-python", python, "--temp-parent", temporary])


def r4e(name, downloads, work):
    """build の tar を受け取り、2 回のビルドの r4e_contract のそれぞれで比較する。"""
    python = r4e_prepare(name, work)
    folder, _ = extract_build(name, downloads, work)
    for number in (1, 2):
        r4e_probe(folder / str(number) / "r4e_contract", python, work)


def r4e_universal(name, downloads, work):
    """署名後の Mac の tar を受け取り、署名と CPU を確かめてから、r4e_contract で比較する。"""
    if name not in R4E_UNIVERSAL_PLATFORMS:
        raise ValueError("r4e-universal は Mac の 2 機種で実行してください")
    python = r4e_prepare(name, work)
    folder, _ = extract_universal(downloads, work)
    fixture = folder / "r4e_contract"
    run(["codesign", "--verify", "--strict", "--verbose=2", fixture])
    if sorted(run(["lipo", "-archs", fixture], capture=True).split()) != ["arm64", "x86_64"]:
        raise ValueError("Mac の最終ファイルの CPU が違います")
    r4e_probe(fixture, python, work)


# S3I-A(#474)の比較も、build と verify-universal の時間の枠に入らないので、別のジョブで流す。
IMPLEMENT_GUARD_UNIVERSAL_PLATFORMS = ("macos-x86_64", "macos-arm64")


def implement_guard_probe(fixture, scope=None):
    """check_implement_guard.py の比較を必須で流す。結果の行は CI の記録に残る。"""
    args = [sys.executable, "-B", ROOT / "native/ci/check_implement_guard.py", "--fixture", fixture]
    if scope is not None:
        args += ["--scope", scope]
    run(args)


def implement_guard(name, downloads, work):
    """build の tar を受け取り、1 本目の implement_guard_contract で比較する(2 本が同じことは reproduce が確かめる)。

    strace は入れない(実行機に無いか ptrace が使えなければ、比較器がその確かめを未実施と記録する)。
    """
    target_config(name)
    folder, _ = extract_build(name, downloads, work)
    implement_guard_probe(folder / "1" / "implement_guard_contract")


def implement_guard_universal(name, downloads, work):
    """署名後の Mac の tar を受け取り、署名と CPU を確かめてから、範囲 signed で比較する。"""
    if name not in IMPLEMENT_GUARD_UNIVERSAL_PLATFORMS:
        raise ValueError("implement-guard-universal は Mac の 2 機種で実行してください")
    target_config(name)
    folder, _ = extract_universal(downloads, work)
    fixture = folder / "implement_guard_contract"
    run(["codesign", "--verify", "--strict", "--verbose=2", fixture])
    if sorted(run(["lipo", "-archs", fixture], capture=True).split()) != ["arm64", "x86_64"]:
        raise ValueError("Mac の最終ファイルの CPU が違います")
    implement_guard_probe(fixture, "signed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "build", "reproduce", "combine", "verify-universal", "package", "test", "r4u", "r4d1", "r4e", "r4e-universal", "implement-guard", "implement-guard-universal", "r4g", "r4g-universal", "s3d", "s3d-universal", "r4a-rest", "r4a-rest-universal", "r4c", "r4c-universal"))
    parser.add_argument("--platform", choices=TARGETS)
    parser.add_argument("--downloads", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "test":
            suite = unittest.defaultTestLoader.discover(str(ROOT / "native/ci/scripts"), pattern="test_native_ci.py")
            suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / "native/ci/scripts"), pattern="test_s3d_ci.py"))
            return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        if args.command in ("prepare", "build", "reproduce", "r4u", "r4d1", "r4e", "r4e-universal", "implement-guard", "implement-guard-universal", "r4g", "r4g-universal", "r4a-rest", "r4a-rest-universal", "r4c", "r4c-universal") and not args.platform:
            parser.error("--platform が必要です")
        if args.command in ("s3d", "s3d-universal") and not args.platform:
            parser.error("--platform が必要です")
        if args.command == "prepare":
            prepare(args.platform)
            return 0
        work = workspace()
        downloads = args.downloads or work / "downloads"
        if args.command in ("build", "reproduce"):
            if args.command == "build":
                quality(args.platform, work)
            stage = reproduce(args.platform, work)
            for number in (1, 2):
                probe(stage / f"{number}/dev-workflow")
                r4b_probe(stage / f"{number}/r4b_contract")
                r4r_probe(stage / f"{number}/r4r_contract")
                loop_state_probe(stage / f"{number}/loop_state_fixture")
            pack_tar(stage, work / (args.platform + ".tar"), [*BUILD_FILES, "record.json"])
            print(f"2 回のビルド: {stage}")
        elif args.command == "r4a-rest":
            r4a_rest_build(args.platform, downloads, work)
        elif args.command == "r4a-rest-universal":
            r4a_rest_universal(args.platform, downloads, work)
        elif args.command == "combine":
            combine(downloads, work)
        elif args.command == "verify-universal":
            verify_universal(downloads, work)
        elif args.command == "package":
            package(downloads, work)
        elif args.command == "r4u":
            r4u(args.platform, work)
        elif args.command == "r4d1":
            r4d1(args.platform, work)
        elif args.command == "r4e":
            r4e(args.platform, downloads, work)
        elif args.command == "r4e-universal":
            r4e_universal(args.platform, downloads, work)
        elif args.command == "implement-guard":
            implement_guard(args.platform, downloads, work)
        elif args.command == "implement-guard-universal":
            implement_guard_universal(args.platform, downloads, work)
        elif args.command == "r4g":
            r4g(args.platform, downloads, work)
        elif args.command == "r4g-universal":
            r4g_universal(args.platform, downloads, work)
        elif args.command == "s3d":
            s3d(args.platform, downloads, work)
        elif args.command == "s3d-universal":
            s3d_universal(args.platform, downloads, work)
        elif args.command == "r4c":
            r4c(args.platform, downloads, work)
        elif args.command == "r4c-universal":
            r4c_universal(args.platform, downloads, work)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
