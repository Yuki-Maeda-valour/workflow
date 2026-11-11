#!/usr/bin/env python3
"""ビルド・比較・Mac の結合・成果物の作成を行う CI の入口。"""

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

from binary_tools import check_static_elf, pack_tar, sha256, unpack_tar
from make_dist import BINARY_PATHS, make_dist, read_json, write_json
from source_input_hash import ROOT, git, source_input_hash


TOOLCHAIN = "1.99.1"
TARGETS = {
    "linux-x86_64": ("x86_64-unknown-linux-musl", "Linux", "x86_64", 62),
    "linux-arm64": ("aarch64-unknown-linux-musl", "Linux", "aarch64", 183),
    "macos-x86_64": ("x86_64-apple-darwin", "Darwin", "x86_64", None),
    "macos-arm64": ("aarch64-apple-darwin", "Darwin", "arm64", None),
}
SIGN_ID = "org.dev-workflow.native"
SOURCE_DATE_EPOCH = "0"
CI_BINARIES = ("dev-workflow", "r4b_contract", "r4r_contract")
# 検査専用入口(cargo の example)。配布物には入れない。
EXAMPLES = ("r4b_contract", "r4r_contract")
BUILD_FILES = tuple(f"{number}/{name}" for number in (1, 2) for name in CI_BINARIES)
UNIVERSAL_FILES = CI_BINARIES
# Rust 1.99.1 の src/ci/docker/scripts/musl-toolchain.sh / musl.sh で確認。
MUSL_VERSION = "1.2.5"


def run(args, *, env=None, capture=False):
    print("+ " + " ".join(map(str, args)), file=sys.stderr, flush=True)
    result = subprocess.run(
        list(map(str, args)), cwd=ROOT / "native", env=env, check=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else None,
        text=capture, timeout=1800,
    )
    return result.stdout.strip() if capture else None


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
    run(["rustup", "toolchain", "install", TOOLCHAIN, "--profile", "minimal", "--component", "rustfmt,clippy"])
    run(["rustup", "target", "add", "--toolchain", TOOLCHAIN, target])


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
        r4r_map(env, Path(temporary))


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
    # 署名後の同じ3実行物を、各CPUの必須ジョブで検査する。
    probe(folder / "dev-workflow")
    r4b_probe(folder / "r4b_contract")
    r4r_probe(folder / "r4r_contract")


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "build", "reproduce", "combine", "verify-universal", "package", "test"))
    parser.add_argument("--platform", choices=TARGETS)
    parser.add_argument("--downloads", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "test":
            suite = unittest.defaultTestLoader.discover(str(ROOT / "native/ci/scripts"), pattern="test_native_ci.py")
            return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        if args.command in ("prepare", "build", "reproduce") and not args.platform:
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
            pack_tar(stage, work / (args.platform + ".tar"), [*BUILD_FILES, "record.json"])
            print(f"2 回のビルド: {stage}")
        elif args.command == "combine":
            combine(downloads, work)
        elif args.command == "verify-universal":
            verify_universal(downloads, work)
        elif args.command == "package":
            package(downloads, work)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
