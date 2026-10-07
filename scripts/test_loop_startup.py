"""起動前の管理入口と、終了・再開時の設定先読みに対する回帰。"""
import ctypes
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/loop-startup.py'
spec = importlib.util.spec_from_file_location('loop_startup_tested', SOURCE)
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


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
        cls.tmp = tempfile.TemporaryDirectory(prefix='loop-startup-shell-')
        cls.root = Path(cls.tmp.name)
        # 同時作業中の source を試験途中に読み直さない。
        cls.plugin = cls.root / 'plugin'
        shutil.copytree(ROOT / 'plugins/dev-workflow', cls.plugin)
        scripts = cls.plugin / 'skills/ship-task/scripts'
        text = (scripts / 'loop-selftest.sh').read_text()
        cls.prefix = text.split('# LOOP_SELFTEST_ONLY=', 1)[0]
        line = next(line for line in cls.prefix.splitlines() if line.startswith('SCRIPT_DIR='))
        cls.prefix = cls.prefix.replace(line, 'SCRIPT_DIR=' + repr(str(scripts)))
        cls.prefix = cls.prefix.replace('g config selftest.added yes;', '''printf '\\n[include]\\n\\tpath = %s\\n' "$SELFTEST_SECRET" >>"$SELFTEST_COMMON/config";''')
        # 同じ物理 cwd からの起動でも早期 local-env-vars 照会を検査する。
        cls.prefix = cls.prefix.replace('( cd "$W/cwd" && exec env', '( cd "${STARTUP_CWD:-$W/cwd}" && exec env')
    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
    def run_case(self, body, expected, transform=None):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            tmp = Path(tmp)
            sentinel = tmp / 'secret-config'
            sentinel.write_text('[test]\n value = harmless\n')
            watch = Watch(sentinel)
            try:
                prefix = self.prefix
                if transform is not None:
                    plugin = tmp / 'instrumented-plugin'
                    shutil.copytree(self.plugin, plugin)
                    loop = plugin / 'skills/ship-task/scripts/loop.sh'
                    loop.write_text(transform(loop.read_text()))
                    prefix = prefix.replace(repr(str(self.plugin / 'skills/ship-task/scripts')),
                                            repr(str(plugin / 'skills/ship-task/scripts')))
                script = tmp / 'case.sh'
                script.write_text(prefix + '\n' + body + '\ncat "$OUT"\nprintf "\\nCASE_RC=%s\\n" "$RC"\nexit 0\n')
                env = {**os.environ, 'SENTINEL': str(sentinel), 'LOOP_SELFTEST_TIMEOUT': '100'}
                result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('CASE_RC=' + str(expected), result.stdout, result.stdout + result.stderr)
                self.assertEqual(watch.events(), b'', result.stdout + result.stderr)
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
wait_file "$REC/started-longsleep-a" 35 || { cat "$BG_OUT"; exit 72; }
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
wait_file "$REC/started-longsleep-a" 35 || { cat "$BG_OUT"; exit 72; }
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
wait_file "$REC/started-longsleep-a" 35 || { cat "$BG_OUT"; exit 72; }
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

    def test_all_signals_defer_every_own_removal_promotion_window(self):
        codes = {"TERM": 143, "HUP": 129, "INT": 130}
        points = {
            "removed-before-record": '    rep "- worktree: 消した"\n',
            "recorded-before-save": '    REMOVED_PARENT_WORKTREES+=("$ITER_WT")\n',
            "saved-before-cleanup": '  REMOVED_PARENT_WORKTREES=()\n',
        }
        for signal, code in codes.items():
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
wait_file "$REC/started-longsleep-b" 35 || { cat "$BG_OUT"; exit 91; }
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


if __name__ == '__main__':
    unittest.main()
