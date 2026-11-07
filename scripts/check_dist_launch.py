#!/usr/bin/env python3
"""リポジトリのコピーにある配布入口を起動し、出力と自己ハッシュを検査する。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = Path("plugins/dev-workflow/bin/dev-workflow")
BINARIES = (
    "linux-x86_64/dev-workflow",
    "linux-arm64/dev-workflow",
    "macos-universal/dev-workflow",
)
COMMANDS = (("--version",), ("--help",), ("probe", "--json"), ("probe", "--self-sha256"))


def host_target():
    system, machine = platform.system(), platform.machine()
    targets = {
        ("Linux", "x86_64"): ("linux", "x86_64", BINARIES[0]),
        ("Linux", "aarch64"): ("linux", "aarch64", BINARIES[1]),
        ("Linux", "arm64"): ("linux", "aarch64", BINARIES[1]),
        ("Darwin", "x86_64"): ("macos", "x86_64", BINARIES[2]),
        ("Darwin", "arm64"): ("macos", "aarch64", BINARIES[2]),
    }
    if (system, machine) not in targets:
        raise ValueError(f"検査対象でない OS または CPU です: {system}/{machine}")
    return targets[system, machine]


def require_file(path, *, executable=False):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"リンクでない通常ファイルが必要です: {path}")
    if executable and (not path.stat().st_mode & 0o111 or not os.access(path, os.X_OK)):
        raise ValueError(f"実行権限がありません: {path}")


def read_checksums(path):
    require_file(path)
    lines = path.read_bytes().splitlines(keepends=True)
    if len(lines) != len(BINARIES):
        raise ValueError("SHA256SUMS は固定の 3 行が必要です")
    hashes = {}
    for relative, line in zip(BINARIES, lines):
        pattern = rb"([0-9a-f]{64})  " + re.escape(relative.encode("ascii")) + rb"\n"
        match = re.fullmatch(pattern, line)
        if match is None:
            raise ValueError(f"SHA256SUMS の形式が不正です: {relative}")
        hashes[relative] = match[1].decode("ascii")
    return hashes


def run(command, *, cwd, env, code=0):
    label = " ".join(map(str, command))
    try:
        result = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"コマンドを実行できません: {label}: {error}") from error
    if result.returncode != code:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"終了コードが不正です: {label}: 期待 {code}・実際 {result.returncode}: {detail}")
    return result


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"probe JSON の重複キーです: {key}")
        result[key] = value
    return result


def read_probe(raw, expected_os, expected_arch):
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ValueError("probe JSON は末尾改行付きの 1 行が必要です")
    info = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    keys = {"schema_version", "ok", "os", "arch", "native_version", "source_input_sha256", "rustc"}
    if not isinstance(info, dict) or set(info) != keys:
        raise ValueError("probe JSON の項目が不正です")
    if type(info["schema_version"]) is not int or info["schema_version"] != 1 or info["ok"] is not True:
        raise ValueError("probe JSON の schema_version または ok が不正です")
    if info["os"] != expected_os or info["arch"] != expected_arch:
        raise ValueError("probe JSON の OS または CPU が実行中の機械と一致しません")
    if not isinstance(info["source_input_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", info["source_input_sha256"]):
        raise ValueError("probe JSON の source_input_sha256 が不正です")
    for key in ("native_version", "rustc"):
        if not isinstance(info[key], str) or not info[key].strip() or any(c in info[key] for c in "\r\n\0"):
            raise ValueError(f"probe JSON の {key} が不正です")
    return info


def check_dist_launch(root=ROOT):
    root = Path(root).resolve(strict=True)
    expected_os, expected_arch, relative = host_target()
    bundle = root / BUNDLE
    launch, binary = bundle / "launch.sh", bundle / relative
    require_file(launch, executable=True)
    if binary.parent.is_symlink():
        raise ValueError(f"配布物の CPU フォルダにリンクは使えません: {binary.parent}")
    require_file(binary, executable=True)
    expected_hash = read_checksums(bundle / "SHA256SUMS")[relative]
    if hashlib.sha256(binary.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("選択した実行ファイルの SHA256 が SHA256SUMS と一致しません")

    env = dict(os.environ, DEV_WORKFLOW_ENGINE="rust")
    if expected_os == "macos":
        architectures = run(["lipo", "-archs", str(binary)], cwd=root, env=env).stdout.split()
        if not {b"x86_64", b"arm64"}.issubset(architectures):
            raise ValueError("lipo -archs に x86_64 と arm64 の両方がありません")
        run(["codesign", "--verify", "--strict", "--all-architectures", str(binary)], cwd=root, env=env)

    outputs = {}
    for args in COMMANDS:
        result = run([str(launch), *args], cwd=root, env=env)
        if result.stderr:
            raise ValueError(f"Rust 指定時の標準エラーが空ではありません: {' '.join(args)}")
        outputs[args] = result.stdout
    info = read_probe(outputs["probe", "--json"], expected_os, expected_arch)
    expected_version = f"dev-workflow {info['native_version']} (source_input_sha256: {info['source_input_sha256']})\n"
    if outputs["--version",] != expected_version.encode("utf-8"):
        raise ValueError("--version が probe JSON と一致しません")
    help_text = outputs["--help",]
    if not help_text.endswith(b"\n") or any(" ".join(args).encode() not in help_text for args in COMMANDS):
        raise ValueError("--help の使い方が不足しています")
    if outputs["probe", "--self-sha256"] != expected_hash.encode("ascii") + b"\n":
        raise ValueError("probe --self-sha256 が SHA256SUMS と一致しません")

    for engine in (None, "legacy"):
        legacy_env = dict(env)
        legacy_env.pop("DEV_WORKFLOW_ENGINE")
        if engine is not None:
            legacy_env["DEV_WORKFLOW_ENGINE"] = engine
        for args in COMMANDS:
            result = run([str(launch), *args], cwd=root, env=legacy_env, code=2)
            if result.stdout or b"ERROR [legacy-unavailable]" not in result.stderr or b"DEV_WORKFLOW_ENGINE=rust" not in result.stderr:
                raise ValueError(f"未設定・legacy の出力が不正です: {engine}: {' '.join(args)}")
    return {**info, "binary": relative, "self_sha256": expected_hash}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="検査するリポジトリのコピー")
    args = parser.parse_args(argv)
    try:
        info = check_dist_launch(args.root)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"配布入口: 合格 ({info['os']} / {info['arch']})")
    print(f"SHA256: {info['self_sha256']}  {info['binary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
