"""起動前の管理入口と、終了・再開時の設定先読みに対する回帰。"""
import ctypes
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/loop-startup.py'
spec = importlib.util.spec_from_file_location('loop_startup_tested', SOURCE)
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)

# シェルのテストの 1 通りの確かめ(run_case の 1 回)を待つ上限の秒数。混んだ機械では環境変数で延ばす。
CASE_TIMEOUT_VAR = 'LOOP_STARTUP_CASE_TIMEOUT'
CASE_TIMEOUT_DEFAULT = 300
# 前置きの見張りは、上限で TERM を送り、その 10 秒後に KILL を送る(timeout -k 10)。外側はその後に止める。
OUTER_MARGIN = 30
TIMEOUT_MARK = '時間切れ: '
EARLY_EXIT_MARK = '背景の loop.sh が起動の印 '
# 背景の loop.sh(start_bg)が子を起動した印を待つ秒数。start_bg は背景の loop.sh を 150 秒で止めるので、
# 印の後に止める合図を処理する時間を残して、それより短くする。
START_WAIT = 100


def wait_started_function(seconds=START_WAIT):
    """起動の印を待つ bash の関数 wait_started。印が現れれば 0 を返す。

    背景の loop.sh(BG_PID)が印を作らずに終わったら、待ち切らずにすぐ、時間切れでない文を出して 1 を返す。
    時間切れの文は、待ち切った時に背景の loop.sh がまだ動いているときだけ出す。
    待つ時間は SECONDS(1 秒刻み)で数えるので、1 足して seconds 秒以上待つ。
    """
    return f'''
wait_started() {{ # $1=起動の印のパス
  local end=$((SECONDS + {seconds} + 1))
  while [ ! -e "$1" ] && proc_alive "$BG_PID" && [ "$SECONDS" -lt "$end" ]; do sleep 0.1; done
  [ -e "$1" ] && return 0
  if ! proc_alive "$BG_PID"; then
    wait_bg
    printf '{EARLY_EXIT_MARK}%s の前に終わった(終了コード %s)\\n' "${{1##*/}}" "$BG_RC" >&2
    return 1
  fi
  printf '{TIMEOUT_MARK}起動の印 %s が {seconds} 秒待っても現れない。混んだ機械では流し直す\\n' "${{1##*/}}" >&2
  return 1
}}
'''


def case_timeout():
    """1 通りの上限の秒数。環境変数が無いか空なら既定の値。"""
    raw = os.environ.get(CASE_TIMEOUT_VAR, '')
    if raw == '':
        return CASE_TIMEOUT_DEFAULT
    if not re.fullmatch(r'[1-9][0-9]*', raw):
        raise ValueError(f'{CASE_TIMEOUT_VAR} は 1 以上の整数の秒数にする: {raw!r}')
    return int(raw)


def timeout_message(limit, elapsed):
    return (f'{TIMEOUT_MARK}{limit} 秒の上限に当たった({elapsed:.0f} 秒で止められた)。'
            f'混んだ機械では {CASE_TIMEOUT_VAR} で延ばすか、流し直す')


class Watch:
    def __init__(self, path):
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0 or libc.inotify_add_watch(self.fd, os.fsencode(path), 0x21) < 0:
            raise unittest.SkipTest('inotify を使えない')
    def events(self):
        try:
            return os.read(self.fd, 65536)
        except BlockingIOError:
            return b''
    def close(self):
        os.close(self.fd)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='loop-startup-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.base = self.root / 'state'
        self.base.mkdir()
        self.env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        self.git('init', '-q', '-b', 'main', str(self.repo))
        self.git('-C', str(self.repo), '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                 'commit', '--allow-empty', '-qm', 'initial')
    def git(self, *args):
        return subprocess.check_output(['git', *args], env=self.env, stderr=subprocess.PIPE).decode().strip()
    def bind(self, repo=None):
        repo = repo or self.repo
        current = startup.resolve(str(repo), str(self.base))
        expected = {k: current[k] for k in ('top', 'repo_admin', 'common', 'management_sha256')}
        return startup.bind(str(repo), str(self.base), expected)
    def watch(self, path):
        watch = Watch(path)
        self.addCleanup(watch.close)
        return watch
    def test_main_resolve_and_binding_are_stable_without_creating_state(self):
        result = startup.resolve(str(self.repo), str(self.base))
        self.assertFalse(result['state_exists'])
        self.assertEqual(result['common'], str(self.repo / '.git'))
        held = self.bind()
        self.assertEqual(held, startup.resolve(str(self.repo), str(self.base)))
        self.assertFalse(Path(held['state_path']).exists())
        self.assertEqual(self.bind(), held)
    def test_linked_and_separate_gitdir_normal(self):
        linked = self.root / 'linked'
        self.git('-C', str(self.repo), 'worktree', 'add', '--detach', str(linked))
        separate = self.root / 'separate'
        self.git('init', '-q', '--separate-git-dir', str(self.root / 'admin'), str(separate))
        for repo in (linked, separate):
            with self.subTest(repo=repo.name):
                result = self.bind(repo)
                self.assertEqual(result['repo_admin'], self.git('-C', str(repo), 'rev-parse', '--path-format=absolute', '--git-dir'))
                self.assertEqual(result['common'], self.git('-C', str(repo), 'rev-parse', '--path-format=absolute', '--git-common-dir'))
                self.assertEqual(result, startup.resolve(str(repo), str(self.base)))
    def test_config_is_never_opened_even_when_fifo(self):
        config = self.repo / '.git/config'
        config.unlink()
        os.mkfifo(config)
        self.bind()
    def test_bound_pointer_replacement_stops_before_new_content(self):
        linked = self.root / 'linked'
        self.git('-C', str(self.repo), 'worktree', 'add', '--detach', str(linked))
        self.bind(linked)
        pointer = linked / '.git'
        pointer.unlink()
        pointer.write_text('SENTINEL secret content\n')
        watch = self.watch(pointer)
        with self.assertRaises(startup.Stop):
            startup.resolve(str(linked), str(self.base))
        self.assertEqual(watch.events(), b'')
    def test_bound_commondir_replacement_stops_before_content(self):
        linked = self.root / 'linked'
        self.git('-C', str(self.repo), 'worktree', 'add', '--detach', str(linked))
        held = self.bind(linked)
        pointer = Path(held['repo_admin']) / 'commondir'
        pointer.write_text('SENTINEL secret content\n')
        watch = self.watch(pointer)
        with self.assertRaises(startup.Stop):
            startup.resolve(str(linked), str(self.base))
        self.assertEqual(watch.events(), b'')
    def test_whole_admin_replacement_stops_and_other_repository_works(self):
        self.bind()
        (self.repo / '.git').rename(self.repo / '.old-git')
        self.git('init', '-q', str(self.repo))
        watch = self.watch(self.repo / '.git/config')
        with self.assertRaises(startup.Stop):
            startup.resolve(str(self.repo), str(self.base))
        self.assertEqual(watch.events(), b'')
        other = self.root / 'other'
        self.git('init', '-q', str(other))
        self.bind(other)
    def test_explicit_rebind_and_same_uid_record_removal_boundary(self):
        # 保存物を同じ UID が全削除すれば新規入力との識別はできない。
        held = self.bind()
        (self.repo / '.git').rename(self.repo / '.old-git')
        self.git('init', '-q', str(self.repo))
        with self.assertRaises(startup.Stop):
            startup.resolve(str(self.repo), str(self.base))
        Path(held['binding_path']).unlink()
        self.assertFalse(startup.resolve(str(self.repo), str(self.base))['bound'])
        self.bind()
    def test_special_and_oversized_pointers_stop(self):
        shutil.rmtree(self.repo / '.git')
        for mode in ('fifo', 'symlink', 'large'):
            with self.subTest(mode=mode):
                p = self.repo / '.git'
                if mode == 'fifo': os.mkfifo(p)
                elif mode == 'symlink': p.symlink_to(self.root / 'secret')
                else: p.write_bytes(b'x' * (startup.MAX_POINTER + 1))
                try:
                    with self.assertRaises(startup.Stop): startup.resolve(str(self.repo), str(self.base))
                finally: p.unlink()
    def test_state_special_file_and_malformed_anchor_stop(self):
        result = self.bind()
        state = Path(result['state_path'])
        state.mkdir()
        os.mkfifo(state / 'lock')
        with self.assertRaises(startup.Stop): startup.resolve(str(self.repo), str(self.base))
        (state / 'lock').unlink()
        Path(result['binding_path']).write_text('{')
        with self.assertRaises(startup.Stop): startup.resolve(str(self.repo), str(self.base))
    def test_copied_binding_and_state_are_not_accepted_as_original_placement(self):
        current = startup.resolve(str(self.repo), str(self.base))
        Path(current['state_path']).mkdir()
        held = self.bind()
        other = self.root / 'new-state'
        shutil.copytree(self.base, other)
        with self.assertRaises(startup.Stop):
            startup.resolve(str(self.repo), str(other))
        original = Path(held['state_path'])
        original.rename(original.with_name('old-state'))
        original.mkdir()
        with self.assertRaises(startup.Stop):
            startup.resolve(str(self.repo), str(self.base))
    def test_unbound_linked_entry_can_report_existing_lock_without_reading_state(self):
        linked = self.root / 'linked'
        self.git('-C', str(self.repo), 'worktree', 'add', '--detach', str(linked))
        current = startup.resolve(str(self.repo), str(self.base))
        state = Path(current['state_path'])
        state.mkdir()
        self.bind()
        with (state / 'lock').open('w') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            result = startup.resolve(str(linked), str(self.base))
            self.assertTrue(result['locked'])
            self.assertFalse(result['bound'])
        self.assertFalse(startup.resolve(str(linked), str(self.base))['locked'])

    def test_management_parent_replacement_is_detected(self):
        held = self.bind()
        original = startup.Observation.pointer
        def replace_after_read(observation, parent, name, before):
            data = original(observation, parent, name, before)
            if name == Path(held['binding_path']).name:
                self.repo.rename(self.root / 'old-repo')
                self.repo.mkdir()
                shutil.copytree(self.root / 'old-repo/.git', self.repo / '.git')
            return data
        with mock.patch.object(startup.Observation, 'pointer', replace_after_read):
            with self.assertRaises(startup.Stop):
                startup.resolve(str(self.repo), str(self.base))
    def test_traversal_has_an_explicit_item_budget(self):
        with mock.patch.object(startup, 'MAX_COMPONENTS', 2):
            with self.assertRaises(startup.Stop):
                startup.resolve(str(self.repo), str(self.base))
    def test_gitfile_relative_path_is_accepted(self):
        admin = self.root / 'admin'
        (self.repo / '.git').rename(admin)
        (self.repo / '.git').write_text('gitdir: ../admin\n')
        self.assertEqual(self.bind()['repo_admin'], str(admin))

    def test_sibling_change_does_not_change_directory_identity(self):
        held = self.bind()
        (self.repo / '.git/irrelevant').touch()
        self.assertEqual(held, startup.resolve(str(self.repo), str(self.base)))
    def test_bind_requires_the_same_observation(self):
        result = startup.resolve(str(self.repo), str(self.base))
        expected = {k: result[k] for k in ('top', 'repo_admin', 'common', 'management_sha256')}
        (self.repo / '.git').rename(self.repo / '.old-git')
        self.git('init', '-q', str(self.repo))
        with self.assertRaises(startup.Stop): startup.bind(str(self.repo), str(self.base), expected)


@unittest.skipUnless(sys.platform.startswith('linux'), 'inotify と scratch shell fixture を使う')
class StartupShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.timeout = case_timeout()
        cls.tmp = tempfile.TemporaryDirectory(prefix='loop-startup-shell-')
        cls.root = Path(cls.tmp.name)
        # 同時作業中の source を試験途中に読み直さない。
        cls.plugin = cls.root / 'plugin'
        # __pycache__ は写さない。並べて流すと、別のテストが起動した python が書きかけの .pyc を置いていることがある。
        shutil.copytree(ROOT / 'plugins/dev-workflow', cls.plugin, ignore=shutil.ignore_patterns('__pycache__'))
        scripts = cls.plugin / 'skills/ship-task/scripts'
        text = (scripts / 'loop-selftest.sh').read_text()
        cls.prefix = text.split('# LOOP_SELFTEST_ONLY=', 1)[0]
        # 証明づくり(loop.sh を 1 回起動する)の手前で切った版。loop.sh を起動しないケースだけが使う。
        proof_mark = '治具: スタブのホスト CLI の証明'
        if cls.prefix.count(proof_mark) != 1:
            raise RuntimeError('共通の準備に証明づくりの目印がちょうど 1 つ無い')
        cls.prefix_no_proof = cls.prefix.split(proof_mark, 1)[0]
        line = next(line for line in cls.prefix.splitlines() if line.startswith('SCRIPT_DIR='))
        old_cwd = '( cd "$W/cwd" && exec env'
        new_cwd = '( cd "${STARTUP_CWD:-$W/cwd}" && exec env'
        old_cfg = 'g config selftest.added yes;'
        new_cfg = '''printf '\\n[include]\\n\\tpath = %s\\n' "$SELFTEST_SECRET" >>"$SELFTEST_COMMON/config";'''
        script_dir = 'SCRIPT_DIR=' + repr(str(scripts))
        # どちらの版にも同じ置き換えを掛ける。同じ物理 cwd からの起動でも早期 local-env-vars 照会を検査する。
        for name in ('prefix', 'prefix_no_proof'):
            part = getattr(cls, name).replace(line, script_dir)
            part = part.replace(old_cfg, new_cfg).replace(old_cwd, new_cwd)
            setattr(cls, name, part)
    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
    def run_case(self, body, expected, transform=None, proof=True):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            tmp = Path(tmp)
            sentinel = tmp / 'secret-config'
            sentinel.write_text('[test]\n value = harmless\n')
            watch = Watch(sentinel)
            try:
                prefix = self.prefix if proof else self.prefix_no_proof
                if transform is not None:
                    plugin = tmp / 'instrumented-plugin'
                    shutil.copytree(self.plugin, plugin)
                    loop = plugin / 'skills/ship-task/scripts/loop.sh'
                    loop.write_text(transform(loop.read_text()))
                    prefix = prefix.replace(repr(str(self.plugin / 'skills/ship-task/scripts')),
                                            repr(str(plugin / 'skills/ship-task/scripts')))
                script = tmp / 'case.sh'
                script.write_text(prefix + '\n' + wait_started_function() + '\n' + body
                                  + '\ncat "$OUT"\nprintf "\\nCASE_RC=%s\\n" "$RC"\nexit 0\n')
                limit = self.timeout
                env = {**os.environ, 'SENTINEL': str(sentinel), 'LOOP_SELFTEST_TIMEOUT': str(limit)}
                # 1 回の loop.sh の上限(run_loop の RUN_TIMEOUT)を 1 通りの上限以上にして、見張りが先に
                # 当たるようにする(時間切れの文を出すため)。環境の値がそれ以上なら、そのまま使う。
                if not (re.fullmatch(r'[0-9]+', env.get('RUN_TIMEOUT', '')) and int(env['RUN_TIMEOUT']) >= limit):
                    env['RUN_TIMEOUT'] = str(limit)
                started = time.monotonic()
                try:
                    result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True,
                                            timeout=limit + OUTER_MARGIN)
                except subprocess.TimeoutExpired as error:
                    output = ''.join(part.decode(errors='replace') if isinstance(part, bytes) else part
                                     for part in (error.stdout or '', error.stderr or ''))
                    self.fail(timeout_message(limit + OUTER_MARGIN, time.monotonic() - started) + '\n' + output)
                elapsed = time.monotonic() - started
                output = result.stdout + result.stderr
                if result.returncode != 0:
                    # 見張りの TERM で止まれば 124。別のプロセスグループの子(loop.sh)を待っている間は
                    # TERM を処理できず、10 秒後の KILL で見張り自身も止まる(-9)。
                    if result.returncode in (124, 137, -9) and elapsed >= limit:
                        self.fail(timeout_message(limit, elapsed) + '\n' + output)
                    # wait_started の文(時間切れか、背景の loop.sh が先に終わったか)を先頭に出す。
                    reasons = [line for line in result.stderr.splitlines()
                               if line.startswith((TIMEOUT_MARK, EARLY_EXIT_MARK))]
                    if reasons:
                        self.fail(reasons[0] + '\n' + output)
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('CASE_RC=' + str(expected), result.stdout, output)
                self.assertEqual(watch.events(), b'', output)
                return result.stdout
            finally:
                watch.close()
    def test_finish_after_config_mutation_never_reopens_include(self):
        out = self.run_case('''
newrepo config-finish
addtask cfgadd-a 2026-01-01
commit
newrec config-finish
run_loop config-finish "SELFTEST_SECRET=$SENTINEL" "SELFTEST_COMMON=$R/.git" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ -f "$(report_of "$OUT")" ] || exit 71
''', 10)
        self.assertIn('shared-state', out)
    def test_environment_rejection_keeps_finish_from_reading_a_new_global_include(self):
        def transform(source):
            point = '  # 照合(D14・D8)。§5 のネットワークの git より前\n  verify_iteration\n'
            self.assertEqual(source.count(point), 1)
            action = '  printf "\\n[include]\\n path = %s\\n" "$SELFTEST_SECRET" >>"$GIT_CONFIG_GLOBAL"\n'
            return source.replace(point, action + point)
        self.run_case("""
newrepo global-config-finish
addtask sync-a 2026-01-01
commit
newrec global-config-finish
run_loop global-config-finish "SELFTEST_SECRET=$SENTINEL" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
""", 10, transform)

    def test_finish_rechecks_environment_after_the_last_successful_verification(self):
        def transform(source):
            point = '  local code="$1" why="$2" n\n  trap - TERM HUP INT\n'
            self.assertEqual(source.count(point), 1)
            action = '  printf "\\n[include]\\n path = %s\\n" "$SELFTEST_SECRET" >>"$GIT_CONFIG_GLOBAL"\n'
            return source.replace(point, point + action)
        out = self.run_case("""
newrepo late-global-config-finish
addtask sync-a 2026-01-01
commit
newrec late-global-config-finish
run_loop late-global-config-finish "SELFTEST_SECRET=$SENTINEL" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
""", 20, transform)
        self.assertIn('environment-changed', out)

    def test_restart_changed_local_config_from_repository_cwd_never_reads_include(self):
        self.run_case(self.restart_body('printf "\\n[include]\\n path = %s\\n" "$SENTINEL" >>"$R/.git/config"'), 20)
    def test_restart_changed_global_config_never_reads_include(self):
        self.run_case(self.restart_body('printf "\\n[include]\\n path = %s\\n" "$SENTINEL" >>"$GIT_CONFIG_GLOBAL"'), 20)
    def restart_body(self, mutation):
        return '''
newrepo restart
addtask longsleep-a 2026-01-01
commit
newrec restart
start_bg restart -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
wait_started "$REC/started-longsleep-a" || { cat "$BG_OUT"; exit 72; }
kill -KILL "$BG_PID"
wait_bg
''' + mutation + '''
STARTUP_CWD="$R"
run_loop restart -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ -d "$(state_dir restart)/inflight" ] || exit 73
'''
    def test_unchanged_inflight_requires_confirmation_before_next_iteration(self):
        self.run_case("""
newrepo unchanged
addtask longsleep-a 2026-01-01
addtask sync-b 2026-01-02
commit
newrec unchanged
start_bg unchanged -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
wait_started "$REC/started-longsleep-a" || { cat "$BG_OUT"; exit 72; }
kill -KILL "$BG_PID"
wait_bg
SD="$(state_dir unchanged)"
cp "$SD/inflight/meta" "$REC/held-meta"
cp "$SD/inflight/base.json" "$REC/held-base.json"
CHILD="$(cat "$REC/pid-longsleep-a")"
run_loop unchanged -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$RC" = 20 ] || { cat "$OUT"; exit 75; }
proc_alive "$CHILD" || exit 78
cmp -s "$REC/held-meta" "$SD/inflight/meta" || exit 79
cmp -s "$REC/held-base.json" "$SD/inflight/base.json" || exit 80
[ "$(calls)" = longsleep-a ] || exit 81
confirm_rejected_inflight "$SD" "unchanged inflight"
run_loop unchanged -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ ! -d "$SD/inflight" ] || { cat "$OUT"; exit 76; }
[ "$(calls)" = 'longsleep-a sync-b' ] || { cat "$OUT"; exit 77; }
""", 0)

    def test_early_stop_mark_reports_its_name_and_human_confirmation_without_reading_it(self):
        for stage in ('early', 'after-environment'):
            with self.subTest(stage=stage):
                def transform(source):
                    if stage == 'early':
                        return source
                    point = 'STARTUP_JSON="$("$PY_ABS" "$LOOP_STARTUP_PY" --repo "$REPO" --state-base "$STATE_BASE")"'
                    self.assertEqual(source.count(point), 1)
                    action = 'if [ -n "${SELFTEST_LATE_MARK:-}" ]; then ln -s "$SELFTEST_SECRET" "$SELFTEST_LATE_MARK"; fi\n'
                    return source.replace(point, action + point)
                mutation = 'ln -s "$SENTINEL" "$SD/stop-mark.md"' if stage == 'early' else ':'
                late_args = '"SELFTEST_SECRET=$SENTINEL" "SELFTEST_LATE_MARK=$SD/stop-mark.md"' if stage != 'early' else ''
                out = self.run_case("""
newrepo early-stop-diagnostic
addtask sync-a 2026-01-01
commit
newrec early-stop-diagnostic
run_loop early-stop-diagnostic -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$RC" = 0 ] || { cat "$OUT"; exit 121; }
SD="$(state_dir early-stop-diagnostic)"
@MUTATION@
run_loop early-stop-diagnostic @LATE_ARGS@ -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ -L "$SD/stop-mark.md" ] || exit 122
""".replace('@MUTATION@', mutation).replace('@LATE_ARGS@', late_args), 20, transform)
                self.assertIn('stop-mark.md', out)
                self.assertIn('人が設定・中断記録・残った子を確認するまで印を外して再開しない', out)

    def test_stop_file_with_inflight_requires_confirmation_then_stops_normally(self):
        for rel in ('.claude/loop.stop', 'custom.stop', 'new/control/stop'):
            with self.subTest(path=rel):
                out = self.run_case("""
newrepo stop-restart
addtask longsleep-a 2026-01-01
addtask pr-b 2026-01-02
commit
newrec stop-restart
STOP_ARGS=()
STOP_PATH="$R/@STOP_REL@"
[ @STOP_REL@ = .claude/loop.stop ] || STOP_ARGS=(--stop-file "$STOP_PATH")
start_bg stop-restart -- --repo "$R" "${STOP_ARGS[@]}" "${COMMON_ARGS[@]}"
wait_started "$REC/started-longsleep-a" || { cat "$BG_OUT"; exit 72; }
mkdir -p "$(dirname "$STOP_PATH")"
: >"$STOP_PATH"
kill -KILL "$BG_PID"
wait_bg
CHILD="$(cat "$REC/pid-longsleep-a")"
proc_alive "$CHILD" || exit 78
SD="$(state_dir stop-restart)"
cp "$SD/inflight/meta" "$REC/held-meta"
cp "$SD/inflight/base.json" "$REC/held-base.json"
run_loop stop-restart -- --repo "$R" "${STOP_ARGS[@]}" "${COMMON_ARGS[@]}"
[ "$RC" = 20 ] || { cat "$OUT"; exit 75; }
proc_alive "$CHILD" || exit 78
cmp -s "$REC/held-meta" "$SD/inflight/meta" || exit 81
cmp -s "$REC/held-base.json" "$SD/inflight/base.json" || exit 82
confirm_rejected_inflight "$SD" "stop with inflight"
run_loop stop-restart -- --repo "$R" "${STOP_ARGS[@]}" "${COMMON_ARGS[@]}"
[ ! -d "$SD/inflight" ] || { cat "$OUT"; exit 76; }
[ "$(calls)" = longsleep-a ] || { cat "$OUT"; exit 77; }
wait_dead "$CHILD" 5 || exit 79
[ -f "$STOP_PATH" ] && [ ! -s "$STOP_PATH" ] || exit 80
""".replace('@STOP_REL@', rel), 0)
                self.assertIn('停止ファイル', out)

    def test_persistent_change_after_verification_cannot_become_the_next_baseline(self):
        mutations = {
            'human-file': 'printf "late-write-sentinel\\n" >>"$TOP/README.md"',
            'unrelated-ref': 'G -C "$TOP" update-ref refs/heads/late-write "$DEF_SHA"',
        }
        for name, mutation in mutations.items():
            with self.subTest(change=name):
                def transform(source):
                    # 決定的な外部書込みの模擬。比較・基準保存の処理は変えない。
                    point = '    rep "- worktree: 消した"\n'
                    self.assertEqual(source.count(point), 1)
                    action = (point + '    if [ "$ITER_COUNT" = 2 ]; then\n'
                              '      cp "$LAST_VERIFIED" "$SELFTEST_PROMOTION_CAPTURE/last-before.json"\n'
                              '      ' + mutation + '\n    fi\n')
                    return source.replace(point, action)
                body = """
newrepo promotion
addtask pr-a 2026-01-01
addtask pr-b 2026-01-02
addtask pr-c 2026-01-03
commit
newrec promotion
run_loop promotion "SELFTEST_PROMOTION_CAPTURE=$REC" -- --repo "$R" --max-iterations 3 "${COMMON_ARGS[@]}"
SD="$(state_dir promotion)"
[ "$(calls)" = 'pr-a pr-b' ] || { cat "$OUT"; exit 81; }
cmp -s "$REC/last-before.json" "$SD/last-verified.json" || { cat "$OUT"; exit 82; }
[ -f "$SD/stop-mark.md" ] && [ -d "$SD/inflight" ] || { cat "$OUT"; exit 83; }
"""
                if name == 'human-file':
                    body += 'grep -q late-write-sentinel "$R/README.md" || exit 84\n'
                else:
                    body += 'G -C "$R" show-ref --verify --quiet refs/heads/late-write || exit 85\n'
                self.run_case(body, 10, transform)

    def test_compared_record_replacement_cannot_become_the_next_baseline(self):
        for phase, variable in (('verify_iteration', 'cur'), ('save_last_verified', 'observed')):
            with self.subTest(phase=phase):
                def transform(source):
                    start = source.index(phase + '() {')
                    end = source.index('\n}\n', start)
                    function = source[start:end]
                    point = '--max-seconds "$STATE_MAX_SECONDS")" || rc=$?\n'
                    self.assertEqual(function.count(point), 1)
                    action = (point + '  if [ "$ITER_COUNT" = 2 ]; then\n'
                              '    cp "$LAST_VERIFIED" "$SELFTEST_PROMOTION_CAPTURE/last-before.json"\n'
                              '    python3 -c \'import json,sys; p=sys.argv[1]; v=json.load(open(p)); '
                              'v["test_mutation"]=True; open(p,"w").write(json.dumps(v))\' "$'
                              + variable + '"\n  fi\n')
                    return source[:start] + function.replace(point, action) + source[end:]
                body = """
newrepo record-replacement
addtask pr-a 2026-01-01
addtask pr-b 2026-01-02
addtask pr-c 2026-01-03
commit
newrec record-replacement
run_loop record-replacement "SELFTEST_PROMOTION_CAPTURE=$REC" -- --repo "$R" --max-iterations 3 "${COMMON_ARGS[@]}"
SD="$(state_dir record-replacement)"
[ "$(calls)" = 'pr-a pr-b' ] || { cat "$OUT"; exit 81; }
cmp -s "$REC/last-before.json" "$SD/last-verified.json" || { cat "$OUT"; exit 82; }
[ -f "$SD/stop-mark.md" ] || { cat "$OUT"; exit 83; }
"""
                if phase == 'save_last_verified':
                    body += '[ -d "$SD/inflight" ] || { cat "$OUT"; exit 84; }\n'
                self.run_case(body, 10, transform)

    def test_state_digest_rejects_a_verify_stage_symlink_without_opening_target(self):
        # The parent-held after/base record is an untrusted filesystem object.
        # Replacing it with a link immediately before verify must not make the
        # shell's digest redirection open the link target.
        def transform(source):
            point = '  if [ -z "$ITER_BASE_SHA" ] || [ "$(state_digest "$ITER_BASE" || true)" != "$ITER_BASE_SHA" ]; then\n'
            self.assertEqual(source.count(point), 1)
            action = ('  if [ "$ITER_COUNT" = 1 ]; then\n'
                      '    rm -f -- "$ITER_BASE"\n'
                      '    ln -s -- "$SELFTEST_SECRET" "$ITER_BASE"\n'
                      '  fi\n' + point)
            return source.replace(point, action)
        out = self.run_case("""
newrepo state-digest-link
addtask sync-a 2026-01-01
commit
newrec state-digest-link
run_loop state-digest-link "SELFTEST_SECRET=$SENTINEL" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
SD="$(state_dir state-digest-link)"
[ -f "$SD/stop-mark.md" ] || { cat "$OUT"; exit 111; }
        """, 10, transform)
        self.assertIn('state', out.lower())

    def test_state_digest_verifies_the_environment_before_running_the_helper(self):
        def transform(source):
            point = '  if ! ITER_BASE_SHA="$(state_digest "$ITER_BASE")"; then\n'
            self.assertEqual(source.count(point), 1)
            action = """  cat >"$LOOP_STATE_PY" <<'PY_DIGEST'
import os
with open(os.environ['SELFTEST_ATTACK_MARKER'], 'w') as stream:
    stream.write('executed')
print('0' * 64)
PY_DIGEST
"""
            return source.replace(point, action + point)
        self.run_case("""
newrepo state-digest-environment
addtask sync-a 2026-01-01
commit
newrec state-digest-environment
run_loop state-digest-environment "SELFTEST_ATTACK_MARKER=$REC/digest-executed" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ ! -e "$REC/digest-executed" ] || { cat "$OUT"; exit 113; }
""", 20, transform)

    def test_initial_state_digest_rejection_prevents_finish_from_reloading_config(self):
        # The initial digest is taken after snapshot creation.  If its pathname
        # is replaced together with a newly active include, the rejection must
        # mark Git unsafe before die()/finish() can reopen that include.
        def transform(source):
            point = '  if ! ITER_BASE_SHA="$(state_digest "$ITER_BASE")"; then\n'
            self.assertEqual(source.count(point), 1)
            action = ('  rm -f -- "$ITER_BASE"\n'
                      '  ln -s -- "$SELFTEST_SECRET" "$ITER_BASE"\n'
                      '  printf "\\n[include]\\n path = %s\\n" "$SELFTEST_SECRET" >>"$COMMON/config"\n')
            return source.replace(point, action + point)
        out = self.run_case("""
newrepo initial-state-digest-link
addtask sync-a 2026-01-01
commit
newrec initial-state-digest-link
run_loop initial-state-digest-link "SELFTEST_SECRET=$SENTINEL" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
""", 20, transform)
        self.assertIn('state-snapshot', out)

    def test_term_after_verified_own_removal_keeps_a_valid_baseline(self):
        def transform(source):
            point = '    REMOVED_PARENT_WORKTREES+=("$ITER_WT")\n'
            self.assertEqual(source.count(point), 1)
            return source.replace(point, point + '    [ "$ITER_COUNT" != 2 ] || kill -TERM "$$"\n')
        self.run_case("""
newrepo term-removal
addtask pr-a 2026-01-01
addtask pr-b 2026-01-02
addtask pr-c 2026-01-03
commit
newrec term-removal
run_loop term-removal -- --repo "$R" --max-iterations 3 "${COMMON_ARGS[@]}"
[ "$RC" = 143 ] || { cat "$OUT"; exit 85; }
SD="$(state_dir term-removal)"
[ "$(calls)" = 'pr-a pr-b' ] || { cat "$OUT"; exit 81; }
[ ! -f "$SD/stop-mark.md" ] && [ ! -d "$SD/inflight" ] || { cat "$OUT"; exit 83; }
[ -s "$SD/last-verified.json" ] || exit 84
run_loop term-removal -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$(calls)" = 'pr-a pr-b pr-c' ] || { cat "$OUT"; exit 86; }
[ ! -f "$SD/stop-mark.md" ] && [ ! -d "$SD/inflight" ] || { cat "$OUT"; exit 87; }
""", 0, transform)

    # シグナルごとに 3 つのテストへ分ける(1 つで 9 通りを流すと約 5 分かかり、
    # 並べて流すときに全体の時間を決めてしまうため。確かめる 9 通りは同じ)。
    def test_all_signals_defer_every_own_removal_promotion_window_term(self):
        self.assert_signal_defers_every_own_removal_promotion_window("TERM", 143)

    def test_all_signals_defer_every_own_removal_promotion_window_hup(self):
        self.assert_signal_defers_every_own_removal_promotion_window("HUP", 129)

    def test_all_signals_defer_every_own_removal_promotion_window_int(self):
        self.assert_signal_defers_every_own_removal_promotion_window("INT", 130)

    def assert_signal_defers_every_own_removal_promotion_window(self, signal, code):
        points = {
            "removed-before-record": '    rep "- worktree: 消した"\n',
            "recorded-before-save": '    REMOVED_PARENT_WORKTREES+=("$ITER_WT")\n',
            "saved-before-cleanup": '  REMOVED_PARENT_WORKTREES=()\n',
        }
        for phase, point in points.items():
            with self.subTest(signal=signal, phase=phase):
                def transform(source, point=point, signal=signal):
                    self.assertEqual(source.count(point), 1)
                    injected = (point + '  [ "${ITER_COUNT:-}" != 2 ] || kill -'
                                + signal + ' "$$"\n')
                    return source.replace(point, injected)
                body = """
newrepo deferred-promotion
addtask pr-a 2026-01-01
addtask pr-b 2026-01-02
addtask pr-c 2026-01-03
commit
newrec deferred-promotion
run_loop deferred-promotion -- --repo "$R" --max-iterations 3 "${COMMON_ARGS[@]}"
[ "$RC" = @CODE@ ] || { cat "$OUT"; exit 101; }
SD="$(state_dir deferred-promotion)"
[ "$(calls)" = 'pr-a pr-b' ] || { cat "$OUT"; exit 102; }
[ ! -e "$SD/stop-mark.md" ] && [ ! -d "$SD/inflight" ] || { cat "$OUT"; exit 103; }
[ -s "$SD/last-verified.json" ] || exit 104
run_loop deferred-promotion -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$(calls)" = 'pr-a pr-b pr-c' ] || { cat "$OUT"; exit 105; }
""".replace("@CODE@", str(code))
                self.run_case(body, 0, transform)

    def test_term_during_next_child_does_not_reuse_a_prior_verified_state(self):
        # The first task establishes ITER_VERIFIED.  TERM while the second
        # child is live must discard that old record, clean this child, and
        # verify this iteration's base rather than attempt a stale promotion.
        self.run_case("""
newrepo term-next-child
addtask sync-a 2026-01-01
addtask longsleep-b 2026-01-02
addtask sync-c 2026-01-03
commit
newrec term-next-child
start_bg term-next-child -- --repo "$R" --max-iterations 3 "${COMMON_ARGS[@]}"
wait_started "$REC/started-longsleep-b" || { cat "$BG_OUT"; exit 91; }
CHILD="$(cat "$REC/pid-longsleep-b")"
kill -TERM "$BG_PID"
wait_bg
[ "$BG_RC" = 143 ] || { cat "$BG_OUT"; exit 92; }
wait_dead "$CHILD" 5 || exit 93
SD="$(state_dir term-next-child)"
[ ! -e "$SD/stop-mark.md" ] || { cat "$OUT"; exit 94; }
[ ! -d "$SD/inflight" ] || { cat "$OUT"; exit 95; }
[ "$(calls)" = 'sync-a longsleep-b' ] || { cat "$OUT"; exit 96; }
run_loop term-next-child -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$(calls)" = 'sync-a longsleep-b sync-c' ] || { cat "$OUT"; exit 97; }
""", 0)

    def test_wait_started_tells_an_early_exit_from_a_timeout(self):
        # 背景の loop.sh の代わりに、渡したコマンドを実行するだけの偽物を start_bg で起動する。
        out = self.run_case('''
printf 'exec "$@"\\n' >"$W/fake-loop.sh"
LOOP_BIN="$W/fake-loop.sh"
check_wait() { # $1=名前 → 返り値・かかった秒数・標準エラーの 1 行目
  local start=$SECONDS rc
  wait_started "$W/mark-$1" 2>"$W/err-$1"; rc=$?
  printf 'WAIT %s rc=%s sec=%s %s\\n' "$1" "$rc" "$((SECONDS - start))" "$(head -n 1 "$W/err-$1")"
}
start_bg fake -- bash -c 'exit 3'
check_wait early-exit
start_bg fake -- touch "$W/mark-mark-then-exit"
check_wait mark-then-exit
wait_bg
''' + wait_started_function(1) + '''
start_bg fake -- sleep 60
check_wait alive
kill "$BG_PID"
wait_bg
OUT=/dev/null
RC=0
''', 0, proof=False)
        early = re.search(r'^WAIT early-exit rc=1 sec=([0-9]+) (.*)$', out, re.M)
        self.assertIsNotNone(early, out)
        self.assertEqual(early.group(2), EARLY_EXIT_MARK + 'mark-early-exit の前に終わった(終了コード 3)', out)
        # 待ち切らずに返る(既定の 100 秒の半分より十分に短い)。
        self.assertLess(int(early.group(1)), START_WAIT // 2, out)
        self.assertRegex(out, r'(?m)^WAIT mark-then-exit rc=0 sec=[0-9]+ $')
        self.assertRegex(out, r'(?m)^WAIT alive rc=1 sec=[0-9]+ ' + re.escape(
            TIMEOUT_MARK + '起動の印 mark-alive が 1 秒待っても現れない。混んだ機械では流し直す') + '$')

    def test_normal_two_iterations_keep_reports(self):
        self.run_case('''
newrepo normal
addtask sync-a 2026-01-01
addtask sync-b 2026-01-02
commit
newrec normal
run_loop normal -- --repo "$R" --max-iterations 2 "${COMMON_ARGS[@]}"
[ -f "$(report_of "$OUT")" ] || exit 74
[ "$(calls)" = 'sync-a sync-b' ] || { cat "$OUT"; exit 75; }
''', 0)

    def test_untracked_normal_failed_remove_keeps_index_and_continues(self):
        # A normal PR may leave an untracked file, so worktree remove correctly
        # fails and the checkout remains locked.  Parent-only status/judge
        # reads must not refresh that retained index's metadata: promotion
        # still needs a strict state comparison before the next normal task.
        def transform(source):
            before = '  G -C "$TOP" worktree unlock "$ITER_WT" >/dev/null 2>&1 || true\n'
            self.assertEqual(source.count(before), 1)
            capture_before = ('  if [ "$ITER_NAME" = untracked-a ]; then\n'
                              '    _selftest_index="$(G -C "$ITER_WT" rev-parse --path-format=absolute --git-dir)/index"\n'
                              '    sha256sum <"$_selftest_index" | cut -d\' \' -f1 >"$SELFTEST_INDEX_CAPTURE/before"\n'
                              '  fi\n')
            source = source.replace(before, capture_before + before)
            failed = '  else\n    G -C "$TOP" worktree lock --reason "dev-workflow-loop: $ITER_REL" "$ITER_WT" >/dev/null 2>&1 || true\n'
            self.assertEqual(source.count(failed), 1)
            capture_after = ('  else\n'
                             '    if [ "$ITER_NAME" = untracked-a ]; then\n'
                             '      sha256sum <"$_selftest_index" | cut -d\' \' -f1 >"$SELFTEST_INDEX_CAPTURE/after"\n'
                             '    fi\n')
            return source.replace(failed, capture_after + failed[len('  else\n'):])
        self.run_case('''
newrepo untracked-normal
addtask untracked-a 2026-01-01
addtask sync-b 2026-01-02
commit
newrec untracked-normal
run_loop untracked-normal "SELFTEST_INDEX_CAPTURE=$REC" -- --repo "$R" --max-iterations 2 "${COMMON_ARGS[@]}"
[ "$(calls)" = 'untracked-a sync-b' ] || { cat "$OUT"; exit 121; }
cmp -s "$REC/before" "$REC/after" || { cat "$OUT"; exit 122; }
git -C "$R" worktree list --porcelain -z | tr '\\0' '\\n' | grep -qx 'locked dev-workflow-loop: docs/tasks/進行中_untracked-a.md' || { cat "$OUT"; exit 123; }
''', 0, transform)


    # ── 1 回の起動で 2 回照合する(verify_environment_pair)の、周の中での振る舞い ──
    def aux_action(self, action):
        """aux() の先頭に action(有効 plugin の一覧の確定後だけ)を差し込む変換。"""
        def transform(source):
            point = 'aux() { # $1=stdout のファイル 残り=補助の CLI の引数(stderr は <stdout のファイル>.err)\n  local out="$1"\n'
            self.assertEqual(source.count(point), 1)
            # 最初のスタブの証明(--prove-host の起動)には効かせない: 試験の run_loop が SELFTEST_AUX_ACTION=1 を渡したときだけ動かす
            return source.replace(point, point + '  if [ "${ENV_INVENTORY_READY:-0}" -eq 1 ] && [ "${SELFTEST_AUX_ACTION:-}" = 1 ]; then\n' + action + '\n  fi\n')
        return transform

    def test_guard_body_rewritten_during_the_helper_cli_stops_before_any_child(self):
        action = ('    if [ ! -e "$ENV_TEMP/mutated" ]; then : >"$ENV_TEMP/mutated"; '
                  'printf "\\n# rewritten while the helper CLI runs\\n" >>"$TRUSTED_ENV_GUARD"; fi')
        out = self.run_case("""
newrepo pair-guard-body
addtask sync-a 2026-01-01
commit
newrec pair-guard-body
run_loop pair-guard-body "SELFTEST_AUX_ACTION=1" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$(calls)" = "" ] || { cat "$OUT"; exit 131; }
""", 20, self.aux_action(action))
        self.assertIn('environment', out)

    def test_plugin_copy_rewritten_during_the_helper_cli_stops_before_any_child(self):
        action = ('    if [ ! -e "$ENV_TEMP/mutated" ]; then : >"$ENV_TEMP/mutated"; '
                  'printf "\\n# rewritten while the helper CLI runs\\n" >>"$PLUGIN_ROOT/skills/ship-task/scripts/loop-state.py"; fi')
        self.run_case("""
newrepo pair-copy
addtask sync-a 2026-01-01
commit
newrec pair-copy
run_loop pair-copy "SELFTEST_AUX_ACTION=1" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$(calls)" = "" ] || { cat "$OUT"; exit 132; }
""", 20, self.aux_action(action))

    def test_guard_body_rewritten_during_the_helper_cli_in_finish_is_environment_changed(self):
        def transform(source):
            point = '  local code="$1" why="$2" n\n  trap - TERM HUP INT\n'
            self.assertEqual(source.count(point), 1)
            source = source.replace(point, point + '  SELFTEST_IN_FINISH=1\n')
            return self.aux_action(
                '    if [ "${SELFTEST_IN_FINISH:-}" = 1 ] && [ ! -e "$ENV_TEMP/mutated" ]; then : >"$ENV_TEMP/mutated"; '
                'printf "\\n# rewritten while the helper CLI runs\\n" >>"$TRUSTED_ENV_GUARD"; fi')(source)
        out = self.run_case("""
newrepo pair-finish
addtask sync-a 2026-01-01
commit
newrec pair-finish
run_loop pair-finish "SELFTEST_AUX_ACTION=1" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
""", 20, transform)
        self.assertIn('environment-changed', out)

    def test_killing_the_guard_midway_stops_for_a_fixed_reason_not_sigpipe(self):
        action = ('    if [ ! -e "$ENV_TEMP/killed" ]; then : >"$ENV_TEMP/killed"; '
                  'pkill -KILL -P "$pair_pid" || true; kill -KILL "$pair_pid" 2>/dev/null || true; sleep 0.3; fi')
        out = self.run_case("""
newrepo pair-kill
addtask sync-a 2026-01-01
commit
newrec pair-kill
run_loop pair-kill "SELFTEST_AUX_ACTION=1" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$(calls)" = "" ] || { cat "$OUT"; exit 133; }
""", 20, self.aux_action(action))
        self.assertIn('environment', out)

    def test_term_to_loop_sh_only_during_the_helper_cli_in_promotion_is_deferred(self):
        action = ('    if [ "$PROMOTION_CRITICAL" = 1 ] && [ "${ITER_COUNT:-}" = 2 ] && [ ! -e "$ENV_TEMP/term-sent" ]; then '
                  ': >"$ENV_TEMP/term-sent"; kill -TERM "$$"; fi')
        self.run_case("""
newrepo pair-term
addtask pr-a 2026-01-01
addtask pr-b 2026-01-02
addtask pr-c 2026-01-03
commit
newrec pair-term
run_loop pair-term "SELFTEST_AUX_ACTION=1" -- --repo "$R" --max-iterations 3 "${COMMON_ARGS[@]}"
[ "$RC" = 143 ] || { cat "$OUT"; exit 134; }
SD="$(state_dir pair-term)"
[ "$(calls)" = 'pr-a pr-b' ] || { cat "$OUT"; exit 135; }
[ ! -e "$SD/stop-mark.md" ] && [ ! -d "$SD/inflight" ] || { cat "$OUT"; exit 136; }
[ -s "$SD/last-verified.json" ] || exit 137
run_loop pair-term -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$(calls)" = 'pr-a pr-b pr-c' ] || { cat "$OUT"; exit 138; }
""", 0, self.aux_action(action))

    def test_helper_cli_failure_keeps_the_same_stderr_and_stops(self):
        def transform(source):
            point = '  if ! host_same; then\n    echo "ERROR [host-proof] 補助の CLI を打つ直前に'
            self.assertEqual(source.count(point), 1)
            return source.replace(point, '  if { [ "${ENV_INVENTORY_READY:-0}" -eq 1 ] && [ "${SELFTEST_AUX_ACTION:-}" = 1 ]; } || ! host_same; then\n    echo "ERROR [host-proof] 補助の CLI を打つ直前に')
        out = self.run_case("""
newrepo pair-aux-fail
addtask sync-a 2026-01-01
commit
newrec pair-aux-fail
run_loop pair-aux-fail "SELFTEST_AUX_ACTION=1" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ "$(calls)" = "" ] || { cat "$OUT"; exit 139; }
""", 20, transform)
        self.assertIn('ERROR [host-proof] 補助の CLI を打つ直前に、ホスト CLI の実体が控えた時と違う', out)
        self.assertNotIn('Broken pipe', out)
        self.assertNotIn('execute_coproc', out)

    # ── 隣り合う digest の組(state_digest_pair)──
    def digest_pair_case(self, label, expected_message, point, action):
        def transform(source):
            self.assertEqual(source.count(point), 1)
            return source.replace(point, '  if [ "$ITER_COUNT" = 1 ]; then\n' + action + '\n  fi\n' + point)
        self.run_case("""
newrepo @LABEL@
addtask pr-a 2026-01-01
addtask pr-b 2026-01-02
commit
newrec @LABEL@
run_loop @LABEL@ "SELFTEST_SECRET=$SENTINEL" -- --repo "$R" --max-iterations 2 "${COMMON_ARGS[@]}"
SD="$(state_dir @LABEL@)"
[ "$RC" = 10 ] || { cat "$OUT"; exit 141; }
[ "$(calls)" = pr-a ] || { cat "$OUT"; exit 142; }
[ -f "$SD/stop-mark.md" ] || { cat "$OUT"; exit 143; }
grep -rqF -- '@MESSAGE@' "$SD" || { cat "$OUT"; exit 144; }
""".replace('@LABEL@', label).replace('@MESSAGE@', expected_message), 10, transform)

    PAIR_FIRST = '  state_digest_pair "$expected" "$verified" || true\n'
    PAIR_SECOND = '  state_digest_pair "$observed" "$verified" || true\n'

    def test_each_digest_of_the_first_pair_replaced_by_a_link_stops_with_its_own_wording(self):
        self.digest_pair_case('pair1-expected', '検証済みの期待状態を保持できない', self.PAIR_FIRST,
                              '    rm -f -- "$expected"\n    ln -s -- "$SELFTEST_SECRET" "$expected"')
        self.digest_pair_case('pair1-verified', '検証済みの状態が基準の作成中に書き換わった', self.PAIR_FIRST,
                              '    rm -f -- "$verified"\n    ln -s -- "$SELFTEST_SECRET" "$verified"')

    def test_each_digest_of_the_second_pair_replaced_by_a_link_stops_with_its_own_wording(self):
        self.digest_pair_case('pair2-observed', '最終観察の状態を保持できない', self.PAIR_SECOND,
                              '    rm -f -- "$observed"\n    ln -s -- "$SELFTEST_SECRET" "$observed"')
        self.digest_pair_case('pair2-verified', '検証済みの状態が最終観察中に書き換わった', self.PAIR_SECOND,
                              '    rm -f -- "$verified"\n    ln -s -- "$SELFTEST_SECRET" "$verified"')

    def test_unreadable_digest_files_stop_with_the_same_wording_as_links(self):
        if os.geteuid() == 0:
            self.skipTest('root では権限 000 でも読めるので確かめられない')
        self.digest_pair_case('pair1-expected-denied', '検証済みの期待状態を保持できない', self.PAIR_FIRST,
                              '    chmod 000 "$expected"')
        self.digest_pair_case('pair1-verified-denied', '検証済みの状態が基準の作成中に書き換わった', self.PAIR_FIRST,
                              '    chmod 000 "$verified"')
        self.digest_pair_case('pair2-observed-denied', '最終観察の状態を保持できない', self.PAIR_SECOND,
                              '    chmod 000 "$observed"')
        self.digest_pair_case('pair2-verified-denied', '検証済みの状態が最終観察中に書き換わった', self.PAIR_SECOND,
                              '    chmod 000 "$verified"')

    def test_rewriting_loop_state_copy_before_the_pair_digest_is_stopped_by_the_check(self):
        for label, point in (('pair1-code', self.PAIR_FIRST), ('pair2-code', self.PAIR_SECOND)):
            with self.subTest(pair=label):
                def transform(source, point=point):
                    self.assertEqual(source.count(point), 1)
                    action = """  if [ "$ITER_COUNT" = 1 ]; then
  cat >"$LOOP_STATE_PY" <<'PY_DIGEST'
import os
with open(os.environ['SELFTEST_ATTACK_MARKER'], 'w') as stream:
    stream.write('executed')
print('0' * 64)
PY_DIGEST
  fi
"""
                    return source.replace(point, action + point)
                self.run_case("""
newrepo @LABEL@
addtask pr-a 2026-01-01
commit
newrec @LABEL@
run_loop @LABEL@ "SELFTEST_ATTACK_MARKER=$REC/digest-executed" -- --repo "$R" --max-iterations 1 "${COMMON_ARGS[@]}"
[ ! -e "$REC/digest-executed" ] || { cat "$OUT"; exit 145; }
[ "$RC" = 10 ] || { cat "$OUT"; exit 146; }
grep -qF 'ERROR [environment-guard] 信頼コピーが変わった' "$OUT" || { cat "$OUT"; exit 147; }
""".replace('@LABEL@', label), 10, transform)


class StateDigestPairTests(unittest.TestCase):
    """state_digest_pair が、補助の出力と終了コードの組み合わせのうち、使える組だけを使う。"""
    HEX_A, HEX_B = 'a' * 64, 'b' * 64

    @classmethod
    def setUpClass(cls):
        text = (ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/loop.sh').read_text()
        cls.function = 'state_digest_pair() {' + text.split('state_digest_pair() {', 1)[1].split('\n}\n', 1)[0] + '\n}\n'
        cls.tmp = tempfile.TemporaryDirectory(prefix='state-digest-pair-')
        cls.helper = Path(cls.tmp.name) / 'fake-loop-state.py'
        cls.helper.write_text('import os, sys\n'
                              'open(os.environ["CALLS"], "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
                              'sys.stdout.write(os.environ["FAKE_OUT"].replace("|", "\\n"))\n'
                              'sys.exit(int(os.environ["FAKE_RC"]))\n')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_pair(self, output, rc, verify_ok=True):
        calls = Path(self.tmp.name) / 'calls'
        calls.unlink(missing_ok=True)
        shell = ('set -euo pipefail\n'
                 f'PY_ABS={shlex.quote(sys.executable)}\nLOOP_STATE_PY={shlex.quote(str(self.helper))}\n'
                 'STATE_MAX_ITEMS=1 STATE_MAX_BYTES=2 STATE_MAX_FILE_BYTES=3 STATE_MAX_SECONDS=4\nSTATE_GIT_UNSAFE=0\n'
                 + ('verify_environment() { return 0; }\n' if verify_ok else 'verify_environment() { STATE_GIT_UNSAFE=1; return 1; }\n')
                 + self.function +
                 'rc=0; state_digest_pair /x/one /x/two || rc=$?\n'
                 'printf "rc=%s [%s] [%s] unsafe=%s" "$rc" "$DIGEST1" "$DIGEST2" "$STATE_GIT_UNSAFE"\n')
        got = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30,
                             env={**os.environ, 'FAKE_OUT': output, 'FAKE_RC': str(rc), 'CALLS': str(calls)})
        self.assertEqual(got.returncode, 0, got.stderr)
        started = calls.read_text().splitlines() if calls.exists() else []
        return got.stdout, started

    def test_usable_outputs(self):
        a, b = self.HEX_A, self.HEX_B
        for output, rc, want in ((f'{a}|{b}', 0, f'rc=0 [{a}] [{b}] unsafe=0'),
                                 (f'-|{b}', 20, f'rc=0 [] [{b}] unsafe=0'),
                                 (f'{a}|-', 20, f'rc=0 [{a}] [] unsafe=0'),
                                 (f'-|-', 20, 'rc=0 [] [] unsafe=0'),
                                 (f'{a}|{b}|', 0, f'rc=0 [{a}] [{b}] unsafe=0')):
            with self.subTest(output=output, rc=rc):
                got, calls = self.run_pair(output, rc)
                self.assertEqual(got, want)
                # 1 回の起動だけ(読めなかった方を個別の起動でやり直さない)。上限の引数は渡る
                self.assertEqual(calls, ['state-digests --state /x/one --state /x/two --max-items 1 --max-bytes 2 '
                                         '--max-file-bytes 3 --max-seconds 4'])

    def test_unusable_outputs_leave_both_empty(self):
        a, b = self.HEX_A, self.HEX_B
        cases = ((f'{a}|{b}', 1), (f'{a}|{b}', 20), (f'{a}|{b}', 9), (f'{a}|{b}', 2), (f'-|{b}', 0), (f'{a}|-', 0), ('-|-', 0),
                 (f'-|{b}', 1), (f'{a}', 0), (f'{a}|{b}|{a}', 0), ('', 0), ('', 20), (f'{a}|{b[:63]}', 0),
                 (f'{a}|{b.upper()}', 0), (f'{a}|zz', 0), (f'{a}| {b}', 0), (f'{a}||{b}', 0), (f'-|x', 20), (f'x|-', 20),
                 (f'{a}|{b}||', 0), (f'{a}|{b}|||', 0), (f'{a}|{b}||', 20), (f'-|{b}||', 20))
        for output, rc in cases:
            with self.subTest(output=output, rc=rc):
                got, calls = self.run_pair(output, rc)
                self.assertRegex(got, r'^rc=1 \[\] \[\] unsafe=0$')
                self.assertEqual(len(calls), 1)

    def test_environment_failure_runs_no_helper(self):
        got, calls = self.run_pair(f'{self.HEX_A}|{self.HEX_B}', 0, verify_ok=False)
        self.assertEqual((got, calls), ('rc=1 [] [] unsafe=1', []))


class PairShellVersionTests(unittest.TestCase):
    """verify_environment_pair を、本物の関数の字面のまま、偽の guard と偽の補助 CLI で動かす小さな確かめ。

    host の bash に加えて、環境変数 SHELL_REQUIREMENTS_BASH_4_4・SHELL_REQUIREMENTS_BASH_5_2 に絶対パスがあれば、
    その bash でも流す(coproc の fd がサブシェルで閉じること・スカラーの変数へ写した fd を閉じ切る形・wait の終了コード)。"""
    HARNESS = r"""
set -eEuo pipefail
shopt -s inherit_errexit
ENVIRONMENT_STATE=state ENVIRONMENT_SHA=held ENV_GUARD_SHA=guard NET_TIMEOUT=10
AUX_PLUGINS=(plugin list)
LOCK_FD_TEST=1
environment_call() { # 偽の guard: verify-pair の約束(verified → go を待つ → 終了コード)だけを真似る
  local line
  case "${FAKE_GUARD:-ok}" in
    first-fails) exit 20 ;;
    sigpipe-bait) printf 'verified\n'; exit 20 ;;
    killed) printf 'verified\n'; kill -KILL "$BASHPID" ;;
  esac
  printf 'verified\n'
  IFS= read -r line || exit 20
  [ "$line" = go ] || { echo "guard got $line" >>"$LOG"; exit 20; }
  echo "guard got go" >>"$LOG"
  exit "${FAKE_GUARD_RC:-0}"
}
aux() { # 偽の補助 CLI: 開いている fd を記録する
  ls -1 "/proc/$BASHPID/fd" >"$LOG.fds"
  case "${FAKE_AUX:-ok}" in
    ok) return 0 ;;
    fail) return 1 ;;
    slow) sleep 0.4; return 0 ;;
  esac
}
@FUNCTION@
rc=0
( verify_environment_pair /tmp/inventory-unused ) || rc=$?
echo "rc=$rc"
"""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/loop.sh').read_text()
        function = 'verify_environment_pair() {' + text.split('verify_environment_pair() {', 1)[1].split('\n}\n', 1)[0] + '\n}\n'
        cls.script = cls.HARNESS.replace('@FUNCTION@', function)
        cls.bashes = [shutil.which('bash')]
        cls.missing = []
        for name in ('SHELL_REQUIREMENTS_BASH_4_4', 'SHELL_REQUIREMENTS_BASH_5_2'):
            if os.environ.get(name):
                cls.bashes.append(os.environ[name])
            else:
                cls.missing.append(name)

    def test_old_bash_versions_are_checked_when_their_paths_are_given(self):
        # host の bash の確かめは、ほかのテストが常に流す。実体の無い版だけを、理由つきで skip する(成功扱いにしない)
        for name in ('SHELL_REQUIREMENTS_BASH_4_4', 'SHELL_REQUIREMENTS_BASH_5_2'):
            with self.subTest(version=name):
                if name in self.missing:
                    self.skipTest('環境変数 ' + name + ' に bash の実体の絶対パスが無い。この版は未確認')
                result, log, fds = self.run_pair(os.environ[name])
                self.assertEqual((result.stdout.strip(), result.stderr), ('rc=0', ''))

    def run_pair(self, bash, **env):
        with tempfile.TemporaryDirectory(prefix='pair-shell-') as tmp:
            log = Path(tmp) / 'log'
            result = subprocess.run([bash, '-c', self.script], capture_output=True, text=True, timeout=60,
                                    env={**os.environ, 'LOG': str(log), **env})
            fds = (Path(str(log) + '.fds').read_text().split() if Path(str(log) + '.fds').exists() else None)
            return result, (log.read_text() if log.exists() else ''), fds

    def test_success_passes_no_extra_fd_to_the_helper_cli(self):
        for bash in self.bashes:
            with self.subTest(bash=bash):
                result, log, fds = self.run_pair(bash)
                self.assertEqual((result.stdout.strip(), result.stderr), ('rc=0', ''), bash)
                self.assertIn('guard got go', log)
                self.assertIsNotNone(fds)
                self.assertEqual(sorted(set(fds) - {'0', '1', '2', '255'}), [], (bash, fds))

    def test_failures_all_return_1_and_the_guard_gets_stop_when_the_helper_cli_fails(self):
        cases = (({'FAKE_AUX': 'fail'}, 'guard got stop'),
                 ({'FAKE_GUARD_RC': '20'}, 'guard got go'),
                 ({'FAKE_GUARD_RC': '3'}, 'guard got go'),
                 ({'FAKE_GUARD': 'first-fails'}, ''))
        for bash in self.bashes:
            for env, logged in cases:
                with self.subTest(bash=bash, env=env):
                    result, log, fds = self.run_pair(bash, **env)
                    self.assertEqual((result.stdout.strip(), result.stderr), ('rc=1', ''), (bash, result.stderr))
                    self.assertIn(logged, log)

    def test_a_guard_that_is_gone_does_not_stop_the_parent_with_sigpipe(self):
        for bash in self.bashes:
            with self.subTest(bash=bash):
                result, log, fds = self.run_pair(bash, FAKE_GUARD='sigpipe-bait', FAKE_AUX='slow')
                self.assertEqual(result.stdout.strip(), 'rc=1', (bash, result.stderr))
                self.assertNotIn('Broken pipe', result.stderr)
                self.assertNotEqual(result.returncode, 141)
                result, log, fds = self.run_pair(bash, FAKE_GUARD='killed', FAKE_AUX='slow')
                self.assertEqual(result.stdout.strip(), 'rc=1', (bash, result.stderr))
                self.assertNotIn('Broken pipe', result.stderr)


if __name__ == '__main__':
    unittest.main()
