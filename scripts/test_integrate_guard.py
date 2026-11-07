"""Regression tests for ship-task/scripts/integrate-guard.py (Issue #371).

Every case runs the helper against throw-away repositories: a local bare
"origin", a work repository, and a second clone that advances the base branch.
No network is used.  A fake `git` answers only the `remote get-url` questions
(so origin-repo.py sees github.com/o/r) and can fail `merge`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support_git
from support_cli import run_python


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "plugins/dev-workflow/skills/ship-task/scripts/integrate-guard.py"
SNAPSHOT = ROOT / "plugins/dev-workflow/skills/do-task/scripts/diff-snapshot.sh"
ERE = r"(^|/)[.]env($|[.])"
# 保守を止める 2 つ(maintenance.auto・gc.auto)は support_git.QUIET に任せる
NEUTRAL = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=")
HOOKS = ("pre-merge-commit", "post-merge", "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit")
UNTRACK = object()  # commit_work: stop tracking the path but keep the file
F_LINES = [f"line{n:02d}" for n in range(1, 31)]


class Run:
    def __init__(self, proc: subprocess.CompletedProcess) -> None:
        self.rc, self.out, self.err = proc.returncode, proc.stdout, proc.stderr
        try:
            self.data = json.loads(self.out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            self.data = None


def f_text(changes: dict[int, str] | None = None, insert_after: dict[int, str] | None = None) -> str:
    lines = list(F_LINES)
    for number, text in (changes or {}).items():
        lines[number - 1] = text
    for number, text in sorted((insert_after or {}).items(), reverse=True):
        lines.insert(number, text)
    return "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.real_git = shutil.which("git")
        assert self.real_git
        self.origin = self.root / "origin.git"
        self.wt = self.root / "wt"
        self.adv = self.root / "adv"
        self.markers = self.root / "markers"
        self.fakebin = self.root / "bin"
        self.markers.mkdir()
        self.fakebin.mkdir()
        self.make_fake_git()
        self.build()

    # ------------------------------------------------------------ fixtures
    def make_fake_git(self) -> None:
        stub = self.fakebin / "git"
        stub.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *' remote get-url --push --all origin '*) printf '%s\\n' \"${IG_PUSH_URL:-${IG_URL:-https://github.com/o/r.git}}\"; exit 0;;\n"
            "  *' remote get-url --all origin '*) printf '%s\\n' \"${IG_URL:-https://github.com/o/r.git}\"; exit 0;;\n"
            "esac\n"
            "case \" $* \" in *' merge --no-ff --no-commit '*) if [ -n \"${IG_FAIL_MERGE:-}\" ]; then exit 128; fi\n"
            "  if [ -n \"${IG_HANG_MERGE:-}\" ]; then " + self.real_git + " \"$@\"; [ -e \"${IG_MERGE_HEAD_FILE:-/nonexistent}\" ] && touch \"${IG_MERGE_MARK:-/dev/null}\"; exec sleep 60; fi;; esac\n"
            "case \" $* \" in *' merge --no-ff --no-commit '*) if [ -n \"${IG_SLEEP_MERGE:-}\" ]; then exec sleep 60; fi;; esac\n"
            "case \" $* \" in *' merge --abort '*) if [ -n \"${IG_HANG_ABORT:-}\" ]; then exec sleep 60; fi;; esac\n"
            f"exec {self.real_git} \"$@\"\n", encoding="utf-8")
        stub.chmod(0o755)

    def git(self, *args: str, cwd: Path | None = None, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        proc = support_git.git(*args, cwd=cwd or self.wt, env={**os.environ, **(env or {})}, check=False,
                               binary=self.real_git, options=NEUTRAL, stdin=None)
        if check and proc.returncode:
            raise AssertionError(f"git {args}: {proc.stderr}")
        return proc

    def write(self, path: str, content, base: Path | None = None) -> None:
        target = (base or self.wt) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    def build(self, *, secret: bool = True, extra: dict | None = None, many: int = 0) -> None:
        for path in (self.origin, self.wt, self.adv):
            shutil.rmtree(path, ignore_errors=True)
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.root)
        self.git("init", "-q", "-b", "main", str(self.wt), cwd=self.root)
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("remote", "add", "origin", str(self.origin))
        files = {"a.txt": "a\n", "f.txt": f_text(), "CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n- base\n",
                 "sub/old.txt": "old\n", "dc/c.txt": "c\n", ".gitignore": "*.log\nlocal.cfg\n", "local.cfg": "cfg\n",
                 "keep.cfg": "keep\n", "version.txt": "1\n", "bin.dat": b"\x00\x01base", "conf/secret.ini": "s=1\n"}
        if secret:
            files[".env.local"] = "secret=1\n"
        files.update(extra or {})
        for number in range(many):
            files[f"many/{number:03d}.txt"] = "x\n"
        for path, content in files.items():
            self.write(path, content)
        self.git("add", "-f", "--all")
        self.git("commit", "-qm", "base")
        self.git("push", "-q", "-u", "origin", "main")
        self.git("tag", "v1")
        self.git("push", "-q", "origin", "v1")
        self.git("tag", "-d", "v1")
        self.git("switch", "-q", "-c", "task/x")
        self.commit_work({"work.txt": "work\n"})
        self.git("clone", "-q", str(self.origin), str(self.adv), cwd=self.root)
        self.git("config", "user.name", "adv", cwd=self.adv)
        self.git("config", "user.email", "adv@example.invalid", cwd=self.adv)
        hooks = self.root / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in HOOKS:
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\ntouch {self.markers / name}\n", encoding="utf-8")
            hook.chmod(0o755)
        monitor = self.root / "monitor"
        monitor.write_text(f"#!/bin/sh\ntouch {self.markers / 'fsmonitor'}\nprintf '%s\\n' 2\nprintf '%s\\n' token\n", encoding="utf-8")
        monitor.chmod(0o755)
        support_git.git("config", "core.hooksPath", str(hooks), cwd=self.wt, binary=self.real_git, text=False, stdin=None)
        support_git.git("config", "core.fsmonitor", str(monitor), cwd=self.wt, binary=self.real_git, text=False, stdin=None)

    def commit_work(self, changes: dict, message: str = "work") -> str:
        for path, content in changes.items():
            if content is UNTRACK:
                self.git("rm", "-q", "--cached", "--", path)
            elif content is None:
                self.git("rm", "-q", "-f", "--", path)
            else:
                self.write(path, content)
                self.git("add", "-f", "--", path)
        self.git("commit", "-qm", message)
        return self.D()

    def advance(self, changes: dict, message: str = "base moves") -> str:
        self.git("pull", "-q", "--ff-only", "origin", "main", cwd=self.adv)
        for path, content in changes.items():
            if content is None:
                self.git("rm", "-q", "-f", "--", path, cwd=self.adv)
            else:
                self.write(path, content, self.adv)
                self.git("add", "-f", "--", path, cwd=self.adv)
        self.git("commit", "-qm", message, cwd=self.adv)
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.adv)
        return self.git("rev-parse", "HEAD", cwd=self.adv).stdout.strip()

    # ------------------------------------------------------------ helpers
    def D(self) -> str:
        return self.rev("refs/heads/task/x")

    def rev(self, name: str) -> str:
        return self.git("rev-parse", name).stdout.strip()

    def merge_head(self) -> str | None:
        proc = self.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def unmerged(self) -> list[str]:
        return [line for line in self.git("ls-files", "-u").stdout.splitlines() if line]

    def stages(self, path: str) -> set[int]:
        return {int(line.split()[2]) for line in self.git("ls-files", "-u", "--", path).stdout.splitlines()}

    def refs(self) -> dict[str, str]:
        out = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout
        return dict(line.split(" ", 1) for line in out.splitlines())

    def parents(self, rev: str = "HEAD") -> list[str]:
        return self.git("rev-list", "--parents", "-n", "1", rev).stdout.split()[1:]

    def body(self, rev: str = "HEAD") -> str:
        return self.git("log", "-1", "--format=%B", rev).stdout

    def no_markers(self) -> None:
        self.assertEqual([], sorted(path.name for path in self.markers.iterdir()))

    def ere_args(self, eres: list[str] | None = None) -> list[str]:
        out: list[str] = []
        for value in (eres if eres is not None else [ERE]):
            out += ["--exclude-ere", value]
        return out

    def guard(self, argv: list[str], *, env: dict | None = None, timeout: int = 120) -> Run:
        full_env = {**os.environ, "PATH": f"{self.fakebin}:{os.environ['PATH']}", **(env or {})}
        proc = run_python(str(GUARD), *argv, flags=("-B",), env=full_env, timeout=timeout, stdin=None)
        return Run(proc)

    def margs(self, *, sha: str | None = None, branch: str = "task/x", base: str = "main", repo: str = "github.com/o/r",
              eres: list[str] | None = None) -> list[str]:
        return ["merge", "--dir", str(self.wt), "--branch", branch, "--reviewed-sha", sha or self.D(), "--base", base,
                "--repo", repo, *self.ere_args(eres)]

    def merge(self, *, env: dict | None = None, **kw) -> Run:
        return self.guard(self.margs(**kw), env=env)

    def save(self, name: str, text: str) -> tuple[str, str]:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path), hashlib.sha256(text.encode()).hexdigest()

    def rargs(self, sub: str, saved: tuple[str, str], d: str, b: str, eres: list[str] | None = None,
              branch: str = "task/x") -> list[str]:
        return [sub, "--dir", str(self.wt), "--branch", branch, "--reviewed-sha", d, "--base-sha", b,
                *self.ere_args(eres), "--merge-result", saved[0], "--expect-merge-result-sha256", saved[1]]

    def conflict_f(self) -> tuple[str, str, Run, tuple[str, str]]:
        self.commit_work({"f.txt": f_text({10: "mine"})})
        self.advance({"f.txt": f_text({10: "theirs"})})
        d = self.D()
        result = self.merge()
        self.assertEqual((3, "conflict"), (result.rc, result.data["reason"]), result.err)
        return d, result.data["base_sha"], result, self.save("merge.json", result.out)

    def resolve_f(self) -> None:
        self.write("f.txt", f_text({10: "resolved"}))

    def assert_untouched(self, d: str, refs: dict[str, str]) -> None:
        self.assertEqual(d, self.D())
        self.assertEqual(d, self.rev("HEAD"))
        self.assertIsNone(self.merge_head())
        heads = lambda mapping: {name: oid for name, oid in mapping.items() if name.startswith("refs/heads/")}
        self.assertEqual(heads(refs), heads(self.refs()))

    def assert_review(self, result: Run, conflict: bool) -> None:
        """needs-review leaves D as head, MERGE_HEAD = B, and conflict entries only when expected."""
        d = result.data["reviewed_sha"]
        self.assertEqual(d, result.data["head"])
        self.assertEqual(d, self.D())
        self.assertEqual(d, self.rev("HEAD"))
        self.assertEqual(result.data["base_sha"], self.merge_head())
        self.assertEqual(conflict, bool(self.unmerged()))

    def assert_done(self, result: Run) -> None:
        """merged / up-to-date: HEAD, parents, no MERGE_HEAD, no unresolved entries."""
        d, b = result.data["reviewed_sha"], result.data["base_sha"]
        self.assertEqual(self.rev("HEAD"), result.data["head"])
        self.assertEqual(self.D(), result.data["head"])
        self.assertIsNone(self.merge_head())
        self.assertEqual([], self.unmerged())
        if result.data["result"] == "merged":
            self.assertEqual([d, b], self.parents())
        else:
            self.assertEqual(d, result.data["head"])

    def reject(self, argv_or_run, *, message: str | None = None) -> Run:
        result = argv_or_run if isinstance(argv_or_run, Run) else self.guard(argv_or_run)
        self.assertEqual(1, result.rc, result.err)
        if message:
            self.assertIn(message, result.err)
        return result

    def crisscross(self, extra_base: dict | None = None, y1_changes: dict | None = None) -> tuple[str, str]:
        """D (task/x) and B (origin/main) with two merge bases.  Returns (D, B)."""
        x1 = self.D()
        self.git("pull", "-q", "--ff-only", "origin", "main", cwd=self.adv)
        self.write("y1.txt", "y1\n", self.adv)
        self.git("add", "y1.txt", cwd=self.adv)
        for path, content in (y1_changes or {}).items():
            self.git("rm", "-q", "-f", "--", path, cwd=self.adv) if content is None else None
        self.git("commit", "-qm", "y1", cwd=self.adv)
        y1 = self.git("rev-parse", "HEAD", cwd=self.adv).stdout.strip()
        self.git("fetch", "-q", str(self.wt), "task/x:refs/heads/x1", cwd=self.adv)
        self.git("merge", "-q", "--no-ff", "-m", "y2", "x1", cwd=self.adv)
        for path, content in (extra_base or {}).items():
            self.write(path, content, self.adv)
            self.git("add", "-f", path, cwd=self.adv)
            self.git("commit", "-qm", "y3", cwd=self.adv)
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.adv)
        b = self.git("rev-parse", "HEAD", cwd=self.adv).stdout.strip()
        self.git("fetch", "-q", str(self.adv), "main:refs/heads/ymain")
        self.git("merge", "-q", "--no-ff", "-m", "x2", y1)
        self.assertEqual(x1, self.parents()[0])
        return self.D(), b


# ------------------------------------------------------------------ merge

class MergeTest(Base):
    def test_01_up_to_date(self) -> None:
        d = self.D()
        result = self.merge()
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual(("up-to-date", d), (result.data["result"], result.data["head"]))
        self.assertEqual(d, self.D())
        self.assertIsNone(self.merge_head())
        self.assertEqual([], self.unmerged())
        self.assert_done(result)

    def test_02_other_file_merges(self) -> None:
        d = self.D()
        b = self.advance({"other.txt": "other\n"})
        result = self.merge()
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual("merged", result.data["result"])
        self.assertEqual([d, b], self.parents())
        self.assertEqual(self.D(), result.data["head"])
        self.assertEqual([], result.data["overlap"])
        self.assertEqual("Merge remote-tracking branch 'origin/main' into task/x", self.git("log", "-1", "--format=%s").stdout.strip())
        self.assertFalse([line for line in self.body().splitlines() if line.startswith("#")])
        self.assertIsNone(self.merge_head())
        self.assertEqual([], self.unmerged())
        self.no_markers()
        self.assert_done(result)

    def test_03_only_two_refs_move_and_tag_is_not_fetched(self) -> None:
        self.advance({"other.txt": "other\n"})
        before = self.refs()
        result = self.merge()
        self.assertEqual(0, result.rc, result.err)
        after = self.refs()
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        self.assertEqual({"refs/heads/task/x", "refs/remotes/origin/main"}, changed)
        self.assertNotIn("refs/tags/v1", after)

    def test_04_distant_lines_of_the_same_file_merge(self) -> None:
        self.commit_work({"f.txt": f_text({25: "mine25"})})
        self.advance({"f.txt": f_text(insert_after={3: "inserted"})})
        result = self.merge()
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual(("merged", ["f.txt"]), (result.data["result"], result.data["overlap"]))
        self.assert_done(result)

    def test_05_near_lines_need_review(self) -> None:
        self.commit_work({"f.txt": f_text({15: "mine15"})})
        d = self.D()
        b = self.advance({"f.txt": f_text({17: "theirs17"})})
        result = self.merge()
        self.assertEqual(3, result.rc, result.err)
        self.assertEqual(("needs-review", "diff-changed", ["f.txt"]),
                         (result.data["result"], result.data["reason"], result.data["problem_paths"]))
        self.assertEqual(d, self.D())
        self.assertEqual(b, self.merge_head())
        self.assertEqual([], self.unmerged())
        self.assertTrue(result.data["index_sha256"])
        self.assert_review(result, False)

    def test_06_same_line_conflicts_in_diff3_form(self) -> None:
        _d, _b, result, _saved = self.conflict_f()
        self.assertEqual(["f.txt"], result.data["problem_paths"])
        self.assertTrue(self.unmerged())
        self.assertIn("|||||||", (self.wt / "f.txt").read_text())
        self.assertEqual([], result.data["binary_conflicts"])
        self.assert_review(result, True)

    def test_07_deleted_by_work_changed_by_base(self) -> None:
        self.commit_work({"a.txt": None})
        self.advance({"a.txt": "changed by base\n"})
        result = self.merge()
        self.assertEqual((3, "conflict"), (result.rc, result.data["reason"]), result.err)
        self.assertEqual({1, 3}, self.stages("a.txt"))
        self.assertEqual("changed by base\n", (self.wt / "a.txt").read_text())
        self.assertEqual([], result.data["binary_conflicts"])
        self.assert_review(result, True)

    def test_08_work_already_in_base(self) -> None:
        d = self.D()
        self.git("push", "-q", "origin", "task/x:main")
        self.advance({"other.txt": "other\n"})
        refs = self.refs()
        self.reject(self.margs(), message=None)
        self.assert_untouched(d, refs)

    def test_09_two_merge_bases(self) -> None:
        d, b = self.crisscross()
        self.assertEqual(2, len(self.git("merge-base", "--all", d, b).stdout.split()))
        result = self.merge()
        self.assertEqual((3, "needs-review", "multiple-merge-bases"),
                         (result.rc, result.data["result"], result.data["reason"]), result.err)
        self.assertEqual(b, self.merge_head())
        self.assert_review(result, False)

    def test_10_rerere_is_not_applied(self) -> None:
        self.commit_work({"f.txt": f_text({10: "mine"})})
        d = self.D()
        self.advance({"f.txt": f_text({10: "theirs"})})
        self.git("config", "rerere.enabled", "true")
        self.git("config", "rerere.autoUpdate", "true")
        self.git("fetch", "-q", "origin")
        self.assertNotEqual(0, self.git("merge", "--no-ff", "-q", "origin/main", check=False).returncode)
        self.write("f.txt", f_text({10: "resolved"}))
        self.git("add", "f.txt")
        self.git("commit", "-qm", "resolved")
        self.git("reset", "-q", "--hard", d)
        result = self.merge()
        self.assertEqual((3, "conflict"), (result.rc, result.data["reason"]), result.err)
        self.assertTrue(self.unmerged())
        self.assert_review(result, True)

    def test_11_minus_diff_attribute_is_still_compared(self) -> None:
        self.commit_work({".gitattributes": "f.txt -diff\n", "f.txt": f_text({15: "mine15"})})
        self.advance({"f.txt": f_text({17: "theirs17"})})
        result = self.merge()
        self.assertEqual((3, "diff-changed"), (result.rc, result.data["reason"]), result.err)
        self.assert_review(result, False)

    def test_12_merge_fails_before_it_starts(self) -> None:
        d = self.D()
        self.advance({"other.txt": "other\n"})
        refs = self.refs()
        self.reject(self.merge(env={"IG_FAIL_MERGE": "1"}), message="取り込みを始められない")
        self.assert_untouched(d, refs)
        self.assertEqual("", self.git("status", "--porcelain", "--untracked-files=no").stdout)

    def test_13_secret_changed_by_both(self) -> None:
        self.commit_work({".env.local": "mine\n"})
        d = self.D()
        self.advance({".env.local": "theirs\n"})
        refs = self.refs()
        result = self.merge()
        self.assertEqual((4, "secret-overlap", [".env.local"]), (result.rc, result.data["result"], result.data["problem_paths"]), result.err)
        self.assert_untouched(d, refs)
        self.assertEqual("mine\n", (self.wt / ".env.local").read_text())

    def test_13b_secret_changed_identically_by_both(self) -> None:
        self.commit_work({".env.local": "same\n"})
        d = self.D()
        self.advance({".env.local": "same\n"})
        refs = self.refs()
        result = self.merge()
        self.assertEqual((4, "secret-overlap", [".env.local"]), (result.rc, result.data["result"], result.data["problem_paths"]), result.err)
        self.assert_untouched(d, refs)

    def test_14_secret_changed_by_base_only(self) -> None:
        self.advance({".env.local": "theirs\n"})
        result = self.merge()
        self.assertEqual((0, "merged"), (result.rc, result.data["result"]), result.err)
        self.assert_done(result)

    def test_15_ignored_file_with_the_name_of_an_added_file(self) -> None:
        d = self.D()
        self.advance({"build.log": "from base\n"})
        self.write("build.log", "mine\n")
        refs = self.refs()
        self.reject(self.margs(), message="取り込み先が足すファイルと同じ名前のものが作業ツリーにある")
        self.assert_untouched(d, refs)
        self.assertEqual("mine\n", (self.wt / "build.log").read_text())

    def test_16_untracked_file_with_the_name_of_an_added_file(self) -> None:
        d = self.D()
        self.advance({"new.txt": "from base\n"})
        self.write("new.txt", "mine\n")
        refs = self.refs()
        self.reject(self.margs(), message="取り込み先が足すファイルと同じ名前のものが作業ツリーにある")
        self.assert_untouched(d, refs)
        self.assertEqual("mine\n", (self.wt / "new.txt").read_text())

    def test_17_minus_merge_attribute_leaves_a_conflict_without_markers(self) -> None:
        self.commit_work({".gitattributes": "bin.dat -merge\n", "bin.dat": b"\x00D"})
        self.advance({"bin.dat": b"\x00B"})
        result = self.merge()
        self.assertEqual((3, "conflict", ["bin.dat"]), (result.rc, result.data["reason"], result.data["binary_conflicts"]), result.err)
        self.assert_review(result, True)

    def filter_setup(self) -> Path:
        self.commit_work({".gitattributes": "*.txt filter=x\n"})
        marker = self.root / "filter-ran"
        self.git("config", "filter.x.clean", f"touch {marker}; cat")
        self.write("a.txt", "A\n")  # same byte count as "a\n", so status would re-read it and run the filter
        return marker

    def test_18_filter_stops_before_the_work_tree_is_read(self) -> None:
        self.advance({"other.txt": "other\n"})
        marker = self.filter_setup()
        before = self.rev("refs/remotes/origin/main")
        self.reject(self.margs(), message="事前検査が止めた")
        self.assertFalse(marker.exists())
        self.assertEqual(before, self.rev("refs/remotes/origin/main"))

    def test_19_attribute_added_by_base_stops_after_the_merge(self) -> None:
        d = self.D()
        self.advance({".gitattributes": "*.txt ident\n"})
        self.reject(self.margs(), message="事前検査が止めた")
        self.assertEqual(d, self.D())
        self.assertIsNone(self.merge_head())

    def test_20_file_added_below_an_existing_folder(self) -> None:
        self.advance({"sub/new.txt": "new\n"})
        result = self.merge()
        self.assertEqual((0, "merged"), (result.rc, result.data["result"]), result.err)
        self.assert_done(result)

    def test_21_parent_is_a_file(self) -> None:
        d = self.D()
        self.advance({"sub2/new.txt": "new\n"})
        self.write("sub2", "a file\n")
        refs = self.refs()
        self.reject(self.margs())
        self.assert_untouched(d, refs)

    def test_22_untracked_and_ignored_file_changed_by_base(self) -> None:
        self.commit_work({"local.cfg": UNTRACK})
        d = self.D()
        self.advance({"local.cfg": "changed by base\n"})
        self.assertEqual("cfg\n", (self.wt / "local.cfg").read_text())
        refs = self.refs()
        self.reject(self.margs())
        self.assert_untouched(d, refs)
        self.assertEqual("cfg\n", (self.wt / "local.cfg").read_text())

    def test_23_rename_into_a_secret_path(self) -> None:
        self.build(secret=False, extra={"a.cfg": "one\ntwo\nthree\nfour\nfive\nsix\n"})
        self.commit_work({"a.cfg": "one\ntwo\nthree\nfour\nfive\nsix mine\n"})
        d = self.D()
        self.git("pull", "-q", "--ff-only", "origin", "main", cwd=self.adv)
        self.git("mv", "a.cfg", ".env.local", cwd=self.adv)
        self.git("commit", "-qm", "rename", cwd=self.adv)
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.adv)
        result = self.merge()
        self.assertEqual((4, "secret-overlap"), (result.rc, result.data["result"]), result.err)
        self.assertIsNone(self.merge_head())
        self.assertEqual(d, self.D())

    def test_24_hundred_and_one_conflicts_are_not_cut(self) -> None:
        self.build(many=101)
        names = [f"many/{number:03d}.txt" for number in range(101)]
        self.commit_work({name: "mine\n" for name in names})
        self.advance({name: "theirs\n" for name in names})
        result = self.merge()
        self.assertEqual((3, "conflict"), (result.rc, result.data["reason"]), result.err)
        self.assertEqual(names, result.data["problem_paths"])
        self.assert_review(result, True)

    def test_25_changed_by_work_deleted_by_base(self) -> None:
        self.commit_work({"a.txt": "mine\n"})
        self.advance({"a.txt": None})
        result = self.merge()
        self.assertEqual((3, "conflict"), (result.rc, result.data["reason"]), result.err)
        self.assertEqual({1, 2}, self.stages("a.txt"))
        self.assertEqual("mine\n", (self.wt / "a.txt").read_text())
        self.assertEqual([], result.data["binary_conflicts"])
        self.assert_review(result, True)

    def test_26_untracked_ignored_file_base_does_not_touch(self) -> None:
        self.commit_work({"keep.cfg": UNTRACK, ".gitignore": "*.log\nlocal.cfg\nkeep.cfg\n"})
        self.advance({"other.txt": "other\n"})
        result = self.merge()
        self.assertEqual((0, "merged"), (result.rc, result.data["result"]), result.err)
        self.assertEqual("keep\n", (self.wt / "keep.cfg").read_text())
        self.assert_done(result)

    def test_27_name_collision_with_two_merge_bases(self) -> None:
        d, _b = self.crisscross({"gen.log": "from base\n"})
        self.write("gen.log", "mine\n")
        refs = self.refs()
        self.reject(self.margs(), message="取り込み先が足すファイルと同じ名前のものが作業ツリーにある")
        self.assert_untouched(d, refs)
        self.assertEqual("mine\n", (self.wt / "gen.log").read_text())

    def test_28_autocrlf_checkout_stops_before_fetch(self) -> None:
        d = self.D()
        self.advance({"other.txt": "other\n"})
        self.git("config", "core.autocrlf", "true")
        (self.wt / "a.txt").unlink()
        self.git("checkout", "--", "a.txt")
        self.assertEqual(b"a\r\n", (self.wt / "a.txt").read_bytes())
        refs = self.refs()
        self.reject(self.margs(), message="作業ツリーの中身が HEAD と違う")
        self.assert_untouched(d, refs)
        self.assertEqual(refs, self.refs())

    def hang_env(self, **extra) -> dict:
        return {"IG_HANG_MERGE": "1", "INTEGRATE_GUARD_NET_TIMEOUT": "5", "IG_MERGE_HEAD_FILE": str(self.wt / ".git/MERGE_HEAD"),
                "IG_MERGE_MARK": str(self.root / "merge-started"), **extra}

    def test_32f_timeout_before_the_merge_did_anything(self) -> None:
        d = self.D()
        self.advance({"other.txt": "other\n"})
        result = self.merge(env={"IG_SLEEP_MERGE": "1", "INTEGRATE_GUARD_NET_TIMEOUT": "5"})
        self.assertEqual(1, result.rc, result.err)
        self.assertIn("取り込みの途中の状態が残った", result.err)
        self.assertNotIn("作業ツリーは変わっていない", result.err)
        self.assert_back_to_d(d)

    def assert_back_to_d(self, d: str) -> None:
        self.assertEqual(d, self.D())
        self.assertEqual(d, self.rev("HEAD"))
        self.assertIsNone(self.merge_head())
        self.assertEqual("", self.git("status", "--porcelain", "--untracked-files=no").stdout)

    def test_32_timeout_after_the_merge_started_is_aborted(self) -> None:
        d = self.D()
        self.advance({"other.txt": "other\n"})
        result = self.merge(env=self.hang_env())
        self.assertEqual(1, result.rc, result.err)
        self.assertTrue((self.root / "merge-started").exists(), "the merge must have started before the stop")
        self.assertIn("取り込みの途中で失敗した", result.err)
        self.assert_back_to_d(d)
        self.assertFalse((self.wt / "other.txt").exists())

    def test_32b_timeout_with_conflicts_is_aborted(self) -> None:
        self.commit_work({"f.txt": f_text({10: "mine"})})
        self.advance({"f.txt": f_text({10: "theirs"})})
        d = self.D()
        result = self.merge(env=self.hang_env())
        self.assertEqual(1, result.rc, result.err)
        self.assertTrue((self.root / "merge-started").exists())
        self.assertIn("取り込みの途中で失敗した", result.err)
        self.assert_back_to_d(d)

    def test_32c_abort_timing_out_is_reported(self) -> None:
        d = self.D()
        self.advance({".gitattributes": "*.txt ident\n"})
        result = self.merge(env={"IG_HANG_ABORT": "1", "INTEGRATE_GUARD_NET_TIMEOUT": "5"})
        self.assertEqual(1, result.rc, result.err)
        self.assertIn("取り込みの途中の状態が残った", result.err)
        self.assertEqual(d, self.D())

    def test_32d_net_timeout_value_is_checked(self) -> None:
        d = self.D()
        self.advance({"other.txt": "other\n"})
        refs = self.refs()
        for value in ("", "abc", "0", "121", "9" * 5000):
            with self.subTest(value=value):
                result = self.merge(env={"INTEGRATE_GUARD_NET_TIMEOUT": value})
                self.assertEqual(2, result.rc)
                self.assertIn("usage: INTEGRATE_GUARD_NET_TIMEOUT は 1〜120 の整数", result.err)
                self.assertEqual("", result.out)
                for secret in (str(self.root), str(GUARD), str(ROOT)) + tuple(v for v in (value,) if v in ("abc", "121", "9" * 5000)):
                    self.assertNotIn(secret, result.err)
                self.assertEqual(refs, self.refs())
                self.assertEqual(d, self.rev("HEAD"))

    def test_32e_signature_verification_is_not_started(self) -> None:
        marker = self.root / "gpg-ran"
        program = self.root / "gpg.sh"
        program.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
        program.chmod(0o755)
        self.git("config", "merge.verifySignatures", "true")
        self.git("config", "gpg.program", str(program))
        self.advance({"other.txt": "other\n"})
        result = self.merge()
        self.assertEqual(0, result.rc, result.err)
        self.assertFalse(marker.exists())

    def test_33_merge_driver_is_refused(self) -> None:
        marker = self.root / "driver-ran"
        self.commit_work({".gitattributes": "f.txt merge=x\n", "f.txt": f_text({10: "mine"})})
        self.advance({"f.txt": f_text({10: "theirs"})})
        self.git("config", "merge.x.driver", f"touch {marker}; false")
        d = self.D()
        refs = self.refs()
        self.reject(self.margs(), message="merge driver")
        self.assertFalse(marker.exists())
        self.assert_untouched(d, refs)
        self.assertEqual(refs, self.refs())

    def test_33b_default_merge_driver_is_refused(self) -> None:
        marker = self.root / "driver-ran"
        self.advance({"other.txt": "other\n"})
        self.git("config", "merge.default", "x")
        self.git("config", "merge.x.driver", f"touch {marker}; false")
        self.reject(self.margs(), message="merge driver")
        self.assertFalse(marker.exists())

    def test_34_name_collision_when_only_one_merge_base_matches(self) -> None:
        # Whichever order `merge-base --all` prints, one variant matches only the second base.
        for variant in ("x1-deleted", "y1-deleted"):
            with self.subTest(variant=variant):
                self.build(extra={"gen.log": "g\n"})
                if variant == "x1-deleted":
                    self.commit_work({"gen.log": None})
                    d, _b = self.crisscross({"gen.log": "g\n"})
                else:
                    d, _b = self.crisscross({"gen.log": "g\n"}, y1_changes={"gen.log": None})
                self.assertEqual(2, len(self.git("merge-base", "--all", d, self.rev("ymain")).stdout.split()))
                self.write("gen.log", "mine\n")
                refs = self.refs()
                self.reject(self.margs(), message="取り込み先が足すファイルと同じ名前のものが作業ツリーにある")
                self.assert_untouched(d, refs)
                self.assertEqual("mine\n", (self.wt / "gen.log").read_text())

    def profile(self, globs: list[str], base: Path | None = None) -> str:
        return "secret_paths: " + json.dumps(globs) + "\n"

    def test_29_secret_glob_added_by_base_profile(self) -> None:
        self.commit_work({"conf/secret.ini": "s=mine\n"})
        d = self.D()
        self.advance({".claude/project-profile.yml": self.profile(["conf/secret.ini"]), "conf/secret.ini": "s=theirs\n"})
        refs = self.refs()
        result = self.merge()
        self.assertEqual((4, "secret-overlap", ["conf/secret.ini"]), (result.rc, result.data["result"], result.data["problem_paths"]), result.err)
        self.assertTrue(any(re.search(value, "conf/secret.ini") for value in result.data["exclude_ere"][1:]))
        self.assert_untouched(d, refs)

    def test_30_up_to_date_still_collects_globs_of_base(self) -> None:
        self.advance({".claude/project-profile.yml": self.profile(["conf/secret.ini"])})
        self.git("fetch", "-q", "origin")
        self.git("rebase", "-q", "origin/main")
        self.commit_work({".claude/project-profile.yml": "other: 1\n"})
        result = self.merge()
        self.assertEqual((0, "up-to-date"), (result.rc, result.data["result"]), result.err)
        self.assertEqual(["conf/secret.ini"], result.data["added_globs"])
        self.assertEqual([".env", ".env.*", ".dev.vars", "conf/secret.ini"], result.data["exclude_globs"])
        self.assertEqual(2, len(result.data["exclude_ere"]))
        self.assertEqual(ERE, result.data["exclude_ere"][0])
        self.assertTrue(re.search(result.data["exclude_ere"][1], "conf/secret.ini"))
        self.assert_done(result)

    def combined_ere(self) -> str:
        return subprocess.run(["bash", str(SNAPSHOT), "--print-exclude-ere", "--exclude-glob", ".env", "--exclude-glob", ".env.*",
                               "--exclude-glob", ".dev.vars"], text=True, stdout=subprocess.PIPE, check=True).stdout.strip()

    def test_31_combined_ere_is_not_extended_by_its_spelling(self) -> None:
        self.advance({"other.txt": "other\n"})
        combined = self.combined_ere()
        result = self.merge(eres=[combined])
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual(([], [combined]), (result.data["added_globs"], result.data["exclude_ere"]))
        self.assert_done(result)



class PremiseTest(Base):
    """Every missing premise refuses with exit 1 and fetches / changes nothing."""

    def refuse(self, argv: list[str], *, env: dict | None = None) -> None:
        d = self.D()
        before_remote = self.rev("refs/remotes/origin/main")
        result = self.guard(argv, env=env)
        self.assertEqual(1, result.rc, result.err)
        self.assertEqual(before_remote, self.rev("refs/remotes/origin/main"))
        self.assertEqual(d, self.D())

    def test_wrong_branch(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.git("switch", "-q", "-c", "other")
        self.refuse(self.margs())

    def test_reviewed_sha_differs(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.refuse(self.margs(sha=self.rev("main")))

    def test_uncommitted_change(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.write("a.txt", "changed\n")
        self.refuse(self.margs())

    def test_uncommitted_change_to_a_secret_file(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.write(".env.local", "changed\n")
        self.refuse(self.margs())

    def test_merge_head_exists(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.git("fetch", "-q", "origin")
        self.git("merge", "-q", "--no-ff", "--no-commit", "origin/main")
        self.assertIsNotNone(self.merge_head())
        self.refuse(self.margs())

    def test_origin_is_another_repository(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.refuse(self.margs(), env={"IG_URL": "https://github.com/x/y.git"})

    def test_fetch_and_push_urls_differ(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.refuse(self.margs(), env={"IG_PUSH_URL": "https://github.com/x/y.git"})

    def test_remote_vcs_is_set(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.git("config", "remote.origin.vcs", "foo")
        self.refuse(self.margs())

    def test_repo_differs(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.refuse(self.margs(repo="github.com/o/other"))

    def test_reviewed_sha_is_not_a_full_oid(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.refuse(self.margs(sha="abc1234"))

    def test_branch_and_base_names(self) -> None:
        self.advance({"other.txt": "other\n"})
        self.refuse(self.margs(branch="task x"))
        self.refuse(self.margs(base="x..y"))

    def test_missing_arguments(self) -> None:
        for argv in ([], ["merge"], ["commit", "--dir", str(self.wt)], ["worktree"], ["exclude"], ["check"]):
            result = self.guard(argv)
            self.assertEqual(2, result.rc)
            self.assertIn("usage:", result.err)


# ------------------------------------------------------------------ stage-resolved / commit

class StageResolvedTest(Base):
    def stage(self, d: str, b: str, saved, **kw) -> Run:
        return self.guard(self.rargs("stage-resolved", saved, d, b, **kw))

    def test_01_resolved(self) -> None:
        d, b, _r, saved = self.conflict_f()
        self.resolve_f()
        result = self.stage(d, b, saved)
        self.assertEqual((0, "resolved"), (result.rc, result.data["result"]), result.err)
        self.assertEqual([], self.unmerged())
        self.assertEqual(f_text({10: "resolved"}), self.git("show", ":f.txt").stdout)

    def test_02_markers_left(self) -> None:
        d, b, _r, saved = self.conflict_f()
        self.reject(self.stage(d, b, saved))
        self.assertTrue(self.unmerged())

    def test_03_unstaged_change_outside(self) -> None:
        d, b, _r, saved = self.conflict_f()
        self.resolve_f()
        self.write("a.txt", "changed\n")
        self.reject(self.stage(d, b, saved))

    def test_04_staged_change_outside(self) -> None:
        d, b, _r, saved = self.conflict_f()
        self.resolve_f()
        self.write("a.txt", "changed\n")
        self.git("add", "a.txt")
        self.reject(self.stage(d, b, saved))

    def test_05_staged_secret_change(self) -> None:
        d, b, _r, saved = self.conflict_f()
        self.resolve_f()
        self.write(".env.local", "changed\n")
        self.git("add", "-f", ".env.local")
        self.reject(self.stage(d, b, saved))

    def test_06_conflicted_path_staged_with_markers(self) -> None:
        d, b, _r, saved = self.conflict_f()
        self.git("add", "f.txt")
        self.assertEqual([], self.unmerged())
        self.reject(self.stage(d, b, saved))

    def test_06b_markers_only_in_the_work_tree_after_staging_clean_content(self) -> None:
        d, b, _r, saved = self.conflict_f()
        with_markers = (self.wt / "f.txt").read_bytes()
        self.resolve_f()
        self.git("add", "f.txt")
        (self.wt / "f.txt").write_bytes(with_markers)
        index_before = self.git("ls-files", "-s").stdout
        self.reject(self.stage(d, b, saved))
        self.assertEqual(index_before, self.git("ls-files", "-s").stdout)
        self.assertEqual(f_text({10: "resolved"}), self.git("show", ":f.txt").stdout)

    def test_07_deleted_conflict(self) -> None:
        d, b, _r, saved = self.conflict_f()
        (self.wt / "f.txt").unlink()
        result = self.stage(d, b, saved)
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual("", self.git("ls-files", "-s", "--", "f.txt").stdout)
        self.assertIn("D  f.txt", self.git("status", "--porcelain").stdout)

    def test_08_merge_head_is_not_base(self) -> None:
        d, b, r, _saved = self.conflict_f()
        forged = dict(r.data, base_sha=d)
        self.resolve_f()
        self.reject(self.guard(self.rargs("stage-resolved", self.save("forged.json", json.dumps(forged)), d, d)))

    def test_09_conflict_without_markers(self) -> None:
        self.commit_work({".gitattributes": "bin.dat -merge\n", "bin.dat": b"\x00D"})
        self.advance({"bin.dat": b"\x00B"})
        d = self.D()
        result = self.merge()
        self.assertEqual(3, result.rc, result.err)
        self.reject(self.stage(d, result.data["base_sha"], self.save("merge.json", result.out)), message="印の無い衝突")

    def test_10_result_file_hash_mismatch(self) -> None:
        d, b, _r, saved = self.conflict_f()
        self.resolve_f()
        self.reject(self.stage(d, b, (saved[0], "0" * 64)))

    def test_11_deleted_by_work_keep_or_delete(self) -> None:
        self.commit_work({"a.txt": None})
        self.advance({"a.txt": "changed by base\n"})
        d = self.D()
        result = self.merge()
        saved = self.save("merge.json", result.out)
        b = result.data["base_sha"]
        keep = self.stage(d, b, saved)
        self.assertEqual(0, keep.rc, keep.err)
        self.assertIn("changed by base", self.git("show", ":a.txt").stdout)

    def test_11b_deleted_by_work_delete(self) -> None:
        self.commit_work({"a.txt": None})
        self.advance({"a.txt": "changed by base\n"})
        d = self.D()
        result = self.merge()
        saved = self.save("merge.json", result.out)
        (self.wt / "a.txt").unlink()
        done = self.stage(d, result.data["base_sha"], saved)
        self.assertEqual(0, done.rc, done.err)
        self.assertEqual("", self.git("ls-files", "-s", "--", "a.txt").stdout)

    def hundred(self) -> tuple[str, str, tuple[str, str], list[str]]:
        self.build(many=101)
        names = [f"many/{number:03d}.txt" for number in range(101)]
        self.commit_work({name: "mine\n" for name in names})
        self.advance({name: "theirs\n" for name in names})
        d = self.D()
        result = self.merge()
        self.assertEqual(3, result.rc, result.err)
        return d, result.data["base_sha"], self.save("merge.json", result.out), names

    def test_12_all_hundred_and_one_resolved(self) -> None:
        d, b, saved, names = self.hundred()
        for name in names:
            self.write(name, "resolved\n")
        result = self.stage(d, b, saved)
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual([], self.unmerged())

    def test_12b_hundred_resolved_one_left(self) -> None:
        d, b, saved, names = self.hundred()
        for name in names[:100]:
            self.write(name, "resolved\n")
        self.reject(self.stage(d, b, saved))

    def test_13_filemode_false_still_stages_the_executable_bit(self) -> None:
        d, b, _r, saved = self.conflict_f()
        self.git("config", "core.filemode", "false")
        self.resolve_f()
        (self.wt / "f.txt").chmod(0o755)
        result = self.stage(d, b, saved)
        self.assertEqual(0, result.rc, result.err)
        self.assertTrue(self.git("ls-files", "-s", "--", "f.txt").stdout.startswith("100755 "))

    def test_14_exclude_ere_of_the_merge_must_be_kept(self) -> None:
        self.commit_work({"f.txt": f_text({10: "mine"})})
        self.advance({"f.txt": f_text({10: "theirs"}), ".claude/project-profile.yml": "secret_paths: [\"zz.secret\"]\n"})
        d = self.D()
        result = self.merge()
        self.assertEqual(3, result.rc, result.err)
        self.assertEqual(2, len(result.data["exclude_ere"]))
        saved = self.save("merge.json", result.out)
        b = result.data["base_sha"]
        self.resolve_f()
        self.reject(self.stage(d, b, saved, eres=[ERE]))
        done = self.stage(d, b, saved, eres=result.data["exclude_ere"])
        self.assertEqual(0, done.rc, done.err)


class CommitTest(Base):
    def commit(self, d: str, b: str, saved, **kw) -> Run:
        return self.guard(self.rargs("commit", saved, d, b, **kw))

    def resolved(self) -> tuple[str, str, Run, tuple[str, str]]:
        d, b, r, saved = self.conflict_f()
        self.resolve_f()
        done = self.guard(self.rargs("stage-resolved", saved, d, b))
        self.assertEqual(0, done.rc, done.err)
        return d, b, r, saved

    def test_01_commit_after_stage_resolved(self) -> None:
        d, b, _r, saved = self.resolved()
        tree = self.git("write-tree").stdout.strip()
        result = self.commit(d, b, saved)
        self.assertEqual((0, "committed"), (result.rc, result.data["result"]), result.err)
        self.assertEqual([d, b], self.parents())
        self.assertEqual(tree, self.rev("HEAD^{tree}"))
        self.assertEqual(self.D(), result.data["head"])
        self.assertFalse([line for line in self.body().splitlines() if line.startswith("#")])
        self.assertIsNone(self.merge_head())
        self.no_markers()

    def test_02_diff_changed_commits(self) -> None:
        self.commit_work({"f.txt": f_text({15: "mine15"})})
        d = self.D()
        self.advance({"f.txt": f_text({17: "theirs17"})})
        result = self.merge()
        self.assertEqual((3, "diff-changed"), (result.rc, result.data["reason"]), result.err)
        done = self.commit(d, result.data["base_sha"], self.save("merge.json", result.out))
        self.assertEqual((0, "committed"), (done.rc, done.data["result"]), done.err)

    def refused(self, d: str) -> None:
        self.assertEqual(d, self.D())
        self.assertEqual(d, self.rev("HEAD"))

    def test_03a_conflict_remains(self) -> None:
        d, b, _r, saved = self.conflict_f()
        self.reject(self.commit(d, b, saved))
        self.refused(d)

    def test_03b_unstaged_change(self) -> None:
        d, b, _r, saved = self.resolved()
        self.write("a.txt", "changed\n")
        self.reject(self.commit(d, b, saved))
        self.refused(d)

    def test_03c_staged_change_outside(self) -> None:
        d, b, _r, saved = self.resolved()
        self.write("a.txt", "changed\n")
        self.git("add", "a.txt")
        self.reject(self.commit(d, b, saved))
        self.refused(d)

    def test_03d_merge_head_is_not_base(self) -> None:
        d, b, r, _saved = self.resolved()
        forged = self.save("forged.json", json.dumps(dict(r.data, base_sha=d)))
        self.reject(self.commit(d, d, forged))
        self.refused(d)
        self.assertEqual(b, self.merge_head())

    def test_03e_head_is_not_reviewed_sha(self) -> None:
        d, b, r, _saved = self.resolved()
        other = self.rev("main")
        forged = self.save("forged.json", json.dumps(dict(r.data, reviewed_sha=other)))
        self.reject(self.commit(other, b, forged))
        self.refused(d)

    def test_03f_resolved_path_that_became_secret_is_not_read(self) -> None:
        self.commit_work({"f.txt": f_text({10: "mine"})})
        self.advance({"f.txt": f_text({10: "theirs"})})
        d = self.D()
        merged = self.merge()
        self.assertEqual(3, merged.rc, merged.err)
        saved = self.save("merge.json", merged.out)
        b = merged.data["base_sha"]
        self.write("f.txt", f_text({10: "mine"}))  # same content as D, staged by the helper
        self.assertEqual(0, self.guard(self.rargs("stage-resolved", saved, d, b)).rc)
        self.write(".claude/project-profile.yml", 'secret_paths: ["f.txt"]\n')
        excluded = self.guard(["exclude", "--dir", str(self.wt), "--reviewed-sha", d, "--base-sha", b, "--previous", saved[0],
                               "--expect-previous-sha256", saved[1]])
        self.assertEqual(0, excluded.rc, excluded.err)
        result = self.guard(self.rargs("commit", saved, d, b, eres=excluded.data["exclude_ere"]))
        self.assertEqual(1, result.rc, result.err)
        self.assertIn("機密の除外に当たる", result.err)
        self.refused(d)

    def test_04_new_secret_path_is_staged(self) -> None:
        d, b, _r, saved = self.resolved()
        self.write(".env.prod", "secret\n")
        self.git("add", "-f", ".env.prod")
        result = self.commit(d, b, saved)
        self.assertEqual((4, "secret-overlap"), (result.rc, result.data["result"]), result.err)
        self.refused(d)

    def test_05_conflict_without_markers_staged_by_hand(self) -> None:
        self.commit_work({".gitattributes": "bin.dat -merge\n", "bin.dat": b"\x00D"})
        self.advance({"bin.dat": b"\x00B"})
        d = self.D()
        result = self.merge()
        saved = self.save("merge.json", result.out)
        self.git("add", "bin.dat")
        self.reject(self.commit(d, result.data["base_sha"], saved), message="印の無い衝突")
        self.refused(d)

    def test_06_conflicted_path_replaced_by_a_link(self) -> None:
        d, b, _r, saved = self.conflict_f()
        (self.wt / "f.txt").unlink()
        os.symlink("a.txt", self.wt / "f.txt")
        self.git("add", "f.txt")
        self.reject(self.commit(d, b, saved))
        self.refused(d)

    def test_07_parent_of_the_conflicted_path_is_a_link(self) -> None:
        self.commit_work({"dc/c.txt": "mine\n"})
        self.advance({"dc/c.txt": None})
        d = self.D()
        result = self.merge()
        self.assertEqual((3, "conflict"), (result.rc, result.data["reason"]), result.err)
        saved = self.save("merge.json", result.out)
        b = result.data["base_sha"]
        shutil.rmtree(self.wt / "dc")
        done = self.guard(self.rargs("stage-resolved", saved, d, b))
        self.assertEqual(0, done.rc, done.err)
        outside = self.root / "outside"
        outside.mkdir()
        os.mkfifo(outside / "c.txt")
        os.symlink(outside, self.wt / "dc")
        self.reject(self.guard(self.rargs("commit", saved, d, b), timeout=30))
        self.refused(d)


# ------------------------------------------------------------------ check

class CheckTest(Base):
    def check(self, sha: str, d: str, b: str, eres: list[str] | None = None) -> Run:
        return self.guard(["check", "--dir", str(self.wt), "--sha", sha, "--reviewed-sha", d, "--base-sha", b, *self.ere_args(eres)])

    def test_match_for_clean_merge_and_distant_lines(self) -> None:
        d = self.D()
        self.advance({"other.txt": "other\n"})
        result = self.merge()
        self.assertEqual(0, result.rc, result.err)
        done = self.check(self.rev("HEAD"), d, result.data["base_sha"])
        self.assertEqual((0, "match"), (done.rc, done.data["result"]), done.err)

    def test_match_for_overlap(self) -> None:
        self.commit_work({"f.txt": f_text({25: "mine25"})})
        d = self.D()
        self.advance({"f.txt": f_text(insert_after={3: "inserted"})})
        result = self.merge()
        done = self.check(self.rev("HEAD"), d, result.data["base_sha"])
        self.assertEqual((0, "match"), (done.rc, done.data["result"]), done.err)

    def manual_merge(self) -> tuple[str, str]:
        d = self.D()
        b = self.advance({"other.txt": "other\n"})
        self.git("fetch", "-q", "origin")
        self.git("merge", "-q", "--no-ff", "--no-commit", "origin/main")
        return d, b

    def test_mismatch_when_work_file_is_edited_in_the_merge(self) -> None:
        d, b = self.manual_merge()
        self.write("work.txt", "work\nmore\n")
        self.git("add", "work.txt")
        self.git("commit", "-qm", "merge with edit")
        done = self.check(self.rev("HEAD"), d, b)
        self.assertEqual((1, "mismatch"), (done.rc, done.data["result"]), done.err)

    def test_mismatch_when_base_only_file_is_rewritten(self) -> None:
        d, b = self.manual_merge()
        self.write("other.txt", "rewritten\n")
        self.git("add", "other.txt")
        self.git("commit", "-qm", "merge with rewrite")
        self.assertEqual(1, self.check(self.rev("HEAD"), d, b).rc)

    def test_reversed_parents(self) -> None:
        d = self.D()
        b = self.advance({"other.txt": "other\n"})
        self.git("fetch", "-q", "origin")
        self.git("switch", "-q", "-c", "reversed", b)
        self.git("merge", "-q", "--no-ff", "-m", "reversed", d)
        self.assertEqual(1, self.check(self.rev("HEAD"), d, b).rc)

    def test_sha_with_one_parent(self) -> None:
        d = self.D()
        b = self.advance({"other.txt": "other\n"})
        self.git("fetch", "-q", "origin")
        self.assertEqual(1, self.check(d, d, b).rc)

    def test_exclude_ere_is_required(self) -> None:
        result = self.guard(["check", "--dir", str(self.wt), "--sha", "a" * 40, "--reviewed-sha", "a" * 40,
                             "--base-sha", "a" * 40])
        self.assertEqual(2, result.rc)
        self.assertIn("usage:", result.err)


# ------------------------------------------------------------------ exclude

class ExcludeTest(Base):
    def merged(self) -> tuple[str, str, Run, tuple[str, str]]:
        d = self.D()
        self.advance({"a.txt": "changed by base\n"})
        result = self.merge()
        self.assertEqual(0, result.rc, result.err)
        return d, result.data["base_sha"], result, self.save("merge.json", result.out)

    def exclude(self, d: str, b: str, saved: tuple[str, str]) -> Run:
        return self.guard(["exclude", "--dir", str(self.wt), "--reviewed-sha", d, "--base-sha", b, "--previous", saved[0],
                           "--expect-previous-sha256", saved[1]])

    def test_01_profile_adds_a_glob(self) -> None:
        d, b, r, saved = self.merged()
        self.write(".claude/project-profile.yml", 'secret_paths: ["foo.txt"]\n')
        result = self.exclude(d, b, saved)
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual(["foo.txt"], result.data["added_globs"])
        previous = r.data["exclude_ere"]
        self.assertEqual(previous, result.data["exclude_ere"][:len(previous)])
        self.assertEqual(len(previous) + 1, len(result.data["exclude_ere"]))
        self.assertEqual(r.data["exclude_globs"] + ["foo.txt"], result.data["exclude_globs"])

    def test_02_unknown_ere_of_the_previous_is_kept(self) -> None:
        d, b, _r, _saved = self.merged()
        previous = {"reviewed_sha": d, "base_sha": b, "exclude_globs": [".env", ".env.*", ".dev.vars"],
                    "exclude_ere": [ERE, "(^|/)x$"]}
        result = self.exclude(d, b, self.save("previous.json", json.dumps(previous)))
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual([ERE, "(^|/)x$"], result.data["exclude_ere"])
        self.assertEqual([], result.data["added_globs"])

    def test_03_unchanged_profile_with_a_combined_ere(self) -> None:
        d = self.D()
        self.advance({"other.txt": "other\n"})
        combined = MergeTest.combined_ere(self)  # type: ignore[arg-type]
        merged = self.merge(eres=[combined])
        self.assertEqual(0, merged.rc, merged.err)
        result = self.exclude(d, merged.data["base_sha"], self.save("merge.json", merged.out))
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual(([], [combined]), (result.data["added_globs"], result.data["exclude_ere"]))

    def test_04_broken_profile(self) -> None:
        d, b, _r, saved = self.merged()
        self.write(".claude/project-profile.yml", "secret_paths: [unclosed\n")
        self.reject(self.exclude(d, b, saved))

    def test_05_resolved_path_becomes_secret(self) -> None:
        d, b, r, saved = self.conflict_f()
        self.resolve_f()
        self.assertEqual(0, self.guard(self.rargs("stage-resolved", saved, d, b)).rc)
        self.assertEqual(0, self.guard(self.rargs("commit", saved, d, b)).rc)
        self.write(".claude/project-profile.yml", 'secret_paths: ["f.txt"]\n')
        result = self.exclude(d, b, saved)
        self.assertEqual((4, "secret-overlap", ["f.txt"]), (result.rc, result.data["result"], result.data["problem_paths"]), result.err)

    def test_06_path_taken_from_base_becomes_secret(self) -> None:
        d, b, _r, saved = self.merged()
        self.write(".claude/project-profile.yml", 'secret_paths: ["a.txt"]\n')
        result = self.exclude(d, b, saved)
        self.assertEqual((0, ["a.txt"]), (result.rc, result.data["added_globs"]), result.err)

    def test_07_previous_hash_mismatch(self) -> None:
        d, b, _r, saved = self.merged()
        self.reject(self.exclude(d, b, (saved[0], "0" * 64)))

    def test_08_output_can_be_the_next_previous(self) -> None:
        d, b, _r, saved = self.merged()
        self.write(".claude/project-profile.yml", 'secret_paths: ["foo.txt"]\n')
        first = self.exclude(d, b, saved)
        again = self.exclude(d, b, self.save("first.json", first.out))
        self.assertEqual(0, again.rc, again.err)
        self.assertEqual([], again.data["added_globs"])
        for key in ("reviewed_sha", "base_sha", "exclude_globs", "exclude_ere"):
            self.assertEqual(first.data[key], again.data[key], key)
        self.write(".claude/project-profile.yml", 'secret_paths: ["foo.txt", "bar.txt"]\n')
        third = self.exclude(d, b, self.save("second.json", again.out))
        self.assertEqual(["bar.txt"], third.data["added_globs"])
        self.assertEqual(again.data["exclude_ere"], third.data["exclude_ere"][:-1])
        self.assertEqual(len(again.data["exclude_ere"]) + 1, len(third.data["exclude_ere"]))


class PromisorTest(Base):
    """Pre-check position: stored contents must not be read before the pre-check stops promisor setups."""

    def setUp(self) -> None:
        super().setUp()
        self.trace = self.root / "uploadpack-ran"
        up = self.root / "up.sh"
        up.write_text(f"#!/bin/sh\ntouch {self.trace}\nexit 1\n", encoding="utf-8")
        promisor = self.root / "promisor"
        promisor.mkdir()
        stub = promisor / "git"
        stub.write_text(
            "#!/bin/sh\n"
            "skip=0\n"
            "for a in \"$@\"; do\n"
            "  if [ \"$skip\" = 1 ]; then skip=0; continue; fi\n"
            "  case \"$a\" in\n"
            "    -c|-C) skip=1; continue;;\n"
            "    -*) continue;;\n"
            "    version) echo 'git version 2.34.1'; exit 0;;\n"
            "    *) break;;\n"
            "  esac\n"
            "done\n"
            f"exec env -u GIT_NO_LAZY_FETCH {self.real_git} \"$@\"\n", encoding="utf-8")
        stub.chmod(0o755)
        self.promisor_bin = promisor
        self.up = up

    def make_promisor(self, rel: str, blob_rev: str) -> None:
        blob = self.git("rev-parse", blob_rev).stdout.strip()
        loose = self.wt / ".git/objects" / blob[:2] / blob[2:]
        self.assertTrue(loose.exists(), "the blob must be a loose object")
        loose.unlink()
        for key, value in (("core.repositoryformatversion", "1"), ("extensions.partialClone", "origin"),
                           ("remote.origin.promisor", "true"), ("remote.origin.url", str(self.root / "nosuchremote")),
                           ("remote.origin.uploadpack", f"sh {self.up}")):
            support_git.git("config", key, value, cwd=self.wt, binary=self.real_git, text=False, stdin=None)
        # without the pre-check the configuration would run uploadpack on a lazy fetch
        env = {**os.environ, "PATH": f"{self.promisor_bin}:{os.environ['PATH']}"}
        subprocess.run(["git", "cat-file", "-p", blob], cwd=self.wt, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertTrue(self.trace.exists(), "the setup must trigger uploadpack when nothing stops it")
        self.trace.unlink()

    def guard_promisor(self, argv: list[str]) -> Run:
        return self.guard(argv, env={"PATH": f"{self.promisor_bin}:{self.fakebin}:{os.environ['PATH']}"})

    def test_01_check(self) -> None:
        self.commit_work({"f.txt": f_text({25: "mine25"})})
        d = self.D()
        b = self.advance({"f.txt": f_text(insert_after={3: "inserted"})})
        merged = self.merge()
        self.assertEqual(0, merged.rc, merged.err)
        self.make_promisor("f.txt", f"{b}:f.txt")
        result = self.guard_promisor(["check", "--dir", str(self.wt), "--sha", self.rev("HEAD"), "--reviewed-sha", d,
                                      "--base-sha", b, *self.ere_args()])
        self.assertEqual(1, result.rc, result.err)
        self.assertIn("事前検査が止めた", result.err)
        self.assertFalse(self.trace.exists())

    def test_02_exclude(self) -> None:
        d = self.D()
        b = self.advance({".claude/project-profile.yml": 'secret_paths: ["zz.secret"]\n'})
        merged = self.merge()
        self.assertEqual(0, merged.rc, merged.err)
        saved = self.save("merge.json", merged.out)
        self.make_promisor("p", f"{b}:.claude/project-profile.yml")
        result = self.guard_promisor(["exclude", "--dir", str(self.wt), "--reviewed-sha", d, "--base-sha", b,
                                      "--previous", saved[0], "--expect-previous-sha256", saved[1]])
        self.assertEqual(1, result.rc, result.err)
        self.assertIn("事前検査が止めた", result.err)
        self.assertFalse(self.trace.exists())


# ------------------------------------------------------------------ worktree

class WorktreeTest(Base):
    def wargs(self, *, head: str | None = None, eres: list[str] | None = None, baseline: tuple[str, str] | None = None,
              allow: tuple[str, ...] = (), branch: str = "task/x") -> list[str]:
        argv = ["worktree", "--dir", str(self.wt), "--branch", branch, "--head", head or self.D(), *self.ere_args(eres)]
        if baseline:
            argv += ["--baseline", baseline[0], "--expect-baseline-sha256", baseline[1]]
        for path in allow:
            argv += ["--allow-path", path]
        return argv

    def base(self, eres: list[str] | None = None) -> tuple[str, str]:
        result = self.guard(self.wargs(eres=eres))
        self.assertEqual(0, result.rc, result.err)
        return self.save("baseline.json", result.out)

    def test_01_clean_and_untracked_list(self) -> None:
        self.write("u1.txt", "u\n")
        self.write("dir/u2.txt", "u\n")
        self.write("build.log", "ignored\n")
        self.write(".claude/reviews/x.md", "state\n")
        result = self.guard(self.wargs())
        self.assertEqual(0, result.rc, result.err)
        self.assertEqual(["dir/u2.txt", "u1.txt"], sorted(result.data["untracked"]))

    def test_02_changed_without_allow(self) -> None:
        self.write("a.txt", "changed\n")
        self.reject(self.guard(self.wargs()))

    def test_03_allowed_change(self) -> None:
        base = self.base()
        self.write("version.txt", "2\n")
        result = self.guard(self.wargs(baseline=base, allow=("version.txt",)))
        self.assertEqual((0, ["version.txt"]), (result.rc, result.data["changed_paths"]), result.err)

    def test_04_union_of_allowed_paths(self) -> None:
        base = self.base()
        self.write("version.txt", "2\n")
        self.write("f.txt", "changed\n")
        ok = self.guard(self.wargs(baseline=base, allow=("version.txt", "f.txt")))
        self.assertEqual(0, ok.rc, ok.err)
        self.reject(self.guard(self.wargs(baseline=base, allow=("f.txt",))))

    def test_05_two_rounds_of_fixes(self) -> None:
        base = self.base()
        self.write("a.txt", "one\n")
        self.assertEqual(0, self.guard(self.wargs(baseline=base, allow=("a.txt",))).rc)
        self.write("CHANGELOG.md", "# Changelog\n\nchanged\n")
        self.assertEqual(0, self.guard(self.wargs(baseline=base, allow=("a.txt", "CHANGELOG.md"))).rc)

    def test_06_new_file(self) -> None:
        base = self.base()
        self.write("tests/new_test.py", "x = 1\n")
        self.assertEqual(0, self.guard(self.wargs(baseline=base, allow=("tests/new_test.py",))).rc)
        self.reject(self.guard(self.wargs(baseline=base)))

    def test_07_state_files_only(self) -> None:
        base = self.base()
        self.write(".claude/agents/x.md", "agent\n")
        self.reject(self.guard(self.wargs(baseline=base)))
        (self.wt / ".claude/agents/x.md").unlink()
        self.write(".claude/reviews/x.md", "state\n")
        self.assertEqual(0, self.guard(self.wargs(baseline=base)).rc)

    def test_08_secret_path(self) -> None:
        base = self.base()
        self.write(".env.local", "changed\n")
        self.reject(self.guard(self.wargs(baseline=base, allow=(".env.local",))))

    def test_09_staged_change(self) -> None:
        self.write("a.txt", "changed\n")
        self.git("add", "a.txt")
        self.reject(self.guard(self.wargs(allow=("a.txt",))))

    def test_10_head_differs(self) -> None:
        self.reject(self.guard(self.wargs(head=self.rev("main"))))

    def test_11_hundred_and_one_changes(self) -> None:
        self.build(many=101)
        base = self.base()
        names = [f"many/{number:03d}.txt" for number in range(101)]
        for name in names:
            self.write(name, "changed\n")
        every = self.guard(self.wargs(baseline=base, allow=tuple(names)))
        self.assertEqual(0, every.rc, every.err)
        self.assertEqual(names, every.data["changed_paths"])
        self.reject(self.guard(self.wargs(baseline=base, allow=tuple(names[:100]))))

    def test_12_filter_stops_before_anything_is_read(self) -> None:
        marker = MergeTest.filter_setup(self)  # type: ignore[arg-type]
        self.reject(self.guard(self.wargs()), message="事前検査が止めた")
        self.assertFalse(marker.exists())

    def test_13_executable_bit_only(self) -> None:
        self.git("config", "core.filemode", "false")
        (self.wt / "a.txt").chmod(0o755)
        self.reject(self.guard(self.wargs()))

    def test_13b_executable_bit_of_a_secret_file(self) -> None:
        self.git("config", "core.filemode", "false")
        (self.wt / ".env.local").chmod(0o755)
        result = self.guard(self.wargs())
        self.assertEqual(1, result.rc, result.err)
        self.assertEqual([".env.local"], result.data["changed_paths"])

    def test_16b_baseline_secret_column_dropped_is_refused_before_reading(self) -> None:
        longer = [ERE, r"(^|/)version\.txt$"]
        base = self.base(eres=longer)
        self.write("version.txt", "changed\n")
        result = self.guard(self.wargs(eres=[ERE], baseline=base))
        self.assertEqual(1, result.rc, result.err)
        self.assertIn("含まない", result.err)

    def test_14_autocrlf(self) -> None:
        base = self.base()
        self.git("config", "core.autocrlf", "true")
        (self.wt / "a.txt").unlink()
        self.git("checkout", "--", "a.txt")
        self.assertEqual("", self.git("status", "--porcelain", "--untracked-files=no").stdout)
        self.reject(self.guard(self.wargs()), message="作業ツリーの中身が HEAD と違う")
        self.reject(self.guard(self.wargs(baseline=base)), message="作業ツリーの中身が HEAD と違う")

    def test_15_parent_replaced_by_a_link(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        os.mkfifo(outside / "old.txt")
        shutil.rmtree(self.wt / "sub")
        os.symlink(outside, self.wt / "sub")
        result = self.guard(self.wargs(), timeout=30)
        self.assertEqual(1, result.rc, result.err)

    def test_16_exclude_columns_must_keep_the_baseline_columns(self) -> None:
        longer = [ERE, "(^|/)zz$"]
        base = self.base(eres=longer)
        self.reject(self.guard(self.wargs(eres=[ERE], baseline=base)))
        self.assertEqual(0, self.guard(self.wargs(eres=longer + ["(^|/)yy$"], baseline=base)).rc)

    def test_17_secret_path_added_after_the_baseline(self) -> None:
        self.write("foo.txt", "secret\n")
        self.git("add", "foo.txt")
        self.git("commit", "-qm", "foo")
        head = self.D()
        base = self.base_for(head)
        self.write("foo.txt", "changed secret\n")
        self.write(".claude/project-profile.yml", 'secret_paths: ["foo.txt"]\n')
        done = self.guard(["exclude", "--dir", str(self.wt), "--reviewed-sha", head, "--base-sha", head, "--previous",
                           self.save("prev.json", json.dumps({"reviewed_sha": head, "base_sha": head,
                                                              "exclude_globs": [".env", ".env.*", ".dev.vars"],
                                                              "exclude_ere": [ERE]}))[0],
                           "--expect-previous-sha256", self.prev_sha("prev.json")])
        self.assertEqual(0, done.rc, done.err)
        result = self.guard(self.wargs(head=head, eres=done.data["exclude_ere"], baseline=base,
                                       allow=(".claude/project-profile.yml",)))
        self.assertEqual(1, result.rc, result.err)
        self.assertEqual(["foo.txt"], result.data["problem_paths"])

    def base_for(self, head: str) -> tuple[str, str]:
        result = self.guard(self.wargs(head=head))
        self.assertEqual(0, result.rc, result.err)
        return self.save("baseline2.json", result.out)

    def prev_sha(self, name: str) -> str:
        return hashlib.sha256((self.root / name).read_bytes()).hexdigest()

    def test_baseline_arguments_come_in_pairs(self) -> None:
        base = self.base()
        result = self.guard(["worktree", "--dir", str(self.wt), "--branch", "task/x", "--head", self.D(), *self.ere_args(),
                             "--baseline", base[0]])
        self.assertEqual(2, result.rc)


if __name__ == "__main__":
    unittest.main()
