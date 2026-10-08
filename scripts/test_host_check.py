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
        empty = HC.sha256(b'')
        init = {'init_schema': HC.INIT_SCHEMA_VERSION, 'public_names': []}
        return HC.proof_write(self.store, self.held, shape or self.shape, str(self.version), str(self.help), self.probe,
                              '9.9.9', empty, empty, [], 1, init)

    def test_missing_then_written_then_matches(self):
        with self.assertRaises(HC.Missing):
            HC.load_proof(self.store, self.held, self.shape)
        path = self.write()
        self.assertTrue(Path(path).is_file())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        HC.proof_match(self.store, self.held, self.shape, str(self.version), str(self.help))

    def test_cli_exit_codes(self):
        ident = json.dumps(self.held)
        component = HC.sha256(b'')
        p = run('proof-get', '--store', self.store, '--identity', ident, '--shape', self.shape,
                '--component-sha256', component, '--policy-sha256', component)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.write()
        p = run('proof-get', '--store', self.store, '--identity', ident, '--shape', self.shape,
                '--component-sha256', component, '--policy-sha256', component)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = run('proof-match', '--store', self.store, '--identity', ident, '--shape', self.shape,
                '--version-file', self.version, '--help-file', self.help,
                '--component-sha256', component, '--policy-sha256', component)
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

    def test_component_or_policy_change_needs_a_new_proof(self):
        component = '1' * 64
        policy = '2' * 64
        HC.proof_write(self.store, self.held, self.shape, str(self.version), str(self.help), self.probe,
                       '9.9.9', component, policy, [], 1,
                       {'init_schema': HC.INIT_SCHEMA_VERSION, 'public_names': []})
        HC.proof_match(self.store, self.held, self.shape, str(self.version), str(self.help), component, policy)
        with self.assertRaises(HC.Stop):
            HC.proof_match(self.store, self.held, self.shape, str(self.version), str(self.help), '3' * 64, policy)
        with self.assertRaises(HC.Stop):
            HC.proof_match(self.store, self.held, self.shape, str(self.version), str(self.help), component, '4' * 64)
        self.version.write_text('2.1.289 (Claude Code)\n')
        self.help.write_text(self.help.read_text() + '  --bare  now default for -p\n')
        with self.assertRaises(HC.Stop):
            HC.proof_match(self.store, self.held, self.shape, str(self.version), str(self.help))

    def test_forged_or_incomplete_proof_stops(self):
        path = Path(self.write())
        good = json.loads(path.read_text())
        for change in ({'probe': {'tool': 'Write', 'hook': 'skipped', 'decision': 'deny', 'file_absent': True}},
                       {'probe': {'tool': 'Edit', 'hook': 'invoked', 'decision': 'deny', 'file_absent': True}},
                       {'ino': good['ino'] + 1}, {'shape_sha256': '0' * 64}, {'version': 1},
                       {'resolver_version': HC.RESOLVER_VERSION + 1},
                       {'help_sha256': 'x'}):
            with self.subTest(change=change):
                path.write_text(json.dumps(dict(good, **change)))
                with self.assertRaises(HC.Stop):
                    HC.load_proof(self.store, self.held, self.shape)
        path.unlink()
        os.symlink(self.base / 'version.txt', path)
        with self.assertRaises((HC.Stop, OSError)):
            HC.load_proof(self.store, self.held, self.shape)

    def test_failed_reprove_preserves_existing_proof(self):
        path = Path(self.write())
        before = path.read_bytes()
        for field, value in (('hook', 'skipped'), ('decision', 'allow'), ('tool', 'Read'), ('file_absent', False)):
            with self.subTest(field=field):
                original = self.probe
                self.probe = dict(original, **{field: value})
                with self.assertRaises(HC.Stop):
                    self.write()
                self.probe = original
                self.assertEqual(path.read_bytes(), before)


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
        self.skill(self.cfg / 'skills', 'safe', 'allowed-tools: Read(docs/**)\n')
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': ['Bash(git log *)']}}))
        self.skill(self.cfg / 'skills', 'git', 'allowed-tools: Bash(git log --oneline *)\n')
        p = self.grants(self.boot(), '--allowed-tools', 'Read(docs/**)')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r'^files=\d+ yaml=(on|off)$')

    def test_permission_adding_user_skill_and_command_stop(self):
        self.skill(self.cfg / 'skills', 'evil', 'allowed-tools: Bash(node:*)\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('allowed-tools', p.stderr)
        self.assertIn('allowed-tools', p.stderr)
        (self.cfg / 'skills/evil/SKILL.md').unlink()
        (self.cfg / 'commands').mkdir()
        (self.cfg / 'commands/c.md').write_text('---\nallowed-tools: Write\n---\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('allowed-tools', p.stderr)

    def test_installed_plugin_command_stops(self):
        plug = self.base / 'cache/codex/1.0'
        (plug / 'commands').mkdir(parents=True)
        (plug / 'commands/rescue.md').write_text('---\ndescription: x\nallowed-tools: Bash(node:*), AskUserQuestion\n---\n')
        self.inventory.write_text(json.dumps([{'id': 'codex', 'version': '1.0', 'enabled': True, 'installPath': str(plug)}]))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('allowed-tools', p.stderr)
        self.inventory.write_text(json.dumps([{'id': 'codex', 'version': '1.0', 'enabled': False, 'installPath': str(plug)}]))
        self.assertEqual(self.grants(self.boot()).returncode, 0)

    def test_selected_marketplace_plugin_is_checked_from_held_source(self):
        market = self.base / 'market'
        plug = market / 'p'
        (plug / 'commands').mkdir(parents=True)
        (plug / '.claude-plugin').mkdir()
        (plug / '.claude-plugin/plugin.json').write_text('{"name":"p"}')
        (plug / 'commands/evil.md').write_text('---\nallowed-tools: Bash(node:*)\n---\n')
        config = self.cfg / 'plugins'
        config.mkdir()
        (config / 'known_marketplaces.json').write_text(json.dumps({'m': {'installLocation': str(market)}}))
        (config / 'installed_plugins.json').write_text(json.dumps({'plugins': {'p@m': [
            {'installPath': str(plug), 'scope': 'user'}]}}))
        (market / '.claude-plugin').mkdir()
        (market / '.claude-plugin/marketplace.json').write_text(json.dumps({'plugins': [
            {'name': 'p', 'source': './p'}]}))
        self.inventory.write_text(json.dumps([{'id': 'p@m', 'version': '1', 'enabled': True, 'installPath': str(plug)}]))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stderr)
        self.assertIn('allowed-tools', p.stderr)

    def market_fixture(self, command):
        market, installed = self.base / 'market', self.base / 'installed'
        for root in (market / 'p', installed):
            (root / '.claude-plugin').mkdir(parents=True, exist_ok=True)
            (root / '.claude-plugin/plugin.json').write_text('{"name":"p"}')
            (root / 'private').mkdir(exist_ok=True)
            (root / 'private/entry.md').write_text('---\nuser-invocable: true\n---\nbody\n')
        (market / '.claude-plugin').mkdir(exist_ok=True)
        (market / '.claude-plugin/marketplace.json').write_text(json.dumps({'plugins': [
            {'name': 'p', 'source': './p', 'strict': False, 'commands': {'mapped': command}},
            {'name': 'inactive', 'source': './missing', 'hooks': {'evil': ['SECRET_COMMAND']}}
        ]}))
        registry = self.cfg / 'plugins'
        registry.mkdir(exist_ok=True)
        (registry / 'known_marketplaces.json').write_text(json.dumps({'m': {'installLocation': str(market)}}))
        (registry / 'installed_plugins.json').write_text(json.dumps({'plugins': {'p@m': [
            {'installPath': str(installed), 'scope': 'user'}]}}))
        self.inventory.write_text(json.dumps([{'id': 'p@m', 'version': '1', 'scope': 'user',
                                              'enabled': True, 'installPath': str(installed)}]))
        return market, installed

    def policy(self, receipt):
        return run('component-policy', '--state', receipt['state'], '--expect-sha256', receipt['sha256'], env=self.env)

    def test_marketplace_map_only_grants_reach_cli(self):
        for command in ({'content': 'body', 'allowedTools': ['Bash']},
                        {'content': '---\nallowed-tools: Bash\n---\nbody'}):
            with self.subTest(command=command):
                self.market_fixture(command)
                result = self.grants(self.boot(), '--allowed-tools', 'Read(docs/**)')
                self.assertEqual(result.returncode, 20, result.stderr)

    def test_marketplace_map_only_names_reach_cli_and_inactive_is_ignored(self):
        for command in ({'content': 'body', 'allowedTools': ['Read(docs/**)']}, {'source': './private/entry.md'}):
            with self.subTest(command=command):
                self.market_fixture(command)
                receipt = self.boot()
                result = self.grants(receipt, '--allowed-tools', 'Read(docs/**)')
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.policy(receipt)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = json.loads(result.stdout)
                self.assertNotIn('p:mapped', result['public_names'])
                self.assertIn('p:mapped', result['loaded_commands'])
                self.assertIn('p:mapped', result['invocation_names'])
                self.assertNotIn('p:entry', result['public_names'])

    def test_manifest_empty_hook_and_mcp_references_are_held(self):
        (self.root / 'private').mkdir()
        (self.root / 'private/hooks.json').write_text('{"hooks":{}}')
        (self.root / 'private/mcp.json').write_text('{"mcpServers":{"data":{"command":"not-executed"}}}')
        manifest = self.root / '.claude-plugin/plugin.json'
        manifest.write_text(json.dumps({'name': 'dev-workflow', 'hooks': './private/hooks.json',
                                        'mcpServers': './private/mcp.json'}))
        receipt = self.boot()
        result = self.policy(receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.grants(receipt).returncode, 0)
        (self.root / 'private/hooks.json').write_text('{"hooks":{"PreToolUse":[{"command":"SECRET_COMMAND"}]}}')
        result = self.policy(self.boot())
        self.assertEqual(result.returncode, 20)
        self.assertNotIn('SECRET_COMMAND', result.stderr)

    def test_empty_declared_skill_directory_is_normal_and_absent_is_rejected(self):
        (self.root / 'empty-skills').mkdir()
        manifest = self.root / '.claude-plugin/plugin.json'
        manifest.write_text('{"name":"dev-workflow","skills":"./empty-skills"}')
        result = self.policy(self.boot())
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest.write_text('{"name":"dev-workflow","skills":"./missing"}')
        self.assertEqual(self.policy(self.boot()).returncode, 20)

    def test_rejected_grant_diagnostics_do_not_disclose_rule_values(self):
        self.skill(self.cfg / 'skills', 'bad', 'allowed-tools: Bash(SECRET_SENTINEL)\n')
        result = self.grants(self.boot(), '--allowed-tools', 'Read(docs/**)')
        self.assertEqual(result.returncode, 20)
        self.assertNotIn('SECRET_SENTINEL', result.stderr)
        self.assertIn('kind=personal-skill', result.stderr)

    def test_skill_folder_manifest_references_use_folder_root(self):
        folder = self.skill(self.cfg / 'skills', 'folder', '')
        (folder / '.claude-plugin').mkdir()
        (folder / 'private').mkdir()
        manifest = folder / '.claude-plugin/plugin.json'
        manifest.write_text(json.dumps({'name': 'folder', 'hooks': './private/hooks.json',
                                        'mcpServers': './private/mcp.json'}))
        (folder / 'private/hooks.json').write_text('{"hooks":{}}')
        (folder / 'private/mcp.json').write_text('{"mcpServers":{}}')
        receipt = self.boot()
        result = self.policy(receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.grants(receipt).returncode, 0)
        (folder / 'private/hooks.json').write_text('{"hooks":{"SessionStart":[{"command":"SECRET"}]}}')
        self.assertEqual(self.policy(self.boot()).returncode, 20)

    def test_skill_folder_plugin_has_personal_root_and_plugin_inner_namespace(self):
        folder = self.skill(self.cfg / 'skills', 'container', '')
        (folder / 'SKILL.md').write_text('---\nname: root-alias\n---\nroot body')
        (folder / '.claude-plugin').mkdir()
        (folder / '.claude-plugin/plugin.json').write_text('{"name":"folderplugin"}')
        (folder / 'skills/inner').mkdir(parents=True)
        (folder / 'skills/inner/SKILL.md').write_text('---\nname: inner-alias\n---\ninner body')
        result = self.policy(self.boot())
        self.assertEqual(result.returncode, 0, result.stderr)
        names = json.loads(result.stdout)
        self.assertEqual(names['public_names'], ['container', 'dev-workflow:a', 'folderplugin:inner'])
        self.assertIn('root-alias', names['invocation_names'])
        self.assertIn('folderplugin:inner-alias', names['invocation_names'])
        self.assertNotIn('inner', names['public_names'])
        self.assertNotIn('folderplugin:root-alias', names['public_names'])
        self.assertEqual(names['invocation_routes']['folderplugin:inner-alias'],
                         {'target': 'folderplugin:inner', 'user': True, 'model': True})
        self.assertEqual(names['loaded_commands']['folderplugin:inner']['kind'], 'plugin')
        self.assertEqual(names['loaded_commands']['container']['kind'], 'personal-skill')

    def test_skill_folder_plugin_agent_has_proven_plugin_privileges(self):
        folder = self.skill(self.cfg / 'skills', 'container', '')
        (folder / '.claude-plugin').mkdir()
        (folder / '.claude-plugin/plugin.json').write_text('{"name":"folderplugin","agents":"./private/helper.md"}')
        (folder / 'private').mkdir()
        agent = folder / 'private/helper.md'
        agent.write_text('---\nhooks: {PreToolUse: [ignored]}\nmcpServers: {x: {command: ignored}}\n'
                         'permissionMode: bypassPermissions\nskills: [container]\n---\nbody')
        receipt = self.boot()
        result = self.policy(receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.grants(receipt).returncode, 0)
        agent.write_text('---\nallowed-tools: Bash\n---\nbody')
        self.assertEqual(self.grants(self.boot(), '--allowed-tools', 'Read(docs/**)').returncode, 20)

    def test_manifestless_plugin_default_mcp_references_are_held(self):
        plug = self.base / 'cache/p/1.0'
        plug.mkdir(parents=True)
        mcp = plug / '.mcp.json'
        mcp.write_text('{"mcpServers":{"x":{"command":"not-executed"}}}')
        self.inventory.write_text(json.dumps([{'id': 'p', 'version': '1.0', 'enabled': True, 'installPath': str(plug)}]))
        result = self.policy(self.boot())
        self.assertEqual(result.returncode, 0, result.stderr)
        mcp.write_text('{"mcpServers":"./missing.json"}')
        self.assertEqual(self.policy(self.boot()).returncode, 20)

    def test_six_bypass_sources_reach_policy_or_grants_cli(self):
        paths = [
            (self.cfg / 'skills/one/SKILL.md', '---\nhooks:\n  PreToolUse:\n    - command: SECRET\n---\n'),
            (self.root / 'hooks/hooks.json', '{"hooks":{"PreToolUse":[{"command":"SECRET"}]}}'),
            (self.root / '.claude-plugin/plugin.json', '{"name":"dev-workflow","modules":["SECRET.js"]}'),
            (self.cfg / 'agents/reader.md', '---\nhooks: {PreToolUse: [SECRET]}\n---\n'),
            (self.cfg / 'skills/plug/.claude-plugin/plugin.json', '{"name":"plug","hooks":{"PreToolUse":["SECRET"]}}'),
        ]
        for path, content in paths:
            with self.subTest(source=str(path)):
                previous = path.read_bytes() if path.exists() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                result = self.policy(self.boot())
                self.assertEqual(result.returncode, 20, result.stdout)
                self.assertNotIn('SECRET', result.stderr)
                if previous is None:
                    path.unlink()
                else:
                    path.write_bytes(previous)
        self.market_fixture({'content': 'body', 'allowedTools': ['Bash(SECRET)']})
        result = self.grants(self.boot(), '--allowed-tools', 'Read(docs/**)')
        self.assertEqual(result.returncode, 20)
        self.assertNotIn('SECRET', result.stderr)

    def test_custom_plugin_agent_keeps_ignored_fields_but_checks_preload(self):
        (self.root / 'private').mkdir()
        (self.root / 'private/helper.md').write_text('---\npermissionMode: bypassPermissions\n'
            'hooks: {PreToolUse: []}\nskills: [dev-workflow:a]\n---\nbody')
        (self.root / '.claude-plugin/plugin.json').write_text(json.dumps({
            'name': 'dev-workflow', 'agents': ['./private/helper.md']}))
        receipt = self.boot()
        result = self.policy(receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.grants(receipt).returncode, 0)

    def test_plugin_agent_exception_does_not_cross_skill_or_command_roles(self):
        cases = (
            ('skills/a/SKILL.md', {}),
            ('commands/shared.md', {}),
            ('SKILL.md', {}),
            ('custom/shared/SKILL.md', {'skills': './custom'}),
            ('custom/shared.md', {'commands': './custom'}),
            ('private/shared.md', {'commands': {'shared': {'source': './private/shared.md'}}}),
        )
        manifest = self.root / '.claude-plugin/plugin.json'
        for relative, fields in cases:
            with self.subTest(relative=relative):
                path = self.root / relative
                before = path.read_bytes() if path.exists() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('---\nhooks: {PreToolUse: [SECRET_ROLE]}\n---\nbody')
                manifest.write_text(json.dumps(dict(name='dev-workflow', agents=['./' + relative], **fields)))
                receipt = self.boot()
                for result in (self.policy(receipt), self.grants(receipt)):
                    self.assertEqual(result.returncode, 20, result.stdout)
                    self.assertNotIn('SECRET_ROLE', result.stderr)
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)
        manifest.write_text('{"name":"dev-workflow","agents":"./private/agent.md"}')
        (self.root / 'private/agent.md').write_text('---\nhooks: {PreToolUse: [ignored]}\n'
            'mcpServers: {x: {command: ignored}}\npermissionMode: bypassPermissions\n---\nbody')
        receipt = self.boot()
        self.assertEqual(self.policy(receipt).returncode, 0)
        self.assertEqual(self.grants(receipt).returncode, 0)

    def test_skill_folder_and_marketplace_shared_agent_roles_are_strict(self):
        folder = self.skill(self.cfg / 'skills', 'container', 'hooks: {PreToolUse: [SECRET_ROLE]}\n')
        (folder / '.claude-plugin').mkdir()
        (folder / '.claude-plugin/plugin.json').write_text('{"name":"folderplugin","agents":"./SKILL.md"}')
        receipt = self.boot()
        for result in (self.policy(receipt), self.grants(receipt)):
            self.assertEqual(result.returncode, 20, result.stdout)
        (folder / 'SKILL.md').write_text('body')
        market, installed = self.market_fixture({'source': './private/entry.md'})
        for root in (market / 'p', installed):
            (root / 'private/entry.md').write_text('---\nhooks: {PreToolUse: [SECRET_ROLE]}\n---\nbody')
            (root / '.claude-plugin/plugin.json').write_text('{"name":"p","agents":"./private/entry.md"}')
        receipt = self.boot()
        for result in (self.policy(receipt), self.grants(receipt)):
            self.assertEqual(result.returncode, 20, result.stdout)
            self.assertNotIn('SECRET_ROLE', result.stderr)

    def test_indentless_agent_security_lists_and_preloads_reach_both_cli_checks(self):
        (self.cfg / 'agents').mkdir()
        agent = self.cfg / 'agents/helper.md'
        for key, tail in (('hooks', '- PreToolUse: [SECRET_LIST]'),
                          ('modules', '- SECRET_LIST.js'),
                          ('mcpServers', '- unsafe:\n    command: SECRET_LIST'),
                          ('permissionMode', '- bypassPermissions'),
                          ('skills', '- unknown')):
            with self.subTest(key=key):
                agent.write_text('---\n' + key + ':\n' + tail + '\n---\nbody')
                receipt = self.boot()
                for result in (self.policy(receipt), self.grants(receipt)):
                    self.assertEqual(result.returncode, 20, result.stdout)
                    self.assertNotIn('SECRET_LIST', result.stderr)
        for body in ('hooks: {}\nmodules: []\nmcpServers: []\nskills: []\n',
                     'skills:\n- dev-workflow:a\n', 'skills:\n- "dev-workflow:a"\n'):
            with self.subTest(normal=body):
                agent.write_text('---\n' + body + '---\nbody')
                receipt = self.boot()
                for result in (self.policy(receipt), self.grants(receipt)):
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_marketplace_inline_hooks_rejected_by_policy_cli(self):
        self.market_fixture({'content': '---\nhooks: {PreToolUse: []}\n---\nbody'})
        result = self.policy(self.boot())
        self.assertEqual(result.returncode, 20, result.stderr)

    def test_plugin_agent_ignored_privileges_and_preload_are_connected(self):
        agent = self.root / 'agents/helper.md'
        agent.parent.mkdir()
        agent.write_text('---\nhooks: {PreToolUse: []}\nmcpServers: {x: {command: node}}\n'
                         'permissionMode: bypassPermissions\nskills: [dev-workflow:a]\n---\nbody')
        receipt = self.boot()
        self.assertEqual(self.policy(receipt).returncode, 0)
        result = self.grants(receipt, '--allowed-tools', 'Read(docs/**)')
        self.assertEqual(result.returncode, 0, result.stderr)
        agent.write_text('---\nskills: [unknown]\n---\nbody')
        result = self.policy(self.boot())
        self.assertEqual(result.returncode, 20)

    def test_unnormalized_install_path_is_still_checked(self):
        plug = self.base / 'cache/codex/1.0'
        (plug / 'commands').mkdir(parents=True)
        (plug / 'commands/rescue.md').write_text('---\nallowed-tools: Bash(node:*)\n---\n')
        for path in (str(self.base) + '//cache/codex/1.0', str(self.base) + '/./cache/codex/1.0'):
            with self.subTest(path=path):
                self.inventory.write_text(json.dumps([{'id': 'codex', 'version': '1.0', 'enabled': True, 'installPath': path}]))
                p = self.grants(self.boot())
                self.assertEqual(p.returncode, 20, p.stdout)
                self.assertIn('allowed-tools', p.stderr)

    def test_upper_case_md_is_checked(self):
        plug = self.base / 'cache/p/1.0'
        (plug / 'commands').mkdir(parents=True)
        (plug / 'commands/evil.MD').write_text('---\nallowed-tools: Bash\n---\n')
        self.inventory.write_text(json.dumps([{'id': 'p', 'version': '1.0', 'enabled': True, 'installPath': str(plug)}]))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stdout)
        self.assertIn('allowed-tools', p.stderr)
        self.inventory.write_text('[]')
        self.skill(self.cfg / 'skills', 'up', '')
        (self.cfg / 'skills/up/SKILL.md').rename(self.cfg / 'skills/up/SKILL.MD')
        (self.cfg / 'skills/up/SKILL.MD').write_text('---\nallowed-tools: Write\n---\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stdout)
        self.assertIn('allowed-tools', p.stderr)

    def test_cli_values_split_like_the_host(self):
        self.skill(self.cfg / 'skills', 'push', 'allowed-tools: Bash(git push:*)\n')
        receipt = self.boot()
        for value in ('Read(docs/**)\tBash', 'Read(docs/**)\nBash(git *)'):
            with self.subTest(value=repr(value)):
                p = self.grants(receipt, '--allowed-tools', value)
                self.assertEqual(p.returncode, 20, p.stdout)
                self.assertIn('--allowed-tools の値の切り方', p.stderr)
        p = self.grants(receipt, '--allowed-tools', 'Read(docs/**) Bash(git push:*)')
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
        self.inventory.write_text(json.dumps([{'id': f'm{self.n}', 'version': '1.0', 'enabled': True, 'installPath': str(plug)}]))
        return plug

    def test_manifest_commands_synthesize_allowed_tools(self):
        cases = [
            ({'name': 'p', 'commands': {'x': {'source': './commands/x.md', 'allowedTools': ['Bash']}}}, 20, 'allowed-tools'),
            ({'name': 'p', 'commands': {'y': {'content': '---\nallowed-tools: Bash(rm:*)\n---\nrun'}}}, 20, 'allowed-tools'),
            ({'name': 'p', 'commands': {'x': {'source': './commands/x.md', 'allowedTools': 'Bash'}}}, 20, '文字列の列でない'),
            ({'name': 'p', 'commands': ['../outside/x.md']}, 20, 'installPath の外'),
            ({'name': 'p', 'commands': {'x': {'source': '/etc/x.md'}}}, 20, 'installPath の外'),
            ({'name': 'p', 'commands': {'x': {'source': './commands/x.md', 'description': 'ok'}}}, 0, ''),
            ({'name': 'p', 'commands': ['./commands/x.md'], 'skills': './skills'}, 0, ''),
            ({'name': 'p', 'commands': {'x': {'source': './commands/x.md', 'allowedTools': ['Read(docs/**)']}}}, 0, ''),
        ]
        for manifest, rc, text in cases:
            with self.subTest(manifest=manifest):
                self.manifest_plugin(manifest, ['x.md'])
                p = self.grants(self.boot(), '--allowed-tools', 'Read(docs/**)')
                self.assertEqual(p.returncode, rc, p.stderr)
                self.assertIn(text, p.stderr)

    def test_user_skill_folder_manifest_is_checked(self):
        manifest = {'name': 'sp', 'commands': {'x': {'source': './commands/x.md', 'allowedTools': ['Bash']}}}
        direct = self.cfg / 'skills/sp'
        (direct / '.claude-plugin').mkdir(parents=True)
        (direct / '.claude-plugin/plugin.json').write_text(json.dumps(manifest))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stdout)
        self.assertIn('allowed-tools', p.stderr)
        # 正規導入のリンクの先にある場合も同じ
        clone = self.base / 'clone2/sp'
        clone.parent.mkdir(parents=True)
        direct.rename(clone)
        os.symlink(clone, self.cfg / 'skills/sp')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stdout)
        self.assertIn('allowed-tools', p.stderr)

    def test_own_plugin_copy_is_checked(self):
        self.skill(self.root / 'skills', 'b', 'allowed-tools: Edit\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('allowed-tools', p.stderr)

    def test_unclear_frontmatter_stops(self):
        self.skill(self.cfg / 'skills', 'odd', 'allowed-tools: |\n  Bash\n')
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20)
        self.assertIn('解釈できない', p.stderr)

    def test_canonical_install_link_first_check_and_recheck(self):
        clone = self.base / 'clone/skills'
        target = self.skill(clone, 'do-task', 'allowed-tools: Read(docs/**)\n')
        os.symlink(target, self.cfg / 'skills/do-task')
        receipt = self.boot()
        self.assertEqual(self.grants(receipt, '--allowed-tools', 'Read(docs/**)').returncode, 0)
        # 正規導入でも、許可を足す frontmatter なら止まる
        bad_target = self.skill(clone, 'evil', 'allowed-tools: Bash(rm *)\n')
        os.symlink(bad_target, self.cfg / 'skills/evil')
        self.assertEqual(self.grants(self.boot(), '--allowed-tools', 'Read(docs/**)').returncode, 20)
        os.unlink(self.cfg / 'skills/evil')
        receipt = self.boot()
        # リンクの差し替え(字面の変更)は本文を読む前に拒否する
        os.unlink(self.cfg / 'skills/do-task')
        os.symlink(bad_target, self.cfg / 'skills/do-task')
        p = self.grants(receipt, '--allowed-tools', 'Read(docs/**)')
        self.assertEqual(p.returncode, 20)
        self.assertIn('component の実体', p.stderr)
        # 対象の実体の差し替え(字面は同じ)
        os.unlink(self.cfg / 'skills/do-task')
        os.symlink(target, self.cfg / 'skills/do-task')
        receipt = self.boot()
        os.rename(target, self.base / 'moved')
        self.skill(clone, 'do-task', 'allowed-tools: Read(docs/**)\n')
        p = self.grants(receipt, '--allowed-tools', 'Read(docs/**)')
        self.assertEqual(p.returncode, 20)
        self.assertIn('component の実体', p.stderr)

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
        target = self.skill(self.base / 'clone/skills', 'do-task', 'allowed-tools: Read(docs/**)\n')
        os.symlink(target, self.cfg / 'skills/do-task')
        receipt = self.boot()
        state = Path(receipt['state'])
        value = json.loads(state.read_text())
        key = next(k for k in value['entries'] if k.startswith('@link:'))
        value['entries'][key][3] += 1  # 控えたリンクの inode を変える(字面と対象は同じ)
        raw = (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n').encode()
        state.chmod(0o600)
        state.write_bytes(raw)
        p = run('grants', '--state', state, '--expect-sha256', HC.sha256(raw), '--allowed-tools', 'Read(docs/**)',
                '--user-settings', self.cfg / 'settings.json', env=self.env)
        self.assertEqual(p.returncode, 20)
        self.assertIn('component の実体', p.stderr)

    def test_odd_settings_shapes_stop_with_the_contract(self):
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': []}))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stderr)
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': 'Bash'}}))
        p = self.grants(self.boot())
        self.assertEqual(p.returncode, 20, p.stderr)

    def test_settings_changed_after_snapshot_stops(self):
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': []}}))
        receipt = self.boot()
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': ['Bash']}}))
        p = self.grants(receipt)
        self.assertEqual(p.returncode, 20)
        self.assertIn('利用者の設定', p.stderr)

    def test_host_allowlist_and_grants_reject_the_same_held_bad_setting(self):
        """許可リストと grants は別に開かず、同じ検証済み reader を通る。"""
        (self.cfg / 'settings.json').write_text(json.dumps({
            'env': {'CLAUDE_CODE_SAFE_MODE': 'must-not-leak'},
            'permissions': {'allow': ['Read(docs/**)']},
        }))
        receipt = self.boot()
        allowlist = subprocess.run([sys.executable, '-B', str(GUARD), 'host-allowlist',
                                    '--state', receipt['state'], '--expect-sha256', receipt['sha256']],
                                   text=True, capture_output=True, env=self.env, timeout=30)
        grants = self.grants(receipt)
        for name, value in (('allowlist', allowlist), ('grants', grants)):
            with self.subTest(consumer=name):
                self.assertEqual(value.returncode, 20, value.stderr)
                self.assertIn('host-settings', value.stderr)
                self.assertNotIn('must-not-leak', value.stderr)

    def test_verified_reader_executes_held_bytes_and_hides_file_errors(self):
        from unittest import mock
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': ['Read(docs/**)']}}))
        receipt = self.boot()
        guard = Path(receipt['guard'])
        held_guard = HC.read_regular(str(guard))
        marker = self.base / 'reader-race-marker'
        actual = HC.read_regular
        changed = [False]
        def read_then_replace(path, *args, **kwargs):
            raw = actual(path, *args, **kwargs)
            if str(path) == str(guard) and not changed[0]:
                changed[0] = True
                guard.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\ndef read_host_settings(*a): return {{'user_settings':'/forged','user':{{}}}}\n")
            return raw
        with mock.patch.object(HC, 'read_regular', side_effect=read_then_replace):
            value = HC.verified_host_settings(receipt['state'], receipt['sha256'])
        self.assertTrue(changed[0])
        self.assertEqual(value['user_settings'], str(self.cfg / 'settings.json'))
        self.assertFalse(marker.exists())

        # A valid cache for different source bytes must likewise be inert.
        guard.write_bytes(held_guard)
        import importlib._bootstrap_external as be
        st = os.stat(guard)
        evil = compile(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n", str(guard), 'exec')
        pyc = be._code_to_timestamp_pyc(evil, int(st.st_mtime), st.st_size)
        cache = guard.parent / '__pycache__'; cache.mkdir()
        (cache / f'environment-guard.{sys.implementation.cache_tag}.pyc').write_bytes(pyc)
        value = HC.verified_host_settings(receipt['state'], receipt['sha256'])
        self.assertEqual(value['user_settings'], str(self.cfg / 'settings.json'))
        self.assertFalse(marker.exists())

        # Unknown filesystem detail is not a grants diagnostic.
        target = self.cfg / 'SECRET_SENTINEL_target'; target.write_text('{}')
        (self.cfg / 'settings.json').unlink(); (self.cfg / 'settings.json').symlink_to(target.name)
        receipt = self.boot(); target.unlink()
        p = self.grants(receipt)
        self.assertEqual(p.returncode, 20)
        self.assertNotIn('SECRET_SENTINEL', p.stderr)

    def test_grants_requires_the_held_absolute_user_settings_path(self):
        (self.cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': ['Read(docs/**)']}}))
        receipt = self.boot()
        p = run('grants', '--state', receipt['state'], '--expect-sha256', receipt['sha256'],
                '--user-settings', 'settings.json', env=self.env)
        self.assertEqual(p.returncode, 20)
        self.assertIn('保持した絶対パス', p.stderr)
        elsewhere = self.base / 'elsewhere'; elsewhere.mkdir()
        p = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), 'grants', '--state', receipt['state'],
                            '--expect-sha256', receipt['sha256'], '--user-settings', str(self.cfg/'settings.json')],
                           cwd=elsewhere, text=True, capture_output=True, env=self.env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_component_hooks_and_modules_are_rejected_but_plain_skill_is_allowed(self):
        self.skill(self.cfg / 'skills', 'plain', 'description: plain\n')
        receipt = self.boot()
        self.assertEqual(self.grants(receipt).returncode, 0)
        self.skill(self.cfg / 'skills', 'hooked', 'hooks: {PreToolUse: []}\n')
        receipt = self.boot()
        p = self.grants(receipt)
        self.assertEqual(p.returncode, 20)
        self.assertIn('hooks', p.stderr)
        (self.cfg / 'skills/hooked/SKILL.md').unlink()
        # plugin manifest modules are executable entrypoints even when no command grant exists.
        (self.root / '.claude-plugin/plugin.json').write_text('{"name":"dev-workflow","modules":["x.js"]}')
        receipt = self.boot()
        p = self.grants(receipt)
        self.assertEqual(p.returncode, 20)
        self.assertIn('modules', p.stderr)

    def test_skill_changed_after_snapshot_stops(self):
        self.skill(self.cfg / 'skills', 'safe', 'allowed-tools: Read(docs/**)\n')
        receipt = self.boot()
        (self.cfg / 'skills/safe/SKILL.md').write_text('---\nname: safe\nallowed-tools: Bash\n---\n')
        p = self.grants(receipt, '--allowed-tools', 'Read(docs/**)')
        self.assertEqual(p.returncode, 20)
        self.assertIn('component の実体', p.stderr)

    def test_special_files_and_links_are_rejected_before_judging(self):
        # environment-guard が控えの段階で止める(黙って通さない)
        cases = {}
        def fifo():
            os.mkfifo(self.cfg / 'skills/f.md')
        def unreadable():
            d = self.skill(self.cfg / 'skills', 'u', 'allowed-tools: Read(docs/**)\n')
            (d / 'SKILL.md').chmod(0)
        def inner_link():
            d = self.skill(self.cfg / 'skills', 'l', '')
            os.symlink(self.root / 'skills/a/SKILL.md', d / 'linked.md')
        def install_link():
            real = self.base / 'realplug'
            (real / 'commands').mkdir(parents=True)
            os.symlink(real, self.base / 'linkplug')
            self.inventory.write_text(json.dumps([{'id': 'p', 'version': '1', 'enabled': True, 'installPath': str(self.base / 'linkplug')}]))
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
    def test_same_as_environment_guard(self):
        spec = importlib.util.spec_from_file_location('environment_guard_rules', GUARD)
        namespace = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(namespace)
        for value in ('Read, Grep', 'Bash(git add *) Read', 'Bash(a, b),Edit', '  ', 'Bash((x)) y'):
            with self.subTest(value=value):
                self.assertEqual(HC.split_rules(value), namespace.split_rules(value))


class ComponentPolicyTests(unittest.TestCase):
    def component(self, kind, relative, frontmatter=''):
        return {'key': 'component:%s:/safe/%s' % (kind, relative), 'kind': kind,
                'root': '/safe', 'relative': relative, 'path': '/safe/' + relative,
                'raw': ('---\n' + frontmatter + '---\nbody\n').encode()}

    def test_namespace_keeps_aliases_out_of_public_names_and_directory_wins(self):
        items = [
            self.component('personal-skill', 'alpha/SKILL.md', 'name: beta\n'),
            self.component('personal-skill', 'beta/SKILL.md', 'user-invocable: false\n'),
            self.component('enterprise-skill', 'same/SKILL.md', 'name: enterprise-alias\n'),
            self.component('personal-skill', 'same/SKILL.md', 'name: personal-alias\n'),
        ]
        ns = HC.component_namespace(items)
        self.assertEqual(ns['public_names'], ['alpha', 'same'])
        self.assertEqual(ns['invocation_names'], ['alpha', 'enterprise-alias', 'same'])
        self.assertIn('enterprise-alias', ns['model_invocation_names'])

    def test_same_plugin_source_and_install_bytes_are_one_definition(self):
        one = self.component('plugin', 'skills/plain/SKILL.md')
        two = dict(one, key='component:plugin:/other/skills/plain/SKILL.md', root='/other',
                   path='/other/skills/plain/SKILL.md', plugin='p@m')
        one['plugin'] = 'p@m'
        self.assertEqual(HC.component_namespace([one, two])['public_names'], ['p:plain'])

    def test_plugin_manifest_command_map_uses_prefix_and_nested_command_uses_colon(self):
        manifest = {'key': 'component:plugin:/safe/.claude-plugin/plugin.json', 'kind': 'plugin',
                    'root': '/safe', 'relative': '.claude-plugin/plugin.json',
                    'path': '/safe/.claude-plugin/plugin.json', 'plugin': 'work@m',
                    'raw': json.dumps({'name': 'work', 'commands': {'run': {'content': 'body'}}}).encode()}
        nested = self.component('personal-command', 'tools/check.md')
        ns = HC.component_namespace([manifest, nested])
        self.assertIn('work:run', ns['loaded_commands'])
        self.assertIn('tools:check', ns['loaded_commands'])
        self.assertEqual(ns['public_names'], [])

    def test_plugin_command_directory_preserves_relative_path_but_file_uses_basename(self):
        manifest = {'key': 'component:plugin:/safe/.claude-plugin/plugin.json', 'kind': 'plugin',
                    'root': '/safe', 'relative': '.claude-plugin/plugin.json',
                    'path': '/safe/.claude-plugin/plugin.json', 'plugin': 'work@m'}
        for source, relative, expected in ((None, 'commands/nested/a.md', 'work:nested:a'),
                                           ('./custom', 'custom/nested/a.md', 'work:nested:a'),
                                           ('./custom/nested/a.md', 'custom/nested/a.md', 'work:a')):
            with self.subTest(source=source):
                data = {'name': 'work'}
                if source is not None:
                    data['commands'] = source
                part = dict(self.component('plugin', relative, 'name: separate-alias\n'), plugin='work@m')
                ns = HC.component_namespace([dict(manifest, raw=json.dumps(data).encode()), part])
                self.assertEqual(list(ns['loaded_commands']), [expected])
                self.assertIn(expected, ns['invocation_names'])
                self.assertEqual(ns['public_names'], [])

    def test_plugin_manifest_source_must_be_held_and_prefix_is_not_doubled(self):
        manifest = {'key': 'component:plugin:/safe/.claude-plugin/plugin.json', 'kind': 'plugin',
                    'root': '/safe', 'relative': '.claude-plugin/plugin.json', 'path': '/safe/.claude-plugin/plugin.json',
                    'plugin': 'work@m', 'raw': json.dumps({'commands': {'work:run': {'source': './commands/run.md'}}}).encode()}
        source = {'key': 'component:plugin:/safe/commands/run.md', 'kind': 'plugin', 'root': '/safe',
                  'relative': 'commands/run.md', 'path': '/safe/commands/run.md', 'raw': b'body'}
        self.assertEqual(HC.plugin_command_names([manifest, source]), {'work:run'})
        with self.assertRaises(HC.Unclear):
            HC.plugin_command_names([manifest])

    def test_reserved_and_synced_names_do_not_enter_any_namespace(self):
        skipped = self.component('personal-skill', 'synced/SKILL.md', 'name: escaped\n')
        reserved = self.component('plugin', 'anthropic-skills/x/SKILL.md')
        ns = HC.component_namespace([skipped, reserved])
        self.assertEqual(ns['public_names'], [])
        self.assertEqual(ns['lookup_names'], [])

    def test_visibility_and_conditional_native_off_are_separate(self):
        hidden = self.component('personal-skill', 'checkup/SKILL.md',
                                'user-invocable: false\ndisable-model-invocation: true\n')
        ns = HC.component_namespace([hidden])
        self.assertEqual(ns['public_names'], [])
        self.assertNotIn('checkup', ns['model_invocation_names'])
        self.assertNotIn('checkup', HC.fixed_host_policy(ns)['skillOverrides'])
        # frontmatter-only checkup is a measured native-alias winner; doctor is not.
        checkup = self.component('personal-skill', 'other/SKILL.md', 'name: checkup\n')
        doctor = self.component('personal-skill', 'else/SKILL.md', 'name: doctor\n')
        off = HC.fixed_host_policy(HC.component_namespace([checkup, doctor]))['skillOverrides']
        self.assertNotIn('checkup', off)
        self.assertIn('doctor', off)

    def test_component_security_rejects_hidden_execution_and_duplicate_json(self):
        for frontmatter in ('hooks: {PreToolUse: []}\n', 'mcpServers: {x: y}\n'):
            with self.subTest(frontmatter=frontmatter), self.assertRaises(HC.Unclear):
                HC.component_security(('---\n' + frontmatter + '---\n').encode(), 'agent.md')
        self.assertIsNone(HC.component_security(b'---\nhooks: {}\n---\n', 'safe.md'))
        self.assertIsNone(HC.component_security(b'---\npermission-mode: plan\ndescription: hooks are documented\n---\n', 'safe.md'))
        with self.assertRaises(HC.Unclear):
            HC.component_security(b'{"name":"x","name":"y"}', 'plugin.json')
        with self.assertRaises(HC.Unclear):
            HC.component_security(b'{"name":"x","modules":["run.js"]}', 'plugin.json')

    def test_component_security_rejects_quoted_escaped_and_indented_hook_keys(self):
        cases = (
            b'---\n"hooks": {PreToolUse: []}\n---\n',
            b'---\n  hooks: {PreToolUse: []}\n---\n',
            b'---\nhooks: {}\nhooks: {PreToolUse: []}\n---\n',
            b'---\n"mcpServers": {danger: {command: node}}\n---\n',
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(HC.Unclear):
                HC.component_security(raw, 'agent.md')

    def test_component_security_rejects_default_hook_and_mod_files(self):
        with self.assertRaises(HC.Unclear):
            HC.component_security(b'{"PreToolUse":[{"command":"node x"}]}', 'hooks.json')
        with self.assertRaises(HC.Unclear):
            HC.component_security(b'export default {}', 'mods/unsafe.ts')

    def test_marketplace_entry_rejects_nested_execution_fields(self):
        entry = {'name': 'p', 'source': './p', 'hooks': {'PreToolUse': []}}
        with self.assertRaises(HC.Unclear):
            HC.marketplace_security({'path': '/safe/marketplace.json',
                                    'raw': json.dumps({'plugins': [entry]}).encode(),
                                    'entry': [{'plugin': 'p@m', 'entry': entry}]})

    def test_plugin_mcp_reference_must_be_held_and_root_relative(self):
        manifest = {'kind': 'plugin', 'root': '/safe', 'relative': '.claude-plugin/plugin.json',
                    'raw': json.dumps({'mcpServers': './mcp.json'}).encode()}
        config = {'kind': 'plugin', 'root': '/safe', 'relative': 'mcp.json', 'raw': b'{}'}
        self.assertIsNone(HC.strict_plugin_mcp([manifest, config]))
        for value in ('/tmp/mcp.json', '../mcp.json', ['./mcp.json', './mcp.json']):
            with self.subTest(value=value), self.assertRaises(HC.Unclear):
                bad = dict(manifest, raw=json.dumps({'mcpServers': value}).encode())
                HC.strict_plugin_mcp([bad, config])

    def test_agent_preload_must_name_an_inspected_skill(self):
        skill = self.component('personal-skill', 'plain/SKILL.md')
        agent = self.component('personal-agent', 'helper.md', 'skills: [plain]\n')
        self.assertIsNone(HC.agent_preloads([skill, agent], HC.component_namespace([skill])))
        bad = self.component('personal-agent', 'helper.md', 'skills: [unseen]\n')
        with self.assertRaises(HC.Unclear):
            HC.agent_preloads([skill, bad], HC.component_namespace([skill]))

    def test_reserved_prefix_and_synced_case_skip_all_aliases(self):
        for relative, fm in (('Synced/SKILL.md', 'name: harmless\n'),
                             ('normal/SKILL.md', 'name: anthropic-skills:hidden\n'),
                             ('anthropic-skills:bad/SKILL.md', 'name: harmless\n')):
            with self.subTest(relative=relative):
                ns = HC.component_namespace([self.component('personal-skill', relative, fm)])
                self.assertEqual(ns['public_names'], [])
                self.assertEqual(ns['lookup_names'], [])

    def test_skill_beats_legacy_and_directory_beats_visible_alias(self):
        items = [self.component('personal-command', 'same.md'),
                 self.component('personal-skill', 'same/SKILL.md', 'user-invocable: false\n'),
                 self.component('personal-skill', 'other/SKILL.md', 'name: same\n')]
        ns = HC.component_namespace(items)
        self.assertNotIn('same', ns['public_names'])
        self.assertNotIn('same', ns['invocation_names'])
        self.assertIn('same', ns['model_invocation_names'])

    def test_plugin_root_and_direct_custom_names_use_observed_fallback(self):
        manifest = self.component('plugin', '.claude-plugin/plugin.json')
        manifest['plugin'] = 'work@m'
        manifest['raw'] = b'{"name":"work","skills":"./"}'
        for name, expected in (('name: root-alias\n', 'work:root-alias'), ('', 'work:safe')):
            skill = self.component('plugin', 'SKILL.md', name)
            skill['plugin'] = 'work@m'
            ns = HC.component_namespace([manifest, skill])
            self.assertEqual(ns['public_names'], [expected])
        manifest['raw'] = b'{"name":"work","skills":"./custom"}'
        skill = self.component('plugin', 'custom/SKILL.md', 'name: root-alias\n')
        skill['plugin'] = 'work@m'
        self.assertEqual(HC.component_namespace([manifest, skill])['public_names'], ['work:root-alias'])

    def test_plugin_namespace_never_reenables_native_names(self):
        item = self.component('plugin', 'skills/doctor/SKILL.md', 'name: checkup\n')
        item['plugin'] = 'p@m'
        ns = HC.component_namespace([item])
        self.assertEqual(ns['public_names'], ['p:doctor'])
        self.assertIn('p:checkup', ns['invocation_names'])
        self.assertEqual(set(HC.fixed_host_policy(ns)['skillOverrides']), HC.NATIVE_RESERVED)

    def test_component_security_is_independent_of_yaml_installation(self):
        original = HC.yaml_loader
        try:
            for loader in (original, lambda: None):
                HC.yaml_loader = loader
                for raw in (b'---\nhooks: {}\nmodules: []\nmcpServers: {}\npermissionMode: plan\n---\n',
                            b'\xef\xbb\xbf---\nhooks: {}\n---\n'):
                    HC.component_security(raw, 'skill.md')
                for raw in (b'---\n"\\u0068ooks": {PreToolUse: []}\n---\n',
                            b'\xef\xbb\xbf---\nhooks: {PreToolUse: []}\n---\n',
                            b'---\nhooks: {}\nhooks: []\n---\n',
                            b'---\npermissionMode: auto\n---\n',
                            b'---\nhooks:\n  PreToolUse:\n    - command: SECRET_COMMAND\n---\n',
                            b'---\nfoo: &a {hooks: {PreToolUse: []}}\n<<: *a\n---\n'):
                    with self.subTest(raw=raw, loader=loader), self.assertRaises(HC.Unclear):
                        HC.component_security(raw, 'skill.md')
                for key in ('hooks', 'modules', 'mcpServers', 'permissionMode'):
                    for tail in ('- unsafe', '-\n  unsafe: value', '- unsafe:\n    command: value'):
                        raw = ('---\n' + key + ':\n' + tail + '\n---\nbody').encode()
                        with self.subTest(key=key, tail=tail, loader=loader), self.assertRaises(HC.Unclear):
                            HC.component_security(raw, 'agent.md')
                known = self.component('personal-skill', 'plain/SKILL.md')
                ns = HC.component_namespace([known])
                for value in ('plain', '"plain"'):
                    agent = self.component('personal-agent', 'a.md', 'skills:\n- ' + value + '\n')
                    self.assertIsNone(HC.agent_preloads([known, agent], ns))
                for tail in ('unknown', '', 'plain\n- unknown', '[plain]'):
                    agent = self.component('personal-agent', 'a.md', 'skills:\n- ' + tail + '\n')
                    with self.subTest(tail=tail, loader=loader), self.assertRaises(HC.Unclear):
                        HC.agent_preloads([known, agent], ns)
        finally:
            HC.yaml_loader = original

    def test_all_retained_off_settings_remove_routes_without_native_fallback(self):
        item = self.component('personal-skill', 'doctor/SKILL.md')
        ns = HC.component_namespace([item])
        for source in ('user', 'cache', 'managed'):
            settings = {source: [{'skillOverrides': {'doctor': 'off'}}] if source == 'managed'
                        else {'skillOverrides': {'doctor': 'off'}}}
            changed = HC.apply_user_skill_off(ns, settings)
            self.assertNotIn('doctor', changed['invocation_names'])
            self.assertNotIn('doctor', HC.fixed_host_policy(changed)['skillOverrides'])

    def test_init_sanitizer_discards_raw_fields_and_rejects_unknown(self):
        saved = HC.sanitize_init(b'{"type":"init","skills":["plain"]}', ['plain'])
        self.assertEqual(saved, {'init_schema': HC.INIT_SCHEMA_VERSION, 'public_names': ['plain']})
        for raw in (b'{"type":"init","skills":[{"name":"plain"}]}',
                    b'{"type":"init","skills":["plain","plain"]}',
                    b'{"type":"init","skills":["other"]}',
                    b'{"type":"init","account":"secret","skills":["plain"]}'):
            with self.subTest(raw=raw), self.assertRaises(HC.Stop):
                HC.sanitize_init(raw, ['plain'])

    def test_supervisor_sanitizer_keeps_only_probe_result_and_public_init(self):
        raw = json.dumps([
            {'type': 'system', 'subtype': 'init', 'account': {'id': 'private'}, 'skills': ['plain']},
            {'type': 'result', 'is_error': False, 'account': 'private', 'result': 'SECRET_RESULT', 'subtype': 'SECRET_SUBTYPE',
             'permission_denials': [{'tool_name': 'Write', 'tool_input': {'file_path': '/safe/probe'}}]},
        ]).encode()
        saved = HC.sanitize_supervisor(raw, ['plain'])
        self.assertEqual(saved, {'init': {'init_schema': HC.INIT_SCHEMA_VERSION, 'public_names': ['plain']},
                                 'result': {'type': 'result', 'is_error': False,
                                            'permission_denials': [{'tool_name': 'Write',
                                                                    'tool_input': {'file_path': '/safe/probe'}}]}})
        self.assertNotIn('private', json.dumps(saved))
        self.assertNotIn('SECRET_', json.dumps(saved))
        with self.assertRaises(HC.Stop):
            HC.sanitize_supervisor(b'[{"type":"init","skills":[]}]', [])

    def test_policy_digest_is_stable_for_same_bytes_and_binds_visibility(self):
        first = self.component('personal-skill', 'plain/SKILL.md')
        copied = dict(first, key='component:personal-skill:/other/plain/SKILL.md', root='/other',
                      path='/other/plain/SKILL.md')
        one = HC.component_namespace([first])
        two = HC.component_namespace([copied])
        digest = 'a' * 64
        self.assertEqual(HC.policy_digest(digest, one, HC.fixed_host_policy(one)),
                         HC.policy_digest(digest, two, HC.fixed_host_policy(two)))
        hidden = self.component('personal-skill', 'plain/SKILL.md', 'user-invocable: false\n')
        changed = HC.component_namespace([hidden])
        self.assertNotEqual(HC.policy_digest(digest, one, HC.fixed_host_policy(one)),
                            HC.policy_digest(digest, changed, HC.fixed_host_policy(changed)))

    def test_conflicting_fixed_settings_stop(self):
        policy = {'disableBundledSkills': True, 'syncClaudeAiSkills': False,
                  'syncClaudeAiPlugins': False, 'skillOverrides': {'doctor': 'off'}}
        HC.assert_fixed_policy({'user': {}, 'cache': None, 'managed': []}, policy)
        with self.assertRaises(HC.Stop):
            HC.assert_fixed_policy({'user': {'syncClaudeAiSkills': True}, 'cache': None, 'managed': []}, policy)

    def test_fixed_settings_require_boolean_types_in_every_source(self):
        policy = HC.fixed_host_policy(HC.component_namespace([]))
        for source in ('user', 'cache', 'managed'):
            for key in ('disableBundledSkills', 'syncClaudeAiSkills', 'syncClaudeAiPlugins'):
                for value in (policy[key], not policy[key], 0, 1, 0.0, 1.0, None, 'false', 'true'):
                    with self.subTest(source=source, key=key, value=repr(value), type=type(value).__name__):
                        settings = {source: [{key: value}] if source == 'managed' else {key: value}}
                        if type(value) is bool and value == policy[key]:
                            self.assertIsNone(HC.assert_fixed_policy(settings, policy))
                        else:
                            with self.assertRaises(HC.Stop):
                                HC.assert_fixed_policy(settings, policy)

    def test_marketplace_entry_must_be_selected_once_and_local(self):
        entry = {'name': 'p', 'source': './plugins/p'}
        selected = [{'plugin': 'p@m', 'entry': entry}]
        item = {'path': '/safe/marketplace.json', 'raw': json.dumps({'plugins': [entry]}).encode(), 'entry': selected}
        self.assertIsNone(HC.marketplace_security(item))
        for registry, chosen in (({'plugins': [entry, entry]}, selected),
                                 ({'plugins': [entry]}, [{'plugin': 'other@m', 'entry': {'name': 'other'}}]),
                                 ({'plugins': [{'name': 'p', 'source': '/tmp/p'}]},
                                  [{'plugin': 'p@m', 'entry': {'name': 'p', 'source': '/tmp/p'}}])):
            with self.subTest(registry=registry), self.assertRaises(HC.Unclear):
                HC.marketplace_security({'path': '/safe/marketplace.json', 'raw': json.dumps(registry).encode(),
                                         'entry': chosen})


class DirectAllowTests(Base):
    """H47: 直接許可と追加定義で同じ危険規則を拒否する。"""
    def setUp(self):
        Base.setUp(self)
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

    skill = GrantsTests.skill
    grants = GrantsTests.grants

    def boot(self, managed=None):
        self.n += 1
        args = [sys.executable, '-B', str(GUARD), 'bootstrap', '--root', str(self.root),
                '--output', str(self.base / ('held' + str(self.n))), '--inventory', str(self.inventory)]
        if managed is not None:
            args += ['--managed-dir', str(managed)]
        result = subprocess.run(args, text=True, capture_output=True, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_bare_cli_and_equal_component_are_rejected(self):
        for tool in [name + suffix for name in ('Read', 'Grep', 'Glob', 'Write', 'Edit', 'NotebookEdit', 'MultiEdit') for suffix in ('', '(*)')]:
            with self.subTest(tool=tool):
                self.skill(self.cfg / 'skills', 'unsafe', 'allowed-tools: ' + tool + '\n')
                result = self.grants(self.boot(), '--allowed-tools', tool)
                self.assertEqual(result.returncode, 20, result.stdout + result.stderr)

    def test_bare_held_sources_rejected_despite_deny(self):
        managed = self.base / 'managed'
        (managed / 'managed-settings.d').mkdir(parents=True)
        sources = [self.cfg / 'settings.json', self.cfg / 'remote-settings.json',
                   managed / 'managed-settings.json', managed / 'managed-settings.d/a.json']
        for path in sources:
            for tool in [name + suffix for name in ('Read', 'Grep', 'Glob', 'Write', 'Edit', 'NotebookEdit', 'MultiEdit') for suffix in ('', '(*)')]:
                with self.subTest(source=path.name, tool=tool):
                    path.write_text(json.dumps({'permissions': {'allow': [tool], 'deny': [tool], 'ask': [tool]}}))
                    result = self.grants(self.boot(managed))
                    self.assertEqual(result.returncode, 20, result.stdout + result.stderr)
                    path.unlink()

    def test_scoped_direct_and_availability_controls(self):
        (self.cfg / 'agents').mkdir()
        (self.cfg / 'agents/reader.md').write_text('---\nname: reader\ntools: Read, Grep, Glob\ndisallowedTools: Write\n---\n')
        for rule in ('Read(/docs/**)', 'Edit(//permitted/**)', 'Glob(docs/**)', 'Grep(docs/**)',
                     'Write(docs/**)', 'NotebookEdit(docs/**)', 'MultiEdit(docs/**)',
                     'Bash(git log *)', 'mcp__srv__tool', 'FutureTool(value)', 'Read(docs/report$2026.txt)'):
            with self.subTest(rule=rule):
                result = self.grants(self.boot(), '--allowed-tools', rule)
                self.assertEqual(result.returncode, 0, result.stderr)


    def test_target_syntax_and_diagnostics(self):
        bad = ['Read', 'Read(*)', 'Read (*)', 'Read( * )', 'Read()', 'Read( )', 'Read( docs/**)', 'Read(docs/** )', 'Read(docs/**',
               'Read(docs/**))', 'Read((docs/**))', 'Read (docs/**)', 'Read\u00a0(docs/**)',
               'Read(docs/\u2003x)', ' Read(docs/**)', 'Read(docs/**) ', 'Read(file_path:SECRET_H47)',
               'Edit(new_string:SECRET_H47)', 'Grep(pattern:SECRET_H47)', 'Glob(path:SECRET_H47)']
        for rule in bad:
            for kind in ('cli', 'user', 'cache', 'managed', 'managed-drop-in', 'personal-skill', 'plugin'):
                with self.subTest(rule=rule, kind=kind), self.assertRaises(HC.Unclear) as caught:
                    HC.validate_direct_allow_rules([rule], kind)
                self.assertNotIn('SECRET_H47', str(caught.exception))
        for rule in ('Read(/docs/**)', 'Read(~/docs/**)', 'Read(//docs/**)', 'Read(docs/my file)', 'Read(report:2026.txt)',
                     'Read(C:/docs/**)', 'Read(docs/report$2026.txt)',
                     'Glob(docs/**)', 'Grep(docs/**)', 'Write(docs/**)', 'FutureTool(value)', 'Bash(git *)'):
            self.assertEqual(HC.validate_direct_allow_rules([rule], 'cli'), [rule])

    def test_settings_types_are_strict_at_all_sources(self):
        managed = self.base / 'managed'; (managed / 'managed-settings.d').mkdir(parents=True)
        sources = [self.cfg / 'settings.json', self.cfg / 'remote-settings.json',
                   managed / 'managed-settings.json', managed / 'managed-settings.d/a.json']
        bad = [{'permissions': value} for value in (None, [], 'SECRET_H47', 1)]
        bad += [{'permissions': {'allow': value}} for value in (None, 'SECRET_H47', {}, [None], [1], [True])]
        for path in sources:
            for value in bad:
                with self.subTest(source=path.name, value=value):
                    path.write_text(json.dumps(value))
                    receipt = self.boot(managed)
                    result = run('component-policy', '--state', receipt['state'], '--expect-sha256', receipt['sha256'], env=self.env)
                    self.assertEqual(result.returncode, 20, result.stdout)
                    self.assertNotIn('SECRET_H47', result.stderr + result.stdout)
                    path.unlink()
        for value in ({}, {'permissions': {}}, {'permissions': {'allow': []}}, {'permissions': {'deny': ['Read'], 'ask': ['Write']}}):
            (self.cfg / 'settings.json').write_text(json.dumps(value))
            result = self.grants(self.boot(managed))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_component_grant_routes_reject_bare(self):
        # Validator runs before containment, including a parent's identical rule.
        for kind in ('personal-skill', 'personal-command', 'personal-agent', 'enterprise-skill',
                     'enterprise-command', 'enterprise-agent', 'plugin', 'marketplace-manifest'):
            for tool in [name + suffix for name in ('Read', 'Grep', 'Glob', 'Write', 'Edit', 'NotebookEdit', 'MultiEdit') for suffix in ('', '(*)')]:
                with self.subTest(kind=kind, tool=tool):
                    with self.assertRaises(HC.Unclear):
                        HC.validate_direct_allow_rules([tool], kind)
        # Actual expanded manifest routes retain allowedTools and inline frontmatter separately.
        for route in ('allowedTools', 'content'):
            for tool in [name + suffix for name in ('Read', 'Grep', 'Glob', 'Write', 'Edit', 'NotebookEdit', 'MultiEdit') for suffix in ('', '(*)')]:
                field = [tool] if route == 'allowedTools' else '---\nallowed-tools: ' + tool + '\n---\n'
                (self.root / '.claude-plugin/plugin.json').write_text(json.dumps({'name': 'dev-workflow',
                    'commands': {'inline': {route: field, 'content': field if route == 'content' else 'body'}}}))
                result = self.grants(self.boot())
                self.assertEqual(result.returncode, 20, result.stdout)
                self.assertIn('allowed-tools', result.stderr)

    def test_file_component_routes_and_scoped_controls(self):
        managed = self.base / 'managed'
        paths = [
            self.cfg / 'skills/unsafe/SKILL.md', self.cfg / 'commands/unsafe.md', self.cfg / 'agents/unsafe.md',
            managed / '.claude/skills/unsafe/SKILL.md', managed / '.claude/commands/unsafe.md',
            managed / '.claude/agents/unsafe.md', self.root / 'skills/unsafe/SKILL.md',
            self.root / 'commands/unsafe.md', self.root / 'agents/unsafe.md',
        ]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            for tool in HC.DIRECT_FILE_TOOLS:
                for suffix in ('', '(*)'):
                    with self.subTest(path=str(path.relative_to(self.base)), tool=tool, suffix=suffix):
                        path.write_text('---\nallowed-tools: ' + tool + suffix + '\n---\nbody\n')
                        result = self.grants(self.boot(managed))
                        self.assertEqual(result.returncode, 20, result.stdout)
                        self.assertIn('allowed-tools を解釈できない', result.stderr)
            path.write_text('---\nallowed-tools: Read(docs/**), Glob(src/**), Grep(src/**)\n---\nbody\n')
            result = self.grants(self.boot(managed), '--allowed-tools', 'Read(docs/**), Glob(src/**), Grep(src/**)')
            self.assertEqual(result.returncode, 0, result.stderr)
            path.unlink()

    def test_selected_marketplace_routes_reach_shared_validator(self):
        for route in ('allowedTools', 'content', 'source'):
            for tool in HC.DIRECT_FILE_TOOLS:
                for suffix in ('', '(*)'):
                    with self.subTest(route=route, tool=tool, suffix=suffix):
                        rule = tool + suffix
                        command = {'content': 'plain', 'allowedTools': [rule]} if route == 'allowedTools' else (
                            {'content': '---\nallowed-tools: ' + rule + '\n---\nbody'} if route == 'content' else
                            {'source': './private/entry.md'})
                        market, installed = GrantsTests.market_fixture(self, command)
                        if route == 'source':
                            for root in (market / 'p', installed):
                                (root / 'private/entry.md').write_text('---\nallowed-tools: ' + rule + '\n---\nbody')
                        result = self.grants(self.boot())
                        self.assertEqual(result.returncode, 20, result.stdout)
                        self.assertIn('allowed-tools を解釈できない', result.stderr)
        GrantsTests.market_fixture(self, {'content': 'body', 'allowedTools': ['Read(docs/**)']})
        result = self.grants(self.boot(), '--allowed-tools', 'Read(docs/**)')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_enterprise_command_namespace_security_and_digest(self):
        managed = self.base / 'managed'
        enterprise = managed / '.claude/commands/nested/one.md'
        personal = self.cfg / 'commands/nested/one.md'
        for path in (enterprise, personal):
            path.parent.mkdir(parents=True)
            path.write_text('---\nallowed-tools: Read(docs/**)\n---\n' + path.parent.parent.name)
        def policy():
            receipt = self.boot(managed)
            result = self.grants(receipt, '--allowed-tools', 'Read(docs/**)')
            self.assertEqual(result.returncode, 0, result.stderr)
            return HC.component_policy_output(HC.component_policy(receipt['state'], receipt['sha256'], ['Read(docs/**)']))
        first = policy()
        self.assertEqual(first['loaded_commands']['nested:one']['kind'], 'enterprise-command')
        self.assertIn('nested:one', first['lookup_names'])
        self.assertIn('nested:one', first['invocation_names'])
        self.assertNotIn('nested:one', first['public_names'])
        self.assertEqual(first['policy_sha256'], policy()['policy_sha256'])
        enterprise.write_text('---\nallowed-tools: Read(docs/**)\n---\nchanged')
        self.assertNotEqual(first['policy_sha256'], policy()['policy_sha256'])
        for field in ('hooks: {PreToolUse: [{command: SECRET_H47}]}', 'modules: [SECRET_H47]'):
            enterprise.write_text('---\n' + field + '\n---\nbody')
            receipt = self.boot(managed)
            result = run('component-policy', '--state', receipt['state'], '--expect-sha256', receipt['sha256'], env=self.env)
            self.assertEqual(result.returncode, 20)
            self.assertNotIn('SECRET_H47', result.stderr)
        # A losing personal definition still undergoes grants validation.
        enterprise.write_text('plain')
        personal.write_text('---\nallowed-tools: Read\n---\nbody')
        self.assertEqual(self.grants(self.boot(managed)).returncode, 20)

    def test_policy_binds_cli_sources_presence_and_rule_version(self):
        from unittest import mock
        managed = self.base / 'managed'; (managed / 'managed-settings.d').mkdir(parents=True)
        sources = [self.cfg / 'settings.json', self.cfg / 'remote-settings.json',
                   managed / 'managed-settings.json', managed / 'managed-settings.d/a.json',
                   managed / 'managed-settings.d/b.json']
        def digest(cli=()):
            receipt = self.boot(managed)
            return HC.component_policy(receipt['state'], receipt['sha256'], cli)['policy_sha256']
        baseline = digest()
        self.assertEqual(baseline, digest())
        first = digest(['Read(docs/**)'])
        self.assertNotEqual(first, baseline)
        self.assertNotEqual(first, digest(['Read(other/**)']))
        self.assertEqual(digest(['Read(docs/**),Edit(src/**)']), digest(['Read(docs/**)', 'Edit(src/**)']))
        with mock.patch.object(HC, 'ALLOW_RULE_VERSION', HC.ALLOW_RULE_VERSION + 1):
            self.assertNotEqual(baseline, digest())
        distinct = []
        for path in sources:
            path.write_text(json.dumps({'permissions': {'allow': ['Read(docs/**)']}}))
            added = digest(); self.assertNotEqual(baseline, added); distinct.append(added)
            path.write_text(json.dumps({'permissions': {'allow': ['Read(other/**)']}}))
            self.assertNotEqual(added, digest())
            path.unlink(); self.assertEqual(baseline, digest())
        self.assertEqual(len(distinct), len(set(distinct)))

    def test_managed_and_cache_bash_never_grant_user_skill(self):
        managed = self.base / 'managed'; managed.mkdir()
        for path in (managed / 'managed-settings.json', self.cfg / 'remote-settings.json'):
            path.write_text(json.dumps({'permissions': {'allow': ['Bash(node:*)']}}))
        self.skill(self.cfg / 'skills', 'script', 'allowed-tools: Bash(node:*)\n')
        receipt = self.boot(managed)
        self.assertEqual(self.grants(receipt).returncode, 20)
        result = self.grants(receipt, '--allowed-tools', 'Bash(node:*)')
        self.assertEqual(result.returncode, 0, result.stderr)
        guard = subprocess.run([sys.executable, '-B', str(GUARD), 'host-allowlist', '--state', receipt['state'],
                                '--expect-sha256', receipt['sha256']], capture_output=True, text=True, env=self.env)
        self.assertEqual(guard.returncode, 0, guard.stderr)
        self.assertEqual(json.loads(guard.stdout), [])


if __name__ == '__main__':
    unittest.main()
