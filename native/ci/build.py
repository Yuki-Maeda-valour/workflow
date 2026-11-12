#!/usr/bin/env python3
"""ビルド・比較・Mac の結合・成果物の作成を行う CI の入口。"""

import argparse
import json
import os
from pathlib import Path
import platform
import re
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


# R4Q(#494)の比較も、build と verify-universal の時間の枠に入れず、別のジョブ r4q で流す(2026-11-12 12:59 の
# 決定)。build の成果物は受け取らず、ジョブの中で検査用の実行物(cargo test の r4q)を作る。Linux は配布と同じ
# musl の対象で作る。
R4Q_LEGACY_PYTHON = "3.10.11"
R4Q_COMMAND = ("cargo", "test", "--locked", "-p", "dev-workflow-cli", "--test", "r4q")
R4Q_COMPARE = ("--", "--r4q-compare")
# 比較の全体(組み立てと比較器と自己検査)の上限。ジョブ r4q の上限(仮に 60 分)の中に収める。
R4Q_TIMEOUT = 3300
# 旧版の動きに関わる標準ライブラリの .py の固定の一覧と、本家 3.10.11 の SHA-256(設計の「全コマンド共通の決定の
# 当て方」の 22:26 の項)。比較器(native/ci/check_r4q.py)もこの表を読む(1 か所にだけ置く)。
# - 一覧の決め方: 本家のソース(Python-3.10.11.tgz)から作った 3.10.11 で、旧版の 2 本(--help・監督・anchor・
#   引数の誤り)と、importlib で読んだ check-process.py の run_command(linux・posix)を -X importtime で流し、
#   読み込まれた標準ライブラリの .py を全部集めた(起動の site・_sitebuiltins を含む)。json.loads(bytes) の
#   文字コードの判定で読む encodings の utf_8_sig・utf_16・utf_32 を足した。外すのは、機械ごとに作られる
#   ファイル(_sysconfigdata_* など)と site-packages だけ(.py でない組み込み・frozen・拡張モジュールと、
#   ファイルの無い sitecustomize などは、そもそも一覧の対象にならない)。
# - SHA-256 は同じ 3.10.11 の lib/python3.10 で計り、ソースの Lib/ と同じ中身であることも確かめた。
R4Q_STDLIB_SHA256 = {
    "_collections_abc.py": "48560945f0f48a364ceff93de6e4cd2e2779b5effce505e70258506be4c67dba",
    "_compression.py": "3ad5d60627477a60939ee44fc1bb3a05dbe8fb52f0f75039b8f5d8f1a278b981",
    "_sitebuiltins.py": "b9388bc1d6d12ed6be12da420ab1feca40f99c0e33ec315d92b1e01cb69b25bc",
    "_weakrefset.py": "49fb432325ef836f89caa60e50b152eb6f32b700ed71fedcd4d56815723f77ba",
    "abc.py": "c702f783a7e81f9c37a1d22095c05360b11916d144d2730ff735c36f879523c5",
    "argparse.py": "a238b700513237e10d26056e42c5b4e42d4961ea70e15ebeef4c61c76615f004",
    "base64.py": "108aaee81d7b9d1ceeb5d64d299ad8b9f78b6358c14a34d29ea11389f676cc91",
    "bisect.py": "e5b2ff166f48a06e70ae831d8c9b47283fcd0c254306eee12d3dae9c55e11526",
    "bz2.py": "76ab3252924e71e859d7d90e8d3db13b6554975cfcac0fdadced4de7f8779330",
    "codecs.py": "166ffd0f0fe40631bb7adef898ebfe59c7fb9efeb25f635b0fd50b68e9353b6a",
    "collections/__init__.py": "5d7b9d899fb1407a614cb2ade46b6d2400ba797634e9fed7d770fc4b7e372760",
    "collections/abc.py": "9cb4208f99128a0489b6c8e6c61637617dd7d4250c59e065491957eda084dd10",
    "contextlib.py": "2e17d45cf3da698c9752c58a668c8a03d0ce2280fdffd6e352794a38a94d9f07",
    "copyreg.py": "65cc0a30e07e9768ff97c78391985459a237083dce0c9086abfb879dc517420b",
    "ctypes/__init__.py": "9b99fa34cb2c454490337a62a9d91f5828c1a4f1ac22d94d94fffea34a2d612e",
    "ctypes/_endian.py": "4c9944875236d4227e8fd80ca0439417870ef387a532403393da91bf7ff67e16",
    "encodings/__init__.py": "05f07fc2a0fbd9a9cc92aa7567df84f343ebbb17fc676432e108e127d81913df",
    "encodings/aliases.py": "6fdcc49ba23a0203ae6cf28e608f8e6297d7c4d77d52e651db3cb49b9564c6d2",
    "encodings/utf_16.py": "6c36257f7b8d214473560d195e71bccef0c69a53e1e52d2800b7a7890aad7e58",
    "encodings/utf_32.py": "2072eece5f6026ad2d3549ab193a9e38894ea15ca9d5b3cd408fd6b116acc0c2",
    "encodings/utf_8.py": "ba0cac060269583523ca9506473a755203037c57d466a11aa89a30a5f6756f3d",
    "encodings/utf_8_sig.py": "1ef3da8d8aa08149e7f274dc64dbfce2155da812e5258ca8e8f832428d3b5c2d",
    "enum.py": "59a62af0e129ae688f5549d6a273c51c5db80b534521f98dae2e4ceb485d9f75",
    "fnmatch.py": "8a47ad23c243c6af0314bca4fc59b077035b6074f1915b6fc2b7c00864a7c16d",
    "functools.py": "1604a69d2d60921502b375a8af0d662702abe625987b773637a34c09c76a8a25",
    "genericpath.py": "9d58ad64056a89f12258a62596c2073cd9e52b459dde152f710edc0cffcd865b",
    "gettext.py": "46c4d4e2e72bcd572e64bfbce15975d8054a65af167664c6da193773c38dd1ae",
    "hashlib.py": "9d56ebad0263a48ac94e0b2b7a3723625f2ff4dc7707d1156580e008d40bc790",
    "hmac.py": "a4790b3cfd1b58875879621dbe8f4c0a30fb50a8582045b0d5c1a712afc8c36b",
    "importlib/__init__.py": "49ad4fdc8139026f7f3773e3f50f09207ff6bd8e92a2c382545525235448e525",
    "importlib/_abc.py": "e24fa90513d1fd6e10df30dc28044dfcad857b88161c79de10f7109c18227e8d",
    "importlib/machinery.py": "b7b47efe3d95ae817e0c61d852682ddf8b8ce95aaf36ae4cf333e145416baf18",
    "importlib/util.py": "de645b9f6d595f5e415d117f4d04ce77f144ce5ad2a6477659a9b5547d54b9dd",
    "io.py": "ee094fcf87d17a3a25816c663b67bd8797dccc3eebabad5a23f6da162146a0a8",
    "json/__init__.py": "d5d41e2c29049515d295d81a6d40b4890fbec8d8482cfb401630f8ef2f77e4d5",
    "json/decoder.py": "9f02654649816145bc76f8c210a5fe3ba1de142d4d97a1c93105732e747c285b",
    "json/encoder.py": "06b881b824f71e95d72af4ab865de4c35553e791b6d959a125caac61401cc350",
    "json/scanner.py": "8604d9d03786d0d509abb49e9f069337278ea988c244069ae8ca2c89acc2cb08",
    "keyword.py": "afbe73afb68d32fa998e5ff3d081090deec457152470f5331cc2bd430a0e9d2a",
    "locale.py": "5f7e28c368751d372719e305a41ca76862f341b1de5d3163db7b2e3452848881",
    "lzma.py": "58fb9d2fdc8a8af7b25e218f17ea3b51bdfa53bdf40f440ab33c605974ca5c2e",
    "ntpath.py": "ad0799b417b2d0246869768bea784702c6838be1f2151ed99fa8132327f66387",
    "operator.py": "fd931e2bf493ddd239208b0c0a2249324602b48aabc984c85b7d8dcc4d99a17d",
    "os.py": "70e420e105d021d5ba2ec4d8a9b2131561db6d93e800e8580ddeb86cbd6959c3",
    "pathlib.py": "936f14a3ecc4ac5e896e4d673666d76b25a9d581f3b4c8d8d424e830fda2a69b",
    "posixpath.py": "2c770bac0680a5a180b1310102d14e1e120f2f2e8e47925b1574ec808cb8ad6d",
    "random.py": "ce80a2471965e64ae93caea14490f16850432e65dce87f15edfe25b6f562f8d1",
    "re.py": "018b1a2c70e8ef537cc328d1026afdeca5839392ce47235a7b8d22e1b788cdc7",
    "reprlib.py": "dc786fbc528e10bc6ea3c1fa84e4178e85c4f3c9b937a4b191546aec317e9cb9",
    "secrets.py": "695195792d0f738bbdcd22ab0493e787d7a167adb1ac453b2b400ce4ebf66842",
    "selectors.py": "b16e673c110fd6a362414308ef73ffc3c15125c87361333b309791735931040d",
    "shutil.py": "4cab312042bfca2f6ce26d7240b6762c64169f602b7d8c6629f601a956b43f1b",
    "signal.py": "6ae9e49fe09b607bc161fc80067de9763aa48f523567a34992f00be06057f00a",
    "site.py": "a92fc1de9082615bf4f3987e0be7b2bdaafe67aa6be4f59859a114485b7a0d09",
    "sre_compile.py": "800f4c7df096e10336cd54f1aa6382721d6fcb974669463fe67f7a9189e52407",
    "sre_constants.py": "6d5f1ff40e42f4be9b98899b58684dffcdb9c0ef1b3b942c17a3fcafc0bbcdc1",
    "sre_parse.py": "dad10892e970f3b9d0ad874d893edde31c0303e39ea5e2678935129e29128c2f",
    "stat.py": "052af0327eae6941b69b05c088b3e748f79995635f80ac4cc7125eb333eb4c77",
    "struct.py": "9c231f9497caf513a22dee8f790b07f969b0e45854a0bdd6dd84b492e08c2856",
    "subprocess.py": "53bb0d0780e166ef4ae94f3b2a817a8aae49a84d899db1f4c31f933d429057d6",
    "textwrap.py": "e1541a31ac906294f915cadd0d780e1e5b256dc1897b560cdaf3fbf46d104cf0",
    "threading.py": "a21926e636bec8c2e5579f3e79cce144c36379ef93c4596252af29970983553d",
    "types.py": "5e6c3b3380332249908d7907bc4c1102c0bf14076248c8244d03314f6b2e2078",
    "urllib/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "urllib/parse.py": "1f840c8962b0cef795c67759bd80fe988bfb360e8841da66cb0929048514edad",
    "warnings.py": "b8e7748e4c67eb65cea5d3b3bd888d245771c0ebe63117f747189a55a9b622db",
}
# R4Q_PYTHON の Python の中で動かす調べ(標準ライブラリだけ。引数は調べるファイルの一覧の JSON)。
R4Q_PROBE = r"""
import hashlib, json, os, platform, signal, sys
base = os.path.dirname(os.__file__)
digest = {}
for name in json.loads(sys.argv[1]):
    try:
        with open(os.path.join(base, name), "rb") as stream:
            digest[name] = hashlib.sha256(stream.read()).hexdigest()
    except OSError:
        digest[name] = None
attributes = {"os.pidfd_open": hasattr(os, "pidfd_open"),
              "signal.pidfd_send_signal": hasattr(signal, "pidfd_send_signal")}
call = None
if all(attributes.values()):
    try:
        fd = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(fd, 0)
        finally:
            os.close(fd)
        call = "成功"
    except OSError as error:
        call = f"{type(error).__name__}: {error}"
print(json.dumps({"implementation": platform.python_implementation(), "version": platform.python_version(),
                  "executable": sys.executable, "stdlib": base, "sha256": digest, "pidfd": attributes,
                  "pidfd_call": call, "strerror": {str(number): os.strerror(number) for number in range(105, 109)}},
                 ensure_ascii=False))
"""


def r4q_python_report(python):
    """R4Q_PYTHON の Python を調べる(版・標準ライブラリの SHA-256・pidfd の有無と自分への呼出し・os.strerror)。"""
    try:
        done = subprocess.run([python, "-I", "-B", "-c", R4Q_PROBE, json.dumps(sorted(R4Q_STDLIB_SHA256))],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"R4Q の比較用の Python を起動できません({python}): {error}") from error
    if done.returncode != 0:
        raise ValueError(f"R4Q の比較用の Python を調べられません({python}): {done.stderr.decode(errors='replace')}")
    return json.loads(done.stdout.decode("utf-8"))


def r4q_mac_errno():
    """SDK の sys/errno.h の ELAST の行と SDK の版(Mac の ELAST の手がかり。合否は比較器が決める)。"""
    try:
        sdk = run(["xcrun", "--show-sdk-path"], capture=True)
        version = run(["xcrun", "--show-sdk-version"], capture=True)
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": f"xcrun を使えない: {error}"}
    header = Path(sdk) / "usr/include/sys/errno.h"
    try:
        text = header.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return {"sdk": sdk, "version": version, "header": str(header), "error": f"読めない: {error}"}
    lines = [line.strip() for line in text.splitlines() if re.search(r"\bELAST\b", line)]
    return {"sdk": sdk, "version": version, "header": str(header), "lines": lines}


def r4q_check_python(name, python, report):
    """調べた結果をログへ出し、基準と違う所の一覧を返す(空なら比べてよい)。"""
    problems = []
    print(f"R4Q の比較用の Python: {python}", flush=True)
    print(f"  実装と版: {report['implementation']} {report['version']}(実体 {report['executable']})", flush=True)
    if report["implementation"] != "CPython" or report["version"] != R4Q_LEGACY_PYTHON:
        problems.append(f"CPython {R4Q_LEGACY_PYTHON} でない({report['implementation']} {report['version']})")
    print(f"  標準ライブラリ: {report['stdlib']}", flush=True)
    different = []
    for file, digest in R4Q_STDLIB_SHA256.items():
        actual = report["sha256"].get(file)
        if actual != digest:
            different.append(file)
            state = "無い" if actual is None else f"本家 3.10.11 と違う(sha256 {actual})"
            print(f"    {file}: {state}", flush=True)
    same = len(R4Q_STDLIB_SHA256) - len(different)
    print(f"  標準ライブラリの照合: {len(R4Q_STDLIB_SHA256)} 件中 {same} 件が本家 3.10.11 と同じ", flush=True)
    if different:
        problems.append(f"標準ライブラリの {len(different)} 件が本家 3.10.11 と違う({'・'.join(different)})")
    attributes = "・".join(f"{key} {'あり' if value else '無し'}" for key, value in report["pidfd"].items())
    print(f"  pidfd: {attributes}・自分への pidfd と signal 0: {report['pidfd_call'] or '呼ばない(無い)'}",
          flush=True)
    if name.startswith("linux-") and report["pidfd_call"] != "成功":
        problems.append("Linux で os.pidfd_open・signal.pidfd_send_signal を使えない")
    if name.startswith("macos-"):
        for number, text in report["strerror"].items():
            print(f"  os.strerror({number}): {text}", flush=True)
        errno_header = r4q_mac_errno()
        if "error" in errno_header:
            print(f"  SDK の sys/errno.h: {errno_header['error']}", flush=True)
        if "version" in errno_header:
            print(f"  SDK: {errno_header['version']}({errno_header['sdk']})", flush=True)
        for line in errno_header.get("lines", []):
            print(f"  {errno_header['header']} の ELAST の行: {line}", flush=True)
        if "lines" in errno_header and not errno_header["lines"]:
            print("  sys/errno.h に ELAST の行が無い", flush=True)
    return problems


def r4q_compare(command, env):
    print("+ " + " ".join(map(str, command)), file=sys.stderr, flush=True)
    subprocess.run(list(map(str, command)), cwd=ROOT / "native", env=env, check=True, stdin=subprocess.DEVNULL,
                   timeout=R4Q_TIMEOUT)


def r4q(name, work):
    """Rust 1.99.1 と本家 3.10.11 の Python を確かめ、その Python を R4Q_PYTHON に渡して R4Q の比較を流す。

    比べる Python は、環境変数 R4Q_PYTHON(作業機は本家のソースから作った 3.10.11 の絶対パス)、無ければこの
    スクリプトを動かす Python(CI は actions/setup-python の 3.10.11)。確かめの結果は、比較の前にログへ出す。
    """
    target, _ = target_config(name)
    rustc = run(["rustc", "--version"], capture=True)
    if not rustc.startswith("rustc " + TOOLCHAIN + " "):
        raise ValueError(f"R4Q の比較は Rust {TOOLCHAIN} が必要です: {rustc}")
    python = os.environ.get("R4Q_PYTHON") or sys.executable
    if not os.path.isabs(python):
        raise ValueError(f"R4Q_PYTHON は絶対パスで渡してください: {python}")
    problems = r4q_check_python(name, python, r4q_python_report(python))
    if problems:
        raise ValueError("R4Q の比較用の Python が基準と違います: " + "・".join(problems))
    with tempfile.TemporaryDirectory(prefix="r4q-", dir=work) as temporary:
        env = build_environment(Path(temporary), name)
        env["R4Q_PYTHON"] = python
        musl = ("--target", target) if name.startswith("linux-") else ()
        r4q_compare([*R4Q_COMMAND, *musl, *R4Q_COMPARE], env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "build", "reproduce", "combine", "verify-universal", "package", "test", "r4u", "r4q"))
    parser.add_argument("--platform", choices=TARGETS)
    parser.add_argument("--downloads", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "test":
            suite = unittest.defaultTestLoader.discover(str(ROOT / "native/ci/scripts"), pattern="test_native_ci.py")
            return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        if args.command in ("prepare", "build", "reproduce", "r4u", "r4q") and not args.platform:
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
        elif args.command == "r4u":
            r4u(args.platform, work)
        elif args.command == "r4q":
            r4q(args.platform, work)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
