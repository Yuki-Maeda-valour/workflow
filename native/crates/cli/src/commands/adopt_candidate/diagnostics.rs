//! 旧 `adopt-candidate.py` の診断(`ValueError` の文と Python の例外の表示)と、停止時の出力の組立て。
//! 停止の行は `候補の採用を停止: ` に例外の表示を続け、stderr へ UTF-8 + backslashreplace で書く。
use super::bytes::{concat, repr};
use super::index::IndexError;

pub(crate) const LOOP_REFUSAL: &str = "DEV_WORKFLOW_LOOP_ITER があるため候補を採用できない";
pub(crate) const UNATTENDED_REFUSAL: &str = "無人実行では候補を採用できない";
pub(crate) const PARENT_REFERENCE: &str = "親への参照を含むパスは使えない";
pub(crate) const NOT_REGULAR_OR_LARGE: &str = "候補は 8 MiB 以下の通常ファイルだけを使う";
pub(crate) const CHANGED_WHILE_READING: &str = "候補が読取り中に変わった";
pub(crate) const DIGEST_MISMATCH: &str = "候補の sha256 が保持値と違う";
pub(crate) const BLOCKING_PROBLEM: &str = "見送りの行を確認し、削除して commit してから呼び直す";
pub(crate) const NOT_A_CANDIDATE_NAME: &str = "候補_{名}.md を指定する";
pub(crate) const INVALID_ADOPT_NAME: &str = "採用名が不正";
pub(crate) const NAME_COLLISION: &str = "同じ名の状態ファイルがある";
pub(crate) const PRECHECK_REFUSED: &str = "git の事前検査に通らない: ";
pub(crate) const WORKTREE_UNKNOWN: &str = "git の作業ツリーを確認できない";
pub(crate) const CHANGED_BEFORE_ADOPT: &str = "採用直前に候補が変わった";
pub(crate) const GIT_MV_FAILED: &str = "git mv に失敗: ";
pub(crate) const DESTINATION_EXISTS: &str = "改名先が既にある";
pub(crate) const DESTINATION_NOT_OURS: &str = "改名先を自分が作った通常ファイルと確認できない";
pub(crate) const CHANGED_BEFORE_UNLINK: &str = "改名直前に候補が変わった";
pub(crate) const DESTINATION_CHANGED: &str = "改名後に宛先が変わった。候補の残存状態を人が確認する";
pub(crate) const TRACKED_BODY_CHANGED: &str =
    "追跡済み候補の改名後に本文または inode が保持値と違う。候補と index の残存状態を人が確認する";
pub(crate) const TRACKED_INDEX_CHANGED: &str = "追跡済み候補の改名後の index stage が改名前の保持値と違う。候補と index の残存状態を人が確認する";

/// `非原子的な採用を完了できない(...)`。巻戻したかどうかで括弧の中が変わる。
pub(crate) fn non_atomic_failure(rolled_back: bool) -> String {
    let detail = if rolled_back {
        "自身が作った宛先を戻した"
    } else {
        "候補と宛先を保全した"
    };
    format!("非原子的な採用を完了できない({detail})")
}

/// `index_stage_zero` の `ValueError` の文。`when` は `改名前の` か `改名後の`。
pub(crate) fn index_message(error: IndexError, when: &str) -> String {
    match error {
        IndexError::Unavailable => format!("追跡済み候補の{when} index を確認できない"),
        IndexError::NotUnique => format!("追跡済み候補の{when} index が一意でない"),
        IndexError::NotRegularStageZero => {
            format!("追跡済み候補の{when} index が通常ファイル1件でない")
        }
        IndexError::Changed => TRACKED_INDEX_CHANGED.to_owned(),
    }
}

/// `OSError(errno, strerror[, filename])` の `str()`。
pub(crate) fn os_error(errno: i32, strerror: &str, filename: Option<&[u8]>) -> Vec<u8> {
    let head = format!("[Errno {errno}] {strerror}");
    match filename {
        Some(name) => concat(&[head.as_bytes(), b": ", &repr(name)]),
        None => head.into_bytes(),
    }
}

/// ファイル名が 2 つの `OSError`(`link` など)の `str()`: `[Errno N] 説明: 'a' -> 'b'`。
pub(crate) fn os_error_pair(errno: i32, filename: &[u8], filename2: &[u8]) -> Vec<u8> {
    concat(&[
        &os_error(errno, &strerror(errno), Some(filename)),
        b" -> ",
        &repr(filename2),
    ])
}

/// Linux の errno の説明文。基準は CI の Ubuntu の glibc の `strerror`(C ロケール、英語)。
/// 静的リンクの musl の文面とは違うので、Rust がこの表を持って旧版と同じ文を出す(2026-11-12 の共通決定)。
/// 表は glibc 2.35(Ubuntu 22.04)の実値から作った。CI の 24.04(glibc 2.39)の値との照合は未実施。
#[cfg(target_os = "linux")]
const GLIBC_STRERROR: &[(i32, &str)] = &[
    (1, "Operation not permitted"),
    (2, "No such file or directory"),
    (3, "No such process"),
    (4, "Interrupted system call"),
    (5, "Input/output error"),
    (6, "No such device or address"),
    (7, "Argument list too long"),
    (8, "Exec format error"),
    (9, "Bad file descriptor"),
    (10, "No child processes"),
    (11, "Resource temporarily unavailable"),
    (12, "Cannot allocate memory"),
    (13, "Permission denied"),
    (14, "Bad address"),
    (15, "Block device required"),
    (16, "Device or resource busy"),
    (17, "File exists"),
    (18, "Invalid cross-device link"),
    (19, "No such device"),
    (20, "Not a directory"),
    (21, "Is a directory"),
    (22, "Invalid argument"),
    (23, "Too many open files in system"),
    (24, "Too many open files"),
    (25, "Inappropriate ioctl for device"),
    (26, "Text file busy"),
    (27, "File too large"),
    (28, "No space left on device"),
    (29, "Illegal seek"),
    (30, "Read-only file system"),
    (31, "Too many links"),
    (32, "Broken pipe"),
    (33, "Numerical argument out of domain"),
    (34, "Numerical result out of range"),
    (35, "Resource deadlock avoided"),
    (36, "File name too long"),
    (37, "No locks available"),
    (38, "Function not implemented"),
    (39, "Directory not empty"),
    (40, "Too many levels of symbolic links"),
    (42, "No message of desired type"),
    (43, "Identifier removed"),
    (44, "Channel number out of range"),
    (45, "Level 2 not synchronized"),
    (46, "Level 3 halted"),
    (47, "Level 3 reset"),
    (48, "Link number out of range"),
    (49, "Protocol driver not attached"),
    (50, "No CSI structure available"),
    (51, "Level 2 halted"),
    (52, "Invalid exchange"),
    (53, "Invalid request descriptor"),
    (54, "Exchange full"),
    (55, "No anode"),
    (56, "Invalid request code"),
    (57, "Invalid slot"),
    (59, "Bad font file format"),
    (60, "Device not a stream"),
    (61, "No data available"),
    (62, "Timer expired"),
    (63, "Out of streams resources"),
    (64, "Machine is not on the network"),
    (65, "Package not installed"),
    (66, "Object is remote"),
    (67, "Link has been severed"),
    (68, "Advertise error"),
    (69, "Srmount error"),
    (70, "Communication error on send"),
    (71, "Protocol error"),
    (72, "Multihop attempted"),
    (73, "RFS specific error"),
    (74, "Bad message"),
    (75, "Value too large for defined data type"),
    (76, "Name not unique on network"),
    (77, "File descriptor in bad state"),
    (78, "Remote address changed"),
    (79, "Can not access a needed shared library"),
    (80, "Accessing a corrupted shared library"),
    (81, ".lib section in a.out corrupted"),
    (82, "Attempting to link in too many shared libraries"),
    (83, "Cannot exec a shared library directly"),
    (84, "Invalid or incomplete multibyte or wide character"),
    (85, "Interrupted system call should be restarted"),
    (86, "Streams pipe error"),
    (87, "Too many users"),
    (88, "Socket operation on non-socket"),
    (89, "Destination address required"),
    (90, "Message too long"),
    (91, "Protocol wrong type for socket"),
    (92, "Protocol not available"),
    (93, "Protocol not supported"),
    (94, "Socket type not supported"),
    (95, "Operation not supported"),
    (96, "Protocol family not supported"),
    (97, "Address family not supported by protocol"),
    (98, "Address already in use"),
    (99, "Cannot assign requested address"),
    (100, "Network is down"),
    (101, "Network is unreachable"),
    (102, "Network dropped connection on reset"),
    (103, "Software caused connection abort"),
    (104, "Connection reset by peer"),
    (105, "No buffer space available"),
    (106, "Transport endpoint is already connected"),
    (107, "Transport endpoint is not connected"),
    (108, "Cannot send after transport endpoint shutdown"),
    (109, "Too many references: cannot splice"),
    (110, "Connection timed out"),
    (111, "Connection refused"),
    (112, "Host is down"),
    (113, "No route to host"),
    (114, "Operation already in progress"),
    (115, "Operation now in progress"),
    (116, "Stale file handle"),
    (117, "Structure needs cleaning"),
    (118, "Not a XENIX named type file"),
    (119, "No XENIX semaphores available"),
    (120, "Is a named type file"),
    (121, "Remote I/O error"),
    (122, "Disk quota exceeded"),
    (123, "No medium found"),
    (124, "Wrong medium type"),
    (125, "Operation canceled"),
    (126, "Required key not available"),
    (127, "Key has expired"),
    (128, "Key has been revoked"),
    (129, "Key was rejected by service"),
    (130, "Owner died"),
    (131, "State not recoverable"),
    (132, "Operation not possible due to RF-kill"),
    (133, "Memory page has hardware error"),
];

/// errno の説明文。Linux は基準の表、Mac は OS の `strerror`(Python と同じ関数)。
pub(crate) fn strerror(errno: i32) -> String {
    #[cfg(target_os = "linux")]
    {
        match GLIBC_STRERROR.iter().find(|(number, _)| *number == errno) {
            Some((_, message)) => (*message).to_owned(),
            None => format!("Unknown error {errno}"),
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        let text = std::io::Error::from_raw_os_error(errno).to_string();
        text.split(" (os error ").next().unwrap_or(&text).to_owned()
    }
}

/// platform の失敗(errno とファイル名)を、Python の `OSError` の表示にする。
pub(crate) fn os_failure(failure: &dev_workflow_platform::adopt_candidate::Failure) -> Vec<u8> {
    match (&failure.filename, &failure.filename2) {
        (Some(first), Some(second)) => os_error_pair(failure.errno, first, second),
        (first, _) => os_error(failure.errno, &strerror(failure.errno), first.as_deref()),
    }
}

/// `std::io::Error` から errno と OS の説明文を取り、Python の `OSError` と同じ表示にする。
pub(crate) fn os_error_from(error: &std::io::Error, filename: Option<&[u8]>) -> Vec<u8> {
    let errno = error.raw_os_error().unwrap_or(0);
    os_error(errno, &strerror(errno), filename)
}

/// `subprocess.TimeoutExpired` の `str()`。引数列は文字列のリストの `repr` で表示する。
pub(crate) fn timeout_message(argv: &[Vec<u8>], seconds: u32) -> Vec<u8> {
    let items = argv.iter().map(|item| repr(item)).collect::<Vec<_>>();
    concat(&[
        b"Command '[",
        &items.join(&b", "[..]),
        b"]' timed out after ",
        seconds.to_string().as_bytes(),
        b" seconds",
    ])
}

/// `data.decode('utf-8')` の `UnicodeDecodeError` の `str()`。
pub(crate) fn utf8_decode_message(data: &[u8], error: std::str::Utf8Error) -> String {
    let start = error.valid_up_to();
    let end = error.error_len().map_or(data.len(), |n| start + n);
    let reason = if error.error_len().is_none() {
        "unexpected end of data"
    } else if !(0xc2..=0xf4).contains(&data[start]) {
        "invalid start byte"
    } else {
        "invalid continuation byte"
    };
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

#[cfg(test)]
mod tests {
    use super::*;

    fn utf8_error(bytes: &[u8]) -> std::str::Utf8Error {
        std::str::from_utf8(std::hint::black_box(bytes)).unwrap_err()
    }

    #[test]
    fn errno_texts_follow_the_reference_glibc_not_the_linked_libc() {
        #[cfg(target_os = "linux")]
        for (errno, text) in [
            (2, "No such file or directory"),
            (13, "Permission denied"),
            (17, "File exists"),
            (20, "Not a directory"),
            (21, "Is a directory"),
            (27, "File too large"),
            (28, "No space left on device"),
            (32, "Broken pipe"),
            (40, "Too many levels of symbolic links"),
            (28 + 1000, "Unknown error 1028"),
        ] {
            assert_eq!(strerror(errno), text);
        }
    }

    #[test]
    fn python_exception_texts_match_the_reference() {
        assert_eq!(
            os_error(2, "No such file or directory", Some(b"bash")),
            b"[Errno 2] No such file or directory: 'bash'"
        );
        assert_eq!(
            os_error(2, "No such file or directory", None),
            b"[Errno 2] No such file or directory"
        );
        assert_eq!(
            timeout_message(
                &[
                    b"git".to_vec(),
                    b"a b".to_vec(),
                    "é\u{ff}".as_bytes().to_vec()
                ],
                30
            ),
            "Command '['git', 'a b', 'é\u{ff}']' timed out after 30 seconds".as_bytes()
        );
        let error = utf8_error(b"ab\xe3x");
        assert_eq!(
            utf8_decode_message(b"ab\xe3x", error),
            "'utf-8' codec can't decode byte 0xe3 in position 2: invalid continuation byte"
        );
        let error = utf8_error(b"\xff");
        assert_eq!(
            utf8_decode_message(b"\xff", error),
            "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
        );
        let error = utf8_error(b"a\xe3\x81");
        assert_eq!(
            utf8_decode_message(b"a\xe3\x81", error),
            "'utf-8' codec can't decode bytes in position 1-2: unexpected end of data"
        );
    }

    #[test]
    fn index_messages_distinguish_before_and_after_the_move() {
        assert_eq!(
            index_message(IndexError::Unavailable, "改名前の"),
            "追跡済み候補の改名前の index を確認できない"
        );
        assert_eq!(
            index_message(IndexError::Changed, "改名後の"),
            TRACKED_INDEX_CHANGED
        );
    }
}
