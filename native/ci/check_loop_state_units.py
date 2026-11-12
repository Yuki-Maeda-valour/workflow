#!/usr/bin/env python3
"""S2LのRust54件を実行し、必須名・件数・未実行0を照合する。"""
import hashlib,json,os,re,subprocess,sys,time
from pathlib import Path
import check_loop_state as check
import check_loop_state_mutations as mutations
REQUIRED=set('loop_state::config_graph_tests::graph_orders_all_roots_conditions_rechecks_candidates\nloop_state::config_graph_tests::graph_returns_exact_keys_digest_origin_order_candidates\nloop_state::config_graph_tests::graph_drains_growing_queue\nloop_state::config_graph_tests::graph_rejects_normal_and_deferred_cycles_itself\nloop_state::config_graph_tests::graph_forwards_one_budget_to_every_operation\nloop_state::config_graph_tests::graph_keeps_preconsumed_item_and_byte_limits\nloop_state::config_graph_tests::graph_keeps_deadline_at_three_late_boundaries\nloop_state::config_graph_tests::graph_rejects_origin_change_after_effective_operation\nloop_state::config_tests::config_snapshot_orders_reads_conditions_rechecks_and_candidates\nloop_state::config_tests::config_snapshot_returns_exact_values_and_ordered_candidates\nloop_state::config_tests::config_snapshot_drains_nested_hasconfig_queue\nloop_state::config_tests::config_snapshot_rejects_normal_and_deferred_cycles_before_effective_git\nloop_state::config_tests::config_snapshot_passes_same_budget_through_every_phase\nloop_state::config_tests::config_snapshot_keeps_callers_item_and_byte_consumption\nloop_state::config_tests::config_snapshot_keeps_callers_deadline_at_late_phases\nloop_state::config_tests::config_snapshot_rejects_origin_change_after_effective_git\nloop_state::safety_tests::i01_secret_globs_never_open_body\nloop_state::safety_tests::i02_double_star_and_secret_children_metadata\nloop_state::safety_tests::i03_git_and_empty_names_share_consumption\nloop_state::safety_tests::i04_secret_symlink_ignores_target_and_text\nloop_state::safety_tests::i07_read_oserror_is_not_promoted_to_stop\nloop_state::safety_tests::i08_growth_and_lstat_open_replacement_rejected\nloop_state::safety_tests::m13_state_total_bound_checked_before_open_and_growth\nloop_state::safety_tests::m03_global_link_switch_rejected_before_git\nloop_state::safety_tests::m04_changed_include_rejected_before_any_parser\nloop_state::safety_tests::m07_changed_git_pointer_never_opens_new_admin\nloop_state::safety_tests::m08_changed_commondir_rejected_before_git\nloop_state::safety_tests::m09_new_config_layer_rejected_before_link_target\nloop_state::safety_tests::m10_absent_global_appearance_rejected_before_git\nloop_state::safety_tests::m11_empty_global_include_change_rejected\nloop_state::safety_tests::l01_profile_parsing_starts_after_held_files_close\nloop_state::safety_tests::l05_run_expired_after_spawn_reaps_direct_child\nloop_state::safety_tests::l06_index_expired_before_spawn\nloop_state::safety_tests::i09_ancestor_sibling_allowed_inode_change_rejected\nloop_state::safety_tests::i05_unavailable_default_system_path_requires_explicit_setting\nloop_state::safety_tests::i06_explicit_system_path_never_queries_git\nloop_state::safety_tests::m01_new_owned_candidate_symlink_never_opened\nloop_state::safety_tests::m02_refs_share_budget_and_keep_dangling\nloop_state::safety_tests::m12_link_fixed_during_run_and_refresh_reads_new_baseline\nloop_state::safety_tests::m05_each_worktree_uses_its_own_branch_context\nloop_state::safety_tests::m06_symbolic_head_chain_uses_final_branch\nloop_state::safety_tests::yaml_recursion_temporary_second_compose_and_merge_frames\nloop_state::json::tests::python_numbers_and_unicode\nloop_state::output::tests::write_and_finalization_faults_are_distinct'.splitlines())
REQUIRED.add('loop_state::walk_recursion::tests::cache_and_temporary_calls_preserve_cpython_boundaries')
# 正式実装レビュー r3 の 3 分類と、全コマンド共通の決定(Rust だけの検査)。
REQUIRED.update({'loop_state::decision_tests::r3_enumeration_failure_keeps_surrogate_name',
                 'loop_state::decision_tests::r3_directory_change_during_read_keeps_surrogate_name',
                 'loop_state::decision_tests::r3_held_directory_listing_duplicates_and_rewinds',
                 'loop_state::decision_tests::interrupt_during_state_write_keeps_temporary',
                 'loop_state::decision_tests::sigint_ignored_at_start_stays_ignored',
                 'loop_state::decision_tests::state_write_buffer_follows_block_size',
                 'loop_state::decision_tests::mkdtemp_candidates_and_names_follow_tempfile',
                 'loop_state::legacy_file::tests::writes_all_chunks_in_order_with_block_size_buffer',
                 'loop_state::runtime::tests::hash_seed_follows_strtoul_and_range',
                 # 正式実装レビュー r4。
                 'loop_state::decision_tests::onbranch_cleanup_follows_os_walk_bottom_up',
                 'loop_state::decision_tests::entry_readlink_failure_is_stop_with_codepoints'})
COUNT=len(REQUIRED)

def assert_listing(raw):
    names={line.removesuffix(': test') for line in raw.decode().splitlines() if line.endswith(': test')}
    if names!=REQUIRED:raise AssertionError('S2L_UNIT_INVENTORY: '+repr(names^REQUIRED))

def assert_result(result,log=None):
    """件数・名前・未実行0。log(libtest の --logfile)があれば、並行の出力の混ざりに頼らず名前を照らす。"""
    output=result.stdout+result.stderr
    if result.returncode or f'{COUNT} passed; 0 failed; 0 ignored'.encode() not in output:raise AssertionError('S2L_UNIT_RESULT: '+repr(output))
    if log is not None:
        rows=set(log.splitlines())
        for name in REQUIRED:
            if f'ok {name}'.encode() not in rows:raise AssertionError('S2L_UNIT_MISSING: '+name)
        if any(not row.startswith(b'ok ') for row in rows if row):raise AssertionError('S2L_UNIT_SKIP')
        return
    for name in REQUIRED:
        if f'test {name} ... ok'.encode() not in output:raise AssertionError('S2L_UNIT_MISSING: '+name)
    if b'... ignored' in output:raise AssertionError('S2L_UNIT_SKIP')

# Mac の既定の APFS が受け付けない入力(UTF-8 でない名前・大文字小文字だけ違う名前)を、
# 飛ばす(skipped)か、別の所に作り直して同じ検査を流す(replaced)検査。ASCII の対照は全 OS で流す。
# Linux では 1 件も出さない。
MAC_FS_REFUSALS=[('i01_secret_globs_never_open_body','replaced'),
                 ('r3_directory_change_during_read_keeps_surrogate_name','skipped'),
                 ('r3_enumeration_failure_keeps_surrogate_name','skipped')]

def fs_refusals(stdout):
    """S2L_OS_BOUNDARY の input= つきの行の(検査の名前, action)。"""
    pattern=rb'S2L_OS_BOUNDARY name=(\S+) os=\S+ input=\S+(?: \S+)*? action=(\S+)'
    rows=sorted((m.group(1).decode(),m.group(2).decode()) for m in re.finditer(pattern,stdout))
    if len(rows)!=len(re.findall(rb'S2L_OS_BOUNDARY name=\S+ os=\S+ input=',stdout)):raise AssertionError('S2L_UNIT_FS_REFUSAL_FORM')
    return rows

def assert_fs_refusals(refusals):
    """Mac ではちょうど MAC_FS_REFUSALS、ほかの OS では 0 件でなければ失敗にする(黙って合格にしない)。"""
    expected=sorted(MAC_FS_REFUSALS) if sys.platform=='darwin' else []
    if refusals!=expected:raise AssertionError('S2L_UNIT_FS_REFUSALS: '+repr(refusals))

def main():
    check.environment();started=time.monotonic();native=check.ROOT/'native';env=os.environ.copy()
    if not env.get('CARGO_TARGET_DIR') or Path(env['CARGO_TARGET_DIR']).resolve()==check.ROOT or check.ROOT in Path(env['CARGO_TARGET_DIR']).resolve().parents:raise RuntimeError('外部CARGO_TARGET_DIRが必要です')
    command=['cargo','test','--locked','-p','dev-workflow-cli','--example','loop_state_fixture','--no-run','--message-format=json']
    binary=mutations.executable(mutations.run(command,native,env));before=hashlib.sha256(binary.read_bytes()).hexdigest()
    listing=mutations.run([str(binary),'--list','--format','terse'],native,env)
    if listing.returncode:raise AssertionError('S2L_UNIT_LIST_EXIT')
    assert_listing(listing.stdout)
    import tempfile
    with tempfile.TemporaryDirectory(prefix='s2l-unit-log-') as folder:
        logfile=Path(folder)/'libtest.log'
        result=mutations.run([str(binary),'--nocapture','--logfile',str(logfile)],native,env);sys.stdout.buffer.write(result.stdout);sys.stderr.buffer.write(result.stderr)
        assert_result(result,logfile.read_bytes() if logfile.exists() else b'')
    # RLIMIT_NOFILE を下げた旧新の比較(低いfdの上限)は、恒久の比較に入れない(00:42 の決定)。
    if hashlib.sha256(binary.read_bytes()).hexdigest()!=before:raise AssertionError('S2L_UNIT_BINARY_CHANGED')
    refusals=fs_refusals(result.stdout);assert_fs_refusals(refusals)
    print(json.dumps({'rust_tests':COUNT,'common_graph':8,'linux_real_connections':0 if sys.platform=='darwin' else 8,'mac_actual_refusals':8 if sys.platform=='darwin' else 0,'safety_observations':25,'yaml_context':1,'other_pure':3,'r3_and_decisions':11,'refs_component_boundaries':3-result.stdout.count(b'M02_OS_REFUSAL_BOUNDARY_NOT_REACHED:'),'refs_component_os_refusals':result.stdout.count(b'M02_OS_REFUSAL_BOUNDARY_NOT_REACHED:'),'low_fd_comparisons':0,'fs_input_refusals':refusals,'skipped':0,'binary_sha256':before,'seconds':round(time.monotonic()-started,3)}));return 0
if __name__=='__main__':raise SystemExit(main())
