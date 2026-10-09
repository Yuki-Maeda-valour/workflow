#!/usr/bin/env python3
"""無人ループ(loop.sh)の許可の仲介。PermissionRequest の hook として、周のホスト CLI から呼ばれる。

契約の正本は ../references/loop.md(設計は Issue #68 の D22 ③)。loop.sh が `--settings` の JSON 文字列で
この hook を渡し、周の子の環境に次の 4 つを付ける:

  DEV_WORKFLOW_LOOP_WORKTREE    周の worktree の物理パス
  DEV_WORKFLOW_LOOP_PERMLOG     判定の記録のファイル(1 呼び出し 1 行の JSON を足す)
  DEV_WORKFLOW_LOOP_PLUGIN_ROOT プラグインルートの物理パス
  DEV_WORKFLOW_LOOP_ALLOW       許可リスト(種類つきの JSON 配列 [{"kind": "all"|"prefix"|"exact", "words": [...]}])

stdin の JSON(tool_name・tool_input・cwd)を読み、stdout に
{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"|"deny", ...}}}
を返す(終了コードは常に 0)。判定は決定的で、入力と環境変数(とパスの解決に要るファイルシステム)だけで決める。
許すのは、周の worktree の中の決まった状態ファイル(W)への書き込みと、限定の構文で読める Bash だけ。
Bash の `cd` は、`CDPATH= cd -P -- <P> && pwd -P` と、先頭の前置き `CDPATH= cd -P -- <P> && <続き>`(続きを <P> で
判定する。<P> は worktree の中のディレクトリ。続きの最初の `||`・`;` より後ろは <P> と入力の cwd の両方で判定する)の
2 つの形だけを受け付ける。追跡できない `pushd`・`popd`、ラッパー経由の移動、引用のない予約語の前置きは拒否する。
<P> の字面が `-` で始まるもの・`~` を含むもの(位置を問わない)は受け付けない。
判定できない入力(JSON が読めない・環境変数が無い)も deny。hook は隔離ではない(同じ利用者の権限で動く)。
標準ライブラリだけで書く。

`--is-protected <管理ルート相対パス>` で起動すると、判定をせず、そのパスが保護パスの下か(`.claude/worktrees` の
下を除く)だけを終了コードで返す(0 = 下 / 1 = 下でない)。loop.sh が task_dir の検査に使う(保護パスの列を
2 か所に置かない)。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time

# ホストの保護パス(公式文書 permission-modes の「Protected paths」。2026-09-24 確認)
PROTECTED_DIRS = {".git", ".vscode", ".idea", ".husky", ".cargo", ".devcontainer", ".yarn", ".mvn", ".claude"}
PROTECTED_FILES = {
    ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".bash_login", ".bash_aliases", ".bash_logout",
    ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".zlogout", ".profile", ".envrc", ".npmrc", ".yarnrc",
    ".yarnrc.yml", ".pnp.cjs", ".pnp.loader.mjs", ".pnpmfile.cjs", "bunfig.toml", ".bunfig.toml", ".bazelrc",
    ".bazelversion", ".bazeliskrc", ".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml", ".lefthook.yml",
    ".lefthook.yaml", "gradle-wrapper.properties", "maven-wrapper.properties", ".devcontainer.json", ".ripgreprc",
    "pyrightconfig.json", ".mcp.json", ".claude.json",
}
PROTECTED_NAMES = PROTECTED_DIRS | PROTECTED_FILES

# 代入を許す環境変数の名(D22。列挙。前方一致にしない)
ASSIGN_NAMES = {"CDPATH", "LC_ALL", "LANG", "GIT_NO_LAZY_FETCH", "GIT_TERMINAL_PROMPT"}

# ファイル操作のコマンドと、受け付ける短いオプションの文字(どれも `--` を受け付ける)
FILE_OP_SHORT = {"mkdir": "", "touch": "", "rmdir": "", "rm": "frRv", "cp": "frRpv", "mv": "frRpv", "tee": "a"}

# 許可の仲介が受け付ける Git の global option と `-c`。loop.sh 自身の GIT_PRE と同じ
# 値だけにする。値を一般化すると alias・filter・helper などが任意のプログラムを起動できる。
SAFE_GIT_CONFIGS = {
    "core.quotePath=false", "core.fsmonitor=", "core.hooksPath=/dev/null", "core.ignoreCase=false",
    "core.splitIndex=false", "core.filemode=true", "core.symlinks=true", "core.ignoreStat=false",
    "diff.autoRefreshIndex=false", "core.autocrlf=false", "core.eol=lf", "apply.whitespace=nowarn",
    "core.sparseCheckout=false", "core.sparseCheckoutCone=false", "filter.lfs.smudge=", "filter.lfs.clean=",
    "filter.lfs.process=", "filter.lfs.required=false", "commit.gpgSign=false", "push.gpgSign=false",
}
SAFE_GIT_GLOBALS = {"--no-pager", "--no-replace-objects", "--literal-pathspecs", "--no-literal-pathspecs"}
SAFE_GIT_PREFIX = (
    "--no-pager", "--no-replace-objects",
    "-c", "core.quotePath=false", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
    "-c", "core.ignoreCase=false", "-c", "core.splitIndex=false", "-c", "core.ignoreStat=false",
    "-c", "commit.gpgSign=false", "-c", "push.gpgSign=false", "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.clean=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
)
SAFE_PUSH_PREFIX = (
    "--no-pager", "--no-replace-objects",
    "-c", "core.quotePath=false", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
    "-c", "core.ignoreCase=false", "-c", "core.splitIndex=false", "-c", "core.ignoreStat=false",
    "-c", "commit.gpgSign=false", "-c", "push.gpgSign=false", "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.clean=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
)
GIT_READ_COMMANDS = {
    "blame", "branch", "cat-file", "check-attr", "check-ignore", "check-ref-format", "describe", "diff",
    "diff-tree", "for-each-ref", "grep", "log", "ls-files", "ls-tree", "merge-base", "name-rev", "reflog",
    "rev-list", "rev-parse", "show", "show-ref", "status", "verify-commit", "verify-tag", "version",
}
GIT_DIRECT_STATE_COMMANDS = {
    "config", "update-ref", "symbolic-ref", "replace", "worktree", "update-index", "read-tree",
    "checkout-index", "remote", "tag",
}

# deny の message に理由へ続けて添える固定の文(D22 ③ (d))。無人の周では打ち直さず、unattended-mode.md の
# 許可の拒否(G1)に従う(拒否されたら別の手段で回り込まない — 保留か失敗扱い)。判定の記録(PERMLOG)の reason には入れない
DENY_NOTE = (
    "loop.sh の許可の仲介が拒否した。無人の周では打ち直さず(別の手段で回り込まず)、"
    "unattended-mode.md の許可の拒否(G1)に従う"
)

REVIEWS = ".claude/reviews"
W_FILES = {".claude/grasp.md", ".claude/.understand-project-done"}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
RESERVED_PREFIXES = {
    "!", "time", "if", "then", "elif", "else", "fi", "for", "while", "until", "do", "done", "select", "case",
    "esac", "in", "function", "coproc",
}


class Denied(Exception):
    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def other(reason: str) -> Denied:
    return Denied("other", reason)


# ── パス ──

def inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def locate(path: str) -> tuple[str, str, bool]:
    """(親の symlink と `..` を解決した場所, 対象の symlink も辿った実体, 対象そのものが symlink か)。"""
    trimmed = path.rstrip("/") or "/"
    head, name = os.path.split(trimmed)
    if name in ("", ".", ".."):
        full = os.path.realpath(trimmed)
        return full, full, False
    loc = os.path.join(os.path.realpath(head or "/"), name)
    return loc, os.path.realpath(loc), os.path.islink(loc)


def is_protected(rel: str) -> bool:
    comps = [c for c in rel.split("/") if c]
    for i, comp in enumerate(comps):
        if comp in PROTECTED_DIRS:
            if comp == ".claude" and i + 1 < len(comps) and comps[i + 1] == "worktrees":
                continue
            return True
        if comp == ".config" and i + 1 < len(comps) and comps[i + 1] == "git":
            return True
    return bool(comps) and comps[-1] in PROTECTED_FILES


class Ctx:
    def __init__(self, env: dict, cwd: str):
        self.env = env
        self.wt = os.path.realpath(env["worktree"])
        self.pr = os.path.realpath(env["plugin_root"])
        self.cwd = cwd
        self.home = os.environ.get("HOME", "")
        self.allow = env["allow"]
        self.environment = env
        self.protected_hits: list[str] = []
        # 書き込みでない単語(パスの引数・代入の値)が保護パスの下で W の外を指したもの(rel, 理由)。
        # 同じ場所が書き込み先でもあれば、その書き込み先の判定(protected)に任せる。そうでなければ other
        self.nonwrite_hits: list[tuple[str, str]] = []
        self.write_rels: set[str] = set()

    def rel(self, loc: str) -> str:
        return os.path.relpath(loc, self.wt)

    def in_w(self, rel: str, *, mkdir: bool = False) -> bool:
        # W: .claude/reviews/ の下・.claude/grasp.md・.claude/.understand-project-done だけ(task_dir が保護パスの下の
        # 構成は loop.sh が §3 の 3 で止めるので、タスク MD は入れない)
        if rel.startswith(REVIEWS + "/"):
            return True
        if mkdir and rel == REVIEWS:
            return True
        return rel in W_FILES

    def resolve(self, word: str, tilde: bool) -> str:
        if tilde:
            if not self.home:
                raise other("HOME が無いので ~ を解けない")
            if word == "~" or word.startswith("~/"):
                return self.home + word[1:]
            raise other(f"~ の形を解けない: {word[:100]}")
        if os.path.isabs(word):
            return word
        return os.path.join(self.cwd, word)

    def protected(self, reason: str) -> None:
        # 保護パスの下で W の外への書き込み(書き込み先)だけが理由の拒否。ほかの理由が無いかを見るため、
        # ここでは止めずに集める
        self.protected_hits.append(reason)

    def mark_write(self, loc: str) -> None:
        if inside(loc, self.wt):
            self.write_rels.add(self.rel(loc))

    # 書き込み先(ファイル操作の対象・行き先・リダイレクト・Write/Edit)
    def check_write(self, path: str, what: str, *, mkdir: bool = False, allow_devnull: bool = False) -> None:
        loc, full, link = locate(path)
        if allow_devnull and (loc == "/dev/null" or full == "/dev/null"):
            return
        if not (inside(loc, self.wt) and inside(full, self.wt)):
            raise other(f"{what}が周の worktree の外: {path[:200]}")
        self.mark_write(loc)
        for target in dict.fromkeys((loc, full)):
            rel = self.rel(target)
            if is_protected(rel) and (link or not self.in_w(rel, mkdir=mkdir)):
                self.protected(f"{what}が保護パスの下で W の外: {rel[:200]}")

    def remove_allowed(self, loc: str, link: bool, directory: bool, recursive: bool) -> bool:
        """loc の削除に既存の保護パスと W の例外を適用する。"""
        rel = self.rel(loc)
        if not is_protected(rel):
            return True
        under_reviews = rel.startswith(REVIEWS + "/")
        if link:
            ok = False
        elif directory:
            ok = under_reviews  # .claude/reviews/ の下のディレクトリだけ(そのものは除く)
        else:
            ok = under_reviews or rel in W_FILES  # W の通常ファイル(無くてもよい)
        return ok and (not recursive or under_reviews)

    def check_recursive_descendants(self, root: str, root_stat: os.stat_result) -> None:
        """symlink を辿らず、root 以下を明示的なスタックで検査する。"""
        if not stat.S_ISDIR(root_stat.st_mode):
            return
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    children = list(entries)
            except OSError as exc:
                raise other(f"再帰削除の子孫を検査できない: {str(exc)[:160]}") from exc
            for entry in children:
                try:
                    entry_stat = os.lstat(entry.path)
                except OSError as exc:
                    raise other(f"再帰削除の子孫を検査できない: {str(exc)[:160]}") from exc
                link = stat.S_ISLNK(entry_stat.st_mode)
                directory_entry = stat.S_ISDIR(entry_stat.st_mode)
                if not self.remove_allowed(entry.path, link, directory_entry, True):
                    rel = self.rel(entry.path)
                    self.protected(f"再帰削除の子孫が保護パスの下で W の外: {rel[:200]}")
                if directory_entry:
                    stack.append(entry.path)

    # 削除・移動の元(rm・rmdir・mv の元)
    def check_remove(self, path: str, what: str, recursive: bool) -> None:
        loc, full, link = locate(path)
        # 最終リンクの末尾 `/` の意味を解く前にも、削除するリンク自体は worktree の中でなければならない。
        # check_path_word は plugin_root の読取りを許すため、ここで従来の削除元の範囲を保つ。
        if not inside(loc, self.wt) or (not link and not inside(full, self.wt)):
            raise other(f"{what}が周の worktree の外: {path[:200]}")
        # `rm -r link/` は link 自体でなくリンク先を再帰削除する。末尾 `/` の場合だけ、最終リンクを
        # たどった場所を対象にする。`rm link` の H36 の契約は変えない。
        follows_final_link = recursive and path.endswith("/") and path.rstrip("/") != ""
        if follows_final_link and link:
            if not inside(full, self.wt):
                raise other(f"{what}が周の worktree の外: {path[:200]}")
            self.mark_write(loc)  # check_path_word が記録したリンク自体の非書き込み判定を打ち消す
            loc, full, link = full, full, False
        self.mark_write(loc)
        if recursive and loc == self.wt:
            raise other(f"{what}が周の worktree 自体: {path[:200]}")
        target_stat = None
        try:
            target_stat = os.lstat(loc) if not link else None
            directory = stat.S_ISDIR(target_stat.st_mode) if target_stat is not None else False
        except FileNotFoundError:
            directory = False
        except OSError as exc:
            raise other(f"{what}の対象を検査できない: {str(exc)[:160]}") from exc
        if not self.remove_allowed(loc, link, directory, recursive):
            rel = self.rel(loc)
            self.protected(f"{what}が保護パスの下で W の外: {rel[:200]}")
        if recursive and target_stat is not None:
            self.check_recursive_descendants(loc, target_stat)

    # パスとして解ける単語(どのコマンドでも)
    def check_path_word(self, path: str) -> None:
        loc, full, link = locate(path)
        if loc == "/dev/null" or full == "/dev/null":
            return
        ok_loc = inside(loc, self.wt) or inside(loc, self.pr)
        ok_full = inside(full, self.wt) or inside(full, self.pr)
        if not (ok_loc and ok_full):
            raise other(f"パスが周の worktree とプラグインルートの外: {path[:200]}")
        # nonwrite_hits のキーは常に元の loc にする。書き込み先でもある loc は
        # mark_write で除かれ、rm・mv の元は引き続き check_remove が判定する。
        rel = self.rel(loc)
        for target in dict.fromkeys((loc, full)):
            if inside(target, self.wt):
                target_rel = self.rel(target)
                if is_protected(target_rel) and (link or not self.in_w(target_rel, mkdir=True)):
                    self.nonwrite_hits.append((rel, f"パスが保護パスの下で W の外: {target_rel[:200]}"))


# ── Bash の限定の構文 ──

class Word:
    def __init__(self) -> None:
        self.chars: list[str] = []
        self.quoted: list[bool] = []
        self.had_quote = False

    def add(self, ch: str, quoted: bool) -> None:
        self.chars.append(ch)
        self.quoted.append(quoted)
        self.had_quote = self.had_quote or quoted

    @property
    def text(self) -> str:
        return "".join(self.chars)

    @property
    def tilde(self) -> bool:
        return bool(self.chars) and self.chars[0] == "~" and not self.quoted[0]

    def glob(self) -> bool:
        return any(c in "*?[" and not q for c, q in zip(self.chars, self.quoted))

    def assignment(self) -> tuple[str, "Word"] | None:
        for i, (c, q) in enumerate(zip(self.chars, self.quoted)):
            if c == "=" and not q:
                name = "".join(self.chars[:i])
                if i > 0 and not any(self.quoted[:i]) and IDENT.match(name):
                    value = Word()
                    for ch, qq in zip(self.chars[i + 1:], self.quoted[i + 1:]):
                        value.add(ch, qq)
                    return name, value
                return None
        return None


def tokenize(s: str) -> list[tuple]:
    """('word', Word) / ('op', '&&'|'||'|';'|'|') / ('redir', 演算子, fd) の列。読めなければ Denied。"""
    toks: list[tuple] = []
    i, n = 0, len(s)
    cur: Word | None = None

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            toks.append(("word", cur))
            cur = None

    while i < n:
        c = s[i]
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise other("閉じていない単引用符")
            cur = cur or Word()
            if j == i + 1:
                cur.add("\0", True)  # 空の引用('')も単語を作る(印は最後に除く)
            for ch in s[i + 1:j]:
                cur.add(ch, True)
            i = j + 1
            continue
        if c == '"':
            cur = cur or Word()
            j = i + 1
            empty = True
            while True:
                if j >= n:
                    raise other("閉じていない二重引用符")
                ch = s[j]
                if ch == '"':
                    break
                if ch in "$`":
                    raise other("引用符の中の $ かバッククォート")
                if ch == "\n":
                    raise other("改行")
                if ch == "\\" and j + 1 < n and s[j + 1] in '"\\':
                    cur.add(s[j + 1], True)
                    j += 2
                    empty = False
                    continue
                cur.add(ch, True)
                empty = False
                j += 1
            if empty:
                cur.add("\0", True)
            i = j + 1
            continue
        if c == "\\":
            if i + 1 >= n or s[i + 1] == "\n":
                raise other("行の継続か末尾のバックスラッシュ")
            if s[i + 1] in "$`":
                raise other("$ かバッククォート")
            cur = cur or Word()
            cur.add(s[i + 1], True)
            i += 2
            continue
        if c in " \t":
            flush()
            i += 1
            continue
        if c in "\n\r":
            raise other("改行")
        if c in "$`":
            raise other("単引用符の外の $ かバッククォート")
        if c == "~":
            raise other("引用符の外の ~")
        # Git の `@{upstream}` は Bash の brace expansion ではなく、rev-parse の固定の revision suffix。
        # 文書化された query だけを許し、一般の `{...}` は従来どおり構文として受け付けない。
        if s.startswith("@{upstream}", i):
            cur = cur or Word()
            for char in "@{upstream}":
                cur.add(char, False)
            i += len("@{upstream}")
            continue
        if c in "(){}":
            raise other("括弧か中括弧")
        if c == "#" and cur is None:
            raise other("コメント")
        if c == "&":
            flush()
            if s.startswith("&&", i):
                toks.append(("op", "&&"))
                i += 2
            elif s.startswith("&>", i) and not s.startswith("&>>", i):
                toks.append(("redir", "&>", None))
                i += 2
            else:
                raise other("単独の & か &>>")
            continue
        if c == "|":
            flush()
            if s.startswith("||", i):
                toks.append(("op", "||"))
                i += 2
            elif s.startswith("|&", i):
                raise other("|&")
            else:
                toks.append(("op", "|"))
                i += 1
            continue
        if c == ";":
            flush()
            if s.startswith(";;", i):
                raise other(";;")
            toks.append(("op", ";"))
            i += 1
            continue
        if c == "<":
            flush()
            if i + 1 < n and s[i + 1] in "<(&>":
                raise other("<< ・<( ・<& ・<> は受け付けない")
            toks.append(("redir", "<", None))
            i += 1
            continue
        if c == ">" or (c.isdigit() and cur is None and re.match(r"\d+>", s[i:])):
            flush()
            m = re.match(r"(\d*)(>>|>\||>&(\d+)(?=[\s;&|]|$)|>)", s[i:])
            if not m or s[i + m.end():].startswith("("):
                raise other("受け付けないリダイレクトの形")
            fd = m.group(1) or None
            op = m.group(2)
            if op.startswith(">&"):
                toks.append(("redir", ">&", fd))
            else:
                toks.append(("redir", op, fd))
            i += m.end()
            continue
        cur = cur or Word()
        cur.add(c, False)
        i += 1
    flush()
    # 空の引用の印を除く
    for t in toks:
        if t[0] == "word":
            w = t[1]
            keep = [(ch, q) for ch, q in zip(w.chars, w.quoted) if ch != "\0"]
            w.chars = [ch for ch, _ in keep]
            w.quoted = [q for _, q in keep]
    return toks


def parse(toks: list[tuple]) -> tuple[list[dict], list[str]]:
    cmds: list[dict] = []
    ops: list[str] = []
    cur = {"assigns": [], "words": [], "redirs": []}
    i = 0
    while i < len(toks):
        t = toks[i]
        if t[0] == "op":
            cmds.append(cur)
            ops.append(t[1])
            cur = {"assigns": [], "words": [], "redirs": []}
            i += 1
            continue
        if t[0] == "redir":
            op, fd = t[1], t[2]
            if op == ">&":
                cur["redirs"].append((op, fd, None))
                i += 1
                continue
            if i + 1 >= len(toks) or toks[i + 1][0] != "word":
                raise other("リダイレクトの先が無い")
            cur["redirs"].append((op, fd, toks[i + 1][1]))
            i += 2
            continue
        word = t[1]
        if not cur["words"] and word.assignment() is not None:
            cur["assigns"].append(word)
        else:
            cur["words"].append(word)
        i += 1
    cmds.append(cur)
    for c in cmds:
        if not c["words"] and not c["assigns"]:
            raise other("空のコマンド(演算子の前後が空)")
    return cmds, ops


def looks_like_path(text: str, tilde: bool) -> bool:
    if tilde:
        return True
    return "/" in text or text.startswith(".") or text in PROTECTED_NAMES


def is_bare_symlink(ctx: Ctx, text: str, tilde: bool) -> bool:
    """字面がパスでなくても、cwd に在る symlink はパスとして判定する。"""
    return not looks_like_path(text, tilde) and bool(text) and os.path.islink(ctx.resolve(text, tilde))


def is_bare_path(ctx: Ctx, text: str, tilde: bool) -> bool:
    """裸名も、保護 cwd 内か既存 symlink ならパスとして判定する。"""
    return (not looks_like_path(text, tilde) and bool(text)
            and (is_protected(ctx.rel(ctx.cwd)) or is_bare_symlink(ctx, text, tilde)))


def check_attached_short_option(ctx: Ctx, word: Word) -> None:
    """短いオプションへ連結した値を、字面だけで保守的にパスとして検査する。"""
    text = word.text
    # コマンド固有のオプション文法は持たない。`-ko.git` のような束ね書きも含め、3 文字目以降の各接尾辞を
    # 候補にする。値を取らない通常の短いフラグも、保護名や既存 symlink と重ならなければ従来どおり通る。
    for start in range(2, len(text)):
        candidate = text[start:]
        # `~` が引用・escape されていても、展開規則をここで再現せず拒否する。
        if candidate.startswith("~"):
            raise other(f"~ を含む短いオプションの形を判定できない: {text[:100]}")
        if looks_like_path(candidate, False) or is_bare_symlink(ctx, candidate, False):
            ctx.check_path_word(ctx.resolve(candidate, False))


def check_words_as_paths(ctx: Ctx, words: list[Word], *, skip_bare_first: bool = False) -> None:
    options = True
    for index, w in enumerate(words):
        text = w.text
        tilde = w.tilde
        if options and text == "--":
            options = False
            continue
        if options and text.startswith("-"):
            if "=" in text and text.startswith("--"):
                value = text.split("=", 1)[1]
                # --name=値 の値の部分を見る(~ は引用符の外で値の先頭にあるときだけ)
                vtilde = value.startswith("~") and not w.quoted[text.index("=") + 1] if value else False
                if value and (looks_like_path(value, vtilde) or is_bare_path(ctx, value, vtilde)):
                    ctx.check_path_word(ctx.resolve(value, vtilde))
                continue
            if "/" in text:
                raise other(f"パスを含むオプションの形を判定できない: {text[:100]}")
            if not text.startswith("--") and len(text) >= 3:
                check_attached_short_option(ctx, w)
            continue
        if looks_like_path(text, tilde) or (not (skip_bare_first and index == 0) and is_bare_path(ctx, text, tilde)):
            ctx.check_path_word(ctx.resolve(text, tilde))


def check_git_words_as_paths(ctx: Ctx, words: list[Word]) -> None:
    """Git の global option の値を一般の短縮 option として誤認せず、operand は既存のパス規則で読む。"""
    skip_next = False
    for word in words:
        text = word.text
        if skip_next:
            skip_next = False
            continue
        if text == "-c":
            skip_next = True
            continue
        if text.startswith("-") or text == "--":
            continue
        if looks_like_path(text, word.tilde) or is_bare_path(ctx, text, word.tilde):
            ctx.check_path_word(ctx.resolve(text, word.tilde))


def check_assignment(ctx: Ctx, word: Word) -> None:
    name, value = word.assignment()  # type: ignore[misc]
    if name not in ASSIGN_NAMES:
        raise other(f"代入を許さない環境変数の名: {name}")
    if name == "CDPATH" and value.text != "":
        raise other("CDPATH の値は空だけ")
    if value.text and (looks_like_path(value.text, value.tilde) or is_bare_path(ctx, value.text, value.tilde)):
        ctx.check_path_word(ctx.resolve(value.text, value.tilde))


def split_opts(words: list[Word]) -> tuple[list[str], list[Word]]:
    opts, operands, ended = [], [], False
    for w in words:
        t = w.text
        if not ended and t == "--":
            ended = True
            continue
        if not ended and t.startswith("-") and t != "-":
            opts.append(t)
        else:
            operands.append(w)
    return opts, operands


def check_file_op(ctx: Ctx, name: str, args: list[Word]) -> None:
    allowed = FILE_OP_SHORT[name]
    opts, operands = split_opts(args)
    recursive = False
    for o in opts:
        if o.startswith("--") or not all(ch in allowed for ch in o[1:]) or len(o) < 2:
            raise other(f"{name} のオプションを受け付けない: {o[:100]}")
        if "r" in o or "R" in o:
            recursive = True
    if not operands:
        raise other(f"{name} の対象が無い")
    paths = [ctx.resolve(w.text, w.tilde) for w in operands]
    if name in ("mkdir",):
        for p in paths:
            ctx.check_write(p, "mkdir の作成先", mkdir=True)
    elif name in ("touch", "tee"):
        for p in paths:
            ctx.check_write(p, f"{name} の対象")
    elif name in ("rm", "rmdir"):
        for p in paths:
            ctx.check_remove(p, f"{name} の対象", recursive)
    else:  # cp・mv
        if len(paths) < 2:
            raise other(f"{name} の行き先が無い")
        dest, sources = paths[-1], paths[:-1]
        dest_is_dir = os.path.isdir(dest) and not os.path.islink(dest.rstrip("/") or "/")
        if dest_is_dir:
            ctx.mark_write(locate(dest)[0])  # 行き先のディレクトリの単語も書き込み先の一部
        for src in sources:
            if name == "mv":
                ctx.check_remove(src, "mv の元", False)
            else:
                ctx.check_path_word(src)
            target = os.path.join(dest, os.path.basename(src.rstrip("/"))) if dest_is_dir else dest
            ctx.check_write(target, f"{name} の行き先")


def _sed_command_start(script: str) -> int | None:
    """限定した sed の address の後にある command の位置を返す。"""
    i, n = 0, len(script)
    if i < n and script[i].isdigit():
        while i < n and script[i].isdigit():
            i += 1
    elif i < n and script[i] == "$":
        i += 1
    elif i + 1 < n and script[i] == "\\":
        delim = script[i + 1]
        i += 2
        while i < n:
            if script[i] == "\\" and i + 1 < n:
                i += 2
            elif script[i] == delim:
                i += 1
                break
            else:
                i += 1
        else:
            return None
    elif i < n and script[i] == "/":
        i += 1
        while i < n:
            if script[i] == "\\" and i + 1 < n:
                i += 2
            elif script[i] == "/":
                i += 1
                break
            else:
                i += 1
        else:
            return None
    if i < n and script[i] == ",":
        i += 1
        if i < n and script[i].isdigit():
            while i < n and script[i].isdigit():
                i += 1
        elif i < n and script[i] == "$":
            i += 1
        else:
            return None
    return i


def sed_script_safe(script: str) -> bool:
    """出力だけを行う、短い sed script だけを受け付ける。"""
    if not script or "\n" in script or "\r" in script or ";" in script:
        return False
    start = _sed_command_start(script)
    if start is None or start >= len(script):
        return False
    command = script[start]
    if command in "pd":
        return start + 1 == len(script)
    if command != "s" or start + 2 >= len(script):
        return False
    delim = script[start + 1]
    if delim.isalnum() or delim.isspace() or delim == "\\":
        return False
    i, closes = start + 2, 0
    while i < len(script):
        if script[i] == "\\" and i + 1 < len(script):
            i += 2
            continue
        if script[i] == delim:
            closes += 1
            if closes == 2:
                flags = script[i + 1:]
                return bool(re.fullmatch(r"[0-9gIp]*", flags))
        i += 1
    return False


def check_sed(ctx: Ctx, args: list[Word]) -> bool:
    """`sed` は入力を読むだけの固定した script に限定する。"""
    scripts: list[Word] = []
    files: list[Word] = []
    ended = False
    in_place = False
    implicit_script = True
    k = 0
    while k < len(args):
        word = args[k]
        text = word.text
        if not ended and text == "--":
            ended = True
        elif not ended and text in ("-i", "--in-place"):
            in_place = True
        elif not ended and (text.startswith("-i") or text.startswith("--in-place")):
            raise other("sed の in-place 接尾辞を受け付けない")
        elif not ended and text in ("-n", "-E", "-r", "--sandbox"):
            pass
        elif not ended and text == "-e":
            if k + 1 >= len(args):
                raise other("sed -e の値が無い")
            scripts.append(args[k + 1])
            implicit_script = False
            k += 1
        elif not ended and text.startswith("-") and text != "-":
            raise other(f"sed のオプションを受け付けない: {text[:100]}")
        elif implicit_script:
            scripts.append(word)
            implicit_script = False
        else:
            files.append(word)
        k += 1
    if not scripts:
        raise other("sed の script が無い")
    for script in scripts:
        if not sed_script_safe(script.text):
            raise other("sed の read/write/execute または未知の script")
    if in_place and not files:
        raise other("sed -i の対象が無い")
    for path in files:
        target = ctx.resolve(path.text, path.tilde)
        if in_place:
            ctx.check_write(target, "sed -i の対象")
        else:
            ctx.check_path_word(target)
    return in_place


def check_find(args: list[Word]) -> None:
    """find の副作用を持つ action は allow=all でも通さない。"""
    denied = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
    for word in args:
        if word.text in denied:
            raise other(f"find の書き込み/実行 action を受け付けない: {word.text}")


def check_awk(args: list[Word]) -> None:
    """awk は任意プログラムなので、必要な固定の品質確認以外に拡げない。"""
    fixed = {
        "{print}", "{ print }", "{print $0}", "{ print $0 }",
        "NR==1 {print}", "NR==1 { print }",
    }
    if not args or args[0].text not in fixed:
        raise other("awk の任意 program を受け付けない")
    if any(word.text.startswith("-") for word in args[1:]):
        raise other("awk の program 後の option を受け付けない")


def git_pathspec_safe(ctx: Ctx, word: Word, *, source: bool = True) -> None:
    """書き込み Git の pathspec を通常ファイル 1 件に限定する。"""
    text = word.text
    if text in ("", ".") or text.startswith("-"):
        raise other("Git の広域 pathspec を受け付けない")
    if text.startswith(":("):
        if not text.startswith(":(literal)"):
            raise other("Git の magic/glob pathspec を受け付けない")
        text = text[len(":(literal)"):]
    elif text.startswith(":") or any(ch in text for ch in "*?["):
        raise other("Git の magic/glob pathspec を受け付けない")
    path = ctx.resolve(text, word.tilde)
    loc, full, link = locate(path)
    if link or not (inside(loc, ctx.wt) and inside(full, ctx.wt)):
        raise other("Git の pathspec が worktree の外か symlink")
    if os.path.isdir(loc):
        raise other("Git のディレクトリ pathspec を受け付けない")
    if source and not git_source_pathspec_safe(ctx, loc):
        raise other("Git の pathspec が index/HEAD の通常ファイル 1 件と整合しない")
    if is_protected(ctx.rel(ctx.cwd)) and not ctx.in_w(ctx.rel(ctx.cwd), mkdir=True) and not looks_like_path(text, word.tilde):
        raise other("保護 cwd の裸の Git pathspec")
    ctx.check_write(path, "Git の書き込み pathspec")


def git_source_pathspec_safe(ctx: Ctx, loc: str) -> bool:
    """Git pathspec を index と HEAD の 1 ファイル集合に照合する。

    不存在の `subtree` を Git が directory pathspec として展開すると保護ファイルを
    復元できる。逆に、現在の通常ファイル `subtree` でも index/HEAD に `subtree/.claude/...`
    が残れば `git add -- subtree` が保護 path を index から消せる。`reset` と
    `restore --source=HEAD` は HEAD tree を使うため、両方の descendant を見る。
    """
    rel = os.path.relpath(loc, ctx.wt)
    dotgit = os.path.join(ctx.wt, ".git")
    if not os.path.exists(dotgit):
        # 単体の permission regression fixture は Git worktree ではない。この場合は実行時の
        # Git 自身が失敗し、directory pathspec の展開は起きないため従来の parser 契約を保つ。
        return True
    try:
        git_env = dict(os.environ)
        git_env["GIT_NO_LAZY_FETCH"] = "1"
        git_prefix = ["git", "-C", ctx.wt, *SAFE_GIT_PREFIX]
        index = subprocess.run(
            [*git_prefix, "ls-files", "--stage", "-z", "--", rel],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, check=False, env=git_env,
        )
        tree = subprocess.run(
            [*git_prefix, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", rel],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, check=False, env=git_env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if index.returncode != 0:
        return False
    paths: set[str] = set()
    for record in index.stdout.split(b"\0"):
        if not record:
            continue
        try:
            _meta, path = record.split(b"\t", 1)
        except ValueError:
            return False
        try:
            paths.add(path.decode("utf-8", "strict"))
        except UnicodeDecodeError:
            return False
    # unborn HEAD では ls-tree が失敗する。index に通常ファイルが 1 件だけなら、
    # pathspec の展開で保護ファイルを復元する source tree 自体が存在しない。
    if tree.returncode == 0:
        try:
            paths.update(item.decode("utf-8", "strict") for item in tree.stdout.split(b"\0") if item)
        except UnicodeDecodeError:
            return False
    if any(path != rel for path in paths):
        return False
    if not os.path.lexists(loc):
        return (paths == {rel}
                and (not is_protected(rel) or ctx.in_w(rel, mkdir=True)))
    return True


def git_output_dir_safe(ctx: Ctx, word: Word) -> None:
    """format-patch の出力先は、書き込み判定より先に通常の path word として読む。

    接尾辞に隠した protected path/symlink は G1 の other として集計する既存契約を保つ。
    """
    path = ctx.resolve(word.text, word.tilde)
    ctx.check_path_word(path)
    pending = pending_denial(ctx)
    if pending is not None:
        raise pending
    ctx.check_write(path, "Git format-patch の出力先", mkdir=True)


def task_branch_safe(name: str) -> bool:
    """task/ 配下の実在する Git ref として安全な branch 名だけを許す。

    ASCII に限定せず、日本語を含む Git が受け付ける通常の task 名を保つ。
    """
    suffix = name.removeprefix("task/")
    if not suffix or name == suffix or suffix.endswith(".") or suffix.endswith("/") or ".." in suffix or "@{" in suffix:
        return False
    return not any(ord(char) < 32 or ord(char) == 127 or char in " ~^:?*[\\" for char in suffix)


def check_git_read_options(subcommand: str, rest: list[Word]) -> None:
    """無人の周で実際に必要な、読み取り Git の option だけを列挙する。"""
    common = {"--"}
    allowed = {
        "status": common | {"--short", "--branch", "--porcelain=v1", "--untracked-files=normal",
                             "--untracked-files=all", "--ignore-submodules=dirty", "-uall", "-z"},
        "diff": common | {"--no-renames", "--raw", "--cached", "--name-only", "--name-status", "--binary",
                           "--no-ext-diff", "--no-textconv", "--no-relative", "--no-color", "--no-index", "-z",
                           "--ignore-submodules=dirty", "--submodule=short", "--text", "--stat",
                           "--stat-width=200", "--stat-name-width=500", "--unified=3", "--src-prefix=a/",
                           "--dst-prefix=b/"},
        "log": common | {"--oneline", "--no-show-signature", "--no-decorate", "--pickaxe-regex"},
        "show": common | {"--no-show-signature"},
        "rev-parse": common | {"--show-toplevel", "--local-env-vars", "--git-dir", "--git-common-dir",
                                 "--absolute-git-dir", "--verify", "--quiet", "-q", "--path-format=absolute",
                                 "--git-path", "--abbrev-ref"},
        "check-ignore": common | {"-q", "--no-index"},
        "check-attr": common | {"--cached", "--stdin", "-z"},
        "check-ref-format": {"--branch"},
        "ls-files": common | {"-o", "--ignored", "--exclude-standard", "--error-unmatch", "-z", "-s",
                                "--stage", "-u", "-v"},
        "ls-tree": common | {"-r", "--name-only", "-z"},
        "rev-list": common | {"--count"},
        "merge-base": common | {"--is-ancestor"},
        "branch": common | {"--show-current"},
        "show-ref": common | {"--verify", "--quiet", "-q"},
    }.get(subcommand, common)
    for word in rest:
        text = word.text
        if not text.startswith("-") or text == "-":
            continue
        if text in allowed:
            continue
        if subcommand == "log" and (text.startswith("--format=") or text.startswith("--grep=")
                                     or re.fullmatch(r"-[0-9]+", text) or text.startswith(("-G", "-S"))):
            continue
        if subcommand == "for-each-ref" and text.startswith("--format=") and len(text) > len("--format="):
            continue
        if subcommand == "reflog" and text.startswith("--format=") and len(text) > len("--format="):
            continue
        raise other(f"Git {subcommand} の option を受け付けない: {text[:100]}")


def _check_git_global_options(ctx: Ctx, args: list[Word]) -> int:
    """共通オプションを順に読み、変更先の cwd と元の引数上の位置を残す。"""
    i = 0
    while i < len(args):
        text = args[i].text
        if text in SAFE_GIT_GLOBALS:
            i += 1
        elif text == "-c":
            if i + 1 >= len(args) or args[i + 1].text not in SAFE_GIT_CONFIGS:
                raise other("Git -c の key=value を受け付けない")
            i += 2
        elif text.startswith("-c") and text != "-c":
            if text[2:] not in SAFE_GIT_CONFIGS:
                raise other("Git -c の key=value を受け付けない")
            i += 1
        elif text == "-C":
            if i + 1 >= len(args):
                raise other("Git -C の行き先が無い")
            target = args[i + 1]
            if target.glob():
                raise other("Git -C のグロブを受け付けない")
            target_path = os.path.realpath(ctx.resolve(target.text, target.tilde))
            if not inside(target_path, ctx.wt) or not os.path.isdir(target_path):
                raise other("Git -C が worktree 内のディレクトリでない")
            ctx.cwd = target_path
            i += 2
        elif text.startswith("-C") and text != "-C":
            raise other("Git -C の連結形を受け付けない")
        elif text.startswith("--config-env"):
            raise other("Git --config-env を受け付けない")
        elif text.startswith("-"):
            raise other(f"Git の global option を受け付けない: {text[:100]}")
        else:
            break
    return i


def check_git(ctx: Ctx, args: list[Word]) -> None:
    """Git の global option・subcommand・書き込み pathspec を先に限定する。"""
    if not args:
        raise other("Git の subcommand が無い")
    original_cwd = ctx.cwd
    try:
        i = _check_git_global_options(ctx, args)
        if i >= len(args):
            raise other("Git の subcommand が無い")
        subcommand = args[i].text
        rest = args[i + 1:]
        if subcommand == "symbolic-ref":
            if [word.text for word in rest] in (["--quiet", "HEAD"], ["-q", "HEAD"],
                                                ["--quiet", "refs/remotes/origin/HEAD"],
                                                ["-q", "refs/remotes/origin/HEAD"]):
                return
            raise other("Git symbolic-ref の更新形を受け付けない")
        if subcommand == "remote":
            values = [word.text for word in rest]
            if values in ([], ["get-url", "--all", "origin"], ["get-url", "--push", "--all", "origin"]):
                return
            raise other("Git remote の更新形を受け付けない")
        if subcommand == "ls-remote":
            values = [word.text for word in rest]
            valid_ref = lambda ref: ref.startswith("refs/heads/") and not any(char in ref for char in " ~^:?[")
            if len(values) in (1, 2) and values[0] == "origin" and (len(values) == 1 or valid_ref(values[1])):
                return
            raise other("Git ls-remote は origin の heads query だけ")
        if subcommand == "switch":
            values = [word.text for word in rest]
            if (len(values) in (2, 3) and values[-2] == "-c" and values[-1].startswith("task/")
                    and (len(values) == 2 or values[0] == "--no-track")
                    and task_branch_safe(values[-1])):
                return
            raise other("Git switch は task/ の --no-track -c だけ")
        if subcommand == "branch":
            if [word.text for word in rest] == ["--show-current"]:
                return
            raise other("Git branch は --show-current だけ")
        if subcommand == "reflog":
            values = [word.text for word in rest]
            if not values or values[0] != "show":
                raise other("Git reflog の更新形を受け付けない")
            formats = [value for value in values[1:] if value.startswith("--format=")]
            refs = [value for value in values[1:] if not value.startswith("--format=")]
            if len(formats) <= 1 and refs in ([], ["refs/stash"]):
                return
            raise other("Git reflog show の query だけ")
        if subcommand in GIT_DIRECT_STATE_COMMANDS or subcommand in {"clean", "stash", "apply"}:
            raise other(f"Git の直接状態変更を受け付けない: {subcommand}")
        if subcommand in GIT_READ_COMMANDS:
            check_git_read_options(subcommand, rest)
            return
        if subcommand in {"add", "rm"}:
            allowed_options = {"--"} if subcommand == "add" else {"--", "-f", "--force", "--cached", "--ignore-unmatch"}
            for word in rest:
                if word.text.startswith("-") and word.text not in allowed_options:
                    raise other(f"Git {subcommand} の option を受け付けない: {word.text[:100]}")
            operands = [w for w in rest if not w.text.startswith("-") and w.text != "--"]
            if not operands:
                raise other(f"Git {subcommand} の pathspec が無い")
            for word in operands:
                git_pathspec_safe(ctx, word)
            return
        if subcommand in {"checkout", "restore", "reset"}:
            if "--" not in [w.text for w in rest]:
                raise other(f"Git {subcommand} の暗黙の全体操作を受け付けない")
            marker = next(k for k, word in enumerate(rest) if word.text == "--")
            before = [word.text for word in rest[:marker]]
            allowed = {
                "checkout": set(),
                "restore": {"--worktree", "--staged", "--source=HEAD"},
                "reset": {"--mixed"},
            }[subcommand]
            if any(option not in allowed for option in before):
                raise other(f"Git {subcommand} の option を受け付けない")
            operands = rest[marker + 1:]
            if not operands:
                raise other(f"Git {subcommand} の pathspec が無い")
            for word in operands:
                git_pathspec_safe(ctx, word)
            return
        if subcommand == "mv":
            if any(word.text.startswith("-") and word.text not in {"--", "-f", "--force"} for word in rest):
                raise other("Git mv の option を受け付けない")
            operands = [w for w in rest if not w.text.startswith("-") and w.text != "--"]
            if len(operands) != 2:
                raise other("Git mv は通常ファイル 2 件だけ")
            git_pathspec_safe(ctx, operands[0])
            git_pathspec_safe(ctx, operands[1], source=False)
            return
        if subcommand == "hash-object":
            values = [word.text for word in rest]
            if values == ["-t", "tree", "/dev/null"]:
                return
            if any(word.text.startswith("-") for word in rest):
                raise other("Git hash-object の option を受け付けない")
            if not rest:
                raise other("Git hash-object の入力が無い")
            return
        if subcommand == "format-patch":
            k = 0
            while k < len(rest):
                text = rest[k].text
                if text == "--":
                    for operand in rest[k + 1:]:
                        if operand.text.startswith(":(") or operand.text.startswith(":") or any(
                            char in operand.text for char in "*?["
                        ):
                            raise other("Git format-patch の magic/glob を受け付けない")
                        if looks_like_path(operand.text, operand.tilde) or is_bare_symlink(ctx, operand.text, operand.tilde):
                            ctx.check_path_word(ctx.resolve(operand.text, operand.tilde))
                    return
                if re.fullmatch(r"-[0-9]+", text) or text == "--stdout" or (
                    text.startswith("-") and len(text) > 1 and set(text[1:]) <= {"n", "p", "v"}
                ):
                    k += 1
                    continue
                if text == "-o":
                    if k + 1 >= len(rest):
                        raise other("Git format-patch -o の行き先が無い")
                    git_output_dir_safe(ctx, rest[k + 1])
                    k += 2
                    continue
                if text.startswith("-o") and len(text) > 2:
                    check_attached_short_option(ctx, rest[k])
                    output = Word()
                    for char in text[2:]:
                        output.add(char, True)
                    git_output_dir_safe(ctx, output)
                    k += 1
                    continue
                if text.startswith("-"):
                    raise other("Git format-patch の option を受け付けない")
                k += 1
            return
        if subcommand == "commit":
            if len(rest) != 2 or rest[0].text not in ("-F", "--file"):
                raise other("Git commit は reviews の -F だけ")
            review_input(ctx, rest[1])
            return
        if subcommand == "push":
            # 親が worktree の外で固定した宛先だけを、完全な refspec で送る。子の
            # 任意の設定注入を避けるため global options もこの safe prefix に一致する。
            repo = ctx.env.get("DEV_WORKFLOW_LOOP_PUSH_REPO", "")
            ref = ctx.env.get("DEV_WORKFLOW_LOOP_PUSH_REF", "")
            prefix = tuple(word.text for word in args[:i])
            if not repo or not ref.startswith("refs/heads/") or not task_branch_safe(ref.removeprefix("refs/heads/")):
                raise other("Git push の親が固定した送信先が無い")
            origin_script = os.path.join(ctx.pr, "skills", "ship-task", "scripts", "origin-repo.py")
            try:
                checked = subprocess.run([sys.executable, "-B", origin_script, "--dir", ctx.wt], stdin=subprocess.DEVNULL,
                                         capture_output=True, text=True, timeout=10, check=False)
                origin = json.loads(checked.stdout) if checked.returncode == 0 else {}
            except (OSError, subprocess.TimeoutExpired, ValueError):
                origin = {}
            if not (origin.get("origin") is True and origin.get("same") is True and origin.get("vcs") is False
                    and origin.get("repo") == repo):
                raise other("Git push の現在の origin が親の固定リポジトリと一致しない")
            expected = ("--no-follow-tags", "--recurse-submodules=no", "origin",
                        f"refs/heads/{ref.removeprefix('refs/heads/')}:{ref}")
            if prefix != SAFE_PUSH_PREFIX or tuple(word.text for word in rest) != expected:
                raise other("Git push は親が固定した safe prefix・origin・完全 refspec だけ")
            return
        raise other(f"Git の subcommand を受け付けない: {subcommand}")
    finally:
        ctx.cwd = original_cwd


def review_input(ctx: Ctx, word: Word) -> None:
    """PR/commit 本文の入力を W の通常ファイル 1 件に固定する。"""
    if word.glob():
        raise other("reviews の入力にグロブ")
    path = ctx.resolve(word.text, word.tilde)
    loc, full, link = locate(path)
    rel = ctx.rel(loc)
    if link or loc != full or not rel.startswith(REVIEWS + "/"):
        raise other("reviews の入力が通常ファイルでない")
    try:
        mode = os.stat(loc).st_mode
    except OSError as exc:
        raise other(f"reviews の入力を検査できない: {str(exc)[:160]}") from exc
    if not stat.S_ISREG(mode):
        raise other("reviews の入力が通常ファイルでない")


def check_gh(ctx: Ctx, command: dict) -> None:
    """gh は必要な読み取りと PR 作成だけを、argv と stdin 契約ごとに許す。"""
    words = command["words"]
    args = [word.text for word in words[1:]]
    redirs = command["redirs"]
    if args[:2] == ["repo", "view"]:
        if redirs or len(args) != 7 or args[3:5] != ["--json", "name"] or args[5] != "-q" or args[6] != ".name":
            if redirs or len(args) != 7 or args[3:5] != ["--json", "isPrivate"] or args[5] != "-q" or args[6] != ".isPrivate":
                raise other("gh repo view の argv を受け付けない")
        return
    if args[:2] == ["auth", "status"] and len(args) == 2 and not redirs:
        return
    if (len(args) == 5 and args[:2] == ["pr", "view"] and args[2].isdigit()
            and args[3:] == ["--json", "state"] and not redirs):
        return
    if args[:2] != ["pr", "create"]:
        raise other("gh の subcommand を受け付けない")
    body_positions = [index for index, value in enumerate(args) if value == "--body-file"]
    if len(body_positions) != 1 or body_positions[0] + 1 >= len(args) or args[body_positions[0] + 1] != "-":
        raise other("gh pr create は --body-file - だけ")
    allowed_flags = {"-R", "--base", "--head", "--title", "--body-file"}
    i = 2
    while i < len(args):
        if args[i] not in allowed_flags or i + 1 >= len(args):
            raise other("gh pr create の flag を受け付けない")
        i += 2
    if len(redirs) != 1 or redirs[0][0] != "<" or redirs[0][1] is not None or redirs[0][2] is None:
        raise other("gh pr create の stdin は reviews の 1 ファイルだけ")
    review_input(ctx, redirs[0][2])


def matches_allow(words: list[str], allow: list[dict]) -> bool:
    for rule in allow:
        kind = rule.get("kind")
        rw = rule.get("words") or []
        if kind == "all":
            return True
        if kind == "prefix" and words[:len(rw)] == rw:
            return True
        if kind == "exact" and words == rw:
            return True
    return False


def require_allow(ctx: Ctx, words: list[Word]) -> None:
    literal = [word.text for word in words]
    if not matches_allow(literal, ctx.allow):
        raise other(f"許可リストに無いコマンド: {' '.join(literal)[:200]}")


def cd_target_rejected(text: str) -> bool:
    """cd の行き先の字面が `-` で始まるか、`~` を(位置を問わず)含むなら True(is_cd_form・cd_prefix_target が使う)。"""
    return text.startswith("-") or "~" in text


def is_cd_form(ctx: Ctx, cmds: list[dict], ops: list[str]) -> bool:
    """`CDPATH= cd -P -- <パス> && pwd -P`(全体でこれだけ。パスは worktree かプラグインルートの中)。
    cd の前置き(cd_prefix_target)より先に判定する。<パス> の字面が `-` で始まるもの・`~` を含むもの(位置と引用の
    有無を問わない)は受け付けない(bash の `cd -P -- -` は `--` の後でも $OLDPWD へ移る。bash は `x=~/y`・`x=a:~/y`
    のような代入の形の引数の `=`・`:` の直後の `~` も HOME に展開し、`~'/…'` のように引用された文字が続く `~` は
    展開しない。どの `~` が展開されるかをここで写すとずれるので、`~` を含む行き先はすべて拒否する)。"""
    if ops != ["&&"] or len(cmds) != 2:
        return False
    a, b = cmds
    if a["redirs"] or b["redirs"] or b["assigns"]:
        return False
    if len(a["assigns"]) != 1 or a["assigns"][0].text != "CDPATH=":
        return False
    aw = [w.text for w in a["words"]]
    if len(aw) != 4 or aw[:3] != ["cd", "-P", "--"] or [w.text for w in b["words"]] != ["pwd", "-P"]:
        return False
    target = a["words"][3]
    if target.glob() or cd_target_rejected(target.text):
        return False
    full = os.path.realpath(ctx.resolve(target.text, target.tilde))
    return inside(full, ctx.wt) or inside(full, ctx.pr)


def cd_prefix_target(ctx: Ctx, cmds: list[dict], ops: list[str]) -> str | None:
    """cd の前置き: 最初のコマンドが決まった形の `CDPATH= cd -P -- <P>`(代入は `CDPATH=` の 1 つだけ・語は
    `cd -P -- <P>` の 4 つ・リダイレクト無し・<P> はグロブでなく `-` で始まらず `~` を含まない)で、次の区切りが `&&`、
    続きのコマンドに cd が無いとき、<P> の物理パスを返す。<P> は周の worktree の中の、在るディレクトリに限る
    (プラグインルートは不可)。前置きの形でなければ None(呼び出し側が「決まった形でない cd」で拒否する)。
    `-` で始まる <P> を拒否するのは、`cd -P -- -` が `--` の後でも $OLDPWD へ移るため。`~` を含む <P> を拒否する
    のは、bash が展開する `~`(先頭・代入の形の `=`・`:` の直後)としない `~`(引用された文字が続く)をここで写すと
    行き先がずれるため(is_cd_form と同じ)。
    続き(cmds[1:]・ops[1:])の判定は decide_bash が行う(最初の `||`・`;` より後ろは <P> と入力の cwd の両方)。"""
    if len(cmds) < 2 or not ops or ops[0] != "&&":
        return None
    a = cmds[0]
    if a["redirs"] or len(a["assigns"]) != 1 or a["assigns"][0].text != "CDPATH=":
        return None
    aw = [w.text for w in a["words"]]
    if len(aw) != 4 or aw[:3] != ["cd", "-P", "--"]:
        return None
    target = a["words"][3]
    if target.glob() or cd_target_rejected(target.text):
        return None
    if any(c["words"] and c["words"][0].text == "cd" for c in cmds[1:]):
        return None
    full = os.path.realpath(ctx.resolve(target.text, target.tilde))
    if not inside(full, ctx.wt) or not os.path.isdir(full):
        return None
    return full


def pending_denial(ctx: Ctx) -> Denied | None:
    """ctx に溜まった判定(書き込みでない単語の保護パス・保護パスへの書き込み)を拒否に直す。無ければ None。"""
    # 書き込みでない単語が保護パスの下を指した拒否は other(protected は書き込み先だけが理由のとき — D22)
    nonwrite = [msg for rel, msg in ctx.nonwrite_hits if rel not in ctx.write_rels]
    if nonwrite:
        return Denied("other", "; ".join(nonwrite + ctx.protected_hits)[:500])
    if ctx.protected_hits:
        return Denied("protected", "; ".join(ctx.protected_hits)[:500])
    return None


def split_after_fallthrough(ops: list[str]) -> int:
    """前置きの後ろの区切り(ops)で、最初の `||` か `;` の位置 k を返す(コマンド列の k+1 番目から後ろが、cd が
    失敗しても動きうる部分)。無ければ len(ops)(後ろは無い)。"""
    for k, op in enumerate(ops):
        if op in ("||", ";"):
            return k
    return len(ops)


def check_untracked_moves(cmds: list[dict]) -> None:
    """全単純コマンドの予約語と、追跡しない移動の実行を先に拒否する。"""
    for command in cmds:
        words = command["words"]
        if not words:
            continue
        index = 0
        ordinary = False
        # 空引用符も含む引用・エスケープがあれば予約語でない。予約語を使う制御構文は追跡しないため拒否する。
        if index < len(words) and words[index].text in RESERVED_PREFIXES and not words[index].had_quote:
            raise other(f"追跡しない予約語の前置き: {words[index].text}")
        wrapped = False
        while index < len(words):
            name = words[index].text
            if name == "builtin":
                wrapped = True
                index += 1
                if index < len(words) and words[index].text == "--":
                    index += 1
                continue
            if name != "command":
                break
            wrapped = True
            index += 1
            while index < len(words):
                option = words[index].text
                if option == "--":
                    index += 1
                    break
                if not option.startswith("-") or option == "-":
                    break
                letters = option[1:]
                # -v/-V は実行でなく照会。未知の option は command が失敗して移動しないので通常の判定へ残す。
                if "v" in letters or "V" in letters or not letters or any(ch != "p" for ch in letters):
                    ordinary = True
                    break
                index += 1
            if ordinary:
                break
        if ordinary:
            continue
        if index >= len(words):
            continue
        target = words[index].text
        if target in ("pushd", "popd") or (wrapped and target == "cd"):
            raise other(f"追跡しないディレクトリ移動: {target}")


def is_sensitive_tool(word: str) -> bool:
    """直接の git/gh と、path で隠した同名実行ファイルを同じものとして扱う。"""
    return os.path.basename(word) in {"git", "gh"}


def bash_script_mentions_sensitive_tool(script: Word) -> bool:
    """`bash -c` の中の Git/GH は外側の allow に落とさず拒否する。"""
    try:
        tokens = tokenize(script.text)
        commands, _ = parse(tokens)
    except Denied:
        return True
    return (any(is_sensitive_tool(word.text) for command in commands for word in command["words"])
            or bool(re.search(r"(?:^|[^A-Za-z0-9_])(?:git|gh)(?:$|[^A-Za-z0-9_])", script.text)))


def check_sensitive_wrappers(words: list[Word]) -> None:
    """専用 grammar を env/command/bash -c で迂回させない。

    `command -v git` は実行しない照会なので既存の許可リスト判定へ残す。
    """
    name = words[0].text
    if is_sensitive_tool(name) and name not in {"git", "gh"}:
        raise other("Git/GH の path ラッパーを受け付けない")
    if name == "env":
        # `env -S 'git …'` は 1 語の中へ command line を再解釈するため、部分的な
        # argv 解釈では安全に正規化できない。無人経路に env は不要なので閉じる。
        raise other("env ラッパーを受け付けない")
    if name == "command":
        # command(1) の問い合わせは、実行対象へ渡す argv を含まない厳密な
        # ``command -v NAME`` / ``command -V NAME`` だけに限る。後ろの Git
        # option にある ``-v`` を command の照会 option と誤認しない。
        query = (len(words) == 3 and words[1].text in {"-v", "-V"})
        if not query and any(is_sensitive_tool(word.text) for word in words[1:]):
            raise other("command 経由の Git/GH を受け付けない")
        return
    if name in {"bash", "sh"}:
        for index, word in enumerate(words[1:], 1):
            if word.text == "-c" and index + 1 < len(words):
                if bash_script_mentions_sensitive_tool(words[index + 1]):
                    raise other("bash -c 経由の Git/GH を受け付けない")
                return


def decide_bash(ctx: Ctx, command: str, env: dict) -> None:
    cwd_phys = os.path.realpath(ctx.cwd)
    if not inside(cwd_phys, ctx.wt):
        raise other(f"入力の cwd が周の worktree の外: {ctx.cwd[:200]}")
    ctx.cwd = cwd_phys
    toks = tokenize(command)
    cmds, ops = parse(toks)
    check_untracked_moves(cmds)
    if "|" in ops and any(c["words"] and c["words"][0].text == "gh" for c in cmds):
        raise other("gh に pipe の stdin を渡せない")
    uses_cd = any(c["words"] and c["words"][0].text == "cd" for c in cmds)
    if not uses_cd:
        check_cmds(ctx, cmds)
        return
    # 1. `CDPATH= cd -P -- <P> && pwd -P`(全体でこれだけ。P はプラグインルートも可)は今までどおり先に判定する
    if is_cd_form(ctx, cmds, ops):
        return
    # 2. cd の前置き `CDPATH= cd -P -- <P> && <続き>`。bash の優先順位は `|` が最強、`&&` と `||` が同じ強さで
    #    左結合、`;` が最弱。そのため cd が実行時に失敗したとき、続きのうち最初の `||`・`;` より前は動かず(`&&` で
    #    飛ばされる)、そこから後ろは動きうる(`||` は失敗で、`;` は無条件で次へ進む)。動きうる後ろは、cd が効いた
    #    <P> と、効かなかった入力の cwd のどちらでも同じ規則で通るときだけ許す(先に cd を 1 文で打ってから続きを
    #    打つのと同じ権限に保つ)
    prefix = cd_prefix_target(ctx, cmds, ops)
    if prefix is None:
        raise other("決まった形でない cd")
    rest, rest_ops = cmds[1:], ops[1:]
    k = split_after_fallthrough(rest_ops)
    input_cwd = ctx.cwd
    # 続きの全体を <P> を作業ディレクトリとして判定する
    ctx.cwd = prefix
    after = rest[k + 1:]
    if not after:
        check_cmds(ctx, rest)
        return  # decide が ctx の判定を返す
    # 最初の `||`・`;` より後ろは、入力の cwd を作業ディレクトリとして別の Ctx でも判定する(状態を混ぜない)。
    # 2 つの判定は、その場で投げた拒否も受け止めて、どちらも必ず最後まで行う(各判定の結果は、その場で投げた拒否・
    # 溜めた拒否・なしのどれか)。両方の結果を合わせる: どちらかに other があれば other、そうでなく protected が
    # あれば protected、どちらも無ければ allow。理由は、各判定の理由をそれぞれ 240 字までに切ってからつなぐ(つないだ
    # 後に切ると、1 回目の理由が長いときに 2 回目の理由が消える。合計は 240 + 2 + 240 = 482 字で 500 字を超えない)
    first = judge_cmds(ctx, rest)
    second = judge_cmds(Ctx(env, input_cwd), after)
    denials = [d for d in (first, second) if d is not None]
    if denials:
        kind = "other" if any(d.kind == "other" for d in denials) else "protected"
        raise Denied(kind, "; ".join(d.reason[:240] for d in denials))


def judge_cmds(ctx: Ctx, cmds: list[dict]) -> Denied | None:
    """check_cmds で判定し、その場で投げた拒否(投げたところでこの判定は終わる)を投げずに返す。投げなければ ctx に
    溜まった拒否(pending_denial)を返し、それも無ければ None。"""
    try:
        check_cmds(ctx, cmds)
    except Denied as exc:
        return exc
    return pending_denial(ctx)


ENVIRONMENT_LOADER = 'import os,sys,stat,re,hashlib,json,signal\n\ndef load_guard(expected, path):\n    if not re.fullmatch("[a-f0-9]{64}", expected):\n        raise RuntimeError("hash")\n    parts = path.split("/")\n    if not path.startswith("/") or len(parts) > 129 or any(p in ("", ".", "..") for p in parts[1:]):\n        raise RuntimeError("path")\n    def expired(*unused):\n        raise RuntimeError("timeout")\n    def identity(st):\n        return st.st_dev, st.st_ino, st.st_mode\n    def version(st):\n        return identity(st), st.st_size, st.st_mtime_ns, st.st_ctime_ns\n    previous = signal.signal(signal.SIGALRM, expired)\n    signal.setitimer(signal.ITIMER_REAL, 15)\n    fd = None\n    try:\n        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)\n        for name in parts[1:-1]:\n            before = os.stat(name, dir_fd=fd, follow_symlinks=False)\n            if not stat.S_ISDIR(before.st_mode):\n                raise RuntimeError("directory")\n            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)\n            os.close(fd); fd = child\n            if identity(before) != identity(os.fstat(fd)):\n                raise RuntimeError("directory changed")\n        before = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)\n        if not stat.S_ISREG(before.st_mode) or before.st_size > 1048576:\n            raise RuntimeError("file")\n        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)\n        try:\n            if version(before) != version(os.fstat(child)):\n                raise RuntimeError("file changed")\n            raw = b""\n            while len(raw) < before.st_size:\n                chunk = os.read(child, min(65536, before.st_size - len(raw)))\n                if not chunk:\n                    raise RuntimeError("short read")\n                raw += chunk\n            if os.read(child, 1) or version(before) != version(os.fstat(child)):\n                raise RuntimeError("file changed")\n            if version(before) != version(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)):\n                raise RuntimeError("path changed")\n            if hashlib.sha256(raw).hexdigest() != expected:\n                raise RuntimeError("hash mismatch")\n            return raw\n        finally:\n            os.close(child)\n    finally:\n        if fd is not None:\n            os.close(fd)\n        signal.setitimer(signal.ITIMER_REAL, 0)\n        signal.signal(signal.SIGALRM, previous)\n\nargs = []\ntry:\n    expected, path, *args = sys.argv[1:]\n    raw = load_guard(expected, path)\n    sys.argv = [path, *args]\n    exec(compile(raw, path, "exec"), {"__name__":"__main__", "__file__":path})\nexcept (Exception, SystemExit) as exc:\n    if isinstance(exc, SystemExit) and exc.code in (0, None):\n        raise\n    if args[:1] == ["hook"]:\n        print(json.dumps({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"deny","message":"環境の保持値との照合に失敗"}}}))\n        raise SystemExit(0)\n    print("ERROR [environment-guard] 保持した検査用コピーを安全に実行できない", file=sys.stderr)\n    raise SystemExit(20)\n'


def environment_command(ctx: Ctx, command: dict) -> bool:
    """外部の状態へ届く例外は、保持した固定 loader と引数の verify だけ。"""
    words = [w.text for w in command['words']]
    held = ctx.environment
    guard = held.get('environment_guard')
    state = held.get('environment_state')
    expected = held.get('environment_sha256')
    guard_hash = held.get('environment_guard_sha256')
    if not guard or not state or not expected or not guard_hash:
        return False
    verify = ['python3', '-I', '-B', '-c', ENVIRONMENT_LOADER, guard_hash, guard, 'verify', '--state', state, '--expect-sha256', expected]
    if words != verify:
        # 名前が一致する guard の未知のモードを一般の python 許可へ戻さない。
        if guard in words:
            raise other('環境照合器は固定本文と保持引数の verify だけ')
        return False
    if command['assigns'] or command['redirs']:
        raise other('環境照合器に代入・リダイレクトを付けない')
    if not all(re.fullmatch('[a-f0-9]{64}', x) for x in (expected, guard_hash)):
        raise other('環境の保持 hash が不正')
    for path, digest in ((guard, guard_hash), (state, expected)):
        parts = os.path.abspath(path).split('/')[1:]
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for name in parts[:-1]:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            before = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            limit = 1048576 if path == guard else 8388608
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise other('環境の控えが通常ファイルでないか上限を超える')
            child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                    raise other('環境の控えが読取前に変わった')
                raw = b''
                while len(raw) < before.st_size:
                    chunk = os.read(child, min(65536, before.st_size - len(raw)))
                    if not chunk: raise other('環境の控えが短くなった')
                    raw += chunk
                if os.read(child, 1) or hashlib.sha256(raw).hexdigest() != digest:
                    raise other('環境の控えが保持 hash と違う')
            finally:
                os.close(child)
        finally:
            os.close(fd)
    return True


def check_cmds(ctx: Ctx, cmds: list[dict]) -> None:
    """コマンドの並びを、ctx.cwd を作業ディレクトリとして通常の規則で判定する(cd を含まないこと)。"""
    for c in cmds:
        # 読む場所を削除・書き込み先として扱う例外は、同じコマンド内だけに限る。
        # 書き込みの拒否は後続の other を優先できるよう残し、読み取りの拒否はここで確定する。
        previous = pending_denial(ctx)
        if previous is not None and previous.kind == "other":
            raise previous
        ctx.nonwrite_hits.clear()
        ctx.write_rels.clear()
        for w in c["assigns"] + c["words"]:
            if w.glob():
                raise other(f"引用符の外のグロブ: {w.text[:100]}")
        for w in c["assigns"]:
            check_assignment(ctx, w)
        words = c["words"]
        name = words[0].text if words else ""
        for op, fd, target in c["redirs"]:
            if target is None:
                continue
            if target.glob():
                raise other("リダイレクトの先に引用符の外のグロブ")
            if name == "gh" and op == "<":
                # gh の stdin は check_gh が reviews の通常ファイル 1 件かを検査する。
                continue
            path = ctx.resolve(target.text, target.tilde)
            if op == "<":
                loc, full, _ = locate(path)
                if not (loc == "/dev/null" or ((inside(loc, ctx.wt) or inside(loc, ctx.pr))
                                               and (inside(full, ctx.wt) or inside(full, ctx.pr)))):
                    raise other(f"読み込み元が周の worktree とプラグインルートの外: {target.text[:200]}")
            else:
                ctx.check_write(path, "リダイレクトの先", allow_devnull=True)
        if not words:
            continue
        check_sensitive_wrappers(words)
        if name == "export":
            if len(words) < 2:
                raise other("export の後に代入が無い")
            for w in words[1:]:
                if w.assignment() is None:
                    raise other(f"export の後に代入でない単語: {w.text[:100]}")
                check_assignment(ctx, w)
            continue
        if name == "git":
            check_git_words_as_paths(ctx, words[1:])
            # `git > protected` は、subcommand 欠落より既存どおり書き込み先の分類を返す。
            if len(words) == 1:
                pending = pending_denial(ctx)
                if pending is not None:
                    raise pending
            check_git(ctx, words[1:])
            require_allow(ctx, words)
            continue
        if name == "gh":
            check_gh(ctx, c)
            require_allow(ctx, words)
            continue
        if name == "sed":
            check_sed(ctx, words[1:])
            pending = pending_denial(ctx)
            if pending is not None:
                raise pending
            require_allow(ctx, words)
            continue
        if name == "find":
            check_find(words[1:])
        if name == "awk":
            check_awk(words[1:])
        if environment_command(ctx, c):
            continue
        # コマンド名は従来どおり字面がパスなら検査する。PATH で解決する裸名だけは
        # 保護 cwd と symlink による裸名の追加判定の対象にしない。
        file_op = name in FILE_OP_SHORT
        check_words_as_paths(ctx, words[1:] if file_op else words, skip_bare_first=not file_op)
        if file_op:
            check_file_op(ctx, name, words[1:])
            continue
        literal = [w.text for w in words]
        if not matches_allow(literal, ctx.allow):
            raise other(f"許可リストに無いコマンド: {' '.join(literal)[:200]}")


def decide(inp: dict, env: dict) -> tuple[str, str | None, str, str]:
    """(behavior, kind, reason, subject)。"""
    tool = inp.get("tool_name")
    tin = inp.get("tool_input")
    cwd = inp.get("cwd")
    if not isinstance(tool, str) or not isinstance(tin, dict) or not isinstance(cwd, str) or not cwd:
        return "deny", "other", "入力の形が違う(tool_name・tool_input・cwd)", ""
    ctx = Ctx(env, cwd)
    subject = ""
    try:
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            key = "notebook_path" if tool == "NotebookEdit" else "file_path"
            target = tin.get(key)
            if not isinstance(target, str) or not target:
                raise other(f"{key} が無い")
            subject = target
            # (a) 対象が W の中なら allow(保護パスの外の worktree の中への書き込みも、ここでは許さない)
            loc, full, link = locate(ctx.resolve(target, False))
            if not (inside(loc, ctx.wt) and inside(full, ctx.wt)):
                raise other(f"{tool} の対象が周の worktree の外: {target[:200]}")
            rel = ctx.rel(loc)
            if link or not ctx.in_w(rel):
                if is_protected(rel):
                    ctx.protected(f"{tool} の対象が保護パスの下で W の外: {rel[:200]}")
                else:
                    raise other(f"{tool} の対象が W の外: {rel[:200]}")
        elif tool in ("Read", "Glob", "Grep"):
            key = "file_path" if tool == "Read" else "path"
            target = tin.get(key) or cwd
            if not isinstance(target, str):
                raise other(f"{key} が文字列でない")
            subject = target
            full = os.path.realpath(ctx.resolve(target, False))
            if not inside(full, ctx.pr):
                raise other(f"{tool} の対象がプラグインルートの外: {target[:200]}")
        elif tool == "Bash":
            command = tin.get("command")
            if not isinstance(command, str):
                raise other("command が無い")
            subject = command
            if "dangerouslyDisableSandbox" in tin:
                disable_sandbox = tin["dangerouslyDisableSandbox"]
                if not isinstance(disable_sandbox, bool):
                    raise other("dangerouslyDisableSandbox が boolean でない")
                if disable_sandbox:
                    raise other("dangerouslyDisableSandbox が true")
            decide_bash(ctx, command, env)
        else:
            raise other(f"扱わないツール: {tool}")
    except Denied as exc:
        return "deny", exc.kind, exc.reason, subject
    except (OSError, ValueError) as exc:
        return "deny", "other", f"判定できない: {exc}", subject
    denial = pending_denial(ctx)
    if denial is not None:
        return "deny", denial.kind, denial.reason, subject
    return "allow", None, "", subject


def load_env() -> dict | None:
    names = {
        "worktree": "DEV_WORKFLOW_LOOP_WORKTREE", "permlog": "DEV_WORKFLOW_LOOP_PERMLOG",
        "plugin_root": "DEV_WORKFLOW_LOOP_PLUGIN_ROOT",
        "allow": "DEV_WORKFLOW_LOOP_ALLOW",
    }
    env: dict = {}
    for key, var in names.items():
        value = os.environ.get(var, "")
        if not value:
            return None
        env[key] = value
    try:
        allow = json.loads(env["allow"])
    except ValueError:
        return None
    if not isinstance(allow, list) or not all(isinstance(r, dict) for r in allow):
        return None
    env["allow"] = allow
    # 親が worktree 外で固定した公開方針。通常の Git 操作には不要なので空も許すが、
    # push の grammar は両方が無ければ閉じる。
    env["DEV_WORKFLOW_LOOP_PUSH_REPO"] = os.environ.get("DEV_WORKFLOW_LOOP_PUSH_REPO", "")
    env["DEV_WORKFLOW_LOOP_PUSH_REF"] = os.environ.get("DEV_WORKFLOW_LOOP_PUSH_REF", "")
    for key, var in {'environment_state':'DEV_WORKFLOW_ENV_STATE', 'environment_sha256':'DEV_WORKFLOW_ENV_SHA256',
                     'environment_guard':'DEV_WORKFLOW_ENV_GUARD', 'environment_guard_sha256':'DEV_WORKFLOW_ENV_GUARD_SHA256'}.items():
        if os.environ.get(var): env[key] = os.environ[var]
    return env


def emit(behavior: str, message: str) -> None:
    decision: dict = {"behavior": behavior}
    if behavior == "deny":
        decision["message"] = f"{message or '理由なし'}。{DENY_NOTE}"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}},
                     ensure_ascii=False))


def record(permlog: str, entry: dict) -> None:
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode('utf-8')
    fds = []
    try:
        parts = os.path.abspath(permlog).split('/')[1:]
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY); fds.append(fd)
        for name in parts[:-1]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd); fds.append(fd)
        fd = os.open(parts[-1], os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        fds.append(fd)
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_size + len(line) <= 1048576:
            os.write(fd, line)
    except OSError:
        pass
    finally:
        for fd in reversed(fds): os.close(fd)


def main() -> int:
    if sys.argv[1:2] == ["--is-protected"]:
        if len(sys.argv) != 3:
            print("使い方: loop-permission.py --is-protected <管理ルート相対パス>", file=sys.stderr)
            return 2
        return 0 if is_protected(sys.argv[2]) else 1
    raw = sys.stdin.buffer.read()
    env = load_env()
    permlog = os.environ.get("DEV_WORKFLOW_LOOP_PERMLOG", "")
    try:
        inp = json.loads(raw.decode("utf-8"))
        if not isinstance(inp, dict):
            raise ValueError("オブジェクトでない")
    except (ValueError, UnicodeDecodeError):
        inp = None
    if env is None or inp is None:
        reason = "環境変数が無い(か許可リストが読めない)" if env is None else "入力の JSON が読めない"
        behavior, kind, subject = "deny", "other", ""
    else:
        behavior, kind, reason, subject = decide(inp, env)
    if permlog:
        record(permlog, {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool_name": (inp or {}).get("tool_name") if isinstance(inp, dict) else None,
            "cwd": (inp or {}).get("cwd") if isinstance(inp, dict) else None,
            "decision": behavior,
            "kind": kind,
            "reason": reason,
            "subject": (subject or "")[:500],
        })
    emit(behavior, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
