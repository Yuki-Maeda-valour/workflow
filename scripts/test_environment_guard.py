import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

SCRIPT = Path(__file__).resolve().parents[1] / 'plugins/dev-workflow/skills/ship-task/scripts/environment-guard.py'

class EnvironmentGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'plugin'
        (self.root / 'skills/x/scripts').mkdir(parents=True)
        (self.root / '.claude-plugin').mkdir()
        (self.root / '.claude-plugin/plugin.json').write_text('{}')
        self.file = self.root / 'skills/x/scripts/helper.py'
        self.file.write_text('print("normal")\n')
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'), GIT_CONFIG_NOSYSTEM='1')
        for k in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'CLAUDE_CONFIG_DIR', 'ZDOTDIR', 'BASH_ENV', 'ENV'):
            self.env.pop(k, None)
        self.bundle = self.base / 'bundle'
    def run_guard(self, *args):
        return subprocess.run([sys.executable, '-B', str(SCRIPT), *map(str,args)], text=True, capture_output=True, env=self.env, timeout=10)
    def boot(self, *args):
        p=self.run_guard('bootstrap','--root',self.root,'--output',self.bundle,*args)
        self.assertEqual(p.returncode,0,p.stderr)
        self.receipt=json.loads(p.stdout)
        return self.receipt
    def verify(self, *args):
        return self.run_guard('verify','--state',self.bundle/'environment.json','--expect-sha256',self.receipt['sha256'],*args)
    def test_normal_and_copy_exec(self):
        self.boot()
        self.assertEqual(self.verify().returncode,0)
        p=self.run_guard('exec','--state',self.bundle/'environment.json','--expect-sha256',self.receipt['sha256'],'--path','skills/x/scripts/helper.py','--','python3')
        self.assertEqual((p.returncode,p.stdout),(0,'normal\n'),p.stderr)
    def test_source_fixed_output_and_skill_change(self):
        self.boot(); self.file.write_text('print("approved")\n')
        self.assertEqual(self.verify().returncode,20)
    def test_copy_modified(self):
        self.boot(); (self.bundle/'plugin/skills/x/scripts/helper.py').write_text('print("forged")')
        self.assertEqual(self.verify().returncode,20)
    def test_manifest_and_hash_forgery(self):
        self.boot(); p=self.bundle/'environment.json'; obj=json.loads(p.read_text()); obj['entries']={}; p.write_text(json.dumps(obj))
        self.assertEqual(self.verify().returncode,20)
    def test_new_and_removed_empty_directories(self):
        self.boot(); (self.root/'skills/new').mkdir()
        self.assertEqual(self.verify().returncode,20)
    def test_missing_then_added_user_setting(self):
        self.boot(); d=self.home/'.claude'; d.mkdir(); (d/'settings.json').write_text('{}')
        self.assertEqual(self.verify().returncode,20)
    def test_quoted_continued_include_normal_and_change(self):
        inc=self.home/'included file'; inc.write_text('[user]\nname=normal\n')
        (self.home/'.gitconfig').write_text('[include]\npath = "included \\\nfile"\n')
        self.boot(); self.assertEqual(self.verify().returncode,0)
        inc.write_text('[user]\nname=changed\n'); self.assertEqual(self.verify().returncode,20)
    def test_special_file_and_parent_symlink_fail_without_read(self):
        self.file.unlink(); os.mkfifo(self.file)
        self.assertEqual(self.run_guard('bootstrap','--root',self.root,'--output',self.bundle).returncode,20)
        self.file.unlink(); self.file.write_text('safe')
        original=self.root/'skills/x'; original.rename(self.base/'external'); original.symlink_to(self.base/'external',target_is_directory=True)
        self.assertEqual(self.run_guard('bootstrap','--root',self.root,'--output',self.bundle).returncode,20)
    def test_state_fifo_no_block(self):
        self.boot(); p=self.bundle/'environment.json'; p.unlink(); os.mkfifo(p)
        self.assertEqual(self.verify().returncode,20)
    def test_reused_output_never_executes_previous_guard(self):
        self.bundle.mkdir(); (self.bundle/'environment-guard.py').write_text('raise SystemExit(0)')
        self.assertEqual(self.run_guard('bootstrap','--root',self.root,'--output',self.bundle).returncode,20)
    def test_component_full_stat_rejects_restored_content(self):
        self.boot(); old=self.file.read_bytes(); self.file.write_text('changed'); self.file.write_bytes(old)
        self.assertEqual(self.verify().returncode,20)

if __name__ == '__main__': unittest.main()

class EnvironmentGuardMoreTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    run_guard = EnvironmentGuardTests.run_guard
    boot = EnvironmentGuardTests.boot
    verify = EnvironmentGuardTests.verify
    def module(self):
        spec=importlib.util.spec_from_file_location('environment_guard', SCRIPT)
        mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod
    def test_read_components_returns_held_bytes_and_stable_policy_digest(self):
        skill = self.home / '.claude/skills/one'; skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: one\n---\nbody')
        receipt = self.boot(); mod = self.module()
        got = mod.read_components(self.bundle / 'environment.json', receipt['sha256'])
        self.assertTrue(any(x['kind'] == 'personal-skill' and x['raw'].endswith(b'body') for x in got))
        digest = mod.component_policy_digest(self.bundle / 'environment.json', receipt['sha256'])
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        (skill / 'SKILL.md').write_text('changed')
        with self.assertRaises(mod.Stop): mod.read_components(self.bundle / 'environment.json', receipt['sha256'])

    def test_component_tree_follows_multihop_directory_link_and_rejects_cycle(self):
        mod=self.module(); skills=self.home/'.claude/skills'; skills.mkdir(parents=True)
        final=self.base/'final'; final.mkdir(); (final/'SKILL.md').write_text('ok')
        middle=self.base/'middle'; middle.symlink_to(final, target_is_directory=True)
        (skills/'linked').symlink_to(middle, target_is_directory=True)
        entries={}; mod.component_tree(skills, 'personal-skill', mod.Budget(), entries)
        self.assertIn('component:personal-skill:'+str(skills)+'/linked/SKILL.md', entries)
        (skills/'cycle').symlink_to('cycle', target_is_directory=True)
        with self.assertRaises(mod.Stop): mod.component_tree(skills, 'personal-skill', mod.Budget(), {})

    def test_component_tree_allows_skill_install_link_but_not_agent_link(self):
        mod = self.module(); entries = {}; budget = mod.Budget()
        outside = self.base / 'outside'; (outside / 'SKILL.md').parent.mkdir(parents=True)
        (outside / 'SKILL.md').write_text('safe')
        skills = self.home / '.claude/skills'; skills.mkdir(parents=True)
        os.symlink(outside, skills / 'installed', target_is_directory=True)
        mod.component_tree(skills, 'personal-skill', budget, entries)
        self.assertIn('@link:component:personal-skill:' + str(skills) + '/installed', entries)
        agents = self.home / '.claude/agents'; agents.mkdir()
        os.symlink(outside, agents / 'bad', target_is_directory=True)
        with self.assertRaises(mod.Stop):
            mod.component_tree(agents, 'personal-agent', mod.Budget(), {})

    def test_empty_directory_budget_and_file_budget(self):
        from unittest import mock
        mod=self.module()
        for n in range(10): (self.root/f'empty{n}').mkdir()
        with mock.patch.object(mod,'MAX_ENTRIES',5):
            with self.assertRaises(mod.Stop): mod.snapshot(self.root, {'files':[],'configs':[],'directories':[]})
        with mock.patch.object(mod,'MAX_FILE',1):
            with self.assertRaises(mod.Stop): mod.read_regular(self.file)
    def test_parent_replacement_does_not_read_external_secret(self):
        from unittest import mock
        mod=self.module(); real=mod.read_at; seen=[]; switched=[False]
        outside=self.base/'outside'; outside.mkdir(); (outside/'helper.py').write_text('EXTERNAL_SECRET')
        scripts=self.root/'skills/x/scripts'
        def race(fd,name,budget):
            if name=='helper.py' and not switched[0]:
                switched[0]=True; scripts.rename(self.base/'old-scripts'); scripts.symlink_to(outside,target_is_directory=True)
            data=real(fd,name,budget); seen.append(data[0]); return data
        with mock.patch.object(mod,'read_at',side_effect=race):
            with self.assertRaises((mod.Stop,OSError)):
                mod.snapshot(self.root,{'files':[],'configs':[],'directories':[]})
        self.assertTrue(switched[0]); self.assertNotIn(b'EXTERNAL_SECRET',seen)
    def test_shell_and_xdg_settings_change(self):
        shell=self.home/'.bashrc'; shell.write_text('initial')
        xdg=self.home/'.config/git'; xdg.mkdir(parents=True); (xdg/'config').write_text('[user]\nname=initial\n')
        self.boot(); shell.write_text('changed'); self.assertEqual(self.verify().returncode,20)
        shell.write_text('initial'); (xdg/'config').write_text('[user]\nname=changed\n'); self.assertEqual(self.verify().returncode,20)
    def test_active_plugin_inventory_and_content_change(self):
        other=self.base/'other'; other.mkdir(); (other/'SKILL.md').write_text('normal')
        inventory=self.base/'plugins.json'; inventory.write_text(json.dumps([{'id':'other','enabled':True,'installPath':str(other)}]))
        self.boot('--inventory',inventory); self.assertEqual(self.verify('--current-inventory',inventory).returncode,0)
        inventory.write_text('[]'); self.assertEqual(self.verify('--current-inventory',inventory).returncode,20)
        (other/'SKILL.md').write_text('changed'); self.assertEqual(self.verify().returncode,20)
    def test_direct_read_exec_no_original_import(self):
        (self.root/'skills/x/scripts/sibling.py').write_text('VALUE="copied"\n')
        self.file.write_text('from sibling import VALUE\nprint(VALUE)\n')
        self.boot()
        p=self.run_guard('exec','--state',self.bundle/'environment.json','--expect-sha256',self.receipt['sha256'],'--path','skills/x/scripts/helper.py','--','python3')
        self.assertEqual((p.returncode,p.stdout),(0,'copied\n'),p.stderr)
        self.assertFalse((self.root/'skills/x/scripts/__pycache__').exists())
        self.assertFalse((self.bundle/'plugin/skills/x/scripts/__pycache__').exists())
        p=self.run_guard('read','--state',self.bundle/'environment.json','--expect-sha256',self.receipt['sha256'],'--path','skills/x/scripts/sibling.py')
        self.assertEqual(p.stdout,'VALUE="copied"\n')
    def test_replaced_external_receipt_is_not_an_isolation_boundary(self):
        self.boot(); old=self.receipt
        self.file.write_text('print("new")\n'); self.bundle=self.base/'new-bundle'; self.boot()
        rejected=self.run_guard('verify','--state',self.bundle/'environment.json','--expect-sha256',old['sha256'])
        self.assertEqual(rejected.returncode,20)
        # 保持値まで攻撃者の新しい値へ置き換えた呼出元は、新しい信頼開始と区別できない。
        self.assertEqual(self.verify().returncode,0)


class HostSettingsTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    run_guard = EnvironmentGuardTests.run_guard
    boot = EnvironmentGuardTests.boot

    def settings_call(self, receipt=None, command='host-settings', *extra):
        receipt = receipt or self.receipt
        return self.run_guard(command, '--state', self.bundle/'environment.json',
                              '--expect-sha256', receipt['sha256'], *extra)

    def test_host_paths_are_absolute_nfc_and_cwd_independent(self):
        mod = EnvironmentGuardMoreTests.module(self)
        good = str(self.base / '日本語 space' / '.' / 'home')
        self.assertEqual(mod.host_paths({'HOME': good, 'CLAUDE_CONFIG_DIR': good + '//.claude/'})['config'],
                         str(Path(good) / '.claude'))
        for env in ({}, {'HOME': ''}, {'HOME': 'relative'}, {'HOME': '/tmp/te\u0301st'},
                    {'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': ''},
                    {'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': '~/x'},
                    {'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': '//tmp/x'},
                    {'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': '/tmp/a/../b'}):
            with self.subTest(env=env):
                with self.assertRaises(mod.Stop):
                    mod.host_paths(env)
        p = self.run_guard('host-paths')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['user_settings'], str(self.home / '.claude/settings.json'))

    def test_forbidden_setting_env_and_helpers_are_rejected_without_values(self):
        cfg = self.home / '.claude'; cfg.mkdir()
        paths = [cfg/'settings.json', cfg/'remote-settings.json']
        managed = self.base/'managed'; (managed/'managed-settings.d').mkdir(parents=True)
        paths += [managed/'managed-settings.json', managed/'managed-settings.d'/'a.json']
        forbidden = ('CLAUDE_CONFIG_DIR', 'HOME', 'XDG_CONFIG_HOME', 'CLAUDE_CODE_SIMPLE',
                     'CLAUDE_CODE_SAFE_MODE', 'CLAUDE_CODE_SHELL_PREFIX')
        for path_index, path in enumerate(paths):
            for key_index, key in enumerate(forbidden):
                for value_index, value in enumerate(('', 0, False, None, 'H50-secret\n\x1b')):
                    with self.subTest(path=path.name, key=key, value=repr(value)):
                        for other in paths: other.unlink(missing_ok=True)
                        path.write_text(json.dumps({'env': {key: value}}))
                        self.bundle = self.base / f'forbidden-{path_index}-{key_index}-{value_index}'
                        receipt = self.boot('--managed-dir', managed)
                        p = self.settings_call(receipt)
                        self.assertEqual(p.returncode, 20)
                        self.assertEqual(p.stdout, '')
                        self.assertIn('ERROR [host-settings]', p.stderr)
                        self.assertNotIn('H50-secret', p.stderr)
        for path_index, path in enumerate(paths):
            for key in ('CLAUDE_CODE_REMOTE_SETTINGS_PATH', 'CLAUDE_CODE_MANAGED_SETTINGS_PATH',
                        'CLAUDE_CODE_MOCK_REMOTE_SETTINGS', 'policyHelper', 'policyHelpers'):
                with self.subTest(path=path.name, key=key):
                    for other in paths: other.unlink(missing_ok=True)
                    value = {'env': {key: 'H50-secret\n\x1b'}} if key.startswith(('CLAUDE_', 'HOME', 'XDG_')) else {key: 'H50-secret\n\x1b'}
                    path.write_text(json.dumps(value))
                    self.bundle = self.base / ('special-' + str(path_index) + key)
                    receipt = self.boot('--managed-dir', managed)
                    p = self.settings_call(receipt)
                    self.assertEqual(p.returncode, 20)
                    self.assertEqual(p.stdout, '')
                    expected = 'host-config-source' if key.startswith('CLAUDE_CODE_') else 'host-settings'
                    self.assertIn(f'ERROR [{expected}]', p.stderr)
                    self.assertNotIn('H50-secret', p.stderr)

    def test_strict_json_and_verified_allowlist(self):
        cfg = self.home / '.claude'; cfg.mkdir()
        setting = cfg/'settings.json'
        setting.write_text('{"env":{},"permissions":{"allow":["Bash(git status)"]}}')
        self.boot()
        p = self.settings_call(None, 'host-allowlist', '--allowed-tools', 'Read Bash(git status)')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"kind": "exact"', p.stdout)
        bad = (b'{"env":{"A":"x","A":"y"}}', b'{"env":{"\\u0048OME":"x","HOME":"y"}}',
               b'[]', b'{"env":[]}', b'{"env":null}', b'{"x":NaN}', b'{"x":Infinity}', b'\xff',
               ('{"x":' * 102 + '0' + '}' * 102).encode())
        for index, raw in enumerate(bad):
            setting.write_bytes(raw)
            self.bundle = self.base / f'strict-{index}'
            self.boot()
            p = self.settings_call()
            self.assertEqual(p.returncode, 20)
            self.assertEqual(p.stdout, '')

    def test_setting_change_after_boot_is_rejected(self):
        cfg = self.home / '.claude'; cfg.mkdir()
        setting = cfg/'settings.json'; setting.write_text('{}')
        self.boot()
        setting.write_text('{"permissions":{"allow":["Bash"]}}')
        p = self.settings_call()
        self.assertEqual(p.returncode, 20)

    def test_reader_rechecks_bytes_mode_missing_and_link_after_verify(self):
        """verify 後の持続変更も、parser に渡す前の held-entry 照合で止める。"""
        from unittest import mock
        mod = EnvironmentGuardMoreTests.module(self)
        cfg = self.home / '.claude'; cfg.mkdir()
        for name in ('bytes', 'mode', 'missing', 'link'):
            with self.subTest(change=name):
                setting = cfg / 'settings.json'; setting.unlink(missing_ok=True)
                target = cfg / 'target.json'; target.unlink(missing_ok=True)
                if name == 'link':
                    target.write_text('{"permissions":{"allow":["Read"]}}')
                    setting.symlink_to(target.name)
                else:
                    setting.write_text('{"permissions":{"allow":["Read"]}}')
                self.bundle = self.base / ('recheck-' + name)
                receipt = self.boot()
                original = mod.verify
                def change_after_verify(*args, **kwargs):
                    held = original(*args, **kwargs)
                    if name == 'bytes':
                        setting.write_text('{"permissions":{"allow":["Bash"]}}')
                    elif name == 'mode':
                        setting.chmod(0o600)
                    elif name == 'missing':
                        setting.unlink()
                    else:
                        setting.unlink(); setting.symlink_to('other.json')
                        (cfg / 'other.json').write_text('{}')
                    return held
                with mock.patch.object(mod, 'verify', side_effect=change_after_verify):
                    with self.assertRaises(mod.Stop):
                        mod.read_host_settings(self.bundle/'environment.json', receipt['sha256'])
                setting.unlink(missing_ok=True); target.unlink(missing_ok=True); (cfg/'other.json').unlink(missing_ok=True)

    def test_reader_rechecks_final_link_set_for_user_cache_and_managed(self):
        """同bytesの通常file化や link 短縮も held link 集合との差で止める。"""
        from unittest import mock
        mod = EnvironmentGuardMoreTests.module(self)
        cfg = self.home / '.claude'; cfg.mkdir()
        managed = self.base / 'managed'; managed.mkdir()
        raw = '{"permissions":{"allow":["Read"]}}'
        for source in ('user', 'cache', 'managed'):
            with self.subTest(source=source):
                setting = {'user': cfg/'settings.json', 'cache': cfg/'remote-settings.json',
                           'managed': managed/'managed-settings.json'}[source]
                target = setting.with_name(setting.stem + '-target.json')
                setting.unlink(missing_ok=True); target.unlink(missing_ok=True)
                target.write_text(raw); setting.symlink_to(target.name)
                self.bundle = self.base / ('final-link-' + source)
                managed_args = ('--managed-dir', managed, '--managed-dir', managed) if source == 'managed' \
                    else ('--managed-dir', managed)
                receipt = self.boot(*managed_args)
                try:
                    # 対照: 最終 symlink は保持した bytes の reader で正常に通る。
                    self.assertEqual(mod.read_host_settings(self.bundle/'environment.json', receipt['sha256'])['user_settings'],
                                     str(cfg/'settings.json'))
                    original = mod.verify
                    def replace_after_verify(*args, **kwargs):
                        held = original(*args, **kwargs)
                        setting.unlink(); setting.write_text(raw)
                        return held
                    with mock.patch.object(mod, 'verify', side_effect=replace_after_verify):
                        with self.assertRaises(mod.Stop):
                            mod.read_host_settings(self.bundle/'environment.json', receipt['sha256'])
                finally:
                    setting.unlink(missing_ok=True); target.unlink(missing_ok=True)

        # A two-link chain may not be shortened after verify either.
        target = cfg/'target.json'; mid = cfg/'mid.json'; setting = cfg/'settings.json'
        setting.unlink(missing_ok=True); mid.unlink(missing_ok=True); target.unlink(missing_ok=True)
        target.write_text(raw); mid.symlink_to(target.name); setting.symlink_to(mid.name)
        self.bundle = self.base / 'shortened-link'; receipt = self.boot('--managed-dir', managed)
        original = mod.verify
        def shorten_after_verify(*args, **kwargs):
            held = original(*args, **kwargs)
            mid.unlink(); mid.write_text(raw)
            return held
        with mock.patch.object(mod, 'verify', side_effect=shorten_after_verify):
            with self.assertRaises(mod.Stop):
                mod.read_host_settings(self.bundle/'environment.json', receipt['sha256'])

    def test_parser_budget_and_unused_output_are_checked_after_parse(self):
        from unittest import mock
        mod = EnvironmentGuardMoreTests.module(self)
        cfg = self.home / '.claude'; cfg.mkdir()
        managed = self.base / 'managed'; dropins = managed / 'managed-settings.d'; dropins.mkdir(parents=True)
        (dropins / 'last.json').write_text('{}')
        receipt = self.boot('--managed-dir', managed)
        original_budget, held = mod.Budget, []
        class ProbeBudget(original_budget):
            def __init__(self):
                super().__init__(); held.append(self)
        original_parse = mod.strict_settings
        def expire_after_last_parse(raw):
            value = original_parse(raw)
            for budget in held: budget.deadline = 0
            return value
        with mock.patch.object(mod, 'Budget', ProbeBudget), \
             mock.patch.object(mod, 'strict_settings', side_effect=expire_after_last_parse):
            with self.assertRaises(mod.Stop):
                mod.read_host_settings(self.bundle/'environment.json', receipt['sha256'])

        (dropins / 'last.json').unlink()
        (cfg / 'settings.json').write_text(json.dumps({'permissions': {'allow': ['Bash(a *\u001b\t\u007f)']}}))
        self.bundle = self.base / 'unused-bundle'; self.boot('--managed-dir', managed)
        p = self.settings_call(None, 'host-allowlist')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('unused=', p.stdout)
        self.assertTrue(all(not (ord(ch) < 32 or ord(ch) == 127)
                            for line in p.stdout.splitlines() for ch in line))

    def test_host_setting_descriptors_are_paths_and_kinds_only(self):
        cfg = self.home / '.claude'; cfg.mkdir()
        managed = self.base / 'managed'; (managed / 'managed-settings.d').mkdir(parents=True)
        (managed / 'managed-settings.d' / 'a.json').write_text('{}')
        self.boot('--managed-dir', managed)
        state = json.loads((self.bundle / 'environment.json').read_text())
        descriptors = state['host_settings']
        self.assertTrue(all(set(item) == {'path', 'kind'} for item in descriptors))
        self.assertIn({'path': str(managed / 'managed-settings.d/a.json'), 'kind': 'managed'}, descriptors)

    def test_single_setting_symlink_is_supported_but_dropin_tree_symlink_is_rejected(self):
        cfg = self.home / '.claude'; cfg.mkdir()
        target = cfg / 'actual.json'; target.write_text('{"permissions":{"allow":["Read"]}}')
        (cfg / 'settings.json').symlink_to(target.name)
        self.boot()
        self.assertEqual(self.settings_call().returncode, 0)
        # managed-settings.d is a tree under our control, so a link to a tree is
        # never a host drop-in source even where one final user settings link is.
        managed = self.base / 'managed'; managed.mkdir()
        outside = self.base / 'outside'; outside.mkdir()
        (managed / 'managed-settings.d').symlink_to(outside, target_is_directory=True)
        self.bundle = self.base / 'dropin-link-bundle'
        p = self.run_guard('bootstrap', '--root', self.root, '--output', self.bundle,
                           '--managed-dir', managed)
        self.assertEqual(p.returncode, 20, p.stderr)

    def test_settings_parent_swap_fifo_mode_and_budget_reject(self):
        cfg = self.home / '.claude'; cfg.mkdir()
        setting = cfg / 'settings.json'; setting.write_text('{}')
        receipt = self.boot()
        old = self.home / 'old-config'; cfg.rename(old); cfg.mkdir()
        (cfg / 'settings.json').write_text('{"env":{"CLAUDE_CODE_SAFE_MODE":"secret"}}')
        p = self.settings_call(receipt)
        self.assertEqual(p.returncode, 20)
        self.assertNotIn('secret', p.stderr)

        # Each bootstrap starts from a fresh path because rejected objects must
        # not be re-used as a test fixture.
        fifo_cfg = self.home / 'fifo-config'; fifo_cfg.mkdir()
        os.mkfifo(fifo_cfg / 'settings.json')
        self.bundle = self.base / 'fifo-bundle'
        self.env['CLAUDE_CONFIG_DIR'] = str(fifo_cfg)
        p = self.run_guard('bootstrap', '--root', self.root, '--output', self.bundle)
        self.assertEqual(p.returncode, 20, p.stderr)

        mode_cfg = self.home / 'mode-config'; mode_cfg.mkdir()
        mode_setting = mode_cfg / 'settings.json'; mode_setting.write_text('{}'); mode_setting.chmod(0o200)
        self.bundle = self.base / 'mode-bundle'
        self.env['CLAUDE_CONFIG_DIR'] = str(mode_cfg)
        p = self.run_guard('bootstrap', '--root', self.root, '--output', self.bundle)
        self.assertEqual(p.returncode, 20, p.stderr)

        large_cfg = self.home / 'large-config'; large_cfg.mkdir()
        (large_cfg / 'settings.json').write_bytes(b'x' * (8 * 1024 * 1024 + 1))
        self.bundle = self.base / 'large-bundle'
        self.env['CLAUDE_CONFIG_DIR'] = str(large_cfg)
        p = self.run_guard('bootstrap', '--root', self.root, '--output', self.bundle)
        self.assertEqual(p.returncode, 20, p.stderr)

    def test_managed_dropin_names_match_the_host_scope(self):
        cfg = self.home / '.claude'; cfg.mkdir()
        managed = self.base / 'managed'; dropins = managed / 'managed-settings.d'
        (dropins / 'nested').mkdir(parents=True)
        ignored = (dropins / '.hidden.json', dropins / 'Alpha.JSON', dropins / 'nested' / 'inside.json')
        for path in ignored:
            path.write_text('{"env":{"CLAUDE_CONFIG_DIR":"must-not-read"}}')
        self.boot('--managed-dir', managed)
        self.assertEqual(self.settings_call().returncode, 0)
        state = json.loads((self.bundle / 'environment.json').read_text())
        descriptors = state['host_settings']
        for path in ignored:
            self.assertNotIn({'path': str(path), 'kind': 'managed'}, descriptors)
        active = dropins / 'Alpha.json'
        active.write_text('{"env":{"CLAUDE_CONFIG_DIR":"must-reject"}}')
        self.bundle = self.base / 'managed-active-bundle'
        self.boot('--managed-dir', managed)
        self.assertEqual(self.settings_call().returncode, 20)

    def test_new_managed_dropin_between_snapshot_and_descriptors_is_rejected(self):
        """descriptor は保持 tree から作り、直後の verify で集合差を止める。"""
        from types import SimpleNamespace
        from unittest import mock
        mod = EnvironmentGuardMoreTests.module(self)
        managed = self.base / 'managed'; dropins = managed / 'managed-settings.d'
        dropins.mkdir(parents=True)
        late = dropins / 'new.json'
        real_snapshot, raced = mod.snapshot, [False]

        def snapshot_then_add(*args, **kwargs):
            result = real_snapshot(*args, **kwargs)
            if not raced[0]:
                raced[0] = True
                late.write_text('{"env":{"CLAUDE_CODE_SAFE_MODE":"1"}}')
            return result

        args = SimpleNamespace(root=str(self.root), output=str(self.bundle), inventory=None,
                               setting=[], shell=[], config=[], skills_dir=[], settings_dir=[],
                               managed_dir=[str(managed)])
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(mod, 'snapshot', side_effect=snapshot_then_add):
            with self.assertRaises(mod.Stop):
                mod.bootstrap(args)
        self.assertTrue(raced[0])
        self.assertTrue(late.exists())


class EnvironmentUnattendedEntryDocumentationTests(unittest.TestCase):
    def test_update_doc_standalone_entry_binds_to_verified_copy_before_modes(self):
        skill = SCRIPT.parents[2] / 'update-doc/SKILL.md'
        text = skill.read_text(encoding='utf-8')
        entry = '## 無人モード(`--unattended`)'
        self.assertIn(entry, text)
        self.assertLess(text.index(entry), text.index('## 原則'))
        self.assertLess(text.index(entry), text.index('## 2 つの実行モード'))
        self.assertLess(text.index('# update-doc — ドキュメント・メモリの実コード同期'), text.index(entry))
        required = (
            '親が `state`・`sha256`・`guard`・`guard_sha256` とコピーの plugin ルートを渡したときは、5 値を継承する。',
            '1 つでも無い、hash が違う、または `verify` が失敗したときは停止する。新しい `bootstrap` で成功へ置き換えない。',
            '親の保持値が無い単独の無人入口',
            'DEV_WORKFLOW_ENV_STATE',
            'DEV_WORKFLOW_ENV_SHA256',
            'DEV_WORKFLOW_ENV_GUARD',
            'DEV_WORKFLOW_ENV_GUARD_SHA256',
            'DEV_WORKFLOW_LOOP_PLUGIN_ROOT',
            'bootstrap --root <pluginルート> --output <外部の新規ディレクトリ> --inventory <有効plugin一覧JSON>',
            '`--setting`・`--settings-dir`・`--skills-dir`',
            '`state`・`sha256`・`guard`・`guard_sha256`・`plugin`',
            '固定本文',
            "verify --state '<state>' --expect-sha256 '<sha256>'",
            'helper 実行・文書読取はコピーだけを使う',
            '元の helper を import/source しない',
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, text)
        ordered = (
            '親が `state`・`sha256`・`guard`・`guard_sha256` とコピーの plugin ルートを渡したときは、5 値を継承する。',
            '親の保持値が無い単独の無人入口',
            'bootstrap --root <pluginルート>',
            '`state`・`sha256`・`guard`・`guard_sha256`・`plugin`',
            '固定本文',
            'helper 実行・文書読取はコピーだけを使う',
            "verify --state '<state>' --expect-sha256 '<sha256>'",
        )
        indexes = [text.index(value) for value in ordered]
        self.assertEqual(indexes, sorted(indexes))


class EnvironmentLauncherTests(unittest.TestCase):
    def test_held_launcher_rejects_replaced_fixed_success_code(self):
        import hashlib
        source=SCRIPT.with_name('loop.sh').read_text()
        held=source.split("read -r -d '' PY_HELPER <<'PY' || true\n",1)[1].split('\nPY\npy()',1)[0]
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'guard.py'; path.write_text('print("normal")\n')
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            command=[sys.executable,'-c',held,'environment-run',digest,str(path)]
            good=subprocess.run(command,cwd='/',text=True,capture_output=True,timeout=5)
            self.assertEqual((good.returncode,good.stdout),(0,'normal\n'))
            path.write_text('print("forged approved")\n')
            bad=subprocess.run(command,cwd='/',text=True,capture_output=True,timeout=5)
            self.assertEqual(bad.returncode,20,bad.stderr)
            self.assertNotIn('forged approved',bad.stdout)
    def test_post_check_execution_gap_is_detected_only_on_next_check(self):
        # 同 UID の実行直前差替えの実測。完全な OS 隔離とは主張しない。
        case=EnvironmentGuardTests(); case.setUp()
        try:
            case.boot(); self.assertEqual(case.verify().returncode,0)
            copied=case.bundle/'plugin/skills/x/scripts/helper.py'
            copied.write_text('print("changed-after-check")\n')
            result=subprocess.run([sys.executable,str(copied)],text=True,capture_output=True)
            self.assertEqual(result.stdout,'changed-after-check\n')
            self.assertEqual(case.verify().returncode,20)
        finally:
            case.doCleanups()

class EnvironmentIncludePathsTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    run_guard = EnvironmentGuardTests.run_guard
    boot = EnvironmentGuardTests.boot
    verify = EnvironmentGuardTests.verify
    # このクラスでは共通 fixture だけ使う(個々の回帰は親のテストに依存しない)。
    def test_symlink_dotdot_is_not_lexically_hidden(self):
        outside=self.base/'outside'; outside.mkdir()
        (self.home/'link').symlink_to(outside,target_is_directory=True)
        (self.home/'.gitconfig').write_text('[include]\npath=link/../outside-secret\n')
        result=self.run_guard('bootstrap','--root',self.root,'--output',self.bundle)
        self.assertEqual(result.returncode,20,result.stderr)
    def test_regular_dotdot_include_and_git_path_expansion(self):
        nested=self.home/'git'; nested.mkdir()
        included=self.home/'common'; included.write_text('[user]\nname=normal\n')
        (nested/'config').write_text('[include]\npath=../common\n')
        (self.home/'.gitconfig').write_text('[include]\npath=~/git/config\n')
        self.boot(); self.assertEqual(self.verify().returncode,0)
        included.write_text('[user]\nname=changed\n'); self.assertEqual(self.verify().returncode,20)

class InstallationLinksTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    run_guard = EnvironmentGuardTests.run_guard
    boot = EnvironmentGuardTests.boot
    verify = EnvironmentGuardTests.verify
    module = EnvironmentGuardMoreTests.module

    def install(self, agents=False):
        import shutil
        checkout = self.base / 'checkout'
        checkout.mkdir()
        shutil.copy2(SCRIPT.parents[5] / 'setup.sh', checkout / 'setup.sh')
        source = checkout / 'plugins/dev-workflow/skills'
        for n in range(12):
            skill = source / ('skill' + str(n)); skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text('通常の skill 本文')
        proc = subprocess.run(['bash', str(checkout / 'setup.sh'), '--agents-global' if agents else '--global'], env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        links = list((self.home / ('.agents/skills' if agents else '.claude/skills')).iterdir())
        self.assertEqual(len(links), 12)
        self.assertTrue(all(x.is_symlink() for x in links))
        return links, source

    def test_official_global_install_and_target_change(self):
        for agents in (False, True):
            with self.subTest(agents=agents):
                case = InstallationLinksTests(); case.setUp()
                try:
                    links, source = case.install(agents)
                    args = ['--skills-dir', case.home / '.agents/skills'] if agents else []
                    case.boot(*args); self.assertEqual(case.verify().returncode, 0)
                    (source / 'skill0/SKILL.md').write_text('変更')
                    self.assertEqual(case.verify().returncode, 20)
                finally:
                    case.doCleanups()

    def test_link_retarget_rejected_before_target_content_read(self):
        from unittest import mock
        for agents in (False, True):
            with self.subTest(agents=agents):
                case = InstallationLinksTests(); case.setUp()
                try:
                    links, source = case.install(agents)
                    args = ['--skills-dir', case.home / '.agents/skills'] if agents else []
                    case.boot(*args)
                    outside = case.base / 'outside'; outside.mkdir()
                    (outside / 'secret').write_text('EXTERNAL_SECRET')
                    links[0].unlink(); links[0].symlink_to(outside, target_is_directory=True)
                    mod = case.module(); real = mod.read_at; seen = []
                    def observed(fd, name, budget, *extra):
                        value = real(fd, name, budget, *extra); seen.append(value[0]); return value
                    with mock.patch.object(mod, 'read_at', side_effect=observed):
                        with self.assertRaises(mod.Stop):
                            mod.verify(case.receipt['state'], case.receipt['sha256'])
                    self.assertNotIn(b'EXTERNAL_SECRET', seen)
                finally:
                    case.doCleanups()

    def test_relative_install_link_and_internal_link_rejection(self):
        directory = self.home / '.claude/skills'; directory.mkdir(parents=True)
        target = self.home / 'source'; target.mkdir(); (target / 'SKILL.md').write_text('normal')
        (directory / 'local').symlink_to('../../source', target_is_directory=True)
        self.boot(); self.assertEqual(self.verify().returncode, 0)
        (target / 'internal').symlink_to(self.file)
        self.assertEqual(self.verify().returncode, 20)

class HookAndDirectEntryTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    run_guard = EnvironmentGuardTests.run_guard
    boot = EnvironmentGuardTests.boot
    verify = EnvironmentGuardTests.verify

    def test_fixed_hook_loader_rejects_permission_and_guard_modification(self):
        script = self.root / 'skills/ship-task/scripts/loop-permission.py'
        script.parent.mkdir(parents=True)
        script.write_text('import json\nprint(json.dumps({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}))\n')
        self.boot()
        loop = SCRIPT.with_name('loop.sh').read_text()
        held = loop.split("read -r -d '' PY_HELPER <<'PY' || true\n", 1)[1].split('\nPY\npy()', 1)[0]
        r = self.receipt
        args = [sys.executable, '-c', held, 'hook-settings', sys.executable, str(script), r['guard_sha256'], r['guard'], r['state'], r['sha256']]
        config = subprocess.run(args, capture_output=True, text=True, env=self.env)
        self.assertEqual(config.returncode, 0, config.stderr)
        command = json.loads(config.stdout)['hooks']['PermissionRequest'][0]['hooks'][0]['command']
        def invoke():
            proc = subprocess.run(['sh', '-c', command], input='{}', capture_output=True, text=True, env=self.env, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)['hookSpecificOutput']['decision']['behavior']
        self.assertEqual(invoke(), 'allow')
        copied = self.bundle / 'plugin/skills/ship-task/scripts/loop-permission.py'
        original = copied.read_bytes()
        copied.write_text('print("FORGED_PERMISSION")')
        self.assertEqual(invoke(), 'deny')
        copied.write_bytes(original)
        self.assertEqual(invoke(), 'allow')
        Path(r['guard']).write_text('print("FORGED_GUARD")')
        self.assertEqual(invoke(), 'deny')

    def test_other_host_dynamic_skills_and_settings_directories(self):
        directory = self.home / '.agents/skills'; directory.mkdir(parents=True)
        target = self.home / 'host-skill'; target.mkdir(); (target / 'SKILL.md').write_text('normal')
        (directory / 'local').symlink_to(target, target_is_directory=True)
        settings = self.home / 'host-settings'; settings.mkdir(); (settings / 'config').write_text('normal')
        self.boot('--skills-dir', directory, '--settings-dir', settings)
        self.assertEqual(self.verify().returncode, 0)
        (target / 'SKILL.md').write_text('changed')
        self.assertEqual(self.verify().returncode, 20)

class ConfigurationLinksTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    run_guard = EnvironmentGuardTests.run_guard
    boot = EnvironmentGuardTests.boot
    verify = EnvironmentGuardTests.verify
    module = EnvironmentGuardMoreTests.module

    def create_links(self):
        dot = self.home / 'dotfiles'; dot.mkdir()
        (dot / 'git').write_text('[include]\npath=include-link\n')
        (dot / 'included').write_text('[user]\nname=normal\n')
        (self.home / '.gitconfig').symlink_to('dotfiles/git')
        (self.home / 'include-link').symlink_to('dotfiles/included')
        (dot / 'shell').write_text('export NORMAL=value\n')
        (self.home / '.bashrc').symlink_to('dotfiles/shell')
        settings = self.home / '.claude'; settings.mkdir()
        (dot / 'settings').write_text('{}')
        (settings / 'settings.json').symlink_to('../dotfiles/settings')
        return dot

    def test_normal_dotfile_links_and_include_target_change(self):
        dot = self.create_links(); self.boot()
        self.assertEqual(self.verify().returncode, 0)
        (dot / 'included').write_text('[user]\nname=changed\n')
        self.assertEqual(self.verify().returncode, 20)

    def test_dotfile_retarget_rejected_before_content(self):
        from unittest import mock
        self.create_links(); self.boot()
        secret = self.home / 'secret'; secret.write_text('EXTERNAL_SECRET')
        link = self.home / '.gitconfig'; link.unlink(); link.symlink_to(secret)
        mod = self.module(); real = mod.read_at; seen = []
        def observed(fd, name, budget, *args):
            value = real(fd, name, budget, *args); seen.append(value[0]); return value
        with mock.patch.object(mod, 'read_at', side_effect=observed):
            with self.assertRaises(mod.Stop): mod.verify(self.receipt['state'], self.receipt['sha256'])
        self.assertNotIn(b'EXTERNAL_SECRET', seen)

    def test_dotfile_target_replacement_and_cycle(self):
        dot = self.create_links(); self.boot()
        original = dot / 'settings'; original.rename(dot / 'previous')
        original.write_text('{}')
        self.assertEqual(self.verify().returncode, 20)
        original.unlink(); original.symlink_to('settings')
        result = self.run_guard('bootstrap', '--root', self.root, '--output', self.base / 'cycle')
        self.assertEqual(result.returncode, 20, result.stderr)

class EnvironmentDiagnosticTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    module = EnvironmentGuardMoreTests.module
    def test_timeout_and_oserror_do_not_print_argument_or_path_secret(self):
        from unittest import mock
        from contextlib import redirect_stderr
        import io
        mod = self.module()
        sentinel = 'https://credential-sentinel@example.invalid/private'
        errors = [subprocess.TimeoutExpired(['git','--get-all','includeif.hasconfig:remote.*.url:'+sentinel+'.path'], 3),
                  OSError(13, 'denied', sentinel)]
        for error in errors:
            with self.subTest(kind=type(error).__name__):
                stream = io.StringIO()
                with mock.patch.object(sys, 'argv', ['guard', 'verify', '--state', 'unused', '--expect-sha256', 'a'*64]), mock.patch.object(mod, 'verify', side_effect=error), redirect_stderr(stream):
                    self.assertEqual(mod.main(), 20)
                self.assertNotIn('credential-sentinel', stream.getvalue())
                self.assertIn(type(error).__name__, stream.getvalue())

class ConfigurationLinkRaceTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    module = EnvironmentGuardMoreTests.module
    def test_target_replacement_between_metadata_and_read_is_not_read(self):
        from unittest import mock
        mod = self.module()
        target = self.home / 'target'; target.write_text('normal')
        link = self.home / '.bashrc'; link.symlink_to(target)
        entries = {}; mod.record(link, 'shell', mod.Budget(), entries)
        real = mod.read_at; seen = []; switched = []
        def race(fd, name, budget, *args):
            if name == 'target' and not switched:
                switched.append(True); target.rename(self.home / 'old-target')
                target.write_text('EXTERNAL_SECRET')
            raw = real(fd, name, budget, *args); seen.append(raw[0]); return raw
        with mock.patch.object(mod, 'read_at', side_effect=race):
            with self.assertRaises(mod.Stop):
                mod.record(link, 'shell', mod.Budget(), {}, expected=entries)
        self.assertTrue(switched)
        self.assertNotIn(b'EXTERNAL_SECRET', seen)

class GitConfigOutputTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    module = EnvironmentGuardMoreTests.module
    def test_git_path_expansion_is_bounded_while_reading(self):
        from unittest import mock
        mod = self.module(); real_read = mod.os.read; real_popen = mod.subprocess.Popen
        lengths = []; processes = []
        def read(fd, size):
            raw = real_read(fd, size); lengths.append(len(raw)); return raw
        def start(*args, **kwargs):
            p = real_popen(*args, **kwargs); processes.append(p); return p
        with mock.patch.dict(os.environ, {'HOME':'/'+'x'*2000}), mock.patch.object(mod,'MAX_FILE',1024), mock.patch.object(mod.os,'read',side_effect=read), mock.patch.object(mod.subprocess,'Popen',side_effect=start):
            with self.assertRaises(mod.Stop):
                mod.includes(self.home/'config', b'[include]\n'+b'path=~\n'*10)
        self.assertLessEqual(sum(lengths), 1025 + 150)
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_stderr_is_discarded_and_timeout_is_fixed(self):
        from unittest import mock
        mod = self.module()
        # 解析失敗でも巨大 stderr を保持しない。値は診断へ出さない。
        with self.assertRaisesRegex(mod.Stop, 'Git 設定を解析できない'):
            mod.git_config_output([sys.executable,'-c','import sys;sys.stderr.write("private-sentinel"*100000);sys.exit(1)'], b'', self.env, mod.Budget())
        with mock.patch.object(mod.time, 'monotonic', side_effect=[0, 0, 4, 4]):
            with self.assertRaisesRegex(mod.Stop, '時間上限'):
                mod.git_config_output([sys.executable,'-c','import time;time.sleep(60)'], b'', self.env, mod.Budget())

@unittest.skipUnless(sys.platform.startswith('linux'), 'inotify による本文未読の実測')
class EnvironmentConfigPrereadTests(unittest.TestCase):
    setUp = EnvironmentGuardTests.setUp
    run_guard = EnvironmentGuardTests.run_guard
    boot = EnvironmentGuardTests.boot
    verify = EnvironmentGuardTests.verify

    def watch(self, path):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            self.skipTest('inotify を初期化できない')
        self.addCleanup(os.close, fd)
        self.assertGreaterEqual(libc.inotify_add_watch(fd, os.fsencode(path), 0x21), 0)
        return fd

    def assert_not_read(self, fd):
        try:
            events = os.read(fd, 65536)
        except BlockingIOError:
            events = b''
        self.assertEqual(events, b'')

    def secret(self):
        target = self.base / 'private-config'
        target.write_text('[private]\n token = SENTINEL\n')
        return target

    def test_changed_known_root_stops_before_new_include(self):
        config = self.home / '.gitconfig'
        config.write_text('[user]\n name = normal\n')
        self.boot()
        self.assertEqual(self.verify().returncode, 0)
        target = self.secret()
        config.write_text('[include]\n path = ' + str(target) + '\n')
        fd = self.watch(target)
        rejected = self.verify()
        self.assertEqual(rejected.returncode, 20, rejected.stderr)
        self.assert_not_read(fd)

    def test_new_root_candidate_stops_before_its_body_and_include(self):
        self.boot()
        config = self.home / '.gitconfig'
        target = self.secret()
        config.write_text('[include]\n path = ' + str(target) + '\n')
        config_fd, target_fd = self.watch(config), self.watch(target)
        rejected = self.verify()
        self.assertEqual(rejected.returncode, 20, rejected.stderr)
        self.assert_not_read(config_fd)
        self.assert_not_read(target_fd)

    def test_changed_known_include_stops_before_new_nested_target(self):
        included = self.home / 'included'
        included.write_text('[user]\n name = normal\n')
        (self.home / '.gitconfig').write_text('[include]\n path = included\n')
        self.boot()
        self.assertEqual(self.verify().returncode, 0)
        target = self.secret()
        included.write_text('[include]\n path = ' + str(target) + '\n')
        fd = self.watch(target)
        rejected = self.verify()
        self.assertEqual(rejected.returncode, 20, rejected.stderr)
        self.assert_not_read(fd)

    def test_absent_include_candidate_stops_before_new_body(self):
        included = self.home / 'included'
        (self.home / '.gitconfig').write_text('[include]\n path = included\n')
        self.boot()
        self.assertEqual(self.verify().returncode, 0)
        included.write_text('[private]\n token = SENTINEL\n')
        fd = self.watch(included)
        rejected = self.verify()
        self.assertEqual(rejected.returncode, 20, rejected.stderr)
        self.assert_not_read(fd)

    def test_known_nested_and_missing_graph_stays_normal(self):
        (self.home / 'leaf').write_text('[user]\n name = normal\n')
        (self.home / 'included file').write_text('[include]\n path = leaf\n path = absent\n')
        (self.home / '.gitconfig').write_text('[include]\n path = "included file"\n')
        self.boot()
        self.assertEqual(self.verify().returncode, 0)
        self.assertEqual(self.verify().returncode, 0)

class EnvironmentDirectLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ast, shlex
        cls.entries = {}
        for name in ('ship-task', 'do-task', 'update-doc'):
            text = (SCRIPT.parents[2] / name / 'SKILL.md').read_text()
            block = text.split('<!-- environment-loader:begin -->\n```bash\n', 1)[1].split('\n```', 1)[0]
            cls.entries[name] = shlex.split(block)
        cls.loader = cls.entries['ship-task'][4]
        loop = SCRIPT.with_name('loop.sh').read_text()
        line = next(line for line in loop.splitlines() if line.startswith('ENVIRONMENT_LOADER = '))
        cls.loop_loader = ast.literal_eval(line.split(' = ', 1)[1])

    def run_loader(self, path, digest, *, code=None, args=(), cwd='/', env=None):
        return subprocess.run([sys.executable, '-I', '-B', '-c', code or self.loader, digest, str(path), *args],
                              capture_output=True, text=True, timeout=20, cwd=cwd, env=env)

    def test_three_entries_and_parent_hold_identical_loader(self):
        self.assertEqual(self.loader, self.loop_loader)
        for name, words in self.entries.items():
            with self.subTest(entry=name):
                self.assertEqual(words[:4], ['python3', '-I', '-B', '-c'])
                self.assertEqual(words[4], self.loader)
                self.assertEqual(words[5:], ['<guard_sha256>', '<guard>', 'verify', '--state', '<state>', '--expect-sha256', '<sha256>'])
                text = (SCRIPT.parents[2] / name / 'SKILL.md').read_text()
                self.assertNotIn('sha256sum -- <guard>', text)
                self.assertIn('1', text.split('固定本文')[0])
                self.assertLessEqual(len(text.splitlines()), 500)

    def test_three_normal_direct_entries_execute_only_matching_bytes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            guard = Path(tmp) / 'guard.py'
            guard.write_text('import sys\nassert sys.argv[1:] == ["verify", "--state", "normal", "--expect-sha256", "held"]\nprint("normal")\n')
            digest = hashlib.sha256(guard.read_bytes()).hexdigest()
            for name, words in self.entries.items():
                with self.subTest(entry=name):
                    good = self.run_loader(guard, digest, code=words[4], args=('verify', '--state', 'normal', '--expect-sha256', 'held'))
                    self.assertEqual((good.returncode, good.stdout), (0, 'normal\n'), good.stderr)
            guard.write_text('print("forged")\n')
            bad = self.run_loader(guard, digest)
            self.assertEqual(bad.returncode, 20)
            self.assertNotIn('forged', bad.stdout)

    def test_real_bootstrap_and_direct_verify_stay_normal(self):
        case = EnvironmentGuardTests(); case.setUp()
        try:
            case.boot()
            held = case.receipt
            result = self.run_loader(held['guard'], held['guard_sha256'],
                                     args=('verify', '--state', held['state'], '--expect-sha256', held['sha256']))
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            case.doCleanups()

    @unittest.skipUnless(sys.platform == 'linux', 'inotify is Linux-specific')
    def test_link_fifo_and_oversize_reject_before_body_open(self):
        import ctypes, hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root / 'target.py'; target.write_text('print("secret")\n')
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            parent = root / 'parent'; parent.mkdir(); (parent / 'guard.py').write_bytes(target.read_bytes())
            link = root / 'link'; link.symlink_to(target)
            parent_link = root / 'parent-link'; parent_link.symlink_to(parent, target_is_directory=True)
            fifo = root / 'fifo'; os.mkfifo(fifo)
            large = root / 'large'; large.write_bytes(b'#' * 1048577)
            libc = ctypes.CDLL(None, use_errno=True)
            for path, watched in [(link, target), (parent_link / 'guard.py', parent / 'guard.py'), (fifo, fifo), (large, large)]:
                with self.subTest(path=path.name):
                    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
                    self.assertGreaterEqual(fd, 0)
                    try:
                        self.assertGreaterEqual(libc.inotify_add_watch(fd, os.fsencode(watched), 0x20 | 0x1), 0)
                        result = self.run_loader(path, digest)
                        self.assertEqual(result.returncode, 20, result.stderr)
                        try: events = os.read(fd, 65536)
                        except BlockingIOError: events = b''
                        self.assertEqual(events, b'', result.stderr)
                        self.assertNotIn('secret', result.stdout + result.stderr)
                    finally:
                        os.close(fd)

    def test_changed_path_after_hash_never_executes_replacement(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            guard = Path(tmp) / 'guard.py'; guard.write_text('print("held")\n')
            digest = hashlib.sha256(guard.read_bytes()).hexdigest()
            # Inject at the hash-return boundary while preserving the returned digest.
            prefix = ('import hashlib\n_original_sha = hashlib.sha256\n'
                      'def _replace_after_hash(raw):\n    value = _original_sha(raw)\n'
                      '    open(' + repr(str(guard)) + ', "w").write(\'print("replacement")\\n\')\n    return value\n'
                      'hashlib.sha256 = _replace_after_hash\n')
            result = self.run_loader(guard, digest, code=prefix + self.loader)
            self.assertEqual((result.returncode, result.stdout), (0, 'held\n'), result.stderr)
            self.assertIn('replacement', guard.read_text())

    def test_read_timeout_uses_fixed_diagnostic(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            guard = Path(tmp) / 'guard.py'; guard.write_text('print("never")\n')
            digest = hashlib.sha256(guard.read_bytes()).hexdigest()
            prefix = ('import os,time,signal\n_original_timer=signal.setitimer\n'
                      'signal.setitimer=lambda which, seconds: _original_timer(which, min(seconds, 0.05))\n'
                      'os.read=lambda *args: time.sleep(2)\n')
            result = self.run_loader(guard, digest, code=prefix + self.loader)
            self.assertEqual(result.returncode, 20)
            self.assertNotIn(str(guard), result.stderr)
            self.assertIn('安全に実行できない', result.stderr)

    def parent_command(self, *args, env=None, cwd='/'):
        import shlex
        source = SCRIPT.with_name('loop.sh').read_text()
        held = source.split("read -r -d '' PY_HELPER <<'PY' || true\n", 1)[1].split('\nPY\npy()', 1)[0]
        function = 'py() {' + source.split('py() {', 1)[1].split('\n}\n', 1)[0] + '\n}\n'
        command = 'PY_HELPER=' + shlex.quote(held) + '\n' + function + '\npy "$@"\n'
        return subprocess.run(['bash', '-c', command, '--', *args], capture_output=True, text=True, timeout=20, env=env, cwd=cwd)

    def test_fixed_loader_and_hook_ignore_cwd_and_pythonpath_modules(self):
        import hashlib, shlex
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); cwd=root/'cwd'; cwd.mkdir(); extra=root/'pythonpath'; extra.mkdir()
            marker=root/'untrusted-import'; guard=root/'guard.py'; guard.write_text('print("normal")\n')
            digest=hashlib.sha256(guard.read_bytes()).hexdigest()
            for directory in (cwd, extra):
                (directory/'hashlib.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("executed")\n')
            env={**os.environ,'PYTHONPATH':str(extra)}
            for name,words in self.entries.items():
                result=self.run_loader(guard,digest,code=words[4],cwd=cwd,env=env)
                self.assertEqual((result.returncode,result.stdout),(0,'normal\n'),(name,result.stderr))
                self.assertFalse(marker.exists())
            parent=self.parent_command('environment-run',digest,str(guard),env=env,cwd=cwd)
            self.assertEqual((parent.returncode,parent.stdout),(0,'normal\n'),parent.stderr)
            hook=self.parent_command('hook-settings',sys.executable,'unused-permission-path',digest,str(guard),'state','0'*64,env=env,cwd=cwd)
            self.assertEqual(hook.returncode,0,hook.stderr)
            hook_cmd=json.loads(hook.stdout)['hooks']['PermissionRequest'][0]['hooks'][0]['command']
            argv=shlex.split(hook_cmd)
            self.assertEqual(argv[1:4],['-I','-B','-c'])
            executed=subprocess.run(argv,cwd=cwd,env=env,capture_output=True,text=True,timeout=20)
            self.assertEqual((executed.returncode,executed.stdout),(0,'normal\n'),executed.stderr)
            self.assertFalse(marker.exists())

    def test_profile_keeps_a_normal_user_site_yaml_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); userbase=root/'userbase'; profile=root/'profile.yml';profile.write_text('features:\n  loop:\n    max_iterations: 3\n')
            env={k:v for k,v in os.environ.items() if k not in ('PYTHONNOUSERSITE','PYTHONPATH','PYTHONHOME')}
            env['PYTHONUSERBASE']=str(userbase)
            resolved=subprocess.run([sys.executable,'-c','import site; print(site.getusersitepackages())'],env=env,capture_output=True,text=True,check=True)
            site=Path(resolved.stdout.strip());site.mkdir(parents=True)
            marker=root/'yaml-imported'
            (site/'yaml.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("normal user-site dependency")\nclass YAMLError(Exception): pass\ndef safe_load(stream):\n    return {"features":{"loop":{"max_iterations":3}}}\n')
            result=self.parent_command('profile',str(profile),env=env)
            self.assertEqual((result.returncode,result.stdout),(0,'max_iterations=3\n'),result.stderr)
            self.assertTrue(marker.exists())


class ComponentDescriptorTests(unittest.TestCase):
    """H46: 新 target を開く前の照合と、保持 bytes の契約を確認する。"""
    setUp = EnvironmentGuardTests.setUp
    run_guard = EnvironmentGuardTests.run_guard
    boot = EnvironmentGuardTests.boot
    verify = EnvironmentGuardTests.verify
    module = EnvironmentGuardMoreTests.module
    def component(self, kind='personal-skill'):
        mod = self.module()
        root = self.base / 'components'
        root.mkdir()
        return mod, root, kind

    def capture(self, mod, root, kind, **kwargs):
        entries, blobs = {}, {}
        mod.component_tree(root, kind, mod.Budget(), entries, blobs=blobs, **kwargs)
        return entries, blobs

    def test_plain_scoped_skill_and_direct_scoped_agents(self):
        for kind in ('personal-skill', 'enterprise-skill', 'personal-agent', 'enterprise-agent'):
            with self.subTest(kind=kind):
                root = self.base / kind
                (root / 'scope/one').mkdir(parents=True)
                name = 'SKILL.md' if kind.endswith('skill') else 'reader.md'
                (root / 'scope/one' / name).write_text('plain')
                (root / name).write_text('direct')
                mod = self.module()
                entries, blobs = self.capture(mod, root, kind)
                again, held = self.capture(mod, root, kind, expected=entries)
                self.assertEqual(again, entries)
                self.assertEqual(set(held.values()), {b'plain', b'direct'})

    def test_absolute_relative_multihop_intermediate_and_dotdot(self):
        mod, root, kind = self.component()
        target = self.base / 'physical/inside'
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_text('safe')
        (self.base / 'intermediate').symlink_to('physical', target_is_directory=True)
        (self.base / 'mid').symlink_to('intermediate/inside', target_is_directory=True)
        (root / 'absolute').symlink_to(target, target_is_directory=True)
        (root / 'relative').symlink_to('../physical/inside', target_is_directory=True)
        (root / 'multi').symlink_to('../mid', target_is_directory=True)
        # ../ is relative to the physical directory reached by the intermediate link.
        (root / 'ordered').symlink_to('../intermediate/inside/.././inside', target_is_directory=True)
        entries, blobs = self.capture(mod, root, kind)
        self.assertEqual(len(blobs), 4)
        self.assertEqual(set(blobs.values()), {b'safe'})
        self.assertEqual(self.capture(mod, root, kind, expected=entries)[0], entries)

    def test_same_physical_target_reads_once_and_keeps_aliases(self):
        from unittest import mock
        mod, root, kind = self.component()
        target = self.base / 'target'; target.mkdir()
        (target / 'SKILL.md').write_text('safe')
        for name in ('first', 'second'):
            (root / name).symlink_to(target, target_is_directory=True)
        with mock.patch.object(mod, 'read_at', wraps=mod.read_at) as reader:
            entries, blobs = self.capture(mod, root, kind)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(len(blobs), 2)
        self.assertEqual(sum(key.startswith('@link:component:') for key in entries), 2)

    def test_cycles_dangling_and_41_links_are_not_missing(self):
        for mode in ('cycle', 'dangling', 'long'):
            with self.subTest(mode=mode):
                mod = self.module(); root = self.base / mode; root.mkdir()
                if mode == 'cycle':
                    (root / 'one').symlink_to('two'); (root / 'two').symlink_to('one')
                elif mode == 'dangling':
                    (root / 'one').symlink_to('does-not-exist')
                else:
                    target = self.base / 'chain'; target.mkdir()
                    for i in range(41):
                        (target / str(i)).symlink_to(str(i + 1))
                    (target / '41').mkdir()
                    (root / 'one').symlink_to(target / '0')
                with self.assertRaises(mod.Stop):
                    self.capture(mod, root, 'personal-skill')

    def test_fifo_socket_and_nested_links_are_rejected(self):
        import socket
        mod, root, kind = self.component()
        bad = root / 'bad'
        os.mkfifo(bad)
        with self.assertRaises(mod.Stop): self.capture(mod, root, kind)
        bad.unlink()
        sock = socket.socket(socket.AF_UNIX); self.addCleanup(sock.close)
        sock.bind(str(bad))
        with self.assertRaises(mod.Stop): self.capture(mod, root, kind)
        bad.unlink(); (root / 'one').mkdir(); (root / 'one/linked').symlink_to(self.base)
        with self.assertRaises(mod.Stop): self.capture(mod, root, kind)

    def test_all_replacements_stop_before_new_body_read(self):
        from unittest import mock
        for change in ('parent', 'root', 'link', 'middle', 'target', 'directory', 'file', 'mode', 'new-entry'):
            with self.subTest(change=change):
                mod = self.module(); top = self.base / change; top.mkdir()
                root = top / 'skills'; root.mkdir()
                target = top / 'target'; (target / 'nested').mkdir(parents=True)
                file = target / 'nested/SKILL.md'; file.write_text('old')
                mid = top / 'mid'; mid.symlink_to('target')
                link = root / 'linked'; link.symlink_to('../mid')
                entries, unused = self.capture(mod, root, 'personal-skill')
                if change == 'parent':
                    top.rename(self.base / (change + '-old')); (top / 'skills/new').mkdir(parents=True)
                    (top / 'skills/new/SKILL.md').write_text('new')
                elif change == 'root':
                    root.rename(top / 'old-skills'); (root / 'new').mkdir(parents=True)
                    (root / 'new/SKILL.md').write_text('new')
                elif change in ('link', 'middle'):
                    alt = top / 'alt'; alt.mkdir(); (alt / 'SKILL.md').write_text('new')
                    changed = link if change == 'link' else mid
                    changed.unlink(); changed.symlink_to(alt)
                elif change == 'target':
                    target.rename(top / 'old-target'); target.mkdir(); (target / 'SKILL.md').write_text('new')
                elif change == 'directory':
                    (target / 'nested').rename(target / 'old-nested'); (target / 'nested').mkdir()
                    (target / 'nested/SKILL.md').write_text('new')
                elif change == 'file':
                    file.rename(top / 'old-file'); file.write_text('new')
                elif change == 'mode':
                    file.chmod(0o600)
                else:
                    (root / 'new').mkdir(); (root / 'new/SKILL.md').write_text('new')
                with mock.patch.object(mod, 'read_at', wraps=mod.read_at) as reader:
                    with self.assertRaises(mod.Stop):
                        self.capture(mod, root, 'personal-skill', expected=entries)
                    self.assertEqual(reader.call_count, 0)

    def test_changed_link_target_directory_is_not_opened(self):
        from unittest import mock
        mod, root, kind = self.component()
        target = self.base / 'target'; target.mkdir(); (target / 'SKILL.md').write_text('old')
        (root / 'one').symlink_to(target)
        entries, unused = self.capture(mod, root, kind)
        target.rename(self.base / 'old'); target.mkdir(); (target / 'SKILL.md').write_text('new')
        real_open, opened = mod.os.open, []
        def watch(name, *args, **kwargs):
            opened.append(str(name)); return real_open(name, *args, **kwargs)
        with mock.patch.object(mod.os, 'open', side_effect=watch):
            with self.assertRaises(mod.Stop): self.capture(mod, root, kind, expected=entries)
        self.assertNotIn('target', opened)

    def test_missing_root_and_entry_creation_reads_no_body(self):
        from unittest import mock
        mod = self.module(); root = self.base / 'missing/skills'
        entries, unused = self.capture(mod, root, 'enterprise-skill')
        (root / 'one').mkdir(parents=True); (root / 'one/SKILL.md').write_text('new')
        with mock.patch.object(mod, 'read_at', wraps=mod.read_at) as reader:
            with self.assertRaises(mod.Stop): self.capture(mod, root, 'enterprise-skill', expected=entries)
            self.assertEqual(reader.call_count, 0)

    def test_post_read_link_replacement_rejects_result(self):
        from unittest import mock
        mod, root, kind = self.component()
        target = self.base / 'target'; target.mkdir(); (target / 'SKILL.md').write_text('old')
        link = root / 'one'; link.symlink_to(target)
        real_read = mod.read_at
        def replace(*args, **kwargs):
            result = real_read(*args, **kwargs)
            link.unlink(); link.symlink_to('missing')
            return result
        with mock.patch.object(mod, 'read_at', side_effect=replace):
            with self.assertRaises(mod.Stop): self.capture(mod, root, kind)

    def test_byte_entry_and_time_budgets_cover_link_walk(self):
        from unittest import mock
        mod, root, kind = self.component()
        (root / 'one').mkdir(); (root / 'one/SKILL.md').write_text('large')
        for limit, value in (('MAX_FILE', 1), ('MAX_TOTAL', 1), ('MAX_ENTRIES', 1), ('MAX_SECONDS', -1)):
            with self.subTest(limit=limit), mock.patch.object(mod, limit, value):
                with self.assertRaises(mod.Stop): self.capture(mod, root, kind)

    def test_bootstrap_multihop_and_additive_managed_roots(self):
        skills = self.home / '.claude/skills'; skills.mkdir(parents=True)
        target = self.base / 'target'; target.mkdir(); (target / 'SKILL.md').write_text('plain')
        (self.base / 'middle').symlink_to(target); (skills / 'one').symlink_to(self.base / 'middle')
        managed = self.base / 'managed'
        (managed / '.claude/skills/group/one').mkdir(parents=True)
        (managed / '.claude/skills/group/one/SKILL.md').write_text('enterprise')
        (managed / '.claude/agents').mkdir(); (managed / '.claude/agents/read.md').write_text('agent')
        self.boot('--managed-dir', managed)
        mod = self.module(); state = json.loads((self.bundle / 'environment.json').read_text())
        paths = {entry['path'] for entry in state['specs']['components']}
        self.assertTrue({'/etc/claude-code/.claude/skills', '/etc/claude-code/.claude/agents'} <= paths)
        self.assertIn(str(managed / '.claude/skills'), paths)
        got = mod.read_components(self.bundle / 'environment.json', self.receipt['sha256'])
        self.assertTrue(any(item['kind'] == 'enterprise-agent' and item['raw'] == b'agent' for item in got))
        self.assertEqual(self.verify().returncode, 0)

    def test_reader_uses_walker_bytes_and_rejects_old_descriptors(self):
        from unittest import mock
        skills = self.home / '.claude/skills/one'; skills.mkdir(parents=True)
        (skills / 'SKILL.md').write_text('old')
        self.boot(); mod = self.module()
        original = mod.read_regular
        def state_only(path, *args, **kwargs):
            self.assertEqual(Path(path), self.bundle / 'environment.json')
            return original(path, *args, **kwargs)
        with mock.patch.object(mod, 'read_regular', side_effect=state_only):
            held = mod.read_components(self.bundle / 'environment.json', self.receipt['sha256'])
        self.assertTrue(any(item['raw'] == b'old' for item in held))
        state = json.loads((self.bundle / 'environment.json').read_text()); state['specs'].pop('components')
        raw = mod.dump(state); legacy = self.base / 'legacy-state'; legacy.write_bytes(raw)
        with self.assertRaises(mod.Stop): mod.read_components(legacy, mod.sha(raw))

    def test_policy_digest_excludes_all_runtime_identity(self):
        skills = self.home / '.claude/skills'; skills.mkdir(parents=True)
        target = self.base / 'target'; target.mkdir(); (target / 'SKILL.md').write_text('same')
        link = skills / 'one'; link.symlink_to(target)
        self.boot(); mod = self.module()
        first = mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256'])
        # 同じ論理sourceを同じ内容で作り直した別runでも証明digestは変わらない。
        link.unlink(); link.symlink_to(target)
        old = target / 'SKILL.md'; old.unlink(); old.write_text('same')
        self.bundle = self.base / 'second-bundle'; self.boot()
        self.assertEqual(first, mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256']))

    def marketplace(self):
        config = self.home / '.claude/plugins'; config.mkdir(parents=True)
        market = self.base / 'market'; (market / '.claude-plugin').mkdir(parents=True)
        plugin = market / 'active'; (plugin / '.claude-plugin').mkdir(parents=True)
        (plugin / '.claude-plugin/plugin.json').write_text('{"name":"active"}')
        (plugin / 'SKILL.md').write_text('active')
        # inactive entry は危険な外部参照でも全tree走査の理由にしない。
        entry = {'name': 'active', 'source': './active', 'commands': {'safe': {'content': 'safe'}}}
        (market / '.claude-plugin/marketplace.json').write_text(json.dumps({'plugins': [entry,
            {'name': 'inactive', 'source': '/outside', 'hooks': './fifo'}]}))
        os.mkfifo(market / 'fifo')
        (config / 'known_marketplaces.json').write_text(json.dumps({'market': {'installLocation': str(market)}}))
        (config / 'installed_plugins.json').write_text(json.dumps({'version': 2, 'plugins': {
            'active@market': [{'installPath': str(plugin), 'scope': 'user'}]}}))
        inventory = self.base / 'inventory.json'
        inventory.write_text(json.dumps([{'id': 'active@market', 'enabled': True,
                                         'scope': 'user', 'installPath': str(plugin)},
                                        {'id': 'inactive@market', 'enabled': False, 'installPath': '/outside'}]))
        return config, market, plugin, inventory

    def test_selected_marketplace_is_held_without_inactive_tree(self):
        config, market, plugin, inventory = self.marketplace()
        self.boot('--inventory', inventory)
        mod = self.module()
        components = mod.read_components(self.bundle / 'environment.json', self.receipt['sha256'])
        selected = [item for item in components if item['kind'] == 'marketplace-manifest']
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['entry'][0]['plugin'], 'active@market')
        self.assertTrue(any(item['raw'] == b'active' for item in components))
        self.assertFalse(any(item['path'].endswith('fifo') for item in components))
        self.assertEqual(self.verify().returncode, 0)

    def test_marketplace_missing_ambiguous_and_external_sources_fail(self):
        config, market, plugin, inventory = self.marketplace()
        installed = config / 'installed_plugins.json'
        original = installed.read_bytes()
        installed.unlink()
        self.assertEqual(self.run_guard('bootstrap', '--root', self.root, '--output', self.bundle,
                                        '--inventory', inventory).returncode, 20)
        installed.write_bytes(original)
        manifest = market / '.claude-plugin/marketplace.json'
        for index, data in enumerate(({'plugins': []}, {'plugins': [
                {'name': 'active', 'source': './active'}, {'name': 'active', 'source': './active'}]},
                {'plugins': [{'name': 'active', 'source': '../outside'}]},
                {'plugins': [{'name': 'active', 'source': './missing'}]})):
            manifest.write_text(json.dumps(data))
            self.assertEqual(self.run_guard('bootstrap', '--root', self.root, '--output', self.base / str(index),
                                            '--inventory', inventory).returncode, 20)

    def test_synced_and_local_inventory_do_not_need_marketplace_registry(self):
        for scope, plugin_id in (('synced', 'one@sync'), ('user', 'one')):
            root = self.base / scope; root.mkdir(); (root / 'SKILL.md').write_text('safe')
            inventory = self.base / (scope + '.json')
            inventory.write_text(json.dumps([{'id': plugin_id, 'scope': scope, 'installPath': str(root)}]))
            self.bundle = self.base / (scope + '-bundle'); self.boot('--inventory', inventory)
            components = self.module().read_components(self.bundle / 'environment.json', self.receipt['sha256'])
            self.assertTrue(any(item['kind'] == 'plugin' and item['root'] == str(root) for item in components))

    def skills_dir_plugin(self, root):
        plugin = root / 'folder'
        (plugin / '.claude-plugin').mkdir(parents=True)
        (plugin / '.claude-plugin/plugin.json').write_text('{"name":"local-name"}')
        (plugin / 'SKILL.md').write_text('root skill')
        (plugin / 'skills/nested').mkdir(parents=True)
        (plugin / 'skills/nested/SKILL.md').write_text('nested skill')
        return plugin

    def test_skills_dir_inventory_merges_only_exact_held_root_and_manifest(self):
        for scope in ('personal', 'enterprise'):
            with self.subTest(scope=scope):
                managed = self.base / ('managed-' + scope)
                root = self.home / '.claude/skills' if scope == 'personal' else managed / '.claude/skills'
                plugin = self.skills_dir_plugin(root)
                inventory = self.base / (scope + '.json')
                inventory.write_text(json.dumps([{'id':'local-name@skills-dir', 'scope':'user', 'installPath':str(plugin)}]))
                self.bundle = self.base / (scope + '-bundle')
                self.boot('--inventory', inventory, '--managed-dir', managed)
                mod = self.module()
                state = json.loads((self.bundle / 'environment.json').read_text())
                self.assertEqual(state['inventory'][0]['id'], 'local-name@skills-dir')
                self.assertFalse(any(item['kind'] == 'plugin' and item['path'] == str(plugin)
                                     for item in state['specs']['components']))
                components = mod.read_components(self.bundle / 'environment.json', self.receipt['sha256'])
                loaded = [item for item in components if item['path'].startswith(str(plugin) + '/')]
                self.assertEqual(len(loaded), 3)
                self.assertTrue(all(item['kind'] == scope + '-skill' for item in loaded))
                self.assertEqual(self.verify().returncode, 0)

    def test_skills_dir_rejects_unknown_ids_wrong_name_missing_manifest_and_outside(self):
        root = self.home / '.claude/skills'
        plugin = self.skills_dir_plugin(root)
        outside = self.skills_dir_plugin(self.base / 'outside')
        inventory = self.base / 'inventory.json'
        for index, (plugin_id, path) in enumerate((('local-name@unknown', plugin),
                ('wrong@skills-dir', plugin), ('local-name@skills-dir', outside),
                ('local-name@skills-dir', plugin / 'skills/nested'), ('@skills-dir', plugin),
                ('local-name@skills-dir', str(root) + '/escape/../folder'))):
            inventory.write_text(json.dumps([{'id':plugin_id, 'scope':'user', 'installPath':str(path)}]))
            got = self.run_guard('bootstrap', '--root', self.root, '--output', self.base / ('bad-' + str(index)),
                                 '--inventory', inventory)
            self.assertEqual(got.returncode, 20, (plugin_id, got.stderr))
        (plugin / '.claude-plugin/plugin.json').unlink()
        inventory.write_text(json.dumps([{'id':'local-name@skills-dir', 'scope':'user', 'installPath':str(plugin)}]))
        self.assertEqual(self.run_guard('bootstrap', '--root', self.root, '--output', self.bundle,
                                       '--inventory', inventory).returncode, 20)

    def test_skills_dir_outside_and_ambiguous_sources_are_rejected_without_opening_target(self):
        from unittest import mock
        mod = self.module(); root = self.home / '.claude/skills'; plugin = self.skills_dir_plugin(root)
        entries, blobs = self.capture(mod, root, 'personal-skill')
        descriptors = [{'kind':'personal-skill', 'path':str(root)}]
        item = {'id':'local-name@skills-dir', 'scope':'user', 'installPath':str(self.base / 'outside')}
        with mock.patch.object(mod, 'component_tree', side_effect=AssertionError('unexpected target read')):
            with self.assertRaises(mod.Stop):
                mod._component_inventory({'components':descriptors}, [item], mod.Budget(), entries, blobs)
            item['installPath'] = str(plugin)
            descriptors.append({'kind':'enterprise-skill', 'path':str(root)})
            with self.assertRaises(mod.Stop):
                mod._component_inventory({'components':descriptors}, [item], mod.Budget(), entries, blobs)

    def test_skills_dir_installation_link_uses_held_chain_and_rejects_replacement(self):
        from unittest import mock
        mod = self.module(); root = self.home / '.claude/skills'; root.mkdir(parents=True)
        target = self.skills_dir_plugin(self.base / 'target')
        middle = self.base / 'middle'; middle.symlink_to(target)
        (root / 'linked').symlink_to(middle)
        inventory = self.base / 'inventory.json'
        inventory.write_text(json.dumps([{'id':'local-name@skills-dir', 'scope':'user', 'installPath':str(root / 'linked')}]))
        self.boot('--inventory', inventory)
        self.assertEqual(self.verify().returncode, 0)
        state = json.loads((self.bundle / 'environment.json').read_text())
        replacement = self.skills_dir_plugin(self.base / 'replacement')
        middle.unlink(); middle.symlink_to(replacement)
        with mock.patch.object(mod, 'read_at', wraps=mod.read_at) as reader:
            with self.assertRaises(mod.Stop): self.capture(mod, root, 'personal-skill', expected=state['entries'])
            self.assertEqual(reader.call_count, 0)

    def test_skills_dir_reordering_preserves_missing_descriptors_and_shared_budget(self):
        from unittest import mock
        mod = self.module(); root = self.home / '.claude/skills'; plugin = self.skills_dir_plugin(root)
        missing = self.base / 'missing-skills'
        specs = {'files':[], 'directories':[], 'configs':[], 'components':[
            {'kind':'plugin', 'path':str(self.root)}, {'kind':'personal-skill', 'path':str(root)},
            {'kind':'enterprise-skill', 'path':str(missing)}]}
        inventory = [{'id':'local-name@skills-dir', 'scope':'user', 'installPath':str(plugin)}]
        budget_ids = []; real_tree = mod.component_tree
        def tree(*args, **kwargs):
            budget_ids.append(id(args[2])); return real_tree(*args, **kwargs)
        with mock.patch.object(mod, 'component_tree', side_effect=tree):
            held = mod.snapshot(self.root, specs, inventory)
        self.assertEqual(len(set(budget_ids)), 1)
        self.assertEqual(held['component:enterprise-skill:' + str(missing)], ['missing'])
        total = sum(path.stat().st_size for top in (self.root, root) for path in top.rglob('*') if path.is_file())
        with mock.patch.object(mod, 'MAX_TOTAL', total - 1):
            with self.assertRaises(mod.Stop): mod.snapshot(self.root, specs, inventory)

    def test_legacy_state_verify_retains_previous_contract(self):
        mod = self.module()
        self.boot()
        value = json.loads((self.bundle / 'environment.json').read_text())
        value['specs'].pop('components')
        value['entries'] = mod.snapshot(value['root'], value['specs'], value['inventory'])
        raw = mod.dump(value); old = self.base / 'legacy'; old.write_bytes(raw)
        self.assertEqual(mod.verify(old, mod.sha(raw))['version'], 3)
        with self.assertRaises(mod.Stop): mod.read_components(old, mod.sha(raw))

    def test_exactly_40_links_are_allowed(self):
        mod, root, kind = self.component()
        chain = self.base / 'chain'; chain.mkdir()
        for i in range(39): (chain / str(i)).symlink_to(str(i + 1))
        (chain / '39').mkdir(); (chain / '39/SKILL.md').write_text('safe')
        (root / 'one').symlink_to(chain / '0')
        entries, blobs = self.capture(mod, root, kind)
        self.assertEqual(list(blobs.values()), [b'safe'])
        self.assertEqual(sum(entry[:1] == ['link-step'] for entry in entries.values()), 40)

    def test_legacy_comparison_reads_replacement_but_component_stops_first(self):
        from unittest import mock
        mod, root, kind = self.component()
        (root / 'one').mkdir(); file = root / 'one/SKILL.md'; file.write_text('old')
        legacy, held = {}, {}
        mod.tree(root, 'legacy', mod.Budget(), legacy)
        mod.component_tree(root, kind, mod.Budget(), held)
        file.rename(self.base / 'old-file'); file.write_text('new')
        with mock.patch.object(mod, 'read_at', wraps=mod.read_at) as reader:
            changed = {}; mod.tree(root, 'legacy', mod.Budget(), changed, expected=legacy)
            self.assertEqual(reader.call_count, 1)
            self.assertNotEqual(changed, legacy)
        with mock.patch.object(mod, 'read_at', wraps=mod.read_at) as reader:
            with self.assertRaises(mod.Stop): mod.component_tree(root, kind, mod.Budget(), {}, expected=held)
            self.assertEqual(reader.call_count, 0)

    def test_unreadable_file_and_agent_install_link_rejected(self):
        mod, root, kind = self.component('personal-agent')
        file = root / 'reader.md'; file.write_text('plain'); file.chmod(0)
        with self.assertRaises(mod.Stop): self.capture(mod, root, kind)
        file.chmod(0o600); file.unlink(); file.symlink_to(self.file)
        with self.assertRaises(mod.Stop): self.capture(mod, root, kind)

    def test_registry_wrong_path_and_duplicate_active_id_rejected(self):
        config, market, plugin, inventory = self.marketplace()
        (config / 'installed_plugins.json').write_text(json.dumps({'plugins': {
            'active@market': [{'installPath': '/wrong', 'scope': 'user'}]}}))
        self.assertEqual(self.run_guard('bootstrap', '--root', self.root, '--output', self.bundle,
                                        '--inventory', inventory).returncode, 20)
        raw = json.loads(inventory.read_text()); raw.append(raw[0]); inventory.write_text(json.dumps(raw))
        self.assertEqual(self.run_guard('bootstrap', '--root', self.root, '--output', self.bundle,
                                        '--inventory', inventory).returncode, 20)

    def test_two_selected_entries_share_manifest_without_losing_sources(self):
        config, market, first, inventory = self.marketplace()
        second = market / 'second'; second.mkdir(); (second / 'SKILL.md').write_text('second')
        manifest = market / '.claude-plugin/marketplace.json'; data = json.loads(manifest.read_text())
        data['plugins'].append({'name': 'second', 'source': './second'}); manifest.write_text(json.dumps(data))
        installed = config / 'installed_plugins.json'; data = json.loads(installed.read_text())
        data['plugins']['second@market'] = [{'installPath': str(second), 'scope': 'user'}]
        installed.write_text(json.dumps(data))
        data = json.loads(inventory.read_text()); data.append({'id': 'second@market', 'scope': 'user',
                                                             'installPath': str(second)})
        inventory.write_text(json.dumps(data)); self.boot('--inventory', inventory)
        components = self.module().read_components(self.bundle / 'environment.json', self.receipt['sha256'])
        selected = [item for item in components if item['kind'] == 'marketplace-manifest']
        self.assertEqual(len(selected), 1)
        self.assertEqual({item['plugin'] for item in selected[0]['entry']}, {'active@market', 'second@market'})
        self.assertTrue(any(item['root'] == str(first) for item in components))
        self.assertTrue(any(item['root'] == str(second) for item in components))

    def test_missing_manifest_creation_before_return_is_rejected(self):
        from unittest import mock
        mod = self.module(); root = self.base / 'market'; root.mkdir()
        manifest = root / 'marketplace.json'; original = mod._component_keep
        def create_after_missing(key, value, entries, expected):
            result = original(key, value, entries, expected)
            if key.startswith('component:') and value == ['missing']:
                manifest.write_text('{}')
            return result
        with mock.patch.object(mod, '_component_keep', side_effect=create_after_missing):
            with self.assertRaises(mod.Stop):
                self.capture(mod, manifest, 'marketplace-manifest')

    def test_policy_digest_normalizes_only_workflow_run_copy_root(self):
        import shutil
        mod = self.module(); self.boot()
        first = mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256'])
        second_root = self.base / 'another-run/plugin'; shutil.copytree(self.root, second_root)
        self.root = second_root; self.bundle = self.base / 'another-bundle'; self.boot()
        second = mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256'])
        self.assertEqual(first, second)
        # 同一bytesでもactive installPathの変更は、実sourceの変更として保持する。
        active = self.base / 'installed'; shutil.copytree(second_root, active)
        inventory = self.base / 'active.json'
        inventory.write_text(json.dumps([{'id': 'local', 'installPath': str(active)}]))
        self.bundle = self.base / 'with-active'; self.boot('--inventory', inventory)
        first = mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256'])
        relocated = self.base / 'relocated'; shutil.copytree(active, relocated)
        inventory.write_text(json.dumps([{'id': 'local', 'installPath': str(relocated)}]))
        self.bundle = self.base / 'relocated-bundle'; self.boot('--inventory', inventory)
        self.assertNotEqual(first, mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256']))


    def test_policy_digest_ignores_missing_parent_location_but_tracks_component(self):
        mod = self.module(); self.boot()
        first = mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256'])
        cfg = self.home / '.claude'; cfg.mkdir(); (cfg / 'settings.json').write_text('{}')
        # 保持中のrunはmetadata変化として止まる。別run証明の内容だけを同値とする。
        self.assertEqual(self.verify().returncode, 20)
        self.bundle = self.base / 'parent-created'; self.boot()
        second = mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256'])
        self.assertEqual(first, second)
        skills = cfg / 'skills'; skills.mkdir()
        target = self.base / 'definition'; target.mkdir(); (target / 'SKILL.md').write_text('same')
        (skills / 'one').symlink_to(target)
        self.bundle = self.base / 'definition-created'; self.boot()
        third = mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256'])
        self.assertNotEqual(second, third)
        (self.base / 'middle').symlink_to(target)
        (skills / 'one').unlink(); (skills / 'one').symlink_to(self.base / 'middle')
        self.bundle = self.base / 'route-changed'; self.boot()
        fourth = mod.component_policy_digest(self.bundle / 'environment.json', self.receipt['sha256'])
        self.assertNotEqual(third, fourth)
