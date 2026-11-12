//! 旧版の docstring と、旧版が捕まえない例外の呼出履歴の定型(到達する経路だけ)。
//! - 行番号・関数名・ソースの行は、旧版(main e19ca1d)の `check-process.py`・`loop-supervisor.py` の値。
//!   ファイル名は固定名(2026-11-11 17:58・21:35 の決定)。
//! - 到達する経路の呼出履歴には標準ライブラリの行が出ない(旧スクリプトの行だけ)。
//! - 書き方は R4U の `traceback::print_exception`(本家 3.10.11 の `Python/traceback.c`・`Python/pythonrun.c`)。

use super::argparse::{ArgumentRaise, ErrorSite};
use super::traceback::{self, Exception, Frame, Traceback};

/// 旧版 `check-process.py` の `__doc__`(`PYTHONOPTIMIZE` が 2 未満のとき)。
pub const CHECK_PROCESS_DOC: &str = "品質コマンドを監督し、回収の確認が取れてから呼出元へ返す。\n\nLinux は既存の専用 subreaper、他 POSIX は未回収の anchor が保持する群を使う。\n群外への切離しと同じ利用者権限による監督への攻撃を完全に隔離するものではない。\n";

/// 旧版 `loop-supervisor.py` の `__doc__`。
pub const LOOP_SUPERVISOR_DOC: &str = "Linux の subreaper で、1 回のイテレーションで起動したプロセス(子孫を含む)を、最後の waitpid まで所有して回収する。";

const CP: &str = "check-process.py";
const LS: &str = "loop-supervisor.py";
/// 標準ライブラリの argparse(2026-11-11 21:35 の決定の固定名。行番号は本家 3.10.11。22:26 の決定)。
const AP: &str = "argparse.py";

const fn frame(file: &'static str, line: u32, name: &'static str, source: &'static str) -> Frame {
    Frame {
        file,
        line,
        name,
        source,
    }
}

pub const CP_MODULE: Frame = frame(CP, 337, "<module>", "sys.exit(main())");
pub const CP_MAIN_DEADLINE: Frame = frame(
    CP,
    325,
    "main",
    "if args.timeout is None or args.grace is None or not math.isfinite(args.timeout) or args.timeout <= 0 or not math.isfinite(args.grace) or args.grace < 0:",
);
pub const CP_MAIN_LINUX: Frame = frame(CP, 327, "main", "linux_worker(args, argv)");
pub const CP_MAIN_ANCHOR: Frame =
    frame(CP, 329, "main", "anchor(args.anchor_fd, args.nonce, argv)");
pub const CP_ANCHOR_INHERITABLE: Frame = frame(CP, 265, "anchor", "os.set_inheritable(fd, False)");
pub const CP_WORKER_OPEN: Frame = frame(
    CP,
    292,
    "linux_worker",
    "fd = os.open(args.result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)",
);
pub const CP_WORKER_SUPERVISE: Frame = frame(
    CP,
    296,
    "linux_worker",
    "result = supervisor.supervise(argv, args.timeout, args.grace)",
);

pub const CP_MAIN_PARSE: Frame = frame(CP, 319, "main", "args = parser.parse_args()");
pub const LS_MODULE: Frame = frame(LS, 152, "<module>", "sys.exit(main())");
pub const LS_MAIN_PARSE: Frame = frame(LS, 129, "main", "args = parser.parse_args()");
pub const LS_MAIN_DEADLINE: Frame = frame(
    LS,
    132,
    "main",
    "if not command or args.timeout <= 0 or args.grace < 0:",
);
pub const LS_MAIN_OPEN: Frame = frame(
    LS,
    135,
    "main",
    "fd = os.open(args.result, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)",
);
pub const LS_MAIN_SUPERVISE: Frame = frame(
    LS,
    137,
    "main",
    "result = supervise(command, args.timeout, args.grace)",
);
pub const LS_SUPERVISE_RETURNCODE: Frame = frame(
    LS,
    106,
    "supervise",
    "process.returncode = statuses[process.pid]",
);
pub const LS_SUPERVISE_SIGNAL: Frame = frame(
    LS,
    102,
    "supervise",
    "signal_owned(signal.SIGTERM if time.monotonic() < term_end else signal.SIGKILL)",
);
pub const LS_SIGNAL_OWNED_BEFORE: Frame = frame(LS, 52, "signal_owned", "before = identity(pid)");
pub const LS_SIGNAL_OWNED_PIDFD: Frame = frame(LS, 55, "signal_owned", "fd = os.pidfd_open(pid)");
pub const LS_SIGNAL_OWNED_AFTER: Frame = frame(LS, 57, "signal_owned", "after = identity(pid)");
pub const LS_IDENTITY: Frame = frame(
    LS,
    40,
    "identity",
    "return (int(fields[1]), int(fields[19]))  # ppid, starttime",
);

/// supervise の中で旧版が捕まえない例外が起きる所(loop-supervisor と check-process の `--linux` で共通)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InSupervise {
    /// `statuses[process.pid]` の `KeyError`(SIGCHLD を無視で受け継いだ起動)。
    Returncode,
    /// 信号を送る前の `identity(pid)` の `IndexError`(`/proc/<pid>/stat` の欄が足りない)。
    IdentityBefore,
    /// pidfd を開いた後の `identity(pid)` の `IndexError`。
    IdentityAfter,
    /// `os.pidfd_open(pid)` の `OverflowError`(`children` の pid が C の int の外)。
    PidfdOpen,
}

impl InSupervise {
    fn frames(self) -> Vec<Frame> {
        match self {
            InSupervise::Returncode => vec![LS_SUPERVISE_RETURNCODE],
            InSupervise::IdentityBefore => {
                vec![LS_SUPERVISE_SIGNAL, LS_SIGNAL_OWNED_BEFORE, LS_IDENTITY]
            }
            InSupervise::IdentityAfter => {
                vec![LS_SUPERVISE_SIGNAL, LS_SIGNAL_OWNED_AFTER, LS_IDENTITY]
            }
            InSupervise::PidfdOpen => vec![LS_SUPERVISE_SIGNAL, LS_SIGNAL_OWNED_PIDFD],
        }
    }
}

/// 旧版が捕まえない例外が起きる所。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Site {
    /// check-process の anchor の `os.set_inheritable(fd, False)`(fd が None・list・C の int の外)。
    CheckProcessAnchor,
    /// check-process の `--linux` の期限の検査(`math.isfinite(list)`)。
    CheckProcessDeadline,
    /// check-process の linux worker の記録の `os.open`(`--result` が None・list)。
    CheckProcessOpen,
    /// check-process の linux worker から呼んだ supervise。
    CheckProcessSupervise(InSupervise),
    /// loop-supervisor の期限の検査(`list <= 0`・`list < 0`)。
    LoopSupervisorDeadline,
    /// loop-supervisor の記録の `os.open`(`--result` が list)。
    LoopSupervisorOpen,
    /// loop-supervisor の supervise。
    LoopSupervisorSupervise(InSupervise),
}

impl Site {
    /// 呼出履歴のフレーム(外側から順)。
    pub fn frames(self) -> Vec<Frame> {
        match self {
            Site::CheckProcessAnchor => vec![CP_MODULE, CP_MAIN_ANCHOR, CP_ANCHOR_INHERITABLE],
            Site::CheckProcessDeadline => vec![CP_MODULE, CP_MAIN_DEADLINE],
            Site::CheckProcessOpen => vec![CP_MODULE, CP_MAIN_LINUX, CP_WORKER_OPEN],
            Site::CheckProcessSupervise(inner) => {
                let mut frames = vec![CP_MODULE, CP_MAIN_LINUX, CP_WORKER_SUPERVISE];
                frames.extend(inner.frames());
                frames
            }
            Site::LoopSupervisorDeadline => vec![LS_MODULE, LS_MAIN_DEADLINE],
            Site::LoopSupervisorOpen => vec![LS_MODULE, LS_MAIN_OPEN],
            Site::LoopSupervisorSupervise(inner) => {
                let mut frames = vec![LS_MODULE, LS_MAIN_SUPERVISE];
                frames.extend(inner.frames());
                frames
            }
        }
    }
}

// ---- argparse 3.10.11 の書き出しの経路のフレーム ----

const AP_PARSE_ARGS: Frame = frame(
    AP,
    1833,
    "parse_args",
    "args, argv = self.parse_known_args(args, namespace)",
);
const AP_PARSE_ARGS_UNRECOGNIZED: Frame =
    frame(AP, 1836, "parse_args", "self.error(msg % ' '.join(argv))");
const AP_PARSE_KNOWN: Frame = frame(
    AP,
    1866,
    "parse_known_args",
    "namespace, args = self._parse_known_args(args, namespace)",
);
const AP_PARSE_KNOWN_ERROR: Frame = frame(AP, 1869, "parse_known_args", "self.error(str(err))");
const AP_INNER_PARSE_OPTIONAL: Frame = frame(
    AP,
    1910,
    "_parse_known_args",
    "option_tuple = self._parse_optional(arg_string)",
);
const AP_INNER_CONSUME_OPTIONAL: Frame = frame(
    AP,
    2079,
    "_parse_known_args",
    "start_index = consume_optional(start_index)",
);
const AP_INNER_REQUIRED: Frame = frame(
    AP,
    2107,
    "_parse_known_args",
    "self.error(_('the following arguments are required: %s') %",
);
const AP_CONSUME_IGNORED_SHORT: Frame = frame(
    AP,
    1987,
    "consume_optional",
    "raise ArgumentError(action, msg % explicit_arg)",
);
const AP_CONSUME_IGNORED: Frame = frame(
    AP,
    2001,
    "consume_optional",
    "raise ArgumentError(action, msg % explicit_arg)",
);
const AP_CONSUME_MATCH: Frame = frame(
    AP,
    2009,
    "consume_optional",
    "arg_count = match_argument(action, selected_patterns)",
);
const AP_CONSUME_TAKE: Frame = frame(
    AP,
    2019,
    "consume_optional",
    "take_action(action, args, option_string)",
);
const AP_TAKE_VALUES: Frame = frame(
    AP,
    1927,
    "take_action",
    "argument_values = self._get_values(action, argument_strings)",
);
const AP_TAKE_CALL: Frame = frame(
    AP,
    1943,
    "take_action",
    "action(self, namespace, argument_values, option_string)",
);
const AP_MATCH_ARGUMENT: Frame = frame(
    AP,
    2174,
    "_match_argument",
    "raise ArgumentError(action, msg)",
);
const AP_PARSE_OPTIONAL_ERROR: Frame = frame(AP, 2230, "_parse_optional", "self.error(msg % args)");
const AP_GET_VALUES: Frame = frame(
    AP,
    2462,
    "_get_values",
    "value = self._get_value(action, arg_string)",
);
const AP_GET_VALUE_CALL: Frame = frame(AP, 2495, "_get_value", "result = type_func(arg_string)");
const AP_GET_VALUE_RAISE: Frame = frame(
    AP,
    2508,
    "_get_value",
    "raise ArgumentError(action, msg % args)",
);
const AP_HELP_CALL: Frame = frame(AP, 1106, "__call__", "parser.print_help()");
const AP_PRINT_USAGE: Frame = frame(
    AP,
    2562,
    "print_usage",
    "self._print_message(self.format_usage(), file)",
);
const AP_PRINT_HELP: Frame = frame(
    AP,
    2567,
    "print_help",
    "self._print_message(self.format_help(), file)",
);
const AP_PRINT_MESSAGE: Frame = frame(AP, 2573, "_print_message", "file.write(message)");
const AP_EXIT: Frame = frame(
    AP,
    2580,
    "exit",
    "self._print_message(message, _sys.stderr)",
);
const AP_ERROR_USAGE: Frame = frame(AP, 2592, "error", "self.print_usage(_sys.stderr)");
const AP_ERROR_EXIT: Frame = frame(
    AP,
    2594,
    "error",
    "self.exit(2, _('%(prog)s: error: %(message)s\\n') % args)",
);

/// 旧スクリプト(`<module>` と、main の `args = parser.parse_args()` の行)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Script {
    CheckProcess,
    LoopSupervisor,
}

impl Script {
    /// `<module>` の `sys.exit(main())`。
    pub fn module(self) -> Frame {
        match self {
            Script::CheckProcess => CP_MODULE,
            Script::LoopSupervisor => LS_MODULE,
        }
    }

    /// main の `args = parser.parse_args()`。
    pub fn parse_args(self) -> Frame {
        match self {
            Script::CheckProcess => CP_MAIN_PARSE,
            Script::LoopSupervisor => LS_MAIN_PARSE,
        }
    }
}

/// 標準出力か標準エラーか。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stream {
    Stdout,
    Stderr,
}

/// argparse が書く流れの選び方(起動の時に閉じていた fd の流れは None。並びの最初の None でない流れへ書き、
/// 全部が None なら `None.write` の `AttributeError`)。
/// - help: `print_help(file=None)` は sys.stdout、None なら `_print_message` が sys.stderr にする。
/// - usage: `print_usage(sys.stderr)` は、None なら sys.stdout にする(それも None なら `_print_message` が
///   sys.stderr〈None〉にする)。
/// - error の文: `exit` の `_print_message(message, sys.stderr)` は sys.stderr だけ。
pub const HELP_STREAMS: &[Stream] = &[Stream::Stdout, Stream::Stderr];
pub const USAGE_STREAMS: &[Stream] = &[Stream::Stderr, Stream::Stdout];
pub const MESSAGE_STREAMS: &[Stream] = &[Stream::Stderr];

/// argparse が `file.write(message)` を呼ぶ 3 つの書き出し。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WriteStep {
    /// `-h` の `print_help`(`_HelpAction.__call__` から)。
    Help,
    /// `error` の `print_usage(_sys.stderr)`。
    Usage,
    /// `error` の `exit(2, message)`。
    Message,
}

/// argparse の書き出しの途中の例外の呼出履歴(前の例外から順。`ArgumentError` の経路は、捕まえた
/// `ValueError`・`ArgumentError` の後に「During handling…」で続く)。`site` は誤りの所(help は None)。
pub fn argparse_chain(
    script: Script,
    site: Option<&ErrorSite>,
    step: WriteStep,
    exception: Exception,
) -> Vec<Traceback> {
    let mut chain = Vec::new();
    let mut frames = vec![script.module(), script.parse_args()];
    match site {
        None => frames.extend([
            AP_PARSE_ARGS,
            AP_PARSE_KNOWN,
            AP_INNER_CONSUME_OPTIONAL,
            AP_CONSUME_TAKE,
            AP_TAKE_CALL,
            AP_HELP_CALL,
        ]),
        Some(ErrorSite::Argument { raised, text }) => {
            let mut raised_frames = vec![AP_PARSE_KNOWN, AP_INNER_CONSUME_OPTIONAL];
            match raised {
                ArgumentRaise::IgnoredShort => raised_frames.push(AP_CONSUME_IGNORED_SHORT),
                ArgumentRaise::IgnoredExplicit => raised_frames.push(AP_CONSUME_IGNORED),
                ArgumentRaise::ExpectedOne => {
                    raised_frames.extend([AP_CONSUME_MATCH, AP_MATCH_ARGUMENT])
                }
                ArgumentRaise::InvalidValue { cause } => {
                    chain.push(Traceback {
                        frames: vec![AP_GET_VALUE_CALL],
                        exception: exception_of("ValueError", cause.clone()),
                    });
                    raised_frames.extend([
                        AP_CONSUME_TAKE,
                        AP_TAKE_VALUES,
                        AP_GET_VALUES,
                        AP_GET_VALUE_RAISE,
                    ]);
                }
            }
            chain.push(Traceback {
                frames: raised_frames,
                exception: exception_of("argparse.ArgumentError", text.clone()),
            });
            frames.extend([AP_PARSE_ARGS, AP_PARSE_KNOWN_ERROR]);
        }
        Some(ErrorSite::Required) => {
            frames.extend([AP_PARSE_ARGS, AP_PARSE_KNOWN, AP_INNER_REQUIRED])
        }
        Some(ErrorSite::Unrecognized) => frames.push(AP_PARSE_ARGS_UNRECOGNIZED),
        Some(ErrorSite::Ambiguous) => frames.extend([
            AP_PARSE_ARGS,
            AP_PARSE_KNOWN,
            AP_INNER_PARSE_OPTIONAL,
            AP_PARSE_OPTIONAL_ERROR,
        ]),
    }
    match step {
        WriteStep::Help => frames.push(AP_PRINT_HELP),
        WriteStep::Usage => frames.extend([AP_ERROR_USAGE, AP_PRINT_USAGE]),
        WriteStep::Message => frames.extend([AP_ERROR_EXIT, AP_EXIT]),
    }
    frames.push(AP_PRINT_MESSAGE);
    chain.push(Traceback { frames, exception });
    chain
}

fn exception_of(type_name: &'static str, value: String) -> Exception {
    Exception { type_name, value }
}

/// 例外の行(型と `str(exc)`)。
pub fn exception(type_name: &'static str, value: impl Into<String>) -> Exception {
    Exception {
        type_name,
        value: value.into(),
    }
}

/// 呼出履歴の全文(標準エラーへ書く文字列)。
pub fn render(site: Site, exception: Exception) -> String {
    let chain = [Traceback {
        frames: site.frames(),
        exception,
    }];
    traceback::print_exception(&chain)
        .into_iter()
        .map(|piece| piece.text)
        .collect()
}

/// 旧版が捕まえない例外の `str(exc)`(到達する経路だけ。型は括弧の中)。
pub mod uncaught {
    /// `TypeError`: `os.set_inheritable(None, False)`(`--anchor-fd` も `--linux` も無い)。
    pub const NONE_AS_INTEGER: &str = "'NoneType' object cannot be interpreted as an integer";
    /// `TypeError`: `os.set_inheritable([], False)`(`--anchor-fd=--`)。
    pub const LIST_AS_INTEGER: &str = "'list' object cannot be interpreted as an integer";
    /// `OverflowError`: `os.set_inheritable(fd, False)` の fd が C の int の外。
    pub const C_INT_OVERFLOW: &str = "Python int too large to convert to C int";
    /// `TypeError`: `math.isfinite([])`(`--timeout=--`・`--grace=--`)。
    pub const LIST_AS_REAL: &str = "must be real number, not list";
    /// `TypeError`: `os.open(None, …)`(`--linux` で `--result` が無い)。
    pub const OPEN_NONE: &str = "open: path should be string, bytes or os.PathLike, not NoneType";
    /// `TypeError`: `os.open([], …)`(`--result=--`)。
    pub const OPEN_LIST: &str = "open: path should be string, bytes or os.PathLike, not list";
    /// `TypeError`: `[] <= 0`(loop-supervisor の `--timeout=--`)。
    pub const LIST_LE_INT: &str = "'<=' not supported between instances of 'list' and 'int'";
    /// `TypeError`: `[] < 0`(loop-supervisor の `--grace=--`)。
    pub const LIST_LT_INT: &str = "'<' not supported between instances of 'list' and 'int'";
    /// `IndexError`: `/proc/<pid>/stat` の欄が足りない。
    pub const INDEX_OUT_OF_RANGE: &str = "list index out of range";
}
