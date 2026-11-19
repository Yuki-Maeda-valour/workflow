//! 旧版(CPython 3.10.11)の例外の文と呼出履歴の部品。
//!
//! - [`ExcType`]: 例外の型(表示の名前と、`except` の判定に使う親の型)。errno から `OSError` の
//!   下位の型を選ぶ表(`Objects/exceptions.c` の `ADD_ERRNO`)。
//! - [`PyException`]: 例外の値。`str()` の文・呼出履歴・連鎖(`__cause__`・`__context__`)を持つ。
//! - `OSError` の文(`[Errno N] <文>: '<repr>'`。2 つ目のファイル名は ` -> '<repr>'`)、[`repr`]、
//!   `UnicodeDecodeError`・`UnicodeEncodeError` の文。
//! - errno の文([`strerror`]): Linux は glibc の `strerror` の表(表に無い番号は
//!   `Unknown error N`)、Mac は OS の文。検査入口が静的に結合する musl の文は使わない。
//! - 呼出履歴の 1 段([`Frame`])と、固定名・行番号・ソースの行・関数の並びの表([`frames`])。
//!
//! 書き込みの単位と途中の信号の確かめは `pyio` の [`Runtime`](super::pyio::Runtime) が受け持つ。

use super::pyio::PyStr;
use super::r4b_unicode;

pub const EINTR: i32 = 4;
pub const EIO: i32 = 5;
pub const EISDIR: i32 = 21;
/// `EAGAIN`(`EWOULDBLOCK` と同じ。書き込みを待たない fd が一杯)。番号は OS で違う。
pub const EAGAIN: i32 = errno::EAGAIN;
#[cfg(test)]
pub const EFBIG: i32 = 27;

// ---------------------------------------------------------------- 例外の型

/// 例外の型。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExcType {
    /// `__module__`。組込みの型は `builtins`(表示では省く)。
    pub module: &'static str,
    /// `__qualname__`。
    pub name: &'static str,
    /// 親の型の名前(近い順に `BaseException` まで)。`except` の判定に使う。
    pub bases: &'static [&'static str],
}

impl ExcType {
    /// `isinstance(例外, <name の型>)` と同じ判定(名前で比べる)。
    pub fn is(&self, name: &str) -> bool {
        self.name == name || self.bases.contains(&name)
    }

    /// 表示の名前(`builtins` は名前だけ、ほかは `module.qualname`)。試験で使う。
    #[cfg(test)]
    pub fn display_name(&self) -> String {
        if self.module == "builtins" {
            self.name.to_owned()
        } else {
            format!("{}.{}", self.module, self.name)
        }
    }
}

/// 組込みの例外の型。
pub mod types {
    use super::ExcType;

    const fn builtin(name: &'static str, bases: &'static [&'static str]) -> ExcType {
        ExcType {
            module: "builtins",
            name,
            bases,
        }
    }

    const EXCEPTION: &[&str] = &["Exception", "BaseException"];
    const OS: &[&str] = &["OSError", "Exception", "BaseException"];
    const CONNECTION: &[&str] = &["ConnectionError", "OSError", "Exception", "BaseException"];

    pub const KEYBOARD_INTERRUPT: ExcType = builtin("KeyboardInterrupt", &["BaseException"]);
    pub const ATTRIBUTE_ERROR: ExcType = builtin("AttributeError", EXCEPTION);
    pub const VALUE_ERROR: ExcType = builtin("ValueError", EXCEPTION);
    pub const RUNTIME_ERROR: ExcType = builtin("RuntimeError", EXCEPTION);
    pub const RECURSION_ERROR: ExcType = builtin(
        "RecursionError",
        &["RuntimeError", "Exception", "BaseException"],
    );
    pub const UNICODE_DECODE_ERROR: ExcType = builtin(
        "UnicodeDecodeError",
        &["UnicodeError", "ValueError", "Exception", "BaseException"],
    );
    pub const UNICODE_ENCODE_ERROR: ExcType = builtin(
        "UnicodeEncodeError",
        &["UnicodeError", "ValueError", "Exception", "BaseException"],
    );
    pub const OS_ERROR: ExcType = builtin("OSError", EXCEPTION);
    pub const BLOCKING_IO_ERROR: ExcType = builtin("BlockingIOError", OS);
    pub const CHILD_PROCESS_ERROR: ExcType = builtin("ChildProcessError", OS);
    pub const BROKEN_PIPE_ERROR: ExcType = builtin("BrokenPipeError", CONNECTION);
    pub const CONNECTION_ABORTED_ERROR: ExcType = builtin("ConnectionAbortedError", CONNECTION);
    pub const CONNECTION_REFUSED_ERROR: ExcType = builtin("ConnectionRefusedError", CONNECTION);
    pub const CONNECTION_RESET_ERROR: ExcType = builtin("ConnectionResetError", CONNECTION);
    pub const FILE_EXISTS_ERROR: ExcType = builtin("FileExistsError", OS);
    pub const FILE_NOT_FOUND_ERROR: ExcType = builtin("FileNotFoundError", OS);
    pub const INTERRUPTED_ERROR: ExcType = builtin("InterruptedError", OS);
    pub const IS_A_DIRECTORY_ERROR: ExcType = builtin("IsADirectoryError", OS);
    pub const NOT_A_DIRECTORY_ERROR: ExcType = builtin("NotADirectoryError", OS);
    pub const PERMISSION_ERROR: ExcType = builtin("PermissionError", OS);
    pub const PROCESS_LOOKUP_ERROR: ExcType = builtin("ProcessLookupError", OS);
    pub const TIMEOUT_ERROR: ExcType = builtin("TimeoutError", OS);
}

/// errno の番号(OS ごとに違うもの)。
mod errno {
    #[cfg(target_os = "linux")]
    pub const EAGAIN: i32 = 11;
    #[cfg(target_os = "macos")]
    pub const EAGAIN: i32 = 35;
    #[cfg(target_os = "linux")]
    pub const EALREADY: i32 = 114;
    #[cfg(target_os = "macos")]
    pub const EALREADY: i32 = 37;
    #[cfg(target_os = "linux")]
    pub const EINPROGRESS: i32 = 115;
    #[cfg(target_os = "macos")]
    pub const EINPROGRESS: i32 = 36;
    #[cfg(target_os = "linux")]
    pub const ESHUTDOWN: i32 = 108;
    #[cfg(target_os = "macos")]
    pub const ESHUTDOWN: i32 = 58;
    #[cfg(target_os = "linux")]
    pub const ECONNABORTED: i32 = 103;
    #[cfg(target_os = "macos")]
    pub const ECONNABORTED: i32 = 53;
    #[cfg(target_os = "linux")]
    pub const ECONNREFUSED: i32 = 111;
    #[cfg(target_os = "macos")]
    pub const ECONNREFUSED: i32 = 61;
    #[cfg(target_os = "linux")]
    pub const ECONNRESET: i32 = 104;
    #[cfg(target_os = "macos")]
    pub const ECONNRESET: i32 = 54;
    #[cfg(target_os = "linux")]
    pub const ETIMEDOUT: i32 = 110;
    #[cfg(target_os = "macos")]
    pub const ETIMEDOUT: i32 = 60;
    // 2 つの OS で同じ番号。EWOULDBLOCK は EAGAIN と同じ。
    pub const EPERM: i32 = 1;
    pub const ENOENT: i32 = 2;
    pub const ESRCH: i32 = 3;
    pub const ECHILD: i32 = 10;
    pub const EACCES: i32 = 13;
    pub const EEXIST: i32 = 17;
    pub const ENOTDIR: i32 = 20;
    pub const EPIPE: i32 = 32;
}

/// `OSError(errno, …)` が選ぶ下位の型(CPython の `ADD_ERRNO` の表)。
pub fn os_error_type(number: i32) -> ExcType {
    use errno::*;
    match number {
        EAGAIN | EALREADY | EINPROGRESS => types::BLOCKING_IO_ERROR,
        EPIPE | ESHUTDOWN => types::BROKEN_PIPE_ERROR,
        ECHILD => types::CHILD_PROCESS_ERROR,
        ECONNABORTED => types::CONNECTION_ABORTED_ERROR,
        ECONNREFUSED => types::CONNECTION_REFUSED_ERROR,
        ECONNRESET => types::CONNECTION_RESET_ERROR,
        EEXIST => types::FILE_EXISTS_ERROR,
        ENOENT => types::FILE_NOT_FOUND_ERROR,
        EISDIR => types::IS_A_DIRECTORY_ERROR,
        ENOTDIR => types::NOT_A_DIRECTORY_ERROR,
        EINTR => types::INTERRUPTED_ERROR,
        EACCES | EPERM => types::PERMISSION_ERROR,
        ESRCH => types::PROCESS_LOOKUP_ERROR,
        ETIMEDOUT => types::TIMEOUT_ERROR,
        _ => types::OS_ERROR,
    }
}

// ---------------------------------------------------------------- 例外の値

/// `OSError` のファイル名(`filename`・`filename2`)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Filename {
    /// 文字列(表示は `repr`)。
    Str(PyStr),
    /// fd の番号など(表示は 10 進)。
    Int(i64),
}

impl Filename {
    fn repr(&self) -> PyStr {
        match self {
            Self::Str(text) => repr(text),
            Self::Int(number) => PyStr::from(number.to_string()),
        }
    }
}

/// `except` の後に `raise … from …` で連鎖したときの文。
pub const CAUSE_MESSAGE: &str =
    "\nThe above exception was the direct cause of the following exception:\n\n";
/// `except` の中で別の例外が起きたときの文。
pub const CONTEXT_MESSAGE: &str =
    "\nDuring handling of the above exception, another exception occurred:\n\n";

/// 例外の値(中身は [`ExceptionData`]。`Result` の誤りの側を小さく保つために箱へ入れる)。
#[derive(Clone, PartialEq, Eq)]
pub struct PyException(Box<ExceptionData>);

/// 例外の値の中身。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExceptionData {
    pub kind: ExcType,
    /// `str(例外)`。
    pub message: PyStr,
    /// `OSError` の仲間の errno。
    pub errno: Option<i32>,
    /// 呼出履歴。外側(最初に呼んだ所)から内側(例外が起きた所)の順。
    pub traceback: Vec<Frame>,
    /// `raise … from …` の元(`__cause__`)。
    pub cause: Option<PyException>,
    /// 処理中だった例外(`__context__`)。
    pub context: Option<PyException>,
    /// `__suppress_context__`(`from` を付けると真)。
    pub suppress_context: bool,
}

impl std::ops::Deref for PyException {
    type Target = ExceptionData;

    fn deref(&self) -> &ExceptionData {
        &self.0
    }
}

impl std::ops::DerefMut for PyException {
    fn deref_mut(&mut self) -> &mut ExceptionData {
        &mut self.0
    }
}

impl std::fmt::Debug for PyException {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(formatter)
    }
}

impl PyException {
    pub fn new(kind: ExcType, message: impl Into<PyStr>) -> Self {
        Self(Box::new(ExceptionData {
            kind,
            message: message.into(),
            errno: None,
            traceback: Vec::new(),
            cause: None,
            context: None,
            suppress_context: false,
        }))
    }

    /// `SIGINT` で起きる `KeyboardInterrupt`(文は空)。
    pub fn keyboard_interrupt() -> Self {
        Self::new(types::KEYBOARD_INTERRUPT, "")
    }

    /// `PyErr_SetFromErrno(PyExc_OSError)`: errno で型を選び、文は `[Errno N] <文>`。
    pub fn os(number: i32) -> Self {
        Self::os_with_filenames(number, None, None)
    }

    /// ファイル名つきの `OSError`(`[Errno N] <文>: <repr>`)。
    pub fn os_with_filename(number: i32, filename: Filename) -> Self {
        Self::os_with_filenames(number, Some(filename), None)
    }

    /// ファイル名 2 つの `OSError`(`[Errno N] <文>: <repr> -> <repr>`)。
    pub fn os_with_filenames(
        number: i32,
        filename: Option<Filename>,
        filename2: Option<Filename>,
    ) -> Self {
        let mut message = PyStr::from(format!("[Errno {number}] {}", strerror(number)));
        if let Some(first) = &filename {
            message.push_str(": ");
            message.push_py(&first.repr());
            if let Some(second) = &filename2 {
                message.push_str(" -> ");
                message.push_py(&second.repr());
            }
        }
        let mut exception = Self::new(os_error_type(number), message);
        exception.errno = Some(number);
        exception
    }

    /// `None.<attribute>` の `AttributeError`。
    pub fn none_has_no_attribute(attribute: &str) -> Self {
        Self::new(
            types::ATTRIBUTE_ERROR,
            format!("'NoneType' object has no attribute '{attribute}'"),
        )
    }

    /// 外側の呼出しの段を前に足す(例外が `stack` の最後の段の呼出しから出てきた)。
    pub fn called_from(mut self, stack: &[Frame]) -> Self {
        let mut traceback = stack.to_vec();
        traceback.append(&mut self.traceback);
        self.traceback = traceback;
        self
    }

    /// 呼出履歴の外側の `outer` 段で捕まえられた例外(その段までの呼出履歴だけを持つ。捕まえた段
    /// より外の段は通っていない)。
    pub fn caught_after(mut self, outer: usize) -> Self {
        let outer = outer.min(self.traceback.len());
        self.traceback.drain(..outer);
        self
    }

    /// `except` の中で起きた例外として、処理中だった例外をつなぐ(`__context__`)。
    pub fn with_context(mut self, context: PyException) -> Self {
        self.context = Some(context);
        self
    }

    /// `raise … from cause` としてつなぐ(`__cause__` と `__suppress_context__`)。
    pub fn with_cause(mut self, cause: PyException) -> Self {
        self.cause = Some(cause);
        self.suppress_context = true;
        self
    }

    /// 例外の最後の行(`[module.]名前` と、文が空でなければ `: 文`)。試験で使う。
    #[cfg(test)]
    pub fn type_line(&self) -> PyStr {
        let mut line = PyStr::from(self.kind.display_name());
        if !self.message.is_empty() {
            line.push_str(": ");
            line.push_py(&self.message);
        }
        line
    }
}

// ---------------------------------------------------------------- 文

/// `repr(str)`(CPython の `unicode_repr`)。`'` を含み `"` を含まなければ `"` で囲む。
/// 表示できない文字(`str.isprintable` が偽)は `\xHH`・`\uHHHH`・`\UHHHHHHHH` にする。
pub fn repr(text: &PyStr) -> PyStr {
    let points = text.as_code_points();
    let single = u32::from('\'');
    let double = u32::from('"');
    let quote = if points.contains(&single) && !points.contains(&double) {
        double
    } else {
        single
    };
    let mut result = PyStr::new();
    result.push(quote);
    for &code in points {
        match code {
            0x5c => result.push_str("\\\\"),
            0x09 => result.push_str("\\t"),
            0x0a => result.push_str("\\n"),
            0x0d => result.push_str("\\r"),
            _ if code == quote => {
                result.push(0x5c);
                result.push(quote);
            }
            _ if !r4b_unicode::printable(code) => {
                let escaped = if code <= 0xff {
                    format!("\\x{code:02x}")
                } else if code <= 0xffff {
                    format!("\\u{code:04x}")
                } else {
                    format!("\\U{code:08x}")
                };
                result.push_str(&escaped);
            }
            _ => result.push(code),
        }
    }
    result.push(quote);
    result
}

/// errno の文(Python の `os.strerror` と `OSError` の文)。
///
/// Linux は glibc 2.35 の `strerror` の表(Linux の x86_64 と aarch64 で番号は同じ)。表に無い番号は
/// glibc と同じ `Unknown error N`。Mac は OS の `strerror` の文。
pub fn strerror(number: i32) -> String {
    #[cfg(target_os = "linux")]
    {
        glibc_strerror(number).map_or_else(|| format!("Unknown error {number}"), str::to_owned)
    }
    #[cfg(target_os = "macos")]
    {
        // 標準ライブラリの文は「<OS の文> (os error N)」。
        let text = std::io::Error::from_raw_os_error(number).to_string();
        let suffix = format!(" (os error {number})");
        text.strip_suffix(&suffix).unwrap_or(&text).to_owned()
    }
}

/// glibc の `strerror` の表(glibc 2.35 の `ctypes` で取り出した文)。
#[cfg(target_os = "linux")]
fn glibc_strerror(number: i32) -> Option<&'static str> {
    Some(match number {
        0 => "Success",
        1 => "Operation not permitted",
        2 => "No such file or directory",
        3 => "No such process",
        4 => "Interrupted system call",
        5 => "Input/output error",
        6 => "No such device or address",
        7 => "Argument list too long",
        8 => "Exec format error",
        9 => "Bad file descriptor",
        10 => "No child processes",
        11 => "Resource temporarily unavailable",
        12 => "Cannot allocate memory",
        13 => "Permission denied",
        14 => "Bad address",
        15 => "Block device required",
        16 => "Device or resource busy",
        17 => "File exists",
        18 => "Invalid cross-device link",
        19 => "No such device",
        20 => "Not a directory",
        21 => "Is a directory",
        22 => "Invalid argument",
        23 => "Too many open files in system",
        24 => "Too many open files",
        25 => "Inappropriate ioctl for device",
        26 => "Text file busy",
        27 => "File too large",
        28 => "No space left on device",
        29 => "Illegal seek",
        30 => "Read-only file system",
        31 => "Too many links",
        32 => "Broken pipe",
        33 => "Numerical argument out of domain",
        34 => "Numerical result out of range",
        35 => "Resource deadlock avoided",
        36 => "File name too long",
        37 => "No locks available",
        38 => "Function not implemented",
        39 => "Directory not empty",
        40 => "Too many levels of symbolic links",
        42 => "No message of desired type",
        43 => "Identifier removed",
        44 => "Channel number out of range",
        45 => "Level 2 not synchronized",
        46 => "Level 3 halted",
        47 => "Level 3 reset",
        48 => "Link number out of range",
        49 => "Protocol driver not attached",
        50 => "No CSI structure available",
        51 => "Level 2 halted",
        52 => "Invalid exchange",
        53 => "Invalid request descriptor",
        54 => "Exchange full",
        55 => "No anode",
        56 => "Invalid request code",
        57 => "Invalid slot",
        59 => "Bad font file format",
        60 => "Device not a stream",
        61 => "No data available",
        62 => "Timer expired",
        63 => "Out of streams resources",
        64 => "Machine is not on the network",
        65 => "Package not installed",
        66 => "Object is remote",
        67 => "Link has been severed",
        68 => "Advertise error",
        69 => "Srmount error",
        70 => "Communication error on send",
        71 => "Protocol error",
        72 => "Multihop attempted",
        73 => "RFS specific error",
        74 => "Bad message",
        75 => "Value too large for defined data type",
        76 => "Name not unique on network",
        77 => "File descriptor in bad state",
        78 => "Remote address changed",
        79 => "Can not access a needed shared library",
        80 => "Accessing a corrupted shared library",
        81 => ".lib section in a.out corrupted",
        82 => "Attempting to link in too many shared libraries",
        83 => "Cannot exec a shared library directly",
        84 => "Invalid or incomplete multibyte or wide character",
        85 => "Interrupted system call should be restarted",
        86 => "Streams pipe error",
        87 => "Too many users",
        88 => "Socket operation on non-socket",
        89 => "Destination address required",
        90 => "Message too long",
        91 => "Protocol wrong type for socket",
        92 => "Protocol not available",
        93 => "Protocol not supported",
        94 => "Socket type not supported",
        95 => "Operation not supported",
        96 => "Protocol family not supported",
        97 => "Address family not supported by protocol",
        98 => "Address already in use",
        99 => "Cannot assign requested address",
        100 => "Network is down",
        101 => "Network is unreachable",
        102 => "Network dropped connection on reset",
        103 => "Software caused connection abort",
        104 => "Connection reset by peer",
        105 => "No buffer space available",
        106 => "Transport endpoint is already connected",
        107 => "Transport endpoint is not connected",
        108 => "Cannot send after transport endpoint shutdown",
        109 => "Too many references: cannot splice",
        110 => "Connection timed out",
        111 => "Connection refused",
        112 => "Host is down",
        113 => "No route to host",
        114 => "Operation already in progress",
        115 => "Operation now in progress",
        116 => "Stale file handle",
        117 => "Structure needs cleaning",
        118 => "Not a XENIX named type file",
        119 => "No XENIX semaphores available",
        120 => "Is a named type file",
        121 => "Remote I/O error",
        122 => "Disk quota exceeded",
        123 => "No medium found",
        124 => "Wrong medium type",
        125 => "Operation canceled",
        126 => "Required key not available",
        127 => "Key has expired",
        128 => "Key has been revoked",
        129 => "Key was rejected by service",
        130 => "Owner died",
        131 => "State not recoverable",
        132 => "Operation not possible due to RF-kill",
        133 => "Memory page has hardware error",
        _ => return None,
    })
}

/// `UnicodeDecodeError` の文の書き方(CPython の `UnicodeDecodeError_str`)。
pub fn decode_error_message(data: &[u8], start: usize, end: usize, reason: &str) -> String {
    if end == start + 1 {
        format!(
            "'utf-8' codec can't decode byte 0x{:02x} in position {start}: {reason}",
            data[start]
        )
    } else {
        format!(
            "'utf-8' codec can't decode bytes in position {start}-{}: {reason}",
            end - 1
        )
    }
}

/// 厳密な UTF-8 の読み取り(`bytes.decode("utf-8")`)。壊れていれば、最初の誤りの
/// `UnicodeDecodeError`(位置と理由は CPython の UTF-8 の読み手と同じ)。
pub fn decode_utf8(data: &[u8]) -> Result<&str, PyException> {
    std::str::from_utf8(data).map_err(|error| {
        let start = error.valid_up_to();
        // CPython: 始めのバイトが不正(0x80〜0xC1・0xF5〜)なら 1 バイト、データの終わりで途切れたら
        // 終わりまで、続きのバイトが不正なら正しかった所まで。Rust の error_len と同じ区切り。
        let (end, reason) = match error.error_len() {
            None => (data.len(), "unexpected end of data"),
            Some(length) if !(0xc2..=0xf4).contains(&data[start]) => {
                (start + length, "invalid start byte")
            }
            Some(length) => (start + length, "invalid continuation byte"),
        };
        PyException::new(
            types::UNICODE_DECODE_ERROR,
            decode_error_message(data, start, end, reason),
        )
    })
}

/// `UnicodeEncodeError`(UTF-8 で書けないサロゲート)。`start..end` は書けない文字を含む範囲。
pub fn unicode_encode_error(text: &PyStr, start: usize, end: usize) -> PyException {
    let message = if end == start + 1 {
        let code = text.as_code_points()[start];
        let escaped = if code <= 0xff {
            format!("\\x{code:02x}")
        } else if code <= 0xffff {
            format!("\\u{code:04x}")
        } else {
            format!("\\U{code:08x}")
        };
        format!(
            "'utf-8' codec can't encode character '{escaped}' in position {start}: surrogates not allowed"
        )
    } else {
        format!(
            "'utf-8' codec can't encode characters in position {start}-{}: surrogates not allowed",
            end - 1
        )
    };
    PyException::new(types::UNICODE_ENCODE_ERROR, message)
}

// ---------------------------------------------------------------- 呼出履歴の段

/// 呼出履歴の 1 段。ファイル名は決定の固定名(旧スクリプトは `md_tables_to_xlsx.py`、標準ライブラリ
/// は標準ライブラリのディレクトリからの相対、導入した部品は site-packages からの相対)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Frame {
    pub file: &'static str,
    pub line: u32,
    /// `co_name`(関数の名前。モジュールの段は `<module>`)。
    pub function: &'static str,
    /// その行のソース(ファイルの行のまま。行末の改行は除く)。
    pub source: &'static str,
}

impl Frame {
    pub const fn new(
        file: &'static str,
        line: u32,
        function: &'static str,
        source: &'static str,
    ) -> Self {
        Self {
            file,
            line,
            function,
            source,
        }
    }

    /// `tb_printinternal` の繰り返しの判定(ファイル・行・関数が同じ)。
    pub fn same_place(&self, other: &Frame) -> bool {
        self.file == other.file && self.line == other.line && self.function == other.function
    }

    /// `  File "<file>", line N, in <function>` の行(改行つき)。
    pub fn file_line(&self) -> PyStr {
        PyStr::from(format!(
            "  File \"{}\", line {}, in {}\n",
            self.file, self.line, self.function
        ))
    }

    /// 呼出履歴に出すソースの行(`_Py_DisplaySourceLine`: 先頭の空白・タブ・改ページだけを除く)。
    /// 行が無ければ `None`(ソースの行を出さない)。
    pub fn display_source(&self) -> Option<&'static str> {
        if self.source.is_empty() {
            return None;
        }
        Some(self.source.trim_start_matches([' ', '\t', '\x0c']))
    }
}

/// `  [Previous line repeated N more time(s)]`(改行つき)。
pub fn repeated_line(more: usize) -> PyStr {
    let unit = if more > 1 { "times" } else { "time" };
    PyStr::from(format!("  [Previous line repeated {more} more {unit}]\n"))
}

/// 旧版の呼出履歴の段と、関数の並び(外側から内側の順)。
///
/// 段は実物(旧スクリプト・本家 CPython 3.10.11 の標準ライブラリ・openpyxl 3.1.5)の行番号と
/// ソースの行を写す。後の単位は、要る段をここへ足し、[`ALL`] にも並べる。検査入口の
/// `R4E_CONTRACT_FRAMES` が [`ALL`] を出し、実物と照らす。
pub mod frames {
    use super::Frame;

    /// 旧スクリプトの固定名。
    pub const SCRIPT: &str = "md_tables_to_xlsx.py";

    pub const MODULE: Frame = Frame::new(SCRIPT, 241, "<module>", "    sys.exit(main())");
    pub const MAIN_CALLS_WRITE_XLSX: Frame = Frame::new(
        SCRIPT,
        233,
        "main",
        "            write_xlsx(tables, out, inputs=inputs, source_roots=args.source_root)",
    );
    pub const MAIN_PRINTS_WARN: Frame = Frame::new(
        SCRIPT,
        219,
        "main",
        "        print(f\"WARN 入力が存在しない: {p}\", file=sys.stderr)",
    );
    pub const MAIN_PRINTS_NO_TABLES: Frame = Frame::new(
        SCRIPT,
        223,
        "main",
        "        print(\"表が見つからなかった(出力なし)\")",
    );
    pub const WRITE_XLSX_PRINTS_OK: Frame = Frame::new(
        SCRIPT,
        197,
        "write_xlsx",
        "    print(f\"OK {out} にシート {len(wb.sheetnames)} 件を出力\")",
    );
    pub const PATHLIB_MKDIR_ACCESSOR: Frame = Frame::new(
        "pathlib.py",
        1175,
        "mkdir",
        "            self._accessor.mkdir(self, mode)",
    );
    pub const PATHLIB_MKDIR_PARENT: Frame = Frame::new(
        "pathlib.py",
        1179,
        "mkdir",
        "            self.parent.mkdir(parents=True, exist_ok=True)",
    );

    // ---- 入力の読取り(Task 2.1)
    /// 入力の繰り返し(信号を確かめる所)。
    pub const MAIN_INPUT_LOOP: Frame = Frame::new(SCRIPT, 211, "main", "    for p in args.inputs:");
    pub const MAIN_CHECKS_IS_FILE: Frame =
        Frame::new(SCRIPT, 213, "main", "        if not path.is_file():");
    pub const MAIN_READS_INPUT: Frame = Frame::new(
        SCRIPT,
        216,
        "main",
        "        tables += extract_tables(path.read_text(encoding=\"utf-8\"), path.stem)",
    );
    pub const PATHLIB_IS_FILE: Frame = Frame::new(
        "pathlib.py",
        1322,
        "is_file",
        "            return S_ISREG(self.stat().st_mode)",
    );
    pub const PATHLIB_STAT: Frame = Frame::new(
        "pathlib.py",
        1097,
        "stat",
        "        return self._accessor.stat(self, follow_symlinks=follow_symlinks)",
    );
    pub const PATHLIB_READ_TEXT_OPENS: Frame = Frame::new(
        "pathlib.py",
        1134,
        "read_text",
        "        with self.open(mode='r', encoding=encoding, errors=errors) as f:",
    );
    pub const PATHLIB_OPEN: Frame = Frame::new(
        "pathlib.py",
        1119,
        "open",
        "        return self._accessor.open(self, mode, buffering, encoding, errors,",
    );
    pub const PATHLIB_READ_TEXT_READS: Frame = Frame::new(
        "pathlib.py",
        1135,
        "read_text",
        "            return f.read()",
    );
    /// `decode` の段に入る所(段の最初の命令で信号を確かめる。行は `def` の行)。
    pub const CODECS_DECODE_ENTRY: Frame = Frame::new(
        "codecs.py",
        319,
        "decode",
        "    def decode(self, input, final=False):",
    );
    pub const CODECS_DECODE: Frame = Frame::new(
        "codecs.py",
        322,
        "decode",
        "        (result, consumed) = self._buffer_decode(data, self.errors, final)",
    );

    // ---- ブックの組み立て(Task 2.3)
    /// 表の繰り返し(信号を確かめる所)。
    pub const WRITE_XLSX_LOOP: Frame =
        Frame::new(SCRIPT, 180, "write_xlsx", "    for hint, rows in tables:");
    pub const WRITE_XLSX_CREATES_SHEET: Frame = Frame::new(
        SCRIPT,
        181,
        "write_xlsx",
        "        ws = wb.create_sheet(unique_sheet_name(hint, used))",
    );
    pub const WRITE_XLSX_APPENDS: Frame =
        Frame::new(SCRIPT, 183, "write_xlsx", "            ws.append(r)");
    pub const WRITE_XLSX_READS_CELL: Frame = Frame::new(
        SCRIPT,
        190,
        "write_xlsx",
        "                v = ws.cell(row=row, column=col).value or \"\"",
    );
    pub const WRITE_XLSX_SETS_WIDTH: Frame = Frame::new(
        SCRIPT,
        194,
        "write_xlsx",
        "            ws.column_dimensions[get_column_letter(col)].width = width",
    );
    pub const WORKSHEET_APPEND: Frame = Frame::new(
        "openpyxl/worksheet/worksheet.py",
        673,
        "append",
        "                    cell = Cell(self, row=row_idx, column=col_idx, value=content)",
    );
    pub const WORKSHEET_CELL: Frame = Frame::new(
        "openpyxl/worksheet/worksheet.py",
        244,
        "cell",
        "        cell = self._get_cell(row, column)",
    );
    pub const WORKSHEET_GET_CELL: Frame = Frame::new(
        "openpyxl/worksheet/worksheet.py",
        257,
        "_get_cell",
        "            raise ValueError(f\"Row numbers must be between 1 and 1048576. Row number supplied was {row}\")",
    );
    pub const CELL_INIT: Frame = Frame::new(
        "openpyxl/cell/cell.py",
        119,
        "__init__",
        "            self.value = value",
    );
    pub const CELL_VALUE_SETTER: Frame = Frame::new(
        "openpyxl/cell/cell.py",
        218,
        "value",
        "        self._bind_value(value)",
    );
    pub const CELL_BIND_VALUE: Frame = Frame::new(
        "openpyxl/cell/cell.py",
        197,
        "_bind_value",
        "            value = self.check_string(value)",
    );
    pub const CELL_CHECK_STRING: Frame = Frame::new(
        "openpyxl/cell/cell.py",
        165,
        "check_string",
        "            raise IllegalCharacterError(f\"{value} cannot be used in worksheets.\")",
    );
    pub const UTILS_GET_COLUMN_LETTER: Frame = Frame::new(
        "openpyxl/utils/cell.py",
        94,
        "get_column_letter",
        "        raise ValueError(\"Invalid column index {0}\".format(col_idx))",
    );

    /// `print(f"WARN 入力が存在しない: {p}", file=sys.stderr)`。
    pub const PRINT_WARN: &[Frame] = &[MODULE, MAIN_PRINTS_WARN];
    /// `print("表が見つからなかった(出力なし)")`。
    pub const PRINT_NO_TABLES: &[Frame] = &[MODULE, MAIN_PRINTS_NO_TABLES];
    /// `print(f"OK {out} にシート {len(wb.sheetnames)} 件を出力")`。
    pub const PRINT_OK: &[Frame] = &[MODULE, MAIN_CALLS_WRITE_XLSX, WRITE_XLSX_PRINTS_OK];

    /// 表のすべての段(実物と照らす対象)。
    pub const ALL: &[Frame] = &[
        MODULE,
        MAIN_CALLS_WRITE_XLSX,
        MAIN_PRINTS_WARN,
        MAIN_PRINTS_NO_TABLES,
        WRITE_XLSX_PRINTS_OK,
        PATHLIB_MKDIR_ACCESSOR,
        PATHLIB_MKDIR_PARENT,
        MAIN_INPUT_LOOP,
        MAIN_CHECKS_IS_FILE,
        MAIN_READS_INPUT,
        PATHLIB_IS_FILE,
        PATHLIB_STAT,
        PATHLIB_READ_TEXT_OPENS,
        PATHLIB_OPEN,
        PATHLIB_READ_TEXT_READS,
        CODECS_DECODE_ENTRY,
        CODECS_DECODE,
        WRITE_XLSX_LOOP,
        WRITE_XLSX_CREATES_SHEET,
        WRITE_XLSX_APPENDS,
        WRITE_XLSX_READS_CELL,
        WRITE_XLSX_SETS_WIDTH,
        WORKSHEET_APPEND,
        WORKSHEET_CELL,
        WORKSHEET_GET_CELL,
        CELL_INIT,
        CELL_VALUE_SETTER,
        CELL_BIND_VALUE,
        CELL_CHECK_STRING,
        UTILS_GET_COLUMN_LETTER,
    ];
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn os_errors_choose_the_subtype_and_text() {
        let error = PyException::os_with_filename(2, Filename::Str(PyStr::from("a'b")));
        assert_eq!(error.kind, types::FILE_NOT_FOUND_ERROR);
        assert_eq!(error.errno, Some(2));
        assert_eq!(
            error.type_line().to_string_lossy(),
            "FileNotFoundError: [Errno 2] No such file or directory: \"a'b\""
        );
        let error = PyException::os_with_filenames(
            17,
            Some(Filename::Str(PyStr::from_os_bytes(b"x\xff"))),
            Some(Filename::Str(PyStr::from("y"))),
        );
        assert_eq!(
            error.type_line().encode_backslashreplace(),
            b"FileExistsError: [Errno 17] File exists: 'x\\udcff' -> 'y'"
        );
        assert_eq!(
            PyException::os_with_filename(EISDIR, Filename::Int(1))
                .type_line()
                .to_string_lossy(),
            "IsADirectoryError: [Errno 21] Is a directory: 1"
        );
        for (number, name) in [
            (1, "PermissionError"),
            (13, "PermissionError"),
            (20, "NotADirectoryError"),
            (32, "BrokenPipeError"),
            (EINTR, "InterruptedError"),
            (EFBIG, "OSError"),
            (28, "OSError"),
            (errno::EAGAIN, "BlockingIOError"),
            (errno::ETIMEDOUT, "TimeoutError"),
        ] {
            assert_eq!(os_error_type(number).name, name, "{number}");
        }
        assert!(types::BROKEN_PIPE_ERROR.is("OSError"));
        assert!(types::RECURSION_ERROR.is("RuntimeError"));
        assert!(!types::KEYBOARD_INTERRUPT.is("Exception"));
        assert!(types::UNICODE_DECODE_ERROR.is("ValueError"));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn errno_texts_are_glibc() {
        for (number, text) in [
            (0, "Success"),
            (2, "No such file or directory"),
            (13, "Permission denied"),
            (21, "Is a directory"),
            (27, "File too large"),
            (28, "No space left on device"),
            (32, "Broken pipe"),
            (36, "File name too long"),
            (40, "Too many levels of symbolic links"),
            (41, "Unknown error 41"),
            (58, "Unknown error 58"),
            (95, "Operation not supported"),
            (133, "Memory page has hardware error"),
            (134, "Unknown error 134"),
            (-1, "Unknown error -1"),
        ] {
            assert_eq!(strerror(number), text, "{number}");
        }
    }

    /// Mac は OS の文(よく出る番号は glibc と同じ字面)。
    #[cfg(target_os = "macos")]
    #[test]
    fn errno_texts_are_the_os_texts() {
        for (number, text) in [
            (2, "No such file or directory"),
            (13, "Permission denied"),
            (21, "Is a directory"),
            (27, "File too large"),
            (28, "No space left on device"),
            (32, "Broken pipe"),
        ] {
            assert_eq!(strerror(number), text, "{number}");
        }
    }

    #[test]
    fn repr_matches_python() {
        let cases: [(&[u32], &str); 7] = [
            (&[0x61, 0x27, 0x62], "\"a'b\""),
            (&[0x61, 0x27, 0x22], "'a\\'\"'"),
            (
                &[0x5c, 0x09, 0x0a, 0x0d, 0x00, 0x7f],
                "'\\\\\\t\\n\\r\\x00\\x7f'",
            ),
            (&[0x3042, 0xa0, 0xad], "'あ\\xa0\\xad'"),
            (&[0xdcff, 0xd800], "'\\udcff\\ud800'"),
            (&[0x2028, 0xe000, 0x10ffff], "'\\u2028\\ue000\\U0010ffff'"),
            (&[0x1f600], "'\u{1f600}'"),
        ];
        for (points, expected) in cases {
            let text = PyStr::from_code_points(points.to_vec());
            assert_eq!(repr(&text).to_string_lossy(), expected, "{points:x?}");
        }
    }

    #[test]
    fn utf8_errors_match_python() {
        for (data, expected) in [
            (
                &b"\xff"[..],
                "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
            ),
            (
                b"ab\x80",
                "'utf-8' codec can't decode byte 0x80 in position 2: invalid start byte",
            ),
            (
                b"\xc0\x80",
                "'utf-8' codec can't decode byte 0xc0 in position 0: invalid start byte",
            ),
            (
                b"a\xe3\x81",
                "'utf-8' codec can't decode bytes in position 1-2: unexpected end of data",
            ),
            (
                b"\xe3",
                "'utf-8' codec can't decode byte 0xe3 in position 0: unexpected end of data",
            ),
            (
                b"\xe3\x81A",
                "'utf-8' codec can't decode bytes in position 0-1: invalid continuation byte",
            ),
            (
                b"\xed\xa0\x80",
                "'utf-8' codec can't decode byte 0xed in position 0: invalid continuation byte",
            ),
            (
                b"\xf4\x90\x80\x80",
                "'utf-8' codec can't decode byte 0xf4 in position 0: invalid continuation byte",
            ),
            (
                b"\xe0\x80\x80",
                "'utf-8' codec can't decode byte 0xe0 in position 0: invalid continuation byte",
            ),
        ] {
            let error = decode_utf8(data).unwrap_err();
            assert_eq!(error.kind, types::UNICODE_DECODE_ERROR);
            assert_eq!(error.message.to_string_lossy(), expected, "{data:x?}");
        }
        assert_eq!(decode_utf8("\u{feff}a".as_bytes()).unwrap(), "\u{feff}a");
        let text = PyStr::from_code_points(vec![0x41, 0xd800]);
        assert_eq!(
            unicode_encode_error(&text, 1, 2)
                .type_line()
                .to_string_lossy(),
            "UnicodeEncodeError: 'utf-8' codec can't encode character '\\ud800' in position 1: surrogates not allowed"
        );
    }

    #[test]
    fn frames_display_like_the_c_traceback() {
        let frame = frames::MAIN_CALLS_WRITE_XLSX;
        assert_eq!(
            frame.file_line().to_string_lossy(),
            "  File \"md_tables_to_xlsx.py\", line 233, in main\n"
        );
        assert_eq!(
            frame.display_source(),
            Some("write_xlsx(tables, out, inputs=inputs, source_roots=args.source_root)")
        );
        // 行末の空白は残す(C の表示は先頭だけを除く)。
        let trailing = Frame::new("x.py", 1, "f", " \t\x0cx = 1  ");
        assert_eq!(trailing.display_source(), Some("x = 1  "));
        assert_eq!(Frame::new("x.py", 1, "f", "").display_source(), None);
        assert_eq!(
            repeated_line(1).to_string_lossy(),
            "  [Previous line repeated 1 more time]\n"
        );
        assert_eq!(
            repeated_line(996).to_string_lossy(),
            "  [Previous line repeated 996 more times]\n"
        );
    }

    /// 旧スクリプトの段は、リポジトリの旧スクリプトの行と同じ(標準ライブラリと openpyxl の段は、
    /// 検査入口の `R4E_CONTRACT_FRAMES` で旧版の環境の実物と照らす)。
    #[test]
    fn script_frames_match_the_legacy_script() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py");
        let source = std::fs::read_to_string(&path).unwrap();
        let lines = source.split('\n').collect::<Vec<_>>();
        let mut checked = 0;
        for frame in frames::ALL
            .iter()
            .filter(|frame| frame.file == frames::SCRIPT)
        {
            assert_eq!(lines[frame.line as usize - 1], frame.source, "{frame:?}");
            // 関数の名前は、その行より前で最も近い、より浅い字下げの def(無ければ <module>)。
            let indent = frame.source.len() - frame.source.trim_start().len();
            let owner = lines[..frame.line as usize - 1]
                .iter()
                .rev()
                .find(|line| {
                    let depth = line.len() - line.trim_start().len();
                    depth < indent && line.trim_start().starts_with("def ")
                        || depth == 0 && !line.trim().is_empty() && !line.starts_with('#')
                })
                .and_then(|line| line.trim_start().strip_prefix("def "))
                .and_then(|rest| rest.split('(').next());
            assert_eq!(owner.unwrap_or("<module>"), frame.function, "{frame:?}");
            checked += 1;
        }
        assert_eq!(checked, 13);
    }
}
