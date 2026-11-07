#!/usr/bin/env python3
"""今回ビルドした実行ファイルを、Task 2.4 の補助で検査する。"""

import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import re
import sys
import tempfile

from source_input_hash import ROOT, source_input_hash
from make_dist import no_duplicates


def load_support():
    spec = importlib.util.spec_from_file_location("support_native", ROOT / "scripts/support_native.py")
    if spec is None or spec.loader is None:
        raise ValueError("scripts/support_native.py を読み込めません")
    module = importlib.util.module_from_spec(spec)
    # dataclass を使う補助でも importlib 経由で読み込めるようにする。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check_probe(support, root=ROOT):
    binary = support.require_native_binary().resolve()
    binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    expected_hash = source_input_hash(root)
    expected_os = {"Linux": "linux", "Darwin": "macos"}.get(platform.system())
    expected_arch = {"x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(platform.machine())
    if expected_os is None or expected_arch is None:
        raise ValueError("検査対象でない OS または CPU です")
    with tempfile.TemporaryDirectory(prefix="native-probe-") as temporary:
        cwd = Path(temporary)
        (cwd / "unchanged").write_bytes(b"before\0\n")
        before = support.snapshot_tree(cwd)

        def run(args, code=0):
            result = support.run_command([str(binary), *args], cwd=cwd, timeout=60)
            if result.timed_out or result.returncode != code:
                raise ValueError(f"probe の終了状態が不正です: {args}: {result}")
            if (code == 0 and result.stderr) or (code != 0 and (result.stdout or not result.stderr)):
                raise ValueError(f"probe の出力先が不正です: {args}")
            if support.snapshot_tree(cwd) != before:
                raise ValueError("probe が作業フォルダを変更しました")
            return result.stdout

        raw = run(["probe", "--json"])
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise ValueError("probe JSON は末尾改行付きの 1 行が必要です")
        info = json.loads(raw, object_pairs_hook=no_duplicates)
        expected_keys = {"schema_version", "ok", "os", "arch", "native_version", "source_input_sha256", "rustc"}
        if not isinstance(info, dict) or set(info) != expected_keys:
            raise ValueError("probe JSON の項目が不正です")
        if (type(info["schema_version"]) is not int or info["schema_version"] != 1 or info["ok"] is not True
                or info["os"] != expected_os or info["arch"] != expected_arch
                or info["source_input_sha256"] != expected_hash):
            raise ValueError("probe の実行環境またはソース入力が一致しません")
        manifest = (Path(root) / "native/Cargo.toml").read_text(encoding="utf-8")
        version = re.search(r'^version = "([^"]+)"$', manifest, re.MULTILINE)
        if version is None or info["native_version"] != version[1]:
            raise ValueError("probe の内部の版が一致しません")
        if not isinstance(info["rustc"], str) or not re.fullmatch(r"rustc 1\.99\.1 [^\r\n]+", info["rustc"]):
            raise ValueError("probe の Rust の版が不正です")
        expected_version = f"dev-workflow {info['native_version']} (source_input_sha256: {expected_hash})\n".encode()
        if run(["--version"]) != expected_version:
            raise ValueError("--version が JSON と一致しません")
        help_text = run(["--help"])
        if not help_text.endswith(b"\n") or any(
            command not in help_text for command in (b"--version", b"--help", b"probe --json", b"probe --self-sha256")
        ):
            raise ValueError("--help の使い方が不足しています")
        if run(["probe", "--self-sha256"]) != binary_sha256.encode() + b"\n":
            raise ValueError("自己の実行ファイルの SHA256 が一致しません")
        for args in ([], ["unknown"], ["--version", "--help"], ["--help", "extra"],
                     ["probe"], ["probe", "--json", "--self-sha256"],
                     ["probe", "--json", "--json"], ["probe", "--self-sha256", "--self-sha256"],
                     ["probe", "--unknown"], ["probe", "--json", "extra"]):
            run(args, 2)
        if hashlib.sha256(binary.read_bytes()).hexdigest() != binary_sha256:
            raise ValueError("probe が実行ファイルを変更しました")
    return info


def main():
    try:
        info = check_probe(load_support())
    except Exception as error:
        # NativeBinaryError もここで失敗にする。指定なしを skip にしない。
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"probe: 合格 ({info['os']} / {info['arch']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
