#!/usr/bin/env python3
"""3 本の検証済み実行ファイルから、入口を含まない成果物を作る。"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
BINARY_PATHS = (
    "linux-x86_64/dev-workflow", "linux-arm64/dev-workflow",
    "macos-universal/dev-workflow",
)


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON のキーが重複しています: {key}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(path).chmod(0o644)


def load_bundle_checker():
    # sys.path 上の同名ファイルではなく、リポジトリ直下の検査を使う。
    spec = importlib.util.spec_from_file_location("native_bundle_validate", ROOT / "scripts/validate.py")
    if spec is None or spec.loader is None:
        raise ValueError("scripts/validate.py を読み込めません")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.check_native_bundle


def make_dist(binaries, metadata, output):
    """binaries は BINARY_PATHS 順の Path 3 個。成功したフォルダだけを公開する。"""
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("出力先は未作成のフォルダを指定してください")
    if len(binaries) != len(BINARY_PATHS):
        raise ValueError("実行ファイルは 3 本必要です")
    if not isinstance(metadata, dict) or "artifacts" in metadata:
        raise ValueError("ビルド情報は object とし、artifacts は材料から生成します")
    checker = load_bundle_checker()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-dist-", dir=output.parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        artifacts = []
        for source, relative in zip(binaries, BINARY_PATHS):
            source = Path(source)
            mode = source.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o755:
                raise ValueError(f"通常ファイル・権限 0755 が必要です: {source}")
            if not 0 < source.stat().st_size <= 8 * 1024 * 1024:
                raise ValueError(f"実行ファイルの容量が不正です: {source}")
            destination = stage / relative
            destination.parent.mkdir()
            shutil.copyfile(source, destination)
            destination.chmod(0o755)
            artifacts.append({"path": relative, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
        (stage / "SHA256SUMS").write_text(
            "".join(f"{item['sha256']}  {item['path']}\n" for item in artifacts), encoding="utf-8",
        )
        (stage / "SHA256SUMS").chmod(0o644)
        write_json(stage / "build-info.json", {**metadata, "artifacts": artifacts})
        problems = checker(stage, "artifact")
        if problems:
            raise ValueError("成果物の検査が失敗しました:\n" + "\n".join(problems))
        stage.rename(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux-x86_64", required=True, type=Path)
    parser.add_argument("--linux-arm64", required=True, type=Path)
    parser.add_argument("--macos-universal", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        make_dist([args.linux_x86_64, args.linux_arm64, args.macos_universal], read_json(args.metadata), args.output)
    except (OSError, ValueError, AttributeError, ImportError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
