//! OS エラーの例外の種類と文。
//! - 種類は errno から Python 3.10 と同じに選ぶ(`Objects/exceptions.c` の errnomap)。errno の番号は OS ごとに違う。
//! - Linux の文は glibc の strerror の固定表を使う(配布物の musl の文を使わない)。Mac の文は OS の文を使う(別の段)。

/// errnomap が OSError の下位の種類へ結び付ける errno の名前・種類・Linux と Mac の番号。
/// EWOULDBLOCK は両方の OS で EAGAIN と同じ番号なので載せない。
/// Linux の列は本家 3.10.11 の実測(native/fixtures/r4u/linux.json)と照らす。
/// Mac の列は Apple の sys/errno.h の値で、Mac の CI の比較で旧版と照らす。
pub const NAMED: [(&str, &str, i32, i32); 18] = [
    ("EPERM", "PermissionError", 1, 1),
    ("ENOENT", "FileNotFoundError", 2, 2),
    ("ESRCH", "ProcessLookupError", 3, 3),
    ("EINTR", "InterruptedError", 4, 4),
    ("ECHILD", "ChildProcessError", 10, 10),
    ("EAGAIN", "BlockingIOError", 11, 35),
    ("EACCES", "PermissionError", 13, 13),
    ("EEXIST", "FileExistsError", 17, 17),
    ("ENOTDIR", "NotADirectoryError", 20, 20),
    ("EISDIR", "IsADirectoryError", 21, 21),
    ("EPIPE", "BrokenPipeError", 32, 32),
    ("EINPROGRESS", "BlockingIOError", 115, 36),
    ("EALREADY", "BlockingIOError", 114, 37),
    ("ECONNABORTED", "ConnectionAbortedError", 103, 53),
    ("ECONNRESET", "ConnectionResetError", 104, 54),
    ("ESHUTDOWN", "BrokenPipeError", 108, 58),
    ("ETIMEDOUT", "TimeoutError", 110, 60),
    ("ECONNREFUSED", "ConnectionRefusedError", 111, 61),
];

/// 番号の列を選ぶ OS。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Os {
    Linux,
    Macos,
}

/// この実行物の OS。
pub const CURRENT: Os = if cfg!(target_os = "macos") {
    Os::Macos
} else {
    Os::Linux
};

/// `type(OSError(errno, ...)).__name__`(この実行物の OS の番号で選ぶ)。
pub fn exception_type(errno: i32) -> &'static str {
    exception_type_on(CURRENT, errno)
}

/// 指定した OS の番号で、errno から例外の種類を選ぶ。
pub fn exception_type_on(os: Os, errno: i32) -> &'static str {
    NAMED
        .iter()
        .find(|&&(_, _, linux, macos)| errno == if os == Os::Linux { linux } else { macos })
        .map_or("OSError", |&(_, name, _, _)| name)
}

/// glibc(Linux)の strerror の文。表に無い番号(41・58 と 134 以上と負の数)は `Unknown error N`。
pub fn glibc_strerror(errno: i32) -> String {
    usize::try_from(errno)
        .ok()
        .and_then(|index| GLIBC.get(index))
        .filter(|text| !text.is_empty())
        .map_or_else(
            || format!("Unknown error {errno}"),
            |text| (*text).to_owned(),
        )
}

/// OSError の str。ファイル名があれば `[Errno N] 文: 'repr'`、無ければ `[Errno N] 文`。
pub fn os_error_text(errno: i32, strerror: &str, filename: Option<&[u8]>) -> String {
    match filename {
        Some(filename) => format!("[Errno {errno}] {strerror}: {}", super::repr(filename)),
        None => format!("[Errno {errno}] {strerror}"),
    }
}

/// glibc 2.35 の strerror(0〜133)。空は glibc の表に無い番号。
const GLIBC: [&str; 134] = [
    "Success",
    "Operation not permitted",
    "No such file or directory",
    "No such process",
    "Interrupted system call",
    "Input/output error",
    "No such device or address",
    "Argument list too long",
    "Exec format error",
    "Bad file descriptor",
    "No child processes",
    "Resource temporarily unavailable",
    "Cannot allocate memory",
    "Permission denied",
    "Bad address",
    "Block device required",
    "Device or resource busy",
    "File exists",
    "Invalid cross-device link",
    "No such device",
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
    "Numerical result out of range",
    "Resource deadlock avoided",
    "File name too long",
    "No locks available",
    "Function not implemented",
    "Directory not empty",
    "Too many levels of symbolic links",
    "",
    "No message of desired type",
    "Identifier removed",
    "Channel number out of range",
    "Level 2 not synchronized",
    "Level 3 halted",
    "Level 3 reset",
    "Link number out of range",
    "Protocol driver not attached",
    "No CSI structure available",
    "Level 2 halted",
    "Invalid exchange",
    "Invalid request descriptor",
    "Exchange full",
    "No anode",
    "Invalid request code",
    "Invalid slot",
    "",
    "Bad font file format",
    "Device not a stream",
    "No data available",
    "Timer expired",
    "Out of streams resources",
    "Machine is not on the network",
    "Package not installed",
    "Object is remote",
    "Link has been severed",
    "Advertise error",
    "Srmount error",
    "Communication error on send",
    "Protocol error",
    "Multihop attempted",
    "RFS specific error",
    "Bad message",
    "Value too large for defined data type",
    "Name not unique on network",
    "File descriptor in bad state",
    "Remote address changed",
    "Can not access a needed shared library",
    "Accessing a corrupted shared library",
    ".lib section in a.out corrupted",
    "Attempting to link in too many shared libraries",
    "Cannot exec a shared library directly",
    "Invalid or incomplete multibyte or wide character",
    "Interrupted system call should be restarted",
    "Streams pipe error",
    "Too many users",
    "Socket operation on non-socket",
    "Destination address required",
    "Message too long",
    "Protocol wrong type for socket",
    "Protocol not available",
    "Protocol not supported",
    "Socket type not supported",
    "Operation not supported",
    "Protocol family not supported",
    "Address family not supported by protocol",
    "Address already in use",
    "Cannot assign requested address",
    "Network is down",
    "Network is unreachable",
    "Network dropped connection on reset",
    "Software caused connection abort",
    "Connection reset by peer",
    "No buffer space available",
    "Transport endpoint is already connected",
    "Transport endpoint is not connected",
    "Cannot send after transport endpoint shutdown",
    "Too many references: cannot splice",
    "Connection timed out",
    "Connection refused",
    "Host is down",
    "No route to host",
    "Operation already in progress",
    "Operation now in progress",
    "Stale file handle",
    "Structure needs cleaning",
    "Not a XENIX named type file",
    "No XENIX semaphores available",
    "Is a named type file",
    "Remote I/O error",
    "Disk quota exceeded",
    "No medium found",
    "Wrong medium type",
    "Operation canceled",
    "Required key not available",
    "Key has expired",
    "Key has been revoked",
    "Key was rejected by service",
    "Owner died",
    "State not recoverable",
    "Operation not possible due to RF-kill",
    "Memory page has hardware error",
];
