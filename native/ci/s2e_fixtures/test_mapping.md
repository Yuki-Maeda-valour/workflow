# scripts/test_environment_guard.py の対応表(S2E・#470)

`native/ci/check_s2e.py` の `TEST_MAPPING` から作る。直すときは `TEST_MAPPING` を直し、
`python3 -B native/ci/check_s2e.py --write-mapping` で作り直す。

- 全 117 テスト: 旧新の両方で比較 54・Rust の検査へ置換 51・対象外 12
- うち予定を含まない(比較済み・実装済みの Rust のテスト・対象外): 100 件・「予定」(2 段目 b で作る)を含む: 17 件
- 「実装済み」を含み、「予定」を含まない行: 50 件
- U2 の差(テストの配布物に guard が無い): 「旧新の両方で比較」の行のうち、既存テストの本文が bootstrap を使い、旧 guard を足した配布物で固定入力を作り直すテスト: 53 件(「Rust の検査へ置換」・対象外の行は数えない)
- 読み込み器を通すテストと、Python の内部だけを観察するテストは、理由を書いて対象外にした。

- 最後の欄は、指す比較のうち「Linux だけ」の印の項目(Mac の経路では流さない)。印は `s2e_commands.py` の項目の一覧から作る。

| テスト | 分類 | 対応先・理由 | Linux だけの比較 |
|---|---|---|---|
| `NativeDistributionEnvironmentTests.test_full_distribution_is_retained_and_binary_changes_are_rejected` | 旧新の両方で比較 | 比較[x-real-distribution, x-real-distribution-flip](配布物の全体の保持と、Rust のバイナリ・launch.sh・resolve-task-dir.py の 1 ビットの書き換えの拒否と戻した後。旧版との比較)。Budget の計上は budget.rs::tick_stops_just_after_ten_thousand_items。check_plugin_launch は対象外(D2。固定の読み込み器と launch の入口) | - |
| `EnvironmentGuardTests.test_normal_and_copy_exec` | 旧新の両方で比較 | bootstrap と verify は 比較[basic]。exec は次の段(予定) | - |
| `EnvironmentGuardTests.test_source_fixed_output_and_skill_change` | 旧新の両方で比較 | 比較[verify-edit-plugin-file, verify-edit-plugin-file-same-size, verify-skill-edit, x-source-guard-changed] | - |
| `EnvironmentGuardTests.test_copy_modified` | 旧新の両方で比較 | 比較[verify-copy-edit, verify-copy-remove, verify-copy-add, verify-copy-chmod, verify-copy-dir-chmod] | - |
| `EnvironmentGuardTests.test_manifest_and_hash_forgery` | 旧新の両方で比較 | 比較[x-forged-state, verify-state-edit, verify-state-remove] | - |
| `EnvironmentGuardTests.test_new_and_removed_empty_directories` | 旧新の両方で比較 | 比較[x-empty-dir-plugin-add, x-empty-dir-plugin-remove, x-empty-dir-copy-add, x-empty-dir-copy-remove, x-empty-dir-skills-add, x-empty-dir-skills-remove] | - |
| `EnvironmentGuardTests.test_missing_then_added_user_setting` | 旧新の両方で比較 | 比較[verify-cache-created, verify-shell-created, verify-xdg-config-created] | - |
| `EnvironmentGuardTests.test_quoted_continued_include_normal_and_change` | 旧新の両方で比較 | 比較[include-quoted, include-plain, include-chain, git-duplicate-include] | - |
| `EnvironmentGuardTests.test_special_file_and_parent_symlink_fail_without_read` | 旧新の両方で比較 | 比較[x-special-fifo-in-plugin, x-special-parent-symlink, x-special-settings-fifo] | x-special-fifo-in-plugin, x-special-parent-symlink, x-special-settings-fifo |
| `EnvironmentGuardTests.test_state_fifo_no_block` | 旧新の両方で比較 | 比較[x-state-fifo] | - |
| `EnvironmentGuardTests.test_reused_output_never_executes_previous_guard` | 旧新の両方で比較 | 比較[x-reused-output, basic] | x-reused-output |
| `EnvironmentGuardTests.test_component_full_stat_rejects_restored_content` | 旧新の両方で比較 | 比較[x-component-restored] | - |
| `EnvironmentGuardMoreTests.test_read_components_returns_held_bytes_and_stable_policy_digest` | Rust の検査へ置換 | 実装済み: 内部 reader の旧新比較(全流れの終わりに reader 3 つの型の印つきの値を比べる。比較[options-readers, options, reader-foreign-component, basic])と、state.rs::component_digest_and_descriptors_match_python_3_10 | - |
| `EnvironmentGuardMoreTests.test_component_tree_follows_multihop_directory_link_and_rejects_cycle` | Rust の検査へ置換 | 実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input(component の木)と 比較[x-multihop-managed-roots, x-install-link-official] | - |
| `EnvironmentGuardMoreTests.test_component_tree_allows_skill_install_link_but_not_agent_link` | Rust の検査へ置換 | 実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と 比較[x-install-link-official, x-install-link-agent-kind] | x-install-link-agent-kind |
| `EnvironmentGuardMoreTests.test_empty_directory_budget_and_file_budget` | Rust の検査へ置換 | 実装済み: budget.rs::tick_stops_just_after_ten_thousand_items と budget.rs::reserve_checks_the_file_limit_just_before_and_after と 比較[x-limit-entries-after, x-settings-size-over] | x-limit-entries-after, x-settings-size-over |
| `EnvironmentGuardMoreTests.test_parent_replacement_does_not_read_external_secret` | Rust の検査へ置換 | 実装済み: fsio.rs::a_swapped_parent_is_rejected_after_the_body と race_tests.rs::a_subdirectory_swapped_between_lstat_and_open_is_refused と 比較[x-race-verify-parent-swap, x-parent-swap-host-settings] | - |
| `EnvironmentGuardMoreTests.test_shell_and_xdg_settings_change` | 旧新の両方で比較 | 比較[verify-shell-created, verify-xdg-config-created] | - |
| `EnvironmentGuardMoreTests.test_active_plugin_inventory_and_content_change` | 旧新の両方で比較 | 比較[options](verify --current-inventory)と、実装済みの pyjson の plugin_inventory の固定期待 | - |
| `EnvironmentGuardMoreTests.test_direct_read_exec_no_original_import` | 旧新の両方で比較 | read・exec/direct(予定) | - |
| `EnvironmentGuardMoreTests.test_replaced_external_receipt_is_not_an_isolation_boundary` | 旧新の両方で比較 | 比較[x-receipt-replaced] | - |
| `HostSettingsTests.test_permission_sources_preserve_identity_presence_and_private_values` | Rust の検査へ置換 | 実装済み: settings.rs::read_host_settings_cases_match_python_3_10 と 内部 reader の旧新比較(source の値を含む型の印つきの値の一致。比較[options-readers]) | - |
| `HostSettingsTests.test_permission_reader_keeps_bash_conversion_cli_user_only` | Rust の検査へ置換 | 実装済み: settings::host_allowlist_cases_match_python_3_10 と rule_cases | - |
| `HostSettingsTests.test_host_paths_are_absolute_nfc_and_cwd_independent` | 旧新の両方で比較 | 実装済み: check_s2e.py の host-paths/env/* | - |
| `HostSettingsTests.test_forbidden_setting_env_and_helpers_are_rejected_without_values` | Rust の検査へ置換 | 実装済み: settings.rs::check_host_setting_cases_match_python_3_10 と 比較[host-env-home, host-env-source, host-env-managed, host-policy-helper, host-hooks-disabled](値を出さない確認は host-settings の標準出力・標準エラーの一致) | - |
| `HostSettingsTests.test_strict_json_and_verified_allowlist` | Rust の検査へ置換 | 実装済み: pyjson の strict_settings の固定期待と settings::host_allowlist_cases | - |
| `HostSettingsTests.test_setting_change_after_boot_is_rejected` | 旧新の両方で比較 | 比較[verify-settings-edit, verify-settings-remove, verify-settings-to-link] | - |
| `HostSettingsTests.test_reader_rechecks_bytes_mode_missing_and_link_after_verify` | Rust の検査へ置換 | 実装済み: settings.rs::read_host_settings_cases_match_python_3_10(順序)と 比較[x-race-host-settings-settings-edit, x-race-host-settings-settings-fifo, x-race-host-settings-settings-link, x-race-host-settings-parent-swap] | - |
| `HostSettingsTests.test_reader_rechecks_final_link_set_for_user_cache_and_managed` | Rust の検査へ置換 | 実装済み: settings::read_host_settings_cases の @file-link の照合 | - |
| `HostSettingsTests.test_parser_budget_and_unused_output_are_checked_after_parse` | Rust の検査へ置換 | 実装済み: settings::allowlist_rules_trim_unused_lines_to_300_after_replacing_controls と host_allowlist_cases | - |
| `HostSettingsTests.test_host_setting_descriptors_are_paths_and_kinds_only` | Rust の検査へ置換 | 実装済み: settings::host_settings_specs_cases と read_host_settings_cases の不正な項目 | - |
| `HostSettingsTests.test_single_setting_symlink_is_supported_but_dropin_tree_symlink_is_rejected` | 旧新の両方で比較 | 比較[x-settings-single-link, x-dropin-tree-link, settings-link] | x-dropin-tree-link |
| `HostSettingsTests.test_settings_parent_swap_fifo_mode_and_budget_reject` | 旧新の両方で比較 | 比較[x-parent-swap-host-settings, x-special-settings-fifo, x-special-settings-mode-0200, x-settings-size-over, x-settings-size-limit, x-config-dir-fifo] | x-special-settings-fifo, x-special-settings-mode-0200, x-settings-size-over |
| `HostSettingsTests.test_managed_dropin_names_match_the_host_scope` | Rust の検査へ置換 | 実装済み: settings::host_settings_specs_cases(並べ替えと名前の規則) | - |
| `HostSettingsTests.test_new_managed_dropin_between_snapshot_and_descriptors_is_rejected` | Rust の検査へ置換 | 比較[x-race-bootstrap-new-managed-dropin, x-race-verify-new-managed-dropin](PATH の偽の git が解析中に書き足す。旧版の回と Rust の回で、終了コード・標準エラー・Git の起動回数・出力フォルダの構成を比べる) | x-race-bootstrap-new-managed-dropin |
| `EnvironmentUnattendedEntryDocumentationTests.test_update_doc_standalone_entry_binds_to_verified_copy_before_modes` | 対象外 | SKILL.md の文書と固定の読み込み器の字面の検査(読み込み器を通す) | - |
| `EnvironmentLauncherTests.test_held_launcher_rejects_replaced_fixed_success_code` | 対象外 | loop.sh 側の固定の読み込み器(Python の字面)の検査。D2 の混在の条件で扱う | - |
| `EnvironmentLauncherTests.test_post_check_execution_gap_is_detected_only_on_next_check` | 旧新の両方で比較 | exec/post-check-gap(予定) | - |
| `EnvironmentIncludePathsTests.test_symlink_dotdot_is_not_lexically_hidden` | 旧新の両方で比較 | 比較[x-git-include-dotdot-symlink] | x-git-include-dotdot-symlink |
| `EnvironmentIncludePathsTests.test_regular_dotdot_include_and_git_path_expansion` | 旧新の両方で比較 | 比較[x-git-include-expansion, include-absolute] | - |
| `InstallationLinksTests.test_official_global_install_and_target_change` | 旧新の両方で比較 | 比較[x-install-link-official, x-install-link-agents] | - |
| `InstallationLinksTests.test_link_retarget_rejected_before_target_content_read` | Rust の検査へ置換 | 実装済み: race_tests.rs::a_link_target_swapped_before_it_is_read_is_refused と 比較[x-install-link-retarget] | - |
| `InstallationLinksTests.test_relative_install_link_and_internal_link_rejection` | 旧新の両方で比較 | 比較[x-install-link-relative] | - |
| `HookAndDirectEntryTests.test_fixed_hook_loader_rejects_permission_and_guard_modification` | 対象外 | 固定の読み込み器と hook の字面の検査(読み込み器を通す) | - |
| `HookAndDirectEntryTests.test_other_host_dynamic_skills_and_settings_directories` | 旧新の両方で比較 | 比較[x-dynamic-directories] | - |
| `ConfigurationLinksTests.test_normal_dotfile_links_and_include_target_change` | 旧新の両方で比較 | 比較[x-dotfile-links, include-link-config] | - |
| `ConfigurationLinksTests.test_dotfile_retarget_rejected_before_content` | Rust の検査へ置換 | 実装済み: race_tests.rs::a_link_target_swapped_before_it_is_read_is_refused と 比較[x-dotfile-retarget] | - |
| `ConfigurationLinksTests.test_dotfile_target_replacement_and_cycle` | 旧新の両方で比較 | 比較[x-dotfile-target-replaced, x-dotfile-cycle] | x-dotfile-cycle |
| `EnvironmentDiagnosticTests.test_timeout_and_oserror_do_not_print_argument_or_path_secret` | Rust の検査へ置換 | 実装済み: error.rs::detail_hides_the_exception_arguments と output.rs::caught_exceptions_become_error_lines_with_exit_20 と 比較[x-special-settings-mode-0200, x-config-dir-fifo] | x-special-settings-mode-0200 |
| `ConfigurationLinkRaceTests.test_target_replacement_between_metadata_and_read_is_not_read` | Rust の検査へ置換 | 実装済み: race_tests.rs::a_file_replaced_after_the_record_lstat_is_refused_before_reading と fsio.rs::a_fifo_swapped_in_after_lstat_is_opened_without_blocking_and_rejected | - |
| `GitConfigOutputTests.test_git_path_expansion_is_bounded_while_reading` | Rust の検査へ置換 | 実装済み: gitconfig.rs::output_is_limited_to_eight_mib_while_it_is_received | - |
| `GitConfigOutputTests.test_stderr_is_discarded_and_timeout_is_fixed` | Rust の検査へ置換 | 実装済み: gitconfig.rs::the_child_runs_in_the_root_directory_with_stderr_discarded と gitconfig.rs::a_slow_child_is_stopped_at_the_deadline_and_killed | - |
| `EnvironmentConfigPrereadTests.test_changed_known_root_stops_before_new_include` | 旧新の両方で比較 | 比較[verify-gitconfig-edit, x-race-verify-gitconfig-edit, x-preread-root-edit-fifo, x-preread-root-edit-0200](読んではならない先を FIFO・権限の無いファイルにして、別の理由で止まらないことで確かめる) | - |
| `EnvironmentConfigPrereadTests.test_new_root_candidate_stops_before_its_body_and_include` | 旧新の両方で比較 | 比較[verify-xdg-config-created, git-global-env, x-preread-new-root-fifo, x-preread-new-root-0200](新しい根の候補自身を FIFO・権限の無いファイルにする) | - |
| `EnvironmentConfigPrereadTests.test_changed_known_include_stops_before_new_nested_target` | 旧新の両方で比較 | 比較[verify-include-target-edit, x-race-verify-include-target-edit, x-preread-include-edit-fifo, x-preread-include-edit-0200] | - |
| `EnvironmentConfigPrereadTests.test_absent_include_candidate_stops_before_new_body` | 旧新の両方で比較 | 比較[include-missing-target, x-race-bootstrap-new-include-target, x-preread-absent-include-fifo, x-preread-absent-include-0200] | x-race-bootstrap-new-include-target |
| `EnvironmentConfigPrereadTests.test_known_nested_and_missing_graph_stays_normal` | 旧新の両方で比較 | 比較[include-chain-two, include-missing-target, x-preread-nested-missing-graph] | - |
| `VerifyPairTests.test_go_after_verified_succeeds_with_nothing_on_stderr` | 旧新の両方で比較 | verify-pair/go(予定) | - |
| `VerifyPairTests.test_go_arriving_in_pieces_still_proceeds` | 旧新の両方で比較 | verify-pair/go-pieces(予定) | - |
| `VerifyPairTests.test_stop_eof_wrong_word_and_unterminated_go_exit_20_silently` | 旧新の両方で比較 | verify-pair/stop-eof-word(予定) | - |
| `VerifyPairTests.test_stop_through_loader_adds_no_false_diagnostic` | 対象外 | 読み込み器を通す(os._exit(20) が読み込み器の変換を迂回する性質の検査)。D2 で扱う | - |
| `VerifyPairTests.test_waiting_for_go_has_a_time_limit` | 旧新の両方で比較 | verify-pair/go-timeout(予定) | - |
| `VerifyPairTests.test_first_failure_never_prints_verified` | 旧新の両方で比較 | verify-pair/first-failure(予定) | - |
| `VerifyPairTests.test_second_check_compares_the_inventory` | 旧新の両方で比較 | verify-pair/second-inventory(予定) | - |
| `VerifyPairTests.test_second_check_reads_the_state_again_and_sees_a_change_between` | 旧新の両方で比較 | verify-pair/change-between(予定) | - |
| `VerifyPairTests.test_guard_body_rewritten_between_the_checks_is_found` | 旧新の両方で比較 | verify-pair/guard-rewritten(予定。U2: コピーを照らす) | - |
| `VerifyPairTests.test_a_different_guard_hash_is_rejected_after_go` | 旧新の両方で比較 | verify-pair/guard-hash(予定) | - |
| `VerifyPairTests.test_bad_arguments_are_rejected_before_any_check` | 旧新の両方で比較 | verify-pair/bad-run-pair-arguments(予定。引数の解析は実装済みの args/verify-pair/* で比べる。解析を通った後の run_pair の検査〈guard の SHA-256 が 64 桁の 16 進でない・--go-timeout が 0 → ERROR の行と 20〉は verify-pair の本体で比べる) | - |
| `VerifyPairTests.test_existing_verify_is_unchanged` | 旧新の両方で比較 | 比較[basic] | - |
| `GitConfigIncludeSkipTests.test_no_include_means_no_git_start_on_verify_but_always_on_bootstrap_snapshot` | Rust の検査へ置換 | 実装済み: command_tests.rs::include_text_is_matched_by_bytes_ignoring_ascii_case と 比較[verify-gitconfig-edit-keeps-include-text, include-comment-upper, basic](Git の起動回数を旧新で比べる) | - |
| `GitConfigIncludeSkipTests.test_include_spellings_still_reach_git_and_follow_the_target` | 旧新の両方で比較 | 比較[include-comment-upper, include-section-mixed, include-quoted, include-absolute](Git の起動回数も比べる) | - |
| `GitConfigIncludeSkipTests.test_include_word_without_a_destination_is_parsed_and_stays_normal` | 旧新の両方で比較 | 比較[verify-gitconfig-edit-keeps-include-text, include-missing-target] | - |
| `GitConfigIncludeSkipTests.test_changed_config_gives_the_same_result_with_or_without_include` | 旧新の両方で比較 | 比較[verify-gitconfig-edit, verify-gitconfig-edit-keeps-include-text] | - |
| `EnvironmentDirectLauncherTests.test_three_entries_and_parent_hold_identical_loader` | 対象外 | 3 つの入口の読み込み器の字面の一致(Python の字面。読み込み器を通す) | - |
| `EnvironmentDirectLauncherTests.test_three_normal_direct_entries_execute_only_matching_bytes` | 対象外 | 読み込み器を通す(照合したバイト列だけを実行する)。D2 で扱う | - |
| `EnvironmentDirectLauncherTests.test_real_bootstrap_and_direct_verify_stay_normal` | 旧新の両方で比較 | bootstrap と verify は CLI で旧新を比べる 比較[basic, x-real-distribution, x-real-distribution-flip]。固定の読み込み器(-I)を通す verify は対象外(D2 で扱う) | - |
| `EnvironmentDirectLauncherTests.test_link_fifo_and_oversize_reject_before_body_open` | 対象外 | 読み込み器自身の拒否(1 MiB 超など。読み込み器を通す) | - |
| `EnvironmentDirectLauncherTests.test_changed_path_after_hash_never_executes_replacement` | 対象外 | 読み込み器を通す(照合と実行の間の差し替え) | - |
| `EnvironmentDirectLauncherTests.test_read_timeout_uses_fixed_diagnostic` | 対象外 | 読み込み器自身の 15 秒の診断 | - |
| `EnvironmentDirectLauncherTests.test_fixed_loader_and_hook_ignore_cwd_and_pythonpath_modules` | 対象外 | 読み込み器の `-I` の性質(Python の内部の観察) | - |
| `EnvironmentDirectLauncherTests.test_profile_keeps_a_normal_user_site_yaml_dependency` | 対象外 | Python の user site と PyYAML 依存の観察(Rust に関係しない) | - |
| `ComponentDescriptorTests.test_plain_scoped_skill_and_direct_scoped_agents` | Rust の検査へ置換 | 実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と 比較[options, x-install-link-agent-kind] | x-install-link-agent-kind |
| `ComponentDescriptorTests.test_absolute_relative_multihop_intermediate_and_dotdot` | Rust の検査へ置換 | 実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と 比較[x-multihop-managed-roots, x-install-link-relative] | - |
| `ComponentDescriptorTests.test_same_physical_target_reads_once_and_keeps_aliases` | Rust の検査へ置換 | 実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input(同じ物理ファイルは 1 回だけ読む。予算の項目数と量を旧版の値と比べる)と 比較[x-multihop-managed-roots] | - |
| `ComponentDescriptorTests.test_cycles_dangling_and_41_links_are_not_missing` | Rust の検査へ置換 | 実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input(導入リンクの 40 と 41 の端を含む) | - |
| `ComponentDescriptorTests.test_fifo_socket_and_nested_links_are_rejected` | Rust の検査へ置換 | 実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と 比較[x-special-fifo-in-plugin, x-install-link-agent-kind] | x-special-fifo-in-plugin, x-install-link-agent-kind |
| `ComponentDescriptorTests.test_all_replacements_stop_before_new_body_read` | Rust の検査へ置換 | 実装済み: race_tests.rs::a_file_rewritten_after_its_stat_was_kept_is_refused_before_reading と race_tests.rs::a_file_replaced_by_a_link_between_the_walk_and_the_read_is_refused と race_tests.rs::a_cached_file_is_still_rechecked_after_its_stat_for_every_logical_name | - |
| `ComponentDescriptorTests.test_changed_link_target_directory_is_not_opened` | Rust の検査へ置換 | 実装済み: race_tests.rs::an_installation_link_target_swapped_during_the_walk_is_refused と race_tests.rs::a_link_target_swapped_before_it_is_read_is_refused | - |
| `ComponentDescriptorTests.test_missing_root_and_entry_creation_reads_no_body` | Rust の検査へ置換 | 実装済み: race_tests.rs::a_manifest_created_between_the_two_missing_checks_is_refused と race_tests.rs::a_file_added_after_the_names_were_compared_stops_without_reading_it と 比較[verify-agents-dir-created, x-enterprise-created] | - |
| `ComponentDescriptorTests.test_post_read_link_replacement_rejects_result` | Rust の検査へ置換 | 実装済み: race_tests.rs::the_link_replaced_after_its_target_was_read_is_refused と race_tests.rs::a_link_target_replaced_after_it_was_read_is_refused | - |
| `ComponentDescriptorTests.test_byte_entry_and_time_budgets_cover_link_walk` | Rust の検査へ置換 | 実装済み: budget.rs::tick_stops_just_after_ten_thousand_items と budget.rs::tick_stops_only_after_the_deadline と observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input(導入リンクの先も同じ予算) | - |
| `ComponentDescriptorTests.test_bootstrap_multihop_and_additive_managed_roots` | 旧新の両方で比較 | 比較[x-multihop-managed-roots, options]。reader の戻りの kind・raw・relative は 内部 reader の旧新比較(比較[options-readers, x-multihop-managed-roots]) | - |
| `ComponentDescriptorTests.test_missing_enterprise_commands_creation_is_rejected` | 旧新の両方で比較 | 比較[x-enterprise-created] | - |
| `ComponentDescriptorTests.test_reader_uses_walker_bytes_and_rejects_old_descriptors` | Rust の検査へ置換 | 実装済み: command_tests.rs::rewritten_state_flows_match_the_old_python_3_10 と 比較[legacy-state, reader-foreign-component](reader は旧い記述子の状態を拒否する) | - |
| `ComponentDescriptorTests.test_policy_digest_excludes_all_runtime_identity` | Rust の検査へ置換 | 実装済み: state::component_digest_and_descriptors_match_python_3_10 と digest_depth | - |
| `ComponentDescriptorTests.test_selected_marketplace_is_held_without_inactive_tree` | Rust の検査へ置換 | 実装済み: inventory.rs::component_inventory_cases_match_python_3_10(full・manifest-*)と 比較[options] | - |
| `ComponentDescriptorTests.test_marketplace_missing_ambiguous_and_external_sources_fail` | Rust の検査へ置換 | 実装済み: inventory::component_inventory_cases(manifest-*・installed-*) | - |
| `ComponentDescriptorTests.test_synced_and_local_inventory_do_not_need_marketplace_registry` | Rust の検査へ置換 | 実装済み: inventory::component_inventory_cases(synced・no-marketplace) | - |
| `ComponentDescriptorTests.test_skills_dir_inventory_merges_only_exact_held_root_and_manifest` | Rust の検査へ置換 | 実装済み: inventory::component_inventory_cases(skills-dir) | - |
| `ComponentDescriptorTests.test_skills_dir_rejects_unknown_ids_wrong_name_missing_manifest_and_outside` | Rust の検査へ置換 | 実装済み: inventory::component_inventory_cases(skills-dir-*) | - |
| `ComponentDescriptorTests.test_skills_dir_outside_and_ambiguous_sources_are_rejected_without_opening_target` | Rust の検査へ置換 | 実装済み: inventory.rs::component_inventory_cases_match_python_3_10(skills-dir-two-candidates)と race_tests.rs::a_link_target_swapped_before_it_is_read_is_refused | - |
| `ComponentDescriptorTests.test_skills_dir_installation_link_uses_held_chain_and_rejects_replacement` | Rust の検査へ置換 | 実装済み: race_tests.rs::an_installation_link_replaced_after_the_walk_is_refused と race_tests.rs::an_installation_link_replaced_after_its_walk_is_refused と 比較[x-install-link-retarget] | - |
| `ComponentDescriptorTests.test_skills_dir_reordering_preserves_missing_descriptors_and_shared_budget` | Rust の検査へ置換 | 実装済み: inventory.rs::component_inventory_cases_match_python_3_10 と budget.rs::tick_stops_just_after_ten_thousand_items と 比較[options] | - |
| `ComponentDescriptorTests.test_legacy_state_verify_retains_previous_contract` | 旧新の両方で比較 | 比較[legacy-state, legacy-state-edit-plugin-file, legacy-state-copy-edit, legacy-state-settings-edit, legacy-state-gitconfig-edit, legacy-state-inventory, legacy-state-inventory-edit-install, legacy-state-inventory-add-install, legacy-state-inventory-remove-install](旧い状態〈version 3 の記述子なし〉の verify と、reader が旧い状態を拒否すること) | - |
| `ComponentDescriptorTests.test_exactly_40_links_are_allowed` | Rust の検査へ置換 | 実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と depth.rs::checks_fail_just_above_the_limit_with_the_old_messages(導入リンクの 40 と 41) | - |
| `ComponentDescriptorTests.test_legacy_comparison_reads_replacement_but_component_stops_first` | Rust の検査へ置換 | 実装済み: race_tests.rs::a_file_rewritten_after_its_stat_was_kept_is_refused_before_reading と 比較[legacy-state-edit-plugin-file] | - |
| `ComponentDescriptorTests.test_unreadable_file_and_agent_install_link_rejected` | Rust の検査へ置換 | 比較[x-install-link-agent-kind, x-special-settings-mode-0200] | x-install-link-agent-kind, x-special-settings-mode-0200 |
| `ComponentDescriptorTests.test_registry_wrong_path_and_duplicate_active_id_rejected` | Rust の検査へ置換 | 実装済み: inventory::component_inventory_cases(installed-wrong-path)と pyjson の plugin_inventory(重複 ID) | - |
| `ComponentDescriptorTests.test_two_selected_entries_share_manifest_without_losing_sources` | Rust の検査へ置換 | 実装済み: inventory::component_inventory_cases(manifest-existing-*) | - |
| `ComponentDescriptorTests.test_missing_manifest_creation_before_return_is_rejected` | Rust の検査へ置換 | 実装済み: race_tests.rs::a_manifest_created_between_the_two_missing_checks_is_refused | - |
| `ComponentDescriptorTests.test_policy_digest_normalizes_only_workflow_run_copy_root` | Rust の検査へ置換 | 実装済み: state の digest の固定期待(workflow の根だけ @workflow) | - |
| `ComponentDescriptorTests.test_policy_digest_ignores_missing_parent_location_but_tracks_component` | Rust の検査へ置換 | 実装済み: state の digest の固定期待(@route の除外) | - |
| `CopyCommandCharacterizationTests.test_success_output_and_return_values` | 旧新の両方で比較 | read の生バイト・exec の子の終了コード・-B・PYTHONDONTWRITEBYTECODE・bash の引数(予定。CLI で比べる) | - |
| `CopyCommandCharacterizationTests.test_stop_lines` | 旧新の両方で比較 | read・exec・hook の run_copy の理由の行(予定。Rust にはまだ無い。output::main_cases の変換は実装済みだが、理由の行は本体と一緒に比べる) | - |
| `CopyCommandCharacterizationTests.test_seam_order` | 旧新の両方で比較 | bootstrap・verify の拒否の順と観測の順は 比較[order-files, order-files-before-directories, order-directories-before-components, order-components-before-configs, verify-state-edit, x-race-verify-settings-edit, x-race-bootstrap-settings-edit](CLI で比べる。モンキーパッチは使っていない)。read・exec の拒否の順は次の段(予定) | x-race-bootstrap-settings-edit |
| `CopyCommandCharacterizationTests.test_hook_runs_copy_in_same_process` | 対象外 | U3: Rust の hook は確認の後に終了コード 20。Python の同じプロセスでの exec の観察 | - |
| `CopyCommandCharacterizationTests.test_hook_exceptions_are_caught_by_main` | Rust の検査へ置換 | 実装済み: output::main_cases_match_python_3_10(例外の変換の部分だけ)。hook の本体を Python が exec する経路は U3 の差。Rust は確認の後に 20 を返すので、2 段目 b で U3 の予定の行へ移す(予定) | - |
