//! libcの`strerror`の英語の文面。旧Pythonの`OSError`と旧bashの診断に使う。
//!
//! 生成した表なので手で直さない。生成: `gen/strerror_table.py`(作業場所の外の記録にある)。
//! Linux はUbuntu 24.04のglibc 2.39(`LC_ALL=C`。CIの基準の機械。2026-11-12 08:55の決定)の`os.strerror`、MacはAppleのLibcの`gen/errlst.c`から作った(CIのmacOSで照らす)。
//! muslの`strerror`は文面が違うので使わない(Rustの`std::io::Error`の文も使わない)。

use super::text::{PyText, from_str, repr};

/// glibcの表。添字がエラー番号。`None`は`Unknown error N`。
#[rustfmt::skip]
const GLIBC: &[Option<&str>] = &[
    Some("Success"),
    Some("Operation not permitted"),
    Some("No such file or directory"),
    Some("No such process"),
    Some("Interrupted system call"),
    Some("Input/output error"),
    Some("No such device or address"),
    Some("Argument list too long"),
    Some("Exec format error"),
    Some("Bad file descriptor"),
    Some("No child processes"),
    Some("Resource temporarily unavailable"),
    Some("Cannot allocate memory"),
    Some("Permission denied"),
    Some("Bad address"),
    Some("Block device required"),
    Some("Device or resource busy"),
    Some("File exists"),
    Some("Invalid cross-device link"),
    Some("No such device"),
    Some("Not a directory"),
    Some("Is a directory"),
    Some("Invalid argument"),
    Some("Too many open files in system"),
    Some("Too many open files"),
    Some("Inappropriate ioctl for device"),
    Some("Text file busy"),
    Some("File too large"),
    Some("No space left on device"),
    Some("Illegal seek"),
    Some("Read-only file system"),
    Some("Too many links"),
    Some("Broken pipe"),
    Some("Numerical argument out of domain"),
    Some("Numerical result out of range"),
    Some("Resource deadlock avoided"),
    Some("File name too long"),
    Some("No locks available"),
    Some("Function not implemented"),
    Some("Directory not empty"),
    Some("Too many levels of symbolic links"),
    None,
    Some("No message of desired type"),
    Some("Identifier removed"),
    Some("Channel number out of range"),
    Some("Level 2 not synchronized"),
    Some("Level 3 halted"),
    Some("Level 3 reset"),
    Some("Link number out of range"),
    Some("Protocol driver not attached"),
    Some("No CSI structure available"),
    Some("Level 2 halted"),
    Some("Invalid exchange"),
    Some("Invalid request descriptor"),
    Some("Exchange full"),
    Some("No anode"),
    Some("Invalid request code"),
    Some("Invalid slot"),
    None,
    Some("Bad font file format"),
    Some("Device not a stream"),
    Some("No data available"),
    Some("Timer expired"),
    Some("Out of streams resources"),
    Some("Machine is not on the network"),
    Some("Package not installed"),
    Some("Object is remote"),
    Some("Link has been severed"),
    Some("Advertise error"),
    Some("Srmount error"),
    Some("Communication error on send"),
    Some("Protocol error"),
    Some("Multihop attempted"),
    Some("RFS specific error"),
    Some("Bad message"),
    Some("Value too large for defined data type"),
    Some("Name not unique on network"),
    Some("File descriptor in bad state"),
    Some("Remote address changed"),
    Some("Can not access a needed shared library"),
    Some("Accessing a corrupted shared library"),
    Some(".lib section in a.out corrupted"),
    Some("Attempting to link in too many shared libraries"),
    Some("Cannot exec a shared library directly"),
    Some("Invalid or incomplete multibyte or wide character"),
    Some("Interrupted system call should be restarted"),
    Some("Streams pipe error"),
    Some("Too many users"),
    Some("Socket operation on non-socket"),
    Some("Destination address required"),
    Some("Message too long"),
    Some("Protocol wrong type for socket"),
    Some("Protocol not available"),
    Some("Protocol not supported"),
    Some("Socket type not supported"),
    Some("Operation not supported"),
    Some("Protocol family not supported"),
    Some("Address family not supported by protocol"),
    Some("Address already in use"),
    Some("Cannot assign requested address"),
    Some("Network is down"),
    Some("Network is unreachable"),
    Some("Network dropped connection on reset"),
    Some("Software caused connection abort"),
    Some("Connection reset by peer"),
    Some("No buffer space available"),
    Some("Transport endpoint is already connected"),
    Some("Transport endpoint is not connected"),
    Some("Cannot send after transport endpoint shutdown"),
    Some("Too many references: cannot splice"),
    Some("Connection timed out"),
    Some("Connection refused"),
    Some("Host is down"),
    Some("No route to host"),
    Some("Operation already in progress"),
    Some("Operation now in progress"),
    Some("Stale file handle"),
    Some("Structure needs cleaning"),
    Some("Not a XENIX named type file"),
    Some("No XENIX semaphores available"),
    Some("Is a named type file"),
    Some("Remote I/O error"),
    Some("Disk quota exceeded"),
    Some("No medium found"),
    Some("Wrong medium type"),
    Some("Operation canceled"),
    Some("Required key not available"),
    Some("Key has expired"),
    Some("Key has been revoked"),
    Some("Key was rejected by service"),
    Some("Owner died"),
    Some("State not recoverable"),
    Some("Operation not possible due to RF-kill"),
    Some("Memory page has hardware error"),
];

/// Macの表。添字がエラー番号。範囲の外は`Unknown error: N`。
#[rustfmt::skip]
const DARWIN: &[&str] = &[
    "Undefined error: 0",
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
    "Capabilities insufficient",
];

/// glibcの`strerror`(`LC_ALL=C`)。
pub fn glibc(errno: i32) -> String {
    match usize::try_from(errno)
        .ok()
        .and_then(|index| GLIBC.get(index))
    {
        Some(Some(text)) => (*text).to_string(),
        _ => format!("Unknown error {errno}"),
    }
}

/// Macの`strerror`。
pub fn darwin(errno: i32) -> String {
    match usize::try_from(errno)
        .ok()
        .and_then(|index| DARWIN.get(index))
    {
        Some(text) => (*text).to_string(),
        None => format!("Unknown error: {errno}"),
    }
}

/// 動いている機械の旧版のlibc(Linuxはglibc、MacはlibSystem)の`strerror`。
pub fn strerror(errno: i32) -> String {
    if cfg!(target_os = "linux") {
        glibc(errno)
    } else {
        darwin(errno)
    }
}

/// 旧Pythonの`str(OSError)`。ファイル名があれば`repr`を添える。
pub fn os_error(errno: i32, filename: Option<&[u32]>) -> PyText {
    let mut text = from_str(&format!("[Errno {errno}] {}", strerror(errno)));
    if let Some(name) = filename {
        text.extend(from_str(": "));
        text.extend(repr(name));
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_table_matches_the_python_of_this_machine() {
        // 旧Pythonの os.strerror は libc の strerror を呼ぶ。動いている機械の表と全部比べる。
        let output = std::process::Command::new("python3")
            .args([
                "-I",
                "-c",
                "import os\nfor n in range(-2, 140): print(os.strerror(n))",
            ])
            .env("LC_ALL", "C")
            .output()
            .unwrap();
        assert!(output.status.success());
        let expected = String::from_utf8(output.stdout).unwrap();
        let ours: String = (-2..140).map(|n| strerror(n) + "\n").collect();
        assert_eq!(ours, expected);
    }

    #[test]
    fn os_error_matches_python_str() {
        assert_eq!(
            os_error(2, Some(&from_str("git"))),
            from_str("[Errno 2] No such file or directory: 'git'")
        );
        assert_eq!(
            os_error(28, None),
            from_str(&format!("[Errno 28] {}", strerror(28)))
        );
        assert_eq!(glibc(41), "Unknown error 41");
        assert_eq!(
            darwin(DARWIN.len() as i32),
            format!("Unknown error: {}", DARWIN.len())
        );
        assert_eq!(darwin(-1), "Unknown error: -1");
    }
}
