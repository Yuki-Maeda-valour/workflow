#!/usr/bin/env python3
"""内蔵の実装担当が、承認済みフィルターを無効化して Git を読む入口。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

VERSION = 1
PRECHECK_TIMEOUT = 120
GIT_TIMEOUT = 30
SAFE_GIT = (
    "--no-pager", "--no-replace-objects", "-c", "core.quotePath=false",
    "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
    "-c", "core.ignoreCase=false", "-c", "core.splitIndex=false",
    "-c", "core.ignoreStat=false", "-c", "commit.gpgSign=false",
    "-c", "push.gpgSign=false", "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.clean=", "-c", "filter.lfs.process=",
    "-c", "filter.lfs.required=false",
)
ITEMS = ("clean", "smudge", "process", "required")
PATHSPEC_ENV = ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS",
                "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS")
FORBIDDEN_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
EXIT_HELP = ("終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、"
             "承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。")


class Failure(Exception):
    def __init__(self, message, code=20):
        super().__init__(message)
        self.message = message
        self.code = code


def fail(message, code=20):
    raise Failure(message, code)


def strict_text(value, label, code=2):
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        fail("ERROR [input] %s は厳密な UTF-8 ではない" % label, code)
    if "\0" in value:
        fail("ERROR [input] %s に NUL がある" % label, code)
    return value


def physical_cwd(value):
    strict_text(value, "cwd")
    if not value:
        fail("ERROR [input] cwd が空", 2)
    try:
        path = Path(value)
        if not path.is_dir():
            fail("ERROR [input] cwd は存在するディレクトリでなければならない", 2)
        return strict_text(str(path.resolve()), "物理 cwd")
    except (OSError, RuntimeError, ValueError) as exc:
        fail("ERROR [input] cwd を解決できない: %s" % exc, 2)


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.update(allow_abbrev=False, add_help=False)
        kwargs.setdefault("epilog", EXIT_HELP)
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置引数"
        self._optionals.title = "オプション"
        self.add_argument("-h", "--help", action="help", help="使い方を表示して終了")

    def error(self, message):
        fail("ERROR [input] 引数が不正: %s" % message, 2)

    def format_help(self):
        return super().format_help().replace("usage: ", "使い方: ", 1)


class Once(argparse.Action):
    """= 形式を含め、同じ単一値オプションを上書きさせない。"""
    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_seen_options", None)
        if seen is None:
            seen = set()
            setattr(namespace, "_seen_options", seen)
        if self.dest in seen:
            fail("ERROR [input] オプションが重複: %s" % option_string, 2)
        seen.add(self.dest)
        setattr(namespace, self.dest, True if self.nargs == 0 else values)


def option(parser, name, **kwargs):
    parser.add_argument(name, action=Once, **kwargs)


def parse_input(argv):
    for value in argv:
        strict_text(value, "引数")
    parser = Parser(description="承認済みフィルターを無効化する、5操作専用の Git 読取り入口。")
    modes = parser.add_subparsers(dest="mode", title="入口")
    prepare = modes.add_parser("prepare", help="委託直前の確認と承認情報の JSON 出力")
    run = modes.add_parser("run", help="承認情報を照合して指定した操作を実行")
    for entry in (prepare, run):
        option(entry, "--cwd", required=True, help="本体リポジトリのルート")
        option(entry, "--accept", help="最後に利用者が承認した一覧の値")
    option(run, "--expect-context-sha256", required=True, help="委託元から受け取った64桁の照合値")
    operations = run.add_subparsers(dest="operation", title="読取り操作")
    status = operations.add_parser("status", help="変更したファイルを表示")
    option(status, "--format", choices=("short", "porcelain"), default="short", help="表示形式（既定: short）")
    option(status, "--untracked", choices=("normal", "all", "no"), default="normal", help="未追跡の表示（既定: normal）")
    diff = operations.add_parser("diff", help="作業ツリー・index・履歴の差分を表示")
    option(diff, "--cached", nargs=0, default=False, help="index と比較")
    option(diff, "--base", help="比較元の commit")
    option(diff, "--target", help="比較先の commit（--base が必要）")
    option(diff, "--format", choices=("patch", "stat", "name-only", "name-status"), default="patch", help="差分の形式（既定: patch）")
    option(diff, "--context", type=int, default=3, help="前後の行数 0〜100（既定: 3）")
    log = operations.add_parser("log", help="履歴を表示（既定: HEAD の先頭20件）")
    option(log, "--from", dest="from_rev", help="表示から除く側の commit")
    option(log, "--to", dest="to_rev", default="HEAD", help="履歴をたどる起点（既定: HEAD）")
    option(log, "--limit", type=int, default=20, help="表示件数 1〜1000（既定: 20）")
    option(log, "--format", choices=("oneline", "fuller"), default="oneline", help="履歴の形式（既定: oneline）")
    show = operations.add_parser("show", help="1 commit またはその1ファイルを表示")
    option(show, "--rev", default="HEAD", help="表示する commit（既定: HEAD）")
    option(show, "--format", choices=("patch", "stat", "name-only"), help="表示形式（既定: patch。--file と併用不可）")
    option(show, "--file", help="commit 内の単一ファイル。リンクは辿らない")
    files = operations.add_parser("ls-files", help="記録済みか未記録のファイルを表示")
    option(files, "--mode", dest="file_mode", choices=("tracked", "untracked"), default="tracked", help="対象（既定: tracked。untracked は ignore 済みを除く）")
    for entry in (status, diff, log, files):
        entry.add_argument("--path", action="append", default=[], help="ルート相対のリテラルパス。反復可能")
    args = parser.parse_args(argv)
    if args.mode is None or (args.mode == "run" and args.operation is None):
        fail("ERROR [input] prepare または run と読取り操作を指定する", 2)
    args.cwd = physical_cwd(args.cwd)
    if args.mode == "prepare":
        return args
    if not re.fullmatch(r"[a-f0-9]{64}", args.expect_context_sha256):
        fail("ERROR [input] expect-context-sha256 は小文字16進64桁が必要", 2)
    if args.operation == "diff":
        if not 0 <= args.context <= 100 or (args.cached and args.target is not None) or (args.target is not None and args.base is None):
            fail("ERROR [input] diff の範囲または組合せが不正", 2)
    if args.operation == "log" and not 1 <= args.limit <= 1000:
        fail("ERROR [input] limit は1〜1000が必要", 2)
    if args.operation == "show" and args.file is not None and args.format is not None:
        fail("ERROR [input] show --file と --format は併用できない", 2)
    for name in ("base", "target", "from_rev", "to_rev", "rev"):
        value = getattr(args, name, None)
        if value is not None:
            validate_revision(value)
    for path in getattr(args, "path", []):
        validate_path(path)
    if getattr(args, "file", None) is not None:
        validate_path(args.file)
    return args


def validate_revision(value):
    strict_text(value, "revision")
    if not 1 <= len(value) <= 1024 or value.startswith("-") or "\n" in value or "\r" in value:
        fail("ERROR [input] revision が不正", 2)


def validate_path(value):
    strict_text(value, "path")
    if not value or os.path.isabs(value) or ".." in value.split("/"):
        fail("ERROR [input] path はルートから外れない相対パスが必要", 2)


def clean_env():
    env = dict(os.environ)
    for key in list(env):
        if key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS") or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del env[key]
    for key in PATHSPEC_ENV:
        env.pop(key, None)
    for key in FORBIDDEN_ENV:
        if env.get(key):
            fail("ERROR [input] 対象を変更する環境変数が設定されている: %s" % key, 2)
        env.pop(key, None)
    env.update(GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    return env


def run_git(cwd, env, *args):
    """Git の唯一の起動点。呼出元が用意した同じ辞書を必ず渡す。"""
    try:
        return subprocess.run(
            ["git", "-C", cwd, *SAFE_GIT, *args], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        fail("ERROR [git] Git を実行できない: %s" % exc)


def precheck(cwd, env, accept):
    script = Path(__file__).with_name("diff-snapshot.sh")
    if not script.is_file() or not os.access(str(script), os.R_OK):
        fail("ERROR [requirements] diff-snapshot.sh を読み込めない")
    command = ["bash", str(script), "--cwd", cwd, "--precheck"]
    if accept is not None:
        command.extend(["--accept", accept])
    try:
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=PRECHECK_TIMEOUT)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        fail("ERROR [precheck] 事前検査を実行できない: %s" % exc)
    sys.stderr.buffer.write(result.stderr)
    if result.returncode:
        code = result.returncode if result.returncode in (2, 20, 22) else 20
        fail("ERROR [precheck] 事前検査の終了コード %s" % result.returncode, code)
    return result.stdout


def parse_filters(raw):
    """意味上の辞書と、Git に渡す検証済みの原バイトを別々に保持する。"""
    if not raw:
        return {}, []
    if not raw.endswith(b"\0"):
        fail("ERROR [precheck] 出力が NUL 終端でない")
    parsed = {}
    for record in raw[:-1].split(b"\0"):
        try:
            record.decode("utf-8", "strict")
        except UnicodeError:
            fail("ERROR [precheck] 出力が厳密な UTF-8 でない")
        key, separator, value = record.partition(b"=")
        if not separator or not re.fullmatch(rb"GIT_CONFIG_(COUNT|KEY_(0|[1-9][0-9]*)|VALUE_(0|[1-9][0-9]*))", key):
            fail("ERROR [precheck] 設定変数名が不正")
        if key in parsed:
            fail("ERROR [precheck] 設定変数が重複")
        parsed[key] = value
    count_text = parsed.pop(b"GIT_CONFIG_COUNT", None)
    # COUNT の巨大な値から配列を作らず、実際のレコード数との一致を先に確かめる。
    if count_text is None or not re.fullmatch(rb"[0-9]+", count_text):
        fail("ERROR [precheck] COUNT が不正")
    count = len(parsed) // 2
    if len(parsed) % 2 or count_text.lstrip(b"0") != str(count).encode("ascii").lstrip(b"0"):
        fail("ERROR [precheck] COUNT と KEY/VALUE の数が一致しない")
    semantic, pairs, names = {}, [], set()
    for index in range(count):
        key = parsed.get(("GIT_CONFIG_KEY_%d" % index).encode("ascii"))
        value = parsed.get(("GIT_CONFIG_VALUE_%d" % index).encode("ascii"))
        if key is None or value is None:
            fail("ERROR [precheck] KEY/VALUE の添字が連続していない")
        key_text, value_text = key.decode("utf-8"), value.decode("utf-8")
        match = re.fullmatch(r"filter\.(.+)\.(clean|smudge|process|required)", key_text, re.DOTALL)
        if not match:
            fail("ERROR [precheck] 許可する filter 設定ではない")
        name, item = match.groups()
        if key_text in semantic:
            fail("ERROR [precheck] 意味上の設定キーが重複")
        if value_text != ("false" if item == "required" else ""):
            fail("ERROR [precheck] filter の無効化値が不正")
        names.add(name)
        semantic[key_text] = value_text
        pairs.append((key, value))
    for name in names:
        if any("filter.%s.%s" % (name, item) not in semantic for item in ITEMS):
            fail("ERROR [precheck] filter の4項目がそろっていない")
    return semantic, pairs


def context(cwd, accept, filters):
    value = {"version": VERSION, "cwd": cwd, "accept": accept, "filters": filters}
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def apply_filters(env, pairs):
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for index, (key, value) in enumerate(pairs):
        env["GIT_CONFIG_KEY_%d" % index] = key
        env["GIT_CONFIG_VALUE_%d" % index] = value


def checked_git(cwd, env, *args):
    result = run_git(cwd, env, *args)
    sys.stderr.buffer.write(result.stderr)
    if result.returncode:
        fail("ERROR [git] 内部確認に失敗（終了コード %s）" % result.returncode)
    return result.stdout


def verify(cwd, env, filters):
    for key, wanted in filters.items():
        raw = checked_git(cwd, env, "config", "--get", key)
        if raw != wanted.encode("utf-8") + b"\n":
            fail("ERROR [git] filter の実効値が無効化値と一致しない")
    raw = checked_git(cwd, env, "rev-parse", "--show-toplevel")
    if not raw.endswith(b"\n"):
        fail("ERROR [git] リポジトリのルート出力に終端改行がない")
    try:
        root = raw[:-1].decode("utf-8", "strict")
        if not os.path.isabs(root) or not Path(root).is_dir() or str(Path(root).resolve()) != cwd:
            fail("ERROR [git] cwd がリポジトリの物理ルートと一致しない")
    except (UnicodeError, OSError, ValueError, RuntimeError):
        fail("ERROR [git] リポジトリのルート出力が不正")


def revision(cwd, env, value):
    raw = checked_git(cwd, env, "rev-parse", "--verify", "--end-of-options", value + "^{commit}")
    if not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\n", raw):
        fail("ERROR [git] revision の解決結果が commit ID ではない")
    return raw[:-1].decode("ascii")


def operation(args, env):
    cwd, op = args.cwd, args.operation
    diff_options = ["--no-ext-diff", "--no-textconv", "--submodule=short", "--no-color"]
    log_options = ["--no-show-signature", "--no-decorate", "--no-color"]
    if op == "status":
        command = ["status", "--short" if args.format == "short" else "--porcelain=v1",
                   "--untracked-files=" + args.untracked, "--ignore-submodules=dirty"]
    elif op == "diff":
        command = ["diff", *diff_options, "--ignore-submodules=dirty",
                   "--unified=%d" % args.context, "--" + args.format]
        if args.cached:
            command.append("--cached")
        if args.base is not None:
            command.append(revision(cwd, env, args.base))
        if args.target is not None:
            command.append(revision(cwd, env, args.target))
    elif op == "log":
        command = ["log", *log_options, "--no-patch", "--max-count=%d" % args.limit,
                   "--format=" + args.format]
        target = revision(cwd, env, args.to_rev)
        command.append(revision(cwd, env, args.from_rev) + ".." + target if args.from_rev is not None else target)
    elif op == "show":
        rev = revision(cwd, env, args.rev)
        command = ["show", *diff_options, *log_options]
        if args.file is not None:
            obj = rev + ":" + args.file
            if checked_git(cwd, env, "cat-file", "-t", obj) != b"blob\n":
                fail("ERROR [git] 指定した file は blob ではない")
            command.append(obj)
        else:
            command.extend(["--" + (args.format or "patch"), rev])
    else:  # parse_input が受理する残りの操作は ls-files だけ。
        command = ["ls-files", "--cached"] if args.file_mode == "tracked" else ["ls-files", "--others", "--exclude-standard"]
    if op in ("status", "diff", "log", "ls-files"):
        command.extend(["--"] + [":(literal)" + path for path in args.path])
    result = run_git(cwd, env, *command)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    if result.returncode:
        fail("ERROR [git] 読取りに失敗（終了コード %s）。部分的な stdout を成功した観察に使わない" % result.returncode)


def main(argv):
    args = parse_input(argv)
    env = clean_env()
    filters, pairs = parse_filters(precheck(args.cwd, env, args.accept))
    digest = context(args.cwd, args.accept, filters)
    if args.mode == "run" and args.expect_context_sha256 != digest:
        fail("ERROR [context] 承認情報または設定が一致しない。委託元へ戻す", 22)
    apply_filters(env, pairs)
    verify(args.cwd, env, filters)
    if args.mode == "prepare":
        print(json.dumps({"version": VERSION, "cwd": args.cwd, "accept": args.accept,
                          "context_sha256": digest}, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    else:
        operation(args, env)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Failure as exc:
        print(exc.message, file=sys.stderr)
        raise SystemExit(exc.code)
    except (OSError, ValueError, UnicodeError, RuntimeError) as exc:
        print("ERROR [runtime] 実行に失敗: %s" % exc, file=sys.stderr)
        raise SystemExit(20)
