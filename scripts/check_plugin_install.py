#!/usr/bin/env python3
"""専用の設定先へ実 plugin を導入し、一致を確かめた導入先だけを起動する。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from check_dist_launch import check_plugin_launch, no_duplicates

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "dev-workflow@valour-workflow"


def run_cli(cli, args, *, root, env):
    try:
        result = subprocess.run([cli, "plugin", *args], cwd=root, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"plugin CLI を実行できません: {args[0]}: {error}") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"plugin CLI が失敗しました: {' '.join(args)}: {result.returncode}: {detail}")
    return result.stdout


def select_install(raw, *, config, source):
    listing = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    if not isinstance(listing, list) or any(not isinstance(item, dict) for item in listing):
        raise ValueError("plugin 一覧はオブジェクトの配列が必要です")
    matches = [item for item in listing if item.get("id") == PLUGIN_ID]
    if len(matches) != 1:
        raise ValueError("対象 plugin は一覧に 1 件だけ必要です")
    item = matches[0]
    if item.get("scope") != "user" or item.get("enabled") is not True:
        raise ValueError("導入した plugin は user scope で有効である必要があります")
    raw_path = item.get("installPath")
    if not isinstance(raw_path, str) or not raw_path.strip() or "\0" in raw_path:
        raise ValueError("installPath は空でないパスが必要です")
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError("installPath は絶対パスが必要です")
    installed = path.resolve(strict=True)
    if not installed.is_dir() or installed == source or source in installed.parents or installed in source.parents:
        raise ValueError("導入先が元ソースを指すか、フォルダではありません")
    if config not in installed.parents:
        raise ValueError("導入先が専用設定の外を指しています")
    return installed


def tree_manifest(root):
    """リンク・特殊ファイルを拒否し、内容と全権限を比べる。"""
    manifest = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISREG(info.st_mode):
            manifest[relative] = ("file", stat.S_IMODE(info.st_mode), info.st_size,
                                  hashlib.sha256(path.read_bytes()).hexdigest())
        elif stat.S_ISDIR(info.st_mode):
            manifest[relative] = ("directory", stat.S_IMODE(info.st_mode))
        else:
            raise ValueError(f"導入物にはリンク・特殊ファイルを使えません: {relative}")
    return manifest


def check_plugin_install(root=ROOT):
    root = Path(root).resolve(strict=True)
    source = root / "plugins/dev-workflow"
    if not source.is_dir():
        raise ValueError("導入元の plugin がありません")
    cli = shutil.which("claude")
    if cli is None:
        raise ValueError("実導入の検査に claude CLI が必要です")
    source_manifest = tree_manifest(source)
    with tempfile.TemporaryDirectory(prefix="workflow-plugin-install-") as temporary:
        config = Path(temporary).resolve() / "config"
        config.mkdir(mode=0o700)
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config))
        # HOME・CODEX_HOME はそのまま。CLI の専用設定先だけを変更する。
        run_cli(cli, ["marketplace", "add", str(root)], root=root, env=env)
        run_cli(cli, ["install", PLUGIN_ID, "--scope", "user"], root=root, env=env)
        listing = run_cli(cli, ["list", "--json"], root=root, env=env)
        installed = select_install(listing, config=config, source=source.resolve())
        installed_manifest = tree_manifest(installed)
        if tree_manifest(source) != source_manifest:
            raise ValueError("導入中に元ソースが変わりました")
        if installed_manifest != source_manifest:
            names = sorted(set(source_manifest) | set(installed_manifest))
            changed = [name for name in names if source_manifest.get(name) != installed_manifest.get(name)]
            raise ValueError("導入物の内容・権限・ハッシュが一致しません: " + ", ".join(changed[:10]))
        # 比較が済むまで、一覧が示した導入先のファイルを実行しない。
        info = check_plugin_launch(installed)
        if tree_manifest(installed) != installed_manifest or tree_manifest(source) != source_manifest:
            raise ValueError("起動検査中に導入物か元ソースが変わりました")
        return {**info, "plugin_id": PLUGIN_ID, "files": len(installed_manifest)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="検証用 commit を持つリポジトリ")
    args = parser.parse_args(argv)
    try:
        info = check_plugin_install(args.root)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"plugin 導入: 合格 ({info['plugin_id']} / {info['os']} / {info['arch']})")
    print(f"導入物: {info['files']} 項目一致 / 保存先解決: {info['resolve_cases']} 件 / 試しの 4 コマンド合格")
    print(f"SHA256: {info['self_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
