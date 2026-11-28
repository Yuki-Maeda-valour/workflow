//! 失敗の場所の表。旧版(sha256 `9486ca07…`)の行番号と、その行の字面の一部と、ERR の罠が働くか。
//! bash の文(`implement-agent.sh: line <行>: …`)と ERR の罠の行(`…(終了コード <値>・行 <行>)`)は、
//! この表の行番号で出す。Rust のソースの行番号へ置き換えない。単体テストが、旧版のその行に字面が
//! あることを確かめる(`tests.rs`)。
// 表だけに置く場所(R4X-2 以降で使う行・ERR の働かない所)は、行の検査と固定期待で使う。
#![allow(dead_code)]

/// 失敗の場所。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Site {
    /// 表の名前(固定期待と差し込み点で使う)。
    pub name: &'static str,
    /// 旧版の行番号(bash の文の `line <行>` と ERR の罠の行)。
    pub line: u32,
    /// `line` の行に含まれる字面の一部。
    pub text: &'static str,
    /// 失敗したときに ERR の罠が働くか(`if`・`||` の中では働かない)。
    pub errexit: bool,
}

const fn site(name: &'static str, line: u32, text: &'static str, errexit: bool) -> Site {
    Site {
        name,
        line,
        text,
        errexit,
    }
}

// ---- 起動・罠・終わりの部品 ----
pub const EXEC_SAVE: Site = site("exec-save", 136, "exec 9>&1 8>&2", true);
pub const EXIT_TRAP: Site = site("exit-trap", 152, "trap cleanup EXIT", true);
pub const DIE_ECHO: Site = site("die-echo", 275, "echo \"ERROR [$reason] $*\" >&2", true);
pub const ON_ERR_ECHO: Site = site(
    "on-err-echo",
    290,
    "echo \"ERROR [internal] 予期しない失敗(終了コード $oe_code・行 $oe_line)\" >&8 || true",
    false,
);
pub const ERR_TRAP: Site = site(
    "err-trap",
    305,
    "trap 'on_err \"$?\" \"$LINENO\"' ERR",
    true,
);
pub const USAGE: Site = site(
    "usage",
    308,
    "usage() { sed -n '2,/^# --- end usage ---$/p' \"$0\" | sed '$d' >&2; }",
    true,
);
pub const NEED_VAL_ECHO: Site = site(
    "need-val-echo",
    312,
    "echo \"ERROR [usage] $1 に値が必要です\" >&2",
    true,
);
pub const NEED_VAL_USAGE: Site = site("need-val-usage", 313, "    usage", true);
pub const UNKNOWN_ECHO: Site = site(
    "unknown-echo",
    330,
    "*) echo \"ERROR [usage] 不明な引数: $1\" >&2; usage; trap - ERR; exit 2 ;;",
    true,
);
pub const FAIL_USAGE_ECHO: Site = site(
    "fail-usage-echo",
    334,
    "fail_usage() { echo \"ERROR [usage] $*\" >&2; trap - ERR; exit 2; }",
    true,
);

// ---- 値の検査 ----
pub const PROMPT_FILE_TEST: Site = site("prompt-file-test", 348, "[ -f \"$PROMPT_FILE\" ]", false);
pub const GREP: Site = site(
    "grep",
    352,
    "if ! LC_ALL=C grep -q '[^[:space:]]' \"$PROMPT_FILE\" 2>/dev/null; then",
    false,
);
/// 352 行の `if` の終わり。一時的な `LC_ALL=C` を戻すときの setlocale の警告と、起動した `grep` が
/// 信号で死んだときの知らせの行は、この行番号で出る(基準の容器で測った)。
pub const GREP_END: Site = site("grep-end", 354, "fi", false);
pub const PROBE_TIMEOUT_TEST: Site = site(
    "probe-timeout-test",
    364,
    "[ \"$PROBE_TIMEOUT\" -gt 0 ] || fail_usage",
    false,
);
pub const RUN_TIMEOUT_TEST: Site = site(
    "run-timeout-test",
    365,
    "[ \"$RUN_TIMEOUT\" -gt 0 ] || fail_usage",
    false,
);

// ---- --log-file ----
pub const PWD_P: Site = site("pwd-p", 387, "PHYS_PWD=\"$(pwd -P)\"", true);
pub const DIRNAME: Site = site("dirname", 390, "LOG_DIR=\"$(dirname \"$LOG_FILE\")\"", true);
pub const BASENAME: Site = site(
    "basename",
    391,
    "LOG_NAME=\"$(basename \"$LOG_FILE\")\"",
    true,
);
pub const LOG_ENTRY: Site = site(
    "log-entry",
    396,
    "fail_usage \"--log-file のパスに既にエントリが在る: $LOG_NORM",
    true,
);

// ---- 既定表 ----
pub const NOT_IN_TABLE: Site = site(
    "not-in-table",
    443,
    "fail_usage \"ランナー '$RUNNER' は実装用既定表に無い",
    true,
);
pub const WRITE_FLAG_SHAPE: Site = site(
    "write-flag-shape",
    452,
    "[ \"${#WRITE_TOKENS[@]}\" -ge 2 ] || die 20 internal",
    true,
);
pub const PROBE_FLAG_SHAPE: Site = site(
    "probe-flag-shape",
    453,
    "[ \"${#PROBE_TOKENS[@]}\" -ge 2 ] || die 20 internal",
    true,
);
pub const TIMEOUT_CHECK: Site = site(
    "timeout-check",
    473,
    "if command -v timeout >/dev/null 2>&1 && timeout -k 1 1 true >/dev/null 2>&1; then",
    false,
);

// ---- 中止 ----
pub const ON_SIGNAL_ECHO: Site = site(
    "on-signal-echo",
    513,
    "echo \"ERROR [aborted] シグナル $os_sig を受信したため中止した",
    true,
);
pub const TRAP_TERM: Site = site("trap-term", 517, "trap 'on_signal TERM 143' TERM", true);
pub const TRAP_HUP: Site = site("trap-hup", 518, "trap 'on_signal HUP 129' HUP", true);
pub const TRAP_INT: Site = site("trap-int", 519, "trap 'on_signal INT 130' INT", true);

// ---- 保護領域の候補 ----
pub const REAL_DIR: Site = site(
    "real-dir",
    699,
    "real_dir() { ( cd \"$1\" 2>/dev/null && pwd -P ) || return 1; }",
    false,
);
pub const EXCL_TMP: Site = site(
    "excl-tmp",
    705,
    "EXCL_TMP=\"$(real_dir /tmp || true)\"",
    false,
);
pub const EXCL_TMPDIR: Site = site(
    "excl-tmpdir",
    707,
    "EXCL_TMPDIR=\"$(real_dir \"$TMPDIR\" || true)\"",
    false,
);
pub const EXCL_CWD: Site = site(
    "excl-cwd",
    708,
    "EXCL_CWD=\"$(real_dir \"$CWD\")\" || die 20 internal",
    true,
);
pub const CANDIDATE_CASE: Site = site(
    "candidate-case",
    712,
    "case \"$cand\" in \"\"|\"/.local/state\"|\"/.cache\") continue ;; esac",
    false,
);
/// 境界(最初の候補の `mkdir -p`)。
pub const MKDIR: Site = site(
    "mkdir",
    715,
    "mkdir -p \"$cand\" 2>/dev/null || continue",
    false,
);
pub const NO_STATE_BASE: Site = site(
    "no-state-base",
    725,
    "[ -n \"$STATE_BASE\" ] || die 20 internal",
    true,
);

/// 表の全部(単体テストが旧版の行と照らす)。
pub const ALL: &[Site] = &[
    EXEC_SAVE,
    EXIT_TRAP,
    DIE_ECHO,
    ON_ERR_ECHO,
    ERR_TRAP,
    USAGE,
    NEED_VAL_ECHO,
    NEED_VAL_USAGE,
    UNKNOWN_ECHO,
    FAIL_USAGE_ECHO,
    PROMPT_FILE_TEST,
    GREP,
    GREP_END,
    PROBE_TIMEOUT_TEST,
    RUN_TIMEOUT_TEST,
    PWD_P,
    DIRNAME,
    BASENAME,
    LOG_ENTRY,
    NOT_IN_TABLE,
    WRITE_FLAG_SHAPE,
    PROBE_FLAG_SHAPE,
    TIMEOUT_CHECK,
    ON_SIGNAL_ECHO,
    TRAP_TERM,
    TRAP_HUP,
    TRAP_INT,
    REAL_DIR,
    EXCL_TMP,
    EXCL_TMPDIR,
    EXCL_CWD,
    CANDIDATE_CASE,
    MKDIR,
    NO_STATE_BASE,
];
