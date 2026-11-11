//! `Stop` と旧版の例外(型名・理由)。捕まえる範囲と、捕まえない経路(U4)を分ける。
use dev_workflow_core::loop_state::CodepointString;

/// 旧版の例外。型名と `str(exc)` の文字列を持つ。
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PyException {
    pub type_name: String,
    pub message: CodepointString,
    /// 例外の処理中に起きたときの、元の例外(`__context__`)。
    pub context: Option<Box<PyException>>,
    /// `raise … from …` で明示した原因か(`__cause__`)。真なら区切りの文が変わる。
    pub explicit_cause: bool,
    /// `from None` で文脈を隠したか(`__suppress_context__`)。
    pub suppress_context: bool,
    /// 一度も `raise` されていない例外(`from KeyError(...)` の原因など)は traceback を持たず、
    /// 見出しの行を出さない。
    pub unraised: bool,
}

/// `Stop` の型名。
pub const STOP: &str = "Stop";

impl PyException {
    pub fn new(type_name: &str, message: &str) -> Self {
        Self::with_cps(type_name, CodepointString::from(message))
    }

    pub fn with_cps(type_name: &str, message: CodepointString) -> Self {
        Self {
            type_name: type_name.to_owned(),
            message,
            context: None,
            explicit_cause: false,
            suppress_context: false,
            unraised: false,
        }
    }

    pub fn stop(message: &str) -> Self {
        Self::new(STOP, message)
    }

    pub fn value_error(message: &str) -> Self {
        Self::new("ValueError", message)
    }

    pub fn type_error(message: &str) -> Self {
        Self::new("TypeError", message)
    }

    pub fn recursion(where_: &str) -> Self {
        Self::new(
            "RecursionError",
            &format!("maximum recursion depth exceeded{where_}"),
        )
    }

    /// 別の例外の処理中に起きた例外にする(`__context__`)。
    pub fn during(mut self, original: PyException) -> Self {
        self.context = Some(Box::new(original));
        self
    }

    /// `raise … from None`。
    pub fn without_context(mut self) -> Self {
        self.suppress_context = true;
        self
    }

    pub fn is_stop(&self) -> bool {
        self.type_name == STOP
    }

    /// 例外の型 `base` を継承しているか(旧版が捕まえる型の判定に使う)。
    pub fn is_instance(&self, base: &str) -> bool {
        let mut name = self.type_name.as_str();
        loop {
            if name == base {
                return true;
            }
            match parent_of(name) {
                Some(parent) => name = parent,
                None => return false,
            }
        }
    }

    /// `main` の `except` が捕まえる型か。
    pub fn caught_by_main(&self) -> bool {
        [
            "OSError",
            STOP,
            "ValueError",
            "KeyError",
            "TypeError",
            "RecursionError",
            "TimeoutExpired",
        ]
        .iter()
        .any(|base| self.is_instance(base))
    }

    /// `main` が出す理由。`Stop` はそのまま、ほかは型名だけにする(P12)。
    pub fn detail(&self) -> String {
        if self.is_stop() {
            return cps_to_lossy(self.message.as_codepoints());
        }
        format!("検査を完了できない ({})", self.type_name)
    }

    /// 捕まえない例外の表示(U4)。最後の例外の行と因果の区切りだけを出し、途中の行は出さない。
    /// 戻り値は標準エラーへ出す前の文字番号の列。
    pub fn uncaught_report(&self) -> Vec<u32> {
        let mut chain = vec![self];
        let mut current = self;
        while let Some(context) = current.context.as_deref() {
            if current.suppress_context && !current.explicit_cause {
                break;
            }
            chain.push(context);
            current = context;
        }
        let mut out = Vec::new();
        let mut index = chain.len();
        while index > 0 {
            index -= 1;
            let item = chain[index];
            if !item.unraised {
                out.extend(
                    "Traceback (most recent call last):\n"
                        .chars()
                        .map(u32::from),
                );
            }
            out.extend(item.type_name.chars().map(u32::from));
            if !item.message.as_codepoints().is_empty() {
                out.extend(": ".chars().map(u32::from));
                out.extend_from_slice(item.message.as_codepoints());
            }
            out.push(10);
            if index > 0 {
                let next = chain[index - 1];
                let text = if next.explicit_cause {
                    "\nThe above exception was the direct cause of the following exception:\n\n"
                } else {
                    "\nDuring handling of the above exception, another exception occurred:\n\n"
                };
                out.extend(text.chars().map(u32::from));
            }
        }
        out
    }
}

fn parent_of(name: &str) -> Option<&'static str> {
    Some(match name {
        "FileNotFoundError" | "PermissionError" | "FileExistsError" | "NotADirectoryError"
        | "IsADirectoryError" | "BrokenPipeError" | "InterruptedError" | "BlockingIOError"
        | "ProcessLookupError" | "ChildProcessError" | "TimeoutError" | "ConnectionError" => {
            "OSError"
        }
        "ConnectionAbortedError" | "ConnectionRefusedError" | "ConnectionResetError" => {
            "ConnectionError"
        }
        "JSONDecodeError" | "UnicodeError" => "ValueError",
        "UnicodeDecodeError" | "UnicodeEncodeError" => "UnicodeError",
        "KeyError" | "IndexError" => "LookupError",
        "RecursionError" | STOP => "RuntimeError",
        "OverflowError" | "ZeroDivisionError" => "ArithmeticError",
        "TimeoutExpired" | "CalledProcessError" => "SubprocessError",
        "OSError" | "ValueError" | "TypeError" | "LookupError" | "RuntimeError"
        | "AttributeError" | "ArithmeticError" | "SubprocessError" | "MemoryError"
        | "AssertionError" | "NameError" => "Exception",
        _ => return None,
    })
}

/// errno の番号が決まる OS。旧版(Python 3.10)の `errno` モジュールと同じ番号の体系を使う。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Os {
    Linux,
    Mac,
}

#[cfg(target_os = "macos")]
pub const CURRENT_OS: Os = Os::Mac;
#[cfg(not(target_os = "macos"))]
pub const CURRENT_OS: Os = Os::Linux;

/// errno に対応する旧版の `OSError` の下位の型(Python 3.10 の `errnomap`)。
pub fn os_error_type_on(os: Os, errno: i32) -> &'static str {
    // (EPERM, ENOENT, ESRCH, EINTR, ECHILD, EAGAIN, EACCES, EEXIST, ENOTDIR, EISDIR, EPIPE,
    //  ECONNABORTED, ECONNRESET, ETIMEDOUT, ECONNREFUSED, ESHUTDOWN, EALREADY, EINPROGRESS)
    let [
        eperm,
        enoent,
        esrch,
        eintr,
        echild,
        eagain,
        eacces,
        eexist,
        enotdir,
        eisdir,
        epipe,
        econnaborted,
        econnreset,
        etimedout,
        econnrefused,
        eshutdown,
        ealready,
        einprogress,
    ] = match os {
        Os::Linux => [
            1, 2, 3, 4, 10, 11, 13, 17, 20, 21, 32, 103, 104, 110, 111, 108, 114, 115,
        ],
        Os::Mac => [
            1, 2, 3, 4, 10, 35, 13, 17, 20, 21, 32, 53, 54, 60, 61, 58, 37, 36,
        ],
    };
    match errno {
        e if e == eperm || e == eacces => "PermissionError",
        e if e == enoent => "FileNotFoundError",
        e if e == esrch => "ProcessLookupError",
        e if e == eintr => "InterruptedError",
        e if e == echild => "ChildProcessError",
        e if e == eagain || e == ealready || e == einprogress => "BlockingIOError",
        e if e == eexist => "FileExistsError",
        e if e == enotdir => "NotADirectoryError",
        e if e == eisdir => "IsADirectoryError",
        e if e == epipe || e == eshutdown => "BrokenPipeError",
        e if e == econnaborted => "ConnectionAbortedError",
        e if e == econnreset => "ConnectionResetError",
        e if e == etimedout => "TimeoutError",
        e if e == econnrefused => "ConnectionRefusedError",
        _ => "OSError",
    }
}

pub fn os_error_type(errno: i32) -> &'static str {
    os_error_type_on(CURRENT_OS, errno)
}

/// `strerror(errno)`。Linux は glibc(旧 Python)の文面の表。musl の文面とは違うので、表で固定する。
/// Mac は OS の `strerror` が旧 Python と同じなので、そのまま使う(Mac での一致は未確認)。
pub fn strerror(errno: i32) -> String {
    #[cfg(target_os = "linux")]
    {
        match usize::try_from(errno)
            .ok()
            .and_then(|n| super::errno_table::GLIBC_STRERROR.get(n))
        {
            Some(text) => (*text).to_owned(),
            None => format!("Unknown error {errno}"),
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        let text = std::io::Error::from_raw_os_error(errno).to_string();
        let suffix = format!(" (os error {errno})");
        text.strip_suffix(&suffix).unwrap_or(&text).to_owned()
    }
}

/// `OSError` の `str()`。`[Errno N] 理由` と、あれば `: 'ファイル名'`。
pub fn os_error_message(errno: i32, reason: &str, filename: Option<&[u32]>) -> CodepointString {
    let mut cps: Vec<u32> = format!("[Errno {errno}] {reason}")
        .chars()
        .map(u32::from)
        .collect();
    if let Some(name) = filename {
        cps.extend(": ".chars().map(u32::from));
        cps.extend(super::pystr::repr(name));
    }
    CodepointString::try_from_codepoints(cps).expect("文字番号は範囲内")
}

/// Rust の `io::Error` を旧版の `OSError` にする。
pub fn from_io(error: &std::io::Error, filename: Option<&[u32]>) -> PyException {
    match error.raw_os_error() {
        Some(errno) => PyException::with_cps(
            os_error_type(errno),
            os_error_message(errno, &strerror(errno), filename),
        ),
        None => PyException::new("OSError", &error.to_string()),
    }
}

pub fn cps_to_lossy(cps: &[u32]) -> String {
    cps.iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn caught_types_follow_the_old_except_clause() {
        for caught in [
            "FileNotFoundError",
            "BrokenPipeError",
            "JSONDecodeError",
            "UnicodeDecodeError",
            "UnicodeEncodeError",
            "KeyError",
            "TypeError",
            "RecursionError",
            "TimeoutExpired",
            "ValueError",
            "OSError",
            STOP,
        ] {
            assert!(PyException::new(caught, "").caught_by_main(), "{caught}");
        }
        for uncaught in [
            "AttributeError",
            "IndexError",
            "OverflowError",
            "RuntimeError",
            "AssertionError",
            "SubprocessError",
            "MemoryError",
            "ZeroDivisionError",
        ] {
            assert!(
                !PyException::new(uncaught, "").caught_by_main(),
                "{uncaught}"
            );
        }
    }

    #[test]
    fn detail_hides_the_exception_arguments() {
        assert_eq!(PyException::stop("理由").detail(), "理由");
        assert_eq!(
            PyException::new("FileNotFoundError", "[Errno 2] 秘密: '/x'").detail(),
            "検査を完了できない (FileNotFoundError)"
        );
    }

    #[test]
    fn uncaught_report_keeps_the_last_line_and_the_chain_separators() {
        let inner = PyException::new("KeyError", "'a'");
        let direct = PyException {
            explicit_cause: true,
            ..PyException::new("Stop", "x").during(inner.clone())
        };
        assert_eq!(
            super::super::pystr::to_string(&direct.uncaught_report()),
            "Traceback (most recent call last):\nKeyError: 'a'\n\nThe above exception was the direct cause of the following exception:\n\nTraceback (most recent call last):\nStop: x\n"
        );
        let context = PyException::new("AttributeError", "y").during(inner.clone());
        assert!(
            super::super::pystr::to_string(&context.uncaught_report()).contains(
                "\nDuring handling of the above exception, another exception occurred:\n\n"
            )
        );
        let hidden = PyException::new("AttributeError", "y")
            .during(inner)
            .without_context();
        assert_eq!(
            super::super::pystr::to_string(&hidden.uncaught_report()),
            "Traceback (most recent call last):\nAttributeError: y\n"
        );
        let empty = PyException::new("IndexError", "");
        assert_eq!(
            super::super::pystr::to_string(&empty.uncaught_report()),
            "Traceback (most recent call last):\nIndexError\n"
        );
    }

    #[test]
    fn os_errors_match_python_names() {
        assert_eq!(os_error_type_on(Os::Linux, 2), "FileNotFoundError");
        assert_eq!(os_error_type_on(Os::Linux, 32), "BrokenPipeError");
        assert_eq!(os_error_type_on(Os::Linux, 28), "OSError");
        assert_eq!(os_error_type_on(Os::Linux, 108), "BrokenPipeError");
        assert_eq!(os_error_type_on(Os::Linux, 114), "BlockingIOError");
        assert_eq!(os_error_type_on(Os::Linux, 115), "BlockingIOError");
        // Mac の番号(EAGAIN=35・ETIMEDOUT=60・ECONNRESET=54 など)。
        assert_eq!(os_error_type_on(Os::Mac, 35), "BlockingIOError");
        assert_eq!(os_error_type_on(Os::Mac, 11), "OSError");
        assert_eq!(os_error_type_on(Os::Mac, 60), "TimeoutError");
        assert_eq!(os_error_type_on(Os::Mac, 54), "ConnectionResetError");
        assert_eq!(os_error_type_on(Os::Mac, 58), "BrokenPipeError");
        assert_eq!(os_error_type_on(Os::Mac, 37), "BlockingIOError");
        assert_eq!(os_error_type_on(Os::Mac, 36), "BlockingIOError");
        assert_eq!(os_error_type_on(Os::Mac, 53), "ConnectionAbortedError");
        assert_eq!(os_error_type_on(Os::Mac, 61), "ConnectionRefusedError");
        let message = os_error_message(2, "No such file or directory", Some(&[0x2f, 0x78, 0xdc80]));
        assert_eq!(
            super::super::pystr::to_string(message.as_codepoints()),
            "[Errno 2] No such file or directory: '/x\\udc80'"
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn errno_cases_match_python_3_10_on_glibc() {
        use super::super::testing::fixture;
        let cases = fixture!("errno_cases.json");
        assert!(cases.as_array().unwrap().len() > 190);
        for case in cases.as_array().unwrap() {
            let errno = case["errno"].as_i64().unwrap() as i32;
            assert_eq!(strerror(errno), case["text"].as_str().unwrap(), "{errno}");
            assert_eq!(
                os_error_type(errno),
                case["type"].as_str().unwrap(),
                "{errno}"
            );
            let error = from_io(&std::io::Error::from_raw_os_error(errno), None);
            assert_eq!(error.type_name, case["type"].as_str().unwrap(), "{errno}");
            assert_eq!(
                super::super::pystr::to_string(error.message.as_codepoints()),
                case["str"].as_str().unwrap(),
                "{errno}"
            );
        }
    }

    /// musl の `strerror` は glibc(旧 Python)と文面が違う。表で固定した文面が出ることを、musl のビルドで確かめる。
    #[cfg(all(target_os = "linux", target_env = "musl"))]
    #[test]
    fn musl_strerror_is_replaced_by_the_glibc_table() {
        for (errno, musl, glibc) in [
            (5, "I/O error", "Input/output error"),
            (18, "Cross-device link", "Invalid cross-device link"),
            (110, "Operation timed out", "Connection timed out"),
        ] {
            let native = std::io::Error::from_raw_os_error(errno).to_string();
            assert!(native.starts_with(musl), "{native}");
            assert_eq!(strerror(errno), glibc);
        }
    }
}
