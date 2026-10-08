"""#238: 公開 CLI・実プロセス・正常 filter・文書契約の回帰。

試験入力の承認は実際の利用者の承認ではない。AI の起動は測定しない。
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/dev-workflow/skills/do-task/scripts"
SCRIPT = SCRIPTS / "implementation-git.py"
REFS = SCRIPTS.parent / "references"
SAFE = ["--no-pager", "--no-replace-objects"]
for setting in ("core.quotePath=false", "core.fsmonitor=", "core.hooksPath=/dev/null",
                "core.ignoreCase=false", "core.splitIndex=false", "core.ignoreStat=false",
                "commit.gpgSign=false", "push.gpgSign=false", "filter.lfs.smudge=",
                "filter.lfs.clean=", "filter.lfs.process=", "filter.lfs.required=false"):
    SAFE += ["-c", setting]
PATH_ENV = ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS",
            "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS")
TARGET_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def filters(*names):
    return {"filter.%s.%s" % (name, item): ("false" if item == "required" else "")
            for name in names for item in ("clean", "smudge", "process", "required")}


def tokens(mapping, reverse=False):
    entries = list(mapping.items())
    if reverse:
        entries.reverse()
    result = ["GIT_CONFIG_COUNT=%d" % len(entries)]
    for i, (key, value) in enumerate(entries):
        result += ["GIT_CONFIG_KEY_%d=%s" % (i, key), "GIT_CONFIG_VALUE_%d=%s" % (i, value)]
    if reverse:
        result.reverse()
    return ("\0".join(result) + "\0").encode("utf-8")


def digest(cwd, accept=None, mapping=None):
    value = {"version": 1, "cwd": str(Path(cwd).resolve()), "accept": accept,
             "filters": mapping or {}}
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


# 記録だけを加える Git と事前検査。通常 Git の代行や AI dispatcher は作らない。
RECORDER = r'''
import json, os, pathlib, subprocess, sys, time
state = json.loads(pathlib.Path(os.environ["IG_STATE"]).read_text())
a = sys.argv[1:]
pre = a and a[0] == "precheck"
parent_cmd = pathlib.Path("/proc/%d/cmdline" % os.getppid())
parent = parent_cmd.read_bytes() if parent_cmd.exists() else b""
kind = "precheck" if pre else ("precheck-git" if b"diff-snapshot.sh" in parent else "git")
record = {"kind": kind, "args": a,
          "env": {k:v for k,v in os.environ.items() if k.startswith("GIT_")}}
with open(os.environ["IG_TRACE"], "a") as f:
    f.write(json.dumps(record, ensure_ascii=True) + "\n")
if pre:
    time.sleep(state.get("pre_sleep", 0))
    sys.stdout.buffer.write(bytes.fromhex(state["raw"]))
    sys.stderr.write("precheck-note\n")
    sys.exit(state.get("pre_rc", 0))
if state.get("real_git"):
    os.execv(state["real_git"], [state["real_git"], *a])
ops = ("config", "rev-parse", "cat-file", "status", "diff", "log", "show", "ls-files")
i = next(i for i, x in enumerate(a) if x in ops)
cmd = a[i:]
if cmd[0] == "config":
    time.sleep(state.get("config_sleep", 0))
    key = cmd[-1]
    values = {os.environ.get("GIT_CONFIG_KEY_%d" % i): os.environ.get("GIT_CONFIG_VALUE_%d" % i)
              for i in range(int(os.environ.get("GIT_CONFIG_COUNT", "0")))}
    if key in state.get("effective", {}):
        rc, value = state["effective"][key]
    else:
        rc, value = (0, values[key]) if key in values else (1, "")
    sys.stdout.write(value + "\n" if rc == 0 else value)
    sys.exit(rc)
if cmd[0] == "rev-parse":
    sys.stdout.write((state.get("top", a[a.index("-C")+1]) if "--show-toplevel" in cmd
                      else "a" * 40) + "\n")
    sys.exit(state.get("rev_rc", 0))
if cmd[0] == "cat-file":
    sys.stdout.write(state.get("kind", "blob") + "\n")
    sys.exit(state.get("kind_rc", 0))
time.sleep(state.get("git_sleep", 0))
sys.stdout.write(state.get("output", "observed\n"))
sys.exit(state.get("git_rc", 0))
'''


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="implementation-git-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith("GIT_"):
                self.env.pop(key)
        self.env.update(HOME=str(self.root / "home"), GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.devnull, PYTHONDONTWRITEBYTECODE="1")
        Path(self.env["HOME"]).mkdir()
        self.real_git = shutil.which("git")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state.json"
        self.trace = self.root / "trace.jsonl"
        self.recorder = self.root / "recorder.py"
        self.recorder.write_text(RECORDER)
        wrapper = self.bin / "git"
        wrapper.write_text("#!" + sys.executable + "\n" + RECORDER)
        wrapper.chmod(0o755)
        self.env.update(IG_STATE=str(self.state), IG_TRACE=str(self.trace))
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
        self.settings = {"raw": ""}
        self.save()
        self.script = SCRIPT

    def save(self, **changes):
        self.settings.update(changes)
        self.state.write_text(json.dumps(self.settings))

    def events(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def clear(self):
        self.trace.write_text("")

    def normal(self):
        return [e for e in self.events() if e["kind"] == "git" and
                any(op in e["args"] for op in ("status", "diff", "log", "show", "ls-files"))]

    def call(self, *args, env=None, script=None):
        child = dict(self.env)
        child.update(env or {})
        return subprocess.run([sys.executable, "-B", str(script or self.script), *args],
                              env=child, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=12)

    def prepare(self, accept=None, cwd=None):
        args = ["prepare", "--cwd", str(cwd or self.repo)]
        if accept is not None:
            args += ["--accept", accept]
        result = self.call(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def read_op(self, p, *op, env=None, accept=None, cwd=None, script=None):
        args = ["run", "--cwd", str(cwd or p["cwd"]), "--expect-context-sha256", p["context_sha256"]]
        value = p["accept"] if accept is None else accept
        if value is not None:
            args += ["--accept", value]
        return self.call(*args, *op, env=env, script=script)

    def stub(self, mapping=None):
        folder = self.root / "copy"
        folder.mkdir(exist_ok=True)
        self.script = folder / SCRIPT.name
        shutil.copy2(SCRIPT, self.script)
        pre = folder / "diff-snapshot.sh"
        pre.write_text('exec "$IG_PYTHON" "$IG_RECORDER" precheck "$@"\n')
        pre.chmod(0o755)
        self.env.update(IG_PYTHON=sys.executable, IG_RECORDER=str(self.recorder))
        self.save(raw=tokens(mapping).hex() if mapping else "")

    def assert_stopped(self, result, rc, no_git=False):
        self.assertEqual(result.returncode, rc, result.stderr)
        self.assertEqual(self.normal(), [])
        if no_git:
            self.assertEqual(self.events(), [])


class StubContractTests(Harness):
    def setUp(self):
        super().setUp()
        self.mapping = filters("通常 名")
        self.stub(self.mapping)

    def test_context_canonical_order_and_binding(self):
        p = self.prepare("approval")
        self.assertEqual(set(p), {"version", "cwd", "accept", "context_sha256"})
        self.assertEqual(p["context_sha256"], digest(self.repo, "approval", self.mapping))
        self.save(raw=tokens(self.mapping, reverse=True).hex())
        self.assertEqual(self.prepare("approval"), p)
        self.assertEqual(self.read_op(p, "status").returncode, 0)
        second = self.root / "other"
        second.mkdir()
        for changes in ({"accept": "other"}, {"cwd": second}):
            self.clear()
            self.assert_stopped(self.read_op(p, "status", **changes), 22)
            self.assertEqual([e for e in self.events() if e["kind"] == "git"], [])
        link = self.root / "link"
        link.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(self.prepare("approval", cwd=link), p)
        for mapping in ({}, filters("通常 名", "second"), filters("renamed")):
            self.save(raw=tokens(mapping).hex() if mapping else "")
            self.clear()
            self.assert_stopped(self.read_op(p, "status"), 22)
            new = self.prepare("approval")
            self.assertNotEqual(new["context_sha256"], p["context_sha256"])
            self.assertEqual(self.read_op(new, "status").returncode, 0)

    def test_same_environment_order_and_original_bytes(self):
        for name in ("通常 名", "a=b 日本語"):
            with self.subTest(name=name):
                mapping = filters(name)
                self.save(raw=tokens(mapping).hex())
                p = self.prepare("approval")
                self.clear()
                inherited = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "filter.old.clean",
                             "GIT_CONFIG_VALUE_0": "old", "GIT_CONFIG_PARAMETERS": "invalid"}
                inherited.update({k: "1" for k in PATH_ENV})
                r = self.read_op(p, "show", "--rev", "HEAD", env=inherited)
                self.assertEqual(r.returncode, 0, r.stderr)
                events = self.events()
                self.assertEqual(events[0]["kind"], "precheck")
                self.assertNotIn("GIT_CONFIG_COUNT", events[0]["env"])
                expected = None
                for e in events:
                    for key, value in (("GIT_NO_LAZY_FETCH", "1"), ("GIT_OPTIONAL_LOCKS", "0"), ("GIT_TERMINAL_PROMPT", "0")):
                        self.assertEqual(e["env"][key], value)
                    for key in PATH_ENV + ("GIT_CONFIG_PARAMETERS",):
                        self.assertNotIn(key, e["env"])
                    if e["kind"] == "git":
                        active = e["env"]
                        actual = {active["GIT_CONFIG_KEY_%d" % i]: active["GIT_CONFIG_VALUE_%d" % i]
                                  for i in range(int(active["GIT_CONFIG_COUNT"]))}
                        self.assertEqual(actual, mapping)
                        expected = active if expected is None else expected
                        self.assertEqual(active, expected)
                        self.assertEqual(e["args"][2:2+len(SAFE)], SAFE)
                ops = [next(x for x in e["args"] if x in ("config", "rev-parse", "show"))
                       for e in events[1:]]
                self.assertEqual(ops, ["config"] * 4 + ["rev-parse", "rev-parse", "show"])
                self.assertNotIn("GIT_CONFIG_COUNT", self.env)

    def test_broken_machine_outputs_stop_before_git(self):
        p = self.prepare()
        good = tokens(self.mapping)
        cases = [good[:-1], b"\xff\0", b"GIT_CONFIG_COUNT=1\0GIT_CONFIG_KEY_0=filter.\xed\xa0\x80.clean\0GIT_CONFIG_VALUE_0=\0", b"GIT_CONFIG_COUNT=4\0GIT_CONFIG_COUNT=4\0",
                 good + good.split(b"\0")[1] + b"\0", good + good.split(b"\0")[2] + b"\0",
                 good.replace(b"GIT_CONFIG_COUNT=4\0", b""),
                 good.replace(b"COUNT=4", b"COUNT=x"), good.replace(b"COUNT=4", b"COUNT=5"), good.replace(b"COUNT=4", b"COUNT=-1"),
                 good + b"OTHER=1\0",
                 good.replace(b"GIT_CONFIG_KEY_0", b"GIT_CONFIG_KEY_9"),
                 good.replace(b"GIT_CONFIG_VALUE_0=\0", b""),
                 tokens({k:v for k,v in self.mapping.items() if not k.endswith(".required")}),
                 good.replace(b"VALUE_0=\0", b"VALUE_0=cat\0"),
                 good.replace(b"VALUE_3=false", b"VALUE_3=true"),
                 good.replace(b".smudge", b".clean"),
                 good.replace(b"filter.", b"other.", 1),
                 good.replace(b".clean", b".unknown", 1), b"\0", b"GIT_CONFIG_COUNT=0\0junk\0"]
        for raw in cases:
            with self.subTest(raw=raw):
                self.save(raw=raw.hex())
                self.clear()
                r = self.call("prepare", "--cwd", str(self.repo))
                self.assert_stopped(r, 20)
                self.assertEqual(r.stdout, "")
                self.assertEqual([e for e in self.events() if e["kind"] == "git"], [])
                self.clear()
                self.assert_stopped(self.read_op(p, "status"), 20)

    def test_effective_values_all_four_and_rc(self):
        for key in self.mapping:
            for rc, value in ((0, "unexpected"), (1, ""), (7, "")):
                with self.subTest(key=key, rc=rc):
                    self.save(effective={key: [rc, value]})
                    self.clear()
                    r = self.call("prepare", "--cwd", str(self.repo))
                    self.assert_stopped(r, 20)
                    self.assertEqual(r.stdout, "")
        self.save(effective={"filter.通常 名.required": [0, ""]})
        self.clear()
        self.assert_stopped(self.call("prepare", "--cwd", str(self.repo)), 20)

    def test_precheck_exit_codes_timeout_and_git_errors(self):
        p = self.prepare()
        for rc, expected in ((2, 2), (20, 20), (22, 22), (9, 20)):
            self.save(pre_rc=rc)
            self.clear()
            r = self.read_op(p, "status")
            self.assert_stopped(r, expected)
            self.assertIn(str(rc), r.stderr)
        self.save(pre_rc=0, git_rc=7, output="partial\n")
        r = self.read_op(p, "status")
        self.assertEqual((r.returncode, r.stdout), (20, "partial\n"))
        source = self.script.read_text()
        self.assertIn("PRECHECK_TIMEOUT = 120", source)
        self.assertIn("GIT_TIMEOUT = 30", source)
        self.script.write_text(source.replace("PRECHECK_TIMEOUT = 120", "PRECHECK_TIMEOUT = 0.05")
                               .replace("GIT_TIMEOUT = 30", "GIT_TIMEOUT = 0.05"))
        self.save(git_rc=0, pre_sleep=0.2)
        self.clear()
        self.assert_stopped(self.read_op(p, "status"), 20)
        self.save(pre_sleep=0, config_sleep=0.2)
        self.clear()
        self.assert_stopped(self.read_op(p, "status"), 20)
        self.save(config_sleep=0, git_sleep=0.2)
        r = self.read_op(p, "status")
        self.assertEqual(r.returncode, 20, r.stderr)
        self.assertEqual(r.stdout, "")

    def test_target_environment_missing_dependency_and_wrong_root(self):
        p = self.prepare()
        for key in TARGET_ENV:
            self.clear()
            self.assert_stopped(self.read_op(p, "status", env={key: "elsewhere"}), 2, no_git=True)
        self.save(top=str(self.root))
        self.clear()
        self.assert_stopped(self.read_op(p, "status"), 20)
        self.clear()
        self.assert_stopped(self.read_op(p, "status", env={"PATH": str(self.bin)}), 20, no_git=True)
        self.script.with_name("diff-snapshot.sh").unlink()
        self.clear()
        self.assert_stopped(self.read_op(p, "status"), 20, no_git=True)

    def test_blob_only_and_resolution_failure(self):
        p = self.prepare()
        for kind, rc in (("tree", 0), ("commit", 0), ("blob", 1)):
            self.save(kind=kind, kind_rc=rc)
            self.clear()
            self.assert_stopped(self.read_op(p, "show", "--file", "dir"), 20)
        self.save(kind="blob", kind_rc=0, rev_rc=1)
        self.clear()
        self.assert_stopped(self.read_op(p, "show", "--file", "file"), 20)

    def test_all_argument_boundaries_before_precheck(self):
        p = self.prepare()
        bad = [(x,) for x in ("add", "commit", "checkout", "restore", "reset", "fetch", "push", "config", "unknown", "status; echo x")]
        bad += [("status", x, "x") for x in ("-c", "--config-env", "--output", "--ext-diff", "--textconv", "--exec", "--command", "--form")]
        bad += [(op, "--path", path) for op in ("status", "diff", "log", "ls-files")
                for path in ("", "/absolute", "..", "a/../b")]
        bad += [("show", "--file", path) for path in ("", "/absolute", "../x")]
        bad += [("show", "--rev=" + rev) for rev in ("", "-HEAD", "x\ny", "x"*1025)]
        bad += [("diff", "--context", n) for n in ("-1", "101", "x")]
        bad += [("log", "--limit", n) for n in ("0", "1001", "x")]
        bad += [("diff", "--target", "HEAD"), ("diff", "--cached", "--base", "HEAD", "--target", "HEAD"),
                ("show", "--file", "a", "--format=patch"), ("show", "--path", "a"),
                ("status", "--format", "patch"), ("status", "--untracked", "bad"),
                ("ls-files", "--mode", "bad"), ("log", "--format", "patch"), ("diff", "--format", "fuller")]
        for op in bad:
            with self.subTest(op=op):
                self.clear()
                self.assert_stopped(self.read_op(p, *op), 2, no_git=True)
        for args in (("run", "--cwd", str(self.repo), "status"),
                     ("prepare", "--cwd", str(self.repo), "--expect-context-sha256", "a"*64),
                     ("prepare", "--cwd", str(self.root / "missing")),
                     ("prepare", "--cwd", str(self.repo), "status")):
            self.clear()
            self.assert_stopped(self.call(*args), 2, no_git=True)

    def test_help_strict_input_and_missing_context(self):
        for args in (("--help",), ("prepare", "--help"), ("run", "--help"),
                     *(("run", op, "--help") for op in ("status", "diff", "log", "show", "ls-files"))):
            self.clear()
            r = self.call(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout)
            self.assertEqual(self.events(), [])
        for value in ("", "a"*63, "A"*64, "g"*64):
            self.clear()
            self.assert_stopped(self.call("run", "--cwd", str(self.repo),
                                         "--expect-context-sha256", value, "status"), 2, no_git=True)
        spec = importlib.util.spec_from_file_location("implementation_git_test_module", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for value in ("HEAD\0other", "\ud800", "\udcff"):
            with self.subTest(value=repr(value)), mock.patch.object(module, "precheck") as pre:
                with self.assertRaises(module.Failure) as caught:
                    module.main(["run", "--cwd", str(self.repo), "--expect-context-sha256", "a"*64,
                                 "show", "--rev", value])
                self.assertEqual(caught.exception.code, 2)
                pre.assert_not_called()

    def test_duplicate_options_all_spellings(self):
        p = self.prepare()
        cases = [("status", "--format", "short"), ("status", "--untracked", "all"),
                 ("diff", "--base", "HEAD"), ("diff", "--target", "HEAD"),
                 ("diff", "--format", "stat"), ("diff", "--context", "3"),
                 ("log", "--from", "HEAD"), ("log", "--to", "HEAD"),
                 ("log", "--limit", "2"), ("log", "--format", "fuller"),
                 ("show", "--rev", "HEAD"), ("show", "--format", "patch"),
                 ("show", "--file", "file"), ("ls-files", "--mode", "tracked")]
        for op, key, value in cases:
            for first in ([key, value], [key+"="+value]):
                for second in ([key, value], [key+"="+value]):
                    self.clear()
                    self.assert_stopped(self.read_op(p, op, *first, *second), 2, no_git=True)
        for key, value in (("--cwd", str(self.repo)), ("--accept", "a"), ("--expect-context-sha256", "a"*64)):
            for a in ([key, value], [key+"="+value]):
                for b in ([key, value], [key+"="+value]):
                    args = ["run", "--cwd", str(self.repo)] if key != "--cwd" else ["run"]
                    if key != "--expect-context-sha256":
                        args += ["--expect-context-sha256", p["context_sha256"]]
                    self.clear()
                    self.assert_stopped(self.call(*args, *a, *b, "status"), 2, no_git=True)
        self.clear()
        self.assert_stopped(self.read_op(p, "diff", "--cached", "--cached"), 2, no_git=True)

    def test_defaults_allowed_options_and_forced_flags(self):
        p = self.prepare()
        cases = [("status",), ("diff",), ("log",), ("show",), ("ls-files",)]
        cases += [("status", "--format", f, "--untracked", u) for f in ("short", "porcelain") for u in ("normal", "all", "no")]
        cases += [("diff", "--format", f, "--context", n) for f in ("patch", "stat", "name-only", "name-status") for n in ("0", "100")]
        cases += [("diff", "--base", "HEAD"), ("diff", "--cached", "--base", "HEAD"), ("diff", "--base", "HEAD", "--target", "HEAD")]
        cases += [("log", "--format", f, "--limit", n) for f in ("oneline", "fuller") for n in ("1", "1000")]
        cases += [("log", "--from", "HEAD"), ("log", "--from", "HEAD", "--to", "HEAD"), ("log", "--to", "HEAD")]
        cases += [("show", "--format", f) for f in ("patch", "stat", "name-only")]
        cases += [("show", "--file", "普通 空白.txt"), ("show", "--rev", "x"*1024), ("ls-files", "--mode", "untracked")]
        for op in cases:
            with self.subTest(op=op):
                self.clear()
                r = self.read_op(p, *op)
                self.assertEqual(r.returncode, 0, r.stderr)
                cmd = self.normal()[-1]["args"]
                if op[0] in ("status", "diff"):
                    self.assertIn("--ignore-submodules=dirty", cmd)
                if op[0] in ("diff", "show"):
                    for flag in ("--no-ext-diff", "--no-textconv", "--no-color"):
                        self.assertIn(flag, cmd)
                if op[0] in ("log", "show") and "--file" not in op:
                    for flag in ("--no-show-signature", "--no-decorate", "--no-color"):
                        self.assertIn(flag, cmd)
                if op == ("diff",): self.assertIn("--unified=3", cmd)
                if op == ("log",):
                    self.assertIn("--max-count=20", cmd)
                    self.assertIn("--format=oneline", cmd)
        for op in ("status", "diff", "log", "ls-files"):
            self.clear()
            self.assertEqual(self.read_op(p, op, "--path", "普通 [x]", "--path", "A").returncode, 0)
            self.assertEqual(self.normal()[-1]["args"][-3:], ["--", ":(literal)普通 [x]", ":(literal)A"])


class RealRepositoryTests(Harness):
    def setUp(self):
        super().setUp()
        self.save(real_git=self.real_git)
        self.git("init", "-q")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.names = ("normal.txt", "space name.txt", "日本語 [a].txt", "Case.txt", "case.txt")
        for name in self.names:
            (self.repo / name).write_text("first\n")
        (self.repo / "dir").mkdir()
        (self.repo / "dir/file").write_text("nested\n")
        (self.repo / "link").symlink_to("normal.txt")
        self.git("add", "--all")
        self.git("commit", "-qm", "initial")
        self.initial = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "normal.txt").write_text("second\n")
        self.git("add", "normal.txt")
        self.git("commit", "-qm", "second")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("config", "filter.通常 名.clean", "cat")
        self.git("config", "filter.通常 名.smudge", "cat")
        self.git("config", "filter.通常 名.required", "true")
        (self.repo / ".gitattributes").write_text('*.txt "filter=通常 名"\n')
        (self.repo / "normal.txt").write_text("working\n")
        self.accept = self.approval()

    def git(self, *args):
        return subprocess.run([self.real_git, "-C", str(self.repo), *SAFE, *args],
                              env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    def approval(self):
        r = self.call("prepare", "--cwd", str(self.repo))
        self.assertEqual(r.returncode, 22, r.stderr)
        values = re.findall(r"承認ダイジェスト:\s*([a-f0-9]{64})", r.stderr)
        self.assertTrue(values, r.stderr)
        return values[-1]

    def verify_five(self, script=None):
        p = self.prepare(self.accept)
        self.clear()
        expected = {"status": " M normal.txt\n?? .gitattributes\n", "ls-files": "\n".join(sorted((*self.names, "dir/file", "link")))+"\n"}
        for op in ("status", "diff", "log", "show", "ls-files"):
            r = self.read_op(p, op, script=script)
            self.assertEqual(r.returncode, 0, r.stderr)
            if op in expected:
                self.assertEqual(r.stdout, expected[op])
            elif op == "diff":
                self.assertIn("-second\n+working\n", r.stdout)
            elif op == "log":
                self.assertEqual(r.stdout, self.head+" second\n"+self.initial+" initial\n")
            else:
                self.assertIn("-first\n+second\n", r.stdout)
            active = self.normal()[-1]["env"]
            self.assertIn("GIT_CONFIG_COUNT", active)
            self.assertEqual(int(active["GIT_CONFIG_COUNT"]), 4)
            got = {active["GIT_CONFIG_KEY_%d" % i]: active["GIT_CONFIG_VALUE_%d" % i] for i in range(4)}
            self.assertEqual(got, filters("通常 名"))
        return p

    def test_normal_filter_separate_processes_all_five(self):
        self.verify_five()

    def test_mutation_removing_only_operation_environment_is_detected(self):
        self.verify_five()
        folder = self.root / "mutation"
        shutil.copytree(SCRIPTS, folder)
        mutant = folder / SCRIPT.name
        source = mutant.read_text()
        old = "result = run_git(cwd, env, *command)"
        self.assertEqual(source.count(old), 1)
        mutant.write_text(source.replace(old, "result = run_git(cwd, clean_env(), *command)"))
        with self.assertRaisesRegex(AssertionError, "GIT_CONFIG_COUNT"):
            self.verify_five(script=mutant)

    def test_regular_config_change_old_rejected_new_approval_resumes(self):
        p = self.prepare(self.accept)
        self.git("config", "filter.通常 名.clean", "cat --")
        self.clear()
        self.assert_stopped(self.read_op(p, "status"), 22)
        new_accept = self.approval()
        self.assertNotEqual(new_accept, self.accept)
        new = self.prepare(new_accept)
        self.assertNotEqual(new["context_sha256"], p["context_sha256"])
        self.assertEqual(self.read_op(new, "status").returncode, 0)

    def test_literal_paths_all_environment_variants_and_repeat(self):
        p = self.prepare(self.accept)
        for name in self.names:
            for setting in ({}, *({k:"1"} for k in PATH_ENV), {k:"1" for k in PATH_ENV}):
                r = self.read_op(p, "ls-files", "--path", name, env=setting)
                self.assertEqual((r.returncode, r.stdout), (0, name+"\n"), r.stderr)
                r = self.read_op(p, "show", "--file", name, env=setting)
                self.assertEqual((r.returncode, r.stdout), (0, "second\n" if name == "normal.txt" else "first\n"), r.stderr)
        for name in ("space name.txt", "Case.txt"):
            (self.repo / name).write_text("changed\n")
        for op in ("status", "diff", "log", "ls-files"):
            extra = ["--format", "name-only"] if op == "diff" else []
            r = self.read_op(p, op, *extra, "--path", "normal.txt", "--path", "Case.txt")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("space name.txt", r.stdout)
            if op != "log":
                self.assertIn("normal.txt", r.stdout)
                self.assertIn("Case.txt", r.stdout)
        self.assertEqual(self.read_op(p, "show", "--file", "link").stdout, "normal.txt")
        self.assertEqual(self.read_op(p, "show", "--file", "dir").returncode, 20)

    def test_index_bytes_stat_refs_and_tracked_files_unchanged(self):
        p = self.prepare(self.accept)
        def snapshot():
            selected = [self.repo / ".git/index", self.repo / ".git/HEAD"]
            selected += list((self.repo / ".git/refs").rglob("*"))
            selected += [self.repo / name for name in self.names] + [self.repo / "dir/file", self.repo / "link"]
            result = {}
            for path in selected:
                if path.is_dir(): continue
                st = path.lstat()
                # atime は読取りでも変化し得る。書込みを示す mtime/ctime と識別子は保持する。
                data = os.readlink(path) if path.is_symlink() else path.read_bytes()
                result[str(path)] = (data, st.st_mode, st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            return result
        before = snapshot()
        for op in ("status", "diff", "log", "show", "ls-files"):
            r = self.read_op(p, op)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(snapshot(), before)

    def test_no_filter_plain_repository(self):
        (self.repo / ".gitattributes").unlink()
        self.git("config", "--remove-section", "filter.通常 名")
        # precheck が使う2本だけでも prepare と run が実際に成功する。
        folder = self.root / "minimal-scripts"
        folder.mkdir()
        for name in ("implementation-git.py", "diff-snapshot.sh"):
            shutil.copy2(SCRIPTS / name, folder / name)
        self.assertFalse((folder / "secret-profiles.py").exists())
        self.script = folder / "implementation-git.py"
        p = self.prepare()
        self.assertEqual(p["accept"], None)
        self.assertEqual(p["context_sha256"], digest(self.repo))
        self.assertEqual(self.read_op(p, "status").returncode, 0)


class RuntimeAndDocumentTests(Harness):
    def test_runtime_actual_document_command_missing_python_import_and_dependency(self):
        text = (REFS / "runtime-requirements.md").read_text()
        section = text.split("<!-- implementation-git-runtime-check:start -->", 1)[1].split("<!-- implementation-git-runtime-check:end -->", 1)[0]
        snippet = re.search(r"```bash\n(.*?)\n```", section, re.S).group(1)
        folder = self.root / "runtime"
        folder.mkdir()
        for name in ("implementation-git.py", "diff-snapshot.sh"):
            shutil.copy2(SCRIPTS / name, folder / name)
        isolated = self.root / "isolated"
        isolated.mkdir()
        (isolated / "git").symlink_to(self.bin / "git")
        env = dict(self.env, PATH=str(isolated))
        bash = shutil.which("bash")
        def check():
            return subprocess.run([bash, "-c", snippet, "runtime-check", str(folder)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(check().returncode, 20)
        py = isolated / "python3"
        py.write_text("#!/bin/sh\nexit 126\n")
        py.chmod(0o755)
        self.assertEqual(check().returncode, 20)
        py.write_text("#!/does-not-exist\n")
        self.assertEqual(check().returncode, 20)
        # 同じ短い起動前プログラム内で import 失敗を起こす。通常 Git は呼ばない。
        blocked = """import builtins
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'hashlib':
        raise ImportError('fixture')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
"""
        py.write_text("#!" + sys.executable + "\nimport sys\nexec(" + repr(blocked) + "+sys.stdin.read())\n")
        r = check()
        self.assertEqual(r.returncode, 20)
        self.assertIn("ImportError", r.stderr)
        py.unlink()
        py.symlink_to(sys.executable)
        self.assertFalse((folder / "secret-profiles.py").exists())
        self.assertEqual(check().returncode, 0)
        for name in ("implementation-git.py", "diff-snapshot.sh"):
            target = folder / name
            old = target.read_bytes()
            target.unlink()
            self.assertEqual(check().returncode, 20)
            target.write_bytes(old)
            target.chmod(0o755)
        target = folder / "implementation-git.py"
        target.write_text("def invalid(:\n")
        self.assertEqual(check().returncode, 20)
        self.assertEqual(self.events(), [])

    def test_actual_five_handoff_documents_and_limits(self):
        skill = (SCRIPTS.parent / "SKILL.md").read_text()
        ext = (REFS / "external-runners.md").read_text()
        protocol = (REFS / "review-protocol.md").read_text()
        sections = {
            "初回": skill.split("**内蔵に解決されたとき**",1)[1].split("### 実装の委託先",1)[0],
            "条件 B": ext.split("**条件 B:",1)[1].split("\n",1)[0],
            "再依頼": protocol.split("Git 対象の同じ内蔵 implementer への再依頼",1)[1].split("\n",1)[0],
            "外部失敗後": ext.split("### 12-7. 引き継ぎの発火条件",1)[1].split("\n\n",2)[1],
            "M2": protocol.split("- **M2 個別再委託**",1)[1].split("    - フル段階",1)[0],
        }
        for name, section in sections.items():
            with self.subTest(route=name):
                for word in ("implementation-git.md", "必要環境確認", "prepare", "失敗時"):
                    self.assertIn(word, section)
                self.assertRegex(section, "起動しない|再依頼しない|起動・再依頼せず停止")
        self.assertIn("既存の設定検査・比較・タスク保護が成功", sections["外部失敗後"])
        self.assertIn("走行中でない", sections["M2"])
        self.assertIn("本文", sections["M2"])
        self.assertIn("implementation-git.md", (REFS / "base-commit.md").read_text())
        doc = (REFS / "implementation-git.md").read_text()
        for word in ("runtime-requirements.md", "非 Git", "品質コマンド", "未停止プロセス", "比較不能", "利用者環境全体", "120秒", "30秒", "部分的な stdout"):
            self.assertIn(word, doc)
        runtime = (REFS / "runtime-requirements.md").read_text()
        for word in ("argparse", "ast", "hashlib", "json", "os", "pathlib", "re", "subprocess", "sys", "起動・再依頼をせず停止"):
            self.assertIn(word, runtime)


if __name__ == "__main__":
    unittest.main()
