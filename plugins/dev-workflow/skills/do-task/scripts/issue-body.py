#!/usr/bin/env python3
"""GitHub Issue 本文を控え、競合を確かめてから書き換える。

本文の機密検査、公開作成の承認、承認済み設計の値の保持は呼出元の責務。
取得から書込までを原子的にする API ではない。競合時は再試行も自動統合もしない。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


SKILLS = Path(__file__).resolve().parents[2]
TIMEOUT = 60
REPO = re.compile(r"[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
CHECKBOX = re.compile(r"^([ \t]*)- \[([ xX])\](?=\s|$)")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[bot\])?\Z")
EDIT_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
REVIEW_LINE = re.compile(r"^- \*\*設計レビュー\*\*\(create-task・(\d{4}-\d{2}-\d{2})\): (?:APPROVED|未収束|未完了) — ")
ISSUE_QUERY = """query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number title body author { login } editor { login } lastEditedAt
      userContentEdits(first: 100, after: $cursor) {
        nodes { editor { login } editedAt }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""


class IssueError(Exception):
    pass


class ExecutionError(IssueError):
    def __init__(self, message: str, value: dict):
        super().__init__(message)
        self.value = value


def helper(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, SKILLS / relative)
    if spec is None or spec.loader is None:
        raise IssueError("必要な補助スクリプトを読み込めない。skills を一括配置してください")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raw_sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def body_digest(body: str) -> str | None:
    digest = helper("issue_task_digest", "create-task/scripts/task-digest.py")
    try:
        return digest.digest(body)
    except digest.DigestError:
        # 設計前の Issue はまだタスクの書式を持たなくても取得できる。
        return None


def resolve_repo(directory: str) -> str:
    if not Path(directory).is_dir():
        raise IssueError("--dir がディレクトリでない")
    origin = helper("issue_origin_repo", "ship-task/scripts/origin-repo.py")
    try:
        result = origin.judge(directory)
    except origin.GitFailed as exc:
        raise IssueError("origin のリポジトリを調べられない") from exc
    repo = result.get("repo")
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise IssueError("origin のリポジトリを一意に確定できない。origin の設定を確認してください")
    return repo


def gh(repo: str, *args: str, body: str | None = None) -> bytes:
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_PAGER="cat")
    if args[:2] == ("repo", "view"):
        command = ["gh", "repo", "view", repo, *args[2:]]
    elif args[0] == "issue":
        command = ["gh", *args, "-R", repo]
    elif args[0] == "api":
        command = ["gh", *args, "--hostname", repo.split("/", 1)[0]]
    else:
        raise IssueError("対象を固定できない gh コマンド")
    try:
        done = subprocess.run(
            command, input=None if body is None else body.encode("utf-8"),
            stdin=subprocess.DEVNULL if body is None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            timeout=TIMEOUT, check=False,
        )
    except FileNotFoundError as exc:
        raise IssueError("gh が見つからない。GitHub CLI を導入し、gh auth login で認証してください") from exc
    except subprocess.TimeoutExpired as exc:
        raise IssueError("gh が時間切れ。書込の有無を確認し、自動で再実行しないでください") from exc
    except OSError as exc:
        raise IssueError("gh を起動できない。GitHub CLI の導入を確認してください") from exc
    if done.returncode:
        # 外部のエラー本文は機密を含みうるため、そのまま出さない。
        reason = "認証が必要" if done.returncode == 4 else f"終了コード {done.returncode}"
        raise IssueError(f"gh が失敗した({reason})。gh auth login の認証と対象の権限を確認してください")
    return done.stdout


def json_object(raw: bytes) -> dict:
    try:
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise IssueError("JSON として読めない応答または控え") from exc
    if not isinstance(result, dict):
        raise IssueError("JSON の応答または控えがオブジェクトでない")
    return result


def permission(repo: str) -> dict:
    result = json_object(gh(repo, "repo", "view", "--json", "name,isPrivate,viewerPermission"))
    if (not isinstance(result.get("name"), str)
            or result["name"].lower() != repo.rsplit("/", 1)[1].lower()
            or type(result.get("isPrivate")) is not bool
            or not isinstance(result.get("viewerPermission"), str)
            or result.get("viewerPermission") not in {"WRITE", "MAINTAIN", "ADMIN"}):
        raise IssueError("対象の書込権限を確認できない。WRITE・MAINTAIN・ADMIN の権限が必要")
    return {"repo": repo, "isPrivate": result["isPrivate"], "viewerPermission": result["viewerPermission"]}


def fetch(repo: str, issue: int) -> dict:
    result = json_object(gh(repo, "issue", "view", str(issue), "--json", "number,title,body,author"))
    if (type(result.get("number")) is not int or result["number"] != issue
            or not isinstance(result.get("title"), str) or not isinstance(result.get("body"), str)):
        raise IssueError("Issue の番号・題名・本文を確認できない")
    author = actor_login(result.get("author"), cli_author=True)
    author_is_bot = result["author"].get("is_bot", False)
    if type(author_is_bot) is not bool:
        raise IssueError("Issue の作者の種類を確認できない")
    viewer = actor_login(json_object(gh(repo, "api", "user")))
    _, owner, name = repo.split("/")
    edits, cursors = [], set()
    cursor = None
    first_edit = None
    while True:
        extra = [] if cursor is None else ["-f", "cursor=" + cursor]
        response = json_object(gh(repo, "api", "graphql", "-f", "query=" + ISSUE_QUERY,
                                  "-f", "owner=" + owner, "-f", "name=" + name,
                                  "-F", "number=" + str(issue), *extra))
        try:
            edited = response["data"]["repository"]["issue"]
            if (response.get("errors") or not isinstance(edited, dict)
                    or type(edited.get("number")) is not int
                    or any(edited.get(key) != result[key] for key in ("number", "title", "body"))
                    or actor_login(edited.get("author")) != author.removeprefix("app/")):
                raise IssueError("Issue 本文と編集者の取得結果が一致しない。再取得して確認してください")
            last_edited_at = edited["lastEditedAt"]
            editor = None if edited["editor"] is None else actor_login(edited["editor"])
            if first_edit is None:
                first_edit = (editor, last_edited_at)
            elif first_edit != (editor, last_edited_at):
                raise IssueError("編集履歴の取得中に最終編集者か日時が変わった。再取得してください")
            history = edited["userContentEdits"]
            nodes, page = history["nodes"], history["pageInfo"]
            if not isinstance(nodes, list) or type(page["hasNextPage"]) is not bool:
                raise IssueError("Issue の編集履歴を確認できない")
            for node in nodes:
                edits.append({"editor": actor_login(node["editor"]), "edited_at": node["editedAt"]})
            if not page["hasNextPage"]:
                break
            cursor = page["endCursor"]
            if not isinstance(cursor, str) or not cursor or cursor in cursors or not nodes:
                raise IssueError("Issue の編集履歴を最後まで取得できない")
            cursors.add(cursor)
        except (KeyError, TypeError) as exc:
            raise IssueError("Issue の編集者と編集履歴を取得できない。実行せず人に確認してください") from exc
    return snapshot(repo, issue, result["title"], result["body"], author, author_is_bot,
                    editor, last_edited_at, viewer, edits)


def actor_login(actor: object, *, cli_author: bool = False) -> str:
    login = actor.get("login") if isinstance(actor, dict) else None
    checked = login
    if (cli_author and isinstance(login, str) and login.startswith("app/")
            and actor.get("is_bot") is True):
        checked = login[4:]
    if not isinstance(checked, str) or not LOGIN.fullmatch(checked):
        raise IssueError("Issue の作者・編集者または実行者を取得できない。実行せず人に確認してください")
    return login


def edit_time(value: object) -> datetime:
    if isinstance(value, str) and EDIT_TIME.fullmatch(value):
        try:
            return datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError:
            pass
    raise IssueError("Issue の編集日時を確認できない。実行せず人に確認してください")


def snapshot(repo: str, issue: int, title: str, body: str, author: str,
             author_is_bot: bool, editor: str | None, last_edited_at: str | None,
             viewer: str, edits: list[dict]) -> dict:
    if type(author_is_bot) is not bool:
        raise IssueError("Issue の作者の種類を確認できない")
    actor_login({"login": author, "is_bot": author_is_bot}, cli_author=True)
    actor_login({"login": viewer})
    if editor is not None:
        actor_login({"login": editor})
    # 未編集の Issue だけは editor と lastEditedAt がともに null になる。
    if (editor is None) != (last_edited_at is None):
        raise IssueError("Issue の最終編集者と編集日時を確認できない。実行せず人に確認してください")
    if not isinstance(edits, list):
        raise IssueError("Issue の編集履歴を確認できない")
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"editor", "edited_at"}:
            raise IssueError("Issue の編集履歴を確認できない")
        actor_login({"login": edit["editor"]})
        edit_time(edit["edited_at"])
    if last_edited_at is None:
        if edits:
            raise IssueError("未編集の Issue に編集履歴がある")
    else:
        latest = edit_time(last_edited_at)
        if (not edits or any(edit_time(e["edited_at"]) > latest for e in edits)
                or not any(edit_time(e["edited_at"]) == latest
                           and e["editor"].casefold() == editor.casefold() for e in edits)):
            raise IssueError("Issue の最終編集と編集履歴が一致しない")
    edits = sorted(edits, key=lambda e: (edit_time(e["edited_at"]), e["editor"]))
    return {
        "version": 1, "repo": repo, "issue": issue, "title": title, "body": body,
        "body_sha256": raw_sha(body), "task_digest": body_digest(body),
        "line_count": len(body.splitlines()),
        "author": author, "author_is_bot": author_is_bot,
        "editor": editor, "last_edited_at": last_edited_at, "viewer": viewer, "edits": edits,
    }


def review_since(body: str, last_edited_at: str | None) -> datetime | None:
    """レビュー行は日付だけなので、その日になり得る最も早い UTC 時刻から調べる。"""
    digest = helper("issue_review_fences", "create-task/scripts/task-digest.py")
    lines = body.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    try:
        digest.body_lines(body)
        fenced = digest._fence_flags(lines, require_closed=True, reject_unsupported_indent=True)
        in_records, review_date = False, None
        for line, in_fence in zip(lines, fenced):
            if in_fence:
                continue
            line = line.rstrip(" \t")
            if line.startswith("## "):
                in_records = line == "## 追加修正記録"
            match = REVIEW_LINE.match(line) if in_records else None
            if match:
                review_date = match[1]
        if review_date is not None and last_edited_at is not None:
            start = datetime.strptime(review_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(hours=14)
            if start <= edit_time(last_edited_at):
                return start
    except (digest.DigestError, ValueError, OverflowError):
        pass
    # 日付を確定できないときや未来の日付では、履歴を省略しない。
    return None


def check_execution(value: dict, approved_sha: str | None) -> None:
    if approved_sha is not None and approved_sha != value["body_sha256"]:
        raise IssueError("人が確認した本文と今回の本文が違う。実行せず確認を取り直してください")
    viewer = value["viewer"].casefold()
    since = review_since(value["body"], value["last_edited_at"])
    foreign_edit = any(e["editor"].casefold() != viewer
                       and (since is None or edit_time(e["edited_at"]) >= since) for e in value["edits"])
    if (value["author_is_bot"] or value["author"].casefold() != viewer
            or foreign_edit) and approved_sha is None:
        raise IssueError("Issue の作者または設計レビュー以降の編集者が実行者と違う。実行せず、取得した本文と"
                         "設計レビュー行の値を人に示して確認してください。"
                         "承認後だけ --approve-body-sha256 に確認した本文の body_sha256 を渡してください")


def read_text(path: str) -> str:
    digest = helper("issue_task_reader", "create-task/scripts/task-digest.py")
    try:
        return digest.read_path(path).decode("utf-8")
    except digest.DigestError as exc:
        raise IssueError("入力が通常ファイルでない") from exc


def read_snapshot(path: str, expected_sha: str) -> dict:
    value = json_object(read_text(path).encode("utf-8"))
    if (type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("repo"), str) or not REPO.fullmatch(value["repo"])
            or type(value.get("issue")) is not int or value["issue"] < 1
            or not isinstance(value.get("title"), str) or not isinstance(value.get("body"), str)
            or not SHA.fullmatch(expected_sha)):
        raise IssueError("本文の控えまたは期待する SHA-256 の形式が不正")
    if not {"author", "author_is_bot", "editor", "last_edited_at", "viewer", "edits"} <= value.keys():
        raise IssueError("本文の控えに作者・編集者・実行者・編集履歴がない。再取得してください")
    fresh = snapshot(value["repo"], value["issue"], value["title"], value["body"],
                     value["author"], value["author_is_bot"], value["editor"],
                     value["last_edited_at"], value["viewer"], value["edits"])
    if (value != fresh or value["body_sha256"] != expected_sha
            or type(value.get("line_count")) is not int):
        raise IssueError("本文の控えが取得時の値と一致しない")
    return value


def check_checkboxes(before: str, after: str) -> dict:
    digest = helper("issue_task_fences", "create-task/scripts/task-digest.py")
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    if len(old) != len(new):
        raise IssueError("チェック以外の変更がある(行数)")
    fence_lines = [line.rstrip("\r\n") for line in old]
    if fence_lines and fence_lines[0].startswith("\ufeff"):
        fence_lines[0] = fence_lines[0][1:]
    try:
        fenced = digest._fence_flags(
            fence_lines, require_closed=True, reject_unsupported_indent=True,
        )
    except digest.DigestError as exc:
        raise IssueError(str(exc)) from exc
    changed = 0
    complete_before = 0
    total = 0
    for line, revised, in_fence in zip(old, new, fenced):
        checkbox = None if in_fence else CHECKBOX.match(line)
        if checkbox:
            total += 1
            complete_before += checkbox.group(2) in {"x", "X"}
        if line == revised:
            continue
        if not checkbox or checkbox.group(2) != " ":
            raise IssueError("チェック以外の変更がある。許すのはフェンス外の - [ ] から - [x] だけ")
        at = checkbox.start(2)
        if revised != line[:at] + "x" + line[at + 1:]:
            raise IssueError("チェック以外の変更がある。行の本文と改行は変えられない")
        changed += 1
    return {"changed": changed, "total": total, "complete_before": complete_before,
            "complete_after": complete_before + changed}


def check_digest(before: dict, after: str, expected: str | None) -> None:
    if expected is not None and (before["task_digest"] is None
                                or before["task_digest"] != expected
                                or body_digest(after) != expected):
        raise IssueError("承認した本文の値と一致しない。設計の承認を取り直してください")


def public_security(info: dict, args: argparse.Namespace) -> None:
    if not info["isPrivate"] and args.security_task and not args.approve_public_security:
        raise IssueError("セキュリティ修正を公開 Issue に書く前の承認が必要(--approve-public-security)")


def output_available(path: str) -> None:
    if os.path.lexists(path) or not Path(path).parent.is_dir():
        raise IssueError("出力先が既にあるか、親ディレクトリがない。新しい出力先を指定してください")


def save(path: str, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def save_snapshot(path: str, value: dict) -> None:
    save(path, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))


def run(args: argparse.Namespace) -> dict:
    if args.command in {"update", "check-checkboxes"}:
        before = read_snapshot(args.snapshot, args.expect_body_sha256)
        body = read_text(args.body_file)
        if args.command == "check-checkboxes":
            return check_checkboxes(before["body"], body)
        if before["issue"] != args.issue:
            raise IssueError("指定した Issue 番号と本文の控えの番号が違う")
        check_digest(before, body, args.expect_task_digest)
        if args.checkbox_only:
            check_checkboxes(before["body"], body)
        repo = resolve_repo(args.dir)
        if before["repo"] != repo:
            raise IssueError("origin のリポジトリが本文の取得時と違う")
        public_security(permission(repo), args)
        current = fetch(repo, before["issue"])
        if any(current[key] != before[key] for key in
               ("body", "author", "author_is_bot", "editor", "last_edited_at", "viewer", "edits")):
            raise IssueError("Issue 本文または作者・編集者・実行者が取得後に変わった。書かずに停止する")
        try:
            gh(repo, "issue", "edit", str(args.issue), "--body-file", "-", body=body)
            result = fetch(repo, args.issue)
        except (IssueError, OSError, UnicodeError) as exc:
            detail = f"({exc})" if isinstance(exc, IssueError) else ""
            raise IssueError("Issue 書込を試みた後に失敗した。変更済みの可能性がある。"
                             "本文を確認し、自動で再実行しないでください" + detail) from exc
        if result["body"] != body:
            raise IssueError("書込後の Issue 本文が意図した本文と一致しない。再書込せず確認してください")
        return {key: value for key, value in result.items() if key != "body"}

    repo = resolve_repo(args.dir)
    if args.command == "preflight":
        return permission(repo)
    output_available(args.out)
    if args.command == "snapshot":
        if args.approve_body_sha256 is not None and not args.for_execution:
            raise IssueError("--approve-body-sha256 は --for-execution と併せて指定してください")
        output_available(args.body_out)
        if Path(args.out).absolute() == Path(args.body_out).absolute():
            raise IssueError("本文と控えの出力先が同じ")
        result = fetch(repo, args.issue)
        save_snapshot(args.out, result)
        save(args.body_out, result["body"].encode("utf-8"))
        if args.for_execution:
            try:
                check_execution(result, args.approve_body_sha256)
            except IssueError as exc:
                raise ExecutionError(str(exc), result) from exc
    else:
        body = read_text(args.body_file)
        if not args.title.strip():
            raise IssueError("題名が空")
        info = permission(repo)
        if not info["isPrivate"] and not args.approve_public:
            raise IssueError("公開 Issue の新規作成には対象と題名を示した承認が必要(--approve-public)")
        public_security(info, args)
        try:
            raw = gh(repo, "issue", "create", "--title", args.title, "--body-file", "-", body=body)
            url = urlsplit(raw.decode("utf-8").strip())
            host, owner, name = repo.split("/")
            match = re.fullmatch(r"/([^/]+)/([^/]+)/issues/([1-9][0-9]*)", url.path)
            if (url.scheme != "https" or url.netloc.lower() != host.lower() or url.query or url.fragment
                    or not match or match[1].lower() != owner.lower() or match[2].lower() != name.lower()):
                raise IssueError("作成した Issue の URL を確定できない")
            result = fetch(repo, int(match[3]))
            if result["body"] != body or result["title"] != args.title:
                raise IssueError("作成後の題名または本文が一致しない")
            save_snapshot(args.out, result)
        except (IssueError, OSError, ValueError) as exc:
            detail = f"({exc})" if isinstance(exc, IssueError) else ""
            raise IssueError("Issue 作成を試みた後に失敗した。作成済みの可能性がある。"
                             "対象を確認し、重複作成しないでください" + detail) from exc
    return {key: value for key, value in result.items() if key != "body"}


def positive(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("Issue 番号は正の整数")
    return int(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "snapshot", "update", "check-checkboxes", "create"):
        command = commands.add_parser(name)
        if name != "check-checkboxes":
            command.add_argument("--dir", required=True)
        if name in {"snapshot", "create"}:
            command.add_argument("--out", required=True)
        if name == "snapshot":
            command.add_argument("--issue", type=positive, required=True)
            command.add_argument("--body-out", required=True)
            command.add_argument("--for-execution", action="store_true")
            command.add_argument("--approve-body-sha256")
        if name in {"update", "check-checkboxes", "create"}:
            command.add_argument("--body-file", required=True)
        if name in {"update", "check-checkboxes"}:
            command.add_argument("--snapshot", required=True)
            command.add_argument("--expect-body-sha256", required=True)
        if name in {"update", "create"}:
            command.add_argument("--security-task", action="store_true")
            command.add_argument("--approve-public-security", action="store_true")
        if name == "update":
            command.add_argument("--issue", type=positive, required=True)
            command.add_argument("--checkbox-only", action="store_true")
            command.add_argument("--expect-task-digest")
        if name == "create":
            command.add_argument("--title", required=True)
            command.add_argument("--approve-public", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args)
    except (IssueError, OSError, UnicodeError) as exc:
        if isinstance(exc, ExecutionError):
            print(json.dumps({key: value for key, value in exc.value.items() if key != "body"},
                             ensure_ascii=False))
        if isinstance(exc, IssueError):
            message = str(exc)
        else:
            message = "ファイルまたは応答を読み書きできない。入力の形式と出力先を確認してください"
        print(f"停止: {message}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
