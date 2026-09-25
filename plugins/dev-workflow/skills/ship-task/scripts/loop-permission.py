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
2 つの形だけを受け付ける。<P> の字面が `-` で始まるもの・`~` を含むもの(位置を問わない)は受け付けない。
判定できない入力(JSON が読めない・環境変数が無い)も deny。hook は隔離ではない(同じ利用者の権限で動く)。
標準ライブラリだけで書く。

`--is-protected <管理ルート相対パス>` で起動すると、判定をせず、そのパスが保護パスの下か(`.claude/worktrees` の
下を除く)だけを終了コードで返す(0 = 下 / 1 = 下でない)。loop.sh が task_dir の検査に使う(保護パスの列を
2 か所に置かない)。
"""

from __future__ import annotations

import json
import os
import re
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
# sed はオプションを束ねない(`-in` は接尾辞 n の -i になる)。接尾辞つきの -i・--in-place は受け付けない
SED_OPTS = {"-i", "--in-place", "-e", "-E", "-n", "-r"}

# deny の message に理由へ続けて添える固定の文(D22 ③ (d))。無人の周では打ち直さず、unattended-mode.md の
# 許可の拒否(G1)に従う(拒否されたら別の手段で回り込まない — 保留か失敗扱い)。判定の記録(PERMLOG)の reason には入れない
DENY_NOTE = (
    "loop.sh の許可の仲介が拒否した。無人の周では打ち直さず(別の手段で回り込まず)、"
    "unattended-mode.md の許可の拒否(G1)に従う"
)

REVIEWS = ".claude/reviews"
W_FILES = {".claude/grasp.md", ".claude/.understand-project-done"}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


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
        self.wt = os.path.realpath(env["worktree"])
        self.pr = os.path.realpath(env["plugin_root"])
        self.cwd = cwd
        self.home = os.environ.get("HOME", "")
        self.allow = env["allow"]
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
        rel = self.rel(loc)
        if is_protected(rel) and (link or not self.in_w(rel, mkdir=mkdir)):
            self.protected(f"{what}が保護パスの下で W の外: {rel[:200]}")

    # 削除・移動の元(rm・rmdir・mv の元)
    def check_remove(self, path: str, what: str, recursive: bool) -> None:
        loc, full, link = locate(path)
        if not inside(loc, self.wt) or (not link and not inside(full, self.wt)):
            raise other(f"{what}が周の worktree の外: {path[:200]}")
        self.mark_write(loc)
        rel = self.rel(loc)
        if not is_protected(rel):
            return
        under_reviews = rel.startswith(REVIEWS + "/")
        if link:
            ok = False
        elif os.path.isdir(loc):
            ok = under_reviews  # .claude/reviews/ の下のディレクトリだけ(そのものは除く)
        else:
            ok = under_reviews or rel in W_FILES  # W の通常ファイル(無くてもよい)
        if recursive and not under_reviews:
            ok = False
        if not ok:
            self.protected(f"{what}が保護パスの下で W の外: {rel[:200]}")

    # パスとして解ける単語(どのコマンドでも)
    def check_path_word(self, path: str) -> None:
        loc, full, link = locate(path)
        if loc == "/dev/null" or full == "/dev/null":
            return
        ok_loc = inside(loc, self.wt) or inside(loc, self.pr)
        ok_full = inside(full, self.wt) or inside(full, self.pr)
        if not (ok_loc and ok_full):
            raise other(f"パスが周の worktree とプラグインルートの外: {path[:200]}")
        if inside(loc, self.wt):
            rel = self.rel(loc)
            if is_protected(rel) and (link or not self.in_w(rel, mkdir=True)):
                self.nonwrite_hits.append((rel, f"パスが保護パスの下で W の外: {rel[:200]}"))


# ── Bash の限定の構文 ──

class Word:
    def __init__(self) -> None:
        self.chars: list[str] = []
        self.quoted: list[bool] = []

    def add(self, ch: str, quoted: bool) -> None:
        self.chars.append(ch)
        self.quoted.append(quoted)

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


def check_words_as_paths(ctx: Ctx, words: list[Word]) -> None:
    for w in words:
        text = w.text
        tilde = w.tilde
        if text.startswith("-"):
            if "=" in text and text.startswith("--"):
                value = text.split("=", 1)[1]
                # --name=値 の値の部分を見る(~ は引用符の外で値の先頭にあるときだけ)
                vtilde = value.startswith("~") and not w.quoted[text.index("=") + 1] if value else False
                if value and looks_like_path(value, vtilde):
                    ctx.check_path_word(ctx.resolve(value, vtilde))
                continue
            if "/" in text:
                raise other(f"パスを含むオプションの形を判定できない: {text[:100]}")
            continue
        if looks_like_path(text, tilde):
            ctx.check_path_word(ctx.resolve(text, tilde))


def check_assignment(ctx: Ctx, word: Word) -> None:
    name, value = word.assignment()  # type: ignore[misc]
    if name not in ASSIGN_NAMES:
        raise other(f"代入を許さない環境変数の名: {name}")
    if name == "CDPATH" and value.text != "":
        raise other("CDPATH の値は空だけ")
    if value.text and looks_like_path(value.text, value.tilde):
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
    if name == "sed":
        opts, operands = [], []
        ended = False
        k = 0
        script_given = False
        while k < len(args):
            t = args[k].text
            if not ended and t == "--":
                ended = True
            elif not ended and t.startswith("-") and t != "-":
                if t not in SED_OPTS:
                    raise other(f"sed のオプションを受け付けない: {t[:100]}")
                opts.append(t)
                if t == "-e":
                    if k + 1 >= len(args):
                        raise other("sed -e の値が無い")
                    script_given = True
                    k += 1
            else:
                operands.append(args[k])
            k += 1
        files = operands if script_given else operands[1:]
        for w in files:
            ctx.check_write(ctx.resolve(w.text, w.tilde), "sed -i の対象")
        return
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


def sed_in_place(args: list[Word]) -> bool:
    """sed が書き込むか(-i・--in-place、接尾辞つき、束ねた短いオプションの中の i を含む)。書くならファイル操作として
    判定する(束ねたオプション・接尾辞は列挙に無いので拒否になる)。`--` の後は見ない。"""
    for w in args:
        t = w.text
        if t == "--":
            return False
        if t.startswith("--in-place"):
            return True
        if t.startswith("-") and not t.startswith("--") and "i" in t[1:]:
            return True
    return False


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


def decide_bash(ctx: Ctx, command: str, env: dict) -> None:
    cwd_phys = os.path.realpath(ctx.cwd)
    if not inside(cwd_phys, ctx.wt):
        raise other(f"入力の cwd が周の worktree の外: {ctx.cwd[:200]}")
    ctx.cwd = cwd_phys
    toks = tokenize(command)
    cmds, ops = parse(toks)
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


def check_cmds(ctx: Ctx, cmds: list[dict]) -> None:
    """コマンドの並びを、ctx.cwd を作業ディレクトリとして通常の規則で判定する(cd を含まないこと)。"""
    for c in cmds:
        for w in c["assigns"] + c["words"]:
            if w.glob():
                raise other(f"引用符の外のグロブ: {w.text[:100]}")
        for w in c["assigns"]:
            check_assignment(ctx, w)
        for op, fd, target in c["redirs"]:
            if target is None:
                continue
            if target.glob():
                raise other("リダイレクトの先に引用符の外のグロブ")
            path = ctx.resolve(target.text, target.tilde)
            if op == "<":
                loc, full, _ = locate(path)
                if not (loc == "/dev/null" or ((inside(loc, ctx.wt) or inside(loc, ctx.pr))
                                               and (inside(full, ctx.wt) or inside(full, ctx.pr)))):
                    raise other(f"読み込み元が周の worktree とプラグインルートの外: {target.text[:200]}")
            else:
                ctx.check_write(path, "リダイレクトの先", allow_devnull=True)
        words = c["words"]
        if not words:
            continue
        name = words[0].text
        if name == "export":
            if len(words) < 2:
                raise other("export の後に代入が無い")
            for w in words[1:]:
                if w.assignment() is None:
                    raise other(f"export の後に代入でない単語: {w.text[:100]}")
                check_assignment(ctx, w)
            continue
        is_sed_inplace = name == "sed" and sed_in_place(words[1:])
        check_words_as_paths(ctx, words[1:] if (name in FILE_OP_SHORT or is_sed_inplace) else words)
        if name in FILE_OP_SHORT or is_sed_inplace:
            check_file_op(ctx, "sed" if is_sed_inplace else name, words[1:])
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
    return env


def emit(behavior: str, message: str) -> None:
    decision: dict = {"behavior": behavior}
    if behavior == "deny":
        decision["message"] = f"{message or '理由なし'}。{DENY_NOTE}"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}},
                     ensure_ascii=False))


def record(permlog: str, entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        fd = os.open(permlog, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


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
