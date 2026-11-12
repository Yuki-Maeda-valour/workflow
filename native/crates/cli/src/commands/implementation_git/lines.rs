//! 呼出履歴に出す旧スクリプトと標準ライブラリの行の固定表。
//!
//! 旧スクリプトの行番号とソースの行は、Rustのソースからではなく旧版の`implementation-git.py`から取る。
//! 表の各行は旧版のその行の字面(行頭の字下げを含む全体)を持ち、旧版と食い違えば単体検査で止める。
//! 標準ライブラリの行は、CIで比べる本家のCPython 3.10.11の値(2026-11-11 22:26の利用者決定)。
//! 所在地の欄は固定のファイル名にする(2026-11-11 17:58・21:35の利用者決定)。

use super::super::r4g_support::traceback::Frame;

/// 呼出履歴の所在地の欄に出す、旧スクリプトの固定のファイル名。
pub const SCRIPT_NAME: &str = "implementation-git.py";

/// 旧スクリプトの1行。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Line {
    pub name: &'static str,
    pub line: u32,
    pub function: &'static str,
    /// 旧版のその行の全体(改行を除く)。
    pub source: &'static str,
}

pub const LINES: &[Line] = &[
    Line {
        name: "module",
        line: 380,
        function: "<module>",
        source: "        main(sys.argv[1:])",
    },
    Line {
        name: "module_failure",
        line: 382,
        function: "<module>",
        source: "        print(exc.message, file=sys.stderr)",
    },
    Line {
        name: "module_runtime",
        line: 385,
        function: "<module>",
        source: "        print(\"ERROR [runtime] 実行に失敗: %s\" % exc, file=sys.stderr)",
    },
    Line {
        name: "main_parse",
        line: 363,
        function: "main",
        source: "    args = parse_input(argv)",
    },
    Line {
        name: "main_precheck",
        line: 365,
        function: "main",
        source: "    filters, pairs = parse_filters(precheck(args.cwd, env, args.accept))",
    },
    Line {
        name: "main_verify",
        line: 370,
        function: "main",
        source: "    verify(args.cwd, env, filters)",
    },
    Line {
        name: "main_prepare",
        line: 372,
        function: "main",
        source: "        print(json.dumps({\"version\": VERSION, \"cwd\": args.cwd, \"accept\": args.accept,",
    },
    Line {
        name: "main_operation",
        line: 375,
        function: "main",
        source: "        operation(args, env)",
    },
    Line {
        name: "parse_input_parse",
        line: 158,
        function: "parse_input",
        source: "    args = build_parser().parse_args(argv)",
    },
    Line {
        name: "parse_input_cwd",
        line: 161,
        function: "parse_input",
        source: "    args.cwd = physical_cwd(args.cwd)",
    },
    Line {
        name: "parse_input_validate",
        line: 164,
        function: "parse_input",
        source: "    validate_run_args(args)",
    },
    Line {
        name: "physical_cwd_strict",
        line: 55,
        function: "physical_cwd",
        source: "    strict_text(value, \"cwd\")",
    },
    Line {
        name: "strict_text_encode",
        line: 46,
        function: "strict_text",
        source: "        value.encode(\"utf-8\", \"strict\")",
    },
    Line {
        name: "validate_hash",
        line: 136,
        function: "validate_run_args",
        source: "    if not re.fullmatch(r\"[a-f0-9]{64}\", args.expect_context_sha256):",
    },
    Line {
        name: "validate_diff",
        line: 139,
        function: "validate_run_args",
        source: "        if not 0 <= args.context <= 100 or (args.cached and args.target is not None) or (args.target is not None and args.base is None):",
    },
    Line {
        name: "validate_limit",
        line: 141,
        function: "validate_run_args",
        source: "    if args.operation == \"log\" and not 1 <= args.limit <= 1000:",
    },
    Line {
        name: "validate_revision_call",
        line: 148,
        function: "validate_run_args",
        source: "            validate_revision(value)",
    },
    Line {
        name: "validate_path_call",
        line: 150,
        function: "validate_run_args",
        source: "        validate_path(path)",
    },
    Line {
        name: "validate_file_call",
        line: 152,
        function: "validate_run_args",
        source: "        validate_path(args.file)",
    },
    Line {
        name: "validate_revision_strict",
        line: 169,
        function: "validate_revision",
        source: "    strict_text(value, \"revision\")",
    },
    Line {
        name: "validate_path_strict",
        line: 175,
        function: "validate_path",
        source: "    strict_text(value, \"path\")",
    },
    Line {
        name: "precheck_run",
        line: 215,
        function: "precheck",
        source: "        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,",
    },
    Line {
        name: "precheck_stderr",
        line: 220,
        function: "precheck",
        source: "    sys.stderr.buffer.write(result.stderr)",
    },
    Line {
        name: "run_git",
        line: 198,
        function: "run_git",
        source: "        return subprocess.run(",
    },
    Line {
        name: "checked_git_run",
        line: 290,
        function: "checked_git",
        source: "    result = run_git(cwd, env, *args)",
    },
    Line {
        name: "checked_git_stderr",
        line: 291,
        function: "checked_git",
        source: "    sys.stderr.buffer.write(result.stderr)",
    },
    Line {
        name: "verify_config",
        line: 299,
        function: "verify",
        source: "        raw = checked_git(cwd, env, \"config\", \"--get\", key)",
    },
    Line {
        name: "verify_toplevel",
        line: 302,
        function: "verify",
        source: "    raw = checked_git(cwd, env, \"rev-parse\", \"--show-toplevel\")",
    },
    Line {
        name: "revision_git",
        line: 314,
        function: "revision",
        source: "    raw = checked_git(cwd, env, \"rev-parse\", \"--verify\", \"--end-of-options\", value + \"^{commit}\")",
    },
    Line {
        name: "operation_status",
        line: 326,
        function: "operation",
        source: "                   \"--untracked-files=\" + args.untracked, \"--ignore-submodules=dirty\"]",
    },
    Line {
        name: "operation_diff",
        line: 329,
        function: "operation",
        source: "                   \"--unified=%d\" % args.context, \"--\" + args.format]",
    },
    Line {
        name: "operation_log",
        line: 338,
        function: "operation",
        source: "                   \"--format=\" + args.format]",
    },
    Line {
        name: "operation_cat_file",
        line: 346,
        function: "operation",
        source: "            if checked_git(cwd, env, \"cat-file\", \"-t\", obj) != b\"blob\\n\":",
    },
    Line {
        name: "operation_run",
        line: 355,
        function: "operation",
        source: "    result = run_git(cwd, env, *command)",
    },
    Line {
        name: "operation_stdout",
        line: 356,
        function: "operation",
        source: "    sys.stdout.buffer.write(result.stdout)",
    },
    Line {
        name: "operation_stderr",
        line: 357,
        function: "operation",
        source: "    sys.stderr.buffer.write(result.stderr)",
    },
];

/// 名前の段(旧スクリプト)。表に無い名前は実装の誤りなので止める。
pub fn frame(name: &str) -> Frame {
    let line = LINES
        .iter()
        .find(|line| line.name == name)
        .unwrap_or_else(|| panic!("行の固定表に無い地点: {name}"));
    Frame {
        file: SCRIPT_NAME,
        line: line.line,
        function: line.function,
        source: Some(line.source),
    }
}

/// 名前の並びの段。
pub fn frames(names: &[&str]) -> Vec<Frame> {
    names.iter().map(|name| frame(name)).collect()
}

/// 本家のCPython 3.10.11の`re.fullmatch`の段。
pub const RE_FULLMATCH: Frame = Frame {
    file: "re.py",
    line: 195,
    function: "fullmatch",
    source: Some("    return _compile(pattern, flags).fullmatch(string)"),
};

/// 本家のCPython 3.10.11の`subprocess.run`から`fork_exec`までの3段。
pub const SUBPROCESS_FORK_EXEC: [Frame; 3] = [
    Frame {
        file: "subprocess.py",
        line: 503,
        function: "run",
        source: Some("    with Popen(*popenargs, **kwargs) as process:"),
    },
    Frame {
        file: "subprocess.py",
        line: 971,
        function: "__init__",
        source: Some("            self._execute_child(args, executable, preexec_fn, close_fds,"),
    },
    Frame {
        file: "subprocess.py",
        line: 1796,
        function: "_execute_child",
        source: Some("                    self.pid = _posixsubprocess.fork_exec("),
    },
];

/// 旧版のファイルの場所(単体検査で字面を照らす)。
#[cfg(test)]
pub fn old_script_path() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../plugins/dev-workflow/skills/do-task/scripts/implementation-git.py")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines_match_the_old_script() {
        let source = std::fs::read_to_string(old_script_path()).unwrap();
        let lines: Vec<&str> = source.split('\n').collect();
        for (index, line) in LINES.iter().enumerate() {
            assert!(
                !LINES[..index].iter().any(|other| other.name == line.name),
                "地点の名前が重複: {}",
                line.name
            );
            assert_eq!(lines[line.line as usize - 1], line.source, "{}", line.name);
        }
    }
}
