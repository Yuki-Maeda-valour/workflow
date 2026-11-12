//! 旧`reviews-dir.sh`の処理地点の行番号の固定表と、旧bashが子の終わり方を知らせる行に出すコマンドの字面。
//!
//! 行番号はRustのソースからではなく、旧スクリプトの行から取る。各行は旧スクリプトのその行にあるべき字面を持ち、
//! 旧スクリプトと食い違えば検査で止める。コマンドの字面は、旧bashが構文木から作り直す文
//! (`make_command_string`)で、旧版の実測と同じ。

use super::super::r4g_support::bash::{LinePoint, LineTable};

pub const LINES: LineTable = LineTable {
    points: &[
        LinePoint {
            name: "cleanup",
            line: 35,
            fingerprint: "rm -f -- \"$TMP\"",
        },
        LinePoint {
            name: "usage",
            line: 39,
            fingerprint: "sed -n '2,/^# --- end usage ---$/p' \"$0\" | sed '$d' >&2",
        },
        LinePoint {
            name: "fail_usage",
            line: 40,
            fingerprint: "echo \"ERROR [usage] $*\" >&2",
        },
        LinePoint {
            name: "die",
            line: 44,
            fingerprint: "echo \"ERROR [$reason] $*\" >&2",
        },
        LinePoint {
            name: "sha256sum",
            line: 59,
            fingerprint: "sha256sum <\"$1\" | cut -d' ' -f1",
        },
        LinePoint {
            name: "shasum",
            line: 60,
            fingerprint: "shasum -a 256 <\"$1\" | cut -d' ' -f1",
        },
        LinePoint {
            name: "openssl",
            line: 61,
            fingerprint: "openssl dgst -sha256 <\"$1\" | sed 's/.*[= ]//'",
        },
        LinePoint {
            name: "ensure_for",
            line: 69,
            fingerprint: "for d in \"$1/.claude\" \"$1/.claude/reviews\"; do",
        },
        LinePoint {
            name: "mkdir",
            line: 74,
            fingerprint: "mkdir -- \"$d\" || die 20 internal",
        },
        LinePoint {
            name: "python_check",
            line: 135,
            fingerprint: "if python3 -I -c 'import os' >/dev/null 2>&1; then",
        },
        LinePoint {
            name: "mktemp",
            line: 139,
            fingerprint: "TMP=\"$(mktemp \"$REVIEWS/.base-untracked.XXXXXX\")\"",
        },
        LinePoint {
            name: "git",
            line: 141,
            fingerprint: "git -C \"$TOP\" \"${GIT_PRE[@]}\" ls-files -o --exclude-standard -z >\"$TMP\" || rc=$?",
        },
        LinePoint {
            name: "sha",
            line: 143,
            fingerprint: "SHA=\"$(sha256_file \"$TMP\")\"",
        },
        // 複数行のコマンド置換(148〜153行)の中の診断とNULの警告は、旧bashでは終わりの153行。
        LinePoint {
            name: "publish_python",
            line: 153,
            fingerprint: "sys.exit(1)' \"$TMP\" \"$LIST\" 2>&1)\" || die 20 internal",
        },
        LinePoint {
            name: "publish_mv",
            line: 155,
            fingerprint: "reason=\"$(mv -f -T -- \"$TMP\" \"$LIST\" 2>&1)\"",
        },
        LinePoint {
            name: "printf",
            line: 159,
            fingerprint: "printf '%s\\n' \"$SHA\"",
        },
    ],
};

/// 行番号(表に無い名前は実装の誤り)。
pub fn line(name: &str) -> u32 {
    LINES
        .line(name)
        .unwrap_or_else(|| panic!("行の固定表に無い地点: {name}"))
}

/// 旧bashが作り直すコマンドの字面(子が信号で終わったときの行に出る)。
pub mod text {
    pub const GIT: &str =
        "git -C \"$TOP\" \"${GIT_PRE[@]}\" ls-files -o --exclude-standard -z > \"$TMP\"";
    pub const MKDIR: &str = "mkdir -- \"$d\"";
    pub const PYTHON_CHECK: &str = "python3 -I -c 'import os' > /dev/null 2>&1";
    pub const USAGE_FIRST: &str = "sed -n '2,/^# --- end usage ---$/p' \"$0\"";
    pub const USAGE_SECOND: &str = "sed '$d' 1>&2";
    pub const EXIT_DIE: &str = "exit \"$code\"";
    pub const SHA: &str = "SHA=\"$(sha256_file \"$TMP\")\"";
    pub const MKTEMP: &str = "TMP=\"$(mktemp \"$REVIEWS/.base-untracked.XXXXXX\")\"";
    pub const PUBLISH_MV: &str = "reason=\"$(mv -f -T -- \"$TMP\" \"$LIST\" 2>&1)\"";
    pub const PUBLISH_PYTHON: &str = "reason=\"$(python3 -I -c 'import os, sys\ntry:\n    os.replace(sys.argv[1], sys.argv[2])\nexcept OSError as e:\n    sys.stderr.write(\"%s\\n\" % (e.strerror or e))\n    sys.exit(1)' \"$TMP\" \"$LIST\" 2>&1)\"";
}

/// 旧`os.replace`の公開の`-c`の本文(旧版`:148-153`の字面のまま)。
pub const PUBLISH_SOURCE: &str = "import os, sys\ntry:\n    os.replace(sys.argv[1], sys.argv[2])\nexcept OSError as e:\n    sys.stderr.write(\"%s\\n\" % (e.strerror or e))\n    sys.exit(1)";

#[cfg(test)]
mod tests {
    use super::*;

    fn source() -> String {
        std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../../plugins/dev-workflow/skills/do-task/scripts/reviews-dir.sh"),
        )
        .unwrap()
    }

    #[test]
    fn the_table_matches_the_old_script() {
        let source = source();
        LINES.verify(source.as_bytes()).unwrap();
        // 公開の本文は、旧版の行148〜153の`-c`の引数と同じ字面。
        let start = source.find("python3 -I -c '").unwrap() + "python3 -I -c '".len();
        let start = start + source[start..].find("import os, sys").unwrap();
        let end = start + source[start..].find("' \"$TMP\" \"$LIST\"").unwrap();
        assert_eq!(&source[start..end], PUBLISH_SOURCE);
        let changed = source.replace("rm -f -- \"$TMP\"", "rm -f \"$TMP\"");
        assert!(LINES.verify(changed.as_bytes()).is_err());
    }
}
