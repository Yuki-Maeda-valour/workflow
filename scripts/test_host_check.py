"""host-check.py の回帰(H31: ホスト CLI の証明 / H34: allowed-tools による権限の追加)。

実物のホスト CLI は起動しない。実 hook の効き方の確認は loop.sh --prove-host の実動確認で行う。
"""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/host-check.py'
GUARD = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/environment-guard.py'
SKILLS = ROOT / 'plugins/dev-workflow/skills'


def load():
    spec = importlib.util.spec_from_file_location('host_check', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HC = load()


def run(*args, env=None):
    return subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), *map(str, args)],
                          text=True, capture_output=True, env=env, timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(os.path.realpath(self.tmp.name))


class IdentityTests(Base):
    def make_host(self, name='claude', body=b'#!/bin/sh\necho stub\n'):
        path = self.base / name
        path.write_bytes(body)
        path.chmod(0o755)
        return path

    def test_identity_resolves_link_and_hashes(self):
        host = self.make_host()
        link = self.base / 'bin'
        link.mkdir()
        (link / 'claude').symlink_to(host)
        value = HC.identity(str(link / 'claude'))
        self.assertEqual(value['realpath'], str(host))
        self.assertEqual(value['kind'], 'script')
        self.assertEqual(value['sha256'], HC.sha256(host.read_bytes()))
        self.assertEqual(HC.parse_identity(json.dumps(value)), value)

    def test_identity_rejects_non_executable_fifo_and_relative(self):
        plain = self.base / 'plain'
        plain.write_text('x')
        fifo = self.base / 'fifo'
        os.mkfifo(fifo)
        for path in (str(plain), str(fifo), 'claude'):
            with self.subTest(path=path), self.assertRaises(HC.Stop):
                HC.identity(path)

    def test_same_detects_in_place_rewrite_and_replacement(self):
        host = self.make_host()
        held = HC.identity(str(host))
        HC.same(str(host), held, False)
        HC.same(str(host), held, True)
        # 同じ inode の書き換え(内容・ctime が変わる)
        with open(host, 'r+b') as f:
            f.seek(0, 2)
            f.write(b'# changed\n')
        with self.assertRaises(HC.Stop):
            HC.same(str(host), held, False)
        # 別の inode への差し替え(内容は同じ)
        host2 = self.make_host()
        held2 = HC.identity(str(host2))
        repl = self.base / 'repl'
        repl.write_bytes(host2.read_bytes())
        repl.chmod(0o755)
        os.replace(repl, host2)
        with self.assertRaises(HC.Stop):
            HC.same(str(host2), held2, False)

    def test_same_detects_ctime_only_change(self):
        host = self.make_host(body=b'#!/bin/sh\n# aaaa\n')
        held = HC.identity(str(host))
        st = os.stat(host)
        time.sleep(0.05)  # ctime は粗い時計で刻まれるので、作成と同じ刻みで書き換えない
        with open(host, 'r+b') as f:
            f.seek(len(b'#!/bin/sh\n# '))
            f.write(b'bbbb')
        os.utime(host, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(os.stat(host).st_size, st.st_size)
        with self.assertRaises(HC.Stop):
            HC.same(str(host), held, False)

    def test_same_detects_path_retarget(self):
        a = self.make_host('a')
        held = HC.identity(str(a))
        with self.assertRaises(HC.Stop):
            HC.same(str(self.base / 'b'), held, False)


class ProofTests(Base):
    def setUp(self):
        super().setUp()
        self.host = self.base / 'claude'
        self.host.write_bytes(b'\x7fELF stub')
        self.host.chmod(0o755)
        self.held = HC.identity(str(self.host))
        self.store = str(self.base / 'store/dev-workflow/loop/host-proofs')
        self.version = self.base / 'version.txt'
        self.version.write_text('2.1.289 (Claude Code)\n')
        self.help = self.base / 'help.txt'
        self.help.write_text('Usage: claude [options]\n  -p, --print  Print response and exit\n')
        self.shape = "-p --output-format json --setting-sources user --permission-mode acceptEdits"
        self.probe = {'tool': 'Write', 'hook': 'invoked', 'decision': 'deny', 'file_absent': True}

    def write(self, shape=None):
        return HC.proof_write(self.store, self.held, shape or self.shape, str(self.version), str(self.help), self.probe, '9.9.9')

    def test_missing_then_written_then_matches(self):
        with self.assertRaises(HC.Missing):
            HC.load_proof(self.store, self.held, self.shape)
        path = self.write()
        self.assertTrue(Path(path).is_file())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        HC.proof_match(self.store, self.held, self.shape, str(self.version), str(self.help))

    def test_cli_exit_codes(self):
        ident = json.dumps(self.held)
        p = run('proof-get', '--store', self.store, '--identity', ident, '--shape', self.shape)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.write()
        p = run('proof-get', '--store', self.store, '--identity', ident, '--shape', self.shape)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = run('proof-match', '--store', self.store, '--identity', ident, '--shape', self.shape,
                '--version-file', self.version, '--help-file', self.help)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_other_shape_or_entity_needs_its_own_proof(self):
        self.write()
        with self.assertRaises(HC.Missing):
            HC.load_proof(self.store, self.held, self.shape + ' --permission-mode auto')
        other = dict(self.held, realpath=str(self.base / 'elsewhere'))
        with self.assertRaises(HC.Missing):
            HC.load_proof(self.store, other, self.shape)

    def test_version_or_help_change_stops(self):
        self.write()
        self.version.write_text('2.1.290 (Claude Code)\n')
        with self.assertRaises(HC.Stop):
            HC.proof_match(self.store, self.held, self.shape, str(self.version), str(self.help))
        self.version.write_text('2.1.289 (Claude Code)\n')
        self.help.write_text(self.help.read_text() + '  --bare  now default for -p\n')
        with self.assertRaises(HC.Stop):
            HC.proof_match(self.store, self.held, self.shape, str(self.version), str(self.help))

    def test_forged_or_incomplete_proof_stops(self):
        path = Path(self.write())
        good = json.loads(path.read_text())
        for change in ({'probe': {'tool': 'Write', 'hook': 'skipped', 'decision': 'deny', 'file_absent': True}},
                       {'probe': {'tool': 'Edit', 'hook': 'invoked', 'decision': 'deny', 'file_absent': True}},
                       {'ino': good['ino'] + 1}, {'shape_sha256': '0' * 64}, {'version': 2},
                       {'help_sha256': 'x'}):
            with self.subTest(change=change):
                path.write_text(json.dumps(dict(good, **change)))
                with self.assertRaises(HC.Stop):
                    HC.load_proof(self.store, self.held, self.shape)
        path.unlink()
        os.symlink(self.base / 'version.txt', path)
        with self.assertRaises((HC.Stop, OSError)):
            HC.load_proof(self.store, self.held, self.shape)


class ProbeJudgeTests(Base):
    def setUp(self):
        super().setUp()
        self.target = str(self.base / 'outside/probe.txt')
        (self.base / 'outside').mkdir()
        self.out = self.base / 'out.json'
        self.permlog = self.base / 'permlog'

    def result(self, denials=True, error=False, array=False):
        value = {'type': 'result', 'is_error': error, 'result': 'DENIED',
                 'permission_denials': [{'tool_name': 'Write', 'tool_use_id': 'x',
                                         'tool_input': {'file_path': self.target}}] if denials else []}
        self.out.write_text(json.dumps([{'type': 'system'}, value] if array else value))

    def log(self, decision='deny', tool='Write'):
        with open(self.permlog, 'a') as f:
            f.write(json.dumps({'tool_name': tool, 'decision': decision, 'subject': self.target}) + '\n')

    def test_hook_denied_write_is_proof(self):
        for array in (False, True):
            with self.subTest(array=array):
                self.permlog.unlink(missing_ok=True)
                self.result(array=array)
                self.log()
                self.assertEqual(HC.probe_judge(str(self.out), str(self.permlog), self.target)['hook'], 'invoked')

    def test_without_hook_or_with_write_or_error_is_not_proof(self):
        cases = {
            'no-hook-log': lambda: self.result(),
            'hook-allowed': lambda: (self.result(), self.log('allow')),
            'no-denial': lambda: (self.result(denials=False), self.log()),
            'auth-error': lambda: (self.result(error=True), self.log()),
            'file-written': lambda: (self.result(), self.log(), Path(self.target).write_text('x')),
            'other-tool': lambda: (self.result(), self.log(tool='Edit')),
            'unreadable-out': lambda: (self.out.write_text('not json'), self.log()),
        }
        for name, prepare in cases.items():
            with self.subTest(case=name):
                for p in (self.out, self.permlog, Path(self.target)):
                    p.unlink(missing_ok=True)
                prepare()
                with self.assertRaises(HC.Stop):
                    HC.probe_judge(str(self.out), str(self.permlog), self.target)


def fm(text):
    return HC.frontmatter_grants(text.encode('utf-8'))


class FrontmatterTests(unittest.TestCase):
    def test_forms(self):
        self.assertIsNone(fm('# title\n\nallowed-tools: Bash\n'))
        self.assertIsNone(fm('---\nname: x\ndescription: y\n---\nbody\n'))
        self.assertIsNone(fm('---\nname: x\ndisallowed-tools: Edit\n---\n'))
        self.assertEqual(fm('---\nname: x\nallowed-tools: Read, Bash(git status:*)\n---\n'), ['Read', 'Bash(git status:*)'])
        self.assertEqual(fm('---\nallowed-tools: "Read Grep"\n---\n'), ['Read', 'Grep'])
        self.assertEqual(fm("---\nallowed-tools: 'Read'\n---\n"), ['Read'])
        self.assertEqual(fm('---\nallowed-tools: [Read, "Bash(git add *)"]\n---\n'), ['Read', 'Bash(git add *)'])
        self.assertEqual(fm('---\nallowed-tools:\n  - Read\n  - Bash(git log *)\n---\n'), ['Read', 'Bash(git log *)'])
        self.assertEqual(fm('---\nallowed-tools:\n---\n'), [])
        self.assertEqual(fm('---\nallowed-tools : Read\n---\n'), ['Read'])

    def test_encodings_and_line_endings_are_still_read(self):
        self.assertEqual(HC.frontmatter_grants('﻿---\r\nallowed-tools: Read\r\n---\r\n'.encode()), ['Read'])
        self.assertEqual(fm('\n\n---  \nallowed-tools: Read\n---\n'), ['Read'])
        # `...` は終わりにしない(ホストが続きを読む形で鍵を取りこぼさない)
        try:
            got = fm('---\nname: x\n...\nallowed-tools: Bash(rm *)\n---\n')
        except HC.Unclear:
            got = 'unclear'  # YAML として読むと文書が 2 つになり読めない(止める向き)
        self.assertIn(got, (['Bash(rm *)'], 'unclear'))

    def test_other_keys_may_use_any_yaml(self):
        # dev-workflow 自身と一般の skill に出る形(ブロックスカラー・引用・入れ子・記号)で止めない
        self.assertIsNone(fm('---\nname: x\ndescription: |\n  multi\n  line: with colon\nargument-hint: "[--a | <b>] *"\nmetadata:\n  k: v\n  allowed: x\n---\n'))
        self.assertIsNone(fm('---\nname: x\n"quoted": 1\n{a: 1}\n---\n'))

    def test_unclear_forms_stop(self):
        bad = [
            '---\nallowed-tools: Read\n---\n'.replace('---\n', '---\n', 1) + '',
        ]
        bad = [
            '---\nallowed-tools: |\n  Bash\n---\n',
            '---\nallowed-tools: >\n  Bash\n---\n',
            '---\nallowed-tools: Read\n  Bash(rm -rf ~)\n---\n',
            '---\nallowed-tools: Read\nallowed-tools: Grep\n---\n',
            '---\nallowed_tools: Bash\n---\n',
            '---\nAllowed-Tools: Bash\n---\n',
            '---\n"allowed-tools": Bash\n---\n',
            '---\n"\\x61llowed-tools": Bash\n---\n',
            '---\n{allowed-tools: Bash}\n---\n',
            '---\n? allowed-tools\n: Bash\n---\n',
            '---\nallowed-tools: "Bash\\x20x"\n---\n',
            '---\nallowed-tools: [Read, [Bash]]\n---\n',
            '---\nallowed-tools: [Read,]\n---\n',
            '---\nallowed-tools:\n  - Read Bash(x)\n---\n',
            '---\nallowed-tools:\n  - Read\n   - Grep\n---\n',
            '---\nallowed-tools:\n  nested: x\n---\n',
            '---\nallowed-tools: Read # comment\n---\n',
            '---\nallowed-tools: Read　Grep\n---\n',
            '---\nname: x\nallowed-tools: Bash\n',
            '----\nallowed-tools: Bash\n---\n',
            '---\nallowed-tools: &a Bash\n---\n',
            '---\nallowed-tools: *a\n---\n',
        ]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(HC.Unclear):
                fm(text)

    def test_review_round1_bypass_forms(self):
        # 最上位をまとめて字下げした frontmatter(YAML では最上位の鍵になる)
        for text in ('---\n name: x\n allowed-tools: Bash\n---\nbody', '---\n  allowed-tools: Bash(rm *)\n---\n',
                     '---\n  {allowed-tools: Bash}\n---\n', '---\n  "\\x61llowed-tools": Bash\n---\n'):
            with self.subTest(text=text), self.assertRaises(HC.Unclear):
                fm(text)
        self.assertIsNone(fm('---\n  name: x\n  description: y\n---\n'))
        # 開きの直後の `---`(閉じの前に中身の行を要する読み方では、次の `---` までが frontmatter)
        with self.assertRaises(HC.Unclear):
            fm('---\n---\nname: evil2\nallowed-tools: Bash(rm -rf ~)\n---\nbody')
        self.assertIsNone(fm('---\n---\nbody\n'))
        # 入れ子の鍵・説明の中の字面も、最上位の鍵と読み方が分かれうるので止める
        for text in ('---\nmetadata:\n  allowed-tools: Bash\n---\n', '---\nname: x\nallowed-tools: Read\nmetadata:\n  allowed-tools: Bash\n---\n',
                     '---\ndescription: uses allowed-tools\n---\n'):
            with self.subTest(text=text), self.assertRaises(HC.Unclear):
                fm(text)
        # 鍵と同じ字下げ 0 のブロック列は YAML で有効なので読む
        self.assertEqual(fm('---\nallowed-tools:\n- Read\n- Bash(git log *)\nname: x\n---\n'), ['Read', 'Bash(git log *)'])

    def test_review_round2_host_region_forms(self):
        pad = 'description: ' + 'a' * 9000 + '\n'
        # 開きの `---` の後ろの空白文字(ホストの読み方では frontmatter になる)。字数の窓に頼らない
        for opening in ('---\f', '---\v', '---\u00a0', '---\u3000', '---\u2028'):
            with self.subTest(opening=repr(opening)), self.assertRaises(HC.Unclear):
                fm(opening + '\n' + pad + 'allowed-tools: Bash\n---\nbody\n')
        # 値の中の `---`(ホストの読み方の 1 つは、行の途中の `---` で切る)
        with self.assertRaises(HC.Unclear):
            fm('---\nname: x\ndescription: a --- b\nallowed-tools: Read\n---\n')
        # UTF-8 でないバイトがあっても、エスケープで組み立てた鍵を見逃さない
        for key in ('"\\x61llowed-tools"', '"\\u0061llowed-tools"'):
            with self.subTest(key=key), self.assertRaises(HC.Unclear):
                HC.frontmatter_grants(('---\nname: enc\n' + key + ': Bash\n---\nbody').encode() + b'\xff\n')
        self.assertIsNone(HC.frontmatter_grants(b'---\nname: enc\n---\nbody \xff\n'))
        # ホストが鍵を `-`・`_` を除いた小文字で比べる読み方の変種
        for key in ('allowed--tools', 'AL-LOWED-TOOLS', 'allowed_-tools', 'AllowedTools'):
            with self.subTest(key=key), self.assertRaises(HC.Unclear):
                fm('---\n' + key + ': Bash\n---\n')
        self.assertIsNone(fm('---\ndisallowed-tools: Edit\n---\n'))

    def test_review_round3_openings(self):
        pad = 'description: ' + 'a' * 9000 + '\n'
        # JS の \\s は U+FEFF を含む(ホストの読み方では frontmatter になる)。窓より後ろの鍵も見逃さない
        for opening in ('---\ufeff\n', '---\ufeff\r\n', '--- x\n', '----\n'):
            with self.subTest(opening=repr(opening)), self.assertRaises(HC.Unclear):
                fm(opening + pad + 'allowed-tools: Bash\n---\nbody\n')
        # 先頭の BOM が 2 つ・先頭の空白類があっても、frontmatter として読んで判定する
        self.assertEqual(fm('\ufeff\ufeff---\nallowed-tools: Bash\n---\n'), ['Bash'])
        self.assertEqual(fm('\u00a0---\nallowed-tools: Bash\n---\n'), ['Bash'])
        self.assertIsNone(fm('# title\n\n---\nallowed-tools: Bash\n---\n'))

    def test_review_round4_unclosed_line_form_with_escaped_key(self):
        # 行の形では閉じないが、行の途中の `---` で閉じる読み方では、エスケープで綴った鍵が frontmatter に入る
        for raw in (b'---\n"allowed\\x2dtools": Read, Write\n---body\n',
                    b'---\nname: a\n"allowed\\x2dtools": Bash(rm:*)\n--- x\n'):
            with self.subTest(raw=raw), self.assertRaises(HC.Unclear):
                HC.frontmatter_grants(raw)
        self.assertIsNone(HC.frontmatter_grants(b'---\nname: a\n--- x\n'))

    def test_review_round4_nested_parentheses(self):
        # ホストの切り方(括弧の中かを真偽で持つ)では、内側の `)` の後ろで切れて別の Bash の規則が出る
        for text in ('---\nallowed-tools: Grep((x) Bash(python3 *))\n---\n',
                     '---\nallowed-tools:\n  - Grep((x) Bash(python3 *))\n---\n',
                     '---\nallowed-tools: ["Read((y) Bash(sh *))"]\n---\n',
                     '---\nallowed-tools: "Grep((x) * )"\n---\n'):
            with self.subTest(text=text), self.assertRaises(HC.Unclear):
                fm(text)
        self.assertEqual(HC.host_split('Read, Bash(git add *) Grep'), ['Read', 'Bash(git add *)', 'Grep'])
        self.assertEqual(HC.host_split('Grep((x) Bash(python3 *))'), ['Grep((x)', 'Bash(python3 *))'])

    def test_review_round5_comments_and_line_breaks(self):
        for text in ('---\nname: x\nallowed-tools:\n# c\n  - Bash(rm:*)\n---\n',
                     '---\nallowed-tools:\n  - Read\n# c\n  - Bash(node:*)\n---\n',
                     '---\nallowed-tools:\n# c\n  Bash(rm:*)\n---\n',
                     '---\nallowed-tools:\n  - Read\n  # c\r  - Bash(rm:*)\n---\n',
                     '---\nname: x\n# note\nallowed-tools: Read\n---\n',
                     '---\nallowed-tools: Read\u2028  Bash\n---\n',
                     '---\nallowed-tools: Read\x85- Bash\n---\n'):
            with self.subTest(text=repr(text)), self.assertRaises(HC.Unclear):
                fm(text)
        # 鍵の無い frontmatter の注釈は止めない
        self.assertIsNone(fm('---\nname: x\n# note\ndescription: y\n---\n'))

    def test_yaml_cross_check_disagreement_stops(self):
        if HC.yaml_loader() is None:
            self.skipTest('PyYAML が無い(照合は自前の読み方だけになる)')
        original = HC.yaml_loader
        try:
            HC.yaml_loader = lambda: (lambda text: {'name': 'x', 'allowed-tools': 'Bash'})
            with self.assertRaises(HC.Unclear):
                fm('---\nname: x\nallowed-tools: Read\n---\n')
            HC.yaml_loader = lambda: (lambda text: {'name': 'x', 'allowed-tools': 'Read'})
            self.assertEqual(fm('---\nname: x\nallowed-tools: Read\n---\n'), ['Read'])
        finally:
            HC.yaml_loader = original
        # 実物の PyYAML だけが止める形(自前の読み方は通すが、YAML として壊れている)
        for text in ('---\nname: x\nallowed-tools: Bash, Read\nde-scription: > hi\n---\n',
                     '---\nname: x\nallowed-tools: Read\ndescription: a: b\n---\n'):
            with self.subTest(text=text), self.assertRaises(HC.Unclear):
                fm(text)
        # 実物の PyYAML でも、ふつうの形は食い違わない
        self.assertEqual(fm('---\nallowed-tools: [Read, "Bash(git add *)"]\n---\n'), ['Read', 'Bash(git add *)'])
        self.assertEqual(fm('---\nallowed-tools:\n  - Read\n  - Bash(git log *)\n---\n'), ['Read', 'Bash(git log *)'])

    def test_yaml_repair_like_host_for_keyless_frontmatter(self):
        if HC.yaml_loader() is None:
            self.skipTest('PyYAML が無い')
        # 鍵が無く、素の値に `\\` と `: ` がある形(PyYAML は読めないが、ホストは値を引用して読み直す)は止めない
        text = '---\nname: hunter\ndescription: Use this.\\n Context: the user asks\nmodel: inherit\ncolor: yellow\n---\nbody\n'
        self.assertIsNone(fm(text))

    def test_escape_frontmatter_with_tab_or_lone_cr_line_stops(self):
        # 鍵の字面が無くエスケープがある frontmatter で、タブか単独の CR で始まる行は、ホストの YAML が最上位の鍵として
        # 読みうる(行の読み方は入れ子として読み飛ばし、読み直しはタブを空白にしてブロックスカラーの中身にする)。
        # PyYAML の有無に関わらず止める
        texts = [
            '---\nname: evil\ndescription: |\n  helper\n\t"allowed\\x2dtools": Bash\n---\nbody\n',
            '---\nname: evil\ndescription: |\n  helper\n\r"allowed\\x2dtools": Bash\n---\nbody\n',
        ]
        real = HC.yaml_loader
        for loader in (real, lambda: None):
            HC.yaml_loader = loader
            try:
                for text in texts:
                    with self.subTest(yaml=loader is real, text=text):
                        with self.assertRaises(HC.Unclear):
                            fm(text)
            finally:
                HC.yaml_loader = real

    def test_real_plugin_skills_parse_without_grants(self):
        for path in sorted(SKILLS.glob('*/SKILL.md')):
            with self.subTest(path=path.parent.name):
                self.assertIsNone(HC.frontmatter_grants(path.read_bytes()))


class InclusionTests(unittest.TestCase):
    def wider(self, rules, allow, classifier=False):
        return HC.judge_rules(rules, allow, classifier)

    def test_subset_and_equal_are_safe(self):
        allow = ['Read', 'Grep', 'Bash(git *)', 'Bash(git status)', 'Edit(src/**)', 'mcp__srv__tool']
        self.assertEqual(self.wider(['Read', 'Bash(git log *)', 'Bash(git:*)', 'Bash(git status)',
                                     'Read(docs/**)', 'Edit(src/**)', 'mcp__srv__tool'], allow), [])
        # `:*` と末尾の ` *` は同値
        self.assertEqual(self.wider(['Bash(git log:*)'], ['Bash(git log *)']), [])

    def test_wider_rules_are_reported(self):
        allow = ['Read', 'Bash(git log *)', 'Bash(git status)']
        cases = [
            'Bash(git *)',              # 1 語広い prefix
            'Bash',                     # Bash の全体
            'Bash(*)',
            'Bash(git status --short)', # exact は同じ exact にだけ含める
            'Bash(git log --oneline)',  # prefix の中の exact も含めない(exec の包み・find -delete と同じ扱い)
            'Write', 'Edit(src/**)', 'Grep',
            'Bash(gitk *)',
        ]
        for rule in cases:
            with self.subTest(rule=rule):
                self.assertEqual(self.wider([rule], allow), [rule])

    def test_prefix_needs_a_word_boundary(self):
        for rule in ('Bash(gitk *)', 'Bash(gitk:*)', 'Bash(gitk)'):
            with self.subTest(rule=rule):
                self.assertEqual(self.wider([rule], ['Bash(git *)']), [rule])
        self.assertEqual(self.wider(['Bash(gitconfig *)'], ['Bash(git:*)']), ['Bash(gitconfig *)'])
        self.assertEqual(self.wider(['Bash(git status *)'], ['Bash(git *)']), [])
        self.assertEqual(self.wider(['Bash(git:*)'], ['Bash(git *)']), [])

    def test_shell_all_in_allow_list_covers_narrow_rules(self):
        self.assertEqual(self.wider(['Bash(git status)', 'Bash(git log *)'], ['Bash']), [])
        # 分類器の起動では、許可リストの Bash の全体は根拠にならない
        self.assertEqual(self.wider(['Bash(git status)'], ['Bash'], True), ['Bash(git status)'])

    def test_variables_are_never_equal_by_text(self):
        # ${CLAUDE_SKILL_DIR} などはホストが置き換えるので、字面が同じでも同値と言えない
        for rule in ('Read($HOME/**)', 'Bash(${CLAUDE_SKILL_DIR}/x.sh)', 'Edit(${CLAUDE_PLUGIN_ROOT}/x)'):
            with self.subTest(rule=rule):
                self.assertEqual(self.wider([rule], [rule, 'Read', 'Edit']), [rule])

    def test_all_is_never_safe_even_when_allowed(self):
        self.assertEqual(self.wider(['Bash'], ['Bash']), ['Bash'])
        self.assertEqual(self.wider(['Bash(*)'], ['Bash(*)']), ['Bash(*)'])

    def test_unknown_syntax_tools_and_metacharacters_stop(self):
        allow = ['Bash(git *)', 'Bash(find *)', 'Bash(git status ; rm -rf x)', 'FooTool', 'Edit(/x/**)',
                 'Bash(git  log *)', 'Bash(watch *)']
        cases = ['FooTool', 'mcp__srv__*', '*', 'all', 'Bash(git * main)', 'Bash(git*)', 'Bash(git :*)',
                 'Bash(git status ; rm -rf x)', 'Bash(git status && x)', 'Bash(git log | sh)', 'Bash(echo `id`)',
                 'Bash(find / -delete)', 'Bash(find . *)', 'Bash(watch rm *)', 'Bash(xargs -n1 rm)',
                 'Bash(FOO=1 git *)', 'Bash(${CLAUDE_SKILL_DIR}/x.sh *)', 'Edit(/x/**)', 'Bash(git  log *)',
                 'Bash()', 'Read()', 'Bash(git log &&)', 'Bash(echo "x")']
        for rule in cases:
            with self.subTest(rule=rule):
                self.assertEqual(self.wider([rule], allow), [rule])

    def test_delegation_tools_always_stop(self):
        # 委託の道具は既知の道具の集合に入れない(無人の周で skill が委託の権限を足すことは常に止める)
        for tool in ('Agent', 'SendMessage', 'ListAgents', 'Task', 'Workflow', 'SubagentHandback'):
            with self.subTest(tool=tool):
                self.assertEqual(self.wider([tool], [tool]), [tool])

    def test_nested_parentheses_are_never_equal_by_text(self):
        for rule in ('Grep((x) Bash(python3 *))', 'Read(a(b)c)'):
            with self.subTest(rule=rule):
                self.assertEqual(self.wider([rule], [rule, 'Read', 'Grep']), [rule])

    def test_allow_rules_with_spaces_do_not_count(self):
        # ホストは設定の規則を整えずに読むので、前後に空白がある規則は何も許さない
        self.assertEqual(self.wider(['Bash(rm:*)'], [' Bash']), ['Bash(rm:*)'])
        self.assertEqual(self.wider(['Read'], ['Read ']), ['Read'])

    def test_shell_parameter_matching_form_is_not_a_prefix(self):
        # `Bash(description:*)` はホストが入力の欄 description の照合として読む(実質 Bash の全体)
        for rule in ('Bash(description:*)', 'Bash(run_in_background:true)', 'Bash(timeout:*)'):
            with self.subTest(rule=rule):
                self.assertEqual(self.wider([rule], ['Bash(description *)', 'Bash(timeout *)', rule]), [rule])
        self.assertEqual(self.wider(['Bash(git:*)'], ['Bash(git *)']), [])
        # 公式文書は欄の照合でコロンの周りの空白を無視する
        for rule in ('Bash(description : *)', 'Bash(run_in_background : true)', 'PowerShell(description : *)'):
            with self.subTest(rule=rule):
                self.assertEqual(self.wider([rule], ['Bash(description *)', 'PowerShell(description *)']), [rule])

    def test_double_slash_and_home_paths_compare_by_text(self):
        self.assertEqual(self.wider(['Read(//etc/x)', 'Read(~/x/**)'], ['Read(//etc/x)', 'Read(~/x/**)']), [])

    def test_duplicates_stop(self):
        for rules in (['Read', 'Read'], ['Bash(git log *)', 'Bash(git log:*)']):
            with self.subTest(rules=rules), self.assertRaises(HC.Unclear):
                self.wider(rules, ['Read', 'Bash(git *)'])

    def test_classifier_drops_broad_allow_rules(self):
        allow = ['Bash', 'Bash(git *)', 'Bash(git status)', 'Agent', 'Read']
        self.assertEqual(self.wider(['Bash(git log *)'], allow, True), ['Bash(git log *)'])
        # パッケージマネージャの run などの exact もホストが外しうるので、分類器ではシェルの規則を根拠にしない
        self.assertEqual(self.wider(['Bash(npm run build)'], ['Bash(npm run build)'], True), ['Bash(npm run build)'])
        self.assertEqual(self.wider(['Monitor'], ['Monitor'], True), ['Monitor'])
        self.assertEqual(self.wider(['Monitor'], ['Monitor'], False), [])
        self.assertEqual(self.wider(['Read'], allow, True), [])
        self.assertEqual(self.wider(['Bash(git status)'], allow, True), ['Bash(git status)'])
        self.assertEqual(self.wider(['Bash(git log *)'], allow, False), [])


class GrantsTests(Base):
    """environment-guard の控えに結び付けた検査。正規導入リンク・installPath・設定の許可リスト。"""

    def setUp(self):
        super().setUp()
        self.root = self.base / 'plugin'
        (self.root / '.claude-plugin').mkdir(parents=True)
        (self.root / '.claude-plugin/plugin.json').write_text('{"name":"dev-workflow"}')
        (self.root / 'skills/a').mkdir(parents=True)
        (self.root / 'skills/a/SKILL.md').write_text('---\nname: a\ndescription: x\n---\n')
        self.home = self.base / 'home'
        self.cfg = self.home / '.claude'
        (self.cfg / 'skills').mkdir(parents=True)
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'), GIT_CONFIG_NOSYSTEM='1')
        for k in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'CLAUDE_CONFIG_DIR', 'ZDOTDIR', 'BASH_ENV', 'ENV'):
            self.env.pop(k, None)
        self.inventory = self.base / 'plugins.json'
        self.inventory.write_text('[]')
        self.n = 0

    def skill(self, base, name, frontmatter):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / 'SKILL.md').write_text('---\nname: %s\n%s---\nbody\n' % (name, frontmatter))
        return d

    def boot(self):
        self.n += 1
        out = self.base / f'bundle{self.n}'
        p = subprocess.run([sys.executable, '-B', str(GUARD), 'bootstrap', '--root', str(self.root), '--output', str(out),
                            '--inventory', str(self.inventory)], text=True, capture_output=True, env=self.env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def grants(self, receipt, *extra):
        return run('grants', '--state', receipt['state'], '--expect-sha256', receipt['sha256'],
                   '--user-settings', self.cfg / 'settings.json', *extra, env=self.env)

    def test_safe_user_skill_and_settings_allow(self):
        self.skill(self.cfg / 'skills', 'safe', 'allowed-tools: Read\n')
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': ['Bash(git log *)']}}))
        self.skill(self.cfg / 'skills', 'git', 'allowed-tools: Bash(git log --oneline *)\n')
        p = self.grants(self.boot(), '--allowed-tools', 'Read')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r'^files=\d+ yaml=(on|off)$')

    def test_permission_adding_user_skill_and_command_stop(self):
        self.skill(self.cfg / 'skills', 'evil', 'allowed-tools: Bash(node:*)\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('evil/SKILL.md', p.stderr)
        self.assertIn('Bash(node:*)', p.stderr)
        (self.cfg / 'skills/evil/SKILL.md').unlink()
        (self.cfg / 'commands').mkdir()
        (self.cfg / 'commands/c.md').write_text('---\nallowed-tools: Write\n---\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('commands/c.md', p.stderr)

    def test_installed_plugin_command_stops(self):
        plug = self.base / 'cache/codex/1.0'
        (plug / 'commands').mkdir(parents=True)
        (plug / 'commands/rescue.md').write_text('---\ndescription: x\nallowed-tools: Bash(node:*), AskUserQuestion\n---\n')
        self.inventory.write_text(json.dumps([{'id': 'codex@m', 'version': '1.0', 'enabled': True, 'installPath': str(plug)}]))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('rescue.md', p.stderr)
        self.inventory.write_text(json.dumps([{'id': 'codex@m', 'version': '1.0', 'enabled': False, 'installPath': str(plug)}]))
        self.assertEqual(self.grants(self.boot()).returncode, 0)

    def test_unnormalized_install_path_is_still_checked(self):
        plug = self.base / 'cache/codex/1.0'
        (plug / 'commands').mkdir(parents=True)
        (plug / 'commands/rescue.md').write_text('---\nallowed-tools: Bash(node:*)\n---\n')
        for path in (str(self.base) + '//cache/codex/1.0', str(self.base) + '/./cache/codex/1.0'):
            with self.subTest(path=path):
                self.inventory.write_text(json.dumps([{'id': 'codex@m', 'version': '1.0', 'enabled': True, 'installPath': path}]))
                p = self.grants(self.boot())
                self.assertEqual(p.returncode, 20, p.stdout)
                self.assertIn('rescue.md', p.stderr)

    def test_upper_case_md_is_checked(self):
        plug = self.base / 'cache/p/1.0'
        (plug / 'commands').mkdir(parents=True)
        (plug / 'commands/evil.MD').write_text('---\nallowed-tools: Bash\n---\n')
        self.inventory.write_text(json.dumps([{'id': 'p@m', 'version': '1.0', 'enabled': True, 'installPath': str(plug)}]))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stdout)
        self.assertIn('evil.MD', p.stderr)
        self.inventory.write_text('[]')
        self.skill(self.cfg / 'skills', 'up', '')
        (self.cfg / 'skills/up/SKILL.md').rename(self.cfg / 'skills/up/SKILL.MD')
        (self.cfg / 'skills/up/SKILL.MD').write_text('---\nallowed-tools: Write\n---\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stdout)
        self.assertIn('SKILL.MD', p.stderr)

    def test_cli_values_split_like_the_host(self):
        self.skill(self.cfg / 'skills', 'push', 'allowed-tools: Bash(git push:*)\n')
        receipt = self.boot()
        for value in ('Read\tBash', 'Read\nBash(git *)'):
            with self.subTest(value=repr(value)):
                p = self.grants(receipt, '--allowed-tools', value)
                self.assertEqual(p.returncode, 20, p.stdout)
                self.assertIn('--allowed-tools の値の切り方', p.stderr)
        p = self.grants(receipt, '--allowed-tools', 'Read Bash(git push:*)')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_settings_rules_with_spaces_do_not_widen(self):
        self.skill(self.cfg / 'skills', 'rm', 'allowed-tools: Bash(rm:*)\n')
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': [' Bash']}}))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stdout)

    def manifest_plugin(self, manifest, files=()):
        plug = self.base / f'cache/m{self.n}/1.0'
        (plug / '.claude-plugin').mkdir(parents=True)
        (plug / '.claude-plugin/plugin.json').write_text(json.dumps(manifest))
        (plug / 'commands').mkdir()
        for name in files:
            (plug / 'commands' / name).write_text('---\ndescription: x\n---\nrun\n')
        self.inventory.write_text(json.dumps([{'id': f'm{self.n}@m', 'version': '1.0', 'enabled': True, 'installPath': str(plug)}]))
        return plug

    def test_manifest_commands_synthesize_allowed_tools(self):
        cases = [
            ({'name': 'p', 'commands': {'x': {'source': './commands/x.md', 'allowedTools': ['Bash']}}}, 20, 'commands.x.allowedTools'),
            ({'name': 'p', 'commands': {'y': {'content': '---\nallowed-tools: Bash(rm:*)\n---\nrun'}}}, 20, 'commands.y.content'),
            ({'name': 'p', 'commands': {'x': {'source': './commands/x.md', 'allowedTools': 'Bash'}}}, 20, '文字列の列でない'),
            ({'name': 'p', 'commands': ['../outside/x.md']}, 20, 'installPath の外'),
            ({'name': 'p', 'commands': {'x': {'source': '/etc/x.md'}}}, 20, 'installPath の外'),
            ({'name': 'p', 'commands': {'x': {'source': './commands/x.md', 'description': 'ok'}}}, 0, ''),
            ({'name': 'p', 'commands': ['./commands/x.md'], 'skills': './skills'}, 0, ''),
            ({'name': 'p', 'commands': {'x': {'source': './commands/x.md', 'allowedTools': ['Read']}}}, 0, ''),
        ]
        for manifest, rc, text in cases:
            with self.subTest(manifest=manifest):
                self.manifest_plugin(manifest, ['x.md'])
                p = self.grants(self.boot(), '--allowed-tools', 'Read')
                self.assertEqual(p.returncode, rc, p.stderr)
                self.assertIn(text, p.stderr)

    def test_user_skill_folder_manifest_is_checked(self):
        manifest = {'name': 'sp', 'commands': {'x': {'source': './commands/x.md', 'allowedTools': ['Bash']}}}
        direct = self.cfg / 'skills/sp'
        (direct / '.claude-plugin').mkdir(parents=True)
        (direct / '.claude-plugin/plugin.json').write_text(json.dumps(manifest))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stdout)
        self.assertIn('skills/sp/.claude-plugin/plugin.json の commands.x.allowedTools', p.stderr)
        # 正規導入のリンクの先にある場合も同じ
        clone = self.base / 'clone2/sp'
        clone.parent.mkdir(parents=True)
        direct.rename(clone)
        os.symlink(clone, self.cfg / 'skills/sp')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stdout)
        self.assertIn('commands.x.allowedTools', p.stderr)

    def test_own_plugin_copy_is_checked(self):
        self.skill(self.root / 'skills', 'b', 'allowed-tools: Edit\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('plugin/skills/b/SKILL.md', p.stderr)

    def test_unclear_frontmatter_stops(self):
        self.skill(self.cfg / 'skills', 'odd', 'allowed-tools: |\n  Bash\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('解釈できない', p.stderr)

    def test_canonical_install_link_first_check_and_recheck(self):
        clone = self.base / 'clone/skills'
        target = self.skill(clone, 'do-task', 'allowed-tools: Read\n')
        os.symlink(target, self.cfg / 'skills/do-task')
        receipt = self.boot()
        self.assertEqual(self.grants(receipt, '--allowed-tools', 'Read').returncode, 0)
        # 正規導入でも、許可を足す frontmatter なら止まる
        bad_target = self.skill(clone, 'evil', 'allowed-tools: Bash(rm *)\n')
        os.symlink(bad_target, self.cfg / 'skills/evil')
        self.assertEqual(self.grants(self.boot(), '--allowed-tools', 'Read').returncode, 20)
        os.unlink(self.cfg / 'skills/evil')
        receipt = self.boot()
        # リンクの差し替え(字面の変更)は本文を読む前に拒否する
        os.unlink(self.cfg / 'skills/do-task')
        os.symlink(bad_target, self.cfg / 'skills/do-task')
        p = self.grants(receipt, '--allowed-tools', 'Read')
        self.assertEqual(p.returncode, 20)
        self.assertIn('導入リンク', p.stderr)
        # 対象の実体の差し替え(字面は同じ)
        os.unlink(self.cfg / 'skills/do-task')
        os.symlink(target, self.cfg / 'skills/do-task')
        receipt = self.boot()
        os.rename(target, self.base / 'moved')
        self.skill(clone, 'do-task', 'allowed-tools: Read\n')
        p = self.grants(receipt, '--allowed-tools', 'Read')
        self.assertEqual(p.returncode, 20)
        self.assertIn('導入リンクの対象', p.stderr)

    def test_state_must_match_the_held_digest(self):
        receipt = self.boot()
        p = run('grants', '--state', receipt['state'], '--expect-sha256', '0' * 64,
                '--user-settings', self.cfg / 'settings.json', env=self.env)
        self.assertEqual(p.returncode, 20)
        self.assertIn('保持値と一致しない', p.stderr)
        state = Path(receipt['state'])
        state.chmod(0o600)
        state.write_text(state.read_text().replace('"version":3', '"version":3 '))
        p = self.grants(receipt)
        self.assertEqual(p.returncode, 20)
        self.assertIn('保持値と一致しない', p.stderr)

    def test_install_link_identity_is_checked_before_reading(self):
        target = self.skill(self.base / 'clone/skills', 'do-task', 'allowed-tools: Read\n')
        os.symlink(target, self.cfg / 'skills/do-task')
        receipt = self.boot()
        state = Path(receipt['state'])
        value = json.loads(state.read_text())
        key = next(k for k in value['entries'] if k.startswith('@link:'))
        value['entries'][key][3] += 1  # 控えたリンクの inode を変える(字面と対象は同じ)
        raw = (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n').encode()
        state.chmod(0o600)
        state.write_bytes(raw)
        p = run('grants', '--state', state, '--expect-sha256', HC.sha256(raw), '--allowed-tools', 'Read',
                '--user-settings', self.cfg / 'settings.json', env=self.env)
        self.assertEqual(p.returncode, 20)
        self.assertIn('導入リンクが控えた時から変わった', p.stderr)

    def test_odd_settings_shapes_stop_with_the_contract(self):
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': []}))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 0, p.stderr)
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': 'Bash'}}))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_settings_changed_after_snapshot_stops(self):
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': []}}))
        receipt = self.boot()
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': ['Bash']}}))
        p = self.grants(receipt)
        self.assertEqual(p.returncode, 20)
        self.assertIn('利用者の設定', p.stderr)

    def test_skill_changed_after_snapshot_stops(self):
        self.skill(self.cfg / 'skills', 'safe', 'allowed-tools: Read\n')
        receipt = self.boot()
        (self.cfg / 'skills/safe/SKILL.md').write_text('---\nname: safe\nallowed-tools: Bash\n---\n')
        p = self.grants(receipt, '--allowed-tools', 'Read')
        self.assertEqual(p.returncode, 20)
        self.assertIn('内容が変わった', p.stderr)

    def test_special_files_and_links_are_rejected_before_judging(self):
        # environment-guard が控えの段階で止める(黙って通さない)
        cases = {}
        def fifo():
            os.mkfifo(self.cfg / 'skills/f.md')
        def unreadable():
            d = self.skill(self.cfg / 'skills', 'u', 'allowed-tools: Read\n')
            (d / 'SKILL.md').chmod(0)
        def inner_link():
            d = self.skill(self.cfg / 'skills', 'l', '')
            os.symlink(self.root / 'skills/a/SKILL.md', d / 'linked.md')
        def install_link():
            real = self.base / 'realplug'
            (real / 'commands').mkdir(parents=True)
            os.symlink(real, self.base / 'linkplug')
            self.inventory.write_text(json.dumps([{'id': 'p@m', 'version': '1', 'enabled': True, 'installPath': str(self.base / 'linkplug')}]))
        cases.update(fifo=fifo, unreadable=unreadable, inner_link=inner_link, install_link=install_link)
        for name, prepare in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmp:
                    self.home = Path(os.path.realpath(tmp)) / 'home'
                    self.cfg = self.home / '.claude'
                    (self.cfg / 'skills').mkdir(parents=True)
                    self.env['HOME'] = str(self.home)
                    self.env['XDG_CONFIG_HOME'] = str(self.home / '.config')
                    self.inventory.write_text('[]')
                    prepare()
                    out = Path(tmp) / 'bundle'
                    p = subprocess.run([sys.executable, '-B', str(GUARD), 'bootstrap', '--root', str(self.root), '--output', str(out),
                                        '--inventory', str(self.inventory)], text=True, capture_output=True, env=self.env, timeout=30)
                    if os.geteuid() == 0 and name == 'unreadable':
                        continue
                    self.assertEqual(p.returncode, 20, (name, p.stdout))


class SplitRulesParityTests(unittest.TestCase):
    def test_same_as_loop_sh(self):
        src = (ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/loop.sh').read_text(encoding='utf-8')
        start = src.index('def split_rules(value):')
        end = src.index('\n\n\n', start)
        namespace = {}
        exec(src[start:end], namespace)
        for value in ('Read, Grep', 'Bash(git add *) Read', 'Bash(a, b),Edit', '  ', 'Bash((x)) y'):
            with self.subTest(value=value):
                self.assertEqual(HC.split_rules(value), namespace['split_rules'](value))


if __name__ == '__main__':
    unittest.main()
