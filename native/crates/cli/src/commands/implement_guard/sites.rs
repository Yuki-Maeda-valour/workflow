//! 失敗の場所の表。旧版(sha256 `b958cc12…`)の行番号と、その行の字面の一部と、ERR の罠が働くか。
//! bash の文(`implement-guard.sh: line <行>: …`)と ERR の罠の行(`…(終了コード <値>・行 <行>)`)は、
//! この表の行番号で出す。Rust のソースの行番号へ置き換えない。単体テストが、旧版のその行に字面が
//! あることを確かめる(`tests.rs`)。
// 表だけに置く場所(道具の地点・ERR の働かない所)は、行の検査と固定期待で使う。
#![allow(dead_code)]

/// 失敗の場所。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Site {
    /// 表の名前(固定期待と差し込み点で使う)。
    pub name: &'static str,
    /// 旧版の行番号(bash の文の `line <行>` と ERR の罠の行)。
    pub line: u32,
    /// ERR の罠の `$LINENO`(多くは `line` と同じ。ループのリダイレクトは違う)。
    pub err_line: u32,
    /// `line` の行に含まれる字面の一部。
    pub text: &'static str,
    /// 失敗したときに ERR の罠が働くか(`if`・`||` の中では働かない)。
    pub errexit: bool,
}

const fn site(name: &'static str, line: u32, text: &'static str, errexit: bool) -> Site {
    Site {
        name,
        line,
        err_line: line,
        text,
        errexit,
    }
}

// ---- 起動・罠・使い方 ----
pub const DEV_NULL: Site = site("dev-null", 218, "exec </dev/null", true);
pub const ON_SIGNAL_PRINTF: Site = site(
    "on-signal-printf",
    318,
    "printf 'RESTORED=no\\nTOUCHED=%s\\nTEMP=none\\nREASON=interrupted\\n'",
    true,
);
pub const ERR_TRAP_ECHO: Site = site(
    "err-trap-echo",
    324,
    "trap 'ec=$?; echo \"ERROR [internal]",
    true,
);
pub const USAGE: Site = site(
    "usage",
    327,
    "usage() { sed -n '2,/^# --- end usage ---$/p'",
    true,
);
pub const FAIL_USAGE_ECHO: Site = site(
    "fail-usage-echo",
    329,
    "fail_usage() { echo \"ERROR [usage] $*\" >&2",
    true,
);
pub const FAIL_INTERNAL_ECHO: Site = site(
    "fail-internal-echo",
    330,
    "fail_internal() { echo \"ERROR [internal] $*\" >&2",
    true,
);
pub const ARG_ERROR_ECHO: Site = site(
    "arg-error-echo",
    350,
    "arg_error() { echo \"ERROR [usage] $*\" >&2; usage;",
    true,
);
pub const NEED_VAL_ECHO: Site = site(
    "need-val-echo",
    360,
    "echo \"ERROR [usage] $1 に値が必要です\" >&2",
    true,
);
pub const NEED_VAL_USAGE: Site = site("need-val-usage", 361, "    usage", true);
pub const HELP_SUBCOMMAND: Site = site(
    "help-subcommand",
    3059,
    "-h|--help) usage; trap - ERR; exit 0 ;;",
    true,
);
pub const HELP_ARGUMENT: Site = site(
    "help-argument",
    3070,
    "-h|--help) usage; trap - ERR; exit 0 ;;",
    true,
);

// ---- 記録の読み取り ----
pub const CUNQUOTE_PRINTF: Site = site("cunquote-printf", 485, "printf -v ch \"\\\\$oct\"", true);
pub const STATE_FAIL_CONFIG: Site = site(
    "state-fail-config-check",
    2256,
    "config-check) echo \"ERROR [$reason] $*\" >&2; printf 'RESULT=incomparable\\nREASON=%s\\nGIT_SKIPPED=yes\\n'",
    true,
);
pub const STATE_FAIL_COMPARE: Site = site(
    "state-fail-compare",
    2257,
    "compare) echo \"ERROR [$reason] $*\" >&2; emit_incomparable \"$reason\"",
    true,
);
pub const STATE_FAIL_TASKMD: Site = site(
    "state-fail-taskmd-diff",
    2258,
    "taskmd-diff) echo \"ERROR [digest] $*\" >&2; printf 'REASON=digest\\n'",
    true,
);
pub const STATE_FAIL_RESTORE: Site = site(
    "state-fail-restore-taskmd",
    2259,
    "restore-taskmd) echo \"ERROR [digest] $*\" >&2; printf 'RESTORED=no\\nTOUCHED=none\\nTEMP=none\\nREASON=digest\\n'",
    true,
);
/// `load_meta` の 1 つ目のループのリダイレクト。bash は関数の始まりの行で文を出し、ERR の罠は
/// 直前の単純な命令(`META=()`)の行になる(基準の容器で測った)。
pub const LOAD_META_OPEN_META: Site = Site {
    name: "load-meta-open-4-meta",
    line: 2281,
    err_line: 2283,
    text: "load_meta() {",
    errexit: true,
};
/// `load_meta` の 2 つ目のループのリダイレクト(ERR の罠は `TM=()` の行)。
pub const LOAD_META_OPEN_TASKMD: Site = Site {
    name: "load-meta-open-taskmd",
    line: 2281,
    err_line: 2290,
    text: "load_meta() {",
    errexit: true,
};
pub const LOAD_META_NOTE: Site = site(
    "load-meta-note",
    2318,
    "echo \"NOTE: --cwd が存在しない",
    true,
);
pub const VERIFY_TASKMD_READ: Site = site(
    "verify-taskmd-read",
    2333,
    "IFS=$'\\t' read -r kind mode val pf <\"$STATE/manifest.tsv\" || return 1",
    false,
);
pub const EMIT_INCOMPARABLE_HEAD: Site = site(
    "emit-incomparable-head",
    2680,
    "printf 'RESULT=incomparable\\nWORKTREE_CHANGED=unknown\\nGITMETA_CHANGED=%s\\n' \"$gm\"",
    true,
);
pub const EMIT_INCOMPARABLE_REASON: Site = site(
    "emit-incomparable-reason",
    2681,
    "printf 'REASON=%s\\n' \"$1\"",
    true,
);
pub const EMIT_INCOMPARABLE_SKIPPED: Site = site(
    "emit-incomparable-skipped",
    2682,
    "then printf 'GIT_SKIPPED=yes\\n'",
    true,
);

// ---- 一時フォルダ ----
pub const MKTEMP_CONFIG_CHECK: Site = site(
    "mktemp-config-check",
    2719,
    "WORK=\"$(mktemp -d \"$STATE/.work-XXXXXX\")\"",
    false,
);
pub const MKTEMP_COMPARE: Site = site(
    "mktemp-compare",
    2884,
    "WORK=\"$(mktemp -d \"$STATE/.work-XXXXXX\")\"",
    false,
);
pub const MKTEMP_TASKMD: Site = site(
    "mktemp-taskmd-diff",
    2934,
    "WORK=\"$(mktemp -d \"$STATE/.work-XXXXXX\")\"",
    false,
);

// ---- taskmd-diff ----
pub const TASKMD_BODY_ECHO: Site =
    site("taskmd-body-echo", 2937, "echo \"ERROR [taskmd-body]", true);
pub const TASKMD_BODY_PRINTF: Site = site(
    "taskmd-body-printf",
    2938,
    "printf 'REASON=taskmd-body\\n'",
    true,
);
pub const MAPPING_ECHO: Site = site(
    "mapping-echo",
    2948,
    "echo \"NOTE: タスク MD の選択パスと実体の対応が変わった",
    true,
);
pub const MAPPING_PRINTF: Site = site(
    "mapping-printf",
    2949,
    "printf 'TASKMD=mapping-changed\\nDOD=%s\\n'",
    true,
);
pub const SAME_PRINTF: Site = site(
    "same-printf",
    2955,
    "printf 'TASKMD=same\\nDOD=%s\\n'",
    true,
);
pub const STAT_SIZE_BEFORE: Site = site(
    "stat-size-before",
    2961,
    "s1=\"$(stat_size \"$STATE/taskmd-body\")\" || s1=\"a\"",
    false,
);
pub const STAT_SIZE_AFTER: Site = site(
    "stat-size-after",
    2962,
    "s2=\"$(stat_size \"$real_now\")\" || s2=\"b\"",
    false,
);
pub const SED_BEFORE: Site = site(
    "sed-before",
    2963,
    "LC_ALL=C sed -E \"$NORM_SED\" <\"$STATE/taskmd-body\" >\"$WORK/before.norm\"",
    true,
);
pub const SED_AFTER: Site = site(
    "sed-after",
    2964,
    "LC_ALL=C sed -E \"$NORM_SED\" <\"$real_now\" >\"$WORK/after.norm\"",
    true,
);
pub const CHECKS_ONLY_PRINTF: Site = site(
    "checks-only-printf",
    2966,
    "printf 'TASKMD=checks-only\\nDOD=%s\\n'",
    true,
);
pub const BODY_CHANGED_ECHO: Site = site(
    "body-changed-echo",
    2970,
    "echo \"NOTE: タスク MD の要件本文が変わっている",
    true,
);
pub const BODY_CHANGED_PRINTF: Site = site(
    "body-changed-printf",
    2971,
    "printf 'TASKMD=body-changed\\nDOD=%s\\n'",
    true,
);

// ---- restore-taskmd ----
pub const RESTORE_FAIL_ECHO: Site = site(
    "restore-fail-echo",
    2979,
    "echo \"ERROR [$reason] $*\" >&2",
    true,
);
pub const RESTORE_FAIL_PRINTF: Site = site(
    "restore-fail-printf",
    2980,
    "printf 'RESTORED=no\\nTOUCHED=%s\\nTEMP=%s\\nREASON=%s\\n'",
    true,
);
pub const RESTORE_READ: Site = site(
    "restore-read",
    3002,
    "if ! IFS=$'\\t' read -r kind mode val pf extra <\"$STATE/manifest.tsv\"; then",
    false,
);

// ---- cleanup と所属の印 ----
pub const MARK_UNCERTAIN_ECHO: Site = site(
    "mark-uncertain-echo",
    2527,
    "所属の印が残ったか確かめられない",
    true,
);
pub const MARK_NOT_OURS_ECHO: Site = site(
    "mark-not-ours-echo",
    2534,
    "確かめられないものが在る(消さない)",
    true,
);
pub const MARK_REMOVE_FAIL_ECHO: Site = site(
    "mark-remove-fail-echo",
    2538,
    "所属の印を消せない(機密を含まないので手で消してよい)",
    true,
);
pub const MARK_REMOVED_ECHO: Site = site(
    "mark-removed-echo",
    2542,
    "echo \"NOTE: 所属の印を消した:",
    true,
);
pub const CLEANUP_RM: Site = site(
    "cleanup-rm",
    3050,
    "rm -rf -- \"$STATE\" || fail_internal",
    false,
);
pub const CLEANUP_NOTE: Site = site("cleanup-note", 3051, "echo \"NOTE: 保護領域を消した:", true);

/// 表の全部(単体テストが旧版の行と照らす)。
pub const ALL: &[Site] = &[
    DEV_NULL,
    ON_SIGNAL_PRINTF,
    ERR_TRAP_ECHO,
    USAGE,
    FAIL_USAGE_ECHO,
    FAIL_INTERNAL_ECHO,
    ARG_ERROR_ECHO,
    NEED_VAL_ECHO,
    NEED_VAL_USAGE,
    HELP_SUBCOMMAND,
    HELP_ARGUMENT,
    CUNQUOTE_PRINTF,
    STATE_FAIL_CONFIG,
    STATE_FAIL_COMPARE,
    STATE_FAIL_TASKMD,
    STATE_FAIL_RESTORE,
    LOAD_META_OPEN_META,
    LOAD_META_OPEN_TASKMD,
    LOAD_META_NOTE,
    VERIFY_TASKMD_READ,
    EMIT_INCOMPARABLE_HEAD,
    EMIT_INCOMPARABLE_REASON,
    EMIT_INCOMPARABLE_SKIPPED,
    MKTEMP_CONFIG_CHECK,
    MKTEMP_COMPARE,
    MKTEMP_TASKMD,
    TASKMD_BODY_ECHO,
    TASKMD_BODY_PRINTF,
    MAPPING_ECHO,
    MAPPING_PRINTF,
    SAME_PRINTF,
    STAT_SIZE_BEFORE,
    STAT_SIZE_AFTER,
    SED_BEFORE,
    SED_AFTER,
    CHECKS_ONLY_PRINTF,
    BODY_CHANGED_ECHO,
    BODY_CHANGED_PRINTF,
    RESTORE_FAIL_ECHO,
    RESTORE_FAIL_PRINTF,
    RESTORE_READ,
    MARK_UNCERTAIN_ECHO,
    MARK_NOT_OURS_ECHO,
    MARK_REMOVE_FAIL_ECHO,
    MARK_REMOVED_ECHO,
    CLEANUP_RM,
    CLEANUP_NOTE,
];
