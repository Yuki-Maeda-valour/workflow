"""Inventory every Git call in the publish path and require its safe contract."""
import ast
import collections
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "plugins/dev-workflow/skills/ship-task/scripts/loop.sh"
PY_CALLERS = {
    "publish": ROOT / "plugins/dev-workflow/skills/ship-task/scripts/publish-guard.py",
    "loop_state": ROOT / "plugins/dev-workflow/skills/ship-task/scripts/loop-state.py",
    "origin": ROOT / "plugins/dev-workflow/skills/ship-task/scripts/origin-repo.py",
    "digest": ROOT / "plugins/dev-workflow/skills/ship-task/scripts/git-config-digest.py",
}
DOCS = [
    ROOT / "plugins/dev-workflow/skills/do-task/SKILL.md",
    ROOT / "plugins/dev-workflow/skills/do-task/references/base-commit.md",
    ROOT / "plugins/dev-workflow/skills/do-task/references/diff-snapshot-call.md",
    ROOT / "plugins/dev-workflow/skills/ship-task/SKILL.md",
    ROOT / "plugins/dev-workflow/skills/ship-task/references/unattended-mode.md",
    ROOT / "plugins/dev-workflow/skills/ship-task/references/discover-mode.md",
]
REQUIRED = ("core.fsmonitor=", "core.hooksPath=/dev/null", "commit.gpgSign=false", "push.gpgSign=false", "filter.lfs.clean=", "filter.lfs.process=")
DOC_MANIFEST_PATH = ROOT / "scripts/fixtures/safe_git_document_inventory.json"
GIT_COMMAND = re.compile(r"(?<![\w-])git\s+(?=[A-Za-z0-9-])")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def normalize_span(value: str) -> str:
    return " ".join(value.split())


def contains_git_command(value: str) -> bool:
    return bool(GIT_COMMAND.search(value))


def markdown_git_spans(text: str) -> collections.Counter[str]:
    """Extract Git command literals from fenced blocks and inline code spans.

    Fence delimiters are consumed before inline parsing, so a triple backtick is
    never mistaken for three adjacent inline-code delimiters.  A span containing
    a ``git <argv>`` token is retained as written (apart from whitespace), even
    when surrounding quotation begins before ``git``; that conservative rule
    makes a new quoted command reviewable rather than silently discarding it.
    """
    result: collections.Counter[str] = collections.Counter()
    outside: list[str] = []
    fence_char: str | None = None
    fence_len = 0
    fenced: list[str] = []
    for line in text.splitlines():
        match = FENCE.match(line)
        if fence_char is None and match:
            fence_char = match.group(1)[0]
            fence_len = len(match.group(1))
            fenced = []
            continue
        if fence_char is not None:
            if match and match.group(1)[0] == fence_char and len(match.group(1)) >= fence_len:
                for command_line in fenced:
                    normalized = normalize_span(command_line)
                    if contains_git_command(normalized):
                        result[normalized] += 1
                fence_char = None
                fence_len = 0
                continue
            fenced.append(line)
            continue
        outside.append(line)
    # An unterminated fence is still text for inventory purposes; retaining it
    # avoids hiding a command merely because Markdown syntax is malformed.
    if fence_char is not None:
        outside.extend(fenced)
    outside_text = "\n".join(outside)
    index = 0
    while index < len(outside_text):
        if outside_text[index] != "`":
            index += 1
            continue
        end_delimiter = index
        while end_delimiter < len(outside_text) and outside_text[end_delimiter] == "`":
            end_delimiter += 1
        delimiter = outside_text[index:end_delimiter]
        closing = outside_text.find(delimiter, end_delimiter)
        if closing < 0:
            index = end_delimiter
            continue
        span = outside_text[end_delimiter:closing]
        normalized = normalize_span(span)
        if contains_git_command(normalized):
            result[normalized] += 1
        index = closing + len(delimiter)
    return result


def document_manifest() -> dict[str, list[dict[str, object]]]:
    return json.loads(DOC_MANIFEST_PATH.read_text(encoding="utf-8"))


def counter_from_manifest(entries: list[dict[str, object]]) -> collections.Counter[str]:
    counter: collections.Counter[str] = collections.Counter()
    for entry in entries:
        if set(entry) != {"code", "count", "classification", "basis"}:
            raise AssertionError(f"invalid document inventory entry: {entry!r}")
        if entry["classification"] not in {"read", "index/worktree", "commit", "network", "setting-or-example"}:
            raise AssertionError(f"invalid classification: {entry!r}")
        if not isinstance(entry["code"], str) or not isinstance(entry["count"], int) or entry["count"] < 1:
            raise AssertionError(f"invalid document inventory count: {entry!r}")
        if not isinstance(entry["basis"], str) or not entry["basis"]:
            raise AssertionError(f"missing document inventory basis: {entry!r}")
        counter[entry["code"]] += entry["count"]
    return counter


def shell_bare_calls(text: str) -> list[str]:
    """Find executable bare git tokens, including functions, tests and $()."""
    result=[]
    for line in text.splitlines():
        s=line.strip()
        if not s or s.startswith('#') or 'git "${GIT_PRE[@]}"' in s or 'subprocess.run' in s or s.startswith('git = ') or '"origin の' in s or '="(git ' in s:
            continue
        # Quoted reports are prose. Everything else following a shell separator,
        # conditional, substitution, or a function body is an executable site.
        if re.search(r'(?:^|[;(|{]\s*|\b(?:if|then)\s+)git\s+', s):
            result.append(s)
    return result


def python_git_calls(text: str) -> list[ast.List]:
    tree=ast.parse(text)
    found=[]
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == 'git':
            found.append(node)
    return found


def has_safe_starred(argv: ast.List) -> bool:
    return any(isinstance(e, ast.Starred) and isinstance(e.value, ast.Name) and 'SAFE_GIT' in e.value.id for e in argv.elts)


class SafeGitCallersTest(unittest.TestCase):
    def test_shell_inventory_is_only_the_safe_wrappers(self):
        text=LOOP.read_text(encoding='utf-8')
        definition=text.split('GIT_PRE=',1)[1].split('\n',1)[0]
        for key in REQUIRED: self.assertIn(key, definition)
        self.assertIn('G() { git "${GIT_PRE[@]}"', text)
        self.assertEqual([], shell_bare_calls(text))

    def test_python_inventory_uses_starred_safe_prefix_per_call(self):
        for name,path in PY_CALLERS.items():
            text=path.read_text(encoding='utf-8')
            with self.subTest(name=name):
                for key in REQUIRED: self.assertIn(key, text)
                calls=python_git_calls(text)
                self.assertTrue(calls)
                self.assertTrue(all(has_safe_starred(call) for call in calls), ast.unparse(calls[0]))

    def test_document_git_inventory_matches_reviewed_manifest(self):
        manifest=document_manifest()
        self.assertEqual(set(manifest), {str(path.relative_to(ROOT)) for path in DOCS})
        for path in DOCS:
            rel=str(path.relative_to(ROOT))
            with self.subTest(path=rel):
                self.assertEqual(markdown_git_spans(path.read_text(encoding="utf-8")), counter_from_manifest(manifest[rel]))

    def test_markdown_extractor_collects_inline_and_fenced_git_commands(self):
        text = """説明だけ。
`引用: git status --porcelain`
```bash
git -c core.hooksPath=/dev/null commit -m ok
not-git status
```
"""
        self.assertEqual(
            collections.Counter({
                "引用: git status --porcelain": 1,
                "git -c core.hooksPath=/dev/null commit -m ok": 1,
            }),
            markdown_git_spans(text),
        )

    def test_document_prose_mutation_does_not_change_git_inventory(self):
        text=DOCS[0].read_text(encoding="utf-8")
        self.assertEqual(markdown_git_spans(text), markdown_git_spans(text+"\n通常の説明だけを追加する。\n"))

    def test_document_unsafe_git_mutation_is_detected_by_inventory(self):
        path=DOCS[0]
        expected=counter_from_manifest(document_manifest()[str(path.relative_to(ROOT))])
        mutated=path.read_text(encoding="utf-8")+"\n`git -c core.hooksPath=evil commit -m x`\n"
        self.assertNotEqual(expected, markdown_git_spans(mutated))

    def test_shell_mutations_in_function_conditional_and_substitution_are_detected(self):
        base=LOOP.read_text(encoding='utf-8')
        for injected in ('f(){ git status; }', 'if git add x; then :; fi', 'x=$(git commit -m x)', 'git push origin x'):
            with self.subTest(injected=injected): self.assertTrue(shell_bare_calls(base+'\n'+injected))

    def test_python_subprocess_mutation_is_not_safe(self):
        mutated='import subprocess\nsubprocess.run(["git", "status"])\n'
        calls=python_git_calls(mutated)
        self.assertEqual(1,len(calls)); self.assertFalse(has_safe_starred(calls[0]))

if __name__ == '__main__': unittest.main()
