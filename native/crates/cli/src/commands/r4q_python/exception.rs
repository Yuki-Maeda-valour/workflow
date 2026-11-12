//! 旧版の例外の `str(exc)` の形と、strerror の表。
//! - `OSError`: `[Errno N] <strerror>`、ファイル名があれば `: <repr>`、2 つ目があれば ` -> <repr>`
//!   (本家 3.10.11 の `Objects/exceptions.c` の `OSError_str`)。
//! - strerror(2026-11-12 08:55 の決定): Rust が基準の機械の表を持って出す(OS の strerror を呼ばない)。
//!   Linux は R4U の glibc の表(`oserror::glibc_strerror`)、Mac は Apple の公開の Libc の
//!   `gen/errlst.c` の errno 1〜106 の表。表に無い番号は libc と同じ形(Linux `Unknown error <n>`、
//!   Mac `Unknown error: <n>`)。表を選ぶのは組み立ての時点の判定(cfg)。
//! - `TimeoutExpired`: `Command '<argv の list の repr>' timed out after <秒> seconds`。
//! - pathlib: `str(PurePosixPath(directory) / name)`。

use super::number::PyNumber;
use super::oserror::{self, Os};
use super::path::PurePath;
use super::value::{self, PyValue};

/// Apple の Libc(`gen/errlst.c`)の `sys_errlist` の errno 1〜106。
/// 出典: <https://github.com/apple-oss-distributions/Libc> の `gen/errlst.c`(106 は EQFULL)。
pub const MAC_STRERROR: [&str; 106] = [
    "Operation not permitted",
    "No such file or directory",
    "No such process",
    "Interrupted system call",
    "Input/output error",
    "Device not configured",
    "Argument list too long",
    "Exec format error",
    "Bad file descriptor",
    "No child processes",
    "Resource deadlock avoided",
    "Cannot allocate memory",
    "Permission denied",
    "Bad address",
    "Block device required",
    "Resource busy",
    "File exists",
    "Cross-device link",
    "Operation not supported by device",
    "Not a directory",
    "Is a directory",
    "Invalid argument",
    "Too many open files in system",
    "Too many open files",
    "Inappropriate ioctl for device",
    "Text file busy",
    "File too large",
    "No space left on device",
    "Illegal seek",
    "Read-only file system",
    "Too many links",
    "Broken pipe",
    "Numerical argument out of domain",
    "Result too large",
    "Resource temporarily unavailable",
    "Operation now in progress",
    "Operation already in progress",
    "Socket operation on non-socket",
    "Destination address required",
    "Message too long",
    "Protocol wrong type for socket",
    "Protocol not available",
    "Protocol not supported",
    "Socket type not supported",
    "Operation not supported",
    "Protocol family not supported",
    "Address family not supported by protocol family",
    "Address already in use",
    "Can't assign requested address",
    "Network is down",
    "Network is unreachable",
    "Network dropped connection on reset",
    "Software caused connection abort",
    "Connection reset by peer",
    "No buffer space available",
    "Socket is already connected",
    "Socket is not connected",
    "Can't send after socket shutdown",
    "Too many references: can't splice",
    "Operation timed out",
    "Connection refused",
    "Too many levels of symbolic links",
    "File name too long",
    "Host is down",
    "No route to host",
    "Directory not empty",
    "Too many processes",
    "Too many users",
    "Disc quota exceeded",
    "Stale NFS file handle",
    "Too many levels of remote in path",
    "RPC struct is bad",
    "RPC version wrong",
    "RPC prog. not avail",
    "Program version wrong",
    "Bad procedure for program",
    "No locks available",
    "Function not implemented",
    "Inappropriate file type or format",
    "Authentication error",
    "Need authenticator",
    "Device power is off",
    "Device error",
    "Value too large to be stored in data type",
    "Bad executable (or shared library)",
    "Bad CPU type in executable",
    "Shared library version mismatch",
    "Malformed Mach-o file",
    "Operation canceled",
    "Identifier removed",
    "No message of desired type",
    "Illegal byte sequence",
    "Attribute not found",
    "Bad message",
    "EMULTIHOP (Reserved)",
    "No message available on STREAM",
    "ENOLINK (Reserved)",
    "No STREAM resources",
    "Not a STREAM",
    "Protocol error",
    "STREAM ioctl timeout",
    "Operation not supported on socket",
    "Policy not found",
    "State not recoverable",
    "Previous owner died",
    "Interface output queue is full",
];

/// Mac(Apple の Libc)の strerror。0 は `sys_errlist[0]` の `Undefined error: 0`。表に無い番号
/// (107 以上と負の数)は `Unknown error: <n>`(`__errstr`)。
pub fn mac_strerror(errno: i32) -> String {
    if errno == 0 {
        return "Undefined error: 0".to_owned();
    }
    usize::try_from(errno)
        .ok()
        .filter(|&number| number >= 1)
        .and_then(|number| MAC_STRERROR.get(number - 1))
        .map_or_else(
            || format!("Unknown error: {errno}"),
            |text| (*text).to_owned(),
        )
}

/// 指定した OS の表の strerror(表と、表に無いときの文を合わせた関数)。
pub fn strerror_on(os: Os, errno: i32) -> String {
    match os {
        Os::Linux => oserror::glibc_strerror(errno),
        Os::Macos => mac_strerror(errno),
    }
}

/// この実行物の OS の表の strerror。
pub fn strerror(errno: i32) -> String {
    strerror_on(oserror::CURRENT, errno)
}

/// `OSError` のファイル名(`filename`・`filename2`)の値。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Filename {
    /// str(argv などを surrogateescape で復号した値を含む)。
    Str(Vec<u32>),
    /// bytes。
    Bytes(Vec<u8>),
    /// int(fd の番号)。
    Int(i64),
}

impl Filename {
    /// surrogateescape のバイト列から作る str。
    pub fn from_raw(raw: &[u8]) -> Self {
        Filename::Str(super::code_points(raw))
    }

    fn repr(&self) -> String {
        match self {
            Filename::Str(codes) => value::text(&value::str_repr(codes)),
            Filename::Bytes(raw) => value::text(&value::bytes_repr(raw)),
            Filename::Int(number) => number.to_string(),
        }
    }
}

/// `OSError` の値(`errno`・`strerror`・`filename`・`filename2`)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OsError {
    pub errno: i32,
    /// `os.strerror(errno)` か、旧版が渡した文(`OSError(errno, 'subreaper を設定できない')`)。
    pub strerror: String,
    pub filename: Option<Filename>,
    pub filename2: Option<Filename>,
}

impl OsError {
    /// OS の誤り(strerror はこの実行物の OS の表)。
    pub fn os(errno: i32, filename: Option<Filename>) -> Self {
        Self {
            errno,
            strerror: strerror(errno),
            filename,
            filename2: None,
        }
    }

    /// `type(exc).__name__`(errno から選ぶ。この実行物の OS の番号)。
    pub fn type_name(&self) -> &'static str {
        oserror::exception_type(self.errno)
    }

    /// `str(exc)`。
    pub fn message(&self) -> String {
        match (&self.filename, &self.filename2) {
            (Some(first), Some(second)) => format!(
                "[Errno {}] {}: {} -> {}",
                self.errno,
                self.strerror,
                first.repr(),
                second.repr()
            ),
            (Some(first), None) => {
                format!("[Errno {}] {}: {}", self.errno, self.strerror, first.repr())
            }
            _ => format!("[Errno {}] {}", self.errno, self.strerror),
        }
    }
}

/// `str(KeyError(key))`(鍵の repr)。
pub fn key_error_message(key: &PyValue) -> String {
    value::text(&key.repr())
}

/// `str(subprocess.TimeoutExpired(cmd, timeout))`。`cmd` は argv(str の list)。
pub fn timeout_expired_message(cmd: &[Vec<u32>], timeout: &PyNumber) -> String {
    let list = PyValue::List(cmd.iter().cloned().map(PyValue::Str).collect());
    format!(
        "Command '{}' timed out after {} seconds",
        value::text(&list.repr()),
        timeout.to_str()
    )
}

/// `str(PurePosixPath(directory) / name)`(バイト列。surrogateescape と 1 対 1)。
pub fn path_child(directory: &[u8], name: &[u8]) -> Vec<u8> {
    PurePath::new(directory).join(name).to_bytes()
}
