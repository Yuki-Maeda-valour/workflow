"""implementer-choice.py の回帰(実装の担当の保存と聞き直し。契約の正本は external-runners.md §12-10)。

置き場の決まり:
- 一時の HOME・XDG_CONFIG_HOME・git のリポジトリは、/tmp と $TMPDIR の外に作る(製品が /tmp の下の置き場を使わないため)。
- 作る所は、実の HOME の下の ~/.cache/workflow-test-implementer-choice/ の中に tempfile.mkdtemp で作るフォルダ。
  addCleanup で消す。親の workflow-test-implementer-choice/ は、空なら消す。
- ~/.cache/dev-workflow と ~/.local/state/dev-workflow(pending-implementation.py scan が見る置き場)には作らない。
- 子プロセスの環境からは TMPDIR を外す。利用者の本当の設定(~/.config/dev-workflow/)は読みも書きもしない。
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import site
import stat
import sys
import tempfile
import time
import unittest

import support_git
from support_cli import describe, run, run_python
from support_load import load_script

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'plugins/dev-workflow/skills/do-task/scripts/implementer-choice.py'
STORE_NAME = 'implementer-choice.json'
TIMEOUT_ENV = 'DEV_WORKFLOW_IMPLEMENTER_CHOICE_TIMEOUT'
LAUNCH = 'codex exec --sandbox workspace-write <プロンプト>'
OTHER_LAUNCH = 'codex exec --sandbox workspace-write -m other <プロンプト>'
EMPTY = hashlib.sha256(b'').hexdigest()
ZERO = '0' * 64
SHORT_KEYS = {'decision', 'reason', 'choice', 'store', 'store_sha256', 'approval_count'}
LONG_KEYS = {'decision', 'reason', 'choice', 'store', 'store_sha256', 'approved_at', 'tuple_sha256'}
RECORD_KEYS = {'choice', 'store', 'store_sha256', 'approval_count'}
# 子プロセスの中で 1 つの関数を差し替えてから CLI を流す(解決の後の差し替えと、持ち主の違いを作るため)
PATCHED = '''
import os, runpy, sys
mode = sys.argv[1]
if mode == "uid":
    real = os.geteuid()
    os.geteuid = lambda: real + 1
elif mode == "realpath":
    os.path.realpath = lambda path, *args, **kwargs: os.path.abspath(path)
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def stamp(seconds):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seconds))


def remove_if_empty(path):
    try:
        path.rmdir()
    except OSError:
        pass


class ChoiceFixture(unittest.TestCase):
    def setUp(self):
        real_home = Path(os.environ.get('HOME') or os.path.expanduser('~')).resolve()
        base = real_home / '.cache/workflow-test-implementer-choice'
        excluded = {Path('/tmp').resolve(), Path(tempfile.gettempdir()).resolve()}
        if os.environ.get('TMPDIR'):
            excluded.add(Path(os.environ['TMPDIR']).resolve())
        for temp in excluded:
            if base == temp or temp in base.parents:
                self.skipTest('実の HOME が一時フォルダの下にあり、/tmp の外に置き場を作れない')
        # 同じテストを同時に流すと、空の親を消し合うことがある。そのときは作り直してやり直す
        for _ in range(5):
            base.mkdir(parents=True, mode=0o700, exist_ok=True)
            try:
                owned = tempfile.mkdtemp(prefix='case-', dir=str(base))
                break
            except FileNotFoundError:
                continue
        else:
            self.fail(f'{base} の中に専用のフォルダを作れない')
        self.addCleanup(remove_if_empty, base)
        self.w = Path(owned).resolve()
        self.addCleanup(self.remove_owned)
        for temp in excluded:
            self.assertNotIn(temp, self.w.parents)
        self.home = self.w / 'home'
        self.home.mkdir(mode=0o700)
        self.config = self.w / 'config'
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.pop('TMPDIR', None)
        env.pop(TIMEOUT_ENV, None)
        env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.config), GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, LC_ALL='C',
                   GIT_CEILING_DIRECTORIES=str(self.w), PYTHONUSERBASE=site.getuserbase())
        self.env = env

    def remove_owned(self):
        for path in [self.w, *self.w.rglob('*')]:
            try:
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(str(self.w), ignore_errors=True)

    # ── 呼び出し ──

    def env_with(self, **changes):
        env = dict(self.env)
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def ic(self, *args, env=None):
        return run_python(str(SCRIPT), *map(str, args), flags=('-I', '-B'), env=env or self.env, cwd=str(self.w),
                          timeout=180)

    def patched(self, mode, *args, env=None):
        return run([sys.executable, '-I', '-B', '-c', PATCHED, mode, str(SCRIPT), *map(str, args)],
                   env=env or self.env, cwd=str(self.w), timeout=180)

    def ok(self, *args, env=None, done=None):
        done = done or self.ic(*args, env=env)
        self.assertEqual(0, done.returncode, describe(done))
        lines = done.stdout.splitlines()
        self.assertEqual(1, len(lines), describe(done))
        return json.loads(lines[0])

    def refused(self, code, *args, env=None, done=None):
        done = done or self.ic(*args, env=env)
        self.assertEqual(code, done.returncode, describe(done))
        self.assertEqual('', done.stdout, describe(done))
        self.assertEqual(1, len(done.stderr.splitlines()), describe(done))
        self.assertTrue(done.stderr.startswith('ERROR [implementer-choice] '), describe(done))
        return done

    def short(self, runner='codex', env=None):
        args = ('status',) if runner is None else ('status', '--runner', runner)
        value = self.ok(*args, env=env)
        self.assertEqual(SHORT_KEYS, set(value))
        return value

    def long(self, project, launch=LAUNCH, secret=None, runner='codex', env=None):
        secret = self.secret(project) if secret is None else secret
        value = self.ok('status', '--runner', runner, '--project', project, '--launch', launch,
                        '--secret-set-sha256', secret, env=env)
        self.assertEqual(LONG_KEYS, set(value))
        return value

    def secret_set(self, cwd):
        value = self.ok('secret-set', '--cwd', cwd)
        self.assertEqual({'count', 'paths', 'globs', 'sha256'}, set(value))
        return value

    def secret(self, project):
        return self.secret_set(project)['sha256']

    def record(self, choice, expect, *approval, env=None):
        value = self.ok('record', '--choice', choice, '--expect-store-sha256', expect, *approval, env=env)
        self.assertEqual(RECORD_KEYS, set(value))
        return value

    def approve(self, project, launch=LAUNCH, secret=None, runner='codex', env=None):
        secret = self.secret(project) if secret is None else secret
        seen = self.long(project, launch, secret, runner, env=env)
        self.assertIsNotNone(seen['tuple_sha256'])
        return self.record(runner, seen['store_sha256'], '--project', project, '--launch', launch,
                           '--secret-set-sha256', secret, '--expect-tuple-sha256', seen['tuple_sha256'], env=env)

    # ── 置き場とリポジトリ ──

    def store_dir(self, config=None):
        return (config or self.config) / 'dev-workflow'

    def store(self, config=None):
        return self.store_dir(config) / STORE_NAME

    def load(self, config=None):
        return json.loads(self.store(config).read_bytes())

    def write_store(self, data, config=None, raw=None):
        directory = self.store_dir(config)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = self.store(config)
        raw = raw if raw is not None else json.dumps(data).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return sha256(raw)

    def git(self, cwd, *args):
        return support_git.git(*args, env=self.env, text=False, timeout=60,
                               options=('-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'init.defaultBranch=main',
                                        '-c', 'core.hooksPath=/dev/null', '-C', str(cwd)))

    def newrepo(self, path):
        path.mkdir(parents=True)
        self.git(path, 'init', '-q')
        self.git(path, 'commit', '-q', '--allow-empty', '-m', 'init')
        return path

    def snapshot(self, root=None):
        root = root or self.w
        items = {}
        for path in [root, *root.rglob('*')]:
            info = path.lstat()
            raw = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            items[str(path)] = (info.st_mode, info.st_size, raw)
        return items

    def key_of(self, repo):
        common = os.path.realpath(repo / '.git')
        return common, str(os.stat(common).st_ino)

    def approval(self, key_id, launch=LAUNCH, secret=EMPTY, config=None, when=0, runner='codex'):
        return {'repo_id': key_id, 'runner': runner, 'launch_sha256': sha256(launch.encode()),
                'secret_set_sha256': secret, 'runner_config_sha256': config or ZERO, 'approved_at': stamp(when)}


class ShortStatusTest(ChoiceFixture):
    def test_short_status_separates_internal_from_verify(self):
        self.assertEqual(('verify', 'unset'), tuple(self.short().values())[:2])
        out = self.record('internal', 'absent')
        value = self.short()
        self.assertEqual(('internal', 'saved-internal', 'internal'), (value['decision'], value['reason'], value['choice']))
        self.record('codex', out['store_sha256'])
        value = self.short()
        self.assertEqual(('verify', 'saved-runner', 'codex'), (value['decision'], value['reason'], value['choice']))

    def test_store_sha256_follows_the_raw_bytes(self):
        self.assertEqual('absent', self.short()['store_sha256'])
        out = self.record('codex', 'absent')
        value = self.short()
        self.assertEqual(out['store_sha256'], value['store_sha256'])
        self.assertEqual(sha256(self.store().read_bytes()), value['store_sha256'])
        with self.store().open('ab') as handle:
            handle.write(b' ')
        changed = self.short()
        self.assertEqual('verify', changed['decision'])
        self.assertNotEqual(value['store_sha256'], changed['store_sha256'])
        self.assertEqual(sha256(self.store().read_bytes()), changed['store_sha256'])

    def test_short_status_without_runner_does_not_judge_mismatch(self):
        self.record('other-runner', 'absent')
        self.assertEqual(('verify', 'saved-runner'), (self.short(None)['decision'], self.short(None)['reason']))
        value = self.short('codex')
        self.assertEqual(('verify', 'runner-mismatch', 'other-runner'), (value['decision'], value['reason'], value['choice']))

    def test_approval_count_is_the_total_of_all_keys(self):
        first = self.newrepo(self.w / 'a')
        second = self.newrepo(self.w / 'b')
        self.approve(first)
        self.approve(first, launch=OTHER_LAUNCH)
        out = self.approve(second)
        self.assertEqual(3, out['approval_count'])
        self.assertEqual(3, self.short()['approval_count'])
        self.assertEqual(3, sum(len(items) for items in self.load()['repos'].values()))

    def test_status_creates_nothing(self):
        repo = self.newrepo(self.w / 'r')
        before = self.snapshot()
        self.short()
        self.short(None)
        self.long(repo, secret=EMPTY)
        self.long(repo, secret='unknown')
        self.ok('status', env=self.env_with(XDG_CONFIG_HOME=None))
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.config.exists())
        self.assertFalse((self.home / '.config').exists())


class LongStatusTest(ChoiceFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.newrepo(self.w / 'r')

    def test_judgment_table_rows(self):
        repo = self.repo
        secret = self.secret(repo)
        rows = []

        def row(name, expect, prepare, **kwargs):
            rows.append((name, expect, prepare, kwargs))

        def broken(config):
            self.write_store(None, config, raw=b'{')

        def internal(config):
            self.record('internal', 'absent', env=self.env_with(XDG_CONFIG_HOME=config))

        def other(config):
            self.record('other-runner', 'absent', env=self.env_with(XDG_CONFIG_HOME=config))

        def chosen(config):
            # 別のリポジトリの承認と、同じ鍵の別のランナー・別の repo_id の承認は、new-project を変えない
            key, repo_id = self.key_of(repo)
            self.write_store({'schema': 1, 'choice': 'codex', 'repos': {
                key: [self.approval(repo_id, runner='other-runner'), self.approval(str(int(repo_id) + 1))],
                '/elsewhere/.git': [self.approval(repo_id)]}}, config)

        def approved(config):
            self.approve(repo, env=self.env_with(XDG_CONFIG_HOME=config))

        row('store-unusable', ('ask', 'store-unusable'), broken)
        row('unset', ('ask', 'unset'), lambda config: None)
        row('saved-internal', ('internal', 'saved-internal'), internal)
        row('runner-mismatch', ('ask', 'runner-mismatch'), other)
        row('new-project', ('ask', 'new-project'), chosen)
        row('launch-changed', ('ask', 'launch-changed'), approved, launch=OTHER_LAUNCH)
        row('runner-config-changed', ('ask', 'runner-config-changed'), approved, codex=True)
        row('secret-set-unknown', ('ask', 'secret-set-unknown'), approved, secret='unknown')
        row('secret-set-changed', ('ask', 'secret-set-changed'), approved, secret='f' * 64)
        row('approved', ('external', 'approved'), approved)
        for index, (name, expect, prepare, kwargs) in enumerate(rows):
            with self.subTest(row=name):
                config = self.w / f'config-{index}'
                prepare(config)
                if kwargs.get('codex'):
                    (repo / '.codex').mkdir()
                try:
                    value = self.long(repo, launch=kwargs.get('launch', LAUNCH), secret=kwargs.get('secret', secret),
                                      env=self.env_with(XDG_CONFIG_HOME=config))
                finally:
                    shutil.rmtree(str(repo / '.codex'), ignore_errors=True)
                self.assertEqual(expect, (value['decision'], value['reason']))
                if name == 'approved':
                    self.assertRegex(value['approved_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
                    self.assertEqual(self.load(config)['repos'][self.key_of(repo)[0]][0]['approved_at'],
                                     value['approved_at'])
                else:
                    self.assertIsNone(value['approved_at'])

    def test_runner_config_changes_ask_again(self):
        repo = self.repo
        codex = repo / '.codex'

        def expect(reason):
            value = self.long(repo, secret=EMPTY)
            self.assertEqual(reason, value['reason'])

        self.approve(repo, secret=EMPTY)
        expect('approved')
        codex.mkdir()
        (codex / 'config.toml').write_text('x = 1\n')
        expect('runner-config-changed')
        self.approve(repo, secret=EMPTY)
        expect('approved')
        (codex / 'config.toml').write_text('x = 2\n')
        expect('runner-config-changed')
        (codex / 'config.toml').write_text('x = 1\n')
        expect('approved')
        (codex / 'hooks.json').write_text('{}')
        expect('runner-config-changed')
        (codex / 'hooks.json').unlink()
        expect('approved')
        shutil.rmtree(str(codex))
        expect('approved')

    def long_refused(self, project, runner='codex'):
        return self.refused(20, 'status', '--runner', runner, '--project', project, '--launch', LAUNCH,
                            '--secret-set-sha256', EMPTY)

    def test_symlink_in_the_config_tree_stops(self):
        repo = self.repo
        codex = repo / '.codex'
        self.approve(repo, secret=EMPTY)
        # 設定のフォルダそのものが symlink: 承認できず、リンク先の中身を変えても approved にならない
        conf = repo / 'conf'
        conf.mkdir()
        (conf / 'config.toml').write_text('x = 1\n')
        codex.symlink_to('conf')
        done = self.long_refused(repo)
        self.assertIn('symlink', done.stderr)
        before = self.snapshot(self.config)
        self.refused(20, 'record', '--choice', 'codex', '--expect-store-sha256', self.short()['store_sha256'],
                     '--project', repo, '--launch', LAUNCH, '--secret-set-sha256', EMPTY,
                     '--expect-tuple-sha256', ZERO)
        self.assertEqual(before, self.snapshot(self.config))
        (conf / 'config.toml').write_text('x = 2\n')
        self.long_refused(repo)
        codex.unlink()
        self.assertEqual('approved', self.long(repo, secret=EMPTY)['reason'])
        # 設定のフォルダの中の symlink(同じ中身のファイルを指しても)
        codex.mkdir()
        (self.w / 'same.toml').write_text('x = 1\n')
        (codex / 'config.toml').symlink_to(self.w / 'same.toml')
        self.long_refused(repo)
        (codex / 'config.toml').unlink()
        (codex / 'deep').mkdir()
        (codex / 'deep' / 'link').symlink_to('missing')
        self.long_refused(repo)
        # --project までの途中のフォルダの下の .codex が symlink
        sub = repo / 'sub'
        sub.mkdir()
        shutil.rmtree(str(codex))
        (sub / '.codex').symlink_to('../conf')
        self.long_refused(sub)

    def test_runner_without_a_config_list_stops(self):
        repo = self.repo
        self.long_refused(repo, runner='other-runner')
        self.refused(20, 'record', '--choice', 'other-runner', '--expect-store-sha256', 'absent', '--project', repo,
                     '--launch', LAUNCH, '--secret-set-sha256', EMPTY, '--expect-tuple-sha256', ZERO)
        self.assertFalse(self.config.exists())
        # 選択だけなら保存でき、短い形の status も読める
        self.record('other-runner', 'absent')
        self.assertEqual('saved-runner', self.short('other-runner')['reason'])
        self.assertEqual(('ask', 'runner-mismatch'), tuple(self.long(repo, secret=EMPTY).values())[:2])

    def test_runner_config_is_read_from_top_to_project(self):
        repo = self.repo
        sub = repo / 'sub'
        sub.mkdir()
        (repo / 'other').mkdir()
        self.approve(sub, secret=EMPTY)
        self.assertEqual('approved', self.long(sub, secret=EMPTY)['reason'])
        # --project の道筋の外の .codex は見ない
        (repo / 'other' / '.codex').mkdir()
        self.assertEqual('approved', self.long(sub, secret=EMPTY)['reason'])
        (sub / '.codex').mkdir()
        (sub / '.codex' / 'config.toml').write_text('x = 1\n')
        self.assertEqual(('ask', 'runner-config-changed'), tuple(self.long(sub, secret=EMPTY).values())[:2])
        shutil.rmtree(str(sub / '.codex'))
        (repo / '.codex').mkdir()
        self.assertEqual('runner-config-changed', self.long(sub, secret=EMPTY)['reason'])

    def test_runner_config_over_the_item_limit_stops(self):
        codex = self.repo / '.codex'
        codex.mkdir()
        for index in range(1999):
            (codex / f'f{index}').write_bytes(b'')
        self.assertEqual('unset', self.long(self.repo, secret=EMPTY)['reason'])
        (codex / 'one-more').write_bytes(b'')
        self.refused(20, 'status', '--runner', 'codex', '--project', self.repo, '--launch', LAUNCH,
                     '--secret-set-sha256', EMPTY)

    def test_runner_config_walk_stops_at_the_limit(self):
        codex = self.repo / '.codex'
        codex.mkdir()
        for index in range(3000):
            (codex / f'f{index}').write_bytes(b'')
        module = load_script(SCRIPT, 'implementer_choice_walk')
        real_scandir = module.os.scandir
        seen = []

        class Counted:
            def __init__(self, inner):
                self.inner = inner

            def __next__(self):
                entry = next(self.inner)
                seen.append(entry.name)
                return entry

            def __iter__(self):
                return self

            def close(self):
                self.inner.close()

        def scandir(target):
            return Counted(real_scandir(target))

        module.os.scandir = scandir
        try:
            with self.assertRaises(module.Failure) as caught:
                module.runner_config_sha256('codex', str(self.repo), str(self.repo))
        finally:
            module.os.scandir = real_scandir
        self.assertEqual(20, caught.exception.code)
        # 1 つ目の項目は .codex 自身。2,000 個を超えた所で止まり、残りの名前を読まない
        self.assertEqual(2000, len(seen))

    def test_runner_config_over_the_byte_limit_stops(self):
        codex = self.repo / '.codex'
        codex.mkdir()
        (codex / 'big').write_bytes(b'x' * (16 * 1024 * 1024 + 1))
        self.refused(20, 'status', '--runner', 'codex', '--project', self.repo, '--launch', LAUNCH,
                     '--secret-set-sha256', EMPTY)

    def test_tuple_sha256_is_the_documented_join(self):
        repo = self.repo
        sub = repo / 'sub'
        (sub / '.codex').mkdir(parents=True)
        (sub / '.codex' / 'config.toml').write_bytes(b'x = 1\n')
        (sub / '.codex' / 'empty').mkdir()
        key, repo_id = self.key_of(repo)
        items = sorted([
            (b'.codex', b'absent', b''),
            (b'sub/.codex', b'dir', b''),
            (b'sub/.codex/config.toml', b'file', sha256(b'x = 1\n').encode()),
            (b'sub/.codex/empty', b'dir', b''),
        ])
        config = sha256(b''.join(path + b'\0' + kind + b'\0' + value + b'\0' for path, kind, value in items))
        secret = 'a' * 64
        fields = [key.encode(), repo_id.encode(), b'codex', sha256(LAUNCH.encode('utf-8')).encode(),
                  secret.encode(), config.encode()]
        expected = sha256(b''.join(field + b'\0' for field in fields))
        self.assertEqual(expected, self.long(sub, secret=secret)['tuple_sha256'])
        self.assertIsNone(self.long(sub, secret='unknown')['tuple_sha256'])

    def test_worktree_shares_the_key_and_a_replaced_repo_does_not(self):
        self.approve(self.repo, secret=EMPTY)
        worktree = self.w / 'wt'
        self.git(self.repo, 'worktree', 'add', '-q', str(worktree))
        value = self.long(worktree, secret=self.secret(worktree))
        self.assertEqual(('external', 'approved'), (value['decision'], value['reason']))
        # 別のパスに先に作り、古い方を消してから同じパスへ移す
        spare = self.newrepo(self.w / 'spare')
        old_ino = self.key_of(self.repo)[1]
        self.git(self.repo, 'worktree', 'remove', str(worktree))
        shutil.rmtree(str(self.repo))
        spare.rename(self.repo)
        self.assertNotEqual(old_ino, self.key_of(self.repo)[1])
        self.assertEqual(('ask', 'new-project'), tuple(self.long(self.repo, secret=EMPTY).values())[:2])

    def test_project_that_is_not_git_stops(self):
        plain = self.w / 'plain'
        plain.mkdir()
        before = self.snapshot()
        self.refused(20, 'status', '--runner', 'codex', '--project', plain, '--launch', LAUNCH,
                     '--secret-set-sha256', EMPTY)
        self.refused(20, 'record', '--choice', 'codex', '--expect-store-sha256', 'absent', '--project', plain,
                     '--launch', LAUNCH, '--secret-set-sha256', EMPTY, '--expect-tuple-sha256', ZERO)
        self.refused(20, 'status', '--runner', 'codex', '--project', self.w / 'missing', '--launch', LAUNCH,
                     '--secret-set-sha256', EMPTY)
        self.assertEqual(before, self.snapshot())


class RecordTest(ChoiceFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.newrepo(self.w / 'r')

    def test_record_creates_the_store_with_private_modes(self):
        self.assertFalse(self.config.exists())
        out = self.approve(self.repo, secret=EMPTY)
        self.assertEqual(stat.S_IMODE(self.store().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store_dir().stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o700)
        data = self.load()
        self.assertEqual({'schema', 'choice', 'repos'}, set(data))
        self.assertEqual((1, 'codex'), (data['schema'], data['choice']))
        self.assertEqual(out['store_sha256'], sha256(self.store().read_bytes()))
        self.assertEqual(str(self.store()), out['store'])
        self.assertEqual([], [p.name for p in self.store_dir().iterdir() if p.name != STORE_NAME])

    def test_choice_only_keeps_the_approvals(self):
        self.approve(self.repo, secret=EMPTY)
        repos = self.load()['repos']
        out = self.record('internal', self.short()['store_sha256'])
        self.assertEqual(('internal', 1), (out['choice'], out['approval_count']))
        self.assertEqual(repos, self.load()['repos'])

    def test_reapproval_updates_only_approved_at(self):
        self.approve(self.repo, secret=EMPTY)
        first = self.load()
        time.sleep(1.1)
        self.approve(self.repo, secret=EMPTY)
        second = self.load()
        key = self.key_of(self.repo)[0]
        self.assertEqual(1, len(second['repos'][key]))
        old, new = first['repos'][key][0], second['repos'][key][0]
        self.assertLess(old['approved_at'], new['approved_at'])
        self.assertEqual(dict(old, approved_at=None), dict(new, approved_at=None))

    def test_ninth_approval_drops_the_oldest(self):
        key, repo_id = self.key_of(self.repo)
        now = time.time()
        items = [self.approval(repo_id, launch=f'launch {index}', when=now - 1000 + order)
                 for index, order in enumerate((5, 3, 8, 1, 7, 2, 6, 4))]
        sha = self.write_store({'schema': 1, 'choice': 'codex', 'repos': {key: items}})
        out = self.record('codex', sha, '--project', self.repo, '--launch', LAUNCH, '--secret-set-sha256', EMPTY,
                          '--expect-tuple-sha256', self.long(self.repo, secret=EMPTY)['tuple_sha256'])
        self.assertEqual(8, out['approval_count'])
        kept = self.load()['repos'][key]
        self.assertEqual(8, len(kept))
        launches = [item['launch_sha256'] for item in kept]
        self.assertNotIn(sha256(b'launch 3'), launches)
        self.assertIn(sha256(LAUNCH.encode()), launches)
        self.assertEqual('approved', self.long(self.repo, secret=EMPTY)['reason'])

    def test_sixty_fifth_key_drops_the_oldest_key(self):
        now = time.time()
        repos = {}
        for index in range(64):
            when = now - 10000 + index
            if index == 17:
                when = now - 20000
            repos[f'/nowhere/repo-{index}/.git'] = [self.approval('1', when=when),
                                                    self.approval('1', launch='x', when=now - 30000)]
        sha = self.write_store({'schema': 1, 'choice': 'codex', 'repos': repos})
        self.record('codex', sha, '--project', self.repo, '--launch', LAUNCH, '--secret-set-sha256', EMPTY,
                    '--expect-tuple-sha256', self.long(self.repo, secret=EMPTY)['tuple_sha256'])
        kept = self.load()['repos']
        self.assertEqual(64, len(kept))
        self.assertNotIn('/nowhere/repo-17/.git', kept)
        self.assertIn(self.key_of(self.repo)[0], kept)

    def test_store_larger_than_one_mebibyte_after_writing_is_not_written(self):
        # 鍵の UTF-8 は、書くときに \\uXXXX に広がる(1 文字 3 バイトが 6 バイトになる)
        repos = {'/' + chr(0x3042 + index) * 30000: [self.approval('1')] for index in range(10)}
        raw = json.dumps({'schema': 1, 'choice': 'codex', 'repos': repos}, ensure_ascii=False).encode()
        self.assertLess(len(raw), 1024 * 1024)
        sha = self.write_store(None, raw=raw)
        self.assertEqual(sha, self.short()['store_sha256'])
        before = self.snapshot(self.config)
        self.refused(20, 'record', '--choice', 'internal', '--expect-store-sha256', sha)
        self.assertEqual(before, self.snapshot(self.config))

    def test_tuple_mismatch_stops_without_creating(self):
        before = self.snapshot()
        self.refused(21, 'record', '--choice', 'codex', '--expect-store-sha256', 'absent', '--project', self.repo,
                     '--launch', LAUNCH, '--secret-set-sha256', EMPTY, '--expect-tuple-sha256', ZERO)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.config.exists())
        sha = self.record('internal', 'absent')['store_sha256']
        before = self.snapshot()
        self.refused(21, 'record', '--choice', 'codex', '--expect-store-sha256', sha, '--project', self.repo,
                     '--launch', LAUNCH, '--secret-set-sha256', EMPTY, '--expect-tuple-sha256', ZERO)
        self.assertEqual(before, self.snapshot())

    def test_store_mismatch_stops_without_writing(self):
        self.refused(2, 'record', '--choice', 'codex')
        self.assertFalse(self.config.exists())
        sha = self.record('codex', 'absent')['store_sha256']
        before = self.snapshot()
        self.refused(22, 'record', '--choice', 'internal', '--expect-store-sha256', 'absent')
        self.refused(22, 'record', '--choice', 'internal', '--expect-store-sha256', ZERO)
        self.refused(22, 'record', '--choice', 'codex', '--expect-store-sha256', 'absent', '--project', self.repo,
                     '--launch', LAUNCH, '--secret-set-sha256', EMPTY,
                     '--expect-tuple-sha256', self.long(self.repo, secret=EMPTY)['tuple_sha256'])
        self.assertEqual(before, self.snapshot())
        # 無い保存ファイルに、ある値を渡しても書かない
        other = self.w / 'other-config'
        self.refused(22, 'record', '--choice', 'codex', '--expect-store-sha256', sha,
                     env=self.env_with(XDG_CONFIG_HOME=other))
        self.assertFalse(other.exists())

    def test_symlinked_config_home_can_save(self):
        real = self.w / 'real'
        real.mkdir()
        (self.w / 'link').symlink_to(real)
        out = self.record('internal', 'absent', env=self.env_with(XDG_CONFIG_HOME=self.w / 'link'))
        self.assertEqual(str(real / 'dev-workflow' / STORE_NAME), out['store'])
        self.assertTrue((real / 'dev-workflow' / STORE_NAME).is_file())


class UnusableStoreTest(ChoiceFixture):
    """安全でない・壊れた置き場では、status は store-unusable、record は 20。どちらも何も作らず変えない。"""

    def setUp(self):
        super().setUp()
        self.repo = self.newrepo(self.w / 'r')

    def check_unusable(self, env=None, patch=None, long=True, short=True):
        env = env or self.env
        before = self.snapshot()

        def call(*args):
            return self.patched(patch, *args, env=env) if patch else self.ic(*args, env=env)

        if short:
            value = self.ok(done=call('status', '--runner', 'codex'))
            self.assertEqual(('verify', 'store-unusable'), (value['decision'], value['reason']))
            self.assertIsNone(value['approval_count'])
        if long:
            value = self.ok(done=call('status', '--runner', 'codex', '--project', self.repo, '--launch', LAUNCH,
                                      '--secret-set-sha256', EMPTY))
            self.assertEqual(('ask', 'store-unusable'), (value['decision'], value['reason']))
        done = call('status', '--runner', 'codex', '--project', self.repo, '--launch', LAUNCH,
                    '--secret-set-sha256', EMPTY)
        self.assertEqual(1, len(done.stderr.splitlines()), describe(done))
        sha = self.ok(done=done)['store_sha256']
        expects = {'absent', sha} if sha != 'unreadable' else {'absent'}
        for expect in sorted(expects):
            self.refused(20, done=call('record', '--choice', 'internal', '--expect-store-sha256', expect))
            tuple_sha = self.long(self.repo, secret=EMPTY, env=self.env_with(XDG_CONFIG_HOME=self.w / 'scratch'))
            self.refused(20, done=call('record', '--choice', 'codex', '--expect-store-sha256', expect,
                                       '--project', self.repo, '--launch', LAUNCH, '--secret-set-sha256', EMPTY,
                                       '--expect-tuple-sha256', tuple_sha['tuple_sha256']))
        self.assertEqual(before, self.snapshot())

    def test_broken_or_unsafe_store_files(self):
        cases = {
            'broken-json': lambda: self.write_store(None, raw=b'{'),
            'extra-key': lambda: self.write_store({'schema': 1, 'choice': 'codex', 'repos': {}, 'extra': 1}),
            'bad-schema': lambda: self.write_store({'schema': 2, 'choice': 'codex', 'repos': {}}),
            'bad-choice': lambda: self.write_store({'schema': 1, 'choice': 'Codex', 'repos': {}}),
            'extra-approval-key': lambda: self.write_store(
                {'schema': 1, 'choice': 'codex', 'repos': {'/x/.git': [dict(self.approval('1'), note='x')]}}),
            'duplicate-key': lambda: self.write_store(None, raw=b'{"schema":1,"choice":"codex","choice":"codex","repos":{}}'),
            'readable-by-others': lambda: (self.write_store({'schema': 1, 'choice': 'codex', 'repos': {}}),
                                           self.store().chmod(0o644)),
            'not-regular': lambda: (self.store_dir().mkdir(parents=True, mode=0o700), self.store().mkdir()),
            'symlink-file': lambda: (self.store_dir().mkdir(parents=True, mode=0o700),
                                     (self.w / 'target.json').write_text('{"schema":1,"choice":"codex","repos":{}}'),
                                     self.store().symlink_to(self.w / 'target.json')),
            'too-large': lambda: self.write_store(None, raw=b'{' + b' ' * (1024 * 1024) + b'}'),
            'nine-approvals': lambda: self.write_store(
                {'schema': 1, 'choice': 'codex', 'repos': {'/x/.git': [self.approval('1', launch=f'l{index}')
                                                                       for index in range(9)]}}),
            'upper-case-sha256': lambda: self.write_store(
                {'schema': 1, 'choice': 'codex', 'repos': {'/x/.git': [dict(self.approval('1'),
                                                                            launch_sha256='A' * 64)]}}),
            'approved-at-format': lambda: self.write_store(
                {'schema': 1, 'choice': 'codex', 'repos': {'/x/.git': [dict(self.approval('1'),
                                                                            approved_at='2026-11-19 00:00:00Z')]}}),
            'group-writable-dir': lambda: (self.write_store({'schema': 1, 'choice': 'codex', 'repos': {}}),
                                           self.store_dir().chmod(0o770)),
            'store-dir-is-file': lambda: (self.config.mkdir(mode=0o700),
                                          self.store_dir().write_text('x')),
        }
        for name, prepare in cases.items():
            with self.subTest(case=name):
                shutil.rmtree(str(self.config), ignore_errors=True)
                (self.w / 'target.json').unlink(missing_ok=True)
                prepare()
                self.check_unusable()

    def test_directory_owned_by_someone_else(self):
        self.record('codex', 'absent')
        self.check_unusable(patch='uid')

    def test_symlink_swapped_in_after_resolution(self):
        real = self.w / 'real'
        real.mkdir()
        (self.w / 'link').symlink_to(real)
        env = self.env_with(XDG_CONFIG_HOME=self.w / 'link')
        self.check_unusable(env=env, patch='realpath')
        self.assertEqual([], list(real.iterdir()))
        self.record('codex', 'absent', env=env)
        self.check_unusable(env=env, patch='realpath')

    def test_store_under_tmp_tmpdir_or_project(self):
        tmp_config = Path('/tmp') / f'dw501-test-{os.getpid()}-{time.monotonic_ns()}'
        self.assertFalse(tmp_config.exists())
        self.check_unusable(env=self.env_with(XDG_CONFIG_HOME=tmp_config))
        self.assertFalse(tmp_config.exists())
        tmpdir = self.w / 'tmpdir'
        tmpdir.mkdir()
        self.check_unusable(env=self.env_with(TMPDIR=tmpdir, XDG_CONFIG_HOME=tmpdir / 'config'))
        self.assertEqual([], list(tmpdir.iterdir()))
        inside = self.env_with(XDG_CONFIG_HOME=self.repo / 'cfg')
        value = self.long(self.repo, secret=EMPTY, env=inside)
        self.assertEqual(('ask', 'store-unusable'), (value['decision'], value['reason']))
        before = self.snapshot()
        self.refused(20, 'record', '--choice', 'codex', '--expect-store-sha256', 'absent', '--project', self.repo,
                     '--launch', LAUNCH, '--secret-set-sha256', EMPTY, '--expect-tuple-sha256',
                     value['tuple_sha256'], env=inside)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.repo / 'cfg').exists())

    def test_no_absolute_home_means_no_store(self):
        self.check_unusable(env=self.env_with(HOME='relative', XDG_CONFIG_HOME='also-relative'))
        self.check_unusable(env=self.env_with(HOME=None, XDG_CONFIG_HOME=None))


@unittest.skipUnless(HAVE_YAML, 'PyYAML が無い(requirements-dev.txt を入れる)')
class SecretSetTest(ChoiceFixture):
    def test_ignored_and_declared_secret_paths_are_listed(self):
        repo = self.newrepo(self.w / 'r')
        (repo / '.claude').mkdir()
        (repo / '.claude' / 'project-profile.yml').write_text('secret_paths: ["keys/**"]\n')
        files = {'.env': 'DW501_SECRET_A', '.dev.vars': 'DW501_SECRET_B', 'app/.env.local': 'DW501_SECRET_C',
                 'keys/a/b.pem': 'DW501_SECRET_D', 'src/main.py': 'print(1)\n', 'README.md': 'x\n',
                 'envfile': 'x'}
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        (repo / '.gitignore').write_text('.env\n.dev.vars\n.env.local\nkeys/\n')
        self.git(repo, 'add', 'src/main.py', 'README.md', '.gitignore')
        self.git(repo, 'commit', '-q', '-m', 'files')
        done = self.ic('secret-set', '--cwd', repo)
        value = self.ok(done=done)
        self.assertEqual(['.dev.vars', '.env', 'app/.env.local', 'keys/a/b.pem'], value['paths'])
        self.assertEqual(4, value['count'])
        self.assertEqual(['.env', '.env.*', '.dev.vars', 'keys/**'], value['globs'])
        self.assertNotIn('DW501_SECRET', done.stdout + done.stderr)
        expected = sha256(b''.join(path.encode() + b'\0' for path in sorted(value['paths'])))
        self.assertEqual(expected, value['sha256'])

    def test_rooted_glob_is_anchored_at_the_top_not_the_management_root(self):
        top = self.newrepo(self.w / 'r8')
        src = top / 'src'
        (src / '.claude').mkdir(parents=True)
        (src / '.claude' / 'project-profile.yml').write_text('secret_paths: ["secrets/**", "/root-only.key"]\n')
        for name in ('secrets/a.key', '.dev.vars', '.env.local', 'root-only.key', 'sub/root-only.key',
                     'src/root-only.key', 'src/a.py'):
            (top / name).parent.mkdir(parents=True, exist_ok=True)
            (top / name).write_text('x')
        (top / '.gitignore').write_text('.dev.vars\n.env.local\nsecrets/\n*.key\n')
        value = self.secret_set(src)
        self.assertEqual(['.dev.vars', '.env.local', 'root-only.key', 'secrets/a.key'], value['paths'])
        self.assertEqual(4, value['count'])
        self.assertIn('secrets/**', value['globs'])
        self.assertIn('/root-only.key', value['globs'])

    def test_many_ignored_files_do_not_change_the_result(self):
        repo = self.newrepo(self.w / 'r')
        (repo / '.env').write_text('x')
        (repo / '.gitignore').write_text('node_modules/\n')
        before = self.secret_set(repo)
        many = repo / 'node_modules'
        many.mkdir()
        for index in range(20000):
            (many / f'f{index}').write_bytes(b'')
        after = self.secret_set(repo)
        self.assertEqual(before, after)
        self.assertEqual((1, ['.env']), (after['count'], after['paths']))

    def test_sha256_does_not_depend_on_the_order(self):
        names = ['.env.b', 'z/.env', '.env.a', 'a/.env', '.env']
        values = []
        for index, order in enumerate((names, list(reversed(names)))):
            repo = self.newrepo(self.w / f'r{index}')
            for name in order:
                (repo / name).parent.mkdir(parents=True, exist_ok=True)
                (repo / name).write_text('x')
            values.append(self.secret_set(repo))
        self.assertEqual(values[0], values[1])
        paths = sorted(name.encode() for name in names)
        self.assertEqual([path.decode() for path in paths], values[0]['paths'])
        self.assertEqual(sha256(b''.join(path + b'\0' for path in paths)), values[0]['sha256'])

    def test_tracked_and_untracked_secrets_are_sorted_together(self):
        # git ls-files は未追跡と追跡済みを別々に並べるので、まとめて並べ直さないと順が崩れる
        repo = self.newrepo(self.w / 'r')
        for name in ('.env.a', 'z/.env', '.env.b'):
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text('x')
        self.git(repo, 'add', '.env.a', 'z/.env')
        self.git(repo, 'commit', '-q', '-m', 'tracked secrets')
        listed = self.git(repo, 'ls-files', '-z', '--cached', '--others').stdout.split(b'\0')
        self.assertNotEqual(sorted(item for item in listed if item), [item for item in listed if item])
        value = self.secret_set(repo)
        self.assertEqual(['.env.a', '.env.b', 'z/.env'], value['paths'])
        self.assertEqual(sha256(b'.env.a\0.env.b\0z/.env\0'), value['sha256'])

    def test_no_match_is_the_empty_sha256(self):
        repo = self.newrepo(self.w / 'r')
        self.assertEqual({'count': 0, 'paths': [], 'globs': ['.env', '.env.*', '.dev.vars'], 'sha256': EMPTY},
                         self.secret_set(repo))

    def test_broken_store_does_not_stop_secret_set(self):
        repo = self.newrepo(self.w / 'r')
        (repo / '.env').write_text('x')
        self.write_store(None, raw=b'{')
        self.store().chmod(0o644)
        self.assertEqual(['.env'], self.secret_set(repo)['paths'])
        self.assertEqual(b'{', self.store().read_bytes())

    def test_not_git_stops(self):
        plain = self.w / 'plain'
        plain.mkdir()
        self.refused(20, 'secret-set', '--cwd', plain)
        self.refused(20, 'secret-set', '--cwd', self.w / 'missing')


class PartTimeoutTest(ChoiceFixture):
    def test_part_that_does_not_finish_is_cut_off(self):
        repo = self.newrepo(self.w / 'r')
        stub = self.w / 'bin'
        stub.mkdir()
        (stub / 'git').write_text('#!/bin/sh\nexec sleep 120\n')
        (stub / 'git').chmod(0o755)
        env = self.env_with(PATH=f'{stub}:{os.environ.get("PATH", "")}', **{TIMEOUT_ENV: '1'})
        for args in (('secret-set', '--cwd', repo),
                     ('status', '--runner', 'codex', '--project', repo, '--launch', LAUNCH,
                      '--secret-set-sha256', EMPTY),
                     ('record', '--choice', 'codex', '--expect-store-sha256', 'absent', '--project', repo,
                      '--launch', LAUNCH, '--secret-set-sha256', EMPTY, '--expect-tuple-sha256', ZERO)):
            with self.subTest(command=args[0]):
                started = time.monotonic()
                done = self.refused(20, *args, env=env)
                self.assertLess(time.monotonic() - started, 30)
                self.assertIn('打ち切った', done.stderr)
        self.assertFalse(self.config.exists())


class ContractSyncTest(unittest.TestCase):
    """実装用既定表(external-runners.md §12-6)のランナー名と、RUNNER_CONFIG_DIRS の鍵が同じか。"""

    @staticmethod
    def default_table_runners(text):
        # 節を見出しで特定してから表を拾う(implement-agent-selftest.sh が §12-6 を拾うやり方と同じ)。
        # §12-6 の中の、1 列目が「ランナー」の表だけを読み、ほかの表(解決順の表など)は読まない
        lines = text.split('\n')
        start = next(index for index, line in enumerate(lines) if line.startswith('### 12-6.'))
        end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith('### '))
        section = lines[start + 1:end]
        header = next(index for index, line in enumerate(section) if line.startswith('| ランナー |'))
        names = []
        for line in section[header + 2:]:
            if not line.startswith('|'):
                break
            names.append(line.split('|')[1].strip().strip('`'))
        return names

    def test_runner_config_dirs_cover_the_default_table(self):
        text = (REPO / 'plugins/dev-workflow/skills/do-task/references/external-runners.md').read_text(encoding='utf-8')
        names = self.default_table_runners(text)
        self.assertTrue(names)
        module = load_script(SCRIPT, 'implementer_choice_sync')
        self.assertEqual(sorted(names), sorted(module.RUNNER_CONFIG_DIRS))


class UsageTest(ChoiceFixture):
    def test_usage_errors_exit_two_without_side_effects(self):
        repo = self.newrepo(self.w / 'r')
        base = ('--runner', 'codex', '--project', repo, '--secret-set-sha256', EMPTY)
        cases = [
            (),
            ('unknown',),
            ('status', '--runner', 'internal'),
            ('status', '--runner', 'Codex'),
            ('status', '--runner', 'a' * 33),
            ('status', '--project', repo),
            ('status', '--project', repo, '--launch', LAUNCH, '--secret-set-sha256', EMPTY),
            ('status', '--runner', 'codex', '--launch', LAUNCH),
            ('status', *base, '--launch', ''),
            ('status', *base, '--launch', 'codex\nexec'),
            ('status', *base, '--launch', 'codex\rexec'),
            ('status', *base, '--launch', 'x' * 4097),
            ('status', '--runner', 'codex', '--project', repo, '--launch', LAUNCH, '--secret-set-sha256', 'xyz'),
            ('status', '--runner', 'codex', '--project', repo, '--launch', LAUNCH, '--secret-set-sha256', 'F' * 64),
            ('status', '--runn', 'codex'),
            ('record', '--choice', 'codex'),
            ('record', '--choice', 'internal', '--expect-store-sha256', 'missing'),
            ('record', '--choice', 'internal', '--expect-store', 'absent'),
            ('record', '--choice', 'BAD', '--expect-store-sha256', 'absent'),
            ('record', '--choice', 'codex', '--expect-store-sha256', 'absent', '--project', repo),
            ('record', '--choice', 'internal', '--expect-store-sha256', 'absent', '--project', repo,
             '--launch', LAUNCH, '--secret-set-sha256', EMPTY, '--expect-tuple-sha256', ZERO),
            ('record', '--choice', 'codex', '--expect-store-sha256', 'absent', '--project', repo,
             '--launch', LAUNCH, '--secret-set-sha256', 'unknown', '--expect-tuple-sha256', ZERO),
            ('record', '--choice', 'codex', '--expect-store-sha256', 'absent', '--project', repo,
             '--launch', LAUNCH, '--secret-set-sha256', EMPTY, '--expect-tuple-sha256', 'x'),
            ('secret-set',),
            ('secret-set', '--cwd', repo, '--extra'),
        ]
        before = self.snapshot()
        for args in cases:
            with self.subTest(args=args):
                self.refused(2, *args)
        self.assertEqual(before, self.snapshot())
        value = self.long(repo, launch='y' * 4096, secret=EMPTY)
        self.assertEqual('unset', value['reason'])


if __name__ == '__main__':
    unittest.main()
