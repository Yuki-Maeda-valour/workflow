//! 旧版の docstring と、旧版が捕まえない例外の呼出履歴の定型(到達する経路だけ)。
//! - 行番号・関数名・ソースの行は、旧版(main e19ca1d)の `check-process.py`・`loop-supervisor.py` の値。
//!   ファイル名は固定名(2026-11-11 17:58・21:35 の決定)。
//! - 到達する経路の呼出履歴には標準ライブラリの行が出ない(旧スクリプトの行だけ)。
//! - 書き方は R4U の `traceback::print_exception`(本家 3.10.11 の `Python/traceback.c`・`Python/pythonrun.c`)。

use super::traceback::{self, Exception, Frame, Traceback};

/// 旧版 `check-process.py` の `__doc__`(`PYTHONOPTIMIZE` が 2 未満のとき)。
pub const CHECK_PROCESS_DOC: &str = "品質コマンドを監督し、回収の確認が取れてから呼出元へ返す。\n\nLinux は既存の専用 subreaper、他 POSIX は未回収の anchor が保持する群を使う。\n群外への切離しと同じ利用者権限による監督への攻撃を完全に隔離するものではない。\n";

/// 旧版 `loop-supervisor.py` の `__doc__`。
pub const LOOP_SUPERVISOR_DOC: &str = "Linux の subreaper で、1 回のイテレーションで起動したプロセス(子孫を含む)を、最後の waitpid まで所有して回収する。";

const CP: &str = "check-process.py";
const LS: &str = "loop-supervisor.py";

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

pub const LS_MODULE: Frame = frame(LS, 152, "<module>", "sys.exit(main())");
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
